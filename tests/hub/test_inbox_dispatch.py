"""Hub dispatcher rules (TI-10, seam T1 with a fake backend port).

A real ``InboxService`` on temp project dirs and a fake backend port (``identity`` / ``inbox_notice``) the test
controls; the test drives ``Dispatcher.tick()`` itself. Expected values come from the SPEC "Hub dispatcher" rules
(one offered notice per project, ``seq`` order, auto/manual, mode-independent events, outcomes, backend identity
change), the inputs the test chose and the inbox rows read back with stdlib ``sqlite3``.
Run with ``discover -s tests/hub -p 'test_inbox_dispatch*.py'``.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_inbox_service import OWNER, make_project  # noqa: E402
from workbench.hub.inbox.backend_port import BackendPortError  # noqa: E402
from workbench.hub.inbox.service import InboxService, Principal, ProjectRef  # noqa: E402

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'


class FakePort:
    """``identity(root)``: ``backends[root]`` (None: off); ``inbox_notice`` records and answers ``queued``
    unless ``refuse`` names a reason for the next call."""

    def __init__(self):
        self.backends: dict[Path, str | None] = {}
        self.notices: list[dict] = []
        self.refuse: list[str] = []

    def identity(self, root: Path) -> str | None:
        return self.backends.get(Path(root))

    def inbox_notice(self, root, request_id, delivery_id, event, fields, **_):
        if self.refuse:
            raise BackendPortError(self.refuse.pop(0))
        self.notices.append({'root': Path(root), 'request_id': request_id, 'delivery_id': delivery_id,
                             'event': event, 'fields': fields})
        return {'ok': True, 'queued': True, 'notice_id': str(uuid.uuid4())}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class DispatchCase(unittest.TestCase):
    def setUp(self):
        from workbench.hub.inbox.dispatch import Dispatcher
        self.temp = tempfile.TemporaryDirectory(prefix='wb-inbox-dispatch-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.root, self.a = make_project(base, 'alpha')
        self.root_b, self.b = make_project(base, 'beta')
        self.port = FakePort()
        self.port.backends = {self.root: '100:5', self.root_b: '200:7'}
        self.clock = Clock()
        self.refs = [ProjectRef(self.a, self.root, 'alpha'), ProjectRef(self.b, self.root_b, 'beta')]
        self.service = InboxService(on_committed=lambda event: self.dispatcher.committed(event))
        self.addCleanup(self.service.close)
        self.service.set_projects(self.refs)
        self.dispatcher = Dispatcher(self.service, lambda: self.refs, port=self.port, clock=self.clock)
        self.manager = Principal.manager(self.a)

    def new(self, title: str = 'Title', body: str = 'Body', project: str | None = None) -> str:
        return self.service.submit(OWNER, project or self.a, title, body, None)['request_id']

    def row(self, request_id: str, root: Path | None = None) -> dict:
        db = (root or self.root) / '.wb' / 'inbox.sqlite3'
        connection = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return dict(connection.execute('SELECT * FROM requests WHERE request_id = ?', (request_id,)).fetchone())
        finally:
            connection.close()

    def comments(self, request_id: str) -> list[tuple[str, str]]:
        db = self.root / '.wb' / 'inbox.sqlite3'
        connection = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        try:
            return connection.execute('SELECT kind, body FROM comments WHERE request_id = ? ORDER BY seq',
                                      (request_id,)).fetchall()
        finally:
            connection.close()

    def tick(self, advance: float = 2.0) -> None:
        self.clock.now += advance
        self.dispatcher.tick()

    def deliver(self, request_id: str, outcome: str = 'delivered') -> dict:
        """The backend's ``inbox_delivery`` for the last notice of ``request_id`` (as the Hub server records it)."""
        notice = [item for item in self.port.notices if item['request_id'] == request_id][-1]
        return self.service.record_delivery(request_id, notice['delivery_id'], outcome)


class AutoDeliveryTests(DispatchCase):
    def test_a_new_request_is_offered_once_and_becomes_reviewing_only_when_delivered(self):
        rid = self.new('Fix the build', 'The build fails on main.')
        self.tick()
        self.assertEqual(len(self.port.notices), 1)
        notice = self.port.notices[0]
        self.assertEqual((notice['root'], notice['request_id'], notice['event']), (self.root, rid, 'new'))
        self.assertRegex(notice['delivery_id'], UUID)
        self.assertEqual({key: notice['fields'][key] for key in ('request_id', 'seq', 'title', 'state', 'excerpt')},
                         {'request_id': rid, 'seq': 1, 'title': 'Fix the build', 'state': 'NEW',
                          'excerpt': 'The build fails on main.'})
        self.assertEqual(set(notice['fields']), {'request_id', 'seq', 'title', 'origin', 'state', 'excerpt', 'hint'})
        row = self.row(rid)
        self.assertEqual((row['state'], row['delivery_state'], row['delivery_id'], row['delivery_backend']),
                         ('NEW', 'offered', notice['delivery_id'], '100:5'), 'P2-AC-07/08: offered is not REVIEWING')
        self.tick()
        self.assertEqual(len(self.port.notices), 1, 'an offered notice is not sent again')
        self.assertEqual(self.deliver(rid)['state'], 'REVIEWING')
        self.tick()
        self.assertEqual((self.row(rid)['state'], self.row(rid)['delivery_state']), ('REVIEWING', 'delivered'))
        self.assertEqual(len(self.port.notices), 1)


class ManualModeTests(DispatchCase):
    def setUp(self):
        super().setUp()
        self.service.review_mode(Principal.local(), self.a, 'manual')

    def test_nothing_is_sent_until_the_owner_starts_the_review(self):
        rid = self.new()
        for _ in range(3):
            self.tick()
        self.assertEqual((self.port.notices, self.row(rid)['state']), ([], 'NEW'), 'P2-AC-24: manual stays NEW')
        from workbench.hub.inbox.limits import InboxError
        for principal in (Principal.editor('k0editor00', 'ed', [self.a]), Principal.board_agent('k0board000'),
                          Principal.requester('k0req00001', 'r1', [self.a])):
            with self.assertRaises(InboxError):
                self.service.start_review(principal, rid)  # P2-AC-39: no API key starts a review
        self.tick()
        self.assertEqual(self.port.notices, [])
        self.service.start_review(OWNER, rid)
        self.tick()
        self.assertEqual([(item['request_id'], item['event']) for item in self.port.notices], [(rid, 'new')])
        self.assertEqual(self.deliver(rid)['state'], 'REVIEWING')

    def test_an_answer_returns_to_reviewing_and_waits_for_the_next_start(self):
        rid = self.new()
        self.service.start_review(OWNER, rid)
        self.tick()
        self.deliver(rid)
        self.service.review(self.manager, rid, 'ask', body='Which branch?')
        self.service.comment(OWNER, rid, 'main')
        self.assertEqual(self.row(rid)['state'], 'REVIEWING')
        self.tick()
        self.assertEqual(len(self.port.notices), 1, 'manual: the new round waits for 검토 시작')
        self.service.start_review(OWNER, rid)
        self.tick()
        self.assertEqual([item['event'] for item in self.port.notices], ['new', 'answered'])


class OrderTests(DispatchCase):
    def test_an_off_project_gets_nothing_and_once_on_its_requests_go_one_at_a_time_in_seq_order(self):
        self.port.backends[self.root] = None
        ids = [self.new(f'request {n}') for n in range(3)]
        other = self.new('beta request', project=self.b)
        for _ in range(3):
            self.tick()
        self.assertEqual([item['request_id'] for item in self.port.notices], [other], 'P2-AC-23: off gets nothing')
        self.port.backends[self.root] = '100:5'
        sent = []
        for rid in ids:
            self.tick()
            self.tick()
            mine = [item['request_id'] for item in self.port.notices if item['root'] == self.root]
            self.assertEqual(mine, sent + [rid], 'one offered notice per project, seq order')
            sent.append(rid)
            self.deliver(rid)
        self.assertEqual([self.row(rid)['state'] for rid in ids], ['REVIEWING'] * 3)


class OutcomeTests(DispatchCase):
    def test_unknown_is_reviewing_with_a_system_comment_and_is_never_resent(self):
        rid = self.new()
        self.tick()
        self.assertEqual(self.deliver(rid, 'unknown')['state'], 'REVIEWING')
        self.assertEqual(self.comments(rid), [('system', 'manager 전달 여부 확인 불명')])
        for _ in range(5):
            self.tick(advance=60.0)
        self.assertEqual(len(self.port.notices), 1, 'P2-AC-11: never resent')
        self.assertEqual(self.row(rid)['delivery_state'], 'unknown')
        self.service.start_review(OWNER, rid)  # the owner's "다시 전달"
        self.tick()
        self.assertEqual(len(self.port.notices), 2)
        self.assertNotEqual(self.port.notices[0]['delivery_id'], self.port.notices[1]['delivery_id'])

    def test_a_lost_notice_answer_is_recorded_unknown_and_not_resent(self):
        rid = self.new()
        self.port.refuse.append('unknown')
        self.tick()
        row = self.row(rid)
        self.assertEqual((row['state'], row['delivery_state']), ('REVIEWING', 'unknown'))
        self.assertEqual(self.comments(rid), [('system', 'manager 전달 여부 확인 불명')])
        self.tick(advance=60.0)
        self.assertEqual(self.port.notices, [])

    def test_rejected_and_dropped_return_to_pending_with_a_comment_and_are_sent_again_later(self):
        for outcome in ('rejected', 'dropped'):
            with self.subTest(outcome=outcome):
                rid = self.new(outcome)
                self.tick()
                self.assertEqual(self.deliver(rid, outcome)['state'], 'NEW')
                self.assertEqual(self.row(rid)['delivery_state'], 'pending')
                self.assertEqual(self.comments(rid)[-1], ('system', f'manager 전달 실패({outcome}). 다시 보냅니다.'))
                count = len(self.port.notices)
                self.tick()
                self.assertEqual(len(self.port.notices), count, 'not at once')
                self.tick(advance=10.0)
                self.assertEqual(self.port.notices[-1]['request_id'], rid)
                self.assertEqual(self.deliver(rid)['state'], 'REVIEWING')

    def test_a_refused_notice_call_marks_nothing_and_is_tried_again_later(self):
        rid = self.new()
        self.port.refuse.append('backend_too_old')
        self.tick()
        row = self.row(rid)
        self.assertEqual((row['state'], row['delivery_state']), ('NEW', 'pending'))
        self.tick()
        self.assertEqual(self.port.notices, [])
        self.tick(advance=30.0)
        self.assertEqual([item['request_id'] for item in self.port.notices], [rid])
        self.assertEqual(self.row(rid)['delivery_state'], 'offered')

    def test_a_result_that_comes_back_before_the_offer_is_recorded_still_counts(self):
        rid = self.new()
        original = self.port.inbox_notice

        def answer_at_once(root, request_id, delivery_id, event, fields, **kwargs):
            reply = original(root, request_id, delivery_id, event, fields, **kwargs)
            self.service.record_delivery(request_id, delivery_id, 'delivered')
            return reply
        self.port.inbox_notice = answer_at_once
        self.tick()
        row = self.row(rid)
        self.assertEqual((row['state'], row['delivery_state']), ('REVIEWING', 'delivered'))
        self.tick()
        self.assertEqual(len(self.port.notices), 1)


class BackendChangeTests(DispatchCase):
    def test_an_offered_notice_of_a_replaced_backend_is_offered_again_with_a_comment(self):
        rid = self.new()
        self.tick()
        first = self.port.notices[0]['delivery_id']
        self.port.backends[self.root] = '300:9'  # the backend restarted (its queue was memory)
        self.tick()
        self.assertEqual(len(self.port.notices), 2, 'P2-AC-12: offered again to the new backend')
        row = self.row(rid)
        self.assertEqual((row['state'], row['delivery_state'], row['delivery_backend']), ('NEW', 'offered', '300:9'))
        self.assertNotEqual(self.port.notices[1]['delivery_id'], first)
        self.assertEqual([kind for kind, _ in self.comments(rid)], ['system'])
        self.assertIn('backend가 다시 시작되어', self.comments(rid)[0][1])
        with self.assertRaises(Exception):
            self.service.record_delivery(rid, first, 'delivered')  # the old backend's result no longer counts
        self.assertEqual(self.deliver(rid)['state'], 'REVIEWING')

    def test_a_backend_turned_off_while_offered_keeps_the_offer_until_a_new_backend(self):
        rid = self.new()
        self.tick()
        self.port.backends[self.root] = None
        self.tick()
        self.assertEqual(self.row(rid)['delivery_state'], 'offered')
        self.port.backends[self.root] = '100:5'  # the same backend again (it was only not observable)
        self.tick()
        self.assertEqual(len(self.port.notices), 1)


class EditNoticeTests(DispatchCase):
    def linked(self, rid: str, status: str = 'running') -> None:
        self.tick()
        self.deliver(rid)
        token = self.service.link_check(self.manager, rid)['link_token']
        self.service.link_commit(self.manager, token, str(uuid.uuid4()), 1, status)

    def version(self, rid: str) -> int:
        return self.row(rid)['version']

    def test_a_revised_linked_request_is_noticed_even_in_manual_mode(self):
        for status, state in (('dispatched', 'READY'), ('running', 'RUNNING')):
            with self.subTest(state=state):
                self.service.review_mode(Principal.local(), self.a, 'auto')
                rid = self.new(state)
                self.linked(rid, status)
                self.assertEqual(self.row(rid)['state'], state)
                self.service.review_mode(Principal.local(), self.a, 'manual')
                self.service.update(OWNER, rid, self.version(rid), body='changed')
                self.tick()
                self.assertEqual((self.port.notices[-1]['request_id'], self.port.notices[-1]['event'],
                                  self.port.notices[-1]['fields']['state']), (rid, 'revised', state))
                self.deliver(rid)
                self.assertEqual(self.row(rid)['state'], state, 'P2-AC-29: the linked state is unchanged')

    def test_a_reviewing_edit_sends_updated_in_auto_mode_only(self):
        rid = self.new()
        self.tick()
        self.deliver(rid)
        self.service.update(OWNER, rid, self.version(rid), title='Better title')
        self.tick()
        self.assertEqual((self.port.notices[-1]['event'], self.port.notices[-1]['fields']['title']),
                         ('updated', 'Better title'))
        self.deliver(rid)
        self.service.review_mode(Principal.local(), self.a, 'manual')
        self.service.update(OWNER, rid, self.version(rid), title='Even better')
        self.tick()
        self.assertEqual(len(self.port.notices), 2, 'manual: the new round waits for 검토 시작')

    def test_a_cancelled_request_the_manager_knew_is_noticed_as_cancelled(self):
        self.service.review_mode(Principal.local(), self.a, 'manual')
        rid = self.new()
        self.service.start_review(OWNER, rid)
        self.tick()
        self.deliver(rid)
        self.service.cancel(OWNER, rid)
        self.tick()
        self.assertEqual((self.port.notices[-1]['event'], self.port.notices[-1]['fields']['state']),
                         ('cancelled', 'CLOSED'))
        silent = self.new('never told')
        self.service.cancel(OWNER, silent)
        self.tick()
        self.assertEqual(len(self.port.notices), 2, 'a NEW request the manager never heard of needs no notice')


if __name__ == '__main__':
    unittest.main()
