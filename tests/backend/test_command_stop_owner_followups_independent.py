"""C-T3 (independent test_designer): owner follow-ups of the command-stop feature, derived from the decisions.

B1 (user decision 2026-10-23). When the stop registration fails for a terminal command or an experiment, the command
   keeps running, is journaled (``terminal_started``), is followed to its end and judged/reported normally. It is NOT
   stoppable: a later ``stop_command`` / ``terminal {stop}`` is answered ``refused`` / ``registration_failed`` with no
   signal; a requester who may not stop it at all still gets ``not_owner``. One audit record
   ``command_stop_register_failed`` (type, command_id, kind, owner, error = the exception's type name only, at) with
   no exception message. The running command stays in ``host_commands`` until it ends and is cleared after. A user
   Ctrl+C on such an experiment records ``command_interrupt`` with the right command id. It never reads as ended
   (``exited`` / ``already_exited``).
B2. Every registered ``StopTarget.forbidden()`` lists the CURRENT OMP pane pids (Manager + Worker), read lazily (a
   respawned pane's new pid appears), and nothing else (no host shell pid).
B3. One strict command id pattern, the bridge's ``UUID_PATTERN`` (read from ``omp_bridge/g3/bridge.ts`` here): version
   1-8, variant 8/9/a/b, lowercase. Manager ``stop_command`` and Worker ``terminal {stop}`` reject anything else as
   ``rejected`` / ``invalid_arguments`` and never echo the value; ids in the pattern are unaffected.

Seams: a real ``Backend`` (fake OMPs on real PTYs, a real host shell, real ``TerminalService`` / ``TaskFlow`` /
``CommandStopService``) driven through the bridge tool entry (``_tool_request``) and the user's Ctrl+C
(``Backend.admit``); the two argument validators for the id table. A structural seam for B2: the OMP pane's own session
and process group are written into the command's run identity, so a scan WOULD track the pane unless it is forbidden.

Expected values are this file's own: the fault classes and secret strings injected here, the uuids written here, the
bridge's regex read from its source, the pids the fake OMPs recorded, /proc reads for the sleep processes started here.
Process hygiene: every command runs in the backend's own host shell (closed with the backend) and ends through a gate
file or the user's Ctrl+C; nothing is signalled that this file did not start.
"""

from __future__ import annotations

import functools
import itertools
import json
import os
from pathlib import Path
import re
import time
import unittest
from unittest import mock
from uuid import uuid4

from workbench.backend.command_stop import LinuxStopEnvironment, scan_tracked
from workbench.backend.flow_recovery import validate_stop_command_arguments
from workbench.backend.flow_terminal import validate_terminal_arguments
from workbench.contracts.v1 import ActorRole, PaneId
from workbench.workflow.run import WorkflowRun

import test_command_stop_authority as authority
import test_experiment_command_stop_independent as experiment

REPO = Path(__file__).resolve().parents[2]
SECRET = "SECRET-registry-fault-text-7731"  # the message of the injected fault: it must appear nowhere
KEYS = {"type", "command_id", "kind", "owner", "error", "at"}
NOT_STOPPED = ("exited", "already_exited", "stopping")


def bridge_pattern() -> re.Pattern:
    """The bridge's command id pattern, parsed from its TypeScript source (the oracle for B3)."""
    text = (REPO / "omp_bridge" / "g3" / "bridge.ts").read_text()
    found = re.search(r"^const UUID_PATTERN = /(.+)/;$", text, re.M)
    assert found, "UUID_PATTERN not found in the bridge source"
    return re.compile(found.group(1))  # ^...$ anchors; the candidates have no newline except where tested separately


def bridge_accepts(value) -> bool:
    # JS ``$`` without the m flag matches only at the very end: Python's ``$`` also before a final newline, so test it so
    return isinstance(value, str) and not value.endswith("\n") and bridge_pattern().search(value) is not None


class Counting(LinuxStopEnvironment):
    """The real environment that records the pids it was asked to pin and the signals it sent."""

    def __init__(self):
        self.opened, self.sent = [], []

    def pidfd_open(self, pid):
        self.opened.append(pid)
        return super().pidfd_open(pid)

    def pidfd_send_signal(self, fd, signum):
        self.sent.append(signum)
        super().pidfd_send_signal(fd, signum)


class BackendHelpers:
    """Shared by the terminal and the experiment fixtures (both have ``self.backend``)."""

    def raw_journal(self) -> str:
        path = self.backend.layout.workflow / "handoffs.jsonl"
        return path.read_text() if path.exists() else ""

    def journal_records(self, *types):
        found = [json.loads(line) for line in self.raw_journal().splitlines() if line.strip()]
        return [r for r in found if r.get("type") in types] if types else found

    def fail_registration(self, error=None):
        """Every ``register`` raises (the targets offered are collected); returns that list."""
        offered = []
        error = error if error is not None else RuntimeError(SECRET)

        def refuse(target):
            offered.append(target)
            raise error
        patcher = mock.patch.object(self.backend.command_stop, "register", side_effect=refuse)
        patcher.start()
        self.addCleanup(patcher.stop)
        return offered

    def capture_registrations(self):
        """The real ``register`` runs; the targets offered are collected."""
        offered, real = [], self.backend.command_stop.register

        def forward(target):
            offered.append(target)
            return real(target)
        patcher = mock.patch.object(self.backend.command_stop, "register", side_effect=forward)
        patcher.start()
        self.addCleanup(patcher.stop)
        return offered

    def assert_register_failed_once(self, command_id, kind, owner, error):
        records = self.journal_records("command_stop_register_failed")
        self.assertEqual(len(records), 1, records)
        record = records[0]
        self.assertEqual(set(record) - {"seq"}, KEYS, record)  # ``seq`` is the journal's own envelope
        self.assertEqual((record["command_id"], record["kind"], record["owner"], record["error"]),
                         (command_id, kind, owner, error), record)
        self.assertTrue(isinstance(record["at"], str) and record["at"], record)
        self.assertNotIn(SECRET, self.raw_journal(), "the exception message reached the journal")

    def assert_refused_unregistered(self, answer, command_id, requested_by):
        self.assertEqual((answer["status"], answer["reason"]), ("refused", "registration_failed"), answer)
        self.assertEqual(answer.get("command_id"), command_id)
        self.assertEqual(answer.get("requested_by"), requested_by)
        for key in ("signals", "signal", "exit_code", "repeat", "ended_at"):
            self.assertNotIn(key, answer, "a refusal carries no outcome of an end or a signal")

    def omp_pane_pids(self):
        return {entry["pid"] for entry in self.invocations()}

    def live_omp_pids(self):
        return {self.backend.panes[PaneId.MANAGER_OMP].pid, self.backend.panes[PaneId.WORKER_OMP].pid}

    def shell_pids(self):
        identity = self.backend.shell.shell.run_identity() or {}
        return {self.backend.shell.pid, *(ref["pid"] for ref in (identity.get("parent"),) if ref)}

    def assert_pane_pids_survive_a_stop(self, target, command_id):
        """B2, structural: write the Worker OMP's own session/process group into the command's identity so that a
        scan tracks the pane unless it is forbidden; then ask for a stop and require that the pane is never pinned."""
        worker = self.backend.panes[PaneId.WORKER_OMP]
        # the run identity can still read ``starting`` for a moment after the command was listed: wait (bounded) for it
        self.wait(lambda: (target.identity() or {}).get("phase") == "running", "the run identity to be running", 30)
        real = target.identity()
        self.assertTrue(real and real.get("phase") == "running", real)
        spoofed = {**real, "session": os.getsid(worker.pid), "child_group": os.getpgid(worker.pid),
                   "uid": os.getuid()}
        control = scan_tracked(spoofed, LinuxStopEnvironment(), forbidden=frozenset())
        self.assertIn(worker.pid, [stat.pid for stat in control.tracked],
                      "the seam is real: without forbidden the scan tracks the OMP pane")
        listed = scan_tracked(spoofed, LinuxStopEnvironment(), forbidden=target.forbidden())
        self.assertNotIn(worker.pid, [stat.pid for stat in listed.tracked])
        counting = Counting()
        previous_env, previous_identity = self.backend.command_stop.env, target.identity
        self.backend.command_stop.env, target.identity = counting, lambda: spoofed
        try:
            answer = self.backend.command_stop.stop("manager", command_id, reason="spoofed identity probe")
        finally:
            self.backend.command_stop.env, target.identity = previous_env, previous_identity
        self.assertNotIn(worker.pid, counting.opened, "an OMP pane was pinned for signalling")
        self.assertEqual(counting.sent, [], "a signal was sent although the only tracked member was forbidden")
        self.assertNotEqual(answer.get("status"), "exited", answer)
        self.assertTrue(experiment.alive(worker.pid) and experiment.alive(self.backend.panes[PaneId.MANAGER_OMP].pid))
        return answer


# ----------------------------------------------------------------------------------------------------------------
# Terminal command (owner worker)
# ----------------------------------------------------------------------------------------------------------------
class TerminalFixture(BackendHelpers, authority.BackendStopFixture):
    def gated(self, tag):
        gate = Path(self._dir.name) / f"gate-{tag}"
        self.addCleanup(lambda: gate.write_text("go"))  # a failed test never leaves the command running
        return gate, f"while [ ! -e {gate} ]; do sleep 0.1; done; echo b1-{tag}-done"

    def await_ended(self, command_id):
        self.wait(lambda: not self.backend.terminal.current().get("running"), "the command to end", 30)

    def running_ids(self, task):
        return [c["command_id"] for c in self.backend.terminal.commands_for_task(task.task_id)]


class TerminalRegistrationFailureTests(TerminalFixture):
    def test_the_command_runs_is_listed_unstoppable_and_ends_normally(self):
        task = self.open_free_task()
        self.fail_registration(OSError(SECRET))
        gate, script = self.gated("t1")
        started = self.worker_terminal(command=script)
        self.assertEqual(started["status"], "running", started)
        command_id = started["command_id"]
        # journaled as started, one audit record, no text of the fault anywhere
        self.assertEqual([r["command_id"] for r in self.journal_records("terminal_started")], [command_id])
        self.assert_register_failed_once(command_id, "terminal", "worker", "OSError")
        self.assertEqual([r for r in self.journal_records("command_stop_target")
                          if r.get("command_id") == command_id], [], "it was never registered")
        # listed on every surface while it runs
        self.assertEqual(self.running_ids(task), [command_id])
        self.assertEqual([c["command_id"] for c in self.backend.snapshot()["task"]["host_commands"]], [command_id])
        # both requesters get the refusal; nothing is signalled; asking twice changes nothing
        for requester, answer in (("manager", self.manager_stop(command_id)),
                                  ("worker", self.worker_terminal(stop=command_id)),
                                  ("manager", self.manager_stop(command_id))):
            self.assert_refused_unregistered(answer, command_id, requester)
        self.assertEqual(self.records("command_stop_requested", "command_stop_signal", "command_stop_finished"), [])
        self.assertEqual(self.worker_terminal()["status"], "running", "still running")
        self.assert_register_failed_once(command_id, "terminal", "worker", "OSError")  # still one record
        # an id the registry never saw is still not_found, not "registration_failed"
        unknown = self.manager_stop(str(uuid4()))
        self.assertEqual((unknown["status"], unknown["reason"]), ("not_found", "unknown_command"), unknown)
        # followed to its end and reported
        gate.write_text("go")
        self.await_ended(command_id)
        final = self.worker_terminal()
        self.assertEqual((final["status"], final.get("exit_code")), ("exited", 0), final)
        self.assertEqual([r["command_id"] for r in self.journal_records("terminal_ended")], [command_id])
        self.assertEqual(self.running_ids(task), [])
        self.assertEqual(self.backend.snapshot()["task"]["host_commands"], [])
        # after the end it still reads "could not be registered", never as an ended command
        for requester in (self.manager_stop, lambda cid: self.worker_terminal(stop=cid)):
            self.assert_refused_unregistered(requester(command_id), command_id,
                                             "manager" if requester == self.manager_stop else "worker")
        self.assertEqual(self.records("command_stop_finished"), [])

    def test_the_next_command_is_stoppable_again_and_the_failed_one_stays_unstoppable(self):
        self.open_free_task()
        gate, script = self.gated("t2")
        with mock.patch.object(self.backend.command_stop, "register", side_effect=RuntimeError(SECRET)):
            first = self.worker_terminal(command=script)["command_id"]
        gate.write_text("go")
        self.await_ended(first)
        second = self.run_command("sleep 6502.2")
        (pid,) = authority.pids_running("sleep", "6502.2")
        self.assertEqual(self.manager_stop(second["command_id"])["status"], "exited")
        self.assertFalse(experiment.alive(pid))
        self.assert_refused_unregistered(self.manager_stop(first), first, "manager")
        (record,) = self.journal_records("command_stop_register_failed")
        self.assertEqual(record["command_id"], first, "the successful registration wrote no failure record")

    def test_a_service_that_cannot_even_record_the_failure_still_lets_the_command_run_and_end(self):
        self.open_free_task()
        gate, script = self.gated("t3")
        with mock.patch.object(self.backend.command_stop, "register", side_effect=RuntimeError(SECRET)), \
                mock.patch.object(self.backend.command_stop, "registration_failed", side_effect=OSError(SECRET)):
            started = self.worker_terminal(command=script)
        self.assertEqual(started["status"], "running", started)
        gate.write_text("go")
        self.await_ended(started["command_id"])
        final = self.worker_terminal()
        self.assertEqual((final["status"], final.get("exit_code")), ("exited", 0), final)
        self.assertEqual([r["command_id"] for r in self.journal_records("terminal_ended")], [started["command_id"]])

    def test_the_failure_record_is_not_written_when_registration_succeeds(self):
        started = self.run_command("sleep 6502.3")
        self.assertEqual(self.journal_records("command_stop_register_failed"), [])
        self.assertEqual(self.manager_stop(started["command_id"])["status"], "exited")


# ----------------------------------------------------------------------------------------------------------------
# Experiment (owner manager)
# ----------------------------------------------------------------------------------------------------------------
class ExperimentFixture(BackendHelpers, experiment.BackendFixture):
    def setUp(self):
        super().setUp()
        self.judge_calls = 0
        original = WorkflowRun.judge

        @functools.wraps(original)
        def counting(run, *args, **kwargs):
            self.judge_calls += 1
            return original(run, *args, **kwargs)
        patcher = mock.patch.object(WorkflowRun, "judge", counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def gated_command(self, tag):
        gate = Path(self._dir.name) / f"gate-{tag}"
        self.addCleanup(lambda: gate.write_text("go"))
        return gate, (f"printf 'started\\n'; while [ ! -e {gate} ]; do sleep 0.1; done; "
                      "printf PASS; printf PASS > outcome.txt")

    def interrupts(self):
        return self.journal_records("command_interrupt")


class ExperimentRegistrationFailureTests(ExperimentFixture):
    def test_the_run_is_followed_and_judged_but_not_stoppable(self):
        self.fail_registration(ValueError(SECRET))
        gate, command = self.gated_command("e1")
        task_id, run_id = self.start_experiment(command)  # it returns only once host_commands lists the command
        flow = self.backend.flow
        child = self.child()
        self.assertEqual(flow.task_view()["status"], "running", "not held")
        self.assertEqual([c["command_id"] for c in self.backend.snapshot()["task"]["host_commands"]], [run_id])
        self.assert_register_failed_once(run_id, "experiment", "manager", "ValueError")
        self.assertEqual([r for r in self.journal_records("command_stop_target")
                          if r.get("command_id") == run_id], [])
        # the Manager is refused with registration_failed; the Worker may not stop a Manager command at all
        self.assert_refused_unregistered(self.call(ActorRole.MANAGER, "stop_command",
                                                   {"command_id": run_id, "reason": "not needed"})[0],
                                         run_id, "manager")
        denied = self.call(ActorRole.WORKER, "terminal", {"command": None, "stop": run_id})[0]
        self.assertEqual((denied["status"], denied["reason"]), ("refused", "not_owner"), denied)
        self.assertNotIn("signals", denied)
        self.assertEqual(self.records_of_stop(), [], "nothing was requested or signalled")
        time.sleep(0.5)
        self.assertTrue(experiment.alive(child), "the experiment's command keeps running")
        self.assertEqual(flow.task_view()["status"], "running")
        self.assert_register_failed_once(run_id, "experiment", "manager", "ValueError")
        # it finishes by itself and is judged exactly once; the surfaces clear
        gate.write_text("go")
        self.wait(lambda: flow.task_view()["status"] == "finished", "the run to finish", 60)
        self.assertEqual(self.judge_calls, 1, "followed and judged as any run")
        last = flow.task_view()["last_result"]
        self.assertEqual(last["run_id"], run_id)
        self.assertNotEqual(last.get("outcome"), "stopped", last)
        self.assertNotIn("stopped_by", last)
        self.assertEqual(self.backend.snapshot()["task"]["host_commands"], [])
        self.assertEqual(flow.status_view()["host_commands"], [])
        # and never an end the stop service knows
        again = self.call(ActorRole.MANAGER, "stop_command", {"command_id": run_id, "reason": "late"})[0]
        self.assert_refused_unregistered(again, run_id, "manager")
        self.assertEqual(self.records_of_stop(), [])

    def records_of_stop(self):
        return self.journal_records("command_stop_requested", "command_stop_signal", "command_stop_finished",
                                    "command_stop_ended")

    def test_a_user_ctrl_c_records_the_run_id_and_the_run_ends(self):
        self.fail_registration(RuntimeError(SECRET))
        task_id, run_id = self.start_experiment()
        identity = experiment.identity_with_child(self.backend.shell.shell)
        child = identity["child"]["pid"]
        self.assertNotEqual(identity["request_id"], run_id)
        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        self.wait(lambda: self.interrupts(), "the command_interrupt record")
        (record,) = self.interrupts()
        self.assertEqual((record["requested_by"], record["command_id"], record["pid"], record["delivered"]),
                         ("user", run_id, child, True), record)
        self.wait(lambda: self.backend.flow.task_view()["status"] != "running", "the interrupted run to end", 60)
        self.assertNotIn("stopped_by", self.backend.flow.task_view().get("last_result") or {})
        self.assertEqual(self.backend.snapshot()["task"]["host_commands"], [])
        self.assert_register_failed_once(run_id, "experiment", "manager", "RuntimeError")
        after = self.call(ActorRole.MANAGER, "stop_command", {"command_id": run_id, "reason": "late"})[0]
        self.assert_refused_unregistered(after, run_id, "manager")

    def test_the_audit_record_names_only_the_type_of_a_custom_exception(self):
        class RegistryDown(Exception):
            pass
        self.fail_registration(RegistryDown(SECRET, {"nested": SECRET}))
        gate, command = self.gated_command("e3")
        task_id, run_id = self.start_experiment(command)
        self.assert_register_failed_once(run_id, "experiment", "manager", "RegistryDown")
        gate.write_text("go")
        self.wait(lambda: self.backend.flow.task_view()["status"] == "finished", "the run to finish", 60)
        self.assertNotIn(SECRET, json.dumps(self.backend.snapshot(), default=str))
        self.assertNotIn(SECRET, self.raw_journal())
        self.assertNotIn(SECRET, (self.backend.layout.workflow / "tasks-flow.jsonl").read_text())


# ----------------------------------------------------------------------------------------------------------------
# B2: forbidden pids
# ----------------------------------------------------------------------------------------------------------------
class ForbiddenPidsTerminalTests(TerminalFixture):
    def test_a_terminal_target_forbids_exactly_the_current_omp_pane_pids_and_follows_a_respawn(self):
        offered = self.capture_registrations()
        started = self.run_command("sleep 6503.1")
        (target,) = offered
        self.assertEqual(target.command_id, started["command_id"])
        manager, worker = self.backend.panes[PaneId.MANAGER_OMP], self.backend.panes[PaneId.WORKER_OMP]
        self.assertEqual(set(target.forbidden()), {manager.pid, worker.pid})
        self.assertEqual(self.omp_pane_pids(), {manager.pid, worker.pid}, "the fake OMPs recorded the same pids")
        self.assertFalse(set(target.forbidden()) & self.shell_pids(), "a non-OMP pane pid was listed")
        old_worker = worker.pid
        result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "respawn for the forbidden check"})
        self.assertEqual(result["status"], "restarted", result)
        new_worker = self.backend.panes[PaneId.WORKER_OMP].pid
        self.assertNotEqual(new_worker, old_worker)
        self.assertEqual(set(target.forbidden()), {manager.pid, new_worker},
                         "read lazily: the respawned pane is listed, the dead one is not")
        self.assertFalse(set(target.forbidden()) & self.shell_pids())

    def test_an_omp_pane_is_never_pinned_or_signalled_even_when_the_scan_would_track_it(self):
        offered = self.capture_registrations()
        started = self.run_command("sleep 6503.2")
        (target,) = offered
        self.assert_pane_pids_survive_a_stop(target, started["command_id"])
        (pid,) = authority.pids_running("sleep", "6503.2")
        self.assertTrue(experiment.alive(pid), "the real command was not signalled either")


class ForbiddenPidsExperimentTests(ExperimentFixture):
    def test_an_experiment_target_forbids_exactly_the_current_omp_pane_pids_and_follows_a_respawn(self):
        offered = self.capture_registrations()
        gate, command = self.gated_command("f1")
        task_id, run_id = self.start_experiment(command)
        (target,) = offered
        self.assertEqual(target.command_id, run_id)
        manager, worker = self.backend.panes[PaneId.MANAGER_OMP], self.backend.panes[PaneId.WORKER_OMP]
        self.assertEqual(set(target.forbidden()), {manager.pid, worker.pid})
        self.assertFalse(set(target.forbidden()) & self.shell_pids())
        old_worker = worker.pid
        result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "respawn for the forbidden check"})
        self.assertEqual(result["status"], "restarted", result)
        new_worker = self.backend.panes[PaneId.WORKER_OMP].pid
        self.assertNotEqual(new_worker, old_worker)
        self.assertEqual(set(target.forbidden()), {manager.pid, new_worker})
        gate.write_text("go")

    def test_an_omp_pane_is_never_pinned_or_signalled_even_when_the_scan_would_track_it(self):
        offered = self.capture_registrations()
        gate, command = self.gated_command("f2")
        task_id, run_id = self.start_experiment(command)
        child = self.child()
        (target,) = offered
        self.assert_pane_pids_survive_a_stop(target, run_id)
        self.assertTrue(experiment.alive(child), "the experiment's command was not signalled")
        gate.write_text("go")


# ----------------------------------------------------------------------------------------------------------------
# B3: one strict command id
# ----------------------------------------------------------------------------------------------------------------
class IdPatternTableTests(unittest.TestCase):
    def candidates(self):
        base = "0f8fad5b-d9cb-{v}69f-{w}165-70867728950e"
        nibbles = "0123456789abcdefABCDEF"
        found = [base.format(v=v, w=w) for v, w in itertools.product(nibbles, nibbles)]
        found += [str(uuid4()) for _ in range(5)]
        found += [
            "00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "00000000-0000-1000-8000-000000000000", "ffffffff-ffff-8fff-bfff-ffffffffffff",
            "0F8FAD5B-D9CB-469F-A165-70867728950E",                 # upper case
            "0f8fad5b-d9cb-469f-a165-70867728950e\n",               # trailing newline
            "\n0f8fad5b-d9cb-469f-a165-70867728950e",
            " 0f8fad5b-d9cb-469f-a165-70867728950e", "0f8fad5b-d9cb-469f-a165-70867728950e ",
            "{0f8fad5b-d9cb-469f-a165-70867728950e}", "urn:uuid:0f8fad5b-d9cb-469f-a165-70867728950e",
            "0f8fad5bd9cb469fa16570867728950e", "0f8fad5b-d9cb-469f-a165-70867728950",
            "0f8fad5b-d9cb-469f-a165-70867728950ee", "0f8fad5b-d9cb-469f-a165-70867728950e-1",
            "0f8fad5b_d9cb_469f_a165_70867728950e", "0f8fad5b-d9cb-469f-a165-70867728950g",
            "０f8fad5b-d9cb-469f-a165-70867728950e",             # full-width digit
            "0f8fad5b-d9cb-469f-a165-70867728950e\x00", "\x000f8fad5b-d9cb-469f-a165-70867728950e",
            "0f8fad5b-d9cb-469f-a165-70867728950e" * 3, "a" * 5000, "../../etc/passwd", "*", "all", "-1",
        ]
        return found

    def test_both_validators_agree_with_the_bridge_on_every_candidate(self):
        pattern = bridge_pattern()
        self.assertIn("[1-8]", pattern.pattern)
        self.assertIn("[89ab]", pattern.pattern)
        disagreements = []
        for value in self.candidates():
            want = bridge_accepts(value)
            manager = not any(e.startswith("command_id") for e in
                              validate_stop_command_arguments({"command_id": value, "reason": "x"}))
            terminal = validate_terminal_arguments({"command": None, "stop": value}) == []
            if (manager, terminal) != (want, want):
                disagreements.append((value[:60], want, manager, terminal))
        self.assertEqual(disagreements, [])

    def test_accepted_in_pattern_ids_include_the_versions_and_variants_the_old_manager_pattern_also_took(self):
        for value in ("0f8fad5b-d9cb-169f-8165-70867728950e", "0f8fad5b-d9cb-869f-b165-70867728950e",
                      "0f8fad5b-d9cb-469f-9165-70867728950e", "0f8fad5b-d9cb-569f-a165-70867728950e"):
            self.assertEqual(validate_stop_command_arguments({"command_id": value, "reason": "x"}), [], value)
            self.assertEqual(validate_terminal_arguments({"command": None, "stop": value}), [], value)

    def test_a_rejection_never_echoes_the_value(self):
        for value in ("0f8fad5b-d9cb-969f-a165-70867728950e", "SECRETMARK-not-an-id-4412", "0F8FAD5B-D9CB-469F-A165-"
                      "70867728950E"):
            text = " ".join(validate_stop_command_arguments({"command_id": value, "reason": "x"})
                            + validate_terminal_arguments({"command": None, "stop": value}))
            self.assertTrue(text)
            self.assertNotIn(value, text)
            self.assertNotIn(value.lower(), text.lower())

    def test_non_string_ids_are_rejected_by_both(self):
        for value in (None, 0, 1.5, True, [], {}, ["0f8fad5b-d9cb-469f-a165-70867728950e"]):
            self.assertTrue(validate_stop_command_arguments({"command_id": value, "reason": "x"}), value)
            if value is not None:  # a null ``stop`` is "not used" for the terminal
                self.assertTrue(validate_terminal_arguments({"command": None, "stop": value}), value)


class IdPatternBackendTests(TerminalFixture):
    OUT = ("0f8fad5b-d9cb-969f-a165-70867728950e", "0f8fad5b-d9cb-069f-a165-70867728950e",
           "0f8fad5b-d9cb-469f-c165-70867728950e", "SECRETMARK-out-of-pattern-9917")

    def assert_rejected_without_echo(self, answer, value):
        self.assertEqual((answer["status"], answer["reason"]), ("rejected", "invalid_arguments"), answer)
        self.assertNotIn(value, json.dumps(answer), "the answer echoed the value")
        self.assertNotIn(value, self.raw_journal(), "the journal holds the value")

    def test_out_of_pattern_ids_are_rejected_by_both_tools_and_never_reach_the_stop_service(self):
        started = self.run_command("sleep 6504.1")
        real = started["command_id"]
        mutated = real[:14] + "9" + real[15:]            # the real id with a version nibble out of the pattern
        upper = real.upper()                              # the real id in upper case: must not stop it
        (pid,) = authority.pids_running("sleep", "6504.1")
        for value in (*self.OUT, mutated, upper):
            self.assert_rejected_without_echo(self.manager_stop(value), value)
            self.assert_rejected_without_echo(self.worker_terminal(stop=value), value)
        self.assertEqual(self.records("command_stop_requested", "command_stop_signal", "command_stop_refused"), [])
        self.assertTrue(experiment.alive(pid), "an out-of-pattern id stopped the real command")
        self.assertEqual(self.worker_terminal()["status"], "running")
        # the valid id is unaffected
        stopped = self.manager_stop(real)
        self.assertEqual((stopped["status"], stopped["command_id"]), ("exited", real), stopped)
        self.assertFalse(experiment.alive(pid))

    def test_valid_ids_in_the_pattern_reach_the_service(self):
        for value in ("0f8fad5b-d9cb-169f-8165-70867728950e", "0f8fad5b-d9cb-869f-b165-70867728950e"):
            for answer in (self.manager_stop(value), self.worker_terminal(stop=value)):
                self.assertEqual((answer["status"], answer["reason"]), ("not_found", "unknown_command"), answer)


if __name__ == "__main__":
    unittest.main()
