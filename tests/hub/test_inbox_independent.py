"""Independent verification of the inbox store and serial writer (TI-04, seam T1).

Expected values come from the task-inbox SPEC ("Principal과 권한", "요청 저장소", "요청 상태 기계",
"취소", "입력 한도"), BRIEF D-13..D-20 / D-31..D-36 / P2-AC-02/03/09/11/19..24/42..44, ADR-0005, and
rows the tests read back with stdlib ``sqlite3``. The transition matrix is written from the SPEC
table, not from the implementation. Run with
``discover -s tests/hub -p 'test_inbox_independent.py'``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.hub.inbox.service import InboxError, InboxService, Principal, ProjectRef  # noqa: E402

OWNER = Principal.owner()
TABLES = ('meta', 'requests', 'request_revisions', 'comments', 'links', 'events')
CHILD_ENV: dict[str, str] = {}


def setUpModule():
    runtime = tempfile.mkdtemp(prefix='x', dir='/dev/shm' if os.path.isdir('/dev/shm') else None)
    os.chmod(runtime, 0o700)
    saved = {key: os.environ.get(key) for key in ('XDG_RUNTIME_DIR', 'WORKBENCH_HUB_AUTOSTART')}
    os.environ['XDG_RUNTIME_DIR'] = runtime
    os.environ['WORKBENCH_HUB_AUTOSTART'] = '0'

    def restore():
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(runtime, ignore_errors=True)
    unittest.addModuleCleanup(restore)
    CHILD_ENV.update(os.environ, PYTHONPATH=str(REPO / 'src'), PYTHONDONTWRITEBYTECODE='1')


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


LOCK_HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print('locked', flush=True)
sys.stdin.read()
"""
LOCK_PROBE = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print('free')
except OSError:
    print('busy')
"""
# argv: root project_id op [arg]; prints one JSON line.
CHILD_SERVICE = """
import json, os, sys
from pathlib import Path
from workbench.hub.inbox.service import InboxError, InboxService, Principal, ProjectRef
root, project_id, op = sys.argv[1], sys.argv[2], sys.argv[3]
service = InboxService()
service.set_projects([ProjectRef(project_id, Path(root), 'child')])
try:
    if op == 'submit':
        out = service.submit(Principal.owner(), project_id, 'from child', 'body', None)
    elif op == 'cancel':
        out = service.cancel(Principal.owner(), sys.argv[4])
    elif op == 'submit_hold':
        out = service.submit(Principal.owner(), project_id, 'held by child', 'body', None)
        print(json.dumps({'ok': out}), flush=True)
        sys.stdin.read()
        sys.exit(0)
    elif op == 'submit_die':
        service.submit(Principal.owner(), project_id, 'committed by child', 'body', None)
        os._exit(7)
    print(json.dumps({'ok': out}))
except InboxError as exc:
    print(json.dumps({'error': exc.code}))
service.close()
"""
# argv: root project_id mode. Crashes in the middle of a write transaction.
CHILD_CRASH = """
import os, signal, sys
from pathlib import Path
from workbench.hub.inbox.store import InboxStore
root, project_id, mode = sys.argv[1], sys.argv[2], sys.argv[3]
store = InboxStore(Path(root), project_id)
INSERT = ("INSERT INTO requests (request_id, seq, created_at, updated_at, version, origin_kind, content_hash, "
          "title, body, revision, state, review_round, delivery_state) "
          "VALUES (?, ?, 1.0, 1.0, 1, 'owner', 'h', 't', 'b', 1, 'NEW', 1, 'pending')")
def half(connection):
    connection.execute(INSERT, ('11111111-1111-4111-8111-111111111111', 1))
    connection.execute("INSERT INTO events (request_id, at, actor_kind, type) VALUES (?, 1.0, 'owner', 'submitted')",
                       ('11111111-1111-4111-8111-111111111111',))
    os.kill(os.getpid(), signal.SIGKILL)
if mode == 'second':
    store.write(lambda c: c.execute(INSERT, ('00000000-0000-4000-8000-000000000000', 1)))
    store.write(lambda c: c.execute(INSERT.replace("VALUES (?, ?", "VALUES (?, 2 + 0*?"),
                                    ('22222222-2222-4222-8222-222222222222', 0)))
    store.write(lambda c: (c.execute("UPDATE requests SET title = 'half'"), os.kill(os.getpid(), signal.SIGKILL)))
store.write(half)
"""


def run_threads(functions, timeout: float = 60.0) -> list:
    """Start every callable at once; return ``('ok', value)`` or ``('err', exception)`` in input order."""
    barrier = threading.Barrier(len(functions))
    results: list = [None] * len(functions)

    def runner(index, fn):
        try:
            barrier.wait(30)
            results[index] = ('ok', fn())
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            results[index] = ('err', exc)
    threads = [threading.Thread(target=runner, args=(i, fn), daemon=True) for i, fn in enumerate(functions)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
        if thread.is_alive():
            raise AssertionError('a worker thread did not finish (deadlock?)')
    return results


class Base(unittest.TestCase):
    backend_up = True
    clock_value = 1000.0

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-inbox-ind-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root, self.a = make_project(self.base, 'alpha')
        self.root_b, self.b = make_project(self.base, 'beta')
        self.now = [self.clock_value]
        self.events: list = []
        self.service = self.make_service()
        self.board = Principal.board_agent('k0board000')
        self.editor = Principal.editor('k0editor00', 'ed', [self.a])
        self.editor_out = Principal.editor('k0editor01', 'ed-out', [self.b])
        self.req1 = Principal.requester('k0req00001', 'r1', [self.a])
        self.req2 = Principal.requester('k0req00002', 'r2', [self.a])
        self.manager = Principal.manager(self.a)
        self.manager_b = Principal.manager(self.b)
        self.local = Principal.local()

    def make_service(self, **kwargs) -> InboxService:
        kwargs.setdefault('on_committed', self.events.append)
        kwargs.setdefault('backend_running', lambda project_id: self.backend_up)
        kwargs.setdefault('clock', lambda: self.now[0])
        service = InboxService(**kwargs)
        self.addCleanup(service.close)
        service.set_projects([ProjectRef(self.a, self.root, 'alpha'), ProjectRef(self.b, self.root_b, 'beta')])
        return service

    # direct stdlib reads --------------------------------------------------------------------
    def db_path(self, root: Path | None = None) -> Path:
        return (root or self.root) / '.wb' / 'inbox.sqlite3'

    def rows(self, sql: str, *args, root: Path | None = None) -> list:
        connection = sqlite3.connect(f'file:{self.db_path(root)}?mode=ro', uri=True)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def dump(self, root: Path | None = None) -> dict:
        connection = sqlite3.connect(f'file:{self.db_path(root)}?mode=ro', uri=True)
        try:
            return {table: connection.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                    for table in TABLES}
        finally:
            connection.close()

    def file_state(self, root: Path | None = None) -> dict:
        path = self.db_path(root)
        out = {}
        for suffix in ('', '-wal'):
            candidate = Path(f'{path}{suffix}')
            out[suffix] = candidate.read_bytes() if candidate.exists() else None
        return out

    def row(self, rid: str) -> dict:
        connection = sqlite3.connect(f'file:{self.db_path()}?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return dict(connection.execute('SELECT * FROM requests WHERE request_id = ?', (rid,)).fetchone())
        finally:
            connection.close()

    def version(self, rid: str) -> int:
        return self.row(rid)['version']

    def error(self, call, *args, **kwargs) -> InboxError:
        with self.assertRaises(InboxError) as caught:
            call(*args, **kwargs)
        return caught.exception

    # building requests through the real API -------------------------------------------------
    def new(self, principal: Principal | None = None, title: str = 'Title', body: str = 'Body',
            project: str | None = None, key: str | None = None) -> str:
        return self.service.submit(principal or self.req1, project or self.a, title, body, key)['request_id']

    def to_reviewing(self, rid):
        self.service.review(self.manager, rid, 'begin_review')

    def to_needs_info(self, rid):
        self.service.review(self.manager, rid, 'ask', body='which dataset?')

    def link(self, rid: str, task_status: str = 'dispatched', task_id: str = 'task-1') -> dict:
        token = self.service.link_check(self.manager, rid)['link_token']
        return self.service.link_commit(self.manager, token, task_id, 1, task_status)

    def make(self, state: str, principal: Principal | None = None) -> str:
        rid = self.new(principal or self.req1)
        if state in ('REVIEWING', 'NEEDS_INFO'):
            self.to_reviewing(rid)
        if state == 'NEEDS_INFO':
            self.to_needs_info(rid)
        if state == 'READY':
            self.link(rid)
        if state == 'RUNNING':
            self.link(rid, 'running')
        if state == 'CLOSED':
            self.service.cancel(OWNER, rid)
        self.assertEqual(self.row(rid)['state'], state)
        return rid

    def spawn(self, code: str, *args: str, stdin: bool = False) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, '-c', code, *args], env=CHILD_ENV, text=True,
                                   stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def reap():
            if process.poll() is None:
                process.kill()
            process.wait(10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        self.addCleanup(reap)
        return process

    def child(self, op: str, *extra: str, root: Path | None = None, project: str | None = None) -> dict:
        process = self.spawn(CHILD_SERVICE, str(root or self.root), project or self.a, op, *extra)
        out, err = process.communicate(timeout=60)
        self.assertTrue(out.strip(), f'child produced no result: rc={process.returncode} {err}')
        return json.loads(out.strip().splitlines()[-1])

    def lock_holder(self, root: Path | None = None) -> subprocess.Popen:
        process = self.spawn(LOCK_HOLDER, str((root or self.root) / '.wb' / 'inbox.lock'), stdin=True)
        self.assertEqual(process.stdout.readline().strip(), 'locked')
        return process

    def lock_free(self) -> bool:
        out = subprocess.run([sys.executable, '-c', LOCK_PROBE, str(self.root / '.wb' / 'inbox.lock')],
                             capture_output=True, text=True, check=True, timeout=30, env=CHILD_ENV).stdout
        return out.strip() == 'free'


# =====================================================================================
# SPEC "요청 상태 기계" x "Principal과 권한": every allowed and forbidden transition
# =====================================================================================
STATES = ('NEW', 'REVIEWING', 'NEEDS_INFO', 'READY', 'RUNNING', 'CLOSED')
PRE = ('NEW', 'REVIEWING', 'NEEDS_INFO')
INVALID = ('invalid_transition',)

# action -> principals the SPEC permission table lets perform it (once the request is visible)
ALLOWED = {
    'edit': {'owner', 'board', 'editor'},
    'comment': {'owner', 'board', 'editor', 'req_own'},
    'to_needs_info': {'owner', 'board', 'editor'},
    'to_new': {'owner', 'board', 'editor'},
    'cancel': {'owner', 'board', 'editor', 'req_own'},
    'start_review': {'owner'},
    'begin_review': {'manager'}, 'ask': {'manager'}, 'm_comment': {'manager'},
    'reject': {'manager'}, 'close': {'manager'}, 'link_check': {'manager'},
}
HIDDEN = {'req_other', 'editor_out', 'manager_b'}  # cannot see request A-r1: not_found


def expected(state: str, action: str):
    """SPEC state-machine table for an allowed principal: None (unspecified, skip), INVALID, or
    ``(new_state, outcome)``."""
    if action == 'edit':
        return INVALID if state == 'CLOSED' else (state, None)
    if action == 'comment':
        return None if state == 'CLOSED' else ('REVIEWING' if state == 'NEEDS_INFO' else state, None)
    if action == 'to_needs_info':
        return ('NEEDS_INFO', None) if state == 'NEW' else INVALID
    if action == 'to_new':
        return ('NEW', None) if state in ('NEEDS_INFO', 'CLOSED') else INVALID
    if action == 'cancel':
        if state in PRE:
            return ('CLOSED', 'cancelled')
        return INVALID if state == 'CLOSED' else (state, None)  # READY/RUNNING: cancel_state=requested
    if action == 'start_review':
        return None if state == 'REVIEWING' else ((state, None) if state == 'NEW' else INVALID)
    if action == 'begin_review':
        return ('REVIEWING', None) if state == 'NEW' else INVALID
    if action == 'ask':
        return ('NEEDS_INFO', None) if state in ('NEW', 'REVIEWING', 'RUNNING') else INVALID
    if action == 'm_comment':
        if state == 'CLOSED':
            return None
        return ('REVIEWING' if state == 'NEW' else state, None)
    if action == 'reject':
        return ('CLOSED', 'rejected') if state in PRE else INVALID
    if action == 'close':
        return ('CLOSED', 'success') if state in ('READY', 'RUNNING') else INVALID
    if action == 'link_check':
        return ('REVIEWING' if state == 'NEW' else state, None) if state in PRE else INVALID
    raise AssertionError(action)


class TransitionMatrixTests(Base):
    def principals(self) -> dict[str, Principal]:
        return {'owner': OWNER, 'board': self.board, 'editor': self.editor, 'req_own': self.req1,
                'req_other': self.req2, 'manager': self.manager, 'local': self.local,
                'editor_out': self.editor_out, 'manager_b': self.manager_b}

    def perform(self, principal: Principal, action: str, rid: str):
        service, version = self.service, self.version(rid)
        return {
            'edit': lambda: service.update(principal, rid, version, title='Edited title'),
            'comment': lambda: service.comment(principal, rid, 'a comment'),
            'to_needs_info': lambda: service.set_state(principal, rid, version, 'NEEDS_INFO'),
            'to_new': lambda: service.set_state(principal, rid, version, 'NEW'),
            'cancel': lambda: service.cancel(principal, rid),
            'start_review': lambda: service.start_review(principal, rid),
            'begin_review': lambda: service.review(principal, rid, 'begin_review'),
            'ask': lambda: service.review(principal, rid, 'ask', body='question?'),
            'm_comment': lambda: service.review(principal, rid, 'comment', body='progress'),
            'reject': lambda: service.review(principal, rid, 'reject', body='no'),
            'close': lambda: service.review(principal, rid, 'close', outcome='success', summary='done'),
            'link_check': lambda: service.link_check(principal, rid),
        }[action]()

    def test_every_state_principal_action_cell_matches_the_spec_table(self):
        cells = refused = applied = 0
        for state in STATES:
            for pname, principal in self.principals().items():
                for action in ALLOWED:
                    with self.subTest(state=state, principal=pname, action=action):
                        rid = self.make(state)
                        before_dump, before_version = self.dump(), self.version(rid)
                        cells += 1
                        if pname in HIDDEN:
                            err = self.error(self.perform, principal, action, rid)
                            self.assertEqual(err.code, 'not_found')
                            self.assertIsNone(err.state)  # nothing about the request leaks
                            self.assertIsNone(err.version)
                            self.assertEqual(self.dump(), before_dump)
                            refused += 1
                            continue
                        if pname == 'local':
                            err = self.error(self.perform, principal, action, rid)
                            self.assertIn(err.code, ('forbidden', 'not_found'))
                            self.assertEqual(self.dump(), before_dump)
                            refused += 1
                            continue
                        if pname not in ALLOWED[action]:
                            err = self.error(self.perform, principal, action, rid)
                            self.assertEqual(err.code, 'forbidden')
                            self.assertEqual((err.state, err.version), (state, before_version))
                            self.assertEqual(self.dump(), before_dump)
                            refused += 1
                            continue
                        want = expected(state, action)
                        if want is None:
                            continue  # SPEC does not decide this cell
                        if want == INVALID:
                            err = self.error(self.perform, principal, action, rid)
                            self.assertEqual(err.code, 'invalid_transition')
                            self.assertEqual((err.state, err.version), (state, before_version))
                            self.assertEqual(self.dump(), before_dump)  # no partial write (e.g. implicit review)
                            refused += 1
                            continue
                        self.perform(principal, action, rid)
                        after = self.row(rid)
                        self.assertEqual((after['state'], after['outcome']), want)
                        if action == 'link_check' and state != 'NEW':
                            self.assertEqual(after['version'], before_version)  # only mints a token
                        else:
                            self.assertGreater(after['version'], before_version)
                        applied += 1
                        if action == 'cancel' and state in ('READY', 'RUNNING'):
                            self.assertEqual(after['cancel_state'], 'requested')
                            self.assertEqual(after['task_id'], 'task-1')
                        if action == 'ask' and state == 'RUNNING':
                            self.assertEqual(after['task_id'], 'task-1')  # P2-AC-27 link stays
        self.assertGreater(cells, 600)
        self.assertGreater(refused, 300)
        self.assertGreater(applied, 40)

    def test_hidden_request_answers_exactly_like_a_request_that_does_not_exist(self):
        rid = self.make('NEEDS_INFO')
        ghost = str(uuid.uuid4())
        for call in ('get', 'comment', 'cancel', 'update', 'set_state', 'start_review'):
            with self.subTest(call=call):
                def fire(target):
                    p = self.req2
                    return {
                        'get': lambda: self.service.get(p, target),
                        'comment': lambda: self.service.comment(p, target, 'hi'),
                        'cancel': lambda: self.service.cancel(p, target),
                        'update': lambda: self.service.update(p, target, 1, title='x'),
                        'set_state': lambda: self.service.set_state(p, target, 1, 'NEW'),
                        'start_review': lambda: self.service.start_review(p, target),
                    }[call]()
                hidden, absent = self.error(fire, rid), self.error(fire, ghost)
                self.assertEqual(hidden.to_json(), absent.to_json())
                self.assertEqual(hidden.code, 'not_found')
        for odd in ('', 'not-a-uuid', None, 7, {'x': 1}):
            self.assertEqual(self.error(self.service.get, self.req2, odd).code, 'not_found')

    def test_requesters_see_only_their_own_through_listing_search_and_badge(self):
        mine = self.new(self.req1, 'visible-mine', 'needle-body-one')
        theirs = self.new(self.req2, 'visible-theirs', 'needle-body-two')
        owners = self.service.submit(OWNER, self.a, 'owner-made', 'needle-body-three', None)['request_id']
        boards = self.service.submit(self.board, self.a, 'board-made', 'needle-body-four', None)['request_id']
        listed = self.service.list_requests(self.req1)['requests']
        self.assertEqual([r['request_id'] for r in listed], [mine])
        found = self.service.list_requests(self.req1, query='needle')['requests']
        self.assertEqual([r['request_id'] for r in found], [mine])
        self.assertEqual(self.service.list_requests(self.req1, query='needle-body-two')['requests'], [])
        self.assertEqual(self.service.badge(self.req1), {'pending': 1, 'needs_info': 0})
        self.assertEqual(self.service.badge(self.req2), {'pending': 1, 'needs_info': 0})
        self.assertEqual(self.service.badge(OWNER), {'pending': 4, 'needs_info': 0})
        for other in (theirs, owners, boards):
            self.assertEqual(self.error(self.service.get, self.req1, other).code, 'not_found')
        # same request count visible to an editor of the project
        self.assertEqual(len(self.service.list_requests(self.editor)['requests']), 4)

    def test_scope_outside_the_projects_answers_like_a_missing_project(self):
        self.assertEqual(self.error(self.service.submit, self.req1, self.b, 't', 'b').to_json(),
                         self.error(self.service.submit, self.req1, str(uuid.uuid4()), 't', 'b').to_json())
        self.assertEqual(self.error(self.service.list_requests, self.req1, project_id=self.b).code, 'not_found')
        beta = self.new(OWNER, project=self.b)
        for principal in (self.editor, self.req1, self.manager):
            self.assertEqual(self.error(self.service.get, principal, beta).code, 'not_found')
        self.assertFalse(self.db_path(self.root).exists())  # nothing was created in alpha
        self.assertEqual(self.service.list_requests(self.editor)['requests'], [])

    def test_manager_only_transitions_are_refused_to_everyone_else_even_the_owner(self):
        rid = self.make('REVIEWING')
        token = self.service.link_check(self.manager, rid)['link_token']
        before = self.dump()
        for principal in (OWNER, self.board, self.editor, self.req1):
            with self.subTest(principal=principal.kind):
                err = self.error(self.service.link_commit, principal, token, 'task-9', 1, 'dispatched')
                self.assertEqual(err.code, 'forbidden')
                self.assertEqual(self.error(self.service.link_check, principal, rid).code, 'forbidden')
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.row(rid)['state'], 'REVIEWING')
        self.assertEqual(self.rows('SELECT count(*) FROM links')[0][0], 0)

    def test_submit_is_new_only_for_every_submitting_principal_and_refused_to_the_rest(self):
        for principal in (OWNER, self.board, self.editor, self.req1):
            out = self.service.submit(principal, self.a, 'T', 'B', None)
            with self.subTest(principal=principal.kind):
                row = self.row(out['request_id'])
                self.assertEqual((row['state'], row['version'], row['revision'], row['review_round']),
                                 ('NEW', 1, 1, 1))
                self.assertEqual(out['state'], 'NEW')
        for principal in (self.manager, self.local):
            self.assertEqual(self.error(self.service.submit, principal, self.a, 'T', 'B').code, 'forbidden')

    def test_origin_is_recorded_per_principal(self):
        ids = {p.kind: self.service.submit(p, self.a, 'T', 'B', None)
               for p in (OWNER, self.board, self.editor, self.req1)}
        want = {'owner': ('owner', None, None), 'board_agent': ('board_agent', 'k0board000', None),
                'editor': ('api_key', 'k0editor00', 'ed'), 'requester': ('api_key', 'k0req00001', 'r1')}
        for kind, out in ids.items():
            row = self.row(out['request_id'])
            self.assertEqual((row['origin_kind'], row['origin_key_id'], row['origin_name']), want[kind])
        labels = {kind: out['origin']['label'] for kind, out in ids.items()}
        self.assertEqual(labels, {'owner': '소유자', 'board_agent': 'Board agent',
                                  'editor': 'key:ed', 'requester': 'key:r1'})

    def test_reopen_moves_the_task_link_to_history_and_clears_the_request(self):
        rid = self.make('RUNNING')
        self.service.review(self.manager, rid, 'close', outcome='failed', summary='it broke')
        closed = self.row(rid)
        self.assertEqual((closed['state'], closed['outcome'], closed['result_summary']), ('CLOSED', 'failed',
                                                                                          'it broke'))
        self.service.set_state(self.editor, rid, closed['version'], 'NEW')
        row = self.row(rid)
        self.assertEqual((row['state'], row['outcome'], row['task_id'], row['spec_revision'],
                          row['cancel_state']), ('NEW', None, None, None, None))
        self.assertEqual(row['delivery_state'], 'pending')
        self.assertEqual(row['review_round'], 2)
        links = self.rows('SELECT task_id, state FROM links WHERE request_id = ?', rid)
        self.assertEqual([task for task, _ in links], ['task-1'])  # history kept
        self.assertNotEqual(links[0][1], 'active')
        self.assertEqual(self.rows('SELECT count(*) FROM request_revisions WHERE request_id = ?', rid)[0][0], 1)
        # a requester cannot reopen
        self.service.cancel(OWNER, rid)
        self.assertEqual(self.error(self.service.set_state, self.req1, rid, self.version(rid), 'NEW').code,
                         'forbidden')

    def test_reopen_works_from_every_close_outcome_and_the_request_is_then_reviewable_again(self):
        for how in ('cancelled', 'rejected', 'success', 'failed'):
            with self.subTest(outcome=how):
                rid = self.make('READY' if how in ('success', 'failed') else 'REVIEWING')
                if how == 'cancelled':
                    self.service.cancel(OWNER, rid)
                elif how == 'rejected':
                    self.service.review(self.manager, rid, 'reject', body='no')
                else:
                    self.service.review(self.manager, rid, 'close', outcome=how, summary='s')
                self.assertEqual(self.row(rid)['outcome'], how)
                self.service.set_state(self.board, rid, self.version(rid), 'NEW')
                self.assertEqual(self.row(rid)['state'], 'NEW')
                self.link_after_reopen(rid)

    def link_after_reopen(self, rid):
        self.service.review(self.manager, rid, 'begin_review')
        self.link(rid, task_id='task-2')
        self.assertEqual(self.row(rid)['task_id'], 'task-2')


# =====================================================================================
# idempotency
# =====================================================================================
class IdempotencyTests(Base):
    def test_same_origin_key_and_content_returns_the_same_request_without_a_new_row(self):
        first = self.service.submit(self.req1, self.a, 'T', 'B', 'key-1')
        again = self.service.submit(self.req1, self.a, 'T', 'B', 'key-1')
        self.assertEqual(first['request_id'], again['request_id'])
        self.assertEqual((first['seq'], again['seq']), (1, 1))
        self.assertEqual(self.rows('SELECT count(*) FROM requests')[0][0], 1)
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'submitted'")[0][0], 1)

    def test_same_key_with_different_content_conflicts_and_writes_nothing(self):
        self.service.submit(self.req1, self.a, 'T', 'B', 'key-1')
        before = self.dump()
        for title, body in (('T2', 'B'), ('T', 'B2'), ('T ', 'B'), ('T', 'B\n')):
            with self.subTest(title=title, body=body):
                err = self.error(self.service.submit, self.req1, self.a, title, body, 'key-1')
                self.assertEqual(err.code, 'idempotency_conflict')
                self.assertEqual((err.state, err.version), ('NEW', 1))
        self.assertEqual(self.dump(), before)

    def test_the_content_split_between_title_and_body_matters(self):
        self.service.submit(self.req1, self.a, 'ab', 'c', 'k')
        self.assertEqual(self.error(self.service.submit, self.req1, self.a, 'a', 'bc', 'k').code,
                         'idempotency_conflict')

    def test_null_key_id_origins_dedupe_and_stay_separate_from_other_origins(self):
        o1 = self.service.submit(OWNER, self.a, 'T', 'B', 'shared')
        o2 = self.service.submit(OWNER, self.a, 'T', 'B', 'shared')
        self.assertEqual(o1['request_id'], o2['request_id'])
        self.assertEqual(self.error(self.service.submit, OWNER, self.a, 'T', 'other', 'shared').code,
                         'idempotency_conflict')
        board = self.service.submit(self.board, self.a, 'T', 'B', 'shared')
        editor = self.service.submit(self.editor, self.a, 'T', 'B', 'shared')
        req1 = self.service.submit(self.req1, self.a, 'T', 'B', 'shared')
        req2 = self.service.submit(self.req2, self.a, 'other content', 'B', 'shared')
        ids = {o1['request_id'], board['request_id'], editor['request_id'], req1['request_id'],
               req2['request_id']}
        self.assertEqual(len(ids), 5)  # different origin / key_id: a separate namespace
        self.assertEqual(self.rows('SELECT count(*) FROM requests')[0][0], 5)

    def test_keyless_submits_are_never_deduplicated_and_null_keys_do_not_collide(self):
        ids = {self.service.submit(OWNER, self.a, 'T', 'B', None)['request_id'] for _ in range(4)}
        self.assertEqual(len(ids), 4)
        self.assertEqual(self.rows('SELECT count(*) FROM requests WHERE idem_key IS NULL')[0][0], 4)

    def test_the_same_key_in_another_project_is_independent(self):
        one = self.service.submit(OWNER, self.a, 'T', 'B', 'k')
        two = self.service.submit(OWNER, self.b, 'different', 'B', 'k')
        self.assertNotEqual(one['request_id'], two['request_id'])

    def test_a_closed_request_still_owns_its_key_and_is_not_resurrected_or_duplicated(self):
        first = self.service.submit(self.req1, self.a, 'T', 'B', 'k')
        self.service.cancel(self.req1, first['request_id'])
        again = self.service.submit(self.req1, self.a, 'T', 'B', 'k')
        self.assertEqual(again['request_id'], first['request_id'])
        self.assertEqual(self.row(first['request_id'])['state'], 'CLOSED')
        self.assertEqual(self.rows('SELECT count(*) FROM requests')[0][0], 1)

    def test_concurrent_submits_with_one_key_create_one_request(self):
        results = run_threads([lambda: self.service.submit(self.req1, self.a, 'T', 'B', 'race')] * 12)
        self.assertTrue(all(kind == 'ok' for kind, _ in results), results)
        self.assertEqual(len({value['request_id'] for _, value in results}), 1)
        self.assertEqual(sum(1 for _, v in results if v['created']), 1)
        self.assertEqual(self.rows('SELECT count(*) FROM requests')[0][0], 1)

    def test_concurrent_submits_with_one_key_and_two_contents_pick_one_winner(self):
        results = run_threads([(lambda i=i: self.service.submit(self.req1, self.a, f'T{i % 2}', 'B', 'race'))
                               for i in range(10)])
        winners = {v['request_id'] for kind, v in results if kind == 'ok'}
        losers = [e for kind, e in results if kind == 'err']
        self.assertEqual(len(winners), 1)
        self.assertTrue(all(isinstance(e, InboxError) and e.code == 'idempotency_conflict' for e in losers))
        self.assertEqual(self.rows('SELECT count(*) FROM requests')[0][0], 1)

    def test_key_syntax_is_checked_before_anything_is_stored(self):
        for bad in ('', 'a' * 129, 'sp ace', 'ключ', 'a/b', 'a\nb', 5):
            with self.subTest(key=bad):
                self.assertEqual(self.error(self.service.submit, OWNER, self.a, 'T', 'B', bad).code,
                                 'invalid_input')
        self.assertFalse(self.db_path().exists())
        self.service.submit(OWNER, self.a, 'T', 'B', 'a' * 128)
        self.service.submit(OWNER, self.a, 'T', 'B', 'Az09._:-')


# =====================================================================================
# edits, revisions, answers
# =====================================================================================
class EditRuleTests(Base):
    def test_new_and_needs_info_edit_in_place_and_keep_the_previous_text_in_events(self):
        for state in ('NEW', 'NEEDS_INFO'):
            with self.subTest(state=state):
                rid = self.make(state)
                before = self.row(rid)
                self.service.update(self.editor, rid, before['version'], title='New title', body='New body')
                row = self.row(rid)
                self.assertEqual((row['title'], row['body'], row['state'], row['revision']),
                                 ('New title', 'New body', state, 1))
                self.assertEqual(row['version'], before['version'] + 1)
                self.assertEqual(row['review_round'], before['review_round'])
                self.assertEqual(self.rows('SELECT count(*) FROM request_revisions WHERE request_id = ?',
                                           rid)[0][0], 1)
                detail = json.loads(self.rows("SELECT detail FROM events WHERE request_id = ? "
                                              "AND type = 'edited' ORDER BY event_id DESC", rid)[0][0])
                self.assertEqual((detail['previous']['title'], detail['previous']['body']),
                                 ('Title', 'Body'))

    def test_reviewing_edits_in_place_and_starts_a_new_review_round(self):
        rid = self.make('REVIEWING')
        before = self.row(rid)
        self.service.update(OWNER, rid, before['version'], body='Revised body')
        row = self.row(rid)
        self.assertEqual((row['state'], row['body'], row['title'], row['revision']),
                         ('REVIEWING', 'Revised body', 'Title', 1))
        self.assertEqual(row['review_round'], before['review_round'] + 1)
        self.assertEqual(row['delivery_state'], 'pending')
        self.assertEqual(self.rows('SELECT count(*) FROM request_revisions WHERE request_id = ?', rid)[0][0], 1)
        self.assertEqual(self.events[-1].type, 'updated')  # SPEC: automatic review -> `updated` notice

    def test_ready_and_running_edits_create_a_new_revision_and_notify_revised(self):
        for state in ('READY', 'RUNNING'):
            with self.subTest(state=state):
                rid = self.make(state)
                before = self.row(rid)
                self.events.clear()
                out = self.service.update(self.board, rid, before['version'], title='Rev 2', body='Body 2')
                self.assertEqual(out['revision'], 2)
                row = self.row(rid)
                self.assertEqual((row['state'], row['revision'], row['title'], row['body']),
                                 (state, 2, 'Rev 2', 'Body 2'))
                self.assertEqual((row['task_id'], row['spec_revision']), (before['task_id'],
                                                                         before['spec_revision']))
                revisions = self.rows('SELECT revision, title, body FROM request_revisions '
                                      'WHERE request_id = ? ORDER BY revision', rid)
                self.assertEqual(revisions, [(1, 'Title', 'Body'), (2, 'Rev 2', 'Body 2')])
                self.assertEqual([e.type for e in self.events], ['revised'])
                self.assertEqual(self.events[0].request_id, rid)
                again = self.service.update(self.board, rid, row['version'], title='Rev 3')
                self.assertEqual(again['revision'], 3)  # body kept from revision 2
                self.assertEqual(self.rows('SELECT body FROM request_revisions WHERE request_id = ? '
                                           'AND revision = 3', rid)[0][0], 'Body 2')

    def test_closed_requests_cannot_be_edited_until_reopened(self):
        rid = self.make('CLOSED')
        err = self.error(self.service.update, OWNER, rid, self.version(rid), title='x')
        self.assertEqual((err.code, err.state), ('invalid_transition', 'CLOSED'))
        self.service.set_state(OWNER, rid, self.version(rid), 'NEW')
        self.service.update(OWNER, rid, self.version(rid), title='x')
        self.assertEqual(self.row(rid)['title'], 'x')

    def test_stale_base_version_is_a_conflict_that_reports_current_state_and_writes_nothing(self):
        rid = self.make('NEW')
        stale = self.version(rid)
        self.service.update(OWNER, rid, stale, title='first')
        before = self.dump()
        err = self.error(self.service.update, self.editor, rid, stale, title='second')
        self.assertEqual((err.code, err.state, err.version), ('conflict', 'NEW', stale + 1))
        for bad in (None, '2', 1.0, True):
            self.assertEqual(self.error(self.service.update, OWNER, rid, bad, title='z').code, 'invalid_input')
        self.assertEqual(self.dump(), before)
        # same rule for state changes
        self.assertEqual(self.error(self.service.set_state, OWNER, rid, stale, 'NEEDS_INFO').code, 'conflict')

    def test_nothing_to_change_and_oversized_edits_are_rejected_untouched(self):
        rid = self.make('NEW')
        before = self.dump()
        v = self.version(rid)
        self.assertEqual(self.error(self.service.update, OWNER, rid, v).code, 'invalid_input')
        self.assertEqual(self.error(self.service.update, OWNER, rid, v, title='t' * 201).code, 'too_large')
        self.assertEqual(self.error(self.service.update, OWNER, rid, v, body='x' * 65537).code, 'too_large')
        self.assertEqual(self.error(self.service.update, OWNER, rid, v, title='bad\x00').code, 'invalid_input')
        self.assertEqual(self.dump(), before)

    def test_an_answer_returns_needs_info_to_reviewing_with_a_new_round(self):
        for name, principal in (('owner', OWNER), ('editor', self.editor), ('board', self.board),
                                ('original requester', self.req1)):
            with self.subTest(by=name):
                rid = self.make('NEEDS_INFO')
                before = self.row(rid)
                out = self.service.comment(principal, rid, 'the answer')
                row = self.row(rid)
                self.assertEqual(out['state'], 'REVIEWING')
                self.assertEqual((row['state'], row['review_round'], row['delivery_state']),
                                 ('REVIEWING', before['review_round'] + 1, 'pending'))
                kinds = [k for (k,) in self.rows('SELECT kind FROM comments WHERE request_id = ? ORDER BY seq',
                                                 rid)]
                self.assertEqual(kinds, ['question', 'answer'])

    def test_comment_kinds_follow_the_author(self):
        rid = self.make('REVIEWING')
        self.service.comment(self.req1, rid, 'plain')
        self.service.review(self.manager, rid, 'comment', body='working on it')
        kinds = self.rows('SELECT kind, author_kind FROM comments WHERE request_id = ? ORDER BY seq', rid)
        self.assertEqual(kinds, [('comment', 'requester'), ('progress', 'manager')])
        self.assertEqual(self.row(rid)['state'], 'REVIEWING')  # a plain comment is not an answer


# =====================================================================================
# cancel / link (D-35)
# =====================================================================================
class CancelLinkTests(Base):
    def test_cancel_that_arrives_first_beats_link_commit_and_nothing_is_linked(self):
        rid = self.make('REVIEWING')
        token = self.service.link_check(self.manager, rid)['link_token']
        self.service.cancel(OWNER, rid, reason='changed my mind')
        err = self.error(self.service.link_commit, self.manager, token, 'task-9', 1, 'dispatched')
        self.assertEqual((err.code, err.state), ('invalid_transition', 'CLOSED'))
        row = self.row(rid)
        self.assertEqual((row['state'], row['outcome'], row['task_id']), ('CLOSED', 'cancelled', None))
        self.assertEqual(self.rows('SELECT count(*) FROM links')[0][0], 0)

    def test_link_commit_that_arrives_first_leaves_a_ready_request_with_the_ready_cancel_path(self):
        rid = self.make('REVIEWING')
        token = self.service.link_check(self.manager, rid)['link_token']
        self.service.link_commit(self.manager, token, 'task-9', 3, 'dispatched')
        row = self.row(rid)
        self.assertEqual((row['state'], row['task_id'], row['spec_revision']), ('READY', 'task-9', 3))
        self.service.cancel(OWNER, rid)
        row = self.row(rid)
        self.assertEqual((row['state'], row['cancel_state']), ('READY', 'requested'))  # not CLOSED yet
        self.assertEqual(self.error(self.service.cancel, OWNER, rid).code, 'invalid_transition')
        self.assertEqual(self.events[-1].type, 'cancel_requested')
        self.assertEqual(self.events[-1].detail['task_id'], 'task-9')

    def test_ready_cancel_with_the_backend_down_is_refused_and_changes_nothing(self):
        rid = self.make('RUNNING')
        self.backend_up = False
        before = self.dump()
        err = self.error(self.service.cancel, self.req1, rid)
        self.assertEqual((err.code, err.state), ('backend_not_running', 'RUNNING'))
        self.assertEqual(self.dump(), before)
        # pre-dispatch cancel needs no backend
        pre = self.make('REVIEWING')
        self.service.cancel(self.req1, pre)
        self.assertEqual(self.row(pre)['outcome'], 'cancelled')

    def test_running_request_closes_cancelled_only_when_the_task_sync_observes_the_user_cancel(self):
        rid = self.make('RUNNING')
        self.service.cancel(OWNER, rid)
        self.assertEqual(self.row(rid)['state'], 'RUNNING')
        self.service.observe_task(rid, 'task-1', 'closed', closed_reason='user', user_cancelled=True)
        row = self.row(rid)
        self.assertEqual((row['state'], row['outcome'], row['cancel_state']), ('CLOSED', 'cancelled', None))

    def test_other_task_closure_does_not_close_the_request_p2_ac_09(self):
        rid = self.make('RUNNING')
        self.service.observe_task(rid, 'task-1', 'closed', closed_reason='manager_cancel', user_cancelled=False)
        row = self.row(rid)
        self.assertEqual((row['state'], row['outcome']), ('RUNNING', None))
        self.assertEqual([e.type for e in self.events if e.type == 'linked_task_closed'], ['linked_task_closed'])

    def test_any_commit_between_check_and_commit_invalidates_the_token(self):
        for name, mutate in (
                ('edit', lambda rid: self.service.update(OWNER, rid, self.version(rid), body='changed')),
                ('comment', lambda rid: self.service.comment(OWNER, rid, 'hey')),
                ('manager progress', lambda rid: self.service.review(self.manager, rid, 'comment', body='p')),
                ('ask', lambda rid: self.service.review(self.manager, rid, 'ask', body='q?')),
                ('cancel', lambda rid: self.service.cancel(OWNER, rid))):
            with self.subTest(change=name):
                rid = self.make('REVIEWING')
                token = self.service.link_check(self.manager, rid)['link_token']
                mutate(rid)
                before = self.dump()
                err = self.error(self.service.link_commit, self.manager, token, 'task-x', 1, 'dispatched')
                self.assertEqual(err.code, 'invalid_transition')
                self.assertEqual(self.dump(), before)

    def test_link_token_is_single_use_bound_to_its_request_and_project(self):
        first, second = self.make('REVIEWING'), self.make('REVIEWING')
        token = self.service.link_check(self.manager, first)['link_token']
        self.service.link_commit(self.manager, token, 'task-1', 1, 'dispatched')
        self.assertEqual(self.error(self.service.link_commit, self.manager, token, 'task-2', 1,
                                    'dispatched').code, 'invalid_transition')
        self.assertEqual(self.row(second)['task_id'], None)
        self.assertEqual(self.error(self.service.link_commit, self.manager, 'bogus', 'task-2', 1,
                                    'dispatched').code, 'invalid_transition')
        token2 = self.service.link_check(self.manager, second)['link_token']
        self.assertNotEqual(token, token2)
        err = self.error(self.service.link_commit, self.manager_b, token2, 'task-2', 1, 'dispatched')
        self.assertEqual(err.code, 'invalid_transition')
        self.assertEqual(self.row(second)['state'], 'REVIEWING')

    def test_link_token_expires_after_its_ttl(self):
        rid = self.make('REVIEWING')
        out = self.service.link_check(self.manager, rid)
        self.assertEqual(out['expires_in'], 30)  # SPEC: 30 s
        self.now[0] += 29
        self.service.link_commit(self.manager, out['link_token'], 'task-1', 1, 'dispatched')
        self.assertEqual(self.row(rid)['state'], 'READY')
        other = self.make('REVIEWING')
        late = self.service.link_check(self.manager, other)['link_token']
        self.now[0] += 31
        before = self.dump()
        err = self.error(self.service.link_commit, self.manager, late, 'task-2', 1, 'dispatched')
        self.assertEqual((err.code, err.state), ('invalid_transition', 'REVIEWING'))
        self.assertEqual(self.dump(), before)
        again = self.service.link_check(self.manager, other)  # a fresh check works
        self.service.link_commit(self.manager, again['link_token'], 'task-2', 1, 'dispatched')
        self.assertEqual(self.row(other)['task_id'], 'task-2')

    def test_task_status_decides_ready_or_running(self):
        for status, want in (('dispatched', 'READY'), ('starting', 'READY'), ('running', 'RUNNING'),
                             ('waiting_report', 'RUNNING'), ('held', 'RUNNING')):
            with self.subTest(status=status):
                rid = self.make('REVIEWING')
                self.link(rid, status)
                self.assertEqual(self.row(rid)['state'], want)
        rid = self.make('REVIEWING')
        token = self.service.link_check(self.manager, rid)['link_token']
        self.assertEqual(self.error(self.service.link_commit, self.manager, token, 't', 1, 'closed').code,
                         'invalid_input')

    def test_link_from_needs_info_and_new_and_not_from_linked_or_closed(self):
        rid = self.make('NEEDS_INFO')
        self.link(rid)
        self.assertEqual(self.row(rid)['state'], 'READY')
        for state in ('READY', 'RUNNING', 'CLOSED'):
            r = self.make(state)
            self.assertEqual(self.error(self.service.link_check, self.manager, r).code, 'invalid_transition')
        rid = self.make('NEW')
        self.link(rid)  # NEW -> REVIEWING implicitly, then linked
        self.assertEqual(self.row(rid)['state'], 'READY')

    def test_cancel_versus_link_commit_threads_always_reach_one_consistent_end_state(self):
        outcomes = set()
        for trial in range(40):
            rid = self.make('REVIEWING')
            token = self.service.link_check(self.manager, rid)['link_token']
            results = run_threads([
                lambda: self.service.cancel(OWNER, rid),
                lambda: self.service.link_commit(self.manager, token, f'task-{trial}', 1, 'dispatched'),
            ])
            row = self.row(rid)
            links = self.rows('SELECT task_id FROM links WHERE request_id = ?', rid)
            (cancel_kind, cancel_value), (link_kind, link_value) = results
            if row['state'] == 'CLOSED':
                outcomes.add('cancel-first')
                self.assertEqual(cancel_kind, 'ok', results)
                self.assertEqual((link_kind, link_value.code), ('err', 'invalid_transition'))
                self.assertEqual((row['outcome'], row['task_id'], links), ('cancelled', None, []))
            else:
                outcomes.add('link-first')
                self.assertEqual(row['state'], 'READY')
                self.assertEqual(link_kind, 'ok', results)
                self.assertEqual(links, [(f'task-{trial}',)])
                self.assertEqual(cancel_kind, 'ok')  # READY cancel path
                self.assertEqual(cancel_value['status'], 'cancel_requested')
                self.assertEqual(row['cancel_state'], 'requested')
        self.assertTrue(outcomes <= {'cancel-first', 'link-first'})

    def test_arrival_order_decides_cancel_versus_link_commit_in_both_directions(self):
        for trial in range(25):
            rid = self.make('REVIEWING')
            token = self.service.link_check(self.manager, rid)['link_token']
            futures = [self.service.cancel(OWNER, rid, wait=False),
                       self.service.link_commit(self.manager, token, 'task-a', 1, 'dispatched', wait=False)]
            self.assertEqual(futures[0].result(30)['status'], 'cancelled')
            self.assertEqual(futures[1].exception(30).code, 'invalid_transition')
            self.assertEqual(self.row(rid)['state'], 'CLOSED')
            rid = self.make('REVIEWING')
            token = self.service.link_check(self.manager, rid)['link_token']
            futures = [self.service.link_commit(self.manager, token, 'task-b', 1, 'dispatched', wait=False),
                       self.service.cancel(OWNER, rid, wait=False)]
            self.assertEqual(futures[0].result(30)['state'], 'READY')
            self.assertEqual(futures[1].result(30)['status'], 'cancel_requested')
            self.assertEqual(self.row(rid)['state'], 'READY')

    def test_a_second_process_cannot_cancel_while_this_hub_holds_the_inbox(self):
        rid = self.make('REVIEWING')
        token = self.service.link_check(self.manager, rid)['link_token']
        before = self.dump()
        outcome = self.child('cancel', rid)
        self.assertIn(outcome.get('error'), ('inbox_busy', 'not_found'), outcome)  # never an applied cancel
        self.assertEqual(self.dump(), before)
        self.service.link_commit(self.manager, token, 'task-1', 1, 'dispatched')
        self.assertEqual(self.row(rid)['state'], 'READY')


# =====================================================================================
# ordering, serialization, no lost updates
# =====================================================================================
class SerializationTests(Base):
    def event_chain(self, rid: str):
        return self.rows('SELECT type, from_state, to_state FROM events WHERE request_id = ? '
                         'ORDER BY event_id', rid)

    def assert_consistent(self, rid: str):
        """version == number of events, each event starts where the previous one ended, last == row."""
        chain = self.event_chain(rid)
        row = self.row(rid)
        self.assertEqual(row['version'], len(chain), chain)
        for (_, _, to_state), (kind, from_state, _) in zip(chain, chain[1:]):
            self.assertEqual(from_state, to_state, f'broken chain at {kind}: {chain}')
        self.assertEqual(chain[-1][2], row['state'])

    def test_concurrent_submits_from_many_threads_get_a_gapless_unique_seq_in_per_thread_order(self):
        def worker(n):
            return [self.service.submit(self.req1, self.a, f't{n}-{i}', 'b', None) for i in range(8)]
        results = run_threads([lambda n=n: worker(n) for n in range(8)])
        self.assertTrue(all(kind == 'ok' for kind, _ in results), results)
        seqs = []
        for _, outs in results:
            thread_seqs = [o['seq'] for o in outs]
            self.assertEqual(thread_seqs, sorted(thread_seqs))
            self.assertEqual(len(set(thread_seqs)), 8)
            seqs.extend(thread_seqs)
        self.assertEqual(sorted(seqs), list(range(1, 65)))
        self.assertEqual(self.rows('SELECT count(DISTINCT request_id), max(seq) FROM requests')[0], (64, 64))

    def test_async_submits_keep_call_order(self):
        futures = [self.service.submit(OWNER, self.a, f'n{i}', 'b', None, wait=False) for i in range(40)]
        ids = [f.result(30)['request_id'] for f in futures]
        titles = [t for (t,) in self.rows('SELECT title FROM requests ORDER BY seq')]
        self.assertEqual(titles, [f'n{i}' for i in range(40)])
        self.assertEqual([r for (r,) in self.rows('SELECT request_id FROM requests ORDER BY seq')], ids)

    def test_concurrent_comments_on_one_request_lose_no_update(self):
        rid = self.make('REVIEWING')
        start = self.version(rid)
        results = run_threads([(lambda i=i: self.service.comment(self.editor, rid, f'c{i}')) for i in range(14)])
        self.assertTrue(all(kind == 'ok' for kind, _ in results), results)
        versions = sorted(v['version'] for _, v in results)
        self.assertEqual(versions, list(range(start + 1, start + 15)))  # strictly +1 each, no duplicates
        self.assertEqual(self.version(rid), start + 14)
        seqs = [s for (s,) in self.rows('SELECT seq FROM comments WHERE request_id = ? ORDER BY seq', rid)]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assert_consistent(rid)

    def test_edits_that_share_a_base_version_cannot_both_win(self):
        rid = self.make('NEW')
        base = self.version(rid)
        results = run_threads([(lambda i=i: self.service.update(OWNER, rid, base, title=f'edit-{i}'))
                               for i in range(10)])
        winners = [v for kind, v in results if kind == 'ok']
        losers = [e for kind, e in results if kind == 'err']
        self.assertEqual(len(winners), 1, results)
        self.assertTrue(all(isinstance(e, InboxError) and e.code == 'conflict' and e.version == base + 1
                            for e in losers), losers)
        row = self.row(rid)
        self.assertEqual(row['version'], base + 1)
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE request_id = ? AND type = 'edited'",
                                   rid)[0][0], 1)
        self.assert_consistent(rid)

    def test_mixed_concurrent_changes_on_one_request_form_a_serial_history(self):
        for trial in range(25):
            rid = self.make('NEW')
            base = self.version(rid)
            calls = [
                lambda: self.service.cancel(OWNER, rid),
                lambda: self.service.update(self.editor, rid, base, title='edited'),
                lambda: self.service.set_state(self.board, rid, base, 'NEEDS_INFO'),
                lambda: self.service.review(self.manager, rid, 'ask', body='q?'),
                lambda: self.service.review(self.manager, rid, 'reject', body='no'),
                lambda: self.service.comment(self.req1, rid, 'ping'),
                lambda: self.service.link_check(self.manager, rid),
            ]
            results = run_threads(calls)
            for kind, value in results:
                if kind == 'err':
                    self.assertIsInstance(value, InboxError, value)
                    self.assertIn(value.code, ('invalid_transition', 'conflict'), value)
                    self.assertIsNotNone(value.version)
            self.assert_consistent(rid)
            row = self.row(rid)
            self.assertIn(row['state'], STATES)
            self.assertEqual(row['state'] == 'CLOSED', row['outcome'] is not None)

    def test_refused_changes_in_a_race_leave_no_trace(self):
        rid = self.make('NEW')
        self.service.cancel(OWNER, rid)
        before = self.dump()
        results = run_threads([
            lambda: self.service.update(OWNER, rid, self.version(rid), title='late'),
            lambda: self.service.cancel(OWNER, rid),
            lambda: self.service.review(self.manager, rid, 'close', outcome='success', summary='s'),
            lambda: self.service.set_state(OWNER, rid, self.version(rid), 'NEEDS_INFO'),
        ])
        self.assertTrue(all(kind == 'err' and value.code == 'invalid_transition' for kind, value in results),
                        results)
        self.assertEqual(self.dump(), before)

    def test_requests_in_different_projects_do_not_block_or_mix(self):
        results = run_threads([(lambda p=p, i=i: self.service.submit(OWNER, p, f't{i}', 'b', None))
                               for i in range(10) for p in (self.a, self.b)])
        self.assertTrue(all(kind == 'ok' for kind, _ in results))
        self.assertEqual(self.rows('SELECT count(*) FROM requests')[0][0], 10)
        self.assertEqual(self.rows('SELECT count(*) FROM requests', root=self.root_b)[0][0], 10)
        self.assertEqual(sorted(s for (s,) in self.rows('SELECT seq FROM requests')), list(range(1, 11)))
        ids_a = {r for (r,) in self.rows('SELECT request_id FROM requests')}
        ids_b = {r for (r,) in self.rows('SELECT request_id FROM requests', root=self.root_b)}
        self.assertFalse(ids_a & ids_b)

    def test_a_failing_on_committed_callback_never_undoes_the_commit(self):
        def boom(event):
            raise RuntimeError('notification failed')
        service = self.make_service(on_committed=boom)
        rid = service.submit(OWNER, self.a, 'T', 'B', None)['request_id']
        service.update(OWNER, rid, 1, title='T2')
        self.assertEqual(self.row(rid)['title'], 'T2')
        self.assertEqual(self.row(rid)['version'], 2)

    def test_the_callback_sees_only_committed_state(self):
        seen = []

        def look(event):
            seen.append((event.type, event.version, self.rows('SELECT version, state FROM requests '
                                                              'WHERE request_id = ?', event.request_id)))
        service = self.make_service(on_committed=look)
        rid = service.submit(OWNER, self.a, 'T', 'B', None)['request_id']
        service.cancel(OWNER, rid)
        self.assertEqual([(t, v, rows[0][0]) for t, v, rows in seen], [('submitted', 1, 1), ('cancelled', 2, 2)])
        self.assertEqual(seen[1][2][0][1], 'CLOSED')

    def test_no_event_is_emitted_for_a_refused_change(self):
        rid = self.make('NEW')
        self.events.clear()
        self.error(self.service.review, self.manager, rid, 'close', outcome='success', summary='s')
        self.error(self.service.update, OWNER, rid, 99, title='x')
        self.error(self.service.cancel, self.req2, rid)
        self.assertEqual(self.events, [])


# =====================================================================================
# store: files, lock, crash
# =====================================================================================
class StoreFileTests(Base):
    def test_files_are_private_wal_and_schema_v1(self):
        self.service.submit(OWNER, self.a, 'T', 'B', None)
        for name in ('inbox.sqlite3', 'inbox.lock', 'inbox.sqlite3-wal', 'inbox.sqlite3-shm'):
            path = self.root / '.wb' / name
            with self.subTest(file=name):
                self.assertTrue(path.exists())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)
                self.assertEqual(path.stat().st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE(self.db_path().stat().st_mode), 0o600)
        self.assertEqual(self.rows('PRAGMA journal_mode')[0][0], 'wal')
        tables = {t for (t,) in self.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, set(TABLES))
        meta = dict(self.rows('SELECT key, value FROM meta'))
        self.assertEqual((meta['schema_version'], meta['project_id'], meta['review_mode']),
                         ('1', self.a, 'auto'))
        self.assertIn('created_at', meta)

    def test_the_inbox_lives_only_in_its_own_project(self):
        self.service.submit(OWNER, self.a, 'in-alpha', 'b', None)
        self.service.submit(OWNER, self.b, 'in-beta', 'b', None)
        self.assertEqual([t for (t,) in self.rows('SELECT title FROM requests')], ['in-alpha'])
        self.assertEqual([t for (t,) in self.rows('SELECT title FROM requests', root=self.root_b)], ['in-beta'])

    def test_reads_and_listings_create_no_file_and_survive_a_store_restart(self):
        self.assertEqual(self.service.list_requests(OWNER)['requests'], [])
        self.assertEqual(self.service.badge(OWNER), {'pending': 0, 'needs_info': 0})
        self.assertEqual(self.service.review_mode(OWNER, self.a), {'review_mode': 'auto'})
        self.assertEqual(self.service.pending_requests(self.a),
                         {'new': 0, 'reviewing': 0, 'needs_info': 0, 'ready': 0, 'running': 0})
        self.assertEqual(sorted(p.name for p in (self.root / '.wb').iterdir()), ['project.json'])
        rid = self.service.submit(OWNER, self.a, 'T', 'B', None)['request_id']
        self.service.close()
        restarted = self.make_service()
        self.assertEqual(restarted.get(OWNER, rid)['title'], 'T')
        self.assertEqual(restarted.submit(OWNER, self.a, 'T2', 'B', None)['seq'], 2)

    def test_idle_close_releases_the_lock_and_the_next_call_reopens(self):
        service = self.make_service(idle_seconds=60.0)
        self.service.close()
        rid = service.submit(OWNER, self.a, 'T', 'B', None)['request_id']
        self.assertFalse(self.lock_free())
        self.now[0] += 59
        self.assertEqual(service.close_idle(), [])
        self.assertFalse(self.lock_free())
        self.now[0] += 1
        self.assertIn(self.a, service.close_idle())
        self.assertTrue(self.lock_free())
        self.assertEqual(service.get(OWNER, rid)['title'], 'T')
        self.assertFalse(self.lock_free())

    def test_another_process_holding_the_lock_blocks_every_write_and_corrupts_nothing(self):
        rid = self.make('REVIEWING')
        self.service.close_idle(force=True)
        self.assertTrue(self.lock_free())
        holder = self.lock_holder()
        before = self.file_state()
        for name, call in (
                ('submit', lambda: self.service.submit(OWNER, self.a, 'during', 'b', None)),
                ('comment', lambda: self.service.comment(OWNER, rid, 'during')),
                ('cancel', lambda: self.service.cancel(OWNER, rid)),
                ('update', lambda: self.service.update(OWNER, rid, 2, title='during')),
                ('review', lambda: self.service.review(self.manager, rid, 'ask', body='during')),
                ('review_mode', lambda: self.service.review_mode(OWNER, self.a, set='manual')),
                ('link_check', lambda: self.service.link_check(self.manager, rid)),
                ('list', lambda: self.service.list_requests(OWNER, project_id=self.a))):
            with self.subTest(call=name):
                self.assertEqual(self.error(call).code, 'inbox_busy')
        self.assertEqual(self.file_state(), before)
        self.assertIsNone(holder.poll())
        # other projects keep working while one is busy
        self.service.submit(OWNER, self.b, 'beta is fine', 'b', None)
        holder.stdin.close()
        holder.wait(30)
        self.service.submit(OWNER, self.a, 'after', 'b', None)
        self.assertEqual(self.rows('PRAGMA integrity_check')[0][0], 'ok')
        self.assertEqual(self.rows('SELECT count(*) FROM requests')[0][0], 2)

    def test_a_busy_first_write_creates_no_inbox_file(self):
        holder = self.lock_holder()
        self.assertEqual(self.error(self.service.submit, OWNER, self.a, 'T', 'B').code, 'inbox_busy')
        self.assertFalse(self.db_path().exists())
        holder.stdin.close()
        holder.wait(30)

    def test_two_hub_processes_never_write_the_same_inbox_at_once(self):
        process = self.spawn(CHILD_SERVICE, str(self.root), self.a, 'submit_hold', stdin=True)
        self.assertIn('ok', json.loads(process.stdout.readline()))
        self.assertEqual(self.error(self.service.submit, OWNER, self.a, 'parent', 'b').code, 'inbox_busy')
        self.assertEqual([t for (t,) in self.rows('SELECT title FROM requests')], ['held by child'])
        process.stdin.close()
        process.wait(30)
        self.assertEqual(self.service.submit(OWNER, self.a, 'parent', 'b', None)['seq'], 2)
        self.assertEqual(self.child('submit').get('error'), 'inbox_busy')  # parent holds it again

    def test_process_killed_inside_a_write_transaction_leaves_no_partial_rows(self):
        self.service.close()
        crash = self.spawn(CHILD_CRASH, str(self.root), self.a, 'second')
        crash.communicate(timeout=60)
        self.assertEqual(crash.returncode, -9)
        service = self.make_service()
        rows = self.rows('SELECT request_id, title FROM requests ORDER BY seq')
        self.assertEqual([r for r, _ in rows], ['00000000-0000-4000-8000-000000000000',
                                               '22222222-2222-4222-8222-222222222222'])
        self.assertEqual([t for _, t in rows], ['t', 't'])  # the unfinished UPDATE did not stick
        self.assertEqual(self.rows('PRAGMA integrity_check')[0][0], 'ok')
        self.assertEqual(service.submit(OWNER, self.a, 'recovered', 'b', None)['seq'], 3)

    def test_process_killed_during_the_very_first_write_leaves_a_recoverable_empty_inbox(self):
        crash = self.spawn(CHILD_CRASH, str(self.root), self.a, 'first')
        crash.communicate(timeout=60)
        self.assertEqual(crash.returncode, -9)
        self.assertEqual(self.service.list_requests(OWNER)['requests'], [])
        out = self.service.submit(OWNER, self.a, 'recovered', 'b', None)
        self.assertEqual(out['seq'], 1)
        self.assertEqual(self.rows('SELECT count(*) FROM requests')[0][0], 1)
        self.assertEqual(self.rows('PRAGMA integrity_check')[0][0], 'ok')

    def test_a_committed_request_survives_the_process_dying_right_after(self):
        process = self.spawn(CHILD_SERVICE, str(self.root), self.a, 'submit_die')
        process.communicate(timeout=60)
        self.assertEqual(process.returncode, 7)
        self.assertTrue(self.lock_free())
        self.assertEqual([t for (t,) in self.rows('SELECT title FROM requests')], ['committed by child'])
        self.assertEqual(self.service.list_requests(OWNER)['requests'][0]['title'], 'committed by child')

    def test_a_symlinked_wb_directory_is_refused_and_nothing_is_created_in_the_target(self):
        target = self.base / 'elsewhere'
        target.mkdir(mode=0o700)
        os.rename(self.root / '.wb', target / 'wb')
        os.symlink(target / 'wb', self.root / '.wb')
        err = self.error(self.service.submit, OWNER, self.a, 'T', 'B')
        self.assertEqual(err.code, 'project_unavailable')
        self.assertEqual(sorted(p.name for p in (target / 'wb').iterdir()), ['project.json'])

    def test_a_symlinked_inbox_file_is_refused_and_the_target_is_untouched(self):
        self.service.submit(OWNER, self.a, 'T', 'B', None)
        self.service.close_idle(force=True)
        real = self.base / 'real.sqlite3'
        os.rename(self.db_path(), real)
        for suffix in ('-wal', '-shm'):
            Path(f'{self.db_path()}{suffix}').unlink(missing_ok=True)
        os.symlink(real, self.db_path())
        before = real.read_bytes()
        for call in (lambda: self.service.submit(OWNER, self.a, 'x', 'b', None),
                     lambda: self.service.list_requests(OWNER, project_id=self.a)):
            self.assertEqual(self.error(call).code, 'inbox_unavailable')
        self.assertEqual(real.read_bytes(), before)

    def test_a_group_readable_inbox_file_is_refused(self):
        self.service.submit(OWNER, self.a, 'T', 'B', None)
        self.service.close_idle(force=True)
        os.chmod(self.db_path(), 0o644)
        before = self.db_path().read_bytes()
        self.assertEqual(self.error(self.service.submit, OWNER, self.a, 'x', 'b').code, 'inbox_unavailable')
        self.assertEqual(self.db_path().read_bytes(), before)

    def test_a_damaged_file_is_refused_not_overwritten_and_other_projects_work(self):
        self.service.submit(OWNER, self.b, 'beta', 'b', None)
        self.db_path().write_bytes(b'this is definitely not a sqlite database' * 50)
        os.chmod(self.db_path(), 0o600)
        before = self.db_path().read_bytes()
        for call in (lambda: self.service.submit(OWNER, self.a, 'x', 'b', None),
                     lambda: self.service.list_requests(OWNER, project_id=self.a)):
            self.assertEqual(self.error(call).code, 'inbox_unavailable')
        self.assertEqual(self.db_path().read_bytes(), before)
        listing = self.service.list_requests(OWNER)
        self.assertEqual([r['title'] for r in listing['requests']], ['beta'])
        self.assertEqual([u['project_id'] for u in listing.get('unavailable', [])], [self.a])
        self.assertIsNone(self.service.pending_requests(self.a))
        self.assertEqual(self.service.badge(OWNER), {'pending': 1, 'needs_info': 0})

    def test_a_foreign_project_id_in_meta_is_inbox_mismatch_and_the_file_is_not_fixed(self):
        self.service.submit(OWNER, self.a, 'T', 'B', None)
        self.service.close_idle(force=True)
        connection = sqlite3.connect(self.db_path())
        connection.execute("UPDATE meta SET value = ? WHERE key = 'project_id'", (str(uuid.uuid4()),))
        connection.commit()
        connection.close()
        dump = self.dump()
        state = self.file_state()
        for call in (lambda: self.service.submit(OWNER, self.a, 'x', 'b', None),
                     lambda: self.service.review_mode(OWNER, self.a, set='manual'),
                     lambda: self.service.list_requests(OWNER, project_id=self.a)):
            self.assertEqual(self.error(call).code, 'inbox_mismatch')
        self.assertEqual(self.file_state()[''], state[''])
        self.assertEqual(self.dump(), dump)

    def test_registry_mismatch_makes_the_project_unavailable_without_touching_files(self):
        self.service.set_projects([ProjectRef(self.a, self.root, 'alpha', unavailable='registry mismatch')])
        for call in (lambda: self.service.submit(OWNER, self.a, 'x', 'b', None),
                     lambda: self.service.review_mode(OWNER, self.a),
                     lambda: self.service.list_requests(OWNER, project_id=self.a)):
            self.assertEqual(self.error(call).code, 'project_unavailable')
        self.assertIsNone(self.service.pending_requests(self.a))
        self.assertFalse(self.db_path().exists())

    def test_a_marker_for_another_project_is_refused(self):
        marker = self.root / '.wb' / 'project.json'
        data = json.loads(marker.read_text())
        data['project_id'] = str(uuid.uuid4())
        marker.write_text(json.dumps(data))
        self.assertEqual(self.error(self.service.submit, OWNER, self.a, 'x', 'b').code, 'project_unavailable')
        self.assertFalse(self.db_path().exists())

    def test_hub_close_releases_every_lock(self):
        self.service.submit(OWNER, self.a, 'T', 'B', None)
        self.service.submit(OWNER, self.b, 'T', 'B', None)
        self.assertFalse(self.lock_free())
        self.service.close()
        self.assertTrue(self.lock_free())

    def test_unregistering_and_reregistering_keeps_requests(self):
        rid = self.service.submit(OWNER, self.a, 'T', 'B', None)['request_id']
        self.service.set_projects([ProjectRef(self.b, self.root_b, 'beta')])
        self.assertEqual(self.error(self.service.get, OWNER, rid).code, 'not_found')
        self.assertTrue(self.lock_free())
        self.service.set_projects([ProjectRef(self.a, self.root, 'alpha'), ProjectRef(self.b, self.root_b, 'beta')])
        self.assertEqual(self.service.get(OWNER, rid)['title'], 'T')


# =====================================================================================
# limits
# =====================================================================================
class LimitTests(Base):
    def snapshot(self):
        return self.dump(), self.file_state()

    def assertRefused(self, call, codes=('too_large', 'invalid_input')):
        err = self.error(call)
        self.assertIn(err.code, codes, err)
        return err

    def test_title_limit_counts_code_points_not_bytes(self):
        ok = [('a' * 200), ('가' * 200), ('😀' * 200), ('é' * 199 + '€')]
        for title in ok:
            with self.subTest(len=len(title), bytes=len(title.encode())):
                self.service.submit(OWNER, self.a, title, 'b', None)
        self.assertEqual(self.rows('SELECT count(*) FROM requests')[0][0], len(ok))
        before = self.snapshot()
        for title in ('a' * 201, '가' * 201, '😀' * 201):
            with self.subTest(len=len(title)):
                self.assertEqual(self.assertRefused(lambda: self.service.submit(OWNER, self.a, title, 'b',
                                                                               None)).code, 'too_large')
        self.assertEqual(self.snapshot(), before)

    def test_title_rejects_empty_non_text_and_control_characters(self):
        self.service.submit(OWNER, self.a, 'seed', 'b', None)
        before = self.snapshot()
        for title in ('', None, 5, b'bytes', 'x\x00y', 'x\ny', 'x\ty', 'x\ry', '\x1b[31m', 'x\x7fy',
                      'x\x85y', '\ud800'):
            with self.subTest(title=repr(title)):
                self.assertRefused(lambda: self.service.submit(OWNER, self.a, title, 'b', None),
                                   ('invalid_input',))
        self.assertEqual(self.snapshot(), before)
        self.service.submit(OWNER, self.a, 'tab and emoji 😀 and 한글', 'b', None)  # non-Cc is fine

    def test_body_limit_is_64_kib_of_utf8_bytes(self):
        self.service.submit(OWNER, self.a, 'seed', 'b', None)
        accepted = ['x' * 65536, '가' * 21845 + 'a', '가' * 21845, '😀' * 16384, 'a\nb\tc\r\n' * 100]
        for body in accepted:
            with self.subTest(bytes=len(body.encode())):
                out = self.service.submit(OWNER, self.a, 'T', body, None)
                self.assertEqual(self.row(out['request_id'])['body'], body)
        before = self.snapshot()
        for body in ('x' * 65537, '가' * 21846, '😀' * 16384 + 'a', '가' * 21845 + 'ab'):
            with self.subTest(bytes=len(body.encode())):
                self.assertEqual(self.assertRefused(lambda: self.service.submit(OWNER, self.a, 'T', body,
                                                                               None)).code, 'too_large')
        self.assertEqual(self.snapshot(), before)

    def test_body_rejects_empty_non_text_and_unencodable(self):
        before_exists = self.db_path().exists()
        for body in ('', None, 7, b'x', '\udfff'):
            with self.subTest(body=repr(body)):
                self.assertRefused(lambda: self.service.submit(OWNER, self.a, 'T', body, None),
                                   ('invalid_input',))
        self.assertEqual(self.db_path().exists(), before_exists)
        self.assertFalse(before_exists)

    def test_comment_limit_is_16_kib_of_utf8_bytes_for_every_comment_path(self):
        rid = self.make('REVIEWING')
        for body in ('x' * 16384, '가' * 5461 + 'a', '😀' * 4096):
            with self.subTest(bytes=len(body.encode())):
                self.service.comment(OWNER, rid, body)
                self.service.review(self.manager, rid, 'comment', body=body)
        before = self.snapshot()
        for body in ('x' * 16385, '가' * 5462, '😀' * 4096 + 'a'):
            for label, call in (
                    ('comment', lambda b=body: self.service.comment(OWNER, rid, b)),
                    ('progress', lambda b=body: self.service.review(self.manager, rid, 'comment', body=b)),
                    ('ask', lambda b=body: self.service.review(self.manager, rid, 'ask', body=b)),
                    ('reject', lambda b=body: self.service.review(self.manager, rid, 'reject', body=b)),
                    ('reason', lambda b=body: self.service.cancel(OWNER, rid, reason=b))):
                with self.subTest(path=label, bytes=len(body.encode())):
                    self.assertEqual(self.assertRefused(call).code, 'too_large')
        for body in ('', None, 3):
            self.assertRefused(lambda: self.service.comment(OWNER, rid, body), ('invalid_input',))
        self.assertEqual(self.snapshot(), before)

    def test_close_summary_limit_is_16_kib(self):
        rid = self.make('READY')
        before = self.snapshot()
        for summary in ('x' * 16385, '가' * 5462, '', None):
            with self.subTest(len=len(summary or '')):
                self.assertRefused(lambda: self.service.review(self.manager, rid, 'close', outcome='success',
                                                               summary=summary))
        for outcome in (None, 'rejected', 'cancelled', 'bogus'):
            self.assertEqual(self.error(self.service.review, self.manager, rid, 'close', outcome=outcome,
                                        summary='ok').code, 'invalid_input')
        self.assertEqual(self.snapshot(), before)
        self.service.review(self.manager, rid, 'close', outcome='failed', summary='x' * 16384)
        self.assertEqual(len(self.row(rid)['result_summary']), 16384)

    def test_query_limit_and_list_limit(self):
        self.new(OWNER)
        self.assertEqual(len(self.service.list_requests(OWNER, query='q' * 200)['requests']), 0)
        self.assertEqual(self.error(self.service.list_requests, OWNER, query='q' * 201).code, 'invalid_input')
        self.assertEqual(self.error(self.service.list_requests, OWNER, query=5).code, 'invalid_input')
        for bad in (0, -1, 101, True, '5', 2.5):
            with self.subTest(limit=bad):
                self.assertEqual(self.error(self.service.list_requests, OWNER, limit=bad).code, 'invalid_input')
        for good in (1, 50, 100, None):
            self.service.list_requests(OWNER, limit=good)
        self.assertEqual(self.error(self.service.list_requests, OWNER, states=['NOPE']).code, 'invalid_input')
        self.assertEqual(self.error(self.service.list_requests, OWNER, cursor='junk').code, 'invalid_input')

    def test_unknown_state_targets_and_review_actions_are_rejected(self):
        rid = self.make('NEW')
        before = self.dump()
        for to in ('READY', 'RUNNING', 'CLOSED', 'REVIEWING', 'new', None):
            self.assertEqual(self.error(self.service.set_state, OWNER, rid, self.version(rid), to).code,
                             'invalid_input')
        self.assertEqual(self.error(self.service.review, self.manager, rid, 'approve').code, 'invalid_input')
        self.assertEqual(self.dump(), before)


# =====================================================================================
# review mode, listing, counts
# =====================================================================================
class ReviewModeTests(Base):
    def test_review_mode_defaults_to_auto_and_persists_in_the_project_inbox(self):
        self.assertEqual(self.service.review_mode(OWNER, self.a)['review_mode'], 'auto')
        self.assertFalse(self.db_path().exists())
        self.assertEqual(self.service.review_mode(OWNER, self.a, set='manual')['review_mode'], 'manual')
        self.assertEqual(dict(self.rows('SELECT key, value FROM meta'))['review_mode'], 'manual')
        self.assertEqual(self.service.review_mode(OWNER, self.b)['review_mode'], 'auto')  # per project
        self.service.close()
        again = self.make_service()
        self.assertEqual(again.review_mode(OWNER, self.a)['review_mode'], 'manual')
        again.set_projects([ProjectRef(self.b, self.root_b, 'beta')])  # unregister
        again.set_projects([ProjectRef(self.a, self.root, 'alpha'), ProjectRef(self.b, self.root_b, 'beta')])
        self.assertEqual(again.review_mode(self.local, self.a)['review_mode'], 'manual')
        rid = again.submit(OWNER, self.a, 'T', 'B', None)['request_id']  # requests do not reset it
        self.assertEqual(dict(self.rows('SELECT key, value FROM meta'))['review_mode'], 'manual')
        self.assertEqual(again.get(OWNER, rid)['state'], 'NEW')

    def test_only_owner_and_local_may_change_it_and_values_are_checked(self):
        self.service.review_mode(self.local, self.a, set='manual')
        self.service.review_mode(OWNER, self.a, set='auto')
        before = self.dump()
        for principal in (self.board, self.editor, self.req1, self.manager):
            with self.subTest(principal=principal.kind):
                self.assertEqual(self.error(self.service.review_mode, principal, self.a, set='manual').code,
                                 'forbidden')
        for bad in ('', 'Auto', 'off', 'manual '):
            self.assertEqual(self.error(self.service.review_mode, OWNER, self.a, set=bad).code, 'invalid_input')
        self.assertEqual(self.dump(), before)
        self.assertEqual(dict(self.rows('SELECT key, value FROM meta'))['review_mode'], 'auto')

    def test_review_mode_change_is_audited_and_unchanged_value_adds_nothing(self):
        self.service.review_mode(OWNER, self.a, set='manual')
        count = self.rows("SELECT count(*) FROM events WHERE type = 'review_mode_changed'")[0][0]
        self.assertEqual(count, 1)
        self.service.review_mode(OWNER, self.a, set='manual')
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'review_mode_changed'")[0][0], 1)


class ListingTests(Base):
    def test_closed_is_hidden_by_default_reachable_by_filter_and_never_deleted(self):
        a1, a2, a3 = self.new(OWNER, 'one'), self.new(OWNER, 'two'), self.new(OWNER, 'three')
        self.service.cancel(OWNER, a2)
        listed = [r['request_id'] for r in self.service.list_requests(OWNER)['requests']]
        self.assertEqual(set(listed), {a1, a3})
        self.assertIn(a2, [r['request_id'] for r in self.service.list_requests(OWNER, include_closed=True)['requests']])
        self.assertEqual([r['request_id'] for r in self.service.list_requests(OWNER, states=['CLOSED'])['requests']],
                         [a2])
        self.assertEqual(self.rows('SELECT count(*) FROM requests')[0][0], 3)
        self.assertFalse([name for name in dir(InboxService) if 'delete' in name.lower() or 'purge' in name.lower()
                          or 'remove' in name.lower()])  # P2-AC-32: no automatic/explicit delete API

    def test_pending_requests_counts_every_open_state_and_ignores_closed(self):
        self.new(OWNER)
        self.make('REVIEWING')
        self.make('NEEDS_INFO')
        self.make('NEEDS_INFO')
        self.make('READY')
        self.make('RUNNING')
        self.make('CLOSED')
        self.assertEqual(self.service.pending_requests(self.a),
                         {'new': 1, 'reviewing': 1, 'needs_info': 2, 'ready': 1, 'running': 1})
        self.assertEqual(self.service.pending_requests(self.b),
                         {'new': 0, 'reviewing': 0, 'needs_info': 0, 'ready': 0, 'running': 0})
        self.assertIsNone(self.service.pending_requests(str(uuid.uuid4())))
        self.assertEqual(self.service.badge(OWNER), {'pending': 1, 'needs_info': 2})

    def test_badge_follows_state_changes_and_scope(self):
        rid = self.new(OWNER)
        self.assertEqual(self.service.badge(self.editor), {'pending': 1, 'needs_info': 0})
        self.service.set_state(OWNER, rid, 1, 'NEEDS_INFO')
        self.assertEqual(self.service.badge(self.editor), {'pending': 0, 'needs_info': 1})
        self.assertEqual(self.service.badge(self.editor_out), {'pending': 0, 'needs_info': 0})
        self.service.comment(OWNER, rid, 'answer')
        self.assertEqual(self.service.badge(OWNER), {'pending': 0, 'needs_info': 0})  # REVIEWING counts as neither
        self.new(OWNER, project=self.b)
        self.assertEqual(self.service.badge(OWNER), {'pending': 1, 'needs_info': 0})

    def test_search_matches_title_and_body_case_insensitively_and_pages_newest_first(self):
        ids = []
        for i in range(5):
            self.now[0] += 1
            ids.append(self.new(OWNER, f'Alpha {i}', f'body {i} Needle' if i % 2 == 0 else 'nothing'))
        page1 = self.service.list_requests(OWNER, limit=2)
        self.assertEqual([r['request_id'] for r in page1['requests']], [ids[4], ids[3]])
        page2 = self.service.list_requests(OWNER, limit=2, cursor=page1['next_cursor'])
        self.assertEqual([r['request_id'] for r in page2['requests']], [ids[2], ids[1]])
        page3 = self.service.list_requests(OWNER, limit=2, cursor=page2['next_cursor'])
        self.assertEqual([r['request_id'] for r in page3['requests']], [ids[0]])
        self.assertIsNone(page3['next_cursor'])
        hits = self.service.list_requests(OWNER, query='NEEDLE')['requests']
        self.assertEqual({r['request_id'] for r in hits}, {ids[0], ids[2], ids[4]})
        self.assertEqual({r['request_id'] for r in self.service.list_requests(OWNER, query='alpha 3')['requests']},
                         {ids[3]})

    def test_origin_label_is_shown_in_listings(self):
        self.service.submit(self.editor, self.a, 'T', 'B', None)
        self.service.submit(self.board, self.a, 'T2', 'B', None)
        labels = sorted(r['origin']['label'] for r in self.service.list_requests(OWNER)['requests'])
        self.assertEqual(labels, ['Board agent', 'key:ed'])


class BackupTests(Base):
    def test_update_backup_snapshot_includes_the_committed_inbox_with_its_wal_changes(self):
        from workbench.backend.update_backup import snapshot
        rid = self.make('REVIEWING')
        self.service.comment(OWNER, rid, 'latest change, possibly still only in the WAL')
        self.service.close()
        self.assertTrue(self.lock_free())
        target = self.base / 'backup'
        result = snapshot(self.root / '.wb', target)
        self.assertIn('inbox.sqlite3', {entry['path'] for entry in result['files']})
        connection = sqlite3.connect(f'file:{target / "inbox.sqlite3"}?mode=ro', uri=True)
        try:
            self.assertEqual(connection.execute('SELECT state FROM requests WHERE request_id = ?',
                                                (rid,)).fetchone(), ('REVIEWING',))
            self.assertEqual(connection.execute('SELECT count(*) FROM comments WHERE request_id = ?',
                                                (rid,)).fetchone()[0], 1)
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
