"""WI-I41 requirement-derived gaps; owned fixtures, actual namespace events.

Expected values were recorded before implementation inspection in the I41
requirements-first receipt. Native SQLite is opened only on builder/control
copies, never on a measured migration source. These are scheduled same-UID
namespace races, not a malicious-code isolation claim.
"""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
FIXED_ROWS = [(11, 'alpha-final'), (33, 'gamma-final')]


def inventory(root):
    result = {}
    for path in (root, *sorted(root.rglob('*'))):
        info = path.lstat()
        row = [stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
               info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino]
        if stat.S_ISREG(info.st_mode):
            row.append(hashlib.sha256(path.read_bytes()).hexdigest())
        elif stat.S_ISLNK(info.st_mode):
            row.append(os.readlink(path))
        result[str(path.relative_to(root))] = row
    return result


class IndependentMigrationRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wi-i41-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'legacy'
        self.project = self.base / 'project'
        self.source.mkdir(mode=0o700)
        self.project.mkdir(mode=0o700)
        self.target = self.project / '.wb'
        self.env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                    'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        for variable in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME',
                         'XDG_RUNTIME_DIR', 'TMPDIR'):
            path = self.base / variable.lower()
            path.mkdir(mode=0o700)
            self.env[variable] = str(path)
        from workbench.tasks.repository import TaskRepository
        repo = TaskRepository(self.source / 'tasks.sqlite3')
        repo.create_task({'title': 'I41_TASK_FIXED'}, task_id='i41-task')
        repo.close()
        (self.source / 'tasks.sqlite3').chmod(0o600)
        self.write_json(self.source / 'backend.json', {
            'contract': {'name': 'workbench.ui', 'version': 1}, 'phase': 'stopped',
            'data_dir': str(self.source), 'project_dir': str(self.project),
            'runtime_root': str(self.source), 'ui_socket': str(self.source / 'ui.sock'),
            'bridge_socket': str(self.source / 'bridge.sock'), 'uid': os.geteuid(),
            'boot_id': '00000000-0000-4000-8000-000000000001',
            'processes': {name: {'pid': 2000000200 + i, 'start_ticks': 71 + i,
                'owner_epoch': 1, 'role': name} for i, name in enumerate(
                    ('backend', 'manager_omp', 'worker_omp', 'host_shell'))},
            'survivors': [], 'shutdown': {'verified': True, 'left_running': [],
                'omp_children': {'alive': [], 'observed': []}, 'previous_survivors': []}})
        (self.source / 'backend.lock').touch(mode=0o600)

    def write_json(self, path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def invoke(self, *arguments):
        from workbench.backend.cli import main
        output, error = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.project)
            with patch.dict(os.environ, self.env, clear=True), redirect_stdout(output), redirect_stderr(error):
                status = main(list(map(str, arguments)))
        finally:
            os.chdir(previous)
        return status, output.getvalue(), error.getvalue()

    def migrate(self):
        return self.invoke('migrate', '--from', self.source, '--project', self.project)

    def evidence(self, value):
        directory = os.environ.get('WI_I41_EVIDENCE_DIR')
        if directory:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            (path / (self._testMethodName + '.json')).write_text(json.dumps(value, indent=2) + '\n')

    def history(self):
        """Build elsewhere, then copy DB+WAL before measuring the source."""
        builder = self.base / 'builder.db'
        db = sqlite3.connect(builder)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE history(id INTEGER PRIMARY KEY AUTOINCREMENT, prompt TEXT, '
                       'created_at TEXT, cwd TEXT, session_id TEXT, use_count INTEGER)')
            db.execute("CREATE VIRTUAL TABLE history_fts USING fts5(prompt, content='history', content_rowid='id')")
            db.execute('PRAGMA user_version=1')
            db.executemany('INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)', [
                (11, 'alpha-original', '2000-01-01', '/fixed/cwd', 'i41-session', 1),
                (22, 'beta-remove', '2000-01-02', '/fixed/cwd', 'i41-session', 2)])
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute("UPDATE history SET prompt='alpha-final' WHERE id=11")
            db.execute('DELETE FROM history WHERE id=22')
            db.execute("INSERT INTO history VALUES (33,'gamma-final','2000-01-03','/fixed/cwd','i41-session',3)")
            db.commit()
            main, wal = builder.read_bytes(), Path(str(builder) + '-wal').read_bytes()
        finally:
            db.close()
        agent = self.source / 'omp-root/agent'
        agent.mkdir(mode=0o700, parents=True)
        for suffix, content in (('', main), ('-wal', wal)):
            path = agent / ('history.db' + suffix)
            path.write_bytes(content)
            path.chmod(0o600)
        (self.source / 'omp-root').chmod(0o700)
        control = self.base / 'native-control.db'
        control.write_bytes(main)
        Path(str(control) + '-wal').write_bytes(wal)
        with sqlite3.connect(control) as db:
            self.assertEqual(db.execute('SELECT id,prompt FROM history ORDER BY id').fetchall(), FIXED_ROWS)
        return main, wal

    def test_verified_and_mismatched_receipts_refuse_all_selectors_without_side_effects(self):
        legacy = self.invoke('status', '--json', '--data-dir', self.source)
        self.assertEqual(legacy[0], 3, legacy)
        alias = self.project / 'alias'
        alias.symlink_to(self.base, target_is_directory=True)
        selected = alias / 'legacy'
        cases = [('verified', False), ('verifying', False), ('completed', False), ('verified', True)]
        events = []
        for state, marker in cases:
            self.write_json(self.source / 'migration.json', {'schema_version': 1, 'state': state,
                'target': str(self.source), 'project_id': '11111111-1111-4111-8111-111111111111'})
            if marker:
                self.write_json(self.source / 'project.json', {'schema_version': 1, 'state': 'completed',
                    'project_root': str(self.source.parent), 'data_root': str(self.source),
                    'project_id': '22222222-2222-4222-8222-222222222222'})
            before = inventory(self.source)
            for selector in ('explicit', 'environment'):
                args = ['--data-dir', selected] if selector == 'explicit' else []
                if selector == 'environment':
                    self.env['WORKBENCH_DATA_DIR'] = str(selected)
                try:
                    for action in (('status', '--json'), ('start', '--no-attach')):
                        with self.subTest(state=state, marker=marker, selector=selector, action=action):
                            result = self.invoke(*action, *args)
                            self.assertEqual(result[0], 1, result)
                            self.assertIn('marker', result[2])
                            self.assertEqual(inventory(self.source), before)
                            events.append([state, marker, selector, action[0], result[0]])
                finally:
                    self.env.pop('WORKBENCH_DATA_DIR', None)
        self.evidence({'public_refusals': events, 'ordinary_legacy_status': legacy[0]})

    def test_success_uses_anonymous_samefs_inputs_memory_sqlite_and_held_first_publication(self):
        from workbench.storage.important_log import ImportantLogCatalog, ImportantLogReader
        from workbench.tasks.repository import TaskRepository
        self.history()
        subject = {'schemaVersion': 1, 'kind': 'command', 'id': '41414141-4141-4141-8141-414141414141'}
        body = {'schemaVersion': 1, 'facts': ['I41_IMPORTANT_FIXED'], 'interpretations': [], 'unknowns': []}
        catalog = ImportantLogCatalog(self.source / 'important-logs', create=True)
        self.assertEqual(catalog.append(subject, 'event', [], body)['state'], 'ok')
        before = inventory(self.source)
        opened, write, closed, connect = os.open, os.write, os.close, sqlite3.connect
        events, anonymous, published, source_opens = [], {}, {}, []
        class ObservedConnection(sqlite3.Connection):
            def backup(connection, destination, *args, **kwargs):
                events.append(['backup', len(anonymous)])
                return super().backup(destination, *args, **kwargs)
            def close(connection):
                events.append(['sqlite-close', len(anonymous)])
                return super().close()
        def memory(database, *args, **kwargs):
            self.assertEqual(database, ':memory:', 'every migration SQLite seam must be memory only')
            events.append(['sqlite-open', len(anonymous)])
            return connect(database, *args, factory=ObservedConnection, **kwargs)
        def observe_open(path, flags, *args, **kwargs):
            fd = opened(path, flags, *args, **kwargs)
            name = os.readlink(f'/proc/self/fd/{fd}')
            info = os.fstat(fd)
            if flags & os.O_TMPFILE == os.O_TMPFILE:
                self.assertEqual(info.st_nlink, 0)
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                self.assertEqual(info.st_dev, self.target.stat().st_dev)
                self.assertTrue(name.startswith(str(self.target) + '/'), name)
                anonymous[fd] = (info.st_dev, info.st_ino)
                events.append(['anonymous-open', name])
            elif name.startswith(str(self.source) + '/') and ('.sqlite3' in name or 'history.db' in name):
                self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
                source_opens.append([name, flags])
            if flags & os.O_CREAT and (name.endswith('.sqlite3') or name.endswith('history.db')):
                self.assertTrue(flags & os.O_EXCL)
                self.assertTrue(flags & os.O_NOFOLLOW)
                published[fd] = (info.st_dev, info.st_ino)
                events.append(['publication-open', name])
            return fd
        def observe_write(fd, data):
            if fd in published:
                info = os.fstat(fd)
                self.assertEqual((info.st_dev, info.st_ino), published[fd])
                events.append(['publication-write', os.readlink(f'/proc/self/fd/{fd}')])
            return write(fd, data)
        def observe_close(fd):
            if fd in anonymous:
                events.append(['anonymous-close', anonymous.pop(fd)])
            published.pop(fd, None)
            return closed(fd)
        with patch('sqlite3.connect', side_effect=memory), patch('os.open', side_effect=observe_open), \
                patch('os.write', side_effect=observe_write), patch('os.close', side_effect=observe_close):
            result = self.migrate()
        self.assertEqual(result[0], 0, result)
        self.assertEqual(inventory(self.source), before)
        self.assertFalse(anonymous)
        self.assertFalse(published)
        self.assertGreaterEqual(sum(e[0] == 'backup' for e in events), 3)
        self.assertTrue(all(e[1] > 0 for e in events if e[0] in ('sqlite-open', 'backup', 'sqlite-close')))
        self.assertGreaterEqual(sum(e[0] == 'anonymous-open' for e in events), 4)
        self.assertTrue(source_opens)
        self.assertEqual((self.target / 'omp-root/agent/history.db').read_bytes()[18:20], b'\x02\x02')
        with sqlite3.connect(self.target / 'omp-root/agent/history.db') as db:
            self.assertEqual(db.execute('SELECT id,prompt FROM history ORDER BY id').fetchall(), FIXED_ROWS)
        repo = TaskRepository(self.target / 'tasks.sqlite3')
        try:
            self.assertEqual(repo.get_task_spec('i41-task', 1)['spec'], {'title': 'I41_TASK_FIXED'})
        finally:
            repo.close()
        found = ImportantLogReader(self.target / 'important-logs').show(subject)
        self.assertTrue(found['complete'], found)
        self.assertEqual([entry['entry']['body'] for entry in found['entries']], [body])
        self.evidence({'events': events, 'source_readonly_opens': source_opens,
                       'fixed_history_rows': FIXED_ROWS, 'public_readers': ['TaskRepository', 'ImportantLogReader']})

    def test_input_copy_leaf_collision_is_real_and_preserves_foreign_database(self):
        self.history()
        other = self.base / 'external'
        other.mkdir(mode=0o700)
        victim = other / 'victim.db'
        victim.write_bytes((self.source / 'omp-root/agent/history.db').read_bytes())
        victim.chmod(0o600)
        before, foreign = inventory(self.source), inventory(other)
        write, events = os.write, []
        def actor(fd, data):
            info = os.fstat(fd)
            if info.st_nlink == 0 and not events:
                (self.target / 'tasks.sqlite3').symlink_to(victim)
                events.append('foreign leaf planted while anonymous input is being copied')
            return write(fd, data)
        with patch('os.write', side_effect=actor):
            result = self.migrate()
        self.assertEqual(len(events), 1, 'race hook must cause a real filesystem event')
        self.assertEqual(result[0], 1, result)
        self.assertEqual(inventory(self.source), before)
        self.assertEqual(inventory(other), foreign)
        self.assertEqual(os.readlink(self.target / 'tasks.sqlite3'), str(victim))
        self.assertFalse((self.target / 'project.json').exists())
        self.evidence({'events': events, 'result': result})

    def test_foreign_sidecar_collision_is_not_deleted_during_publication(self):
        self.history()
        other = self.base / 'external'
        other.mkdir(mode=0o700)
        victim = other / 'foreign-wal'
        victim.write_bytes(b'I41_FOREIGN_SIDECAR_MUST_REMAIN')
        before, foreign = inventory(self.source), inventory(other)
        connect, events = sqlite3.connect, []
        def actor(database, *args, **kwargs):
            if database == ':memory:' and not events:
                (self.target / 'tasks.sqlite3-wal').symlink_to(victim)
                events.append('foreign sidecar planted during memory verification')
            return connect(database, *args, **kwargs)
        with patch('sqlite3.connect', side_effect=actor):
            result = self.migrate()
        self.assertEqual(len(events), 1)
        self.assertEqual(result[0], 1, result)
        self.assertIn('sidecar collision', result[2])
        self.assertEqual(inventory(self.source), before)
        self.assertEqual(inventory(other), foreign)
        self.assertEqual(os.readlink(self.target / 'tasks.sqlite3-wal'), str(victim))
        self.assertFalse((self.target / 'project.json').exists())
        self.evidence({'events': events, 'result': result})

    def test_header_only_wal_and_invalid_format_boundaries_use_fixed_native_control(self):
        from workbench.backend.migration_sqlite import committed_image
        from workbench.backend.paths import DataDirError
        builder = self.base / 'sentinel-builder.db'
        db = sqlite3.connect(builder)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE sentinel(id INTEGER PRIMARY KEY, note TEXT NOT NULL)')
            db.executemany('INSERT INTO sentinel VALUES (?,?)', [(11, 'alpha-original'), (22, 'beta-remove')])
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute("UPDATE sentinel SET note='alpha-final' WHERE id=11")
            db.execute('DELETE FROM sentinel WHERE id=22')
            db.execute("INSERT INTO sentinel VALUES (33,'gamma-final')")
            db.commit()
            main, wal = builder.read_bytes(), Path(str(builder) + '-wal').read_bytes()
        finally:
            db.close()
        for index, (content, expected) in enumerate(((wal, FIXED_ROWS), (wal[:32], [(11, 'alpha-original'), (22, 'beta-remove')]))):
            control = self.base / f'sentinel-control-{index}.db'
            control.write_bytes(main)
            Path(str(control) + '-wal').write_bytes(content)
            with sqlite3.connect(control) as native:
                self.assertEqual(native.execute('SELECT id,note FROM sentinel ORDER BY id').fetchall(), expected)
            image, _ = committed_image(main, content)
            with sqlite3.connect(':memory:') as memory:
                memory.deserialize(image)
                self.assertEqual(memory.execute('SELECT id,note FROM sentinel ORDER BY id').fetchall(), expected)
        bad_magic = b'BAD!' + wal[4:]
        mismatch = wal[:8] + (512).to_bytes(4, 'big') + wal[12:]
        malformed_main = b'X' + main[1:]
        for label, original, content in (('magic', main, bad_magic), ('page-mismatch', main, mismatch),
                                         ('main-header', malformed_main, wal)):
            with self.subTest(case=label), patch('workbench.backend.migration_sqlite.bytearray',
                    side_effect=AssertionError('invalid format allocated image'), create=True):
                with self.assertRaises(DataDirError):
                    committed_image(original, content)

    def test_stale_approved_current_run_and_ended_child_remain_history_on_fresh_backend(self):
        from dataclasses import asdict
        from workbench.app.lifecycle import LifecycleRecord, PeerRef, ControlState
        from workbench.backend.flow_tasks import FlowTask
        from workbench.backend.launcher import LaunchPlan
        from workbench.backend.paths import DataLayout
        from workbench.backend.service import Backend
        from workbench.runtime.process_evidence import ProcessRef
        from workbench.tasks.repository import TaskRepository
        from workbench.terminal.shell_g2.prototype import ShellChoice
        sentinel = self.base / 'SHOULD_NEVER_EXECUTE'
        repo = TaskRepository(self.source / 'tasks.sqlite3')
        repo.set_session_settings({'fixture': 'I41'})
        repo.approve_scope('i41-task', 1, {'command': 'touch ' + str(sentinel)})
        repo.proceed('i41-task', 1, 'I41_HISTORICAL_PROCEED')
        run = repo.start_run('i41-task', 1, inputs={'worktree_path': '/fixed/original'})
        repo.record_shell_event(run, 'started', {'child_pid': 2000000299, 'child_start_ticks': 55})
        repo.record_shell_event(run, 'ended', {'exit_status': 0, 'history_only': True})
        original_decisions = repo.get_decisions('i41-task', 1)
        repo.close()
        old_boot = '00000000-0000-4000-8000-000000000001'
        life = LifecycleRecord('i41-task', 1, run, 'historical-approval', 1, old_boot, None,
            PeerRef('manager', 'old-manager', 1, ProcessRef('manager_omp', 2000000201, 72, 1)),
            PeerRef('worker', 'old-worker', 1, ProcessRef('worker_omp', 2000000202, 73, 1)),
            ProcessRef('host_shell', 2000000203, 74, 1),
            (ProcessRef('run_target', 2000000299, 55, 1),),
            ControlState('manager', 1, 'control_wait', False, False))
        self.write_json(self.source / 'lifecycle.json', {'version': 1, 'record': asdict(life)})
        self.write_json(self.source / 'automation.json', {'version': 1, 'paused': True})
        self.write_json(self.source / 'boot.json', {'version': 1, 'recorded_boot_id': old_boot,
            'confirmed_boot_id': old_boot, 'pending': True, 'pending_boot_id': old_boot,
            'reason': 'reboot', 'history': []})
        workflow = self.source / 'workflow'
        workflow.mkdir(mode=0o700)
        flow_task = FlowTask('i41-flow-task', 'experiment', status='running', message='historical start',
            summary='historical command', run_id=run, run_revision=1, task_message_id='historical-message',
            runs_started=1, spec={'goal': 'I41 history only', 'paths': [str(sentinel)]})
        (workflow / 'tasks-flow.jsonl').write_text(json.dumps({'type': 'task', 'task': asdict(flow_task)}) + '\n')
        (workflow / 'tasks-flow.jsonl').chmod(0o600)
        before = inventory(self.source)
        result = self.migrate()
        self.assertEqual(result[0], 0, result)
        self.assertEqual(inventory(self.source), before)
        self.assertFalse((self.target / 'lifecycle.json').exists())
        self.assertFalse((self.target / 'workflow/tasks-flow.jsonl').exists())
        self.assertEqual((self.target / 'migration-history/lifecycle.json').read_bytes(),
                         (self.source / 'lifecycle.json').read_bytes())
        fake = self.base / 'fake-omp'
        fake.write_text('#!' + sys.executable + '\n' + (ROOT / 'tests/recovery_boot/fake_omp.py').read_text())
        fake.chmod(0o700)
        records, notices, frames = (self.base / name for name in ('panes.jsonl', 'notices.jsonl', 'frames.jsonl'))
        environment = dict(self.env, FAKE_PANE_RECORD=str(records), FAKE_NOTICES=str(notices), FAKE_FRAMES=str(frames))
        plan = LaunchPlan(ShellChoice('bash', '/usr/bin/bash'), str(fake), 'omp/fixture', '/unused/bridge.ts', ())
        backend = Backend(DataLayout(self.target), plan, project_dir=str(self.project), environment=environment)
        cleanup = None
        try:
            with patch('workbench.backend.service.check_isolation',
                    side_effect=lambda command, **kw: {'role': kw['role'], 'state': 'ok', 'ok': True,
                        'leaks': [], 'warnings': [], 'error': None}):
                backend._reconcile_startup()
                self.assertTrue(backend.boot['confirmation_required'])
                self.assertEqual(backend.hold.reason_for(None), 'boot_confirmation_required')
                backend._open()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and backend.phase != 'ready':
                    backend._tick(.02)
                self.assertEqual(backend.phase, 'ready', backend.reason)
                self.assertTrue(backend.snapshot()['automation']['paused'])
                end = time.monotonic() + .5
                while time.monotonic() < end:
                    backend._tick(.02)
                self.assertEqual(backend.repository.get_current_run('i41-task')['run_id'], run)
                self.assertEqual(backend.repository.get_decisions('i41-task', 1), original_decisions)
                self.assertFalse(notices.exists(), 'archived current-run control must not create a notice')
                if frames.exists():
                    self.assertNotIn('"kind": "deliver"', frames.read_text())
                self.assertFalse(sentinel.exists())
        finally:
            backend._shutdown_confirmed = True
            cleanup = backend._close()
        self.assertTrue(cleanup['verified'], cleanup)
        self.assertEqual(inventory(self.source), before)
        self.evidence({'historical_run_id': run, 'decision_kinds': [d['kind'] for d in original_decisions],
            'boot_confirmation_held': True, 'paused_on_ready': True, 'no_notice_or_delivery': True,
            'sentinel_absent': not sentinel.exists(), 'shutdown': cleanup,
            'boundary': 'real backend/PTY/G3/host shell with existing fake OMP; no real provider/model turn'})
