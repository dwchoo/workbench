"""Per-command stop: process pins, the tracked-process scan (CS-01), and the stop service (CS-02).

``ProcessPin.send`` is the only place a signal can leave, and only for a process
whose pid, start ticks, uid and boot were re-read from ``/proc`` after its pidfd
was opened. argv, command strings and ``comm`` never decide a target.

``CommandStopService`` keeps the registry of stoppable commands (the owner of a command, e.g.
``TerminalService``, registers a ``StopTarget`` and reports its end with ``command_ended``) and
runs one ``StopEpisode`` thread per command: identify the run, pin the tracked members, send
SIGINT, then wait for the *owner* to observe the end (never ``/proc``) up to the stage time.
A stop request is not a waiter of the command: it neither consumes nor delays the owner's completion
path. CS-02 has the SIGINT stage only: a command that outlives it ends the episode as ``unknown``
(SIGTERM/SIGKILL, repeat requests and the ``unknown`` correction are later tickets).
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
ENDED_KEPT = 256  # ended command ids remembered for ``already_exited``
_STALE_REASONS = frozenset({STALE, IDENTITY_CHANGED, DIFFERENT_UID, "identity_mismatch"})


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True, slots=True)
class StopPolicy:
    """Stage times in seconds (SIGINT -> SIGTERM -> SIGKILL, then ``unknown``); injected so tests can shorten them.

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
    ``forbidden`` lists pids that must never be signalled whatever the scan says.
    """

    command_id: str
    owner: str  # "worker" | "manager"
    kind: str  # "terminal" | "experiment"
    task_id: str | None
    identity: Callable[[], Mapping[str, Any] | None]
    forbidden: Callable[[], frozenset[int]] = lambda: frozenset()


@dataclass(eq=False)
class _Entry:
    target: StopTarget
    ended: threading.Event = field(default_factory=threading.Event)
    final: Mapping[str, Any] | None = None  # the owner's final result, once it observed the end
    episode: StopEpisode | None = None
    repeats: int = 0


def _members(stats) -> list[dict[str, int]]:
    return [{"pid": stat.pid, "start_ticks": stat.start_ticks} for stat in stats]


def _left_session(stats) -> list[dict[str, int]]:
    return [{"pid": stat.pid, "start_ticks": stat.start_ticks, "session": stat.session} for stat in stats]


def _complete(identity: Mapping[str, Any] | None, command_id: str) -> bool:
    """The run is this command's, is running, and every field a pin needs is known."""
    if not identity or identity.get("request_id") != command_id or identity.get("phase") != "running":
        return False
    child, parent, supervisor = identity.get("child"), identity.get("parent"), identity.get("supervisor")
    return (all(isinstance(ref, dict) and type(ref.get("pid")) is int and type(ref.get("start_ticks")) is int
                for ref in (child, parent, supervisor))
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
        self.result: dict[str, Any] | None = None
        self.done = threading.Event()
        self._started = service.env.monotonic()
        self._identity: Mapping[str, Any] | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"command-stop-{self.command_id[:8]}")

    def start(self) -> None:
        self._thread.start()

    def summary(self) -> dict[str, Any]:
        return {"state": "stopping" if not self.done.is_set() else (self.result or {}).get("status"),
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
        service, entry, policy, env = self._service, self._entry, self._service.policy, self._service.env
        scan = self._await_identity()
        if scan is None:
            return self._after_end(None) if entry.ended.is_set() else self._unknown("identity_unavailable")
        identity = self._identity
        child = identity["child"]
        # the run identity names the command process: if its pid reads as another process, every claim of the
        # identity (and so every member found through it) is untrusted
        main_changed = any(stat.pid == child["pid"] and stat.start_ticks != child["start_ticks"]
                           for stat in scan.tracked)
        service.record({"type": "command_stop_target", "command_id": self.command_id, "pid": child["pid"],
                        "start_ticks": child["start_ticks"], "process_group": identity["child_group"],
                        "session": identity["session"], "uid": identity["uid"], "boot_id": identity["boot_id"],
                        "supervisor": identity["supervisor"], "tracked": _members(scan.tracked),
                        "left_session": _left_session(scan.left_session)})
        if main_changed:
            return self._stale()
        failed: list[dict[str, Any]] = []
        with ExitStack() as pins:
            live = []
            for stat in scan.tracked:
                pin = pins.enter_context(ProcessPin.open(env, stat.pid, stat.start_ticks, uid=identity["uid"],
                                                         boot_id=identity["boot_id"]))
                if pin.status == PINNED:
                    live.append(pin)
                elif pin.status != ENDED:
                    failed.append({"pid": stat.pid, "reason": "identity_mismatch" if pin.status == STALE
                                   else pin.status})
            sent = []
            if live:
                self.stage = "sigint"
                at = _now()
                for pin in live:
                    outcome = pin.send(signal.SIGINT)
                    if outcome == SENT:
                        sent.append({"pid": pin.pid, "start_ticks": pin.start_ticks})
                    elif outcome != ENDED:
                        failed.append({"pid": pin.pid, "reason": outcome})
                if sent:
                    self.signals.append({"signal": "SIGINT", "number": int(signal.SIGINT), "at": at,
                                         "stage": "sigint", "auxiliary": False, "targets": sent,
                                         "failed": [dict(item) for item in failed]})
                service.record({"type": "command_stop_signal", "command_id": self.command_id, "signal": "SIGINT",
                                "at": at, "stage": "sigint", "targets": sent, "failed": list(failed),
                                "auxiliary": False})
            if not sent and failed and not entry.ended.is_set():
                # nothing could be signalled: before any signal, a changed identity is ``stale``
                if all(item["reason"] in _STALE_REASONS for item in failed):
                    return self._stale()
                return self._unknown(failed[0]["reason"])
        # the owner decides when the command is over: ``/proc`` never does
        self.stage = "sigint_wait"
        deadline = env.monotonic() + policy.sigint_wait
        if self._wait_owner(deadline):
            return self._after_end(identity)
        return self._unknown("still_alive_after_sigint", identity)

    def _await_identity(self):
        """The tracked scan once the run's identity is complete, or None (ended, closed or ``identity_wait`` over)."""
        service, entry, policy, env = self._service, self._entry, self._service.policy, self._service.env
        deadline = env.monotonic() + policy.identity_wait
        while not entry.ended.is_set():
            try:
                identity = entry.target.identity()
            except Exception:
                identity = None
            if _complete(identity, self.command_id):
                scan = scan_tracked(identity, env, forbidden=frozenset(entry.target.forbidden()))
                if scan.tracked:
                    self._identity = identity
                    return scan
            if env.monotonic() >= deadline or service.closed.is_set():
                return None
            entry.ended.wait(policy.poll_interval)
        return None

    def _wait_owner(self, deadline: float) -> bool:
        service, entry, env = self._service, self._entry, self._service.env
        while not entry.ended.is_set():
            remaining = deadline - env.monotonic()
            if remaining <= 0 or service.closed.is_set():
                return False
            entry.ended.wait(min(remaining, 0.1))
        return True

    # -- results -------------------------------------------------------------------------------
    def _base(self, status: str, **extra: Any) -> dict[str, Any]:
        return {"status": status, "command_id": self.command_id, "requested_by": self.requested_by, **extra}

    def _residual(self, identity: Mapping[str, Any] | None) -> dict[str, Any]:
        alive, left = [], []
        if identity is not None:
            try:
                scan = scan_tracked(identity, self._service.env,
                                    forbidden=frozenset(self._entry.target.forbidden()))
                alive = _members(scan.tracked)
                left = _left_session(scan.left_session)
            except Exception:
                return {"checked": False, "alive": [], "left_session": [], "unchecked": UNCHECKED}
        return {"checked": identity is not None, "alive": alive, "left_session": left, "unchecked": UNCHECKED}

    def _after_end(self, identity: Mapping[str, Any] | None) -> dict[str, Any]:
        final = self._entry.final or {}
        if not self.signals:  # the command ended by itself before any signal left
            return self._base("already_exited", exit_code=final.get("exit_code"), signal=final.get("signal"),
                              ended_at=_now(), signals=[])
        extra = {"signals": [dict(item) for item in self.signals], "residual": self._residual(identity)}
        if final.get("status") == "exited":
            number = final.get("signal")
            return self._base("exited", exit_code=final.get("exit_code"), signal=number,
                              signal_name=signal.Signals(number).name if type(number) is int and number in
                              signal.Signals._value2member_map_ else None, **extra)
        return self._base("unknown", reason=final.get("reason") or "owner_unknown", **extra)

    def _unknown(self, reason: str, identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._base("unknown", reason=reason, signals=[dict(item) for item in self.signals],
                          residual=self._residual(identity))

    def _stale(self) -> dict[str, Any]:
        self._service.record({"type": "command_stop_refused", "command_id": self.command_id,
                              "requested_by": self.requested_by, "status": "stale", "reason": "identity_mismatch"})
        return self._base("stale", reason="identity_mismatch", signals=[])

    def _finish(self, result: dict[str, Any]) -> None:
        elapsed = round(self._service.env.monotonic() - self._started, 3)
        self._service.record({"type": "command_stop_finished", "command_id": self.command_id,
                              "status": result["status"], "exit_code": result.get("exit_code"),
                              "signal": result.get("signal"), "reason": result.get("reason"),
                              "residual": result.get("residual"), "elapsed_seconds": elapsed,
                              "repeat_requests": self._entry.repeats})
        self.stage = "done"
        self.result = result
        self.done.set()


class CommandStopService:
    """The registry of stoppable commands and the episodes that stop them."""

    def __init__(self, *, journal: Callable[[dict[str, Any]], Any], policy: StopPolicy = StopPolicy(),
                 env: StopEnvironment | None = None):
        self._journal, self.policy = journal, policy
        self.env = env or LinuxStopEnvironment()
        self._lock = threading.Lock()
        self._live: dict[str, _Entry] = {}
        self._ended: dict[str, dict[str, Any]] = {}
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
            self._ended[command_id] = {"exit_code": result.get("exit_code"), "signal": result.get("signal"),
                                       "ended_at": _now()}
            while len(self._ended) > ENDED_KEPT:
                del self._ended[next(iter(self._ended))]
        if entry is not None:
            entry.final = dict(result)
            entry.ended.set()

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
        """Stop one command and wait for the final state (at most ``policy.call_wait``); never raises."""
        base = {"command_id": command_id, "requested_by": requested_by}
        if self.closed.is_set():
            return self._refused(base, "refused", "backend_shutdown")
        with self._lock:
            entry = self._live.get(command_id)
            ended = dict(self._ended[command_id]) if entry is None and command_id in self._ended else None
            joined = entry is not None and entry.episode is not None
            if entry is not None and not joined:
                entry.episode = StopEpisode(self, entry, requested_by)
            elif joined:
                entry.repeats += 1
            episode = entry.episode if entry is not None else None
        if entry is None:
            if ended is None:
                return self._refused(base, "not_found", "unknown_command")
            result = {"status": "already_exited", **base, **ended}
            return self._refused(result, "already_exited", "command_ended")
        if not joined:
            target = entry.target
            self.record({"type": "command_stop_requested", "command_id": command_id, "requested_by": requested_by,
                         "kind": target.kind, "owner": target.owner, "task_id": target.task_id,
                         "task_status": None, "reason": reason})
            episode.start()
        if not episode.done.wait(self.policy.call_wait):
            return {"status": "stopping", **base, "stop": episode.summary()}
        return {**(episode.result or {}), **({"repeat": True} if joined else {})}

    def _refused(self, result: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
        result = {**result, "status": status}
        if status != "already_exited":
            result["reason"] = reason
        self.record({"type": "command_stop_refused", "command_id": result["command_id"],
                     "requested_by": result["requested_by"], "status": status, "reason": reason})
        return result
