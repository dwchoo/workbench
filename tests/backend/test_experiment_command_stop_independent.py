"""CS-09 (independent test_designer): stopping an experiment's host command, derived from the contracts.

Sources: ``docs/features/command-stop`` SPEC "실험 명령 stop과 outcome", "취소 응답", "명령 상태 표면", "결과 계약",
"감사 기록", "테스트 결정" (S1, S2, S7); BRIEF CS-AC-02 (experiment part), 06, 12, 20, 21, 23 and the CS-09 ticket.

Seam S1/S2: ``TaskFlow`` over the ``test_task_flow`` pattern with the real ``TaskWorkflow`` on a real host
``ShellPane`` through ``HostShellPort``; the experiment command is a real process in that shell and the stop is a
real ``CommandStopService.stop`` (pidfd signals). Seam S7 for the cases that need an error or a timing injected.
A real ``Backend`` (fake OMPs) covers the wiring of the stop tools and of the user's Ctrl+C.

Expected values come from the contracts and from what this file creates, never from the implementation: the
experiment spec/command, the run events and ``complete_run`` details read straight from the ``TaskRepository``, the
Task's ``last_result``/``host_commands``, the stop journal, the number of ``WorkflowRun.judge`` calls (a stopped run
makes none), 128+n for a signal n, and the pids this file started.

Process hygiene: every command here runs in the test's own host shell (it dies with the pane); the two processes that
leave the shell (a setsid descendant, a sentinel) are started and killed by exact pidfd identity by this file.
"""

from __future__ import annotations

import functools
import gc
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
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
from workbench.contracts import ui_v1
from workbench.contracts.v1 import ActorRole, PaneId
from workbench.tasks.repository import TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.workflow.run import TaskWorkflow, WorkflowRun

from test_cd70_restart_worker import RestartWorkerFixture, alive
from test_task_flow import AUTOMATION, FakeMailbox, PublicMailboxFixture, git, request, wait_until

# The test's own stop policy for real commands (production defaults are verified elsewhere).
POLICY = StopPolicy(identity_wait=3.0, sigint_wait=3.0, sigterm_wait=2.0, sigkill_wait=2.0, call_wait=20.0)
SLEEPING = "printf 'started\\n'; sleep 120; printf PASS > outcome.txt"
CALL_LONG = 60  # seconds a test waits for a Task state change


class Lifecycle:
    """The automation lifecycle hook as a recorder (the real controller is not part of this seam)."""

    def __init__(self):
        self.events, self._lock = [], threading.Lock()

    def _add(self, *event):
        with self._lock:
            self.events.append(event)

    def experiment_started(self, run):
        self._add("experiment_started", run.run_id)

    def work_started(self, *args):
        self._add("work_started", *args)

    def run_ended(self, run_id):
        self._add("run_ended", run_id)

    def status(self):
        return {}

    def named(self, name):
        with self._lock:
            return [event[1:] for event in self.events if event[0] == name]


class CountingEnvironment(LinuxStopEnvironment):
    """The real environment that records every pin and every signal, and what the owner's marker said then."""

    def __init__(self):
        self.opened, self.sent, self.watch = [], [], None
        self.marker_at_open, self.marker_at_send = [], []

    def pidfd_open(self, pid):
        self.opened.append(pid)
        if self.watch is not None:
            self.marker_at_open.append(self.watch())
        return super().pidfd_open(pid)

    def pidfd_send_signal(self, fd, signum):
        self.sent.append(signum)
        if self.watch is not None:
            self.marker_at_send.append(self.watch())
        super().pidfd_send_signal(fd, signum)


class DeniedEnvironment(CountingEnvironment):
    """S7: the kernel refuses every signal (EPERM) while ``deny`` is set."""

    deny = True

    def pidfd_send_signal(self, fd, signum):
        if self.deny:
            raise PermissionError(1, "Operation not permitted")
        super().pidfd_send_signal(fd, signum)


def stat_ticks(pid):
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def kill_exact(pid, ticks):
    """SIGKILL ``pid`` only if it is still the process with these start ticks (pidfd, re-read after opening)."""
    if pid is None or ticks is None or stat_ticks(pid) != ticks:
        return
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if stat_ticks(pid) == ticks:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(fd)


class Fx(unittest.TestCase):
    """TaskFlow + real host shell + real stop service, with the judgment stage and lifecycle observed."""

    ENV = CountingEnvironment

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cs09-ind-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)  # registered first, so it runs last: after the flow thread is closed
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.email", "fixture@example.invalid")
        git(self.source, "config", "user.name", "CS09 Independent")
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
        self.records: list[dict] = []
        self.lifecycle = Lifecycle()
        self.paused = False
        self.judge_calls, self.judge_probe = 0, None
        self.run_refs: list[weakref.ref] = []
        self.port_refs: list[weakref.ref] = []
        self.owned: list[tuple[int, int]] = []  # (pid, start_ticks) of processes this test must end
        original = WorkflowRun.judge

        @functools.wraps(original)
        def counting(run, *args, **kwargs):
            self.judge_calls += 1
            if self.judge_probe is not None:
                self.judge_probe(run.run_id)
            return original(run, *args, **kwargs)
        patcher = mock.patch.object(WorkflowRun, "judge", counting)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._reap_owned)
        self.env = self.ENV()
        self.stop_service = CommandStopService(journal=self.records.append, policy=POLICY, env=self.env)
        self.addCleanup(self.stop_service.close)
        self.service, self.flow = self.start_flow()

    def _pump(self):
        while not self.halt.is_set():
            self.pane.pump()
            time.sleep(0.01)

    def _reap_owned(self):
        for pid, ticks in self.owned:
            kill_exact(pid, ticks)

    def tearDown(self):
        self.halt.set()
        self.loop.join(5)
        self.pane.close()

    def host_shell(self):
        port = HostShellPort(self.pane, lambda: self.pane)
        self.port_refs.append(weakref.ref(port))
        return port

    def make_workflow(self, repository):
        workflow = TaskWorkflow(repository, PublicMailboxFixture(repository))
        fixture, start = self, workflow.start

        @functools.wraps(start)
        def tracked(*args, **kwargs):
            run = start(*args, **kwargs)
            fixture.run_refs.append(weakref.ref(run))
            return run
        workflow.start = tracked
        return workflow

    def start_flow(self):
        service = HandoffService(self.root / "workflow" / "handoffs.jsonl", mailbox=FakeMailbox())
        ports = ExperimentPorts(host_shell=self.host_shell, make_workflow=self.make_workflow,
                                automation=lambda: AUTOMATION, environment_names=lambda: {"PATH"},
                                worktrees_root=self.root / "worktrees", artifacts_root=self.root / "runs")
        flow = TaskFlow(self.root / "workflow" / "tasks-flow.jsonl", repository_factory=lambda: TaskRepository(self.db),
                        handoffs=service, omp_idle=lambda role: True, experiment=ports, poll_interval=0.05,
                        collect_slice=0.2, paused=lambda: self.paused, lifecycle=self.lifecycle)
        flow.command_stop = self.stop_service
        service.configure(policy=flow)
        service.start()
        flow.start()
        self.addCleanup(service.close)
        self.addCleanup(flow.close)
        self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt"))
        self.calls = 0
        return service, flow

    # -- tool calls and reads -------------------------------------------------------------------------
    def to_worker(self, args):
        self.calls += 1
        return self.service.handle(ActorRole.MANAGER, request("to_worker", args, f"ind-{self.calls}"))

    def cancel(self, task_id):
        return self.to_worker({"kind": "experiment", "message": "not needed any more", "task_id": task_id,
                               "cancel": True})

    def spec(self, command):
        return {"goal": "fixture run", "paths": ["outcome.txt"], "execution": {
            "source": str(self.source), "commit": self.commit, "command": command,
            "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
            "environment": ["PATH"], "shell": "bash"}}

    def running_experiment(self, command=SLEEPING):
        """Create the experiment; return ``(task_id, run_id)`` once its command prints ``started``."""
        result = self.to_worker({"kind": "experiment", "message": "run the fixture", "spec": self.spec(command)})
        self.assertEqual(result["status"], "dispatched", result)
        wanted = result["task_id"]
        self.assertTrue(wait_until(lambda: (self.view() or {}).get("task_id") == wanted
                                   and self.view()["status"] == "running" and self.view()["run_id"] is not None,
                                   CALL_LONG), self.view())
        run_id = self.view()["run_id"]
        self.assertTrue(wait_until(lambda: "started" in [e["kind"] for e in self.shell_history(run_id)], 10),
                        self.shell_history(run_id))
        self.assertTrue(wait_until(lambda: self.view()["host_commands"], 10), self.view())
        return wanted, run_id

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

    def child_pid(self):
        return self.pane.shell.run_identity()["child"]["pid"]

    def await_status(self, status, timeout=CALL_LONG):
        self.assertTrue(wait_until(lambda: self.view()["status"] == status, timeout), self.view())
        return self.view()


class ManagerStopTests(Fx):
    """CS-AC-21 / ticket behavior 1: a Manager stop without a cancel closes the run as ``stopped``."""

    def test_stop_closes_the_run_as_stopped_and_every_record_agrees(self):
        secret = "cs09-secret-token-5521"
        task_id, run_id = self.running_experiment(f"printf 'started {secret}\\n'; sleep 120; printf PASS > outcome.txt")
        sentinel = subprocess.Popen(["sleep", "86197"], start_new_session=True)
        self.addCleanup(lambda: (sentinel.kill(), sentinel.wait()))
        shell_pid = self.pane.shell.run_identity()["parent"]["pid"]
        stopped = self.stop_service.stop("manager", run_id, reason="the benchmark is not needed any more")
        self.assertEqual((stopped["status"], stopped["requested_by"], stopped["command_id"]),
                         ("exited", "manager", run_id), stopped)
        self.assertEqual((stopped["exit_code"], stopped["signal"], stopped["signal_name"]), (130, 2, "SIGINT"))
        task = self.await_status("finished")
        # last_result is exactly the contract's shape (plus nothing a judged run would carry)
        self.assertEqual(task["last_result"], {"run_id": run_id, "outcome": "stopped", "stopped_by": "manager",
                                                "exit_code": 130, "signal": 2, "run_closed": True})
        self.assertEqual((task["active"], task["run_id"], task["host_commands"]), (False, None, []))
        # the repository: complete_run with a stopped outcome, no judgment, no new event kind
        history = self.run_history(run_id)
        self.assertEqual(history[-1]["kind"], "completed")
        details = history[-1]["details"]
        self.assertEqual((details.get("outcome"), details.get("stopped_by"), details.get("judgment")),
                         ("stopped", "manager", None))
        self.assertIn("judgment", details, "the details carry an explicit null judgment")
        self.assertEqual(details.get("stop_signal"), "SIGINT")
        self.assertEqual((details.get("exit_code"), details.get("signal")), (130, 2))
        self.assertNotIn("failed", [event["kind"] for event in history], "a stopped run is not a failure")
        self.assertNotIn("cancelled", [event["kind"] for event in history], "a stopped run is not a cancel")
        self.assertIsNone(self.current_run(task_id), "the run is closed in the repository")
        # no judgment, no retry, no second run, no report
        self.assertEqual(self.judge_calls, 0)
        time.sleep(0.8)
        self.assertEqual(self.judge_calls, 0)
        self.assertEqual(self.flow.tasks[task_id].runs_started, 1)
        self.assertIsNone(self.flow.tasks[task_id].pending_start)
        self.assertEqual(self.view()["status"], "finished")
        self.assertEqual(self.flow.worker_view(), {"state": "idle", "task_id": None})
        # the audit trail: the Task ledger's run_ended record and the automation's run_ended hook
        ended = [r for r in self.ledger() if r["type"] == "run_ended" and r.get("run_id") == run_id]
        self.assertEqual([(r["outcome"], r["stopped_by"], r["task_id"]) for r in ended],
                         [("stopped", "manager", task_id)])
        self.assertEqual([r for r in self.ledger() if r["type"] == "run_held"], [])
        self.assertEqual(self.lifecycle.named("run_ended"), [(run_id,)], "the automation learns the end exactly once")
        self.assertTrue(wait_until(lambda: self.lifecycle.named("experiment_started") == [(run_id,)], 5))
        # stop journal: requester, kind, owner, task, reason; signals with times; the finish; no argv/command text
        (requested,) = self.of_type("command_stop_requested")
        self.assertEqual({k: requested[k] for k in ("command_id", "requested_by", "kind", "owner", "task_id",
                                                     "task_status", "reason")},
                         {"command_id": run_id, "requested_by": "manager", "kind": "experiment", "owner": "manager",
                          "task_id": task_id, "task_status": "running",
                          "reason": "the benchmark is not needed any more"})
        (signal_record,) = self.of_type("command_stop_signal")
        self.assertEqual((signal_record["signal"], signal_record["command_id"]), ("SIGINT", run_id))
        self.assertTrue(signal_record["at"] and signal_record["targets"])
        (finished,) = self.of_type("command_stop_finished")
        self.assertEqual((finished["command_id"], finished["status"], finished["exit_code"], finished["signal"]),
                         (run_id, "exited", 130, 2))
        self.assertNotIn(secret, json.dumps(self.records), "the command text is never journaled")
        # nothing outside the command was touched: the sentinel, the host shell parent, the user's prompt
        self.assertIsNone(sentinel.poll(), "a process outside the command was signalled")
        self.assertTrue(alive(shell_pid))
        self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10),
                        "the host shell is given back to the user")

    def test_a_cooperative_exit_after_sigint_is_still_a_stop_not_a_judged_success(self):
        # the command already wrote what the criteria want; traps SIGINT and exits 0; a stop is never judged
        command = ("printf 'PASS\\n'; printf PASS > outcome.txt; trap 'exit 0' INT; printf 'started\\n'; "
                   "sleep 120 & wait")
        task_id, run_id = self.running_experiment(command)
        stopped = self.stop_service.stop("manager", run_id)
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["signal"]), ("exited", 0, None), stopped)
        self.await_status("finished")
        self.assertEqual(self.view()["last_result"], {"run_id": run_id, "outcome": "stopped", "stopped_by": "manager",
                                                       "exit_code": 0, "signal": None, "run_closed": True})
        self.assertEqual(self.judge_calls, 0, "even a passing result is not judged once a stop ended the command")
        self.assertEqual(self.run_history(run_id)[-1]["details"].get("judgment"), None)

    def test_sigint_ignored_escalates_to_sigterm_and_the_run_records_the_last_signal(self):
        command = "trap '' INT; printf 'started\\n'; sleep 120"
        task_id, run_id = self.running_experiment(command)
        stopped = self.stop_service.stop("manager", run_id)
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["signal"], stopped["signal_name"]),
                         ("exited", 143, 15, "SIGTERM"), stopped)
        self.assertEqual([s["signal"] for s in stopped["signals"] if not s.get("auxiliary")], ["SIGINT", "SIGTERM"])
        self.await_status("finished")
        self.assertEqual(self.view()["last_result"], {"run_id": run_id, "outcome": "stopped", "stopped_by": "manager",
                                                       "exit_code": 143, "signal": 15, "run_closed": True})
        self.assertEqual(self.run_history(run_id)[-1]["details"].get("stop_signal"), "SIGTERM")
        self.assertEqual(self.judge_calls, 0)

    def test_sigterm_and_sigint_ignored_ends_with_sigkill_and_still_closes_stopped(self):
        command = "trap '' INT TERM; printf 'started\\n'; sleep 120"
        task_id, run_id = self.running_experiment(command)
        stopped = self.stop_service.stop("manager", run_id)
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["signal"], stopped["signal_name"]),
                         ("exited", 137, 9, "SIGKILL"), stopped)
        self.await_status("finished")
        self.assertEqual(self.view()["last_result"]["outcome"], "stopped")
        self.assertEqual((self.view()["last_result"]["exit_code"], self.view()["last_result"]["signal"]), (137, 9))
        self.assertEqual(self.run_history(run_id)[-1]["details"].get("stop_signal"), "SIGKILL")
        self.assertEqual(self.judge_calls, 0)

    def test_stop_while_the_system_is_paused_still_stops_and_closes_the_run(self):
        # CS-D13: pause does not block a stop; the owner collects while paused, so the run still closes
        task_id, run_id = self.running_experiment()
        self.paused = True
        time.sleep(0.5)
        stopped = self.stop_service.stop("manager", run_id)
        self.assertEqual(stopped["status"], "exited", stopped)
        self.await_status("finished")
        self.assertEqual(self.view()["last_result"]["outcome"], "stopped")
        self.assertEqual(self.judge_calls, 0)

    def test_run_events_use_only_the_existing_kinds_and_the_schema_gets_no_stopped_kind(self):
        task_id, run_id = self.running_experiment()
        self.stop_service.stop("manager", run_id)
        self.await_status("finished")
        with sqlite3.connect(self.db) as connection:
            ddl = " ".join(row[0] or "" for row in connection.execute(
                "select sql from sqlite_master where sql like '%run_events%'"))
            self.assertNotIn("'stopped'", ddl, "no event kind was added for a stopped run")
            kinds = {row[0] for row in connection.execute("select kind from run_events where run_id = ?", (run_id,))}
        for kind in kinds:
            self.assertIn(f"'{kind}'", ddl, f"event kind {kind} is not one of the schema's kinds")
        self.assertIn("completed", kinds)


class SurfaceWhileStoppingTests(Fx):
    """Ticket behavior 1/3 surface: ``host_commands`` shows ``stopping``; a cancel during the stop still wins."""

    SLOW = "trap '' INT; printf 'started\\n'; sleep 120"  # SIGINT is ignored: the SIGINT stage lasts sigint_wait

    def stop_in_thread(self, run_id):
        box = {}
        thread = threading.Thread(target=lambda: box.setdefault("result", self.stop_service.stop("manager", run_id)),
                                  daemon=True)
        thread.start()
        return thread, box

    def test_host_commands_show_stopping_while_the_episode_runs_then_the_run_closes_stopped(self):
        task_id, run_id = self.running_experiment(self.SLOW)
        thread, box = self.stop_in_thread(run_id)
        self.assertTrue(wait_until(lambda: self.view()["host_commands"][0]["state"] == "stopping", 10), self.view())
        shown = self.view()["host_commands"][0]
        self.assertEqual((shown["command_id"], shown["kind"], shown["owner"]), (run_id, "experiment", "manager"))
        self.assertIsInstance(shown["stop"], dict)
        self.assertEqual(self.view()["status"], "running", "the run is not closed before the exit is observed")
        self.assertEqual(self.flow.status_view()["host_commands"][0]["state"], "stopping")
        self.assertEqual(self.judge_calls, 0)
        thread.join(30)
        self.assertEqual(box["result"]["status"], "exited", box)
        self.await_status("finished")
        self.assertEqual(self.view()["last_result"]["outcome"], "stopped")

    def test_a_cancel_asked_while_the_stop_is_under_way_wins_and_nothing_is_judged(self):
        task_id, run_id = self.running_experiment(self.SLOW)
        thread, box = self.stop_in_thread(run_id)
        self.assertTrue(wait_until(lambda: self.view()["host_commands"][0]["state"] == "stopping", 10), self.view())
        cancel = self.cancel(task_id)
        self.assertEqual(cancel["status"], "cancel_requested", cancel)
        thread.join(30)
        self.assertEqual(box["result"]["status"], "exited", box)
        self.await_status("closed")
        task = self.view()
        self.assertEqual((task["closed_reason"], task["last_result"]), ("cancelled", {"run_id": run_id,
                                                                                    "outcome": "cancelled"}))
        kinds = [event["kind"] for event in self.run_history(run_id)]
        self.assertEqual(kinds[-1], "cancelled")
        self.assertNotIn("completed", kinds, "the cancel won: the run is not closed as completed/stopped")
        self.assertEqual(self.judge_calls, 0)
        self.assertEqual([r for r in self.ledger() if r["type"] == "run_ended" and r.get("outcome") == "stopped"], [])


class ConcurrentAndRerunTests(Fx):
    def test_two_stops_at_once_share_one_episode_and_one_set_of_signals(self):
        task_id, run_id = self.running_experiment("trap '' INT; printf 'started\\n'; sleep 120")
        boxes = [{}, {}]
        threads = [threading.Thread(target=lambda b=b: b.setdefault("r", self.stop_service.stop("manager", run_id)),
                                    daemon=True) for b in boxes]
        threads[0].start()
        self.assertTrue(wait_until(lambda: self.view()["host_commands"][0]["state"] == "stopping", 10), self.view())
        threads[1].start()
        for thread in threads:
            thread.join(40)
        first, second = boxes[0]["r"], boxes[1]["r"]
        self.assertEqual((first["status"], second["status"]), ("exited", "exited"), (first, second))
        self.assertEqual(sorted(bool(r.get("repeat")) for r in (first, second)), [False, True])
        self.assertEqual((first["exit_code"], first["signal"]), (second["exit_code"], second["signal"]))
        self.assertEqual(len(self.of_type("command_stop_requested")), 1, "one episode, one request record")
        stages = [r["signal"] for r in self.of_type("command_stop_signal") if not r["auxiliary"] and r["targets"]]
        self.assertEqual(stages, ["SIGINT", "SIGTERM"], "the second caller added no signal")
        self.await_status("finished")
        self.assertEqual(self.judge_calls, 0)

    def test_a_stopped_task_can_be_rerun_by_the_manager_and_the_new_run_is_stoppable_on_its_own(self):
        task_id, first = self.running_experiment()
        self.stop_service.stop("manager", first)
        self.await_status("finished")
        again = self.to_worker({"kind": "experiment", "message": "run it again", "task_id": task_id, "run": True})
        self.assertEqual(again["status"], "dispatched", again)
        self.assertTrue(wait_until(lambda: self.view()["status"] == "running" and self.view()["run_id"] not in
                                   (None, first), CALL_LONG), self.view())
        second = self.view()["run_id"]
        self.assertTrue(wait_until(lambda: self.view()["host_commands"], 10))
        self.assertEqual([c["command_id"] for c in self.view()["host_commands"]], [second])
        self.assertEqual(self.stop_service.stop("manager", second)["status"], "exited")
        self.await_status("finished")
        self.assertEqual(self.view()["last_result"]["run_id"], second)
        self.assertEqual(self.view()["last_result"]["outcome"], "stopped")
        self.assertEqual(self.flow.tasks[task_id].runs_started, 2)
        self.assertEqual(self.judge_calls, 0)
        self.assertEqual(self.lifecycle.named("run_ended"), [(first,), (second,)])


class OwnerRefusalTests(Fx):
    """CS-AC-06: the Worker cannot stop the Manager's experiment command; refusal sends no signal."""

    def test_worker_stop_is_refused_and_the_manager_can_still_stop_it_afterwards(self):
        task_id, run_id = self.running_experiment()
        child = self.child_pid()
        refused = self.stop_service.stop("worker", run_id)
        self.assertEqual((refused["status"], refused["reason"], refused["requested_by"]),
                         ("refused", "not_owner", "worker"), refused)
        self.assertNotIn("signals", refused)
        self.assertEqual((self.env.opened, self.env.sent), ([], []), "no pid was pinned and no signal sent")
        self.assertEqual(self.of_type("command_stop_signal"), [])
        self.assertEqual([(r["requested_by"], r["status"], r["reason"]) for r in self.of_type("command_stop_refused")],
                         [("worker", "refused", "not_owner")])
        self.assertEqual(self.stop_service.stop_marker(run_id), {}, "a refusal marks nothing")
        time.sleep(0.5)
        self.assertTrue(alive(child))
        self.assertEqual(self.view()["status"], "running")
        # the refusal opened no episode: the Manager's stop is a first stop (no repeat marker) and works
        stopped = self.stop_service.stop("manager", run_id)
        self.assertEqual(stopped["status"], "exited", stopped)
        self.assertNotIn("repeat", stopped)
        self.await_status("finished")
        self.assertEqual(self.view()["last_result"]["stopped_by"], "manager")
        # even after the end the Worker still gets not_owner, never the command's exit state
        again = self.stop_service.stop("worker", run_id)
        self.assertEqual((again["status"], again["reason"]), ("refused", "not_owner"), again)

    def test_an_unknown_id_and_the_hosts_own_request_id_are_not_stoppable_commands(self):
        task_id, run_id = self.running_experiment()
        request_id = self.pane.shell.run_identity()["request_id"]
        self.assertNotEqual(request_id, run_id)
        for wanted in (request_id, "00000000-0000-4000-8000-000000000000"):
            result = self.stop_service.stop("manager", wanted)
            self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)
        self.assertEqual((self.env.opened, self.env.sent), ([], []))
        self.assertTrue(alive(self.child_pid()))
        self.assertEqual(self.view()["status"], "running")


class CancelCombinationTests(Fx):
    """CS-AC-02 / CS-AC-12: the cancel response, and the Task closing once the stop shows the exit."""

    def test_the_cancel_response_lists_the_command_and_keeps_the_never_kills_policy(self):
        task_id, run_id = self.running_experiment()
        cancel = self.cancel(task_id)
        self.assertEqual((cancel["status"], cancel["task_id"]), ("cancel_requested", task_id), cancel)
        self.assertEqual((self.view()["status"], self.view()["held_reason"]),
                         ("cancelling", "cancel_waiting_for_host_exit"), "the wait reason is unchanged")
        self.assertEqual(len(cancel["host_commands"]), 1, cancel)
        item = cancel["host_commands"][0]
        self.assertTrue(item.pop("started_at"))
        self.assertEqual(item, {"command_id": run_id, "kind": "experiment", "owner": "manager", "state": "running",
                                "log_path": str(self.root / "runs" / run_id / "raw.log"), "stop": None})
        detail = cancel["detail"]
        self.assertIn("stop_command", detail)
        self.assertIn("run_id", detail, "it says the command id is the run id")
        self.assertIn("never kills", detail, "a cancel itself still stops nothing")
        for word in ("stopped", "terminated", "killed"):
            self.assertNotIn(word, detail.lower())
        self.assertEqual([c["command_id"] for c in self.view()["host_commands"]], [run_id])
        self.assertEqual([c["command_id"] for c in self.flow.status_view()["host_commands"]], [run_id])
        time.sleep(0.6)
        self.assertTrue(alive(self.child_pid()), "a cancel does not stop the command")
        self.assertEqual(self.view()["status"], "cancelling")
        self.assertEqual((self.env.opened, self.env.sent), ([], []))
        # the Task is still listed as cancelling and the manager's stop then ends it cancelled
        stopped = self.stop_service.stop("manager", run_id, reason="the cancel was meant to end the command")
        self.assertEqual(stopped["status"], "exited", stopped)
        task = self.await_status("closed")
        self.assertEqual((task["closed_reason"], task["last_result"], task["host_commands"]),
                         ("cancelled", {"run_id": run_id, "outcome": "cancelled"}, []))
        (requested,) = self.of_type("command_stop_requested")
        self.assertEqual((requested["task_id"], requested["task_status"]), (task_id, "cancelling"),
                         "the audit names the Task and its status at the request")
        self.assertEqual(self.judge_calls, 0)
        self.assertEqual(self.lifecycle.named("run_ended"), [(run_id,)])

    def test_a_stop_asked_before_the_cancel_but_observed_after_it_still_closes_cancelled(self):
        # an episode that is under way when the cancel is asked: the cancel wins at the observed exit
        task_id, run_id = self.running_experiment("trap '' INT; printf 'started\\n'; sleep 120")
        box = {}
        thread = threading.Thread(target=lambda: box.setdefault("r", self.stop_service.stop("manager", run_id)),
                                  daemon=True)
        thread.start()
        self.assertTrue(wait_until(lambda: self.stop_service.stop_marker(run_id) != {}, 10))
        self.assertEqual(self.cancel(task_id)["status"], "cancel_requested")
        thread.join(30)
        self.await_status("closed")
        self.assertEqual(self.view()["last_result"], {"run_id": run_id, "outcome": "cancelled"})
        self.assertEqual(self.judge_calls, 0)


class SetsidDescendantTests(Fx):
    """CS-AC-23 with a real process: a descendant that left the session leaves the exit unobserved (``unknown``)."""

    UNIQUE = "86311"

    def command(self):
        return (f"setsid sleep {self.UNIQUE} & echo $! > {self.root}/descendant.pid; printf 'started\\n'; sleep 120")

    def descendant(self):
        path = self.root / "descendant.pid"
        self.assertTrue(wait_until(lambda: path.exists() and path.read_text().strip(), 10))
        pid = int(path.read_text())
        ticks = stat_ticks(pid)
        self.assertIsNotNone(ticks)
        self.owned.append((pid, ticks))
        return pid, ticks

    def end_descendant(self, pid, ticks):
        kill_exact(pid, ticks)
        self.assertTrue(wait_until(lambda: not alive(pid), 10))

    def test_unknown_without_a_cancel_keeps_the_run_open_then_the_exit_closes_it_as_stopped(self):
        task_id, run_id = self.running_experiment(self.command())
        pid, ticks = self.descendant()
        unknown = self.stop_service.stop("manager", run_id)
        self.assertEqual((unknown["status"], unknown["reason"]), ("unknown", "left_session_descendants"), unknown)
        self.assertTrue(alive(pid), "a descendant outside the session is never signalled")
        self.assertEqual(unknown["residual"]["left_session"][0]["pid"], pid)
        sent = list(self.env.sent)
        # the outcome is not decided: the run stays running and says why
        time.sleep(0.8)
        task = self.view()
        self.assertEqual(task["status"], "running")
        self.assertIsNone(task["last_result"].get("outcome") if task["last_result"] else None)
        shown = task["host_commands"][0]
        self.assertEqual((shown["command_id"], shown["state"], shown["stop"]["reason"]),
                         (run_id, "unknown", "left_session_descendants"))
        self.assertEqual(self.current_run(task_id)["run_id"], run_id)
        self.assertEqual(self.judge_calls, 0)
        # the descendant ends: the owner sees the exit and the rule for a stopped run applies
        self.end_descendant(pid, ticks)
        self.await_status("finished")
        self.assertEqual(self.view()["last_result"]["outcome"], "stopped")
        self.assertEqual(self.view()["last_result"]["stopped_by"], "manager")
        self.assertEqual(self.run_history(run_id)[-1]["details"].get("outcome"), "stopped")
        self.assertEqual(self.judge_calls, 0)
        self.assertEqual(self.view()["host_commands"], [])
        corrected = self.stop_service.stop("manager", run_id)
        self.assertEqual((corrected["status"], corrected["corrected"], corrected["repeat"]), ("exited", True, True))
        self.assertEqual([(h["status"], h["reason"]) for h in corrected["history"]],
                         [("unknown", "left_session_descendants")])
        self.assertEqual(self.env.sent, sent, "a correction sends no signal")
        self.assertEqual([(r["from"], r["previous_reason"], r["to"]) for r in self.of_type("command_stop_corrected")],
                         [("unknown", "left_session_descendants", "exited")])

    def test_unknown_with_a_cancel_keeps_the_task_cancelling_then_the_exit_closes_it_cancelled(self):
        task_id, run_id = self.running_experiment(self.command())
        pid, ticks = self.descendant()
        self.assertEqual(self.cancel(task_id)["status"], "cancel_requested")
        unknown = self.stop_service.stop("manager", run_id)
        self.assertEqual((unknown["status"], unknown["reason"]), ("unknown", "left_session_descendants"), unknown)
        sent = list(self.env.sent)
        time.sleep(0.8)
        task = self.view()
        self.assertEqual((task["status"], task["held_reason"]), ("cancelling", "cancel_waiting_for_host_exit"))
        self.assertEqual(task["host_commands"][0]["stop"]["reason"], "left_session_descendants")
        self.assertEqual(self.env.sent, sent, "no further signal")
        self.end_descendant(pid, ticks)
        self.await_status("closed")
        self.assertEqual(self.view()["last_result"], {"run_id": run_id, "outcome": "cancelled"})
        self.assertEqual(self.run_history(run_id)[-1]["kind"], "cancelled")
        self.assertEqual(self.view()["host_commands"], [])
        corrected = self.stop_service.stop("manager", run_id)
        self.assertEqual((corrected["status"], corrected["corrected"]), ("exited", True))
        self.assertEqual(self.judge_calls, 0)
        self.assertEqual(self.env.sent, sent)


class DeniedStopTests(Fx):
    ENV = DeniedEnvironment

    def test_a_permission_denied_stop_leaves_the_run_running_and_later_closes_it_without_a_new_signal(self):
        go = self.root / "go"
        command = f"printf 'started\\n'; while [ ! -e {go} ]; do sleep 0.05; done; printf PASS > outcome.txt"
        task_id, run_id = self.running_experiment(command)
        unknown = self.stop_service.stop("manager", run_id)
        self.assertEqual((unknown["status"], unknown["reason"]), ("unknown", "permission_denied"), unknown)
        self.assertEqual(self.env.sent, [])
        time.sleep(0.6)
        task = self.view()
        self.assertEqual(task["status"], "running", "an unknown stop closes nothing")
        self.assertEqual((task["host_commands"][0]["state"], task["host_commands"][0]["stop"]["reason"]),
                         ("unknown", "permission_denied"))
        self.assertEqual(self.judge_calls, 0, "nothing was judged while the exit is unobserved")
        go.write_text("go")
        self.await_status("finished")
        self.assertEqual(self.view()["host_commands"], [])
        history = self.run_history(run_id)
        self.assertEqual(history[-1]["kind"], "completed")
        # no signal was ever delivered (the kernel refused all): whichever way the run is classified, the stop
        # service's correction records the exit and sends nothing
        corrected = self.stop_service.stop("manager", run_id)
        self.assertEqual((corrected["status"], corrected["exit_code"], corrected["corrected"]), ("exited", 0, True))
        self.assertEqual([(h["status"], h["reason"]) for h in corrected["history"]], [("unknown", "permission_denied")])
        self.assertEqual(self.env.sent, [])
        outcome = self.view()["last_result"]["outcome"]
        self.assertIn(outcome, ("stopped", "reported"), self.view()["last_result"])
        self.assertEqual(self.judge_calls, 0 if outcome == "stopped" else 1)


class NaturalExitTests(Fx):
    """Ticket behavior 5: a command that ends by itself is the ordinary judgment path; the stop is ``already_exited``."""

    QUICK = "printf 'PASS from host\\n'; printf PASS > outcome.txt"

    def test_a_stop_after_a_natural_end_is_already_exited_and_the_run_is_not_stopped(self):
        result = self.to_worker({"kind": "experiment", "message": "run the fixture", "spec": self.spec(self.QUICK)})
        task_id = result["task_id"]
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", CALL_LONG), self.view())
        run_id = self.view()["last_result"]["run_id"]
        last = self.view()["last_result"]
        self.assertEqual((last["outcome"], last["judgment"], last["run_closed"]), ("reported", "success", True), last)
        self.assertNotIn("stopped_by", last)
        self.assertEqual(self.judge_calls, 1)
        stopped = self.stop_service.stop("manager", run_id)
        self.assertEqual((stopped["status"], stopped["exit_code"]), ("already_exited", 0), stopped)
        self.assertNotIn("reason", stopped)
        self.assertEqual((self.env.opened, self.env.sent), ([], []), "an ended command is never pinned or signalled")
        self.assertEqual(self.stop_service.stop_marker(run_id), {})
        details = self.run_history(run_id)[-1]["details"]
        self.assertNotEqual(details.get("outcome"), "stopped")
        self.assertNotIn("stopped_by", details)
        self.assertEqual(self.view()["last_result"], last, "the late stop changed nothing")

    def test_a_failing_command_that_ended_on_its_own_is_never_reported_as_stopped(self):
        result = self.to_worker({"kind": "experiment", "message": "run the fixture",
                                 "spec": self.spec("printf 'broken\\n'; exit 3")})
        task_id = result["task_id"]
        self.assertTrue(wait_until(lambda: self.view()["status"] in ("finished", "held", "closed", "waiting_report")
                                   and self.view()["last_result"] is not None, CALL_LONG), self.view())
        run_id = self.view()["last_result"]["run_id"]
        self.assertNotEqual(self.view()["last_result"]["outcome"], "stopped")
        self.assertNotIn("stopped_by", self.view()["last_result"])
        stopped = self.stop_service.stop("manager", run_id)
        self.assertEqual((stopped["status"], stopped["exit_code"]), ("already_exited", 3), stopped)
        self.assertEqual(self.env.sent, [])

    def test_the_end_is_reported_to_the_stop_service_before_the_judgment_stage_runs(self):
        seen = {}

        def probe(run_id):
            box = {}
            thread = threading.Thread(target=lambda: box.setdefault("r", self.stop_service.stop("manager", run_id)),
                                      daemon=True)
            thread.start()
            thread.join(5)
            seen["result"] = box.get("r")
        self.judge_probe = probe
        result = self.to_worker({"kind": "experiment", "message": "run the fixture", "spec": self.spec(self.QUICK)})
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", CALL_LONG), self.view())
        self.assertIsNotNone(seen.get("result"), "the probe ran during the judgment stage")
        self.assertEqual((seen["result"]["status"], seen["result"]["exit_code"]), ("already_exited", 0),
                         "the command was already reported ended when the judgment stage began")

    def test_the_stop_marker_is_set_only_right_before_the_first_signal(self):
        task_id, run_id = self.running_experiment()
        self.env.watch = lambda: self.stop_service.stop_marker(run_id)
        self.assertEqual(self.stop_service.stop_marker(run_id), {}, "nothing before a stop")
        self.stop_service.stop("manager", run_id)
        self.assertTrue(self.env.marker_at_open, "the stop pinned processes")
        self.assertEqual(set(map(json.dumps, self.env.marker_at_open)), {"{}"},
                         "pinning processes (before any signal) leaves the run unmarked")
        self.assertEqual(self.env.marker_at_send[0], {"stopped_by": "manager", "stop_signal": "SIGINT"},
                         "the marker is there when the first signal leaves")

    def test_races_between_a_natural_end_and_a_stop_are_never_inconsistent(self):
        # the command ends by itself ~0.15 s after the stop request; whichever the interleaving, the stop's answer
        # and the run's outcome must agree: an acted-on stop -> stopped without a judgment; a late stop -> ordinary
        command = "printf 'PASS\\n'; printf 'started\\n'; sleep 0.15; printf PASS > outcome.txt"
        verdicts = []
        for attempt in range(4):
            before = self.judge_calls
            result = self.to_worker({"kind": "experiment", "message": f"race {attempt}", "spec": self.spec(command)})
            wanted = result["task_id"]
            self.assertTrue(wait_until(lambda: (self.view() or {}).get("task_id") == wanted
                                       and self.view()["run_id"] is not None, CALL_LONG), self.view())
            run_id = self.view()["run_id"]
            stopped = self.stop_service.stop("manager", run_id)
            self.assertTrue(wait_until(lambda: self.view()["status"] in ("finished", "held")
                                       and self.view()["task_id"] == wanted and self.view()["run_id"] is None, CALL_LONG),
                            self.view())
            last = self.view()["last_result"]
            self.assertEqual(last["run_id"], run_id)
            if last["outcome"] == "stopped":
                self.assertEqual(stopped["status"], "exited", (stopped, last))
                self.assertTrue(stopped["signals"], "a stopped run is one a stop signalled")
                self.assertEqual(self.judge_calls, before, "a stopped run is not judged")
                self.assertEqual(last["stopped_by"], "manager")
            else:
                self.assertEqual(stopped["status"], "already_exited", (stopped, last))
                self.assertEqual(stopped["signals"] if "signals" in stopped else [], [])
                self.assertNotIn("stopped_by", last)
                self.assertEqual(self.judge_calls, before + 1)
            verdicts.append(last["outcome"])
        self.assertEqual(len(verdicts), 4)


class RegistryCleanupTests(Fx):
    """Ticket behavior 6 and the identity mapping: nothing live stays registered, and a run id names one command."""

    def assert_released(self):
        self.assertTrue(wait_until(lambda: gc.collect() is not None
                                   and all(ref() is None for ref in self.run_refs + self.port_refs), 10),
                        "a WorkflowRun or HostShellPort is still referenced")

    def test_after_the_run_ends_the_request_id_no_longer_resolves_and_nothing_is_held(self):
        task_id, run_id = self.running_experiment()
        request_id = self.pane.shell.run_identity()["request_id"]
        self.assertEqual(self.flow.experiment_command_for(request_id), run_id)
        for value in (None, 12, "no-such-request", ""):
            self.assertIsNone(self.flow.experiment_command_for(value))
        self.stop_service.stop("manager", run_id)
        self.await_status("finished")
        self.assertIsNone(self.flow.experiment_command_for(request_id))
        self.assert_released()
        repeat = self.stop_service.stop("manager", run_id)
        self.assertEqual((repeat["status"], repeat["repeat"]), ("exited", True), repeat)

    def test_a_cancelled_and_stopped_run_is_released_too(self):
        task_id, run_id = self.running_experiment()
        self.cancel(task_id)
        self.stop_service.stop("manager", run_id)
        self.await_status("closed")
        self.assert_released()
        self.assertEqual(self.view()["host_commands"], [])
        self.assertEqual(self.stop_service.stop("manager", run_id)["repeat"], True)

    def test_a_failing_follower_never_leaves_a_stoppable_entry_for_a_command_still_running(self):
        seen = []

        def failing_follow(task, run, port):
            seen.append(run.run_id)
            raise RuntimeError("follower crashed")
        with mock.patch.object(self.flow, "_follow", failing_follow):
            result = self.to_worker({"kind": "experiment", "message": "run it", "spec": self.spec(SLEEPING)})
            self.assertEqual(result["status"], "dispatched", result)
            self.assertTrue(wait_until(lambda: (self.view() or {}).get("held_reason") == "runner_error:RuntimeError",
                                       CALL_LONG), self.view())
        run_id = seen[0]
        self.assertEqual(self.view()["host_commands"], [], "no listed command after the follower stopped")
        result = self.stop_service.stop("manager", run_id)
        self.assertNotEqual(result["status"], "exited", result)
        self.assertEqual(result.get("signals", []), [])
        self.assertEqual((self.env.opened, self.env.sent), ([], []), "no pid of a dead-owned entry was pinned")

    def test_a_failing_registration_does_not_leave_a_stoppable_entry(self):
        with mock.patch.object(self.stop_service, "register", side_effect=RuntimeError("registry broke")):
            result = self.to_worker({"kind": "experiment", "message": "run it", "spec": self.spec(SLEEPING)})
            self.assertEqual(result["status"], "dispatched", result)
            self.assertTrue(wait_until(lambda: (self.view() or {}).get("held_reason") == "runner_error:RuntimeError",
                                       CALL_LONG), self.view())
        run_id = self.view()["run_id"]
        self.assertEqual(self.view()["host_commands"], [])
        answer = self.stop_service.stop("manager", run_id)
        self.assertNotEqual(answer["status"], "exited", answer)
        self.assertEqual(answer.get("signals", []), [])
        self.assertEqual((self.env.opened, self.env.sent), ([], []))

    def test_a_stopped_runs_id_never_reaches_the_next_runs_command(self):
        first_task, first = self.running_experiment()
        self.stop_service.stop("manager", first)
        self.await_status("finished")
        second_task, second = self.running_experiment()
        self.assertNotEqual(first, second)
        second_child = self.child_pid()
        sent = list(self.env.sent)
        again = self.stop_service.stop("manager", first)
        self.assertEqual((again["status"], again["repeat"]), ("exited", True), again)
        self.assertEqual(again["exit_code"], 130, "the first run's own result")
        self.assertEqual(self.env.sent, sent, "the stale id signalled nothing")
        time.sleep(0.4)
        self.assertTrue(alive(second_child), "the second run's command was not touched")
        self.assertEqual(self.view()["status"], "running")
        stopped = self.stop_service.stop("manager", second)
        self.assertEqual((stopped["status"], stopped["command_id"]), ("exited", second), stopped)
        self.await_status("finished")
        self.assertEqual(self.view()["last_result"]["run_id"], second)
        self.assertEqual(self.view()["last_result"]["outcome"], "stopped")
        self.assertEqual(self.judge_calls, 0)


class StaticContractTests(unittest.TestCase):
    def test_the_ui_contract_documents_the_experiment_stop_without_a_new_status(self):
        text = ui_v1.__doc__ or ""
        for needle in ("stop_command", "run_id", "host_commands", "stopped"):
            self.assertIn(needle, text)
        self.assertNotIn("stopped", ui_v1.TASK_STATUSES, "a stopped run is an outcome, not a Task status")


class BackendFixture(RestartWorkerFixture):
    """Seam S2 on a real ``Backend`` with fake OMPs and a real host shell."""

    def setUp(self):
        patcher = mock.patch.object(Backend, "_make_workflow", lambda backend, repository: TaskWorkflow(
            repository, PublicMailboxFixture(repository)))
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        self.backend.command_stop.policy = POLICY
        self.source = Path(self._dir.name) / "source"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.email", "fixture@example.invalid")
        git(self.source, "config", "user.name", "CS09 Independent")
        (self.source / "tracked.txt").write_text("base\n")
        git(self.source, "add", "tracked.txt")
        git(self.source, "commit", "-qm", "baseline")
        self.commit = git(self.source, "rev-parse", "HEAD")

    def start_experiment(self, command=SLEEPING):
        spec = {"goal": "fixture run", "paths": ["outcome.txt"], "execution": {
            "source": str(self.source), "commit": self.commit, "command": command,
            "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
            "environment": ["PATH"], "shell": "bash"}}
        result = self.call(ActorRole.MANAGER, "to_worker", {"kind": "experiment", "message": "run it", "spec": spec})[0]
        self.assertEqual(result["status"], "dispatched", result)
        flow = self.backend.flow
        self.wait(lambda: flow.task_view()["status"] == "running" and flow.task_view()["run_id"], "the run", 60)
        run_id = flow.task_view()["run_id"]
        self.wait(lambda: flow.task_view()["host_commands"], "the command to be listed")
        return result["task_id"], run_id

    def journal(self):
        path = self.backend.layout.workflow / "handoffs.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def child(self):
        return self.backend.shell.shell.run_identity()["child"]["pid"]


class BackendStopTests(BackendFixture):
    def test_the_worker_cannot_stop_the_experiment_through_either_tool_and_nothing_is_signalled(self):
        task_id, run_id = self.start_experiment()
        child = self.child()
        refused = self.call(ActorRole.WORKER, "terminal", {"command": None, "stop": run_id})[0]
        self.assertEqual((refused["status"], refused["reason"], refused["command_id"], refused["requested_by"]),
                         ("refused", "not_owner", run_id, "worker"), refused)
        self.assertNotIn("signals", refused)
        denied = self.call(ActorRole.WORKER, "stop_command", {"command_id": run_id, "reason": "worker tries"})[0]
        self.assertNotEqual(denied.get("status"), "exited", denied)
        self.assertIn("tool_not_allowed_for_role", json.dumps(denied))
        time.sleep(0.6)
        self.assertTrue(alive(child), "the experiment's command keeps running")
        self.assertEqual(self.backend.flow.task_view()["status"], "running")
        refusals = [r for r in self.journal() if r.get("type") == "command_stop_refused"]
        self.assertEqual([(r["command_id"], r["requested_by"], r["reason"]) for r in refusals],
                         [(run_id, "worker", "not_owner")])
        self.assertEqual([r for r in self.journal() if r.get("type") == "command_stop_signal"], [])

    def test_the_task_surface_and_the_managers_stop_command_close_the_run_as_stopped(self):
        task_id, run_id = self.start_experiment()
        flow = self.backend.flow
        for shown in (self.backend.snapshot()["task"]["host_commands"], flow.status_view()["host_commands"]):
            self.assertEqual([(c["command_id"], c["kind"], c["owner"], c["state"]) for c in shown],
                             [(run_id, "experiment", "manager", "running")])
        stopped = self.call(ActorRole.MANAGER, "stop_command", {"command_id": run_id, "reason": "not needed"})[0]
        self.assertEqual((stopped["status"], stopped["requested_by"], stopped["exit_code"], stopped["signal"]),
                         ("exited", "manager", 130, 2), stopped)
        self.wait(lambda: flow.task_view()["status"] == "finished", "the Task to finish", 30)
        self.assertEqual(flow.task_view()["last_result"], {"run_id": run_id, "outcome": "stopped",
                                                           "stopped_by": "manager", "exit_code": 130, "signal": 2,
                                                           "run_closed": True})
        self.assertEqual(self.backend.snapshot()["task"]["host_commands"], [])
        records = self.journal()
        (requested,) = [r for r in records if r.get("type") == "command_stop_requested"]
        self.assertEqual((requested["command_id"], requested["kind"], requested["owner"], requested["task_id"],
                          requested["reason"]), (run_id, "experiment", "manager", task_id, "not needed"))
        self.assertTrue([r for r in records if r.get("type") == "command_stop_target" and r["command_id"] == run_id])
        self.assertEqual([r["status"] for r in records if r.get("type") == "command_stop_finished"], ["exited"])
        ledger = [json.loads(line) for line in (self.backend.layout.workflow / "tasks-flow.jsonl").read_text()
                  .splitlines() if line.strip()]
        self.assertEqual([(r["outcome"], r["stopped_by"]) for r in ledger
                          if r["type"] == "run_ended" and r.get("run_id") == run_id], [("stopped", "manager")])


class BackendCtrlCTests(BackendFixture):
    def interrupts(self):
        return [r for r in self.journal() if r.get("type") == "command_interrupt"]

    def test_a_user_ctrl_c_on_an_experiment_is_journaled_with_the_run_id_not_the_shell_request_id(self):
        task_id, run_id = self.start_experiment()
        identity = self.backend.shell.shell.run_identity()
        child = identity["child"]["pid"]
        self.assertNotEqual(identity["request_id"], run_id)
        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        self.wait(lambda: self.interrupts(), "the command_interrupt record")
        (record,) = self.interrupts()
        self.assertEqual((record["requested_by"], record["command_id"], record["pid"], record["delivered"]),
                         ("user", run_id, child, True), record)
        flow = self.backend.flow
        self.wait(lambda: flow.task_view()["status"] != "running", "the interrupted run to be collected", 30)
        # a user's Ctrl+C is not a stop episode: no stopped_by, no stop signal marker
        last = flow.task_view().get("last_result") or {}
        self.assertNotIn("stopped_by", last)
        self.assertNotEqual(last.get("outcome"), "stopped")
        self.assertEqual(self.backend.command_stop.stop_marker(run_id), {})

    def test_when_the_experiment_lookup_fails_the_interrupt_is_still_delivered_with_a_null_command_id(self):
        task_id, run_id = self.start_experiment()
        child = self.child()
        with mock.patch.object(self.backend.flow, "experiment_command_for", side_effect=RuntimeError("unreadable")):
            self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
            self.wait(lambda: self.interrupts(), "the command_interrupt record")
        (record,) = self.interrupts()
        self.assertEqual((record["command_id"], record["pid"], record["delivered"]), (None, child, True), record)
        self.wait(lambda: self.backend.flow.task_view()["status"] != "running", "the run to end", 30)


if __name__ == "__main__":
    unittest.main()
