"""C-T1b (independent test_designer): the ``_stage`` split and the forbidden-pid fallback of ``command_stop``.

Contracts under test
--------------------
bb0f810  The pids an owner forbids are read through one helper. If the provider fails when the episode finishes (the
         residual scan after the owner's release), the LAST SUCCESSFULLY READ set is used, so a pid the owner forbade
         is never listed as a tracked residual; a set is replaced by the next successful read (not accumulated). A
         provider that fails before the first signal leaves nothing signalled, pinned or marked, and the stop
         ``unknown`` (``episode_error:*``). A provider that fails between two stages fails closed: no further signal.
89cbc08  ``_stage`` was split into pin / signal / unsent-result helpers and must behave exactly as before: the same
         signals in the same order (SIGINT -> SIGTERM -> SIGKILL, an auxiliary SIGCONT to a stopped member that was
         reached, never after SIGKILL), the stop marker attached right before the first signal attempt, the pins of a
         stage closed before the next stage (and while the owner is waited for), and the same outcome and reason
         codes for stale / identity_mismatch / permission_denied / ended / pidfd_unavailable / different_uid and
         session-changed members.

Method
------
* The ``/proc`` table is synthetic (``Table``): every call into the environment (reads, pidfd open/exited/close/send,
  boot id) is traced, the signal hook captures the stop marker at the instant a signal leaves.
* A scenario matrix is run against the pre-refactor source (``git show bb0f810:...`` loaded under another module name)
  and against the refactored module, and the results, journals and full environment traces must be IDENTICAL. The
  expected values of the main scenarios are also written out below so that the oracle does not depend on the baseline.
* Real processes: a session with a process tree (host -> supervisor -> command + sibling) whose members ignore
  SIGINT/SIGTERM, a stopped member that needs SIGCONT, a member that exits between pin and send, a member that
  changes session after the scan; the same differential comparison on signal sequences.

Hygiene: every real process is started here, recorded with its start ticks, and killed at the end through a pidfd
whose start ticks were re-verified; nothing else is signalled.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import Counter
from dataclasses import replace
from uuid import uuid4

from workbench.backend import command_stop as current

REPO = Path(__file__).resolve().parents[2]
BASELINE_REV = "bb0f810"  # the commit just before the split (it carries the forbidden fallback)
BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
UID = os.getuid()
VOLATILE = {"at", "ended_at", "elapsed_seconds", "requested_at", "seq"}


def load_baseline():
    """The ``command_stop`` module as of ``BASELINE_REV``, under its own name, or None when git cannot give it."""
    try:
        source = subprocess.run(["git", "-C", str(REPO), "show", f"{BASELINE_REV}:src/workbench/backend/command_stop.py"],
                                capture_output=True, text=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    module = types.ModuleType("command_stop_baseline_pre_split")
    module.__file__ = f"<{BASELINE_REV}:command_stop.py>"
    sys.modules[module.__name__] = module  # dataclasses look their module up
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


BASELINE = load_baseline()
needs_baseline = unittest.skipIf(BASELINE is None, f"git cannot provide {BASELINE_REV} (differential checks only)")


def strip(value):
    """A journal/result value without timestamps and counters that differ between two runs."""
    if isinstance(value, dict):
        return {key: strip(item) for key, item in value.items() if key not in VOLATILE}
    if isinstance(value, (list, tuple)):
        return [strip(item) for item in value]
    return value


# ----------------------------------------------------------------------------------------------------------------
# the synthetic /proc table
# ----------------------------------------------------------------------------------------------------------------
HOST, SUP, CHILD, A, B = 940, 941, 942, 943, 944
SESSION = 900
INT, TERM, KILL, CONT = signal.SIGINT, signal.SIGTERM, signal.SIGKILL, signal.SIGCONT


class Table:
    """A synthetic ``/proc`` that traces every call and reacts to signals as the scenario says.

    Per-pid ``dies_on``: signals that end the member; a stopped member (state T) dies on its pending signal when it is
    continued. ``end_when_gone``: the owner reports the command's end once all of these pids are gone.
    """

    def __init__(self, mod, stats):
        self.mod = mod
        self.table = {stat.pid: stat for stat in stats}
        self.trace, self.fds, self.reads, self.exited_calls = [], {}, Counter(), Counter()
        self.service = self.cid = None
        self.boot_hooks, self.read_hooks = [], {}
        self.boot_nth, self.list_nth, self.boot_calls, self.list_calls = {}, {}, 0, 0  # hooks by call number
        self.exited_true_from = {}  # pid -> the n-th pidfd_exited call (per pidfd) from which the pid reads as exited
        self.open_error = lambda pid: None
        self.send_error = lambda pid, signum: None
        self.on_signal = lambda pid, signum: None
        self.dies_on, self.pending, self.end_when_gone, self.restop = {}, set(), set(), set()
        self.sent_total = 0
        self.forbidden_open = []  # the number of open pidfds each time the owner's provider was asked
        self.identity_hook = lambda call: None
        self.identity_patch = {}
        self.identity_calls = 0

    # -- environment ---------------------------------------------------------------------------
    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def boot_id(self):
        self.trace.append(("boot_id",))
        self.boot_calls += 1
        if self.boot_calls in self.boot_nth:
            self.boot_nth[self.boot_calls](self)
        hooks, self.boot_hooks = self.boot_hooks, []
        for hook in hooks:
            hook(self)
        return BOOT_ID

    def list_pids(self):
        self.trace.append(("list",))
        self.list_calls += 1
        if self.list_calls in self.list_nth:
            self.list_nth[self.list_calls](self)
        return list(self.table)

    def read_stat(self, pid):
        self.reads[pid] += 1
        hook = self.read_hooks.get((pid, self.reads[pid]))
        if hook:
            hook(self)
        found = self.table.get(pid)
        self.trace.append(("read", pid, None if found is None else (found.state, found.start_ticks, found.session, found.uid)))
        return found

    def pidfd_open(self, pid):
        error = self.open_error(pid)
        if error is not None:
            self.trace.append(("open_error", pid, error.errno))
            raise error
        fd = 1000 + len(self.fds) + sum(1 for event in self.trace if event[0] == "open")
        self.fds[fd] = pid
        self.trace.append(("open", pid))
        return fd

    def pidfd_exited(self, fd):
        self.exited_calls[fd] += 1
        pid = self.fds[fd]
        start = self.exited_true_from.get(pid)
        exited = start is not None and self.exited_calls[fd] >= start
        self.trace.append(("exited?", pid, exited))
        return exited

    def close(self, fd):
        self.trace.append(("close", self.fds.pop(fd)))

    def pidfd_send_signal(self, fd, signum):
        pid = self.fds[fd]
        marker = tuple(sorted(self.service.stop_marker(self.cid).items()))  # captured while the signal is leaving
        self.trace.append(("send", pid, int(signum), marker, tuple(sorted(self.fds.values()))))
        error = self.send_error(pid, signum)
        if error is not None:
            self.trace.append(("send_error", pid, int(signum), type(error).__name__))
            raise error
        self.sent_total += 1
        self.react(pid, signum)
        self.on_signal(pid, signum)

    # -- the processes' reaction -----------------------------------------------------------------
    def react(self, pid, signum):
        stat = self.table.get(pid)
        if stat is None:
            return
        if signum == KILL:
            if KILL in self.dies_on.get(pid, ()):  # an immortal synthetic member survives it (the table is only a model)
                self.die(pid)
        elif signum == CONT:
            if stat.state == "T":
                self.table[pid] = replace(stat, state="T" if pid in self.restop else "S")  # restop: stopped again at once
                if pid in self.pending and pid not in self.restop:
                    self.die(pid)
        elif signum in self.dies_on.get(pid, ()):
            if stat.state == "T":
                self.pending.add(pid)
            else:
                self.die(pid)

    def die(self, pid):
        self.table.pop(pid, None)
        if self.end_when_gone and not (self.end_when_gone & set(self.table)):
            gone, self.end_when_gone = self.end_when_gone, set()
            self.service.command_ended(self.cid, {"status": "exited", "exit_code": 137, "signal": 9})


def world(mod):
    def stat(pid, ppid, group, **extra):
        return mod.ProcStat(pid, extra.get("state", "S"), ppid, group, extra.get("session", SESSION), 1000 + pid,
                            extra.get("uid", UID))
    return [stat(HOST, 1, HOST), stat(SUP, HOST, HOST), stat(CHILD, SUP, CHILD), stat(A, CHILD, CHILD),
            stat(B, CHILD, CHILD)]


def run_synthetic(mod, setup, *, stats=None, forbidden=lambda: frozenset(), wait=0.05):
    """Run one stop over a synthetic table; returns the traced outcome."""
    policy = mod.StopPolicy(identity_wait=0.3, sigint_wait=wait, sigterm_wait=wait, sigkill_wait=wait, call_wait=10.0,
                            poll_interval=0.005)
    env = Table(mod, stats if stats is not None else world(mod))
    journal = []
    service = mod.CommandStopService(journal=journal.append, policy=policy, env=env)
    env.service, env.cid = service, str(uuid4())

    def identity():
        env.identity_calls += 1
        env.identity_hook(env.identity_calls)
        return {"request_id": env.cid, "phase": "running", "uid": UID, "boot_id": BOOT_ID, "session": SESSION,
                "child_group": CHILD, "parent": {"pid": HOST, "start_ticks": 1000 + HOST},
                "supervisor": {"pid": SUP, "start_ticks": 1000 + SUP},
                "child": {"pid": CHILD, "start_ticks": 1000 + CHILD}, **env.identity_patch}

    def provider():
        env.forbidden_open.append(len(env.fds))
        return forbidden()
    service.register(mod.StopTarget(env.cid, "worker", "terminal", None, identity=identity, forbidden=provider))
    setup(env)
    result = service.stop("worker", env.cid)
    return types.SimpleNamespace(result=result, journal=journal, env=env, cid=env.cid, service=service,
                                 marker_after=service.stop_marker(env.cid))


def normal(outcome):
    """Everything an observer can compare, with the random command id and the timestamps taken out."""
    cid = outcome.cid
    def clean(value):
        value = strip(value)
        return str(value).replace(cid, "CID") if not isinstance(value, str) else value.replace(cid, "CID")
    return {"result": clean(outcome.result), "journal": clean(outcome.journal),
            "trace": [tuple(str(part).replace(cid, "CID") for part in event) for event in outcome.env.trace],
            "forbidden_open": outcome.env.forbidden_open, "open_fds_left": dict(outcome.env.fds),
            "marker_after": outcome.marker_after}


def signal_rows(outcome):
    """(signal, stage, auxiliary, target pids, failed) per signal entry of the result."""
    return [(row["signal"], row["stage"], row["auxiliary"], [item["pid"] for item in row["targets"]],
             [(item["pid"], item["reason"]) for item in row["failed"]]) for row in outcome.result.get("signals", [])]


def sent_sequence(outcome):
    return [(event[1], event[2]) for event in outcome.env.trace if event[0] == "send"]


# ----------------------------------------------------------------------------------------------------------------
# scenarios (setup functions over the synthetic table)
# ----------------------------------------------------------------------------------------------------------------
def s_ignore_all(env):
    pass


def s_die_on_int(env):
    env.dies_on = {CHILD: {INT}, A: {INT}, B: {INT}}
    env.end_when_gone = {CHILD, A, B}


def s_owner_ends_after_term(env):
    env.dies_on = {CHILD: {TERM}, A: {TERM}, B: {TERM}}
    env.end_when_gone = {CHILD, A, B}


def s_kill_needed(env):
    env.dies_on = {CHILD: {KILL}, A: {KILL}, B: {KILL}}
    env.end_when_gone = {CHILD, A, B}


def s_stopped_member_continued_then_dies_on_int(env):
    env.table[A] = replace(env.table[A], state="T")
    env.dies_on = {CHILD: {INT}, A: {INT}, B: {INT}}
    env.end_when_gone = {CHILD, A, B}


def s_stopped_member_needs_kill(env):
    env.table[A] = replace(env.table[A], state="T")
    env.restop = {A}  # something stops it again after every SIGCONT
    env.dies_on = {A: {KILL}}
    env.end_when_gone = {A}


def s_member_exits_between_pin_and_send(env):
    env.exited_true_from = {A: 2}  # the pin reads "alive" once; the verification before the send reads "exited"


def s_every_member_exits_between_pin_and_send(env):
    env.exited_true_from = {CHILD: 2, A: 2, B: 2}


def s_identity_changes_before_send(env):
    def change(table):
        table.table[A] = replace(table.table[A], start_ticks=424242)
    env.read_hooks = {(A, 5): change}  # scan, pin (2 reads), verify, immediately-before-send read


def s_one_session_changes_after_scan(env):
    env.boot_hooks = [lambda table: table.table.__setitem__(A, replace(table.table[A], session=777))]
    env.dies_on = {CHILD: {KILL}, B: {KILL}}


def s_all_sessions_change_after_scan(env):
    def move(table):
        for pid in (CHILD, A, B):
            table.table[pid] = replace(table.table[pid], session=777)
    env.boot_hooks = [move]


def s_one_uid_changes_after_scan(env):
    env.boot_hooks = [lambda table: table.table.__setitem__(A, replace(table.table[A], uid=UID + 1))]


def s_one_member_becomes_a_zombie_after_scan(env):
    env.boot_hooks = [lambda table: table.table.__setitem__(A, replace(table.table[A], state="Z"))]
    env.dies_on = {CHILD: {KILL}, B: {KILL}}


def s_every_member_is_stale_at_the_pin_of_the_second_stage_only(env):
    def move(table):
        for pid in (CHILD, A, B):
            table.table[pid] = replace(table.table[pid], session=777)

    def back(table):
        for pid in (CHILD, A, B):
            table.table[pid] = replace(table.table[pid], session=SESSION)
    env.boot_nth = {4: move}  # the first pin of the SIGTERM stage (three pins per stage)
    env.list_nth = {3: back}  # the SIGKILL stage's scan sees them in the session again


def s_one_member_is_stale_at_one_pin_only(env):
    def move(table):
        table.table[A] = replace(table.table[A], session=777)

    def back(table):
        table.table[A] = replace(table.table[A], session=SESSION)
    env.read_hooks = {(A, 3): move, (A, 4): back}  # the pin's second read; the next stage's scan


def s_pidfd_unavailable_for_one_and_permission_denied_for_the_others(env):
    env.open_error = lambda pid: OSError(errno.ENOMEM, "no memory") if pid == A else None
    env.send_error = lambda pid, signum: PermissionError(errno.EPERM, "denied")


def s_main_process_identity_is_another_process(env):
    env.identity_patch = {"child": {"pid": CHILD, "start_ticks": 5}}


def s_permission_denied_for_all(env):
    env.send_error = lambda pid, signum: PermissionError(errno.EPERM, "denied")


def s_permission_denied_for_one(env):
    env.send_error = lambda pid, signum: PermissionError(errno.EPERM, "denied") if pid == A else None
    env.dies_on = {CHILD: {KILL}, B: {KILL}}
    env.end_when_gone = {CHILD, B}


def s_permission_denied_for_all_from_sigterm_on(env):
    env.send_error = lambda pid, signum: PermissionError(errno.EPERM, "denied") if signum != INT else None


def s_pidfd_unavailable_for_all(env):
    env.open_error = lambda pid: OSError(errno.EMFILE, "too many")


def s_pidfd_unavailable_for_all_from_sigterm_on(env):
    env.open_error = lambda pid: OSError(errno.EMFILE, "too many") if env.sent_total else None


def s_pidfd_unavailable_for_one(env):
    env.open_error = lambda pid: OSError(errno.ENOMEM, "no memory") if pid == B else None


def s_process_gone_at_send_for_one(env):
    env.send_error = lambda pid, signum: ProcessLookupError(errno.ESRCH, "gone") if pid == A else None


def s_owner_ends_before_the_second_stage(env):
    def hook(call):
        if call == 2:  # the SIGTERM stage re-reads the identity after its check that the owner is still there
            env.service.command_ended(env.cid, {"status": "exited", "exit_code": 143, "signal": None})
    env.identity_hook = hook


def s_all_leave_the_session_on_sigint(env):
    def leave(pid, signum):
        if signum == INT:
            for member in (CHILD, A, B):
                if member in env.table:
                    env.table[member] = replace(env.table[member], session=777)
    env.on_signal = leave


def s_one_member_already_left_the_session(env):
    env.table[A] = replace(env.table[A], session=777)
    env.dies_on = {CHILD: {KILL}, B: {KILL}}
    env.end_when_gone = {CHILD, B}


def s_identity_change_then_second_stage_skips_the_member(env):
    def change(table):
        table.table[A] = replace(table.table[A], start_ticks=424242)
    env.read_hooks = {(A, 5): change}
    env.dies_on = {CHILD: {TERM}, B: {TERM}}
    env.end_when_gone = {CHILD, B}


SCENARIOS = {
    "ignore_all": s_ignore_all,
    "die_on_int": s_die_on_int,
    "owner_ends_after_term": s_owner_ends_after_term,
    "kill_needed": s_kill_needed,
    "stopped_member_continued_then_dies_on_int": s_stopped_member_continued_then_dies_on_int,
    "stopped_member_needs_kill": s_stopped_member_needs_kill,
    "member_exits_between_pin_and_send": s_member_exits_between_pin_and_send,
    "every_member_exits_between_pin_and_send": s_every_member_exits_between_pin_and_send,
    "identity_changes_before_send": s_identity_changes_before_send,
    "one_session_changes_after_scan": s_one_session_changes_after_scan,
    "all_sessions_change_after_scan": s_all_sessions_change_after_scan,
    "one_uid_changes_after_scan": s_one_uid_changes_after_scan,
    "one_member_becomes_a_zombie_after_scan": s_one_member_becomes_a_zombie_after_scan,
    "every_member_is_stale_at_the_pin_of_the_second_stage_only": s_every_member_is_stale_at_the_pin_of_the_second_stage_only,
    "one_member_is_stale_at_one_pin_only": s_one_member_is_stale_at_one_pin_only,
    "pidfd_unavailable_for_one_and_permission_denied_for_the_others": s_pidfd_unavailable_for_one_and_permission_denied_for_the_others,
    "main_process_identity_is_another_process": s_main_process_identity_is_another_process,
    "permission_denied_for_all": s_permission_denied_for_all,
    "permission_denied_for_one": s_permission_denied_for_one,
    "permission_denied_for_all_from_sigterm_on": s_permission_denied_for_all_from_sigterm_on,
    "pidfd_unavailable_for_all": s_pidfd_unavailable_for_all,
    "pidfd_unavailable_for_all_from_sigterm_on": s_pidfd_unavailable_for_all_from_sigterm_on,
    "pidfd_unavailable_for_one": s_pidfd_unavailable_for_one,
    "process_gone_at_send_for_one": s_process_gone_at_send_for_one,
    "owner_ends_before_the_second_stage": s_owner_ends_before_the_second_stage,
    "all_leave_the_session_on_sigint": s_all_leave_the_session_on_sigint,
    "one_member_already_left_the_session": s_one_member_already_left_the_session,
    "identity_change_then_second_stage_skips_the_member": s_identity_change_then_second_stage_skips_the_member,
}


# ----------------------------------------------------------------------------------------------------------------
# 89cbc08: the split is a pure refactor
# ----------------------------------------------------------------------------------------------------------------
@needs_baseline
class SplitMatchesThePreRefactorStageTests(unittest.TestCase):
    """Every scenario: identical result, journal, signal order, markers, pin lifetime, environment trace."""

    def test_every_scenario_is_identical_before_and_after_the_split(self):
        for name, setup in SCENARIOS.items():
            with self.subTest(scenario=name):
                before = normal(run_synthetic(BASELINE, setup))
                after = normal(run_synthetic(current, setup))
                for key in before:
                    self.assertEqual(after[key], before[key], f"{name}: {key} differs from {BASELINE_REV}")

    def test_the_comparison_can_tell_scenarios_apart(self):
        # guard against a vacuous comparison: different scenarios give different traces
        traces = {name: str(normal(run_synthetic(current, setup))["trace"]) for name, setup in SCENARIOS.items()}
        self.assertGreater(len(set(traces.values())), len(SCENARIOS) - 4, "the scenarios hardly differ")

    def test_repeated_runs_of_one_scenario_are_stable(self):
        # the trace is the instrument: it must itself be deterministic (otherwise a difference would be noise)
        first = normal(run_synthetic(current, s_stopped_member_needs_kill))
        second = normal(run_synthetic(current, s_stopped_member_needs_kill))
        self.assertEqual(first, second)


class StageContractTests(unittest.TestCase):
    """The same behaviours with expected values derived from the contract (no baseline needed)."""

    def run_it(self, setup, **kwargs):
        return run_synthetic(current, setup, **kwargs)

    def assert_pins_all_released(self, outcome):
        self.assertEqual(outcome.env.fds, {}, "a pidfd is still open")
        opened, closed = Counter(), Counter()
        for event in outcome.env.trace:
            if event[0] in ("open", "close"):
                (opened if event[0] == "open" else closed)[event[1]] += 1
        self.assertEqual(opened, closed, "every pidfd opened was closed")

    def assert_pins_not_held_while_waiting(self, outcome):
        self.assertTrue(set(outcome.env.forbidden_open) <= {0}, outcome.env.forbidden_open)

    def test_members_that_ignore_everything_get_sigint_then_sigterm_then_sigkill_each_with_fresh_pins(self):
        out = self.run_it(s_ignore_all)
        self.assertEqual((out.result["status"], out.result["reason"]), ("unknown", "still_alive_after_sigkill"))
        self.assertEqual(signal_rows(out), [("SIGINT", "sigint", False, [CHILD, A, B], []),
                                            ("SIGTERM", "sigterm", False, [CHILD, A, B], []),
                                            ("SIGKILL", "sigkill", False, [CHILD, A, B], [])])
        self.assertEqual(sent_sequence(out), [(pid, number) for number in (INT, TERM, KILL) for pid in (CHILD, A, B)])
        opens = [event[1] for event in out.env.trace if event[0] == "open"]
        self.assertEqual(opens, [CHILD, A, B] * 3, "each stage pins the members again")
        # the pins of a stage are closed before the next stage opens its own: never more than three open at a time
        for event in out.env.trace:
            if event[0] == "send":
                self.assertEqual(sorted(event[4]), [CHILD, A, B], "exactly the stage's pins are open when it signals")
        self.assert_pins_all_released(out)
        self.assert_pins_not_held_while_waiting(out)
        self.assertEqual(dict(out.marker_after), {"stopped_by": "worker", "stop_signal": "SIGKILL"})

    def test_the_stop_marker_is_attached_right_before_the_first_signal_leaves(self):
        out = self.run_it(s_ignore_all)
        sends = [event for event in out.env.trace if event[0] == "send"]
        self.assertEqual({event[3] for event in sends[:3]}, {(("stop_signal", "SIGINT"), ("stopped_by", "worker"))},
                         "the marker names the signal that is leaving, from the very first send")
        self.assertEqual({event[3] for event in sends[3:6]}, {(("stop_signal", "SIGTERM"), ("stopped_by", "worker"))})
        self.assertEqual({event[3] for event in sends[6:]}, {(("stop_signal", "SIGKILL"), ("stopped_by", "worker"))})

    def test_no_marker_before_anything_is_attempted_and_none_after_a_refused_stop(self):
        # nothing signalled (every member stale): the marker is empty at the end
        out = self.run_it(s_all_sessions_change_after_scan)
        self.assertEqual(out.marker_after, {})
        self.assertEqual([e for e in out.env.trace if e[0] == "send"], [])

    def test_members_that_die_on_sigint_end_the_stop_after_the_first_stage(self):
        out = self.run_it(s_die_on_int)
        self.assertEqual(out.result["status"], "exited", out.result)
        self.assertEqual(signal_rows(out), [("SIGINT", "sigint", False, [CHILD, A, B], [])])
        self.assert_pins_all_released(out)

    def test_the_owner_seeing_the_end_after_sigterm_stops_the_ladder_before_sigkill(self):
        out = self.run_it(s_owner_ends_after_term)
        self.assertEqual(out.result["status"], "exited", out.result)
        self.assertEqual([row[0] for row in signal_rows(out)], ["SIGINT", "SIGTERM"])
        self.assertNotIn(KILL, [number for _pid, number in sent_sequence(out)])

    def test_only_sigkill_helps_so_all_three_stages_run(self):
        out = self.run_it(s_kill_needed)
        self.assertEqual(out.result["status"], "exited", out.result)
        self.assertEqual([row[0] for row in signal_rows(out)], ["SIGINT", "SIGTERM", "SIGKILL"])

    def test_a_stopped_member_that_was_reached_gets_sigcont_right_after_sigint_and_it_dies_of_the_pending_signal(self):
        out = self.run_it(s_stopped_member_continued_then_dies_on_int)
        rows = signal_rows(out)
        self.assertEqual(rows[0], ("SIGINT", "sigint", False, [CHILD, A, B], []))
        self.assertEqual(rows[1], ("SIGCONT", "sigint", True, [A], []), "only the stopped member is continued")
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(out.result["status"], "exited", out.result)
        self.assertEqual(sent_sequence(out), [(CHILD, INT), (A, INT), (B, INT), (A, CONT)])

    def test_sigcont_follows_sigint_and_sigterm_for_a_member_that_is_stopped_again_but_never_follows_sigkill(self):
        out = self.run_it(s_stopped_member_needs_kill)
        self.assertEqual([(row[0], row[1], row[2]) for row in signal_rows(out)],
                         [("SIGINT", "sigint", False), ("SIGCONT", "sigint", True),
                          ("SIGTERM", "sigterm", False), ("SIGCONT", "sigterm", True),
                          ("SIGKILL", "sigkill", False)])
        self.assertNotIn((A, CONT), sent_sequence(out)[sent_sequence(out).index((A, KILL)):])
        # the auxiliary SIGCONT does not change who the stop was by and what stopped it
        self.assertEqual(dict(out.marker_after), {"stopped_by": "worker", "stop_signal": "SIGKILL"})
        self.assertEqual(out.result["status"], "exited", out.result)

    def test_a_member_that_exits_between_pin_and_send_is_ended_not_a_failure_and_the_others_are_signalled(self):
        out = self.run_it(s_member_exits_between_pin_and_send)
        first = signal_rows(out)[0]
        self.assertEqual(first[:4], ("SIGINT", "sigint", False, [CHILD, B]), first)
        self.assertEqual(first[4], [(A, "ended")], "recorded as ended")
        self.assertEqual(out.result["status"], "unknown")
        self.assertNotIn((A, INT), sent_sequence(out), "nothing was sent to the member that had exited")
        self.assertEqual(out.marker_after.get("stopped_by"), "worker", "the others were signalled: the stop is marked")
        self.assert_pins_all_released(out)

    def test_when_every_member_exited_before_the_send_the_stop_waits_for_the_owner_instead_of_failing(self):
        out = self.run_it(s_every_member_exits_between_pin_and_send)
        self.assertEqual((out.result["status"], out.result["reason"]), ("unknown", "still_alive_after_sigkill"), out.result)
        self.assertEqual(sent_sequence(out), [])
        records = [(r["stage"], r["targets"], sorted(item["reason"] for item in r["failed"]))
                   for r in out.journal if r["type"] == "command_stop_signal"]
        self.assertEqual(records, [(stage, [], ["ended"] * 3) for stage in ("sigint", "sigterm", "sigkill")])
        self.assertEqual(out.result["signals"], [], "no signal reached anything")
        self.assertEqual(out.marker_after, {})
        self.assertEqual(Counter(event[1] for event in out.env.trace if event[0] == "exited?" and not event[2]).get(A), 3,
                         "every stage pinned them again")
        self.assert_pins_all_released(out)

    def test_an_identity_that_changes_between_verification_and_send_is_identity_changed_and_skipped_in_every_later_stage(self):
        out = self.run_it(s_identity_changes_before_send)
        rows = signal_rows(out)
        self.assertEqual(rows[0][:4], ("SIGINT", "sigint", False, [CHILD, B]))
        self.assertEqual(rows[0][4], [(A, "identity_changed")])
        for row in rows[1:]:  # later stages never pin it again: ``identity_mismatch`` from the episode's memory
            self.assertEqual((row[3], row[4]), ([CHILD, B], [(A, "identity_mismatch")]), row)
        self.assertEqual([pid for pid, _n in sent_sequence(out) if pid == A], [])
        self.assertEqual(Counter(event[1] for event in out.env.trace if event[0] == "open")[A], 1,
                         "pinned once (the first stage); never again")

    def test_the_skipped_member_does_not_stop_the_ladder_when_the_others_end(self):
        out = self.run_it(s_identity_change_then_second_stage_skips_the_member)
        self.assertEqual(out.result["status"], "exited", out.result)
        self.assertEqual([row[0] for row in signal_rows(out)], ["SIGINT", "SIGTERM"])

    def test_a_member_that_changed_session_since_the_scan_is_not_signalled_and_recorded_as_identity_mismatch(self):
        out = self.run_it(s_one_session_changes_after_scan)
        rows = signal_rows(out)
        self.assertEqual(rows[0][:5], ("SIGINT", "sigint", False, [CHILD, B], [(A, "identity_mismatch")]))
        self.assertEqual([pid for pid, _n in sent_sequence(out) if pid == A], [])

    def test_when_every_member_changed_session_the_stop_is_stale_with_no_signal_no_pin_left_and_a_refusal_record(self):
        out = self.run_it(s_all_sessions_change_after_scan)
        self.assertEqual((out.result["status"], out.result["reason"], out.result["signals"]),
                         ("stale", "identity_mismatch", []))
        self.assertEqual(sent_sequence(out), [])
        self.assertEqual([r["status"] for r in out.journal if r["type"] == "command_stop_refused"], ["stale"])
        self.assert_pins_all_released(out)

    def test_a_member_refused_at_the_pin_is_remembered_and_not_pinned_again_even_when_the_scan_tracks_it_again(self):
        out = self.run_it(s_one_member_is_stale_at_one_pin_only)
        rows = signal_rows(out)
        self.assertEqual(rows[0][3:], ([CHILD, B], [(A, "identity_mismatch")]))
        for row in rows[1:]:
            self.assertEqual(row[3:], ([CHILD, B], [(A, "identity_mismatch")]), row)
        self.assertEqual(Counter(event[1] for event in out.env.trace if event[0] == "open")[A], 1, "pinned only once")
        self.assertNotIn(A, [pid for pid, _n in sent_sequence(out)])

    def test_failures_of_a_later_stage_that_signalled_nothing_are_still_that_stages_record_and_the_ladder_goes_on(self):
        out = self.run_it(s_every_member_is_stale_at_the_pin_of_the_second_stage_only)
        self.assertEqual((out.result["status"], out.result["reason"]), ("unknown", "still_alive_after_sigkill"))
        records = [(r["stage"], r["signal"], r["targets"], [(item["pid"], item["reason"]) for item in r["failed"]])
                   for r in out.journal if r["type"] == "command_stop_signal"]
        self.assertEqual(records, [("sigint", "SIGINT", [{"pid": pid, "start_ticks": 1000 + pid} for pid in (CHILD, A, B)], []),
                                   ("sigterm", "SIGTERM", [], [(CHILD, "identity_mismatch"), (A, "identity_mismatch"),
                                                               (B, "identity_mismatch")]),
                                   ("sigkill", "SIGKILL", [], [(CHILD, "identity_mismatch"), (A, "identity_mismatch"),
                                                               (B, "identity_mismatch")])])
        self.assertEqual([row[0] for row in signal_rows(out)], ["SIGINT"])

    def test_mixed_failures_with_nothing_sent_are_not_permission_denied_unless_every_one_is(self):
        out = self.run_it(s_pidfd_unavailable_for_one_and_permission_denied_for_the_others)
        self.assertEqual((out.result["status"], out.result["reason"]), ("unknown", "pidfd_unavailable"), out.result)
        self.assertEqual(len([e for e in out.env.trace if e[0] == "send_error"]), 2, "stopped after the first stage")

    def test_the_main_process_reading_as_another_process_is_stale_before_any_pin(self):
        out = self.run_it(s_main_process_identity_is_another_process)
        self.assertEqual(out.result["status"], "stale", out.result)
        self.assertEqual([e for e in out.env.trace if e[0] in ("open", "send")], [])

    def test_a_member_with_another_uid_is_refused_as_different_uid_and_never_signalled(self):
        out = self.run_it(s_one_uid_changes_after_scan)
        rows = signal_rows(out)
        self.assertEqual(rows[0][3], [CHILD, B])
        self.assertEqual(rows[0][4], [(A, "different_uid")])
        self.assertNotIn(A, [pid for pid, _n in sent_sequence(out)])

    def test_a_member_that_became_a_zombie_after_the_scan_is_ended_and_not_a_failure(self):
        out = self.run_it(s_one_member_becomes_a_zombie_after_scan)
        self.assertEqual(signal_rows(out)[0], ("SIGINT", "sigint", False, [CHILD, B], []))
        self.assertNotIn(A, [pid for pid, _n in sent_sequence(out)])

    def test_permission_denied_everywhere_from_the_first_stage_ends_unknown_permission_denied_at_once(self):
        out = self.run_it(s_permission_denied_for_all)
        self.assertEqual((out.result["status"], out.result["reason"]), ("unknown", "permission_denied"))
        self.assertEqual(len([e for e in out.env.trace if e[0] == "send_error"]), 3, "only the first stage was tried")
        self.assertEqual(out.marker_after, {}, "no signal reached anything: the command's end is not the stop's")
        self.assertEqual([r for r in out.journal if r["type"] == "command_stop_signal"][0]["failed"],
                         [{"pid": pid, "reason": "permission_denied"} for pid in (CHILD, A, B)])
        self.assert_pins_all_released(out)

    def test_permission_denied_for_one_member_does_not_end_the_stop(self):
        out = self.run_it(s_permission_denied_for_one)
        self.assertEqual(out.result["status"], "exited", out.result)
        self.assertEqual(signal_rows(out)[0][3:], ([CHILD, B], [(A, "permission_denied")]))

    def test_permission_denied_everywhere_in_a_later_stage_is_unknown_permission_denied_not_a_still_alive_unknown(self):
        out = self.run_it(s_permission_denied_for_all_from_sigterm_on)
        self.assertEqual((out.result["status"], out.result["reason"]), ("unknown", "permission_denied"))
        self.assertEqual([row[0] for row in signal_rows(out)], ["SIGINT"])
        self.assertEqual(len([e for e in out.env.trace if e[0] == "send_error"]), 3)
        self.assertNotIn("SIGKILL", [r["signal"] for r in out.journal if r["type"] == "command_stop_signal"])

    def test_pidfd_unavailable_for_every_member_at_the_first_stage_is_unknown_pidfd_unavailable(self):
        out = self.run_it(s_pidfd_unavailable_for_all)
        self.assertEqual((out.result["status"], out.result["reason"]), ("unknown", "pidfd_unavailable"))
        self.assertEqual(sent_sequence(out), [])
        self.assertEqual(out.result["signals"], [])
        records = [r for r in out.journal if r["type"] == "command_stop_signal"]
        self.assertEqual([[item["reason"] for item in r["failed"]] for r in records],
                         [["pidfd_unavailable:EMFILE"] * 3], "the failures are the stage's record")

    def test_pidfd_unavailable_after_a_signal_went_out_does_not_end_the_ladder(self):
        out = self.run_it(s_pidfd_unavailable_for_all_from_sigterm_on)
        self.assertEqual((out.result["status"], out.result["reason"]), ("unknown", "still_alive_after_sigkill"))
        self.assertEqual([row[0] for row in signal_rows(out)], ["SIGINT"])
        stages = [r["stage"] for r in out.journal if r["type"] == "command_stop_signal"]
        self.assertEqual(stages, ["sigint", "sigterm", "sigkill"], "every later stage is still recorded")

    def test_pidfd_unavailable_for_one_member_leaves_it_unsignalled_and_signals_the_others(self):
        out = self.run_it(s_pidfd_unavailable_for_one)
        self.assertEqual(signal_rows(out)[0][3:], ([CHILD, A], [(B, "pidfd_unavailable:ENOMEM")]))

    def test_a_process_that_vanished_at_the_send_is_ended_in_the_failed_list_and_not_signalled(self):
        out = self.run_it(s_process_gone_at_send_for_one)
        self.assertEqual(signal_rows(out)[0][3:], ([CHILD, B], [(A, "ended")]))

    def test_the_owner_ending_while_the_second_stage_reads_the_identity_ends_exited_after_that_stage_with_no_sigkill(self):
        out = self.run_it(s_owner_ends_before_the_second_stage)
        self.assertEqual(out.result["status"], "exited", out.result)
        self.assertEqual([row[0] for row in signal_rows(out)], ["SIGINT", "SIGTERM"],
                         "the check that the owner is still there precedes the identity read")

    def test_members_that_all_left_the_session_end_the_stop_unknown_left_session_descendants(self):
        out = self.run_it(s_all_leave_the_session_on_sigint)
        self.assertEqual((out.result["status"], out.result["reason"]), ("unknown", "left_session_descendants"))
        self.assertEqual([row[0] for row in signal_rows(out)], ["SIGINT"], "nothing more is signalled")
        self.assertEqual(Counter(event[1] for event in out.env.trace if event[0] == "open"),
                         Counter({CHILD: 1, A: 1, B: 1}))

    def test_a_member_that_was_never_in_the_session_is_not_pinned_or_signalled_but_reported(self):
        out = self.run_it(s_one_member_already_left_the_session)
        self.assertNotIn(A, [e[1] for e in out.env.trace if e[0] in ("open", "send")])
        target = [r for r in out.journal if r["type"] == "command_stop_target"][0]
        self.assertEqual([item["pid"] for item in target["left_session"]], [A])
        self.assertEqual(out.result["status"], "exited", out.result)


# ----------------------------------------------------------------------------------------------------------------
# bb0f810: the forbidden fallback
# ----------------------------------------------------------------------------------------------------------------
PROTECTED, OTHER = 945, 946


class Provider:
    """An owner's ``forbidden`` that follows a plan: a set is returned, an exception raised, a callable called."""

    def __init__(self, *plan):
        self.plan, self.calls = list(plan), 0

    def __call__(self):
        item = self.plan[min(self.calls, len(self.plan) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        return item


def forbidden_world(mod):
    stats = world(mod)
    extra = [mod.ProcStat(pid, "S", CHILD, CHILD, SESSION, 1000 + pid, UID) for pid in (PROTECTED, OTHER)]
    return [stat for stat in stats if stat.pid != B] + extra  # CHILD, A, PROTECTED, OTHER


BROKEN = RuntimeError("the owner's provider is gone")


def partial_then_fail():
    yield OTHER
    raise BROKEN


def not_iterable():
    return None


def alive(result_part):
    return [item["pid"] for item in result_part["residual"]["alive"]]


# the calls the provider sees in a stop that never ends: 1 identity wait, 2 SIGTERM scan, 3 SIGKILL scan,
# 4 the residual of the unknown result, 5 the episode's finish
FINISH = 5


class ForbiddenFallbackTests(unittest.TestCase):
    def stop(self, provider, *, then_end=True):
        out = run_synthetic(current, lambda env: None, stats=forbidden_world(current), forbidden=provider)
        correction = None
        if then_end:
            out.service.command_ended(out.cid, {"status": "exited", "exit_code": 137, "signal": 9})
            correction = out.service.stop("worker", out.cid)
        return out, correction

    def never_signalled(self, out, *pids):
        for pid in pids:
            self.assertNotIn(pid, [target for target, _number in sent_sequence(out) if target == pid], pid)
            self.assertNotIn(pid, [event[1] for event in out.env.trace if event[0] == "open"], f"{pid} was pinned")

    def test_the_provider_call_sequence_is_what_this_file_assumes(self):
        provider = Provider(frozenset({PROTECTED}))
        out, _ = self.stop(provider, then_end=False)
        self.assertEqual(provider.calls, FINISH, "scan x3, residual, finish: the plans below index these calls")
        self.assertEqual(out.result["reason"], "still_alive_after_sigkill")

    def test_a_provider_that_fails_at_the_finish_keeps_the_last_set_it_gave_out_of_the_residual(self):
        provider = Provider(frozenset({PROTECTED}), frozenset({PROTECTED}), frozenset({PROTECTED}),
                            frozenset({PROTECTED}), BROKEN)
        out, corrected = self.stop(provider)
        self.assertEqual(provider.calls, FINISH, "the failing call was the one at the finish")
        self.assertEqual(corrected["status"], "exited", corrected)
        self.assertTrue(corrected["residual"]["checked"])
        self.assertIn(CHILD, alive(corrected), "the late scan itself ran")
        self.assertNotIn(PROTECTED, alive(corrected), "a forbidden pid is never a tracked residual")
        self.never_signalled(out, PROTECTED)

    def test_the_kept_set_is_the_last_one_read_not_the_first_and_not_the_union(self):
        # the owner forbids PROTECTED first; from the second scan on it forbids OTHER only
        provider = Provider(frozenset({PROTECTED}), frozenset({OTHER}), frozenset({OTHER}), frozenset({OTHER}), BROKEN)
        out, corrected = self.stop(provider)
        residual = alive(corrected)
        self.assertNotIn(OTHER, residual, "the last set is kept")
        self.assertIn(PROTECTED, residual, "a pid the owner stopped forbidding is a residual member again")
        sent = sent_sequence(out)
        self.assertIn((OTHER, INT), sent, "OTHER was not forbidden in the first stage")
        self.assertNotIn((OTHER, TERM), sent)
        self.assertNotIn((PROTECTED, INT), sent, "PROTECTED was forbidden in the first stage")
        self.assertIn((PROTECTED, TERM), sent, "and the owner's later set is respected as it is")

    def test_the_set_grown_after_the_first_stage_is_the_one_kept(self):
        provider = Provider(frozenset({PROTECTED}), frozenset({PROTECTED, OTHER}), frozenset({PROTECTED, OTHER}),
                            frozenset({PROTECTED, OTHER}), BROKEN)
        out, corrected = self.stop(provider)
        self.assertEqual(alive(corrected), [CHILD, A], corrected["residual"])
        self.assertNotIn((OTHER, TERM), sent_sequence(out))
        self.assertNotIn((PROTECTED, INT), sent_sequence(out))

    def test_a_provider_that_raises_part_way_through_its_iteration_does_not_replace_the_kept_set(self):
        provider = Provider(frozenset({PROTECTED}), frozenset({PROTECTED}), frozenset({PROTECTED}),
                            frozenset({PROTECTED}), partial_then_fail)
        out, corrected = self.stop(provider)
        residual = alive(corrected)
        self.assertNotIn(PROTECTED, residual, "the earlier complete set is kept")
        self.assertIn(OTHER, residual, "the half-read set (OTHER) was not adopted")

    def test_a_provider_that_returns_something_unusable_at_the_finish_keeps_the_last_set_too(self):
        provider = Provider(frozenset({PROTECTED}), frozenset({PROTECTED}), frozenset({PROTECTED}),
                            frozenset({PROTECTED}), not_iterable)
        out, corrected = self.stop(provider)
        self.assertNotIn(PROTECTED, alive(corrected))
        self.assertIn(CHILD, alive(corrected))

    def test_a_set_read_successfully_at_the_finish_is_the_one_that_is_kept(self):
        provider = Provider(frozenset({PROTECTED}), frozenset({PROTECTED}), frozenset({PROTECTED}),
                            frozenset({PROTECTED}), frozenset())
        out, corrected = self.stop(provider)
        self.assertIn(PROTECTED, alive(corrected), "the owner's newest answer wins over an older one")

    def test_the_provider_is_not_asked_again_once_the_episode_has_finished(self):
        provider = Provider(frozenset({PROTECTED}), frozenset({PROTECTED}), frozenset({PROTECTED}),
                            frozenset({PROTECTED}), BROKEN)
        out, corrected = self.stop(provider)
        repeat = out.service.stop("worker", out.cid)
        self.assertEqual(provider.calls, FINISH)
        self.assertTrue(repeat.get("repeat"), repeat)
        self.assertEqual(alive(repeat), alive(corrected))

    def test_a_provider_that_fails_between_stages_fails_closed_and_the_late_correction_uses_the_last_set(self):
        provider = Provider(frozenset({PROTECTED}), BROKEN)  # fails at the SIGTERM stage's scan, and at every call after
        out, corrected = self.stop(provider, then_end=False)
        self.assertEqual(out.result["status"], "unknown")
        self.assertTrue(out.result["reason"].startswith("episode_error:RuntimeError"), out.result["reason"])
        self.assertEqual([row[0] for row in signal_rows(out)], ["SIGINT"], "no further signal after the failure")
        self.assertEqual([number for _pid, number in sent_sequence(out)], [INT] * len(sent_sequence(out)))
        self.assertFalse(out.result["residual"]["checked"], "failed closed: not claimed to be checked")
        self.assertEqual(out.env.fds, {}, "no pin outlives the failed stage")
        out.service.command_ended(out.cid, {"status": "exited", "exit_code": 137, "signal": 9})
        corrected = out.service.stop("worker", out.cid)
        self.assertEqual(corrected["status"], "exited", corrected)
        self.assertTrue(corrected["residual"]["checked"])
        self.assertNotIn(PROTECTED, alive(corrected), "the last set that was read stays forbidden")
        self.never_signalled(out, PROTECTED)

    def test_a_provider_that_fails_before_the_first_signal_is_unknown_with_nothing_pinned_signalled_or_marked(self):
        for failure in (BROKEN, not_iterable):
            with self.subTest(failure=getattr(failure, "__name__", type(failure).__name__)):
                provider = Provider(failure)
                out, _ = self.stop(provider, then_end=False)
                self.assertEqual(out.result["status"], "unknown", out.result)
                self.assertTrue(out.result["reason"].startswith("episode_error:"), out.result["reason"])
                self.assertEqual(out.result["signals"], [])
                self.assertEqual(sent_sequence(out), [])
                self.assertEqual([e for e in out.env.trace if e[0] == "open"], [])
                self.assertEqual(out.marker_after, {})
                self.assertFalse(out.result["residual"]["checked"])
                self.assertEqual([r for r in out.journal if r["type"] == "command_stop_signal"], [])

    def test_the_failure_before_any_signal_leaves_no_kept_set_and_a_late_correction_still_signals_and_pins_nothing(self):
        provider = Provider(BROKEN)
        out, _ = self.stop(provider, then_end=False)
        out.service.command_ended(out.cid, {"status": "exited", "exit_code": 0, "signal": None})
        corrected = out.service.stop("worker", out.cid)
        self.assertEqual(corrected["status"], "exited", corrected)
        self.assertFalse(corrected["residual"]["checked"], "no identity was ever adopted: nothing to scan with")
        self.assertEqual(sent_sequence(out), [])
        self.assertEqual([e for e in out.env.trace if e[0] == "open"], [])
        self.assertEqual(corrected["signals"], [])

    def test_the_finish_record_is_written_whatever_the_provider_does_at_the_end(self):
        provider = Provider(frozenset({PROTECTED}), frozenset({PROTECTED}), frozenset({PROTECTED}),
                            frozenset({PROTECTED}), BROKEN)
        out, _ = self.stop(provider, then_end=False)
        self.assertEqual([r["status"] for r in out.journal if r["type"] == "command_stop_finished"], ["unknown"])
        self.assertEqual(out.service.surface(out.cid)["state"], "unknown")


# ----------------------------------------------------------------------------------------------------------------
# real processes
# ----------------------------------------------------------------------------------------------------------------
TREE = r'''
import os, signal, subprocess, sys, time
role, workdir, command_mode, sibling_mode = sys.argv[1:5]

def announce(name):
    with open(os.path.join(workdir, name + ".pid.tmp"), "w") as handle:
        handle.write(str(os.getpid()))
    os.rename(os.path.join(workdir, name + ".pid.tmp"), os.path.join(workdir, name + ".pid"))

def configure(mode):
    if mode == "stubborn":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif mode == "setsid-on-usr1":
        signal.signal(signal.SIGUSR1, lambda *_: os.setsid())
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

if role == "host":
    announce("host")
    child = subprocess.Popen([sys.executable, __file__, "supervisor", workdir, command_mode, sibling_mode])
    child.wait()
elif role == "supervisor":
    announce("supervisor")
    children = [subprocess.Popen([sys.executable, __file__, name, workdir, command_mode, sibling_mode])
                for name in ("command", "sibling")]
    while any(child.poll() is None for child in children):
        time.sleep(0.05)
else:
    configure(command_mode if role == "command" else sibling_mode)
    announce(role)
    while True:
        time.sleep(0.05)
'''


def kill_exact(pid, start_ticks):
    """SIGKILL the process only when it is still the one recorded (pidfd, then start ticks re-read through it)."""
    env = current.LinuxStopEnvironment()
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        stat = env.read_stat(pid)
        if stat is not None and stat.start_ticks == start_ticks:
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except OSError:
                pass
    finally:
        os.close(fd)


class RealTree:
    """host -> supervisor -> {command, sibling}, all in one new session; the command and the sibling run ``mode``."""

    def __init__(self, test, command_mode, sibling_mode):
        self.test, self.env = test, current.LinuxStopEnvironment()
        self.dir = tempfile.TemporaryDirectory(prefix="stage-split-")
        test.addCleanup(self.dir.cleanup)
        self.script = Path(self.dir.name) / "tree.py"
        self.script.write_text(TREE)
        self.recorded = []  # (pid, start ticks)
        test.addCleanup(self.cleanup)
        self.proc = subprocess.Popen([sys.executable, str(self.script), "host", self.dir.name, command_mode, sibling_mode],
                                     start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.recorded.append((self.proc.pid, self.ticks(self.proc.pid)))
        self.pids = {name: self.wait_pid(name) for name in ("host", "supervisor", "command", "sibling")}
        self.recorded = [(pid, self.ticks(pid)) for pid in self.pids.values()]

    def ticks(self, pid):
        stat = self.env.read_stat(pid)
        return None if stat is None else stat.start_ticks

    def wait_pid(self, name, timeout=30):
        path = Path(self.dir.name) / f"{name}.pid"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return int(path.read_text())
            time.sleep(0.02)
        raise AssertionError(f"{name} did not start")

    def cleanup(self):
        for pid, ticks in self.recorded:
            if ticks is not None:
                kill_exact(pid, ticks)
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=30)

    def identity(self):
        stat = self.env.read_stat
        return {"request_id": self.cid, "phase": "running", "uid": UID, "boot_id": BOOT_ID,
                "session": os.getsid(self.pids["host"]), "child_group": os.getpgid(self.pids["command"]),
                "parent": {"pid": self.pids["host"], "start_ticks": stat(self.pids["host"]).start_ticks},
                "supervisor": {"pid": self.pids["supervisor"], "start_ticks": stat(self.pids["supervisor"]).start_ticks},
                "child": {"pid": self.pids["command"], "start_ticks": stat(self.pids["command"]).start_ticks}}

    def state(self, pid):
        stat = self.env.read_stat(pid)
        return None if stat is None else stat.state

    def gone(self, pid):
        return self.state(pid) in (None, "Z", "X")

    def wait_for(self, predicate, what, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError(f"timed out waiting for {what}")


def make_hook_env(mod, tree):
    """The real environment of ``mod`` that records signals and runs the scenario's hooks."""

    class HookEnv(mod.LinuxStopEnvironment):
        def __init__(self):
            self.sent, self.fds, self.boot_once, self.exited_hook = [], {}, [], None
            self.exited_calls = Counter()

        def boot_id(self):
            hooks, self.boot_once = self.boot_once, []
            for hook in hooks:
                hook()
            return super().boot_id()

        def pidfd_open(self, pid):
            fd = super().pidfd_open(pid)
            self.fds[fd] = pid
            return fd

        def pidfd_exited(self, fd):
            self.exited_calls[fd] += 1
            if self.exited_hook:
                self.exited_hook(self.fds.get(fd), self.exited_calls[fd])
            return super().pidfd_exited(fd)

        def pidfd_send_signal(self, fd, signum):
            self.sent.append((self.fds[fd], int(signum)))
            super().pidfd_send_signal(fd, signum)

        def close(self, fd):
            self.fds.pop(fd, None)
            super().close(fd)
    return HookEnv()


def run_real(test, mod, command_mode, sibling_mode, *, prepare=None, owner_ends=True, forbidden=frozenset(),
             sigint_wait=0.6, tweak=None):
    """Start a fresh tree, stop it through ``mod`` and return what happened (the tree is cleaned up by the test)."""
    tree = RealTree(test, command_mode, sibling_mode)
    policy = mod.StopPolicy(identity_wait=3.0, sigint_wait=sigint_wait, sigterm_wait=0.6, sigkill_wait=1.5,
                            call_wait=30.0, poll_interval=0.01)
    env = make_hook_env(mod, tree)
    journal = []
    service = mod.CommandStopService(journal=journal.append, policy=policy, env=env)
    tree.cid = str(uuid4())
    service.register(mod.StopTarget(tree.cid, "worker", "terminal", None, identity=tree.identity,
                                    forbidden=lambda: frozenset(forbidden)))
    if prepare:
        prepare(tree, env)
    stop_poller = threading.Event()

    def watch():  # the owner: it reports the end once nothing tracked is left
        ident = tree.identity()
        while not stop_poller.is_set():
            if not mod.scan_tracked(ident, mod.LinuxStopEnvironment()).tracked:
                service.command_ended(tree.cid, {"status": "exited", "exit_code": 137, "signal": 9})
                return
            time.sleep(0.01)
    poller = threading.Thread(target=watch, daemon=True) if owner_ends else None
    if poller:
        poller.start()
    test.addCleanup(lambda: (stop_poller.set(), poller and poller.join(30)))
    if tweak:
        tweak(tree, env)
    result = service.stop("worker", tree.cid)
    return types.SimpleNamespace(tree=tree, env=env, result=result, journal=journal, service=service)


def real_summary(run):
    """The comparable outcome: statuses and signals by role (pids differ between runs)."""
    role = {pid: name for name, pid in run.tree.pids.items()}
    return {"status": run.result["status"], "reason": run.result.get("reason"),
            "signals": [(row["signal"], row["stage"], row["auxiliary"], sorted(role[item["pid"]] for item in row["targets"]),
                         sorted((role[item["pid"]], item["reason"]) for item in row["failed"]))
                        for row in run.result.get("signals", [])],
            "sent": [(role[pid], number) for pid, number in run.env.sent],
            "marker": run.service.stop_marker(run.tree.cid), "fds_left": dict(run.env.fds)}


class RealProcessTests(unittest.TestCase):
    MODULES = [("current", current)] + ([("baseline", BASELINE)] if BASELINE is not None else [])

    def each_module(self, scenario):
        outcomes = {}
        for name, mod in self.MODULES:
            with self.subTest(module=name):
                outcomes[name] = scenario(mod)
        if len(outcomes) == 2:
            self.assertEqual(outcomes["current"], outcomes["baseline"], "the split changed what real processes see")
        return outcomes

    def test_a_tree_that_ignores_sigint_and_sigterm_is_killed_after_the_full_ladder(self):
        def scenario(mod):
            run = run_real(self, mod, "stubborn", "stubborn")
            summary = real_summary(run)
            self.assertEqual(summary["status"], "exited", run.result)
            self.assertEqual([(s[0], s[1], s[2], s[3]) for s in summary["signals"]],
                             [("SIGINT", "sigint", False, ["command", "sibling"]),
                              ("SIGTERM", "sigterm", False, ["command", "sibling"]),
                              ("SIGKILL", "sigkill", False, ["command", "sibling"])])
            self.assertEqual(summary["marker"], {"stopped_by": "worker", "stop_signal": "SIGKILL"})
            run.tree.wait_for(lambda: run.tree.gone(run.tree.pids["command"]) and run.tree.gone(run.tree.pids["sibling"]),
                              "the killed members")
            self.assertEqual(summary["fds_left"], {})
            self.assertNotIn("supervisor", [role for role, _n in summary["sent"]])
            self.assertNotIn("host", [role for role, _n in summary["sent"]])
            return summary
        self.each_module(scenario)

    def test_a_cooperative_tree_ends_on_sigint_alone(self):
        def scenario(mod):
            run = run_real(self, mod, "default", "default")
            summary = real_summary(run)
            self.assertEqual(summary["status"], "exited", run.result)
            self.assertEqual([s[0] for s in summary["signals"]], ["SIGINT"])
            self.assertEqual(summary["sent"], [("command", 2), ("sibling", 2)])
            return summary
        self.each_module(scenario)

    def test_a_stopped_member_gets_sigcont_after_sigint_so_that_it_can_act_and_is_not_continued_after_sigkill(self):
        def scenario(mod):
            def prepare(tree, env):
                os.kill(tree.pids["command"], signal.SIGSTOP)
                tree.wait_for(lambda: tree.state(tree.pids["command"]) == "T", "the command to be stopped")
            run = run_real(self, mod, "default", "default", prepare=prepare)
            summary = real_summary(run)
            self.assertEqual(summary["status"], "exited", run.result)
            self.assertEqual([(s[0], s[2], s[3]) for s in summary["signals"]],
                             [("SIGINT", False, ["command", "sibling"]), ("SIGCONT", True, ["command"])])
            self.assertEqual(summary["sent"], [("command", 2), ("sibling", 2), ("command", int(signal.SIGCONT))])
            run.tree.wait_for(lambda: run.tree.gone(run.tree.pids["command"]), "the continued command to act on SIGINT")
            return summary
        self.each_module(scenario)

    def test_a_stopped_member_that_ignores_sigint_and_sigterm_is_continued_once_and_killed(self):
        def scenario(mod):
            def prepare(tree, env):
                os.kill(tree.pids["command"], signal.SIGSTOP)
                tree.wait_for(lambda: tree.state(tree.pids["command"]) == "T", "the command to be stopped")
            run = run_real(self, mod, "stubborn", "stubborn", prepare=prepare)
            summary = real_summary(run)
            self.assertEqual(summary["status"], "exited", run.result)
            # once continued it runs on: no further SIGCONT is due before the SIGKILL
            self.assertEqual([(s[0], s[1], s[2]) for s in summary["signals"]],
                             [("SIGINT", "sigint", False), ("SIGCONT", "sigint", True),
                              ("SIGTERM", "sigterm", False), ("SIGKILL", "sigkill", False)])
            return summary
        self.each_module(scenario)

    def test_a_member_that_exits_between_pin_and_send_is_ended_and_the_other_is_still_signalled(self):
        def scenario(mod):
            def tweak(tree, env):
                victim = tree.pids["sibling"]

                def exit_it(pid, call):
                    if pid == victim and call == 2 and not tree.gone(victim):  # the verification before the send
                        os.kill(victim, signal.SIGKILL)
                        tree.wait_for(lambda: tree.gone(victim), "the sibling to be gone")
                env.exited_hook = exit_it
            run = run_real(self, mod, "default", "default", tweak=tweak)
            summary = real_summary(run)
            first = summary["signals"][0]
            self.assertEqual(first[:4], ("SIGINT", "sigint", False, ["command"]), first)
            self.assertEqual(first[4], [("sibling", "ended")])
            self.assertNotIn(("sibling", 2), summary["sent"], "nothing was sent to the member that had exited")
            self.assertEqual(summary["status"], "exited", run.result)
            self.assertEqual(summary["fds_left"], {})
            return summary
        self.each_module(scenario)

    def test_a_member_that_called_setsid_after_the_scan_is_never_signalled_and_is_reported_as_left_the_session(self):
        def scenario(mod):
            def tweak(tree, env):
                sibling = tree.pids["sibling"]

                def leave():
                    os.kill(sibling, signal.SIGUSR1)
                    tree.wait_for(lambda: os.getsid(sibling) != os.getsid(tree.pids["host"]), "the sibling's setsid")
                env.boot_once = [leave]
            run = run_real(self, mod, "default", "setsid-on-usr1", owner_ends=False, tweak=tweak)
            summary = real_summary(run)
            self.assertEqual(summary["signals"][0][:5], ("SIGINT", "sigint", False, ["command"],
                                                         [("sibling", "identity_mismatch")]), summary["signals"])
            self.assertNotIn("sibling", [role for role, _n in summary["sent"]])
            self.assertEqual(summary["status"], "unknown", run.result)
            self.assertEqual(summary["reason"], "left_session_descendants")
            run.tree.wait_for(lambda: run.tree.gone(run.tree.pids["command"]), "the command to end on SIGINT")
            self.assertFalse(run.tree.gone(run.tree.pids["sibling"]), "the escaped sibling was left alone")
            return summary
        self.each_module(scenario)

    def test_a_forbidden_member_is_neither_pinned_nor_signalled_in_any_stage(self):
        def scenario(mod):
            # the sibling is forbidden through the owner's provider: it must survive the whole ladder
            tree = RealTree(self, "stubborn", "stubborn")
            policy = mod.StopPolicy(identity_wait=3.0, sigint_wait=0.4, sigterm_wait=0.4, sigkill_wait=0.8,
                                    call_wait=30.0, poll_interval=0.01)
            env = make_hook_env(mod, tree)
            service = mod.CommandStopService(journal=lambda record: None, policy=policy, env=env)
            tree.cid = str(uuid4())
            service.register(mod.StopTarget(tree.cid, "worker", "terminal", None, identity=tree.identity,
                                            forbidden=lambda: frozenset({tree.pids["sibling"]})))
            result = service.stop("worker", tree.cid)
            role = {pid: name for name, pid in tree.pids.items()}
            sent = [(role[pid], number) for pid, number in env.sent]
            self.assertNotIn("sibling", [name for name, _n in sent])
            self.assertEqual([row["signal"] for row in result["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"])
            tree.wait_for(lambda: tree.gone(tree.pids["command"]), "the command to be killed")
            self.assertFalse(tree.gone(tree.pids["sibling"]), "the forbidden sibling is untouched")
            return (result["status"], result.get("reason"), sent)
        self.each_module(scenario)


if __name__ == "__main__":
    unittest.main()
