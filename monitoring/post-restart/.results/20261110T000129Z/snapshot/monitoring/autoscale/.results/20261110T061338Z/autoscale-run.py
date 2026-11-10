#!/usr/bin/env python3
"""CPU/latency-triggered 1→2→1 benchmark controller; not Kubernetes HPA."""
import argparse
import copy
from datetime import datetime,timezone
import hashlib
import html
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('scaleout',ROOT.parent/'scaleout/run.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)


def save(path,value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w') as file:os.chmod(temporary,0o600);json.dump(value,file,indent=2);file.write('\n')
    temporary.replace(path)

s.save=save


def cpu_query(state,impl):
    if impl=='java':pods='order-service-.*';container='order-service'
    else:
        pods='wasmcloud-host-.*|'+state['wasm']['host']['deployment_name']+'-.*';container='wasmcloud-host'
    metric=f'container_cpu_usage_seconds_total{{job="kubelet-cadvisor",namespace="{impl}",pod=~"{pods}",container="{container}"}}'
    return f'sum(rate({metric}[60s]) and (timestamp({metric}) > time()-35))'


def cpu(state,impl):
    url='http://localhost:8080/prometheus/api/v1/query?'+urllib.parse.urlencode(dict(query=cpu_query(state,impl)))
    with urllib.request.urlopen(url,timeout=5) as response:data=json.load(response)
    rows=data.get('data',{}).get('result',[])
    if data.get('status')!='success' or len(rows)!=1:raise RuntimeError('CPU signal absent or stale')
    value=float(rows[0]['value'][1])
    if not math.isfinite(value) or value<0:raise RuntimeError('Invalid CPU signal')
    return value


class CalibrationError(Exception):pass


def baseline_gate(testid,args):
    """Check the final warmup cohorts; baseline time allows requests/RW flush."""
    last=args.warmup//args.window
    observations=[]
    for window in range(last-2,last):
        row={}
        for name,field in [('completed_total','completed'),('accepted_total','accepted'),('failures_total','failures'),('latency_ms_p95','p95_ms')]:
            query=f'k6_autoscale_{name}{{testid="{testid}",window="{window}"}}'
            url='http://localhost:8080/prometheus/api/v1/query?'+urllib.parse.urlencode(dict(query=query))
            with urllib.request.urlopen(url,timeout=5) as response:result=json.load(response)['data']['result']
            if len(result)!=1:raise RuntimeError('Waiting for baseline cohort metric: '+field)
            row[field]=float(result[0]['value'][1])
        if row['completed']<args.baseline_rate*args.window-1:raise RuntimeError('Waiting for final warmup cohort completions')
        if not (row['failures']==0 and row['accepted']==row['completed'] and row['p95_ms']<args.p95_ms):raise CalibrationError('Warmup did not sustain baseline rate/SLO; CPU calibration invalid')
        row['window']=window;observations.append(row)
    return observations


class Policy:
    def __init__(self,target,hold=60):
        self.target=target;self.hold=hold;self.low_since=None;self.up_at=None;self.completed=False
    def decide(self,cores,current,now,pending=False):
        if pending or self.completed:return None
        if not math.isfinite(cores) or cores<0:raise ValueError('CPU must be finite and nonnegative')
        if current==1 and self.up_at is None and cores>self.target*1.1:
            self.up_at=now;return 'up'
        if current==2 and self.up_at is not None:
            if cores/current<self.target*.7:
                if self.low_since is None:self.low_since=now
                if now-self.low_since>=self.hold and now-self.up_at>=self.hold:self.completed=True;return 'down'
            else:self.low_since=None
        return None


def latency_signal(testid,elapsed,args):
    """Use a settled request-start cohort, not a partial in-flight quantile."""
    window=math.floor((elapsed-args.latency_settle)/args.window)-1
    if window<0:raise RuntimeError('No settled latency cohort yet')
    names={'k6_autoscale_latency_ms_p95':'p95_ms','k6_autoscale_completed_total':'completed','k6_autoscale_accepted_total':'accepted','k6_autoscale_failures_total':'failures'}
    query='{__name__=~"'+('|'.join(names))+'",testid="'+testid+'",window="'+str(window)+'"}'
    url='http://localhost:8080/prometheus/api/v1/query?'+urllib.parse.urlencode(dict(query=query))
    with urllib.request.urlopen(url,timeout=5) as response:data=json.load(response)
    signal=dict(window=window,end_s=(window+1)*args.window,observed_s=elapsed)
    for row in data.get('data',{}).get('result',[]):
        name=row['metric'].get('__name__');stamp,value=row['value'];value=float(value)
        if name not in names or not math.isfinite(value) or value<0 or time.time()-float(stamp)>45:raise RuntimeError('Invalid or stale latency cohort metric')
        if names[name] in signal:raise RuntimeError('Ambiguous latency cohort metric')
        signal[names[name]]=value
    if data.get('status')!='success' or not all(name in signal for name in names.values()):raise RuntimeError('Latency cohort metric absent')
    if signal['completed']<100:raise RuntimeError('Too few completed requests for latency trigger')
    return signal


class LatencyPolicy(Policy):
    """Change scale-out only; retain the CPU-based scale-in policy."""
    def __init__(self,target,hold=60,p95_ms=500):
        super().__init__(target,hold);self.p95_ms=p95_ms;self.high_count=0;self.last_window=None
    def missing_signal(self):
        self.high_count=0;self.last_window=None
    def decide(self,cores,current,now,pending=False,signal=None):
        if pending or self.completed:return None
        if current==1 and self.up_at is None:
            if signal is None:self.missing_signal();return None
            window=signal['window']
            if window==self.last_window:return None
            adjacent=self.last_window is not None and window==self.last_window+1
            self.last_window=window
            if signal['p95_ms']>self.p95_ms:self.high_count=(self.high_count+1) if adjacent else 1
            else:self.high_count=0
            if self.high_count>=2:self.up_at=now;return 'up'
            return None
        if cores is None:self.low_since=None;return None
        return super().decide(cores,current,now,pending)


def mark_extra_serving(data,now):
    """A queued downscale must not overwrite the completed upscale event."""
    if (data['observed']==2 and data['pending'] and data['actions']
            and data['actions'][-1]['action']=='up'
            and 'ready_time' not in data['actions'][-1]):
        data['actions'][-1]['ready_time']=now;data['pending']=False
        return True
    return False


class Actuator:
    def __init__(self,state,impl,port,monitor):
        self.state,self.impl,self.port,self.monitor=state,impl,port,monitor
        self.queue=queue.Queue();self.stop=threading.Event();self.lock=threading.Lock();self.data=dict(observed=1,pending=False,actions=[],errors=[]);self.thread=threading.Thread(target=self.run,daemon=True)
    def snapshot(self):
        with self.lock:return copy.deepcopy(self.data)
    def submit(self,action):
        with self.lock:self.data['pending']=True
        self.queue.put(action)
    def run(self):
        observing=False
        while not self.stop.is_set():
            try:action=self.queue.get(timeout=2)
            except queue.Empty:action=None
            try:
                if action:
                    event=dict(action=action,request_time=time.time())
                    with self.lock:self.data['actions'].append(event)
                    if action=='up':self.monitor.arm()
                    s.scale(self.state,self.impl,restore=action=='down',port=self.port)
                    event['ack_time']=time.time()
                    if action=='down':
                        s.wait_original(self.state,self.impl,self.port)
                        event['ready_time']=time.time()
                        with self.lock:self.data.update(observed=1,pending=False)
                        observing=False
                    else:observing=True
                if observing:
                    current=s.capacity(self.state,self.impl,self.port,self.monitor)
                    with self.lock:
                        self.data['observed']=current['observed']
                        if mark_extra_serving(self.data,time.time()):
                            print(self.impl,'automatic extra capacity serving',flush=True)
            except BaseException as error:
                with self.lock:self.data['errors'].append(dict(time=time.time(),error=str(error)))
                if action:
                    with self.lock:self.data['fatal']=True
                self.stop.wait(3)
    def close(self):
        self.stop.set();self.thread.join(timeout=240)
        if self.thread.is_alive():raise RuntimeError('Scaling actuator did not stop; do not race recovery')


def cohorts(summary,args):
    metrics=summary['metrics'];rows=[];burst_start=args.warmup+args.baseline+1;burst_end=burst_start+args.burst
    for i in range(math.ceil((burst_end+1+args.recovery)/args.window)):
        start,end=i*args.window,(i+1)*args.window
        phase='baseline' if start>=args.warmup and end<=burst_start-1 else 'burst' if start>=burst_start and end<=burst_end else 'recovery' if start>=burst_end+1 else 'transition/warmup'
        def get(name,key,default=None):return metrics.get(f'autoscale_{name}{{window:{i}}}',{}).get(key,default)
        count=get('completed','count',0);accepted=get('accepted','count',0);failed=get('failures','count',0);p95=get('latency_ms','p(95)');rate=args.burst_rate if phase=='burst' else args.baseline_rate
        rows.append(dict(start_s=start,end_s=end,phase=phase,accepted=accepted,completed=count,failures=failed,median_ms=get('latency_ms','med'),p95_ms=p95,passed=p95 is not None and p95<args.p95_ms and failed==0 and accepted==count and count>=rate*args.window-1))
    return rows


def recovery_after(rows,ready_s,phase,rate,window,p95):
    rows=[r for r in rows if r['phase']==phase and r['start_s']>=ready_s]
    for a,b in zip(rows,rows[1:]):
        if a['end_s']!=b['start_s']:continue
        if all(r['p95_ms'] is not None and r['p95_ms']<p95 and r['failures']==0 and r['accepted']==r['completed'] for r in [a,b]) and a['completed']+b['completed']>=2*rate*window-1:return b['end_s']
    return None


def run_trial(state,impl,output,args,port):
    output.mkdir();control=dict(desired_capacity=1,observed_capacity=1,cpu_cores=None,target_cpu_cores=None,signal_valid=0,controller_state=0,latency_p95_ms=None,target_latency_ms=args.p95_ms,latency_signal_valid=0,trigger_latency=int(args.trigger=='latency'))
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body=json.dumps(control).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*_):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    values=dict(IMPLEMENTATION=impl,TRIGGER=args.trigger,RUN_ID=state['run_token']+'-'+impl,WARMUP=args.warmup,BASELINE=args.baseline,BURST=args.burst,RECOVERY=args.recovery,BASELINE_RATE=args.baseline_rate,BURST_RATE=args.burst_rate,VUS=args.vus,WINDOW_SECONDS=args.window,CONTROL_URL='http://127.0.0.1:'+str(server.server_port))
    env=dict(os.environ,K6_PROMETHEUS_RW_SERVER_URL='http://localhost:8080/prometheus/api/v1/write',K6_PROMETHEUS_RW_TREND_STATS='p(95),avg,max',K6_PROMETHEUS_RW_STALE_MARKERS='true')
    cmd=['k6','run','--quiet','--log-format','json','-o','experimental-prometheus-rw','--summary-export='+str(output/'summary.json')]
    for key,value in values.items():cmd+=['-e',key+'='+str(value)]
    cmd.append(str(output.parent/'load.js'));process=None;start=None;policy=None;samples=[];latency_samples=[];signal_errors=[];decision_at=0;next_sample=0;actuator=None;last_observation=None;calibration=None
    try:
        with s.q.Monitor(state,impl,output,s.K,save) as monitor:
            actuator=Actuator(state,impl,port,monitor);actuator.thread.start();launched=time.monotonic()
            with (output/'k6.log').open('w') as log,(output/'observations.jsonl').open('w') as observations:
                process=subprocess.Popen(cmd,env=env,stdout=log,stderr=subprocess.STDOUT)
                while process.poll() is None:
                    if time.monotonic()-launched>args.warmup+args.baseline+args.burst+args.recovery+75:raise RuntimeError('Bounded load run expired')
                    if start is None:
                        for line in (output/'k6.log').read_text().splitlines():
                            try:
                                message=json.loads(json.loads(line).get('msg','{}'))
                                if message.get('event')=='autoscale_started':start=message['start_ms']/1000;break
                            except (ValueError,TypeError):pass
                    if start is None:time.sleep(1);continue
                    elapsed=time.time()-start;current=actuator.snapshot()
                    if current.get('fatal'):raise RuntimeError('Scaling actuator failed; inspect observations')
                    if policy is None and elapsed>args.warmup+args.baseline:raise CalibrationError('CPU calibration did not complete before the burst')
                    control['observed_capacity']=current['observed']
                    if elapsed>=next_sample:
                        next_sample=elapsed+5
                        cores=None
                        try:
                            cores=cpu(state,impl);control.update(cpu_cores=cores,signal_valid=1);samples.append(dict(elapsed=elapsed,cores=cores))
                            if policy is None and elapsed>=args.warmup:
                                baseline=[x['cores'] for x in samples if args.warmup-30<=x['elapsed']<=args.warmup]
                                if len(baseline)<3:raise RuntimeError('Insufficient fresh CPU calibration samples')
                                calibration=baseline_gate(values['RUN_ID'],args)
                                target=max(.05,statistics.median(baseline)*args.cpu_headroom)
                                policy=Policy(target,args.down_hold) if args.trigger=='cpu' else LatencyPolicy(target,args.down_hold,args.p95_ms)
                                control['target_cpu_cores']=target
                                print(impl,'CPU target',round(target,4),'cores per unit; baseline',round(statistics.median(baseline),4),flush=True)
                        except (OSError,ValueError,RuntimeError) as error:
                            control['signal_valid']=0;signal_errors.append(dict(elapsed=elapsed,error=str(error),signal='cpu/calibration'))
                            if policy:policy.low_since=None
                        if policy and elapsed>=decision_at:
                            decision_at=elapsed+15
                            try:
                                if args.trigger=='latency':
                                    signal=latency_signal(values['RUN_ID'],elapsed,args);latency_samples.append(signal)
                                    control.update(latency_p95_ms=signal['p95_ms'],latency_signal_valid=1)
                                    action=policy.decide(cores,current['observed'],elapsed,current['pending'],signal)
                                else:action=None if cores is None else policy.decide(cores,current['observed'],elapsed,current['pending'])
                                if action:
                                    control.update(desired_capacity=2 if action=='up' else 1,controller_state=1 if action=='up' else 2)
                                    actuator.submit(action);print(impl,('latency' if args.trigger=='latency' and action=='up' else 'CPU')+'-triggered',action,'at',round(elapsed,1),'seconds',flush=True)
                            except (OSError,ValueError,RuntimeError) as error:
                                control['latency_signal_valid']=0;signal_errors.append(dict(elapsed=elapsed,error=str(error),signal='decision'))
                                if args.trigger=='latency':
                                    policy.missing_signal()
                                    # Scale-in still depends only on a valid CPU signal.
                                    if current['observed']==2:
                                        action=policy.decide(cores,2,elapsed,current['pending'])
                                        if action:
                                            control.update(desired_capacity=1,controller_state=2);actuator.submit(action)
                                            print(impl,'CPU-triggered down at',round(elapsed,1),'seconds',flush=True)
                    last_observation=dict(time=time.time(),elapsed=elapsed,control=copy.deepcopy(control),actuator=current,signal_error_count=len(signal_errors),last_signal_error=signal_errors[-1] if signal_errors else None)
                    observations.write(json.dumps(last_observation)+'\n');observations.flush();save(output/'progress.json',last_observation);time.sleep(1)
                if process.wait()!=0:raise RuntimeError('k6 did not complete normally')
            actuator.close();readiness=monitor.snapshot();events=actuator.snapshot();summary=json.loads((output/'summary.json').read_text());rows=cohorts(summary,args)
            up=next((e for e in events['actions'] if e['action']=='up'),{});down=next((e for e in events['actions'] if e['action']=='down'),{})
            request_s=None if not up else up['request_time']-start;ready_s=None if not up.get('ready_time') else up['ready_time']-start
            recovery=None if ready_s is None else recovery_after(rows,ready_s,'burst',args.burst_rate,args.window,args.p95_ms)
            result=dict(implementation=impl,start_time=start,cpu_query=cpu_query(state,impl),cpu_target_cores=None if policy is None else policy.target,baseline_gate=calibration,cpu_samples=samples,signal_errors=signal_errors,actions=events['actions'],actuator_errors=events['errors'],readiness=readiness,cohorts=rows,up_requested_s=request_s,up_serving_s=ready_s,up_startup_seconds=None if ready_s is None else up['ready_time']-up['request_time'],burst_recovery_s=recovery,down_requested_s=None if not down else down['request_time']-start,down_complete_s=None if not down.get('ready_time') else down['ready_time']-start,automatic_cycle_completed=bool(up.get('ready_time') and down.get('ready_time')),dropped_iterations=summary['metrics'].get('dropped_iterations',{}).get('count',0),accepted=summary['metrics']['autoscale_accepted']['count'],failures=summary['metrics']['autoscale_failures']['count'])
            result.update(s.q.timings(readiness));result['readiness_valid']=not readiness['errors'] and not readiness['baseline_changed'] and all('order_accepted_seconds' in pod and not pod.get('image_mismatch') for pod in readiness['pods'].values() if not pod['baseline']) and bool(up.get('ready_time'))
            result.update(trigger=args.trigger,latency_samples=latency_samples)
            save(output/'result.json',result);return result
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        if actuator:actuator.close()
        server.shutdown();server.server_close()


def report(directory,data):
    save(directory/'report.json',data);esc=lambda x:html.escape(str(x));fmt=lambda x:'—' if x is None else f'{x:,.2f}'
    kind='latency' if data['settings'].get('trigger','cpu')=='latency' else 'CPU'
    page='<!doctype html><meta charset="utf-8"><title>Automatic '+kind+' scaling</title>'
    if data['status']=='running':page+='<meta http-equiv="refresh" content="5">'
    page+='<style>body{font:17px system-ui;background:#101827;color:#eef;max-width:1350px;margin:35px auto;padding:20px}td,th{padding:10px;border-bottom:1px solid #456;text-align:left}table{border-collapse:collapse}a{color:#8bc9ff}svg{width:100%}</style><h1>Automatic '+kind+' scaling: Java / Wasm</h1><p>'+esc(data['status'])+' '+esc(data.get('current',''))+'</p>'
    page+='<p>Shared benchmark controller, not Kubernetes HPA. CPU scale-in uses fresh 60-second CPU rates evaluated every 15 seconds. Target: '+str(data['settings'].get('cpu_headroom',2))+' times each implementation’s median CPU over the last 30 seconds of warmup, minimum 0.05 core per capacity unit. CPU mode scales up at &gt;110% target; both modes scale down at &lt;70% average target sustained for '+str(data['settings']['down_hold'])+' seconds. Missing signals reset the low-CPU hold. One automatic 1→2→1 cycle per trial, capped at two units. No load-phase timer issues scale commands.</p><p>Java adds one order-service pod; Wasm adds a whole runtime pod with providers/components, gated by a successful direct order before ingress admission. The CPU scopes and calibrated targets differ. Shared dependencies remain warm, images may be cached, resources and patched timeouts remain unchanged. This compares the configured policy/actuators; earlier trigger times do not prove a runtime starts faster.</p>'
    if kind=='latency':page+='<p>Latency scale-out: two consecutive 15-second request-start cohorts with p95 above '+str(data['settings']['p95_ms'])+' ms, at least 100 completed requests each, settled for '+str(data['settings']['latency_settle'])+' seconds before evaluation. Missing, stale or nonadjacent cohorts reset the high-latency sequence. CPU alone cannot trigger scale-out in this mode. Cohort settling and export add deliberate decision delay. CPU scale-in is unchanged.</p>'
    page+='<table><tr><th>Pair</th><th>Application</th><th>CPU target/core</th><th>Up requested, s</th><th>New pod order after request, s</th><th>Serving at, s</th><th>Burst SLO restored at, s</th><th>Down complete at, s</th><th>Auto cycle</th><th>Readiness valid</th><th>Dropped / failed</th></tr>'
    for row in data['trials']:
        values=[row['pair'],row['implementation'],fmt(row['cpu_target_cores']),fmt(row['up_requested_s']),fmt(row.get('new_pod_order_accepted_seconds')),fmt(row['up_serving_s']),fmt(row['burst_recovery_s']),fmt(row['down_complete_s']),row['automatic_cycle_completed'],row['readiness_valid'],str(row['dropped_iterations'])+' / '+str(row['failures'])]
        page+='<tr>'+''.join('<td>'+esc(value)+'</td>' for value in values)+'</tr>'
    page+='</table><p>All “at” times are relative to load start. Recovery requires two consecutive complete 15-second request-start cohorts with p95 below '+str(data['settings']['p95_ms'])+' ms, zero failed orders and sufficient accepted throughput. Dash means not observed, not zero. Dropped iterations are unsent work. Readiness observer errors and all failed runs remain in raw files.</p>'
    for row in data['trials']:
        page+='<h2>Pair '+str(row['pair'])+' '+row['implementation']+' — cohort latency</h2><table><tr><th>Seconds</th><th>Phase</th><th>Accepted</th><th>p95 ms</th><th>Failed</th><th>SLO passed</th></tr>'
        for cohort in row['cohorts']:
            if cohort['phase']=='transition/warmup':continue
            page+='<tr>'+''.join('<td>'+esc(value)+'</td>' for value in [str(cohort['start_s'])+'–'+str(cohort['end_s']),cohort['phase'],cohort['accepted'],fmt(cohort['p95_ms']),cohort['failures'],cohort['passed']])+'</tr>'
        page+='</table>'
    for note in data.get('post_test_notes',[]):page+='<p>'+esc(note)+'</p>'
    page+='<p>Restored: '+esc(data.get('restored',False))+'. '+esc(data.get('error',''))+' '+esc(data.get('restoration_error',''))+'</p>'
    tmp=directory/'report.html.tmp';tmp.write_text(page);tmp.replace(directory/'report.html')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['plan','run','restore']);parser.add_argument('--state',type=Path);parser.add_argument('--pairs',type=int,default=2);parser.add_argument('--target',choices=['java','wasm','both'],default='both')
    for name,default in [('warmup',180),('baseline',15),('burst',300),('recovery',300),('baseline-rate',100),('burst-rate',400),('vus',250),('window',15),('down-hold',60)]:parser.add_argument('--'+name,type=int,default=default)
    parser.add_argument('--p95-ms',type=float,default=500);parser.add_argument('--cpu-headroom',type=float,default=1.25);parser.add_argument('--trigger',choices=['cpu','latency'],default='cpu');parser.add_argument('--latency-settle',type=int,default=20);args=parser.parse_args()
    if not math.isfinite(args.cpu_headroom) or args.cpu_headroom<=0:parser.error('--cpu-headroom must be finite and positive')
    if args.latency_settle<15:parser.error('--latency-settle must allow at least 15 seconds for the 10-second HTTP limit and metric export')
    if args.action=='restore':
        if not args.state:parser.error('--state required')
        state=json.loads(args.state.read_text())
        with s.NatsForward(14223):
            for impl in state['targets']:s.scale(state,impl,restore=True);s.wait_original(state,impl,14223)
        return
    if min(args.pairs,args.warmup,args.baseline,args.burst,args.recovery,args.baseline_rate,args.burst_rate,args.vus,args.window,args.down_hold)<=0 or args.warmup<45:parser.error('Positive settings and at least 45 seconds warmup required')
    targets=['java','wasm'] if args.target=='both' else [args.target]
    if args.action=='plan':
        state=s.snapshot(targets,2,'host')
        with s.NatsForward(14223):
            if 'wasm' in targets:s.record_runtime(state,14223)
            for impl in targets:s.wait_original(state,impl,14223);cpu(state,impl);s.scale(state,impl,dry_run=True)
        print('CPU signals, original capacity and server validations passed; no capacity changed.');return
    if sys.platform=='darwin' and "'AC Power'" not in subprocess.check_output(['pmset','-g','batt'],text=True,timeout=10):raise RuntimeError('Connect mains first')
    directory=ROOT/'.results'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');directory.mkdir(parents=True);data=dict(status='running',settings=vars(args)|{'state':None},trials=[],restored=False);report(directory,data)
    for file in [ROOT/'run.py',ROOT/'load.js',ROOT.parent/'scaleout/run.py',ROOT.parent/'scaleout/host_mode.py',ROOT.parent/'scaleout/readiness.py']:
        content=file.read_bytes();(directory/(file.parent.name+'-'+file.name)).write_bytes(content);data.setdefault('source_sha256',{})[str(file)]=hashlib.sha256(content).hexdigest()
    (directory/'load.js').write_bytes((ROOT/'load.js').read_bytes())
    guard=subprocess.Popen(['caffeinate','-i','-s']) if sys.platform=='darwin' else None;state=None
    try:
        with s.NatsForward(14223):
            for pair in range(1,args.pairs+1):
                for impl in (['java','wasm'] if pair%2 else ['wasm','java']):
                    if impl not in targets:continue
                    if sys.platform=='darwin' and "'AC Power'" not in subprocess.check_output(['pmset','-g','batt'],text=True,timeout=10):raise RuntimeError('Mains disconnected')
                    state=s.snapshot([impl],2,'host');state['_state_path']=str(directory/'state.json');state['run_token']='autoscale-'+args.trigger+'-'+directory.name+'-'+str(pair)+'-'+impl
                    if impl=='wasm':s.record_runtime(state,14223)
                    save(directory/'state.json',state);s.wait_original(state,impl,14223)
                    data['current']=f'Pair {pair}: {impl}';report(directory,data);print(data['current'],flush=True)
                    if impl=='wasm':s.host_mode(state,14223).prepare()
                    result=run_trial(state,impl,directory/f'pair-{pair:02d}-{impl}',args,14223);result['pair']=pair;data['trials'].append(result);report(directory,data)
                    s.scale(state,impl,restore=True);s.wait_original(state,impl,14223);save(directory/f'pair-{pair:02d}-{impl}'/'state.json',state);state=None
            data.update(status='complete',restored=True)
    except BaseException as error:data.update(status='failed',error=str(error));raise
    finally:
        if state:
            try:
                with s.NatsForward(14223):
                    for impl in state['targets']:s.scale(state,impl,restore=True);s.wait_original(state,impl,14223)
                data['restored']=True
            except BaseException as error:data['restoration_error']=str(error)
        report(directory,data)
        if guard:guard.terminate();guard.wait()
        print('Report:',directory/'report.html',flush=True)
        if not data['restored']:raise RuntimeError('Restoration incomplete; use saved state.json')

if __name__=='__main__':main()
