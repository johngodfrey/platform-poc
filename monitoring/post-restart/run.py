#!/usr/bin/env python3
"""Run the existing benchmark families in an isolated post-restart campaign.

Drivers are copied, not edited in place. Every new raw result stays beneath the
campaign directory. Interrupted/failed studies are retained; safe restoration
is required before another study begins. No runtime images are upgraded.
"""
import datetime
import hashlib
import html
import importlib.util
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parent.parent
SUITES=['footprint','budget','density','capacity','scaleout','scalezero','startup_cache','autoscale','validation']


def save(path,value):
    path=Path(path);temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def module(path):
    sys.path.insert(0,str(path.parent))
    spec=importlib.util.spec_from_file_location('post_restart_validation',path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def record_identity(state):
    return dict(records=[{k:r[k] for k in ('namespace','name','uid','template_hash','original_replicas','images')} for r in state['records']],
                hostconfig=state['hostconfig'],application_version=state['application_version'],
                inventory_fixture=state['inventory_fixture'])


def render(directory,status):
    rows=[]
    for job in status['jobs']:
        links=' '.join('<a href="'+html.escape(os.path.relpath(p,directory))+'">report</a>' for p in job.get('reports',[]))
        rows.append('<tr><td>'+html.escape(job['name'])+'</td><td>'+html.escape(job['state'])+'</td><td>'+links+'</td><td>'+html.escape(job.get('error',''))+'</td></tr>')
    page='''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>post-restart benchmarks</title><style>body{font:17px system-ui;max-width:1300px;margin:35px auto;padding:20px;background:#101c2b;color:#eef}td,th{padding:12px;border-bottom:1px solid #456;text-align:left}a{color:#7bd}p{line-height:1.5}</style><h1>post-restart benchmark campaign</h1>'''
    page+='<p>'+html.escape(status['state'])+' · '+html.escape(status.get('active',''))+'</p><p>New campaign only. No historical measurements are pooled here. Full protocols, raw failures and restoration checks are retained.</p><table><tr><th>Study</th><th>Execution state</th><th>Evidence</th><th>Issue</th></tr>'+''.join(rows)+'</table><p>Completed means the study executed and restored safely; it does not mean every performance gate passed.</p><p><a href="campaign.json">Journal</a> · <a href="protocol.json">Protocol</a> · <a href="http://localhost:8080/grafana/">Grafana</a></p>'
    (directory/'report.html').write_text(page)


def prepare(directory):
    destination=directory/'snapshot'/'monitoring';sources=[]
    for suite in SUITES:
        for path in (REPO/'monitoring'/suite).rglob('*'):
            if not path.is_file() or any(x in path.parts for x in ('.results','__pycache__','.generated')):continue
            if path.suffix not in ('.py','.js','.json','.md','.yaml','.yml'):continue
            target=destination/path.relative_to(REPO/'monitoring');target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(path.read_bytes());sources.append(dict(original=str(path),snapshot=str(target),sha256=digest(path)))
        (destination/suite/'.results').mkdir(parents=True,exist_ok=True)
    # Validation's source/git inventory must refer to the real repository. Its
    # execution drivers and child output directories still use this frozen copy.
    target=destination/'validation/run.py';source=target.read_text()
    assert 'REPO = ROOT.parent.parent' in source
    target.write_text(source.replace('REPO = ROOT.parent.parent','REPO = Path('+repr(str(REPO))+')'))
    save(directory/'snapshot-adaptations.json',{'purpose':'Source inventory points to the actual repository; all measurements and child outputs remain in the isolated copy.',
         'file':str(target),'original_sha256':next(x['sha256'] for x in sources if x['snapshot']==str(target)),
         'snapshot_sha256':digest(target)})
    save(directory/'source-manifest.json',sources)
    old=[]
    for suite in SUITES:
        for path in (REPO/'monitoring'/suite/'.results').rglob('*'):
            if path.is_file():old.append(dict(path=str(path),sha256=digest(path),bytes=path.stat().st_size))
    save(directory/'historical-files.json',old)
    return destination,old


def main():
    os.umask(0o077)
    active=json.loads((ROOT/'.results/ACTIVE.json').read_text());directory=Path(active['directory'])
    if (directory/'campaign.json').exists():raise RuntimeError('Campaign already started; inspect retained journal before resuming')
    guard=subprocess.Popen(['caffeinate','-i','-s'])
    child=None
    status=dict(label='post-restart',state='preparing',started=time.time(),jobs=[],directory=str(directory))
    def checkpoint():
        save(directory/'campaign.json',status);render(directory,status)
        save(ROOT/'.results/ACTIVE.json',{'directory':str(directory),'label':'post-restart','status':status['state'],'active':status.get('active'),'pid':os.getpid()})
    def check():
        validator.physical()
        current=validator.z.snapshot()
        if record_identity(current)!=identity:raise RuntimeError('Deployment resources/templates/images/replicas or fixture not restored')
        if validator.controls()!=controls:raise RuntimeError('Configuration, routing or secret hashes changed')
        for impl in ('java','wasm'):validator.z.s.b.wait_functional(impl)
        return dict(checked=time.time(),restored=True,applications_healthy=True)
    def run(name,argv,bound):
        nonlocal child
        check();before=set(snapshot.rglob('report.html'))
        job=dict(name=name,state='running',command=argv,started=time.time());status['jobs'].append(job);status['active']=name;checkpoint()
        print('START',name,flush=True)
        with (directory/(name+'.log')).open('w') as stream:
            child=subprocess.Popen(argv,cwd=REPO,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+bound;last=0
            while child.poll() is None:
                validator.physical()
                if time.monotonic()>deadline:raise RuntimeError('Bounded study expired: '+name)
                if time.monotonic()-last>45:
                    print('RUNNING',name,'elapsed_minutes',round((time.time()-job['started'])/60,1),flush=True);last=time.monotonic()
                    checkpoint()
                time.sleep(2)
        job.update(exit_code=child.returncode,finished=time.time(),reports=[str(p) for p in sorted(set(snapshot.rglob('report.html'))-before)])
        job['restoration']=check()
        job['state']='completed' if child.returncode==0 else 'failed-retained'
        child=None;checkpoint();print('FINISHED',name,job['state'],flush=True)
        return job
    checkpoint()
    try:
        snapshot,historical=prepare(directory)
        validator=module(snapshot/'validation/run.py');validator.physical()
        original=validator.z.snapshot();validator.z.runtime_snapshot(original)
        validator.z.save(directory/'private-baseline.json',original)
        identity=record_identity(original);controls=validator.controls()
        save(directory/'freeze.json',dict(label='post-restart',identity=identity,controls=controls,power=validator.physical()))
        standalone=directory/'standalone';standalone.mkdir()
        for impl in ('java','wasm'):
            source=(REPO/('orders_'+impl+'.js')).read_text()
            if impl=='java':
                source,count=re.subn(r'duration: "[^"]+",','duration: "5m",',source,count=1)
                if count!=1:raise RuntimeError('Cannot set Java standalone duration without changing repository source')
            (standalone/('orders_'+impl+'.js')).write_text(source)
        jobs=[
          ('frozen-footprint-density-startup',[str(snapshot/'validation/run.py')],21600),
          ('standalone-java-orders',['k6','run','--quiet','--summary-export='+str(standalone/'java-summary.json'),str(standalone/'orders_java.js')],900),
          ('standalone-wasm-orders',['k6','run','--quiet','-e','TEST_DURATION=5m','--summary-export='+str(standalone/'wasm-summary.json'),str(standalone/'orders_wasm.js')],900),
          ('fixed-budget',[str(snapshot/'budget/run.py'),'run','both'],7200),
          ('burst-scale-out',[str(snapshot/'scaleout/repeat.py'),'--pairs','5','--burst-rate','400','--after-scale','180'],10800),
          ('scale-from-zero',[str(snapshot/'scalezero/run.py'),'run','--pairs','5'],7200),
          ('automatic-cpu-scaling',[str(snapshot/'autoscale/run.py'),'run','--trigger','cpu','--pairs','2'],10800),
          ('automatic-latency-scaling',[str(snapshot/'autoscale/run.py'),'run','--trigger','latency','--pairs','2'],10800),
          ('allocation-repeated-capacity-stability',[str(snapshot/'capacity/validate.py')],25200),
        ]
        protocol=dict(label='post-restart',jobs=[dict(name=n,argv=a if a[0]=='k6' else [sys.executable]+a,bound_seconds=b) for n,a,b in jobs],
          historical_policy='Preserve every existing file unchanged; do not pool the new campaign with earlier runs.',
          failure_policy='Retain failed attempts. Proceed to the next independent study only after templates/images/resources/config hashes and both functional paths verify restored. No silent retries or runtime upgrades.',
          inputs='Frozen copies of existing drivers; measurement settings unchanged. Source/git inventory adapter is archived.',
          stability_fallback='If the capacity matrix skips a long trial, run the existing declared common-step fallback once, capped at 400/s. No lower-rate retry.',
          baseline_orders='Existing footprint suite sends the representative Java/Wasm order shapes; no concurrent load generators.',
          note='All result files and source snapshots beneath this directory are labelled post-restart by campaign membership. Completed execution is separate from performance pass/fail.')
        save(directory/'protocol.json',protocol);status['state']='running';checkpoint()
        for name,arguments,bound in jobs:
            job=run(name,arguments if arguments[0]=='k6' else [sys.executable]+arguments,bound)
            if name=='frozen-footprint-density-startup':
                # If validation halts before scheduling its later families, preserve
                # that failure and still execute the missing independent families.
                reports=list((snapshot/'validation/.results').glob('*/campaign.json'))
                scheduled=json.loads(reports[0].read_text()).get('attempts',[]) if reports else []
                for suite,args in [('density',['run','both']),('startup_cache',['run','--pairs','5'])]:
                    if not any(a['suite']==suite for a in scheduled):run('independent-'+suite,[sys.executable,str(snapshot/suite/'run.py')]+args,10800)
            if name=='allocation-repeated-capacity-stability' and job['exit_code']==0:
                matrix=next((snapshot/'capacity/.results').glob('validation-*'))
                save(matrix/'stability-addendum.json',dict(state='planned',label='post-restart',jobs=[],caps={'java':400,'wasm':400},declared_in=str(directory/'protocol.json')))
                run('stability-common-rate-fallback',[sys.executable,str(snapshot/'capacity/finish_stability.py'),str(matrix)],10800)
        status['final_restoration']=check()
        status['historical_files_unchanged']=all(Path(r['path']).is_file() and digest(Path(r['path']))==r['sha256'] for r in historical)
        if not status['historical_files_unchanged']:raise RuntimeError('Historical integrity check failed')
        status['state']='completed-with-retained-failures' if any(j['exit_code'] for j in status['jobs']) else 'completed'
    except BaseException as error:
        status.update(state='stopped',error=type(error).__name__+': '+str(error));raise
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid,signal.SIGINT)
            try:child.wait(timeout=600)
            except subprocess.TimeoutExpired:status['recovery_required']='Child still cleaning up after timeout; inspect its saved state before continuing'
        guard.terminate();guard.wait(timeout=10)
        status['finished']=time.time();status['active']='';checkpoint()
        print('POST-RESTART REPORT',directory/'report.html',flush=True)


if __name__=='__main__':main()
