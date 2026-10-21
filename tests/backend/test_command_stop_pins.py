"""S6/S7: ``ProcessPin`` and ``scan_tracked`` against real processes this test starts.

Expected values come from this test's own ``/proc/<pid>/stat`` parsing,
``os.getuid()`` and the boot id file, never from production scan output. Only
processes started here are ever signalled, and each is reaped on cleanup.
"""
from pathlib import Path
import os
import signal
import subprocess
import tempfile
import time
import unittest
from uuid import uuid4

from workbench.backend.command_stop import LinuxStopEnvironment, ProcessPin, ProcStat, scan_tracked
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell

BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def proc_stat(pid):
    """(state, ppid, pgrp, session, starttime) parsed by the test itself."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    return fields[0], int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19])


class RealProcessCase(unittest.TestCase):
    def spawn(self, *argv):
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.reap, process)
        return process

    @staticmethod
    def reap(process):
        if process.poll() is None:
            process.kill()
        process.wait()

    def open_pin(self, process, start_ticks=None, *, uid=None, env=None):
        ticks = proc_stat(process.pid)[4] if start_ticks is None else start_ticks
        pin = ProcessPin.open(env or LinuxStopEnvironment(), process.pid, ticks,
                              uid=os.getuid() if uid is None else uid, boot_id=BOOT_ID)
        self.addCleanup(pin.close)
        return pin


class ProcessPinTests(RealProcessCase):
    def test_matching_start_ticks_pin_and_a_null_signal_leaves_the_process_running(self):
        process = self.spawn("sleep", "30")
        pin = self.open_pin(process)
        self.assertEqual(pin.status, "pinned")
        self.assertEqual(pin.verify(), "pinned")
        self.assertEqual(pin.send(0), "sent")
        self.assertIsNone(process.poll())

    def test_wrong_start_ticks_are_stale_and_no_signal_is_sent(self):
        process = self.spawn("sleep", "30")
        pin = self.open_pin(process, proc_stat(process.pid)[4] + 1)
        self.assertEqual(pin.status, "stale")
        self.assertEqual(pin.send(signal.SIGTERM), "stale")
        time.sleep(0.1)
        self.assertIsNone(process.poll(), "a stale pin must not signal the process")

    def test_reaped_and_zombie_processes_are_ended(self):
        reaped = self.spawn("sleep", "0.2")
        reaped_ticks = proc_stat(reaped.pid)[4]
        reaped.wait()
        self.assertEqual(self.open_pin(reaped, reaped_ticks).status, "ended")
        zombie = self.spawn("true")
        ticks = proc_stat(zombie.pid)[4]
        deadline = time.monotonic() + 5
        while proc_stat(zombie.pid)[0] != "Z" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(proc_stat(zombie.pid)[0], "Z")
        pin = self.open_pin(zombie, ticks)
        self.assertEqual((pin.status, pin.send(0)), ("ended", "ended"))

    def test_a_different_expected_uid_is_refused_without_a_signal(self):
        # No second user is available without privileges: the identity names another
        # uid than the live process runs as, which is the same comparison.
        process = self.spawn("sleep", "30")
        pin = self.open_pin(process, uid=os.getuid() + 1)
        self.assertEqual((pin.status, pin.send(signal.SIGTERM)), ("different_uid", "different_uid"))
        time.sleep(0.1)
        self.assertIsNone(process.poll())


class ClosingEnvironment(LinuxStopEnvironment):
    def __init__(self):
        self.closed = []

    def close(self, fd):
        self.closed.append(fd)
        super().close(fd)


class ProcessPinContextTests(RealProcessCase):
    def test_leaving_the_with_block_closes_the_pidfd(self):
        process = self.spawn("sleep", "30")
        env = ClosingEnvironment()
        with self.open_pin(process, env=env) as pin:
            self.assertEqual(pin.send(0), "sent")
            fd = pin._fd
        self.assertEqual(env.closed, [fd])
        self.assertEqual(pin.send(signal.SIGTERM), "closed")
        self.assertIsNone(process.poll())


class ChangesAfterVerifyEnvironment(LinuxStopEnvironment):
    """S7: once armed, the process reads back with other start ticks right after a verification completes."""

    armed = False
    changed = False
    signals = 0

    def pidfd_exited(self, fd):
        result = super().pidfd_exited(fd)  # the last step of every verification
        self.changed = self.armed
        return result

    def read_stat(self, pid):
        stat = super().read_stat(pid)
        if stat is not None and self.changed:
            stat = ProcStat(stat.pid, stat.state, stat.ppid, stat.pgrp, stat.session, stat.start_ticks + 1, stat.uid)
        return stat

    def pidfd_send_signal(self, fd, signum):
        self.signals += 1
        super().pidfd_send_signal(fd, signum)


class IdentityChangeBeforeSignalTests(RealProcessCase):
    def test_start_ticks_changing_after_verify_sends_nothing(self):
        process = self.spawn("sleep", "30")
        env = ChangesAfterVerifyEnvironment()
        pin = self.open_pin(process, env=env)
        self.assertEqual(pin.status, "pinned")
        env.armed = True
        self.assertEqual(pin.send(signal.SIGTERM), "identity_changed")
        self.assertEqual(env.signals, 0)
        time.sleep(0.1)
        self.assertIsNone(process.poll(), "the real process must keep running")
        self.assertNotEqual(pin.send(signal.SIGTERM), "sent")
        self.assertEqual(env.signals, 0)


def ports(shell):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "e" * 64, "phase": "accepted"}},
        {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})


def until(shell, condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = shell.poll(.02)
        shell.display_bytes()
        if condition(state):
            return state
    raise AssertionError({"timeout": shell.snapshot()})


class ScanTrackedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cs01-scan-")
        self.addCleanup(self.directory.cleanup)
        self.shell = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "HOME": self.directory.name},
                                     choice=ShellChoice("bash", "/usr/bin/bash"))
        self.addCleanup(self.shell.close)
        self.shell.send_user(b"wb-handoff\n")
        until(self.shell, lambda s: s["parent_mode"] == "control_wait")
        self.shell.claim_manager()

    def run_command(self, script):
        """Run ``script`` (it writes named pids into files first); return {name: pid}."""
        control, automation = ports(self.shell)
        self.shell.submit(control, script.replace("@", self.directory.name), automation)
        until(self.shell, lambda s: s["lifecycle"]["experiment_started"])
        pids = {}
        deadline = time.monotonic() + 5
        for name in ("cmd", "bg", "away", "short"):
            path = Path(self.directory.name) / name
            if f"> @/{name}" not in script:
                continue
            while not (path.exists() and path.read_text().strip()) and time.monotonic() < deadline:
                self.shell.poll(.02)
            pids[name] = int(path.read_text())
        self.addCleanup(self.kill_all, list(pids.values()), self.shell.run_identity()["supervisor"]["pid"])
        return pids

    @staticmethod
    def kill_all(pids, supervisor):
        for pid in pids:  # only processes this test's command started
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_session_members_are_tracked_and_setsid_descendants_are_left_session(self):
        pids = self.run_command("echo $$ > @/cmd; sleep 60 & echo $! > @/bg; setsid sleep 61 & echo $! > @/away; wait")
        # The test reads the structure itself.
        away_state = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            away_state = proc_stat(pids["away"])
            if away_state[3] == pids["away"]:
                break
            time.sleep(0.01)
        _, _, _, parent_session, _ = proc_stat(pids["cmd"])
        self.assertEqual(parent_session, self.shell.parent_pid)
        self.assertEqual(proc_stat(pids["bg"])[3], self.shell.parent_pid)
        self.assertEqual(proc_stat(pids["away"])[3], pids["away"])
        identity = self.shell.run_identity()
        scan = scan_tracked(identity)
        tracked = {item.pid: item.start_ticks for item in scan.tracked}
        left = {item.pid: item.start_ticks for item in scan.left_session}
        self.assertEqual(tracked, {pids["cmd"]: proc_stat(pids["cmd"])[4], pids["bg"]: proc_stat(pids["bg"])[4]})
        self.assertEqual(left, {pids["away"]: proc_stat(pids["away"])[4]})
        excluded = {identity["supervisor"]["pid"], self.shell.parent_pid, os.getpid()}
        self.assertFalse(excluded & (set(tracked) | set(left)))

    def test_group_members_stay_tracked_without_the_supervisor_and_forbidden_pids_are_never_listed(self):
        pids = self.run_command("echo $$ > @/cmd; sleep 60 & echo $! > @/bg; setsid sleep 61 & echo $! > @/away; wait")
        identity = self.shell.run_identity()
        gone = {**identity, "supervisor": {**identity["supervisor"], "start_ticks": identity["supervisor"]["start_ticks"] + 1}}
        scan = scan_tracked(gone)  # the supervisor no longer matches: only the process group + session remain
        self.assertEqual({item.pid for item in scan.tracked}, {pids["cmd"], pids["bg"]})
        self.assertEqual(scan.left_session, ())
        # Supervisor and host shell parent are excluded whatever the recorded ticks say.
        listed = {item.pid for item in scan.tracked + scan.left_session}
        self.assertFalse({identity["supervisor"]["pid"], self.shell.parent_pid} & listed)
        # A pgrp-only match (supervisor identity disagrees) still never lists the supervisor, even when
        # the identity claims the supervisor's own group as the command group.
        tricked = {**gone, "child_group": proc_stat(identity["supervisor"]["pid"])[2]}
        listed = {item.pid for item in scan_tracked(tricked).tracked + scan_tracked(tricked).left_session}
        self.assertNotIn(identity["supervisor"]["pid"], listed)
        self.assertNotIn(self.shell.parent_pid, listed)
        scan = scan_tracked(identity, forbidden=frozenset({pids["bg"]}))
        self.assertEqual({item.pid for item in scan.tracked}, {pids["cmd"]})

    def test_supervisor_is_excluded_even_when_its_ticks_are_unknown_and_the_parent_always_is(self):
        pids = self.run_command("echo $$ > @/cmd; sleep 60 & echo $! > @/bg; wait")
        identity = self.shell.run_identity()
        supervisor = identity["supervisor"]["pid"]
        # Synthetic: ticks unknown. With the real command group the pgrp rule still finds the members.
        blind = {**identity, "supervisor": {"pid": supervisor, "start_ticks": None}}
        scan = scan_tracked(blind)
        self.assertEqual({item.pid for item in scan.tracked}, {pids["cmd"], pids["bg"]})
        # The "command group" names the supervisor's own group, and the host shell shares the session:
        # only an explicit exclusion keeps the supervisor and the parent out.
        for group in (proc_stat(supervisor)[2], proc_stat(self.shell.parent_pid)[2]):
            scan = scan_tracked({**blind, "child_group": group})
            listed = {item.pid for item in scan.tracked + scan.left_session}
            self.assertNotIn(supervisor, listed)
            self.assertNotIn(self.shell.parent_pid, listed)

    def test_exited_members_are_not_reported(self):
        pids = self.run_command("echo $$ > @/cmd; sleep 0.1 & echo $! > @/short; exec sleep 60")
        deadline = time.monotonic() + 5
        while proc_stat(pids["short"])[0] != "Z" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(proc_stat(pids["short"])[0], "Z", "the exited child stays a zombie of the exec'd command")
        scan = scan_tracked(self.shell.run_identity())
        self.assertEqual({item.pid for item in scan.tracked}, {pids["cmd"]})
        self.assertEqual(scan.left_session, ())


if __name__ == "__main__":
    unittest.main()
