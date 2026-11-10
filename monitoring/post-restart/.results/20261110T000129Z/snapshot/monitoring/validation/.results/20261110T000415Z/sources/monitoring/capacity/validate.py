#!/usr/bin/env python3
"""Allocation sensitivity, alternating throughput repeats, then 30-minute stability."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent

def load(path):
    return json.loads(Path(path).read_text())

def save(path, value):
    temporary=Path(path).with_suffix('.tmp')
    temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.replace(path)

def module():
    spec=importlib.util.spec_from_file_location('capacity_driver',ROOT/'run.py')
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result

def query_range(query, start, end):
    url='http://localhost:8080/prometheus/api/v1/query_range?'+urllib.parse.urlencode(dict(query=query,start=start,end=end,step=15))
    with urllib.request.urlopen(url,timeout=20) as response: return json.load(response)

def counters(driver, implementation):
    data=[]
    nodes=json.loads(subprocess.check_output(driver.b.K+['get','nodes','-o','json'],text=True,timeout=25))['items']
    for node in nodes:
        name=node['metadata']['name']
        raw=subprocess.check_output(driver.b.K+['get','--raw','/api/v1/nodes/'+name+'/proxy/metrics/cadvisor'],text=True,timeout=25)
        data.extend(dict(node=name,line=line) for line in raw.splitlines()
                    if line.startswith('container_cpu_cfs_') and 'namespace="'+implementation+'"' in line and 'container=""' not in line and 'container="POD"' not in line)
    return dict(time=time.time(),samples=data)

def log_start(log):
    match=re.search(r'start_ms\\?"\s*:\s*(\d+)',log)
    if not match:raise ValueError("Missing k6 stage start marker")
    return int(match.group(1))/1000

def instrumented_child(arguments):
    driver=module()
    original=driver.b.step
    original_command=driver.b.command
    def command(arguments,**kwargs):
        if 'rollout' in arguments and 'status' in arguments:
            arguments=[{'--request-timeout=260s':'--request-timeout=620s','--timeout=240s':'--timeout=600s'}.get(arg,arg) for arg in arguments]
            kwargs['timeout']=630
        return original_command(arguments,**kwargs)
    driver.b.command=command
    def observed(args, implementation, rate, output, records, warmup=False):
        before=counters(driver,implementation)
        result=original(args,implementation,rate,output,records,warmup)
        # Observation failure is retained separately; it never converts a failed load to a pass.
        telemetry=dict(before=before)
        try:
            telemetry['after']=counters(driver,implementation)
            log=(output/'k6.log').read_text()
            start=log_start(log)+5
            seconds=args.warmup if warmup else args.seconds
            pods=sorted({p['metadata']['name'] for record in records for p in driver.b.pods_for(implementation,driver.b.get(implementation,record['deployment']))})
            labels='namespace="'+implementation+'",pod=~"'+ '|'.join(pods)+'",container!="",container!="POD"'
            telemetry.update(load_start=start,load_end=start+seconds,current_pods=pods,queries={})
            queries=dict(cpu='sum by(pod,container)(rate(container_cpu_usage_seconds_total{'+labels+'}[30s]))',
                         memory='sum by(pod,container)(container_memory_working_set_bytes{'+labels+'})')
            for name,query in queries.items():telemetry['queries'][name]=query_range(query,start,start+seconds)
        except Exception as error:telemetry['error']=str(error)
        save(output/'telemetry.json',telemetry)
        return result
    def step(args, implementation, rate, output, records, warmup=False):
        priming=None
        if warmup:
            priming=observed(args,implementation,25,output.with_name(implementation+'-priming'),records,True)
            if not priming['passed']:raise RuntimeError(implementation+' gentle priming failed; no capacity inference')
        result=observed(args,implementation,rate,output,records,warmup)
        if priming:result['priming']=priming
        return result
    driver.b.step=step
    sys.argv=[str(ROOT/'run.py')]+arguments
    driver.main()

def candidate_rates(report):
    data=report['implementations']['java']
    passing=data.get('confirmation')
    if not passing or not passing['passed']:raise RuntimeError('Allocation screen has no successful five-minute confirmation')
    rate=passing['rate']
    higher=[row['rate'] for row in data['stages'] if row['rate']>rate]
    # Retest the observed bracket; if no failing upper step exists, leave room to extend.
    return [rate,min(higher)] if higher else [rate,rate+50]

def choose_budget(original, tuned):
    def confirmed(report):
        row=report['implementations']['java'].get('confirmation',{})
        return row.get('rate',0) if row.get('passed') else 0
    return 'tuned' if confirmed(tuned)>=confirmed(original) and confirmed(tuned)>0 else 'original'

def render(directory,status):
    rows=[]
    for job in status['jobs']:
        report=load(job['report']) if job.get('report') else None
        if report:
            for impl,data in report['implementations'].items():
                measured=data['stages']+([data['confirmation']] if data.get('confirmation') else [])
                for r in measured:
                    values=[job['name'],impl,str(r['rate']),str(r['accepted']),f"{r['p95_ms']:.2f}" if r.get('p95_ms') is not None else '—',str(r['failures']),str(r['dropped']),'PASS' if r['passed'] else r['status']]
                    rows.append('<tr>'+''.join('<td>'+html.escape(v)+'</td>' for v in values)+'</tr>')
    body='''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>Throughput validation</title><style>body{font:17px system-ui;color:#edf;background:#101827;margin:35px}td,th{padding:10px;border-bottom:1px solid #456;text-align:left}a{color:#8df}</style><h1>Throughput validation</h1>'''
    body+='<p>'+html.escape(status['state'])+' · '+html.escape(status.get('active',''))+'</p>'
    body+='<p>2 application CPU cores / 4 GiB each; p95 &lt;100 ms; zero errors/drops. Java CPU allocation is an experimental factor. Shared infrastructure excluded. Failed probes are retained.</p>'
    body+='<p><a href="http://localhost:8080/grafana/d/java-wasm-capacity">Live Grafana</a></p>'
    body+='<table><tr>'+''.join('<th>'+x+'</th>' for x in ['Job','App','Offered/s','Accepted','p95 ms','Errors','Dropped','Outcome'])+'</tr>'+''.join(rows)+'</table>'
    body+='<p>See protocol.json and validation.json for source paths, exact commands, allocation choice and all results. Confirmation rows repeat their candidate rate. Overall latency cannot prove absence of brief stalls; inspect the archived 15-second telemetry and live per-stage trends.</p>'
    (directory/'report.html').write_text(body)

def run_suite(resume=None):
    directory=ROOT/'.results'/('validation-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    directory.mkdir(parents=True)
    guard=subprocess.Popen(['caffeinate','-i','-s']) if sys.platform=='darwin' else None
    child=None
    status=dict(state='running',started=time.time(),jobs=[])
    original=load(ROOT.parent/'budget/budget.json')
    tuned=copy.deepcopy(original)
    allocations={'inventory-service':600,'notification-service':150,'order-service':1200,'product-service':50}
    for row in tuned['java']:row['cpu_millicores']=allocations[row['deployment']]
    tuned['description']='Allocation sensitivity: unchanged total CPU/RAM; Java order 1200m, inventory 600m, notification 150m, product 50m.'
    save(directory/'original-budget.json',original);save(directory/'tuned-budget.json',tuned)
    protocol=dict(cpu_sensitivity=dict(original=original,tuned=tuned,rates=[100,200,250,300,350,400,450,500,600]),
        priming_seconds=180,priming_rate=25,warmup_seconds=180,warmup_rate=100,step_seconds=120,confirmation_seconds=300,
        repeats=3,pair_orders=[['java','wasm'],['wasm','java'],['java','wasm']],
        wasm_rates=[350,400,450],p95_ms=100,vus=250,request_timeout='10s',
        selection='Use tuned Java if its confirmed rate is at least original; otherwise retain original. Repeats retest its confirmed bracket.',
        stability='1800 seconds at the minimum passing confirmation rate across all three repeats; no stability run if any repeat has no passing confirmation.',
        rollout_wait_seconds=600,
        caveats='One shared local VM; per-application budgets exclude databases, messaging, Wasm sidecar and monitoring. Warmup now 100/s, so do not pool earlier 25/s-warmup results.')
    save(directory/'protocol.json',protocol)
    source_files=[Path(__file__),ROOT/'run.py',ROOT.parent/'budget/run.py',ROOT.parent/'footprint/footprint.js']
    (directory/'sources').mkdir()
    for index,path in enumerate(source_files):(directory/'sources'/f'{index}-{path.name}').write_bytes(path.read_bytes())
    hashes={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
    save(directory/'source-integrity.json',hashes)
    def checkpoint():
        save(directory/'validation.json',status);render(directory,status)
    def run(name,impl,budget,rates,seconds=120,confirm=300):
        nonlocal child
        status['active']=name
        job=dict(name=name,implementation=impl,started=time.time(),state='running')
        status['jobs'].append(job);checkpoint()
        before=set(ROOT.glob('.results/*/report.json'))
        argv=[sys.executable,str(Path(__file__).resolve()),'child',impl,'--budget',str(budget),'--rates',','.join(map(str,rates)),
              '--seconds',str(seconds),'--warmup','180','--warmup-rate','100','--p95-ms','100','--vus','250','--confirm-seconds',str(confirm)]
        job['command']=argv;checkpoint()
        print('Starting',name,flush=True)
        with (directory/(name+'.log')).open('w') as stream:
            child=subprocess.Popen(argv,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+5400
            while child.poll() is None:
                if time.monotonic()>deadline:raise RuntimeError('Bounded child execution exceeded 90 minutes')
                time.sleep(1)
        reports=set(ROOT.glob('.results/*/report.json'))-before
        job.update(exit_code=child.returncode,finished=time.time())
        if len(reports)==1:job['report']=str(reports.pop())
        job['state']='completed' if child.returncode==0 else 'failed';checkpoint()
        if child.returncode or not job.get('report'):raise RuntimeError(name+' driver failed; inspect checkpoint and logs')
        report=load(job['report'])
        if not all(report.get(k) for k in ('completed','restored','applications_healthy')):raise RuntimeError(name+' did not verify restoration')
        print('Completed',name,job['report'],flush=True)
        return report
    try:
        print('Validation directory:',directory,flush=True);checkpoint()
        if resume:
            predecessor=load(resume/'validation.json')
            if predecessor['state']!='failed':raise RuntimeError('Resume requires an explicitly failed prior journal')
            prior=load(resume/'protocol.json')
            comparable=lambda value:{k:v for k,v in value.items() if k!='rollout_wait_seconds'}
            if comparable(prior)!=comparable(protocol):raise RuntimeError('Measurement protocol changed; cannot reuse allocation baseline')
            frozen=load(resume/'source-integrity.json')
            for source in source_files[1:]:
                if frozen[str(source)]!=hashlib.sha256(source.read_bytes()).hexdigest():raise RuntimeError('Executable benchmark source changed')
            for job in predecessor['jobs']:
                report=load(job['report'])
                if not all(report.get(k) for k in ('restored','applications_healthy')):raise RuntimeError('Predecessor still needs recovery')
            source=next(job for job in predecessor['jobs'] if job['name']=='allocation-original')
            baseline=load(source['report'])
            if not baseline['completed']:raise RuntimeError('Original allocation baseline incomplete')
            status['predecessor']=str(resume/'validation.json')
            status['setup_revision']='Only rollout wait extended from 240 to 600 seconds; retained setup failure has no capacity measurements.'
            status['jobs'].append(dict(source,name='allocation-original',reused=True));checkpoint()
        else:
            baseline=run('allocation-original','java',directory/'original-budget.json',protocol['cpu_sensitivity']['rates'])
        candidate=run('allocation-tuned','java',directory/'tuned-budget.json',protocol['cpu_sensitivity']['rates'])
        choice=choose_budget(baseline,candidate);status['selected_java_budget']=choice
        budget=directory/(choice+'-budget.json')
        java_rates=candidate_rates(candidate if choice=='tuned' else baseline)
        status['java_repeat_rates']=java_rates;checkpoint()
        repeated={'java':[],'wasm':[]}
        for pair,order in enumerate(protocol['pair_orders'],1):
            for impl in order:
                report=run(f'pair-{pair}-{impl}',impl,budget,java_rates if impl=='java' else protocol['wasm_rates'])
                repeated[impl].append(report)
        status['stability_rates']={}
        for impl in ['wasm','java']:
            confirmations=[r['implementations'][impl].get('confirmation',{}) for r in repeated[impl]]
            if not all(c.get('passed') for c in confirmations):
                status.setdefault('skipped',[]).append(impl+' stability: not all repeats confirmed a passing rate');checkpoint();continue
            rate=min(c['rate'] for c in confirmations);status['stability_rates'][impl]=rate;checkpoint()
            run('stability-'+impl,impl,budget,[rate],seconds=1800,confirm=0)
        status['state']='completed'
    except BaseException as error:
        status.update(state='failed',error=type(error).__name__+': '+str(error));raise
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid,signal.SIGINT)
            try:child.wait(timeout=600)
            except subprocess.TimeoutExpired:
                child.kill();child.wait();status['recovery_required']='Child cleanup timed out; restore its saved state.json'
        status['source_hashes_unchanged']=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in hashes.items())
        status['finished']=time.time();status['active']='';checkpoint()
        if guard:guard.terminate();guard.wait(timeout=5)
        print('Validation report:',directory/'report.html',flush=True)

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='child':instrumented_child(sys.argv[2:])
    else:
        parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--resume',type=Path)
        run_suite(parser.parse_args().resume)
