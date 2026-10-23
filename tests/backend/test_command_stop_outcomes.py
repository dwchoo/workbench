"""CS-04: repeated stops are one episode, an unconfirmed end is ``unknown``, a late end corrects it.

Seam S2: a real PTY ``ShellPane`` + ``HostShellPort`` + ``TerminalService`` + ``CommandStopService`` (the
``test_command_stop_escalation`` pattern) with S7 error injection where the real kernel cannot be made to misbehave
on demand. The registry-only tests use a ``StopTarget`` whose identity is never available (no process involved) to
reach the service's bookkeeping deterministically.

How to inject errors (S7, reusable by independent tests): subclass ``LinuxStopEnvironment`` and pass it as
``CommandStopService(env=...)``. ``pidfd_send_signal(fd, signum)`` is the only place a signal leaves; to make a
signal "have no effect" return without sending (a process in uninterruptible sleep: ``OutcomeEnvironment.ignored``),
to make it fail raise ``PermissionError``/``ProcessLookupError`` (``OutcomeEnvironment.deny``,
``SendFailureEnvironment.errors``). ``pidfd_open`` failing is a pin that cannot be opened
(``OutcomeEnvironment.fail_open``). ``read_stat`` returning other start ticks is PID reuse. Every attempt is recorded
so a test can count the signals itself.

Expected values are this test's own: the reduced policy constants it passes, exit codes the processes it killed
produce (128+n), the errors it injected, and pid life/death read from ``/proc``. Every process here is one the test
started inside its own host shell; the survivors the injection creates are killed by the test (pid + start ticks).
"""

from __future__ import annotations

import errno
import gc
import signal
import subprocess
import threading
import time
import unittest
import weakref
from unittest import mock
from uuid import uuid4

from workbench.backend.command_stop import CommandStopService, StopPolicy, StopTarget
from workbench.backend.panes import HostShellPort

from test_command_stop_escalation import (
    POLICY, TOLERANCE, EscalationFixture, SendFailureEnvironment, kill_same_process)
from command_stop_support import alive as proc_exists, pids_running, stat_fields as proc_fields
from test_flow_terminal import journal, wait_until


class OutcomeEnvironment(SendFailureEnvironment):
    """S7: the signal numbers in ``ignored`` are accepted but have no effect (uninterruptible sleep); with ``deny``
    every signal fails with EPERM; ``fail_open`` pids cannot be pinned (``pidfd_open`` fails). Every attempt is
    recorded as (pid, signal number)."""

    def __init__(self):
        super().__init__()
        self.ignored, self.fail_open, self.deny = set(), set(), False

    def pidfd_open(self, pid):
        if pid in self.fail_open:
            raise OSError(errno.EINVAL, "injected: pidfd cannot be opened")
        return super().pidfd_open(pid)

    def pidfd_send_signal(self, fd, signum):
        if signum in self.ignored or self.deny:
            self.attempts.append((self.fds.get(fd), signum))
            if self.deny:
                raise PermissionError(errno.EPERM, "denied")
            return
        super().pidfd_send_signal(fd, signum)


class OutcomeFixture(EscalationFixture):
    def make_stop_env(self):
        return OutcomeEnvironment()

    def records(self, kind):
        return [r for r in journal(self.root) if r["type"] == kind]

    def own_process(self, *argv):
        """The one live process the test started with this argv: (pid, start ticks); killed at cleanup."""
        pids = pids_running(*argv)
        self.assertEqual(len(pids), 1, pids)
        self.kill_at_cleanup(*pids)
        return pids[0], int(proc_fields(pids[0])[19])

    def end_own(self, pid, ticks):
        """The test kills its own process (pid + start ticks) and waits until the owner observed the end."""
        kill_same_process(pid, ticks)
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 30), self.query())

    def signal_attempts(self):
        return list(self.stop_env.attempts)

    def without_repeat(self, result):
        return {key: value for key, value in result.items() if key != "repeat"}


class RepeatedStopWhileRunningTests(OutcomeFixture):
    def test_two_overlapping_stops_share_one_episode_and_get_the_same_final_result(self):
        # Behavior 1
        started = self.run_command("bash -c 'trap \"\" INT; sleep 640.1'")
        self.assertEqual(started["status"], "running", started)
        command_id = started["command_id"]
        self.own_process("sleep", "640.1")
        results, began = [], time.monotonic()
        first = threading.Thread(target=lambda: results.append(self.stop(command_id)), daemon=True)
        first.start()
        self.assertTrue(wait_until(lambda: self.query()["status"] == "stopping", 30))
        time.sleep(0.3)  # the second request arrives while the SIGINT stage is still being waited out
        second = threading.Thread(target=lambda: results.append(self.stop(command_id)), daemon=True)
        second.start()
        first.join(30)
        second.join(30)
        elapsed = time.monotonic() - began
        self.assertEqual(len(results), 2, results)
        self.assertEqual([r["status"] for r in results], ["exited", "exited"], results)
        self.assertEqual(self.without_repeat(results[0]), self.without_repeat(results[1]), "one final result")
        self.assertEqual(sorted(r.get("repeat", False) for r in results), [False, True], "the joiner is the repeat")
        self.assertEqual((results[0]["exit_code"], results[0]["signal"]), (128 + 15, 15))
        self.assertEqual([item["signal"] for item in results[0]["signals"]], ["SIGINT", "SIGTERM"])
        self.assertLess(elapsed, POLICY.sigint_wait + POLICY.sigterm_wait + TOLERANCE,
                        "the second request started no timer of its own")
        attempts = self.signal_attempts()
        self.assertEqual(len(attempts), len(set(attempts)), "no member got the same signal twice")
        self.assertEqual([r["signal"] for r in self.records("command_stop_signal")], ["SIGINT", "SIGTERM"])
        self.assertEqual(len(self.records("command_stop_requested")), 1, "one episode")
        self.assertEqual(self.records("command_stop_finished")[0]["repeat_requests"], 1)


class RepeatedStopAfterFinishTests(OutcomeFixture):
    def test_a_stop_after_the_end_returns_the_original_result_marked_repeat_and_signals_nothing(self):
        # Behavior 2
        bystander = subprocess.Popen(["sleep", "641.9"], stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (bystander.poll() is None and bystander.kill(), bystander.wait()))
        started = self.run_command("sleep 641.1")
        self.assertEqual(started["status"], "running", started)
        command_id = started["command_id"]
        original = self.stop(command_id)
        self.assertEqual((original["status"], original["exit_code"], original["signal"]), ("exited", 130, 2), original)
        self.assertNotIn("repeat", original)
        attempts = self.signal_attempts()
        kinds = ("command_stop_requested", "command_stop_target", "command_stop_signal", "command_stop_finished")
        before = {kind: len(self.records(kind)) for kind in kinds}
        threads_before = threading.active_count()
        began = time.monotonic()
        again = self.stop(command_id)
        # structural proof: ``signal_attempts()``, the record counts and the thread count are compared below; the latency bound is only a
        # sanity net at the shortest stage wait, which a repeat that started a stage could not undercut
        self.assertLess(time.monotonic() - began, POLICY.sigint_wait, "no timer or stage is started")
        self.assertEqual(again["status"], "exited", "the original result, not already_exited")
        self.assertIs(again["repeat"], True)
        self.assertEqual(self.without_repeat(again), original)
        self.assertEqual(self.signal_attempts(), attempts, "not one more signal attempt")
        self.assertEqual({kind: len(self.records(kind)) for kind in kinds}, before, "no new episode records")
        self.assertIsNone(bystander.poll(), "an unrelated process is untouched")
        third = self.stop(command_id)
        self.assertEqual(self.without_repeat(third), original)
        self.assertEqual(threading.active_count(), threads_before, "no new thread (timer) was started")
        repeats = self.records("command_stop_repeat")
        self.assertEqual([(r["command_id"], r["requested_by"], r["repeat_requests"]) for r in repeats],
                         [(command_id, "worker", 1), (command_id, "worker", 2)])


class UnknownAfterSigkillTests(OutcomeFixture):
    def test_sigkill_without_effect_is_unknown_and_the_command_stays_running(self):
        # Behavior 3
        self.stop_env.ignored = {signal.SIGTERM, signal.SIGKILL}
        started = self.run_command("trap '' INT; sleep 642.1")
        self.assertEqual(started["status"], "running", started)
        command_id = started["command_id"]
        pid, ticks = self.own_process("sleep", "642.1")
        began = time.monotonic()
        result = self.stop(command_id)
        waited = time.monotonic() - began
        self.assertEqual((result["status"], result["reason"]), ("unknown", "still_alive_after_sigkill"), result)
        self.assertGreaterEqual(waited, POLICY.sigint_wait + POLICY.sigterm_wait + POLICY.sigkill_wait - 0.1)
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertEqual([r["status"] for r in self.records("command_stop_finished")], ["unknown"])
        self.assertEqual(self.records("command_stop_finished")[0]["reason"], "still_alive_after_sigkill")
        self.assertTrue(proc_exists(pid, ticks), "the process really is still there")
        state = self.terminal.watch_state()
        self.assertEqual(state["last"]["state"], "unknown")
        self.assertTrue(state["running"], "unknown keeps the command running for the watchdog")
        self.assertEqual(self.run_command("echo no", wait=1)["status"], "terminal_command_running")
        attempts = self.signal_attempts()
        again = self.stop(command_id)
        self.assertEqual(self.without_repeat(again), result, "the same result")
        self.assertIs(again["repeat"], True)
        self.assertEqual(self.signal_attempts(), attempts, "a repeat adds no signal")
        self.assertEqual(self.terminal.watch_state()["last"]["state"], "unknown")
        self.end_own(pid, ticks)


class PermissionDeniedOutcomeTests(OutcomeFixture):
    def test_every_member_refusing_is_unknown_at_once_with_each_failure_recorded(self):
        # Behavior 4
        started = self.run_command("bash -c 'sleep 643.1 & sleep 643.2 & wait'")
        self.assertEqual(started["status"], "running", started)
        command_id = started["command_id"]
        sleepers = pids_running("sleep", "643.1") + pids_running("sleep", "643.2")
        self.assertEqual(len(sleepers), 2, sleepers)
        self.kill_at_cleanup(*sleepers)
        self.stop_env.deny = True  # every signal fails with EPERM
        began = time.monotonic()
        result = self.stop(command_id)
        self.assertEqual((result["status"], result["reason"]), ("unknown", "permission_denied"), result)
        self.assertLess(time.monotonic() - began, POLICY.sigint_wait, "no stage is waited out")
        self.assertEqual(result["signals"], [], "nothing was delivered")
        (target,) = self.records("command_stop_target")
        members = {(item["pid"], item["start_ticks"]) for item in target["tracked"]}
        self.assertTrue({(pid, int(proc_fields(pid)[19])) for pid in sleepers} <= members, "both sleepers are members")
        self.assertEqual(sorted(self.signal_attempts()), sorted((pid, signal.SIGINT) for pid, _ in members),
                         "one attempt per member, no escalation")
        (record,) = self.records("command_stop_signal")
        self.assertEqual(record["targets"], [])
        self.assertEqual(sorted((item["pid"], item["reason"]) for item in record["failed"]),
                         sorted((pid, "permission_denied") for pid, _ in members))
        state = self.terminal.watch_state()
        self.assertEqual(state["last"]["state"], "unknown")
        self.assertTrue(state["running"])
        self.assertEqual(self.run_command("echo no", wait=1)["status"], "terminal_command_running")
        attempts = self.signal_attempts()
        again = self.stop(command_id)
        self.assertEqual(self.without_repeat(again), result)
        self.assertIs(again["repeat"], True)
        self.assertEqual(self.signal_attempts(), attempts)
        for pid, ticks in members:
            kill_same_process(pid, ticks)  # the test's own processes
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 30), self.query())


class LateEndCorrectsUnknownTests(OutcomeFixture):
    def test_an_end_the_owner_observes_later_corrects_the_unknown_without_a_new_signal(self):
        # Behavior 5
        self.stop_env.ignored = {signal.SIGTERM, signal.SIGKILL}
        started = self.run_command("trap '' INT; sleep 644.1")
        self.assertEqual(started["status"], "running", started)
        command_id = started["command_id"]
        pid, ticks = self.own_process("sleep", "644.1")
        unknown = self.stop(command_id)
        self.assertEqual((unknown["status"], unknown["reason"]), ("unknown", "still_alive_after_sigkill"), unknown)
        attempts = self.signal_attempts()
        self.assertEqual(len(attempts), 3)
        self.end_own(pid, ticks)  # the test really ends its process; the owner observes it
        self.assertTrue(wait_until(lambda: self.records("command_stop_corrected"), 30))
        corrected = self.stop(command_id)
        self.assertEqual((corrected["status"], corrected["exit_code"], corrected["signal"],
                          corrected["signal_name"]), ("exited", 128 + 9, 9, "SIGKILL"), corrected)
        self.assertIs(corrected["corrected"], True)
        self.assertEqual(len(corrected["history"]), 1)
        self.assertEqual((corrected["history"][0]["status"], corrected["history"][0]["reason"]),
                         ("unknown", "still_alive_after_sigkill"))
        self.assertEqual(corrected["signals"], unknown["signals"], "the signals sent are the same three")
        self.assertEqual(corrected["residual"]["alive"], [])
        self.assertIs(corrected["repeat"], True)
        self.assertEqual(self.signal_attempts(), attempts, "the correction sent no signal")
        (record,) = self.records("command_stop_corrected")
        self.assertEqual((record["command_id"], record["from"], record["previous_reason"], record["to"],
                          record["exit_code"], record["signal"]),
                         (command_id, "unknown", "still_alive_after_sigkill", "exited", 128 + 9, 9))
        self.assertTrue(record["at"])
        self.assertEqual(len(self.records("command_stop_finished")), 1, "the episode finished once")
        self.assertEqual(self.terminal.watch_state()["last"]["state"], "exited")
        self.assert_user_owns_the_shell_again()


class RegistryRig(unittest.TestCase):
    """A service over targets whose run identity never completes: no process, only the service's bookkeeping."""

    REGISTRY_POLICY = StopPolicy(identity_wait=0.4, sigint_wait=0.5, sigterm_wait=0.5, sigkill_wait=0.5,
                                 call_wait=5.0, poll_interval=0.02)
    EXITED = {"status": "exited", "exit_code": 5, "signal": None}

    def setUp(self):
        self.records = []
        self.service = CommandStopService(journal=self.records.append, policy=self.REGISTRY_POLICY)
        self.addCleanup(self.service.close)

    def register(self):
        command_id = str(uuid4())
        self.service.register(StopTarget(command_id, "worker", "terminal", "T-registry", identity=lambda: None))
        return command_id

    def types_for(self, command_id):
        return [r["type"] for r in self.records if r.get("command_id") == command_id]

    def records_for(self, command_id, kind):
        return [r for r in self.records if r.get("command_id") == command_id and r["type"] == kind]

    @staticmethod
    def without_repeat(result):
        return {key: value for key, value in result.items() if key != "repeat"}


class EndedBeforeSignalAuditTests(RegistryRig):
    def test_the_command_ending_as_the_stop_starts_leaves_the_record_set_of_a_stop_after_the_end(self):
        after = self.register()
        self.service.command_ended(after, self.EXITED)
        direct = self.service.stop("worker", after)
        racing = self.register()
        threading.Timer(0.15, self.service.command_ended, (racing, self.EXITED)).start()
        raced = self.service.stop("worker", racing)
        for result in (direct, raced):
            self.assertEqual((result["status"], result["exit_code"]), ("already_exited", 5), result)
        self.assertEqual(self.types_for(after), ["command_stop_requested", "command_stop_refused"])
        self.assertEqual(self.types_for(racing), self.types_for(after), "one audit shape for both paths")
        shapes = [{key: self.records_for(cid, "command_stop_refused")[0][key]
                   for key in ("status", "reason", "requested_by")} for cid in (after, racing)]
        self.assertEqual(shapes[0], shapes[1])
        self.assertEqual(shapes[0], {"status": "already_exited", "reason": "command_ended", "requested_by": "worker"})
        for cid in (after, racing):
            requested = self.records_for(cid, "command_stop_requested")[0]
            self.assertEqual((requested["kind"], requested["owner"], requested["task_id"]),
                             ("terminal", "worker", "T-registry"))

    def test_a_repeat_after_the_race_returns_the_same_already_exited_result(self):
        racing = self.register()
        threading.Timer(0.15, self.service.command_ended, (racing, self.EXITED)).start()
        original = self.service.stop("worker", racing)
        again = self.service.stop("worker", racing)
        self.assertIs(again["repeat"], True)
        self.assertEqual(self.without_repeat(again), original)


class AlreadyExitedEndTimeTests(RegistryRig):
    """``already_exited`` reports when the command ended (the owner's report), never when the stop was asked."""

    END, LATER = "2026-10-01T10:00:00Z", "2026-10-01T10:05:00Z"

    def setUp(self):
        super().setUp()
        self.clock = self.END
        patcher = mock.patch("workbench.backend.command_stop._now", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def end_then_let_time_pass(self, command_id):
        self.clock = self.END
        self.service.command_ended(command_id, self.EXITED)
        self.clock = self.LATER

    def test_a_command_that_ended_before_the_stop_reports_its_own_end_time(self):
        command_id = self.register()
        self.end_then_let_time_pass(command_id)
        result = self.service.stop("worker", command_id)
        self.assertEqual((result["status"], result["ended_at"]), ("already_exited", self.END), result)

    def test_a_command_that_ends_after_the_stop_started_reports_its_own_end_time(self):
        command_id = self.register()
        threading.Timer(0.15, self.end_then_let_time_pass, (command_id,)).start()
        result = self.service.stop("worker", command_id)
        self.assertEqual((result["status"], result["ended_at"]), ("already_exited", self.END), result)
        again = self.service.stop("worker", command_id)
        self.assertEqual(again["ended_at"], self.END, "a repeat returns the same end time")

    def test_an_id_the_service_never_registered_reports_the_time_its_owner_reported(self):
        command_id = str(uuid4())
        self.end_then_let_time_pass(command_id)
        result = self.service.stop("worker", command_id)
        self.assertEqual((result["status"], result["ended_at"]), ("already_exited", self.END), result)


class EndedCommandIsNotKeptAliveTests(RegistryRig):
    class Owner:
        """Stands for the owner's command object (output buffers, port) that the target's identity reads."""

    def test_a_command_that_ended_without_a_stop_does_not_keep_the_owners_object_alive(self):
        def make():
            owner = self.Owner()
            return weakref.ref(owner), lambda: owner and None  # the closure is the only reference to the owner
        probe, identity = make()
        command_id = str(uuid4())
        self.service.register(StopTarget(command_id, "worker", "terminal", None, identity=identity))
        del identity
        self.assertIsNotNone(probe(), "alive while the command is registered")
        self.service.command_ended(command_id, self.EXITED)
        gc.collect()
        self.assertIsNone(probe(), "the remembered ended command holds no reference to the owner's object")
        self.assertEqual(self.service.stop("worker", command_id)["status"], "already_exited", "still remembered")


class UnregisteredEndedCommandAuditTests(RegistryRig):
    def test_an_ended_id_the_service_never_registered_leaves_the_same_record_pair(self):
        exited, declared = str(uuid4()), str(uuid4())
        self.service.command_ended(exited, self.EXITED)
        self.service.command_ended(declared, {"status": "unknown", "reason": "host shell state unknown: x"})
        self.assertEqual(self.service.stop("worker", exited)["status"], "already_exited")
        result = self.service.stop("worker", declared)
        self.assertEqual((result["status"], result["reason"]), ("unknown", "host shell state unknown: x"), result)
        self.assertEqual(self.types_for(exited), ["command_stop_requested", "command_stop_refused"])
        self.assertEqual(self.types_for(declared), ["command_stop_requested", "command_stop_finished"])


class JoinerAtTheCallLimitTests(RegistryRig):
    REGISTRY_POLICY = StopPolicy(identity_wait=0.6, sigint_wait=0.5, sigterm_wait=0.5, sigkill_wait=0.5,
                                 call_wait=0.1, poll_interval=0.02)

    def test_a_joiner_that_reaches_its_call_limit_gets_stopping_marked_repeat(self):
        command_id = self.register()
        first = []
        starter = threading.Thread(target=lambda: first.append(self.service.stop("worker", command_id)), daemon=True)
        starter.start()
        time.sleep(0.03)  # the episode is under way (waiting for the run identity)
        joiner = self.service.stop("worker", command_id)
        starter.join(30)
        self.assertEqual(joiner["status"], "stopping", joiner)
        self.assertIs(joiner["repeat"], True)
        self.assertEqual(first[0]["status"], "stopping")
        self.assertNotIn("repeat", first[0], "the request that started the episode is not a repeat")


class CorrectionBookkeepingTests(RegistryRig):
    def test_an_end_observed_while_the_episode_is_finishing_still_corrects_the_unknown(self):
        command_id = self.register()
        original_journal = self.service._journal

        def journal_then_end(record):
            original_journal(record)
            if record["type"] == "command_stop_finished":  # the owner observes the end right here
                self.service.command_ended(command_id, self.EXITED)
        self.service._journal = journal_then_end
        result = self.service.stop("worker", command_id)
        self.assertEqual((result["status"], result["exit_code"], result["corrected"]), ("exited", 5, True), result)
        self.assertEqual([(h["status"], h["reason"]) for h in result["history"]], [("unknown", "identity_unavailable")])
        self.assertEqual(len(self.records_for(command_id, "command_stop_corrected")), 1)
        again = self.service.stop("worker", command_id)
        self.assertEqual(self.without_repeat(again), result)

    def test_an_unknown_the_owner_declares_is_final_and_is_never_corrected(self):
        command_id = self.register()
        unknown = self.service.stop("worker", command_id)
        self.assertEqual(unknown["status"], "unknown")
        self.service.command_ended(command_id, {"status": "unknown", "reason": "host shell state unknown: x"})
        again = self.service.stop("worker", command_id)
        self.assertEqual(self.without_repeat(again), unknown)
        self.assertNotIn("corrected", again)
        self.assertEqual(self.records_for(command_id, "command_stop_corrected"), [])


class PinFailureWithoutLiveTargetTests(OutcomeFixture):
    def test_a_stage_where_no_member_can_be_pinned_still_records_the_failures_with_empty_targets(self):
        started = self.run_command("trap '' INT; sleep 645.1")
        self.assertEqual(started["status"], "running", started)
        pid, ticks = self.own_process("sleep", "645.1")
        original = self.stop_env.pidfd_send_signal

        def break_the_pin_after_sigint(fd, signum):
            original(fd, signum)
            self.stop_env.fail_open.add(pid)  # from the next stage on its pidfd cannot be opened
        self.stop_env.pidfd_send_signal = break_the_pin_after_sigint
        self.addCleanup(lambda: self.stop_env.__dict__.pop("pidfd_send_signal", None))
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("unknown", "still_alive_after_sigkill"), result)
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT"], "only SIGINT was delivered")
        self.assertEqual(self.signal_attempts(), [(pid, signal.SIGINT)])
        records = self.records("command_stop_signal")
        self.assertEqual([r["signal"] for r in records], ["SIGINT", "SIGTERM", "SIGKILL"])
        for later in records[1:]:
            self.assertEqual(later["targets"], [], later)
            self.assertEqual(later["failed"], [{"pid": pid, "reason": "pidfd_unavailable:EINVAL"}], later)
        self.end_own(pid, ticks)


class FollowFailureTests(OutcomeFixture):
    def test_a_follow_that_raises_midway_does_not_leave_the_command_registered_as_live(self):
        def failing(_command, _result):
            raise RuntimeError("injected failure in the owner's completion path")
        self.terminal._finish_run = failing
        seen = []
        previous, threading.excepthook = threading.excepthook, lambda args: seen.append(args.exc_type)
        self.addCleanup(setattr, threading, "excepthook", previous)
        started = self.run_command("echo ended-by-itself", wait=0.5)
        command_id = started["command_id"]
        self.assertTrue(wait_until(lambda: seen, 30), "the owner's follow raised")
        began = time.monotonic()
        result = self.stop(command_id)
        self.assertEqual((result["status"], result["reason"]), ("unknown", "owner_follow_failed"), result)
        self.assertLess(time.monotonic() - began, POLICY.identity_wait / 2, "no identity wait for a dead entry")
        self.assertEqual(self.signal_attempts(), [])


class HidingMainExitPort(HostShellPort):
    """S7: the owner does not show ``main_exit`` for the next ``hidden['reads']`` identity reads (it observes the
    main command's end only a moment after the service looks)."""

    hidden = {"reads": 0}

    def run_identity(self):
        identity = super().run_identity()
        if identity is not None and self.hidden["reads"] > 0:
            self.hidden["reads"] -= 1
            return {key: value for key, value in identity.items() if key != "main_exit"}
        return identity


class LeftSessionMainExitTests(OutcomeFixture):
    def test_the_unknown_for_left_session_descendants_reports_a_main_exit_observed_a_moment_later(self):
        hidden = {"reads": 0}
        self.terminal.close()
        self.terminal = self.make_terminal(lambda: type("Port", (HidingMainExitPort,), {"hidden": hidden})(
            self.pane, lambda: self.pane), **self.terminal_extras())
        started = self.run_command("bash -c 'setsid sleep 646.1 & exit 3'")
        self.assertEqual(started["status"], "running", "the escaped descendant keeps the run open")
        pid, ticks = self.own_process("sleep", "646.1")
        hidden["reads"] = 2  # the first reads of the stop (identity wait, then the unknown) show no main exit
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("unknown", "left_session_descendants"), result)
        self.assertEqual((result["exit_code"], result["signal"]), (3, None), "the main exit seen on the re-read")
        self.assertEqual(hidden["reads"], 0)
        self.end_own(pid, ticks)
        self.assert_user_owns_the_shell_again()


if __name__ == "__main__":
    unittest.main()
