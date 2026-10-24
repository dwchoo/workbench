"""Hub status aggregation: hub_v1 ``list`` and ``wb hub list`` (HB-05, seam S4 + S2).

A real ``_hub --foreground`` runs in a temp HOME/XDG sandbox with a short private
``XDG_RUNTIME_DIR`` (so no runtime ever falls back to ``/tmp/omp-workbench-<uid>``).
"On" projects are ``fixtures/contract_backend.py`` processes answering the snapshot the
test gives. Expected values come from the SPEC "상태 집계" tables and the test's own
snapshot values, paths and ``/proc`` reads. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'contract_backend.py'
sys.path.insert(0, str(REPO / 'src'))

from workbench.backend.project import initialize_project, resolve_project_context  # noqa: E402


def wait_until(predicate, timeout: float, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def proc_start_ticks(pid: int) -> int:
    return int(Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])


class ListSandbox:
    """Temp HOME/XDG dirs, a short private runtime dir and the processes one test owns."""

    def __init__(self, case: unittest.TestCase):
        self.case = case
        self.temp = tempfile.TemporaryDirectory(prefix='wb-hublist-')
        case.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        for name in ('home', 'state', 'config'):
            (self.base / name).mkdir(mode=0o700)
        self.run = Path(tempfile.mkdtemp(prefix='r', dir='/tmp')).resolve()
        os.chmod(self.run, 0o700)
        case.addCleanup(shutil.rmtree, self.run, ignore_errors=True)
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.base / 'home'), 'LANG': 'C.UTF-8',
                    'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'XDG_STATE_HOME': str(self.base / 'state'), 'XDG_RUNTIME_DIR': str(self.run),
                    'XDG_CONFIG_HOME': str(self.base / 'config'), 'WORKBENCH_HUB_AUTOSTART': '0'}
        self.state = self.base / 'state' / 'omp-workbench' / 'hub'
        self.registry_path = self.state / 'registry.json'
        self.processes: list[subprocess.Popen] = []
        case.addCleanup(self.cleanup)

    # -- processes ---------------------------------------------------------------------------
    def popen(self, argv: list[str], **kwargs) -> subprocess.Popen:
        kwargs.setdefault('stdout', subprocess.PIPE)
        kwargs.setdefault('stderr', subprocess.DEVNULL)
        process = subprocess.Popen(argv, env=self.env, cwd=self.base, stdin=subprocess.DEVNULL, text=True,
                                   start_new_session=True, **kwargs)
        self.processes.append(process)
        return process

    def start_hub(self) -> subprocess.Popen:
        process = self.popen([sys.executable, '-m', 'workbench', '_hub', '--foreground'])
        record = self.state / 'hub.json'

        def ready() -> bool:
            if process.poll() is not None:
                return True
            try:
                value = json.loads(record.read_text())
            except (OSError, ValueError):
                return False
            return value.get('pid') == process.pid and Path(value.get('socket', '')).exists()

        if not wait_until(ready, 10) or process.poll() is not None:
            self.case.fail(f'Hub did not become ready: rc={process.poll()}')
        return process

    def backend(self, root: Path, *, snapshot: dict | None = None, backend: dict | None = None,
                delay: float = 0.0, frames: Path | None = None, closing: str | None = None) -> subprocess.Popen:
        """A contract fixture backend serving ``root`` (``ready`` once connectable)."""
        argv = [sys.executable, str(FIXTURE), str(root), '--snapshot-json', json.dumps(snapshot or {}),
                '--backend-json', json.dumps(backend or {}), '--snapshot-delay', str(delay)]
        if frames is not None:
            argv += ['--frames-log', str(frames)]
        if closing is not None:
            argv += ['--closing-on-snapshot', closing]
        process = self.popen(argv)
        self.case.assertEqual(process.stdout.readline().strip(), 'ready')
        return process

    def wb(self, *args: str, env: dict | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, '-m', 'workbench', *args], env=env or self.env, cwd=self.base,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    def list_json(self) -> dict:
        result = self.wb('hub', 'list', '--json')
        self.case.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def hub_list(self, fresh: bool | None = None) -> dict:
        """hub_v1 ``list`` straight over the control socket (seam S2)."""
        from workbench.hub.client import HubClient
        from workbench.hub.paths import hub_layout
        fields = {} if fresh is None else {'fresh': fresh}
        with HubClient(hub_layout(self.env), timeout=30) as client:
            reply = client.request('list', **fields)
        self.case.assertTrue(reply.get('ok'), reply)
        return reply

    def cleanup(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                try:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                except ProcessLookupError:
                    pass
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    # -- projects and registry ---------------------------------------------------------------
    def folder(self, name: str) -> Path:
        path = self.base / 'work' / name
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def project(self, name: str) -> Path:
        root = self.folder(name)
        initialize_project(resolve_project_context(root, self.env), confirm=lambda _root: True,
                           interactive=True, environment=self.env)
        return root

    def marker_id(self, root: Path) -> str:
        return json.loads((root / '.wb' / 'project.json').read_text())['project_id']

    def register(self, *entries: tuple[Path, str]) -> None:
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        projects = [{'root': str(root), 'project_id': project_id, 'name': root.name,
                     'registered_at': 1.0, 'source': 'add'} for root, project_id in entries]
        self.registry_path.write_text(json.dumps({'schema_version': 1, 'projects': projects}))
        os.chmod(self.registry_path, 0o600)

    def register_projects(self, *roots: Path) -> None:
        self.register(*((root, self.marker_id(root)) for root in roots))


class ListCase(unittest.TestCase):
    def setUp(self):
        self.box = ListSandbox(self)

    def row(self, result: dict, root: Path) -> dict:
        matches = [row for row in result['rows'] if row['root'] == str(root)]
        self.assertEqual(len(matches), 1, result)
        return matches[0]


class EmptyRegistryTests(ListCase):
    def test_an_empty_registry_lists_no_rows(self):
        self.box.start_hub()
        result = self.box.list_json()
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['registry'], {'state': 'ok', 'path': str(self.box.registry_path)})
        self.assertIsInstance(result['observed_at'], (int, float))
        self.assertLess(abs(result['observed_at'] - time.time()), 30)


ROW_KEYS = {'root', 'name', 'project_id', 'power', 'power_detail', 'task', 'command', 'waits',
            'pending_requests', 'last_activity', 'backend', 'observed_at'}


class OffRowTests(ListCase):
    def test_a_project_without_lock_or_record_is_off_with_no_last_activity(self):
        root = self.box.project('alpha')
        self.box.register_projects(root)
        self.box.start_hub()
        row = self.row(self.box.list_json(), root)
        self.assertEqual(set(row), ROW_KEYS)
        self.assertEqual({key: row[key] for key in ROW_KEYS - {'observed_at'}},
                         {'root': str(root), 'name': 'alpha', 'project_id': self.box.marker_id(root),
                          'power': 'off', 'power_detail': None, 'task': None, 'command': None, 'waits': [],
                          'pending_requests': None, 'last_activity': None, 'backend': None})
        self.assertLess(abs(row['observed_at'] - time.time()), 30)

    def test_an_off_project_with_a_backend_record_reports_its_mtime(self):
        root = self.box.project('alpha')
        backend = self.box.backend(root)  # leaves a real backend.json behind once it ends
        backend.send_signal(signal.SIGTERM)
        backend.wait(timeout=5)
        record = root / '.wb' / 'backend.json'
        os.utime(record, (1_700_000_000.5, 1_700_000_000.5))
        self.box.register_projects(root)
        self.box.start_hub()
        row = self.row(self.box.list_json(), root)
        self.assertEqual((row['power'], row['power_detail'], row['backend']), ('off', None, None))
        self.assertEqual(row['last_activity'], os.stat(record).st_mtime)
        self.assertEqual(row['last_activity'], 1_700_000_000.5)


OTHER_ID = '00000000-0000-4000-8000-000000000001'


class MismatchRowTests(ListCase):
    def assert_mismatch(self, row: dict, detail: str):
        self.assertEqual({key: row[key] for key in ROW_KEYS - {'observed_at', 'root', 'name', 'project_id'}},
                         {'power': 'mismatch', 'power_detail': detail, 'task': None, 'command': None, 'waits': [],
                          'pending_requests': None, 'last_activity': None, 'backend': None})

    def test_the_four_mismatches_in_spec_order(self):
        gone = self.box.project('gone')
        gone_id = self.box.marker_id(gone)
        shutil.rmtree(gone)
        plain = self.box.folder('plain')
        broken = self.box.project('broken')
        broken_id = self.box.marker_id(broken)
        marker = broken / '.wb' / 'project.json'
        marker.write_text('{not json')
        os.chmod(marker, 0o600)
        other = self.box.project('other')
        self.box.register((gone, gone_id), (plain, OTHER_ID.replace('1', '2')), (broken, broken_id),
                          (other, OTHER_ID))
        self.box.start_hub()
        result = self.box.list_json()
        self.assertEqual([row['root'] for row in result['rows']], [str(gone), str(plain), str(broken), str(other)])
        self.assert_mismatch(self.row(result, gone), 'root_missing')
        self.assert_mismatch(self.row(result, plain), 'not_initialized')
        self.assert_mismatch(self.row(result, broken), 'marker_invalid')
        self.assert_mismatch(self.row(result, other), 'project_id_mismatch')
        self.assertEqual(self.row(result, other)['project_id'], OTHER_ID)

    def test_a_running_project_under_another_project_id_is_a_mismatch_not_on(self):
        root = self.box.project('alpha')
        self.box.backend(root)
        self.box.register((root, OTHER_ID))
        self.box.start_hub()
        self.assert_mismatch(self.row(self.box.list_json(), root), 'project_id_mismatch')


def read_frames(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class OnRowTests(ListCase):
    def test_a_running_fixture_backend_is_on_with_its_process_identity(self):
        root = self.box.project('alpha')
        backend = self.box.backend(root)
        self.box.register_projects(root)
        self.box.start_hub()
        row = self.row(self.box.list_json(), root)
        self.assertEqual((row['power'], row['power_detail']), ('on', None))
        self.assertEqual(row['backend'], {'pid': backend.pid, 'start_ticks': proc_start_ticks(backend.pid)})
        self.assertIsNone(row['pending_requests'])  # HB-AC-19: unknown until (A), not 0

    def test_the_observation_says_hello_as_workbench_hub_and_never_attaches(self):
        root = self.box.project('alpha')
        frames = self.box.base / 'frames.jsonl'
        self.box.backend(root, frames=frames)
        self.box.register_projects(root)
        self.box.start_hub()
        self.assertEqual(self.row(self.box.list_json(), root)['power'], 'on')
        headers = [item['header'] for item in read_frames(frames)]
        self.assertEqual([header['type'] for header in headers], ['hello', 'snapshot'])
        self.assertEqual(headers[0]['client'], 'workbench-hub')


TASK = {'task_id': 't-7', 'kind': 'experiment', 'status': 'running', 'summary': 'sweep lr',
        'since': '2026-10-25T01:02:03Z', 'active': True, 'held_reason': None}
COMMAND = {'command_id': 'c-3', 'owner': 'worker', 'label': 'python train.py', 'state': 'running'}
TASK_SINCE = 1_792_890_123.0  # 2026-10-25T01:02:03Z


class FieldMappingTests(ListCase):
    def test_task_command_and_last_activity_come_from_the_snapshot(self):
        root = self.box.project('alpha')
        self.box.backend(root, snapshot={'task': TASK, 'panes': {'host_shell': {'command': COMMAND}}},
                         backend={'started_at': 1_792_800_000.25})
        self.box.register_projects(root)
        self.box.start_hub()
        row = self.row(self.box.list_json(), root)
        self.assertEqual(row['power'], 'on')
        self.assertEqual(row['task'], {'task_id': 't-7', 'kind': 'experiment', 'status': 'running',
                                       'summary': 'sweep lr'})
        self.assertEqual(row['command'], COMMAND)
        self.assertEqual(row['last_activity'], TASK_SINCE)

    def test_a_closed_task_and_no_host_command_are_null_and_the_later_start_wins(self):
        root = self.box.project('alpha')
        self.box.backend(root, snapshot={'task': {**TASK, 'status': 'closed'},
                                         'panes': {'host_shell': {'command': None}}},
                         backend={'started_at': TASK_SINCE + 60.5})
        self.box.register_projects(root)
        self.box.start_hub()
        row = self.row(self.box.list_json(), root)
        self.assertEqual((row['power'], row['task'], row['command']), ('on', None, None))
        self.assertEqual(row['last_activity'], TASK_SINCE + 60.5)

    def test_no_task_and_no_started_at_leave_last_activity_null(self):
        root = self.box.project('alpha')
        self.box.backend(root, snapshot={'task': None})
        self.box.register_projects(root)
        self.box.start_hub()
        row = self.row(self.box.list_json(), root)
        self.assertEqual((row['power'], row['task'], row['command'], row['last_activity']),
                         ('on', None, None, None))


class WaitsTests(ListCase):
    def waits(self, snapshot: dict) -> list[dict]:
        root = self.box.project('alpha')
        self.box.backend(root, snapshot=snapshot)
        self.box.register_projects(root)
        self.box.start_hub()
        row = self.row(self.box.list_json(), root)
        self.assertEqual(row['power'], 'on')
        return row['waits']

    def test_every_wait_reason_appears_once_in_spec_order(self):
        holds = [{'reason': 'model_hold:worker', 'since': 30.0, 'detail': 'overloaded'},
                 {'reason': 'metadata_unavailable', 'since': 20.0, 'detail': None},
                 {'reason': 'model_hold:manager', 'since': 31.0, 'detail': None},
                 {'reason': 'boot_confirmation_required', 'since': 10.0, 'detail': 'boot_changed'}]
        waits = self.waits({'automation': {'paused': True, 'state': 'paused'}, 'holds': holds,
                            'recovery': {'report_wait': {'count': 2, 'reason': 'manager_editor_not_empty',
                                                         'since': 40.5}},
                            'task': {**TASK, 'status': 'held', 'held_reason': 'retry_limit'}})
        self.assertEqual([wait['code'] for wait in waits],
                         ['paused', 'boot_confirmation_required', 'manager_editor_not_empty',
                          'metadata_unavailable', 'model_hold', 'task_held'])
        by_code = {wait['code']: wait for wait in waits}
        self.assertEqual(by_code['boot_confirmation_required']['since'], 10.0)
        self.assertEqual(by_code['manager_editor_not_empty']['since'], 40.5)
        self.assertEqual(by_code['metadata_unavailable']['since'], 20.0)
        self.assertEqual(by_code['model_hold']['since'], 30.0)
        self.assertEqual(by_code['task_held']['detail'], 'retry_limit')
        self.assertEqual(by_code['task_held']['since'], TASK_SINCE)

    def test_a_manager_session_wait_is_its_own_code(self):
        waits = self.waits({'recovery': {'report_wait': {'count': 1, 'reason': 'manager_session_not_connected',
                                                         'since': 7.0}}})
        self.assertEqual([(wait['code'], wait['since']) for wait in waits], [('manager_session_not_connected', 7.0)])

    def test_no_reason_means_no_waits(self):
        self.assertEqual(self.waits({'automation': {'paused': False}, 'holds': [],
                                     'recovery': {'report_wait': None}, 'task': TASK}), [])


class SnapshotPowerTests(ListCase):
    def power(self, snapshot: dict) -> dict:
        root = self.box.project('alpha')
        self.box.backend(root, snapshot=snapshot)
        self.box.register_projects(root)
        self.box.start_hub()
        return self.row(self.box.list_json(), root)

    def test_a_shutdown_closing_hold_is_closing_even_while_starting(self):
        row = self.power({'phase': 'starting', 'task': TASK,
                          'holds': [{'reason': 'shutdown_closing', 'since': 5.0, 'detail': None}]})
        self.assertEqual((row['power'], row['power_detail']), ('closing', None))
        self.assertEqual(row['task']['task_id'], 't-7')
        self.assertIsNotNone(row['backend'])

    def test_a_backend_shutdown_closing_frame_instead_of_a_snapshot_is_closing(self):
        root = self.box.project('alpha')
        backend = self.box.backend(root, closing='backend_shutdown')
        self.box.register_projects(root)
        self.box.start_hub()
        row = self.row(self.box.list_json(), root)
        self.assertEqual((row['power'], row['power_detail']), ('closing', None))
        self.assertEqual(row['backend'], {'pid': backend.pid, 'start_ticks': proc_start_ticks(backend.pid)})

    def test_a_starting_phase_is_starting_with_its_snapshot_fields(self):
        row = self.power({'phase': 'starting', 'task': TASK})
        self.assertEqual((row['power'], row['power_detail']), ('starting', None))
        self.assertEqual(row['task']['status'], 'running')


class UnknownRowTests(ListCase):
    """Observation failures are "연결 없음/불명", never ``off`` (SPEC 불변식 3)."""

    def assert_unknown(self, row: dict, detail: str):
        self.assertEqual({key: row[key] for key in ROW_KEYS - {'observed_at', 'root', 'name', 'project_id'}},
                         {'power': 'unknown', 'power_detail': detail, 'task': None, 'command': None, 'waits': [],
                          'pending_requests': None, 'last_activity': None, 'backend': None})

    def listed(self, root: Path) -> dict:
        self.box.register_projects(root)
        self.box.start_hub()
        return self.row(self.box.list_json(), root)

    def test_a_snapshot_slower_than_two_seconds_is_unknown_timeout(self):
        root = self.box.project('alpha')
        self.box.backend(root, snapshot={'task': TASK}, delay=3.5)
        self.assert_unknown(self.listed(root), 'timeout')

    def test_a_snapshot_naming_another_pid_is_unknown_snapshot_mismatch(self):
        root = self.box.project('alpha')
        self.box.backend(root, backend={'pid': 1})
        self.assert_unknown(self.listed(root), 'snapshot_mismatch')

    def test_a_snapshot_naming_another_data_dir_is_unknown_snapshot_mismatch(self):
        root = self.box.project('alpha')
        self.box.backend(root, backend={'data_dir': str(self.box.base / 'elsewhere' / '.wb')})
        self.assert_unknown(self.listed(root), 'snapshot_mismatch')

    def test_a_snapshot_of_another_project_identity_is_unknown_snapshot_mismatch(self):
        root = self.box.project('alpha')
        self.box.backend(root, backend={'boot_id': 'another-boot'})
        self.assert_unknown(self.listed(root), 'snapshot_mismatch')

    def test_a_running_record_whose_socket_is_gone_is_unknown_unreachable(self):
        root = self.box.project('alpha')
        self.box.backend(root)
        record = json.loads((root / '.wb' / 'backend.json').read_text())
        os.unlink(record['ui_socket'])
        self.assert_unknown(self.listed(root), 'unreachable')

    def test_an_unreadable_backend_record_is_unknown_identity_reason(self):
        root = self.box.project('alpha')
        record = root / '.wb' / 'backend.json'
        record.write_text('{broken')
        os.chmod(record, 0o600)
        self.assert_unknown(self.listed(root), 'identity:record_unreadable')

    def test_a_held_lock_with_a_dead_recorded_process_is_unknown_identity_reason(self):
        root = self.box.project('alpha')
        backend = self.box.backend(root)
        backend.send_signal(signal.SIGTERM)
        backend.wait(timeout=5)
        holder = self.box.popen([sys.executable, '-c',
                                 'import fcntl,os,sys,time\n'
                                 'fd=os.open(sys.argv[1],os.O_RDWR)\n'
                                 'fcntl.flock(fd,fcntl.LOCK_EX)\n'
                                 'print("held",flush=True)\n'
                                 'time.sleep(120)\n', str(root / '.wb' / 'backend.lock')])
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        self.assert_unknown(self.listed(root), 'identity:process_mismatch_or_unknown')

    def test_a_held_lock_without_a_record_is_starting_not_off(self):
        root = self.box.project('alpha')
        holder = self.box.popen([sys.executable, '-c',
                                 'import fcntl,os,sys,time\n'
                                 'fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600)\n'
                                 'fcntl.flock(fd,fcntl.LOCK_EX)\n'
                                 'print("held",flush=True)\n'
                                 'time.sleep(120)\n', str(root / '.wb' / 'backend.lock')])
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        row = self.listed(root)
        self.assertEqual((row['power'], row['power_detail'], row['backend'], row['last_activity']),
                         ('starting', None, None, None))


class IsolationTests(ListCase):
    def test_slow_rows_are_observed_in_parallel_and_do_not_change_the_others(self):
        slow = [self.box.project(f'slow{number}') for number in range(3)]
        fast = self.box.project('fast')
        for root in slow:
            self.box.backend(root, delay=6.0)
        self.box.backend(fast, snapshot={'task': TASK})
        self.box.register_projects(*slow, fast)
        self.box.start_hub()
        started = time.monotonic()
        result = self.box.hub_list(fresh=True)
        elapsed = time.monotonic() - started
        # Serial observation would take at least 3 x 2 s; each row is bounded by 2 s.
        self.assertLess(elapsed, 4.0)
        for root in slow:
            self.assertEqual((self.row(result, root)['power'], self.row(result, root)['power_detail']),
                             ('unknown', 'timeout'))
        row = self.row(result, fast)
        self.assertEqual((row['power'], row['task']['task_id']), ('on', 't-7'))

    def test_a_row_queued_behind_eight_slow_rows_is_still_observed(self):
        slow = [self.box.project(f'slow{number}') for number in range(8)]
        fast = self.box.project('fast')
        for root in slow:
            self.box.backend(root, delay=6.0)
        self.box.backend(fast, snapshot={'task': TASK})
        self.box.register_projects(*slow, fast)
        self.box.start_hub()
        result = self.box.hub_list(fresh=True)
        self.assertEqual({self.row(result, root)['power_detail'] for root in slow}, {'timeout'})
        self.assertEqual(self.row(result, fast)['power'], 'on')


class CacheTests(ListCase):
    def connections(self, frames: Path) -> int:
        return max((item['connection'] for item in read_frames(frames)), default=0)

    def test_repeated_lists_within_two_seconds_reuse_the_observation_and_fresh_observes_again(self):
        root = self.box.project('alpha')
        frames = self.box.base / 'frames.jsonl'
        self.box.backend(root, frames=frames)
        self.box.register_projects(root)
        self.box.start_hub()
        first = self.box.hub_list()
        self.assertEqual(self.row(first, root)['power'], 'on')
        self.assertEqual(self.connections(frames), 1)
        second = self.box.hub_list(fresh=False)
        self.assertEqual(self.connections(frames), 1)
        self.assertEqual(self.row(second, root)['observed_at'], self.row(first, root)['observed_at'])
        third = self.box.hub_list(fresh=True)
        self.assertEqual(self.connections(frames), 2)
        self.assertGreater(self.row(third, root)['observed_at'], self.row(first, root)['observed_at'])
        time.sleep(2.2)
        self.box.hub_list()
        self.assertEqual(self.connections(frames), 3)

    def test_the_registry_is_read_again_for_every_list_even_within_the_cache(self):
        alpha, beta = self.box.project('alpha'), self.box.project('beta')
        self.box.register_projects(alpha)
        self.box.start_hub()
        self.assertEqual([row['root'] for row in self.box.hub_list()['rows']], [str(alpha)])
        self.box.register_projects(alpha, beta)
        self.assertEqual([row['root'] for row in self.box.hub_list()['rows']], [str(alpha), str(beta)])
        self.box.register_projects(beta)
        self.assertEqual([row['root'] for row in self.box.hub_list()['rows']], [str(beta)])


class TextAndRegistryTests(ListCase):
    def test_text_output_has_the_tui_columns_in_order_and_dashes(self):
        alpha = self.box.project('alpha')
        self.box.backend(alpha, snapshot={'task': TASK, 'panes': {'host_shell': {'command': COMMAND}},
                                          'automation': {'paused': True}})
        beta = self.box.project('beta')
        self.box.register_projects(alpha, beta)
        self.box.start_hub()
        result = self.box.wb('hub', 'list')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0].split(), ['이름', '경로', '상태', 'Task·단계', '명령', '대기', '사유', '요청',
                                            '마지막', '활동'])
        header = lines[0]
        positions = [header.index(column) for column in
                     ('이름', '경로', '상태', 'Task·단계', '명령', '대기 사유', '요청', '마지막 활동')]
        self.assertEqual(positions, sorted(positions))
        alpha_line = next(line for line in lines[1:] if line.startswith('alpha'))
        for text in (str(alpha), '켜짐', 'sweep lr · running', 'python train.py (실행 중)', '일시정지', '—'):
            self.assertIn(text, alpha_line)
        beta_line = next(line for line in lines[1:] if line.startswith('beta'))
        self.assertIn('꺼짐', beta_line)
        self.assertNotIn(' 0 ', beta_line)
        self.assertEqual(len(lines), 3)

    def test_an_initialized_but_unregistered_project_is_not_listed(self):
        alpha = self.box.project('alpha')
        self.box.project('hidden')
        self.box.register_projects(alpha)
        self.box.start_hub()
        self.assertEqual([row['root'] for row in self.box.list_json()['rows']], [str(alpha)])

    def test_a_damaged_registry_is_invalid_and_wb_hub_list_exits_1(self):
        self.box.state.mkdir(parents=True, mode=0o700)
        self.box.registry_path.write_text('{damaged')
        os.chmod(self.box.registry_path, 0o600)
        self.box.start_hub()
        listed = self.box.hub_list()
        self.assertEqual((listed['registry'], listed['rows']),
                         ({'state': 'invalid', 'path': str(self.box.registry_path)}, []))
        for args in (('hub', 'list'), ('hub', 'list', '--json')):
            result = self.box.wb(*args)
            self.assertEqual(result.returncode, 1, (args, result.stdout, result.stderr))
            self.assertIn(str(self.box.registry_path), result.stdout + result.stderr)
        self.assertEqual(json.loads(self.box.wb('hub', 'list', '--json').stdout)['registry']['state'], 'invalid')
        self.assertEqual(self.box.registry_path.read_text(), '{damaged')

    def test_without_a_hub_and_with_autostart_off_wb_hub_list_exits_1(self):
        result = self.box.wb('hub', 'list')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Hub 실행 중 아님', result.stderr)

    def test_wb_hub_list_starts_the_hub_when_none_runs(self):
        env = {**self.box.env, 'WORKBENCH_HUB_AUTOSTART': '1'}
        self.addCleanup(lambda: self.box.wb('hub', 'stop', '--yes'))
        result = self.box.wb('hub', 'list', '--json', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['rows'], [])
        hub = json.loads((self.box.state / 'hub.json').read_text())
        stopped = self.box.wb('hub', 'stop', '--yes')
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertTrue(wait_until(lambda: not Path(f'/proc/{hub["pid"]}').exists(), 10))


if __name__ == '__main__':
    unittest.main()
