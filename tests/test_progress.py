import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import manager


class BuildProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = pathlib.Path(self.temp.name)
        (root / 'deployment').mkdir()
        (root / 'deployment/.env').write_text('')
        self.manager = manager.Manager({'deployment': str(root / 'deployment'),
            'core': str(root / 'core'), 'packaging': str(root / 'packaging'),
            'state': str(root / 'state'), 'backups': str(root / 'backups')})
        self.manager.operation = {'id': 'test', 'action': 'build', 'status': 'running'}
        self.manager.log_path = root / 'build.log'

    def test_stream_persists_percentage_and_later_stages(self):
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.StringIO('#12 4.2 [ 42%] Building CXX object bot.cpp.o\n')
        process.wait.return_value = 0
        with patch('manager.subprocess.Popen', return_value=process):
            self.manager.run(['docker', 'build'], stream=True)
        state = json.loads(self.manager.state_path.read_text())
        self.assertEqual(state['active']['progress'], {'stage': 'Compiling', 'percent': 42})
        self.manager.build_output('#12 4.3 [ 39%] Built target game')
        self.assertEqual(self.manager.operation['progress']['percent'], 42)
        self.manager.build_output('#12 5.0 [100%] Built target mangosd')
        self.assertEqual(self.manager.operation['status'], 'running')
        self.manager.build_output('#12 5.1 -- Installing: /opt/turtle/bin/mangosd')
        self.assertEqual(self.manager.operation['progress'], {'stage': 'Packaging image', 'percent': None})
        self.manager.build_output('#13 exporting to image')
        self.assertEqual(self.manager.operation['progress']['stage'], 'Exporting image')

    def test_cached_build_and_unrelated_output(self):
        self.manager.build_progress('Preparing compiler and cache')
        for line in ['#12 CACHED', 'download 99%', '[999%] Building bad', '[50%] unrelated']:
            self.manager.build_output(line)
        self.assertIsNone(self.manager.operation['progress']['percent'])
        self.manager.build_output('#13 exporting to image')
        self.assertEqual(self.manager.operation['progress']['stage'], 'Exporting image')
        self.manager.operation['action'] = 'backup'
        self.manager.build_output('[80%] Building CXX object x.o')
        self.assertEqual(self.manager.operation['progress']['stage'], 'Exporting image')

    def test_failed_stream_preserves_last_progress(self):
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.StringIO('[25%] Building CXX object broken.cpp.o\nerror: failed\n')
        process.wait.return_value = 1
        with patch('manager.subprocess.Popen', return_value=process):
            with self.assertRaises(RuntimeError):
                self.manager.run(['docker', 'build'], stream=True)
        self.assertEqual(self.manager.operation['progress']['percent'], 25)


if __name__ == '__main__':
    unittest.main()
