"""TI-19 / Phase 2 final integration (part B): the Task Inbox end to end on a real Hub, a real backend and fake OMPs.

Everything is a real process on loopback: ``wb hub start`` (web listener on ``127.0.0.1:0``, ``/mcp``), real
backends whose manager/worker are the fake OMP of ``tests/recovery_boot/fake_omp.py`` (no model, no provider, no
network), the web through a cookie-keeping ``http.client`` browser stand-in and ``/mcp`` through the stdlib
``McpClient``. The fake manager's ``workbench_inbox`` / ``to_worker`` calls are bridge ``tool_request`` frames sent
on the real backend UI socket (the way a model's tool call arrives). Expected values come from the SPEC / USAGE
texts, ``/proc`` (fd tables, ``/proc/locks``, pids) and the inbox / task databases read with stdlib ``sqlite3``.

Not covered here (``pending`` on the target host): real browser rendering, LAN/VPN reachability, GPU, Codex CLI MCP
compatibility, real systemd/linger, the online advisory check.

Run: ``PYTHONPATH=src:. python -m unittest discover -s tests/integration -p 'live_inbox*.py'``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
HUB_TESTS = REPO / 'tests' / 'hub'
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(HUB_TESTS))

import test_inbox_flow_link as link  # noqa: E402
import test_web_requests_hub as web_hub  # noqa: E402
from fixtures.web_sandbox import tcp_listening, wait_until as web_wait  # noqa: E402
from support.mcp_client import McpClient  # noqa: E402
from test_project_control import alive, wait_until  # noqa: E402

PASSWORD = 'correct horse battery'
RAW_KEY = re.compile(r'wbk_[a-z2-7]{10}_[A-Za-z0-9_-]{43}')


def sleeps(seconds: str) -> list[int]:
    found = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            argv = Path(f'/proc/{name}/cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if argv[:2] == [b'sleep', seconds.encode()]:
            found.append(int(name))
    return found


def fd_holders(prefix: str, pids) -> list[int]:
    """The ``pids`` that hold an open fd whose target starts with ``prefix`` (``readlink`` only, never opens)."""
    found = []
    for pid in pids:
        try:
            names = os.listdir(f'/proc/{pid}/fd')
        except OSError:
            continue
        for name in names:
            try:
                if os.readlink(f'/proc/{pid}/fd/{name}').startswith(prefix):
                    found.append(pid)
                    break
            except OSError:
                continue
    return found


def lock_holders(path: Path) -> set[int]:
    inode = os.stat(path).st_ino
    holders = set()
    for line in Path('/proc/locks').read_text().splitlines():
        parts = line.split()
        fields = [part for part in parts if part.count(':') == 2]
        if fields and fields[0].rsplit(':', 1)[1] == str(inode):
            holders.add(int(parts[parts.index(fields[0]) - 1]))
    return holders


class FinalCase(link.LinkCase):
    """A real Hub with the web listener on, a logged-in owner browser and helpers for keys and the fake manager."""

    def start_web_hub(self, *titles: str):
        config = Path(self.box.env['XDG_CONFIG_HOME']) / 'omp-workbench'
        config.mkdir(parents=True, exist_ok=True, mode=0o700)
        (config / 'hub.toml').write_text('[web]\nbind = "127.0.0.1"\nport = 0\n')
        done = subprocess.run([sys.executable, '-m', 'workbench', 'hub', 'passwd', '--stdin'], env=self.box.env,
                              cwd=self.box.base, input=PASSWORD + '\n', capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        root, project_id, ids, ui = self.linked_hub('alpha', *titles)
        self.locate_web()
        return root, project_id, ids, ui

    def locate_web(self) -> None:
        pid = json.loads((self.box.hub_state / 'hub.json').read_text())['pid']
        found: list = []
        web_wait(lambda: bool(found.extend(tcp_listening(pid)) or found), 15)
        self.assertEqual(len(found), 1, found)
        self.host, self.port = found[0]
        self.hub_pid = pid

    def login(self) -> web_hub.Browser:
        browser = web_hub.Browser(self.host, self.port)
        _status, _headers, page = browser.request('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        status, _headers, _ = browser.request('POST', '/login', {'login_token': token, 'password': PASSWORD})
        self.assertEqual(status, 303)
        return browser

    def issue_key(self, browser: web_hub.Browser, name: str, grade: str) -> str:
        """The web's own key form (``/settings/keys``); the raw key is shown once on the reply page."""
        _status, _headers, page = browser.request('GET', '/settings/keys')
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        status, _headers, reply = browser.request('POST', '/settings/keys', {
            'csrf': csrf, 'name': name, 'grade': grade, 'scope': 'all'})
        self.assertEqual(status, 200, reply)
        return RAW_KEY.search(reply).group(0)

    def mcp(self, key: str) -> McpClient:
        client = McpClient(self.host, self.port, key)
        client.initialize()
        client.initialized()
        return client

    def web_submit(self, browser: web_hub.Browser, project_id: str, title: str, body: str) -> str:
        _status, _headers, page = browser.request('GET', '/requests/new')
        fields = {name: re.search(rf'name="{name}" value="([^"]+)"', page).group(1) for name in ('csrf', 'nonce')}
        status, headers, reply = browser.request('POST', '/requests', {
            **fields, 'project': project_id, 'title': title, 'body': body})
        self.assertEqual(status, 303, reply)
        return headers['Location'].rsplit('/', 1)[1].split('?')[0]

    def command_running(self, ui, root: Path, rid: str, seconds: str) -> tuple[str, int]:
        """A linked Task whose worker runs ``sleep SECONDS`` in the host shell: (task id, the sleep pid)."""
        answer = self.to_worker(ui, rid)
        self.assertEqual(answer['status'], 'dispatched', answer)
        self.until(lambda: self.worker_tasks(), 30, 'the TASK went to the worker')
        self.until(lambda: self.row(root, rid)['state'] == 'RUNNING', 30)
        from workbench.contracts.ui_v1 import ClientType
        sent = ui.request(ClientType.INPUT, f'tool terminal {json.dumps({"command": f"sleep {seconds}"})}\n'.encode(),
                          pane='worker_omp')
        self.assertTrue(sent.get('ok'), sent)
        pids: list = []
        wait_until(lambda: bool(pids.extend(sleeps(seconds)) or pids), 30)
        self.assertEqual(len(pids), 1, pids)
        self.until(lambda: (self.view(root, rid).get('command') or {}).get('label') == f'sleep {seconds}', 30,
                   'the Hub sees the running command')
        return answer['task_id'], pids[0]

    def box_pids_without_hub(self) -> list[int]:
        return [pid for pid in self.box.ours() if pid != self.hub_pid]


class OwnerFlowTests(FinalCase):
    def test_owner_login_submit_auto_review_link_run_close_and_both_result_views(self):
        """Item 1 (owner origin): web login -> request -> auto notice -> manager show -> to_worker(request_id) ->
        host command -> RUNNING -> manager close success -> web and MCP summary / full views; item 6: single writer."""
        root, project_id, _ids, ui = self.start_web_hub()
        browser = self.login()
        rid = self.web_submit(browser, project_id, 'Owner request', 'Build the thing\nline two')
        self.assertEqual(self.row(root, rid)['state'] in ('NEW', 'REVIEWING'), True)
        self.until(lambda: self.row(root, rid)['state'] == 'REVIEWING', 40, 'P2-AC-24 auto review delivery')
        notices = [n for n in self.inbox_notices() if n['request_id'] == rid]
        self.assertEqual([(n['event'], n['origin'], n['title']) for n in notices], [('new', '소유자', 'Owner request')])
        shown = self.inbox(ui, 'show', rid)
        self.assertEqual((shown['status'], shown['request']['body'], shown['request']['origin']['kind']),
                         ('ok', 'Build the thing\nline two', 'owner'))
        task_id, sleeper = self.command_running(ui, root, rid, '716.3')
        self.assertEqual(self.row(root, rid)['state'], 'RUNNING')
        self.assertEqual(self.spec_of(root, task_id, 1)['inbox_request_id'], rid)
        # item 6 (TI-T03): only the Hub holds the inbox; backend, panes and the sleeping command never do
        inbox_files = str(root / '.wb' / 'inbox.')  # inbox.sqlite3 (+ -wal/-shm) and inbox.lock
        others = self.box_pids_without_hub()
        self.assertTrue(others, 'the backend and its panes are running')
        self.assertEqual(fd_holders(inbox_files, others), [], 'no backend/pane/command process holds the inbox')
        self.assertEqual(fd_holders(inbox_files, [self.hub_pid]), [self.hub_pid], 'positive control: the Hub does')
        self.assertEqual(lock_holders(root / '.wb' / 'inbox.lock'), {self.hub_pid}, 'the Hub is the single writer')
        # another process cannot write the same inbox while the Hub holds it (P2-AC-01 single writer)
        from workbench.hub.inbox.service import InboxError, InboxService, Principal, ProjectRef
        rival = InboxService()
        try:
            rival.set_projects([ProjectRef(project_id, root, 'alpha')])
            with self.assertRaises(InboxError) as caught:
                rival.submit(Principal.owner(), project_id, 'rival', 'rival', None)
            self.assertEqual(caught.exception.code, 'inbox_busy')
        finally:
            rival.close()
        # the running web detail shows the linked Task
        status, _headers, page = browser.request('GET', f'/requests/{rid}')
        self.assertEqual(status, 200)
        self.assertIn(task_id, page)
        # the manager reports and closes (the long host command is still alive; the sandbox ends it)
        self.assertTrue(os.path.exists(f'/proc/{sleeper}'))
        closed = self.inbox(ui, 'close', rid, outcome='success', summary='Built and verified TI19-SUMMARY.')
        self.assertEqual((closed['status'], closed['state']), ('ok', 'CLOSED'), closed)
        row = self.row(root, rid)
        self.assertEqual((row['outcome'], row['result_summary']), ('success', 'Built and verified TI19-SUMMARY.'))
        status, _headers, page = browser.request('GET', f'/requests/{rid}')
        self.assertEqual(status, 200)
        self.assertIn('TI19-SUMMARY', page, 'web summary')
        status, headers, full = browser.request('GET', f'/requests/{rid}/full')
        self.assertEqual(status, 200)
        self.assertIn(task_id, full, 'web full view lists the linked Task')
        self.assertTrue(headers['Content-Type'].startswith('text/html'))
        # MCP: an editor key sees the summary and the full view
        editor = self.mcp(self.issue_key(browser, 'ti19-editor', 'editor'))
        summary = editor.tool('inbox_result', request_id=rid)
        self.assertIn('TI19-SUMMARY', json.dumps(summary, ensure_ascii=False))
        fullview = editor.tool('inbox_result', request_id=rid, full=True)
        self.assertIn(task_id, json.dumps(fullview, ensure_ascii=False))

    def test_requester_key_submit_isolation_and_result_through_mcp(self):
        """Item 1 (external requester key origin) + P2-AC-33-style isolation of one requester from another."""
        root, project_id, _ids, ui = self.start_web_hub()
        browser = self.login()
        key_a = self.issue_key(browser, 'req-a', 'requester')
        key_b = self.issue_key(browser, 'req-b', 'requester')
        a, b = self.mcp(key_a), self.mcp(key_b)
        names = {tool['name'] for tool in a.list_tools()}
        self.assertNotIn('inbox_update', names)
        self.assertNotIn('inbox_set_state', names)
        self.assertIn('inbox_submit', names)
        first = a.tool('inbox_submit', project_id=project_id, title='From A', body='external body',
                       idempotency_key='ti19-idem-1')
        again = a.tool('inbox_submit', project_id=project_id, title='From A', body='external body',
                       idempotency_key='ti19-idem-1')
        self.assertEqual((first['created'], again['created'], again['request_id']),
                         (True, False, first['request_id']))
        rid = first['request_id']
        self.until(lambda: self.row(root, rid)['state'] == 'REVIEWING', 40)
        notice = [n for n in self.inbox_notices() if n['request_id'] == rid][0]
        self.assertEqual(notice['origin'], 'key:req-a')
        shown = self.inbox(ui, 'show', rid)
        self.assertEqual((shown['request']['origin']['kind'], shown['request']['origin'].get('name')), ('api_key', 'req-a'))
        # another requester can neither read nor learn that it exists
        error = b.tool_error('inbox_get', request_id=rid)
        missing = b.tool_error('inbox_get', request_id='11111111-2222-4333-8444-555555555555')
        self.assertEqual(error['code'], missing['code'])
        self.assertEqual([item['request_id'] for item in b.tool('inbox_list')['requests']], [])
        # an API key cannot do web things; a web cookie cannot do MCP things
        reply = a.raw('POST', json.dumps({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/list'}).encode(),
                      {'Authorization': None, 'Cookie': '; '.join(f'{k}={v}' for k, v in browser.cookies.items())})
        self.assertEqual(reply.status, 401, 'MCP never accepts the web cookie')
        status, _headers, _ = web_hub.Browser(self.host, self.port).request('GET', '/requests')
        self.assertIn(status, (303, 401))
        # the manager runs and closes it; the requester reads the summary and the full view
        task_id, _sleeper = self.command_running(ui, root, rid, '716.4')
        closed = self.inbox(ui, 'close', rid, outcome='failed', summary='It failed TI19-REQ-SUMMARY.')
        self.assertEqual(closed['state'], 'CLOSED')
        got = a.tool('inbox_result', request_id=rid)
        self.assertIn('TI19-REQ-SUMMARY', json.dumps(got, ensure_ascii=False))
        full = a.tool('inbox_result', request_id=rid, full=True)
        self.assertIn(task_id, json.dumps(full, ensure_ascii=False))
        self.assertEqual(b.tool_error('inbox_result', request_id=rid)['code'], error['code'])


class HubLifetimeTests(FinalCase):
    def test_hub_stop_leaves_core_running_and_a_new_hub_keeps_requests(self):
        """Item 2 (P2-AC-01/23): `wb hub stop` mid-Task keeps backend, worker and command; the manager tool answers
        `inbox_unavailable`; the next Hub finds the request and the link as they were."""
        root, project_id, _ids, ui = self.start_web_hub()
        browser = self.login()
        rid = self.web_submit(browser, project_id, 'Long job', 'run a long command')
        self.until(lambda: self.row(root, rid)['state'] == 'REVIEWING', 40)
        task_id, sleeper = self.command_running(ui, root, rid, '716.5')
        backend_before = self.box.backends()
        panes_before = self.box.pane_processes(self.box.status(root))
        stopped = self.box.wb('hub', 'stop', '--yes', timeout=60)
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertTrue(os.path.exists(f'/proc/{sleeper}'), 'the host command keeps running')
        self.assertEqual(self.box.backends(), backend_before, 'the same backend process')
        panes_now = self.box.pane_processes(self.box.status(root))
        self.assertEqual(panes_now, panes_before, 'manager, worker and shell panes are the same processes')
        down = self.inbox(ui, 'show', rid)
        self.assertEqual((down['status'], down['reason']), ('rejected', 'inbox_unavailable'), down)
        self.box.start_hub()
        self.locate_web()
        self.assertEqual(self.box.backends(), backend_before)
        row = self.row(root, rid)
        self.assertEqual((row['state'], row['task_id']), ('RUNNING', task_id), 'request and link survived the Hub')
        self.until(lambda: self.inbox(ui, 'show', rid).get('status') == 'ok', 30, 'the manager tool works again')
        # the web login still works with the same password after the restart
        again = self.login()
        status, _headers, page = again.request('GET', f'/requests/{rid}')
        self.assertEqual(status, 200)
        self.assertIn('Long job', page)

    def test_requests_made_while_the_project_is_off_are_noticed_in_order_after_the_web_power_on(self):
        """Item 3 (P2-AC-04/23): intake works with the project off; the web power-on delivers them in seq order."""
        config = Path(self.box.env['XDG_CONFIG_HOME']) / 'omp-workbench'
        config.mkdir(parents=True, exist_ok=True, mode=0o700)
        (config / 'hub.toml').write_text('[web]\nbind = "127.0.0.1"\nport = 0\n')
        done = subprocess.run([sys.executable, '-m', 'workbench', 'hub', 'passwd', '--stdin'], env=self.box.env,
                              cwd=self.box.base, input=PASSWORD + '\n', capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        root, project_id = self.box.project('alpha')
        self.box.register(self.box.entry(root, project_id))
        self.box.start_hub()
        self.locate_web()
        browser = self.login()
        rids = [self.web_submit(browser, project_id, title, f'{title} body') for title in ('first', 'second', 'third')]
        time.sleep(5.0)  # more than two dispatcher passes
        self.assertEqual([self.row(root, rid)['delivery_state'] for rid in rids], ['pending'] * 3)
        self.assertEqual(self.inbox_notices(), [], 'nothing is sent to a project that is off')
        self.assertFalse((root / '.wb' / 'backend.json').exists() and self.box.backends(), 'no backend yet')
        # power on through the web (confirm page -> POST), not through the library
        status, _headers, page = browser.request('GET', f'/projects/{project_id}/power-on')
        self.assertEqual(status, 200)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        status, headers, reply = browser.request('POST', f'/projects/{project_id}/power-on', {'csrf': csrf})
        self.assertIn(status, (200, 303), reply)
        self.until(lambda: bool(self.box.backends()), 60, 'the web power-on started the backend')
        self.until(lambda: all(self.row(root, rid)['state'] == 'REVIEWING' for rid in rids), 90)
        self.assertEqual([(n['request_id'], n['seq']) for n in self.inbox_notices()],
                         [(rid, seq) for seq, rid in enumerate(rids, start=1)])


class BoardPageTests(unittest.TestCase):
    """Item: the Board page, its assets and CSP, WebSocket attach / takeover, key refusal and Hub stop with the set
    on a real Hub with the fake Board ``omp`` (the ``Rig`` of ``tests/hub/test_board_independent.py``)."""

    def test_board_page_assets_csp_websocket_takeover_key_refusal_and_stop_with_the_set(self):
        import test_board_independent as board_tests
        from support.ws_client import Tab
        rig = board_tests.Rig(self)
        browser = rig.browser
        status, headers, page = browser.request('GET', '/board')
        self.assertEqual(status, 200)
        sources = re.findall(r'(?:src|href)="(/[^"]+)"', page)
        scripts = [item for item in sources if item.endswith('.js')]
        self.assertTrue(any('xterm' in item for item in scripts), sources)
        self.assertTrue(any(item.endswith('board.js') for item in scripts), sources)
        self.assertNotRegex(page, r'(?:src|href)="https?://', 'no external URL')
        for item in sources:
            code, asset_headers, _ = browser.request('GET', item)
            if item.startswith('/static') or item.endswith(('.js', '.css')):
                self.assertEqual(code, 200, item)
        csp_board = headers['Content-Security-Policy']
        self.assertIn("style-src 'self' 'unsafe-inline'", csp_board)
        self.assertIn("script-src 'self'", csp_board)
        self.assertNotIn("'unsafe-inline'", csp_board.replace("style-src 'self' 'unsafe-inline'", ''))
        for path in ('/requests', '/settings', '/login'):
            code, other, _ = browser.request('GET', path)
            self.assertNotIn("'unsafe-inline'", other.get('Content-Security-Policy', ''), path)
        self.assertIsNone(rig.board(), 'serving the page starts nothing')
        url = f'http://{rig.box.wait_listening(rig.hub.pid)[0]}:{rig.box.wait_listening(rig.hub.pid)[1]}'
        cookie = browser.cookies['wb_hub_session']
        csrf = rig.csrf()

        async def scenario() -> None:
            import aiohttp
            # an API key (no cookie) cannot open the terminal
            from workbench.hub import auth
            db = auth.HubDb.open(rig.box.state)
            try:
                key = auth.ApiKeyStore(db).issue('ws-key', 'editor', {'all': True}, None, now=time.time()).raw
            finally:
                db.close()
            keyed = Tab(url, None, headers={'Authorization': f'Bearer {key}'})
            with self.assertRaises(aiohttp.WSServerHandshakeError) as refused:
                await keyed.connect()
            self.assertIn(refused.exception.status, (401, 403))
            await keyed.close()
            self.assertIsNone(rig.board(), 'a refused upgrade starts nothing')
            old, new = Tab(url, cookie), Tab(url, cookie)
            try:
                first = await old.attach(csrf)
                self.assertEqual(first['heartbeat'], 10)
                second = await new.attach(csrf)
                self.assertEqual(second['epoch'], first['epoch'] + 1)
                taken = await old.event('taken_over')
                self.assertEqual(taken['epoch'], second['epoch'])
                self.assertEqual(await old.wait_closed(10), 4001)
                channels = second['channels']
                self.assertIn(channels['agent']['state'], ('starting', 'running'))
                self.assertIn(channels['shell']['state'], ('starting', 'running'))
            finally:
                await old.close()
                await new.close()

        asyncio.run(scenario())
        # the set is up now: stop with a terminal and answer y -> the set ends, then the Hub (item: hub stop)
        board = rig.board()
        self.assertIsNotNone(board)
        pids = [board[name]['pid'] for name in ('agent', 'shell') if board.get(name)]
        code, text = rig.tty_stop('y')
        self.assertEqual(code, 0, text)
        self.assertIn('Board agent', text)
        self.assertEqual(rig.hub.wait(timeout=20), 0)
        for pid in pids:
            self.assertTrue(wait_until(lambda pid=pid: not alive(pid), 10), f'board process {pid} still alive')


if __name__ == '__main__':
    unittest.main()
