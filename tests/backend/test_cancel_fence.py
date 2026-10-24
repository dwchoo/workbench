"""CS-O01 (FU-D03..D05): the cancel fence for task-less ``terminal`` commands after a free-work Task is cancelled.

After the manager cancels a free-work Task, the worker's next task-less ``terminal`` command is refused once
(``task_cancelled``, nothing typed) until the earliest of: the cancel notice reached the worker's OMP, one such
refusal was returned, or a newer Task exists. Commands after that run, journaled with ``after_cancel_of``.

Seam: ``TerminalService.handle`` with the ``test_cancel_host_command`` flow fixture (real TaskFlow and
HandoffService, a fake mailbox that can hold back the cancel notice, a real host shell pane), the terminal service
wired to the flow the way ``Backend`` wires it. Every process here is one the test started in its own pane.
"""

from __future__ import annotations

import threading
import time
import unittest

from workbench.backend.flow_terminal import HostGate, TerminalService
from workbench.backend.panes import HostShellPort
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, MailboxStatus

from test_cancel_host_command import CancelHostCommandFixture
from test_flow_terminal import AUTOMATION, call, journal, wait_until
import test_task_flow as fx


RETRY_INTERVAL = 2.0
SETTLE = 0.2  # after a deferred attempt returned: its outbox event has run


class NoticeMailbox(fx.FakeMailbox):
    """Defers (nothing submitted) the worker's cancel notice while ``hold_cancel`` is set."""

    def __init__(self):
        super().__init__()
        self.hold_cancel = threading.Event()
        self.block_cancel = threading.Event()  # cleared: the attempt waits inside deliver (handing over)
        self.block_cancel.set()
        self.in_cancel = threading.Event()
        self.cancel_attempts = 0

    def deliver(self, message, *, timeout=20):
        if message.target_role is ActorRole.WORKER and message.payload.get("cancel") is True:
            self.cancel_attempts += 1
            self.in_cancel.set()
            self.block_cancel.wait(30)
            if self.hold_cancel.is_set():
                return DeliveryReceipt(message.message_id, None, message.target_role, message.session_id, 1,
                                       MailboxStatus.DEFERRED, {"reason": "omp_busy"})
        return super().deliver(message, timeout=timeout)

    def cancel_notices(self):
        return [m for m in self.created if m.target_role is ActorRole.WORKER and m.payload.get("cancel") is True
                and m.message_id in self.delivered]


class FenceFixture(CancelHostCommandFixture):
    def open(self, *, start=True):
        if not isinstance(self.mailbox, NoticeMailbox):
            self.mailbox = NoticeMailbox()
        flow = super().open(start=start)
        # a held notice is retried seconds apart, so a check between two attempts is not one under way
        self.service._retry_interval = RETRY_INTERVAL
        return flow

    def setUp(self):
        super().setUp()
        self.terminal.close()  # the fixture's service is not wired to the fence; this one is, as Backend does
        self.shell_asks = 0
        self.terminal = self.wired_terminal()

    def host_shell(self):
        self.shell_asks += 1
        return HostShellPort(self.pane, lambda: self.pane)

    def wired_terminal(self):
        terminal = TerminalService(
            handoffs=self.service, host_shell=self.host_shell, gate=HostGate(),
            log_root=self.root / "workflow" / "terminal", automation=lambda: AUTOMATION,
            active_task=self.flow.active_task, task_commands=self.flow.active_commands, poll_interval=0.02,
            command_stop=self.command_stop, cancel_fence=self.flow.take_cancel_fence,
            after_cancel_of=self.flow.after_cancel_of)
        self.flow.host_commands = terminal.commands_for_task
        return terminal

    def cancel_with_notice_held(self):
        work = self.running_work()
        self.mailbox.hold_cancel.set()
        result = self.cancel(work["task_id"])
        self.assertEqual(result["status"], "cancelled", result)
        self.deferred(1)
        return work["task_id"]

    def deferred(self, attempts):
        """Wait until the lane's ``attempts``-th try of the held notice was deferred (and its outcome handled)."""
        self.assertTrue(wait_until(lambda: self.mailbox.cancel_attempts >= attempts, 10), "the lane tried it")
        time.sleep(SETTLE)

    def records(self, kind):
        return [r for r in journal(self.root) if r.get("type") == kind]

    def assert_fence_refusal(self, result, task_id):
        self.assertEqual((result.get("status"), result.get("reason")), ("rejected", "task_cancelled"), result)
        self.assertEqual(result["task_id"], task_id)
        self.assertIn(task_id, result["detail"])
        self.assertIn("cancelled", result["detail"])
        self.assertIn("not run", result["detail"])
        self.assertIn("call again", result["detail"])


class FenceRefusalTests(FenceFixture):
    def test_a_task_less_command_before_the_cancel_notice_arrives_is_refused_and_types_nothing(self):
        task_id = self.cancel_with_notice_held()
        asks = self.shell_asks
        result = self.run_command("echo after-cancel-1")
        self.assert_fence_refusal(result, task_id)
        self.assertEqual(self.shell_asks, asks, "the host shell was not even asked")
        self.assertIsNone(self.terminal.current())
        self.assertEqual(self.records("terminal_started"), [])
        refused = self.records("terminal_refused")[-1]
        self.assertEqual((refused["status"], refused["reason"]), ("rejected", "task_cancelled"))

    def test_a_status_call_is_not_refused_and_does_not_use_up_the_fence(self):
        task_id = self.cancel_with_notice_held()
        status = self.terminal.handle(ActorRole.WORKER, call({"command": None}))
        self.assertNotEqual(status.get("reason"), "task_cancelled", status)
        self.assert_fence_refusal(self.run_command("echo after-status"), task_id)

    def test_after_one_refusal_the_next_task_less_command_runs(self):
        task_id = self.cancel_with_notice_held()
        self.assert_fence_refusal(self.run_command("echo after-cancel-2"), task_id)
        again = self.run_command("echo after-cancel-2", wait=30)
        self.assertEqual((again.get("status"), again.get("exit_code")), ("exited", 0), again)
        self.assertIn("after-cancel-2", again["output_tail"])


class FenceLiftTests(FenceFixture):
    def test_no_refusal_once_the_cancel_notice_reached_the_worker(self):
        # test_f45: the worker answers the delivered cancel notice with a terminal command at once
        work = self.running_work()
        self.assertEqual(self.cancel(work["task_id"])["status"], "cancelled")
        self.assertTrue(wait_until(lambda: len(self.mailbox.cancel_notices()) == 1), "the notice was delivered")
        result = self.run_command("echo notice-seen", wait=30)
        self.assertEqual((result.get("status"), result.get("exit_code")), ("exited", 0), result)

    def test_no_refusal_while_the_notice_is_being_handed_to_the_worker_omp(self):
        # The worker may already have the notice in its input before the bridge's acceptance reaches Workbench
        # (test_f45 race): a delivery attempt under way never refuses; a deferred attempt arms it again.
        work = self.running_work()
        self.mailbox.block_cancel.clear()
        self.assertEqual(self.cancel(work["task_id"])["status"], "cancelled")
        self.assertTrue(self.mailbox.in_cancel.wait(10), "the lane is handing the notice over")
        try:
            result = self.run_command("echo during-injection", wait=30)
        finally:
            self.mailbox.block_cancel.set()
        self.assertEqual((result.get("status"), result.get("exit_code")), ("exited", 0), result)

    def test_a_deferred_attempt_arms_the_fence_again(self):
        task_id = self.cancel_with_notice_held()
        self.deferred(2)
        self.assert_fence_refusal(self.run_command("echo after-deferral"), task_id)

    def test_a_newer_task_lifts_the_fence(self):
        self.cancel_with_notice_held()
        second = self.running_work("a second job")["task_id"]
        self.assertEqual(self.to_manager({"kind": "done", "message": "second done", "task_id": second})["status"],
                         "queued")
        self.assertTrue(wait_until(lambda: self.flow.task_view()["closed_reason"] == "done"))
        result = self.run_command("echo after-newer-task", wait=30)
        self.assertEqual((result.get("status"), result.get("exit_code")), ("exited", 0), result)
        self.mailbox.hold_cancel.clear()


class AfterCancelMarkTests(FenceFixture):
    def test_task_less_commands_after_the_fence_carry_after_cancel_of_and_stay_out_of_the_task(self):
        task_id = self.cancel_with_notice_held()
        self.assert_fence_refusal(self.run_command("sleep 711.1"), task_id)
        started = self.run_command("sleep 711.1")
        self.assertEqual(started.get("status"), "running", started)
        request = [r for r in self.records("terminal_request") if r.get("command") == "sleep 711.1"][-1]
        begun = [r for r in self.records("terminal_started") if r["command_id"] == started["command_id"]][0]
        for record in (request, begun):
            self.assertIsNone(record["task_id"], record)
            self.assertEqual(record.get("after_cancel_of"), task_id, record)
        self.assertEqual(self.flow.status_view(task_id)["host_commands"], [])
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")  # the test ends its own command

    def test_no_mark_once_a_newer_task_exists(self):
        first = self.running_work()["task_id"]
        self.cancel(first)
        self.assertTrue(wait_until(lambda: len(self.mailbox.cancel_notices()) == 1))
        second = self.running_work("a second job")["task_id"]
        self.to_manager({"kind": "done", "message": "second done", "task_id": second})
        self.assertTrue(wait_until(lambda: self.flow.task_view()["closed_reason"] == "done"))
        done = self.run_command("echo no-mark", wait=30)
        self.assertEqual(done.get("status"), "exited", done)
        begun = [r for r in self.records("terminal_started") if r["command_id"] == done["command_id"]][0]
        self.assertNotIn("after_cancel_of", begun)


class ScopeTests(FenceFixture):
    def test_an_experiment_cancel_arms_no_fence_and_marks_nothing(self):
        self.gates.exit.clear()
        pending = self.new_experiment()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"))
        self.mailbox.hold_cancel.set()  # its cancel notice (if any) stays undelivered
        result = self.cancel(pending["task_id"])
        self.assertEqual(result["status"], "cancel_requested", result)
        self.gates.exit.set()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "closed"))
        self.assertEqual(self.flow.task_view()["closed_reason"], "cancelled")
        done = self.run_command("echo after-experiment-cancel", wait=30)
        self.assertEqual((done.get("status"), done.get("exit_code")), ("exited", 0), done)
        begun = [r for r in self.records("terminal_started") if r["command_id"] == done["command_id"]][0]
        self.assertNotIn("after_cancel_of", begun)
        self.mailbox.hold_cancel.clear()

    def test_the_fence_survives_a_backend_restart(self):
        task_id = self.cancel_with_notice_held()
        self.terminal.close()
        self.restart()  # the undelivered notice is not resent after a restart; the worker was never told
        self.terminal = self.wired_terminal()
        self.assert_fence_refusal(self.run_command("echo after-restart"), task_id)
        done = self.run_command("echo after-restart", wait=30)
        self.assertEqual((done.get("status"), done.get("exit_code")), ("exited", 0), done)
        begun = [r for r in self.records("terminal_started") if r["command_id"] == done["command_id"]][0]
        self.assertEqual(begun.get("after_cancel_of"), task_id)


if __name__ == "__main__":
    unittest.main()
