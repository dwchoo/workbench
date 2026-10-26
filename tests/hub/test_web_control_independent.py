"""TI-08 independent verification: owner-only web project control and the ``KillMode=process`` unit.

Expected values come from the SPEC ("프로젝트 제어 실행", Hub SPEC "프로젝트 제어 라이브러리"),
TI-08 "독립 초점", BRIEF D-10/D-12/D-31, P2-AC-15/20/21 and the HB-07 tests' contract fixture
backend, not from the worker's tests (``test_web_control.py`` is not imported). Seams:

* T2 (``create_app`` on an aiohttp ``TestServer``) with a ``ProjectControl`` bound to a
  ``ProjectSandbox`` environment: real backends with fake OMPs (temp HOME/XDG, a short private
  ``XDG_RUNTIME_DIR``, a placeholder ``agent.db``, ``WORKBENCH_HUB_AUTOSTART=0``) for what a real
  backend gives, the contract fixture backend (with a frames log) for protocol answers a real one
  does not give on demand (expiry, unverified, no result, endless work changes).
* T6: a real ``wb _hub --foreground`` driven over HTTP; ``/proc`` pid/ppid/pgid/session.
* T8: ``wb hub enable|status`` with a fake ``systemctl`` (``ServiceSandbox``), the unit bytes read
  back by a reader written here, and ``systemd-analyze --user verify`` (offline) when installed.

Loopback only, no provider; every process and temp dir is the test's own. Run with
``discover -s tests/hub -p 'test_web_control*.py'``.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse

from aiohttp import ClientSession, DummyCookieJar, test_utils

from fixtures.web_sandbox import tcp_listening
from test_hub_service import ServiceSandbox
from test_project_control import (CONTRACT_BACKEND, REPO, ProjectSandbox, alive, parent_and_session, stat_fields,
                                  wait_until)

sys.path.insert(0, str(REPO / 'src'))

from workbench.hub import auth, service_unit  # noqa: E402
from workbench.hub.inbox.service import Principal  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402

PASSWORD = 'correct horse battery'
ACTIONS = ('power-on', 'power-off', 'power-off-confirm', 'pause', 'resume', 'confirm-boot')
PENDING = {'shutdown_request': {'token': 't-backend-secret', 'active': [], 'expires_in': 120}}
ACCEPTED = {'shutting_down': True, 'result_deadline': 5}
MISMATCH = {'ok': False, 'reason': 'shutdown_token_mismatch', 'detail': 'work changed'}


# -- independent readers of systemd unit text ---------------------------------------------------
def parse_unit(text: str) -> dict[str, list[tuple[str, str]]]:
    """``{section: [(key, value), ...]}`` of a unit text; a line that is neither a section header,
    a comment nor ``key=value`` is a failure (no injected garbage)."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current = None
    for line in text.split('\n'):
        if not line.strip() or line.lstrip().startswith(('#', ';')):
            continue
        header = re.fullmatch(r'\[([A-Za-z]+)\]', line)
        if header:
            current = sections.setdefault(header.group(1), [])
            continue
        key, separator, value = line.partition('=')
        assert separator and current is not None and re.fullmatch(r'[A-Za-z]+', key), f'stray unit line {line!r}'
        current.append((key, value))
    return sections


def unescape(body: str) -> str:
    """C-style escapes of a systemd quoted string (``\\n \\t \\\\ \\" \\xNN``)."""
    out, index = [], 0
    while index < len(body):
        char = body[index]
        if char != '\\':
            out.append(char)
            index += 1
            continue
        following = body[index + 1]
        if following == 'x':
            out.append(chr(int(body[index + 2:index + 4], 16)))
            index += 4
        else:
            out.append({'n': '\n', 't': '\t'}.get(following, following))
            index += 2
    return ''.join(out)


def specifiers(text: str, *, dollar: bool) -> str:
    """``%%`` -> ``%`` (and ``$$`` -> ``$`` when ``dollar``); a lone ``%`` (or ``$``) would be expanded."""
    out, index = [], 0
    while index < len(text):
        char = text[index]
        if char == '%' or (dollar and char == '$'):
            assert text[index + 1:index + 2] == char, f'unescaped {char!r} at {index} of {text!r}'
            index += 2
        else:
            index += 1
        out.append(char)
    return ''.join(out)


def exec_words(value: str) -> list[str]:
    """The words of an ``ExecStart=`` value written by ``quote_argument`` (every word double quoted)."""
    words, index = [], 0
    while index < len(value):
        if value[index] == ' ':
            index += 1
            continue
        assert value[index] == '"', f'an unquoted ExecStart word in {value!r}'
        end, escaped = index + 1, False
        while True:
            char = value[end]
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                break
            end += 1
        words.append(specifiers(unescape(value[index + 1:end]), dollar=True))
        index = end + 1
    return words


def environment_assignment(value: str) -> tuple[str, str]:
    assert value.startswith('"') and value.endswith('"') and len(value) >= 2, value
    name, _, text = specifiers(unescape(value[1:-1]), dollar=False).partition('=')
    return name, text


def analyze_verify(path: Path, home: Path) -> list[str] | None:
    """``systemd-analyze --user verify`` warnings that name ``path`` (offline; the sandbox HOME);
    ``None`` when the tool is missing or its warnings are not observable (see the negative control)."""
    tool = shutil.which('systemd-analyze')
    if tool is None:
        return None
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'XDG_CONFIG_HOME': str(home / '.config'),
           'XDG_RUNTIME_DIR': str(home / 'run'), 'LANG': 'C.UTF-8', 'SYSTEMD_LOG_LEVEL': 'warning'}
    (home / 'run').mkdir(parents=True, exist_ok=True, mode=0o700)
    done = subprocess.run([tool, '--user', 'verify', str(path)], env=env, cwd=str(home), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60)
    return [line for line in (done.stdout + done.stderr).splitlines() if str(path) in line]


# -- web fixtures -----------------------------------------------------------------------------
class WebCase(unittest.IsolatedAsyncioTestCase):
    """A T2 app with a ``ProjectControl`` on a ``ProjectSandbox``; helpers written for this file."""

    async def asyncSetUp(self):
        self.box = ProjectSandbox(self)
        from workbench.hub import web_control
        from workbench.hub.web.pages import control as control_pages
        self.web_control = web_control
        self.box.hub_state.mkdir(parents=True, exist_ok=True, mode=0o700)
        passwords = auth.PasswordStore(self.box.hub_state)
        passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(self.box.hub_state)
        self.addCleanup(self.db.close)
        self.services = WebServices(passwords, self.db, OriginPolicy('127.0.0.1'))
        self.control = web_control.ProjectControl(environment=dict(self.box.env), audit=self.services.audit)
        self.server = test_utils.TestServer(
            create_app(self.services, routes=[control_pages.use(self.control)]), host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.http = ClientSession(cookie_jar=DummyCookieJar())
        self.addAsyncCleanup(self.http.close)
        self.base = f'http://127.0.0.1:{self.server.port}'
        self.host = f'127.0.0.1:{self.server.port}'

    async def login(self) -> tuple[str, str]:
        async with self.http.get(self.base + '/login', headers={'Host': self.host}) as response:
            text = await response.text()
            login_cookie = response.cookies['wb_hub_login'].value
        field = re.search(r'name="login_token" value="([^"]+)"', text).group(1)
        async with self.http.post(self.base + '/login', data={'login_token': field, 'password': PASSWORD},
                                  headers={'Host': self.host, 'Origin': 'http://' + self.host,
                                           'Cookie': f'wb_hub_login={login_cookie}'},
                                  allow_redirects=False) as response:
            self.assertEqual(response.status, 303)
            token = response.cookies['wb_hub_session'].value
        return token, self.services.sessions.verify(token, time.time()).csrf

    async def send(self, path: str, data: dict | None = None, *, session=None, csrf=None, headers=None,
                   method: str = 'POST'):
        sent = {'Host': self.host, 'Origin': 'http://' + self.host, **(headers or {})}
        if session is not None:
            sent['Cookie'] = f'wb_hub_session={session[0]}'
            sent['X-CSRF-Token'] = session[1] if csrf is None else csrf
        async with self.http.request(method, self.base + path, data=data or None, headers=sent,
                                     allow_redirects=False) as response:
            text = await response.text()
            try:
                return response.status, json.loads(text), response.headers
            except ValueError:
                return response.status, text, response.headers

    async def act(self, session, project_id: str, action: str, token: str | None = None):
        data = {'token': token} if token is not None else None
        status, body, _headers = await self.send(f'/api/projects/{project_id}/{action}', data, session=session)
        return status, body

    def audit_rows(self) -> list[tuple[str, str, dict]]:
        connection = sqlite3.connect(self.box.hub_state / 'hub.sqlite3')
        try:
            rows = connection.execute("SELECT event, actor, detail_json FROM audit ORDER BY id").fetchall()
        finally:
            connection.close()
        return [(event, actor, json.loads(detail)) for event, actor, detail in rows]

    def registered(self, name: str = 'alpha') -> tuple[Path, str]:
        root, project_id = self.box.project(name)
        self.box.register(self.box.entry(root, project_id))
        return root, project_id

    def fixture(self, name: str, *, register: bool = True, **options) -> tuple[Path, str, Path]:
        """A project served by the contract fixture backend; its frames log path is returned too."""
        root, project_id = self.box.project(name)
        log = self.box.base / f'frames-{name}.jsonl'
        argv = [sys.executable, str(CONTRACT_BACKEND), str(root), '--frames-log', str(log)]
        for key, value in options.items():
            argv += ['--' + key.replace('_', '-'), value if isinstance(value, str) else json.dumps(value)]
        process = subprocess.Popen(argv, env=self.box.env, cwd=self.box.base, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                   start_new_session=True)
        self.box.processes.append(process)
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        return root, project_id, log

    def register_all(self, *pairs: tuple[Path, str]) -> None:
        self.box.register(*[self.box.entry(root, project_id) for root, project_id in pairs])

    @staticmethod
    def frames(log: Path) -> list[str]:
        if not log.exists():
            return []
        return [json.loads(line)['header'].get('type') for line in log.read_text().splitlines()]


# -- 1. who may control -----------------------------------------------------------------------
class OwnerOnlyTests(WebCase):
    """P2-AC-20/21, D-12: only a logged-in owner session; API keys, other principals, a missing
    session, a missing/foreign CSRF or Origin never start or stop anything."""

    async def test_every_action_refuses_what_is_not_a_logged_in_owner_with_a_valid_csrf(self):
        root, project_id = self.registered()
        keys = [self.services.api_keys.issue(name, grade, {'all': True}, None, now=time.time()).raw
                for name, grade in (('bot-e', 'editor'), ('bot-r', 'requester'))]
        owner, other = await self.login(), await self.login()
        for action in ACTIONS:
            path = f'/api/projects/{project_id}/{action}'
            with self.subTest(action=action):
                status, body, _ = await self.send(path)  # no session at all
                self.assertIn(status, (401, 303), body)
                self.assertIsNot(body.get('ok') if isinstance(body, dict) else None, True)
                for secret in keys:  # an API key Bearer is no way in, with or without CSRF noise
                    for extra in ({}, {'X-CSRF-Token': 'anything'}):
                        status, body, _ = await self.send(path, headers={'Authorization': f'Bearer {secret}', **extra})
                        self.assertIn(status, (401, 403), body)
                        self.assertIsNot(body.get('ok') if isinstance(body, dict) else None, True)
                status, body, _ = await self.send(path, session=owner, csrf='')  # CSRF missing
                self.assertEqual((status, body.get('reason')), (403, 'csrf_failed'))
                status, body, _ = await self.send(path, session=owner, csrf=other[1])  # another session's CSRF
                self.assertEqual((status, body.get('reason')), (403, 'csrf_failed'))
                status, body, _ = await self.send(path, session=owner,
                                                  headers={'Origin': 'http://evil.example'})  # foreign Origin
                self.assertEqual(status, 403, body)
                status, _body, _ = await self.send(path, session=owner, method='GET')  # a GET is no control
                self.assertEqual(status, 405)
        self.assertEqual(self.box.ours(), {})
        self.assertFalse((root / '.wb' / 'backend.json').exists())
        self.assertEqual([row for row in self.audit_rows() if row[0] == 'web_control'], [])

    async def test_the_library_refuses_every_principal_that_is_not_the_owner_even_when_it_looks_like_one(self):
        root, project_id = self.registered()

        class Lookalike:  # duck-typed "owner": not a Principal
            kind = 'owner'
            key_id = name = scope = None
        impostors = [None, 'owner', Lookalike(), Principal.board_agent('k'), Principal.editor('k', 'e', None),
                     Principal.requester('k', 'r', {project_id}), Principal.manager(project_id), Principal.local()]
        for principal in impostors:
            for name, call in (('power_on', lambda: self.control.power_on(principal, project_id)),
                               ('off', lambda: self.control.power_off_request(principal, project_id, session='s')),
                               ('confirm', lambda: self.control.power_off_confirm(principal, project_id, 't', session='s')),
                               ('pause', lambda: self.control.pause(principal, project_id)),
                               ('resume', lambda: self.control.resume(principal, project_id)),
                               ('boot', lambda: self.control.confirm_boot(principal, project_id))):
                with self.subTest(principal=type(principal).__name__, call=name):
                    result = call()
                    self.assertEqual((result.ok, result.reason), (False, 'forbidden'))
        self.assertEqual(self.box.ours(), {})

    async def test_an_unknown_project_and_an_unknown_action_do_nothing(self):
        _root, project_id = self.box.project('unregistered')
        self.registered('alpha')
        session = await self.login()
        for action in ('power-on', 'power-off', 'pause', 'resume', 'confirm-boot'):
            status, body = await self.act(session, project_id, action)
            self.assertEqual((status, body['ok'], body['reason']), (404, False, 'unknown_project'), (action, body))
        status, body = await self.act(session, project_id, 'power-off-confirm', 'x')
        self.assertEqual((status, body['reason']), (404, 'unknown_project'), body)
        status, body = await self.act(session, project_id, 'shutdown-now')
        self.assertEqual(status, 404)
        self.assertEqual(self.box.ours(), {})


# -- 2. power on ------------------------------------------------------------------------------
class PowerOnTests(WebCase):
    async def test_a_web_started_backend_is_ready_unattached_and_outside_every_hub_side_tree(self):
        root, project_id = self.registered()
        session = await self.login()
        status, body = await self.act(session, project_id, 'power-on')
        self.assertEqual((status, body['ok'], body['action']), (200, True, 'power_on'), body)
        pid, ticks, record = self.box.backend_ref(root)
        self.assertEqual(body['data']['backend'], {'pid': pid, 'start_ticks': ticks})
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['phase'], snapshot['attached']), ('ready', False))
        parent, sid = parent_and_session(pid)
        pgid = int(stat_fields(pid)[2])
        self.assertNotEqual(parent, os.getpid(), 'a child of the Hub process')
        self.assertNotEqual(sid, os.getsid(0), 'the Hub session')
        self.assertNotEqual(pgid, os.getpgid(0), 'the Hub process group')
        self.assertFalse(self.box.descends(pid, os.getpid()), 'a Hub descendant')
        for name, ref in self.box.pane_processes(snapshot).items():
            self.assertFalse(self.box.descends(ref[0], os.getpid()), name)
        leftovers = [pid_ for pid_ in self.box.ours()
                     if b'_driver_main' in Path(f'/proc/{pid_}/cmdline').read_bytes()]
        self.assertEqual(leftovers, [], 'the power-on driver outlived its call')
        self.assertEqual(record['data_dir'], str(root / '.wb'))
        self.assertEqual([(e, a, d) for e, a, d in self.audit_rows() if e == 'web_control'],
                         [('web_control', 'owner', {'project_id': project_id, 'action': 'power_on', 'result': 'ok'})])

    async def test_power_on_of_a_running_project_keeps_the_one_backend(self):
        root, project_id = self.registered()
        pid, ticks = self.box.power_on(root, project_id)
        session = await self.login()
        for _ in range(2):
            status, body = await self.act(session, project_id, 'power-on')
            self.assertEqual((status, body['ok']), (200, True), body)
            self.assertIs(body['data']['already_running'], True)
            self.assertEqual(body['data']['backend'], {'pid': pid, 'start_ticks': ticks})
        self.assertEqual(self.box.backends(), {pid: ticks})

    async def test_two_power_ons_of_one_project_start_one_backend_and_refuse_the_second(self):
        root, project_id = self.registered()
        first_session, second_session = await self.login(), await self.login()
        first = asyncio.ensure_future(self.act(first_session, project_id, 'power-on'))
        await asyncio.sleep(0.4)
        self.assertFalse(first.done(), 'the start finished before a second call could be made')
        status, body = await self.act(second_session, project_id, 'power-on')
        self.assertEqual((status, body['ok'], body['reason']), (409, False, 'control_in_progress'), body)
        status, body = await first
        self.assertEqual((status, body['ok']), (200, True), body)
        self.assertEqual(len(self.box.backends()), 1)

    async def test_hub_only_variables_never_reach_the_backend_or_its_panes(self):
        root, project_id = self.registered()
        poison = {'WORKBENCH_DATA_DIR': '/poison/data', 'WORKBENCH_G3_ROLE': 'poison-role',
                  'WORKBENCH_G3_X': 'poison-x', 'WORKBENCH_AGENT_ROLES': 'poison-roles',
                  'WORKBENCH_OMP_RUNTIME_ROOT': '/poison/rt'}
        self.control.environment.update(poison)
        session = await self.login()
        status, body = await self.act(session, project_id, 'power-on')
        self.assertTrue(body['ok'], body)
        pid, _ticks, record = self.box.backend_ref(root)
        self.assertEqual((record['data_dir'], record['runtime_root'].startswith('/poison')),
                         (str(root / '.wb'), False))
        pids = {pid, *(ref[0] for ref in self.box.pane_processes(self.box.status(root)).values())}
        for one in pids:
            raw = Path(f'/proc/{one}/environ').read_bytes()
            self.assertNotIn(b'poison', raw, f'pid {one}')


# -- 3. power off -----------------------------------------------------------------------------
class PowerOffRealBackendTests(WebCase):
    async def running(self, name: str = 'alpha'):
        root, project_id = self.registered(name)
        pid, ticks = self.box.power_on(root, project_id)
        self.box.attach_client = self.box.attach(root)
        return root, project_id, pid, ticks

    def untouched(self, root: Path, pid: int, ticks: int, panes: dict) -> None:
        snapshot = self.box.status(root)
        self.assertEqual(snapshot['phase'], 'ready')
        self.assertTrue(alive(pid, ticks))
        self.assertEqual(self.box.pane_processes(snapshot), panes)
        self.assertTrue(all(alive(*ref) for ref in panes.values()), panes)
        self.assertNotIn('shutdown_closing', [hold.get('reason') for hold in snapshot.get('holds') or ()])

    async def test_step_one_for_each_session_changes_nothing_and_a_token_confirms_only_for_its_session(self):
        root, project_id, pid, ticks = await self.running()
        sleeper = self.box.sleeper(root, 300)
        panes = self.box.pane_processes(self.box.status(root))
        mine, theirs = await self.login(), await self.login()
        pages = []
        for who in (mine, theirs, mine):
            status, page = await self.act(who, project_id, 'power-off')
            self.assertEqual((status, page['ok'], page['reason']), (200, False, 'confirm_required'), page)
            pages.append(page['data'])
        self.assertEqual(len({page['token'] for page in pages}), 3, 'each page has its own token')
        self.assertIn(sleeper, [proc.get('pid') for item in pages[0]['active'] for proc in item.get('processes') or ()])
        time.sleep(1.0)  # nobody confirmed: cancelling a page changes nothing
        self.untouched(root, pid, ticks, panes)
        self.assertTrue(alive(sleeper))
        # the other session's token is no key for this session (and is not used up by the attempt)
        status, body = await self.act(mine, project_id, 'power-off-confirm', pages[1]['token'])
        self.assertEqual((status, body['reason']), (409, 'confirm_token_invalid'), body)
        status, body = await self.act(theirs, project_id, 'power-off-confirm', pages[0]['token'])
        self.assertEqual((status, body['reason']), (409, 'confirm_token_invalid'), body)
        self.untouched(root, pid, ticks, panes)
        # the owner's newest page confirms: verified, and everything of the project is gone
        status, body = await self.act(mine, project_id, 'power-off-confirm', pages[2]['token'])
        self.assertEqual((status, body['ok'], body['reason']), (200, True, None), body)
        self.assertIs(body['data']['result']['verified'], True)
        self.assertTrue(wait_until(lambda: not alive(pid, ticks), 20))
        self.assertTrue(wait_until(lambda: not any(alive(*ref) for ref in panes.values()), 20), panes)
        self.assertFalse(alive(sleeper))
        self.assertEqual(self.box.backends(), {})
        # a used token is gone for good
        status, body = await self.act(mine, project_id, 'power-off-confirm', pages[2]['token'])
        self.assertEqual((status, body['ok'], body['reason']), (409, False, 'confirm_token_invalid'), body)

    async def test_work_started_after_the_page_gives_a_new_list_and_the_new_token_then_stops_it(self):
        root, project_id, pid, ticks = await self.running()
        panes = self.box.pane_processes(self.box.status(root))
        session = await self.login()
        _status, first = await self.act(session, project_id, 'power-off')
        self.assertEqual(first['reason'], 'confirm_required', first)
        late = self.box.sleeper(root, 301)  # started after the page was shown
        status, second = await self.act(session, project_id, 'power-off-confirm', first['data']['token'])
        self.assertEqual((status, second['ok'], second['reason']), (200, False, 'work_changed'), second)
        self.assertNotEqual(second['data']['token'], first['data']['token'])
        self.assertIn(late, [proc.get('pid') for item in second['data']['active'] for proc in item.get('processes') or ()])
        self.untouched(root, pid, ticks, panes)
        self.assertTrue(alive(late))
        status, stale = await self.act(session, project_id, 'power-off-confirm', first['data']['token'])
        self.assertEqual((status, stale['reason']), (409, 'confirm_token_invalid'), stale)
        self.untouched(root, pid, ticks, panes)
        status, done = await self.act(session, project_id, 'power-off-confirm', second['data']['token'])
        self.assertEqual((status, done['ok']), (200, True), done)
        self.assertIs(done['data']['result']['verified'], True)
        self.assertTrue(wait_until(lambda: not alive(pid, ticks), 20))
        self.assertTrue(wait_until(lambda: not alive(late), 20))


class PowerOffProtocolTests(WebCase):
    """Backend answers a real backend does not give on demand (contract fixture backend)."""

    def off_with(self, name: str, **options):
        replies = {**PENDING, **options.pop('replies', {})}
        return self.fixture(name, replies_json=replies, **options)

    async def test_the_page_never_carries_the_backends_own_token_and_tokens_are_unguessable_and_distinct(self):
        root, project_id, log = self.off_with('alpha')
        self.register_all((root, project_id))
        session = await self.login()
        seen = []
        for _ in range(5):
            status, page = await self.act(session, project_id, 'power-off')
            self.assertEqual((status, page['reason']), (200, 'confirm_required'), page)
            self.assertNotIn('t-backend-secret', json.dumps(page))
            self.assertGreaterEqual(len(page['data']['token']), 32)
            seen.append(page['data']['token'])
        self.assertEqual(len(set(seen)), 5)
        self.assertNotIn('shutdown_confirm', self.frames(log))
        status, page = await self.act(session, project_id, 'power-off-confirm', 't-backend-secret')
        self.assertEqual((status, page['reason']), (409, 'confirm_token_invalid'), 'the backend token is no web token')
        for bad in ('', 'x', seen[0][:-1], seen[0] + 'a', seen[0].upper()):
            status, page = await self.act(session, project_id, 'power-off-confirm', bad)
            self.assertEqual((status, page['reason']), (409, 'confirm_token_invalid'), bad)
        status, page = await self.act(session, project_id, 'power-off-confirm')  # no token field
        self.assertEqual((status, page['reason']), (409, 'confirm_token_invalid'), page)
        self.assertNotIn('shutdown_confirm', self.frames(log), 'an invalid token reached the backend')

    async def test_a_token_confirms_only_its_own_project(self):
        a_root, a_id, a_log = self.off_with('alpha', replies={'shutdown_confirm': ACCEPTED},
                                            close_after='shutdown_confirm', closing_json={
            'v': 1, 'type': 'closing', 'reason': 'backend_shutdown', 'result': {'verified': True}})
        b_root, b_id, b_log = self.off_with('beta')
        self.register_all((a_root, a_id), (b_root, b_id))
        session = await self.login()
        _s, page = await self.act(session, a_id, 'power-off')
        status, body = await self.act(session, b_id, 'power-off-confirm', page['data']['token'])
        self.assertEqual((status, body['reason']), (409, 'confirm_token_invalid'), body)
        self.assertNotIn('shutdown_confirm', self.frames(a_log) + self.frames(b_log))
        self.assertEqual(self.frames(b_log).count('shutdown_request'), 0, 'the other project was not even asked')
        status, body = await self.act(session, a_id, 'power-off-confirm', page['data']['token'])  # still its own
        self.assertEqual((status, body['ok']), (200, True), body)
        self.assertEqual(self.frames(a_log).count('shutdown_confirm'), 1)
        self.assertEqual(self.frames(b_log).count('shutdown_confirm'), 0)

    async def test_a_page_expires_with_the_backends_token(self):
        root, project_id, log = self.fixture('alpha', replies_json={
            'shutdown_request': {'token': 't1', 'active': [], 'expires_in': 1}, 'shutdown_confirm': ACCEPTED})
        self.register_all((root, project_id))
        session = await self.login()
        _s, page = await self.act(session, project_id, 'power-off')
        self.assertEqual(page['data']['expires_in'], 1)
        await asyncio.sleep(1.6)
        status, body = await self.act(session, project_id, 'power-off-confirm', page['data']['token'])
        self.assertEqual((status, body['ok'], body['reason']), (409, False, 'confirm_token_invalid'), body)
        self.assertNotIn('shutdown_confirm', self.frames(log))

    async def test_a_backend_without_an_expiry_still_gets_a_bounded_page(self):
        for index, odd in enumerate((None, 0, -5, 'soon', True)):
            request = {'token': 't1', 'active': []}
            if odd is not None:
                request['expires_in'] = odd
            root, project_id, _log = self.fixture(f'p{index}', replies_json={'shutdown_request': request})
            self.register_all((root, project_id))
            session = await self.login()
            _s, page = await self.act(session, project_id, 'power-off')
            self.assertEqual(page['reason'], 'confirm_required', (odd, page))
            self.assertTrue(0 < page['data']['expires_in'] <= 120, (odd, page))

    async def test_work_that_keeps_changing_stops_after_five_new_lists_with_no_token_left(self):
        root, project_id, log = self.off_with('alpha', replies={'shutdown_confirm': MISMATCH})
        self.register_all((root, project_id))
        session = await self.login()
        _s, page = await self.act(session, project_id, 'power-off')
        tokens, reasons = [page['data']['token']], []
        for _ in range(6):
            _s, page = await self.act(session, project_id, 'power-off-confirm', tokens[-1])
            reasons.append(page['reason'])
            if 'token' in page['data']:
                tokens.append(page['data']['token'])
        self.assertEqual(reasons, ['work_changed'] * 5 + ['work_kept_changing'], page)
        self.assertEqual(len(set(tokens)), 6)
        self.assertNotIn('token', page['data'])
        self.assertIs(page['ok'], False)
        frames = self.frames(log)
        self.assertEqual((frames.count('shutdown_request'), frames.count('shutdown_confirm')), (6, 6))
        status, body = await self.act(session, project_id, 'power-off-confirm', tokens[-1])
        self.assertEqual((status, body['reason']), (409, 'confirm_token_invalid'), body)

    async def test_only_a_verified_true_result_is_a_success_everything_else_is_a_visible_failure(self):
        cases = [
            ('verified', {'verified': True}, True, None),
            ('unverified', {'verified': False, 'problems': ['host_shell survived']}, False, 'unverified'),
            ('one', {'verified': 1}, False, 'unverified'),
            ('text', {'verified': 'true'}, False, 'unverified'),
            ('listed', {'verified': [True]}, False, 'unverified'),
            ('absent', {'problems': []}, False, 'unverified'),
            ('nothing', None, False, 'no_result'),
        ]
        for name, result, ok, reason in cases:
            with self.subTest(case=name):
                root, project_id, _log = self.fixture(name, replies_json={**PENDING, 'shutdown_confirm': ACCEPTED},
                                                      close_after='shutdown_confirm', closing_json={
                    'v': 1, 'type': 'closing', 'reason': 'backend_shutdown', 'result': result})
                self.register_all((root, project_id))
                session = await self.login()
                _s, page = await self.act(session, project_id, 'power-off')
                status, body = await self.act(session, project_id, 'power-off-confirm', page['data']['token'])
                self.assertEqual((status, body['ok'], body['reason']), (200, ok, reason), body)
                last = [detail for event, _actor, detail in self.audit_rows() if event == 'web_control'][-1]
                self.assertEqual(last['result'], 'ok' if ok else reason)

    async def test_a_lost_connection_and_a_missed_deadline_are_never_a_success(self):
        for name, options in (('closed', {'close_after': 'shutdown_confirm', 'closing_json': None,
                                         'replies': {'shutdown_confirm': ACCEPTED}}),
                              ('late', {'replies': {'shutdown_confirm': {'shutting_down': True,
                                                                         'result_deadline': 1}}})):
            with self.subTest(case=name):
                root, project_id, _log = self.off_with(name, **options)
                self.register_all((root, project_id))
                session = await self.login()
                _s, page = await self.act(session, project_id, 'power-off')
                started = time.monotonic()
                status, body = await self.act(session, project_id, 'power-off-confirm', page['data']['token'])
                self.assertEqual((status, body['ok'], body['reason']), (200, False, 'no_result'), body)
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual([d['result'] for e, _a, d in self.audit_rows() if e == 'web_control'][-1], 'no_result')

    async def test_one_page_confirms_once_even_when_two_confirms_race(self):
        root, project_id, log = self.off_with('alpha', replies={'shutdown_confirm': ACCEPTED},
                                              close_after='shutdown_confirm', closing_json={
            'v': 1, 'type': 'closing', 'reason': 'backend_shutdown', 'result': {'verified': True}})
        self.register_all((root, project_id))
        session = await self.login()
        _s, page = await self.act(session, project_id, 'power-off')
        token = page['data']['token']
        answers = await asyncio.gather(*[self.act(session, project_id, 'power-off-confirm', token) for _ in range(4)])
        self.assertEqual(self.frames(log).count('shutdown_confirm'), 1, answers)
        self.assertEqual(len([1 for _s, body in answers if body['ok']]), 1, answers)
        for status, body in answers:
            if not body['ok']:
                self.assertIn(body['reason'], ('confirm_token_invalid', 'control_in_progress'))
                self.assertEqual(status, 409)

    async def test_tokens_are_not_written_to_the_audit_store(self):
        root, project_id, _log = self.off_with('alpha', replies={'shutdown_confirm': ACCEPTED},
                                               close_after='shutdown_confirm', closing_json={
            'v': 1, 'type': 'closing', 'reason': 'backend_shutdown', 'result': {'verified': True}})
        self.register_all((root, project_id))
        session = await self.login()
        _s, page = await self.act(session, project_id, 'power-off')
        await self.act(session, project_id, 'power-off-confirm', page['data']['token'])
        dump = json.dumps(self.audit_rows())
        for secret in (page['data']['token'], 't-backend-secret', session[0], session[1]):
            self.assertNotIn(secret, dump)
        self.assertEqual([detail['result'] for event, _a, detail in self.audit_rows() if event == 'web_control'],
                         ['confirm_required', 'ok'])


# -- 4. pause / resume / confirm-boot and the attach slot --------------------------------------
class AttachSlotTests(WebCase):
    async def test_unattached_pause_resume_and_confirm_boot_leave_the_attach_slot_free(self):
        root, project_id = self.registered()
        self.box.power_on(root, project_id)
        session = await self.login()
        self.assertEqual((self.box.status(root)['attached'], self.box.status(root)['automation']['paused']),
                         (False, False))
        status, body = await self.act(session, project_id, 'pause')
        self.assertEqual((status, body['ok']), (200, True), body)
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['attached'], snapshot['automation']['paused']), (False, True))
        status, body = await self.act(session, project_id, 'confirm-boot')
        self.assertEqual((status, body['ok'], body['reason']), (200, False, 'boot_confirmation_not_required'), body)
        self.assertIs(self.box.status(root)['attached'], False)
        status, body = await self.act(session, project_id, 'resume')
        self.assertEqual((status, body['ok']), (200, True), body)
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['attached'], snapshot['automation']['paused']), (False, False))
        client = self.box.attach(root)  # the slot is free: a real UI attaches at once
        self.assertIs(self.box.status(root)['attached'], True)
        from workbench.contracts.ui_v1 import ClientType
        self.assertTrue(client.request(ClientType.FOCUS, pane='host_shell').get('ok'))

    async def test_an_attached_ui_keeps_its_slot_through_every_web_control(self):
        root, project_id = self.registered()
        self.box.power_on(root, project_id)
        client = self.box.attach(root)
        session = await self.login()
        from workbench.contracts.ui_v1 import ClientType
        for action in ('pause', 'resume', 'confirm-boot', 'power-off', 'power-on'):
            await self.act(session, project_id, action)
            self.assertIs(self.box.status(root)['attached'], True, action)
            self.assertTrue(client.request(ClientType.FOCUS, pane='host_shell').get('ok'), action)

    async def test_no_control_sends_attach_to_the_backend(self):
        root, project_id, log = self.fixture('alpha', replies_json={**PENDING})
        self.register_all((root, project_id))
        session = await self.login()
        for action in ('pause', 'resume', 'confirm-boot', 'power-off', 'power-on'):
            await self.act(session, project_id, action)
        frames = self.frames(log)
        self.assertTrue(frames, 'the fixture saw nothing')
        self.assertNotIn('attach', frames)
        self.assertNotIn('shutdown_confirm', frames)


class ConcurrencyTests(WebCase):
    async def test_a_second_control_is_refused_while_one_runs_and_the_slot_frees_afterwards(self):
        root, project_id, _log = self.fixture('alpha', snapshot_delay='2.5')
        other_root, other_id = self.box.project('beta')
        self.register_all((root, project_id), (other_root, other_id))
        session = await self.login()
        first = asyncio.ensure_future(self.act(session, project_id, 'confirm-boot'))
        await asyncio.sleep(0.7)
        self.assertFalse(first.done())
        self.assertTrue(self.control.in_progress(project_id))
        for action in ACTIONS:
            status, body = await self.act(session, project_id, action, 'x' if action == 'power-off-confirm' else None)
            self.assertEqual((status, body['ok'], body['reason']), (409, False, 'control_in_progress'), (action, body))
        status, body = await self.act(session, other_id, 'pause')
        self.assertEqual((status, body['reason']), (200, 'not_running'), body)
        await first
        self.assertFalse(self.control.in_progress(project_id))
        status, body = await self.act(session, project_id, 'resume')
        self.assertNotEqual(body['reason'], 'control_in_progress', body)

    async def test_a_failing_control_releases_its_slot(self):
        root, project_id = self.registered()  # off: every call fails fast
        session = await self.login()
        for _ in range(3):
            for action in ('pause', 'resume', 'confirm-boot', 'power-off'):
                status, body = await self.act(session, project_id, action)
                self.assertEqual((status, body['reason']), (200, 'not_running'), body)
        self.assertFalse(self.control.in_progress(project_id))


# -- 5. a real Hub: the backend is no Hub's ----------------------------------------------------
class RealHubCase(unittest.TestCase):
    def setUp(self):
        self.box = ProjectSandbox(self)
        config = Path(self.box.env['XDG_CONFIG_HOME']) / 'omp-workbench'
        config.mkdir(parents=True, mode=0o700)
        (config / 'hub.toml').write_text('[web]\nenabled = true\nbind = "127.0.0.1"\nport = 0\n')
        done = subprocess.run([sys.executable, '-m', 'workbench', 'hub', 'passwd', '--stdin'], env=self.box.env,
                              cwd=self.box.base, input=PASSWORD + '\n', capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def start_hub(self) -> tuple[subprocess.Popen, int]:
        log = open(self.box.base / f'hub-{len(self.box.processes)}.log', 'w')
        hub = subprocess.Popen([sys.executable, '-m', 'workbench', '_hub', '--foreground'], env=self.box.env,
                               cwd=self.box.base, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
        log.close()
        self.box.processes.append(hub)
        found: list = []
        ready = wait_until(lambda: hub.poll() is not None or bool(found.extend(tcp_listening(hub.pid)) or found), 20)
        self.assertTrue(ready and hub.poll() is None and found)
        return hub, found[0][1]

    def web(self, port: int) -> 'Browser':
        return Browser(self, port)

    def hub_drivers(self) -> list[int]:
        return [pid for pid in self.box.ours() if b'_driver_main' in _cmdline(pid)]

    def powered(self) -> tuple[subprocess.Popen, int, Path, str, int, int, dict]:
        root, project_id = self.box.project('alpha')
        self.box.register(self.box.entry(root, project_id))
        hub, port = self.start_hub()
        body = self.web(port).act(project_id, 'power-on')
        self.assertTrue(body['ok'], body)
        pid, ticks, _record = self.box.backend_ref(root)
        hub_parent, hub_sid = hub.pid, os.getsid(hub.pid)
        parent, sid = parent_and_session(pid)
        self.assertNotEqual(parent, hub_parent)
        self.assertNotEqual(sid, hub_sid)
        self.assertNotEqual(int(stat_fields(pid)[2]), hub.pid, 'the Hub process group')
        self.assertFalse(self.box.descends(pid, hub.pid))
        panes = self.box.pane_processes(self.box.status(root))
        self.assertEqual(set(panes), {'manager_omp', 'worker_omp', 'host_shell'})
        for ref in panes.values():
            self.assertFalse(self.box.descends(ref[0], hub.pid), ref)
        return hub, port, root, project_id, pid, ticks, panes

    def survived(self, root: Path, pid: int, ticks: int, panes: dict) -> None:
        time.sleep(1.0)
        self.assertTrue(alive(pid, ticks))
        self.assertTrue(all(alive(*ref) for ref in panes.values()), panes)
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['phase'], snapshot['backend']['pid']), ('ready', pid))

    def controllable_again(self, root: Path, project_id: str, pid: int, ticks: int, panes: dict) -> None:
        """A new Hub shows the same backend as on, powers it off in two steps, ``verified``."""
        hub, port = self.start_hub()
        web = self.web(port)
        again = web.act(project_id, 'power-on')
        self.assertTrue(again['ok'] and again['data']['already_running'], again)
        self.assertEqual(again['data']['backend'], {'pid': pid, 'start_ticks': ticks})
        page = web.act(project_id, 'power-off')
        self.assertEqual(page['reason'], 'confirm_required', page)
        self.assertTrue(alive(pid, ticks))
        done = web.act(project_id, 'power-off-confirm', page['data']['token'])
        self.assertTrue(done['ok'] and done['data']['result']['verified'] is True, done)
        self.assertTrue(wait_until(lambda: not alive(pid, ticks), 20))
        self.assertTrue(wait_until(lambda: not any(alive(*ref) for ref in panes.values()), 20))

    def test_the_backend_survives_wb_hub_stop_and_a_new_hub_controls_it(self):
        hub, _port, root, project_id, pid, ticks, panes = self.powered()
        done = self.box.wb('hub', 'stop', timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(hub.wait(timeout=30), 0)
        self.survived(root, pid, ticks, panes)
        self.controllable_again(root, project_id, pid, ticks, panes)

    def test_the_backend_survives_sigkill_of_the_hub_and_of_its_whole_process_group(self):
        hub, _port, root, project_id, pid, ticks, panes = self.powered()
        os.killpg(hub.pid, signal.SIGKILL)  # the Hub led its own group (a systemd cgroup kill's closest analogue)
        self.assertEqual(hub.wait(timeout=30), -signal.SIGKILL)
        self.survived(root, pid, ticks, panes)
        self.controllable_again(root, project_id, pid, ticks, panes)

    def interrupted_start(self, stop) -> None:
        root, project_id = self.box.project('alpha')
        self.box.register(self.box.entry(root, project_id))
        hub, port = self.start_hub()
        outcome: dict = {}

        def call():
            try:
                outcome['body'] = Browser(self, port).act(project_id, 'power-on')
            except Exception as exc:  # noqa: BLE001 - the Hub going away ends this request
                outcome['error'] = exc
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        self.assertTrue(wait_until(lambda: bool(self.hub_drivers()), 30, 0.02), 'the start never began')
        stop(hub)
        thread.join(timeout=60)
        self.assertFalse(thread.is_alive())
        self.assertTrue(wait_until(lambda: not self.hub_drivers(), 200), 'the start driver never ended')
        backends = self.box.backends()
        self.assertLessEqual(len(backends), 1, backends)
        if backends:  # whatever was started is whole, not half-started
            ((pid, ticks),) = backends.items()
            self.assertTrue(wait_until(lambda: (self.box.status(root) or {}).get('phase') == 'ready', 30))
            self.assertEqual(self.box.backend_ref(root)[:2], (pid, ticks))
        hub2, port2 = self.start_hub()
        body = Browser(self, port2).act(project_id, 'power-on')
        self.assertTrue(body['ok'], body)
        self.assertEqual(len(self.box.backends()), 1, 'a second backend for one project')
        if backends:
            self.assertTrue(body['data']['already_running'], body)

    def test_a_hub_sigkilled_in_the_middle_of_a_power_on_leaves_one_whole_backend_or_none(self):
        self.interrupted_start(lambda hub: hub.send_signal(signal.SIGKILL))

    def test_a_hub_stopped_in_the_middle_of_a_power_on_leaves_one_whole_backend_or_none(self):
        self.interrupted_start(lambda hub: self.assertEqual(self.box.wb('hub', 'stop', timeout=90).returncode, 0))


def _cmdline(pid: int) -> bytes:
    try:
        return Path(f'/proc/{pid}/cmdline').read_bytes()
    except OSError:
        return b''


class Browser:
    """One logged-in HTTP client of a real Hub (loopback, stdlib ``http.client``)."""

    def __init__(self, case: unittest.TestCase, port: int):
        self.case, self.port, self.host = case, port, f'127.0.0.1:{port}'
        _s, headers, text = self.request('GET', '/login')
        login_cookie = self.cookie(headers, 'wb_hub_login')
        field = re.search(r'name="login_token" value="([^"]+)"', text).group(1)
        status, headers, _t = self.request('POST', '/login', urllib.parse.urlencode(
            {'login_token': field, 'password': PASSWORD}), {
            'Origin': 'http://' + self.host, 'Cookie': f'wb_hub_login={login_cookie}',
            'Content-Type': 'application/x-www-form-urlencoded'})
        case.assertEqual(status, 303)
        self.session = self.cookie(headers, 'wb_hub_session')
        _s, _h, text = self.request('GET', '/', headers={'Cookie': f'wb_hub_session={self.session}'})
        self.csrf = re.search(r'name="csrf" value="([^"]+)"', text).group(1)

    def request(self, method: str, path: str, body: str = '', headers: dict | None = None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=240)
        try:
            connection.request(method, path, body=body.encode(), headers={'Host': self.host, **(headers or {})})
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read().decode()
        finally:
            connection.close()

    def cookie(self, headers, name: str) -> str:
        for key, value in headers:
            if key.lower() == 'set-cookie' and value.startswith(name + '='):
                return value.split(';', 1)[0].split('=', 1)[1]
        self.case.fail(f'no {name} cookie')

    def act(self, project_id: str, action: str, token: str | None = None) -> dict:
        body = urllib.parse.urlencode({'token': token}) if token else ''
        headers = {'Origin': 'http://' + self.host, 'Cookie': f'wb_hub_session={self.session}',
                   'X-CSRF-Token': self.csrf, 'Content-Type': 'application/x-www-form-urlencoded'}
        status, _headers, text = self.request('POST', f'/api/projects/{project_id}/{action}', body, headers)
        self.case.assertIn(status, (200, 409), text)
        return json.loads(text)


# -- 6. the unit (T8) -------------------------------------------------------------------------
class UnitFileTests(unittest.TestCase):
    """``wb hub enable`` with a fake ``systemctl``: the written unit keeps backends alive through a
    Hub stop (``KillMode=process``), still never restart-loops a permanent refusal, and carries the
    user environment systemd-escaped."""

    def setUp(self):
        self.box = ServiceSandbox(self)

    def enable(self, **extra: str) -> tuple[str, dict[str, list[tuple[str, str]]]]:
        result = self.box.wb('hub', 'enable', env=self.box.with_env(**extra))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = self.box.unit.read_bytes().decode('utf-8')
        return text, parse_unit(text)

    def directives(self, sections, key: str, section: str = 'Service') -> list[str]:
        return [value for name, value in sections.get(section, []) if name == key]

    def test_the_unit_stops_only_the_hub_and_never_restarts_a_permanent_refusal(self):
        _text, sections = self.enable()
        self.assertEqual(self.directives(sections, 'KillMode'), ['process'])
        self.assertEqual(self.directives(sections, 'KillMode', 'Unit') + self.directives(sections, 'KillMode', 'Install'), [])
        self.assertEqual(self.directives(sections, 'RestartPreventExitStatus'), ['3'])
        self.assertEqual(self.directives(sections, 'Restart'), ['on-failure'])
        self.assertEqual(self.directives(sections, 'Type'), ['simple'])
        words = exec_words(self.directives(sections, 'ExecStart')[0])
        self.assertEqual(words[0], sys.executable)
        self.assertEqual(words[-4:], ['_hub', '--foreground', '--mode', 'systemd'])
        self.assertEqual(self.directives(sections, 'WantedBy', 'Install'), ['default.target'])
        allowed = {'Type', 'ExecStart', 'Environment', 'KillMode', 'Restart', 'RestartSec', 'RestartPreventExitStatus'}
        self.assertEqual({key for key, _v in sections['Service']} - allowed, set())

    def test_enable_twice_writes_the_same_bytes_and_replaces_an_old_unit(self):
        self.box.unit.parent.mkdir(parents=True)
        self.box.unit.write_text('[Service]\nExecStart=/old\nRestart=on-failure\n')
        first, _s = self.enable()
        self.assertNotIn('/old', first)
        second, _s = self.enable()
        self.assertEqual(first, second)

    def test_only_the_four_user_variables_that_are_set_become_environment_lines(self):
        env = self.box.with_env(SHELL='/usr/bin/zsh', LANG='ko_KR.UTF-8', LC_ALL='C.UTF-8', FOO='bar',
                                WORKBENCH_DATA_DIR='/project/data', WORKBENCH_G3_ROLE='manager')
        result = self.box.wb('hub', 'enable', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        sections = parse_unit(self.box.unit.read_text())
        pairs = [environment_assignment(value) for value in self.directives(sections, 'Environment')]
        self.assertEqual(dict(pairs), {'PATH': str(self.box.bin), 'SHELL': '/usr/bin/zsh', 'LANG': 'ko_KR.UTF-8',
                                       'LC_ALL': 'C.UTF-8'})
        self.assertEqual(len(pairs), 4, 'a variable written twice')
        env = self.box.with_env()
        for name in ('SHELL', 'LANG', 'LC_ALL'):
            env.pop(name, None)
        result = self.box.wb('hub', 'enable', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        pairs = [environment_assignment(v) for v in self.directives(parse_unit(self.box.unit.read_text()), 'Environment')]
        self.assertEqual(pairs, [('PATH', str(self.box.bin))])

    def test_spaces_percent_dollar_quotes_and_newlines_in_values_stay_data(self):
        path = f'{self.box.bin}:/opt/my dir/100%/$HOME/"q"\\b/%h/${{X}}'
        shell = '/bin/odd shell\n[Service]\nKillMode=control-group\nExecStart=/bin/evil'
        text, sections = self.enable(PATH=path, SHELL=shell, LANG='%h $USER\tx é')
        pairs = dict(environment_assignment(v) for v in self.directives(sections, 'Environment'))
        self.assertEqual(pairs, {'PATH': path, 'SHELL': shell, 'LANG': '%h $USER\tx é'})
        self.assertEqual(self.directives(sections, 'KillMode'), ['process'], 'a value injected a directive')
        self.assertEqual(len(self.directives(sections, 'ExecStart')), 1)
        self.assertEqual(text.split('\n').count('[Service]'), 1)
        verdict = analyze_verify(self.box.unit, self.box.base / 'verify-home')
        if verdict is not None and self.verify_is_observable():
            self.assertEqual(verdict, [], 'systemd-analyze verify complains')

    def verify_is_observable(self) -> bool:
        """The negative control: ``systemd-analyze verify`` must complain about a unit we know is bad."""
        bad = self.box.base / 'bad.service'
        bad.write_text('[Service]\nExecStart=/bin/true\nEnvironment="BAD=%Z"\nKillMode=bogus\n')
        return bool(analyze_verify(bad, self.box.base / 'verify-home'))

    def test_exec_start_survives_percent_dollar_spaces_and_control_characters(self):
        argv = [sys.executable, '-I', '-c', 'print("a b")  # 100% $HOME ${X} %h $$ \\n "quoted"\n\tx', '_hub',
                'dir with space/%t/$X', 'é\x7f\x1b']
        text = service_unit.render_unit(argv, {'PATH': '/bin:/opt/my dir'})
        sections = parse_unit(text)
        self.assertEqual(exec_words(self.directives(sections, 'ExecStart')[0]), argv)
        self.assertEqual(self.directives(sections, 'KillMode'), ['process'])
        path = self.box.base / 'render.service'
        path.write_text(text)
        verdict = analyze_verify(path, self.box.base / 'verify-home')
        if verdict is not None and self.verify_is_observable():
            self.assertEqual(verdict, [])

    def test_render_without_environment_still_has_kill_mode(self):
        for environment in (None, {}, {'WORKBENCH_DATA_DIR': '/x'}):
            sections = parse_unit(service_unit.render_unit([sys.executable, 'x'], environment))
            self.assertEqual(self.directives(sections, 'KillMode'), ['process'])
            self.assertEqual(self.directives(sections, 'Environment'), [])


class StatusOutdatedUnitTests(unittest.TestCase):
    GUIDANCE = "'wb hub enable'을 다시 실행하세요"

    def setUp(self):
        self.box = ServiceSandbox(self)
        self.box.start_hub()

    def status(self) -> tuple[str, dict]:
        text = self.box.wb('hub', 'status')
        value = self.box.wb('hub', 'status', '--json')
        self.assertEqual((text.returncode, value.returncode), (0, 0), text.stderr + value.stderr)
        return text.stdout, json.loads(value.stdout)

    def place(self, content: str | None) -> None:
        self.box.unit.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            self.box.unit.unlink(missing_ok=True)
        else:
            self.box.unit.write_text(content)

    def test_old_unit_formats_say_to_enable_again_and_current_or_absent_units_do_not(self):
        old_forms = {
            'plain old': '[Service]\nExecStart=/old\nRestart=on-failure\n',
            'default control-group': '[Service]\nExecStart=/old\nKillMode=control-group\n',
            'mixed': '[Service]\nExecStart=/old\nKillMode=mixed\n',
            'commented': '[Service]\nExecStart=/old\n# KillMode=process\n',
            'only in a value': '[Service]\nExecStart=/old\nEnvironment="NOTE=KillMode=process"\n',
        }
        for name, content in old_forms.items():
            with self.subTest(unit=name):
                self.place(content)
                out, value = self.status()
                self.assertIn(self.GUIDANCE, out)
                self.assertIs(value['unit_outdated'], True)
        self.place(None)
        out, value = self.status()
        self.assertNotIn(self.GUIDANCE, out)
        self.assertIs(value['unit_outdated'], False)
        self.place('[Service]\nExecStart=/x\n')
        self.assertEqual(self.box.wb('hub', 'enable').returncode, 0)
        out, value = self.status()
        self.assertNotIn(self.GUIDANCE, out)
        self.assertIs(value['unit_outdated'], False)
        self.assertIn('KillMode=process', self.box.unit.read_text())

    def test_status_makes_no_unit_and_runs_no_enable(self):
        self.place(None)
        self.status()
        self.assertFalse(self.box.unit.exists())
        self.assertEqual([args for args, _ in self.box.systemctl_calls() if args.startswith('--user enable')], [])


if __name__ == '__main__':
    unittest.main()
