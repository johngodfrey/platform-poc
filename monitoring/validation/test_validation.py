import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import catalog
import run


class ValidationTests(unittest.TestCase):
    def test_bad_performance_is_retained_as_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a';p.mkdir()
            (p/'report.json').write_text(json.dumps(dict(implementations={'java':[dict(status='fail')]})))
            result=catalog.classify(p)
            self.assertEqual(result['role'],'historical_observation')
            self.assertFalse(result['fresh_validation_pool'])

    def test_derived_reports_are_not_independent_trials(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'comparison-example';p.mkdir()
            (p/'report.json').write_text('{}')
            self.assertEqual(catalog.classify(p)['role'],'derived_summary')

    def test_missing_container_samples_are_not_zero(self):
        series=[dict(metric=dict(pod='p',container='a'),values=[[0,'1'],[5,'2'],[10,'3']])]
        r=run.telemetry_stats(series,[('p','a'),('p','b')],0,10)
        self.assertFalse(r['complete']);self.assertEqual(r['n'],0)

    def test_complete_samples_sum_containers_and_ignore_outside_phase(self):
        series=[dict(metric=dict(pod='p',container=name),values=[[-5,'999'],[0,value],[5,value],[10,value]]) for name,value in [('a','1'),('b','2')]]
        r=run.telemetry_stats(series,[('p','a'),('p','b')],0,10)
        self.assertTrue(r['complete']);self.assertEqual(r['median'],3)

    def test_duplicate_series_cannot_double_count_cpu(self):
        series=[dict(metric=dict(pod='p',container='a'),values=[[0,'1']])]*2
        with self.assertRaises(RuntimeError):run.telemetry_stats(series,[('p','a')],0,0)

    def test_invalid_startup_member_excludes_whole_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            trials=[dict(pair=1,implementation='wasm',cache_mode=mode,valid=mode=='cold',first_order_seconds=2) for mode in ['cold','cached']]
            (p/'report.json').write_text(json.dumps(dict(trials=trials)))
            c=object.__new__(run.Campaign)
            c.data=dict(attempts=[dict(suite='startup_cache',reports=[tmp])])
            r=c.aggregate()['wasm']['startup_cache']
            self.assertEqual(r['valid_pairs'],0);self.assertEqual(r['cold']['n'],0)


if __name__=='__main__':unittest.main()
