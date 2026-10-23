"""CS-08 independent verification: a free-work cancel shows the still-running host command and never stops it.

Seam S1 (command-stop SPEC "테스트 결정"): a ``TaskFlow`` from the ``test_task_flow`` fixture plus a real
``TerminalService`` running a real PTY command (``ShellPane`` + ``HostShellPort``), wired the way ``Backend``
wires it (``flow.host_commands = terminal.commands_for_task``).

Expected values come from the contracts, not from the implementation: BRIEF CS-AC-01 / CS-AC-02 (free-work part) /
CS-D01, SPEC "취소 응답" and "명령 상태 표면", ticket CS-08 (entry schema, "tasks without commands keep their
cancel semantics"). The Tasks, command ids, sleep arguments and shell traps are this file's own.

Every process here is started by a test inside its own host shell; ``tearDown`` closes the pane (and with it the
process group) and then checks, by exact argv and start ticks, that no ``sleep`` this file started is left.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import signal
import threading
import time
import unittest
from pathlib import Path

from workbench.backend.command_stop import CommandStopService, LinuxStopEnvironment, StopPolicy
from workbench.backend.flow_terminal import HostGate, TerminalService
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.contracts.v1 import ActorRole
from workbench.terminal.shell_g2.prototype import ShellChoice

from test_flow_terminal import AUTOMATION, call, wait_until
from test_task_flow import FlowFixture

REDUCED = StopPolicy(identity_wait=2.0, sigint_wait=2.0, sigterm_wait=1.0, sigkill_wait=1.0, call_wait=8.0)
ENTRY_KEYS = {"command_id", "kind", "owner", "state", "started_at", "log_path", "stop"}
# words that would tell the reader a command ended or was ended; "stop" itself is the advice and is allowed
TERMINATION_WORDS = ("stopped", "terminated", "terminate", "killed", "kill ", "ended", "exited", "finished",
                     "interrupted", "aborted", "halted", "ceased", "종료", "중지됨", "중단됨", "멈췄", "멈춤",
                     "죽었", "죽임", "정지됨")
BAD_STATUS_VALUES = {"stopped", "terminated", "killed", "ended", "exited", "finished", "stopping"}
_UNIQUE = itertools.count(1)


def strings_of(value):
    """Every string in a nested structure, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings_of(key)
            yield from strings_of(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings_of(item)


def proc_argv_ticks(pid: int):
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat[stat.rindex(")") + 2:].split()
    return [a.decode(errors="replace") for a in argv if a], int(fields[19]), fields[0]


class DroppingSignalEnv(LinuxStopEnvironment):
    """S7: every signal send "succeeds" but nothing is delivered, so no stop stage can end the command; the stop
    ends ``unknown`` whatever stages the policy has (SIGINT only, or SIGINT/SIGTERM/SIGKILL)."""

    def __init__(self) -> None:
        self.attempts: list[int] = []

    def pidfd_send_signal(self, fd, signum):
        self.attempts.append(signum)


class CancelHostCommandBase(FlowFixture):
    """A free-work Task flow whose worker runs commands in a real host shell, with a leak check."""

    CHOICE = ShellChoice("bash", "/usr/bin/bash")
    def make_stop_env(self):
        return None

    def setUp(self):
        super().setUp()
        self.marks: set[str] = set()
        self.home = self.root / "home"
        self.home.mkdir()
        self.pane = ShellPane(self.CHOICE, {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8"})
        self.halt = threading.Event()
        self.loop = threading.Thread(target=self._backend_loop, daemon=True)
        self.loop.start()
        self.command_stop = CommandStopService(journal=self.service.record, policy=REDUCED, env=self.make_stop_env())
        self.terminal = TerminalService(
            handoffs=self.service, host_shell=lambda: HostShellPort(self.pane, lambda: self.pane), gate=HostGate(),
            log_root=self.root / "workflow" / "terminal", automation=lambda: AUTOMATION,
            active_task=self.flow.active_task, poll_interval=0.02, command_stop=self.command_stop)
        self.flow.host_commands = self.terminal.commands_for_task
        self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 30))
        self.assertIsNone(self.pane.admit(f"cd {self.home}\r".encode()))
        self.assertTrue(wait_until(lambda: self.pane.cwd() == str(self.home), 30))

    def _backend_loop(self):
        while not self.halt.is_set():
            self.pane.pump()
            time.sleep(0.01)

    def tearDown(self):
        self.terminal.close()
        self.command_stop.close()
        self.halt.set()
        self.loop.join(30)
        self.pane.close()
        leaked = self.leaked_sleeps()
        # clean by exact identity (pidfd + start ticks) before failing, so a defect leaves no residue
        for pid, ticks in leaked:
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                continue
            try:
                now = proc_argv_ticks(pid)
                if now is not None and now[1] == ticks:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            except OSError:
                pass
            finally:
                os.close(fd)
        super().tearDown()
        self.assertEqual(leaked, [], "sleep processes this test started survived the pane close")

    # -- own helpers -----------------------------------------------------------------------
    def sleeper(self, seconds: float | None = None) -> str:
        """A unique ``sleep`` argument this test owns; registered so the leak check can find it."""
        n = next(_UNIQUE)
        arg = f"{seconds if seconds is not None else 7000}.{n:04d}"
        self.marks.add(arg)
        return arg

    def leaked_sleeps(self):
        found = []
        deadline = time.monotonic() + 30
        while True:
            found = []
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                info = proc_argv_ticks(int(entry))
                if info and len(info[0]) == 2 and info[0][0].endswith("sleep") and info[0][1] in self.marks \
                        and info[2] not in ("Z", "X"):
                    found.append((int(entry), info[1]))
            if not found or time.monotonic() > deadline:
                return found
            time.sleep(0.1)

    def run_command(self, command, wait=1):
        self.terminal._wait_seconds = wait
        return self.terminal.handle(ActorRole.WORKER, call({"command": command}))

    def stop(self, command_id):
        return self.terminal.handle(ActorRole.WORKER, call({"command": None, "stop": command_id}))

    def cancel(self, task_id, message="the user changed plans"):
        return self.to_worker({"kind": "work", "message": message, "cancel": True, "task_id": task_id})

    def end_command(self, command_id):
        """The test ends its own command through the existing terminal stop path."""
        self.assertEqual(self.stop(command_id)["status"], "exited")

    def commands(self, task_id=None):
        return self.flow.status_view(task_id)["host_commands"]


class CancelResponseTests(CancelHostCommandBase):
    def test_running_command_is_listed_with_the_full_entry_and_a_stop_hint(self):
        # BRIEF CS-AC-01; ticket "제공할 동작": entry schema command_id/kind/owner/state/started_at/log_path/stop
        work = self.running_work()
        started = self.run_command(f"sleep {self.sleeper()}")
        self.assertEqual(started["status"], "running", started)
        result = self.cancel(work["task_id"])
        self.assertEqual((result["status"], result["task_id"]), ("cancelled", work["task_id"]), result)
        self.assertIn("worker_notified", result)
        self.assertIsInstance(result["host_commands"], list)
        self.assertEqual(len(result["host_commands"]), 1, result)
        entry = result["host_commands"][0]
        self.assertEqual(set(entry), ENTRY_KEYS, "exactly the specified entry keys (CS-09 consumes this schema)")
        self.assertEqual(entry["command_id"], started["command_id"])
        self.assertEqual((entry["kind"], entry["owner"], entry["state"]), ("terminal", "worker", "running"))
        self.assertEqual(entry["log_path"], started["log_path"])
        self.assertTrue(os.path.exists(entry["log_path"]), "the log path points at the command's real log")
        self.assertIsInstance(entry["started_at"], str)
        self.assertTrue(entry["started_at"])
        self.assertIsNone(entry["stop"], "no stop was requested")
        note = result["host_command_note"]
        self.assertIsInstance(note, str)
        lowered = note.lower()
        self.assertIn("cancel", lowered)
        self.assertRegex(lowered, r"does not stop|doesn't stop|never stops|not stop",
                         "the note says the cancel does not stop the command")
        self.assertIn("stop_command", note, "and names how to stop it")
        self.assertIn("terminal", lowered)
        self.end_command(started["command_id"])

    def test_cancel_response_has_no_termination_wording_or_status_and_the_command_keeps_running(self):
        # BRIEF CS-D01 / CS-AC-02: "어떤 취소 응답도 종료를 뜻하는 문구·상태를 쓰지 않는다"
        work = self.running_work()
        arg = self.sleeper()
        started = self.run_command(f"sleep {arg}")
        pids_before = self.sleep_pids(arg)
        self.assertEqual(len(pids_before), 1, pids_before)
        result = self.cancel(work["task_id"])
        self.assertEqual(result["status"], "cancelled")
        text = "\n".join(strings_of(result)).lower()
        for word in TERMINATION_WORDS:
            self.assertNotIn(word, text, f"{word!r} in {result}")
        for text_value in strings_of(result):
            self.assertNotIn(text_value.lower(), BAD_STATUS_VALUES, result)
        # the command really keeps running: same process, same pid, still alive after a grace period
        time.sleep(0.5)
        self.assertEqual(self.sleep_pids(arg), pids_before, "cancel signalled nothing")
        self.assertTrue(self.terminal.current()["running"])
        self.assertEqual(self.terminal.current()["command_id"], started["command_id"])
        self.assertEqual(self.commands(work["task_id"])[0]["state"], "running")
        self.end_command(started["command_id"])

    def sleep_pids(self, arg):
        pids = []
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                info = proc_argv_ticks(int(entry))
                if info and info[0][-2:] == ["sleep", arg] and info[2] not in ("Z", "X"):
                    pids.append((int(entry), info[1]))
        return pids

    def test_cancel_while_paused_keeps_its_detail_and_still_lists_the_command_without_termination_words(self):
        # ticket: "통지 보류 문구와 충돌하지 않는다"; SPEC: detail and note do not share a key
        work = self.running_work()
        started = self.run_command(f"sleep {self.sleeper()}")
        self.controller.automation = {"state": "paused"}
        result = self.cancel(work["task_id"])
        self.assertEqual(result["status"], "cancelled", result)
        self.assertEqual(result["worker_notice"], "after_resume")
        self.assertIn("after the resume", result["detail"])
        self.assertEqual([e["command_id"] for e in result["host_commands"]], [started["command_id"]])
        self.assertIn("host_command_note", result)
        self.assertNotEqual(result["detail"], result["host_command_note"])
        self.assertNotIn("Cancel does not stop", result["detail"], "the note does not leak into the held-notice detail")
        text = "\n".join(strings_of(result)).lower()
        for word in TERMINATION_WORDS:
            self.assertNotIn(word, text, f"{word!r} in {result}")
        self.controller.automation = {"state": "active"}
        self.end_command(started["command_id"])

    def test_no_command_gives_an_empty_list_no_note_and_the_old_response_shape(self):
        # BRIEF CS-AC-01 "없으면 실행 중이라고 표시하지 않음"; ticket: existing cancel semantics unchanged
        work = self.running_work()
        result = self.cancel(work["task_id"])
        # "notices" is the handoff service's pre-existing addition to every response, not part of this ticket
        self.assertEqual({k: v for k, v in result.items() if k != "notices"},
                         {"status": "cancelled", "task_id": work["task_id"], "worker_notified": True,
                          "host_commands": []})
        self.assertEqual([n["kind"] for n in result["notices"]], ["task_cancelled"])
        self.assertNotIn("host_command_note", result)

    def test_no_command_cancel_keeps_the_held_notice_detail_unchanged(self):
        work = self.running_work()
        self.controller.automation = {"state": "paused"}
        result = self.cancel(work["task_id"])
        self.assertEqual(set(result), {"status", "task_id", "worker_notified", "worker_notice", "detail",
                                       "notices", "host_commands"}, result)
        self.assertEqual(result["host_commands"], [])
        self.assertEqual(result["detail"], "Workbench is paused: the worker is told of the cancel after the resume "
                                           "(the notice was not sent yet and is delivered once).")
        self.assertIs(result["worker_notified"], False)

    def test_a_command_that_already_ended_is_not_listed(self):
        # BRIEF CS-AC-01 "이미 끝났으면 실행 중이라고 표시하지 않음"
        work = self.running_work()
        done = self.run_command("echo done-before-cancel; exit 5", wait=30)
        self.assertEqual((done["status"], done["exit_code"]), ("exited", 5), done)
        result = self.cancel(work["task_id"])
        self.assertEqual(result["host_commands"], [])
        self.assertNotIn("host_command_note", result)

    def test_a_command_ended_by_a_stop_before_the_cancel_is_not_listed(self):
        work = self.running_work()
        started = self.run_command(f"sleep {self.sleeper()}")
        self.end_command(started["command_id"])
        result = self.cancel(work["task_id"])
        self.assertEqual(result["host_commands"], [])
        self.assertNotIn("host_command_note", result)

    def test_a_rejected_cancel_keeps_its_old_shape(self):
        # existing semantics: unknown / closed / missing task_id are rejections, not "cancelled"
        work = self.running_work()
        self.assertEqual(self.cancel(None)["reason"], "task_id_required")
        self.cancel(work["task_id"])
        again = self.cancel(work["task_id"])
        self.assertEqual(again["reason"], "task_closed")
        self.assertNotEqual(again.get("status"), "cancelled")

    def test_the_experiment_cancel_request_is_not_this_tickets_surface_and_stays_cancel_requested(self):
        # CS-09 owns the experiment cancel; here only the pre-existing semantics must still hold
        self.gates.exit.clear()
        pending = self.new_experiment()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "running", 30))
        result = self.to_worker({"kind": "experiment", "message": "cancel it", "cancel": True,
                                 "task_id": pending["task_id"]})
        self.assertEqual(result["status"], "cancel_requested", result)
        self.assertEqual(result["task_id"], pending["task_id"])
        self.assertIn("host", result["detail"])
        self.gates.exit.set()
        self.assertTrue(wait_until(lambda: self.flow.task_view()["status"] == "closed", 30))


class TaskViewSurfaceTests(CancelHostCommandBase):
    def test_closed_task_shows_running_command_everywhere_then_empties_when_it_ends(self):
        # BRIEF CS-AC-02 free-work part; SPEC "명령 상태 표면": /task = snapshot()["task"], workbench_status.task
        work = self.running_work()
        started = self.run_command(f"sleep {self.sleeper()}")
        self.cancel(work["task_id"])
        for name, view in (("task_view", self.flow.task_view()), ("snapshot", self.controller.snapshot()["task"]),
                           ("status_view(id)", self.flow.status_view(work["task_id"])),
                           ("status_view()", self.flow.status_view())):
            self.assertEqual((view["status"], view["closed_reason"]), ("closed", "cancelled"), name)
            self.assertEqual([(e["command_id"], e["state"]) for e in view["host_commands"]],
                             [(started["command_id"], "running")], name)
            self.assertEqual(set(view["host_commands"][0]), ENTRY_KEYS, name)
        self.end_command(started["command_id"])
        self.assertTrue(wait_until(lambda: self.flow.task_view()["host_commands"] == [], 30))
        for view in (self.flow.task_view(), self.controller.snapshot()["task"], self.flow.status_view(),
                     self.flow.status_view(work["task_id"])):
            self.assertEqual(view["host_commands"], [])
            self.assertEqual(view["status"], "closed")

    def test_an_open_task_with_no_command_has_the_field_empty(self):
        work = self.running_work()
        self.assertEqual(self.flow.task_view()["host_commands"], [])
        self.assertEqual(self.flow.status_view(work["task_id"])["host_commands"], [])

    def test_a_stop_in_progress_reads_stopping_with_a_stop_summary_then_empties(self):
        # SPEC: running -> stopping while the episode runs; entry.stop is the episode summary
        work = self.running_work()
        started = self.run_command("trap 'sleep 1; exit 130' INT; echo ready; while :; do sleep 0.2; done")
        self.assertEqual(started["status"], "running", started)
        self.cancel(work["task_id"])
        box = {}
        thread = threading.Thread(target=lambda: box.update(result=self.stop(started["command_id"])), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 15)
        self.assertTrue(wait_until(lambda: [e["state"] for e in self.commands(work["task_id"])] == ["stopping"], 30),
                        self.commands(work["task_id"]))
        entry = self.commands(work["task_id"])[0]
        self.assertEqual(entry["command_id"], started["command_id"])
        self.assertIsInstance(entry["stop"], dict)
        self.assertTrue(entry["stop"], "a non-empty stop summary")
        self.assertEqual(entry["stop"].get("state"), "stopping")
        self.assertEqual(self.flow.task_view()["status"], "closed", "a stop never reopens the Task")
        self.assertEqual(self.terminal.current().get("state"), "stopping", "the terminal surface agrees")
        thread.join(30)
        self.assertEqual(box["result"]["status"], "exited", box)
        self.assertTrue(wait_until(lambda: self.commands(work["task_id"]) == [], 30))

    def make_stop_env(self):
        self.dropping = DroppingSignalEnv()
        return self.dropping if self.id().endswith("_unknown_and_the_command_stays_listed") else None

    def test_an_unconfirmed_stop_surfaces_as_unknown_and_the_command_stays_listed(self):
        # SPEC: unknown while the stop is unconfirmed and the owner has not seen the end; still listed.
        # S7: the signals are dropped, so this holds for a SIGINT-only engine and for the escalating one alike.
        work = self.running_work()
        arg = self.sleeper(9)
        started = self.run_command(f"sleep {arg}")
        self.assertEqual(started["status"], "running", started)
        self.cancel(work["task_id"])
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "unknown", result)
        self.assertTrue(self.dropping.attempts, "the engine did try to signal")
        entry = self.commands(work["task_id"])[0]
        self.assertEqual((entry["command_id"], entry["state"]), (started["command_id"], "unknown"), entry)
        self.assertIsInstance(entry["stop"], dict)
        self.assertEqual(entry["stop"].get("state"), "unknown")
        self.assertEqual(self.flow.task_view()["host_commands"][0]["state"], "unknown")
        self.assertEqual(self.flow.task_view()["status"], "closed")
        self.assertTrue(wait_until(lambda: self.commands(work["task_id"]) == [], 30),
                        "the owner observes the end by itself; then nothing is listed")

    def test_returned_views_are_copies_not_the_stored_state(self):
        work = self.running_work()
        started = self.run_command(f"sleep {self.sleeper()}")
        self.cancel(work["task_id"])
        view = self.flow.task_view()
        view["host_commands"][0]["state"] = "tampered"
        view["host_commands"].clear()
        view["status"] = "tampered"
        again = self.flow.task_view()
        self.assertEqual((again["status"], again["host_commands"][0]["state"]), ("closed", "running"))
        self.assertEqual(self.terminal.commands_for_task(work["task_id"])[0]["state"], "running")
        self.end_command(started["command_id"])


class AttachmentTests(CancelHostCommandBase):
    def test_a_command_without_a_task_never_attaches_to_any_task(self):
        # SPEC/ticket: task_id=null commands belong to no Task (fencing is CS-O01, out of scope)
        arg = self.sleeper()
        loose = self.run_command(f"sleep {arg}")
        self.assertEqual(loose["status"], "running", loose)
        self.assertEqual(self.terminal.commands_for_task("anything"), [])
        work = self.running_work()
        self.assertEqual(self.flow.task_view()["host_commands"], [])
        self.assertEqual(self.flow.status_view(work["task_id"])["host_commands"], [])
        self.assertEqual(self.controller.snapshot()["task"]["host_commands"], [])
        result = self.cancel(work["task_id"])
        self.assertEqual(result["host_commands"], [])
        self.assertNotIn("host_command_note", result)
        self.assertEqual(self.flow.task_view()["host_commands"], [])
        self.end_command(loose["command_id"])

    def test_another_tasks_command_never_attaches_and_stays_with_its_own_task(self):
        first = self.running_work("first job")
        started = self.run_command(f"sleep {self.sleeper()}")
        self.cancel(first["task_id"])
        second = self.running_work("second job")
        self.assertNotEqual(second["task_id"], first["task_id"])
        self.assertEqual(self.flow.task_view()["task_id"], second["task_id"])
        self.assertEqual(self.flow.task_view()["host_commands"], [], "the new current Task has no command")
        self.assertEqual(self.flow.status_view(second["task_id"])["host_commands"], [])
        self.assertEqual([e["command_id"] for e in self.flow.status_view(first["task_id"])["host_commands"]],
                         [started["command_id"]], "the old closed Task still shows its own")
        # cancelling the second Task must not claim the first Task's command
        result = self.cancel(second["task_id"])
        self.assertEqual(result["host_commands"], [])
        self.assertNotIn("host_command_note", result)
        self.end_command(started["command_id"])

    def test_an_unknown_task_id_has_no_commands(self):
        started = self.run_command(f"sleep {self.sleeper()}")
        self.assertEqual(self.terminal.commands_for_task("no-such-task"), [])
        self.assertEqual(self.terminal.commands_for_task(""), [])
        self.end_command(started["command_id"])

    def test_the_experiment_task_view_has_the_field_empty_without_a_worker_command(self):
        task_id = self.finished_experiment()
        self.assertEqual(self.flow.task_view()["host_commands"], [])
        self.assertEqual(self.flow.status_view(task_id)["host_commands"], [])


class NotStoredTests(CancelHostCommandBase):
    def test_host_commands_never_reach_the_ledger_or_the_sqlite_store_and_a_restart_recomputes(self):
        # ticket: "view()는 저장 대상이므로 host_commands는 조립 시점에 계산하고 ledger에 쓰지 않는다"
        work = self.running_work()
        started = self.run_command(f"sleep {self.sleeper()}")
        self.cancel(work["task_id"])
        self.flow.task_view()
        self.flow.status_view(work["task_id"])
        self.controller.snapshot()
        ledger_text = (self.root / "workflow" / "tasks-flow.jsonl").read_text()
        self.assertNotIn("host_command", ledger_text)
        self.assertNotIn(started["command_id"], ledger_text, "the command id is the terminal service's, not the ledger's")
        self.assertNotIn("host_commands", json.dumps(self.flow.tasks[work["task_id"]].__dict__, default=str))
        self.assertFalse(hasattr(self.flow.tasks[work["task_id"]], "host_commands"))
        sqlite_bytes = self.db.read_bytes() if self.db.exists() else b""
        self.assertNotIn(b"host_command", sqlite_bytes)
        self.assertNotIn(started["command_id"].encode(), sqlite_bytes)

        flow = self.restart()  # a fresh flow reloads from the stored records; the provider is not wired yet
        view = flow.task_view()
        self.assertEqual((view["task_id"], view["status"]), (work["task_id"], "closed"))
        self.assertEqual(view["host_commands"], [], "nothing was stored to resurrect, and no provider asked")
        flow.host_commands = self.terminal.commands_for_task  # wired again: computed from the live terminal
        self.assertEqual([e["command_id"] for e in flow.task_view()["host_commands"]], [started["command_id"]])
        ledger_after = (self.root / "workflow" / "tasks-flow.jsonl").read_text()
        self.assertNotIn("host_command", ledger_after)
        self.end_command(started["command_id"])
        self.assertTrue(wait_until(lambda: flow.task_view()["host_commands"] == [], 30))


class ProviderRobustnessTests(CancelHostCommandBase):
    def test_a_failing_provider_does_not_break_cancel_or_the_views(self):
        work = self.running_work()

        def boom(task_id):
            raise RuntimeError("provider failed")

        self.flow.host_commands = boom
        result = self.cancel(work["task_id"])
        self.assertEqual((result["status"], result["task_id"]), ("cancelled", work["task_id"]), result)
        self.assertEqual(result["host_commands"], [])
        self.assertNotIn("host_command_note", result)
        self.assertEqual(self.flow.task_view()["host_commands"], [])
        self.assertEqual(self.flow.status_view(work["task_id"])["host_commands"], [])

    def test_no_provider_wired_gives_empty_lists(self):
        work = self.running_work()
        self.flow.host_commands = None
        result = self.cancel(work["task_id"])
        self.assertEqual(result["host_commands"], [])
        self.assertEqual(self.flow.task_view()["host_commands"], [])


class ConcurrencyTests(CancelHostCommandBase):
    def flow_lock_is_free(self) -> bool:
        """From another thread: does a plain flow call finish at once (the flow lock is not held)?"""
        box = {}
        thread = threading.Thread(target=lambda: box.update(view=self.flow.worker_view()), daemon=True)
        thread.start()
        thread.join(3)
        return not thread.is_alive()

    def test_the_provider_is_asked_with_the_flow_lock_released_in_every_surface(self):
        # lock order: the terminal service must never wait on a flow lock held by the caller of commands_for_task
        work = self.running_work()
        started = self.run_command(f"sleep {self.sleeper()}")
        seen = []
        real = self.terminal.commands_for_task

        def probe(task_id):
            seen.append(self.flow_lock_is_free())
            return real(task_id)

        self.flow.host_commands = probe
        self.flow.task_view()
        self.flow.status_view(work["task_id"])
        self.flow.status_view()
        self.controller.snapshot()
        self.cancel(work["task_id"])
        self.assertGreaterEqual(len(seen), 5, seen)
        self.assertEqual(set(seen), {True}, f"the flow lock was held while the provider ran: {seen}")
        self.end_command(started["command_id"])

    def test_a_stalled_terminal_lock_blocks_only_the_view_not_the_flow(self):
        work = self.running_work()
        started = self.run_command(f"sleep {self.sleeper()}")
        release, grabbed = threading.Event(), threading.Event()

        def hold():
            with self.terminal._lock:
                grabbed.set()
                release.wait(20)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.assertTrue(grabbed.wait(30))
        box = {}
        asker = threading.Thread(target=lambda: box.update(view=self.flow.task_view()), daemon=True)
        asker.start()
        time.sleep(0.3)
        self.assertTrue(asker.is_alive(), "the view waits for the terminal lock")
        self.assertTrue(self.flow_lock_is_free(), "but the flow lock is free meanwhile")
        release.set()
        asker.join(30)
        holder.join(30)
        self.assertFalse(asker.is_alive())
        self.assertEqual([e["state"] for e in box["view"]["host_commands"]], ["running"])
        self.end_command(started["command_id"])

    def test_cancel_and_views_race_a_command_ending_without_deadlock_or_a_wrong_state(self):
        # repeated: cancel at a random moment around a short command's end while other threads read the views
        rng = random.Random(8128)
        for round_number in range(8):
            work = self.running_work(f"race round {round_number}")
            arg = self.sleeper(0)
            started = self.run_command(f"sleep 0.{rng.randint(15, 45):02d}; echo {arg}", wait=0.05)
            self.assertEqual(started["status"], "running", started)
            stop_readers = threading.Event()
            problems, outcome = [], {}

            def reader():
                while not stop_readers.is_set():
                    try:
                        for view in (self.flow.task_view(), self.flow.status_view(work["task_id"])):
                            for entry in view["host_commands"]:
                                if entry["state"] not in ("running", "stopping", "unknown") \
                                        or entry["command_id"] != started["command_id"] \
                                        or set(entry) != ENTRY_KEYS:
                                    problems.append(entry)
                    except Exception as exc:  # a view must never raise
                        problems.append(repr(exc))

            readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
            canceller = threading.Thread(
                target=lambda: (time.sleep(rng.random() * 0.12), outcome.update(result=self.cancel(work["task_id"]))),
                daemon=True)
            for thread in readers:
                thread.start()
            canceller.start()
            canceller.join(30)
            self.assertFalse(canceller.is_alive(), f"cancel deadlocked in round {round_number}")
            self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 30))
            stop_readers.set()
            for thread in readers:
                thread.join(30)
                self.assertFalse(thread.is_alive(), "a view call deadlocked")
            self.assertEqual(problems, [])
            result = outcome["result"]
            self.assertEqual(result["status"], "cancelled", result)
            listed = result["host_commands"]
            self.assertIn(len(listed), (0, 1), result)
            self.assertEqual("host_command_note" in result, bool(listed), "the note exists exactly when listed")
            if listed:
                self.assertEqual((listed[0]["command_id"], listed[0]["state"]), (started["command_id"], "running"))
            self.assertTrue(wait_until(lambda: self.flow.task_view()["host_commands"] == [], 30))


if __name__ == "__main__":
    unittest.main()
