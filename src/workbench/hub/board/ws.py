"""The Board WebSocket terminal (Task Inbox SPEC "WebSocket terminal 프로토콜", D-26, D-28, P2-AC-34·35).

``GET /ws/board`` carries both Board channels over one WebSocket. The set has one lease: the current connection.

Frames: control messages are text JSON; data is binary ``byte0 = channel (0 agent, 1 shell) | bytes1..4 = epoch
(uint32 BE) | payload`` both ways (server output carries the current epoch).
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
import secrets
import struct
import sys
from typing import Any, Callable

from aiohttp import WSMsgType, web

from ..web import middleware
from .set import MAX_SIZE, MIN_SIZE

PROTOCOL_VERSION = 1
CHANNEL_IDS = {"agent": 0, "shell": 1}
CHANNEL_NAMES = {value: key for key, value in CHANNEL_IDS.items()}
HEADER = struct.Struct(">BI")
REPLAY_CHUNK = 64 * 1024

HELLO_TIMEOUT = 5.0
SESSION_CHECK = 5.0  # an open terminal re-verifies its login session this often
MAX_CONNECTIONS = 8  # open /ws/board sockets in all (more: 503 before the upgrade)
MAX_PENDING = 2  # sockets of one login session before their hello (more: 429 before the upgrade)
HEARTBEAT, MISSES, SLACK = 10.0, 2, 1.0  # the client pings every 10 s; 2 x 10 s + 1 s of silence ends it
SEND_QUEUE_LIMIT = 4 << 20  # queued output bytes per connection
REPLY_BURST, REPLY_RATE = 64, 32.0  # pong/error replies: a burst, then this many per second (more are dropped)
CLOSE_WAIT = 3.0  # a close handshake's limit (aiohttp's own ``timeout``)
CLOSE_DEADLINE = CLOSE_WAIT + 1.0  # a queued close not done by then aborts the TCP connection
EPOCH_MAX = 0xFFFFFFFF
CONTROL_LIMIT = INPUT_LIMIT = 64 * 1024  # SPEC 입력 한도: a WebSocket input frame (bigger: dropped + ``error``)
MESSAGE_LIMIT = 1 << 20  # aiohttp closes the connection (1009) on a message beyond this

# Close codes (TI-18 reads them): 1000 release, 1001 Hub stopping (the listener), 4001 taken over / taken elsewhere,
# 4002 heartbeat timeout, 4003 send queue over its limit, 4400 bad or missing hello, 4401 the login session ended
# (logout, password change, expiry: log in again), 4403 hello csrf mismatch.
CLOSE_RELEASED = 1000
CLOSE_TAKEN = 4001
CLOSE_TIMEOUT = 4002
CLOSE_OVERFLOW = 4003
CLOSE_BAD_HELLO = 4400
CLOSE_SESSION = 4401
CLOSE_CSRF = 4403


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
    """``value`` is the integer ``expected`` (JSON ``true`` and ``1.0`` are not)."""
    return type(value) is int and value == expected


def _size(value: object) -> tuple[int, int]:
    """A hello size ``[rows, cols]`` of integers, clamped into 1..1000 (a too small or too big window still
    attaches); anything else is a bad hello. (A ``resize`` out of range is ``bad_resize`` instead.)"""
    if not isinstance(value, list) or len(value) != 2 or not all(type(item) is int for item in value):
        raise BadHello("sizes")
    rows, cols = (min(max(item, MIN_SIZE), MAX_SIZE) for item in value)
    return rows, cols


def parse_hello(message) -> dict:
    """The validated hello: ``csrf`` (any value; compared later), ``resume`` (None or ``{run, epoch}``) and
    ``sizes`` (``{channel: (rows, cols)}``). :class:`BadHello` for anything else (also a binary first frame)."""
    if message.type != WSMsgType.TEXT or len(message.data) > CONTROL_LIMIT:
        raise BadHello("not a text hello")
    try:
        hello = json.loads(message.data)
    except ValueError:
        raise BadHello("json") from None
    if not isinstance(hello, dict) or hello.get("t") != "hello" or not _is_int(hello.get("v"), PROTOCOL_VERSION):
        raise BadHello("type")
    resume = hello.get("resume")
    if resume is not None:
        if (not isinstance(resume, dict) or not isinstance(resume.get("run"), str)
                or type(resume.get("epoch")) is not int or not 0 <= resume["epoch"] <= EPOCH_MAX):
            raise BadHello("resume")
        resume = {"run": resume["run"], "epoch": resume["epoch"]}
    sizes = hello.get("sizes")
    if sizes is None:
        sizes = {}
    if not isinstance(sizes, dict) or not set(sizes) <= set(CHANNEL_IDS):
        raise BadHello("sizes")
    return {"csrf": hello.get("csrf"), "resume": resume,
            "sizes": {name: _size(value) for name, value in sizes.items()}}


Audit = Callable[[str, "str | None", dict], None]  # (event, client address, detail); blocking (SQLite)


def audit_to(services) -> Audit:
    """``terminal_*`` rows in ``hub.sqlite3.audit`` (actor ``owner``); epochs only, never terminal content."""
    def record(event: str, address: str | None, detail: dict) -> None:
        services.audit.record(event, "owner", address, detail, now=float(services.clock()))
    return record


def data_frame(channel: str, epoch: int, payload: bytes) -> bytes:
    return HEADER.pack(CHANNEL_IDS[channel], epoch) + payload


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
        self.tasks: list[asyncio.Task] = []  # the heartbeat watch
        self.stopper = _Stopper(self)
        services = request.app[middleware.SERVICES]
        self.token = request.get(middleware.SESSION_TOKEN)

        def session_valid() -> bool:  # blocking (SQLite, ``auth.json``): executor only
            return (self.token is not None
                    and services.sessions.verify(self.token, float(services.clock())) is not None)
        self.session_valid = session_valid
        self.replies, self.replies_at = float(REPLY_BURST), loop.time()

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
    """The Board set's one lease and every WebSocket connection to it. Loop-thread only."""

    def __init__(self, board, *, audit: Audit | None = None, heartbeat: float = HEARTBEAT, misses: int = MISSES,
                 slack: float = SLACK, hello_timeout: float = HELLO_TIMEOUT, queue_limit: int = SEND_QUEUE_LIMIT,
                 session_check: float = SESSION_CHECK, max_connections: int = MAX_CONNECTIONS,
                 max_pending: int = MAX_PENDING):
        self.board = board
        self.audit = audit
        self._audit_tail: asyncio.Future | None = None
        self.heartbeat = heartbeat
        self.misses = misses
        self.slack = slack
        self.queue_limit = queue_limit
        self.session_check = session_check
        self.max_connections, self.max_pending = max_connections, max_pending
        self.open = 0  # upgrades admitted and not yet ended (also while ``prepare`` runs)
        self.pending: dict[str | None, int] = {}  # per login session: admitted sockets before their hello
        self.hello_timeout = hello_timeout
        self.run = secrets.token_hex(8)  # this Hub run: a resume from another run never names this lease
        self.epoch = 0
        self.current: Connection | None = None
        self.dropped_inputs = 0  # input frames not written: another connection's, a stale epoch, refused
        self.connections: set[Connection] = set()  # every open WebSocket, the current one included

    def view(self) -> dict[str, Any]:
        """The Board page's terminal view (TI-18): the set's lease (as ``status.board_agent.lease``), the open
        WebSockets and how many input frames were dropped."""
        return {"lease": dict(self.board.lease), "connections": len(self.connections),
                "dropped_inputs": self.dropped_inputs}

    # --- audit -----------------------------------------------------------------------------------

    def _record(self, event: str, address: str | None, epoch: int, previous: int | None = None) -> None:
        """Queue one audit row; rows are written in order in the executor and never delay the lease."""
        if self.audit is None:
            return
        tail, detail = self._audit_tail, {"epoch": epoch, "previous_epoch": previous}

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

    # --- lease ------------------------------------------------------------------------------------

    def _release(self, connection: Connection, event: str) -> bool:
        """``connection`` stops being the lease (lease ``none``, epoch kept: the same tab may resume)."""
        if connection is not self.current:
            return False
        self.current = None
        self.board.set_lease("none", self.epoch)
        self._record(event, connection.address, self.epoch)
        return True

    async def handle(self, request: web.Request) -> web.StreamResponse:
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
            return await self._handle(request, greet)
        finally:
            greet()
            self.open -= 1

    async def _handle(self, request: web.Request, greet: Callable[[], None]) -> web.StreamResponse:
        ws = web.WebSocketResponse(max_msg_size=MESSAGE_LIMIT, timeout=CLOSE_WAIT)
        await ws.prepare(request)
        from ..web.app import WEBSOCKETS  # (the app module registers this route)
        connection = Connection(ws, request, limit=self.queue_limit, on_overflow=self._overflow)
        # The listener closes it with 1001 when the Hub stops, through the send queue, so this handler waits for
        # the close handshake before it ends the connection.
        request.app[WEBSOCKETS].add(connection.stopper)
        connection.start()
        self.connections.add(connection)
        try:
            await self._serve(connection, request, greet)
        finally:
            self._release(connection, "terminal_closed")  # EOF, RST, 1001 or any other end of the current one
            try:
                await connection.finish()
            finally:
                self.connections.discard(connection)
        return ws

    async def _serve(self, connection: Connection, request: web.Request, greet: Callable[[], None]) -> None:
        try:
            message = await connection.next_message(self.hello_timeout)
            if message is None or message.type in CLOSED_TYPES:  # gone before its hello (Hub stop, logout, tab)
                return
            greet()
            hello = parse_hello(message)
        except (asyncio.TimeoutError, BadHello):
            self._record("terminal_denied", connection.address, self.epoch)
            connection.close(CLOSE_BAD_HELLO, "hello")
            return
        session = middleware.session_of(request)
        if session is None or not middleware.csrf_matches(hello["csrf"], session.csrf):
            self._record("terminal_denied", connection.address, self.epoch)
            connection.close(CLOSE_CSRF, "csrf")
            return
        await self.board.ensure_started(actor="owner", address=request.remote)
        if connection.closing is not None:
            return
        if not self._may_attach(hello["resume"]):
            self._record("terminal_denied", connection.address, self.epoch)
            connection.close(CLOSE_TAKEN, "taken elsewhere", final={"t": "taken_elsewhere", "epoch": self.epoch})
            return
        self._attach(connection, hello)
        connection.tasks.append(asyncio.create_task(self._watch(connection)))
        while connection.closing is None:
            message = await connection.next_message()
            if message is None or message.type in CLOSED_TYPES:
                return
            self._received(connection, message)

    def _may_attach(self, resume: dict | None) -> bool:
        """SPEC resume rule. No resume (a fresh tab or "다시 가져오기") always takes over. A resume takes over
        only while nobody else attached since: the same run and the current epoch (a release or timeout keeps
        the epoch). An older or a never-issued epoch is someone else's lease (``taken_elsewhere``). A resume
        from an earlier Hub run names no lease here: it attaches only while this run has had no attach."""
        if resume is None:
            return True
        if resume["run"] != self.run:
            # Across Hub runs (a restart, or the epoch past uint32): epochs of another run compare with nothing
            # here. While this run has had no attach there is no lease anyone else holds, so the tab gets it;
            # once someone attached in this run, the tab's lease is gone and it is ``taken_elsewhere``.
            return self.epoch == 0
        return resume["epoch"] == self.epoch

    def _attach(self, connection: Connection, hello: dict) -> None:
        """Synchronous (no await): the epoch, the lease, ``attached``, the replay and the live output subscription
        happen in one step, so no output chunk falls between the replay and the stream."""
        previous = self.epoch or None
        if self.epoch >= EPOCH_MAX:  # the frame header holds 32 bits: a new run restarts the count
            self.run, self.epoch = secrets.token_hex(8), 0
        self.epoch += 1
        old, self.current = self.current, connection
        connection.last_seen = asyncio.get_running_loop().time()  # the heartbeat counts from the attach
        self.board.set_lease("connected", self.epoch)
        if old is not None:  # D-28 (1): the new tab always wins at once, a live or a half-open old one alike
            self._record("terminal_takeover", connection.address, self.epoch, previous)
            old.close(CLOSE_TAKEN, "taken over", drop_queue=True, final={"t": "taken_over", "epoch": self.epoch})
        self._record("terminal_attach", connection.address, self.epoch, previous)
        for name, (rows, cols) in hello["sizes"].items():
            self.board.resize(name, rows, cols)
        connection.send_json({"t": "attached", "v": PROTOCOL_VERSION, "run": self.run, "epoch": self.epoch,
                              "channels": self._channels(), "heartbeat": self.heartbeat})
        for name in CHANNEL_IDS:
            replay = self.board.replay(name)
            for start in range(0, len(replay), REPLAY_CHUNK):
                connection.send_data(data_frame(name, self.epoch, replay[start:start + REPLAY_CHUNK]))
            connection.unsubscribe.append(self.board.subscribe(name, self._output_to(connection, name)))

        def changed() -> None:  # a start, exit, restart or isolation verdict of either channel
            if connection is self.current:
                connection.send_json({"t": "state", "channels": self._channels()})
        connection.unsubscribe.append(self.board.watch(changed))

    def _channels(self) -> dict[str, Any]:
        status = self.board.status() or {}
        return {name: status.get(name) for name in CHANNEL_IDS}

    def _overflow(self, connection: Connection) -> None:
        self._release(connection, "terminal_closed")
        connection.close(CLOSE_OVERFLOW, "send queue full", drop_queue=True)

    async def _watch(self, connection: Connection) -> None:
        """D-28 (3): the current connection's silence beyond ``misses x heartbeat + slack`` ends it (4002) and
        leaves the lease ``none``. A half-open TCP connection (no EOF) ends here."""
        loop = asyncio.get_running_loop()
        limit = self.misses * self.heartbeat + self.slack
        next_check = loop.time() + self.session_check
        while connection is self.current:
            now = loop.time()
            remaining = connection.last_seen + limit - now
            if remaining <= 0:
                self._release(connection, "terminal_timeout")
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
        self._release(connection, "terminal_closed")
        connection.close(CLOSE_SESSION, "session ended", drop_queue=True)

    def sessions_ended(self, token: str | None) -> None:
        """A web logout (``token``) or a password change (None: every session): their sockets end now."""
        for connection in list(self.connections):
            if token is None or connection.token == token:
                self._end_session(connection)

    def _received(self, connection: Connection, message) -> None:
        connection.last_seen = asyncio.get_running_loop().time()
        if message.type == WSMsgType.BINARY:
            self._input(connection, message.data)
        elif message.type == WSMsgType.TEXT:
            self._control(connection, message.data)

    def _control(self, connection: Connection, text: str) -> None:
        if connection is not self.current:
            return  # a taken-over or released connection's message is never even parsed
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
        if not _is_int(control.get("epoch"), self.epoch):
            return  # another lease's message: nothing to do
        kind = control["t"]
        if kind == "resize":
            self._resize(connection, control)
        elif kind == "ping":
            number = control.get("n")
            connection.reply({"t": "pong", "n": number if type(number) is int else None})
        elif kind == "release":
            self._release(connection, "terminal_release")
            connection.close(CLOSE_RELEASED, "released")
        else:
            connection.reply({"t": "error", "reason": "bad_message"})

    def _resize(self, connection: Connection, control: dict) -> None:
        name = control.get("channel")
        try:
            if name not in CHANNEL_IDS:
                raise ValueError(name)
            self.board.resize(name, control.get("rows"), control.get("cols"))
        except ValueError:
            connection.reply({"t": "error", "reason": "bad_resize"})

    def _input(self, connection: Connection, data: bytes) -> None:
        """A data frame reaches the PTY only from the current lease and with the current epoch (D-28 (2))."""
        if len(data) < HEADER.size or data[0] not in CHANNEL_NAMES:
            self.dropped_inputs += 1
            connection.reply({"t": "error", "reason": "bad_frame"})
            return
        if len(data) - HEADER.size > INPUT_LIMIT:
            self.dropped_inputs += 1
            connection.reply({"t": "error", "reason": "too_large"})
            return
        channel_id, epoch = HEADER.unpack_from(data)
        if connection is not self.current or epoch != self.epoch:
            self.dropped_inputs += 1
            return
        if not self.board.write(CHANNEL_NAMES[channel_id], data[HEADER.size:]):
            self.dropped_inputs += 1
            connection.reply({"t": "error", "reason": "input_refused", "channel": CHANNEL_NAMES[channel_id]})

    def _output_to(self, connection: Connection, name: str) -> Callable[[bytes], None]:
        def deliver(chunk: bytes) -> None:
            if connection is not self.current:
                return
            connection.send_data(data_frame(name, self.epoch, chunk))
        return deliver


def routes(terminal: Callable[[], BoardTerminal | None]) -> Callable[[web.Application], None]:
    """``GET /ws/board`` over ``terminal()`` (None: no Board set, 503). The session middleware answers 401 before
    the handler runs (an ``/ws/`` route is an API route); API keys never reach it."""
    @middleware.api
    async def get_ws(request: web.Request) -> web.StreamResponse:
        if not origin_exact(request):
            return middleware.refuse(request, 403, "origin_mismatch")
        current = terminal()
        if current is None:
            return middleware.refuse(request, 503, "board_unavailable")
        return await current.handle(request)

    def sessions_ended(token: str | None) -> None:
        current = terminal()
        if current is not None:
            current.sessions_ended(token)

    async def flush(_app: web.Application) -> None:
        current = terminal()
        if current is not None:
            await current.flush()

    def register(app: web.Application) -> None:
        app.router.add_get("/ws/board", get_ws)
        app.on_cleanup.append(flush)  # the listener closes ``hub.sqlite3`` only after the runner cleanup
        listeners = app[middleware.SERVICES].session_listeners
        if sessions_ended not in listeners:  # the same services across a listener reopen
            listeners.append(sessions_ended)
    return register
