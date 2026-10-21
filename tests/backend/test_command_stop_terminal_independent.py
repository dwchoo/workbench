"""CS-02 independent verification: the worker stops its own terminal command (``terminal {command: null, stop: id}``).

Derived from the contract (SPEC "Worker: terminal 도구의 stop 인자", "결과 계약", "명령 상태 표면", "단계와 타이머",
"감사 기록", "신원·신호 불변식", "테스트 결정" S2/S7; BRIEF CS-AC-03/07/08/09/19/20), not from the implementation or
from the worker's own tests.

Seams: S2 (a real PTY ``ShellPane`` + ``HostShellPort`` + ``TerminalService`` + ``CommandStopService``) for process,
terminal-occupation and survival behavior; S7 (an error-injecting ``StopEnvironment``) for identity mismatches and
races; a fake-port fixture for argument validation; a fake ``StopTarget`` for the registry's refusals.

Independent oracles: exit codes 128+n of processes this file starts, ``/proc`` reads done here (pid, start ticks,
ppid, session, state), the policy constants passed in, the command ids it was given, and a sentinel process outside
every tree that must survive every probe. Production formatters are never used to build expectations.

Safety: only processes this file starts are ever signalled (by pidfd, after checking their start ticks); the host
shell pane is closed on teardown. Sleep durations are distinctive (6xx) so a stray can be recognised.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from workbench.backend.command_stop import CommandStopService, LinuxStopEnvironment, StopPolicy, StopTarget
from workbench.backend.flow_terminal import HostGate, TerminalService, validate_terminal_arguments
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.contracts.v1 import ActorRole
from workbench.terminal.shell_g2.prototype import ShellChoice

from test_flow_terminal import AUTOMATION, SECRET, FakeHostPort, ServiceFixture, call, journal, wait_until

# The test's own policy constants: a reduced StopPolicy (production defaults are 3/10/5/5/25 s).
POLICY = StopPolicy(identity_wait=2.0, sigint_wait=2.0, sigterm_wait=1.0, sigkill_wait=1.0, call_wait=8.0)
ENV_VALUE = "exported-value-for-the-independent-stop-test"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
SLOW_INT = "trap 'sleep 1.2; exit 130' INT; echo ready-for-int; while :; do sleep 0.2; done"  # answers SIGINT late


# -- /proc oracle (read here, never through the production reader) --------------------------------------------
@dataclasses.dataclass(frozen=True)
class Proc:
    pid: int
    state: str
    ppid: int
    pgrp: int
    session: int
    ticks: int


def proc(pid: int) -> Proc | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return None
    return Proc(pid, fields[0], int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19]))


def alive(pid: int, ticks: int | None = None) -> bool:
    found = proc(pid)
    return found is not None and found.state not in ("Z", "X") and (ticks is None or found.ticks == ticks)


def all_procs() -> list[Proc]:
    found = []
    for name in os.listdir("/proc"):
        if name.isdecimal() and (item := proc(int(name))) is not None:
            found.append(item)
    return found


def pids_running(*argv: str) -> list[int]:
    """Live pids whose argv equals ``argv`` (used by this file only to find the processes it started)."""
    wanted = b"\0".join(item.encode() for item in argv) + b"\0"
    found = []
    for name in os.listdir("/proc"):
        if not name.isdecimal():
            continue
        try:
            if Path(f"/proc/{name}/cmdline").read_bytes() == wanted and alive(int(name)):
                found.append(int(name))
        except OSError:
            continue
    return found


def descendants(pid: int) -> list[int]:
    stats = all_procs()
    found, pending = [], [pid]
    while pending:
        parent = pending.pop()
        for item in stats:
            if item.ppid == parent and item.pid not in found:
                found.append(item.pid)
                pending.append(item.pid)
    return found


class Owned:
    """Processes this file started or found; killed (by pidfd, only while their start ticks still match) on teardown."""

    def __init__(self) -> None:
        self.items: list[tuple[int, int]] = []

    def add(self, pid: int) -> int:
        found = proc(pid)
        if found is not None:
            self.items.append((pid, found.ticks))
        return pid

    def kill_all(self) -> None:
        for pid, ticks in self.items:
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                continue
            try:
                found = proc(pid)
                if found is not None and found.ticks == ticks:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            except OSError:
                pass
            finally:
                os.close(fd)


# -- S7: an error-injecting StopEnvironment ---------------------------------------------------------------
class InjectingEnv(LinuxStopEnvironment):
    """Records every signal attempt as (pid, signum); can shift a pid's start ticks from its n-th read on (PID reuse),
    fake the boot id, or fail every send. ``violations`` lists signals that left although the process's latest read
    (the one right before the send) showed a start-ticks mismatch."""

    def __init__(self) -> None:
        self.shift_after: dict[int, int] = {}
        self.reads: dict[int, int] = {}
        self.last_shifted: dict[int, bool] = {}
        self.fd_pid: dict[int, int] = {}
        self.attempts: list[tuple[int | None, int]] = []
        self.sent: list[tuple[int | None, int]] = []
        self.violations: list[tuple[int | None, int]] = []
        self.boot_fake: str | None = None
        self.send_error: type[OSError] | None = None
        self.lock = threading.Lock()

    def attempted_pids(self) -> set[int | None]:
        return {pid for pid, _ in self.attempts}

    def boot_id(self):
        return self.boot_fake or super().boot_id()

    def read_stat(self, pid):
        stat = super().read_stat(pid)
        if stat is None:
            return stat
        with self.lock:
            if pid not in self.shift_after:
                return stat
            count = self.reads[pid] = self.reads.get(pid, 0) + 1
            shifted = self.last_shifted[pid] = count > self.shift_after[pid]
        return dataclasses.replace(stat, start_ticks=stat.start_ticks + 1) if shifted else stat

    def pidfd_open(self, pid):
        fd = super().pidfd_open(pid)
        with self.lock:
            self.fd_pid[fd] = pid
        return fd

    def close(self, fd):
        with self.lock:
            self.fd_pid.pop(fd, None)
        super().close(fd)

    def pidfd_send_signal(self, fd, signum):
        with self.lock:
            pid = self.fd_pid.get(fd)
            self.attempts.append((pid, signum))
            if self.last_shifted.get(pid):
                self.violations.append((pid, signum))
        if self.send_error is not None:
            raise self.send_error(f"injected {self.send_error.__name__}")
        super().pidfd_send_signal(fd, signum)
        with self.lock:
            self.sent.append((pid, signum))


# -- argument validation and refusals on a fake host port (nothing real runs) ------------------------------
class FakePortRig(ServiceFixture):
    def make_terminal(self, host_shell, **extra):
        previous = getattr(self, "command_stop", None)
        if previous is not None:
            previous.close()
        self.env = InjectingEnv()
        self.command_stop = CommandStopService(journal=self.handoffs.record, policy=POLICY, env=self.env)
        return TerminalService(handoffs=self.handoffs, host_shell=host_shell, gate=self.gate,
                               log_root=self.root / "workflow" / "terminal",
                               automation=lambda: AUTOMATION, paused=lambda: self.paused,
                               activity=lambda: self.activity, sensitive_values=lambda: (SECRET,),
                               poll_interval=0.02, command_stop=self.command_stop, **extra)

    def tearDown(self):
        self.command_stop.close()
        super().tearDown()

    def stop_records(self):
        return [r for r in journal(self.root) if str(r["type"]).startswith("command_stop")]

    def assert_nothing_happened(self):
        self.assertEqual(self.port.sent, [], "nothing is typed into the host terminal")
        self.assertEqual(self.port.holds, [])
        self.assertIsNone(self.gate.owner)
        self.assertEqual(self.env.attempts, [], "no signal was attempted")
        self.assertEqual(self.stop_records(), [], "no stop was started or recorded")


class StopArgumentTests(FakePortRig):
    COMMAND_ID = str(uuid4())

    def test_stop_with_a_command_string_is_invalid_arguments_naming_fields_only_and_does_nothing(self):
        # SPEC "terminal 도구의 stop 인자": invalid_arguments, field names only, values not echoed, nothing done
        command = f"echo {SECRET}"
        result = self.terminal.handle(ActorRole.WORKER, call({"command": command, "stop": self.COMMAND_ID}))
        self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), result)
        text = json.dumps(result)
        self.assertIn("stop", text)
        self.assertIn("command", text)
        for leaked in (self.COMMAND_ID, SECRET, command):
            self.assertNotIn(leaked, text, "the values sent are never echoed")
        self.assertNotIn(self.COMMAND_ID, json.dumps(journal(self.root)), "nor journaled for an invalid call")
        self.assertNotIn(SECRET, json.dumps(journal(self.root)))
        self.assert_nothing_happened()

    def test_an_empty_blank_or_null_stop_is_unused_and_the_command_path_runs_untouched(self):
        busy = FakeHostPort(busy="user typing")
        self.port = busy
        for stop in (None, "", "   "):
            result = self.terminal.handle(ActorRole.WORKER, call({"command": "echo unused-stop", "stop": stop}))
            self.assertEqual(result["status"], "host_terminal_busy", (stop, result))  # reached the command path
            fetched = self.terminal.handle(ActorRole.WORKER, call({"command": None, "stop": stop}))
            self.assertEqual(fetched.get("reason"), "no_terminal_command", (stop, fetched))  # the plain state query
        self.assertEqual(self.stop_records(), [])
        self.assertEqual(self.env.attempts, [])

    def test_unknown_fields_next_to_stop_are_still_refused(self):
        for extra in ({"signal": "SIGKILL"}, {"pid": 1}, {"force": True}, {"timeout_seconds": 5}):
            result = self.terminal.handle(ActorRole.WORKER,
                                          call({"command": None, "stop": self.COMMAND_ID, **extra}))
            self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), result)
            name = next(iter(extra))
            self.assertTrue(any(error.startswith(name) for error in result["errors"]), result)
            self.assertNotIn(self.COMMAND_ID, json.dumps(result))
        self.assert_nothing_happened()

    def test_a_stop_that_is_not_a_command_id_is_refused_without_its_value_and_signals_nothing(self):
        canonical = self.COMMAND_ID
        non_canonical = (canonical.upper(), "{" + canonical + "}", "urn:uuid:" + canonical, canonical.replace("-", ""))
        for value in (5, True, ["x"], {"id": 1}, "not-a-uuid", "../../etc/passwd", "1" * 5000, *non_canonical):
            result = self.terminal.handle(ActorRole.WORKER, call({"command": None, "stop": value}))
            self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), (value, result))
            self.assertTrue(any(error.startswith("stop") for error in result["errors"]), result)
            self.assertNotIn("passwd", json.dumps(result))
            if isinstance(value, str):
                self.assertNotIn(value, json.dumps(result), "the value sent is not echoed")
            self.assertLess(len(json.dumps(result)), 3000, "a long value is not echoed")
        self.assert_nothing_happened()

    def test_validation_alone_keeps_the_existing_command_shapes(self):
        self.assertEqual(validate_terminal_arguments({"command": "ls"}), [])
        self.assertEqual(validate_terminal_arguments({"command": None}), [])
        self.assertTrue(validate_terminal_arguments({"command": "ls", "stop": self.COMMAND_ID}))
        self.assertTrue(validate_terminal_arguments("stop"))
        self.assertTrue(validate_terminal_arguments(None))

    def test_a_blank_command_string_next_to_a_stop_is_command_unused_and_the_stop_proceeds(self):
        # Root ruling (b): "" or whitespace command = not used; the stop goes ahead and no command is typed
        for blank in ("", "   ", "\t\n"):
            result = self.terminal.handle(ActorRole.WORKER, call({"command": blank, "stop": self.COMMAND_ID}))
            self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), (blank, result))
        self.assertEqual(self.port.sent, [])
        self.assertEqual(self.port.holds, [])
        self.assertEqual(self.env.attempts, [])

    def test_the_manager_cannot_use_the_terminal_stop_argument(self):
        fake = StopTarget(self.COMMAND_ID, "worker", "terminal", None, identity=lambda: None)
        self.command_stop.register(fake)
        result = self.terminal.handle(ActorRole.MANAGER, call({"command": None, "stop": self.COMMAND_ID}))
        self.assertEqual((result["status"], result["reason"]), ("rejected", "tool_not_allowed_for_role"), result)
        self.assertIsNone(self.command_stop.surface(self.COMMAND_ID))
        self.assertEqual(self.stop_records(), [])
        self.assertEqual(self.env.attempts, [])


class RegistryRefusalTests(FakePortRig):
    """A fake StopTarget (no PTY): the registry's no-signal outcomes and its structural exclusions."""

    def test_an_unknown_id_is_not_found_and_an_ended_id_is_already_exited_without_any_signal(self):
        ghost = str(uuid4())
        result = self.command_stop.stop("worker", ghost)
        self.assertEqual((result["status"], result["reason"], result["command_id"], result["requested_by"]),
                         ("not_found", "unknown_command", ghost, "worker"), result)
        done = str(uuid4())
        self.command_stop.register(StopTarget(done, "worker", "terminal", None, identity=lambda: None))
        self.command_stop.command_ended(done, {"status": "exited", "exit_code": 7, "signal": None})
        result = self.command_stop.stop("worker", done)
        self.assertEqual((result["status"], result["exit_code"], result["signal"]), ("already_exited", 7, None))
        self.assertTrue(result["ended_at"])
        self.assertEqual(self.env.attempts, [])
        refused = [r for r in self.stop_records() if r["type"] == "command_stop_refused"]
        self.assertEqual([(r["command_id"], r["status"]) for r in refused],
                         [(ghost, "not_found"), (done, "already_exited")])
        self.assertTrue(all(r.get("requested_by") == "worker" and r.get("reason") and r.get("at") for r in refused))

    def test_an_identity_that_never_completes_is_unknown_identity_unavailable_with_no_signal(self):
        cid = str(uuid4())
        incomplete = {"request_id": cid, "phase": "running", "child": {"pid": 1, "start_ticks": 1}}
        for identity in (None, incomplete, {**incomplete, "request_id": str(uuid4())}):
            self.command_stop.register(StopTarget(cid, "worker", "terminal", None, identity=lambda i=identity: i))
            began = time.monotonic()
            result = self.command_stop.stop("worker", cid)
            self.assertEqual((result["status"], result["reason"]), ("unknown", "identity_unavailable"), result)
            self.assertGreaterEqual(time.monotonic() - began, POLICY.identity_wait - 0.2)
            self.assertEqual(self.env.attempts, [], "an unclear target is never signalled")
            self.command_stop.command_ended(cid, {"status": "exited", "exit_code": 0, "signal": None})
            cid = str(uuid4())

    def test_the_command_ending_while_its_identity_is_awaited_is_already_exited_without_a_signal(self):
        cid = str(uuid4())
        self.command_stop.register(StopTarget(cid, "worker", "terminal", None, identity=lambda: None))
        threading.Timer(0.2, self.command_stop.command_ended,
                        (cid, {"status": "exited", "exit_code": 0, "signal": None})).start()
        began = time.monotonic()
        result = self.command_stop.stop("worker", cid)
        self.assertEqual(result["status"], "already_exited", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertLess(time.monotonic() - began, POLICY.identity_wait, "ends early, not at the identity deadline")
        self.assertEqual(self.env.attempts, [])

    def test_the_supervisor_the_host_shell_and_forbidden_pids_are_never_signalled(self):
        # A fake run identity over a sentinel tree: S1 = host shell stand-in (session leader), S2 = supervisor
        # stand-in, two sleepers under S2. The scan must list the sleepers only, minus the forbidden one.
        tree = subprocess.Popen(["bash", "-c", "bash -c 'sleep 617.1 & sleep 617.2 & wait' & wait"],
                                stdin=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: (tree.poll() is None and tree.kill(), tree.wait()))
        self.assertTrue(wait_until(lambda: len(pids_running("sleep", "617.1")) == 1
                                   and len(pids_running("sleep", "617.2")) == 1, 5))
        first, second = pids_running("sleep", "617.1")[0], pids_running("sleep", "617.2")[0]
        owned = Owned()
        for pid in (first, second):
            owned.add(pid)
        self.addCleanup(owned.kill_all)
        s2 = proc(first).ppid
        s1 = proc(s2).ppid
        self.assertEqual(s1, tree.pid)
        cid = str(uuid4())
        identity = {"request_id": cid, "phase": "running", "child": {"pid": s2, "start_ticks": proc(s2).ticks},
                    "parent": {"pid": s1, "start_ticks": proc(s1).ticks},
                    "supervisor": {"pid": s2, "start_ticks": proc(s2).ticks},
                    "child_group": proc(s2).pgrp, "session": proc(s1).session, "uid": os.getuid(),
                    "boot_id": BOOT_ID}
        self.command_stop.register(StopTarget(cid, "worker", "terminal", None, identity=lambda: identity,
                                              forbidden=lambda: frozenset({first})))
        result = self.command_stop.stop("worker", cid)
        self.assertEqual(self.env.attempted_pids(), {second}, "only the one allowed member is signalled")
        self.assertNotIn(os.getpid(), self.env.attempted_pids())
        self.assertTrue(alive(s1) and alive(s2) and alive(first))
        self.assertNotEqual(result["status"], "exited", "the owner never reported an end")
        self.command_stop.command_ended(cid, {"status": "exited", "exit_code": 0, "signal": None})


# -- S2: a real PTY host shell ---------------------------------------------------------------------------
class RealRig(ServiceFixture):
    CHOICE = ShellChoice("bash", "/usr/bin/bash")
    POLICY = POLICY

    def make_env(self) -> InjectingEnv:
        return InjectingEnv()

    def terminal_extras(self) -> dict:
        return {}

    def make_terminal(self, host_shell, **extra):
        previous = getattr(self, "command_stop", None)
        if previous is not None:
            previous.close()
        self.command_stop = CommandStopService(journal=self.handoffs.record, policy=self.POLICY, env=self.env)
        return TerminalService(handoffs=self.handoffs, host_shell=host_shell, gate=self.gate,
                               log_root=self.root / "workflow" / "terminal",
                               automation=lambda: AUTOMATION, paused=lambda: self.paused,
                               activity=lambda: self.activity, sensitive_values=lambda: (SECRET,),
                               poll_interval=0.02, command_stop=self.command_stop, **extra)

    def setUp(self):
        self.env = self.make_env()
        self.owned = Owned()
        self.bystanders: list[subprocess.Popen] = []
        self.pane = None
        self.loop = None
        self.halt = threading.Event()
        self.ui = bytearray()
        super().setUp()
        try:
            self.home = self.root / "home"
            self.home.mkdir()
            self.pane = ShellPane(self.CHOICE, {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8",
                                                "WB_STOP_FIXTURE": ENV_VALUE})
            self.loop = threading.Thread(target=self._backend_loop, daemon=True)
            self.loop.start()
            self.terminal.close()
            self.terminal = self.make_terminal(lambda: HostShellPort(self.pane, lambda: self.pane),
                                               **self.terminal_extras())
            self.assertTrue(wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))
            self.user_types(f"cd {self.home}\r".encode())
            self.assertTrue(wait_until(lambda: self.pane.cwd() == str(self.home), 5))
            self.assertTrue(wait_until(self.idle, 5))
        except BaseException:
            self.tearDown()
            raise

    def _backend_loop(self):
        while not self.halt.is_set():
            for chunk in self.pane.pump():
                self.ui.extend(chunk.data)
            time.sleep(0.01)

    def tearDown(self):
        self.owned.kill_all()  # the test's own strays: a command that outlived its test can end
        for process in self.bystanders:
            if process.poll() is None:
                process.kill()
            process.wait()
        self.terminal.close()
        self.command_stop.close()
        self.halt.set()
        if self.loop is not None:
            self.loop.join(5)
        if self.pane is not None:
            self.pane.close()
        super().tearDown()

    # -- helpers
    def user_types(self, data):
        self.assertIsNone(self.pane.admit(data))

    def idle(self):
        port = HostShellPort(self.pane, lambda: self.pane)
        try:
            return port.busy() is None
        finally:
            port.detach()

    def run_command(self, command, wait=0.6):
        self.terminal._wait_seconds = wait
        return self.terminal.handle(ActorRole.WORKER, call({"command": command}))

    def query(self):
        return self.terminal.handle(ActorRole.WORKER, call({"command": None}))

    def stop(self, command_id):
        return self.terminal.handle(ActorRole.WORKER, call({"command": None, "stop": command_id}))

    def stop_in_background(self, command_id):
        box = {}

        def work():
            box["began"] = time.monotonic()
            box["result"] = self.stop(command_id)
            box["ended"] = time.monotonic()
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 15)
        return thread, box

    def bystander(self, seconds):
        process = subprocess.Popen(["sleep", seconds], stdin=subprocess.DEVNULL)
        self.bystanders.append(process)
        self.assertTrue(alive(process.pid))
        return process

    def find(self, *argv, count=1, timeout=5):
        self.assertTrue(wait_until(lambda: len(pids_running(*argv)) >= count, timeout), (argv, pids_running(*argv)))
        pids = pids_running(*argv)
        for pid in pids:
            self.owned.add(pid)
        return pids

    def stop_records(self):
        return [r for r in journal(self.root) if str(r["type"]).startswith("command_stop")]

    def records_of(self, kind):
        return [r for r in self.stop_records() if r["type"] == kind]

    def assert_user_owns_the_shell_again(self):
        self.assertTrue(wait_until(lambda: self.pane.state["input_owner"] == "user"
                                   and self.pane.state["parent_mode"] == "manual_prompt", 10), self.pane.state)
        self.assertIsNone(self.pane.automation_hold)
        self.assertIsNone(self.gate.owner)
        marker = f"user-typed-{uuid4().hex[:6]}"
        self.user_types(f"echo {marker}\r".encode())
        self.assertTrue(wait_until(lambda: bytes(self.ui).count(marker.encode()) >= 2, 5), "the user regains input")

    def kill_owned_and_wait_for_the_owner(self, *pids):
        for pid in pids:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)  # only pids this test started and found
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10), self.terminal.current())


class StopRunningCommandTests(RealRig):
    def test_stop_ends_a_sleep_with_sigint_and_reports_what_the_test_can_verify(self):
        # CS-AC-03
        started = self.run_command("sleep 621.1")
        self.assertEqual(started["status"], "running", started)
        self.assertIn("stop", started["detail"], "the running result tells the worker how to stop")
        self.assertIn("End your turn", started["detail"], "the existing guidance stays")
        command_id = started["command_id"]
        (pid,) = self.find("sleep", "621.1")
        ticks = proc(pid).ticks
        result = self.stop(command_id)
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"], result["signal_name"]), (128 + 2, 2, "SIGINT"))
        self.assertEqual((result["command_id"], result["requested_by"]), (command_id, "worker"))
        self.assertEqual([item["signal"] for item in result["signals"]], ["SIGINT"])
        self.assertIn({"pid": pid, "start_ticks": ticks}, result["signals"][0]["targets"])
        self.assertTrue(result["signals"][0]["at"])
        self.assertEqual(result["residual"]["alive"], [])
        self.assertTrue(result["residual"]["unchecked"], "the unchecked scope is always stated")
        self.assertFalse({"clean", "ok", "clear", "all_clear"} & set(result["residual"]), "no 'clean' claim exists")
        self.assertFalse(alive(pid), "the command's process is gone")
        self.assertEqual(self.env.attempts, [(pid, signal.SIGINT)], "exactly one SIGINT, to the verified process")
        self.assertEqual(self.env.violations, [])
        self.assertEqual(self.terminal.current()["status"], "exited")

    def test_a_restarted_worker_with_another_session_can_stop_the_command(self):
        # SPEC: the owner is the role "worker"; the OMP session is not part of the ownership
        started = self.run_command("sleep 621.4")
        (pid,) = self.find("sleep", "621.4")
        other_session = str(uuid4())
        result = self.terminal.handle(ActorRole.WORKER, call({"command": None, "stop": started["command_id"]},
                                                             role_session=other_session))
        self.assertEqual((result["status"], result["exit_code"], result["requested_by"]), ("exited", 130, "worker"),
                         result)
        self.assertFalse(alive(pid))

    def test_a_non_canonical_id_of_the_running_command_is_refused_and_a_blank_command_still_stops(self):
        # Root rulings (a) and (b) on a live command
        started = self.run_command("sleep 621.5")
        (pid,) = self.find("sleep", "621.5")
        command_id = started["command_id"]
        for variant in (command_id.upper(), "{" + command_id + "}", command_id.replace("-", "")):
            refused = self.stop(variant)
            self.assertEqual((refused["status"], refused["reason"]), ("rejected", "invalid_arguments"), refused)
            self.assertNotIn(variant, json.dumps(refused))
        self.assertEqual(self.env.attempts, [], "a refused spelling sends no signal")
        self.assertTrue(alive(pid))
        self.assertEqual(self.query()["status"], "running")
        result = self.terminal.handle(ActorRole.WORKER, call({"command": "  ", "stop": command_id}))
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), result)
        self.assertFalse(alive(pid))

    def test_the_call_waits_for_the_owner_observed_end_not_for_acceptance(self):
        # CS-AC-03: "요청 수락만으로 성공을 보고하지 않으며"
        started = self.run_command(SLOW_INT)
        self.assertEqual(started["status"], "running", started)
        thread, box = self.stop_in_background(started["command_id"])
        time.sleep(0.6)
        self.assertNotIn("result", box, "the call is still waiting while the command handles SIGINT slowly")
        self.assertTrue(self.terminal.current()["running"])
        self.assertEqual(self.gate.owner, "terminal", "the command still holds the host terminal")
        thread.join(10)
        result = box["result"]
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), result)
        self.assertGreaterEqual(box["ended"] - box["began"], 1.1, "returned only after the 1.2 s trap finished")
        # at return time the owner had already finished the command and released the terminal
        self.assertFalse(self.terminal.current()["running"])
        self.assertEqual(self.terminal.current()["status"], "exited")
        self.assertIsNone(self.gate.owner)

    def test_while_the_call_waits_other_paths_read_stopping_and_the_terminal_stays_occupied(self):
        # CS-AC-03 (stopping) + CS-AC-09 (occupied terminal: new command refused, nothing typed)
        started = self.run_command(SLOW_INT)
        command_id = started["command_id"]
        thread, box = self.stop_in_background(command_id)
        self.assertTrue(wait_until(lambda: self.query()["status"] == "stopping", 5), "a query sees stopping")
        with mock.patch.object(HostShellPort, "send_user", autospec=True) as send_user, \
                mock.patch.object(HostShellPort, "submit", autospec=True) as submit, \
                mock.patch.object(HostShellPort, "claim_manager", autospec=True) as claim, \
                mock.patch.object(HostShellPort, "hold", autospec=True) as hold:
            seen = self.query()
            self.assertEqual((seen["status"], seen["command_id"]), ("stopping", command_id), seen)
            self.assertEqual(seen["stop"]["requested_by"], "worker")
            self.assertEqual(self.terminal.current()["state"], "stopping")
            watch = self.terminal.watch_state()
            self.assertEqual(watch["last"]["state"], "stopping")
            self.assertTrue(watch["running"], "the watchdog must not read a stopping command as idle")
            before = len(self.ui)
            refused = self.run_command("echo never-run-while-stopping", wait=1)
            self.assertEqual(refused["status"], "terminal_command_running", refused)
            self.assertEqual(refused["command_id"], command_id)
            time.sleep(0.2)
            for spied in (send_user, submit, claim, hold):
                spied.assert_not_called()
            self.assertNotIn(b"never-run-while-stopping", bytes(self.ui)[before:],
                             "neither the stop nor the refused command typed into the host terminal")
        self.assertEqual(self.gate.owner, "terminal")
        thread.join(10)
        self.assertEqual(box["result"]["status"], "exited", box["result"])

    def test_the_stop_call_is_not_a_waiter_and_the_completion_notice_is_delivered_once(self):
        # CS-AC-03: terminal_done is delivered separately, once; the stop call does not consume it
        delivered = []
        self.terminal.close()
        self.terminal = self.make_terminal(lambda: HostShellPort(self.pane, lambda: self.pane),
                                           notify=lambda notice: delivered.append(dict(notice)) or "delivered",
                                           tick_interval=0.05)
        self.terminal.start()
        started = self.run_command("sleep 621.2")
        command_id = started["command_id"]
        result = self.stop(command_id)
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), result)
        self.assertTrue(wait_until(lambda: any(n["type"] == "terminal_done" for n in delivered), 5), delivered)
        again = self.stop(command_id)  # a repeated stop after the end must not recreate or consume a notice
        self.assertEqual(again["status"], "already_exited", again)
        time.sleep(0.6)
        done = [n for n in delivered if n["type"] == "terminal_done"]
        self.assertEqual(len(done), 1, done)
        self.assertEqual((done[0]["command_id"], done[0]["status"], done[0]["exit_code"]),
                         (command_id, "exited", 130))

    def test_pause_and_a_task_command_list_do_not_block_a_stop_but_still_block_new_commands(self):
        # SPEC: no terminal_command_running / host_terminal_busy / paused / held / not_in_task_commands checks
        started = self.run_command("sleep 621.3")
        self.paused = True
        self.terminal._task_commands = lambda: ["echo blocked"]  # the new command is allowed by the Task list
        refused = self.run_command("echo blocked", wait=1)
        self.assertEqual(refused["status"], "terminal_command_running", refused)
        self.terminal._task_commands = lambda: ["a list that does not contain the stop"]
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), result)

    def test_call_wait_expiry_returns_stopping_and_the_episode_continues_to_the_final_state(self):
        # SPEC: when CALL_WAIT passes the call returns status stopping with the progress; the episode goes on
        shortened = dataclasses.replace(POLICY, call_wait=0.4)
        self.command_stop.policy = shortened
        started = self.run_command(SLOW_INT)
        began = time.monotonic()
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "stopping", result)
        self.assertLess(time.monotonic() - began, 1.1, "returned at the call-wait limit, not at the end")
        self.assertEqual(result["command_id"], started["command_id"])
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))
        self.assertTrue(wait_until(lambda: self.records_of("command_stop_finished"), 5))
        self.assertEqual(self.records_of("command_stop_finished")[0]["status"], "exited")
        self.assertEqual(self.terminal.current()["status"], "exited")

    def test_a_command_that_ignores_sigint_is_unknown_and_nothing_stronger_is_sent(self):
        # CS-02 stops at the SIGINT stage: unknown, still occupied; later stages belong to CS-03
        started = self.run_command("trap '' INT; sleep 3.6")
        (pid,) = self.find("sleep", "3.6")
        began = time.monotonic()
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "unknown", result)
        self.assertGreaterEqual(time.monotonic() - began, POLICY.sigint_wait - 0.1)
        self.assertEqual({signum for _, signum in self.env.attempts}, {signal.SIGINT}, "no SIGTERM/SIGKILL yet")
        self.assertTrue(alive(pid))
        self.assertEqual(self.query()["status"], "unknown", "not ended, not stopping any more: unknown")
        self.assertEqual(self.run_command("echo no", wait=1)["status"], "terminal_command_running",
                         "an unknown command is not reported as safe to replace")
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))
        self.assertEqual(self.terminal.current()["status"], "exited", "the owner reports its own end")
        self.assert_user_owns_the_shell_again()


class NoSignalTests(RealRig):
    def test_unknown_and_ended_ids_signal_nothing_and_leave_a_running_command_and_a_sentinel_alone(self):
        # CS-AC-07
        sentinel = self.bystander("623.9")
        done = self.run_command("echo finished; exit 7", wait=15)
        self.assertEqual((done["status"], done["exit_code"]), ("exited", 7), done)
        self.assert_user_owns_the_shell_again()
        running = self.run_command("sleep 623.1")
        (pid,) = self.find("sleep", "623.1")
        ghost = str(uuid4())
        for attempt in (self.stop(ghost), self.stop(done["command_id"])):
            self.assertIn(attempt["status"], ("not_found", "already_exited"), attempt)
            self.assertFalse(attempt.get("signals"), "no signal was sent")
        upper = self.stop(ghost.upper())  # Root ruling (a): only the lowercase canonical uuid is accepted
        self.assertEqual((upper["status"], upper["reason"]), ("rejected", "invalid_arguments"), upper)
        self.assertNotIn(ghost.upper(), json.dumps(upper), "the value sent is not echoed")
        self.assertFalse(upper.get("signals"), "no signal was sent")
        ended = self.stop(done["command_id"])
        self.assertEqual((ended["status"], ended["exit_code"], ended["signal"]), ("already_exited", 7, None))
        self.assertEqual(ended["command_id"], done["command_id"])
        self.assertTrue(ended["ended_at"])
        self.assertEqual(self.stop(ghost)["reason"], "unknown_command")
        self.assertEqual(self.env.attempts, [], "not one signal attempt for any of these ids")
        time.sleep(0.2)
        self.assertEqual(self.query()["status"], "running", "the running command is untouched")
        self.assertTrue(alive(pid) and alive(sentinel.pid))
        self.assertEqual(self.stop(running["command_id"])["status"], "exited")
        self.assertTrue(alive(sentinel.pid), "a process outside every tree survives the real stop as well")

    def test_a_command_that_ended_by_a_signal_is_already_exited_with_that_last_state(self):
        done = self.run_command("kill -USR1 $$", wait=15)
        self.assertEqual(done["status"], "exited", done)
        expected_signal, expected_code = done["signal"], done["exit_code"]
        self.assert_user_owns_the_shell_again()
        result = self.stop(done["command_id"])
        self.assertEqual((result["status"], result["exit_code"], result["signal"]),
                         ("already_exited", expected_code, expected_signal), result)
        self.assertEqual(self.env.attempts, [])

    def test_stopping_an_ended_id_never_touches_the_command_running_now(self):
        first = self.run_command("echo one; exit 2", wait=15)
        self.assert_user_owns_the_shell_again()
        second = self.run_command("sleep 623.2")
        (pid,) = self.find("sleep", "623.2")
        result = self.stop(first["command_id"])
        self.assertEqual((result["status"], result["exit_code"]), ("already_exited", 2), result)
        self.assertEqual(self.env.attempts, [])
        self.assertTrue(alive(pid))
        self.assertEqual(self.query()["command_id"], second["command_id"])
        self.assertEqual(self.stop(second["command_id"])["status"], "exited")


class StopIdentityTests(RealRig):
    """CS-AC-08 on S7: whatever the injection, no signal leaves for a process whose latest verification mismatched."""

    def test_a_main_command_whose_start_ticks_differ_stops_the_whole_stop_members_included(self):
        started = self.run_command("sleep 624.1 & sleep 624.2 & wait")
        sleepers = self.find("sleep", "624.1") + self.find("sleep", "624.2")
        (main,) = {proc(pid).ppid for pid in sleepers}
        self.owned.add(main)
        self.env.shift_after[main] = 0
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("stale", "identity_mismatch"), result)
        self.assertEqual(self.env.attempts, [], "no signal went to the main command, nor to any member")
        self.assertTrue(all(alive(pid) for pid in sleepers + [main]))
        self.assertFalse(result.get("signals"))
        self.kill_owned_and_wait_for_the_owner(*sleepers, main)

    def test_a_pid_that_changes_between_any_two_reads_never_receives_a_signal(self):
        # every point of the scan -> open -> re-read -> verify -> re-read -> send sequence: a change seen at the
        # last read means no signal for that process; if nothing was sent the answer is stale
        saw_stale = saw_sent = False
        for after in range(0, 7):
            with self.subTest(shift_after_reads=after):
                self.env.attempts.clear()
                self.env.sent.clear()
                self.env.violations.clear()
                self.env.reads.clear()
                self.env.last_shifted.clear()
                self.env.shift_after.clear()
                seconds = f"624.{3 + after}"
                started = self.run_command(f"sleep {seconds}")
                (pid,) = self.find("sleep", seconds)
                self.env.shift_after[pid] = after
                result = self.stop(started["command_id"])
                self.assertEqual(self.env.violations, [], "a signal left after a mismatching read")
                if self.env.sent:
                    saw_sent = True
                    self.assertEqual(self.env.attempts, [(pid, signal.SIGINT)])
                    self.assertIn(result["status"], ("exited", "unknown"), result)
                    self.assertTrue(result["signals"])
                else:
                    saw_stale = True
                    self.assertEqual((result["status"], result["reason"]), ("stale", "identity_mismatch"), result)
                    self.assertFalse(result.get("signals"))
                    self.assertTrue(alive(pid), "the target was not signalled")
                self.kill_owned_and_wait_for_the_owner(pid)
                self.assertTrue(wait_until(self.idle, 10))
        self.assertTrue(saw_stale and saw_sent, "the sweep covered both a refusal and a delivery")

    def test_a_member_that_changes_is_skipped_and_recorded_while_the_verified_ones_are_signalled(self):
        # SPEC invariant 2: the target that changed is skipped and recorded; only all-failed is stale
        started = self.run_command("sleep 624.20; sleep 624.21")
        (member,) = self.find("sleep", "624.20")
        main = proc(member).ppid
        self.owned.add(main)
        self.env.shift_after[member] = 1  # the scan sees it right, the pin does not
        result = self.stop(started["command_id"])
        self.assertNotIn(member, self.env.attempted_pids(), "the changed member is never signalled")
        self.assertIn(main, self.env.attempted_pids(), "the verified main command is")
        self.assertEqual(self.env.violations, [])
        self.assertNotEqual(result["status"], "stale", "some target verified, so it is not stale")
        sent = self.records_of("command_stop_signal")[0]
        self.assertIn(member, [item["pid"] for item in sent["failed"]], sent)
        self.assertNotIn(member, [item["pid"] for item in sent["targets"]])
        if alive(member):  # the orphaned member lives: the stop must not call that a clean exit
            self.assertNotEqual(result["status"], "exited", result)
        self.kill_owned_and_wait_for_the_owner(member, main)

    def test_a_different_boot_id_is_stale_and_signals_nothing(self):
        started = self.run_command("sleep 624.30")
        (pid,) = self.find("sleep", "624.30")
        self.env.boot_fake = "00000000-0000-0000-0000-000000000000"
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "stale", result)
        self.assertEqual(self.env.attempts, [])
        self.assertTrue(alive(pid))
        self.kill_owned_and_wait_for_the_owner(pid)

    def test_permission_denied_on_every_member_is_unknown_at_once_and_the_command_is_untouched(self):
        # SPEC invariant 7: EPERM on all tracked -> unknown(permission_denied) immediately
        started = self.run_command("sleep 624.40")
        (pid,) = self.find("sleep", "624.40")
        self.env.send_error = PermissionError
        began = time.monotonic()
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("unknown", "permission_denied"), result)
        self.assertLess(time.monotonic() - began, POLICY.sigint_wait, "no stage time is spent waiting")
        self.assertEqual(self.env.sent, [])
        self.assertTrue(alive(pid))
        self.assertEqual(self.query()["status"], "unknown")
        self.env.send_error = None
        self.kill_owned_and_wait_for_the_owner(pid)


class StopProcessTreeTests(RealRig):
    def test_a_foreground_pipeline_gets_sigint_in_every_member_and_ends_exited(self):
        sentinel = self.bystander("625.9")
        started = self.run_command("sleep 625.1 | sleep 625.2")
        pids = self.find("sleep", "625.1") + self.find("sleep", "625.2")
        (main,) = {proc(pid).ppid for pid in pids}
        self.owned.add(main)
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), result)
        sent = {item["pid"] for item in result["signals"][0]["targets"]}
        self.assertTrue({main, *pids} <= sent, (sent, main, pids))
        self.assertFalse(any(alive(pid) for pid in pids + [main]))
        self.assertEqual(result["residual"]["alive"], [])
        self.assertTrue(alive(sentinel.pid))
        self.assertEqual(self.env.violations, [])

    def test_a_script_does_not_start_its_next_step_after_the_stop(self):
        started = self.run_command("sleep 625.3; sleep 625.4; echo reached-next-step")
        self.find("sleep", "625.3")
        result = self.stop(started["command_id"])
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), result)
        time.sleep(0.4)
        self.assertEqual(pids_running("sleep", "625.4"), [], "the second step never started")
        self.assertNotIn(b"reached-next-step", Path(started["log_path"]).read_bytes().replace(
            b"echo reached-next-step", b""), "the third step never ran")

    def test_background_jobs_that_outlive_the_signal_make_the_answer_unknown_not_exited(self):
        # SPEC "추적 범위": exited only when main and every tracked member are gone and the owner saw the end
        started = self.run_command("sleep 625.5 & sleep 625.6 & wait")
        sleepers = self.find("sleep", "625.5") + self.find("sleep", "625.6")
        (main,) = {proc(pid).ppid for pid in sleepers}
        self.owned.add(main)
        sentinel = self.bystander("625.9")
        result = self.stop(started["command_id"])
        still = sorted(pid for pid in sleepers if alive(pid))
        self.assertTrue(alive(sentinel.pid))
        sent = {item["pid"] for item in result["signals"][0]["targets"]}
        self.assertTrue({main, *sleepers} <= sent, "SIGINT went to the main command and both background jobs")
        if still:
            self.assertNotEqual(result["status"], "exited", result)
            reported = sorted(item["pid"] for item in result["residual"]["alive"])
            self.assertEqual(reported, still, "the residual lists exactly the survivors /proc shows")
            self.assertEqual(self.query()["status"], "unknown")
            self.assertEqual(self.run_command("echo again", wait=1)["status"], "terminal_command_running")
            self.kill_owned_and_wait_for_the_owner(*still)
            self.assertEqual(self.terminal.current()["status"], "exited")
        else:
            self.assertEqual(result["status"], "exited", result)
            self.assertEqual(result["residual"]["alive"], [])
        self.assert_user_owns_the_shell_again()

    def test_a_process_that_left_the_session_is_reported_and_never_signalled(self):
        # CS-AC-05 reporting (stage logic is CS-03); CS-02 must already refuse to signal it
        started = self.run_command("setsid sleep 625.7 & wait")
        (escaped,) = self.find("sleep", "625.7")
        found = proc(escaped)
        self.assertNotEqual(found.session, self.pane.state["parent_pid"], "it really left the host shell session")
        result = self.stop(started["command_id"])
        self.assertNotIn(escaped, self.env.attempted_pids(), "an escaped process gets no signal")
        self.assertTrue(alive(escaped))
        self.assertNotEqual(result["status"], "exited", "it lives, so the command did not end")
        self.assertIn({"pid": escaped, "start_ticks": found.ticks, "session": found.session},
                      result["residual"]["left_session"], result["residual"])
        self.assertNotIn(escaped, [m["pid"] for m in result["residual"]["alive"]])
        target = self.records_of("command_stop_target")[0]
        self.assertIn(escaped, [item["pid"] for item in target["left_session"]])
        self.assertNotIn(escaped, [item["pid"] for item in target["tracked"]])
        self.kill_owned_and_wait_for_the_owner(escaped)
        self.assert_user_owns_the_shell_again()

    def test_a_child_forked_by_a_non_main_thread_is_found_and_signalled(self):
        script = self.root / "thread_child.py"
        script.write_text("import subprocess, threading\n"
                          "t = threading.Thread(target=lambda: subprocess.run(['sleep', '625.8']))\n"
                          "t.start(); t.join()\n")
        started = self.run_command(f"{sys.executable} {script}", wait=1.5)
        self.assertEqual(started["status"], "running", started)
        (child,) = self.find("sleep", "625.8")
        result = self.stop(started["command_id"])
        sent = {item["pid"] for rec in result.get("signals", []) for item in rec["targets"]}
        self.assertIn(child, sent, "the thread-forked child is a tracked member")
        self.assertFalse(alive(child))
        self.assertEqual(result["status"], "exited", result)


class SurvivalTests(RealRig):
    def test_host_shell_backend_and_log_survive_and_the_shell_returns_to_the_user(self):
        # CS-AC-19
        started = self.run_command("echo before-stop; sleep 626.1 & wait")
        self.assertEqual(started["status"], "running", started)
        parent = self.pane.state["parent_pid"]
        parent_ticks = proc(parent).ticks
        sleepers = self.find("sleep", "626.1")
        result = self.stop(started["command_id"])
        self.kill_owned_and_wait_for_the_owner(*sleepers) if result["status"] != "exited" else None
        self.assert_user_owns_the_shell_again()
        self.assertTrue(alive(parent, parent_ticks), "the host shell is the same live process")
        self.assertFalse(self.pane.exited())
        self.assertEqual(self.pane.cwd(), str(self.home))
        self.assertTrue(alive(os.getpid()), "the backend is alive")
        log = Path(started["log_path"])
        self.assertTrue(log.is_file())
        self.assertIn(b"before-stop", log.read_bytes(), "the output log is preserved")
        self.assertTrue(any(r["type"] == "terminal_ended" and r["command_id"] == started["command_id"]
                            for r in journal(self.root)))

    def test_the_host_terminal_runs_a_new_command_after_a_stop_and_that_one_can_be_stopped_too(self):
        first = self.run_command("sleep 626.2")
        self.assertEqual(self.stop(first["command_id"])["status"], "exited")
        self.assert_user_owns_the_shell_again()
        second = self.run_command("echo second-command; sleep 626.3")
        self.assertEqual(second["status"], "running", second)
        self.assertNotEqual(second["command_id"], first["command_id"])
        result = self.stop(second["command_id"])
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), result)
        self.assertEqual(self.stop(first["command_id"])["status"], "already_exited")
        self.assert_user_owns_the_shell_again()
        self.assertEqual(self.env.violations, [])


class RaceTests(RealRig):
    def test_a_stop_right_after_the_start_waits_for_the_identity_and_still_stops_the_command(self):
        # concurrency: stop while the command is just starting
        for index in range(4):
            seconds = f"627.{index}"
            started = self.run_command(f"sleep {seconds}", wait=0.0)
            self.assertEqual(started["status"], "running", started)
            result = self.stop(started["command_id"])
            self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), (index, result))
            self.assertEqual(pids_running("sleep", seconds), [])
            self.assertTrue(wait_until(self.idle, 10))
        self.assertEqual(self.env.violations, [])

    def test_a_stop_racing_the_natural_end_is_never_stale_or_inconsistent(self):
        # concurrency: stop while the command is just ending
        outcomes = set()
        for index, delay in enumerate((0.0, 0.08, 0.16, 0.24, 0.32, 0.4)):
            started = self.run_command("sleep 0.35", wait=0.0)
            self.assertEqual(started["status"], "running", started)
            time.sleep(delay)
            result = self.stop(started["command_id"])
            outcomes.add(result["status"])
            with self.subTest(delay=delay, result=result["status"]):
                self.assertIn(result["status"], ("exited", "already_exited"), result)
                if result["status"] == "already_exited":
                    self.assertFalse(result.get("signals"))
                    self.assertEqual(result["exit_code"], 0)
                else:
                    self.assertIn(result["exit_code"], (0, 130), "natural end or the SIGINT death, nothing else")
                self.assertTrue(wait_until(self.idle, 10))
                self.assertFalse(self.terminal.current()["running"])
            self.assertEqual(self.env.violations, [])
        self.assertIn("exited", outcomes | {"exited"})

    def test_two_simultaneous_stops_send_one_sigint_and_both_get_the_final_result(self):
        # idempotency belongs to CS-04; here only the safety: no second signal round
        started = self.run_command(SLOW_INT)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.stop(started["command_id"])), daemon=True)
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(12)
        self.assertEqual(len(results), 2, results)
        self.assertEqual([r["status"] for r in results], ["exited", "exited"], results)
        sigints = [pid for pid, signum in self.env.sent if signum == signal.SIGINT]
        self.assertEqual(len(sigints), len(set(sigints)), "no process was signalled twice")
        self.assertEqual(len(self.records_of("command_stop_signal")), 1)

    def test_a_stop_on_a_command_whose_host_shell_died_signals_nothing_it_cannot_verify(self):
        started = self.run_command("sleep 627.8")
        (pid,) = self.find("sleep", "627.8")
        os.kill(pid, signal.SIGKILL)  # the command dies by itself, then the stop arrives
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "already_exited", result)
        self.assertEqual(self.env.attempts, [])


class JournalTests(RealRig):
    def setUp(self):
        super().setUp()

    def terminal_extras(self):
        return {"active_task": lambda: SimpleNamespace(task_id="T-independent-42")}

    def test_the_stop_leaves_records_with_the_spec_fields_and_no_environment_or_argv(self):
        # CS-AC-20 (part)
        started = self.run_command("sleep 628.1 | sleep 628.2")
        pids = self.find("sleep", "628.1") + self.find("sleep", "628.2")
        (main,) = {proc(pid).ppid for pid in pids}
        main_proc, host = proc(main), self.pane.state["parent_pid"]
        supervisor = proc(main_proc.ppid)
        facts = {pid: proc(pid) for pid in pids + [main]}
        command_id = started["command_id"]
        result = self.stop(command_id)
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual(self.stop(str(uuid4()))["status"], "not_found")
        by_type: dict[str, list[dict]] = {}
        for record in self.stop_records():
            by_type.setdefault(record["type"], []).append(record)
        order = [r["type"] for r in self.stop_records()]
        self.assertEqual(order[:4], ["command_stop_requested", "command_stop_target", "command_stop_signal",
                                     "command_stop_finished"], order)
        requested = by_type["command_stop_requested"][0]
        self.assertEqual({k: requested[k] for k in ("command_id", "requested_by", "kind", "owner", "task_id")},
                         {"command_id": command_id, "requested_by": "worker", "kind": "terminal",
                          "owner": "worker", "task_id": "T-independent-42"})
        for name in ("task_status", "reason", "at"):
            self.assertIn(name, requested)
        target = by_type["command_stop_target"][0]
        self.assertEqual((target["command_id"], target["pid"], target["start_ticks"], target["process_group"],
                          target["session"], target["uid"], target["boot_id"]),
                         (command_id, main, main_proc.ticks, main_proc.pgrp, host, os.getuid(), BOOT_ID))
        self.assertEqual(target["supervisor"], {"pid": supervisor.pid, "start_ticks": supervisor.ticks})
        self.assertEqual({(i["pid"], i["start_ticks"]) for i in target["tracked"]},
                         {(pid, facts[pid].ticks) for pid in pids + [main]})
        self.assertEqual(target["left_session"], [])
        signal_record = by_type["command_stop_signal"][0]
        self.assertEqual((signal_record["command_id"], signal_record["signal"], signal_record["stage"],
                          signal_record["auxiliary"], signal_record["failed"]),
                         (command_id, "SIGINT", "sigint", False, []))
        self.assertEqual({(i["pid"], i["start_ticks"]) for i in signal_record["targets"]},
                         {(pid, facts[pid].ticks) for pid in pids + [main]})
        self.assertRegex(signal_record["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        finished = by_type["command_stop_finished"][0]
        self.assertEqual((finished["command_id"], finished["status"], finished["exit_code"],
                          finished["repeat_requests"]), (command_id, "exited", 130, 0))
        for name in ("signal", "reason", "residual", "elapsed_seconds"):
            self.assertIn(name, finished)
        self.assertGreater(finished["elapsed_seconds"], 0)
        self.assertEqual(finished["residual"]["alive"], [])
        refused = by_type["command_stop_refused"][0]
        self.assertEqual((refused["status"], refused["reason"], refused["requested_by"]),
                         ("not_found", "unknown_command", "worker"))
        self.assertTrue(refused["command_id"] and refused["at"])
        text = json.dumps(self.stop_records())
        for forbidden in (ENV_VALUE, "WB_STOP_FIXTURE", "sleep 628", "628.1", "628.2", "cmdline", '"comm"', "argv",
                          SECRET, "/usr/bin/bash"):
            self.assertNotIn(forbidden, text, "no environment value, argv, comm or command string in stop records")

    def test_a_stale_stop_is_recorded_as_a_refusal_with_its_reason_and_no_signal_record(self):
        # SPEC command_stop_refused: "(not_found/refused/stale 포함)"
        started = self.run_command("sleep 628.5")
        (pid,) = self.find("sleep", "628.5")
        self.env.shift_after[pid] = 0
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "stale", result)
        types = [r["type"] for r in self.stop_records()]
        self.assertNotIn("command_stop_signal", types)
        refused = [r for r in self.records_of("command_stop_refused") if r["command_id"] == started["command_id"]]
        self.assertEqual([(r["status"], r["reason"], r["requested_by"]) for r in refused],
                         [("stale", "identity_mismatch", "worker")], types)
        self.kill_owned_and_wait_for_the_owner(pid)

    def test_an_unknown_end_is_recorded_with_its_reason_and_the_residual(self):
        started = self.run_command("trap '' INT; sleep 3.7")
        (pid,) = self.find("sleep", "3.7")
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "unknown", result)
        finished = self.records_of("command_stop_finished")[0]
        self.assertEqual((finished["status"], finished["reason"]), ("unknown", result["reason"]))
        self.assertTrue(finished["reason"])
        self.assertIn("unchecked", finished["residual"])
        self.assertTrue(wait_until(lambda: self.terminal.current()["running"] is False, 10))


if __name__ == "__main__":
    unittest.main()
