"""Per-command stop foundation: process pins and the tracked-process scan (CS-01).

Read-only building blocks. Nothing here decides *whether* to stop a command;
``ProcessPin.send`` is the only place a signal can leave, and only for a process
whose pid, start ticks, uid and boot were re-read from ``/proc`` after its pidfd
was opened. argv, command strings and ``comm`` never decide a target.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import select
import signal
import time

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_DEAD_STATES = frozenset({"Z", "X"})

# ProcessPin / send outcomes.
PINNED = "pinned"
SENT = "sent"
STALE = "stale"  # pid now names another process (or another boot)
ENDED = "ended"  # the process exited (zombie counts)
DIFFERENT_UID = "different_uid"
IDENTITY_CHANGED = "identity_changed"  # changed between verification and the signal
PERMISSION_DENIED = "permission_denied"
CLOSED = "closed"  # the pin released its pidfd; it can no longer signal


@dataclass(frozen=True, slots=True)
class ProcStat:
    pid: int
    state: str
    ppid: int
    pgrp: int
    session: int
    start_ticks: int
    uid: int


class StopEnvironment:
    """Clock, wait, ``/proc`` reads, pidfd and signal operations the stop code uses.

    Tests substitute error-injecting subclasses (S7); production uses ``LinuxStopEnvironment``.
    """

    def monotonic(self) -> float:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError

    def boot_id(self) -> str | None:
        raise NotImplementedError

    def list_pids(self) -> list[int]:
        raise NotImplementedError

    def read_stat(self, pid: int) -> ProcStat | None:
        """The process's current stat, or None when it is gone or unreadable."""
        raise NotImplementedError

    def pidfd_open(self, pid: int) -> int:
        raise NotImplementedError

    def pidfd_exited(self, fd: int) -> bool:
        raise NotImplementedError

    def pidfd_send_signal(self, fd: int, signum: int) -> None:
        raise NotImplementedError

    def close(self, fd: int) -> None:
        raise NotImplementedError


class LinuxStopEnvironment(StopEnvironment):
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def boot_id(self) -> str | None:
        try:
            return BOOT_ID_PATH.read_text().strip() or None
        except OSError:
            return None

    def list_pids(self) -> list[int]:
        try:
            return [int(name) for name in os.listdir("/proc") if name.isdecimal()]
        except OSError:
            return []

    def read_stat(self, pid: int) -> ProcStat | None:
        try:
            fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
            return ProcStat(pid, fields[0].decode(), int(fields[1]), int(fields[2]), int(fields[3]),
                            int(fields[19]), os.stat(f"/proc/{pid}").st_uid)
        except (OSError, IndexError, ValueError):
            return None

    def pidfd_open(self, pid: int) -> int:
        return os.pidfd_open(pid)

    def pidfd_exited(self, fd: int) -> bool:
        """A pidfd polls readable once its process has exited (zombie or reaped)."""
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        return bool(poller.poll(0))

    def pidfd_send_signal(self, fd: int, signum: int) -> None:
        signal.pidfd_send_signal(fd, signum)

    def close(self, fd: int) -> None:
        os.close(fd)


class ProcessPin:
    """A pidfd proven to name one process: same pid, start ticks, uid and boot.

    ``open`` reads ``/proc``, opens the pidfd and reads ``/proc`` again, so the pidfd
    provably names the recorded process (the ``panes._pin_member`` precedent).
    ``status`` is ``pinned`` or the reason it is not (``stale``, ``ended``,
    ``different_uid``); a pin that is not ``pinned`` holds no pidfd and never signals.
    """

    def __init__(self, env: StopEnvironment, pid: int, start_ticks: int, uid: int, status: str, fd: int | None):
        self._env, self.pid, self.start_ticks, self.uid = env, pid, start_ticks, uid
        self.status, self._fd = status, fd

    @classmethod
    def open(cls, env: StopEnvironment, pid: int, start_ticks: int, *, uid: int, boot_id: str) -> ProcessPin:
        def refuse(status: str) -> ProcessPin:
            return cls(env, pid, start_ticks, uid, status, None)
        if not boot_id or env.boot_id() != boot_id:
            return refuse(STALE)
        before = env.read_stat(pid)
        reason = cls._mismatch(before, start_ticks, uid)
        if reason:
            return refuse(reason)
        try:
            fd = env.pidfd_open(pid)
        except ProcessLookupError:
            return refuse(ENDED)
        except OSError:
            return refuse(STALE)
        reason = cls._mismatch(env.read_stat(pid), start_ticks, uid)
        if reason is None and env.pidfd_exited(fd):
            reason = ENDED
        if reason:
            env.close(fd)
            return refuse(reason)
        return cls(env, pid, start_ticks, uid, PINNED, fd)

    @staticmethod
    def _mismatch(stat: ProcStat | None, start_ticks: int, uid: int) -> str | None:
        if stat is None or stat.state in _DEAD_STATES:
            return ENDED if stat is None or stat.start_ticks == start_ticks else STALE
        if stat.start_ticks != start_ticks:
            return STALE
        if stat.uid != uid:
            return DIFFERENT_UID
        return None

    def verify(self) -> str:
        """Re-read the process now; the status becomes the first mismatch found, else stays ``pinned``."""
        if self._fd is None:
            return self.status
        reason = self._mismatch(self._env.read_stat(self.pid), self.start_ticks, self.uid)
        if reason is None and self._env.pidfd_exited(self._fd):
            reason = ENDED
        if reason:
            self.status = reason
        return self.status

    def send(self, signum: int) -> str:
        """Signal the pinned process only after verifying it, and once more immediately before.

        Returns ``sent`` or why nothing was sent (``stale``, ``ended``, ``different_uid``,
        ``identity_changed``, ``permission_denied``, ``signal_failed:<errno name>``).
        """
        status = self.verify()
        if status != PINNED or self._fd is None:
            return status
        reason = self._mismatch(self._env.read_stat(self.pid), self.start_ticks, self.uid)
        if reason:
            self.status = reason
            return ENDED if reason == ENDED else IDENTITY_CHANGED
        try:
            self._env.pidfd_send_signal(self._fd, signum)
        except ProcessLookupError:
            self.status = ENDED
            return ENDED
        except PermissionError:
            return PERMISSION_DENIED
        except OSError as exc:
            return f"signal_failed:{errno.errorcode.get(exc.errno, exc.errno)}"
        return SENT

    def __enter__(self) -> ProcessPin:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            self.status = CLOSED if self.status == PINNED else self.status
            try:
                self._env.close(fd)
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class TrackScan:
    """``tracked``: signal targets. ``left_session``: supervisor descendants that moved to another
    session (setsid); reported, never signalled. Everything else is unchecked."""

    tracked: tuple[ProcStat, ...] = ()
    left_session: tuple[ProcStat, ...] = ()


def _ref(identity: dict, name: str, *, ticks_optional: bool = False) -> tuple[int, int | None] | None:
    """(pid, start_ticks) of a recorded process, or None when the record is incomplete.

    ``ticks_optional`` keeps the pid when the ticks are unknown: a pid alone is enough to exclude a process.
    """
    ref = identity.get(name)
    if not isinstance(ref, dict) or type(ref.get("pid")) is not int:
        return None
    ticks = ref.get("start_ticks")
    if type(ticks) is not int:
        return (ref["pid"], None) if ticks_optional else None
    return ref["pid"], ticks


def scan_tracked(identity: dict, env: StopEnvironment | None = None, *,
                 forbidden: frozenset[int] = frozenset()) -> TrackScan:
    """Classify live processes of one run from ``/proc`` alone (pid, ppid, pgrp, session, uid, state).

    Every pid is read (no ``task/<tid>/children``, which misses children forked by non-main threads).
    ``tracked`` are same-uid live processes in the host shell's session that descend from the
    supervisor, or sit in the command's process group (re-parented after the supervisor is gone).
    The supervisor, the host shell parent, this backend and ``forbidden`` are never listed.
    """
    env = env or LinuxStopEnvironment()
    parent, supervisor = _ref(identity, "parent"), _ref(identity, "supervisor", ticks_optional=True)
    session, group, uid = identity.get("session"), identity.get("child_group"), identity.get("uid")
    if parent is None or type(session) is not int or type(uid) is not int:
        return TrackScan()
    stats = {pid: stat for pid in env.list_pids() if (stat := env.read_stat(pid)) is not None}
    excluded = {parent[0], os.getpid(), *forbidden}
    if supervisor:
        excluded.add(supervisor[0])  # structural: never a target, whatever its ticks now read
    descendants: set[int] = set()
    anchor = stats.get(supervisor[0]) if supervisor else None
    if anchor is not None and anchor.start_ticks == supervisor[1]:
        children: dict[int, list[int]] = {}
        for stat in stats.values():
            children.setdefault(stat.ppid, []).append(stat.pid)
        pending = [anchor.pid]
        while pending:
            for child in children.get(pending.pop(), ()):
                if child not in descendants:
                    descendants.add(child)
                    pending.append(child)
    tracked, left = [], []
    for pid, stat in sorted(stats.items()):
        if pid in excluded or stat.uid != uid or stat.state in _DEAD_STATES:
            continue
        if stat.session == session and (pid in descendants or (type(group) is int and stat.pgrp == group)):
            tracked.append(stat)
        elif pid in descendants:
            left.append(stat)
    return TrackScan(tuple(tracked), tuple(left))
