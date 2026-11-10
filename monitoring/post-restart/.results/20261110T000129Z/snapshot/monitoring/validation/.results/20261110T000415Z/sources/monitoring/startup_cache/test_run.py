import copy
import unittest
from unittest.mock import patch
import run as runner
import analyse


class CacheTests(unittest.TestCase):
    def test_cold_rejects_any_existing_images(self):
        with patch.object(runner,'images',return_value=[dict(id='existing')]):
            with self.assertRaises(RuntimeError):runner.cache_audit('node',[],True)

    def test_cached_requires_every_app_image(self):
        records=[dict(template=dict(spec=dict(containers=[dict(image='registry/app@sha256:abc')])))]
        with patch.object(runner,'images',return_value=[]):
            with self.assertRaises(RuntimeError):runner.cache_audit('node',records,False)
        with patch.object(runner,'images',return_value=[dict(repoDigests=['registry/app@sha256:abc'])]):
            self.assertFalse(runner.cache_audit('node',records,False)['cold'])

    def test_placement_preserves_existing_constraints(self):
        original=dict(spec=dict(nodeSelector=dict(existing='value'),tolerations=[dict(key='old')],containers=[]))
        before=copy.deepcopy(original);result=runner.placement(original,'temporary-worker','owner-token')
        self.assertEqual(original,before)
        self.assertEqual(result['spec']['nodeSelector'],{'existing':'value','kubernetes.io/hostname':'temporary-worker'})
        self.assertEqual(result['spec']['tolerations'][-1]['value'],'owner-token')

    def test_concurrent_template_edit_blocks_patch(self):
        obj=dict(metadata=dict(uid='uid',resourceVersion='1'),spec=dict(template={'changed':True}))
        with patch.object(runner.z.s,'read',return_value=obj),patch.object(runner,'command') as mutate:
            with self.assertRaises(RuntimeError):runner.guarded_patch('deployment','java','app','uid','/spec/template',{},dict(test=True))
            mutate.assert_not_called()

    def test_analysis_excludes_both_members_when_one_is_invalid(self):
        rows=[dict(implementation='wasm',pair=1,cache_mode=mode,valid=mode=='cold') for mode in ['cold','cached']]
        selected,excluded=analyse.select_pairs([('raw-report',dict(trials=rows))])
        self.assertEqual(selected,[])
        self.assertEqual(len(excluded),1)

    def test_analysis_keeps_same_numbered_pairs_from_different_runs(self):
        rows=[dict(implementation='wasm',pair=1,cache_mode=mode,valid=True) for mode in ['cold','cached']]
        selected,excluded=analyse.select_pairs([(source,dict(trials=rows)) for source in ['first','second']])
        self.assertEqual([row['pair'] for row in selected],[1,1,2,2])
        self.assertEqual(excluded,[])


if __name__=='__main__':unittest.main()
