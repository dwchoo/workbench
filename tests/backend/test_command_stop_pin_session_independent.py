"""C-T1a independent checks: a pin proves the session the scan recorded, with real processes.

Contract (SPEC command-stop invariant 2, C-D45): command-stop signals only processes whose identity is verified at
pin time. A target whose session changed between the scan and the pin / verify / send steps is ``stale`` and gets
NO signal; same-session targets keep today's behaviour; a uid mismatch stays ``different_uid``; ``pidfd_unavailable``
is unchanged; ``ProcessPin.open`` callers that pass no session are unchanged.

Seams: ``ProcessPin.open`` / ``send`` and ``CommandStopService.stop`` over ``LinuxStopEnvironment`` subclasses that
run a real child's ``setsid`` at an exact ``/proc`` read or pidfd step (pipe/file handshake), plus a synthetic table
for the uid / pidfd_unavailable guards. Every process is spawned by this file, signalled only through pidfds of
those processes, and removed by pid + start ticks.
"""
from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from workbench.backend.command_stop import (CommandStopService, LinuxStopEnvironment, ProcessPin, ProcStat,
                                            StopPolicy, StopTarget)

BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
UID = os.getuid()
SHORT = StopPolicy(identity_wait=0.3, sigint_wait=0.2, sigterm_wait=0.2, sigkill_wait=0.2, call_wait=6.0,
                   poll_interval=0.02)

# A child that records every catchable signal it receives (one line each) into ``signals`` and, once the file
# ``go`` exists, calls setsid and writes ``done``. It writes its pid to ``ready`` when it is armed.
CHILD = r"""
import os, signal, sys, time
d = sys.argv[1]; name = sys.argv[2]
def note(num, _frame):
    with open(os.path.join(d, name + ".signals"), "a") as f:
        f.write(signal.Signals(num).name + "\n")
for s in (signal.SIGINT, signal.SIGTERM, signal.SIGCONT, signal.SIGUSR1, signal.SIGHUP):
    signal.signal(s, note)
if len(sys.argv) > 3 and sys.argv[3] == "pgrp":
    os.setpgid(0, 0)
open(os.path.join(d, name + ".ready"), "w").write(str(os.getpid()))
go = os.path.join(d, name + ".go")
pg = os.path.join(d, name + ".pg")
while True:
    if os.path.exists(pg) and not os.path.exists(pg + ".done"):
        os.setpgid(0, 0)
        open(pg + ".done", "w").write(str(os.getpgrp()))
    if os.path.exists(go) and not os.path.exists(os.path.join(d, name + ".done")):
        os.setsid()
        open(os.path.join(d, name + ".done"), "w").write(str(os.getsid(0)))
    time.sleep(0.01)
"""

# host (session leader, forked tree): host -> supervisor -> command child [-> grandchild]
TREE = r"""
import os, subprocess, sys, time
d, child_src, with_grandchild = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
open(os.path.join(d, "host.pid"), "w").write(str(os.getpid()))
sup = os.fork()
if sup == 0:
    open(os.path.join(d, "sup.pid"), "w").write(str(os.getpid()))
    procs = [subprocess.Popen([sys.executable, "-I", "-c", child_src, d, "child"])]
    if with_grandchild:
        # the grandchild descends from the supervisor too; it is in its own process group, same session
        procs.append(subprocess.Popen([sys.executable, "-I", "-c", child_src, d, "grand", "pgrp"]))
    for p in procs:
        p.wait()
    os._exit(0)
os.waitpid(sup, 0)
"""


def read_stat(pid):
    return LinuxStopEnvironment().read_stat(pid)


def wait_for(path: Path, timeout=30.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text()
            if text:
                return text
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


class RealProcessCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="wb-pinsess."))
        self.addCleanup(self._rmtree)
        self.owned: list[tuple[int, int]] = []  # (pid, start_ticks): the only processes this test ever kills
        self.popens: list[subprocess.Popen] = []
        self.addCleanup(self._reap)

    def _rmtree(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def own(self, pid: int) -> int:
        stat = read_stat(pid)
        if stat is not None:
            self.owned.append((pid, stat.start_ticks))
        return pid

    def _reap(self):
        for pid, ticks in self.owned:
            stat = read_stat(pid)
            if stat is None or stat.start_ticks != ticks or stat.state in ("Z", "X"):
                continue
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                continue
            try:
                again = read_stat(pid)  # the pidfd names the process we recorded
                if again is not None and again.start_ticks == ticks:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            except OSError:
                pass
            finally:
                os.close(fd)
        for popen in self.popens:
            try:
                popen.wait(timeout=30)
            except subprocess.TimeoutExpired:
                popen.kill()
                popen.wait(timeout=30)
        deadline = time.monotonic() + 30
        for pid, ticks in self.owned:
            while time.monotonic() < deadline:
                stat = read_stat(pid)
                if stat is None or stat.start_ticks != ticks or stat.state in ("Z", "X"):
                    break
                time.sleep(0.01)

    # -- the single-child helpers (child stays in the test runner's session, so setsid is legal) ----------------
    def spawn_child(self, name="child") -> int:
        popen = subprocess.Popen([sys.executable, "-I", "-c", CHILD, str(self.dir), name],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.popens.append(popen)
        self.own(popen.pid)
        wait_for(self.dir / f"{name}.ready")
        return popen.pid

    def setsid_now(self, name="child") -> int:
        (self.dir / f"{name}.go").write_text("1")
        return int(wait_for(self.dir / f"{name}.done"))

    def signals(self, name="child") -> list[str]:
        path = self.dir / f"{name}.signals"
        return path.read_text().split() if path.exists() else []

    def assertAliveUnsignalled(self, pid, name="child"):
        time.sleep(0.15)  # a delivered signal is recorded by the handler within a few ms
        stat = read_stat(pid)
        self.assertIsNotNone(stat)
        self.assertNotIn(stat.state, ("Z", "X"), "the target must still be alive")
        self.assertEqual(self.signals(name), [], "the target received a signal")


class HookedEnvironment(LinuxStopEnvironment):
    """A real ``/proc`` + pidfd environment that runs ``action`` once, exactly before the Nth ``/proc`` read of
    ``pid`` made *after* the first ``boot_id`` call (the first thing ``ProcessPin.open`` does), or before the
    pidfd signal / pidfd open when ``at`` says so."""

    def __init__(self, pid, at, action):
        self.pid, self.at, self.action = pid, at, action
        self.pin_reads, self.in_pin, self.fired, self.signals_sent = 0, False, False, []
        self.events: list[str] = []

    def _fire(self, label):
        if not self.fired:
            self.fired = True
            self.events.append(label)
            self.action()

    def boot_id(self):
        self.in_pin = True
        self.events.append("boot_id")
        if self.at == "boot_id":
            self._fire("fired@boot_id")
        return super().boot_id()

    def read_stat(self, pid):
        if pid == self.pid and self.in_pin:
            self.pin_reads += 1
            self.events.append(f"read#{self.pin_reads}")
            if self.at == self.pin_reads:
                self._fire(f"fired@read#{self.pin_reads}")
        return super().read_stat(pid)

    def pidfd_open(self, pid):
        if pid == self.pid and self.at == "pidfd_open":
            self._fire("fired@pidfd_open")
        return super().pidfd_open(pid)

    def pidfd_send_signal(self, fd, signum):
        self.signals_sent.append(signum)
        super().pidfd_send_signal(fd, signum)


class DirectPinRealProcessTests(RealProcessCase):
    def pin(self, env, pid, session, **kw):
        pin = ProcessPin.open(env, pid, kw.pop("ticks", None) or read_stat(pid).start_ticks,
                              uid=kw.pop("uid", UID), boot_id=BOOT_ID, session=session)
        self.addCleanup(pin.close)
        return pin

    def test_premise_a_real_child_setsid_changes_the_session_the_env_reads(self):
        pid = self.spawn_child()
        before = read_stat(pid).session
        self.assertEqual(before, os.getsid(0))
        self.assertEqual(self.setsid_now(), pid)
        self.assertEqual(read_stat(pid).session, pid)

    def test_setsid_after_the_scan_but_before_the_pin_is_stale_and_unsignalled(self):
        pid = self.spawn_child()
        scanned = read_stat(pid)
        self.setsid_now()  # the child has left the session the scan recorded
        env = HookedEnvironment(pid, None, lambda: None)
        pin = self.pin(env, pid, scanned.session, ticks=scanned.start_ticks)
        self.assertEqual(pin.status, "stale")
        self.assertEqual(pin.send(signal.SIGTERM), "stale")
        self.assertEqual(env.signals_sent, [])
        self.assertAliveUnsignalled(pid)

    def test_setsid_exactly_at_pin_open_start_is_stale_and_unsignalled(self):
        pid = self.spawn_child()
        scanned = read_stat(pid)
        env = HookedEnvironment(pid, "boot_id", self.setsid_now)
        pin = self.pin(env, pid, scanned.session, ticks=scanned.start_ticks)
        self.assertTrue(env.fired)
        self.assertEqual((pin.status, pin.send(signal.SIGKILL)), ("stale", "stale"))
        self.assertEqual(env.signals_sent, [])
        self.assertAliveUnsignalled(pid)

    def test_setsid_between_the_pidfd_open_and_the_confirming_read_is_stale_and_unsignalled(self):
        pid = self.spawn_child()
        scanned = read_stat(pid)
        env = HookedEnvironment(pid, "pidfd_open", self.setsid_now)  # lands after the first read, before pidfd_open
        env_events = env.events
        pin = self.pin(env, pid, scanned.session, ticks=scanned.start_ticks)
        self.assertTrue(env.fired, env_events)
        self.assertEqual(pin.status, "stale", env_events)
        self.assertEqual(pin.send(signal.SIGTERM), "stale")
        self.assertEqual(env.signals_sent, [])
        self.assertAliveUnsignalled(pid)

    def test_setsid_after_the_pin_is_pinned_but_before_send_verifies_is_stale_and_unsignalled(self):
        pid = self.spawn_child()
        scanned = read_stat(pid)
        env = HookedEnvironment(pid, 3, self.setsid_now)  # open reads #1 and #2; send's verify is read #3
        pin = self.pin(env, pid, scanned.session, ticks=scanned.start_ticks)
        self.assertEqual(pin.status, "pinned", "premise: the session was intact while the pin was opened")
        self.assertFalse(env.fired)
        self.assertEqual(pin.send(signal.SIGTERM), "stale")
        self.assertTrue(env.fired, env.events)
        self.assertEqual(env.signals_sent, [])
        self.assertAliveUnsignalled(pid)

    def test_setsid_between_the_verify_and_the_last_recheck_is_not_signalled(self):
        pid = self.spawn_child()
        scanned = read_stat(pid)
        env = HookedEnvironment(pid, 4, self.setsid_now)  # read #4 is send's recheck right before the signal
        pin = self.pin(env, pid, scanned.session, ticks=scanned.start_ticks)
        self.assertEqual(pin.status, "pinned")
        self.assertEqual(pin.send(signal.SIGTERM), "identity_changed")
        self.assertTrue(env.fired, env.events)
        self.assertEqual(env.signals_sent, [])
        self.assertAliveUnsignalled(pid)
        self.assertEqual(pin.status, "stale", "the pin remembers why it refused")
        self.assertEqual(pin.send(signal.SIGKILL), "stale", "and keeps refusing")
        self.assertEqual(env.signals_sent, [])

    def test_a_same_session_child_is_pinned_and_signalled_as_before(self):
        pid = self.spawn_child()
        scanned = read_stat(pid)
        env = HookedEnvironment(pid, None, lambda: None)
        pin = self.pin(env, pid, scanned.session, ticks=scanned.start_ticks)
        self.assertEqual(pin.status, "pinned")
        self.assertEqual(pin.send(signal.SIGUSR1), "sent")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not self.signals():
            time.sleep(0.01)
        self.assertEqual(self.signals(), ["SIGUSR1"])

    def test_a_process_group_change_alone_does_not_stale_the_pin(self):
        pid = self.spawn_child()
        scanned = read_stat(pid)
        def new_group():  # same session, a new process group, applied by the child itself just before verify
            (self.dir / "child.pg").write_text("1")
            wait_for(self.dir / "child.pg.done")
        env = HookedEnvironment(pid, 3, new_group)
        pin = self.pin(env, pid, scanned.session, ticks=scanned.start_ticks)
        self.assertEqual(pin.send(signal.SIGUSR1), "sent")
        self.assertEqual(read_stat(pid).pgrp, pid, "premise: the group did change")
        self.assertEqual(read_stat(pid).session, scanned.session)

    def test_no_session_argument_keeps_the_old_contract_even_after_setsid(self):
        pid = self.spawn_child()
        scanned = read_stat(pid)
        self.setsid_now()
        env = HookedEnvironment(pid, None, lambda: None)
        pin = self.pin(env, pid, None, ticks=scanned.start_ticks)
        self.assertEqual(pin.status, "pinned")
        self.assertEqual(pin.send(signal.SIGUSR1), "sent")

    def test_the_uid_mismatch_stays_different_uid_not_stale_whatever_the_session(self):
        pid = self.spawn_child()
        scanned = read_stat(pid)
        self.setsid_now()
        env = HookedEnvironment(pid, None, lambda: None)
        pin = self.pin(env, pid, scanned.session, ticks=scanned.start_ticks, uid=UID + 1)
        self.assertEqual(pin.status, "different_uid")
        self.assertEqual(pin.send(signal.SIGTERM), "different_uid")
        self.assertEqual(env.signals_sent, [])

    def test_a_reused_start_ticks_mismatch_wins_over_the_session_check_as_stale(self):
        pid = self.spawn_child()
        scanned = read_stat(pid)
        env = HookedEnvironment(pid, None, lambda: None)
        pin = self.pin(env, pid, scanned.session, ticks=scanned.start_ticks + 1)
        self.assertEqual(pin.status, "stale")
        self.assertEqual(env.signals_sent, [])

    def test_an_exited_target_is_ended_not_stale_when_a_session_is_expected(self):
        pid = self.spawn_child()
        scanned = read_stat(pid)
        popen = self.popens[-1]
        popen.kill()
        popen.wait(timeout=30)
        env = HookedEnvironment(pid, None, lambda: None)
        pin = self.pin(env, pid, scanned.session, ticks=scanned.start_ticks)
        self.assertEqual(pin.status, "ended")


class PidfdUnavailableUnchangedTests(unittest.TestCase):
    def test_pidfd_unavailable_is_reported_with_a_session_and_a_changed_session_is_not_masked_by_it(self):
        class Table(LinuxStopEnvironment):
            def __init__(self, session):
                self.stat = ProcStat(7777, "S", 1, 7777, session, 5, UID)
                self.sent = []

            def boot_id(self):
                return BOOT_ID

            def read_stat(self, pid):
                return self.stat

            def pidfd_open(self, pid):
                raise OSError(errno.EMFILE, "too many open files")

            def pidfd_send_signal(self, fd, signum):
                self.sent.append(signum)

        same = ProcessPin.open(Table(40), 7777, 5, uid=UID, boot_id=BOOT_ID, session=40)
        self.assertEqual(same.status, "pidfd_unavailable:EMFILE")
        self.assertEqual(same.send(signal.SIGTERM), "pidfd_unavailable:EMFILE")
        # the session check runs on the first read, before any pidfd is attempted: it is a proven change, not
        # an unprovable identity
        moved_env = Table(41)
        moved = ProcessPin.open(moved_env, 7777, 5, uid=UID, boot_id=BOOT_ID, session=40)
        self.assertEqual(moved.status, "stale")
        self.assertEqual(moved_env.sent, [])


class ServiceStopRealProcessTests(RealProcessCase):
    """The stop engine end to end over a real host -> supervisor -> command-child tree."""

    def start_tree(self, grandchild=False):
        host = subprocess.Popen([sys.executable, "-I", "-c", TREE, str(self.dir), CHILD, "1" if grandchild else "0"],
                                start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.popens.append(host)
        self.own(host.pid)
        sup = int(wait_for(self.dir / "sup.pid"))
        child = int(wait_for(self.dir / "child.ready"))
        pids = {"host": host.pid, "sup": sup, "child": child}
        if grandchild:
            pids["grand"] = int(wait_for(self.dir / "grand.ready"))
        for pid in pids.values():
            self.own(pid)
        self.assertEqual(read_stat(child).session, host.pid, "premise: the child is in the host's session")
        return pids

    def identity(self, command_id, pids):
        return {"request_id": command_id, "phase": "running", "uid": UID, "boot_id": BOOT_ID,
                "session": pids["host"], "child_group": pids["child"],
                "parent": {"pid": pids["host"], "start_ticks": read_stat(pids["host"]).start_ticks},
                "supervisor": {"pid": pids["sup"], "start_ticks": read_stat(pids["sup"]).start_ticks},
                "child": {"pid": pids["child"], "start_ticks": read_stat(pids["child"]).start_ticks}}

    def stop(self, env, pids):
        service = CommandStopService(journal=lambda record: None, policy=SHORT, env=env)
        self.addCleanup(service.close) if hasattr(service, "close") else None
        command_id = str(uuid4())
        identity = self.identity(command_id, pids)
        service.register(StopTarget(command_id, "worker", "terminal", None, identity=lambda: identity,
                                    forbidden=lambda: frozenset()))
        return service.stop("worker", command_id)

    def test_a_member_that_called_setsid_between_the_scan_and_the_pin_gets_no_signal(self):
        pids = self.start_tree()
        env = HookedEnvironment(pids["child"], "boot_id", lambda: self.setsid_now("child"))
        result = self.stop(env, pids)
        self.assertTrue(env.fired, env.events)
        self.assertEqual(env.signals_sent, [], "no signal of any kind left the backend")
        self.assertEqual(result["status"], "stale", result)
        self.assertEqual(result.get("signals"), [])
        self.assertAliveUnsignalled(pids["child"])
        for name in ("host", "sup"):
            self.assertNotIn(read_stat(pids[name]).state, ("Z", "X"))

    def test_a_member_that_called_setsid_after_the_pin_opened_gets_no_signal(self):
        pids = self.start_tree()
        # reads of the child: the scan (1 per scan, outside in_pin), then pin open #1/#2, verify #3, recheck #4
        env = HookedEnvironment(pids["child"], 3, lambda: self.setsid_now("child"))
        result = self.stop(env, pids)
        self.assertTrue(env.fired, env.events)
        self.assertEqual(env.signals_sent, [])
        self.assertIn(result["status"], ("stale", "unknown"), result)
        self.assertAliveUnsignalled(pids["child"])

    def test_the_descendant_that_stayed_in_the_session_is_signalled_while_the_one_that_left_is_not(self):
        pids = self.start_tree(grandchild=True)
        grand = pids["grand"]
        self.assertEqual(read_stat(grand).pgrp, grand, "premise: the grandchild leads its own process group")
        self.assertEqual(read_stat(grand).session, read_stat(pids["child"]).session)
        env = HookedEnvironment(pids["child"], "boot_id", lambda: self.setsid_now("child"))
        result = self.stop(env, pids)
        self.assertTrue(env.fired, env.events)
        self.assertIn(result["status"], ("exited", "unknown"), result)
        self.assertEqual(self.signals("child"), [], "the member that left the session got nothing")
        time.sleep(0.1)
        reached = {int(t["pid"]) for sig in result.get("signals", []) for t in sig["targets"]}
        self.assertEqual(reached, {grand}, result)
        self.assertIn("SIGINT", self.signals("grand"), "the same-session member keeps getting the staged signals")
        child_stat = read_stat(pids["child"])
        self.assertIsNotNone(child_stat)
        self.assertNotIn(child_stat.state, ("Z", "X"))

    def test_the_unchanged_tree_is_stopped_with_the_staged_signals_as_before(self):
        pids = self.start_tree()
        env = HookedEnvironment(pids["child"], None, lambda: None)
        result = self.stop(env, pids)
        self.assertEqual(env.signals_sent[:1], [signal.SIGINT], env.signals_sent)
        self.assertIn(signal.SIGKILL, env.signals_sent, "the child ignores INT/TERM (it only notes them): killed")
        self.assertIn(result["status"], ("exited", "unknown"), result)
        self.assertEqual(self.signals("child")[:2], ["SIGINT", "SIGTERM"])

    def test_a_session_change_before_the_stop_is_reported_left_session_and_never_signalled(self):
        pids = self.start_tree()
        self.setsid_now("child")  # the scan itself sees it: the pre-existing behaviour, unchanged
        env = HookedEnvironment(pids["child"], None, lambda: None)
        result = self.stop(env, pids)
        self.assertEqual(env.signals_sent, [])
        self.assertEqual(result["status"], "unknown", result)
        self.assertAliveUnsignalled(pids["child"])


if __name__ == "__main__":
    unittest.main()
