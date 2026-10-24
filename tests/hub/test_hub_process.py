"""Hub process lifetime, identity and ``wb hub status/stop`` (HB-01, seams S1 and S2).

Every Hub here is a real ``wb _hub --foreground`` subprocess started by the test in a
temp HOME/XDG sandbox. Expected values come from the kernel (``/proc/locks`` holder,
``/proc/<pid>/stat`` start ticks, file modes) and the SPEC protocol table, never from
the product's own identity helpers. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[2]


def proc_start_ticks(pid: int) -> int:
    return int(Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])


def flock_holder(path: Path) -> int | None:
    """The pid /proc/locks names for an exclusive flock on exactly this inode."""
    info = os.stat(path, follow_symlinks=False)
    wanted = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    for row in Path('/proc/locks').read_text().splitlines():
        parts = row.split()
        if len(parts) >= 8 and parts[1:4] == ['FLOCK', 'ADVISORY', 'WRITE']:
            major, minor, inode = parts[5].split(':')
            if (int(major, 16), int(minor, 16), int(inode)) == wanted:
                return int(parts[4])
    return None


def alive(pid: int) -> bool:
    try:
        state = Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in (b'Z', b'X')


def wait_until(predicate, timeout: float, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class HubSandbox:
    """Temp HOME/XDG dirs plus the Hub/holder subprocesses one test owns."""

    def __init__(self, case: unittest.TestCase):
        self.case = case
        self.temp = tempfile.TemporaryDirectory(prefix='wb-hub-')
        self.base = Path(self.temp.name)
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.state_home = self.base / 'state'
        self.state_home.mkdir(mode=0o700)
        self.runtime = self.base / 'run'
        self.runtime.mkdir(mode=0o700)
        self.config = self.base / 'config'
        self.config.mkdir(mode=0o700)
        self.state = self.state_home / 'omp-workbench' / 'hub'
        self.socket_dir = self.runtime / 'omp-workbench-hub'
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.home), 'LANG': 'C.UTF-8',
                    'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'XDG_STATE_HOME': str(self.state_home), 'XDG_RUNTIME_DIR': str(self.runtime),
                    'XDG_CONFIG_HOME': str(self.config), 'WORKBENCH_HUB_AUTOSTART': '0'}
        self.processes: list[subprocess.Popen] = []
        case.addCleanup(self.cleanup)

    @property
    def lock(self) -> Path: return self.state / 'hub.lock'
    @property
    def record_path(self) -> Path: return self.state / 'hub.json'

    def record(self) -> dict:
        return json.loads(self.record_path.read_text())

    def popen(self, argv: list[str], **kwargs) -> subprocess.Popen:
        process = subprocess.Popen(argv, env=self.env, cwd=self.base, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True, **kwargs)
        self.processes.append(process)
        return process

    def hub_process(self, *extra: str) -> subprocess.Popen:
        return self.popen([sys.executable, '-m', 'workbench', '_hub', '--foreground', *extra])

    def start_hub(self, *extra: str) -> subprocess.Popen:
        """Start a Hub and wait until its record names it and the socket is bound."""
        process = self.hub_process(*extra)

        def ready() -> bool:
            if process.poll() is not None:
                return True
            try:
                record = self.record()
            except (OSError, ValueError):
                return False
            return record.get('pid') == process.pid and Path(record.get('socket', '')).exists()

        if not wait_until(ready, 10) or process.poll() is not None:
            out, err = process.communicate(timeout=5) if process.poll() is not None else ('', '')
            self.case.fail(f'Hub did not become ready: rc={process.poll()} stdout={out!r} stderr={err!r}')
        return process

    def holder(self, path: Path) -> subprocess.Popen:
        """A plain process (not a Hub) that holds an exclusive flock on ``path``."""
        process = self.popen([sys.executable, '-c',
                              'import fcntl,os,sys,time\n'
                              'fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600)\n'
                              'fcntl.flock(fd,fcntl.LOCK_EX)\n'
                              'print("held",flush=True)\n'
                              'time.sleep(120)\n', str(path)])
        self.case.assertEqual(process.stdout.readline().strip(), 'held')
        return process

    def cli(self, *args: str, timeout: float = 40) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, '-m', 'workbench', *args], env=self.env, cwd=self.base,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    def cleanup(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.kill(process.pid, signal.SIGCONT)
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
        self.temp.cleanup()


class HubStartTests(unittest.TestCase):
    def setUp(self):
        self.box = HubSandbox(self)

    def test_foreground_hub_holds_the_lock_and_publishes_a_private_socket(self):
        hub = self.box.start_hub()
        record = self.box.record()
        self.assertEqual(record['pid'], hub.pid)
        self.assertEqual(flock_holder(self.box.lock), hub.pid)
        self.assertEqual(record['start_ticks'], proc_start_ticks(hub.pid))
        self.assertEqual(record['uid'], os.geteuid())
        self.assertEqual(record['boot_id'], Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        self.assertEqual(record['mode'], 'foreground')
        self.assertEqual(record['schema_version'], 1)
        socket_path = Path(record['socket'])
        self.assertEqual(socket_path, self.box.socket_dir / 'hub.sock')
        info = socket_path.lstat()
        self.assertTrue(stat.S_ISSOCK(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.box.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.box.socket_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.box.record_path.stat().st_mode), 0o600)
        # The Hub must not create anything under the backend runtime base.
        self.assertFalse((self.box.runtime / 'omp-workbench').exists())

    def test_second_hub_loses_the_lock_exits_zero_and_leaves_the_first_untouched(self):
        first = self.box.start_hub()
        before = self.box.record_path.read_bytes()
        socket_inode = Path(self.box.record()['socket']).lstat().st_ino
        second = self.box.hub_process()
        self.assertEqual(second.wait(timeout=10), 0)
        self.assertIsNone(first.poll())
        self.assertEqual(flock_holder(self.box.lock), first.pid)
        self.assertEqual(self.box.record_path.read_bytes(), before)
        self.assertEqual(Path(self.box.record()['socket']).lstat().st_ino, socket_inode)
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(5)
            probe.connect(self.box.record()['socket'])
            probe.sendall(b'{"v":1,"type":"hello","client":"test"}\n')
            self.assertEqual(json.loads(probe.makefile('rb').readline())['hub']['pid'], first.pid)


def hello_pid(socket_path: str) -> int:
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(5)
        probe.connect(socket_path)
        probe.sendall(b'{"v":1,"type":"hello","client":"test"}\n')
        return json.loads(probe.makefile('rb').readline())['hub']['pid']


class HubWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.box = HubSandbox(self)

    def assert_ends_within_four_seconds(self, hub) -> None:
        started = time.monotonic()
        self.assertTrue(wait_until(lambda: hub.poll() is not None, 4.0),
                        'Hub kept running after its single-instance guard broke')
        self.assertLess(time.monotonic() - started, 4.0)

    def test_removed_lock_file_ends_the_hub(self):
        hub = self.box.start_hub()
        self.box.lock.unlink()
        self.assert_ends_within_four_seconds(hub)

    def test_lock_file_replaced_by_another_inode_ends_the_hub(self):
        hub = self.box.start_hub()
        replacement = self.box.state / 'hub.lock.new'
        replacement.write_bytes(b'')
        os.chmod(replacement, 0o600)
        os.replace(replacement, self.box.lock)
        self.assert_ends_within_four_seconds(hub)

    def test_removed_state_directory_ends_the_hub(self):
        hub = self.box.start_hub()
        shutil.rmtree(self.box.state)
        self.assert_ends_within_four_seconds(hub)

    def test_state_directory_replaced_by_another_inode_ends_the_hub(self):
        hub = self.box.start_hub()
        moved = self.box.state.with_name('hub.old')
        os.rename(self.box.state, moved)
        self.box.state.mkdir(mode=0o700)
        shutil.copy2(moved / 'hub.json', self.box.state / 'hub.json')
        self.assert_ends_within_four_seconds(hub)


class HubSignalTests(unittest.TestCase):
    def setUp(self):
        self.box = HubSandbox(self)

    def test_sigterm_is_a_normal_stop_that_cleans_up(self):
        hub = self.box.start_hub()
        socket_path = self.box.record()['socket']
        hub.send_signal(signal.SIGTERM)
        self.assertEqual(hub.wait(timeout=10), 0)
        self.assertIsNone(flock_holder(self.box.lock))
        self.assertFalse(os.path.lexists(socket_path))
        self.assertFalse(os.path.lexists(self.box.record_path))

    def test_sighup_is_ignored(self):
        hub = self.box.start_hub()
        hub.send_signal(signal.SIGHUP)
        time.sleep(0.5)
        self.assertIsNone(hub.poll())
        self.assertEqual(hello_pid(self.box.record()['socket']), hub.pid)


def sigterm_pending(pid: int) -> bool:
    for row in Path(f'/proc/{pid}/status').read_text().splitlines():
        if row.startswith('ShdPnd:'):
            return bool(int(row.split()[1], 16) & (1 << (signal.SIGTERM - 1)))
    return False


class HubStopTests(unittest.TestCase):
    def setUp(self):
        self.box = HubSandbox(self)

    def test_stop_ends_the_hub_and_removes_lock_holder_socket_and_record(self):
        hub = self.box.start_hub()
        socket_path = self.box.record()['socket']
        result = self.box.cli('hub', 'stop')
        self.assertEqual(result.returncode, 0, result)
        self.assertIn('Hub를 멈췄습니다. 켜져 있던 WB 프로젝트는 계속 실행됩니다.', result.stdout)
        self.assertEqual(hub.wait(timeout=5), 0)
        self.assertIsNone(flock_holder(self.box.lock))
        self.assertFalse(os.path.lexists(socket_path))
        self.assertFalse(os.path.lexists(self.box.record_path))

    def test_stop_without_a_hub_is_not_running_and_starts_nothing(self):
        before = hub_processes()
        result = self.box.cli('hub', 'stop')
        self.assertEqual(result.returncode, 3, result)
        self.assertIn('Hub 실행 중 아님', result.stdout + result.stderr)
        self.assertEqual(hub_processes() - before, set())
        self.assertFalse(self.box.state.exists())

    def test_unresponsive_hub_gets_sigterm_and_stop_fails_with_its_pid(self):
        hub = self.box.start_hub()
        os.kill(hub.pid, signal.SIGSTOP)
        self.assertTrue(wait_until(lambda: Path(f'/proc/{hub.pid}/stat').read_bytes()
                                   .rsplit(b') ', 1)[1].split()[0] == b'T', 5))
        started = time.monotonic()
        result = self.box.cli('hub', 'stop')
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 1, result)
        self.assertIn(str(hub.pid), result.stdout + result.stderr)
        self.assertLess(elapsed, 20)
        self.assertIsNone(hub.poll())
        self.assertTrue(sigterm_pending(hub.pid))  # SIGTERM was delivered to exactly this Hub
        socket_path = self.box.record()['socket']
        os.kill(hub.pid, signal.SIGCONT)
        self.assertEqual(hub.wait(timeout=10), 0)  # the pending SIGTERM is a normal stop
        self.assertIsNone(flock_holder(self.box.lock))
        self.assertFalse(os.path.lexists(socket_path))
        self.assertFalse(os.path.lexists(self.box.record_path))

    def test_stop_never_signals_a_lock_holder_that_is_not_the_recorded_hub(self):
        hub = self.box.start_hub()
        record = self.box.record()
        hub.send_signal(signal.SIGTERM)
        hub.wait(timeout=10)
        holder = self.box.holder(self.box.lock)
        self.box.record_path.write_text(json.dumps(record))
        os.chmod(self.box.record_path, 0o600)
        result = self.box.cli('hub', 'stop')
        self.assertEqual(result.returncode, 1, result)
        self.assertIsNone(holder.poll())
        self.assertFalse(sigterm_pending(holder.pid))
        self.assertEqual(flock_holder(self.box.lock), holder.pid)


def hub_processes() -> set[int]:
    """Live processes whose argv contains the ``_hub`` element (any Hub of this uid)."""
    found = set()
    for name in os.listdir('/proc'):
        if name.isdigit():
            try:
                argv = Path(f'/proc/{name}/cmdline').read_bytes().split(b'\0')
            except OSError:
                continue
            if b'_hub' in argv and alive(int(name)):
                found.add(int(name))
    return found


class HubStatusTests(unittest.TestCase):
    def setUp(self):
        self.box = HubSandbox(self)

    def test_status_without_a_hub_is_not_running_and_starts_nothing(self):
        before = hub_processes()
        result = self.box.cli('hub', 'status')
        self.assertEqual(result.returncode, 3, result)
        self.assertIn('Hub 실행 중 아님', result.stdout + result.stderr)
        self.assertEqual(hub_processes() - before, set())
        self.assertFalse(self.box.state.exists())
        self.assertFalse(self.box.socket_dir.exists())

    def test_status_of_a_running_hub_shows_its_identity(self):
        hub = self.box.start_hub()
        result = self.box.cli('hub', 'status')
        self.assertEqual(result.returncode, 0, result)
        self.assertIn(str(hub.pid), result.stdout)
        self.assertIn('foreground', result.stdout)
        self.assertIn(self.box.record()['socket'], result.stdout)
        self.assertIn('not_available', result.stdout)

    def test_status_json_carries_the_status_result_fields(self):
        hub = self.box.start_hub()
        result = self.box.cli('hub', 'status', '--json')
        self.assertEqual(result.returncode, 0, result)
        value = json.loads(result.stdout)
        self.assertEqual(value['state'], 'running')
        self.assertEqual(value['hub']['pid'], hub.pid)
        self.assertEqual(value['hub']['start_ticks'], proc_start_ticks(hub.pid))
        self.assertEqual(value['registry']['state'], 'ok')
        self.assertEqual(value['web'], {'state': 'not_available'})
        self.assertIsNone(value['board_agent'])

    def test_stale_record_of_a_dead_hub_is_not_running(self):
        hub = self.box.start_hub()
        stale = self.box.record_path.read_bytes()
        hub.kill()
        hub.wait(timeout=5)
        self.box.record_path.write_bytes(stale)  # survives an abnormal exit
        os.chmod(self.box.record_path, 0o600)
        result = self.box.cli('hub', 'status')
        self.assertEqual(result.returncode, 3, result)
        self.assertIn('Hub 실행 중 아님', result.stdout + result.stderr)

    def test_lock_held_without_a_record_is_unknown(self):
        self.box.state.mkdir(parents=True, mode=0o700)
        self.box.holder(self.box.lock)
        result = self.box.cli('hub', 'status', '--json')
        self.assertEqual(result.returncode, 1, result)
        value = json.loads(result.stdout)
        self.assertEqual(value['state'], 'unknown')
        self.assertTrue(value['reason'])

    def test_lock_held_by_a_process_other_than_the_record_pid_is_unknown(self):
        hub = self.box.start_hub()
        record = self.box.record()
        hub.send_signal(signal.SIGTERM)
        hub.wait(timeout=10)
        holder = self.box.holder(self.box.lock)  # not a Hub; record still names the old pid
        self.box.record_path.write_text(json.dumps(record))
        os.chmod(self.box.record_path, 0o600)
        self.assertEqual(flock_holder(self.box.lock), holder.pid)
        result = self.box.cli('hub', 'status')
        self.assertEqual(result.returncode, 1, result)
        self.assertNotIn('Hub 실행 중 아님', result.stdout + result.stderr)
        self.assertIsNone(holder.poll())  # status never signals anything


if __name__ == '__main__':
    unittest.main()
