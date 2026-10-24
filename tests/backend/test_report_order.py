"""CS-O02 (FU-D01/FU-D02): worker->manager reports of a Task reach the manager in send order.

Incident E-08: a 73% progress report reached the manager before the 42% one, because a deferred (backoff) older
report was overtaken by a newer one in the outbox. Seam: the real TaskFlow inside the real HandoffService with a
fake mailbox that can defer chosen messages (no OMP, no provider); the HandoffService alone with its placeholder
policy for the stream cases.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from workbench.backend.flow import ActiveTask
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, MailboxStatus

import test_handoff_service as hs
import test_task_flow as fx


class DeferringMailbox(fx.FakeMailbox):
    """Answers DEFERRED (nothing submitted) for a message whose payload ``message`` is in ``held`` until released."""

    def __init__(self):
        super().__init__()
        self.held: set[str] = set()
        self.deferrals: dict[str, int] = {}
        self.order: list[str] = []  # payload messages in delivery order
        self.lock = threading.Lock()

    def deliver(self, message, *, timeout=20):
        text = message.payload.get("message")
        with self.lock:
            if text in self.held:
                self.deferrals[text] = self.deferrals.get(text, 0) + 1
                return DeliveryReceipt(message.message_id, None, message.target_role, message.session_id, 1,
                                       MailboxStatus.DEFERRED, {"reason": "omp_busy"})
            self.order.append(text)
        return super().deliver(message, timeout=timeout)

    def release(self, text):
        with self.lock:
            self.held.discard(text)

    def deferred(self, text):
        with self.lock:
            return self.deferrals.get(text, 0)


class ReportFixture(fx.FlowFixture):
    def setUp(self):
        self.tmp_mailbox = DeferringMailbox()
        super().setUp()

    def open(self, *, start=True):
        self.mailbox = self.tmp_mailbox
        return super().open(start=start)

    def work(self):
        result = self.running_work()
        return result["task_id"]

    def manager_reports(self):
        return [m for m in self.mailbox.to(ActorRole.MANAGER) if m.kind is MessageKind.REPORT]

    def defer_until_cycled(self, text, times=2):
        self.assertTrue(fx.wait_until(lambda: self.mailbox.deferred(text) >= times), "the lane retried it")


class TaskFifoTests(ReportFixture):
    def test_a_newer_report_waits_for_a_deferred_older_report_of_the_same_task(self):
        task_id = self.work()
        self.mailbox.held.add("42%")
        self.assertEqual(self.to_manager({"kind": "progress", "message": "42%", "task_id": task_id})["status"],
                         "queued")
        self.defer_until_cycled("42%")
        self.assertEqual(self.to_manager({"kind": "progress", "message": "73%", "task_id": task_id})["status"],
                         "queued")
        seen = self.mailbox.deferred("42%")
        self.defer_until_cycled("42%", seen + 3)  # the lane ran several times since 73% was queued
        self.assertNotIn("73%", self.mailbox.order, "73% overtook the deferred 42% report")
        self.mailbox.release("42%")
        self.assertTrue(fx.wait_until(lambda: "73%" in self.mailbox.order))
        self.assertEqual([t for t in self.mailbox.order if t in ("42%", "73%")], ["42%", "73%"])

    def test_a_deferred_report_does_not_hold_manager_messages_or_workbench_notices(self):
        task_id = self.work()
        self.mailbox.held.add("42%")
        self.to_manager({"kind": "progress", "message": "42%", "task_id": task_id})
        self.defer_until_cycled("42%")
        result = self.to_worker({"kind": "work", "message": "also check the tests", "task_id": task_id})
        self.assertEqual(result["status"], "queued", result)
        self.assertTrue(fx.wait_until(lambda: "also check the tests" in self.mailbox.order))
        task = self.flow.active()
        notice = fx.flow_module.OutboundMessage(task_id, task.run_revision, task.run_id, ActorRole.WORKER,
                                                ActorRole.MANAGER, MessageKind.REPORT,
                                                {"notice": "workbench_notice", "message": "a Workbench notice"},
                                                in_reply_to_message_id=task.task_message_id)
        self.assertEqual(self.service.enqueue(notice, origin="test-notice")["status"], "queued")
        self.assertTrue(fx.wait_until(lambda: "a Workbench notice" in self.mailbox.order))
        self.assertNotIn("42%", self.mailbox.order)
        self.mailbox.release("42%")
        self.assertTrue(fx.wait_until(lambda: "42%" in self.mailbox.order))


class ReportSeqTests(ReportFixture):
    def test_each_report_carries_a_per_task_report_seq_from_one(self):
        task_id = self.work()
        for text in ("10%", "42%", "73%"):
            self.assertEqual(self.to_manager({"kind": "progress", "message": text, "task_id": task_id})["status"],
                             "queued")
        self.assertTrue(fx.wait_until(lambda: len(self.manager_reports()) == 3))
        self.assertEqual([(m.payload["message"], m.payload.get("report_seq")) for m in self.manager_reports()],
                         [("10%", 1), ("42%", 2), ("73%", 3)])

    def test_an_answer_is_in_the_same_order_and_seq_as_the_reports(self):
        task_id = self.work()
        self.mailbox.held.add("42%")
        self.to_manager({"kind": "progress", "message": "42%", "task_id": task_id})
        self.defer_until_cycled("42%")
        self.assertEqual(self.to_manager({"kind": "answer", "message": "yes", "task_id": task_id,
                                          "in_reply_to": str(uuid4())})["status"], "queued")
        seen = self.mailbox.deferred("42%")
        self.defer_until_cycled("42%", seen + 2)
        self.assertNotIn("yes", self.mailbox.order)
        self.mailbox.release("42%")
        self.assertTrue(fx.wait_until(lambda: "yes" in self.mailbox.order))
        sent = {m.payload["message"]: m.payload.get("report_seq") for m in self.mailbox.to(ActorRole.MANAGER)}
        self.assertEqual((sent["42%"], sent["yes"]), (1, 2))

    def test_a_new_task_starts_again_from_one(self):
        first = self.work()
        self.to_manager({"kind": "progress", "message": "first 1", "task_id": first})
        self.to_manager({"kind": "blocked", "message": "first blocked", "task_id": first, "reason": "disk full"})
        self.assertTrue(fx.wait_until(lambda: self.flow.task_view()["status"] == "blocked"))
        second = self.work()
        self.assertNotEqual(first, second)
        self.to_manager({"kind": "progress", "message": "second 1", "task_id": second})
        self.assertTrue(fx.wait_until(lambda: len(self.manager_reports()) == 3))
        self.assertEqual([(m.task_id, m.payload.get("report_seq")) for m in self.manager_reports()],
                         [(first, 1), (first, 2), (second, 1)])

    def test_the_report_seq_continues_after_a_backend_restart(self):
        task_id = self.work()
        self.to_manager({"kind": "progress", "message": "before 1", "task_id": task_id})
        self.to_manager({"kind": "progress", "message": "before 2", "task_id": task_id})
        self.assertTrue(fx.wait_until(lambda: len(self.manager_reports()) == 2))
        self.restart()
        self.assertEqual(self.flow.active().task_id, task_id, "the work Task with a run survives the restart")
        self.assertEqual(self.to_manager({"kind": "progress", "message": "after", "task_id": task_id})["status"],
                         "queued")
        self.assertTrue(fx.wait_until(lambda: len(self.manager_reports()) == 3))
        self.assertEqual([m.payload.get("report_seq") for m in self.manager_reports()], [1, 2, 3])


class FinalReportTests(ReportFixture):
    """FU-D02: no progress/report after the run's done/blocked was queued (before its acceptance ends the run)."""

    def assert_final(self, result, task_id):
        self.assertEqual((result["status"], result.get("reason")), ("rejected", "run_report_final"), result)
        self.assertEqual(result.get("task_id"), task_id)
        self.assertIn("not sent", result.get("detail", ""))

    def test_progress_or_report_after_a_queued_done_is_refused_and_the_done_still_ends_the_run(self):
        task_id = self.work()
        self.mailbox.held.add("all done")
        self.assertEqual(self.to_manager({"kind": "done", "message": "all done", "task_id": task_id})["status"],
                         "queued")
        self.defer_until_cycled("all done")
        for kind in ("progress", "report"):
            self.assert_final(self.to_manager({"kind": kind, "message": f"late {kind}", "task_id": task_id}),
                              task_id)
        self.mailbox.release("all done")
        self.assertTrue(fx.wait_until(lambda: self.flow.task_view()["status"] == "closed"))
        self.assertEqual(self.flow.task_view()["closed_reason"], "done")
        self.assertEqual([m.payload["message"] for m in self.manager_reports()], ["all done"])

    def test_progress_after_a_blocked_is_refused_until_the_manager_follows_up(self):
        task_id = self.work()
        self.assertEqual(self.to_manager({"kind": "blocked", "message": "stuck", "task_id": task_id,
                                          "reason": "disk full"})["status"], "queued")
        self.assert_final(self.to_manager({"kind": "progress", "message": "late", "task_id": task_id}), task_id)
        self.assertTrue(fx.wait_until(lambda: self.flow.task_view()["status"] == "blocked"))
        self.assertEqual(self.to_worker({"kind": "work", "message": "disk freed; go on", "task_id": task_id})
                         ["status"], "queued")
        self.assertEqual(self.to_manager({"kind": "progress", "message": "resumed", "task_id": task_id})["status"],
                         "queued")
        # another (new) Task's reports are not affected by the old run's blocked
        self.to_manager({"kind": "blocked", "message": "stuck again", "task_id": task_id, "reason": "disk full"})
        self.assertTrue(fx.wait_until(lambda: self.flow.task_view()["status"] == "blocked"
                                      and self.flow.task_view()["held_reason"] == "worker_blocked"))
        second = self.work()
        self.assertEqual(self.to_manager({"kind": "progress", "message": "new task", "task_id": second})["status"],
                         "queued")

    def test_an_answer_after_a_queued_done_is_not_refused(self):
        task_id = self.work()
        self.mailbox.held.add("all done")
        self.to_manager({"kind": "done", "message": "all done", "task_id": task_id})
        result = self.to_manager({"kind": "answer", "message": "yes", "task_id": task_id,
                                  "in_reply_to": str(uuid4())})
        self.assertEqual(result["status"], "queued", result)
        self.mailbox.release("all done")

    def test_a_done_held_by_a_ledger_fault_does_not_refuse_later_reports(self):
        task_id = self.work()
        with mock.patch.object(self.flow._ledger, "append", side_effect=OSError("disk full")):  # the Task save fails
            held = self.to_manager({"kind": "done", "message": "all done", "task_id": task_id})
        self.assertEqual((held["status"], held.get("reason")), ("held", "metadata_unavailable"), held)
        result = self.to_manager({"kind": "progress", "message": "still working", "task_id": task_id})
        self.assertEqual(result["status"], "queued", result)
        self.assertNotEqual(self.flow.task_view()["held_reason"], "done_report_pending", "nothing was queued")
        self.assertTrue(fx.wait_until(lambda: len(self.manager_reports()) == 1))
        # the held done used up seq 1 (a gap is allowed); the seq never goes back
        self.assertEqual(self.manager_reports()[0].payload["report_seq"], 2)


class StreamTests(unittest.TestCase):
    """The HandoffService alone (placeholder policy): another Task's reports are not held by a deferred one."""

    setUp = hs.HandoffServiceTests.setUp
    tearDown = hs.HandoffServiceTests.tearDown
    service = hs.HandoffServiceTests.service
    active_task = hs.HandoffServiceTests.active_task

    def test_a_deferred_report_of_one_task_does_not_hold_another_tasks_report(self):
        self.mailbox = DeferringHsMailbox()
        service = self.service()
        first = self.active = self.active_task()
        self.mailbox.held.add("first 42%")
        self.assertEqual(service.handle(ActorRole.WORKER, hs.request(
            "to_manager", {"kind": "progress", "message": "first 42%"}, call="a1"))["status"], "queued")
        self.assertTrue(hs.wait_until(lambda: self.mailbox.deferrals.get("first 42%", 0) >= 2))
        self.active = self.active_task()
        self.assertNotEqual(self.active.task_id, first.task_id)
        self.assertEqual(service.handle(ActorRole.WORKER, hs.request(
            "to_manager", {"kind": "progress", "message": "second 10%"}, call="b1"))["status"], "queued")
        self.assertTrue(hs.wait_until(lambda: "second 10%" in self.mailbox.order))
        self.assertNotIn("first 42%", self.mailbox.order)
        self.mailbox.held.discard("first 42%")
        self.assertTrue(hs.wait_until(lambda: "first 42%" in self.mailbox.order))


class DeferringHsMailbox(hs.FakeMailbox):
    def __init__(self):
        super().__init__()
        self.held: set[str] = set()
        self.deferrals: dict[str, int] = {}
        self.order: list[str] = []

    def deliver(self, message, *, timeout=20):
        text = message.payload.get("message")
        if text in self.held:
            self.deferrals[text] = self.deferrals.get(text, 0) + 1
            return DeliveryReceipt(message.message_id, None, message.target_role, "s", 1, MailboxStatus.DEFERRED,
                                   {"reason": "omp_busy"})
        self.order.append(text)
        return super().deliver(message, timeout=timeout)


if __name__ == "__main__":
    unittest.main()
