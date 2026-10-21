"""Independent S6 verification of ``PersistentShell.run_identity()`` / ``HostShellPort.run_identity()`` (CS-01).

Every expected value is read by this test itself from ``/proc/<pid>/stat``, ``os.getuid()`` and
``/proc/sys/kernel/random/boot_id``, or recorded by the command (``$$``, ``$!``) into a file.
Production scan/identity output is never used to compute an expectation.

Safety: only processes this test started are ever signalled, and only through a pidfd whose
start ticks were re-checked by this file's own helpers. A sentinel process in its own session is
started per test and must survive.
"""
from pathlib import Path
import os
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from workbench.backend.command_stop import ProcessPin, LinuxStopEnvironment, PINNED, ENDED, STALE
from workbench.backend.panes import HostShellPort
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell

BASH = ShellChoice("bash", "/usr/bin/bash")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def ports(shell):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "d" * 64, "phase": "accepted"}},
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


def stat_fields(pid):
    """Raw /proc/<pid>/stat fields after the comm: state, ppid, pgrp, session, ..., starttime (index 19)."""
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
    stats = all_stats()
    found, pending = set(), [root]
    while pending:
        parent = pending.pop()
        for pid, info in stats.items():
            if info["ppid"] == parent and pid not in found:
                found.add(pid)
                pending.append(pid)
    return found


def kill_exact(pid, start):
    """SIGKILL ``pid`` only if it still has ``start``: pidfd, re-read, signal. The test's own helper."""
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


class ManagedShellCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cs01-ind-identity-")
        self.addCleanup(self.directory.cleanup)
        self.owned = {}  # pid -> start ticks, every process this test is responsible for
        self.sentinel = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.addCleanup(self.stop_sentinel)
        self.sentinel_start = proc(self.sentinel.pid)["start"]
        self.shell = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "HOME": self.directory.name}, choice=BASH)
        # Registered last so it runs first: kill the tree while the supervisor still anchors it.
        self.addCleanup(self.shell.close)
        self.addCleanup(self.kill_tree)
        self.shell.send_user(b"wb-handoff\n")
        until(self.shell, lambda s: s["parent_mode"] == "control_wait")
        self.shell.claim_manager()
        self.parent_pid = self.shell.parent_pid

    def stop_sentinel(self):
        survived = self.sentinel.poll() is None and alive(self.sentinel.pid, self.sentinel_start)
        self.sentinel.kill()
        self.sentinel.wait()
        self.assertTrue(survived, "sentinel outside the test-spawned tree was signalled")

    def kill_tree(self):
        victims = {}
        for pid in descendants_of(self.parent_pid) | set(self.owned):
            try:
                victims[pid] = proc(pid)["start"] if pid not in self.owned else self.owned[pid]
            except (OSError, IndexError):
                pass
        for pid, start in victims.items():
            kill_exact(pid, start)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(alive(p, s) for p, s in victims.items()):
            time.sleep(.02)
        leaked = [p for p, s in victims.items() if alive(p, s)]
        self.assertEqual(leaked, [], "leaked test-spawned processes")

    def own(self, pid):
        self.owned[pid] = proc(pid)["start"]
        return pid

    def submit(self, command, *, started=True):
        control, automation = ports(self.shell)
        self.shell.submit(control, command, automation)
        if started:
            until(self.shell, lambda s: s["lifecycle"]["experiment_started"])
        return control["payload"]["requestId"]

    def pid_file(self, name):
        return Path(self.directory.name) / name


class RunIdentityValuesTests(ManagedShellCase):
    def test_before_any_request_is_none(self):
        self.assertIsNone(self.shell.run_identity())

    def test_running_identity_matches_proc_for_plain_command(self):
        # Contract: CS-01 behavior 1 / SPEC "신원 기준". Command is a plain bash -c (no exec trick).
        f0 = self.pid_file("self")
        request_id = self.submit(f"echo $$ > {f0}; sleep 30; :")
        child = self.own(read_pid(f0))
        info = proc(child)
        supervisor, group, session = info["ppid"], info["pgrp"], info["session"]
        sup_info, parent_info = proc(supervisor), proc(self.parent_pid)
        identity = self.shell.run_identity()
        self.assertIsNotNone(identity)
        self.assertEqual(identity["request_id"], request_id)
        self.assertEqual(identity["phase"], "running")
        self.assertEqual(identity["parent"], {"pid": self.parent_pid, "start_ticks": parent_info["start"]})
        self.assertEqual(identity["supervisor"], {"pid": supervisor, "start_ticks": sup_info["start"]})
        self.assertEqual(identity["child"], {"pid": child, "start_ticks": info["start"]})
        self.assertEqual(identity["child_group"], group)
        self.assertEqual(group, child, "test premise: child is its own group leader")
        self.assertEqual(identity["session"], session)
        self.assertEqual(session, self.parent_pid, "test premise: session is the host shell pid")
        self.assertEqual(sup_info["ppid"], self.parent_pid, "test premise: supervisor is a child of the host shell")
        self.assertEqual(identity["uid"], os.getuid())
        self.assertEqual(identity["boot_id"], BOOT_ID.read_text().strip())
        for ref in ("parent", "supervisor", "child"):
            self.assertIs(type(identity[ref]["pid"]), int)
            self.assertIs(type(identity[ref]["start_ticks"]), int)

    def test_identity_names_the_command_child_not_its_forked_children(self):
        f0, f1, f2 = (self.pid_file(n) for n in ("self", "bg1", "bg2"))
        self.submit(f"echo $$ > {f0}; sleep 61 & echo $! > {f1}; sleep 62 & echo $! > {f2}; wait")
        child, bg1, bg2 = (self.own(read_pid(f)) for f in (f0, f1, f2))
        identity = self.shell.run_identity()
        self.assertEqual(identity["child"]["pid"], child)
        self.assertNotIn(identity["child"]["pid"], {bg1, bg2})
        self.assertNotIn(identity["supervisor"]["pid"], {bg1, bg2})
        self.assertEqual(identity["child"]["start_ticks"], proc(child)["start"])
        self.assertEqual(identity["child_group"], child)
        for bg in (bg1, bg2):  # the forked children share the command's group: that is why the group is the cross-check
            self.assertEqual(proc(bg)["pgrp"], identity["child_group"])
            self.assertEqual(proc(bg)["ppid"], child)

    def test_identity_survives_a_command_that_changes_its_children_session(self):
        f0, f1 = self.pid_file("self"), self.pid_file("setsid")
        self.submit(f"echo $$ > {f0}; setsid sleep 63 & echo $! > {f1}; wait")
        child, detached = self.own(read_pid(f0)), self.own(read_pid(f1))
        identity = self.shell.run_identity()
        self.assertEqual(identity["child"]["pid"], child)
        self.assertEqual(identity["session"], self.parent_pid)
        self.assertNotEqual(proc(detached)["session"], identity["session"], "test premise: setsid moved it")

    def test_result_is_a_copy_not_live_state(self):
        self.submit("sleep 30; :")
        first = self.shell.run_identity()
        pristine = repr(first)
        first["child"]["pid"] = -1
        first["parent"] = None
        first["uid"] = -1
        first["phase"] = "forged"
        second = self.shell.run_identity()
        self.assertEqual(repr(second), pristine)

    def test_run_identity_is_read_only_for_shell_state(self):
        self.submit("sleep 30; :")
        before = repr(self.shell.snapshot())
        for _ in range(5):
            self.shell.run_identity()
        self.assertEqual(repr(self.shell.snapshot()), before)

    def test_identity_pins_open_for_parent_supervisor_and_child(self):
        # Cross-API check: the numbers run_identity hands out are exactly what a pin accepts.
        self.submit("sleep 30; :")
        identity = self.shell.run_identity()
        env = LinuxStopEnvironment()
        for name in ("parent", "supervisor", "child"):
            pin = ProcessPin.open(env, identity[name]["pid"], identity[name]["start_ticks"],
                                  uid=identity["uid"], boot_id=identity["boot_id"])
            try:
                self.assertEqual(pin.status, PINNED, name)
            finally:
                pin.close()


class RunIdentityLifecycleTests(ManagedShellCase):
    def finish(self):
        until(self.shell, lambda s: s["lifecycle"]["lifetime"] == "ended")

    def test_phase_ended_after_command_ends_and_identity_stays_readable(self):
        f0 = self.pid_file("self")
        request_id = self.submit(f"echo $$ > {f0}; sleep 1")
        child = read_pid(f0)
        started = self.shell.run_identity()
        self.assertEqual((started["phase"], started["request_id"]), ("running", request_id))
        self.finish()
        identity = self.shell.run_identity()
        if identity is not None:  # contract: None or phase == "ended"
            self.assertEqual(identity["phase"], "ended")
            self.assertEqual(identity["request_id"], request_id)
            self.assertEqual(identity["child"]["pid"], child)
        # The recorded child is gone; a pin on the recorded identity must not signal anything.
        self.assertFalse(alive(child))
        pin = ProcessPin.open(LinuxStopEnvironment(), child, started["child"]["start_ticks"],
                              uid=os.getuid(), boot_id=started["boot_id"])
        try:
            self.assertEqual(pin.status, ENDED)
            self.assertEqual(pin.send(signal.SIGTERM), ENDED)
        finally:
            pin.close()

    def test_ended_phase_is_not_reported_while_a_descendant_keeps_the_lifetime_active(self):
        # SPEC: the supervisor is a subreaper; lifetime stays active while descendants remain.
        f1 = self.pid_file("bg")
        self.submit(f"(sleep 64 & echo $! > {f1}); exit 0")
        bg = self.own(read_pid(f1))
        until(self.shell, lambda s: s["lifecycle"]["main_exit"] is not None)
        for _ in range(20):
            self.shell.poll(.02)
        identity = self.shell.run_identity()
        self.assertTrue(alive(bg))
        self.assertIsNotNone(identity)
        self.assertNotEqual(identity["phase"], "ended", "descendant still alive but identity claims the run ended")

    def test_instant_command_never_raises_and_has_consistent_shape(self):
        request_id = self.submit(":", started=False)
        seen = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.shell.poll(.01)
            self.shell.display_bytes()
            identity = self.shell.run_identity()
            seen.append(identity)
            if identity is not None:
                self.assertEqual(identity["request_id"], request_id)
                self.assertIn(identity["phase"], {"starting", "running", "ended", "unknown"})
                for ref in ("parent", "supervisor", "child"):
                    entry = identity[ref]
                    if entry is not None:
                        self.assertIs(type(entry["pid"]), int)
                        self.assertIn(type(entry["start_ticks"]), (int, type(None)))
                        if entry["start_ticks"] is not None and alive(entry["pid"]):
                            self.assertEqual(entry["start_ticks"], proc(entry["pid"])["start"], (ref, identity["phase"]))
            if state["lifecycle"]["lifetime"] == "ended":
                break
        final = self.shell.run_identity()
        self.assertTrue(final is None or final["phase"] == "ended", final)

    def test_second_request_reports_its_own_identity_without_stale_start_ticks(self):
        f0 = self.pid_file("first")
        first_id = self.submit(f"echo $$ > {f0}; sleep 1")
        first_child = read_pid(f0)
        first = self.shell.run_identity()
        self.finish()
        until(self.shell, lambda s: s["lifecycle"]["input_barrier"])
        self.shell.release_input()
        until(self.shell, lambda s: s["lifecycle"]["control_returned"])
        self.shell.request_takeover()
        until(self.shell, lambda s: s["parent_mode"] == "manual_prompt")
        self.shell.send_user(b"wb-handoff\n")
        until(self.shell, lambda s: s["parent_mode"] == "control_wait")
        self.shell.claim_manager()
        # A fresh request: no identity until it exists, and never the previous run's refs.
        self.assertTrue(self.shell.run_identity() is None or self.shell.run_identity()["request_id"] != first_id)
        f1 = self.pid_file("second")
        second_id = self.submit(f"echo $$ > {f1}; sleep 30; :", started=False)
        observed = []
        deadline = time.monotonic() + 10
        second_child = None
        violations = []
        while time.monotonic() < deadline:
            state = self.shell.poll(.0)
            self.shell.display_bytes()
            identity = self.shell.run_identity()
            if identity is not None:
                self.assertEqual(identity["request_id"], second_id)
                observed.append(identity)
                for ref in ("supervisor", "child"):
                    entry = identity[ref]
                    if entry is None:
                        continue
                    self.assertNotEqual(entry["pid"], first_child)
                    previous = first["child"] if ref == "child" else first["supervisor"]
                    if entry["start_ticks"] is not None and alive(entry["pid"]) \
                            and entry["start_ticks"] != proc(entry["pid"])["start"]:
                        violations.append((ref, identity["phase"], entry, "previous run's ticks:", previous["start_ticks"]))
            if state["lifecycle"]["experiment_started"] and f1.exists() and f1.read_text().strip():
                second_child = int(f1.read_text())
                self.own(second_child)
                break
        self.assertIsNotNone(second_child)
        identity = self.shell.run_identity()
        self.assertEqual(identity["child"]["pid"], second_child)
        self.assertEqual(identity["child"]["start_ticks"], proc(second_child)["start"])
        self.assertEqual(violations, [], "run_identity reported start_ticks that do not belong to the reported pid")


class _PaneStandIn:
    """Just what ``HostShellPort`` touches; the shell behind it is the real one."""

    def __init__(self, shell):
        self.shell, self.choice, self.io_lock = shell, shell._transport.choice, threading.RLock()

    def open_tap(self):
        return bytearray()

    def exited(self):
        return False


class HostShellPortIdentityTests(ManagedShellCase):
    def test_port_identity_equals_shell_identity_and_proc(self):
        pane = _PaneStandIn(self.shell)
        port = HostShellPort(pane, lambda: pane)
        self.assertIsNone(port.run_identity())
        f0 = self.pid_file("self")
        request_id = self.submit(f"echo $$ > {f0}; sleep 30; :")
        child = self.own(read_pid(f0))
        identity = port.run_identity()
        self.assertEqual(identity["request_id"], request_id)
        self.assertEqual(identity["child"], {"pid": child, "start_ticks": proc(child)["start"]})
        self.assertEqual(identity["session"], proc(child)["session"])
        self.assertEqual(identity["child_group"], proc(child)["pgrp"])
        self.assertEqual(identity, self.shell.run_identity())

    def test_port_identity_waits_for_the_pane_io_lock(self):
        pane = _PaneStandIn(self.shell)
        port = HostShellPort(pane, lambda: pane)
        self.submit("sleep 30; :")
        result = []
        pane.io_lock.acquire()
        try:
            worker = threading.Thread(target=lambda: result.append(port.run_identity()), daemon=True)
            worker.start()
            worker.join(0.3)
            self.assertTrue(worker.is_alive(), "run_identity must serialize on the pane io_lock like the other port reads")
        finally:
            pane.io_lock.release()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["phase"], "running")


if __name__ == "__main__":
    unittest.main()
