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


if __name__ == '__main__':
    unittest.main()
