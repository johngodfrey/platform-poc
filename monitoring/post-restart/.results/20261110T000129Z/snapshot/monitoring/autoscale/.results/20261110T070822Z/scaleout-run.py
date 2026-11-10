#!/usr/bin/env python3
"""Order-tier scaling: Java order pods versus new Wasm hosts or existing-host concurrency."""
import argparse
import copy
from datetime import datetime, timezone
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('capacity',ROOT.parent/'capacity/run.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
b=c.b
K=b.K
hspec=importlib.util.spec_from_file_location("scaleout_host",ROOT/"host_mode.py")
h=importlib.util.module_from_spec(hspec);hspec.loader.exec_module(h)
qspec=importlib.util.spec_from_file_location("scaleout_readiness",ROOT/"readiness.py")
q=importlib.util.module_from_spec(qspec);qspec.loader.exec_module(q)

def host_mode(state,port,monitor=None):
    return h.HostMode(state,port,SimpleNamespace(b=b,read=read,save=save,runtime_scale=runtime_scale,scale=scale,readiness=monitor))

def capacity(state,impl,port,monitor=None):
    if impl=="wasm" and state.get("wasm_mode")=="host":
        data=host_mode(state,port,monitor).observe()
        return dict(observed=data.pop("serving_hosts"),**data)
    if impl=="java" and monitor is not None and monitor.snapshot().get("request_started_at"):
        data=monitor.snapshot()
        return dict(observed=monitor.ready_count(),ready_pods=sum(p.get("currently_ready",False) for p in data["pods"].values()))
    return dict(observed=observed(impl,port))

def bounds(state,impl):
    return (1,2) if impl=="wasm" and state.get("wasm_mode")=="host" else (state[impl]["original"],state[impl]["target"])


def read(kind, namespace, name):
    return c.retry_read(lambda: json.loads(subprocess.check_output(K+['-n',namespace,'get',kind,name,'-o','json'],text=True)))


def save(path,data):
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(data,indent=2)+'\n');tmp.replace(path)


def component_path(app):
    for i,component in enumerate(app['spec']['components']):
        if component['name']=='orders-service':
            for j,trait in enumerate(component.get('traits',[])):
                if trait['type']=='spreadscaler':
                    return f'/spec/components/{i}/traits/{j}/properties/instances',trait['properties']['instances']
    raise RuntimeError('orders-service spreadscaler is missing')


def snapshot(targets,factor,wasm_mode="concurrency"):
    state=dict(targets=targets,factor=factor,wasm_mode=wasm_mode,wasm_actuator='versioned desired count plus explicit runtime scale command')
    if 'java' in targets:
        d=b.get('java','order-service');original=d['spec'].get('replicas',1)
        if original!=1: raise RuntimeError('Java baseline must have exactly one order pod')
        ddl=[e.get('value') for e in b.container(d,'order-service').get('env',[]) if e['name']=='SPRING_JPA_HIBERNATE_DDL_AUTO']
        if ddl!=['update']: raise RuntimeError('Java order-service must use Hibernate update before replica scaling')
        state['java']=dict(uid=d['metadata']['uid'],original=original,target=original*factor,image=b.container(d,'order-service')['image'],resources=copy.deepcopy(b.container(d,'order-service').get('resources',{})))
        pods=b.pods_for('java',d)
        if len(pods)!=1 or not q.pod_ready(pods[0]):raise RuntimeError('Expected one stable ready Java order pod')
        state['java']['source_template_hash']=h.fingerprint(d['spec']['template'])
        state['java'].update(baseline_pod_uids=[p['metadata']['uid'] for p in pods],pod_selector=','.join(k+'='+v for k,v in d['spec']['selector']['matchLabels'].items()),images={c['name']:c['imageID'] for c in pods[0]['status']['containerStatuses']})
    if 'wasm' in targets:
        app=read('application','wasm','platform-poc');_,original=component_path(app)
        version=app['metadata'].get('annotations',{}).get('version')
        if not version: raise RuntimeError('Application version annotation is required')
        state['wasm']=dict(uid=app['metadata']['uid'],original=original,target=original*factor,version=version,
                           scaled_version=version+'-scaleout-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    if 'wasm' in state:
        state['wasm']['scaled_uid']=str(uuid.uuid5(uuid.NAMESPACE_OID,'platform-poc/'+state['wasm']['scaled_version']))
    if "wasm" in targets and wasm_mode=="host":
        if factor!=2:raise RuntimeError("Host mode currently adds exactly one host; use --factor 2")
        state["wasm"]["host"]=h.new_record()
        state["wasm_actuator"]="temporary additional host Deployment, pinned component spreads and verified HTTP routing"
        host_mode(state,14223).snapshot_template()
    return state


def runtime_scale(record,count,port):
    runtime=record.get('runtime')
    if not runtime:return
    cmd=['wash','scale','component',runtime['host_id'],runtime['image_ref'],runtime.get('component_id','orders-service'),
         '--max-instances',str(count),'--skip-wait','--ctl-host','127.0.0.1',
         '--ctl-port',str(port),'--lattice','default','--timeout-ms','10000']
    for key,value in runtime['annotations'].items():cmd+=['--annotations',key+'='+value]
    subprocess.run(cmd,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=20)


def record_runtime(state,port):
    data=json.loads(subprocess.check_output(['wash','get','inventory','--ctl-host','127.0.0.1','--ctl-port',str(port),'--lattice','default','-o','json'],text=True,timeout=10))
    found=[(host,component) for host in data.get('inventories',[]) for component in host.get('components',[]) if component['id']=='orders-service']
    if len(found)!=1:raise RuntimeError('This experiment requires one Wasm host running orders-service')
    host,component=found[0]
    app=read('application','wasm','platform-poc')
    component_spec=next(x for x in app['spec']['components'] if x['name']=='orders-service')
    if component_spec['properties'].get('config'):raise RuntimeError('Named orders component config needs explicit support before native scaling')
    state['wasm']['runtime']=dict(host_id=host['host_id'],image_ref=component['image_ref'],annotations=component.get('annotations',{}))
    if state.get('wasm_mode')=='host':host_mode(state,port).record_runtime(host,app)


def scale(state,impl,restore=False,dry_run=False,port=14223):
    if impl=="wasm" and state.get("wasm_mode")=="host":
        mode=host_mode(state,port)
        if dry_run:return mode.validate()
        if restore:return mode.restore()
        return mode.apply()
    record=state[impl]
    kind,ns,name=('deployment','java','order-service') if impl=='java' else ('application','wasm','platform-poc')
    obj=read(kind,ns,name)
    if obj['metadata']['uid'] not in [record['uid'], record.get('scaled_uid',record['uid'])]: raise RuntimeError('Resource identity changed; refusing scale overwrite')
    if impl=='java' and 'image' in record:
        current_container=b.container(obj,'order-service')
        if current_container['image']!=record['image'] or not b.same_resources(current_container.get('resources',{}),record['resources']):raise RuntimeError('Concurrent Java image/resource edit')
    path,current=('/spec/replicas',obj['spec'].get('replicas',1)) if impl=='java' else component_path(obj)
    desired=record['original'] if restore else record['target']
    if current not in [record['original'],record['target']]: raise RuntimeError('Concurrent scaling edit; refusing overwrite')
    if impl=='wasm':
        version=obj['metadata'].get('annotations',{}).get('version')
        if version not in [record['version'],record['scaled_version']]: raise RuntimeError('Concurrent application version edit')
        wanted_version=record['version'] if restore else record['scaled_version']
        if current==desired and version==wanted_version:
            if restore:runtime_scale(record,desired,port)
            return
        ctl=['--ctl-host','127.0.0.1','--ctl-port',str(port),'--lattice','default','--timeout-ms','10000']
        if restore:
            subprocess.run(['wash','app','deploy','platform-poc',record['version']]+ctl,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
            runtime_scale(record,desired,port)
            return
        # Operator 0.4 only implements merge patch, ignores JSON Patch tests and
        # does not honor dryRun. Use wadm's versioned CLI, validating locally.
        manifest=dict(apiVersion='core.oam.dev/v1beta1',kind='Application',metadata=dict(name='platform-poc',annotations=copy.deepcopy(obj['metadata'].get('annotations',{}))),spec=copy.deepcopy(obj['spec']))
        manifest['metadata']['annotations'].pop('kubectl.kubernetes.io/last-applied-configuration',None)
        manifest['metadata']['annotations']['version']=wanted_version
        target=manifest
        parts=path.strip('/').split('/')
        for part in parts[:-1]:target=target[int(part)] if isinstance(target,list) else target[part]
        target[parts[-1]]=desired
        # Manifests can contain existing inline provider configuration. Keep the
        # temporary file private and delete it; never persist it with results.
        with tempfile.NamedTemporaryFile(mode='w',suffix='.json') as file:
            json.dump(manifest,file);file.flush()
            subprocess.run(['wash','app','validate',file.name],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
            if not dry_run:
                subprocess.run(['wash','app','deploy',file.name]+ctl,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
                runtime_scale(record,desired,port)
        return
    if current==desired:return
    ops=[dict(op='test',path='/metadata/resourceVersion',value=obj['metadata']['resourceVersion']),dict(op='test',path=path,value=current),dict(op='replace',path=path,value=desired)]
    cmd=K+['-n',ns,'patch',kind,name,'--type=json','-p',json.dumps(ops)]
    if dry_run:cmd.append('--dry-run=server')
    subprocess.run(cmd,check=True,text=True,stdout=subprocess.DEVNULL,timeout=30)


class NatsForward:
    def __init__(self,port):self.port=port;self.process=None
    def __enter__(self):
        with socket.socket() as probe:probe.bind(('127.0.0.1',self.port))
        self.process=subprocess.Popen(K+['-n','wasm','port-forward','--address=127.0.0.1','pod/nats-0',f'{self.port}:4222'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                if self.process.poll() is not None:raise RuntimeError('NATS forwarding exited')
                try:
                    with socket.create_connection(('127.0.0.1',self.port),timeout=.3):return self
                except OSError:time.sleep(.3)
            raise RuntimeError('NATS forwarding timed out')
        except BaseException:self.__exit__(None,None,None);raise
    def __exit__(self,*_):
        if self.process:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()


def observed(impl,port):
    if impl=='java':
        d=b.get('java','order-service')
        pods=b.pods_for('java',d)
        return sum(not p['metadata'].get('deletionTimestamp') and bool(p['status'].get('containerStatuses')) and all(x['ready'] for x in p['status']['containerStatuses']) for p in pods)
    data=json.loads(subprocess.check_output(['wash','get','inventory','--ctl-host','127.0.0.1','--lattice','default','--ctl-port',str(port),'-o','json','--timeout-ms','3000'],text=True,timeout=10))
    if not data.get('success'):raise RuntimeError('Runtime inventory query failed')
    return sum(x['max_instances'] for host in data.get('inventories',[]) for x in host.get('components',[]) if x['id']=='orders-service')


def wait_original(state,impl,port):
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        try:
            if observed(impl,port)==state[impl]['original']:
                if impl!='java' or len(b.pods_for('java',b.get('java','order-service')))==state[impl]['original']:
                    b.wait_functional(impl)
                    if impl=='wasm':
                        versions=json.loads(subprocess.check_output(['wash','app','list','--ctl-host','127.0.0.1','--ctl-port',str(port),'--lattice','default','-o','json'],text=True,timeout=10))
                        deployed=next((a.get('deployed_version') for a in versions.get('applications',[]) if a['name']=='platform-poc'),None)
                        if deployed!=state[impl]['version']:
                            raise RuntimeError('Stored and deployed application versions differ from the required original')
                        app=read('application','wasm','platform-poc')
                        version=app['metadata'].get('annotations',{}).get('version')
                        if version==state[impl]['scaled_version']:
                            subprocess.run(['wash','app','delete','platform-poc',version,'--ctl-host','127.0.0.1','--ctl-port',str(port),'--lattice','default','--timeout-ms','10000'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
                        elif version!=state[impl]['version']:raise RuntimeError('Concurrent application version after restoration')
                    return
        except (RuntimeError,subprocess.SubprocessError):pass
        time.sleep(2)
    raise RuntimeError('Original serving capacity did not return: '+impl)


def recovery_windows(summary,args,command_s,ready_s=None):
    metrics=summary['metrics'];rows=[]
    burst_start=args.warmup+args.baseline+1;burst_end=burst_start+args.before_scale+args.after_scale
    for w in range(math.ceil(burst_end/args.window)):
        start,end=w*args.window,(w+1)*args.window
        if start<burst_start or end>burst_end:continue
        def value(metric,key,default=0):return metrics.get(f'{metric}{{window:{w}}}',{}).get(key,default)
        p95=value('scaleout_latency_ms','p(95)',None)
        count=value('scaleout_completed','count')
        fails=value('scaleout_failures','count')
        accepted=value('scaleout_accepted','count')
        passed=p95 is not None and p95<args.p95_ms and fails==0 and accepted==count and count>=args.burst_rate*args.window-1
        rows.append(dict(start_s=start,end_s=end,accepted=accepted,completed=count,failures=fails,p95_ms=p95,passed=passed))
    def pair_passes(left,right):
        # Completion jitter at a bucket boundary must not manufacture a recovery
        # claim. Evaluate delivered throughput across the full 30-second pair.
        return (left['end_s']==right['start_s'] and
                all(r['p95_ms'] is not None and r['p95_ms']<args.p95_ms and r['failures']==0 and r['accepted']==r['completed'] for r in [left,right]) and
                left['completed']+right['completed']>=args.burst_rate*args.window*2-1)
    before=[r for r in rows if r['end_s']<=command_s]
    already_healthy=len(before)>=2 and pair_passes(*before[-2:])
    recovery=None
    post=[r for r in rows if r['start_s']>=max(command_s, ready_s if ready_s is not None else command_s)]
    for left,right in zip(post,post[1:]):
        if pair_passes(left,right):
            recovery=right['end_s']-command_s;break
    return dict(windows=rows,already_healthy_before_scale=already_healthy,
                recovery_seconds=None if already_healthy else recovery,
                post_scale_slo_observed=recovery is not None)


def write_report(directory,report):
    save(directory/'report.json',report)
    rows=[]
    for impl,r in report['implementations'].items():
        vals=[impl,r.get('original_capacity'),r.get('target_capacity'),r.get('capacity_ready_seconds'),r.get('already_healthy_before_scale'),r.get('recovery_seconds'),r.get('post_scale_slo_observed')]
        rows.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in vals)+'</tr>')
    page='''<!doctype html><meta charset="utf-8"><title>Order-tier scale-out</title><style>body{font:18px system-ui;background:#101827;color:#eef;margin:40px auto;max-width:1200px}td,th{padding:12px;border-bottom:1px solid #456}table{border-collapse:collapse}</style><h1>Order-tier scaling under a burst</h1>'''
    if report['settings'].get('wasm_mode')=='host':
        page+='<p>Java adds an order-service pod. Wasm starts an additional runtime host pod, its providers and serving components, then verifies an order directly against that pod before adding it to HTTP routing. This scales different scopes of the application. Images may already be cached: this is a new process/pod test, not a cold image-download test. Capacity is Java Ready pods with a successful direct order / verified routable Wasm hosts.</p>'
    else:page+='<p>Java adds order-service pods; Wasm increases orders-component concurrency inside an existing host. Dependencies, other components and host count stay fixed. These are different scaling mechanisms, not equivalent pod startups or a cold-start comparison.</p>'
    page+='<p>Recovery requires two consecutive complete '+str(report['settings']['window'])+'-second request-start cohorts with p95 &lt; '+str(report['settings']['p95_ms'])+' ms, zero failures and enough completions to match the burst rate. None means no recovery observed, or the application was already healthy; consult the flags and windows in report.json. An observed capacity count is not proof of increased end-to-end throughput.</p>'
    page+='<p>'+html.escape(str(report.get('analysis_note','')) )+'</p>'
    page+='<p>'+html.escape(str({k:report.get(k) for k in ['restored','applications_healthy','error','restoration_error']}))+'</p>'
    page+='<p>Protocol v2 measures both new pods with an independent 2-second monitor: Kubernetes Ready and HTTP 201 plus a valid response to an order sent directly to the new pod, using the same 10-second timeout. These timings start when the scale request begins. Wasm full-host verification/routing remains a separate observation; images may be cached. Probes bypass ingress but exercise downstream application calls.</p>'
    page+='<table><tr><th>Application</th><th>Original capacity</th><th>Target</th><th>Observed capacity ready, seconds</th><th>Already healthy</th><th>Traffic recovery, seconds</th><th>Post-scale SLO observed</th></tr>'+''.join(rows)+'</table>'
    page+='<h2>Common readiness checks (seconds from request start)</h2><table><tr><th>Application</th><th>Pod Ready</th><th>Direct order accepted</th></tr>'
    for impl,result in report['implementations'].items():
        page+='<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in [impl,result.get('new_pod_ready_seconds'),result.get('new_pod_order_accepted_seconds')])+'</tr>'
    page+='</table>'
    for impl,result in report['implementations'].items():
        startup=result.get('host_startup')
        if not startup:continue
        started=startup.get('request_started_at')
        page+='<h2>Additional Wasm host startup</h2><p>Seconds since the request began; polling includes API/CLI delays.</p><table><tr><th>Event</th><th>Seconds</th><th>Evidence</th></tr>'
        for event in startup.get('events',[]):
            if event['name']=='baseline_label_prepared':continue
            fields={k:v for k,v in event.items() if k not in ['name','time','images']}
            seconds=None if started is None else round(event['time']-started,2)
            page+='<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in [event['name'],seconds,fields])+'</tr>'
        page+='</table>'
    (directory/'report.html').write_text(page)


def run_load(args,state,impl,output,port):
    output.mkdir()
    with q.Monitor(state,impl,output,K,save) as monitor:
        return _run_load(args,state,impl,output,port,monitor)


def _run_load(args,state,impl,output,port,monitor):
    original,target=bounds(state,impl)
    control=dict(desired=original,accepted=False,**capacity(state,impl,port,monitor))
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data=json.dumps(control).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def log_message(self,*_):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    env=dict(os.environ,K6_PROMETHEUS_RW_SERVER_URL='http://localhost:8080/prometheus/api/v1/write',K6_PROMETHEUS_RW_TREND_STATS='p(95),avg,max',K6_PROMETHEUS_RW_STALE_MARKERS='true')
    values=dict(WASM_MODE=state.get('wasm_mode','concurrency'),IMPLEMENTATION=impl,RUN_ID='scaleout-'+state.get('run_token',output.parent.name)+'-'+impl,WARMUP=args.warmup,BASELINE=args.baseline,BEFORE_SCALE=args.before_scale,AFTER_SCALE=args.after_scale,RECOVERY=args.recovery,BASELINE_RATE=args.baseline_rate,BURST_RATE=args.burst_rate,VUS=args.vus,WINDOW_SECONDS=args.window,CONTROL_URL='http://127.0.0.1:'+str(server.server_port))
    cmd=['k6','run','--quiet','--log-format','json','-o','experimental-prometheus-rw','--summary-export='+str(output/'summary.json')]
    for key,value in values.items():cmd+=['-e',key+'='+str(value)]
    cmd.append(str(output.parent/'load.js' if (output.parent/'load.js').exists() else ROOT/'load.js'))
    scale_due=args.warmup+args.baseline+1+args.before_scale
    total=scale_due+args.after_scale+1+args.recovery
    start=None;command_at=None;ready_at=None;process=None;observation_errors=[]
    launched=time.time()
    try:
        with (output/'k6.log').open('w') as log,(output/'observations.jsonl').open('w') as observations:
            process=subprocess.Popen(cmd,env=env,stdout=log,stderr=subprocess.STDOUT)
            while process.poll() is None:
                if time.time()-launched>total+60:raise RuntimeError('Bounded scale-out run expired')
                if start is None:
                    for line in (output/'k6.log').read_text().splitlines():
                        try:
                            message=json.loads(json.loads(line).get('msg','{}'))
                            if message.get('event')=='scaleout_started':start=message['start_ms']/1000;break
                        except (ValueError,TypeError):pass
                if start is not None:
                    elapsed=time.time()-start
                    if command_at is None and elapsed>=scale_due:
                        command_begin=time.time();monitor.arm();scale(state,impl,port=port);command_at=time.time()
                        control.update(desired=target,accepted=True)
                        print(impl,'scale accepted at',round(command_at-start,2),'seconds',flush=True)
                    try:
                        control.update(capacity(state,impl,port,monitor))
                        control.update(q.timings(monitor.snapshot()))
                    except (RuntimeError,subprocess.SubprocessError) as error:
                        control['observed']=None;observation_errors.append(str(error))
                    if command_at is not None and ready_at is None and control['observed']==target:
                        ready_at=time.time();print(impl,'target capacity observed after',round(ready_at-command_at,2),'seconds',flush=True)
                    observations.write(json.dumps(dict(time=time.time(),elapsed=elapsed,**control))+'\n');observations.flush()
                time.sleep(2)
            code=process.wait()
        if start is None or command_at is None:raise RuntimeError('Load/scale command was not observed')
        if code!=0:raise RuntimeError('k6 did not complete normally: '+str(code))
        summary=json.loads((output/'summary.json').read_text())
        result=recovery_windows(summary,args,command_at-start,None if ready_at is None else ready_at-start)
        result.update(original_capacity=original,target_capacity=target,
                      scale_accepted_s=command_at-start,scale_command_seconds=command_at-command_begin,
                      capacity_ready_seconds=None if ready_at is None else ready_at-command_at,
                      dropped_iterations=summary['metrics'].get('dropped_iterations',{}).get('count',0))
        readiness=monitor.snapshot()
        result.update(q.timings(readiness))
        result.update(readiness_protocol='new-pod-direct-order-v1',readiness=readiness,scale_request_s=command_begin-start)
        extra=[p for p in readiness['pods'].values() if not p['baseline']]
        expected=state['factor']-1 if impl=='java' else 1
        result['readiness_comparison_valid']=(len(extra)==expected and not readiness['baseline_changed'] and not readiness['errors'] and all('order_accepted_seconds' in p and not p.get('image_mismatch') for p in extra))
        result.update(traffic_window_kind='request_start',target_capacity_observed=ready_at is not None,observation_errors=observation_errors)
        observations=[json.loads(line) for line in (output/'observations.jsonl').read_text().splitlines()]
        later=[o for o in observations if ready_at is not None and o['time']>=ready_at and o['observed'] is not None]
        stable=len(later)>=2 and all(o['observed']==target for o in later)
        result['target_capacity_stable_after_ready']=stable
        if not stable:result['recovery_seconds']=None
        if impl=='wasm' and state.get('wasm_mode')=='host':
            result.update(capacity_unit='verified routable runtime hosts',host_startup=copy.deepcopy(state['wasm']['host']))
        save(output/'result.json',result);return result
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        server.shutdown();server.server_close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['plan','run','restore']);p.add_argument('target',nargs='?',default='both')
    for name,default in [('factor',2),('warmup',60),('baseline',30),('before-scale',45),('after-scale',120),('recovery',30),('baseline-rate',100),('burst-rate',200),('vus',250),('window',15),('nats-port',14223)]:p.add_argument('--'+name,type=int,default=default)
    p.add_argument('--order',choices=['java-first','wasm-first'],default='java-first')
    p.add_argument('--wasm-mode',choices=['host','concurrency'],default='host',help='host creates a new pod; concurrency only raises existing-host allowance')
    p.add_argument('--output',help='New results directory (must not exist)')
    p.add_argument('--p95-ms',type=float,default=500)
    args=p.parse_args()
    if args.action=='restore':state=json.loads(Path(args.target).read_text());targets=state['targets']
    else:
        if args.target not in ['java','wasm','both']:p.error('target must be java, wasm or both')
        if min(args.factor,args.warmup,args.baseline,args.before_scale,args.after_scale,args.recovery,args.baseline_rate,args.burst_rate,args.vus,args.window,args.nats_port)<=0 or args.factor<2 or not math.isfinite(args.p95_ms) or args.p95_ms<=0:p.error('positive settings and factor >= 2 required')
        if args.before_scale<3*args.window or args.after_scale<3*args.window:p.error('before-scale needs at least three windows; after-scale at least three')
        targets=(['java','wasm'] if args.order=='java-first' else ['wasm','java']) if args.target=='both' else [args.target];state=snapshot(targets,args.factor,args.wasm_mode)
    forward=NatsForward(args.nats_port) if 'wasm' in targets else None
    guard=None;directory=None;report=None
    try:
        if forward:forward.__enter__()
        if args.action=='restore':
            for impl in targets:scale(state,impl,restore=True,port=args.nats_port);wait_original(state,impl,args.nats_port)
            return
        for impl in targets:wait_original(state,impl,args.nats_port)
        if 'wasm' in targets:record_runtime(state,args.nats_port)
        if args.action=='plan':
            for impl in targets:scale(state,impl,dry_run=True,port=args.nats_port)
            print(json.dumps(state,indent=2));print('Selected manifest validations passed. No capacity changed.');return
        directory=Path(args.output).resolve() if args.output else ROOT/'.results'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');directory.mkdir(parents=True)
        state['_state_path']=str((directory/'state.json').resolve())
        state['run_token']=directory.parent.name+'-'+directory.name+'-'+uuid.uuid4().hex[:8]
        save(directory/'state.json',state)
        (directory/'load.js').write_text((ROOT/'load.js').read_text())
        report=dict(settings=vars(args),run_token=state['run_token'],implementations={},restored=False)
        guard=subprocess.Popen(['caffeinate','-i','-s']) if sys.platform=='darwin' else None
        print('Dashboard: http://localhost:8080/grafana/d/java-wasm-scaleout',flush=True)
        try:
            for impl in targets:
                if impl=='wasm' and state.get('wasm_mode')=='host':host_mode(state,args.nats_port).prepare()
                report['implementations'][impl]=run_load(args,state,impl,directory/impl,args.nats_port)
                write_report(directory,report)
                scale(state,impl,restore=True,port=args.nats_port);wait_original(state,impl,args.nats_port)
        except BaseException as error:report['error']=str(error);raise
        finally:
            errors=[]
            for impl in targets:
                try:scale(state,impl,restore=True,port=args.nats_port);wait_original(state,impl,args.nats_port)
                except BaseException as error:errors.append(str(error))
            if errors:report['restoration_error']='; '.join(errors)
            else:report.update(restored=True,applications_healthy=True)
            write_report(directory,report)
            print('Report:',directory/'report.html',flush=True)
            print('Recovery: python3 monitoring/scaleout/run.py restore '+str(directory/'state.json'),flush=True)
            if errors:raise RuntimeError(report['restoration_error'])
    finally:
        if guard:guard.terminate();guard.wait()
        if forward:forward.__exit__(None,None,None)


if __name__=='__main__':main()
