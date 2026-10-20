"""Initial marker publication preserves competing state at the public init seam."""
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from workbench.backend.paths import DataDirError
from workbench.backend.project import initialize_project, resolve_project_context


class MarkerPublicationTests(unittest.TestCase):
    def test_marker_appearing_after_directory_open_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix='wi-initial-marker-') as directory:
            base = Path(directory)
            project = base / 'project'
            project.mkdir()
            runtime = base / 'run'
            runtime.mkdir(mode=0o700)
            environment = {'HOME': str(base), 'XDG_RUNTIME_DIR': str(runtime)}
            context = resolve_project_context(project, environment)
            marker = context.data_root / 'project.json'
            original = b'concurrently supplied user marker: do not replace\n'
            actual_open = os.open
            observed = []

            def open_then_create_marker(path, flags, mode=0o777, *, dir_fd=None):
                descriptor = actual_open(path, flags, mode, dir_fd=dir_fd)
                if Path(path) == context.data_root and flags & os.O_DIRECTORY:
                    marker.write_bytes(original)
                    marker.chmod(0o600)
                    observed.append(True)
                return descriptor

            with patch('os.open', new=open_then_create_marker):
                with self.assertRaises(DataDirError):
                    initialize_project(context, confirm=lambda _: True, interactive=True,
                                       environment=environment)
            self.assertTrue(observed, 'actual directory-open interleaving did not run')
            self.assertEqual(marker.read_bytes(), original)
            self.assertEqual(sorted(p.name for p in context.data_root.iterdir()), ['project.json'])

    def test_directory_replaced_after_open_cannot_redirect_publication(self):
        with tempfile.TemporaryDirectory(prefix='wi-bound-marker-') as directory:
            base = Path(directory)
            project = base / 'project'
            project.mkdir()
            existing = base / 'existing-private-state'
            existing.mkdir(mode=0o700)
            original = b'existing private marker remains unchanged\n'
            marker = existing / 'project.json'
            marker.write_bytes(original)
            marker.chmod(0o600)
            before = marker.stat().st_mtime_ns
            runtime = base / 'run'
            runtime.mkdir(mode=0o700)
            environment = {'HOME': str(base), 'XDG_RUNTIME_DIR': str(runtime)}
            context = resolve_project_context(project, environment)
            displaced = project / '.wb-displaced'
            actual_open = os.open
            observed = []

            def open_then_schedule_actor(path, flags, mode=0o777, *, dir_fd=None):
                descriptor = actual_open(path, flags, mode, dir_fd=dir_fd)
                if Path(path) == context.data_root and flags & os.O_DIRECTORY:
                    actor = subprocess.run([sys.executable, '-c',
                        "import sys; from pathlib import Path; "
                        "a,b,c=map(Path,sys.argv[1:]); a.rename(b); a.symlink_to(c,target_is_directory=True)",
                        str(context.data_root), str(displaced), str(existing)],
                        capture_output=True, timeout=5, check=True)
                    observed.append(actor.returncode)
                return descriptor

            with patch('os.open', new=open_then_schedule_actor):
                with self.assertRaises(DataDirError):
                    initialize_project(context, confirm=lambda _: True, interactive=True,
                                       environment=environment)
            self.assertEqual(observed, [0])
            self.assertTrue(context.data_root.is_symlink())
            self.assertEqual(marker.read_bytes(), original)
            self.assertEqual(marker.stat().st_mtime_ns, before)
            self.assertEqual(sorted(p.name for p in existing.iterdir()), ['project.json'])
            self.assertTrue((displaced / 'project.json').is_file())

    def test_default_private_json_writer_retains_legitimate_update_behavior(self):
        from workbench.backend.paths import read_private_json, write_private_json
        with tempfile.TemporaryDirectory(prefix='wi-private-update-') as directory:
            parent = Path(directory)
            parent.chmod(0o700)
            target = parent / 'backend.json'
            write_private_json(target, {'generation': 1})
            write_private_json(target, {'generation': 2, 'state': 'running'})
            self.assertEqual(read_private_json(target), {'generation': 2, 'state': 'running'})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(p.name for p in parent.iterdir()), ['backend.json'])


if __name__ == '__main__':
    unittest.main()
