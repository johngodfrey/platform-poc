import unittest
from types import SimpleNamespace
from unittest.mock import patch
import run as runner


class PolicyTests(unittest.TestCase):
    def test_latency_needs_two_adjacent_distinct_high_windows(self):
        p=runner.LatencyPolicy(.2)
        self.assertIsNone(p.decide(10,1,100,signal=dict(window=1,p95_ms=10)))
        self.assertIsNone(p.decide(.01,1,115,signal=dict(window=2,p95_ms=600)))
        self.assertIsNone(p.decide(.01,1,116,signal=dict(window=2,p95_ms=600)))
        self.assertEqual(p.decide(.01,1,130,signal=dict(window=3,p95_ms=600)),'up')

    def test_latency_gap_missing_or_low_window_breaks_sequence(self):
        for kind in ['gap','missing','low']:
            p=runner.LatencyPolicy(.2)
            p.decide(.01,1,100,signal=dict(window=1,p95_ms=600))
            if kind=='missing':p.decide(.01,1,110,signal=None)
            if kind=='low':p.decide(.01,1,110,signal=dict(window=2,p95_ms=500))
            self.assertIsNone(p.decide(.01,1,130,signal=dict(window=3,p95_ms=600)))
            self.assertEqual(p.decide(.01,1,145,signal=dict(window=4,p95_ms=600)),'up')

    def test_latency_mode_keeps_cpu_scale_in_and_missing_cpu_resets_hold(self):
        p=runner.LatencyPolicy(.2)
        for i in [1,2]:p.decide(.01,1,i*15,signal=dict(window=i,p95_ms=600))
        self.assertIsNone(p.decide(.2,2,60))
        self.assertIsNone(p.decide(None,2,90))
        self.assertIsNone(p.decide(.2,2,120))
        self.assertIsNone(p.decide(.2,2,179))
        self.assertEqual(p.decide(.2,2,180),'down')

    def test_latency_signal_waits_for_settled_cohort_and_rejects_bad_data(self):
        args=SimpleNamespace(window=15,latency_settle=20)
        def response(count=100,stamp=1000):
            values={'latency_ms_p95':600,'completed_total':count,'accepted_total':count,'failures_total':0}
            return dict(status='success',data=dict(result=[dict(metric={'__name__':'k6_autoscale_'+name},value=[stamp,str(value)]) for name,value in values.items()]))
        with patch.object(runner.urllib.request,'urlopen'),patch.object(runner.time,'time',return_value=1000):
            with patch.object(runner.json,'load',return_value=response()):
                signal=runner.latency_signal('test',245,args)
                self.assertEqual((signal['window'],signal['end_s']),(14,225))
            for invalid in [response(99),response(stamp=954),dict(status='success',data=dict(result=[]))]:
                with patch.object(runner.json,'load',return_value=invalid):
                    with self.assertRaises(RuntimeError):runner.latency_signal('test',245,args)

    def test_queued_downscale_preserves_first_upscale_serving_time(self):
        data=dict(observed=2,pending=True,actions=[dict(action='up',ready_time=25)])
        self.assertFalse(runner.mark_extra_serving(data,400))
        self.assertEqual(data['actions'][0]['ready_time'],25)
        self.assertTrue(data['pending'])
        data['actions'].append(dict(action='down'))
        self.assertFalse(runner.mark_extra_serving(data,401))
        self.assertNotIn('ready_time',data['actions'][-1])

    def test_new_upscale_records_serving_once(self):
        data=dict(observed=2,pending=True,actions=[dict(action='up')])
        self.assertTrue(runner.mark_extra_serving(data,25))
        self.assertFalse(data['pending'])
        self.assertFalse(runner.mark_extra_serving(data,26))
        self.assertEqual(data['actions'][0]['ready_time'],25)

    def test_cpu_not_elapsed_time_triggers_up(self):
        policy=runner.Policy(.2)
        self.assertIsNone(policy.decide(.1,1,1000))
        self.assertEqual(policy.decide(.3,1,1001),'up')

    def test_pending_action_does_not_issue_another_command(self):
        policy=runner.Policy(.2)
        self.assertIsNone(policy.decide(.8,1,0,pending=True))
        self.assertEqual(policy.decide(.8,1,1),'up')
        self.assertIsNone(policy.decide(.8,1,20))

    def test_low_cpu_must_be_continuous_and_uses_per_unit_average(self):
        policy=runner.Policy(.2,hold=60);policy.decide(.8,1,0)
        self.assertIsNone(policy.decide(.2,2,20))
        self.assertIsNone(policy.decide(.5,2,50))
        self.assertIsNone(policy.decide(.2,2,60))
        self.assertIsNone(policy.decide(.2,2,119))
        self.assertEqual(policy.decide(.2,2,120),'down')
        self.assertIsNone(policy.decide(10,1,121))

    def test_invalid_cpu_is_rejected(self):
        for value in [float('nan'),float('inf'),-1]:
            with self.assertRaises(ValueError):runner.Policy(.2).decide(value,1,0)

    def test_recovery_requires_full_throughput_in_two_windows(self):
        rows=[dict(phase='burst',start_s=i*15,end_s=(i+1)*15,p95_ms=100,failures=0,accepted=6000,completed=6000) for i in range(3)]
        self.assertEqual(runner.recovery_after(rows,0,'burst',400,15,500),30)
        rows[1]['accepted']=100;rows[1]['completed']=100
        self.assertIsNone(runner.recovery_after(rows,0,'burst',400,15,500))

    def test_baseline_gate_requires_two_healthy_complete_cohorts(self):
        args=SimpleNamespace(warmup=180,window=15,baseline_rate=100,p95_ms=500)
        def responses(values):return [dict(data=dict(result=[dict(value=[0,str(value)])])) for value in values]
        with patch.object(runner.urllib.request,'urlopen'),patch.object(runner.json,'load',side_effect=responses([1500,1500,0,100]*2)):
            self.assertEqual([r['window'] for r in runner.baseline_gate('test',args)],[10,11])
        with patch.object(runner.urllib.request,'urlopen'),patch.object(runner.json,'load',side_effect=responses([1500,1499,1,100])):
            with self.assertRaises(runner.CalibrationError):runner.baseline_gate('test',args)

    def test_incomplete_final_warmup_waits_for_export_instead_of_accepting(self):
        args=SimpleNamespace(warmup=180,window=15,baseline_rate=100,p95_ms=500)
        responses=[dict(data=dict(result=[dict(value=[0,str(v)])])) for v in [1480,1480,0,10]]
        with patch.object(runner.urllib.request,'urlopen'),patch.object(runner.json,'load',side_effect=responses):
            with self.assertRaisesRegex(RuntimeError,'Waiting for final warmup'):runner.baseline_gate('test',args)


if __name__=='__main__':unittest.main()
