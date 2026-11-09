import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('series', Path(__file__).with_name('run_series.py'))
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class SeriesTests(unittest.TestCase):
    def exercise(self, code=0, restored=True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guard = Mock()
            calls = []
            def spawn(command, **kwargs):
                calls.append(command)
                if command[0] == 'caffeinate':
                    return guard
                impl = command[2]
                result = root/'.results'/impl
                result.mkdir()
                (result/'report.json').write_text(json.dumps(dict(completed=True,
                    restored=restored, applications_healthy=True)))
                return Mock(returncode=code, poll=Mock(return_value=code))
            with patch.object(s, 'ROOT', root), patch.object(s.sys, 'platform', 'darwin'), \
                 patch.object(s.subprocess, 'Popen', side_effect=spawn):
                if code or not restored:
                    with self.assertRaises(RuntimeError):
                        s.main()
                else:
                    s.main()
            guard.terminate.assert_called_once()
            guard.wait.assert_called_once()
            self.assertEqual(calls[0], ['caffeinate', '-i', '-s'])
            rows = json.loads(next((root/'.results').glob('series-*/series.json')).read_text())
            return calls, rows

    def test_guard_covers_both_runs_and_cleanup(self):
        calls, rows = self.exercise()
        self.assertEqual([c[2] for c in calls[1:]], ['java', 'wasm'])
        self.assertEqual(rows['state'], 'completed')

    def test_failed_child_prevents_peer_load(self):
        calls, rows = self.exercise(code=1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(rows['state'], 'failed')

    def test_unrestored_child_prevents_peer_load(self):
        calls, rows = self.exercise(restored=False)
        self.assertEqual(len(calls), 2)
        self.assertEqual(rows['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
