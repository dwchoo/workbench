"""Independent WI-03 behavior checks from SPEC s1, fixture-only G-WI-03.

Observation: public Python project/layout API, real scratch Git and filesystem.
This does not establish CLI/backend/OMP, migration or active-process behavior.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import unittest

from workbench.backend.paths import DataDirError, DataLayout
from workbench.backend.project import initialize_project, resolve_project_context
from workbench.backend.runtime_paths import ensure_runtime_root, resolve_runtime_root


class IndependentProjectTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='wi-independent-')
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.project = self.base / 'study'
        self.project.mkdir()
        self.runtime = self.base / 'run'
        self.runtime.mkdir(mode=0o700)
        self.env = {'HOME': str(self.base), 'XDG_RUNTIME_DIR': str(self.runtime)}

    def git_result(self, *args, root=None, check=True):
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(HOME=str(self.base), XDG_CONFIG_HOME=str(self.base / 'config'),
                   GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
        return subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C',
                               str(root or self.project), *args],
                              env=env, capture_output=True, check=check, timeout=5)

    def git(self, *args, root=None):
        return self.git_result(*args, root=root).stdout

    def init(self, root=None):
        context = resolve_project_context(root or self.project, self.env)
        return initialize_project(context, confirm=lambda _: True,
                                  interactive=True, environment=self.env)

    def test_marker_only_ignore_still_protects_all_durable_project_files(self):
        """WI-AC-03/11: ignoring project.json alone must not expose logs/DB."""
        self.git('init', '-q')
        original = b'# existing precise rule\n/.wb/project.json\n'
        ignore = self.project / '.gitignore'
        ignore.write_bytes(original)
        self.init()
        self.assertTrue(ignore.read_bytes().startswith(original))
        # Paths are predetermined durable contract names, not helper-derived oracles.
        for name in ('tasks.sqlite3', 'backend.log', 'raw-logs/sample.raw',
                     'omp-root/agent/sessions/session.jsonl'):
            with self.subTest(path=name):
                ignored = self.git_result('check-ignore', '--no-index', '--',
                                          f'.wb/{name}', check=False)
                self.assertEqual(ignored.returncode, 0,
                                 f'durable path is exposed to Git: .wb/{name}; '
                                 f'stderr={ignored.stderr!r}')
                self.assertEqual(ignored.stdout.strip(), f'.wb/{name}'.encode())

    def test_nested_git_root_adds_local_rule_without_changing_index_or_parent_ignore(self):
        """WI-AC-03/05: project folder need not equal Git root."""
        self.git('init', '-q')
        parent_ignore = self.project / '.gitignore'
        parent_ignore.write_bytes(b'# root research rules\r\n/output\r\n')
        tracked = self.project / 'existing.txt'
        tracked.write_bytes(b'independent preserved contents\x00\xff')
        self.git('add', '.gitignore', 'existing.txt')
        index = self.project / '.git/index'
        index_before = index.read_bytes()
        parent_before = parent_ignore.read_bytes()
        tracked_before = tracked.read_bytes()
        child = self.project / 'nested'
        child.mkdir()
        local = child / '.gitignore'
        local.write_bytes(b'# local no newline')
        self.init(child)
        self.assertEqual(local.read_bytes(), b'# local no newline\n/.wb/\n')
        self.assertEqual(parent_ignore.read_bytes(), parent_before)
        self.assertEqual(tracked.read_bytes(), tracked_before)
        self.assertEqual(index.read_bytes(), index_before)
        self.assertEqual(self.git('check-ignore', '--no-index', '--',
                                  'nested/.wb/tasks.sqlite3').strip(),
                         b'nested/.wb/tasks.sqlite3')

    def test_tracked_missing_state_blocks_init_without_mutating_git_index(self):
        """WI-AC-11: no automatic untrack, deletion or index update."""
        self.git('init', '-q')
        data = self.project / '.wb'
        data.mkdir()
        tracked = data / 'old.raw'
        tracked.write_bytes(b'tracked history')
        self.git('add', '.wb/old.raw')
        index = self.project / '.git/index'
        expected_index = index.read_bytes()
        tracked.unlink()
        data.rmdir()
        ignore = self.project / '.gitignore'
        ignore.write_bytes(b'# unchanged\n')
        with self.assertRaises(DataDirError):
            self.init()
        self.assertEqual(index.read_bytes(), expected_index)
        self.assertEqual(ignore.read_bytes(), b'# unchanged\n')
        self.assertFalse(data.exists())
        self.assertEqual(self.git('ls-files', '--', '.wb/old.raw').strip(), b'.wb/old.raw')

    def test_linked_and_hardlinked_ignore_preserve_referent_and_refuse_init(self):
        """WI-AC-11: writable aliases may not modify unrelated user files."""
        self.git('init', '-q')
        referent = self.base / 'shared-ignore'
        original = b'# owned elsewhere\n/data\n'
        referent.write_bytes(original)
        ignore = self.project / '.gitignore'
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    ignore.symlink_to(referent)
                else:
                    os.link(referent, ignore)
                try:
                    with self.assertRaises(DataDirError):
                        self.init()
                    self.assertEqual(referent.read_bytes(), original)
                    self.assertFalse((self.project / '.wb').exists())
                finally:
                    ignore.unlink()

    def test_real_project_write_permission_denial_does_not_publish_marker(self):
        """WI-AC-05/11: no mocked access result; euid must enforce chmod."""
        if os.geteuid() == 0:
            self.skipTest('root bypasses ordinary DAC directory permissions')
        self.project.chmod(0o500)
        try:
            with self.assertRaises((DataDirError, PermissionError)):
                self.init()
            self.assertFalse((self.project / '.wb').exists())
            self.assertFalse((self.project / '.gitignore').exists())
            self.assertEqual(list(self.runtime.iterdir()), [])
        finally:
            self.project.chmod(0o700)

    def test_unreadable_nearest_marker_is_not_skipped_for_valid_parent(self):
        """WI-AC-06/11: permission failure cannot switch project identity."""
        self.init()
        child = self.project / 'nested'
        child.mkdir()
        data = child / '.wb'
        data.mkdir(mode=0o700)
        marker = data / 'project.json'
        payload = {'schema_version': 1, 'state': 'completed',
                   'project_root': str(child),
                   'project_id': '71db6494-3d79-44ee-b953-2310af42b07c'}
        expected = json.dumps(payload).encode()
        marker.write_bytes(expected)
        marker.chmod(0o000)
        try:
            with self.assertRaises(DataDirError):
                resolve_project_context(child, self.env)
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o000)
        finally:
            marker.chmod(0o600)
        self.assertEqual(marker.read_bytes(), expected)

    def test_stale_proposal_cannot_overwrite_late_file_collision(self):
        """WI-AC-11: recheck discovery after confirmation, preserve file bytes."""
        context = resolve_project_context(self.project, self.env)
        collision = self.project / '.wb'
        def confirm(root):
            self.assertEqual(root, self.project)
            collision.write_bytes(b'user file appeared during confirmation\x00')
            return True
        with self.assertRaises(DataDirError):
            initialize_project(context, confirm=confirm, interactive=True, environment=self.env)
        self.assertEqual(collision.read_bytes(), b'user file appeared during confirmation\x00')
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_cwd_symlink_and_child_resolve_same_canonical_identity_without_rewrite(self):
        """WI-AC-06/15: cwd alias is normalized while project marker stays intact."""
        expected = self.init()
        marker = self.project / '.wb/project.json'
        before = (marker.read_bytes(), marker.stat().st_mtime_ns)
        alias = self.base / 'study-alias'
        alias.symlink_to(self.project, target_is_directory=True)
        (self.project / 'subdir').mkdir()
        found = resolve_project_context(alias / 'subdir', self.env)
        self.assertEqual(found.project_root, self.project)
        self.assertEqual(found.project_id, expected.project_id)
        self.assertEqual(found.runtime_root, expected.runtime_root)
        self.assertEqual((marker.read_bytes(), marker.stat().st_mtime_ns), before)

    def test_multibyte_long_project_and_shared_uuid_have_separate_short_bound_sockets(self):
        """WI-AC-10: same UUID alone cannot cause cross-project runtime sharing."""
        identity = '33c7c0d0-4ab2-4d9e-81d1-0d8e6d075b82'
        a = self.base / ('연구' * 35) / '.wb'
        b = self.base / ('결과' * 35) / '.wb'
        roots = [resolve_runtime_root(data, identity, self.env) for data in (a, b)]
        self.assertNotEqual(*roots)
        for data, runtime in zip((a, b), roots):
            ensure_runtime_root(runtime, data, identity)
            self.assertFalse(runtime.is_relative_to(data))
            self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)
            layout = DataLayout(data, runtime_root=runtime)
            for endpoint in (layout.ui_socket, layout.bridge_socket):
                self.assertLessEqual(len(os.fsencode(endpoint)), 107)
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.bind(str(endpoint))
                endpoint.unlink()
            self.assertFalse(data.exists())

    def test_runtime_symlink_identity_refuses_without_changing_referent(self):
        """WI-AC-10/11: runtime identity lookup cannot follow user files."""
        data = self.project / '.wb'
        runtime = resolve_runtime_root(data, 'fixed-identity', self.env)
        ensure_runtime_root(runtime, data, 'fixed-identity')
        marker = runtime / 'runtime.json'
        original = marker.read_bytes()
        referent = self.base / 'private-record'
        referent.write_bytes(original)
        referent.chmod(0o600)
        before = (hashlib.sha256(referent.read_bytes()).hexdigest(), referent.stat().st_mtime_ns)
        marker.unlink()
        marker.symlink_to(referent)
        with self.assertRaises(DataDirError):
            resolve_runtime_root(data, 'fixed-identity', self.env)
        with self.assertRaises(DataDirError):
            ensure_runtime_root(runtime, data, 'fixed-identity')
        self.assertTrue(marker.is_symlink())
        self.assertEqual((hashlib.sha256(referent.read_bytes()).hexdigest(),
                          referent.stat().st_mtime_ns), before)

    def test_explicit_legacy_context_retains_all_durable_and_socket_locations(self):
        """WI-AC-10: runtime split must not silently relocate old stores."""
        legacy = self.base / 'legacy'
        legacy.mkdir(mode=0o700)
        sentinel = legacy / 'backend.json'
        sentinel.write_bytes(b'legacy identity bytes')
        env = dict(self.env, WORKBENCH_DATA_DIR=str(self.base / 'other'))
        selected = resolve_project_context(self.project, env, legacy)
        self.assertEqual((selected.data_root, selected.runtime_root), (legacy, legacy))
        self.assertFalse(selected.needs_initialization)
        self.assertIsNone(selected.project_id)
        layout = DataLayout(legacy)
        for attribute, suffix in {
            'lock': 'backend.lock', 'record': 'backend.json',
            'ui_socket': 'ui.sock', 'bridge_socket': 'bridge.sock',
            'log': 'backend.log', 'tasks': 'tasks.sqlite3',
            'raw_logs': 'raw-logs', 'raw_v2': 'raw-v2',
            'raw_rotation': 'raw-rotation.json', 'important_logs': 'important-logs',
            'summary_compatibility': 'summary-compatibility',
            'pause_journal': 'pause.jsonl', 'lifecycle_journal': 'lifecycle.json',
            'workflow': 'workflow',
        }.items():
            with self.subTest(attribute=attribute):
                self.assertEqual(getattr(layout, attribute), legacy / suffix)
        self.assertEqual(sentinel.read_bytes(), b'legacy identity bytes')
        self.assertEqual(list(self.runtime.iterdir()), [])
        self.assertFalse((legacy / 'project.json').exists())


if __name__ == '__main__':
    unittest.main()
