"""Identical new-pod readiness and HTTP order probes, independent of runtime polling."""
import copy
import json
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def pod_ready(pod):
    return (not pod['metadata'].get('deletionTimestamp') and
            any(c['type']=='Ready' and c['status']=='True' for c in pod.get('status',{}).get('conditions',[])) and
            bool(pod.get('status',{}).get('containerStatuses')) and
            all(c.get('ready') for c in pod['status']['containerStatuses']))


def accepted(impl,status,body):
    return status==201 and ('Placed Successfully' in body if impl=='java' else body.strip()=='201 Created')


class Monitor:
    interval=2
    order_timeout=10

    def __init__(self,state,impl,output,K,save):
        self.state,self.impl,self.output,self.K,self.save=state,impl,output,K,save
        self.lock=threading.Lock();self.stop=threading.Event();self.process=None;self.thread=None
        self.origin=None;self.data=dict(protocol='new-pod-direct-order-v1',poll_seconds=self.interval,order_timeout_seconds=self.order_timeout,pods={},errors=[],baseline_changed=False)
        record=state[impl]
        self.baseline=set(record['baseline_pod_uids'] if impl=='java' else [record['host']['baseline_pod_uid']])
        self.images=record['images'] if impl=='java' else record['host']['images']
        self.selector=record['pod_selector'] if impl=='java' else 'benchmark.platform-poc/run='+record['host']['nonce']
        self.ns=impl
        # Ignore HTTP proxy environment variables for the loopback control channel.
        self.http=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __enter__(self):
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        self.url='http://127.0.0.1:'+str(port)
        self.process=subprocess.Popen(self.K+['proxy','--address=127.0.0.1','--port='+str(port),'--accept-hosts=^127\\.0\\.0\\.1$'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if self.process.poll() is not None:raise RuntimeError('Readiness API proxy exited')
                try:
                    self.request('/version',timeout=2);break
                except (OSError,urllib.error.URLError):time.sleep(.2)
            else:raise RuntimeError('Readiness API proxy did not start')
            self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start();return self
        except BaseException:self.__exit__(None,None,None);raise

    def __exit__(self,*_):
        self.stop.set()
        if self.thread:self.thread.join(timeout=15)
        if self.process:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        self.persist()

    def request(self,path,data=None,timeout=3):
        request=urllib.request.Request(self.url+path,data=None if data is None else json.dumps(data).encode(),headers={'Content-Type':'application/json'})
        with self.http.open(request,timeout=timeout) as response:return response.status,response.read().decode()

    def arm(self):
        with self.lock:
            self.origin=time.monotonic();self.data['request_started_at']=time.time()
        self.persist()

    def snapshot(self):
        with self.lock:return copy.deepcopy(self.data)

    def persist(self):
        self.save(Path(self.output)/'readiness.json',self.snapshot())

    def proven(self,uid):
        return 'order_accepted_seconds' in self.snapshot()['pods'].get(uid,{})

    def ready_count(self):
        data=self.snapshot()
        return sum(p.get('currently_ready',False) and ('order_accepted_seconds' in p or uid in self.baseline) for uid,p in data['pods'].items())

    def sample(self):
        _,body=self.request('/api/v1/namespaces/'+self.ns+'/pods?'+urllib.parse.urlencode(dict(labelSelector=self.selector)))
        pods=json.loads(body)['items']
        with self.lock:
            if self.origin is None:return
            origin=self.origin
            for entry in self.data['pods'].values():entry['currently_ready']=False
            if self.impl=='java' and not self.baseline.issubset({p['metadata']['uid'] for p in pods if not p['metadata'].get('deletionTimestamp')}):self.data['baseline_changed']=True
            # Publish the whole pod-state snapshot before making a potentially
            # slow order probe. Readers must not see the baseline temporarily
            # cleared merely because the new pod happened to be listed first.
            for pod in pods:
                uid=pod['metadata']['uid'];ready=pod_ready(pod)
                entry=self.data['pods'].setdefault(uid,dict(name=pod['metadata']['name'],baseline=uid in self.baseline,created_observed_seconds=time.monotonic()-origin))
                entry['currently_ready']=ready
                if ready:entry.setdefault('pod_ready_seconds',time.monotonic()-origin)
        for pod in pods:
            uid=pod['metadata']['uid']
            with self.lock:
                entry=self.data['pods'][uid];ready=entry['currently_ready'];proven='order_accepted_seconds' in entry
            if not ready or uid in self.baseline or proven:continue
            images={c['name']:c.get('imageID') for c in pod['status']['containerStatuses']}
            if images!=self.images:
                with self.lock:entry['image_mismatch']=True
                continue
            path='/api/v1/namespaces/'+self.ns+'/pods/'+pod['metadata']['name']+(':'+('80' if self.impl=='java' else '8080'))+'/proxy'+('/api/order' if self.impl=='java' else '/orders')
            payload=dict(orderLineItemsDtoList=[dict(skuCode='iphone_13',price=200,quantity=1)]) if self.impl=='java' else [dict(sku='ENG-V8-500',price=8500,quantity=1)]
            began=time.monotonic()
            try:
                status,body=self.request(path,data=payload,timeout=self.order_timeout)
                ok=accepted(self.impl,status,body)
            except urllib.error.HTTPError as error:status=error.code;ok=False
            except (OSError,urllib.error.URLError):status=0;ok=False
            with self.lock:
                entry.setdefault('probe_attempts',[]).append(dict(start_seconds=began-origin,duration_seconds=time.monotonic()-began,status=status,accepted=ok))
                if ok:entry.update(order_accepted_seconds=time.monotonic()-origin,images=images)

    def run(self):
        while not self.stop.is_set():
            began=time.monotonic()
            try:self.sample()
            except (OSError,ValueError,KeyError,urllib.error.URLError) as error:
                with self.lock:self.data['errors'].append(dict(time=time.time(),error=str(error)))
            self.persist()
            self.stop.wait(max(0,self.interval-(time.monotonic()-began)))


def timings(data):
    new=[p for p in data['pods'].values() if not p['baseline']]
    # All additional pods must pass when factor > 2; never report the fastest only.
    def last(key):return max(p[key] for p in new) if new and all(key in p for p in new) else None
    return dict(new_pod_ready_seconds=last('pod_ready_seconds'),new_pod_order_accepted_seconds=last('order_accepted_seconds'))
