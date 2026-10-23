"""CS-04 independent verification: repeated stops, ``unknown`` and the late correction.

Derived from the contract (ticket CS-04; SPEC "결과 계약" (repeat, corrected, history; a repeat after a finished stop
returns the ORIGINAL final result, ``already_exited`` only when the command ended with no stop episode), "상태 전이"
(an owner-declared unknown is never corrected), "신원·신호 불변식" 4, "감사 기록", "테스트 결정" S2/S7; BRIEF
CS-AC-10/18/20/23 (terminal part), CS-D11/D14), not from the implementation or from the worker's own tests.

Seams: S2 (a real PTY ``ShellPane`` + ``HostShellPort`` + ``TerminalService`` + ``CommandStopService``) for process
and surface behavior; S7 (an error-injecting ``StopEnvironment``: a signal that leaves but has no effect, EPERM)
where the kernel cannot be made to misbehave on demand; a registry rig (a ``StopTarget`` with no process) for
bookkeeping races.

Independent oracles: the reduced ``StopPolicy`` constants below, exit codes 128+n of the processes this file kills,
``/proc`` reads done here (pid, start ticks, process group, session, state), the errors it injects, the journal
counts, and a sentinel process outside every tree that must never be signalled.

Safety: only processes this file starts are signalled (by pidfd, after checking their start ticks); the host shell
pane is closed on teardown. Sleep durations are distinctive (66x) so a stray can be recognised.
"""

from __future__ import annotations

import os
import signal
import threading
import time
import unittest
from uuid import uuid4

from workbench.backend.command_stop import CommandStopService, StopPolicy, StopTarget

import test_command_stop_terminal_independent as base
from test_command_stop_terminal_independent import journal, wait_until
from command_stop_support import alive, all_procs, proc

SLOW = StopPolicy(identity_wait=2.0, sigint_wait=2.0, sigterm_wait=1.0, sigkill_wait=1.0, call_wait=10.0)
FAST = StopPolicy(identity_wait=2.0, sigint_wait=1.0, sigterm_wait=1.0, sigkill_wait=1.0, call_wait=10.0)
TOLERANCE = 1.5  # how late a stage may be in real time on a loaded machine


class OutcomeEnv(base.InjectingEnv):
    """S7: the signal numbers in ``ignored`` are accepted (recorded as attempts) but have no effect, like a process in
    uninterruptible sleep; ``send_error`` (inherited) fails every send with that exception."""

    def __init__(self) -> None:
        super().__init__()
        self.ignored: set[int] = set()

    def pidfd_send_signal(self, fd, signum):
        if signum in self.ignored:
            with self.lock:
                self.attempts.append((self.fd_pid.get(fd), signum))
            return
        super().pidfd_send_signal(fd, signum)

    def count(self, signum):
        with self.lock:
            return sum(1 for _, number in self.attempts if number == signum)


def kill_same_process(pid: int, ticks: int) -> None:
    """SIGKILL ``pid`` only while it still is the live process that had ``ticks`` (pidfd, re-checked)."""
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        found = proc(pid)
        if found is not None and found.ticks == ticks:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(fd)


class Rig(base.RealRig):
    POLICY = FAST

    def make_env(self):
        return OutcomeEnv()

    def setUp(self):
        super().setUp()
        self.sentinel = self.bystander("669.9")  # outside every tree: nothing may signal it
        self.sentinel_ticks = proc(self.sentinel.pid).ticks
        self.host_pid = self.pane.state["parent_pid"]

    def tearDown(self):
        try:
            self.check_no_stray_signal()
        finally:
            super().tearDown()

    def check_no_stray_signal(self):
        if not hasattr(self, "sentinel"):
            return
        attempted = self.env.attempted_pids()
        forbidden = {self.sentinel.pid, os.getpid(), self.host_pid}
        for record in self.records_of("command_stop_target"):
            forbidden.add(record["supervisor"]["pid"])
        self.assertEqual(attempted & forbidden, set(), "host shell, supervisor, backend and sentinel are never signalled")
        self.assertTrue(alive(self.sentinel.pid, self.sentinel_ticks), "the sentinel outside every tree survives")

    # -- oracles
    def command_group(self, sleeper):
        """The members of the command's process group, read from ``/proc`` (not from the production scan)."""
        group = proc(sleeper).pgrp
        self.assertNotIn(group, (0, 1, os.getpgrp(), self.host_pid), "a group of its own")
        members = [item for item in all_procs() if item.pgrp == group and item.state not in ("Z", "X")]
        for item in members:
            self.assertEqual(item.session, self.host_pid, "inside the host shell session")
            self.owned.add(item.pid)
        return members

    def end_group(self, members):
        """The test ends its own command: SIGKILL every member (pid + ticks), then wait for the owner to see it."""
        for item in members:
            kill_same_process(item.pid, item.ticks)
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 30), self.query())

    @staticmethod
    def sans_repeat(result):
        return {key: value for key, value in result.items() if key != "repeat"}


class ConcurrentRepeatTests(Rig):
    POLICY = SLOW

    def test_staggered_callers_share_one_episode_one_signal_set_and_one_final_result(self):
        # CS-AC-10 in-flight: 4 callers join over the SIGINT stage; a 5th arrives after the end
        started = self.run_command("bash -c 'trap \"\" INT; sleep 660.1'")
        self.assertEqual(started["status"], "running", started)
        command_id = started["command_id"]
        (sleeper,) = self.find("sleep", "660.1")
        group = self.command_group(sleeper)
        results: list[tuple[float, dict]] = []
        began = time.monotonic()

        def caller(delay, requester):
            time.sleep(delay)
            result = (self.command_stop.stop(requester, command_id) if requester != "worker"
                      else self.stop(command_id))
            results.append((time.monotonic() - began, result))
        offsets = [(0.0, "worker"), (0.3, "manager"), (0.7, "worker"), (1.2, "manager")]
        threads = [threading.Thread(target=caller, args=pair, daemon=True) for pair in offsets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(results), 4, results)
        total = SLOW.sigint_wait + SLOW.sigterm_wait
        for returned_at, result in results:
            self.assertLess(returned_at, total + TOLERANCE, "a joiner started no timer of its own")
        finals = [result for _, result in results]
        self.assertTrue(all(item["status"] == "exited" for item in finals), finals)
        self.assertEqual(sum(1 for item in finals if "repeat" not in item), 1, "exactly one caller created the episode")
        self.assertEqual(sum(1 for item in finals if item.get("repeat") is True), 3, "the others are repeats")
        for item in finals[1:]:
            self.assertEqual(self.sans_repeat(item), self.sans_repeat(finals[0]), "one final result")
        first = self.sans_repeat(finals[0])
        self.assertEqual((first["exit_code"], first["signal"]), (128 + 15, 15))
        self.assertEqual([item["signal"] for item in first["signals"]], ["SIGINT", "SIGTERM"], "one signal set")
        after = self.stop(command_id)  # the 5th: after the end
        self.assertIs(after["repeat"], True)
        self.assertEqual(self.sans_repeat(after), first)
        for member in group:  # the independent oracle: each member got each signal at most once
            attempts = [pair for pair in self.env.attempts if pair[0] == member.pid]
            self.assertEqual(len(attempts), len(set(attempts)), (member.pid, attempts))
        self.assertEqual(sorted(set(signum for _, signum in self.env.attempts)), sorted([signal.SIGINT, signal.SIGTERM]))
        self.assertEqual([r["signal"] for r in self.records_of("command_stop_signal")], ["SIGINT", "SIGTERM"])
        self.assertEqual(len(self.records_of("command_stop_requested")), 1, "one episode")
        self.assertEqual(len(self.records_of("command_stop_target")), 1)
        (finished,) = self.records_of("command_stop_finished")
        repeats = self.records_of("command_stop_repeat")
        self.assertEqual(finished["repeat_requests"] + len(repeats), 4,
                         "all four repeat requests are counted, in the finish record or as later repeat records")
        self.assertFalse(alive(sleeper))

    def test_many_simultaneous_callers_on_a_registry_make_one_episode(self):
        # the lock around "is there an episode": 8 simultaneous first calls
        records = []
        service = CommandStopService(journal=records.append, policy=StopPolicy(
            identity_wait=0.5, sigint_wait=0.5, sigterm_wait=0.5, sigkill_wait=0.5, call_wait=5.0, poll_interval=0.02))
        self.addCleanup(service.close)
        command_id = str(uuid4())
        service.register(StopTarget(command_id, "worker", "terminal", "T-1", identity=lambda: None))
        gate, results = threading.Barrier(8), []

        def caller():
            gate.wait()
            results.append(service.stop("worker", command_id))
        threads = [threading.Thread(target=caller, daemon=True) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(1 for item in results if "repeat" not in item), 1)
        self.assertEqual(sum(1 for item in results if item.get("repeat") is True), 7)
        self.assertEqual(len({str(self.sans_repeat(item)) for item in results}), 1, "one final result")
        by_type = [r["type"] for r in records if r.get("command_id") == command_id]
        self.assertEqual(by_type.count("command_stop_requested"), 1)
        self.assertEqual(by_type.count("command_stop_finished"), 1)
        finished = next(r for r in records if r["type"] == "command_stop_finished")
        late = [r for r in records if r["type"] == "command_stop_repeat"]
        self.assertEqual(finished["repeat_requests"] + len(late), 7)


class RepeatAfterFinishTests(Rig):
    def test_repeats_after_a_finished_stop_return_the_original_result_and_start_nothing(self):
        # CS-AC-10 after the end: original result + repeat:true, zero new signal/timer/episode record
        started = self.run_command("sleep 661.1")
        command_id = started["command_id"]
        (sleeper,) = self.find("sleep", "661.1")
        original = self.stop(command_id)
        self.assertEqual((original["status"], original["exit_code"], original["signal"]), ("exited", 130, 2), original)
        self.assertNotIn("repeat", original)
        self.assertFalse(alive(sleeper))
        attempts = list(self.env.attempts)
        before = {kind: len(self.records_of(kind)) for kind in (
            "command_stop_requested", "command_stop_target", "command_stop_signal", "command_stop_finished",
            "command_stop_refused", "command_stop_corrected")}
        threads_before = threading.active_count()
        survivor = self.bystander("669.8")
        for index, requester in enumerate(("worker", "manager", "worker"), 1):
            began = time.monotonic()
            again = (self.stop(command_id) if requester == "worker" else self.command_stop.stop(requester, command_id))
            # Structural proof of "no stage or timer": the attempts, the records and the thread count are compared below.
            # The latency bound is only a sanity net and sits at the shortest stage wait (a repeat that started a stage
            # could not answer sooner than FAST.sigint_wait), so a loaded machine cannot trip it with ordinary jitter.
            self.assertLess(time.monotonic() - began, FAST.sigint_wait, "no stage or timer is started")
            self.assertEqual(again["status"], "exited", "the original result, not already_exited")
            self.assertIs(again["repeat"], True)
            self.assertEqual(self.sans_repeat(again), original, f"repeat {index}: the original, untouched")
        self.assertEqual(self.env.attempts, attempts, "not one more signal attempt")
        self.assertEqual({kind: len(self.records_of(kind)) for kind in before}, before, "no new episode record")
        self.assertEqual(threading.active_count(), threads_before, "no new thread (timer) was started")
        self.assertTrue(alive(survivor.pid), "an unrelated process is untouched")
        self.assertEqual([r["repeat_requests"] for r in self.records_of("command_stop_repeat")], [1, 2, 3],
                         "every repeat request is counted")
        self.assertEqual([r["requested_by"] for r in self.records_of("command_stop_repeat")],
                         ["worker", "manager", "worker"])
        self.assertEqual(self.terminal.current()["status"], "exited")
        self.assert_user_owns_the_shell_again()

    def test_a_command_that_ended_by_itself_is_already_exited_and_that_is_not_an_episode(self):
        # SPEC: already_exited only when the command ended with no stop episode
        started = self.run_command("echo done-by-itself", wait=5)
        self.assertEqual((started["status"], started["exit_code"]), ("exited", 0), started)
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["exit_code"]), ("already_exited", 0), result)
        self.assertNotIn("repeat", result, "there was no episode to repeat")
        self.assertEqual(self.env.attempts, [])
        self.assertEqual(self.records_of("command_stop_signal"), [])
        again = self.stop(started["command_id"])
        self.assertEqual((again["status"], again["exit_code"]), ("already_exited", 0), again)
        self.assertEqual(self.env.attempts, [])


class UnknownAfterIneffectiveSigkillTests(Rig):
    def start_unkillable(self, seconds):
        self.env.ignored = {signal.SIGKILL}
        started = self.run_command(f"trap '' INT TERM; sleep {seconds}")
        self.assertEqual(started["status"], "running", started)
        (sleeper,) = self.find("sleep", seconds)
        return started["command_id"], sleeper, self.command_group(sleeper)

    def test_unknown_with_a_reason_is_surfaced_and_never_reported_as_exited_or_replaceable(self):
        # CS-AC-18
        command_id, sleeper, group = self.start_unkillable("662.1")
        began = time.monotonic()
        result = self.stop(command_id)
        waited = time.monotonic() - began
        self.assertEqual((result["status"], result["reason"]), ("unknown", "still_alive_after_sigkill"), result)
        self.assertGreaterEqual(waited, FAST.sigint_wait + FAST.sigterm_wait + FAST.sigkill_wait - 0.1,
                                "SIGKILL was given its whole confirmation wait")
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertTrue(alive(sleeper), "the process really is still there")
        self.assertIn(sleeper, [item["pid"] for item in result["residual"]["alive"]], "the survivor is reported")
        self.assertNotEqual(result["status"], "exited")
        self.assertIsNone(result.get("corrected"))
        # surfaced
        view = self.terminal.current()
        self.assertEqual(view["state"], "unknown")
        self.assertTrue(view["running"], "an unknown command is not done")
        self.assertNotIn("status", view, "no final status is claimed")
        watch = self.terminal.watch_state()
        self.assertEqual(watch["last"]["state"], "unknown")
        self.assertTrue(watch["running"], "the watchdog must not read an unknown command as idle")
        self.assertEqual(self.query()["status"], "unknown")
        self.assertEqual(self.gate.owner, "terminal", "the host terminal is still held by the command")
        # not replaceable
        for _ in range(2):
            refused = self.run_command("echo replaced", wait=1)
            self.assertEqual(refused["status"], "terminal_command_running", refused)
            self.assertEqual(refused["command_id"], command_id)
        self.assertNotIn(b"replaced", bytes(self.ui), "nothing was typed into the host terminal")
        # the journal says unknown, with the reason
        (finished,) = self.records_of("command_stop_finished")
        self.assertEqual((finished["status"], finished["reason"]), ("unknown", "still_alive_after_sigkill"))
        self.assertEqual(self.records_of("command_stop_corrected"), [])
        self.end_group(group)

    def test_repeats_while_unknown_give_the_same_result_and_signal_nothing(self):
        # CS-AC-10 x CS-AC-18 / invariant 4: unknown gets no new signal
        command_id, sleeper, group = self.start_unkillable("662.2")
        original = self.stop(command_id)
        self.assertEqual(original["status"], "unknown", original)
        attempts = list(self.env.attempts)
        self.assertEqual(self.env.count(signal.SIGKILL), 1)
        began = time.monotonic()
        for index, requester in enumerate(("worker", "manager", "worker"), 1):
            again = (self.stop(command_id) if requester == "worker" else self.command_stop.stop(requester, command_id))
            self.assertIs(again["repeat"], True)
            self.assertEqual(self.sans_repeat(again), original, f"repeat {index}")
        # structural proof is the unchanged ``env.attempts`` below; the bound stays under one stage wait (see above)
        self.assertLess(time.monotonic() - began, FAST.sigint_wait, "no timer was started")
        self.assertEqual(self.env.attempts, attempts, "unknown gets no new signal")
        self.assertEqual(len(self.records_of("command_stop_signal")), 3)
        self.assertEqual(self.terminal.watch_state()["last"]["state"], "unknown")
        self.end_group(group)

    def test_repeat_requests_made_while_unknown_are_recorded(self):
        # CS-AC-20 (partial): the repeat request count is recorded -- also for repeats after the stop ended unknown
        command_id, sleeper, group = self.start_unkillable("662.3")
        self.assertEqual(self.stop(command_id)["status"], "unknown")
        for _ in range(3):
            self.assertIs(self.stop(command_id)["repeat"], True)
        (finished,) = self.records_of("command_stop_finished")
        repeats = self.records_of("command_stop_repeat")
        self.assertEqual(finished["repeat_requests"] + len(repeats), 3,
                         "three repeats were requested after the first call; the audit trail must account for them")
        self.end_group(group)

    def test_the_unknown_reason_is_visible_on_the_surfaced_state(self):
        # CS-AC-18 "기록·표시": the reason is shown where the state is shown, not only in the call's return
        command_id, sleeper, group = self.start_unkillable("662.4")
        self.assertEqual(self.stop(command_id)["status"], "unknown")
        summary = self.terminal.watch_state()["last"]["stop"]
        self.assertEqual(summary.get("reason"), "still_alive_after_sigkill", summary)
        self.assertEqual(self.terminal.current()["stop"].get("reason"), "still_alive_after_sigkill")
        self.end_group(group)


class AllPermissionDeniedTests(Rig):
    def test_every_member_refusing_is_unknown_at_once_and_stays_running_with_no_further_signal(self):
        # CS-AC-18 (EPERM) + invariant 7
        started = self.run_command("bash -c 'sleep 663.1 & sleep 663.2 & wait'")
        self.assertEqual(started["status"], "running", started)
        command_id = started["command_id"]
        sleepers = self.find("sleep", "663.1") + self.find("sleep", "663.2")
        group = self.command_group(sleepers[0])
        self.env.send_error = PermissionError
        began = time.monotonic()
        result = self.stop(command_id)
        self.assertEqual((result["status"], result["reason"]), ("unknown", "permission_denied"), result)
        self.assertLess(time.monotonic() - began, FAST.sigint_wait, "no stage is waited out")
        self.assertEqual(result["signals"], [], "nothing was delivered")
        self.assertNotEqual(result["status"], "exited")
        attempted = [pair for pair in self.env.attempts]
        self.assertEqual({signum for _, signum in attempted}, {signal.SIGINT}, "no escalation after refusal")
        self.assertTrue({pid for pid, _ in attempted} >= set(sleepers))
        (record,) = self.records_of("command_stop_signal")
        self.assertEqual(record["targets"], [])
        self.assertEqual({item["reason"] for item in record["failed"]}, {"permission_denied"})
        self.assertTrue({item["pid"] for item in record["failed"]} >= set(sleepers), "each member's failure is recorded")
        self.assertEqual(self.terminal.watch_state()["last"]["state"], "unknown")
        self.assertTrue(self.terminal.watch_state()["running"])
        self.assertEqual(self.run_command("echo no", wait=1)["status"], "terminal_command_running")
        count = len(self.env.attempts)
        for _ in range(2):
            again = self.stop(command_id)
            self.assertIs(again["repeat"], True)
            self.assertEqual(self.sans_repeat(again), result)
        self.assertEqual(len(self.env.attempts), count, "repeats do not retry the refused signal")
        self.env.send_error = None
        self.end_group(group)


class LateCorrectionTests(Rig):
    def unknown_then(self, seconds):
        self.env.ignored = {signal.SIGKILL}
        started = self.run_command(f"trap '' INT TERM; sleep {seconds}")
        self.assertEqual(started["status"], "running", started)
        command_id = started["command_id"]
        (sleeper,) = self.find("sleep", seconds)
        group = self.command_group(sleeper)
        unknown = self.stop(command_id)
        self.assertEqual((unknown["status"], unknown["reason"]), ("unknown", "still_alive_after_sigkill"), unknown)
        return command_id, group, unknown

    def test_a_late_observed_end_corrects_the_same_episode_once_without_any_signal(self):
        # CS-AC-23 (terminal)
        command_id, group, unknown = self.unknown_then("664.1")
        attempts = list(self.env.attempts)
        self.end_group(group)  # the test really ends the command; the owner observes it
        self.assertTrue(wait_until(lambda: self.records_of("command_stop_corrected"), 30))
        time.sleep(0.4)
        corrected = self.stop(command_id)
        self.assertEqual((corrected["status"], corrected["exit_code"], corrected["signal"],
                          corrected["signal_name"]), ("exited", 128 + 9, 9, "SIGKILL"), corrected)
        self.assertIs(corrected["corrected"], True)
        self.assertEqual(len(corrected["history"]), 1)
        self.assertEqual((corrected["history"][0]["status"], corrected["history"][0]["reason"]),
                         ("unknown", "still_alive_after_sigkill"))
        self.assertEqual([item["signal"] for item in corrected["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"],
                         "the signals count is unchanged")
        self.assertEqual(corrected["signals"], unknown["signals"])
        self.assertEqual(corrected["residual"]["alive"], [])
        self.assertIs(corrected["repeat"], True)
        self.assertEqual(self.env.attempts, attempts, "the correction sent no signal")
        self.assertEqual(len(self.records_of("command_stop_signal")), 3)
        (record,) = self.records_of("command_stop_corrected")
        self.assertEqual((record["command_id"], record["from"], record["previous_reason"], record["to"],
                          record["exit_code"], record["signal"]),
                         (command_id, "unknown", "still_alive_after_sigkill", "exited", 128 + 9, 9))
        self.assertTrue(record["at"])
        order = [r["type"] for r in self.stop_records() if r["type"] in ("command_stop_finished",
                                                                         "command_stop_corrected")]
        self.assertEqual(order, ["command_stop_finished", "command_stop_corrected"], "the unknown stays in the trail")
        self.assertEqual(len(self.records_of("command_stop_finished")), 1, "the episode finished once")
        # the correction is final and stable
        again = self.stop(command_id)
        self.assertEqual(self.sans_repeat(again), self.sans_repeat(corrected))
        self.assertEqual(len(self.records_of("command_stop_corrected")), 1, "corrected exactly once")
        # surface and reuse
        self.assertEqual(self.terminal.watch_state()["last"]["state"], "exited")
        self.assertFalse(self.terminal.watch_state()["running"])
        self.assert_user_owns_the_shell_again()
        reuse = self.run_command("echo after-correction", wait=5)
        self.assertEqual((reuse["status"], reuse["exit_code"]), ("exited", 0), "the terminal is usable again")

    def test_an_end_seen_during_the_final_wait_is_exited_directly_and_not_a_correction(self):
        # boundary: SIGKILL "had no effect" but the process dies a moment later, inside the confirmation wait
        self.env.ignored = {signal.SIGKILL}
        started = self.run_command("trap '' INT TERM; sleep 664.2")
        (sleeper,) = self.find("sleep", "664.2")
        group = self.command_group(sleeper)
        killer = threading.Thread(target=lambda: (wait_until(lambda: self.env.count(signal.SIGKILL) >= 1, 30),
                                                  time.sleep(0.3), [kill_same_process(i.pid, i.ticks) for i in group]),
                                  daemon=True)
        killer.start()
        result = self.stop(started["command_id"])
        killer.join(30)
        self.assertEqual((result["status"], result["exit_code"], result["signal"]), ("exited", 137, 9), result)
        self.assertNotIn("corrected", result)
        self.assertNotIn("history", result)
        self.assertEqual(self.records_of("command_stop_corrected"), [])

    def test_left_session_unknown_is_corrected_with_the_main_exit_when_the_descendant_ends(self):
        # CS-AC-23 for the other unknown reason: exit_code filled, no signal ever reaches the escaped process
        started = self.run_command("bash -c 'setsid sleep 664.3 & exit 3'")
        self.assertEqual(started["status"], "running", "the escaped descendant keeps the run open")
        command_id = started["command_id"]
        (escaped,) = self.find("sleep", "664.3")
        ticks = proc(escaped).ticks
        self.assertNotEqual(proc(escaped).session, self.host_pid, "it left the host shell session")
        unknown = self.stop(command_id)
        self.assertEqual((unknown["status"], unknown["reason"]), ("unknown", "left_session_descendants"), unknown)
        self.assertEqual(self.sans_repeat(self.stop(command_id)), unknown)
        self.assertEqual(self.env.attempts, [], "the escaped descendant was never signalled")
        self.assertTrue(alive(escaped, ticks))
        kill_same_process(escaped, ticks)  # the test's own process
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 30), self.query())
        self.assertTrue(wait_until(lambda: self.records_of("command_stop_corrected"), 30))
        corrected = self.stop(command_id)
        self.assertEqual((corrected["status"], corrected["exit_code"], corrected["signal"]), ("exited", 3, None))
        self.assertIs(corrected["corrected"], True)
        self.assertEqual((corrected["history"][0]["status"], corrected["history"][0]["reason"]),
                         ("unknown", "left_session_descendants"))
        self.assertEqual(corrected["signals"], [])
        self.assertEqual(self.env.attempts, [], "the correction sent no signal")
        self.assertEqual(len(self.records_of("command_stop_corrected")), 1)
        self.assert_user_owns_the_shell_again()


class OwnerDeclaredUnknownTests(Rig):
    def test_an_unknown_the_owner_declares_is_final_and_never_corrected(self):
        # SPEC "상태 전이": the owner's own unknown (here: backend stopping) is its final result
        started = self.run_command("bash -c 'trap \"\" INT; sleep 665.1'")
        command_id = started["command_id"]
        (sleeper,) = self.find("sleep", "665.1")
        group = self.command_group(sleeper)
        thread, box = self.stop_in_background(command_id)
        self.assertTrue(wait_until(lambda: self.env.count(signal.SIGINT) >= 1, 30))
        self.terminal.close()  # the owner stops following: it declares "backend stopping" for the running command
        thread.join(30)
        result = box["result"]
        self.assertEqual(result["status"], "unknown", result)
        self.assertIn("backend stopping", str(result.get("reason")))
        self.assertNotIn("corrected", result)
        self.assertNotEqual(result["status"], "exited")
        again = self.stop(command_id)
        self.assertEqual(self.sans_repeat(again), result)
        self.assertEqual(self.records_of("command_stop_corrected"), [])
        for item in group:  # the test ends its own command; the closed owner cannot report it
            kill_same_process(item.pid, item.ticks)
        time.sleep(0.5)
        self.assertEqual(self.sans_repeat(self.stop(command_id)), result, "still not corrected")
        self.assertEqual(self.records_of("command_stop_corrected"), [])


class RegistryRig(unittest.TestCase):
    """A service over targets whose run identity never completes: only the service's bookkeeping, no process."""

    POL = StopPolicy(identity_wait=0.3, sigint_wait=0.3, sigterm_wait=0.3, sigkill_wait=0.3, call_wait=5.0,
                     poll_interval=0.02)
    EXITED = {"status": "exited", "exit_code": 5, "signal": None}

    def setUp(self):
        self.records = []
        self.service = CommandStopService(journal=self.records.append, policy=self.POL)
        self.addCleanup(self.service.close)

    def register(self):
        command_id = str(uuid4())
        self.service.register(StopTarget(command_id, "worker", "terminal", "T-r", identity=lambda: None))
        return command_id

    def of(self, command_id, kind):
        return [r for r in self.records if r.get("command_id") == command_id and r["type"] == kind]

    @staticmethod
    def sans_repeat(result):
        return {key: value for key, value in result.items() if key != "repeat"}


class CorrectionRaceTests(RegistryRig):
    def test_the_owners_end_racing_the_unknown_decision_corrects_at_most_once_and_consistently(self):
        # the end lands before / during / after the moment the episode decides unknown (identity_wait = 0.3 s)
        for step in range(0, 17):
            delay = 0.22 + step * 0.01
            with self.subTest(delay=round(delay, 2)):
                command_id = self.register()
                threading.Timer(delay, self.service.command_ended, (command_id, self.EXITED)).start()
                first = self.service.stop("worker", command_id)
                time.sleep(0.25)  # whatever the owner reports has been reported by now
                later = self.service.stop("worker", command_id)
                corrected = self.of(command_id, "command_stop_corrected")
                self.assertLessEqual(len(corrected), 1, "a correction is made exactly once")
                if first["status"] == "already_exited":  # the end was seen before the unknown was decided
                    self.assertEqual(corrected, [])
                    self.assertEqual(later["status"], "already_exited")
                    continue
                self.assertIn(first["status"] if not first.get("corrected") else "unknown", ("unknown",), first)
                self.assertEqual(len(corrected), 1, "the unknown the owner later contradicted was corrected")
                self.assertEqual((later["status"], later["exit_code"], later["corrected"]), ("exited", 5, True), later)
                self.assertIs(later["repeat"], True)
                self.assertEqual([(h["status"], h["reason"]) for h in later["history"]],
                                 [("unknown", "identity_unavailable")])
                if first.get("corrected"):
                    self.assertEqual(self.sans_repeat(later), first)
                self.assertEqual(len(self.of(command_id, "command_stop_finished")), 1)
                self.assertEqual(corrected[0]["exit_code"], 5)

    def test_a_declared_unknown_arriving_before_during_or_after_the_decision_is_never_corrected(self):
        for step in range(0, 9):
            delay = 0.26 + step * 0.02
            with self.subTest(delay=round(delay, 2)):
                command_id = self.register()
                declared = {"status": "unknown", "reason": "host shell state unknown: x"}
                threading.Timer(delay, self.service.command_ended, (command_id, declared)).start()
                first = self.service.stop("worker", command_id)
                time.sleep(0.2)
                later = self.service.stop("worker", command_id)
                self.assertEqual(self.of(command_id, "command_stop_corrected"), [])
                self.assertNotIn("corrected", later)
                self.assertNotEqual(later["status"], "exited", later)
                self.assertEqual(self.sans_repeat(later), self.sans_repeat(first) if first.get("repeat") is None
                                 else first)


class OwnerUnknownWithoutSignalsTests(RegistryRig):
    DECLARED = {"status": "unknown", "reason": "host shell state unknown: x"}

    def test_an_owner_declared_unknown_is_never_reported_as_an_already_exited_command(self):
        # ticket "범위 밖": ``unknown`` is not hidden or lowered to ``exited``; SPEC: already_exited = a command that ended
        before = self.register()
        self.service.command_ended(before, self.DECLARED)  # no stop episode yet
        during = self.register()
        threading.Timer(0.1, self.service.command_ended, (during, self.DECLARED)).start()  # inside the identity wait
        for command_id in (before, during):
            result = self.service.stop("worker", command_id)
            self.assertNotIn(result["status"], ("exited", "already_exited"), result)
            self.assertEqual(result["status"], "unknown", result)
            self.assertNotIn("corrected", result)


class FollowFailureTests(Rig):
    def failing_owner(self):
        def failing(_command, _result):
            raise RuntimeError("injected failure in the owner's completion path")
        self.terminal._finish_run = failing
        seen = []
        previous, threading.excepthook = threading.excepthook, lambda args: seen.append(args.exc_type)
        self.addCleanup(setattr, threading, "excepthook", previous)
        return seen

    def test_a_failed_follow_leaves_no_live_registry_entry_and_a_stop_does_not_wait_for_it(self):
        seen = self.failing_owner()
        started = self.run_command("echo ended-by-itself", wait=0.5)
        command_id = started["command_id"]
        self.assertTrue(wait_until(lambda: seen, 30), "the owner's follow raised")
        time.sleep(0.2)
        self.assertIsNone(self.command_stop.surface(command_id))
        began = time.monotonic()
        result = self.stop(command_id)
        self.assertLess(time.monotonic() - began, FAST.identity_wait / 2, "a dead entry is not waited on")
        self.assertNotEqual(result["status"], "stopping", result)
        self.assertEqual(self.env.attempts, [], "no signal for an ended command")

    def test_a_stop_running_when_the_follow_fails_still_gets_a_final_answer_and_does_not_claim_a_clean_end(self):
        seen = self.failing_owner()
        started = self.run_command("bash -c 'trap \"\" INT; sleep 666.1'")
        command_id = started["command_id"]
        (sleeper,) = self.find("sleep", "666.1")
        began = time.monotonic()
        result = self.stop(command_id)  # SIGTERM ends the command; the owner then fails to finish following
        self.assertLess(time.monotonic() - began, FAST.call_wait, "the call returned a final answer")
        self.assertTrue(wait_until(lambda: seen, 30))
        self.assertNotEqual(result["status"], "stopping", result)
        self.assertEqual(result["status"], "unknown", "the owner never confirmed the end")
        self.assertTrue(result.get("reason"))
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT", "SIGTERM"])
        again = self.stop(command_id)
        self.assertEqual(self.sans_repeat(again), result)
        self.assertFalse(alive(sleeper))


if __name__ == "__main__":
    unittest.main()
