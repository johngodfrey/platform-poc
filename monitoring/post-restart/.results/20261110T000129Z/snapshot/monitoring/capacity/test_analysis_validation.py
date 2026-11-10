import importlib.util
from pathlib import Path
import unittest
import json
import tempfile

spec=importlib.util.spec_from_file_location('analysis_validation',Path(__file__).with_name('analyse_validation.py'))
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)

class AnalysisTests(unittest.TestCase):
    def test_cadvisor_sample_timestamps_are_not_counter_values(self):
        def rows(periods,throttled,timestamp):
            return {'samples':[{'node':'n','line':f'container_cpu_cfs_{name}_total{{container="order-service",pod="current"}} {value} {timestamp}'} for name,value in [('periods',periods),('throttled_periods',throttled)]]}
        output=a.throttle({'before':rows(100,10,500000),'after':rows(200,40,550000),'current_pods':['current']})
        self.assertEqual(output['order-service']['throttled_period_fraction'],.3)

    def test_followup_does_not_reclassify_failed_candidate_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            failed=root/'failed.json';followup=root/'followup.json'
            candidate={'rate':450,'passed':False,'confirmation':True}
            stable={'rate':400,'passed':True}
            failed.write_text(json.dumps({'completed':True,'restored':True,'applications_healthy':True,'implementations':{'wasm':{'stages':[],'confirmation':candidate}}}))
            followup.write_text(json.dumps({'completed':True,'restored':True,'applications_healthy':True,'implementations':{'wasm':{'stages':[stable]}}}))
            journal={'state':'completed','jobs':[{'name':'pair-1-wasm','report':str(failed)}]}
            (root/'validation.json').write_text(json.dumps(journal))
            (root/'protocol.json').write_text('{}')
            (root/'stability-addendum.json').write_text(json.dumps({'state':'completed','jobs':[{'name':'stability-wasm-fallback','report':str(followup)}]}))
            output=a.derive(root)
            self.assertEqual(len(output['jobs']),2)
            self.assertEqual(len(output['repeats']['wasm']),1)
            self.assertFalse(output['repeats']['wasm'][0]['confirmation']['passed'])
            self.assertEqual(json.loads((root/'validation.json').read_text()),journal)

    def test_missing_container_samples_are_not_counted_as_complete(self):
        telemetry={'load_start':100,'load_end':130,'queries':{'cpu':{'status':'success','data':{'result':[{'metric':{'container':'order-service'},'values':[[100,'1'],[115,'1']]}]}}}}
        result=a.resources(telemetry,'java')
        self.assertEqual(result['cpu']['coverage'],0)
        self.assertIsNone(result['cpu']['mean'])

if __name__=='__main__':unittest.main()
