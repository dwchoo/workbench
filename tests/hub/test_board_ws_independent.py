"""TI-17 / WBX-D1 independent verification of the Board WebSocket terminals, protocol v2 (seam T5).

Expected values come from web-board-ux SPEC s4 "WebSocket v2: terminal마다 공유 연결" (shared attach: every client gets
the same replay and live output, input in arrival order, the size follows the client that last typed or resized,
8 clients per terminal -> 4429, 4404 when the terminal goes, per-connection heartbeat/session/queue, per-connection
audit), BRIEF WBX-D54/D55 and the liveness cases that stay from D-28 (tab close, browser killed, silent death,
network cut, "a leftover connection never blocks a new one"); never from the product's helpers or the worker's tests.
The client is a small raw RFC 6455 client of this file (``Wire``: its own handshake, masking, framing, TCP RST), so a
framing assumption of ``support/ws_client.py`` or aiohttp's client cannot hide a defect.

Migrated from protocol v1 (WBX-D1): the lease, epochs, ``resume``, ``taken_over``/``taken_elsewhere``, 4001 and
``terminal_takeover`` are gone. Every v1 case is kept as its shared-attach counterpart; where the expectation changed,
the test's docstring says what v1 expected.

Seams: the real ``create_app`` (real ``hub.sqlite3`` sessions and audit, session/Origin middleware) on a loopback
port with a real ``BoardSet`` whose terminals (agent-1 and one shell, both started in the set-up) are a recorder PTY
command (raw mode: every byte its terminal delivers goes to a per-terminal file, then it echoes ``<bytes>``);
``support.tcp_proxy`` for the half-open and cut cases; one real ``wb _hub --foreground`` for the Hub-stop close. The
Board WebSockets get a shortened heartbeat (0.5 s x 2 + 0.25 s = 1.25 s of silence); the shipped 10 s interval is
covered by the worker's real-Hub case.

Hygiene: loopback only, temp HOME/XDG, every PTY process of a test is checked gone (pid + start ticks) at the end.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import struct
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import aiohttp
from aiohttp import web

from support.tcp_proxy import TcpProxy

from workbench.hub import auth
from workbench.hub.board import ws as board_ws
from workbench.hub.board.set import BoardSet
from workbench.hub.web.app import WEBSOCKETS, OriginPolicy, WebServices, create_app

PASSWORD = 'correct horse battery'

RECORDER = textwrap.dedent('''\
    import os, sys, tty
    record, name = sys.argv[1], sys.argv[2]
    tty.setraw(0)
    os.write(1, b"ready:" + name.encode() + b";")
    out = os.open(record, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    while True:
        data = os.read(0, 65536)
        if not data:
            break
        os.write(out, data)
        if data.startswith(b"SIZE?"):
            size = os.get_terminal_size(0)
            os.write(1, b"size=%d,%d;" % (size.lines, size.columns))
        elif data.startswith(b"FLOOD"):
            chunk = b"x" * 65536
            for _ in range(int(data[5:].split(b";")[0])):
                os.write(1, chunk)
            os.write(1, b"flood-done;")
        else:
            os.write(1, b"<" + data + b">")
    ''')


def starttime(pid: int) -> str | None:
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def pid_alive(pid: int, ticks: str | None) -> bool:
    """The process (pid + start ticks) still exists and is no zombie."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
    except (OSError, IndexError):
        return False
    return fields[19] == ticks and fields[0] not in ('Z', 'X')


async def until(predicate, timeout: float = 5.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return bool(predicate())
        await asyncio.sleep(step)


def mask_bytes(payload: bytes, mask: bytes) -> bytes:
    if not payload:
        return payload
    size = len(payload)
    key = (mask * (size // 4 + 1))[:size]
    return (int.from_bytes(payload, 'big') ^ int.from_bytes(key, 'big')).to_bytes(size, 'big')


class Wire:
    """One raw WebSocket client of one terminal (protocol v2). ``events`` = every control message (dicts),
    ``output`` = the data bytes, ``frames`` = each data payload; ``close_code`` is the server's close code, ``eof``
    the TCP end."""

    def __init__(self, port: int, headers: list[tuple[str, str]], *, path: str = '/ws/board', read: bool = True,
                 rcvbuf: int | None = None, label: str = '', reply_close: bool = True):
        self.path = path
        self.lenient = False
        self.reply_close = reply_close  # False: a tab that never answers the server's close (it is already gone)
        self.port = port
        self.headers = headers
        self.read = read
        self.rcvbuf = rcvbuf
        self.label = label
        self.status: int | None = None
        self.events: list[dict] = []
        self.frames: list[bytes] = []
        self.output = bytearray()
        self.close_code: int | None = None
        self.closed_at: float | None = None
        self.eof = False
        self.eof_at: float | None = None
        self.task: asyncio.Task | None = None
        self.sent_close = False
        self.r: asyncio.StreamReader | None = None
        self.w: asyncio.StreamWriter | None = None

    async def open(self) -> int:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if self.rcvbuf:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf)
        sock.setblocking(False)
        await loop.sock_connect(sock, ('127.0.0.1', self.port))
        self.r, self.w = await asyncio.open_connection(sock=sock)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f'GET {self.path} HTTP/1.1', 'Upgrade: websocket', 'Connection: Upgrade', f'Sec-WebSocket-Key: {key}',
                 'Sec-WebSocket-Version: 13']
        lines += [f'{name}: {value}' for name, value in self.headers]
        self.w.write(('\r\n'.join(lines) + '\r\n\r\n').encode())
        await self.w.drain()
        head = await asyncio.wait_for(self.r.readuntil(b'\r\n\r\n'), 10)
        self.status = int(head.split()[1])
        if self.status == 101 and self.read:
            self.start_reader()
        elif self.status != 101:
            self.abort()
        return self.status

    def start_reader(self) -> None:
        self.task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        pending = bytearray()
        pending_op = 0
        try:
            while True:
                head = await self.r.readexactly(2)
                fin, opcode, length = head[0] & 0x80, head[0] & 0x0F, head[1] & 0x7F
                if length == 126:
                    length = struct.unpack('>H', await self.r.readexactly(2))[0]
                elif length == 127:
                    length = struct.unpack('>Q', await self.r.readexactly(8))[0]
                payload = await self.r.readexactly(length)
                if opcode in (1, 2, 0):
                    if opcode:
                        pending, pending_op = bytearray(), opcode
                    pending += payload
                    if not fin:
                        continue
                    data, kind = bytes(pending), pending_op
                    if kind == 1:
                        self.events.append(json.loads(data))
                    else:
                        self.frames.append(data)
                        self.output += data
                elif opcode == 8:
                    if self.close_code is None:
                        self.close_code = struct.unpack('>H', payload[:2])[0] if len(payload) >= 2 else 1005
                        self.closed_at = time.monotonic()
                    if not self.sent_close and self.reply_close:
                        self._send(8, payload[:2])
                elif opcode == 9:
                    self._send(10, payload)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self.eof = True
            self.eof_at = time.monotonic()

    def _send(self, opcode: int, payload: bytes) -> None:
        if opcode == 8:
            self.sent_close = True
        self.w.write(self.raw_frame(opcode, payload))

    @staticmethod
    def raw_frame(opcode: int, payload: bytes) -> bytes:
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            head = bytes([0x80 | opcode, 0x80 | size])
        elif size < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack('>H', size)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack('>Q', size)
        return head + mask + mask_bytes(payload, mask)

    async def _drain(self) -> None:
        try:
            await self.w.drain()
        except (ConnectionError, OSError):
            if not self.lenient:  # a tab that was already cut off: its late frames simply do not arrive
                raise

    async def send_text(self, text: str) -> None:
        self._send(1, text.encode())
        await self._drain()

    async def send_json(self, message: dict) -> None:
        await self.send_text(json.dumps(message))

    async def send_binary(self, data: bytes) -> None:
        self._send(2, data)
        await self._drain()

    async def send_input(self, payload: bytes) -> None:
        await self.send_binary(payload)

    async def send_close(self, code: int = 1001) -> None:
        self._send(8, struct.pack('>H', code))
        await self._drain()

    async def hello(self, csrf, *, size=(31, 101), **extra) -> None:
        message = {'t': 'hello', 'v': 2, 'csrf': csrf, **extra}
        if size is not None:
            message['size'] = list(size)
        await self.send_json(message)

    # --- reading -------------------------------------------------------------------------------

    def of(self, kind: str) -> list[dict]:
        return [event for event in self.events if event.get('t') == kind]

    async def wait_event(self, kind: str, timeout: float = 10.0) -> dict:
        if not await until(lambda: self.of(kind) or self.eof, timeout):
            raise AssertionError(f'{self.label}: no {kind!r} in {timeout}s; events={self.events}')
        if not self.of(kind):
            raise AssertionError(f'{self.label}: closed ({self.close_code}) before {kind!r}; events={self.events}')
        return self.of(kind)[0]

    async def wait_output(self, needle: bytes, timeout: float = 10.0) -> None:
        if not await until(lambda: needle in self.output, timeout):
            raise AssertionError(f'{self.label}: {needle!r} not in output; tail={bytes(self.output[-200:])!r}')

    async def wait_close(self, timeout: float = 10.0) -> int | None:
        if not await until(lambda: self.eof, timeout):
            raise AssertionError(f'{self.label}: no close in {timeout}s; events={self.events}')
        return self.close_code

    async def attach(self, csrf, *, size=(31, 101)) -> dict:
        await self.hello(csrf, size=size)
        return await self.wait_event('attached')

    async def ping(self, n: int = 1, timeout: float = 5.0) -> None:
        before = len(self.of('pong'))
        await self.send_json({'t': 'ping', 'n': n})
        if not await until(lambda: len(self.of('pong')) > before, timeout):
            raise AssertionError(f'{self.label}: no pong; events={self.events}')

    # --- ending --------------------------------------------------------------------------------

    def socket_(self):
        return self.w.get_extra_info('socket')

    def rst(self) -> None:
        """Browser killed hard: the TCP connection ends with an RST, no close frame."""
        sock = self.socket_()
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        self.w.transport.abort()

    def fin(self) -> None:
        """Browser killed gently: the process dies, the kernel sends FIN; no WebSocket close frame."""
        sock = self.socket_()
        sock.shutdown(socket.SHUT_RDWR)

    def abort(self) -> None:
        if self.w is not None and not self.w.transport.is_closing():
            self.w.transport.abort()

    async def close(self) -> None:
        self.abort()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)




@dataclass
class Started:
    argv: list
    env: dict
    cwd: str
    key_id: str = ''


@dataclass
class RecorderLauncher:
    """agent-1 records to ``agent.in``, the first shell to ``shell.in``, later shells to ``shell<n>.in``."""

    base: Path
    state_dir: Path
    shells: int = 0

    def __post_init__(self):
        self.script = self.base / 'recorder.py'
        self.script.write_text(RECORDER)

    def started(self, name: str) -> Started:
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.base), 'TERM': 'xterm-256color'}
        return Started([sys.executable, '-I', str(self.script), str(self.base / f'{name}.in'), name], env,
                       str(self.base))

    def prepare_shell(self):
        self.shells += 1
        return self.started('shell' if self.shells == 1 else f'shell{self.shells}')

    async def prepare_agent(self, spec):
        return self.started('agent')

    async def check_agent(self, started, cancel=None):
        return None

    async def close(self):
        pass


class Env(unittest.IsolatedAsyncioTestCase):
    HEARTBEAT = 0.5
    SLACK = 0.25
    MISSES = 2
    HELLO = 1.0
    QUEUE_LIMIT = board_ws.SEND_QUEUE_LIMIT
    MAX_CONNECTIONS: int | None = None  # None: the shipped caps (128 sockets, 8 before the hello per session)
    MAX_PENDING: int | None = None
    MAX_CLIENTS: int | None = None  # None: the shipped 8 clients per terminal
    SESSION_CHECK: float | None = None

    @property
    def limit(self) -> float:
        return self.MISSES * self.HEARTBEAT + self.SLACK

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-wi-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        runtime = Path(tempfile.mkdtemp(prefix='wx'))
        runtime.chmod(0o700)
        self.addCleanup(shutil.rmtree, runtime, True)
        patch = mock.patch.dict(os.environ, {
            'HOME': str(self.base), 'XDG_CONFIG_HOME': str(self.base / 'cfg'), 'XDG_STATE_HOME': str(self.base / 'st'),
            'XDG_DATA_HOME': str(self.base / 'data'), 'XDG_CACHE_HOME': str(self.base / 'cache'),
            'XDG_RUNTIME_DIR': str(runtime), 'WORKBENCH_HUB_AUTOSTART': '0'})
        patch.start()
        self.addCleanup(patch.stop)
        self.state = self.base / 'hub'
        self.passwords = auth.PasswordStore(self.state)
        self.generation = self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(self.state)
        self.addCleanup(self.db.close)
        self.services = WebServices(self.passwords, self.db, OriginPolicy('127.0.0.1'))
        self.launcher = RecorderLauncher(self.base, self.state)
        self.board = BoardSet(self.launcher, grace=1.0)
        self.terminal = board_ws.BoardTerminal(
            self.board, heartbeat=self.HEARTBEAT, misses=self.MISSES, slack=self.SLACK, hello_timeout=self.HELLO,
            queue_limit=self.QUEUE_LIMIT, audit=board_ws.audit_to(self.services),
            **{key: value for key, value in (('max_connections', self.MAX_CONNECTIONS),
                                             ('max_pending', self.MAX_PENDING), ('max_clients', self.MAX_CLIENTS),
                                             ('session_check', self.SESSION_CHECK)) if value is not None})
        self.app = create_app(self.services, routes=[board_ws.routes(lambda: self.terminal)])
        self.runner = web.AppRunner(self.app, access_log=None, handle_signals=False, shutdown_timeout=2)
        await self.runner.setup()
        await web.TCPSite(self.runner, '127.0.0.1', 0).start()
        self.port = self.runner.addresses[0][1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.wires: list[Wire] = []
        self.proxies: list[TcpProxy] = []
        self.procs: dict[int, str | None] = {}
        self.session = self.new_session()
        self.addAsyncCleanup(self.shutdown)
        # agent-1 (the first open) and one shell; both recorders in raw mode before any test sends a byte
        self.ids = {'agent': self.board.views()[0]['id']}
        await self.board.open([self.ids['agent']])
        self.ids['shell'] = next(iter(self.board.shells))
        await self.board.settled()
        ok = await until(lambda: all(f'ready:{name};'.encode() in self.board.replay(self.ids[name])
                                     for name in ('agent', 'shell')), 15)
        self.assertTrue(ok, 'recorder PTYs did not start')
        self.remember_procs()

    async def shutdown(self):
        for wire in self.wires:
            await wire.close()
        for proxy in self.proxies:
            await proxy.close()
        self.remember_procs()
        await self.runner.cleanup()
        await self.board.stop()
        leaked = [pid for pid, ticks in self.procs.items() if ticks and pid_alive(pid, ticks)]
        self.assertEqual(leaked, [], 'PTY processes outlived the test')

    # --- builders ------------------------------------------------------------------------------

    def new_session(self):
        return self.services.sessions.create('127.0.0.1', 'test', time.time(), generation=self.generation)

    def headers(self, cookie='auto', origin='auto', extra=()) -> list[tuple[str, str]]:
        cookie = self.session.token if cookie == 'auto' else cookie
        origin = self.url if origin == 'auto' else origin
        rows = [('Host', f'127.0.0.1:{self.port}')]
        if origin is not None:
            rows.append(('Origin', origin))
        if cookie is not None:
            rows.append(('Cookie', f'wb_hub_session={cookie}'))
        return rows + list(extra)

    async def wire(self, *, terminal: str = 'shell', proxy: TcpProxy | None = None, session=None, label='', read=True,
                   rcvbuf=None, connect=True, reply_close=True, **kwargs) -> Wire:
        cookie = kwargs.pop('cookie', (session or self.session).token)
        path = f'/ws/board/{self.ids.get(terminal, terminal)}'
        wire = Wire(proxy.port if proxy else self.port, self.headers(cookie=cookie, **kwargs), path=path, read=read,
                    rcvbuf=rcvbuf, label=label, reply_close=reply_close)
        wire.csrf = (session or self.session).csrf
        self.wires.append(wire)
        if connect:
            self.assertEqual(await wire.open(), 101)
        return wire

    async def tab(self, *, terminal: str = 'shell', session=None, proxy=None, label='', size=(31, 101), **kwargs):
        """A connected, attached connection to ``terminal``: ``(wire, attached)``."""
        wire = await self.wire(terminal=terminal, proxy=proxy, session=session, label=label, **kwargs)
        attached = await wire.attach((session or self.session).csrf, size=size)
        return wire, attached

    async def proxy(self) -> TcpProxy:
        proxy = await TcpProxy('127.0.0.1', self.port).start()
        self.proxies.append(proxy)
        return proxy

    # --- observation ---------------------------------------------------------------------------

    def clients(self, terminal: str = 'shell') -> int:
        return self.board.view(self.ids[terminal])['clients']

    def remember_procs(self) -> None:
        for view in self.board.views():
            if view['pid']:
                self.procs[view['pid']] = starttime(view['pid'])

    def recorded(self, terminal: str = 'shell') -> bytes:
        try:
            return (self.base / f'{terminal}.in').read_bytes()
        except FileNotFoundError:
            return b''

    def audit_rows(self) -> list[tuple[str, str, dict]]:
        db = sqlite3.connect(f'file:{self.state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            rows = db.execute("SELECT event, actor, detail_json FROM audit WHERE event LIKE 'terminal_%' "
                              'ORDER BY id').fetchall()
        finally:
            db.close()
        return [(event, actor, json.loads(detail)) for event, actor, detail in rows]

    def audit(self) -> list[tuple[str, int, int]]:
        """(event, client number, the terminal's clients after it) of every ``terminal_*`` row."""
        return [(event, detail['client'], detail['clients']) for event, _, detail in self.audit_rows()]

    async def audit_has(self, count: int, timeout: float = 3.0) -> list[tuple[str, int, int]]:
        await until(lambda: len(self.audit()) >= count, timeout)
        return self.audit()

    async def settled(self, delay: float = 0.3) -> None:
        await asyncio.sleep(delay)

    async def stay(self, wire: Wire, seconds: float) -> None:
        """A live tab: ping on the heartbeat for ``seconds``."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await wire.ping(n=3)
            await asyncio.sleep(self.HEARTBEAT * 0.6)

    async def type_in(self, wire: Wire, text: bytes) -> None:
        await wire.send_input(text)
        await wire.wait_output(b'<' + text + b'>')



# ===================================================================================================
# Upgrade authentication (P2-AC-13/20, AC-38: session cookie, exact Origin, no API key, a known terminal)
# ===================================================================================================

class UpgradeTests(Env):
    async def status(self, **kwargs) -> int:
        wire = await self.wire(connect=False, **kwargs)
        return await wire.open()

    async def test_the_upgrade_needs_the_session_cookie(self):
        self.assertEqual(await self.status(cookie=None), 401)
        self.assertEqual(await self.status(cookie='junk-token'), 401)
        self.assertEqual(await self.status(cookie=''), 401)

    async def test_a_deleted_session_cookie_is_refused(self):
        other = self.new_session()
        self.assertEqual(await self.status(cookie=other.token), 101)
        self.services.sessions.delete(other.token)
        self.assertEqual(await self.status(cookie=other.token), 401)

    async def test_an_api_key_cannot_open_the_terminal_by_any_route(self):
        key = self.services.api_keys.issue('editor-wi', 'editor', {'all': True}, None, now=time.time())
        bearer = ('Authorization', f'Bearer {key.raw}')
        self.assertEqual(await self.status(cookie=None, extra=[bearer]), 401)
        self.assertEqual(await self.status(cookie=key.raw), 401)  # the key as a cookie value
        self.assertEqual(await self.status(cookie=None, extra=[('X-Api-Key', key.raw), ('Authorization', key.raw)]),
                         401)

    async def test_origin_must_be_exactly_this_origin(self):
        for bad in ('http://evil.example', 'null', f'http://127.0.0.1:{self.port + 1}', f'https://127.0.0.1:{self.port}',
                    f'http://127.0.0.1:{self.port}.evil.example', f'http://localhost:{self.port}', ''):
            self.assertEqual(await self.status(origin=bad), 403, bad)
        self.assertEqual(await self.status(origin=None), 403, 'no Origin header')
        self.assertEqual(await self.status(origin=None, extra=[('Referer', self.url + '/board')]), 403,
                         'a Referer never replaces Origin')
        self.assertEqual(await self.status(extra=[('Origin', 'http://evil.example')]), 403, 'two Origin headers')
        self.assertEqual(await self.status(), 101)

    async def test_a_refused_upgrade_changes_nothing(self):
        """v1 also checked that no set started; v2 starts nothing on any upgrade, refused or not."""
        await self.status(origin='http://evil.example')
        await self.status(cookie=None)
        self.assertEqual(await self.status(terminal='s-ffffffffffff'), 404, 'an unknown terminal, before the upgrade')
        self.assertEqual(await self.status(terminal='nonsense'), 404)
        self.assertEqual((self.clients('shell'), self.clients('agent'), self.audit()), (0, 0, []))


# ===================================================================================================
# Connection caps (SPEC s4: 128 sockets in all -> 503, 8 per session before the hello -> 429; both refused before
# the upgrade). The cap logic runs here with small caps; the shipped values are checked on their own.
# ===================================================================================================

class ConnectionCapTests(Env):
    HELLO = 1.5
    HEARTBEAT = 5.0  # an idle client must not time out while the test waits for hello windows
    MAX_CONNECTIONS = 8  # v1's shipped values, so the same cases run quickly
    MAX_PENDING = 2

    async def refused(self, session=None) -> int:
        wire = await self.wire(session=session, connect=False)
        return await wire.open()

    def test_the_shipped_caps_are_128_sockets_and_8_before_the_hello_per_session(self):
        """v1 shipped 8 and 2 (one lease); v2 (one socket per terminal, 8 clients each) ships 128 and 8."""
        self.assertEqual((board_ws.MAX_CONNECTIONS, board_ws.MAX_PENDING, board_ws.MAX_CLIENTS), (128, 8, 8))

    async def test_the_socket_over_the_total_cap_is_refused_503_and_attached_sockets_count(self):
        holder, attached = await self.tab(label='attached')  # 1 open, greeted
        waiting = []
        for _ in range(3):  # 3 sessions x 2 sockets before their hello = 6
            session = self.new_session()
            waiting += [await self.wire(session=session) for _ in range(2)]
        last = self.new_session()
        waiting.append(await self.wire(session=last))  # 8th socket: one before its hello in a fifth session
        self.assertEqual(self.terminal.open, 8)
        self.assertEqual(await self.refused(last), 503, 'the 9th socket: below its per-session cap, over the total')
        self.assertEqual(await self.refused(self.new_session()), 503)
        self.assertEqual(await self.refused(self.session), 503)
        await holder.ping()
        self.assertEqual(self.clients(), 1)
        self.assertEqual(self.audit(), [('terminal_attach', 1, 1)], 'a refused upgrade leaves no row')
        # A socket that ends frees its place.
        await waiting[0].close()
        self.assertTrue(await until(lambda: self.terminal.open == 7, 5))
        again = await self.wire(session=last)
        self.assertEqual(self.terminal.open, 8)
        self.assertEqual(await self.refused(self.new_session()), 503)
        attached = await again.attach(last.csrf)  # and it works, next to the first client
        self.assertEqual(attached['clients'], 2)

    async def test_a_third_socket_before_the_hello_in_one_session_is_refused_429(self):
        first = await self.wire(label='first')
        second = await self.wire(label='second')
        self.assertEqual(await self.refused(), 429)
        other = self.new_session()
        elsewhere = await self.wire(session=other, label='elsewhere')  # another session is not limited by it
        self.assertEqual(elsewhere.status, 101)
        # A greeted socket no longer counts as waiting.
        await first.attach(self.session.csrf)
        third = await self.wire(label='third')
        self.assertEqual(await self.refused(), 429, 'second and third are waiting again')
        self.assertEqual(self.audit(), [('terminal_attach', 1, 1)], 'a refused upgrade leaves no row')
        # The hello window frees the places (4400), then the session may open sockets again.
        self.assertEqual(await second.wait_close(5), 4400)
        self.assertEqual(await third.wait_close(5), 4400)
        self.assertTrue(await until(lambda: self.terminal.pending.get(self.session.token, 0) == 0, 5))
        await self.wire(label='fresh')
        self.assertEqual(self.clients(), 1, 'the attached client was never touched')

    async def test_the_caps_never_touch_an_attached_client_or_its_input(self):
        holder, attached = await self.tab(label='holder')
        for _ in range(2):
            await self.wire()
        for _ in range(3):
            self.assertEqual(await self.refused(), 429)
        await self.type_in(holder, b'still-typing;')
        self.assertEqual(self.recorded(), b'still-typing;')


# ===================================================================================================
# Hello (5 s window -> 4400; csrf -> 4403)
# ===================================================================================================

class HelloTests(Env):
    denied = 0

    async def assert_denied(self) -> None:
        """Every failed hello leaves exactly one ``terminal_denied`` row: the terminal, the client and a reason."""
        self.denied += 1
        await self.audit_has(self.denied)
        await self.settled()
        rows = self.audit_rows()
        self.assertEqual([event for event, _, _ in rows], ['terminal_denied'] * self.denied)
        for _, actor, detail in rows:
            self.assertEqual(actor, 'owner')
            self.assertEqual(set(detail), {'terminal_id', 'kind', 'name', 'client', 'clients', 'reason'})
            self.assertEqual((detail['terminal_id'], detail['kind'], detail['clients']), (self.ids['shell'], 'shell', 0))
            self.assertIn(detail['reason'], ('hello', 'csrf'))
        dump = json.dumps(rows)
        for secret in (self.session.csrf, self.session.token, 'not json', 'wrong'):
            self.assertNotIn(secret, dump)

    async def assert_no_attach(self, wire: Wire, code: int):
        self.assertEqual(await wire.wait_close(5), code, wire.events)
        self.assertEqual(wire.of('attached'), [])
        await self.assert_denied()
        self.assertEqual(self.clients(), 0)

    async def test_no_hello_in_time_closes_4400(self):
        wire = await self.wire()
        t0 = time.monotonic()
        self.assertEqual(await wire.wait_close(5), 4400)
        self.assertGreaterEqual(wire.closed_at - t0, self.HELLO - 0.05)
        self.assertLess(wire.closed_at - t0, self.HELLO + 1.5)
        await self.assert_denied()

    async def test_the_hello_window_is_per_connection_and_a_late_hello_is_too_late(self):
        wire = await self.wire()
        await asyncio.sleep(self.HELLO + 0.4)
        try:
            await wire.hello(self.session.csrf)
        except (ConnectionError, OSError):
            pass
        self.assertEqual(await wire.wait_close(3), 4400)
        self.assertEqual(wire.of('attached'), [])
        await self.assert_denied()

    async def test_a_first_frame_that_is_not_a_valid_hello_closes_4400(self):
        """v1's ``resume``/``sizes`` cases are now: ``v`` other than 2 and a bad ``size``."""
        valid = {'t': 'hello', 'v': 2, 'csrf': self.session.csrf, 'size': [24, 80]}
        texts = ['not json', '[]', '"hello"', json.dumps({**valid, 't': 'ping'}), json.dumps({**valid, 'v': 1}),
                 json.dumps({**valid, 'v': True}), json.dumps({**valid, 'v': 2.0}), json.dumps({**valid, 'size': 'x'}),
                 json.dumps({**valid, 'size': [24]}), json.dumps({**valid, 'size': ['24', 80]}),
                 json.dumps({**valid, 'size': [24, 80, 1]}), json.dumps({**valid, 'size': [24.5, 80]}),
                 json.dumps({'t': 'hello', 'v': 1, 'csrf': self.session.csrf, 'resume': None, 'sizes': {}})]
        for text in texts:
            wire = await self.wire()
            await wire.send_text(text)
            await self.assert_no_attach(wire, 4400)

    async def test_binary_before_hello_closes_4400_and_reaches_no_pty(self):
        wire = await self.wire()
        await wire.send_input(b'BEFORE-HELLO')
        await self.assert_no_attach(wire, 4400)
        self.assertEqual(self.recorded(), b'')
        wire = await self.wire()  # binary first, then a valid hello on the same connection
        await wire.send_input(b'X')
        try:
            await wire.hello(self.session.csrf)
        except (ConnectionError, OSError):
            pass
        self.assertEqual(await wire.wait_close(5), 4400)
        self.assertEqual(wire.of('attached'), [])
        await self.assert_denied()  # the first socket's row above, this one's second

    async def test_a_wrong_missing_or_foreign_csrf_closes_4403_without_attaching(self):
        other = self.new_session()
        for csrf in ('wrong', '', None, 7, other.csrf, self.session.csrf + 'x', self.session.csrf[:-1]):
            wire = await self.wire()
            await wire.hello(csrf)
            await self.assert_no_attach(wire, 4403)
        wire = await self.wire()
        await wire.send_json({'t': 'hello', 'v': 2, 'size': [24, 80]})  # no csrf field at all
        await self.assert_no_attach(wire, 4403)

    async def test_a_correct_csrf_attaches(self):
        wire, attached = await self.tab()
        self.assertEqual((attached['v'], attached['clients'], attached['heartbeat']), (2, 1, self.HEARTBEAT))
        self.assertEqual((attached['terminal']['id'], attached['terminal']['kind']), (self.ids['shell'], 'shell'))
        self.assertEqual(self.clients(), 1)

    async def test_a_second_hello_on_an_attached_connection_neither_attaches_again_nor_breaks_it(self):
        wire, attached = await self.tab()
        await wire.hello(self.session.csrf)
        await self.settled()
        self.assertEqual((self.clients(), len(wire.of('attached'))), (1, 1))
        self.assertEqual(wire.of('error')[-1]['reason'], 'bad_message')
        await wire.ping(1)
        self.assertIsNone(wire.close_code)


# ===================================================================================================
# Scenario 1: tab closed
# ===================================================================================================

class TabCloseTests(Env):
    async def test_release_detaches_at_once_closes_1000_and_the_terminal_keeps_running(self):
        wire, attached = await self.tab()
        await wire.wait_output(b'ready:shell;')
        await self.type_in(wire, b'before-close')
        self.remember_procs()
        t0 = time.monotonic()
        await wire.send_json({'t': 'release'})
        self.assertTrue(await until(lambda: self.clients() == 0, 1.0))
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(await wire.wait_close(3), 1000)
        self.assertEqual({view['state'] for view in self.board.views()}, {'running'})
        for pid, ticks in self.procs.items():
            self.assertTrue(pid_alive(pid, ticks), 'a tab close never ends the PTY')
        await asyncio.sleep(self.limit + 0.5)  # no later timeout row for a released connection
        self.assertEqual(self.audit(), [('terminal_attach', 1, 1), ('terminal_release', 1, 0)])
        # The terminal still works: a fresh tab sees what was typed.
        again, second = await self.tab(label='again')
        await again.wait_output(b'<before-close>')
        self.assertEqual(second['clients'], 1)

    async def test_a_close_frame_without_a_release_message_detaches_too(self):
        wire, attached = await self.tab()
        t0 = time.monotonic()
        await wire.send_close(1001)  # what a browser sends when a tab closes without the pagehide message
        self.assertTrue(await until(lambda: self.clients() == 0, 1.0))
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertIn(await wire.wait_close(3), (1000, 1001))
        rows = await self.audit_has(2)
        self.assertEqual(rows, [('terminal_attach', 1, 1), ('terminal_closed', 1, 0)])
        self.assertEqual(self.board.view(self.ids['shell'])['state'], 'running')

    async def test_one_clients_release_never_ends_another_client(self):
        """v1 "a stale epoch release frees nothing": in v2 a release (with any extra field) ends only its own
        connection; the other client and its typing stay."""
        old, first = await self.tab(label='old')
        new, second = await self.tab(label='new')
        self.assertEqual(second['clients'], 2)
        for stale in (0, 1, 2, (1 << 32) - 1):
            await new.send_json({'t': 'ping', 'n': 1, 'epoch': stale})
        await old.send_json({'t': 'release', 'epoch': 2})
        self.assertEqual(await old.wait_close(3), 1000)
        await new.ping()
        self.assertIsNone(new.close_code)
        self.assertEqual(self.clients(), 1)
        await until(lambda: new.of('clients'), 3)  # (its own attach carried the 2)
        self.assertEqual(new.of('clients'), [{'t': 'clients', 'count': 1}])
        await self.type_in(new, b'still-here;')


# ===================================================================================================
# Scenario 2: browser killed (TCP end without a close frame) and silent death
# ===================================================================================================

class BrowserKillTests(Env):
    HEARTBEAT = 5.0  # the timeout (11.25 s) cannot be what detaches here

    async def killed(self, how: str):
        wire, attached = await self.tab()
        await wire.wait_output(b'ready:shell;')
        t0 = time.monotonic()
        getattr(wire, how)()
        self.assertTrue(await until(lambda: self.clients() == 0, 3.0), self.clients())
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0, f'{how}: detached after {elapsed:.2f}s')
        rows = await self.audit_has(2)
        self.assertEqual(rows, [('terminal_attach', 1, 1), ('terminal_closed', 1, 0)])
        self.assertEqual(self.board.view(self.ids['shell'])['state'], 'running')

    async def test_an_rst_without_a_close_frame_detaches_within_a_second(self):
        await self.killed('rst')

    async def test_a_fin_without_a_close_frame_detaches_within_a_second(self):
        await self.killed('fin')

    async def test_a_kill_in_the_middle_of_a_frame_detaches_and_writes_nothing(self):
        wire, attached = await self.tab()
        await wire.wait_output(b'ready:shell;')
        wire.w.write(b'\x82\xfe\x10\x00' + os.urandom(100))  # a masked binary frame that announces 4 KiB, then dies
        await wire.w.drain()
        wire.rst()
        self.assertTrue(await until(lambda: self.clients() == 0, 2.0))
        self.assertEqual(self.recorded(), b'')

    async def test_a_new_tab_attaches_after_a_kill_with_the_old_output(self):
        """v1: a fresh epoch and a later resume; v2: a plain new hello, alone again (clients 1), with the replay."""
        await self.killed('rst')
        wire, attached = await self.tab(label='next')
        self.assertEqual(attached['clients'], 1)
        await wire.wait_output(b'ready:shell;')
        again, third = await self.tab(label='second-next')
        self.assertEqual(third['clients'], 2)

    async def test_a_kill_never_looks_like_a_timeout_in_the_audit(self):
        await self.killed('rst')
        await asyncio.sleep(0.5)
        self.assertNotIn('terminal_timeout', [row[0] for row in self.audit()])


class SilentDeathTests(Env):
    async def test_half_open_silence_detaches_after_two_intervals_and_slack(self):
        proxy = await self.proxy()
        wire, attached = await self.tab(proxy=proxy)
        await wire.wait_output(b'ready:shell;')
        self.remember_procs()
        await wire.ping()
        proxy.pause()  # neither side sees an end, both see silence
        t0 = time.monotonic()
        await asyncio.sleep(self.HEARTBEAT * 0.8)
        self.assertEqual(self.clients(), 1, 'one missed interval is not a timeout')
        self.assertTrue(await until(lambda: self.clients() == 0, self.limit + 2.0))
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 2 * self.HEARTBEAT - 0.05, f'dropped too early: {elapsed:.2f}s')
        self.assertLess(elapsed, self.limit + 0.75, f'dropped too late: {elapsed:.2f}s')
        rows = await self.audit_has(2)
        self.assertEqual(rows, [('terminal_attach', 1, 1), ('terminal_timeout', 1, 0)])
        for pid, ticks in self.procs.items():
            self.assertTrue(pid_alive(pid, ticks), 'a timeout never ends the PTY')
        proxy.resume()
        self.assertEqual(await wire.wait_close(5), 4002)
        await asyncio.sleep(0.4)
        self.assertEqual([row[0] for row in self.audit()], ['terminal_attach', 'terminal_timeout'])

    async def test_a_connection_that_never_pings_times_out_even_while_the_server_keeps_sending(self):
        wire, attached = await self.tab()
        await wire.send_input(b'FLOOD40;')  # the server now streams output to a silent client
        await wire.wait_output(b'flood-done;', 10)
        t0 = time.monotonic()
        self.assertTrue(await until(lambda: self.clients() == 0, self.limit + 2.0))
        self.assertLess(time.monotonic() - t0, self.limit + 0.75)
        self.assertEqual(await wire.wait_close(3), 4002)

    async def test_any_client_frame_defers_the_timeout_and_silence_after_it_ends_it(self):
        wire, attached = await self.tab()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2 * self.limit:  # pings only
            await wire.ping(n=7)
            await asyncio.sleep(self.HEARTBEAT * 0.8)
        self.assertEqual(self.clients(), 1)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2 * self.limit:  # input frames only (no ping)
            await wire.send_input(b'k')
            await asyncio.sleep(self.HEARTBEAT * 0.8)
        self.assertEqual(self.clients(), 1)
        self.assertIsNone(wire.close_code)
        self.assertTrue(await until(lambda: self.clients() == 0, self.limit + 2.0))
        self.assertEqual(await wire.wait_close(3), 4002)

    async def test_a_leftover_half_open_connection_never_blocks_a_new_hello(self):
        """Scenario 6. v1: the new tab took over (4001 to the old one); v2: it attaches at once next to the half-open
        one, which then ends by its own heartbeat (4002) without touching the new client."""
        proxy = await self.proxy()
        old, first = await self.tab(proxy=proxy, label='old')
        await old.wait_output(b'ready:shell;')
        await old.ping()
        proxy.pause()
        t0 = time.monotonic()
        new, second = await self.tab(label='new')
        self.assertLess(time.monotonic() - t0, 1.0, 'the new hello is not held up by the old connection')
        self.assertEqual(second['clients'], 2)
        await new.wait_output(b'ready:shell;')  # replay
        await self.type_in(new, b'NEW-TAB')
        await self.stay(new, 2 * self.limit)  # the old connection's silence ends only the old one
        self.assertEqual(self.clients(), 1)
        self.assertIsNone(new.close_code)
        proxy.resume()
        self.assertEqual(await old.wait_close(5), 4002)
        self.assertEqual(old.of('taken_over'), [])
        self.assertEqual(self.audit(), [('terminal_attach', 1, 1), ('terminal_attach', 2, 2),
                                        ('terminal_timeout', 1, 1)])


# ===================================================================================================
# Scenario 3: network cut and restore
# ===================================================================================================

class NetworkCutTests(Env):
    async def test_a_cut_shorter_than_two_intervals_keeps_the_connection_and_delivers_what_was_held(self):
        proxy = await self.proxy()
        wire, attached = await self.tab(proxy=proxy)
        await wire.wait_output(b'ready:shell;')
        await wire.ping()
        proxy.pause()
        await wire.send_input(b'TYPED-IN-CUT')  # held by the cut
        await asyncio.sleep(self.HEARTBEAT * 0.8)
        self.assertEqual(self.clients(), 1)
        proxy.resume()
        await wire.wait_output(b'<TYPED-IN-CUT>')
        await wire.ping(n=2)
        self.assertIsNone(wire.close_code)
        await self.stay(wire, self.limit)
        self.assertEqual(self.clients(), 1)
        self.assertEqual(self.audit(), [('terminal_attach', 1, 1)])

    async def long_cut(self):
        proxy = await self.proxy()
        wire, attached = await self.tab(proxy=proxy, label='cut-tab')
        await wire.wait_output(b'ready:shell;')
        await self.type_in(wire, b'before-cut')
        proxy.pause()
        await wire.send_input(b'TYPED-AFTER-DROP')  # held, and arrives after the server gave up
        self.assertTrue(await until(lambda: self.clients() == 0, self.limit + 2.0))
        return proxy, wire, attached

    async def test_a_cut_longer_than_two_intervals_closes_4002_and_a_new_hello_attaches_again(self):
        """v1: a resume with a new epoch; v2: a new hello (no resume), with the replay."""
        proxy, wire, attached = await self.long_cut()
        proxy.resume()
        self.assertEqual(await wire.wait_close(5), 4002)
        await asyncio.sleep(0.3)
        self.assertNotIn(b'TYPED-AFTER-DROP', self.recorded(), 'input held through the cut never reaches the PTY')
        back, second = await self.tab(label='back')
        self.assertEqual(second['clients'], 1)
        await back.wait_output(b'<before-cut>')  # replay
        await self.type_in(back, b'after-reconnect')
        self.assertEqual(self.recorded(), b'before-cut' + b'after-reconnect')
        self.assertEqual(await self.audit_has(3),
                         [('terminal_attach', 1, 1), ('terminal_timeout', 1, 0), ('terminal_attach', 2, 1)])

    async def test_a_reconnect_after_a_cut_joins_the_client_that_attached_meanwhile(self):
        """v1: the revived resume was ``taken_elsewhere`` (4001) and 다시 가져오기 took over; v2: it simply attaches
        too, and nobody is closed."""
        proxy, wire, attached = await self.long_cut()
        other, second = await self.tab(label='other')
        self.assertEqual(second['clients'], 1)
        proxy.resume()
        self.assertEqual(await wire.wait_close(5), 4002)
        revived, third = await self.tab(label='revived')
        self.assertEqual(third['clients'], 2)
        self.assertEqual((await other.wait_event('clients'))['count'], 2)
        await other.ping()
        await self.type_in(other, b'still-mine')
        await revived.wait_output(b'<still-mine>')
        self.assertIsNone(other.close_code)
        self.assertEqual(revived.of('taken_elsewhere') + other.of('taken_over'), [])

    async def test_a_connection_back_from_a_short_half_open_pause_is_still_attached_next_to_the_new_one(self):
        """v1: the revived old connection was told ``taken_over`` then closed 4001; v2: both stay and see the same
        output."""
        proxy = await self.proxy()
        old, first = await self.tab(proxy=proxy, label='old')
        await old.ping()
        proxy.pause()
        new, second = await self.tab(label='new')
        self.assertEqual(second['clients'], 2)
        await self.type_in(new, b'while-paused;')
        proxy.resume()
        await old.wait_output(b'<while-paused;>')
        await old.ping()
        await self.type_in(old, b'old-is-back;')
        await new.wait_output(b'<old-is-back;>')
        self.assertEqual((old.close_code, new.close_code, self.clients()), (None, None, 2))
        self.assertEqual([row[0] for row in self.audit()], ['terminal_attach', 'terminal_attach'])

    async def test_hellos_with_v1_resume_fields_attach_next_to_the_client_and_never_evict_it(self):
        """v1: a resume that was not the current lease was refused and never evicted the holder; v2 has no resume
        (an extra field of a v2 hello is ignored): each such hello is one more client, and the first one stays."""
        holder, attached = await self.tab(label='holder')
        cases = [{'run': 'r', 'epoch': 0}, {'run': 'r', 'epoch': 2}, {'run': '', 'epoch': (1 << 32) - 1}]
        for index, resume in enumerate(cases):
            wire = await self.wire(label=str(resume))
            await wire.hello(self.session.csrf, resume=resume)
            self.assertEqual((await wire.wait_event('attached'))['clients'], index + 2, resume)
            await holder.ping()
            self.assertIsNone(holder.close_code, resume)
        self.assertEqual(self.clients(), 4)
        self.assertEqual(holder.of('taken_over'), [])

    async def test_the_same_tab_reconnecting_before_the_server_noticed_attaches_as_a_second_client(self):
        """v1: a resume at the current epoch took over (4001 to the old socket); v2: the new connection is a second
        client and the old one is not closed by it."""
        old, attached = await self.tab(label='old')
        new, second = await self.tab(label='again')
        self.assertEqual(second['clients'], 2)
        await old.ping()
        self.assertIsNone(old.close_code)
        self.assertEqual(old.of('taken_over'), [])


# ===================================================================================================
# Scenario 4: many tabs at once (v1: exactly one holder; v2: every client attaches while the terminal has places)
# ===================================================================================================

class RaceChecks(Env):
    """Concurrent hellos to one terminal. ``sessions``: the login sessions the tabs are spread over round-robin."""

    async def race(self, count: int, sessions=None):
        sessions = sessions or [self.session]
        wires = [await self.wire(label=f'tab{index}', session=sessions[index % len(sessions)])
                 for index in range(count)]
        samples: list[int] = []
        stop = False

        async def sampler():
            while not stop:
                samples.append(self.clients())
                await asyncio.sleep(0.002)
        watcher = asyncio.create_task(sampler())
        await asyncio.gather(*(wire.hello(wire.csrf) for wire in wires))
        await until(lambda: all(wire.of('attached') or wire.eof for wire in wires), 10)
        await asyncio.sleep(0.4)
        stop = True
        await watcher
        return wires, samples

    async def check_race(self, count: int, sessions=None):
        wires, samples = await self.race(count, sessions)
        limit = self.terminal.max_clients
        attached = [wire for wire in wires if wire.of('attached')]
        refused = [wire for wire in wires if not wire.of('attached')]
        beating = True

        async def heartbeat():  # live tabs: the checks below take longer than one heartbeat limit
            while beating:
                for wire in attached:
                    if not wire.eof:
                        await wire.send_json({'t': 'ping', 'n': 0})
                await asyncio.sleep(self.HEARTBEAT * 0.5)
        beats = asyncio.create_task(heartbeat())
        self.addAsyncCleanup(lambda: asyncio.gather(beats, return_exceptions=True))
        self.assertEqual(len(attached), min(count, limit), 'every client attaches while there is a place')
        self.assertEqual(self.clients(), min(count, limit))
        for wire in attached:
            self.assertIsNone(wire.close_code, wire.label)
            self.assertEqual(wire.of('taken_over'), [])
        for wire in refused:
            self.assertEqual(wire.of('refused'), [{'t': 'refused', 'reason': 'too_many_clients', 'limit': limit}])
            self.assertEqual(await wire.wait_close(3), 4429, wire.label)
        self.assertEqual(sorted(wire.of('attached')[0]['clients'] for wire in attached),
                         list(range(1, len(attached) + 1)), 'each attach saw its own count')
        self.assertEqual(samples, sorted(samples), 'the client count only grows during the race')
        self.assertLessEqual(max(samples), limit)
        # Everyone attached types, and everyone sees all of it.
        for index, wire in enumerate(attached):
            await wire.send_input(b'T%02d;' % index)
        want = [b'<T%02d;>' % index for index in range(len(attached))]
        for wire in attached:
            self.assertTrue(await until(lambda wire=wire: all(item in wire.output for item in want), 5), wire.label)
        self.assertEqual(sorted(self.recorded().split(b';')[:-1]), sorted(b'T%02d' % i for i in range(len(attached))))
        rows = [row[0] for row in await self.audit_has(count)]
        self.assertEqual((rows.count('terminal_attach'), rows.count('terminal_denied'), rows.count('terminal_takeover')),
                         (len(attached), len(refused), 0))
        # Nobody reconnects by itself and nothing changes afterwards.
        await asyncio.sleep(self.limit / 2)
        self.assertEqual(self.clients(), len(attached))
        beating = False
        await beats


class SimultaneousTests(RaceChecks):
    """Many tabs at once: the socket caps are raised so the per-terminal limit (8 clients) is what decides;
    ``RealCapRaceTests`` repeats the invariant under the shipped caps."""
    MAX_CONNECTIONS = 64
    MAX_PENDING = 64

    async def test_five_tabs_at_once_all_attach(self):
        await self.check_race(5)

    async def test_twelve_tabs_at_once_attach_eight_and_refuse_four_4429(self):
        await self.check_race(12)

    async def test_forty_tabs_at_once_attach_eight_and_refuse_the_rest_4429(self):
        await self.check_race(40)

    async def test_tabs_from_two_login_sessions_racing_all_attach(self):
        other = self.new_session()
        await self.check_race(6, [self.session, other])

    async def test_four_reconnects_at_once_after_a_kill_all_attach(self):
        """v1: two resumes of the same epoch attached one and refused the other (taken_elsewhere)."""
        first, attached = await self.tab(label='first')
        first.rst()
        self.assertTrue(await until(lambda: self.clients() == 0, 2))
        racers = [await self.wire(label=f'racer{index}') for index in range(4)]
        await asyncio.gather(*(wire.hello(self.session.csrf) for wire in racers))
        await until(lambda: all(wire.of('attached') for wire in racers), 5)
        self.assertEqual(sorted(wire.of('attached')[0]['clients'] for wire in racers), [1, 2, 3, 4])
        self.assertEqual(self.clients(), 4)
        for wire in racers:
            await wire.ping()
            self.assertIsNone(wire.close_code)

    async def test_a_hello_storm_attaches_in_hello_arrival_order(self):
        wires = [await self.wire(label=f'w{index}') for index in range(8)]
        for index, wire in enumerate(wires):
            await wire.hello(self.session.csrf)
            await asyncio.sleep(0.005 * (index % 3))
        await until(lambda: all(wire.of('attached') for wire in wires), 10)
        await asyncio.sleep(0.3)
        self.assertEqual([wire.of('attached')[0]['clients'] for wire in wires], list(range(1, 9)),
                         'attach order is hello arrival order')
        self.assertEqual(len([wire for wire in wires if not wire.eof]), 8)


class RealCapRaceTests(RaceChecks):
    """The same under the shipped caps: 128 sockets in all, 8 per session before the hello, 8 clients a terminal."""

    async def test_two_tabs_of_one_session_at_once_both_attach(self):
        self.assertEqual((self.terminal.max_connections, self.terminal.max_pending, self.terminal.max_clients),
                         (128, 8, 8))
        await self.check_race(2)

    async def test_four_tabs_of_two_sessions_at_once_all_attach(self):
        await self.check_race(4, [self.session, self.new_session()])

    async def test_eight_tabs_of_one_session_filling_the_terminal_all_attach(self):
        await self.check_race(8)


# ===================================================================================================
# Scenario 5 (v1: "the old tab types right after a takeover"): input of several clients, in arrival order
# ===================================================================================================

class SharedInputTests(Env):
    async def test_input_of_an_earlier_and_a_later_client_both_reach_the_pty_and_both_see_it(self):
        """v1: the old tab's input after a takeover never reached the PTY; v2: both clients type, in arrival order."""
        old, first = await self.tab(label='old')
        new, second = await self.tab(label='new')
        await self.type_in(new, b'GOOD1;')
        for index in range(20):
            await old.send_input(b'OLD-%d;' % index)
        await old.wait_output(b'<OLD-19;>')
        await self.type_in(new, b'GOOD2;')
        expected = b'GOOD1;' + b''.join(b'OLD-%d;' % index for index in range(20)) + b'GOOD2;'
        self.assertEqual(self.recorded(), expected)
        for wire in (old, new):
            self.assertIn(b'<OLD-19;>', bytes(wire.output))
            self.assertIn(b'<GOOD2;>', bytes(wire.output))
        later, third = await self.tab(label='later')  # a later attach replays all of it
        await later.wait_output(b'<GOOD2;>')
        self.assertIn(b'<OLD-0;>', bytes(later.output))

    async def test_bytes_shaped_like_v1_frame_headers_are_plain_input(self):
        """v1: frames with a stale or crafted epoch header were dropped; v2 frames carry no header, so such bytes are
        input like any other and reach the PTY verbatim."""
        wire, attached = await self.tab()
        crafted = [bytes([1]) + struct.pack('>I', epoch) + b'X;' for epoch in (0, 1, 2, (1 << 32) - 1)]
        for payload in crafted:
            await wire.send_input(payload)
        await self.type_in(wire, b'OK;')
        self.assertEqual(self.recorded(), b''.join(crafted) + b'OK;')
        self.assertEqual(self.recorded('agent'), b'')
        self.assertIsNone(wire.close_code)

    async def test_input_racing_another_clients_attach_arrives_whole_and_in_order(self):
        """v1: an in-order prefix and nothing after the new attach; v2: everything, in order."""
        old, first = await self.tab(label='old')
        chunks = [b'a%04d;' % index for index in range(300)]

        async def typist():
            for chunk in chunks:
                await old.send_input(chunk)
                await asyncio.sleep(0.0005)
        typing = asyncio.create_task(typist())
        await asyncio.sleep(0.02)
        new, second = await self.tab(label='new')
        await typing
        await self.type_in(new, b'NEW;')
        await old.wait_output(b'<NEW;>')
        self.assertTrue(await until(lambda: len(self.recorded()) >= len(b''.join(chunks)) + 4, 5))
        record = self.recorded()
        # the old client's chunks once each and in their order; the other client's input lands where it arrived
        self.assertEqual(record.count(b'NEW;'), 1)
        self.assertEqual(record.replace(b'NEW;', b''), b''.join(chunks), 'every chunk, once, in order')

    async def test_a_burst_written_with_another_clients_hello_arrives_whole_and_in_order(self):
        """v1: all-or-nothing in order, nothing after the takeover; v2: the whole burst and the later input."""
        for trial in range(6):
            before = self.recorded()
            old, first = await self.tab(label=f'old{trial}')
            new = await self.wire(label=f'new{trial}')
            hello = json.dumps({'t': 'hello', 'v': 2, 'csrf': self.session.csrf, 'size': [31, 101]})
            chunks = [b'F%02d.%03d;' % (trial, index) for index in range(60)]
            burst = b''.join(Wire.raw_frame(2, chunk) for chunk in chunks)
            new.w.write(Wire.raw_frame(1, hello.encode()))  # no await between the two writes
            old.w.write(burst)
            await new.wait_event('attached')
            late = b'LATE%02d;' % trial
            await old.send_input(late)
            await self.type_in(new, b'M%02d;' % trial)
            await old.wait_output(b'<M%02d;>' % trial)
            want = len(b''.join(chunks) + late) + len(b'M%02d;' % trial)
            self.assertTrue(await until(lambda: len(self.recorded()) - len(before) >= want, 5), trial)
            delta = self.recorded()[len(before):]
            # Each connection's bytes stay in their order; between connections only arrival order (mixing is
            # accepted, SPEC s4 "입력"): the old burst and its late input, with the new client's marker somewhere.
            marker = b'M%02d;' % trial
            self.assertEqual(delta.count(marker), 1, trial)
            self.assertEqual(delta.replace(marker, b''), b''.join(chunks) + late, trial)
            await old.close()
            await new.close()
            await until(lambda: self.clients() == 0, 3)

    async def test_input_queued_behind_a_release_in_the_same_segment_is_dropped(self):
        """One TCP write carries ``release`` and then an input frame: the connection is closing when the input is
        read, so it must not reach the PTY."""
        wire, attached = await self.tab()
        release = Wire.raw_frame(1, json.dumps({'t': 'release'}).encode())
        typed = Wire.raw_frame(2, b'BEHIND-RELEASE;')
        wire.w.write(release + typed)  # no await in between
        await wire.w.drain()
        self.assertEqual(await wire.wait_close(5), 1000)
        await self.settled()
        self.assertEqual(self.recorded(), b'')
        self.assertEqual(self.clients(), 0)

    async def test_a_reconnect_after_a_kill_types_and_the_killed_connections_half_frame_never_arrives(self):
        """v1: a stale epoch after the same tab resumed was dropped."""
        wire, first = await self.tab(label='before')
        wire.w.write(b'\x82\x8a' + os.urandom(4) + b'PART')  # a masked 10-byte binary frame cut after 4 bytes
        await wire.w.drain()
        wire.rst()
        self.assertTrue(await until(lambda: self.clients() == 0, 2))
        again, second = await self.tab(label='after')
        await self.type_in(again, b'NEW-CONNECTION;')
        self.assertEqual(self.recorded(), b'NEW-CONNECTION;')

    async def test_input_after_a_release_or_timeout_never_reaches_the_pty(self):
        wire, attached = await self.tab(label='released')
        await wire.send_json({'t': 'release'})
        await until(lambda: self.clients() == 0, 2)
        try:
            await wire.send_input(b'AFTER-RELEASE;')
        except (ConnectionError, OSError):
            pass
        await self.settled()
        self.assertEqual(self.recorded(), b'')

    async def test_resize_messages_with_mistyped_sizes_change_nothing_and_the_hello_size_holds(self):
        """v1 "a control message with the wrong epoch type changes nothing" (flaky under load: the forked child set
        the PTY window after the hello's resize, see WBX-D1). v2 has no epoch; mistyped sizes are ``bad_resize``, and
        the hello size is on the PTY before the child runs, so the answer is deterministic."""
        wire, attached = await self.tab(size=(31, 101))
        for bad in ('5', 5.0, True, None, [5], {'e': 5}):
            await wire.send_json({'t': 'resize', 'rows': bad, 'cols': 5})
            await wire.send_json({'t': 'resize', 'rows': 5, 'cols': bad})
        await wire.ping()
        await wire.send_input(b'SIZE?')
        await wire.wait_output(b'size=31,101;')
        self.assertEqual(len([event for event in wire.of('error') if event['reason'] == 'bad_resize']), 12)
        self.assertEqual(self.clients(), 1)


# ===================================================================================================
# Frames, limits, resize, ping
# ===================================================================================================

class FrameTests(Env):
    async def test_input_reaches_only_the_terminal_of_its_socket_and_echo_comes_back_on_it(self):
        """v1: the channel byte addressed agent or shell on one socket; v2: one socket per terminal."""
        shell, _ = await self.tab(label='shell')
        agent, _ = await self.tab(terminal='agent', label='agent')
        await shell.wait_output(b'ready:shell;')
        await agent.wait_output(b'ready:agent;')
        await self.type_in(shell, b'to-shell')
        await self.type_in(agent, b'to-agent')
        self.assertEqual((self.recorded('shell'), self.recorded('agent')), (b'to-shell', b'to-agent'))
        self.assertNotIn(b'to-agent', bytes(shell.output))
        self.assertNotIn(b'to-shell', bytes(agent.output))

    async def test_ping_is_answered_with_its_number_whatever_else_it_carries(self):
        """v1: a foreign epoch ping was not answered; v2 has no epoch, every ping is."""
        wire, attached = await self.tab()
        for number in (0, 1, 12345, 2 ** 31):
            await wire.send_json({'t': 'ping', 'n': number})
        await wire.send_json({'t': 'ping', 'epoch': 99, 'n': 99})
        await until(lambda: len(wire.of('pong')) == 5, 3)
        self.assertEqual([pong['n'] for pong in wire.of('pong')], [0, 1, 12345, 2 ** 31, 99])

    async def test_oversized_input_is_dropped_with_an_error_and_the_connection_stays(self):
        wire, attached = await self.tab()
        limit = 64 * 1024
        await wire.send_input(b'z' * limit)  # exactly the limit: accepted
        await wire.wait_output(b'>', 10)
        await until(lambda: len(self.recorded()) == limit, 5)
        self.assertEqual(len(self.recorded()), limit)
        await wire.send_input(b'Q' * (limit + 1))
        error = await wire.wait_event('error')
        self.assertEqual(error['reason'], 'too_large')
        await wire.send_input(b'R' * (limit * 4))
        await wire.ping()
        self.assertIsNone(wire.close_code)
        await self.type_in(wire, b'after;')
        record = self.recorded()
        self.assertEqual(len(record), limit + len(b'after;'))
        self.assertNotIn(b'Q', record)
        self.assertNotIn(b'R', record)

    async def test_a_huge_frame_never_reaches_the_pty_and_leaves_the_hub_usable(self):
        wire, attached = await self.tab(label='huge')
        wire.lenient = True
        await wire.send_input(b'H' * (3 << 20))
        await until(lambda: wire.eof or wire.of('error'), 5)
        self.assertNotIn(b'H', self.recorded())
        if wire.eof:  # a frame beyond the transport limit (1009) ends the connection: it detaches
            self.assertTrue(await until(lambda: self.clients() == 0, 3))
        else:
            await wire.ping()
        other, second = await self.tab(label='next')
        await self.type_in(other, b'ok;')
        self.assertEqual(self.recorded(), b'ok;')

    async def test_empty_frames_write_nothing_and_text_junk_never_reaches_the_pty(self):
        """v1: short or unknown-channel data frames were dropped; v2 frames are bare input: an empty one writes
        nothing; a text frame is a control message (an error for junk), never input."""
        wire, attached = await self.tab()
        for _ in range(3):
            await wire.send_binary(b'')
        for text in ('JUNK-TEXT;', '{"t": "input", "data": "TEXT-INPUT;"}'):
            await wire.send_text(text)
        await self.settled()
        self.assertEqual((self.recorded('shell'), self.recorded('agent')), (b'', b''))
        self.assertEqual([event['reason'] for event in wire.of('error')], ['bad_message', 'bad_message'])
        await self.type_in(wire, b'fine;')

    async def test_an_oversized_or_malformed_control_message_is_answered_without_effect(self):
        wire, attached = await self.tab()
        await wire.send_text('{' + ' ' * (64 * 1024 + 8) + '}')
        self.assertEqual((await wire.wait_event('error'))['reason'], 'too_large')
        await wire.send_text('not json')
        await wire.send_text('[1, 2]')
        await wire.send_json({'t': 'unknown-kind'})
        await wire.ping()
        self.assertEqual(self.clients(), 1)
        self.assertIsNone(wire.close_code)


class ResizeTests(Env):
    async def size(self, wire: Wire) -> tuple[int, int]:
        before = bytes(wire.output).count(b'size=')
        await wire.send_input(b'SIZE?')
        await until(lambda: bytes(wire.output).count(b'size=') > before, 5)
        text = bytes(wire.output)
        found = text[text.rindex(b'size=') + 5:].split(b';')[0]
        rows, cols = found.split(b',')
        return int(rows), int(cols)

    async def resize(self, wire, rows, cols):
        await wire.send_json({'t': 'resize', 'rows': rows, 'cols': cols})

    async def test_hello_sizes_set_each_terminal_and_a_valid_resize_changes_only_its_terminal(self):
        """v1 (one socket, per-channel ``sizes``) was flaky under load: the child set its own window after the fork,
        which could land after the hello's resize. v2 sets the PTY window before the fork: deterministic."""
        agent, _ = await self.tab(terminal='agent', size=(20, 70), label='agent')
        shell, _ = await self.tab(size=(33, 111), label='shell')
        self.assertEqual(await self.size(agent), (20, 70))
        self.assertEqual(await self.size(shell), (33, 111))
        await self.resize(agent, 40, 120)
        await agent.ping()
        self.assertEqual(await self.size(agent), (40, 120))
        self.assertEqual(await self.size(shell), (33, 111))

    async def test_the_smallest_and_largest_sizes_are_accepted(self):
        wire, attached = await self.tab()
        await self.resize(wire, 1, 1)
        await wire.ping()
        self.assertEqual(await self.size(wire), (1, 1))
        await self.resize(wire, 500, 500)
        await wire.ping()
        self.assertEqual(await self.size(wire), (500, 500))

    async def test_the_spec_upper_bound_500_is_accepted_and_501_is_not(self):
        """v1 (task-inbox SPEC): rows/cols 1~1000; v2 (web-board-ux SPEC s4): 1..500."""
        wire, attached = await self.tab()
        await self.resize(wire, 500, 500)
        await wire.ping()
        self.assertEqual(await self.size(wire), (500, 500))
        await self.resize(wire, 501, 500)
        self.assertEqual((await wire.wait_event('error'))['reason'], 'bad_resize')
        self.assertEqual(await self.size(wire), (500, 500))

    async def test_out_of_range_or_mistyped_sizes_change_nothing(self):
        wire, attached = await self.tab()
        await self.resize(wire, 12, 34)
        await wire.ping()
        self.assertEqual(await self.size(wire), (12, 34))
        for rows, cols in ((0, 10), (10, 0), (-1, 10), (10, -5), (501, 10), (10, 501), (1 << 20, 1 << 20),
                           (1 << 40, 5), ('10', 10), (10.5, 10), (True, 10), (None, 10), ([10], 10)):
            await self.resize(wire, rows, cols)
        await wire.send_json({'t': 'resize', 'rows': 10})
        await wire.send_json({'t': 'resize', 'cols': 10})
        await wire.ping()
        self.assertEqual(await self.size(wire), (12, 34))
        agent, _ = await self.tab(terminal='agent', size=(24, 80), label='agent')
        self.assertEqual(await self.size(agent), (24, 80))
        self.assertIsNone(wire.close_code)
        self.assertTrue(wire.of('error'), 'a refused resize is answered with an error')

    async def test_the_last_client_to_resize_or_type_owns_the_size(self):
        """v1: a stale epoch resize changed nothing; v2: no epoch, the last client to resize or type decides (and a
        new client that is not alone never resizes by attaching)."""
        old, first = await self.tab(label='old', size=(31, 101))
        new, second = await self.tab(label='new', size=(20, 60))
        self.assertEqual(second['size'], [31, 101])
        await self.resize(old, 7, 7)
        await self.resize(new, 8, 8)
        await new.ping()
        self.assertEqual(await self.size(new), (8, 8))
        self.assertEqual(await self.size(old), (7, 7), "old's input makes it the owner with its last size")
        self.assertEqual(await self.size(new), (8, 8))


# ===================================================================================================
# Replay (P2-AC-34, AC-32)
# ===================================================================================================

class ReplayTests(Env):
    async def test_a_later_tab_sees_everything_before_the_live_stream(self):
        first, one = await self.tab(label='first')
        await first.wait_output(b'ready:shell;')
        await self.type_in(first, b'alpha;')
        await self.type_in(first, b'beta;')
        seen_by_first = bytes(first.output)
        second, two = await self.tab(label='second')
        await second.wait_output(b'<beta;>')
        self.assertEqual(bytes(second.output), seen_by_first, 'replay == what the first tab saw, in order')
        await self.type_in(second, b'live;')
        self.assertEqual(bytes(second.output).count(b'<live;>'), 1)
        self.assertEqual(bytes(second.output).count(b'<alpha;>'), 1)
        await first.wait_output(b'<live;>')
        # attached comes before any data frame of the connection
        self.assertEqual(second.events[0]['t'], 'attached')

    async def test_another_login_session_sees_the_earlier_output_and_the_first_stays(self):
        """v1: the other session's tab took over (4001); v2: both stay."""
        first, one = await self.tab(label='first')
        await self.type_in(first, b'from-session-one;')
        other = self.new_session()
        second, two = await self.tab(session=other, label='second')
        await second.wait_output(b'<from-session-one;>')
        await self.type_in(second, b'from-session-two;')
        await first.wait_output(b'<from-session-two;>')
        self.assertIsNone(first.close_code)
        self.assertEqual(self.recorded(), b'from-session-one;from-session-two;')

    async def test_replay_after_a_flood_is_the_exact_ring_tail_in_order(self):
        first, one = await self.tab(label='first')
        await first.send_input(b'FLOOD20;')  # 20 x 64 KiB = 1.25 MiB > the 1 MiB ring
        await first.wait_output(b'flood-done;', 20)
        await asyncio.sleep(0.2)
        everything = bytes(first.output)
        self.assertGreater(len(everything), 1 << 20)
        second, two = await self.tab(label='second')
        await second.wait_output(b'flood-done;', 20)
        await asyncio.sleep(0.3)
        replay = bytes(second.output)
        self.assertTrue(everything.endswith(replay), 'the replay is a contiguous suffix of the full stream')
        self.assertLessEqual(len(replay), 1 << 20)
        self.assertGreater(len(replay), (1 << 20) - 65536 - 1024)

    async def test_nothing_between_the_replay_and_the_live_stream_is_lost_or_doubled(self):
        first, one = await self.tab(label='first')
        for index in range(20):
            await first.send_input(b'm%02d;' % index)
        typing = asyncio.ensure_future(self._type_more(first))  # output keeps coming while the second attaches
        second = await self.wire(label='second')
        await second.hello(self.session.csrf)
        await second.wait_event('attached')
        await typing
        for index in range(40, 60):
            await second.send_input(b'm%02d;' % index)
        await second.wait_output(b'm59;>')
        await first.wait_output(b'm59;>')
        for wire in (first, second):
            text = bytes(wire.output)
            for index in range(60):
                self.assertEqual(text.count(b'm%02d;' % index), 1, (wire.label, index))

    @staticmethod
    async def _type_more(wire: Wire) -> None:
        for index in range(20, 40):
            await wire.send_input(b'm%02d;' % index)
            await asyncio.sleep(0.001)


# ===================================================================================================
# Slow client / send queue overflow
# ===================================================================================================

class SendQueueTests(Env):
    HEARTBEAT = 5.0  # the silent client below must not be ended by the heartbeat before its queue overflows
    QUEUE_LIMIT = 2 << 20  # smaller than the production 4 MiB so the test stays quick; above a 1 MiB ring replay

    async def test_a_client_that_never_reads_is_closed_4003_and_never_stalls_the_hub_or_the_others(self):
        stuck = await self.wire(label='stuck', read=False, rcvbuf=4096)
        await stuck.hello(self.session.csrf)
        self.assertTrue(await until(lambda: self.clients() == 1, 5))
        reader, _ = await self.tab(label='reader')
        lags: list[float] = []
        running = True

        async def ticker():
            while running:
                t0 = time.monotonic()
                await asyncio.sleep(0.01)
                lags.append(time.monotonic() - t0 - 0.01)
        tick = asyncio.create_task(ticker())
        await stuck.send_input(b'FLOOD400;')  # ~25 MiB of output toward a client that never reads
        self.assertTrue(await until(lambda: self.clients() == 1, 10), 'the overflow detaches the stuck client')
        await reader.wait_output(b'flood-done;', 30)  # the reading client got all of it
        fresh, second = await self.tab(label='fresh')  # another tab attaches at once
        self.assertEqual(second['clients'], 2)
        await self.type_in(fresh, b'quick;')
        running = False
        await tick
        self.assertLess(max(lags), 0.5, f'the event loop stalled for {max(lags):.2f}s')
        # The stuck client finally reads: whatever was sent, then the close 4003.
        stuck.start_reader()
        self.assertEqual(await stuck.wait_close(8), 4003)
        self.assertIsNone(reader.close_code)
        rows = await self.audit_has(4)
        self.assertEqual(rows[:3], [('terminal_attach', 1, 1), ('terminal_attach', 2, 2), ('terminal_closed', 1, 1)])
        self.assertNotIn('terminal_timeout', [row[0] for row in rows])

    async def test_a_reading_client_with_a_big_replay_is_not_closed_for_it(self):
        first, one = await self.tab(label='first')
        await first.send_input(b'FLOOD20;')
        await first.wait_output(b'flood-done;', 20)
        second, two = await self.tab(label='second')  # a 1 MiB replay: at the limit but read at once
        await second.wait_output(b'flood-done;', 20)
        await second.ping()
        self.assertIsNone(second.close_code)
        self.assertEqual(self.clients(), 2)


# ===================================================================================================
# The login session ends -> that session's connections end with 4401 (logout at once, CLI passwd within ~6 s)
# ===================================================================================================

class SessionEndTests(Env):
    async def post(self, path: str, data: dict, session=None) -> int:
        session = session or self.session
        headers = {'Origin': self.url, 'Cookie': f'wb_hub_session={session.token}'}
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as http:
            async with http.post(self.url + path, data=data, headers=headers, allow_redirects=False) as response:
                return response.status

    async def test_logout_closes_that_sessions_connections_4401_at_once_and_spares_others(self):
        wire, attached = await self.tab(label='logged-out')
        await self.type_in(wire, b'before-logout;')
        other = self.new_session()
        attached_elsewhere, _ = await self.tab(session=other, label='other-attached')
        bystander = await self.wire(session=other, label='bystander')  # another session, before its hello
        self.remember_procs()
        t0 = time.monotonic()
        self.assertEqual(await self.post('/logout', {'csrf': self.session.csrf}), 303)
        self.assertEqual(await wire.wait_close(3), 4401)
        self.assertLess(time.monotonic() - t0, 1.0, 'logout does not wait for the 5 s session check')
        self.assertEqual(self.clients(), 1)
        rows = await self.audit_has(3)
        self.assertEqual(rows, [('terminal_attach', 1, 1), ('terminal_attach', 2, 2), ('terminal_closed', 1, 1)])
        for pid, ticks in self.procs.items():
            self.assertTrue(pid_alive(pid, ticks), 'a logout never ends the PTY')
        again = await self.wire(connect=False)  # the dead cookie opens nothing
        self.assertEqual(await again.open(), 401)
        await bystander.hello(other.csrf)
        self.assertEqual((await bystander.wait_event('attached'))['clients'], 2)
        await bystander.wait_output(b'<before-logout;>')  # the terminal and its replay live on
        self.assertIsNone(attached_elsewhere.close_code)

    async def test_a_cli_password_change_closes_the_terminal_4401_within_about_six_seconds(self):
        """``wb hub passwd`` rewrites the password file; the Hub's 5 s session re-check notices it."""
        wire, attached = await self.tab(label='cli')
        t0 = time.monotonic()
        await asyncio.get_running_loop().run_in_executor(None, self.passwords.set_password, 'another password 1')
        while wire.close_code is None and time.monotonic() - t0 < 9:
            wire.lenient = True
            await wire.send_json({'t': 'ping', 'n': 1})  # a live tab: not the heartbeat
            await asyncio.sleep(0.2)
        await wire.wait_close(3)
        elapsed = wire.closed_at - t0
        self.assertEqual(wire.close_code, 4401)
        self.assertLess(elapsed, 6.5, f'closed after {elapsed:.1f}s')
        self.assertEqual(self.clients(), 0)
        self.assertEqual((await self.audit_has(2))[-1], ('terminal_closed', 1, 0))


# ===================================================================================================
# Hub stop (SPEC: WebSocket closed 1001 before the terminals end)
# ===================================================================================================

class HubStopTests(Env):
    async def test_the_listener_stop_closes_every_board_socket_1001_and_detaches_everyone(self):
        pre = await self.wire(label='pre-hello')
        first, _ = await self.tab(label='first')
        second, _ = await self.tab(label='second')
        agent, _ = await self.tab(terminal='agent', label='agent')
        self.assertEqual(len(self.app[WEBSOCKETS]), 4)
        await asyncio.gather(*(socket_.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b'hub stopping')
                               for socket_ in list(self.app[WEBSOCKETS])))  # what ``WebListener.stop`` does
        for wire in (pre, first, second, agent):
            self.assertEqual(await wire.wait_close(5), 1001, wire.label)
        self.assertTrue(await until(lambda: self.clients('shell') == 0 and self.clients('agent') == 0, 3))
        rows = await self.audit_has(6)
        closed = [row for row in rows if row[0] == 'terminal_closed']
        # client 1 is the socket before its hello (no attach, no end row); 2 and 3 the shell's, 4 the agent's
        self.assertEqual(sorted(client for _, client, _ in closed), [2, 3, 4], 'one row per attached connection')
        self.assertEqual(sorted(row[2] for row in closed if row[1] in (2, 3)), [0, 1], 'the shell: 2 -> 1 -> 0')

    async def test_a_stop_while_a_client_never_reads_still_ends_the_connection(self):
        stuck = await self.wire(label='stuck', read=False, rcvbuf=4096)
        await stuck.hello(self.session.csrf)
        await until(lambda: self.clients() == 1, 5)
        t0 = time.monotonic()
        await asyncio.gather(*(socket_.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b'hub stopping')
                               for socket_ in list(self.app[WEBSOCKETS])))
        self.assertLess(time.monotonic() - t0, board_ws.CLOSE_DEADLINE + 2)
        self.assertEqual(self.clients(), 0)


# ===================================================================================================
# Audit: per-connection rows, never content (P2-AC-18, AC-34)
# ===================================================================================================

class AuditTests(Env):
    SECRET = b'TOPSECRET-INPUT-7f3a'

    async def test_audit_rows_name_the_terminal_and_the_client_and_no_terminal_content_reaches_the_database(self):
        first, one = await self.tab(label='first')
        await first.wait_output(b'ready:shell;')
        await self.type_in(first, self.SECRET)
        await first.send_json({'t': 'resize', 'rows': 20, 'cols': 20})
        second, two = await self.tab(label='second')
        await self.type_in(second, self.SECRET + b'-2')
        await second.send_json({'t': 'release'})
        await second.wait_close(3)
        third, three = await self.tab(label='third')
        third.rst()
        await self.audit_has(5)
        await asyncio.sleep(0.3)
        rows = self.audit_rows()
        self.assertEqual(self.audit(), [('terminal_attach', 1, 1), ('terminal_attach', 2, 2),
                                        ('terminal_release', 2, 1), ('terminal_attach', 3, 2),
                                        ('terminal_closed', 3, 1)])
        for event, actor, detail in rows:
            self.assertEqual(actor, 'owner')
            self.assertEqual(set(detail), {'terminal_id', 'kind', 'name', 'client', 'clients'}, event)
            self.assertEqual((detail['terminal_id'], detail['kind'], detail['name']),
                             (self.ids['shell'], 'shell', 'shell-1'))
        events = [event for event, _, _ in rows]
        self.assertEqual(events.count('terminal_takeover'), 0, 'v1 had one takeover row here')
        blob = b''
        for path in self.state.rglob('*'):
            if path.is_file():
                blob += path.read_bytes()
        for needle in (self.SECRET, b'TOPSECRET'):
            self.assertNotIn(needle, blob, needle)
        db = sqlite3.connect(f'file:{self.state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            dump = json.dumps(db.execute("SELECT * FROM audit WHERE event LIKE 'terminal_%'").fetchall())
        finally:
            db.close()
        for secret in (self.session.csrf, self.session.token, 'TOPSECRET'):
            self.assertNotIn(secret, dump)
        self.assertTrue(all(len(json.dumps(detail)) < 120 for _, _, detail in rows))


# ===================================================================================================
# A real Hub (wb _hub --foreground): wiring and the 1001 close at stop
# ===================================================================================================

class RealHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_board_stop import BoardHub
        self.scratch = tempfile.TemporaryDirectory(prefix='wb-wi-hub-')
        self.addCleanup(self.scratch.cleanup)
        scratch = Path(self.scratch.name)
        recorder = scratch / 'recorder.py'
        recorder.write_text(RECORDER)
        self.record = scratch / 'shell.in'
        self.hub = await asyncio.to_thread(
            BoardHub, self, shell_script=f'exec {sys.executable} -I {recorder} {self.record} shell\n')
        self.host, self.port = self.hub.browser.host, self.hub.browser.port
        self.url = f'http://{self.host}:{self.port}'
        self.cookie = self.hub.browser.cookies['wb_hub_session']
        self.csrf = await asyncio.to_thread(self.hub.csrf)
        self.wires: list[Wire] = []
        self.addAsyncCleanup(self.close_wires)

    async def close_wires(self):
        for wire in self.wires:
            await wire.close()

    async def wire(self, label: str, terminal: str, *, cookie='auto', origin='auto') -> Wire:
        headers = [('Host', f'{self.host}:{self.port}')]
        if origin is not None:
            headers.append(('Origin', self.url if origin == 'auto' else origin))
        if cookie is not None:
            headers.append(('Cookie', f'wb_hub_session={self.cookie if cookie == "auto" else cookie}'))
        wire = Wire(self.port, headers, path=f'/ws/board/{terminal}', label=label)
        self.wires.append(wire)
        return wire

    async def test_the_real_hub_refuses_bad_upgrades_and_closes_every_board_socket_1001_at_stop(self):
        agent_id = (await asyncio.to_thread(self.hub.board))['agents'][0]['id']
        for kwargs in ({'cookie': None}, {'cookie': 'junk'}, {'origin': 'http://evil.example'}, {'origin': None}):
            wire = await self.wire('refused', agent_id, **kwargs)
            self.assertIn(await wire.open(), (401, 403), kwargs)
        board = await asyncio.to_thread(self.hub.board)
        self.assertEqual(([view['state'] for view in board['agents']], board['shells']), (['not_started'], []),
                         'a refused upgrade starts nothing')
        await asyncio.to_thread(self.hub.start_set)
        shell = self.hub.ids['shell']
        old = await self.wire('old', shell)
        self.assertEqual(await old.open(), 101)
        first = await old.attach(self.csrf)
        self.assertEqual(first['heartbeat'], 10)
        current = await self.wire('current', shell)
        self.assertEqual(await current.open(), 101)
        second = await current.attach(self.csrf)
        self.assertEqual(second['clients'], 2)
        await current.wait_output(b'ready:shell;', 20)
        # one input at a time: the recorder may read two quick inputs in one read and echo them as one
        await old.send_input(b'FROM-OLD;')
        await current.wait_output(b'<FROM-OLD;>')
        await current.send_input(b'FRESH;')
        await current.wait_output(b'<FRESH;>')
        await old.wait_output(b'<FRESH;>')
        await old.wait_output(b'<FROM-OLD;>')
        self.assertEqual(self.record.read_bytes(), b'FROM-OLD;FRESH;')
        waiting = await self.wire('pre-hello', shell)
        self.assertEqual(await waiting.open(), 101)
        board = await asyncio.to_thread(self.hub.board)
        pids = {view['name']: (view['pid'], starttime(view['pid'])) for view in board['agents'] + board['shells']}
        self.assertEqual([view['clients'] for view in board['shells']], [2])
        stopping = asyncio.ensure_future(asyncio.to_thread(self.hub.box.stop_hub, self.hub.hub))
        self.assertEqual(await current.wait_close(15), 1001)
        self.assertEqual(await old.wait_close(15), 1001)
        self.assertEqual(await waiting.wait_close(15), 1001)
        self.assertEqual(await stopping, 0)
        for name, (pid, ticks) in pids.items():
            self.assertFalse(pid_alive(pid, ticks), f'{name} PTY process outlived the Hub')
        rows = [(row['client'], row['clients']) for row in self.hub.audit('terminal_attach')]
        self.assertEqual(sorted(rows), [(1, 1), (2, 2)])
        self.assertNotIn(b'FRESH', (self.hub.box.state / 'hub.sqlite3').read_bytes())

    async def test_a_cli_password_change_closes_the_open_terminal_4401_within_about_six_seconds(self):
        """The real ``wb hub passwd`` while a tab is attached (the Hub re-checks the login every 5 s)."""
        await asyncio.to_thread(self.hub.start_set)
        tab = await self.wire('cli-tab', self.hub.ids['shell'])
        self.assertEqual(await tab.open(), 101)
        await tab.attach(self.csrf)
        await tab.wait_output(b'ready:shell;', 20)
        t0 = time.monotonic()
        result = await asyncio.to_thread(self.hub.box.set_password, 'a second hub password 7')
        self.assertEqual(result.returncode, 0)
        tab.lenient = True
        while tab.close_code is None and time.monotonic() - t0 < 12:
            await tab.send_json({'t': 'ping', 'n': 1})
            await asyncio.sleep(0.25)
        await tab.wait_close(3)
        elapsed = tab.closed_at - t0
        self.assertEqual(tab.close_code, 4401)
        self.assertLess(elapsed, 7.5, f'closed {elapsed:.1f}s after the password change started')
        board = await asyncio.to_thread(self.hub.board)
        self.assertEqual([(view['clients'], view['state']) for view in board['shells']], [(0, 'running')])
        self.assertEqual([(row['client'], row['clients']) for row in self.hub.audit('terminal_closed')], [(1, 0)])


if __name__ == '__main__':
    unittest.main()
