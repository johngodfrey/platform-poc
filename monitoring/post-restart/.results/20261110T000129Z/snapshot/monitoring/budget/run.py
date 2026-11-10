#!/usr/bin/env python3
"""Fixed application-budget capacity test. Plan first; run changes resources and restores them."""
import argparse
import copy
import csv
from datetime import datetime, timezone
from decimal import Decimal
import html
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
import urllib.error

ROOT = Path(__file__).resolve().parent
K = ['kubectl', '--context', 'k3d-platform-poc', '--request-timeout=20s']
JAVA = {'inventory-service', 'notification-service', 'order-service', 'product-service'}

def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)

def get(namespace, name):
    return json.loads(subprocess.check_output(K + ['-n', namespace, 'get', 'deployment', name, '-o', 'json'], text=True))

def container(deployment, name):
    return next(c for c in deployment['spec']['template']['spec']['containers'] if c['name'] == name)

def quantity(value, cpu=False):
    match = re.fullmatch(r'([0-9.]+)([A-Za-z]*)', str(value))
    if not match:
        raise ValueError('Unsupported resource quantity: ' + str(value))
    number, suffix = match.groups()
    units = {'': 1, 'm': Decimal('.001')} if cpu else {'': 1, 'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'Ti': 1024**4}
    return Decimal(number) * units[suffix]

def same_resources(left, right):
    left, right = copy.deepcopy(left), copy.deepcopy(right)
    for data in [left, right]:
        for scope in ['requests', 'limits']:
            for key, value in data.get(scope, {}).items():
                if key in ['cpu', 'memory']:
                    data[scope][key] = quantity(value, key == 'cpu')
    return left == right

def records_for(budget, implementations):
    result = []
    for implementation in implementations:
        allocations = budget[implementation]
        if sum(a['cpu_millicores'] for a in allocations) != budget['cpu_millicores']:
            raise ValueError('CPU allocations must sum to the budget for ' + implementation)
        if sum(a['memory_mib'] for a in allocations) != budget['memory_mib']:
            raise ValueError('Memory allocations must sum to the budget for ' + implementation)
        expected = JAVA if implementation == 'java' else {'wasmcloud-host'}
        if {a['deployment'] for a in allocations} != expected or len(allocations) != len(expected):
            raise ValueError('Budget must contain exactly the application deployments for ' + implementation)
        for allocation in allocations:
            if min(allocation['cpu_millicores'], allocation['memory_mib']) <= 0:
                raise ValueError('Budget allocations must be positive')
            deployment = get(implementation, allocation['deployment'])
            if deployment['spec'].get('replicas', 1) != 1:
                raise ValueError('This test requires one replica per application deployment')
            if implementation == 'java' and allocation['deployment'] == 'inventory-service' and deployment['spec'].get('strategy',{}).get('type') != 'Recreate':
                raise ValueError('Inventory uses create-drop and requires strategy Recreate before testing. Apply the updated Java local overlay; rolling updates can drop the replacement pod\'s tables.')
            current = container(deployment, allocation['container'])
            original = current.get('resources', {})
            resources = copy.deepcopy(original)
            for scope in ['limits', 'requests']:
                resources.setdefault(scope, {}).update(cpu=str(allocation['cpu_millicores'])+'m', memory=str(allocation['memory_mib'])+'Mi')
            result.append(dict(namespace=implementation, deployment=allocation['deployment'], container=allocation['container'],
                               uid=deployment['metadata']['uid'], original=original, budget=resources))
    return result

def patch(record, restore=False, dry_run=False):
    deployment = get(record['namespace'], record['deployment'])
    if deployment['metadata']['uid'] != record['uid']:
        raise RuntimeError('Deployment was recreated; refusing to overwrite ' + record['deployment'])
    current = container(deployment, record['container']).get('resources', {})
    expected, desired = (record['budget'], record['original']) if restore else (record['original'], record['budget'])
    if same_resources(current, desired):
        return
    if not same_resources(current, expected):
        raise RuntimeError('Resources changed concurrently; refusing to overwrite ' + record['deployment'])
    index = next(i for i,c in enumerate(deployment['spec']['template']['spec']['containers']) if c['name'] == record['container'])
    operations = [{'op':'test','path':'/metadata/resourceVersion','value':deployment['metadata']['resourceVersion']},
                  {'op':'add','path':f'/spec/template/spec/containers/{index}/resources','value':desired}]
    args = K + ['-n', record['namespace'], 'patch', 'deployment', record['deployment'], '--type=json', '-p', json.dumps(operations)]
    if dry_run:
        args.append('--dry-run=server')
    command(args, stdout=subprocess.DEVNULL)

def rollout(records):
    for record in records:
        # A rollout watch must outlive the short timeout used for ordinary API reads.
        command(K + ['--request-timeout=260s', '-n', record['namespace'], 'rollout', 'status', 'deployment/'+record['deployment'], '--timeout=240s'], timeout=270)
        # rollout status can complete while an old pod is still terminating.
        # Such a pod is still outside the intended one-replica budget.
        deadline = time.monotonic()+90
        while True:
            deployment = get(record['namespace'],record['deployment'])
            pods = pods_for(record['namespace'],deployment)
            if len(pods)==1 and not pods[0]['metadata'].get('deletionTimestamp'):
                break
            if time.monotonic()>deadline:
                raise RuntimeError('Old pods did not terminate for '+record['deployment'])
            time.sleep(2)

def pods_for(namespace, deployment):
    labels = deployment['spec']['selector']['matchLabels']
    selector = ','.join(k+'='+v for k,v in labels.items())
    return json.loads(subprocess.check_output(K+['-n',namespace,'get','pods','-l',selector,'-o','json'],text=True))['items']

def restore(state):
    errors = []
    for record in state['records']:
        try:
            patch(record, restore=True)
        except Exception as error:
            errors.append(str(error))
    if errors:
        raise RuntimeError('Restore needs attention: ' + '; '.join(errors))
    rollout(state['records'])

def pod_state(records, require_ready=True):
    states = []
    for record in records:
        deployment = get(record['namespace'], record['deployment'])
        if not same_resources(container(deployment,record['container']).get('resources',{}), record['budget']):
            raise RuntimeError('Budget was changed during the test: '+record['deployment'])
        pods = pods_for(record['namespace'],deployment)
        if len(pods) != 1 or pods[0]['metadata'].get('deletionTimestamp'):
            raise RuntimeError('Expected one live pod for '+record['deployment'])
        pod = pods[0]
        statuses = pod['status'].get('containerStatuses',[])
        ready=bool(statuses) and all(c['ready'] for c in statuses)
        if require_ready and not ready:
            raise RuntimeError('Pod not ready: '+pod['metadata']['name'])
        running_container = next(c for c in pod['spec']['containers'] if c['name'] == record['container'])
        if not same_resources(running_container.get('resources',{}),record['budget']):
            raise RuntimeError('Running pod has not adopted the budget')
        states.append((pod['metadata']['uid'], sum(c['restartCount'] for c in statuses),ready))
    return states

def functional_check(implementation):
    """Require a real accepted order; Kubernetes readiness can miss schema faults."""
    if implementation == 'java':
        inventory_url='http://localhost:8081/api/inventory?skuCode=iphone_13'
        try:
            with urllib.request.urlopen(inventory_url,timeout=10) as response:
                inventory=json.load(response)
            if not any(row.get('skuCode')=='iphone_13' and row.get('inStock') for row in inventory):
                raise RuntimeError('Java demo inventory is not in stock')
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'Java inventory preflight returned HTTP {error.code}; inspect inventory logs/schema before benchmarking') from error
        url='http://localhost:8081/api/order'
        payload={'orderLineItemsDtoList':[{'skuCode':'iphone_13','price':200,'quantity':1}]}
    else:
        url='http://localhost:8080/orders'
        payload=[{'sku':'ENG-V8-500','price':8500,'quantity':1}]
    request=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=20) as response:
            body=response.read().decode()
            if response.status!=201 or (implementation=='java' and 'Placed Successfully' not in body):
                raise RuntimeError(implementation+' functional preflight did not accept the order')
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'{implementation} order preflight returned HTTP {error.code}; inspect application logs before benchmarking') from error
    print(implementation+' functional preflight: order accepted',flush=True)

def wait_functional(implementation):
    deadline=time.monotonic()+60
    while True:
        try:
            functional_check(implementation)
            return
        except (RuntimeError, urllib.error.URLError, TimeoutError):
            if time.monotonic()>=deadline:
                raise
            time.sleep(2)

def capture_logs(records, output):
    for record in records:
        try:
            result=subprocess.run(K+['-n',record['namespace'],'logs','deployment/'+record['deployment'],
                                  '-c',record['container'],'--since=5m','--tail=200'],
                                  text=True,capture_output=True,timeout=25)
            content=result.stdout+result.stderr
        except (OSError, subprocess.TimeoutExpired) as error:
            content='Could not capture diagnostic logs: '+str(error)
        (output/(record['deployment']+'.log')).write_text(content)

def result_from(summary, code, rate, seconds, p95_ms, integrity=True, readiness_lost=False):
    metrics = summary['metrics']
    requests = metrics.get('http_reqs',{}).get('count',0)
    failures = metrics.get('demo_order_failures',{}).get('count',0)
    accepted = metrics.get('demo_orders_accepted',{}).get('count',0)
    drops = metrics.get('dropped_iterations',{}).get('count',0)
    http_failure_rate = metrics.get('http_req_failed',{}).get('value', 1)
    p95 = metrics.get('http_req_duration',{}).get('p(95)')
    passed = (code == 0 and integrity and not readiness_lost and failures == 0 and http_failure_rate == 0 and drops == 0 and accepted == requests
              and accepted >= rate*seconds-1 and p95 is not None and p95 < p95_ms)
    return dict(rate=rate, requests=requests, accepted=accepted, failures=failures, dropped=drops,
                p95_ms=p95, achieved_orders_per_second=accepted/seconds, passed=passed,
                budget_and_pods_unchanged=integrity, readiness_lost=readiness_lost, http_failure_rate=http_failure_rate, exit_code=code)

def step(args, implementation, rate, output, records, warmup=False):
    output.mkdir()
    seconds = args.warmup if warmup else args.seconds
    environment = dict(os.environ, K6_PROMETHEUS_RW_SERVER_URL='http://localhost:8080/prometheus/api/v1/write',
                       K6_PROMETHEUS_RW_TREND_STATS='p(95),avg,max', K6_PROMETHEUS_RW_STALE_MARKERS='true')
    argv = ['k6','run','--quiet','-o','experimental-prometheus-rw','--summary-export='+str(output/'summary.json'),
            '-e','IMPLEMENTATION='+implementation,'-e','RUN_ID='+getattr(args,'run_prefix','budget')+'-'+output.parent.name+'-'+output.name,
            '-e','IDLE_SECONDS=5','-e','LOAD_SECONDS='+str(seconds),'-e','RECOVERY_SECONDS=10',
            '-e','ORDER_RATE='+str(rate),'-e','ORDER_VUS='+str(args.vus),
            '-e','P95_MS='+('1000000' if warmup else str(args.p95_ms)),
            str(ROOT.parent/'footprint/footprint.js')]
    for key, value in getattr(args, 'extra_env', {}).items():
        argv[-1:-1] = ['-e', key+'='+str(value)]
    baseline = [state[:2] for state in pod_state(records)]
    deadline = time.monotonic()+seconds+55
    with (output/'k6.log').open('w') as log, (output/'resources.jsonl').open('w') as samples:
        process = subprocess.Popen(argv,env=environment,stdout=log,stderr=subprocess.STDOUT)
        integrity = True
        readiness_lost = False
        try:
            while process.poll() is None:
                if time.monotonic()>deadline:
                    raise RuntimeError('k6 exceeded bounded stage execution time')
                current = pod_state(records,require_ready=False)
                integrity = integrity and [state[:2] for state in current] == baseline
                readiness_lost = readiness_lost or not all(state[2] for state in current)
                top = subprocess.run(K+['-n',implementation,'top','pods','--containers','--no-headers'],capture_output=True,text=True,timeout=15)
                samples.write(json.dumps({'time':datetime.now(timezone.utc).isoformat(),'resources':top.stdout,'metrics_error':top.stderr})+'\n'); samples.flush()
                if not integrity:
                    raise RuntimeError('A pod restarted or was replaced during the stage')
                time.sleep(5)
            code = process.wait()
            current = pod_state(records,require_ready=False)
            integrity = integrity and [state[:2] for state in current] == baseline
            readiness_lost = readiness_lost or not all(state[2] for state in current)
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
    summary = json.loads((output/'summary.json').read_text())
    result = result_from(summary,code,rate,seconds,1000000 if warmup else args.p95_ms,integrity,readiness_lost)
    (output/'result.json').write_text(json.dumps(result,indent=2))
    if not result['passed']:
        capture_logs(records,output)
    print(json.dumps(dict(implementation=implementation,warmup=warmup,**result)),flush=True)
    return result

def write_report(directory, report):
    (directory/'report.json').write_text(json.dumps(report,indent=2))
    rows = [dict(implementation=impl,**row) for impl, data in report['implementations'].items() for row in data['stages']]
    if rows:
        with (directory/'stages.csv').open('w') as f:
            writer = csv.DictWriter(f,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    cards=[]
    for impl,data in report['implementations'].items():
        passing=[s['rate'] for s in data['stages'] if s['passed']]
        highest=str(max(passing)) if passing else '—'
        cards.append(f'<article><h2>{html.escape(impl)}</h2><strong>{highest}</strong> orders/s<br>Highest passing tested rate</article>')
    headings=['implementation','rate','accepted','dropped','failures','p95_ms','passed']
    table=''.join('<tr>'+''.join('<td>'+html.escape(str(row.get(k,'')))+'</td>' for k in headings)+'</tr>' for row in rows)
    status = 'Original resource settings restored.' if report['restored'] else 'Experiment in progress; resource settings have not yet been restored.'
    if report.get('error'):
        status='Experiment aborted: '+str(report['error'])+' '+status
    page=f"""<!doctype html>
<meta charset="utf-8"><title>Fixed-budget capacity</title>
<style>
body{{font:18px system-ui;background:#101827;color:#eef;padding:36px;max-width:1100px;margin:auto}}
section{{display:flex;gap:24px}} article{{padding:24px;background:#22314b;border-radius:12px;flex:1}}
strong{{font-size:60px;color:#80d7aa}} table{{border-collapse:collapse;width:100%;margin-top:32px}}
td,th{{padding:12px;border-bottom:1px solid #456;text-align:left}}
</style>
<h1>Fixed-budget application capacity</h1>
<p>{report['budget']['cpu_millicores']/1000} cores and {report['budget']['memory_mib']} MiB per implementation.
 p95 &lt; {report['p95_ms']} ms; zero failures or drops.</p>
<p>{html.escape(status)}</p>
<section>{''.join(cards)}</section>
<table><tr><th>Implementation</th><th>Offered/s</th><th>Accepted</th><th>Dropped</th><th>Failed</th><th>p95 ms</th><th>Pass</th></tr>{table}</table>
<p>This is highest passing tested throughput, not a count of independent application copies.
Java limits are partitioned across four containers; Wasm shares its limit inside one host container.
Databases, messaging/control-plane infrastructure and monitoring are outside the budget.
See report.json for allocations and test settings. Partial/error results do not establish capacity.</p>
"""
    temporary=directory/'report.html.tmp'
    temporary.write_text(page)
    temporary.replace(directory/'report.html')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['plan','run','restore'])
    parser.add_argument('target',nargs='?',default='both',help='java, wasm, both; or state.json for restore')
    parser.add_argument('--budget',type=Path,default=ROOT/'budget.json')
    parser.add_argument('--rates',default='1,2,5,10,15,20,25,30,35,40,45,50',
                        help='increasing orders/s ladder (default: 1,2,5,10,15,20,25,30,35,40,45,50)')
    parser.add_argument('--seconds',type=int,default=60)
    parser.add_argument('--warmup',type=int,default=30)
    parser.add_argument('--warmup-rate',type=int,default=10)
    parser.add_argument('--p95-ms',type=float,default=100)
    parser.add_argument('--vus',type=int,default=250)
    args=parser.parse_args()
    if args.action=='restore':
        restore(json.loads(Path(args.target).read_text())); return
    if args.target not in ['java','wasm','both']: parser.error('target must be java, wasm or both')
    try: rates=[int(r) for r in args.rates.split(',')]
    except ValueError: parser.error('rates must be comma-separated integers')
    if not rates or rates != sorted(set(rates)) or min(rates+[args.seconds,args.warmup,args.warmup_rate,args.vus])<1 or not math.isfinite(args.p95_ms) or args.p95_ms<=0:
        parser.error('rates must increase; durations, p95 and VUs must be positive')
    implementations=['java','wasm'] if args.target=='both' else [args.target]
    budget=json.loads(args.budget.read_text())
    records=records_for(budget,implementations)
    if args.action=='plan':
        print(json.dumps(dict(budget=budget,changes=records,rates=rates,p95_ms=args.p95_ms),indent=2))
        for record in records: patch(record,dry_run=True)
        print('Server-side dry run passed. No workloads changed.'); return
    if not shutil.which('k6'): parser.error('k6 must be on PATH')
    with urllib.request.urlopen('http://localhost:8080/prometheus/-/ready',timeout=10) as response:
        if response.status!=200: raise RuntimeError('Prometheus is not ready')
    directory=ROOT/'.results'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory.mkdir(parents=True)
    state={'records':records}; (directory/'state.json').write_text(json.dumps(state,indent=2))
    report={'budget':budget,'rates':rates,'seconds':args.seconds,'warmup_seconds':args.warmup,'warmup_rate':args.warmup_rate,'p95_ms':args.p95_ms,'vus':args.vus,'implementations':{},'restored':False}
    print('Saved resource backup:',directory/'state.json',flush=True)
    print('Applying budgets restarts application pods. Inventory uses create-drop and will recreate its demo tables.',flush=True)
    print('Live dashboard: http://localhost:8080/grafana/d/java-wasm-budget',flush=True)
    guard=subprocess.Popen(['caffeinate','-i','-s']) if sys.platform=='darwin' else None
    try:
        for record in records: patch(record)
        rollout(records)
        for impl in implementations:
            selected=[r for r in records if r['namespace']==impl]
            data=report['implementations'][impl]={'stages':[]}
            wait_functional(impl)
            warm = step(args,impl,args.warmup_rate,directory/(impl+'-warmup'),selected,warmup=True)
            if not warm['passed']:
                raise RuntimeError(impl+' could not sustain the warmup rate under this budget')
            for rate in rates:
                row=step(args,impl,rate,directory/(impl+'-'+str(rate)),selected)
                data['stages'].append(row); write_report(directory,report)
                if not row['passed']: break
            # Do not benchmark the next implementation against a still-overloaded peer.
            wait_functional(impl)
    except BaseException as error:
        report['error']=str(error)
        raise
    finally:
        try:
            restore(state); report['restored']=True
            # Restoration also rolls pods: verify it did not leave a broken app.
            for impl in implementations:
                wait_functional(impl)
            report['restored_applications_healthy']=True
        except BaseException as error:
            report['restoration_error']=str(error)
            if not report.get('error'):
                report['error']=str(error)
            raise
        finally:
            write_report(directory,report)
            if guard: guard.terminate(); guard.wait()
            print('Report:',directory/'report.html',flush=True)
            print('Manual recovery if needed: python3 monitoring/budget/run.py restore '+str(directory/'state.json'),flush=True)

if __name__=='__main__': main()
