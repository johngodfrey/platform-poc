#!/usr/bin/env python3
"""Continue the interrupted post-restart capacity matrix without rewriting it."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def read(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def save(path,value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.replace(path)
def load(path,name):
    sys.path.insert(0,str(path.parent))
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
def identity(snapshot):
    return dict(records=[{k:r[k] for k in ('namespace','name','uid','template_hash','original_replicas','images')} for r in snapshot['records']],
                hostconfig=snapshot['hostconfig'],application_version=snapshot['application_version'],inventory_fixture=snapshot['inventory_fixture'])


def main(campaign):
    os.umask(0o077)
    campaign=campaign.resolve();monitoring=campaign/'snapshot/monitoring'
    predecessor=monitoring/'capacity/.results/validation-20261110T080306Z'
    prior=read(predecessor/'validation.json')
    if prior['state']!='failed':raise RuntimeError('Expected a retained interrupted matrix')
    if list(campaign.glob('continuation-*/continuation.json')):raise RuntimeError('A continuation already exists; inspect it before starting another')
    directory=campaign/('continuation-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));directory.mkdir()
    guard=subprocess.Popen(['caffeinate','-i','-s'])
    child=None
    state=dict(label='post-restart',state='preparing',started=time.time(),jobs=[],source_journal=str(predecessor/'validation.json'),active='')
    def checkpoint():
        save(directory/'continuation.json',state)
        save(campaign/'continuation.json',dict(label='post-restart',state=state['state'],directory=str(directory),active=state['active']))
        rows=[]
        for job in state['jobs']:
            results=[]
            if job.get('report'):
                report=read(job['report'])
                for impl,values in report['implementations'].items():
                    for row in values['stages']+([values['confirmation']] if values.get('confirmation') else []):
                        results.append(f"{impl}: {row['rate']}/s; p95 {row['p95_ms']:.2f} ms; {row['failures']} failures; {row['dropped']} dropped; {'PASS' if row['passed'] else 'FAIL'}")
            link='<a href="'+html.escape(os.path.relpath(Path(job['report']).with_name('report.html'),directory))+'">raw report</a>' if job.get('report') else ''
            rows.append('<tr><td>'+html.escape(job['name'])+'</td><td>'+html.escape(job['state'])+(' · retained' if job.get('reused') else '')+'</td><td>'+html.escape(job.get('qualification',''))+'</td><td>'+link+'</td><td>'+'<br>'.join(html.escape(x) for x in results)+'</td></tr>')
        page='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>post-restart continuation</title><style>body{font:17px system-ui;background:#101c2b;color:#eef;margin:35px}td,th{padding:12px;text-align:left;border-bottom:1px solid #456}a{color:#7bd}p{line-height:1.5}</style><h1>post-restart continuation</h1><p>'+html.escape(state['state'])+' · '+html.escape(state['active'])+'</p><p>The interrupted journal is retained unchanged. Completed measurements are linked, not counted twice. Diagnostic stability does not establish a qualified capacity.</p><table><tr><th>Job</th><th>Execution</th><th>Qualification</th><th>Evidence</th><th>Performance</th></tr>'+''.join(rows)+'</table><p>'+html.escape(state.get('error',''))+'</p><p><a href="../report.html">Original campaign</a> · <a href="protocol.json">Continuation protocol</a> · <a href="continuation.json">Journal</a></p>'
        (directory/'report.html').write_text(page)
        (campaign/'live.html').write_text('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="15;url='+html.escape(directory.name)+'/report.html"><title>post-restart continuation</title><h1>post-restart continuation: '+html.escape(state['state'])+'</h1><p>'+html.escape(state['active'])+'</p><p><a href="'+html.escape(directory.name)+'/report.html">Open continuation results</a> · <a href="report.html">Original campaign</a></p>')
    checkpoint()
    try:
        validator=load(monitoring/'validation/run.py','post_restart_continuation_validation')
        frozen=read(campaign/'freeze.json');hashes=read(predecessor/'source-integrity.json')
        def check():
            validator.physical()
            if not all(sha(p)==expected for p,expected in hashes.items()):raise RuntimeError('Frozen measurement executable changed')
            current=validator.z.snapshot()
            if identity(current)!=frozen['identity']:raise RuntimeError('Application images/templates/resources/replicas differ from the post-restart baseline')
            if validator.controls()!=frozen['controls']:raise RuntimeError('Configuration/routing/secret hashes differ from the post-restart baseline')
            for impl in ['java','wasm']:validator.z.s.b.wait_functional(impl)
            return dict(checked=time.time(),restored=True,applications_healthy=True)
        state['preflight']=check()
        save(directory/'source-integrity.json',dict(measurement_sources=hashes,continuation_driver_sha256=sha(__file__)))
        (directory/'driver.py').write_bytes(Path(__file__).read_bytes())
        interrupted=monitoring/'capacity/.results/20261110T094853Z/report.json'
        partial=read(interrupted)
        if partial.get('completed') or not all(partial.get(k) for k in ['restored','applications_healthy']):raise RuntimeError('Interrupted child needs verified recovery')
        state['interrupted_attempt']=dict(path=str(interrupted),sha256=sha(interrupted),classification='environmental_interruption',count_as_completed_repetition=False)
        retained=[]
        for job in prior['jobs']:
            if job['state']!='completed':continue
            report=read(job['report'])
            if not all(report.get(k) for k in ['completed','restored','applications_healthy']):raise RuntimeError('Retained job did not complete safely')
            retained.append(dict(job,reused=True,source_report_sha256=sha(job['report'])))
        if [j['name'] for j in retained]!=['allocation-original','allocation-tuned','pair-1-java','pair-1-wasm']:raise RuntimeError('Unexpected continuation point')
        state['jobs']=retained
        protocol=read(predecessor/'protocol.json')
        budget=predecessor/(prior['selected_java_budget']+'-budget.json')
        save(directory/'protocol.json',dict(label='post-restart',source_protocol=str(predecessor/'protocol.json'),source_protocol_sha256=sha(predecessor/'protocol.json'),measurement_protocol=protocol,
            remaining=['pair-2-wasm','pair-2-java','pair-3-java','pair-3-wasm'],retained_jobs=[j['name'] for j in retained],
            interruption_policy='Repeat only the incomplete child; retain its partial raw report separately. Completed failed performance gates remain part of the matrix.',
            stability='Use the original minimum passing-confirmation rate if all three confirmations pass; otherwise the predeclared common passing short-step fallback capped at400/s. If neither qualifies a Wasm rate, run one separately labelled diagnostic30-minute350/s trial at the lowest originally scheduled Wasm rate. Do not interpret that diagnostic as qualified capacity. No lower-rate retry.',
            comparability='Different selected long-run rates must not be used for matched-load CPU efficiency ratios. Shared infrastructure is outside application budgets.'))
        def run(name,impl,rates,seconds=120,confirm=300,qualification='capacity-repetition'):
            nonlocal child
            check()
            job=dict(name=name,implementation=impl,state='running',started=time.time(),qualification=qualification)
            state['jobs'].append(job);state['active']=name;checkpoint()
            before=set((monitoring/'capacity/.results').glob('*/report.json'))
            args=[sys.executable,str(monitoring/'capacity/validate.py'),'child',impl,'--budget',str(budget),'--rates',','.join(map(str,rates)),
                  '--seconds',str(seconds),'--warmup','180','--warmup-rate','100','--p95-ms','100','--vus','250','--confirm-seconds',str(confirm)]
            job['command']=args;checkpoint();print('START',name,flush=True)
            with (directory/(name+'.log')).open('w') as stream:
                child=subprocess.Popen(args,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
                deadline=time.monotonic()+5400;last=0
                while child.poll() is None:
                    validator.physical()
                    if time.monotonic()>deadline:raise RuntimeError('Bounded child expired')
                    if time.monotonic()-last>45:
                        print('RUNNING',name,round((time.time()-job['started'])/60,1),'minutes',flush=True);last=time.monotonic()
                    time.sleep(2)
            created=set((monitoring/'capacity/.results').glob('*/report.json'))-before
            job.update(exit_code=child.returncode,finished=time.time())
            if len(created)==1:job['report']=str(created.pop())
            job['state']='completed' if child.returncode==0 else 'failed';checkpoint()
            if child.returncode or not job.get('report'):raise RuntimeError(name+' driver failed; retain checkpoint and recover before continuing')
            report=read(job['report'])
            if not all(report.get(k) for k in ['completed','restored','applications_healthy']):raise RuntimeError('Child did not verify restoration')
            job['restoration']=check();child=None;checkpoint();print('FINISHED',name,flush=True)
            return report
        state['state']='running';checkpoint()
        for pair,impl in [(2,'wasm'),(2,'java'),(3,'java'),(3,'wasm')]:
            run(f'pair-{pair}-{impl}',impl,prior['java_repeat_rates'] if impl=='java' else protocol['wasm_rates'])
        for impl in ['wasm','java']:
            reports=[read(j['report']) for j in state['jobs'] if j['name'].startswith('pair-') and j['implementation']==impl]
            if len(reports)!=3:raise RuntimeError('Expected three complete repetitions')
            values=[r['implementations'][impl] for r in reports]
            confirmations=[v.get('confirmation',{}) for v in values]
            if all(c.get('passed') for c in confirmations):
                rate=min(c['rate'] for c in confirmations);qualification='qualified-confirmation-rate'
            else:
                common=set.intersection(*[{s['rate'] for s in v['stages'] if s['passed'] and s['rate']<=400} for v in values])
                if common:rate=max(common);qualification='qualified-common-short-step-fallback'
                elif impl=='wasm':
                    rate=min(protocol['wasm_rates']);qualification='diagnostic-unqualified-rate'
                    state.setdefault('qualification_gaps',[]).append('No Wasm rate passed all three repetitions. The350/s long run is diagnostic, not confirmed sustainable capacity.')
                else:
                    state.setdefault('skipped',[]).append('Java stability: no common qualifying rate');checkpoint();continue
            run('stability-'+impl,impl,[rate],seconds=1800,confirm=0,qualification=qualification)
        state['final_restoration']=check()
        historical=read(campaign/'historical-files.json')
        state['historical_files_unchanged']=all(Path(x['path']).is_file() and sha(x['path'])==x['sha256'] for x in historical)
        if not state['historical_files_unchanged']:raise RuntimeError('Historical integrity mismatch')
        state['retained_reports_unchanged']=all(sha(j['report'])==j['source_report_sha256'] for j in retained if j.get('reused'))
        if not state['retained_reports_unchanged']:raise RuntimeError('Retained completed reports changed')
        state['state']='completed'
    except BaseException as error:
        state.update(state='stopped',error=type(error).__name__+': '+str(error));raise
    finally:
        if child and child.poll() is None:
            os.killpg(child.pid,signal.SIGINT)
            try:child.wait(timeout=600)
            except subprocess.TimeoutExpired:state['recovery_required']='Child cleanup still running; inspect saved state before further tests'
            state['cleanup_exit_code']=child.returncode
        state['finished']=time.time();state['active']='';checkpoint()
        guard.terminate();guard.wait(timeout=10)
        print('POST-RESTART CONTINUATION',directory/'report.html',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('campaign',type=Path)
    main(parser.parse_args().campaign)
