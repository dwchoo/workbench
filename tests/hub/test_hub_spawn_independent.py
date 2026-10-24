"""Independent verification of Hub spawn, auto-start and single instance (HB-02, seam S1).

Expected outcomes come from SPEC "Hub 프로세스 수명" (Spawn), "CLI 계약" (auto-start rule and
exception table), BRIEF HB-D05/D11 and HB-AC-01/15, not from the worker's tests. Every ``wb``
is a real CLI subprocess in a temp HOME/XDG sandbox. The user update fence
(``/tmp/omp-workbench-maintenance-<uid>``) is redirected to a sandbox directory by a tiny
driver that sets ``maintenance.FENCE_BASE`` and then runs the unchanged
``workbench.backend.cli.main()`` (the real entry, so auto-start applies). The control socket
lives under the sandbox ``XDG_RUNTIME_DIR``, so no test can share a real Hub's locations.
Facts are read from the kernel: ``/proc/locks`` holder, ``/proc/<pid>/stat`` (ppid, session,
tty), ``/proc/<pid>/fd``, ``environ``, ``cwd``, ``status`` (Umask). Signals go only to
processes this module started, after re-checking pid + start ticks.

Run with ``discover -s tests/hub -p 'test_hub_spawn_independent.py'``.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO = Path(__file__).resolve().parents[2]

# The real entry (``main()`` with argv=None) with only the update fence relocated.
DRIVER = textwrap.dedent("""\
    import os, pathlib
    import workbench.backend.maintenance as maintenance
    maintenance.FENCE_BASE = pathlib.Path(os.environ.pop('WB_TEST_FENCE_BASE'))
    from workbench.backend.cli import main
    raise SystemExit(main())
    """)

# In-process probe: relocated fence, then a direct spawn_hub / ensure_hub call; reports the
# outcome and every child process still attached to this caller (zombies included).
PROBE = textwrap.dedent("""\
    import json, os, pathlib, sys, time
    import workbench.backend.maintenance as maintenance
    maintenance.FENCE_BASE = pathlib.Path(os.environ.pop('WB_TEST_FENCE_BASE'))
    from workbench.hub import process
    call, timeout = sys.argv[1], float(sys.argv[2])
    if call == 'spawn':
        outcome = process.spawn_hub(timeout=timeout)
    else:
        result = process.ensure_hub(timeout=timeout)
        outcome = result.reason
    def children():
        found = []
        for task in os.listdir('/proc/self/task'):
            try:
                found += [int(x) for x in open(f'/proc/self/task/{task}/children').read().split()]
            except OSError:
                pass
        states = {}
        for pid in found:
            try:
                states[pid] = open(f'/proc/{pid}/stat', 'rb').read().rsplit(b') ', 1)[1].split()[0].decode()
            except OSError:
                states[pid] = 'gone'
        return states
    first = children()
    time.sleep(0.5)
    print(json.dumps({'outcome': outcome, 'pid': os.getpid(), 'children': first, 'later': children()}))
    """)

FENCE_DENIED_TEXT = "업데이트"


def stat_fields(pid: int) -> list[bytes]:
    return Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()


def ticks_of(pid: int) -> int:
    return int(stat_fields(pid)[19])


def alive(pid: int) -> bool:
    try:
        return stat_fields(pid)[0] not in (b"Z", b"X")
    except (OSError, IndexError):
        return False


def session_of(pid: int) -> int:
    return int(stat_fields(pid)[3])


def parent_of(pid: int) -> int:
    return int(stat_fields(pid)[1])


def tty_nr_of(pid: int) -> int:
    return int(stat_fields(pid)[4])


def environ_of(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    return dict(item.decode(errors="replace").split("=", 1) for item in raw.split(b"\0") if b"=" in item)


def fd_targets(pid: int) -> dict[int, str]:
    found = {}
    for name in os.listdir(f"/proc/{pid}/fd"):
        try:
            found[int(name)] = os.readlink(f"/proc/{pid}/fd/{name}")
        except OSError:
            pass
    return found


def flock_holder(path: Path) -> int | None:
    """Pid that /proc/locks names for an exclusive flock on exactly this inode."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    wanted = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    for row in Path("/proc/locks").read_text().splitlines():
        parts = row.split()
        if len(parts) >= 8 and parts[1:3] == ["FLOCK", "ADVISORY"] and parts[3] == "WRITE":
            major, minor, inode = parts[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == wanted:
                return int(parts[4])
    return None


def lock_holders(path: Path) -> list[int]:
    """Every pid /proc/locks lists (any mode) for this inode."""
    info = os.stat(path, follow_symlinks=False)
    wanted = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    found = []
    for row in Path("/proc/locks").read_text().splitlines():
        parts = row.split()
        if len(parts) >= 8 and parts[1] == "FLOCK":
            major, minor, inode = parts[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == wanted:
                found.append(int(parts[4]))
    return found


def wait_until(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def reported_pid(result: subprocess.CompletedProcess) -> int:
    match = re.search(r"pid (\d+)", result.stdout)
    if match is None:
        raise AssertionError(f"no pid in output: rc={result.returncode} out={result.stdout!r} err={result.stderr!r}")
    return int(match.group(1))


class Box:
    """Temp HOME/XDG/fence dirs plus the Hubs and helper processes one test owns."""

    def __init__(self, case: unittest.TestCase):
        self.case = case
        self.temp = tempfile.TemporaryDirectory(prefix="wbsp-")
        self.base = Path(self.temp.name)
        self.home = self.base / "h"
        self.state_home = self.base / "s"
        self.runtime = self.base / "r"
        self.config = self.base / "c"
        self.work = self.base / "w"
        self.fence_base = self.base / "f"
        for directory in (self.home, self.state_home, self.runtime, self.config, self.work):
            directory.mkdir(mode=0o700)
        self.state = self.state_home / "omp-workbench" / "hub"
        self.socket_dir = self.runtime / "omp-workbench-hub"
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8",
                    "PYTHONPATH": str(REPO / "src"), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_STATE_HOME": str(self.state_home), "XDG_RUNTIME_DIR": str(self.runtime),
                    "XDG_CONFIG_HOME": str(self.config), "WB_TEST_FENCE_BASE": str(self.fence_base)}
        self.helpers: list[tuple[subprocess.Popen, int]] = []
        self.closers: list = []
        case.addCleanup(self.cleanup)

    lock = property(lambda self: self.state / "hub.lock")
    record_path = property(lambda self: self.state / "hub.json")
    log_path = property(lambda self: self.state / "hub.log")
    fence_file = property(lambda self: self.fence_base / "update.lock")

    def record(self) -> dict:
        return json.loads(self.record_path.read_text())

    def argv(self, *args: str) -> list[str]:
        return [sys.executable, "-c", DRIVER, *args]

    def popen_wb(self, *args: str, env: dict | None = None, cwd: Path | None = None, **kwargs) -> subprocess.Popen:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
        return subprocess.Popen(self.argv(*args), env=self.env if env is None else env,
                                cwd=self.work if cwd is None else cwd, text=True, **kwargs)

    def wb(self, *args: str, timeout: float = 40, env: dict | None = None, cwd: Path | None = None
           ) -> subprocess.CompletedProcess:
        return subprocess.run(self.argv(*args), env=self.env if env is None else env,
                              cwd=self.work if cwd is None else cwd, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)

    def probe(self, call: str, timeout: float = 5.0, env: dict | None = None) -> dict:
        done = subprocess.run([sys.executable, "-c", PROBE, call, str(timeout)],
                              env=self.env if env is None else env, cwd=self.work, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=40)
        self.case.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def hubs(self) -> list[int]:
        """Live ``_hub`` processes whose environment names this sandbox's state home."""
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                if b"_hub" not in (entry / "cmdline").read_bytes().split(b"\0") or not alive(pid):
                    continue
                if environ_of(pid).get("XDG_STATE_HOME") == str(self.state_home):
                    found.append(pid)
            except OSError:
                continue
        return sorted(found)

    def hold(self, argv_source: str, *args: str) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, "-c", argv_source, *args], env=self.env, cwd=self.work,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        self.helpers.append((process, ticks_of(process.pid)))
        self.case.assertEqual(process.stdout.readline().strip(), "held")
        return process

    def hold_flock(self, path: Path) -> subprocess.Popen:
        """A plain non-Hub process holding an exclusive flock on ``path``."""
        return self.hold("import fcntl,os,sys,time\n"
                         "fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600)\n"
                         "fcntl.flock(fd,fcntl.LOCK_EX)\nprint('held',flush=True)\ntime.sleep(300)\n", str(path))

    def start_hub(self) -> int:
        result = self.wb("hub", "start")
        self.case.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        return reported_pid(result)

    def cleanup(self) -> None:
        for closer in self.closers:
            try:
                closer()
            except OSError:
                pass
        owned = []
        for pid in self.hubs():
            try:
                owned.append((pid, ticks_of(pid)))
            except OSError:
                pass
        for process, ticks in self.helpers:
            owned.append((process.pid, ticks))
        for pid, ticks in owned:
            self._signal(pid, ticks, signal.SIGTERM)
        if not wait_until(lambda: not any(self._same(pid, ticks) for pid, ticks in owned), 10):
            for pid, ticks in owned:
                self._signal(pid, ticks, signal.SIGKILL)
            wait_until(lambda: not any(self._same(pid, ticks) for pid, ticks in owned), 5)
        for process, _ in self.helpers:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
        leftover = self.hubs()
        self.temp.cleanup()
        self.case.assertEqual(leftover, [], "a Hub outlived its test")

    @staticmethod
    def _same(pid: int, ticks: int) -> bool:
        try:
            return alive(pid) and ticks_of(pid) == ticks
        except OSError:
            return False

    def _signal(self, pid: int, ticks: int, signum: int) -> None:
        if self._same(pid, ticks):
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass


class ConcurrentSpawnTests(unittest.TestCase):
    def setUp(self):
        self.box = Box(self)

    def test_many_simultaneous_commands_leave_exactly_one_hub(self):
        """HB-AC-01: auto-start and `hub start` racing together; three rounds, each from no Hub."""
        for round_number in range(3):
            with self.subTest(round=round_number):
                callers = [(args, self.box.popen_wb(*args))
                           for args in [("status",)] * 10 + [("hub", "start")] * 6]
                outcomes = []
                for args, caller in callers:
                    out, err = caller.communicate(timeout=60)
                    outcomes.append((args, caller.returncode, out, err))
                for args, code, out, err in outcomes:
                    if args == ("hub", "start"):
                        self.assertEqual(code, 0, (out, err))
                    else:
                        self.assertEqual(code, 3, (out, err))  # `no backend is running`, whatever the Hub did
                        self.assertNotIn("Hub를 자동으로 시작하지 못했습니다", err)
                pids = {reported_pid(subprocess.CompletedProcess([], code, out, err))
                        for args, code, out, err in outcomes if args == ("hub", "start")}
                self.assertEqual(len(pids), 1, outcomes)
                (pid,) = pids
                self.assertEqual(flock_holder(self.box.lock), pid)
                self.assertEqual(self.box.record()["pid"], pid)
                # Losers exit after `L`; only the lock holder may remain.
                self.assertTrue(wait_until(lambda: self.box.hubs() == [pid], 8), self.box.hubs())
                time.sleep(0.3)
                self.assertEqual(self.box.hubs(), [pid])
                stopped = self.box.wb("hub", "stop", "--yes")
                self.assertEqual(stopped.returncode, 0, (stopped.stdout, stopped.stderr))
                self.assertTrue(wait_until(lambda: not self.box.hubs(), 8))

    def test_status_commands_alone_also_converge_on_one_hub(self):
        callers = [self.box.popen_wb("status") for _ in range(12)]
        for caller in callers:
            caller.communicate(timeout=60)
        holder = flock_holder(self.box.lock)
        self.assertIsNotNone(holder)
        self.assertTrue(wait_until(lambda: self.box.hubs() == [holder], 8), (holder, self.box.hubs()))
        self.assertEqual(self.box.record()["pid"], holder)


class NoZombieTests(unittest.TestCase):
    def setUp(self):
        self.box = Box(self)

    def test_spawning_caller_has_no_child_left_not_even_a_zombie(self):
        result = self.box.probe("ensure")
        self.assertEqual(result["outcome"], "started")
        self.assertEqual(result["children"], {}, result)
        self.assertEqual(result["later"], {}, result)
        (hub,) = self.box.hubs()
        self.assertNotEqual(parent_of(hub), result["pid"])  # reparented away from the caller
        self.assertNotEqual(session_of(hub), session_of(result["pid"]) if alive(result["pid"]) else -1)

    def test_losing_the_lock_race_also_leaves_no_child_and_keeps_the_winner(self):
        winner = self.box.start_hub()
        ticks = ticks_of(winner)
        result = self.box.probe("spawn")  # bypasses the `running` shortcut: the new Hub must lose the lock
        self.assertEqual(result["outcome"], "lost", result)
        self.assertEqual(result["children"], {}, result)
        self.assertEqual(result["later"], {}, result)
        self.assertTrue(wait_until(lambda: self.box.hubs() == [winner], 8), self.box.hubs())
        self.assertEqual(ticks_of(winner), ticks)
        self.assertEqual(flock_holder(self.box.lock), winner)

    def test_no_intermediate_process_survives_the_spawn(self):
        self.box.start_hub()
        self.assertTrue(wait_until(lambda: self._sandbox_pids() == self.box.hubs(), 5), self._sandbox_pids())

    def _sandbox_pids(self) -> list[int]:
        found = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit() and int(entry.name) != os.getpid():
                try:
                    if environ_of(int(entry.name)).get("XDG_STATE_HOME") == str(self.box.state_home) and alive(int(entry.name)):
                        found.append(int(entry.name))
                except OSError:
                    continue
        return sorted(found)


class ReadyFdTests(unittest.TestCase):
    def setUp(self):
        self.box = Box(self)

    def test_a_hub_that_cannot_open_its_log_ends_in_eof_quickly_with_spawn_failed(self):
        """The grandchild dies before exec (O_NOFOLLOW on a symlinked hub.log): EOF, not a 5 s wait."""
        self.box.state.mkdir(parents=True, mode=0o700)
        target = self.box.base / "elsewhere.log"
        os.symlink(target, self.box.log_path)
        started = time.monotonic()
        result = self.box.wb("hub", "start")
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn("spawn_failed", result.stderr)
        self.assertIn(str(self.box.log_path), result.stderr)
        self.assertLess(elapsed, 4.5, "EOF must be reported at once, not after the ready timeout")
        self.assertFalse(target.exists(), "the symlink target must never be written")
        self.assertEqual(self.box.hubs(), [])
        # The auto-start path only warns (one stderr line) and the command still runs.
        status = self.box.wb("status")
        self.assertEqual(status.returncode, 3, status.stderr)
        self.assertIn("no backend is running", status.stdout)
        self.assertEqual(len(status.stderr.strip().splitlines()), 1, status.stderr)
        self.assertIn("spawn_failed", status.stderr)

    def test_a_hub_that_exits_after_exec_before_ready_ends_in_eof_quickly(self):
        self.box.socket_dir.write_text("not a directory")  # the Hub cannot bind and exits before `R`
        started = time.monotonic()
        result = self.box.wb("hub", "start")
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn("spawn_failed", result.stderr)
        self.assertIn(str(self.box.log_path), result.stderr)
        self.assertLess(elapsed, 4.5)
        self.assertEqual(self.box.hubs(), [])
        self.assertIn("Hub start failed", self.box.log_path.read_text())
        self.assertFalse(flock_holder(self.box.lock), "a failed Hub must not leave the lock held")

    def test_ready_timeout_is_reported_without_killing_or_wedging_the_hub(self):
        result = self.box.probe("ensure", timeout=0.001)  # no Hub can be ready within 1 ms
        self.assertEqual(result["outcome"], "ready_timeout", result)
        self.assertEqual(result["children"], {}, result)
        # The Hub keeps starting even though nobody listens on the ready fd any more.
        self.assertTrue(wait_until(lambda: len(self.box.hubs()) == 1 and self.box.record_path.exists(), 10))
        (hub,) = self.box.hubs()
        status = self.box.wb("hub", "status")
        self.assertEqual(status.returncode, 0, (status.stdout, status.stderr))
        self.assertIn(f"pid {hub}", status.stdout)
        self.assertNotIn("Traceback", self.box.log_path.read_text())

    def test_a_foreign_process_holding_the_lock_is_waited_out_never_raced(self):
        self.box.state.mkdir(parents=True, mode=0o700)
        holder = self.box.hold_flock(self.box.lock)
        started = time.monotonic()
        result = self.box.wb("hub", "start")
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn("hub_unknown", result.stderr)
        self.assertGreaterEqual(elapsed, 4.0, "an unproven lock holder must be waited out (5 s), not skipped")
        self.assertLess(elapsed, 15)
        self.assertEqual(self.box.hubs(), [], "no second Hub may be spawned against a held lock")
        self.assertEqual(flock_holder(self.box.lock), holder.pid)
        # The auto-start path warns on one line and the command still answers.
        status = self.box.wb("status")
        self.assertEqual(status.returncode, 3, status.stderr)
        self.assertEqual(len(status.stderr.strip().splitlines()), 1, status.stderr)
        self.assertEqual(self.box.hubs(), [])


class CallerIsolationTests(unittest.TestCase):
    def setUp(self):
        self.box = Box(self)

    def test_a_capture_output_caller_returns_promptly_for_both_start_paths(self):
        for args in (("hub", "start"), ("status",)):
            with self.subTest(args=args):
                if self.box.hubs():
                    self.assertEqual(self.box.wb("hub", "stop", "--yes").returncode, 0)
                    self.assertTrue(wait_until(lambda: not self.box.hubs(), 8))
                started = time.monotonic()
                try:
                    # If the Hub inherited the pipes, communicate() would block until the Hub exits.
                    result = subprocess.run(self.box.argv(*args), env=self.box.env, cwd=self.box.work,
                                            capture_output=True, text=True, timeout=25)
                except subprocess.TimeoutExpired:
                    self.fail("the caller hung: the Hub holds the caller's stdout/stderr pipe")
                self.assertLess(time.monotonic() - started, 20)
                self.assertIn(result.returncode, (0, 3))
                self.assertEqual(len(self.box.hubs()), 1)

    def test_the_hub_holds_no_descriptor_of_its_caller(self):
        extra_read, extra_write = os.pipe()
        file_fd = os.open(self.box.base / "caller-private.txt", os.O_RDWR | os.O_CREAT, 0o600)
        dir_fd = os.open(self.box.work, os.O_RDONLY)
        high = os.dup2(file_fd, 211)
        for fd in (extra_read, extra_write, file_fd, dir_fd, high):
            os.set_inheritable(fd, True)
        caller_targets = {os.readlink(f"/proc/self/fd/{fd}") for fd in (extra_read, extra_write, file_fd, dir_fd, high)}
        try:
            caller = subprocess.Popen(self.box.argv("hub", "start"), env=self.box.env, cwd=self.box.work,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                      pass_fds=(extra_read, extra_write, file_fd, dir_fd, high))
            out, err = caller.communicate(timeout=40)
        finally:
            for fd in (extra_read, extra_write, file_fd, dir_fd, high):
                os.close(fd)
        self.assertEqual(caller.returncode, 0, err)
        pid = reported_pid(subprocess.CompletedProcess([], 0, out, err))
        targets = fd_targets(pid)
        self.assertEqual(sorted(set(targets.values()) & caller_targets), [], targets)
        self.assertEqual([t for t in targets.values() if t.startswith("pipe:")], [], targets)
        self.assertEqual([t for t in targets.values() if t.startswith("/dev/pts") or t == "/dev/tty"], [], targets)
        self.assertEqual(sorted(fd for fd in targets if fd > 20), [], targets)
        log = str(self.box.log_path)
        self.assertEqual((targets[0], targets[1], targets[2]), ("/dev/null", log, log))
        # The Hub never holds the update fence (only spawn does), neither as a path nor as a lock.
        self.assertFalse([t for t in targets.values() if t.endswith("update.lock")], targets)
        self.assertNotIn(pid, lock_holders(self.box.fence_file))

    def test_the_hub_does_not_hold_the_fence_so_an_update_can_take_it_exclusively(self):
        self.box.start_hub()
        descriptor = os.open(self.box.fence_file, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if anything still holds it
        finally:
            os.close(descriptor)

    def test_environment_cwd_stdio_umask_and_session_are_scrubbed_for_both_start_paths(self):
        nested = self.box.work / "deep" / "er"
        nested.mkdir(parents=True)
        dirty = dict(self.box.env, WORKBENCH_DATA_DIR=str(self.box.base / "legacy"), WORKBENCH_G3_PROBE="1",
                     WORKBENCH_G3_ANOTHER="x", WORKBENCH_AGENT_ROLES="manager", WORKBENCH_OMP_RUNTIME_ROOT="/x",
                     HB02_KEEP="kept")
        for args in (("hub", "start"), ("status",)):
            with self.subTest(args=args):
                if self.box.hubs():
                    self.assertEqual(self.box.wb("hub", "stop", "--yes").returncode, 0)
                    self.assertTrue(wait_until(lambda: not self.box.hubs(), 8))
                result = self.box.wb(*args, env=dirty, cwd=nested)
                self.assertIn(result.returncode, (0, 1, 3), (result.stdout, result.stderr))
                self.assertTrue(wait_until(lambda: len(self.box.hubs()) == 1 and self.box.record_path.exists(), 10))
                (pid,) = self.box.hubs()
                environ = environ_of(pid)
                for name in ("WORKBENCH_DATA_DIR", "WORKBENCH_G3_PROBE", "WORKBENCH_G3_ANOTHER",
                             "WORKBENCH_AGENT_ROLES", "WORKBENCH_OMP_RUNTIME_ROOT"):
                    self.assertNotIn(name, environ)
                self.assertEqual(environ.get("HB02_KEEP"), "kept")  # everything else is the caller's
                self.assertEqual(environ.get("HOME"), str(self.box.home))
                self.assertEqual(os.readlink(f"/proc/{pid}/cwd"), "/")
                targets = fd_targets(pid)
                self.assertEqual((targets[0], targets[1], targets[2]),
                                 ("/dev/null", str(self.box.log_path), str(self.box.log_path)))
                self.assertIn("Umask:\t0077", Path(f"/proc/{pid}/status").read_text())
                self.assertEqual(stat.S_IMODE(os.stat(self.box.log_path).st_mode), 0o600)
                self.assertNotEqual(session_of(pid), os.getsid(0))
                self.assertNotEqual(session_of(pid), pid)  # a second fork: never the session leader
                self.assertEqual(tty_nr_of(pid), 0, "the Hub must have no controlling terminal")

    def test_the_hub_survives_sighup_and_a_closed_terminal_after_an_auto_start(self):
        script = self.box.base / "launch.sh"
        script.write_text("#!/bin/sh\n" + f"{sys.executable} -c \"$DRIVER\" status; echo AUTOSTART-DONE; exec sleep 60\n")
        script.chmod(0o700)
        child, master = pty.fork()
        if child == 0:  # pragma: no cover - exec'd immediately
            try:
                signal.signal(signal.SIGHUP, signal.SIG_DFL)  # a nohup'd runner would otherwise leave it ignored
                os.chdir(self.box.work)
                os.execve("/bin/sh", ["/bin/sh", str(script)], dict(self.box.env, DRIVER=DRIVER))
            finally:
                os._exit(127)
        output = b""
        deadline = time.monotonic() + 40
        try:
            while b"AUTOSTART-DONE" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.5)[0]:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
            self.assertIn(b"AUTOSTART-DONE", output)
            self.assertTrue(wait_until(lambda: len(self.box.hubs()) == 1, 10))
            (pid,) = self.box.hubs()
            self.assertEqual(tty_nr_of(pid), 0)
            self.assertEqual([t for t in fd_targets(pid).values() if t.startswith("/dev/pts") or t == "/dev/tty"], [])
        finally:
            os.close(master)  # hang up: SIGHUP to the shell's foreground session
        _, status = os.waitpid(child, 0)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGHUP, status)
        time.sleep(0.5)
        self.assertTrue(alive(pid))
        # A direct SIGHUP to the Hub (sent by this test, to a Hub it started) is ignored too.
        os.kill(pid, signal.SIGHUP)
        time.sleep(0.5)
        self.assertTrue(alive(pid))
        status = self.box.wb("hub", "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn(f"pid {pid}", status.stdout)


class ExemptCommandTests(unittest.TestCase):
    # BRIEF HB-D11 / HB-AC-15 exception list (args, accepted exit codes; None = any).
    EXEMPT = (
        (("hub", "stop"), {3}), (("hub", "status"), {3}), (("hub", "passwd"), None), (("hub", "tls"), None),
        (("hub", "enable"), None), (("hub", "disable"), None),
        (("setup", "--omp", "/nonexistent/omp-for-hub-test"), {1}),
        (("update",), {1}),  # refused: a development checkout is not a uv Git install
        (("doctor",), None), (("--help",), {0}), (("--version",), None),
        (("hub", "--help"), {0}), (("status", "--help"), {0}),
        # argparse errors end before the handler, so before auto-start
        (("status", "--no-such-option"), {2}), (("no-such-command",), {2}), (("hub",), {2}),
    )
    # Every other command starts the Hub (HB-D05); run before its handler, whatever the handler does.
    AUTOSTARTING = (("status",), ("shutdown",), ("confirm-boot",), ("logs", "raw-status"), ("attach",))

    def test_every_exempt_command_starts_no_hub(self):
        for args, accepted in self.EXEMPT:
            with self.subTest(args=args):
                box = Box(self)
                result = box.wb(*args)
                if accepted is not None:
                    self.assertIn(result.returncode, accepted, (result.stdout, result.stderr))
                time.sleep(0.2)
                self.assertEqual(box.hubs(), [])
                self.assertIsNone(flock_holder(box.lock))
                self.assertFalse(box.record_path.exists())
                self.assertFalse(box.log_path.exists(), "spawn creates hub.log; none means no spawn happened")
                self.assertFalse(box.socket_dir.exists())
                box.cleanup()

    def test_hub_status_and_stop_without_a_hub_report_it_and_leave_none(self):
        box = Box(self)
        for args in (("hub", "status"), ("hub", "stop")):
            result = box.wb(*args)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("Hub 실행 중 아님", result.stdout + result.stderr)
        self.assertEqual(box.hubs(), [])

    def test_commands_outside_the_exception_list_do_start_the_hub(self):
        for args in self.AUTOSTARTING:
            with self.subTest(args=args):
                box = Box(self)
                box.wb(*args)
                self.assertTrue(wait_until(lambda: len(box.hubs()) == 1, 10), (args, box.hubs()))
                self.assertEqual(flock_holder(box.lock), box.hubs()[0])
                box.cleanup()


class UpdateFenceTests(unittest.TestCase):
    def setUp(self):
        self.box = Box(self)
        self.box.fence_base.mkdir(mode=0o700)
        self.fence = os.open(self.box.fence_file, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(self._close)
        self.held = False

    def _close(self):
        try:
            os.close(self.fence)
        except OSError:
            pass

    def lock(self, mode: int) -> None:
        fcntl.flock(self.fence, mode | fcntl.LOCK_NB)

    def test_an_exclusive_update_fence_skips_auto_start_but_the_command_runs(self):
        self.lock(fcntl.LOCK_EX)
        result = self.box.wb("status")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("no backend is running", result.stdout)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
        self.assertIn(FENCE_DENIED_TEXT, result.stderr)
        time.sleep(0.3)
        self.assertEqual(self.box.hubs(), [])
        self.assertIsNone(flock_holder(self.box.lock))

    def test_an_exclusive_update_fence_makes_hub_start_exit_1_with_the_reason_then_it_recovers(self):
        self.lock(fcntl.LOCK_EX)
        result = self.box.wb("hub", "start")
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn(FENCE_DENIED_TEXT, result.stderr)
        self.assertEqual(self.box.hubs(), [])
        fcntl.flock(self.fence, fcntl.LOCK_UN)
        pid = self.box.start_hub()
        self.assertEqual(self.box.hubs(), [pid])

    def test_a_shared_holder_does_not_block_spawning(self):
        self.lock(fcntl.LOCK_SH)  # e.g. another wb/backend start preparing: shared fences coexist
        pid = self.box.start_hub()
        self.assertEqual(self.box.hubs(), [pid])

    def test_a_running_hub_needs_no_fence_for_later_commands(self):
        pid = self.box.start_hub()
        self.lock(fcntl.LOCK_EX)
        result = self.box.wb("status")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.box.hubs(), [pid])


class OptOutTests(unittest.TestCase):
    def setUp(self):
        self.box = Box(self)

    def test_zero_disables_only_auto_start(self):
        env = dict(self.box.env, WORKBENCH_HUB_AUTOSTART="0")
        for args in (("status",), ("logs", "raw-status"), ("shutdown",)):
            with self.subTest(args=args):
                result = self.box.wb(*args, env=env)
                self.assertEqual(result.stderr.count("Hub"), 0, result.stderr)
                time.sleep(0.2)
                self.assertEqual(self.box.hubs(), [])
                self.assertFalse(self.box.log_path.exists())
        started = self.box.wb("hub", "start", env=env)
        self.assertEqual(started.returncode, 0, (started.stdout, started.stderr))
        pid = reported_pid(started)
        self.assertEqual(self.box.hubs(), [pid])
        self.assertEqual(self.box.wb("hub", "status", env=env).returncode, 0)

    def test_any_other_value_keeps_auto_start_on(self):
        for value in ("1", ""):
            with self.subTest(value=value):
                box = Box(self)
                box.wb("status", env=dict(box.env, WORKBENCH_HUB_AUTOSTART=value))
                self.assertTrue(wait_until(lambda: len(box.hubs()) == 1, 10), box.hubs())
                box.cleanup()


class SelfExitTests(unittest.TestCase):
    def setUp(self):
        self.box = Box(self)

    def test_a_killed_hub_is_replaced_by_the_next_auto_start_command(self):
        first = self.box.start_hub()
        first_ticks = ticks_of(first)
        os.kill(first, signal.SIGKILL)  # a Hub this test started
        self.assertTrue(wait_until(lambda: not alive(first), 5))
        self.assertTrue(self.box.record_path.exists(), "a stale record stays behind")
        result = self.box.wb("status")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stderr, "", "a stale record is just not_running: no warning")
        holder = flock_holder(self.box.lock)
        self.assertIsNotNone(holder)
        self.assertNotEqual((holder, ticks_of(holder)), (first, first_ticks))
        self.assertEqual(self.box.hubs(), [holder])
        status = self.box.wb("hub", "status")
        self.assertIn(f"pid {holder}", status.stdout)

    def test_removing_the_temp_home_ends_the_hub(self):
        pid = self.box.start_hub()
        for directory in (self.box.home, self.box.state_home, self.box.runtime, self.box.config):
            shutil.rmtree(directory)  # what removing a test's temp HOME does
        self.assertTrue(wait_until(lambda: not alive(pid), 12), "Hub outlived its state directory")

    def test_removing_only_the_lock_file_ends_the_hub_and_a_new_one_can_start(self):
        pid = self.box.start_hub()
        self.box.lock.unlink()
        self.assertTrue(wait_until(lambda: not alive(pid), 12), "Hub kept running without its lock file")
        replacement = self.box.start_hub()
        self.assertNotEqual(replacement, pid)
        self.assertEqual(self.box.hubs(), [replacement])
        self.assertEqual(flock_holder(self.box.lock), replacement)

    def test_replacing_the_lock_file_ends_the_old_hub_so_two_never_hold_two_locks(self):
        pid = self.box.start_hub()
        self.box.lock.unlink()
        fd = os.open(self.box.lock, os.O_RDWR | os.O_CREAT, 0o600)  # same path, new inode
        os.close(fd)
        self.assertTrue(wait_until(lambda: not alive(pid), 12), "Hub kept running on a replaced lock file")


if __name__ == "__main__":
    unittest.main()
