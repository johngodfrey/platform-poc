#!/usr/bin/env python3
"""Sweep application memory caps without CPU limits; density is a projection, not a replica test."""
import argparse
import copy
from datetime import datetime, timezone
import html
import importlib.util
import json
import math
import os
import runpy
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('budget', ROOT.parent/'budget/run.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
PROM = 'http://localhost:8080/prometheus'
CPU_QUERY = runpy.run_path(str(ROOT/'dashboard.py'))['CPU_QUERY']


def fetch(path, params=None):
    url = PROM + path + ('?' + urllib.parse.urlencode(params) if params else '')
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


def memory_resources(original, mib, implementation):
    resources = copy.deepcopy(original)
    resources.setdefault('limits', {}).pop('cpu', None)
    resources['limits']['memory'] = f'{mib}Mi'
    resources.setdefault('requests', {}).update(cpu='100m' if implementation == 'java' else '400m', memory=f'{mib}Mi')
    return resources


def records_for(implementations):
    # Reuse deployment/replica/strategy validation and quantity-safe restoration.
    config = json.loads((ROOT.parent/'budget/budget.json').read_text())
    records = b.records_for(config, implementations)
    for record in records:
        record['budget'] = copy.deepcopy(record['original'])
        record['allowed'] = [copy.deepcopy(record['original'])]
    return records


def save(path, data):
    temp = path.with_suffix(path.suffix+'.tmp')
    with temp.open('w') as stream:
        os.chmod(temp, 0o600)
        stream.write(json.dumps(data, indent=2)+'\n')
    temp.replace(path)


def apply_cap(state, path, implementation, total, dry_run=False):
    selected = [r for r in state['records'] if r['namespace'] == implementation]
    for record in selected:
        desired = memory_resources(record['original'], total//len(selected), implementation)
        change = dict(record, original=record['budget'], budget=desired)
        if not dry_run:
            record['allowed'].append(desired)
            save(path, state)  # Recovery knows both old and new values before mutation.
        b.patch(change, dry_run=dry_run)
        if not dry_run:
            record['budget'] = desired
            save(path, state)
    return selected


def restore(state):
    errors = []
    for record in state['records']:
        try:
            deployment = b.get(record['namespace'], record['deployment'])
            current = b.container(deployment, record['container']).get('resources', {})
            if not any(b.same_resources(current, allowed) for allowed in record['allowed']):
                raise RuntimeError('Concurrent resource edit: '+record['deployment'])
            b.patch(dict(record, budget=current), restore=True)
        except Exception as error:
            errors.append(str(error))
    if errors:
        raise RuntimeError('; '.join(errors))
    b.rollout(state['records'])


def cpu_samples(start, end):
    result = fetch('/api/v1/query_range', dict(query=CPU_QUERY, start=start, end=end, step=5))
    if result['status'] != 'success':
        raise RuntimeError('CPU query failed')
    # Match the node set, not just whichever series happened to return data.
    nodes = json.loads(subprocess.check_output(b.K+['get','nodes','-o','json'], text=True))['items']
    expected = {n['metadata']['name'] for n in nodes}
    observed = {s['metric']['node'] for s in result['data']['result']}
    peaks = []
    coverage = {}
    for series in result['data']['result']:
        values = [float(v) for _,v in series['values'] if math.isfinite(float(v))]
        coverage[series['metric']['node']] = len(values)
        peaks.extend(values)
    required = max(1, int((end-start)/5)*0.8)
    valid = observed == expected and all(n >= required for n in coverage.values())
    return dict(complete=valid, max_fraction=max(peaks) if peaks else None, coverage=coverage,
                query=CPU_QUERY, expected_nodes=sorted(expected), samples=result['data']['result'])


def classify(summary, code, args, integrity, ready, cpu):
    result = b.result_from(summary, code, args.rate, args.seconds, args.p95_ms, integrity, not ready)
    headroom = cpu['complete'] and cpu['max_fraction'] is not None and cpu['max_fraction'] < args.max_node_cpu
    result.update(cpu=cpu, cpu_headroom=headroom)
    result['status'] = ('pass' if result['passed'] else 'fail') if headroom else 'inconclusive_cpu'
    result['passed'] = result['passed'] and headroom
    return result


def load(args, implementation, cap, output, selected, warmup=False):
    output.mkdir()
    duration = args.warmup if warmup else args.seconds
    baseline = [s[:2] for s in b.pod_state(selected)]
    env = dict(os.environ, K6_PROMETHEUS_RW_SERVER_URL=PROM+'/api/v1/write',
               K6_PROMETHEUS_RW_TREND_STATS='p(95),avg,max', K6_PROMETHEUS_RW_STALE_MARKERS='true')
    values = dict(IMPLEMENTATION=implementation, RUN_ID='density-'+output.parent.parent.name+'-'+implementation+'-'+str(cap)+'-'+output.name,
                  IDLE_SECONDS=5, LOAD_SECONDS=duration, RECOVERY_SECONDS=5,
                  ORDER_RATE=args.rate, ORDER_VUS=args.vus, P95_MS=1000000 if warmup else args.p95_ms,
                  MEMORY_MIB=cap, POOL_MIB=args.pool_mib, MAX_NODE_CPU=args.max_node_cpu)
    cmd = ['k6','run','--quiet','-o','experimental-prometheus-rw','--summary-export='+str(output/'summary.json')]
    for key,value in values.items(): cmd += ['-e',key+'='+str(value)]
    cmd += [str(ROOT/'load.js')]
    start = time.time()
    integrity, ready = True, True
    with (output/'k6.log').open('w') as log:
        process = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            while process.poll() is None:
                if time.time()-start > duration+55:
                    raise RuntimeError('Bounded load execution expired')
                current = b.pod_state(selected, require_ready=False)
                integrity &= [s[:2] for s in current] == baseline
                ready &= all(s[2] for s in current)
                if not integrity: raise RuntimeError('Application restarted or was replaced during load')
                time.sleep(5)
            code = process.wait()
            current = b.pod_state(selected, require_ready=False)
            integrity &= [s[:2] for s in current] == baseline
            ready &= all(s[2] for s in current)
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
    summary = json.loads((output/'summary.json').read_text())
    cpu = cpu_samples(start+5, min(time.time(), start+5+duration))
    settings = copy.copy(args)
    settings.seconds = duration
    if warmup: settings.p95_ms = 1000000
    result = classify(summary, code, settings, integrity, ready, cpu)
    telemetry = {}
    for name in ['poc_app_memory_bytes', 'poc_app_cpu_cores']:
        data = fetch('/api/v1/query_range', dict(query=name+'{implementation="'+implementation+'"}',
                                                start=start+5, end=start+5+duration, step=5))
        telemetry[name] = data['data']['result']
    save(output/'telemetry.json', telemetry)
    memory = [float(v) for series in telemetry['poc_app_memory_bytes'] for _,v in series['values'] if math.isfinite(float(v))]
    result['peak_application_memory_mib'] = max(memory)/1048576 if memory else None
    save(output/'result.json', result)
    console = copy.deepcopy(result)
    console['cpu'].pop('samples', None)
    print(implementation, cap, output.name, json.dumps(console), flush=True)
    return result


def write_report(directory, report):
    save(directory/'report.json', report)
    rows, cards = [], []
    for implementation, stages in report['implementations'].items():
        passed = [s['memory_mib'] for s in stages if s['status'] == 'pass']
        minimum = min(passed) if passed else None
        cards.append(f'<h2>{implementation}</h2><p>Smallest passing tested allocation: <b>{minimum or "—"} MiB</b>; projected slots in {report["pool_mib"]} MiB: <b>{report["pool_mib"]//minimum if minimum else "—"}</b></p>')
        for stage in stages:
            rows.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in [implementation,stage['memory_mib'],stage['status'],stage.get('reason',''),[round(r.get('p95_ms') or 0,2) for r in stage.get('repeats',[])], [round(r['peak_application_memory_mib'],1) if r.get('peak_application_memory_mib') is not None else '—' for r in stage.get('repeats',[])]])+'</tr>')
    page = '''<!doctype html><meta charset="utf-8"><title>Memory density</title><style>body{font:18px system-ui;background:#101827;color:#eef;max-width:1100px;margin:40px auto}td,th{padding:12px;border-bottom:1px solid #456}table{border-collapse:collapse}</style><h1>Memory density — projected slots</h1>'''
    page += '<p>Fixed '+str(report['rate'])+' orders/s per application. No application CPU limits. Slots are a memory-only projection, not independently deployed or concurrently load-tested stacks. Shared services, control plane, sidecars, monitoring and host overhead need a separate reserve; projected CPU capacity has not been validated.</p>'
    page += '<p>'+html.escape(str({k:report.get(k) for k in ['restored','restored_applications_healthy','error','restoration_error']}))+'</p>'
    page += ''.join(cards)+'<table><tr><th>Application</th><th>MiB</th><th>Status</th><th>Reason</th><th>Repeat p95 ms</th><th>Repeat peak working set MiB</th></tr>'+''.join(rows)+'</table>'
    (directory/'report.html').write_text(page)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan','run','restore'])
    parser.add_argument('target', nargs='?', default='both')
    parser.add_argument('--limits', default='4096,2048,1024,768,512,384,256', help='descending total application MiB; Java splits equally across four services')
    parser.add_argument('--rate', type=int, default=25)
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--warmup', type=int, default=120)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--p95-ms', type=float, default=100)
    parser.add_argument('--vus', type=int, default=250)
    parser.add_argument('--pool-mib', type=int, default=8192, help='RAM reserved for application allocations, excluding infrastructure')
    parser.add_argument('--max-node-cpu', type=float, default=.6, help='maximum sampled node CPU fraction; otherwise inconclusive')
    args = parser.parse_args()
    if args.action == 'restore':
        state = json.loads(Path(args.target).read_text()); restore(state)
        for impl in sorted({r['namespace'] for r in state['records']}): b.wait_functional(impl)
        return
    try: limits = [int(v) for v in args.limits.split(',')]
    except ValueError: parser.error('limits must be comma-separated integer MiB')
    if args.target not in ['java','wasm','both']: parser.error('target must be java, wasm or both')
    if not limits or limits != sorted(set(limits), reverse=True) or min(limits+[args.rate,args.seconds,args.warmup,args.repeats,args.vus,args.pool_mib]) <= 0 or any(v%4 for v in limits):
        parser.error('positive descending unique limits divisible by four and positive durations/rate/repeats/VUs/pool required')
    if not math.isfinite(args.p95_ms) or args.p95_ms <= 0 or not 0 < args.max_node_cpu < 1:
        parser.error('positive p95 and 0 < max-node-cpu < 1 required')
    implementations = ['java','wasm'] if args.target == 'both' else [args.target]
    state = dict(records=records_for(implementations))
    if args.action == 'plan':
        for impl in implementations:
            for cap in limits: apply_cap(state, None, impl, cap, dry_run=True)
        print(json.dumps(dict(limits_mib=limits, settings=vars(args), cpu_limits='removed', java_split='equal four-way'), indent=2))
        print('Server dry runs passed; no workloads changed.'); return
    if not shutil.which('k6'): parser.error('k6 must be installed')
    initial_cpu = cpu_samples(time.time()-40, time.time())
    if not initial_cpu['complete']: raise RuntimeError('Node CPU metrics missing; install updated monitoring first')
    directory = ROOT/'.results'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory.mkdir(parents=True)
    state_path = directory/'state.json'; save(state_path, state)
    report = dict(rate=args.rate, settings=vars(args), pool_mib=args.pool_mib, implementations={}, restored=False)
    guard = subprocess.Popen(['caffeinate','-i','-s']) if sys.platform == 'darwin' else None
    print('Dashboard: http://localhost:8080/grafana/d/java-wasm-memory-density', flush=True)
    print('Resource backup:',state_path,flush=True)
    print('Memory changes restart applications; inventory demo tables are recreated.', flush=True)
    try:
        for impl in implementations:
            stages = report['implementations'][impl] = []
            for cap in limits:
                stage = dict(memory_mib=cap, status='in_progress', repeats=[])
                stages.append(stage)
                selected = [r for r in state['records'] if r['namespace']==impl]
                apply_cap(state, state_path, impl, cap)
                try:
                    b.rollout(selected); b.wait_functional(impl)
                    base = directory/(impl+'-'+str(cap)); base.mkdir()
                    warm = load(args,impl,cap,base/'warmup',selected,warmup=True)
                    if warm['status'] != 'pass':
                        stage.update(status=warm['status'],reason='Warmup failed or CPU headroom unavailable')
                    else:
                        for repeat in range(args.repeats):
                            stage['repeats'].append(load(args,impl,cap,base/('repeat-'+str(repeat+1)),selected))
                        stage['status'] = 'inconclusive_cpu' if any(r['status']=='inconclusive_cpu' for r in stage['repeats']) else ('pass' if all(r['passed'] for r in stage['repeats']) else 'fail')
                        stage['reason'] = {'pass':'All measurement repeats passed', 'fail':'One or more repeats failed latency/reliability/readiness gates', 'inconclusive_cpu':'Missing node telemetry or node CPU reached the ceiling'}[stage['status']]
                except (RuntimeError,subprocess.SubprocessError) as error:
                    stage.update(status='inconclusive_runtime',reason=str(error))
                    # Retain exit reasons (especially OOMKilled) rather than
                    # inferring a memory bound from any rollout timeout.
                    evidence = {}
                    for record in selected:
                        try:
                            dep = b.get(record['namespace'], record['deployment'])
                            evidence[record['deployment']] = b.pods_for(record['namespace'], dep)
                        except (RuntimeError,subprocess.SubprocessError) as failure:
                            evidence[record['deployment']] = {'error':str(failure)}
                    save(directory/(impl+'-'+str(cap)+'-pod-evidence.json'), evidence)
                write_report(directory,report)
                if stage['status'] != 'pass':
                    b.capture_logs(selected,directory)
                    break
            # Restore this implementation before testing its peer, so a failing
            # low-memory app cannot stay in a restart loop during the next test.
            restore(dict(records=selected)); b.wait_functional(impl)
            for record in selected: record['budget'] = copy.deepcopy(record['original'])
            save(state_path,state)
    except BaseException as error:
        report['error'] = str(error)
        raise
    finally:
        try:
            restore(state); report['restored'] = True
            for impl in implementations: b.wait_functional(impl)
            report['restored_applications_healthy'] = True
        except BaseException as error:
            report['restoration_error'] = str(error)
            raise
        finally:
            write_report(directory,report)
            if guard: guard.terminate(); guard.wait()
            print('Report:',directory/'report.html',flush=True)
            print('Recovery: python3 monitoring/density/run.py restore '+str(state_path),flush=True)


if __name__ == '__main__': main()
