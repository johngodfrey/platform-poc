"""Create one temporary additional wasmCloud host, then admit its verified pod to HTTP routing."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

LABEL='benchmark.platform-poc/run'
INSTANCE='app.kubernetes.io/instance'
SERVING={'http-controller','orders-service','inventory-service','products-service','notification-service'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class HostMode:
    def __init__(self,state,port,driver):
        self.state,self.port,self.d=state,port,driver
        self.record=state['wasm'];self.host=self.record['host'];self.K=driver.b.K

    def checkpoint(self):
        if self.state.get('_state_path'):self.d.save(Path(self.state['_state_path']),self.state)

    def event(self,name,**fields):
        if not any(e['name']==name for e in self.host.setdefault('events',[])):
            self.host['events'].append(dict(name=name,time=time.time(),**fields));self.checkpoint()

    def ctl(self):
        return ['--ctl-host','127.0.0.1','--ctl-port',str(self.port),'--lattice','default','--timeout-ms','10000']

    def inventory(self):
        data=json.loads(subprocess.check_output(['wash','get','inventory','-o','json']+self.ctl(),text=True,timeout=15))
        if not data.get('success'):raise RuntimeError('Host inventory unavailable')
        return data.get('inventories',[])

    def snapshot_template(self):
        dep=self.d.b.get('wasm','wasmcloud-host')
        if dep['spec'].get('replicas',1)!=1:raise RuntimeError('New-host mode requires exactly one baseline host pod')
        pods=self.d.b.pods_for('wasm',dep)
        if len(pods)!=1 or pods[0]['metadata'].get('deletionTimestamp'):raise RuntimeError('Expected one stable baseline host pod')
        pod=pods[0]
        for container in dep['spec']['template']['spec']['containers']:
            if any(e['name']=='WASMCLOUD_HOST_SEED' for e in container.get('env',[])):
                raise RuntimeError('Fixed host identity is unsupported: a copied pod must generate a unique host ID')
        for volume in dep['spec']['template']['spec'].get('volumes',[]):
            if 'persistentVolumeClaim' in volume or 'hostPath' in volume:raise RuntimeError('Shared writable host volumes are unsupported')
        statuses={c['name']:c for c in pod['status'].get('containerStatuses',[])}
        if not statuses or not all(c.get('ready') for c in statuses.values()):raise RuntimeError('Baseline host pod is not ready')
        service=self.d.read('service','wasm','platform-poc-http')
        selector=service['spec'].get('selector',{})
        if selector.get(INSTANCE)!='wasmcloud-host' or not selector:
            raise RuntimeError('Expected the persistent selector-based Wasm HTTP service')
        self.host.update(source_deployment_uid=dep['metadata']['uid'],source_template_hash=fingerprint(dep['spec']['template']),
                         baseline_pod_uid=pod['metadata']['uid'],baseline_pod_name=pod['metadata']['name'],
                         images={k:v['imageID'] for k,v in statuses.items()},service_uid=service['metadata']['uid'],service_selector=selector)

    def record_runtime(self,inventory,app):
        if inventory['host_id']!=self.record['runtime']['host_id']:raise RuntimeError('Baseline runtime identity changed')
        key=self.host['label_key']
        if key in inventory.get('labels',{}):raise RuntimeError('Experiment host-label collision')
        entries={c['id']:c for c in inventory.get('components',[])}
        if not SERVING.issubset(entries):raise RuntimeError('Baseline host does not contain every serving component')
        components={}
        for component in app['spec']['components']:
            if component.get('type')!='component':continue
            traits=[t for t in component.get('traits',[]) if t['type']=='spreadscaler']
            if len(traits)!=1 or traits[0]['properties'].get('spread'):raise RuntimeError('Host mode requires the unpartitioned local component spreadscalers')
            if component['properties'].get('config'):raise RuntimeError('Named component configuration requires explicit support')
            entry=entries.get(component['properties']['id'])
            if not entry:raise RuntimeError('Baseline component inventory missing')
            count=traits[0]['properties']['instances']
            if entry['max_instances']!=count:raise RuntimeError('Stored and running component counts differ')
            if entry.get('annotations',{}).get('wasmcloud.dev/spread_name')!='default':raise RuntimeError('Expected default managed spread annotation')
            components[component['name']]=dict(id=entry['id'],count=count,image_ref=entry['image_ref'],annotations=entry.get('annotations',{}))
        self.host['components']=components
        self.host['providers']=[p['id'] for p in inventory.get('providers',[])]
        if not {'http-server','kv-redis','sqldb-postgres','nats'}.issubset(self.host['providers']):raise RuntimeError('Baseline providers missing')

    def make_deployment(self):
        dep=self.d.b.get('wasm','wasmcloud-host')
        if dep['metadata']['uid']!=self.host['source_deployment_uid'] or fingerprint(dep['spec']['template'])!=self.host['source_template_hash']:
            raise RuntimeError('Baseline host template changed; refusing to copy different runtime settings')
        template=copy.deepcopy(dep['spec']['template'])
        template.setdefault('metadata',{})['labels']=dict(template['metadata'].get('labels',{}),**{LABEL:self.host['nonce'],INSTANCE:self.host['pending_instance'],'app.kubernetes.io/managed-by':'scaleout-benchmark'})
        spec=template['spec']
        runtime=next(c for c in spec['containers'] if c['name']=='wasmcloud-host')
        runtime.setdefault('env',[]).append(dict(name='WASMCLOUD_LABEL_'+self.host['label_key'],value='extra'))
        runtime['readinessProbe']=dict(tcpSocket=dict(port=8080),periodSeconds=3,timeoutSeconds=3,failureThreshold=3,initialDelaySeconds=1)
        spec.pop('hostname',None);spec.pop('subdomain',None)
        return dict(apiVersion='apps/v1',kind='Deployment',metadata=dict(name=self.host['deployment_name'],namespace='wasm',labels={LABEL:self.host['nonce']}),
                    spec=dict(replicas=1,selector=dict(matchLabels={LABEL:self.host['nonce']}),template=template,strategy=dict(type='Recreate')))

    def manifest(self,app):
        manifest=dict(apiVersion='core.oam.dev/v1beta1',kind='Application',metadata=dict(name='platform-poc',annotations=copy.deepcopy(app['metadata'].get('annotations',{}))),spec=copy.deepcopy(app['spec']))
        manifest['metadata']['annotations'].pop('kubectl.kubernetes.io/last-applied-configuration',None)
        manifest['metadata']['annotations']['version']=self.record['scaled_version']
        for component in manifest['spec']['components']:
            if component['name'] not in self.host['components']:continue
            count=self.host['components'][component['name']]['count']
            trait=next(t for t in component['traits'] if t['type']=='spreadscaler')
            spreads=[dict(name='default',requirements={self.host['label_key']:'baseline'},weight=1)]
            if component['name']!='data-init':spreads.append(dict(name='scaleout-extra',requirements={self.host['label_key']:'extra'},weight=1))
            trait['properties']=dict(instances=count*len(spreads),spread=spreads)
        return manifest

    def check_app(self):
        app=self.d.read('application','wasm','platform-poc')
        version=app['metadata'].get('annotations',{}).get('version')
        if version not in [self.record['version'],self.record['scaled_version']]:raise RuntimeError('Concurrent application version edit')
        return app

    def validate(self):
        manifest=self.manifest(self.check_app())
        with tempfile.NamedTemporaryFile(mode='w',suffix='.json') as file:
            json.dump(manifest,file);file.flush()
            subprocess.run(['wash','app','validate',file.name],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
        subprocess.run(self.K+['apply','--dry-run=server','-f','-'],input=json.dumps(self.make_deployment()),text=True,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)

    def prepare(self):
        self.check_app()
        subprocess.run(['wash','label',self.record['runtime']['host_id'],self.host['label_key']+'=baseline']+self.ctl(),check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=20)
        self.event('baseline_label_prepared')

    def apply(self):
        self.host['request_started_at']=time.time();self.checkpoint()
        manifest=self.manifest(self.check_app())
        with tempfile.NamedTemporaryFile(mode='w',suffix='.json') as file:
            json.dump(manifest,file);file.flush()
            subprocess.run(['wash','app','deploy',file.name]+self.ctl(),check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
        # create, never apply/adopt: a name collision must not replace another deployment.
        subprocess.run(self.K+['create','-f','-'],input=json.dumps(self.make_deployment()),text=True,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
        dep=self.extra_deployment()
        self.host['deployment_uid']=dep['metadata']['uid'];self.host['deployment_spec_hash']=fingerprint(dep['spec']);self.event('deployment_created')

    def extra_deployment(self):
        cmd=self.K+['-n','wasm','get','deployment',self.host['deployment_name'],'--ignore-not-found','-o','json']
        text=subprocess.check_output(cmd,text=True)
        if not text.strip():return None
        dep=json.loads(text)
        if dep['metadata'].get('labels',{}).get(LABEL)!=self.host['nonce']:raise RuntimeError('Extra deployment ownership marker changed')
        if self.host.get('deployment_uid') and dep['metadata']['uid']!=self.host['deployment_uid']:raise RuntimeError('Extra deployment was recreated; refusing to adopt or delete it')
        return dep

    def service(self):
        service=self.d.read('service','wasm','platform-poc-http')
        if service['metadata']['uid']!=self.host['service_uid'] or service['spec'].get('selector')!=self.host['service_selector']:
            raise RuntimeError('HTTP service changed concurrently')
        return service

    def route(self,pod,enable):
        current=pod['metadata']['labels'].get(INSTANCE)
        desired='wasmcloud-host' if enable else self.host['pending_instance']
        if current not in ['wasmcloud-host',self.host['pending_instance']]:raise RuntimeError('Extra pod routing label changed concurrently')
        if current==desired:return
        ops=[dict(op='test',path='/metadata/uid',value=pod['metadata']['uid']),dict(op='test',path='/metadata/resourceVersion',value=pod['metadata']['resourceVersion']),dict(op='replace',path='/metadata/labels/app.kubernetes.io~1instance',value=desired)]
        subprocess.run(self.K+['-n','wasm','patch','pod',pod['metadata']['name'],'--type=json','-p',json.dumps(ops)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=20)

    def endpoint_ready(self,uid):
        data=json.loads(subprocess.check_output(self.K+['-n','wasm','get','endpointslices','-l','kubernetes.io/service-name=platform-poc-http','-o','json'],text=True))
        return any(e.get('targetRef',{}).get('uid')==uid and e.get('conditions',{}).get('ready') is True for item in data['items'] for e in item.get('endpoints',[]))

    def scale_components(self,host):
        entries={c['id']:c for c in host.get('components',[])}
        ready=True
        for name,component in self.host['components'].items():
            if name=='data-init':continue
            entry=entries.get(component['id'])
            annotations=dict(component['annotations'],**{'wasmcloud.dev/spread_name':'scaleout-extra'})
            if entry and entry['max_instances']==component['count'] and all(entry.get('annotations',{}).get(k)==v for k,v in annotations.items()):continue
            ready=False
            # The host only updates annotations when count/config changes. If a
            # prior reconciliation started this ID with the old spread tag,
            # first change its allowance on the still-unrouted extra host.
            counts=[component['count']+1,component['count']] if entry and entry['max_instances']==component['count'] else [component['count']]
            for count in counts:
                runtime=dict(host_id=host['host_id'],component_id=component['id'],image_ref=component['image_ref'],annotations=annotations)
                self.d.runtime_scale(dict(runtime=runtime),count,self.port)
        return ready

    def observe(self):
        self.service()
        inventories=self.inventory()
        baseline=next((h for h in inventories if h['host_id']==self.record['runtime']['host_id']),None)
        baseline_ready=baseline is not None and SERVING.issubset({c['id'] for c in baseline.get('components',[])})
        count=int(baseline_ready);ready_pods=int(baseline_ready)
        extra=self.extra_deployment()
        if not extra:return dict(serving_hosts=count,ready_pods=ready_pods,orders_concurrency=sum(c['max_instances'] for h in inventories for c in h.get('components',[]) if c['id']=='orders-service'))
        pods=self.d.b.pods_for('wasm',extra)
        if len(pods)!=1:return dict(serving_hosts=count,ready_pods=ready_pods,orders_concurrency=None)
        pod=pods[0]
        if pod['metadata'].get('deletionTimestamp'):return dict(serving_hosts=count,ready_pods=ready_pods,orders_concurrency=None)
        statuses={s['name']:s for s in pod['status'].get('containerStatuses',[])}
        pod_ready=bool(statuses) and all(s.get('ready') for s in statuses.values())
        self.event('extra_pod_created',pod_uid=pod['metadata']['uid'],pod_name=pod['metadata']['name'])
        if pod_ready:ready_pods+=1;self.event('extra_pod_ready',pod_uid=pod['metadata']['uid'])
        hosts=[h for h in inventories if h.get('labels',{}).get(self.host['label_key'])=='extra']
        if len(hosts)!=1:return dict(serving_hosts=count,ready_pods=ready_pods,orders_concurrency=None)
        host=hosts[0]
        if host['host_id']==self.record['runtime']['host_id']:raise RuntimeError('Additional pod reused the baseline host identity')
        self.event('extra_runtime_registered',host_id=host['host_id'])
        providers=set(p['id'] for p in host.get('providers',[]))
        if not set(self.host['providers']).issubset(providers):return dict(serving_hosts=count,ready_pods=ready_pods,orders_concurrency=None)
        components_ready=self.scale_components(host)
        if not components_ready or not pod_ready:return dict(serving_hosts=count,ready_pods=ready_pods,orders_concurrency=None)
        for name,status in statuses.items():
            if status.get('imageID')!=self.host['images'].get(name):raise RuntimeError('Additional pod image digest differs from the baseline')
        self.event('extra_components_ready',images={name:status['imageID'] for name,status in statuses.items()})
        if not self.host.get('probed_pod_uid')==pod['metadata']['uid']:
            monitor=getattr(self.d,'readiness',None)
            if monitor is not None:
                if not monitor.proven(pod['metadata']['uid']):return dict(serving_hosts=count,ready_pods=ready_pods,orders_concurrency=None)
            else:
                path='/api/v1/namespaces/wasm/pods/'+pod['metadata']['name']+':8080/proxy/orders'
                result=subprocess.run(self.K+['create','--raw',path,'-f','-'],input=json.dumps([dict(sku='ENG-V8-500',price=8500,quantity=1)]),text=True,capture_output=True,timeout=15)
                if result.returncode or result.stdout.strip()!='201 Created':return dict(serving_hosts=count,ready_pods=ready_pods,orders_concurrency=None)
            self.host['probed_pod_uid']=pod['metadata']['uid'];self.event('extra_pod_order_accepted')
        self.route(pod,True)
        if self.endpoint_ready(pod['metadata']['uid']):
            count+=1;self.event('extra_http_endpoint_ready',pod_uid=pod['metadata']['uid'],host_id=host['host_id'])
        return dict(serving_hosts=count,ready_pods=ready_pods,orders_concurrency=sum(c['max_instances'] for h in inventories for c in h.get('components',[]) if c['id']=='orders-service'))

    def restore(self):
        self.check_app();dep=self.extra_deployment()
        if dep:
            if self.host.get('deployment_spec_hash') and fingerprint(dep['spec'])!=self.host['deployment_spec_hash']:raise RuntimeError('Extra deployment spec changed concurrently; refusing deletion')
            for pod in self.d.b.pods_for('wasm',dep):self.route(pod,False)
            # Only delete the exact deployment we created, with an API UID
            # precondition. Its pod routing has already been withdrawn.
            options=dict(apiVersion='v1',kind='DeleteOptions',preconditions=dict(uid=dep['metadata']['uid']),propagationPolicy='Foreground')
            path='/apis/apps/v1/namespaces/wasm/deployments/'+self.host['deployment_name']
            subprocess.run(self.K+['delete','--raw',path,'-f','-'],input=json.dumps(options),text=True,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
            deadline=time.monotonic()+150
            while time.monotonic()<deadline:
                pods=json.loads(subprocess.check_output(self.K+['-n','wasm','get','pods','-l',LABEL+'='+self.host['nonce'],'-o','json'],text=True))['items']
                if not pods and self.extra_deployment() is None:break
                time.sleep(2)
            else:raise RuntimeError('Additional host did not terminate')
        # Restore the original version on the baseline host, never on the
        # retiring pod. Temporary model spreads pinned its counts throughout.
        view=copy.deepcopy(self.state);view['wasm_mode']='concurrency'
        self.d.scale(view,'wasm',restore=True,port=self.port)
        for name,component in self.host['components'].items():
            if name=='orders-service':continue
            self.d.runtime_scale(dict(runtime=dict(host_id=self.record['runtime']['host_id'],component_id=component['id'],image_ref=component['image_ref'],annotations=component['annotations'])),component['count'],self.port)
        inventories=self.inventory();baseline=next((h for h in inventories if h['host_id']==self.record['runtime']['host_id']),None)
        if baseline and self.host['label_key'] in baseline.get('labels',{}):
            if baseline['labels'][self.host['label_key']]!='baseline':raise RuntimeError('Experiment runtime label changed concurrently')
            subprocess.run(['wash','label',self.record['runtime']['host_id'],self.host['label_key'],'--delete']+self.ctl(),check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=20)
        self.event('extra_host_removed')


def new_record():
    nonce=uuid.uuid4().hex[:12]
    return dict(nonce=nonce,deployment_name='wasmcloud-scaleout-'+nonce,pending_instance='scaleout-pending-'+nonce,label_key='scaleout_'+nonce,events=[])
