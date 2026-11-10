#!/usr/bin/env python3
"""Find highest passing order rates, optionally within an equal application resource budget."""
import argparse
import copy
from datetime import datetime, timezone
import html
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('budget', ROOT.parent/'budget/run.py')
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)


def retry_read(function, *args, **kwargs):
    for attempt in range(3):
        try:
            return function(*args, **kwargs)
        except subprocess.CalledProcessError:
            if attempt == 2: raise
            print('Kubernetes monitoring read failed; retrying', flush=True)
            time.sleep(1)


def snapshot(implementations):
    records = []
    for impl in implementations:
        for name in sorted(b.JAVA if impl == 'java' else {'wasmcloud-host'}):
            deployment = b.get(impl, name)
            if deployment['spec'].get('replicas',1) != 1:
                raise RuntimeError('Requires one replica per application deployment')
            current = b.container(deployment,name)
            resources = copy.deepcopy(current.get('resources',{}))
            records.append(dict(namespace=impl,deployment=name,container=name,uid=deployment['metadata']['uid'],
                                original=resources,budget=resources,image=current['image']))
    return records


def classify(result):
    if result['passed']: return 'pass'
    if not result['budget_and_pods_unchanged'] or result.get('readiness_lost'): return 'unhealthy'
    if result['dropped']:
        return 'inconclusive_dropped_work'
    return 'fails_slo'


def physical():
    if sys.platform != 'darwin': return
    power=subprocess.check_output(['pmset','-g','batt'],text=True,timeout=5)
    lid=subprocess.check_output(['ioreg','-r','-k','AppleClamshellState','-d','4'],text=True,timeout=5)
    if "'AC Power'" not in power or re.search(r'"AppleClamshellState"\s*=\s*Yes',lid):
        raise RuntimeError('Environmental interruption: mains disconnected or lid closed')


def save(path, value):
    path=Path(path)
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary,0o600)
        json.dump(value,stream,indent=2)
    temporary.replace(path)


def write_report(directory,report):
    (directory/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    rows, cards = [], []
    for impl, data in report['implementations'].items():
        passing = [r['rate'] for r in data['stages'] if r['passed']]
        cards.append('<h2>'+impl+'</h2><p>Highest passing tested rate: <b>'+str(max(passing) if passing else '—')+' orders/s</b></p>')
        for r in data['stages']+([data['confirmation']] if data.get('confirmation') else []):
            rows.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in [impl+(' (5-minute confirmation)' if r.get('confirmation') and report['settings'].get('confirm_seconds')==300 else ' (confirmation)' if r.get('confirmation') else ''),r['rate'],r['accepted'],round(r['p95_ms'],2) if r.get('p95_ms') is not None else '—',r['failures'],r['dropped'],r['status']])+'</tr>')
    page='''<!doctype html><meta charset="utf-8"><title>Order-rate saturation</title><style>body{font:18px system-ui;background:#101827;color:#eef;max-width:1100px;margin:40px auto}td,th{padding:12px;border-bottom:1px solid #456}table{border-collapse:collapse}</style><h1>Order-rate saturation</h1>'''
    allocation=('Equal application budget: '+str(report['budget']['cpu_millicores']/1000)+' cores and '+str(report['budget']['memory_mib'])+' MiB. Java divides its budget across four services; Wasm shares its budget in one host. Shared infrastructure is outside the budget.' if report.get('budget') else 'Current deployment resources unchanged.')
    page += '<p>'+allocation+' p95 &lt; '+str(report['settings']['p95_ms'])+' ms; zero failed or dropped orders. No retries. Same worker limit and rate ladder for both applications.</p>'
    page += '<p>Shared databases, messaging, ingress, host VM and load generator can constrain results. This measures the complete local request path, not an intrinsic language limit. Dropped work is inconclusive about application capacity. A passing top step is a lower bound, not the maximum.</p>'
    page += '<p>'+html.escape(str({k:report.get(k) for k in ['completed','applications_healthy','restored','error','recovery_error']}))+'</p>'
    page += ''.join(cards)+'<table><tr><th>Application</th><th>Offered/s</th><th>Accepted</th><th>p95 ms</th><th>Failures</th><th>Dropped</th><th>Status</th></tr>'+''.join(rows)+'</table>'
    if not report.get('completed') and not report.get('error'):page=page.replace('<title>','<meta http-equiv="refresh" content="15"><title>',1)
    (directory/'report.html').write_text(page)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('target',nargs='?',default='both',choices=['java','wasm','both'])
    p.add_argument('--rates',default='100,200,400,800,1600')
    p.add_argument('--seconds',type=int,default=60)
    p.add_argument('--warmup',type=int,default=120)
    p.add_argument('--warmup-rate',type=int,default=100)
    p.add_argument('--p95-ms',type=float,default=500)
    p.add_argument('--vus',type=int,default=250)
    p.add_argument('--budget',type=Path,help='Apply equal total application CPU/RAM allocations and restore originals afterward')
    p.add_argument('--confirm-seconds',type=int,default=0,help='Re-test the highest passing step for this many seconds (0 disables)')
    args=p.parse_args()
    try: rates=[int(v) for v in args.rates.split(',')]
    except ValueError: p.error('rates must be comma-separated integers')
    if not rates or rates != sorted(set(rates)) or min(rates+[args.seconds,args.warmup,args.warmup_rate,args.vus]) < 1 or args.confirm_seconds<0 or not math.isfinite(args.p95_ms) or args.p95_ms<=0:
        p.error('positive increasing unique rates and positive settings required')
    if not shutil.which('k6'): p.error('k6 must be installed')
    implementations=['java','wasm'] if args.target=='both' else [args.target]
    original_get, original_pods = b.get, b.pods_for
    b.get = lambda *a, **kw: retry_read(original_get, *a, **kw)
    b.pods_for = lambda *a, **kw: retry_read(original_pods, *a, **kw)
    physical()
    budget=json.loads(args.budget.read_text()) if args.budget else None
    records=b.records_for(budget,implementations) if budget else snapshot(implementations)
    if budget:
        for record in records:record['image']=b.container(b.get(record['namespace'],record['deployment']),record['container'])['image']
    with urllib.request.urlopen('http://localhost:8080/prometheus/-/ready',timeout=10): pass
    directory=ROOT/'.results'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');directory.mkdir(parents=True)
    save(directory/'deployments.json',records)
    settings=vars(args).copy();settings['budget']=str(args.budget) if args.budget else None
    report=dict(settings=settings,budget=budget,implementations={},completed=False,restored=not bool(budget))
    save(directory/'state.json',dict(records=records))
    args.run_prefix='capacity';args.extra_env=dict(REQUEST_TIMEOUT='10s')
    guard=subprocess.Popen(['caffeinate','-i','-s']) if sys.platform=='darwin' else None
    print('Report:',directory/'report.html',flush=True)
    print('Dashboard: http://localhost:8080/grafana/d/java-wasm-capacity',flush=True)
    write_report(directory,report)
    original_state=b.pod_state
    def guarded_state(*a,**kw):
        physical()
        return original_state(*a,**kw)
    b.pod_state=guarded_state
    try:
        if budget:
            for record in records:b.patch(record)
            b.rollout(records)
        for impl in implementations:
            selected=[r for r in records if r['namespace']==impl]
            data=report['implementations'][impl]=dict(stages=[])
            b.wait_functional(impl)
            data['warmup']=b.step(args,impl,args.warmup_rate,directory/(impl+'-warmup'),selected,warmup=True)
            if not data['warmup']['passed']: raise RuntimeError(impl+' warmup failed; no capacity inference')
            for rate in rates:
                result=b.step(args,impl,rate,directory/(impl+'-'+str(rate)),selected)
                result['status']=classify(result)
                summary=json.loads((directory/(impl+'-'+str(rate))/'summary.json').read_text())['metrics']
                result['median_ms']=summary.get('http_req_duration',{}).get('med')
                result['max_ms']=summary.get('http_req_duration',{}).get('max')
                data['stages'].append(result);write_report(directory,report)
                if not result['passed']: break
            passing=[r['rate'] for r in data['stages'] if r['passed']]
            if args.confirm_seconds and passing:
                seconds=args.seconds
                try:
                    args.seconds=args.confirm_seconds
                    result=b.step(args,impl,max(passing),directory/(impl+'-confirmation'),selected)
                    result.update(status=classify(result),confirmation=True,seconds=args.confirm_seconds)
                    data['confirmation']=result;write_report(directory,report)
                finally:args.seconds=seconds
            b.wait_functional(impl)
        report['completed']=True
    except BaseException as error:
        report['error']=str(error);raise
    finally:
        b.pod_state=original_state
        try:
            if budget:
                b.restore(dict(records=records));report['restored']=True
            for impl in implementations: b.wait_functional(impl)
            for record in records:
                dep=b.get(record['namespace'],record['deployment'])
                if dep['metadata']['uid']!=record['uid'] or b.container(dep,record['container'])['image']!=record['image'] or not b.same_resources(b.container(dep,record['container']).get('resources',{}),record['original']):
                    raise RuntimeError('Deployment/resources changed during test')
            report['applications_healthy']=True
        except BaseException as error:
            report['recovery_error']=str(error)
        write_report(directory,report)
        if guard: guard.terminate();guard.wait()
        print('Report:',directory/'report.html',flush=True)
    if report.get('recovery_error'): raise RuntimeError(report['recovery_error'])


if __name__=='__main__': main()
