"""Independent S6/S7 verification of ``ProcessPin`` and ``scan_tracked`` (CS-01).

Contracts: CS-01 ticket; SPEC "추적 범위와 잔존 확인" and "신원·신호 불변식" 1-3, 7; BRIEF CS-AC-08.

Expected values come from this file only: ``/proc/<pid>/stat`` read here, ``os.getuid()``,
``/proc/sys/kernel/random/boot_id`` and pids recorded by the commands themselves (``$$``, ``$!``).
Production scan/identity output is never used to compute an expectation.

Safety: signals are only ever delivered to processes this file started. Every test starts a
sentinel process in its own session and asserts it survives; leftovers are killed through a
pidfd whose start ticks were re-checked by this file's own helper, then asserted gone.
"""
from dataclasses import replace
from pathlib import Path
import errno
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from uuid import uuid4

from workbench.backend import command_stop
from workbench.backend.command_stop import (
    CLOSED, DIFFERENT_UID, ENDED, IDENTITY_CHANGED, PERMISSION_DENIED, PINNED, SENT, STALE,
    LinuxStopEnvironment, ProcStat, ProcessPin, StopEnvironment, TrackScan, scan_tracked,
)
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell

BASH = ShellChoice("bash", "/usr/bin/bash")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
UID = os.getuid()


# --------------------------------------------------------------------------- own /proc helpers
def stat_fields(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()


def proc(pid):
    f = stat_fields(pid)
    return {"state": f[0], "ppid": int(f[1]), "pgrp": int(f[2]), "session": int(f[3]), "start": int(f[19])}


def alive(pid, start=None):
    try:
        info = proc(pid)
    except (OSError, IndexError):
        return False
    return info["state"] not in {"Z", "X"} and (start is None or info["start"] == start)


def exists(pid):
    return Path(f"/proc/{pid}").exists()


def all_stats():
    out = {}
    for name in os.listdir("/proc"):
        if name.isdecimal():
            try:
                out[int(name)] = proc(int(name))
            except (OSError, IndexError, ValueError):
                pass
    return out


def descendants_of(root):
    stats, found, pending = all_stats(), set(), [root]
    while pending:
        parent = pending.pop()
        for pid, info in stats.items():
            if info["ppid"] == parent and pid not in found:
                found.add(pid)
                pending.append(pid)
    return found


def kill_exact(pid, start):
    """SIGKILL ``pid`` only if it still has ``start`` (pidfd first, then re-read). Test-owned helper."""
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if alive(pid, start):
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except OSError:
                pass
    finally:
        os.close(fd)


def open_fds():
    return len(os.listdir("/proc/self/fd"))


def read_pid(path, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            text = Path(path).read_text().strip()
            if text:
                return int(text)
        except (OSError, ValueError):
            pass
        time.sleep(.02)
    raise AssertionError(f"pid file never written: {path}")


def wait_until(predicate, seconds=10.0, what="condition"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError(f"timed out waiting for {what}")


def ports(shell):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "e" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


def until(shell, condition, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = shell.poll(.02)
        shell.display_bytes()
        if condition(state):
            return state
    raise AssertionError({"timeout": shell.snapshot()})


# --------------------------------------------------------------------------- S7 environments
class RecordingEnv(LinuxStopEnvironment):
    """Real /proc and pidfd, with an event log and optional error injection (S7).

    ``mutate_read(n, stat)`` is called for the n-th ``read_stat`` (1-based, counted from the last
    ``arm()``) and may return a replacement. ``send_error`` makes ``pidfd_send_signal`` raise instead
    of delivering. Every delivered or attempted signal is logged.
    """

    def __init__(self, *, mutate_read=None, send_error=None, open_error=None, exited_after=None, boot=None,
                 armed=True):
        self.armed = armed
        self.events, self.opened, self.closed = [], [], []
        self.mutate_read, self.send_error, self.open_error = mutate_read, send_error, open_error
        self.exited_after, self.boot = exited_after, boot
        self.reads = 0
        self.mutated_event_indexes = []

    def arm(self):
        self.reads, self.armed = 0, True

    def boot_id(self):
        return self.boot if self.boot is not None else super().boot_id()

    def read_stat(self, pid):
        stat = super().read_stat(pid)
        self.reads += 1
        self.events.append(("read", pid))
        if self.mutate_read is not None and self.armed:
            changed = self.mutate_read(self.reads, stat)
            if changed is not stat:
                self.mutated_event_indexes.append(len(self.events) - 1)
            stat = changed
        return stat

    def pidfd_open(self, pid):
        self.events.append(("pidfd_open", pid))
        if self.open_error is not None:
            raise self.open_error
        fd = super().pidfd_open(pid)
        self.opened.append(fd)
        return fd

    def pidfd_exited(self, fd):
        self.events.append(("exited", fd))
        if self.exited_after is not None and len(self.opened) and self.exited_after():
            return True
        return super().pidfd_exited(fd)

    def pidfd_send_signal(self, fd, signum):
        self.events.append(("signal", signum))
        if self.send_error is not None:
            raise self.send_error
        super().pidfd_send_signal(fd, signum)

    def close(self, fd):
        self.events.append(("close", fd))
        self.closed.append(fd)
        super().close(fd)

    @property
    def signal_events(self):
        return [i for i, e in enumerate(self.events) if e[0] == "signal"]


class FakeProcEnv(StopEnvironment):
    """A synthetic /proc table for scan_tracked classification of shapes real kernels rarely produce."""

    def __init__(self, stats):
        self.table = {s.pid: s for s in stats}

    def list_pids(self):
        return list(self.table)

    def read_stat(self, pid):
        return self.table.get(pid)

    def boot_id(self):
        return BOOT_ID

    def pidfd_open(self, pid):
        raise AssertionError("scan must not open pidfds")

    def pidfd_send_signal(self, fd, signum):
        raise AssertionError("scan must never signal")


class OverlayEnv(LinuxStopEnvironment):
    """Real /proc plus synthetic extra processes (a real host-shell background job cannot coexist with a
    managed run: the shell holds itself closed with ``manual_jobs``, so that shape is overlaid)."""

    def __init__(self, extra):
        self.extra = {s.pid: s for s in extra}

    def list_pids(self):
        return super().list_pids() + list(self.extra)

    def read_stat(self, pid):
        return self.extra.get(pid) or super().read_stat(pid)


class Spawned:
    """Test-started processes outside any shell, each in its own session, with exact ticks."""

    def __init__(self, test):
        self.test = test
        self.procs = []

    def sleeper(self, seconds=300, **kw):
        p = subprocess.Popen(["sleep", str(seconds)], start_new_session=kw.pop("new_session", True), **kw)
        self.procs.append((p, proc(p.pid)["start"]))
        return p, proc(p.pid)["start"]

    def cleanup(self):
        for p, start in self.procs:
            if p.poll() is None:
                kill_exact(p.pid, start)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise AssertionError(f"leaked spawned process {p.pid}")


class PinCase(unittest.TestCase):
    def setUp(self):
        self.spawned = Spawned(self)
        self.addCleanup(self.spawned.cleanup)
        self.sentinel, self.sentinel_start = self.spawned.sleeper()
        self.addCleanup(self.assert_sentinel_survived)
        self.fds_before = open_fds()

    def assert_sentinel_survived(self):
        self.assertTrue(self.sentinel.poll() is None and alive(self.sentinel.pid, self.sentinel_start),
                        "sentinel (not part of any pin) was signalled")

    def target(self, **kw):
        p, start = self.spawned.sleeper(**kw)
        return p, start

    def open_pin(self, p, start, env=None, **kw):
        env = env or LinuxStopEnvironment()
        pin = ProcessPin.open(env, p.pid, start, uid=kw.get("uid", UID), boot_id=kw.get("boot_id", BOOT_ID))
        self.addCleanup(pin.close)
        return pin

    def assert_untouched(self, p):
        self.assertIsNone(p.poll(), "target process was signalled")


# --------------------------------------------------------------------------- real-process pin behaviour
class PinRealProcessTests(PinCase):
    def test_matching_identity_pins_and_zero_signal_is_harmless(self):
        p, start = self.target()
        pin = self.open_pin(p, start)
        self.assertEqual(pin.status, PINNED)
        self.assertEqual(pin.verify(), PINNED)
        self.assertEqual(pin.send(0), SENT)
        self.assert_untouched(p)
        self.assertTrue(alive(p.pid, start))

    def test_send_delivers_only_to_the_pinned_process(self):
        p, start = self.target()
        twin, _ = self.target()  # identical argv; argv must never decide a target
        pin = self.open_pin(p, start)
        self.assertEqual(pin.send(signal.SIGTERM), SENT)
        self.assertEqual(p.wait(timeout=5), -signal.SIGTERM)
        self.assert_untouched(twin)
        self.assertEqual(pin.send(signal.SIGTERM), ENDED, "a dead target must never be signalled again")

    def test_wrong_start_ticks_is_stale_and_never_signals(self):
        p, start = self.target()
        for wrong in (start - 1, start + 1, 0, 2 ** 40):
            with self.subTest(start_ticks=wrong):
                pin = self.open_pin(p, wrong)
                self.assertEqual(pin.status, STALE)
                self.assertEqual(pin.send(signal.SIGKILL), STALE)
                self.assertEqual(pin.verify(), STALE)
                self.assert_untouched(p)
        self.assertTrue(alive(p.pid, start))

    def test_pid_reuse_like_mismatch_same_argv_other_process(self):
        # The recorded identity belongs to process A; the pid now names process B with the same argv.
        a, a_start = self.target()
        time.sleep(.05)  # start ticks are 10 ms resolution; two processes in one tick would share them
        b, b_start = self.target()
        self.assertNotEqual(a_start, b_start)
        reused = self.open_pin(b, a_start)  # B's pid, A's start ticks
        self.assertEqual(reused.status, STALE)
        self.assertEqual(reused.send(signal.SIGKILL), STALE)
        self.assert_untouched(b)
        self.assert_untouched(a)

    def test_exited_and_reaped_pid_is_ended(self):
        p, start = self.spawned.sleeper(0.05)
        p.wait(timeout=5)
        self.assertFalse(exists(p.pid))
        pin = ProcessPin.open(LinuxStopEnvironment(), p.pid, start, uid=UID, boot_id=BOOT_ID)
        self.assertEqual(pin.status, ENDED)
        self.assertEqual(pin.send(signal.SIGKILL), ENDED)
        pin.close()

    def test_zombie_counts_as_ended_and_is_not_signalled(self):
        p, start = self.spawned.sleeper(0.05)
        wait_until(lambda: proc(p.pid)["state"] == "Z", what="zombie")  # not reaped: p.wait() not called yet
        env = RecordingEnv()
        pin = ProcessPin.open(env, p.pid, start, uid=UID, boot_id=BOOT_ID)
        self.assertEqual(pin.status, ENDED)
        self.assertEqual(pin.send(signal.SIGTERM), ENDED)
        self.assertEqual(env.signal_events, [])
        pin.close()

    def test_target_dying_after_pin_is_reported_ended_not_signalled(self):
        p, start = self.target()
        env = RecordingEnv()
        pin = self.open_pin(p, start, env)
        self.assertEqual(pin.status, PINNED)
        kill_exact(p.pid, start)
        p.wait(timeout=5)
        self.assertEqual(pin.send(signal.SIGTERM), ENDED)
        self.assertEqual(env.signal_events, [], "no signal may leave for an already ended target")
        self.assertEqual(pin.verify(), ENDED)

    def test_wrong_uid_is_different_uid_and_never_signals(self):
        p, start = self.target()
        pin = self.open_pin(p, start, uid=UID + 1)
        self.assertEqual(pin.status, DIFFERENT_UID)
        self.assertEqual(pin.send(signal.SIGKILL), DIFFERENT_UID)
        self.assert_untouched(p)

    @unittest.skipIf(UID == 0, "needs a non-root caller to have a foreign-uid process")
    def test_real_foreign_uid_process_pid1_is_refused_without_signal(self):
        pid1 = proc(1)
        owner = os.stat("/proc/1").st_uid
        self.assertNotEqual(owner, UID)
        env = RecordingEnv()
        pin = ProcessPin.open(env, 1, pid1["start"], uid=UID, boot_id=BOOT_ID)
        try:
            self.assertEqual(pin.status, DIFFERENT_UID)
            self.assertEqual(pin.send(0), DIFFERENT_UID)
            self.assertEqual(env.signal_events, [])
        finally:
            pin.close()
        self.assertEqual(sorted(env.opened), sorted(env.closed))

    def test_changed_or_missing_boot_id_is_stale(self):
        p, start = self.target()
        for boot in ("00000000-0000-0000-0000-000000000000", BOOT_ID[:-1] + "0" if BOOT_ID[-1] != "0" else BOOT_ID[:-1] + "1", ""):
            with self.subTest(boot=boot):
                pin = self.open_pin(p, start, boot_id=boot)
                self.assertEqual(pin.status, STALE)
                self.assertEqual(pin.send(signal.SIGKILL), STALE)
                self.assert_untouched(p)
        unreadable = RecordingEnv(boot="")
        pin = ProcessPin.open(unreadable, p.pid, start, uid=UID, boot_id=BOOT_ID)
        self.assertNotEqual(pin.status, PINNED, "an unreadable current boot_id cannot confirm identity")
        self.assertNotEqual(pin.send(signal.SIGKILL), SENT)
        self.assert_untouched(p)

    def test_stopped_process_still_pins_and_can_be_signalled(self):
        p, start = self.target()
        fd = os.pidfd_open(p.pid)
        try:
            signal.pidfd_send_signal(fd, signal.SIGSTOP)
        finally:
            os.close(fd)
        wait_until(lambda: proc(p.pid)["state"] == "T", what="stopped")
        pin = self.open_pin(p, start)
        self.assertEqual(pin.status, PINNED, "SIGSTOP'd members are still live members (SPEC invariant 5)")
        self.assertEqual(pin.send(signal.SIGKILL), SENT)
        self.assertEqual(p.wait(timeout=5), -signal.SIGKILL)

    def test_closed_pin_cannot_send_and_close_is_idempotent(self):
        p, start = self.target()
        pin = ProcessPin.open(LinuxStopEnvironment(), p.pid, start, uid=UID, boot_id=BOOT_ID)
        self.assertEqual(pin.status, PINNED)
        pin.close()
        pin.close()
        self.assertEqual(pin.send(signal.SIGKILL), CLOSED)
        self.assertEqual(pin.send(signal.SIGTERM), CLOSED)
        self.assertNotEqual(pin.verify(), PINNED)
        self.assert_untouched(p)
        self.assertEqual(open_fds(), self.fds_before, "pidfd leaked after close")

    def test_refused_and_closed_pins_leak_no_file_descriptors(self):
        p, start = self.target()
        dead, dead_start = self.spawned.sleeper(0.01)
        dead.wait(timeout=5)
        before = open_fds()
        for _ in range(40):
            for pid, ticks, uid, boot in ((p.pid, start + 1, UID, BOOT_ID), (p.pid, start, UID + 1, BOOT_ID),
                                          (dead.pid, dead_start, UID, BOOT_ID), (p.pid, start, UID, "x")):
                pin = ProcessPin.open(LinuxStopEnvironment(), pid, ticks, uid=uid, boot_id=boot)
                pin.send(0)
                pin.close()
            ok = ProcessPin.open(LinuxStopEnvironment(), p.pid, start, uid=UID, boot_id=BOOT_ID)
            ok.close()
        self.assertEqual(open_fds(), before)

    def test_full_flow_never_uses_kill_or_killpg(self):
        p, start = self.target()
        with mock.patch("os.kill", side_effect=AssertionError("os.kill is forbidden (PID reuse unsafe)")), \
                mock.patch("os.killpg", side_effect=AssertionError("os.killpg is forbidden (PID reuse unsafe)")), \
                mock.patch("signal.pthread_kill", side_effect=AssertionError("forbidden")):
            pin = ProcessPin.open(LinuxStopEnvironment(), p.pid, start, uid=UID, boot_id=BOOT_ID)
            self.assertEqual(pin.send(0), SENT)
            scan_tracked({"parent": {"pid": 1, "start_ticks": 1}, "session": 1, "uid": UID})
            pin.close()
        self.assert_untouched(p)

    def test_pin_stays_valid_when_the_target_execs(self):
        # exec keeps pid and start ticks: argv/comm changes must neither break nor be needed for the pin.
        marker = tempfile.NamedTemporaryFile(delete=False)
        marker.close()
        self.addCleanup(os.unlink, marker.name)
        p = subprocess.Popen(["/bin/sh", "-c", f"echo go > {marker.name}; exec sleep 300"], start_new_session=True)
        start = proc(p.pid)["start"]
        self.spawned.procs.append((p, start))
        wait_until(lambda: Path(marker.name).read_text().strip() == "go", what="marker")
        pin = self.open_pin(p, start)
        self.assertEqual(pin.status, PINNED)
        self.assertEqual(pin.send(signal.SIGTERM), SENT)
        self.assertEqual(p.wait(timeout=5), -signal.SIGTERM)


class PinProcFormatTests(PinCase):
    def test_comm_containing_parenthesis_and_spaces_does_not_shift_the_fields(self):
        code = "import ctypes,time\nctypes.CDLL(None).prctl(15, b'a) S 1 2 3 4 5', 0, 0, 0)\ntime.sleep(300)"
        p = subprocess.Popen([sys.executable, "-I", "-c", code], start_new_session=True)
        self.spawned.procs.append((p, proc(p.pid)["start"]))
        wait_until(lambda: Path(f"/proc/{p.pid}/comm").read_text().startswith("a)"), what="comm change")
        expected = proc(p.pid)  # parsed by this file after the last ") "
        read = LinuxStopEnvironment().read_stat(p.pid)
        self.assertEqual((read.state, read.ppid, read.pgrp, read.session, read.start_ticks, read.uid),
                         (expected["state"], expected["ppid"], expected["pgrp"], expected["session"], expected["start"], UID))
        pin = self.open_pin(p, expected["start"])
        self.assertEqual(pin.status, PINNED)
        self.assertEqual(pin.send(0), SENT)

    def test_a_non_leader_thread_id_is_never_pinned(self):
        code = "import threading,time\nthreading.Thread(target=time.sleep, args=(300,), daemon=True).start()\ntime.sleep(300)"
        p = subprocess.Popen([sys.executable, "-I", "-c", code], start_new_session=True)
        self.spawned.procs.append((p, proc(p.pid)["start"]))
        wait_until(lambda: len(os.listdir(f"/proc/{p.pid}/task")) >= 2, what="thread")
        for tid in (int(t) for t in os.listdir(f"/proc/{p.pid}/task") if int(t) != p.pid):
            pin = self.open_pin(p, proc(tid)["start"])  # wrong pid on purpose first: leader pid, thread's ticks
            self.assertEqual(pin.status, STALE)
            tpin = ProcessPin.open(LinuxStopEnvironment(), tid, proc(tid)["start"], uid=UID, boot_id=BOOT_ID)
            try:
                self.assertNotEqual(tpin.status, PINNED, "a thread id must not become a signal target")
                self.assertNotEqual(tpin.send(signal.SIGKILL), SENT)
            finally:
                tpin.close()
        self.assert_untouched(p)

    def test_invalid_signal_numbers_are_reported_not_raised_and_do_not_kill(self):
        p, start = self.target()
        pin = self.open_pin(p, start)
        for bad in (-1, 65, 9999):
            outcome = pin.send(bad)
            self.assertNotEqual(outcome, SENT)
            self.assertTrue(outcome.startswith("signal_failed"), outcome)
        self.assert_untouched(p)
        self.assertEqual(pin.status, PINNED)


# --------------------------------------------------------------------------- S7 error injection
class PinInjectionTests(PinCase):
    def read_count_before_signal(self, p, start):
        """Reads send() performs before it signals, measured on an untouched run (signal 0)."""
        env = RecordingEnv()
        pin = ProcessPin.open(env, p.pid, start, uid=UID, boot_id=BOOT_ID)
        env.events.clear()
        self.assertEqual(pin.send(0), SENT)
        signal_at = env.signal_events[0]
        reads = [i for i, e in enumerate(env.events[:signal_at]) if e[0] == "read"]
        pin.close()
        return len(reads)

    def test_open_reads_proc_before_and_after_pidfd_open(self):
        p, start = self.target()
        env = RecordingEnv()
        pin = self.open_pin(p, start, env)
        kinds = [e[0] for e in env.events]
        self.assertEqual(pin.status, PINNED)
        first_open = kinds.index("pidfd_open")
        self.assertIn("read", kinds[:first_open], "/proc must be read before the pidfd is opened")
        self.assertIn("read", kinds[first_open + 1:], "/proc must be re-read after the pidfd is opened")

    def test_send_rereads_proc_inside_send_before_the_signal(self):
        p, start = self.target()
        env = RecordingEnv()
        pin = self.open_pin(p, start, env)
        env.events.clear()
        self.assertEqual(pin.send(0), SENT)
        signal_at = env.signal_events[0]
        self.assertIn("read", [e[0] for e in env.events[:signal_at]],
                      "ProcessPin.send must re-read /proc itself before signalling (CS-AC-08)")

    def test_identity_change_just_before_the_signal_blocks_it(self):
        # Ticket behavior 3: the start ticks change right after verify; send must not signal and
        # reports identity_changed. The real process lives on.
        p, start = self.target()
        n = self.read_count_before_signal(p, start)
        self.assertGreaterEqual(n, 1)
        env = RecordingEnv(armed=False,
                           mutate_read=lambda k, s: replace(s, start_ticks=s.start_ticks + 1) if k == n and s else s)
        pin = self.open_pin(p, start, env)
        env.arm()
        outcome = pin.send(signal.SIGKILL)
        self.assertEqual(env.signal_events, [], "a signal left although identity changed before it")
        if n >= 2:
            self.assertEqual(outcome, IDENTITY_CHANGED)
        else:
            self.assertIn(outcome, {STALE, IDENTITY_CHANGED})
        self.assert_untouched(p)
        self.assertTrue(alive(p.pid, start))

    def test_identity_change_at_any_read_never_precedes_a_signal(self):
        p, start = self.target()
        n = self.read_count_before_signal(p, start)
        for k in range(1, n + 1):
            for label, mutate in (
                ("start_ticks", lambda s: replace(s, start_ticks=s.start_ticks + 7)),
                ("uid", lambda s: replace(s, uid=s.uid + 1)),
                ("zombie", lambda s: replace(s, state="Z")),
                ("gone", lambda s: None),
            ):
                with self.subTest(read=k, change=label):
                    env = RecordingEnv(armed=False, mutate_read=lambda i, s, k=k, mutate=mutate: mutate(s) if i == k and s else s)
                    pin = ProcessPin.open(env, p.pid, start, uid=UID, boot_id=BOOT_ID)
                    env.arm()
                    env.events.clear()
                    env.mutated_event_indexes.clear()
                    outcome = pin.send(signal.SIGKILL)
                    pin.close()
                    self.assertEqual(env.signal_events, [], f"signal sent after {label} changed at read {k}: {outcome}")
                    self.assertNotEqual(outcome, SENT)
                    self.assert_untouched(p)

    def test_pidfd_reports_exit_between_verify_and_signal(self):
        p, start = self.target()
        state = {"armed": False}
        env = RecordingEnv(exited_after=lambda: state["armed"])
        pin = self.open_pin(p, start, env)
        state["armed"] = True
        outcome = pin.send(signal.SIGKILL)
        self.assertEqual(outcome, ENDED)
        self.assertEqual(env.signal_events, [])
        self.assert_untouched(p)

    def test_open_race_pid_replaced_between_first_read_and_pidfd_open(self):
        p, start = self.target()
        for second in (lambda s: replace(s, start_ticks=s.start_ticks + 1), lambda s: None,
                       lambda s: replace(s, state="Z"), lambda s: replace(s, uid=s.uid + 1)):
            env = RecordingEnv(mutate_read=lambda k, s, second=second: second(s) if k == 2 and s else s)
            pin = ProcessPin.open(env, p.pid, start, uid=UID, boot_id=BOOT_ID)
            try:
                self.assertNotEqual(pin.status, PINNED)
                self.assertNotEqual(pin.send(signal.SIGKILL), SENT)
                self.assertEqual(env.signal_events, [])
            finally:
                pin.close()
            self.assertEqual(sorted(env.opened), sorted(env.closed), "pidfd leaked on a failed pin")
            self.assert_untouched(p)

    def test_pidfd_open_failures_are_reported_without_raising(self):
        p, start = self.target()
        for error in (ProcessLookupError(errno.ESRCH, "gone"), PermissionError(errno.EPERM, "denied"),
                      OSError(errno.EMFILE, "too many")):
            with self.subTest(error=type(error).__name__):
                env = RecordingEnv(open_error=error)
                pin = ProcessPin.open(env, p.pid, start, uid=UID, boot_id=BOOT_ID)
                self.assertNotEqual(pin.status, PINNED)
                self.assertNotEqual(pin.send(signal.SIGKILL), SENT)
                self.assertEqual(env.signal_events, [])
                pin.close()
        self.assert_untouched(p)

    def test_eperm_esrch_and_other_signal_errors_are_reported_per_pin(self):
        p, start = self.target()
        env = RecordingEnv(send_error=PermissionError(errno.EPERM, "denied"))
        pin = self.open_pin(p, start, env)
        self.assertEqual(pin.send(signal.SIGTERM), PERMISSION_DENIED)
        self.assert_untouched(p)
        env2 = RecordingEnv(send_error=ProcessLookupError(errno.ESRCH, "gone"))
        pin2 = self.open_pin(p, start, env2)
        self.assertEqual(pin2.send(signal.SIGTERM), ENDED)
        env3 = RecordingEnv(send_error=OSError(errno.EINVAL, "bad signal"))
        pin3 = self.open_pin(p, start, env3)
        outcome = pin3.send(signal.SIGTERM)
        self.assertNotEqual(outcome, SENT)
        self.assertTrue(outcome.startswith("signal_failed"), outcome)
        self.assert_untouched(p)


# --------------------------------------------------------------------------- scan: synthetic /proc shapes
def S(pid, ppid, pgrp, session, *, state="S", uid=1000, start=None):
    return ProcStat(pid, state, ppid, pgrp, session, start if start is not None else pid * 10, uid)


HOST, SUP, CHILD = 100, 200, 300
BACKEND = os.getpid()


def ident(**over):
    base = {"parent": {"pid": HOST, "start_ticks": HOST * 10}, "supervisor": {"pid": SUP, "start_ticks": SUP * 10},
            "child": {"pid": CHILD, "start_ticks": CHILD * 10}, "child_group": CHILD, "session": HOST,
            "uid": 1000, "boot_id": BOOT_ID, "phase": "running", "request_id": "r"}
    base.update(over)
    return base


def world(*extra):
    return [S(HOST, 1, HOST, HOST), S(SUP, HOST, SUP, HOST), S(CHILD, SUP, CHILD, HOST), *extra]


def pids(stats):
    return {s.pid for s in stats}


class ScanSyntheticTests(unittest.TestCase):
    def test_classification_of_every_shape_in_one_tree(self):
        extra = [
            S(301, CHILD, CHILD, HOST),                 # plain child, same group
            S(302, 301, 302, HOST),                     # job-control grandchild: own pgrp, same session
            S(303, CHILD, 303, 303),                    # setsid child
            S(304, 303, 303, 303),                      # its child (still other session)
            S(305, CHILD, CHILD, HOST, state="Z"),      # zombie
            S(306, CHILD, CHILD, HOST, uid=0),          # other uid
            S(307, 1, CHILD, HOST),                     # orphan, reparented away, still in the command group
            S(308, HOST, 308, HOST),                    # host shell's own background job (not a supervisor descendant)
            S(309, 1, CHILD, 999),                      # pgrp number matches but another session
            S(310, 301, 302, HOST, state="T"),          # stopped but live
            S(311, 301, 311, HOST, state="X"),          # dead
        ]
        scan = scan_tracked(ident(), FakeProcEnv(world(*extra)))
        self.assertIsInstance(scan, TrackScan)
        self.assertEqual(pids(scan.tracked), {CHILD, 301, 302, 307, 310})
        self.assertEqual({s.pid for s in scan.left_session if s.state not in {"Z", "X"}}, {303, 304})
        listed = pids(scan.tracked) | pids(scan.left_session)
        for outsider in (HOST, SUP, BACKEND, 306, 308, 309, 311):
            self.assertNotIn(outsider, listed)
        self.assertNotIn(305, pids(scan.tracked))  # a zombie is never a signal target

    def test_scan_reports_the_procstat_it_read(self):
        extra = [S(301, CHILD, CHILD, HOST, start=4242), S(303, CHILD, 303, 303, start=777)]
        scan = scan_tracked(ident(), FakeProcEnv(world(*extra)))
        by_pid = {s.pid: s for s in scan.tracked}
        self.assertEqual((by_pid[301].start_ticks, by_pid[301].ppid, by_pid[301].pgrp, by_pid[301].session, by_pid[301].uid),
                         (4242, CHILD, CHILD, HOST, 1000))
        self.assertEqual({s.pid: s.start_ticks for s in scan.left_session}, {303: 777})

    def test_never_listed_pids_supervisor_parent_backend_and_forbidden(self):
        omp_pane, other = 410, 411
        extra = [
            S(BACKEND, CHILD, CHILD, HOST),     # the backend itself, even if it looks like a member
            S(omp_pane, CHILD, CHILD, HOST),    # caller-supplied forbidden pid
            S(other, CHILD, CHILD, HOST),
            S(412, CHILD, 412, 412),            # setsid descendant that is forbidden: not even reported
        ]
        scan = scan_tracked(ident(), FakeProcEnv(world(*extra)), forbidden=frozenset({omp_pane, 412}))
        listed = pids(scan.tracked) | pids(scan.left_session)
        self.assertEqual(pids(scan.tracked), {CHILD, other})
        for banned in (HOST, SUP, BACKEND, omp_pane, 412):
            self.assertNotIn(banned, listed)

    def test_parent_and_supervisor_are_excluded_even_when_they_look_like_members(self):
        # Make the host shell and supervisor satisfy the group and session rules on purpose.
        stats = [S(HOST, 1, CHILD, HOST), S(SUP, HOST, CHILD, HOST), S(CHILD, SUP, CHILD, HOST)]
        scan = scan_tracked(ident(), FakeProcEnv(stats))
        self.assertEqual(pids(scan.tracked), {CHILD})
        self.assertNotIn(HOST, pids(scan.left_session))
        self.assertNotIn(SUP, pids(scan.left_session))

    def test_supervisor_gone_still_tracks_group_members_and_reports_no_left_session(self):
        extra = [S(301, 1, CHILD, HOST), S(302, 1, 302, HOST), S(303, 1, 303, 303), S(308, HOST, 308, HOST)]
        stats = [S(HOST, 1, HOST, HOST), S(CHILD, 1, CHILD, HOST), *extra]  # no supervisor entry
        scan = scan_tracked(ident(), FakeProcEnv(stats))
        self.assertEqual(pids(scan.tracked), {CHILD, 301})
        self.assertEqual(pids(scan.left_session), set())

    def test_supervisor_pid_reused_by_another_process_is_not_an_anchor(self):
        # Same pid, other start ticks: descendants must not be derived from it.
        extra = [S(301, SUP, 301, HOST), S(303, SUP, 303, 303), S(304, CHILD, CHILD, HOST)]
        stats = [S(HOST, 1, HOST, HOST), S(SUP, HOST, SUP, HOST, start=SUP * 10 + 1), S(CHILD, SUP, CHILD, HOST), *extra]
        scan = scan_tracked(ident(), FakeProcEnv(stats))
        self.assertNotIn(301, pids(scan.tracked), "a descendant of a reused supervisor pid must not be signal-eligible")
        self.assertNotIn(303, pids(scan.left_session))
        self.assertNotIn(SUP, pids(scan.tracked))
        self.assertIn(CHILD, pids(scan.tracked))  # in the command group of the right session

    def test_incomplete_identity_tracks_nothing_unsafe(self):
        env = FakeProcEnv(world(S(301, CHILD, CHILD, HOST)))
        for bad in ({}, ident(parent=None), ident(session=None), ident(uid=None), ident(parent={"pid": HOST})):
            with self.subTest(identity=bad):
                scan = scan_tracked(bad, env)
                self.assertEqual((scan.tracked, scan.left_session), ((), ()))
        no_group = scan_tracked(ident(child_group=None), env)
        self.assertEqual(pids(no_group.tracked), {CHILD, 301}, "descendants in the session are still tracked without a group")
        self.assertNotIn(HOST, pids(no_group.tracked))
        no_supervisor = scan_tracked(ident(supervisor=None), env)
        self.assertEqual(pids(no_supervisor.tracked), {CHILD, 301}, "the command group alone still tracks")
        self.assertEqual(no_supervisor.left_session, ())

    def test_deep_and_wide_trees_and_pid_order_do_not_matter(self):
        chain = [S(500 + i, 500 + i - 1 if i else CHILD, CHILD if i % 2 else 500 + i, HOST) for i in range(60)]
        env = FakeProcEnv(world(*chain))
        env.table = dict(reversed(list(env.table.items())))
        scan = scan_tracked(ident(), env)
        self.assertEqual(pids(scan.tracked), {CHILD} | {500 + i for i in range(60)})

    def test_scan_is_read_only(self):
        # FakeProcEnv raises on pidfd_open / pidfd_send_signal.
        scan_tracked(ident(), FakeProcEnv(world(S(301, CHILD, CHILD, HOST))))


# --------------------------------------------------------------------------- scan: real processes
class RealTreeCase(unittest.TestCase):
    def before_handoff(self):
        """Hook: type into the interactive host shell before wb-handoff."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cs01-ind-scan-")
        self.addCleanup(self.directory.cleanup)
        self.owned = {}
        self.spawned = Spawned(self)
        self.addCleanup(self.spawned.cleanup)
        self.sentinel, self.sentinel_start = self.spawned.sleeper()
        self.addCleanup(self.assert_sentinel_survived)
        self.shell = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "HOME": self.directory.name}, choice=BASH)
        self.parent_pid = self.shell.parent_pid
        self.addCleanup(self.shell.close)
        self.addCleanup(self.kill_tree)
        self.before_handoff()
        self.shell.send_user(b"wb-handoff\n")
        until(self.shell, lambda s: s["parent_mode"] == "control_wait")
        self.shell.claim_manager()

    def assert_sentinel_survived(self):
        self.assertTrue(self.sentinel.poll() is None and alive(self.sentinel.pid, self.sentinel_start),
                        "sentinel outside the test-spawned tree was signalled")

    def kill_tree(self):
        victims = {}
        for pid in descendants_of(self.parent_pid) | set(self.owned):
            try:
                victims[pid] = self.owned.get(pid) or proc(pid)["start"]
            except (OSError, IndexError):
                pass
        for pid, start in victims.items():
            kill_exact(pid, start)
        wait_until(lambda: not any(alive(p, s) for p, s in victims.items()), 5, "tree to die")

    def own(self, pid):
        self.owned[pid] = proc(pid)["start"]
        return pid

    def file(self, name):
        return Path(self.directory.name) / name

    def start(self, command):
        control, automation = ports(self.shell)
        self.shell.submit(control, command, automation)
        until(self.shell, lambda s: s["lifecycle"]["experiment_started"])
        return control["payload"]["requestId"]

    def identity(self):
        identity = self.shell.run_identity()
        self.assertIsNotNone(identity)
        return identity

    def scan(self, **kw):
        return scan_tracked(self.identity(), **kw)

    def pump(self, seconds=0.2):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.shell.poll(.02)
            self.shell.display_bytes()


class ScanRealProcessTests(RealTreeCase):
    def test_ticket_behavior_4_tracked_and_left_session(self):
        f = {n: self.file(n) for n in ("bash", "sleep60", "setsid61")}
        self.start(f"echo $$ > {f['bash']}; sleep 60 & echo $! > {f['sleep60']}; setsid sleep 61 & echo $! > {f['setsid61']}; wait")
        bash, sleep60, setsid61 = (self.own(read_pid(f[n])) for n in ("bash", "sleep60", "setsid61"))
        identity = self.identity()
        # Independent expectations from /proc.
        self.assertEqual(proc(bash)["ppid"], identity["supervisor"]["pid"])
        self.assertEqual(proc(sleep60)["ppid"], bash)
        self.assertEqual((proc(sleep60)["session"], proc(sleep60)["pgrp"]), (self.parent_pid, bash))
        self.assertEqual(proc(setsid61)["session"], setsid61, "premise: setsid61 leads its own session")
        scan = scan_tracked(identity)
        self.assertEqual(pids(scan.tracked), {bash, sleep60})
        self.assertEqual(pids(scan.left_session), {setsid61})
        listed = pids(scan.tracked) | pids(scan.left_session)
        for banned in (self.parent_pid, identity["supervisor"]["pid"], os.getpid(), self.sentinel.pid):
            self.assertNotIn(banned, listed)
        for entry in scan.tracked + scan.left_session:
            info = proc(entry.pid)
            self.assertEqual((entry.ppid, entry.pgrp, entry.session, entry.start_ticks, entry.uid),
                             (info["ppid"], info["pgrp"], info["session"], info["start"], UID))

    def test_nested_children_and_job_control_group_are_tracked(self):
        f = {n: self.file(n) for n in ("outer", "mid", "leaf", "job")}
        self.start(
            f"echo $$ > {f['outer']}; "
            f"bash -c 'echo $$ > {f['mid']}; sleep 62 & echo $! > {f['leaf']}; wait' & "
            f"set -m; sleep 63 & echo $! > {f['job']}; set +m; wait")
        outer, mid, leaf, job = (self.own(read_pid(f[n])) for n in ("outer", "mid", "leaf", "job"))
        self.assertEqual(proc(leaf)["ppid"], mid)
        self.assertEqual(proc(mid)["ppid"], outer)
        self.assertNotEqual(proc(job)["pgrp"], proc(outer)["pgrp"], "premise: job control gave the job its own group")
        self.assertEqual(proc(job)["session"], self.parent_pid)
        scan = self.scan()
        self.assertEqual(pids(scan.tracked), {outer, mid, leaf, job})
        self.assertEqual(scan.left_session, ())

    def test_double_forked_orphan_reparented_to_the_supervisor_is_tracked(self):
        f0, f1 = self.file("main"), self.file("orphan")
        self.start(f"echo $$ > {f0}; (sleep 64 & echo $! > {f1}); sleep 65; :")
        main, orphan = self.own(read_pid(f0)), self.own(read_pid(f1))
        supervisor = self.identity()["supervisor"]["pid"]
        wait_until(lambda: proc(orphan)["ppid"] == supervisor, what="reparent to the subreaper supervisor")
        scan = self.scan()
        self.assertIn(orphan, pids(scan.tracked))
        self.assertIn(main, pids(scan.tracked))

    def test_child_forked_by_a_non_main_thread_is_tracked(self):
        f0, f1 = self.file("py"), self.file("threaded")
        code = (
            "import os,subprocess,threading,time\n"
            f"open({str(f0)!r},'w').write(str(os.getpid()))\n"
            "def work():\n"
            f"    p=subprocess.Popen(['/usr/bin/sleep','66']); open({str(f1)!r},'w').write(str(p.pid)); time.sleep(300)\n"
            "threading.Thread(target=work,daemon=True).start()\n"
            "time.sleep(300)\n")
        script = self.file("t.py")
        script.write_text(code)
        self.start(f"{shlex.quote(sys.executable)} -I {script}")
        py, threaded = self.own(read_pid(f0)), self.own(read_pid(f1))
        try:
            main_children = Path(f"/proc/{py}/task/{py}/children").read_text().split()
        except OSError:
            main_children = []
        self.assertNotIn(str(threaded), main_children, "premise: the main thread's children list misses it")
        self.assertEqual(proc(threaded)["ppid"], py)
        scan = self.scan()
        self.assertEqual(pids(scan.tracked), {py, threaded})

    def test_zombie_members_are_excluded(self):
        f0, f1 = self.file("py"), self.file("zombie")
        code = (
            "import os,time\n"
            f"open({str(f0)!r},'w').write(str(os.getpid()))\n"
            "pid=os.fork()\n"
            "if pid==0: os._exit(0)\n"
            f"open({str(f1)!r},'w').write(str(pid))\n"
            "time.sleep(300)\n")
        script = self.file("z.py")
        script.write_text(code)
        self.start(f"{shlex.quote(sys.executable)} -I {script}")
        py, zombie = self.own(read_pid(f0)), read_pid(f1)
        wait_until(lambda: proc(zombie)["state"] == "Z", what="zombie")
        scan = self.scan()
        self.assertEqual(pids(scan.tracked), {py})
        self.assertNotIn(zombie, pids(scan.tracked))

    def test_exited_members_disappear_from_the_next_scan(self):
        f = {n: self.file(n) for n in ("bash", "a", "b")}
        self.start(f"echo $$ > {f['bash']}; sleep 67 & echo $! > {f['a']}; sleep 68 & echo $! > {f['b']}; wait")
        bash, a, b = (self.own(read_pid(f[n])) for n in ("bash", "a", "b"))
        self.assertEqual(pids(self.scan().tracked), {bash, a, b})
        kill_exact(a, self.owned[a])
        wait_until(lambda: not exists(a), what="reaped member")
        self.assertEqual(pids(self.scan().tracked), {bash, b})

    def test_host_shell_background_job_in_same_session_is_neither_tracked_nor_left_session(self):
        f0 = self.file("cmd")
        self.start(f"echo $$ > {f0}; sleep 69; :")
        cmd = self.own(read_pid(f0))
        job = 4_000_001  # beyond pid_max-in-use: a synthetic user job of the host shell
        overlay = OverlayEnv([ProcStat(job, "S", self.parent_pid, job, self.parent_pid, 1, UID)])
        scan = scan_tracked(self.identity(), overlay)
        self.assertEqual(pids(scan.tracked), {cmd} | descendants_of(cmd))
        self.assertNotIn(job, pids(scan.tracked) | pids(scan.left_session))

    def test_forbidden_pids_are_excluded_from_a_real_scan(self):
        f = {n: self.file(n) for n in ("bash", "a", "b")}
        self.start(f"echo $$ > {f['bash']}; sleep 70 & echo $! > {f['a']}; setsid sleep 71 & echo $! > {f['b']}; wait")
        bash, a, b = (self.own(read_pid(f[n])) for n in ("bash", "a", "b"))
        scan = self.scan(forbidden=frozenset({a, b}))
        self.assertEqual(pids(scan.tracked), {bash})
        self.assertEqual(pids(scan.left_session), set())

    def test_supervisor_gone_group_members_remain_tracked_and_detached_are_not_listed(self):
        f = {n: self.file(n) for n in ("bash", "a", "b")}
        self.start(f"echo $$ > {f['bash']}; sleep 72 & echo $! > {f['a']}; setsid sleep 73 & echo $! > {f['b']}; wait")
        bash, a, b = (self.own(read_pid(f[n])) for n in ("bash", "a", "b"))
        identity = self.identity()
        sup = identity["supervisor"]
        kill_exact(sup["pid"], sup["start_ticks"])  # the test's own supervisor
        wait_until(lambda: not alive(sup["pid"]), what="supervisor to die")
        wait_until(lambda: proc(bash)["ppid"] != sup["pid"], what="reparent")
        scan = scan_tracked(identity)
        self.assertEqual(pids(scan.tracked), {bash, a}, "same group and session survive the supervisor")
        self.assertNotIn(b, pids(scan.tracked))
        self.assertNotIn(sup["pid"], pids(scan.tracked) | pids(scan.left_session))

    def test_scan_results_feed_pins_and_a_signal_reaches_only_its_target(self):
        f = {n: self.file(n) for n in ("bash", "a", "b")}
        self.start(f"echo $$ > {f['bash']}; sleep 74 & echo $! > {f['a']}; sleep 75 & echo $! > {f['b']}; wait")
        bash, a, b = (self.own(read_pid(f[n])) for n in ("bash", "a", "b"))
        identity = self.identity()
        scan = scan_tracked(identity)
        self.assertEqual(pids(scan.tracked), {bash, a, b})
        env = LinuxStopEnvironment()
        pins = {}
        for pid, start in ((p, self.owned[p]) for p in (a, b, bash)):
            pins[pid] = ProcessPin.open(env, pid, start, uid=identity["uid"], boot_id=identity["boot_id"])
            self.addCleanup(pins[pid].close)
            self.assertEqual(pins[pid].status, PINNED)
        self.assertEqual(pins[a].send(signal.SIGKILL), SENT)
        wait_until(lambda: not alive(a), what="a to die")
        self.assertTrue(alive(b, self.owned[b]), "signal escaped to the sibling in the same group")
        self.assertTrue(alive(bash, self.owned[bash]), "signal escaped to the parent of the target")
        self.assertEqual(pins[a].send(signal.SIGKILL), ENDED)
        # The dead member leaves the next scan; the live ones remain.
        wait_until(lambda: a not in pids(scan_tracked(identity).tracked), what="a to leave the scan")
        self.assertEqual(pids(scan_tracked(identity).tracked), {bash, b})


if __name__ == "__main__":
    unittest.main()
