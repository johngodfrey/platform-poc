import unittest

import analyse
import reconstruct_memory


class AnalysisTests(unittest.TestCase):
    def cpu(self, values, expected=('a', 'b')):
        return dict(complete=True, expected_nodes=list(expected), coverage={n: 5 for n in expected},
                    samples=[dict(metric=dict(node=n), values=[[t, str(v)] for t in range(5)])
                             for n, v in values.items()])

    def test_shared_vm_gate_sums_node_consumption(self):
        self.assertFalse(analyse.shared_cpu(self.cpu({'a': .35, 'b': .35}))['headroom'])
        self.assertTrue(analyse.shared_cpu(self.cpu({'a': .10, 'b': .20}))['headroom'])

    def test_missing_node_and_exact_ceiling_rejected(self):
        self.assertFalse(analyse.shared_cpu(self.cpu({'a': .10}))['headroom'])
        self.assertFalse(analyse.shared_cpu(self.cpu({'a': .30, 'b': .30}))['headroom'])

    def observation(self, name, time=100, ready=True):
        return dict(time=time, namespaces=dict(wasm=[dict(name=name, ready=ready, deleting=False,
            containers=[dict(name='wasmcloud-host', resources=dict(limits=dict(memory='512Mi')))])]))

    def test_memory_pins_require_matching_live_allocation(self):
        observations=[self.observation('live'), self.observation('old', time=20)]
        self.assertEqual(reconstruct_memory.choose_pods(observations, 'wasm', 512, 90, 110)[0],
                         [('live', 'wasmcloud-host')])
        self.assertEqual(reconstruct_memory.choose_pods(observations, 'wasm', 384, 90, 110)[0], [])
        self.assertEqual(reconstruct_memory.choose_pods([self.observation('unready', ready=False)],
                         'wasm', 512, 90, 110)[0], [])

    def test_replacement_during_memory_interval_rejected(self):
        with self.assertRaises(RuntimeError):
            reconstruct_memory.choose_pods([self.observation('old'), self.observation('new')],
                                            'wasm', 512, 90, 110)


if __name__ == '__main__':
    unittest.main()
