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
HEARTBEAT, MISSES, SLACK = 10.0, 2, 1.0  # the client pings every 10 s; 2 x 10 s + 1 s of silence ends it
SEND_QUEUE_LIMIT = 4 << 20  # queued output bytes per connection
CLOSE_WAIT = 3.0  # a close handshake's limit (aiohttp's own ``timeout``)
CLOSE_DEADLINE = CLOSE_WAIT + 1.0  # a queued close not done by then aborts the TCP connection
EPOCH_MAX = 0xFFFFFFFF
CONTROL_LIMIT = INPUT_LIMIT = 64 * 1024  # SPEC 입력 한도: a WebSocket input frame (bigger: dropped + ``error``)
MESSAGE_LIMIT = 1 << 20  # aiohttp closes the connection (1009) on a message beyond this

# Close codes (TI-18 reads them): 1000 release, 1001 Hub stopping (the listener), 4001 taken over / taken elsewhere,
# 4002 heartbeat timeout, 4003 send queue over its limit, 4400 bad or missing hello, 4403 hello csrf mismatch.
CLOSE_RELEASED = 1000
CLOSE_TAKEN = 4001
CLOSE_TIMEOUT = 4002
CLOSE_OVERFLOW = 4003
CLOSE_BAD_HELLO = 4400
CLOSE_CSRF = 4403


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
    if (not isinstance(value, list) or len(value) != 2
            or not all(type(item) is int and MIN_SIZE <= item <= MAX_SIZE for item in value)):
        raise BadHello("sizes")
    return value[0], value[1]


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
        if connection.sender is not None:
            await asyncio.wait({connection.sender}, timeout=CLOSE_DEADLINE)


class Connection:
    """One WebSocket: an ordered send queue drained by its own task, so a PTY chunk never waits on a client."""

    def __init__(self, ws: web.WebSocketResponse, request: web.Request):
        self.ws = ws
        self.address = request.remote
        self.transport = request.transport
        self.deadline: asyncio.TimerHandle | None = None
        self.queue: deque = deque()
        self.queued = 0
        self.wakeup = asyncio.Event()
        self.sender: asyncio.Task | None = None
        self.unsubscribe: list[Callable[[], None]] = []
        self.closing: int | None = None  # the close code once a close is queued; nothing is queued after it
        self.last_seen = asyncio.get_running_loop().time()  # any frame from the client
        self.tasks: list[asyncio.Task] = []  # the heartbeat watch
        self.stopper = _Stopper(self)

    def start(self) -> None:
        self.sender = asyncio.create_task(self._send_loop())

    def send_json(self, message: dict) -> None:
        if self.closing is None:
            self.queue.append(json.dumps(message, separators=(",", ":")))
            self.wakeup.set()

    def send_data(self, data: bytes) -> None:
        if self.closing is None:
            self.queue.append(data)
            self.queued += len(data)
            self.wakeup.set()

    def close(self, code: int, reason: str, *, drop_queue: bool = False, final: dict | None = None) -> None:
        """Close after what is queued (``drop_queue``: after only ``final``); later sends are ignored. A client
        that does not take the close within :data:`CLOSE_DEADLINE` (half-open, not reading) loses the TCP
        connection, so no connection outlives its close."""
        if self.closing is not None:
            return
        if drop_queue:
            self.queue.clear()
            self.queued = 0
        if final is not None:
            self.send_json(final)
        self.closing = code
        self.queue.append((code, reason.encode()))
        self.wakeup.set()
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
                if isinstance(item, str):
                    await self.ws.send_str(item)
                elif isinstance(item, tuple):
                    await self.ws.close(code=item[0], message=item[1])
                    return
                else:
                    self.queued -= len(item)
                    await self.ws.send_bytes(item)
            self.wakeup.clear()
            await self.wakeup.wait()

    async def finish(self) -> None:
        if self.deadline is not None:
            self.deadline.cancel()
        for unsubscribe in self.unsubscribe:
            unsubscribe()
        self.unsubscribe.clear()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.sender is not None:
            if self.closing is not None and not self.sender.done():
                try:  # let a queued close finish its handshake (aiohttp bounds it with its close timeout)
                    await asyncio.wait_for(asyncio.shield(self.sender), CLOSE_WAIT)
                except (asyncio.TimeoutError, Exception):
                    pass
            self.sender.cancel()
            await asyncio.gather(self.sender, return_exceptions=True)


class BoardTerminal:
    """The Board set's one lease and every WebSocket connection to it. Loop-thread only."""

    def __init__(self, board, *, audit: Audit | None = None, heartbeat: float = HEARTBEAT, misses: int = MISSES,
                 slack: float = SLACK, hello_timeout: float = HELLO_TIMEOUT, queue_limit: int = SEND_QUEUE_LIMIT):
        self.board = board
        self.audit = audit
        self._audit_tail: asyncio.Future | None = None
        self.heartbeat = heartbeat
        self.misses = misses
        self.slack = slack
        self.queue_limit = queue_limit
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
        ws = web.WebSocketResponse(max_msg_size=MESSAGE_LIMIT, timeout=CLOSE_WAIT)
        await ws.prepare(request)
        from ..web.app import WEBSOCKETS  # (the app module registers this route)
        connection = Connection(ws, request)
        # The listener closes it with 1001 when the Hub stops, through the send queue, so this handler waits for
        # the close handshake before it ends the connection.
        request.app[WEBSOCKETS].add(connection.stopper)
        connection.start()
        self.connections.add(connection)
        try:
            try:
                hello = parse_hello(await asyncio.wait_for(ws.receive(), self.hello_timeout))
            except (asyncio.TimeoutError, BadHello):
                await ws.close(code=CLOSE_BAD_HELLO, message=b"hello")
                return ws
            session = middleware.session_of(request)
            if session is None or not middleware.csrf_matches(hello["csrf"], session.csrf):
                await ws.close(code=CLOSE_CSRF, message=b"csrf")
                return ws
            await self.board.ensure_started(actor="owner", address=request.remote)
            if not self._may_attach(hello["resume"]):
                connection.close(CLOSE_TAKEN, "taken elsewhere", final={"t": "taken_elsewhere", "epoch": self.epoch})
                return ws
            self._attach(connection, hello)
            connection.tasks.append(asyncio.create_task(self._watch(connection)))
            async for message in ws:
                self._received(connection, message)
        finally:
            self._release(connection, "terminal_closed")  # EOF, RST, 1001 or any other end of the current one
            try:
                await connection.finish()
            finally:
                self.connections.discard(connection)
        return ws

    def _may_attach(self, resume: dict | None) -> bool:
        """SPEC resume rule. No resume (a fresh tab or "다시 가져오기") always takes over. A resume takes over
        only while nobody else attached since: the same run and the current epoch (a release or timeout keeps
        the epoch). An older or a never-issued epoch is someone else's lease (``taken_elsewhere``). A resume
        from an earlier Hub run names no lease here: it attaches only while this run has had no attach."""
        if resume is None:
            return True
        if resume["run"] != self.run:
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

    async def _watch(self, connection: Connection) -> None:
        """D-28 (3): the current connection's silence beyond ``misses x heartbeat + slack`` ends it (4002) and
        leaves the lease ``none``. A half-open TCP connection (no EOF) ends here."""
        loop = asyncio.get_running_loop()
        limit = self.misses * self.heartbeat + self.slack
        while connection is self.current:
            remaining = connection.last_seen + limit - loop.time()
            if remaining <= 0:
                self._release(connection, "terminal_timeout")
                connection.close(CLOSE_TIMEOUT, "heartbeat timeout", drop_queue=True)
                return
            await asyncio.sleep(remaining)

    def _received(self, connection: Connection, message) -> None:
        connection.last_seen = asyncio.get_running_loop().time()
        if message.type == WSMsgType.BINARY:
            self._input(connection, message.data)
        elif message.type == WSMsgType.TEXT:
            self._control(connection, message.data)

    def _control(self, connection: Connection, text: str) -> None:
        if len(text) > CONTROL_LIMIT:
            connection.send_json({"t": "error", "reason": "too_large"})
            return
        try:
            control = json.loads(text)
        except ValueError:
            control = None
        if not isinstance(control, dict) or not isinstance(control.get("t"), str):
            connection.send_json({"t": "error", "reason": "bad_message"})
            return
        if connection is not self.current or not _is_int(control.get("epoch"), self.epoch):
            return  # another lease's message: nothing to do
        kind = control["t"]
        if kind == "resize":
            self._resize(connection, control)
        elif kind == "ping":
            number = control.get("n")
            connection.send_json({"t": "pong", "n": number if type(number) is int else None})
        elif kind == "release":
            self._release(connection, "terminal_release")
            connection.close(CLOSE_RELEASED, "released")
        else:
            connection.send_json({"t": "error", "reason": "bad_message"})

    def _resize(self, connection: Connection, control: dict) -> None:
        name = control.get("channel")
        try:
            if name not in CHANNEL_IDS:
                raise ValueError(name)
            self.board.resize(name, control.get("rows"), control.get("cols"))
        except ValueError:
            connection.send_json({"t": "error", "reason": "bad_resize"})

    def _input(self, connection: Connection, data: bytes) -> None:
        """A data frame reaches the PTY only from the current lease and with the current epoch (D-28 (2))."""
        if len(data) < HEADER.size or data[0] not in CHANNEL_NAMES:
            self.dropped_inputs += 1
            connection.send_json({"t": "error", "reason": "bad_frame"})
            return
        if len(data) - HEADER.size > INPUT_LIMIT:
            self.dropped_inputs += 1
            connection.send_json({"t": "error", "reason": "too_large"})
            return
        channel_id, epoch = HEADER.unpack_from(data)
        if connection is not self.current or epoch != self.epoch:
            self.dropped_inputs += 1
            return
        if not self.board.write(CHANNEL_NAMES[channel_id], data[HEADER.size:]):
            self.dropped_inputs += 1
            connection.send_json({"t": "error", "reason": "input_refused", "channel": CHANNEL_NAMES[channel_id]})

    def _output_to(self, connection: Connection, name: str) -> Callable[[bytes], None]:
        def deliver(chunk: bytes) -> None:
            if connection is not self.current:
                return
            connection.send_data(data_frame(name, self.epoch, chunk))
            if connection.queued > self.queue_limit:  # a client that does not read never holds the Hub's memory
                self._release(connection, "terminal_closed")
                connection.close(CLOSE_OVERFLOW, "send queue full", drop_queue=True)
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

    def register(app: web.Application) -> None:
        app.router.add_get("/ws/board", get_ws)
    return register
