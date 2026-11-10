import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('scaleout',Path(__file__).with_name('run.py'))
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)


class ScaleTests(unittest.TestCase):
    def args(self):return SimpleNamespace(warmup=30,baseline=30,before_scale=45,after_scale=90,window=15,burst_rate=200,p95_ms=500)
    def summary(self):
        metrics={}
        for w in range(4,14):
            for name,value in [('scaleout_completed',3000),('scaleout_accepted',3000),('scaleout_failures',0)]:metrics[f'{name}{{window:{w}}}']={'count':value}
            metrics[f'scaleout_latency_ms{{window:{w}}}']={'p(95)':20}
        return dict(metrics=metrics)

    def test_already_healthy_does_not_get_a_recovery_claim(self):
        result=r.recovery_windows(self.summary(),self.args(),107)
        self.assertTrue(result['already_healthy_before_scale'])
        self.assertIsNone(result['recovery_seconds'])

    def test_recovery_needs_two_full_consecutive_windows(self):
        summary=self.summary()
        summary['metrics']['scaleout_latency_ms{window:5}']['p(95)']=1000
        result=r.recovery_windows(summary,self.args(),107)
        self.assertFalse(result['already_healthy_before_scale'])
        self.assertEqual(result['recovery_seconds'],43) # windows 120-135 and 135-150
        summary['metrics']['scaleout_completed{window:8}']['count']=2000
        summary['metrics']['scaleout_accepted{window:8}']['count']=2000
        result=r.recovery_windows(summary,self.args(),107)
        self.assertEqual(result['recovery_seconds'],58) # 135-165

    def test_completion_jitter_does_not_invent_a_recovery_benefit(self):
        summary=self.summary()
        summary['metrics']['scaleout_completed{window:5}']['count']=2936
        summary['metrics']['scaleout_accepted{window:5}']['count']=2936
        summary['metrics']['scaleout_completed{window:6}']['count']=3064
        summary['metrics']['scaleout_accepted{window:6}']['count']=3064
        result=r.recovery_windows(summary,self.args(),107)
        self.assertTrue(result['already_healthy_before_scale'])
        self.assertIsNone(result['recovery_seconds'])

    def test_recovery_cannot_precede_observed_capacity(self):
        summary=self.summary();summary['metrics']['scaleout_latency_ms{window:5}']['p(95)']=1000
        result=r.recovery_windows(summary,self.args(),107,ready_s=140)
        self.assertEqual(result['recovery_seconds'],73) # cohorts 150-165 and 165-180

    def test_restore_still_sets_runtime_if_manifest_is_already_original(self):
        state=self.state();state['wasm']['runtime']=dict(host_id='H',image_ref='ref',annotations={'wasmcloud.dev/appspec':'platform-poc'})
        with patch.object(r,'read',return_value=self.app()),patch.object(r.subprocess,'run') as call:
            r.scale(state,'wasm',restore=True)
        cmd=call.call_args.args[0]
        self.assertEqual(cmd[:3],['wash','scale','component'])
        self.assertEqual(cmd[cmd.index('--max-instances')+1],'10')
        self.assertIn('wasmcloud.dev/appspec=platform-poc',cmd)

    def test_missing_or_slow_windows_cannot_pass(self):
        summary=self.summary()
        for w in range(8,14):summary['metrics']['scaleout_latency_ms{window:'+str(w)+'}']['p(95)']=500
        result=r.recovery_windows(summary,self.args(),107)
        self.assertFalse(result['post_scale_slo_observed'])

    def app(self):
        return dict(metadata=dict(uid='u',resourceVersion='7',annotations=dict(version='old')),spec=dict(components=[dict(name='orders-service',traits=[dict(type='spreadscaler',properties=dict(instances=10))])]))
    def state(self):return dict(wasm=dict(uid='u',original=10,target=20,version='old',scaled_version='new'))

    def test_only_count_and_version_change_in_private_manifest(self):
        original=self.app()
        with patch.object(r,'read',return_value=original),patch.object(r.subprocess,'run') as call:
            import json
            def inspect(cmd,**kw):
                if cmd[:3]==['wash','app','validate']:
                    manifest=json.loads(Path(cmd[3]).read_text())
                    self.assertEqual(manifest['spec']['components'][0]['traits'][0]['properties']['instances'],20)
                    self.assertEqual(manifest['metadata']['annotations']['version'],'new')
                    self.assertEqual(Path(cmd[3]).stat().st_mode & 0o777,0o600)
            call.side_effect=inspect
            r.scale(self.state(),'wasm',dry_run=True)
            self.assertEqual(call.call_count,1) # validation must never deploy
        self.assertEqual(original['metadata']['annotations']['version'],'old')

    def test_version_derived_uid_is_accepted_for_restoration(self):
        app=self.app();app['metadata'].update(uid='scaled-uid');app['metadata']['annotations']['version']='new'
        app['spec']['components'][0]['traits'][0]['properties']['instances']=20
        state=self.state();state['wasm']['scaled_uid']='scaled-uid'
        with patch.object(r,'read',return_value=app),patch.object(r.subprocess,'run') as call:
            r.scale(state,'wasm',restore=True)
        self.assertEqual(call.call_args.args[0][:5],['wash','app','deploy','platform-poc','old'])

    def test_restore_refuses_concurrent_version_or_count_changes(self):
        for version,count in [('other',20),('new',30)]:
            app=self.app();app['metadata']['annotations']['version']=version;app['spec']['components'][0]['traits'][0]['properties']['instances']=count
            with patch.object(r,'read',return_value=app),patch.object(r.subprocess,'run') as call:
                with self.assertRaises(RuntimeError):r.scale(self.state(),'wasm',restore=True)
                call.assert_not_called()

    def test_restore_accepts_patch_completed_before_process_died(self):
        app=self.app();app['metadata']['annotations']['version']='new';app['spec']['components'][0]['traits'][0]['properties']['instances']=20
        with patch.object(r,'read',return_value=app),patch.object(r.subprocess,'run') as call:
            r.scale(self.state(),'wasm',restore=True)
        self.assertEqual(call.call_args.args[0][:5],['wash','app','deploy','platform-poc','old'])


if __name__=='__main__':unittest.main()
