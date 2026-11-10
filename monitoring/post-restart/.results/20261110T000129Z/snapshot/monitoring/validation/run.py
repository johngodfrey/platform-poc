#!/usr/bin/env python3
"""Retain history, freeze actual inputs, and validate three local comparisons."""
import copy
from datetime import datetime, timezone
import hashlib
import html
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import catalog

ROOT = Path(__file__).resolve().parent
REPO = Path('/Users/johngodfrey/Repos/platform-poc')
PROM = 'http://localhost:8080/prometheus'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


z = module('validation_scalezero', ROOT.parent/'scalezero/run.py')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',',':')).encode()).hexdigest()


def command(args, timeout=30):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=timeout)


def physical():
    if sys.platform != 'darwin': return {}
    power = command(['pmset','-g','batt'], 5)
    lid = command(['ioreg','-r','-k','AppleClamshellState','-d','4'], 5)
    if "'AC Power'" not in power or re.search(r'"AppleClamshellState"\s*=\s*Yes', lid):
        raise RuntimeError('Environmental interruption: mains disconnected or lid closed')
    return dict(power=power, lid_open=True)


def sources():
    paths = command(['rg','--files','--hidden','-g','!.git','-g','!.results','-g','!target','-g','!node_modules','-g','!__pycache__'], 30).splitlines()
    result = {}
    for name in paths:
        p = Path(name)
        if p.suffix not in ('.py','.js','.json','.toml','.lock','.fish','.yaml','.yml','.rs','.wit','.patch','.md') and not p.name.startswith('Dockerfile'): continue
        if any(part in ('.generated','.results','.git','target','node_modules','__pycache__') for part in p.parts) or '.env' in p.name or 'secret' in p.name.lower(): continue
        if p.is_file(): result[name] = catalog.digest(p)
    return result


def controls():
    """Only hashes escape memory; Secret values and provider configs are not printed."""
    values = {}
    for ns in ('java','wasm'):
        for kind in ('secrets','configmaps'):
            obj = json.loads(command(z.K+['-n',ns,'get',kind,'-o','json']))
            values[ns+'/'+kind] = fingerprint(sorted((dict(name=x['metadata']['name'],data=x.get('data'),binaryData=x.get('binaryData'),type=x.get('type')) for x in obj['items']), key=lambda x:x['name']))
    app = z.s.read('application','wasm','platform-poc')
    values['application_spec'] = fingerprint(app['spec'])
    for ns,kind in (('wasm','deployments'),('wasm','statefulsets'),('monitoring','deployments'),('monitoring','configmaps')):
        obj=json.loads(command(z.K+['-n',ns,'get',kind,'-o','json']))
        # Resource sweeps and startup deliberately change the application host.
        items=[x for x in obj['items'] if x['metadata']['name']!='wasmcloud-host']
        values[ns+'/'+kind]=fingerprint(sorted((dict(name=x['metadata']['name'],spec=x.get('spec'),data=x.get('data')) for x in items),key=lambda x:x['name']))
    for ns, kind in (('java','services'),('java','ingresses'),('wasm','services'),('wasm','ingresses')):
        obj = json.loads(command(z.K+['-n',ns,'get',kind,'-o','json']))
        values[ns+'/'+kind] = fingerprint(sorted((dict(name=x['metadata']['name'],spec=x['spec']) for x in obj['items']),key=lambda x:x['name']))
    return values


def summarize(values):
    return dict(n=len(values),median=statistics.median(values),minimum=min(values),maximum=max(values)) if values else dict(n=0)


def telemetry_stats(series, expected, start, end, step=5, coverage=.8):
    """Only aggregate timestamps with every expected container, no NaN or duplicates."""
    buckets = {}
    for row in series:
        key = (row['metric'].get('pod'),row['metric'].get('container'))
        if key not in expected: continue
        for timestamp, value in row['values']:
            if not start <= float(timestamp) <= end or not math.isfinite(float(value)): continue
            bucket = buckets.setdefault(float(timestamp), {})
            if key in bucket: raise RuntimeError('Duplicate telemetry for application container')
            bucket[key] = float(value)
    values = [sum(row.values()) for row in buckets.values() if set(row)==set(expected)]
    required = math.ceil((math.floor((end-start)/step)+1)*coverage)
    return dict(complete=len(values)>=required,required=required,**summarize(values))


class Campaign:
    def __init__(self):
        os.umask(0o077)
        self.directory = ROOT/'.results'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.directory.mkdir(parents=True)
        self.protocol = json.loads((ROOT/'protocol.json').read_text())
        self.data = dict(status='preparing',directory=str(self.directory),protocol=self.protocol,attempts=[],started=time.time())
        self.baseline = None
        self.write()

    def save(self, name, value): z.save(self.directory/name,value)

    def write(self):
        self.save('campaign.json',self.data)
        esc=lambda x:html.escape(str(x))
        rows=''
        for a in self.data['attempts']:
            link=os.path.relpath(a['directory'],self.directory)
            rows+='<tr><td>'+esc(a['suite'])+'</td><td>'+esc(a.get('pair',''))+'</td><td>'+esc(a.get('implementation','both'))+'</td><td>'+esc(a.get('status','running'))+'</td><td><a href="'+esc(link)+'/">raw files</a></td><td>'+esc(a.get('error',''))+'</td></tr>'
        page='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="20"><title>Frozen validation</title><style>body{font:17px system-ui;background:#101827;color:#eef;max-width:1200px;margin:35px auto;padding:15px}td,th{padding:10px;border-bottom:1px solid #456}a{color:#8bd}</style><h1>Frozen validation campaign</h1><p>'+esc(self.data['status'])+' · '+esc(self.data.get('current',''))+'</p><p><a href="history.html">Retained history</a> · <a href="freeze.json">Frozen setup</a> · <a href="protocol.json">Protocol and exclusion rules</a></p><table><tr><th>Suite</th><th>Pair/sweep</th><th>Implementation</th><th>Outcome</th><th>Evidence</th><th>Reason</th></tr>'+rows+'</table><h2>Aggregate results</h2><pre>'+esc(json.dumps(self.aggregate(),indent=2))+'</pre><p>All attempts are retained. Resource/latency failures are findings, not silently replaced. Sample sizes are independent runs; density slots are projections. Cold/cache refers to container images with warm dependencies.</p><p>'+esc(self.data.get('error',''))+'</p>'
        (self.directory/'report.html').write_text(page)

    def aggregate(self):
        result={}
        for impl in ('java','wasm'):
            measured=[a for a in self.data['attempts'] if a['suite']=='footprint' and a.get('implementation')==impl and a.get('measured')]
            good=[a for a in measured if a.get('status')=='pass']
            result[impl]=dict(footprint=dict(measured=len(measured),passing=len(good),scheduled=5))
            for phase in ('idle','load'):
                for metric in ('cpu_cores','memory_bytes'):
                    vals=[a['telemetry'][phase][metric]['median'] for a in good]
                    result[impl]['footprint'][phase+'_'+metric]=summarize(vals)
            result[impl]['density']=[]
            for a in self.data['attempts']:
                if a['suite']!='density' or a.get('implementation')!=impl:continue
                for rp in a.get('reports',[]):
                    p=Path(rp)/'report.json'
                    if not p.exists():continue
                    stages=json.loads(p.read_text()).get('implementations',{}).get(impl,[])
                    passing=[s['memory_mib'] for s in stages if s['status']=='pass']
                    result[impl]['density'].append(dict(sweep=a['pair'],lowest_passing_mib=min(passing) if passing else None,stages=[{k:s.get(k) for k in ('memory_mib','status','reason')} for s in stages]))
            for a in self.data['attempts']:
                if a['suite']!='startup_cache':continue
                for rp in a.get('reports',[]):
                    p=Path(rp)/'report.json'
                    if not p.exists():continue
                    trials=json.loads(p.read_text()).get('trials',[])
                    pairs={r['pair'] for r in trials if r['implementation']==impl}
                    good_pairs=[pair for pair in pairs if len([r for r in trials if r['pair']==pair and r['implementation']==impl])==2 and all(r.get('valid') for r in trials if r['pair']==pair and r['implementation']==impl)]
                    result[impl]['startup_cache']=dict(scheduled_pairs=5,valid_pairs=len(good_pairs),excluded_or_missing_pairs=5-len(good_pairs))
                    for mode in ('cold','cached'):
                        result[impl]['startup_cache'][mode]=summarize([r['first_order_seconds'] for r in trials if r['implementation']==impl and r['pair'] in good_pairs and r['cache_mode']==mode])
        return result

    def freeze(self):
        history=catalog.inventory(ROOT.parent)
        self.save('history.json',history);(self.directory/'history.html').write_text(catalog.page(history))
        self.save('protocol.json',self.protocol)
        physical()
        self.baseline=z.snapshot();z.runtime_snapshot(self.baseline)
        self.save('private-baseline.json',self.baseline)
        self.hashes=sources()
        for name in self.hashes:
            target=self.directory/'sources'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((REPO/name).read_bytes())
        self.frozen_controls=controls()
        freeze=dict(created=time.time(),source_sha256=self.hashes,controls=self.frozen_controls,
                    git_head=command(['git','rev-parse','HEAD']).strip(),git_status=command(['git','status','--short']),
                    records=[{k:r[k] for k in ('namespace','name','uid','template_hash','images')} for r in self.baseline['records']],
                    hostconfig_hash=fingerprint(self.baseline['hostconfig']),component_counts=self.baseline['component_counts'],providers=self.baseline['providers'],power=physical())
        app=z.s.read('application','wasm','platform-poc')
        freeze['application_images']=[dict(name=c['name'],type=c['type'],image=c['properties'].get('image')) for c in app['spec']['components']]
        freeze['nodes']=json.loads(command(z.K+['get','nodes','-o','json']))
        freeze['versions']={name:command(args).strip() for name,args in dict(k6=['k6','version'],docker=['docker','version','--format','{{.Server.Version}}'],k3d=['k3d','version'],rust=['rustc','--version'],wash=['wash','--version']).items()}
        self.save('freeze.json',freeze);self.data.update(status='running',freeze_sha256=catalog.digest(self.directory/'freeze.json'));self.write()
        print('Frozen campaign:',self.directory/'report.html',flush=True)

    def guard(self, resources=False, live=True):
        physical()
        if sources()!=self.hashes:raise RuntimeError('Frozen source changed; stop and start a new campaign')
        if not live:return
        if controls()!=self.frozen_controls:raise RuntimeError('Frozen config, secret, routing or Application changed')
        for r in self.baseline['records']:
            dep=z.s.read('deployment',r['namespace'],r['name'])
            template=copy.deepcopy(dep['spec']['template']);expected=copy.deepcopy(r['template'])
            if resources:
                for t in (template,expected):
                    for c in t['spec']['containers']:
                        if c['name']==r['name']:c.pop('resources',None)
            if dep['metadata']['uid']!=r['uid'] or template!=expected:raise RuntimeError('Frozen Deployment changed: '+r['name'])
            if resources:
                for pod in z.s.b.pods_for(r['namespace'],dep):
                    for c in pod.get('status',{}).get('containerStatuses',[]):
                        if c.get('ready') and c.get('imageID')!=r['images'].get(c['name']):raise RuntimeError('Frozen imageID changed during density rollout')
        if not resources:
            host=z.s.read('wasmcloudhostconfig','wasm','wasmcloud-host')
            if host['spec']!=self.baseline['hostconfig']['spec']:raise RuntimeError('Frozen HostConfig changed')

    def stability(self, impl):
        rows=[]
        for r in self.baseline['records']:
            if r['namespace']!=impl:continue
            dep=z.s.read('deployment',impl,r['name']);pods=z.s.b.pods_for(impl,dep)
            if len(pods)!=1 or not z.s.q.pod_ready(pods[0]):raise RuntimeError('Expected one ready pod: '+r['name'])
            statuses=pods[0]['status']['containerStatuses']
            if {c['name']:c['imageID'] for c in statuses}!=r['images']:raise RuntimeError('Frozen imageID changed: '+r['name'])
            rows.append((pods[0]['metadata']['uid'],sum(c['restartCount'] for c in statuses)))
        return rows

    def supervise(self, cmd, log, deadline, tick=None):
        with log.open('w') as stream:
            p=subprocess.Popen(cmd,stdout=stream,stderr=subprocess.STDOUT,env=dict(os.environ,REQUEST_TIMEOUT='10s'))
            try:
                while p.poll() is None:
                    if time.monotonic()>deadline:raise RuntimeError('Bounded execution expired')
                    physical()
                    if tick:tick()
                    time.sleep(10)
                return p.returncode
            finally:
                if p.poll() is None:
                    p.send_signal(signal.SIGINT)
                    try:p.wait(timeout=45)
                    except subprocess.TimeoutExpired:p.terminate();p.wait(timeout=10)

    def load(self, impl, output, idle, duration, recovery, p95):
        output.mkdir(parents=True)
        stable=self.stability(impl)
        envs=dict(IMPLEMENTATION=impl,RUN_ID='validation-'+self.directory.name+'-'+output.name,IDLE_SECONDS=idle,LOAD_SECONDS=duration,RECOVERY_SECONDS=recovery,ORDER_RATE=100,ORDER_VUS=250,P95_MS=p95,REQUEST_TIMEOUT='10s')
        cmd=['k6','run','--quiet','--summary-export='+str(output/'summary.json')]
        for k,v in envs.items():cmd+=['-e',str(k)+'='+str(v)]
        cmd+=[str(ROOT.parent/'footprint/footprint.js')]
        self.save(str(output.relative_to(self.directory)/'command.json'),cmd)
        def tick():
            self.guard(live=False)
            if self.stability(impl)!=stable:raise RuntimeError('Pod replaced/restarted during footprint')
        code=self.supervise(cmd,output/'k6.log',time.monotonic()+idle+duration+recovery+75,tick)
        summary=json.loads((output/'summary.json').read_text())
        result=z.s.b.result_from(summary,code,100,duration,p95)
        matches=re.findall(r'\{[^\n]*"event":"demo_started"[^\n]*\}',(output/'k6.log').read_text().replace('\\"','"'))
        # k6 console text may append source=console; decode only the JSON object.
        if not matches:raise RuntimeError('Missing authoritative k6 start marker')
        result['start']=json.loads(matches[0])['start_ms']/1000
        z.save(output/'result.json',result)
        return result

    def query(self, expr, start, end):
        url=PROM+'/api/v1/query_range?'+urllib.parse.urlencode(dict(query=expr,start=start,end=end,step=5))
        with urllib.request.urlopen(url,timeout=20) as response:data=json.load(response)
        if data['status']!='success':raise RuntimeError('Prometheus query failed')
        return data['data']['result']

    def telemetry(self, impl, start, output):
        pods=[];expected=[]
        for r in self.baseline['records']:
            if r['namespace']!=impl:continue
            dep=z.s.read('deployment',impl,r['name']);pod=z.s.b.pods_for(impl,dep)[0]['metadata']['name'];pods.append(pod);expected.append((pod,r['name']))
        sel='job="kubelet-cadvisor",namespace="'+impl+'",pod=~"'+'|'.join(pods)+'",container=~"'+'|'.join(c for _,c in expected)+'"'
        cpu='container_cpu_usage_seconds_total{'+sel+'}'
        mem='container_memory_working_set_bytes{'+sel+'}'
        queries=dict(cpu_cores='rate('+cpu+'[60s]) and (timestamp('+cpu+') > time()-35)',memory_bytes=mem+' and (timestamp('+mem+') > time()-35)')
        raw={k:self.query(v,start,start+300) for k,v in queries.items()}
        raw['namespace_context']={k:self.query(k+'{implementation="'+impl+'"}',start,start+300) for k in ('poc_namespace_cpu_cores','poc_namespace_memory_bytes')}
        z.save(output/'telemetry-raw.json',dict(queries=queries,expected_containers=expected,series=raw))
        result={}
        for phase,lo,hi in (('idle',60,90),('load',150,210)):
            result[phase]={k:telemetry_stats(raw[k],expected,start+lo,start+hi) for k in queries}
        return result

    def footprint(self):
        failed_gates=0
        for pair in range(1,6):
            for impl in (('java','wasm') if pair%2 else ('wasm','java')):
                self.guard()
                out=self.directory/f'footprint-{pair:02d}-{impl}'
                a=dict(suite='footprint',pair=pair,implementation=impl,directory=str(out),status='running')
                self.data['attempts'].append(a);self.data['current']=f'Footprint pair {pair}/5: {impl} warmup';self.write()
                print(self.data['current'],flush=True)
                try:
                    warm=self.load(impl,out/'warmup',1,180,5,1000000)
                    gate=self.load(impl,out/'gate',1,30,5,500)
                    a.update(warmup=warm,gate=gate)
                    if not warm['passed'] or not gate['passed']:
                        a['status']='baseline_failed';failed_gates+=1
                    else:
                        self.data['current']=f'Footprint pair {pair}/5: {impl} measured idle/load/recovery';self.write()
                        result=self.load(impl,out/'measurement',90,120,90,500)
                        a.update(measured=True,result=result,telemetry=self.telemetry(impl,result['start'],out))
                        coverage=all(v['complete'] for phase in a['telemetry'].values() for v in phase.values())
                        a['status']='pass' if result['passed'] and coverage else ('inconclusive_telemetry' if not coverage else 'fail')
                except Exception as e:
                    a.update(status='interrupted_or_invalid',error=str(e));raise
                finally:
                    self.write()
                if failed_gates>=2:
                    self.data['footprint_halted']='Two baseline failures; remaining scheduled trials not run; no runtime reset';self.write();return

    def child(self, suite, impl, pair, args, bound):
        self.guard()
        if suite=='density':z.inventory_fixture()
        out=self.directory/f'{suite}-{pair:02d}-{impl}';out.mkdir()
        a=dict(suite=suite,pair=pair,implementation=impl,directory=str(out),status='running')
        self.data['attempts'].append(a);self.data['current']=f'{suite}: {impl}, sweep/pair series {pair}';self.write()
        print(self.data['current'],flush=True)
        existing=set((ROOT.parent/suite/'.results').iterdir())
        cmd=[sys.executable,str(ROOT.parent/suite/'run.py')]+args
        self.save(str(out.relative_to(self.directory)/'command.json'),cmd)
        try:
            code=self.supervise(cmd,out/'driver.log',time.monotonic()+bound,lambda:self.guard(resources=suite=='density',live=suite=='density'))
            a.update(exit_code=code,status='completed' if code==0 else 'driver_failed')
        except BaseException as e:a.update(status='interrupted_or_invalid',error=str(e));raise
        finally:
            a['reports']=[str(p) for p in sorted(set((ROOT.parent/suite/'.results').iterdir())-existing)]
            self.write()
        self.guard()
        self.stability('java');self.stability('wasm')
        if code:raise RuntimeError(suite+' driver failed; use its saved checkpoint before continuing')

    def run(self):
        guard=subprocess.Popen(['caffeinate','-i','-s']) if sys.platform=='darwin' else None
        try:
            self.freeze()
            self.footprint()
            for sweep in range(1,4):
                for impl in (('java','wasm') if sweep%2 else ('wasm','java')):
                    self.child('density',impl,sweep,['run',impl],4500)
            self.child('startup_cache','both',1,['run','--pairs','5','--target','both'],5400)
            self.guard();self.data['status']='completed'
        except BaseException as e:self.data.update(status='stopped',error=str(e));raise
        finally:
            self.data['finished']=time.time();self.write()
            if guard:guard.terminate();guard.wait()
            print('Campaign report:',self.directory/'report.html',flush=True)


if __name__=='__main__':
    os.chdir(REPO)
    Campaign().run()
