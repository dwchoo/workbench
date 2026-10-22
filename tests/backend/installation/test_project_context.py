import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from workbench.backend.paths import MAX_SOCKET_PATH_BYTES


class ProjectContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wp-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'research'
        self.project.mkdir()
        self.runtime = self.base / 'r'  # short: a long XDG_RUNTIME_DIR would fall back to the real /tmp dir
        self.runtime.mkdir(mode=0o700)
        self.assertLessEqual(
            len(os.fsencode(self.runtime / 'omp-workbench' / ('x' * 32) / 'bridge.sock')), MAX_SOCKET_PATH_BYTES,
            f'temp root too long for an isolated runtime dir: {self.runtime}')
        self.env = {'HOME': str(self.base), 'XDG_RUNTIME_DIR': str(self.runtime)}

    def test_discovery_and_refused_initialization_never_write(self):
        from workbench.backend.project import resolve_project_context, initialize_project
        context = resolve_project_context(self.project, self.env)
        self.assertEqual(context.project_root, self.project)
        self.assertEqual(context.data_root, self.project / '.wb')
        self.assertTrue(context.needs_initialization)
        seen = []
        for interactive, answer in ((False, True), (True, False)):
            with self.assertRaisesRegex(RuntimeError, 'confirm'):
                initialize_project(context, confirm=lambda root: seen.append(root) or answer,
                                   interactive=interactive, environment=self.env)
            self.assertEqual(list(self.project.iterdir()), [])
            self.assertEqual(list(self.runtime.iterdir()), [])
        self.assertEqual(seen, [self.project])

    def test_confirmed_nongit_init_reuses_nearest_project_from_subdirectory(self):
        from workbench.backend.project import resolve_project_context, initialize_project
        context = initialize_project(resolve_project_context(self.project, self.env),
                                     confirm=lambda root: root == self.project,
                                     interactive=True, environment=self.env)
        marker = json.loads((self.project / '.wb/project.json').read_text())
        self.assertEqual(marker['schema_version'], 1)
        self.assertEqual(marker['project_root'], str(self.project))
        self.assertEqual(marker['state'], 'completed')
        import uuid
        self.assertEqual(str(uuid.UUID(marker['project_id'])), marker['project_id'])
        self.assertEqual(stat.S_IMODE((self.project / '.wb').stat().st_mode), 0o700)
        subdir = self.project / 'experiments/deep'
        subdir.mkdir(parents=True)
        found = resolve_project_context(subdir, self.env)
        self.assertEqual(found, context)
        self.assertFalse(found.needs_initialization)
        self.assertFalse((self.project / '.gitignore').exists())

    def test_explicit_and_environment_paths_precede_project_discovery(self):
        from workbench.backend.project import resolve_project_context
        (self.project / '.wb').write_text('invalid nearest marker')
        legacy = self.base / 'legacy'
        env = dict(self.env, WORKBENCH_DATA_DIR=str(legacy))
        chosen = resolve_project_context(self.project, env)
        self.assertEqual((chosen.data_root, chosen.runtime_root, chosen.source),
                         (legacy, legacy, 'environment'))
        self.assertFalse(chosen.needs_initialization)
        selected = resolve_project_context(self.project, env, 'explicit')
        self.assertEqual(selected.data_root, self.project / 'explicit')
        self.assertEqual(selected.source, 'explicit')
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_invalid_nearest_marker_blocks_parent_and_copied_root(self):
        from workbench.backend.project import resolve_project_context, initialize_project
        initialize_project(resolve_project_context(self.project, self.env), confirm=lambda _: True,
                           interactive=True, environment=self.env)
        child = self.project / 'child'
        child.mkdir()
        data = child / '.wb'
        data.mkdir(mode=0o700)
        marker = self.project / '.wb/project.json'
        for value in ({}, json.loads(marker.read_text()),
                      dict(json.loads(marker.read_text()), project_root=str(child), state='copying')):
            (data / 'project.json').write_text(json.dumps(value))
            (data / 'project.json').chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, str(data)):
                resolve_project_context(child, self.env)
        (data / 'project.json').unlink()
        data.rmdir()
        data.symlink_to(self.project / '.wb', target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            resolve_project_context(child, self.env)

    def test_nonprivate_project_directory_is_rejected_without_chmod(self):
        from workbench.backend.project import resolve_project_context, initialize_project
        initialize_project(resolve_project_context(self.project, self.env), confirm=lambda _: True,
                           interactive=True, environment=self.env)
        (self.project / '.wb').chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, 'private'):
            resolve_project_context(self.project, self.env)
        self.assertEqual(stat.S_IMODE((self.project / '.wb').stat().st_mode), 0o755)

    def test_git_ignore_preserves_bytes_and_actually_ignores_project_state(self):
        from workbench.backend.project import resolve_project_context, initialize_project
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        original = b'# research rules\r\n/results\r\n!keep'
        (self.project / '.gitignore').write_bytes(original)
        context = initialize_project(resolve_project_context(self.project, self.env), confirm=lambda _: True,
                                     interactive=True, environment=self.env)
        self.assertEqual((self.project / '.gitignore').read_bytes(), original + b'\n/.wb/\n')
        result = subprocess.run(['git', '-C', str(self.project), 'check-ignore', '.wb/project.json'],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), b'.wb/project.json')
        self.assertFalse(context.needs_initialization)

    def test_repeated_confirmed_initializers_converge_without_replacing_identity(self):
        from workbench.backend.project import resolve_project_context, initialize_project
        proposed = resolve_project_context(self.project, self.env)
        first = initialize_project(proposed, confirm=lambda _: True, interactive=True, environment=self.env)
        marker = (self.project / '.wb/project.json').read_bytes()
        second = initialize_project(proposed, confirm=lambda _: True, interactive=True, environment=self.env)
        self.assertEqual(second, first)
        self.assertEqual((self.project / '.wb/project.json').read_bytes(), marker)

    def test_context_and_explicit_project_marker_share_external_runtime(self):
        from workbench.backend.project import resolve_project_context, initialize_project
        context = initialize_project(resolve_project_context(self.project, self.env), confirm=lambda _: True,
                                     interactive=True, environment=self.env)
        self.assertFalse(context.runtime_root.is_relative_to(context.data_root))
        self.assertTrue(context.runtime_root.is_relative_to(self.runtime))
        self.assertFalse(context.runtime_root.exists())
        explicit = resolve_project_context(self.base, self.env, context.data_root)
        self.assertEqual(explicit.project_root, self.project)
        self.assertEqual(explicit.runtime_root, context.runtime_root)
        self.assertEqual(explicit.project_id, context.project_id)
        self.assertEqual(explicit.source, 'explicit')

    def test_start_prerequisite_failure_leaves_git_project_untouched(self):
        from workbench.backend.project import resolve_project_context, initialize_project
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        original = b'# unchanged\n'
        (self.project / '.gitignore').write_bytes(original)
        def missing_setup():
            raise RuntimeError('run wb setup')
        with self.assertRaisesRegex(RuntimeError, 'wb setup'):
            initialize_project(resolve_project_context(self.project, self.env), confirm=lambda _: True,
                               interactive=True, environment=self.env, prerequisites=missing_setup)
        self.assertFalse((self.project / '.wb').exists())
        self.assertEqual((self.project / '.gitignore').read_bytes(), original)

    def test_tracked_state_and_ignore_write_failure_never_publish_marker(self):
        from workbench.backend.project import resolve_project_context, initialize_project
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        data = self.project / '.wb'
        data.mkdir()
        (data / 'old.log').write_bytes(b'user history')
        subprocess.run(['git', '-C', str(self.project), 'add', '.wb/old.log'], check=True)
        (data / 'old.log').unlink()
        data.rmdir()
        proposed = resolve_project_context(self.project, self.env)
        with self.assertRaisesRegex(RuntimeError, 'tracked'):
            initialize_project(proposed, confirm=lambda _: True, interactive=True, environment=self.env)
        self.assertFalse((self.project / '.gitignore').exists())
        self.assertFalse(data.exists())
        subprocess.run(['git', '-C', str(self.project), 'rm', '--cached', '-q', '.wb/old.log'], check=True)
        original = b'# read only\n'
        (self.project / '.gitignore').write_bytes(original)
        (self.project / '.gitignore').chmod(0o444)
        with self.assertRaisesRegex(RuntimeError, 'Git ignore'):
            initialize_project(proposed, confirm=lambda _: True, interactive=True, environment=self.env)
        self.assertFalse(data.exists())
        self.assertEqual((self.project / '.gitignore').read_bytes(), original)

    def test_late_parent_initialization_does_not_create_an_implicit_nested_project(self):
        from workbench.backend.project import resolve_project_context, initialize_project
        child = self.project / 'child'
        child.mkdir()
        proposed_child = resolve_project_context(child, self.env)
        parent = initialize_project(resolve_project_context(self.project, self.env), confirm=lambda _: True,
                                    interactive=True, environment=self.env)
        resolved = initialize_project(proposed_child, confirm=lambda _: True,
                                      interactive=True, environment=self.env)
        self.assertEqual(resolved.project_id, parent.project_id)
        self.assertFalse((child / '.wb').exists())

    def test_common_init_lock_excludes_second_writer_and_nested_target(self):
        from workbench.backend.project import project_initialization_lock, resolve_project_context
        code = """import sys
from pathlib import Path
from workbench.backend.project import initialize_project, resolve_project_context
from workbench.backend.paths import DataDirError
try:
    initialize_project(resolve_project_context(Path(sys.argv[1]), {}), confirm=lambda _: True,
                       interactive=True, environment={})
except DataDirError as exc:
    print(exc)
    raise SystemExit(0 if 'locked' in str(exc) else 5)
raise SystemExit(4)
"""
        child = self.project / 'child'
        child.mkdir()
        with project_initialization_lock(self.project, self.env):
            for target in (self.project, child):
                result = subprocess.run([sys.executable, '-c', code, str(target)],
                                        capture_output=True, timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertFalse((target / '.wb').exists())
        self.assertTrue(resolve_project_context(self.project, self.env).needs_initialization)

    def test_linked_git_worktree_ignores_its_own_state_without_editing_source(self):
        from workbench.backend.project import resolve_project_context, initialize_project
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        subprocess.run(['git', '-C', str(self.project), '-c', 'user.name=WB fixture',
                        '-c', 'user.email=fixture@invalid', 'commit', '-q', '--allow-empty', '-m', 'fixture'],
                       check=True)
        linked = self.base / 'linked'
        subprocess.run(['git', '-C', str(self.project), 'worktree', 'add', '-q', '-b', 'fixture-linked', str(linked)],
                       check=True)
        self.assertTrue((linked / '.git').is_file())
        context = initialize_project(resolve_project_context(linked, self.env), confirm=lambda _: True,
                                     interactive=True, environment=self.env)
        self.assertEqual(context.project_root, linked)
        self.assertEqual((linked / '.gitignore').read_bytes(), b'/.wb/\n')
        self.assertFalse((self.project / '.gitignore').exists())
        result = subprocess.run(['git', '-C', str(linked), 'check-ignore', '.wb/project.json'], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_initialization_rejects_context_whose_durable_path_was_replaced(self):
        from dataclasses import replace
        from workbench.backend.project import resolve_project_context, initialize_project
        proposed = resolve_project_context(self.project, self.env)
        foreign = self.base / 'unrelated-state'
        altered = replace(proposed, data_root=foreign)
        with self.assertRaisesRegex(RuntimeError, 'context'):
            initialize_project(altered, confirm=lambda _: True, interactive=True, environment=self.env)
        self.assertFalse(foreign.exists())
        self.assertFalse((self.project / '.wb').exists())


if __name__ == '__main__':
    unittest.main()
