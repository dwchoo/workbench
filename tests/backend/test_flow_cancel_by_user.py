"""TI-13: the user's Task cancel (``TaskFlow.cancel_by_user``) from the Task Inbox.

Seam: the ``CancelHostCommandFixture`` of ``test_cancel_host_command`` (a ``TaskFlow`` + real ``TerminalService`` +
real ``CommandStopService`` + a real PTY host shell), with ``TaskFlow.command_stop`` wired the way ``Backend`` does.

Expected values: the Task Inbox SPEC "취소" text (``task_changed`` when the Task status or its running command differ
from ``expect``; the existing ``_cancel`` procedure with ``by: user``; the user stop of CS-O10 with
``requested_by: user``), the ledger and handoff journal records, ``/proc`` and the ``sleep`` arguments this test chose.
Every process here is one the test started inside its own host shell.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import unittest
import uuid

from test_cancel_host_command import CancelHostCommandFixture
from test_flow_terminal import wait_until


def sleep_pids(seconds: str) -> list[int]:
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            argv = Path(f"/proc/{name}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv[:2] == [b"sleep", seconds.encode()]:
            found.append(int(name))
    return found


class UserCancelFixture(CancelHostCommandFixture):
    def setUp(self):
        super().setUp()
        self.flow.command_stop = self.command_stop  # as Backend wires it

    def journal(self) -> list[dict]:
        path = self.root / "workflow" / "handoffs.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    def stop_records(self) -> list[dict]:
        return [item for item in self.journal() if str(item.get("type", "")).startswith("command_stop_")
                and item["type"] != "command_stop_target"]

    def cancel_records(self) -> list[dict]:
        return [item for item in self.ledger() if item.get("type") == "cancel_requested"]

    def user_cancel(self, task_id, status, command_id=None, reason="not needed any more"):
        return self.flow.cancel_by_user(task_id, reason, {"status": status, "command_id": command_id},
                                        request_id=str(uuid.uuid4()))


class ExpectMismatchTests(UserCancelFixture):
    def test_a_different_status_or_command_is_task_changed_and_nothing_is_cancelled_or_stopped(self):
        work = self.running_work()
        task_id = work["task_id"]
        started = self.run_command("sleep 713.1")
        self.assertEqual(started["status"], "running", started)
        for status, command_id in (("dispatched", started["command_id"]),  # the status moved on
                                   ("running", None),  # a command started meanwhile
                                   ("running", str(uuid.uuid4()))):  # another command
            with self.subTest(status=status, command_id=command_id):
                result = self.user_cancel(task_id, status, command_id)
                self.assertEqual((result["status"], result["reason"]), ("rejected", "task_changed"), result)
                self.assertEqual(self.flow.task_status(task_id), "running")
        self.assertEqual(self.cancel_records(), [])
        self.assertEqual(self.stop_records(), [], "no stop was asked")
        self.assertTrue(self.terminal.current()["running"], "the command was not touched")
        self.assertEqual(len(sleep_pids("713.1")), 1)
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")  # the test ends its own command

    def test_an_unknown_task_is_task_changed(self):
        result = self.user_cancel(str(uuid.uuid4()), "running")
        self.assertEqual((result["status"], result["reason"]), ("rejected", "task_changed"), result)


class CancelWithRunningCommandTests(UserCancelFixture):
    def test_the_confirmed_command_gets_the_users_stop_and_the_task_closes(self):
        work = self.running_work()
        task_id = work["task_id"]
        started = self.run_command("sleep 713.3")
        self.assertEqual(started["status"], "running", started)
        (pid,) = sleep_pids("713.3")
        result = self.user_cancel(task_id, "running", started["command_id"])
        self.assertEqual((result["status"], result["cancel"]), ("accepted", "cancelled"), result)
        self.assertEqual(result["stop"], {"status": "stopping", "command_id": started["command_id"]})
        self.assertEqual(self.flow.task_status(task_id), "closed")
        self.assertTrue(wait_until(lambda: not os.path.exists(f"/proc/{pid}"), 20), "the command ended")
        self.assertTrue(wait_until(lambda: any(r["type"] == "command_stop_finished" for r in self.stop_records()), 20))
        requested = [r for r in self.stop_records() if r["type"] == "command_stop_requested"]
        self.assertEqual([(r["command_id"], r["requested_by"]) for r in requested],
                         [(started["command_id"], "user")], "one user stop of exactly that command")
        signals = [r for r in self.stop_records() if r["type"] == "command_stop_signal"]
        self.assertEqual(signals[0]["signal"], "SIGINT", "SIGINT first (CS-D03)")
        self.assertTrue(wait_until(lambda: self.flow.status_view(task_id)["host_commands"] == [], 20))

    def test_a_repeated_cancel_does_not_stop_again(self):
        work = self.running_work()
        started = self.run_command("sleep 713.4")
        self.assertIsNotNone(self.user_cancel(work["task_id"], "running", started["command_id"])["stop"])
        again = self.user_cancel(work["task_id"], "closed", started["command_id"])
        self.assertEqual((again["cancel"], again["stop"], again["repeat"]), ("cancelled", None, True), again)
        self.assertTrue(wait_until(lambda: any(r["type"] == "command_stop_finished" for r in self.stop_records()), 20))
        self.assertEqual(len([r for r in self.stop_records() if r["type"] == "command_stop_requested"]), 1)


class CancelWithoutCommandTests(UserCancelFixture):
    def test_a_task_without_a_command_closes_as_the_users_cancel_and_the_worker_is_told(self):
        work = self.running_work()
        task_id = work["task_id"]
        result = self.user_cancel(task_id, "running", None, reason="changed my mind")
        self.assertEqual((result["status"], result["cancel"], result["stop"]), ("accepted", "cancelled", None), result)
        self.assertEqual(self.flow.task_status(task_id), "closed")
        view = self.flow.status_view(task_id)
        self.assertEqual((view["closed_reason"], view["cancel_requested"]), ("cancelled", True))
        (record,) = self.cancel_records()
        self.assertEqual((record["task_id"], record["by"], record["message"]), (task_id, "user", "changed my mind"))
        self.assertIn("request_id", record)
        self.assertEqual(self.flow.tasks[task_id].cancel_requested["by"], "user")
        self.assertTrue(wait_until(lambda: any(m.payload.get("cancel") is True and m.payload["task_id"] == task_id
                                               for m in self.mailbox.to(self.worker_role)), 10),
                        "the worker gets the cancel notice as with the manager's cancel")
        notice = next(m for m in self.mailbox.to(self.worker_role) if m.payload.get("cancel") is True)
        self.assertEqual(notice.payload["message"], "changed my mind")
        self.assertEqual(self.stop_records(), [], "no command, no stop")

    def test_a_repeated_user_cancel_is_the_same_answer_and_records_nothing_more(self):
        work = self.running_work()
        task_id = work["task_id"]
        self.assertEqual(self.user_cancel(task_id, "running")["cancel"], "cancelled")
        again = self.user_cancel(task_id, "running")
        self.assertEqual((again["status"], again["cancel"], again["stop"], again["repeat"]),
                         ("accepted", "cancelled", None, True), again)
        self.assertEqual(len(self.cancel_records()), 1)

    @property
    def worker_role(self):
        from workbench.contracts.v1 import ActorRole
        return ActorRole.WORKER


from test_task_flow import FlowFixture  # noqa: E402


class ExperimentUserCancelTests(FlowFixture):
    """The user's cancel of an experiment Task follows the manager's procedure (TaskFlow fixture, fake host)."""

    def cancel(self, task_id, message="stop"):
        return self.to_worker({"kind": "work", "message": message, "cancel": True, "task_id": task_id})

    def test_an_unstarted_task_closes_and_the_worker_gets_nothing(self):
        self.port.busy_reason = "a job is running"
        pending = self.new_experiment()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["held_reason"] == "host_terminal_busy"))
        status = self.flow.task_status(pending["task_id"])
        result = self.flow.cancel_by_user(pending["task_id"], None, {"status": status, "command_id": None})
        self.assertEqual((result["status"], result["cancel"], result["stop"]), ("accepted", "cancelled", None))
        self.port.busy_reason = None
        time.sleep(0.2)
        self.assertEqual(self.runs, [], "nothing started")
        self.assertEqual(self.mailbox.created, [], "the worker never got anything, so nothing is sent")

    def test_a_running_host_command_waits_for_its_exit_and_a_later_manager_cancel_keeps_the_users(self):
        self.gates.exit.clear()
        pending = self.new_experiment()
        task_id = pending["task_id"]
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"
                                   and self.flow.status_view(task_id)["host_commands"]))
        run_id = self.flow.task_view()["run_id"]
        result = self.flow.cancel_by_user(task_id, "no longer needed", {"status": "running", "command_id": run_id})
        self.assertEqual((result["status"], result["cancel"]), ("accepted", "cancel_requested"), result)
        self.assertEqual(result["stop"]["command_id"], run_id, "the experiment command is the one stopped")
        self.assertEqual(self.flow.task_status(task_id), "cancelling")
        manager = self.cancel(task_id)
        self.assertEqual(manager["status"], "cancel_requested", manager)
        self.assertEqual(self.flow.tasks[task_id].cancel_requested["by"], "user", "the user's cancel stays")
        self.gates.exit.set()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "closed"))
        self.assertEqual((self.flow.task_view()["closed_reason"], self.gates.judged), ("cancelled", 0))
        self.assertEqual(self.run_history(run_id)[-1]["details"].get("reason"), "cancelled_by_user")


if __name__ == "__main__":
    unittest.main()
