import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('phases',Path(__file__).with_name('analyse_phases.py'))
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)

class PhaseTests(unittest.TestCase):
    def test_phases_exclude_ramps_and_wait_for_slowest_ready_resource(self):
        settings=dict(window=15,warmup=60,baseline=30,before_scale=45,after_scale=180,recovery=30)
        results=[dict(scale_request_s=136.1,scale_accepted_s=138.5,capacity_ready_seconds=40.1),dict(scale_request_s=137,scale_accepted_s=137.7,capacity_ready_seconds=23.3)]
        self.assertEqual(a.phase_bounds(settings,results),dict(before=(105,135),transition=(135,180),after=(180,315),cooldown=(330,345)))

    def test_mean_is_weighted_by_requests_and_quantiles_are_not_pooled(self):
        rows=[dict(start_s=0,end_s=15,completed=1,accepted=1,failures=0,mean_ms=100,median_ms=100,p95_ms=100,passed=False),dict(start_s=15,end_s=30,completed=9,accepted=9,failures=0,mean_ms=10,median_ms=10,p95_ms=10,passed=False)]
        result=a.summarise(rows,400)
        self.assertEqual(result['mean_latency_ms'],19)
        self.assertEqual(result['median_window_p95_ms'],55)
        self.assertNotIn('phase_p95_ms',result)
        self.assertEqual(result['accepted_orders_per_start_second'],10/30)

    def test_latency_alone_does_not_pass_delivery_gate(self):
        metrics={'scaleout_completed{window:0}':dict(count=5000),'scaleout_accepted{window:0}':dict(count=5000),'scaleout_failures{window:0}':dict(count=0),'scaleout_latency_ms{window:0}':dict(avg=10,med=5,**{'p(95)':20,'max':100})}
        self.assertFalse(a.cohorts(metrics,0,15,15,400,500)[0]['passed'])
        metrics['scaleout_completed{window:0}']['count']=5999;metrics['scaleout_accepted{window:0}']['count']=5999
        self.assertTrue(a.cohorts(metrics,0,15,15,400,500)[0]['passed'])

    def test_rejects_inconsistent_order_counters(self):
        metrics={'scaleout_completed{window:0}':dict(count=5),'scaleout_accepted{window:0}':dict(count=4),'scaleout_failures{window:0}':dict(count=0),'scaleout_latency_ms{window:0}':dict(avg=10,med=5,**{'p(95)':20,'max':100})}
        with self.assertRaises(ValueError):a.cohorts(metrics,0,15,15,400,500)

if __name__=='__main__':unittest.main()
