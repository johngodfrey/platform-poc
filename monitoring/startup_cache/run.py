#!/usr/bin/env python3
"""Fresh-worker container-image cold versus cached startup, with warm dependencies."""
import argparse
import copy
from datetime import datetime,timezone
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('scalezero',ROOT.parent/'scalezero/run.py')
z=importlib.util.module_from_spec(spec);spec.loader.exec_module(z)
LABEL='benchmark.platform-poc/startup'
REPOSITORY='k3d-platform-poc.localhost:5001/platform-poc/wasmcloud'


def command(args,timeout=60):
    return subprocess.check_output(args,text=True,stderr=subprocess.PIPE,timeout=timeout)


def placement(template,node,token):
    result=copy.deepcopy(template);pod=result['spec']
    pod.setdefault('nodeSelector',{})['kubernetes.io/hostname']=node
    pod.setdefault('tolerations',[]).append(dict(key=LABEL,operator='Equal',value=token,effect='NoSchedule'))
    return result


def guarded_patch(kind,namespace,name,uid,field,expected,desired):
    """Compare complete values and UID before retrying only resourceVersion conflicts."""
    for attempt in range(4):
        obj=z.s.read(kind,namespace,name)
        current=obj
        for part in field.strip('/').split('/'):current=current[part]
        if obj['metadata']['uid']!=uid or current!=expected:raise RuntimeError('Concurrent edit: '+kind+'/'+name)
        if current==desired:return
        ops=[dict(op='test',path='/metadata/uid',value=uid),dict(op='test',path='/metadata/resourceVersion',value=obj['metadata']['resourceVersion']),dict(op='test',path=field,value=current),dict(op='replace',path=field,value=desired)]
        try:command(z.K+['-n',namespace,'patch',kind,name,'--type=json','-p',json.dumps(ops)],25);return
        except subprocess.CalledProcessError:
            if attempt==3:raise


def publish(state):
    runtime=next(c for c in state['original']['records'][-1]['template']['spec']['containers'] if c['name']=='wasmcloud-host')
    source=runtime['image'];inspection=json.loads(command(['docker','image','inspect',source]))[0]
    if inspection['Id']!=state['original']['records'][-1]['images']['wasmcloud-host']:raise RuntimeError('Local patched image differs from running runtime')
    tag='startup-'+state['token'];local='localhost:5001/platform-poc/wasmcloud:'+tag
    command(['docker','tag',source,local]);print('Publishing unchanged patched runtime to local registry',flush=True)
    command(['docker','push',local],180)
    result=json.loads(command(['docker','image','inspect',local]))[0]
    digests=[r.split('@')[1] for r in result['RepoDigests'] if r.startswith('localhost:5001/platform-poc/wasmcloud@')]
    if len(set(digests))!=1:raise RuntimeError('Registry digest unavailable')
    state['runtime_image']=REPOSITORY+'@'+digests[0];state['runtime_config_digest']=inspection['Id'];state['runtime_manifest_digest']=digests[0]


def images(node):
    return json.loads(command(['docker','exec',node,'crictl','images','-o','json'],20))['images']


def cache_audit(node,records,cold):
    found=images(node);required={c['image'] for r in records for c in r['template']['spec']['containers']}
    # Fresh k3d agents contain no container images. Avoid pruning shared existing workers.
    if cold and found:raise RuntimeError('Fresh worker already has images; cold trial invalid')
    if not cold:
        available={ref for image in found for ref in image.get('repoTags',[])+image.get('repoDigests',[])}
        missing={ref for ref in required if ref not in available and ('docker.io/library/'+ref) not in available}
        if missing:raise RuntimeError('Application images absent before cached trial: '+str(sorted(missing)))
    return dict(node=node,cold=cold,images=found,required_images=sorted(required),time=time.time())


def node_create(state,implementation,pair):
    short='startup-'+state['token']+'-'+implementation+'-'+str(pair);node='k3d-'+short+'-0'
    state.setdefault('nodes',[]).append(node);checkpoint(state)
    command(['k3d','node','create',short,'--cluster','platform-poc','--role','agent','--k3s-node-label',LABEL+'='+state['token'],'--k3s-arg','--node-taint='+LABEL+'='+state['token']+':NoSchedule','--timeout','120s'],150)
    command(z.K+['wait','node/'+node,'--for=condition=Ready','--timeout=120s'],130)
    obj=z.s.read('node','',node)
    if obj['metadata']['labels'].get(LABEL)!=state['token']:raise RuntimeError('Temporary node ownership mismatch')
    return node


def node_delete(state,node):
    raw=command(z.K+['get','node',node,'--ignore-not-found','-o','json'],25)
    if raw.strip():
        obj=json.loads(raw)
        if obj['metadata']['labels'].get(LABEL)!=state['token']:raise RuntimeError('Refusing to remove unowned node')
        pods=json.loads(command(z.K+['get','pods','-A','--field-selector','spec.nodeName='+node,'-o','json'],25))['items']
        if any(p['metadata']['namespace']!='kube-system' for p in pods):raise RuntimeError('Application pods still on temporary node')
    # Container may survive a failed node registration; exact run-specific name is recorded before create.
    exists=subprocess.run(['docker','inspect',node],capture_output=True,timeout=10).returncode==0
    if exists:command(['k3d','node','delete',node],90)
    if raw.strip():command(z.K+['delete','node',node,'--ignore-not-found'],25)
    state['nodes']=[n for n in state['nodes'] if n!=node];checkpoint(state)


def checkpoint(state):z.save(Path(state['directory'])/'state.json',state)


def prepare(state,implementation,node,proxy):
    original=state['original'];active=copy.deepcopy(original)
    if implementation=='java':z.inventory_fixture()
    z.patch_replicas(original,implementation,0);z.wait_zero(original,implementation,proxy)
    for record in active['records']:
        if record['namespace']!=implementation:continue
        record['template']=placement(record['template'],node,state['token'])
        if implementation=='wasm':
            for container in record['template']['spec']['containers']:
                if container['name']=='wasmcloud-host':container['image']=state['runtime_image']
                if container['name']=='nats-leaf':container['image']=record['images']['nats-leaf']
            record['allowed_image_ids']={name:[image] for name,image in record['images'].items()}
            record['allowed_image_ids']['wasmcloud-host']+=[state['runtime_image'],state['runtime_manifest_digest']]
        record['template_hash']=z.s.h.fingerprint(record['template'])
    if implementation=='wasm':
        active['hostconfig']['spec']['image']=state['runtime_image']
        active['hostconfig']['spec']['natsLeafImage']=original['records'][-1]['images']['nats-leaf']
        # Operator 0.4.0 skips Deployments entirely when schedulingOptions is
        # present with daemonset=false. Keep that field absent; own placement
        # fields directly, which its server-side apply preserves.
        intermediate=copy.deepcopy(original['records'][-1]['template'])
        for container in intermediate['spec']['containers']:
            if container['name']=='wasmcloud-host':container['image']=state['runtime_image']
            if container['name']=='nats-leaf':container['image']=original['records'][-1]['images']['nats-leaf']
        state['intermediate_template']=intermediate
    state['active']=active;state['active_implementation']=implementation;checkpoint(state)
    if implementation=='java':
        for before,after in zip(original['records'],active['records']):
            if before['namespace']=='java':guarded_patch('deployment','java',before['name'],before['uid'],'/spec/template',before['template'],after['template'])
    else:
        before=copy.deepcopy(original['hostconfig']['spec']);before['hostReplicas']=0
        after=copy.deepcopy(active['hostconfig']['spec']);after['hostReplicas']=0
        guarded_patch('wasmcloudhostconfig','wasm','wasmcloud-host',original['hostconfig']['uid'],'/spec',before,after)
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            dep=z.s.read('deployment','wasm','wasmcloud-host')
            if dep['spec']['template']==intermediate:break
            time.sleep(1)
        else:
            z.save(Path(state['directory'])/'unexpected-host-template.json',dep['spec']['template'])
            raise RuntimeError('Operator did not produce the expected isolated host template')
        guarded_patch('deployment','wasm','wasmcloud-host',active['records'][-1]['uid'],'/spec/template',intermediate,active['records'][-1]['template'])
    return active


def restore_active(state,proxy):
    active=state.get('active');original=state['original'];implementation=state.get('active_implementation')
    if active:
        # An interrupted prepare can leave original or experiment templates. Refuse unknown edits.
        if implementation=='java':
            for before,after in zip(original['records'],active['records']):
                if before['namespace']!='java':continue
                dep=z.s.read('deployment','java',before['name'])
                if dep['spec']['template'] not in [before['template'],after['template']]:raise RuntimeError('Unknown Java template during recovery')
                current=copy.deepcopy(after);current['template']=dep['spec']['template'];current['template_hash']=z.s.h.fingerprint(current['template'])
                temp=copy.deepcopy(active);temp['records']=[current]
                z.patch_replicas(temp,'java',0)
            # Wait for all former pods before restoring create-drop inventory.
            deadline=time.monotonic()+180
            while any(proxy.pods(r) for r in original['records'] if r['namespace']=='java'):
                if time.monotonic()>deadline:raise RuntimeError('Java pods did not stop during recovery')
                time.sleep(1)
            for before,after in zip(original['records'],active['records']):
                if before['namespace']!='java':continue
                dep=z.s.read('deployment','java',before['name'])
                guarded_patch('deployment','java',before['name'],before['uid'],'/spec/template',dep['spec']['template'],before['template'])
        else:
            host=z.s.read('wasmcloudhostconfig','wasm','wasmcloud-host');current=host['spec'];expected=copy.deepcopy(active['hostconfig']['spec']);expected['hostReplicas']=current['hostReplicas']
            baseline=copy.deepcopy(original['hostconfig']['spec']);baseline['hostReplicas']=current['hostReplicas']
            if current not in [expected,baseline]:raise RuntimeError('Unknown HostConfig spec during recovery')
            zero=copy.deepcopy(current);zero['hostReplicas']=0
            guarded_patch('wasmcloudhostconfig','wasm','wasmcloud-host',original['hostconfig']['uid'],'/spec',current,zero)
            deadline=time.monotonic()+180
            while proxy.pods(original['records'][-1]):
                if time.monotonic()>deadline:raise RuntimeError('Wasm host did not stop during recovery')
                time.sleep(1)
            dep=z.s.read('deployment','wasm','wasmcloud-host')
            known=[original['records'][-1]['template'],active['records'][-1]['template']]
            if state.get('intermediate_template'):known.append(state['intermediate_template'])
            if dep['spec']['template'] not in known:raise RuntimeError('Unknown Wasm template during recovery')
            if dep['spec']['template']==active['records'][-1]['template']:
                guarded_patch('deployment','wasm','wasmcloud-host',original['records'][-1]['uid'],'/spec/template',dep['spec']['template'],state['intermediate_template'])
            target=copy.deepcopy(original['hostconfig']['spec']);target['hostReplicas']=0
            guarded_patch('wasmcloudhostconfig','wasm','wasmcloud-host',original['hostconfig']['uid'],'/spec',zero,target)
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                dep=z.s.read('deployment','wasm','wasmcloud-host')
                if dep['spec']['template']==original['records'][-1]['template']:break
                time.sleep(1)
            else:raise RuntimeError('Original Wasm template not restored')
        state.pop('active',None);state.pop('active_implementation',None);state.pop('intermediate_template',None);checkpoint(state)
    z.restore(original)


def report(state):
    directory=Path(state['directory']);z.save(directory/'report.json',{k:v for k,v in state.items() if k not in ['original','active']})
    page='<!doctype html><meta charset="utf-8"><title>Cold versus cached startup</title>'
    if state['status']=='running':page+='<meta http-equiv="refresh" content="5">'
    page+='<style>body{font:18px system-ui;background:#101827;color:#eef;max-width:1250px;margin:35px auto;padding:20px}td,th{padding:12px;border-bottom:1px solid #456;text-align:left}table{border-collapse:collapse;width:100%}</style><h1>Cold versus cached container-image startup</h1>'
    page+='<p>'+html.escape(state['status'])+' — '+html.escape(state.get('current',''))+'</p><p>Fresh tainted k3d worker per implementation/pair. Cold starts with an empty container-image inventory; cached repeats on that same worker. Cold always precedes cached to populate the cache. Dependencies, local registry, ingress and monitoring remain warm. Worker creation is outside the clock. Cache preparation is verified before each trial.</p>'
    page+='<p>Clock starts at replica request and ends at the first accepted HTTP order. Fresh application processes in both conditions; Wasm components/providers are fetched/compiled afresh because the runtime has no persistent cache volume. Thus this compares <b>node container-image caches</b>, not persistent Wasm compilation caching or cold databases. Application/runtime images come from the local registry; the pinned Wasm NATS sidecar comes from Docker Hub, so its cold pull includes that network path. Pull policies and 5-second/4-second limits are unchanged. Java inventory schema/seed initialization is included. One extra worker exists at a time; all four Java pods share that worker.</p>'
    page+='<table><tr><th>Application</th><th>Image cache</th><th>Valid trials</th><th>First order median [range], s</th><th>All pods Ready median, s</th></tr>'
    maximum=max([r['first_order_seconds'] for r in state['trials']]+[1])
    for implementation in ['java','wasm']:
        for mode in ['cold','cached']:
            rows=[r for r in state['trials'] if r['implementation']==implementation and r['cache_mode']==mode and r['valid']]
            points=[r['first_order_seconds'] for r in rows]
            value='—' if not points else f'{statistics.median(points):.2f} [{min(points):.2f}–{max(points):.2f}]'
            ready='—' if not rows else f'{statistics.median(r["all_pods_ready_seconds"] for r in rows):.2f}'
            page+='<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in [implementation,mode,len(rows),value,ready])+'</tr>'
    page+='</table><h2>Every startup</h2>'
    for row in state['trials']:
        label=f'Pair {row["pair"]} {row["implementation"]} {row["cache_mode"]}'
        colour='#65baff' if row['cache_mode']=='cold' else '#80dfb0'
        if not row['valid']:label+=' INVALID';colour='#777'
        page+='<div style="display:flex;align-items:center;gap:12px;margin:12px 0"><span style="width:215px">'+label+'</span><span style="height:22px;background:'+colour+';width:'+str(60*row['first_order_seconds']/maximum)+'%"></span><span>'+f'{row["first_order_seconds"]:.2f} s'+'</span></div>'
    page+='<p>Restored: '+str(state.get('restored',False))+'. '+html.escape(state.get('error',''))+' '+html.escape(state.get('restoration_error',''))+'</p>'
    if state.get('excluded_pairs'):page+='<p>Excluded pairs (both members omitted from comparison): '+html.escape(json.dumps(state['excluded_pairs']))+'</p>'
    for source in state.get('source_reports',[]):page+='<p>Raw source: <a href="'+html.escape(source,quote=True)+'">'+html.escape(source)+'</a></p>'
    temp=directory/'report.html.tmp';temp.write_text(page);temp.replace(directory/'report.html')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['run','restore']);parser.add_argument('--pairs',type=int,default=3);parser.add_argument('--target',choices=['both','java','wasm'],default='both');parser.add_argument('--state',type=Path);args=parser.parse_args()
    if args.action=='restore':
        if not args.state:parser.error('--state required')
        state=json.loads(args.state.read_text())
        with z.Proxy() as proxy:restore_active(state,proxy)
        for node in list(state.get('nodes',[])):node_delete(state,node)
        state.update(restored=True,status='recovered');checkpoint(state);report(state);return
    if args.pairs<1:parser.error('Positive pair count required')
    power=command(['pmset','-g','batt']) if sys.platform=='darwin' else ''
    if sys.platform=='darwin' and "'AC Power'" not in power:raise RuntimeError('Connect mains power first')
    guard=subprocess.Popen(['caffeinate','-i','-s']) if sys.platform=='darwin' else None
    state=None
    try:
        original=z.snapshot();z.runtime_snapshot(original)
        for implementation in ['java','wasm']:z.s.b.wait_functional(implementation)
        token=datetime.now(timezone.utc).strftime('%m%d%H%M%S');directory=ROOT/'.results'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');directory.mkdir(parents=True)
        state=dict(directory=str(directory),token=token,original=original,status='running',trials=[],nodes=[],restored=False,power_before=power,settings=dict(pairs=args.pairs,target=args.target))
        checkpoint(state);report(state);print('Report:',directory/'report.html',flush=True)
        for source in [Path(__file__),ROOT.parent/'scalezero/run.py']:
            content=source.read_bytes();(directory/(source.parent.name+'-driver.py')).write_bytes(content)
            state.setdefault('source_sha256',{})[source.parent.name]=hashlib.sha256(content).hexdigest()
        publish(state);checkpoint(state)
        with z.Proxy() as proxy:
            for pair in range(1,args.pairs+1):
                for implementation in (['java','wasm'] if pair%2 else ['wasm','java']):
                    if args.target not in ['both',implementation]:continue
                    if sys.platform=='darwin' and "'AC Power'" not in command(['pmset','-g','batt']):raise RuntimeError('Mains disconnected')
                    state['current']=f'Pair {pair}: {implementation}, creating fresh worker';report(state)
                    node=node_create(state,implementation,pair);active=prepare(state,implementation,node,proxy)
                    records=[r for r in active['records'] if r['namespace']==implementation]
                    for mode in ['cold','cached']:
                        state['current']=f'Pair {pair}: {implementation} {mode}';report(state)
                        audit=cache_audit(node,records,mode=='cold');output=directory/f'pair-{pair:02d}-{implementation}-{mode}'
                        result=z.trial(active,implementation,pair,output,proxy,argparse.Namespace(timeout=300,stable_seconds=10,zero_prepared=mode=='cold'))
                        result.update(cache_mode=mode,node=node,cache_before=audit)
                        pod_uids=set(result['pods']['pods'])
                        events=json.loads(command(z.K+['-n',implementation,'get','events','-o','json'],25))['items']
                        result['image_events']=[dict(reason=e.get('reason'),message=e.get('message'),first_timestamp=e.get('firstTimestamp'),last_timestamp=e.get('lastTimestamp'),pod=e.get('involvedObject',{}).get('name')) for e in events if e.get('involvedObject',{}).get('uid') in pod_uids and e.get('reason') in ['Pulling','Pulled','Failed','BackOff']]
                        # All selected pods must have been placed on the isolated worker.
                        for r in records:
                            if any(p['spec']['nodeName']!=node for p in proxy.pods(r)):raise RuntimeError('Pod scheduled outside test worker')
                        z.save(output/'result.json',result);state['trials'].append(result);checkpoint(state);report(state)
                    restore_active(state,proxy);node_delete(state,node)
        state['status']='complete'
    except BaseException as error:
        if state:state.update(status='failed',error=str(error))
        raise
    finally:
        if state:
            try:
                with z.Proxy() as proxy:restore_active(state,proxy)
                for node in list(state.get('nodes',[])):node_delete(state,node)
                state['restored']=True
            except BaseException as error:state.update(status='failed',restoration_error=str(error))
            if sys.platform=='darwin':state['power_after']=command(['pmset','-g','batt'])
            checkpoint(state);report(state);print('Report:',Path(state['directory'])/'report.html',flush=True)
            print('Recovery: python3 monitoring/startup_cache/run.py restore --state '+str(Path(state['directory'])/'state.json'),flush=True)
        if guard:guard.terminate();guard.wait()
        if state and not state['restored']:raise RuntimeError('Recovery needed: '+state.get('restoration_error','unknown'))

if __name__=='__main__':main()
