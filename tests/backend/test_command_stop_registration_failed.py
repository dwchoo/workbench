"""A failed stop registration is recorded and answered, never silent (user decision 2026-10-23).

The command keeps running and is followed and judged normally; it is not stoppable.
B1a one ``command_stop_register_failed`` audit record (command id, kind, owner, error type name only).
B1b a later stop answers ``refused: registration_failed`` (no signal, not previous_backend_command, not an end).
B1c no partial state: an ended command whose registration failed is not reported ended to the stop service.

Seams: the real ``CommandStopService`` with a failing ``register`` (``CapturingStop``), the real terminal/flow owners
of the existing fixtures, and the ``register_target`` helper. Expected values are this file's own strings.
"""

from __future__ import annotations

import json
from uuid import uuid4
import unittest
from unittest import mock
from unittest.mock import Mock

from workbench.backend.command_stop import FAILED_KEPT, CommandStopService, StopTarget
from workbench.backend.command_stop_owner import register_target
from workbench.backend.service import Backend
from workbench.contracts.v1 import PaneId

from test_command_stop_owner_followups import CapturingStop, FailingRegisterTerminal
from test_command_stop_terminal import REDUCED
from test_experiment_command_stop import ExperimentStopFixture
from test_flow_terminal import journal, wait_until
from test_task_flow import wait_until as flow_wait_until

SECRET_MESSAGE = "the stop registry is down"  # the message CapturingStop raises: it must never reach a record


def register_failed(records):
    return [record for record in records if record.get("type") == "command_stop_register_failed"]


class ServiceAnswerTests(unittest.TestCase):
    def failed(self, owner="worker", kind="terminal", previous=True):
        records = []
        stop = CapturingStop(journal=records.append, fail_register=True, previous_backend=lambda _id: previous)
        self.addCleanup(stop.close)
        command_id = str(uuid4())
        self.assertFalse(register_target(stop, StopTarget(command_id, owner, kind, None, identity=lambda: None)))
        return stop, records, command_id

    def test_a_manager_stop_is_refused_as_registration_failed_not_as_a_previous_backends_command(self):
        stop, records, command_id = self.failed()
        answer = stop.stop("manager", command_id)
        self.assertEqual((answer["status"], answer["reason"]), ("refused", "registration_failed"), answer)
        self.assertEqual(answer["command_id"], command_id)
        self.assertTrue(answer.get("detail"))
        self.assertNotIn("signals", answer)
        refusals = [r for r in records if r["type"] == "command_stop_refused"]
        self.assertEqual([(r["status"], r["reason"]) for r in refusals], [("refused", "registration_failed")])

    def test_a_worker_stop_of_its_own_command_gets_the_same_answer(self):
        stop, _records, command_id = self.failed()
        answer = stop.stop("worker", command_id)
        self.assertEqual((answer["status"], answer["reason"]), ("refused", "registration_failed"), answer)

    def test_a_worker_may_not_learn_about_a_manager_command(self):
        stop, _records, command_id = self.failed(owner="manager", kind="experiment")
        answer = stop.stop("worker", command_id)
        self.assertEqual((answer["status"], answer["reason"]), ("refused", "not_owner"), answer)

    def test_the_answer_holds_after_the_failure_record_is_forgotten_and_is_not_a_previous_backends(self):
        stop, _records, command_id = self.failed()
        for _ in range(FAILED_KEPT + 1):
            register_target(stop, StopTarget(str(uuid4()), "worker", "terminal", None, identity=lambda: None))
        answer = stop.stop("manager", command_id)
        self.assertEqual((answer["status"], answer["reason"]), ("not_found", "unknown_command"), answer)

    def test_the_failures_remembered_are_bounded(self):
        stop, _records, _command_id = self.failed()
        for _ in range(FAILED_KEPT * 2):
            register_target(stop, StopTarget(str(uuid4()), "worker", "terminal", None, identity=lambda: None))
        self.assertLessEqual(len(stop._failed), FAILED_KEPT)

    def test_a_service_whose_failure_notice_also_raises_still_answers_false(self):
        stop = CapturingStop(journal=lambda _r: None, fail_register=True)
        self.addCleanup(stop.close)
        with mock.patch.object(stop, "registration_failed", side_effect=RuntimeError("x")):
            self.assertFalse(register_target(stop, StopTarget(str(uuid4()), "worker", "terminal", None,
                                                              identity=lambda: None)))


class TerminalAuditTests(FailingRegisterTerminal):
    def test_a_failed_registration_writes_one_audit_record_with_the_error_type_only(self):
        started = self.run_command("sleep 614.1")
        self.assertEqual(started["status"], "running", started)
        records = register_failed(journal(self.root))
        self.assertEqual(len(records), 1, records)
        record = records[0]
        self.assertEqual((record["command_id"], record["kind"], record["owner"], record["error"]),
                         (started["command_id"], "terminal", "worker", "RuntimeError"), record)
        self.assertTrue(record.get("at"))
        self.assertNotIn(SECRET_MESSAGE, json.dumps(record))
        self.user_types(b"\x03")
        self.assertTrue(wait_until(lambda: self.query().get("status") != "running", 10))

    def test_a_worker_stop_is_answered_registration_failed_and_signals_nothing(self):
        started = self.run_command("sleep 614.2")
        answer = self.stop(started["command_id"])
        self.assertEqual((answer["status"], answer["reason"]), ("refused", "registration_failed"), answer)
        self.assertEqual(self.command_stop.stop_marker(started["command_id"]), {})
        self.assertEqual(self.query()["status"], "running", "the command keeps running")
        self.user_types(b"\x03")
        self.assertTrue(wait_until(lambda: self.query().get("status") != "running", 10))

    def test_the_answer_survives_the_commands_end(self):
        started = self.run_command("sleep 614.3")
        self.user_types(b"\x03")
        self.assertTrue(wait_until(lambda: self.query().get("status") != "running", 10))
        answer = self.command_stop.stop("manager", started["command_id"])
        self.assertEqual((answer["status"], answer["reason"]), ("refused", "registration_failed"), answer)
        self.assertNotIn("exit_code", answer)


class GatedFlow(ExperimentStopFixture):
    """An experiment whose command runs until the test opens its gate file, then passes."""

    def new_stop_service(self, env):
        return CapturingStop(journal=self.records.append, policy=REDUCED, env=env, fail_register=True)

    def gated(self):
        gate = self.root / "gate"
        return gate, (f"while [ ! -e {gate} ]; do sleep 0.1; done; printf 'PASS from host\\n'; "
                      "printf PASS > outcome.txt")

    def finish(self, gate):
        gate.touch()
        self.assertTrue(flow_wait_until(lambda: self.view()["status"] == "finished", 30), self.view())


class FlowAuditTests(GatedFlow):
    def test_a_failed_registration_writes_one_audit_record_with_the_error_type_only(self):
        gate, command = self.gated()
        task_id, run_id = self.running_experiment(command)
        records = register_failed(self.records)
        self.assertEqual(len(records), 1, records)
        record = records[0]
        self.assertEqual((record["command_id"], record["kind"], record["owner"], record["error"]),
                         (run_id, "experiment", "manager", "RuntimeError"), record)
        self.assertTrue(record.get("at"))
        self.assertNotIn(SECRET_MESSAGE, json.dumps(record))
        self.finish(gate)

    def test_a_manager_stop_is_answered_registration_failed_and_signals_nothing(self):
        gate, command = self.gated()
        task_id, run_id = self.running_experiment(command)
        answer = self.stop_service.stop("manager", run_id)
        self.assertEqual((answer["status"], answer["reason"]), ("refused", "registration_failed"), answer)
        self.assertEqual((self.env.opened, self.env.sent), ([], []))
        self.finish(gate)
        self.assertEqual(self.judge_calls, 1, "followed and judged normally")

    def test_the_running_command_stays_listed_and_the_ended_one_is_neither_reported_nor_listed(self):
        gate, command = self.gated()
        task_id, run_id = self.running_experiment(command)
        listed = self.view()["host_commands"]
        self.assertEqual([(item["command_id"], item["kind"], item["owner"], item["state"]) for item in listed],
                         [(run_id, "experiment", "manager", "running")], "it is really running: the user sees it")
        self.finish(gate)
        self.assertEqual(self.view()["host_commands"], [])
        answer = self.stop_service.stop("manager", run_id)
        self.assertEqual((answer["status"], answer["reason"]), ("refused", "registration_failed"), answer)
        self.assertNotIn("ended_at", answer, "the run was never reported ended to the stop service")


    def test_a_user_ctrl_c_is_journaled_with_the_run_id_of_a_command_that_could_not_be_registered(self):
        gate, command = self.gated()
        task_id, run_id = self.running_experiment(command)
        backend = Backend.__new__(Backend)  # built without starting a backend, as the CS-07 audit tests do
        backend.panes = {PaneId.HOST_SHELL: self.pane}
        backend.handoffs = Mock()
        backend.terminal = Mock()
        backend.terminal.summary_command_active.return_value = False  # a Worker command is not what ran
        backend.flow = self.flow
        backend._wire_shell(self.pane)
        self.assertIsNone(backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        interrupts = [call.args[0] for call in backend.handoffs.record.call_args_list
                      if call.args[0]["type"] == "command_interrupt"]
        self.assertEqual([(r["requested_by"], r["command_id"]) for r in interrupts], [("user", run_id)])
        self.assertTrue(flow_wait_until(lambda: self.view()["status"] in ("finished", "held", "cancelled"), 30),
                        self.view())
        # the lookup never reaches the stop service: still no ended record, the stop answer is unchanged
        answer = self.stop_service.stop("manager", run_id)
        self.assertEqual((answer["status"], answer["reason"]), ("refused", "registration_failed"), answer)


class RegisterTargetTests(unittest.TestCase):
    def test_it_answers_true_when_the_service_registered_the_target(self):
        stop = CommandStopService(journal=lambda _r: None)
        self.addCleanup(stop.close)
        target = StopTarget(str(uuid4()), "worker", "terminal", None, identity=lambda: None)
        self.assertTrue(register_target(stop, target))

    def test_it_answers_false_without_a_service(self):
        self.assertFalse(register_target(None, StopTarget(str(uuid4()), "worker", "terminal", None,
                                                          identity=lambda: None)))


if __name__ == "__main__":
    unittest.main()
