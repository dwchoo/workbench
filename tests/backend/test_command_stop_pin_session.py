"""C-T1a: a pin also proves the session recorded at scan time (a process that called setsid after the scan is not signalled).

Seams: the injected ``StopEnvironment.read_stat`` (a synthetic ``/proc`` table, ``SyntheticTableEnvironment``) used
through ``ProcessPin.open`` directly and through a ``CommandStopService`` stop. Expected pids, sessions and signals are
this file's own numbers; no real process exists or is signalled.
"""
from __future__ import annotations

import os
import signal
import unittest
from uuid import uuid4

from workbench.backend.command_stop import CommandStopService, ProcessPin, ProcStat, StopTarget

from test_command_stop_hardening import SHORT, SyntheticTableEnvironment
from test_command_stop_pins import BOOT_ID

UID = os.getuid()
PID, SESSION, OTHER_SESSION = 930, 900, 931


def stat(pid=PID, session=SESSION, ppid=1, group=PID):
    return ProcStat(pid, "S", ppid, group, session, 1000 + pid, UID)


class PinSessionTests(unittest.TestCase):
    def open_pin(self, env, **kwargs):
        pin = ProcessPin.open(env, PID, 1000 + PID, uid=kwargs.pop("uid", UID), boot_id=BOOT_ID, **kwargs)
        self.addCleanup(pin.close)
        return pin

    def test_a_target_whose_session_changed_since_the_scan_is_stale_and_never_signalled(self):
        env = SyntheticTableEnvironment([stat(session=OTHER_SESSION)])
        pin = self.open_pin(env, session=SESSION)
        self.assertEqual(pin.status, "stale")
        self.assertEqual(pin.send(signal.SIGTERM), "stale")
        self.assertEqual(env.sent, [], "a pin for a process that left the session must not signal it")

    def test_a_session_change_after_the_pidfd_opened_is_also_stale_and_never_signalled(self):
        env = SyntheticTableEnvironment([stat()])
        open_fd = env.pidfd_open
        env.pidfd_open = lambda pid: (env.table.update({pid: stat(session=OTHER_SESSION)}), open_fd(pid))[1]
        pin = self.open_pin(env, session=SESSION)
        self.assertEqual(pin.status, "stale")
        self.assertEqual(pin.send(signal.SIGTERM), "stale")
        self.assertEqual(env.sent, [])
        self.assertEqual(env.fds, {}, "the pidfd of a refused pin is closed")

    def test_the_same_session_pins_and_signals_as_before(self):  # guard: not a RED, the session check is new
        env = SyntheticTableEnvironment([stat()])
        pin = self.open_pin(env, session=SESSION)
        self.assertEqual((pin.status, pin.send(signal.SIGTERM)), ("pinned", "sent"))
        self.assertEqual(env.sent, [(PID, signal.SIGTERM)])

    def test_a_pin_without_an_expected_session_keeps_the_old_contract(self):  # guard: existing direct callers
        env = SyntheticTableEnvironment([stat(session=OTHER_SESSION)])
        self.assertEqual(self.open_pin(env).status, "pinned")

    def test_a_different_uid_stays_different_uid_whatever_the_session(self):
        env = SyntheticTableEnvironment([stat(session=OTHER_SESSION)])
        pin = self.open_pin(env, uid=UID + 1, session=SESSION)
        self.assertEqual(pin.status, "different_uid")
        self.assertEqual(env.sent, [])


class ReadsThenSetsid(SyntheticTableEnvironment):
    """Reports ``pid`` as the scan saw it once, then as a process that has called setsid."""

    def __init__(self, stats, pid, moved):
        super().__init__(stats)
        self.pid, self.moved, self.reads = pid, moved, 0

    def read_stat(self, pid):
        if pid == self.pid:
            self.reads += 1
            if self.reads > 1:
                return self.moved
        return super().read_stat(pid)


class ScanRecordedSessionTests(unittest.TestCase):
    """The stop engine pins with the session its scan recorded."""

    HOST, SUPERVISOR, CHILD = 940, 941, 942
    SESSION = 900

    def member(self, pid, ppid, group, session=None):
        return ProcStat(pid, "S", ppid, group, self.SESSION if session is None else session, 1000 + pid, UID)

    def stop(self, env):
        service = CommandStopService(journal=lambda record: None, policy=SHORT, env=env)
        command_id = str(uuid4())
        identity = {"request_id": command_id, "phase": "running", "uid": UID, "boot_id": BOOT_ID,
                    "session": self.SESSION, "child_group": self.CHILD,
                    "parent": {"pid": self.HOST, "start_ticks": 1000 + self.HOST},
                    "supervisor": {"pid": self.SUPERVISOR, "start_ticks": 1000 + self.SUPERVISOR},
                    "child": {"pid": self.CHILD, "start_ticks": 1000 + self.CHILD}}
        service.register(StopTarget(command_id, "worker", "terminal", None, identity=lambda: identity,
                                    forbidden=lambda: frozenset()))
        return service.stop("worker", command_id)

    def test_a_member_that_called_setsid_between_the_scan_and_the_pin_is_never_signalled(self):
        stats = [self.member(self.HOST, 1, self.HOST), self.member(self.SUPERVISOR, self.HOST, 1),
                 self.member(self.CHILD, self.SUPERVISOR, self.CHILD)]
        env = ReadsThenSetsid(stats, self.CHILD, self.member(self.CHILD, self.SUPERVISOR, self.CHILD, 950))
        self.stop(env)
        self.assertEqual([pid for pid, _signum in env.sent if pid == self.CHILD], [])
        self.assertGreaterEqual(env.reads, 2, "the pin re-read the member after the scan")


if __name__ == "__main__":
    unittest.main()
