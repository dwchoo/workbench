"""Project control library ``workbench.hub.projects`` (HB-07, seam S6).

Real backends with fake OMPs (``tests/recovery_boot/fake_omp.py``: no model, no provider, no
network) in a temp HOME/XDG sandbox with a short private ``XDG_RUNTIME_DIR``, so no runtime falls
back to ``/tmp/omp-workbench-<uid>`` and no Hub socket to ``/tmp/omp-workbench-hub-<uid>``. Every
library call runs in a driver subprocess (``fixtures/project_driver.py``) the test starts; nothing
forks inside the test runner. Expected values come from ``/proc`` (pid, start ticks, ppid,
session), ``backend.json``, snapshots read by the test and the SPEC/BRIEF texts. Each backend is
ended through ``wb shutdown --yes``; anything of this sandbox still alive afterwards is ended by
pidfd after its start ticks are checked.

A real backend of a ``.wb`` project always prepares its OMP ephemeral namespace under
``/tmp/omp-workbench-<uid>/<hash of data root + project_id>`` (``omp_home.prepare_omp_home``
resolves it with an empty environment, by design). The project ids here are fresh uuid4s, so that
exact path is checked absent when the project is made and is removed in cleanup once every process
of the sandbox has ended; no other entry there is ever read or touched. Run with
``discover -s tests/hub``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

REPO = Path(__file__).resolve().parents[2]
DRIVER = Path(__file__).resolve().parent / 'fixtures' / 'project_driver.py'
CONTRACT_BACKEND = Path(__file__).resolve().parent / 'fixtures' / 'contract_backend.py'
FAKE_OMP = REPO / 'tests' / 'recovery_boot' / 'fake_omp.py'
BLOCKED_PROXY = 'http://127.0.0.1:9'
sys.path.insert(0, str(REPO / 'src'))

from workbench.backend.project import initialize_project, resolve_project_context  # noqa: E402
from workbench.backend.runtime_paths import resolve_runtime_root  # noqa: E402


def stat_fields(pid: int) -> list[bytes] | None:
    try:
        return Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()
    except (OSError, IndexError):
        return None


def start_ticks(pid: int) -> int | None:
    fields = stat_fields(pid)
    return int(fields[19]) if fields else None


def alive(pid: int, start: int | None = None) -> bool:
    fields = stat_fields(pid)
    return bool(fields) and fields[0] not in (b'Z', b'X') and (start is None or int(fields[19]) == start)


def parent_and_session(pid: int) -> tuple[int, int]:
    fields = stat_fields(pid)
    return int(fields[1]), int(fields[3])


def kill_exact(pid: int, start: int, signum: int = signal.SIGKILL) -> bool:
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return False
    try:
        if start_ticks(pid) != start:
            return False
        signal.pidfd_send_signal(fd, signum)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def wait_until(predicate, timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class ProjectSandbox:
    """Temp HOME/XDG dirs, a short private runtime dir, a fake OMP and every process one test owns."""

    def __init__(self, case: unittest.TestCase):
        self.case = case
        self.base = Path(tempfile.mkdtemp(prefix='wb-hb07-')).resolve()
        self.run = Path(tempfile.mkdtemp(prefix='r', dir='/tmp')).resolve()
        os.chmod(self.run, 0o700)
        case.addCleanup(self.cleanup)
        home = self.base / 'home'
        (home / '.omp' / 'agent').mkdir(parents=True)
        (home / '.omp' / 'agent' / 'agent.db').write_bytes(b'')  # fake empty store, no credential
        self.fake = self.base / 'omp'
        self.fake.write_text(f'#!{sys.executable}\n' + FAKE_OMP.read_text())
        self.fake.chmod(0o700)
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'SHELL': '/usr/bin/bash', 'LANG': 'C.UTF-8',
                    'TERM': 'xterm-256color', 'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'WORKBENCH_HUB_AUTOSTART': '0', 'WORKBENCH_OMP': str(self.fake),
                    'XDG_RUNTIME_DIR': str(self.run),
                    'XDG_CONFIG_HOME': str(home / '.config'), 'XDG_STATE_HOME': str(home / '.local/state'),
                    'XDG_DATA_HOME': str(home / '.local/share'), 'XDG_CACHE_HOME': str(home / '.cache'),
                    'FAKE_PANE_RECORD': str(self.base / 'panes.jsonl'),
                    'FAKE_NOTICES': str(self.base / 'notices.jsonl'), 'FAKE_FRAMES': str(self.base / 'frames.jsonl'),
                    **{key: BLOCKED_PROXY for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy',
                                                      'https_proxy', 'all_proxy')}}
        self.hub_state = home / '.local/state' / 'omp-workbench' / 'hub'
        self.roots: list[Path] = []
        self.ephemeral: list[Path] = []  # this sandbox's own namespaces under /tmp/omp-workbench-<uid>
        self.processes: list[subprocess.Popen] = []
        self.calls = 0

    # -- projects ----------------------------------------------------------------------------
    def folder(self, name: str) -> Path:
        path = self.base / 'work' / name
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def project(self, name: str) -> tuple[Path, str]:
        root = self.folder(name)
        initialize_project(resolve_project_context(root, self.env), confirm=lambda _root: True,
                           interactive=True, environment=self.env)
        self.roots.append(root)
        project_id = json.loads((root / '.wb' / 'project.json').read_text())['project_id']
        ephemeral = resolve_runtime_root(root / '.wb', project_id, {})
        self.case.assertFalse(os.path.lexists(ephemeral), ephemeral)
        self.ephemeral.append(ephemeral)
        return root, project_id

    @staticmethod
    def entry(root: Path, project_id: str) -> dict:
        return {'root': str(root), 'project_id': project_id, 'name': Path(root).name}

    def register(self, *entries: dict) -> None:
        self.hub_state.mkdir(parents=True, exist_ok=True, mode=0o700)
        projects = [{**entry, 'registered_at': 1.0, 'source': 'add'} for entry in entries]
        path = self.hub_state / 'registry.json'
        path.write_text(json.dumps({'schema_version': 1, 'projects': projects}))
        os.chmod(path, 0o600)

    # -- driver ------------------------------------------------------------------------------
    def spec(self, op: str, entry: dict, **fields) -> dict:
        self.calls += 1
        return {'op': op, 'entry': entry, 'result': str(self.base / f'result-{self.calls}.json'), **fields}

    def drive(self, op: str, entry: dict, timeout: float = 240, **fields) -> dict:
        spec = self.spec(op, entry, **fields)
        done = subprocess.run([sys.executable, '-I', str(DRIVER), json.dumps(spec)], env=self.env, cwd=self.base,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        self.case.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.last_driver = done
        return json.loads(Path(spec['result']).read_text())

    def drive_async(self, op: str, entry: dict, **fields) -> tuple[subprocess.Popen, Path]:
        spec = self.spec(op, entry, **fields)
        process = subprocess.Popen([sys.executable, '-I', str(DRIVER), json.dumps(spec)], env=self.env,
                                   cwd=self.base, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return process, Path(spec['result'])

    # -- product CLI and observation --------------------------------------------------------
    def wb(self, *args: str, cwd: Path | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, '-m', 'workbench', *args], env=self.env, cwd=cwd or self.base,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    def status(self, root: Path) -> dict | None:
        done = self.wb('status', '--json', cwd=root, timeout=30)
        try:
            value = json.loads(done.stdout)
        except ValueError:
            return None
        return value.get('snapshot') if value.get('running') else None

    def backend_ref(self, root: Path) -> tuple[int, int, dict]:
        record = json.loads((root / '.wb' / 'backend.json').read_text())
        backend = record['processes']['backend']
        return backend['pid'], backend['start_ticks'], record

    def lock_holders(self, root: Path) -> set[int]:
        """pids holding a lock on ``.wb/backend.lock`` (``/proc/locks`` by inode)."""
        inode = os.stat(root / '.wb' / 'backend.lock').st_ino
        holders = set()
        for line in Path('/proc/locks').read_text().splitlines():
            parts = line.split()
            fields = [part for part in parts if part.count(':') == 2]
            if fields and fields[0].rsplit(':', 1)[1] == str(inode):
                holders.add(int(parts[parts.index(fields[0]) - 1]))
        return holders

    def ours(self) -> dict[int, int]:
        """Live processes whose command line, cwd or environment names this sandbox (only ours)."""
        found = {}
        marker = str(self.base).encode()
        for name in os.listdir('/proc'):
            if not name.isdigit() or int(name) == os.getpid():
                continue
            try:
                cmdline = Path(f'/proc/{name}/cmdline').read_bytes()
                cwd = os.readlink(f'/proc/{name}/cwd').encode()
                environ = Path(f'/proc/{name}/environ').read_bytes()
            except OSError:
                continue
            if marker in cmdline or cwd.startswith(marker) or marker in environ:
                ticks = start_ticks(int(name))
                if ticks is not None and alive(int(name), ticks):
                    found[int(name)] = ticks
        return found

    # -- Hub ---------------------------------------------------------------------------------
    def start_hub(self) -> tuple[int, int]:
        """``wb hub start`` (the product's own background spawn); the Hub's pid and start ticks."""
        done = self.wb('hub', 'start', timeout=60)
        self.case.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        record = json.loads((self.hub_state / 'hub.json').read_text())
        return record['pid'], record['start_ticks']

    def hub_row(self, root: Path) -> dict:
        done = self.wb('hub', 'list', '--json', timeout=60)
        self.case.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        rows = [row for row in json.loads(done.stdout)['rows'] if row['root'] == str(root)]
        self.case.assertEqual(len(rows), 1, done.stdout)
        return rows[0]

    def power_on(self, root: Path, project_id: str) -> tuple[int, int]:
        """A ready backend started through the library; its pid and start ticks."""
        outcome = self.drive('power_on', self.entry(root, project_id))
        self.case.assertTrue(outcome['ok'], outcome)
        pid, ticks, _record = self.backend_ref(root)
        return pid, ticks

    def backends(self) -> dict[int, int]:
        """This sandbox's live ``_backend`` processes."""
        found = {}
        for pid, ticks in self.ours().items():
            try:
                argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            except OSError:
                continue
            if b'_backend' in argv:
                found[pid] = ticks
        return found

    # -- attached UI and host shell -----------------------------------------------------------
    def attach(self, root: Path):
        """An attached UI client (the user's screen) on the verified backend socket."""
        from workbench.backend.client import UiClient
        pid, ticks, record = self.backend_ref(root)
        client = UiClient(Path(record['ui_socket']), name='hb07-ui', timeout=10,
                          expected_process={'pid': pid, 'start_ticks': ticks})
        self.case.addCleanup(client.close)
        client.attach((30, 120))
        return client

    def type_shell(self, client, text: str) -> None:
        from workbench.contracts.ui_v1 import ClientType
        answer = client.request(ClientType.INPUT, text.encode(), pane='host_shell')
        self.case.assertTrue(answer.get('ok'), answer)

    def sleeper(self, root: Path, seconds: int, timeout: float = 15) -> int:
        """Start ``sleep SECONDS`` in the background of the host shell; its pid (from ``/proc``)."""
        snapshot = self.status(root)
        shell = snapshot['panes']['host_shell']['process']['pid']
        found: list[int] = []

        def started() -> bool:
            for name in os.listdir('/proc'):
                if not name.isdigit():
                    continue
                try:
                    argv = Path(f'/proc/{name}/cmdline').read_bytes().split(b'\0')
                except OSError:
                    continue
                if argv[:2] == [b'sleep', str(seconds).encode()] and self.descends(int(name), shell):
                    found.append(int(name))
                    return True
            return False

        client = self.attach_client
        self.type_shell(client, f'sleep {seconds} &\n')
        client.pump(0.2)
        self.case.assertTrue(wait_until(started, timeout), f'sleep {seconds} not seen under shell {shell}')
        return found[0]

    @staticmethod
    def descends(pid: int, ancestor: int) -> bool:
        for _ in range(64):
            if pid == ancestor:
                return True
            if pid <= 1:
                return False
            fields = stat_fields(pid)
            if fields is None:
                return False
            pid = int(fields[1])
        return False

    def pane_processes(self, snapshot: dict) -> dict[str, tuple[int, int]]:
        return {name: (pane['process']['pid'], pane['process']['start_ticks'])
                for name, pane in snapshot['panes'].items() if pane.get('process')}

    def asks(self, path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    # -- cleanup -----------------------------------------------------------------------------
    def cleanup(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                try:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        if self.hub_state.exists():
            try:
                self.wb('hub', 'stop', timeout=30)
            except Exception:
                pass
        for root in self.roots:
            try:
                if self.status(root) is not None:
                    self.wb('shutdown', '--yes', cwd=root, timeout=120)
            except Exception:
                pass
        deadline = time.monotonic() + 15
        while self.ours() and time.monotonic() < deadline:
            for pid, ticks in self.ours().items():
                kill_exact(pid, ticks)
            time.sleep(0.2)
        if not self.ours():
            for path in self.ephemeral:
                try:
                    info = os.lstat(path)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid():
                    shutil.rmtree(path, ignore_errors=True)
        shutil.rmtree(self.base, ignore_errors=True)
        shutil.rmtree(self.run, ignore_errors=True)


class ProjectControlCase(unittest.TestCase):
    def setUp(self):
        self.box = ProjectSandbox(self)


class MismatchRefusalTests(ProjectControlCase):
    """HB-AC-13: a ``mismatch`` entry is refused by every function and nothing is started or sent."""

    def test_mismatched_entries_are_refused_without_any_process_or_state(self):
        root, project_id = self.box.project('alpha')
        bare = self.box.folder('bare')
        broken, broken_id = self.box.project('broken')
        os.chmod(broken / '.wb' / 'project.json', 0o644)  # read_project_marker refuses it
        cases = [(self.box.entry(self.box.base / 'work' / 'gone', project_id), 'root_missing'),
                 (self.box.entry(bare, str(uuid.uuid4())), 'not_initialized'),
                 (self.box.entry(broken, broken_id), 'marker_invalid'),
                 (self.box.entry(root, str(uuid.uuid4())), 'project_id_mismatch')]
        asks = self.box.base / 'asks.jsonl'
        for op in ('power_on', 'power_off', 'pause', 'resume', 'confirm_boot'):
            for entry, detail in cases:
                with self.subTest(op=op, detail=detail):
                    before = {path: sorted(os.listdir(path / '.wb')) for path in (root, broken)}
                    outcome = self.box.drive(op, entry, answers=[True], asks=str(asks))
                    self.assertEqual((outcome['ok'], outcome['reason'], outcome['detail']),
                                     (False, 'mismatch', detail), outcome)
                    self.assertEqual(self.box.ours(), {})
                    self.assertEqual(os.listdir(self.box.run), [])
                    self.assertFalse(os.path.lexists(bare / '.wb'))
                    self.assertEqual({path: sorted(os.listdir(path / '.wb')) for path in (root, broken)}, before)
                    self.assertFalse(asks.exists())


class PowerOnTests(ProjectControlCase):
    """HB-AC-08: ``power_on(attach=False)`` starts the backend without a screen; the Hub lists it ``on``."""

    def test_power_on_starts_a_ready_unattached_backend_that_the_hub_lists_on(self):
        root, project_id = self.box.project('alpha')
        entry = self.box.entry(root, project_id)
        self.box.register(entry)
        outcome = self.box.drive('power_on', entry)
        self.assertTrue(outcome['ok'], outcome)
        pid, ticks, record = self.box.backend_ref(root)
        self.assertTrue(alive(pid, ticks))
        self.assertEqual(outcome['data']['backend'], {'pid': pid, 'start_ticks': ticks})
        self.assertEqual(record['data_dir'], str(root / '.wb'))
        self.assertTrue(record['runtime_root'].startswith(str(self.box.run) + '/'), record)
        snapshot = self.box.status(root)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot['phase'], 'ready')
        self.assertIs(snapshot['attached'], False)
        self.assertEqual((snapshot['backend']['pid'], snapshot['backend']['process']['start_ticks']), (pid, ticks))
        self.box.start_hub()
        row = self.box.hub_row(root)
        self.assertEqual((row['power'], row['backend']), ('on', {'pid': pid, 'start_ticks': ticks}), row)

    def test_power_on_of_a_running_project_joins_it_without_a_second_backend(self):
        root, project_id = self.box.project('alpha')
        pid, ticks = self.box.power_on(root, project_id)
        holders = self.box.lock_holders(root)
        self.assertIn(pid, holders)
        outcome = self.box.drive('power_on', self.box.entry(root, project_id))
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(outcome['data']['backend'], {'pid': pid, 'start_ticks': ticks})
        self.assertEqual(self.box.backend_ref(root)[:2], (pid, ticks))
        self.assertEqual(self.box.lock_holders(root), holders)
        self.assertEqual(self.box.backends(), {pid: ticks})

    def test_the_backend_is_no_child_of_the_driver_or_the_hub_and_outlives_both(self):
        root, project_id = self.box.project('alpha')
        hub_pid, hub_ticks = self.box.start_hub()
        driver, result = self.box.drive_async('power_on', self.box.entry(root, project_id))
        self.assertEqual(driver.wait(timeout=240), 0)
        self.assertTrue(json.loads(result.read_text())['ok'])
        pid, ticks, _record = self.box.backend_ref(root)
        parent, session = parent_and_session(pid)
        self.assertNotIn(parent, (driver.pid, hub_pid, os.getpid()))
        self.assertNotIn(session, (os.getsid(0), parent_and_session(hub_pid)[1], driver.pid, hub_pid))
        self.assertNotEqual(parent_and_session(parent)[1], os.getsid(0))  # not reparented into our session
        done = self.box.wb('hub', 'stop', timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(wait_until(lambda: not alive(hub_pid, hub_ticks), 15))
        self.assertTrue(alive(pid, ticks))
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['phase'], snapshot['backend']['pid']), ('ready', pid))




class HubEndTests(ProjectControlCase):
    """HB-AC-02/03: a backend started by ``power_on`` does not depend on the Hub."""

    def test_after_wb_hub_stop_the_backend_lives_and_a_new_hub_lists_it_on(self):
        root, project_id = self.box.project('alpha')
        self.box.register(self.box.entry(root, project_id))
        pid, ticks = self.box.power_on(root, project_id)
        hub_pid, hub_ticks = self.box.start_hub()
        self.assertEqual(self.box.hub_row(root)['power'], 'on')
        done = self.box.wb('hub', 'stop', timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(wait_until(lambda: not alive(hub_pid, hub_ticks), 15))
        self.assertTrue(alive(pid, ticks))
        new_pid, _new_ticks = self.box.start_hub()
        self.assertNotEqual(new_pid, hub_pid)
        row = self.box.hub_row(root)
        self.assertEqual((row['power'], row['backend']), ('on', {'pid': pid, 'start_ticks': ticks}), row)

    def test_after_the_hub_is_sigkilled_the_backend_and_panes_live_and_wb_status_is_ready(self):
        root, project_id = self.box.project('alpha')
        self.box.register(self.box.entry(root, project_id))
        pid, ticks = self.box.power_on(root, project_id)
        panes = self.box.pane_processes(self.box.status(root))
        self.assertEqual(set(panes), {'manager_omp', 'worker_omp', 'host_shell'})
        hub_pid, hub_ticks = self.box.start_hub()
        self.assertTrue(kill_exact(hub_pid, hub_ticks))
        self.assertTrue(wait_until(lambda: not alive(hub_pid, hub_ticks), 15))
        time.sleep(1.0)  # anything tied to the Hub would end now
        self.assertTrue(alive(pid, ticks))
        self.assertTrue(all(alive(*ref) for ref in panes.values()), panes)
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['phase'], snapshot['backend']['pid']), ('ready', pid))
        self.assertEqual(self.box.pane_processes(snapshot), panes)


class PowerOffTests(ProjectControlCase):
    """HB-AC-09: ``power_off`` shows the active work, stops nothing unless confirmed, re-asks on change."""

    def running(self, name: str = 'alpha') -> tuple[Path, str, int, int]:
        root, project_id = self.box.project(name)
        pid, ticks = self.box.power_on(root, project_id)
        self.box.attach_client = self.box.attach(root)
        return root, project_id, pid, ticks

    def test_a_declined_power_off_shows_the_active_work_and_stops_nothing(self):
        root, project_id, pid, ticks = self.running()
        sleeper = self.box.sleeper(root, 300)
        before = self.box.status(root)
        panes = self.box.pane_processes(before)
        asks = self.box.base / 'asks.jsonl'
        outcome = self.box.drive('power_off', self.box.entry(root, project_id), answers=[False], asks=str(asks))
        self.assertEqual((outcome['ok'], outcome['reason']), (False, 'declined'), outcome)
        calls = self.box.asks(asks)
        self.assertEqual([call['call'] for call in calls], [1])
        jobs = [item for item in calls[0]['active'] if item.get('kind') == 'shell_jobs']
        self.assertEqual(len(jobs), 1, calls[0]['active'])
        self.assertIn(sleeper, [item.get('pid') for item in jobs[0]['processes']], jobs)
        # the same list a shutdown_request answers now (the driver is gone; nothing changed)
        from workbench.contracts.ui_v1 import ClientType
        pending = self.box.attach_client.request(ClientType.SHUTDOWN_REQUEST)
        self.assertEqual(calls[0]['active'], pending['active'])
        after = self.box.status(root)
        self.assertTrue(alive(pid, ticks))
        self.assertEqual(self.box.pane_processes(after), panes)
        self.assertTrue(all(alive(*ref) for ref in panes.values()))
        self.assertTrue(alive(sleeper))
        self.assertNotIn('shutdown_closing', [hold.get('reason') for hold in after.get('holds') or ()])
        self.assertEqual(after['phase'], 'ready')

    def test_a_confirmed_power_off_returns_the_verified_result_and_the_backend_ends(self):
        root, project_id, pid, ticks = self.running()
        panes = self.box.pane_processes(self.box.status(root))
        asks = self.box.base / 'asks.jsonl'
        outcome = self.box.drive('power_off', self.box.entry(root, project_id), answers=[True], asks=str(asks))
        self.assertTrue(outcome['ok'], outcome)
        self.assertIs(outcome['data']['result']['verified'], True, outcome)
        self.assertEqual(len(self.box.asks(asks)), 1)
        self.assertTrue(wait_until(lambda: not alive(pid, ticks), 15))
        self.assertTrue(wait_until(lambda: not any(alive(*ref) for ref in panes.values()), 15), panes)
        self.assertIsNone(self.box.status(root))
        self.assertEqual(self.box.backends(), {})

    def test_work_changed_before_the_confirm_is_refused_and_asked_again_with_the_new_list(self):
        root, project_id, pid, ticks = self.running()
        panes = self.box.pane_processes(self.box.status(root))
        asks, gate, out = self.box.base / 'asks.jsonl', self.box.base / 'gate', self.box.base / 'out.txt'
        driver, result = self.box.drive_async('power_off', self.box.entry(root, project_id), answers=[True, False],
                                              asks=str(asks), gate=str(gate), out=str(out))
        self.assertTrue(wait_until(lambda: len(self.box.asks(asks)) == 1, 60))
        first = self.box.asks(asks)[0]['active']
        sleeper = self.box.sleeper(root, 301)  # the user starts work after the list was shown
        Path(f'{gate}.1').touch()
        self.assertTrue(wait_until(lambda: len(self.box.asks(asks)) == 2, 60))
        second = self.box.asks(asks)[1]['active']
        self.assertNotIn(sleeper, [proc.get('pid') for item in first for proc in item.get('processes') or ()])
        self.assertIn(sleeper, [proc.get('pid') for item in second for proc in item.get('processes') or ()], second)
        Path(f'{gate}.2').touch()
        self.assertEqual(driver.wait(timeout=60), 0)
        outcome = json.loads(result.read_text())
        self.assertEqual((outcome['ok'], outcome['reason']), (False, 'declined'), outcome)
        self.assertIn('작업이 바뀌었습니다', out.read_text())
        after = self.box.status(root)
        self.assertTrue(alive(pid, ticks))
        self.assertEqual(self.box.pane_processes(after), panes)
        self.assertTrue(alive(sleeper))
        self.assertNotIn('shutdown_closing', [hold.get('reason') for hold in after.get('holds') or ()])

    def test_work_that_keeps_changing_ends_after_five_new_lists_and_stops_nothing(self):
        root, project_id, pid, ticks = self.running()
        asks, gate, out = self.box.base / 'asks.jsonl', self.box.base / 'gate', self.box.base / 'out.txt'
        driver, result = self.box.drive_async('power_off', self.box.entry(root, project_id), answers=[True] * 10,
                                              asks=str(asks), gate=str(gate), out=str(out))
        for call in range(1, 7):  # the first list and five new ones (SPEC: 최대 5회)
            self.assertTrue(wait_until(lambda: len(self.box.asks(asks)) == call or driver.poll() is not None, 60))
            self.assertIsNone(driver.poll(), f'driver ended after {call - 1} asks')
            self.box.sleeper(root, 400 + call)
            Path(f'{gate}.{call}').touch()
        self.assertEqual(driver.wait(timeout=60), 0)
        self.assertEqual(len(self.box.asks(asks)), 6)
        outcome = json.loads(result.read_text())
        self.assertEqual((outcome['ok'], outcome['reason']), (False, 'work_kept_changing'), outcome)
        self.assertIn('작업이 계속 바뀌어 끄지 않았습니다', out.read_text())
        self.assertTrue(alive(pid, ticks))
        self.assertEqual(self.box.status(root)['phase'], 'ready')



class AutomationControlTests(ProjectControlCase):
    """HB-T02 (real backend): ``pause``/``resume``/``confirm_boot`` without taking the attach slot."""

    def test_pause_and_resume_keep_the_attached_ui_and_confirm_boot_passes_the_backend_reason_through(self):
        root, project_id = self.box.project('alpha')
        self.box.power_on(root, project_id)
        client = self.box.attach(root)
        entry = self.box.entry(root, project_id)
        self.assertIs(self.box.status(root)['automation']['paused'], False)
        outcome = self.box.drive('pause', entry)
        self.assertTrue(outcome['ok'], outcome)
        snapshot = self.box.status(root)
        self.assertIs(snapshot['automation']['paused'], True)
        self.assertIs(snapshot['attached'], True)
        self.assertTrue(wait_until(lambda: client.pump(0.1) and any(
            (state.get('automation') or {}).get('paused') is True for state in client.states), 10))
        outcome = self.box.drive('resume', entry)
        self.assertTrue(outcome['ok'], outcome)
        snapshot = self.box.status(root)
        self.assertIs(snapshot['automation']['paused'], False)
        self.assertIs(snapshot['attached'], True)
        self.assertIs(snapshot['boot']['confirmation_required'], False)
        outcome = self.box.drive('confirm_boot', entry)
        self.assertEqual((outcome['ok'], outcome['reason']), (False, 'boot_confirmation_not_required'), outcome)
        # the attached UI still holds the attach slot: its own request is answered as attached
        from workbench.contracts.ui_v1 import ClientType
        self.assertTrue(client.request(ClientType.FOCUS, pane='host_shell').get('ok'))


class FixtureBackendTests(ProjectControlCase):
    """Backend answers a real backend does not give on demand (contract fixture backend, seam S4)."""

    def fixture(self, name: str, **options) -> tuple[Path, str]:
        root, project_id = self.box.project(name)
        argv = [sys.executable, str(CONTRACT_BACKEND), str(root)]
        for key, value in options.items():
            argv += ['--' + key.replace('_', '-'), value if isinstance(value, str) else json.dumps(value)]
        process = subprocess.Popen(argv, env=self.box.env, cwd=self.box.base, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                   start_new_session=True)
        self.box.processes.append(process)
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        return root, project_id

    PENDING = {'shutdown_request': {'token': 't-1', 'active': [], 'expires_in': 120}}

    def test_an_unverified_shutdown_result_is_not_a_success(self):
        result = {'verified': False, 'problems': ['host_shell survived']}
        root, project_id = self.fixture('alpha', replies_json={**self.PENDING, 'shutdown_confirm': {
            'shutting_down': True, 'result_deadline': 5}}, close_after='shutdown_confirm',
            closing_json={'v': 1, 'type': 'closing', 'reason': 'backend_shutdown', 'result': result})
        outcome = self.box.drive('power_off', self.box.entry(root, project_id), answers=[True])
        self.assertEqual((outcome['ok'], outcome['reason'], outcome['detail']),
                         (False, 'unverified', 'host_shell survived'), outcome)
        self.assertEqual(outcome['data']['result'], result)

    def test_a_connection_lost_before_the_result_is_not_a_success(self):
        root, project_id = self.fixture('alpha', replies_json={**self.PENDING, 'shutdown_confirm': {
            'shutting_down': True, 'result_deadline': 5}}, close_after='shutdown_confirm', closing_json=None)
        outcome = self.box.drive('power_off', self.box.entry(root, project_id), answers=[True])
        self.assertEqual((outcome['ok'], outcome['reason']), (False, 'no_result'), outcome)
        self.assertEqual(outcome['data']['backend'], 'unknown')  # no backend reference was given

    def test_no_result_within_the_backends_deadline_is_not_a_success(self):
        root, project_id = self.fixture('alpha', replies_json={**self.PENDING, 'shutdown_confirm': {
            'shutting_down': True, 'result_deadline': 1}})
        started = time.monotonic()
        outcome = self.box.drive('power_off', self.box.entry(root, project_id), answers=[True])
        self.assertEqual((outcome['ok'], outcome['reason']), (False, 'no_result'), outcome)
        self.assertIn('no result within', outcome['detail'])
        self.assertLess(time.monotonic() - started, 30)

    def test_an_older_backends_not_attached_is_reported_as_backend_too_old(self):
        refused = {'ok': False, 'reason': 'not_attached', 'detail': 'attach before sending terminal or control input'}
        root, project_id = self.fixture('alpha', replies_json={'pause': refused, 'resume': refused})
        for op in ('pause', 'resume'):
            with self.subTest(op=op):
                outcome = self.box.drive(op, self.box.entry(root, project_id))
                self.assertEqual((outcome['ok'], outcome['reason']), (False, 'backend_too_old'), outcome)


if __name__ == '__main__':
    unittest.main()
