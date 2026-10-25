"""TI-13 independent verification of ``TaskFlow.cancel_by_user`` (the user's Task cancel), derived from the contract.

Sources: docs/features/task-inbox SPEC "취소" and "취소 주체", BRIEF D-20/D-33/D-35/D-37, P2-AC-30/41/43, ticket TI-13
"독립 초점", docs/features/command-stop (user stop CS-O10: ``requested_by: user``, SIGINT -> SIGTERM -> SIGKILL, signals only to
the command, never the host shell, the backend or an unrelated process). Not the implementer's tests.

Seam: a ``TaskFlow`` + real ``TerminalService`` + real ``CommandStopService`` + a real PTY host shell (the
``CancelHostCommandFixture`` of ``test_cancel_host_command``) with ``TaskFlow.command_stop`` wired as ``Backend`` wires
it; the experiment cases use the ``FlowFixture`` fake host.

Independent oracles: the Task ledger (``tasks-flow.jsonl``) and handoff journal this file reads back, ``/proc`` (pids and
process groups of ``sleep`` commands this file chose), the exit signals the command-stop records name, and two
sentinels (one in the test process's own group, one in its own session) that no stop may touch. Every process here is
one the test started; leftovers are killed by exact pid and start ticks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import unittest
import uuid

from command_stop_support import alive, stat_ticks
from test_cancel_host_command import CancelHostCommandFixture
from test_flow_terminal import wait_until
from test_task_flow import FlowFixture
from workbench.contracts.ui_v1 import TASK_STATUSES
from workbench.contracts.v1 import ActorRole


def pids_of(argv_head: list[str]) -> list[int]:
    wanted = [item.encode() for item in argv_head]
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            argv = Path(f"/proc/{name}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv[:len(wanted)] == wanted:
            found.append(int(name))
    return found


def pgrp_of(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[2])


class Sentinels:
    """Unrelated processes this test started; a stop may never touch them."""

    def __init__(self, case: unittest.TestCase):
        self.case = case
        self.procs: list[tuple[subprocess.Popen, int]] = []
        self.previous = signal.signal(signal.SIGINT, signal.SIG_IGN)  # the runner itself must survive a group SIGINT
        case.addCleanup(self.close)

    def start(self, seconds: str, *, new_session: bool) -> subprocess.Popen:
        proc = subprocess.Popen(["sleep", seconds], start_new_session=new_session, stdin=subprocess.DEVNULL,
                                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        ticks = None
        for _ in range(200):
            ticks = stat_ticks(proc.pid)
            if ticks is not None:
                break
            time.sleep(0.01)
        self.case.assertIsNotNone(ticks)
        self.procs.append((proc, ticks))
        return proc

    def alive(self, proc: subprocess.Popen) -> bool:
        ticks = dict((p.pid, t) for p, t in self.procs)[proc.pid]
        return proc.poll() is None and alive(proc.pid, ticks)

    def close(self):
        signal.signal(signal.SIGINT, self.previous)
        for proc, ticks in self.procs:
            if proc.poll() is None and alive(proc.pid, ticks):  # exact identity: pid + start ticks
                proc.kill()
            proc.wait(timeout=10)


class CancelFixture(CancelHostCommandFixture):
    def setUp(self):
        super().setUp()
        self.flow.command_stop = self.command_stop  # as Backend wires it

    def journal(self) -> list[dict]:
        path = self.root / "workflow" / "handoffs.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    def stops(self, kind: str | None = None) -> list[dict]:
        found = [item for item in self.journal() if str(item.get("type", "")).startswith("command_stop_")
                 and item["type"] != "command_stop_target"]
        return found if kind is None else [item for item in found if item["type"] == f"command_stop_{kind}"]

    def user_cancels(self, task_id: str | None = None) -> list[dict]:
        return [item for item in self.ledger() if item.get("type") == "cancel_requested" and item.get("by") == "user"
                and (task_id is None or item.get("task_id") == task_id)]

    def all_cancels(self) -> list[dict]:
        return [item for item in self.ledger() if item.get("type") == "cancel_requested"]

    def cancel_notices(self) -> list[dict]:
        return [m.payload for m in self.mailbox.to(ActorRole.WORKER) if m.payload.get("cancel") is True]

    def ask(self, task_id, status, command_id=None, reason="because"):
        return self.flow.cancel_by_user(task_id, reason, {"status": status, "command_id": command_id},
                                        request_id=str(uuid.uuid4()))

    def assert_nothing_happened(self, task_id: str, *, status: str = "running") -> None:
        self.assertEqual(self.all_cancels(), [], "no cancel was recorded")
        self.assertEqual([item for item in self.stops("requested") if item["requested_by"] == "user"], [],
                         "no user stop was asked (P2-AC-41)")
        self.assertEqual(self.cancel_notices(), [], "the worker was told nothing")
        self.assertEqual(self.flow.task_status(task_id), status)
        self.assertIsNone(self.flow.tasks[task_id].cancel_requested)


class ExpectBindingTests(CancelFixture):
    def test_every_other_task_status_is_task_changed_and_the_running_command_is_not_touched(self):
        work = self.running_work()
        task_id = work["task_id"]
        started = self.run_command("sleep 717.1")
        self.assertEqual(started["status"], "running", started)
        (pid,) = pids_of(["sleep", "717.1"])
        wrong = [status for status in TASK_STATUSES if status != "running"]
        self.assertGreaterEqual(len(wrong), 4, "the status vocabulary is the contract's")
        for status in wrong:
            for command_id in (started["command_id"], None):
                with self.subTest(status=status, command_id=command_id):
                    result = self.ask(task_id, status, command_id)
                    self.assertEqual((result["status"], result["reason"]), ("rejected", "task_changed"), result)
                    self.assertEqual(result["task_status"], "running", "the refusal says what the Task is now")
                    self.assertEqual(result["command_id"], started["command_id"])
        self.assert_nothing_happened(task_id)
        self.assertTrue(alive(pid), "the command was not signalled")
        self.assertTrue(self.terminal.current()["running"])
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")

    def test_a_different_or_missing_command_is_task_changed(self):
        work = self.running_work()
        task_id = work["task_id"]
        # (1) expecting no command while one runs
        started = self.run_command("sleep 717.2")
        (pid,) = pids_of(["sleep", "717.2"])
        self.assertEqual(self.ask(task_id, "running", None)["reason"], "task_changed")
        # (2) a command other than the running one (same shape, never started)
        self.assertEqual(self.ask(task_id, "running", str(uuid.uuid4()))["reason"], "task_changed")
        # (3) a command id that is not even an id
        for bad in ("", "not-an-id", 7, {"x": 1}):
            with self.subTest(command_id=bad):
                self.assertEqual(self.ask(task_id, "running", bad)["reason"], "task_changed")
        self.assert_nothing_happened(task_id)
        self.assertTrue(alive(pid))
        # (4) the command ended: the confirmation that named it no longer matches
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")
        self.assertTrue(wait_until(lambda: not alive(pid), 10))
        self.assertEqual(self.ask(task_id, "running", started["command_id"])["reason"], "task_changed")
        self.assert_nothing_happened(task_id)

    def test_the_command_changed_between_confirm_and_cancel_stops_nothing(self):
        work = self.running_work()
        task_id = work["task_id"]
        first = self.run_command("sleep 717.3")
        self.assertEqual(self.stop(first["command_id"])["status"], "exited")  # the confirmed command ended ...
        second = self.run_command("sleep 717.4")  # ... and another one started meanwhile
        self.assertEqual(second["status"], "running", second)
        (pid,) = pids_of(["sleep", "717.4"])
        result = self.ask(task_id, "running", first["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("rejected", "task_changed"), result)
        self.assertEqual(result["command_id"], second["command_id"])
        self.assertEqual(self.all_cancels(), [])
        self.assertEqual([item["command_id"] for item in self.stops("requested")], [first["command_id"]],
                         "only the test's own stop of the first command; nothing for the second")
        self.assertTrue(alive(pid), "the new command was not signalled")
        self.assertEqual(self.stop(second["command_id"])["status"], "exited")

    def test_a_task_the_manager_closed_meanwhile_is_task_changed_for_a_running_confirmation(self):
        work = self.running_work()
        task_id = work["task_id"]
        started = self.run_command("sleep 717.5")
        (pid,) = pids_of(["sleep", "717.5"])
        self.assertEqual(self.cancel(task_id)["status"], "cancelled")  # the manager's cancel (leaves the command)
        before = (len(self.all_cancels()), len(self.cancel_notices()))
        result = self.ask(task_id, "running", started["command_id"])
        self.assertEqual((result["status"], result["reason"], result["task_status"]), ("rejected", "task_changed",
                                                                                         "closed"), result)
        self.assertEqual((len(self.all_cancels()), len(self.cancel_notices())), before)
        self.assertEqual(self.user_cancels(), [])
        self.assertEqual(self.stops(), [], "no stop")
        self.assertTrue(alive(pid))
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")

    def test_an_unknown_task_and_an_empty_expect_are_task_changed(self):
        work = self.running_work()
        self.assertEqual(self.ask(str(uuid.uuid4()), "running")["reason"], "task_changed")
        for expect in ({}, {"command_id": None}, {"status": None, "command_id": None}):
            with self.subTest(expect=expect):
                result = self.flow.cancel_by_user(work["task_id"], None, expect, request_id=str(uuid.uuid4()))
                self.assertEqual((result["status"], result["reason"]), ("rejected", "task_changed"), result)
        self.assert_nothing_happened(work["task_id"])


class ExperimentExpectTests(FlowFixture):
    def test_an_experiment_that_finished_since_the_confirmation_is_task_changed_and_stays_finished(self):
        self.gates.exit.clear()
        pending = self.new_experiment()
        task_id = pending["task_id"]
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"
                                   and self.flow.status_view(task_id)["host_commands"]))
        run_id = self.flow.task_view()["run_id"]
        self.gates.exit.set()
        self.assertTrue(wait_until(lambda: self.flow.task_status(task_id) == "finished", 10))
        result = self.flow.cancel_by_user(task_id, None, {"status": "running", "command_id": run_id},
                                          request_id=str(uuid.uuid4()))
        self.assertEqual((result["status"], result["reason"], result["task_status"]),
                         ("rejected", "task_changed", "finished"), result)
        self.assertEqual(self.flow.task_status(task_id), "finished")
        self.assertIsNone(self.flow.tasks[task_id].cancel_requested)
        self.assertEqual([r for r in self.ledger() if r.get("type") == "cancel_requested"], [])


class CancelWithoutCommandTests(CancelFixture):
    def test_the_task_closes_as_the_users_cancel_and_the_worker_is_told_once(self):
        work = self.running_work()
        task_id = work["task_id"]
        result = self.ask(task_id, "running", None, reason="changed my mind")
        self.assertEqual((result["status"], result["cancel"], result["stop"]), ("accepted", "cancelled", None), result)
        self.assertEqual(self.flow.task_status(task_id), "closed")
        self.assertEqual(self.flow.status_view(task_id)["closed_reason"], "cancelled")
        (record,) = self.user_cancels(task_id)
        self.assertEqual((record["by"], record["message"]), ("user", "changed my mind"))
        self.assertEqual(self.flow.tasks[task_id].cancel_requested["by"], "user",
                         "the Hub tells a user cancel from a manager cancel by this field")
        self.assertTrue(wait_until(lambda: len(self.cancel_notices()) == 1, 10), "the worker is notified")
        self.assertEqual(self.cancel_notices()[0]["task_id"], task_id)
        self.assertEqual(self.cancel_notices()[0]["message"], "changed my mind")
        time.sleep(0.3)
        self.assertEqual(len(self.cancel_notices()), 1, "once")
        self.assertEqual(self.stops(), [], "no command, no stop")
        self.assertEqual(self.run_history(self.flow.status_view(task_id)["last_result"]["run_id"])[-1]["details"]
                         .get("reason"), "cancelled_by_user")

    def test_without_a_reason_the_record_still_says_the_user_did_it(self):
        work = self.running_work()
        result = self.flow.cancel_by_user(work["task_id"], None, {"status": "running", "command_id": None},
                                          request_id=str(uuid.uuid4()))
        self.assertEqual(result["cancel"], "cancelled", result)
        (record,) = self.user_cancels()
        self.assertEqual(record["by"], "user")
        self.assertTrue(record["message"].strip())

    def test_a_task_closed_by_the_user_refuses_the_managers_cancel_and_keeps_the_record(self):
        work = self.running_work()
        task_id = work["task_id"]
        self.ask(task_id, "running", None, reason="user reason")
        again = self.cancel(task_id, "manager reason")
        self.assertEqual(again["status"], "rejected", again)
        self.assertEqual(again["reason"], "task_closed", again)
        self.assertEqual(self.flow.tasks[task_id].cancel_requested["by"], "user")
        self.assertEqual(self.flow.tasks[task_id].cancel_requested["message"], "user reason")
        self.assertEqual(len(self.all_cancels()), 1)


class CancelWithCommandTests(CancelFixture):
    def test_only_the_commands_group_is_signalled_and_every_unrelated_process_survives(self):
        sentinels = Sentinels(self)
        group_sibling = sentinels.start("717.8", new_session=False)  # shares the test (= backend) process group
        outsider = sentinels.start("717.7", new_session=True)
        work = self.running_work()
        task_id = work["task_id"]
        started = self.run_command("sleep 718.1")
        self.assertEqual(started["status"], "running", started)
        (pid,) = pids_of(["sleep", "718.1"])
        command_group = pgrp_of(pid)
        self.assertNotEqual(command_group, os.getpgrp(), "the command is not in the backend's group")
        self.assertNotEqual(command_group, pgrp_of(group_sibling.pid))
        shell_pid = self.pane.pid
        result = self.ask(task_id, "running", started["command_id"])
        self.assertEqual((result["status"], result["cancel"]), ("accepted", "cancelled"), result)
        self.assertEqual(result["stop"], {"status": "stopping", "command_id": started["command_id"]})
        self.assertTrue(wait_until(lambda: not alive(pid), 20), "the command ended")
        self.assertTrue(wait_until(lambda: bool(self.stops("finished")), 20))
        (requested,) = self.stops("requested")
        self.assertEqual((requested["command_id"], requested["requested_by"], requested["task_id"]),
                         (started["command_id"], "user", task_id))
        signals = self.stops("signal")
        self.assertEqual(signals[0]["signal"], "SIGINT", "SIGINT first (CS-D03)")
        targeted = {pid_ for signal_ in signals for pid_ in
                    ([t.get("pid") for t in signal_.get("targets") or [] if isinstance(t, dict)]
                     or [t for t in signal_.get("targets") or [] if isinstance(t, int)])}
        self.assertLessEqual(targeted, {pid}, f"only the command's own processes were targeted: {targeted}")
        time.sleep(0.5)  # a late stray signal would land here
        self.assertTrue(sentinels.alive(group_sibling), "a process in the backend's own group survives")
        self.assertTrue(sentinels.alive(outsider), "an unrelated process survives")
        self.assertTrue(alive(shell_pid), "the host shell is not terminated")
        self.assertEqual(self.flow.task_status(task_id), "closed")
        after = self.run_command("echo still-here", wait=30)
        self.assertEqual((after["status"], after["exit_code"]), ("exited", 0), "the host shell still takes commands")

    def test_a_command_that_ignores_int_and_term_keeps_the_stop_going_while_the_task_is_already_closed(self):
        work = self.running_work()
        task_id = work["task_id"]
        started = self.run_command("(trap '' INT TERM; exec sleep 718.2)")
        self.assertEqual(started["status"], "running", started)
        (pid,) = pids_of(["sleep", "718.2"])
        result = self.ask(task_id, "running", started["command_id"])
        self.assertEqual(result["cancel"], "cancelled", result)
        self.assertEqual(self.flow.task_status(task_id), "closed", "the Task closes at once ...")
        self.assertTrue(alive(pid))
        commands = self.flow.status_view(task_id)["host_commands"]
        self.assertEqual([item["command_id"] for item in commands], [started["command_id"]],
                         "... while its command is still listed: the Hub keeps the request open until it ended")
        self.assertTrue(wait_until(lambda: not alive(pid), 20), "SIGKILL ends it after the grace times")
        self.assertTrue(wait_until(lambda: self.flow.status_view(task_id)["host_commands"] == [], 20))
        names = [item["signal"] for item in self.stops("signal")]
        self.assertEqual(names[:1], ["SIGINT"])
        self.assertEqual(names, sorted(set(names), key=names.index), "each stage once, in order")
        self.assertEqual([n for n in ("SIGINT", "SIGTERM", "SIGKILL") if n in names], names)
        self.assertIn("SIGKILL", names)
        self.assertEqual(len(self.stops("requested")), 1)

    def test_repeated_and_concurrent_cancels_cancel_once_and_stop_once(self):
        work = self.running_work()
        task_id = work["task_id"]
        started = self.run_command("sleep 718.3")
        results: list[dict] = []
        barrier = threading.Barrier(5)

        def go():
            barrier.wait(10)
            results.append(self.ask(task_id, "running", started["command_id"]))
        threads = [threading.Thread(target=go) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        accepted = [item for item in results if item.get("status") == "accepted"]
        others = [item for item in results if item.get("status") != "accepted"]
        self.assertEqual(len(results), 5, results)
        self.assertEqual([item["reason"] for item in others if item.get("reason") != "task_changed"], [])
        stopping = [item for item in accepted if item.get("stop")]
        self.assertEqual(len(stopping), 1, f"exactly one caller started the stop: {results}")
        self.assertEqual(len(self.user_cancels(task_id)), 1, "one cancel record")
        self.assertEqual(len(self.cancel_notices()) <= 1, True)
        sequential = [self.ask(task_id, status, started["command_id"]) for status in ("running", "closed")]
        for item in sequential:
            self.assertEqual((item["status"], item["cancel"], item["stop"]), ("accepted", "cancelled", None), item)
        self.assertTrue(wait_until(lambda: bool(self.stops("finished")), 20))
        self.assertEqual(len(self.stops("requested")), 1, "one stop however often it was asked")
        self.assertEqual(len(self.user_cancels(task_id)), 1)

    def test_another_tasks_command_is_never_stopped(self):
        first = self.running_work("task one")
        first_id = first["task_id"]
        started = self.run_command("sleep 718.4")
        (pid,) = pids_of(["sleep", "718.4"])
        self.assertEqual(self.cancel(first_id)["status"], "cancelled")  # leaves the command running (CS-08)
        second = self.running_work("task two")
        second_id = second["task_id"]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self.flow.status_view(second_id)["host_commands"], [], "the command belongs to the first Task")
        result = self.ask(second_id, "running", None)
        self.assertEqual((result["status"], result["cancel"], result["stop"]), ("accepted", "cancelled", None), result)
        time.sleep(0.5)
        self.assertEqual(self.stops(), [], "no stop was asked for the first Task's command")
        self.assertTrue(alive(pid))
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")


class ManagerRaceTests(CancelFixture):
    def test_the_manager_and_the_user_cancel_at_once_exactly_one_cancel_is_recorded(self):
        wins = {"user": 0, "manager": 0}
        for round_ in range(10):
            with self.subTest(round=round_):
                work = self.running_work(f"race {round_}")
                task_id = work["task_id"]
                barrier = threading.Barrier(2)
                answers: dict[str, dict] = {}

                def user():
                    barrier.wait(10)
                    answers["user"] = self.ask(task_id, "running", None, reason="user")

                def manager():
                    barrier.wait(10)
                    answers["manager"] = self.cancel(task_id, "manager")
                threads = [threading.Thread(target=user), threading.Thread(target=manager)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
                records = [item for item in self.ledger() if item.get("type") == "cancel_requested"
                           and item.get("task_id") == task_id]
                self.assertEqual(len(records), 1, f"one winner, one record: {records} {answers}")
                self.assertEqual(self.flow.tasks[task_id].cancel_requested["by"], records[0]["by"])
                self.assertEqual(self.flow.task_status(task_id), "closed")
                if records[0]["by"] == "user":
                    wins["user"] += 1
                    self.assertEqual(answers["user"]["status"], "accepted")
                    self.assertEqual(answers["manager"]["status"], "rejected", answers)
                else:
                    wins["manager"] += 1
                    self.assertEqual((answers["user"]["status"], answers["user"]["reason"]),
                                     ("rejected", "task_changed"), answers)
                    self.assertEqual(answers["manager"]["status"], "cancelled")
        self.assertEqual(sum(wins.values()), 10)


class ExperimentManagerRaceTests(FlowFixture):
    def cancel(self, task_id, message="stop"):
        return self.to_worker({"kind": "work", "message": message, "cancel": True, "task_id": task_id})

    def cancel_records(self):
        return [item for item in self.ledger() if item.get("type") == "cancel_requested"]

    def test_a_manager_cancel_during_the_users_cancelling_run_does_not_overwrite_it(self):
        self.gates.exit.clear()
        pending = self.new_experiment()
        task_id = pending["task_id"]
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"
                                   and self.flow.status_view(task_id)["host_commands"]))
        run_id = self.flow.task_view()["run_id"]
        user = self.flow.cancel_by_user(task_id, "user reason", {"status": "running", "command_id": run_id},
                                        request_id=str(uuid.uuid4()))
        self.assertEqual((user["status"], user["cancel"]), ("accepted", "cancel_requested"), user)
        self.assertEqual(self.flow.task_status(task_id), "cancelling")
        for attempt in range(3):  # the manager asks again and again while the host run winds down
            manager = self.cancel(task_id, f"manager {attempt}")
            self.assertEqual(manager["status"], "cancel_requested", manager)
        self.assertEqual(self.flow.tasks[task_id].cancel_requested["by"], "user")
        self.assertEqual(self.flow.tasks[task_id].cancel_requested["message"], "user reason")
        self.assertEqual([(item["by"], item["message"]) for item in self.cancel_records()],
                         [("user", "user reason")], "the manager's asks left no record over the user's")
        again = self.flow.cancel_by_user(task_id, "second", {"status": "cancelling", "command_id": run_id},
                                         request_id=str(uuid.uuid4()))
        self.assertEqual((again["status"], again["cancel"], again.get("repeat")), ("accepted", "cancel_requested", True))
        self.gates.exit.set()
        self.assertTrue(wait_until(lambda: self.flow.task_status(task_id) == "closed", 10))
        self.assertEqual(self.flow.status_view(task_id)["closed_reason"], "cancelled")
        self.assertEqual(self.gates.judged, 0, "a cancelled run is not judged")
        self.assertEqual(self.flow.tasks[task_id].cancel_requested["by"], "user")
        self.assertEqual(len(self.cancel_records()), 1)

    def test_a_flow_restart_keeps_the_user_cancel_record_of_a_closed_task(self):
        """The Hub closes the request only for a user-cancelled Task; a restart must not turn it into a manager's."""
        self.gates.exit.clear()
        pending = self.new_experiment()
        task_id = pending["task_id"]
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"
                                   and self.flow.status_view(task_id)["host_commands"]))
        run_id = self.flow.task_view()["run_id"]
        self.flow.cancel_by_user(task_id, "keep me", {"status": "running", "command_id": run_id},
                                 request_id=str(uuid.uuid4()))
        self.gates.exit.set()
        self.assertTrue(wait_until(lambda: self.flow.task_status(task_id) == "closed", 10))
        self.restart()
        task = self.flow.tasks[task_id]
        self.assertEqual((task.status, task.closed_reason), ("closed", "cancelled"))
        self.assertEqual((task.cancel_requested or {}).get("by"), "user")
        repeat = self.flow.cancel_by_user(task_id, None, {"status": "closed", "command_id": None},
                                          request_id=str(uuid.uuid4()))
        self.assertEqual((repeat["status"], repeat["cancel"], repeat.get("repeat")), ("accepted", "cancelled", True))
        self.assertEqual(len(self.cancel_records()), 1)


if __name__ == "__main__":
    unittest.main()
