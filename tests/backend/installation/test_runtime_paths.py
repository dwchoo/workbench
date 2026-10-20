import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest


class RuntimePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wi-runtime-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runtime = self.base / 'run'
        self.runtime.mkdir(mode=0o700)
        self.env = {'XDG_RUNTIME_DIR': str(self.runtime), 'HOME': str(self.base)}

    def test_project_runtime_is_private_short_and_durable_layout_remains_compatible(self):
        from workbench.backend.paths import DataLayout
        from workbench.backend.runtime_paths import resolve_runtime_root, ensure_runtime_root
        data = self.base / ('deep-' * 40) / '.wb'
        identity = '782839a6-1056-4051-aed1-48dc01482918'
        runtime = resolve_runtime_root(data, identity, self.env)
        self.assertFalse(runtime.exists())
        self.assertTrue(runtime.is_relative_to(self.runtime))
        layout = DataLayout(data, runtime_root=runtime)
        self.assertEqual(layout.tasks, data / 'tasks.sqlite3')
        self.assertEqual(layout.lock, data / 'backend.lock')
        self.assertEqual(layout.ui_socket, runtime / 'ui.sock')
        layout.check_socket_paths()
        ensure_runtime_root(runtime, data, identity)
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)
        with socket.socket(socket.AF_UNIX) as stream:
            stream.bind(str(layout.ui_socket))
        self.assertEqual(DataLayout(data).ui_socket, data / 'ui.sock')
        self.assertNotEqual(runtime, resolve_runtime_root(data, '122a4448-28af-4086-9138-8d069d6ccb18', self.env))

    def test_direct_runtime_preparation_refuses_unsafe_ancestor_and_collision(self):
        from workbench.backend.runtime_paths import resolve_runtime_root, ensure_runtime_root
        from workbench.backend.paths import DataDirError
        data = self.base / 'state'
        runtime = resolve_runtime_root(data, 'project-A', self.env)
        ensure_runtime_root(runtime, data, 'project-A')
        with self.assertRaisesRegex(DataDirError, 'collision'):
            ensure_runtime_root(runtime, data, 'project-B')
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir(mode=0o700)
        link = self.base / 'linked'
        link.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(DataDirError):
            ensure_runtime_root(link / 'omp-workbench' / 'namespace', data, 'project-A')
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_long_or_missing_runtime_uses_short_fallback_and_unsafe_paths_fail(self):
        from workbench.backend.runtime_paths import resolve_runtime_root
        from workbench.backend.paths import DataDirError
        deep = self.base / ('runtime-' * 22)
        deep.mkdir(mode=0o700)
        result = resolve_runtime_root(self.base / 'data', 'fixed-project', {'XDG_RUNTIME_DIR': str(deep)})
        self.assertEqual(result.parent, Path('/tmp') / f'omp-workbench-{os.geteuid()}')
        self.assertLessEqual(len(os.fsencode(result / 'bridge.sock')), 107)
        self.assertFalse(result.exists())
        self.assertEqual(result, resolve_runtime_root(self.base / 'data', 'fixed-project',
                                                      {'XDG_RUNTIME_DIR': 'relative-ignored'}))
        self.runtime.chmod(0o755)
        with self.assertRaises(DataDirError):
            resolve_runtime_root(self.base / 'data', 'fixed-project', self.env)
        self.runtime.chmod(0o700)
        namespace = resolve_runtime_root(self.base / 'data', 'fixed-project', self.env)
        namespace.parent.mkdir(mode=0o700)
        namespace.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(DataDirError):
            resolve_runtime_root(self.base / 'data', 'fixed-project', self.env)

    def test_runtime_fifo_marker_is_rejected_without_blocking(self):
        from workbench.backend.runtime_paths import resolve_runtime_root
        runtime = resolve_runtime_root(self.base / 'data', 'project-A', self.env)
        runtime.mkdir(parents=True, mode=0o700)
        runtime.parent.chmod(0o700)
        os.mkfifo(runtime / 'runtime.json', mode=0o600)
        code = """import sys
from pathlib import Path
from workbench.backend.runtime_paths import ensure_runtime_root
from workbench.backend.paths import DataDirError
try:
    ensure_runtime_root(Path(sys.argv[1]), Path(sys.argv[2]), 'project-A')
except DataDirError:
    raise SystemExit(0)
raise SystemExit(4)
"""
        result = subprocess.run([sys.executable, '-c', code, str(runtime), str(self.base / 'data')],
                                capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_discovery_rejects_a_runtime_namespace_bound_to_another_store(self):
        from workbench.backend.runtime_paths import resolve_runtime_root, ensure_runtime_root
        from workbench.backend.paths import DataDirError
        data = self.base / 'data'
        runtime = resolve_runtime_root(data, 'project-A', self.env)
        ensure_runtime_root(runtime, data, 'project-A')
        marker = runtime / 'runtime.json'
        original = json.loads(marker.read_text())
        marker.write_text(json.dumps(dict(original, data_root=str(self.base / 'different'))))
        with self.assertRaisesRegex(DataDirError, 'collision'):
            resolve_runtime_root(data, 'project-A', self.env)


if __name__ == '__main__':
    unittest.main()
