"""WBX-E2 independent adversarial verification (test_designer): the Hub relay of WB project panes.

Expected values come from the SPEC s5 "트랙 E" text (not from the worker's tests or the implementation): exactly one
``share_attach`` per project however many web clients; output byte-identical and contiguous for every client including
late joiners under heavy output; input frames never merged/split and per-sender ordered; refusals only to the sender;
the web never changes the pane size while a TUI is attached; the action API's closed doors; audit rows without input.

Harness: ``test_board_pane.PaneHubCase`` (real Hub with web, real backend + fake OMPs, or the Hub SPEC S4 contract
fixture backend with ``--frames-log``) and, for two cases, the real curses TUI on a PTY (``test_board_pane_tui.Tui``,
user confirmation Q62). Run with ``discover -s tests/hub -p 'test_board_pane*.py'``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / 'src'))

from support.ws_client import Tab  # noqa: E402
from test_board_pane import PaneHubCase, RUNNING_PANES, COMMAND_ID, WITH_COMMAND, refused_upgrade  # noqa: E402
from test_board_pane_tui import Tui  # noqa: E402

HANGUL = re.compile('[가-힣]')


def event_types(tab: Tab, start: int = 0) -> list[str]:
    return [event.get('t') for event in tab.events[start:] if isinstance(event, dict)]


def view_of(tab: Tab) -> dict:
    return [item for item in tab.events if item.get('terminal')][-1]['terminal']


async def state_of(tab: Tab, state: str, timeout: float = 30.0) -> dict:
    if view_of(tab)['state'] == state:
        return view_of(tab)
    item = await tab.receive_until(lambda e: isinstance(e, dict) and e.get('t') == 'state'
                                   and e['terminal']['state'] == state, timeout)
    return item['terminal']


async def keep_reading(tabs, seconds: float) -> None:
    """Read on every tab for ``seconds`` and ping every 5 s as a page does (silence beyond 21 s ends a connection)."""
    deadline = time.monotonic() + seconds
    next_ping = time.monotonic()
    while time.monotonic() < deadline:
        if time.monotonic() >= next_ping:
            next_ping = time.monotonic() + 5
            for tab in tabs:
                if tab.close_code is None:
                    await tab.send({'t': 'ping', 'n': 0})
        await asyncio.gather(*(tab.drain(min(1.0, max(0.05, deadline - time.monotonic()))) for tab in tabs
                               if tab.close_code is None))


class Helpers(PaneHubCase):
    maxDiff = None

    async def hub_stop(self, tabs=()):
        """``wb hub stop`` off the event loop so the tabs answer the Hub's close handshakes meanwhile."""
        loop = asyncio.get_running_loop()
        stop = loop.run_in_executor(None, lambda: self.box.wb('hub', 'stop', timeout=90))
        codes = await asyncio.gather(*(tab.wait_closed(30) for tab in tabs))
        done = await stop
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return codes
    def kinds(self, log: Path) -> list[str]:
        return [line['header']['type'] for line in self.frames(log)]

    def inputs(self, log: Path, pane: str | None = None) -> list[bytes]:
        return [bytes.fromhex(line.get('payload', '')) for line in self.frames(log)
                if line['header']['type'] == 'input' and (pane is None or line['header'].get('pane') == pane)]

    def resizes(self, log: Path) -> list[tuple]:
        return [(line['header']['pane'], line['header']['rows'], line['header']['cols'])
                for line in self.frames(log) if line['header']['type'] == 'resize']

    def shared(self, root: Path) -> int:
        return self.box.status(root)['shared_attachments']

    def pane_size(self, root: Path, pane: str = 'host_shell') -> tuple[int, int]:
        info = self.box.status(root)['panes'][pane]
        return info['rows'], info['cols']

    def audit_dump(self) -> str:
        db = sqlite3.connect(f'file:{self.box.hub_state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            return '\n'.join(str(row) for row in db.execute('SELECT * FROM audit'))
        finally:
            db.close()


# ----------------------------------------------------------------------------------------------------------
class OneAttachTests(Helpers):
    """Exactly one ``share_attach`` per project whatever the number of web clients (SPEC "프로젝트당 연결 하나")."""

    async def test_many_clients_on_all_panes_attaching_at_once_share_one_attachment_per_project(self):
        root_a, id_a = self.project('alpha')
        root_b, id_b = self.project('beta')
        self.start_hub()
        wanted = [f'{id_a}:{pane}' for pane in ('manager', 'worker', 'host') for _ in range(3)]
        wanted += [f'{id_b}:{pane}' for pane in ('manager', 'host') for _ in range(2)]
        tabs = await asyncio.gather(*(self.attach(pane_id) for pane_id in wanted))
        self.assertEqual(len(tabs), 13)
        self.assertEqual((self.shared(root_a), self.shared(root_b)), (1, 1), 'one attach per project, 13 clients')
        for tab in tabs[:3]:  # all releasing leaves the relay for the grace; closing is not re-attaching either
            await tab.send({'t': 'release'})
        await asyncio.sleep(1.5)
        self.assertEqual((self.shared(root_a), self.shared(root_b)), (1, 1))
        extra = await self.attach(f'{id_a}:manager')
        self.assertEqual(self.shared(root_a), 1, 'a reconnect in the grace does not attach again')
        self.assertEqual(extra.events[0]['t'], 'attached')

    async def test_hellos_that_do_not_pass_never_open_a_relay(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        pane_id = f'{project_id}:host'
        bad = self.tab(pane_id)
        await bad.connect()
        await bad.hello('x' * len(self.csrf))
        self.assertEqual(await bad.wait_closed(), 4403)
        silent = self.tab(pane_id)
        await silent.connect()  # connected, never says hello
        wrong_version = self.tab(pane_id)
        await wrong_version.connect()
        await wrong_version.send({'t': 'hello', 'v': 1, 'csrf': self.csrf, 'size': [30, 100]})
        await asyncio.sleep(1.5)
        self.assertEqual(self.shared(root), 0, 'only a hello that passed opens the relay')
        self.assertIs(self.box.status(root)['attached'], False)
        status, _ = await self.api('GET', '/api/board', csrf=False)
        self.assertEqual(status, 200)
        self.assertEqual(self.shared(root), 0, 'GET /api/board never connects to a backend')

    async def test_a_hub_stop_closes_every_web_client_with_1001_and_detaches_every_project(self):
        root_a, id_a = self.project('alpha')
        root_b, id_b = self.project('beta')
        self.start_hub()
        tabs = [await self.attach(f'{id_a}:host'), await self.attach(f'{id_a}:manager'),
                await self.attach(f'{id_b}:worker')]
        self.assertEqual((self.shared(root_a), self.shared(root_b)), (1, 1))
        self.assertEqual(await self.hub_stop(tabs), [1001, 1001, 1001])
        self.assertTrue(await self.eventually(lambda: (self.shared(root_a), self.shared(root_b)) == (0, 0), 10))


class GraceTests(Helpers):
    async def test_the_relay_is_per_project_not_per_pane_so_another_panes_client_keeps_it_open(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES, echo_input=True)
        self.start_hub()
        manager, host = await self.attach(f'{project_id}:manager'), await self.attach(f'{project_id}:host')
        await manager.send({'t': 'release'})
        await manager.wait_closed()
        await keep_reading([host], 32)  # past the 30 s grace of the manager pane's last client
        self.assertNotIn('share_detach', self.kinds(log), 'a host client is still connected')
        await host.send_input(b'still-here;')
        await host.wait_output(b'still-here;', 10)
        left = time.monotonic()
        await host.send({'t': 'release'})
        await host.wait_closed()
        self.assertTrue(await self.eventually(lambda: 'share_detach' in self.kinds(log), 40))
        self.assertGreaterEqual(time.monotonic() - left, 29.0)
        self.assertLessEqual(time.monotonic() - left, 36.0)
        self.assertEqual(self.kinds(log).count('share_attach'), 1)


class PaneIdShapeTests(Helpers):
    """SPEC "Id 판정(upgrade 전): ``^<소문자 canonical UUID>:(manager|worker|host)$``이고 ... 아니면 404"."""

    async def test_an_id_with_a_trailing_line_feed_is_404_on_the_websocket_and_the_action_api(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES)
        self.start_hub()
        for suffix in ('%0A', '%0a'):
            with self.subTest(suffix=suffix):
                self.assertEqual(await refused_upgrade(self.tab(f'{project_id}:host{suffix}')), 404,
                                 'a pane id is exactly <uuid>:<pane>; "...:host\\n" is another string')
        status, reply = await self.api('POST', f'/api/board/panes/{project_id}:host%0A/restart', {})
        self.assertEqual(status, 404, reply)
        self.assertEqual(reply['reason'], 'unknown_terminal')
        self.assertNotIn('restart_pane', self.kinds(log))

    async def test_a_line_feed_id_cannot_hide_connections_from_the_relays_client_count(self):
        """Consequence: such connections are a second client set of the same pane; when they leave the relay thinks
        the pane has no client and detaches under the real clients."""
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES, echo_input=True)
        self.start_hub()
        real = await self.attach(f'{project_id}:host')
        odd = self.tab(f'{project_id}:host%0A')
        try:
            await odd.attach(self.csrf)
        except Exception:
            pass
        await odd.close()
        await keep_reading([real], 33)
        self.assertNotIn('share_detach', self.kinds(log), 'the relay detached while a real client was connected')
        await real.send_input(b'alive;')
        await real.wait_output(b'alive;', 10)


class RelayHandoverTests(Helpers):
    """In-process PaneHub (a tiny grace) over the contract fixture backend."""

    async def test_a_hello_while_the_idle_relay_is_closing_never_gets_a_dead_relay(self):
        import types
        from workbench.hub.board.panes import PaneHub
        root, project_id, _log = self.fixture('alpha', snapshot_json=RUNNING_PANES)
        entry = types.SimpleNamespace(project_id=project_id, name='alpha', root=str(root))
        hub = PaneHub(lambda: [entry], grace=0.05)
        await hub.read()
        self.addAsyncCleanup(hub.stop)
        orphans = 0
        for _ in range(6):
            relay = hub.relay(project_id)
            relay.set_clients('host', 1)
            deadline = time.monotonic() + 10
            while relay.state != 'ready' and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual(relay.state, 'ready')
            relay.set_clients('host', 0)  # idle: the relay closes after the grace
            while not relay.closed:
                await asyncio.sleep(0)
            again = hub.relay(project_id)  # a pane WebSocket's hello arrives while the old relay is closing
            again.set_clients('host', 1)
            await asyncio.sleep(0.4)
            if again.closed or hub.relays.get(project_id) is not again:
                orphans += 1
            again.set_clients('host', 0)
            await asyncio.sleep(0.4)
        self.assertEqual(orphans, 0, 'the client was attached to a relay that is closed and no longer registered')


# ----------------------------------------------------------------------------------------------------------
class IdenticalOutputTests(Helpers):
    async def test_every_client_incl_late_joiners_sees_one_contiguous_stream_under_heavy_output(self):
        _root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        pane_id = f'{project_id}:host'
        first, second = await self.attach(pane_id), await self.attach(pane_id)
        await first.send_input(b"seq -f 'L%07.0f' 1 90000; echo DONE-$((6*7))\r")
        await first.wait_output(b'L0030000\r\n', 60)
        mid = await self.attach(pane_id)  # in the middle of ~900 KB of output (the ring is 256 KiB)
        await first.wait_output(b'L0060000\r\n', 60)
        mid2 = await self.attach(pane_id)
        for tab in (first, second, mid, mid2):
            await tab.wait_output(b'DONE-42', 60)
        for tab in (first, second, mid, mid2):
            await tab.drain(0.5)
        done = await self.attach(pane_id)  # after the output: the pure ring replay
        await done.drain(0.5)
        reference = bytes(first.output)
        self.assertGreater(len(reference), 800_000)
        self.assertEqual(bytes(second.output), reference, 'same replay then the same live bytes')
        for name, tab in (('mid', mid), ('mid2', mid2), ('done', done)):
            data = bytes(tab.output)
            self.assertGreater(len(data), 0, name)
            self.assertTrue(reference.endswith(data), f'{name}: not a contiguous suffix of the live stream '
                                                      f'({len(data)} bytes)')
            numbers = [int(item) for item in re.findall(rb'L(\d{7})\r\n', data)]
            self.assertTrue(numbers, name)
            self.assertEqual(numbers, list(range(numbers[0], numbers[0] + len(numbers))), f'{name}: gap or repeat')
            self.assertEqual(numbers[-1], 90000)
        self.assertLessEqual(len(done.output), 256 * 1024, 'a late joiner gets the 256 KiB ring, not more')
        self.assertGreater(len(done.output), 128 * 1024)
        self.assertNotIn('reset', event_types(mid), 'a late joiner sees no reset (no re-attach happened)')


class ResetScopeTests(Helpers):
    async def test_a_pane_restart_resets_only_that_pane_and_a_late_joiner_never_sees_the_old_session(self):
        _root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        host_a, host_b = await self.attach(f'{project_id}:host'), await self.attach(f'{project_id}:host')
        manager = await self.attach(f'{project_id}:manager')
        await host_a.send_input(b'echo OLD-$((40+2))\r')
        await host_a.wait_output(b'OLD-42')
        before = len(manager.events)
        status, reply = await self.action(f'{project_id}:host', 'kill', {'confirm': True})
        self.assertEqual(status, 200, reply)
        await state_of(host_a, 'exited')
        status, reply = await self.action(f'{project_id}:host', 'restart')
        self.assertEqual(status, 200, reply)
        for tab in (host_a, host_b):
            await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'reset', 30)
        await state_of(host_b, 'running')
        await self.take_host(project_id)
        await host_b.send_input(b'echo NEW-$((40+3))\r')
        await host_b.wait_output(b'NEW-43')
        await host_a.wait_output(b'NEW-43')
        mark_a = bytes(host_a.output).rfind(b'NEW-43')
        self.assertNotIn('reset', event_types(manager, before), 'the manager pane is not reset by the host restart')
        late = await self.attach(f'{project_id}:host')
        await late.drain(0.4)
        self.assertNotIn(b'OLD-', bytes(late.output))
        self.assertGreater(mark_a, 0)
        self.assertEqual(self.shared(self.root_of(project_id)), 1, 'a pane restart is not a re-attach')

    async def test_a_new_backend_resets_every_client_once_and_there_is_still_one_attachment(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        tabs = [await self.attach(f'{project_id}:host'), await self.attach(f'{project_id}:host'),
                await self.attach(f'{project_id}:manager')]
        await tabs[0].send_input(b'echo GEN1-$((40+2))\r')
        await tabs[0].wait_output(b'GEN1-42')
        marks = [len(tab.events) for tab in tabs]
        done = self.box.wb('shutdown', '--yes', cwd=root, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for tab in tabs:
            await state_of(tab, 'off', 40)
        self.box.power_on(root, project_id)
        for tab in tabs:
            await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                    and item['terminal']['state'] == 'running', 40)
        await asyncio.sleep(1.0)
        for tab, mark in zip(tabs, marks):
            self.assertGreaterEqual(event_types(tab, mark).count('reset'), 1, 'every client is told to start over')
        self.assertEqual(event_types(tabs[0], marks[0]).count('reset'), event_types(tabs[1], marks[1]).count('reset'))
        self.assertEqual(self.shared(root), 1)
        late = await self.attach(f'{project_id}:host')
        await late.drain(0.4)
        self.assertNotIn(b'GEN1-', bytes(late.output), 'the Hub ring was emptied for the new backend')


# ----------------------------------------------------------------------------------------------------------
class InputOrderTests(Helpers):
    async def test_three_concurrent_senders_keep_their_own_order_nothing_is_merged_or_lost(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES, echo_input=True)
        self.start_hub()
        pane_id = f'{project_id}:worker'
        senders = [await self.attach(pane_id) for _ in range(3)]
        watcher = await self.attach(pane_id)
        tags = ['A', 'B', 'C']

        async def run(tab: Tab, tag: str) -> None:
            for number in range(40):
                await tab.send_input(b'%s:%03d;' % (tag.encode(), number))
                if number % 3 == 0:
                    await asyncio.sleep(0)
                if number % 7 == 0:
                    await tab.send_input(b'\x03')  # a lone Ctrl+C in the middle of the stream
        await asyncio.gather(*(run(tab, tag) for tab, tag in zip(senders, tags)))
        total = 3 * (40 + 6)
        self.assertTrue(await self.eventually(lambda: len(self.inputs(log)) == total, 20), len(self.inputs(log)))
        await asyncio.sleep(0.5)
        payloads = self.inputs(log)
        self.assertEqual(len(payloads), total)
        for tag in tags:
            mine = [p for p in payloads if p.startswith(tag.encode() + b':')]
            self.assertEqual(mine, [b'%s:%03d;' % (tag.encode(), n) for n in range(40)], f'sender {tag} order')
        self.assertEqual(payloads.count(b'\x03'), 18, 'every lone Ctrl+C is its own lone request')
        for payload in payloads:
            self.assertRegex(payload, rb'^(?:[ABC]:\d{3};|\x03)$', 'a request is exactly one web frame')
        await watcher.wait_output(b'C:039;', 10)
        await keep_reading(senders + [watcher], 1.0)
        for tab in senders + [watcher]:
            self.assertEqual(bytes(tab.output), b''.join(payloads), 'all clients see the backend arrival order')

    async def test_oversized_input_is_refused_alone_and_the_64_kib_limit_itself_passes(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES)
        self.start_hub()
        pane_id = f'{project_id}:manager'
        big, other = await self.attach(pane_id), await self.attach(pane_id)
        await big.send_input(b'a' * 65536)
        self.assertTrue(await self.eventually(lambda: len(self.inputs(log)) == 1))
        await big.send_input(b'b' * 65537)
        error = await big.event('error')
        self.assertEqual(error['reason'], 'too_large')
        await asyncio.sleep(0.5)
        self.assertEqual([len(p) for p in self.inputs(log)], [65536], 'the oversized frame never reached the backend')
        await other.send({'t': 'ping', 'n': 1})
        self.assertEqual((await other.event('pong'))['n'], 1)
        self.assertNotIn('error', event_types(other))
        await big.send_input(b'c' * (1 << 20) + b'c')  # past the WebSocket message limit
        self.assertIn(await big.wait_closed(15), (1009, 1006), 'too big: 1009 (or the TCP reset that races it)')
        await other.send_input(b'z')
        self.assertTrue(await self.eventually(lambda: self.inputs(log)[-1:] == [b'z']))
        self.assertEqual(self.kinds(log).count('share_attach'), 1, 'the relay survived')
        self.assertIsNone(other.close_code)

    async def test_refusal_reasons_go_to_the_sender_only_with_korean_text_and_interleaved_senders_are_not_mixed(self):
        reasons = ['input_owner_manager', 'host_shell_automation', 'input_target_unknown', 'pane_unavailable',
                   'queue_full', 'paste_too_large', 'some_future_reason']
        details, names = {}, {}
        for reason in reasons:
            name = 'p-' + reason.replace('_', '-')
            self.fixture(name, snapshot_json=RUNNING_PANES, replies_json={'input': {'ok': False, 'reason': reason}})
            names[name] = reason
        self.start_hub()
        for entry in self.entries:
            reason = names[entry['name']]
            pane_id = f'{entry["project_id"]}:host'
            first, second, watcher = [await self.attach(pane_id) for _ in range(3)]
            for number in range(3):
                await first.send_input(b'f%d' % number)
                if number < 2:
                    await second.send_input(b's%d' % number)
            await first.drain(1.0)
            await second.drain(1.0)
            await watcher.drain(0.5)
            mine = [e for e in first.events if e.get('t') == 'input_refused']
            theirs = [e for e in second.events if e.get('t') == 'input_refused']
            self.assertEqual([e['reason'] for e in mine], [reason] * 3, reason)
            self.assertEqual([e['reason'] for e in theirs], [reason] * 2, reason)
            self.assertNotIn('input_refused', event_types(watcher), 'a refusal never reaches another client')
            for item in mine + theirs:
                self.assertRegex(item['detail'], HANGUL)
                self.assertNotIn(reason, item['detail'], 'no raw code in the sentence')
                self.assertEqual(set(item), {'t', 'reason', 'detail'})
            details[reason] = mine[0]['detail']
        named = [details[r] for r in reasons[:4]]
        self.assertEqual(len(set(named)), 4, 'a fixed, distinct sentence per host reason')

    async def test_a_sender_that_leaves_while_answers_are_pending_does_not_stall_the_others(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES, echo_input=True)
        self.start_hub()
        pane_id = f'{project_id}:manager'
        leaver, stayer = await self.attach(pane_id), await self.attach(pane_id)
        for number in range(30):
            await leaver.send_input(b'l%02d;' % number)
        leaver.kill()
        await stayer.send_input(b'after-leaver;')
        await stayer.wait_output(b'after-leaver;', 10)
        payloads = self.inputs(log)
        self.assertEqual(payloads.count(b'after-leaver;'), 1)
        mine = [p for p in payloads if p.startswith(b'l')]
        self.assertEqual(mine, [b'l%02d;' % n for n in range(len(mine))], 'what the leaver sent stays ordered')
        self.assertEqual(self.kinds(log).count('share_attach'), 1)


# ----------------------------------------------------------------------------------------------------------
class SizeTests(Helpers):
    async def test_resize_requests_follow_the_last_active_client_and_never_repeat_or_leak_into_other_panes(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES, echo_input=True)
        self.start_hub()
        host = f'{project_id}:host'
        first = await self.attach(host, size=(30, 100))  # equals the pane's size: nothing to send
        await asyncio.sleep(0.5)
        self.assertEqual(self.resizes(log), [])
        for _ in range(40):
            await first.send({'t': 'resize', 'rows': 30, 'cols': 100})
        for bad in ({'rows': 0, 'cols': 10}, {'rows': 9999, 'cols': 10}, {'rows': '30', 'cols': 100},
                    {'rows': True, 'cols': 100}, {'rows': 30}):
            await first.send({'t': 'resize', **bad})
        await first.send({'t': 'ping', 'n': 3})
        await first.event('pong')
        self.assertEqual(self.resizes(log), [], 'same or invalid sizes send nothing')
        second = await self.attach(host, size=(40, 120))
        await asyncio.sleep(0.5)
        self.assertEqual(self.resizes(log), [], 'a second client never resizes by attaching')
        expected = []
        for round_ in range(6):  # alternating typists: each flip is exactly one resize, repeats are none
            for tab, size in ((second, (40, 120)), (first, (30, 100))):
                await tab.send_input(b'k%d;' % round_)
                await tab.send_input(b'm%d;' % round_)
                expected.append(('host_shell', *size))
                count = 2 * len(expected)  # the Hub reads connections concurrently: wait for this pair to arrive
                self.assertTrue(await self.eventually(lambda: len(self.inputs(log, 'host_shell')) >= count, 10))
        await asyncio.sleep(0.5)
        self.assertEqual(self.resizes(log), expected)
        manager = await self.attach(f'{project_id}:manager', size=(25, 90))
        await manager.send_input(b'x')
        self.assertTrue(await self.eventually(lambda: self.resizes(log)[-1:] == [('manager_omp', 25, 90)]))
        self.assertEqual(self.resizes(log)[:-1], expected, 'the manager pane has its own owner')

    async def test_without_a_tui_a_resize_message_alone_moves_the_real_pane_and_the_ninth_client_is_refused(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        pane_id = f'{project_id}:worker'
        first = await self.attach(pane_id, size=(26, 90))
        self.assertTrue(await self.eventually(lambda: self.pane_size(root, 'worker_omp') == (26, 90)))
        second = await self.attach(pane_id, size=(41, 130))
        await second.send({'t': 'resize', 'rows': 41, 'cols': 131})
        self.assertTrue(await self.eventually(lambda: self.pane_size(root, 'worker_omp') == (41, 131)))
        await first.receive_until(lambda e: isinstance(e, dict) and e.get('t') == 'size'
                                  and (e['rows'], e['cols']) == (41, 131), 15)
        self.assertEqual(self.pane_size(root, 'manager_omp')[1] != 131, True, 'other panes keep their size')
        extras = [await self.attach(pane_id) for _ in range(6)]
        self.assertEqual(len(extras), 6)
        ninth = self.tab(pane_id)
        await ninth.connect()
        await ninth.hello(self.csrf)
        self.assertEqual(await ninth.wait_closed(), 4429)
        await extras[0].send({'t': 'release'})
        await extras[0].wait_closed()
        again = await self.attach(pane_id)
        self.assertEqual(again.events[0]['t'], 'attached', 'a freed slot is usable again')


# ----------------------------------------------------------------------------------------------------------
class ActionDoorTests(Helpers):
    async def test_every_closed_door_of_the_action_api_leaves_the_backend_untouched(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=WITH_COMMAND, echo_input=True)
        self.start_hub()
        host, manager = f'{project_id}:host', f'{project_id}:manager'
        await self.attach(host)
        before = self.kinds(log)
        self.assertEqual(before, ['hello', 'share_attach'])
        bearer = {'Authorization': 'Bearer wbk_' + 'a' * 40}
        other = '3b0a1c2e-5d6f-4a7b-8c9d-0e1f2a3b4c5d'
        bad_ids = [f'{other}:host', f'{project_id.upper()}:host', f'{project_id}:shell', f'{project_id}:host:x',
                   project_id]

        async def check(status_set, *args, **kwargs):
            status, reply = await self.api(*args, **kwargs)
            self.assertIn(status, status_set, (args, kwargs, status, reply))
            return status, reply
        base = f'/api/board/panes/{host}/'
        await check({401}, 'POST', base + 'kill', {'confirm': True}, cookie=False, csrf=False)
        await check({401}, 'POST', base + 'kill', {'confirm': True}, cookie=False, csrf=True)
        await check({401}, 'POST', base + 'kill', {'confirm': True}, cookie=False, csrf=False, headers=bearer)
        await check({401}, 'POST', base + 'kill', {'confirm': True}, cookie=False, csrf=True, headers=bearer)
        await check({403}, 'POST', base + 'kill', {'confirm': True}, csrf=False)
        await check({403}, 'POST', base + 'kill', {'confirm': True}, headers={'X-CSRF-Token': 'y' * len(self.csrf)},
                    csrf=False)
        await check({403}, 'POST', base + 'kill', {'confirm': True}, headers={'Origin': 'http://evil.example'})
        await check({404, 405}, 'GET', base + 'kill')
        await check({404, 405}, 'PUT', base + 'kill', {'confirm': True})
        await check({404}, 'POST', base + 'nuke', {})
        for bad in bad_ids:
            for name in ('restart', 'kill', 'takeover_request'):
                await check({404}, 'POST', f'/api/board/panes/{bad}/{name}', {'confirm': True})
        for name in ('takeover_request', 'takeover_confirm', 'handoff', 'kill', 'stop'):
            status, reply = await check({400}, 'POST', f'/api/board/panes/{manager}/{name}',
                                        {'confirm': True, 'command_id': COMMAND_ID} if name == 'stop' else
                                        ({'confirm': True} if name == 'kill' else {}))
            self.assertEqual(reply['reason'], 'wrong_pane')
        for body in ({}, {'confirm': False}, {'confirm': 'true'}, {'confirm': 1}, {'confirm': None}, {'confirm': [True]}):
            status, reply = await check({400}, 'POST', base + 'kill', body)
            self.assertEqual(reply['reason'], 'confirm_required', body)
        for body in ({'confirm': True, 'command_id': 7}, {'confirm': True, 'command_id': ''},
                     {'confirm': True, 'command_id': 'x' * 65}, {'confirm': True}, {'confirm': True, 'command_id': COMMAND_ID,
                                                                                   'extra': 1}):
            status, reply = await check({400}, 'POST', base + 'stop', body)
            self.assertEqual(reply['reason'], 'bad_input', body)
        status, reply = await check({400}, 'POST', base + 'handoff', {'x': 1})
        self.assertEqual(reply['reason'], 'bad_input')
        for payload in ('[]', 'null', '"x"', '{not json', 'x' * 5000, json.dumps({'confirm': True, 'pad': 'x' * 5000})):
            async with self.http.post(self.base_url + base + 'kill', data=payload, headers={
                    'Origin': self.base_url, 'Cookie': f'wb_hub_session={self.cookie}', 'X-CSRF-Token': self.csrf,
                    'Content-Type': 'application/json'}) as response:
                self.assertEqual(response.status, 400, payload[:30])
        await asyncio.sleep(0.5)
        self.assertEqual(self.kinds(log), before, f'no door let a request through: {self.kinds(log)}')

    async def test_an_unreachable_backend_is_503_at_once_and_nothing_is_sent(self):
        _root, project_id, log = self.fixture('alpha', welcome_json={'backend_pid': 1})
        self.start_hub()
        started = time.monotonic()
        status, reply = await self.action(f'{project_id}:host', 'restart')
        self.assertEqual((status, reply['reason']), (503, 'backend_unreachable'), reply)
        self.assertLess(time.monotonic() - started, 9.0)
        self.assertNotIn('restart_pane', self.kinds(log))
        self.assertNotIn('share_attach', self.kinds(log))

    async def test_stop_notice_reaches_only_that_panes_clients_and_unfinished_or_refused_stops_are_audited(self):
        _root, project_id, log = self.fixture(
            'alpha', snapshot_json=WITH_COMMAND,
            replies_json={'stop_command': {'command_id': COMMAND_ID, 'stop': {'status': 'stopping'}}},
            stop_final_json={'status': 'stopped', 'command_id': COMMAND_ID, 'signal': 'SIGINT'})
        _root2, other_id, log2 = self.fixture(
            'beta', snapshot_json=WITH_COMMAND,
            replies_json={'stop_command': {'ok': False, 'reason': 'command_not_found'}})
        _root3, silent_id, log3 = self.fixture(
            'gamma', snapshot_json=WITH_COMMAND,
            replies_json={'stop_command': {'command_id': COMMAND_ID, 'stop': {'status': 'stopping'}}})
        self.start_hub()
        host_a, host_b = await self.attach(f'{project_id}:host'), await self.attach(f'{project_id}:host')
        manager = await self.attach(f'{project_id}:manager')
        elsewhere = await self.attach(f'{other_id}:host')
        silent = await self.attach(f'{silent_id}:host')
        status, reply = await self.action(f'{project_id}:host', 'stop', {'confirm': True, 'command_id': COMMAND_ID})
        self.assertEqual(status, 202, reply)
        for tab in (host_a, host_b):
            notice = await tab.event('notice')
            self.assertEqual((notice['action'], notice['command_id'], notice['stop']['status']),
                             ('stop', COMMAND_ID, 'stopped'))
        status, reply = await self.action(f'{other_id}:host', 'stop', {'confirm': True, 'command_id': COMMAND_ID})
        self.assertEqual((status, reply['reason'], reply['backend_reason']), (409, 'backend_refused',
                                                                                 'command_not_found'))
        status, reply = await self.action(f'{silent_id}:host', 'stop', {'confirm': True, 'command_id': COMMAND_ID})
        self.assertEqual(status, 202)
        for tab in (manager, elsewhere, silent):
            await tab.drain(0.8)
            self.assertNotIn('notice', event_types(tab), 'a notice goes to that pane\'s connections only')
        await self.hub_stop([host_a, host_b, manager, elsewhere, silent])  # the silent stop never got a final result
        rows = [detail for event, detail in self.audit() if event == 'pane_stop']
        outcomes = {detail['project_id']: detail['outcome'] for detail in rows}
        self.assertEqual(outcomes.get(project_id), 'stopped', rows)
        self.assertTrue(outcomes.get(other_id, '').startswith('refused'), rows)
        self.assertEqual(outcomes.get(silent_id), 'unknown', rows)
        self.assertEqual(len(rows), 3, 'one pane_stop row per stop request')
        del log, log2, log3

    async def test_a_timed_out_action_clears_busy_and_is_not_resent_by_the_hub(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES, no_reply='kill_pane')
        self.start_hub()
        host = await self.attach(f'{project_id}:host')
        task = asyncio.ensure_future(self.action(f'{project_id}:host', 'kill', {'confirm': True}))
        busy = await host.receive_until(lambda e: isinstance(e, dict) and e.get('t') == 'state'
                                        and e['terminal']['busy'] is True, 10)
        self.assertTrue(busy['terminal']['busy'])
        status, reply = await task
        self.assertEqual((status, reply['reason']), (504, 'backend_timeout'))
        idle = await host.receive_until(lambda e: isinstance(e, dict) and e.get('t') == 'state'
                                        and e['terminal']['busy'] is False, 10)
        self.assertFalse(idle['terminal']['busy'])
        await asyncio.sleep(1.0)
        self.assertEqual(self.kinds(log).count('kill_pane'), 1)


class OwnerSharedWithTuiTests(Helpers):
    def tui_client(self, root: Path):
        from workbench.backend.client import UiClient
        pid, ticks, record = self.box.backend_ref(root)
        client = UiClient(Path(record['ui_socket']), name='workbench-ui', timeout=10,
                          expected_process={'pid': pid, 'start_ticks': ticks})
        self.addCleanup(client.close)
        answer = client.attach((30, 120))
        self.assertTrue(answer['ok'], answer)
        return client

    async def test_handoff_and_takeover_are_one_backend_state_seen_by_the_tui_and_the_web(self):
        from workbench.contracts.ui_v1 import ClientType
        root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        pane_id = f'{project_id}:host'
        loop = asyncio.get_running_loop()
        tui = await loop.run_in_executor(None, self.tui_client, root)
        tab = await self.attach(pane_id)
        self.assertTrue(view_of(tab)['ui_attached'])
        await self.hand_host(project_id, tab)
        snapshot = await loop.run_in_executor(None, tui.snapshot)
        self.assertEqual(snapshot['panes']['host_shell']['input_owner'], 'manager', 'the TUI sees the web handoff')
        await tab.receive_until(lambda e: isinstance(e, dict) and e.get('t') == 'state'
                                and e['terminal']['input_owner'] == 'manager', 15)
        answer = await loop.run_in_executor(None, lambda: tui.request(ClientType.TAKEOVER_REQUEST))
        self.assertTrue(answer['ok'], answer)
        item = await tab.receive_until(lambda e: isinstance(e, dict) and e.get('t') == 'state' and (
            e['terminal']['input_owner'] == 'user' or 'takeover_confirm' in e['terminal']['actions']), 15)
        if item['terminal']['input_owner'] == 'manager':  # the request waits for a confirm: the web gives it
            self.assertNotIn('takeover_request', item['terminal']['actions'])
            status, reply = await self.action(pane_id, 'takeover_confirm')
            self.assertEqual(status, 200, reply)
            self.assertEqual(reply['pane']['input_owner'], 'user')
        snapshot = await loop.run_in_executor(None, tui.snapshot)
        self.assertEqual(snapshot['panes']['host_shell']['input_owner'], 'user', 'the TUI sees the web confirm')
        rows = [(event, detail.get('input_owner')) for event, detail in self.audit() if event == 'pane_handoff']
        self.assertEqual(rows, [('pane_handoff', 'manager')], 'only web-side actions are audited')

    async def test_a_lone_ctrl_c_from_the_web_interrupts_a_foreground_command_and_a_refused_one_stays_private(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        pane_id = f'{project_id}:host'
        typing, watching = await self.attach(pane_id), await self.attach(pane_id)
        await typing.send_input(b'sleep 300\r')
        await asyncio.sleep(1.0)
        await typing.send_input(b'\x03')  # one frame, one byte
        await watching.wait_output(b'^C', 15)
        await typing.send_input(b'echo AFTER-$((40+2))\r')  # the shell is back at its prompt: sleep was interrupted
        await watching.wait_output(b'AFTER-42', 15)
        await typing.wait_output(b'AFTER-42', 15)
        await self.hand_host(project_id, typing)
        await typing.send_input(b'\x03')
        refused = await typing.event('input_refused')
        self.assertIn(refused['reason'], ('input_owner_manager', 'input_target_unknown'))
        await watching.drain(0.6)
        self.assertNotIn('input_refused', event_types(watching))


# ----------------------------------------------------------------------------------------------------------
class AuditDetailTests(Helpers):
    async def test_rows_carry_only_the_spec_fields_and_failures_name_a_reason_not_an_input(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        host, manager = f'{project_id}:host', f'{project_id}:manager'
        tab = await self.attach(host)
        marker = b'PASTE-MARKER-9f3a' * 40
        await tab.send_input(b'echo ' + marker + b'\r')
        await tab.send_input(b'\x1b[200~' + marker + b'\x1b[201~')
        await tab.wait_output(b'PASTE-MARKER-9f3a', 10)
        mtab = await self.attach(manager)
        await mtab.send_input(b'typed into manager PASTE-MARKER-9f3a')
        status, reply = await self.action(manager, 'restart')
        self.assertEqual((status, reply['backend_reason']), (409, 'pane_alive'))
        await tab.send({'t': 'release'})
        await tab.wait_closed()
        mtab.kill()
        self.assertTrue(await self.eventually(lambda: [e for e, _ in self.audit()].count('pane_closed') == 1))
        rows = self.audit()
        allowed_connection = {'pane_id', 'project_id', 'project_name', 'pane', 'client', 'clients'}
        for event, detail in rows:
            if event in ('pane_attach', 'pane_release', 'pane_closed', 'pane_timeout'):
                self.assertEqual(set(detail), allowed_connection, (event, detail))
        restart = [detail for event, detail in rows if event == 'pane_restart']
        self.assertEqual(restart, [{'pane_id': manager, 'project_id': project_id, 'project_name': 'alpha',
                                    'pane': 'manager', 'ok': False, 'reason': 'pane_alive'}])
        dump = self.audit_dump()
        for needle in ('PASTE-MARKER', '9f3a', 'typed into', '\\x1b', 'echo'):
            self.assertNotIn(needle, dump)
        for path in self.box.hub_state.rglob('*'):  # the Hub log and every other Hub state file
            if path.is_file() and path.stat().st_size < (64 << 20):
                self.assertNotIn(b'PASTE-MARKER', path.read_bytes(), str(path))
        self.assertEqual(len([r for r in rows if r[0] == 'pane_attach']), 2)

    async def test_refused_input_and_resize_leave_no_audit_row(self):
        _root, project_id, _log = self.fixture('alpha', snapshot_json=RUNNING_PANES,
                                               replies_json={'input': {'ok': False, 'reason': 'host_shell_automation'}})
        self.start_hub()
        tab = await self.attach(f'{project_id}:host')
        for number in range(5):
            await tab.send_input(b'x%d' % number)
            await tab.send({'t': 'resize', 'rows': 20 + number, 'cols': 80})
        await tab.drain(1.0)
        await tab.send({'t': 'release'})
        await tab.wait_closed()
        self.assertTrue(await self.eventually(lambda: [e for e, _ in self.audit()] == ['pane_attach', 'pane_release']))
        self.assertEqual([e for e, _ in self.audit('')].count('pane_denied'), 0)


class SessionTests(Helpers):
    async def test_logout_ends_an_open_pane_connection_with_4401_and_a_cookieless_origin_less_upgrade_is_refused(self):
        _root, project_id, _log = self.fixture('alpha', snapshot_json=RUNNING_PANES)
        self.start_hub()
        pane_id = f'{project_id}:host'
        self.assertIn(await refused_upgrade(self.tab(pane_id, cookie=None)), (401, 403))
        self.assertIn(await refused_upgrade(self.tab(pane_id, origin='')), (401, 403))
        self.assertEqual(await refused_upgrade(self.tab(pane_id, origin=self.base_url.replace('127.0.0.1', 'localhost'))),
                         403)
        tab = await self.attach(pane_id)
        status, _headers, _ = self.browser.request('POST', '/logout', {'csrf': self.csrf})
        self.assertIn(status, (200, 303))
        self.assertEqual(await tab.wait_closed(20), 4401)
        late = self.tab(pane_id)
        self.assertEqual(await refused_upgrade(late), 401)


class RealOrderTests(Helpers):
    async def test_concurrent_senders_into_a_real_host_shell_keep_each_ones_order_and_lose_nothing(self):
        _root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        pane_id = f'{project_id}:host'
        tabs = [await self.attach(pane_id) for _ in range(3)]
        await tabs[0].send_input(b'cat\r')
        await asyncio.sleep(1.0)

        async def run(tab: Tab, tag: str) -> None:
            for number in range(40):
                await tab.send_input(b'%s%03d\n' % (tag.encode(), number))
        await asyncio.gather(*(run(tab, tag) for tab, tag in zip(tabs, 'XYZ')))
        await tabs[0].wait_output(b'Z039\r\n', 20)
        await keep_reading(tabs, 1.5)
        reference = bytes(tabs[0].output)
        for tab in tabs[1:]:
            self.assertEqual(bytes(tab.output), reference, 'every client sees the same bytes')
        for tag in 'XYZ':
            positions = [reference.find(b'%s%03d' % (tag.encode(), n)) for n in range(40)]
            self.assertTrue(all(p >= 0 for p in positions), f'{tag}: a line was lost')
            self.assertEqual(positions, sorted(positions), f'{tag}: out of order')
            for n in range(40):
                self.assertEqual(reference.count(b'%s%03d' % (tag.encode(), n)), 2, 'typed echo + cat output')


class HeartbeatAuditTests(Helpers):
    async def test_a_silent_client_is_closed_4002_alone_and_audited_as_pane_timeout(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES, echo_input=True)
        self.start_hub()
        pane_id = f'{project_id}:worker'
        quiet, live = await self.attach(pane_id), await self.attach(pane_id)
        started = time.monotonic()
        while quiet.close_code is None and time.monotonic() - started < 45:
            await live.send({'t': 'ping', 'n': 1})
            await asyncio.gather(live.drain(1.0), quiet.drain(1.0))  # `quiet` reads (so it can take the close) but never pings
        await quiet.wait_closed(15)
        self.assertEqual(quiet.close_code, 4002)
        self.assertIsNone(live.close_code)
        self.assertTrue(await self.eventually(lambda: any(e == 'pane_timeout' for e, _ in self.audit())))
        row = [d for e, d in self.audit() if e == 'pane_timeout'][0]
        self.assertEqual((row['pane_id'], row['clients']), (pane_id, 1))
        self.assertGreaterEqual(time.monotonic() - started, 20.0)
        await live.send_input(b'ok;')
        await live.wait_output(b'ok;', 10)


# ----------------------------------------------------------------------------------------------------------
class RealTuiAndWebTests(Helpers):
    """User-approved (Q62) combination: the real curses TUI on a PTY and web pane WebSockets on one real backend."""

    async def tui(self, root: Path, size=(30, 120)) -> Tui:
        _pid, _ticks, record = self.box.backend_ref(root)
        tui = Tui(record['ui_socket'], self.box.env, size)
        self.addCleanup(tui.close)
        self.assertTrue(await tui.until(lambda: 'MANAGER OMP' in tui.text()), tui.text())
        self.assertTrue(await self.eventually(lambda: self.box.status(root)['attached'] is True))
        return tui

    async def test_web_resize_spam_around_a_tui_attach_never_wins_afterwards(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        tab = await self.attach(f'{project_id}:host', size=(41, 133))
        stop = asyncio.Event()

        async def spam() -> None:
            sizes = [(41, 133), (43, 150), (22, 70), (35, 99)]
            number = 0
            while not stop.is_set():
                rows, cols = sizes[number % len(sizes)]
                await tab.send({'t': 'resize', 'rows': rows, 'cols': cols})
                if number % 5 == 0:
                    await tab.send_input(b':;')
                number += 1
                await asyncio.sleep(0.01)
        spammer = asyncio.ensure_future(spam())
        try:
            await asyncio.sleep(0.8)
            tui = await self.tui(root, (30, 120))
            # the pane sizes are now the TUI's: record, then keep spamming
            await asyncio.sleep(1.5)
            tui_size = self.pane_size(root)
            self.assertNotIn(tui_size, [(41, 133), (43, 150), (22, 70), (35, 99)])
            for _ in range(10):
                await asyncio.sleep(0.4)
                self.assertEqual(self.pane_size(root), tui_size, 'a web resize won over the attached TUI')
        finally:
            stop.set()
            await spammer
        view = view_of(tab)
        state = await tab.receive_until(lambda e: isinstance(e, dict) and e.get('t') == 'state'
                                        and tuple(e['terminal']['size']) == tui_size, 10) if tuple(view['size']) != tui_size else None
        del state
        tui.resize((38, 132))
        self.assertTrue(await self.eventually(lambda: self.pane_size(root) != tui_size, 15), 'the TUI resized')
        new_size = self.pane_size(root)
        await tab.receive_until(lambda e: isinstance(e, dict) and e.get('t') == 'size'
                                and (e['rows'], e['cols']) == new_size, 15)
        await asyncio.sleep(1.0)
        self.assertEqual(self.pane_size(root), new_size)

    async def test_a_stalled_web_client_is_closed_alone_the_tui_stays_attached_and_the_relay_survives(self):
        from support.tcp_proxy import TcpProxy
        root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        tui = await self.tui(root)
        pane_id = f'{project_id}:host'
        proxy = await TcpProxy(self.host, self.port).start()
        self.addAsyncCleanup(proxy.close)
        slow = Tab(proxy.url, self.cookie, pane_id, origin=self.base_url, headers={'Host': f'{self.host}:{self.port}'})
        self.tabs.append(slow)
        await slow.attach(self.csrf)
        reader = await self.attach(pane_id)
        seen = len(reader.events)
        stop_drain = asyncio.Event()

        async def keep_tui_drained() -> None:
            while not stop_drain.is_set():
                tui.drain(0.0)
                await asyncio.sleep(0.01)
        drainer = asyncio.ensure_future(keep_tui_drained())
        try:
            proxy.pause()
            await reader.send_input(b"head -c 12582912 /dev/zero | tr '\\0' x; echo FLOOD-$((1+1))DONE\r")
            tail, received = b'', 0
            while b'FLOOD-2DONE' not in tail:
                item = await reader._next(60)
                self.assertIsNotNone(item, f'the reader closed ({reader.close_code})')
                if isinstance(item, bytes):
                    received += len(item)
                    tail = (tail + item)[-64:]
                    reader.output.clear()
                    reader.frames.clear()
            self.assertGreater(received, 12 << 20)
            self.assertTrue(await self.eventually(lambda: any(e == 'pane_closed' for e, _ in self.audit()), 20))
            proxy.resume()
            self.assertIn(await slow.wait_closed(20), (4003, 1006))
        finally:
            stop_drain.set()
            await drainer
        self.assertIsNone(reader.close_code)
        self.assertNotIn('reset', event_types(reader, seen))
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['shared_attachments'], snapshot['attached']), (1, True), 'TUI and relay untouched')
        self.assertIsNone(tui.proc.poll(), 'the TUI process is alive')
        tui.send(b'\x1d3')
        self.assertTrue(await tui.until(lambda: 'focus: HOST SHELL' in tui.text(), 20), tui.text())
        tui.send(b'echo TUI-AFTER-$((40+3))\r')
        await reader.wait_output(b'TUI-AFTER-43', 20)


if __name__ == '__main__':
    unittest.main()
