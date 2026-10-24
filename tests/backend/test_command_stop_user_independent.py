"""CS-O10 independent verification: the user's stop of a Workbench command, derived from the contract.

Sources: docs/features/command-stop FOLLOWUPS-BRIEF FU-D06~D08 / FU-AC-07~10, SPEC "인가 매트릭스", "결과 계약",
"감사 기록", "신원·신호 불변식", and the CS-O10 task packet; not the implementer's tests.

Seams
  S3  ``CommandStopService`` with fake providers (the authority table's ``user`` row, stale/finished/unknown ids,
      repeats and joins, audit ``requested_by``).
  S2  a real ``Backend`` (fake OMPs, real PTY host shell): ``Backend.stop_command`` (the controller method behind the
      UI's key), the ``panes.host_shell.command`` snapshot field, a command that ignores INT/TERM, a command that puts
      its tty in raw mode with ISIG off, a Manager experiment command, and the whole chain through the backend's own
      ui_v1 socket (ack, final, state push, a client that leaves, a controller that raises).

Independent oracles: the BRIEF authority table written out here, exit codes 128+n of commands this file starts, /proc
reads and signal logs written by shell ``trap`` handlers of the test's own commands, the ids and Task states this file
creates, and two sentinel processes outside every command tree (inherited from the CS-05 fixture) that must survive.
Every process the product signals is a command this file started inside the backend's own host shell; leftovers are
killed by exact pid and start ticks and fail the test.
"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from workbench.backend import command_stop
from workbench.backend.client import UiClient
from workbench.contracts.v1 import ActorRole, PaneId

import test_command_stop_authority_independent as auth
import test_experiment_command_stop_independent as exp
from command_stop_support import alive, pids_with, stat_of

USER_TABLE = {"worker": {"worker"}, "manager": {"worker", "manager"}, "user": {"worker", "manager"}}
STOP_TYPES = ("command_stop_requested", "command_stop_refused", "command_stop_target", "command_stop_signal",
              "command_stop_finished", "command_stop_repeat", "command_stop_corrected")


# =============================================================================================================
# S3: the authority table and the stop service's answers for the user
# =============================================================================================================
class UserAuthorityTests(unittest.TestCase):
    def test_the_stoppable_table_is_exactly_the_documented_one(self):
        self.assertEqual({k: set(v) for k, v in command_stop.STOPPABLE_BY.items()}, USER_TABLE)

    def test_the_user_may_stop_worker_and_manager_commands_and_is_recorded_as_the_requester(self):
        for owner in auth.OWNERS:
            with self.subTest(owner=owner):
                registry = auth.Registry()
                command_id = registry.register(owner)
                result = registry.service.stop("user", command_id, reason="user stop")
                self.assertEqual((result["command_id"], result["requested_by"]), (command_id, "user"))
                self.assertNotEqual(result["status"], "refused", result)
                (requested,) = registry.of("command_stop_requested")
                self.assertEqual((requested["requested_by"], requested["owner"], requested["reason"]),
                                 ("user", owner, "user stop"))
                self.assertEqual(registry.of("command_stop_refused"), [])
                self.assertEqual(registry.of("command_interrupt"), [], "a stop is not an interrupt record")

    def test_requester_spellings_other_than_user_are_refused_and_touch_nothing(self):
        for spelling in ("User", "USER", " user", "user ", "user\n", "usr", "ui", "human", "root", "worker,user",
                         "manager/user", "user\x00"):
            for owner in auth.OWNERS:
                with self.subTest(requester=repr(spelling), owner=owner):
                    registry = auth.Registry()
                    command_id = registry.register(owner)
                    result = registry.service.stop(spelling, command_id)
                    self.assertEqual((result["status"], result["reason"]), ("refused", "not_owner"), result)
                    registry.untouched(self)

    def test_a_finished_id_is_already_exited_for_the_user_and_nothing_is_signalled(self):
        for owner in auth.OWNERS:
            with self.subTest(owner=owner):
                registry = auth.Registry()
                command_id = registry.register(owner)
                registry.service.command_ended(command_id, {"status": "exited", "exit_code": 3, "signal": None})
                result = registry.service.stop("user", command_id, reason="user stop")
                self.assertEqual((result["status"], result["exit_code"], result["requested_by"]),
                                 ("already_exited", 3, "user"), result)
                self.assertEqual((registry.env.opened, registry.env.sent, registry.identity_reads), ([], [], 0))
                self.assertEqual(registry.of("command_stop_signal", "command_stop_target"), [])

    def test_an_unknown_id_and_a_previous_backend_id_are_refused_for_the_user_too(self):
        previous = str(uuid4())
        registry = auth.Registry(previous=lambda command_id: command_id == previous)
        registry.register("worker")
        other = str(uuid4())
        result = registry.service.stop("user", other, reason="user stop")
        self.assertEqual((result["status"], result["reason"], result["requested_by"]),
                         ("not_found", "unknown_command", "user"), result)
        result = registry.service.stop("user", previous, reason="user stop")
        self.assertEqual((result["status"], result["reason"], result["requested_by"]),
                         ("refused", "previous_backend_command", "user"), result)
        registry.untouched(self)
        self.assertEqual([(r["command_id"], r["requested_by"], r["reason"]) for r in registry.of("command_stop_refused")],
                         [(other, "user", "unknown_command"), (previous, "user", "previous_backend_command")])

    def test_a_command_without_a_live_identity_ends_unknown_without_a_signal_for_the_user(self):
        registry = auth.Registry()
        command_id = registry.register("manager")  # identity() returns None
        result = registry.service.stop("user", command_id, reason="user stop")
        self.assertEqual((result["status"], result["reason"], result["requested_by"]),
                         ("unknown", "identity_unavailable", "user"), result)
        self.assertEqual(registry.env.sent, [])

    def test_the_users_repeat_after_the_episode_is_the_same_result_with_one_request_record(self):
        registry = auth.Registry()
        command_id = registry.register("worker")
        first = registry.service.stop("user", command_id, reason="user stop")
        again = registry.service.stop("user", command_id, reason="user stop")
        self.assertIs(again.get("repeat"), True, again)
        self.assertEqual(again["status"], first["status"])
        self.assertEqual(len(registry.of("command_stop_requested")), 1, "one episode, one request record")
        self.assertEqual([r["requested_by"] for r in registry.of("command_stop_repeat")], ["user"])

    def test_a_user_and_a_manager_stopping_one_command_at_once_share_one_episode(self):
        policy = command_stop.StopPolicy(identity_wait=0.8, sigint_wait=0.2, sigterm_wait=0.2, sigkill_wait=0.2,
                                         call_wait=5.0, poll_interval=0.01)
        registry = auth.Registry(policy=policy)
        command_id = registry.register("worker")
        box: dict = {}
        thread = threading.Thread(target=lambda: box.update(user=registry.service.stop("user", command_id)))
        thread.start()
        deadline = time.monotonic() + 30
        while registry.service.surface(command_id) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        joined = registry.service.stop("manager", command_id, reason="me too")
        thread.join(30)
        self.assertEqual((box["user"]["requested_by"], joined.get("repeat")), ("user", True), (box, joined))
        self.assertEqual(joined["requested_by"], "user", "a join reports the episode's own requester")
        (requested,) = registry.of("command_stop_requested")
        self.assertEqual(requested["requested_by"], "user")

    def test_many_users_at_once_still_make_one_episode(self):
        policy = command_stop.StopPolicy(identity_wait=0.8, sigint_wait=0.2, sigterm_wait=0.2, sigkill_wait=0.2,
                                         call_wait=5.0, poll_interval=0.01)
        registry = auth.Registry(policy=policy)
        command_id = registry.register("manager")
        results: list = []
        threads = [threading.Thread(target=lambda: results.append(registry.service.stop("user", command_id)))
                   for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(results), 12)
        self.assertEqual(len(registry.of("command_stop_requested")), 1)
        self.assertEqual(len({(r["status"], r.get("reason")) for r in results}), 1, results)
        self.assertEqual(sum(1 for r in results if not r.get("repeat")), 1, "exactly one original result")


# =============================================================================================================
# S2: a real Backend, real PTY host shell, real processes
# =============================================================================================================
class UserBackendFixture(auth.Cs05Fixture):
    def user_stop(self, command_id, *, timeout=60):
        """``Backend.stop_command`` (what the UI server runs on its stop thread) while this thread runs the loop."""
        box: dict = {}
        thread = threading.Thread(target=lambda: box.update(result=self.backend.stop_command(command_id)),
                                  name="test-user-stop")
        thread.start()
        self.addCleanup(thread.join, 60)
        self.wait(lambda: "result" in box, "the user's stop to finish", timeout)
        return box["result"]

    def user_stop_in_background(self, command_id):
        box: dict = {}
        thread = threading.Thread(target=lambda: box.update(result=self.backend.stop_command(command_id)),
                                  name="test-user-stop")
        thread.start()
        self.addCleanup(thread.join, 60)
        return box, thread

    def host_command(self):
        return self.backend.snapshot()["panes"]["host_shell"]["command"]

    def quick_command(self, label):
        mark = self.mark(label)
        started = self.worker_terminal(command=f": {mark}; echo finished")
        self.assertEqual(started["status"], "exited", started)
        return mark, started


class UserStopOfWorkerCommandTests(UserBackendFixture):
    def test_a_command_that_ignores_int_and_term_is_ended_by_the_users_escalation_and_audited_as_user(self):
        mark, started, log = self.trapping("a command that ignores INT and TERM")
        leader, (_, supervisor, group, shell, _) = self.leader_of(mark)
        result = self.user_stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"], result["signal_name"]), (137, 9, "SIGKILL"))
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertEqual((result["requested_by"], result["command_id"]), ("user", started["command_id"]))
        self.assertGreaterEqual(self.signals_seen(log), 2, "the command did receive INT and TERM")
        self.assertFalse(alive(leader), "the command is still alive")
        self.assertTrue(alive(shell), "the host shell survived the stop")
        self.assertEqual(self.backend.handoffs.pending_approvals(), [], "no approval was asked of anybody")
        (requested,) = self.records("command_stop_requested")
        self.assertEqual((requested["command_id"], requested["requested_by"], requested["owner"], requested["kind"],
                          requested["reason"]), (started["command_id"], "user", "worker", "terminal", "user stop"))
        for record in self.records(*STOP_TYPES):
            self.assertNotIn(record.get("requested_by", "user"), ("worker", "manager"), record)
        self.assertEqual(self.records("command_interrupt"), [], "a user stop is not a Ctrl+C interrupt record")
        targeted = {item["pid"] for record in self.records("command_stop_signal") for item in record["targets"]}
        self.assertEqual(targeted, {leader}, "only the command's own process was signalled")
        self.assertFalse(targeted & {os.getpid(), shell, supervisor, *[e["pid"] for e in self.invocations()]})
        self.assertEqual(self.backend.terminal.current().get("running"), False)

    def test_the_workers_view_of_a_user_stopped_command_names_the_user_not_a_role_that_did_not_ask(self):
        mark, started, log = self.trapping("a command the user stops")
        self.user_stop(started["command_id"])
        after = self.worker_terminal(command=None)
        self.assertEqual(after.get("status"), "exited", after)
        self.assertEqual(after.get("stopped_by"), "user", after)
        self.assertNotEqual(after.get("exit_code"), 0)

    def test_a_cooperating_command_ends_at_sigint_without_escalation(self):
        mark = self.mark("a plain sleep")
        started = self.run_command(f"sleep {mark}")
        result = self.user_stop(started["command_id"])
        self.assertEqual((result["status"], result["exit_code"], result["signal_name"]), ("exited", 130, "SIGINT"),
                         result)
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT"])
        self.assertEqual(pids_with(mark), [])

    def test_the_users_second_stop_is_idempotent_and_sends_no_new_signal(self):
        mark, started, log = self.trapping("a command stopped twice by the user")
        first = self.user_stop(started["command_id"])
        sent = len(self.records("command_stop_signal"))
        second = self.user_stop(started["command_id"])
        self.assertIs(second.get("repeat"), True, second)
        self.assertEqual((second["status"], second["exit_code"]), (first["status"], first["exit_code"]))
        self.assertEqual(len(self.records("command_stop_signal")), sent, "a repeat made new signals")
        self.assertEqual(len(self.records("command_stop_requested")), 1)

    def test_a_finished_command_is_already_exited_and_the_next_command_is_never_touched_by_its_old_id(self):
        _, first = self.quick_command("first, already finished")
        mark, second, log = self.trapping("the next command")
        result = self.user_stop(first["command_id"])
        self.assertEqual(result["status"], "already_exited", result)
        self.assertEqual(result["requested_by"], "user")
        self.settle()
        self.assertEqual(self.signals_seen(log), 0, "the old id reached the next command")
        self.assertEqual(self.backend.terminal.current().get("running"), True)
        self.assertEqual(self.records("command_stop_signal", "command_stop_target"), [])
        stopped = self.user_stop(second["command_id"])  # and the right id still works
        self.assertEqual(stopped["status"], "exited", stopped)

    def test_ids_that_are_not_the_running_commands_signal_nothing(self):
        mark, started, log = self.trapping("a running command next to wrong ids")
        leader, (_, supervisor, group, shell, _) = self.leader_of(mark)
        cid = started["command_id"]
        wrong = ["", "x", str(os.getpid()), str(shell), "$$", "../x", cid.upper(), cid[:-1], cid + "0", f" {cid}",
                 f"{cid}\n", str(uuid4()), "00000000-0000-0000-0000-000000000000"]
        for value in wrong:
            with self.subTest(command_id=value):
                result = self.backend.stop_command(value)
                self.assertNotIn(result["status"], ("exited", "stopping", "unknown"), result)
                self.assertEqual(result["requested_by"], "user")
        self.settle()
        self.assertEqual(self.signals_seen(log), 0)
        self.assertTrue(alive(leader) and alive(shell) and alive(supervisor))
        self.assertEqual(self.records("command_stop_signal", "command_stop_target", "command_stop_requested"), [])
        self.assertEqual(self.backend.terminal.current().get("running"), True)

    def test_a_user_stop_while_the_worker_and_the_manager_also_ask_is_one_episode(self):
        mark, started, log = self.trapping("a command three requesters stop at once")
        cid = started["command_id"]
        user_box, user_thread = self.user_stop_in_background(cid)
        results: dict = {}

        def worker():
            results["worker"] = self.call(ActorRole.WORKER, "terminal", {"command": None, "stop": cid})[0]

        def manager():
            results["manager"] = self.manager_stop(cid)
        threads = [threading.Thread(target=worker), threading.Thread(target=manager)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 60)
        self.wait(lambda: "result" in user_box and len(results) == 2, "all three stops", 90)
        statuses = {user_box["result"]["status"], results["worker"]["status"], results["manager"]["status"]}
        self.assertEqual(statuses, {"exited"})
        signals = [r["signal"] for r in self.records("command_stop_signal") if not r.get("auxiliary")]
        self.assertEqual(signals, ["SIGINT", "SIGTERM", "SIGKILL"], "one set of signals for one episode")
        self.assertEqual(len(self.records("command_stop_requested")), 1)
        self.assertEqual(self.records("command_stop_refused"), [])

    def test_ctrl_c_and_a_user_stop_leave_different_audit_records(self):
        mark, started, log = self.trapping("a command that sees a Ctrl+C then a stop")
        cid = started["command_id"]
        self.backend.admit(PaneId.HOST_SHELL, b"\x03", "input")
        self.settle(1.0)
        interrupts = self.records("command_interrupt")
        self.assertEqual([(r["requested_by"], r.get("command_id")) for r in interrupts], [("user", cid)], interrupts)
        self.assertEqual(self.stop_records(), [], "a Ctrl+C is not a stop episode")
        self.assertGreaterEqual(self.signals_seen(log), 1)
        self.user_stop(cid)
        self.assertEqual(len(self.records("command_interrupt")), 1, "the stop did not add an interrupt record")
        self.assertTrue(self.records("command_stop_requested"))

    def test_without_a_stop_service_the_user_gets_a_refusal_not_an_exception(self):
        mark, started, log = self.trapping("a command whose stop service is gone")
        service = self.backend.command_stop
        self.backend.command_stop = None
        self.addCleanup(setattr, self.backend, "command_stop", service)
        result = self.backend.stop_command(started["command_id"])
        self.assertEqual((result["status"], result["requested_by"], result["command_id"]),
                         ("refused", "user", started["command_id"]), result)
        self.assertTrue(result.get("reason"))
        self.settle()
        self.assertEqual(self.signals_seen(log), 0)


class HostShellCommandSnapshotTests(UserBackendFixture):
    """FU-AC-10: the snapshot names the running Workbench command, with or without a Task."""

    def test_the_field_is_none_when_nothing_runs_and_a_quiet_shell_is_not_a_target(self):
        snapshot = self.backend.snapshot()
        self.assertIn("command", snapshot["panes"]["host_shell"])
        self.assertIsNone(snapshot["panes"]["host_shell"]["command"])
        for pane in ("manager_omp", "worker_omp"):
            self.assertNotIn("command", snapshot["panes"][pane], "only the host shell names a command")

    def test_a_task_less_worker_command_is_shown_with_id_owner_label_and_state_then_cleared(self):
        mark = self.mark("task-less command")
        started = self.run_command(f"sleep {mark}")
        shown = self.host_command()
        self.assertEqual(set(shown), {"command_id", "owner", "label", "state"}, shown)
        self.assertEqual((shown["command_id"], shown["owner"], shown["state"]),
                         (started["command_id"], "worker", "running"))
        self.assertIn(mark, shown["label"])
        json.dumps(self.backend.snapshot())  # the whole snapshot stays JSON-encodable for the UI frame
        self.user_stop(started["command_id"])
        self.assertIsNone(self.host_command())

    def test_the_label_is_the_first_line_and_is_bounded(self):
        mark = self.mark("long command")
        started = self.run_command(f"sleep {mark} # " + "z" * 600)
        label = self.host_command()["label"]
        self.assertLessEqual(len(label), 200, "an unbounded label in every snapshot push")
        self.assertNotIn("\n", label)
        self.assertIn(mark, label)
        self.user_stop(started["command_id"])

    def test_the_command_is_shown_while_a_task_is_open_too(self):
        task = self.open_free_task()
        mark = self.mark("command during a free Task")
        started = self.run_command(f"sleep {mark}")
        self.assertEqual(self.host_command()["command_id"], started["command_id"])
        self.assertEqual(self.backend.snapshot()["task"]["task_id"], task.task_id)
        self.user_stop(started["command_id"])

    def test_the_state_reads_stopping_while_the_episode_runs_and_nothing_after_it(self):
        mark, started, log = self.trapping("a command the snapshot sees stopping")
        box, thread = self.user_stop_in_background(started["command_id"])
        seen: list = []

        def probe():
            shown = self.host_command()
            if shown is not None:
                seen.append(shown["state"])
            return "result" in box
        self.wait(probe, "the stop to finish", 60)
        self.assertIn("stopping", seen, f"the snapshot never showed the stop under way: {set(seen)}")
        self.assertTrue(set(seen) <= {"running", "stopping", "unknown"}, set(seen))
        self.assertIsNone(self.host_command())

    def test_a_program_the_user_started_in_the_shell_is_not_a_target(self):
        """FU-D08: only Workbench commands are targets; a foreground program the user typed is not listed."""
        mark = self.mark("user typed program")
        self.backend.admit(PaneId.HOST_SHELL, f"sleep {mark}\r".encode(), "input")
        self.wait(lambda: pids_with(mark), "the user's program to start")
        self.settle(0.4)
        self.assertIsNone(self.host_command(), "a program the user typed is offered to the stop key")
        self.assertEqual(self.backend.stop_command(str(uuid4()))["status"], "not_found")
        self.assertTrue(pids_with(mark), "the stop of an unknown id touched the user's program")
        self.backend.admit(PaneId.HOST_SHELL, b"\x03", "input")  # the user's own Ctrl+C ends it
        self.wait(lambda: not pids_with(mark), "Ctrl+C to end the user's program")

    def test_a_state_after_a_finished_command_does_not_resurrect_it(self):
        _, started = self.quick_command("short")
        self.assertIsNone(self.host_command())
        again = self.worker_terminal(command=None)
        self.assertNotEqual(again.get("status"), "running")
        self.assertIsNone(self.host_command())


class RawModeTests(UserBackendFixture):
    """The user's Ctrl+C is a byte on the tty; a command with ISIG off ignores it. The stop path does not need it."""

    def test_a_command_with_isig_off_survives_ctrl_c_and_is_ended_by_the_user_stop_and_the_shell_is_usable(self):
        mark = self.mark("raw mode command")
        flags = self.log_dir / f"{mark}.stty"
        started = self.run_command(f"stty -isig -icanon -echo; stty -a > {flags}; sleep {mark}")
        self.wait(lambda: flags.exists() and flags.read_text().strip(), "the command to record its tty flags")
        recorded = flags.read_text()
        self.assertIn("-isig", recorded.split(), "the scenario needs ISIG off")
        self.backend.admit(PaneId.HOST_SHELL, b"\x03", "input")
        self.settle(1.0)
        self.assertEqual(self.backend.terminal.current().get("running"), True,
                         "Ctrl+C ended a command that has ISIG off: the scenario is not the raw mode one")
        self.assertTrue(pids_with(mark), "the raw mode command is gone")
        result = self.user_stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(result["requested_by"], "user")
        self.assertEqual(pids_with(mark), [])
        # the host shell goes back to a usable state: the Worker's next command runs and finishes normally
        word = f"alive-{self.mark('after raw mode')}"
        follow = self.worker_terminal(command=f"echo {word}")
        if follow.get("status") == "running":
            self.wait(lambda: self.worker_terminal(command=None).get("status") == "exited", "the next command")
            follow = self.worker_terminal(command=None)
        self.assertEqual((follow["status"], follow["exit_code"]), ("exited", 0), follow)
        self.assertIsNone(self.host_command())


class ExperimentUserStopTests(exp.BackendFixture):
    """The user stops a Manager experiment command (owner manager, command_id = run_id)."""

    def user_stop(self, command_id, timeout=60):
        box: dict = {}
        thread = threading.Thread(target=lambda: box.update(result=self.backend.stop_command(command_id)))
        thread.start()
        self.addCleanup(thread.join, 60)
        self.wait(lambda: "result" in box, "the user's stop of the experiment", timeout)
        return box["result"]

    def test_the_experiment_command_is_shown_and_the_user_stop_closes_the_run_as_stopped(self):
        task_id, run_id = self.start_experiment()
        shown = self.backend.snapshot()["panes"]["host_shell"]["command"]
        self.assertEqual((shown["command_id"], shown["owner"], shown["state"]), (run_id, "manager", "running"))
        self.assertTrue(shown["label"], "the modal needs something to show")
        child = self.child()
        result = self.user_stop(run_id)
        self.assertEqual((result["status"], result["requested_by"], result["command_id"]), ("exited", "user", run_id))
        self.assertFalse(exp.alive(child), "the experiment's command is still alive")
        flow = self.backend.flow
        self.wait(lambda: flow.task_view()["status"] == "finished", "the Task to finish", 30)
        last = flow.task_view()["last_result"]
        self.assertEqual((last["run_id"], last["outcome"], last["run_closed"]), (run_id, "stopped", True), last)
        self.assertEqual(last["stopped_by"], "user", "a user stop is not attributed to the Manager")
        self.assertIsNone(self.backend.snapshot()["panes"]["host_shell"]["command"])
        records = self.journal()
        (requested,) = [r for r in records if r.get("type") == "command_stop_requested"]
        self.assertEqual((requested["command_id"], requested["requested_by"], requested["owner"], requested["kind"],
                          requested["task_id"], requested["reason"]),
                         (run_id, "user", "manager", "experiment", task_id, "user stop"))
        self.assertEqual([r for r in records if r.get("type") == "command_stop_refused"], [])
        self.assertEqual([r for r in records if r.get("type") == "command_interrupt"], [])
        ledger = [json.loads(line) for line in (self.backend.layout.workflow / "tasks-flow.jsonl").read_text()
                  .splitlines() if line.strip()]
        self.assertEqual([(r["outcome"], r["stopped_by"]) for r in ledger
                          if r["type"] == "run_ended" and r.get("run_id") == run_id], [("stopped", "user")])

    def test_the_user_may_stop_an_experiment_the_worker_may_not(self):
        task_id, run_id = self.start_experiment()
        refused = self.call(ActorRole.WORKER, "terminal", {"command": None, "stop": run_id})[0]
        self.assertEqual((refused["status"], refused["reason"]), ("refused", "not_owner"), refused)
        child = self.child()
        self.assertTrue(exp.alive(child))
        result = self.user_stop(run_id)
        self.assertEqual(result["status"], "exited", result)

    def test_a_second_user_stop_of_the_ended_experiment_is_already_exited_or_a_repeat_and_signals_nothing(self):
        task_id, run_id = self.start_experiment()
        self.user_stop(run_id)
        sent = len([r for r in self.journal() if r.get("type") == "command_stop_signal"])
        second = self.user_stop(run_id)
        self.assertTrue(second.get("repeat") or second["status"] in ("already_exited", "not_found"), second)
        self.assertEqual(len([r for r in self.journal() if r.get("type") == "command_stop_signal"]), sent)


# =============================================================================================================
# the whole chain through the backend's own ui_v1 socket
# =============================================================================================================
class ChainFixture(auth.Cs05Fixture):
    def setUp(self):
        super().setUp()
        self.ui_clients: list[UiClient] = []
        self.addCleanup(self._close_clients)

    def _close_clients(self):
        for client in self.ui_clients:
            client.close()

    def ui_client(self, *, attach=True):
        # the connect and the attach are answered by the backend loop, which this thread runs while it waits
        client = self.in_thread(lambda: UiClient(self.backend.layout.ui_socket, timeout=30))
        self.ui_clients.append(client)
        if attach:
            self.assertTrue(self.in_thread(lambda: client.attach((24, 80)))["ok"])
        return client

    def in_thread(self, fn):
        box: dict = {}

        def run():
            try:
                box["value"] = fn()
            except BaseException as exc:
                box["error"] = exc
        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 60)
        self.wait(lambda: box, "the client thread", 90)
        if "error" in box:
            raise box["error"]
        return box["value"]

    def frames_for(self, client, request_id, count, timeout=60):
        found = []
        end = time.monotonic() + timeout
        while len(found) < count and time.monotonic() < end:
            frame = client.next_frame(max(0.1, end - time.monotonic()))
            if frame.header.get("type") == "result" and frame.header.get("id") == request_id:
                found.append(frame.header)
        return found


class ChainTests(ChainFixture):
    def test_ack_then_final_then_state_push_for_a_real_command_that_ignores_signals(self):
        mark, started, log = self.trapping("a command stopped from the UI socket")
        cid = started["command_id"]
        client = self.ui_client()
        client.states.clear()
        request_id = f"u-{uuid4().hex[:8]}"
        client.send_frame({"v": 1, "type": "stop_command", "id": request_id, "command_id": cid})
        seen_states: list = []
        probe = self.ui_client(attach=False)
        got: list = []

        def collect():
            got.extend(self.frames_for(client, request_id, 2))
            return True
        thread = threading.Thread(target=collect)
        thread.start()
        self.addCleanup(thread.join, 60)
        served: list[float] = []

        def other_client_is_served():  # the loop is ticking this very thread: a blocked loop would not answer
            started_at = time.monotonic()
            seen_states.append(self.in_thread(lambda: probe.snapshot()["panes"]["host_shell"]["command"]))
            served.append(time.monotonic() - started_at)
            return len(got) == 2
        self.wait(other_client_is_served, "ack and final", 90)
        ack, final = got
        self.assertEqual((ack["ok"], ack["stop"]), (True, {"status": "stopping"}), ack)
        self.assertNotIn("final", ack)
        self.assertEqual((final["ok"], final["final"], final["command_id"]), (True, True, cid), final)
        self.assertEqual((final["stop"]["status"], final["stop"]["requested_by"]), ("exited", "user"), final)
        self.assertEqual([s["signal"] for s in final["stop"]["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertLess(max(served), 5.0, "another client waited a whole stop for its snapshot")
        self.assertTrue(len(served) >= 2, "the loop was not asked anything while the stop ran")
        self.assertIn("stopping", [s["state"] for s in seen_states if s], seen_states)
        self.wait(lambda: client.pump(0.05) is not None and client.states, "the state push after the final", 10)
        self.assertIsNone(client.states[-1]["panes"]["host_shell"]["command"], "the push still shows the command")
        self.assertEqual(self.records("command_stop_requested")[0]["requested_by"], "user")

    def test_a_client_that_leaves_after_the_ack_does_not_stop_the_stop_or_hurt_the_backend(self):
        mark, started, log = self.trapping("a command whose UI client leaves")
        cid = started["command_id"]
        client = self.ui_client()
        request_id = f"u-{uuid4().hex[:8]}"
        client.send_frame({"v": 1, "type": "stop_command", "id": request_id, "command_id": cid})
        (ack,) = self.in_thread(lambda: self.frames_for(client, request_id, 1))
        self.assertEqual(ack["stop"], {"status": "stopping"})
        client.close()
        self.wait(lambda: self.records("command_stop_finished"), "the episode to finish without its client", 60)
        self.assertEqual(self.records("command_stop_finished")[0]["status"], "exited")
        self.assertEqual(pids_with(mark), [])
        self.settle(0.5)
        fresh = self.ui_client()
        self.assertIn("panes", self.in_thread(fresh.snapshot))
        stray = [f for f in fresh.pending if f.header.get("final")]
        self.assertEqual(stray, [], "the final reached a later connection")

    def test_a_controller_exception_gives_an_error_final_leaves_the_command_alone_and_the_backend_serving(self):
        mark, started, log = self.trapping("a command whose stop raises")
        cid = started["command_id"]
        client = self.ui_client()
        request_id = f"u-{uuid4().hex[:8]}"
        with mock.patch.object(self.backend.command_stop, "stop", side_effect=RuntimeError("boom")):
            client.send_frame({"v": 1, "type": "stop_command", "id": request_id, "command_id": cid})
            ack, final = self.in_thread(lambda: self.frames_for(client, request_id, 2))
        self.assertEqual(ack["stop"], {"status": "stopping"})
        self.assertEqual((final["final"], final["stop"]["status"]), (True, "error"), final)
        self.assertNotIn("exited", json.dumps(final["stop"]))
        self.settle()
        self.assertEqual(self.signals_seen(log), 0)
        self.assertEqual(self.backend.terminal.current().get("running"), True)
        self.assertIn("panes", self.in_thread(client.snapshot), "the backend stopped serving")
        # and the very next stop of the same command, with the service healthy again, works
        again = f"u-{uuid4().hex[:8]}"
        client.send_frame({"v": 1, "type": "stop_command", "id": again, "command_id": cid})
        ack, final = self.in_thread(lambda: self.frames_for(client, again, 2))
        self.assertEqual(final["stop"]["status"], "exited", final)

    def test_a_stop_for_a_command_that_ended_meanwhile_is_final_already_exited_and_signals_nothing(self):
        mark = self.mark("short command")
        started = self.worker_terminal(command=f": {mark}; echo finished")
        self.assertEqual(started["status"], "exited")
        client = self.ui_client()
        request_id = f"u-{uuid4().hex[:8]}"
        client.send_frame({"v": 1, "type": "stop_command", "id": request_id, "command_id": started["command_id"]})
        ack, final = self.in_thread(lambda: self.frames_for(client, request_id, 2))
        self.assertEqual((final["final"], final["stop"]["status"], final["stop"]["requested_by"]),
                         (True, "already_exited", "user"), final)
        self.assertEqual(self.records("command_stop_signal"), [])

    def test_stop_command_from_an_unattached_connection_is_refused_and_signals_nothing(self):
        mark, started, log = self.trapping("a command an unattached client tries to stop")
        client = self.ui_client(attach=False)
        request_id = f"u-{uuid4().hex[:8]}"
        client.send_frame({"v": 1, "type": "stop_command", "id": request_id, "command_id": started["command_id"]})
        (answer,) = self.in_thread(lambda: self.frames_for(client, request_id, 1))
        self.assertEqual((answer["ok"], answer["reason"]), (False, "not_attached"), answer)
        self.settle()
        self.assertEqual(self.signals_seen(log), 0)
        self.assertEqual(self.stop_records(), [])


if __name__ == "__main__":
    unittest.main()
