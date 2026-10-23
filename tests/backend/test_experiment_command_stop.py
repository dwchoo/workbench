"""CS-09: the Manager stops a running experiment's host command; the run closes as ``stopped``.

Seam S1/S2: ``TaskFlow`` on the ``test_task_flow`` fixture pattern with the real ``TaskWorkflow`` on a real host
``ShellPane`` through ``HostShellPort``; the experiment command is a real process in that shell, and the Manager's
stop is a real ``CommandStopService.stop("manager", run_id)`` (SIGINT -> SIGTERM -> SIGKILL through pidfds). Seam S7
for the one case that needs a command to end between the stop request and its first signal.

Expected values are this file's own: the experiment spec/command the test creates, the run events read directly
from the ``TaskRepository`` (``completed`` + details), ``task_view()['last_result']``, the exit code 128+n of the
signal the test expects, and the number of calls of the judgment stage (``WorkflowRun.judge``), which a stopped run
must never make. Every process here is one the test started in its own host shell; the shell dies with the pane.
"""

from __future__ import annotations

import functools
import gc
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from unittest.mock import Mock
import weakref

from workbench.backend.command_stop import CommandStopService, LinuxStopEnvironment, StopPolicy
from workbench.backend.flow import HandoffService
from workbench.backend.flow_tasks import ExperimentPorts, TaskFlow
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.backend.service import Backend
from workbench.contracts.v1 import ActorRole, PaneId
from workbench.tasks.repository import TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.workflow.run import TaskWorkflow, WorkflowRun

from test_cd70_restart_worker import RestartWorkerFixture
from test_command_stop_outcomes import OutcomeFixture
from test_experiment_command_stop_independent import identity_with_child
from test_task_flow import AUTOMATION, FakeMailbox, PublicMailboxFixture, git, request, wait_until



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


# the test's own policy for real commands (production defaults are checked elsewhere)
REDUCED = StopPolicy(identity_wait=3.0, sigint_wait=3.0, sigterm_wait=2.0, sigkill_wait=2.0, call_wait=15.0)
SLEEPING = "printf 'started\\n'; sleep 120; printf PASS > outcome.txt"


class ExperimentStopFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cs09-exp-stop-", dir=tempfile.gettempdir())
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.email", "fixture@example.invalid")
        git(self.source, "config", "user.name", "CS09 Fixture")
        (self.source / "tracked.txt").write_text("base\n")
        git(self.source, "add", "tracked.txt")
        git(self.source, "commit", "-qm", "baseline")
        self.commit = git(self.source, "rev-parse", "HEAD")
        for name in ("workflow", "worktrees", "runs"):
            (self.root / name).mkdir(mode=0o700)
        self.db = self.root / "tasks.sqlite3"
        with TaskRepository(self.db):
            pass
        self.pane = ShellPane(ShellChoice("bash", "/usr/bin/bash"),
                              {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "LANG": "C.UTF-8"})
        self.halt = threading.Event()
        self.loop = threading.Thread(target=self._pump, daemon=True)
        self.loop.start()
        self.records: list[dict] = []  # the stop journal
        self.judge_calls = 0
        self.run_refs: list[weakref.ref] = []
        self.port_refs: list[weakref.ref] = []
        original = WorkflowRun.judge

        @functools.wraps(original)
        def counting(run, *args, **kwargs):
            self.judge_calls += 1
            return original(run, *args, **kwargs)
        patcher = mock.patch.object(WorkflowRun, "judge", counting)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = self.make_env()
        self.stop_service = self.new_stop_service(self.env)
        self.addCleanup(self.stop_service.close)
        self.service, self.flow = self.start_flow(self.stop_service)

    def _pump(self):
        while not self.halt.is_set():
            self.pane.pump()
            time.sleep(0.01)

    def tearDown(self):
        self.halt.set()
        self.loop.join(5)
        self.pane.close()
        self.tmp.cleanup()

    def make_env(self):
        return CountingEnvironment()

    def new_stop_service(self, env):
        return CommandStopService(journal=self.records.append, policy=REDUCED, env=env)

    def host_shell(self):
        port = HostShellPort(self.pane, lambda: self.pane)
        self.port_refs.append(weakref.ref(port))
        return port

    def make_workflow(self, repository):
        workflow = TaskWorkflow(repository, PublicMailboxFixture(repository))
        fixture, start = self, workflow.start

        @functools.wraps(start)
        def tracked_start(*args, **kwargs):
            run = start(*args, **kwargs)
            fixture.run_refs.append(weakref.ref(run))
            return run
        workflow.start = tracked_start
        return workflow

    def start_flow(self, stop_service):
        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=FakeMailbox())
        ports = ExperimentPorts(host_shell=self.host_shell, make_workflow=self.make_workflow,
                                automation=lambda: AUTOMATION, environment_names=lambda: {"PATH"},
                                worktrees_root=self.root / "worktrees", artifacts_root=self.root / "runs")
        flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl", repository_factory=lambda: TaskRepository(self.db),
                        handoffs=service, omp_idle=lambda role: True, experiment=ports, poll_interval=0.05,
                        collect_slice=0.2)
        flow.command_stop = stop_service
        service.configure(policy=flow)
        service.start()
        flow.start()
        self.addCleanup(service.close)
        self.addCleanup(flow.close)
        self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt"))
        self.calls = 0
        return service, flow

    # -- the Manager's and Worker's tool calls -----------------------------------------------------------
    def to_worker(self, args):
        self.calls += 1
        return self.service.handle(ActorRole.MANAGER, request("to_worker", args, f"m-{self.calls}"))

    def spec(self, command):
        return {"goal": "fixture run", "paths": ["outcome.txt"], "execution": {
            "source": str(self.source), "commit": self.commit, "command": command,
            "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
            "environment": ["PATH"], "shell": "bash"}}

    def running_experiment(self, command=SLEEPING):
        """Create the experiment; return ``(task_id, run_id)`` once its command is running in the host shell."""
        result = self.to_worker({"kind": "experiment", "message": "run the fixture", "spec": self.spec(command)})
        self.assertEqual(result["status"], "dispatched", result)
        task_id = result["task_id"]
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running"
                                   and self.flow.task_view()["run_id"] is not None, 30), self.flow.task_view())
        run_id = self.flow.task_view()["run_id"]
        self.assertTrue(wait_until(lambda: "started" in [e["kind"] for e in self.shell_history(run_id)], 10),
                        self.shell_history(run_id))
        return task_id, run_id

    def shell_history(self, run_id):
        with TaskRepository(self.db) as repository:
            return repository.get_shell_history(run_id)

    def run_history(self, run_id):
        with TaskRepository(self.db) as repository:
            return repository.get_run_history(run_id)

    def current_run(self, task_id):
        with TaskRepository(self.db) as repository:
            return repository.get_current_run(task_id)

    def view(self):
        return self.flow.task_view()

    def of_type(self, kind):
        return [record for record in self.records if record["type"] == kind]

    def ledger(self):
        path = self.root / "workflow" / "tasks-flow.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class StoppedRunTests(ExperimentStopFixture):
    """Behavior 1: a Manager stop with no cancel closes the run as ``stopped``, without a judgment."""

    def test_a_manager_stop_ends_the_command_and_closes_the_run_as_stopped_without_analysis(self):
        task_id, run_id = self.running_experiment()
        stopped = self.stop_service.stop("manager", run_id, reason="the benchmark is not needed any more")
        self.assertEqual((stopped["status"], stopped["requested_by"], stopped["command_id"]),
                         ("exited", "manager", run_id), stopped)
        self.assertEqual((stopped["exit_code"], stopped["signal"], stopped["signal_name"]), (130, 2, "SIGINT"), stopped)
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", 30), self.view())
        task = self.view()
        self.assertEqual((task["active"], task["run_id"]), (False, None))
        self.assertEqual(task["last_result"], {"run_id": run_id, "outcome": "stopped", "stopped_by": "manager",
                                                "exit_code": 130, "signal": 2, "run_closed": True})
        history = self.run_history(run_id)
        self.assertEqual(history[-1]["kind"], "completed")
        self.assertEqual({key: history[-1]["details"].get(key) for key in ("outcome", "stopped_by", "judgment")},
                         {"outcome": "stopped", "stopped_by": "manager", "judgment": None})
        self.assertIsNone(self.current_run(task_id), "the run is closed in the repository")
        ended = [r for r in self.ledger() if r["type"] == "run_ended" and r["run_id"] == run_id]
        self.assertEqual([(r["outcome"], r["stopped_by"]) for r in ended], [("stopped", "manager")])
        self.assertEqual(self.judge_calls, 0, "a stopped run is never analysed")
        time.sleep(0.5)  # nothing else follows: no re-run, no second run, no report
        self.assertEqual(self.flow.tasks[task_id].runs_started, 1)
        self.assertIsNone(self.flow.tasks[task_id].pending_start)
        self.assertEqual(self.view()["status"], "finished")
        self.assertEqual(self.flow.worker_view(), {"state": "idle", "task_id": None})
        self.assertEqual(self.judge_calls, 0)


class OwnerTests(ExperimentStopFixture):
    """Behavior 2: the experiment command is the Manager's; the Worker's stop is refused and signals nothing."""

    def test_a_worker_may_not_stop_the_managers_experiment_command(self):
        task_id, run_id = self.running_experiment()
        child = identity_with_child(self.pane.shell)["child"]["pid"]  # the command's own process, read from the shell
        refused = self.stop_service.stop("worker", run_id)
        self.assertEqual((refused["status"], refused["reason"], refused["requested_by"], refused["command_id"]),
                         ("refused", "not_owner", "worker", run_id), refused)
        self.assertEqual((self.env.opened, self.env.sent), ([], []), "no pidfd was opened and no signal sent")
        self.assertEqual(self.of_type("command_stop_signal"), [])
        self.assertEqual([(r["command_id"], r["requested_by"], r["status"], r["reason"])
                          for r in self.of_type("command_stop_refused")],
                         [(run_id, "worker", "refused", "not_owner")])
        time.sleep(0.5)
        os.kill(child, 0)  # still alive
        self.assertEqual(self.view()["status"], "running")
        self.assertEqual(self.current_run(task_id)["run_id"], run_id)
        self.assertEqual(self.judge_calls, 0)


class CancelAndStopTests(ExperimentStopFixture):
    """Behavior 3: a cancel waits for the host command; the Manager's stop ends it and the Task closes cancelled."""

    def cancel(self, task_id):
        return self.to_worker({"kind": "experiment", "message": "not needed any more", "task_id": task_id,
                               "cancel": True})

    def expected_command(self, run_id):
        # started_at is the only value the test cannot know in advance; the rest is what the test created
        return {"command_id": run_id, "kind": "experiment", "owner": "manager", "state": "running",
                "log_path": str(self.root / "runs" / run_id / "raw.log"), "stop": None}

    def assert_listed(self, commands, run_id):
        self.assertEqual(len(commands), 1, commands)
        self.assertTrue(commands[0].pop("started_at"), "the start time is shown")
        self.assertEqual(commands, [self.expected_command(run_id)])

    def test_a_stop_after_a_cancel_closes_the_task_as_cancelled_without_a_judgment(self):
        task_id, run_id = self.running_experiment()
        cancel = self.cancel(task_id)
        self.assertEqual((cancel["status"], cancel["task_id"]), ("cancel_requested", task_id), cancel)
        self.assertIn("stop_command", cancel["detail"])
        self.assertIn("never kills", cancel["detail"], "a cancel still stops nothing")
        self.assertIn("host_commands", cancel["detail"], "it says where the command id is")
        for word in ("stopped", "terminated"):
            self.assertNotIn(word, cancel["detail"].lower())
        self.assert_listed(cancel["host_commands"], run_id)
        task = self.view()
        self.assertEqual((task["status"], task["held_reason"], task["cancel_requested"]),
                         ("cancelling", "cancel_waiting_for_host_exit", True))
        self.assert_listed(task["host_commands"], run_id)
        stopped = self.stop_service.stop("manager", run_id, reason="cancelled by the user")
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["signal"]), ("exited", 130, 2), stopped)
        self.assertTrue(wait_until(lambda: self.view()["status"] == "closed", 30), self.view())
        task = self.view()
        self.assertEqual((task["closed_reason"], task["last_result"]), ("cancelled", {"run_id": run_id,
                                                                                    "outcome": "cancelled"}))
        self.assertEqual(task["host_commands"], [], "the command ended")
        self.assertEqual(self.run_history(run_id)[-1]["kind"], "cancelled")
        self.assertIsNone(self.current_run(task_id))
        self.assertEqual(self.judge_calls, 0, "no judgment after a cancel")


class DeniedSignalEnvironment(CountingEnvironment):
    """S7: the kernel refuses every signal (EPERM) while ``deny`` is set, so a stop ends ``unknown``."""

    deny = True

    def pidfd_send_signal(self, fd, signum):
        if self.deny:
            raise PermissionError(1, "Operation not permitted")
        super().pidfd_send_signal(fd, signum)


class UnknownStopTests(ExperimentStopFixture):
    """Behavior 4: a stop that ends ``unknown`` closes nothing; the Task closes when the exit is observed."""

    def make_env(self):
        self.denied = DeniedSignalEnvironment()
        return self.denied

    @property
    def go(self):
        return self.root / "go"

    def waiting_command(self):
        return f"printf 'started\\n'; while [ ! -e {self.go} ]; do sleep 0.05; done; printf PASS > outcome.txt"

    def test_an_unknown_stop_of_a_cancelling_task_shows_its_reason_and_the_exit_closes_the_task(self):
        task_id, run_id = self.running_experiment(self.waiting_command())
        self.assertEqual(self.to_worker({"kind": "experiment", "message": "not needed", "task_id": task_id,
                                         "cancel": True})["status"], "cancel_requested")
        unknown = self.stop_service.stop("manager", run_id)
        self.assertEqual((unknown["status"], unknown["reason"]), ("unknown", "permission_denied"), unknown)
        self.assertEqual(self.denied.sent, [], "the kernel refused every signal")
        task = self.view()
        self.assertEqual((task["status"], task["held_reason"]), ("cancelling", "cancel_waiting_for_host_exit"))
        self.assertEqual(len(task["host_commands"]), 1, task["host_commands"])
        shown = task["host_commands"][0]
        self.assertEqual((shown["command_id"], shown["state"], shown["stop"]["state"], shown["stop"]["reason"]),
                         (run_id, "unknown", "unknown", "permission_denied"))
        time.sleep(0.5)
        self.assertEqual(self.view()["status"], "cancelling", "nothing closes it while the exit is unobserved")
        self.go.write_text("go")  # the command ends by itself (exit 0)
        self.assertTrue(wait_until(lambda: self.view()["status"] == "closed", 30), self.view())
        task = self.view()
        self.assertEqual((task["closed_reason"], task["last_result"]), ("cancelled", {"run_id": run_id,
                                                                                    "outcome": "cancelled"}))
        self.assertEqual((task["host_commands"], self.judge_calls), ([], 0))
        corrected = self.stop_service.stop("manager", run_id)
        self.assertEqual((corrected["status"], corrected["exit_code"], corrected["corrected"], corrected["repeat"]),
                         ("exited", 0, True, True), corrected)
        self.assertEqual([(h["status"], h["reason"]) for h in corrected["history"]], [("unknown", "permission_denied")])
        self.assertEqual([(r["from"], r["previous_reason"], r["to"], r["exit_code"])
                          for r in self.of_type("command_stop_corrected")], [("unknown", "permission_denied", "exited", 0)])
        self.assertEqual(self.denied.sent, [], "a correction sends no signal")


class EndedFirstEnvironment(CountingEnvironment):
    """S7: before the stop pins its first process the command is let go and its end waited for, so the command
    ends by itself between the stop request and the first signal."""

    def __init__(self, release, owner_saw_end):
        super().__init__()
        self._release, self._owner_saw_end, self.armed = release, owner_saw_end, True

    def pidfd_open(self, pid):
        if self.armed:
            self.armed = False
            self._release()
            self._owner_saw_end()
        return super().pidfd_open(pid)


class EndedBeforeSignalTests(ExperimentStopFixture):
    """Behavior 5: a command that ends by itself before the first signal is an ordinary run, not ``stopped``."""

    @property
    def go(self):
        return self.root / "go"

    def command(self):
        return f"printf 'PASS from host\\n'; while [ ! -e {self.go} ]; do sleep 0.05; done; printf PASS > outcome.txt"

    def make_env(self):
        def owner_saw_end():
            self.assertTrue(wait_until(lambda: self.view()["status"] in ("waiting_report", "finished"), 30),
                            self.view())
        return EndedFirstEnvironment(lambda: self.go.write_text("go"), owner_saw_end)

    def test_a_command_that_ends_before_the_first_signal_takes_the_ordinary_judgment_path(self):
        task_id, run_id = self.running_experiment(self.command())
        stopped = self.stop_service.stop("manager", run_id)
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["signals"]), ("already_exited", 0, []),
                         stopped)
        self.assertFalse(self.env.armed is True, "the command was let go at the stop's first pin")
        self.assertEqual(self.env.sent, [], "no signal left")
        self.assertEqual(self.of_type("command_stop_signal"), [])
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", 30), self.view())
        result = self.view()["last_result"]
        self.assertEqual((result["run_id"], result["outcome"], result["judgment"], result["run_closed"]),
                         (run_id, "reported", "success", True), result)
        self.assertNotIn("stopped_by", result)
        history = self.run_history(run_id)
        self.assertEqual(history[-1]["kind"], "completed")
        self.assertEqual((history[-1]["details"].get("outcome"), history[-1]["details"].get("judgment")),
                         (None, "success"), "the ordinary judgment closed the run")
        self.assertEqual(self.judge_calls, 1)


class RegistryCleanupTests(ExperimentStopFixture):
    """Behavior 6: nothing stays registered, or in memory, once the experiment's command ended."""

    QUICK = "printf 'PASS from host\\n'; printf PASS > outcome.txt"

    def assert_released(self):
        self.assertTrue(wait_until(lambda: gc.collect() is not None
                                   and all(ref() is None for ref in self.run_refs + self.port_refs), 10),
                        "the run or the host shell port is still referenced")

    def test_a_finished_experiment_is_no_longer_live_and_holds_neither_run_nor_port(self):
        task_id, run_id = self.running_experiment(self.QUICK)
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", 30), self.view())
        self.assertEqual(self.view()["host_commands"], [])
        self.assertEqual(self.run_refs and len(self.run_refs), 1)
        self.assert_released()
        again = self.stop_service.stop("manager", run_id)
        self.assertEqual((again["status"], again["exit_code"]), ("already_exited", 0), again)
        self.assertNotIn("reason", again)
        self.assertEqual((self.env.opened, self.env.sent), ([], []), "an ended command is never pinned")
        refused = self.stop_service.stop("worker", run_id)
        self.assertEqual((refused["status"], refused["reason"]), ("refused", "not_owner"), "its owner is remembered")

    def test_a_stopped_experiment_is_released_and_a_repeated_stop_gets_the_original_result(self):
        task_id, run_id = self.running_experiment()
        first = self.stop_service.stop("manager", run_id)
        self.assertEqual(first["status"], "exited", first)
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", 30), self.view())
        self.assertEqual(self.view()["host_commands"], [])
        self.assert_released()
        sent = list(self.env.sent)
        repeat = self.stop_service.stop("manager", run_id)
        self.assertEqual({k: repeat[k] for k in ("status", "exit_code", "signal", "repeat")},
                         {"status": "exited", "exit_code": 130, "signal": 2, "repeat": True}, repeat)
        self.assertEqual(self.env.sent, sent, "the repeat sent nothing")

    def test_a_failure_between_registering_and_following_does_not_leave_the_command_live(self):
        seen = []

        def failing_follow(task, run, port):
            seen.append(run.run_id)
            raise RuntimeError("follower did not start")
        with mock.patch.object(self.flow, "_follow", failing_follow):
            result = self.to_worker({"kind": "experiment", "message": "run the fixture",
                                     "spec": self.spec(self.QUICK)})
            self.assertEqual(result["status"], "dispatched", result)
            self.assertTrue(wait_until(lambda: self.view()["held_reason"] == "runner_error:RuntimeError", 30),
                            self.view())
        run_id = seen[0]
        self.assertEqual(self.flow.task_view()["host_commands"], [])
        result = self.stop_service.stop("manager", run_id)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(result["signals"], [], result)
        self.assertEqual((self.env.opened, self.env.sent), ([], []), "nothing live was pinned or signalled")


class InterruptAuditTests(ExperimentStopFixture):
    """Hand-over from CS-07: the user's Ctrl+C on an experiment command is journaled with its run id."""

    def make_backend(self, flow):
        backend = Backend.__new__(Backend)  # built without starting a backend, as the CS-07 audit tests do
        backend.panes = {PaneId.HOST_SHELL: self.pane}
        backend.handoffs = Mock()
        backend.terminal = Mock()
        backend.terminal.summary_command_active.return_value = False  # a Worker command is not what ran
        backend.flow = flow
        backend._wire_shell(self.pane)
        return backend

    def interrupts(self, backend):
        return [call.args[0] for call in backend.handoffs.record.call_args_list
                if call.args[0]["type"] == "command_interrupt"]

    def test_a_user_ctrl_c_on_an_experiment_command_is_journaled_with_the_run_id(self):
        task_id, run_id = self.running_experiment()
        child = identity_with_child(self.pane.shell)["child"]
        backend = self.make_backend(self.flow)
        self.assertIsNone(backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        (record,) = self.interrupts(backend)
        self.assertEqual((record["requested_by"], record["command_id"], record["pid"], record["delivered"]),
                         ("user", run_id, child["pid"], True), record)
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", 30), self.view())

    def test_a_failing_experiment_lookup_is_journaled_as_null_and_the_ctrl_c_is_still_delivered(self):
        task_id, run_id = self.running_experiment()
        child = identity_with_child(self.pane.shell)["child"]
        broken = Mock()
        broken.experiment_command_for.side_effect = RuntimeError("flow unreadable")
        backend = self.make_backend(broken)
        self.assertIsNone(backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        (record,) = self.interrupts(backend)
        self.assertEqual((record["command_id"], record["pid"], record["delivered"]), (None, child["pid"], True))
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", 30), self.view())


class BackendExperimentStopTests(RestartWorkerFixture):
    """Seam S2 on a real ``Backend`` (fake OMPs, real host shell): the wiring of the experiment registry.

    Only the workflow's mailbox is a fixture (the fake OMPs do not analyse); the experiment, its host command, the
    stop service behind ``stop_command`` and the ``terminal`` tool are the Backend's own.
    """

    def setUp(self):
        patcher = mock.patch.object(Backend, "_make_workflow", lambda backend, repository: TaskWorkflow(
            repository, PublicMailboxFixture(repository)))
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        self.backend.command_stop.policy = REDUCED
        self.source = Path(self._dir.name) / "source"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.email", "fixture@example.invalid")
        git(self.source, "config", "user.name", "CS09 Fixture")
        (self.source / "tracked.txt").write_text("base\n")
        git(self.source, "add", "tracked.txt")
        git(self.source, "commit", "-qm", "baseline")
        self.commit = git(self.source, "rev-parse", "HEAD")

    def start_experiment(self):
        spec = {"goal": "fixture run", "paths": ["outcome.txt"], "execution": {
            "source": str(self.source), "commit": self.commit, "command": SLEEPING,
            "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
            "environment": ["PATH"], "shell": "bash"}}
        result = self.call(ActorRole.MANAGER, "to_worker", {"kind": "experiment", "message": "run it", "spec": spec})[0]
        self.assertEqual(result["status"], "dispatched", result)
        flow = self.backend.flow
        self.wait(lambda: flow.task_view()["status"] == "running" and flow.task_view()["run_id"], "the run", 60)
        run_id = flow.task_view()["run_id"]
        self.wait(lambda: flow.task_view()["host_commands"], "the command to be listed")
        return result["task_id"], run_id

    def test_the_managers_stop_command_stops_the_experiment_the_worker_may_not_and_the_task_shows_it(self):
        task_id, run_id = self.start_experiment()
        listed = self.backend.flow.task_view()["host_commands"]
        self.assertEqual([(c["command_id"], c["kind"], c["owner"], c["state"]) for c in listed],
                         [(run_id, "experiment", "manager", "running")])
        refused = self.call(ActorRole.WORKER, "terminal", {"command": None, "stop": run_id})[0]
        self.assertEqual((refused["status"], refused["reason"]), ("refused", "not_owner"), refused)
        stopped = self.call(ActorRole.MANAGER, "stop_command", {"command_id": run_id, "reason": "not needed"})[0]
        self.assertEqual((stopped["status"], stopped["requested_by"], stopped["exit_code"]),
                         ("exited", "manager", 130), stopped)
        flow = self.backend.flow
        self.wait(lambda: flow.task_view()["status"] == "finished", "the Task to finish", 30)
        task = flow.task_view()
        self.assertEqual((task["last_result"]["outcome"], task["last_result"]["stopped_by"], task["host_commands"]),
                         ("stopped", "manager", []))
        again = self.call(ActorRole.MANAGER, "stop_command", {"command_id": run_id, "reason": "again"})[0]
        self.assertEqual((again["status"], again["repeat"]), ("exited", True), again)


class RefusedSignalEnvironment(CountingEnvironment):
    """S7: every signal fails with ``error`` (EPERM, or ESRCH as when the process ended just before it)."""

    error = PermissionError(1, "Operation not permitted")

    def pidfd_send_signal(self, fd, signum):
        self.sent_attempts = getattr(self, "sent_attempts", 0) + 1
        raise self.error


class NoSignalDeliveredTests(ExperimentStopFixture):
    """A stop that delivered no signal is not what ended the command: the run takes the ordinary judgment path."""

    @property
    def go(self):
        return self.root / "go"

    def command(self):
        return f"printf 'PASS from host\\n'; while [ ! -e {self.go} ]; do sleep 0.05; done; printf PASS > outcome.txt"

    def make_env(self):
        return RefusedSignalEnvironment()  # EPERM unless a test sets ``error``

    def assert_ordinary_run(self, run_id):
        self.go.write_text("go")  # the command ends by itself (exit 0)
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", 30), self.view())
        result = self.view()["last_result"]
        self.assertEqual((result["run_id"], result["outcome"], result.get("judgment")),
                         (run_id, "reported", "success"), result)
        for key in ("stopped_by", "stop_signal"):
            self.assertNotIn(key, result)
        details = self.run_history(run_id)[-1]["details"]
        self.assertEqual((details.get("outcome"), details.get("judgment"), details.get("stopped_by")),
                         (None, "success", None))
        self.assertEqual(self.judge_calls, 1, "the ordinary judgment ran")
        self.assertEqual(self.stop_service.stop_marker(run_id), {})

    def test_every_signal_refused_with_eperm_then_a_natural_exit_is_judged_as_usual(self):
        task_id, run_id = self.running_experiment(self.command())
        unknown = self.stop_service.stop("manager", run_id)
        self.assertEqual((unknown["status"], unknown["reason"], unknown["signals"]),
                         ("unknown", "permission_denied", []), unknown)
        self.assertGreater(self.env.sent_attempts, 0, "the stop did try to signal")
        self.assert_ordinary_run(run_id)

    def test_a_signal_that_finds_the_process_gone_is_not_a_delivered_signal(self):
        self.env.error = ProcessLookupError(3, "No such process")  # every signal reads as "it ended just before"
        task_id, run_id = self.running_experiment(self.command())
        unknown = self.stop_service.stop("manager", run_id)
        self.assertEqual((unknown["status"], unknown["signals"]), ("unknown", []), unknown)
        self.assert_ordinary_run(run_id)


class TerminalNoSignalDeliveredTests(OutcomeFixture):
    """The same for a Worker ``terminal`` command: a stop whose signals were all refused marks nothing."""

    def test_a_command_that_ends_after_only_refused_signals_carries_no_stop_marker(self):
        started = self.run_command("sleep 647.1")
        self.assertEqual(started["status"], "running", started)
        pid, ticks = self.own_process("sleep", "647.1")
        self.stop_env.deny = True  # every signal fails with EPERM
        unknown = self.stop(started["command_id"])
        self.assertEqual((unknown["status"], unknown["reason"]), ("unknown", "permission_denied"), unknown)
        self.end_own(pid, ticks)  # the test's own process ends; nothing the stop sent had reached it
        final = self.query()
        self.assertEqual(final["status"], "exited", final)
        for key in ("stopped_by", "stop_signal"):
            self.assertNotIn(key, final)
        (ended,) = [r for r in self.records("terminal_ended") if r["command_id"] == started["command_id"]]
        for key in ("stopped_by", "stop_signal"):
            self.assertNotIn(key, ended)


if __name__ == "__main__":
    unittest.main()
