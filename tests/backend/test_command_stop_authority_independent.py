"""CS-05 independent verification: who may stop which command, and the stop paths around it.

Derived from the contract (docs/features/command-stop SPEC "Manager: stop_command", "인가 매트릭스", "결과 계약"
(refused), "감사 기록", "테스트 결정" S2/S3; BRIEF CS-AC-06/11/13/14/15/20/22 and CS-D02/D06/D08/D09/D13; ticket
CS-05), not from the implementation or from the worker's own tests.

Seams
  S3  ``CommandStopService`` with fake providers and a counting ``StopEnvironment``: the owner x requester matrix,
      the previous-backend classifier, refusals that must not mutate a command's one episode.
  S2  a real ``Backend`` (fake OMPs, real PTY host shell): the Manager's ``stop_command`` and the Worker's
      ``terminal`` ``stop`` both enter through ``Backend._tool_request``.

Independent oracles (never the production formatters): the BRIEF matrix written out below, exit codes 128+n of
commands this file starts, ``/proc`` reads done here (pid, ppid, pgrp, session, start ticks, state), signal logs
written by shell ``trap`` handlers of the test's own commands (so a signal that was delivered but ignored is still
seen), the session ids and Task states the test creates, and two sentinel processes outside every command tree
(one in its own session, one in the test runner's own session) that must survive every probe.

Safety: the only processes that are signalled are the commands this file starts inside the backend's host shell (by
the product's own stop path). Sentinels are killed here, by exact pid and start ticks. Every command carries a
distinctive ``6xx.<tag>`` mark; a leftover process with the mark after teardown fails the test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import random
import signal
import subprocess
import threading
import time
import unittest
from uuid import uuid4

from workbench.app.recovery import BOOT_HOLD
from workbench.backend.command_stop import CommandStopService, LinuxStopEnvironment, StopPolicy, StopTarget
from workbench.contracts.v1 import ActorRole

from test_cd70_restart_worker import RestartWorkerFixture

# the test's own policy constants (production defaults are 3/10/5/5/25 s and are checked elsewhere)
OWN = StopPolicy(identity_wait=2.0, sigint_wait=1.5, sigterm_wait=1.0, sigkill_wait=1.0, call_wait=12.0)
SHORT = StopPolicy(identity_wait=0.3, sigint_wait=0.2, sigterm_wait=0.2, sigkill_wait=0.2, call_wait=4.0,
                   poll_interval=0.01)
TAG = f"{random.randrange(100, 1000)}"

# BRIEF "인가 매트릭스" written out here: (requester, owner of the command) pairs that are allowed
ALLOWED = {("worker", "worker"), ("manager", "worker"), ("manager", "manager")}
OWNERS = ("worker", "manager")
REQUESTERS = ("worker", "manager")


# -- /proc oracle (read here) ------------------------------------------------------------------------------
from command_stop_support import alive, pids_with, stat_of  # (state, ppid, pgrp, session, start_ticks) or None


def kill_exact(pid: int, ticks: int) -> None:
    """SIGKILL this exact process (pid + start ticks), through a pidfd opened after the check."""
    if not alive(pid, ticks):
        return
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if alive(pid, ticks):
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(fd)


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


# =============================================================================================================
# S3: the authorization rules on the stop service (fake providers, no process is ever reached)
# =============================================================================================================
class Registry:
    def __init__(self, *, previous=None, policy=SHORT):
        self.records: list[dict] = []
        self.env = CountingEnvironment()
        self.identity_reads = 0
        self.service = CommandStopService(journal=self.records.append, policy=policy, env=self.env,
                                          previous_backend=previous)

    def register(self, owner: str, *, kind: str | None = None, task_id=None, task_status=None, identity=None):
        command_id = str(uuid4())

        def read():
            self.identity_reads += 1
            return identity() if identity else None
        extra = {"task_status": task_status} if task_status else {}
        self.service.register(StopTarget(command_id, owner, kind or ("terminal" if owner == "worker" else "experiment"),
                                         task_id, identity=read, **extra))
        return command_id

    def of(self, *types):
        return [r for r in self.records if r["type"] in types]

    def untouched(self, test):
        test.assertEqual((self.env.opened, self.env.sent, self.identity_reads), ([], [], 0),
                         "a refusal must not read an identity, open a pidfd or signal")
        test.assertEqual(self.of("command_stop_signal", "command_stop_target", "command_stop_requested"), [])


class AuthorizationMatrixTests(unittest.TestCase):
    """CS-AC-06 / CS-D02: the owner is looked up in the registry and compared with the requester."""

    def test_the_whole_matrix_for_a_live_command(self):
        for requester in REQUESTERS:
            for owner in OWNERS:
                with self.subTest(requester=requester, owner=owner, state="live"):
                    registry = Registry()
                    command_id = registry.register(owner)
                    result = registry.service.stop(requester, command_id, reason="matrix")
                    self.assertEqual((result["command_id"], result["requested_by"]), (command_id, requester))
                    if (requester, owner) in ALLOWED:
                        self.assertNotEqual(result["status"], "refused", result)
                        self.assertNotEqual(result.get("reason"), "not_owner", result)
                        (requested,) = registry.of("command_stop_requested")
                        self.assertEqual((requested["requested_by"], requested["owner"]), (requester, owner))
                        self.assertEqual(registry.of("command_stop_refused"), [])
                    else:
                        self.assertEqual((result["status"], result["reason"]), ("refused", "not_owner"), result)
                        registry.untouched(self)
                        (refused,) = registry.of("command_stop_refused")
                        self.assertEqual((refused["command_id"], refused["requested_by"], refused["status"],
                                          refused["reason"]), (command_id, requester, "refused", "not_owner"))

    def test_the_whole_matrix_for_a_command_that_ended(self):
        end = {"status": "exited", "exit_code": 7, "signal": None}
        for requester in REQUESTERS:
            for owner in OWNERS:
                with self.subTest(requester=requester, owner=owner, state="ended"):
                    registry = Registry()
                    command_id = registry.register(owner)
                    registry.service.command_ended(command_id, end)
                    result = registry.service.stop(requester, command_id)
                    if (requester, owner) in ALLOWED:
                        self.assertEqual((result["status"], result["exit_code"]), ("already_exited", 7), result)
                    else:  # an id the requester may not touch tells it nothing about the end
                        self.assertEqual((result["status"], result["reason"]), ("refused", "not_owner"), result)
                        self.assertNotIn("exit_code", result)
                        self.assertNotIn("ended_at", result)
                    self.assertEqual((registry.env.opened, registry.env.sent, registry.identity_reads), ([], [], 0))
                    self.assertEqual(registry.of("command_stop_signal", "command_stop_target"), [])

    def test_the_requester_authority_table_is_exactly_worker_manager_and_user(self):
        """CS-O10 (user decision 2026-10-24): the user, through the UI stop key, joins the Worker and the Manager.

        The table is written out here (FOLLOWUPS-BRIEF FU-D07): worker -> {worker}, manager -> {worker, manager},
        user -> {worker, manager}. Every other requester string, including case and whitespace variants of the
        three, is refused ``not_owner`` before an identity is read or a signal is sent.
        """
        table = {"worker": {"worker"}, "manager": {"worker", "manager"}, "user": {"worker", "manager"}}
        for requester, targets in table.items():
            for owner in OWNERS:
                with self.subTest(requester=requester, owner=owner):
                    registry = Registry()
                    command_id = registry.register(owner)
                    result = registry.service.stop(requester, command_id)
                    self.assertEqual((result["command_id"], result["requested_by"]), (command_id, requester))
                    if owner in targets:
                        self.assertNotEqual((result["status"], result.get("reason")), ("refused", "not_owner"), result)
                        (requested,) = registry.of("command_stop_requested")
                        self.assertEqual((requested["requested_by"], requested["owner"]), (requester, owner))
                    else:
                        self.assertEqual((result["status"], result["reason"]), ("refused", "not_owner"), result)
                        registry.untouched(self)
        for requester in ("system", "", "Manager", "WORKER", "worker ", "User", "USER", " user", "user ", "user\n",
                          "ui", "root", "admin", "workbench", None):
            with self.subTest(requester=repr(requester)):
                registry = Registry()
                command_id = registry.register("worker")
                result = registry.service.stop(requester, command_id)
                self.assertEqual((result["status"], result["reason"]), ("refused", "not_owner"), result)
                registry.untouched(self)

    def test_an_unknown_id_is_not_found_whoever_asks_and_signals_nothing(self):
        for requester in REQUESTERS:
            with self.subTest(requester=requester):
                registry = Registry()
                registry.register("worker")
                registry.register("manager")
                other = str(uuid4())
                result = registry.service.stop(requester, other)
                self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)
                self.assertEqual(registry.of("command_stop_refused")[0]["command_id"], other)
                self.assertEqual((registry.env.opened, registry.env.sent), ([], []))
                self.assertEqual(registry.identity_reads, 0, "no other command's identity is read for it")

    def test_a_refusal_does_not_poison_the_commands_one_episode(self):
        """The Worker's refused attempts must neither start an episode nor count as a joiner of the Manager's."""
        registry = Registry(policy=StopPolicy(identity_wait=0.8, sigint_wait=0.2, sigterm_wait=0.2,
                                              sigkill_wait=0.2, call_wait=5.0, poll_interval=0.01))
        command_id = registry.register("manager")
        for _ in range(3):
            self.assertEqual(registry.service.stop("worker", command_id)["reason"], "not_owner")
        self.assertEqual(registry.of("command_stop_requested"), [], "refusals start no stop")
        box = {}
        thread = threading.Thread(target=lambda: box.update(result=registry.service.stop("manager", command_id,
                                                                                       reason="mine")))
        thread.start()
        deadline = time.monotonic() + 30
        while registry.service.surface(command_id) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(registry.service.surface(command_id), "the Manager's episode is under way")
        during = registry.service.stop("worker", command_id)  # while the Manager's episode runs
        self.assertEqual((during["status"], during["reason"]), ("refused", "not_owner"), during)
        self.assertNotIn("repeat", during)
        self.assertNotIn("stop", during, "the Worker is shown nothing of a command it may not stop")
        thread.join(30)
        result = box["result"]
        self.assertEqual(result["requested_by"], "manager")
        self.assertNotIn("repeat", result, "the Manager's stop is the original, not a repeat")
        (finished,) = registry.of("command_stop_finished")
        self.assertEqual(finished["repeat_requests"], 0, "refused requests are not joiners")
        self.assertEqual(registry.of("command_stop_repeat"), [])
        self.assertEqual(len(registry.of("command_stop_refused")), 4)
        after = registry.service.stop("worker", command_id)
        self.assertEqual(after["reason"], "not_owner", "after the episode too: no original result is shown")
        self.assertNotIn("exit_code", after)

    def test_a_manager_join_of_a_worker_command_is_a_repeat_of_the_workers_episode(self):
        registry = Registry(policy=StopPolicy(identity_wait=0.8, sigint_wait=0.2, sigterm_wait=0.2,
                                              sigkill_wait=0.2, call_wait=5.0, poll_interval=0.01))
        command_id = registry.register("worker")
        box = {}
        thread = threading.Thread(target=lambda: box.update(result=registry.service.stop("worker", command_id)))
        thread.start()
        deadline = time.monotonic() + 30
        while registry.service.surface(command_id) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        joined = registry.service.stop("manager", command_id, reason="me too")
        thread.join(30)
        self.assertEqual(box["result"]["requested_by"], "worker")
        self.assertEqual((joined["requested_by"], joined.get("repeat")), ("worker", True), joined)
        self.assertEqual(len(registry.of("command_stop_requested")), 1, "one episode, one request record")


class PreviousBackendClassifierTests(unittest.TestCase):
    """CS-AC-13 / C-D71 (1): an id only the journal knows is never stopped from here."""

    def test_the_classifier_is_asked_only_about_ids_the_registry_does_not_hold(self):
        asked = []

        def everything_is_previous(command_id):
            asked.append(command_id)
            return True
        registry = Registry(previous=everything_is_previous)
        live_manager, live_worker = registry.register("manager"), registry.register("worker")
        ended = registry.register("worker")
        registry.service.command_ended(ended, {"status": "exited", "exit_code": 0, "signal": None})
        self.assertEqual(registry.service.stop("worker", live_manager)["reason"], "not_owner")
        self.assertEqual(registry.service.stop("worker", ended)["status"], "already_exited")
        self.assertNotEqual(registry.service.stop("manager", live_worker)["status"], "refused")
        self.assertEqual(asked, [], "a held or remembered id is classified by its owner, never as a survivor")
        unknown = str(uuid4())
        result = registry.service.stop("worker", unknown)
        self.assertEqual((result["status"], result["reason"]), ("refused", "previous_backend_command"), result)
        self.assertEqual(asked, [unknown])

    def test_both_requesters_are_refused_with_the_stop_survivor_pointer_and_no_signal(self):
        previous = str(uuid4())
        registry = Registry(previous=lambda command_id: command_id == previous)
        for requester in REQUESTERS:
            result = registry.service.stop(requester, previous, reason="please")
            self.assertEqual((result["status"], result["reason"], result["requested_by"], result["command_id"]),
                             ("refused", "previous_backend_command", requester, previous), result)
            self.assertIn("stop_survivor", result["detail"])
            self.assertNotIn("exit_code", result)
        registry.untouched(self)
        refused = registry.of("command_stop_refused")
        self.assertEqual([(r["requested_by"], r["status"], r["reason"]) for r in refused],
                         [(r, "refused", "previous_backend_command") for r in REQUESTERS])

    def test_a_classifier_that_fails_claims_nothing(self):
        def broken(_command_id):
            raise RuntimeError("journal unreadable")
        registry = Registry(previous=broken)
        result = registry.service.stop("manager", str(uuid4()))
        self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)
        registry.untouched(self)

    def test_a_service_without_a_classifier_has_no_previous_backend(self):
        registry = Registry(previous=None)
        self.assertEqual(registry.service.stop("manager", str(uuid4()))["status"], "not_found")

    def test_the_task_status_is_read_for_the_audit_record_only(self):
        for status, expected in (("closed", "closed"), ("running", "running"), (None, None)):
            with self.subTest(status=status):
                registry = Registry()
                command_id = registry.register("worker", task_id="t-1", task_status=lambda s=status: s)
                registry.service.stop("worker", command_id)
                (requested,) = registry.of("command_stop_requested")
                self.assertEqual((requested["task_id"], requested["task_status"]), ("t-1", expected))
        registry = Registry()
        command_id = registry.register("worker", task_status=lambda: "closed")  # no Task: nothing to say about one
        registry.service.stop("worker", command_id)
        (requested,) = registry.of("command_stop_requested")
        self.assertEqual((requested["task_id"], requested["task_status"]), (None, None))


# =============================================================================================================
# S2: a real Backend
# =============================================================================================================
class Cs05Fixture(RestartWorkerFixture):
    """A real Backend (fake OMPs, real host shell) whose Worker runs commands through the ``terminal`` tool."""

    def setUp(self):
        self.marks: list[str] = []
        self.sentinels: list[tuple[subprocess.Popen, int]] = []
        self.addCleanup(self._no_residue)  # registered first: runs last, after the backend was closed
        super().setUp()
        self.backend.command_stop.policy = OWN
        self.backend.terminal._wait_seconds = 1
        self.handoffs_path = self.backend.layout.workflow / "handoffs.jsonl"
        self.log_dir = Path(self._dir.name) / "signals"
        self.log_dir.mkdir()
        self.outside = self.sentinel(self.mark("sentinel-own-session"), new_session=True)
        self.sibling = self.sentinel(self.mark("sentinel-runner-session"), new_session=False)
        self.addCleanup(self._sentinels_survived)  # registered last: runs before the sentinels are killed

    # -- processes this test owns ------------------------------------------------------------------------
    def mark(self, label: str) -> str:
        mark = f"6{len(self.marks):02d}.{TAG}"
        self.marks.append(mark)
        self._labels = getattr(self, "_labels", {})
        self._labels[mark] = label
        return mark

    def sentinel(self, mark: str, *, new_session: bool):
        process = subprocess.Popen(["sleep", mark], start_new_session=new_session)
        ticks = stat_of(process.pid)[4]
        self.addCleanup(lambda: (kill_exact(process.pid, ticks), process.wait()))
        self.sentinels.append((process, ticks))
        return process

    def _sentinels_survived(self):
        for process, ticks in self.sentinels:
            self.assertTrue(alive(process.pid, ticks), f"sentinel {process.args} outside the commands was signalled")

    def _no_residue(self):
        left = [(mark, pid) for mark in self.marks for pid in pids_with(mark)
                if not any(process.pid == pid for process, _ in self.sentinels)]
        for _mark, pid in left:  # cleaned by exact identity even when the assertion fails
            found = stat_of(pid)
            if found is not None:
                kill_exact(pid, found[4])
        self.assertEqual(left, [], "a command of this test outlived it")

    # -- the Worker and Manager tool calls -----------------------------------------------------------------
    def worker_terminal(self, **args):
        return self.call(ActorRole.WORKER, "terminal", {"command": None, **args})[0]

    def manager_stop(self, command_id, reason="the user asked to stop it", **extra):
        return self.call(ActorRole.MANAGER, "stop_command", {"command_id": command_id, "reason": reason, **extra})[0]

    def run_command(self, command):
        started = self.worker_terminal(command=command)
        self.assertEqual(started["status"], "running", started)
        self.addCleanup(self.end_command, started["command_id"])
        return started

    def end_command(self, command_id):
        if self.backend.terminal.current().get("running"):
            self.backend.terminal._command_stop.stop("worker", command_id)

    def sleeping(self, label):
        mark = self.mark(label)
        return mark, self.run_command(f"sleep {mark}")

    def trapping(self, label, signals="INT TERM"):
        """A command that logs every signal it receives and keeps running; (mark, started, log path)."""
        mark = self.mark(label)
        log = self.log_dir / f"{mark}.log"
        fifo = self.log_dir / f"{mark}.fifo"  # a builtin `read -t` loop: the command forks nothing while it waits
        started = self.run_command(f": {mark}; mkfifo {fifo}; exec 9<>{fifo}; trap 'echo got >> {log}' {signals}; "
                                   "echo ready; while :; do read -t 0.2 -u 9 _; done")
        return mark, started, log

    def signals_seen(self, log: Path) -> int:
        return len(log.read_text().splitlines()) if log.exists() else 0

    def settle(self, seconds=0.7):
        """Tick the backend for a while (a trap handler runs between the loop's sleeps)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.backend._tick(0.02)

    def leader_of(self, mark):
        """(pid, stat) of the process group leader among the live processes carrying ``mark``."""
        for pid in pids_with(mark):
            found = stat_of(pid)
            if found is not None and found[2] == pid:
                return pid, found
        self.fail(f"no process group leader with mark {mark}")

    # -- records -------------------------------------------------------------------------------------------
    def records(self, *types):
        lines = self.handoffs_path.read_text().splitlines() if self.handoffs_path.exists() else []
        found = [json.loads(line) for line in lines if line.strip()]
        return [r for r in found if r.get("type") in types] if types else found

    def stop_records(self):
        return [r for r in self.records() if str(r.get("type", "")).startswith("command_stop")]

    def notices_of(self, kind, role="worker"):
        return [item["notice"] for item in self.received() if item["role"] == role and item["notice"]["type"] == kind]

    def open_free_task(self):
        result = self.call(ActorRole.MANAGER, "to_worker",
                           {"kind": "work", "message": "collect facts", "spec": {"goal": "g", "paths": []}})[0]
        self.assertEqual(result["status"], "dispatched", result)
        flow_task = self.backend.flow.tasks[result["task_id"]]
        self.wait(lambda: flow_task.status == "running", "the Task to run")
        return flow_task

    def cancel(self, task):
        result, _ = self.call(ActorRole.MANAGER, "to_worker", {"kind": "work", "message": "the user changed plans",
                                                              "cancel": True, "task_id": task.task_id})
        self.assertEqual(result.get("status"), "cancelled", result)
        return result

    def restart_worker(self):
        old = self.backend.bridge.peer(ActorRole.WORKER)
        result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "the worker lost its context"})
        self.assertEqual(result["status"], "restarted", result)
        self.wait(lambda: self.backend.bridge.peer(ActorRole.WORKER) is not None
                  and self.backend.bridge.peer(ActorRole.WORKER).session_id != old.session_id, "the new session")
        return old, self.backend.bridge.peer(ActorRole.WORKER)


class BackendAuthorizationTests(Cs05Fixture):
    """The matrix through the real tool entries, with real processes that must not notice a refusal."""

    def test_a_worker_cannot_stop_a_manager_command_and_the_manager_can(self):
        mark, mine, log = self.trapping("worker command next to the manager's")
        foreign = StopTarget(str(uuid4()), "manager", "experiment", None, identity=lambda: None)
        self.backend.command_stop.register(foreign)
        self.backend.command_stop.policy = StopPolicy(identity_wait=0.3, sigint_wait=0.2, sigterm_wait=0.2,
                                                      sigkill_wait=0.2, call_wait=5.0)
        refused = self.worker_terminal(stop=foreign.command_id)
        self.assertEqual((refused["status"], refused["reason"], refused["requested_by"], refused["command_id"]),
                         ("refused", "not_owner", "worker", foreign.command_id), refused)
        self.assertLessEqual(set(refused), {"status", "reason", "requested_by", "command_id", "detail"},
                             "a refusal carries no process, argv or state of the command")
        self.settle()
        self.assertEqual(self.signals_seen(log), 0, "the Worker's own command was not signalled by the refusal")
        self.assertEqual(self.backend.terminal.current().get("running"), True)
        self.assertEqual(self.records("command_stop_target", "command_stop_signal"), [])
        self.assertEqual(self.records("command_stop_requested"), [])
        (journaled,) = self.records("command_stop_refused")
        self.assertEqual((journaled["command_id"], journaled["requested_by"], journaled["status"],
                          journaled["reason"]), (foreign.command_id, "worker", "refused", "not_owner"))
        # the Manager is allowed (requested_by manager, owner manager, no approval); the fake owner has no live
        # identity, so the episode ends unknown without a signal
        result = self.manager_stop(foreign.command_id)
        self.assertEqual(result["requested_by"], "manager", result)
        self.assertNotEqual(result.get("reason"), "not_owner", result)
        self.assertEqual((result["status"], result["reason"]), ("unknown", "identity_unavailable"), result)
        (requested,) = self.records("command_stop_requested")
        self.assertEqual((requested["command_id"], requested["requested_by"], requested["owner"], requested["kind"]),
                         (foreign.command_id, "manager", "manager", "experiment"))
        self.assertEqual(self.records("command_stop_signal"), [], "no identity, no signal")
        self.assertEqual(self.signals_seen(log), 0)
        self.assertEqual(self.backend.handoffs.pending_approvals(), [])

    def test_ids_that_are_not_commands_are_never_targets(self):
        """Workbench, OMP and the host shell have no command_id; no id Workbench knows makes them targetable."""
        mark, started, log = self.trapping("a running command")
        leader_pid, (_, supervisor_pid, _, shell_pid, _) = self.leader_of(mark)
        protected = {os.getpid(): stat_of(os.getpid())[4], supervisor_pid: stat_of(supervisor_pid)[4],
                     shell_pid: stat_of(shell_pid)[4]}
        for entry in self.invocations():
            protected[entry["pid"]] = stat_of(entry["pid"])[4]
        peer = self.backend.bridge.peer(ActorRole.WORKER)
        task = self.open_free_task()
        ids = {"worker omp session": peer.session_id, "manager omp session": self.backend.bridge.peer(
            ActorRole.MANAGER).session_id, "task": task.task_id, "run": task.run_id,
            "task message": task.task_message_id, "random": str(uuid4())}
        ids = {label: value for label, value in ids.items()
               if isinstance(value, str) and len(value) == 36 and value == value.lower()}
        self.assertIn("task", ids)
        for requester, send in (("manager", self.manager_stop),
                                ("worker", lambda cid: self.worker_terminal(stop=cid))):
            for label, command_id in ids.items():
                with self.subTest(requester=requester, id=label):
                    result = send(command_id)
                    self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)
        for text in (str(os.getpid()), str(shell_pid), "$$", "-1", "0", "../etc", "bash", "omp"):
            with self.subTest(argument=text):
                result = self.call(ActorRole.MANAGER, "stop_command", {"command_id": text, "reason": "x"})[0]
                self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), result)
        self.settle()
        self.assertEqual(self.signals_seen(log), 0)
        self.assertEqual(self.records("command_stop_target", "command_stop_signal", "command_stop_requested"), [])
        for pid, ticks in protected.items():
            self.assertTrue(alive(pid, ticks), f"pid {pid} (backend/OMP/host shell/supervisor) was touched")

    def test_the_real_signals_reach_only_the_commands_own_group(self):
        """CS-AC-11 on a real tree: SIGINT, SIGTERM, SIGKILL, requester manager, no approval, targets in the group."""
        mark, started, log = self.trapping("a command that ignores INT and TERM")
        leader, (_, supervisor, group, shell, _) = self.leader_of(mark)
        before = {pid for pid in pids_with(mark)}
        self.assertEqual(before, {leader}, "the command is one bash process: the group has no other member")
        self.assertEqual(group, leader)
        result = self.manager_stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"], result["signal_name"]), (137, 9, "SIGKILL"))
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertEqual((result["requested_by"], result["command_id"]), ("manager", started["command_id"]))
        self.assertGreaterEqual(self.signals_seen(log), 2, "the command did receive INT and TERM")
        self.assertEqual(self.backend.handoffs.pending_approvals(), [], "no user approval was asked for")
        self.assertFalse([r for r in self.records() if "approval" in str(r.get("type"))])
        for pid in before:
            self.assertFalse(alive(pid), f"command process {pid} is still alive")
        targeted = {item["pid"] for record in self.records("command_stop_signal") for item in record["targets"]}
        self.assertTrue(targeted, "signals were journaled with their targets")
        self.assertEqual(targeted, {leader}, "only the command's own process was signalled")
        forbidden = {os.getpid(), shell, supervisor, *[entry["pid"] for entry in self.invocations()]}
        self.assertFalse(targeted & forbidden, "Workbench, OMP, the host shell or the supervisor was a target")
        self.assertTrue(alive(shell), "the host shell is not ended by a stop (CS-AC-19)")
        self.assertEqual(self.backend.terminal.current().get("running"), False)
        # nothing in the audit records names the command's text, comm or argv
        text = json.dumps(self.stop_records())
        self.assertNotIn(mark, text)
        self.assertNotIn("sleep", text)
        self.assertNotIn("trap", text)

    def test_the_worker_may_not_call_stop_command(self):
        mark, started, log = self.trapping("a command the Worker tries to stop through the Manager's tool")
        result = self.call(ActorRole.WORKER, "stop_command",
                           {"command_id": started["command_id"], "reason": "the worker tries"})[0]
        self.assertEqual((result["status"], result["reason"]), ("rejected", "tool_not_allowed_for_role"), result)
        self.assertNotIn("exit_code", result)
        self.settle()
        self.assertEqual(self.signals_seen(log), 0)
        self.assertEqual(self.stop_records(), [], "the stop service never saw the request")
        self.assertEqual(self.records("stop_command_request"), [], "the Manager-side handler never ran")
        self.assertEqual(self.backend.terminal.current().get("running"), True)
        # an invalid Worker call is also refused on the role, not on its arguments
        result = self.call(ActorRole.WORKER, "stop_command", {"nonsense": True})[0]
        self.assertEqual((result["status"], result["reason"]), ("rejected", "tool_not_allowed_for_role"), result)


class PreviousBackendWiringTests(Cs05Fixture):
    """CS-AC-13: a command the journal remembers but this backend does not hold is never signalled."""

    def as_a_new_backend(self):
        """The command stays running; the stop service that knows it is replaced by a fresh one (a new backend)."""
        old = self.backend.command_stop
        fresh = CommandStopService(journal=self.backend.handoffs.record, policy=OWN,
                                   previous_backend=self.backend._journaled_unfinished_command)
        self.backend.command_stop = fresh
        self.backend.terminal._command_stop = fresh

        def restore():
            self.backend.command_stop = old
            self.backend.terminal._command_stop = old
        self.addCleanup(restore)
        return fresh

    def test_a_running_command_this_backend_does_not_hold_is_refused_and_not_signalled(self):
        mark, started, log = self.trapping("a command the 'previous backend' started")
        leader, (_, _, group, _, ticks) = self.leader_of(mark)
        members = [(pid, stat_of(pid)[4]) for pid in pids_with(mark)]
        self.as_a_new_backend()
        for requester, send in (("worker", lambda: self.worker_terminal(stop=started["command_id"])),
                                ("manager", lambda: self.manager_stop(started["command_id"]))):
            result = send()
            self.assertEqual((result["status"], result["reason"], result["requested_by"]),
                             ("refused", "previous_backend_command", requester), result)
            self.assertIn("stop_survivor", result["detail"])
            self.assertNotIn("exit_code", result)
        self.settle(1.0)
        self.assertEqual(self.signals_seen(log), 0, "not even an ignored signal arrived")
        for pid, member_ticks in members:
            self.assertTrue(alive(pid, member_ticks))
        refused = [(r["requested_by"], r["status"], r["reason"]) for r in self.records("command_stop_refused")]
        self.assertEqual(refused, [("worker", "refused", "previous_backend_command"),
                                   ("manager", "refused", "previous_backend_command")])
        self.assertEqual(self.records("command_stop_requested", "command_stop_target", "command_stop_signal"), [])

    def test_a_journaled_command_that_ended_is_not_a_survivor(self):
        self.backend.terminal._wait_seconds = 20
        done = self.worker_terminal(command=f": {self.mark('short')}; echo done")
        self.assertEqual((done["status"], done["exit_code"]), ("exited", 0), done)
        self.as_a_new_backend()
        for send in (lambda: self.manager_stop(done["command_id"]),
                     lambda: self.worker_terminal(stop=done["command_id"])):
            result = send()
            self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)
        self.assertEqual(self.records("command_stop_requested", "command_stop_signal"), [])

    def test_a_started_record_with_no_end_for_another_id_does_not_make_an_unrelated_id_a_survivor(self):
        mark, started, log = self.trapping("some running command")
        self.as_a_new_backend()
        result = self.manager_stop(str(uuid4()))
        self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)
        self.settle()
        self.assertEqual(self.signals_seen(log), 0)


class StopCommandArgumentTests(Cs05Fixture):
    """SPEC "Manager: stop_command": strict {command_id, reason}; the errors name fields, never values."""

    def test_arguments_are_validated_strictly_and_values_are_not_echoed(self):
        mark, started, log = self.trapping("a command no bad call may touch")
        good = started["command_id"]
        secretish = "zz-value-that-must-not-come-back"
        cases = [
            (None, "arguments"), ([], "arguments"), ("not an object", "arguments"), (7, "arguments"),
            ({}, "command_id"), ({"command_id": good}, "reason"), ({"reason": "x"}, "command_id"),
            ({"command_id": good, "reason": ""}, "reason"), ({"command_id": good, "reason": "   \t\n"}, "reason"),
            ({"command_id": good, "reason": "　"}, "reason"), ({"command_id": good, "reason": None}, "reason"),
            ({"command_id": good, "reason": 5}, "reason"), ({"command_id": good, "reason": ["x"]}, "reason"),
            ({"command_id": good, "reason": f"a\x00b {secretish}"}, "reason"),
            ({"command_id": good, "reason": f"{secretish} " + "x" * 500}, "reason"),
            ({"command_id": good.upper(), "reason": "x"}, "command_id"),
            ({"command_id": good + "\n", "reason": "x"}, "command_id"),
            ({"command_id": " " + good, "reason": "x"}, "command_id"),
            ({"command_id": good.replace("-", ""), "reason": "x"}, "command_id"),
            ({"command_id": 12345, "reason": "x"}, "command_id"), ({"command_id": None, "reason": "x"}, "command_id"),
            ({"command_id": [good], "reason": "x"}, "command_id"),
            ({"command_id": good, "reason": "x", "force": True}, "force"),
            ({"command_id": good, "reason": "x", "approved": True}, "approved"),
            ({"command_id": good, "reason": "x", "signal": "SIGKILL"}, "signal"),
            ({"command_id": good, "reason": "x", "pid": os.getpid()}, "pid"),
            ({"command_id": good, "reason": "x", "stop": good}, "stop"),
            ({"command_id": good, "reason": "x", "command": f"kill -9 {secretish}"}, "command"),
        ]
        for args, field in cases:
            with self.subTest(args=repr(args)[:70]):
                result = self.call(ActorRole.MANAGER, "stop_command", args)[0]
                self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), result)
                self.assertTrue(any(error.startswith(field) for error in result["errors"]), result)
                self.assertNotIn(secretish, json.dumps(result), "a value sent came back")
                self.assertNotIn(good, json.dumps(result), "the command id sent came back")
        self.settle()
        self.assertEqual(self.signals_seen(log), 0, "no invalid call reached the command")
        self.assertEqual(self.stop_records(), [])
        self.assertNotIn(secretish, self.handoffs_path.read_text(), "no value of an invalid call is journaled")
        self.assertEqual(self.backend.terminal.current().get("running"), True)

    def test_the_reason_may_be_one_to_five_hundred_characters(self):
        one = self.trapping("reason of one character", signals="USR1")[1]
        self.assertEqual(self.manager_stop(one["command_id"], reason="x")["status"], "exited")
        longest = self.run_command(f": {self.mark('reason of 500')}; sleep 600")
        result = self.manager_stop(longest["command_id"], reason="y" * 500)
        self.assertEqual((result["status"], result["requested_by"]), ("exited", "manager"), result)
        (requested,) = [r for r in self.records("command_stop_requested") if r["command_id"] == longest["command_id"]]
        self.assertEqual(requested["reason"], "y" * 500)

    def test_a_reason_with_an_environment_value_is_refused_and_written_nowhere(self):
        token = "tok-0123456789-abcdefghij-independent"
        self.backend.environment = {**self.backend.environment, "WB_CS05_API_TOKEN": token}
        mark, started, log = self.trapping("a command the secret reason must not stop")
        for reason in (f"it printed {token}", f"  {token}  ", f"SECRET_KEY=hunter2-assigned {token[:5]}"):
            with self.subTest(reason=reason[:20]):
                result = self.manager_stop(started["command_id"], reason=reason)
                self.assertEqual((result["status"], result["reason"]), ("rejected", "environment_value"), result)
                self.assertNotIn(token, json.dumps(result))
                self.assertNotIn("hunter2-assigned", json.dumps(result))
        self.settle()
        self.assertEqual(self.signals_seen(log), 0)
        self.assertEqual(self.records("command_stop_requested", "command_stop_signal"), [])
        for path in Path(self._dir.name).rglob("*"):
            if path.is_file() and path.stat().st_size < 4_000_000:
                data = path.read_bytes()
                self.assertNotIn(token.encode(), data, f"the environment value was written to {path.name}")
                self.assertNotIn(b"hunter2-assigned", data, f"an assigned value was written to {path.name}")
        # the same command is then stopped with a clean reason
        self.assertEqual(self.manager_stop(started["command_id"], reason="clean")["status"], "exited")

    def test_a_resent_call_key_is_replayed_for_every_final_result_and_never_stops_twice(self):
        mark, started, log = self.trapping("a command stopped once", signals="USR1")
        args = {"command_id": started["command_id"], "reason": "hangs"}
        first = self.call(ActorRole.MANAGER, "stop_command", args, call="cs05-key-final")[0]
        self.assertEqual(first["status"], "exited", first)
        before = self.stop_records()
        again = self.call(ActorRole.MANAGER, "stop_command", args, call="cs05-key-final")[0]
        self.assertEqual(again, first, "the cached final result, not a repeat")
        self.assertNotIn("repeat", again)
        self.assertEqual(self.stop_records(), before, "a replay writes no record and signals nothing")
        unknown = {"command_id": str(uuid4()), "reason": "nobody"}
        missing = self.call(ActorRole.MANAGER, "stop_command", unknown, call="cs05-key-missing")[0]
        self.assertEqual(missing["status"], "not_found")
        again = self.call(ActorRole.MANAGER, "stop_command", unknown, call="cs05-key-missing")[0]
        self.assertEqual(again, missing)
        self.assertEqual(len([r for r in self.records("command_stop_refused")
                              if r["command_id"] == unknown["command_id"]]), 1)
        # a different key for the finished command is a repeat of the original, with no new signal
        other = self.call(ActorRole.MANAGER, "stop_command", args, call="cs05-key-other")[0]
        self.assertEqual((other["status"], other.get("repeat"), other["requested_by"]), ("exited", True, "manager"))
        self.assertEqual(len(self.records("command_stop_signal")), len(first["signals"]))

    def test_a_still_stopping_answer_is_not_the_final_result_of_the_call_key(self):
        mark, started, log = self.trapping("a command that outlasts the call wait", signals="INT TERM")
        self.backend.command_stop.policy = StopPolicy(identity_wait=2.0, sigint_wait=0.8, sigterm_wait=0.8,
                                                      sigkill_wait=1.0, call_wait=0.3)
        args = {"command_id": started["command_id"], "reason": "hangs"}
        interim = self.call(ActorRole.MANAGER, "stop_command", args, call="cs05-key-interim")[0]
        self.assertEqual(interim["status"], "stopping", interim)
        self.backend.command_stop.policy = OWN
        final = self.call(ActorRole.MANAGER, "stop_command", args, call="cs05-key-interim")[0]
        self.assertEqual((final["status"], final["exit_code"]), ("exited", 137), final)
        self.assertIs(final.get("repeat"), True, "it joined the one running episode")
        self.assertEqual(len(self.records("command_stop_requested")), 1)
        self.assertEqual([r["signal"] for r in self.records("command_stop_signal")], ["SIGINT", "SIGTERM", "SIGKILL"])


class StoppedByMarkerTests(Cs05Fixture):
    """The tech supplement: a command a stop ended says who stopped it, on every surface the Worker reads."""

    def surfaces(self, task, command_id):
        fetched = self.worker_terminal()
        (ended,) = [r for r in self.records("terminal_ended") if r["command_id"] == command_id]
        (run,) = [r for r in self.backend.terminal.runs_for_task(task.task_id) if command_id in str(r.get("log_path"))]
        return fetched, ended, run

    def test_manager_stopped_command_is_marked_on_result_notice_journal_and_commands_run(self):
        task = self.open_free_task()
        mark, started, log = self.trapping("a command the Manager stops after INT is trapped", signals="INT")
        stopped = self.manager_stop(started["command_id"])
        self.assertEqual(stopped["status"], "exited", stopped)
        self.assertEqual((stopped["exit_code"], stopped["signal_name"]), (143, "SIGTERM"), stopped)
        self.wait(lambda: self.notices_of("terminal_done"), "the terminal_done notice")
        (done,) = self.notices_of("terminal_done")
        fetched, ended, run = self.surfaces(task, started["command_id"])
        for where, item in (("terminal result", fetched), ("terminal_done", done), ("terminal_ended", ended),
                            ("commands_run", run)):
            self.assertEqual((item.get("stopped_by"), item.get("stop_signal")), ("manager", "SIGTERM"), where)
            if "command_id" in item:
                self.assertEqual(item["command_id"], started["command_id"], where)
        self.assertEqual(fetched["exit_code"], 143)
        self.assertEqual(self.worker_terminal()["stopped_by"], "manager", "stable on a second look")

    def test_a_worker_stop_marks_worker_and_a_command_that_ended_alone_has_no_marker(self):
        task = self.open_free_task()
        mark, mine = self.sleeping("a command the Worker stops")
        stopped = self.worker_terminal(stop=mine["command_id"])
        self.assertEqual((stopped["status"], stopped["requested_by"], stopped["exit_code"]), ("exited", "worker", 130))
        fetched, ended, run = self.surfaces(task, mine["command_id"])
        for where, item in (("result", fetched), ("journal", ended), ("commands_run", run)):
            self.assertEqual((item.get("stopped_by"), item.get("stop_signal")), ("worker", "SIGINT"), where)
        self.backend.terminal._wait_seconds = 20
        alone = self.worker_terminal(command=f": {self.mark('ends alone')}; echo fine")
        self.assertEqual((alone["status"], alone["exit_code"]), ("exited", 0), alone)
        fetched, ended, run = self.surfaces(task, alone["command_id"])
        for where, item in (("result", alone), ("fetched", fetched), ("journal", ended), ("commands_run", run)):
            self.assertNotIn("stopped_by", item, where)
            self.assertNotIn("stop_signal", item, where)

    def test_a_stop_that_arrives_after_the_command_ended_marks_nothing(self):
        task = self.open_free_task()
        self.backend.terminal._wait_seconds = 20
        alone = self.worker_terminal(command=f": {self.mark('ended before the stop')}; exit 3")
        self.assertEqual((alone["status"], alone["exit_code"]), ("exited", 3), alone)
        for send in (lambda: self.manager_stop(alone["command_id"]),
                     lambda: self.worker_terminal(stop=alone["command_id"])):
            late = send()
            self.assertEqual((late["status"], late["exit_code"]), ("already_exited", 3), late)
            self.assertEqual(late["signals"], [])
            self.assertNotIn("stopped_by", late)
        fetched, ended, run = self.surfaces(task, alone["command_id"])
        for where, item in (("fetched", fetched), ("journal", ended), ("commands_run", run)):
            self.assertNotIn("stopped_by", item, where)
            self.assertNotIn("stop_signal", item, where)
        self.assertEqual(self.records("command_stop_signal", "command_stop_target"), [])

    def test_a_refused_stop_marks_nothing(self):
        task = self.open_free_task()
        mark, mine, log = self.trapping("a command whose stop was refused", signals="USR1")
        foreign = StopTarget(str(uuid4()), "manager", "experiment", None, identity=lambda: None)
        self.backend.command_stop.register(foreign)
        self.assertEqual(self.worker_terminal(stop=foreign.command_id)["reason"], "not_owner")
        self.assertEqual(self.manager_stop(str(uuid4()))["status"], "not_found")
        self.assertEqual(self.manager_stop(mine["command_id"])["status"], "exited")
        fetched = self.worker_terminal()
        self.assertEqual(fetched["stopped_by"], "manager", "only the real stop marked the real command")


class RestartedWorkerTests(Cs05Fixture):
    """CS-AC-14 / CS-D08: ownership is the Worker role, not the OMP session that started the command."""

    def test_a_third_worker_session_stops_a_command_the_first_session_started(self):
        mark, started, log = self.trapping("a command that outlives two restarts", signals="USR1")
        (start,) = [r for r in self.records("terminal_started") if r["command_id"] == started["command_id"]]
        first = self.backend.bridge.peer(ActorRole.WORKER)
        self.assertEqual(start["key"]["session_id"], first.session_id)
        _, second = self.restart_worker()
        _, third = self.restart_worker()
        self.assertEqual(len({first.session_id, second.session_id, third.session_id}), 3)
        group = self.leader_of(mark)[0]
        self.assertTrue(alive(group), "the command kept running across both restarts")
        result = self.worker_terminal(stop=started["command_id"])
        self.assertEqual((result["status"], result["exit_code"], result["requested_by"]), ("exited", 130, "worker"))
        self.assertFalse(alive(group))
        (requested,) = self.records("command_stop_requested")
        self.assertEqual((requested["command_id"], requested["owner"], requested["requested_by"]),
                         (started["command_id"], "worker", "worker"))
        calls = [r for r in self.records("terminal_request") if r.get("stop") == started["command_id"]]
        self.assertEqual([r["key"]["session_id"] for r in calls], [third.session_id])
        self.assertEqual(self.records("command_stop_refused"), [])
        self.assertNotEqual(calls[0]["key"]["session_id"], start["key"]["session_id"])

    def test_the_manager_stops_it_after_a_restart_and_is_told_who_stopped_it(self):
        mark, started = self.sleeping("a command the Manager stops after a restart")
        self.restart_worker()
        result = self.manager_stop(started["command_id"])
        self.assertEqual((result["status"], result["exit_code"], result["requested_by"]), ("exited", 130, "manager"))
        (requested,) = self.records("command_stop_requested")
        self.assertEqual(requested["owner"], "worker", "the owner is a role")
        self.wait(lambda: self.notices_of("worker_terminal_done", "manager") or self.notices_of("terminal_done"),
                  "a completion notice")
        notices = self.notices_of("worker_terminal_done", "manager") + self.notices_of("terminal_done")
        for notice in notices:
            self.assertEqual((notice["command_id"], notice.get("stopped_by"), notice.get("stop_signal")),
                             (started["command_id"], "manager", "SIGINT"))

    def test_a_restart_leaves_the_stop_authority_of_a_second_command_alone(self):
        """Commands of different sessions are all the Worker's: the new session stops the second one too."""
        _, first = self.sleeping("first command")
        self.restart_worker()
        self.assertEqual(self.worker_terminal(stop=first["command_id"])["status"], "exited")
        mark, second = self.sleeping("second command, started by the new session")
        self.restart_worker()
        self.assertEqual(self.manager_stop(second["command_id"])["status"], "exited")
        self.assertEqual(len(self.records("command_stop_requested")), 2)
        self.assertEqual(self.records("command_stop_refused"), [])


class ClosedTaskTests(Cs05Fixture):
    """CS-AC-15 / CS-D09: stopping a cancelled Task's command does not reopen the Task or give it new powers."""

    def first_command_after_cancel(self, task, command, mark=None):
        """The Worker's first task-less command after the cancel, as a real Worker sends it.

        CS-O01 (H-05): while the cancel notice has not reached the Worker, that first command is refused once
        (``task_cancelled``) and nothing is typed; the Worker then sends it again and it runs. When the notice was
        already submitted it runs at once. Either way the result is the command's own."""
        started_before = len(self.records("terminal_started"))
        result = self.worker_terminal(command=command)
        if (result.get("status"), result.get("reason")) == ("rejected", "task_cancelled"):
            self.assertEqual(result["task_id"], task.task_id, result)
            self.assertIn(task.task_id, result["detail"])
            self.assertIn("not run", result["detail"])
            self.assertEqual(len(self.records("terminal_started")), started_before, "the refusal typed nothing")
            self.assertFalse(self.backend.terminal.current().get("running"), "nothing runs after the refusal")
            if mark is not None:
                self.assertEqual(pids_with(mark), [], "the refused command never ran")
            result = self.worker_terminal(command=command)  # the Worker sends it again
        self.assertNotEqual(result.get("reason"), "task_cancelled", "one refusal per cancel")
        return result

    def test_the_manager_and_the_worker_stop_commands_of_a_cancelled_task(self):
        task = self.open_free_task()
        mark, started, log = self.trapping("a command that survives its Task's cancel", signals="USR1")
        cancelled = self.cancel(task)
        self.assertEqual([item["command_id"] for item in cancelled["host_commands"]], [started["command_id"]])
        self.assertEqual(self.backend.flow.tasks[task.task_id].status, "closed")
        notices_before = len(self.received())
        stopped = self.manager_stop(started["command_id"])
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["requested_by"]), ("exited", 130, "manager"))
        flow_task = self.backend.flow.tasks[task.task_id]
        self.assertEqual(flow_task.status, "closed", "the stop does not reopen the Task")
        self.assertIsNone(self.backend.flow.active(), "no Task became active")
        (requested,) = self.records("command_stop_requested")
        self.assertEqual((requested["command_id"], requested["requested_by"], requested["task_id"],
                          requested["task_status"], requested["owner"]),
                         (started["command_id"], "manager", task.task_id, "closed", "worker"))
        self.assertEqual(self.backend.flow.status_view(task.task_id).get("host_commands"), [], "nothing is running")
        # whatever the Worker is told afterwards, it is nothing that resumes work
        self.settle(2.5)
        later = [item["notice"]["type"] for item in self.received()[notices_before:] if item["role"] == "worker"]
        self.assertLessEqual(set(later), {"terminal_done", "terminal_check"}, later)
        # a second command of the closed Task's era, stopped by the Worker, gets the same audit fields
        second_mark = self.mark("second")
        second = self.first_command_after_cancel(task, f": {second_mark}; sleep 600", mark=second_mark)
        self.assertEqual(second["status"], "running", second)
        self.addCleanup(self.end_command, second["command_id"])
        # (the Worker's command now belongs to no Task: the cancelled one lost its claim on new commands)
        result = self.worker_terminal(stop=second["command_id"])
        self.assertEqual((result["status"], result["requested_by"]), ("exited", "worker"), result)
        starts = {r["command_id"]: r for r in self.records("terminal_started")}
        self.assertIsNone(starts[second["command_id"]]["task_id"], "a new command is not attributed to a closed Task")
        self.assertEqual(self.backend.flow.tasks[task.task_id].status, "closed")
        follow_up = self.call(ActorRole.MANAGER, "to_worker",
                              {"kind": "work", "message": "continue", "task_id": task.task_id})[0]
        self.assertEqual(follow_up["reason"], "task_closed", "the closed Task takes no new work after the stop")

    def later_command_after_cancel(self, how):
        """Cancel a Task whose command ended alone or was stopped, then run a new command; (result, task_id)."""
        task = self.open_free_task()
        if how == "alone":
            self.backend.terminal._wait_seconds = 20
            ended = self.worker_terminal(command=f": {self.mark('ends alone')}; echo first")
            self.assertEqual((ended["status"], ended["exit_code"]), ("exited", 0))
            self.cancel(task)
        else:
            running = self.run_command(f": {self.mark('to be stopped')}; sleep 600")
            self.cancel(task)
            self.assertEqual(self.worker_terminal(stop=running["command_id"])["exit_code"], 130)
        self.backend.terminal._wait_seconds = 20
        later_mark = self.mark("later")
        later = self.first_command_after_cancel(task, f": {later_mark}; echo later", mark=later_mark)
        (start,) = [r for r in self.records("terminal_started") if r["command_id"] == later["command_id"]]
        self.assertEqual(self.backend.flow.tasks[task.task_id].status, "closed")
        return later, start["task_id"]

    def test_new_command_rules_after_a_command_ended_alone(self):
        later, task_id = self.later_command_after_cancel("alone")
        self.assertEqual((later["status"], later["exit_code"], task_id), ("exited", 0, None))

    def test_new_command_rules_after_a_stop_are_the_same(self):
        later, task_id = self.later_command_after_cancel("stopped")
        self.assertEqual((later["status"], later["exit_code"], task_id), ("exited", 0, None))

    def test_a_stop_is_not_a_command_for_a_task_that_lists_its_commands(self):
        """The Task's exact-commands rule applies to new commands, never to a stop of an older one."""
        mark, started, log = self.trapping("a command started before a restricted Task was active",
                                           signals="USR1")
        restricted = self.open_task()  # commands = ["lscpu"], busy
        self.assertEqual(self.backend.flow.active_commands(), ["lscpu"])
        refused = self.worker_terminal(command=f"sleep {self.mark('not listed')}")
        self.assertEqual(refused["reason"], "not_in_task_commands", refused)
        both = self.worker_terminal(command="lscpu", stop=started["command_id"])
        self.assertEqual((both["status"], both["reason"]), ("rejected", "invalid_arguments"), both)
        self.settle()
        self.assertEqual(self.backend.terminal.current().get("running"), True, "an invalid stop signalled nothing")
        stopped = self.worker_terminal(stop=started["command_id"])
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["requested_by"]),
                         ("exited", 130, "worker"), stopped)
        self.assertEqual(self.backend.flow.active_commands(), ["lscpu"], "the restricted Task is unchanged")
        again = self.worker_terminal(command=f"sleep {self.mark('still not listed')}")
        self.assertEqual(again["reason"], "not_in_task_commands", "new commands are still restricted")
        (requested,) = self.records("command_stop_requested")
        self.assertIsNone(requested["task_id"], "the command was started with no Task")
        self.assertEqual(restricted.status, "running")


class PausedAndHeldStopTests(Cs05Fixture):
    """CS-AC-22 / CS-D13: stop is processed in both holds; new work and automatic delivery stay held."""

    def new_command_is(self, status):
        marker = Path(self._dir.name) / f"never-{uuid4().hex[:8]}"
        refused = self.worker_terminal(command=f"touch {marker}")
        self.assertEqual(refused["status"], status, refused)
        self.assertFalse(marker.exists(), "a held command never ran")

    def new_task_is_held(self):
        held = self.call(ActorRole.MANAGER, "to_worker", {"kind": "work", "message": "more",
                                                         "spec": {"goal": "g", "paths": []}})[0]
        self.assertEqual(held["status"], "held", held)

    def test_stop_during_a_pause_ends_the_command_but_keeps_the_pause_and_withholds_the_notice(self):
        # (no Task: with an open Task the explicit resume needs a full reconciliation the fixture does not offer)
        mark, started, log = self.trapping("a command stopped while paused", signals="USR1")
        self.backend.pause()
        self.assertTrue(self.backend._automation_paused())
        stopped = self.manager_stop(started["command_id"])
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["requested_by"]), ("exited", 130, "manager"))
        self.assertFalse(pids_with(mark), "the command is gone")
        self.settle(2.5)
        self.assertEqual(self.notices_of("terminal_done"), [], "the automatic delivery stays paused")
        self.assertTrue(self.backend._automation_paused(), "the stop did not resume automation")
        self.new_command_is("paused")
        self.new_task_is_held()
        self.backend.resume(True)
        self.wait(lambda: self.notices_of("terminal_done"), "the held notice after the resume", timeout=30)
        (done,) = self.notices_of("terminal_done")
        self.assertEqual((done["command_id"], done["stopped_by"]), (started["command_id"], "manager"))

    def test_the_worker_stops_during_a_pause_and_a_boot_hold_together(self):
        mark, started, log = self.trapping("a command stopped under pause and hold", signals="USR1")
        self.backend.pause()
        self.backend.hold.set(BOOT_HOLD, detail="boot not confirmed")
        self.assertEqual(self.backend._hold_reason(ActorRole.WORKER), BOOT_HOLD)
        stopped = self.worker_terminal(stop=started["command_id"])
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["requested_by"]), ("exited", 130, "worker"))
        # a refusal and a not_found are answered as themselves, not as "paused"/"held"
        self.assertEqual(self.manager_stop(str(uuid4()))["status"], "not_found")
        foreign = StopTarget(str(uuid4()), "manager", "experiment", None, identity=lambda: None)
        self.backend.command_stop.register(foreign)
        self.assertEqual(self.worker_terminal(stop=foreign.command_id)["reason"], "not_owner")
        self.new_command_is("paused")
        self.new_task_is_held()
        self.assertTrue(self.backend._automation_paused())
        self.assertEqual(self.backend._hold_reason(ActorRole.WORKER), BOOT_HOLD, "the stop did not clear the hold")

    def test_the_manager_stops_during_the_boot_hold_and_the_hold_stays(self):
        mark, started = self.sleeping("a command stopped under the boot hold")
        self.backend.hold.set(BOOT_HOLD, detail="boot not confirmed")
        stopped = self.manager_stop(started["command_id"])
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["requested_by"]), ("exited", 130, "manager"))
        self.new_command_is("held")
        self.new_task_is_held()
        self.assertEqual(self.backend._hold_reason(ActorRole.WORKER), BOOT_HOLD)
        self.assertFalse(self.backend._automation_paused())


class OneEpisodeUnderRaceTests(Cs05Fixture):
    """CS-D11 / SPEC "episode": a Worker and a Manager stopping at the same instant cause one SIGINT."""

    def test_concurrent_requests_from_both_roles_make_one_episode(self):
        mark = self.mark("slow to answer SIGINT")
        started = self.run_command(f": {mark}; trap 'sleep 1; exit 130' INT; echo ready; "
                                   "while :; do sleep 0.2; done")
        worker_peer = self.backend.bridge.peer(ActorRole.WORKER)
        manager_peer = self.backend.bridge.peer(ActorRole.MANAGER)
        barrier, boxes = threading.Barrier(2), {}

        def request(role, peer, tool, args):
            frame = {"request_id": str(uuid4()), "tool_call_id": f"race-{role}-{uuid4()}", "tool": tool, "args": args,
                     "session_id": peer.session_id, "generation": peer.generation}
            barrier.wait(30)
            boxes[role] = self.backend._tool_request(peer, frame)
        threads = [threading.Thread(target=request, daemon=True, args=(
            "worker", worker_peer, "terminal", {"command": None, "stop": started["command_id"]})),
            threading.Thread(target=request, daemon=True, args=(
                "manager", manager_peer, "stop_command", {"command_id": started["command_id"], "reason": "race"}))]
        for thread in threads:
            thread.start()
        self.addCleanup(lambda: [thread.join(30) for thread in threads])
        self.wait(lambda: len(boxes) == 2, "both stop results", timeout=30)
        results = list(boxes.values())
        self.assertEqual([r["status"] for r in results], ["exited", "exited"], results)
        self.assertEqual({r["exit_code"] for r in results}, {130})
        self.assertEqual(len({r["requested_by"] for r in results}), 1, "both read the one episode's requester")
        self.assertEqual(sum(1 for r in results if r.get("repeat")), 1, "exactly one is the original")
        self.assertEqual(len(self.records("command_stop_requested")), 1)
        self.assertEqual([r["signal"] for r in self.records("command_stop_signal")], ["SIGINT"])
        (finished,) = self.records("command_stop_finished")
        self.assertEqual((finished["status"], finished["repeat_requests"]), ("exited", 1))
        self.assertEqual(self.worker_terminal()["stopped_by"], results[0]["requested_by"])


if __name__ == "__main__":
    unittest.main()
