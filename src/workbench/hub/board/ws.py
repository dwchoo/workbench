"""The Board WebSocket terminals, protocol v2 (web-board-ux SPEC s4 "WebSocket v2: terminal마다 공유 연결",
WBX-D54/D55, AC-32/33).

``GET /ws/board/{terminal_id}``: one WebSocket per terminal, and up to :data:`MAX_CLIENTS` of them share it: every
client gets the same replay and live output, every client's input reaches the PTY in the order it arrived at the Hub,
and the window size follows the client that last typed or resized (a new client sets it only while it is alone).
Each connection keeps its own send queue, backpressure, heartbeat and login session check.

Frames: control messages are text JSON; data is binary, the bare payload both ways (input bytes / output bytes).
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
import sys
from typing import Any, Callable

from aiohttp import WSMsgType, web

from ..web import middleware
from . import panes as pane_relay
from .set import INPUT_HIGH_WATER, INPUT_LOW_WATER, MAX_SIZE, MIN_SIZE

PROTOCOL_VERSION = 2
REPLAY_CHUNK = 64 * 1024

HELLO_TIMEOUT = 5.0
SESSION_CHECK = 5.0  # an open terminal re-verifies its login session this often
MAX_CONNECTIONS = 128  # open /ws/board/* sockets in all (more: 503 before the upgrade)
MAX_PENDING = 8  # sockets of one login session before their hello (more: 429 before the upgrade)
MAX_CLIENTS = 8  # attached sockets per terminal (WBX-D54; the 9th: ``refused`` + 4429)
HEARTBEAT, MISSES, SLACK = 10.0, 2, 1.0  # the client pings every 10 s; 2 x 10 s + 1 s of silence ends it
SEND_QUEUE_LIMIT = 4 << 20  # queued output bytes per connection
REPLY_BURST, REPLY_RATE = 64, 32.0  # pong/error replies: a burst, then this many per second (more are dropped)
CLOSE_WAIT = 3.0  # a close handshake's limit (aiohttp's own ``timeout``)
CLOSE_DEADLINE = CLOSE_WAIT + 1.0  # a queued close not done by then aborts the TCP connection
CONTROL_LIMIT = INPUT_LIMIT = 64 * 1024  # SPEC 입력 한도: a WebSocket input frame (bigger: dropped + ``error``)
MESSAGE_LIMIT = 1 << 20  # aiohttp closes the connection (1009) on a message beyond this

# Close codes v2 (board-state.js reads them): 1000 release, 1001 Hub stopping (the listener), 1009 too big,
# 4002 heartbeat timeout, 4003 send queue over its limit, 4400 bad or missing hello, 4401 the login session ended
# (logout, password change, expiry: log in again), 4403 hello csrf mismatch, 4404 the terminal was closed or deleted,
# 4429 the terminal has its 8 clients. (4001 and the takeover of v1 are gone.)
CLOSE_RELEASED = 1000
CLOSE_TIMEOUT = 4002
CLOSE_OVERFLOW = 4003
CLOSE_BAD_HELLO = 4400
CLOSE_SESSION = 4401
CLOSE_CSRF = 4403
CLOSE_GONE = 4404
CLOSE_TOO_MANY = 4429


CLOSED_TYPES = frozenset({WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR})


def _log(text: str) -> None:
    print(f"[workbench] board ws: {text}", file=sys.stderr, flush=True)


def origin_exact(request: web.Request) -> bool:
    """Exactly one ``Origin`` header naming this origin (``<scheme>://<Host>``); no Referer fallback."""
    origins = request.headers.getall("Origin", [])
    if len(origins) != 1:
        return False
    expected = middleware.expected_origin(request)
    return expected is not None and middleware.origin_of_header(origins[0]) == expected


class BadHello(ValueError):
    pass


def _is_int(value: object, expected: int) -> bool:
    """``value`` is the integer ``expected`` (JSON ``true`` and ``2.0`` are not)."""
    return type(value) is int and value == expected


def _size(value: object) -> tuple[int, int]:
    """A hello size ``[rows, cols]`` of integers, clamped into 1..500 (a too small or too big window still attaches);
    anything else is a bad hello. (A ``resize`` out of range is ``bad_resize`` instead.)"""
    if not isinstance(value, list) or len(value) != 2 or not all(type(item) is int for item in value):
        raise BadHello("size")
    rows, cols = (min(max(item, MIN_SIZE), MAX_SIZE) for item in value)
    return rows, cols


def parse_hello(message) -> dict:
    """The validated v2 hello: ``csrf`` (any value; compared later) and ``size`` (``(rows, cols)`` or None when the
    client gave none). :class:`BadHello` for anything else (also a binary first frame or another version)."""
    if message.type != WSMsgType.TEXT or len(message.data) > CONTROL_LIMIT:
        raise BadHello("not a text hello")
    try:
        hello = json.loads(message.data)
    except ValueError:
        raise BadHello("json") from None
    if not isinstance(hello, dict) or hello.get("t") != "hello" or not _is_int(hello.get("v"), PROTOCOL_VERSION):
        raise BadHello("type")
    size = hello.get("size")
    return {"csrf": hello.get("csrf"), "size": None if size is None else _size(size)}


Audit = Callable[[str, "str | None", dict], None]  # (event, client address, detail); blocking (SQLite)


def audit_to(services) -> Audit:
    """``terminal_*`` rows in ``hub.sqlite3.audit`` (actor ``owner``); never terminal content."""
    def record(event: str, address: str | None, detail: dict) -> None:
        services.audit.record(event, "owner", address, detail, now=float(services.clock()))
    return record


class _Stopper:
    """What ``WEBSOCKETS`` holds for a Board connection: the listener's ``close(code=, message=)``."""

    def __init__(self, connection: "Connection"):
        self.connection = connection

    async def close(self, *, code: int, message: bytes = b"") -> None:
        connection = self.connection
        connection.close(code, message.decode("utf-8", "replace"), drop_queue=True)
        try:
            await asyncio.wait_for(connection.finished.wait(), CLOSE_DEADLINE + CLOSE_WAIT)
        except asyncio.TimeoutError:
            pass


class Connection:
    """One WebSocket: an ordered send queue drained by its own task, so a PTY chunk never waits on a client.

    Every close is the handler's: :meth:`close` stops the read and the handler sends the queued messages, then
    the close frame, and waits for the client's answer (aiohttp would drop the TCP connection at once if another
    task closed it during a ``receive``)."""

    def __init__(self, ws: web.WebSocketResponse, request: web.Request, *, limit: int = SEND_QUEUE_LIMIT,
                 on_overflow: Callable[["Connection"], None] = lambda connection: None):
        self.ws = ws
        self.address = request.remote
        self.transport = request.transport
        self.deadline: asyncio.TimerHandle | None = None
        self.queue: deque = deque()
        self.queued = 0  # bytes waiting in ``queue`` (text and data alike)
        self.limit = limit
        self.on_overflow = on_overflow
        self.wakeup = asyncio.Event()
        self.sender: asyncio.Task | None = None
        self.unsubscribe: list[Callable[[], None]] = []
        self.closing: int | None = None  # the close code once a close is queued; nothing is queued after it
        self.reason = b""
        self.close_requested = asyncio.Event()
        self.flushed = asyncio.Event()  # the sender reached the close: everything before it was sent
        self.finished = asyncio.Event()  # the handler is done with this connection
        loop = asyncio.get_running_loop()
        self.last_seen = loop.time()  # any frame from the client (set again at attach)
        self.backpressure = False  # the terminal's PTY input queue is past the high water mark
        self.paused = False  # reads stopped until that queue drains: the client is paced by TCP, not silent
        self.tasks: list[asyncio.Task] = []  # the heartbeat watch
        self.stopper = _Stopper(self)
        services = request.app[middleware.SERVICES]
        self.token = request.get(middleware.SESSION_TOKEN)

        def session_valid() -> bool:  # blocking (SQLite, ``auth.json``): executor only
            return (self.token is not None
                    and services.sessions.verify(self.token, float(services.clock())) is not None)
        self.session_valid = session_valid
        self.replies, self.replies_at = float(REPLY_BURST), loop.time()
        self.terminal_id: str | None = None  # set at attach
        self.who: tuple[str | None, str] = (None, "")  # the terminal's kind and name (audit)
        self.client = 0  # this Hub run's connection number (audit)
        self.size: tuple[int, int] | None = None  # the size this client last told (hello, resize)
        self.end_event: str | None = None  # the audit row its end writes (release, timeout; else closed)
        self.pane: tuple[str, str] | None = None  # a project pane's (project_id, pane) (web-board-ux s5 트랙 E)
        self.on_close: Callable[["Connection"], None] = lambda connection: None

    def start(self) -> None:
        self.sender = asyncio.create_task(self._send_loop())

    def _enqueue(self, item: str | bytes) -> None:
        if self.closing is not None:
            return
        self.queue.append(item)
        self.queued += len(item)
        self.wakeup.set()
        if self.queued > self.limit:  # a client that does not read never holds the Hub's memory
            self.on_overflow(self)

    def send_json(self, message: dict) -> None:
        self._enqueue(json.dumps(message, separators=(",", ":")))

    def send_data(self, data: bytes) -> None:
        self._enqueue(data)

    def reply(self, message: dict) -> None:
        """A ``pong`` or ``error``: at most :data:`REPLY_BURST` at once and :data:`REPLY_RATE` per second after
        that; the rest are dropped (a client's flood of pings or junk never turns into a flood of replies)."""
        now = asyncio.get_running_loop().time()
        self.replies = min(float(REPLY_BURST), self.replies + (now - self.replies_at) * REPLY_RATE)
        self.replies_at = now
        if self.replies < 1:
            return
        self.replies -= 1
        self.send_json(message)

    def close(self, code: int, reason: str, *, drop_queue: bool = False, final: dict | None = None) -> None:
        """Close after what is queued (``drop_queue``: after only ``final``); later sends are ignored. A client
        that does not take the close within :data:`CLOSE_DEADLINE` (half-open, not reading) loses the TCP
        connection, so no connection outlives its close."""
        if self.closing is not None:
            return
        if drop_queue:
            self.queue.clear()
            self.queued = 0
        if final is not None:  # the last message is never refused, also past the limit
            text = json.dumps(final, separators=(",", ":"))
            self.queue.append(text)
            self.queued += len(text)
        self.closing, self.reason = code, reason.encode()
        self.queue.append((code, self.reason))
        self.wakeup.set()
        self.close_requested.set()
        self.deadline = asyncio.get_running_loop().call_later(CLOSE_DEADLINE, self.abort)
        self.on_close(self)  # detached from its terminal at once (clients count, audit), not after the handshake

    def abort(self) -> None:
        if self.transport is not None and not self.transport.is_closing():
            self.transport.abort()

    async def _send_loop(self) -> None:
        try:
            await self._send_all()
        except (ConnectionError, RuntimeError, OSError):
            self.abort()  # the transport is gone: the read loop ends and the handler cleans up

    async def _send_all(self) -> None:
        while True:
            while self.queue:
                item = self.queue.popleft()
                if isinstance(item, tuple):
                    self.flushed.set()
                    return
                self.queued -= len(item)
                if isinstance(item, str):
                    await self.ws.send_str(item)
                else:
                    await self.ws.send_bytes(item)
            self.wakeup.clear()
            await self.wakeup.wait()

    async def next_message(self, timeout: float | None = None):
        """The next client message; None once :meth:`close` was called; ``TimeoutError`` after ``timeout``."""
        receive = asyncio.ensure_future(self.ws.receive())
        stop = asyncio.ensure_future(self.close_requested.wait())
        try:
            done, _ = await asyncio.wait({receive, stop}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if receive.done():
            return receive.result()
        receive.cancel()
        await asyncio.gather(receive, return_exceptions=True)
        if not done:
            raise asyncio.TimeoutError
        return None

    async def finish(self) -> None:
        for unsubscribe in self.unsubscribe:
            unsubscribe()
        self.unsubscribe.clear()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            if self.closing is not None and not self.ws.closed:
                try:  # what was queued before the close goes first (a stuck client: the deadline aborts)
                    await asyncio.wait_for(self.flushed.wait(), CLOSE_WAIT)
                except asyncio.TimeoutError:
                    pass
                if self.sender is not None:
                    self.sender.cancel()
                    await asyncio.gather(self.sender, return_exceptions=True)
                try:  # the close frame, then the client's answer (aiohttp's ``timeout``)
                    await self.ws.close(code=self.closing, message=self.reason)
                except Exception:
                    pass
        finally:
            if self.sender is not None:
                self.sender.cancel()
                await asyncio.gather(self.sender, return_exceptions=True)
            if self.deadline is not None:
                self.deadline.cancel()
            self.finished.set()


class BoardTerminal:
    """Every Board WebSocket of one ``BoardSet``: the attached clients of each terminal, its size owner and the
    upgrade caps. Loop-thread only."""

    def __init__(self, board, *, audit: Audit | None = None, heartbeat: float = HEARTBEAT, misses: int = MISSES,
                 slack: float = SLACK, hello_timeout: float = HELLO_TIMEOUT, queue_limit: int = SEND_QUEUE_LIMIT,
                 session_check: float = SESSION_CHECK, max_connections: int = MAX_CONNECTIONS,
                 max_pending: int = MAX_PENDING, max_clients: int = MAX_CLIENTS, panes=None):
        self.board = board
        self.panes = panes  # the Hub's ``PaneHub`` (project pane WebSockets, s5 트랙 E), None: no pane ids
        self.audit = audit
        self._audit_tail: asyncio.Future | None = None
        self.heartbeat = heartbeat
        self.misses = misses
        self.slack = slack
        self.queue_limit = queue_limit
        self.session_check = session_check
        self.max_connections, self.max_pending, self.max_clients = max_connections, max_pending, max_clients
        self.open = 0  # upgrades admitted and not yet ended (also while ``prepare`` runs)
        self.pending: dict[str | None, int] = {}  # per login session: admitted sockets before their hello
        self.hello_timeout = hello_timeout
        self.serial = 0  # connection numbers of this Hub run (audit ``client``)
        self.connections: set[Connection] = set()  # every open WebSocket
        self.attached: dict[str, list[Connection]] = {}  # terminal id -> its clients, in attach order
        self.owner: dict[str, Connection] = {}  # terminal id -> the client whose size the PTY follows
        unwatch_board = board.watch(self._board_changed)
        unwatch_panes = panes.watch(self._pane_changed) if panes is not None else (lambda: None)

        def unwatch() -> None:
            unwatch_board()
            unwatch_panes()
        self.unwatch = unwatch

    # --- audit -----------------------------------------------------------------------------------

    def _who(self, terminal_id: str, connection: Connection, **more) -> dict:
        pane = pane_relay.parse_pane_id(terminal_id)
        if pane is not None:  # a project pane (SPEC s5 감사): its registry name now, else the last one seen
            name = self.panes.name(pane[0]) if self.panes is not None else None
            if name is not None:
                connection.who = ("pane", name)
            return {"pane_id": terminal_id, "project_id": pane[0], "project_name": connection.who[1],
                    "pane": pane[1], "client": connection.client,
                    "clients": len(self.attached.get(terminal_id, ())), **more}
        terminal = self.board.find(terminal_id)
        if terminal is not None:  # its name now; a closed or deleted terminal keeps the last one seen
            connection.who = (terminal.kind, terminal.name)
        kind, name = connection.who
        return {"terminal_id": terminal_id, "kind": kind, "name": name, "client": connection.client,
                "clients": len(self.attached.get(terminal_id, ())), **more}

    def _record(self, event: str, address: str | None, detail: dict) -> None:
        """Queue one audit row; rows are written in order in the executor and never delay the terminal. A project
        pane's rows are ``pane_*`` (SPEC s5 감사) instead of ``terminal_*``."""
        if self.audit is None:
            return
        if "pane_id" in detail and event.startswith("terminal_"):
            event = "pane_" + event[len("terminal_"):]
        tail = self._audit_tail

        async def write() -> None:
            if tail is not None:
                await asyncio.gather(tail, return_exceptions=True)
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.audit, event, address, detail)
            except Exception as exc:  # an audit problem never breaks the terminal
                _log(f"audit {event} failed: {type(exc).__name__}")
        self._audit_tail = asyncio.ensure_future(write())

    async def flush(self, timeout: float = CLOSE_DEADLINE + CLOSE_WAIT) -> None:
        """Hub stop, before ``hub.sqlite3`` closes: the connections end (their last rows are queued), then every
        queued audit row is written."""
        waiting = [connection.finished.wait() for connection in list(self.connections)]
        if waiting:
            await asyncio.wait([asyncio.ensure_future(item) for item in waiting], timeout=timeout)
        while self._audit_tail is not None and not self._audit_tail.done():
            await asyncio.gather(self._audit_tail, return_exceptions=True)  # a row queued meanwhile: again

    # --- the clients of a terminal ----------------------------------------------------------------------

    def clients(self, terminal_id: str) -> int:
        return len(self.attached.get(terminal_id, ()))

    def _broadcast(self, terminal_id: str, message: dict, *, skip: Connection | None = None) -> None:
        for connection in list(self.attached.get(terminal_id, ())):
            if connection is not skip:
                connection.send_json(message)

    def _detach(self, connection: Connection) -> None:
        """``connection`` leaves its terminal (once): the clients count, the others told, its end row. The PTY size
        stays (an owner that leaves keeps it until the next input or resize)."""
        terminal_id = connection.terminal_id
        clients = self.attached.get(terminal_id) if terminal_id is not None else None
        if clients is None or connection not in clients:
            return
        clients.remove(connection)
        if self.owner.get(terminal_id) is connection:
            del self.owner[terminal_id]
        if not clients:
            del self.attached[terminal_id]
        for unsubscribe in connection.unsubscribe:
            unsubscribe()
        connection.unsubscribe.clear()
        if connection.pane is not None:
            relay = self.panes.relays.get(connection.pane[0]) if self.panes is not None else None
            if relay is not None:
                relay.set_clients(connection.pane[1], len(clients))
        else:
            self.board.set_clients(terminal_id, len(clients))
        self._broadcast(terminal_id, {"t": "clients", "count": len(clients)})
        self._record(connection.end_event or "terminal_closed", connection.address,
                     self._who(terminal_id, connection))

    def _board_changed(self, terminal_id: str, event: str) -> None:
        """A state, name or ``busy`` change: ``state`` to its clients; a closed or deleted terminal: ``gone``, 4404."""
        if event == "gone":
            for connection in list(self.attached.get(terminal_id, ())):
                connection.close(CLOSE_GONE, "terminal gone", final={"t": "gone"})
            return
        view = self.board.view(terminal_id)
        if view is not None:
            self._broadcast(terminal_id, {"t": "state", "terminal": view})

    def _pane_changed(self, pane_id: str, event: str, value) -> None:
        """A project pane's relay event (SPEC s5 "화면 중계"): output bytes, ``reset``, ``state``, ``size``, a
        ``notice``; ``gone`` (registration removed) closes its WebSockets with 4404."""
        clients = list(self.attached.get(pane_id, ()))
        if event == "data":
            for connection in clients:
                connection.send_data(value)
        elif event == "reset":
            for connection in clients:
                connection.send_json({"t": "reset"})
        elif event == "state":
            for connection in clients:
                connection.send_json({"t": "state", "terminal": {**value, "clients": len(clients)}})
        elif event == "size":
            rows, cols = value
            for connection in clients:
                connection.send_json({"t": "size", "rows": rows, "cols": cols})
        elif event == "notice":
            for connection in clients:
                connection.send_json(value)
        elif event == "gone":
            for connection in clients:
                connection.close(CLOSE_GONE, "terminal gone", final={"t": "gone"})

    def _set_size(self, terminal_id: str, size: tuple[int, int]) -> None:
        if self.board.resize(terminal_id, *size):
            rows, cols = size
            self._broadcast(terminal_id, {"t": "size", "rows": rows, "cols": cols})

    # --- the upgrade ------------------------------------------------------------------------------------

    async def handle(self, request: web.Request, terminal_id: str) -> web.StreamResponse:
        # Caps, checked and taken before the first await (no two upgrades pass on the same free place): sockets in
        # all, and sockets of one login session still waiting for their hello.
        token = request.get(middleware.SESSION_TOKEN)
        if self.open >= self.max_connections:
            return middleware.refuse(request, 503, "too_many_connections")
        if self.pending.get(token, 0) >= self.max_pending:
            return middleware.refuse(request, 429, "too_many_pending")
        self.open += 1
        self.pending[token] = self.pending.get(token, 0) + 1
        greeted = False

        def greet() -> None:
            nonlocal greeted
            if not greeted:
                greeted = True
                self.pending[token] -= 1
                if not self.pending[token]:
                    del self.pending[token]
        try:
            return await self._handle(request, terminal_id, greet)
        finally:
            greet()
            self.open -= 1

    async def _handle(self, request: web.Request, terminal_id: str, greet: Callable[[], None]) -> web.StreamResponse:
        ws = web.WebSocketResponse(max_msg_size=MESSAGE_LIMIT, timeout=CLOSE_WAIT)
        await ws.prepare(request)
        from ..web.app import WEBSOCKETS  # (the app module registers this route)
        connection = Connection(ws, request, limit=self.queue_limit, on_overflow=self._overflow)
        self.serial += 1
        connection.client = self.serial
        self._who(terminal_id, connection)  # its kind and name for the audit, also if it goes before the hello
        connection.on_close = self._detach
        # The listener closes it with 1001 when the Hub stops, through the send queue, so this handler waits for
        # the close handshake before it ends the connection.
        request.app[WEBSOCKETS].add(connection.stopper)
        connection.start()
        self.connections.add(connection)
        try:
            await self._serve(connection, request, terminal_id, greet)
        finally:
            self._detach(connection)  # EOF, RST, 1001 or any other end
            try:
                await connection.finish()
            finally:
                self.connections.discard(connection)
        return ws

    def _deny(self, connection: Connection, terminal_id: str, reason: str) -> None:
        self._record("terminal_denied", connection.address, self._who(terminal_id, connection, reason=reason))

    async def _serve(self, connection: Connection, request: web.Request, terminal_id: str,
                     greet: Callable[[], None]) -> None:
        try:
            message = await connection.next_message(self.hello_timeout)
            if message is None or message.type in CLOSED_TYPES:  # gone before its hello (Hub stop, logout, tab)
                return
            greet()
            hello = parse_hello(message)
        except (asyncio.TimeoutError, BadHello):
            self._deny(connection, terminal_id, "hello")
            connection.close(CLOSE_BAD_HELLO, "hello")
            return
        session = middleware.session_of(request)
        if session is None or not middleware.csrf_matches(hello["csrf"], session.csrf):
            self._deny(connection, terminal_id, "csrf")
            connection.close(CLOSE_CSRF, "csrf")
            return
        pane = pane_relay.parse_pane_id(terminal_id)
        if pane is not None:
            gone = self.panes is None or await self.panes.entry(pane[0]) is None
        else:
            gone = self.board.find(terminal_id) is None
        if gone:  # closed or deleted (a project: unregistered) after the upgrade
            connection.close(CLOSE_GONE, "terminal gone", final={"t": "gone"})
            return
        if self.clients(terminal_id) >= self.max_clients:
            self._deny(connection, terminal_id, "too_many_clients")
            connection.close(CLOSE_TOO_MANY, "too many clients",
                             final={"t": "refused", "reason": "too_many_clients", "limit": self.max_clients})
            return
        if pane is not None:
            relay = self.panes.relay(pane[0])
            if relay is None:  # the Hub is stopping
                connection.close(CLOSE_GONE, "terminal gone", final={"t": "gone"})
                return
            if relay.state == "too_old":
                relay.poke()  # a new web hello checks an older backend again (SPEC s5 실패와 경계 동작)
            self._attach_pane(connection, terminal_id, pane, relay, hello)
        else:
            self._attach(connection, terminal_id, hello)
        connection.tasks.append(asyncio.create_task(self._watch(connection)))
        while connection.closing is None:
            message = await connection.next_message()
            if message is None or message.type in CLOSED_TYPES:
                return
            self._received(connection, message)
            if connection.backpressure:
                await self._paced(connection)

    def _attach(self, connection: Connection, terminal_id: str, hello: dict) -> None:
        """Synchronous (no await): the client set, ``attached``, the replay and the live output subscription happen
        in one step, so no output chunk falls between the replay and the stream (or comes twice)."""
        clients = self.attached.setdefault(terminal_id, [])
        clients.append(connection)
        connection.terminal_id = terminal_id
        connection.size = hello["size"]  # None: this client told no size (it never becomes the size owner)
        connection.last_seen = asyncio.get_running_loop().time()  # the heartbeat counts from the attach
        if len(clients) == 1 and connection.size is not None:  # alone: its window sets the PTY (a later client never
            self.owner[terminal_id] = connection            # resizes it just by attaching)
            self.board.resize(terminal_id, *connection.size)
        self.board.set_clients(terminal_id, len(clients))
        self._record("terminal_attach", connection.address, self._who(terminal_id, connection))
        rows, cols = self.board.size(terminal_id)
        connection.send_json({"t": "attached", "v": PROTOCOL_VERSION, "terminal": self.board.view(terminal_id),
                              "size": [rows, cols], "clients": len(clients), "heartbeat": self.heartbeat})
        replay = self.board.replay(terminal_id)
        for start in range(0, len(replay), REPLAY_CHUNK):
            connection.send_data(replay[start:start + REPLAY_CHUNK])
        connection.unsubscribe.append(self.board.subscribe(terminal_id, connection.send_data))
        self._broadcast(terminal_id, {"t": "clients", "count": len(clients)}, skip=connection)

    def _attach_pane(self, connection: Connection, pane_id: str, pane: tuple[str, str], relay, hello: dict) -> None:
        """A project pane's attach, like :meth:`_attach` in one step (no await): the client set, ``attached`` with
        the pane view, the Hub ring's replay, then live output (``_pane_changed``). The size rule is the Board's
        while no TUI is attached; with one, the backend's pane size is followed and nothing is resized."""
        clients = self.attached.setdefault(pane_id, [])
        clients.append(connection)
        connection.terminal_id = pane_id
        connection.pane = pane
        connection.size = hello["size"]
        connection.last_seen = asyncio.get_running_loop().time()
        name = pane[1]
        relay.set_clients(name, len(clients))
        if len(clients) == 1 and connection.size is not None:
            self.owner[pane_id] = connection
            relay.resize(name, connection.size)
        self._record("terminal_attach", connection.address, self._who(pane_id, connection))
        view = relay.view(name)
        connection.send_json({"t": "attached", "v": PROTOCOL_VERSION, "terminal": view, "size": view["size"],
                              "clients": len(clients), "heartbeat": self.heartbeat})
        replay = relay.rings[name].snapshot()
        for start in range(0, len(replay), REPLAY_CHUNK):
            connection.send_data(replay[start:start + REPLAY_CHUNK])
        self._broadcast(pane_id, {"t": "clients", "count": len(clients)}, skip=connection)

    def _pane_relay(self, connection: Connection):
        return self.panes.relays.get(connection.pane[0]) if self.panes is not None else None

    def _pane_input(self, connection: Connection, data: bytes) -> None:
        """One binary frame = one ``input`` request (never merged or split), in arrival order; a refusal goes back
        to this connection only. Past the relay's unanswered cap this socket's reads stop until answers come."""
        if len(data) > INPUT_LIMIT:
            connection.reply({"t": "error", "reason": "too_large"})
            return
        pane_id, pane = connection.terminal_id, connection.pane[1]
        relay = self._pane_relay(connection)

        def refused(reason: str) -> None:
            connection.send_json({"t": "input_refused", "reason": reason,
                                  "detail": pane_relay.refused_detail(reason)})
        if relay is None:
            refused("pane_unavailable")
            return
        if connection.size is not None and self.owner.get(pane_id) is not connection:
            self.owner[pane_id] = connection
        if connection.size is not None and self.owner.get(pane_id) is connection:
            relay.resize(pane, connection.size)  # sent only without a TUI and when the size differs
        relay.input(pane, data, refused)
        if relay.input_full():
            connection.backpressure = True

    def _pane_resize(self, connection: Connection, control: dict) -> None:
        rows, cols = control.get("rows"), control.get("cols")
        if not all(type(value) is int and MIN_SIZE <= value <= MAX_SIZE for value in (rows, cols)):
            connection.reply({"t": "error", "reason": "bad_resize"})
            return
        connection.size = (rows, cols)
        self.owner[connection.terminal_id] = connection
        relay = self._pane_relay(connection)
        if relay is not None:
            relay.resize(connection.pane[1], connection.size)

    async def _paced_pane(self, connection: Connection) -> None:
        """The relay has its unanswered cap of input in flight: stop reading this socket until answers come (the
        other connections' output goes on)."""
        connection.backpressure = False
        relay = self._pane_relay(connection)
        if relay is None or not relay.input_full():
            return
        connection.paused = True
        try:
            room = asyncio.ensure_future(relay.room.wait())
            stop = asyncio.ensure_future(connection.close_requested.wait())
            try:
                await asyncio.wait({room, stop}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (room, stop):
                    task.cancel()
                await asyncio.gather(room, stop, return_exceptions=True)
        finally:
            connection.paused = False
            connection.last_seen = asyncio.get_running_loop().time()

    async def _paced(self, connection: Connection) -> None:
        """A paste larger than the program reads at once: stop reading this socket (TCP then holds the client back)
        until the terminal's queued input drains; the other clients' input goes on. A close ends the wait at once;
        the heartbeat watch counts the pause as activity."""
        if connection.pane is not None:
            await self._paced_pane(connection)
            return
        connection.backpressure = False
        connection.paused = True
        terminal_id = connection.terminal_id
        try:
            if self.board.input_backlog(terminal_id) <= INPUT_LOW_WATER:
                return
            drained = asyncio.ensure_future(self.board.input_drained(terminal_id))  # low water, or the process ended
            stop = asyncio.ensure_future(connection.close_requested.wait())
            try:
                await asyncio.wait({drained, stop}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (drained, stop):
                    task.cancel()
                await asyncio.gather(drained, stop, return_exceptions=True)
        finally:
            connection.paused = False
            connection.last_seen = asyncio.get_running_loop().time()

    def _overflow(self, connection: Connection) -> None:
        connection.close(CLOSE_OVERFLOW, "send queue full", drop_queue=True)

    async def _watch(self, connection: Connection) -> None:
        """This connection's silence beyond ``misses x heartbeat + slack`` ends it alone (4002); a half-open TCP
        connection (no EOF) ends here. Its login session is checked every ``session_check`` seconds (4401)."""
        loop = asyncio.get_running_loop()
        limit = self.misses * self.heartbeat + self.slack
        next_check = loop.time() + self.session_check
        while connection.closing is None:
            now = loop.time()
            seen = now if connection.paused else connection.last_seen  # a paced paste is not silence
            remaining = seen + limit - now
            if remaining <= 0:
                connection.end_event = "terminal_timeout"
                connection.close(CLOSE_TIMEOUT, "heartbeat timeout", drop_queue=True)
                return
            if now >= next_check:
                next_check = now + self.session_check
                if not await loop.run_in_executor(None, connection.session_valid):
                    self._end_session(connection)
                    return
                continue
            await asyncio.sleep(min(remaining, next_check - now))

    def _end_session(self, connection: Connection) -> None:
        """The login behind ``connection`` ended (logout, password change, ``wb hub passwd``, 24 h expiry)."""
        connection.close(CLOSE_SESSION, "session ended", drop_queue=True)

    def sessions_ended(self, token: str | None) -> None:
        """A web logout (``token``) or a password change (None: every session): their sockets end now."""
        for connection in list(self.connections):
            if token is None or connection.token == token:
                self._end_session(connection)

    # --- client frames ------------------------------------------------------------------------------------

    def _received(self, connection: Connection, message) -> None:
        connection.last_seen = asyncio.get_running_loop().time()
        if connection.closing is not None:
            return
        if message.type == WSMsgType.BINARY:
            if connection.pane is not None:
                self._pane_input(connection, message.data)
            else:
                self._input(connection, message.data)
        elif message.type == WSMsgType.TEXT:
            self._control(connection, message.data)

    def _control(self, connection: Connection, text: str) -> None:
        if len(text) > CONTROL_LIMIT:
            connection.reply({"t": "error", "reason": "too_large"})
            return
        try:
            control = json.loads(text)
        except ValueError:
            control = None
        if not isinstance(control, dict) or not isinstance(control.get("t"), str):
            connection.reply({"t": "error", "reason": "bad_message"})
            return
        kind = control["t"]
        if kind == "resize":
            if connection.pane is not None:
                self._pane_resize(connection, control)
            else:
                self._resize(connection, control)
        elif kind == "ping":
            number = control.get("n")
            connection.reply({"t": "pong", "n": number if type(number) is int else None})
        elif kind == "release":
            connection.end_event = "terminal_release"
            connection.close(CLOSE_RELEASED, "released")
        else:
            connection.reply({"t": "error", "reason": "bad_message"})

    def _resize(self, connection: Connection, control: dict) -> None:
        """This client becomes the size owner and the PTY takes its size (``size`` to every client on a change)."""
        rows, cols = control.get("rows"), control.get("cols")
        if not all(type(value) is int and MIN_SIZE <= value <= MAX_SIZE for value in (rows, cols)):
            connection.reply({"t": "error", "reason": "bad_resize"})
            return
        connection.size = (rows, cols)
        self.owner[connection.terminal_id] = connection
        self._set_size(connection.terminal_id, connection.size)

    def _input(self, connection: Connection, data: bytes) -> None:
        """Input reaches the PTY in arrival order; a client that types becomes the size owner first (its size before
        its bytes)."""
        if len(data) > INPUT_LIMIT:
            connection.reply({"t": "error", "reason": "too_large"})
            return
        terminal_id = connection.terminal_id
        if connection.size is not None and self.owner.get(terminal_id) is not connection:
            self.owner[terminal_id] = connection  # only a client that announced a size (hello, resize) owns it
            self._set_size(terminal_id, connection.size)
        if not self.board.write(terminal_id, data):
            connection.reply({"t": "error", "reason": "input_refused"})
        elif self.board.input_backlog(terminal_id) > INPUT_HIGH_WATER:
            connection.backpressure = True  # the read loop waits for the PTY before this client's next frame


def routes(terminal: Callable[[], BoardTerminal | None]) -> Callable[[web.Application], None]:
    """``GET /ws/board/{terminal_id}`` over ``terminal()`` (None: no Board, 503). The session middleware answers 401
    before the handler runs (an ``/ws/`` route is an API route); API keys never reach it. Order before the upgrade:
    Origin 403, no Board 503 ``board_unavailable``, an invalid registry 503 ``registry_invalid``, an unknown terminal
    404, then the socket caps (503 / 429)."""
    @middleware.api
    async def get_ws(request: web.Request) -> web.StreamResponse:
        if not origin_exact(request):
            return middleware.refuse(request, 403, "origin_mismatch")
        current = terminal()
        if current is None or current.board.closed:
            return middleware.refuse(request, 503, "board_unavailable")
        pane = pane_relay.parse_pane_id(request.match_info["terminal_id"])
        if pane is not None:  # a project pane (SPEC s5): a registered project only; the Board registry is not asked
            if current.panes is None or current.panes.stopped:
                return middleware.refuse(request, 503, "board_unavailable")
            if await current.panes.entry(pane[0]) is None:
                return middleware.refuse(request, 404, "unknown_terminal")
            return await current.handle(request, request.match_info["terminal_id"])
        if current.board.invalid is not None:
            return middleware.refuse(request, 503, "registry_invalid")
        terminal_id = request.match_info["terminal_id"]
        if current.board.find(terminal_id) is None:
            return middleware.refuse(request, 404, "unknown_terminal")
        return await current.handle(request, terminal_id)

    def sessions_ended(token: str | None) -> None:
        current = terminal()
        if current is not None:
            current.sessions_ended(token)

    async def flush(_app: web.Application) -> None:
        current = terminal()
        if current is not None:
            await current.flush()

    def register(app: web.Application) -> None:
        app.router.add_get("/ws/board/{terminal_id}", get_ws)
        app.on_cleanup.append(flush)  # the listener closes ``hub.sqlite3`` only after the runner cleanup
        listeners = app[middleware.SERVICES].session_listeners
        if sessions_ended not in listeners:  # the same services across a listener reopen
            listeners.append(sessions_ended)
    return register
