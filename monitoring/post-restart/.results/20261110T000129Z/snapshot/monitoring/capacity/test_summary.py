import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('summary', Path(__file__).with_name('summarize.py'))
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class SummaryTests(unittest.TestCase):
    def test_short_passing_step_is_not_sustained_confirmation(self):
        report = dict(implementations=dict(java=dict(stages=[dict(rate=400, passed=True)])))
        self.assertIsNone(s.confirmed(report, 'java'))
        report['implementations']['java']['confirmation'] = dict(rate=400, passed=False, confirmation=True, seconds=300)
        self.assertIsNone(s.confirmed(report, 'java'))
        report['implementations']['java']['confirmation'].update(passed=True, seconds=120)
        self.assertIsNone(s.confirmed(report, 'java'))

    def test_different_latency_targets_are_not_compared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);runs = []
            for impl, latency in [('java', 100), ('wasm', 500)]:
                target = root/impl;target.mkdir()
                settings = {key: 1 for key in s.FIELDS};settings['p95_ms'] = latency
                (target/'report.json').write_text(json.dumps(dict(completed=True,restored=True,
                    applications_healthy=True,settings=settings,budget=dict(cpu_millicores=2000))))
                runs.append(dict(implementation=impl,reports=[str(target)]))
            (root/'series.json').write_text(json.dumps(dict(state='completed',runs=runs)))
            with self.assertRaisesRegex(ValueError, 'protocols differ'):
                s.load_reports(root)

    def test_unfinished_series_cannot_be_presented_as_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'series.json').write_text(json.dumps(dict(state='running',runs=[])))
            with self.assertRaisesRegex(ValueError, 'not completed'):
                s.load_reports(root)

    def test_failed_higher_followup_does_not_replace_confirmed_bound(self):
        def source(rate, passed):
            return dict(report=dict(implementations=dict(wasm=dict(confirmation=dict(
                rate=rate, passed=passed, confirmation=True, seconds=300)))))
        base = source(400, True)
        base['extras'] = [source(500, False)]
        self.assertEqual(s.best_confirmed(base, 'wasm')['rate'], 400)
        base['extras'].append(source(450, True))
        self.assertEqual(s.best_confirmed(base, 'wasm')['rate'], 450)

    def test_duplicate_followup_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);runs = []
            for impl in ('java', 'wasm'):
                target = root/impl;target.mkdir()
                report = dict(completed=True, restored=True, applications_healthy=True,
                    settings={key:1 for key in s.FIELDS}, budget=dict(cpu_millicores=2000),
                    implementations={impl:dict(stages=[])})
                (target/'report.json').write_text(json.dumps(report))
                runs.append(dict(implementation=impl, reports=[str(target)]))
            (root/'series.json').write_text(json.dumps(dict(state='completed', runs=runs)))
            with self.assertRaisesRegex(ValueError, 'Duplicate source'):
                s.load_reports(root, [root/'java'])


if __name__ == '__main__':
    unittest.main()
