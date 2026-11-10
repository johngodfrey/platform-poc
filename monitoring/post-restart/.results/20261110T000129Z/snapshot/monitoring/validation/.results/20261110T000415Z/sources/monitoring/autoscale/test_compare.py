import copy
import unittest
import compare


class ProvenanceTests(unittest.TestCase):
    def source(self,impl):
        return dict(status='complete',restored=True,settings={key:1 for key in compare.POLICY_KEYS},source_sha256={'driver':'same'},trials=[dict(implementation=impl,pair=1,baseline_gate=[dict(passed=True)],cpu_query='rate(cpu[60s])')])

    def test_failed_series_requires_explicit_recovery_provenance(self):
        java=self.source('java');java.update(status='failed',operational_recovery='Original configuration verified')
        sources=[('java',java),('wasm',self.source('wasm'))]
        with self.assertRaises(RuntimeError):compare.combine(sources)
        result=compare.combine(sources,include_recovered=True)
        self.assertEqual(len(result['trials']),2)
        self.assertEqual(result['sources'][0]['status'],'failed')

    def test_policy_or_source_changes_block_comparison(self):
        for change in ['policy','hash','trigger','headroom']:
            wasm=self.source('wasm')
            if change=='policy':wasm['settings']['vus']=999
            elif change=='trigger':wasm['settings']['trigger']='latency'
            elif change=='headroom':wasm['settings']['cpu_headroom']=1.25
            else:wasm['source_sha256']['driver']='changed'
            with self.assertRaises(RuntimeError):compare.combine([('java',self.source('java')),('wasm',wasm)])


if __name__=='__main__':unittest.main()
