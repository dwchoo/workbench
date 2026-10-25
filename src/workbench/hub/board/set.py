"""The Hub-owned Board agent set (Task Inbox SPEC "Board agent 세트", D-25/D-26, ADR-0003).

Two channels, ``agent`` (the Board agent OMP) and ``shell`` (the owner's shell), each on its own PTY as a new
session leader and a direct child of the Hub (``pty.fork``), never a descendant of a backend. The set starts on the
first Board connection (never at Hub start), keeps running with no browser attached, and ends with the Hub.

Ownership follows the backend's ``_OwnedSession`` rules (C-D45): signals reach only the unreaped child's own
group or pidfds that provably name a member of its session. Ending a channel is SIGHUP -> SIGTERM -> grace ->
SIGKILL of every pinned session member, then a survivor check.

Output is kept per channel in a 1 MiB ring buffer for attach replay. Subscribers (TI-17's WebSocket) get every
chunk; the set never blocks on them.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import errno
import os
import pty
import select
import signal
import sys
import threading
import time
import warnings
from typing import Any, Callable

from workbench.backend.panes import _OwnedSession, _set_winsize, process_ref

from .launch import BoardLaunchError

CHANNELS = ("agent", "shell")
RING_BYTES = 1 << 20
READ_CHUNK = 65536
INPUT_QUEUE_BYTES = 4 << 20  # one paste beyond this is refused, never split
TERM_GRACE = 5.0
HUP_GRACE = 0.5
DEFAULT_SIZE = (30, 100)
MIN_SIZE, MAX_SIZE = 1, 1000  # rows/cols accepted by resize (SPEC 1~1000; TIOCSWINSZ takes 16-bit values)
LIVE_STATES = frozenset({"starting", "running"})
LEASE_STATES = frozenset({"connected", "none"})
ACTIVE_KIND = "board_agent_set"

Audit = Callable[[str, str, "str | None", dict], None]


def _log(text: str) -> None:
    print(f"[workbench] board: {text}", file=sys.stderr, flush=True)


class Ring:
    """The newest ``limit`` bytes of a channel's output; ``total`` counts every byte ever appended."""

    def __init__(self, limit: int = RING_BYTES):
        self.limit = limit
        self.data = bytearray()
        self.total = 0

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        self.data += chunk
        if len(self.data) > self.limit:
            del self.data[:len(self.data) - self.limit]
            # The replay must not start inside a UTF-8 sequence (a Hangul syllable is 3 bytes): drop the cut
            # sequence's 1-3 continuation bytes (10xxxxxx) when a lead byte follows them.
            skip = 0
            while skip < 3 and skip < len(self.data) and self.data[skip] & 0xC0 == 0x80:
                skip += 1
            if skip and (skip == len(self.data) or self.data[skip] & 0xC0 != 0x80):
                del self.data[:skip]

    def snapshot(self) -> bytes:
        return bytes(self.data)


class Channel(_OwnedSession):
    """One PTY session the Hub leads: the child is ``pid`` (session and group leader), ``master_fd`` is ours."""

    def __init__(self, name: str, argv: list[str], env: dict[str, str], cwd: str,
                 size: tuple[int, int] = DEFAULT_SIZE, ring: Ring | None = None):
        self.name = name
        rows, cols = size
        with warnings.catch_warnings():
            # The child only sets its terminal/signals and execs (no import, no lock), like the backend's panes.
            warnings.simplefilter("ignore", DeprecationWarning)
            pid, master = pty.fork()
        if pid == 0:  # pragma: no cover - child side
            try:
                os.chdir(cwd)
                _set_winsize(0, rows, cols)
                signal.signal(signal.SIGPIPE, signal.SIG_DFL)
                signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
                signal.signal(signal.SIGHUP, signal.SIG_DFL)
                os.execvpe(argv[0], argv, env)
            except BaseException as exc:
                try:
                    os.write(2, f"workbench: cannot start the Board {name}: {exc}\n".encode("utf-8", "replace"))
                finally:
                    os._exit(127)
        # pty.fork's master is inheritable: a later channel would otherwise keep this PTY's master open, so a
        # Hub SIGKILL would not hang up this channel's session.
        os.set_inheritable(master, False)
        self.pid, self.master_fd = pid, master
        self.size = size
        self.started_at = time.time()
        self.returncode: int | None = None
        self._reaped = False
        self._pins: dict[int, tuple[int, bytes]] = {}
        self._unsignallable: dict[int, str] = {}
        self._eof = False
        self._pending = bytearray()
        self.ending = False  # set on the loop thread before its fds are closed in the executor
        self.ring = ring if ring is not None else Ring()
        os.set_blocking(master, False)
        self.ref = process_ref(f"board_{name}", pid)
        try:
            self.pidfd = os.pidfd_open(pid)
        except OSError:
            # Without a pidfd the exit cannot be observed: end the unreaped child (its pid is still ours).
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            os.waitpid(pid, 0)
            os.close(master)
            raise
        self.loop: asyncio.AbstractEventLoop | None = None
        self.on_output: Callable[[bytes], None] = lambda chunk: None
        self.on_exit: Callable[["Channel"], None] = lambda channel: None

    # --- loop wiring ---------------------------------------------------------------------------

    def start_io(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        loop.add_reader(self.master_fd, self._readable)
        loop.add_reader(self.pidfd, self._exited)

    def begin_end(self) -> None:
        """Loop thread, before :meth:`terminate` runs in the executor: no more writes or reads, so a write can
        never reach a closed (and possibly reused) fd number."""
        self.ending = True
        self._pending.clear()
        self.stop_io()

    def stop_io(self) -> None:
        if self.loop is None:
            return
        for fd in (self.master_fd, self.pidfd):
            if fd >= 0:
                self.loop.remove_reader(fd)
                self.loop.remove_writer(fd)
        self.loop = None

    def _read(self) -> bytes | None:
        """One chunk; ``b""`` at EOF/EIO (the session's last holder of the slave closed), None when nothing waits."""
        try:
            return os.read(self.master_fd, READ_CHUNK)
        except BlockingIOError:
            return None
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            return b""

    def _readable(self) -> None:
        data = self._read()
        if data:
            self.ring.append(data)
            self.on_output(data)
        elif data == b"":
            self._eof = True
            if self.loop is not None:
                self.loop.remove_reader(self.master_fd)

    def _exited(self) -> None:
        if self.loop is not None:
            self.loop.remove_reader(self.pidfd)
        while not self._eof:  # what the child wrote before it ended
            data = self._read()
            if not data:
                break
            self.ring.append(data)
            self.on_output(data)
        self.poll()
        self.on_exit(self)

    def write(self, data: bytes) -> bool:
        """Queue ``data`` for the PTY; False when the channel has ended or the queue is full."""
        if self.ending or self.master_fd < 0 or self._reaped or self._eof:
            return False
        if len(self._pending) + len(data) > INPUT_QUEUE_BYTES:
            return False
        self._pending += data
        self._flush()
        return True

    def _flush(self) -> None:
        if self.master_fd < 0:
            self._pending.clear()
            return
        try:
            written = os.write(self.master_fd, self._pending) if self._pending else 0
        except BlockingIOError:
            written = 0
        except OSError as exc:
            if exc.errno not in (errno.EIO, errno.EBADF):
                raise
            self._pending.clear()
            written = 0
        del self._pending[:written]
        if self.loop is not None:
            if self._pending:
                self.loop.add_writer(self.master_fd, self._flush)
            else:
                self.loop.remove_writer(self.master_fd)

    def resize(self, rows: int, cols: int) -> None:
        self.size = (rows, cols)
        if self.master_fd >= 0 and not self._reaped:
            _set_winsize(self.master_fd, rows, cols)

    # --- process -------------------------------------------------------------------------------

    def poll(self) -> int | None:
        if self._reaped:
            return self.returncode
        if not self._leader_exited():
            return None
        if self._reaped:
            return self.returncode
        self._pin_session()  # the unreaped zombie still proves the session: pin its members first
        _, status = os.waitpid(self.pid, 0)
        self._reaped = True
        self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    @property
    def alive(self) -> bool:
        return self.poll() is None

    def _drain_quietly(self, timeout: float) -> None:
        if self.master_fd < 0:
            time.sleep(timeout)
            return
        try:
            if select.select([self.master_fd], [], [], timeout)[0]:
                data = self._read()
                if data:
                    self.ring.append(data)
        except OSError:
            time.sleep(timeout)

    def _signal_group(self, signum: int) -> bool:
        if self._leader_exited() or not self._leader_proven():
            return False
        try:
            os.killpg(self.pid, signum)
        except OSError:
            return False
        return True

    def _wait_leader(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._leader_exited() and time.monotonic() < deadline:
            self._drain_quietly(0.05)

    def terminate(self, grace: float = TERM_GRACE) -> dict[str, Any]:
        """Blocking (executor, after :meth:`stop_io`): SIGHUP, SIGTERM, ``grace``, SIGKILL of the pinned session
        members, reap; returns the exit status and the members that did not end."""
        if not self._leader_exited() and self._leader_proven():
            self._pin_session()
            if self._signal_group(signal.SIGHUP):
                self._wait_leader(HUP_GRACE)
            if self._signal_group(signal.SIGTERM):
                self._wait_leader(grace)
        self._kill_owned_session()
        deadline = time.monotonic() + 2.0
        while self.poll() is None and time.monotonic() < deadline:
            self._drain_quietly(0.02)
        survivors = self._survivors()
        self._release_pins()
        self.close_fds()
        return {"channel": self.name, "pid": self.pid, "exit_status": self.returncode, "survivors": survivors}

    def close_fds(self) -> None:
        for name in ("master_fd", "pidfd"):
            fd = getattr(self, name)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, -1)


@dataclass
class Slot:
    """One channel's place in the set: the current :class:`Channel` (replaced on restart) and its view."""

    name: str
    state: str = "not_started"
    channel: Channel | None = None
    pid: int | None = None
    started_at: float | None = None
    detail: str | None = None
    exit_status: int | None = None
    size: tuple[int, int] = DEFAULT_SIZE
    ring: Ring | None = None
    subscribers: list = field(default_factory=list)

    def view(self) -> dict[str, Any]:
        return {"state": self.state, "pid": self.pid, "started_at": self.started_at, "detail": self.detail,
                "exit_status": self.exit_status}


class BoardSet:
    """The owner's one Board agent set (D-26). Loop-thread only; blocking work runs in the default executor."""

    def __init__(self, launcher, *, audit: Audit | None = None, grace: float = TERM_GRACE):
        self.launcher = launcher
        self.audit = audit
        self.grace = grace
        self.slots = {name: Slot(name) for name in CHANNELS}
        self.lock = asyncio.Lock()
        self.closed = False
        self._checks: dict[str, tuple[asyncio.Task, threading.Event]] = {}
        self._tasks: set[asyncio.Task] = set()  # every check task, also the ones a restart cancelled
        self.lease: dict[str, Any] = {"state": "none", "epoch": 0, "since": None}
        self.watchers: list[Callable[[], None]] = []

    # --- audit ---------------------------------------------------------------------------------

    async def _audit(self, event: str, actor: str, address: str | None, detail: dict) -> None:
        if self.audit is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.audit, event, actor, address, detail)
        except Exception as exc:  # an audit problem never blocks the set
            _log(f"audit {event} failed: {type(exc).__name__}")

    # --- start ---------------------------------------------------------------------------------

    async def ensure_started(self, *, actor: str = "owner", address: str | None = None) -> None:
        """First Board connection: start every channel that never started (exited ones wait for a restart)."""
        async with self.lock:
            if self.closed:
                return
            for slot in self.slots.values():
                if slot.state == "not_started":
                    await self._start(slot, "first_open", actor, address)

    async def _start(self, slot: Slot, reason: str, actor: str, address: str | None) -> None:
        if slot.name == "agent":
            await self._start_agent(slot, reason, actor, address)
        else:
            await self._start_shell(slot, reason, actor, address)

    async def _start_shell(self, slot: Slot, reason: str, actor: str, address: str | None) -> None:
        try:
            started = self.launcher.prepare_shell()
            channel = self._spawn(slot, started.argv, started.env, started.cwd)
        except BoardLaunchError as exc:
            slot.state, slot.detail, slot.channel, slot.pid = "failed", exc.reason, None, None
            self._changed()
            return
        except OSError as exc:
            slot.state, slot.detail, slot.channel, slot.pid = "failed", f"spawn_failed:{exc.strerror}", None, None
            self._changed()
            return
        slot.state = "running"
        self._changed()
        await self._audit("board_start", actor, address, {"channel": slot.name, "pid": channel.pid, "reason": reason})

    def _spawn(self, slot: Slot, argv: list[str], env: dict, cwd: str) -> Channel:
        channel = Channel(slot.name, argv, env, cwd, slot.size)
        channel.on_exit = self._exited
        channel.on_output = lambda chunk, slot=slot: self._output(slot, chunk)
        channel.start_io(asyncio.get_running_loop())
        slot.channel, slot.pid, slot.started_at, slot.ring = channel, channel.pid, channel.started_at, channel.ring
        slot.detail, slot.exit_status = None, None
        return channel

    async def _start_agent(self, slot: Slot, reason: str, actor: str, address: str | None) -> None:
        slot.state, slot.detail = "starting", None
        try:
            started = await self.launcher.prepare_agent()
        except BoardLaunchError as exc:
            slot.state, slot.detail, slot.channel, slot.pid = "failed", exc.reason, None, None
            self._changed()
            return
        except Exception as exc:  # noqa: BLE001 - never leave the slot "starting"
            _log(f"agent start failed: {exc!r}")
            slot.state, slot.detail, slot.channel, slot.pid = "failed", f"internal_error:{type(exc).__name__}", None, None
            self._changed()
            return
        try:
            channel = self._spawn(slot, started.argv, started.env, started.cwd)
        except OSError as exc:
            slot.state, slot.detail, slot.channel, slot.pid = "failed", f"spawn_failed:{exc.strerror}", None, None
            self._changed()
            return
        await self._audit("board_start", actor, address, {"channel": slot.name, "pid": channel.pid, "reason": reason})
        cancel = threading.Event()
        task = asyncio.create_task(self._check(slot, channel, started, cancel))
        self._checks[slot.name] = (task, cancel)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _check(self, slot: Slot, channel: Channel, started, cancel: threading.Event) -> None:
        try:
            verdict = await self.launcher.check_agent(started, cancel)
        except Exception as exc:  # fail closed: an agent whose isolation is unproven does not keep running
            verdict = f"check_failed:{type(exc).__name__}"
        async with self.lock:  # never ends a channel a restart or the Hub stop is ending
            if slot.channel is not channel or cancel.is_set() or self.closed:
                return
            if verdict is None:
                if slot.state == "starting":
                    slot.state = "running"
                    self._changed()
                return
            _log(f"agent isolation check failed: {verdict}")
            await self._end(slot, channel)
            slot.state, slot.detail, slot.channel = "isolation_failed", verdict, None
            self._changed()
            await self._audit("board_stop", "hub", None, {"channel": slot.name, "pid": channel.pid,
                                                          "reason": "isolation_failed"})

    async def settled(self) -> None:
        """Wait for the pending start-up isolation checks (tests, TI-17's attach)."""
        tasks = list(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- exit / end ----------------------------------------------------------------------------

    def _exited(self, channel: Channel) -> None:
        for slot in self.slots.values():
            if slot.channel is channel and slot.state in LIVE_STATES:
                slot.state, slot.exit_status = "exited", channel.returncode
                slot.detail = f"exit_status:{channel.returncode}"
                self._changed()

    def _changed(self) -> None:
        for callback in list(self.watchers):
            try:
                callback()
            except Exception as exc:
                _log(f"state watcher failed: {exc!r}")

    def watch(self, callback: Callable[[], None]) -> Callable[[], None]:
        """TI-17: ``callback()`` after any channel state change (read :meth:`status`); returns the unwatch."""
        self.watchers.append(callback)
        return lambda: callback in self.watchers and self.watchers.remove(callback)

    async def _end(self, slot: Slot, channel: Channel) -> dict[str, Any]:
        channel.begin_end()
        result = await asyncio.get_running_loop().run_in_executor(None, channel.terminate, self.grace)
        if result["survivors"]:
            _log(f"{slot.name} session members left after the end: {result['survivors']}")
        if slot.channel is channel:
            slot.exit_status = channel.returncode
        return result

    async def restart(self, name: str, *, actor: str = "owner", address: str | None = None,
                      reason: str = "owner") -> dict[str, Any]:
        """End channel ``name`` (whole session, SIGHUP -> SIGTERM -> grace -> SIGKILL) and start it again; the
        agent continues its previous OMP session. Returns the channel view."""
        if name not in self.slots:
            raise KeyError(name)
        # A cancelled caller (e.g. a closed HTTP request) never leaves the channel half ended.
        return await asyncio.shield(self._restart(name, actor, address, reason))

    async def _restart(self, name: str, actor: str, address: str | None, reason: str) -> dict[str, Any]:
        async with self.lock:
            slot = self.slots[name]
            if self.closed:
                return slot.view()
            pending = self._checks.pop(name, None)
            if pending is not None:
                pending[1].set()
            channel = slot.channel
            old_pid = slot.pid
            if channel is not None:
                await self._end(slot, channel)
                slot.channel = None
            await self._audit("board_restart", actor, address, {"channel": name, "pid": old_pid, "reason": reason})
            await self._start(slot, "restart", actor, address)
            return slot.view()

    async def stop(self, reason: str = "hub_stop") -> None:
        """End the set (Hub stop, both stop paths, no question): both channels together, the pending checks and
        the internal listener. Idempotent; the set never starts again."""
        async with self.lock:
            self.closed = True
            for _task, cancel in self._checks.values():
                cancel.set()

            async def end(slot: Slot) -> None:
                channel = slot.channel
                if channel is None:
                    return
                was_live = slot.state in LIVE_STATES
                try:
                    await self._end(slot, channel)
                except Exception as exc:
                    _log(f"{slot.name} end failed: {exc!r}")
                slot.channel = None
                if was_live:
                    slot.state, slot.detail = "stopped", reason
                    await self._audit("board_stop", "hub", None, {"channel": slot.name, "pid": channel.pid,
                                                                  "reason": reason})
            await asyncio.gather(*(end(slot) for slot in self.slots.values()))
            self._changed()
        await self.settled()  # outside the lock: a check waiting for it sees ``closed`` and returns
        try:
            await self.launcher.close()
        except Exception as exc:
            _log(f"internal listener stop failed: {exc!r}")

    # --- terminal I/O (TI-17's WebSocket) -----------------------------------------------------

    def _output(self, slot: Slot, chunk: bytes) -> None:
        for callback in list(slot.subscribers):
            try:
                callback(chunk)
            except Exception as exc:  # one bad subscriber never stops the PTY pump
                _log(f"{slot.name} output subscriber failed: {exc!r}")

    def subscribe(self, name: str, callback: Callable[[bytes], None]) -> Callable[[], None]:
        """Every later output chunk of channel ``name`` (across restarts); returns the unsubscribe."""
        subscribers = self.slots[name].subscribers
        subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in subscribers:
                subscribers.remove(callback)
        return unsubscribe

    def replay(self, name: str) -> bytes:
        """The newest :data:`RING_BYTES` of the current (or last) process of channel ``name``."""
        ring = self.slots[name].ring
        return ring.snapshot() if ring is not None else b""

    def output_total(self, name: str) -> int:
        ring = self.slots[name].ring
        return ring.total if ring is not None else 0

    def write(self, name: str, data: bytes) -> bool:
        channel = self.slots[name].channel
        return channel is not None and channel.write(data)

    def resize(self, name: str, rows: int, cols: int) -> None:
        """Set channel ``name``'s window size (kept for its restarts); :class:`ValueError` outside 1..500."""
        for value in (rows, cols):
            if type(value) is not int or not MIN_SIZE <= value <= MAX_SIZE:
                raise ValueError(f"terminal size must be integers in {MIN_SIZE}..{MAX_SIZE}")
        slot = self.slots[name]
        slot.size = (rows, cols)
        if slot.channel is not None:
            slot.channel.resize(rows, cols)

    # --- views ---------------------------------------------------------------------------------

    def set_lease(self, state: str, epoch: int) -> None:
        """TI-17: the WebSocket lease (``connected``/``none``) and its epoch; a change shows in ``active()``."""
        if state not in LEASE_STATES:
            raise ValueError(state)
        if (state, epoch) != (self.lease["state"], self.lease["epoch"]):
            self.lease = {"state": state, "epoch": epoch, "since": time.time()}

    def active(self) -> list[dict[str, Any]]:
        """``Hub.active()`` entry while a channel lives (SPEC "정지 연동"); any channel or lease change alters it,
        so a confirm against an older offer is ``stop_token_mismatch``."""
        if not any(slot.state in LIVE_STATES for slot in self.slots.values()):
            return []
        return [{"kind": ACTIVE_KIND, "lease": self.lease["state"],
                 "channels": {name: {"pid": slot.pid, "state": slot.state} for name, slot in self.slots.items()}}]

    def status(self) -> dict[str, Any] | None:
        """hub_v1 ``status.board_agent``; None until a channel was ever started."""
        if all(slot.state == "not_started" for slot in self.slots.values()):
            return None
        return {**{name: slot.view() for name, slot in self.slots.items()}, "lease": dict(self.lease)}
