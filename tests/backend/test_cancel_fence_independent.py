"""CS-O01 (FOLLOWUPS-BRIEF FU-D03..05): independent adversarial checks of the cancel fence.

Two seams, both public:

* ``TaskFlow`` (the policy the Backend wires into ``HandoffService``): ``take_cancel_fence()`` and
  ``after_cancel_of()``, the two calls the terminal service gets, with a scripted mailbox that decides what happens to
  the cancel notice (acknowledged, deferred, held in an attempt, unknown, rejected). Expectations: the fence is armed by
  the Manager's cancel of a free-work Task that queued a cancel notice; it ends with the notice reaching the worker
  (``submitted``/``delivered``/``unknown``), with one refusal, or with a newer Task; an attempt in progress refuses
  nothing and a deferred attempt arms it again; ``not_needed`` and experiment cancels never arm it; it is kept with the
  Task across a backend restart (the memory-only outbox does not resend the notice).
* a real ``Backend`` (fake OMPs, real host shell) through ``Backend._tool_request``: the Worker's ``terminal`` tool
  refuses the first task-less command once with ``task_cancelled`` and types nothing, status-only calls and ``stop`` are
  not fenced, later task-less commands carry ``after_cancel_of`` in ``terminal_request``/``terminal_started`` with
  ``task_id`` null and are not in the cancelled Task's host commands, a newer Task ends it.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from workbench.contracts.v1 import ActorRole
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, MailboxStatus

import test_task_flow
from test_cd70_restart_worker import RestartWorkerFixture
from test_task_flow import FakeMailbox, FlowFixture, wait_until


class NoticeMailbox(FakeMailbox):
    """Delivers everything at once except the cancel notice, whose fate ``mode`` decides.

    modes: ``ok`` (submitted, processed), ``defer`` (deferred, not submitted), ``block`` (the attempt waits on
    ``gate`` before it ends deferred, or acknowledges first when ``submit_first``), ``unknown`` (not submitted, outcome
    unknown), ``rejected`` (never submitted).
    """

    def __init__(self):
        super().__init__()
        self.mode = "ok"
        self.submit_first = False
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.mutex = threading.Lock()
        self.notice_attempts: list[str] = []
        self.defer_tasks = False  # also defer the TASK message (the worker never gets it)

    def deliver(self, message, *, timeout=20, on_submitted=None):
        payload = message.payload
        notice = payload.get("cancel") is True
        if not notice:
            if self.defer_tasks and payload.get("handoff") == "to_worker":
                return self.receipt(message, MailboxStatus.DEFERRED, {"reason": "current_omp_state_not_safe",
                                                                       "blockers": ["busy"]})
            self.delivered.append(message.message_id)
            if on_submitted is not None and message.target_role is ActorRole.WORKER:
                on_submitted()
            return self.receipt(message, MailboxStatus.OMP_PROCESSED, {})
        with self.mutex:
            self.notice_attempts.append(self.mode)
            mode = self.mode
        if mode == "block":
            self.entered.set()
            if self.submit_first and on_submitted is not None:
                on_submitted()
            self.gate.wait(10)
            if self.submit_first:
                self.delivered.append(message.message_id)
                return self.receipt(message, MailboxStatus.OMP_PROCESSED, {})
            return self.receipt(message, MailboxStatus.DEFERRED, {"reason": "current_omp_state_not_safe",
                                                                   "blockers": ["busy"]})
        if mode == "defer":
            return self.receipt(message, MailboxStatus.DEFERRED, {"reason": "current_omp_state_not_safe",
                                                                   "blockers": ["busy"]})
        if mode == "unknown":
            return self.receipt(message, MailboxStatus.UNKNOWN, {"reason": "BridgeTimeout", "stage": "delivery"})
        if mode == "rejected":
            return self.receipt(message, MailboxStatus.REJECTED, {"reason": "NoSuchTarget"})
        if on_submitted is not None:
            on_submitted()
        self.delivered.append(message.message_id)
        return self.receipt(message, MailboxStatus.OMP_PROCESSED, {})

    @staticmethod
    def receipt(message, status, details):
        return DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, message.session_id, 1, status,
                               details)

    def cancel_notices(self):
        return [m for m in self.to(ActorRole.WORKER) if m.payload.get("cancel") is True]


class FenceFixture(FlowFixture):
    def setUp(self):
        with mock.patch.object(test_task_flow, "FakeMailbox", NoticeMailbox):
            super().setUp()

    def tearDown(self):
        self.mailbox.gate.set()  # a lane held in a notice attempt must end before the flow closes
        super().tearDown()

    def cancel_work(self, mode, message="the user changed plans"):
        self.mailbox.mode = mode
        work = self.running_work()
        result = self.to_worker({"kind": "work", "message": message, "cancel": True, "task_id": work["task_id"]})
        self.assertEqual(result["status"], "cancelled", result)
        return work["task_id"], result

    def fence_of(self, task_id):
        return self.flow.tasks[task_id].cancel_fence

    def armed(self, task_id):
        return self.fence_of(task_id) == "armed"

    def take_eventually(self, expected):
        """``take_cancel_fence()`` once no delivery attempt of the notice is under way (it refuses nothing then)."""
        found = []
        wait_until(lambda: found.append(self.flow.take_cancel_fence()) or found[-1] is not None, 10)
        self.assertEqual(found[-1], expected, f"no refusal in {len(found)} tries")
        return found[-1]

    def task_delivered(self, task_id):
        self.assertTrue(wait_until(lambda: any(m.task_id == task_id and m.message_id in self.mailbox.delivered
                                               for m in self.mailbox.to(ActorRole.WORKER)), 10),
                        "the Task's TASK message reached the worker")

    def notice_attempts(self, count=1):
        self.assertTrue(wait_until(lambda: len(self.mailbox.notice_attempts) >= count, 10), "the notice was tried")


class ArmedUntilTheNoticeReachesTheWorkerTests(FenceFixture):
    def test_a_deferred_notice_leaves_the_fence_armed_and_one_refusal_ends_it(self):
        task_id, result = self.cancel_work("defer")
        self.assertTrue(result["worker_notified"], "a cancel notice was queued")
        self.notice_attempts(3)  # it keeps being deferred
        self.assertTrue(self.armed(task_id))
        self.take_eventually(task_id)
        self.assertIsNone(self.flow.take_cancel_fence(), "one refusal per cancel")
        self.assertFalse(self.armed(task_id))
        self.assertTrue(self.fence_of(task_id), "the end is recorded in the Task, not erased")
        self.mailbox.mode = "ok"  # the deferred notice goes through later: nothing re-arms
        self.assertTrue(wait_until(lambda: self.mailbox.cancel_notices() and len(self.mailbox.delivered) >= 2, 10))
        time.sleep(0.2)
        self.assertIsNone(self.flow.take_cancel_fence())
        self.assertFalse(self.armed(task_id))

    def test_an_acknowledged_notice_ends_the_fence_without_a_refusal(self):
        task_id, _ = self.cancel_work("ok")
        self.assertTrue(wait_until(lambda: not self.armed(task_id), 10))
        self.assertIsNone(self.flow.take_cancel_fence())

    def test_an_unknown_outcome_also_ends_it(self):
        task_id, _ = self.cancel_work("unknown")
        self.assertTrue(wait_until(lambda: not self.armed(task_id), 10), "unknown: it may have reached the worker")
        self.assertIsNone(self.flow.take_cancel_fence())

    def test_a_rejected_notice_never_reached_the_worker_so_the_fence_stays(self):
        task_id, _ = self.cancel_work("rejected")
        self.notice_attempts(1)
        time.sleep(0.2)
        self.assertTrue(self.armed(task_id))
        self.take_eventually(task_id)
        self.assertIsNone(self.flow.take_cancel_fence())

    def test_an_attempt_in_progress_refuses_nothing_and_a_deferred_end_arms_it_again(self):
        self.mailbox.submit_first = False
        task_id, _ = self.cancel_work("block")
        self.assertTrue(self.mailbox.entered.wait(10), "the notice attempt is under way")
        self.assertIsNone(self.flow.take_cancel_fence(), "the worker may already hold the notice: no refusal")
        self.assertTrue(self.armed(task_id), "and the call did not end the fence")
        self.mailbox.mode = "defer"
        self.mailbox.gate.set()  # the attempt ends deferred: nothing was submitted
        self.assertTrue(wait_until(lambda: len(self.mailbox.notice_attempts) >= 3, 10))
        self.take_eventually(task_id)
        self.assertIsNone(self.flow.take_cancel_fence())

    def test_the_fence_ends_at_the_submission_even_while_the_attempt_is_still_open(self):
        self.mailbox.submit_first = True
        task_id, _ = self.cancel_work("block")
        self.assertTrue(self.mailbox.entered.wait(10))
        self.assertTrue(wait_until(lambda: not self.armed(task_id), 10), "submitted: the worker has it")
        self.assertIsNone(self.flow.take_cancel_fence())
        self.mailbox.gate.set()
        time.sleep(0.2)
        self.assertIsNone(self.flow.take_cancel_fence())

    def test_concurrent_terminal_calls_race_the_submission_and_never_refuse_twice_or_after_the_end(self):
        for round_ in range(8):
            with self.subTest(round=round_):
                self.mailbox.mode = "block"
                self.mailbox.submit_first = bool(round_ % 2)
                self.mailbox.gate = threading.Event()
                self.mailbox.entered = threading.Event()
                work = self.running_work(f"change {round_}")
                refusals, stop = [], threading.Event()

                def hammer():
                    while not stop.is_set():
                        found = self.flow.take_cancel_fence()
                        if found is not None:
                            refusals.append((found, self.fence_of(found)))
                        time.sleep(0.0005)
                threads = [threading.Thread(target=hammer, daemon=True) for _ in range(3)]
                for thread in threads:
                    thread.start()
                try:
                    self.to_worker({"kind": "work", "message": "stop", "cancel": True, "task_id": work["task_id"]})
                    self.assertTrue(self.mailbox.entered.wait(10))
                    self.mailbox.mode = "ok"
                    self.mailbox.gate.set()
                    self.assertTrue(wait_until(lambda: any(
                        m.task_id == work["task_id"] and m.message_id in self.mailbox.delivered
                        for m in self.mailbox.cancel_notices()), 10), "this round's notice was delivered")
                    time.sleep(0.05)
                finally:  # the hammering threads never outlive a failing round
                    stop.set()
                    for thread in threads:
                        thread.join(5)
                    self.mailbox.gate.set()
                self.assertLessEqual(len(refusals), 1, f"refused more than once: {refusals}")
                self.assertIsNone(self.flow.take_cancel_fence())
                # a Task that is not the newest never fences: the next round's Task replaces this one

    def test_a_second_cancel_of_the_closed_task_is_refused_and_does_not_arm_the_fence_again(self):
        task_id, _ = self.cancel_work("defer")
        self.take_eventually(task_id)
        again = self.to_worker({"kind": "work", "message": "again", "cancel": True, "task_id": task_id})
        self.assertEqual(again["reason"], "task_closed", again)
        self.assertIsNone(self.flow.take_cancel_fence())
        self.assertFalse(self.armed(task_id))

    def test_a_cancel_during_a_pause_keeps_the_fence_until_the_resume_delivers_the_notice(self):
        self.mailbox.mode = "ok"
        work = self.running_work()
        self.controller.automation["state"] = "paused"
        result = self.to_worker({"kind": "work", "message": "stop", "cancel": True, "task_id": work["task_id"]})
        self.assertEqual(result["status"], "cancelled", result)
        self.assertEqual(result.get("worker_notice"), "after_resume", result)
        time.sleep(0.3)
        self.assertTrue(self.armed(work["task_id"]), "the notice has not reached the worker")
        self.assertEqual(self.mailbox.cancel_notices(), [])
        self.controller.automation["state"] = "running"
        self.assertTrue(wait_until(lambda: not self.armed(work["task_id"]), 10), "delivered after the resume")
        self.assertIsNone(self.flow.take_cancel_fence())


class WhatNeverArmsTheFenceTests(FenceFixture):
    def test_a_notice_that_was_not_needed_never_arms_it(self):
        self.mailbox.defer_tasks = True  # the worker never receives the TASK
        work = self.new_work()
        self.assertEqual(work["status"], "dispatched", work)
        self.assertTrue(wait_until(lambda: len(self.mailbox.to(ActorRole.WORKER)) >= 1))
        result = self.to_worker({"kind": "work", "message": "never mind", "cancel": True, "task_id": work["task_id"]})
        self.assertEqual(result["status"], "cancelled", result)
        self.assertFalse(result["worker_notified"], "the worker never had the Task: no notice")
        self.assertIsNone(self.fence_of(work["task_id"]))
        self.assertIsNone(self.flow.take_cancel_fence())

    def test_an_experiment_cancel_never_arms_it(self):
        self.gates.exit.clear()  # the experiment's host command keeps running
        experiment = self.new_experiment()
        self.assertEqual(experiment["status"], "dispatched", experiment)
        self.assertTrue(wait_until(lambda: len(self.runs) == 1))
        result = self.to_worker({"kind": "experiment", "message": "stop", "cancel": True,
                                 "task_id": experiment["task_id"]})
        self.assertIn(result["status"], ("cancelled", "cancel_requested"), result)
        self.gates.exit.set()
        self.assertTrue(wait_until(lambda: self.flow.tasks[experiment["task_id"]].status == "closed", 10))
        self.assertEqual(self.flow.tasks[experiment["task_id"]].closed_reason, "cancelled")
        self.assertIsNone(self.fence_of(experiment["task_id"]))
        self.assertIsNone(self.flow.take_cancel_fence())
        self.assertIsNone(self.flow.after_cancel_of(), "an experiment is not a free-work cancel")

    def test_a_task_that_ended_in_another_way_never_arms_it(self):
        work = self.running_work()
        self.to_manager({"kind": "done", "message": "finished"})
        self.assertTrue(wait_until(lambda: self.flow.tasks[work["task_id"]].status == "closed", 10))
        self.assertIsNone(self.fence_of(work["task_id"]))
        self.assertIsNone(self.flow.take_cancel_fence())
        self.assertIsNone(self.flow.after_cancel_of())


class NewerTaskAndAfterCancelOfTests(FenceFixture):
    def test_after_cancel_of_names_the_cancelled_task_until_a_newer_task_exists(self):
        task_id, _ = self.cancel_work("defer")
        self.assertEqual(self.flow.after_cancel_of(), task_id)
        self.take_eventually(task_id)
        self.assertEqual(self.flow.after_cancel_of(), task_id, "the mark outlives the fence")
        newer = self.new_work("a newer change")
        self.assertEqual(newer["status"], "dispatched", newer)
        self.assertIsNone(self.flow.after_cancel_of(), "a newer Task (even a running one) ends it")
        self.assertIsNone(self.flow.take_cancel_fence())

    def test_a_newer_task_lifts_an_armed_fence_for_good(self):
        task_id, _ = self.cancel_work("defer")
        self.assertTrue(self.armed(task_id))
        newer = self.new_work("a newer change")
        self.assertEqual(newer["status"], "dispatched", newer)
        self.assertIsNone(self.flow.take_cancel_fence(), "the newer Task decides")
        self.task_delivered(newer["task_id"])
        self.assertTrue(wait_until(lambda: self.flow.tasks[newer["task_id"]].status == "running", 10))
        self.to_manager({"kind": "done", "message": "finished"})
        self.assertTrue(wait_until(lambda: self.flow.tasks[newer["task_id"]].status == "closed", 10))
        self.assertIsNone(self.flow.take_cancel_fence(), "the old fence does not come back when the newer Task ends")
        self.assertIsNone(self.flow.after_cancel_of())

    def test_the_status_views_of_the_cancelled_task_are_not_changed_by_the_fence(self):
        task_id, _ = self.cancel_work("defer")
        view = self.flow.status_view(task_id)
        self.assertEqual((view["status"], view["closed_reason"]), ("closed", "cancelled"))
        self.assertFalse(any("fence" in key for key in view), view)


class RestartKeepsTheFenceTests(FenceFixture):
    def test_an_armed_fence_survives_a_restart_and_the_notice_is_not_sent_again(self):
        task_id, _ = self.cancel_work("defer")
        self.notice_attempts(2)
        self.assertTrue(self.armed(task_id))
        self.mailbox.mode = "ok"
        self.restart()  # the outbox is memory-only: the pending notice is gone
        sent = len(self.mailbox.cancel_notices())
        time.sleep(0.4)
        self.assertEqual(len(self.mailbox.cancel_notices()), sent, "the lost notice is not resent")
        self.assertTrue(self.armed(task_id), "the fence was kept in the Task ledger")
        self.assertEqual(self.flow.after_cancel_of(), task_id)
        self.take_eventually(task_id)
        self.assertIsNone(self.flow.take_cancel_fence())

    def test_an_ended_fence_stays_ended_after_a_restart(self):
        task_id, _ = self.cancel_work("defer")
        self.take_eventually(task_id)
        self.restart()
        self.assertFalse(self.armed(task_id))
        self.assertIsNone(self.flow.take_cancel_fence())
        self.assertEqual(self.flow.after_cancel_of(), task_id)

    def test_a_fence_ended_by_the_notice_stays_ended_after_a_restart(self):
        task_id, _ = self.cancel_work("ok")
        self.assertTrue(wait_until(lambda: not self.armed(task_id), 10))
        self.restart()
        self.assertFalse(self.armed(task_id))
        self.assertIsNone(self.flow.take_cancel_fence())


# ---------------------------------------------------------------------------------------------------------------
# The Worker's terminal tool on a real Backend (fake OMPs, real host shell)
# ---------------------------------------------------------------------------------------------------------------
class BackendFenceFixture(RestartWorkerFixture):
    """In this fixture the Worker lane waits for the receipt of the TASK message, so the cancel notice is still
    pending (the fence armed) unless ``quick`` shortens that wait."""

    quick = False

    def setUp(self):
        super().setUp()
        if self.quick:
            self.backend.handoffs._deliver_timeout = 1.0
        self.backend.terminal._wait_seconds = 20
        self.handoffs_path = self.backend.layout.workflow / "handoffs.jsonl"
        self.markers = Path(self._dir.name) / "markers"
        self.markers.mkdir()

    def worker_terminal(self, **args):
        return self.call(ActorRole.WORKER, "terminal", {"command": None, **args})[0]

    def records(self, *types):
        lines = self.handoffs_path.read_text().splitlines() if self.handoffs_path.exists() else []
        found = [json.loads(line) for line in lines if line.strip()]
        return [r for r in found if r.get("type") in types] if types else found

    def free_task(self):
        result = self.call(ActorRole.MANAGER, "to_worker",
                           {"kind": "work", "message": "collect facts", "spec": {"goal": "g", "paths": []}})[0]
        self.assertEqual(result["status"], "dispatched", result)
        task = self.backend.flow.tasks[result["task_id"]]
        self.wait(lambda: task.status == "running", "the Task to run")
        return task

    def cancel(self, task):
        result, _ = self.call(ActorRole.MANAGER, "to_worker", {"kind": "work", "message": "the user changed plans",
                                                              "cancel": True, "task_id": task.task_id})
        self.assertEqual(result.get("status"), "cancelled", result)
        return result

    def touch(self, name):
        return f"touch {self.markers / name}"

    def requests_of(self, command_text):
        return [r for r in self.records("terminal_request") if r.get("command") == command_text]


class FencedTerminalTests(BackendFenceFixture):
    def test_the_first_task_less_command_is_refused_once_and_types_nothing_then_it_runs_marked(self):
        task = self.free_task()
        self.cancel(task)
        started_before = len(self.records("terminal_started"))
        command = self.touch("first")
        refused = self.worker_terminal(command=command)
        self.assertEqual((refused["status"], refused["reason"]), ("rejected", "task_cancelled"), refused)
        self.assertEqual(refused["task_id"], task.task_id)
        self.assertIn(task.task_id, refused["detail"])
        self.assertIn("not run", refused["detail"])
        self.assertIn("call again", refused["detail"].lower(), "it says the Worker may re-send")
        self.assertFalse((self.markers / "first").exists(), "nothing was typed to the shell")
        self.assertFalse((self.backend.terminal.current() or {}).get("running"))
        self.assertEqual(len(self.records("terminal_started")), started_before)
        self.assertEqual([r["reason"] for r in self.records("terminal_refused")], ["task_cancelled"],
                         "the refusal is journaled")
        (request,) = self.requests_of(command)
        self.assertIsNone(request["task_id"])
        # the second call (the Worker's re-send) runs, once, and carries the mark
        ran = self.worker_terminal(command=command)
        self.assertEqual((ran["status"], ran["exit_code"]), ("exited", 0), ran)
        self.assertTrue((self.markers / "first").exists())
        (started,) = [r for r in self.records("terminal_started") if r["command_id"] == ran["command_id"]]
        self.assertIsNone(started["task_id"])
        self.assertEqual(started["after_cancel_of"], task.task_id)
        self.assertEqual([r.get("after_cancel_of") for r in self.requests_of(command)], [task.task_id, task.task_id])
        self.assertEqual([r["reason"] for r in self.records("terminal_refused")], ["task_cancelled"],
                         "refused exactly once")
        # the cancelled Task has no part in it
        self.assertEqual(self.backend.flow.status_view(task.task_id).get("host_commands"), [])
        self.assertEqual(self.backend.terminal.commands_for_task(task.task_id), [])
        runs = self.backend.terminal.runs_for_task(task.task_id)
        self.assertEqual(list(runs.runs) if hasattr(runs, "runs") else [], [])
        # and the next one is marked too, with no new refusal
        third = self.worker_terminal(command=self.touch("third"))
        self.assertEqual(third["status"], "exited", third)
        (started3,) = [r for r in self.records("terminal_started") if r["command_id"] == third["command_id"]]
        self.assertEqual((started3["task_id"], started3["after_cancel_of"]), (None, task.task_id))

    def test_status_only_calls_and_unknown_stop_are_not_fenced_and_do_not_use_up_the_fence(self):
        task = self.free_task()
        self.cancel(task)
        status = self.worker_terminal(command=None)
        self.assertNotEqual(status.get("reason"), "task_cancelled", status)
        unknown = self.worker_terminal(stop=str(uuid4()))
        self.assertNotEqual(unknown.get("reason"), "task_cancelled", unknown)
        self.assertEqual([r for r in self.records("terminal_refused") if r["reason"] == "task_cancelled"], [])
        refused = self.worker_terminal(command=self.touch("later"))
        self.assertEqual(refused["reason"], "task_cancelled", "the fence was still there")
        self.assertFalse((self.markers / "later").exists())

    def test_a_call_refused_for_another_reason_does_not_use_up_the_fence(self):
        task = self.free_task()
        self.cancel(task)
        secret = "hunter2-hunter2-hunter2"
        self.backend.environment = {**self.backend.environment, "WB_FENCE_API_TOKEN": secret}
        both = self.worker_terminal(command="true", stop=str(uuid4()))
        self.assertEqual((both["status"], both["reason"]), ("rejected", "invalid_arguments"), both)
        leaked = self.worker_terminal(command=f"echo {secret}")
        self.assertEqual((leaked["status"], leaked["reason"]), ("rejected", "environment_value"), leaked)
        self.assertEqual([r for r in self.records("terminal_refused") if r["reason"] == "task_cancelled"], [])
        refused = self.worker_terminal(command=self.touch("valid"))
        self.assertEqual(refused["reason"], "task_cancelled", "the Worker has still not been told")
        self.assertFalse((self.markers / "valid").exists())

    def test_stop_of_a_command_started_before_the_cancel_is_not_fenced_and_leaves_the_fence(self):
        task = self.free_task()
        self.backend.terminal._wait_seconds = 1
        started = self.worker_terminal(command="sleep 615.7")
        self.assertEqual(started["status"], "running", started)
        self.addCleanup(lambda: self.backend.terminal.current().get("running")
                        and self.backend.command_stop.stop("worker", started["command_id"]))
        cancelled = self.cancel(task)
        self.assertEqual([c["command_id"] for c in cancelled["host_commands"]], [started["command_id"]])
        stopped = self.worker_terminal(stop=started["command_id"])
        self.assertEqual((stopped["status"], stopped["exit_code"]), ("exited", 130), stopped)
        refused = self.worker_terminal(command=self.touch("after-stop"))
        self.assertEqual(refused["reason"], "task_cancelled", "a stop did not end the fence")
        self.assertFalse((self.markers / "after-stop").exists())
        ran = self.worker_terminal(command=self.touch("after-stop"))
        self.assertEqual(ran["status"], "exited", ran)
        starts = {r["command_id"]: r for r in self.records("terminal_started")}
        self.assertEqual(starts[started["command_id"]]["task_id"], task.task_id)
        self.assertIsNone(starts[started["command_id"]].get("after_cancel_of"),
                          "a command of the Task is not marked as after its cancel")

    def test_the_manager_is_not_fenced(self):
        task = self.free_task()
        self.backend.terminal._wait_seconds = 1
        started = self.worker_terminal(command="sleep 615.8")
        self.assertEqual(started["status"], "running", started)
        self.addCleanup(lambda: self.backend.terminal.current().get("running")
                        and self.backend.command_stop.stop("manager", started["command_id"]))
        self.cancel(task)
        stopped = self.call(ActorRole.MANAGER, "stop_command", {"command_id": started["command_id"],
                                                               "reason": "the user asked to stop it"})[0]
        self.assertEqual(stopped["status"], "exited", stopped)
        refused = self.worker_terminal(command=self.touch("worker-after"))
        self.assertEqual(refused["reason"], "task_cancelled", "the Manager's call did not use up the Worker's fence")


class QuickNoticeTerminalTests(BackendFenceFixture):
    quick = True

    def test_once_the_notice_is_in_the_workers_session_nothing_is_refused_but_the_commands_are_marked(self):
        task = self.free_task()
        self.cancel(task)
        self.wait(lambda: self.backend.flow.tasks[task.task_id].cancel_fence != "armed", "the notice to be delivered",
                  timeout=15)
        self.assertNotEqual(self.backend.flow.tasks[task.task_id].cancel_fence, "refused_once")
        ran = self.worker_terminal(command=self.touch("no-refusal"))
        self.assertEqual((ran["status"], ran["exit_code"]), ("exited", 0), ran)
        self.assertEqual([r for r in self.records("terminal_refused") if r["reason"] == "task_cancelled"], [])
        (started,) = [r for r in self.records("terminal_started") if r["command_id"] == ran["command_id"]]
        self.assertEqual((started["task_id"], started["after_cancel_of"]), (None, task.task_id))
        (request,) = self.requests_of(self.touch("no-refusal"))
        self.assertEqual(request["after_cancel_of"], task.task_id)
        self.assertEqual(self.backend.flow.status_view(task.task_id).get("host_commands"), [])

    def test_a_newer_task_ends_the_marks_and_its_commands_belong_to_it(self):
        first = self.free_task()
        self.cancel(first)
        second = self.free_task()  # a newer Task: it decides now
        self.assertIsNone(self.backend.flow.after_cancel_of())
        ran = self.worker_terminal(command=self.touch("under-the-newer-task"))
        self.assertEqual((ran["status"], ran["exit_code"]), ("exited", 0), ran)
        (started,) = [r for r in self.records("terminal_started") if r["command_id"] == ran["command_id"]]
        self.assertEqual(started["task_id"], second.task_id)
        self.assertIsNone(started.get("after_cancel_of"))
        self.assertEqual([r for r in self.records("terminal_refused") if r["reason"] == "task_cancelled"], [])
        self.assertIsNone(self.backend.flow.take_cancel_fence())


if __name__ == "__main__":
    unittest.main()
