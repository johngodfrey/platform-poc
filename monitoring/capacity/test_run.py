import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import json
import tempfile

spec=importlib.util.spec_from_file_location('capacity',Path(__file__).with_name('run.py'))
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)


class CapacityTests(unittest.TestCase):
    def result(self,**changes):
        data=dict(passed=False,budget_and_pods_unchanged=True,readiness_lost=False,dropped=0)
        data.update(changes);return data

    def test_drops_do_not_prove_application_limit(self):
        self.assertEqual(r.classify(self.result(dropped=5)),'inconclusive_dropped_work')

    def test_readiness_loss_is_unhealthy(self):
        self.assertEqual(r.classify(self.result(readiness_lost=True)),'unhealthy')

    def test_latency_or_error_failure_is_distinct_from_success(self):
        self.assertEqual(r.classify(self.result()),'fails_slo')
        self.assertEqual(r.classify(self.result(passed=True)),'pass')

    def test_api_read_retries_are_bounded(self):
        import subprocess
        error=subprocess.CalledProcessError(1,['kubectl'])
        with patch.object(r.time,'sleep'):
            from unittest.mock import Mock
            read=Mock(side_effect=[error,{'ok':True}])
            self.assertEqual(r.retry_read(read),{'ok':True})
            read=Mock(side_effect=error)
            with self.assertRaises(subprocess.CalledProcessError): r.retry_read(read)
            self.assertEqual(read.call_count,3)

    def test_snapshot_uses_existing_resources_without_mutation(self):
        resources=dict(limits=dict(memory='512Mi'))
        dep=dict(metadata=dict(uid='u'),spec=dict(replicas=1,template=dict(spec=dict(containers=[dict(name='wasmcloud-host',image='tested',resources=resources)]))))
        with patch.object(r.b,'get',return_value=dep):
            records=r.snapshot(['wasm'])
        self.assertEqual(records[0]['budget'],resources)
        self.assertNotIn('cpu',records[0]['budget']['limits'])
        records[0]['budget']['limits']['memory']='other'
        self.assertEqual(resources['limits']['memory'],'512Mi')

    def test_partial_budget_application_enters_restoration_and_retains_error(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            budget=root/'budget.json';budget.write_text(json.dumps(dict(cpu_millicores=2000,memory_mib=4096)))
            record=dict(namespace='wasm',deployment='wasmcloud-host',container='wasmcloud-host',
                        uid='u',original={},budget=dict(limits=dict(cpu='2',memory='4Gi')))
            dep=dict(metadata=dict(uid='u'),spec=dict(template=dict(spec=dict(containers=[
                dict(name='wasmcloud-host',image='tested',resources={})]))))
            with patch.object(r,'ROOT',root), patch.object(r,'physical'), \
                 patch.object(r.sys,'argv',['run.py','wasm','--budget',str(budget)]), \
                 patch.object(r.sys,'platform','test'), patch.object(r.shutil,'which',return_value='/k6'), \
                 patch.object(r.urllib.request,'urlopen',return_value=MagicMock()), \
                 patch.object(r.b,'records_for',return_value=[record]), \
                 patch.object(r.b,'get',return_value=dep), patch.object(r.b,'pods_for'), \
                 patch.object(r.b,'patch',side_effect=RuntimeError('injected partial apply failure')), \
                 patch.object(r.b,'restore') as restore, patch.object(r.b,'wait_functional'):
                with self.assertRaisesRegex(RuntimeError,'injected partial apply failure'):
                    r.main()
                restore.assert_called_once_with(dict(records=[record]))
            report=json.loads(next((root/'.results').glob('*/report.json')).read_text())
            self.assertTrue(report['restored'])
            self.assertFalse(report['completed'])
            self.assertIn('injected partial apply failure',report['error'])


if __name__=='__main__':unittest.main()
