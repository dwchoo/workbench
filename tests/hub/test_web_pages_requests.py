"""TI-06 request pages (seam T2): the real ``create_app`` with ``requests.routes`` over a real inbox store.

Expected values are the SPEC strings (routes, labels, status codes, input limits), the inputs the
test chose and rows the test reads back with stdlib ``sqlite3``. HTML is checked with stdlib
``html.parser``. Run with ``discover -s tests/hub -p 'test_web_pages*.py'``.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

import test_hub_web_auth as web_auth  # noqa: E402  (module import: its test classes stay uncollected here)
from workbench.hub.inbox.service import InboxError, InboxService, Principal, ProjectRef  # noqa: E402
from workbench.hub.web.pages import requests as request_pages  # noqa: E402

OWNER = Principal.owner()


def make_project(base: Path, name: str) -> tuple[Path, str]:
    root = base / name
    root.mkdir(parents=True)
    root = root.resolve()
    data = root / '.wb'
    data.mkdir(mode=0o700)
    os.chmod(data, 0o700)
    project_id = str(uuid.uuid4())
    descriptor = os.open(data / 'project.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump({'schema_version': 1, 'project_id': project_id, 'project_root': str(root), 'state': 'completed'},
                  stream)
    return root, project_id


VOID = ('input', 'meta', 'link', 'br', 'hr', 'img')


class Page(HTMLParser):
    """Minimal DOM facts: every element (tag, attrs), text nodes, forms with fields, table rows."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict]] = []
        self.text: list[str] = []
        self.pre: list[str] = []
        self.forms: list[dict] = []
        self.rows: list[list[str]] = []
        self.row_links: list[list[str]] = []
        self.row_is_data: list[bool] = []
        self._has_td = False
        self._form: dict | None = None
        self._pre_open = 0
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self._row_links: list[str] | None = None
        self.sections: dict[str, str] = {}
        self.item_classes: list[str] = []
        self._open_ids: list[tuple[str, str]] = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = {key: value for key, value in attrs}
        self.elements.append((tag, attrs))
        if tag == 'li':
            self.item_classes.append(attrs.get('class', ''))
        if attrs.get('id') and tag not in VOID:
            self._open_ids.append((tag, attrs['id']))
            self.sections.setdefault(attrs['id'], '')
        if tag == 'form':
            self._form = {'attrs': attrs, 'fields': {}, 'buttons': [], 'selects': {}}
            self.forms.append(self._form)
        elif tag in ('input', 'textarea', 'select') and self._form is not None and attrs.get('name'):
            self._form['fields'][attrs['name']] = attrs.get('value')
        elif tag == 'textarea' and self._form is not None:
            pass
        if tag == 'pre':
            self._pre_open += 1
            self.pre.append('')
        if tag == 'tr':
            self._row, self._row_links, self._has_td = [], [], False
        if tag in ('td', 'th') and self._row is not None:
            self._cell = []
            self._has_td = self._has_td or tag == 'td'
        if tag == 'a' and self._row_links is not None and 'href' in attrs:
            self._row_links.append(attrs['href'])

    def handle_endtag(self, tag):
        if self._open_ids and self._open_ids[-1][0] == tag:
            self._open_ids.pop()
        if tag == 'form':
            self._form = None
        if tag == 'pre':
            self._pre_open -= 1
        if tag in ('td', 'th') and self._cell is not None and self._row is not None:
            self._row.append(''.join(self._cell))
            self._cell = None
        if tag == 'tr' and self._row is not None:
            self.rows.append(self._row)
            self.row_links.append(self._row_links or [])
            self.row_is_data.append(self._has_td)
            self._row = self._row_links = None

    def handle_data(self, data):
        self.text.append(data)
        for _, ident in self._open_ids:
            self.sections[ident] += data
        if self._pre_open:
            self.pre[-1] += data
        if self._cell is not None:
            self._cell.append(data)

    def tags(self, name: str) -> list[dict]:
        return [attrs for tag, attrs in self.elements if tag == name]

    @property
    def body_text(self) -> str:
        return ''.join(self.text)

    def form(self, action: str) -> dict | None:
        return next((f for f in self.forms if f['attrs'].get('action') == action), None)

    def links(self) -> list[str]:
        return [attrs['href'] for attrs in self.tags('a') if 'href' in attrs]

    def data_rows(self) -> list[list[str]]:
        return [row for row, data in zip(self.rows, self.row_is_data) if data]

    def data_links(self) -> list[list[str]]:
        return [links for links, data in zip(self.row_links, self.row_is_data) if data]


class RequestsCase(web_auth.WebAppCase):
    async def asyncSetUp(self):
        self.base_temp = tempfile.TemporaryDirectory(prefix='wb-req-')
        self.addCleanup(self.base_temp.cleanup)
        base = Path(self.base_temp.name).resolve()
        self.root_a, self.a = make_project(base, 'alpha')
        self.root_b, self.b = make_project(base, 'beta')
        self.refs = [ProjectRef(self.a, self.root_a, 'alpha'), ProjectRef(self.b, self.root_b, 'beta')]
        self.events = []
        self.backend_up = False
        self.inbox = InboxService(on_committed=self.events.append, backend_running=lambda project_id: self.backend_up)
        self.addCleanup(self.inbox.close)
        self.inbox.set_projects(self.refs)
        self.routes = (request_pages.routes(self.inbox, lambda: list(self.refs)),)
        await super().asyncSetUp()
        self.cookie = await self.session_cookie()
        self.cookies = {'wb_hub_session': self.cookie}

    async def page(self, path: str, **kwargs) -> tuple:
        response = await self.get(path, cookies=self.cookies, **kwargs)
        return response, Page(response.body_text)

    async def csrf(self) -> str:
        _, page = await self.page('/requests/new')
        form = page.form('/requests')
        self.assertIsNotNone(form, 'create form is rendered')
        return form['fields']['csrf']

    async def submit(self, path: str, data: dict | None = None, **kwargs):
        data = {'csrf': await self.csrf(), **(data or {})}
        return await self.post(path, data, cookies=self.cookies, **kwargs)

    def add(self, title='Title', body='Body', project=None, principal=None) -> str:
        return self.inbox.submit(principal or OWNER, project or self.a, title, body)['request_id']

    def link(self, request_id: str, status: str = 'dispatched', task_id: str = 'task-9') -> None:
        manager = Principal.manager(self.a)
        token = self.inbox.link_check(manager, request_id)['link_token']
        self.inbox.link_commit(manager, token, task_id, 2, status)

    def db_rows(self, sql: str, *args, root: Path | None = None):
        db = (root or self.root_a) / '.wb' / 'inbox.sqlite3'
        connection = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()


class ListTests(RequestsCase):
    async def test_default_list_hides_closed_and_shows_seq_project_title_state_origin(self):
        open_id = self.add('Open one')
        closed_id = self.add('Closed one')
        self.inbox.cancel(OWNER, closed_id)
        response, page = await self.page('/requests')
        self.assertEqual(response.status, 200)
        rows = page.data_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][0], '#1')
        self.assertEqual(rows[0][1], 'alpha')
        self.assertEqual(rows[0][2], 'Open one')
        self.assertEqual(rows[0][3], '신규')
        self.assertEqual(rows[0][4], '소유자')
        self.assertIn(f'/requests/{open_id}', page.data_links()[0])
        self.assertNotIn(closed_id, response.body_text)

    async def test_unauthenticated_list_redirects_to_login(self):
        response = await self.get('/requests')
        self.assertEqual((response.status, response.headers['Location']), (303, '/login'))

    async def test_project_state_archive_and_search_filters(self):
        a1 = self.add('Alpha login bug', 'cannot sign in')
        self.add('Beta layout', 'misaligned header', project=self.b)
        a3 = self.add('Alpha closed thing', 'old')
        self.inbox.cancel(OWNER, a3)
        a4 = self.add('Alpha needs answer', 'unclear')
        self.inbox.set_state(OWNER, a4, 1, 'NEEDS_INFO')

        def titles(page):
            return sorted(row[2] for row in page.data_rows())
        _, page = await self.page(f'/requests?project={self.b}')
        self.assertEqual(titles(page), ['Beta layout'])
        _, page = await self.page('/requests?state=NEEDS_INFO')
        self.assertEqual(titles(page), ['Alpha needs answer'])
        _, page = await self.page(f'/requests?project={self.a}&archive=1')
        self.assertEqual(titles(page), ['Alpha closed thing', 'Alpha login bug', 'Alpha needs answer'])
        _, page = await self.page('/requests?state=CLOSED')
        self.assertEqual(titles(page), ['Alpha closed thing'])
        _, page = await self.page('/requests?q=SIGN')
        self.assertEqual(titles(page), ['Alpha login bug'])
        self.assertIn(a1, ''.join(page.data_links()[0]))

    async def test_filter_form_keeps_choices_and_has_no_attachment_input(self):
        _, page = await self.page(f'/requests?project={self.b}&state=NEW&archive=1&q=zz')
        form = page.form('/requests')
        self.assertIsNotNone(form)
        self.assertEqual(form['attrs']['method'], 'get')
        self.assertEqual(set(form['fields']), {'project', 'state', 'archive', 'q'})
        self.assertFalse([a for a in page.tags('input') if a.get('type') == 'file'])
        selected = [a.get('value') for a in page.tags('option') if 'selected' in a]
        self.assertEqual(set(selected), {self.b, 'NEW'})
        self.assertIn('checked', [k for a in page.tags('input') if a.get('name') == 'archive' for k in a])
        self.assertEqual([a['value'] for a in page.tags('input') if a.get('name') == 'q'], ['zz'])

    async def test_pages_of_fifty_newest_first(self):
        for number in range(1, 53):
            self.add(f'item {number:02d}')
        _, first = await self.page('/requests')
        self.assertEqual(len(first.data_rows()), 50)
        self.assertEqual(first.data_rows()[0][0], '#52')
        self.assertEqual(first.data_rows()[-1][0], '#3')
        self.assertIn('/requests?page=2', first.links())
        _, second = await self.page('/requests?page=2')
        self.assertEqual([row[0] for row in second.data_rows()], ['#2', '#1'])
        self.assertIn('/requests', second.links())
        self.assertNotIn('/requests?page=3', second.links())

    async def test_unknown_state_filter_is_a_visible_error_and_lists_nothing(self):
        self.add('hidden by error')
        response, page = await self.page('/requests?state=BOGUS')
        self.assertEqual(response.status, 400)
        self.assertIn('입력이 올바르지 않습니다', page.body_text)
        self.assertEqual(page.data_rows(), [])

    async def test_list_shows_origin_labels_for_board_agent_and_named_key(self):
        self.add('from board', principal=Principal.board_agent('k0board000'))
        self.add('from key', principal=Principal.editor('k0editor00', 'ci-bot', None))
        _, page = await self.page('/requests')
        origins = {row[2]: row[4] for row in page.data_rows()}
        self.assertEqual(origins, {'from board': 'Board agent', 'from key': 'key:ci-bot'})


class CreateTests(RequestsCase):
    async def new_form(self, path='/requests/new'):
        response, page = await self.page(path)
        self.assertEqual(response.status, 200)
        form = page.form('/requests')
        self.assertIsNotNone(form)
        return page, form

    def count(self, root=None) -> int:
        try:
            return self.db_rows('SELECT count(*) FROM requests', root=root)[0][0]
        except sqlite3.OperationalError:
            return 0

    async def test_new_form_has_project_title_body_nonce_csrf_and_no_attachment(self):
        page, form = await self.new_form()
        self.assertEqual(form['attrs']['method'], 'post')
        self.assertEqual(set(form['fields']), {'csrf', 'project', 'title', 'body', 'nonce'})
        self.assertTrue(form['fields']['nonce'])
        self.assertEqual(sorted(a['value'] for a in page.tags('option')), sorted([self.a, self.b]))
        self.assertFalse([a for a in page.tags('input') if a.get('type') == 'file'])
        self.assertNotIn('multipart', form['attrs'].get('enctype', ''))

    async def test_new_form_preselects_the_project_from_the_query(self):
        page, _ = await self.new_form(f'/requests/new?project={self.b}')
        self.assertEqual([a['value'] for a in page.tags('option') if 'selected' in a], [self.b])

    async def test_two_loads_get_different_nonces(self):
        _, first = await self.new_form()
        _, second = await self.new_form()
        self.assertNotEqual(first['fields']['nonce'], second['fields']['nonce'])

    async def test_create_stores_an_owner_new_request_and_redirects_to_its_detail(self):
        _, form = await self.new_form()
        response = await self.post('/requests', {'csrf': form['fields']['csrf'], 'project': self.b,
                                                 'title': 'Add dark mode', 'body': 'line1\r\nline2',
                                                 'nonce': form['fields']['nonce']}, cookies=self.cookies)
        self.assertEqual(response.status, 303)
        rows = self.db_rows('SELECT request_id, title, body, state, origin_kind, version FROM requests',
                            root=self.root_b)
        self.assertEqual(len(rows), 1)
        request_id, title, body, state, origin_kind, version = rows[0]
        self.assertEqual((title, body, state, origin_kind, version), ('Add dark mode', 'line1\nline2', 'NEW', 'owner', 1))
        self.assertEqual(response.headers['Location'], f'/requests/{request_id}')
        self.assertEqual(self.count(), 0)

    async def test_resubmitting_the_same_form_creates_one_request(self):
        _, form = await self.new_form()
        data = {'csrf': form['fields']['csrf'], 'project': self.a, 'title': 'Once', 'body': 'text',
                'nonce': form['fields']['nonce']}
        first = await self.post('/requests', data, cookies=self.cookies)
        second = await self.post('/requests', data, cookies=self.cookies)
        self.assertEqual((first.status, second.status), (303, 303))
        self.assertEqual(first.headers['Location'], second.headers['Location'])
        self.assertEqual(self.count(), 1)

    async def test_rejected_form_is_shown_again_with_reason_and_input_and_stores_nothing(self):
        cases = {
            'title too long': ({'title': 'x' * 201, 'body': 'b'}, '입력이 한도를 넘었습니다'),
            'empty title': ({'title': '', 'body': 'b'}, '입력이 올바르지 않습니다'),
            'empty body': ({'title': 'T', 'body': ''}, '입력이 올바르지 않습니다'),
            'body too large': ({'title': 'T', 'body': 'y' * (64 * 1024 + 1)}, '입력이 한도를 넘었습니다'),
            'control char': ({'title': 'a\x01b', 'body': 'b'}, '입력이 올바르지 않습니다'),
        }
        for name, (fields, reason) in cases.items():
            with self.subTest(name):
                _, form = await self.new_form()
                response = await self.post('/requests', {'csrf': form['fields']['csrf'], 'project': self.a,
                                                         'nonce': form['fields']['nonce'], **fields},
                                           cookies=self.cookies)
                page = Page(response.body_text)
                self.assertEqual(response.status, 400)
                self.assertIn(reason, page.body_text)
                again = page.form('/requests')
                self.assertIsNotNone(again, 'the same form is shown again')
                self.assertEqual(again['fields']['title'], fields['title'] if '\x01' not in fields['title'] else 'a\x01b')
                self.assertEqual(again['fields']['nonce'], form['fields']['nonce'])
                self.assertEqual(self.count(), 0)
                self.assertEqual(self.count(self.root_b), 0)

    async def test_same_nonce_with_different_text_is_refused_with_a_fresh_form(self):
        _, form = await self.new_form()
        base = {'csrf': form['fields']['csrf'], 'project': self.a, 'nonce': form['fields']['nonce']}
        await self.post('/requests', {**base, 'title': 'one', 'body': 'b'}, cookies=self.cookies)
        response = await self.post('/requests', {**base, 'title': 'two', 'body': 'b'}, cookies=self.cookies)
        page = Page(response.body_text)
        self.assertEqual(response.status, 409)
        self.assertIn('이미 제출된 양식입니다', page.body_text)
        self.assertNotEqual(page.form('/requests')['fields']['nonce'], base['nonce'])
        self.assertEqual(page.form('/requests')['fields']['title'], 'two')
        self.assertEqual(self.count(), 1)

    async def test_unknown_project_is_refused_and_missing_csrf_is_forbidden(self):
        _, form = await self.new_form()
        response = await self.post('/requests', {'csrf': form['fields']['csrf'], 'project': str(uuid.uuid4()),
                                                 'title': 'T', 'body': 'b', 'nonce': form['fields']['nonce']},
                                   cookies=self.cookies)
        self.assertEqual(response.status, 404)
        self.assertIsNotNone(Page(response.body_text).form('/requests'))
        response = await self.post('/requests', {'project': self.a, 'title': 'T', 'body': 'b', 'nonce': 'n1'},
                                   cookies=self.cookies)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.count(), 0)


class DetailTests(RequestsCase):
    async def detail(self, request_id):
        response, page = await self.page(f'/requests/{request_id}')
        self.assertEqual(response.status, 200)
        return page

    async def test_header_facts_origin_state_revision_and_body_in_pre(self):
        request_id = self.add('Fix the parser', 'first line\nsecond line')
        page = await self.detail(request_id)
        self.assertIn('#1', page.sections['summary'])
        self.assertIn('Fix the parser', page.sections['summary'])
        for fact in ('alpha', '소유자', '신규', 'r1'):
            self.assertIn(fact, page.sections['summary'], fact)
        self.assertIn('first line\nsecond line', page.pre)
        self.assertTrue(page.sections['task'].endswith('—'), page.sections['task'])
        self.assertTrue(page.sections['result'].endswith('—'), page.sections['result'])

    async def test_origin_labels_in_detail(self):
        for principal, label in ((Principal.board_agent('k0board000'), 'Board agent'),
                                 (Principal.editor('k0editor00', 'ci-bot', None), 'key:ci-bot')):
            page = await self.detail(self.add('t', principal=principal))
            self.assertIn(label, page.sections['summary'])

    async def test_comment_thread_distinguishes_kinds_in_order(self):
        manager = Principal.manager(self.a)
        request_id = self.add('Thread', 'body')
        self.inbox.review(manager, request_id, 'ask', body='which file?')
        self.inbox.comment(OWNER, request_id, 'the parser')
        self.inbox.link_check(manager, request_id)
        self.inbox.review(manager, request_id, 'comment', body='working on it')
        self.inbox.review(manager, request_id, 'reject', body='out of scope')
        page = await self.detail(request_id)
        self.assertEqual(page.item_classes, ['comment kind-question', 'comment kind-answer',
                                              'comment kind-progress', 'comment kind-decision'])
        thread = page.sections['thread']
        order = [thread.index(label) for label in ('질문', '답변', '진행', '결정')]
        self.assertEqual(order, sorted(order))
        self.assertEqual(page.pre[1:5], ['which file?', 'the parser', 'working on it', 'out of scope'])
        self.assertIn('manager', thread)
        self.assertIn('소유자', thread)

    async def test_revision_history_after_an_edit_of_a_running_request(self):
        request_id = self.add('Original title', 'original body')
        self.link(request_id, 'running')
        self.inbox.update(OWNER, request_id, 3, title='Revised title', body='revised body')
        page = await self.detail(request_id)
        history = page.sections['revisions']
        self.assertIn('r1', history)
        self.assertIn('r2', history)
        self.assertIn('Original title', history)
        self.assertIn('Revised title', history)
        self.assertIn('original body', page.pre)
        self.assertIn('revised body', page.pre)
        self.assertIn('r2', page.sections['summary'])

    async def test_linked_task_is_shown_when_present(self):
        request_id = self.add('Run it')
        self.link(request_id, 'dispatched', task_id='task-77')
        page = await self.detail(request_id)
        for fact in ('task-77', 'dispatched', '명세 revision 2'):
            self.assertIn(fact, page.sections['task'])
        self.assertIn('실행 준비', page.sections['summary'])

    async def test_closed_request_shows_outcome_and_result_summary(self):
        manager = Principal.manager(self.a)
        request_id = self.add('Done')
        self.link(request_id, 'running')
        self.inbox.review(manager, request_id, 'close', outcome='success', summary='all green <b>ok</b>')
        page = await self.detail(request_id)
        self.assertIn('닫힘', page.sections['summary'])
        self.assertIn('성공', page.sections['summary'])
        self.assertIn('all green <b>ok</b>', page.pre)
        self.assertEqual(page.tags('b'), [])

    async def test_unknown_or_malformed_ids_are_the_same_not_found_page(self):
        for ident in (str(uuid.uuid4()), 'not-a-uuid', 'x' * 300):
            response, page = await self.page(f'/requests/{ident}')
            self.assertEqual(response.status, 404, ident)
            self.assertIn('찾을 수 없습니다', page.body_text)

    ACTIONS = {  # state -> (edit link, cancel link, state-form target or None)
        'NEW': (True, True, 'NEEDS_INFO'), 'NEEDS_INFO': (True, True, 'NEW'), 'REVIEWING': (True, True, None),
        'READY': (True, True, None), 'RUNNING': (True, True, None), 'CLOSED': (False, False, 'NEW')}

    def make_in(self, state):
        manager = Principal.manager(self.a)
        request_id = self.add(f'in {state}')
        if state == 'NEEDS_INFO':
            self.inbox.set_state(OWNER, request_id, 1, 'NEEDS_INFO')
        elif state == 'REVIEWING':
            self.inbox.review(manager, request_id, 'begin_review')
        elif state in ('READY', 'RUNNING'):
            self.link(request_id, 'dispatched' if state == 'READY' else 'running')
        elif state == 'CLOSED':
            self.inbox.cancel(OWNER, request_id)
        return request_id

    async def test_buttons_follow_the_state(self):
        for state, (edit, cancel, target) in self.ACTIONS.items():
            with self.subTest(state):
                request_id = self.make_in(state)
                page = await self.detail(request_id)
                links = page.links()
                self.assertEqual(f'/requests/{request_id}/edit' in links, edit)
                self.assertEqual(f'/requests/{request_id}/cancel' in links, cancel)
                form = page.form(f'/requests/{request_id}/state')
                if target is None:
                    self.assertIsNone(form)
                else:
                    self.assertEqual(form['fields']['to'], target)
                    self.assertTrue(form['fields']['base_version'].isdigit())
                    self.assertTrue(form['fields']['csrf'])
                comment = page.form(f'/requests/{request_id}/comments')
                self.assertIsNotNone(comment)
                self.assertEqual(set(comment['fields']), {'csrf', 'body'})
                self.assertNotIn(f'/requests/{request_id}/start-review', page.body_text)

    async def test_cancel_already_requested_hides_the_cancel_button(self):
        request_id = self.add('Running')
        self.link(request_id, 'running')
        self.backend_up = True
        self.inbox.cancel(OWNER, request_id)
        page = await self.detail(request_id)
        self.assertNotIn(f'/requests/{request_id}/cancel', page.links())
        self.assertIn('취소 요청됨', page.sections['summary'])

    def test_allowed_actions_follow_the_principal_permission_table(self):
        detail = {'state': 'NEW', 'cancel_state': None}
        owner = request_pages.allowed_actions(OWNER, detail)
        self.assertEqual(owner, {'edit', 'comment', 'set_state', 'cancel'})
        editor = request_pages.allowed_actions(Principal.editor('k0editor00', 'ed', None), detail)
        self.assertEqual(editor, owner)
        requester = request_pages.allowed_actions(Principal.requester('k0req00001', 'r', None), detail)
        self.assertEqual(requester, {'comment', 'cancel'})
        self.assertEqual(request_pages.allowed_actions(Principal.manager(self.a), detail), set())
        self.assertEqual(request_pages.allowed_actions(OWNER, {'state': 'CLOSED', 'cancel_state': None}),
                         {'comment', 'set_state'})


class EditTests(RequestsCase):
    def request_row(self, request_id):
        return self.db_rows('SELECT title, body, version, revision, review_round, state FROM requests '
                            'WHERE request_id = ?', request_id)[0]

    async def edit_form(self, request_id):
        response, page = await self.page(f'/requests/{request_id}/edit')
        self.assertEqual(response.status, 200)
        form = page.form(f'/requests/{request_id}/edit')
        self.assertIsNotNone(form)
        return form

    async def send(self, request_id, form, **fields):
        data = {'csrf': form['fields']['csrf'], 'base_version': form['fields']['base_version'], **fields}
        return await self.post(f'/requests/{request_id}/edit', data, cookies=self.cookies)

    async def test_form_is_prefilled_and_carries_the_base_version(self):
        request_id = self.add('Old title', 'old body')
        response, page = await self.page(f'/requests/{request_id}/edit')
        form = page.form(f'/requests/{request_id}/edit')
        self.assertEqual(set(form['fields']), {'csrf', 'base_version', 'title', 'body'})
        self.assertEqual(form['fields']['base_version'], '1')
        self.assertEqual(form['fields']['title'], 'Old title')
        self.assertIn('old body', page.body_text)

    async def test_new_request_is_edited_in_place_and_the_page_says_so(self):
        request_id = self.add('Old title', 'old body')
        form = await self.edit_form(request_id)
        response = await self.send(request_id, form, title='New title', body='new body')
        self.assertEqual((response.status, response.headers['Location']),
                         (303, f'/requests/{request_id}?notice=edited'))
        self.assertEqual(self.request_row(request_id), ('New title', 'new body', 2, 1, 1, 'NEW'))
        _, page = await self.page(response.headers['Location'])
        self.assertIn('내용을 바꿨습니다', page.body_text)

    async def test_reviewing_edit_starts_a_new_review_round(self):
        request_id = self.add('T', 'b')
        self.inbox.review(Principal.manager(self.a), request_id, 'begin_review')
        form = await self.edit_form(request_id)
        response = await self.send(request_id, form, title='T2', body='b2')
        self.assertEqual(response.status, 303)
        row = self.request_row(request_id)
        self.assertEqual((row[0], row[3], row[4], row[5]), ('T2', 1, 2, 'REVIEWING'))

    async def test_ready_edit_makes_a_new_revision_and_says_so(self):
        request_id = self.add('T', 'b')
        self.link(request_id, 'dispatched')
        form = await self.edit_form(request_id)
        response = await self.send(request_id, form, title='T v2', body='b v2')
        self.assertEqual(response.headers['Location'], f'/requests/{request_id}?notice=revised')
        self.assertEqual(self.request_row(request_id)[3], 2)
        self.assertEqual(len(self.db_rows('SELECT * FROM request_revisions WHERE request_id = ?', request_id)), 2)
        _, page = await self.page(response.headers['Location'])
        self.assertIn('새 revision으로 저장했습니다', page.body_text)

    async def test_conflict_shows_the_other_change_and_keeps_the_typed_text(self):
        request_id = self.add('Original', 'original body')
        form = await self.edit_form(request_id)
        self.inbox.update(OWNER, request_id, 1, title='Changed elsewhere', body='elsewhere body')
        response = await self.send(request_id, form, title='My title', body='my body')
        page = Page(response.body_text)
        self.assertEqual(response.status, 409)
        self.assertIn('다른 곳에서 바뀌었습니다', page.body_text)
        self.assertIn('Changed elsewhere', page.body_text)
        self.assertIn('elsewhere body', page.pre)
        again = page.form(f'/requests/{request_id}/edit')
        self.assertEqual((again['fields']['title'], again['fields']['base_version']), ('My title', '2'))
        self.assertIn('my body', page.body_text)
        self.assertEqual(self.request_row(request_id)[:3], ('Changed elsewhere', 'elsewhere body', 2))

    async def test_too_large_edit_is_shown_again_with_the_reason_and_changes_nothing(self):
        request_id = self.add('T', 'b')
        form = await self.edit_form(request_id)
        response = await self.send(request_id, form, title='x' * 201, body='b')
        page = Page(response.body_text)
        self.assertEqual(response.status, 400)
        self.assertIn('입력이 한도를 넘었습니다', page.body_text)
        self.assertEqual(page.form(f'/requests/{request_id}/edit')['fields']['title'], 'x' * 201)
        self.assertEqual(self.request_row(request_id)[:3], ('T', 'b', 1))

    async def test_closed_request_cannot_be_edited_by_get_or_post(self):
        request_id = self.add('T', 'b')
        form = await self.edit_form(request_id)
        self.inbox.cancel(OWNER, request_id)
        response, page = await self.page(f'/requests/{request_id}/edit')
        self.assertEqual(response.status, 409)
        self.assertIsNone(page.form(f'/requests/{request_id}/edit'))
        response = await self.send(request_id, form, title='again', body='b')
        self.assertEqual(response.status, 409)
        self.assertEqual(self.request_row(request_id)[0], 'T')

    async def test_bad_base_version_and_unknown_request(self):
        request_id = self.add('T', 'b')
        form = await self.edit_form(request_id)
        for bad in ('', 'x', '1.5'):
            response = await self.post(f'/requests/{request_id}/edit',
                                       {'csrf': form['fields']['csrf'], 'base_version': bad, 'title': 'n', 'body': 'b'},
                                       cookies=self.cookies)
            self.assertEqual(response.status, 400, bad)
        self.assertEqual(self.request_row(request_id)[0], 'T')
        response, _ = await self.page(f'/requests/{uuid.uuid4()}/edit')
        self.assertEqual(response.status, 404)


if __name__ == '__main__':
    unittest.main()
