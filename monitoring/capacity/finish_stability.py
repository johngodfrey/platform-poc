#!/usr/bin/env python3
"""Execute the retained stability addendum after validate.py finishes safely."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent

def read(path):return json.loads(Path(path).read_text())
def save(path,value):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.replace(path)

def common_rate(reports,implementation,cap):
    if len(reports)!=3:raise RuntimeError('Exactly three current repeats are required')
    common=None
    for report in reports:
        if not all(report.get(k) for k in ['completed','restored','applications_healthy']):raise RuntimeError('Repeat needs recovery')
        rates={r['rate'] for r in report['implementations'][implementation]['stages'] if r['passed']}
        common=rates if common is None else common & rates
    candidates=[rate for rate in common if rate<=cap]
    if not candidates:raise RuntimeError('No common passing short-step rate; diagnosis required')
    return max(candidates)

def main(directory):
    directory=directory.resolve();path=directory/'stability-addendum.json';status=read(path)
    if status['jobs'] or status['state']!='planned':raise RuntimeError('Addendum already started; inspect retained journal before proceeding')
    guard=subprocess.Popen(['caffeinate','-i','-s']) if sys.platform=='darwin' else None
    child=None
    try:
        status.update(state='waiting-for-matrix',watcher_started=time.time())
        source=(directory/'stability-followup-source.py');source.write_bytes(Path(__file__).read_bytes())
        status['driver_source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest();save(path,status)
        print('Waiting for the matrix:',directory,flush=True)
        deadline=time.monotonic()+18000
        while True:
            journal=read(directory/'validation.json')
            if journal['state']!='running':break
            if time.monotonic()>deadline:raise RuntimeError('Matrix did not finish within five-hour watcher bound')
            time.sleep(5)
        if journal['state']!='completed':raise RuntimeError('Matrix failed; refusing an automatic follow-up')
        for impl in ['wasm','java']:
            # A failed SLO in an existing long trial is still a completed study; never replace it.
            if any(j['name']=='stability-'+impl for j in journal['jobs']):continue
            hashes=read(directory/'source-integrity.json')
            if not all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in hashes.items()):raise RuntimeError('Frozen executable benchmark source changed')
            reports=[read(j['report']) for j in journal['jobs'] if j['name'].startswith('pair-') and j['implementation']==impl]
            rate=common_rate(reports,impl,status['caps'][impl])
            budget=directory/(journal['selected_java_budget']+'-budget.json')
            command=[sys.executable,str(ROOT/'validate.py'),'child',impl,'--budget',str(budget),'--rates',str(rate),
                     '--seconds','1800','--warmup','180','--warmup-rate','100','--p95-ms','100','--vus','250','--confirm-seconds','0']
            row=dict(name='stability-'+impl+'-fallback',implementation=impl,rate=rate,command=command,state='running',started=time.time())
            status['jobs'].append(row);status['state']='running-'+impl;save(path,status)
            before=set(ROOT.glob('.results/*/report.json'))
            print('Starting',impl,'30-minute follow-up at',rate,'/s',flush=True)
            with (directory/(row['name']+'.log')).open('w') as stream:
                child=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
                deadline=time.monotonic()+5400
                while child.poll() is None:
                    if time.monotonic()>deadline:raise RuntimeError('Bounded follow-up exceeded 90 minutes')
                    time.sleep(1)
            created=set(ROOT.glob('.results/*/report.json'))-before
            row.update(exit_code=child.returncode,finished=time.time())
            if len(created)==1:row['report']=str(created.pop())
            row['state']='completed' if child.returncode==0 else 'failed';save(path,status)
            if child.returncode or not row.get('report'):raise RuntimeError('Follow-up driver failed; inspect checkpoint')
            report=read(row['report'])
            if not all(report.get(k) for k in ['completed','restored','applications_healthy']):raise RuntimeError('Follow-up restoration not verified')
            row['result']=report['implementations'][impl]['stages'][0];save(path,status)
            print('Finished',impl,json.dumps(row['result']),flush=True)
        status['state']='completed'
    except BaseException as error:
        status.update(state='failed',error=type(error).__name__+': '+str(error));raise
    finally:
        if child and child.poll() is None:
            os.killpg(child.pid,signal.SIGINT)
            try:child.wait(timeout=600)
            except subprocess.TimeoutExpired:
                child.kill();child.wait();status['recovery_required']='Child cleanup timed out; restore its retained state.json'
        status['finished']=time.time();save(path,status)
        if guard:guard.terminate();guard.wait(timeout=5)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path)
    main(parser.parse_args().directory)
