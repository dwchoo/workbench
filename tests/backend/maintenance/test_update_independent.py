"""요구에서 도출한 update 거절·실패 경계의 독립 관찰.

공개 cli.main을 사용한다. 기존 fixture는 uv/Git/fresh-interpreter와 RPC 및
process 탐색 경계만 대체한다. 실제 private sleeper PID·원본 bytes·manifest로
결과를 확인하며 실제 설치/OMP/backend runtime coverage를 주장하지 않는다.
"""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import unittest

from tests.backend.maintenance import test_update_cli as support
from workbench.contracts.ui_v1 import ClientType


class Tty(io.StringIO):
    def isatty(self):
        return True


class InterruptedTty(Tty):
    def readline(self, *args):
        raise KeyboardInterrupt


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


class UpdateIndependent(unittest.TestCase):
    def setUp(self):
        self.fixture = support.InstalledFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def project(self, active=None):
        return self.fixture.project('independent-project',
            [{'kind': 'task_run', 'task_id': 'task-a', 'run_id': 'run-a'}]
            if active is None else active)

    def manifests(self):
        return list((self.fixture.home/'state/omp-workbench/updates').glob('*/manifest.json'))

    def assert_untouched(self, data, backend, before):
        self.assertIsNone(backend.poll())
        self.assertEqual(hashes(data), before)
        self.assertFalse(self.fixture.applied.exists())
        self.assertEqual(self.manifests(), [])

    def test_eof_interrupt_and_noninteractive_yes_do_not_authorize_shutdown(self):
        # Supported producers: terminal EOF/Ctrl-C and redirected stdin containing y.
        _, data, backend = self.project()
        before = hashes(data)
        for stream in (Tty(''), InterruptedTty(), io.StringIO('y\n')):
            with self.subTest(stream=type(stream).__name__):
                status, output, error = self.fixture.invoke(stdin=stream)
                self.assertEqual(status, 1, output+error)
                self.assertIn('업데이트 취소', output)
                self.assert_untouched(data, backend, before)

    def test_unknown_uv_receipt_source_is_refused_before_stopping_work(self):
        _, data, backend = self.project()
        before = hashes(data)
        (self.fixture.prefix/'uv-receipt.toml').write_text(
            '[tool]\nrequirements = [{name="workbench", git="https://different.invalid/wb.git?rev='
            + self.fixture.old + '"}]\n')
        status, output, error = self.fixture.invoke(stdin=Tty('y\n'))
        self.assertEqual(status, 1, output+error)
        self.assertIn('uv 관리 Git 설치', error)
        self.assert_untouched(data, backend, before)

    def test_uv_disappearing_before_invocation_is_refused_before_stopping_work(self):
        _, data, backend = self.project()
        before = hashes(data)
        # Keep installed metadata; the selected executable is no longer available.
        original = self.fixture.tool
        def unavailable(argv, **kwargs):
            if list(map(str, argv))[1:] == ['tool', 'dir']:
                raise FileNotFoundError('uv disappeared before invocation')
            return original(argv, **kwargs)
        status, output, error = self.fixture.invoke(stdin=Tty('y\n'), tool=unavailable)
        self.assertEqual(status, 1, output+error)
        self.assertIn('uv 관리 Git 설치', error)
        self.assert_untouched(data, backend, before)

    def test_missing_ref_resolution_fails_before_stopping_work(self):
        _, data, backend = self.project()
        before = hashes(data)
        def missing(argv, **kwargs):
            if 'rev-parse' in list(map(str, argv)):
                return subprocess.CompletedProcess(argv, 128, '', 'missing ref')
            return self.fixture.tool(argv, **kwargs)
        status, output, error = self.fixture.invoke(('update', '--ref', 'absent'),
            stdin=Tty('y\n'), tool=missing)
        self.assertEqual(status, 1, output+error)
        self.assertIn('Git ref', error)
        self.assert_untouched(data, backend, before)

    def test_previous_survivor_prevents_even_an_approval_prompt_from_stopping_work(self):
        _, data, backend = self.project([{'kind': 'previous_survivor', 'pid': 765432}])
        before = hashes(data)
        status, output, error = self.fixture.invoke(stdin=Tty('y\n'))
        self.assertEqual(status, 1, output+error)
        self.assertIn('생존', error)
        self.assert_untouched(data, backend, before)

    def test_expired_shutdown_confirmation_does_not_install_or_stop_backend(self):
        _, data, backend = self.project()
        before = hashes(data)
        client = next(iter(self.fixture.clients.values()))
        original = client.request
        def expired(kind, **kwargs):
            if kind == ClientType.SHUTDOWN_CONFIRM:
                return {'ok': False}
            return original(kind, **kwargs)
        client.request = expired
        status, output, error = self.fixture.invoke(stdin=Tty('y\n'))
        self.assertEqual(status, 1, output+error)
        self.assertIsNone(backend.poll())
        self.assertEqual(hashes(data), before)
        self.assertFalse(self.fixture.applied.exists())
        manifest = json.loads(self.manifests()[0].read_text())
        self.assertFalse(manifest['backup_complete'])
        self.assertEqual(manifest['installation'], 'not_started')

    def test_unverified_shutdown_result_stops_before_backup_or_install(self):
        _, data, backend = self.project([])
        client = next(iter(self.fixture.clients.values()))
        original = client.request
        def unverified(kind, **kwargs):
            response = original(kind, **kwargs)
            if kind == ClientType.SHUTDOWN_CONFIRM:
                client.closing = {'result': {'verified': False, 'left_running': ['unknown']}}
            return response
        client.request = unverified
        status, output, error = self.fixture.invoke()
        self.assertEqual(status, 1, output+error)
        self.assertIsNotNone(backend.poll())
        self.assertFalse(self.fixture.applied.exists())
        manifest_path = self.manifests()[0]
        manifest = json.loads(manifest_path.read_text())
        self.assertFalse(manifest['backup_complete'])
        self.assertEqual(manifest['installation'], 'not_started')
        self.assertFalse((manifest_path.parent/'projects').exists())

    def test_corrupt_required_database_prevents_install_and_preserves_original(self):
        # A truncated/corrupted WB database is an explicitly unverified backup input.
        _, data, backend = self.project([])
        database = data/'records.sqlite3'
        database.write_bytes(b'SQLite format 3\0TRUNCATED_WB_DATABASE')
        expected = database.read_bytes()
        status, output, error = self.fixture.invoke()
        self.assertEqual(status, 1, output+error)
        self.assertIsNotNone(backend.poll())
        self.assertFalse(self.fixture.applied.exists())
        self.assertEqual(database.read_bytes(), expected)
        self.assertFalse(Path(str(database)+'-wal').exists())
        self.assertFalse(Path(str(database)+'-shm').exists())
        manifest = json.loads(self.manifests()[0].read_text())
        self.assertFalse(manifest['backup_complete'])
        self.assertFalse(manifest['complete'])
        self.assertEqual(manifest['installation'], 'not_started')

    def test_backup_root_inside_project_is_refused_before_shutdown(self):
        project, data, backend = self.project([])
        before = hashes(data)
        self.fixture.env['XDG_STATE_HOME'] = str(project/'state')
        status, output, error = self.fixture.invoke()
        self.assertEqual(status, 1, output+error)
        self.assertIn('겹칩니다', error)
        self.assertIsNone(backend.poll())
        self.assertEqual(hashes(data), before)
        self.assertFalse((project/'state').exists())
        self.assertFalse(self.fixture.applied.exists())

    def test_new_interpreter_verification_failure_is_not_reported_as_success(self):
        def wrong_install(argv, **kwargs):
            if '-c' in list(map(str, argv)):
                return subprocess.CompletedProcess(argv, 1, '{"verified": false}', '')
            return self.fixture.tool(argv, **kwargs)
        status, output, error = self.fixture.invoke(tool=wrong_install)
        self.assertEqual(status, 1, output+error)
        self.assertTrue(self.fixture.applied.exists())
        self.assertNotIn('업데이트 완료', output)
        self.assertIn(self.fixture.old, error)
        self.assertIn(self.fixture.target, error)
        self.assertIn('수동 재설치', error)
        manifest = json.loads(self.manifests()[0].read_text())
        self.assertTrue(manifest['backup_complete'])
        self.assertEqual(manifest['uv_returncode'], 0)
        self.assertEqual(manifest['installation'], 'unknown')
        self.assertFalse(manifest['complete'])

    def test_legacy_backend_cannot_accept_changed_work_under_previous_consent(self):
        # Supported producer: a still-running pre-update a6d877f backend. Its
        # SHUTDOWN_REQUEST returns token/active/expires_in/processes; CONFIRM
        # checks token+TTL, without binding active work. Current discover accepts
        # its ordinary runtime binding/process record. Do not contact a real one.
        work = [{'kind': 'task_run', 'task_id': 'task-a', 'run_id': 'run-a'}]
        _, data, backend = self.project(work)
        before = hashes(data)
        client = next(iter(self.fixture.clients.values()))
        original_request, original_snapshot = client.request, client.snapshot
        accepted = []
        def legacy_request(kind, **kwargs):
            response = original_request(kind, **kwargs)
            if kind == ClientType.SHUTDOWN_REQUEST:
                # Exact a6d877f controller fields plus its successful RPC wrapper;
                # current/future fixture capabilities must never leak into legacy.
                return {'ok': True, 'token': response['token'],
                    'active': [dict(item) for item in work], 'expires_in': 30,
                    'processes': response['processes']}
            # a6d877f's real response has no ok/support marker; absence of ok
            # must not silently grant stronger current-server consent semantics.
            accepted.extend(dict(item) for item in work)
            return {'shutting_down': True, 'result_deadline': 60}
        def snapshot():
            result = original_snapshot()
            result['task'] = {key: work[0][key] for key in ('task_id', 'run_id')}
            return result
        client.request, client.snapshot = legacy_request, snapshot
        class ChangedWorkTty(Tty):
            def readline(self, *args):
                work[:] = [{'kind': 'task_run', 'task_id': 'task-b', 'run_id': 'run-b'}]
                return 'y\n'
        status, output, error = self.fixture.invoke(stdin=ChangedWorkTty())
        observation = {'status': status, 'accepted': accepted,
            'backend_exit': backend.poll(), 'installer_applied': self.fixture.applied.exists(),
            'displayed_a': 'task-a' in output, 'displayed_b': 'task-b' in output}
        self.assertEqual(status, 1, json.dumps(observation))
        self.assertIsNone(backend.poll(), json.dumps(observation))
        self.assertFalse(self.fixture.applied.exists(), json.dumps(observation))
        self.assertEqual(hashes(data), before)


class ShutdownAutomationIndependent(unittest.TestCase):
    def test_unchanged_active_experiment_consent_survives_own_shutdown_hold_tick(self):
        # Real supported producer: TaskWorkflow starts an approved experiment and
        # AutomationController.experiment_started binds it. Reuse the established
        # standalone automation harness (real G3/socket and host shell; scripted
        # peers, no OMP/provider/backend process). Its ExitStack owns all resources.
        from tests.backend import test_automation_loop as automation_support
        from workbench.backend.launcher import LaunchPlan
        from workbench.backend.paths import DataLayout
        from workbench.backend.service import Backend
        from workbench.backend.ui_server import Held
        from workbench.terminal.shell_g2.prototype import ShellChoice

        fixture = automation_support.AutomationFixture()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        run = fixture.start_experiment()
        fixture.controller.experiment_started(run)
        plan = LaunchPlan(ShellChoice('sh', '/bin/sh'), '/not-executed/omp',
            'fixture', '/not-executed/bridge')
        backend = Backend(DataLayout(fixture.root/'backend'), plan,
            project_dir=str(fixture.project), environment={})
        backend.automation_loop = fixture.controller
        fixture.controller._hold = lambda: backend._hold_reason('worker')
        class TaskState:
            def task_view(self):
                return {'task_id': run.task_id, 'run_id': run.run_id}
        backend.flow = TaskState()
        interleaves = []
        class Probe:
            def probe(self, role, **kwargs):
                # Deterministic scheduling of the normal automation loop during
                # the confirm's bounded OMP probe wait, after SHUTDOWN_HOLD set.
                if backend._hold_reason('worker') is not None and not interleaves:
                    interleaves.append(fixture.controller.tick())
                return fixture.bridge.probe(role, **kwargs)
        backend.bridge = Probe()
        before = fixture.controller.status()
        self.assertEqual(before['state'], 'active', before)
        pending = backend.shutdown_request()
        self.assertIn({'kind': 'automation', 'state': 'active'}, pending['active'])
        processes = backend.process_refs()
        failure = None
        try:
            response = backend.shutdown_confirm(pending['token'])
        except Held as error:
            failure = str(error)
            response = None
        after = fixture.controller.status()
        self.assertEqual(after['run'], before['run'])
        self.assertEqual(backend.process_refs(), processes)
        self.assertEqual(len(interleaves), 1)
        self.assertIsNone(failure, json.dumps({'failure': failure,
            'before': before['state'], 'after': after['state'],
            'tick': interleaves[0], 'same_run': after['run'] == before['run']}))
        self.assertTrue(response['shutting_down'])
