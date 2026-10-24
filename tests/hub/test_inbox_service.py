"""InboxService rules (TI-04, seam T1): permissions, transitions, edits, cancel, ordering.

Expected values come from the SPEC tables ("Principal과 권한", "요청 상태 기계", 편집 규칙,
"취소"), the inputs the test chose and the rows the test reads back with stdlib ``sqlite3``.
Run with ``discover -s tests/hub -p 'test_inbox_service*.py'``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.hub.inbox.service import InboxError, InboxService, Principal, ProjectRef  # noqa: E402

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
        json.dump({'schema_version': 1, 'project_id': project_id,
                   'project_root': str(root), 'state': 'completed'}, stream)
    return root, project_id


class ServiceCase(unittest.TestCase):
    backend_up = True

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-inbox-svc-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root, self.a = make_project(self.base, 'alpha')
        self.root_b, self.b = make_project(self.base, 'beta')
        self.events = []
        self.service = InboxService(on_committed=self.events.append,
                                    backend_running=lambda project_id: self.backend_up)
        self.addCleanup(self.service.close)
        self.service.set_projects([ProjectRef(self.a, self.root, 'alpha'), ProjectRef(self.b, self.root_b, 'beta')])
        self.board = Principal.board_agent('k0board000')
        self.editor = Principal.editor('k0editor00', 'ed', [self.a])
        self.req1 = Principal.requester('k0req00001', 'r1', [self.a])
        self.req2 = Principal.requester('k0req00002', 'r2', [self.a])
        self.manager = Principal.manager(self.a)
        self.manager_b = Principal.manager(self.b)
        self.local = Principal.local()

    def rows(self, sql: str, *args, root: Path | None = None):
        db = (root or self.root) / '.wb' / 'inbox.sqlite3'
        connection = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def new(self, principal: Principal | None = None, title: str = 'Title', body: str = 'Body',
            project: str | None = None) -> str:
        return self.service.submit(principal or self.req1, project or self.a, title, body, None)['request_id']

    def state(self, request_id: str) -> tuple:
        return self.rows('SELECT state, outcome, version FROM requests WHERE request_id = ?', request_id)[0]

    def code(self, call, *args, **kwargs) -> InboxError:
        with self.assertRaises(InboxError) as caught:
            call(*args, **kwargs)
        return caught.exception

    # state helpers through the real API -----------------------------------------------------
    def to_reviewing(self, rid: str) -> None:
        self.service.review(self.manager, rid, 'begin_review')

    def to_needs_info(self, rid: str) -> None:
        self.service.review(self.manager, rid, 'ask', body='which dataset?')

    def to_ready(self, rid: str, task_status: str = 'dispatched') -> dict:
        token = self.service.link_check(self.manager, rid)['link_token']
        return self.service.link_commit(self.manager, token, 'task-1', 1, task_status)

    def to_running(self, rid: str) -> dict:
        return self.to_ready(rid, 'running')

    def version(self, rid: str) -> int:
        return self.state(rid)[2]

    def make(self, state: str) -> str:
        rid = self.new(self.req1)
        if state in ('REVIEWING', 'NEEDS_INFO'):
            self.to_reviewing(rid)
        if state == 'NEEDS_INFO':
            self.to_needs_info(rid)
        if state == 'READY':
            self.to_ready(rid)
        if state == 'RUNNING':
            self.to_running(rid)
        if state == 'CLOSED':
            self.service.cancel(OWNER, rid)
        self.assertEqual(self.state(rid)[0], state)
        return rid

    def snapshot(self, rid: str) -> tuple:
        return (self.rows('SELECT * FROM requests WHERE request_id = ?', rid),
                self.rows('SELECT count(*) FROM comments'), self.rows('SELECT count(*) FROM events'),
                self.rows('SELECT * FROM links'), self.rows('SELECT * FROM request_revisions'))


class PermissionMatrixTests(ServiceCase):
    """SPEC "Principal과 권한": hidden requests are ``not_found``, visible-but-denied ``forbidden``."""

    def test_reads_follow_visibility_and_scope(self):
        mine = self.new(self.req1)
        owners = self.new(OWNER)
        other_project = self.new(OWNER, project=self.b)
        cases = [
            (OWNER, owners, None), (self.board, owners, None), (self.editor, owners, None),
            (self.manager, owners, None), (self.req1, mine, None),
            (self.req1, owners, 'not_found'), (self.req2, mine, 'not_found'),
            (self.manager_b, owners, 'not_found'), (self.editor, other_project, 'not_found'),
            (self.req1, other_project, 'not_found'), (self.manager, other_project, 'not_found'),
            (OWNER, other_project, None), (self.local, owners, 'forbidden'),
        ]
        for principal, rid, expected in cases:
            with self.subTest(principal=principal.kind, key=principal.key_id, rid=rid):
                if expected is None:
                    self.assertEqual(self.service.get(principal, rid)['request_id'], rid)
                else:
                    self.assertEqual(self.code(self.service.get, principal, rid).code, expected)

    def test_a_hidden_request_answers_exactly_like_a_missing_one(self):
        mine = self.new(self.req1)
        hidden = self.code(self.service.get, self.req2, mine).to_json()
        missing = self.code(self.service.get, self.req2, str(uuid.uuid4())).to_json()
        self.assertEqual(hidden, missing)
        self.assertNotIn('state', hidden)
        for op in (lambda rid: self.service.comment(self.req2, rid, 'x'),
                   lambda rid: self.service.cancel(self.req2, rid)):
            self.assertEqual(self.code(op, mine).to_json(), self.code(op, str(uuid.uuid4())).to_json())

    def test_lists_show_only_what_the_principal_may_see(self):
        mine, theirs, owners = self.new(self.req1), self.new(self.req2), self.new(OWNER)
        in_b = self.new(OWNER, project=self.b)
        ids = lambda p: {r['request_id'] for r in self.service.list_requests(p)['requests']}  # noqa: E731
        self.assertEqual(ids(self.req1), {mine})
        self.assertEqual(ids(self.req2), {theirs})
        self.assertEqual(ids(self.editor), {mine, theirs, owners})
        self.assertEqual(ids(self.manager), {mine, theirs, owners})
        self.assertEqual(ids(self.manager_b), {in_b})
        self.assertEqual(ids(OWNER), {mine, theirs, owners, in_b})
        self.assertEqual(ids(self.board), {mine, theirs, owners, in_b})

    def test_submit_scope_and_principal_kind(self):
        self.assertEqual(self.code(self.service.submit, self.editor, self.b, 't', 'b').code, 'not_found')
        self.assertEqual(self.code(self.service.submit, self.req1, self.b, 't', 'b').code, 'not_found')
        self.assertEqual(self.code(self.service.submit, self.manager, self.a, 't', 'b').code, 'forbidden')
        self.assertEqual(self.code(self.service.submit, self.local, self.a, 't', 'b').code, 'forbidden')
        self.assertFalse((self.root_b / '.wb' / 'inbox.sqlite3').exists())

    def test_operation_matrix_on_a_requester_owned_new_request(self):
        ops = {
            'edit': lambda p, rid: self.service.update(p, rid, 1, title='changed'),
            'comment': lambda p, rid: self.service.comment(p, rid, 'note'),
            'set_state': lambda p, rid: self.service.set_state(p, rid, 1, 'NEEDS_INFO'),
            'cancel': lambda p, rid: self.service.cancel(p, rid),
            'start_review': lambda p, rid: self.service.start_review(p, rid),
            'begin_review': lambda p, rid: self.service.review(p, rid, 'begin_review'),
            'link_check': lambda p, rid: self.service.link_check(p, rid),
        }
        everyone = {'owner': OWNER, 'board': self.board, 'editor': self.editor, 'req1': self.req1,
                    'req2': self.req2, 'manager': self.manager, 'manager_b': self.manager_b, 'local': self.local}
        allowed = {
            'edit': {'owner', 'board', 'editor'}, 'comment': {'owner', 'board', 'editor', 'req1'},
            'set_state': {'owner', 'board', 'editor'}, 'cancel': {'owner', 'board', 'editor', 'req1'},
            'start_review': {'owner'}, 'begin_review': {'manager'}, 'link_check': {'manager'},
        }
        hidden = {'req2', 'manager_b'}
        for op, call in ops.items():
            for name, principal in everyone.items():
                with self.subTest(op=op, principal=name):
                    rid = self.new(self.req1)
                    before = self.rows('SELECT * FROM requests WHERE request_id = ?', rid)
                    if name in allowed[op]:
                        call(principal, rid)
                        continue
                    error = self.code(call, principal, rid)
                    self.assertEqual(error.code, 'not_found' if name in hidden else 'forbidden')
                    self.assertEqual(self.rows('SELECT * FROM requests WHERE request_id = ?', rid), before)
                    if error.code == 'forbidden' and name != 'local':
                        self.assertEqual((error.state, error.version), ('NEW', 1))

    def test_review_mode_may_be_set_only_by_owner_and_local(self):
        for principal, expected in ((self.board, 'forbidden'), (self.editor, 'forbidden'),
                                    (self.req1, 'forbidden'), (self.manager, 'forbidden')):
            with self.subTest(principal=principal.kind):
                self.assertEqual(self.code(self.service.review_mode, principal, self.a, set='manual').code, expected)
        self.assertEqual(self.service.review_mode(OWNER, self.a, set='manual'), {'review_mode': 'manual'})
        self.assertEqual(self.service.review_mode(self.local, self.a, set='auto'), {'review_mode': 'auto'})
        self.assertEqual(self.code(self.service.review_mode, self.req1, self.a).code, 'forbidden')


class TransitionTableTests(ServiceCase):
    """SPEC "요청 상태 기계": every (operation, current state) pair; a refusal writes nothing."""

    def link(self, rid: str, status: str) -> dict:
        token = self.service.link_check(self.manager, rid)['link_token']
        return self.service.link_commit(self.manager, token, 'task-9', 2, status)

    def test_every_operation_from_every_state(self):
        ops = {
            'begin_review': lambda rid: self.service.review(self.manager, rid, 'begin_review'),
            'ask': lambda rid: self.service.review(self.manager, rid, 'ask', body='q?'),
            'reject': lambda rid: self.service.review(self.manager, rid, 'reject', body='no'),
            'close': lambda rid: self.service.review(self.manager, rid, 'close', outcome='success', summary='done'),
            'progress': lambda rid: self.service.review(self.manager, rid, 'comment', body='working'),
            'link_ready': lambda rid: self.link(rid, 'dispatched'),
            'link_running': lambda rid: self.link(rid, 'running'),
            'to_needs_info': lambda rid: self.service.set_state(self.editor, rid, self.version(rid), 'NEEDS_INFO'),
            'to_new': lambda rid: self.service.set_state(self.editor, rid, self.version(rid), 'NEW'),
            'cancel': lambda rid: self.service.cancel(self.editor, rid),
            'comment': lambda rid: self.service.comment(self.req1, rid, 'more detail'),
            'edit': lambda rid: self.service.update(self.editor, rid, self.version(rid), body='edited'),
        }
        X = 'invalid_transition'
        expected = {  # state after the operation as (state, outcome), or the refusal code
            'begin_review': {'NEW': ('REVIEWING', None), 'REVIEWING': X, 'NEEDS_INFO': X, 'READY': X, 'RUNNING': X,
                             'CLOSED': X},
            'ask': {'NEW': ('NEEDS_INFO', None), 'REVIEWING': ('NEEDS_INFO', None), 'NEEDS_INFO': X, 'READY': X,
                    'RUNNING': ('NEEDS_INFO', None), 'CLOSED': X},
            'reject': {'NEW': ('CLOSED', 'rejected'), 'REVIEWING': ('CLOSED', 'rejected'),
                       'NEEDS_INFO': ('CLOSED', 'rejected'), 'READY': X, 'RUNNING': X, 'CLOSED': X},
            'close': {'NEW': X, 'REVIEWING': X, 'NEEDS_INFO': X, 'READY': ('CLOSED', 'success'),
                      'RUNNING': ('CLOSED', 'success'), 'CLOSED': X},
            'progress': {'NEW': ('REVIEWING', None), 'REVIEWING': ('REVIEWING', None),
                         'NEEDS_INFO': ('NEEDS_INFO', None), 'READY': ('READY', None),
                         'RUNNING': ('RUNNING', None), 'CLOSED': X},
            'link_ready': {'NEW': ('READY', None), 'REVIEWING': ('READY', None), 'NEEDS_INFO': ('READY', None),
                           'READY': X, 'RUNNING': X, 'CLOSED': X},
            'link_running': {'NEW': ('RUNNING', None), 'REVIEWING': ('RUNNING', None),
                             'NEEDS_INFO': ('RUNNING', None), 'READY': X, 'RUNNING': X, 'CLOSED': X},
            'to_needs_info': {'NEW': ('NEEDS_INFO', None), 'REVIEWING': X, 'NEEDS_INFO': X, 'READY': X,
                              'RUNNING': X, 'CLOSED': X},
            'to_new': {'NEW': X, 'REVIEWING': X, 'NEEDS_INFO': ('NEW', None), 'READY': X, 'RUNNING': X,
                       'CLOSED': ('NEW', None)},
            'cancel': {'NEW': ('CLOSED', 'cancelled'), 'REVIEWING': ('CLOSED', 'cancelled'),
                       'NEEDS_INFO': ('CLOSED', 'cancelled'), 'READY': ('READY', None), 'RUNNING': ('RUNNING', None),
                       'CLOSED': X},
            'comment': {'NEW': ('NEW', None), 'REVIEWING': ('REVIEWING', None), 'NEEDS_INFO': ('REVIEWING', None),
                        'READY': ('READY', None), 'RUNNING': ('RUNNING', None), 'CLOSED': ('CLOSED', 'cancelled')},
            'edit': {'NEW': ('NEW', None), 'REVIEWING': ('REVIEWING', None), 'NEEDS_INFO': ('NEEDS_INFO', None),
                     'READY': ('READY', None), 'RUNNING': ('RUNNING', None), 'CLOSED': X},
        }
        for op, call in ops.items():
            for state, outcome in expected[op].items():
                with self.subTest(op=op, state=state):
                    rid = self.make(state)
                    before = self.snapshot(rid)
                    version = self.version(rid)
                    if outcome == X:
                        error = self.code(call, rid)
                        self.assertEqual(error.code, X)
                        self.assertEqual((error.state, error.version), (state, version))
                        self.assertEqual(self.snapshot(rid), before)
                    else:
                        call(rid)
                        self.assertEqual(self.state(rid)[:2], outcome)
                        self.assertGreater(self.version(rid), version)

    def test_cancel_of_a_linked_request_only_marks_cancel_state(self):
        for state in ('READY', 'RUNNING'):
            with self.subTest(state=state):
                rid = self.make(state)
                result = self.service.cancel(OWNER, rid)
                self.assertEqual((result['status'], result['state']), ('cancel_requested', state))
                self.assertEqual(self.rows('SELECT cancel_state, outcome FROM requests WHERE request_id = ?', rid),
                                 [('requested', None)])
                self.assertEqual(self.code(self.service.cancel, OWNER, rid).code, 'invalid_transition')

    def test_hub_transitions(self):
        delivered = self.make('NEW')
        self.service.mark_offered(delivered, 'd-1', '100:5')
        self.assertEqual(self.service.record_delivery(delivered, 'd-1', 'delivered')['state'], 'REVIEWING')
        self.assertEqual(self.rows('SELECT delivery_state FROM requests WHERE request_id = ?', delivered),
                         [('delivered',)])
        ready = self.make('READY')
        self.assertEqual(self.service.observe_task(ready, 'task-1', 'running')['state'], 'RUNNING')
        voided = self.make('READY')
        self.assertEqual(self.service.observe_task(voided, 'task-1', 'closed',
                                                   closed_reason='inbox_link_unconfirmed')['state'], 'REVIEWING')
        self.assertEqual(self.rows('SELECT state FROM links WHERE request_id = ?', voided), [('void',)])
        cancelled = self.make('RUNNING')
        self.service.cancel(OWNER, cancelled)
        self.service.observe_task(cancelled, 'task-1', 'closed', closed_reason='user', user_cancelled=True)
        self.assertEqual(self.state(cancelled)[:2], ('CLOSED', 'cancelled'))
        finished = self.make('RUNNING')
        before = len([e for e in self.events if e.type == 'linked_task_closed'])
        for _ in range(2):
            self.service.observe_task(finished, 'task-1', 'closed', closed_reason='manager')
        self.assertEqual(self.state(finished)[:2], ('RUNNING', None))  # P2-AC-09: no success by Task end alone
        self.assertEqual(len([e for e in self.events if e.type == 'linked_task_closed']), before + 1)


class EditRuleTests(ServiceCase):
    """SPEC 편집 규칙 (D-19, orchestrator 지시) and ``base_version`` conflicts (P2-AC-29)."""

    def revisions(self, rid: str):
        return self.rows('SELECT revision, title, body FROM request_revisions WHERE request_id = ? ORDER BY revision',
                         rid)

    def round(self, rid: str):
        return self.rows('SELECT review_round, delivery_state FROM requests WHERE request_id = ?', rid)[0]

    def test_new_and_needs_info_edit_in_place_keeping_the_previous_text_in_events(self):
        for state in ('NEW', 'NEEDS_INFO'):
            with self.subTest(state=state):
                rid = self.make(state)
                round_before = self.round(rid)
                result = self.service.update(self.editor, rid, self.version(rid), title='New title', body='New body')
                self.assertEqual((result['mode'], result['revision'], result['state']), ('in_place', 1, state))
                self.assertEqual(self.revisions(rid), [(1, 'New title', 'New body')])
                self.assertEqual(self.round(rid), round_before)
                detail = json.loads(self.rows("SELECT detail FROM events WHERE request_id = ? AND type = 'edited'",
                                              rid)[0][0])
                self.assertEqual(detail['previous'], {'title': 'Title', 'body': 'Body', 'revision': 1})

    def test_reviewing_edit_is_in_place_and_starts_a_new_review_round(self):
        rid = self.make('REVIEWING')
        rounds = self.round(rid)[0]
        result = self.service.update(OWNER, rid, self.version(rid), body='Clarified')
        self.assertEqual((result['mode'], result['revision'], result['state']), ('in_place', 1, 'REVIEWING'))
        self.assertEqual(self.revisions(rid), [(1, 'Title', 'Clarified')])
        self.assertEqual(self.round(rid), (rounds + 1, 'pending'))
        self.assertEqual(self.events[-1].type, 'updated')

    def test_ready_and_running_edits_make_a_new_revision_and_keep_the_task(self):
        for state in ('READY', 'RUNNING'):
            with self.subTest(state=state):
                rid = self.make(state)
                task = self.rows('SELECT task_id, spec_revision FROM requests WHERE request_id = ?', rid)
                result = self.service.update(self.board, rid, self.version(rid), body='Also plot it')
                self.assertEqual((result['mode'], result['revision'], result['state']), ('new_revision', 2, state))
                self.assertEqual(self.revisions(rid), [(1, 'Title', 'Body'), (2, 'Title', 'Also plot it')])
                self.assertEqual(self.rows('SELECT task_id, spec_revision FROM requests WHERE request_id = ?', rid),
                                 task)
                self.assertEqual(self.events[-1].type, 'revised')
                self.assertEqual(self.service.get(OWNER, rid)['revision'], 2)

    def test_a_stale_base_version_is_a_conflict_and_writes_nothing(self):
        rid = self.make('NEW')
        stale = self.version(rid)
        self.service.comment(OWNER, rid, 'bump')
        before = self.snapshot(rid)
        for call in (lambda: self.service.update(self.editor, rid, stale, body='late'),
                     lambda: self.service.set_state(self.editor, rid, stale, 'NEEDS_INFO')):
            error = self.code(call)
            self.assertEqual((error.code, error.state, error.version), ('conflict', 'NEW', stale + 1))
        self.assertEqual(self.snapshot(rid), before)


class CommentAndAnswerTests(ServiceCase):
    """D-18: a non-manager comment on NEEDS_INFO is the answer → REVIEWING with a new review round."""

    def comments(self, rid: str):
        return self.rows('SELECT seq, author_kind, kind, body FROM comments WHERE request_id = ? ORDER BY seq', rid)

    def test_owner_board_agent_and_original_requester_answers_return_to_reviewing(self):
        for principal in (OWNER, self.board, self.req1):
            with self.subTest(principal=principal.kind):
                rid = self.make('NEEDS_INFO')
                rounds = self.rows('SELECT review_round FROM requests WHERE request_id = ?', rid)[0][0]
                result = self.service.comment(principal, rid, 'Use dataset B')
                self.assertEqual(result['state'], 'REVIEWING')
                self.assertEqual(self.comments(rid), [(1, 'manager', 'question', 'which dataset?'),
                                                      (2, principal.kind, 'answer', 'Use dataset B')])
                self.assertEqual(self.rows('SELECT review_round, delivery_state FROM requests WHERE request_id = ?',
                                           rid), [(rounds + 1, 'pending')])
                self.assertEqual(self.events[-1].type, 'answered')
        rid = self.make('NEEDS_INFO')
        self.assertEqual(self.code(self.service.comment, self.req2, rid, 'not mine').code, 'not_found')

    def test_comment_outside_needs_info_keeps_the_state(self):
        rid = self.make('REVIEWING')
        self.service.comment(OWNER, rid, 'fyi')
        self.service.review(self.manager, rid, 'comment', body='looking at it')
        self.assertEqual(self.state(rid)[0], 'REVIEWING')
        self.assertEqual([c[2] for c in self.comments(rid)], ['comment', 'progress'])
        shown = self.service.get(self.req1, rid)['comments']
        self.assertEqual([(c['kind'], c['body']) for c in shown], [('comment', 'fyi'), ('progress', 'looking at it')])

    def test_comment_limits(self):
        rid = self.make('REVIEWING')
        self.service.comment(OWNER, rid, 'x' * 16384)
        self.service.comment(OWNER, rid, '한' * 5461 + 'a')  # 16384 bytes
        before = self.snapshot(rid)
        for body, code in (('x' * 16385, 'too_large'), ('한' * 5461 + 'ab', 'too_large'), ('', 'invalid_input')):
            with self.subTest(size=len(body.encode())):
                self.assertEqual(self.code(self.service.comment, OWNER, rid, body).code, code)
                self.assertEqual(self.code(self.service.review, self.manager, rid, 'ask', body=body).code, code)
        self.assertEqual(self.code(self.service.review, self.manager, rid, 'close', outcome='success',
                                   summary='s' * 16385).code, 'too_large')
        self.assertEqual(self.snapshot(rid), before)


class CancelTests(ServiceCase):
    """SPEC "취소": before dispatch at once; READY/RUNNING mark ``cancel_state`` and hand the backend
    call to the ``on_committed`` port; the backend must be running before anything is committed."""

    def test_pre_dispatch_cancel_closes_and_says_whether_the_manager_must_hear(self):
        quiet = self.make('NEW')
        told = self.make('REVIEWING')
        self.assertEqual(self.service.cancel(self.req1, quiet, reason='not needed')['status'], 'cancelled')
        self.assertEqual(self.events[-1].detail['notify_manager'], False)
        self.service.cancel(self.req1, told)
        self.assertEqual(self.events[-1].type, 'cancelled')
        self.assertEqual(self.events[-1].detail['notify_manager'], True)
        self.assertEqual(self.rows('SELECT request_id, state, outcome, delivery_state FROM requests '
                                   'WHERE request_id IN (?, ?) ORDER BY seq', quiet, told),
                         [(quiet, 'CLOSED', 'cancelled', 'none'), (told, 'CLOSED', 'cancelled', 'pending')])

    def test_linked_cancel_needs_a_running_backend_and_emits_the_port_event(self):
        for state, status in (('READY', 'dispatched'), ('RUNNING', 'running')):
            with self.subTest(state=state):
                rid = self.make(state)
                before = self.snapshot(rid)
                self.backend_up = False
                error = self.code(self.service.cancel, OWNER, rid)
                self.assertEqual((error.code, error.state), ('backend_not_running', state))
                self.assertEqual(self.snapshot(rid), before)
                self.backend_up = True
                count = len(self.events)
                result = self.service.cancel(self.req1, rid, reason='stop')
                self.assertEqual(result, {'status': 'cancel_requested', 'state': state,
                                          'version': before[0][0][4] + 1, 'task_id': 'task-1'})
                emitted = self.events[count:]
                self.assertEqual([e.type for e in emitted], ['cancel_requested'])
                self.assertEqual((emitted[0].request_id, emitted[0].project_id), (rid, self.a))
                self.assertEqual(emitted[0].detail, {'task_id': 'task-1', 'reason': 'stop',
                                                     'expect': {'status': status}})
                self.assertEqual(self.state(rid)[:2], (state, None))

    def test_a_failing_notification_does_not_undo_the_commit(self):
        def boom(event):
            raise RuntimeError('backend unreachable')
        service = InboxService(on_committed=boom)
        self.addCleanup(service.close)
        self.service.close()
        service.set_projects([ProjectRef(self.a, self.root, 'alpha')])
        rid = service.submit(OWNER, self.a, 'kept', 'body')['request_id']
        self.assertEqual(service.cancel(OWNER, rid)['state'], 'CLOSED')
        self.assertEqual(self.state(rid)[:2], ('CLOSED', 'cancelled'))


class ReopenAndLinkHistoryTests(ServiceCase):
    def links(self, rid: str):
        return self.rows('SELECT task_id, spec_revision, state FROM links WHERE request_id = ? ORDER BY rowid', rid)

    def test_reopen_moves_the_current_link_to_history_and_clears_it(self):
        rid = self.make('RUNNING')
        self.service.review(self.manager, rid, 'close', outcome='failed', summary='OOM on GPU 0')
        self.assertEqual(self.rows('SELECT state, outcome, result_summary FROM requests WHERE request_id = ?', rid),
                         [('CLOSED', 'failed', 'OOM on GPU 0')])
        result = self.service.set_state(self.editor, rid, self.version(rid), 'NEW', reason='retry with less memory')
        self.assertEqual(result['state'], 'NEW')
        self.assertEqual(self.rows('SELECT state, outcome, task_id, spec_revision, task_view, cancel_state, '
                                   'result_summary, delivery_state FROM requests WHERE request_id = ?', rid),
                         [('NEW', None, None, None, None, None, None, 'pending')])
        self.assertEqual(self.links(rid), [('task-1', 1, 'closed')])  # history keeps how the link ended
        self.assertEqual(self.events[-1].type, 'reopened')
        token = self.service.link_check(self.manager, rid)['link_token']
        self.service.link_commit(self.manager, token, 'task-2', 1, 'dispatched')
        self.assertEqual(self.links(rid), [('task-1', 1, 'closed'), ('task-2', 1, 'active')])

    def test_reopen_after_reject_of_a_linked_request_keeps_the_closed_link_in_history(self):
        # Orchestrator decision (review of TI-04): reject closes an active link, so no CLOSED request
        # keeps an active one; reopen only clears the request's current Task fields.
        rid = self.make('RUNNING')
        self.service.review(self.manager, rid, 'ask', body='continue?')
        self.service.review(self.manager, rid, 'reject', body='dropping it')
        self.assertEqual(self.links(rid), [('task-1', 1, 'closed')])
        self.service.set_state(OWNER, rid, self.version(rid), 'NEW')
        self.assertEqual(self.links(rid), [('task-1', 1, 'closed')])
        self.assertEqual(self.rows('SELECT task_id FROM requests WHERE request_id = ?', rid), [(None,)])

    def test_question_while_running_keeps_the_link_and_a_follow_up_spec_relinks(self):
        rid = self.make('RUNNING')
        self.service.review(self.manager, rid, 'ask', body='retry limit reached; continue?')
        self.assertEqual(self.rows('SELECT state, task_id FROM requests WHERE request_id = ?', rid),
                         [('NEEDS_INFO', 'task-1')])
        self.service.comment(self.req1, rid, 'yes, continue')
        token = self.service.link_check(self.manager, rid)['link_token']
        self.assertEqual(self.service.link_commit(self.manager, token, 'task-1', 2, 'running')['state'], 'RUNNING')
        self.assertEqual(self.links(rid), [('task-1', 1, 'superseded'), ('task-1', 2, 'active')])
        self.assertEqual(self.service.get(OWNER, rid)['task'], {'task_id': 'task-1', 'spec_revision': 2,
                                                                 'view': {'status': 'running'}})


class SerializationTests(ServiceCase):
    """D-35 / P2-AC-43: one executor per project applies changes in arrival order; a change made
    invalid by an earlier one is refused with the reason and writes nothing."""

    def outcome(self, future):
        try:
            return future.result(timeout=10)
        except InboxError as exc:
            return exc

    def test_cancel_before_link_commit_wins_and_later_changes_are_refused(self):
        rid = self.make('REVIEWING')
        token = self.service.link_check(self.manager, rid)['link_token']
        version = self.version(rid)
        futures = [self.service.cancel(OWNER, rid, wait=False),
                   self.service.link_commit(self.manager, token, 'task-7', 1, 'dispatched', wait=False),
                   self.service.update(self.editor, rid, version, body='late edit', wait=False),
                   self.service.review(self.manager, rid, 'ask', body='late question', wait=False)]
        cancel, link, edit, ask = (self.outcome(f) for f in futures)
        self.assertEqual(cancel['status'], 'cancelled')
        self.assertEqual((link.code, link.state), ('invalid_transition', 'CLOSED'))
        self.assertEqual((edit.code, edit.state, edit.version), ('conflict', 'CLOSED', version + 1))
        self.assertEqual((ask.code, ask.state), ('invalid_transition', 'CLOSED'))
        self.assertEqual(self.state(rid), ('CLOSED', 'cancelled', version + 1))
        self.assertEqual(self.rows('SELECT count(*) FROM links'), [(0,)])

    def test_link_commit_before_cancel_makes_the_cancel_a_linked_cancel(self):
        rid = self.make('REVIEWING')
        token = self.service.link_check(self.manager, rid)['link_token']
        futures = [self.service.link_commit(self.manager, token, 'task-7', 1, 'dispatched', wait=False),
                   self.service.cancel(OWNER, rid, wait=False)]
        link, cancel = (self.outcome(f) for f in futures)
        self.assertEqual(link['state'], 'READY')
        self.assertEqual(cancel['status'], 'cancel_requested')
        self.assertEqual(self.rows('SELECT state, cancel_state FROM requests WHERE request_id = ?', rid),
                         [('READY', 'requested')])

    def test_concurrent_threads_apply_in_submission_order_without_lost_updates(self):
        rid = self.make('REVIEWING')
        start_version = self.version(rid)
        order_lock = threading.Lock()
        order: list[tuple[str, object]] = []
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            barrier.wait()
            for step in range(10):
                with order_lock:  # the submission order is recorded atomically with the enqueue
                    if index == 3 and step == 5:
                        order.append(('cancel', self.service.cancel(self.board, rid, wait=False)))
                    elif index == 5 and step == 2:
                        order.append(('edit', self.service.update(self.editor, rid, start_version, body='x',
                                                                  wait=False)))
                    else:
                        body = f'{index}-{step}'
                        order.append((body, self.service.comment(OWNER, rid, body, wait=False)))
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        results = [(name, self.outcome(future)) for name, future in order]
        accepted = [(name, r) for name, r in results if not isinstance(r, InboxError)]
        versions = [r['version'] for _, r in accepted]
        self.assertEqual(versions, list(range(start_version + 1, start_version + 1 + len(accepted))))
        comments = [name for name, r in accepted if name not in ('cancel', 'edit')]
        self.assertEqual([row[0] for row in self.rows(
            'SELECT body FROM comments WHERE request_id = ? AND kind = ? ORDER BY seq', rid, 'comment')], comments)
        cancel = dict(results)['cancel']
        self.assertEqual(cancel['status'], 'cancelled')
        edit = dict(results)['edit']
        self.assertIsInstance(edit, InboxError)  # its base_version was stale by the time it ran
        self.assertEqual(edit.code, 'conflict')
        self.assertEqual(self.state(rid), ('CLOSED', 'cancelled', start_version + len(accepted)))
        self.assertEqual(self.rows('SELECT count(*) FROM events WHERE request_id = ? AND to_state = ? '
                                   "AND from_state != 'CLOSED'",
                                   rid, 'CLOSED'), [(1,)])


class ShutdownRaceTests(unittest.TestCase):
    """Work scheduled on a project being removed or closed is refused as ``InboxError``, never a raw
    ``RuntimeError``, and nothing reopens a store after it was closed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-inbox-race-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.root_a, self.a = make_project(base, 'alpha')
        self.root_b, self.b = make_project(base, 'beta')
        self.entered, self.release = threading.Event(), threading.Event()
        self.block = False

        def on_committed(event):  # runs on the project's executor thread
            if self.block and event.project_id == self.a:
                self.entered.set()
                self.release.wait(10)
        self.service = InboxService(on_committed=on_committed)
        self.addCleanup(self.release.set)
        self.addCleanup(self.service.close)
        self.refs = [ProjectRef(self.a, self.root_a, 'alpha'), ProjectRef(self.b, self.root_b, 'beta')]
        self.service.set_projects(self.refs)
        self.service.submit(OWNER, self.a, 'in A', 'b')
        self.service.submit(OWNER, self.b, 'in B', 'b')

    def test_a_project_removed_while_a_listing_waits_is_reported_unavailable(self):
        self.block = True
        blocker = threading.Thread(target=lambda: self.service.submit(OWNER, self.a, 'blocks A', 'b'))
        blocker.start()
        self.assertTrue(self.entered.wait(10))
        outcome = {}

        def listing():
            try:
                outcome['value'] = self.service.list_requests(OWNER)
            except BaseException as exc:  # noqa: BLE001 - the test records whatever escapes
                outcome['value'] = exc
        lister = threading.Thread(target=listing)
        lister.start()
        threading.Event().wait(0.3)  # the listing has taken its project snapshot and waits on A
        self.service.set_projects(self.refs[:1])  # B is removed and its executor shut down
        self.release.set()
        lister.join(10)
        blocker.join(10)
        value = outcome['value']
        self.assertIsInstance(value, dict, repr(value))
        self.assertEqual({r['title'] for r in value['requests']}, {'in A', 'blocks A'})
        self.assertEqual(value['unavailable'], [{'project_id': self.b, 'code': 'project_unavailable'}])

    def test_calls_racing_close_fail_as_inbox_errors_and_the_lock_is_released(self):
        errors, stop = [], threading.Event()

        def hammer():
            while not stop.is_set():
                for call in (lambda: self.service.submit(OWNER, self.a, 't', 'b'),
                             lambda: self.service.list_requests(OWNER),
                             lambda: self.service.close_idle(force=True)):
                    try:
                        call()
                    except BaseException as exc:  # noqa: BLE001
                        errors.append(exc)
        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for thread in threads:
            thread.start()
        threading.Event().wait(0.2)
        self.service.close()
        threading.Event().wait(0.1)
        stop.set()
        for thread in threads:
            thread.join(10)
        bad = [e for e in errors if not isinstance(e, InboxError)
               or e.code not in ('hub_stopping', 'not_found', 'project_unavailable')]
        self.assertEqual(bad, [])
        for root in (self.root_a, self.root_b):
            descriptor = os.open(root / '.wb' / 'inbox.lock', os.O_RDWR)
            try:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if a store was reopened
            finally:
                os.close(descriptor)


class LinkedTaskCancelTests(ServiceCase):
    """Orchestrator decision: a NEEDS_INFO/REVIEWING request that still links a live Task (asked while
    RUNNING) is cancelled through the linked path; a manager reject closes the active link."""

    def asked_while_running(self) -> str:
        rid = self.make('RUNNING')
        self.service.review(self.manager, rid, 'ask', body='continue?')
        return rid

    def test_cancel_with_a_linked_task_marks_cancel_state_instead_of_closing(self):
        for answered in (False, True):
            with self.subTest(state='REVIEWING' if answered else 'NEEDS_INFO'):
                rid = self.asked_while_running()
                if answered:
                    self.service.comment(OWNER, rid, 'yes')
                state = self.state(rid)[0]
                count = len(self.events)
                result = self.service.cancel(self.req1, rid)
                self.assertEqual((result['status'], result['state'], result['task_id']),
                                 ('cancel_requested', state, 'task-1'))
                self.assertEqual(self.rows('SELECT state, outcome, cancel_state, task_id FROM requests '
                                           'WHERE request_id = ?', rid), [(state, None, 'requested', 'task-1')])
                self.assertEqual([e.type for e in self.events[count:]], ['cancel_requested'])
                self.assertEqual(self.rows('SELECT state FROM links WHERE request_id = ?', rid), [('active',)])
                self.service.observe_task(rid, 'task-1', 'closed', closed_reason='user', user_cancelled=True)
                self.assertEqual(self.state(rid)[:2], ('CLOSED', 'cancelled'))

    def test_cancel_with_a_linked_task_needs_the_backend(self):
        rid = self.asked_while_running()
        self.backend_up = False
        before = self.snapshot(rid)
        self.assertEqual(self.code(self.service.cancel, OWNER, rid).code, 'backend_not_running')
        self.assertEqual(self.snapshot(rid), before)

    def test_reject_closes_the_active_link(self):
        rid = self.asked_while_running()
        self.service.review(self.manager, rid, 'reject', body='dropping it')
        self.assertEqual(self.rows('SELECT state FROM links WHERE request_id = ?', rid), [('closed',)])
        self.assertEqual(self.state(rid)[:2], ('CLOSED', 'rejected'))


if __name__ == '__main__':
    unittest.main()
