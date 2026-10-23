"""C-T3: the owner adapters of the command-stop feature (VERIFICATION.md section 9 follow-ups).

B1 a failing stop registration never changes the flow: the command is still followed to its end and reported.
B2 the owners hand the stop service an explicit ``forbidden`` set (the OMP panes' pids).
B3 one command-id pattern: the Manager's ``stop_command`` refuses the ids the terminal stop refuses.

Seams: the owners' public constructors/attributes and the service's ``register`` call (a ``CapturingStop`` records the
``StopTarget`` an owner offered), a real ``Backend`` with fake OMPs for the wiring, and the two argument validators.
Expected values are this file's own: the pids the fake OMPs recorded, the uuids written here, the exit code 0 of
``true``-like commands the test starts. Every process is one the test started in its own host shell.
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from workbench.backend.command_stop import CommandStopService
from workbench.backend.flow_recovery import validate_stop_command_arguments
from workbench.backend.flow_terminal import TerminalService, validate_terminal_arguments
from workbench.workflow.run import WorkflowRun

from test_command_stop_authority import BackendStopFixture
from test_command_stop_terminal import REDUCED, RealStopFixture
from test_experiment_command_stop import ExperimentStopFixture
from test_flow_terminal import AUTOMATION, SECRET, journal, wait_until
from test_task_flow import wait_until as flow_wait_until


class CapturingStop(CommandStopService):
    """The real service that remembers every target offered to it and can refuse the registration."""

    def __init__(self, *args, fail_register: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.offered: list = []
        self.fail_register = fail_register

    def register(self, target):
        self.offered.append(target)
        if self.fail_register:
            raise RuntimeError("the stop registry is down")
        return super().register(target)


class FailingRegisterTerminal(RealStopFixture):
    """A real host shell and TerminalService whose stop registration always fails."""

    def make_terminal(self, host_shell, **extra):
        previous = getattr(self, "command_stop", None)
        if previous is not None:
            previous.close()
        self.command_stop = CapturingStop(journal=self.handoffs.record, policy=REDUCED, env=self.stop_env,
                                          fail_register=True)
        return TerminalService(handoffs=self.handoffs, host_shell=host_shell, gate=self.gate,
                               log_root=self.root / "workflow" / "terminal",
                               automation=lambda: AUTOMATION, paused=lambda: self.paused,
                               activity=lambda: self.activity, sensitive_values=lambda: (SECRET,),
                               poll_interval=0.02, command_stop=self.command_stop, **extra)


class TerminalRegistrationFailureTests(FailingRegisterTerminal):
    def test_a_failed_registration_still_runs_follows_and_reports_the_command(self):
        result = self.run_command("echo b1-terminal-ok", wait=10)
        self.assertEqual((result["status"], result.get("exit_code")), ("exited", 0), result)
        self.assertEqual(len(self.command_stop.offered), 1, "the registration was attempted once")
        types = [record["type"] for record in journal(self.root)
                 if record.get("command_id") == result["command_id"]]
        self.assertIn("terminal_started", types)
        self.assertIn("terminal_ended", types)
        self.assert_user_owns_the_shell_again()

    def test_such_a_command_is_not_stoppable_and_a_stop_does_not_signal(self):
        started = self.run_command("sleep 612.7")
        self.assertEqual(started["status"], "running", started)
        result = self.stop(started["command_id"])
        self.assertNotEqual(result["status"], "exited", result)
        self.assertEqual(self.command_stop.stop_marker(started["command_id"]), {})
        # the command is the test's own: end it the plain way (Ctrl-C typed as the user)
        self.user_types(b"\x03")
        self.assertTrue(wait_until(lambda: self.query().get("status") != "running", 10))


class FlowRegistrationFailureTests(ExperimentStopFixture):
    QUICK = "printf 'PASS from host\\n'; printf PASS > outcome.txt"

    def new_stop_service(self, env):
        return CapturingStop(journal=self.records.append, policy=REDUCED, env=env, fail_register=True)

    def test_a_failed_registration_still_follows_the_experiment_to_its_end(self):
        task_id, run_id = self.running_experiment(self.QUICK)
        self.assertTrue(flow_wait_until(lambda: self.view()["status"] == "finished", 30), self.view())
        self.assertEqual(self.judge_calls, 1, "the run was followed and judged")
        self.assertEqual(self.view()["host_commands"], [], "nothing stays listed once the command ended")
        self.assertEqual([target.command_id for target in self.stop_service.offered], [run_id])
        self.assertEqual((self.env.opened, self.env.sent), ([], []), "nothing was pinned or signalled")


class ForbiddenPidsTerminalTests(RealStopFixture):
    """B2 (terminal owner): the registered target lists the pids the owner was given."""

    def make_stop_env(self):
        return None

    def make_terminal(self, host_shell, **extra):
        previous = getattr(self, "command_stop", None)
        if previous is not None:
            previous.close()
        self.command_stop = CapturingStop(journal=self.handoffs.record, policy=REDUCED, env=self.stop_env)
        return TerminalService(handoffs=self.handoffs, host_shell=host_shell, gate=self.gate,
                               log_root=self.root / "workflow" / "terminal",
                               automation=lambda: AUTOMATION, paused=lambda: self.paused,
                               activity=lambda: self.activity, sensitive_values=lambda: (SECRET,),
                               poll_interval=0.02, command_stop=self.command_stop,
                               forbidden=lambda: frozenset({41001, 41002}), **extra)

    def test_the_registered_target_forbids_the_pids_the_owner_was_given(self):
        started = self.run_command("sleep 612.8")
        self.assertEqual(started["status"], "running", started)
        (target,) = self.command_stop.offered
        self.assertEqual(target.forbidden(), frozenset({41001, 41002}))
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")


class ForbiddenPidsFlowTests(ExperimentStopFixture):
    """B2 (experiment owner)."""

    def new_stop_service(self, env):
        return CapturingStop(journal=self.records.append, policy=REDUCED, env=env)

    def test_the_registered_target_forbids_the_pids_the_flow_was_given(self):
        self.flow.stop_forbidden = lambda: frozenset({41003})
        task_id, run_id = self.running_experiment()
        (target,) = self.stop_service.offered
        self.assertEqual(target.command_id, run_id)
        self.assertEqual(target.forbidden(), frozenset({41003}))
        self.assertEqual(self.stop_service.stop("manager", run_id)["status"], "exited")
        self.assertTrue(flow_wait_until(lambda: self.view()["status"] == "finished", 30), self.view())


class ForbiddenPidsBackendTests(BackendStopFixture):
    """B2 (wiring): both owners of a real Backend forbid the OMP panes' pids."""

    def omp_pids(self):
        return {entry["pid"] for entry in self.invocations()}

    def test_a_terminal_command_forbids_the_omp_pane_pids(self):
        offered = []
        real = self.backend.command_stop.register
        self.backend.command_stop.register = lambda target: (offered.append(target), real(target))[1]
        self.run_command("sleep 612.9")
        (target,) = offered
        pids = self.omp_pids()
        self.assertEqual(len(pids), 2, "the fake manager and worker OMPs")
        self.assertTrue(pids <= set(target.forbidden()), (pids, target.forbidden()))

    def test_the_experiment_flow_forbids_the_omp_pane_pids(self):
        self.assertTrue(self.omp_pids() <= set(self.backend.flow.stop_forbidden()))
        self.assertEqual(len(self.omp_pids()), 2)


class CommandIdPatternTests(unittest.TestCase):
    VALID = ("0f8fad5b-d9cb-469f-a165-70867728950e", "0f8fad5b-d9cb-169f-8165-70867728950e",
             "0f8fad5b-d9cb-869f-b165-70867728950e")
    INVALID = ("0f8fad5b-d9cb-969f-a165-70867728950e",  # version 9
               "0f8fad5b-d9cb-069f-a165-70867728950e",  # version 0
               "0f8fad5b-d9cb-469f-c165-70867728950e",  # variant c
               "00000000-0000-0000-0000-000000000000",
               "0F8FAD5B-D9CB-469F-A165-70867728950E")

    def manager_errors(self, command_id):
        return validate_stop_command_arguments({"command_id": command_id, "reason": "x"})

    def terminal_errors(self, command_id):
        return validate_terminal_arguments({"command": None, "stop": command_id})

    def test_the_manager_refuses_the_ids_the_terminal_refuses(self):
        for command_id in self.INVALID:
            self.assertTrue(self.terminal_errors(command_id), command_id)
            errors = self.manager_errors(command_id)
            self.assertTrue(any(error.startswith("command_id") for error in errors), (command_id, errors))
            self.assertNotIn(command_id, " ".join(errors), "the value sent is never echoed")

    def test_both_accept_the_ids_the_bridge_accepts(self):
        for command_id in (*self.VALID, str(uuid4())):
            self.assertEqual(self.terminal_errors(command_id), [], command_id)
            self.assertEqual(self.manager_errors(command_id), [], command_id)


class ManagerRefusesAnOutOfPatternIdTests(BackendStopFixture):
    def test_a_version_9_id_is_an_invalid_argument_and_nothing_is_signalled(self):
        started = self.run_command("sleep 613.1")
        result = self.manager_stop("0f8fad5b-d9cb-969f-a165-70867728950e")
        self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), result)
        self.assertEqual(self.records("command_stop_requested", "command_stop_signal"), [])
        self.assertEqual(self.manager_stop(started["command_id"])["status"], "exited")


class RunShellRequestIdTests(unittest.TestCase):
    """R2: the run offers the host shell request id it recorded through a read-only accessor."""

    def run_with(self, record):
        run = object.__new__(WorkflowRun)  # the accessor reads the record only
        run._record = record
        return run

    def test_the_recorded_request_id_is_offered(self):
        request_id = str(uuid4())
        self.assertEqual(self.run_with({"shell_request_id": request_id}).shell_request_id, request_id)

    def test_nothing_recorded_or_not_a_string_reads_as_none(self):
        for record in ({}, {"shell_request_id": None}, {"shell_request_id": 7}):
            self.assertIsNone(self.run_with(record).shell_request_id, record)

    def test_it_is_read_only(self):
        run = self.run_with({})
        with self.assertRaises(AttributeError):
            run.shell_request_id = "x"


if __name__ == "__main__":
    unittest.main()
