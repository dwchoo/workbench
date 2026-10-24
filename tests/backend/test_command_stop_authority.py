"""CS-05: who may stop which command (Worker and Manager), and the stop paths around it.

Seam S3: ``CommandStopService`` with a fake provider (targets whose owner is ``manager``, the way the experiment
provider of CS-09 will register them); the service never reaches a process in these tests, an instrumented
``StopEnvironment`` counts every pidfd it is asked to open or signal.

Seam S2: a real ``Backend`` with fake OMPs and a real host shell PTY; the Manager's ``stop_command`` goes through
``Backend._tool_request`` and the Worker's ``terminal`` through the same entry.

Expected values are this file's own: the BRIEF authorization matrix, the exit codes 130/143/137 of the commands
the test starts (128 + the signal it expects), and the session ids and Task states the test creates. Every
process here is one the test started in its own host shell; the backend closes (and its host shell with it) on
teardown.
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
from uuid import uuid4

from workbench.backend.command_stop import CommandStopService, LinuxStopEnvironment, StopPolicy, StopTarget
from workbench.app.recovery import BOOT_HOLD
from workbench.contracts.v1 import ActorRole

from test_cd70_restart_worker import RestartWorkerFixture, alive
from command_stop_support import pids_running

SHORT = StopPolicy(identity_wait=0.2, sigint_wait=0.2, sigterm_wait=0.2, sigkill_wait=0.2, call_wait=3.0,
                   poll_interval=0.01)
ELSEWHERE = "stop_survivor"
# the test's own policy for real commands (production defaults are checked elsewhere)
REDUCED = StopPolicy(identity_wait=2.0, sigint_wait=1.5, sigterm_wait=1.0, sigkill_wait=1.0, call_wait=10.0)


class CountingEnvironment(LinuxStopEnvironment):
    """The real environment that counts every attempt to pin or signal a process."""

    def __init__(self):
        self.opened, self.sent = [], []

    def pidfd_open(self, pid):
        self.opened.append(pid)
        return super().pidfd_open(pid)

    def pidfd_send_signal(self, fd, signum):
        self.sent.append(signum)
        super().pidfd_send_signal(fd, signum)


class Registry:
    """A service over a recording journal, with fake targets whose identity provider counts its calls."""

    def __init__(self, **options):
        self.records: list[dict] = []
        self.env = CountingEnvironment()
        self.service = CommandStopService(journal=self.records.append, policy=SHORT, env=self.env, **options)
        self.identity_reads = 0

    def register(self, owner, kind="terminal", task_id=None, **extra):
        command_id = str(uuid4())

        def identity():
            self.identity_reads += 1
            return None
        self.service.register(StopTarget(command_id, owner, kind, task_id, identity=identity, **extra))
        return command_id

    def of_type(self, kind):
        return [record for record in self.records if record["type"] == kind]

    def assert_no_process_was_touched(self, test):
        test.assertEqual((self.env.opened, self.env.sent, self.identity_reads), ([], [], 0))
        test.assertEqual(self.of_type("command_stop_signal"), [])


class RefusalMatrixTests(unittest.TestCase):
    """Behavior 1: a refusal gives its reason, signals nothing and is recorded."""

    def test_a_worker_may_not_stop_a_command_the_manager_owns(self):
        registry = Registry()
        command_id = registry.register("manager", kind="experiment")
        result = registry.service.stop("worker", command_id)
        self.assertEqual((result["status"], result["reason"]), ("refused", "not_owner"), result)
        self.assertEqual((result["command_id"], result["requested_by"]), (command_id, "worker"))
        registry.assert_no_process_was_touched(self)
        refused = registry.of_type("command_stop_refused")
        self.assertEqual([(r["command_id"], r["requested_by"], r["status"], r["reason"]) for r in refused],
                         [(command_id, "worker", "refused", "not_owner")])
        self.assertEqual(registry.of_type("command_stop_requested"), [], "a refused request starts no stop")

    def test_a_worker_refused_for_an_ended_manager_command_is_told_nothing_about_its_end(self):
        registry = Registry()
        command_id = registry.register("manager", kind="experiment")
        registry.service.command_ended(command_id, {"status": "exited", "exit_code": 0, "signal": None})
        result = registry.service.stop("worker", command_id)
        self.assertEqual((result["status"], result["reason"]), ("refused", "not_owner"), result)
        self.assertNotIn("exit_code", result)
        registry.assert_no_process_was_touched(self)

    def test_an_unknown_command_id_is_not_found_for_either_requester(self):
        registry = Registry()
        for requester in ("worker", "manager"):
            command_id = str(uuid4())
            result = registry.service.stop(requester, command_id)
            self.assertEqual((result["status"], result["reason"], result["requested_by"]),
                             ("not_found", "unknown_command", requester), result)
            self.assertIn({"command_id": command_id, "requested_by": requester, "status": "not_found",
                           "reason": "unknown_command"},
                          [{k: r[k] for k in ("command_id", "requested_by", "status", "reason")}
                           for r in registry.of_type("command_stop_refused")])
        registry.assert_no_process_was_touched(self)

    def test_a_command_the_previous_backend_started_is_refused_and_points_to_stop_survivor(self):
        previous = str(uuid4())
        registry = Registry(previous_backend=lambda command_id: command_id == previous)
        for requester in ("worker", "manager"):
            result = registry.service.stop(requester, previous)
            self.assertEqual((result["status"], result["reason"], result["requested_by"]),
                             ("refused", "previous_backend_command", requester), result)
            self.assertIn(ELSEWHERE, result["detail"])
        refused = registry.of_type("command_stop_refused")
        self.assertEqual([(r["requested_by"], r["status"], r["reason"]) for r in refused],
                         [("worker", "refused", "previous_backend_command"),
                          ("manager", "refused", "previous_backend_command")])
        registry.assert_no_process_was_touched(self)
        self.assertEqual(registry.service.stop("manager", str(uuid4()))["status"], "not_found",
                         "an id the journal does not know stays not_found")

    def test_a_failing_previous_backend_lookup_reads_as_not_found_and_signals_nothing(self):
        def broken(_command_id):
            raise OSError("journal unreadable")
        registry = Registry(previous_backend=broken)
        result = registry.service.stop("manager", str(uuid4()))
        self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)
        registry.assert_no_process_was_touched(self)


class AllowedMatrixTests(unittest.TestCase):
    """The permitted half of the matrix: the request is accepted and recorded with the owner's role."""

    def accepted(self, requester, owner, kind):
        registry = Registry()
        command_id = registry.register(owner, kind=kind, task_id="task-1")
        result = registry.service.stop(requester, command_id, reason="because")
        self.assertNotEqual(result["status"], "refused", result)
        self.assertNotEqual(result.get("reason"), "not_owner", result)
        requested = registry.of_type("command_stop_requested")
        self.assertEqual([(r["command_id"], r["requested_by"], r["owner"], r["kind"], r["task_id"], r["reason"])
                          for r in requested], [(command_id, requester, owner, kind, "task-1", "because")])
        self.assertEqual(registry.of_type("command_stop_refused"), [])

    def test_the_worker_stops_its_own_command(self):
        self.accepted("worker", "worker", "terminal")

    def test_the_manager_stops_a_worker_command(self):
        self.accepted("manager", "worker", "terminal")

    def test_the_manager_stops_its_own_command(self):
        self.accepted("manager", "manager", "experiment")

    def test_the_task_status_provider_feeds_the_request_record_and_its_failure_does_not_stop_the_stop(self):
        def broken():
            raise RuntimeError("flow unavailable")
        for provider, expected in ((lambda: "closed", "closed"), (broken, None), (lambda: 5, None)):
            registry = Registry()
            command_id = registry.register("worker", task_id="task-9", task_status=provider)
            result = registry.service.stop("worker", command_id)
            self.assertNotEqual(result["status"], "refused", result)
            (requested,) = registry.of_type("command_stop_requested")
            self.assertEqual((requested["task_id"], requested["task_status"]), ("task-9", expected))

    def test_a_command_without_a_task_records_no_task_status(self):
        registry = Registry()
        command_id = registry.register("worker", task_status=lambda: "closed")
        registry.service.stop("worker", command_id)
        (requested,) = registry.of_type("command_stop_requested")
        self.assertEqual((requested["task_id"], requested["task_status"]), (None, None))

    def test_nobody_else_is_a_requester(self):
        registry = Registry()
        command_id = registry.register("worker")
        for requester in ("", "system"):  # "user" became a requester with the UI stop key (CS-O10)
            result = registry.service.stop(requester, command_id)
            self.assertEqual((result["status"], result["reason"]), ("refused", "not_owner"), (requester, result))
        registry.assert_no_process_was_touched(self)


class BackendStopFixture(RestartWorkerFixture):
    """A real Backend (fake OMPs, real host shell) whose Worker runs commands through the ``terminal`` tool."""

    def setUp(self):
        super().setUp()
        self.backend.command_stop.policy = REDUCED
        self.backend.terminal._wait_seconds = 1
        self.handoffs_path = self.backend.layout.workflow / "handoffs.jsonl"

    def notices_of(self, kind):
        return [item["notice"] for item in self.received() if item["role"] == "worker"
                and item["notice"]["type"] == kind]

    def open_free_task(self):
        """A free-work Task the Manager sends through the real flow (no command list: the Worker's terminal is
        not restricted), once the fake Worker OMP has it."""
        result = self.call(ActorRole.MANAGER, "to_worker",
                           {"kind": "work", "message": "collect facts", "spec": {"goal": "g", "paths": []}})[0]
        self.assertEqual(result["status"], "dispatched", result)
        flow_task = self.backend.flow.tasks[result["task_id"]]
        self.wait(lambda: flow_task.status == "running", "the Task to run")
        return flow_task

    def worker_terminal(self, **args):
        return self.call(ActorRole.WORKER, "terminal", {"command": None, **args})[0]

    def run_command(self, command):
        started = self.worker_terminal(command=command)
        self.assertEqual(started["status"], "running", started)
        self.addCleanup(self.end_command, started["command_id"])
        return started

    def end_command(self, command_id):
        """The test's own command must not outlive it: stop it again if it still runs (idempotent)."""
        if self.backend.terminal.current().get("running"):
            self.backend.command_stop.stop("worker", command_id)

    def manager_stop(self, command_id, reason="the user asked to stop it", **extra):
        return self.call(ActorRole.MANAGER, "stop_command", {"command_id": command_id, "reason": reason, **extra})[0]

    def records(self, *types):
        lines = self.handoffs_path.read_text().splitlines() if self.handoffs_path.exists() else []
        found = [json.loads(line) for line in lines if line.strip()]
        return [r for r in found if r.get("type") in types] if types else found


class ManagerStopCommandTests(BackendStopFixture):
    """Behavior 2: the Manager's stop_command escalates without any user approval and records requested_by."""

    def test_a_sigint_ignoring_command_is_taken_down_by_sigterm_with_requester_manager(self):
        started = self.run_command("trap '' INT; sleep 611.1")
        (pid,) = pids_running("sleep", "611.1")
        result = self.manager_stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"], result["signal_name"]), (128 + 15, 15, "SIGTERM"))
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT", "SIGTERM"])
        self.assertEqual((result["command_id"], result["requested_by"]), (started["command_id"], "manager"))
        self.assertFalse(alive(pid), "the command's process is gone")
        self.assertEqual(self.backend.handoffs.pending_approvals(), [], "no user approval was asked for")
        self.assertFalse([r for r in self.records() if "approval" in r.get("type", "")])
        requested = self.records("command_stop_requested")
        self.assertEqual([(r["command_id"], r["requested_by"], r["owner"], r["kind"]) for r in requested],
                         [(started["command_id"], "manager", "worker", "terminal")])

    def test_a_command_that_ignores_sigint_and_sigterm_is_killed(self):
        started = self.run_command("trap '' INT TERM; while :; do sleep 0.2; done")
        result = self.manager_stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal_name"]), (128 + 9, "SIGKILL"), result)
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertEqual(result["requested_by"], "manager")

    def test_the_worker_may_not_call_stop_command(self):
        started = self.run_command("sleep 611.2")
        result = self.call(ActorRole.WORKER, "stop_command",
                           {"command_id": started["command_id"], "reason": "the worker tries"})[0]
        self.assertEqual((result["status"], result["reason"]), ("rejected", "tool_not_allowed_for_role"), result)
        self.assertTrue(pids_running("sleep", "611.2"), "nothing was signalled")
        self.assertEqual(self.records("command_stop_requested", "command_stop_signal"), [])
        self.assertEqual(self.manager_stop(started["command_id"])["status"], "exited")  # the test ends its command

    def test_invalid_arguments_are_named_by_field_and_nothing_is_signalled(self):
        started = self.run_command("sleep 611.3")
        good = started["command_id"]
        for args, field in (({"command_id": good}, "reason"), ({"reason": "x"}, "command_id"),
                            ({"command_id": good, "reason": "   "}, "reason"),
                            ({"command_id": good, "reason": "x" * 501}, "reason"),
                            ({"command_id": good.upper(), "reason": "x"}, "command_id"),
                            ({"command_id": "not-a-uuid", "reason": "x"}, "command_id"),
                            ({"command_id": good, "reason": "x", "force": True}, "force")):
            with self.subTest(args={k: v for k, v in args.items() if k != "command_id"}):
                result = self.call(ActorRole.MANAGER, "stop_command", args)[0]
                self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), result)
                self.assertTrue(any(error.startswith(field) for error in result["errors"]), result)
                self.assertNotIn("not-a-uuid", json.dumps(result), "the value sent is not echoed")
        self.assertTrue(pids_running("sleep", "611.3"), "nothing was signalled")
        self.assertEqual(self.manager_stop(good)["status"], "exited")

    def test_a_reason_holding_an_environment_value_is_refused_and_not_stored(self):
        secret = "hunter2-hunter2-hunter2"
        self.backend.environment = {**self.backend.environment, "WB_STOP_API_TOKEN": secret}
        started = self.run_command("sleep 611.4")
        result = self.manager_stop(started["command_id"], reason=f"it printed {secret}")
        self.assertEqual((result["status"], result["reason"]), ("rejected", "environment_value"), result)
        self.assertNotIn(secret, json.dumps(result))
        self.assertTrue(pids_running("sleep", "611.4"), "nothing was signalled")
        self.assertNotIn(secret, self.handoffs_path.read_text(), "the value is never written to the journal")
        self.assertEqual(self.manager_stop(started["command_id"])["status"], "exited")

    def test_the_reason_is_recorded_with_the_request(self):
        started = self.run_command("sleep 611.5")
        self.manager_stop(started["command_id"], reason="  the user said it hangs  ")
        (requested,) = self.records("command_stop_requested")
        self.assertEqual(requested["reason"], "the user said it hangs")

    def test_a_resent_call_key_gets_the_cached_result_and_signals_nothing_new(self):
        started = self.run_command("sleep 611.6")
        args = {"command_id": started["command_id"], "reason": "hangs"}
        first = self.call(ActorRole.MANAGER, "stop_command", args, call="stop-call-1")[0]
        self.assertEqual(first["status"], "exited", first)
        signals = self.records("command_stop_signal")
        again = self.call(ActorRole.MANAGER, "stop_command", args, call="stop-call-1")[0]
        self.assertEqual(again, first)
        self.assertEqual(self.records("command_stop_signal"), signals)
        self.assertEqual(len(self.records("command_stop_requested")), 1)

    def test_a_second_call_for_the_same_command_is_a_repeat_with_the_original_result(self):
        started = self.run_command("sleep 611.7")
        first = self.manager_stop(started["command_id"])
        second = self.manager_stop(started["command_id"], reason="again")
        self.assertEqual(second["status"], "exited", second)
        self.assertIs(second.get("repeat"), True)
        self.assertEqual((second["exit_code"], second["requested_by"]), (first["exit_code"], "manager"))
        self.assertEqual(len(self.records("command_stop_signal")), len(first["signals"]), "no new signal")

    def test_an_interim_stopping_result_is_not_cached_for_the_call_key(self):
        started = self.run_command("trap '' INT TERM; while :; do sleep 0.2; done")
        self.backend.command_stop.policy = StopPolicy(identity_wait=2.0, sigint_wait=0.6, sigterm_wait=0.6,
                                                      sigkill_wait=1.0, call_wait=0.3)
        args = {"command_id": started["command_id"], "reason": "hangs"}
        interim = self.call(ActorRole.MANAGER, "stop_command", args, call="stop-call-2")[0]
        self.assertEqual(interim["status"], "stopping", interim)
        self.backend.command_stop.policy = REDUCED
        final = self.call(ActorRole.MANAGER, "stop_command", args, call="stop-call-2")[0]
        self.assertEqual(final["status"], "exited", final)
        self.assertIs(final.get("repeat"), True, "the resend joined the running episode")


class JoinedEpisodeTests(BackendStopFixture):
    """A command has one episode: a second requester joins it and reads the original result."""

    SLOW_INT = "trap 'sleep 1; exit 130' INT; echo ready; while :; do sleep 0.2; done"

    def test_a_manager_stop_joining_a_workers_episode_gets_the_workers_result_as_a_repeat(self):
        started = self.run_command(self.SLOW_INT)
        peer = self.backend.bridge.peer(ActorRole.WORKER)
        request = {"request_id": str(uuid4()), "tool_call_id": f"call-{uuid4()}", "tool": "terminal",
                   "args": {"command": None, "stop": started["command_id"]}, "session_id": peer.session_id,
                   "generation": peer.generation}
        box = {}
        thread = threading.Thread(target=lambda: box.update(result=self.backend._tool_request(peer, request)),
                                  daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.wait(lambda: self.backend.command_stop.surface(started["command_id"]) is not None,
                  "the worker's stop to be under way")
        joined = self.manager_stop(started["command_id"], reason="me too")
        self.wait(lambda: "result" in box, "the worker's stop result")
        original = box["result"]
        self.assertEqual((original["status"], original["exit_code"], original["requested_by"]),
                         ("exited", 130, "worker"), original)
        self.assertNotIn("repeat", original)
        self.assertEqual((joined["status"], joined["exit_code"], joined["requested_by"], joined.get("repeat")),
                         ("exited", 130, "worker", True), joined)
        self.assertEqual([item["signal"] for item in joined["signals"]], ["SIGINT"], "one episode, one signal")
        after = self.manager_stop(started["command_id"], reason="and again")
        self.assertEqual((after["requested_by"], after.get("repeat"), after["exit_code"]), ("worker", True, 130))
        self.assertEqual(len(self.records("command_stop_signal")), 1)
        self.assertEqual(self.worker_terminal()["stopped_by"], "worker", "the command's stopper is the original one")


class StoppedByTests(BackendStopFixture):
    """Behavior 3: what the Worker reads about a command the Manager stopped says who stopped it."""

    def test_the_workers_result_notice_and_commands_run_say_the_manager_stopped_it(self):
        task = self.open_free_task()
        started = self.run_command("sleep 612.1")
        stopped = self.manager_stop(started["command_id"])
        self.assertEqual((stopped["status"], stopped["exit_code"]), ("exited", 130), stopped)
        # the completion notice the Worker's OMP receives (before any fetch consumes it)
        self.wait(lambda: self.notices_of("terminal_done"), "the terminal_done notice")
        (done,) = self.notices_of("terminal_done")
        self.assertEqual((done["command_id"], done["exit_code"], done["stopped_by"], done["stop_signal"]),
                         (started["command_id"], 130, "manager", "SIGINT"))
        # the Worker's own query of the finished command
        fetched = self.worker_terminal()
        self.assertEqual((fetched["status"], fetched["command_id"]), ("exited", started["command_id"]))
        self.assertEqual((fetched["stopped_by"], fetched["stop_signal"]), ("manager", "SIGINT"))
        # the journal and the Task's commands_run
        (ended,) = self.records("terminal_ended")
        self.assertEqual((ended["stopped_by"], ended["stop_signal"]), ("manager", "SIGINT"))
        runs = self.backend.terminal.runs_for_task(task.task_id)
        (entry,) = list(runs)
        self.assertEqual((entry["status"], entry["exit_code"], entry["stopped_by"], entry["stop_signal"]),
                         ("exited", 130, "manager", "SIGINT"))

    def test_a_command_that_ended_by_itself_has_no_stop_marker(self):
        task = self.open_free_task()
        self.backend.terminal._wait_seconds = 20
        finished = self.worker_terminal(command="echo ends-by-itself")
        self.assertEqual((finished["status"], finished["exit_code"]), ("exited", 0), finished)
        self.assertNotIn("stopped_by", finished)
        self.assertNotIn("stop_signal", finished)
        (entry,) = list(self.backend.terminal.runs_for_task(task.task_id))
        self.assertNotIn("stopped_by", entry)

    def test_the_worker_stopping_its_own_command_is_marked_worker(self):
        started = self.run_command("sleep 612.2")
        stopped = self.worker_terminal(command=None, stop=started["command_id"])
        self.assertEqual((stopped["status"], stopped["requested_by"]), ("exited", "worker"), stopped)
        fetched = self.worker_terminal()
        self.assertEqual((fetched["stopped_by"], fetched["stop_signal"]), ("worker", "SIGINT"))

    def test_the_marker_names_the_last_signal_sent(self):
        started = self.run_command("trap '' INT; sleep 612.3")
        stopped = self.manager_stop(started["command_id"])
        self.assertEqual([item["signal"] for item in stopped["signals"]], ["SIGINT", "SIGTERM"])
        fetched = self.worker_terminal()
        self.assertEqual((fetched["exit_code"], fetched["stopped_by"], fetched["stop_signal"]),
                         (143, "manager", "SIGTERM"))


class RestartedWorkerStopsOldCommandTests(BackendStopFixture):
    """Behavior 4: ownership is the role, so a new Worker session stops what the previous session started."""

    def restart(self):
        old = self.backend.bridge.peer(ActorRole.WORKER)
        result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "the worker lost its context"})
        self.assertEqual(result["status"], "restarted", result)
        self.wait(lambda: self.backend.bridge.peer(ActorRole.WORKER) is not None
                  and self.backend.bridge.peer(ActorRole.WORKER).session_id != old.session_id,
                  "the new worker session")
        return old, self.backend.bridge.peer(ActorRole.WORKER)

    def test_the_new_worker_session_stops_a_command_the_previous_session_started(self):
        started = self.run_command("sleep 613.1")
        (pid,) = pids_running("sleep", "613.1")
        old, new = self.restart()
        self.assertNotEqual(old.session_id, new.session_id)
        self.assertTrue(alive(pid), "the command kept running across the restart")
        result = self.worker_terminal(command=None, stop=started["command_id"])
        self.assertEqual((result["status"], result["exit_code"], result["requested_by"]), ("exited", 130, "worker"),
                         result)
        self.assertFalse(alive(pid))
        (requested,) = self.records("command_stop_requested")
        self.assertEqual((requested["command_id"], requested["owner"], requested["requested_by"]),
                         (started["command_id"], "worker", "worker"))
        self.assertEqual(self.records("command_stop_refused"), [])
        stop_call = [r for r in self.records("terminal_request") if r.get("stop") == started["command_id"]]
        self.assertEqual([r["key"]["session_id"] for r in stop_call], [new.session_id], "called by the new session")

    def test_the_manager_stops_it_after_the_restart_too(self):
        started = self.run_command("sleep 613.2")
        self.restart()
        result = self.manager_stop(started["command_id"])
        self.assertEqual((result["status"], result["exit_code"], result["requested_by"]), ("exited", 130, "manager"))
        (requested,) = self.records("command_stop_requested")
        self.assertEqual(requested["owner"], "worker", "the owner is a role, not the session that started it")


class PreviousBackendCommandTests(BackendStopFixture):
    """Behavior 1 on the real wiring: the journal's started-only commands are a previous backend's."""

    def previous_backend_command(self, seconds, *, ended=False):
        """A process this test starts outside the host shell, journaled the way a previous backend left it."""
        process = subprocess.Popen(["sleep", seconds], start_new_session=True)
        self.addCleanup(lambda: (process.poll() is None and process.kill(), process.wait()))
        command_id = str(uuid4())
        self.backend.handoffs.record({"type": "terminal_started", "command_id": command_id, "task_id": None,
                                      "command": "sleep " + seconds, "cwd": "/tmp", "started_at": "2026-10-20T10:00:00Z",
                                      "log_path": "/tmp/none.log"})
        if ended:
            self.backend.handoffs.record({"type": "terminal_ended", "command_id": command_id, "status": "exited",
                                          "exit_code": 0, "signal": None})
        return command_id, process

    def test_neither_requester_can_stop_a_command_only_the_journal_remembers(self):
        command_id, process = self.previous_backend_command("613.5")
        worker = self.worker_terminal(command=None, stop=command_id)
        manager = self.manager_stop(command_id)
        for result, requester in ((worker, "worker"), (manager, "manager")):
            self.assertEqual((result["status"], result["reason"], result["requested_by"]),
                             ("refused", "previous_backend_command", requester), result)
            self.assertIn("stop_survivor", result["detail"])
        self.assertIsNone(process.poll(), "the process was not signalled")
        refused = [(r["requested_by"], r["status"], r["reason"]) for r in self.records("command_stop_refused")
                   if r["command_id"] == command_id]
        self.assertEqual(refused, [("worker", "refused", "previous_backend_command"),
                                   ("manager", "refused", "previous_backend_command")])
        self.assertEqual(self.records("command_stop_requested", "command_stop_target", "command_stop_signal"), [])

    def test_a_journaled_command_that_ended_is_not_a_survivor(self):
        command_id, process = self.previous_backend_command("613.6", ended=True)
        result = self.manager_stop(command_id)
        self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)
        self.assertIsNone(process.poll())

    def test_an_id_the_journal_never_saw_is_not_found(self):
        result = self.manager_stop(str(uuid4()))
        self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)


class RegistrationOrderTests(BackendStopFixture):
    def test_a_command_is_registered_for_stop_before_its_start_is_journaled(self):
        seen = []
        register = self.backend.command_stop.register

        def spy(target):
            seen.append((target.command_id, [r["command_id"] for r in self.records("terminal_started")]))
            register(target)
        self.backend.command_stop.register = spy
        started = self.run_command("sleep 616.1")
        self.assertEqual([item[0] for item in seen], [started["command_id"]])
        self.assertNotIn(started["command_id"], seen[0][1], "the start was not journaled yet at registration")
        self.assertEqual(self.manager_stop(started["command_id"])["status"], "exited")


class TaskStatusGetterTests(BackendStopFixture):
    def test_task_status_reads_a_tasks_status_by_id(self):
        flow = self.backend.flow
        task = self.open_free_task()
        self.assertEqual(flow.task_status(task.task_id), "running")
        self.assertIsNone(flow.task_status(str(uuid4())))
        self.call(ActorRole.MANAGER, "to_worker", {"kind": "work", "message": "stop", "cancel": True,
                                                  "task_id": task.task_id})
        self.assertEqual(flow.task_status(task.task_id), "closed")


class ClosedTaskStopTests(BackendStopFixture):
    """Behavior 5: a cancelled Task's remaining command is still stoppable; the Task stays closed."""

    def cancel(self, task):
        result, _ = self.call(ActorRole.MANAGER, "to_worker", {"kind": "work", "message": "the user changed plans",
                                                              "cancel": True, "task_id": task.task_id})
        self.assertEqual(result.get("status"), "cancelled", result)
        self.assertEqual(result["task_id"], task.task_id)
        return result

    def test_the_worker_stops_a_command_of_a_cancelled_task_without_reopening_it(self):
        task = self.open_free_task()
        started = self.run_command("sleep 614.1")
        cancelled = self.cancel(task)
        self.assertEqual([item["command_id"] for item in cancelled["host_commands"]], [started["command_id"]])
        self.assertEqual(self.backend.flow.tasks[task.task_id].status, "closed")
        result = self.worker_terminal(command=None, stop=started["command_id"])
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), result)
        flow_task = self.backend.flow.tasks[task.task_id]
        self.assertEqual(flow_task.status, "closed", "the stop does not reopen the Task")
        self.assertIsNone(self.backend.flow.active(), "and no Task became active")
        (requested,) = self.records("command_stop_requested")
        self.assertEqual((requested["command_id"], requested["requested_by"], requested["task_id"],
                          requested["task_status"]), (started["command_id"], "worker", task.task_id, "closed"))
        # a new command is a plain command again: it is not attributed to the closed Task
        self.backend.terminal._wait_seconds = 20
        later = self.worker_terminal(command="echo after-the-stop")
        self.assertEqual((later["status"], later["exit_code"]), ("exited", 0), later)
        starts = {r["command_id"]: r for r in self.records("terminal_started")}
        self.assertIsNone(starts[later["command_id"]]["task_id"])
        self.assertEqual(self.backend.flow.tasks[task.task_id].status, "closed")
        follow_up = self.call(ActorRole.MANAGER, "to_worker", {"kind": "work", "message": "continue",
                                                              "task_id": task.task_id})[0]
        self.assertEqual(follow_up["reason"], "task_closed", "the closed Task takes no new work")

    def test_the_manager_stops_it_too_and_an_open_task_is_recorded_with_its_status(self):
        task = self.open_free_task()
        first = self.run_command("sleep 614.2")
        self.assertEqual(self.manager_stop(first["command_id"])["status"], "exited")
        self.cancel(task)
        second = self.run_command("sleep 614.3")
        self.assertEqual(self.manager_stop(second["command_id"])["status"], "exited")
        requested = {r["command_id"]: r for r in self.records("command_stop_requested")}
        self.assertEqual((requested[first["command_id"]]["task_id"], requested[first["command_id"]]["task_status"]),
                         (task.task_id, "running"), "an open Task is recorded with its own status")
        self.assertEqual(requested[second["command_id"]]["requested_by"], "manager")

    def test_a_command_without_a_task_has_no_task_status(self):
        started = self.run_command("sleep 614.4")
        self.manager_stop(started["command_id"])
        (requested,) = self.records("command_stop_requested")
        self.assertEqual((requested["task_id"], requested["task_status"]), (None, None))


class StopWhilePausedOrHeldTests(BackendStopFixture):
    """Behavior 6: neither the automation pause nor the boot-confirmation hold stops a stop; new work stays held."""

    def pause(self):
        self.backend.pause()
        self.assertTrue(self.backend._automation_paused())

    def hold(self):
        self.backend.hold.set(BOOT_HOLD, detail="boot not confirmed")
        self.assertEqual(self.backend._hold_reason(ActorRole.WORKER), BOOT_HOLD)

    def assert_task_is_held(self):
        task = self.call(ActorRole.MANAGER, "to_worker", {"kind": "work", "message": "more",
                                                         "spec": {"goal": "g", "paths": []}})[0]
        self.assertEqual(task["status"], "held", task)

    def assert_new_command_is(self, status):
        """Asked once no command runs any more: a held command is refused before it is typed anywhere."""
        marker = f"/tmp/wb-cs05-never-{uuid4().hex[:8]}"
        refused = self.worker_terminal(command=f"touch {marker}")
        self.assertEqual(refused["status"], status, refused)
        self.assertFalse(Path(marker).exists(), "a held command never ran")

    def test_both_requesters_stop_commands_while_automation_is_paused(self):
        mine = self.run_command("sleep 615.1")
        self.pause()
        self.assert_task_is_held()
        stopped = self.worker_terminal(command=None, stop=mine["command_id"])
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["requested_by"]),
                         ("exited", 130, "worker"), stopped)
        self.assert_new_command_is("paused")
        self.backend.resume(True)
        self.wait(lambda: not self.backend._automation_paused(), "the resume")
        theirs = self.run_command("sleep 615.2")
        self.pause()
        stopped = self.manager_stop(theirs["command_id"])
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["requested_by"]),
                         ("exited", 130, "manager"), stopped)
        self.assert_new_command_is("paused")
        self.assert_task_is_held()
        self.assertTrue(self.backend._automation_paused(), "the stop did not resume automation")

    def test_the_manager_stops_a_command_before_the_boot_is_confirmed(self):
        mine = self.run_command("sleep 615.3")
        self.hold()
        self.assert_task_is_held()
        stopped = self.manager_stop(mine["command_id"])
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["requested_by"]),
                         ("exited", 130, "manager"), stopped)
        self.assert_new_command_is("held")
        self.assert_task_is_held()
        self.assertEqual(self.backend._hold_reason(ActorRole.WORKER), BOOT_HOLD, "the stop did not clear the hold")

    def test_the_worker_stops_its_command_before_the_boot_is_confirmed(self):
        mine = self.run_command("sleep 615.4")
        self.hold()
        stopped = self.worker_terminal(command=None, stop=mine["command_id"])
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["requested_by"]),
                         ("exited", 130, "worker"), stopped)
        self.assert_new_command_is("held")


if __name__ == "__main__":
    unittest.main()
