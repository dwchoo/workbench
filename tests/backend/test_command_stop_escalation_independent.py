"""CS-03 independent verification: SIGINT -> SIGTERM -> SIGKILL escalation, per-stage re-scan and the residual check.

Derived from the contract (ticket CS-03; SPEC "단계와 타이머", "추적 범위와 잔존 확인", "신원·신호 불변식" 2/4/5/7,
"결과 계약", "감사 기록", "테스트 결정" S2/S7, "확정된 결정" 2; BRIEF CS-AC-04/05/18/20), not from the implementation
or from the worker's own tests.

Seams: S2 (a real PTY ``ShellPane`` + ``HostShellPort`` + ``TerminalService`` + ``CommandStopService``) for process
behavior; S7 (an error-injecting ``StopEnvironment``) where the real kernel cannot be made to misbehave on demand
(a signal that has no effect, EPERM / ESRCH per member, a pid whose identity changes between stages).

Independent oracles: the reduced ``StopPolicy`` constants below, exit codes 128+n, receipt times that a command logs
itself, the moments the injecting environment saw each signal leave, and ``/proc`` reads done here (pid, start ticks,
ppid, session, state). Production formatters are never used to build expectations; no clock is mocked.

Safety: only processes this file starts are signalled; the sentinel (outside every tree) is checked after every probe;
setsid survivors and strays are killed by pidfd (after checking their start ticks) on teardown.
Sleep durations are distinctive (7xx) so a stray can be recognised.
"""

from __future__ import annotations

import calendar
import dataclasses
import os
import signal
import sys
import textwrap
import threading
import time
import unittest

from workbench.backend.command_stop import LinuxStopEnvironment, StopPolicy

import test_command_stop_terminal_independent as base
from test_command_stop_terminal_independent import journal, wait_until
from command_stop_support import alive, descendants, proc

# A reduced policy: sigint 1.5 s, sigterm 1.0 s, sigkill (the confirmation) 1.0 s. Production is 10/5/5 (CS-10).
POL = StopPolicy(identity_wait=2.0, sigint_wait=1.5, sigterm_wait=1.0, sigkill_wait=1.0, call_wait=10.0)
TOTAL = POL.sigint_wait + POL.sigterm_wait + POL.sigkill_wait
TOL = 1.5  # how late a stage may be in real time on a loaded machine
EPS = 0.05  # clock granularity between the sender and the receiver


class ControlEnv(base.InjectingEnv):
    """S7 port. ``rules[pid]`` is called as ``rule(signum)`` and returns None (deliver), ``"swallow"`` (report success,
    deliver nothing: a signal that has no effect, e.g. a D-state process) or an OSError subclass to raise.
    ``flaky`` pids read with their real start ticks only once per scan (the first read after ``list_pids``) and with
    shifted ticks on every other read, which is how an identity that changes between the scan and the pin looks."""

    def __init__(self) -> None:
        super().__init__()
        self.timeline: list[tuple[float, int | None, int]] = []
        self.delivered: list[tuple[int | None, int]] = []
        self.rules: dict[int, object] = {}
        self.opens: list[int] = []
        self.flaky: set[int] = set()
        self.fresh: set[int] = set()
        self.by_signal: dict[int, type[OSError]] = {}

    def list_pids(self):
        with self.lock:
            self.fresh |= self.flaky
        return super().list_pids()

    def read_stat(self, pid):
        stat = LinuxStopEnvironment.read_stat(self, pid)
        if stat is None:
            return stat
        with self.lock:
            if pid in self.flaky:
                if pid in self.fresh:
                    self.fresh.discard(pid)
                    return stat
                return dataclasses.replace(stat, start_ticks=stat.start_ticks + 1)
        return stat

    def pidfd_open(self, pid):
        fd = LinuxStopEnvironment.pidfd_open(self, pid)
        with self.lock:
            self.fd_pid[fd] = pid
            self.opens.append(pid)
        return fd

    def pidfd_send_signal(self, fd, signum):
        with self.lock:
            pid = self.fd_pid.get(fd)
            self.attempts.append((pid, signum))
            self.timeline.append((time.monotonic(), pid, signum))
            rule = self.rules.get(pid)
            error = self.by_signal.get(signum)
        action = rule(signum) if rule is not None else None
        if isinstance(action, type) and issubclass(action, OSError):
            raise action(f"injected {action.__name__}")
        if action == "swallow":
            return
        if error is not None:
            raise error(f"injected {error.__name__}")
        LinuxStopEnvironment.pidfd_send_signal(self, fd, signum)
        with self.lock:
            self.sent.append((pid, signum))
            self.delivered.append((pid, signum))

    # -- reading what happened
    def first_time(self, signum):
        times = [t for t, _pid, number in self.timeline if number == signum]
        return min(times) if times else None

    def attempts_on(self, pid):
        return [signum for p, signum in self.attempts if p == pid]

    def wait_for(self, signum, timeout=30.0, pid=None):
        return wait_until(lambda: any(number == signum and (pid is None or p == pid)
                                      for _t, p, number in list(self.timeline)), timeout)


def at_seconds(text: str) -> int:
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


class EscRig(base.RealRig):
    POLICY = POL

    def make_env(self):
        return ControlEnv()

    def setUp(self):
        super().setUp()
        self.sentinel = self.bystander("790.9")

    # -- oracle helpers
    def names(self, result):
        return [item["signal"] for item in result["signals"]]

    def targets(self, result, name, *, auxiliary=False):
        return [{(t["pid"], t["start_ticks"]) for t in item["targets"]}
                for item in result["signals"] if item["signal"] == name and bool(item["auxiliary"]) == auxiliary]

    def pids_in(self, result, name, *, auxiliary=False):
        return {pid for group in self.targets(result, name, auxiliary=auxiliary) for pid, _ in group}

    def tick_of(self, pid):
        found = proc(pid)
        self.assertIsNotNone(found, pid)
        return found.ticks

    def chain_to_host(self, pid):
        """pid, its parent, ... up to (not including) the host shell: the last entry is the supervisor."""
        host, chain = self.pane.state["parent_pid"], [pid]
        while proc(chain[-1]) is not None and proc(chain[-1]).ppid != host:
            chain.append(proc(chain[-1]).ppid)
            self.assertLess(len(chain), 12, chain)
        return chain

    def family(self, *pids):
        """Every pid below the supervisor of the command that owns ``pids`` (taken before the stop)."""
        supervisor = self.chain_to_host(pids[0])[-1]
        self.supervisor = supervisor
        found = {supervisor, *descendants(supervisor)}
        for pid in found:
            self.owned.add(pid)
        return found - {supervisor}

    def write_script(self, name, body):
        path = self.root / name
        path.write_text(textwrap.dedent(body))
        return path

    def assert_safe(self, allowed=None):
        """Nothing outside the command ever received a signal; no pin leaked; no stale start ticks were signalled."""
        attempted = self.env.attempted_pids()
        host = self.pane.state["parent_pid"]
        self.assertNotIn(None, attempted)
        for forbidden in (host, os.getpid(), self.sentinel.pid, getattr(self, "supervisor", -1)):
            self.assertNotIn(forbidden, attempted, "host shell, backend, sentinel and supervisor are never signalled")
        if allowed is not None:
            self.assertLessEqual(attempted, set(allowed), "only the command's own tree is signalled")
        self.assertTrue(alive(self.sentinel.pid), "the sentinel outside every tree survives")
        self.assertTrue(alive(host))
        self.assertEqual(self.env.violations, [])
        self.assertEqual(self.env.fd_pid, {}, "no pidfd is left open when the episode is over")

    def stop_records(self):
        return [r for r in journal(self.root) if str(r["type"]).startswith("command_stop")]

    def reap(self, *pids):
        """Let the owner see the end of a command that outlived its stop (the test's own processes only)."""
        self.env.rules.clear()
        self.env.by_signal.clear()
        self.env.flaky.clear()
        self.kill_owned_and_wait_for_the_owner(*pids)


# ================================================================================================
# CS-AC-04: stage order, minimum intervals, bounded total
# ================================================================================================
class StageTimingTests(EscRig):
    STUBBORN = """
        import signal, sys, time
        log = sys.argv[1]
        def note(name):
            def handler(signum, frame):
                with open(log, "a") as out:
                    out.write(f"{name} {time.time()!r}\\n")
            return handler
        signal.signal(signal.SIGINT, note("INT"))
        signal.signal(signal.SIGTERM, note("TERM"))
        with open(log, "a") as out:
            out.write("ready 0\\n")
        while True:
            time.sleep(0.05)
    """

    def test_each_signal_arrives_one_whole_stage_after_the_previous_one_and_kill_ends_it(self):
        # CS-AC-04: SIGINT, then after sigint_wait SIGTERM, then after sigterm_wait SIGKILL; ends exited with the
        # last signal. Receipt times come from the command's own log (python handlers), the SIGKILL gap from the
        # moment the environment saw the signal leave.
        log = self.root / "received.log"
        script = self.write_script("stubborn.py", self.STUBBORN)
        started = self.run_command(f"{sys.executable} {script} {log}", wait=0.3)
        self.assertEqual(started["status"], "running", started)
        self.assertTrue(wait_until(lambda: log.exists() and "ready" in log.read_text(), 30))
        main = self.find(sys.executable, str(script), str(log))[0]
        began = time.monotonic()
        result = self.stop(started["command_id"])
        elapsed = time.monotonic() - began
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"], result["signal_name"]), (137, 9, "SIGKILL"))
        self.assertEqual(self.names(result), ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertEqual([item["stage"] for item in result["signals"]], ["sigint", "sigterm", "sigkill"])
        self.assertTrue(all(item["auxiliary"] is False and item["failed"] == [] for item in result["signals"]))
        received = {}
        for line in log.read_text().splitlines():
            name, stamp = line.split()
            received.setdefault(name, float(stamp))
        gap = received["TERM"] - received["INT"]
        self.assertGreaterEqual(gap, POL.sigint_wait - EPS, received)
        self.assertLess(gap, POL.sigint_wait + TOL, received)
        kill_gap = self.env.first_time(signal.SIGKILL) - self.env.first_time(signal.SIGTERM)
        self.assertGreaterEqual(kill_gap, POL.sigterm_wait - EPS)
        self.assertLess(kill_gap, POL.sigterm_wait + TOL)
        self.assertGreaterEqual(elapsed, POL.sigint_wait + POL.sigterm_wait - EPS)
        self.assertLess(elapsed, TOTAL + TOL, "a SIGKILL that works ends the call within the policy total")
        self.assertFalse(alive(main))
        self.assertEqual(result["residual"]["alive"], [])
        self.assert_safe({main})

    def test_the_recorded_send_times_are_ordered_and_a_stage_apart(self):
        # CS-AC-04 "각 신호와 전송 시각이 감사 기록에 있다" / CS-AC-20: command_stop_signal per stage with stage/at/targets/failed
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 749.1'", wait=0.4)
        self.assertEqual(started["status"], "running", started)
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(self.names(result), ["SIGINT", "SIGTERM", "SIGKILL"], result)
        records = [r for r in self.stop_records() if r["type"] == "command_stop_signal"]
        self.assertEqual([r["signal"] for r in records], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertEqual([r["stage"] for r in records], ["sigint", "sigterm", "sigkill"])
        for record in records:
            self.assertEqual(record["command_id"], started["command_id"])
            self.assertIs(record["auxiliary"], False)
            self.assertEqual(record["failed"], [])
            self.assertTrue(record["targets"], "every stage names who it reached")
            self.assertRegex(record["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        stamps = [at_seconds(r["at"]) for r in records]
        # the stamp has one-second resolution: floor(t2) - floor(t1) >= floor(gap) for a real gap of at least `gap`
        self.assertGreaterEqual(stamps[1] - stamps[0], int(POL.sigint_wait), stamps)
        self.assertGreaterEqual(stamps[2] - stamps[1], int(POL.sigterm_wait), stamps)
        types = [r["type"] for r in self.stop_records()]
        self.assertEqual(types[:2], ["command_stop_requested", "command_stop_target"], types)
        self.assertEqual(types[2:], ["command_stop_signal"] * 3 + ["command_stop_finished"], types)
        finished = self.records_of("command_stop_finished")[0]
        self.assertEqual((finished["status"], finished["exit_code"], finished["repeat_requests"]),
                         ("exited", 137, 0))
        self.assertEqual(finished["residual"]["alive"], [])
        text = repr(self.stop_records())
        for forbidden in (base.ENV_VALUE, "WB_STOP_FIXTURE", "sleep 749", "749.1", "cmdline", "argv"):
            self.assertNotIn(forbidden, text, "no environment value, argv or command string in the stop records")
        self.assertGreaterEqual(finished["elapsed_seconds"], POL.sigint_wait + POL.sigterm_wait - EPS)
        self.assertLess(finished["elapsed_seconds"], TOTAL + TOL)
        self.assert_safe()

    def test_the_stop_ends_as_soon_as_the_command_does_without_waiting_out_the_stage(self):
        # SPEC: "종료가 확인되면 즉시 끝난다". Answers SIGINT after 0.4 s -> only SIGINT, well before sigint_wait.
        script = self.write_script("slowint.py", """
            import signal, sys, time
            def done(signum, frame):
                time.sleep(0.4)
                sys.exit(130)
            signal.signal(signal.SIGINT, done)
            open(sys.argv[1], "w").write("ready")
            while True:
                time.sleep(0.05)
        """)
        ready = self.root / "slowint.ready"
        started = self.run_command(f"{sys.executable} {script} {ready}", wait=0.3)
        self.assertTrue(wait_until(ready.exists, 30))
        began = time.monotonic()
        result = self.stop(started["command_id"])
        elapsed = time.monotonic() - began
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), result)
        self.assertEqual(self.names(result), ["SIGINT"], "nothing stronger once it has ended")
        self.assertGreaterEqual(elapsed, 0.4 - EPS)
        self.assertLess(elapsed, POL.sigint_wait, "returned when the command ended, not at the end of the stage")
        self.assertNotIn(signal.SIGTERM, {number for _, number in self.env.attempts})
        self.assert_safe()

    def test_a_command_that_answers_sigterm_late_in_its_stage_is_not_killed(self):
        script = self.write_script("slowterm.py", """
            import signal, sys, time
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            def done(signum, frame):
                time.sleep(0.4)
                sys.exit(143)
            signal.signal(signal.SIGTERM, done)
            open(sys.argv[1], "w").write("ready")
            while True:
                time.sleep(0.05)
        """)
        ready = self.root / "slowterm.ready"
        started = self.run_command(f"{sys.executable} {script} {ready}", wait=0.3)
        self.assertTrue(wait_until(ready.exists, 30))
        began = time.monotonic()
        result = self.stop(started["command_id"])
        elapsed = time.monotonic() - began
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 143), result)
        self.assertEqual(self.names(result), ["SIGINT", "SIGTERM"])
        self.assertGreaterEqual(elapsed, POL.sigint_wait + 0.4 - EPS)
        # it died 0.4 s after SIGTERM; the owner sees the end a few hundred ms later (measured 0.2-0.35 s, more
        # under load), so the bound is the stage plus the observation lag, and the sharp check is "no SIGKILL"
        self.assertLess(elapsed, POL.sigint_wait + POL.sigterm_wait + TOL)
        self.assertNotIn(signal.SIGKILL, {number for _, number in self.env.attempts})
        self.assert_safe()

    def test_the_state_stays_stopping_through_every_stage_and_no_pin_is_held_while_waiting(self):
        # CS-AC-04 "단계가 진행되는 동안 상태는 stopping" + CS-AC-09 occupied terminal; SPEC invariant 2: pins are per stage.
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 749.2'", wait=0.4)
        command_id = started["command_id"]
        samples, open_while_waiting = [], []
        stop_flag = threading.Event()

        def sampler():
            while not stop_flag.is_set():
                seen = self.query()
                samples.append((time.monotonic(), seen["status"], seen.get("command_id")))
                with self.env.lock:
                    open_while_waiting.append((time.monotonic(), dict(self.env.fd_pid)))
                time.sleep(0.07)

        thread, box = self.stop_in_background(command_id)
        watcher = threading.Thread(target=sampler, daemon=True)
        watcher.start()
        thread.join(30)
        time.sleep(0.2)
        stop_flag.set()
        watcher.join(30)
        result = box["result"]
        self.assertEqual(result["status"], "exited", result)
        first = next(i for i, item in enumerate(samples) if item[1] == "stopping")
        statuses = [item[1] for item in samples[first:]]
        stopping_until = max(i for i, s in enumerate(statuses) if s == "stopping")
        self.assertTrue(all(s == "stopping" for s in statuses[:stopping_until + 1]),
                        f"every sample between the first stopping and the end reads stopping: {statuses}")
        self.assertEqual(statuses[-1], "exited")
        self.assertGreaterEqual(stopping_until, 10, "the sampler really covered the stages")
        # in the middle of each wait no pidfd is held (the pins live for the signalling of one stage only)
        for stage_signal in (signal.SIGINT, signal.SIGTERM):
            sent_at = self.env.first_time(stage_signal)
            mid = [fds for t, fds in open_while_waiting if sent_at + 0.4 < t < sent_at + 0.8]
            self.assertTrue(mid, "sampled mid-stage")
            self.assertTrue(all(fds == {} for fds in mid), f"pins are released before the stage wait: {mid}")
        self.assertFalse(self.terminal.current()["running"])
        self.assert_safe()

    def test_a_call_that_returns_stopping_mid_escalation_sees_the_later_stages_arrive(self):
        # SPEC "결과 계약": stopping = episode summary on call-wait expiry; the episode (not the call) owns the stages
        self.command_stop.policy = dataclasses.replace(POL, call_wait=0.4)
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 749.3'", wait=0.4)
        (member,) = self.find("sleep", "749.3")
        family = self.family(member)
        began = time.monotonic()
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "stopping", result)
        # "stopping" is the structural proof; the bound is a sanity net at the SIGINT stage wait (call_wait is 0.4 s)
        self.assertLess(time.monotonic() - began, POL.sigint_wait)
        counts = set()
        deadline = time.monotonic() + TOTAL + TOL
        while time.monotonic() < deadline and self.terminal.current()["running"]:
            seen = self.query()
            if seen["status"] == "stopping":
                counts.add(len(seen["stop"]["signals"]))
            time.sleep(0.05)
        # the SIGKILL ends the command within milliseconds, so a 50 ms sampler may legitimately miss the third count
        self.assertTrue({1, 2} <= counts, f"the summary showed the signals arriving one stage apart: {counts}")
        self.assertTrue(wait_until(lambda: self.records_of("command_stop_finished"), 30))
        self.assertEqual([r["signal"] for r in self.records_of("command_stop_signal")],
                         ["SIGINT", "SIGTERM", "SIGKILL"])
        finished = self.records_of("command_stop_finished")[0]
        self.assertEqual((finished["status"], finished["exit_code"]), ("exited", 137))
        self.assertFalse(alive(member))
        self.assert_safe(family)

    def test_when_nothing_works_the_total_is_the_policy_sum_and_each_stage_is_waited_in_full(self):
        # CS-AC-04/18: SIGKILL has no effect (S7) -> after the confirmation wait the answer is unknown, never exited
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 748.1'", wait=0.4)
        (member,) = self.find("sleep", "748.1")
        family = self.family(member)
        for pid in family:
            self.env.rules[pid] = lambda signum: "swallow" if signum == signal.SIGKILL else None
        began = time.monotonic()
        result = self.stop(started["command_id"])
        elapsed = time.monotonic() - began
        self.assertEqual((result["status"], result["reason"]), ("unknown", "still_alive_after_sigkill"), result)
        self.assertGreaterEqual(elapsed, TOTAL - EPS, "every stage including the confirmation wait is waited out")
        self.assertLess(elapsed, TOTAL + TOL, "and the whole stays inside the policy sum")
        self.assertEqual(self.names(result), ["SIGINT", "SIGTERM", "SIGKILL"])
        t_int, t_term, t_kill = (self.env.first_time(n) for n in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL))
        self.assertGreaterEqual(t_term - t_int, POL.sigint_wait - EPS)
        self.assertGreaterEqual(t_kill - t_term, POL.sigterm_wait - EPS)
        live = sorted(pid for pid in family if alive(pid))
        self.assertTrue(live, "SIGKILL was swallowed, so the command is still there")
        self.assertEqual(sorted(item["pid"] for item in result["residual"]["alive"]), live,
                         "the residual lists exactly what /proc still shows alive")
        for item in result["residual"]["alive"]:
            self.assertEqual(item["start_ticks"], self.tick_of(item["pid"]))
        self.assertEqual(self.query()["status"], "unknown")
        self.assertEqual(self.run_command("echo no", wait=0.5)["status"], "terminal_command_running",
                         "an unknown command is not reported as safe to replace")
        finished = self.records_of("command_stop_finished")[0]
        self.assertEqual((finished["status"], finished["reason"]), ("unknown", "still_alive_after_sigkill"))
        self.assertTrue(finished["residual"]["alive"])
        self.assert_safe(family)
        self.reap(*live)
        self.assert_user_owns_the_shell_again()


# ================================================================================================
# CS-AC-05: descendants
# ================================================================================================
class DescendantTests(EscRig):
    def assert_family_dead(self, family):
        self.assertTrue(wait_until(lambda: not any(alive(pid) for pid in family), 30),
                        [pid for pid in family if alive(pid)])

    def test_background_jobs_of_a_command_that_ignores_everything_all_receive_every_stage(self):
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 730.1 & sleep 730.2 & wait'")
        sleepers = self.find("sleep", "730.1") + self.find("sleep", "730.2")
        (main,) = {proc(pid).ppid for pid in sleepers}
        family = self.family(*sleepers)
        expected = {(pid, self.tick_of(pid)) for pid in (main, *sleepers)}
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["exit_code"], result["signal"]), ("exited", 137, 9), result)
        self.assertEqual(self.names(result), ["SIGINT", "SIGTERM", "SIGKILL"])
        for name in ("SIGINT", "SIGTERM", "SIGKILL"):
            (reached,) = self.targets(result, name)
            self.assertGreaterEqual(reached, expected, f"{name} reached the main command and both jobs")
            self.assertLessEqual({pid for pid, _ in reached}, family)
        self.assert_family_dead(family)
        self.assertEqual(result["residual"]["alive"], [])
        self.assertEqual(result["residual"]["left_session"], [])
        self.assertTrue(result["residual"]["unchecked"])
        self.assert_safe(family)

    def test_a_descendant_of_a_finished_main_is_reparented_and_still_gets_the_stages(self):
        # ticket behavior 4: main exited 0, the stop still escalates against the reparented descendant
        started = self.run_command("bash -c '(trap \"\" INT TERM; exec sleep 731.1) & exit 0'")
        self.assertEqual(started["status"], "running", "a tracked descendant keeps the command running")
        (orphan,) = self.find("sleep", "731.1")
        found = proc(orphan)
        self.assertEqual(found.session, self.pane.state["parent_pid"])
        family = self.family(orphan)
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"]), (0, None), "the main's own end is reported")
        self.assertEqual(self.names(result), ["SIGINT", "SIGTERM", "SIGKILL"])
        for name in ("SIGINT", "SIGTERM", "SIGKILL"):
            (reached,) = self.targets(result, name)
            self.assertEqual(reached, {(orphan, found.ticks)}, "only the reparented descendant is left to signal")
        self.assertFalse(alive(orphan))
        self.assertEqual(result["residual"]["alive"], [])
        self.assert_safe(family | {orphan})

    def test_a_double_forked_grandchild_is_found_and_signalled(self):
        started = self.run_command("bash -c '( (trap \"\" INT TERM; exec sleep 732.1) & ); "
                                   "trap \"\" INT TERM; sleep 732.2'")
        (grandchild,) = self.find("sleep", "732.1")
        (mainsleep,) = self.find("sleep", "732.2")
        family = self.family(grandchild, mainsleep)
        ticks = self.tick_of(grandchild)
        self.assertEqual(proc(grandchild).ppid, self.supervisor, "its forker is gone: the subreaper adopted it")
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertIn((grandchild, ticks), self.targets(result, "SIGINT")[0])
        self.assertIn((grandchild, ticks), self.targets(result, "SIGKILL")[0])
        self.assert_family_dead(family | {grandchild})
        self.assertEqual(result["residual"]["alive"], [])
        self.assert_safe(family | {grandchild})

    def test_a_member_in_its_own_process_group_of_the_same_session_is_still_tracked(self):
        # SPEC: tracked = supervisor descendants of the host shell session (a group of its own does not escape it)
        started = self.run_command("bash -c 'set -m; sleep 733.1 & wait'")
        (member,) = self.find("sleep", "733.1")
        mainproc = proc(proc(member).ppid)
        found = proc(member)
        self.assertEqual(found.session, self.pane.state["parent_pid"])
        self.assertNotEqual(found.pgrp, mainproc.pgrp, "job control put it in a group of its own")
        family = self.family(member)
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertIn((member, found.ticks), self.targets(result, "SIGINT")[0])
        self.assertFalse(alive(member))
        self.assertEqual(result["residual"]["alive"], [])
        self.assert_safe(family | {member})

    def test_a_child_born_after_the_first_signal_is_found_by_the_next_stage(self):
        # SPEC invariant 2: before every stage the tracked members are read again
        born = self.root / "born.pid"
        script = self.write_script("spawner.py", """
            import signal, subprocess, sys, time
            def quiet():
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            def on_int(signum, frame):
                if not open(sys.argv[1]).read():
                    child = subprocess.Popen(["sleep", "734.1"], preexec_fn=quiet)
                    open(sys.argv[1], "w").write(str(child.pid))
            signal.signal(signal.SIGINT, on_int)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            open(sys.argv[1], "w").write("")
            open(sys.argv[2], "w").write("ready")
            while True:
                time.sleep(0.05)
        """)
        born.write_text("")
        ready = self.root / "spawner.ready"
        started = self.run_command(f"{sys.executable} {script} {born} {ready}", wait=0.3)
        self.assertTrue(wait_until(ready.exists, 30))
        (main,) = self.find(sys.executable, str(script), str(born), str(ready))
        self.assertEqual(pids_none("734.1"), [], "the child does not exist before the first signal")
        family = self.family(main)
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        child = int(born.read_text())
        self.owned.add(child)
        self.assertEqual(self.names(result), ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertNotIn(child, self.pids_in(result, "SIGINT"), "it did not exist when SIGINT left")
        self.assertIn(child, self.pids_in(result, "SIGTERM"), "the second stage found the newborn")
        self.assertIn(child, self.pids_in(result, "SIGKILL"))
        self.assertIn(main, self.pids_in(result, "SIGKILL"))
        self.assertFalse(alive(child))
        self.assertEqual(result["residual"]["alive"], [])
        self.assert_safe(family | {child})

    def test_main_ending_at_sigint_leaves_the_descendant_to_the_next_stage_and_the_main_code_stays(self):
        started = self.run_command("bash -c 'trap \"exit 130\" INT; (trap \"\" INT; exec sleep 735.1) & wait'")
        (orphan,) = self.find("sleep", "735.1")
        (main,) = {proc(orphan).ppid}
        family = self.family(orphan)
        ticks = self.tick_of(orphan)
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(result["exit_code"], 130, "the command's own end, not the descendant's")
        self.assertEqual(self.names(result), ["SIGINT", "SIGTERM"])
        self.assertIn((orphan, ticks), self.targets(result, "SIGINT")[0])
        self.assertEqual(self.targets(result, "SIGTERM")[0], {(orphan, ticks)}, "the ended main is not signalled again")
        self.assertNotIn(main, self.pids_in(result, "SIGTERM"))
        self.assertFalse(alive(orphan))
        self.assertEqual(result["residual"]["alive"], [])
        self.assert_safe(family | {orphan})

    def test_a_zombie_child_of_a_member_is_neither_signalled_nor_reported(self):
        # a Z process is not alive: no target, no failed entry, not in the residual
        zombies = self.root / "zombie.pid"
        script = self.write_script("zombie.py", """
            import os, signal, sys, time
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            child = os.fork()
            if child == 0:
                os._exit(0)
            open(sys.argv[1], "w").write(str(child))
            while True:
                time.sleep(0.05)
        """)
        started = self.run_command(f"{sys.executable} {script} {zombies}", wait=0.3)
        self.assertTrue(wait_until(lambda: zombies.exists() and zombies.read_text(), 30))
        zombie = int(zombies.read_text())
        self.assertTrue(wait_until(lambda: proc(zombie) is not None and proc(zombie).state == "Z", 30), proc(zombie))
        (main,) = self.find(sys.executable, str(script), str(zombies))
        family = self.family(main)
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 137), result)
        self.assertNotIn(zombie, self.env.attempted_pids())
        for item in result["signals"]:
            self.assertNotIn(zombie, [entry["pid"] for entry in item["failed"]])
            self.assertNotIn(zombie, [entry["pid"] for entry in item["targets"]])
        self.assertEqual(result["residual"]["alive"], [])
        self.assert_safe(family | {main})

    def test_exited_is_never_claimed_while_a_tracked_descendant_is_alive(self):
        # CS-AC-05 residual honesty + SPEC: exited only when main and every tracked descendant are gone
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 736.1 & sleep 736.2 & wait'")
        first, second = self.find("sleep", "736.1")[0], self.find("sleep", "736.2")[0]
        family = self.family(first, second)
        survivor = second
        ticks = self.tick_of(survivor)
        self.env.rules[survivor] = lambda signum: "swallow"  # no signal reaches this one
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("unknown", "still_alive_after_sigkill"), result)
        self.assertTrue(alive(survivor))
        self.assertFalse(alive(first))
        self.assertEqual(result["residual"]["alive"], [{"pid": survivor, "start_ticks": ticks}])
        self.assertEqual(self.env.attempts_on(survivor), [signal.SIGINT, signal.SIGTERM, signal.SIGKILL],
                         "it was tried at every stage")
        self.assertEqual(self.query()["status"], "unknown")
        self.assert_safe(family)
        self.reap(survivor)
        self.assertEqual(self.terminal.current()["status"], "exited")
        self.assert_user_owns_the_shell_again()


def pids_none(seconds):
    return base.pids_running("sleep", seconds)


# ================================================================================================
# CS-AC-05 + "확정된 결정" 2: left_session
# ================================================================================================
class LeftSessionTests(EscRig):
    def test_only_a_setsid_survivor_left_is_reported_and_never_signalled_and_ends_unknown_at_once(self):
        started = self.run_command("bash -c 'setsid sleep 737.1 & exit 0'")
        (escaped,) = self.find("sleep", "737.1")
        found = proc(escaped)
        self.assertNotEqual(found.session, self.pane.state["parent_pid"])
        began = time.monotonic()
        result = self.stop(started["command_id"])
        elapsed = time.monotonic() - began
        self.assertEqual((result["status"], result["reason"]), ("unknown", "left_session_descendants"), result)
        self.assertLess(elapsed, POL.sigint_wait, "no signal can help: the stages are not waited out")
        self.assertEqual(self.env.attempts, [], "nothing at all was signalled")
        self.assertEqual(result["signals"], [])
        self.assertTrue(alive(escaped))
        self.assertEqual(result["residual"]["left_session"],
                         [{"pid": escaped, "start_ticks": found.ticks, "session": found.session}])
        self.assertEqual(result["residual"]["alive"], [])
        self.assertTrue(result["residual"]["unchecked"])
        self.assertEqual(self.query()["status"], "unknown")
        self.assertEqual(self.run_command("echo no", wait=0.5)["status"], "terminal_command_running",
                         "the terminal stays occupied until the survivor ends")
        finished = self.records_of("command_stop_finished")[0]
        self.assertEqual((finished["status"], finished["reason"]), ("unknown", "left_session_descendants"))
        self.assertEqual(finished["residual"]["left_session"][0]["pid"], escaped)
        self.assert_safe(set())
        self.reap(escaped)
        self.assertEqual(self.terminal.current()["status"], "exited")
        self.assert_user_owns_the_shell_again()

    def test_the_main_end_is_reported_as_observed_next_to_left_session_unknown(self):
        # SPEC "추적 범위": 'main의 exit_code/signal은 관측값으로 함께 보고'
        started = self.run_command("bash -c 'setsid sleep 737.2 & exit 3'")
        (escaped,) = self.find("sleep", "737.2")
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("unknown", "left_session_descendants"), result)
        self.assertEqual(result.get("exit_code"), 3, result)
        self.reap(escaped)

    def test_a_left_session_survivor_next_to_a_tracked_member_gets_the_whole_escalation_only_for_the_member(self):
        # mixed: the tracked part is escalated to the end, the escaped one is reported only; once only it remains: unknown
        started = self.run_command("bash -c 'trap \"\" INT TERM; setsid sleep 738.1 & sleep 738.2'")
        (escaped,) = self.find("sleep", "738.1")
        (member,) = self.find("sleep", "738.2")
        main = proc(member).ppid
        family = self.family(member)
        found = proc(escaped)
        member_ticks = self.tick_of(member)
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("unknown", "left_session_descendants"), result)
        self.assertEqual(self.names(result), ["SIGINT", "SIGTERM", "SIGKILL"], "the tracked part ran to SIGKILL")
        for name in ("SIGINT", "SIGTERM", "SIGKILL"):
            reached = self.pids_in(result, name)
            self.assertTrue({main, member} <= reached, (name, reached))
            self.assertNotIn(escaped, reached)
        self.assertNotIn(escaped, self.env.attempted_pids())
        self.assertFalse(alive(member))
        self.assertFalse(alive(main))
        self.assertTrue(alive(escaped))
        self.assertEqual(result["residual"]["alive"], [])
        self.assertEqual(result["residual"]["left_session"],
                         [{"pid": escaped, "start_ticks": found.ticks, "session": found.session}])
        self.assertIn((member, member_ticks), self.targets(result, "SIGKILL")[0])
        self.assert_safe(family)
        self.reap(escaped)

    def test_the_survivor_left_after_a_term_death_stops_the_escalation_and_is_not_killed(self):
        # the tracked part dies at SIGTERM; only the escaped is left -> unknown now, SIGKILL has nobody to go to
        started = self.run_command("bash -c 'trap \"\" INT; setsid sleep 738.3 & sleep 738.4'")
        (escaped,) = self.find("sleep", "738.3")
        (member,) = self.find("sleep", "738.4")
        family = self.family(member)
        began = time.monotonic()
        result = self.stop(started["command_id"])
        elapsed = time.monotonic() - began
        self.assertEqual((result["status"], result["reason"]), ("unknown", "left_session_descendants"), result)
        self.assertEqual(self.names(result), ["SIGINT", "SIGTERM"])
        self.assertNotIn(signal.SIGKILL, {number for _, number in self.env.attempts})
        self.assertLess(elapsed, POL.sigint_wait + POL.sigterm_wait + 0.5, "the confirmation stage is not spent")
        self.assertTrue(alive(escaped))
        self.assertEqual([item["pid"] for item in result["residual"]["left_session"]], [escaped])
        self.assertEqual(result["residual"]["alive"], [])
        self.assert_safe(family)
        self.reap(escaped)

    def test_a_process_outside_the_host_session_is_not_reported_or_signalled_at_all(self):
        # BRIEF CS-AC-05: processes outside the tracked tree are 'unchecked', never signalled
        outsider = self.bystander("739.1")
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 739.2'")
        (member,) = self.find("sleep", "739.2")
        family = self.family(member)
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertTrue(alive(outsider.pid) and alive(self.sentinel.pid))
        self.assertNotIn(outsider.pid, self.env.attempted_pids())
        reported = [item["pid"] for item in result["residual"]["alive"] + result["residual"]["left_session"]]
        self.assertNotIn(outsider.pid, reported)
        self.assertTrue(result["residual"]["unchecked"])
        self.assert_safe(family)


# ================================================================================================
# SPEC invariant 5: stopped (T) members
# ================================================================================================
class StoppedMemberTests(EscRig):
    def wait_state(self, pid, state):
        self.assertTrue(wait_until(lambda: (found := proc(pid)) is not None and found.state == state, 30),
                        (pid, state, proc(pid)))

    def test_a_stopped_member_gets_sigcont_after_sigint_as_an_auxiliary_signal_and_the_command_ends(self):
        started = self.run_command("sleep 740.1 | sleep 740.2")
        first, second = self.find("sleep", "740.1")[0], self.find("sleep", "740.2")[0]
        family = self.family(first, second)
        ticks = self.tick_of(first)
        os.kill(first, signal.SIGSTOP)
        self.wait_state(first, "T")
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(result["exit_code"], 130, result)
        aux = [item for item in result["signals"] if item["auxiliary"]]
        self.assertEqual([item["signal"] for item in aux], ["SIGCONT"], result["signals"])
        self.assertEqual({(t["pid"], t["start_ticks"]) for t in aux[0]["targets"]}, {(first, ticks)},
                         "only the stopped member is continued")
        order = [item["signal"] for item in result["signals"]]
        self.assertLess(order.index("SIGINT"), order.index("SIGCONT"), "SIGCONT follows the SIGINT it lets work")
        self.assertEqual(aux[0]["stage"], "sigint")
        self.assertNotIn("SIGTERM", order, "continuing it was enough")
        record = [r for r in self.stop_records() if r["type"] == "command_stop_signal" and r["signal"] == "SIGCONT"]
        self.assertEqual(len(record), 1)
        self.assertIs(record[0]["auxiliary"], True)
        self.assertEqual(record[0]["stage"], "sigint")
        self.assertEqual({t["pid"] for t in record[0]["targets"]}, {first})
        self.assertFalse(alive(first) or alive(second))
        self.assertEqual(result["residual"]["alive"], [])
        self.assert_safe(family)

    def test_a_member_stopped_again_between_stages_is_continued_in_each_term_and_int_stage(self):
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 741.1 & sleep 741.2 & wait'")
        stopped, other = self.find("sleep", "741.1")[0], self.find("sleep", "741.2")[0]
        family = self.family(stopped, other)
        os.kill(stopped, signal.SIGSTOP)
        self.wait_state(stopped, "T")
        thread, box = self.stop_in_background(started["command_id"])
        self.assertTrue(self.env.wait_for(signal.SIGCONT, 8, pid=stopped))
        self.wait_state(stopped, "S")
        os.kill(stopped, signal.SIGSTOP)  # stopped again before the next stage
        self.wait_state(stopped, "T")
        thread.join(30)
        result = box["result"]
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 137), result)
        aux = [(item["stage"], {t["pid"] for t in item["targets"]}) for item in result["signals"] if item["auxiliary"]]
        self.assertEqual(aux[:2], [("sigint", {stopped}), ("sigterm", {stopped})], result["signals"])
        self.assertNotIn(other, {pid for _, group in aux for pid in group}, "a running member gets no SIGCONT")
        self.assertFalse(alive(stopped) or alive(other))
        self.assert_safe(family)

    def test_no_sigcont_is_sent_when_nobody_is_stopped(self):
        started = self.run_command("bash -c 'trap \"\" INT; sleep 741.3'")
        (member,) = self.find("sleep", "741.3")
        family = self.family(member)
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertNotIn("SIGCONT", self.names(result))
        self.assertNotIn(signal.SIGCONT, {number for _, number in self.env.attempts})
        self.assert_safe(family)


# ================================================================================================
# SPEC invariant 7 / S7: EPERM and ESRCH per member
# ================================================================================================
class PerMemberFailureTests(EscRig):
    def test_eperm_on_every_member_at_a_later_stage_is_unknown_at_once_without_waiting_it_out(self):
        started = self.run_command("bash -c 'trap \"\" INT; sleep 742.1'")
        (member,) = self.find("sleep", "742.1")
        family = self.family(member)
        self.env.by_signal[signal.SIGTERM] = PermissionError
        began = time.monotonic()
        result = self.stop(started["command_id"])
        elapsed = time.monotonic() - began
        self.assertEqual((result["status"], result["reason"]), ("unknown", "permission_denied"), result)
        self.assertGreaterEqual(elapsed, POL.sigint_wait - EPS, "the SIGINT stage was served in full")
        self.assertLess(elapsed, POL.sigint_wait + POL.sigterm_wait, "but the SIGTERM stage was not waited out")
        self.assertNotIn(signal.SIGKILL, {number for _, number in self.env.attempts},
                         "no further escalation once every member answered EPERM")
        failed = [r for r in self.stop_records() if r["type"] == "command_stop_signal" and r["signal"] == "SIGTERM"]
        self.assertEqual(len(failed), 1, "the refused stage is journaled")
        self.assertEqual(failed[0]["targets"], [])
        self.assertTrue({item["pid"] for item in failed[0]["failed"]} >= {member}, failed)
        self.assertTrue(all("perm" in str(item["reason"]).lower() for item in failed[0]["failed"]), failed)
        self.assertTrue(alive(member))
        self.assertEqual(self.query()["status"], "unknown")
        self.assertIn(member, [item["pid"] for item in result["residual"]["alive"]])
        self.assert_safe(family)
        self.reap(member)

    def test_eperm_on_one_member_is_recorded_and_the_rest_keep_the_full_escalation(self):
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 743.1 & sleep 743.2 & wait'")
        denied, other = self.find("sleep", "743.1")[0], self.find("sleep", "743.2")[0]
        (main,) = {proc(denied).ppid}
        family = self.family(denied, other)
        self.env.rules[denied] = lambda signum: PermissionError
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "unknown", result)
        self.assertIn(result["reason"], ("still_alive_after_sigkill", "permission_denied"), result)
        self.assertEqual(self.names(result), ["SIGINT", "SIGTERM", "SIGKILL"], "the other members went on to SIGKILL")
        for name in ("SIGINT", "SIGTERM", "SIGKILL"):
            reached = self.pids_in(result, name)
            self.assertTrue({main, other} <= reached and denied not in reached, (name, reached))
        for item in result["signals"]:
            self.assertEqual([entry["pid"] for entry in item["failed"]], [denied], item)
            self.assertIn("perm", str(item["failed"][0]["reason"]).lower())
        self.assertEqual(self.env.attempts_on(denied), [signal.SIGINT, signal.SIGTERM, signal.SIGKILL],
                         "a failed member is tried at every stage")
        self.assertFalse(alive(other) or alive(main))
        self.assertTrue(alive(denied))
        self.assertEqual([item["pid"] for item in result["residual"]["alive"]], [denied])
        self.assert_safe(family)
        self.reap(denied)

    def test_esrch_on_one_member_is_recorded_and_that_member_is_found_again_by_the_next_stage(self):
        started = self.run_command("sleep 744.1 | sleep 744.2")
        victim, other = self.find("sleep", "744.1")[0], self.find("sleep", "744.2")[0]
        family = self.family(victim, other)
        fired = []

        def once(signum):
            if not fired:
                fired.append(signum)
                return ProcessLookupError
            return None
        self.env.rules[victim] = once
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(self.names(result)[:2], ["SIGINT", "SIGTERM"], result["signals"])
        interrupt = [r for r in self.stop_records() if r["type"] == "command_stop_signal" and r["signal"] == "SIGINT"]
        self.assertIn(victim, [item["pid"] for item in interrupt[0]["failed"]], "ESRCH is recorded per member")
        self.assertTrue(all(item["reason"] for item in interrupt[0]["failed"]))
        self.assertIn(other, {t["pid"] for t in interrupt[0]["targets"]}, "the other members were still signalled")
        self.assertIn(victim, self.pids_in(result, "SIGTERM"), "it was tracked again and reached at the next stage")
        self.assertFalse(alive(victim) or alive(other))
        self.assertEqual(result["residual"]["alive"], [])
        self.assert_safe(family)


# ================================================================================================
# SPEC invariants 2 and 4: identity changes between stages
# ================================================================================================
class IdentityBetweenStagesTests(EscRig):
    def test_a_member_whose_identity_stops_matching_is_never_signalled_again_in_the_episode(self):
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 745.1 & sleep 745.2 & wait'")
        changing, steady = self.find("sleep", "745.1")[0], self.find("sleep", "745.2")[0]
        (main,) = {proc(changing).ppid}
        family = self.family(changing, steady)

        def after_first(signum):
            if signum == signal.SIGINT:
                self.env.flaky.add(changing)  # from the next scan on, its pin never agrees with the scan
            return None
        self.env.rules[changing] = after_first
        result = self.stop(started["command_id"])
        self.assertEqual(self.env.attempts_on(changing), [signal.SIGINT],
                         "signalled once, before its identity stopped matching; never again")
        self.assertEqual(self.env.violations, [])
        self.assertEqual(self.names(result), ["SIGINT", "SIGTERM", "SIGKILL"], "the verified members went on")
        for name in ("SIGTERM", "SIGKILL"):
            reached = self.pids_in(result, name)
            self.assertTrue({main, steady} <= reached and changing not in reached, (name, reached))
        for name in ("SIGTERM", "SIGKILL"):
            record = [r for r in self.stop_records() if r["type"] == "command_stop_signal" and r["signal"] == name]
            self.assertIn(changing, [item["pid"] for item in record[0]["failed"]],
                          f"the skipped member is recorded at {name}")
        self.assertEqual(result["status"], "unknown", "a member that could not be verified is not claimed gone")
        self.assertTrue(alive(changing))
        self.assertFalse(alive(steady) or alive(main))
        self.assert_safe(family)
        self.reap(changing)

    def test_each_stage_opens_its_own_pins(self):
        # SPEC invariant 2: re-pin per stage; a pin never lives across a stage wait
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 746.1'")
        (member,) = self.find("sleep", "746.1")
        family = self.family(member)
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(self.env.attempts_on(member), [signal.SIGINT, signal.SIGTERM, signal.SIGKILL])
        self.assertGreaterEqual(self.env.opens.count(member), 3, "one pidfd per stage for the same member")
        self.assert_safe(family)


# ================================================================================================
# backend shutdown
# ================================================================================================
class BackendShutdownTests(EscRig):
    def test_closing_the_service_mid_episode_ends_it_unknown_backend_shutdown_without_further_signals(self):
        # A long SIGINT stage: "ends at once" is told from "ends at the stage end" by a wide margin on both sides, so a
        # loaded machine cannot blur it (the old 1.0 s bound sat next to a 1.5 s stage).
        stage = dataclasses.replace(POL, sigint_wait=4.0)
        self.command_stop.policy = stage
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 747.1'")
        (member,) = self.find("sleep", "747.1")
        family = self.family(member)
        thread, box = self.stop_in_background(started["command_id"])
        self.assertTrue(self.env.wait_for(signal.SIGINT, 30))
        closed_at = time.monotonic()
        self.command_stop.close()
        thread.join(30)
        self.assertIn("result", box, "close ends the waiting call quickly")
        self.assertLess(time.monotonic() - closed_at, stage.sigint_wait / 2, "ends at once, not at the stage end")
        result = box["result"]
        self.assertEqual((result["status"], result["reason"]), ("unknown", "backend_shutdown"), result)
        before = list(self.env.attempts)
        time.sleep(stage.sigint_wait + 0.3)  # past the end of the stage the episode was in
        self.assertEqual(self.env.attempts, before, "no signal leaves after the shutdown")
        self.assertEqual(self.names(result), ["SIGINT"])
        self.assertTrue(alive(member), "shutdown does not kill the command")
        refused = self.command_stop.stop("worker", started["command_id"])
        self.assertEqual((refused["status"], refused["reason"]), ("refused", "backend_shutdown"), refused)
        self.assertEqual(self.env.attempts, before)
        self.assert_safe(family)
        self.reap(member)


if __name__ == "__main__":
    unittest.main()
