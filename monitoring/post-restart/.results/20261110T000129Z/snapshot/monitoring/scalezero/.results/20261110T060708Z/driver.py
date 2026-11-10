#!/usr/bin/env python3
"""Whole-application scale from zero, with warm dependencies and retained image caches."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime,timezone
import html
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('scaleout',ROOT.parent/'scaleout/run.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
K=s.K
JAVA=['inventory-service','notification-service','order-service','product-service']
EXPECTED_INVENTORY=[dict(id=1,sku_code='iphone_13',quantity=100),dict(id=2,sku_code='iphone_13_red',quantity=0)]


def save(path,value):
    path=Path(path);tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w') as file:os.chmod(tmp,0o600);json.dump(value,file,indent=2);file.write('\n')
    tmp.replace(path)


def inventory_fixture():
    """Assert only the two auto-recreated demo rows exist; credentials stay in RAM."""
    dep=s.read('deployment','java','inventory-service');values={};secrets={}
    for entry in s.b.container(dep,'inventory-service')['env']:
        name=entry['name']
        if name not in ['DATABASE_URL','DATABASE_USERNAME','DATABASE_PASSWORD']:continue
        if 'value' in entry:values[name]=entry['value']
        else:
            ref=entry['valueFrom']['secretKeyRef']
            if ref['name'] not in secrets:secrets[ref['name']]=s.read('secret','java',ref['name'])
            values[name]=base64.b64decode(secrets[ref['name']]['data'][ref['key']]).decode()
    url=urllib.parse.urlparse(values['DATABASE_URL'].removeprefix('jdbc:'))
    if url.hostname not in ['host.k3d.internal','host.docker.internal','localhost','127.0.0.1']:raise RuntimeError('Fixture inspection only supports the local PostgreSQL server')
    env=dict(os.environ,PGHOST='127.0.0.1',PGPORT=str(url.port or 5432),PGDATABASE=url.path.lstrip('/'),PGUSER=values['DATABASE_USERNAME'],PGPASSWORD=values['DATABASE_PASSWORD'],PGCONNECT_TIMEOUT='5')
    query="select coalesce(json_agg(t order by id),'[]'::json) from (select id,sku_code,quantity from public.t_inventory) t"
    result=subprocess.run(['psql','-X','-A','-t','-c',query],env=env,text=True,capture_output=True,timeout=10)
    if result.returncode:raise RuntimeError('Inventory fixture inspection failed; connection details omitted')
    rows=json.loads(result.stdout)
    if rows!=EXPECTED_INVENTORY:raise RuntimeError('Inventory contains non-demo data; refusing create-drop shutdown')
    return rows


def deployment_record(namespace,name):
    dep=s.read('deployment',namespace,name);pods=s.b.pods_for(namespace,dep)
    if dep['spec'].get('replicas',1)!=1 or len(pods)!=1 or not s.q.pod_ready(pods[0]):raise RuntimeError('Expected one stable ready pod: '+name)
    return dict(namespace=namespace,name=name,uid=dep['metadata']['uid'],original_replicas=1,template=dep['spec']['template'],template_hash=s.h.fingerprint(dep['spec']['template']),selector=dep['spec']['selector']['matchLabels'],images={c['name']:c['imageID'] for c in pods[0]['status']['containerStatuses']})


def snapshot():
    records=[deployment_record('java',name) for name in JAVA]+[deployment_record('wasm','wasmcloud-host')]
    host=s.read('wasmcloudhostconfig','wasm','wasmcloud-host')
    if host['spec']['hostReplicas']!=1:raise RuntimeError('HostConfig must start at one replica')
    runtime=next(c for c in records[-1]['template']['spec']['containers'] if c['name']=='wasmcloud-host')
    if host['spec'].get('image')!=runtime['image']:raise RuntimeError('HostConfig/runtime image mismatch')
    limits={e['name']:e.get('value') for e in runtime.get('env',[])}
    if limits.get('WASMCLOUD_MAX_EXECUTION_TIME_MS')!='5000' or limits.get('WASMCLOUD_RPC_TIMEOUT_MS')!='4000':raise RuntimeError('Expected the patched 5-second/4-second runtime limits')
    app=s.read('application','wasm','platform-poc')
    for component in app['spec']['components']:
        for trait in component.get('traits',[]):
            if trait['type']=='spreadscaler' and trait['properties'].get('spread'):raise RuntimeError('Temporary or partitioned component spreads are unsupported')
    return dict(records=records,hostconfig=dict(uid=host['metadata']['uid'],spec=host['spec']),application_version=app['metadata']['annotations']['version'],inventory_fixture=inventory_fixture())


def assert_template(record):
    dep=s.read('deployment',record['namespace'],record['name'])
    if dep['metadata']['uid']!=record['uid'] or s.h.fingerprint(dep['spec']['template'])!=record['template_hash']:raise RuntimeError('Concurrent or controller template change: '+record['name'])
    return dep


def patch_replicas(state,impl,count,dry_run=False):
    records=[r for r in state['records'] if r['namespace']==impl]
    for record in records:assert_template(record)
    def patch(record):
        kind='deployment';name=record['name'];ns=impl;field='replicas'
        if impl=='wasm':kind='wasmcloudhostconfig';field='hostReplicas'
        obj=s.read(kind,ns,name);expected_uid=state['hostconfig']['uid'] if impl=='wasm' else record['uid']
        if obj['metadata']['uid']!=expected_uid:raise RuntimeError('Resource identity changed')
        current=obj['spec'].get(field,1)
        if current not in [0,1]:raise RuntimeError('Concurrent replica edit')
        if impl=='wasm':
            spec=copy.deepcopy(obj['spec']);original=copy.deepcopy(state['hostconfig']['spec']);spec.pop('hostReplicas',None);original.pop('hostReplicas',None)
            if spec!=original:raise RuntimeError('Concurrent HostConfig spec edit')
        if current==count:return
        ops=[dict(op='test',path='/metadata/uid',value=expected_uid),dict(op='test',path='/metadata/resourceVersion',value=obj['metadata']['resourceVersion']),dict(op='test',path='/spec/'+field,value=current),dict(op='replace',path='/spec/'+field,value=count)]
        command=K+['-n',ns,'patch',kind,name,'--type=json','-p',json.dumps(ops)]
        if dry_run:command.append('--dry-run=server')
        subprocess.run(command,check=True,text=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=25)
    # Start Java's four pods together; preserve its normal application topology.
    with ThreadPoolExecutor(max_workers=len(records)) as pool:
        for future in [pool.submit(patch,r) for r in records]:future.result()


class Proxy:
    def __enter__(self):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.url='http://127.0.0.1:'+str(port);self.http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.process=subprocess.Popen(K+['proxy','--address=127.0.0.1','--port='+str(port),'--accept-hosts=^127\\.0\\.0\\.1$'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if self.process.poll() is not None:raise RuntimeError('API proxy exited')
                try:self.get('/version');return self
                except (OSError,urllib.error.URLError):time.sleep(.2)
            raise RuntimeError('API proxy startup timed out')
        except BaseException:self.__exit__(None,None,None);raise
    def get(self,path):
        with self.http.open(self.url+path,timeout=3) as response:return json.load(response)
    def pods(self,record):
        query=urllib.parse.urlencode(dict(labelSelector=','.join(k+'='+v for k,v in record['selector'].items())))
        return self.get('/api/v1/namespaces/'+record['namespace']+'/pods?'+query)['items']
    def __exit__(self,*_):
        self.process.terminate()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait()


def order(impl,timeout=10):
    payload=dict(orderLineItemsDtoList=[dict(skuCode='iphone_13',price=200,quantity=1)]) if impl=='java' else [dict(sku='ENG-V8-500',price=8500,quantity=1)]
    url='http://localhost:8081/api/order' if impl=='java' else 'http://localhost:8080/orders'
    began=time.monotonic()
    try:
        request=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request,timeout=timeout) as response:
            body=response.read().decode();status=response.status;ok=s.q.accepted(impl,status,body)
        return dict(status=status,accepted=ok,duration_seconds=time.monotonic()-began)
    except urllib.error.HTTPError as error:return dict(status=error.code,accepted=False,duration_seconds=time.monotonic()-began)
    except (OSError,urllib.error.URLError) as error:return dict(status=0,accepted=False,error=type(error).__name__,duration_seconds=time.monotonic()-began)


def wait_zero(state,impl,proxy,timeout=180):
    records=[r for r in state['records'] if r['namespace']==impl];deadline=time.monotonic()+timeout;clear_since=None
    while time.monotonic()<deadline:
        for record in records:assert_template(record)
        if all(not proxy.pods(record) for record in records):
            clear_since=clear_since or time.monotonic()
            if time.monotonic()-clear_since>=3:
                response=order(impl,timeout=3)
                if response['accepted']:raise RuntimeError('Orders still accepted at zero; another serving path exists')
                return
        else:clear_since=None
        time.sleep(1)
    raise RuntimeError('Application pods did not reach zero: '+impl)


class Pods:
    def __init__(self,records,proxy,output):
        self.records,self.proxy,self.output=records,proxy,output;self.origin=None;self.stop=threading.Event();self.lock=threading.Lock();self.data=dict(pods={},errors=[],all_ready_seconds=None)
    def run(self):
        while not self.stop.is_set():
            began=time.monotonic()
            try:
                current=[]
                for record in self.records:
                    pods=self.proxy.pods(record)
                    for pod in pods:
                        if pod['metadata'].get('deletionTimestamp'):continue
                        ready=s.q.pod_ready(pod);uid=pod['metadata']['uid']
                        with self.lock:
                            entry=self.data['pods'].setdefault(uid,dict(name=pod['metadata']['name'],deployment=record['name'],created_seconds=time.monotonic()-self.origin))
                            entry['currently_ready']=ready
                            if ready:
                                entry.setdefault('ready_seconds',time.monotonic()-self.origin)
                                images={c['name']:c['imageID'] for c in pod['status']['containerStatuses']}
                                allowed=record.get('allowed_image_ids')
                                if allowed:
                                    if set(images)!=set(allowed) or any(value not in allowed[name] for name,value in images.items()):raise RuntimeError('Image digest changed')
                                elif images!=record['images']:raise RuntimeError('Image digest changed')
                        current.append((record['name'],uid,ready))
                with self.lock:
                    if all(sum(name==r['name'] and ready for name,uid,ready in current)==1 for r in self.records) and len(current)==len(self.records):
                        if self.data['all_ready_seconds'] is None:self.data['all_ready_seconds']=time.monotonic()-self.origin
                        self.data['currently_all_ready']=True
                    else:self.data['currently_all_ready']=False
            except (OSError,ValueError,KeyError,RuntimeError,urllib.error.URLError) as error:
                with self.lock:self.data['errors'].append(dict(seconds=time.monotonic()-self.origin,error=str(error)))
            save(self.output/'pods.json',self.snapshot());self.stop.wait(max(0,1-(time.monotonic()-began)))
    def snapshot(self):
        with self.lock:return copy.deepcopy(self.data)


def trial(state,impl,pair,output,proxy,args):
    output.mkdir();records=[r for r in state['records'] if r['namespace']==impl]
    if impl=='java':
        if getattr(args,'zero_prepared',False):
            # Cache trials validated the fixture before stopping the app to
            # change placement. create-drop has now removed its demo tables.
            if any(assert_template(r)['spec'].get('replicas',1)!=0 or proxy.pods(r) for r in records):raise RuntimeError('Prepared-zero Java application is still running')
        else:inventory_fixture()
    print(f'Pair {pair}: stopping {impl}',flush=True);patch_replicas(state,impl,0);wait_zero(state,impl,proxy)
    save(output/'zero.json',dict(verified_zero=True,time=time.time(),records=[r['name'] for r in records]))
    monitor=Pods(records,proxy,output);monitor.origin=time.monotonic();wall=time.time();thread=threading.Thread(target=monitor.run,daemon=True);thread.start()
    probes=[];first=None;consecutive=0;healthy_start=None;result=None;completed=False
    try:
        print(f'Pair {pair}: starting {impl} from zero',flush=True);patch_replicas(state,impl,1);ack=time.monotonic()-monitor.origin
        deadline=monitor.origin+args.timeout
        while time.monotonic()<deadline:
            began=time.monotonic();response=order(impl);elapsed=time.monotonic()-monitor.origin;response.update(start_seconds=began-monitor.origin,end_seconds=elapsed);probes.append(response)
            if response['accepted']:
                if first is None:first=elapsed;print(impl,'first order after',round(first,2),'seconds',flush=True)
                consecutive+=1;healthy_start=healthy_start or began
            else:consecutive=0;healthy_start=None
            data=monitor.snapshot();ready=data.get('currently_all_ready',False)
            progress=dict(implementation=impl,pair=pair,elapsed=elapsed,first_order_seconds=first,all_pods_ready_seconds=data['all_ready_seconds'],consecutive_successes=consecutive,last_status=response['status'])
            save(output/'progress.json',progress)
            if first is not None and ready and healthy_start is not None and time.monotonic()-healthy_start>=args.stable_seconds:
                result=dict(implementation=impl,pair=pair,start_time=wall,scale_ack_seconds=ack,first_order_seconds=first,all_pods_ready_seconds=data['all_ready_seconds'],stable_serving_seconds=elapsed,probe_attempts=probes,pods=data,valid=not data['errors'],verified_zero=True);break
            time.sleep(max(0,1-(time.monotonic()-began)))
        if result is None:raise RuntimeError('Startup/healthy serving deadline expired: '+impl)
        for record in records:assert_template(record)
        if impl=='java':inventory_fixture()
        # These checks occur after startup timing; they must not delay the first-order probe.
        if impl=='java':
            for url in ['http://localhost:8081/api/product','http://localhost:8081/api/inventory?skuCode=iphone_13']:
                with urllib.request.urlopen(url,timeout=10) as response:
                    if response.status!=200:raise RuntimeError('Java functional endpoint failed')
        else:
            with s.NatsForward(14223):
                data=json.loads(subprocess.check_output(['wash','get','inventory','--ctl-host','127.0.0.1','--ctl-port','14223','--lattice','default','--timeout-ms','10000','-o','json'],text=True,timeout=15))
                inventories=data.get('inventories',[])
                if len(inventories)!=1:raise RuntimeError('Expected exactly one running lattice host')
                runtime=inventories[0];components={c['id']:c['max_instances'] for c in runtime.get('components',[])}
                if components!=state['component_counts']:raise RuntimeError('Wasm component counts did not return')
                if not set(state['providers']).issubset(p['id'] for p in runtime.get('providers',[])):raise RuntimeError('Wasm providers missing')
                result['runtime_verified']=dict(seconds=time.monotonic()-monitor.origin,host_id=runtime['host_id'],components=components,providers=[p['id'] for p in runtime['providers']])
        save(output/'result.json',result);completed=True;return result
    finally:
        monitor.stop.set();thread.join(timeout=15);save(output/'probes.json',probes)
        if not completed:save(output/'failure.json',dict(pods=monitor.snapshot(),probes=probes))


def runtime_snapshot(state):
    with s.NatsForward(14223):
        data=json.loads(subprocess.check_output(['wash','get','inventory','--ctl-host','127.0.0.1','--ctl-port','14223','--lattice','default','--timeout-ms','10000','-o','json'],text=True,timeout=15))
        inventories=data.get('inventories',[])
        if len(inventories)!=1:raise RuntimeError('One baseline lattice host required')
        host=inventories[0];state['component_counts']={c['id']:c['max_instances'] for c in host.get('components',[])};state['providers']=[p['id'] for p in host.get('providers',[])]


def restore(state):
    errors=[]
    for impl in ['java','wasm']:
        try:
            patch_replicas(state,impl,1)
            for record in state['records']:
                if record['namespace']==impl:
                    subprocess.run(K+['-n',impl,'rollout','status','deployment/'+record['name'],'--timeout=180s'],check=True,stdout=subprocess.DEVNULL,timeout=190);assert_template(record)
            s.b.wait_functional(impl)
        except Exception as error:errors.append(impl+': '+str(error))
    if not errors:inventory_fixture()
    if errors:raise RuntimeError('; '.join(errors))
    host=s.read('wasmcloudhostconfig','wasm','wasmcloud-host')
    app=s.read('application','wasm','platform-poc')
    if host['spec']!=state['hostconfig']['spec']:raise RuntimeError('HostConfig was not fully restored')
    if app['metadata']['annotations']['version']!=state['application_version']:raise RuntimeError('Application version changed')


def report(directory,data):
    save(directory/'report.json',data);esc=lambda value:html.escape(str(value));fmt=lambda value:'—' if value is None else f'{value:.2f}'
    page='<!doctype html><meta charset="utf-8"><title>Application scale from zero</title>'
    if data['status']=='running':page+='<meta http-equiv="refresh" content="5">'
    page+='<style>body{font:18px system-ui;background:#101827;color:#eef;max-width:1200px;margin:35px auto;padding:20px}table{border-collapse:collapse;width:100%}td,th{padding:12px;border-bottom:1px solid #456;text-align:left}a{color:#8bc9ff}</style><h1>Application scale from zero</h1>'
    page+='<p>Status: '+esc(data['status'])+'; completed trials: '+str(len(data['trials']))+'. '+esc(data.get('current',''))+'</p>'
    page+='<p>Four Java application pods versus one wasmCloud serving host (including providers/components). PostgreSQL, MongoDB, Redis, NATS, wadm, operator, ingress and monitoring stay running. Container caches are retained; pull policies are unchanged. Wasm component artifacts may be fetched/compiled in each new host. This measures fresh application processes, not fresh-node or image-download startup. The runtime keeps the patched image and 5-second/4-second limits.</p>'
    page+='<p>Clock starts before requesting replicas 0→1. A 1-second probe requires HTTP 201 and the application success body, with the same 10-second timeout, through persistent ingress. Native pod readiness and a consecutive healthy-serving interval are separate measurements. Java inventory uses create-drop: every trial checks that only the original two demo rows exist and checks them again after startup. Its schema/seed work is included in Java startup.</p>'
    page+='<table><tr><th>Implementation</th><th>Valid trials</th><th>First order median [min–max], s</th><th>All pods Ready median, s</th><th>Stable serving median, s</th></tr>'
    for impl in ['java','wasm']:
        rows=[r for r in data['trials'] if r['implementation']==impl and r['valid']]
        points=[r['first_order_seconds'] for r in rows]
        cells=[impl,len(rows),'—' if not points else fmt(statistics.median(points))+' ['+fmt(min(points))+'–'+fmt(max(points))+']','—' if not rows else fmt(statistics.median(r['all_pods_ready_seconds'] for r in rows)),'—' if not rows else fmt(statistics.median(r['stable_serving_seconds'] for r in rows))]
        page+='<tr>'+''.join('<td>'+esc(c)+'</td>' for c in cells)+'</tr>'
    page+='</table><h2>Time to first accepted order</h2>'
    maximum=max([r['first_order_seconds'] for r in data['trials']]+[1])
    for row in data['trials']:
        colour='#65baff' if row['implementation']=='java' else '#cc98ff'
        page+='<div style="display:flex;align-items:center;gap:12px;margin:12px 0"><span style="width:130px">Pair '+str(row['pair'])+' '+esc(row['implementation'])+'</span><span style="display:block;height:24px;background:'+colour+';width:'+str(65*row['first_order_seconds']/maximum)+'%"></span><span>'+fmt(row['first_order_seconds'])+' s</span></div>'
    page+='<h2>Every trial</h2><table><tr><th>Pair</th><th>Application</th><th>Valid</th><th>Scale ack, s</th><th>First order, s</th><th>All pods Ready, s</th><th>Stable serving, s</th></tr>'
    for row in data['trials']:
        cells=[row['pair'],row['implementation'],row['valid'],fmt(row['scale_ack_seconds']),fmt(row['first_order_seconds']),fmt(row['all_pods_ready_seconds']),fmt(row['stable_serving_seconds'])]
        page+='<tr>'+''.join('<td>'+esc(c)+'</td>' for c in cells)+'</tr>'
    page+='</table><p>Restored: '+esc(data.get('restored',False))+'. '+esc(data.get('error',''))+' '+esc(data.get('restoration_error',''))+'</p><p>Full raw data: report.json, trial probes/pod records, private recovery state.json. Invalid and failed trials are retained.</p>'
    tmp=directory/'report.html.tmp';tmp.write_text(page);tmp.replace(directory/'report.html')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['plan','run','restore']);parser.add_argument('--pairs',type=int,default=5);parser.add_argument('--target',choices=['java','wasm','both'],default='both');parser.add_argument('--stable-seconds',type=int,default=10);parser.add_argument('--timeout',type=int,default=240);parser.add_argument('--state',type=Path);args=parser.parse_args()
    if min(args.pairs,args.stable_seconds,args.timeout)<=0:parser.error('Positive settings required')
    if args.action=='restore':
        if not args.state:parser.error('--state required')
        restore(json.loads(args.state.read_text()));return
    state=snapshot();runtime_snapshot(state)
    for impl in ['java','wasm']:s.b.wait_functional(impl);patch_replicas(state,impl,0,dry_run=True)
    if args.action=='plan':print('Zero-replica server validations and functional checks passed; no workload changed.');return
    if sys.platform=='darwin':
        power=subprocess.check_output(['pmset','-g','batt'],text=True,timeout=10)
        if "'AC Power'" not in power:raise RuntimeError('Connect mains before running scale-from-zero')
    directory=ROOT/'.results'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');directory.mkdir(parents=True);save(directory/'state.json',state)
    source=Path(__file__).read_bytes();(directory/'driver.py').write_bytes(source)
    data=dict(status='running',settings=vars(args)|{'state':None},trials=[],restored=False);report(directory,data)
    data['driver_sha256']=hashlib.sha256(source).hexdigest()
    if sys.platform=='darwin':data['power_before']=power
    guard=subprocess.Popen(['caffeinate','-i','-s']) if sys.platform=='darwin' else None
    print('Report:',directory/'report.html',flush=True)
    try:
        with Proxy() as proxy:
            for pair in range(1,args.pairs+1):
                order_impl=['java','wasm'] if pair%2 else ['wasm','java']
                for impl in order_impl:
                    if args.target!='both' and impl!=args.target:continue
                    if sys.platform=='darwin' and "'AC Power'" not in subprocess.check_output(['pmset','-g','batt'],text=True,timeout=10):raise RuntimeError('Mains disconnected; stopping')
                    data['current']=f'Pair {pair}: {impl}';report(directory,data)
                    result=trial(state,impl,pair,directory/f'pair-{pair:02d}-{impl}',proxy,args);data['trials'].append(result);report(directory,data)
                    s.b.wait_functional(impl)
        data['status']='complete'
    except BaseException as error:data.update(status='interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',error=str(error));raise
    finally:
        try:restore(state);data['restored']=True
        except BaseException as error:data.update(status='failed',restoration_error=str(error))
        if sys.platform=='darwin':data['power_after']=subprocess.check_output(['pmset','-g','batt'],text=True,timeout=10)
        report(directory,data)
        if guard:guard.terminate();guard.wait()
        print('Report:',directory/'report.html',flush=True)
        print('Recovery: python3 monitoring/scalezero/run.py restore --state '+str(directory/'state.json'),flush=True)
        if not data['restored']:raise RuntimeError('Restoration failed: '+data.get('restoration_error','unknown'))

if __name__=='__main__':main()
