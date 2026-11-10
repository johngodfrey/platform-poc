import copy
import unittest
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import run as runner


class ReplicaGuards(unittest.TestCase):
    def setUp(self):
        self.record=dict(namespace='wasm',name='wasmcloud-host',uid='deployment-uid')
        self.state=dict(records=[self.record],hostconfig=dict(uid='config-uid',spec=dict(hostReplicas=1,image='patched-image')))
        self.config=dict(metadata=dict(uid='config-uid',resourceVersion='10'),spec=dict(hostReplicas=1,image='patched-image'))

    def test_zero_uses_hostconfig_and_identity_checks(self):
        with patch.object(runner,'assert_template'),patch.object(runner.s,'read',return_value=self.config),patch.object(runner.subprocess,'run') as command:
            runner.patch_replicas(self.state,'wasm',0)
        args=command.call_args.args[0]
        self.assertIn('wasmcloudhostconfig',args)
        operations=runner.json.loads(args[args.index('-p')+1])
        self.assertEqual(operations[-1],dict(op='replace',path='/spec/hostReplicas',value=0))
        self.assertEqual(operations[0]['value'],'config-uid')
        self.assertEqual(operations[1]['value'],'10')

    def test_concurrent_edits_abort_without_mutation(self):
        for change in [dict(hostReplicas=2),dict(image='different-image')]:
            config=copy.deepcopy(self.config);config['spec'].update(change)
            with patch.object(runner,'assert_template'),patch.object(runner.s,'read',return_value=config),patch.object(runner.subprocess,'run') as command:
                with self.assertRaises(RuntimeError):runner.patch_replicas(self.state,'wasm',0)
                command.assert_not_called()

    def test_success_requires_application_body_and_status(self):
        for implementation,body in [('java','Order Placed Successfully'),('wasm','201 Created')]:
            self.assertTrue(runner.s.q.accepted(implementation,201,body))
            self.assertFalse(runner.s.q.accepted(implementation,200,body))
            self.assertFalse(runner.s.q.accepted(implementation,201,'unrelated response'))

    def test_prepared_java_requires_no_replicas_and_no_pods(self):
        record=dict(namespace='java',name='inventory-service')
        for replicas,pods in [(1,[]),(0,[dict(metadata={})])]:
            with tempfile.TemporaryDirectory() as directory,patch.object(runner,'assert_template',return_value=dict(spec=dict(replicas=replicas))),patch.object(runner,'patch_replicas') as mutate:
                proxy=SimpleNamespace(pods=lambda _:pods)
                with self.assertRaisesRegex(RuntimeError,'still running'):
                    runner.trial(dict(records=[record]),'java',1,Path(directory)/'trial',proxy,SimpleNamespace(zero_prepared=True))
                mutate.assert_not_called()


if __name__=='__main__':unittest.main()
