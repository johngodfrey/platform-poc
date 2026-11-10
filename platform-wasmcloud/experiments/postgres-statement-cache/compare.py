#!/usr/bin/env python3
"""Compare stock, same-build control and cached providers; always restore stock."""
from collections import Counter
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
import urllib.request

import psycopg
from psycopg import sql

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parents[2]
K=['kubectl','--context','k3d-platform-poc','--request-timeout=20s']
CTL=['--ctl-host','127.0.0.1','--ctl-port','14223','--lattice','default']
STOCK='ghcr.io/wasmcloud/sqldb-postgres:0.7.2'
CACHED='host.docker.internal:5001/v2/platform-poc/sqldb-postgres:0.7.2-stmtcache.1'
CONTROL='host.docker.internal:5001/v2/platform-poc/sqldb-postgres:0.7.2-stmtcache-control.1'

def read(p):return json.loads(Path(p).read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,d):
    tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(d,indent=2,default=str)+'\n');tmp.replace(p)
def cmd(args,timeout=30):return subprocess.check_output(args,text=True,stderr=subprocess.PIPE,timeout=timeout)
def module(path,name):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
def get(kind,name):return json.loads(cmd(K+['-n','wasm','get',kind,name,'-o','json']))
def metric_request():
    req=urllib.request.Request('http://localhost:8080/orders',b'[{"sku":"ENG-V8-500","price":8500,"quantity":1}]',{'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=10) as r:
        return r.status==201 and r.read().decode().strip()=='201 Created'

def main(resume=None):
    os.umask(0o077)
    guard=None;child=None;forward=None;app=None;touched=False
    directory=ROOT/'.build/performance'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');directory.mkdir(parents=True)
    state=dict(state='preparing',started=time.time(),phases=[],active='',restored=False)
    created_versions=[];used_versions=set()
    if resume:
        resume=Path(resume).resolve();prior=read(resume/'report.json');recovery=read(resume/'recovery.json')
        if prior['state']!='failed' or not all(recovery.get(k) for k in ['application_spec_restored','version_restored','deployment_restored','hostconfig_restored']):raise RuntimeError('Predecessor requires verified recovery')
        for source,expected in read(resume/'source-integrity.json').items():
            if Path(source).resolve()!=Path(__file__).resolve() and sha(source)!=expected:raise RuntimeError('Frozen measurement input changed')
        complete=[p for p in prior['phases'] if p['state']=='completed']
        if [p['name'] for p in complete]!=['stock','control-1','cached-1']:raise RuntimeError('Unexpected continuation point')
        state['predecessor']=str(resume/'report.json');state['predecessor_sha256']=sha(resume/'report.json')
        state['continuation_revision']='Lifecycle only: deploy already-stored versions by name; delete owned undeployed test versions during restoration. Measurement settings/binaries unchanged.'
        state['phases']=[dict(p,reused=True,source_directory=str(resume/p['name']),source_summary_sha256=sha(resume/p['name']/'measurement/summary.json')) for p in complete]
    profile=module(REPO/'monitoring/provider-profile/run.py','pg_profile')
    scaling=module(REPO/'monitoring/scaleout/run.py','pg_scaling')
    def checkpoint():
        save(directory/'report.json',state)
        rows=[]
        for row in state['phases']:
            r=row.get('result',{});pg=r.get('postgres_provider_cores');total=r.get('application_cores')
            cells=[row['name'],row['state'],r.get('requests',''),r.get('failures',''),r.get('dropped',''),f'{r["p95_ms"]:.3f}' if r.get('p95_ms') is not None else '',f'{total:.4f}' if total is not None else '',f'{pg:.4f}' if pg is not None else '']
            rows.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in cells)+'</tr>')
        page='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>PostgreSQL statement-cache comparison</title><style>body{font:17px system-ui;background:#101c2b;color:#eef;margin:35px}td,th{padding:12px;border-bottom:1px solid #456}a{color:#7bd}p{line-height:1.5}</style><h1>PostgreSQL statement-cache comparison</h1><p>'+html.escape(state['state'])+' · '+html.escape(state['active'])+'</p><p>100 orders/s;60s warmup;120s measured;250 VUs. Fresh Wasm host each phase. No statement-duration logging during timed runs. Two matched uncached/cached pairs plus a stock baseline. No failures or probes are discarded.</p><table><tr><th>Phase</th><th>Execution</th><th>Requests</th><th>Failures</th><th>Dropped</th><th>p95 ms</th><th>App cores</th><th>PG provider cores</th></tr>'+''.join(rows)+'</table><p>Application CPU is host plus native providers, excluding NATS sidecar/brokers, database and monitoring. A separate10s SQL probe after selected phases verifies Parse/Execute behavior without contaminating the performance interval.</p><p>Restored: '+str(state['restored'])+' · '+html.escape(state.get('error',''))+'</p><p><a href="report.json">Raw journal</a> · <a href="protocol.json">Protocol</a></p>'
        (directory/'report.html').write_text(page)
    def pod(require_ready=True):
        rows=json.loads(cmd(K+['-n','wasm','get','pods','-o','json']))['items']
        rows=[p for p in rows if p['metadata']['name'].startswith('wasmcloud-host-') and not p['metadata'].get('deletionTimestamp')]
        if len(rows)!=1 or require_ready and not all(c['ready'] for c in rows[0]['status']['containerStatuses']):raise RuntimeError('Expected one ready host')
        return rows[0]
    def process_map():
        p=pod();node=p['spec']['nodeName'];cid=next(c['containerID'].split('://')[1] for c in p['status']['containerStatuses'] if c['name']=='wasmcloud-host')
        hostpid=json.loads(cmd(['docker','exec',node,'crictl','inspect',cid]))['info']['pid']
        raw=cmd(['docker','exec',node,'sh','-c',f'for p in {hostpid} $(cat /proc/{hostpid}/task/*/children); do echo "$p $(readlink /proc/$p/exe)"; done'])
        processes={int(x.split()[0]):x.split()[1] for x in raw.splitlines()}
        matches=[pid for pid,path in processes.items() if 'sqldb_postgres' in path]
        if len(matches)!=1:raise RuntimeError('PostgreSQL provider process not unique/ready')
        checksum=cmd(['docker','exec',node,'sha256sum',f'/proc/{matches[0]}/exe']).split()[0]
        return dict(pod=p['metadata']['name'],uid=p['metadata']['uid'],node=node,processes=processes,provider_pid=matches[0],provider_sha256=checksum)
    def counters(mapping):
        now=time.monotonic();raw=cmd(['docker','exec',mapping['node'],'sh','-c','cat '+' '.join(f'/proc/{pid}/stat' for pid in mapping['processes'])])
        ticks={}
        for line in raw.splitlines():
            pid=int(line.split()[0]);v=line.rsplit(')',1)[1].split();ticks[pid]=int(v[11])+int(v[12])
        return dict(time=now,ticks=ticks)
    def manifest(reference,version):
        annotations=copy.deepcopy(app['metadata'].get('annotations',{}));annotations.pop('kubectl.kubernetes.io/last-applied-configuration',None);annotations['version']=version
        result=dict(apiVersion=app['apiVersion'],kind=app['kind'],metadata=dict(name='platform-poc',annotations=annotations),spec=copy.deepcopy(app['spec']))
        next(c for c in result['spec']['components'] if c['name']=='sqldb-postgres')['properties']['image']=reference
        return result
    def deploy(reference,version,path):
        nonlocal touched
        data=manifest(reference,version);save(path,data);touched=True
        if reference==STOCK or version in used_versions:
            cmd(['wash','app','deploy','platform-poc',version]+CTL,45)
        else:
            created_versions.append(version);state['created_versions']=created_versions;checkpoint()
            cmd(['wash','app','deploy',str(path)]+CTL,45)
            used_versions.add(version)
    def fresh(reference,version,path,expected_hash=None):
        deploy(reference,version,path)
        old=pod(require_ready=False);cmd(K+['-n','wasm','delete','pod',old['metadata']['name'],'--wait=true','--timeout=120s'],130)
        deadline=time.monotonic()+300;last=''
        while time.monotonic()<deadline:
            try:
                p=pod()
                if p['metadata']['uid']==old['metadata']['uid']:raise RuntimeError('Still the old host')
                mapping=process_map()
                if expected_hash and mapping['provider_sha256']!=expected_hash:raise RuntimeError('Selected provider binary not loaded yet')
                if not metric_request():raise RuntimeError('Order preflight did not pass')
                return mapping
            except Exception as e:last=type(e).__name__;time.sleep(3)
        raise RuntimeError('Provider readiness timed out: '+last)
    def load_run(folder,seconds,tag):
        nonlocal child
        folder.mkdir()
        env=dict(os.environ,IMPLEMENTATION='wasm',RUN_ID=tag,IDLE_SECONDS='1',LOAD_SECONDS=str(seconds),RECOVERY_SECONDS='1',ORDER_RATE='100',ORDER_VUS='250',REQUEST_TIMEOUT='10s')
        argv=['k6','run','--quiet','--summary-export='+str(folder/'summary.json'),str(REPO/'monitoring/footprint/footprint.js')]
        with (folder/'k6.log').open('w') as log:
            child=subprocess.Popen(argv,env=env,stdout=log,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+seconds+90;last=0
            while child.poll() is None:
                profile.physical()
                if time.monotonic()>deadline:raise RuntimeError('Bounded load expired')
                if time.monotonic()-last>45:print('RUNNING',state['active'],folder.name,flush=True);last=time.monotonic()
                time.sleep(2)
            code=child.returncode;child=None
        summary=read(folder/'summary.json')['metrics']
        return dict(exit_code=code,requests=summary['http_reqs']['count'],accepted=summary['demo_orders_accepted']['count'],failures=summary['demo_order_failures']['count'],dropped=summary.get('dropped_iterations',{}).get('count',0),p95_ms=summary['http_req_duration']['p(95)'],median_ms=summary['http_req_duration']['med'],mean_ms=summary['http_req_duration']['avg'],maximum_ms=summary['http_req_duration']['max'])
    def sql_probe(folder,tag):
        # Separate diagnostic interval; restore exact server overrides even on failure.
        con=psycopg.connect('dbname=postgres application_name=statement-cache-probe',autocommit=True);original={};changed=[]
        try:
            for k,v in profile.SETTINGS.items():
                old=con.execute("select setting from pg_file_settings where name=%s and sourcefile like '%%/postgresql.auto.conf' and applied order by seqno desc limit 1",(k,)).fetchone()
                original[k]=old[0] if old else None
                con.execute(sql.SQL('ALTER SYSTEM SET {} TO {}').format(sql.Identifier(k),sql.Literal(v)));changed.append(k)
            con.execute('select pg_reload_conf()');time.sleep(1)
            logfile=Path('/opt/homebrew/var/log/postgresql@15.log');offset=logfile.stat().st_size
            result=load_run(folder,10,tag)
            con.execute("ALTER SYSTEM SET log_min_duration_statement TO '-1'");con.execute('select pg_reload_conf()');time.sleep(1)
            with logfile.open('rb') as f:f.seek(offset);text=f.read().decode(errors='replace')
            rows=[r for r in profile.parse_log(text) if r['database']=='platform-poc-wasm'];save(folder/'statements.json',rows)
            phases=Counter(r['phase'] for r in rows)
            return dict(load=result,phases=dict(phases),parse_per_order=phases['parse']/result['requests'],execute_per_order=phases['execute']/result['requests'])
        finally:
            for k in changed:
                query=sql.SQL('ALTER SYSTEM RESET {}').format(sql.Identifier(k)) if original[k] is None else sql.SQL('ALTER SYSTEM SET {} TO {}').format(sql.Identifier(k),sql.Literal(original[k]))
                con.execute(query)
            if changed:con.execute('select pg_reload_conf()');time.sleep(1)
            con.close()
    checkpoint()
    guard=subprocess.Popen(['caffeinate','-i','-s'])
    try:
        profile.physical()
        if subprocess.run(['pgrep','-x','k6'],stdout=subprocess.DEVNULL).returncode==0:raise RuntimeError('Another load test is running')
        con=psycopg.connect('dbname=postgres',autocommit=True)
        logging0={k:con.execute('select setting from pg_settings where name=%s',(k,)).fetchone()[0] for k in profile.SETTINGS};con.close()
        if logging0['log_min_duration_statement']!='-1':raise RuntimeError('Statement-duration logging must be off for performance measurements')
        app=get('application','platform-poc');dep=get('deployment','wasmcloud-host');hostconfig=get('wasmcloudhostconfig','wasmcloud-host')
        if next(c for c in app['spec']['components'] if c['name']=='sqldb-postgres')['properties']['image']!=STOCK:raise RuntimeError('Expected the original PostgreSQL provider')
        original_version=app['metadata']['annotations']['version']
        used_versions.add(original_version)
        save(directory/'private-baseline.json',dict(application=app,deployment=dep,hostconfig=hostconfig,logging=logging0))
        codehash={str(p):sha(p) for p in [Path(__file__),REPO/'monitoring/footprint/footprint.js',ROOT/'.build/artifacts/control/sqldb-postgres-provider',ROOT/'.build/artifacts/sqldb-postgres-provider']}
        save(directory/'source-integrity.json',codehash);(directory/'driver.py').write_bytes(Path(__file__).read_bytes())
        expected={'control':sha(ROOT/'.build/artifacts/control/sqldb-postgres-provider'),'cached':sha(ROOT/'.build/artifacts/sqldb-postgres-provider')}
        protocol=dict(rate=100,warmup_seconds=60,measurement_seconds=120,vus=250,request_timeout='10s',order=['stock','control-1','cached-1','control-2','cached-2'],sql_probes='10s separately after stock/control-1/cached-1',baseline=STOCK,control=CONTROL,experimental=CACHED,restoration='Redeploy original saved manifest/version, restart only the Wasm host, verify provider binary and unchanged templates/config. Never modify Java deployment or reset databases.',caveats='One local shared VM; diagnostic small sample. Same-build control isolates compiler differences. CPU includes host and providers; NATS and DB CPU excluded.')
        save(directory/'protocol.json',protocol)
        forward=scaling.NatsForward(14223);forward.__enter__()
        state['state']='running';checkpoint();stockhash=state['phases'][0]['processes']['provider_sha256'] if resume else None
        for name,kind,reference in [('stock','stock',STOCK),('control-1','control',CONTROL),('cached-1','cached',CACHED),('control-2','control',CONTROL),('cached-2','cached',CACHED)]:
            if any(p['name']==name for p in state['phases']):continue
            state['active']=name;row=dict(name=name,kind=kind,state='preparing',reference=reference);state['phases'].append(row);checkpoint();print('START',name,flush=True)
            folder=directory/name;folder.mkdir();version=original_version if kind=='stock' else f'v0.0.10-pgcache-{directory.name.lower()}-{kind}'
            mapping=fresh(reference,version,folder/'manifest.json',expected.get(kind));row['processes']=mapping
            if kind=='stock':stockhash=mapping['provider_sha256']
            row['state']='warming';checkpoint()
            warm=load_run(folder/'warmup',60,name+'-warm')
            row['warmup']=warm;checkpoint()
            if warm['failures'] or warm['dropped']:raise RuntimeError('Warmup failed; retained without replacing')
            row['state']='measuring';checkpoint();before=counters(mapping)
            result=load_run(folder/'measurement',120,name+'-measurement');after=counters(mapping)
            if process_map()['provider_sha256']!=mapping['provider_sha256'] or pod()['metadata']['uid']!=mapping['uid']:raise RuntimeError('Host or provider changed during measurement')
            duration=after['time']-before['time'];ticks=int(cmd(K+['-n','wasm','exec',mapping['pod'],'-c','wasmcloud-host','--','getconf','CLK_TCK']).strip())
            usage=[dict(pid=pid,path=path,cpu_seconds=(after['ticks'][pid]-before['ticks'][pid])/ticks,cores=(after['ticks'][pid]-before['ticks'][pid])/ticks/duration) for pid,path in mapping['processes'].items()]
            result.update(application_cores=sum(x['cores'] for x in usage),postgres_provider_cores=next(x['cores'] for x in usage if x['pid']==mapping['provider_pid']),cpu_seconds_per_1000_orders=sum(x['cpu_seconds'] for x in usage)*1000/result['accepted'],cpu_window_seconds=duration,native_processes=usage,cpu_before=before,cpu_after=after)
            row.update(result=result,state='completed');checkpoint()
            if name in ['stock','control-1','cached-1']:
                row['sql_probe']=sql_probe(folder/'sql-probe',name+'-sql-probe');checkpoint()
            print('FINISHED',name,json.dumps({k:result[k] for k in ['failures','dropped','p95_ms','application_cores','postgres_provider_cores']}),flush=True)
        state['state']='completed'
    except BaseException as e:state.update(state='failed',error=type(e).__name__+': '+str(e));raise
    finally:
        if child and child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:child.wait(timeout=35)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        if touched and app is not None:
            try:
                state['active']='restoring-original-provider';checkpoint()
                # Cleanup is allowed even if mains disconnected; it must finish.
                current=fresh(STOCK,app['metadata']['annotations']['version'],directory/'restore-manifest.json',locals().get('stockhash'))
                # The Application API exposes the latest stored model, which can
                # differ from the deployed model. Remove only this run's versions.
                for version in created_versions:
                    if version==app['metadata']['annotations']['version']:raise RuntimeError('Refusing to delete original version')
                    try:cmd(['wash','app','delete','platform-poc',version]+CTL,30)
                    except subprocess.CalledProcessError:state.setdefault('cleanup_warnings',[]).append('Could not delete owned version '+version)
                live=get('application','platform-poc');live_dep=get('deployment','wasmcloud-host');live_host=get('wasmcloudhostconfig','wasmcloud-host')
                if live['spec']!=app['spec'] or live_dep['spec']!=dep['spec'] or live_host['spec']!=hostconfig['spec']:raise RuntimeError('Original application/config/template not restored')
                state['restored']=True;state['restored_provider_sha256']=current['provider_sha256']
            except BaseException as e:state['restoration_error']=type(e).__name__+': '+str(e)
        if forward:forward.__exit__(None,None,None)
        if 'logging0' in locals():
            con=psycopg.connect('dbname=postgres',autocommit=True);state['logging_restored']=all(con.execute('select setting from pg_settings where name=%s',(k,)).fetchone()[0]==v for k,v in logging0.items());con.close()
        state['finished']=time.time();state['active']='';checkpoint()
        if guard:guard.terminate();guard.wait(timeout=5)
        print('COMPARISON REPORT',directory/'report.html',flush=True)
        if touched and not state['restored']:raise RuntimeError('Provider restoration requires recovery from private-baseline.json')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--resume',type=Path)
    main(parser.parse_args().resume)
