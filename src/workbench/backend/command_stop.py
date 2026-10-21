"""Per-command stop: process pins, the tracked-process scan (CS-01), and the stop service (CS-02, CS-03).

``ProcessPin.send`` is the only place a signal can leave, and only for a process
whose pid, start ticks, uid and boot were re-read from ``/proc`` after its pidfd
was opened. argv, command strings and ``comm`` never decide a target.

``CommandStopService`` keeps the registry of stoppable commands (the owner of a command, e.g.
``TerminalService``, registers a ``StopTarget`` and reports its end with ``command_ended``) and
runs one ``StopEpisode`` thread per command: identify the run, then per stage (SIGINT, SIGTERM, SIGKILL)
read the tracked members again, pin them, signal them and wait for the *owner* to observe the end (never
``/proc``) up to the stage time. A stop request is not a waiter of the command: it neither consumes nor
delays the owner's completion path. Descendants that left the session (setsid) are reported, never
signalled. A command that outlives the last stage ends the episode as ``unknown``.

A command has one episode (CS-04): a repeat request joins it while it runs and, after it finished, gets its original
result (``repeat: true``) without any new signal or timer. An ``unknown`` is never shown as exited: only when the
owner later observes the command's end is the same episode corrected to ``exited`` (``corrected: true``, ``history``
holding the earlier ``unknown`` and its reason; no signal is sent). An ``unknown`` the owner itself declared is final.

Who may stop what (CS-05, ``STOPPABLE_BY``): the request is checked against the owner role of the registered command
before anything else. The Manager stops the Worker's and its own commands, the Worker only its own
(``refused: not_owner`` otherwise, also for an ended command); the owner is a role, so a restarted Worker session, a
paused automation, a boot-confirmation hold or a closed Task change nothing. An id the service does not hold but the
journal remembers as started and not ended is ``refused: previous_backend_command`` (``stop_survivor``); every
refusal signals nothing and is recorded as ``command_stop_refused``.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
import errno
import os
from pathlib import Path
import select
import signal
import threading
import time
from typing import Any, Callable, Mapping

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


# -- the stop service (CS-02) ---------------------------------------------------------------------

UNCHECKED = "Processes started outside the host shell session are not checked."
SCAN_INTERVAL = 0.25  # how often a waiting stage re-reads the tracked members
# (stage, signal, the StopPolicy field that is its wait)
STAGES = (("sigint", signal.SIGINT, "sigint_wait"),
          ("sigterm", signal.SIGTERM, "sigterm_wait"),
          ("sigkill", signal.SIGKILL, "sigkill_wait"))
# ended command ids remembered (``already_exited``, and their episode for repeats and corrections); an id older
# than the last ENDED_KEPT ended commands is ``not_found`` again, whatever happened to it
ENDED_KEPT = 256
_STALE_REASONS = frozenset({STALE, IDENTITY_CHANGED, DIFFERENT_UID, "identity_mismatch"})
# who may stop a command of which owner (CS-D02): the Manager stops the Worker's and its own commands, the Worker
# only its own. The owner is a role, never an OMP session, a Task or the requester's earlier calls.
STOPPABLE_BY = {"worker": frozenset({"worker"}), "manager": frozenset({"worker", "manager"})}


PREVIOUS_BACKEND_DETAIL = (
    "This command was started by a previous Workbench backend, whose processes Workbench cannot verify as the "
    "command. It is not stopped from here: use workbench_status to see what that backend left running and "
    "stop_survivor for a proven survivor.")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True, slots=True)
class StopPolicy:
    """Stage times in seconds (SIGINT -> SIGTERM -> SIGKILL, the last one is the check; then ``unknown``); injected so tests can shorten them.

    ``identity_wait`` (the run's identity becoming known) is not part of the stage time and ``call_wait`` is how long
    one stop call waits for its episode.
    """

    identity_wait: float = 3.0
    sigint_wait: float = 10.0
    sigterm_wait: float = 5.0
    sigkill_wait: float = 5.0
    call_wait: float = 25.0
    poll_interval: float = 0.02


@dataclass(eq=False)
class StopTarget:
    """What an owner offers for stopping one of its commands.

    ``identity`` returns ``PersistentShell.run_identity()``-shaped data for the outstanding run (or None).
    ``forbidden`` lists pids that must never be signalled whatever the scan says. ``task_status`` returns the status
    of the command's Task as it is when a stop is requested (None without a Task or when unknown); it is read for the
    audit record only and a closed Task never changes what a stop may do.
    """

    command_id: str
    owner: str  # "worker" | "manager"
    kind: str  # "terminal" | "experiment"
    task_id: str | None
    identity: Callable[[], Mapping[str, Any] | None]
    forbidden: Callable[[], frozenset[int]] = lambda: frozenset()
    task_status: Callable[[], str | None] = lambda: None


@dataclass(eq=False)
class _Entry:
    target: StopTarget
    ended: threading.Event = field(default_factory=threading.Event)
    final: Mapping[str, Any] | None = None  # the owner's final result, once it observed the end
    episode: StopEpisode | None = None
    repeats: int = 0


@dataclass(eq=False)
class _Ended:
    """What is remembered of a command the owner saw end: its last state and the entry (so its episode)."""

    info: dict[str, Any]
    entry: _Entry | None
    declared_unknown: str | None = None  # the reason when the owner declared the command unknown, not exited


def _no_identity() -> None:
    return None


def _signal_name(number: Any) -> str | None:
    """The name of a signal number as the owner reported it, or None for anything that is not one."""
    try:
        return signal.Signals(number).name if type(number) is int else None
    except ValueError:
        return None


def _members(stats) -> list[dict[str, int]]:
    return [{"pid": stat.pid, "start_ticks": stat.start_ticks} for stat in stats]


def _left_session(stats) -> list[dict[str, int]]:
    return [{"pid": stat.pid, "start_ticks": stat.start_ticks, "session": stat.session} for stat in stats]


def _complete(identity: Mapping[str, Any] | None, command_id: str) -> bool:
    """The run is this command's, is running, and every field a pin needs is known."""
    if not identity or identity.get("request_id") != command_id or identity.get("phase") != "running":
        return False
    child, parent, supervisor = identity.get("child"), identity.get("parent"), identity.get("supervisor")
    # the command process's own start ticks are unknown (None) once it was reaped before they were read: a
    # command whose main ended, with descendants left, is still stoppable (no scan uses the child's ticks)
    return (all(isinstance(ref, dict) and type(ref.get("pid")) is int and type(ref.get("start_ticks")) is int
                for ref in (parent, supervisor))
            and isinstance(child, dict) and type(child.get("pid")) is int
            and type(child.get("start_ticks")) in (int, type(None))
            and type(identity.get("child_group")) is int and type(identity.get("session")) is int
            and type(identity.get("uid")) is int and isinstance(identity.get("boot_id"), str))


class StopEpisode:
    """The one stop of one command: a thread drives the stages; callers wait on ``done``."""

    def __init__(self, service: CommandStopService, entry: _Entry, requested_by: str):
        self._service, self._entry, self.requested_by = service, entry, requested_by
        self.command_id = entry.target.command_id
        self.requested_at = _now()
        self.stage = "identifying"
        self.signals: list[dict[str, Any]] = []
        self.stop_signal: str | None = None  # the last signal this episode sent (set just before it leaves)
        self.result: dict[str, Any] | None = None
        self.done = threading.Event()
        self._settle = threading.Lock()  # the final result and the late correction are decided one at a time
        self._started = service.env.monotonic()
        self._identity: Mapping[str, Any] | None = None
        self._unknown_at = ""  # when the stop ended unknown (for the history of a later correction)
        self._mismatched: set[int] = set()  # pids whose identity failed verification: never signalled in this episode
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"command-stop-{self.command_id[:8]}")

    def start(self) -> None:
        self._thread.start()

    def summary(self) -> dict[str, Any]:
        done = self.done.is_set()
        return {"state": "stopping" if not done else (self.result or {}).get("status"),
                "reason": (self.result or {}).get("reason") if done else None,
                "stage": self.stage, "requested_by": self.requested_by, "requested_at": self.requested_at,
                "signals": [dict(item) for item in self.signals]}

    # -- the stages ----------------------------------------------------------------------------
    def _run(self) -> None:
        try:
            result = self._drive()
        except Exception as exc:  # never leave a waiting call hanging
            result = self._unknown(f"episode_error:{type(exc).__name__}")
        self._finish(result)

    def _drive(self) -> dict[str, Any]:
        service, entry, policy = self._service, self._entry, self._service.policy
        scan = self._await_identity()
        if scan is None:
            return self._after_end() if entry.ended.is_set() else self._unknown("identity_unavailable")
        identity = self._identity
        child = identity["child"]
        # the run identity names the command process: if its pid reads as another process, every claim of the
        # identity (and so every member found through it) is untrusted
        main_changed = child["start_ticks"] is not None and any(
            stat.pid == child["pid"] and stat.start_ticks != child["start_ticks"] for stat in scan.tracked)
        service.record({"type": "command_stop_target", "command_id": self.command_id, "pid": child["pid"],
                        "start_ticks": child["start_ticks"], "process_group": identity["child_group"],
                        "session": identity["session"], "uid": identity["uid"], "boot_id": identity["boot_id"],
                        "supervisor": identity["supervisor"], "tracked": _members(scan.tracked),
                        "left_session": _left_session(scan.left_session)})
        if main_changed:
            return self._stale()
        for name, signum, wait in STAGES:
            result = self._stage(name, signum, getattr(policy, wait), scan)
            if result is not None:
                return result
            scan = None  # every later stage reads the tracked members again
        return self._unknown(f"still_alive_after_{STAGES[-1][0]}")

    def _stage(self, name: str, signum: int, seconds: float, scan: TrackScan | None) -> dict[str, Any] | None:
        """One stage: re-read the tracked members, pin them, signal them, wait for the owner to see the end.

        Returns the final result, or None when the command is still there and the next stage should follow.
        The pins live for this stage only: a later stage never reuses them.
        """
        service, entry, env = self._service, self._entry, self._service.env
        if entry.ended.is_set():
            return self._after_end()
        if service.closed.is_set():
            return self._unknown("backend_shutdown")
        if scan is None:
            self._refresh_identity()
            scan = self._scan()
        if not scan.tracked and scan.left_session:
            return self._left_session_unknown()  # nothing left that may be signalled
        identity = self._identity
        self.stage = name
        failed: list[dict[str, Any]] = []
        first = not self.signals
        with ExitStack() as pins:
            live = []
            for stat in scan.tracked:
                if stat.pid in self._mismatched:
                    failed.append({"pid": stat.pid, "reason": "identity_mismatch"})
                    continue
                pin = pins.enter_context(ProcessPin.open(env, stat.pid, stat.start_ticks, uid=identity["uid"],
                                                         boot_id=identity["boot_id"]))
                if pin.status == PINNED:
                    live.append(pin)
                elif pin.status != ENDED:
                    reason = "identity_mismatch" if pin.status == STALE else pin.status
                    if reason in _STALE_REASONS:
                        self._mismatched.add(stat.pid)
                    failed.append({"pid": stat.pid, "reason": reason})
            sent = self._send(live, signum, name, failed, auxiliary=False) if live else []
            if sent and signum != signal.SIGKILL:
                # a stopped member cannot act on SIGINT/SIGTERM until it is continued
                reached = {item["pid"] for item in sent}
                stopped = [pin for pin in live if pin.pid in reached and self._stopped(pin.pid)]
                if stopped:
                    self._send(stopped, signal.SIGCONT, name, [], auxiliary=True)
            hard = [item for item in failed if item["reason"] != ENDED]
            unsent_stale = first and hard and all(item["reason"] in _STALE_REASONS for item in hard)
            if not live and failed and not unsent_stale:  # nothing to signal: the failures are still the stage's record
                self._send([], signum, name, failed, auxiliary=False)
            if not sent and hard and not entry.ended.is_set():
                if all(item["reason"] == PERMISSION_DENIED for item in hard):
                    return self._unknown(PERMISSION_DENIED)
                if first:  # nothing could be signalled, and nothing was before
                    if all(item["reason"] in _STALE_REASONS for item in hard):
                        return self._stale()
                    return self._unknown(hard[0]["reason"])
        # the owner decides when the command is over: ``/proc`` never does
        self.stage = f"{name}_wait"
        outcome = self._wait_stage(seconds)
        if outcome == "ended":
            return self._after_end()
        if outcome == "closed":
            return self._unknown("backend_shutdown")
        if outcome == "left_session":
            return self._left_session_unknown()
        return None

    def _send(self, pins: list[ProcessPin], signum: int, stage: str, failed: list[dict[str, Any]], *,
              auxiliary: bool) -> list[dict[str, int]]:
        """Signal each pinned member; record the signal with the members reached and those that failed."""
        at, sent = _now(), []
        if pins and not auxiliary:
            self.stop_signal = signal.Signals(signum).name  # before it leaves: the owner may see the end at once
        for pin in pins:
            outcome = pin.send(signum)
            if outcome == SENT:
                sent.append({"pid": pin.pid, "start_ticks": pin.start_ticks})
            else:  # ESRCH reads as ``ended``, EPERM as ``permission_denied``
                if outcome in _STALE_REASONS:
                    self._mismatched.add(pin.pid)
                failed.append({"pid": pin.pid, "reason": outcome})
        name = signal.Signals(signum).name
        if sent:
            self.signals.append({"signal": name, "number": int(signum), "at": at, "stage": stage,
                                 "auxiliary": auxiliary, "targets": sent, "failed": [dict(item) for item in failed]})
        self._service.record({"type": "command_stop_signal", "command_id": self.command_id, "signal": name,
                              "at": at, "stage": stage, "targets": sent, "failed": list(failed),
                              "auxiliary": auxiliary})
        return sent

    def _stopped(self, pid: int) -> bool:
        stat = self._service.env.read_stat(pid)
        return stat is not None and stat.state == "T"

    def _refresh_identity(self) -> None:
        """Adopt the owner's current identity when it is a complete running one; otherwise keep the last one.

        An ended (or not yet complete) identity is never scanned with.
        """
        try:
            identity = self._entry.target.identity()
        except Exception:
            return
        if _complete(identity, self.command_id):
            self._identity = identity

    def _scan(self) -> TrackScan:
        return scan_tracked(self._identity, self._service.env, forbidden=frozenset(self._entry.target.forbidden()))

    def _await_identity(self):
        """The scan once the run's identity is complete and names a member, or None (ended, closed or ``identity_wait`` over)."""
        service, entry, policy, env = self._service, self._entry, self._service.policy, self._service.env
        deadline = env.monotonic() + policy.identity_wait
        while not entry.ended.is_set():
            try:
                identity = entry.target.identity()
            except Exception:
                identity = None
            if _complete(identity, self.command_id):
                scan = scan_tracked(identity, env, forbidden=frozenset(entry.target.forbidden()))
                if scan.tracked or scan.left_session:  # descendants that left the session are reported, not signalled
                    self._identity = identity
                    return scan
            if env.monotonic() >= deadline or service.closed.is_set():
                return None
            entry.ended.wait(policy.poll_interval)
        return None

    def _wait_stage(self, seconds: float) -> str:
        """Wait up to ``seconds`` for the owner to observe the end: ``ended``, ``closed``, ``left_session`` or ``timeout``.

        ``left_session``: only descendants that left the session remain, so no further signal can help.
        """
        service, entry, env = self._service, self._entry, self._service.env
        deadline = env.monotonic() + seconds
        next_scan = env.monotonic() + SCAN_INTERVAL
        while True:
            if entry.ended.is_set():
                return "ended"
            if service.closed.is_set():
                return "closed"
            remaining = deadline - env.monotonic()
            if remaining <= 0:
                return "timeout"
            entry.ended.wait(min(remaining, 0.1))
            if not entry.ended.is_set() and env.monotonic() >= next_scan:
                next_scan = env.monotonic() + SCAN_INTERVAL
                try:
                    scan = self._scan()
                except Exception:
                    continue
                if not scan.tracked and scan.left_session:
                    return "left_session"

    # -- results -------------------------------------------------------------------------------
    def _base(self, status: str, **extra: Any) -> dict[str, Any]:
        return {"status": status, "command_id": self.command_id, "requested_by": self.requested_by, **extra}

    def _residual(self) -> dict[str, Any]:
        """What is left, read with the last running identity (never an ended one)."""
        identity = self._identity
        alive, left = [], []
        if identity is not None:
            try:
                scan = self._scan()
                alive = _members(scan.tracked)
                left = _left_session(scan.left_session)
            except Exception:
                return {"checked": False, "alive": [], "left_session": [], "unchecked": UNCHECKED}
        return {"checked": identity is not None, "alive": alive, "left_session": left, "unchecked": UNCHECKED}

    def _after_end(self) -> dict[str, Any]:
        final = self._entry.final or {}
        if not self.signals and final.get("status") != "unknown":  # it ended by itself before any signal left
            return self._base("already_exited", exit_code=final.get("exit_code"), signal=final.get("signal"),
                              ended_at=_now(), signals=[])
        extra = {"signals": [dict(item) for item in self.signals], "residual": self._residual()}
        if final.get("status") == "exited":
            number = final.get("signal")
            return self._base("exited", exit_code=final.get("exit_code"), signal=number,
                              signal_name=_signal_name(number), **extra)
        return self._base("unknown", reason=final.get("reason") or "owner_unknown", **extra)

    def _main_exit(self) -> dict[str, Any]:
        """``exit_code``/``signal`` of the main process when the owner observed its end (else nothing)."""
        try:
            identity = self._entry.target.identity()
        except Exception:
            return {}
        observed = identity.get("main_exit") if identity and identity.get("request_id") == self.command_id else None
        if not isinstance(observed, dict):
            return {}
        return {"exit_code": observed.get("exit_code"), "signal": observed.get("signal")}

    def _unknown(self, reason: str) -> dict[str, Any]:
        self._unknown_at = _now()
        return self._base("unknown", reason=reason, **self._main_exit(),
                          signals=[dict(item) for item in self.signals], residual=self._residual())

    def _left_session_unknown(self) -> dict[str, Any]:
        """``unknown`` for descendants that left the session; the main command's end may be about to be observed.

        The owner learns of the main exit a poll after the process is gone, and this decision is made as soon as the
        scan shows only escaped descendants, so the exit is read once more after one poll interval. If the owner has
        then seen the whole run end, that end is the result.
        """
        entry = self._entry
        if not self._main_exit():
            entry.ended.wait(self._service.policy.poll_interval)
            if entry.ended.is_set():
                return self._after_end()
        return self._unknown("left_session_descendants")

    def _stale(self) -> dict[str, Any]:
        self._service.record({"type": "command_stop_refused", "command_id": self.command_id,
                              "requested_by": self.requested_by, "status": "stale", "reason": "identity_mismatch"})
        return self._base("stale", reason="identity_mismatch", signals=[])

    def _finish(self, result: dict[str, Any]) -> None:
        service = self._service
        if result["status"] == "already_exited":  # it ended before any signal: the record of a refused stop
            service.record_refusal(self.command_id, self.requested_by, "already_exited", "command_ended")
        else:
            elapsed = round(service.env.monotonic() - self._started, 3)
            service.record({"type": "command_stop_finished", "command_id": self.command_id,
                            "status": result["status"], "exit_code": result.get("exit_code"),
                            "signal": result.get("signal"), "reason": result.get("reason"),
                            "residual": result.get("residual"), "elapsed_seconds": elapsed,
                            "repeat_requests": self._entry.repeats})
        with self._settle:
            self.stage = "done"
            self.result = result
            corrected = self._correct()  # the owner may have observed the end while this was being decided
            self.done.set()
        self._entry.target.identity = _no_identity  # nothing reads it again: do not keep the owner's command alive
        if corrected is not None:
            service.record(corrected)

    def owner_ended(self) -> None:
        """The owner observed the command's end after this episode was created.

        An episode that already ended ``unknown`` is corrected to ``exited`` (no signal is sent); one that is
        still running sees the end itself, and ``_finish`` corrects if it had already decided ``unknown``.
        """
        with self._settle:
            corrected = self._correct() if self.done.is_set() else None
        if corrected is not None:
            self._service.record(corrected)

    def _correct(self) -> dict[str, Any] | None:
        """Under ``_settle``: turn a final ``unknown`` into ``exited`` once the owner reports the command exited.

        Returns the ``command_stop_corrected`` record, or None when there is nothing to correct (an unknown the
        owner declared itself is final: it is not an exited end).
        """
        before, final = self.result, self._entry.final
        if before is None or before.get("status") != "unknown" or not final or final.get("status") != "exited":
            return None
        number = final.get("signal")
        history = [{"status": "unknown", "reason": before.get("reason"), "at": self._unknown_at,
                    "exit_code": before.get("exit_code"), "signal": before.get("signal")}]
        self.result = self._base("exited", exit_code=final.get("exit_code"), signal=number,
                                 signal_name=_signal_name(number), signals=[dict(item) for item in self.signals],
                                 residual=self._residual(), corrected=True, history=history)
        return {"type": "command_stop_corrected", "command_id": self.command_id, "from": "unknown",
                "previous_reason": before.get("reason"), "to": "exited", "exit_code": final.get("exit_code"),
                "signal": number, "repeat_requests": self._entry.repeats}


class CommandStopService:
    """The registry of stoppable commands and the episodes that stop them."""

    def __init__(self, *, journal: Callable[[dict[str, Any]], Any], policy: StopPolicy = StopPolicy(),
                 env: StopEnvironment | None = None,
                 previous_backend: Callable[[str], bool] | None = None):
        self._journal, self.policy = journal, policy
        self._previous_backend = previous_backend
        self.env = env or LinuxStopEnvironment()
        self._lock = threading.Lock()
        self._live: dict[str, _Entry] = {}
        self._ended: dict[str, _Ended] = {}
        self.closed = threading.Event()

    def record(self, record: dict[str, Any]) -> None:
        """One audit record; a journal failure never changes what a stop does."""
        try:
            self._journal({**record, "at": record.get("at") or _now()})
        except Exception:
            pass

    def close(self) -> None:
        self.closed.set()  # episodes are daemon threads: they stop signalling and end, shutdown never waits on them

    # -- the owner's side ----------------------------------------------------------------------
    def register(self, target: StopTarget) -> None:
        with self._lock:
            self._live[target.command_id] = _Entry(target)

    def command_ended(self, command_id: str, result: Mapping[str, Any]) -> None:
        """The owner observed the command's end (its final result)."""
        with self._lock:
            entry = self._live.pop(command_id, None)
            self._ended[command_id] = _Ended(
                {"exit_code": result.get("exit_code"), "signal": result.get("signal"), "ended_at": _now()}, entry,
                (result.get("reason") or "owner_unknown") if result.get("status") == "unknown" else None)
            while len(self._ended) > ENDED_KEPT:
                del self._ended[next(iter(self._ended))]
        if entry is not None:
            entry.final = dict(result)
            entry.target.identity = _no_identity  # the owner's command is not kept alive by what is remembered
            entry.ended.set()
            if entry.episode is not None:
                entry.episode.owner_ended()  # an episode that already ended unknown is corrected

    def stop_marker(self, command_id: str) -> dict[str, str]:
        """``{"stopped_by", "stop_signal"}`` once a stop of this command has sent a signal, else ``{}``.

        For the owner, which puts it on the command's result, notice and run record (the Worker must not take a
        command the Manager stopped for a failure of its own). ``stopped_by`` is the requester of the command's
        one episode; ``stop_signal`` the last signal that episode sent.
        """
        with self._lock:
            entry = self._live.get(command_id)
            if entry is None:
                gone = self._ended.get(command_id)
                entry = gone.entry if gone is not None else None
            episode = entry.episode if entry is not None else None
        if episode is None or episode.stop_signal is None:
            return {}
        return {"stopped_by": episode.requested_by, "stop_signal": episode.stop_signal}

    def surface(self, command_id: str) -> dict[str, Any] | None:
        """``{"state": "stopping"|"unknown", "stop": summary}`` while a stop is under way or unconfirmed, else None."""
        with self._lock:
            entry = self._live.get(command_id)
            episode = entry.episode if entry is not None else None
        if episode is None:
            return None
        summary = episode.summary()
        if not episode.done.is_set():
            return {"state": "stopping", "stop": summary}
        if (episode.result or {}).get("status") == "unknown":
            return {"state": "unknown", "stop": summary}
        return None

    # -- a stop request ------------------------------------------------------------------------
    def stop(self, requested_by: str, command_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """Stop one command and wait for the final state (at most ``policy.call_wait``); never raises.

        A command has one episode: a request while it runs joins it, a request after it finished gets its original
        final result. Both carry ``repeat: true`` and start no signal and no timer.

        Limit: a joiner arriving in the instant between the episode's ``command_stop_finished`` record and ``done``
        gets the result, but is in neither ``repeat_requests`` of that record nor a ``command_stop_repeat`` record.
        """
        base = {"command_id": command_id, "requested_by": requested_by}
        if self.closed.is_set():
            return self._refused(base, "refused", "backend_shutdown")
        with self._lock:
            entry = self._live.get(command_id)
            gone = self._ended.get(command_id) if entry is None else None
            owner = entry if entry is not None else (gone.entry if gone is not None else None)
            denied = owner is not None and owner.target.owner not in STOPPABLE_BY.get(requested_by, ())
            joined = owner is not None and not denied and owner.episode is not None
            if owner is not None and not denied and not joined:
                owner.episode = StopEpisode(self, owner, requested_by)
            elif joined:
                owner.repeats += 1
            episode = owner.episode if owner is not None else None
            repeats = owner.repeats if owner is not None else 0
        if denied:  # live or ended, never the state of a command that is not this requester's to stop
            return self._refused(base, "refused", "not_owner")
        if episode is None:
            if gone is None:
                if self._from_previous_backend(command_id):
                    return self._refused(base, "refused", "previous_backend_command", detail=PREVIOUS_BACKEND_DETAIL)
                return self._refused(base, "not_found", "unknown_command")
            # the command ended without a stop episode: its last state, with the same record pair for every id the
            # service remembers (the request, then its refusal; for a declared unknown the request, then a finish).
            # An id the owner reported without registering has no target: the request record has no kind/owner/task.
            self._record_requested(gone.entry.target if gone.entry is not None else None, requested_by, reason,
                                   command_id)
            if gone.declared_unknown is not None:  # never an exit: the owner does not know how it ended
                result = {"status": "unknown", **base, "reason": gone.declared_unknown, "exit_code": gone.info["exit_code"],
                          "signal": gone.info["signal"], "signals": [],
                          "residual": {"checked": False, "alive": [], "left_session": [], "unchecked": UNCHECKED}}
                self.record({"type": "command_stop_finished", "command_id": command_id, "status": "unknown",
                             "exit_code": result["exit_code"], "signal": result["signal"], "reason": result["reason"],
                             "residual": result["residual"], "elapsed_seconds": 0.0, "repeat_requests": 0})
                return result
            return self._refused({"status": "already_exited", **base, **gone.info}, "already_exited", "command_ended")
        if not joined:
            self._record_requested(owner.target, requested_by, reason)
            episode.start()
        elif episode.done.is_set():  # after the episode: no new work, only the count
            self.record({"type": "command_stop_repeat", "command_id": command_id, "requested_by": requested_by,
                         "repeat_requests": repeats})
        if not episode.done.wait(self.policy.call_wait):
            return {"status": "stopping", **base, "stop": episode.summary(), **({"repeat": True} if joined else {})}
        return {**(episode.result or {}), **({"repeat": True} if joined else {})}

    def _record_requested(self, target: StopTarget | None, requested_by: str, reason: str | None,
                          command_id: str | None = None) -> None:
        self.record({"type": "command_stop_requested", "command_id": target.command_id if target else command_id,
                     "requested_by": requested_by, "kind": target.kind if target else None,
                     "owner": target.owner if target else None, "task_id": target.task_id if target else None,
                     "task_status": self._task_status(target), "reason": reason})

    @staticmethod
    def _task_status(target: StopTarget | None) -> str | None:
        if target is None or target.task_id is None:
            return None
        try:
            status = target.task_status()
        except Exception:
            return None  # the audit record says nothing rather than something wrong
        return status if isinstance(status, str) else None

    def _from_previous_backend(self, command_id: str) -> bool:
        """The terminal journal shows a start of this id that no end followed and this backend does not hold: a
        previous backend's. Workbench cannot prove who that process is now, so it is not stopped from here."""
        try:
            return bool(self._previous_backend is not None and self._previous_backend(command_id))
        except Exception:
            return False  # unreadable: nothing is claimed about the id

    def _refused(self, result: dict[str, Any], status: str, reason: str, *, detail: str | None = None) -> dict[str, Any]:
        result = {**result, "status": status}
        if detail is not None:
            result["detail"] = detail
        if status != "already_exited":
            result["reason"] = reason
        self.record_refusal(result["command_id"], result["requested_by"], status, reason)
        return result

    def record_refusal(self, command_id: str, requested_by: str, status: str, reason: str) -> None:
        self.record({"type": "command_stop_refused", "command_id": command_id, "requested_by": requested_by,
                     "status": status, "reason": reason})
