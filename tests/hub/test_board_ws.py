"""TI-17: the Board WebSocket terminal protocol (``GET /ws/board``; seam T5).

In-process cases serve the real ``create_app`` (real ``hub.sqlite3`` sessions and audit, session/Origin middleware)
on a loopback port with a real :class:`BoardSet` whose two channels are a recorder PTY command (raw mode; every
byte it reads from its terminal goes to a per-channel file, then it echoes ``<bytes>``). The Board terminal gets a
shortened heartbeat. Real-Hub cases (``wb _hub --foreground``) check the wiring, the default 10 s heartbeat and the
1001 close at Hub stop.

Expected values: SPEC "WebSocket terminal 프로토콜" (message and close codes), D-28's six cases, the bytes the
recorder wrote, ``audit`` rows read back with ``sqlite3`` and the lease in ``status``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import textwrap
import time
import unittest

import aiohttp
from aiohttp import web

from support.tcp_proxy import TcpProxy
from support.ws_client import Tab

from workbench.hub import auth
from workbench.hub.board import ws as board_ws
from workbench.hub.board.set import BoardSet
from workbench.hub.web.app import WEBSOCKETS, OriginPolicy, WebServices, create_app

PASSWORD = 'correct horse battery'
RECORDER = textwrap.dedent('''\
    import os, sys, tty
    record = sys.argv[1]
    try:
        tty.setraw(0)
    except Exception:
        pass
    os.write(1, b"ready:" + sys.argv[2].encode() + b"\\r\\n")
    out = os.open(record, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    while True:
        data = os.read(0, 65536)
        if not data:
            break
        os.write(out, data)
        if data.startswith(b"SIZE"):
            size = os.get_terminal_size(0)
            os.write(1, b"size:%d,%d;" % (size.lines, size.columns))
            continue
        if data.startswith(b"FLOOD"):
            chunk = b"x" * 65536
            for _ in range(int(data[5:].split(b";")[0])):
                os.write(1, chunk)
            continue
        os.write(1, b"<" + data + b">")
    ''')


@dataclass
class Started:
    argv: list
    env: dict
    cwd: str


class RecorderLauncher:
    """Both channels run the recorder; the agent's isolation check passes at once."""

    def __init__(self, base: Path):
        self.base = base
        self.script = base / 'recorder.py'
        self.script.write_text(RECORDER)

    def started(self, name: str) -> Started:
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.base), 'TERM': 'xterm-256color'}
        return Started([sys.executable, '-I', str(self.script), str(self.base / f'{name}.stdin'), name], env,
                       str(self.base))

    def prepare_shell(self):
        return self.started('shell')

    async def prepare_agent(self):
        return self.started('agent')

    async def check_agent(self, started, cancel=None):
        return None

    async def close(self):
        pass


class BoardWsCase(unittest.IsolatedAsyncioTestCase):
    heartbeat = 2.0  # 5 s of silence ends a connection; HeartbeatTests shorten it further
    slack = 1.0

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-ws-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / 'hub'
        self.passwords = auth.PasswordStore(self.state)
        self.generation = self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(self.state)
        self.addCleanup(self.db.close)
        self.clock_offset = 0.0  # moves the Hub's clock (session expiry)
        self.services = WebServices(self.passwords, self.db, OriginPolicy('127.0.0.1'),
                                    clock=lambda: time.time() + self.clock_offset)
        self.launcher = RecorderLauncher(self.base)
        self.board = BoardSet(self.launcher, grace=1.0)
        self.terminal = board_ws.BoardTerminal(self.board, heartbeat=self.heartbeat, slack=self.slack,
                                               audit=board_ws.audit_to(self.services))
        self.app = create_app(self.services, routes=[board_ws.routes(lambda: self.terminal)])
        self.runner = web.AppRunner(self.app, access_log=None, handle_signals=False, shutdown_timeout=2)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.port = self.runner.addresses[0][1]
        self.base_url = f'http://127.0.0.1:{self.port}'
        self.tabs: list[Tab] = []
        self.addAsyncCleanup(self.shutdown)
        self.session = self.services.sessions.create('127.0.0.1', 'test', time.time(), generation=self.generation)

    async def shutdown(self):
        for tab in self.tabs:
            await tab.close()
        await self.runner.cleanup()
        await self.board.stop()

    def tab(self, session=None, **kwargs) -> Tab:
        session = session or self.session
        tab = Tab(self.base_url, kwargs.pop('cookie', session.token), **kwargs)
        self.tabs.append(tab)
        return tab

    def recorded(self, channel: str) -> bytes:
        try:
            return (self.base / f'{channel}.stdin').read_bytes()
        except FileNotFoundError:
            return b''

    def audit(self, prefix: str = 'terminal_') -> list[tuple[str, dict]]:
        db = sqlite3.connect(f'file:{self.state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            rows = db.execute('SELECT event, detail_json FROM audit WHERE event LIKE ? ORDER BY id',
                              (prefix + '%',)).fetchall()
        finally:
            db.close()
        return [(event, json.loads(detail)) for event, detail in rows]

    async def proxied_tab(self) -> tuple[Tab, TcpProxy]:
        proxy = await TcpProxy('127.0.0.1', self.port).start()
        self.addAsyncCleanup(proxy.close)
        tab = Tab(proxy.url, self.session.token, origin=self.base_url,
                  headers={'Host': f'127.0.0.1:{self.port}'})
        self.tabs.append(tab)
        return tab, proxy

    async def eventually(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return bool(predicate())


class UpgradeTests(BoardWsCase):
    async def status_of(self, headers: dict) -> int:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as http:
            try:
                ws = await http.ws_connect(self.base_url + '/ws/board', headers=headers)
            except aiohttp.WSServerHandshakeError as exc:
                return exc.status
            await ws.close()
            return 101

    async def test_the_upgrade_needs_the_session_cookie_and_exactly_this_origin(self):
        origin = self.base_url
        cookie = f'wb_hub_session={self.session.token}'
        key = self.services.api_keys.issue('editor-one', 'editor', {'all': True}, None, now=time.time())
        self.assertEqual(await self.status_of({'Origin': origin}), 401)
        self.assertEqual(await self.status_of({'Origin': origin, 'Cookie': 'wb_hub_session=junk'}), 401)
        self.assertEqual(await self.status_of({'Origin': origin, 'Authorization': f'Bearer {key.raw}'}), 401)
        self.assertEqual(await self.status_of({'Origin': 'http://evil.example', 'Cookie': cookie}), 403)
        self.assertEqual(await self.status_of({'Origin': 'null', 'Cookie': cookie}), 403)
        self.assertEqual(await self.status_of({'Cookie': cookie}), 403)  # no Origin: a browser always sends one
        self.assertEqual(await self.status_of({'Referer': origin + '/board', 'Cookie': cookie}), 403)
        self.assertEqual(await self.status_of({'Origin': origin, 'Cookie': cookie}), 101)


class AttachTests(BoardWsCase):
    async def test_hello_starts_the_set_attaches_with_epoch_1_and_streams_both_channels(self):
        tab = self.tab()
        attached = await tab.attach(self.session.csrf)
        self.assertEqual((attached['v'], attached['epoch'], attached['heartbeat']), (1, 1, self.heartbeat))
        self.assertIsInstance(attached['run'], str)
        self.assertTrue(attached['run'])
        self.assertEqual(set(attached['channels']), {'agent', 'shell'})
        self.assertIn(attached['channels']['shell']['state'], ('running', 'starting'))
        await tab.wait_output('shell', b'ready:shell')
        await tab.wait_output('agent', b'ready:agent')
        self.assertTrue(all(epoch == 1 for _, epoch, _ in tab.frames))
        lease = self.board.status()['lease']
        self.assertEqual((lease['state'], lease['epoch']), ('connected', 1))

    async def test_a_later_attach_replays_the_earlier_output_first(self):
        first = self.tab()
        await first.attach(self.session.csrf)
        await first.wait_output('shell', b'ready:shell')
        await first.wait_output('agent', b'ready:agent')
        other_login = self.services.sessions.create('127.0.0.2', 'other', time.time(), generation=self.generation)
        second = self.tab(other_login)
        await second.connect()
        await second.hello(other_login.csrf)
        attached = await second.event('attached')
        self.assertEqual(attached['epoch'], 2)
        # The replay is the first data after ``attached``: both banners, without any new output.
        await second.wait_output('shell', b'ready:shell', timeout=2)
        await second.wait_output('agent', b'ready:agent', timeout=2)


class InputTests(BoardWsCase):
    async def test_current_epoch_input_reaches_the_channel_pty_and_its_echo_comes_back(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        await tab.wait_output('agent', b'ready:agent')
        await tab.send_input('shell', epoch, b'ls\r')
        await tab.send_input('agent', epoch, b'\x1b[A')
        await tab.wait_output('shell', b'<ls\r>')
        await tab.wait_output('agent', b'<\x1b[A>')
        self.assertEqual((self.recorded('shell'), self.recorded('agent')), (b'ls\r', b'\x1b[A'))

    async def test_oversized_or_malformed_input_is_dropped_with_an_error_and_the_connection_stays(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        await tab.send_input('shell', epoch, b'y' * (64 * 1024 + 1))
        self.assertEqual((await tab.event('error'))['reason'], 'too_large')
        await tab.ws.send_bytes(b'\x01\x00')
        self.assertEqual((await tab.event('error'))['reason'], 'bad_frame')
        await tab.send_input(7, epoch, b'z')
        self.assertEqual((await tab.event('error'))['reason'], 'bad_frame')
        await tab.send_input('shell', epoch, b'y' * (64 * 1024))  # exactly the limit is accepted
        await tab.send_input('shell', epoch, b'ok')
        await tab.wait_output('shell', b'ok>')
        self.assertEqual(self.recorded('shell'), b'y' * (64 * 1024) + b'ok')


class ResizeTests(BoardWsCase):
    async def test_hello_sizes_and_resize_messages_set_the_pty_window(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf, sizes={'agent': [40, 120], 'shell': [24, 80]}))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        await tab.wait_output('agent', b'ready:agent')
        await tab.send_input('shell', epoch, b'SIZE')
        await tab.send_input('agent', epoch, b'SIZE')
        await tab.wait_output('shell', b'size:24,80;')
        await tab.wait_output('agent', b'size:40,120;')
        await tab.send({'t': 'resize', 'epoch': epoch, 'channel': 'shell', 'rows': 50, 'cols': 200})
        await tab.send_input('shell', epoch, b'SIZE')
        await tab.wait_output('shell', b'size:50,200;')

    async def test_a_bad_resize_is_an_error_and_a_stale_epoch_resize_changes_nothing(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf, sizes={'shell': [24, 80]}))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        for rows, cols, channel in ((0, 80, 'shell'), (24, 1001, 'shell'), ('24', 80, 'shell'), (24, 80, 'tty')):
            await tab.send({'t': 'resize', 'epoch': epoch, 'channel': channel, 'rows': rows, 'cols': cols})
            self.assertEqual((await tab.event('error'))['reason'], 'bad_resize')
        await tab.send({'t': 'resize', 'epoch': epoch + 5, 'channel': 'shell', 'rows': 33, 'cols': 99})
        await tab.send_input('shell', epoch, b'SIZE')
        await tab.wait_output('shell', b'size:24,80;')


class PingTests(BoardWsCase):
    async def test_a_control_with_a_non_integer_epoch_is_ignored(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        self.assertEqual(epoch, 1)
        await tab.send({'t': 'ping', 'epoch': True, 'n': 1})
        await tab.send({'t': 'ping', 'epoch': 1.0, 'n': 2})
        await tab.send({'t': 'ping', 'epoch': epoch, 'n': 3})
        self.assertEqual((await tab.event('pong'))['n'], 3)

    async def test_ping_is_answered_with_its_number(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        for n in (1, 2, 99):
            await tab.send({'t': 'ping', 'epoch': epoch, 'n': n})
            self.assertEqual(await tab.event('pong'), {'t': 'pong', 'n': n})


class ReleaseTests(BoardWsCase):
    async def test_release_frees_the_lease_at_once_closes_1000_and_the_set_keeps_running(self):
        """D-28 (4) / case 1: the tab closes (``pagehide``)."""
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        pids = {name: self.board.status()[name]['pid'] for name in ('agent', 'shell')}
        await tab.send({'t': 'release', 'epoch': epoch})
        self.assertEqual(await tab.wait_closed(2), 1000)
        lease = self.board.status()['lease']
        self.assertEqual((lease['state'], lease['epoch']), ('none', epoch))
        status = self.board.status()
        self.assertEqual({name: (status[name]['state'], status[name]['pid']) for name in pids},
                         {name: ('running', pid) for name, pid in pids.items()})
        self.assertTrue(await self.eventually(lambda: self.audit() == [
            ('terminal_attach', {'epoch': 1, 'previous_epoch': None}),
            ('terminal_release', {'epoch': 1, 'previous_epoch': None})]), self.audit())
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.audit()), 2, 'the release close is no second row')

    async def test_a_stale_epoch_release_is_ignored(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.send({'t': 'release', 'epoch': epoch + 1})
        await tab.send({'t': 'ping', 'epoch': epoch, 'n': 5})
        await tab.event('pong')
        self.assertEqual(self.board.status()['lease']['state'], 'connected')


class BrowserKillTests(BoardWsCase):
    async def test_a_connection_ending_without_a_close_frame_frees_the_lease_within_a_second(self):
        """Case 2 (browser killed): the transport ends, no close frame."""
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        tab.kill()
        killed = time.monotonic()
        self.assertTrue(await self.eventually(lambda: self.board.status()['lease']['state'] == 'none', 1.0))
        self.assertLess(time.monotonic() - killed, 1.0)
        self.assertEqual(self.board.status()['lease']['epoch'], epoch)
        self.assertTrue(await self.eventually(lambda: self.audit()[-1:] == [
            ('terminal_closed', {'epoch': epoch, 'previous_epoch': None})]), self.audit())
        self.assertEqual(self.board.status()['shell']['state'], 'running')


class HeartbeatTests(BoardWsCase):
    heartbeat = 0.4
    slack = 0.2  # timeout = 2 x 0.4 + 0.2 = 1.0 s

    async def test_regular_pings_keep_the_lease(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        for n in range(8):  # 2.4 s, well past one timeout
            await tab.send({'t': 'ping', 'epoch': epoch, 'n': n})
            await tab.event('pong')
            await asyncio.sleep(self.heartbeat * 0.75)
        self.assertEqual(self.board.status()['lease']['state'], 'connected')
        self.assertFalse(tab.ws.closed)

    async def test_half_open_silence_closes_4002_after_two_intervals_and_slack(self):
        """Case 2 (half-open): the proxy holds both directions; nothing arrives, nothing ends."""
        tab, proxy = await self.proxied_tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        await tab.send({'t': 'ping', 'epoch': epoch, 'n': 1})
        await tab.event('pong')
        last = time.monotonic()
        proxy.pause()
        self.assertTrue(await self.eventually(lambda: self.board.status()['lease']['state'] == 'none', 3))
        elapsed = time.monotonic() - last
        self.assertGreaterEqual(elapsed, 2 * self.heartbeat + self.slack - 0.1)
        self.assertLess(elapsed, 2 * self.heartbeat + self.slack + 0.4)
        self.assertEqual(self.board.status()['lease']['epoch'], epoch)
        self.assertTrue(await self.eventually(lambda: self.audit()[-1:] == [
            ('terminal_timeout', {'epoch': epoch, 'previous_epoch': None})]), self.audit())
        proxy.resume()
        self.assertEqual(await tab.wait_closed(5), 4002)
        await asyncio.sleep(0.2)
        self.assertEqual([event for event, _ in self.audit()], ['terminal_attach', 'terminal_timeout'])


class AttachClockTests(BoardWsCase):
    heartbeat = 0.4
    slack = 0.2

    async def test_the_heartbeat_counts_from_the_attach_not_from_the_upgrade(self):
        tab = self.tab()
        await tab.connect()
        await asyncio.sleep(0.9)  # a slow hello, still inside the hello window
        await tab.hello(self.session.csrf)
        await tab.event('attached')
        attached_at = time.monotonic()
        await asyncio.sleep(0.6)
        self.assertEqual(self.board.status()['lease']['state'], 'connected')
        self.assertTrue(await self.eventually(lambda: self.board.status()['lease']['state'] == 'none', 2))
        self.assertGreaterEqual(time.monotonic() - attached_at, 2 * self.heartbeat + self.slack - 0.1)


class DeniedAuditTests(BoardWsCase):
    async def test_refused_hellos_and_taken_elsewhere_are_audited_as_denied_without_content(self):
        self.terminal.hello_timeout = 0.3
        for send in (lambda tab: asyncio.sleep(0), lambda tab: tab.ws.send_str('junk-hello-text'),
                     lambda tab: tab.hello('wrong-csrf-value')):
            tab = self.tab()
            await tab.connect()
            await send(tab)
            await tab.wait_closed(3)
        holder = self.tab()
        attached = await holder.attach(self.session.csrf)
        late = self.tab()
        await late.connect()
        await late.hello(self.session.csrf, resume={'run': attached['run'], 'epoch': 0})
        await late.event('taken_elsewhere')
        quitter = self.tab()  # a tab that leaves before its hello is no denial
        await quitter.connect()
        await quitter.ws.close()
        await asyncio.sleep(0.3)
        self.assertEqual(self.audit(), [('terminal_denied', {'epoch': 0, 'previous_epoch': None})] * 3 + [
            ('terminal_attach', {'epoch': 1, 'previous_epoch': None}),
            ('terminal_denied', {'epoch': 1, 'previous_epoch': None})])
        dump = json.dumps(self.audit())
        for secret in ('junk-hello-text', 'wrong-csrf-value', self.session.csrf):
            self.assertNotIn(secret, dump)


class TakeoverTests(BoardWsCase):
    async def test_a_new_tab_takes_over_at_once_and_the_old_one_is_told_and_closed_4001(self):
        """D-28 (1): another login session's new tab wins immediately and sees the earlier output (P2-AC-34)."""
        old = self.tab()
        await old.attach(self.session.csrf)
        await old.wait_output('shell', b'ready:shell')
        other_login = self.services.sessions.create('127.0.0.2', 'other', time.time(), generation=self.generation)
        new = self.tab(other_login)
        attached = await new.attach(other_login.csrf)
        self.assertEqual(attached['epoch'], 2)
        self.assertEqual(await old.event('taken_over', 2), {'t': 'taken_over', 'epoch': 2})
        self.assertEqual(await old.wait_closed(2), 4001)
        await new.wait_output('shell', b'ready:shell', timeout=2)
        lease = self.board.status()['lease']
        self.assertEqual((lease['state'], lease['epoch']), ('connected', 2))
        self.assertTrue(await self.eventually(lambda: self.audit() == [
            ('terminal_attach', {'epoch': 1, 'previous_epoch': None}),
            ('terminal_takeover', {'epoch': 2, 'previous_epoch': 1}),
            ('terminal_attach', {'epoch': 2, 'previous_epoch': 1})]), self.audit())
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.audit()), 3, "the taken-over socket's close is no extra row")
        self.assertEqual(self.board.status()['lease']['state'], 'connected')

    async def test_a_left_over_connected_lease_never_blocks_a_new_hello(self):
        """D-28 invariant (case 6): the old tab is half-open (still "connected"); the new hello wins at once."""
        old, proxy = await self.proxied_tab()
        await old.attach(self.session.csrf)
        await old.wait_output('shell', b'ready:shell')
        proxy.pause()
        new = self.tab()
        started = time.monotonic()
        attached = await new.attach(self.session.csrf)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(attached['epoch'], 2)
        self.assertEqual(self.board.status()['lease'], {**self.board.status()['lease'], 'state': 'connected',
                                                         'epoch': 2})
        proxy.resume()
        self.assertEqual((await old.event('taken_over'))['epoch'], 2)
        self.assertEqual(await old.wait_closed(5), 4001)


class StaleInputTests(BoardWsCase):
    async def test_old_tab_input_right_after_a_takeover_never_reaches_the_pty(self):
        """D-28 (2) / case 5: the old tab types at the moment of the takeover, with its epoch and with the new one."""
        old, proxy = await self.proxied_tab()
        old_epoch = (await old.attach(self.session.csrf))['epoch']
        await old.wait_output('shell', b'ready:shell')
        proxy.pause()  # the old tab's typing is in flight while the new tab says hello
        for payload in (b'OLD1', b'OLD2'):
            await old.send_input('shell', old_epoch, payload)
        await old.send_input('shell', old_epoch + 1, b'OLDFORGED')
        new = self.tab()
        new_epoch = (await new.attach(self.session.csrf))['epoch']
        proxy.resume()
        for payload in (b'OLD3', b'OLD4'):
            try:
                await old.send_input('shell', old_epoch, payload)
                await old.send_input('shell', new_epoch, payload + b'F')
            except (ConnectionError, aiohttp.ClientError):
                break
        await old.wait_closed(5)
        await new.send_input('shell', new_epoch, b'NEW')
        await new.wait_output('shell', b'<NEW>')
        await asyncio.sleep(0.2)
        self.assertEqual(self.recorded('shell'), b'NEW')

    async def test_a_stale_epoch_on_the_current_connection_is_dropped_and_counted(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        before = self.terminal.dropped_inputs
        await tab.send_input('shell', epoch + 1, b'AHEAD')
        await tab.send_input('shell', 0, b'BEHIND')
        await tab.send_input('shell', 0xFFFFFFFF, b'MAX')
        await tab.send_input('shell', epoch, b'NOW')
        await tab.wait_output('shell', b'<NOW>')
        self.assertEqual(self.recorded('shell'), b'NOW')
        self.assertEqual(self.terminal.dropped_inputs - before, 3)


class ResumeTests(BoardWsCase):
    heartbeat = 0.4
    slack = 0.2

    async def cut_until_timeout(self) -> tuple[Tab, dict]:
        """Case 3: the network goes away long enough for the server's heartbeat timeout, then comes back."""
        tab, proxy = await self.proxied_tab()
        attached = await tab.attach(self.session.csrf)
        await tab.wait_output('shell', b'ready:shell')
        proxy.pause()
        self.assertTrue(await self.eventually(lambda: self.board.status()['lease']['state'] == 'none', 3))
        return tab, attached, proxy

    async def resume_hello(self, resume: dict) -> Tab:
        tab = self.tab()
        await tab.connect()
        await tab.hello(self.session.csrf, resume=resume)
        return tab

    async def test_after_a_cut_the_same_tab_resumes_with_a_new_epoch_when_nobody_took_over(self):
        tab, attached, proxy = await self.cut_until_timeout()
        proxy.resume()
        self.assertEqual(await tab.wait_closed(5), 4002)
        again = await self.resume_hello({'run': attached['run'], 'epoch': attached['epoch']})
        resumed = await again.event('attached')
        self.assertEqual((resumed['run'], resumed['epoch']), (attached['run'], attached['epoch'] + 1))
        await again.wait_output('shell', b'ready:shell', timeout=2)
        self.assertEqual(self.board.status()['lease']['state'], 'connected')

    async def test_after_a_cut_the_tab_is_told_taken_elsewhere_when_another_tab_attached(self):
        """D-28 (5): the revived old tab sees "다른 곳에서 연결됨" and does not take over by itself."""
        tab, attached, proxy = await self.cut_until_timeout()
        other = self.tab()
        other_epoch = (await other.attach(self.session.csrf))['epoch']
        proxy.resume()
        self.assertEqual(await tab.wait_closed(5), 4002)
        again = await self.resume_hello({'run': attached['run'], 'epoch': attached['epoch']})
        self.assertEqual(await again.event('taken_elsewhere'), {'t': 'taken_elsewhere', 'epoch': other_epoch})
        self.assertEqual(await again.wait_closed(2), 4001)
        lease = self.board.status()['lease']
        self.assertEqual((lease['state'], lease['epoch']), ('connected', other_epoch))
        await other.send({'t': 'ping', 'epoch': other_epoch, 'n': 1})
        await other.event('pong')  # the current tab is untouched
        # "다시 가져오기": a ``resume: null`` hello takes over.
        retake = self.tab()
        self.assertEqual((await retake.attach(self.session.csrf))['epoch'], other_epoch + 1)
        self.assertEqual((await other.event('taken_over'))['epoch'], other_epoch + 1)

    async def test_a_resume_naming_another_run_or_a_future_epoch_never_takes_over(self):
        first = self.tab()
        attached = await first.attach(self.session.csrf)
        for resume in ({'run': 'f' * 16, 'epoch': attached['epoch']}, {'run': attached['run'], 'epoch': 7},
                       {'run': attached['run'], 'epoch': 0xFFFFFFFF}):
            tab = await self.resume_hello(resume)
            self.assertEqual((await tab.event('taken_elsewhere'))['epoch'], attached['epoch'])
            self.assertEqual(await tab.wait_closed(2), 4001)
        await first.send({'t': 'ping', 'epoch': attached['epoch'], 'n': 2})
        await first.event('pong')
        self.assertEqual(self.terminal.epoch, attached['epoch'])

    async def test_a_resume_from_an_earlier_hub_run_attaches_while_this_run_had_no_attach(self):
        tab = await self.resume_hello({'run': 'e' * 16, 'epoch': 41})
        attached = await tab.event('attached')
        self.assertEqual(attached['epoch'], 1)
        self.assertNotEqual(attached['run'], 'e' * 16)

    async def test_the_epoch_past_uint32_starts_a_new_run_at_epoch_1(self):
        first = self.tab()
        old = await first.attach(self.session.csrf)
        await first.wait_output('shell', b'ready:shell')  # the recorder is in raw mode
        self.terminal.epoch = 0xFFFFFFFF  # ~4 billion attaches later
        second = self.tab()
        attached = await second.attach(self.session.csrf)
        self.assertEqual(attached['epoch'], 1)
        self.assertNotEqual(attached['run'], old['run'])
        await second.send_input('shell', 1, b'WRAP')
        await second.wait_output('shell', b'<WRAP>')
        # The old run's epochs are someone else's now.
        tab = await self.resume_hello({'run': old['run'], 'epoch': 1})
        self.assertEqual((await tab.event('taken_elsewhere'))['epoch'], 1)


class SimultaneousTests(BoardWsCase):
    async def outcome(self, tab: Tab) -> str:
        """``current`` for the tab still attached after the dust settles, else its first refusal."""
        try:
            await tab.wait_closed(1.5)
        except (AssertionError, asyncio.TimeoutError):
            return 'current'
        kinds = [event['t'] for event in tab.events if event['t'] in ('taken_over', 'taken_elsewhere')]
        self.assertEqual(tab.close_code, 4001, tab.events)
        return kinds[0] if kinds else f'closed:{tab.close_code}'

    async def race(self, count: int, resume=None) -> list[Tab]:
        # Two tabs per login session (the per-session cap on sockets before their hello).
        logins = [self.session] + [self.services.sessions.create('127.0.0.1', 'race', time.time(),
                                                                 generation=self.generation)
                                   for _ in range((count - 1) // 2)]
        sessions = [logins[index // 2] for index in range(count)]
        tabs = [self.tab(session) for session in sessions]
        await asyncio.gather(*(tab.connect() for tab in tabs))
        await asyncio.gather(*(tab.hello(session.csrf, resume=resume) for tab, session in zip(tabs, sessions)))
        return tabs

    async def test_two_tabs_at_once_leave_exactly_one_lease_and_no_ping_pong(self):
        """Case 4: the first set start is part of the race (both hellos wait for it)."""
        tabs = await self.race(2)
        outcomes = await asyncio.gather(*(self.outcome(tab) for tab in tabs))
        self.assertEqual(sorted(outcomes), ['current', 'taken_over'])
        winner = tabs[outcomes.index('current')]
        attached = [event for event in winner.events if event['t'] == 'attached'][0]
        lease = self.board.status()['lease']
        self.assertEqual((lease['state'], lease['epoch'], self.terminal.epoch), ('connected', attached['epoch'], 2))
        await asyncio.sleep(1.0)
        self.assertEqual((self.board.status()['lease']['epoch'], winner.ws.closed), (2, False))

    async def test_many_tabs_at_once_end_with_the_last_attach_current(self):
        tabs = await self.race(8)
        outcomes = await asyncio.gather(*(self.outcome(tab) for tab in tabs))
        self.assertEqual(outcomes.count('current'), 1, outcomes)
        self.assertEqual(outcomes.count('taken_over'), 7, outcomes)
        winner = tabs[outcomes.index('current')]
        attached = [event for event in winner.events if event['t'] == 'attached'][0]
        self.assertEqual(attached['epoch'], self.terminal.epoch)
        self.assertEqual(self.board.status()['lease']['epoch'], 8)

    async def test_two_resumes_of_the_same_epoch_at_once_attach_one_and_refuse_the_other(self):
        first = self.tab()
        attached = await first.attach(self.session.csrf)
        await first.send({'t': 'release', 'epoch': attached['epoch']})
        await first.wait_closed(2)
        tabs = await self.race(2, resume={'run': attached['run'], 'epoch': attached['epoch']})
        outcomes = await asyncio.gather(*(self.outcome(tab) for tab in tabs))
        self.assertEqual(sorted(outcomes), ['current', 'taken_elsewhere'])
        self.assertEqual(self.board.status()['lease']['epoch'], attached['epoch'] + 1)


class SendQueueTests(BoardWsCase):
    async def test_a_client_that_stops_reading_is_closed_4003_and_the_hub_keeps_answering(self):
        tab, proxy = await self.proxied_tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        proxy.pause()  # the client reads nothing (and its typing would be held: the PTY gets the command directly)
        self.assertTrue(self.board.write('shell', b'FLOOD512;'))  # 32 MiB of output
        self.assertTrue(await self.eventually(lambda: self.board.status()['lease']['state'] == 'none', 20))
        started = time.monotonic()
        async with aiohttp.ClientSession() as http:  # the Hub loop is not stuck behind the slow client
            async with http.get(self.base_url + '/login') as response:
                self.assertEqual(response.status, 200)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.board.status()['lease']['epoch'], epoch)
        self.assertTrue(await self.eventually(lambda: self.audit()[-1:] == [
            ('terminal_closed', {'epoch': epoch, 'previous_epoch': None})]), self.audit())
        # The close cannot reach a client that does not read: the server drops the connection after the close wait.
        self.assertTrue(await self.eventually(lambda: not self.terminal.connections, board_ws.CLOSE_WAIT + 2))
        proxy.resume()
        self.assertIn(await tab.wait_closed(20), (4003, 1006))
        self.assertEqual(self.board.status()['shell']['state'], 'running')


class ControlQueueTests(BoardWsCase):
    async def test_control_messages_count_toward_the_send_queue_limit(self):
        self.terminal.queue_limit = 100  # smaller than ``attached`` alone
        tab = self.tab()
        await tab.connect()
        await tab.hello(self.session.csrf)
        self.assertEqual(await tab.wait_closed(5), 4003)
        self.assertEqual(self.board.status()['lease']['state'], 'none')

    async def test_pong_and_error_replies_are_rate_limited(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        started = time.monotonic()
        for n in range(400):
            await tab.send({'t': 'ping', 'epoch': epoch, 'n': n})
            await tab.ws.send_str('not json')
        await tab.send({'t': 'ping', 'epoch': epoch, 'n': -1})
        await asyncio.sleep(0.5)
        elapsed = time.monotonic() - started
        while True:
            try:
                await tab._next(0.2)
            except asyncio.TimeoutError:
                break
        replies = [event for event in tab.events if event['t'] in ('pong', 'error')]
        allowed = board_ws.REPLY_BURST + board_ws.REPLY_RATE * elapsed
        self.assertLessEqual(len(replies), allowed + 1)
        self.assertGreaterEqual(len(replies), board_ws.REPLY_BURST)
        self.assertIsNone(tab.close_code)
        await asyncio.sleep(1.0)  # the budget refills
        await tab.send({'t': 'ping', 'epoch': epoch, 'n': 7777})
        self.assertEqual((await tab.receive_until(lambda item: isinstance(item, dict) and item.get('n') == 7777))['t'],
                         'pong')


class SessionEndTests(BoardWsCase):
    async def attached_tab(self) -> tuple[Tab, int]:
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        return tab, epoch

    async def assert_ended(self, tab: Tab, within: float) -> None:
        started = time.monotonic()
        self.assertEqual(await tab.wait_closed(within + 2), 4401)
        self.assertLess(time.monotonic() - started, within)
        self.assertEqual(self.board.status()['lease']['state'], 'none')
        self.assertTrue(await self.eventually(lambda: self.audit()[-1][0] == 'terminal_closed'), self.audit())

    async def post(self, path: str, data: dict) -> int:
        headers = {'Origin': self.base_url, 'Cookie': f'wb_hub_session={self.session.token}'}
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as http:
            async with http.post(self.base_url + path, data=data, headers=headers, allow_redirects=False) as response:
                return response.status

    async def test_a_cli_password_change_ends_the_open_terminal_within_the_session_check(self):
        self.terminal.session_check = 0.3
        tab, epoch = await self.attached_tab()
        await asyncio.get_running_loop().run_in_executor(None, self.passwords.set_password, 'another password 1')
        await self.assert_ended(tab, 1.5)

    async def test_the_24_hour_expiry_ends_the_open_terminal(self):
        self.terminal.session_check = 0.3
        tab, epoch = await self.attached_tab()
        await tab.send({'t': 'ping', 'epoch': epoch, 'n': 1})
        await tab.event('pong')
        self.clock_offset = 86400 + 1
        await self.assert_ended(tab, 1.5)

    async def test_a_valid_session_keeps_the_terminal_across_checks(self):
        self.terminal.session_check = 0.2
        tab, epoch = await self.attached_tab()
        for n in range(5):
            await asyncio.sleep(0.25)
            await tab.send({'t': 'ping', 'epoch': epoch, 'n': n})
            await tab.event('pong')
        self.assertIsNone(tab.close_code)

    async def test_logout_ends_that_sessions_terminal_at_once_and_spares_other_sessions(self):
        tab, epoch = await self.attached_tab()  # session_check stays at its default 5 s
        other_login = self.services.sessions.create('127.0.0.2', 'other', time.time(), generation=self.generation)
        bystander = self.tab(other_login)
        await bystander.connect()  # another session's socket before its hello
        self.assertEqual(await self.post('/logout', {'csrf': self.session.csrf}), 303)
        await self.assert_ended(tab, 1.0)
        await bystander.hello(other_login.csrf)
        self.assertEqual((await bystander.event('attached'))['epoch'], epoch + 1)

    async def test_a_web_password_change_ends_every_open_terminal_at_once(self):
        tab, epoch = await self.attached_tab()
        status = await self.post('/settings/password', {'csrf': self.session.csrf, 'current': PASSWORD,
                                                        'new': 'a brand new password', 'confirm': 'a brand new password'})
        self.assertEqual(status, 303)
        await self.assert_ended(tab, 1.0)


class ChannelStateTests(BoardWsCase):
    async def test_a_restart_or_exit_is_told_to_the_current_tab_and_the_new_process_streams(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        old_pid = self.board.status()['shell']['pid']
        await self.board.restart('shell')
        new_pid = self.board.status()['shell']['pid']
        self.assertNotEqual(new_pid, old_pid)
        state = await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                        and (item['channels']['shell'] or {}).get('pid') == new_pid)
        self.assertEqual(state['channels']['shell']['state'], 'running')
        self.assertEqual(set(state['channels']), {'agent', 'shell'})
        tab.output['shell'].clear()
        await tab.wait_output('shell', b'ready:shell')  # the new process, same subscription
        await tab.send_input('shell', epoch, b'AFTER')
        await tab.wait_output('shell', b'<AFTER>')


class HubStopTests(BoardWsCase):
    async def test_the_listener_stop_closes_every_board_socket_1001_and_frees_the_lease(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        sockets = list(self.app[WEBSOCKETS])
        self.assertEqual(len(sockets), 1)
        closing = asyncio.gather(*(socket_.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b'hub stopping')
                                   for socket_ in sockets))  # what ``WebListener.stop`` does
        self.assertEqual(await tab.wait_closed(3), 1001)
        await closing
        self.assertTrue(await self.eventually(lambda: self.board.status()['lease']['state'] == 'none', 2))
        self.assertTrue(await self.eventually(lambda: self.audit()[-1:] == [
            ('terminal_closed', {'epoch': epoch, 'previous_epoch': None})]), self.audit())


class ViewTests(BoardWsCase):
    async def test_the_board_page_view_shows_the_lease_and_the_dropped_input_count(self):
        self.assertEqual(self.terminal.view(), {'lease': {'state': 'none', 'epoch': 0, 'since': None},
                                                'connections': 0, 'dropped_inputs': 0})
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.send_input('shell', epoch + 1, b'stale')
        await tab.send({'t': 'ping', 'epoch': epoch, 'n': 1})
        await tab.event('pong')
        view = self.terminal.view()
        self.assertEqual((view['lease']['state'], view['lease']['epoch'], view['connections'], view['dropped_inputs']),
                         ('connected', 1, 1, 1))
        self.assertEqual(view['lease'], self.board.status()['lease'])


class SizeBoundTests(BoardWsCase):
    async def test_resize_accepts_the_spec_bound_1000(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        await tab.send({'t': 'resize', 'epoch': epoch, 'channel': 'shell', 'rows': 1000, 'cols': 1000})
        await tab.send_input('shell', epoch, b'SIZE')
        await tab.wait_output('shell', b'size:1000,1000;')

    async def test_out_of_range_hello_sizes_are_clamped_not_refused(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf, sizes={'agent': [0, 5000], 'shell': [-3, 1001]}))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        await tab.wait_output('agent', b'ready:agent')
        await tab.send_input('shell', epoch, b'SIZE')
        await tab.send_input('agent', epoch, b'SIZE')
        await tab.wait_output('shell', b'size:1,1000;')
        await tab.wait_output('agent', b'size:1,1000;')


class AuditFlushTests(BoardWsCase):
    async def test_the_app_cleanup_waits_for_the_terminal_audit_rows(self):
        record = self.terminal.audit

        def slow(event, address, detail):  # a busy disk: each row takes a while
            time.sleep(0.3)
            record(event, address, detail)
        self.terminal.audit = slow
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        closing = asyncio.gather(*(socket_.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b'hub stopping')
                                   for socket_ in list(self.app[WEBSOCKETS])))  # ``WebListener.stop`` order
        await tab.wait_closed(3)
        await closing
        await self.runner.cleanup()  # then ``hub.sqlite3`` closes
        self.assertEqual(self.audit()[-1], ('terminal_closed', {'epoch': epoch, 'previous_epoch': None}))


class ConnectionCapTests(BoardWsCase):
    async def upgrade(self, session) -> tuple[int, Tab | None]:
        tab = self.tab(session)
        try:
            await tab.connect()
        except aiohttp.WSServerHandshakeError as exc:
            return exc.status, None
        return 101, tab

    def login(self):
        return self.services.sessions.create('127.0.0.1', 'cap', time.time(), generation=self.generation)

    async def test_a_session_has_at_most_two_sockets_before_their_hello(self):
        other = self.login()
        self.assertEqual([(await self.upgrade(self.session))[0] for _ in range(2)], [101, 101])
        self.assertEqual((await self.upgrade(self.session))[0], 429)
        self.assertEqual((await self.upgrade(other))[0], 101)
        greeted = self.tabs[0]
        await greeted.hello(self.session.csrf)
        await greeted.event('attached')
        self.assertEqual((await self.upgrade(self.session))[0], 101, 'an attached socket is no longer pending')

    async def test_at_most_eight_board_sockets_are_open_and_a_closed_one_frees_its_place(self):
        logins = [self.login() for _ in range(4)]
        opened = [(await self.upgrade(login)) for login in logins for _ in range(2)]
        self.assertEqual([status for status, _ in opened], [101] * 8)
        self.assertEqual((await self.upgrade(self.login()))[0], 503)
        await opened[0][1].ws.close()
        self.assertTrue(await self.eventually(lambda: len(self.terminal.connections) == 7))
        self.assertEqual((await self.upgrade(self.login()))[0], 101)
        self.assertIsNone(self.board.status(), 'a refused upgrade starts nothing')


class HelloTests(BoardWsCase):
    async def closed_with(self, send) -> int:
        tab = self.tab()
        await tab.connect()
        await send(tab)
        return await tab.wait_closed(5)

    async def test_no_hello_in_time_closes_4400(self):
        self.terminal.hello_timeout = 0.3
        started = time.monotonic()
        self.assertEqual(await self.closed_with(lambda tab: asyncio.sleep(0)), 4400)
        self.assertLess(time.monotonic() - started, 2)

    async def test_a_malformed_first_frame_closes_4400_and_starts_nothing(self):
        bad = [lambda tab: tab.ws.send_str('not json'),
               lambda tab: tab.ws.send_str('[1]'),
               lambda tab: tab.send({'t': 'ping', 'epoch': 0, 'n': 1}),
               lambda tab: tab.hello(self.session.csrf, v=2),
               lambda tab: tab.hello(self.session.csrf, v=True),
               lambda tab: tab.hello(self.session.csrf, resume={'run': 'x', 'epoch': -1}),
               lambda tab: tab.hello(self.session.csrf, resume={'run': 'x', 'epoch': 1 << 32}),
               lambda tab: tab.hello(self.session.csrf, resume='yes'),
               lambda tab: tab.hello(self.session.csrf, sizes={'shell': ['24', 80]}),
               lambda tab: tab.hello(self.session.csrf, sizes={'shell': [24]}),
               lambda tab: tab.send_input('shell', 0, b'early')]
        for send in bad:
            self.assertEqual(await self.closed_with(send), 4400)
        self.assertIsNone(self.board.status())
        self.assertEqual(self.terminal.epoch, 0)

    async def test_a_wrong_csrf_closes_4403_without_attaching(self):
        self.assertEqual(await self.closed_with(lambda tab: tab.hello('x' * 43)), 4403)
        self.assertEqual(await self.closed_with(lambda tab: tab.hello(None)), 4403)
        self.assertIsNone(self.board.status())


class RealHubCase(unittest.IsolatedAsyncioTestCase):
    """``wb _hub --foreground`` with the fake Board agent and the recorder as ``board_agent.shell``."""

    async def asyncSetUp(self):
        from test_board_stop import BoardHub
        self.scratch = tempfile.TemporaryDirectory(prefix='wb-ws-hub-')
        self.addCleanup(self.scratch.cleanup)
        scratch = Path(self.scratch.name)
        recorder = scratch / 'recorder.py'
        recorder.write_text(RECORDER)
        self.record = scratch / 'shell.stdin'
        # Blocking start-up (processes, HTTP login) off the test loop.
        self.hub = await asyncio.to_thread(
            BoardHub, self, shell_script=f'exec {sys.executable} -I {recorder} {self.record} shell\n')
        self.host, self.port = self.hub.browser.host, self.hub.browser.port
        self.base_url = f'http://{self.host}:{self.port}'
        self.cookie = self.hub.browser.cookies['wb_hub_session']
        self.csrf = await asyncio.to_thread(self.hub.csrf)
        self.tabs: list[Tab] = []
        self.addAsyncCleanup(self.close_tabs)

    async def close_tabs(self):
        for tab in self.tabs:
            await tab.close()

    def tab(self, base: str | None = None, **kwargs) -> Tab:
        tab = Tab(base or self.base_url, kwargs.pop('cookie', self.cookie), origin=self.base_url, **kwargs)
        self.tabs.append(tab)
        return tab

    async def lease(self) -> dict:
        return ((await asyncio.to_thread(self.hub.board)) or {}).get('lease') or {}

    def recorded(self) -> bytes:
        try:
            return self.record.read_bytes()
        except FileNotFoundError:
            return b''


class RealHubTests(RealHubCase):
    async def status_of(self, headers: dict) -> int:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as http:
            try:
                ws = await http.ws_connect(self.base_url + '/ws/board', headers=headers)
            except aiohttp.WSServerHandshakeError as exc:
                return exc.status
            await ws.close()
            return 101

    async def test_the_hub_refuses_api_keys_missing_sessions_and_other_origins(self):
        """P2-AC-20/13: no API key opens the terminal; Origin is exact."""
        key = self.hub.box.cli('hub', 'status', '--json')  # any CLI call: the Hub is up
        self.assertEqual(key.returncode, 0)
        self.assertEqual(await self.status_of({'Origin': self.base_url}), 401)
        self.assertEqual(await self.status_of({'Origin': self.base_url, 'Authorization': 'Bearer wbk_x_y'}), 401)
        self.assertEqual(await self.status_of({'Origin': 'http://localhost:1',
                                               'Cookie': f'wb_hub_session={self.cookie}'}), 403)
        self.assertIsNone(self.hub.board(), 'no refused upgrade starts the set')

    async def test_attach_type_release_and_reattach_with_replay_and_audit(self):
        """Cases 1 and 5 on the real Hub, P2-AC-34 (another login session sees the earlier output)."""
        tab = self.tab()
        epoch = (await tab.attach(self.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell', 20)
        await tab.send_input('shell', epoch, b'secret-typing\r')
        await tab.wait_output('shell', b'<secret-typing\r>')
        lease = await self.lease()
        self.assertEqual((lease['state'], lease['epoch']), ('connected', epoch))
        await tab.send({'t': 'release', 'epoch': epoch})
        self.assertEqual(await tab.wait_closed(3), 1000)
        self.assertEqual((await self.lease())['state'], 'none')
        board = self.hub.board()
        self.assertEqual((board['agent']['state'], board['shell']['state']), ('running', 'running'))
        # Another tab takes over; the old one's typing never reaches the PTY.
        first = self.tab()
        first_epoch = (await first.attach(self.csrf))['epoch']
        second = self.tab()
        second_epoch = (await second.attach(self.csrf))['epoch']
        await first.event('taken_over')
        try:
            await first.send_input('shell', first_epoch, b'OLD')
        except (ConnectionError, aiohttp.ClientError):
            pass
        await second.wait_output('shell', b'<secret-typing\r>', 5)  # replay
        await second.send_input('shell', second_epoch, b'NEW')
        await second.wait_output('shell', b'<NEW>')
        self.assertEqual(self.recorded(), b'secret-typing\rNEW')
        await asyncio.sleep(0.3)
        rows = [row for row in self.hub.audit('terminal_attach') + self.hub.audit('terminal_release')
                + self.hub.audit('terminal_takeover')]
        self.assertEqual(sorted((row['epoch'], row.get('previous_epoch')) for row in self.hub.audit('terminal_attach')),
                         [(1, None), (2, 1), (3, 2)])
        self.assertEqual([(row['epoch'], row['previous_epoch']) for row in self.hub.audit('terminal_takeover')],
                         [(3, 2)])
        self.assertEqual(len(self.hub.audit('terminal_release')), 1)
        self.assertTrue(all(row['actor'] == 'owner' and set(row) == {'actor', 'epoch', 'previous_epoch'}
                            for row in rows), rows)
        database = (self.hub.box.state / 'hub.sqlite3').read_bytes()
        self.assertNotIn(b'secret-typing', database)

    async def test_hub_stop_closes_the_terminal_1001(self):
        tab = self.tab()
        await tab.attach(self.csrf)
        await tab.wait_output('shell', b'ready:shell', 20)
        stopping = asyncio.ensure_future(asyncio.to_thread(self.hub.box.stop_hub, self.hub.hub))
        self.assertEqual(await tab.wait_closed(10), 1001)  # read while the Hub stops: the client answers the close
        self.assertEqual(await stopping, 0)
        # The Hub wrote the terminal's last row before it closed ``hub.sqlite3``.
        self.assertEqual([(row['epoch'], row['previous_epoch']) for row in self.hub.audit('terminal_closed')],
                         [(1, None)])


class RealHubHeartbeatTests(RealHubCase):
    async def test_the_default_heartbeat_ends_a_half_open_connection_within_21_seconds(self):
        """D-28 (3) with the shipped 10 s interval: two missed pings plus 1 s."""
        proxy = await TcpProxy(self.host, self.port).start()
        self.addAsyncCleanup(proxy.close)
        tab = self.tab(proxy.url, headers={'Host': f'{self.host}:{self.port}'})
        attached = await tab.attach(self.csrf)
        self.assertEqual(attached['heartbeat'], 10)
        await tab.wait_output('shell', b'ready:shell', 20)
        await tab.send({'t': 'ping', 'epoch': attached['epoch'], 'n': 1})
        await tab.event('pong')
        last = time.monotonic()
        proxy.pause()
        while (await self.lease()).get('state') == 'connected' and time.monotonic() - last < 25:
            await asyncio.sleep(0.25)
        elapsed = time.monotonic() - last
        self.assertEqual((await self.lease())['state'], 'none')
        self.assertGreaterEqual(elapsed, 20.0)
        self.assertLess(elapsed, 21.0 + 0.6)  # + the status poll
        self.assertEqual([(row['epoch'], row['previous_epoch']) for row in self.hub.audit('terminal_timeout')],
                         [(attached['epoch'], None)])
        proxy.resume()
        self.assertEqual(await tab.wait_closed(10), 4002)


if __name__ == '__main__':
    unittest.main()
