#!/usr/bin/env python3
"""Matched database-path diagnostics; no application image or schema changes.

Requires psycopg. Temporarily enables PostgreSQL duration logging via reload,
then restores the exact pre-existing ALTER SYSTEM overrides in finally.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time
import urllib.request

import psycopg
from psycopg import sql

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parent.parent
K=['kubectl','--context','k3d-platform-poc']
DATABASES=['inventory-service','order-service','platform-poc-wasm']
SETTINGS={'log_line_prefix':'%m [%p] user=%u db=%d app=%a ', 'log_parameter_max_length':'0', 'log_min_duration_statement':'0'}

def save(p,d):p.write_text(json.dumps(d,indent=2,default=str)+'\n')
def cmd(a):return subprocess.check_output(a,text=True,stderr=subprocess.PIPE,timeout=25)
def request(url):
    with urllib.request.urlopen(url,timeout=5) as r:return json.load(r)
def quantiles(values):
    if not values:return {'count':0}
    x=sorted(values)
    return dict(count=len(x),mean=statistics.mean(x),median=statistics.median(x),p95=x[min(len(x)-1,int(.95*(len(x)-1)))],maximum=max(x),total=sum(x))
def physical():
    if "'AC Power'" not in cmd(['pmset','-g','batt']) or re.search(r'AppleClamshellState"\s*=\s*Yes',cmd(['ioreg','-r','-k','AppleClamshellState','-d','4'])):raise RuntimeError('Mains power and an open lid required')
def pod_identity():
    rows=json.loads(cmd(K+['get','pods','-A','-o','json']))['items'];out={}
    for p in rows:
        ns=p['metadata']['namespace']
        if ns=='java' or ns=='wasm' and p['metadata']['name'].startswith('wasmcloud-host-'):
            for c in p['status'].get('containerStatuses',[]):
                if not c['ready']:raise RuntimeError('Unready application pod')
            out[ns+'/'+p['metadata']['name']]=dict(uid=p['metadata']['uid'],node=p['spec']['nodeName'],statuses=[{k:c[k] for k in ['name','restartCount','imageID']} for c in p['status']['containerStatuses']],pod=p)
    if len(out)!=5:raise RuntimeError('Expected four Java pods and one Wasm host')
    return out
def pool_metrics():
    out={}
    for svc,port in [('order-service',8081),('inventory-service',8080)]:
        out[svc]={}
        for metric in ['hikaricp.connections.active','hikaricp.connections.pending','hikaricp.connections.max','hikaricp.connections.acquire','hikaricp.connections.timeout']:
            url=f'http://127.0.0.1:18001/api/v1/namespaces/java/services/{svc}:{port}/proxy/actuator/metrics/{metric}'
            try:out[svc][metric]=request(url)
            except Exception as e:out[svc][metric]={'error':str(e)}
    return out
def normalize(query):
    query=re.sub(r"'(?:[^']|'')*'",'?',query)
    query=re.sub(r'\b\d+(?:\.\d+)?\b','?',query)
    return re.sub(r'\s+',' ',query).strip()
def parse_log(text):
    blocks=re.split(r'(?=^\d{4}-\d\d-\d\d .*?\[\d+\] user=)',text,flags=re.M)
    results=[]
    for block in blocks:
        meta=re.search(r'\[(\d+)\] user=(.*?) db=(.*?) app=(.*?) (LOG|DETAIL|STATEMENT|ERROR):',block)
        if not meta or meta[3] not in DATABASES or meta[5]!='LOG':continue
        m=re.search(r'duration:\s+([\d.]+) ms\s+(parse|bind|execute|statement)(?:\s+([^:]*))?:\s*([\s\S]*)',block)
        if not m:continue
        query=m[4].strip()
        results.append(dict(pid=int(meta[1]),database=meta[3],phase=m[2],ms=float(m[1]),query=query,normalized=normalize(query)))
    return results

def main():
    os.umask(0o077)
    out=ROOT/'.results'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');out.mkdir(parents=True)
    guard=subprocess.Popen(['caffeinate','-i','-s']);proxy=None;load=None;connection=None;original={};changed=[]
    status={'state':'preparing','directory':str(out),'stages':{},'method':'100/s;60s warmup;90s measurement; unchanged live images; PostgreSQL duration logging +10Hz activity sampling +Hikari pool metrics +provider process CPU'}
    save(out/'report.json',status)
    try:
        physical()
        if subprocess.run(['pgrep','-x','k6'],stdout=subprocess.DEVNULL).returncode==0:raise RuntimeError('Another k6 process is running')
        before=pod_identity();save(out/'pods-before.json',before)
        connection=psycopg.connect('dbname=postgres application_name=provider-profile-observer',autocommit=True)
        for setting in SETTINGS:
            row=connection.execute('select setting,source,sourcefile from pg_settings where name=%s',(setting,)).fetchone()
            override=connection.execute("select setting from pg_file_settings where name=%s and sourcefile like '%%/postgresql.auto.conf' and applied order by seqno desc limit 1",(setting,)).fetchone()
            original[setting]={'effective':row[0],'source':row[1],'auto_override':override[0] if override else None}
        save(out/'logging-before.json',original)
        proxylog=(out/'proxy.log').open('w');proxy=subprocess.Popen(K+['proxy','--port=18001'],stdout=proxylog,stderr=proxylog)
        for _ in range(30):
            try:request('http://127.0.0.1:18001/version');break
            except Exception:time.sleep(.2)
        host=next(v['pod'] for k,v in before.items() if k.startswith('wasm/'));node=host['spec']['nodeName']
        cid=next(c['containerID'].split('://')[1] for c in host['status']['containerStatuses'] if c['name']=='wasmcloud-host')
        hostpid=json.loads(cmd(['docker','exec',node,'crictl','inspect',cid]))['info']['pid']
        children=cmd(['docker','exec',node,'sh','-c',f'for p in {hostpid} $(cat /proc/{hostpid}/task/*/children); do echo "$p $(readlink /proc/$p/exe)"; done'])
        processes={int(line.split()[0]):line.split()[1] for line in children.splitlines()}
        if sum('sqldb_postgres_0_7_2' in name for name in processes.values())!=1:raise RuntimeError('Cannot uniquely identify pinned PostgreSQL provider')
        ticks=int(cmd(K+['-n','wasm','exec',host['metadata']['name'],'-c','wasmcloud-host','--','getconf','CLK_TCK']).strip())
        def cpu():
            now=time.monotonic();raw=cmd(['docker','exec',node,'sh','-c','cat '+' '.join(f'/proc/{p}/stat' for p in processes)])
            values={}
            for line in raw.splitlines():
                pid=int(line.split()[0]);fields=line.rsplit(')',1)[1].split();values[pid]=(int(fields[11])+int(fields[12]))/ticks
            return dict(time=now,seconds=values)
        save(out/'processes.json',{'node':node,'processes':processes,'clock_ticks':ticks})
        metrics=pool_metrics();save(out/'pool-preflight.json',metrics)
        if 'error' in metrics['order-service']['hikaricp.connections.acquire']:raise RuntimeError('Order-service pool acquisition metrics unavailable; inspect preflight')
        if 'error' in metrics['inventory-service']['hikaricp.connections.acquire']:
            status.setdefault('limitations',[]).append('Inventory Actuator pool metrics are not exposed; no inventory pool-wait duration can be inferred. Its PostgreSQL sessions and waits are still sampled.')
        logfile=Path('/opt/homebrew/var/log/postgresql@15.log')
        status['state']='running';save(out/'report.json',status)
        for impl in ['java','wasm']:
            print('WARMUP',impl,'60s',flush=True)
            script=(REPO/f'orders_{impl}.js').read_text()
            if impl=='java':script,n=re.subn(r'duration: "[^"]+",','duration: __ENV.TEST_DURATION || "60s",',script,count=1);assert n==1
            copy=out/f'orders_{impl}.js';copy.write_text(script)
            env=dict(os.environ,TEST_DURATION='60s',ORDER_VUS='250',BASE_URL='http://localhost:8080')
            with (out/f'{impl}-warmup.log').open('w') as log:
                load=subprocess.Popen(['k6','run','--quiet','--summary-export='+str(out/f'{impl}-warmup-summary.json'),str(copy)],env=env,stdout=log,stderr=subprocess.STDOUT)
                while load.poll() is None:physical();time.sleep(2)
                if load.returncode:raise RuntimeError('Warmup failed')
                load=None
            warm=json.loads((out/f'{impl}-warmup-summary.json').read_text())['metrics']
            if warm['checks']['fails'] or warm.get('dropped_iterations',{}).get('count',0):raise RuntimeError('Warmup had failed or dropped orders')
            for setting,value in SETTINGS.items():
                connection.execute(sql.SQL('ALTER SYSTEM SET {} TO {}').format(sql.Identifier(setting),sql.Literal(value)))
                if setting not in changed:changed.append(setting)
            connection.execute('select pg_reload_conf()');time.sleep(1)
            offset=logfile.stat().st_size;start=time.time();cpu0=cpu();pools0=pool_metrics();activity=[];pools=[];nextpool=0
            print('MEASURE',impl,'90s',flush=True)
            with (out/f'{impl}-k6.log').open('w') as log:
                load=subprocess.Popen(['k6','run','--quiet','--summary-export='+str(out/f'{impl}-summary.json'),str(copy)],env=dict(env,TEST_DURATION='90s'),stdout=log,stderr=subprocess.STDOUT)
                while load.poll() is None:
                    physical() if len(activity)%50==0 else None
                    rows=connection.execute("select pid,datname,usename,application_name,state,wait_event_type,wait_event,extract(epoch from clock_timestamp()-query_start)::float as query_age_s from pg_stat_activity where datname=any(%s) and pid<>pg_backend_pid() and backend_type='client backend'",(DATABASES,)).fetchall()
                    activity.append({'time':time.time(),'rows':rows})
                    if time.monotonic()>nextpool:pools.append({'time':time.time(),'metrics':pool_metrics()});nextpool=time.monotonic()+5
                    time.sleep(.1)
                exitcode=load.returncode;load=None
            end=time.time();cpu1=cpu();pools1=pool_metrics()
            # Disable duration logging before reading or analysing this interval.
            connection.execute("ALTER SYSTEM SET log_min_duration_statement TO '-1'");connection.execute('select pg_reload_conf()');time.sleep(1)
            with logfile.open('rb') as stream:stream.seek(offset);raw=stream.read().decode(errors='replace')
            entries=parse_log(raw);save(out/f'{impl}-statements.json',entries);save(out/f'{impl}-activity.json',activity);save(out/f'{impl}-pools.json',{'before':pools0,'samples':pools,'after':pools1})
            groups=defaultdict(list)
            for entry in entries:groups[(entry['database'],entry['phase'],entry['normalized'])].append(entry['ms'])
            sqlrows=[dict(database=db,phase=phase,sql=query,timing_ms=quantiles(values)) for (db,phase,query),values in groups.items()]
            native=[dict(pid=pid,executable=name,cpu_seconds=cpu1['seconds'][pid]-cpu0['seconds'][pid],mean_cores=(cpu1['seconds'][pid]-cpu0['seconds'][pid])/(cpu1['time']-cpu0['time'])) for pid,name in processes.items()]
            status['stages'][impl]={'exit_code':exitcode,'start':start,'end':end,'sql':sqlrows,'native_processes':native,'k6':json.loads((out/f'{impl}-summary.json').read_text()),'activity_sample_count':len(activity)}
            save(out/'report.json',status);print('FINISHED',impl,'statements',len(entries),flush=True)
            if exitcode:status.setdefault('performance_failures',[]).append(impl+' load thresholds failed; retained')
            stable=lambda data:{k:{field:v[field] for field in ['uid','node','statuses']} for k,v in data.items()}
            if stable(pod_identity())!=stable(before):raise RuntimeError('Application pods or images changed during diagnostics')
        status['state']='completed'
    except BaseException as e:status.update(state='failed',error=type(e).__name__+': '+str(e));raise
    finally:
        if load and load.poll() is None:
            load.terminate()
            try:load.wait(timeout=15)
            except subprocess.TimeoutExpired:load.kill();load.wait()
        if connection:
            try:
                for setting in changed:
                    old=original[setting]['auto_override']
                    statement=sql.SQL('ALTER SYSTEM RESET {}').format(sql.Identifier(setting)) if old is None else sql.SQL('ALTER SYSTEM SET {} TO {}').format(sql.Identifier(setting),sql.Literal(old))
                    connection.execute(statement)
                if changed:connection.execute('select pg_reload_conf()');time.sleep(1)
                live=psycopg.connect('dbname=postgres application_name=provider-profile-final-check',autocommit=True)
                status['logging_restored']=all(live.execute('select setting from pg_settings where name=%s',(k,)).fetchone()[0]==v['effective'] for k,v in original.items());live.close()
            finally:connection.close()
        if proxy:proxy.terminate();proxy.wait(timeout=10)
        guard.terminate();guard.wait(timeout=5)
        status['finished']=time.time();save(out/'report.json',status);print('PROFILE REPORT',out/'report.json',flush=True)

if __name__=='__main__':main()
