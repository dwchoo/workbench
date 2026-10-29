"""WBX-E2: the Hub relay of WB project panes to the Board workspace (web-board-ux SPEC s5 "Hub 중계: PaneRelay").

Seam T6 (task-inbox): the ``ProjectSandbox`` of ``test_project_control`` (temp HOME/XDG, a short private
``XDG_RUNTIME_DIR``, ``WORKBENCH_HUB_AUTOSTART=0``, fake OMPs, the sandbox removes only its own OMP namespace dir) with a
real Hub (``wb hub start``, ``[web] enabled``, a password) and real backends; the web side is several
:class:`~support.ws_client.Tab` connections on ``/ws/board/<project_id>:<pane>`` and the session API. The TUI stand-in
is a real ``UiClient(name="workbench-ui")`` primary attach opened by the test. Older backends and identity mismatches
use the Hub SPEC S4 contract fixture backend (``fixtures/contract_backend.py``, ``--frames-log``).

Expected values come from the SPEC s5 tables (pane view, messages, close codes, API codes), the ``audit`` rows read with
``sqlite3``, the bytes the host shell echoes, the request frames the contract fixture logged, backend snapshots
(``wb status --json``) and ``/proc``. Run with ``discover -s tests/hub -p 'test_board_pane*.py'``.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import unittest
import uuid

import aiohttp

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(HERE))

from support.ws_client import Tab  # noqa: E402
from test_project_control import CONTRACT_BACKEND, ProjectSandbox, alive, wait_until  # noqa: E402

PASSWORD = 'correct horse battery'
UUID_RE = re.compile(r'^[0-9a-f-]{36}$')


class Browser:
    """A cookie-keeping HTTP client for one loopback Hub (blocking)."""

    def __init__(self, host: str, port: int):
        self.host, self.port, self.cookies = host, port, {}

    def request(self, method: str, path: str, data: dict | None = None, headers: dict | None = None):
        from urllib.parse import urlencode
        sent = dict(headers or {})
        body = None
        if self.cookies:
            sent['Cookie'] = '; '.join(f'{k}={v}' for k, v in self.cookies.items())
        if data is not None:
            body = urlencode(data)
            sent['Content-Type'] = 'application/x-www-form-urlencoded'
            sent['Origin'] = f'http://{self.host}:{self.port}'
        connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            text = response.read().decode('utf-8')
            for header in response.headers.get_all('Set-Cookie') or []:
                name, _, rest = header.partition('=')
                value = rest.split(';')[0]
                if value:
                    self.cookies[name] = value
                else:
                    self.cookies.pop(name, None)
            return response.status, response.headers, text
        finally:
            connection.close()


class PaneHubCase(unittest.IsolatedAsyncioTestCase):
    """A real Hub with web, real backends with fake OMPs, a logged-in session."""

    async def asyncSetUp(self):
        self.box = ProjectSandbox(self)
        config = Path(self.box.env['XDG_CONFIG_HOME']) / 'omp-workbench'
        config.mkdir(parents=True, exist_ok=True, mode=0o700)
        (config / 'hub.toml').write_text('[web]\nenabled = true\nbind = "127.0.0.1"\nport = 0\n')
        done = subprocess.run(
            [sys.executable, '-m', 'workbench', 'hub', 'passwd', '--stdin'], env=self.box.env, cwd=self.box.base,
            input=PASSWORD + '\n', capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.tabs: list[Tab] = []
        self.entries: list[dict] = []
        self.http = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        self.addAsyncCleanup(self.close_all)

    async def close_all(self):
        for tab in self.tabs:
            await tab.close()
        await self.http.close()

    # -- projects --------------------------------------------------------------------------
    def project(self, name: str = 'alpha', *, on: bool = True) -> tuple[Path, str]:
        root, project_id = self.box.project(name)
        self.entries.append(self.box.entry(root, project_id))
        self.box.register(*self.entries)
        if on:
            self.box.power_on(root, project_id)
        return root, project_id

    def fixture(self, name: str, **options) -> tuple[Path, str, Path]:
        """A registered contract fixture backend; its frames log."""
        root, project_id = self.box.project(name)
        self.entries.append(self.box.entry(root, project_id))
        self.box.register(*self.entries)
        log = self.box.base / f'frames-{name}.jsonl'
        argv = [sys.executable, str(CONTRACT_BACKEND), str(root), '--frames-log', str(log)]
        for key, value in options.items():
            argv.append('--' + key.replace('_', '-'))
            if value is not True:
                argv.append(value if isinstance(value, str) else json.dumps(value))
        process = subprocess.Popen(argv, env=self.box.env, cwd=self.box.base, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                   start_new_session=True)
        self.box.processes.append(process)
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        return root, project_id, log

    @staticmethod
    def frames(log: Path) -> list[dict]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    # -- Hub and login ----------------------------------------------------------------------
    def start_hub(self) -> int:
        from fixtures.web_sandbox import tcp_listening
        self.hub_pid, _ticks = self.box.start_hub()
        found: list = []
        wait_until(lambda: bool(found.extend(tcp_listening(self.hub_pid)) or found), 15)
        self.assertEqual(len(found), 1, found)
        self.host, self.port = found[0]
        self.base_url = f'http://{self.host}:{self.port}'
        self.browser = Browser(self.host, self.port)
        _status, _headers, page = self.browser.request('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        status, _headers, _ = self.browser.request('POST', '/login', {'login_token': token, 'password': PASSWORD})
        self.assertEqual(status, 303, 'login')
        self.cookie = self.browser.cookies['wb_hub_session']
        _status, _headers, page = self.browser.request('GET', '/settings')
        self.csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        return self.hub_pid

    def tab(self, pane_id: str, **kwargs) -> Tab:
        tab = Tab(self.base_url, kwargs.pop('cookie', self.cookie), pane_id, **kwargs)
        self.tabs.append(tab)
        return tab

    async def attach(self, pane_id: str, *, size=(30, 100), state: str | None = 'running') -> Tab:
        tab = self.tab(pane_id)
        await tab.attach(self.csrf, size=size)
        if state is not None and tab.events[-1]['terminal']['state'] != state:
            await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                    and item['terminal']['state'] == state, 30)
        return tab

    async def api(self, method: str, path: str, body: dict | None = None, *, csrf: bool = True,
                  cookie: bool = True, headers: dict | None = None) -> tuple[int, dict | None]:
        sent = {'Origin': self.base_url, **(headers or {})}
        if cookie:
            sent['Cookie'] = f'wb_hub_session={self.cookie}'
        if csrf:
            sent['X-CSRF-Token'] = self.csrf
        data = None
        if body is not None:
            data = json.dumps(body)
            sent['Content-Type'] = 'application/json'
        async with self.http.request(method, self.base_url + path, data=data, headers=sent) as response:
            text = await response.text()
            try:
                return response.status, json.loads(text)
            except ValueError:
                return response.status, None

    async def action(self, pane_id: str, name: str, body: dict | None = None) -> tuple[int, dict | None]:
        return await self.api('POST', f'/api/board/panes/{pane_id}/{name}', body if body is not None else {})

    def root_of(self, project_id: str) -> Path:
        return Path(next(entry['root'] for entry in self.entries if entry['project_id'] == project_id))

    def host_owner(self, project_id: str) -> str:
        return self.box.status(self.root_of(project_id))['panes']['host_shell']['input_owner']

    async def take_host(self, project_id: str) -> None:
        """The host shell's input owner is ``user`` (a takeover request + confirm through the API when needed)."""
        if self.host_owner(project_id) == 'user':
            return
        status, reply = await self.action(f'{project_id}:host', 'takeover_request')
        self.assertEqual(status, 200, reply)
        if reply['pane']['input_owner'] != 'user':
            status, reply = await self.action(f'{project_id}:host', 'takeover_confirm')
            self.assertEqual(status, 200, reply)
        self.assertEqual(reply['pane']['input_owner'], 'user', reply)
        self.assertTrue(await self.eventually(lambda: self.host_owner(project_id) == 'user', 15))

    def parent_mode(self, project_id: str) -> str:
        return self.box.status(self.root_of(project_id))['panes']['host_shell']['shell']['parent_mode']

    async def hand_host(self, project_id: str, tab: Tab) -> dict:
        """``wb-handoff`` typed in the host shell, then the handoff button: the input owner is ``manager``."""
        self.assertTrue(await self.eventually(lambda: self.parent_mode(project_id) == 'manual_prompt', 20))
        await tab.send_input(b'wb-handoff\r')
        self.assertTrue(await self.eventually(lambda: self.parent_mode(project_id) == 'control_wait', 20))
        status, reply = await self.action(f'{project_id}:host', 'handoff')
        self.assertEqual(status, 200, reply)
        return reply

    def audit(self, prefix: str = 'pane_') -> list[tuple[str, dict]]:
        db = sqlite3.connect(f'file:{self.box.hub_state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            rows = db.execute('SELECT event, detail_json FROM audit WHERE event LIKE ? ORDER BY id',
                              (prefix + '%',)).fetchall()
        finally:
            db.close()
        return [(event, json.loads(detail)) for event, detail in rows]

    async def eventually(self, predicate, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.1)
        return bool(predicate())


async def refused_upgrade(tab: Tab) -> int:
    try:
        await tab.connect()
    except aiohttp.WSServerHandshakeError as exc:
        return exc.status
    raise AssertionError('the upgrade was accepted')


class PaneIdTests(PaneHubCase):
    """(1) Pane id shape and registration before the upgrade (404), Bearer 401, hello CSRF 4403, API CSRF 403."""

    async def test_unregistered_or_malformed_pane_ids_are_404_before_the_upgrade_and_a_registered_one_upgrades(self):
        _root, project_id = self.project('alpha', on=False)
        self.start_hub()
        other = str(uuid.uuid4())
        for pane_id in (f'{other}:manager', f'{project_id.upper()}:manager', f'{project_id}:shell',
                        f'{project_id}', f'{project_id}:host:x', f'{project_id[:-1]}:host'):
            with self.subTest(pane_id=pane_id):
                self.assertEqual(await refused_upgrade(self.tab(pane_id)), 404)
        tab = self.tab(f'{project_id}:manager')
        attached = await tab.attach(self.csrf)
        view = attached['terminal']
        self.assertEqual((view['id'], view['kind'], view['project_id'], view['project_name'], view['pane'],
                          view['name']),
                         (f'{project_id}:manager', 'pane', project_id, 'alpha', 'manager', 'alpha · manager'))
        if view['state'] != 'off':  # ``connecting`` until the identity check ends
            view = (await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                            and item['terminal']['state'] != 'connecting'))['terminal']
        self.assertEqual(view['state'], 'off', view)  # its backend is not running
        self.assertIn('꺼져', view['message'])
        status, reply = await self.action(f'{other}:host', 'restart')
        self.assertEqual((status, reply['reason']), (404, 'unknown_terminal'))

    async def test_bearer_and_cross_origin_never_reach_a_pane_and_the_csrf_is_checked(self):
        _root, project_id = self.project('alpha', on=False)
        self.start_hub()
        pane_id = f'{project_id}:host'
        bearer = {'Authorization': 'Bearer wbk_' + 'a' * 40}
        self.assertEqual(await refused_upgrade(self.tab(pane_id, cookie=None, headers=bearer)), 401)
        self.assertEqual(await refused_upgrade(self.tab(pane_id, origin='http://evil.example')), 403)
        status, _ = await self.api('POST', f'/api/board/panes/{pane_id}/restart', {}, cookie=False, csrf=False,
                                   headers=bearer)
        self.assertEqual(status, 401)
        status, _ = await self.api('POST', f'/api/board/panes/{pane_id}/restart', {}, csrf=False)
        self.assertEqual(status, 403)
        tab = self.tab(pane_id)
        await tab.connect()
        await tab.hello('x' * len(self.csrf))
        self.assertEqual(await tab.wait_closed(), 4403)
        await self.eventually(lambda: any(event == 'pane_denied' for event, _ in self.audit()))
        denied = [detail for event, detail in self.audit() if event == 'pane_denied']
        self.assertEqual([(d['pane_id'], d['reason']) for d in denied], [(pane_id, 'csrf')])



class RelayOpenTests(PaneHubCase):
    """(2) A hello opens the project's relay: identity check, hello ``workbench-hub``, welcome ``backend_pid``,
    ``share_attach``; a failed check is ``unreachable`` and nothing is attached."""

    async def test_a_hello_opens_one_shared_attach_and_every_pane_runs(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        tabs = [await self.attach(f'{project_id}:{pane}') for pane in ('manager', 'worker', 'host')]
        for tab in tabs:
            view = [item for item in tab.events if item.get('terminal')][-1]['terminal']
            self.assertEqual((view['state'], view['ui_attached']), ('running', False), view)
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['shared_attachments'], snapshot['attached']), (1, False), 'one relay per project')
        host = [item for item in tabs[2].events if item.get('terminal')][-1]['terminal']
        self.assertEqual(host['size'], [snapshot['panes']['host_shell']['rows'],
                                        snapshot['panes']['host_shell']['cols']])
        self.assertIn(host['input_owner'], ('manager', 'user'))

    async def test_a_welcome_naming_another_pid_is_unreachable_and_nothing_is_attached(self):
        _root, project_id, log = self.fixture('alpha', welcome_json={'backend_pid': 1})
        self.start_hub()
        tab = await self.attach(f'{project_id}:manager', state='unreachable')
        view = tab.events[-1]['terminal']
        self.assertIn('연결하지 못했습니다', view['message'])
        kinds = [line['header']['type'] for line in self.frames(log)]
        self.assertIn('hello', kinds)
        self.assertNotIn('share_attach', kinds)
        hello = next(line['header'] for line in self.frames(log) if line['header']['type'] == 'hello')
        self.assertEqual(hello['client'], 'workbench-hub')

    async def test_the_fixture_with_its_own_pid_is_attached_once(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json={'panes': {
            'manager_omp': {'pane': 'manager_omp', 'alive': True, 'rows': 24, 'cols': 80}}})
        self.start_hub()
        await self.attach(f'{project_id}:manager')
        await self.attach(f'{project_id}:manager')
        kinds = [line['header']['type'] for line in self.frames(log)]
        self.assertEqual(kinds.count('share_attach'), 1, kinds)



async def until_reset(tab: Tab, timeout: float = 30.0) -> int:
    """Read until a ``reset``; the output length at that moment (what came before it)."""
    await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'reset', timeout)
    return len(tab.output)


class ScreenRelayTests(PaneHubCase):
    """(3) Two web connections get the same replay and live bytes; nothing falls between replay and live or comes
    twice. (4) A restarted pane and a new backend (a new ``share_attach``) are a ``reset`` then the new screen."""

    async def test_a_connection_attaching_during_output_gets_the_same_bytes_without_gap_or_repeat(self):
        _root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        pane_id = f'{project_id}:host'
        first = await self.attach(pane_id)
        await first.send_input(b'for i in $(seq 1 400); do echo N-$i; sleep 0.004; done; echo DONE-$((6*7))\r')
        await first.wait_output(b'N-100\r\n', 30)
        second = await self.attach(pane_id)  # in the middle of the loop
        for tab in (first, second):
            await tab.wait_output(b'DONE-42', 30)
        await asyncio.sleep(0.3)
        await first.drain(0.3)
        await second.drain(0.3)
        self.assertEqual(bytes(second.output), bytes(first.output), 'the same ring replay, then the same live bytes')
        for number in range(1, 401):
            self.assertEqual(second.output.count(b'N-%d\r\n' % number), 1, number)

    async def test_a_restarted_pane_is_a_reset_and_then_only_its_new_session(self):
        _root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        pane_id = f'{project_id}:host'
        tab = await self.attach(pane_id)
        await tab.send_input(b'echo OLD-$((40+2))\r')
        await tab.wait_output(b'OLD-42')
        status, reply = await self.action(pane_id, 'kill', {'confirm': True})
        self.assertEqual(status, 200, reply)
        await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                and item['terminal']['state'] == 'exited', 30)
        status, reply = await self.action(pane_id, 'restart')
        self.assertEqual(status, 200, reply)
        mark = await until_reset(tab)
        await self.take_host(project_id)
        await tab.send_input(b'echo NEW-$((40+3))\r')
        await tab.wait_output(b'NEW-43')
        self.assertNotIn(b'OLD-', bytes(tab.output[mark:]), 'nothing of the old session after the reset')
        late = await self.attach(pane_id)
        await late.drain(0.3)
        self.assertNotIn(b'OLD-', bytes(late.output), 'the Hub ring holds the new session only')
        self.assertIn(b'NEW-43', bytes(late.output))

    async def test_a_new_backend_is_a_reset_and_its_replay(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        pane_id = f'{project_id}:host'
        tab = await self.attach(pane_id)
        await tab.send_input(b'echo GEN1-$((40+2))\r')
        await tab.wait_output(b'GEN1-42')
        done = self.box.wb('shutdown', '--yes', cwd=root, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                and item['terminal']['state'] in ('unreachable', 'off'), 30)
        self.box.power_on(root, project_id)
        mark = await until_reset(tab, 40)  # reconnect with backoff (<= 15 s)
        await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                and item['terminal']['state'] == 'running', 30)
        await self.take_host(project_id)
        await tab.send_input(b'echo GEN2-$((40+3))\r')
        await tab.wait_output(b'GEN2-43')
        self.assertNotIn(b'GEN1-', bytes(tab.output[mark:]))
        snapshot = self.box.status(root)
        self.assertEqual(snapshot['shared_attachments'], 1)



RUNNING_PANES = {'panes': {
    'manager_omp': {'pane': 'manager_omp', 'alive': True, 'rows': 30, 'cols': 100, 'input_owner': 'user'},
    'worker_omp': {'pane': 'worker_omp', 'alive': True, 'rows': 30, 'cols': 100, 'input_owner': 'user'},
    'host_shell': {'pane': 'host_shell', 'alive': True, 'rows': 30, 'cols': 100, 'input_owner': 'user',
                   'automation_hold': None, 'command': None, 'shell': {'takeover_requested': False}}}}


class InputTests(PaneHubCase):
    """(5) One binary frame is one ``input`` request (never merged or split), in arrival order; a lone Ctrl+C stays
    lone. (6) A refusal reaches the sending connection only as ``input_refused``; off / not running is not sent."""

    def inputs(self, log: Path) -> list[tuple[str, bytes]]:
        return [(line['header']['pane'], bytes.fromhex(line.get('payload', '')))
                for line in self.frames(log) if line['header']['type'] == 'input']

    async def test_each_frame_is_one_request_in_arrival_order_and_a_lone_ctrl_c_stays_lone(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES, echo_input=True)
        self.start_hub()
        pane_id = f'{project_id}:manager'
        first, second = await self.attach(pane_id), await self.attach(pane_id)
        sent = [b'a', b'\x03', b'bc', b'\x03', b'\x03', 'ㅎ한'.encode(), b'x' * 60000, b'z']
        for chunk in sent:  # one connection, no wait: its frames keep their order
            await first.send_input(chunk)
        self.assertTrue(await self.eventually(lambda: len(self.inputs(log)) == len(sent)), self.inputs(log))
        await second.send_input(b'\x03')
        self.assertTrue(await self.eventually(lambda: len(self.inputs(log)) == len(sent) + 1))
        await first.send_input(b'after')
        self.assertTrue(await self.eventually(lambda: len(self.inputs(log)) == len(sent) + 2))
        self.assertEqual(self.inputs(log), [('manager_omp', chunk) for chunk in sent + [b'\x03', b'after']])
        await second.wait_output(b'after')
        self.assertEqual(bytes(second.output), b''.join(sent + [b'\x03', b'after']), 'echoed to both, in order')

    async def test_a_refusal_goes_back_to_its_connection_only_and_nothing_is_sent_when_off_or_exited(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        pane_id = f'{project_id}:host'
        typing, watching = await self.attach(pane_id), await self.attach(pane_id)
        reply = await self.hand_host(project_id, typing)
        self.assertEqual(reply['pane']['input_owner'], 'manager')
        await typing.send_input(b'echo nope\r')
        refused = await typing.event('input_refused')
        self.assertEqual(refused, {'t': 'input_refused', 'reason': 'input_owner_manager',
                                   'detail': 'host terminal의 입력 주인이 manager입니다. 인수 요청·확인 뒤에 입력할 수 '
                                             '있습니다.'})
        await watching.drain(0.5)
        self.assertNotIn('input_refused', [event.get('t') for event in watching.events])
        await self.take_host(project_id)
        status, reply = await self.action(pane_id, 'kill', {'confirm': True})
        self.assertEqual(status, 200, reply)
        await typing.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                   and item['terminal']['state'] == 'exited', 30)
        await typing.send_input(b'x')
        self.assertEqual((await typing.event('input_refused'))['reason'], 'pane_unavailable')
        done = self.box.wb('shutdown', '--yes', cwd=root, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        await typing.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                   and item['terminal']['state'] == 'off', 40)
        await typing.send_input(b'y')
        self.assertEqual((await typing.event('input_refused'))['reason'], 'backend_off')



class UnansweredCapTests(PaneHubCase):
    """(7) At most 64 input requests or 1 MiB of input wait for answers per relay; past that the sending socket is
    no longer read (its later frames wait in TCP), while other connections go on."""

    async def test_sixty_four_unanswered_requests_stop_the_sender_and_others_still_get_answers(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES, no_reply='input')
        self.start_hub()
        pane_id = f'{project_id}:worker'
        sender, other = await self.attach(pane_id), await self.attach(pane_id)
        for number in range(80):
            await sender.send_input(b'%d;' % number)
        count = lambda: sum(1 for line in self.frames(log) if line['header']['type'] == 'input')  # noqa: E731
        self.assertTrue(await self.eventually(lambda: count() >= 64))
        await asyncio.sleep(1.0)
        self.assertEqual(count(), 64)
        await other.send({'t': 'ping', 'n': 7})
        self.assertEqual((await other.event('pong'))['n'], 7, 'another connection is still served')
        await sender.send({'t': 'ping', 'n': 8})
        await sender.drain(1.0)
        self.assertNotIn('pong', [event.get('t') for event in sender.events], 'the sender is not read')

    async def test_one_mebibyte_of_unanswered_input_stops_the_sender(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES, no_reply='input')
        self.start_hub()
        sender = await self.attach(f'{project_id}:manager')
        for _ in range(30):
            await sender.send_input(b'y' * 60000)
        count = lambda: sum(1 for line in self.frames(log) if line['header']['type'] == 'input')  # noqa: E731
        self.assertTrue(await self.eventually(lambda: count() >= 18))
        await asyncio.sleep(1.0)
        self.assertEqual(count(), 18, '18 x 60000 bytes reach 1 MiB')


class SizeOwnerTests(PaneHubCase):
    """(8) With a TUI attached the Hub never resizes and tells ``size``; without one the Board's size owner rule
    applies per pane; ``resize_owned_by_ui`` answers are dropped."""

    def resizes(self, log: Path) -> list[tuple]:
        return [(line['header']['pane'], line['header']['rows'], line['header']['cols'])
                for line in self.frames(log) if line['header']['type'] == 'resize']

    def tui(self, root: Path, size=(30, 120)):
        from workbench.backend.client import UiClient
        pid, ticks, record = self.box.backend_ref(root)
        client = UiClient(Path(record['ui_socket']), name='workbench-ui', timeout=10,
                          expected_process={'pid': pid, 'start_ticks': ticks})
        self.addCleanup(client.close)
        answer = client.attach(size)
        self.assertTrue(answer['ok'], answer)
        return client

    def pane_size(self, root: Path, pane: str = 'host_shell') -> tuple[int, int]:
        info = self.box.status(root)['panes'][pane]
        return info['rows'], info['cols']

    async def test_without_a_tui_the_lone_client_sets_the_size_and_the_last_typing_client_owns_it(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        pane_id = f'{project_id}:host'
        first = await self.attach(pane_id, size=(25, 90))
        self.assertTrue(await self.eventually(lambda: self.pane_size(root) == (25, 90)))
        second = await self.attach(pane_id, size=(40, 120))
        await asyncio.sleep(1.0)
        self.assertEqual(self.pane_size(root), (25, 90), 'a second client never resizes by attaching')
        await second.send_input(b'true\r')
        self.assertTrue(await self.eventually(lambda: self.pane_size(root) == (40, 120)))
        message = await first.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'size'
                                            and (item['rows'], item['cols']) == (40, 120), 15)
        self.assertEqual(message, {'t': 'size', 'rows': 40, 'cols': 120})
        await second.close()
        await asyncio.sleep(1.0)
        self.assertEqual(self.pane_size(root), (40, 120), 'an owner that leaves keeps the size')

    async def test_with_a_tui_attached_web_resizes_and_input_never_resize_and_the_web_follows(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        tui = await asyncio.get_running_loop().run_in_executor(None, self.tui, root, (30, 120))
        tui_size = self.pane_size(root)
        pane_id = f'{project_id}:host'
        tab = await self.attach(pane_id, size=(20, 70))
        view = [item for item in tab.events if item.get('terminal')][-1]['terminal']
        self.assertTrue(view['ui_attached'], view)
        self.assertEqual(tuple(view['size']), tui_size)
        await tab.send({'t': 'resize', 'rows': 22, 'cols': 75})
        await tab.send_input(b'echo WEB-$((40+2))\r')
        await tab.wait_output(b'WEB-42')
        await asyncio.sleep(1.0)
        self.assertEqual(self.pane_size(root), tui_size, 'the TUI owns the size')
        answer = await asyncio.get_running_loop().run_in_executor(None, tui.detach)
        self.assertTrue(answer['ok'], answer)
        await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                and item['terminal']['ui_attached'] is False, 15)
        await asyncio.sleep(0.5)
        self.assertEqual(self.pane_size(root), tui_size, 'the TUI left: its size stays until the web types')
        await tab.send_input(b'true\r')
        self.assertTrue(await self.eventually(lambda: self.pane_size(root) == (22, 75)))
        tui.close()

    async def test_the_hub_sends_no_resize_while_the_snapshot_says_a_tui_is_attached(self):
        attached = {**RUNNING_PANES, 'attached': True}
        _root, project_id, log = self.fixture('alpha', snapshot_json=attached)
        self.start_hub()
        tab = await self.attach(f'{project_id}:host', size=(20, 70))
        self.assertTrue(tab.events[-1]['terminal']['ui_attached'])
        await tab.send({'t': 'resize', 'rows': 22, 'cols': 75})
        await tab.send_input(b'x')
        self.assertTrue(await self.eventually(lambda: any(line['header']['type'] == 'input'
                                                          for line in self.frames(log))))
        self.assertEqual(self.resizes(log), [])

    async def test_a_resize_owned_by_ui_answer_is_dropped(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES,
                                              replies_json={'resize': {'ok': False, 'reason': 'resize_owned_by_ui'}})
        self.start_hub()
        tab = await self.attach(f'{project_id}:host', size=(20, 70))
        self.assertTrue(await self.eventually(lambda: self.resizes(log) == [('host_shell', 20, 70)]))
        await tab.send({'t': 'resize', 'rows': 22, 'cols': 75})
        self.assertTrue(await self.eventually(lambda: len(self.resizes(log)) == 2))
        await tab.drain(0.5)
        self.assertEqual([event['t'] for event in tab.events if event['t'] not in ('attached', 'state', 'clients', 'reset')],
                         [], 'nothing reaches the web for a refused resize')



COMMAND_ID = '5f0c5c1e-8a3d-4b55-9b8f-3c2f1d0e9a77'
WITH_COMMAND = {'panes': {**RUNNING_PANES['panes'], 'host_shell': {
    **RUNNING_PANES['panes']['host_shell'],
    'command': {'command_id': COMMAND_ID, 'label': 'sleep 300', 'owner': 'worker', 'state': 'running'}}}}


class ActionApiTests(PaneHubCase):
    """(9) ``POST /api/board/panes/{id}/{action}``: host-only actions, ``confirm``, restart/kill on a real backend,
    stop (202 + the final ``notice``), ``backend_refused`` + reason, ``terminal_busy``, 504, ``backend_off``."""

    async def test_host_buttons_on_a_real_backend_and_the_refusals_before_the_backend(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        host, manager = f'{project_id}:host', f'{project_id}:manager'
        for name in ('takeover_request', 'takeover_confirm', 'handoff'):
            status, reply = await self.action(manager, name)
            self.assertEqual((status, reply['reason']), (400, 'wrong_pane'), name)
        status, reply = await self.action(manager, 'kill', {'confirm': True})
        self.assertEqual((status, reply['reason']), (400, 'wrong_pane'))
        status, reply = await self.action(manager, 'stop', {'confirm': True, 'command_id': COMMAND_ID})
        self.assertEqual((status, reply['reason']), (400, 'wrong_pane'))
        status, reply = await self.action(host, 'kill', {})
        self.assertEqual((status, reply['reason']), (400, 'confirm_required'))
        status, reply = await self.action(host, 'kill', {'confirm': True, 'extra': 1})
        self.assertEqual((status, reply['reason']), (400, 'bad_input'))
        status, reply = await self.action(manager, 'restart')
        self.assertEqual((status, reply['reason'], reply['backend_reason']), (409, 'backend_refused', 'pane_alive'))
        status, reply = await self.action(host, 'kill', {'confirm': True})
        self.assertEqual(status, 200, reply)
        self.assertEqual((reply['pane']['id'], reply['pane']['kind']), (host, 'pane'))
        self.assertTrue(await self.eventually(lambda: self.box.status(root)['panes']['host_shell']['alive'] is False))
        status, reply = await self.action(host, 'kill', {'confirm': True})
        self.assertEqual((status, reply['reason']), (409, 'backend_refused'))
        self.assertIn(reply['backend_reason'], ('pane_exited', 'pane_not_killable'))
        status, reply = await self.action(host, 'restart')
        self.assertEqual(status, 200, reply)
        self.assertTrue(await self.eventually(lambda: self.box.status(root)['panes']['host_shell']['alive'] is True))
        done = self.box.wb('shutdown', '--yes', cwd=root, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(await self.eventually(lambda: self.box.status(root) is None, 20))
        status, reply = await self.action(host, 'restart')
        self.assertEqual((status, reply['reason']), (503, 'backend_off'), reply)

    async def test_stop_answers_202_and_the_final_outcome_reaches_the_pane_connections(self):
        _root, project_id, log = self.fixture(
            'alpha', snapshot_json=WITH_COMMAND,
            replies_json={'stop_command': {'command_id': COMMAND_ID, 'stop': {'status': 'stopping'}}},
            stop_final_json={'status': 'stopped', 'command_id': COMMAND_ID, 'signal': 'SIGINT'})
        self.start_hub()
        host = f'{project_id}:host'
        tab = await self.attach(host)
        view = tab.events[-1]['terminal']
        self.assertEqual(view['command'], {'command_id': COMMAND_ID, 'label': 'sleep 300', 'owner': 'worker',
                                           'state': 'running'})
        self.assertIn('stop', view['actions'])
        status, reply = await self.action(host, 'stop', {'confirm': True})
        self.assertEqual((status, reply['reason']), (400, 'bad_input'), 'the command shown is required')
        status, reply = await self.action(host, 'stop', {'confirm': True, 'command_id': COMMAND_ID})
        self.assertEqual((status, reply), (202, {'ok': True, 'command_id': COMMAND_ID,
                                                 'stop': {'status': 'stopping'}}))
        notice = await tab.event('notice')
        self.assertEqual(notice, {'t': 'notice', 'action': 'stop', 'command_id': COMMAND_ID,
                                  'stop': {'status': 'stopped', 'command_id': COMMAND_ID, 'signal': 'SIGINT'}})
        sent = [line['header'] for line in self.frames(log) if line['header']['type'] == 'stop_command']
        self.assertEqual([header['command_id'] for header in sent], [COMMAND_ID])
        self.assertTrue(await self.eventually(lambda: any(event == 'pane_stop' for event, _ in self.audit())))
        self.assertEqual([detail for event, detail in self.audit() if event == 'pane_stop'],
                         [{'pane_id': host, 'project_id': project_id, 'project_name': 'alpha', 'pane': 'host',
                           'command_id': COMMAND_ID, 'outcome': 'stopped'}])

    async def test_a_backend_refusal_a_busy_pane_and_a_silent_backend(self):
        _root, project_id, log = self.fixture(
            'alpha', snapshot_json=RUNNING_PANES, no_reply='restart_pane',
            replies_json={'kill_pane': {'ok': False, 'reason': 'kill_failed'}})
        self.start_hub()
        host = f'{project_id}:host'
        status, reply = await self.action(host, 'kill', {'confirm': True})
        self.assertEqual((status, reply['reason'], reply['backend_reason']), (409, 'backend_refused', 'kill_failed'))
        started = time.monotonic()
        first = asyncio.ensure_future(self.action(host, 'restart'))
        self.assertTrue(await self.eventually(lambda: any(line['header']['type'] == 'restart_pane'
                                                          for line in self.frames(log))))
        status, reply = await self.action(host, 'restart')
        self.assertEqual((status, reply['reason']), (409, 'terminal_busy'))
        status, reply = await first
        self.assertEqual((status, reply['reason']), (504, 'backend_timeout'))
        self.assertIn('결과를 알 수 없음', reply['detail'])
        self.assertGreaterEqual(time.monotonic() - started, 9.5)
        restarts = [line for line in self.frames(log) if line['header']['type'] == 'restart_pane']
        self.assertEqual(len(restarts), 1, 'a timed-out action is not sent again')


class AuditTests(PaneHubCase):
    """(10) ``pane_*`` audit rows (attach, release, closed, denied, takeover, handoff, restart, kill, stop) with the
    SPEC details, and never input bytes."""

    async def test_rows_name_the_pane_and_never_the_input(self):
        _root, project_id = self.project('alpha')
        self.start_hub()
        host = f'{project_id}:host'
        await self.take_host(project_id)
        tab = await self.attach(host)
        await tab.send_input(b'echo SECRET-INPUT-$((40+2))\r')
        await tab.wait_output(b'SECRET-INPUT-42')
        await self.hand_host(project_id, tab)
        status, reply = await self.action(host, 'takeover_request')
        self.assertEqual(status, 200, reply)
        status, reply = await self.action(host, 'kill', {'confirm': True})
        self.assertEqual(status, 200, reply)
        status, reply = await self.action(host, 'restart')
        self.assertEqual(status, 200, reply)
        await tab.send({'t': 'release'})
        self.assertEqual(await tab.wait_closed(), 1000)
        other = await self.attach(host)
        other.kill()
        self.assertTrue(await self.eventually(lambda: [e for e, _ in self.audit()].count('pane_closed') == 1))
        rows = self.audit()
        who = {'pane_id': host, 'project_id': project_id, 'project_name': 'alpha', 'pane': 'host'}
        events = [event for event, _ in rows]
        self.assertEqual(events, ['pane_attach', 'pane_handoff', 'pane_takeover_request', 'pane_kill', 'pane_restart',
                                  'pane_release', 'pane_attach', 'pane_closed'], rows)
        details = dict(rows[1:5][i] for i in range(4))
        self.assertEqual(details['pane_handoff'], {**who, 'ok': True, 'input_owner': 'manager'})
        self.assertEqual(details['pane_takeover_request']['input_owner'], 'user')
        self.assertEqual(details['pane_kill'], {**who, 'ok': True})
        self.assertEqual(details['pane_restart'], {**who, 'ok': True})
        self.assertEqual(rows[0][1], {**who, 'client': rows[0][1]['client'], 'clients': 1})
        self.assertEqual(rows[5][1]['clients'], 0)
        db = sqlite3.connect(f'file:{self.box.hub_state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            dump = '\n'.join(str(row) for row in db.execute('SELECT * FROM audit'))
        finally:
            db.close()
        self.assertNotIn('SECRET', dump)
        self.assertNotIn('echo', dump)
        self.assertNotIn('wb-handoff', dump)



class ClientLimitTests(PaneHubCase):
    """(11) Eight connections per pane (the 9th: ``refused`` + 4429); a connection that stops reading is closed
    4003 alone: the other connections and the relay go on."""

    async def test_the_ninth_connection_of_a_pane_is_refused_4429(self):
        _root, project_id, _log = self.fixture('alpha', snapshot_json=RUNNING_PANES)
        self.start_hub()
        pane_id = f'{project_id}:worker'
        for _ in range(8):
            await self.attach(pane_id)
        ninth = self.tab(pane_id)
        await ninth.connect()
        await ninth.hello(self.csrf)
        self.assertEqual(await ninth.wait_closed(), 4429)
        self.assertIn({'t': 'refused', 'reason': 'too_many_clients', 'limit': 8}, ninth.events)
        other = await self.attach(f'{project_id}:manager')  # the limit is per pane
        self.assertEqual(other.events[-1]['terminal']['pane'], 'manager')
        self.assertTrue(await self.eventually(lambda: any(
            event == 'pane_denied' and detail['reason'] == 'too_many_clients' for event, detail in self.audit())))

    async def test_a_connection_that_stops_reading_is_closed_4003_and_the_others_and_the_relay_go_on(self):
        from support.tcp_proxy import TcpProxy
        root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        pane_id = f'{project_id}:host'
        proxy = await TcpProxy(self.host, self.port).start()
        self.addAsyncCleanup(proxy.close)
        slow = Tab(proxy.url, self.cookie, pane_id, origin=self.base_url,
                   headers={'Host': f'{self.host}:{self.port}'})
        self.tabs.append(slow)
        await slow.attach(self.csrf)
        reader = await self.attach(pane_id)
        seen = len(reader.events)  # the first share_attach's reset came before
        proxy.pause()
        await reader.send_input(b"head -c 33554432 /dev/zero | tr '\\0' x; echo FLOOD-$((1+1))DONE\r")
        tail = b''
        received = 0
        while b'FLOOD-2DONE' not in tail:
            item = await reader._next(30)
            self.assertIsNotNone(item, f'the reader closed ({reader.close_code}) after {received} bytes')
            if isinstance(item, bytes):
                received += len(item)
                tail = (tail + item)[-64:]
                reader.output.clear()
                reader.frames.clear()
        self.assertGreater(received, 32 << 20)
        self.assertTrue(await self.eventually(lambda: any(event == 'pane_closed' for event, _ in self.audit()), 20))
        proxy.resume()
        self.assertIn(await slow.wait_closed(20), (4003, 1006))
        self.assertIsNone(reader.close_code)
        self.assertNotIn('reset', [event.get('t') for event in reader.events[seen:]], 'the relay was not re-attached')
        self.assertEqual(self.box.status(root)['shared_attachments'], 1)
        await reader.send_input(b'echo STILL-$((40+2))\r')
        await reader.wait_output(b'STILL-42')


class RelayLifetimeTests(PaneHubCase):
    """(12) The relay stays 30 s after its last connection (a reconnect within it does not attach again), then
    ``share_detach``. (13) A backend that ends and starts again is attached again with backoff (<= 15 s)."""

    def kinds(self, log: Path) -> list[str]:
        return [line['header']['type'] for line in self.frames(log)]

    async def test_the_relay_closes_thirty_seconds_after_the_last_connection_and_not_before(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES)
        self.start_hub()
        pane_id = f'{project_id}:manager'
        tab = await self.attach(pane_id)
        await tab.send({'t': 'release'})
        await tab.wait_closed()
        await asyncio.sleep(3)
        tab = await self.attach(pane_id)
        self.assertEqual(self.kinds(log).count('share_attach'), 1, 'a reconnect within the grace reuses the relay')
        left = time.monotonic()
        await tab.send({'t': 'release'})
        await tab.wait_closed()
        self.assertTrue(await self.eventually(lambda: 'share_detach' in self.kinds(log), 40))
        self.assertGreaterEqual(time.monotonic() - left, 29.0)
        await self.attach(pane_id)
        self.assertEqual(self.kinds(log).count('share_attach'), 2)

    async def test_a_restarted_backend_is_attached_again_within_the_backoff(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        tab = await self.attach(f'{project_id}:worker')
        done = self.box.wb('shutdown', '--yes', cwd=root, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                and item['terminal']['state'] == 'off', 40)
        await asyncio.sleep(5)  # the backoff has grown meanwhile
        self.box.power_on(root, project_id)
        on = time.monotonic()
        await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'state'
                                and item['terminal']['state'] == 'running', 30)
        self.assertLessEqual(time.monotonic() - on, 16.0)
        self.assertEqual(self.box.status(root)['shared_attachments'], 1)


    async def test_a_hub_restart_closes_the_web_1001_detaches_and_the_new_hub_attaches_again(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        tab = await self.attach(f'{project_id}:manager')
        self.assertEqual(self.box.status(root)['shared_attachments'], 1)
        done = self.box.wb('hub', 'stop', timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(await tab.wait_closed(15), 1001)
        self.assertFalse(alive(self.hub_pid))
        self.assertTrue(await self.eventually(lambda: self.box.status(root)['shared_attachments'] == 0, 10),
                        'no relay outlives its Hub')
        self.start_hub()
        await self.attach(f'{project_id}:manager')
        self.assertEqual(self.box.status(root)['shared_attachments'], 1)


class OlderBackendTests(PaneHubCase):
    """(14) ``share_attach`` -> ``unsupported_type``: ``too_old`` with its sentence, no automatic retry, one check
    again on a new web hello; a refused shared attach (``not_attached``) is ``unreachable``."""

    def attaches(self, log: Path) -> int:
        return sum(1 for line in self.frames(log) if line['header']['type'] == 'share_attach')

    async def test_an_older_backend_is_too_old_and_is_asked_again_only_on_a_new_hello(self):
        _root, project_id, log = self.fixture('alpha', replies_json={'share_attach': {
            'ok': False, 'reason': 'unsupported_type'}})
        self.start_hub()
        tab = await self.attach(f'{project_id}:manager', state='too_old')
        view = tab.events[-1]['terminal']
        self.assertEqual(view['message'], '이 프로젝트의 backend는 이전 판입니다. 프로젝트를 끄고 다시 켜면 웹에서 '
                                          'pane을 쓸 수 있습니다')
        await asyncio.sleep(5)
        self.assertEqual(self.attaches(log), 1, 'no automatic retry')
        await tab.send_input(b'x')
        self.assertEqual((await tab.event('input_refused'))['reason'], 'pane_unavailable')
        await self.attach(f'{project_id}:host', state='too_old')
        self.assertTrue(await self.eventually(lambda: self.attaches(log) == 2))
        status, reply = await self.action(f'{project_id}:host', 'restart')
        self.assertEqual((status, reply['reason']), (503, 'backend_too_old'))
        self.assertEqual(self.frames(log)[-1]['header']['type'], 'share_attach', 'nothing else was sent')

    async def test_a_refused_shared_attach_is_unreachable(self):
        _root, project_id, log = self.fixture('alpha', replies_json={'share_attach': {
            'ok': False, 'reason': 'not_attached'}})
        self.start_hub()
        tab = await self.attach(f'{project_id}:manager', state='unreachable')
        self.assertIn('연결하지 못했습니다', tab.events[-1]['terminal']['message'])


class RegistrationTests(PaneHubCase):
    """(15) A project whose registration is removed: its pane connections get ``gone`` and 4404, the relay ends."""

    async def test_removing_the_registration_ends_its_pane_connections_with_4404(self):
        _root, project_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES)
        other_root, other_id, _other_log = self.fixture('beta', snapshot_json=RUNNING_PANES)
        self.start_hub()
        gone, kept = await self.attach(f'{project_id}:host'), await self.attach(f'{other_id}:host')
        self.entries = [entry for entry in self.entries if entry['project_id'] != project_id]
        self.box.register(*self.entries)
        self.assertEqual(await gone.wait_closed(10), 4404)
        self.assertIn({'t': 'gone'}, gone.events)
        self.assertTrue(await self.eventually(lambda: any(line['header']['type'] == 'share_detach'
                                                          for line in self.frames(log))))
        self.assertIsNone(kept.close_code)
        self.assertEqual(await refused_upgrade(self.tab(f'{project_id}:host')), 404)


class ProjectsListTests(PaneHubCase):
    """(16) ``GET /api/board`` ``projects``: registry order, ``power`` from the Hub list, pane views without a relay
    from it (``on`` -> ``idle``, ``off`` -> ``off``), never a shared attach."""

    async def test_projects_list_every_registered_project_with_three_pane_views(self):
        _root, on_id, log = self.fixture('alpha', snapshot_json=RUNNING_PANES)
        _off_root, off_id = self.project('beta', on=False)
        self.start_hub()
        status, reply = await self.api('GET', '/api/board', csrf=False)
        self.assertEqual(status, 200, reply)
        projects = reply['projects']
        self.assertEqual([(item['project_id'], item['name'], item['power']) for item in projects],
                         [(on_id, 'alpha', 'on'), (off_id, 'beta', 'off')])
        self.assertEqual([[(view['id'], view['state']) for view in item['panes']] for item in projects],
                         [[(f'{on_id}:{pane}', 'idle') for pane in ('manager', 'worker', 'host')],
                          [(f'{off_id}:{pane}', 'off') for pane in ('manager', 'worker', 'host')]])
        self.assertEqual(projects[1]['panes'][0]['name'], 'beta · manager')
        self.assertNotIn('share_attach', [line['header']['type'] for line in self.frames(log)])
        await self.attach(f'{on_id}:host')
        status, reply = await self.api('GET', '/api/board', csrf=False)
        views = reply['projects'][0]['panes']
        self.assertEqual([(view['state'], view['clients']) for view in views],
                         [('running', 0), ('running', 0), ('running', 1)])


if __name__ == '__main__':
    unittest.main()
