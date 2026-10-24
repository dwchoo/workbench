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
    """HB-AC-13: a ``mismatch`` entry is refused by ``power_on``/``power_off`` and nothing is started."""

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
        for op in ('power_on', 'power_off'):
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


if __name__ == '__main__':
    unittest.main()
