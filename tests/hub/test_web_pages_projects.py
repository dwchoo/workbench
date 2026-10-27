"""TI-12 status board, project pages and control confirm pages (seam T2): the real ``create_app`` with
``projects.routes`` over a fake Hub list (rows the test chooses), a recording fake ``ProjectControl`` and a real
inbox store.

Expected values are the SPEC/ticket strings (routes, Hub label table, BRIEF texts), the inputs the test chose and
rows the test reads back with stdlib ``sqlite3``. HTML is checked with stdlib ``html.parser``. Run with
``discover -s tests/hub -p 'test_web_pages_projects*.py'``.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from aiohttp import test_utils

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

import test_hub_web_auth as web_auth  # noqa: E402  (module import: its test classes stay uncollected here)
from test_web_pages_requests import Page, make_project  # noqa: E402
from workbench.hub import web_control  # noqa: E402
from workbench.hub.inbox.service import InboxService, Principal, ProjectRef  # noqa: E402
from workbench.hub.web.app import create_app  # noqa: E402
from workbench.hub.web.pages import control as control_pages  # noqa: E402
from workbench.hub.web.pages import projects as project_pages  # noqa: E402
from workbench.hub.web.pages import requests as request_pages  # noqa: E402

OWNER = Principal.owner()
COUNTS = {'new': 0, 'reviewing': 0, 'needs_info': 0, 'ready': 0, 'running': 0}


def hub_row(name: str, project_id: str, **fields) -> dict:
    row = {'root': f'/srv/work/{name}', 'name': name, 'project_id': project_id, 'power': 'on', 'power_detail': None,
           'task': None, 'command': None, 'waits': [], 'pending_requests': dict(COUNTS), 'review_mode': 'auto',
           'last_activity': None, 'backend': None, 'observed_at': 1_000_000.0}
    row.update(fields)
    return row


class FakeControl(web_control.ProjectControl):
    """Records every control call and answers from ``replies[name]`` (a ``Result`` or a list of them)."""

    def __init__(self):
        super().__init__(environment={})
        self.calls: list[tuple] = []
        self.replies: dict = {}

    def _reply(self, name: str, project_id: str, *args):
        self.calls.append((name, project_id, *args))
        reply = self.replies.get(name)
        if isinstance(reply, list):
            reply = reply.pop(0)
        return reply if reply is not None else web_control.Result(True, name, project_id)

    def power_on(self, principal, project_id, *, client_addr=None):
        return self._reply('power_on', project_id)

    def power_off_request(self, principal, project_id, *, session, client_addr=None):
        return self._reply('power_off_request', project_id)

    def power_off_confirm(self, principal, project_id, token, *, session, client_addr=None):
        return self._reply('power_off_confirm', project_id, token)

    def pause(self, principal, project_id, *, client_addr=None):
        return self._reply('pause', project_id)

    def resume(self, principal, project_id, *, client_addr=None):
        return self._reply('resume', project_id)

    def confirm_boot(self, principal, project_id, *, client_addr=None):
        return self._reply('confirm_boot', project_id)


class ProjectsCase(web_auth.WebAppCase):
    insecure_warning = False

    async def asyncSetUp(self):
        self.base_temp = tempfile.TemporaryDirectory(prefix='wb-prj-')
        self.addCleanup(self.base_temp.cleanup)
        base = Path(self.base_temp.name).resolve()
        self.root_a, self.a = make_project(base, 'alpha')
        self.root_b, self.b = make_project(base, 'beta')
        self.refs = [ProjectRef(self.a, self.root_a, 'alpha'), ProjectRef(self.b, self.root_b, 'beta')]
        self.events = []
        self.inbox = InboxService(on_committed=self.events.append, backend_running=lambda project_id: False)
        self.addCleanup(self.inbox.close)
        self.inbox.set_projects(self.refs)
        self.rows = [hub_row('alpha', self.a, power='on'), hub_row('beta', self.b, power='off')]
        self.registry_state = 'ok'
        self.list_error: Exception | None = None
        self.list_calls: list[bool] = []
        self.one_calls: list[str] = []
        self.control = FakeControl()
        self.addAsyncCleanup(self.control.close)
        self.routes = (control_pages.use(self.control), project_pages.routes(self.hub_list, self.inbox, self.hub_one))
        await super().asyncSetUp()
        self.cookie = await self.session_cookie()
        self.cookies = {'wb_hub_session': self.cookie}

    async def hub_list(self, fresh: bool = False) -> dict:
        self.list_calls.append(fresh)
        if self.list_error is not None:
            raise self.list_error
        return {'observed_at': 1_000_000.0, 'registry': {'state': self.registry_state, 'path': '/x/registry.json'},
                'rows': list(self.rows)}

    async def hub_one(self, project_id: str, fresh: bool = True) -> dict:
        self.one_calls.append(project_id)
        listing = await self.hub_list(True)
        self.list_calls.pop()  # the single-project read is counted on its own
        return {**listing, 'rows': [row for row in listing['rows'] if row['project_id'] == project_id]}

    async def page(self, path: str, **kwargs) -> tuple:
        response = await self.get(path, cookies=self.cookies, **kwargs)
        return response, Page(response.body_text)


class BoardObservationTests(ProjectsCase):
    async def test_a_row_whose_observation_failed_is_unknown_never_off(self):
        self.rows = [hub_row('alpha', self.a, power='unknown', power_detail='timeout', pending_requests=None,
                             review_mode=None),
                     hub_row('beta', self.b, power='mismatch', power_detail='root_missing')]
        response, page = await self.page('/')
        rows = page.data_rows()
        self.assertEqual(rows[0][2], '연결 없음/불명')
        self.assertEqual(rows[0][6:], ['—', '—', '—'])  # requests, last activity, review mode: unknown, not 0
        self.assertEqual(rows[1][2], '불일치(root_missing)')
        self.assertNotIn('꺼짐', response.body_text)

    async def test_a_row_without_a_known_power_value_is_unknown_not_off(self):
        self.rows = [{'root': '/srv/work/alpha', 'name': 'alpha', 'project_id': self.a}]
        _, page = await self.page('/')
        self.assertEqual(page.data_rows()[0][2], '연결 없음/불명')

    async def test_a_list_that_cannot_be_read_shows_a_fixed_message_and_no_rows(self):
        self.list_error = RuntimeError('/secret/path/to/hub.sock exploded')
        response, page = await self.page('/')
        self.assertEqual(response.status, 200)
        self.assertEqual(page.data_rows(), [])
        self.assertIn('프로젝트 상태를 읽지 못했습니다', response.body_text)
        self.assertNotIn('/secret', response.body_text)
        self.assertNotIn('꺼짐', response.body_text)

    async def test_an_unreadable_registry_is_not_an_empty_list(self):
        self.registry_state = 'invalid'
        self.rows = []
        response, _ = await self.page('/')
        self.assertIn('등록된 프로젝트 목록을 읽지 못했습니다', response.body_text)
        self.assertNotIn('등록된 프로젝트가 없습니다', response.body_text)
        self.assertNotIn('/x/registry.json', response.body_text)

    async def test_project_text_is_escaped(self):
        self.rows = [hub_row('<b>x</b>', self.a, task={'summary': '<script>alert(1)</script>', 'status': 's'})]
        response, _ = await self.page('/')
        self.assertNotIn('<b>x</b>', response.body_text)
        self.assertNotIn('<script>alert', response.body_text)
        self.assertIn('&lt;b&gt;x&lt;/b&gt;', response.body_text)

    async def test_the_insecure_warning_banner_is_on_the_board(self):
        self.services.insecure_warning = True
        response, _ = await self.page('/')
        self.assertIn('class="banner insecure"', response.body_text)


class BoardTests(ProjectsCase):
    async def test_board_table_uses_the_hub_labels_and_columns_and_links_each_row(self):
        self.rows = [
            hub_row('alpha', self.a, power='on', task={'task_id': 't1', 'summary': '로그인 수정', 'status': 'running'},
                    command={'label': 'build', 'state': 'running'},
                    waits=[{'code': 'manager_editor_not_empty'}],
                    pending_requests={**COUNTS, 'new': 2, 'needs_info': 1}, review_mode='manual',
                    last_activity=1_000_000.0 - 120),
            hub_row('beta', self.b, power='off', pending_requests=dict(COUNTS), review_mode='auto')]
        response, page = await self.page('/')
        self.assertEqual(response.status, 200)
        header = [row for row, data in zip(page.rows, page.row_is_data) if not data][0]
        self.assertEqual(header, ['이름', '경로', '상태', 'Task·단계', '명령', '대기 사유', '요청', '마지막 활동', '검토 방식'])
        rows = page.data_rows()
        self.assertEqual(rows[0], ['alpha', '/srv/work/alpha', '켜짐', '로그인 수정 · running', 'build (실행 중)',
                                   'manager 입력창에 글이 있어 알림 대기', '대기 2 · 질문 1', '2분 전', '수동'])
        self.assertEqual(rows[1], ['beta', '/srv/work/beta', '꺼짐', '—', '—', '—', '0', '—', '자동'])
        self.assertEqual([links[0] for links in page.data_links()], [f'/projects/{self.a}', f'/projects/{self.b}'])

    async def test_board_refreshes_every_ten_seconds_with_the_badge_script_and_no_inline_code(self):
        response, page = await self.page('/')
        metas = [attrs for attrs in page.tags('meta') if attrs.get('http-equiv') == 'refresh']
        self.assertEqual([attrs['content'] for attrs in metas], ['10'])
        self.assertEqual([attrs.get('src') for attrs in page.tags('script')], ['/static/badge.js'])
        self.assertNotIn('style=', response.body_text)
        self.assertNotIn('onclick', response.body_text)
        self.assertIn('id="wb-badge"', response.body_text)


class ProjectPageTests(ProjectsCase):
    def controls(self, page: Page, project_id: str) -> list[str]:
        prefix = f'/projects/{project_id}/'
        return [href[len(prefix):] for href in page.links() if href.startswith(prefix)]

    async def open(self, project_id: str, **row) -> tuple:
        self.rows = [hub_row('alpha', project_id, **row)]
        return await self.page(f'/projects/{project_id}')

    async def test_page_shows_state_task_command_waits_requests_and_review_mode(self):
        self.inbox.review_mode(OWNER, self.a, 'manual')
        response, page = await self.open(
            self.a, power='on', task={'task_id': 't1', 'summary': '로그인 수정', 'status': 'running'},
            command={'label': 'build', 'state': 'running'}, waits=[{'code': 'paused'}],
            pending_requests={**COUNTS, 'new': 2, 'needs_info': 1}, review_mode='manual')
        self.assertEqual(response.status, 200)
        facts = page.sections['status']
        for text in ('alpha', '/srv/work/alpha', '켜짐', '로그인 수정 · running', 'build (실행 중)', '일시정지',
                     '대기 2 · 질문 1', '수동'):
            self.assertIn(text, facts)
        self.assertIn(f'/requests?project={self.a}', page.links())

    async def test_off_offers_only_power_on(self):
        _, page = await self.open(self.a, power='off')
        self.assertEqual(self.controls(page, self.a), ['power-on'])

    async def test_on_offers_power_off_and_pause_or_resume(self):
        _, page = await self.open(self.a, power='on')
        self.assertEqual(self.controls(page, self.a), ['power-off', 'pause'])
        _, page = await self.open(self.a, power='on', waits=[{'code': 'paused'}])
        self.assertEqual(self.controls(page, self.a), ['power-off', 'resume'])

    async def test_boot_confirmation_hold_offers_confirm_boot(self):
        _, page = await self.open(self.a, power='on', waits=[{'code': 'boot_confirmation_required'}])
        self.assertEqual(self.controls(page, self.a), ['power-off', 'confirm-boot'])

    async def test_states_that_cannot_be_controlled_have_no_buttons_and_never_read_as_off(self):
        for power in ('unknown', 'mismatch', 'starting', 'closing'):
            with self.subTest(power=power):
                response, page = await self.open(self.a, power=power)
                self.assertEqual(self.controls(page, self.a), [])
                self.assertNotIn('꺼짐', response.body_text)
        response, _ = await self.open(self.a, power='unknown')
        self.assertIn('연결 없음/불명', response.body_text)

    async def test_a_control_already_running_hides_the_buttons(self):
        self.control._busy.add(self.a)
        response, page = await self.open(self.a, power='off')
        self.assertEqual(self.controls(page, self.a), [])
        self.assertIn('다른 제어가 진행 중입니다', response.body_text)

    async def test_the_project_page_reads_the_cached_list_and_observes_nothing_afresh(self):
        await self.open(self.a, power='on')
        self.assertEqual((self.list_calls, self.one_calls), ([False], []))

    async def test_the_requests_link_quotes_the_project_id(self):
        self.rows = [hub_row('alpha', 'a b&c=d', power='on')]
        _, page = await self.page('/projects/a%20b%26c%3Dd')
        self.assertIn('/requests?project=a%20b%26c%3Dd', page.links())

    async def test_unknown_project_is_404(self):
        response, _ = await self.page('/projects/nope')
        self.assertEqual(response.status, 404)

    async def test_page_needs_a_session(self):
        response = await self.get(f'/projects/{self.a}')
        self.assertEqual((response.status, response.headers['Location']), (303, '/login'))


def confirm_required(project_id: str, token: str = 'tok-1', active=None) -> web_control.Result:
    return web_control.Result(False, 'power_off', project_id, web_control.CONFIRM_REQUIRED, None,
                              {'token': token, 'active': [] if active is None else active, 'expires_in': 120.0,
                               'refusals': []})


SHELL_JOBS = [{'kind': 'shell_jobs', 'foreground_group': 77, 'processes': [
    {'pid': 4242, 'start_ticks': 5, 'command': 'sleep 300', 'cwd': '/home/secret/place'}]},
              {'kind': 'omp_turn', 'role': 'manager', 'idle': False}]


class ConfirmPageTests(ProjectsCase):
    CASES = {'power-on': ('off', '켜기'), 'power-off': ('on', '끄기'), 'pause': ('on', '일시정지'),
             'resume': ('on', '재개'), 'confirm-boot': ('on', '재부팅 확인')}
    WAITS = {'resume': [{'code': 'paused'}], 'confirm-boot': [{'code': 'boot_confirmation_required'}]}

    def set_state(self, action: str, **row):
        power, _label = self.CASES[action]
        self.rows = [hub_row('alpha', self.a, power=power, waits=self.WAITS.get(action, []), **row)]
        self.control.replies['power_off_request'] = confirm_required(self.a)

    async def test_each_control_has_a_confirm_page_with_a_csrf_post_form_and_a_cancel_link(self):
        for action, (_power, label) in self.CASES.items():
            with self.subTest(action=action):
                self.set_state(action)
                self.control.calls.clear()
                response, page = await self.page(f'/projects/{self.a}/{action}')
                self.assertEqual(response.status, 200, response.body_text)
                form = page.form(f'/projects/{self.a}/{action}')
                self.assertIsNotNone(form)
                self.assertEqual(form['attrs']['method'], 'post')
                self.assertTrue(form['fields']['csrf'])
                self.assertIn(label, response.body_text)
                self.assertIn(f'/projects/{self.a}', page.links())  # the cancel link goes back to the project page
                expected = [('power_off_request', self.a)] if action == 'power-off' else []
                self.assertEqual(self.control.calls, expected)  # opening a confirm page runs no control

    async def test_a_confirm_page_observes_only_its_project_afresh(self):
        self.set_state('pause')
        await self.page(f'/projects/{self.a}/pause')
        self.assertEqual((self.list_calls, self.one_calls), ([], [self.a]))

    async def test_power_off_page_lists_the_active_work_and_carries_the_token(self):
        self.set_state('power-off')
        self.control.replies['power_off_request'] = confirm_required(self.a, 'tok-77', SHELL_JOBS)
        response, page = await self.page(f'/projects/{self.a}/power-off')
        form = page.form(f'/projects/{self.a}/power-off')
        self.assertEqual(form['fields']['token'], 'tok-77')
        self.assertIn('4242', page.sections['active'])
        self.assertIn('sleep 300', page.sections['active'])
        self.assertIn('manager', page.sections['active'])
        self.assertNotIn('/home/secret/place', response.body_text)

    async def test_power_off_page_without_active_work_says_so(self):
        self.set_state('power-off')
        response, _ = await self.page(f'/projects/{self.a}/power-off')
        self.assertIn('진행 중인 작업이 없습니다', response.body_text)

    async def test_cancel_changes_nothing(self):
        self.set_state('power-off')
        await self.page(f'/projects/{self.a}/power-off')
        response, page = await self.page(f'/projects/{self.a}')  # what the cancel link opens
        self.assertEqual(response.status, 200)
        self.assertEqual(self.control.calls, [('power_off_request', self.a)])  # nothing was confirmed

    async def test_a_control_that_does_not_fit_the_state_has_no_confirm_form(self):
        self.rows = [hub_row('alpha', self.a, power='on')]
        response, page = await self.page(f'/projects/{self.a}/power-on')
        self.assertEqual(response.status, 409)
        self.assertEqual([f for f in page.forms if f['attrs'].get('action', '').startswith('/projects')], [])
        self.assertEqual(self.control.calls, [])

    async def test_unknown_action_and_project_are_404(self):
        self.rows = [hub_row('alpha', self.a, power='on')]
        self.assertEqual((await self.page(f'/projects/{self.a}/launch-missiles'))[0].status, 404)
        self.assertEqual((await self.page('/projects/nope/power-off'))[0].status, 404)
        self.assertEqual(self.control.calls, [])

    async def test_a_refused_first_step_shows_a_fixed_text_never_the_backend_detail(self):
        self.set_state('power-off')
        self.control.replies['power_off_request'] = web_control.Result(
            False, 'power_off', self.a, 'unreachable', 'ConnectionError: /run/user/1000/wb/secret.sock refused')
        response, page = await self.page(f'/projects/{self.a}/power-off')
        self.assertEqual(response.status, 502)
        self.assertEqual([f for f in page.forms if f['attrs'].get('action') == f'/projects/{self.a}/power-off'], [])
        self.assertNotIn('secret.sock', response.body_text)
        self.assertIn('프로젝트에 연결하지 못했습니다', response.body_text)


class NoPaneInputTests(ProjectsCase):
    """D-09: the web has no way to type into a manager, worker or host pane outside ``/board``."""

    PANE_MARKERS = ('<textarea', 'xterm', '<iframe', '<canvas', 'websocket', '/ws/', 'contenteditable', 'type="text"',
                    'type="search"', 'type="file"')

    async def test_board_project_and_confirm_pages_have_no_terminal_or_free_text_input(self):
        self.control.replies['power_off_request'] = confirm_required(self.a, 'tok', SHELL_JOBS)
        states = [([], ['power-off', 'pause']), ([{'code': 'paused'}], ['resume']),
                  ([{'code': 'boot_confirmation_required'}], ['confirm-boot'])]
        for waits, actions in states:
            self.rows = [hub_row('alpha', self.a, power='on', waits=waits), hub_row('beta', self.b, power='off')]
            paths = ['/', f'/projects/{self.a}', f'/projects/{self.b}', f'/projects/{self.b}/power-on'] + [
                f'/projects/{self.a}/{action}' for action in actions]
            for path in paths:
                with self.subTest(path=path, waits=waits):
                    response, page = await self.page(path)
                    self.assertEqual(response.status, 200, path)
                    lowered = response.body_text.lower()
                    for marker in self.PANE_MARKERS:
                        self.assertNotIn(marker, lowered, path)
                    self.assertEqual([a.get('src') for a in page.tags('script')], ['/static/badge.js'], path)
                    # The global nav's narrow-screen toggle (WBX-A1) is the one non-hidden input allowed.
                    self.assertFalse([a for a in page.tags('input') if a.get('type') not in ('hidden', None)
                                      and a.get('id') != 'wb-nav-toggle'], path)


class StartReviewTests(ProjectsCase):
    """P2-AC-39 (web): the owner's "검토 시작" for a manual project's pending round, and "다시 전달" after unknown."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.request_routes = request_pages.routes(self.inbox, lambda: list(self.refs))
        # the base case builds the app once; rebuild it with both route sets
        await self.server.close()
        self.routes = (control_pages.use(self.control), project_pages.routes(self.hub_list, self.inbox, self.hub_one),
                       self.request_routes)
        self.server = test_utils.TestServer(create_app(self.services, routes=self.routes), host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.port = self.server.port
        self.base = f'http://127.0.0.1:{self.port}'

    def csrf(self) -> str:
        return self.services.sessions.verify(self.cookie, self.now[0]).csrf

    def add(self, project: str | None = None) -> str:
        return self.inbox.submit(OWNER, project or self.a, 'Title', 'Body')['request_id']

    def delivery(self, request_id: str, state: str) -> None:
        connection = sqlite3.connect(self.root_a / '.wb' / 'inbox.sqlite3')
        try:
            connection.execute("UPDATE requests SET delivery_state = ? WHERE request_id = ?", (state, request_id))
            connection.commit()
        finally:
            connection.close()

    def buttons(self, page: Page, request_id: str) -> list[str]:
        return [form['attrs']['action'] for form in page.forms
                if form['attrs'].get('action', '').endswith('/start-review')]

    def started(self) -> list:
        return [event for event in self.events if getattr(event, 'type', None) == 'review_started']

    async def test_a_manual_projects_pending_new_request_has_the_button_and_pressing_notifies(self):
        self.inbox.review_mode(OWNER, self.a, 'manual')
        rid = self.add()
        response, page = await self.page(f'/requests/{rid}')
        self.assertEqual(self.buttons(page, rid), [f'/requests/{rid}/start-review'])
        self.assertIn('검토 시작', page.body_text)
        form = page.form(f'/requests/{rid}/start-review')
        self.assertTrue(form['fields']['csrf'])
        response = await self.post(f'/requests/{rid}/start-review', {'csrf': self.csrf()}, cookies=self.cookies)
        self.assertEqual(response.status, 303, response.body_text)
        self.assertEqual(response.headers['Location'], f'/requests/{rid}?notice=review_started')
        self.assertEqual(len(self.started()), 1, self.events)
        self.assertEqual(self.started()[0].request_id, rid)

    async def test_an_auto_project_has_no_button_and_a_direct_post_is_refused(self):
        rid = self.add()  # review mode defaults to auto
        _, page = await self.page(f'/requests/{rid}')
        self.assertEqual(self.buttons(page, rid), [])
        response = await self.post(f'/requests/{rid}/start-review', {'csrf': self.csrf()}, cookies=self.cookies)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.started(), [])

    async def test_a_round_that_is_not_pending_has_no_button_and_a_direct_post_is_refused(self):
        self.inbox.review_mode(OWNER, self.a, 'manual')
        for state in ('offered', 'delivered', 'none'):
            with self.subTest(delivery=state):
                rid = self.add()
                self.delivery(rid, state)
                _, page = await self.page(f'/requests/{rid}')
                self.assertEqual(self.buttons(page, rid), [])
                response = await self.post(f'/requests/{rid}/start-review', {'csrf': self.csrf()},
                                           cookies=self.cookies)
                self.assertEqual(response.status, 409)
        self.assertEqual(self.started(), [])

    async def test_a_closed_request_has_no_button(self):
        self.inbox.review_mode(OWNER, self.a, 'manual')
        rid = self.add()
        self.inbox.cancel(OWNER, rid)
        _, page = await self.page(f'/requests/{rid}')
        self.assertEqual(self.buttons(page, rid), [])
        response = await self.post(f'/requests/{rid}/start-review', {'csrf': self.csrf()}, cookies=self.cookies)
        self.assertEqual(response.status, 409)

    async def test_after_an_unknown_delivery_the_button_reads_resend(self):
        for mode in ('manual', 'auto'):
            with self.subTest(mode=mode):
                self.inbox.review_mode(OWNER, self.a, mode)
                rid = self.add()
                self.delivery(rid, 'unknown')
                _, page = await self.page(f'/requests/{rid}')
                self.assertEqual(self.buttons(page, rid), [f'/requests/{rid}/start-review'])
                self.assertIn('다시 전달', page.body_text)
                response = await self.post(f'/requests/{rid}/start-review', {'csrf': self.csrf()},
                                           cookies=self.cookies)
                self.assertEqual(response.status, 303)

    async def test_the_post_needs_the_csrf_field(self):
        self.inbox.review_mode(OWNER, self.a, 'manual')
        rid = self.add()
        response = await self.post(f'/requests/{rid}/start-review', {}, cookies=self.cookies)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.started(), [])


class ReviewModeTests(ProjectsCase):
    def stored(self, root: Path | None = None):
        db = (root or self.root_a) / '.wb' / 'inbox.sqlite3'
        if not db.exists():
            return None
        connection = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        try:
            row = connection.execute("SELECT value FROM meta WHERE key = 'review_mode'").fetchone()
        finally:
            connection.close()
        return row and row[0]

    def csrf(self) -> str:
        return self.services.sessions.verify(self.cookie, self.now[0]).csrf

    async def test_project_page_has_a_review_mode_form_with_the_current_choice(self):
        self.inbox.review_mode(OWNER, self.a, 'manual')
        _, page = await self.page(f'/projects/{self.a}')
        form = page.form(f'/projects/{self.a}/review-mode')
        self.assertEqual(form['attrs']['method'], 'post')
        self.assertTrue(form['fields']['csrf'])
        self.assertEqual({a['value'] for a in page.tags('option')}, {'auto', 'manual'})
        self.assertEqual([a['value'] for a in page.tags('option') if 'selected' in a], ['manual'])

    async def test_changing_the_review_mode_is_stored_and_shown(self):
        response = await self.post(f'/projects/{self.a}/review-mode', {'csrf': self.csrf(), 'mode': 'manual'},
                                   cookies=self.cookies)
        self.assertEqual(response.status, 303, response.body_text)
        self.assertEqual(response.headers['Location'], f'/projects/{self.a}?notice=review_mode')
        self.assertEqual(self.stored(), 'manual')
        shown, page = await self.page(response.headers['Location'])
        self.assertIn('검토 방식을 바꿨습니다', shown.body_text)
        self.assertIn('수동', page.sections['status'])
        self.assertEqual([a['value'] for a in page.tags('option') if 'selected' in a], ['manual'])
        await self.post(f'/projects/{self.a}/review-mode', {'csrf': self.csrf(), 'mode': 'auto'}, cookies=self.cookies)
        self.assertEqual(self.stored(), 'auto')

    async def test_an_unknown_mode_is_refused_and_changes_nothing(self):
        response = await self.post(f'/projects/{self.a}/review-mode', {'csrf': self.csrf(), 'mode': 'yolo'},
                                   cookies=self.cookies)
        self.assertEqual(response.status, 400)
        self.assertNotEqual(self.stored(), 'yolo')

    async def test_csrf_and_project_are_required(self):
        response = await self.post(f'/projects/{self.a}/review-mode', {'mode': 'manual'}, cookies=self.cookies)
        self.assertEqual(response.status, 403)
        self.assertIsNone(self.stored())
        response = await self.post('/projects/nope/review-mode', {'csrf': self.csrf(), 'mode': 'manual'},
                                   cookies=self.cookies)
        self.assertEqual(response.status, 404)

    async def test_the_review_mode_post_is_not_a_control_action(self):
        await self.post(f'/projects/{self.a}/review-mode', {'csrf': self.csrf(), 'mode': 'manual'}, cookies=self.cookies)
        self.assertEqual(self.control.calls, [])


class ControlPostTests(ProjectsCase):
    async def csrf(self) -> str:
        return self.services.sessions.verify(self.cookie, self.now[0]).csrf

    async def submit(self, action: str, data: dict | None = None, *, with_csrf: bool = True):
        data = {**({'csrf': await self.csrf()} if with_csrf else {}), **(data or {})}
        return await self.post(f'/projects/{self.a}/{action}', data, cookies=self.cookies)

    async def test_each_simple_control_posts_once_then_redirects_to_the_project_with_a_notice(self):
        cases = {'power-on': ('off', 'power_on', 'power_on', '프로젝트를 켰습니다'),
                 'pause': ('on', 'pause', 'pause', '일시정지했습니다'),
                 'resume': ('on', 'resume', 'resume', '재개했습니다'),
                 'confirm-boot': ('on', 'confirm_boot', 'confirm_boot', '재부팅을 확인했습니다')}
        for action, (power, call, notice, text) in cases.items():
            with self.subTest(action=action):
                self.rows = [hub_row('alpha', self.a, power=power)]
                self.control.calls.clear()
                response = await self.submit(action)
                self.assertEqual(response.status, 303, response.body_text)
                self.assertEqual(response.headers['Location'], f'/projects/{self.a}?notice={notice}')
                self.assertEqual(self.control.calls, [(call, self.a)])
                shown, _ = await self.page(response.headers['Location'])
                self.assertIn(text, shown.body_text)

    async def test_power_on_of_a_running_backend_says_it_was_already_on(self):
        self.control.replies['power_on'] = web_control.Result(True, 'power_on', self.a, data={'already_running': True})
        response = await self.submit('power-on')
        self.assertEqual(response.headers['Location'], f'/projects/{self.a}?notice=power_on_already')

    async def test_a_post_without_the_csrf_field_is_refused_and_runs_nothing(self):
        response = await self.submit('power-on', with_csrf=False)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.control.calls, [])

    async def test_a_failed_control_shows_a_fixed_text_and_no_backend_detail(self):
        detail = 'driver exit 1: Traceback /home/secret/.wb/backend.sock PermissionError'
        for reason, text in (('start_failed', '프로젝트를 켜지 못했습니다'), ('start_requirement', '시작에 필요한 조건'),
                             ('weird_unlisted_reason', '처리하지 못했습니다')):
            with self.subTest(reason=reason):
                self.control.replies['power_on'] = web_control.Result(False, 'power_on', self.a, reason, detail,
                                                                       {'output': '/home/secret/out'})
                response = await self.submit('power-on')
                self.assertEqual(response.status, 502)
                self.assertIn(text, response.body_text)
                self.assertNotIn('secret', response.body_text)
                self.assertNotIn('Traceback', response.body_text)
                self.assertNotIn('weird_unlisted_reason', response.body_text)
                self.assertNotIn('프로젝트를 켰습니다', response.body_text)

    async def test_busy_and_in_progress_and_unknown_project_have_their_statuses(self):
        for reason, status in (('control_busy', 503), ('control_in_progress', 409), ('unknown_project', 404),
                               ('forbidden', 403)):
            with self.subTest(reason=reason):
                self.control.replies['pause'] = web_control.Result(False, 'pause', self.a, reason, 'x')
                response = await self.submit('pause')
                self.assertEqual(response.status, status)

    async def test_the_backend_refusal_of_a_boot_confirm_is_a_fixed_text(self):
        self.control.replies['confirm_boot'] = web_control.Result(False, 'confirm_boot', self.a,
                                                                   'boot_id_mismatch', 'old=1 new=2 /x')
        response = await self.submit('confirm-boot')
        self.assertIn('재부팅 정보가 바뀌었습니다', response.body_text)
        self.assertNotIn('old=1', response.body_text)
        self.assertEqual(response.status, 409)

    async def test_failed_controls_use_409_for_conflicts_503_for_unavailable_and_502_for_backend_failures(self):
        expected = {'confirm_token_invalid': 409, 'control_in_progress': 409, 'not_running': 409, 'mismatch': 409,
                    'boot_confirmation_not_required': 409, 'boot_id_mismatch': 409, 'work_kept_changing': 409,
                    'control_busy': 503, 'registry_invalid': 503, 'boot_id_unavailable': 503,
                    'unreachable': 502, 'start_failed': 502, 'start_requirement': 502, 'no_result': 502,
                    'unverified': 502, 'backend_too_old': 502, 'a_reason_nobody_listed': 502,
                    'unknown_project': 404, 'forbidden': 403, 'internal_error': 500}
        for reason, status in expected.items():
            with self.subTest(reason=reason):
                # The sentinel is not "detail": the global nav's settings menu is a <details> element (WBX-A1).
                self.control.replies['pause'] = web_control.Result(False, 'pause', self.a, reason, 'LEAKED-DETAIL')
                response = await self.submit('pause')
                self.assertEqual(response.status, status)
                self.assertNotIn('LEAKED-DETAIL', response.body_text)

    async def test_power_off_confirm_runs_with_the_page_token_and_succeeds_only_when_verified(self):
        self.control.replies['power_off_confirm'] = web_control.Result(
            True, 'power_off', self.a, data={'result': {'verified': True}})
        response = await self.submit('power-off', {'token': 'tok-9'})
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers['Location'], f'/projects/{self.a}?notice=power_off')
        self.assertEqual(self.control.calls, [('power_off_confirm', self.a, 'tok-9')])

    async def test_a_power_off_that_is_not_verified_never_reads_as_success(self):
        cases = [(web_control.Result(False, 'power_off', self.a, 'no_result', 'lost /run/x'), '종료 결과를 확인하지 못했습니다'),
                 (web_control.Result(False, 'power_off', self.a, 'unverified', 'problems: /p'), '종료가 확인되지 않았습니다'),
                 (web_control.Result(True, 'power_off', self.a, data={'result': {'verified': False}}),
                  '종료가 확인되지 않았습니다'),
                 (web_control.Result(True, 'power_off', self.a), '종료가 확인되지 않았습니다')]
        for result, text in cases:
            with self.subTest(reason=result.reason, ok=result.ok):
                self.control.replies['power_off_confirm'] = result
                response = await self.submit('power-off', {'token': 'tok-9'})
                self.assertEqual(response.status, 502)
                self.assertIn(text, response.body_text)
                self.assertNotIn('꺼졌습니다', response.body_text)
                self.assertNotIn('/run/x', response.body_text)

    async def test_a_used_or_foreign_token_is_refused_with_a_way_back_to_the_confirm_page(self):
        self.control.replies['power_off_confirm'] = web_control.Result(
            False, 'power_off', self.a, 'confirm_token_invalid', 'unknown')
        response = await self.submit('power-off', {'token': 'tok-old'})
        self.assertEqual(response.status, 409)
        self.assertIn(f'/projects/{self.a}/power-off', Page(response.body_text).links())

    async def test_work_that_changed_shows_a_new_confirm_page_with_the_new_list_and_token(self):
        self.control.replies['power_off_confirm'] = web_control.Result(
            False, 'power_off', self.a, 'work_changed', '작업이 바뀌었습니다 (the work changed: /home/secret). 새 목록을 다시 확인하세요.',
            {'token': 'tok-new', 'active': SHELL_JOBS, 'expires_in': 120.0, 'refusals': ['x']})
        response = await self.submit('power-off', {'token': 'tok-old'})
        page = Page(response.body_text)
        self.assertEqual(response.status, 200)
        self.assertIn('작업이 바뀌었습니다', response.body_text)
        form = page.form(f'/projects/{self.a}/power-off')
        self.assertEqual(form['fields']['token'], 'tok-new')
        self.assertNotIn('tok-old', response.body_text)
        self.assertIn('4242', page.sections['active'])
        self.assertNotIn('/home/secret', response.body_text)
        self.assertEqual(self.control.calls, [('power_off_confirm', self.a, 'tok-old')])  # nothing else ran

    async def test_power_off_without_a_token_runs_nothing(self):
        response = await self.submit('power-off')
        self.assertEqual(response.status, 409)
        self.assertEqual(self.control.calls, [])

    async def test_unknown_action_post_is_404(self):
        response = await self.submit('reboot-host')
        self.assertEqual(response.status, 404)
        self.assertEqual(self.control.calls, [])


if __name__ == '__main__':
    unittest.main()
