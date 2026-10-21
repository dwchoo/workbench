"""S6: ``PersistentShell.run_identity()`` against a real shell and real processes.

Expected values never come from production code: the command records its own
pid, and every other number is read by this test from ``/proc/<pid>/stat``,
``os.getuid()`` and ``/proc/sys/kernel/random/boot_id``.
"""
from pathlib import Path
import os
import signal
import tempfile
import time
import unittest
import threading
from uuid import uuid4

from workbench.backend.panes import HostShellPort
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell

BASH = ShellChoice("bash", "/usr/bin/bash")


def ports(shell):
    state = shell.snapshot()
    return ({"portVersion": 2, "kind": "ShellControl", "payload": {
        "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
        "requestId": str(uuid4()), "approvalHash": "c" * 64, "phase": "accepted"}},
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


def proc_stat(pid):
    """(ppid, pgrp, session, starttime) parsed by the test itself."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    return int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19])


class ManagedShellCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cs01-identity-")
        self.addCleanup(self.directory.cleanup)
        self.shell = PersistentShell(user_environment={"PATH": "/usr/bin:/bin", "HOME": self.directory.name}, choice=BASH)
        self.addCleanup(self.shell.close)
        self.shell.send_user(b"wb-handoff\n")
        until(self.shell, lambda s: s["parent_mode"] == "control_wait")
        self.shell.claim_manager()

    def run_recording_pid(self, tail):
        """Submit a command that records its own pid, then replaces itself (same pid)."""
        marker = Path(self.directory.name) / f"pid-{uuid4().hex}"
        control, automation = ports(self.shell)
        self.shell.submit(control, f"echo $$ > {marker}; {tail}", automation)
        until(self.shell, lambda s: s["lifecycle"]["experiment_started"])
        deadline = time.monotonic() + 5
        while not (marker.exists() and marker.read_text().strip()) and time.monotonic() < deadline:
            self.shell.poll(.02)
        pid = int(marker.read_text())
        self.addCleanup(self.reap, pid, control["payload"]["requestId"])
        return pid, control["payload"]["requestId"]

    def reap(self, pid, _request):
        try:
            os.kill(pid, signal.SIGKILL)  # only the test's own command
        except ProcessLookupError:
            pass


class RunIdentityTests(ManagedShellCase):
    def test_no_request_yields_none(self):
        self.assertIsNone(self.shell.run_identity())

    def test_running_command_identity_equals_proc_values(self):
        child, request_id = self.run_recording_pid("exec sleep 30")
        supervisor, group, session, child_start = proc_stat(child)
        parent, _, _, supervisor_start = proc_stat(supervisor)
        parent_start = proc_stat(parent)[3]
        identity = self.shell.run_identity()
        self.assertIsNotNone(identity, "a running managed request must have an identity")
        self.assertEqual(parent, self.shell.parent_pid)
        self.assertEqual(identity["request_id"], request_id)
        self.assertEqual(identity["phase"], "running")
        self.assertEqual(identity["parent"], {"pid": parent, "start_ticks": parent_start})
        self.assertEqual(identity["supervisor"], {"pid": supervisor, "start_ticks": supervisor_start})
        self.assertEqual(identity["child"], {"pid": child, "start_ticks": child_start})
        self.assertEqual(identity["child_group"], child)
        self.assertEqual(group, child)
        self.assertEqual(identity["session"], parent)
        self.assertEqual(session, parent)
        self.assertEqual(identity["uid"], os.getuid())
        self.assertEqual(identity["boot_id"], Path("/proc/sys/kernel/random/boot_id").read_text().strip())

    def test_phase_is_ended_after_the_command_ends(self):
        child, _ = self.run_recording_pid("exec sleep 1")
        running = self.shell.run_identity()
        self.assertIsNotNone(running)
        self.assertEqual(running["phase"], "running")
        until(self.shell, lambda s: s["lifecycle"]["lifetime"] == "ended")
        identity = self.shell.run_identity()
        self.assertIsNotNone(identity)
        self.assertEqual(identity["phase"], "ended")
        self.assertEqual(identity["child"]["pid"], child)


class _PaneStandIn:
    """Just what ``HostShellPort`` touches; the shell behind it is the real one."""

    def __init__(self, shell):
        self.shell, self.choice, self.io_lock = shell, shell._transport.choice, threading.RLock()

    def open_tap(self):
        return bytearray()

    def exited(self):
        return False


class HostShellPortIdentityTests(ManagedShellCase):
    def test_port_returns_the_shells_identity_under_the_pane_lock(self):
        pane = _PaneStandIn(self.shell)
        port = HostShellPort(pane, lambda: pane)
        self.assertIsNone(port.run_identity())
        child, request_id = self.run_recording_pid("exec sleep 30")
        identity = port.run_identity()
        self.assertIsNotNone(identity)
        self.assertEqual((identity["request_id"], identity["child"]["pid"], identity["phase"]),
                         (request_id, child, "running"))
        self.assertEqual(identity, self.shell.run_identity())


if __name__ == "__main__":
    unittest.main()
