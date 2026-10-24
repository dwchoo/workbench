"""Independent verification of the Hub process and identity contract (HB-01, seams S1/S2).

Scenarios derive from SPEC "위치", "`hub.json` schema", "Hub 프로세스 수명",
"정지 2단계" and "hub_v1 제어 socket 프로토콜", not from the worker's tests.
Every Hub is a real ``wb _hub --foreground`` subprocess in a temp HOME/XDG sandbox;
``wb hub status/stop`` are real subprocesses; hostile peers are small stand-in
processes ("fakes") this module owns. Expected values come from the kernel
(``/proc/locks`` holder, ``/proc/<pid>/stat`` start ticks, file modes). Signals go only
to processes this module started, after re-checking pid + start ticks.

Run with ``discover -s tests/hub -p 'test_hub_process_independent.py'``.
"""
from __future__ import annotations

import fcntl
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
import textwrap
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
NOT_RUNNING_TEXT = "Hub 실행 중 아님"


def proc_start_ticks(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()[19])


def proc_alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in (b"Z", b"X")


def flock_holder(path: Path) -> int | None:
    """Pid that /proc/locks names for an exclusive flock on exactly this inode."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    wanted = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    for row in Path("/proc/locks").read_text().splitlines():
        parts = row.split()
        if len(parts) >= 8 and parts[1:4] == ["FLOCK", "ADVISORY", "WRITE"]:
            major, minor, inode = parts[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == wanted:
                return int(parts[4])
    return None


def lock_is_free(path: Path) -> bool:
    """True when a fresh non-blocking flock on the named file succeeds (then drops it)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def wait_until(predicate, timeout: float, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


# A hostile / inert peer. Optionally holds hub.lock, optionally serves a socket. Frames
# it receives are appended (op names only) to --log so tests can prove none arrived.
FAKE = textwrap.dedent(
    """
    import argparse, fcntl, json, os, signal, socket, sys, threading, time
    p = argparse.ArgumentParser()
    p.add_argument("--lock"); p.add_argument("--socket"); p.add_argument("--log", required=True)
    p.add_argument("--record"); p.add_argument("--welcome", default="{}")
    p.add_argument("--silent", action="store_true")
    a = p.parse_args()
    log = lambda text: open(a.log, "a").write(text + "\\n")
    open(a.log, "a").close()
    def term(*_):
        log("SIGTERM")
        sys.exit(0)
    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    ticks = int(open("/proc/self/stat", "rb").read().rsplit(b") ", 1)[1].split()[19])
    if a.lock:
        fd = os.open(a.lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    server = None
    if a.socket:
        try: os.unlink(a.socket)
        except FileNotFoundError: pass
        server = socket.socket(socket.AF_UNIX); server.bind(a.socket); server.listen(8)
    override = json.loads(a.welcome)
    def serve(conn):
        conn.settimeout(30)
        buf = b""
        try:
            while True:
                data = conn.recv(65536)
                if not data: return
                buf += data
                while b"\\n" in buf:
                    line, buf = buf.split(b"\\n", 1)
                    frame = json.loads(line)
                    log("op:" + str(frame.get("type")))
                    if a.silent: continue
                    if frame.get("type") == "hello":
                        hub = {"pid": os.getpid(), "start_ticks": ticks, "version": {"release": "x", "commit": None, "kind": "unknown"},
                               "started_at": time.time(), "mode": "foreground"}
                        hub.update(override)
                        out = {"v": 1, "type": "welcome", "hub": hub}
                    elif frame.get("type") == "status":
                        hub = json.load(open(a.record)) if a.record else {}
                        out = {"v": 1, "id": frame["id"], "type": "result", "ok": True, "hub": hub,
                               "registry": {"state": "ok", "count": 0, "path": "-"}, "web": {"state": "not_available"},
                               "board_agent": None}
                    else:
                        out = {"v": 1, "id": frame.get("id"), "type": "result", "ok": False, "reason": "unsupported_type", "detail": "fake"}
                    conn.sendall((json.dumps(out) + "\\n").encode())
        except OSError:
            pass
        finally:
            conn.close()
    def accept_loop():
        while True:
            conn, _ = server.accept()
            threading.Thread(target=serve, args=(conn,), daemon=True).start()
    if server is not None:
        threading.Thread(target=accept_loop, daemon=True).start()
    print("ready %d %d" % (os.getpid(), ticks), flush=True)
    while True: time.sleep(1)
    """
)


class Owned:
    """A subprocess this module started; every signal re-checks pid + start ticks first."""

    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.pid = process.pid
        self.ticks = proc_start_ticks(process.pid)

    def same_process(self) -> bool:
        return self.process.poll() is None and proc_alive(self.pid) and proc_start_ticks(self.pid) == self.ticks

    def send(self, signum: int) -> None:
        if self.same_process():
            os.kill(self.pid, signum)

    def poll(self):
        return self.process.poll()

    def wait(self, timeout: float):
        return self.process.wait(timeout=timeout)


class Sandbox:
    """Temp HOME/XDG dirs plus the processes one test owns."""

    def __init__(self, case: unittest.TestCase):
        self.case = case
        self.temp = tempfile.TemporaryDirectory(prefix="wbi-")
        self.base = Path(self.temp.name)
        self.home = self.base / "h"
        self.state_home = self.base / "s"
        self.runtime = self.base / "r"
        self.config = self.base / "c"
        self.work = self.base / "w"
        for directory in (self.home, self.state_home, self.runtime, self.config, self.work):
            directory.mkdir(mode=0o700)
        self.state = self.state_home / "omp-workbench" / "hub"
        self.socket_dir = self.runtime / "omp-workbench-hub"
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8",
                    "PYTHONPATH": str(REPO / "src"), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_STATE_HOME": str(self.state_home), "XDG_RUNTIME_DIR": str(self.runtime),
                    "XDG_CONFIG_HOME": str(self.config), "WORKBENCH_HUB_AUTOSTART": "0"}
        self.marker = f"XDG_STATE_HOME={self.state_home}".encode() + b"\0"
        self.owned: list[Owned] = []
        case.addCleanup(self.cleanup)

    lock = property(lambda self: self.state / "hub.lock")
    record_path = property(lambda self: self.state / "hub.json")
    socket_path = property(lambda self: self.socket_dir / "hub.sock")

    def record(self) -> dict:
        return json.loads(self.record_path.read_text())

    def write_record(self, value: dict) -> None:
        self.record_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.record_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(value))

    def record_for(self, pid: int, ticks: int, *, socket_path: Path | None = None, **override) -> dict:
        value = {"schema_version": 1, "pid": pid, "start_ticks": ticks, "boot_id": boot_id(),
                 "uid": os.geteuid(), "socket": str(socket_path or self.socket_path),
                 "started_at": time.time(), "mode": "foreground", "log": str(self.state / "hub.log"),
                 "version": {"release": "x", "commit": None, "kind": "unknown"}}
        value.update(override)
        return value

    def popen(self, argv: list[str], **kwargs) -> Owned:
        process = subprocess.Popen(argv, env=self.env, cwd=self.work, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True, **kwargs)
        owned = Owned(process)
        self.owned.append(owned)
        return owned

    def hub(self, *extra: str, **kwargs) -> Owned:
        return self.popen([sys.executable, "-m", "workbench", "_hub", "--foreground", *extra], **kwargs)

    def start_hub(self, *extra: str, **kwargs) -> Owned:
        """Start a Hub and wait until its record names it and its socket exists."""
        hub = self.hub(*extra, **kwargs)

        def ready() -> bool:
            if hub.poll() is not None:
                return True
            try:
                record = self.record()
            except (OSError, ValueError):
                return False
            return record.get("pid") == hub.pid and Path(record.get("socket", "")).exists()

        wait_until(ready, 10)
        if hub.poll() is not None or self.record().get("pid") != hub.pid:
            out, err = hub.process.communicate(timeout=5)
            self.case.fail(f"Hub not ready: rc={hub.poll()} out={out!r} err={err!r}")
        return hub

    def sleeper(self) -> Owned:
        """An unrelated live process (stands in for a reused pid)."""
        process = self.popen([sys.executable, "-c", "import time; print('up', flush=True); time.sleep(300)"])
        self.case.assertEqual(process.process.stdout.readline().strip(), "up")
        return process

    def fake(self, *, lock: bool = False, serve: bool = False, silent: bool = False,
             welcome: dict | None = None, record: bool = False) -> tuple[Owned, Path]:
        """A hostile stand-in; returns it and its op log path. ``ready`` is awaited."""
        log = self.work / f"fake-{len(self.owned)}.log"
        args = ["--log", str(log), "--welcome", json.dumps(welcome or {})]
        if lock:
            self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            args += ["--lock", str(self.lock)]
        if serve:
            self.socket_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            args += ["--socket", str(self.socket_path)]
        if silent:
            args.append("--silent")
        if record:
            args += ["--record", str(self.record_path)]
        fake = self.popen([sys.executable, "-c", FAKE, *args])
        line = fake.process.stdout.readline().split()
        self.case.assertEqual(line[:1], ["ready"], fake.process.stderr.read() if fake.poll() is not None else "")
        self.case.assertEqual((int(line[1]), int(line[2])), (fake.pid, fake.ticks))
        return fake, log

    def cli(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "workbench", *args], env=self.env, cwd=self.work,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    def sandbox_pids(self) -> list[int]:
        """Pids (any depth, any session) whose environment carries this sandbox's marker."""
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                if self.marker in (entry / "environ").read_bytes() + b"\0":
                    found.append(int(entry.name))
            except OSError:
                continue
        return found

    def exchange(self, lines: list[dict | bytes], *, socket_path: str | None = None,
                 read: int = 1, timeout: float = 5.0) -> list[dict]:
        """Raw hub_v1: send each line, read ``read`` reply lines."""
        replies = []
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(timeout)
            probe.connect(socket_path or str(self.socket_path))
            stream = probe.makefile("rb")
            for line in lines:
                probe.sendall(line if isinstance(line, bytes) else json.dumps(line).encode() + b"\n")
            for _ in range(read):
                text = stream.readline()
                replies.append(json.loads(text) if text else {})
        return replies

    def cleanup(self) -> None:
        for owned in reversed(self.owned):
            if owned.poll() is None:
                owned.send(signal.SIGCONT)
                owned.send(signal.SIGTERM)
                try:
                    owned.wait(5)
                except subprocess.TimeoutExpired:
                    owned.send(signal.SIGKILL)
                    owned.wait(5)
            for stream in (owned.process.stdout, owned.process.stderr):
                if stream is not None:
                    stream.close()
        leaked = [pid for pid in self.sandbox_pids() if proc_alive(pid)]
        self.temp.cleanup()
        if leaked:
            # Not ours to kill by guess; report so the leak is a visible failure.
            self.case.fail(f"leaked processes in sandbox: {leaked}")


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)

    def assertHubGone(self, hub: Owned, *, within: float = 10) -> None:
        self.assertTrue(wait_until(lambda: hub.poll() is not None, within), "Hub still running")

    def assertNothingRemains(self) -> None:
        self.assertFalse(os.path.lexists(self.box.socket_path), "socket left behind")
        self.assertFalse(os.path.lexists(self.box.record_path), "hub.json left behind")
        self.assertIsNone(flock_holder(self.box.lock))
        self.assertTrue(lock_is_free(self.box.lock))

    def assertUntouched(self, process: Owned) -> None:
        self.assertTrue(process.same_process(), "process was signalled or ended")


class SingleInstanceTests(SandboxCase):
    def test_eight_concurrent_hubs_leave_exactly_one_lock_holder(self):
        hubs = [self.box.hub() for _ in range(8)]
        self.assertTrue(wait_until(lambda: sum(h.poll() is None for h in hubs) <= 1 and self.box.record_path.exists(), 20))
        # Losers exit 0 (SPEC step 2); give stragglers time to finish.
        self.assertTrue(wait_until(lambda: sum(h.poll() is None for h in hubs) == 1, 20))
        winners = [h for h in hubs if h.poll() is None]
        self.assertEqual(len(winners), 1)
        for hub in hubs:
            if hub is not winners[0]:
                self.assertEqual(hub.poll(), 0, hub.process.stderr.read())
        winner = winners[0]
        self.assertTrue(wait_until(lambda: self.box.record_path.exists() and self.box.record()["pid"] == winner.pid, 10))
        self.assertEqual(flock_holder(self.box.lock), winner.pid)
        self.assertEqual(self.box.record()["pid"], winner.pid)
        self.assertEqual(self.box.record()["start_ticks"], winner.ticks)
        self.assertEqual(sorted(p.name for p in self.box.socket_dir.iterdir()), ["hub.sock"])
        welcome = self.box.exchange([{"v": 1, "type": "hello", "client": "t"}])[0]
        self.assertEqual(welcome["hub"]["pid"], winner.pid)
        # A second wave of 8 against the live Hub changes nothing.
        before = self.box.record_path.read_bytes()
        inode = self.box.socket_path.lstat().st_ino
        wave = [self.box.hub() for _ in range(8)]
        for hub in wave:
            self.assertEqual(hub.wait(20), 0, hub.process.stderr.read())
        self.assertUntouched(winner)
        self.assertEqual(self.box.record_path.read_bytes(), before)
        self.assertEqual(self.box.socket_path.lstat().st_ino, inode)
        self.assertEqual(flock_holder(self.box.lock), winner.pid)

    def test_hub_is_not_started_by_status_or_stop_and_nothing_is_left(self):
        for args in (("hub", "status"), ("hub", "status", "--json"), ("hub", "stop"), ("hub", "stop", "--yes")):
            result = self.box.cli(*args)
            self.assertEqual(result.returncode, 3, (args, result.stdout, result.stderr))
        text = self.box.cli("hub", "status")
        self.assertIn(NOT_RUNNING_TEXT, text.stdout + text.stderr)
        as_json = json.loads(self.box.cli("hub", "status", "--json").stdout)
        self.assertEqual(as_json["state"], "not_running")
        self.assertEqual(self.box.sandbox_pids(), [])
        self.assertFalse(os.path.lexists(self.box.socket_path))
        self.assertFalse(os.path.lexists(self.box.record_path))
        self.assertFalse((self.box.runtime / "omp-workbench").exists())


class LayoutTests(SandboxCase):
    def test_hostile_umask_still_yields_private_state_socket_and_record(self):
        hub = self.box.start_hub(umask=0)
        modes = {name: stat.S_IMODE(path.lstat().st_mode) for name, path in {
            "state": self.box.state, "socket_dir": self.box.socket_dir, "socket": self.box.socket_path,
            "record": self.box.record_path, "lock": self.box.lock}.items()}
        self.assertEqual(modes, {"state": 0o700, "socket_dir": 0o700, "socket": 0o600, "record": 0o600, "lock": 0o600})
        self.assertTrue(stat.S_ISSOCK(self.box.socket_path.lstat().st_mode))
        self.assertEqual(flock_holder(self.box.lock), hub.pid)
        self.assertEqual(self.box.record()["pid"], hub.pid)
        self.assertEqual(self.box.record()["start_ticks"], proc_start_ticks(hub.pid))
        self.assertEqual(self.box.record()["boot_id"], boot_id())

    def test_symlinked_state_dir_is_refused_and_nothing_is_created_behind_it(self):
        target = self.box.base / "elsewhere"
        target.mkdir(mode=0o700)
        (self.box.state_home / "omp-workbench").mkdir(mode=0o700)
        self.box.state.symlink_to(target)
        hub = self.box.hub()
        self.assertNotEqual(hub.wait(15), 0)
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse(os.path.lexists(self.box.socket_path))

    def test_hub_never_creates_anything_under_the_backend_runtime_base(self):
        self.box.start_hub()
        self.assertFalse((self.box.runtime / "omp-workbench").exists())
        self.assertFalse((self.box.base / "s" / "omp-workbench" / "hub" / "omp-workbench").exists())


class WatchdogTests(SandboxCase):
    def test_deleting_hub_lock_ends_the_hub_within_four_seconds(self):
        hub = self.box.start_hub()
        started = time.monotonic()
        self.box.lock.unlink()
        self.assertTrue(wait_until(lambda: hub.poll() is not None, 4.0 - (time.monotonic() - started)),
                        "Hub survived hub.lock deletion for 4 s")
        self.assertLessEqual(time.monotonic() - started, 4.0)
        self.assertFalse(os.path.lexists(self.box.socket_path), "socket left behind")
        self.assertFalse(os.path.lexists(self.box.record_path), "hub.json left behind")

    def test_replacing_hub_lock_with_another_inode_ends_the_hub(self):
        hub = self.box.start_hub()
        original = self.box.lock.stat().st_ino
        replacement = self.box.state / "hub.lock.new"
        replacement.write_bytes(b"")
        replacement.chmod(0o600)
        os.rename(replacement, self.box.lock)
        self.assertNotEqual(self.box.lock.stat().st_ino, original)
        started = time.monotonic()
        self.assertTrue(wait_until(lambda: hub.poll() is not None, 4.0), "Hub survived lock replacement")
        self.assertLessEqual(time.monotonic() - started, 4.0)
        self.assertFalse(os.path.lexists(self.box.socket_path))
        self.assertFalse(os.path.lexists(self.box.record_path))

    def test_replacing_hub_lock_with_a_symlink_ends_the_hub(self):
        hub = self.box.start_hub()
        self.box.lock.unlink()
        self.box.lock.symlink_to(self.box.work)
        self.assertTrue(wait_until(lambda: hub.poll() is not None, 4.0), "Hub survived lock-to-symlink swap")

    def test_deleting_the_state_directory_ends_the_hub(self):
        hub = self.box.start_hub()
        started = time.monotonic()
        shutil.rmtree(self.box.state)
        self.assertTrue(wait_until(lambda: hub.poll() is not None, 4.0), "Hub survived state dir deletion")
        self.assertLessEqual(time.monotonic() - started, 4.0)
        self.assertFalse(os.path.lexists(self.box.socket_path))

    def test_deleting_the_whole_temp_home_state_tree_ends_the_hub(self):
        hub = self.box.start_hub()
        shutil.rmtree(self.box.state_home)
        self.assertTrue(wait_until(lambda: hub.poll() is not None, 4.0), "Hub survived temp-HOME deletion")

    def test_swapping_the_state_directory_for_a_new_one_ends_the_hub(self):
        hub = self.box.start_hub()
        moved = self.box.base / "moved-state"
        os.rename(self.box.state, moved)
        self.box.state.mkdir(mode=0o700)
        self.assertTrue(wait_until(lambda: hub.poll() is not None, 4.0), "Hub survived state dir swap")
        self.assertFalse(os.path.lexists(self.box.socket_path))
        # The new directory is not the old Hub's to clean up beyond its own record.
        self.assertFalse(self.box.record_path.exists())

    def test_hub_started_while_lock_is_replaced_takes_over_and_old_hub_leaves_it_intact(self):
        old = self.box.start_hub()
        self.box.lock.unlink()
        new = self.box.hub()  # may win a fresh lock before the old watchdog fires
        self.assertTrue(wait_until(lambda: old.poll() is not None, 4.0), "old Hub survived lock deletion")
        self.assertTrue(wait_until(lambda: new.poll() is None and self.box.record_path.exists()
                                   and self.box.record().get("pid") == new.pid, 10),
                        f"new Hub did not take over: rc={new.poll()}")
        # Single instance again, and the old Hub's cleanup did not remove the new Hub's files.
        self.assertEqual(flock_holder(self.box.lock), new.pid)
        self.assertTrue(self.box.socket_path.exists())
        welcome = self.box.exchange([{"v": 1, "type": "hello", "client": "t"}])[0]
        self.assertEqual(welcome["hub"]["pid"], new.pid)
        result = self.box.cli("hub", "status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["hub"]["pid"], new.pid)


class StopTests(SandboxCase):
    def test_wb_hub_stop_ends_hub_and_leaves_no_socket_record_or_lock(self):
        hub = self.box.start_hub()
        result = self.box.cli("hub", "stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Hub를 멈췄습니다. 켜져 있던 WB 프로젝트는 계속 실행됩니다.", result.stdout)
        self.assertEqual(hub.wait(10), 0)
        self.assertNothingRemains()
        self.assertEqual(self.box.cli("hub", "status").returncode, 3)
        # A fresh Hub can start again over the cleaned state.
        again = self.box.start_hub()
        self.assertNotEqual(again.pid, hub.pid)

    def test_sigterm_stops_cleanly_and_sighup_sigint_behave_as_specified(self):
        hub = self.box.start_hub()
        hub.send(signal.SIGHUP)
        time.sleep(0.5)
        self.assertUntouched(hub)
        self.assertEqual(flock_holder(self.box.lock), hub.pid)
        hub.send(signal.SIGTERM)
        self.assertEqual(hub.wait(10), 0)
        self.assertNothingRemains()
        second = self.box.start_hub()
        second.send(signal.SIGINT)
        self.assertEqual(second.wait(10), 0)
        self.assertNothingRemains()

    def test_stop_does_not_leave_files_a_later_wb_would_misjudge(self):
        hub = self.box.start_hub()
        keep_record = self.box.record_path.read_bytes()
        self.assertEqual(self.box.cli("hub", "stop", "--yes").returncode, 0)
        self.assertEqual(hub.wait(10), 0)
        self.assertNotEqual(keep_record, b"")
        for name in ("hub.sock",):
            self.assertFalse((self.box.socket_dir / name).exists())

    def test_unresponsive_hub_gets_sigterm_only_via_stop_and_stop_reports_pid_and_exit_1(self):
        hub = self.box.start_hub()
        hub.send(signal.SIGSTOP)
        self.assertTrue(wait_until(lambda: self.state_letter(hub.pid) == b"T", 3))
        result = self.box.cli("hub", "stop")
        # A stopped process cannot act on SIGTERM; SPEC: still there -> failure with the pid.
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(str(hub.pid), result.stdout + result.stderr)
        self.assertTrue(hub.same_process(), "Hub must not be SIGKILLed")
        # The SIGTERM was queued for exactly this Hub: it exits cleanly once continued.
        hub.send(signal.SIGCONT)
        self.assertEqual(hub.wait(10), 0, "no SIGTERM was delivered to the unresponsive Hub")
        self.assertNothingRemains()

    @staticmethod
    def state_letter(pid: int) -> bytes:
        return Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()[0]

    def test_wrong_stop_token_is_refused_and_hub_stays(self):
        hub = self.box.start_hub()
        replies = self.box.exchange([
            {"v": 1, "type": "hello", "client": "t"},
            {"v": 1, "id": "1", "type": "stop_confirm", "token": "not-the-token"},
            {"v": 1, "id": "2", "type": "stop_request"},
        ], read=3)
        self.assertEqual(replies[1]["ok"], False)
        self.assertEqual(replies[1]["reason"], "stop_token_mismatch")
        self.assertTrue(replies[2]["ok"])
        self.assertEqual(replies[2]["active"], [])
        time.sleep(0.3)
        self.assertUntouched(hub)
        # Another connection cannot use a token issued on a different offer path with a wrong value.
        wrong = self.box.exchange([{"v": 1, "type": "hello", "client": "t"},
                                   {"v": 1, "id": "3", "type": "stop_confirm", "token": replies[2]["token"][::-1]}], read=2)
        self.assertEqual(wrong[1]["reason"], "stop_token_mismatch")
        self.assertUntouched(hub)

    def test_expired_stop_token_is_refused(self):
        hub = self.box.start_hub("--stop-token-ttl", "1")
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(5)
            probe.connect(str(self.box.socket_path))
            stream = probe.makefile("rb")
            probe.sendall(b'{"v":1,"type":"hello","client":"t"}\n')
            stream.readline()
            probe.sendall(b'{"v":1,"id":"1","type":"stop_request"}\n')
            token = json.loads(stream.readline())["token"]
            time.sleep(1.4)
            probe.sendall(json.dumps({"v": 1, "id": "2", "type": "stop_confirm", "token": token}).encode() + b"\n")
            reply = json.loads(stream.readline())
        self.assertEqual((reply["ok"], reply["reason"]), (False, "stop_token_mismatch"))
        self.assertUntouched(hub)

    def test_valid_token_stops_the_hub_through_the_raw_socket(self):
        hub = self.box.start_hub()
        replies = self.box.exchange([
            {"v": 1, "type": "hello", "client": "t"},
            {"v": 1, "id": "1", "type": "stop_request"},
        ], read=2)
        token = replies[1]["token"]
        self.assertEqual(replies[1]["expires_in"], 60)
        confirm = self.box.exchange([{"v": 1, "type": "hello", "client": "t"},
                                     {"v": 1, "id": "2", "type": "stop_confirm", "token": token}], read=2)
        self.assertEqual(confirm[1]["ok"], True)
        self.assertEqual(confirm[1]["stopping"], True)
        self.assertEqual(hub.wait(10), 0)
        self.assertNothingRemains()


class StaleRecordTests(SandboxCase):
    def start_and_sigkill_hub(self) -> tuple[Owned, dict]:
        hub = self.box.start_hub()
        record = self.box.record()
        hub.send(signal.SIGKILL)
        hub.wait(10)
        self.assertTrue(lock_is_free(self.box.lock))
        self.assertTrue(self.box.record_path.exists())
        return hub, record

    def test_record_left_by_a_killed_hub_is_stale_not_running(self):
        self.start_and_sigkill_hub()
        status = self.box.cli("hub", "status")
        self.assertEqual(status.returncode, 3, status.stdout + status.stderr)
        self.assertIn(NOT_RUNNING_TEXT, status.stdout + status.stderr)
        self.assertEqual(json.loads(self.box.cli("hub", "status", "--json").stdout)["state"], "not_running")
        stop = self.box.cli("hub", "stop", "--yes")
        self.assertEqual(stop.returncode, 3)
        self.assertEqual(self.box.sandbox_pids(), [])

    def test_stale_record_naming_a_reused_live_pid_is_never_the_hub_and_never_signalled(self):
        _dead, _ = self.start_and_sigkill_hub()
        sleeper = self.box.sleeper()
        # The "reused pid": a live process with its genuine start ticks, nobody holds the lock.
        self.box.write_record(self.box.record_for(sleeper.pid, sleeper.ticks))
        for args in (("hub", "status"), ("hub", "status", "--json"), ("hub", "stop"), ("hub", "stop", "--yes")):
            result = self.box.cli(*args)
            self.assertEqual(result.returncode, 3, (args, result.stdout, result.stderr))
        self.assertUntouched(sleeper)

    def test_restart_over_leftovers_replaces_record_and_socket(self):
        _dead, old = self.start_and_sigkill_hub()
        self.assertTrue(os.path.lexists(self.box.socket_path), "SIGKILL should leave the old socket file")
        new = self.box.start_hub()
        self.assertNotEqual(new.pid, old["pid"])
        self.assertEqual(self.box.record()["pid"], new.pid)
        reply = self.box.exchange([{"v": 1, "type": "hello", "client": "t"}])[0]
        self.assertEqual(reply["hub"]["pid"], new.pid)
        status = self.box.cli("hub", "status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["hub"]["pid"], new.pid)


class MutatedRecordTests(SandboxCase):
    """A real, healthy Hub plus a record that no longer proves it: never `running`, never signalled."""

    def assert_unknown_and_unsignalled(self, hub: Owned, *, also: tuple[Owned, ...] = ()) -> None:
        status = self.box.cli("hub", "status", "--json")
        self.assertEqual(status.returncode, 1, status.stdout + status.stderr)
        info = json.loads(status.stdout)
        self.assertEqual(info["state"], "unknown")
        self.assertTrue(info.get("reason"))
        plain = self.box.cli("hub", "status")
        self.assertEqual(plain.returncode, 1)
        stop = self.box.cli("hub", "stop", "--yes")
        self.assertEqual(stop.returncode, 1, stop.stdout + stop.stderr)
        self.assertNotIn("멈췄습니다", stop.stdout)
        time.sleep(0.2)
        self.assertUntouched(hub)
        self.assertEqual(flock_holder(self.box.lock), hub.pid)
        for other in also:
            self.assertUntouched(other)

    def test_record_pid_differing_from_the_lock_holder_is_unknown_and_the_named_pid_is_not_signalled(self):
        hub = self.box.start_hub()
        sleeper = self.box.sleeper()
        record = self.box.record()
        self.box.write_record({**record, "pid": sleeper.pid, "start_ticks": sleeper.ticks})
        self.assert_unknown_and_unsignalled(hub, also=(sleeper,))

    def test_record_with_wrong_start_ticks_is_unknown_and_not_signalled(self):
        hub = self.box.start_hub()
        self.box.write_record({**self.box.record(), "start_ticks": hub.ticks + 7})
        self.assert_unknown_and_unsignalled(hub)

    def test_record_with_foreign_boot_id_is_unknown_and_not_signalled(self):
        hub = self.box.start_hub()
        self.box.write_record({**self.box.record(), "boot_id": "00000000-0000-0000-0000-000000000000"})
        self.assert_unknown_and_unsignalled(hub)

    def test_record_with_foreign_uid_is_unknown_and_not_signalled(self):
        hub = self.box.start_hub()
        self.box.write_record({**self.box.record(), "uid": os.geteuid() + 1})
        self.assert_unknown_and_unsignalled(hub)

    def test_record_with_wrong_schema_or_garbage_is_unknown_while_lock_is_held(self):
        hub = self.box.start_hub()
        good = self.box.record()
        for broken in ({**good, "schema_version": 2}, {**good, "pid": "1"}, {**good, "start_ticks": None},
                       {k: v for k, v in good.items() if k != "socket"}):
            self.box.write_record(broken)
            self.assert_unknown_and_unsignalled(hub)
        self.box.record_path.write_text("not json")
        os.chmod(self.box.record_path, 0o600)
        self.assert_unknown_and_unsignalled(hub)
        self.box.record_path.unlink()
        self.assert_unknown_and_unsignalled(hub)

    def test_group_readable_record_is_not_trusted(self):
        hub = self.box.start_hub()
        os.chmod(self.box.record_path, 0o644)
        self.assert_unknown_and_unsignalled(hub)

    def test_socket_served_by_another_process_is_rejected_by_peercred_before_any_frame_is_sent(self):
        hub = self.box.start_hub()
        genuine = self.box.record()
        impostor_dir = self.box.base / "imp"
        impostor_dir.mkdir(mode=0o700)
        impostor_path = impostor_dir / "hub.sock"
        # The impostor serves its own socket path and forges the real Hub's welcome.
        forged = {"pid": hub.pid, "start_ticks": hub.ticks}
        impostor, log = self.fake_on(impostor_path, welcome=forged)
        self.box.write_record({**genuine, "socket": str(impostor_path)})
        self.assert_unknown_and_unsignalled(hub, also=(impostor,))
        ops = log.read_text().split() if log.exists() else []
        self.assertEqual([op for op in ops if op != "SIGTERM"], [], "client spoke to an unproven peer")

    def fake_on(self, path: Path, **kwargs) -> tuple[Owned, Path]:
        log = self.box.work / f"imp-{len(self.box.owned)}.log"
        args = ["--log", str(log), "--socket", str(path), "--welcome", json.dumps(kwargs.get("welcome", {}))]
        fake = self.box.popen([sys.executable, "-c", FAKE, *args])
        self.assertEqual(fake.process.stdout.readline().split()[:1], ["ready"])
        return fake, log


class FakeHolderTests(SandboxCase):
    """Lock holder is a stand-in with the right pid; each forged field must still block `running`."""

    def prepare(self, *, welcome: dict | None = None, silent: bool = False, **record_override):
        fake, log = self.box.fake(lock=True, serve=True, silent=silent, welcome=welcome, record=True)
        self.box.write_record(self.box.record_for(fake.pid, fake.ticks, **record_override))
        return fake, log

    def test_positive_control_a_faithful_standin_is_running(self):
        fake, _log = self.prepare()
        result = self.box.cli("hub", "status", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "running")
        self.assertEqual(json.loads(result.stdout)["hub"]["pid"], fake.pid)

    def test_welcome_with_a_different_pid_is_unknown_and_nothing_is_signalled_or_requested(self):
        fake, log = self.prepare(welcome={"pid": os.getpid() + 100000})
        for args in (("hub", "status", "--json"), ("hub", "stop", "--yes")):
            result = self.box.cli(*args)
            self.assertEqual(result.returncode, 1, (args, result.stdout, result.stderr))
        self.assertUntouched(fake)
        ops = log.read_text().split()
        self.assertNotIn("op:stop_request", ops)
        self.assertNotIn("op:stop_confirm", ops)
        self.assertNotIn("op:status", ops)
        self.assertIn("welcome_mismatch", self.box.cli("hub", "status", "--json").stdout)

    def test_welcome_with_a_different_start_ticks_is_unknown_and_nothing_is_signalled(self):
        fake, log = self.prepare(welcome={"start_ticks": 1})
        for args in (("hub", "status", "--json"), ("hub", "stop", "--yes")):
            result = self.box.cli(*args)
            self.assertEqual(result.returncode, 1, (args, result.stdout, result.stderr))
        self.assertUntouched(fake)
        self.assertNotIn("op:stop_request", log.read_text().split())

    def test_positive_control_a_silent_proven_peer_receives_sigterm_through_stop(self):
        fake, log = self.prepare(silent=True)
        result = self.box.cli("hub", "stop", "--yes", timeout=60)
        self.assertIn("SIGTERM", log.read_text().split(), result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fake.wait(5), 0)

    def test_silent_peer_with_wrong_start_ticks_is_never_sigtermed(self):
        fake, log = self.prepare(silent=True)
        # Same pid, plausible but wrong start ticks: what a recycled pid looks like.
        self.box.write_record(self.box.record_for(fake.pid, fake.ticks + 3))
        result = self.box.cli("hub", "stop", "--yes", timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertUntouched(fake)
        self.assertNotIn("SIGTERM", log.read_text().split())

    def test_silent_peer_whose_pid_is_not_the_lock_holder_is_never_sigtermed(self):
        holder, holder_log = self.box.fake(lock=True)
        silent, silent_log = self.box.fake(serve=True, silent=True)
        # Record names the silent socket server; the lock is held by someone else.
        self.box.write_record(self.box.record_for(silent.pid, silent.ticks))
        result = self.box.cli("hub", "stop", "--yes", timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertUntouched(holder)
        self.assertUntouched(silent)
        self.assertNotIn("SIGTERM", holder_log.read_text().split())
        self.assertNotIn("SIGTERM", silent_log.read_text().split())

    def test_record_naming_the_lock_holder_but_socket_served_by_another_pid_is_never_sigtermed(self):
        holder, holder_log = self.box.fake(lock=True)
        silent, silent_log = self.box.fake(serve=True, silent=True)
        self.box.write_record(self.box.record_for(holder.pid, holder.ticks))
        result = self.box.cli("hub", "stop", "--yes", timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertUntouched(holder)
        self.assertUntouched(silent)
        self.assertNotIn("SIGTERM", holder_log.read_text().split())
        self.assertEqual(silent_log.read_text().strip() if silent_log.exists() else "", "")

    def test_faithful_protocol_peer_that_is_not_the_lock_holder_is_never_trusted(self):
        # Every check except lock-holder == record.pid passes: pid is alive with true start
        # ticks, the socket peer and the welcome name that pid. Only the lock says otherwise.
        holder, _holder_log = self.box.fake(lock=True)
        peer, peer_log = self.box.fake(serve=True, record=True)
        self.box.write_record(self.box.record_for(peer.pid, peer.ticks))
        status = self.box.cli("hub", "status", "--json")
        self.assertEqual(status.returncode, 1, status.stdout + status.stderr)
        self.assertEqual(json.loads(status.stdout)["state"], "unknown")
        stop = self.box.cli("hub", "stop", "--yes")
        self.assertEqual(stop.returncode, 1, stop.stdout + stop.stderr)
        self.assertUntouched(holder)
        self.assertUntouched(peer)
        self.assertNotIn("op:stop_request", peer_log.read_text().split())
        self.assertNotIn("op:status", peer_log.read_text().split())

    def test_lock_held_by_a_non_hub_with_no_record_is_unknown_and_never_signalled(self):
        holder, log = self.box.fake(lock=True)
        status = self.box.cli("hub", "status", "--json")
        self.assertEqual(status.returncode, 1, status.stdout + status.stderr)
        info = json.loads(status.stdout)
        self.assertEqual(info["state"], "unknown")
        self.assertTrue(info["reason"])
        stop = self.box.cli("hub", "stop", "--yes")
        self.assertEqual(stop.returncode, 1)
        self.assertUntouched(holder)
        self.assertEqual(self.box.hub().wait(15), 0)  # a Hub started now loses the lock quietly
        self.assertUntouched(holder)
        self.assertEqual(self.box.sandbox_pids().count(holder.pid), 1)

    def test_valid_looking_record_but_socket_missing_is_unknown_and_not_signalled(self):
        holder, log = self.box.fake(lock=True)
        self.box.write_record(self.box.record_for(holder.pid, holder.ticks))
        for args in (("hub", "status", "--json"), ("hub", "stop", "--yes")):
            result = self.box.cli(*args)
            self.assertEqual(result.returncode, 1, (args, result.stdout, result.stderr))
        self.assertUntouched(holder)


class TerminateReproofTests(SandboxCase):
    """`stop_hub` -> SIGTERM fallback re-proves the identity after pinning the pid.

    The race between the client's judgement and the signal cannot be hit through the CLI, so
    the client judgement is replaced by one that claims "unresponsive but proven" for a
    record that is no longer true, and the public ``stop_hub`` must still refuse to signal.
    """

    def run_stop_hub(self, record: dict):
        from workbench.hub import client as client_module
        from workbench.hub import process as process_module
        from workbench.hub.paths import HubLayout

        class Unresponsive:
            def __init__(self, *_args, **_kwargs):
                raise process_module.HubUnresponsive("timeout", record)

        original = client_module.HubClient
        client_module.HubClient = Unresponsive
        try:
            return process_module.stop_hub(lambda _active: True, HubLayout(self.box.state))
        finally:
            client_module.HubClient = original

    def test_positive_control_proven_lock_holder_is_terminated(self):
        holder, log = self.box.fake(lock=True)
        result = self.run_stop_hub(self.box.record_for(holder.pid, holder.ticks))
        self.assertEqual((result.status, result.signalled), ("stopped", True))
        self.assertIn("SIGTERM", log.read_text().split())

    def test_pid_with_wrong_start_ticks_is_not_signalled(self):
        holder, log = self.box.fake(lock=True)
        result = self.run_stop_hub(self.box.record_for(holder.pid, holder.ticks + 5))
        self.assertNotEqual(result.status, "stopped")
        self.assertFalse(result.signalled)
        self.assertUntouched(holder)
        self.assertNotIn("SIGTERM", log.read_text().split())

    def test_live_pid_that_does_not_hold_the_lock_is_not_signalled(self):
        sleeper = self.box.sleeper()
        self.box.state.mkdir(parents=True, mode=0o700)
        self.box.lock.write_bytes(b"")
        os.chmod(self.box.lock, 0o600)
        result = self.run_stop_hub(self.box.record_for(sleeper.pid, sleeper.ticks))
        self.assertFalse(result.signalled)
        self.assertUntouched(sleeper)


if __name__ == "__main__":
    unittest.main()
