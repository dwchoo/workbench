"""Inbox store (TI-04, seam T1): ``workbench.hub.inbox`` against a temp project ``.wb``.

Expected values come from SPEC "요청 저장소" (schema, modes, pragmas), the test's own inputs,
and the bytes the test reads back with stdlib ``sqlite3``/``os``. Lock contention uses a real
subprocess that holds ``inbox.lock``. Run with ``discover -s tests/hub -p 'test_inbox_store*.py'``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.hub.inbox.service import InboxError, InboxService, Principal, ProjectRef  # noqa: E402

OWNER = Principal.owner()


def make_project(base: Path, name: str) -> tuple[Path, str]:
    """A completed project marker exactly as ``initialize_project`` publishes it."""
    root = (base / name)
    root.mkdir(parents=True)
    root = root.resolve()
    data = root / '.wb'
    data.mkdir(mode=0o700)
    os.chmod(data, 0o700)
    project_id = str(uuid.uuid4())
    marker = data / 'project.json'
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump({'schema_version': 1, 'project_id': project_id,
                   'project_root': str(root), 'state': 'completed'}, stream)
    return root, project_id


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-inbox-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root, self.project_id = make_project(self.base, 'alpha')
        self.service = self.make_service()

    def make_service(self, **kwargs) -> InboxService:
        service = InboxService(**kwargs)
        self.addCleanup(service.close)
        service.set_projects([ProjectRef(self.project_id, self.root, 'alpha')])
        return service

    @property
    def db(self) -> Path:
        return self.root / '.wb' / 'inbox.sqlite3'

    def rows(self, sql: str, *args, db: Path | None = None):
        connection = sqlite3.connect(f'file:{db or self.db}?mode=ro', uri=True)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()


class MissingFileTests(StoreCase):
    def test_reads_of_a_missing_inbox_are_empty_and_create_nothing(self):
        before = sorted(os.listdir(self.root / '.wb'))
        self.assertEqual(self.service.list_requests(OWNER)['requests'], [])
        self.assertEqual(self.service.review_mode(OWNER, self.project_id), {'review_mode': 'auto'})
        self.assertEqual(self.service.badge(OWNER), {'pending': 0, 'needs_info': 0})
        self.assertEqual(self.service.pending_requests(self.project_id),
                         {'new': 0, 'reviewing': 0, 'needs_info': 0, 'ready': 0, 'running': 0})
        with self.assertRaises(InboxError) as caught:
            self.service.get(OWNER, str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, 'not_found')
        self.assertEqual(sorted(os.listdir(self.root / '.wb')), before)


class SubmitTests(StoreCase):
    TABLES = {'meta', 'requests', 'request_revisions', 'comments', 'links', 'events'}

    def test_first_submit_creates_private_wal_store_with_new_request(self):
        result = self.service.submit(OWNER, self.project_id, 'Train model', 'Run the sweep', 'key-1')
        self.assertEqual(result['state'], 'NEW')
        self.assertEqual(result['seq'], 1)
        self.assertTrue(result['created'])
        self.assertEqual(str(uuid.UUID(result['request_id'], version=4)), result['request_id'])
        self.assertEqual(result['origin']['kind'], 'owner')
        for path in (self.db, self.root / '.wb' / 'inbox.lock'):
            info = os.lstat(path)
            self.assertTrue(stat.S_ISREG(info.st_mode), path)
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600, path)
        tables = {row[0] for row in self.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, self.TABLES)
        self.assertEqual(self.rows('PRAGMA journal_mode'), [('wal',)])
        meta = dict(self.rows('SELECT key, value FROM meta'))
        self.assertEqual(meta['schema_version'], '1')
        self.assertEqual(meta['project_id'], self.project_id)
        self.assertEqual(meta['review_mode'], 'auto')
        row = self.rows('SELECT request_id, seq, state, origin_kind, origin_key_id, title, body, revision, '
                        'version, idem_key, review_round, delivery_state, outcome FROM requests')
        self.assertEqual(row, [(result['request_id'], 1, 'NEW', 'owner', None, 'Train model', 'Run the sweep',
                                1, 1, 'key-1', 1, 'pending', None)])
        self.assertEqual(self.rows('SELECT revision, title, body FROM request_revisions'),
                         [(1, 'Train model', 'Run the sweep')])

    def test_seq_follows_arrival_and_origin_records_the_api_key(self):
        editor = Principal.editor('k1editor0', 'codex', None)
        first = self.service.submit(OWNER, self.project_id, 'one', 'b', None)
        second = self.service.submit(editor, self.project_id, 'two', 'b', None)
        board = self.service.submit(Principal.board_agent('k2board00'), self.project_id, 'three', 'b', None)
        self.assertEqual([first['seq'], second['seq'], board['seq']], [1, 2, 3])
        self.assertEqual(second['origin'], {'kind': 'api_key', 'key_id': 'k1editor0', 'name': 'codex',
                                            'label': 'key:codex'})
        self.assertEqual(board['origin']['label'], 'Board agent')
        self.assertEqual(first['origin']['label'], '소유자')
        self.assertEqual(self.rows('SELECT seq, origin_kind, origin_key_id, origin_name FROM requests ORDER BY seq'),
                         [(1, 'owner', None, None), (2, 'api_key', 'k1editor0', 'codex'),
                          (3, 'board_agent', 'k2board00', None)])
        shown = self.service.get(OWNER, second['request_id'])
        self.assertEqual((shown['title'], shown['body'], shown['state'], shown['version']), ('two', 'b', 'NEW', 1))

    def test_projects_are_separate_files_and_survive_a_new_service(self):
        root_b, id_b = make_project(self.base, 'beta')
        self.service.set_projects([ProjectRef(self.project_id, self.root, 'alpha'), ProjectRef(id_b, root_b, 'beta')])
        a = self.service.submit(OWNER, self.project_id, 'for A', 'a', None)
        b = self.service.submit(OWNER, id_b, 'for B', 'b', None)
        self.service.close()
        self.assertEqual(self.rows('SELECT request_id FROM requests'), [(a['request_id'],)])
        self.assertEqual(self.rows('SELECT request_id FROM requests', db=root_b / '.wb' / 'inbox.sqlite3'),
                         [(b['request_id'],)])
        again = InboxService()
        self.addCleanup(again.close)
        again.set_projects([ProjectRef(self.project_id, self.root, 'alpha'), ProjectRef(id_b, root_b, 'beta')])
        self.assertEqual(again.get(OWNER, b['request_id'])['title'], 'for B')
        listed = again.list_requests(OWNER)['requests']
        self.assertEqual({(r['project_id'], r['title']) for r in listed}, {(self.project_id, 'for A'), (id_b, 'for B')})


class IdempotencyTests(StoreCase):
    def snapshot(self) -> bytes:
        self.service.close_idle(force=True)
        return self.db.read_bytes()

    def test_same_origin_key_and_content_returns_the_existing_request(self):
        for principal in (OWNER, Principal.requester('k1req0000', 'ext', None)):
            with self.subTest(principal=principal.kind):
                first = self.service.submit(principal, self.project_id, 'Same', 'body', 'idem-1')
                again = self.service.submit(principal, self.project_id, 'Same', 'body', 'idem-1')
                self.assertTrue(first['created'])
                self.assertFalse(again['created'])
                self.assertEqual((again['request_id'], again['seq']), (first['request_id'], first['seq']))
        self.assertEqual(self.rows('SELECT count(*) FROM requests'), [(2,)])

    def test_same_key_with_different_content_is_a_conflict_and_writes_nothing(self):
        first = self.service.submit(OWNER, self.project_id, 'Same', 'body', 'idem-1')
        before = self.snapshot()
        for title, body in (('Same', 'other body'), ('Other', 'body')):
            with self.assertRaises(InboxError) as caught:
                self.service.submit(OWNER, self.project_id, title, body, 'idem-1')
            self.assertEqual(caught.exception.code, 'idempotency_conflict')
            self.assertEqual((caught.exception.state, caught.exception.version), ('NEW', 1))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.rows('SELECT request_id FROM requests'), [(first['request_id'],)])

    def test_the_same_key_from_another_origin_is_a_separate_request(self):
        a = self.service.submit(Principal.requester('k1aaaaaaa', 'a', None), self.project_id, 'T', 'b', 'shared')
        b = self.service.submit(Principal.requester('k1bbbbbbb', 'b', None), self.project_id, 'T', 'b', 'shared')
        c = self.service.submit(OWNER, self.project_id, 'T', 'b', 'shared')
        d = self.service.submit(Principal.board_agent('k1aaaaaaa'), self.project_id, 'T', 'b', 'shared')
        self.assertEqual(len({a['request_id'], b['request_id'], c['request_id'], d['request_id']}), 4)
        self.assertTrue(all(r['created'] for r in (a, b, c, d)))


class LimitTests(StoreCase):
    """SPEC "입력 한도": title ≤ 200 code points, body ≤ 64 KiB UTF-8, rejected before any write."""

    def test_rejected_input_creates_no_file(self):
        with self.assertRaises(InboxError) as caught:
            self.service.submit(OWNER, self.project_id, 'x' * 201, 'body', None)
        self.assertEqual(caught.exception.code, 'too_large')
        self.assertFalse(self.db.exists())
        self.assertFalse((self.root / '.wb' / 'inbox.lock').exists())

    def test_limits_at_and_over_the_boundary(self):
        self.service.submit(OWNER, self.project_id, 'seed', 'seed', None)
        self.service.close_idle(force=True)
        before = self.db.read_bytes()
        cases = [
            ('title 201', 'x' * 201, 'b', None, 'too_large'),
            ('title 201 multibyte', '한' * 201, 'b', None, 'too_large'),
            ('title empty', '', 'b', None, 'invalid_input'),
            ('title control', 'a\nb', 'b', None, 'invalid_input'),
            ('title DEL', 'a\x7fb', 'b', None, 'invalid_input'),
            ('title tab', 'a\tb', 'b', None, 'invalid_input'),
            ('body 64KiB+1', 't', 'x' * (65536 + 1), None, 'too_large'),
            ('body multibyte 64KiB+1', 't', '한' * 21845 + 'ab', None, 'too_large'),
            ('body empty', 't', '', None, 'invalid_input'),
            ('body surrogate', 't', 'a\ud800', None, 'invalid_input'),
            ('idem too long', 't', 'b', 'k' * 129, 'invalid_input'),
            ('idem bad chars', 't', 'b', 'a b', 'invalid_input'),
            ('idem empty', 't', 'b', '', 'invalid_input'),
        ]
        for name, title, body, idem, code in cases:
            with self.subTest(name):
                with self.assertRaises(InboxError) as caught:
                    self.service.submit(OWNER, self.project_id, title, body, idem)
                self.assertEqual(caught.exception.code, code)
        self.service.close_idle(force=True)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.rows('SELECT count(*) FROM requests'), [(1,)])

    def test_values_at_the_limit_are_accepted(self):
        accepted = [('x' * 200, 'b'), ('한' * 200, 'b'), ('t', 'x' * 65536), ('t', '한' * 21845 + 'a'),
                    ('plain title', 'line\nbreaks\tand tabs')]
        for index, (title, body) in enumerate(accepted):
            with self.subTest(index=index):
                result = self.service.submit(OWNER, self.project_id, title, body, f'k{index}:{"z" * 100}')
                self.assertEqual(self.rows('SELECT title, body FROM requests WHERE request_id = ?',
                                           result['request_id']), [(title, body)])


if __name__ == '__main__':
    unittest.main()
