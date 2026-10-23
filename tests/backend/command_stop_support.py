"""Shared test-side helpers for the command-stop suites (tests/backend and tests/integration/live_command_stop.py).

Everything here reads ``/proc`` or waits on the *test's own* view of a process; none of it is the production reader
(``workbench.backend.command_stop``), so a test that uses it still derives its expectations independently of the
implementation under test.

Error handling that the former local copies agreed on and that is kept here:

* ``stat_fields`` / ``proc_stat`` / ``proc`` raise (``OSError`` for a vanished pid, ``IndexError`` for an unparsable
  stat line) -- callers that expect the pid to exist get the real error;
* ``stat_fields_or_none`` / ``stat_of`` / ``stat_ticks`` / ``alive`` swallow exactly those errors and answer
  ``None`` / ``False`` for a pid that vanished while it was being read.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path


# ------------------------------------------------------------------------------------------ /proc/<pid>/stat
def stat_fields(pid: int) -> list[str]:
    """Fields 3.. of /proc/<pid>/stat: [0] state, [1] ppid, [2] pgrp, [3] session, [19] start ticks.

    Splits after the last ``") "`` so a command name containing spaces or parentheses does not shift the fields.
    Raises ``OSError`` when the pid is gone and ``IndexError`` when the line has no ``") "``.
    """
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()


def stat_fields_or_none(pid: int) -> list[str] | None:
    """``stat_fields`` or ``None`` when the pid vanished (or its line cannot be parsed)."""
    try:
        return stat_fields(pid)
    except (OSError, IndexError):
        return None


def proc_stat(pid: int) -> tuple[str, int, int, int, int]:
    """(state, ppid, pgrp, session, start_ticks); raises like ``stat_fields``."""
    fields = stat_fields(pid)
    return fields[0], int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19])


def stat_of(pid: int) -> tuple[str, int, int, int, int] | None:
    """``proc_stat`` or ``None`` when the pid vanished (or its line cannot be parsed)."""
    try:
        return proc_stat(pid)
    except (OSError, IndexError):
        return None


def stat_ticks(pid: int) -> int | None:
    """Start ticks of ``pid``, or ``None`` when it is gone."""
    try:
        return int(stat_fields(pid)[19])
    except (OSError, IndexError, ValueError):
        return None


@dataclasses.dataclass(frozen=True)
class Proc:
    pid: int
    state: str
    ppid: int
    pgrp: int
    session: int
    ticks: int


def proc(pid: int) -> Proc | None:
    found = stat_of(pid)
    return None if found is None else Proc(pid, *found)


def alive(pid: int, ticks: int | None = None) -> bool:
    """True while ``pid`` is a live (non-zombie, not dead) process, optionally with exactly these start ticks."""
    found = stat_fields_or_none(pid)
    return found is not None and found[0] not in ("Z", "X") and (ticks is None or int(found[19]) == ticks)


def all_procs() -> list[Proc]:
    found = []
    for name in os.listdir("/proc"):
        if name.isdecimal() and (item := proc(int(name))) is not None:
            found.append(item)
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


# ---------------------------------------------------------------------------------------- /proc/<pid>/cmdline
def pids_running(*argv: str, session: int | None = None) -> list[int]:
    """Live pids whose whole cmdline equals ``argv``, read from /proc by the test (production never uses argv).

    With ``session`` only processes of that session count (the session leader's pid is the session id): a concurrent
    suite's identical ``sleep`` on the same machine must not be counted.
    """
    wanted = b"\0".join(item.encode() for item in argv) + b"\0"
    found = []
    for name in os.listdir("/proc"):
        if not name.isdecimal():
            continue
        try:
            if Path(f"/proc/{name}/cmdline").read_bytes() != wanted or not alive(int(name)):
                continue
            if session is not None:
                fields = stat_fields(int(name))
                if int(fields[3]) != session:
                    continue
            found.append(int(name))
        except (OSError, IndexError, ValueError):
            continue
    return found


def pids_with(marker: str) -> list[int]:
    """Live pids (not this process) whose command line contains ``marker``."""
    needle, found = marker.encode(), []
    for name in os.listdir("/proc"):
        if not name.isdecimal() or int(name) == os.getpid():
            continue
        try:
            if needle in Path(f"/proc/{name}/cmdline").read_bytes() and alive(int(name)):
                found.append(int(name))
        except OSError:
            continue
    return found


# ------------------------------------------------------------------------------------------------ run identity
def identity_with_child(shell, timeout=30.0):
    """The run identity once the command child is known: its `child` is None while the run is starting.

    ``timeout`` only costs time when the child never shows up (a failing test), so it is generous.
    """
    from test_task_flow import wait_until  # lazy: the /proc helpers above must not pull the Task-flow fixtures in

    found = []

    def has_child():
        identity = shell.run_identity()
        if identity and identity.get("child"):
            found.append(identity)
        return bool(found)

    if not wait_until(has_child, timeout):
        raise AssertionError(f"the command child was not identified within {timeout}s: {shell.run_identity()}")
    return found[-1]
