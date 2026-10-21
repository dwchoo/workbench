"""Independent verification of CS-07 (a lone user Ctrl+C reaches a verified Workbench command).

Seam S5: a real ``ShellPane`` over a real ``PersistentShell`` and Bash, driven through ``ShellPane.admit``
(and ``Backend.admit`` for the kind pass-through and the journal). Expected outcomes come from the
contracts (CS-07 ticket, SPEC "사용자 Ctrl+C" / "감사 기록" / "신원·신호 불변식", BRIEF CS-AC-16/17/20),
not from the implementation:

- CS-AC-16: a lone ``b"\\x03"`` of kind ``input`` reaches the verified foreground Workbench command
  whoever owns input (manager, takeover requested, takeover confirmed); the command ends through a real
  SIGINT (``main_exit == -2``, ``controller_exit == 130``); owner / takeover fields and the boundary's
  byte accounting (``input_sequence``, ``dirty``) are untouched.
- CS-AC-17: everything else is refused exactly as before and not one byte of it reaches the PTY. The proof
  is a command that puts its tty in raw mode and logs every byte it reads: the log must be empty after a
  refusal. User-owned jobs, prompts, unknown / control-lost state, a stopped command, a command whose
  child became the foreground, and anything after main exit never take the exception.
- CS-AC-20 (part): one ``command_interrupt`` record per delivered Ctrl+C, with the SPEC fields; none for a
  refusal; a listener / journal / resolution failure never withholds the Ctrl+C.
- Safety: a sentinel process and a second pane's command survive.

Every test owns its panes, temp dir and processes. Processes are signalled only through a pidfd opened
and checked against the start ticks recorded when the test saw them; ``ShellPane.close`` is the normal
shutdown and the shell session must be empty afterwards.
"""
from __future__ import annotations

from datetime import datetime
import inspect
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
import unittest
from unittest.mock import Mock
from uuid import uuid4

from workbench.backend.flow import HandoffService
from workbench.backend.panes import OmpPane, ShellPane
from workbench.backend.service import Backend
from workbench.contracts.ui_v1 import PaneId, Reason
from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState

BASH = ShellChoice("bash", "/usr/bin/bash")
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "xterm"}
PYTHON3 = "/usr/bin/python3"
SPEC_FIELDS = {"type", "requested_by", "command_id", "pid", "start_ticks", "process_group", "delivered",
               "input_owner", "parent_mode", "at"}


# -- process helpers (parsed by the test itself, never taken from production code) -------------------
def stat(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return None


def alive(pid, start=None):
    fields = stat(pid)
    return fields is not None and fields[0] not in {"Z", "X"} and (start is None or fields[19] == start)


def pgrp_of(pid):
    return int(stat(pid)[2])


def start_of(pid):
    return stat(pid)[19]


def kill_exact(pid, start, signum=signal.SIGKILL):
    """Signal ``pid`` only if it is still the process whose start ticks the test recorded."""
    try:
        descriptor = os.pidfd_open(pid)
    except OSError:
        return False
    try:
        if start is not None and (stat(pid) or [None] * 20)[19] != start:
            return False
        signal.pidfd_send_signal(descriptor, signum)
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


def session_members(session):
    found = {}
    for name in os.listdir("/proc"):
        if name.isdecimal():
            fields = stat(int(name))
            if fields and int(fields[3]) == session and fields[0] not in {"Z", "X"}:
                found[int(name)] = fields[19]
    return found


RAW_LOGGER = textwrap.dedent('''
    import os, sys, tty
    out = sys.argv[1]
    tty.setraw(0)                      # ISIG off: a Ctrl+C byte is data, never a signal
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    open(out + ".ready", "w").write(str(os.getpid()))
    while True:
        data = os.read(0, 1024)
        if not data:
            break
        os.write(fd, data)
''')

IGNORE_SIGINT = textwrap.dedent('''
    import os, signal, sys, time
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    open(sys.argv[1] + ".ready", "w").write(str(os.getpid()))
    time.sleep(60)
''')

# The command spawns a child that takes the terminal's foreground (its own process group).
OWN_GROUP_CHILD = textwrap.dedent('''
    import os, signal, sys, time
    out = sys.argv[1]
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    if sys.argv[2] == "wait":                      # let the test confirm a takeover first
        while not os.path.exists(out + ".go"):
            time.sleep(0.02)
    pid = os.fork()
    if pid == 0:
        os.setpgid(0, 0)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        time.sleep(60)
        os._exit(0)
    os.setpgid(pid, pid)
    os.tcsetpgrp(0, pid)
    open(out + ".child", "w").write(str(pid))
    open(out + ".ready", "w").write(str(os.getpid()))
    os.waitpid(pid, 0)
''')


class Managed:
    def __init__(self, pid, start, group, request_id, marker):
        self.pid, self.start, self.group, self.request_id, self.marker = pid, start, group, request_id, marker


class Case(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="cs07-ind-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.tracked: list[tuple[int, str]] = []
        self.sentinels: list[subprocess.Popen] = []
        self.panes: list[ShellPane] = []
        self.addCleanup(self.cleanup_processes)
        self.pane = self.new_pane()
        self.sentinel = self.new_sentinel()

    # -- ownership and cleanup ---------------------------------------------------------------
    def new_pane(self):
        home = self.directory / f"home-{len(self.panes)}"
        home.mkdir()
        pane = ShellPane(BASH, {**ENV, "HOME": str(home)})
        self.panes.append(pane)
        self.until(lambda: pane.state["parent_mode"] == "manual_prompt", pane=pane)
        return pane

    def new_sentinel(self):
        process = subprocess.Popen(["/usr/bin/sleep", "120"], start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sentinels.append(process)
        self.track(process.pid)
        return process

    def track(self, pid):
        start = stat(pid)
        if start is not None:
            self.tracked.append((pid, start[19]))
        return pid

    def cleanup_processes(self):
        sessions = []
        for pane in self.panes:
            sessions.append(pane.pid)
            try:
                survivors = pane.close()["survivors"]
            except Exception as exc:  # report, never mask the test's own failure
                survivors = [repr(exc)]
            self.assertEqual(survivors, [], "pane.close left survivors")
        for pid, start in self.tracked:
            kill_exact(pid, start)
        for process in self.sentinels:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.fail(f"test-started process {process.pid} survived SIGKILL")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(session_members(s) for s in sessions):
            time.sleep(0.02)
        for session in sessions:
            self.assertEqual(session_members(session), {}, "shell session processes survived the test")
        for pid, start in self.tracked:
            self.assertFalse(alive(pid, start), f"tracked test process {pid} survived")

    def until(self, condition, seconds=10.0, pane=None):
        pane = pane or self.pane
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pane.pump()
            if condition():
                return
            time.sleep(0.02)
        raise AssertionError({"timeout": pane.shell.snapshot()})

    def settle(self, seconds=0.4, pane=None):
        pane = pane or self.pane
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pane.pump()
            time.sleep(0.02)

    def assert_sentinel(self):
        self.assertIsNone(self.sentinel.poll(), "an unrelated process was signalled")

    # -- managed commands (manager-owned, as the Worker terminal / Manager experiment run them) ----
    def hand_off(self, pane=None):
        pane = pane or self.pane
        self.assertIsNone(pane.admit(b"wb-handoff\n"))
        self.until(lambda: pane.state["parent_mode"] == "control_wait", pane=pane)
        pane.state = pane.shell.claim_manager()

    def submit(self, script, pane=None):
        pane = pane or self.pane
        shell = pane.shell
        state = shell.snapshot()
        request_id = str(uuid4())
        shell.submit({"portVersion": 2, "kind": "ShellControl", "payload": {
            "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
            "requestId": request_id, "approvalHash": "e" * 64, "phase": "accepted"}}, script,
            {"portVersion": 2, "kind": "AutomationState", "payload": {
                "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}},
            return_timeout=120)
        return request_id

    def run_managed(self, tail="exec sleep 60", pane=None, hand_off=True, ready=None):
        pane = pane or self.pane
        if hand_off:
            self.hand_off(pane)
        marker = self.directory / f"pid-{uuid4().hex}"
        request_id = self.submit(f"echo $$ > {marker}; {tail}", pane)
        self.until(lambda: pane.state["lifecycle"]["experiment_started"] and marker.exists()
                   and marker.read_text().strip() and (ready is None or Path(str(ready) + ".ready").exists()),
                   pane=pane)
        pane.state = pane.shell.snapshot()
        pid = int(marker.read_text())
        self.track(pid)
        life = pane.state["lifecycle"]
        # the lifecycle's own idea of the child agrees with the pid the command reported about itself
        self.assertEqual(life["child_pid"], pid)
        return Managed(pid, start_of(pid), pgrp_of(pid), request_id, marker)

    def run_script(self, source, *args, pane=None, hand_off=True):
        """Run a Python program as the managed command; it writes ``<log>.ready`` with its pid."""
        log = self.directory / f"log-{uuid4().hex}"
        script = self.directory / f"prog-{uuid4().hex}.py"
        script.write_text(source)
        managed = self.run_managed(f"exec {PYTHON3} {script} {log} {' '.join(args)}", pane=pane, ready=log, hand_off=hand_off)
        ready_pid = int(Path(f"{log}.ready").read_text())
        self.assertEqual(ready_pid, managed.pid)
        return managed, log

    def logged(self, log):
        return log.read_bytes() if log.exists() else b""

    def fields(self, pane=None):
        state = (pane or self.pane).shell.snapshot()
        return {key: state[key] for key in ("input_owner", "owner_epoch", "takeover_requested", "takeover_confirmed")}

    def accounting(self, pane=None):
        boundary = (pane or self.pane).shell._transport.boundary
        return {key: getattr(boundary, key) for key in
                ("input_sequence", "dirty", "_handoff_ready", "uncertain", "needs_review", "control_lost",
                 "pending_line", "submitted_lines")}

    def wait_exit(self, managed, pane=None):
        pane = pane or self.pane
        self.until(lambda: not alive(managed.pid, managed.start), pane=pane)
        self.until(lambda: pane.state["lifecycle"]["main_exit"] is not None, pane=pane)

    def listener(self, pane=None):
        mock = Mock()
        (pane or self.pane).interrupt_listener = mock
        return mock

    def backend(self, terminal="default", handoffs="default"):
        backend = Backend.__new__(Backend)
        backend.panes = {PaneId.HOST_SHELL: self.pane}
        backend.handoffs = HandoffService(self.directory / "handoffs.jsonl") if handoffs == "default" else handoffs
        backend.terminal = terminal
        if terminal == "default":
            backend.terminal = Mock()
            backend.terminal.summary_command_active.side_effect = lambda command_id: True
        backend._wire_shell(self.pane)
        return backend

    def journal_records(self, backend):
        return backend.handoffs.read_records(["command_interrupt"])


# =================================================================================================
class DeliveryTests(Case):
    """CS-AC-16: delivery whoever owns input; real SIGINT; owner / takeover / accounting untouched."""

    def delivered(self, managed, listener, before_fields, before_accounting):
        self.assertIsNone(self.pane.admit(b"\x03"))
        self.assertEqual(self.accounting(), before_accounting)  # observe_*_bytes never called
        self.wait_exit(managed)
        life = self.pane.state["lifecycle"]
        self.assertEqual(life["main_exit"], -signal.SIGINT)  # killed by a real SIGINT
        listener.assert_called_once()
        self.assert_sentinel()

    def test_manager_owned_command_dies_of_sigint_with_exit_130_and_unchanged_owner(self):
        managed = self.run_managed()
        self.assertEqual(os.tcgetpgrp(self.pane._master_fd), managed.group)  # expected value from the kernel
        listener = self.listener()
        before, accounting = self.fields(), self.accounting()
        self.assertEqual(before["input_owner"], "manager")
        self.delivered(managed, listener, before, accounting)
        self.assertEqual(self.fields(), before)
        self.pane.shell.release_input()
        self.until(lambda: self.pane.state["lifecycle"]["controller_exit"] is not None)
        self.assertEqual(self.pane.state["lifecycle"]["controller_exit"], 130)

    def test_command_with_a_subprocess_in_the_same_group_loses_the_whole_group(self):
        managed = self.run_managed("sleep 60; sleep 60")  # no exec: the shell forks sleep into the same group
        self.until(lambda: any(pgrp_of(int(n)) == managed.group and int(n) != managed.pid
                               for n in os.listdir("/proc") if n.isdecimal() and stat(int(n))))
        members = [int(n) for n in os.listdir("/proc")
                   if n.isdecimal() and stat(int(n)) and int(stat(int(n))[2]) == managed.group]
        for member in members:
            self.track(member)
        listener = self.listener()
        self.assertIsNone(self.pane.admit(b"\x03"))
        self.wait_exit(managed)
        self.until(lambda: not any(alive(m) for m in members))
        self.assertIn(self.pane.state["lifecycle"]["main_exit"], (-signal.SIGINT, 130))
        listener.assert_called_once()
        self.assert_sentinel()

    def test_unconfirmed_takeover_request_delivers_and_changes_nothing(self):
        managed = self.run_managed()
        self.pane.state = self.pane.shell.request_takeover()
        self.assertTrue(self.pane.state["takeover_requested"])
        self.assertFalse(self.pane.state["takeover_confirmed"])
        listener = self.listener()
        before, accounting = self.fields(), self.accounting()
        self.delivered(managed, listener, before, accounting)
        after = self.fields()
        for key in ("input_owner", "owner_epoch", "takeover_requested"):
            self.assertEqual(after[key], before[key])

    def test_confirmed_takeover_delivers_with_no_accounting_side_effect(self):
        managed = self.run_managed()
        self.pane.shell.request_takeover()
        self.pane.state = self.pane.shell.confirm_takeover()
        self.assertEqual(self.pane.state["foreground_target"]["process_group"], managed.group)
        listener = self.listener()
        before, accounting = self.fields(), self.accounting()
        self.assertEqual((before["input_owner"], before["takeover_confirmed"]), ("user", True))
        # ticket "risk point": the old confirmed path counted this byte as experiment input
        self.delivered(managed, listener, before, accounting)
        after = self.fields()
        for key in ("input_owner", "owner_epoch", "takeover_requested"):
            self.assertEqual(after[key], before[key])

    def test_nothing_else_in_the_machine_is_signalled_and_a_second_pane_keeps_running(self):
        other = self.new_pane()
        theirs = self.run_managed(pane=other)
        mine = self.run_managed()
        theirs_listener, my_listener = self.listener(other), self.listener()
        self.assertIsNone(self.pane.admit(b"\x03"))
        self.wait_exit(mine)
        self.settle(0.5, pane=other)
        self.assertTrue(alive(theirs.pid, theirs.start), "another pane's command was interrupted")
        theirs_listener.assert_not_called()
        my_listener.assert_called_once()
        self.assert_sentinel()


class SigintIgnoredAndRawModeTests(Case):
    """Delivery means 'the byte was written'; the kernel decides the rest (SPEC 확정된 결정 3)."""

    def test_raw_mode_command_gets_the_byte_as_data_and_is_not_killed(self):
        managed, log = self.run_script(RAW_LOGGER)
        listener = self.listener()
        self.assertIsNone(self.pane.admit(b"\x03"))
        self.until(lambda: self.logged(log) == b"\x03")
        self.assertTrue(alive(managed.pid, managed.start))
        listener.assert_called_once()

    def test_a_command_ignoring_sigint_survives_and_every_delivered_ctrl_c_is_recorded_once(self):
        managed, log = self.run_script(IGNORE_SIGINT)
        listener = self.listener()
        for count in (1, 2, 3):
            self.assertIsNone(self.pane.admit(b"\x03"))
            self.assertEqual(listener.call_count, count)
        self.settle(0.3)
        self.assertTrue(alive(managed.pid, managed.start))
        self.assertEqual(self.pane.state["lifecycle"]["main_exit"], None)
        events = [call.args[0] for call in listener.call_args_list]
        self.assertEqual({(e["pid"], e["start_ticks"], e["process_group"]) for e in events},
                         {(managed.pid, int(managed.start), managed.group)})


class RefusalTests(Case):
    """CS-AC-17: no partial bytes; the oracle is a raw-mode command that logs every byte it reads."""

    def refusals(self, cases, reason):
        managed, log = self.run_script(RAW_LOGGER)
        listener = self.listener()
        before, accounting = self.fields(), self.accounting()
        reference = self.pane.admit(b"a")  # what the current rule says for an ordinary key
        self.assertIsNotNone(reference)
        self.assertEqual(reference[0], reason)
        for data, kind in cases:
            with self.subTest(data=data, kind=kind):
                refused = self.pane.admit(data, kind)
                self.assertIsNotNone(refused)
                self.assertEqual(refused[0], reason)
        self.settle(0.4)
        self.assertEqual(self.logged(log), b"", "a refused input reached the PTY")
        self.assertEqual(len(self.pane._pending), 0)
        self.assertEqual(self.pane.dropped_input_bytes, 0)
        self.assertTrue(alive(managed.pid, managed.start))
        self.assertEqual(self.fields(), before)
        self.assertEqual(self.accounting(), accounting)
        listener.assert_not_called()
        # the command is still a verified target: a lone Ctrl+C still gets through afterwards (raw: as data)
        self.assertIsNone(self.pane.admit(b"\x03"))
        self.until(lambda: self.logged(log) == b"\x03")
        listener.assert_called_once()

    CASES = ((b"a", "input"), (b"\x03a", "input"), (b"a\x03", "input"), (b"\x03\x03", "input"),
             (b"\x03\x03\x03", "input"), (b"\x02", "input"), (b"\x1a", "input"), (b"\x04", "input"),
             (b"\x1b[A", "input"), (b"", "input"), (b"\x03", "paste"), (b"\x03\x03", "paste"),
             (b"\x03", "text"), (b"\x03", ""), (b"\x03", "INPUT"), (b"\x03", "resize"))

    def test_manager_owned_refusals_deliver_not_one_byte(self):
        self.refusals(self.CASES, Reason.INPUT_OWNER_MANAGER)

    def test_takeover_requested_only_refusals_deliver_not_one_byte(self):
        managed, log = self.run_script(RAW_LOGGER)
        self.pane.state = self.pane.shell.request_takeover()
        listener = self.listener()
        before, accounting = self.fields(), self.accounting()
        for data, kind in self.CASES:
            with self.subTest(data=data, kind=kind):
                refused = self.pane.admit(data, kind)
                self.assertIsNotNone(refused)
                self.assertEqual(refused[0], Reason.INPUT_TARGET_UNKNOWN)
        self.settle(0.4)
        self.assertEqual(self.logged(log), b"")
        self.assertEqual(self.fields(), before)
        self.assertEqual(self.accounting(), accounting)
        listener.assert_not_called()

    def test_confirmed_takeover_keeps_the_current_rule_for_everything_but_the_lone_ctrl_c(self):
        managed, log = self.run_script(RAW_LOGGER)
        self.pane.shell.request_takeover()
        self.pane.state = self.pane.shell.confirm_takeover()
        listener = self.listener()
        expected = b""
        # current rule: experiment input goes through whole, in order, and is not an interrupt
        for data, kind in ((b"a", "input"), (b"\x03a", "input"), (b"a\x03", "input"), (b"\x03\x03", "input"),
                           (b"\x03", "paste")):
            with self.subTest(data=data, kind=kind):
                self.assertIsNone(self.pane.admit(data, kind))
                expected += data
                self.until(lambda: self.logged(log) == expected)
        listener.assert_not_called()  # none of those is the exception
        self.assertIsNone(self.pane.admit(b"\x03"))  # the lone Ctrl+C: new path, audited once
        expected += b"\x03"
        self.until(lambda: self.logged(log) == expected)
        listener.assert_called_once()

    def test_backend_admit_passes_the_kind_so_paste_is_not_an_interrupt(self):
        managed, log = self.run_script(RAW_LOGGER)
        backend = self.backend()
        refused = backend.admit(PaneId.HOST_SHELL, b"\x03", "paste")
        self.assertEqual(refused[0], Reason.INPUT_OWNER_MANAGER)
        self.settle(0.3)
        self.assertEqual(self.logged(log), b"")
        self.assertEqual(self.journal_records(backend), [])



class NonWorkbenchForegroundTests(Case):
    """The exception never applies to a foreground that is not a verified Workbench command."""

    def test_user_started_job_gets_the_ordinary_rule_and_no_record(self):
        listener = self.listener()
        self.assertIsNone(self.pane.admit(b"sleep 60\n"))
        self.until(lambda: os.tcgetpgrp(self.pane._master_fd) != self.pane.pid)
        group = os.tcgetpgrp(self.pane._master_fd)
        self.until(lambda: self.pane.state["parent_mode"] == "manual_foreground")
        for pid in [int(n) for n in os.listdir("/proc") if n.isdecimal() and stat(int(n))
                    and int(stat(int(n))[2]) == group]:
            self.track(pid)
        self.assertIsNone(self.pane.admit(b"\x03"))  # forwarded like any manual key (SIGINT to the user's job)
        self.until(lambda: not alive(group))
        listener.assert_not_called()
        self.assert_sentinel()

    def test_python_repl_gets_the_ordinary_rule_and_no_record(self):
        listener = self.listener()
        marker = self.directory / "repl-ready"
        code = ("import os, signal, time; "
                "signal.signal(signal.SIGINT, lambda *a: open(%r + '.int', 'w').write('x')); "
                "open(%r, 'w').write(str(os.getpid())); time.sleep(60)" % (str(marker), str(marker)))
        self.assertIsNone(self.pane.admit(f"{PYTHON3} -c \"{code}\"\n".encode()))
        self.until(lambda: marker.exists() and marker.read_text().strip())
        pid = self.track(int(marker.read_text()))
        self.until(lambda: self.pane.state["parent_mode"] == "manual_foreground")
        self.assertIsNone(self.pane.admit(b"\x03"))
        self.until(lambda: Path(f"{marker}.int").exists())  # the user's REPL-like program handled its SIGINT
        self.assertTrue(alive(pid))
        listener.assert_not_called()

    def test_idle_user_prompt_gets_the_ordinary_rule_and_no_record(self):
        listener = self.listener()
        self.assertIsNone(self.pane.admit(b"typed"))
        self.until(lambda: self.pane.state["parent_mode"] == "manual_input")
        self.assertIsNone(self.pane.admit(b"\x03"))
        listener.assert_not_called()

    def test_idle_control_wait_with_no_run_refuses_ctrl_c_like_any_key(self):
        self.hand_off()
        listener = self.listener()
        reference = self.pane.admit(b"a")
        refused = self.pane.admit(b"\x03")
        self.assertIsNotNone(refused)
        self.assertEqual(refused[0], reference[0])
        self.assertEqual(refused[0], Reason.INPUT_OWNER_MANAGER)
        listener.assert_not_called()

    def test_a_user_job_started_after_a_managed_run_is_not_a_workbench_command(self):
        managed = self.run_managed("exec sleep 0.3")
        self.until(lambda: self.pane.state["lifecycle"]["main_exit"] is not None)
        self.pane.state = self.pane.shell.request_takeover()
        self.pane.shell.release_input()
        self.until(lambda: self.pane.state["parent_mode"] == "manual_prompt")
        listener = self.listener()
        self.assertIsNone(self.pane.admit(b"sleep 60\n"))
        self.until(lambda: self.pane.state["parent_mode"] == "manual_foreground")
        group = os.tcgetpgrp(self.pane._master_fd)
        for n in os.listdir("/proc"):
            if n.isdecimal() and stat(int(n)) and int(stat(int(n))[2]) == group:
                self.track(int(n))
        self.assertIsNone(self.pane.admit(b"\x03"))
        self.until(lambda: not alive(group))
        listener.assert_not_called()

    def test_after_main_exit_the_current_rule_applies_and_nothing_is_recorded(self):
        managed = self.run_managed("exec sleep 0.3")
        listener = self.listener()
        self.until(lambda: self.pane.state["lifecycle"]["main_exit"] is not None)
        self.assertFalse(alive(managed.pid, managed.start))
        reference = self.pane.admit(b"a")
        refused = self.pane.admit(b"\x03")
        self.assertIsNotNone(refused)
        self.assertEqual(refused[0], reference[0])
        listener.assert_not_called()

    def test_unknown_lifecycle_state_never_takes_the_exception(self):
        managed, log = self.run_script(RAW_LOGGER)
        listener = self.listener()
        self.pane.shell._transport.lifecycle.fail_unknown("cs07_independent_unknown")
        self.pane.state = self.pane.shell.snapshot()
        self.assertEqual(self.pane.state["phase"], "unknown")
        self.assertIsNotNone(self.pane.admit(b"\x03"))
        self.settle(0.3)
        self.assertEqual(self.logged(log), b"")
        listener.assert_not_called()

    def test_control_lost_never_takes_the_exception(self):
        managed, log = self.run_script(RAW_LOGGER)
        listener = self.listener()
        self.pane.shell._transport.boundary.control_lost = True
        self.assertIsNotNone(self.pane.admit(b"\x03"))
        self.settle(0.3)
        self.assertEqual(self.logged(log), b"")
        listener.assert_not_called()

    def boundary_flag_blocks(self, attribute):
        managed, log = self.run_script(RAW_LOGGER)
        listener = self.listener()
        setattr(self.pane.shell._transport.boundary, attribute, True)
        self.assertIsNotNone(self.pane.admit(b"\x03"))
        self.settle(0.3)
        self.assertEqual(self.logged(log), b"")
        listener.assert_not_called()
        self.assertTrue(alive(managed.pid, managed.start))

    def test_boundary_needs_review_never_takes_the_exception(self):
        self.boundary_flag_blocks("needs_review")

    def test_boundary_uncertain_never_takes_the_exception(self):
        self.boundary_flag_blocks("uncertain")

    def test_a_stopped_command_is_not_a_target(self):
        managed = self.run_managed()
        listener = self.listener()
        kill_exact(managed.pid, managed.start, signal.SIGSTOP)
        self.until(lambda: stat(managed.pid)[0] == "T")
        self.until(lambda: self.pane.state["lifecycle"]["main_stopped"])
        self.assertIsNotNone(self.pane.admit(b"\x03"))
        listener.assert_not_called()
        kill_exact(managed.pid, managed.start, signal.SIGCONT)
        self.settle(0.5)
        self.assertTrue(alive(managed.pid, managed.start), "a SIGINT was pending for the stopped command")

    def test_a_child_that_became_the_foreground_is_not_the_verified_command(self):
        managed, log = self.run_script(OWN_GROUP_CHILD, "now")
        child = self.track(int(Path(f"{log}.child").read_text()))
        listener = self.listener()
        self.assertEqual(os.tcgetpgrp(self.pane._master_fd), child)
        self.assertNotEqual(child, managed.group)
        refused = self.pane.admit(b"\x03")
        self.assertIsNotNone(refused)
        self.assertEqual(refused[0], Reason.INPUT_OWNER_MANAGER)
        self.settle(0.4)
        self.assertTrue(alive(child), "the child's group received a SIGINT")
        self.assertTrue(alive(managed.pid, managed.start))
        listener.assert_not_called()

    def test_a_child_that_became_the_foreground_after_a_takeover_confirmation_gets_no_interrupt(self):
        log = self.directory / f"log-{uuid4().hex}"
        script = self.directory / "prog-wait.py"
        script.write_text(OWN_GROUP_CHILD)
        managed = self.run_managed(f"exec {PYTHON3} {script} {log} wait")
        self.pane.shell.request_takeover()
        self.pane.state = self.pane.shell.confirm_takeover()
        self.assertEqual(self.pane.state["foreground_target"]["process_group"], managed.group)
        Path(f"{log}.go").write_text("go")
        self.until(lambda: Path(f"{log}.child").exists() and Path(f"{log}.ready").exists())
        child = self.track(int(Path(f"{log}.child").read_text()))
        self.until(lambda: os.tcgetpgrp(self.pane._master_fd) == child)
        listener = self.listener()
        self.pane.admit(b"\x03")  # the current rule decides what happens to the byte, not the new exception
        self.settle(0.5)
        self.assertTrue(alive(child), "the child's group received a SIGINT")
        listener.assert_not_called()

    def test_pane_error_state_does_not_deliver(self):
        managed, log = self.run_script(RAW_LOGGER)
        listener = self.listener()
        self.pane.error = "UnsafeShellState: injected boundary error"
        refused = self.pane.admit(b"\x03")
        self.settle(0.3)
        self.assertEqual((refused is not None, self.logged(log), listener.call_count), (True, b"", 0),
                         "ShellPane.error means the boundary is unknown; the exception must not apply")


class IdentityReverificationTests(Case):
    """interrupt_run / send_interrupt re-verify against lifecycle and /proc right before the write."""

    def test_every_identity_field_is_checked_by_send_interrupt(self):
        managed, log = self.run_script(RAW_LOGGER)
        transport = self.pane.shell._transport
        good = {"request_id": managed.request_id, "pid": managed.pid, "start_ticks": int(managed.start),
                "process_group": managed.group}
        self.assertEqual(transport._interrupt_target(), good)  # the expected identity, computed by this test
        for field, wrong in (("request_id", str(uuid4())), ("pid", managed.pid + 1),
                             ("start_ticks", int(managed.start) + 1), ("process_group", managed.group + 1)):
            with self.subTest(field=field):
                with self.assertRaises(UnsafeShellState):
                    transport.send_interrupt({**good, field: wrong})
        self.settle(0.3)
        self.assertEqual(self.logged(log), b"")

    def test_a_target_of_an_earlier_run_is_stale_for_a_later_run(self):
        first, _ = self.run_script(RAW_LOGGER)
        transport = self.pane.shell._transport
        old = transport._interrupt_target()
        self.assertIsNotNone(old)
        kill_exact(first.pid, first.start)
        self.until(lambda: self.pane.state["lifecycle"]["main_exit"] is not None)
        self.pane.state = self.pane.shell.request_takeover()  # the user will get the prompt back
        self.pane.shell.release_input()
        self.until(lambda: self.pane.state["parent_mode"] == "manual_prompt")
        second, log = self.run_script(RAW_LOGGER)
        self.assertNotEqual(second.request_id, first.request_id)
        with self.assertRaises(UnsafeShellState):
            transport.send_interrupt(old)
        self.settle(0.3)
        self.assertEqual(self.logged(log), b"")

    def test_identity_not_yet_known_is_not_a_target(self):
        managed, log = self.run_script(RAW_LOGGER)
        transport = self.pane.shell._transport
        saved, transport._child_start = transport._child_start, None  # EXEC_READY not seen yet
        try:
            self.assertIsNone(self.pane.shell.interrupt_run())
            self.assertIsNotNone(self.pane.admit(b"\x03"))
        finally:
            transport._child_start = saved
        self.settle(0.3)
        self.assertEqual(self.logged(log), b"")

    def test_the_world_changing_between_verification_and_write_withholds_the_byte(self):
        managed, log = self.run_script(RAW_LOGGER)
        transport = self.pane.shell._transport
        real, calls = transport._foreground_group, []

        def flaky():
            calls.append(1)
            return real() if len(calls) == 1 else managed.group + 1  # the foreground moved after the first look

        transport._foreground_group = flaky
        try:
            self.assertIsNone(self.pane.shell.interrupt_run())
        finally:
            del transport._foreground_group
        self.assertGreaterEqual(len(calls), 2, "the target was not looked at again before writing")
        self.settle(0.3)
        self.assertEqual(self.logged(log), b"")

    def test_the_confirmed_takeover_helper_keeps_its_owner_requirement(self):
        managed = self.run_managed()
        transport = self.pane.shell._transport
        self.assertEqual(transport.boundary.owner, "manager")
        self.assertIsNone(transport._current_foreground_child_group())  # existing meaning unchanged
        self.assertEqual(transport._current_foreground_child_group(any_owner=True), managed.group)

    def test_a_direct_interrupt_run_does_not_touch_the_owner_or_takeover_fields(self):
        managed = self.run_managed()
        before, accounting = self.fields(), self.accounting()
        target = self.pane.shell.interrupt_run()
        self.assertEqual({key: target[key] for key in ("request_id", "pid", "start_ticks", "process_group")},
                         {"request_id": managed.request_id, "pid": managed.pid,
                          "start_ticks": int(managed.start), "process_group": managed.group})
        self.assertEqual(self.accounting(), accounting)
        self.assertEqual(self.fields(), before)
        self.wait_exit(managed)

    def test_ctrl_c_spam_from_submit_on_only_fires_once_the_identity_is_verified(self):
        self.hand_off()
        listener = self.listener()
        self.submit("exec sleep 60")  # no marker: the first Ctrl+C may legitimately land before any script line
        deadline = time.monotonic() + 10
        refusals = deliveries = 0
        seen = {}
        while time.monotonic() < deadline and self.pane.state["lifecycle"]["main_exit"] is None:
            life = self.pane.state["lifecycle"]
            if life["child_pid"] and alive(life["child_pid"]):
                seen[life["child_pid"]] = start_of(life["child_pid"])  # ticks read by the test, as early as possible
            before = listener.call_count
            refused = self.pane.admit(b"\x03")
            self.assertEqual(listener.call_count, before + (1 if refused is None else 0),
                             "exactly one record per delivered Ctrl+C, none per refusal")
            if refused is None:
                deliveries += 1
                after = self.pane.shell.snapshot()["lifecycle"]  # monotonic: true now means true at the write
                self.assertTrue(after["experiment_started"] and after["exec_ready"] and after["foreground_verified"], after)
            else:
                refusals += 1
                self.assertEqual(refused[0], Reason.INPUT_OWNER_MANAGER)
            self.pane.pump()
        life = self.pane.state["lifecycle"]
        self.assertEqual(life["main_exit"], -signal.SIGINT)
        self.assertGreaterEqual(deliveries, 1)
        self.assertEqual(deliveries, listener.call_count)
        for call in listener.call_args_list:
            event = call.args[0]
            self.assertEqual((event["pid"], event["process_group"]), (life["child_pid"], life["child_group"]))
            self.assertEqual(event["request_id"], life["request_id"])
            if event["pid"] in seen:
                self.assertEqual(event["start_ticks"], int(seen[event["pid"]]))
        self.assert_sentinel()


class AuditTests(Case):
    """CS-AC-20 (part) at the Backend level with a real handoff journal on disk."""

    def test_one_record_per_delivered_ctrl_c_with_the_spec_fields(self):
        managed = self.run_managed()
        backend = self.backend()
        self.assertIsNone(backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        self.wait_exit(managed)
        (record,) = self.journal_records(backend)
        self.assertTrue(SPEC_FIELDS <= set(record), set(record))
        self.assertEqual(record["type"], "command_interrupt")
        self.assertEqual(record["requested_by"], "user")
        self.assertEqual(record["command_id"], managed.request_id)  # terminal commands: request_id == command_id
        self.assertEqual((record["pid"], record["start_ticks"], record["process_group"]),
                         (managed.pid, int(managed.start), managed.group))
        self.assertIs(record["delivered"], True)
        self.assertEqual((record["input_owner"], record["parent_mode"]), ("manager", "control_wait"))
        datetime.fromisoformat(record["at"])
        for forbidden in ("argv", "cmdline", "comm", "env", "environment", "command"):
            self.assertNotIn(forbidden, record)
        self.assertNotIn(self.directory.name, repr(record))  # no script text / paths from the command

    def test_a_refusal_and_a_second_ctrl_c_after_the_end_leave_exactly_one_record(self):
        managed = self.run_managed()
        backend = self.backend()
        self.assertIsNotNone(backend.admit(PaneId.HOST_SHELL, b"a", "input"))
        self.assertIsNotNone(backend.admit(PaneId.HOST_SHELL, b"\x03\x03", "input"))
        self.assertEqual(self.journal_records(backend), [])
        self.assertIsNone(backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        self.wait_exit(managed)
        self.assertIsNotNone(backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))  # command gone: current rule
        self.assertEqual(len(self.journal_records(backend)), 1)

    def unresolved(self, terminal):
        managed = self.run_managed()
        backend = self.backend(terminal=terminal)
        self.assertIsNone(backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        self.wait_exit(managed)
        records = self.journal_records(backend)
        self.assertEqual(len(records), 1, "a registry that cannot resolve must still leave a null-id record")
        self.assertIsNone(records[0]["command_id"])
        self.assertEqual(records[0]["pid"], managed.pid)

    def test_no_terminal_service_gives_a_null_command_id_and_still_delivers(self):
        self.unresolved(None)

    def test_a_registry_that_does_not_know_the_request_gives_a_null_command_id(self):
        terminal = Mock()
        terminal.summary_command_active.return_value = False
        self.unresolved(terminal)

    def test_a_registry_that_raises_gives_a_null_command_id_and_still_delivers(self):
        terminal = Mock()
        terminal.summary_command_active.side_effect = RuntimeError("registry unavailable")
        self.unresolved(terminal)

    def journal_fails(self, error):
        managed = self.run_managed()
        handoffs = Mock()
        handoffs.record.side_effect = error
        backend = self.backend(handoffs=handoffs)
        self.assertIsNone(backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        self.wait_exit(managed)
        self.assertEqual(self.pane.state["lifecycle"]["main_exit"], -signal.SIGINT)

    def test_journal_oserror_never_withholds_the_ctrl_c(self):
        self.journal_fails(OSError("disk full"))

    def test_journal_runtime_error_never_withholds_the_ctrl_c(self):
        self.journal_fails(RuntimeError("journal down"))

    def test_journal_value_error_never_withholds_the_ctrl_c(self):
        self.journal_fails(ValueError("bad record"))

    def test_a_raising_listener_never_withholds_the_ctrl_c(self):
        managed = self.run_managed()
        self.pane.interrupt_listener = Mock(side_effect=RuntimeError('listener down'))
        self.assertIsNone(self.pane.admit(b"\x03"))
        self.wait_exit(managed)

    def test_listener_is_told_under_the_pane_lock_and_exactly_the_spec_identity(self):
        managed = self.run_managed()
        seen = []
        pane = self.pane

        def listener(event):
            seen.append((event, pane.io_lock._is_owned() if hasattr(pane.io_lock, "_is_owned") else None))

        pane.interrupt_listener = listener
        self.assertIsNone(pane.admit(b"\x03"))
        self.wait_exit(managed)
        (event, owned), = seen
        self.assertIn(owned, (True, None))
        self.assertTrue({"pid", "start_ticks", "process_group", "delivered", "request_id"} <= set(event))
        self.assertEqual(event["request_id"], managed.request_id)


class KindPassThroughTests(unittest.TestCase):
    def test_backend_admit_hands_the_kind_to_the_pane_unchanged(self):
        seen = []

        class FakePane:
            def admit(self, data, kind="input"):
                seen.append((data, kind))
                return None

        backend = Backend.__new__(Backend)
        backend.panes = {PaneId.HOST_SHELL: FakePane(), PaneId.MANAGER_OMP: FakePane()}
        for pane_id in (PaneId.HOST_SHELL, PaneId.MANAGER_OMP):
            for kind in ("input", "paste"):
                backend.admit(pane_id, b"\x03", kind)
        self.assertEqual(seen, [(b"\x03", "input"), (b"\x03", "paste")] * 2)

    def test_omp_pane_takes_the_kind_and_ignores_it(self):
        parameters = inspect.signature(OmpPane.admit).parameters
        self.assertIn("kind", parameters)
        self.assertEqual(parameters["kind"].default, "input")


if __name__ == "__main__":
    unittest.main()
