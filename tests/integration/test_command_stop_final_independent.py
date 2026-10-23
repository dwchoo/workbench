"""CS-10 final independent verification of the command-stop feature: scenarios that cross ticket boundaries.

Derived from the contracts (docs/features/command-stop/BRIEF.md CS-D01..17 / CS-AC-01..23, SPEC.md, tickets/CS-10.md),
not from the implementation. Real ``Backend`` (fake OMPs, real host-shell PTY, real bridge-frame entry
``Backend._tool_request``) for the Worker/terminal scenarios; the ``TaskFlow`` + real ``ShellPane`` rig of
``tests/backend/test_experiment_command_stop.py`` for the experiment scenarios (a Backend with fake OMPs cannot run
an experiment itself: its start waits for a real Worker). The production policy (10/5/5 s) is the other CS-10
module's job; every stop here runs a reduced ``StopPolicy``.

Expected values are this file's own: the BRIEF acceptance examples, exit codes 128+n of the commands the test starts,
pids / process groups / start ticks that the test reads itself from ``/proc`` or ``tcgetpgrp``, and the process
tree the test builds. A decoy process with the *same argv* as the stopped command, a sentinel process and a
"previous backend" process outside the tree are started by the tests; none may ever be signalled (CS-AC-19, and the
SPEC invariant "argv is not an identity").

Scenario -> acceptance map (each test's docstring repeats its own):

* FreeWorkCancelStopTests          CS-AC-01 02 03 10 15 19 20
* WorkerRestartManagerStopTests    CS-AC-03 11 14 20
* RefusalTests                     CS-AC-06 07 13 17 19
* UnknownAndCorrectionTests        CS-AC-10 18 23
* LeftSessionTests                 CS-AC-05 23
* PausedAndHeldStopTests           CS-AC-22
* UserCtrlCTests                   CS-AC-16 17
* ExperimentFinalTests             CS-AC-02 06 12 16 17 19 20 21 23

Every Backend test also checks the safety invariant (CS-AC-19) on teardown: nothing outside the host shell's
session was signalled, the sentinels / Workbench / OMPs / host shell survive.

Gate: ``WB_FINAL_COMMAND_STOP=1`` (see ``GATE``); the module runs a few minutes of real time and real processes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from workbench.backend.command_stop import LinuxStopEnvironment, StopPolicy  # noqa: E402
from workbench.app.recovery import BOOT_HOLD  # noqa: E402
from workbench.contracts.ui_v1 import PaneId, Reason  # noqa: E402
from workbench.contracts.v1 import ActorRole  # noqa: E402

import test_command_stop_authority as authority  # noqa: E402
import test_experiment_command_stop as experiment  # noqa: E402
from test_cd70_restart_worker import alive  # noqa: E402
from command_stop_support import alive as is_live, stat_of  # noqa: E402  (live pid with optional start ticks; /proc tuple or None)
from test_task_flow import wait_until  # noqa: E402

GATE = "WB_FINAL_COMMAND_STOP"
gated = unittest.skipUnless(os.environ.get(GATE) == "1",
                            f"final command-stop scenarios are opt-in (set {GATE}=1): minutes of real processes")

FORBIDDEN = ("stopped", "terminated", "killed", "exited", "finished")
# a command that answers SIGINT after one second: the stop episode is observable while it runs
SLOW_INT = "trap 'sleep 1; exit 130' INT; echo ready-{tag}; while :; do sleep 0.2; done"
BANNED_AUDIT_KEYS = {"argv", "cmdline", "comm", "env", "environment", "command"}


# ----------------------------------------------------------------------------------------------- /proc helpers
def proc_info(pid):
    """(state, ppid, pgrp, session, start_ticks) read by the test itself; None when the process is gone."""
    found = stat_of(pid)
    if found is None:
        return None
    state, ppid, pgrp, session, start_ticks = found
    return {"state": state, "ppid": ppid, "pgrp": pgrp, "session": session, "start_ticks": start_ticks}


def kill_exact(pid, start_ticks):
    """SIGKILL ``pid`` only while it is still the process with ``start_ticks`` (pidfd re-check): test cleanup."""
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if is_live(pid, start_ticks):
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(fd)


class AuditedEnvironment(LinuxStopEnvironment):
    """The real environment that records every pidfd it is asked to open and every signal it delivers.

    ``shell_pid`` is the host shell's pid: every delivered signal's target must live in that session and must not be
    in ``protected`` (Workbench, the OMPs, the host shell itself, the supervisor). ``deny`` makes the kernel refuse
    every signal (EPERM), the S7 way to get an ``unknown`` stop.
    """

    def __init__(self, *, deny=False, shell_pid=None):
        self.deny, self.shell_pid = deny, shell_pid
        self.lock = threading.Lock()
        self.fd_pid, self.attempts, self.sent, self.violations = {}, [], [], []
        self.protected = {os.getpid()}  # never a signal target
        self.must_live = {os.getpid()}  # and still alive at the end (the per-run supervisor is not in this set)

    def pidfd_open(self, pid):
        fd = super().pidfd_open(pid)
        with self.lock:
            self.fd_pid[fd] = pid
        return fd

    def pidfd_send_signal(self, fd, signum):
        with self.lock:
            pid = self.fd_pid.get(fd)
            self.attempts.append((pid, signum))
        if self.deny:
            raise PermissionError(1, "Operation not permitted")
        info = proc_info(pid) if pid else None
        if pid in self.protected or (self.shell_pid and info and info["session"] != self.shell_pid):
            self.violations.append({"pid": pid, "signal": signum, "info": info})
        super().pidfd_send_signal(fd, signum)
        with self.lock:
            self.sent.append((pid, signum))

    def signalled_pids(self):
        with self.lock:
            return {pid for pid, _ in self.sent}


# ----------------------------------------------------------------------------------------------- audit helpers
def stop_records(records):
    return [r for r in records if str(r.get("type", "")).startswith(("command_stop_", "command_interrupt"))]


class AuditChecks:
    """Mixed into the fixtures: CS-AC-20 record checks and the no-secret scan."""

    def records_of(self, *types):
        raise NotImplementedError

    def assert_audit_clean(self, records, *forbidden_text):
        """No argv/comm/env/command key in a stop or interrupt record, and none of the given text anywhere."""
        for record in stop_records(records):
            self.assertFalse(BANNED_AUDIT_KEYS & set(record), record)
            text = json.dumps(record)
            for item in forbidden_text:
                self.assertNotIn(item, text, record)

    def assert_episode_audit(self, records, command_id, *, requested_by, owner, kind, status, exit_code=None,
                             signals=(), task_id=None, task_status=None, target=None):
        """Requester, target (command_id, verified pid and start ticks), signals with times, end state, residual."""
        mine = [r for r in stop_records(records) if r.get("command_id") == command_id]
        by_type = lambda kind_: [r for r in mine if r["type"] == kind_]  # noqa: E731
        (requested,) = by_type("command_stop_requested")
        self.assertEqual((requested["requested_by"], requested["owner"], requested["kind"], requested["task_id"],
                          requested["task_status"]), (requested_by, owner, kind, task_id, task_status))
        (target_record,) = by_type("command_stop_target")
        if target:
            for key, value in target.items():
                self.assertEqual(target_record[key], value, (key, target_record))
        self.assertIsInstance(target_record["pid"], int)
        self.assertIsInstance(target_record["start_ticks"], int)
        self.assertEqual(target_record["uid"], os.getuid())
        sent = [r for r in by_type("command_stop_signal") if not r.get("auxiliary")]
        self.assertEqual([r["signal"] for r in sent], list(signals), sent)
        for record in sent:
            self.assertTrue(record["at"] and record["targets"], record)
        (finished,) = by_type("command_stop_finished")
        self.assertEqual((finished["status"], finished.get("exit_code")), (status, exit_code), finished)
        self.assertIs(finished["residual"]["checked"], True, finished)
        self.assertIn("unchecked", finished["residual"], "the residual check never claims 'clean'")
        self.assertIsInstance(finished["elapsed_seconds"], (int, float))
        return target_record


# ----------------------------------------------------------------------------------------------- Backend fixture
class FinalBackend(AuditChecks, authority.BackendStopFixture):
    """A real Backend whose stop engine records every delivered signal, with processes outside the tree."""

    deny = False

    def setUp(self):
        super().setUp()
        self.shell = self.backend.shell
        self.env = AuditedEnvironment(deny=self.deny, shell_pid=self.shell.shell.parent_pid)
        self.backend.command_stop.env = self.env
        self.protect_omps()
        self.env.protected.add(self.shell.shell.parent_pid)
        self.env.must_live.add(self.shell.shell.parent_pid)
        self.spawned = []
        self.addCleanup(self.kill_spawned)  # registered first, so it runs after assert_safety
        self.sentinel = self.spawn("sleep", "620.00")
        self.addCleanup(self.assert_safety)  # runs before the backend closes and before the spawned are killed

    # -- processes the test owns ---------------------------------------------------------------------------
    def spawn(self, *argv):
        """A process outside the host shell's session (own session); killed by exact identity on cleanup."""
        process = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ticks = proc_info(process.pid)["start_ticks"]
        self.spawned.append((process, ticks))
        return process

    def kill_spawned(self):
        for process, ticks in self.spawned:
            kill_exact(process.pid, ticks)
            process.wait()

    def protect_omps(self):
        for entry in self.invocations():
            self.env.protected.add(entry["pid"])  # (an old Worker OMP is gone after a restart: not must_live)

    def command_group(self):
        """The command's process group, read the way a terminal does: the PTY's foreground group."""
        group = os.tcgetpgrp(self.shell._master_fd)
        info = proc_info(group)
        self.assertIsNotNone(info, group)
        return group, info["start_ticks"]

    def run_command(self, command):
        started = super().run_command(command)
        try:
            identity = self.shell.shell.run_identity()
        except Exception:
            identity = None
        for key in ("supervisor", "parent"):
            pid = ((identity or {}).get(key) or {}).get("pid")
            if pid:
                self.env.protected.add(pid)
        return started

    def assert_safety(self):
        """CS-AC-19: nothing outside the command's session was signalled; Workbench, the OMPs and the shell live."""
        self.assertEqual(self.env.violations, [], "a signal reached a protected process or left the shell's session")
        signalled = self.env.signalled_pids()
        for process, ticks in self.spawned:
            self.assertNotIn(process.pid, signalled)
            self.assertTrue(is_live(process.pid, ticks), f"process {process.pid} started outside the tree was hit")
        current = {self.backend.panes[PaneId.WORKER_OMP].pid, self.backend.panes[PaneId.MANAGER_OMP].pid}
        for pid in self.env.must_live | current:
            self.assertTrue(alive(pid), f"process {pid} (Workbench, an OMP or the host shell) is gone")
        self.assertTrue(self.shell.alive(), "the host shell survived")

    # -- calls ------------------------------------------------------------------------------------------------
    def records_of(self, *types):
        return self.records(*types)

    def start_call(self, role, tool, args):
        """A tool call on another thread (so the test can observe the call while it waits); ``finish`` joins it."""
        peer = self.backend.bridge.peer(role)
        request = {"request_id": str(uuid4()), "tool_call_id": f"call-{uuid4()}", "tool": tool, "args": args,
                   "session_id": peer.session_id, "generation": peer.generation}
        box = {}
        thread = threading.Thread(target=lambda: box.update(result=self.backend._tool_request(peer, request)),
                                  daemon=True)
        thread.start()
        self.addCleanup(thread.join, 15)
        return box

    def finish(self, box, what):
        self.wait(lambda: "result" in box, what, timeout=20)
        return box["result"]

    def worker_stop_async(self, command_id):
        return self.start_call(ActorRole.WORKER, "terminal", {"command": None, "stop": command_id})

    def snapshot_commands(self):
        return self.backend.snapshot()["task"]["host_commands"]

    def wait_stopping(self, command_id):
        """The stop episode is under way (the engine's own surface; no Task is needed for it)."""
        self.wait(lambda: (self.backend.command_stop.surface(command_id) or {}).get("state") == "stopping",
                  "the command to read as stopping")

    def cancel(self, task):
        result, _ = self.call(ActorRole.MANAGER, "to_worker", {"kind": "work", "message": "the user changed plans",
                                                              "cancel": True, "task_id": task.task_id})
        self.assertEqual(result.get("status"), "cancelled", result)
        return result

    def run_entry(self, task_id, command_id):
        """The Task's commands_run entry of the command (found by its log, which is named after the command id)."""
        (entry,) = [e for e in self.backend.terminal.runs_for_task(task_id)
                    if str(e.get("log_path", "")).endswith(f"{command_id}.log")]
        with self.subTest("commands_run carries the command_id (SPEC s1 'Identifiers and ownership')"):
            self.assertEqual(entry.get("command_id"), command_id, entry)
        return entry

    def terminal_done(self, command_id):
        """The one completion notice of a command: to the Worker (`terminal_done`) or, while its Task is not
        delivered to the current Worker session, to the Manager (`worker_terminal_done`, C-D70)."""
        return [item["notice"] for item in self.received()
                if item["notice"]["type"] in ("terminal_done", "worker_terminal_done")
                and item["notice"].get("command_id") == command_id]


# ----------------------------------------------------------------------------------------------- scenarios
@gated
class FreeWorkCancelStopTests(FinalBackend):
    def test_cancel_shows_the_command_then_a_worker_stop_ends_it_and_the_task_stays_closed(self):
        """CS-AC-01, 02, 03, 10, 15, 19, 20: cancel -> running command + note -> Worker stop (joined by a Manager
        stop) -> exited; the Task stays closed; one terminal_done notice; the journal tells the whole story."""
        task = self.open_free_task()
        started = self.run_command(SLOW_INT.format(tag="t1"))
        command_id = started["command_id"]
        group, ticks = self.command_group()

        # CS-AC-01/02: the cancel answers with the live command and never claims it ended
        cancelled = self.cancel(task)
        self.assertEqual([(c["command_id"], c["state"]) for c in cancelled["host_commands"]],
                         [(command_id, "running")])
        note = cancelled["host_command_note"].lower()
        self.assertTrue("cancel" in note and "stop" in note, note)
        self.assertEqual(cancelled["task_id"], task.task_id)
        text = json.dumps(cancelled).lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, text)
        shown = self.backend.snapshot()["task"]
        self.assertEqual((shown["status"], [(c["command_id"], c["state"]) for c in shown["host_commands"]]),
                         ("closed", [(command_id, "running")]), "/task: closed Task, running command")
        status = self.call(ActorRole.MANAGER, "workbench_status", {})[0]
        self.assertEqual([(c["command_id"], c["state"]) for c in status["task"]["host_commands"]],
                         [(command_id, "running")])
        self.assertEqual(self.worker_terminal()["status"], "running", "a cancel stops nothing")
        self.assertTrue(is_live(group, ticks))

        # CS-AC-03/10: the Worker's stop waits for the final state; meanwhile the command reads as stopping
        box = self.worker_stop_async(command_id)
        self.wait_stopping(command_id)
        self.assertEqual(self.worker_terminal()["status"], "stopping")
        self.assertEqual(self.backend.flow.tasks[task.task_id].status, "closed")
        joined = self.manager_stop(command_id, reason="me too")  # a second requester joins the one episode
        stopped = self.finish(box, "the Worker's stop result")
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["requested_by"], stopped["command_id"]),
                         ("exited", 130, "worker", command_id), stopped)
        self.assertNotIn("repeat", stopped)
        self.assertEqual([s["signal"] for s in stopped["signals"]], ["SIGINT"])
        self.assertEqual((joined["status"], joined["requested_by"], joined["exit_code"], joined.get("repeat")),
                         ("exited", "worker", 130, True), joined)
        again = self.worker_terminal(command=None, stop=command_id)
        self.assertEqual((again["status"], again["exit_code"], again.get("repeat")), ("exited", 130, True))
        self.assertFalse(is_live(group, ticks))

        # CS-AC-15: the stop did not reopen the closed Task or make it active
        self.assertEqual(self.backend.flow.tasks[task.task_id].status, "closed")
        self.assertIsNone(self.backend.flow.active())
        self.assertEqual(self.backend.snapshot()["task"]["host_commands"], [], "an ended command is not listed")
        # the usual completion notice came once, separately from the stop result
        # (a Task that was just cancelled keeps the Worker busy for a while: the notice can take ~40 s)
        self.wait(lambda: self.terminal_done(command_id), "the completion notice", timeout=80)
        time.sleep(0.5)
        (done,) = self.terminal_done(command_id)
        self.assertEqual((done["exit_code"], done["stopped_by"]), (130, "worker"))
        # CS-AC-19: the shell is back for ordinary commands and the log is kept
        self.backend.terminal._wait_seconds = 20
        later = self.worker_terminal(command="echo after-the-stop")
        self.assertEqual((later["status"], later["exit_code"]), ("exited", 0), later)
        self.assertIn("ready-t1", Path(started["log_path"]).read_text())
        # CS-AC-20: requester, target, signals with times, end state, residual, closed-Task context
        records = self.records()
        target = self.assert_episode_audit(
            records, command_id, requested_by="worker", owner="worker", kind="terminal", status="exited",
            exit_code=130, signals=["SIGINT"], task_id=task.task_id, task_status="closed",
            target={"pid": group, "start_ticks": ticks, "process_group": group, "session": self.shell.shell.parent_pid})
        self.assertIn({"pid": group, "start_ticks": ticks}, target["tracked"])
        self.assertEqual(len([r for r in records if r["type"] == "command_stop_signal"]), 1, "one episode, one signal")
        self.assert_audit_clean(records, "ready-t1", "trap ")
        entry = self.run_entry(task.task_id, command_id)
        self.assertEqual((entry["status"], entry["exit_code"], entry["stopped_by"]), ("exited", 130, "worker"))

    def test_a_cancel_without_any_command_lists_none(self):
        """CS-AC-01 (second half): no command, no 'running' claim and no note."""
        task = self.open_free_task()
        quiet = self.cancel(task)
        self.assertEqual(quiet["host_commands"], [])
        self.assertNotIn("host_command_note", quiet)
        self.assertEqual(self.backend.snapshot()["task"]["host_commands"], [])

    def test_a_cancel_after_the_command_ended_lists_none(self):
        """CS-AC-01 (second half): a command that already ended is never shown as running."""
        task = self.open_free_task()
        self.backend.terminal._wait_seconds = 20
        ended = self.worker_terminal(command="echo already-over")
        self.assertEqual((ended["status"], ended["exit_code"]), ("exited", 0))
        after = self.cancel(task)
        self.assertEqual(after["host_commands"], [])
        self.assertNotIn("host_command_note", after)
        self.assertEqual(self.backend.snapshot()["task"]["host_commands"], [])


@gated
class WorkerRestartManagerStopTests(FinalBackend):
    def restart(self):
        old = self.backend.bridge.peer(ActorRole.WORKER)
        result, _ = self.call(ActorRole.MANAGER, "restart_worker", {"reason": "the worker lost its context"})
        self.assertEqual(result["status"], "restarted", result)
        self.wait(lambda: self.backend.bridge.peer(ActorRole.WORKER) is not None
                  and self.backend.bridge.peer(ActorRole.WORKER).session_id != old.session_id, "the new worker")
        self.protect_omps()
        return self.backend.bridge.peer(ActorRole.WORKER)

    def test_the_manager_stops_the_old_workers_command_and_a_new_worker_stops_its_own(self):
        """CS-AC-03, 11, 14, 20: owner is the role `worker` across a restart; the Manager's stop is recorded as
        manager on the result, the Worker's notice and commands_run; a decoy with the same argv is never hit."""
        task = self.open_free_task()
        decoy = self.spawn("sleep", "620.21")  # same argv as the command below, outside the host shell
        started = self.run_command("sleep 620.21")
        command_id = started["command_id"]
        (pid,) = [p for p in authority.pids_running("sleep", "620.21") if p != decoy.pid]
        group, ticks = self.command_group()
        self.restart()
        self.assertTrue(is_live(pid), "the restart killed nothing")

        stopped = self.manager_stop(command_id)
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["signal"], stopped["signal_name"],
                          stopped["requested_by"]), ("exited", 130, 2, "SIGINT", "manager"), stopped)
        self.assertEqual(self.backend.handoffs.pending_approvals(), [], "no user approval")
        self.assertFalse(is_live(pid))
        self.assertTrue(is_live(decoy.pid), "argv equal to the command's is not an identity")
        self.wait(lambda: self.terminal_done(command_id), "the terminal_done notice")
        (done,) = self.terminal_done(command_id)
        self.assertEqual((done["exit_code"], done["stopped_by"], done["stop_signal"]), (130, "manager", "SIGINT"))
        fetched = self.worker_terminal()
        self.assertEqual((fetched["status"], fetched["stopped_by"], fetched["stop_signal"]),
                         ("exited", "manager", "SIGINT"))
        entry = self.run_entry(task.task_id, command_id)
        self.assertEqual((entry["status"], entry["exit_code"], entry["stopped_by"]), ("exited", 130, "manager"))
        self.assert_episode_audit(self.records(), command_id, requested_by="manager", owner="worker",
                                  kind="terminal", status="exited", exit_code=130, signals=["SIGINT"],
                                  task_id=task.task_id, task_status="running",
                                  target={"pid": group, "start_ticks": ticks})

        # a command of the new Worker session: the new session stops it (the role owns it)
        second = self.run_command("sleep 620.22")
        again = self.worker_terminal(command=None, stop=second["command_id"])
        self.assertEqual((again["status"], again["requested_by"], again["exit_code"]), ("exited", "worker", 130))
        self.assertEqual(self.backend.flow.tasks[task.task_id].status, "running", "the Task is not touched")


@gated
class RefusalTests(FinalBackend):
    def worker_stop(self, command_id):
        return self.worker_terminal(command=None, stop=command_id)

    def assert_nothing_signalled(self):
        self.assertEqual((self.env.attempts, self.env.sent), ([], []))
        self.assertEqual(self.records("command_stop_signal", "command_stop_target"), [])

    def test_ended_unknown_previous_backend_and_user_programs_get_no_signal(self):
        """CS-AC-06, 07, 13, 17, 19: already_exited / not_found / previous_backend_command / a user's own foreground
        program: reasons returned, journaled, zero signals; a lone Ctrl+C at a user program takes the ordinary rule."""
        # CS-AC-07: a command that has ended
        self.backend.terminal._wait_seconds = 20
        for requester, stop in (("worker", self.worker_stop), ("manager", self.manager_stop)):
            ended = self.worker_terminal(command="echo over-620; exit 3")  # a fresh ended command per requester
            self.assertEqual((ended["status"], ended["exit_code"]), ("exited", 3), ended)
            result = stop(ended["command_id"])
            self.assertEqual((result["status"], result["exit_code"], result["requested_by"], result["signals"]),
                             ("already_exited", 3, requester, []), result)
            self.assertIn("signal", result)
            self.assertTrue(result.get("ended_at"), result)
        # the other requester asking about the same ended command reads the same final answer
        other = self.manager_stop(ended["command_id"])
        self.assertEqual((other["status"], other["exit_code"], other["signals"]), ("already_exited", 3, []), other)
        # CS-AC-07 / CS-AC-13: an id nobody knows, and one only an earlier backend's journal knows
        unknown_id = str(uuid4())
        previous = str(uuid4())
        survivor = self.spawn("sleep", "620.31")
        self.backend.handoffs.record({"type": "terminal_started", "command_id": previous, "task_id": None,
                                      "command": "sleep 620.31", "cwd": "/tmp", "log_path": "/tmp/none.log",
                                      "started_at": "2026-10-20T10:00:00Z"})
        for requester, stop in (("worker", self.worker_stop), ("manager", self.manager_stop)):
            result = stop(unknown_id)
            self.assertEqual((result["status"], result["reason"], result["requested_by"]),
                             ("not_found", "unknown_command", requester), result)
            result = stop(previous)
            self.assertEqual((result["status"], result["reason"], result["requested_by"]),
                             ("refused", "previous_backend_command", requester), result)
            self.assertIn("stop_survivor", result["detail"])
        self.assertTrue(is_live(survivor.pid), "the earlier backend's process was not signalled")
        refused = [(r["command_id"], r["requested_by"], r["status"], r["reason"])
                   for r in self.records("command_stop_refused")]
        for requester in ("worker", "manager"):
            self.assertIn((unknown_id, requester, "not_found", "unknown_command"), refused)
            self.assertIn((previous, requester, "refused", "previous_backend_command"), refused)
        self.assert_nothing_signalled()

        # CS-AC-06 / 17: a program the user starts in the host shell has no command_id and is no target
        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"sleep 620.32\r", "input"))
        self.wait(lambda: authority.pids_running("sleep", "620.32"), "the user's program")
        (user_pid,) = authority.pids_running("sleep", "620.32")
        for stop in (self.worker_stop, self.manager_stop):
            self.assertEqual(stop(str(uuid4()))["status"], "not_found")
        self.assertTrue(is_live(user_pid))
        self.assert_nothing_signalled()
        # the Workbench Ctrl+C exception does not apply: the user's own program gets the ordinary key
        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        self.wait(lambda: not is_live(user_pid), "the user's program to end by its own Ctrl+C")
        self.assertEqual(self.records("command_interrupt"), [], "no Workbench command, no interrupt record")
        self.wait(lambda: self.shell.state["parent_mode"] == "manual_prompt", "the prompt to return")

        # a live Worker command is the potential victim for the remaining refusals
        self.backend.terminal._wait_seconds = 1
        started = self.run_command("sleep 620.33")
        (victim,) = authority.pids_running("sleep", "620.33")
        for stop in (self.worker_stop, self.manager_stop):
            self.assertEqual(stop(unknown_id)["status"], "not_found")
            self.assertEqual(stop(previous)["reason"], "previous_backend_command")
        both = self.worker_terminal(command="echo never", stop=started["command_id"])
        self.assertEqual((both["status"], both["reason"]), ("rejected", "invalid_arguments"), both)
        self.assertNotIn(started["command_id"], json.dumps(both), "the value is not echoed")
        refused_new = self.worker_terminal(command="echo never")
        self.assertEqual(refused_new["reason"], "terminal_command_running", "stop is not a way around the rule")
        self.assertTrue(is_live(victim))
        self.assert_nothing_signalled()
        # only now is the command stopped, by its owner
        final = self.worker_stop(started["command_id"])
        self.assertEqual((final["status"], final["exit_code"]), ("exited", 130), final)
        self.assertTrue(self.env.signalled_pids(), "the command's own tree was signalled")
        self.assert_audit_clean(self.records())


@gated
class UnknownAndCorrectionTests(FinalBackend):
    deny = True

    @property
    def go(self):
        return Path(self._dir.name) / "go"

    def test_an_unconfirmed_stop_is_unknown_blocks_reuse_and_the_late_end_corrects_it(self):
        """CS-AC-10, 18, 23: signals refused (EPERM) -> unknown with a reason, never exited; the command stays
        occupied; repeats from either requester change nothing; the later natural exit corrects the record
        (history kept) without a new signal; the corrected result is what later requests read."""
        task = self.open_free_task()
        started = self.run_command(f"while [ ! -e {self.go} ]; do sleep 0.05; done; echo finished-620; exit 7")
        command_id = started["command_id"]
        unknown = self.worker_terminal(command=None, stop=command_id)
        self.assertEqual((unknown["status"], unknown["reason"], unknown["requested_by"]),
                         ("unknown", "permission_denied", "worker"), unknown)
        self.assertNotEqual(unknown["status"], "exited")
        self.assertEqual(self.env.sent, [], "the kernel refused every signal")
        attempts = len(self.env.attempts)
        state = self.worker_terminal()
        self.assertEqual(state["status"], "unknown", state)
        self.assertEqual([c["state"] for c in self.snapshot_commands()], ["unknown"], "shown, not hidden")
        blocked = self.worker_terminal(command="echo reuse")
        self.assertEqual(blocked["reason"], "terminal_command_running", "an unconfirmed command is not rerunnable")
        repeat = self.manager_stop(command_id, reason="again")
        self.assertEqual((repeat["status"], repeat["reason"], repeat["requested_by"], repeat.get("repeat")),
                         ("unknown", "permission_denied", "worker", True), repeat)
        self.assertEqual(len(self.env.attempts), attempts, "a repeat starts no new signal or timer")
        (finished,) = self.records("command_stop_finished")
        self.assertEqual((finished["status"], finished["reason"]), ("unknown", "permission_denied"))
        signal_records = len(self.records("command_stop_signal"))

        self.go.write_text("go")  # the command ends by itself (exit 7)
        self.wait(lambda: self.records("command_stop_corrected"), "the correction")
        (corrected,) = self.records("command_stop_corrected")
        self.assertEqual((corrected["command_id"], corrected["from"], corrected["previous_reason"],
                          corrected["to"], corrected["exit_code"]),
                         (command_id, "unknown", "permission_denied", "exited", 7), corrected)
        late = self.manager_stop(command_id, reason="late")
        self.assertEqual((late["status"], late["exit_code"], late["corrected"], late.get("repeat")),
                         ("exited", 7, True, True), late)
        self.assertEqual([(h["status"], h["reason"]) for h in late["history"]], [("unknown", "permission_denied")])
        self.assertEqual(late["requested_by"], "worker", "the original requester")
        self.assertEqual(len(self.env.attempts), attempts, "the correction sent nothing")
        self.assertEqual(len(self.records("command_stop_signal")), signal_records)
        self.assertEqual(self.env.sent, [])
        self.backend.terminal._wait_seconds = 20
        again = self.worker_terminal(command="echo free-again")
        self.assertEqual((again["status"], again["exit_code"]), ("exited", 0), again)
        self.assertEqual(self.backend.flow.tasks[task.task_id].status, "running")
        self.assert_audit_clean(self.records())


@gated
class LeftSessionTests(FinalBackend):
    def test_a_descendant_that_left_the_session_is_reported_never_signalled_and_its_end_corrects(self):
        """CS-AC-05, 23: the part of the tree that can be signalled ends; a setsid descendant is reported
        (left_session), not signalled, the stop is unknown (not 'clean'); when the test ends it, `exited`."""
        command = "setsid -f sleep 620.41 >/dev/null 2>&1 </dev/null; echo ready-t6; sleep 620.42"
        started = self.run_command(command)
        command_id = started["command_id"]
        self.wait(lambda: authority.pids_running("sleep", "620.41"), "the setsid descendant")
        (escaped,) = authority.pids_running("sleep", "620.41")
        info = proc_info(escaped)
        self.assertNotEqual(info["session"], self.shell.shell.parent_pid, "it left the host shell's session")
        self.addCleanup(kill_exact, escaped, info["start_ticks"])
        result = self.manager_stop(command_id)
        self.assertEqual((result["status"], result["reason"]), ("unknown", "left_session_descendants"), result)
        self.assertEqual(result["exit_code"], 130, "the main process's exit is reported")
        left = result["residual"]["left_session"]
        self.assertIn(escaped, [item["pid"] for item in left], result["residual"])
        self.assertTrue(result["residual"]["unchecked"], "what was not checked is said")
        self.assertTrue(is_live(escaped, info["start_ticks"]), "the escaped process was not signalled")
        self.assertNotIn(escaped, self.env.signalled_pids())
        self.assertEqual(self.worker_terminal()["status"], "unknown")
        self.assertEqual(self.worker_terminal(command="echo busy")["reason"], "terminal_command_running")
        # the test ends the process it started; the owner's observation corrects the stop (no new signal)
        sent = list(self.env.sent)
        kill_exact(escaped, info["start_ticks"])
        self.wait(lambda: self.records("command_stop_corrected"), "the correction", )
        late = self.worker_terminal(command=None, stop=command_id)
        self.assertEqual((late["status"], late["corrected"], late["exit_code"]), ("exited", True, 130), late)
        self.assertEqual([h["reason"] for h in late["history"]], ["left_session_descendants"])
        self.assertEqual(self.env.sent, sent)
        self.backend.terminal._wait_seconds = 20
        self.assertEqual(self.worker_terminal(command="echo free")["status"], "exited")


@gated
class PausedAndHeldStopTests(FinalBackend):
    def test_pause_and_boot_hold_during_a_stop_do_not_hold_the_stop_but_hold_new_work(self):
        """CS-AC-22: a stop under way when automation is paused (Worker's) or the boot hold is set (Manager's)
        completes; stops requested in those states complete; new commands and new Task work stay held."""
        # (no open Task: pausing with an active Task needs the Worker's interruption confirmation, which a fake
        # OMP does not give, and has nothing to do with stop)
        first = self.run_command(SLOW_INT.format(tag="t5a"))
        box = self.worker_stop_async(first["command_id"])
        self.wait_stopping(first["command_id"])
        self.backend.pause()
        self.assertTrue(self.backend._automation_paused())
        stopped = self.finish(box, "the Worker's stop under pause")
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["requested_by"]),
                         ("exited", 130, "worker"), stopped)
        marker = Path(self._dir.name) / "never-ran"
        refused = self.worker_terminal(command=f"touch {marker}")
        self.assertEqual(refused["status"], "paused", refused)
        held = self.call(ActorRole.MANAGER, "to_worker", {"kind": "work", "message": "more",
                                                         "spec": {"goal": "g", "paths": []}})[0]
        self.assertEqual(held["status"], "held", held)
        self.backend.resume(True)
        self.wait(lambda: not self.backend._automation_paused(), "the resume")

        second = self.run_command(SLOW_INT.format(tag="t5b"))
        mbox = self.start_call(ActorRole.MANAGER, "stop_command", {"command_id": second["command_id"],
                                                                   "reason": "the user asked"})
        self.wait_stopping(second["command_id"])
        self.backend.hold.set(BOOT_HOLD, detail="boot not confirmed")
        stopped = self.finish(mbox, "the Manager's stop under the boot hold")
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["requested_by"]),
                         ("exited", 130, "manager"), stopped)
        refused = self.worker_terminal(command=f"touch {marker}")
        self.assertEqual(refused["status"], "held", refused)
        self.assertFalse(marker.exists(), "a held command never ran")
        self.assertEqual(self.backend._hold_reason(ActorRole.WORKER), BOOT_HOLD, "the stop did not clear the hold")
        third = self.call(ActorRole.MANAGER, "to_worker", {"kind": "work", "message": "later",
                                                          "spec": {"goal": "g", "paths": []}})[0]
        self.assertEqual(third["status"], "held", third)


@gated
class UserCtrlCTests(FinalBackend):
    def test_only_a_lone_ctrl_c_reaches_a_worker_command_while_the_manager_owns_the_input(self):
        """CS-AC-16, 17, 20: other keys, paste and mixed input are rejected whole; the lone byte reaches the
        foreground group, the command ends 130 (not a stop), and the journal names requester `user`."""
        started = self.run_command("sleep 620.51")
        command_id = started["command_id"]
        group, ticks = self.command_group()
        for data, kind in ((b"a", "input"), (b"\x03a", "input"), (b"a\x03", "input"), (b"\x03\x03", "input"),
                           (b"\x03", "paste")):
            with self.subTest(data=data, kind=kind):
                refused = self.backend.admit(PaneId.HOST_SHELL, data, kind)
                self.assertEqual(refused[0], Reason.INPUT_OWNER_MANAGER, refused)
        self.assertTrue(is_live(group, ticks), "no byte of a rejected input reached the command")
        self.assertEqual(self.records("command_interrupt"), [])

        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        self.wait(lambda: self.worker_terminal()["status"] == "exited", "the command to end")
        ended = self.worker_terminal()
        self.assertEqual((ended["exit_code"], ended["command_id"]), (130, command_id), ended)
        self.assertNotIn("stopped_by", ended, "a user's Ctrl+C is not a stop")
        (record,) = self.records("command_interrupt")
        at = record.pop("at")
        self.assertTrue(at)
        self.assertEqual({k: record[k] for k in ("requested_by", "command_id", "pid", "start_ticks",
                                                 "process_group", "delivered")},
                         {"requested_by": "user", "command_id": command_id, "pid": group, "start_ticks": ticks,
                          "process_group": group, "delivered": True})
        self.assertEqual(record["input_owner"], "manager", "the input owner did not change")
        self.assertEqual(self.records("command_stop_requested", "command_stop_signal"), [])
        self.assert_audit_clean(self.records())

    def test_a_lone_ctrl_c_also_reaches_the_command_while_a_takeover_is_only_requested(self):
        """CS-AC-16/17: the intermediate (requested, unconfirmed) takeover state still delivers only the lone byte."""
        self.run_command("sleep 620.52")
        group, ticks = self.command_group()
        self.shell.state = self.shell.shell.request_takeover()
        self.assertTrue(self.shell.state["takeover_requested"])
        refused = self.backend.admit(PaneId.HOST_SHELL, b"a", "input")
        self.assertIsNotNone(refused, "an ordinary key stays refused")
        self.assertTrue(is_live(group, ticks))
        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        self.wait(lambda: not is_live(group, ticks), "the command to end by Ctrl+C")
        (record,) = self.records("command_interrupt")
        self.assertEqual((record["requested_by"], record["pid"], record["delivered"]), ("user", group, True))


# ----------------------------------------------------------------------------------------------- experiment rig
class ExperimentFinalFixture(AuditChecks, experiment.ExperimentStopFixture):
    deny = False

    def make_env(self):
        env = AuditedEnvironment(deny=self.deny, shell_pid=self.pane.shell.parent_pid)
        env.protected.add(self.pane.shell.parent_pid)
        env.must_live.add(self.pane.shell.parent_pid)
        self.audited = env
        return env

    def setUp(self):
        super().setUp()
        self.sentinel = subprocess.Popen(["sleep", "620.60"], start_new_session=True, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sentinel_ticks = proc_info(self.sentinel.pid)["start_ticks"]
        self.addCleanup(lambda: (kill_exact(self.sentinel.pid, self.sentinel_ticks), self.sentinel.wait()))

    def tearDown(self):
        try:  # before the rig closes the pane: the shell must have survived everything the test did
            self.assert_experiment_safety()
        finally:
            super().tearDown()

    def assert_experiment_safety(self):
        self.assertEqual(self.audited.violations, [])
        self.assertTrue(is_live(self.sentinel.pid, self.sentinel_ticks), "the sentinel outside the tree was hit")
        self.assertNotIn(self.sentinel.pid, self.audited.signalled_pids())
        self.assertTrue(self.pane.alive() and not self.pane.exited(), "the host shell survived")

    def records_of(self, *types):
        return [r for r in self.records if r["type"] in types]

    def command(self, seconds, tag):
        return f"printf 'started-{tag}\\n'; sleep {seconds}; printf PASS > outcome.txt"

    def cancel(self, task_id):
        return self.to_worker({"kind": "experiment", "message": "not needed any more", "task_id": task_id,
                               "cancel": True})

    @property
    def go(self):
        return self.root / "go"

    def waiting_command(self):
        return f"printf 'started\\n'; while [ ! -e {self.go} ]; do sleep 0.05; done; printf PASS > outcome.txt"


@gated
class ExperimentFinalTests(ExperimentFinalFixture):
    def test_cancel_then_worker_refusal_then_manager_stop_closes_the_task_cancelled_without_judgment(self):
        """CS-AC-02, 06, 12, 19, 20: cancel_requested + host_commands; the Worker's stop is refused (not_owner) with
        zero signals; the Manager's stop ends the command and the Task closes `cancelled`, no judgment."""
        task_id, run_id = self.running_experiment(self.command(620.61, "e1"))
        group = os.tcgetpgrp(self.pane._master_fd)
        ticks = proc_info(group)["start_ticks"]
        cancel = self.cancel(task_id)
        self.assertEqual((cancel["status"], cancel["task_id"]), ("cancel_requested", task_id), cancel)
        self.assertEqual([(c["command_id"], c["kind"], c["owner"], c["state"]) for c in cancel["host_commands"]],
                         [(run_id, "experiment", "manager", "running")])
        self.assertIn("stop_command", cancel["detail"])
        for word in ("stopped", "terminated", "killed"):
            self.assertNotIn(word, cancel["detail"].lower())
        self.assertEqual(self.view()["status"], "cancelling")
        refused = self.stop_service.stop("worker", run_id)
        self.assertEqual((refused["status"], refused["reason"], refused["requested_by"]),
                         ("refused", "not_owner", "worker"), refused)
        self.assertEqual((self.audited.attempts, self.audited.sent), ([], []))
        self.assertTrue(is_live(group, ticks))
        self.assertEqual(self.view()["status"], "cancelling")

        stopped = self.stop_service.stop("manager", run_id, reason="the user cancelled")
        self.assertEqual((stopped["status"], stopped["exit_code"], stopped["signal_name"]),
                         ("exited", 130, "SIGINT"), stopped)
        self.assertTrue(wait_until(lambda: self.view()["status"] == "closed", 30), self.view())
        task = self.view()
        self.assertEqual((task["closed_reason"], task["last_result"]),
                         ("cancelled", {"run_id": run_id, "outcome": "cancelled"}))
        self.assertEqual((task["host_commands"], self.judge_calls), ([], 0))
        self.assertFalse(is_live(group, ticks))
        self.assertEqual(self.run_history(run_id)[-1]["kind"], "cancelled")
        self.assert_episode_audit(self.records, run_id, requested_by="manager", owner="manager", kind="experiment",
                                  status="exited", exit_code=130, signals=["SIGINT"], task_id=task_id,
                                  task_status="cancelling", target={"pid": group, "start_ticks": ticks})
        self.assertEqual([(r["command_id"], r["requested_by"], r["reason"])
                          for r in self.of_type("command_stop_refused")], [(run_id, "worker", "not_owner")])
        self.assert_audit_clean(self.records, "printf", "sleep 620")

    def test_a_manager_stop_without_a_cancel_closes_the_run_as_stopped_with_no_retry(self):
        """CS-AC-21, 19, 20: outcome `stopped` (not failure/cancel), stopped_by manager, no judgment, no new run."""
        task_id, run_id = self.running_experiment(self.command(620.62, "e2"))
        stopped = self.stop_service.stop("manager", run_id, reason="not needed")
        self.assertEqual((stopped["status"], stopped["exit_code"]), ("exited", 130), stopped)
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", 30), self.view())
        result = self.view()["last_result"]
        self.assertEqual((result["outcome"], result["stopped_by"], result["run_id"], result["run_closed"]),
                         ("stopped", "manager", run_id, True), result)
        self.assertNotIn(result.get("judgment"), ("success", "failure"))
        self.assertEqual(self.judge_calls, 0)
        history = self.run_history(run_id)
        self.assertEqual((history[-1]["kind"], history[-1]["details"].get("outcome"),
                          history[-1]["details"].get("stopped_by")), ("completed", "stopped", "manager"))
        ended = [r for r in self.ledger() if r["type"] == "run_ended" and r["run_id"] == run_id]
        self.assertEqual([(r["outcome"], r["stopped_by"]) for r in ended], [("stopped", "manager")])
        time.sleep(1.0)  # nothing follows: no automatic retry, no second run
        self.assertEqual(self.flow.tasks[task_id].runs_started, 1)
        self.assertEqual(self.view()["status"], "finished")
        self.assertEqual(self.flow.worker_view(), {"state": "idle", "task_id": None})

    def test_an_unknown_stop_closes_nothing_and_the_observed_exit_decides_both_outcomes(self):
        """CS-AC-12, 21, 23 (unknown): a refused-signal stop is unknown; the cancelling Task stays cancelling (reason
        visible); once the command ends the Task closes cancelled; the corrected result is repeatable."""
        self.audited.deny = True
        task_id, run_id = self.running_experiment(self.waiting_command())
        self.assertEqual(self.cancel(task_id)["status"], "cancel_requested")
        unknown = self.stop_service.stop("manager", run_id)
        self.assertEqual((unknown["status"], unknown["reason"]), ("unknown", "permission_denied"), unknown)
        task = self.view()
        self.assertEqual((task["status"], task["held_reason"]), ("cancelling", "cancel_waiting_for_host_exit"))
        (shown,) = task["host_commands"]
        self.assertEqual((shown["command_id"], shown["state"], shown["stop"]["reason"]),
                         (run_id, "unknown", "permission_denied"))
        time.sleep(0.5)
        self.assertEqual(self.view()["status"], "cancelling", "nothing closes it while the end is unobserved")
        attempts = len(self.audited.attempts)
        self.go.write_text("go")
        self.assertTrue(wait_until(lambda: self.view()["status"] == "closed", 30), self.view())
        self.assertEqual((self.view()["last_result"], self.judge_calls), ({"run_id": run_id, "outcome": "cancelled"}, 0))
        late = self.stop_service.stop("worker", run_id)
        self.assertEqual((late["status"], late["reason"]), ("refused", "not_owner"), "the Worker still may not")
        corrected = self.stop_service.stop("manager", run_id)
        self.assertEqual((corrected["status"], corrected["corrected"], corrected["repeat"]), ("exited", True, True))
        self.assertEqual([(h["status"], h["reason"]) for h in corrected["history"]], [("unknown", "permission_denied")])
        self.assertEqual(len(self.audited.attempts), attempts, "the correction sent no signal")

    def test_an_unknown_stop_without_a_cancel_leaves_the_outcome_open_until_the_exit_is_observed(self):
        """CS-AC-21 (second sentence), 23: unknown -> outcome not decided; the observed exit then closes the run
        as `stopped` by the Manager (SPEC: the stop mark is set before the first signal)."""
        self.audited.deny = True
        task_id, run_id = self.running_experiment(self.waiting_command())
        unknown = self.stop_service.stop("manager", run_id)
        self.assertEqual((unknown["status"], unknown["reason"]), ("unknown", "permission_denied"), unknown)
        time.sleep(0.5)
        task = self.view()
        self.assertEqual(task["status"], "running")
        self.assertIsNone(task["last_result"], "no outcome while the end is unconfirmed")
        self.assertEqual([c["stop"]["reason"] for c in task["host_commands"]], ["permission_denied"])
        self.go.write_text("go")
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", 30), self.view())
        result = self.view()["last_result"]
        # Contract gap (reported as an ambiguity): SPEC says the stop mark is set "just before the first signal"; the
        # kernel refused every signal here and the command ended by itself, so either `stopped` (mark set) or the
        # ordinary judged path is a defensible reading. What is fixed: the run closes once, with no retry.
        self.assertIn(result["outcome"], ("stopped", "reported"), result)
        if result["outcome"] == "stopped":
            self.assertEqual(result.get("stopped_by"), "manager")
        self.assertIs(result["run_closed"], True)
        self.assertEqual(self.flow.tasks[task_id].runs_started, 1)

    def test_a_lone_ctrl_c_reaches_the_experiment_command_and_other_input_is_rejected(self):
        """CS-AC-16, 17, 20: the Manager owns the input of an experiment's command; the lone byte reaches it
        (exit 130, journal fact via the pane's listener, command id = run id), other input is rejected whole."""
        events = []
        self.pane.interrupt_listener = events.append
        task_id, run_id = self.running_experiment(self.command(620.63, "e5"))
        group = os.tcgetpgrp(self.pane._master_fd)
        ticks = proc_info(group)["start_ticks"]
        for data, kind in ((b"a", "input"), (b"\x03a", "input"), (b"\x03\x03", "input"), (b"\x03", "paste")):
            with self.subTest(data=data, kind=kind):
                refused = self.pane.admit(data, kind)
                self.assertEqual(refused[0], Reason.INPUT_OWNER_MANAGER, refused)
        self.assertTrue(is_live(group, ticks))
        self.assertEqual(events, [])
        self.assertIsNone(self.pane.admit(b"\x03"))
        self.assertTrue(wait_until(lambda: self.pane.state["lifecycle"]["main_exit"] == -signal.SIGINT, 15),
                        self.pane.state["lifecycle"])
        self.assertFalse(is_live(group, ticks))
        (event,) = events
        self.assertEqual((event["pid"], event["start_ticks"], event["process_group"], event["delivered"]),
                         (group, ticks, group, True))
        self.assertEqual(self.flow.experiment_command_for(event["request_id"]), run_id)
        self.assertEqual(self.of_type("command_stop_requested"), [], "a user's Ctrl+C is not a stop")
        # no automatic re-run may outlive the test: end the Task and let the rig close the pane
        self.cancel(task_id)
