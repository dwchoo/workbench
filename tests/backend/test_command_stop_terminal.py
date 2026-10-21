"""CS-02: the worker stops its own terminal command with ``terminal {command: null, stop: <command_id>}``.

Seam S2: a real PTY ``ShellPane`` + ``HostShellPort`` + ``TerminalService`` + ``CommandStopService`` (the
``test_flow_terminal`` pattern); argument validation runs on fixtures; the identity-mismatch case injects a
``StopEnvironment`` (S7).

Expected values are this test's own: the exit code 128+2 of the ``sleep`` it started, pid disappearance read
from ``/proc``, the policy constants it passes, and the command_id it was given. Nothing is derived from the
production formatter. Every process here is one the test started inside its own host shell; the host shell
pane is closed (and its process group with it) on teardown.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import unittest
from uuid import uuid4

from workbench.backend.command_stop import CommandStopService, LinuxStopEnvironment, StopPolicy
from workbench.backend.flow_terminal import TerminalService, validate_terminal_arguments
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.contracts.v1 import ActorRole
from workbench.terminal.shell_g2.prototype import ShellChoice

from test_flow_terminal import AUTOMATION, SECRET, ServiceFixture, call, journal, wait_until

# The test's own policy constants (a reduced StopPolicy; the production defaults are checked elsewhere).
REDUCED = StopPolicy(identity_wait=2.0, sigint_wait=2.0, sigterm_wait=1.0, sigkill_wait=1.0, call_wait=8.0)


def proc_exists(pid: int, start_ticks: int | None = None) -> bool:
    """True while ``pid`` is a live (non-zombie) process, read from /proc by the test itself."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except OSError:
        return False
    if fields[0] in ("Z", "X"):
        return False
    return start_ticks is None or int(fields[19]) == start_ticks


class StopArgumentValidationTests(unittest.TestCase):
    """Behavior 1: ``validate_terminal_arguments`` and the optional ``stop`` field."""

    COMMAND_ID = str(uuid4())

    def test_stop_with_a_null_command_is_valid(self):
        self.assertEqual(validate_terminal_arguments({"command": None, "stop": self.COMMAND_ID}), [])

    def test_stop_alone_is_valid(self):
        self.assertEqual(validate_terminal_arguments({"stop": self.COMMAND_ID}), [])

    def test_null_and_empty_stop_mean_not_used(self):
        for stop in (None, "", "  "):
            self.assertEqual(validate_terminal_arguments({"command": "ls", "stop": stop}), [], stop)
            self.assertEqual(validate_terminal_arguments({"command": None, "stop": stop}), [], stop)

    def test_stop_together_with_a_command_string_is_refused_naming_fields_only(self):
        command = f"echo {SECRET}"
        errors = validate_terminal_arguments({"command": command, "stop": self.COMMAND_ID})
        self.assertTrue(errors)
        text = " ".join(errors)
        self.assertIn("stop", text)
        self.assertIn("command", text)
        self.assertNotIn("unknown field", text, "stop is a known field; the refusal is about the combination")
        self.assertNotIn(self.COMMAND_ID, text, "the value sent is never echoed")
        self.assertNotIn(SECRET, text)

    def test_a_blank_command_string_is_not_used_so_stop_goes_ahead(self):
        for command in ("", "   ", "\n"):
            self.assertEqual(validate_terminal_arguments({"command": command, "stop": self.COMMAND_ID}), [], command)

    def test_only_the_lowercase_canonical_uuid_is_accepted_for_stop(self):
        canonical = "0f8fad5b-d9cb-469f-a165-70867728950e"
        self.assertEqual(validate_terminal_arguments({"command": None, "stop": canonical}), [])
        for stop in (canonical.upper(), "{" + canonical + "}", "urn:uuid:" + canonical, canonical.replace("-", ""),
                     " " + canonical):
            errors = validate_terminal_arguments({"command": None, "stop": stop})
            self.assertTrue(any(error.startswith("stop") for error in errors), (stop, errors))
            self.assertNotIn("unknown field", " ".join(errors))
            self.assertNotIn(canonical, " ".join(errors), "the value sent is never echoed")

    def test_a_stop_that_is_not_a_command_id_is_refused_without_its_value(self):
        for stop in (5, ["x"], "not-a-uuid"):
            errors = validate_terminal_arguments({"command": None, "stop": stop})
            self.assertTrue(any(error.startswith("stop") for error in errors), (stop, errors))
            self.assertNotIn("not-a-uuid", " ".join(errors))
            self.assertNotIn("unknown field", " ".join(errors))

    def test_unknown_fields_are_still_refused(self):
        errors = validate_terminal_arguments({"command": None, "stop": self.COMMAND_ID, "timeout_seconds": 60})
        self.assertTrue(any(error.startswith("timeout_seconds") for error in errors), errors)
        self.assertFalse(any(error.startswith("stop") for error in errors), errors)

    def test_a_plain_command_is_unchanged(self):
        self.assertEqual(validate_terminal_arguments({"command": "ls"}), [])
        self.assertEqual(validate_terminal_arguments({"command": None}), [])


ENV_VALUE = "exported-value-for-the-stop-test"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def proc_fields(pid: int) -> list[str]:
    """Fields 3.. of /proc/<pid>/stat, parsed by the test: [0] state, [1] ppid, [2] pgrp, [3] session, [19] start ticks."""
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()


def pids_running(*argv: str) -> list[int]:
    """Live pids of this uid whose argv equals ``argv``, read from /proc by the test (production never uses argv)."""
    wanted = b"\0".join(item.encode() for item in argv) + b"\0"
    found = []
    for name in os.listdir("/proc"):
        if not name.isdecimal():
            continue
        try:
            if Path(f"/proc/{name}/cmdline").read_bytes() == wanted and proc_exists(int(name)):
                found.append(int(name))
        except OSError:
            continue
    return found


class RealStopFixture(ServiceFixture):
    """A real backend ShellPane through HostShellPort, a TerminalService and a CommandStopService."""

    CHOICE = ShellChoice("bash", "/usr/bin/bash")
    def make_stop_env(self):
        """A StopEnvironment for the CommandStopService (S7), or None for the real one."""
        return None

    def make_terminal(self, host_shell, **extra):
        previous = getattr(self, "command_stop", None)
        if previous is not None:
            previous.close()
        self.command_stop = CommandStopService(journal=self.handoffs.record, policy=REDUCED, env=self.stop_env)
        return TerminalService(handoffs=self.handoffs, host_shell=host_shell, gate=self.gate,
                               log_root=self.root / "workflow" / "terminal",
                               automation=lambda: AUTOMATION, paused=lambda: self.paused,
                               activity=lambda: self.activity, sensitive_values=lambda: (SECRET,),
                               poll_interval=0.02, command_stop=self.command_stop, **extra)

    def setUp(self):
        self.stop_env = self.make_stop_env()
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self.pane = ShellPane(self.CHOICE,
                              {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8",
                               "WB_STOP_FIXTURE": ENV_VALUE})
        self.ui = bytearray()
        self.halt = threading.Event()
        self.loop = threading.Thread(target=self._backend_loop, daemon=True)
        self.loop.start()
        self.terminal.close()
        self.terminal = self.make_terminal(lambda: HostShellPort(self.pane, lambda: self.pane),
                                           **self.terminal_extras())
        self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
        self.user_types(f"cd {self.home}\r".encode())
        self.assertTrue(wait_until(lambda: self.pane.cwd() == str(self.home), 5))
        self.assertTrue(wait_until(self.idle, 5))

    def terminal_extras(self):
        return {}

    def _backend_loop(self):
        while not self.halt.is_set():
            for chunk in self.pane.pump():
                self.ui.extend(chunk.data)
            time.sleep(0.01)

    def tearDown(self):
        self.terminal.close()
        self.command_stop.close()
        self.halt.set()
        self.loop.join(5)
        self.pane.close()
        super().tearDown()

    def user_types(self, data):
        self.assertIsNone(self.pane.admit(data))

    def idle(self):
        port = HostShellPort(self.pane, lambda: self.pane)
        try:
            return port.busy() is None
        finally:
            port.detach()

    def run_command(self, command, wait=1):
        self.terminal._wait_seconds = wait
        return self.terminal.handle(ActorRole.WORKER, call({"command": command}))

    def query(self):
        return self.terminal.handle(ActorRole.WORKER, call({"command": None}))

    def stop(self, command_id):
        return self.terminal.handle(ActorRole.WORKER, call({"command": None, "stop": command_id}))

    def assert_user_owns_the_shell_again(self):
        self.assertTrue(wait_until(lambda: self.pane.state["input_owner"] == "user"
                                   and self.pane.state["parent_mode"] == "manual_prompt", 10), self.pane.state)
        self.assertIsNone(self.pane.automation_hold)
        self.assertIsNone(self.gate.owner)
        marker = f"user-typed-{uuid4().hex[:6]}"
        self.user_types(f"echo {marker}\r".encode())
        self.assertTrue(wait_until(lambda: bytes(self.ui).count(marker.encode()) >= 2, 5), "the user regains input")


class StopRunningCommandTests(RealStopFixture):
    def test_stop_ends_a_running_sleep_with_sigint_and_reports_the_final_state(self):
        # Behavior 2
        started = self.run_command("sleep 601.2")
        self.assertEqual(started["status"], "running", started)
        command_id = started["command_id"]
        pids = pids_running("sleep", "601.2")
        self.assertEqual(len(pids), 1, pids)
        result = self.stop(command_id)
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"], result["signal_name"]), (128 + 2, 2, "SIGINT"))
        self.assertEqual(result["command_id"], command_id)
        self.assertEqual(result["requested_by"], "worker")
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT"])
        self.assertEqual(result["residual"]["alive"], [])
        self.assertTrue(result["residual"]["unchecked"])
        self.assertFalse(proc_exists(pids[0]), "the command's process is gone")


    # A command that answers SIGINT only after a second, so the stop is observably under way.
    SLOW_INT = "trap 'sleep 1; exit 130' INT; echo ready-for-int; while :; do sleep 0.2; done"

    def stop_in_background(self, command_id):
        box = {}
        thread = threading.Thread(target=lambda: box.update(result=self.stop(command_id)), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        return thread, box

    def test_a_stop_call_with_a_blank_command_string_stops_the_command(self):
        started = self.run_command("sleep 601.9")
        result = self.terminal.handle(ActorRole.WORKER,
                                      call({"command": "", "stop": started["command_id"]}))
        self.assertEqual((result["status"], result["exit_code"], result["signal"]), ("exited", 130, 2), result)

    def test_the_running_result_tells_the_worker_how_to_stop_its_command(self):
        started = self.run_command(self.SLOW_INT)
        self.assertEqual(started["status"], "running", started)
        self.assertIn("stop", started["detail"])
        self.assertIn("End your turn", started["detail"], "the existing guidance stays")
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")  # end the test's own command

    def test_while_the_call_waits_the_command_reads_as_stopping_and_the_host_terminal_stays_occupied(self):
        # Behavior 3
        started = self.run_command(self.SLOW_INT)
        self.assertEqual(started["status"], "running", started)
        command_id = started["command_id"]
        thread, box = self.stop_in_background(command_id)
        self.assertTrue(wait_until(lambda: self.query()["status"] == "stopping", 5), "a query sees stopping")
        seen = self.query()
        self.assertEqual(seen["command_id"], command_id)
        self.assertEqual(self.terminal.current()["state"], "stopping")
        watch = self.terminal.watch_state()
        self.assertEqual(watch["last"]["state"], "stopping")
        self.assertTrue(watch["running"], "the watchdog must not take a stopping command for idle")
        self.assertEqual(self.gate.owner, "terminal", "the command still holds the host terminal")
        typed_before = bytes(self.ui)
        refused = self.run_command("echo never-run-while-stopping", wait=1)
        self.assertEqual(refused["status"], "terminal_command_running", refused)
        self.assertEqual(refused["command_id"], command_id)
        time.sleep(0.3)
        self.assertNotIn(b"never-run-while-stopping", bytes(self.ui)[len(typed_before):],
                         "a stop or a refused command types nothing into the host terminal")
        thread.join(10)
        result = box["result"]
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), result)
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT"])


    def test_after_the_stop_the_shell_is_the_users_again_and_the_log_and_workbench_survive(self):
        # Behavior 7
        started = self.run_command("echo before-stop; sleep 605.1")
        self.assertEqual(started["status"], "running", started)
        parent = self.pane.state["parent_pid"]
        self.assertTrue(proc_exists(parent))
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(result["exit_code"], 130, result)
        self.assert_user_owns_the_shell_again()
        self.assertTrue(proc_exists(parent), "the host shell is alive")
        self.assertFalse(self.pane.exited())
        self.assertEqual(self.pane.cwd(), str(self.home))
        self.assertTrue(proc_exists(os.getpid()), "the backend process is alive")
        log = Path(started["log_path"])
        self.assertTrue(log.is_file())
        self.assertIn(b"before-stop", log.read_bytes(), "the log is preserved")
        ended = next(r for r in journal(self.root) if r["type"] == "terminal_ended")
        self.assertEqual((ended["command_id"], ended["status"], ended["exit_code"]),
                         (started["command_id"], "exited", 130))

    def test_the_journal_records_the_stop_with_the_audit_fields_and_no_environment_or_argv(self):
        # Behavior 8
        started = self.run_command("sleep 606.1")
        pid = pids_running("sleep", "606.1")[0]
        fields = proc_fields(pid)
        ticks, group, session = int(fields[19]), int(fields[2]), int(fields[3])
        supervisor = int(fields[1])
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(self.stop(str(uuid4()))["status"], "not_found")
        records = [r for r in journal(self.root) if r["type"].startswith("command_stop")]
        by_type = {}
        for record in records:
            by_type.setdefault(record["type"], []).append(record)
        command_id = started["command_id"]
        requested = by_type["command_stop_requested"][0]
        self.assertEqual({k: requested[k] for k in ("command_id", "requested_by", "kind", "owner", "task_id")},
                         {"command_id": command_id, "requested_by": "worker", "kind": "terminal",
                          "owner": "worker", "task_id": None})
        for name in ("task_status", "reason", "at"):
            self.assertIn(name, requested)
        target = by_type["command_stop_target"][0]
        self.assertEqual((target["command_id"], target["pid"], target["start_ticks"], target["process_group"],
                          target["session"], target["uid"], target["boot_id"]),
                         (command_id, pid, ticks, group, session, os.getuid(), BOOT_ID))
        self.assertEqual(target["supervisor"]["pid"], supervisor)
        self.assertIn({"pid": pid, "start_ticks": ticks}, target["tracked"])
        self.assertEqual(target["left_session"], [])
        sent = by_type["command_stop_signal"]
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]["command_id"], sent[0]["signal"], sent[0]["stage"], sent[0]["auxiliary"],
                          sent[0]["failed"]), (command_id, "SIGINT", "sigint", False, []))
        self.assertIn({"pid": pid, "start_ticks": ticks}, sent[0]["targets"])
        self.assertTrue(sent[0]["at"])
        finished = by_type["command_stop_finished"][0]
        self.assertEqual((finished["command_id"], finished["status"], finished["exit_code"], finished["signal"],
                          finished["repeat_requests"]), (command_id, "exited", 130, 2, 0))
        for name in ("reason", "residual", "elapsed_seconds"):
            self.assertIn(name, finished)
        self.assertGreaterEqual(finished["elapsed_seconds"], 0)
        refused = by_type["command_stop_refused"][0]
        self.assertEqual((refused["status"], refused["reason"], refused["requested_by"]),
                         ("not_found", "unknown_command", "worker"))
        self.assertTrue(refused["command_id"] and refused["at"])
        text = json.dumps(records)
        for secret in (ENV_VALUE, "sleep 606.1", "606.1", "cmdline"):
            self.assertNotIn(secret, text, "no environment value, argv or command string in the stop records")

    def bystander(self, seconds):
        process = subprocess.Popen(["sleep", seconds], stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.poll() is None and process.kill(), process.wait()))
        return process

    def test_an_unknown_command_id_is_not_found_and_no_process_is_signalled(self):
        # Behavior 5a
        started = self.run_command("sleep 603.1")
        pids = pids_running("sleep", "603.1")
        outsider = self.bystander("603.2")
        result = self.stop(str(uuid4()))
        self.assertEqual(result["status"], "not_found", result)
        self.assertEqual(result["reason"], "unknown_command")
        self.assertFalse(result.get("signals"), "no signal was sent")
        time.sleep(0.2)
        self.assertEqual(self.query()["status"], "running", "the running command is untouched")
        self.assertTrue(all(proc_exists(pid) for pid in pids) and pids)
        self.assertIsNone(outsider.poll(), "an unrelated process is untouched")
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")  # end the test's own command

    def test_a_finished_command_id_is_already_exited_with_its_last_state_and_nothing_is_signalled(self):
        # Behavior 5b
        outsider = self.bystander("603.3")
        done = self.run_command("echo finished; exit 3", wait=15)
        self.assertEqual((done["status"], done["exit_code"]), ("exited", 3), done)
        self.assert_user_owns_the_shell_again()
        result = self.stop(done["command_id"])
        self.assertEqual(result["status"], "already_exited", result)
        self.assertEqual(result["exit_code"], 3)
        self.assertIsNone(result["signal"])
        self.assertTrue(result["ended_at"])
        self.assertEqual(result["command_id"], done["command_id"])
        self.assertFalse(result.get("signals"), "no signal was sent")
        self.assertIsNone(outsider.poll())
        records = [r for r in journal(self.root) if r["type"].startswith("command_stop")]
        self.assertNotIn("command_stop_signal", [r["type"] for r in records])


class IneffectiveSignalsEnvironment(LinuxStopEnvironment):
    """S7: SIGTERM and SIGKILL are accepted but have no effect (as for a process in uninterruptible sleep)."""

    def pidfd_send_signal(self, fd, signum):
        if signum in (signal.SIGTERM, signal.SIGKILL):
            return
        super().pidfd_send_signal(fd, signum)


class StopStagesExhaustedTests(RealStopFixture):
    def make_stop_env(self):
        return IneffectiveSignalsEnvironment()

    def test_a_command_that_survives_every_stage_is_unknown_and_keeps_the_terminal(self):
        # CS-03: SIGINT is ignored by the command and SIGTERM/SIGKILL do nothing (injected): all stages run out
        started = self.run_command("trap '' INT; sleep 6.3")
        pids = pids_running("sleep", "6.3")
        self.assertEqual(len(pids), 1, pids)
        began = time.monotonic()
        result = self.stop(started["command_id"])
        waited = time.monotonic() - began
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(result["reason"], "still_alive_after_sigkill")
        self.assertGreaterEqual(waited, REDUCED.sigint_wait + REDUCED.sigterm_wait + REDUCED.sigkill_wait - 0.1)
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertTrue(proc_exists(pids[0]))
        self.assertEqual(self.query()["status"], "unknown", "still not ended: the surface says unknown")
        self.assertEqual(self.run_command("echo no", wait=1)["status"], "terminal_command_running")
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))
        self.assertEqual(self.terminal.current()["status"], "exited", "it ended by itself; the owner reports it")
        self.assert_user_owns_the_shell_again()


class ShiftedTicksEnvironment(LinuxStopEnvironment):
    """S7: PID reuse. Reads of a pid in ``shifted`` show other start ticks (``after_reads``: only from that read
    of the pid on, so the scan sees the recorded process and the pin does not). Every signal attempt is recorded."""

    def __init__(self, after_reads=0):
        self.shifted, self.after_reads, self.reads, self.signals = set(), after_reads, {}, []

    def read_stat(self, pid):
        stat = super().read_stat(pid)
        if stat is None or pid not in self.shifted:
            return stat
        self.reads[pid] = self.reads.get(pid, 0) + 1
        if self.reads[pid] <= self.after_reads:
            return stat
        return dataclasses.replace(stat, start_ticks=stat.start_ticks + 1)

    def pidfd_send_signal(self, fd, signum):
        self.signals.append(signum)
        super().pidfd_send_signal(fd, signum)


class StopIdentityMismatchTests(RealStopFixture):
    AFTER_READS = 0
    SECONDS = "604.1"

    def make_stop_env(self):
        return ShiftedTicksEnvironment(after_reads=self.AFTER_READS)

    def test_a_pid_whose_start_ticks_differ_from_the_recorded_run_is_stale(self):
        # Behavior 6: the process the recorded run identity names reads as another process (PID reuse)
        env = self.stop_env
        started = self.run_command(f"sleep {self.SECONDS}")
        self.assertEqual(started["status"], "running", started)
        pids = pids_running("sleep", self.SECONDS)
        self.assertEqual(len(pids), 1, pids)
        env.shifted.add(pids[0])
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "stale", result)
        self.assertEqual(result["reason"], "identity_mismatch")
        self.assertEqual(env.signals, [], "no signal was sent")
        self.assertTrue(proc_exists(pids[0]), "the target is alive")
        types = [r["type"] for r in journal(self.root) if r["type"].startswith("command_stop")]
        self.assertNotIn("command_stop_signal", types)
        self.assertEqual(types[-1], "command_stop_finished")
        os.kill(pids[0], signal.SIGKILL)  # the test's own sleep: end it so the host shell is the user's again
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 10))


class StopIdentityChangesBetweenScanAndPinTests(StopIdentityMismatchTests):
    """The same case, but the process changes only after the tracked set was read (the pin's re-read differs)."""

    AFTER_READS = 1
    SECONDS = "604.2"


class StopMainCommandIdentityTests(RealStopFixture):
    def make_stop_env(self):
        return ShiftedTicksEnvironment()

    def test_a_main_command_that_does_not_match_the_run_identity_stops_the_whole_stop(self):
        # Behavior 6c: the run identity's own command process reads as another process: its descendants are
        # not signalled either, because the identity the whole target set rests on is wrong
        started = self.run_command("sleep 604.3 & sleep 604.4 & wait")
        self.assertEqual(started["status"], "running", started)
        sleepers = pids_running("sleep", "604.3") + pids_running("sleep", "604.4")
        self.assertEqual(len(sleepers), 2, sleepers)
        self.addCleanup(lambda: [os.kill(pid, signal.SIGKILL) for pid in sleepers if proc_exists(pid)])
        main = int(Path(f"/proc/{sleepers[0]}/stat").read_text().rsplit(") ", 1)[1].split()[1])  # their parent
        self.assertTrue(proc_exists(main))
        self.addCleanup(lambda: proc_exists(main) and os.kill(main, signal.SIGKILL))
        self.stop_env.shifted.add(main)
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("stale", "identity_mismatch"), result)
        self.assertEqual(self.stop_env.signals, [], "no signal was sent to anything")
        self.assertTrue(all(proc_exists(pid) for pid in sleepers))
        for pid in sleepers + [main]:
            os.kill(pid, signal.SIGKILL)  # the test's own processes
        self.assertTrue(wait_until(lambda: self.query()["status"] == "exited", 10))


class StopAndDoneNoticeTests(RealStopFixture):
    def terminal_extras(self):
        self.delivered = []

        def notify(notice):
            self.delivered.append(dict(notice))
            return "delivered"
        return {"notify": notify, "tick_interval": 0.05}

    def setUp(self):
        super().setUp()
        self.terminal.start()

    def test_the_completion_notice_is_delivered_once_and_the_stop_call_does_not_consume_it(self):
        # Behavior 4
        started = self.run_command("sleep 601.4")
        command_id = started["command_id"]
        result = self.stop(command_id)
        self.assertEqual((result["status"], result["exit_code"], result["signal"]), ("exited", 130, 2), result)
        self.assertTrue(wait_until(lambda: any(item["type"] == "terminal_done" for item in self.delivered), 5),
                        "the owner's completion notice is still delivered after a stop call returned the result")
        time.sleep(0.4)  # a duplicate would show up within a few ticks
        done = [item for item in self.delivered if item["type"] == "terminal_done"]
        self.assertEqual(len(done), 1, done)
        self.assertEqual((done[0]["command_id"], done[0]["status"], done[0]["exit_code"], done[0]["signal"]),
                         (command_id, "exited", 130, 2))


if __name__ == "__main__":
    unittest.main()
