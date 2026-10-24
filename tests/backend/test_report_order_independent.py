"""CS-O02 (FOLLOWUPS-BRIEF FU-D01/FU-D02): independent adversarial checks of the Worker->Manager report order.

Seam: the public ``HandoffService.handle`` (the Worker's ``to_manager`` tool call) over the real ``TaskFlow`` policy, as
the Backend wires them, with a scripted mailbox that defers chosen messages the way ``current_omp_state_not_safe`` does.
A second group runs the real ``HandoffService`` with a stub policy to check the lane rules without any Task logic
(other Tasks, backend notices and the Manager->Worker stream are not held by a deferred report).

Expected values come from the contract, not from the implementation:

* messages of one Task reach the Manager in the order they were sent (``report_seq`` 1, 2, 3 ... strictly increasing in
  delivery order), also when an older one is deferred more than once, also when the deferred one is a done/blocked;
* ``report_seq`` is per Task, starts at 1 and continues across a backend restart; a refusal consumes none;
* a progress/report of a run after its done/blocked was queued is refused ``run_report_final`` (status ``rejected``)
  until the done/blocked is not queued or the manager follows up; an answer is never refused that way;
* a Task cancel withdraws the Task's pending reports, the cancel notice (other lane) is not held by them.
"""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from workbench.backend.flow import HandoffDecision, HandoffService, OutboundMessage
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, MailboxStatus

import test_task_flow
from test_task_flow import FakeMailbox, FlowFixture, request, wait_until

DEFER_DETAIL = {"reason": "current_omp_state_not_safe", "blockers": ["busy"]}


class ScriptMailbox(FakeMailbox):
    """A mailbox whose ``deliver`` defers the messages ``defer_if`` names and records every attempt in order."""

    def __init__(self):
        super().__init__()
        self.mutex = threading.Lock()
        self.defer_if = lambda message: False
        self.attempts: list[tuple[str, str | None, object, str]] = []  # (target, kind, report_seq, outcome)
        self.accepted: list[SimpleNamespace] = []

    def deliver(self, message, *, timeout=20, on_submitted=None):
        deferred = bool(self.defer_if(message))
        with self.mutex:
            payload = message.payload
            self.attempts.append((message.target_role.value, payload.get("kind"), payload.get("report_seq"),
                                  "deferred" if deferred else "ok"))
            if not deferred:
                self.accepted.append(message)
                self.delivered.append(message.message_id)
        status = MailboxStatus.DEFERRED if deferred else MailboxStatus.OMP_PROCESSED
        if not deferred and on_submitted is not None:
            on_submitted()
        return DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, message.session_id, 1, status,
                               DEFER_DETAIL if deferred else {})

    def manager_seqs(self, task_id=None):
        with self.mutex:
            return [m.payload["report_seq"] for m in self.accepted if m.target_role is ActorRole.MANAGER
                    and "report_seq" in m.payload and (task_id is None or m.task_id == task_id)]

    def manager_kinds(self):
        with self.mutex:
            return [m.payload.get("kind") for m in self.accepted if m.target_role is ActorRole.MANAGER
                    and "report_seq" in m.payload]

    def manager_attempts(self):
        with self.mutex:
            return [item for item in self.attempts if item[0] == "manager" and item[2] is not None]


class Holds:
    """``defer_if`` for report_seq values: ``count`` deferrals each, or ``until`` an Event for the given seqs."""

    def __init__(self):
        self.counts: dict[int, int] = {}
        self.events: dict[int, threading.Event] = {}
        self.lock = threading.Lock()

    def __call__(self, message):
        seq = message.payload.get("report_seq")
        if message.target_role is not ActorRole.MANAGER or seq is None:
            return False
        with self.lock:
            event = self.events.get(seq)
            if event is not None and not event.is_set():
                return True
            if self.counts.get(seq, 0) > 0:
                self.counts[seq] -= 1
                return True
        return False

    def hold(self, *seqs):
        for seq in seqs:
            self.events[seq] = threading.Event()

    def release(self, *seqs):
        for seq in (seqs or tuple(self.events)):
            self.events[seq].set()


class OrderFixture(FlowFixture):
    def setUp(self):
        with mock.patch.object(test_task_flow, "FakeMailbox", ScriptMailbox):
            super().setUp()
        self.holds = Holds()
        self.mailbox.defer_if = self.holds

    def tearDown(self):
        self.holds.release()  # a report held back on a failing test must not keep the lane open
        super().tearDown()

    def queued(self, kind, message="m", **extra):
        result = self.to_manager({"kind": kind, "message": message, **extra})
        self.assertEqual(result["status"], "queued", result)
        return result

    def accepted(self, count, timeout=10.0):
        self.assertTrue(wait_until(lambda: len(self.mailbox.manager_seqs()) >= count, timeout),
                        f"{count} report(s) delivered; saw {self.mailbox.manager_attempts()}")

    def assert_send_order(self):
        """Delivery order == seq order and no later report was tried before an earlier one was accepted."""
        ok_at: dict[object, int] = {}
        first_at: dict[object, int] = {}
        for index, (_target, _kind, seq, outcome) in enumerate(self.mailbox.manager_attempts()):
            first_at.setdefault(seq, index)
            if outcome == "ok":
                ok_at[seq] = index
        for seq, ok in ok_at.items():
            for later in first_at:
                if later > seq:
                    self.assertGreater(first_at[later], ok, f"report_seq {later} was tried before {seq} was "
                                                            f"delivered: {self.mailbox.manager_attempts()}")
        seqs = self.mailbox.manager_seqs()
        self.assertEqual(seqs, sorted(seqs), "delivered out of order")
        self.assertEqual(len(seqs), len(set(seqs)), "a report_seq was delivered twice")


class SendOrderTests(OrderFixture):
    def test_three_reports_with_the_first_deferred_five_times_arrive_in_send_order(self):
        self.running_work()
        self.holds.counts[1] = 5
        self.queued("progress", "p1")
        self.queued("report", "p2")
        self.queued("progress", "p3")
        self.accepted(3)
        self.assertEqual(self.mailbox.manager_seqs(), [1, 2, 3])
        self.assertEqual(self.mailbox.manager_kinds(), ["progress", "report", "progress"])
        deferred = [a for a in self.mailbox.manager_attempts() if a[3] == "deferred"]
        self.assertEqual(len(deferred), 5, "the test really deferred the first report")
        self.assertTrue(all(a[2] == 1 for a in deferred), "nothing else was tried while the first waited")
        self.assert_send_order()

    def check_pattern(self, counts):
        self.running_work()
        self.holds.counts = dict(counts)
        for number in range(4):
            self.queued(("progress", "report")[number % 2], f"p{number + 1}")
        self.accepted(4)
        self.assertEqual(self.mailbox.manager_seqs(), [1, 2, 3, 4])
        self.assertEqual(sum(1 for a in self.mailbox.manager_attempts() if a[3] == "deferred"), sum(counts.values()))
        self.assert_send_order()

    def test_the_first_of_four_deferred(self):
        self.check_pattern({1: 3})

    def test_the_middle_of_four_deferred(self):
        self.check_pattern({2: 4})

    def test_the_last_of_four_deferred(self):
        self.check_pattern({4: 3})

    def test_the_first_and_the_third_deferred(self):
        self.check_pattern({1: 2, 3: 3})

    def test_all_but_one_deferred(self):
        self.check_pattern({1: 2, 2: 2, 4: 2})

    def test_a_deferred_done_waits_behind_a_deferred_progress_and_is_delivered_second(self):
        work = self.running_work()
        self.holds.counts.update({1: 3, 2: 3})
        self.queued("progress", "p1")
        self.queued("done", "all finished")
        self.assertTrue(wait_until(lambda: self.mailbox.manager_seqs() == [1, 2], 10), self.mailbox.manager_attempts())
        self.assertEqual(self.mailbox.manager_kinds(), ["progress", "done"])
        self.assert_send_order()
        self.assertTrue(wait_until(lambda: self.flow.tasks[work["task_id"]].status == "closed"))
        self.assertEqual(self.flow.tasks[work["task_id"]].closed_reason, "done")

    def test_a_progress_that_is_held_back_cannot_be_overtaken_by_an_answer_of_the_same_task(self):
        self.running_work()
        self.holds.counts[1] = 4
        self.queued("progress", "p1")
        self.queued("answer", "a2", in_reply_to=str(uuid4()))
        self.queued("report", "p3")
        self.accepted(3)
        self.assertEqual(self.mailbox.manager_kinds(), ["progress", "answer", "report"])
        self.assertEqual(self.mailbox.manager_seqs(), [1, 2, 3], "an answer shares the Task's report_seq")
        self.assert_send_order()

    def test_every_to_manager_kind_carries_report_seq_and_nothing_else_does(self):
        self.running_work()
        for kind, extra in (("progress", {}), ("report", {}), ("answer", {"in_reply_to": str(uuid4())}),
                            ("blocked", {"reason": "needs input"}), ):
            self.queued(kind, kind, **extra)
        self.accepted(4)
        with self.mailbox.mutex:
            to_manager = [m for m in self.mailbox.accepted if m.target_role is ActorRole.MANAGER]
            to_worker = [m for m in self.mailbox.accepted if m.target_role is ActorRole.WORKER]
        self.assertEqual([m.payload.get("report_seq") for m in to_manager], [1, 2, 3, 4])
        self.assertTrue(to_worker and all("report_seq" not in m.payload for m in to_worker),
                        "the TASK message to the worker carries no report_seq")

    def test_concurrent_reports_get_distinct_consecutive_seqs_and_arrive_in_that_order(self):
        self.running_work()
        self.holds.counts.update({3: 2, 7: 2, 11: 3, 14: 1})
        errors, results = [], []

        def worker(index):
            try:
                for number in range(5):
                    results.append(self.to_manager({"kind": "progress", "message": f"t{index}-{number}"},
                                                   call=f"c-{index}-{number}"))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
        threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual([r["status"] for r in results], ["queued"] * 20, results)
        self.accepted(20, timeout=30)
        self.assertEqual(self.mailbox.manager_seqs(), list(range(1, 21)))
        self.assert_send_order()


class RefusalTests(OrderFixture):
    def hold_final(self, kind):
        """A running Task whose ``kind`` (done/blocked) is queued and held back (never delivered)."""
        work = self.running_work()
        self.queued("progress", "p1")
        self.accepted(1)
        self.holds.hold(2)
        final = self.queued(kind, f"{kind} report")
        self.assertTrue(wait_until(lambda: any(a[2] == 2 for a in self.mailbox.manager_attempts())),
                        "the final report was tried (and deferred)")
        return work, final

    def check_refused(self, kind):
        work, _ = self.hold_final(kind)
        before = (len(self.service.outbox_snapshot()), len(self.mailbox.manager_attempts()),
                  self.flow.tasks[work["task_id"]].report_seq)
        for refused_kind in ("progress", "report", "progress"):
            result = self.to_manager({"kind": refused_kind, "message": "too late"})
            self.assertEqual((result["status"], result["reason"]), ("rejected", "run_report_final"), result)
            self.assertEqual(result["task_id"], work["task_id"])
            self.assertTrue(result.get("detail"))
        tagged = self.to_manager({"kind": "report", "message": "tagged", "task_id": work["task_id"]})
        self.assertEqual(tagged["reason"], "run_report_final", "the explicit task_id changes nothing")
        self.assertEqual((len(self.service.outbox_snapshot()), self.flow.tasks[work["task_id"]].report_seq),
                         (before[0], before[2]), "a refusal queues nothing and consumes no report_seq")
        answer = self.queued("answer", "still allowed", in_reply_to=str(uuid4()))
        self.assertEqual(answer["status"], "queued", answer)
        self.assertEqual(self.flow.tasks[work["task_id"]].report_seq, 3, "the answer took the next seq")
        self.holds.release()
        self.accepted(3)
        self.assertEqual(self.mailbox.manager_seqs(), [1, 2, 3])
        self.assertEqual(self.mailbox.manager_kinds(), ["progress", kind, "answer"])
        self.assert_send_order()

    def test_progress_and_report_are_refused_once_done_is_queued(self):
        self.check_refused("done")

    def test_progress_and_report_are_refused_once_blocked_is_queued(self):
        self.check_refused("blocked")

    def test_a_refusal_is_the_same_while_the_final_is_being_retried_and_after_blocked_was_delivered(self):
        work, _ = self.hold_final("blocked")
        self.assertEqual(self.to_manager({"kind": "progress", "message": "x"})["reason"], "run_report_final")
        self.holds.release()
        self.accepted(2)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "blocked"))
        result = self.to_manager({"kind": "progress", "message": "after blocked was accepted"})
        self.assertEqual((result["status"], result["reason"]), ("rejected", "run_report_final"), result)
        self.assertEqual(self.flow.tasks[work["task_id"]].report_seq, 2)

    def test_after_done_was_delivered_the_task_is_closed_and_the_old_reason_applies(self):
        self.hold_final("done")
        self.holds.release()
        self.accepted(2)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "closed"))
        self.assertEqual(self.to_manager({"kind": "progress", "message": "late"})["reason"], "no_active_task")

    def test_a_manager_follow_up_resumes_the_run_and_reports_flow_again_with_continuing_seq(self):
        work, _ = self.hold_final("blocked")
        self.holds.release()
        self.accepted(2)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "blocked"))
        run_id = self.flow.task_view()["run_id"]
        resumed = self.to_worker({"kind": "work", "message": "the schema is in src/parser/schema.py",
                                  "task_id": work["task_id"]})
        self.assertEqual(resumed["status"], "queued", resumed)
        self.assertEqual(self.flow.task_view()["run_id"], run_id, "the same run resumes")
        self.queued("progress", "back at work")
        self.queued("done", "now done")
        self.accepted(4)
        self.assertEqual(self.mailbox.manager_seqs(), [1, 2, 3, 4])
        self.assertEqual(self.mailbox.manager_kinds(), ["progress", "blocked", "progress", "done"])
        self.assert_send_order()

    def test_a_second_blocked_after_a_follow_up_closes_the_gate_again(self):
        work, _ = self.hold_final("blocked")
        self.holds.release()
        self.accepted(2)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "blocked"))
        self.to_worker({"kind": "work", "message": "more info", "task_id": work["task_id"]})
        self.queued("blocked", "blocked again")
        self.assertEqual(self.to_manager({"kind": "progress", "message": "x"})["reason"], "run_report_final")

    def test_a_done_that_was_not_queued_leaves_the_run_open_for_reports(self):
        work = self.running_work()
        with mock.patch.object(self.service, "_mailbox", None):  # nothing can be queued: held, nothing is sent
            done = self.to_manager({"kind": "done", "message": "finished"})
            self.assertNotEqual(done.get("status"), "queued", done)
            progress = self.to_manager({"kind": "progress", "message": "still going"})
            self.assertNotEqual(progress.get("reason"), "run_report_final",
                                "a done that never went out does not close the run for progress")
        self.queued("progress", "p after the failure")
        self.accepted(1)
        seqs = self.mailbox.manager_seqs()
        self.assertEqual(len(seqs), 1)
        self.assertGreater(seqs[0], 0)
        self.assertEqual(self.flow.tasks[work["task_id"]].report_seq, seqs[0], "report_seq never goes backwards")

    def test_a_done_queued_while_no_manager_is_connected_is_queued_and_gates_progress(self):
        service = self.service
        service._peer_lookup = lambda role: None if role is ActorRole.MANAGER else SimpleNamespace(
            session_id=test_task_flow.WORKER_SESSION, generation=1)
        self.running_work()
        done = self.to_manager({"kind": "done", "message": "finished while nobody listens"})
        self.assertEqual(done["status"], "queued", done)
        self.assertEqual(self.to_manager({"kind": "progress", "message": "x"})["reason"], "run_report_final")

    def test_replaying_a_tool_call_neither_queues_again_nor_takes_a_seq(self):
        work = self.running_work()
        first = self.to_manager({"kind": "progress", "message": "p"}, call="same-call")
        again = self.to_manager({"kind": "progress", "message": "p"}, call="same-call")
        self.assertEqual(first, again)
        self.accepted(1)
        time.sleep(0.2)
        self.assertEqual(self.mailbox.manager_seqs(), [1])
        self.assertEqual(self.flow.tasks[work["task_id"]].report_seq, 1)

    def test_a_pause_holds_every_report_without_taking_a_seq_and_the_first_after_the_resume_is_one(self):
        work = self.running_work()
        self.controller.automation["state"] = "paused"
        for kind in ("progress", "done"):
            held = self.to_manager({"kind": kind, "message": f"{kind} while paused"})
            self.assertEqual((held["status"], held["reason"]), ("held", "paused"), held)
        self.assertEqual(self.flow.tasks[work["task_id"]].report_seq, 0, "a held report takes no report_seq")
        self.controller.automation["state"] = "running"
        self.assertEqual(self.to_manager({"kind": "progress", "message": "after the resume"})["status"], "queued")
        self.accepted(1)
        self.assertEqual(self.mailbox.manager_seqs(), [1])


class SeqPersistenceTests(OrderFixture):
    def test_report_seq_is_per_task_and_each_task_starts_at_one(self):
        first = self.running_work("first change")
        self.queued("progress", "a1")
        self.queued("progress", "a2")
        self.queued("done", "a done")
        self.accepted(3)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "closed"))
        second = self.running_work("second change")
        self.assertNotEqual(first["task_id"], second["task_id"])
        before = len(self.mailbox.manager_seqs())
        self.queued("progress", "b1")
        self.queued("blocked", "b2")
        self.assertTrue(wait_until(lambda: len(self.mailbox.manager_seqs()) >= before + 2))
        self.assertEqual(self.mailbox.manager_seqs(first["task_id"]), [1, 2, 3])
        self.assertEqual(self.mailbox.manager_seqs(second["task_id"]), [1, 2])
        self.assertEqual((self.flow.tasks[first["task_id"]].report_seq, self.flow.tasks[second["task_id"]].report_seq),
                         (3, 2))

    def test_a_new_task_does_not_inherit_the_seq_of_a_task_it_supersedes_while_blocked(self):
        old = self.running_work("old")
        self.queued("progress", "o1")
        self.queued("blocked", "stuck")
        self.accepted(2)
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "blocked"))
        new = self.running_work("new")
        self.queued("progress", "n1")
        self.assertTrue(wait_until(lambda: self.mailbox.manager_seqs(new["task_id"]) == [1]))
        self.assertEqual(self.flow.tasks[old["task_id"]].report_seq, 2)

    def test_report_seq_continues_after_a_backend_restart(self):
        work = self.running_work()
        self.queued("progress", "p1")
        self.queued("progress", "p2")
        self.accepted(2)
        self.restart()
        self.assertEqual(self.flow.tasks[work["task_id"]].report_seq, 2, "kept in the Task ledger")
        self.queued("report", "p3", task_id=work["task_id"])
        self.assertTrue(wait_until(lambda: self.mailbox.manager_seqs() == [1, 2, 3], 10),
                        self.mailbox.manager_attempts())

    def test_a_seq_issued_to_a_message_the_restart_lost_is_not_reused(self):
        work = self.running_work()
        self.queued("progress", "p1")
        self.accepted(1)
        self.holds.hold(2, 3)
        self.queued("progress", "p2-lost")
        self.queued("progress", "p3-lost")
        self.assertTrue(wait_until(lambda: any(a[2] == 2 for a in self.mailbox.manager_attempts())))
        self.restart()  # the memory-only outbox drops both
        self.holds.release()
        self.queued("progress", "p4", task_id=work["task_id"])
        self.assertTrue(wait_until(lambda: len(self.mailbox.manager_seqs()) >= 2, 10), self.mailbox.manager_attempts())
        seqs = self.mailbox.manager_seqs()
        self.assertEqual(seqs[0], 1)
        self.assertGreater(seqs[-1], 3, f"{seqs}: a lost report's seq is never given again")
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_a_done_the_restart_lost_does_not_keep_the_run_closed_for_reports(self):
        work = self.running_work()
        self.holds.hold(1)
        self.queued("done", "finished")
        self.assertTrue(wait_until(lambda: any(a[2] == 1 for a in self.mailbox.manager_attempts())))
        self.assertEqual(self.to_manager({"kind": "progress", "message": "x"})["reason"], "run_report_final")
        self.restart()
        self.holds.release()
        result = self.to_manager({"kind": "progress", "message": "after restart"}, call="after-restart")
        self.assertEqual(result["status"], "queued", result)
        self.assertTrue(wait_until(lambda: self.mailbox.manager_seqs(work["task_id"]) == [2], 10),
                        self.mailbox.manager_attempts())


class CancelMidQueueTests(OrderFixture):
    def test_a_cancel_withdraws_the_pending_reports_and_the_notice_is_not_held_behind_them(self):
        work = self.running_work()
        self.holds.hold(1)
        for number in range(3):
            self.queued("progress", f"p{number + 1}")
        self.assertTrue(wait_until(lambda: any(a[2] == 1 for a in self.mailbox.manager_attempts())))
        notices_before = len(self.mailbox.to(ActorRole.WORKER))
        cancelled = self.to_worker({"kind": "work", "message": "stop it", "cancel": True, "task_id": work["task_id"]})
        self.assertEqual(cancelled["status"], "cancelled", cancelled)
        self.assertTrue(wait_until(lambda: len(self.mailbox.to(ActorRole.WORKER)) == notices_before + 1, 10),
                        "the cancel notice (Manager->Worker) is not held by the deferred report")
        self.assertTrue(wait_until(lambda: self.mailbox.to(ActorRole.WORKER)[-1].payload.get("cancel") is True))
        manager_states = [e["state"] for e in self.service.outbox_snapshot() if e["target_role"] == "manager"]
        self.assertEqual(manager_states, ["withdrawn"] * 3, "every pending report of the cancelled Task is dropped")
        self.holds.release()
        time.sleep(0.3)
        self.assertEqual(self.mailbox.manager_seqs(), [], "nothing of the cancelled Task reaches the Manager")
        self.assertEqual(self.to_manager({"kind": "progress", "message": "late"})["reason"], "no_active_task")

    def test_a_cancel_after_the_first_report_was_delivered_withdraws_only_the_rest(self):
        work = self.running_work()
        self.queued("progress", "p1")
        self.accepted(1)
        self.holds.hold(2)
        self.queued("progress", "p2")
        self.queued("progress", "p3")
        self.assertTrue(wait_until(lambda: any(a[2] == 2 for a in self.mailbox.manager_attempts())))
        self.to_worker({"kind": "work", "message": "stop", "cancel": True, "task_id": work["task_id"]})
        self.holds.release()
        time.sleep(0.3)
        self.assertEqual(self.mailbox.manager_seqs(), [1])


# ---------------------------------------------------------------------------------------------------------------
# The lane rules without Task logic: a stub policy hands the real HandoffService messages of chosen Tasks.
# ---------------------------------------------------------------------------------------------------------------
TASKS = {"A": str(uuid4()), "B": str(uuid4())}
RUN = str(uuid4())


class StubPolicy:
    """``to_manager`` messages tagged ``A:`` or ``B:`` belong to Task A or B; ``report_seq`` is the message's own."""

    def decide(self, request, active):
        text = request.args["message"]
        task = text.split(":")[0]
        payload = {"handoff": "to_manager", "kind": request.args["kind"], "message": text, "report_seq": int(text.split(":")[1])}
        return HandoffDecision({"status": "queued"}, message=OutboundMessage(
            TASKS[task], 1, RUN, ActorRole.WORKER, ActorRole.MANAGER, MessageKind.REPORT, payload))


class LaneTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory(prefix="cso2-lane-", dir=tempfile.gettempdir())
        self.addCleanup(self.tmp.cleanup)
        self.mailbox = ScriptMailbox()
        self.holds: dict[str, threading.Event] = {}

        def defer(message):
            text = message.payload.get("message", "")
            event = self.holds.get(text)
            return event is not None and not event.is_set()
        self.mailbox.defer_if = defer
        self.service = HandoffService(Path(self.tmp.name) / "handoffs.jsonl", mailbox=self.mailbox,
                                      policy=StubPolicy(), retry_interval=0.01)
        self.service.start()
        self.addCleanup(self.service.close)
        self.addCleanup(self.release_all)
        self.calls = 0

    def release_all(self):
        for event in self.holds.values():
            event.set()

    def send(self, text, kind="progress"):
        self.calls += 1
        result = self.service.handle(ActorRole.WORKER, request("to_manager", {"kind": kind, "message": text},
                                                               f"lane-{self.calls}"))
        self.assertEqual(result["status"], "queued", result)

    def delivered_texts(self):
        with self.mailbox.mutex:
            return [m.payload.get("message") for m in self.mailbox.accepted if m.target_role is ActorRole.MANAGER]

    def hold(self, text):
        self.holds[text] = threading.Event()

    def test_another_task_is_not_held_by_a_deferred_report(self):
        self.hold("A:1")
        self.send("A:1")
        self.send("A:2")
        self.send("B:1")
        self.send("B:2")
        self.assertTrue(wait_until(lambda: self.delivered_texts() == ["B:1", "B:2"], 5), self.delivered_texts())
        self.assertEqual(self.delivered_texts(), ["B:1", "B:2"], "A:2 still waits behind A:1")
        self.holds["A:1"].set()
        self.assertTrue(wait_until(lambda: len(self.delivered_texts()) == 4, 5))
        self.assertEqual([t for t in self.delivered_texts() if t.startswith("A")], ["A:1", "A:2"])
        self.assertEqual([t for t in self.delivered_texts() if t.startswith("B")], ["B:1", "B:2"])

    def test_both_tasks_deferred_each_keeps_its_own_order(self):
        self.hold("A:1")
        self.hold("B:1")
        for text in ("A:1", "B:1", "A:2", "B:2", "A:3", "B:3"):
            self.send(text)
        time.sleep(0.2)
        self.assertEqual(self.delivered_texts(), [])
        self.holds["B:1"].set()
        self.assertTrue(wait_until(lambda: self.delivered_texts() == ["B:1", "B:2", "B:3"], 5), self.delivered_texts())
        self.holds["A:1"].set()
        self.assertTrue(wait_until(lambda: len(self.delivered_texts()) == 6, 5))
        self.assertEqual([t for t in self.delivered_texts() if t.startswith("A")], ["A:1", "A:2", "A:3"])

    def notice(self, target, text):
        payload = {"handoff": "to_manager" if target is ActorRole.MANAGER else "to_worker", "notice": "n",
                   "message": text}
        sender, receiver = ((ActorRole.WORKER, ActorRole.MANAGER) if target is ActorRole.MANAGER
                            else (ActorRole.MANAGER, ActorRole.WORKER))
        return self.service.enqueue(OutboundMessage(TASKS["A"], 1, RUN, sender, receiver, MessageKind.REPORT, payload),
                                    origin=f"test:{text}")

    def test_a_backend_notice_to_the_manager_and_a_message_to_the_worker_are_not_held(self):
        self.hold("A:1")
        self.send("A:1")
        self.send("A:2")
        self.assertEqual(self.notice(ActorRole.MANAGER, "notice-to-manager")["status"], "queued")
        self.assertEqual(self.notice(ActorRole.WORKER, "message-to-worker")["status"], "queued")
        self.assertTrue(wait_until(lambda: "notice-to-manager" in self.delivered_texts(), 5),
                        "a backend notice is another stream")
        def to_worker():
            with self.mailbox.mutex:
                return [m.payload.get("message") for m in self.mailbox.accepted if m.target_role is ActorRole.WORKER]
        self.assertTrue(wait_until(lambda: to_worker() == ["message-to-worker"], 5), to_worker())
        self.assertEqual([t for t in self.delivered_texts() if t.startswith("A:")], [])
        self.holds["A:1"].set()
        self.assertTrue(wait_until(lambda: [t for t in self.delivered_texts() if t.startswith("A:")] == ["A:1", "A:2"],
                                   5))

    def test_a_withdrawn_head_does_not_block_the_reports_behind_it_of_another_task_or_its_own_end(self):
        self.hold("A:1")
        self.send("A:1")
        self.send("A:2")
        self.send("B:1")
        self.assertTrue(wait_until(lambda: self.delivered_texts() == ["B:1"], 5))
        dropped = self.service.withdraw(TASKS["A"])
        self.assertEqual(dropped, ["report", "report"])
        self.holds["A:1"].set()
        time.sleep(0.2)
        self.assertEqual(self.delivered_texts(), ["B:1"])
        self.send("B:2")
        self.assertTrue(wait_until(lambda: self.delivered_texts() == ["B:1", "B:2"], 5))


if __name__ == "__main__":
    unittest.main()
