"""Regression checks for capacity gates and safe resource restoration."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('budget_runner', Path(__file__).with_name('run.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

class BudgetTests(unittest.TestCase):
    def test_a_missing_or_low_request_count_cannot_pass(self):
        summary={'metrics':{'http_req_duration':{'p(95)':10},'http_req_failed':{'value':0}}}
        self.assertFalse(runner.result_from(summary,0,100,60,100)['passed'])

    def test_no_http_failures_is_not_enough_if_work_was_dropped(self):
        metrics={'http_reqs':{'count':6000},'demo_orders_accepted':{'count':6000},
                 'demo_order_failures':{'count':0},'http_req_failed':{'value':0},
                 'dropped_iterations':{'count':3},'http_req_duration':{'p(95)':10}}
        self.assertFalse(runner.result_from({'metrics':metrics},0,100,60,100)['passed'])
        metrics['dropped_iterations']['count']=0
        self.assertTrue(runner.result_from({'metrics':metrics},0,100,60,100)['passed'])
        self.assertFalse(runner.result_from({'metrics':metrics},0,100,60,100,readiness_lost=True)['passed'])
        self.assertFalse(runner.result_from({'metrics':metrics},0,100,60,100,integrity=False)['passed'])
        metrics['http_req_duration']['p(95)']=100
        self.assertFalse(runner.result_from({'metrics':metrics},0,100,60,100)['passed'])

    def record(self):
        return dict(namespace='java',deployment='order-service',container='order-service',uid='original',
                    original={},budget={'limits':{'cpu':'500m','memory':'1024Mi'},'requests':{'cpu':'500m','memory':'1024Mi'}})

    def deployment(self,resources):
        return {'metadata':{'uid':'original','resourceVersion':'42'},'spec':{'template':{'spec':{
            'containers':[{'name':'order-service','resources':resources,'env':[{'name':'UNCHANGED','value':'keep'}]}]}}}}

    def test_restore_accepts_equivalent_kubernetes_quantities_and_only_patches_resources(self):
        record=self.record()
        actual=copy.deepcopy(record['budget'])
        for scope in actual: actual[scope]['memory']='1Gi'
        with patch.object(runner,'get',return_value=self.deployment(actual)), patch.object(runner,'command') as call:
            runner.patch(record,restore=True)
        args=call.call_args.args[0]
        operations=json.loads(args[args.index('-p')+1])
        self.assertEqual(operations[0],{'op':'test','path':'/metadata/resourceVersion','value':'42'})
        self.assertEqual(operations[1],{'op':'add','path':'/spec/template/spec/containers/0/resources','value':{}})

    def test_restore_refuses_to_overwrite_a_concurrent_edit(self):
        resources={'limits':{'cpu':'750m','memory':'1Gi'}}
        with patch.object(runner,'get',return_value=self.deployment(resources)), patch.object(runner,'command') as call:
            with self.assertRaisesRegex(RuntimeError,'concurrently'):
                runner.patch(self.record(),restore=True)
            call.assert_not_called()

    def test_partial_failure_does_not_prevent_restoring_other_deployments(self):
        first=self.record(); second=dict(first,deployment='inventory-service')
        with patch.object(runner,'patch',side_effect=[RuntimeError('concurrent edit'),None]) as call:
            with self.assertRaisesRegex(RuntimeError,'Restore needs attention'):
                runner.restore({'records':[first,second]})
        self.assertEqual(call.call_count,2)

    def test_unequal_budgets_are_rejected_before_kubernetes_is_read(self):
        budget={'cpu_millicores':2000,'memory_mib':4096,'wasm':[
            {'deployment':'wasmcloud-host','container':'wasmcloud-host','cpu_millicores':1000,'memory_mib':4096}]}
        with patch.object(runner,'get') as get:
            with self.assertRaisesRegex(ValueError,'CPU allocations'):
                runner.records_for(budget,['wasm'])
            get.assert_not_called()

    def test_inventory_rolling_update_is_rejected_before_resources_are_changed(self):
        budget=json.loads(Path(__file__).with_name('budget.json').read_text())
        deployment=self.deployment({})
        deployment['spec']['strategy']={'type':'RollingUpdate'}
        with patch.object(runner,'get',return_value=deployment), patch.object(runner,'command') as command:
            with self.assertRaisesRegex(ValueError,'requires strategy Recreate'):
                runner.records_for(budget,['java'])
            command.assert_not_called()

    def test_rollout_waits_until_the_old_pod_has_disappeared(self):
        new={'metadata':{'uid':'new'}}
        old={'metadata':{'uid':'old','deletionTimestamp':'now'}}
        with patch.object(runner,'command'), patch.object(runner,'get',return_value=self.deployment({})), \
             patch.object(runner,'pods_for',side_effect=[[old,new],[new]]) as pods, patch.object(runner.time,'sleep') as sleep:
            runner.rollout([self.record()])
            self.assertEqual(pods.call_count,2)
            sleep.assert_called_once_with(2)

    def test_terminating_pods_are_not_ignored_by_budget_integrity_check(self):
        record=self.record()
        with patch.object(runner,'get',return_value=self.deployment(record['budget'])), \
             patch.object(runner,'pods_for',return_value=[{'metadata':{'deletionTimestamp':'now'}},{'metadata':{}}]):
            with self.assertRaisesRegex(RuntimeError,'Expected one live pod'):
                runner.pod_state([record])

    def test_inventory_http_error_fails_preflight_before_order_load(self):
        error=urllib.error.HTTPError('http://inventory',500,'missing table',{},None)
        with patch.object(runner.urllib.request,'urlopen',side_effect=error) as request:
            with self.assertRaisesRegex(RuntimeError,'inventory preflight returned HTTP 500'):
                runner.functional_check('java')
            self.assertEqual(request.call_count,1)

if __name__=='__main__': unittest.main()
