import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('validate_capacity',Path(__file__).with_name('validate.py'))
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)

def report(rate,passed=True,upper=300):
    return {'implementations':{'java':{'confirmation':{'rate':rate,'passed':passed},'stages':[{'rate':rate,'passed':True},{'rate':upper,'passed':False}]}}}

class ValidationTests(unittest.TestCase):
    def test_tuning_selected_only_if_confirmed_rate_not_worse(self):
        self.assertEqual(v.choose_budget(report(200),report(300,upper=350)),'tuned')
        self.assertEqual(v.choose_budget(report(200),report(200)),'tuned')
        self.assertEqual(v.choose_budget(report(200),report(150)),'original')
        self.assertEqual(v.choose_budget(report(200),report(300,False)),'original')

    def test_start_marker_supports_escaped_and_plain_k6_logs(self):
        self.assertEqual(v.log_start(r'msg="{\"event\":\"demo_started\",\"start_ms\":1234500}"'),1234.5)
        self.assertEqual(v.log_start('{"event":"demo_started","start_ms":1234500}'),1234.5)
        with self.assertRaises(ValueError):v.log_start('no marker')

    def test_bracket_retains_failed_upper_rate(self):
        self.assertEqual(v.candidate_rates(report(250)),[250,300])

    def test_failed_confirmation_cannot_define_repeat_bracket(self):
        with self.assertRaises(RuntimeError):v.candidate_rates(report(250,False))

if __name__=='__main__':unittest.main()
