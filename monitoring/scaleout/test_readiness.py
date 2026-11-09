import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('readiness',Path(__file__).with_name('readiness.py'))
q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)


def pod(uid='new',ready=True):
    return dict(metadata=dict(uid=uid,name='pod-'+uid),status=dict(conditions=[dict(type='Ready',status='True' if ready else 'False')],containerStatuses=[dict(name='runtime',ready=ready,imageID='patched-digest')]))


class ReadinessTests(unittest.TestCase):
    def monitor(self,impl='java'):
        state=dict(java=dict(baseline_pod_uids=['old'],images={'runtime':'patched-digest'},pod_selector='service=order'),wasm=dict(host=dict(baseline_pod_uid='old',images={'runtime':'patched-digest'},nonce='nonce')))
        m=q.Monitor(state,impl,Path('/tmp'),['kubectl'],Mock());m.arm();return m

    def test_container_running_without_pod_ready_is_not_ready(self):
        p=pod();p['status']['conditions'][0]['status']='False'
        self.assertFalse(q.pod_ready(p));p['status']['conditions'][0]['status']='True';p['metadata']['deletionTimestamp']='now';self.assertFalse(q.pod_ready(p))

    def test_both_require_201_and_the_application_success_body(self):
        for impl,body in [('java','Order Placed Successfully'),('wasm','201 Created')]:
            self.assertTrue(q.accepted(impl,201,body));self.assertFalse(q.accepted(impl,200,body));self.assertFalse(q.accepted(impl,201,'invalid'))

    def test_same_direct_pod_protocol_and_timeout_for_both(self):
        for impl,body in [('java','Order Placed Successfully'),('wasm','201 Created')]:
            m=self.monitor(impl);m.request=Mock(side_effect=[(200,json.dumps(dict(items=[pod('old'),pod()]))),(201,body)])
            m.sample();self.assertTrue(m.proven('new'));call=m.request.call_args
            self.assertIn('/pods/pod-new:',call.args[0]);self.assertIn('/proxy/',call.args[0]);self.assertEqual(call.kwargs['timeout'],10)
            self.assertNotIn('order_accepted_seconds',m.snapshot()['pods']['old'])

    def test_native_readiness_does_not_count_as_functional_readiness(self):
        m=self.monitor();m.request=Mock(side_effect=[(200,json.dumps(dict(items=[pod('old'),pod()]))),(500,'error')]);m.sample()
        self.assertFalse(m.proven('new'));self.assertEqual(m.ready_count(),1);self.assertIsNotNone(q.timings(m.snapshot())['new_pod_ready_seconds']);self.assertIsNone(q.timings(m.snapshot())['new_pod_order_accepted_seconds'])

    def test_rejects_different_image_and_records_baseline_replacement(self):
        m=self.monitor();p=pod();p['status']['containerStatuses'][0]['imageID']='wrong';m.request=Mock(return_value=(200,json.dumps(dict(items=[p]))));m.sample()
        self.assertFalse(m.proven('new'));self.assertTrue(m.snapshot()['baseline_changed']);self.assertTrue(m.snapshot()['pods']['new']['image_mismatch']);self.assertEqual(m.request.call_count,1)

    def test_failed_probes_are_retained_and_a_later_success_is_timed(self):
        m=self.monitor();pods=json.dumps(dict(items=[pod('old'),pod()]));m.request=Mock(side_effect=[(200,pods),(503,'no'),(200,pods),(201,'Order Placed Successfully')]);m.sample();m.sample()
        entry=m.snapshot()['pods']['new'];self.assertEqual([p['accepted'] for p in entry['probe_attempts']],[False,True]);self.assertGreaterEqual(entry['order_accepted_seconds'],entry['pod_ready_seconds'])

    def test_baseline_remains_counted_while_new_pod_order_is_pending(self):
        m=self.monitor()
        def request(path,**kwargs):
            if kwargs.get('data') is not None:
                self.assertEqual(m.ready_count(),1)
                return 201,'Order Placed Successfully'
            return 200,json.dumps(dict(items=[pod(),pod('old')]))
        m.request=Mock(side_effect=request);m.sample();self.assertEqual(m.ready_count(),2)

    def test_multiple_new_pods_require_all_not_the_fastest(self):
        data=dict(pods={'a':dict(baseline=False,pod_ready_seconds=1,order_accepted_seconds=2),'b':dict(baseline=False,pod_ready_seconds=3)})
        self.assertIsNone(q.timings(data)['new_pod_order_accepted_seconds']);data['pods']['b']['order_accepted_seconds']=5;self.assertEqual(q.timings(data)['new_pod_order_accepted_seconds'],5)

if __name__=='__main__':unittest.main()
