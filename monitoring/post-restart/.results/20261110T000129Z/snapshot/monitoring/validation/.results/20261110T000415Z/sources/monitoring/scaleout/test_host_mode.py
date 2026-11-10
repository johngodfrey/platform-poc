import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('runner',Path(__file__).with_name('run.py'))
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
h=r.h

class HostModeTests(unittest.TestCase):
    def setUp(self):
        self.template=dict(metadata=dict(labels={'app.kubernetes.io/name':'wasmcloud',h.INSTANCE:'wasmcloud-host'}),spec=dict(containers=[dict(name='wasmcloud-host',image='patched:1',env=[dict(name='WASMCLOUD_MAX_EXECUTION_TIME_MS',value='5000'),dict(name='WASMCLOUD_RPC_TIMEOUT_MS',value='4000')],resources=dict(limits=dict(memory='1Gi')))],volumes=[dict(name='config',configMap=dict(name='nats-config'))]))
        self.dep=dict(metadata=dict(uid='original'),spec=dict(replicas=1,template=self.template))
        self.state=dict(wasm_mode='host',wasm=dict(original=10,target=20,version='v1',scaled_version='v2',runtime=dict(host_id='BASE'),host=h.new_record()))
        self.state['wasm']['host'].update(source_deployment_uid='original',source_template_hash=h.fingerprint(self.template),components={name:dict(id=name,count=10 if name=='orders-service' else 1,image_ref='ref/'+name,annotations={'wasmcloud.dev/spread_name':'default'}) for name in list(h.SERVING)+['data-init']})
        self.driver=SimpleNamespace(b=SimpleNamespace(K=['kubectl'],get=Mock(return_value=self.dep)),runtime_scale=Mock(),save=Mock(),read=Mock())
        self.mode=h.HostMode(self.state,14223,self.driver)

    def test_template_preserves_patch_limits_and_has_unique_pending_selector(self):
        dep=self.mode.make_deployment();t=dep['spec']['template']
        self.assertNotEqual(t['metadata']['labels'][h.INSTANCE],'wasmcloud-host')
        self.assertEqual(dep['spec']['selector']['matchLabels'],{h.LABEL:self.state['wasm']['host']['nonce']})
        self.assertFalse(dep['metadata'].get('ownerReferences'))
        container=t['spec']['containers'][0]
        self.assertEqual(container['image'],'patched:1')
        self.assertEqual(container['resources'],self.template['spec']['containers'][0]['resources'])
        self.assertEqual(container['env'][:2],self.template['spec']['containers'][0]['env'])
        self.assertEqual(container['readinessProbe']['tcpSocket']['port'],8080)
        self.assertEqual(self.template['metadata']['labels'][h.INSTANCE],'wasmcloud-host')

    def test_refuses_changed_original_template(self):
        self.dep['spec']['template']['spec']['containers'][0]['image']='different'
        with self.assertRaises(RuntimeError):self.mode.make_deployment()

    def test_manifest_preserves_baseline_and_excludes_data_init_from_new_host(self):
        components=[dict(name=name,type='component',properties=dict(id=name,image='ref/'+name),traits=[dict(type='spreadscaler',properties=dict(instances=data['count']))]) for name,data in self.state['wasm']['host']['components'].items()]
        provider=dict(name='http-server',type='capability',properties=dict(image='patched-provider'))
        app=dict(metadata=dict(annotations=dict(version='v1')),spec=dict(components=components+[provider]))
        original=copy.deepcopy(app);m=self.mode.manifest(app)
        self.assertEqual(app,original)
        self.assertEqual(m['spec']['components'][-1],provider)
        for component in m['spec']['components'][:-1]:
            props=component['traits'][0]['properties'];count=self.state['wasm']['host']['components'][component['name']]['count']
            self.assertEqual(props['spread'][0]['name'],'default')
            self.assertEqual(props['spread'][0]['requirements'],{self.state['wasm']['host']['label_key']:'baseline'})
            self.assertEqual(props['instances'],count*(1 if component['name']=='data-init' else 2))

    def test_native_scale_uses_each_component_id_and_keeps_data_init_off_new_host(self):
        self.assertFalse(self.mode.scale_components(dict(host_id='EXTRA',components=[])))
        self.assertEqual({call.args[0]['runtime']['component_id'] for call in self.driver.runtime_scale.call_args_list},h.SERVING)
        for call in self.driver.runtime_scale.call_args_list:
            runtime=call.args[0]['runtime'];self.assertEqual(runtime['host_id'],'EXTRA')
            self.assertEqual(runtime['annotations']['wasmcloud.dev/spread_name'],'scaleout-extra')

    def test_runtime_helper_does_not_hardcode_orders(self):
        with patch.object(r.subprocess,'run') as call:
            r.runtime_scale(dict(runtime=dict(host_id='EXTRA',image_ref='ref',component_id='inventory-service',annotations={})),10,14223)
        self.assertEqual(call.call_args.args[0][5],'inventory-service')

    def test_deployment_identity_prevents_adopting_recreated_resource(self):
        self.state['wasm']['host']['deployment_uid']='ours'
        dep=dict(metadata=dict(uid='replacement',labels={h.LABEL:self.state['wasm']['host']['nonce']}))
        with patch.object(h.subprocess,'check_output',return_value=json.dumps(dep)):
            with self.assertRaises(RuntimeError):self.mode.extra_deployment()

    def test_dry_run_validates_private_model_and_never_deploys(self):
        app=dict(metadata=dict(annotations=dict(version='v1')),spec=dict(components=[]))
        self.driver.read.return_value=app
        with patch.object(h.subprocess,'run') as run:
            def inspect(cmd,**kw):
                if cmd[:3]==['wash','app','validate']:
                    self.assertEqual(Path(cmd[3]).stat().st_mode & 0o777,0o600)
                else:
                    self.assertIn('--dry-run=server',cmd)
            run.side_effect=inspect
            self.mode.validate()
            self.assertEqual(run.call_count,2)
        self.assertEqual(self.state['wasm']['host']['events'],[])

    def test_route_change_checks_exact_pod_uid_and_version(self):
        pod=dict(metadata=dict(uid='pod-uid',resourceVersion='42',name='extra',labels={h.INSTANCE:self.state['wasm']['host']['pending_instance']}))
        with patch.object(h.subprocess,'run') as run:self.mode.route(pod,True)
        cmd=run.call_args.args[0];ops=json.loads(cmd[cmd.index('-p')+1])
        self.assertEqual(ops[0],dict(op='test',path='/metadata/uid',value='pod-uid'))
        self.assertEqual(ops[1]['value'],'42')
        self.assertEqual(ops[2]['value'],'wasmcloud-host')

    def test_host_capacity_units_are_not_orders_concurrency(self):
        self.assertEqual(r.bounds(self.state,'wasm'),(1,2))
        self.state['wasm_mode']='concurrency';self.assertEqual(r.bounds(self.state,'wasm'),(10,20))

if __name__=='__main__':unittest.main()
