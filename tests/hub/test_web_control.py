"""Hub-side project control for the web (TI-08, seams T2 + T6).

The control functions (``workbench.hub.web_control.ProjectControl``) and their minimal JSON
endpoints run in-process: ``create_app`` with aiohttp ``TestServer`` (T2) and a controller bound to
the sandbox environment. The projects are real backends with fake OMPs (``ProjectSandbox`` of
``test_project_control``: temp HOME/XDG, a short private ``XDG_RUNTIME_DIR``, a placeholder
``agent.db``, ``WORKBENCH_HUB_AUTOSTART=0``). Expected values come from ``/proc`` (pid, ppid,
session), ``backend.json``, ``wb status --json`` snapshots, ``hub.sqlite3`` rows read with stdlib
``sqlite3`` and the SPEC/ticket texts. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
from pathlib import Path
import re
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
from test_project_control import REPO, ProjectSandbox, alive, parent_and_session, wait_until

sys.path.insert(0, str(REPO / 'src'))

from workbench.hub import auth  # noqa: E402
from workbench.hub.inbox.service import Principal  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402

PASSWORD = 'correct horse battery'


class WebControlCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.box = ProjectSandbox(self)
        from workbench.hub import web_control
        from workbench.hub.web.pages import control as control_pages
        self.web_control = web_control
        self.box.hub_state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.passwords = auth.PasswordStore(self.box.hub_state)
        self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(self.box.hub_state)
        self.addCleanup(self.db.close)
        self.services = WebServices(self.passwords, self.db, OriginPolicy('127.0.0.1'))
        self.control = web_control.ProjectControl(environment=self.box.env, audit=self.services.audit)
        self.server = test_utils.TestServer(
            create_app(self.services, routes=[control_pages.use(self.control)]), host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.http = ClientSession(cookie_jar=DummyCookieJar())
        self.addAsyncCleanup(self.http.close)
        self.base = f'http://127.0.0.1:{self.server.port}'
        self.host = f'127.0.0.1:{self.server.port}'

    # -- web helpers ------------------------------------------------------------------------
    async def login(self) -> tuple[str, str]:
        """(session cookie, csrf) of a fresh owner session."""
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
        session = self.services.sessions.verify(token, time.time())
        return token, session.csrf

    async def post(self, path: str, data: dict | None = None, *, session: tuple[str, str] | None = None,
                   csrf: str | None = None, headers: dict | None = None):
        sent = {'Host': self.host, 'Origin': 'http://' + self.host, **(headers or {})}
        if session is not None:
            sent['Cookie'] = f'wb_hub_session={session[0]}'
            sent['X-CSRF-Token'] = session[1] if csrf is None else csrf
        async with self.http.post(self.base + path, data=data or {}, headers=sent,
                                  allow_redirects=False) as response:
            text = await response.text()
            try:
                body = json.loads(text)
            except ValueError:
                body = text
            return response.status, body

    async def control_call(self, session, project_id: str, action: str, data: dict | None = None):
        return await self.post(f'/api/projects/{project_id}/{action}', data, session=session)

    def audit(self) -> list[tuple]:
        connection = sqlite3.connect(self.box.hub_state / 'hub.sqlite3')
        try:
            rows = connection.execute("SELECT event, actor, detail_json FROM audit WHERE event = 'web_control' "
                                      "ORDER BY id").fetchall()
        finally:
            connection.close()
        return [(event, actor, json.loads(detail)) for event, actor, detail in rows]

    def registered(self, name: str = 'alpha') -> tuple[Path, str]:
        root, project_id = self.box.project(name)
        self.box.register(self.box.entry(root, project_id))
        return root, project_id


ACTIONS = ('power-on', 'power-off', 'power-off-confirm', 'pause', 'resume', 'confirm-boot')


class PermissionTests(WebControlCase):
    """P2-AC-20/21: only a logged-in owner session reaches the control functions."""

    async def test_principals_other_than_the_owner_are_refused_and_nothing_starts(self):
        root, project_id = self.registered()
        others = [None, Principal.board_agent('k1'), Principal.editor('k2', 'e', None),
                  Principal.requester('k3', 'r', None), Principal.manager(project_id), Principal.local()]
        for principal in others:
            for call in (lambda: self.control.power_on(principal, project_id),
                         lambda: self.control.power_off_request(principal, project_id, session='s'),
                         lambda: self.control.power_off_confirm(principal, project_id, 't', session='s'),
                         lambda: self.control.pause(principal, project_id),
                         lambda: self.control.resume(principal, project_id),
                         lambda: self.control.confirm_boot(principal, project_id)):
                with self.subTest(principal=principal):
                    result = call()
                    self.assertEqual((result.ok, result.reason), (False, 'forbidden'), result)
        self.assertEqual(self.box.ours(), {})
        self.assertFalse((root / '.wb' / 'backend.json').exists())

    async def test_endpoints_refuse_api_keys_missing_sessions_and_missing_csrf(self):
        root, project_id = self.registered()
        secret = self.services.api_keys.issue('bot', 'editor', {'all': True}, None, now=time.time()).raw
        session = await self.login()
        for action in ACTIONS:
            path = f'/api/projects/{project_id}/{action}'
            with self.subTest(action=action):
                status, body = await self.post(path, headers={'Authorization': f'Bearer {secret}'})
                self.assertEqual((status, body), (401, {'ok': False, 'reason': 'unauthorized'}))
                status, _body = await self.post(path)
                self.assertEqual(status, 401)
                status, body = await self.post(path, session=session, csrf='wrong')
                self.assertEqual((status, body), (403, {'ok': False, 'reason': 'csrf_failed'}))
        self.assertEqual(self.box.ours(), {})
        self.assertFalse((root / '.wb' / 'backend.json').exists())
        self.assertEqual(self.audit(), [])


class PowerOnTests(WebControlCase):
    """P2-AC-21: a session turns a project on; the backend is ``ready``/``attached: false`` and no Hub child."""

    async def test_power_on_starts_a_ready_unattached_backend_outside_the_hub_tree(self):
        root, project_id = self.registered()
        session = await self.login()
        status, body = await self.control_call(session, project_id, 'power-on')
        self.assertEqual(status, 200, body)
        self.assertEqual((body['ok'], body['action'], body['project_id']), (True, 'power_on', project_id), body)
        pid, ticks, _record = self.box.backend_ref(root)
        self.assertEqual(body['data']['backend'], {'pid': pid, 'start_ticks': ticks})
        self.assertTrue(alive(pid, ticks))
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['phase'], snapshot['attached']), ('ready', False), snapshot)
        parent, sid = parent_and_session(pid)
        self.assertNotEqual(parent, os.getpid())
        self.assertNotEqual(sid, os.getsid(0))
        self.assertEqual(self.audit(), [('web_control', 'owner',
                                         {'project_id': project_id, 'action': 'power_on', 'result': 'ok'})])

    async def test_power_on_of_a_running_project_says_so_and_keeps_the_one_backend(self):
        root, project_id = self.registered()
        session = await self.login()
        _status, first = await self.control_call(session, project_id, 'power-on')
        self.assertTrue(first['ok'], first)
        pid, ticks, _record = self.box.backend_ref(root)
        self.assertIs(first['data'].get('already_running', False), False, first)
        status, body = await self.control_call(session, project_id, 'power-on')
        self.assertEqual((status, body['ok']), (200, True), body)
        self.assertIs(body['data']['already_running'], True, body)
        self.assertEqual(body['data']['backend'], {'pid': pid, 'start_ticks': ticks})
        self.assertEqual(self.box.backends(), {pid: ticks})
        self.assertEqual(self.box.backend_ref(root)[:2], (pid, ticks))

    async def test_project_only_variables_of_the_hub_do_not_reach_the_backend(self):
        root, project_id = self.registered()
        self.control.environment.update({'WORKBENCH_DATA_DIR': str(self.box.base / 'elsewhere'),
                                         'WORKBENCH_G3_ROLE': 'manager', 'WORKBENCH_AGENT_ROLES': 'x',
                                         'WORKBENCH_OMP_RUNTIME_ROOT': str(self.box.base / 'rt')})
        session = await self.login()
        status, body = await self.control_call(session, project_id, 'power-on')
        self.assertTrue(body['ok'], body)
        pid, _ticks, record = self.box.backend_ref(root)
        self.assertEqual(record['data_dir'], str(root / '.wb'))
        environ = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
        names = {item.split(b'=', 1)[0] for item in environ if item}
        self.assertFalse(names & {b'WORKBENCH_DATA_DIR', b'WORKBENCH_AGENT_ROLES', b'WORKBENCH_OMP_RUNTIME_ROOT'},
                         names)
        self.assertFalse([name for name in names if name.startswith(b'WORKBENCH_G3_')], names)
        self.assertIn(b'WORKBENCH_OMP', names)

    def stub_driver(self, source: str, timeout: float) -> None:
        """The controller's driver replaced by ``python -c source`` (no backend is started)."""
        control = self.control
        control.driver_timeout = timeout
        control._driver_command = lambda _spec: [sys.executable, '-I', '-c', source]

    async def test_a_driver_timeout_is_start_failed_with_an_unknown_state_to_recheck(self):
        root, project_id = self.registered()
        self.stub_driver('import time; time.sleep(30)', timeout=1.0)
        session = await self.login()
        started = time.monotonic()
        status, body = await self.control_call(session, project_id, 'power-on')
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((status, body['ok'], body['reason']), (200, False, 'start_failed'), body)
        self.assertIn('상태를 확인', body['detail'])  # fixed text (fix 7): check the project state
        def sleepers() -> list[int]:
            found = []
            for pid in self.box.ours():
                try:
                    if b'time.sleep(30)' in Path(f'/proc/{pid}/cmdline').read_bytes():
                        found.append(pid)
                except OSError:
                    pass
            return found
        self.assertTrue(wait_until(lambda: not sleepers(), 10), 'the timed-out driver is ended')

    async def test_a_driver_that_floods_its_output_is_reported_with_a_bounded_tail(self):
        root, project_id = self.registered()
        flood = ("import sys; sys.stdout.write('y' * (8 << 20)); sys.stderr.write('x' * (8 << 20)); "
                 "sys.stderr.write('END-OF-OUTPUT')")
        self.stub_driver(flood, timeout=60)
        session = await self.login()
        status, body = await self.control_call(session, project_id, 'power-on')
        self.assertEqual((status, body['ok'], body['reason']), (200, False, 'start_failed'), body)
        # Security review fix 7: the JSON detail is the reason's fixed text, never the driver's output.
        self.assertNotIn('END-OF-OUTPUT', json.dumps(body))
        self.assertNotIn('xxxx', json.dumps(body))
        self.assertLessEqual(len(body['detail']), 200)

    async def test_an_unregistered_project_id_is_unknown_and_starts_nothing(self):
        root, project_id = self.box.project('alpha')  # initialized, not registered
        session = await self.login()
        status, body = await self.control_call(session, project_id, 'power-on')
        self.assertEqual((status, body['ok'], body['reason']), (404, False, 'unknown_project'), body)
        self.assertEqual(self.box.ours(), {})


class PowerOffTests(WebControlCase):
    """P2-AC-21 + HB-AC-09: the first step shows the active work and stops nothing; the confirm stops."""

    async def running(self, name: str = 'alpha'):
        root, project_id = self.registered(name)
        pid, ticks = self.box.power_on(root, project_id)
        self.box.attach_client = self.box.attach(root)
        return root, project_id, pid, ticks

    def unchanged(self, root: Path, pid: int, ticks: int, panes: dict) -> None:
        after = self.box.status(root)
        self.assertTrue(alive(pid, ticks))
        self.assertEqual(self.box.pane_processes(after), panes)
        self.assertTrue(all(alive(*ref) for ref in panes.values()), panes)
        self.assertNotIn('shutdown_closing', [hold.get('reason') for hold in after.get('holds') or ()])
        self.assertEqual(after['phase'], 'ready')

    async def test_the_first_step_shows_the_active_work_with_a_token_and_stops_nothing(self):
        root, project_id, pid, ticks = await self.running()
        sleeper = self.box.sleeper(root, 300)
        panes = self.box.pane_processes(self.box.status(root))
        session = await self.login()
        status, body = await self.control_call(session, project_id, 'power-off')
        self.assertEqual(status, 200, body)
        self.assertEqual((body['ok'], body['action'], body['reason']), (False, 'power_off', 'confirm_required'), body)
        token = body['data']['token']
        self.assertIsInstance(token, str)
        self.assertGreaterEqual(len(token), 32)
        jobs = [item for item in body['data']['active'] if item.get('kind') == 'shell_jobs']
        self.assertEqual(len(jobs), 1, body['data']['active'])
        self.assertIn(sleeper, [item.get('pid') for item in jobs[0]['processes']], jobs)
        self.assertGreater(body['data']['expires_in'], 0)
        time.sleep(1.0)  # nobody confirms (the page's cancel link changes nothing)
        self.unchanged(root, pid, ticks, panes)
        self.assertTrue(alive(sleeper))
        self.assertEqual(self.audit()[-1][2], {'project_id': project_id, 'action': 'power_off',
                                               'result': 'confirm_required'})

    async def test_work_changed_before_the_confirm_shows_the_new_list_with_a_new_token(self):
        root, project_id, pid, ticks = await self.running()
        panes = self.box.pane_processes(self.box.status(root))
        session = await self.login()
        _status, first = await self.control_call(session, project_id, 'power-off')
        self.assertEqual(first['reason'], 'confirm_required', first)
        shown = [proc.get('pid') for item in first['data']['active'] for proc in item.get('processes') or ()]
        sleeper = self.box.sleeper(root, 301)  # work starts after the page was shown
        self.assertNotIn(sleeper, shown)
        status, body = await self.control_call(session, project_id, 'power-off-confirm',
                                               {'token': first['data']['token']})
        self.assertEqual(status, 200, body)
        self.assertEqual((body['ok'], body['reason']), (False, 'work_changed'), body)
        self.assertIn('작업이 바뀌었습니다', body['detail'])
        self.assertNotEqual(body['data']['token'], first['data']['token'])
        listed = [proc.get('pid') for item in body['data']['active'] for proc in item.get('processes') or ()]
        self.assertIn(sleeper, listed, body['data']['active'])
        self.assertEqual(len(body['data']['refusals']), 1, body)
        self.unchanged(root, pid, ticks, panes)
        self.assertTrue(alive(sleeper))
        # the replaced token is used up
        status, again = await self.control_call(session, project_id, 'power-off-confirm',
                                                {'token': first['data']['token']})
        self.assertEqual((status, again['ok'], again['reason']), (409, False, 'confirm_token_invalid'), again)
        self.unchanged(root, pid, ticks, panes)

    async def test_work_that_keeps_changing_ends_after_five_new_lists_and_stops_nothing(self):
        root, project_id, pid, ticks = await self.running()
        session = await self.login()
        _status, page = await self.control_call(session, project_id, 'power-off')
        reasons = []
        for call in range(1, 7):  # the first list and five new ones (SPEC: 최대 5회)
            self.box.sleeper(root, 400 + call)
            _status, page = await self.control_call(session, project_id, 'power-off-confirm',
                                                    {'token': page['data']['token']})
            reasons.append(page['reason'])
        self.assertEqual(reasons, ['work_changed'] * 5 + ['work_kept_changing'], page)
        self.assertEqual(len(page['data']['refusals']), 6, page)
        self.assertNotIn('token', page['data'])
        self.assertTrue(alive(pid, ticks))
        self.assertEqual(self.box.status(root)['phase'], 'ready')

    async def test_a_new_first_step_of_the_same_session_and_project_drops_the_earlier_token(self):
        root, project_id, pid, ticks = await self.running()
        panes = self.box.pane_processes(self.box.status(root))
        session = await self.login()
        _status, first = await self.control_call(session, project_id, 'power-off')
        _status, second = await self.control_call(session, project_id, 'power-off')
        self.assertEqual((first['reason'], second['reason']), ('confirm_required', 'confirm_required'))
        status, body = await self.control_call(session, project_id, 'power-off-confirm',
                                               {'token': first['data']['token']})
        self.assertEqual((status, body['reason']), (409, 'confirm_token_invalid'), body)
        self.unchanged(root, pid, ticks, panes)
        status, body = await self.control_call(session, project_id, 'power-off-confirm',
                                               {'token': second['data']['token']})
        self.assertEqual((status, body['ok']), (200, True), body)
        self.assertTrue(wait_until(lambda: not alive(pid, ticks), 15))

    async def test_the_confirm_returns_the_verified_result_and_only_its_own_session_and_project_may_send_it(self):
        root, project_id, pid, ticks = await self.running()
        other_root, other_id = self.box.project('beta')
        self.box.register(self.box.entry(root, project_id), self.box.entry(other_root, other_id))
        panes = self.box.pane_processes(self.box.status(root))
        session, intruder = await self.login(), await self.login()
        _status, first = await self.control_call(session, project_id, 'power-off')
        token = first['data']['token']
        for who, target in ((intruder, project_id), (session, other_id)):
            status, body = await self.control_call(who, target, 'power-off-confirm', {'token': token})
            self.assertEqual((status, body['reason']), (409, 'confirm_token_invalid'), body)
        self.unchanged(root, pid, ticks, panes)
        status, body = await self.control_call(session, project_id, 'power-off-confirm', {'token': token})
        self.assertEqual((status, body['ok'], body['reason']), (200, True, None), body)
        self.assertIs(body['data']['result']['verified'], True, body)
        self.assertTrue(wait_until(lambda: not alive(pid, ticks), 15))
        self.assertTrue(wait_until(lambda: not any(alive(*ref) for ref in panes.values()), 15), panes)
        self.assertIsNone(self.box.status(root))
        self.assertEqual(self.box.backends(), {})
        self.assertEqual([detail['result'] for _event, _actor, detail in self.audit()],
                         ['confirm_required', 'confirm_token_invalid', 'confirm_token_invalid', 'ok'])


class AutomationControlTests(WebControlCase):
    """P2-AC-21: pause/resume/confirm-boot call the same backend behavior as HB-07 (results as is)."""

    async def test_pause_resume_and_confirm_boot_through_the_session_endpoint(self):
        root, project_id = self.registered()
        self.box.power_on(root, project_id)
        client = self.box.attach(root)
        session = await self.login()
        self.assertIs(self.box.status(root)['automation']['paused'], False)
        status, body = await self.control_call(session, project_id, 'pause')
        self.assertEqual((status, body['ok'], body['action']), (200, True, 'pause'), body)
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['automation']['paused'], snapshot['attached']), (True, True))
        status, body = await self.control_call(session, project_id, 'resume')
        self.assertEqual((status, body['ok'], body['action']), (200, True, 'resume'), body)
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['automation']['paused'], snapshot['attached']), (False, True))
        status, body = await self.control_call(session, project_id, 'confirm-boot')
        self.assertEqual((status, body['ok'], body['action'], body['reason']),
                         (200, False, 'confirm_boot', 'boot_confirmation_not_required'), body)
        from workbench.contracts.ui_v1 import ClientType
        self.assertTrue(client.request(ClientType.FOCUS, pane='host_shell').get('ok'))
        self.assertEqual([(detail['action'], detail['result']) for _event, _actor, detail in self.audit()],
                         [('pause', 'ok'), ('resume', 'ok'), ('confirm_boot', 'boot_confirmation_not_required')])

    async def test_an_unexpected_library_error_is_an_audited_result_and_frees_the_project(self):
        from unittest import mock
        from workbench.hub import projects
        root, project_id = self.registered()
        session = await self.login()
        with mock.patch.object(projects, 'pause', side_effect=RuntimeError('boom')):
            status, body = await self.control_call(session, project_id, 'pause')
        self.assertEqual((status, body['ok'], body['action'], body['reason']),
                         (200, False, 'pause', 'internal_error'), body)
        self.assertNotIn('RuntimeError', json.dumps(body))
        self.assertNotIn('boom', json.dumps(body))
        self.assertEqual(self.audit()[-1][2], {'project_id': project_id, 'action': 'pause',
                                               'result': 'internal_error'})
        status, body = await self.control_call(session, project_id, 'pause')  # the slot was released
        self.assertEqual((status, body['reason']), (200, 'not_running'), body)

    async def test_json_detail_is_fixed_text_never_exception_or_path_text(self):
        """Security review fix 7: ``/api/projects/..`` ``detail`` is one fixed sentence per reason."""
        from unittest import mock
        from workbench.hub import projects
        _root, project_id = self.registered()
        session = await self.login()
        secret = '/home/someone/.ssh/id_rsa: Permission denied'
        details = set()
        for error in (RuntimeError(secret), OSError(13, secret), ValueError(secret)):
            with mock.patch.object(projects, 'resume', side_effect=error):
                _status, body = await self.control_call(session, project_id, 'resume')
            self.assertEqual(body['reason'], 'internal_error', body)
            self.assertNotIn('.ssh', json.dumps(body))
            self.assertNotIn(type(error).__name__, json.dumps(body))
            details.add(body['detail'])
        self.assertEqual(len(details), 1, details)
        self.assertTrue(next(iter(details)))

    async def test_a_project_that_is_off_is_not_running_for_every_backend_call(self):
        root, project_id = self.registered()
        session = await self.login()
        for action in ('pause', 'resume', 'confirm-boot', 'power-off'):
            with self.subTest(action=action):
                status, body = await self.control_call(session, project_id, action)
                self.assertEqual((status, body['ok'], body['reason']), (200, False, 'not_running'), body)
        self.assertEqual(self.box.ours(), {})


class SlowFixtureCase(WebControlCase):
    def slow_fixture(self, name: str, delay: float = 3) -> tuple[Path, str]:
        """A project served by the contract fixture backend whose snapshots take ``delay`` s."""
        from test_project_control import CONTRACT_BACKEND
        root, project_id = self.box.project(name)
        process = subprocess.Popen([sys.executable, str(CONTRACT_BACKEND), str(root), '--snapshot-delay', str(delay)],
                                   env=self.box.env, cwd=self.box.base, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                   start_new_session=True)
        self.box.processes.append(process)
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        return root, project_id


class ConcurrentControlTests(SlowFixtureCase):
    """Ticket: a second control of the same project while one runs is ``control_in_progress``."""

    async def test_a_second_control_of_the_same_project_is_refused_while_the_first_runs(self):
        root, project_id = self.slow_fixture('alpha')
        other_root, other_id = self.box.project('beta')
        self.box.register(self.box.entry(root, project_id), self.box.entry(other_root, other_id))
        session = await self.login()
        first = asyncio.ensure_future(self.control_call(session, project_id, 'confirm-boot'))
        await asyncio.sleep(0.8)  # confirm_boot now waits for the fixture's 3 s snapshot
        self.assertFalse(first.done())
        for action in ('power-on', 'power-off', 'pause', 'resume', 'confirm-boot'):
            with self.subTest(action=action):
                status, body = await self.control_call(session, project_id, action)
                self.assertEqual((status, body['ok'], body['reason']), (409, False, 'control_in_progress'), body)
        status, body = await self.control_call(session, other_id, 'pause')  # another project is not held
        self.assertEqual((status, body['reason']), (200, 'not_running'), body)
        self.assertFalse(first.done())
        status, body = await first
        self.assertEqual(status, 200, body)
        self.assertNotEqual(body['reason'], 'control_in_progress', body)
        status, body = await self.control_call(session, project_id, 'pause')  # free again
        self.assertNotEqual(body['reason'], 'control_in_progress', body)


class ControlExecutorTests(SlowFixtureCase):
    """Security review P2: long controls run on the control's own bounded executor, never on the
    loop's shared default executor that session checks and logins use; a full one is ``control_busy``."""

    async def asyncSetUp(self):
        # a small shared default executor: controls running there would starve logins and sessions
        from concurrent.futures import ThreadPoolExecutor
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(4))
        await super().asyncSetUp()

    async def test_login_and_pages_answer_while_the_control_executor_is_full(self):
        size = self.web_control.CONTROL_WORKERS
        self.assertGreaterEqual(size, 4)
        projects = [self.slow_fixture(f'slow{index}', delay=8) for index in range(size + 1)]
        self.box.register(*(self.box.entry(root, project_id) for root, project_id in projects))
        session = await self.login()
        running = [asyncio.ensure_future(self.control_call(session, project_id, 'confirm-boot'))
                   for _root, project_id in projects[:size]]
        await asyncio.sleep(1.0)  # every control now waits for its fixture's 8 s snapshot
        self.assertFalse(any(task.done() for task in running))
        started = time.monotonic()
        status, body = await self.control_call(session, projects[size][1], 'confirm-boot')
        self.assertEqual((status, body['ok'], body['reason']), (503, False, 'control_busy'), body)
        self.assertLess(time.monotonic() - started, 1.5, 'a full control executor answers at once')
        started = time.monotonic()
        fresh = await self.login()  # scrypt and the session store run on the shared executor
        async with self.http.get(self.base + '/settings', headers={
                'Host': self.host, 'Cookie': f'wb_hub_session={fresh[0]}'}, allow_redirects=False) as response:
            self.assertEqual(response.status, 200)
        self.assertLess(time.monotonic() - started, 4, 'login and session pages do not wait for controls')
        self.assertFalse(any(task.done() for task in running))
        for task in running:
            status, body = await task
            self.assertEqual(status, 200, body)
            self.assertNotEqual(body['reason'], 'control_busy', body)
        status, body = await self.control_call(session, projects[size][1], 'pause')  # free again
        self.assertNotEqual(body['reason'], 'control_busy', body)


class HubLifetimeTests(unittest.TestCase):
    """TI-T06: a backend turned on from the web of a foreground Hub is no Hub descendant and lives
    through ``wb hub stop`` and a Hub ``SIGKILL`` (``/proc`` pid, ppid, session)."""

    def setUp(self):
        self.box = ProjectSandbox(self)
        config = Path(self.box.env['XDG_CONFIG_HOME']) / 'omp-workbench'
        config.mkdir(parents=True, mode=0o700)
        (config / 'hub.toml').write_text('[web]\nenabled = true\nbind = "127.0.0.1"\nport = 0\n')
        done = subprocess.run([sys.executable, '-m', 'workbench', 'hub', 'passwd', '--stdin'], env=self.box.env,
                              cwd=self.box.base, input=PASSWORD + '\n', capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def start_hub(self) -> tuple[subprocess.Popen, int]:
        log = open(self.box.base / 'hub.log', 'w')
        hub = subprocess.Popen([sys.executable, '-m', 'workbench', '_hub', '--foreground'], env=self.box.env,
                               cwd=self.box.base, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
        log.close()
        self.box.processes.append(hub)
        found: list = []
        ready = wait_until(lambda: hub.poll() is not None or bool(found.extend(tcp_listening(hub.pid)) or found), 20)
        self.assertTrue(ready and hub.poll() is None and found, (self.box.base / 'hub.log').read_text())
        return hub, found[0][1]

    def web_power_on(self, port: int, project_id: str) -> dict:
        status, text = self.web_call(port, project_id, 'power-on')
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def web_call(self, port: int, project_id: str, action: str) -> tuple[int | None, str]:
        """Log in over HTTP like a browser, then ``POST /api/projects/<id>/<action>`` with the CSRF
        token; ``(None, error)`` when the connection ends without a reply."""
        host = f'127.0.0.1:{port}'

        def request(method: str, path: str, body: str = '', headers: dict | None = None):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=240)
            try:
                connection.request(method, path, body=body.encode(), headers={'Host': host, **(headers or {})})
                response = connection.getresponse()
                return response.status, response.getheaders(), response.read().decode()
            finally:
                connection.close()

        def cookie(headers, name: str) -> str:
            for key, value in headers:
                if key.lower() == 'set-cookie' and value.startswith(name + '='):
                    return value.split(';', 1)[0].split('=', 1)[1]
            self.fail(f'no {name} cookie in {headers}')

        _status, headers, text = request('GET', '/login')
        login_cookie = cookie(headers, 'wb_hub_login')
        field = re.search(r'name="login_token" value="([^"]+)"', text).group(1)
        form = urllib.parse.urlencode({'login_token': field, 'password': PASSWORD})
        status, headers, _text = request('POST', '/login', form, {
            'Origin': 'http://' + host, 'Cookie': f'wb_hub_login={login_cookie}',
            'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 303)
        session = cookie(headers, 'wb_hub_session')
        _status, _headers, text = request('GET', '/dashboard', headers={'Cookie': f'wb_hub_session={session}'})
        csrf = re.search(r'name="csrf" value="([^"]+)"', text).group(1)
        try:
            status, _headers, text = request('POST', f'/api/projects/{project_id}/{action}', '', {
                'Origin': 'http://' + host, 'Cookie': f'wb_hub_session={session}', 'X-CSRF-Token': csrf})
        except (OSError, http.client.HTTPException) as exc:
            return None, repr(exc)
        return status, text

    def powered_from_the_web(self) -> tuple[subprocess.Popen, Path, int, int, dict]:
        root, project_id = self.box.project('alpha')
        self.box.register(self.box.entry(root, project_id))
        hub, port = self.start_hub()
        body = self.web_power_on(port, project_id)
        self.assertTrue(body['ok'], body)
        pid, ticks, _record = self.box.backend_ref(root)
        self.assertEqual(body['data']['backend'], {'pid': pid, 'start_ticks': ticks})
        parent, sid = parent_and_session(pid)
        self.assertNotEqual(parent, hub.pid)
        self.assertFalse(self.box.descends(pid, hub.pid), 'the backend is no Hub descendant')
        self.assertNotEqual(sid, os.getsid(hub.pid))
        panes = self.box.pane_processes(self.box.status(root))
        self.assertEqual(set(panes), {'manager_omp', 'worker_omp', 'host_shell'})
        for ref in panes.values():
            self.assertFalse(self.box.descends(ref[0], hub.pid), ref)
        return hub, root, pid, ticks, panes

    def assert_survived(self, root: Path, pid: int, ticks: int, panes: dict) -> None:
        time.sleep(1.0)  # anything tied to the Hub would end now
        self.assertTrue(alive(pid, ticks))
        self.assertTrue(all(alive(*ref) for ref in panes.values()), panes)
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['phase'], snapshot['backend']['pid']), ('ready', pid))

    def test_the_backend_lives_after_wb_hub_stop(self):
        hub, root, pid, ticks, panes = self.powered_from_the_web()
        done = self.box.wb('hub', 'stop', timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(hub.wait(timeout=30), 0)
        self.assert_survived(root, pid, ticks, panes)

    def test_wb_hub_stop_does_not_wait_for_a_running_control_and_audits_it_unknown(self):
        from test_project_control import CONTRACT_BACKEND
        root, project_id = self.box.project('slow')
        fixture = subprocess.Popen([sys.executable, str(CONTRACT_BACKEND), str(root), '--snapshot-delay', '60'],
                                   env=self.box.env, cwd=self.box.base, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                   start_new_session=True)
        self.box.processes.append(fixture)
        self.assertEqual(fixture.stdout.readline().strip(), 'ready')
        self.box.register(self.box.entry(root, project_id))
        hub, port = self.start_hub()
        replies: list = []
        caller = threading.Thread(target=lambda: replies.append(self.web_call(port, project_id, 'confirm-boot')))
        caller.start()
        self.addCleanup(caller.join, 30)
        time.sleep(2.0)  # confirm_boot now waits (up to its 10 s request timeout) for the slow snapshot
        self.assertEqual(replies, [])
        started = time.monotonic()
        done = self.box.wb('hub', 'stop', timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(hub.wait(timeout=30), 0)
        self.assertLess(time.monotonic() - started, 5.0, 'the Hub stop does not wait for the control')
        caller.join(30)
        connection = sqlite3.connect(self.box.hub_state / 'hub.sqlite3')
        try:
            rows = [json.loads(detail) for (detail,) in connection.execute(
                "SELECT detail_json FROM audit WHERE event = 'web_control' ORDER BY id")]
        finally:
            connection.close()
        self.assertEqual(rows, [{'project_id': project_id, 'action': 'confirm_boot', 'result': 'unknown'}])

    def test_the_backend_lives_after_the_hub_is_sigkilled(self):
        hub, root, pid, ticks, panes = self.powered_from_the_web()
        hub.send_signal(signal.SIGKILL)
        self.assertEqual(hub.wait(timeout=30), -signal.SIGKILL)
        self.assert_survived(root, pid, ticks, panes)


if __name__ == '__main__':
    unittest.main()
