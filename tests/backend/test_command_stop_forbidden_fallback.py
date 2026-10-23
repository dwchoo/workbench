"""C-T1b: the forbidden pids an episode last obtained are kept for the residual scan after the owner's provider fails.

Seam: an injected ``StopTarget.forbidden`` provider over a ``CommandStopService`` with a synthetic ``/proc`` table
(``SyntheticTableEnvironment``); no real process exists or is signalled. Pids, the provider's failure points and the
expected residual are this file's own.
"""
from __future__ import annotations

import os
import unittest
from uuid import uuid4

from workbench.backend.command_stop import CommandStopService, ProcStat, StopPolicy, StopTarget

from test_command_stop_hardening import SyntheticTableEnvironment
from test_command_stop_pins import BOOT_ID

POLICY = StopPolicy(identity_wait=0.2, sigint_wait=0.1, sigterm_wait=0.1, sigkill_wait=0.1, call_wait=3.0,
                    poll_interval=0.01)
HOST, SUPERVISOR, CHILD, PROTECTED = 940, 941, 942, 943
SESSION, UID = 900, os.getuid()


def stat(pid, ppid, group):
    return ProcStat(pid, "S", ppid, group, SESSION, 1000 + pid, UID)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.env = SyntheticTableEnvironment([stat(HOST, 1, HOST), stat(SUPERVISOR, HOST, 1),
                                              stat(CHILD, SUPERVISOR, CHILD), stat(PROTECTED, CHILD, CHILD)])
        self.command_id, self.journal, self.provider_fails = str(uuid4()), [], False
        self.service = CommandStopService(journal=self.record, policy=POLICY, env=self.env)
        self.service.register(StopTarget(self.command_id, "worker", "terminal", None, identity=self.identity,
                                         forbidden=self.forbidden))

    def record(self, record):
        self.journal.append(record)
        self.on_record(record)

    def on_record(self, record):
        pass

    def identity(self):
        return {"request_id": self.command_id, "phase": "running", "uid": UID, "boot_id": BOOT_ID, "session": SESSION,
                "child_group": CHILD, "parent": {"pid": HOST, "start_ticks": 1000 + HOST},
                "supervisor": {"pid": SUPERVISOR, "start_ticks": 1000 + SUPERVISOR},
                "child": {"pid": CHILD, "start_ticks": 1000 + CHILD}}

    def forbidden(self):
        if self.provider_fails:
            raise RuntimeError("the owner's provider is gone")
        return frozenset({PROTECTED})


class ForbiddenProviderFailsAtTheEndTests(Fixture):
    def on_record(self, record):
        if record.get("type") == "command_stop_finished":  # the next provider call is the one ``_finish`` makes
            self.provider_fails = True

    def test_a_pid_forbidden_during_the_stages_is_not_a_residual_member_after_the_provider_fails(self):
        result = self.service.stop("worker", self.command_id)
        self.assertEqual((result["status"], result["reason"]), ("unknown", "still_alive_after_sigkill"), result)
        self.assertTrue(self.provider_fails, "the provider failed at the end of the episode")
        self.assertNotIn(PROTECTED, [item["pid"] for item in result["residual"]["alive"]], "stage-time residual")
        self.service.command_ended(self.command_id, {"status": "exited", "exit_code": 137, "signal": None})
        corrected = self.service.stop("worker", self.command_id)  # the late correction scanned with the kept set
        self.assertEqual(corrected["status"], "exited", corrected)
        self.assertTrue(corrected["residual"]["checked"], corrected)
        self.assertIn(CHILD, [item["pid"] for item in corrected["residual"]["alive"]], "the scan itself ran")
        self.assertNotIn(PROTECTED, [item["pid"] for item in corrected["residual"]["alive"]], corrected["residual"])
        self.assertEqual([pid for pid, _signum in self.env.sent if pid == PROTECTED], [])


class ForbiddenProviderFailsBeforeAnySignalTests(Fixture):  # guard: fail closed, not a RED
    def setUp(self):
        super().setUp()
        self.provider_fails = True

    def test_a_provider_that_fails_before_the_first_signal_leaves_nothing_signalled_and_the_stop_unknown(self):
        result = self.service.stop("worker", self.command_id)
        self.assertEqual(result["status"], "unknown", result)
        self.assertTrue(result["reason"].startswith("episode_error:"), result)
        self.assertEqual(result["signals"], [])
        self.assertEqual(self.env.sent, [], "nothing was signalled")
        self.assertEqual(self.env.opened, [], "nothing was pinned")
        self.assertEqual(self.service.stop_marker(self.command_id), {})


if __name__ == "__main__":
    unittest.main()
