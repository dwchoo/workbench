"""CS-10 final-review hardening of the per-command stop.

Seams: S2 (a real ``Backend`` with fake OMPs through ``_tool_request``), S3 (``CommandStopService`` with fake
targets), S6/S7 (``ProcessPin`` and a real ``TerminalService`` over a real host shell with an injected
``StopEnvironment``).

Expected values are this file's own: the number of journal reads the test counts, the errno it injects, the timestamp
shape it spells out, the call keys and ids it sends. Every process here is one the test started in its own host shell
or outside it; each is killed and reaped on cleanup.
"""
from __future__ import annotations

import errno
import gc
import os
import signal
import subprocess
import threading
import time
import types
import unittest
import weakref
from collections.abc import Mapping
from uuid import uuid4

from workbench.backend.command_stop import (CommandStopService, LinuxStopEnvironment, ProcessPin, ProcStat,
                                            StopEnvironment, StopPolicy, StopTarget)
from workbench.backend.flow_terminal import TerminalService
from workbench.contracts.v1 import ActorRole

import test_command_stop_authority as authority
from test_command_stop_escalation import EscalationFixture, POLICY
from test_command_stop_pins import BOOT_ID
from command_stop_support import alive as proc_exists, pids_running, proc_stat
from test_flow_terminal import call, journal, wait_until

SHORT = StopPolicy(identity_wait=0.2, sigint_wait=0.2, sigterm_wait=0.2, sigkill_wait=0.2, call_wait=3.0,
                   poll_interval=0.01)


class PreviousBackendIndexTests(authority.BackendStopFixture):
    """Security P2-1: unknown ids are answered from an index, never by re-reading the journal each time."""

    def count_journal_reads(self):
        """Every read of the journal file (``read_records`` goes through ``read_all``): one entry per read."""
        reads = []
        journal_file = self.backend.handoffs._journal
        read_all = journal_file.read_all
        journal_file.read_all = lambda: (reads.append("read_all"), read_all())[1]
        self.addCleanup(lambda: setattr(journal_file, "read_all", read_all))
        return reads

    def previous_command(self, seconds):
        process = subprocess.Popen(["sleep", seconds], start_new_session=True)
        self.addCleanup(lambda: (process.poll() is None and process.kill(), process.wait()))
        command_id = str(uuid4())
        self.backend.handoffs.record({"type": "terminal_started", "command_id": command_id, "task_id": None,
                                      "command": "sleep " + seconds, "cwd": "/tmp",
                                      "started_at": "2026-10-20T10:00:00Z", "log_path": "/tmp/none.log"})
        return command_id, process

    def test_many_unknown_id_stops_read_the_journal_at_most_once(self):
        previous, process = self.previous_command("613.9")
        reads = self.count_journal_reads()
        for number in range(12):
            requester = "worker" if number % 2 else "manager"
            result = (self.worker_terminal(command=None, stop=str(uuid4())) if requester == "worker"
                      else self.manager_stop(str(uuid4())))
            self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)
        self.assertLessEqual(len(reads), 1, reads)
        # the index still knows what the journal remembered from before, for either requester
        for result in (self.manager_stop(previous), self.worker_terminal(command=None, stop=previous)):
            self.assertEqual((result["status"], result["reason"]), ("refused", "previous_backend_command"), result)
        self.assertLessEqual(len(reads), 1, reads)
        self.assertIsNone(process.poll(), "the process was not signalled")

    def test_a_command_this_backend_started_and_ended_is_still_not_a_survivor(self):
        reads = self.count_journal_reads()
        started = self.run_command("sleep 613.8")
        self.assertEqual(self.manager_stop(started["command_id"])["status"], "exited")
        self.wait(lambda: not self.backend.terminal.current().get("running"), "the command to end")
        self.manager_stop(str(uuid4()))  # builds the index after this backend's command started and ended
        again = self.manager_stop(started["command_id"], reason="again")
        self.assertEqual(again["status"], "exited", again)
        self.assertIs(again.get("repeat"), True)
        self.assertLessEqual(len(reads), 1, reads)

    def forget_ended(self):
        """What the stop service forgets of an ended command once later ones push it out of its bounded memory."""
        with self.backend.command_stop._lock:
            self.backend.command_stop._ended.clear()

    def test_a_command_running_when_the_index_was_built_is_not_a_survivor_after_it_ended(self):
        started = self.run_command("sleep 613.7")
        self.assertEqual(self.manager_stop(str(uuid4()))["reason"], "unknown_command")  # the index is built now
        self.assertEqual(self.manager_stop(started["command_id"])["status"], "exited")
        self.wait(lambda: not self.backend.terminal.current().get("running"), "the command to end")
        self.forget_ended()
        for result in (self.manager_stop(started["command_id"]),
                       self.worker_terminal(command=None, stop=started["command_id"])):
            self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)

    def test_a_command_started_after_the_index_was_built_is_never_a_survivor(self):
        self.assertEqual(self.manager_stop(str(uuid4()))["reason"], "unknown_command")  # the index is built now
        started = self.run_command("sleep 613.6")
        self.assertEqual(self.manager_stop(started["command_id"])["status"], "exited")
        self.wait(lambda: not self.backend.terminal.current().get("running"), "the command to end")
        self.forget_ended()
        result = self.manager_stop(started["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)

    def test_an_own_command_whose_end_record_was_lost_is_never_a_survivor(self):
        """Delta P2-1: the journal lost this backend's ``terminal_ended`` (a storage fault) and the index is built only
        afterwards, so the journal shows the command started and not ended; it is still this backend's, not a survivor."""
        started = self.run_command("sleep 613.4")
        handoffs, record = self.backend.handoffs, self.backend.handoffs.record

        def losing_the_end(entry):
            if entry.get("type") == "terminal_ended" and entry.get("command_id") == started["command_id"]:
                raise OSError(errno.ENOSPC, "no space left on device")
            return record(entry)
        handoffs.record = losing_the_end
        self.addCleanup(lambda: vars(handoffs).pop("record", None))
        self.assertEqual(self.manager_stop(started["command_id"])["status"], "exited")
        self.wait(lambda: not self.backend.terminal.current().get("running"), "the command to end")
        vars(handoffs).pop("record", None)
        self.assertEqual([r for r in self.records("terminal_ended") if r["command_id"] == started["command_id"]], [],
                         "the end record is lost")
        self.forget_ended()
        for result in (self.manager_stop(started["command_id"]),
                       self.worker_terminal(command=None, stop=started["command_id"])):
            self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)


class ReleasedTargetTests(unittest.TestCase):
    """Standards P2-1: an ended command's target keeps nothing of the owner's command alive."""

    class Owner:
        pass

    @classmethod
    def target(cls, command_id):
        """A target whose providers capture an owner object only they hold; the weak reference to it."""
        owner = cls.Owner()
        target = StopTarget(command_id, "worker", "terminal", None, identity=lambda: owner and None,
                            forbidden=lambda: owner and frozenset())
        return target, weakref.ref(owner)

    def test_an_ended_target_does_not_hold_what_its_providers_captured(self):
        service = CommandStopService(journal=lambda record: None, policy=SHORT)
        command_id = str(uuid4())
        target, reference = self.target(command_id)
        service.register(target)
        del target
        self.assertIsNotNone(reference(), "the owner object is alive while the command runs")
        service.command_ended(command_id, {"status": "exited", "exit_code": 0, "signal": None})
        gc.collect()
        self.assertIsNone(reference(), "the ended command's object is collectable")
        self.assertIn(command_id, service._ended, "while its id is still remembered")
        self.assertEqual(service.stop("worker", command_id)["status"], "already_exited")

    def test_a_finished_episode_keeps_its_late_correction_but_not_the_owners_object(self):
        service = CommandStopService(journal=lambda record: None, policy=SHORT)
        command_id = str(uuid4())
        target, reference = self.target(command_id)
        service.register(target)
        del target
        result = service.stop("worker", command_id)  # no identity ever: the episode ends unknown
        self.assertEqual((result["status"], result["reason"]), ("unknown", "identity_unavailable"), result)
        gc.collect()
        self.assertIsNone(reference(), "an episode that finished does not keep the owner's command alive")
        service.command_ended(command_id, {"status": "exited", "exit_code": 0, "signal": None})
        self.assertEqual(service.stop("worker", command_id)["status"], "exited", "the correction still works")


def holds(root, wanted, *, barrier=()):
    """True if ``wanted`` is reachable from ``root`` through object references (function closures included), not
    going through modules, classes or instances of ``barrier`` (the long-lived owner, whose own caches are bounded)."""
    seen, pending = set(), [root]
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if item is wanted:
            return True
        if isinstance(item, (type, types.ModuleType)) or isinstance(item, barrier):
            continue
        if isinstance(item, types.FunctionType):
            pending.extend(list(item.__closure__ or ()) + list(item.__defaults__ or ()))
        elif isinstance(item, types.MethodType):
            pending.extend((item.__self__, item.__func__))
        elif isinstance(item, types.CellType):
            try:
                pending.append(item.cell_contents)
            except ValueError:
                pass
        else:
            pending.extend(gc.get_referents(item))
    return False


class EndedTerminalCommandTests(EscalationFixture):
    def test_an_ended_commands_remembered_target_does_not_hold_the_command(self):
        started = self.run_command("sleep 612.1")
        command = self.terminal._current
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")
        self.assertTrue(wait_until(lambda: not self.query().get("running"), 30))
        remembered = self.command_stop._ended[started["command_id"]]
        self.assertTrue(holds(command, command), "the helper finds a plain reference")
        self.assertFalse(holds(remembered, command, barrier=(TerminalService,)),
                         "what the stop service remembers of an ended command leads back to the command object")

    def test_the_call_key_cache_is_bounded(self):
        keys = [f"bound-{number}" for number in range(300)]
        for key in keys:
            result = self.terminal.handle(ActorRole.WORKER, call({"command": None, "stop": str(uuid4())}, key))
            self.assertEqual(result["reason"], "unknown_command", result)
        before = len([r for r in journal(self.root) if r["type"] == "terminal_duplicate"])
        self.terminal.handle(ActorRole.WORKER, call({"command": None, "stop": str(uuid4())}, keys[-1]))
        self.assertEqual(len([r for r in journal(self.root) if r["type"] == "terminal_duplicate"]), before + 1,
                         "a recent key is still replayed")
        self.terminal.handle(ActorRole.WORKER, call({"command": None, "stop": str(uuid4())}, keys[0]))
        self.assertEqual(len([r for r in journal(self.root) if r["type"] == "terminal_duplicate"]), before + 1,
                         "the oldest key was forgotten")
        self.assertLessEqual(len(self.terminal._results), 256)


class InterimStopNotCachedTests(EscalationFixture):
    def test_a_workers_interim_stopping_result_is_not_cached_for_the_call_key(self):
        started = self.run_command("trap '' INT TERM; while :; do sleep 0.2; done")
        self.assertEqual(started["status"], "running", started)
        self.kill_at_cleanup(*pids_running("sleep", "0.2"))
        self.command_stop.policy = StopPolicy(identity_wait=2.0, sigint_wait=0.6, sigterm_wait=0.6, sigkill_wait=1.0,
                                              call_wait=0.3)
        request = {"command": None, "stop": started["command_id"]}
        interim = self.terminal.handle(ActorRole.WORKER, call(request, "worker-stop-1"))
        self.assertEqual(interim["status"], "stopping", interim)
        self.command_stop.policy = POLICY
        final = self.terminal.handle(ActorRole.WORKER, call(request, "worker-stop-1"))
        self.assertEqual(final["status"], "exited", final)
        self.assertIs(final.get("repeat"), True, "the resend joined the running episode")
        self.assertEqual([r["type"] for r in journal(self.root) if r["type"] == "terminal_duplicate"], [])


class SyntheticTableEnvironment(StopEnvironment):
    """S7: a synthetic ``/proc`` table (no real process exists) that records every pidfd it opens and every signal."""

    def __init__(self, stats):
        self.table = {stat.pid: stat for stat in stats}
        self.opened, self.sent, self.fds = [], [], {}

    def monotonic(self):
        return time.monotonic()

    def boot_id(self):
        return BOOT_ID

    def list_pids(self):
        return list(self.table)

    def read_stat(self, pid):
        return self.table.get(pid)

    def pidfd_open(self, pid):
        fd = 1000 + len(self.opened)
        self.opened.append(pid)
        self.fds[fd] = pid
        return fd

    def pidfd_exited(self, fd):
        return False

    def pidfd_send_signal(self, fd, signum):
        self.sent.append((self.fds[fd], signum))

    def close(self, fd):
        self.fds.pop(fd, None)


class EndedDuringAStageTests(unittest.TestCase):
    HOST, SUPERVISOR, CHILD, PROTECTED = 910, 911, 912, 913
    SESSION, UID = 900, os.getuid()

    def stat(self, pid, ppid, group):
        return ProcStat(pid, "S", ppid, group, self.SESSION, 1000 + pid, self.UID)

    def test_a_pid_the_owner_forbids_is_not_signalled_when_the_owner_reports_the_end_mid_stage(self):
        env = SyntheticTableEnvironment([self.stat(self.HOST, 1, self.HOST), self.stat(self.SUPERVISOR, self.HOST, 1),
                                         self.stat(self.CHILD, self.SUPERVISOR, self.CHILD),
                                         self.stat(self.PROTECTED, self.CHILD, self.CHILD)])
        service = CommandStopService(journal=lambda record: None, policy=SHORT, env=env)
        command_id, reads = str(uuid4()), []

        def identity():
            reads.append(1)
            if len(reads) == 2:  # the SIGTERM stage re-reads the identity, after its check that the owner is still there
                service.command_ended(command_id, {"status": "exited", "exit_code": 143, "signal": None})
            return {"request_id": command_id, "phase": "running", "uid": self.UID, "boot_id": BOOT_ID,
                    "session": self.SESSION, "child_group": self.CHILD,
                    "parent": {"pid": self.HOST, "start_ticks": 1000 + self.HOST},
                    "supervisor": {"pid": self.SUPERVISOR, "start_ticks": 1000 + self.SUPERVISOR},
                    "child": {"pid": self.CHILD, "start_ticks": 1000 + self.CHILD}}
        service.register(StopTarget(command_id, "worker", "terminal", None, identity=identity,
                                    forbidden=lambda: frozenset({self.PROTECTED})))
        result = service.stop("worker", command_id)
        self.assertEqual(result["status"], "exited", result)
        self.assertGreaterEqual(len(reads), 2, "the end was reported inside the second stage")
        self.assertEqual([item for item in env.opened if item == self.PROTECTED], [], "never even pinned")
        self.assertEqual([pid for pid, _signum in env.sent if pid == self.PROTECTED], [])
        self.assertIn((self.CHILD, signal.SIGINT), env.sent)


class StopArrivingAsTheOwnerEndsTests(unittest.TestCase):
    """Delta P2-2: a stop that starts while the owner is still reporting the end never loses the forbidden pids."""

    HOST, SUPERVISOR, CHILD, PROTECTED = 920, 921, 922, 923
    SESSION, UID = 900, os.getuid()

    def stat(self, pid, ppid, group):
        return ProcStat(pid, "S", ppid, group, self.SESSION, 1000 + pid, self.UID)

    def test_a_stop_started_between_the_registry_update_and_the_release_never_signals_a_forbidden_pid(self):
        env = SyntheticTableEnvironment([self.stat(self.HOST, 1, self.HOST), self.stat(self.SUPERVISOR, self.HOST, 1),
                                         self.stat(self.CHILD, self.SUPERVISOR, self.CHILD),
                                         self.stat(self.PROTECTED, self.CHILD, self.CHILD)])
        service = CommandStopService(journal=lambda record: None, policy=SHORT, env=env)
        command_id, reads, results = str(uuid4()), [], []
        second_read, resume_read, stopped = threading.Event(), threading.Event(), threading.Event()

        def identity():
            reads.append(1)
            if len(reads) == 2:  # the SIGTERM stage re-reads the identity after its check that the owner is there
                second_read.set()
                resume_read.wait(30)
            return {"request_id": command_id, "phase": "running", "uid": self.UID, "boot_id": BOOT_ID,
                    "session": self.SESSION, "child_group": self.CHILD,
                    "parent": {"pid": self.HOST, "start_ticks": 1000 + self.HOST},
                    "supervisor": {"pid": self.SUPERVISOR, "start_ticks": 1000 + self.SUPERVISOR},
                    "child": {"pid": self.CHILD, "start_ticks": 1000 + self.CHILD}}
        service.register(StopTarget(command_id, "worker", "terminal", None, identity=identity,
                                    forbidden=lambda: frozenset({self.PROTECTED})))

        def stop():
            results.append(service.stop("worker", command_id))
            stopped.set()
        stopper = threading.Thread(target=stop, daemon=True)

        class Final(Mapping):
            """The owner's final result. Once the registry has the command as ended, ``command_ended`` copies it:
            that copy is where this test starts the stop, inside the window before the owner-side cleanup."""

            data = {"status": "exited", "exit_code": 143, "signal": None}

            def __getitem__(self, key):
                return self.data[key]

            def __len__(self):
                return len(self.data)

            def __iter__(self):
                if not stopper.is_alive() and not stopped.is_set():
                    stopper.start()
                    deadline = time.monotonic() + 30
                    while not (second_read.is_set() or stopped.is_set()) and time.monotonic() < deadline:
                        time.sleep(0.01)
                return iter(self.data)

        service.command_ended(command_id, Final())
        resume_read.set()
        stopper.join(30)
        self.assertFalse(stopper.is_alive())
        self.assertTrue(stopped.is_set(), "the stop was started inside command_ended")
        self.assertEqual([item for item in env.opened if item == self.PROTECTED], [], "never even pinned")
        self.assertEqual([pid for pid, _signum in env.sent if pid == self.PROTECTED], [], results)


class UnavailablePidfdEnvironment(LinuxStopEnvironment):
    """S7: the kernel refuses a pidfd (``errors``: the errors ``pidfd_open`` raises); nothing is ever signalled."""

    def __init__(self, error):
        self.error, self.opened, self.sent = error, [], []

    def pidfd_open(self, pid):
        self.opened.append(pid)
        raise self.error

    def pidfd_send_signal(self, fd, signum):
        self.sent.append(signum)
        super().pidfd_send_signal(fd, signum)


class PidfdUnavailableTests(unittest.TestCase):
    def test_a_pidfd_that_cannot_be_opened_is_not_a_stale_identity(self):
        process = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.poll() is None and process.kill(), process.wait()))
        env = UnavailablePidfdEnvironment(OSError(errno.EMFILE, "too many open files"))
        pin = ProcessPin.open(env, process.pid, proc_stat(process.pid)[4], uid=os.getuid(), boot_id=BOOT_ID)
        self.assertEqual(pin.status, "pidfd_unavailable:EMFILE")
        self.assertEqual(pin.send(signal.SIGKILL), "pidfd_unavailable:EMFILE")
        self.assertEqual(env.sent, [])
        self.assertIsNone(process.poll())


class PidfdUnavailableEpisodeTests(EscalationFixture):
    def make_stop_env(self):
        return UnavailablePidfdEnvironment(OSError(errno.EMFILE, "too many open files"))

    def test_the_stop_is_unknown_not_stale_and_nothing_is_signalled(self):
        started = self.run_command("sleep 612.2")
        (sleeper,) = pids_running("sleep", "612.2")
        self.kill_at_cleanup(sleeper)
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("unknown", "pidfd_unavailable"), result)
        self.assertEqual(result["signals"], [])
        self.assertEqual(self.stop_env.sent, [])
        self.assertTrue(self.stop_env.opened)
        self.assertEqual([r for r in journal(self.root) if r["type"] == "command_stop_refused"], [],
                         "no stale refusal is recorded")
        self.assertTrue(proc_exists(sleeper))
        os.kill(sleeper, signal.SIGKILL)  # the test's own process
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 30))
