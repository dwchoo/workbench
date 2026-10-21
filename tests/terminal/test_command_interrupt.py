"""S5: a lone user Ctrl+C reaches a verified Workbench command through the real ``ShellPane.admit``.

Real ``ShellPane`` -> ``PersistentShell`` -> Bash and a real ``sleep``. Expected values are read by this
test, never taken from production code: the command records its own pid, the process group and start
ticks come from ``/proc/<pid>/stat``, the foreground group from ``os.tcgetpgrp`` on the PTY master, and
the exit code (130) is the one a real SIGINT produces. Input owner / takeover fields are compared before
and after each admit.
"""
from pathlib import Path
import os
import signal
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from workbench.backend.panes import ShellPane
from workbench.backend.service import Backend
from workbench.contracts.ui_v1 import PaneId, Reason
from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState

BASH = ShellChoice("bash", "/usr/bin/bash")
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "xterm"}


def proc_stat(pid):
    """(ppid, pgrp, session, starttime) parsed by the test itself."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    return int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19])


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0] not in {"Z", "X"}
    except OSError:
        return False


class InterruptCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cs07-interrupt-")
        self.addCleanup(self.directory.cleanup)
        self.pane = ShellPane(BASH, {**ENV, "HOME": self.directory.name})
        self.addCleanup(self.pane.close)
        self.pids: list[int] = []
        self.addCleanup(self.reap)
        self.until(lambda: self.pane.state["parent_mode"] == "manual_prompt")

    def reap(self):
        for pid in self.pids:  # only processes this test started (their own pid files)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def until(self, condition, seconds=10.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pane.pump()
            if condition():
                return
            time.sleep(0.02)
        raise AssertionError({"timeout": self.pane.shell.snapshot()})

    def hand_off(self):
        self.assertIsNone(self.pane.admit(b"wb-handoff\n"))
        self.until(lambda: self.pane.state["parent_mode"] == "control_wait")
        self.pane.state = self.pane.shell.claim_manager()

    def run_managed(self, tail="exec sleep 30"):
        """A managed request that records its own pid then replaces itself (same pid, own group)."""
        self.hand_off()
        shell = self.pane.shell
        state = shell.snapshot()
        marker = Path(self.directory.name) / f"pid-{uuid4().hex}"
        request_id = str(uuid4())
        shell.submit({"portVersion": 2, "kind": "ShellControl", "payload": {
            "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
            "requestId": request_id, "approvalHash": "c" * 64, "phase": "accepted"}},
            f"echo $$ > {marker}; {tail}",
            {"portVersion": 2, "kind": "AutomationState", "payload": {
                "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})
        self.until(lambda: self.pane.state["lifecycle"]["experiment_started"]
                   and marker.exists() and marker.read_text().strip())
        pid = int(marker.read_text())
        self.pids.append(pid)
        self.pane.state = shell.snapshot()
        return pid, request_id

    def fields(self):
        """Owner / takeover fields compared before and after an admit."""
        state = self.pane.shell.snapshot()
        return {key: state[key] for key in ("input_owner", "owner_epoch", "takeover_requested", "takeover_confirmed")}


class ManagedCommandInterruptTests(InterruptCase):
    def test_lone_ctrl_c_interrupts_a_managed_command_while_the_manager_owns_input(self):
        pid, _request = self.run_managed()
        _, group, _, _ = proc_stat(pid)
        self.assertEqual(os.tcgetpgrp(self.pane._master_fd), group)  # the command is the PTY foreground
        before = self.fields()
        self.assertEqual(before["input_owner"], "manager")
        self.assertFalse(before["takeover_requested"])

        self.assertIsNone(self.pane.admit(b"\x03"))

        self.until(lambda: self.pane.state["lifecycle"]["main_exit"] is not None)
        self.pane.shell.release_input()  # the owner (HostShellPort) gives the input barrier back after the run
        self.until(lambda: self.pane.state["lifecycle"]["controller_exit"] is not None)
        life = self.pane.state["lifecycle"]
        self.assertEqual(life["main_exit"], -signal.SIGINT)  # the command died of SIGINT ...
        self.assertEqual(life["controller_exit"], 130)  # ... which the shell reports as 128 + 2
        self.assertFalse(alive(pid))
        self.assertEqual(self.fields(), before)

    def wait_interrupted(self, pid):
        self.until(lambda: not alive(pid))
        self.until(lambda: self.pane.state["lifecycle"]["main_exit"] == -signal.SIGINT)

    def test_unconfirmed_takeover_request_still_delivers_and_changes_nothing(self):
        pid, _request = self.run_managed()
        requested = self.pane.shell.request_takeover()
        self.assertTrue(requested["takeover_requested"])
        self.assertFalse(requested["takeover_confirmed"])  # requested, never confirmed
        self.pane.state = requested
        before = self.fields()
        # the ordinary rule holds a different key in this state ...
        self.assertEqual(self.pane.admit(b"a")[0], Reason.INPUT_TARGET_UNKNOWN)
        self.assertTrue(alive(pid))

        self.assertIsNone(self.pane.admit(b"\x03"))  # ... but the lone Ctrl+C reaches the verified command

        self.wait_interrupted(pid)
        self.assertEqual(self.fields(), before)
        self.assertTrue(before["takeover_requested"])
        self.assertFalse(before["takeover_confirmed"])

    def test_confirmed_takeover_delivers_through_the_same_path_and_changes_nothing(self):
        pid, _request = self.run_managed()
        self.pane.shell.request_takeover()
        confirmed = self.pane.shell.confirm_takeover()
        self.assertIsNotNone(confirmed["foreground_target"])
        self.pane.state = confirmed
        before = self.fields()
        self.assertTrue(before["takeover_confirmed"])

        self.assertIsNone(self.pane.admit(b"\x03"))

        self.assertEqual(self.fields(), before)  # nothing is observed until the shell's own events are drained
        self.wait_interrupted(pid)
        after = self.fields()  # the ended run drops the confirmed target by itself; owner/request stay
        for key in ("input_owner", "owner_epoch", "takeover_requested"):
            self.assertEqual(after[key], before[key])

    def test_a_target_that_no_longer_matches_is_re_verified_and_nothing_is_written(self):
        pid, request_id = self.run_managed()
        _ppid, group, _session, start = proc_stat(pid)
        transport = self.pane.shell._transport
        stale = {"request_id": request_id, "pid": pid, "start_ticks": start + 1, "process_group": group}
        with self.assertRaises(UnsafeShellState):
            transport.send_interrupt(stale)  # a reused pid would carry other start ticks
        self.assertTrue(alive(pid))
        good = {"request_id": request_id, "pid": pid, "start_ticks": start, "process_group": group}
        transport.send_interrupt(good)  # the same call with the verified identity writes the byte
        self.until(lambda: not alive(pid))


class InterruptAuditTests(InterruptCase):
    def test_listener_gets_the_verified_identity_once_per_delivered_ctrl_c(self):
        pid, request_id = self.run_managed()
        _ppid, group, session, start = proc_stat(pid)
        listener = Mock()
        self.pane.interrupt_listener = listener

        self.assertIsNone(self.pane.admit(b"\x03"))

        listener.assert_called_once()
        (event,), _kwargs = listener.call_args
        self.assertEqual(event["request_id"], request_id)
        self.assertEqual((event["pid"], event["start_ticks"], event["process_group"]), (pid, start, group))
        self.assertEqual(event["session"], session)
        self.assertEqual(event["session"], self.pane.pid)
        self.assertEqual(event["uid"], os.getuid())
        self.assertIs(event["delivered"], True)
        self.assertEqual(event["input_owner"], "manager")
        self.assertEqual(event["parent_mode"], "control_wait")

    def test_a_failing_audit_snapshot_never_turns_a_delivered_ctrl_c_into_an_error(self):
        pid, _request = self.run_managed()
        listener = Mock()
        self.pane.interrupt_listener = listener
        real = self.pane.shell.snapshot
        calls = []

        def snapshot():
            calls.append(1)
            if len(calls) == 2:  # admit's own state read came first; the audit read fails once
                raise OSError("snapshot unavailable")
            return real()

        self.pane.shell.snapshot = snapshot
        self.assertIsNone(self.pane.admit(b"\x03"))  # delivered; no exception, no refusal
        self.until(lambda: not alive(pid))

    def test_a_failing_listener_never_withholds_the_ctrl_c(self):
        pid, _request = self.run_managed()
        self.pane.interrupt_listener = Mock(side_effect=RuntimeError("journal down"))

        self.assertIsNone(self.pane.admit(b"\x03"))

        self.until(lambda: not alive(pid))


class BackendInterruptJournalTests(InterruptCase):
    """``Backend`` forwards the input kind and journals ``command_interrupt`` (built without starting a backend)."""

    def make_backend(self, active_commands=()):
        backend = Backend.__new__(Backend)
        backend.panes = {PaneId.HOST_SHELL: self.pane}
        backend.handoffs = Mock()
        backend.terminal = Mock()
        backend.terminal.summary_command_active.side_effect = lambda command_id: command_id in active_commands
        backend._wire_shell(self.pane)
        return backend

    def records(self, backend):
        return [call.args[0] for call in backend.handoffs.record.call_args_list]

    def test_delivered_ctrl_c_is_journaled_as_a_user_request_with_the_verified_identity(self):
        pid, request_id = self.run_managed()
        _ppid, group, _session, start = proc_stat(pid)
        backend = self.make_backend(active_commands=(request_id,))

        self.assertIsNone(backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))

        (record,) = self.records(backend)
        at = record.pop("at")
        self.assertIsInstance(at, str)
        self.assertEqual(record, {"type": "command_interrupt", "requested_by": "user", "command_id": request_id,
                                  "pid": pid, "start_ticks": start, "process_group": group, "delivered": True,
                                  "input_owner": "manager", "parent_mode": "control_wait"})
        self.until(lambda: not alive(pid))

    def test_an_unresolvable_request_is_journaled_with_a_null_command_id_and_still_delivered(self):
        pid, _request = self.run_managed()
        backend = self.make_backend(active_commands=())

        self.assertIsNone(backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))

        (record,) = self.records(backend)
        self.assertIsNone(record["command_id"])
        self.assertEqual(record["pid"], pid)
        self.until(lambda: not alive(pid))

    def test_rejected_inputs_through_the_backend_leave_no_record_and_paste_kind_is_not_an_interrupt(self):
        pid, request_id = self.run_managed()
        backend = self.make_backend(active_commands=(request_id,))

        for data, kind in ((b"a", "input"), (b"\x03a", "input"), (b"a\x03", "input"), (b"\x03", "paste")):
            with self.subTest(data=data, kind=kind):
                refused = backend.admit(PaneId.HOST_SHELL, data, kind)
                self.assertEqual(refused[0], Reason.INPUT_OWNER_MANAGER)
        self.assertTrue(alive(pid))
        self.assertEqual(self.records(backend), [])

    def test_a_journal_failure_does_not_withhold_the_ctrl_c(self):
        pid, request_id = self.run_managed()
        backend = self.make_backend(active_commands=(request_id,))
        backend.handoffs.record.side_effect = OSError("disk full")

        self.assertIsNone(backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))

        self.until(lambda: not alive(pid))

    def test_a_restarted_host_shell_is_wired_and_journals_its_ctrl_c(self):
        backend = self.make_backend()
        backend.plan = SimpleNamespace(shell=BASH)
        backend.shell = self.pane
        backend._shell_env = {**ENV, "HOME": self.directory.name}
        backend._shell_cwd = None
        backend._shell_lock = threading.Lock()
        backend.restarts = []
        backend.phase = "ready"
        backend._write_record = Mock()
        self.assertIsNone(self.pane.admit(b"exit\n"))  # the old shell exits; only then may it be restarted
        self.until(self.pane.exited)

        result = backend._restart_shell()  # the real restart path builds the new ShellPane

        new = backend.shell
        self.addCleanup(new.close)
        self.assertTrue(result["restarted"])
        self.assertIsNot(new, self.pane)
        self.assertIsNotNone(new.interrupt_listener)
        self.pane = new
        self.until(lambda: new.state["parent_mode"] == "manual_prompt")
        pid, request_id = self.run_managed()
        backend.terminal.summary_command_active.side_effect = lambda command_id: command_id == request_id

        self.assertIsNone(backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))

        self.until(lambda: not alive(pid))
        (record,) = self.records(backend)
        self.assertEqual((record["type"], record["requested_by"], record["command_id"], record["pid"]),
                         ("command_interrupt", "user", request_id, pid))


class RejectedInputTests(InterruptCase):
    """Anything but a lone ``\\x03`` of kind ``input`` keeps the current rule; no byte of it is delivered."""

    def assert_rejected_and_untouched(self, pid, reason, cases):
        listener = Mock()
        self.pane.interrupt_listener = listener
        before = self.fields()
        for data, kind in cases:
            with self.subTest(data=data, kind=kind):
                refused = self.pane.admit(data, kind)
                self.assertIsNotNone(refused)
                self.assertEqual(refused[0], reason)
                self.assertTrue(alive(pid), "a rejected input must not reach the command")
                self.assertEqual(len(self.pane._pending), 0)
        self.assertEqual(self.pane.dropped_input_bytes, 0)
        self.assertEqual(self.fields(), before)
        listener.assert_not_called()

    CASES = ((b"a", "input"), (b"\x03a", "input"), (b"a\x03", "input"), (b"\x03\x03", "input"),
             (b"\x03", "paste"), (b"", "input"))

    def test_other_keys_mixed_input_and_paste_are_rejected_for_a_manager_owned_command(self):
        pid, _request = self.run_managed()
        self.assert_rejected_and_untouched(pid, Reason.INPUT_OWNER_MANAGER, self.CASES)

    def test_other_keys_mixed_input_and_paste_are_rejected_while_takeover_is_only_requested(self):
        pid, _request = self.run_managed()
        self.pane.state = self.pane.shell.request_takeover()
        self.assert_rejected_and_untouched(pid, Reason.INPUT_TARGET_UNKNOWN, self.CASES)


class NonWorkbenchForegroundTests(InterruptCase):
    """The exception applies only to a verified Workbench command; the current rules stay otherwise."""

    def setUp(self):
        super().setUp()
        self.listener = Mock()
        self.pane.interrupt_listener = self.listener

    def test_user_started_foreground_program_keeps_the_ordinary_rule_without_an_audit_record(self):
        self.assertIsNone(self.pane.admit(b"sleep 30\n"))
        self.until(lambda: os.tcgetpgrp(self.pane._master_fd) != self.pane.pid)
        group = os.tcgetpgrp(self.pane._master_fd)
        self.pids.append(group)
        self.until(lambda: self.pane.state["parent_mode"] == "manual_foreground")

        self.assertIsNone(self.pane.admit(b"\x03"))  # the user's own program: forwarded like any manual key

        self.until(lambda: not alive(group))
        self.listener.assert_not_called()

    def test_idle_user_prompt_keeps_the_ordinary_rule_without_an_audit_record(self):
        self.assertIsNone(self.pane.admit(b"typed"))
        self.until(lambda: self.pane.state["parent_mode"] == "manual_input")
        self.assertIsNone(self.pane.admit(b"\x03"))
        self.listener.assert_not_called()

    def test_unknown_lifecycle_state_never_takes_the_exception(self):
        pid, _request = self.run_managed()
        self.pane.shell._transport.lifecycle.fail_unknown("cs07_test_unknown_state")
        self.pane.state = self.pane.shell.snapshot()
        refused = self.pane.admit(b"\x03")
        self.assertIsNotNone(refused)
        self.assertTrue(alive(pid))
        self.listener.assert_not_called()

    def test_control_lost_never_takes_the_exception(self):
        pid, _request = self.run_managed()
        self.pane.shell._transport.boundary.control_lost = True
        refused = self.pane.admit(b"\x03")
        self.assertIsNotNone(refused)
        self.assertTrue(alive(pid))
        self.listener.assert_not_called()


class AfterMainExitTests(InterruptCase):
    def test_after_the_main_process_ended_the_manager_owned_rule_applies_and_nothing_is_recorded(self):
        pid, _request = self.run_managed("exec sleep 0.3")
        listener = Mock()
        self.pane.interrupt_listener = listener
        self.until(lambda: self.pane.state["lifecycle"]["main_exit"] is not None)
        self.assertFalse(alive(pid))

        refused = self.pane.admit(b"\x03")

        self.assertEqual(refused[0], Reason.INPUT_OWNER_MANAGER)
        listener.assert_not_called()

    def test_after_the_main_process_ended_and_the_shell_is_back_the_prompt_rule_applies(self):
        pid, _request = self.run_managed("exec sleep 0.3")
        listener = Mock()
        self.pane.interrupt_listener = listener
        self.pane.state = self.pane.shell.request_takeover()  # the user will get the prompt back
        self.until(lambda: self.pane.state["lifecycle"]["main_exit"] is not None)
        self.pane.shell.release_input()  # the owner gives the input barrier back after the run
        self.until(lambda: self.pane.state["parent_mode"] == "manual_prompt")
        self.assertFalse(alive(pid))
        self.assertEqual(os.tcgetpgrp(self.pane._master_fd), self.pane.pid)  # the shell is the foreground again

        self.assertIsNone(self.pane.admit(b"\x03"))  # an ordinary Ctrl+C at the user's prompt

        listener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
