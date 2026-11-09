"""Safety and verdict regression tests for memory sweeps."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('density_runner', Path(__file__).with_name('run.py'))
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)


class DensityTests(unittest.TestCase):
    def test_cpu_limit_removed_without_losing_other_resources(self):
        original = {'limits':{'cpu':'500m','memory':'1Gi','ephemeral-storage':'1Gi'},'requests':{'cpu':'500m','memory':'1Gi'}}
        resources = r.memory_resources(original,256,'java')
        self.assertNotIn('cpu',resources['limits'])
        self.assertEqual(resources['limits']['memory'],'256Mi')
        self.assertEqual(resources['requests']['cpu'],'100m')
        self.assertEqual(resources['limits']['ephemeral-storage'],'1Gi')
        self.assertEqual(original['limits']['cpu'],'500m')

    def summary(self):
        return {'metrics':{'http_reqs':{'count':1500},'demo_orders_accepted':{'count':1500},'demo_order_failures':{'count':0},'http_req_failed':{'value':0},'dropped_iterations':{'count':0},'http_req_duration':{'p(95)':20}}}

    def test_missing_or_busy_cpu_is_inconclusive_not_memory_failure(self):
        args = SimpleNamespace(rate=25,seconds=60,p95_ms=100,max_node_cpu=.6)
        for cpu in [dict(complete=False,max_fraction=.1),dict(complete=True,max_fraction=.6)]:
            result = r.classify(self.summary(),0,args,True,True,cpu)
            self.assertEqual(result['status'],'inconclusive_cpu')
            self.assertFalse(result['passed'])
        self.assertTrue(r.classify(self.summary(),0,args,True,True,dict(complete=True,max_fraction=.2))['passed'])

    def test_latency_failure_remains_failure_with_cpu_headroom(self):
        args = SimpleNamespace(rate=25,seconds=60,p95_ms=100,max_node_cpu=.6)
        summary = self.summary(); summary['metrics']['http_req_duration']['p(95)']=101
        self.assertEqual(r.classify(summary,99,args,True,True,dict(complete=True,max_fraction=.2))['status'],'fail')

    def record(self):
        return dict(namespace='java',deployment='order-service',container='order-service',uid='u',original={},budget={},allowed=[{}])

    def test_recovery_is_saved_before_patch_even_when_patch_fails(self):
        state = dict(records=[self.record()])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            def fail(*args,**kwargs):
                import json
                saved = json.loads(path.read_text())
                self.assertEqual(saved['records'][0]['allowed'][-1]['limits']['memory'],'1024Mi')
                raise RuntimeError('patch interrupted')
            with patch.object(r.b,'patch',side_effect=fail):
                with self.assertRaises(RuntimeError): r.apply_cap(state,path,'java',1024)

    def test_restore_refuses_concurrent_edit(self):
        record = self.record()
        deployment = {'spec':{'template':{'spec':{'containers':[{'name':'order-service','resources':{'limits':{'memory':'9Gi'}}}]}}}}
        with patch.object(r.b,'get',return_value=deployment), patch.object(r.b,'patch') as mutate:
            with self.assertRaisesRegex(RuntimeError,'Concurrent'): r.restore(dict(records=[record]))
            mutate.assert_not_called()

    def test_restore_accepts_patch_completed_before_state_update(self):
        record = self.record()
        pending = {'limits':{'memory':'256Mi'}}
        record['allowed'].append(pending)
        deployment = {'spec':{'template':{'spec':{'containers':[{'name':'order-service','resources':pending}]}}}}
        with patch.object(r.b,'get',return_value=deployment), patch.object(r.b,'patch') as mutate, patch.object(r.b,'rollout'):
            r.restore(dict(records=[record]))
            self.assertEqual(mutate.call_args.args[0]['budget'],pending)
            self.assertTrue(mutate.call_args.kwargs['restore'])

    def test_total_java_memory_is_split_four_ways(self):
        records = [dict(self.record(),deployment=name) for name in r.b.JAVA]
        with patch.object(r.b,'patch') as mutate:
            r.apply_cap(dict(records=records),None,'java',1024,dry_run=True)
        self.assertEqual(len(mutate.call_args_list),4)
        for call in mutate.call_args_list:
            self.assertEqual(call.args[0]['budget']['limits']['memory'],'256Mi')

    def test_missing_node_telemetry_cannot_pass(self):
        response = dict(status='success',data=dict(result=[dict(metric=dict(node='a'),values=[[i,'0.1'] for i in range(12)])]))
        nodes = '{"items":[{"metadata":{"name":"a"}},{"metadata":{"name":"b"}}]}'
        with patch.object(r,'fetch',return_value=response), patch.object(r.subprocess,'check_output',return_value=nodes):
            self.assertFalse(r.cpu_samples(0,60)['complete'])


if __name__ == '__main__': unittest.main()
