"""TI-17 independent verification of the Board WebSocket terminal (test_designer owned; seam T5).

Expected values come from the SPEC "WebSocket terminal 프로토콜", BRIEF D-26/D-28 (the five liveness scenarios plus
"a leftover 'connected' never blocks a new tab") and P2-AC-34/35, never from the product's helpers or the worker's
tests. The client is a small raw RFC 6455 client of this file (``Wire``: its own handshake, masking, framing,
TCP RST), so a framing assumption of ``support/ws_client.py`` or aiohttp's client cannot hide a defect.

Seams: the real ``create_app`` (real ``hub.sqlite3`` sessions and audit, session/Origin middleware) on a loopback
port with a real ``BoardSet`` whose two channels are a recorder PTY command (raw mode: every byte its terminal
delivers goes to a per-channel file, then it echoes ``<bytes>``); ``support.tcp_proxy`` for the half-open and cut
cases; one real ``wb _hub --foreground`` for the Hub-stop close. The terminal gets a shortened heartbeat
(0.5 s x 2 + 0.25 s = 1.25 s of silence); the shipped 10 s interval is covered by the worker's real-Hub case.

Hygiene: loopback only, temp HOME/XDG, every PTY process of a test is checked gone (pid + start ticks) at the end.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import random
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
SHELL, AGENT = 1, 0
CH = {'agent': 0, 'shell': 1}

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
    """One raw WebSocket client. ``events`` = every control message (dicts), ``output`` = data bytes per channel,
    ``frames`` = (channel, epoch, payload); ``close_code`` is the server's close code, ``eof`` the TCP end."""

    def __init__(self, port: int, headers: list[tuple[str, str]], *, read: bool = True, rcvbuf: int | None = None,
                 label: str = '', reply_close: bool = True):
        self.lenient = False
        self.reply_close = reply_close  # False: a tab that never answers the server's close (it is already gone)
        self.port = port
        self.headers = headers
        self.read = read
        self.rcvbuf = rcvbuf
        self.label = label
        self.status: int | None = None
        self.events: list[dict] = []
        self.frames: list[tuple[int, int, bytes]] = []
        self.output = {0: bytearray(), 1: bytearray()}
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
        lines = ['GET /ws/board HTTP/1.1', 'Upgrade: websocket', 'Connection: Upgrade', f'Sec-WebSocket-Key: {key}',
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
                        channel, epoch = data[0], struct.unpack('>I', data[1:5])[0]
                        self.frames.append((channel, epoch, data[5:]))
                        self.output[channel] += data[5:]
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

    async def send_input(self, channel: str | int, epoch: int, payload: bytes) -> None:
        channel_id = CH[channel] if isinstance(channel, str) else channel
        await self.send_binary(bytes([channel_id]) + struct.pack('>I', epoch) + payload)

    async def send_close(self, code: int = 1001) -> None:
        self._send(8, struct.pack('>H', code))
        await self._drain()

    async def hello(self, csrf, *, resume=None, sizes=None, **extra) -> None:
        message = {'t': 'hello', 'v': 1, 'csrf': csrf, 'resume': resume,
                   'sizes': {'agent': [24, 80], 'shell': [31, 101]} if sizes is None else sizes, **extra}
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

    async def wait_output(self, channel: str, needle: bytes, timeout: float = 10.0) -> None:
        buffer = self.output[CH[channel]]
        if not await until(lambda: needle in buffer, timeout):
            raise AssertionError(f'{self.label}: {needle!r} not in {channel} output; tail={bytes(buffer[-200:])!r}')

    async def wait_close(self, timeout: float = 10.0) -> int | None:
        if not await until(lambda: self.eof, timeout):
            raise AssertionError(f'{self.label}: no close in {timeout}s; events={self.events}')
        return self.close_code

    async def attach(self, csrf, *, resume=None, sizes=None) -> dict:
        await self.hello(csrf, resume=resume, sizes=sizes)
        return await self.wait_event('attached')

    async def ping(self, epoch: int, n: int = 1, timeout: float = 5.0) -> None:
        before = len(self.of('pong'))
        await self.send_json({'t': 'ping', 'epoch': epoch, 'n': n})
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


class RecorderLauncher:
    def __init__(self, base: Path):
        self.base = base
        self.script = base / 'recorder.py'
        self.script.write_text(RECORDER)

    def started(self, name: str) -> Started:
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.base), 'TERM': 'xterm-256color'}
        return Started([sys.executable, '-I', str(self.script), str(self.base / f'{name}.in'), name], env,
                       str(self.base))

    def prepare_shell(self):
        return self.started('shell')

    async def prepare_agent(self):
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
    MAX_CONNECTIONS: int | None = None  # None: the shipped caps (8 sockets, 2 before the hello per session)
    MAX_PENDING: int | None = None
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
        self.launcher = RecorderLauncher(self.base)
        self.board = BoardSet(self.launcher, grace=1.0)
        self.terminal = board_ws.BoardTerminal(
            self.board, heartbeat=self.HEARTBEAT, misses=self.MISSES, slack=self.SLACK, hello_timeout=self.HELLO,
            queue_limit=self.QUEUE_LIMIT, audit=board_ws.audit_to(self.services),
            **{key: value for key, value in (('max_connections', self.MAX_CONNECTIONS),
                                             ('max_pending', self.MAX_PENDING),
                                             ('session_check', self.SESSION_CHECK)) if value is not None})
        self.app = create_app(self.services, routes=[board_ws.routes(lambda: self.terminal)])
        self.runner = web.AppRunner(self.app, access_log=None, handle_signals=False, shutdown_timeout=2)
        await self.runner.setup()
        await web.TCPSite(self.runner, '127.0.0.1', 0).start()
        self.port = self.runner.addresses[0][1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.is_ready = False
        self.wires: list[Wire] = []
        self.proxies: list[TcpProxy] = []
        self.procs: dict[int, str | None] = {}
        self.session = self.new_session()
        self.addAsyncCleanup(self.shutdown)

    async def shutdown(self):
        for wire in self.wires:
            await wire.close()
        for proxy in self.proxies:
            await proxy.close()
        status = self.board.status() or {}
        for name in ('agent', 'shell'):
            pid = (status.get(name) or {}).get('pid')
            if pid:
                self.procs[pid] = starttime(pid)
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

    async def wire(self, *, proxy: TcpProxy | None = None, session=None, label='', read=True, rcvbuf=None,
                   connect=True, reply_close=True, **kwargs) -> Wire:
        cookie = kwargs.pop('cookie', (session or self.session).token)
        wire = Wire(proxy.port if proxy else self.port, self.headers(cookie=cookie, **kwargs), read=read,
                    rcvbuf=rcvbuf, label=label, reply_close=reply_close)
        wire.csrf = (session or self.session).csrf
        self.wires.append(wire)
        if connect:
            self.assertEqual(await wire.open(), 101)
        return wire

    async def tab(self, *, session=None, resume=None, proxy=None, label='', sizes=None, **kwargs):
        """A connected, attached tab: ``(wire, attached)``. Returns once both recorder PTYs have switched to raw
        mode (their ``ready`` banner is in the ring), so input is never echoed or cut by the line discipline."""
        wire = await self.wire(proxy=proxy, session=session, label=label, **kwargs)
        attached = await wire.attach((session or self.session).csrf, resume=resume, sizes=sizes)
        await self.ready()
        return wire, attached

    async def ready(self) -> None:
        if self.is_ready:  # (a flood later pushes the banner out of the ring)
            return
        ok = await until(lambda: all(f'ready:{name};'.encode() in self.board.replay(name)
                                     for name in ('agent', 'shell')), 15)
        self.assertTrue(ok, 'recorder PTYs did not start')
        self.is_ready = True

    async def proxy(self) -> TcpProxy:
        proxy = await TcpProxy('127.0.0.1', self.port).start()
        self.proxies.append(proxy)
        return proxy

    # --- observation ---------------------------------------------------------------------------

    def lease(self) -> dict:
        return dict(self.board.lease)

    def remember_procs(self) -> None:
        status = self.board.status() or {}
        for name in ('agent', 'shell'):
            pid = (status.get(name) or {}).get('pid')
            if pid:
                self.procs[pid] = starttime(pid)

    def recorded(self, channel: str = 'shell') -> bytes:
        try:
            return (self.base / f'{channel}.in').read_bytes()
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

    def audit(self) -> list[tuple[str, int, int | None]]:
        return [(event, detail['epoch'], detail['previous_epoch']) for event, _, detail in self.audit_rows()]

    async def audit_has(self, count: int, timeout: float = 3.0) -> list[tuple[str, int, int | None]]:
        await until(lambda: len(self.audit()) >= count, timeout)
        return self.audit()

    async def settled(self, delay: float = 0.3) -> None:
        await asyncio.sleep(delay)

    async def stay(self, wire: Wire, epoch: int, seconds: float) -> None:
        """A live tab: ping on the heartbeat for ``seconds``."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await wire.ping(epoch, n=3)
            await asyncio.sleep(self.HEARTBEAT * 0.6)

    async def type_in(self, wire: Wire, epoch: int, text: bytes, channel: str = 'shell') -> None:
        await wire.send_input(channel, epoch, text)
        await wire.wait_output(channel, b'<' + text + b'>')


# ===================================================================================================
# Upgrade authentication (P2-AC-13/20, SPEC: session cookie, exact Origin, no API key)
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
        await self.status(origin='http://evil.example')
        await self.status(cookie=None)
        self.assertIsNone(self.board.status(), 'no refused upgrade starts the set')
        self.assertEqual((self.terminal.epoch, self.audit()), (0, []))


# ===================================================================================================
# Connection caps (SPEC 2026-10-25: 8 sockets in all -> 503, 2 per session before the hello -> 429; both
# refused before the upgrade)
# ===================================================================================================

class ConnectionCapTests(Env):
    HELLO = 1.5
    HEARTBEAT = 5.0  # an idle holder must not time out while the test waits for hello windows

    async def refused(self, session=None) -> int:
        wire = await self.wire(session=session, connect=False)
        return await wire.open()

    async def test_the_ninth_socket_in_all_is_refused_503_and_attached_sockets_count(self):
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
        await holder.ping(attached['epoch'])
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', 1))
        self.assertEqual(self.audit(), [('terminal_attach', 1, None)], 'a refused upgrade leaves no row')
        # A socket that ends frees its place.
        await waiting[0].close()
        self.assertTrue(await until(lambda: self.terminal.open == 7, 5))
        again = await self.wire(session=last)
        self.assertEqual(self.terminal.open, 8)
        self.assertEqual(await self.refused(self.new_session()), 503)
        await again.attach(last.csrf)  # and it works
        self.assertEqual(again.of('attached')[0]['epoch'], 2)

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
        self.assertEqual(self.audit(), [('terminal_attach', 1, None)], 'a refused upgrade leaves no row')
        # The hello window frees the places (4400), then the session may open sockets again.
        self.assertEqual(await second.wait_close(5), 4400)
        self.assertEqual(await third.wait_close(5), 4400)
        self.assertTrue(await until(lambda: self.terminal.pending.get(self.session.token, 0) == 0, 5))
        await self.wire(label='fresh')
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', 1), 'the holder was never touched')

    async def test_the_caps_never_touch_the_current_holder_or_its_input(self):
        holder, attached = await self.tab(label='holder')
        for _ in range(2):
            await self.wire()
        for _ in range(3):
            self.assertEqual(await self.refused(), 429)
        await self.type_in(holder, attached['epoch'], b'still-typing;')
        self.assertEqual(self.recorded(), b'still-typing;')


# ===================================================================================================
# Hello (5 s window -> 4400; csrf -> 4403)
# ===================================================================================================

class HelloTests(Env):
    denied = 0

    async def assert_denied(self) -> None:
        """Every failed hello leaves exactly one ``terminal_denied`` row: epoch (and a reason) only."""
        self.denied += 1
        rows = await self.audit_has(self.denied)
        await self.settled()
        rows = self.audit_rows()
        self.assertEqual([event for event, _, _ in rows], ['terminal_denied'] * self.denied)
        for _, actor, detail in rows:
            self.assertEqual(actor, 'owner')
            self.assertEqual(detail['epoch'], 0)
            self.assertLessEqual(set(detail), {'epoch', 'previous_epoch', 'reason'})
            self.assertIsNone(detail.get('previous_epoch'))
        dump = json.dumps(rows)
        for secret in (self.session.csrf, self.session.token, 'not json', 'wrong'):
            self.assertNotIn(secret, dump)

    async def assert_no_attach(self, wire: Wire, code: int):
        self.assertEqual(await wire.wait_close(5), code, wire.events)
        self.assertEqual(wire.of('attached'), [])
        await self.assert_denied()
        self.assertEqual((self.terminal.epoch, self.lease()['state']), (0, 'none'))
        self.assertIsNone(self.terminal.current)

    async def test_no_hello_in_time_closes_4400(self):
        wire = await self.wire()
        t0 = time.monotonic()
        self.assertEqual(await wire.wait_close(5), 4400)
        self.assertGreaterEqual(wire.closed_at - t0, self.HELLO - 0.05)
        self.assertLess(wire.closed_at - t0, self.HELLO + 1.5)
        self.assertEqual(self.terminal.epoch, 0)
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
        self.assertEqual(self.terminal.epoch, 0)
        await self.assert_denied()

    async def test_a_first_frame_that_is_not_a_valid_hello_closes_4400(self):
        valid = {'t': 'hello', 'v': 1, 'csrf': self.session.csrf, 'resume': None, 'sizes': {}}
        texts = ['not json', '[]', '"hello"', json.dumps({**valid, 't': 'ping'}), json.dumps({**valid, 'v': 2}),
                 json.dumps({**valid, 'v': True}), json.dumps({**valid, 'resume': 'x'}),
                 json.dumps({**valid, 'resume': {'run': 5, 'epoch': 1}}),
                 json.dumps({**valid, 'resume': {'run': 'r', 'epoch': -1}}),
                 json.dumps({**valid, 'resume': {'run': 'r', 'epoch': 1 << 32}}),
                 json.dumps({**valid, 'resume': {'run': 'r', 'epoch': 1.5}}),
                 json.dumps({**valid, 'sizes': {'bogus': [1, 1]}})]
        for text in texts:
            wire = await self.wire()
            await wire.send_text(text)
            await self.assert_no_attach(wire, 4400)

    async def test_binary_before_hello_closes_4400_and_reaches_no_pty(self):
        wire = await self.wire()
        await wire.send_input('shell', 1, b'BEFORE-HELLO')
        await self.assert_no_attach(wire, 4400)
        self.assertEqual(self.recorded(), b'')
        wire = await self.wire()  # binary first, then a valid hello on the same connection
        await wire.send_input('shell', 0, b'X')
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
        await wire.send_json({'t': 'hello', 'v': 1, 'resume': None, 'sizes': {}})  # no csrf field at all
        await self.assert_no_attach(wire, 4403)

    async def test_a_correct_csrf_attaches(self):
        wire, attached = await self.tab()
        self.assertEqual((attached['v'], attached['epoch'], attached['heartbeat']), (1, 1, self.HEARTBEAT))
        self.assertEqual(self.lease()['state'], 'connected')

    async def test_a_second_hello_on_an_attached_connection_neither_attaches_again_nor_breaks_it(self):
        wire, attached = await self.tab()
        await wire.hello(self.session.csrf)
        await self.settled()
        self.assertEqual((self.terminal.epoch, self.lease()['epoch']), (1, 1))
        await wire.ping(1)
        self.assertIsNone(wire.close_code)


# ===================================================================================================
# Scenario 1: tab closed
# ===================================================================================================

class TabCloseTests(Env):
    async def test_release_frees_the_lease_at_once_closes_1000_and_the_set_keeps_running(self):
        wire, attached = await self.tab()
        epoch = attached['epoch']
        await wire.wait_output('shell', b'ready:shell;')
        await self.type_in(wire, epoch, b'before-close')
        self.remember_procs()
        t0 = time.monotonic()
        await wire.send_json({'t': 'release', 'epoch': epoch})
        self.assertTrue(await until(lambda: self.lease()['state'] == 'none', 1.0))
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(await wire.wait_close(3), 1000)
        self.assertEqual(self.lease()['epoch'], epoch, 'a release keeps the epoch (the same tab may resume)')
        self.assertEqual(self.board.active()[0]['lease'], 'none')
        self.assertEqual(self.terminal.view()['lease']['state'], 'none')
        status = self.board.status()
        self.assertEqual((status['agent']['state'], status['shell']['state']), ('running', 'running'))
        for pid, ticks in self.procs.items():
            self.assertTrue(pid_alive(pid, ticks), 'a tab close never ends the PTY')
        await asyncio.sleep(self.limit + 0.5)  # no later timeout row for a released connection
        self.assertEqual(self.audit(), [('terminal_attach', epoch, None), ('terminal_release', epoch, None)])
        self.assertEqual(status['agent']['pid'], self.board.status()['agent']['pid'])
        # The set still works: a fresh tab sees what was typed.
        again, second = await self.tab(label='again')
        await again.wait_output('shell', b'<before-close>')
        self.assertEqual(second['epoch'], epoch + 1)

    async def test_a_close_frame_without_a_release_message_frees_the_lease_too(self):
        wire, attached = await self.tab()
        t0 = time.monotonic()
        await wire.send_close(1001)  # what a browser sends when a tab closes without the pagehide message
        self.assertTrue(await until(lambda: self.lease()['state'] == 'none', 1.0))
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertIn(await wire.wait_close(3), (1000, 1001))
        rows = await self.audit_has(2)
        self.assertEqual(rows, [('terminal_attach', 1, None), ('terminal_closed', 1, None)])
        self.assertEqual(self.board.status()['shell']['state'], 'running')

    async def test_a_stale_epoch_release_from_the_current_socket_or_an_old_tab_frees_nothing(self):
        old, first = await self.tab(label='old')
        new, second = await self.tab(label='new')
        await old.wait_close(3)
        for stale in (first['epoch'], second['epoch'] + 1, 0, (1 << 32) - 1):
            await new.send_json({'t': 'release', 'epoch': stale})
        await new.ping(second['epoch'])
        self.assertEqual(self.lease(), {**self.lease(), 'state': 'connected', 'epoch': 2})
        self.assertIsNone(new.close_code)
        try:  # the old, already-taken socket asks for a release of the new epoch
            await old.send_json({'t': 'release', 'epoch': second['epoch']})
        except (ConnectionError, OSError):
            pass
        await self.settled()
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', 2))
        self.assertIsNone(new.close_code)


# ===================================================================================================
# Scenario 2: browser killed (TCP end without a close frame) and silent death
# ===================================================================================================

class BrowserKillTests(Env):
    HEARTBEAT = 5.0  # the timeout (11.25 s) cannot be what frees the lease here

    async def killed(self, how: str):
        wire, attached = await self.tab()
        epoch = attached['epoch']
        await wire.wait_output('shell', b'ready:shell;')
        t0 = time.monotonic()
        getattr(wire, how)()
        self.assertTrue(await until(lambda: self.lease()['state'] == 'none', 3.0), self.lease())
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0, f'{how}: freed after {elapsed:.2f}s')
        self.assertEqual(self.lease()['epoch'], epoch)
        rows = await self.audit_has(2)
        self.assertEqual(rows, [('terminal_attach', epoch, None), ('terminal_closed', epoch, None)])
        self.assertEqual(self.board.status()['shell']['state'], 'running')
        return epoch

    async def test_an_rst_without_a_close_frame_frees_the_lease_within_a_second(self):
        await self.killed('rst')

    async def test_a_fin_without_a_close_frame_frees_the_lease_within_a_second(self):
        await self.killed('fin')

    async def test_a_kill_in_the_middle_of_a_frame_frees_the_lease(self):
        wire, attached = await self.tab()
        await wire.wait_output('shell', b'ready:shell;')
        wire.w.write(b'\x82\xfe\x10\x00' + os.urandom(100))  # a masked binary frame that announces 4 KiB, then dies
        await wire.w.drain()
        wire.rst()
        self.assertTrue(await until(lambda: self.lease()['state'] == 'none', 2.0))
        self.assertEqual(self.recorded(), b'')

    async def test_a_new_tab_attaches_after_a_kill_with_a_fresh_epoch_and_the_old_output(self):
        epoch = await self.killed('rst')
        wire, attached = await self.tab(label='next')
        self.assertEqual(attached['epoch'], epoch + 1)
        self.assertEqual(wire.of('taken_over'), [])
        await wire.wait_output('shell', b'ready:shell;')
        resumed, third = await self.tab(label='resume', resume={'run': attached['run'], 'epoch': attached['epoch']})
        self.assertEqual(third['epoch'], attached['epoch'] + 1)

    async def test_a_kill_never_looks_like_a_timeout_in_the_audit(self):
        await self.killed('rst')
        await asyncio.sleep(0.5)
        self.assertNotIn('terminal_timeout', [row[0] for row in self.audit()])


class SilentDeathTests(Env):
    async def test_half_open_silence_drops_the_lease_after_two_intervals_and_slack(self):
        proxy = await self.proxy()
        wire, attached = await self.tab(proxy=proxy)
        epoch = attached['epoch']
        await wire.wait_output('shell', b'ready:shell;')
        self.remember_procs()
        await wire.ping(epoch)
        proxy.pause()  # neither side sees an end, both see silence
        t0 = time.monotonic()
        await asyncio.sleep(self.HEARTBEAT * 0.8)
        self.assertEqual(self.lease()['state'], 'connected', 'one missed interval is not a timeout')
        self.assertTrue(await until(lambda: self.lease()['state'] == 'none', self.limit + 2.0))
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 2 * self.HEARTBEAT - 0.05, f'dropped too early: {elapsed:.2f}s')
        self.assertLess(elapsed, self.limit + 0.75, f'dropped too late: {elapsed:.2f}s')
        self.assertEqual(self.lease()['epoch'], epoch)
        self.assertEqual(self.board.active()[0]['lease'], 'none', 'status shows "연결 없음"')
        self.assertEqual(self.terminal.view()['lease']['state'], 'none')
        rows = await self.audit_has(2)
        self.assertEqual(rows, [('terminal_attach', epoch, None), ('terminal_timeout', epoch, None)])
        for pid, ticks in self.procs.items():
            self.assertTrue(pid_alive(pid, ticks), 'a timeout never ends the PTY')
        proxy.resume()
        self.assertEqual(await wire.wait_close(5), 4002)
        await asyncio.sleep(0.4)
        self.assertEqual([row[0] for row in self.audit()], ['terminal_attach', 'terminal_timeout'])

    async def test_a_connection_that_never_pings_times_out_even_while_the_server_keeps_sending(self):
        wire, attached = await self.tab()
        epoch = attached['epoch']
        await wire.send_input('shell', epoch, b'FLOOD40;')  # the server now streams output to a silent client
        await wire.wait_output('shell', b'flood-done;', 10)
        t0 = time.monotonic()
        self.assertTrue(await until(lambda: self.lease()['state'] == 'none', self.limit + 2.0))
        self.assertLess(time.monotonic() - t0, self.limit + 0.75)
        self.assertEqual(await wire.wait_close(3), 4002)

    async def test_any_client_frame_defers_the_timeout_and_silence_after_it_ends_it(self):
        wire, attached = await self.tab()
        epoch = attached['epoch']
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2 * self.limit:  # pings only
            await wire.ping(epoch, n=7)
            await asyncio.sleep(self.HEARTBEAT * 0.8)
        self.assertEqual(self.lease()['state'], 'connected')
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2 * self.limit:  # input frames only (no ping)
            await wire.send_input('shell', epoch, b'k')
            await asyncio.sleep(self.HEARTBEAT * 0.8)
        self.assertEqual(self.lease()['state'], 'connected')
        self.assertIsNone(wire.close_code)
        self.assertTrue(await until(lambda: self.lease()['state'] == 'none', self.limit + 2.0))
        self.assertEqual(await wire.wait_close(3), 4002)

    async def test_a_leftover_half_open_connection_never_blocks_a_new_hello(self):
        """Scenario 6: the old tab still shows 'connected' on the server (nothing noticed yet)."""
        proxy = await self.proxy()
        old, first = await self.tab(proxy=proxy, label='old')
        await old.wait_output('shell', b'ready:shell;')
        await old.ping(first['epoch'])
        proxy.pause()
        t0 = time.monotonic()
        new, second = await self.tab(label='new')
        self.assertLess(time.monotonic() - t0, 1.0, 'the new hello is not held up by the old connection')
        self.assertEqual(second['epoch'], first['epoch'] + 1)
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', second['epoch']))
        await new.wait_output('shell', b'ready:shell;')  # replay
        await self.type_in(new, second['epoch'], b'NEW-TAB')
        await self.stay(new, second['epoch'], 2 * self.limit)  # the old connection's silence must not flip the lease
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', second['epoch']))
        proxy.resume()
        await old.wait_event('taken_over')
        self.assertEqual(await old.wait_close(5), 4001)
        self.assertEqual(old.of('taken_over')[0]['epoch'], second['epoch'])
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', second['epoch']))
        self.assertEqual(self.audit(), [('terminal_attach', 1, None), ('terminal_takeover', 2, 1),
                                        ('terminal_attach', 2, 1)])


# ===================================================================================================
# Scenario 3: network cut and restore
# ===================================================================================================

class NetworkCutTests(Env):
    async def test_a_cut_shorter_than_two_intervals_keeps_the_connection_and_delivers_what_was_held(self):
        proxy = await self.proxy()
        wire, attached = await self.tab(proxy=proxy)
        epoch = attached['epoch']
        await wire.wait_output('shell', b'ready:shell;')
        await wire.ping(epoch)
        proxy.pause()
        await wire.send_input('shell', epoch, b'TYPED-IN-CUT')  # held by the cut
        await asyncio.sleep(self.HEARTBEAT * 0.8)
        self.assertEqual(self.lease()['state'], 'connected')
        proxy.resume()
        await wire.wait_output('shell', b'<TYPED-IN-CUT>')
        await wire.ping(epoch, n=2)
        self.assertIsNone(wire.close_code)
        await self.stay(wire, epoch, self.limit)
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', epoch))
        self.assertEqual(self.audit(), [('terminal_attach', epoch, None)])

    async def long_cut(self):
        proxy = await self.proxy()
        wire, attached = await self.tab(proxy=proxy, label='cut-tab')
        epoch = attached['epoch']
        await wire.wait_output('shell', b'ready:shell;')
        await self.type_in(wire, epoch, b'before-cut')
        proxy.pause()
        await wire.send_input('shell', epoch, b'TYPED-AFTER-DROP')  # held, and arrives after the server gave up
        self.assertTrue(await until(lambda: self.lease()['state'] == 'none', self.limit + 2.0))
        return proxy, wire, attached

    async def test_a_cut_longer_than_two_intervals_closes_4002_and_a_resume_attaches_with_a_new_epoch(self):
        proxy, wire, attached = await self.long_cut()
        epoch, run = attached['epoch'], attached['run']
        proxy.resume()
        self.assertEqual(await wire.wait_close(5), 4002)
        await asyncio.sleep(0.3)
        self.assertNotIn(b'TYPED-AFTER-DROP', self.recorded(), 'input held through the cut never reaches the PTY')
        back, second = await self.tab(label='resumed', resume={'run': run, 'epoch': epoch})
        self.assertEqual(second['epoch'], epoch + 1)
        self.assertEqual(second['run'], run)
        self.assertEqual(back.of('taken_elsewhere'), [])
        await back.wait_output('shell', b'<before-cut>')  # replay
        self.assertEqual({e for _, e, _ in back.frames}, {epoch + 1})
        await self.type_in(back, epoch + 1, b'after-resume')
        self.assertEqual(self.recorded(), b'before-cut' + b'after-resume')
        self.assertEqual(await self.audit_has(3),
                         [('terminal_attach', epoch, None), ('terminal_timeout', epoch, None),
                          ('terminal_attach', epoch + 1, epoch)])

    async def test_a_resume_after_a_cut_is_taken_elsewhere_when_another_tab_attached_meanwhile(self):
        proxy, wire, attached = await self.long_cut()
        epoch, run = attached['epoch'], attached['run']
        other, second = await self.tab(label='other')
        self.assertEqual(second['epoch'], epoch + 1)
        self.assertEqual(other.of('taken_over'), [], 'the lease was already none: nobody to take over from')
        proxy.resume()
        self.assertEqual(await wire.wait_close(5), 4002)
        revived = await self.wire(label='revived')
        await revived.hello(self.session.csrf, resume={'run': run, 'epoch': epoch})
        message = await revived.wait_event('taken_elsewhere')
        self.assertEqual(message['epoch'], second['epoch'])
        self.assertEqual(await revived.wait_close(5), 4001)
        self.assertEqual(revived.of('attached'), [])
        # The other tab was not disturbed.
        await other.ping(second['epoch'])
        await self.type_in(other, second['epoch'], b'still-mine')
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', second['epoch']))
        self.assertIsNone(other.close_code)
        # "다시 가져오기": a resume:null hello takes over for real.
        taken, third = await self.tab(label='retake')
        self.assertEqual(third['epoch'], second['epoch'] + 1)
        await other.wait_event('taken_over')
        self.assertEqual(await other.wait_close(5), 4001)
        events = [row for row in self.audit()]
        self.assertEqual(events[-2:], [('terminal_takeover', 3, 2), ('terminal_attach', 3, 2)])

    async def test_a_revived_old_connection_is_told_taken_over_then_closed_4001(self):
        proxy = await self.proxy()
        old, first = await self.tab(proxy=proxy, label='old')
        await old.ping(first['epoch'])
        proxy.pause()
        new, second = await self.tab(label='new')
        proxy.resume()
        await old.wait_event('taken_over')
        self.assertEqual(await old.wait_close(5), 4001)
        self.assertEqual(old.of('taken_over')[0]['epoch'], second['epoch'])
        # Its resume with the epoch it knew is now taken_elsewhere and must not evict the new tab.
        revived = await self.wire(label='revived')
        await revived.hello(self.session.csrf, resume={'run': first['run'], 'epoch': first['epoch']})
        await revived.wait_event('taken_elsewhere')
        self.assertEqual(await revived.wait_close(5), 4001)
        await new.ping(second['epoch'])
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', second['epoch']))
        rows = await self.audit_has(4)
        self.assertEqual(rows[-1], ('terminal_denied', second['epoch'], None), 'taken_elsewhere is one denied row')
        self.assertEqual(len([row for row in rows if row[0] == 'terminal_denied']), 1)
        self.assertEqual({tuple(detail) for _, _, detail in self.audit_rows()[-1:]}, {('epoch', 'previous_epoch')})

    async def test_a_resume_that_is_not_the_current_lease_never_evicts_the_holder(self):
        holder, attached = await self.tab(label='holder')
        epoch, run = attached['epoch'], attached['run']
        cases = [{'run': run, 'epoch': 0}, {'run': run, 'epoch': epoch + 1}, {'run': run, 'epoch': epoch + 1000},
                 {'run': run, 'epoch': (1 << 32) - 1}, {'run': 'a-run-that-is-not-this-one', 'epoch': epoch},
                 {'run': 'a-run-that-is-not-this-one', 'epoch': 0}, {'run': '', 'epoch': epoch}]
        for resume in cases:
            wire = await self.wire(label=str(resume))
            await wire.hello(self.session.csrf, resume=resume)
            await until(lambda: wire.eof, 3)
            self.assertEqual(wire.of('attached'), [], resume)
            self.assertTrue(wire.eof, resume)
            await holder.ping(epoch)
            self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', epoch), resume)
            self.assertIsNone(holder.close_code, resume)
        self.assertEqual(holder.of('taken_over'), [])

    async def test_a_resume_of_the_current_epoch_while_the_lease_is_held_takes_over(self):
        """The same tab reconnects before the server noticed the old connection died (RFC: resume at the current
        epoch is allowed; the old socket is told taken_over)."""
        old, attached = await self.tab(label='old')
        new, second = await self.tab(label='resume', resume={'run': attached['run'], 'epoch': attached['epoch']})
        self.assertEqual(second['epoch'], attached['epoch'] + 1)
        await old.wait_event('taken_over')
        self.assertEqual(await old.wait_close(5), 4001)


# ===================================================================================================
# Scenario 4: many tabs at once
# ===================================================================================================

class RaceChecks(Env):
    """The single-holder invariant under true concurrency. ``sessions``: the login sessions the tabs are spread
    over round-robin (each must stay within the per-session cap of sockets before their hello)."""

    async def race(self, count: int, sessions=None):
        sessions = sessions or [self.session]
        wires = [await self.wire(label=f'tab{index}', session=sessions[index % len(sessions)])
                 for index in range(count)]
        samples: list[tuple[str, int]] = []
        stop = False

        async def sampler():
            while not stop:
                samples.append((self.lease()['state'], self.lease()['epoch']))
                await asyncio.sleep(0.002)
        watcher = asyncio.create_task(sampler())
        await asyncio.gather(*(wire.hello(wire.csrf) for wire in wires))
        await until(lambda: self.terminal.epoch == count, 10)
        await asyncio.sleep(0.4)
        stop = True
        await watcher
        return wires, samples

    async def check_race(self, count: int, sessions=None):
        wires, samples = await self.race(count, sessions)
        alive = [wire for wire in wires if not wire.eof]
        self.assertEqual(len(alive), 1, 'exactly one connection survives')
        survivor = alive[0]
        self.assertEqual(survivor.of('attached')[0]['epoch'], count, 'the last attach holds the lease')
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', count))
        self.assertIs(self.terminal.current is not None, True)
        takeovers: list[int] = []
        for wire in wires:
            if wire is survivor:
                self.assertEqual(wire.of('taken_over'), [])
                continue
            self.assertEqual(wire.close_code, 4001, wire.label)
            told = wire.of('taken_over')
            self.assertEqual(len(told), 1, wire.label)
            takeovers.append(told[0]['epoch'])
            if wire.of('attached'):  # an attach taken over before its first flush may never be sent
                self.assertGreater(told[0]['epoch'], wire.of('attached')[0]['epoch'])
        self.assertEqual(sorted(takeovers), list(range(2, count + 1)), 'each takeover has its own epoch')
        self.assertTrue(all(state == 'connected' for state, _ in samples if _ > 0), 'never an instant without a lease')
        self.assertEqual([epoch for _, epoch in samples], sorted(epoch for _, epoch in samples),
                         'the lease epoch never goes back')
        # No two holders: only the survivor's input counts, a loser's epoch never works on the survivor's socket.
        await survivor.send_input('shell', count, b'WIN')
        await survivor.wait_output('shell', b'<WIN>')
        for stale in range(1, count):
            await survivor.send_input('shell', stale, b'LOSE%d' % stale)
        await survivor.ping(count)
        await asyncio.sleep(0.2)
        self.assertEqual(self.recorded(), b'WIN')
        rows = [row[0] for row in await self.audit_has(2 * count - 1)]
        self.assertEqual((rows.count('terminal_attach'), rows.count('terminal_takeover')), (count, count - 1))
        # Nobody reconnects by itself and nothing changes afterwards.
        await asyncio.sleep(self.limit / 2)
        self.assertEqual((self.terminal.epoch, survivor.close_code), (count, None))



class SimultaneousTests(RaceChecks):
    """Many tabs at once: the caps are raised so the race itself (not the 8-socket limit) is what is exercised;
    ``RealCapRaceTests`` repeats the invariant under the shipped caps."""
    MAX_CONNECTIONS = 64
    MAX_PENDING = 64

    async def test_five_tabs_at_once_leave_exactly_one_holder(self):
        await self.check_race(5)

    async def test_twelve_tabs_at_once_leave_exactly_one_holder(self):
        await self.check_race(12)

    async def test_forty_tabs_at_once_leave_exactly_one_holder(self):
        await self.check_race(40)

    async def test_tabs_from_two_login_sessions_racing_leave_one_holder(self):
        other = self.new_session()
        sessions = [self.session, other] * 3
        wires = [await self.wire(session=session, label=f's{index}') for index, session in enumerate(sessions)]
        await asyncio.gather(*(wire.hello(wire.csrf) for wire in wires))
        await until(lambda: self.terminal.epoch == 6, 10)
        await asyncio.sleep(0.3)
        self.assertEqual(len([wire for wire in wires if not wire.eof]), 1)
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', 6))

    async def test_two_resumes_of_the_same_epoch_at_once_attach_one_and_refuse_the_other(self):
        first, attached = await self.tab(label='first')
        first.rst()
        self.assertTrue(await until(lambda: self.lease()['state'] == 'none', 2))
        resume = {'run': attached['run'], 'epoch': attached['epoch']}
        racers = [await self.wire(label=f'racer{index}') for index in range(4)]
        await asyncio.gather(*(wire.hello(self.session.csrf, resume=resume) for wire in racers))
        await until(lambda: all(wire.of('attached') or wire.of('taken_elsewhere') for wire in racers), 5)
        winners = [wire for wire in racers if wire.of('attached')]
        losers = [wire for wire in racers if wire.of('taken_elsewhere')]
        self.assertEqual((len(winners), len(losers)), (1, 3))
        self.assertEqual(winners[0].of('attached')[0]['epoch'], attached['epoch'] + 1)
        for wire in losers:
            self.assertEqual(await wire.wait_close(5), 4001)
            self.assertEqual(wire.of('taken_elsewhere')[0]['epoch'], attached['epoch'] + 1)
        await winners[0].ping(attached['epoch'] + 1)
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', attached['epoch'] + 1))

    async def test_a_hello_storm_does_not_stop_a_steady_tab_from_being_replaced_cleanly(self):
        wires = [await self.wire(label=f'w{index}') for index in range(8)]
        for index, wire in enumerate(wires):
            await wire.hello(self.session.csrf)
            await asyncio.sleep(0.005 * (index % 3))
        await until(lambda: self.terminal.epoch == 8, 10)
        await asyncio.sleep(0.3)
        self.assertEqual([wire.of('attached')[0]['epoch'] for wire in wires], list(range(1, 9)),
                         'attach order is hello arrival order')
        self.assertEqual(len([wire for wire in wires if not wire.eof]), 1)
        self.assertFalse(wires[-1].eof)


class RealCapRaceTests(RaceChecks):
    """The single-holder invariant under the shipped caps: 8 sockets in all, 2 per session before the hello."""

    async def test_two_tabs_of_one_session_at_once_leave_exactly_one_holder(self):
        self.assertEqual((self.terminal.max_connections, self.terminal.max_pending), (8, 2))
        await self.check_race(2)

    async def test_four_tabs_of_two_sessions_at_once_leave_exactly_one_holder(self):
        await self.check_race(4, [self.session, self.new_session()])

    async def test_eight_tabs_of_four_sessions_filling_the_cap_leave_exactly_one_holder(self):
        await self.check_race(8, [self.session] + [self.new_session() for _ in range(3)])


# ===================================================================================================
# Scenario 5: the old tab types right after a takeover
# ===================================================================================================

class StaleInputTests(Env):
    async def test_old_tab_input_after_a_takeover_never_reaches_the_pty(self):
        old, first = await self.tab(label='old', reply_close=False)
        old.lenient = True  # the server closes the old socket at once; whatever the old tab still sends is lost
        new, second = await self.tab(label='new')
        await new.wait_output('shell', b'ready:shell;')
        await self.type_in(new, second['epoch'], b'GOOD1;')
        # The old tab keeps typing with its own (old) epoch, with the new epoch it cannot know, and junk.
        for index in range(20):
            await old.send_input('shell', first['epoch'], b'STALE-OLD-%d;' % index)
            await old.send_input('shell', second['epoch'], b'STALE-GUESS-%d;' % index)
            await old.send_input('agent', second['epoch'], b'STALE-AGENT-%d;' % index)
        await old.send_json({'t': 'resize', 'epoch': second['epoch'], 'channel': 'shell', 'rows': 9, 'cols': 9})
        await old.send_json({'t': 'release', 'epoch': second['epoch']})
        await self.type_in(new, second['epoch'], b'GOOD2;')
        await new.send_input('shell', second['epoch'], b'SIZE?')
        await new.wait_output('shell', b'size=31,101;')  # the old tab's resize did nothing
        self.assertEqual(self.recorded(), b'GOOD1;GOOD2;SIZE?')
        self.assertEqual(self.recorded('agent'), b'')
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', second['epoch']))
        self.assertNotIn(b'STALE', bytes(new.output[SHELL]) + bytes(new.output[AGENT]), 'not even echoed')
        # A later attach (replay) never shows any stale byte either.
        later, third = await self.tab(label='later')
        await later.wait_output('shell', b'<GOOD2;>')
        self.assertNotIn(b'STALE', bytes(later.output[SHELL]))

    async def test_old_epoch_frames_crafted_on_the_new_socket_never_reach_the_pty(self):
        old, first = await self.tab(label='old')
        new, second = await self.tab(label='new')
        await old.wait_close(5)
        bad_epochs = [0, first['epoch'], second['epoch'] + 1, second['epoch'] + 1000, (1 << 32) - 1, 1 << 31]
        for epoch in bad_epochs:
            await new.send_input('shell', epoch, b'EPOCH-%d;' % epoch)
            await new.send_input('agent', epoch, b'EPOCH-%d;' % epoch)
        await self.type_in(new, second['epoch'], b'OK;')
        self.assertEqual(self.recorded(), b'OK;')
        self.assertEqual(self.recorded('agent'), b'')
        self.assertEqual(self.terminal.view()['dropped_inputs'], len(bad_epochs) * 2)
        self.assertIsNone(new.close_code)
        self.assertEqual(self.lease()['state'], 'connected')

    async def test_input_racing_the_takeover_is_a_prefix_of_what_was_sent_and_nothing_after_the_attach(self):
        old, first = await self.tab(label='old', reply_close=False)
        old.lenient = True
        chunks = [b'a%04d;' % index for index in range(300)]
        sent_after_attach = asyncio.Event()
        late: list[bytes] = []

        async def typist():
            for chunk in chunks:
                if sent_after_attach.is_set():
                    late.append(chunk)
                await old.send_input('shell', first['epoch'], chunk)
                await asyncio.sleep(0.0005)
        typing = asyncio.create_task(typist())
        await asyncio.sleep(0.02)
        new, second = await self.tab(label='new')  # returns once the new tab has received `attached`
        sent_after_attach.set()
        await typing
        await self.type_in(new, second['epoch'], b'NEW;')
        await asyncio.sleep(0.2)
        record = self.recorded()
        prefixes = {b''.join(chunks[:cut]) for cut in range(len(chunks) + 1)}
        head, _, tail = record.rpartition(b'NEW;')
        self.assertIn(head, prefixes, 'old-epoch bytes are an in-order prefix, nothing out of order')
        self.assertEqual(tail, b'')
        for chunk in late:
            self.assertNotIn(chunk, record, 'sent after the new tab saw attached')
        self.assertTrue(late, 'the typist outlived the takeover')

    async def test_frames_already_in_the_socket_when_the_takeover_is_handled_are_all_or_nothing_in_order(self):
        """The old tab's burst and the new hello reach the server in one loop step (the hello first): whatever
        of the burst was written is an in-order prefix, and nothing sent after the new tab's attach is."""
        seen = set()
        for trial in range(12):
            before = self.recorded()
            old, first = await self.tab(label=f'old{trial}', reply_close=False)
            old.lenient = True
            new = await self.wire(label=f'new{trial}')
            hello = json.dumps({'t': 'hello', 'v': 1, 'csrf': self.session.csrf, 'resume': None, 'sizes': {}})
            chunks = [b'F%02d.%03d;' % (trial, index) for index in range(60)]
            burst = b''.join(Wire.raw_frame(2, bytes([SHELL]) + struct.pack('>I', first['epoch']) + chunk)
                             for chunk in chunks)
            new.w.write(Wire.raw_frame(1, hello.encode()))  # no await between the two writes
            old.w.write(burst)
            second = await new.wait_event('attached')
            late = b'LATE%02d;' % trial
            await old.send_input('shell', first['epoch'], late)
            await self.type_in(new, second['epoch'], b'M%02d;' % trial)
            delta = self.recorded()[len(before):]
            head, marker, tail = delta.rpartition(b'M%02d;' % trial)
            self.assertEqual(tail, b'', trial)
            prefixes = {b''.join(chunks[:cut]) for cut in range(len(chunks) + 1)}
            self.assertIn(head, prefixes, f'trial {trial}: not an in-order prefix of the old burst')
            self.assertNotIn(late, delta, trial)
            seen.add(len(head))
            await old.close()  # a socket that never answers the close would count against the 8-socket cap
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('connected', 24))

    async def test_input_queued_behind_a_release_in_the_same_segment_is_dropped(self):
        """One TCP write carries ``release`` and then an input frame of the same epoch: the lease is gone by the
        time the input is read, so it must not reach the PTY (the epoch alone cannot tell, a release keeps it)."""
        wire, attached = await self.tab()
        epoch = attached['epoch']
        release = Wire.raw_frame(1, json.dumps({'t': 'release', 'epoch': epoch}).encode())
        typed = Wire.raw_frame(2, bytes([SHELL]) + struct.pack('>I', epoch) + b'BEHIND-RELEASE;')
        wire.w.write(release + typed)  # no await in between
        await wire.w.drain()
        self.assertEqual(await wire.wait_close(5), 1000)
        await self.settled()
        self.assertEqual(self.recorded(), b'')
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('none', epoch))

    async def test_a_stale_epoch_input_after_the_same_tab_resumed_is_dropped(self):
        wire, first = await self.tab(label='before')
        wire.rst()
        self.assertTrue(await until(lambda: self.lease()['state'] == 'none', 2))
        again, second = await self.tab(label='after', resume={'run': first['run'], 'epoch': first['epoch']})
        self.assertEqual(second['epoch'], first['epoch'] + 1)
        await again.send_input('shell', first['epoch'], b'OLD-EPOCH;')
        await self.type_in(again, second['epoch'], b'NEW-EPOCH;')
        self.assertEqual(self.recorded(), b'NEW-EPOCH;')

    async def test_input_after_a_release_or_timeout_never_reaches_the_pty(self):
        wire, attached = await self.tab(label='released')
        await wire.send_json({'t': 'release', 'epoch': attached['epoch']})
        await until(lambda: self.lease()['state'] == 'none', 2)
        try:
            await wire.send_input('shell', attached['epoch'], b'AFTER-RELEASE;')
        except (ConnectionError, OSError):
            pass
        await self.settled()
        self.assertEqual(self.recorded(), b'')

    async def test_a_control_message_with_the_wrong_epoch_type_changes_nothing(self):
        wire, attached = await self.tab()
        epoch = attached['epoch']
        for bad in (str(epoch), float(epoch), True, None, [epoch], {'e': epoch}):
            await wire.send_json({'t': 'release', 'epoch': bad})
            await wire.send_json({'t': 'resize', 'epoch': bad, 'channel': 'shell', 'rows': 5, 'cols': 5})
        await wire.ping(epoch)
        await wire.send_input('shell', epoch, b'SIZE?')
        await wire.wait_output('shell', b'size=31,101;')
        self.assertEqual(self.lease()['state'], 'connected')


# ===================================================================================================
# Frames, limits, resize, ping
# ===================================================================================================

class FrameTests(Env):
    async def test_input_reaches_only_the_addressed_channel_and_echo_comes_back_with_the_current_epoch(self):
        wire, attached = await self.tab()
        epoch = attached['epoch']
        await wire.wait_output('shell', b'ready:shell;')
        await wire.wait_output('agent', b'ready:agent;')
        await self.type_in(wire, epoch, b'to-shell', 'shell')
        await self.type_in(wire, epoch, b'to-agent', 'agent')
        self.assertEqual((self.recorded('shell'), self.recorded('agent')), (b'to-shell', b'to-agent'))
        self.assertEqual({e for _, e, _ in wire.frames}, {epoch})
        self.assertEqual({channel for channel, _, _ in wire.frames}, {0, 1})

    async def test_ping_is_answered_with_its_number_and_a_foreign_epoch_ping_is_not(self):
        wire, attached = await self.tab()
        epoch = attached['epoch']
        for number in (0, 1, 12345, 2 ** 31):
            await wire.send_json({'t': 'ping', 'epoch': epoch, 'n': number})
        await until(lambda: len(wire.of('pong')) == 4, 3)
        self.assertEqual([pong['n'] for pong in wire.of('pong')], [0, 1, 12345, 2 ** 31])
        await wire.send_json({'t': 'ping', 'epoch': epoch + 1, 'n': 99})
        await self.settled()
        self.assertEqual(len(wire.of('pong')), 4)

    async def test_oversized_input_is_dropped_with_an_error_and_the_connection_stays(self):
        wire, attached = await self.tab()
        epoch = attached['epoch']
        limit = 64 * 1024
        await wire.send_input('shell', epoch, b'z' * limit)  # exactly the limit: accepted
        await wire.wait_output('shell', b'>', 10)
        await until(lambda: len(self.recorded()) == limit, 5)
        self.assertEqual(len(self.recorded()), limit)
        await wire.send_input('shell', epoch, b'Q' * (limit + 1))
        error = await wire.wait_event('error')
        self.assertEqual(error['reason'], 'too_large')
        await wire.send_input('shell', epoch, b'R' * (limit * 4))
        await wire.ping(epoch)
        self.assertIsNone(wire.close_code)
        await self.type_in(wire, epoch, b'after;')
        record = self.recorded()
        self.assertEqual(len(record), limit + len(b'after;'))
        self.assertNotIn(b'Q', record)
        self.assertNotIn(b'R', record)

    async def test_a_huge_frame_never_reaches_the_pty_and_leaves_the_hub_usable(self):
        wire, attached = await self.tab(label='huge')
        wire.lenient = True
        await wire.send_input('shell', attached['epoch'], b'H' * (3 << 20))
        await until(lambda: wire.eof or wire.of('error'), 5)
        self.assertNotIn(b'H', self.recorded())
        if wire.eof:  # a connection that sends a frame beyond the transport limit may lose it; the lease must follow
            self.assertTrue(await until(lambda: self.lease()['state'] == 'none', 3))
        else:
            await wire.ping(attached['epoch'])
        other, second = await self.tab(label='next')
        await self.type_in(other, second['epoch'], b'ok;')
        self.assertEqual(self.recorded(), b'ok;')

    async def test_malformed_data_frames_never_reach_the_pty(self):
        wire, attached = await self.tab()
        epoch = attached['epoch']
        for raw in (b'', b'\x01', b'\x01\x00\x00', b'\x01\x00\x00\x00', b'\x02' + struct.pack('>I', epoch) + b'BAD-CH;',
                    b'\xff' + struct.pack('>I', epoch) + b'BAD-CH;'):
            try:
                await wire.send_binary(raw)
            except (ConnectionError, OSError):
                break
        await self.settled()
        self.assertEqual((self.recorded('shell'), self.recorded('agent')), (b'', b''))
        if not wire.eof:
            await self.type_in(wire, epoch, b'fine;')

    async def test_an_oversized_or_malformed_control_message_is_answered_without_effect(self):
        wire, attached = await self.tab()
        epoch = attached['epoch']
        await wire.send_text('{' + ' ' * (64 * 1024 + 8) + '}')
        self.assertEqual((await wire.wait_event('error'))['reason'], 'too_large')
        await wire.send_text('not json')
        await wire.send_text('[1, 2]')
        await wire.send_json({'t': 'unknown-kind', 'epoch': epoch})
        await wire.ping(epoch)
        self.assertEqual(self.lease()['state'], 'connected')
        self.assertIsNone(wire.close_code)


class ResizeTests(Env):
    async def size(self, wire: Wire, epoch: int, channel: str) -> tuple[int, int]:
        before = bytes(wire.output[CH[channel]]).count(b'size=')
        await wire.send_input(channel, epoch, b'SIZE?')
        await until(lambda: bytes(wire.output[CH[channel]]).count(b'size=') > before, 5)
        text = bytes(wire.output[CH[channel]])
        found = text[text.rindex(b'size=') + 5:].split(b';')[0]
        rows, cols = found.split(b',')
        return int(rows), int(cols)

    async def resize(self, wire, epoch, rows, cols, channel='shell'):
        await wire.send_json({'t': 'resize', 'epoch': epoch, 'channel': channel, 'rows': rows, 'cols': cols})

    async def test_hello_sizes_set_each_channel_and_a_valid_resize_changes_only_its_channel(self):
        wire, attached = await self.tab(sizes={'agent': [20, 70], 'shell': [33, 111]})
        epoch = attached['epoch']
        self.assertEqual(await self.size(wire, epoch, 'agent'), (20, 70))
        self.assertEqual(await self.size(wire, epoch, 'shell'), (33, 111))
        await self.resize(wire, epoch, 40, 120, 'agent')
        await wire.ping(epoch)
        self.assertEqual(await self.size(wire, epoch, 'agent'), (40, 120))
        self.assertEqual(await self.size(wire, epoch, 'shell'), (33, 111))

    async def test_the_smallest_and_largest_sizes_are_accepted(self):
        wire, attached = await self.tab()
        epoch = attached['epoch']
        await self.resize(wire, epoch, 1, 1)
        await wire.ping(epoch)
        self.assertEqual(await self.size(wire, epoch, 'shell'), (1, 1))
        await self.resize(wire, epoch, 500, 500)
        await wire.ping(epoch)
        self.assertEqual(await self.size(wire, epoch, 'shell'), (500, 500))

    async def test_the_spec_upper_bound_1000_is_accepted(self):
        """SPEC: ``resize`` rows/cols are 1~1000."""
        wire, attached = await self.tab()
        epoch = attached['epoch']
        await self.resize(wire, epoch, 1000, 1000)
        await wire.ping(epoch)
        self.assertEqual(await self.size(wire, epoch, 'shell'), (1000, 1000))

    async def test_out_of_range_or_mistyped_sizes_change_nothing(self):
        wire, attached = await self.tab()
        epoch = attached['epoch']
        await self.resize(wire, epoch, 12, 34)
        await wire.ping(epoch)
        self.assertEqual(await self.size(wire, epoch, 'shell'), (12, 34))
        for rows, cols in ((0, 10), (10, 0), (-1, 10), (10, -5), (1001, 10), (10, 1001), (1 << 20, 1 << 20),
                           (1 << 40, 5), ('10', 10), (10.5, 10), (True, 10), (None, 10), ([10], 10)):
            await self.resize(wire, epoch, rows, cols)
        await wire.send_json({'t': 'resize', 'epoch': epoch, 'channel': 'shell', 'rows': 10})
        await wire.send_json({'t': 'resize', 'epoch': epoch, 'channel': 'bogus', 'rows': 10, 'cols': 10})
        await wire.send_json({'t': 'resize', 'epoch': epoch, 'rows': 10, 'cols': 10})
        await wire.ping(epoch)
        self.assertEqual(await self.size(wire, epoch, 'shell'), (12, 34))
        self.assertEqual(await self.size(wire, epoch, 'agent'), (24, 80))
        self.assertIsNone(wire.close_code)
        self.assertTrue(wire.of('error'), 'a refused resize is answered with an error')

    async def test_a_stale_epoch_resize_changes_nothing(self):
        old, first = await self.tab(label='old', reply_close=False)
        new, second = await self.tab(label='new')
        await self.resize(old, first['epoch'], 7, 7)
        await self.resize(new, first['epoch'], 8, 8)
        await new.ping(second['epoch'])
        self.assertEqual(await self.size(new, second['epoch'], 'shell'), (31, 101))


# ===================================================================================================
# Replay (P2-AC-34)
# ===================================================================================================

class ReplayTests(Env):
    async def test_a_later_tab_sees_everything_before_the_live_stream_with_its_own_epoch(self):
        first, one = await self.tab(label='first')
        await first.wait_output('shell', b'ready:shell;')
        await self.type_in(first, one['epoch'], b'alpha;')
        await self.type_in(first, one['epoch'], b'beta;')
        seen_by_first = bytes(first.output[SHELL])
        second, two = await self.tab(label='second')
        await second.wait_output('shell', b'<beta;>')
        self.assertEqual(bytes(second.output[SHELL]), seen_by_first, 'replay == what the first tab saw, in order')
        self.assertEqual({e for _, e, _ in second.frames}, {two['epoch']}, 'replay frames carry the new epoch')
        await self.type_in(second, two['epoch'], b'live;')
        self.assertEqual(bytes(second.output[SHELL]).count(b'<live;>'), 1)
        self.assertEqual(bytes(second.output[SHELL]).count(b'<alpha;>'), 1)
        # attached comes before any data frame of the connection
        self.assertEqual(second.events[0]['t'], 'attached')

    async def test_another_login_session_taking_over_sees_the_earlier_output(self):
        first, one = await self.tab(label='first')
        await self.type_in(first, one['epoch'], b'from-session-one;')
        other = self.new_session()
        second, two = await self.tab(session=other, label='second')
        await second.wait_output('shell', b'<from-session-one;>')
        await first.wait_event('taken_over')
        self.assertEqual(await first.wait_close(5), 4001)
        await self.type_in(second, two['epoch'], b'from-session-two;')
        self.assertEqual(self.recorded(), b'from-session-one;from-session-two;')

    async def test_replay_after_a_flood_is_the_exact_ring_tail_in_order(self):
        first, one = await self.tab(label='first')
        await first.send_input('shell', one['epoch'], b'FLOOD20;')  # 20 x 64 KiB = 1.25 MiB > the 1 MiB ring
        await first.wait_output('shell', b'flood-done;', 20)
        await asyncio.sleep(0.2)
        everything = bytes(first.output[SHELL])
        self.assertGreater(len(everything), 1 << 20)
        second, two = await self.tab(label='second')
        await second.wait_output('shell', b'flood-done;', 20)
        await asyncio.sleep(0.3)
        replay = bytes(second.output[SHELL])
        self.assertTrue(everything.endswith(replay), 'the replay is a contiguous suffix of the full stream')
        self.assertLessEqual(len(replay), 1 << 20)
        self.assertGreater(len(replay), (1 << 20) - 65536 - 1024)
        self.assertEqual({e for _, e, _ in second.frames}, {two['epoch']})

    async def test_nothing_between_the_replay_and_the_live_stream_is_lost_or_doubled(self):
        first, one = await self.tab(label='first')
        for index in range(20):
            await first.send_input('shell', one['epoch'], b'm%02d;' % index)
        await first.wait_output('shell', b'm19;>')
        second = await self.wire(label='second')
        await second.hello(self.session.csrf)
        await first.wait_close(5)
        await second.wait_event('attached')
        # keep typing as soon as the hello was sent; every marker appears exactly once
        epoch = second.of('attached')[0]['epoch']
        for index in range(20, 40):
            await second.send_input('shell', epoch, b'm%02d;' % index)
        await second.wait_output('shell', b'm39;>')
        text = bytes(second.output[SHELL])
        for index in range(40):
            self.assertEqual(text.count(b'm%02d;' % index), 1, index)


# ===================================================================================================
# Slow client / send queue overflow
# ===================================================================================================

class SendQueueTests(Env):
    HEARTBEAT = 5.0  # the silent client below must not be ended by the heartbeat before its queue overflows
    QUEUE_LIMIT = 2 << 20  # smaller than the production 4 MiB so the test stays quick; above a 1 MiB ring replay

    async def test_a_client_that_never_reads_is_closed_4003_and_never_stalls_the_hub(self):
        stuck = await self.wire(label='stuck', read=False, rcvbuf=4096)
        await stuck.hello(self.session.csrf)
        self.assertTrue(await until(lambda: self.lease()['state'] == 'connected', 5))
        await self.ready()
        epoch = self.lease()['epoch']
        lags: list[float] = []
        running = True

        async def ticker():
            while running:
                t0 = time.monotonic()
                await asyncio.sleep(0.01)
                lags.append(time.monotonic() - t0 - 0.01)
        tick = asyncio.create_task(ticker())
        await stuck.send_input('shell', epoch, b'FLOOD400;')  # ~25 MiB of output toward a client that never reads
        self.assertTrue(await until(lambda: self.lease()['state'] == 'none', 10), 'the overflow frees the lease')
        self.assertEqual(self.lease()['epoch'], epoch)
        # Another tab attaches at once while the stuck socket is still open.
        fresh, second = await self.tab(label='fresh')
        self.assertEqual(second['epoch'], epoch + 1)
        await self.type_in(fresh, second['epoch'], b'quick;')
        running = False
        await tick
        self.assertLess(max(lags), 0.5, f'the event loop stalled for {max(lags):.2f}s')
        # The stuck client finally reads: whatever was sent, then the close 4003.
        stuck.start_reader()
        self.assertEqual(await stuck.wait_close(8), 4003)
        rows = await self.audit_has(3)
        self.assertEqual(rows[:2], [('terminal_attach', epoch, None), ('terminal_closed', epoch, None)])
        self.assertNotIn('terminal_timeout', [row[0] for row in rows])

    async def test_a_reading_client_with_a_big_replay_is_not_closed_for_it(self):
        first, one = await self.tab(label='first')
        await first.send_input('shell', one['epoch'], b'FLOOD20;')
        await first.wait_output('shell', b'flood-done;', 20)
        second, two = await self.tab(label='second')  # a 1 MiB replay: at the limit but read at once
        await second.wait_output('shell', b'flood-done;', 20)
        await second.ping(two['epoch'])
        self.assertIsNone(second.close_code)
        self.assertEqual(self.lease()['state'], 'connected')


# ===================================================================================================
# The login session ends -> the open terminal ends with 4401 (logout at once, CLI passwd within ~6 s)
# ===================================================================================================

class SessionEndTests(Env):
    async def post(self, path: str, data: dict, session=None) -> int:
        session = session or self.session
        headers = {'Origin': self.url, 'Cookie': f'wb_hub_session={session.token}'}
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as http:
            async with http.post(self.url + path, data=data, headers=headers, allow_redirects=False) as response:
                return response.status

    async def test_logout_closes_that_sessions_terminal_4401_at_once_and_spares_others(self):
        wire, attached = await self.tab(label='logged-out')
        epoch = attached['epoch']
        await self.type_in(wire, epoch, b'before-logout;')
        other = self.new_session()
        bystander = await self.wire(session=other, label='bystander')  # another session, before its hello
        self.remember_procs()
        t0 = time.monotonic()
        self.assertEqual(await self.post('/logout', {'csrf': self.session.csrf}), 303)
        self.assertEqual(await wire.wait_close(3), 4401)
        self.assertLess(time.monotonic() - t0, 1.0, 'logout does not wait for the 5 s session check')
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('none', epoch))
        rows = await self.audit_has(2)
        self.assertEqual(rows, [('terminal_attach', epoch, None), ('terminal_closed', epoch, None)])
        for pid, ticks in self.procs.items():
            self.assertTrue(pid_alive(pid, ticks), 'a logout never ends the PTY')
        again = await self.wire(connect=False)  # the dead cookie opens nothing
        self.assertEqual(await again.open(), 401)
        await bystander.hello(other.csrf)
        self.assertEqual((await bystander.wait_event('attached'))['epoch'], epoch + 1)
        await bystander.wait_output('shell', b'<before-logout;>')  # the set and its replay live on

    async def test_a_cli_password_change_closes_the_terminal_4401_within_about_six_seconds(self):
        """``wb hub passwd`` rewrites the password file; the Hub's 5 s session re-check notices it."""
        wire, attached = await self.tab(label='cli')
        epoch = attached['epoch']
        t0 = time.monotonic()
        await asyncio.get_running_loop().run_in_executor(None, self.passwords.set_password, 'another password 1')
        while wire.close_code is None and time.monotonic() - t0 < 9:
            wire.lenient = True
            await wire.send_json({'t': 'ping', 'epoch': epoch, 'n': 1})  # a live tab: not the heartbeat
            await asyncio.sleep(0.2)
        await wire.wait_close(3)
        elapsed = wire.closed_at - t0
        self.assertEqual(wire.close_code, 4401)
        self.assertLess(elapsed, 6.5, f'closed after {elapsed:.1f}s')
        self.assertEqual((self.lease()['state'], self.lease()['epoch']), ('none', epoch))
        self.assertEqual((await self.audit_has(2))[-1], ('terminal_closed', epoch, None))


# ===================================================================================================
# Hub stop (SPEC: WebSocket closed 1001 before the set ends)
# ===================================================================================================

class HubStopTests(Env):
    async def test_the_listener_stop_closes_every_board_socket_1001_and_leaves_the_lease_none(self):
        pre = await self.wire(label='pre-hello')
        current, attached = await self.tab(label='current')
        taken_over, _ = await self.tab(label='second')
        await current.wait_close(3)
        third = await self.wire(label='fresh-pre-hello')
        self.assertEqual(len(self.app[WEBSOCKETS]), 4)
        epoch = self.lease()['epoch']
        await asyncio.gather(*(socket_.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b'hub stopping')
                               for socket_ in list(self.app[WEBSOCKETS])))  # what ``WebListener.stop`` does
        for wire in (pre, taken_over, third):
            self.assertEqual(await wire.wait_close(5), 1001, wire.label)
        self.assertTrue(await until(lambda: self.lease()['state'] == 'none', 3))
        self.assertEqual(self.lease()['epoch'], epoch)
        rows = await self.audit_has(4)
        self.assertEqual(rows[-1], ('terminal_closed', epoch, None))

    async def test_a_stop_while_a_client_never_reads_still_ends_the_connection(self):
        stuck = await self.wire(label='stuck', read=False, rcvbuf=4096)
        await stuck.hello(self.session.csrf)
        await until(lambda: self.lease()['state'] == 'connected', 5)
        t0 = time.monotonic()
        await asyncio.gather(*(socket_.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b'hub stopping')
                               for socket_ in list(self.app[WEBSOCKETS])))
        self.assertLess(time.monotonic() - t0, board_ws.CLOSE_DEADLINE + 2)
        self.assertEqual(self.lease()['state'], 'none')


# ===================================================================================================
# Audit: epochs only, never content (P2-AC-18)
# ===================================================================================================

class AuditTests(Env):
    SECRET = b'TOPSECRET-INPUT-7f3a'

    async def test_audit_rows_carry_epochs_only_and_no_terminal_content_reaches_the_database(self):
        first, one = await self.tab(label='first')
        await first.wait_output('shell', b'ready:shell;')
        await self.type_in(first, one['epoch'], self.SECRET)
        await first.send_json({'t': 'resize', 'epoch': one['epoch'], 'channel': 'shell', 'rows': 20, 'cols': 20})
        second, two = await self.tab(label='second')
        await self.type_in(second, two['epoch'], self.SECRET + b'-2')
        await second.send_json({'t': 'release', 'epoch': two['epoch']})
        third, three = await self.tab(label='third')
        third.rst()
        await self.audit_has(6)
        await asyncio.sleep(0.3)
        rows = self.audit_rows()
        self.assertEqual([(event, detail['epoch'], detail['previous_epoch']) for event, _, detail in rows],
                         [('terminal_attach', 1, None), ('terminal_takeover', 2, 1), ('terminal_attach', 2, 1),
                          ('terminal_release', 2, None), ('terminal_attach', 3, 2), ('terminal_closed', 3, None)])
        for event, actor, detail in rows:
            self.assertEqual(actor, 'owner')
            self.assertEqual(set(detail), {'epoch', 'previous_epoch'}, event)
        events = [event for event, _, _ in rows]
        self.assertEqual(events.count('terminal_attach'), 3)
        self.assertEqual(events.count('terminal_takeover'), 1)
        self.assertEqual(events.count('terminal_release'), 1)
        self.assertEqual(events.count('terminal_closed'), 1)
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
        self.assertTrue(all(len(json.dumps(detail)) < 60 for _, _, detail in rows))


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

    async def wire(self, label: str, *, cookie='auto', origin='auto') -> Wire:
        headers = [('Host', f'{self.host}:{self.port}')]
        if origin is not None:
            headers.append(('Origin', self.url if origin == 'auto' else origin))
        if cookie is not None:
            headers.append(('Cookie', f'wb_hub_session={self.cookie if cookie == "auto" else cookie}'))
        wire = Wire(self.port, headers, label=label)
        self.wires.append(wire)
        return wire

    async def test_the_real_hub_refuses_bad_upgrades_and_closes_every_board_socket_1001_at_stop(self):
        for kwargs in ({'cookie': None}, {'cookie': 'junk'}, {'origin': 'http://evil.example'}, {'origin': None}):
            wire = await self.wire('refused', **kwargs)
            self.assertIn(await wire.open(), (401, 403), kwargs)
        self.assertIsNone(self.hub.board(), 'a refused upgrade starts nothing')
        old = await self.wire('old')
        self.assertEqual(await old.open(), 101)
        first = await old.attach(self.csrf)
        self.assertEqual(first['heartbeat'], 10)
        current = await self.wire('current')
        self.assertEqual(await current.open(), 101)
        second = await current.attach(self.csrf)
        self.assertEqual(second['epoch'], first['epoch'] + 1)
        await old.wait_event('taken_over')
        await current.wait_output('shell', b'ready:shell;', 20)
        await current.send_input('shell', first['epoch'], b'STALE;')
        await current.send_input('shell', second['epoch'], b'FRESH;')
        await current.wait_output('shell', b'<FRESH;>')
        self.assertEqual(self.record.read_bytes(), b'FRESH;')
        waiting = await self.wire('pre-hello')
        self.assertEqual(await waiting.open(), 101)
        board = self.hub.board()
        pids = {name: (board[name]['pid'], starttime(board[name]['pid'])) for name in ('agent', 'shell')}
        self.assertEqual((board['lease']['state'], board['lease']['epoch']), ('connected', second['epoch']))
        stopping = asyncio.ensure_future(asyncio.to_thread(self.hub.box.stop_hub, self.hub.hub))
        self.assertEqual(await current.wait_close(15), 1001)
        self.assertEqual(await waiting.wait_close(15), 1001)
        self.assertEqual(await stopping, 0)
        for name, (pid, ticks) in pids.items():
            self.assertFalse(pid_alive(pid, ticks), f'{name} PTY process outlived the Hub')
        rows = [row['epoch'] for row in self.hub.audit('terminal_attach')]
        self.assertEqual(sorted(rows), [first['epoch'], second['epoch']])
        self.assertNotIn(b'FRESH', (self.hub.box.state / 'hub.sqlite3').read_bytes())

    async def test_a_cli_password_change_closes_the_open_terminal_4401_within_about_six_seconds(self):
        """The real ``wb hub passwd`` while a tab is attached (the Hub re-checks the login every 5 s)."""
        tab = await self.wire('cli-tab')
        self.assertEqual(await tab.open(), 101)
        attached = await tab.attach(self.csrf)
        await tab.wait_output('shell', b'ready:shell;', 20)
        t0 = time.monotonic()
        result = await asyncio.to_thread(self.hub.box.set_password, 'a second hub password 7')
        self.assertEqual(result.returncode, 0)
        tab.lenient = True
        while tab.close_code is None and time.monotonic() - t0 < 12:
            await tab.send_json({'t': 'ping', 'epoch': attached['epoch'], 'n': 1})
            await asyncio.sleep(0.25)
        await tab.wait_close(3)
        elapsed = tab.closed_at - t0
        self.assertEqual(tab.close_code, 4401)
        self.assertLess(elapsed, 7.5, f'closed {elapsed:.1f}s after the password change started')
        self.assertEqual((await self.lease_state()), 'none')
        self.assertEqual([row['epoch'] for row in self.hub.audit('terminal_closed')], [attached['epoch']])
        self.assertEqual(self.hub.board()['shell']['state'], 'running')

    async def lease_state(self) -> str:
        return ((await asyncio.to_thread(self.hub.board)) or {}).get('lease', {}).get('state')


if __name__ == '__main__':
    unittest.main()
