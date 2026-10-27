"""WBX-C2 dashboard ``/`` and ``GET /api/dashboard`` (seams S1+S6: the real ``create_app`` with the project routes over a
fake Hub list and a real inbox store; HTML read with stdlib ``html.parser``).

Expected values are the ticket/SPEC/BRIEF words (WBX-D13, D18, AC-20), the inputs the test chose, and request states the
test writes into the store itself (``set_state``/``link`` through the service, ``UPDATE`` through stdlib ``sqlite3``),
counted here independently of the page code. The JS refresh logic is ``tests/hub/js/dashboard.test.mjs`` (S8) and the
CSS checks are ``test_web_dashboard_css.py`` (S5). Run with ``discover -s tests/hub -p 'test_web_dashboard*.py'``.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import sqlite3
import sys
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

import test_web_pages_projects as projects_tests  # noqa: E402  (module import: its test classes stay uncollected here)
from test_web_pages_requests import OWNER, Page  # noqa: E402
from workbench.hub.inbox.service import InboxError  # noqa: E402

COUNTS = projects_tests.COUNTS
hub_row = projects_tests.hub_row


class Cards(HTMLParser):
    """``{data-card: [label, value, sub]}`` of the summary cards, read from the stat-label/stat-value/stat-sub spans."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.cards: dict[str, list[str]] = {}
        self._card: list[str] | None = None
        self._part: int | None = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('data-card'):
            self._card = self.cards.setdefault(attrs['data-card'], ['', '', ''])
        names = (attrs.get('class') or '').split()
        for index, name in enumerate(('stat-label', 'stat-value', 'stat-sub')):
            if name in names:
                self._part = index

    def handle_endtag(self, tag):
        if tag in ('span', 'strong'):
            self._part = None
        if tag == 'a':
            self._card = None

    def handle_data(self, data):
        if self._card is not None and self._part is not None:
            self._card[self._part] += data


class DashboardCase(projects_tests.ProjectsCase):
    """Two projects (alpha on, beta off) over a real inbox; helpers write request states straight into the store."""

    def add(self, title='Title', body='Body', project=None, principal=None) -> str:
        return self.inbox.submit(principal or OWNER, project or self.a, title, body)['request_id']

    def force(self, request_id: str, root: Path, **columns) -> None:
        """Write columns of one request row directly (a state the service would only reach through a Task)."""
        connection = sqlite3.connect(root / '.wb' / 'inbox.sqlite3')
        try:
            names = ', '.join(f'{name} = ?' for name in columns)
            connection.execute(f'UPDATE requests SET {names} WHERE request_id = ?', (*columns.values(), request_id))
            connection.commit()
        finally:
            connection.close()

    def populate(self) -> dict:
        """Requests in every state, written into the store by the test. Returns the ids by label."""
        ids = {}
        ids['new_a1'], ids['new_a2'] = self.add('new one'), self.add('new two')
        ids['new_b'] = self.add('new beta', project=self.b)
        ids['reviewing'] = self.add('under review')
        self.force(ids['reviewing'], self.root_a, state='REVIEWING')
        ids['running'] = self.add('running now')
        self.force(ids['running'], self.root_a, state='RUNNING', task_id='task-1')
        ids['ready'] = self.add('ready to start')
        self.force(ids['ready'], self.root_a, state='READY', task_id='task-2')
        ids['question'] = self.add('needs an answer')
        self.inbox.set_state(OWNER, ids['question'], 1, 'NEEDS_INFO')
        ids['decision'] = self.add('retry limit used up', project=self.b)  # NEEDS_INFO with the Task link kept (P2-AC-27)
        self.force(ids['decision'], self.root_b, state='NEEDS_INFO', task_id='task-3')
        ids['closed'] = self.add('already done')
        self.inbox.cancel(OWNER, ids['closed'])
        return ids

    async def api(self, path: str = '/api/dashboard', **kwargs):
        response = await self.get(path, cookies=self.cookies, **kwargs)
        return response, (json.loads(response.body_text) if response.content_type == 'application/json' else None)


class AccessTests(DashboardCase):
    async def test_without_a_session_the_json_is_a_401_not_a_redirect(self):
        response = await self.get('/api/dashboard')
        self.assertEqual(response.status, 401)
        self.assertNotIn('Location', response.headers)
        self.assertNotIn('alpha', response.body_text)

    async def test_an_api_key_is_not_a_session(self):
        issued = self.services.api_keys.issue('bot', 'requester', {'all': True}, 1, now=self.now[0])
        response = await self.get('/api/dashboard', headers={'Authorization': f'Bearer {issued.raw}'})
        self.assertEqual(response.status, 401)

    async def test_a_session_gets_json_that_is_never_stored(self):
        response, body = await self.api()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, 'application/json')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIsInstance(body, dict)


class ProjectSummaryTests(DashboardCase):
    async def test_project_cards_count_the_hub_rows_by_power(self):
        self.rows = [hub_row('a1', self.a, power='on'), hub_row('a2', 'p-2', power='on'), hub_row('b1', self.b, power='off'),
                     hub_row('u1', 'p-4', power='unknown', power_detail='timeout'),
                     hub_row('m1', 'p-5', power='mismatch', power_detail='root_missing'),
                     {'root': '/srv/x', 'name': 'bare', 'project_id': 'p-6'}]  # no power value: unknown, never off
        _, body = await self.api()
        self.assertEqual(body['summary']['projects'], {'total': 6, 'running': 2, 'closed': 1, 'unknown': 2, 'other': 1})
        self.assertIsNone(body['messages']['projects'])

    async def test_an_unreadable_list_is_a_fixed_problem_text_and_no_numbers(self):
        self.list_error = RuntimeError('/secret/path/to/hub.sock exploded')
        response, body = await self.api()
        self.assertEqual(response.status, 200)
        self.assertEqual(body['messages']['projects'], {'kind': 'error', 'text': '프로젝트 상태를 읽지 못했습니다. 잠시 뒤 다시 시도하세요'})
        self.assertEqual(body['summary']['projects'], {'total': None, 'running': None, 'closed': None,
                                                       'unknown': None, 'other': None})
        self.assertEqual(body['projects'], [])
        self.assertNotIn('/secret', response.body_text)


class RequestSummaryTests(DashboardCase):
    async def test_request_cards_count_the_open_requests_of_every_project_by_state(self):
        self.populate()
        _, body = await self.api()
        # NEW 3 (two alpha, one beta), REVIEWING 1, NEEDS_INFO 2 (alpha question, beta decision wait), RUNNING 1;
        # READY and CLOSED have no card.
        self.assertEqual(body['summary']['requests'], {'new': 3, 'reviewing': 1, 'needs_info': 2, 'running': 1})

    async def test_requests_waiting_for_the_owner_are_the_questions_and_the_decisions_after_the_retry_limit(self):
        self.populate()
        _, body = await self.api()
        self.assertEqual(body['summary']['attention'], {'total': 2, 'questions': 1, 'decisions': 1})

    async def test_a_request_state_changed_in_the_store_shows_on_the_next_read(self):
        ids = self.populate()
        _, before = await self.api()
        self.force(ids['reviewing'], self.root_a, state='NEEDS_INFO')
        _, after = await self.api()
        self.assertEqual((before['summary']['requests']['reviewing'], after['summary']['requests']['reviewing']), (1, 0))
        self.assertEqual(after['summary']['attention']['total'], before['summary']['attention']['total'] + 1)

    async def test_no_requests_is_zero_not_unknown(self):
        _, body = await self.api()
        self.assertEqual(body['summary']['requests'], {'new': 0, 'reviewing': 0, 'needs_info': 0, 'running': 0})
        self.assertEqual(body['summary']['attention'], {'total': 0, 'questions': 0, 'decisions': 0})


class ProjectTableTests(DashboardCase):
    async def test_a_project_row_carries_the_hub_list_words_and_its_links(self):
        self.rows = [
            hub_row('alpha', self.a, power='on', task={'task_id': 't1', 'summary': '로그인 수정', 'status': 'running'},
                    command={'label': 'build', 'state': 'running'}, waits=[{'code': 'manager_editor_not_empty'}],
                    pending_requests={**COUNTS, 'new': 2, 'needs_info': 1}, review_mode='manual',
                    last_activity=1_000_000.0 - 120),
            hub_row('beta', self.b, power='off')]
        _, body = await self.api()
        self.assertEqual(body['projects'][0], {
            'project_id': self.a, 'url': f'/projects/{self.a}', 'requests_url': f'/requests?project={self.a}',
            'name': 'alpha', 'path': '/srv/work/alpha', 'power': '켜짐', 'power_kind': 'on', 'task': '로그인 수정 · running',
            'command': 'build (실행 중)', 'wait': 'manager 입력창에 글이 있어 알림 대기', 'requests': '대기 2 · 질문 1',
            'last_activity': '2분 전', 'review_mode': '수동'})
        self.assertEqual(body['projects'][1], {
            'project_id': self.b, 'url': f'/projects/{self.b}', 'requests_url': f'/requests?project={self.b}',
            'name': 'beta', 'path': '/srv/work/beta', 'power': '꺼짐', 'power_kind': 'off', 'task': '—', 'command': '—',
            'wait': '—', 'requests': '0', 'last_activity': '—', 'review_mode': '자동'})

    async def test_an_unobserved_project_is_unknown_never_off(self):
        self.rows = [hub_row('alpha', self.a, power='unknown', power_detail='timeout', pending_requests=None,
                             review_mode=None),
                     hub_row('beta', self.b, power='mismatch', power_detail='root_missing'),
                     hub_row('gamma', 'p-3', power='starting')]
        _, body = await self.api()
        self.assertEqual([(row['power'], row['power_kind'], row['requests'], row['review_mode']) for row in body['projects']],
                         [('연결 없음/불명', 'unknown', '—', '—'), ('불일치(root_missing)', 'unknown', '0', '자동'),
                          ('시작 중', 'busy', '0', '자동')])

    async def test_project_text_is_data_in_the_json(self):
        self.rows = [hub_row('<b>x</b>', self.a, task={'summary': '<script>alert(1)</script>', 'status': 's'})]
        _, body = await self.api()
        self.assertEqual(body['projects'][0]['name'], '<b>x</b>')
        self.assertEqual(body['projects'][0]['task'], '<script>alert(1)</script> · s')


class RecentRequestTests(DashboardCase):
    def eleven_alpha_one_beta(self) -> list[str]:
        beta = self.add('beta first', project=self.b)
        self.force(beta, self.root_b, updated_at=1_000_000.0 - 86400 * 3)
        alpha = [self.add(f'alpha {number:02d}') for number in range(1, 12)]
        for number, request_id in enumerate(alpha, start=1):
            self.force(request_id, self.root_a, updated_at=1_000_000.0 - 3600 * (12 - number))
        return alpha

    async def test_the_ten_newest_requests_of_all_projects_closed_ones_included(self):
        alpha = self.eleven_alpha_one_beta()
        self.inbox.cancel(OWNER, alpha[-1])  # the newest one is closed: it still counts as recent
        self.force(alpha[-1], self.root_a, updated_at=1_000_000.0 - 180)
        _, body = await self.api()
        recent = body['recent']
        self.assertEqual([item['title'] for item in recent], [f'alpha {number:02d}' for number in range(11, 1, -1)])
        self.assertEqual(recent[0], {
            'request_id': alpha[-1], 'url': f'/requests/{alpha[-1]}', 'title': 'alpha 11', 'project': 'alpha',
            'state': 'CLOSED', 'state_label': '닫힘(취소)', 'state_kind': 'closed', 'origin': '소유자',
            'updated': '3분 전', 'attention': False})
        self.assertEqual([item['updated'] for item in recent[1:3]], ['2시간 전', '3시간 전'])

    async def test_a_request_the_owner_must_answer_is_flagged_in_the_recent_list(self):
        request_id = self.add('please answer')
        self.inbox.set_state(OWNER, request_id, 1, 'NEEDS_INFO')
        _, body = await self.api()
        self.assertEqual([(item['state_label'], item['state_kind'], item['attention']) for item in body['recent']],
                         [('추가 정보 필요', 'needs', True)])

    async def test_the_project_of_a_request_is_named_by_the_hub_row(self):
        self.add('in beta', project=self.b)
        _, body = await self.api()
        self.assertEqual([item['project'] for item in body['recent']], ['beta'])


class AttentionTests(DashboardCase):
    async def test_the_waiting_requests_are_listed_newest_first_with_the_reason(self):
        question = self.add('needs an answer')
        self.inbox.set_state(OWNER, question, 1, 'NEEDS_INFO')
        self.force(question, self.root_a, updated_at=1_000_000.0 - 180)
        decision = self.add('retry limit used up', project=self.b)
        self.force(decision, self.root_b, state='NEEDS_INFO', task_id='task-3', updated_at=1_000_000.0 - 1500)
        self.add('only new')  # NEW is not waiting for the owner
        _, body = await self.api()
        self.assertEqual(body['attention'], [
            {'request_id': question, 'url': f'/requests/{question}', 'title': 'needs an answer', 'project': 'alpha',
             'reason': '추가 정보 필요', 'updated': '3분 전'},
            {'request_id': decision, 'url': f'/requests/{decision}', 'title': 'retry limit used up', 'project': 'beta',
             'reason': 'Task 결정 대기', 'updated': '25분 전'}])

    async def test_the_list_shows_at_most_ten_while_the_card_counts_all(self):
        for number in range(12):
            request_id = self.add(f'q{number}')
            self.inbox.set_state(OWNER, request_id, 1, 'NEEDS_INFO')
        _, body = await self.api()
        self.assertEqual(len(body['attention']), 10)
        self.assertEqual(body['summary']['attention']['total'], 12)


class BoardCardTests(DashboardCase):
    async def test_the_board_agent_state_is_the_nav_state_with_a_link_to_open_it(self):
        for state, label in (('on', '실행 중'), ('off', '중지'), ('error', '오류')):
            self.services.board_state = lambda state=state: state
            _, body = await self.api()
            self.assertEqual(body['board'], {'state': state, 'label': label, 'url': '/board'})

    async def test_a_broken_board_state_reads_as_error(self):
        def broken() -> str:
            raise RuntimeError('boom')
        self.services.board_state = broken
        _, body = await self.api()
        self.assertEqual(body['board']['state'], 'error')


class EmptyMessageTests(DashboardCase):
    async def test_what_is_empty_says_so_and_what_is_not_says_nothing(self):
        self.rows = []
        _, body = await self.api()
        self.assertEqual(body['messages'], {
            'projects': {'kind': 'empty', 'text': '등록된 프로젝트가 없습니다'},
            'recent': {'kind': 'empty', 'text': '요청이 없습니다'},
            'attention': {'kind': 'empty', 'text': '답을 기다리는 요청이 없습니다'}, 'requests': None})
        self.add('one')
        question = self.add('two')
        self.inbox.set_state(OWNER, question, 1, 'NEEDS_INFO')
        self.rows = [hub_row('alpha', self.a)]
        _, body = await self.api()
        self.assertEqual(body['messages'], {'projects': None, 'recent': None, 'attention': None, 'requests': None})


class UnreadableInboxTests(DashboardCase):
    async def test_an_inbox_that_cannot_be_read_is_unknown_numbers_and_a_fixed_text_never_zero(self):
        self.add('exists')

        def broken(*args, **kwargs):
            raise InboxError('inbox_unavailable', '/secret/alpha/.wb/inbox.sqlite3 is locked')
        self.inbox.list_requests = broken
        response, body = await self.api()
        self.assertEqual(response.status, 200)
        self.assertEqual(body['summary']['requests'], dict.fromkeys(('new', 'reviewing', 'needs_info', 'running')))
        self.assertEqual(body['summary']['attention'], {'total': None, 'questions': None, 'decisions': None})
        self.assertEqual((body['recent'], body['attention']), ([], []))
        self.assertEqual(body['messages']['requests'], {'kind': 'error', 'text': '요청 저장소를 읽지 못했습니다'})
        self.assertEqual((body['messages']['recent'], body['messages']['attention']), (None, None))  # unknown, not empty
        self.assertNotIn('/secret', response.body_text)
        self.assertEqual(body['summary']['projects']['total'], 2)  # the project half still answers

    async def test_a_project_whose_inbox_is_unavailable_is_named_as_partial_numbers(self):
        self.add('exists')
        real = self.inbox.list_requests

        def partial(*args, **kwargs):
            found = real(*args, **kwargs)
            return {**found, 'unavailable': [{'project_id': self.b, 'code': 'inbox_unavailable'}]}
        self.inbox.list_requests = partial
        _, body = await self.api()
        self.assertEqual(body['summary']['requests']['new'], 1)
        self.assertEqual(body['messages']['requests'], {'kind': 'error', 'text': '일부 프로젝트의 요청을 읽지 못했습니다'})

    async def test_a_readable_inbox_has_no_requests_problem(self):
        _, body = await self.api()
        self.assertIsNone(body['messages']['requests'])


class PageTests(DashboardCase):
    """``GET /`` in HTML: the same model as the JSON, drawn on the server (complete without JavaScript)."""

    async def test_without_a_session_the_page_goes_to_login(self):
        response = await self.get('/')
        self.assertEqual((response.status, response.headers['Location']), (303, '/login'))

    async def test_the_page_has_the_four_sections_and_no_meta_refresh(self):
        response, page = await self.page('/')
        self.assertEqual(response.status, 200)
        ids = {attrs.get('id') for _, attrs in page.elements}
        self.assertGreaterEqual(ids, {'summary', 'projects', 'recent-requests', 'board-agent', 'attention'})
        self.assertEqual([attrs for attrs in page.tags('meta') if attrs.get('http-equiv')], [])
        self.assertNotIn('http-equiv', response.body_text)
        self.assertIn('프로젝트', page.sections['projects'])
        self.assertIn('최근 요청', page.sections['recent-requests'])
        self.assertIn('Board agent', page.sections['board-agent'])
        self.assertIn('내 답이 필요한 요청', page.sections['attention'])

    async def test_the_page_is_complete_without_javascript(self):
        self.populate()
        self.rows = [hub_row('alpha', self.a)]
        response, page = await self.page('/')
        hidden = sorted(attrs['id'] for _, attrs in page.elements if 'hidden' in attrs and attrs.get('id'))
        # Only what JavaScript fills in or an absent message stays hidden; every table, card and list is in the HTML.
        self.assertEqual(hidden, sorted(['dash-auto', 'projects-message', 'recent-message', 'attention-message',
                                         'requests-message']))
        self.assertEqual(len(page.data_rows()), 1 + 9)  # one project and the nine requests
        self.assertIn('needs an answer', page.sections['attention'])

    async def test_the_page_has_every_element_the_dashboard_script_updates(self):
        # The contract between the server markup and static/dashboard.js (tests/hub/js/dashboard.test.mjs builds the
        # same skeleton): ids it writes to, one value and one sub line per card.
        response, page = await self.page('/')
        ids = {attrs.get('id') for _, attrs in page.elements}
        card_ids = ['projects-running', 'projects-closed', 'requests-new', 'requests-reviewing', 'requests-needs-info',
                    'requests-running', 'attention']
        wanted = {'dash-auto', 'dash-time', 'projects-note', 'projects-message', 'projects-wrap', 'projects-body',
                  'requests-message', 'recent-message', 'recent-wrap', 'recent-body', 'attention-count',
                  'attention-list', 'attention-message', 'board-dot', 'board-label', 'board-open'}
        wanted |= {f'card-{name}{part}' for name in card_ids for part in ('', '-value', '-sub')}
        self.assertEqual(sorted(wanted - ids), [])

    async def test_the_page_loads_the_dashboard_script_and_no_inline_code(self):
        response, page = await self.page('/')
        self.assertEqual([attrs.get('src') for attrs in page.tags('script')],
                         ['/static/session-check.js', '/static/badge.js', '/static/dashboard.js'])
        self.assertNotIn('style=', response.body_text)
        self.assertNotIn('onclick', response.body_text)
        self.assertIn('id="wb-badge"', response.body_text)

    async def test_the_cards_show_the_counts_the_store_holds(self):
        self.populate()
        self.rows = [hub_row('a1', self.a, power='on'), hub_row('a2', 'p-2', power='on'), hub_row('b1', self.b, power='off'),
                     hub_row('u1', 'p-4', power='unknown')]
        response, page = await self.page('/')
        cards = Cards(response.body_text).cards
        self.assertEqual(cards, {
            'projects-running': ['프로젝트 실행 중', '2', '전체 4개 중'],
            'projects-closed': ['프로젝트 꺼짐', '1', '연결 없음/불명 1'],
            'requests-new': ['신규', '3', '검토 대기'],
            'requests-reviewing': ['검토 중', '1', 'manager 검토'],
            'requests-needs-info': ['추가 정보 필요', '2', '질문 답변 대기'],
            'requests-running': ['실행 중', '1', '승인된 요청'],
            'attention': ['내 답이 필요한 요청', '2', '추가 정보 필요 1 · Task 결정 대기 1']})

    async def test_each_card_links_to_where_its_requests_are(self):
        _, page = await self.page('/')
        links = {attrs['data-card']: attrs['href'] for tag, attrs in page.elements if tag == 'a' and attrs.get('data-card')}
        self.assertEqual(links, {
            'projects-running': '#projects', 'projects-closed': '#projects',
            'requests-new': '/requests?state=NEW', 'requests-reviewing': '/requests?state=REVIEWING',
            'requests-needs-info': '/requests?state=NEEDS_INFO', 'requests-running': '/requests?state=RUNNING',
            'attention': '/requests?state=NEEDS_INFO'})

    async def test_the_project_table_keeps_every_hub_list_column_and_adds_the_shortcuts(self):
        self.rows = [
            hub_row('alpha', self.a, power='on', task={'task_id': 't1', 'summary': '로그인 수정', 'status': 'running'},
                    command={'label': 'build', 'state': 'running'}, waits=[{'code': 'manager_editor_not_empty'}],
                    pending_requests={**COUNTS, 'new': 2, 'needs_info': 1}, review_mode='manual',
                    last_activity=1_000_000.0 - 120),
            hub_row('beta', self.b, power='off')]
        _, page = await self.page('/')
        header = [row for row, data in zip(page.rows, page.row_is_data) if not data][0]
        self.assertEqual(header, ['이름', '경로', '상태', 'Task·단계', '명령', '대기 사유', '요청', '마지막 활동', '검토 방식',
                                  '바로가기'])
        rows = page.data_rows()
        self.assertEqual(rows[0][:9], ['alpha', '/srv/work/alpha', '켜짐', '로그인 수정 · running', 'build (실행 중)',
                                       'manager 입력창에 글이 있어 알림 대기', '대기 2 · 질문 1', '2분 전', '수동'])
        self.assertEqual(rows[1][:9], ['beta', '/srv/work/beta', '꺼짐', '—', '—', '—', '0', '—', '자동'])
        self.assertEqual(page.data_links()[0], [f'/projects/{self.a}', f'/projects/{self.a}',
                                                f'/requests?project={self.a}'])
        self.assertEqual([row[9] for row in rows], ['상태 요청', '상태 요청'])

    async def test_the_page_and_the_json_say_the_same_things(self):
        self.populate()
        _, page = await self.page('/')
        _, body = await self.api()
        self.assertEqual([row[0] for row in page.data_rows()][:2], [project['name'] for project in body['projects']])
        recent = page.sections['recent-requests']
        for item in body['recent']:
            self.assertIn(item['title'], recent)
        attention = page.sections['attention']
        for item in body['attention']:
            self.assertIn(item['title'], attention)
            self.assertIn(item['reason'], attention)
        self.assertIn(body['board']['label'], page.sections['board-agent'])

    async def test_a_recent_request_the_owner_must_answer_carries_the_flag(self):
        request_id = self.add('please answer')
        self.inbox.set_state(OWNER, request_id, 1, 'NEEDS_INFO')
        self.add('fine')
        _, page = await self.page('/')
        flags = [attrs for tag, attrs in page.elements if tag == 'span' and 'flag' in attrs.get('class', '').split()]
        self.assertEqual(len(flags), 1)
        self.assertIn('내 답 필요', page.sections['recent-requests'])

    async def test_the_board_card_has_the_state_and_an_open_button(self):
        self.services.board_state = lambda: 'on'
        _, page = await self.page('/')
        self.assertIn('실행 중', page.sections['board-agent'])
        buttons = [attrs for tag, attrs in page.elements if tag == 'a' and attrs.get('href') == '/board'
                   and 'btn' in attrs.get('class', '').split()]
        self.assertEqual(len(buttons), 1)
        self.assertIn('열기', page.sections['board-agent'])

    async def test_titles_and_names_are_escaped_text(self):
        self.rows = [hub_row('<b>x</b>', self.a)]
        request_id = self.add('<script>alert(1)</script>')
        self.inbox.set_state(OWNER, request_id, 1, 'NEEDS_INFO')
        response, _ = await self.page('/')
        self.assertNotIn('<b>x</b>', response.body_text)
        self.assertNotIn('<script>alert', response.body_text)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', response.body_text)

    async def test_what_is_empty_or_wrong_is_announced_in_text_not_shown_as_zero_rows(self):
        self.rows = []
        _, page = await self.page('/')
        self.assertIn('등록된 프로젝트가 없습니다', page.sections['projects-message'])
        self.assertIn('요청이 없습니다', page.sections['recent-message'])
        self.assertEqual(page.data_rows(), [])
        self.list_error = RuntimeError('/secret/path exploded')
        response, page = await self.page('/')
        self.assertIn('프로젝트 상태를 읽지 못했습니다', page.sections['projects-message'])
        self.assertNotIn('등록된 프로젝트가 없습니다', response.body_text)
        self.assertNotIn('/secret', response.body_text)

    async def test_the_page_names_a_requests_problem_and_shows_dashes_for_the_unknown_numbers(self):
        def broken(*args, **kwargs):
            raise InboxError('inbox_unavailable', '/secret/db')
        self.inbox.list_requests = broken
        response, page = await self.page('/')
        self.assertIn('요청 저장소를 읽지 못했습니다', page.sections['requests-message'])
        self.assertEqual(Cards(response.body_text).cards['requests-new'], ['신규', '—', '검토 대기'])
        self.assertNotIn('/secret', response.body_text)


if __name__ == '__main__':
    unittest.main()
