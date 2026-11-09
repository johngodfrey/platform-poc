import importlib.util
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('repeat',Path(__file__).with_name('repeat.py'))
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)


class RepeatTests(unittest.TestCase):
    def pair(self,number,java,wasm,valid=True):
        def values(value):return {key:value for key,_ in r.METRICS}
        return dict(pair=number,order='java-first' if number%2 else 'wasm-first',path='pair-'+str(number),valid=valid,implementations=dict(java=dict(values(java),already_healthy=False,recovery_seconds=None),wasm=dict(values(wasm),already_healthy=False,recovery_seconds=None)))

    def test_invalid_pair_is_excluded_for_both_implementations(self):
        series=dict(pairs=[self.pair(1,10,20),self.pair(2,1000,1,False),self.pair(3,30,40)])
        stats=r.aggregates(series)
        self.assertEqual(stats['java']['new_pod_order_accepted_seconds'],dict(n=2,median=20,minimum=10,maximum=30))
        self.assertEqual(stats['wasm']['new_pod_order_accepted_seconds']['median'],30)

    def test_empty_and_completed_reports_render_and_preserve_all_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);series=dict(status='running',settings=dict(pairs=5),pairs=[])
            r.write_report(path,series);self.assertIn('refresh', (path/'report.html').read_text())
            series.update(status='complete',pairs=[self.pair(1,10,20),self.pair(2,30,40,False)])
            r.write_report(path,series);page=(path/'report.html').read_text();self.assertNotIn('http-equiv="refresh"',page);self.assertIn('pair-2/report.html',page);self.assertIn('<circle',page)

if __name__=='__main__':unittest.main()
