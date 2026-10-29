"""WBX-D1 behaviours 15-21: the Board WebSocket terminals, protocol v2 (``GET /ws/board/{terminal_id}``; seam T5).

In-process cases serve the real ``create_app`` (real ``hub.sqlite3`` sessions and audit, session/Origin middleware)
on a loopback port with a real :class:`BoardSet` whose terminals run the recorder PTY command of
``support/board_stub.py`` (raw mode; every byte it reads goes to a per-terminal file, then it echoes ``<bytes>``),
and several :class:`~support.ws_client.Tab` connections per terminal. The Board WebSockets get a shortened heartbeat.
Real-Hub cases (``wb _hub --foreground``) check the wiring, the default 10 s heartbeat and the 1001 close at Hub stop.

Expected values: web-board-ux SPEC s4 "WebSocket v2" (messages, close codes, size owner rule, limits) and "감사"
(``terminal_*`` details), the bytes the recorder wrote, ``audit`` rows read back with ``sqlite3``.

Migrated from protocol v1 (WBX-D1): the lease, epochs, ``resume``, ``taken_over``/``taken_elsewhere``, 4001 and
``terminal_takeover`` are gone; each v1 case below is kept as its shared-attach counterpart (see the class notes).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

import aiohttp
from aiohttp import web

from support.board_stub import RECORDER, RecorderLauncher
from support.tcp_proxy import TcpProxy
from support.ws_client import Tab

from workbench.hub import auth
from workbench.hub.board import ws as board_ws
from workbench.hub.board.set import BoardSet
from workbench.hub.web.app import WEBSOCKETS, OriginPolicy, WebServices, create_app

PASSWORD = 'correct horse battery'


def cmdline(pid: int) -> list[str]:
    return Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')[:-1]


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
        self.launcher = RecorderLauncher(self.base / 'terminals', state_dir=self.state)
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
        self.records: dict[str, Path] = {}
        self.addAsyncCleanup(self.shutdown)
        self.session = self.services.sessions.create('127.0.0.1', 'test', time.time(), generation=self.generation)

    async def shutdown(self):
        for tab in self.tabs:
            await tab.close()
        await self.runner.cleanup()
        await self.board.stop()

    async def shell(self) -> str:
        """A new running shell's id (its recorder printed ``ready``)."""
        view = await self.board.create_shell()
        self.note(view['id'])
        await self.ready(view['id'])
        return view['id']

    async def agent(self) -> str:
        """``agent-1`` started (open), its id."""
        agent_id = self.board.views()[0]['id']
        await self.board.open([agent_id])
        await self.board.settled()
        self.note(agent_id)
        await self.ready(agent_id)
        return agent_id

    def note(self, terminal_id: str) -> None:
        """Where the terminal's recorder writes (its argv, once the child has exec'd it)."""
        pid = self.board.view(terminal_id)['pid']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            argv = cmdline(pid)
            if len(argv) >= 5 and argv[1] == '-I' and argv[2].endswith('.py'):  # the stub's script (or a variant)
                self.records[terminal_id] = Path(argv[3])
                return
            time.sleep(0.01)
        raise AssertionError(f'pid {pid} is no recorder: {cmdline(pid)}')

    async def ready(self, terminal_id: str) -> None:
        self.assertTrue(await self.eventually(lambda: b'ready:' in self.board.replay(terminal_id)))

    def tab(self, terminal_id: str, session=None, **kwargs) -> Tab:
        session = session or self.session
        tab = Tab(self.base_url, kwargs.pop('cookie', session.token), terminal_id, **kwargs)
        self.tabs.append(tab)
        return tab

    def login(self, address: str = '127.0.0.2'):
        return self.services.sessions.create(address, 'other', time.time(), generation=self.generation)

    def recorded(self, terminal_id: str) -> bytes:
        try:
            return self.records[terminal_id].read_bytes()
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

    def audit_events(self) -> list[tuple[str, int, int]]:
        """(event, client, clients) of each ``terminal_*`` row."""
        return [(event, detail['client'], detail['clients']) for event, detail in self.audit()]

    async def proxied_tab(self, terminal_id: str) -> tuple[Tab, TcpProxy]:
        proxy = await TcpProxy('127.0.0.1', self.port).start()
        self.addAsyncCleanup(proxy.close)
        tab = Tab(proxy.url, self.session.token, terminal_id, origin=self.base_url,
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

    async def sizes(self, tab: Tab, count: int) -> list[dict]:
        """``tab``'s first ``count`` ``size`` messages (read on until it has them)."""
        def seen():
            return [event for event in tab.events if event.get('t') == 'size']
        if len(seen()) < count:
            await tab.receive_until(lambda item: len(seen()) >= count)
        return seen()[:count]

    async def size_of(self, tab: Tab) -> bytes:
        """The PTY window size as the program sees it, asked through ``tab`` (which makes it the size owner). The
        output is shared: what other clients' input printed is read first, so the answer is this one's."""
        await tab.drain(0.15)
        marker = len(tab.output)
        await tab.send_input(b'SIZE')
        await tab.receive_until(lambda item: b';' in tab.output[marker:] and b'size:' in tab.output[marker:])
        text = bytes(tab.output[marker:])
        return text[text.index(b'size:'):text.index(b';', text.index(b'size:')) + 1]


# --- 15: upgrade and hello ------------------------------------------------------------------------------------

class UpgradeTests(BoardWsCase):
    async def status_of(self, path: str, headers: dict) -> int:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as http:
            try:
                ws = await http.ws_connect(self.base_url + path, headers=headers)
            except aiohttp.WSServerHandshakeError as exc:
                return exc.status
            await ws.close()
            return 101

    async def test_the_upgrade_needs_the_session_cookie_exactly_this_origin_and_a_known_terminal(self):
        shell = await self.shell()
        path = f'/ws/board/{shell}'
        origin = self.base_url
        cookie = f'wb_hub_session={self.session.token}'
        key = self.services.api_keys.issue('editor-one', 'editor', {'all': True}, None, now=time.time())
        self.assertEqual(await self.status_of(path, {'Origin': origin}), 401)
        self.assertEqual(await self.status_of(path, {'Origin': origin, 'Cookie': 'wb_hub_session=junk'}), 401)
        self.assertEqual(await self.status_of(path, {'Origin': origin, 'Authorization': f'Bearer {key.raw}'}), 401)
        self.assertEqual(await self.status_of(path, {'Origin': 'http://evil.example', 'Cookie': cookie}), 403)
        self.assertEqual(await self.status_of(path, {'Origin': 'null', 'Cookie': cookie}), 403)
        self.assertEqual(await self.status_of(path, {'Cookie': cookie}), 403)  # no Origin: a browser always sends one
        self.assertEqual(await self.status_of(path, {'Referer': origin + '/board', 'Cookie': cookie}), 403)
        self.assertEqual(await self.status_of('/ws/board/s-ffffffffffff', {'Origin': origin, 'Cookie': cookie}), 404)
        self.assertEqual(await self.status_of('/ws/board', {'Origin': origin, 'Cookie': cookie}), 404)
        self.assertEqual(await self.status_of(path, {'Origin': origin, 'Cookie': cookie}), 101)


class HelloTests(BoardWsCase):
    """v1 bad-hello cases kept; ``v: 1`` (the old protocol) and its ``resume``/``sizes`` are now refused or ignored."""

    async def closed_with(self, terminal_id: str, send) -> int:
        tab = self.tab(terminal_id)
        await tab.connect()
        await send(tab)
        return await tab.wait_closed(5)

    async def test_no_hello_in_time_closes_4400(self):
        shell = await self.shell()
        self.terminal.hello_timeout = 0.3
        started = time.monotonic()
        self.assertEqual(await self.closed_with(shell, lambda tab: asyncio.sleep(0)), 4400)
        self.assertLess(time.monotonic() - started, 2)

    async def test_a_malformed_first_frame_closes_4400_and_attaches_nothing(self):
        shell = await self.shell()
        bad = [lambda tab: tab.ws.send_str('not json'),
               lambda tab: tab.ws.send_str('[1]'),
               lambda tab: tab.send({'t': 'ping', 'n': 1}),
               lambda tab: tab.hello(self.session.csrf, v=1),
               lambda tab: tab.hello(self.session.csrf, v=True),
               lambda tab: tab.hello(self.session.csrf, v=2.0),
               lambda tab: tab.hello(self.session.csrf, size=['24', 80]),
               lambda tab: tab.hello(self.session.csrf, size=[24]),
               lambda tab: tab.ws.send_str(json.dumps({'t': 'hello', 'v': 1, 'csrf': self.session.csrf,
                                                       'resume': None, 'sizes': {'shell': [30, 100]}})),
               lambda tab: tab.send_input(b'early')]
        for send in bad:
            self.assertEqual(await self.closed_with(shell, send), 4400)
        self.assertEqual(self.terminal.clients(shell), 0)
        self.assertEqual(self.board.view(shell)['clients'], 0)

    async def test_a_wrong_csrf_closes_4403_without_attaching(self):
        shell = await self.shell()
        self.assertEqual(await self.closed_with(shell, lambda tab: tab.hello('x' * 43)), 4403)
        self.assertEqual(await self.closed_with(shell, lambda tab: tab.hello(None)), 4403)
        self.assertEqual(self.terminal.clients(shell), 0)


class AttachTests(BoardWsCase):
    async def test_hello_attaches_with_the_terminal_view_size_clients_and_heartbeat_and_streams_output(self):
        """v1 "hello starts the set": in v2 an attach never starts a process (only the session API does)."""
        agent_id = self.board.views()[0]['id']
        idle = self.tab(agent_id)
        attached = await idle.attach(self.session.csrf, size=(24, 80))
        self.assertEqual((attached['v'], attached['clients'], attached['heartbeat'], attached['size']),
                         (2, 1, self.heartbeat, [24, 80]))
        self.assertEqual((attached['terminal']['id'], attached['terminal']['state']), (agent_id, 'not_started'))
        await asyncio.sleep(0.3)
        self.assertEqual(self.launcher.agent_starts, [], 'an attach starts nothing')
        shell = await self.shell()
        tab = self.tab(shell)
        attached = await tab.attach(self.session.csrf)
        self.assertEqual((attached['terminal']['kind'], attached['terminal']['state'], attached['clients']),
                         ('shell', 'running', 1))
        await tab.wait_output(b'ready:')
        await tab.send_input(b'ls\r')
        await tab.wait_output(b'<ls\r>')
        self.assertEqual(self.recorded(shell), b'ls\r')


# --- 16: two connections, the same replay and live output, input in arrival order ----------------------------

class SharedTests(BoardWsCase):
    """v1 takeover cases: a second tab no longer takes over (no 4001, no ``taken_over``); both stay and see the
    same output, and both type."""

    async def test_a_later_attach_replays_the_earlier_output_and_both_then_get_every_chunk(self):
        shell = await self.shell()
        first = self.tab(shell)
        await first.attach(self.session.csrf)
        await first.send_input(b'one')
        await first.wait_output(b'<one>')
        other = self.login()
        second = self.tab(shell, other)
        attached = await second.attach(other.csrf)
        self.assertEqual(attached['clients'], 2)
        await second.wait_output(b'<one>', timeout=2)  # the replay
        self.assertEqual((await first.event('clients'))['count'], 2)
        await second.send_input(b'two')
        await first.wait_output(b'<two>')
        await second.wait_output(b'<two>')
        await first.send_input(b'FLOOD3;')
        for tab in (first, second):
            await tab.receive_until(lambda item, tab=tab: tab.output.count(b'x' * 65536) >= 3 or
                                    len(tab.output) - tab.output.find(b'FLOOD3;') > 3 * 65536)
        await asyncio.sleep(0.2)
        await first.drain()
        await second.drain()
        # The same stream: the second one's view starts at the replay (the whole ring) and never misses a chunk.
        self.assertEqual(bytes(second.output), bytes(first.output)[bytes(first.output).index(b'ready:'):])
        self.assertIsNone(first.close_code)
        self.assertEqual(self.recorded(shell), b'onetwoFLOOD3;')

    async def test_input_from_both_connections_reaches_the_pty_in_arrival_order(self):
        shell = await self.shell()
        first, second = self.tab(shell), self.tab(shell)
        await first.attach(self.session.csrf)
        await second.attach(self.session.csrf)
        for index in range(20):
            await (first if index % 2 == 0 else second).send_input(b'%c' % (65 + index))
            await asyncio.sleep(0.005)
        expected = bytes(range(65, 85))
        self.assertTrue(await self.eventually(lambda: self.recorded(shell) == expected), self.recorded(shell))

    async def test_a_half_open_connection_never_blocks_a_new_hello(self):
        shell = await self.shell()
        old, proxy = await self.proxied_tab(shell)
        await old.attach(self.session.csrf)
        await old.wait_output(b'ready:')
        proxy.pause()
        new = self.tab(shell)
        started = time.monotonic()
        attached = await new.attach(self.session.csrf)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(attached['clients'], 2)
        await new.send_input(b'NEW')
        await new.wait_output(b'<NEW>')
        proxy.resume()
        await old.wait_output(b'<NEW>')
        self.assertIsNone(old.close_code)

    async def test_two_terminals_are_two_sockets_and_never_mix(self):
        shell_a, shell_b = await self.shell(), await self.shell()
        a, b = self.tab(shell_a), self.tab(shell_b)
        await a.attach(self.session.csrf)
        await b.attach(self.session.csrf)
        await a.send_input(b'to-a')
        await b.send_input(b'to-b')
        await a.wait_output(b'<to-a>')
        await b.wait_output(b'<to-b>')
        self.assertNotIn(b'to-b', a.output)
        self.assertNotIn(b'to-a', b.output)
        self.assertEqual((self.recorded(shell_a), self.recorded(shell_b)), (b'to-a', b'to-b'))


class InputTests(BoardWsCase):
    async def test_an_oversized_frame_is_dropped_with_an_error_and_the_connection_stays(self):
        """v1 ``bad_frame`` (a short or unknown channel header) has no v2 counterpart: frames carry no header."""
        shell = await self.shell()
        tab = self.tab(shell)
        await tab.attach(self.session.csrf)
        await tab.send_input(b'y' * (64 * 1024 + 1))
        self.assertEqual((await tab.event('error'))['reason'], 'too_large')
        await tab.send_input(b'y' * (64 * 1024))  # exactly the limit is accepted
        await tab.send_input(b'ok')
        await tab.wait_output(b'ok>')
        self.assertEqual(self.recorded(shell), b'y' * (64 * 1024) + b'ok')

    async def test_input_without_a_process_is_input_refused(self):
        agent_id = self.board.views()[0]['id']
        tab = self.tab(agent_id)
        await tab.attach(self.session.csrf)
        await tab.send_input(b'nobody')
        self.assertEqual((await tab.event('error'))['reason'], 'input_refused')


# --- 17: the ninth connection --------------------------------------------------------------------------------------

class ClientLimitTests(BoardWsCase):
    async def test_eight_attach_the_ninth_is_refused_4429_and_a_freed_place_attaches_again(self):
        shell = await self.shell()
        logins = [self.session] + [self.login() for _ in range(4)]
        tabs = [self.tab(shell, logins[index // 2]) for index in range(9)]
        for index, tab in enumerate(tabs[:8]):
            attached = await tab.attach(logins[index // 2].csrf)
            self.assertEqual(attached['clients'], index + 1)
        ninth = tabs[8]
        await ninth.connect()
        await ninth.hello(logins[4].csrf)
        self.assertEqual(await ninth.event('refused'), {'t': 'refused', 'reason': 'too_many_clients', 'limit': 8})
        self.assertEqual(await ninth.wait_closed(3), 4429)
        self.assertEqual(self.terminal.clients(shell), 8)
        self.assertTrue(await self.eventually(lambda: self.audit()[-1][0] == 'terminal_denied'))
        self.assertEqual(self.audit()[-1][1]['reason'], 'too_many_clients')
        other_shell = await self.shell()  # the limit is per terminal
        self.assertEqual((await self.tab(other_shell).attach(self.session.csrf))['clients'], 1)
        await tabs[0].send({'t': 'release'})
        self.assertEqual(await tabs[0].wait_closed(3), 1000)
        again = self.tab(shell, logins[4])
        self.assertEqual((await again.attach(logins[4].csrf))['clients'], 8)

    async def test_many_connections_at_once_attach_up_to_the_limit_and_refuse_the_rest(self):
        """v1 "many tabs at once end with one lease": now all attach while places last."""
        shell = await self.shell()
        logins = [self.session] + [self.login() for _ in range(5)]
        tabs = [self.tab(shell, logins[index // 2]) for index in range(10)]
        await asyncio.gather(*(tab.connect() for tab in tabs))
        await asyncio.gather(*(tab.hello(logins[index // 2].csrf) for index, tab in enumerate(tabs)))

        async def outcome(tab: Tab) -> str:
            item = await tab.receive_until(lambda item: isinstance(item, dict) and item['t'] in ('attached', 'refused'))
            return item['t']
        outcomes = await asyncio.gather(*(outcome(tab) for tab in tabs))
        self.assertEqual(sorted(outcomes), ['attached'] * 8 + ['refused'] * 2)
        self.assertEqual(self.terminal.clients(shell), 8)
        self.assertEqual(self.board.view(shell)['clients'], 8)


# --- 18: the size owner ---------------------------------------------------------------------------------------------

class SizeTests(BoardWsCase):
    async def test_hello_size_sets_the_pty_only_for_the_first_connection(self):
        shell = await self.shell()
        first = self.tab(shell)
        attached = await first.attach(self.session.csrf, size=(24, 80))
        self.assertEqual(attached['size'], [24, 80])
        self.assertEqual(await self.size_of(first), b'size:24,80;')
        second = self.tab(shell)
        attached = await second.attach(self.session.csrf, size=(40, 120))
        self.assertEqual(attached['size'], [24, 80], 'a new client is not alone: the PTY keeps its size')
        await first.send_input(b'SIZE')
        await first.wait_output(b'size:24,80;size:24,80;')
        self.assertFalse([event for event in first.events if event['t'] == 'size'])

    async def test_the_last_client_to_type_or_resize_owns_the_size_and_every_change_is_told_once(self):
        shell = await self.shell()
        a, b = self.tab(shell), self.tab(shell)
        await a.attach(self.session.csrf, size=(24, 80))
        await b.attach(self.session.csrf, size=(40, 120))
        told = [{'t': 'size', 'rows': 40, 'cols': 120}, {'t': 'size', 'rows': 24, 'cols': 80},
                {'t': 'size', 'rows': 50, 'cols': 200}]
        self.assertEqual(await self.size_of(b), b'size:40,120;', "b's input makes it the owner: its size first")
        for tab in (a, b):
            self.assertEqual(await self.sizes(tab, 1), told[:1])
        self.assertEqual(await self.size_of(a), b'size:24,80;')
        for tab in (a, b):
            self.assertEqual(await self.sizes(tab, 2), told[:2])
        await b.send({'t': 'resize', 'rows': 50, 'cols': 200})
        for tab in (a, b):
            self.assertEqual(await self.sizes(tab, 3), told)
        self.assertEqual(await self.size_of(b), b'size:50,200;')
        # The owner typing again changes nothing: no ``size`` message, no PTY change.
        self.assertEqual(await self.size_of(b), b'size:50,200;')
        await a.drain()
        await b.drain()
        for tab in (a, b):
            self.assertEqual([event for event in tab.events if event['t'] == 'size'], told)

    async def test_alternating_typing_never_sets_off_a_resize_loop(self):
        """A client never answers ``size`` with ``resize`` (SPEC client rule); the server answers nothing by itself:
        once both stop, no more ``size`` messages come."""
        shell = await self.shell()
        a, b = self.tab(shell), self.tab(shell)
        await a.attach(self.session.csrf, size=(24, 80))
        await b.attach(self.session.csrf, size=(40, 120))
        await b.send_input(b'b')
        await a.send_input(b'a')
        await b.wait_output(b'<a>')
        await asyncio.sleep(0.3)
        await a.drain()
        await b.drain()
        count = len([event for event in a.events if event['t'] == 'size'])
        self.assertEqual(count, 2)
        await asyncio.sleep(0.5)
        await a.drain()
        self.assertEqual(len([event for event in a.events if event['t'] == 'size']), count)

    async def test_the_owner_leaving_keeps_the_size(self):
        shell = await self.shell()
        a, b = self.tab(shell), self.tab(shell)
        await a.attach(self.session.csrf, size=(24, 80))
        await b.attach(self.session.csrf, size=(40, 120))
        await b.send({'t': 'resize', 'rows': 33, 'cols': 99})
        await self.sizes(a, 1)
        await b.send({'t': 'release'})
        await b.wait_closed(3)
        await a.event('clients')
        third = self.tab(shell)
        self.assertEqual((await third.attach(self.session.csrf, size=(10, 10)))['size'], [33, 99])

    async def test_out_of_range_hello_sizes_are_clamped_not_refused(self):
        """Deterministic (WBX-D1): the PTY gets its size before the fork, so no child sets it after the hello."""
        for size, expected in (((0, 5000), b'size:1,500;'), ((-3, 501), b'size:1,500;'), ((501, 1), b'size:500,1;')):
            shell = await self.shell()
            tab = self.tab(shell)
            self.assertEqual((await tab.attach(self.session.csrf, size=size))['size'],
                             [int(part) for part in expected[5:-1].split(b',')])
            self.assertEqual(await self.size_of(tab), expected)

    async def test_resize_accepts_the_spec_bound_500_and_refuses_beyond(self):
        """v1 bound 1000 (task-inbox SPEC) is 500 in v2 (SPEC s4)."""
        shell = await self.shell()
        tab = self.tab(shell)
        await tab.attach(self.session.csrf, size=(24, 80))
        for rows, cols in ((0, 80), (24, 501), ('24', 80), (24, None), (True, 80), (24.0, 80)):
            await tab.send({'t': 'resize', 'rows': rows, 'cols': cols})
            self.assertEqual((await tab.event('error'))['reason'], 'bad_resize')
        self.assertEqual(await self.size_of(tab), b'size:24,80;')
        await tab.send({'t': 'resize', 'rows': 500, 'cols': 500})
        self.assertEqual(await self.sizes(tab, 1), [{'t': 'size', 'rows': 500, 'cols': 500}])
        self.assertEqual(await self.size_of(tab), b'size:500,500;')

    async def test_a_restart_keeps_the_size(self):
        shell = await self.shell()
        tab = self.tab(shell)
        await tab.attach(self.session.csrf, size=(31, 101))
        await self.board.restart(shell)
        self.note(shell)
        await tab.receive_until(lambda item: tab.output.count(b'ready:') >= 2)
        self.assertEqual(await self.size_of(tab), b'size:31,101;')


# --- 19: per-connection heartbeat, session, queue -----------------------------------------------------------------

class PingTests(BoardWsCase):
    async def test_ping_is_answered_with_its_number_and_extra_fields_are_ignored(self):
        """v1 "a non-integer epoch is ignored": v2 has no epoch; such a field changes nothing."""
        shell = await self.shell()
        tab = self.tab(shell)
        await tab.attach(self.session.csrf)
        for n in (1, 2, 99):
            await tab.send({'t': 'ping', 'n': n, 'epoch': True})
            self.assertEqual(await tab.event('pong'), {'t': 'pong', 'n': n})
        await tab.send({'t': 'ping', 'n': 'x'})
        self.assertEqual(await tab.event('pong'), {'t': 'pong', 'n': None})
        await tab.send({'t': 'nonsense'})
        self.assertEqual(await tab.event('error'), {'t': 'error', 'reason': 'bad_message'})


class ReleaseTests(BoardWsCase):
    async def test_release_closes_1000_audits_and_the_process_keeps_running(self):
        shell = await self.shell()
        tab = self.tab(shell)
        await tab.attach(self.session.csrf)
        pid = self.board.view(shell)['pid']
        await tab.send({'t': 'release'})
        self.assertEqual(await tab.wait_closed(2), 1000)
        view = self.board.view(shell)
        self.assertEqual((view['state'], view['pid'], view['clients']), ('running', pid, 0))
        self.assertTrue(await self.eventually(lambda: len(self.audit()) == 2), self.audit())
        name = view['name']
        self.assertEqual(self.audit(), [
            ('terminal_attach', {'terminal_id': shell, 'kind': 'shell', 'name': name, 'client': 1, 'clients': 1}),
            ('terminal_release', {'terminal_id': shell, 'kind': 'shell', 'name': name, 'client': 1, 'clients': 0})])
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.audit()), 2, 'the release close is no second row')

    async def test_one_clients_release_leaves_the_other_attached(self):
        """v1 "a stale epoch release is ignored": in v2 a release only ever ends its own connection."""
        shell = await self.shell()
        a, b = self.tab(shell), self.tab(shell)
        await a.attach(self.session.csrf)
        await b.attach(self.session.csrf)
        await a.send({'t': 'release'})
        self.assertEqual(await a.wait_closed(2), 1000)
        self.assertEqual(await b.event('clients'), {'t': 'clients', 'count': 1})
        await b.send({'t': 'ping', 'n': 5})
        await b.event('pong')
        self.assertIsNone(b.close_code)


class BrowserKillTests(BoardWsCase):
    async def test_a_connection_ending_without_a_close_frame_detaches_within_a_second(self):
        shell = await self.shell()
        tab = self.tab(shell)
        await tab.attach(self.session.csrf)
        tab.kill()
        killed = time.monotonic()
        self.assertTrue(await self.eventually(lambda: self.board.view(shell)['clients'] == 0, 1.0))
        self.assertLess(time.monotonic() - killed, 1.0)
        self.assertTrue(await self.eventually(lambda: self.audit_events()[-1:] == [('terminal_closed', 1, 0)]),
                        self.audit())
        self.assertEqual(self.board.view(shell)['state'], 'running')


class HeartbeatTests(BoardWsCase):
    heartbeat = 0.4
    slack = 0.2  # timeout = 2 x 0.4 + 0.2 = 1.0 s

    async def test_regular_pings_keep_the_connection(self):
        shell = await self.shell()
        tab = self.tab(shell)
        await tab.attach(self.session.csrf)
        for n in range(8):  # 2.4 s, well past one timeout
            await tab.send({'t': 'ping', 'n': n})
            await tab.event('pong')
            await asyncio.sleep(self.heartbeat * 0.75)
        self.assertEqual(self.board.view(shell)['clients'], 1)
        self.assertFalse(tab.ws.closed)

    async def test_half_open_silence_closes_only_that_connection_4002(self):
        shell = await self.shell()
        tab, proxy = await self.proxied_tab(shell)
        await tab.attach(self.session.csrf)
        other = self.tab(shell)
        await other.attach(self.session.csrf)
        await tab.send({'t': 'ping', 'n': 1})
        await tab.event('pong')
        last = time.monotonic()
        proxy.pause()
        pinging = True

        async def keep_pinging():
            n = 0
            while pinging:
                n += 1
                await other.send({'t': 'ping', 'n': n})
                await asyncio.sleep(0.2)
        pinger = asyncio.create_task(keep_pinging())
        try:
            self.assertTrue(await self.eventually(lambda: self.board.view(shell)['clients'] == 1, 3))
            elapsed = time.monotonic() - last
            self.assertGreaterEqual(elapsed, 2 * self.heartbeat + self.slack - 0.1)
            self.assertLess(elapsed, 2 * self.heartbeat + self.slack + 0.4)
            self.assertTrue(await self.eventually(lambda: ('terminal_timeout', 1, 1) in self.audit_events()),
                            self.audit())
            proxy.resume()
            self.assertEqual(await tab.wait_closed(5), 4002)
            await asyncio.sleep(0.5)
        finally:
            pinging = False
            await pinger
        self.assertIsNone(other.close_code)
        self.assertEqual([event for event, _, _ in self.audit_events()],
                         ['terminal_attach', 'terminal_attach', 'terminal_timeout'])

    async def test_after_a_cut_a_new_hello_attaches_again_with_the_replay(self):
        """v1 resume cases: v2 has no resume; the client reconnects with a new hello and gets the whole ring."""
        shell = await self.shell()
        tab, proxy = await self.proxied_tab(shell)
        await tab.attach(self.session.csrf)
        await tab.send_input(b'before')
        await tab.wait_output(b'<before>')
        proxy.pause()
        self.assertTrue(await self.eventually(lambda: self.board.view(shell)['clients'] == 0, 3))
        proxy.resume()
        self.assertEqual(await tab.wait_closed(5), 4002)
        again = self.tab(shell)
        attached = await again.attach(self.session.csrf)
        self.assertEqual(attached['clients'], 1)
        await again.wait_output(b'<before>', timeout=2)


class AttachClockTests(BoardWsCase):
    heartbeat = 0.4
    slack = 0.2

    async def test_the_heartbeat_counts_from_the_attach_not_from_the_upgrade(self):
        shell = await self.shell()
        tab = self.tab(shell)
        await tab.connect()
        await asyncio.sleep(0.9)  # a slow hello, still inside the hello window
        await tab.hello(self.session.csrf)
        await tab.event('attached')
        attached_at = time.monotonic()
        await asyncio.sleep(0.6)
        self.assertEqual(self.board.view(shell)['clients'], 1)
        self.assertTrue(await self.eventually(lambda: self.board.view(shell)['clients'] == 0, 2))
        self.assertGreaterEqual(time.monotonic() - attached_at, 2 * self.heartbeat + self.slack - 0.1)


class SendQueueTests(BoardWsCase):
    async def test_a_client_that_stops_reading_is_closed_4003_and_the_others_keep_streaming(self):
        shell = await self.shell()
        tab, proxy = await self.proxied_tab(shell)
        await tab.attach(self.session.csrf)
        reader = self.tab(shell)
        await reader.attach(self.session.csrf)
        proxy.pause()  # the client reads nothing
        self.assertTrue(self.board.write(shell, b'FLOOD512;'))  # 32 MiB of output
        received = 0

        async def read_all():
            nonlocal received
            while received < 512 * 65536:
                item = await reader._next(20)
                if item is None:  # the reader was closed (e.g. 4002): fail, never spin on a closed socket
                    raise AssertionError(f'the reading client closed ({reader.close_code}) after {received} bytes')
                if isinstance(item, bytes):
                    received += len(item)
                    reader.output.clear()
                    reader.frames.clear()
        await asyncio.wait_for(read_all(), 60)
        self.assertTrue(await self.eventually(lambda: self.board.view(shell)['clients'] == 1, 20))
        started = time.monotonic()
        async with aiohttp.ClientSession() as http:  # the Hub loop is not stuck behind the slow client
            async with http.get(self.base_url + '/login') as response:
                self.assertEqual(response.status, 200)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(await self.eventually(lambda: ('terminal_closed', 1, 1) in self.audit_events()), self.audit())
        self.assertTrue(await self.eventually(lambda: len(self.terminal.connections) == 1, board_ws.CLOSE_WAIT + 2))
        proxy.resume()
        self.assertIn(await tab.wait_closed(20), (4003, 1006))
        self.assertIsNone(reader.close_code)
        self.assertEqual(self.board.view(shell)['state'], 'running')


class ControlQueueTests(BoardWsCase):
    async def test_control_messages_count_toward_the_send_queue_limit(self):
        shell = await self.shell()
        self.terminal.queue_limit = 100  # smaller than ``attached`` alone
        tab = self.tab(shell)
        await tab.connect()
        await tab.hello(self.session.csrf)
        self.assertEqual(await tab.wait_closed(5), 4003)
        self.assertEqual(self.board.view(shell)['clients'], 0)

    async def test_pong_and_error_replies_are_rate_limited(self):
        shell = await self.shell()
        tab = self.tab(shell)
        await tab.attach(self.session.csrf)
        started = time.monotonic()
        for n in range(400):
            await tab.send({'t': 'ping', 'n': n})
            await tab.ws.send_str('not json')
        await tab.send({'t': 'ping', 'n': -1})
        await asyncio.sleep(0.5)
        while True:
            try:
                await tab._next(0.2)
            except asyncio.TimeoutError:
                break
        # The budget refills for as long as the server is still working through the 801 messages (under load that
        # outlasts the 0.5 s above): the bound is taken over the whole time until the last reply was read.
        elapsed = time.monotonic() - started
        replies = [event for event in tab.events if event['t'] in ('pong', 'error')]
        allowed = board_ws.REPLY_BURST + board_ws.REPLY_RATE * elapsed
        self.assertLessEqual(len(replies), allowed + 1)
        self.assertGreaterEqual(len(replies), board_ws.REPLY_BURST)
        self.assertIsNone(tab.close_code)
        await asyncio.sleep(1.0)  # the budget refills
        await tab.send({'t': 'ping', 'n': 7777})
        self.assertEqual((await tab.receive_until(lambda item: isinstance(item, dict) and item.get('n') == 7777))['t'],
                         'pong')


class SessionEndTests(BoardWsCase):
    async def attached_tab(self, shell: str, session=None) -> Tab:
        session = session or self.session
        tab = self.tab(shell, session)
        await tab.attach(session.csrf)
        return tab

    async def assert_ended(self, tab: Tab, within: float) -> None:
        started = time.monotonic()
        self.assertEqual(await tab.wait_closed(within + 2), 4401)
        self.assertLess(time.monotonic() - started, within)
        self.assertTrue(await self.eventually(lambda: self.audit()[-1][0] == 'terminal_closed'), self.audit())

    async def post(self, path: str, data: dict) -> int:
        headers = {'Origin': self.base_url, 'Cookie': f'wb_hub_session={self.session.token}'}
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as http:
            async with http.post(self.base_url + path, data=data, headers=headers, allow_redirects=False) as response:
                return response.status

    async def test_a_cli_password_change_ends_the_open_terminal_within_the_session_check(self):
        self.terminal.session_check = 0.3
        tab = await self.attached_tab(await self.shell())
        await asyncio.get_running_loop().run_in_executor(None, self.passwords.set_password, 'another password 1')
        await self.assert_ended(tab, 1.5)

    async def test_the_24_hour_expiry_ends_the_open_terminal(self):
        self.terminal.session_check = 0.3
        tab = await self.attached_tab(await self.shell())
        await tab.send({'t': 'ping', 'n': 1})
        await tab.event('pong')
        self.clock_offset = 86400 + 1
        await self.assert_ended(tab, 1.5)

    async def test_a_valid_session_keeps_the_terminal_across_checks(self):
        self.terminal.session_check = 0.2
        tab = await self.attached_tab(await self.shell())
        for n in range(5):
            await asyncio.sleep(0.25)
            await tab.send({'t': 'ping', 'n': n})
            await tab.event('pong')
        self.assertIsNone(tab.close_code)

    async def test_logout_ends_that_sessions_connections_at_once_and_spares_other_sessions(self):
        shell = await self.shell()
        tab = await self.attached_tab(shell)  # session_check stays at its default 5 s
        other_login = self.login()
        bystander = await self.attached_tab(shell, other_login)
        waiting = self.tab(shell, other_login)
        await waiting.connect()  # another session's socket before its hello
        self.assertEqual(await self.post('/logout', {'csrf': self.session.csrf}), 303)
        await self.assert_ended(tab, 1.0)
        await waiting.hello(other_login.csrf)
        self.assertEqual((await waiting.event('attached'))['clients'], 2)
        self.assertIsNone(bystander.close_code)

    async def test_a_web_password_change_ends_every_open_terminal_at_once(self):
        tab = await self.attached_tab(await self.shell())
        status = await self.post('/settings/password', {'csrf': self.session.csrf, 'current': PASSWORD,
                                                        'new': 'a brand new password', 'confirm': 'a brand new password'})
        self.assertEqual(status, 303)
        await self.assert_ended(tab, 1.0)


# --- 20: clients, state, gone ---------------------------------------------------------------------------------------

class StateTests(BoardWsCase):
    async def test_a_restart_or_exit_is_told_as_state_and_the_new_process_streams(self):
        shell = await self.shell()
        tab = self.tab(shell)
        await tab.attach(self.session.csrf)
        old_pid = self.board.view(shell)['pid']
        await self.board.restart(shell)
        new_pid = self.board.view(shell)['pid']
        self.assertNotEqual(new_pid, old_pid)
        state = await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                        and item['terminal']['pid'] == new_pid and not item['terminal']['busy'])
        self.assertEqual((state['terminal']['id'], state['terminal']['state']), (shell, 'running'))
        self.note(shell)
        await tab.receive_until(lambda item: tab.output.count(b'ready:') >= 2)  # the new process, same subscription
        await tab.send_input(b'AFTER')
        await tab.wait_output(b'<AFTER>')

    async def test_a_rename_is_told_to_every_client(self):
        shell = await self.shell()
        a, b = self.tab(shell), self.tab(shell)
        await a.attach(self.session.csrf)
        await b.attach(self.session.csrf)
        await self.board.rename(shell, '빌드')
        for tab in (a, b):
            state = await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                            and item['terminal']['name'] == '빌드')
            self.assertEqual(state['terminal']['clients'], 2)

    async def test_closing_a_shell_sends_gone_then_4404_to_every_client_and_a_new_upgrade_is_404(self):
        shell = await self.shell()
        a, b = self.tab(shell), self.tab(shell)
        await a.attach(self.session.csrf)
        await b.attach(self.session.csrf)
        await self.board.close(shell)
        for tab in (a, b):
            self.assertEqual(await tab.event('gone'), {'t': 'gone'})
            self.assertEqual(await tab.wait_closed(3), 4404)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.tab(shell).connect()
        self.assertEqual(caught.exception.status, 404)
        self.assertTrue(await self.eventually(lambda: [event for event, *_ in self.audit_events()].count(
            'terminal_closed') == 2))

    async def test_deleting_an_agent_sends_gone_then_4404(self):
        agent_id = await self.agent()
        tab = self.tab(agent_id)
        await tab.attach(self.session.csrf)
        await self.board.delete(agent_id)
        self.assertEqual(await tab.event('gone'), {'t': 'gone'})
        self.assertEqual(await tab.wait_closed(3), 4404)

    async def test_the_view_counts_clients_and_every_client_hears_of_a_change(self):
        """v1 ``BoardTerminal.view`` (lease, connections, dropped inputs) is the terminal view's ``clients``."""
        shell = await self.shell()
        self.assertEqual(self.board.view(shell)['clients'], 0)
        a = self.tab(shell)
        await a.attach(self.session.csrf)
        self.assertEqual(self.board.view(shell)['clients'], 1)
        b = self.tab(shell)
        await b.attach(self.session.csrf)
        self.assertEqual(await a.event('clients'), {'t': 'clients', 'count': 2})
        self.assertFalse([event for event in b.events if event['t'] == 'clients'], 'the attacher has it in attached')
        b.kill()
        self.assertEqual(await a.event('clients'), {'t': 'clients', 'count': 1})
        self.assertEqual(self.board.view(shell)['clients'], 1)
        self.assertEqual(self.board.active(), [{'kind': 'board_agent_set', 'terminals': [
            {'id': shell, 'kind': 'shell', 'name': 'shell-1', 'pid': self.board.view(shell)['pid'],
             'state': 'running'}]}], 'connections never change the stop offer')


class HubStopTests(BoardWsCase):
    async def test_the_listener_stop_closes_every_board_socket_1001(self):
        shell = await self.shell()
        a, b = self.tab(shell), self.tab(await self.shell())
        await a.attach(self.session.csrf)
        await b.attach(self.session.csrf)
        sockets = list(self.app[WEBSOCKETS])
        self.assertEqual(len(sockets), 2)
        closing = asyncio.gather(*(socket_.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b'hub stopping')
                                   for socket_ in sockets))  # what ``WebListener.stop`` does
        self.assertEqual(await a.wait_closed(3), 1001)
        self.assertEqual(await b.wait_closed(3), 1001)
        await closing
        self.assertTrue(await self.eventually(lambda: [event for event, *_ in self.audit_events()].count(
            'terminal_closed') == 2), self.audit())


# --- 21: audit ------------------------------------------------------------------------------------------------------

class AuditTests(BoardWsCase):
    async def test_refused_hellos_and_the_ninth_client_are_denied_rows_without_content(self):
        shell = await self.shell()
        name = self.board.view(shell)['name']
        self.terminal.hello_timeout = 0.3
        self.terminal.max_clients = 1
        for send in (lambda tab: asyncio.sleep(0), lambda tab: tab.ws.send_str('junk-hello-text'),
                     lambda tab: tab.hello('wrong-csrf-value')):
            tab = self.tab(shell)
            await tab.connect()
            await send(tab)
            await tab.wait_closed(3)
        holder = self.tab(shell)
        await holder.attach(self.session.csrf)
        late = self.tab(shell)
        await late.connect()
        await late.hello(self.session.csrf)
        await late.event('refused')
        quitter = self.tab(shell)  # a tab that leaves before its hello is no denial
        await quitter.connect()
        await quitter.ws.close()
        await asyncio.sleep(0.3)
        who = {'terminal_id': shell, 'kind': 'shell', 'name': name}
        self.assertEqual(self.audit(), [
            ('terminal_denied', {**who, 'client': 1, 'clients': 0, 'reason': 'hello'}),
            ('terminal_denied', {**who, 'client': 2, 'clients': 0, 'reason': 'hello'}),
            ('terminal_denied', {**who, 'client': 3, 'clients': 0, 'reason': 'csrf'}),
            ('terminal_attach', {**who, 'client': 4, 'clients': 1}),
            ('terminal_denied', {**who, 'client': 5, 'clients': 1, 'reason': 'too_many_clients'})])
        dump = json.dumps(self.audit())
        for secret in ('junk-hello-text', 'wrong-csrf-value', self.session.csrf):
            self.assertNotIn(secret, dump)

    async def test_the_app_cleanup_waits_for_the_terminal_audit_rows(self):
        shell = await self.shell()
        record = self.terminal.audit

        def slow(event, address, detail):  # a busy disk: each row takes a while
            time.sleep(0.3)
            record(event, address, detail)
        self.terminal.audit = slow
        tab = self.tab(shell)
        await tab.attach(self.session.csrf)
        closing = asyncio.gather(*(socket_.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b'hub stopping')
                                   for socket_ in list(self.app[WEBSOCKETS])))  # ``WebListener.stop`` order
        await tab.wait_closed(3)
        await closing
        await self.runner.cleanup()  # then ``hub.sqlite3`` closes
        self.assertEqual(self.audit_events()[-1], ('terminal_closed', 1, 0))

    async def test_no_terminal_input_or_output_ever_reaches_the_audit(self):
        shell = await self.shell()
        tab = self.tab(shell)
        await tab.attach(self.session.csrf)
        await tab.send_input(b'secret-typing')
        await tab.wait_output(b'<secret-typing>')
        await tab.send({'t': 'release'})
        await tab.wait_closed(3)
        await asyncio.sleep(0.2)
        self.assertNotIn(b'secret-typing', (self.state / 'hub.sqlite3').read_bytes())


class ConnectionCapTests(BoardWsCase):
    async def upgrade(self, shell: str, session) -> tuple[int, Tab | None]:
        tab = self.tab(shell, session)
        try:
            await tab.connect()
        except aiohttp.WSServerHandshakeError as exc:
            return exc.status, None
        return 101, tab

    def test_the_shipped_caps_are_128_sockets_8_pending_per_session_and_8_clients(self):
        self.assertEqual((board_ws.MAX_CONNECTIONS, board_ws.MAX_PENDING, board_ws.MAX_CLIENTS), (128, 8, 8))
        terminal = board_ws.BoardTerminal(self.board)
        self.assertEqual((terminal.max_connections, terminal.max_pending, terminal.max_clients), (128, 8, 8))

    async def test_a_session_has_at_most_eight_sockets_before_their_hello(self):
        shell = await self.shell()
        other = self.login()
        self.assertEqual([(await self.upgrade(shell, self.session))[0] for _ in range(8)], [101] * 8)
        self.assertEqual((await self.upgrade(shell, self.session))[0], 429)
        self.assertEqual((await self.upgrade(shell, other))[0], 101)
        greeted = self.tabs[0]
        await greeted.hello(self.session.csrf)
        await greeted.event('attached')
        self.assertEqual((await self.upgrade(shell, self.session))[0], 101, 'an attached socket is no longer pending')

    async def test_at_most_max_connections_sockets_are_open_and_a_closed_one_frees_its_place(self):
        shell = await self.shell()
        self.terminal.max_connections = 6  # the same check as 128, with fewer sockets
        logins = [self.login() for _ in range(3)]
        opened = [(await self.upgrade(shell, login)) for login in logins for _ in range(2)]
        self.assertEqual([status for status, _ in opened], [101] * 6)
        self.assertEqual((await self.upgrade(shell, self.login()))[0], 503)
        await opened[0][1].ws.close()
        self.assertTrue(await self.eventually(lambda: len(self.terminal.connections) == 5))
        self.assertEqual((await self.upgrade(shell, self.login()))[0], 101)


# --- the real Hub ---------------------------------------------------------------------------------------------------

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

    def tab(self, terminal_id: str, base: str | None = None, **kwargs) -> Tab:
        tab = Tab(base or self.base_url, kwargs.pop('cookie', self.cookie), terminal_id, origin=self.base_url,
                  **kwargs)
        self.tabs.append(tab)
        return tab

    async def shell_id(self) -> str:
        await asyncio.to_thread(self.hub.start_set)
        return self.hub.ids['shell']

    def recorded(self) -> bytes:
        try:
            return self.record.read_bytes()
        except FileNotFoundError:
            return b''

    async def clients(self, terminal_id: str) -> int:
        board = (await asyncio.to_thread(self.hub.board)) or {}
        for view in board.get('agents', []) + board.get('shells', []):
            if view['id'] == terminal_id:
                return view['clients']
        return -1


class RealHubTests(RealHubCase):
    async def status_of(self, path: str, headers: dict) -> int:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as http:
            try:
                ws = await http.ws_connect(self.base_url + path, headers=headers)
            except aiohttp.WSServerHandshakeError as exc:
                return exc.status
            await ws.close()
            return 101

    async def test_the_hub_refuses_api_keys_missing_sessions_and_other_origins(self):
        """P2-AC-20/13, AC-38: no API key opens a terminal; Origin is exact."""
        agent = (await asyncio.to_thread(self.hub.board))['agents'][0]['id']
        path = f'/ws/board/{agent}'
        self.assertEqual(await self.status_of(path, {'Origin': self.base_url}), 401)
        self.assertEqual(await self.status_of(path, {'Origin': self.base_url, 'Authorization': 'Bearer wbk_x_y'}), 401)
        self.assertEqual(await self.status_of(path, {'Origin': 'http://localhost:1',
                                                     'Cookie': f'wb_hub_session={self.cookie}'}), 403)
        self.assertEqual(await self.status_of('/ws/board', {'Origin': self.base_url,
                                                            'Cookie': f'wb_hub_session={self.cookie}'}), 404)
        board = await asyncio.to_thread(self.hub.board)
        self.assertEqual(([view['state'] for view in board['agents']], board['shells']), (['not_started'], []),
                         'no refused upgrade starts anything')

    async def test_two_connections_type_release_and_reattach_with_replay_and_audit(self):
        """AC-32 on the real Hub: two connections share the shell; another login session sees the earlier output."""
        shell = await self.shell_id()
        first = self.tab(shell)
        await first.attach(self.csrf)
        await first.wait_output(b'ready:shell', 20)
        await first.send_input(b'secret-typing\r')
        await first.wait_output(b'<secret-typing\r>')
        second = self.tab(shell)
        self.assertEqual((await second.attach(self.csrf))['clients'], 2)
        await second.wait_output(b'<secret-typing\r>', 5)  # replay
        await second.send_input(b'NEW')
        await first.wait_output(b'<NEW>')
        self.assertEqual(self.recorded(), b'secret-typing\rNEW')
        await first.send({'t': 'release'})
        self.assertEqual(await first.wait_closed(3), 1000)
        self.assertEqual(await self.clients(shell), 1)
        await asyncio.sleep(0.3)
        rows = self.hub.audit('terminal_attach') + self.hub.audit('terminal_release')
        self.assertEqual(sorted((row['client'], row['clients']) for row in self.hub.audit('terminal_attach')),
                         [(1, 1), (2, 2)])
        self.assertEqual([(row['client'], row['clients']) for row in self.hub.audit('terminal_release')], [(1, 1)])
        self.assertTrue(all(row['actor'] == 'owner' and set(row) == {'actor', 'terminal_id', 'kind', 'name', 'client',
                                                                     'clients'} for row in rows), rows)
        self.assertEqual(self.hub.audit('terminal_takeover'), [])
        database = (self.hub.box.state / 'hub.sqlite3').read_bytes()
        self.assertNotIn(b'secret-typing', database)

    async def test_hub_stop_closes_the_terminal_1001(self):
        shell = await self.shell_id()
        tab = self.tab(shell)
        await tab.attach(self.csrf)
        await tab.wait_output(b'ready:shell', 20)
        stopping = asyncio.ensure_future(asyncio.to_thread(self.hub.box.stop_hub, self.hub.hub))
        self.assertEqual(await tab.wait_closed(10), 1001)  # read while the Hub stops: the client answers the close
        self.assertEqual(await stopping, 0)
        # The Hub wrote the terminal's last row before it closed ``hub.sqlite3``.
        self.assertEqual([(row['client'], row['clients']) for row in self.hub.audit('terminal_closed')], [(1, 0)])


class RealHubHeartbeatTests(RealHubCase):
    async def test_the_default_heartbeat_ends_a_half_open_connection_within_21_seconds(self):
        """The shipped 10 s interval: two missed pings plus 1 s."""
        shell = await self.shell_id()
        proxy = await TcpProxy(self.host, self.port).start()
        self.addAsyncCleanup(proxy.close)
        tab = self.tab(shell, proxy.url, headers={'Host': f'{self.host}:{self.port}'})
        attached = await tab.attach(self.csrf)
        self.assertEqual(attached['heartbeat'], 10)
        await tab.wait_output(b'ready:shell', 20)
        await tab.send({'t': 'ping', 'n': 1})
        await tab.event('pong')
        last = time.monotonic()
        proxy.pause()
        while await self.clients(shell) == 1 and time.monotonic() - last < 25:
            await asyncio.sleep(0.25)
        elapsed = time.monotonic() - last
        self.assertEqual(await self.clients(shell), 0)
        self.assertGreaterEqual(elapsed, 20.0)
        self.assertLess(elapsed, 21.0 + 0.6)  # + the status poll
        self.assertEqual([(row['client'], row['clients']) for row in self.hub.audit('terminal_timeout')], [(1, 0)])
        proxy.resume()
        self.assertEqual(await tab.wait_closed(10), 4002)


if __name__ == '__main__':
    unittest.main()
