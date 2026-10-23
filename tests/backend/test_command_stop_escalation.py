"""CS-03: the stop episode escalates SIGINT -> SIGTERM -> SIGKILL and checks what is left.

Seam S2: a real PTY ``ShellPane`` + ``HostShellPort`` + ``TerminalService`` + ``CommandStopService`` (the
``test_command_stop_terminal`` pattern), with S7 error injection where the real kernel cannot be made to
misbehave on demand.

Expected values are this test's own: the reduced policy constants it passes, the exit code 128+n that the
command itself produces, the moments a command logs its own signal receipt, and pid life/death read from
``/proc``. Nothing is derived from the production formatter, and no clock is mocked: the timing assertions are
lower bounds (and generous upper bounds) taken from the constants, measured in real time.

Every process here is one the test started inside its own host shell (the pane's process group dies with it),
except ``setsid`` survivors, which escape the pane and are killed by the test's own cleanup.
"""

from __future__ import annotations

import dataclasses
import errno
import os
from pathlib import Path
import signal
import threading
import time
import unittest
from uuid import uuid4

from workbench.backend.command_stop import CommandStopService, LinuxStopEnvironment, StopPolicy
from workbench.backend.flow_terminal import TerminalService
from workbench.backend.panes import HostShellPort
from workbench.contracts.v1 import ActorRole

from test_command_stop_terminal import RealStopFixture
from command_stop_support import alive as proc_exists, pids_running, stat_fields as proc_fields
from test_flow_terminal import AUTOMATION, SECRET, journal, wait_until

# The test's own reduced policy (the production 10/5/5 s defaults are confirmed once, in CS-10).
POLICY = StopPolicy(identity_wait=2.0, sigint_wait=1.0, sigterm_wait=1.0, sigkill_wait=1.0, call_wait=10.0)
TOLERANCE = 1.5  # how late a stage may be in real time on a loaded machine


def kill_same_process(pid: int, start_ticks: int) -> None:
    """SIGKILL ``pid`` only while it is still the live process that had ``start_ticks`` (pidfd, re-checked)."""
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if proc_exists(pid, start_ticks):
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(fd)


class EscalationFixture(RealStopFixture):
    """``RealStopFixture`` with this ticket's reduced policy."""

    def make_terminal(self, host_shell, **extra):
        previous = getattr(self, "command_stop", None)
        if previous is not None:
            previous.close()
        self.command_stop = CommandStopService(journal=self.handoffs.record, policy=POLICY, env=self.stop_env)
        return TerminalService(handoffs=self.handoffs, host_shell=host_shell, gate=self.gate,
                               log_root=self.root / "workflow" / "terminal",
                               automation=lambda: AUTOMATION, paused=lambda: self.paused,
                               activity=lambda: self.activity, sensitive_values=lambda: (SECRET,),
                               poll_interval=0.02, command_stop=self.command_stop, **extra)

    def kill_at_cleanup(self, *pids):
        """The test's own processes: whatever of them is still alive is killed when the test ends.

        Each pid is remembered with its start ticks now, and only the process that still has those ticks is signalled
        (through a pidfd that is re-checked), so a pid the kernel reused for another process is never killed.
        """
        started = {}
        for pid in pids:
            try:
                started[pid] = int(proc_fields(pid)[19])
            except (OSError, IndexError, ValueError):
                pass  # already gone: nothing to kill later
        self.addCleanup(lambda: [kill_same_process(pid, ticks) for pid, ticks in started.items()])

    def signal_names(self, result):
        return [item["signal"] for item in result["signals"]]


class IgnoredSigintEscalatesToSigtermTests(EscalationFixture):
    def test_a_command_that_ignores_sigint_ends_at_sigterm_after_the_sigint_stage(self):
        # Behavior 1
        started = self.run_command("bash -c 'trap \"\" INT; sleep 610.1'")
        self.assertEqual(started["status"], "running", started)
        sleepers = pids_running("sleep", "610.1")
        self.assertEqual(len(sleepers), 1, sleepers)
        self.kill_at_cleanup(*sleepers)
        began = time.monotonic()
        result = self.stop(started["command_id"])
        elapsed = time.monotonic() - began
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"], result["signal_name"]), (128 + 15, 15, "SIGTERM"))
        self.assertEqual(self.signal_names(result), ["SIGINT", "SIGTERM"])
        self.assertGreaterEqual(elapsed, POLICY.sigint_wait, "SIGTERM waited for the whole SIGINT stage")
        self.assertLess(elapsed, POLICY.sigint_wait + POLICY.sigterm_wait + TOLERANCE)
        self.assertEqual(result["residual"]["alive"], [])
        self.assertFalse(proc_exists(sleepers[0]))

    def test_the_command_receives_sigterm_one_sigint_stage_after_sigint(self):
        # Behavior 1, the gap read from the command's own log
        log = self.root / "received.log"
        script = self.root / "logger.sh"
        script.write_text("trap 'echo INT $EPOCHREALTIME >> \"$1\"' INT\n"
                          "trap 'echo TERM $EPOCHREALTIME >> \"$1\"; exit 143' TERM\n"
                          "echo ready >> \"$1\"\n"
                          "while :; do sleep 100 & wait $!; done\n")
        started = self.run_command(f"bash {script} {log}")
        self.assertEqual(started["status"], "running", started)
        self.assertTrue(wait_until(lambda: log.exists() and "ready" in log.read_text(), 30))
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["exit_code"], result["signal"]), ("exited", 143, None), result)
        received = {line.split()[0]: float(line.split()[1])
                    for line in log.read_text().splitlines() if line.split()[0] in ("INT", "TERM")}
        gap = received["TERM"] - received["INT"]
        self.assertGreaterEqual(gap, POLICY.sigint_wait, received)
        self.assertLess(gap, POLICY.sigint_wait + TOLERANCE, received)


class IgnoredSigtermEscalatesToSigkillTests(EscalationFixture):
    def test_a_command_that_ignores_sigint_and_sigterm_is_killed_within_the_policy_total(self):
        # Behavior 2
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 611.1'")
        self.assertEqual(started["status"], "running", started)
        sleepers = pids_running("sleep", "611.1")
        self.assertEqual(len(sleepers), 1, sleepers)
        self.kill_at_cleanup(*sleepers)
        began = time.monotonic()
        result = self.stop(started["command_id"])
        elapsed = time.monotonic() - began
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"], result["signal_name"]), (128 + 9, 9, "SIGKILL"))
        self.assertEqual(self.signal_names(result), ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertGreaterEqual(elapsed, POLICY.sigint_wait + POLICY.sigterm_wait, "both earlier stages were waited out")
        stage_total = POLICY.sigint_wait + POLICY.sigterm_wait + POLICY.sigkill_wait
        self.assertLess(elapsed, stage_total + TOLERANCE, "within the policy total")
        self.assertFalse(proc_exists(sleepers[0]))
        stages = [(item["signal"], item["stage"], item["auxiliary"]) for item in result["signals"]]
        self.assertEqual(stages, [("SIGINT", "sigint", False), ("SIGTERM", "sigterm", False),
                                  ("SIGKILL", "sigkill", False)])
        records = [r for r in journal(self.root) if r["type"] == "command_stop_signal"]
        self.assertEqual([r["signal"] for r in records], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertTrue(all(r["at"] and r["targets"] for r in records), records)


class DescendantsAreSignalledTests(EscalationFixture):
    def test_every_descendant_of_an_ignoring_command_ends_and_nothing_is_left(self):
        # Behavior 3
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 612.1 & sleep 612.2 & wait'")
        self.assertEqual(started["status"], "running", started)
        sleepers = pids_running("sleep", "612.1") + pids_running("sleep", "612.2")
        self.assertEqual(len(sleepers), 2, sleepers)
        self.kill_at_cleanup(*sleepers)
        main = int(proc_fields(sleepers[0])[1])  # their parent: the bash that ignores INT and TERM
        self.assertEqual(int(proc_fields(sleepers[1])[1]), main)
        self.kill_at_cleanup(main)
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"]), (128 + 9, 9))
        self.assertEqual(self.signal_names(result), ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertEqual(result["residual"]["alive"], [])
        self.assertFalse(any(proc_exists(pid) for pid in (main, *sleepers)), "main and both descendants are gone")
        reached = {target["pid"] for item in result["signals"] for target in item["targets"]}
        self.assertTrue({main, *sleepers} <= reached, (reached, main, sleepers))
        for item in result["signals"]:  # each stage reached the whole tracked set again
            self.assertTrue({main, *sleepers} <= {t["pid"] for t in item["targets"]}, item)


class DescendantAfterMainEndedTests(EscalationFixture):
    def test_the_stages_apply_to_a_descendant_that_outlives_the_main_command(self):
        # Behavior 4
        main_file = self.root / "main.pid"
        started = self.run_command(f"bash -c 'echo $$ > {main_file}; (trap \"\" INT; sleep 613.1) & exit 0'")
        self.assertEqual(started["status"], "running", "a descendant keeps the run open after the main exit")
        main = int(main_file.read_text())
        self.assertTrue(wait_until(lambda: not proc_exists(main), 30), "the main command has ended")
        sleepers = pids_running("sleep", "613.1")
        self.assertEqual(len(sleepers), 1, sleepers)
        self.kill_at_cleanup(*sleepers)
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"]), (0, None), "the main command's own exit")
        self.assertEqual(self.signal_names(result), ["SIGINT", "SIGTERM"], "the sleeper ignores only SIGINT")
        for item in result["signals"]:  # every signal went out after the main command ended, to the descendant
            self.assertEqual([t["pid"] for t in item["targets"]], sleepers, item)
        self.assertEqual(result["residual"]["alive"], [])
        self.assertFalse(proc_exists(sleepers[0]))


class LeftSessionDescendantTests(EscalationFixture):
    def test_a_descendant_in_another_session_is_reported_never_signalled_and_the_stop_is_unknown(self):
        # Behavior 5
        started = self.run_command("bash -c 'setsid sleep 615.1 & exit 0'")
        self.assertEqual(started["status"], "running", "the escaped descendant keeps the run open")
        sleepers = pids_running("sleep", "615.1")
        self.assertEqual(len(sleepers), 1, sleepers)
        self.kill_at_cleanup(*sleepers)
        fields = proc_fields(sleepers[0])
        self.assertNotEqual(int(fields[3]), self.pane.state["parent_pid"], "it really left the host shell session")
        ticks, session = int(fields[19]), int(fields[3])
        began = time.monotonic()
        result = self.stop(started["command_id"])
        elapsed = time.monotonic() - began
        self.assertEqual((result["status"], result["reason"]), ("unknown", "left_session_descendants"), result)
        self.assertLess(elapsed, POLICY.sigint_wait, "no stage is waited out: nothing can be signalled")
        self.assertEqual(result["signals"], [], "the descendant got no signal")
        self.assertEqual(result["residual"]["left_session"], [{"pid": sleepers[0], "start_ticks": ticks,
                                                               "session": session}])
        self.assertEqual(result["residual"]["alive"], [])
        self.assertTrue(result["residual"]["unchecked"])
        time.sleep(0.3)
        self.assertTrue(proc_exists(sleepers[0], ticks), "the descendant is alive and untouched")
        types = [r["type"] for r in journal(self.root) if r["type"].startswith("command_stop")]
        self.assertNotIn("command_stop_signal", types)
        self.assertEqual(self.query()["status"], "unknown", "the owner has not seen the end")
        os.kill(sleepers[0], signal.SIGKILL)  # the test's own process
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 30))
        self.assert_user_owns_the_shell_again()


class StoppedMemberTests(EscalationFixture):
    def test_a_stopped_member_is_continued_as_an_auxiliary_signal_and_the_command_ends(self):
        # Behavior 6
        started = self.run_command("sleep 616.1")
        self.assertEqual(started["status"], "running", started)
        sleepers = pids_running("sleep", "616.1")
        self.assertEqual(len(sleepers), 1, sleepers)
        self.kill_at_cleanup(*sleepers)
        os.kill(sleepers[0], signal.SIGSTOP)  # the test's own process
        self.assertTrue(wait_until(lambda: proc_fields(sleepers[0])[0] == "T", 30), "it is stopped")
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"]), (130, 2), result)
        self.assertEqual(self.signal_names(result), ["SIGINT", "SIGCONT"])
        self.assertEqual([item["auxiliary"] for item in result["signals"]], [False, True])
        self.assertEqual([t["pid"] for t in result["signals"][1]["targets"]], sleepers,
                         "only the stopped member is continued")
        self.assertEqual(result["residual"]["alive"], [])
        self.assertFalse(proc_exists(sleepers[0]))
        records = [r for r in journal(self.root) if r["type"] == "command_stop_signal"]
        self.assertEqual([(r["signal"], r["auxiliary"]) for r in records], [("SIGINT", False), ("SIGCONT", True)])


class EveryStageReadsTheMembersAgainTests(EscalationFixture):
    def test_a_descendant_born_after_sigint_gets_the_later_stages(self):
        spawned = self.root / "spawned.pids"
        script = self.root / "spawner.sh"
        script.write_text("trap 'sleep 619.2 & echo $! >> \"$1\"' INT\n"
                          "trap '' TERM\n"
                          "echo ready >> \"$1.ready\"\n"
                          "while :; do sleep 100 & wait $!; done\n")
        started = self.run_command(f"bash {script} {spawned}")
        self.assertEqual(started["status"], "running", started)
        self.assertTrue(wait_until(lambda: (self.root / "spawned.pids.ready").exists(), 30))
        result = self.stop(started["command_id"])
        born = [int(line) for line in spawned.read_text().split()]
        self.assertEqual(len(born), 1, "the command spawned one descendant when it got SIGINT")
        self.kill_at_cleanup(*born)
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(self.signal_names(result), ["SIGINT", "SIGTERM", "SIGKILL"])
        targets = [{t["pid"] for t in item["targets"]} for item in result["signals"]]
        self.assertNotIn(born[0], targets[0], "it did not exist at the SIGINT stage")
        self.assertIn(born[0], targets[1], "the SIGTERM stage found it")
        self.assertIn(born[0], targets[2], "and so did the SIGKILL stage")
        self.assertFalse(proc_exists(born[0]))
        self.assertEqual(result["residual"]["alive"], [])


class SendFailureEnvironment(LinuxStopEnvironment):
    """S7: a signal to a chosen pid fails the way the kernel would report it (``errors``: pid -> exception).

    ``signals`` limits the failure to those signal numbers; ``once`` makes it a single failure. Every attempt is
    recorded as (pid, signal number); a signal that is not made to fail really goes out.
    """

    def __init__(self):
        self.fds, self.errors, self.signals, self.once, self.attempts = {}, {}, None, False, []

    def pidfd_open(self, pid):
        fd = super().pidfd_open(pid)
        self.fds[fd] = pid
        return fd

    def pidfd_send_signal(self, fd, signum):
        pid = self.fds.get(fd)
        self.attempts.append((pid, signum))
        if pid in self.errors and (self.signals is None or signum in self.signals):
            error = self.errors.pop(pid) if self.once else self.errors[pid]
            raise error
        super().pidfd_send_signal(fd, signum)

    def close(self, fd):
        self.fds.pop(fd, None)
        super().close(fd)


class SendFailureFixture(EscalationFixture):
    def make_stop_env(self):
        return SendFailureEnvironment()

    def records(self, kind="command_stop_signal"):
        return [r for r in journal(self.root) if r["type"] == kind]


class PermissionDeniedTests(SendFailureFixture):
    def test_every_member_refusing_the_signal_ends_the_stop_at_once_as_permission_denied(self):
        started = self.run_command("sleep 620.1")
        sleepers = pids_running("sleep", "620.1")
        self.assertEqual(len(sleepers), 1, sleepers)
        self.kill_at_cleanup(*sleepers)
        self.stop_env.errors[sleepers[0]] = PermissionError(errno.EPERM, "denied")
        began = time.monotonic()
        result = self.stop(started["command_id"])
        elapsed = time.monotonic() - began
        self.assertEqual((result["status"], result["reason"]), ("unknown", "permission_denied"), result)
        self.assertLess(elapsed, POLICY.sigint_wait, "no stage is waited out")
        self.assertEqual(self.stop_env.attempts, [(sleepers[0], signal.SIGINT)], "one attempt, no escalation")
        self.assertEqual(result["signals"], [], "nothing was delivered")
        record = self.records()[0]
        self.assertEqual((record["signal"], record["targets"], record["failed"]),
                         ("SIGINT", [], [{"pid": sleepers[0], "reason": "permission_denied"}]))
        self.assertTrue(proc_exists(sleepers[0]))
        os.kill(sleepers[0], signal.SIGKILL)  # the test's own process
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 30))

    def test_every_member_refusing_a_later_stage_ends_the_stop_at_that_stage(self):
        started = self.run_command("bash -c 'trap \"\" INT; sleep 620.2'")
        sleepers = pids_running("sleep", "620.2")
        self.assertEqual(len(sleepers), 1, sleepers)
        self.kill_at_cleanup(*sleepers)
        self.stop_env.errors[sleepers[0]] = PermissionError(errno.EPERM, "denied")
        self.stop_env.signals = {signal.SIGTERM, signal.SIGKILL}
        began = time.monotonic()
        result = self.stop(started["command_id"])
        elapsed = time.monotonic() - began
        self.assertEqual((result["status"], result["reason"]), ("unknown", "permission_denied"), result)
        self.assertEqual(self.signal_names(result), ["SIGINT"])
        self.assertGreaterEqual(elapsed, POLICY.sigint_wait)
        self.assertLess(elapsed, POLICY.sigint_wait + POLICY.sigterm_wait, "the SIGTERM stage is not waited out")
        self.assertEqual([signum for _pid, signum in self.stop_env.attempts], [signal.SIGINT, signal.SIGTERM])
        self.assertEqual([r["signal"] for r in self.records()], ["SIGINT", "SIGTERM"])
        os.kill(sleepers[0], signal.SIGKILL)  # the test's own process
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 30))

    def test_one_member_refusing_does_not_stop_the_others_and_is_recorded_per_stage(self):
        started = self.run_command("sleep 621.1 & sleep 621.2 & wait")
        refusing, other = pids_running("sleep", "621.1"), pids_running("sleep", "621.2")
        self.assertEqual((len(refusing), len(other)), (1, 1))
        self.kill_at_cleanup(*refusing, *other)
        self.stop_env.errors[refusing[0]] = PermissionError(errno.EPERM, "denied")
        result = self.stop(started["command_id"])
        # the other member is gone after SIGTERM; at the SIGKILL stage only the refusing member is left: all EPERM
        self.assertEqual((result["status"], result["reason"]), ("unknown", "permission_denied"), result)
        self.assertEqual(self.signal_names(result), ["SIGINT", "SIGTERM"])
        self.assertTrue(proc_exists(refusing[0]), "the refusing member is still there")
        self.assertFalse(proc_exists(other[0]), "the others were signalled through every stage")
        self.assertEqual([r["signal"] for r in self.records()], ["SIGINT", "SIGTERM", "SIGKILL"])
        for record in self.records():
            self.assertIn({"pid": refusing[0], "reason": "permission_denied"}, record["failed"], record)
            self.assertNotIn(refusing[0], [t["pid"] for t in record["targets"]], record)
        self.assertEqual(self.records()[2]["targets"], [], "SIGKILL: the only member left refused")
        self.assertIn(other[0], [t["pid"] for t in self.records()[1]["targets"]])
        self.assertEqual([r["pid"] for r in result["residual"]["alive"]], refusing)
        os.kill(refusing[0], signal.SIGKILL)  # the test's own process
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 30))

    def test_a_member_that_vanished_before_the_signal_is_recorded_and_the_others_still_get_it(self):
        started = self.run_command("sleep 622.1 & sleep 622.2 & wait")
        gone, other = pids_running("sleep", "622.1"), pids_running("sleep", "622.2")
        self.assertEqual((len(gone), len(other)), (1, 1))
        self.kill_at_cleanup(*gone, *other)
        self.stop_env.errors[gone[0]] = ProcessLookupError(errno.ESRCH, "no such process")
        self.stop_env.once = True
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        first = result["signals"][0]
        self.assertEqual(first["signal"], "SIGINT")
        self.assertIn({"pid": gone[0], "reason": "ended"}, first["failed"], "ESRCH is recorded for that member")
        self.assertNotIn(gone[0], [t["pid"] for t in first["targets"]])
        self.assertIn(other[0], [t["pid"] for t in first["targets"]], "the other member still got the signal")
        self.assertIn({"pid": gone[0], "reason": "ended"}, self.records()[0]["failed"])


class BackendShutdownTests(EscalationFixture):
    def test_closing_the_service_during_a_stage_ends_the_stop_as_backend_shutdown_without_further_signals(self):
        # A long SIGINT stage: "ends at once" is told from "ends at the stage end" by a wide margin on both sides, so a
        # loaded machine cannot blur it (the old 0.5 s bound sat next to the ~0.8 s left of a 1.0 s stage).
        stage = dataclasses.replace(POLICY, sigint_wait=4.0)
        self.command_stop.policy = stage
        started = self.run_command("bash -c 'trap \"\" INT TERM; sleep 623.1'")
        sleepers = pids_running("sleep", "623.1")
        self.assertEqual(len(sleepers), 1, sleepers)
        self.kill_at_cleanup(*sleepers)
        box = {}
        thread = threading.Thread(target=lambda: box.update(result=self.stop(started["command_id"])), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.assertTrue(wait_until(lambda: self.query()["status"] == "stopping", 30))
        time.sleep(0.2)
        began = time.monotonic()
        self.command_stop.close()
        thread.join(30)
        result = box["result"]
        self.assertEqual((result["status"], result["reason"]), ("unknown", "backend_shutdown"), result)
        self.assertLess(time.monotonic() - began, stage.sigint_wait / 2, "the episode ends at once, not at the stage end")
        self.assertEqual(self.signal_names(result), ["SIGINT"], "nothing more was sent after the close")
        time.sleep(stage.sigint_wait)  # the whole stage the episode was in: SIGTERM would have been due by now
        self.assertTrue(proc_exists(sleepers[0]), "it was never escalated")
        os.kill(sleepers[0], signal.SIGKILL)  # the test's own process
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 30))


class EndedWhileFlippingPort(HostShellPort):
    """The port reports the run as ended once ``state['ended']`` is set (the process is still there)."""

    state = {"ended": False}

    def run_identity(self):
        identity = super().run_identity()
        if identity is not None and self.state["ended"]:
            return {**identity, "phase": "ended", "supervisor": None, "child": None, "parent": None}
        return identity


class AfterFirstSignalEnvironment(LinuxStopEnvironment):
    def __init__(self, state):
        self.state = state

    def pidfd_send_signal(self, fd, signum):
        super().pidfd_send_signal(fd, signum)
        self.state["ended"] = True


class EndedIdentityIsNeverScannedTests(EscalationFixture):
    def make_stop_env(self):
        self.flip = {"ended": False}
        return AfterFirstSignalEnvironment(self.flip)

    def test_later_stages_and_the_residual_use_the_last_running_identity(self):
        self.terminal.close()
        port_state = self.flip
        self.terminal = self.make_terminal(lambda: type("Port", (EndedWhileFlippingPort,), {"state": port_state})(
            self.pane, lambda: self.pane), **self.terminal_extras())
        started = self.run_command("bash -c 'trap \"\" INT; sleep 624.1'")
        self.assertEqual(started["status"], "running", started)
        sleepers = pids_running("sleep", "624.1")
        self.assertEqual(len(sleepers), 1, sleepers)
        self.kill_at_cleanup(*sleepers)
        result = self.stop(started["command_id"])
        self.assertTrue(self.flip["ended"], "the owner's identity read ended after the first signal")
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(self.signal_names(result), ["SIGINT", "SIGTERM"], "the SIGTERM stage still found the member")
        self.assertEqual([t["pid"] for t in result["signals"][1]["targets"]], sleepers)
        self.assertTrue(result["residual"]["checked"], "the residual was read with the last running identity")
        self.assertEqual(result["residual"]["alive"], [])


class ShiftedMemberEnvironment(SendFailureEnvironment):
    """S7: PID reuse for the pids in ``shifted``: from their ``after_reads``-th read on they show other start ticks."""

    def __init__(self, after_reads=1):
        super().__init__()
        self.shifted, self.after_reads, self.reads = set(), after_reads, {}

    def read_stat(self, pid):
        stat = super().read_stat(pid)
        if stat is None or pid not in self.shifted:
            return stat
        self.reads[pid] = self.reads.get(pid, 0) + 1
        if self.reads[pid] <= self.after_reads:
            return stat
        return dataclasses.replace(stat, start_ticks=stat.start_ticks + 1)


class MismatchedMemberStaysSkippedTests(EscalationFixture):
    def make_stop_env(self):
        return ShiftedMemberEnvironment(after_reads=1)

    def test_a_member_whose_identity_failed_verification_is_not_signalled_by_a_later_stage(self):
        started = self.run_command("sleep 625.1 & sleep 625.2 & wait")
        changed, other = pids_running("sleep", "625.1"), pids_running("sleep", "625.2")
        self.assertEqual((len(changed), len(other)), (1, 1))
        self.kill_at_cleanup(*changed, *other)
        self.stop_env.shifted.add(changed[0])  # the scan reads it right, the pin does not (then it stays shifted)
        result = self.stop(started["command_id"])
        attempted = {pid for pid, _signum in self.stop_env.attempts}
        self.assertNotIn(changed[0], attempted, "no stage signalled the member whose identity did not verify")
        self.assertIn(other[0], attempted)
        self.assertEqual(self.signal_names(result), ["SIGINT", "SIGTERM"], result)
        self.assertEqual(result["status"], "unknown", "the unverified member is still there")
        self.assertIn(changed[0], [item["pid"] for item in result["residual"]["alive"]])
        self.assertTrue(proc_exists(changed[0]))
        self.assertFalse(proc_exists(other[0]))
        os.kill(changed[0], signal.SIGKILL)  # the test's own process
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 30))


if __name__ == "__main__":
    unittest.main()
