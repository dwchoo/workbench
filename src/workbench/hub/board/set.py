"""The Hub-owned Board terminals (web-board-ux SPEC s4 "트랙 D", WBX-D44..D58; ADR-0003).

Board agents (each its own OMP, home, memory and system key) and shells, each on its own PTY as a new session leader
and a direct child of the Hub (``os.openpty`` + ``fork``), never a descendant of a backend. The agents and the limits
are in the terminal registry (:mod:`.registry`); shells live only in this Hub run. Nothing starts with the Hub: an
agent starts when it is opened (the session API), a shell when it is made; everything ends with the Hub.

Ownership follows the backend's ``_OwnedSession`` rules (C-D45): signals reach only the unreaped child's own
group or pidfds that provably name a member of its session. Ending a channel is SIGHUP -> SIGTERM -> grace ->
SIGKILL of every pinned session member, then a survivor check.

Output is kept per terminal in a 1 MiB ring buffer for attach replay. Subscribers (the WebSockets) get every chunk;
the set never blocks on them. Starts, stops and deletes are serialised per terminal (``busy``); registry writes under
one lock, always before the memory state changes.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import errno
import os
import re
import select
import signal
import sys
import termios
import threading
import time
import warnings
from typing import Any, Callable

from workbench.backend.panes import _OwnedSession, _set_winsize, process_ref

from . import registry
from .launch import AgentSpec, BoardLaunchError, HomeRemovalRefused, remove_board_home

KINDS = ("agent", "shell")
RING_BYTES = 1 << 20
READ_CHUNK = 65536
INPUT_QUEUE_BYTES = 4 << 20  # a hard cap; the WebSocket's backpressure keeps a holder's input far below it
INPUT_HIGH_WATER = 1 << 20  # queued PTY input beyond this: the WebSocket stops reading the holder's frames ...
INPUT_LOW_WATER = 256 << 10  # ... until the program has taken the queue down to this
TERM_GRACE = 5.0
HUP_GRACE = 0.5
DEFAULT_SIZE = (30, 100)
MIN_SIZE, MAX_SIZE = 1, 500  # rows/cols of a terminal (SPEC s4 WebSocket v2: 1..500)
LIVE_STATES = frozenset({"starting", "running"})
ACTIVE_KIND = "board_agent_set"
AGENT_STARTABLE = frozenset({"not_started", "stopped", "exited", "failed", "isolation_failed"})
SHELL_STARTABLE = frozenset({"exited", "failed"})
VISIBLE_MAX = 40  # ``POST /api/board/open`` ``visible`` ids

Audit = Callable[[str, str, "str | None", dict], None]


def _log(text: str) -> None:
    print(f"[workbench] board: {text}", file=sys.stderr, flush=True)


# Python's termios has no IUTF8; Linux's c_iflag bit (asm-generic/termbits.h) is 0o40000.
IUTF8 = getattr(termios, "IUTF8", 0o40000 if sys.platform.startswith("linux") else 0)


def _set_iutf8(fd: int) -> None:
    """The browser terminal is UTF-8: the line discipline's erase (cooked mode, e.g. ``sh``'s ``read``) then
    removes a whole multi-byte character such as a Hangul syllable, not its last byte."""
    if not IUTF8:
        return
    try:
        attributes = termios.tcgetattr(fd)
        attributes[0] |= IUTF8
        termios.tcsetattr(fd, termios.TCSANOW, attributes)
    except termios.error:
        pass


ESC = 0x1B
ESCAPE_LOOKBACK = 4096  # how far before a cut a still-open escape sequence is looked for (longer strings: no)
_STRING_INTRODUCERS = frozenset(b"]P_^X")  # OSC, DCS, APC, PM, SOS: ended by ST (ESC \\) or, for OSC, BEL
# DEC private modes the page needs back after its reset: alternate screen, mouse reporting and encodings, focus
# events, bracketed paste, application cursor keys.
REPLAY_MODES = frozenset({1, 47, 1047, 1049, 1000, 1002, 1003, 1004, 1005, 1006, 1015, 2004})
_DEC_MODE = re.compile(rb"\x1b\[\?([0-9;]*)([hl])|\x1bc")


def _sequence_end(data: bytearray, start: int) -> int | None:
    """The index just past the escape sequence that starts with ESC at ``start``; None while it is incomplete."""
    size = len(data)
    if start + 1 >= size:
        return None
    kind = data[start + 1]
    if kind == 0x5B:  # CSI: parameters and intermediates (0x20-0x3F), then one final byte (0x40-0x7E)
        index = start + 2
        while index < size and 0x20 <= data[index] <= 0x3F:
            index += 1
        if index >= size:
            return None
        return index + 1 if 0x40 <= data[index] <= 0x7E else index
    if kind in _STRING_INTRODUCERS:
        index = start + 2
        while index < size:
            if data[index] == 0x07 and kind == 0x5D:
                return index + 1
            if data[index] == ESC:
                return index + 2 if index + 1 < size and data[index + 1] == 0x5C else index
            index += 1
        return None
    index = start + 1  # ESC, intermediates (0x20-0x2F), one final byte: ``ESC ( B``, ``ESC 7``, ``ESC c``
    while index < size and 0x20 <= data[index] <= 0x2F:
        index += 1
    return index + 1 if index < size else None


def _safe_cut(data: bytearray, cut: int) -> int:
    """``cut`` moved off the inside of an escape sequence (past its end, or back to its ESC while it is still
    incomplete) and then off the inside of a UTF-8 sequence (forward over 1-3 continuation bytes 10xxxxxx)."""
    escape = data.rfind(bytes([ESC]), max(0, cut - ESCAPE_LOOKBACK), cut)
    if escape >= 0:
        end = _sequence_end(data, escape)
        if end is None:
            return escape
        if end > cut:
            cut = end
    skip = 0
    while skip < 3 and cut + skip < len(data) and data[cut + skip] & 0xC0 == 0x80:
        skip += 1
    if skip and (cut + skip == len(data) or data[cut + skip] & 0xC0 != 0x80):
        cut += skip
    return cut


class Ring:
    """The newest ``limit`` bytes of a channel's output; ``total`` counts every byte ever appended.

    The page resets xterm before every replay, so the window never starts inside an escape or UTF-8 sequence
    (a Hangul syllable is 3 bytes) and :meth:`snapshot` first sets again the :data:`REPLAY_MODES` that bytes
    trimmed off had left on (an OMP TUI's alternate screen, mouse reporting, bracketed paste)."""

    def __init__(self, limit: int = RING_BYTES):
        self.limit = limit
        self.data = bytearray()
        self.total = 0
        self.modes: set[int] = set()  # REPLAY_MODES on at the start of ``data``

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        self.data += chunk
        if len(self.data) > self.limit:
            cut = _safe_cut(self.data, len(self.data) - self.limit)
            self._track(self.data[:cut])
            del self.data[:cut]

    def _track(self, trimmed: bytearray) -> None:
        for match in _DEC_MODE.finditer(trimmed):
            if match.group(2) is None:  # ESC c: a full reset
                self.modes.clear()
                continue
            for value in match.group(1).split(b";"):
                if value.isdigit() and int(value) in REPLAY_MODES:
                    (self.modes.add if match.group(2) == b"h" else self.modes.discard)(int(value))

    def snapshot(self) -> bytes:
        prefix = b"".join(b"\x1b[?%dh" % mode for mode in sorted(self.modes))
        return prefix + bytes(self.data)


class Channel(_OwnedSession):
    """One PTY session the Hub leads: the child is ``pid`` (session and group leader), ``master_fd`` is ours."""

    def __init__(self, name: str, argv: list[str], env: dict[str, str], cwd: str,
                 size: tuple[int, int] = DEFAULT_SIZE, ring: Ring | None = None):
        self.name = name
        rows, cols = size
        # The window size and IUTF8 are set on the PTY before the fork: a child that set its own size after the fork
        # could do so after a later resize from the loop (a hello size) and undo it (the WBX-D1 size race).
        master, slave = os.openpty()
        try:
            _set_winsize(slave, rows, cols)
            _set_iutf8(slave)
            with warnings.catch_warnings():
                # The child only sets its terminal/signals and execs (no import, no lock), like the backend's panes.
                warnings.simplefilter("ignore", DeprecationWarning)
                pid = os.fork()
        except BaseException:
            os.close(master)
            os.close(slave)
            raise
        if pid == 0:  # pragma: no cover - child side
            try:
                os.close(master)
                os.login_tty(slave)  # new session, the PTY as the controlling terminal and stdin/stdout/stderr
                os.chdir(cwd)
                signal.signal(signal.SIGPIPE, signal.SIG_DFL)
                signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
                signal.signal(signal.SIGHUP, signal.SIG_DFL)
                os.execvpe(argv[0], argv, env)
            except BaseException as exc:
                try:
                    os.write(2, f"workbench: cannot start the Board {name}: {exc}\n".encode("utf-8", "replace"))
                finally:
                    os._exit(127)
        os.close(slave)
        # The master is never inheritable: a later channel would otherwise keep this PTY's master open, so a Hub
        # SIGKILL would not hang up this channel's session.
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
        self._drain_waiters: list[asyncio.Future] = []  # :meth:`drained` callers
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
        self._wake_drained()
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
        self._wake_drained()
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
        if len(self._pending) <= INPUT_LOW_WATER:
            self._wake_drained()
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

    def backlog(self) -> int:
        """Input bytes queued for the PTY (the program has not read them yet)."""
        return len(self._pending)

    def drained(self) -> "asyncio.Future[None]":
        """Done once the queued input is at most :data:`INPUT_LOW_WATER` bytes or the channel has ended."""
        future = asyncio.get_running_loop().create_future()
        if (len(self._pending) <= INPUT_LOW_WATER or self.ending or self.master_fd < 0 or self._reaped
                or self._eof):
            future.set_result(None)
        else:
            self._drain_waiters.append(future)
        return future

    def _wake_drained(self) -> None:
        waiters, self._drain_waiters = self._drain_waiters, []
        for future in waiters:
            if not future.done():
                future.set_result(None)

    def close_fds(self) -> None:
        self._wake_drained()
        for name in ("master_fd", "pidfd"):
            fd = getattr(self, name)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, -1)


class BoardError(Exception):
    """A refused Board action (SPEC s4 session API codes); ``message`` is an optional human explanation."""

    def __init__(self, reason: str, message: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.message = message


@dataclass(eq=False)
class Terminal:
    """One Board terminal: an agent (in the registry) or a shell (this Hub run only), its current :class:`Channel`
    (replaced on restart), its output ring, subscribers and view."""

    id: str
    kind: str
    name: str
    home: str | None = None  # agent: its home relative to ``<hub state>``
    state: str = "not_started"
    channel: Channel | None = None
    pid: int | None = None
    started_at: float | None = None
    detail: str | None = None
    message: str | None = None  # human explanation of a failure (directory, fix); ``detail`` stays the code
    exit_status: int | None = None
    size: tuple[int, int] = DEFAULT_SIZE
    ring: Ring | None = None
    subscribers: list = field(default_factory=list)
    busy: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    key_id: str | None = None  # agent: this start's system key (WBX-D58)
    clients: int = 0  # WebSockets attached now (the WebSocket layer sets it)
    check: tuple | None = None  # agent: the start-up isolation check (task, cancel event)

    def view(self) -> dict[str, Any]:
        view = {"id": self.id, "kind": self.kind, "name": self.name, "state": self.state, "pid": self.pid,
                "started_at": self.started_at, "detail": self.detail, "exit_status": self.exit_status,
                "clients": self.clients, "busy": self.busy}
        if self.message:
            view["message"] = self.message
        return view


def _limit(value: object, bounds: tuple[int, int]) -> int:
    if type(value) is not int or not bounds[0] <= value <= bounds[1]:
        raise BoardError("limit_out_of_range")
    return value


class BoardSet:
    """The owner's Board terminals (WBX-D44/D45). Loop-thread only; blocking work runs in the default executor.

    Agents come from the registry (``<hub state>/board-agents/registry.json``); a registry that cannot be used leaves
    the whole Board ``registry_invalid`` (nothing starts, the file is never written). Watchers get
    ``(terminal_id, "state" | "gone")``.
    """

    def __init__(self, launcher, *, state_dir: Path | str | None = None, audit: Audit | None = None,
                 grace: float = TERM_GRACE, clock: Callable[[], float] = time.time):
        self.launcher = launcher
        self.state_dir = Path(state_dir if state_dir is not None else launcher.state_dir)
        self.audit = audit
        self.grace = grace
        self.clock = clock
        self.closed = False
        self.registry_lock = asyncio.Lock()
        self.agents: dict[str, Terminal] = {}
        self.shells: dict[str, Terminal] = {}
        self.shell_made = False  # this Hub run made a shell (the first-open rule, WBX-D56 (3))
        self.shell_ids: set[str] = set()  # never reused within the run
        self._tasks: set[asyncio.Task] = set()
        self.watchers: list[Callable[[str, str], None]] = []
        self.data: registry.Registry | None = None
        self.invalid: str | None = None
        self._purged = False
        self._purge_lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        try:
            self.data = registry.load_or_create(self.state_dir, now=self.clock)
        except registry.RegistryInvalid as exc:
            self.invalid = exc.message
            _log(f"registry invalid: {exc.message}")
            return
        except registry.RegistryWriteFailed as exc:
            path = registry.path_of(self.state_dir)
            self.invalid = f"Board registry {path} could not be written ({exc}). {registry.FIX}"
            _log(self.invalid)
            return
        for entry in self.data.agents:
            self.agents[entry.id] = Terminal(entry.id, "agent", entry.name, home=entry.home)

    # --- audit and events --------------------------------------------------------------------------

    async def _audit(self, event: str, actor: str, address: str | None, detail: dict) -> None:
        if self.audit is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.audit, event, actor, address, detail)
        except Exception as exc:  # an audit problem never blocks the set
            _log(f"audit {event} failed: {type(exc).__name__}")

    @staticmethod
    def _who(terminal: Terminal, **more) -> dict:
        return {"terminal_id": terminal.id, "kind": terminal.kind, "name": terminal.name, **more}

    def _event(self, terminal: Terminal, event: dict) -> None:
        """WBX-B12: an agent's start/stop line in its Board events log (diagnostics; never raises)."""
        record = getattr(self.launcher, "record_event", None)
        if record is not None and terminal.home is not None:
            try:
                record(event, self.state_dir / terminal.home)
            except Exception as exc:  # noqa: BLE001
                _log(f"board event failed: {exc!r}")

    def _changed(self, terminal: Terminal, event: str = "state") -> None:
        for callback in list(self.watchers):
            try:
                callback(terminal.id, event)
            except Exception as exc:
                _log(f"state watcher failed: {exc!r}")

    def watch(self, callback: Callable[[str, str], None]) -> Callable[[], None]:
        """``callback(terminal_id, "state" | "gone")`` after a state, name or ``busy`` change, or the terminal's
        removal; returns the unwatch."""
        self.watchers.append(callback)
        return lambda: callback in self.watchers and self.watchers.remove(callback)

    def _task(self, coroutine) -> asyncio.Task:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)

        def done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                exc = finished.exception()
                if not isinstance(exc, BoardError):
                    _log(f"board task failed: {exc!r}")
        task.add_done_callback(done)
        return task

    # --- lookups and views ------------------------------------------------------------------------

    def find(self, terminal_id: object) -> Terminal | None:
        if not isinstance(terminal_id, str):
            return None
        return self.agents.get(terminal_id) or self.shells.get(terminal_id)

    def terminals(self) -> list[Terminal]:
        return [*self.agents.values(), *self.shells.values()]

    def views(self) -> list[dict[str, Any]]:
        return [terminal.view() for terminal in self.terminals()]

    def view(self, terminal_id: str) -> dict[str, Any] | None:
        terminal = self.find(terminal_id)
        return terminal.view() if terminal is not None else None

    def board_view(self) -> dict[str, Any]:
        if self.invalid is not None:
            return {"state": "registry_invalid", "message": self.invalid}
        return {"state": "ok"}

    def options(self) -> dict[str, int]:
        return (self.data.options if self.data is not None else registry.Options()).as_dict()

    def status(self) -> dict[str, Any]:
        """hub_v1 ``status.board_agent`` (SPEC s4 "Hub 상태"): the registry state, the limits and every terminal."""
        return {**self.board_view(), "options": self.options(),
                "agents": [terminal.view() for terminal in self.agents.values()],
                "shells": [terminal.view() for terminal in self.shells.values()]}

    def active(self) -> list[dict[str, Any]]:
        """``Hub.active()`` while a terminal lives: id, kind, name, pid and state of the live ones (no connection
        count: attaching never changes a stop offer)."""
        live = [{"id": terminal.id, "kind": terminal.kind, "name": terminal.name, "pid": terminal.pid,
                 "state": terminal.state} for terminal in self.terminals() if terminal.state in LIVE_STATES]
        return [{"kind": ACTIVE_KIND, "terminals": live}] if live else []

    def dot(self) -> str:
        """The nav's Board dot: ``error`` (an agent failed, or the registry is invalid), ``on`` (an agent lives),
        else ``off``."""
        if self.invalid is not None:
            return "error"
        states = [terminal.state for terminal in self.agents.values()]
        if any(state in ("failed", "isolation_failed") for state in states):
            return "error"
        return "on" if any(state in LIVE_STATES for state in states) else "off"

    def counts(self) -> dict[str, int]:
        return {"agents": len(self.agents),
                "agents_running": sum(terminal.state in LIVE_STATES for terminal in self.agents.values()),
                "shells": len(self.shells),
                "clients": sum(terminal.clients for terminal in self.terminals())}

    def agent_for_key(self, key_id: str, key_name: str) -> tuple[str, str] | None:
        """The internal listener's check (WBX-D58): ``(agent id, name)`` when ``key_id`` is the current start's key
        of a starting/running agent named by ``key_name`` (``board-agent.<id>``), else None."""
        from ..auth import SYSTEM_KEY_PREFIX
        if not isinstance(key_name, str) or not key_name.startswith(SYSTEM_KEY_PREFIX):
            return None
        terminal = self.agents.get(key_name[len(SYSTEM_KEY_PREFIX):])
        if (terminal is None or terminal.state not in LIVE_STATES or terminal.channel is None
                or terminal.channel.ending or terminal.key_id is None or terminal.key_id != key_id):
            return None
        return terminal.id, terminal.name

    # --- guards -----------------------------------------------------------------------------------

    def _require(self) -> None:
        if self.closed:
            raise BoardError("board_unavailable")
        if self.invalid is not None:
            raise BoardError("registry_invalid", self.invalid)

    def get(self, terminal_id: object) -> Terminal:
        """The terminal, or the refusal: ``board_unavailable``, ``registry_invalid``, ``unknown_terminal``."""
        self._require()
        terminal = self.find(terminal_id)
        if terminal is None:
            raise BoardError("unknown_terminal")
        return terminal

    def _present(self, terminal: Terminal) -> bool:
        return self.find(terminal.id) is terminal

    def _claim(self, terminal: Terminal) -> None:
        if terminal.busy:
            raise BoardError("terminal_busy")
        terminal.busy = True
        self._changed(terminal)

    async def _run(self, terminal: Terminal, work: Callable[[], Any]) -> Any:
        """``work()`` under the terminal's lock, ``busy`` meanwhile (claimed by the caller, synchronously). A
        cancelled caller (a closed HTTP request) never leaves the terminal half done."""
        async def body():
            try:
                async with terminal.lock:
                    return await work()
            finally:
                terminal.busy = False
                if self._present(terminal):
                    self._changed(terminal)
        return await asyncio.shield(self._task(body()))

    async def _save(self, data: registry.Registry) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(None, registry.save, self.state_dir, data)
        except registry.RegistryWriteFailed as exc:
            _log(f"registry write failed: {exc}")
            raise BoardError("registry_write_failed") from None
        self.data = data

    # --- system keys ------------------------------------------------------------------------------

    async def purge_system_keys(self) -> None:
        """Every system key row gone once per Board (rows of an earlier Hub run, the pre-Track-D ``board-agent``),
        before this run's first agent key; retried while the key store is closed."""
        async with self._purge_lock:
            if self._purged:
                return
            purge = getattr(self.launcher, "purge_keys", None)
            if purge is None:
                self._purged = True
                return
            if any(terminal.key_id is not None for terminal in self.agents.values()):
                self._purged = True  # an agent already holds a key of this run: never remove it
                return
            self._purged = bool(await asyncio.get_running_loop().run_in_executor(None, purge))

    async def _drop_key(self, terminal: Terminal, key_id: str | None) -> None:
        """The system key row of one start of ``terminal`` (only that ``key_id``: a later start keeps its own)."""
        if key_id is None:
            return
        if terminal.key_id == key_id:
            terminal.key_id = None
        drop = getattr(self.launcher, "drop_key", None)
        if drop is not None:
            await asyncio.get_running_loop().run_in_executor(None, drop, terminal.id, key_id)

    # --- start --------------------------------------------------------------------------------------

    async def _start(self, terminal: Terminal, reason: str, actor: str, address: str | None) -> None:
        if terminal.kind == "agent":
            await self._start_agent(terminal, reason, actor, address)
        else:
            await self._start_shell(terminal, reason, actor, address)

    def _failed(self, terminal: Terminal, detail: str, message: str | None = None) -> None:
        terminal.state, terminal.detail, terminal.message = "failed", detail, message
        terminal.channel, terminal.pid = None, None
        self._changed(terminal)

    def _spawn(self, terminal: Terminal, argv: list[str], env: dict, cwd: str) -> Channel:
        channel = Channel(terminal.kind, argv, env, cwd, terminal.size)
        channel.on_exit = self._exited
        channel.on_output = lambda chunk, terminal=terminal: self._output(terminal, chunk)
        channel.start_io(asyncio.get_running_loop())
        terminal.channel, terminal.pid, terminal.started_at = channel, channel.pid, channel.started_at
        terminal.ring = channel.ring
        terminal.detail, terminal.message, terminal.exit_status = None, None, None
        return channel

    async def _start_shell(self, terminal: Terminal, reason: str, actor: str, address: str | None) -> None:
        try:
            started = self.launcher.prepare_shell()
            channel = self._spawn(terminal, started.argv, started.env, started.cwd)
        except BoardLaunchError as exc:
            self._failed(terminal, exc.reason, exc.message)
            return
        except OSError as exc:
            self._failed(terminal, f"spawn_failed:{exc.strerror}")
            return
        terminal.state = "running"
        self._changed(terminal)
        await self._audit("board_start", actor, address, self._who(terminal, pid=channel.pid, reason=reason))

    async def _start_agent(self, terminal: Terminal, reason: str, actor: str, address: str | None) -> None:
        terminal.state, terminal.detail, terminal.message = "starting", None, None
        self._changed(terminal)
        await self.purge_system_keys()
        spec = AgentSpec(terminal.id, terminal.name, self.state_dir / terminal.home)
        try:
            started = await self.launcher.prepare_agent(spec)
        except BoardLaunchError as exc:
            self._failed(terminal, exc.reason, exc.message)
            return
        except Exception as exc:  # noqa: BLE001 - never leave the agent "starting"
            _log(f"agent start failed: {exc!r}")
            self._failed(terminal, f"internal_error:{type(exc).__name__}")
            return
        key_id = getattr(started, "key_id", None) or None
        if self.closed:  # the Hub began to stop meanwhile: this start never runs
            terminal.key_id = key_id
            await self._drop_key(terminal, key_id)
            terminal.state = "stopped"
            self._changed(terminal)
            return
        try:
            channel = self._spawn(terminal, started.argv, started.env, started.cwd)
        except OSError as exc:
            terminal.key_id = key_id
            await self._drop_key(terminal, key_id)
            self._failed(terminal, f"spawn_failed:{exc.strerror}")
            return
        terminal.key_id = key_id
        await self._audit("board_start", actor, address, self._who(terminal, pid=channel.pid, reason=reason))
        cancel = threading.Event()
        verdict = asyncio.ensure_future(self.launcher.check_agent(started, cancel))  # the check thread itself
        task = self._task(self._check(terminal, channel, verdict, cancel))
        terminal.check = (task, cancel, verdict)

    async def _check(self, terminal: Terminal, channel: Channel, pending, cancel: threading.Event) -> None:
        try:
            verdict = await pending
        except Exception as exc:  # fail closed: an agent whose isolation is unproven does not keep running
            verdict = f"check_failed:{type(exc).__name__}"
        async with terminal.lock:  # never ends a channel a stop, restart or the Hub stop is ending
            if terminal.channel is not channel or cancel.is_set() or self.closed:
                return
            if verdict is None:
                if terminal.state == "starting":
                    terminal.state = "running"
                    self._changed(terminal)
                return
            _log(f"agent isolation check failed: {verdict}")
            await self._end(terminal, channel, "isolation_failed")
            terminal.state, terminal.detail, terminal.channel = "isolation_failed", verdict, None
            self._changed(terminal)
            await self._audit("board_stop", "hub", None, self._who(terminal, pid=channel.pid,
                                                                    reason="isolation_failed"))

    def _cancel_check(self, terminal: Terminal):
        """Cancel the pending isolation check; returns the check thread's future (None without one)."""
        if terminal.check is None:
            return None
        terminal.check[1].set()
        thread = terminal.check[2]
        terminal.check = None
        return thread

    async def settled(self) -> None:
        """Wait for the pending start-up isolation checks and background work (tests, the WebSocket)."""
        while True:
            tasks = [task for task in self._tasks if not task.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- exit / end -------------------------------------------------------------------------------

    def _exited(self, channel: Channel) -> None:
        for terminal in self.terminals():
            if terminal.channel is not channel:
                continue
            if terminal.kind == "agent" and not channel.ending:
                self._event(terminal, {"event": "exit", "status": channel.returncode})
                self._task(self._drop_key(terminal, terminal.key_id))
            if terminal.state in LIVE_STATES:
                terminal.state, terminal.exit_status = "exited", channel.returncode
                terminal.detail = f"exit_status:{channel.returncode}"
                self._changed(terminal)

    async def _end(self, terminal: Terminal, channel: Channel, reason: str | None = None) -> dict[str, Any]:
        live = channel.returncode is None  # not reaped yet: this end stops a running process
        key_id = terminal.key_id
        channel.begin_end()
        if terminal.channel is channel:
            terminal.key_id = None  # the internal listener refuses this start's key from now on, not after the end
        result = await asyncio.get_running_loop().run_in_executor(None, channel.terminate, self.grace)
        if terminal.kind == "agent":
            if live:
                self._event(terminal, {"event": "stop", "status": channel.returncode, "reason": reason})
            await self._drop_key(terminal, key_id)
        if result["survivors"]:
            _log(f"{terminal.kind} {terminal.id} session members left after the end: {result['survivors']}")
        if terminal.channel is channel:
            terminal.exit_status = channel.returncode
        return result

    async def _end_current(self, terminal: Terminal, reason: str, *, join_check: bool = False) -> int | None:
        """End the terminal's process if it has one (cancelling a pending check); returns its pid. ``join_check``
        (delete): also wait for the check thread, so a late check never writes into a home being removed."""
        thread = self._cancel_check(terminal)
        if join_check and thread is not None:
            await asyncio.wait([thread], timeout=self.grace + 10)
        channel, pid = terminal.channel, terminal.pid
        if channel is not None:
            await self._end(terminal, channel, reason)
            terminal.channel = None
        return pid

    # --- session API actions (SPEC s4 "Session API") ---------------------------------------------------

    async def open(self, visible: list, *, actor: str = "owner", address: str | None = None) -> list[dict]:
        """WBX-D56 (1)/(3): the first open of this Hub run makes ``shell-1`` (no shell made yet, none now), and each
        ``visible`` agent that has not started in this run starts. Returns every terminal view."""
        self._require()
        work = []
        if not self.shell_made and not self.shells:
            self.shell_made = True  # now: a second open at the same moment makes no second shell
            work.append(self._new_shell("first_open", actor, address))
        for terminal_id in visible:
            terminal = self.agents.get(terminal_id) if isinstance(terminal_id, str) else None
            if terminal is None or terminal.state != "not_started" or terminal.busy:
                continue
            self._claim(terminal)
            work.append(self._run(terminal, lambda terminal=terminal: self._start(terminal, "first_open", actor,
                                                                                   address)))
        for result in await asyncio.gather(*work, return_exceptions=True):
            if isinstance(result, BoardError) and result.reason not in ("limit_reached", "terminal_busy"):
                raise result
            if isinstance(result, BaseException) and not isinstance(result, BoardError):
                raise result
        return self.views()

    async def create_agent(self, *, actor: str = "owner", address: str | None = None) -> dict:
        """A new agent with the default name (registry first), then its start (WBX-D56 (5))."""
        self._require()
        async with self.registry_lock:
            self._require()
            if len(self.agents) >= self.data.options.agent_limit:
                raise BoardError("limit_reached")
            agent_id = await self._new_agent_id()
            name = registry.default_name("agent", [terminal.name for terminal in self.agents.values()])
            entry = registry.AgentEntry(agent_id, name, registry.agent_home(agent_id), float(self.clock()))
            await self._save(self.data.with_agents([*self.data.agents, entry]))
            terminal = self.agents[agent_id] = Terminal(agent_id, "agent", name, home=entry.home)
            self._claim(terminal)
        await self._audit("board_terminal_open", actor, address, self._who(terminal, reason="create"))
        await self._run(terminal, lambda: self._start(terminal, "create", actor, address))
        return terminal.view()

    async def _new_agent_id(self) -> str:
        taken = set(self.agents)
        free = getattr(self.launcher, "key_name_free", None)
        while True:
            agent_id = registry.new_id("agent", taken)
            taken.add(agent_id)
            if free is None or await asyncio.get_running_loop().run_in_executor(None, free, agent_id):
                return agent_id

    async def create_shell(self, *, actor: str = "owner", address: str | None = None) -> dict:
        self._require()
        return await self._new_shell("create", actor, address)

    async def _new_shell(self, reason: str, actor: str, address: str | None) -> dict:
        async with self.registry_lock:  # the default name against concurrent renames (see rename)
            if len(self.shells) >= self.data.options.shell_limit:
                raise BoardError("limit_reached")
            shell_id = registry.new_id("shell", self.shell_ids)
            self.shell_ids.add(shell_id)
            name = registry.default_name("shell", [terminal.name for terminal in self.shells.values()])
            terminal = self.shells[shell_id] = Terminal(shell_id, "shell", name)
            self.shell_made = True
            self._claim(terminal)
        await self._audit("board_terminal_open", actor, address, self._who(terminal, reason=reason))
        await self._run(terminal, lambda: self._start(terminal, reason, actor, address))
        return terminal.view()

    async def start(self, terminal_id: str, *, actor: str = "owner", address: str | None = None) -> dict:
        terminal = self.get(terminal_id)
        allowed = AGENT_STARTABLE if terminal.kind == "agent" else SHELL_STARTABLE
        if terminal.state not in allowed:
            raise BoardError("bad_state")
        self._claim(terminal)

        async def work():
            if terminal.state in allowed:
                await self._start(terminal, "owner", actor, address)
        await self._run(terminal, work)
        return terminal.view()

    async def stop_terminal(self, terminal_id: str, *, actor: str = "owner", address: str | None = None) -> dict:
        """An agent's stop (memory kept): ``stopped``; it starts again only when the owner starts it."""
        terminal = self.get(terminal_id)
        if terminal.kind != "agent":
            raise BoardError("wrong_kind")
        if terminal.state not in LIVE_STATES:
            raise BoardError("bad_state")
        self._claim(terminal)

        async def work():
            pid = await self._end_current(terminal, "owner")
            terminal.state, terminal.detail, terminal.message = "stopped", None, None
            self._changed(terminal)
            await self._audit("board_stop", actor, address, self._who(terminal, pid=pid, reason="owner"))
        await self._run(terminal, work)
        return terminal.view()

    async def restart(self, terminal_id: str, *, actor: str = "owner", address: str | None = None,
                      reason: str = "owner") -> dict:
        """End the terminal's whole session (SIGHUP -> SIGTERM -> grace -> SIGKILL) and start it again (an agent
        continues its previous OMP session)."""
        terminal = self.get(terminal_id)
        self._claim(terminal)

        async def work():
            pid = await self._end_current(terminal, f"restart:{reason}")
            await self._audit("board_restart", actor, address, self._who(terminal, pid=pid, reason=reason))
            if not self.closed:
                await self._start(terminal, "restart", actor, address)
        await self._run(terminal, work)
        return terminal.view()

    async def close(self, terminal_id: str, *, actor: str = "owner", address: str | None = None) -> None:
        """A shell's end and removal (``gone`` to its WebSockets)."""
        terminal = self.get(terminal_id)
        if terminal.kind != "shell":
            raise BoardError("wrong_kind")
        self._claim(terminal)

        async def work():
            await self._end_current(terminal, "close")
            self.shells.pop(terminal.id, None)
            terminal.subscribers.clear()
            self._changed(terminal, "gone")
            await self._audit("board_terminal_close", actor, address, self._who(terminal))
        await self._run(terminal, work)

    async def delete(self, terminal_id: str, *, actor: str = "owner", address: str | None = None) -> None:
        """An agent's stop, key row and home removal (no symlink followed, nothing outside ``<hub state>``), then its
        registry entry. A refused or partial removal leaves the entry (``failed``/``delete_failed``) to retry."""
        terminal = self.get(terminal_id)
        if terminal.kind != "agent":
            raise BoardError("wrong_kind")
        self._claim(terminal)

        async def work():
            pid = await self._end_current(terminal, "delete", join_check=True)
            if pid is not None and terminal.state in LIVE_STATES:
                await self._audit("board_stop", actor, address, self._who(terminal, pid=pid, reason="delete"))
            loop = asyncio.get_running_loop()
            home = self.state_dir / terminal.home
            try:
                await loop.run_in_executor(None, remove_board_home, self.state_dir, terminal.home)
            except (HomeRemovalRefused, OSError) as exc:
                text = str(exc) if isinstance(exc, HomeRemovalRefused) else f"{home}: {exc.strerror or exc}"
                terminal.state, terminal.detail = "failed", "delete_failed"
                terminal.message = (f"Board agent home not removed: {text}. Fix the directory (owner, mode, a "
                                    f"symlink in its place), then delete the agent again.")[:1024]
                self._changed(terminal)
                raise BoardError("delete_failed", terminal.message) from None
            async with self.registry_lock:
                try:
                    await self._save(self.data.with_agents(entry for entry in self.data.agents
                                                           if entry.id != terminal.id))
                except BoardError:
                    terminal.state, terminal.detail = "failed", "registry_write_failed"
                    self._changed(terminal)
                    raise
                self.agents.pop(terminal.id, None)
            terminal.subscribers.clear()
            self._changed(terminal, "gone")
            await self._audit("board_terminal_close", actor, address, self._who(terminal, memory_removed=True))
        await self._run(terminal, work)

    async def rename(self, terminal_id: str, name: object, *, actor: str = "owner",
                     address: str | None = None) -> dict:
        terminal = self.get(terminal_id)
        group = self.agents if terminal.kind == "agent" else self.shells
        # The check, the uniqueness and the save are one step under the registry lock: two renames (or a rename
        # and a create picking its default name) never both take a name (WBX-D1 verifier finding 1).
        async with self.registry_lock:
            self._require()
            if not self._present(terminal):
                raise BoardError("unknown_terminal")
            if terminal.busy:
                raise BoardError("terminal_busy")
            try:
                new = registry.check_name(name, [other.name for other in group.values() if other is not terminal])
            except registry.NameRefused as exc:
                raise BoardError(exc.reason) from None
            old = terminal.name
            if new == old:
                return terminal.view()
            if terminal.kind == "agent":
                await self._save(self.data.with_agents(
                    registry.AgentEntry(entry.id, new, entry.home, entry.created_at) if entry.id == terminal.id
                    else entry for entry in self.data.agents))
            terminal.name = new
        self._changed(terminal)
        await self._audit("board_rename", actor, address, {"terminal_id": terminal.id, "kind": terminal.kind,
                                                            "old": old, "new": new})
        return terminal.view()

    async def set_options(self, *, agent_limit: object = None, shell_limit: object = None, actor: str = "owner",
                          address: str | None = None) -> dict:
        """Store the limits (WBX-D46): agents 1..8, shells 1..32, never below the current count (WBX-D56 (4))."""
        self._require()
        async with self.registry_lock:
            self._require()
            before = self.data.options
            agents = before.agent_limit if agent_limit is None else _limit(agent_limit, registry.AGENT_RANGE)
            shells = before.shell_limit if shell_limit is None else _limit(shell_limit, registry.SHELL_RANGE)
            if agents < len(self.agents) or shells < len(self.shells):
                raise BoardError("limit_below_count")
            after = registry.Options(agents, shells)
            if after != before:
                await self._save(registry.Registry(after, list(self.data.agents)))
        if after != before:
            await self._audit("board_options", actor, address, {"before": before.as_dict(), "after": after.as_dict()})
        return after.as_dict()

    async def stop(self, reason: str = "hub_stop") -> None:
        """End every terminal (Hub stop, both stop paths, no question), the pending checks and the internal
        listener. Idempotent; nothing starts afterwards (new actions are ``board_unavailable``)."""
        self.closed = True
        for terminal in self.terminals():
            self._cancel_check(terminal)

        async def end(terminal: Terminal) -> None:
            async with terminal.lock:
                channel = terminal.channel
                if channel is None:
                    return
                was_live = terminal.state in LIVE_STATES
                try:
                    await self._end(terminal, channel, reason)
                except Exception as exc:
                    _log(f"{terminal.kind} {terminal.id} end failed: {exc!r}")
                terminal.channel = None
                if was_live:
                    terminal.state = "stopped" if terminal.kind == "agent" else "exited"
                    terminal.detail = reason
                    self._changed(terminal)
                    await self._audit("board_stop", "hub", None, self._who(terminal, pid=channel.pid, reason=reason))
        await asyncio.gather(*(end(terminal) for terminal in self.terminals()))
        await self.settled()  # outside the locks: a check waiting for one sees ``closed`` and returns
        try:
            await self.launcher.close()
        except Exception as exc:
            _log(f"internal listener stop failed: {exc!r}")

    # --- terminal I/O (the WebSockets) ----------------------------------------------------------------

    def _output(self, terminal: Terminal, chunk: bytes) -> None:
        for callback in list(terminal.subscribers):
            try:
                callback(chunk)
            except Exception as exc:  # one bad subscriber never stops the PTY pump
                _log(f"{terminal.id} output subscriber failed: {exc!r}")

    def subscribe(self, terminal_id: str, callback: Callable[[bytes], None]) -> Callable[[], None]:
        """Every later output chunk of the terminal (across restarts); returns the unsubscribe."""
        subscribers = self.find(terminal_id).subscribers
        subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in subscribers:
                subscribers.remove(callback)
        return unsubscribe

    def replay(self, terminal_id: str) -> bytes:
        """The newest :data:`RING_BYTES` of the terminal's current (or last) process."""
        terminal = self.find(terminal_id)
        ring = terminal.ring if terminal is not None else None
        return ring.snapshot() if ring is not None else b""

    def output_total(self, terminal_id: str) -> int:
        terminal = self.find(terminal_id)
        return terminal.ring.total if terminal is not None and terminal.ring is not None else 0

    def write(self, terminal_id: str, data: bytes) -> bool:
        terminal = self.find(terminal_id)
        return terminal is not None and terminal.channel is not None and terminal.channel.write(data)

    def input_backlog(self, terminal_id: str) -> int:
        terminal = self.find(terminal_id)
        return terminal.channel.backlog() if terminal is not None and terminal.channel is not None else 0

    async def input_drained(self, terminal_id: str) -> None:
        """Return once the terminal's queued input is down to :data:`INPUT_LOW_WATER` (or it has no process any
        more: a restart or an exit never leaves a writer waiting)."""
        terminal = self.find(terminal_id)
        if terminal is not None and terminal.channel is not None:
            await terminal.channel.drained()

    def size(self, terminal_id: str) -> tuple[int, int]:
        terminal = self.find(terminal_id)
        return terminal.size if terminal is not None else DEFAULT_SIZE

    def resize(self, terminal_id: str, rows: int, cols: int) -> bool:
        """Set the terminal's window size (kept for its restarts); :class:`ValueError` outside 1..500. True when the
        size changed."""
        for value in (rows, cols):
            if type(value) is not int or not MIN_SIZE <= value <= MAX_SIZE:
                raise ValueError(f"terminal size must be integers in {MIN_SIZE}..{MAX_SIZE}")
        terminal = self.find(terminal_id)
        if terminal is None or terminal.size == (rows, cols):
            return False
        terminal.size = (rows, cols)
        if terminal.channel is not None:
            terminal.channel.resize(rows, cols)
        return True

    def set_clients(self, terminal_id: str, count: int) -> None:
        """The WebSocket layer's attached count (views only; no watcher call)."""
        terminal = self.find(terminal_id)
        if terminal is not None:
            terminal.clients = count
