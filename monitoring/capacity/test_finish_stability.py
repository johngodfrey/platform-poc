import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('finish_stability',Path(__file__).with_name('finish_stability.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)

def report(*rates):
    return {'completed':True,'restored':True,'applications_healthy':True,'implementations':{'wasm':{'stages':[{'rate':r,'passed':True} for r in rates],'confirmation':{'rate':450,'passed':False}}}}

class FollowupTests(unittest.TestCase):
    def test_selects_common_short_step_without_promoting_failed_confirmation(self):
        self.assertEqual(f.common_rate([report(350,400,450),report(350,400),report(350,400)],'wasm',400),400)
        self.assertEqual(f.common_rate([report(350,400),report(350),report(350,400)],'wasm',400),350)

    def test_missing_pair_or_common_rate_is_rejected(self):
        with self.assertRaises(RuntimeError):f.common_rate([report(350)]*2,'wasm',400)
        with self.assertRaises(RuntimeError):f.common_rate([report(350),report(400),report(350)],'wasm',400)

    def test_unrestored_repeat_is_rejected(self):
        failed=report(350);failed['restored']=False
        with self.assertRaises(RuntimeError):f.common_rate([report(350),failed,report(350)],'wasm',400)

if __name__=='__main__':unittest.main()
