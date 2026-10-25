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
        sidecars = [Path(f'{self.db}{suffix}') for suffix in ('-wal', '-shm')]
        self.assertTrue(all(p.exists() for p in sidecars))  # the Hub keeps the WAL store open
        for path in (self.db, self.root / '.wb' / 'inbox.lock', *sidecars):
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


LOCK_HOLDER = r'''
import fcntl, os, sys
descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
sys.stdin.read()
'''

LOCK_PROBE = r'''
import fcntl, os, sys
descriptor = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("busy")
else:
    print("free")
'''


class LockTests(StoreCase):
    def holder(self) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, '-c', LOCK_HOLDER, str(self.root / '.wb' / 'inbox.lock')],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

        def stop():
            process.stdin.close()
            process.wait(10)
            process.stdout.close()
        self.addCleanup(stop)
        self.assertEqual(process.stdout.readline().strip(), 'locked')
        return process

    def probe(self) -> str:
        return subprocess.run([sys.executable, '-c', LOCK_PROBE, str(self.root / '.wb' / 'inbox.lock')],
                              capture_output=True, text=True, check=True, timeout=10).stdout.strip()

    def test_another_process_holding_inbox_lock_makes_the_inbox_busy_and_nothing_is_written(self):
        rid = self.service.submit(OWNER, self.project_id, 'before', 'b')['request_id']
        self.service.close_idle(force=True)
        before = self.db.read_bytes()
        self.holder()
        for call in (lambda: self.service.submit(OWNER, self.project_id, 'during', 'b'),
                     lambda: self.service.get(OWNER, rid),
                     lambda: self.service.review_mode(OWNER, self.project_id, set='manual')):
            with self.assertRaises(InboxError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'inbox_busy')
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(os.path.exists(f'{self.db}-wal'))

    def test_the_hub_holds_the_lock_while_open_and_releases_it_when_idle(self):
        clock = [1000.0]
        service = self.make_service(clock=lambda: clock[0], idle_seconds=60.0)
        self.service.close()
        service.submit(OWNER, self.project_id, 'held', 'b')
        self.assertEqual(self.probe(), 'busy')
        clock[0] += 59.0
        self.assertEqual(service.close_idle(), [])
        self.assertEqual(self.probe(), 'busy')
        clock[0] += 1.0
        self.assertEqual(service.close_idle(), [self.project_id])
        self.assertEqual(self.probe(), 'free')
        self.assertEqual(len(service.list_requests(OWNER)['requests']), 1)  # reopens on demand

    def test_service_close_releases_every_lock(self):
        self.service.submit(OWNER, self.project_id, 'held', 'b')
        self.assertEqual(self.probe(), 'busy')
        self.service.close()
        self.assertEqual(self.probe(), 'free')


class RefusalTests(StoreCase):
    def test_meta_project_id_mismatch_is_refused_and_the_file_is_not_changed(self):
        self.service.submit(OWNER, self.project_id, 'ours', 'b')
        self.service.close()
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE meta SET value = ? WHERE key = 'project_id'", (str(uuid.uuid4()),))
        connection.commit()
        connection.close()
        before = self.db.read_bytes()
        service = self.make_service()
        for call in (lambda: service.submit(OWNER, self.project_id, 'x', 'b'),
                     lambda: service.review_mode(OWNER, self.project_id)):
            with self.assertRaises(InboxError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'inbox_mismatch')
        self.assertEqual(self.db.read_bytes(), before)

    def test_a_damaged_file_is_refused_without_overwriting_and_other_projects_still_work(self):
        root_b, id_b = make_project(self.base, 'beta')
        garbage = b'this is not a database' * 100
        descriptor = os.open(self.db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, garbage)
        os.close(descriptor)
        self.service.set_projects([ProjectRef(self.project_id, self.root, 'alpha'), ProjectRef(id_b, root_b, 'beta')])
        with self.assertRaises(InboxError) as caught:
            self.service.submit(OWNER, self.project_id, 'x', 'b')
        self.assertEqual(caught.exception.code, 'inbox_unavailable')
        self.assertEqual(self.db.read_bytes(), garbage)
        self.assertIsNone(self.service.pending_requests(self.project_id))
        self.service.submit(OWNER, id_b, 'fine', 'b')
        listed = self.service.list_requests(OWNER)
        self.assertEqual([r['title'] for r in listed['requests']], ['fine'])
        self.assertEqual(listed['unavailable'], [{'project_id': self.project_id, 'code': 'inbox_unavailable'}])

    def test_a_symlinked_data_dir_is_refused_and_nothing_is_created(self):
        real = self.base / 'elsewhere'
        os.rename(self.root / '.wb', real)
        os.symlink(real, self.root / '.wb')
        service = self.make_service()
        self.service.close()
        with self.assertRaises(InboxError) as caught:
            service.submit(OWNER, self.project_id, 'x', 'b')
        self.assertEqual(caught.exception.code, 'project_unavailable')
        self.assertEqual(sorted(os.listdir(real)), ['project.json'])

    def test_a_registry_mismatch_makes_the_project_unavailable(self):
        self.service.set_projects([ProjectRef(self.project_id, self.root, 'alpha', unavailable='mismatch')])
        with self.assertRaises(InboxError) as caught:
            self.service.submit(OWNER, self.project_id, 'x', 'b')
        self.assertEqual(caught.exception.code, 'project_unavailable')
        self.assertFalse(self.db.exists())


class ReviewModeTests(StoreCase):
    """D-32 / P2-AC-40: ``meta.review_mode`` in ``.wb/inbox.sqlite3`` survives a new store and
    unregistering/re-registering the project."""

    def test_review_mode_is_stored_in_the_project_inbox_and_persists(self):
        events = []
        service = self.make_service(on_committed=events.append)
        self.service.close()
        self.assertEqual(service.review_mode(Principal.local(), self.project_id, set='manual'),
                         {'review_mode': 'manual'})
        self.assertEqual([(e.type, e.detail) for e in events],
                         [('review_mode_changed', {'review_mode': 'manual', 'previous': 'auto'})])
        self.assertEqual(self.rows("SELECT value FROM meta WHERE key = 'review_mode'"), [('manual',)])
        service.close()
        again = self.make_service()
        self.assertEqual(again.review_mode(OWNER, self.project_id), {'review_mode': 'manual'})
        again.set_projects([])
        with self.assertRaises(InboxError):
            again.review_mode(OWNER, self.project_id)
        again.set_projects([ProjectRef(self.project_id, self.root, 'alpha')])
        self.assertEqual(again.review_mode(OWNER, self.project_id), {'review_mode': 'manual'})
        with self.assertRaises(InboxError) as caught:
            again.review_mode(OWNER, self.project_id, set='sometimes')
        self.assertEqual(caught.exception.code, 'invalid_input')


class ListingTests(StoreCase):
    """P2-AC-32: no automatic deletion, CLOSED hidden by default, filters, search, badge counts."""

    def setUp(self):
        super().setUp()
        self.ids = {}
        for title, body in (('Train model', 'GPU sweep'), ('Plot loss', 'matplotlib 그래프'),
                            ('Fix data', 'dedupe rows'), ('Old idea', 'drop it')):
            self.ids[title] = self.service.submit(OWNER, self.project_id, title, body)['request_id']
        self.service.cancel(OWNER, self.ids['Old idea'])
        manager = Principal.manager(self.project_id)
        self.service.review(manager, self.ids['Fix data'], 'ask', body='which table?')

    def titles(self, **kwargs) -> list[str]:
        return [r['title'] for r in self.service.list_requests(OWNER, **kwargs)['requests']]

    def test_closed_is_hidden_by_default_and_reachable_by_filter(self):
        self.assertEqual(self.titles(), ['Fix data', 'Plot loss', 'Train model'])
        self.assertEqual(self.titles(include_closed=True), ['Old idea', 'Fix data', 'Plot loss', 'Train model'])
        self.assertEqual(self.titles(states=['CLOSED']), ['Old idea'])
        self.assertEqual(self.titles(states=['NEW', 'NEEDS_INFO']), ['Fix data', 'Plot loss', 'Train model'])
        summary = self.service.list_requests(OWNER, states=['CLOSED'])['requests'][0]
        self.assertEqual(set(summary), {'request_id', 'project_id', 'seq', 'title', 'state', 'outcome', 'origin',
                                        'created_at', 'updated_at', 'task_id', 'untrusted_text'})  # fix 1
        self.assertEqual((summary['seq'], summary['outcome']), (4, 'cancelled'))
        self.assertEqual([n for n in dir(InboxService) if any(w in n for w in ('delete', 'purge', 'prune'))], [])

    def test_search_paging_and_input_checks(self):
        self.assertEqual(self.titles(query='gpu'), ['Train model'])
        self.assertEqual(self.titles(query='그래프'), ['Plot loss'])
        self.assertEqual(self.titles(query='drop', include_closed=True), ['Old idea'])
        self.assertEqual(self.titles(query='%'), [])
        first = self.service.list_requests(OWNER, limit=2)
        self.assertEqual([r['title'] for r in first['requests']], ['Fix data', 'Plot loss'])
        second = self.service.list_requests(OWNER, limit=2, cursor=first['next_cursor'])
        self.assertEqual(([r['title'] for r in second['requests']], second['next_cursor']), (['Train model'], None))
        for kwargs in ({'query': 'q' * 201}, {'limit': 0}, {'limit': 101}, {'states': ['DONE']},
                       {'cursor': 'garbage'}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InboxError) as caught:
                    self.service.list_requests(OWNER, **kwargs)
                self.assertEqual(caught.exception.code, 'invalid_input')

    def test_badge_and_pending_requests_counts(self):
        self.assertEqual(self.service.badge(OWNER), {'pending': 2, 'needs_info': 1})
        requester = Principal.requester('k9req0000', 'ext', None)
        self.assertEqual(self.service.badge(requester), {'pending': 0, 'needs_info': 0})
        self.service.submit(requester, self.project_id, 'mine', 'b')
        self.assertEqual(self.service.badge(requester), {'pending': 1, 'needs_info': 0})
        self.assertEqual(self.service.pending_requests(self.project_id),
                         {'new': 3, 'reviewing': 0, 'needs_info': 1, 'ready': 0, 'running': 0})
        self.assertIsNone(self.service.pending_requests(str(uuid.uuid4())))


SQLITE_WRITER = r'''
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("BEGIN IMMEDIATE")
print("writing", flush=True)
sys.stdin.read()
'''


class ReviewFollowUpTests(LockTests):
    """TI-04 review follow-ups (items 3-6)."""

    def test_a_busy_inbox_reads_as_not_found_for_everyone_but_the_owner(self):
        rid = self.service.submit(OWNER, self.project_id, 'hidden', 'b')['request_id']
        self.service.close_idle(force=True)
        self.holder()
        missing = None
        for principal in (Principal.requester('k5req0000', 'r', None), Principal.editor('k5ed00000', 'e', None),
                          Principal.manager(self.project_id)):
            with self.subTest(principal=principal.kind):
                with self.assertRaises(InboxError) as hidden:
                    self.service.get(principal, rid)
                with self.assertRaises(InboxError) as absent:
                    self.service.get(principal, str(uuid.uuid4()))
                self.assertEqual(hidden.exception.to_json(), absent.exception.to_json())
                missing = hidden.exception.code
        self.assertEqual(missing, 'not_found')
        with self.assertRaises(InboxError) as caught:
            self.service.get(OWNER, rid)
        self.assertEqual(caught.exception.code, 'inbox_busy')

    def test_format_and_separator_characters_are_refused_in_titles(self):
        for char in ('‮', '​', '⁦', '﻿', ' ', ' '):
            with self.subTest(char=hex(ord(char))):
                with self.assertRaises(InboxError) as caught:
                    self.service.submit(OWNER, self.project_id, f'a{char}b', 'body')
                self.assertEqual(caught.exception.code, 'invalid_input')
        self.assertFalse(self.db.exists())

    def test_listing_skips_unavailable_projects_like_the_badge_and_orders_by_seq(self):
        clock = [5000.0]
        service = self.make_service(clock=lambda: clock[0])
        self.service.close()
        for title in ('first', 'second', 'third'):
            service.submit(OWNER, self.project_id, title, 'b')
            clock[0] -= 100.0  # a wall clock stepping backwards must not reorder requests
        self.assertEqual([r['title'] for r in service.list_requests(OWNER)['requests']],
                         ['third', 'second', 'first'])
        service.set_projects([ProjectRef(self.project_id, self.root, 'alpha', unavailable='mismatch')])
        self.assertEqual(service.list_requests(OWNER), {'requests': [], 'next_cursor': None})
        self.assertEqual(service.badge(OWNER), {'pending': 0, 'needs_info': 0})

    def test_observe_task_inputs_are_bounded_and_offers_of_closed_requests_are_cancel_notices(self):
        manager = Principal.manager(self.project_id)
        rid = self.service.submit(OWNER, self.project_id, 't', 'b')['request_id']
        token = self.service.link_check(manager, rid)['link_token']
        self.service.link_commit(manager, token, 'task-1', 1, 'running')
        for kwargs in ({'task_id': 't' * 129, 'status': 'running'},
                       {'task_id': 'task-1', 'status': 'closed', 'closed_reason': 'r' * 129},
                       {'task_id': 'task-1', 'status': 'running', 'view': {'log': 'x' * (256 * 1024)}},
                       {'task_id': 'task-1', 'status': 's' * 129}):
            with self.subTest(keys=sorted(kwargs)):
                with self.assertRaises(InboxError) as caught:
                    self.service.observe_task(rid, kwargs.pop('task_id'), kwargs.pop('status'), **kwargs)
                self.assertEqual(caught.exception.code, 'invalid_input')
        closed = self.service.submit(OWNER, self.project_id, 'c', 'b')['request_id']
        self.service.mark_offered(closed, 'd-0', '1:1')
        self.service.record_delivery(closed, 'd-0', 'delivered')
        self.service.cancel(OWNER, closed)  # manager knew it: a cancel notice is pending
        with self.assertRaises(InboxError) as caught:
            self.service.mark_offered(closed, 'd-1', '1:1', event='new')
        self.assertEqual(caught.exception.code, 'invalid_transition')
        with self.assertRaises(InboxError):
            self.service.mark_offered(closed, 'd-1', '1:1')
        self.assertEqual(self.service.mark_offered(closed, 'd-1', '1:1', event='cancelled')['state'], 'CLOSED')

    def test_an_sqlite_write_lock_held_elsewhere_is_busy_not_corrupt(self):
        self.service.submit(OWNER, self.project_id, 'seed', 'b')
        self.service.close_idle(force=True)
        writer = subprocess.Popen([sys.executable, '-c', SQLITE_WRITER, str(self.db)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)

        def stop():
            writer.stdin.close()
            writer.wait(10)
            writer.stdout.close()
        self.addCleanup(stop)
        self.assertEqual(writer.stdout.readline().strip(), 'writing')
        with self.assertRaises(InboxError) as caught:
            self.service.submit(OWNER, self.project_id, 'blocked', 'b')
        self.assertEqual(caught.exception.code, 'inbox_busy')

    def test_review_mode_takes_mode_and_still_accepts_set(self):
        self.assertEqual(self.service.review_mode(OWNER, self.project_id, 'manual'), {'review_mode': 'manual'})
        self.assertEqual(self.service.review_mode(OWNER, self.project_id, mode='auto'), {'review_mode': 'auto'})
        self.assertEqual(self.service.review_mode(OWNER, self.project_id, set='manual'), {'review_mode': 'manual'})


class BackupTests(StoreCase):
    """TI-T08: after the Hub store is closed, ``update_backup.snapshot`` holds the committed inbox."""

    def test_snapshot_contains_the_committed_inbox(self):
        from workbench.backend.update_backup import snapshot
        rid = self.service.submit(OWNER, self.project_id, 'backed up', 'body')['request_id']
        self.service.review_mode(OWNER, self.project_id, set='manual')
        self.service.close()
        target = self.base / 'backup'
        result = snapshot(self.root / '.wb', target)
        self.assertIn('inbox.sqlite3', {entry['path'] for entry in result['files']})
        copy = target / 'inbox.sqlite3'
        self.assertEqual(self.rows('SELECT request_id, title FROM requests', db=copy), [(rid, 'backed up')])
        self.assertEqual(self.rows("SELECT value FROM meta WHERE key = 'review_mode'", db=copy), [('manual',)])


if __name__ == '__main__':
    unittest.main()
