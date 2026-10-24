"""CS-O10: the user stops a Workbench command from the UI (``requested_by: user``).

Seam S3: ``CommandStopService`` with fake targets (the authority matrix and the audit record).
Seam S2: a real ``Backend`` with fake OMPs and a real host shell PTY (escalation, the snapshot's ``host_shell.command``
and the controller entry ``stop_command``).

Expected values are this file's own: the authority matrix of the user decision (the user may stop what the Worker
and the Manager started, nothing else is a requester), the exit codes 128 + signal of the commands the test starts,
and the command ids and owners the test registers. Every process here was started by the test in its own host shell.
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from uuid import uuid4

from workbench.backend.client import UiClient
from command_stop_support import alive, pids_running
from test_experiment_command_stop import ExperimentStopFixture
from test_task_flow import wait_until
from test_command_stop_authority import BackendStopFixture, Registry


class UserAuthorityTests(unittest.TestCase):
    """Behavior 1: the user may stop a Worker's and a Manager's command, recorded with ``requested_by: user``."""

    def accepted(self, owner, kind):
        registry = Registry()
        command_id = registry.register(owner, kind=kind, task_id=None)
        result = registry.service.stop("user", command_id, reason="user stop")
        self.assertNotEqual(result["status"], "refused", result)
        self.assertEqual(result["requested_by"], "user")
        (requested,) = registry.of_type("command_stop_requested")
        self.assertEqual((requested["command_id"], requested["requested_by"], requested["owner"],
                          requested["reason"]), (command_id, "user", owner, "user stop"))
        self.assertEqual(registry.of_type("command_stop_refused"), [])

    def test_the_user_stops_a_worker_command(self):
        self.accepted("worker", "terminal")

    def test_the_user_stops_a_manager_experiment_command(self):
        self.accepted("manager", "experiment")

    def test_a_finished_command_is_already_exited_for_the_user_and_nothing_is_signalled(self):
        registry = Registry()
        command_id = registry.register("worker")
        registry.service.command_ended(command_id, {"status": "exited", "exit_code": 7, "signal": None})
        result = registry.service.stop("user", command_id, reason="user stop")
        self.assertEqual((result["status"], result["exit_code"], result["requested_by"]), ("already_exited", 7, "user"))
        registry.assert_no_process_was_touched(self)

    def test_an_unknown_command_id_is_not_found_for_the_user_and_nothing_is_signalled(self):
        registry = Registry()
        registry.register("worker")
        result = registry.service.stop("user", str(uuid4()), reason="user stop")
        self.assertEqual((result["status"], result["reason"], result["requested_by"]),
                         ("not_found", "unknown_command", "user"), result)
        registry.assert_no_process_was_touched(self)

    def test_only_the_exact_requester_name_user_is_accepted(self):
        for requester in ("User", "user ", "operator", ""):
            registry = Registry()
            command_id = registry.register("worker")
            result = registry.service.stop(requester, command_id)
            self.assertEqual((result["status"], result["reason"]), ("refused", "not_owner"), (requester, result))
            registry.assert_no_process_was_touched(self)


class UserStopThroughTheBackendTests(BackendStopFixture):
    """Behavior 2: the controller entry ``stop_command`` stops a real command with the full escalation, as the user."""

    def test_a_sigint_ignoring_command_is_taken_down_by_sigterm_with_requester_user(self):
        started = self.run_command("trap '' INT; sleep 612.1")
        (pid,) = pids_running("sleep", "612.1")
        result = self.backend.stop_command(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"], result["signal_name"]), (128 + 15, 15, "SIGTERM"))
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT", "SIGTERM"])
        self.assertEqual((result["command_id"], result["requested_by"]), (started["command_id"], "user"))
        self.assertFalse(alive(pid), "the command's process is gone")
        self.assertEqual(self.backend.handoffs.pending_approvals(), [], "no approval was asked for")
        (requested,) = self.records("command_stop_requested")
        self.assertEqual((requested["command_id"], requested["requested_by"], requested["owner"], requested["kind"],
                          requested["reason"]), (started["command_id"], "user", "worker", "terminal", "user stop"))
        self.assertEqual(self.records("command_interrupt"), [], "a stop is not a Ctrl+C forward")

    def test_a_stale_command_id_signals_nothing(self):
        started = self.run_command("sleep 612.2")
        self.assertEqual(self.backend.stop_command(started["command_id"])["status"], "exited")
        signals = self.records("command_stop_signal")
        again = self.backend.stop_command(started["command_id"])
        self.assertEqual((again["status"], again.get("repeat")), ("exited", True), "an ended command's own result")
        unknown = self.backend.stop_command(str(uuid4()))
        self.assertEqual((unknown["status"], unknown["reason"]), ("not_found", "unknown_command"), unknown)
        self.assertEqual(self.records("command_stop_signal"), signals, "no new signal")


class UserStopOverTheUiSocketTests(BackendStopFixture):
    """Behavior 2b: ``stop_command`` through the real UiServer: an immediate ack, the final outcome and a state push in
    which the command is gone, while the backend loop keeps serving."""

    def test_the_ack_final_outcome_and_state_push_reach_an_attached_client(self):
        started = self.run_command("trap '' INT; sleep 612.3")
        made = []  # the hello handshake needs the backend loop, which this thread runs
        maker = threading.Thread(target=lambda: made.append(
            UiClient(self.backend.layout.ui_socket, name="cs-o10-test", timeout=10)), daemon=True)
        maker.start()
        self.wait(lambda: bool(made), "the ui client's welcome")
        (client,) = made
        self.addCleanup(client.close)
        results = []

        def collect():
            client.pump(0)
            while client.pending:
                frame = client.pending.pop(0)
                if frame.header.get("type") == "result":
                    results.append(frame.header)
            return results
        client.send_frame({"v": client.version, "type": "attach", "id": "a1"})
        self.wait(lambda: any(r.get("id") == "a1" for r in collect()), "the attach result")
        client.send_frame({"v": client.version, "type": "stop_command", "id": "s1",
                           "command_id": started["command_id"]})
        self.wait(lambda: any(r.get("id") == "s1" for r in collect()), "the stop ack")
        ack = next(r for r in results if r["id"] == "s1")
        self.assertEqual((ack["ok"], ack["stop"]["status"], ack["command_id"]),
                         (True, "stopping", started["command_id"]), ack)
        self.assertNotIn("final", ack)
        self.wait(lambda: any(r.get("final") for r in collect()), "the final outcome")
        final = next(r for r in results if r.get("final"))
        self.assertEqual((final["id"], final["stop"]["status"], final["stop"]["signal_name"],
                          final["stop"]["requested_by"]), ("s1", "exited", "SIGTERM", "user"), final)

        def last_state_without_command():
            client.pump(0)
            return bool(client.states) and client.states[-1]["panes"]["host_shell"]["command"] is None
        self.wait(last_state_without_command, "a pushed state without the command")
        self.assertEqual([pid for pid in pids_running("sleep", "612.3")], [])


class HostCommandSnapshotTests(BackendStopFixture):
    """Behavior 3: ``panes.host_shell.command`` names the Workbench command running in the host terminal, also one
    without a Task, so the UI can offer the stop key for it; ``None`` when there is none."""

    def host_command(self):
        return self.backend.snapshot()["panes"]["host_shell"]["command"]

    def test_a_task_less_worker_command_is_exposed_while_it_runs_and_not_after(self):
        self.assertIsNone(self.host_command())
        started = self.run_command("sleep 613.1")
        self.assertEqual(self.host_command(), {"command_id": started["command_id"], "owner": "worker",
                                               "label": "sleep 613.1", "state": "running"})
        self.assertEqual(self.backend.snapshot()["panes"]["host_shell"]["operated_by"], "worker", "kept")
        self.backend.stop_command(started["command_id"])
        self.wait(lambda: self.host_command() is None, "the ended command to leave the snapshot")

    def test_the_command_reads_stopping_while_a_stop_is_under_way(self):
        started = self.run_command("trap '' INT TERM; while :; do sleep 0.2; done")
        stopper = threading.Thread(target=self.backend.stop_command, args=(started["command_id"],), daemon=True)
        stopper.start()
        self.addCleanup(stopper.join, 30)
        self.wait(lambda: (self.host_command() or {}).get("state") == "stopping", "the command to read stopping")
        self.assertEqual(self.host_command()["command_id"], started["command_id"])

    def test_a_long_multiline_command_is_labelled_by_its_first_line_only(self):
        first = "sleep 613.3 # " + "x" * 300
        started = self.run_command(first + "\necho second-line-secret")
        label = self.host_command()["label"]
        self.assertTrue(label.startswith("sleep 613.3 # xxx"), label)
        self.assertNotIn("second-line-secret", label)
        self.assertLessEqual(len(label), 130)
        self.backend.stop_command(started["command_id"])

    def test_a_running_experiment_command_is_exposed_as_the_managers(self):
        found = {"command_id": str(uuid4()), "owner": "manager", "label": "experiment abcd1234", "state": "running"}
        with mock.patch.object(self.backend.flow, "running_host_command", return_value=found):
            self.assertEqual(self.host_command(), found)


class UserStopsAnExperimentTests(ExperimentStopFixture):
    """Behavior 4: the Manager's experiment command is listed for the stop key and a user stop closes the run as
    ``stopped`` (``stopped_by: user``), without a judgment."""

    def test_the_running_experiment_is_the_current_host_command_until_it_ends(self):
        self.assertIsNone(self.flow.running_host_command())
        _task_id, run_id = self.running_experiment()
        found = self.flow.running_host_command()
        self.assertEqual(found, {"command_id": run_id, "owner": "manager", "label": f"experiment {run_id[:8]}",
                                 "state": "running"})
        stopped = self.stop_service.stop("user", run_id, reason="user stop")
        self.assertEqual((stopped["status"], stopped["requested_by"]), ("exited", "user"), stopped)
        self.assertTrue(wait_until(lambda: self.flow.running_host_command() is None, 30))
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", 30), self.view())  # the run closes

    def test_a_user_stop_closes_the_run_as_stopped_by_the_user_without_analysis(self):
        _task_id, run_id = self.running_experiment()
        stopped = self.stop_service.stop("user", run_id, reason="user stop")
        self.assertEqual((stopped["exit_code"], stopped["signal"]), (130, 2), stopped)
        self.assertTrue(wait_until(lambda: self.view()["status"] == "finished", 30), self.view())
        self.assertEqual(self.view()["last_result"], {"run_id": run_id, "outcome": "stopped", "stopped_by": "user",
                                                       "exit_code": 130, "signal": 2, "run_closed": True})
        self.assertEqual(self.judge_calls, 0)


if __name__ == "__main__":
    unittest.main()
