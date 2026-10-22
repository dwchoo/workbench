"""CS-08: cancelling a free-work Task shows the still-running host command; cancel never stops it.

Seam S1: a ``TaskFlow`` built by the ``test_task_flow`` fixture plus a real ``TerminalService`` running a real
PTY command (a ``ShellPane`` + ``HostShellPort``, as ``test_flow_terminal`` does). Nothing is mocked on the
command side; ``TaskFlow.host_commands`` is wired to ``TerminalService.commands_for_task`` the way ``Backend`` does.

Expected values are this test's own: the Task and command it created (ids, ``sleep`` arguments it chose), the
state words of the command-stop spec (``running``/``stopping``), and the BRIEF wording requirement that a cancel
response never says a command was stopped or terminated.

Every process here is one the test started inside its own host shell; the pane is closed (with its process
group) on teardown.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from workbench.backend.command_stop import CommandStopService, StopPolicy
from workbench.backend.flow_terminal import HostGate, TerminalService
from workbench.backend.launcher import LaunchPlan
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.service import Backend
from workbench.contracts.v1 import ActorRole
from workbench.terminal.shell_g2.prototype import ShellChoice

from test_flow_terminal import AUTOMATION, call, wait_until
from test_task_flow import FlowFixture
import test_restart_pane as restart_fixtures

REDUCED = StopPolicy(identity_wait=2.0, sigint_wait=2.0, sigterm_wait=1.0, sigkill_wait=1.0, call_wait=8.0)
FORBIDDEN = ("stopped", "terminated", "killed", "종료됨", "종료되", "ended", "exited", "finished")
HOST_COMMAND_KEYS = {"command_id", "kind", "owner", "state", "started_at", "log_path", "stop"}


class CancelHostCommandFixture(FlowFixture):
    """A free-work Task flow whose worker runs commands in a real host shell."""

    CHOICE = ShellChoice("bash", "/usr/bin/bash")

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self.pane = ShellPane(self.CHOICE, {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8"})
        self.halt = threading.Event()
        self.loop = threading.Thread(target=self._backend_loop, daemon=True)
        self.loop.start()
        self.command_stop = CommandStopService(journal=self.service.record, policy=REDUCED)
        self.terminal = TerminalService(
            handoffs=self.service, host_shell=lambda: HostShellPort(self.pane, lambda: self.pane), gate=HostGate(),
            log_root=self.root / "workflow" / "terminal", automation=lambda: AUTOMATION,
            active_task=self.flow.active_task, poll_interval=0.02, command_stop=self.command_stop)
        self.flow.host_commands = self.terminal.commands_for_task
        self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        self.assertIsNone(self.pane.admit(f"cd {self.home}\r".encode()))
        self.assertTrue(wait_until(lambda: self.pane.cwd() == str(self.home), 5))

    def _backend_loop(self):
        while not self.halt.is_set():
            self.pane.pump()
            time.sleep(0.01)

    def tearDown(self):
        self.terminal.close()
        self.command_stop.close()
        self.halt.set()
        self.loop.join(5)
        self.pane.close()
        super().tearDown()

    def run_command(self, command, wait=1):
        self.terminal._wait_seconds = wait
        return self.terminal.handle(ActorRole.WORKER, call({"command": command}))

    def stop(self, command_id):
        return self.terminal.handle(ActorRole.WORKER, call({"command": None, "stop": command_id}))

    def cancel(self, task_id, message="the user changed plans"):
        return self.to_worker({"kind": "work", "message": message, "cancel": True, "task_id": task_id})


class CancelShowsRunningCommandTests(CancelHostCommandFixture):
    def test_cancelling_a_task_with_a_running_command_lists_it_and_says_cancel_does_not_stop_it(self):
        # Behavior 1
        work = self.running_work()
        started = self.run_command("sleep 701.1")
        self.assertEqual(started["status"], "running", started)

        result = self.cancel(work["task_id"])

        self.assertEqual((result["status"], result["task_id"]), ("cancelled", work["task_id"]), result)
        self.assertEqual(len(result["host_commands"]), 1, result)
        item = result["host_commands"][0]
        self.assertEqual(set(item), HOST_COMMAND_KEYS)
        self.assertEqual((item["command_id"], item["kind"], item["owner"], item["state"]),
                         (started["command_id"], "terminal", "worker", "running"))
        self.assertEqual(item["log_path"], started["log_path"])
        self.assertTrue(item["started_at"])
        self.assertIsNone(item["stop"])
        note = result["host_command_note"]
        self.assertIn("Cancel does not stop the command", note)
        self.assertIn("terminal", note)
        self.assertIn("stop_command", note)
        text = json.dumps(result).lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, text)
        self.assertTrue(self.terminal.current()["running"], "the cancel did not touch the command")
        self.assertEqual(self.stop(started["command_id"])["status"],
                         "exited")  # the test ends its own command

    def test_cancelling_a_task_without_a_command_lists_none_and_adds_no_note(self):
        # Behavior 2
        work = self.running_work()
        result = self.cancel(work["task_id"])
        self.assertEqual(result["status"], "cancelled", result)
        self.assertEqual(result["host_commands"], [])
        self.assertNotIn("host_command_note", result)

    def test_cancelling_a_task_whose_command_already_ended_lists_none_and_adds_no_note(self):
        # Behavior 2
        work = self.running_work()
        done = self.run_command("echo finished-before-cancel; exit 3", wait=30)
        self.assertEqual((done["status"], done["exit_code"]), ("exited", 3), done)
        result = self.cancel(work["task_id"])
        self.assertEqual(result["status"], "cancelled", result)
        self.assertEqual(result["host_commands"], [])
        self.assertNotIn("host_command_note", result)


class RepeatedCancelShowsRunningCommandTests(CancelHostCommandFixture):
    """A cancel of an already closed Task is still ``rejected: task_closed``, and now also lists the commands that
    still run (user-approved change of 2026-10-22; additive fields only)."""

    def test_a_repeated_cancel_lists_the_command_that_is_still_running(self):
        work = self.running_work()
        started = self.run_command("sleep 701.2")
        self.assertEqual(started["status"], "running", started)
        first = self.cancel(work["task_id"])
        self.assertEqual(first["status"], "cancelled", first)

        again = self.cancel(work["task_id"])

        self.assertEqual((again["status"], again["reason"], again["closed_reason"]),
                         ("rejected", "task_closed", "cancelled"), again)
        self.assertEqual(again["host_commands"], first["host_commands"])
        self.assertEqual([item["command_id"] for item in again["host_commands"]], [started["command_id"]])
        self.assertNotIn("host_commands_unavailable", again)
        self.assertEqual(again["host_command_note"], first["host_command_note"])
        self.assertTrue(self.terminal.current()["running"], "the repeated cancel did not touch the command")
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")

    def test_a_repeated_cancel_after_the_command_ended_lists_none_and_adds_no_note(self):
        work = self.running_work()
        started = self.run_command("sleep 701.3")
        self.assertEqual(self.cancel(work["task_id"])["status"], "cancelled")
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")
        again = self.cancel(work["task_id"])
        self.assertEqual((again["status"], again["reason"]), ("rejected", "task_closed"), again)
        self.assertEqual(again["host_commands"], [])
        self.assertNotIn("host_command_note", again)

    def test_a_repeated_cancel_of_a_task_that_never_ran_a_command_lists_none(self):
        work = self.running_work()
        self.assertEqual(self.cancel(work["task_id"])["status"], "cancelled")
        again = self.cancel(work["task_id"])
        self.assertEqual((again["status"], again["reason"]), ("rejected", "task_closed"), again)
        self.assertEqual(again["host_commands"], [])
        self.assertNotIn("host_command_note", again)

    def test_a_follow_up_to_a_closed_task_stays_a_plain_rejection(self):
        work = self.running_work()
        self.assertEqual(self.cancel(work["task_id"])["status"], "cancelled")
        follow_up = self.to_worker({"kind": "work", "message": "more", "task_id": work["task_id"]})
        self.assertEqual((follow_up["status"], follow_up["reason"]), ("rejected", "task_closed"), follow_up)
        self.assertNotIn("host_commands", follow_up)


class TaskViewShowsCommandAfterCancelTests(CancelHostCommandFixture):
    def view_commands(self):
        return self.flow.task_view()["host_commands"]

    def test_the_closed_task_still_shows_the_running_command_until_it_ends(self):
        # Behavior 3
        work = self.running_work()
        started = self.run_command("sleep 702.1")
        self.assertEqual(started["status"], "running", started)
        self.assertEqual([c["state"] for c in self.view_commands()], ["running"], "shown while the Task is open")
        self.cancel(work["task_id"])

        view = self.flow.task_view()
        self.assertEqual((view["status"], view["closed_reason"]), ("closed", "cancelled"))
        self.assertEqual([(c["command_id"], c["state"]) for c in view["host_commands"]],
                         [(started["command_id"], "running")])
        snapshot = self.controller.snapshot()["task"]
        self.assertEqual([c["state"] for c in snapshot["host_commands"]], ["running"], "/task shows it too")
        status = self.flow.status_view(work["task_id"])
        self.assertEqual([c["command_id"] for c in status["host_commands"]], [started["command_id"]])

        self.assertNotIn("host_commands", json.dumps(self.ledger()), "computed when asked, never written")
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")  # the test ends its own command
        self.assertTrue(wait_until(lambda: self.view_commands() == [], 5), self.view_commands())
        self.assertEqual(self.flow.task_view()["status"], "closed")
        self.assertEqual(self.flow.status_view(work["task_id"])["host_commands"], [])

    def test_a_command_being_stopped_reads_as_stopping_on_the_closed_task(self):
        # Behavior 3
        work = self.running_work()
        started = self.run_command("trap 'sleep 1; exit 130' INT; echo ready; while :; do sleep 0.2; done")
        self.assertEqual(started["status"], "running", started)
        self.cancel(work["task_id"])
        box = {}
        thread = threading.Thread(target=lambda: box.update(result=self.stop(started["command_id"])), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)

        self.assertTrue(wait_until(lambda: [c["state"] for c in self.view_commands()] == ["stopping"], 5),
                        self.view_commands())
        item = self.view_commands()[0]
        self.assertEqual(item["command_id"], started["command_id"])
        self.assertIsInstance(item["stop"], dict)
        self.assertEqual(self.flow.task_view()["status"], "closed")
        thread.join(10)
        self.assertEqual(box["result"]["status"], "exited", box)
        self.assertTrue(wait_until(lambda: self.view_commands() == [], 5))


class CommandWithoutTaskTests(CancelHostCommandFixture):
    def test_a_command_without_a_task_is_in_no_tasks_host_commands(self):
        # Behavior 4
        loose = self.run_command("sleep 703.1")  # no Task is open: task_id null
        self.assertEqual(loose["status"], "running", loose)
        work = self.running_work()
        self.assertEqual(self.flow.task_view()["host_commands"], [])
        self.assertEqual(self.flow.status_view(work["task_id"])["host_commands"], [])
        self.assertEqual(self.flow.status_view()["host_commands"], [])
        result = self.cancel(work["task_id"])
        self.assertEqual(result["host_commands"], [])
        self.assertNotIn("host_command_note", result)
        self.assertEqual(self.stop(loose["command_id"])["status"], "exited")  # the test ends its own command

    def test_a_command_of_a_cancelled_task_is_not_listed_under_the_next_task(self):
        # Behavior 4
        first = self.running_work()
        started = self.run_command("sleep 704.1")
        self.cancel(first["task_id"])
        second = self.running_work("a second job")
        self.assertNotEqual(second["task_id"], first["task_id"])
        self.assertEqual(self.flow.task_view()["task_id"], second["task_id"])
        self.assertEqual(self.flow.task_view()["host_commands"], [])
        self.assertEqual([c["command_id"] for c in self.flow.status_view(first["task_id"])["host_commands"]],
                         [started["command_id"]])
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")


class HostCommandsUnavailableTests(FlowFixture):
    """The list cannot be read (no provider wired, or it fails): ``[]`` plus an explicit marker, never a bare
    ``[]`` that reads as "nothing runs". A readable list carries no marker."""

    def views(self, task_id):
        return (self.flow.task_view(), self.flow.status_view(task_id), self.cancel_result)

    def cancel_work(self):
        work = self.running_work()
        self.cancel_result = self.to_worker({"kind": "work", "message": "stop it", "cancel": True,
                                             "task_id": work["task_id"]})
        return work["task_id"]

    def assert_unavailable(self, task_id):
        for surface in self.views(task_id):
            self.assertEqual(surface["host_commands"], [], surface)
            self.assertIs(surface["host_commands_unavailable"], True, surface)
            self.assertNotIn("host_command_note", surface)

    def test_without_a_provider_every_surface_says_the_list_is_unavailable(self):
        self.assertIsNone(self.flow.host_commands)
        self.assert_unavailable(self.cancel_work())

    def test_a_failing_provider_is_marked_unavailable_not_empty(self):
        def failing(task_id):
            raise RuntimeError("terminal gone")
        self.flow.host_commands = failing
        self.assert_unavailable(self.cancel_work())

    def test_a_repeated_cancel_of_the_closed_task_says_the_list_is_unavailable_too(self):
        task_id = self.cancel_work()
        self.flow.host_commands = lambda task_id: 1 / 0
        again = self.to_worker({"kind": "work", "message": "again", "cancel": True, "task_id": task_id})
        self.assertEqual((again["status"], again["reason"]), ("rejected", "task_closed"), again)
        self.assertEqual(again["host_commands"], [])
        self.assertIs(again["host_commands_unavailable"], True, again)
        self.assertNotIn("host_command_note", again)

    def test_a_readable_empty_list_has_no_marker(self):
        self.flow.host_commands = lambda task_id: []
        task_id = self.cancel_work()
        for surface in self.views(task_id):
            self.assertEqual(surface["host_commands"], [])
            self.assertNotIn("host_commands_unavailable", surface)


class BackendWiringTests(unittest.TestCase):
    """``Backend`` hands the flow the terminal service's ``commands_for_task`` (the real wiring, fake OMPs)."""

    def test_the_backends_flow_reads_its_terminals_commands(self):
        directory = tempfile.TemporaryDirectory(prefix="cs08-backend-")
        self.addCleanup(directory.cleanup)  # runs last: after the backend is closed
        root = Path(directory.name)
        project, home = root / "p", root / "h"
        project.mkdir()
        home.mkdir()
        fake = root / "omp"
        fake.write_text(restart_fixtures.FAKE_OMP.format(python=sys.executable))
        fake.chmod(0o700)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
               "FAKE_PANE_RECORD": str(root / "panes.jsonl"), "PI_AUTO_QA": "1"}
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), str(fake), "omp/18.4.4", "/x/bridge.ts")
        layout = DataLayout(root / "d")
        ensure_private_dir(layout.root)

        def isolation_ok(command, **kwargs):
            return {"role": kwargs["role"], **restart_fixtures.OK}
        patcher = mock.patch("workbench.backend.service.check_isolation", isolation_ok)
        patcher.start()
        self.addCleanup(patcher.stop)
        backend = Backend(layout, plan, project_dir=str(project), environment=env)
        self.addCleanup(backend._close)
        backend._open()

        self.assertIsNotNone(backend.terminal)
        self.assertIsNotNone(backend.flow)
        self.assertEqual(backend.flow.host_commands, backend.terminal.commands_for_task)


if __name__ == "__main__":
    unittest.main()
