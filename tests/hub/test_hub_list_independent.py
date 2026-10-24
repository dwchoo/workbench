"""Independent verification of Hub status aggregation, hub_v1 ``list`` and ``wb hub list`` (HB-05, seam S4).

Expected values come from the SPEC "상태 집계" mapping table and its invariant "연결 없음/불명 != 꺼짐",
from the snapshots this module hands the contract fixture backend (``fixtures/contract_backend.py``)
and from what the fixture records about the connections it received. Not from the worker's tests.

Every Hub is a real ``wb _hub --foreground`` subprocess in a temp HOME/XDG sandbox. Each sandbox has
its own short private 0700 ``XDG_RUNTIME_DIR`` (so no runtime path ever falls back to the real
``/tmp/omp-workbench-<uid>``) and ``WORKBENCH_HUB_AUTOSTART=0`` unless a test needs the auto start.
Signals go only to processes this module started, after re-checking pid + start ticks.

Run with ``discover -s tests/hub -p 'test_hub_list_independent.py'``.
"""
from __future__ import annotations

import calendar
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'contract_backend.py'
sys.path.insert(0, str(REPO / 'src'))

from workbench.backend.project import initialize_project, resolve_project_context  # noqa: E402

OTHER_ID = '00000000-0000-4000-8000-0000000000aa'
ROW_KEYS = {'root', 'name', 'project_id', 'power', 'power_detail', 'task', 'command', 'waits',
            'pending_requests', 'last_activity', 'backend', 'observed_at'}
UNKNOWN_DETAILS = {'unreachable', 'timeout', 'snapshot_mismatch'}
COLUMNS = ['이름', '경로', '상태', 'Task·단계', '명령', '대기 사유', '요청', '마지막 활동']


def wait_until(predicate, timeout: float, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def proc_start_ticks(pid: int) -> int | None:
    try:
        return int(Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def proc_alive(pid: int) -> bool:
    try:
        state = Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in (b'Z', b'X')


def read_frames(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def connections(path: Path) -> dict[int, list[dict]]:
    """Frames the fixture received, grouped by connection number."""
    grouped: dict[int, list[dict]] = {}
    for item in read_frames(path):
        grouped.setdefault(item['connection'], []).append(item['header'])
    return grouped


# A backend that holds the lock and the real record like the fixture but misbehaves on the wire,
# or (mode "squat") a stranger that takes over the recorded ui socket of a running fixture.
MISBEHAVING = textwrap.dedent(
    """
    import fcntl, json, os, socket, sys, threading, time
    from pathlib import Path
    from workbench.backend.project import resolve_project_context
    from workbench.backend.runtime_paths import ensure_runtime_root
    from workbench.contracts.ui_v1 import FrameDecoder, encode_frame

    mode, project = sys.argv[1], Path(sys.argv[2])
    context = resolve_project_context(project, dict(os.environ))
    data, runtime = context.data_root, context.runtime_root
    if mode == "squat":
        record = json.loads((data / "backend.json").read_text())
        ref = record["processes"]["backend"]
        snapshot = {"phase": "ready", "reason": None, "panes": {}, "attached": False,
                    "backend": dict(record, pid=ref["pid"], process=ref)}
        sock_path = record["ui_socket"]
        os.unlink(sock_path)
    else:
        lock = os.open(data / "backend.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        ticks = int(Path(f"/proc/{os.getpid()}/stat").read_bytes().rsplit(b") ", 1)[1].split()[19])
        marker = json.loads((data / "project.json").read_text())
        ensure_runtime_root(runtime, data, marker["project_id"])
        ref = {"pid": os.getpid(), "start_ticks": ticks}
        record = {"data_dir": str(data), "project_dir": str(context.project_root), "runtime_root": str(runtime),
                  "project_id": marker["project_id"], "uid": os.geteuid(),
                  "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                  "ui_socket": str(runtime / "ui.sock"), "bridge_socket": str(runtime / "bridge.sock"),
                  "processes": {"backend": ref}}
        snapshot = {"phase": "ready", "reason": None, "panes": {}, "attached": False,
                    "backend": dict(record, pid=os.getpid(), process=ref)}
        sock_path = record["ui_socket"]

    def serve(connection):
        with connection:
            if mode == "close_on_connect":
                return
            decoder = FrameDecoder()
            while chunk := connection.recv(65536):
                for frame in decoder.feed(chunk):
                    kind = frame.header["type"]
                    if kind == "hello":
                        if mode == "hang_hello":
                            time.sleep(60)
                            return
                        if mode == "garbage_welcome":
                            connection.sendall(b"this is not json at all\\n")
                            return
                        connection.sendall(encode_frame({"v": 1, "type": "welcome", "version": 1}))
                        continue
                    if mode == "close_on_snapshot":
                        return
                    reply = {"v": 1, "type": "result", "id": frame.header["id"], "ok": True, "snapshot": snapshot}
                    if mode == "not_ok":
                        reply = {"v": 1, "type": "result", "id": frame.header["id"], "ok": False, "reason": "boom"}
                    elif mode == "snapshot_not_dict":
                        reply["snapshot"] = "not a dict"
                    elif mode == "no_backend_key":
                        reply["snapshot"] = {"phase": "ready"}
                    elif mode == "garbage_snapshot":
                        connection.sendall(b"{{{{ nope\\n")
                        return
                    connection.sendall(encode_frame(reply))

    with socket.socket(socket.AF_UNIX) as server:
        server.bind(sock_path)
        server.listen()
        if mode != "squat":
            temporary = data / "fixture-record"
            temporary.write_text(json.dumps(record))
            temporary.chmod(0o600)
            temporary.replace(data / "backend.json")
        print("ready", flush=True)
        while True:
            connection, _ = server.accept()
            threading.Thread(target=serve, args=(connection,), daemon=True).start()
    """
)

# Holds the project's backend.lock (an exclusive flock) and nothing else.
LOCK_HOLDER = textwrap.dedent(
    """
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    print("ready", flush=True)
    time.sleep(600)
    """
)


class Sandbox:
    """Temp HOME/XDG dirs, a short private runtime dir and the processes one test owns."""

    def __init__(self, case: unittest.TestCase, *, autostart: bool = False):
        self.case = case
        self.temp = tempfile.TemporaryDirectory(prefix='wb-hli-')
        case.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        for name in ('home', 'state', 'config', 'work'):
            (self.base / name).mkdir(mode=0o700)
        # Short on purpose: a long runtime path would fall back to the real /tmp/omp-workbench-<uid>.
        self.runtime = Path(tempfile.mkdtemp(prefix='r', dir='/tmp')).resolve()
        os.chmod(self.runtime, 0o700)
        case.addCleanup(shutil.rmtree, self.runtime, True)
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.base / 'home'), 'LANG': 'C.UTF-8',
                    'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'XDG_STATE_HOME': str(self.base / 'state'), 'XDG_CONFIG_HOME': str(self.base / 'config'),
                    'XDG_RUNTIME_DIR': str(self.runtime)}
        if not autostart:
            self.env['WORKBENCH_HUB_AUTOSTART'] = '0'
        self.state = self.base / 'state' / 'omp-workbench' / 'hub'
        self.registry_path = self.state / 'registry.json'
        self.owned: list[tuple[subprocess.Popen, int | None]] = []
        case.addCleanup(self.cleanup)

    # -- processes ------------------------------------------------------------------------------
    def popen(self, argv: list[str]) -> subprocess.Popen:
        process = subprocess.Popen(argv, env=self.env, cwd=self.base, stdin=subprocess.DEVNULL, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.owned.append((process, proc_start_ticks(process.pid)))
        return process

    def cleanup(self) -> None:
        leaked = []
        for process, ticks in self.owned:
            if process.poll() is None and proc_start_ticks(process.pid) == ticks:
                try:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                except ProcessLookupError:
                    pass
            if process.stdout is not None:
                process.stdout.close()
            if proc_alive(process.pid) and proc_start_ticks(process.pid) == ticks:
                leaked.append(process.pid)
        self.case.assertEqual(leaked, [], 'sandbox processes outlived their cleanup')

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
                delay: float = 0.0, frames: Path | None = None) -> subprocess.Popen:
        argv = [sys.executable, str(FIXTURE), str(root), '--snapshot-json', json.dumps(snapshot or {}),
                '--backend-json', json.dumps(backend or {}), '--snapshot-delay', str(delay)]
        if frames is not None:
            argv += ['--frames-log', str(frames)]
        process = self.popen(argv)
        self.case.assertEqual(process.stdout.readline().strip(), 'ready')
        return process

    def misbehaving(self, mode: str, root: Path) -> subprocess.Popen:
        script = self.base / 'misbehaving.py'
        script.write_text(MISBEHAVING)
        process = self.popen([sys.executable, str(script), mode, str(root)])
        self.case.assertEqual(process.stdout.readline().strip(), 'ready')
        return process

    def lock_holder(self, root: Path) -> subprocess.Popen:
        process = self.popen([sys.executable, '-c', LOCK_HOLDER, str(root / '.wb' / 'backend.lock')])
        self.case.assertEqual(process.stdout.readline().strip(), 'ready')
        return process

    def wb(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, '-m', 'workbench', *args], env=self.env, cwd=self.base,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    # -- Hub requests -----------------------------------------------------------------------------
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

    # -- projects and registry ----------------------------------------------------------------------
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

    def write_registry(self, *entries: tuple[Path, str]) -> None:
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        projects = [{'root': str(root), 'project_id': project_id, 'name': root.name,
                     'registered_at': 1.0, 'source': 'add'} for root, project_id in entries]
        self.registry_path.write_text(json.dumps({'schema_version': 1, 'projects': projects}))
        os.chmod(self.registry_path, 0o600)

    def register(self, *roots: Path) -> None:
        self.write_registry(*((root, self.marker_id(root)) for root in roots))

    def record_path(self, root: Path) -> Path:
        return root / '.wb' / 'backend.json'


class Case(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)

    def row(self, result: dict, root: Path) -> dict:
        matches = [row for row in result['rows'] if row['root'] == str(root)]
        self.assertEqual(len(matches), 1, result)
        return matches[0]

    def observe(self, *roots: Path, fresh: bool | None = None) -> dict:
        """Register ``roots``, start a Hub and return its ``list`` result."""
        self.box.register(*roots)
        self.box.start_hub()
        return self.box.hub_list(fresh)

    def one(self, root: Path) -> dict:
        return self.row(self.observe(root), root)

    def assert_unknown(self, row: dict, details=UNKNOWN_DETAILS) -> None:
        """The invariant: an unobserved project is unknown with every other field null/empty, never off."""
        self.assertNotEqual(row['power'], 'off', row)
        self.assertEqual(row['power'], 'unknown', row)
        if isinstance(details, str):
            self.assertEqual(row['power_detail'], details, row)
        else:
            self.assertIn(row['power_detail'], details, row)
        self.assertEqual({key: row[key] for key in ROW_KEYS - {'observed_at', 'root', 'name', 'project_id',
                                                               'power', 'power_detail'}},
                         {'task': None, 'command': None, 'waits': [], 'pending_requests': None,
                          'last_activity': None, 'backend': None}, row)


def fixture_row(case: Case, name: str = 'alpha', **options) -> dict:
    """A running fixture backend registered and listed; returns its row."""
    root = case.box.project(name)
    case.box.backend(root, **options)
    return case.one(root)


# -- SPEC mapping table: power / power_detail ------------------------------------------------------
class PowerMappingTests(Case):
    def test_empty_registry_lists_no_rows_and_an_ok_registry(self):
        self.box.start_hub()
        result = self.box.hub_list()
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['registry'], {'state': 'ok', 'path': str(self.box.registry_path)})
        self.assertLess(abs(result['observed_at'] - time.time()), 30)

    def test_stopped_project_is_off_without_last_activity(self):
        root = self.box.project('alpha')
        row = self.one(root)
        self.assertEqual(set(row), ROW_KEYS)
        self.assertEqual((row['power'], row['power_detail'], row['last_activity'], row['backend']),
                         ('off', None, None, None))
        self.assertEqual((row['task'], row['command'], row['waits'], row['pending_requests']),
                         (None, None, [], None))

    def test_stopped_project_with_stale_backend_json_is_off_with_its_mtime(self):
        root = self.box.project('alpha')
        process = self.box.backend(root)
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
        record = self.box.record_path(root)
        self.assertTrue(record.exists())
        os.utime(record, (1_700_000_123.25, 1_700_000_123.25))
        row = self.one(root)
        self.assertEqual((row['power'], row['power_detail']), ('off', None))
        self.assertEqual(row['last_activity'], 1_700_000_123.25)

    def test_running_fixture_is_on_with_the_process_identity(self):
        root = self.box.project('alpha')
        process = self.box.backend(root)
        row = self.one(root)
        self.assertEqual((row['power'], row['power_detail']), ('on', None))
        self.assertEqual(row['backend'], {'pid': process.pid, 'start_ticks': proc_start_ticks(process.pid)})
        self.assertEqual(row['name'], 'alpha')
        self.assertEqual(row['project_id'], self.box.marker_id(root))

    def test_the_four_mismatches_in_spec_order(self):
        gone = self.box.project('gone')
        shutil.rmtree(gone)
        plain = self.box.folder('plain')
        broken = self.box.project('broken')
        (broken / '.wb' / 'project.json').write_text('{not json')
        other = self.box.project('other')
        # Each entry would also fail the later checks (a wrong id on a missing root / a plain folder / a
        # broken marker), so the SPEC order decides the code.
        ids = [OTHER_ID[:-2] + f'{n:02x}' for n in range(1, 5)]  # distinct: the registry rejects duplicates
        self.box.write_registry((gone, ids[0]), (plain, ids[1]), (broken, ids[2]), (other, ids[3]))
        self.box.start_hub()
        result = self.box.hub_list()
        expected = {gone: 'root_missing', plain: 'not_initialized', broken: 'marker_invalid',
                    other: 'project_id_mismatch'}
        for root, detail in expected.items():
            row = self.row(result, root)
            self.assertEqual((row['power'], row['power_detail']), ('mismatch', detail), row)
            self.assertEqual({key: row[key] for key in ROW_KEYS - {'observed_at', 'root', 'name', 'project_id',
                                                                   'power', 'power_detail'}},
                             {'task': None, 'command': None, 'waits': [], 'pending_requests': None,
                              'last_activity': None, 'backend': None}, row)
        self.assertEqual(self.row(result, other)['project_id'], ids[3])

    def test_a_running_backend_under_another_project_id_is_a_mismatch_and_is_not_contacted(self):
        root = self.box.project('alpha')
        frames = self.box.base / 'frames.log'
        self.box.backend(root, frames=frames)
        self.box.write_registry((root, OTHER_ID))
        self.box.start_hub()
        row = self.row(self.box.hub_list(), root)
        self.assertEqual((row['power'], row['power_detail'], row['backend']), ('mismatch', 'project_id_mismatch', None))
        self.assertEqual(read_frames(frames), [], 'a mismatched entry must not be observed')

    def test_closing_and_starting_by_snapshot(self):
        root = self.box.project('closing')
        self.box.backend(root, snapshot={'holds': [{'reason': 'shutdown_closing', 'since': 5.0, 'detail': None}]})
        row = self.one(root)
        self.assertEqual((row['power'], row['power_detail']), ('closing', None))
        self.assertIsNotNone(row['backend'])
        self.assertEqual(row['waits'], [], 'shutdown_closing is a power state, not a wait reason')

    def test_phase_starting_is_starting_and_closing_wins_over_it(self):
        root = self.box.project('s')
        self.box.backend(root, snapshot={'phase': 'starting'})
        row = self.one(root)
        self.assertEqual((row['power'], row['power_detail']), ('starting', None))
        self.assertIsNotNone(row['backend'])
        box = Sandbox(self)  # a second, independent sandbox: closing + phase starting
        root2 = box.project('c')
        box.backend(root2, snapshot={'phase': 'starting',
                                     'holds': [{'reason': 'shutdown_closing', 'since': 1, 'detail': None}]})
        box.register(root2)
        box.start_hub()
        self.assertEqual(self.row(box.hub_list(), root2)['power'], 'closing')

    def test_lock_held_without_a_record_is_never_off(self):
        root = self.box.project('alpha')
        self.box.lock_holder(root)
        row = self.one(root)
        self.assertIn(row['power'], ('starting', 'unknown'), row)
        self.assertNotEqual(row['power'], 'off')
        if row['power'] == 'unknown':
            self.assertTrue(row['power_detail'].startswith('identity:'), row)

    def test_unregistered_initialized_project_is_not_listed_or_contacted(self):
        listed, hidden = self.box.project('listed'), self.box.project('hidden')
        frames = self.box.base / 'hidden-frames.log'
        self.box.backend(hidden, frames=frames)
        result = self.observe(listed)
        self.assertEqual([row['root'] for row in result['rows']], [str(listed)])
        text = self.box.wb('hub', 'list').stdout
        self.assertNotIn('hidden', text)
        self.assertEqual(read_frames(frames), [])


# -- identity failures: unknown, never off --------------------------------------------------------------
class UnknownIsNeverOffTests(Case):
    def test_identity_errors_from_inspect_backend_are_unknown_identity(self):
        cases = {
            'data_dir': ({'data_dir': '/elsewhere/.wb'}, 'identity:data_root_mismatch'),
            'ticks': (None, 'identity:'),  # pid reuse: the recorded start_ticks no longer match the holder
        }
        for label, (edit, expected) in cases.items():
            with self.subTest(label):
                box = Sandbox(self)
                root = box.project(label)
                process = box.backend(root)
                record = box.record_path(root)
                value = json.loads(record.read_text())
                if edit is None:
                    value['processes']['backend']['start_ticks'] += 1
                else:
                    value.update(edit)
                record.write_text(json.dumps(value))
                box.register(root)
                box.start_hub()
                row = [r for r in box.hub_list()['rows'] if r['root'] == str(root)][0]
                self.assertEqual(row['power'], 'unknown', row)
                self.assertNotEqual(row['power'], 'off')
                self.assertTrue(row['power_detail'].startswith(expected), row)
                self.assertTrue(proc_alive(process.pid))

    def test_unreachable_when_the_socket_is_gone(self):
        root = self.box.project('alpha')
        self.box.backend(root)
        os.unlink(self.runtime_socket(root))
        self.assert_unknown(self.one(root), 'unreachable')

    def runtime_socket(self, root: Path) -> str:
        return json.loads(self.box.record_path(root).read_text())['ui_socket']

    def test_unreachable_when_the_socket_is_a_dead_listener(self):
        root = self.box.project('alpha')
        self.box.backend(root)
        path = self.runtime_socket(root)
        os.unlink(path)
        dead = socket.socket(socket.AF_UNIX)
        dead.bind(path)  # bound, never listening: connection refused
        self.addCleanup(dead.close)
        self.assert_unknown(self.one(root), 'unreachable')

    def test_a_stranger_squatting_the_recorded_socket_is_unknown_not_on(self):
        # Same perfect snapshot, but the peer pid is not the recorded backend (pid reuse / hijack).
        root = self.box.project('alpha')
        self.box.backend(root)
        self.box.misbehaving('squat', root)
        row = self.one(root)
        self.assert_unknown(row, UNKNOWN_DETAILS)

    def test_wire_misbehaviour_is_unknown_never_off(self):
        for mode in ('close_on_connect', 'close_on_snapshot', 'garbage_welcome', 'garbage_snapshot',
                     'not_ok', 'snapshot_not_dict', 'no_backend_key'):
            with self.subTest(mode):
                box = Sandbox(self)
                root = box.project('alpha')
                box.misbehaving(mode, root)
                box.register(root)
                box.start_hub()
                row = [r for r in box.hub_list()['rows'] if r['root'] == str(root)][0]
                self.assert_unknown(row)

    def test_a_backend_that_never_answers_hello_is_a_timeout_within_the_bound(self):
        root = self.box.project('alpha')
        self.box.misbehaving('hang_hello', root)
        self.box.register(root)
        self.box.start_hub()
        started = time.monotonic()
        row = self.row(self.box.hub_list(), root)
        elapsed = time.monotonic() - started
        self.assert_unknown(row, UNKNOWN_DETAILS)
        self.assertLess(elapsed, 4.5, 'one hung backend must stay inside the 2 s budget plus slack')

    def test_slow_snapshot_is_timeout(self):
        root = self.box.project('alpha')
        self.box.backend(root, delay=8)
        started = time.monotonic()
        row = self.one(root)
        self.assertLess(time.monotonic() - started, 4.5)
        self.assert_unknown(row, 'timeout')

    def test_snapshot_naming_another_backend_is_snapshot_mismatch(self):
        # (label, --backend-json) each overlays one identity field of the snapshot only.
        overlays = {
            'pid': {'pid': 1},
            'start_ticks': {'process': {'pid': 0, 'start_ticks': 1}},
            'data_dir': {'data_dir': '/elsewhere/.wb'},
            'project_dir': {'project_dir': '/elsewhere'},
            'project_id': {'project_id': OTHER_ID},
            'runtime_root': {'runtime_root': '/elsewhere/run'},
            'uid': {'uid': os.geteuid() + 1},
            'boot_id': {'boot_id': 'not-this-boot'},
        }
        for label, overlay in overlays.items():
            with self.subTest(label):
                box = Sandbox(self)
                root = box.project('alpha')
                box.backend(root, backend=overlay)
                box.register(root)
                box.start_hub()
                row = [r for r in box.hub_list()['rows'] if r['root'] == str(root)][0]
                self.assert_unknown(row, 'snapshot_mismatch')

    def test_unknown_is_never_rendered_as_off_in_text_or_json(self):
        root = self.box.project('alpha')
        self.box.backend(root, backend={'pid': 1})
        self.box.register(root)
        self.box.start_hub()
        result = self.box.list_json()
        row = self.row(result, root)
        self.assert_unknown(row, 'snapshot_mismatch')
        text = self.box.wb('hub', 'list').stdout
        line = [line for line in text.splitlines() if 'alpha' in line][0]
        cells = re.split(r'\s{2,}', line.strip())
        self.assertEqual(cells[2], '연결 없음/불명')
        self.assertNotIn('꺼짐', text)
        self.assertNotIn('꺼짐', json.dumps(result, ensure_ascii=False))


# -- field projection from the snapshot --------------------------------------------------------------
class ProjectionTests(Case):
    def test_task_is_projected_to_four_fields_and_stage_is_status(self):
        task = {'task_id': 't-1', 'kind': 'experiment', 'status': 'running', 'summary': 'train the model',
                'since': 1_800_000_000.5, 'active': True, 'revision': 3, 'run_id': 'r1', 'held_reason': None}
        row = fixture_row(self, snapshot={'task': task})
        self.assertEqual(row['task'], {'task_id': 't-1', 'kind': 'experiment', 'status': 'running',
                                       'summary': 'train the model'})

    def test_closed_or_absent_task_is_null(self):
        for label, task in (('closed', {'task_id': 't', 'kind': 'work', 'status': 'closed', 'summary': 's',
                                        'since': 1.0, 'closed_reason': 'done'}), ('none', None)):
            with self.subTest(label):
                box = Sandbox(self)
                root = box.project('p')
                box.backend(root, snapshot={'task': task})
                box.register(root)
                box.start_hub()
                row = [r for r in box.hub_list()['rows'] if r['root'] == str(root)][0]
                self.assertEqual((row['power'], row['task']), ('on', None))

    def test_command_comes_from_the_host_shell_pane_only(self):
        command = {'command_id': 'c-9', 'owner': 'worker', 'label': 'pytest -x', 'state': 'stopping',
                   'started_at': 'x', 'log_path': '/x'}
        row = fixture_row(self, snapshot={'panes': {'host_shell': {'command': command},
                                                    'manager': {'command': {'command_id': 'no', 'owner': 'm',
                                                                            'label': 'no', 'state': 'running'}}}})
        self.assertEqual(row['command'], {'command_id': 'c-9', 'owner': 'worker', 'label': 'pytest -x',
                                          'state': 'stopping'})
        box = Sandbox(self)
        root = box.project('q')
        box.backend(root, snapshot={'panes': {'host_shell': {}}})
        box.register(root)
        box.start_hub()
        self.assertIsNone([r for r in box.hub_list()['rows'] if r['root'] == str(root)][0]['command'])

    def test_last_activity_is_the_max_of_started_at_and_task_since(self):
        cases = {
            'task_later': ({'started_at': 1_000.0}, {'task': {'task_id': 't', 'kind': 'work', 'status': 'running',
                                                                'summary': 's', 'since': 2_000.0}}, 2_000.0),
            'start_later': ({'started_at': 3_000.0}, {'task': {'task_id': 't', 'kind': 'work', 'status': 'running',
                                                                 'summary': 's', 'since': 2_000.0}}, 3_000.0),
            'no_task': ({'started_at': 1_500.5}, {}, 1_500.5),
            'iso_since': ({'started_at': 10.0}, {'task': {'task_id': 't', 'kind': 'work', 'status': 'running',
                                                            'summary': 's', 'since': '2026-10-25T01:02:03Z'}},
                          float(calendar.timegm((2026, 10, 25, 1, 2, 3)))),
        }
        for label, (backend, snapshot, expected) in cases.items():
            with self.subTest(label):
                box = Sandbox(self)
                root = box.project('p')
                box.backend(root, backend=backend, snapshot=snapshot)
                box.register(root)
                box.start_hub()
                row = [r for r in box.hub_list()['rows'] if r['root'] == str(root)][0]
                self.assertEqual(row['last_activity'], expected, row)

    def test_each_wait_code_maps_alone(self):
        t = {'task_id': 't', 'kind': 'work', 'status': 'held', 'summary': 's', 'since': 7.0,
             'held_reason': 'host_terminal_busy'}
        cases = {
            'paused': {'automation': {'paused': True}},
            'boot_confirmation_required': {'holds': [{'reason': 'boot_confirmation_required', 'since': 1, 'detail': 'reboot'}]},
            'manager_editor_not_empty': {'recovery': {'report_wait': {'count': 1, 'reason': 'manager_editor_not_empty',
                                                                       'since': 1_234.5}, 'watch': None}},
            'manager_session_not_connected': {'recovery': {'report_wait': {'count': 2, 'reason': 'manager_session_not_connected',
                                                                            'since': 2_345.5}, 'watch': None}},
            'metadata_unavailable': {'holds': [{'reason': 'metadata_unavailable', 'since': 1, 'detail': None}]},
            'model_hold': {'holds': [{'reason': 'model_hold:manager', 'since': 1, 'detail': 'quota'}]},
            'task_held': {'task': t},
        }
        for code, snapshot in cases.items():
            with self.subTest(code):
                box = Sandbox(self)
                root = box.project('p')
                box.backend(root, snapshot=snapshot)
                box.register(root)
                box.start_hub()
                row = [r for r in box.hub_list()['rows'] if r['root'] == str(root)][0]
                self.assertEqual(row['power'], 'on')
                self.assertEqual([wait['code'] for wait in row['waits']], [code], row)
                self.assertLessEqual(set(row['waits'][0]), {'code', 'since', 'detail'})
                self.assertIn('since', row['waits'][0])
                if code.startswith('manager_'):
                    self.assertEqual(row['waits'][0]['since'], snapshot['recovery']['report_wait']['since'])
                if code == 'task_held':
                    self.assertEqual(row['task']['status'], 'held')
                    self.assertEqual(row['waits'][0].get('detail'), 'host_terminal_busy')

    def test_waits_come_in_the_spec_order_not_the_snapshot_order(self):
        snapshot = {
            'holds': [{'reason': 'model_hold:worker', 'since': 9, 'detail': None},
                      {'reason': 'metadata_unavailable', 'since': 8, 'detail': None},
                      {'reason': 'boot_confirmation_required', 'since': 7, 'detail': None}],
            'recovery': {'report_wait': {'count': 1, 'reason': 'manager_editor_not_empty', 'since': 6.0}, 'watch': None},
            'automation': {'paused': True},
            'task': {'task_id': 't', 'kind': 'work', 'status': 'held', 'summary': 's', 'since': 5.0, 'held_reason': 'x'},
        }
        row = fixture_row(self, snapshot=snapshot)
        self.assertEqual([wait['code'] for wait in row['waits']],
                         ['paused', 'boot_confirmation_required', 'manager_editor_not_empty',
                          'metadata_unavailable', 'model_hold', 'task_held'])

    def test_non_waits_are_not_waits(self):
        snapshot = {'automation': {'paused': False},
                    'recovery': {'report_wait': {'count': 1, 'reason': 'something_else', 'since': 1.0}, 'watch': None},
                    'holds': [], 'task': {'task_id': 't', 'kind': 'work', 'status': 'running', 'summary': 's',
                                          'since': 1.0, 'held_reason': None}}
        self.assertEqual(fixture_row(self, snapshot=snapshot)['waits'], [])


# -- hub_v1 / JSON schema, text columns, pending requests ---------------------------------------------------
class SchemaAndTextTests(Case):
    def build(self) -> tuple[Path, Path, Path]:
        on = self.box.project('on-one')
        self.box.backend(on, snapshot={
            'task': {'task_id': 't-1', 'kind': 'work', 'status': 'held', 'summary': '실험 요약', 'since': 5.0,
                     'held_reason': 'why'},
            'panes': {'host_shell': {'command': {'command_id': 'c', 'owner': 'worker', 'label': 'make', 'state': 'running'}}},
            'automation': {'paused': True}}, backend={'started_at': time.time() - 300})
        off = self.box.project('off-one')
        gone = self.box.project('gone-one')
        gone_id = self.box.marker_id(gone)
        shutil.rmtree(gone)
        self.box.write_registry((on, self.box.marker_id(on)), (off, self.box.marker_id(off)), (gone, gone_id))
        self.box.start_hub()
        return on, off, gone

    def test_wb_hub_list_json_has_exactly_the_spec_keys(self):
        on, off, gone = self.build()
        result = self.box.list_json()
        self.assertEqual(set(result), {'observed_at', 'registry', 'rows'})
        self.assertEqual(set(result['registry']), {'state', 'path'})
        self.assertEqual([row['root'] for row in result['rows']], [str(on), str(off), str(gone)], 'registry order')
        for row in result['rows']:
            self.assertEqual(set(row), ROW_KEYS)
            self.assertIsNone(row['pending_requests'])
            self.assertIsInstance(row['observed_at'], (int, float))
        on_row = self.row(result, on)
        self.assertEqual(set(on_row['backend']), {'pid', 'start_ticks'})
        self.assertEqual(set(on_row['task']), {'task_id', 'kind', 'status', 'summary'})
        self.assertEqual(set(on_row['command']), {'command_id', 'owner', 'label', 'state'})
        self.assertEqual({wait['code'] for wait in on_row['waits']}, {'paused', 'task_held'})
        for wait in on_row['waits']:
            self.assertTrue({'code', 'since'} <= set(wait) <= {'code', 'since', 'detail'}, wait)
        self.assertIsInstance(on_row['last_activity'], (int, float))
        self.assertEqual(self.row(result, off)['power'], 'off')
        self.assertEqual(self.row(result, gone)['power_detail'], 'root_missing')

    def test_wb_hub_list_text_columns_and_labels(self):
        on, off, gone = self.build()
        result = self.box.wb('hub', 'list')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(re.split(r'\s{2,}', lines[0].strip()), COLUMNS, 'column order')
        self.assertEqual(len(lines), 4)
        by_name = {re.split(r'\s{2,}', line.strip())[0]: re.split(r'\s{2,}', line.strip()) for line in lines[1:]}
        on_cells = by_name['on-one']
        self.assertEqual(on_cells[1], str(on))
        self.assertEqual(on_cells[2], '켜짐')
        self.assertIn('실험 요약', on_cells[3])
        self.assertIn('held', on_cells[3])
        self.assertIn('make', on_cells[4])
        self.assertIn('일시정지', on_cells[5])
        self.assertIn('Task 보류', on_cells[5])
        self.assertEqual(on_cells[6], '—')
        self.assertTrue(on_cells[7].endswith('분 전'), on_cells)
        self.assertEqual(by_name['off-one'][2:], ['꺼짐', '—', '—', '—', '—', '—'])
        self.assertTrue(by_name['gone-one'][2].startswith('불일치'))
        self.assertIn('root_missing', by_name['gone-one'][2])
        for cells in by_name.values():
            self.assertEqual(cells[6], '—', 'pending_requests is — and never 0 (HB-AC-19)')
        self.assertEqual(result.stdout.count('요청'), 1, 'only the header names the column')

    def test_registry_changes_are_visible_on_the_next_call(self):
        a, b = self.box.project('a'), self.box.project('b')
        self.box.register(a)
        self.box.start_hub()
        self.assertEqual([r['root'] for r in self.box.hub_list()['rows']], [str(a)])
        self.box.register(a, b)
        self.assertEqual([r['root'] for r in self.box.hub_list()['rows']], [str(a), str(b)])
        self.box.register(b)
        self.assertEqual([r['root'] for r in self.box.hub_list()['rows']], [str(b)])
        self.box.register()
        self.assertEqual(self.box.hub_list()['rows'], [])
        added = self.box.wb('hub', 'add', str(a))
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual([r['root'] for r in self.box.hub_list()['rows']], [str(a)])

    def test_corrupted_registry_is_invalid_with_exit_1_and_a_reason(self):
        self.box.start_hub()
        self.box.state.mkdir(parents=True, exist_ok=True)
        self.box.registry_path.write_text('{ this is not a registry')
        os.chmod(self.box.registry_path, 0o600)
        reply = self.box.hub_list()
        self.assertEqual(reply['registry'], {'state': 'invalid', 'path': str(self.box.registry_path)})
        self.assertEqual(reply['rows'], [])
        for args in (('hub', 'list'), ('hub', 'list', '--json')):
            result = self.box.wb(*args)
            self.assertEqual(result.returncode, 1, (args, result.stdout, result.stderr))
            self.assertIn(str(self.box.registry_path), result.stderr)
            self.assertIn('손상', result.stderr)
        self.assertEqual(json.loads(self.box.wb('hub', 'list', '--json').stdout)['registry']['state'], 'invalid')

    def test_wb_hub_list_without_a_hub_fails_with_a_reason_and_starts_nothing(self):
        result = self.box.wb('hub', 'list')
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn('Hub', result.stderr)
        self.assertFalse((self.box.state / 'hub.json').exists())


# -- parallelism, isolation, cache ----------------------------------------------------------------------------
class IsolationAndCacheTests(Case):
    def test_hung_backends_do_not_delay_or_change_other_rows(self):
        slow = [self.box.project(f'slow{i}') for i in range(3)]
        fast = [self.box.project(f'fast{i}') for i in range(2)]
        for root in slow:
            self.box.backend(root, delay=10)
        fast_snapshot = {'task': {'task_id': 'tf', 'kind': 'work', 'status': 'running', 'summary': 'ok', 'since': 1.0}}
        for root in fast:
            self.box.backend(root, snapshot=fast_snapshot)
        off = self.box.project('stopped')
        self.box.register(*slow, *fast, off)
        self.box.start_hub()
        started = time.monotonic()
        result = self.box.hub_list(fresh=True)
        elapsed = time.monotonic() - started
        # Parallel: bounded by one 2 s budget (+ slack). Serialized three hung rows alone would take >= 6 s.
        self.assertLess(elapsed, 4.5, f'list took {elapsed:.1f}s')
        for root in slow:
            self.assert_unknown(self.row(result, root), 'timeout')
        for root in fast:
            row = self.row(result, root)
            self.assertEqual((row['power'], row['task']['summary']), ('on', 'ok'))
        self.assertEqual(self.row(result, off)['power'], 'off')

    def test_more_rows_than_workers_still_finish_in_bounded_time(self):
        roots = [self.box.project(f'p{i}') for i in range(10)]
        started = time.monotonic()
        result = self.observe(*roots)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual({row['power'] for row in result['rows']}, {'off'})
        self.assertEqual(len(result['rows']), 10)

    def test_second_call_inside_the_cache_window_makes_no_new_backend_connection(self):
        root = self.box.project('alpha')
        frames = self.box.base / 'frames.log'
        self.box.backend(root, frames=frames)
        self.box.register(root)
        self.box.start_hub()
        first = self.box.hub_list()
        self.assertEqual(len(connections(frames)), 1)
        second = self.box.hub_list()
        self.assertEqual(len(connections(frames)), 1, 'cached row must not reconnect')
        self.assertEqual(self.row(first, root)['observed_at'], self.row(second, root)['observed_at'])
        self.assertEqual(self.box.wb('hub', 'list', '--json').returncode, 0)
        self.assertEqual(len(connections(frames)), 1, 'wb hub list (fresh:false) shares the cache')

    def test_fresh_bypasses_the_cache_and_expiry_observes_again(self):
        root = self.box.project('alpha')
        frames = self.box.base / 'frames.log'
        self.box.backend(root, frames=frames)
        self.box.register(root)
        self.box.start_hub()
        self.box.hub_list()
        self.box.hub_list(fresh=False)
        self.assertEqual(len(connections(frames)), 1)
        self.box.hub_list(fresh=True)
        self.assertEqual(len(connections(frames)), 2)
        time.sleep(2.3)
        self.box.hub_list()
        self.assertEqual(len(connections(frames)), 3, 'after 2 s the row is observed again')

    def test_cache_does_not_keep_a_removed_project_or_hide_a_new_one(self):
        a, b = self.box.project('a'), self.box.project('b')
        frames_b = self.box.base / 'b.log'
        self.box.backend(b, frames=frames_b)
        self.box.register(a)
        self.box.start_hub()
        self.box.hub_list()
        self.box.register(a, b)
        rows = self.box.hub_list()['rows']
        self.assertEqual([row['power'] for row in rows], ['off', 'on'])
        self.assertEqual(len(connections(frames_b)), 1)


# -- the Hub observes without attaching ----------------------------------------------------------------------------
class NonAttachTests(Case):
    def test_observation_sends_only_hello_and_snapshot_as_workbench_hub(self):
        root = self.box.project('alpha')
        frames = self.box.base / 'frames.log'
        self.box.backend(root, frames=frames)
        self.observe(root)
        self.box.hub_list(fresh=True)
        grouped = connections(frames)
        self.assertEqual(len(grouped), 2)
        for number, headers in grouped.items():
            self.assertEqual([header['type'] for header in headers], ['hello', 'snapshot'], (number, headers))
            self.assertEqual(headers[0].get('client'), 'workbench-hub')
        self.assertNotIn('attach', {h['type'] for headers in grouped.values() for h in headers})

    def test_an_attached_ui_connection_is_not_disturbed_by_listing(self):
        from workbench.contracts.ui_v1 import FrameDecoder, encode_frame
        root = self.box.project('alpha')
        frames = self.box.base / 'frames.log'
        self.box.backend(root, frames=frames)
        path = json.loads(self.box.record_path(root).read_text())['ui_socket']
        ui = socket.socket(socket.AF_UNIX)
        self.addCleanup(ui.close)
        ui.settimeout(5)
        ui.connect(path)
        decoder = FrameDecoder()

        def exchange(header: dict) -> dict:
            ui.sendall(encode_frame(header))
            while True:
                frames_in = decoder.feed(ui.recv(65536))
                if frames_in:
                    return frames_in[0].header

        self.assertEqual(exchange({'v': 1, 'type': 'hello', 'client': 'workbench-test-ui'})['type'], 'welcome')
        self.assertTrue(exchange({'v': 1, 'type': 'attach', 'id': 'a1'}).get('ok'))
        self.observe(root)
        self.box.hub_list(fresh=True)
        self.box.wb('hub', 'list')
        ui.setblocking(False)
        with self.assertRaises(BlockingIOError):  # open and silent: nothing was pushed, nothing closed it
            ui.recv(1)
        ui.setblocking(True)
        ui.settimeout(5)
        self.assertTrue(exchange({'v': 1, 'type': 'snapshot', 'id': 's1'}).get('ok'), 'the attached UI still works')
        grouped = connections(frames)
        self.assertEqual([h['type'] for h in grouped[1]], ['hello', 'attach', 'snapshot'])
        for number, headers in grouped.items():
            if number != 1:
                self.assertEqual([h['type'] for h in headers], ['hello', 'snapshot'])


# -- auto start ------------------------------------------------------------------------------------------------------
class AutoStartTests(unittest.TestCase):
    def test_wb_hub_list_starts_a_hub_by_the_auto_start_rule_and_stops_cleanly(self):
        box = Sandbox(self, autostart=True)
        root = box.project('alpha')
        box.register(root)

        def stop() -> None:
            record = box.state / 'hub.json'
            try:
                value = json.loads(record.read_text())
            except (OSError, ValueError):
                return
            pid, ticks = value.get('pid'), value.get('start_ticks')
            if isinstance(pid, int) and proc_start_ticks(pid) == ticks:
                box.wb('hub', 'stop', '--yes')
                if not wait_until(lambda: proc_start_ticks(pid) != ticks or not proc_alive(pid), 10):
                    os.kill(pid, signal.SIGTERM)  # still the same identity: our own spawned Hub
                    wait_until(lambda: not proc_alive(pid), 5)
            self.assertFalse(proc_alive(pid) and proc_start_ticks(pid) == ticks, 'auto-started Hub leaked')

        self.addCleanup(stop)
        result = box.wb('hub', 'list', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)['rows']
        self.assertEqual([(row['root'], row['power']) for row in rows], [(str(root), 'off')])


if __name__ == '__main__':
    unittest.main()
