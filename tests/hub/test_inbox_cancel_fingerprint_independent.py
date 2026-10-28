"""Independent verification of the cancel confirm fingerprint (SPEC "취소", BRIEF P2-AC-41, user decision 2026-10-26).

Contract, taken from the SPEC and the decision rather than from the implementation: a cancel confirm token (REST
``POST /api/v1/requests/{id}/cancel`` and the web cancel confirm of a request that links a Task) is bound to user-meaningful state only --
request ``state``, ``revision``, ``task_id``, ``spec_revision``, ``cancel_state``, ``title``, ``body``, the number of
people-written (non-``system``) comments, the linked Task's ``status`` and ``closed`` flag, and the running
``command_id``. Notice-ledger bookkeeping (``delivery_*``, system ledger comments), the request ``version`` /
``updated_at`` and Task view refreshes that keep status, closed and command_id must NOT invalidate it. A bound change
between issue and use gives ``token_mismatch`` and nothing is cancelled; the comparison is inside the writer
transaction. A web pre-run cancel without a Task still compares ``base_version``.

Seams: the real loopback Hub with ``/api/v1/`` and the stdlib REST client (``support.rest_hub``), the real
``requests`` web pages over a real inbox store, and ``InboxService`` itself. Ledger and Task changes are driven through
the real ``Dispatcher`` (``tick()``) with a fake backend port (notice outcomes via ``record_delivery`` as the backend
reports them), the service's manager/owner API and the REST endpoints -- not by writing SQL. The oracle of "what is
user-meaningful" is computed here from the stored rows (``user_state``), so a scenario that does not really isolate a
ledger-only change (or really change a bound field) fails the test itself. A backend cancel is one committed
``cancel_requested`` event. Run with ``discover -s tests/hub -p 'test_inbox_cancel_fingerprint_independent.py'``.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support.rest_hub import RestHub  # noqa: E402
from support.rest_tools import ERROR_STATUS, RestTools  # noqa: E402  (REST calls named by operation)
from test_inbox_service import make_project  # noqa: E402
from test_web_pages_requests import Page, RequestsCase  # noqa: E402
from workbench.hub.inbox.dispatch import Dispatcher  # noqa: E402
from workbench.hub.inbox.service import (CommittedEvent, InboxError, InboxService, Principal,  # noqa: E402
                                        ProjectRef, cancel_fingerprint)

OWNER = Principal.owner()
CMD1 = {'command_id': 'cmd-1', 'owner': 'worker', 'label': 'python train.py', 'state': 'running'}
CMD2 = {'command_id': 'cmd-2', 'owner': 'worker', 'label': 'python train.py', 'state': 'running'}
MISMATCH_TEXT = '작업이 바뀌었습니다'


# -- an oracle of "user-meaningful state", read from the stored rows ------------------------------------------------
def snap(root: Path, request_id: str) -> dict:
    connection = sqlite3.connect(f'file:{root / ".wb" / "inbox.sqlite3"}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = dict(connection.execute('SELECT * FROM requests WHERE request_id = ?', (request_id,)).fetchone())
        count = lambda sql: connection.execute(sql, (request_id,)).fetchone()[0]  # noqa: E731
        row['people'] = count("SELECT count(*) FROM comments WHERE request_id = ? AND kind != 'system'")
        row['system'] = count("SELECT count(*) FROM comments WHERE request_id = ? AND kind = 'system'")
        row['events'] = count('SELECT count(*) FROM events WHERE request_id = ?')
        return row
    finally:
        connection.close()


def user_state(row: dict) -> tuple:
    view = json.loads(row['task_view']) if row['task_view'] else {}
    command = view.get('command')
    return (row['state'], row['revision'], row['title'], row['body'], row['people'], row['task_id'],
            row['spec_revision'], row['cancel_state'], view.get('status'), bool(view.get('closed')),
            command.get('command_id') if isinstance(command, dict) else None)


def ledger(row: dict) -> tuple:
    return (row['delivery_state'], row['delivery_id'], row['delivery_backend'], row['delivery_at'], row['system'],
            row['task_view'], row['updated_at'])


# -- a fake backend port for the real dispatcher ------------------------------------------------------------------
class Port:
    def __init__(self):
        self.identity_value = '100:5'
        self.notices: list[dict] = []
        self.tasks: dict[str, dict] = {}

    def identity(self, root):
        return self.identity_value

    def inbox_notice(self, root, request_id, delivery_id, event, fields, **_):
        self.notices.append({'request_id': request_id, 'delivery_id': delivery_id, 'event': event})
        return {'ok': True, 'queued': True, 'notice_id': f'n-{len(self.notices)}'}

    def task_status(self, root, task_ids, **_):
        return [{'task_id': task_id, 'exists': True, **self.tasks[task_id]} for task_id in task_ids
                if task_id in self.tasks]


def task(status='running', command=CMD1, *, closed_reason=None, revision=1, runs=()):
    return {'status': status, 'closed_reason': closed_reason, 'revision': revision, 'runs': list(runs),
            'command': dict(command) if command else None, 'cancel_requested_by': None}


class World:
    """A project whose requests are driven by the real dispatcher against :class:`Port`."""

    def init_world(self, inbox: InboxService, project_id: str, root: Path) -> None:
        self.inbox, self.pid, self.root = inbox, project_id, root
        self.fake = Port()
        self.clock = [100.0]
        refs = [ProjectRef(project_id, root, 'alpha')]
        self.dispatcher = Dispatcher(inbox, lambda: refs, port=self.fake, clock=lambda: self.clock[0],
                                     sync_workers=0)
        self.manager = Principal.manager(project_id)

    def advance_time(self) -> None:  # a transport whose service clock is fixed moves it here
        pass

    def tick(self) -> None:
        self.clock[0] += 2.0
        self.advance_time()
        self.dispatcher.committed(CommittedEvent('test', self.pid, None, None, None, 'hub'))
        self.dispatcher.tick()

    def deliver(self, request_id: str, outcome: str = 'delivered') -> None:
        notice = [n for n in self.fake.notices if n['request_id'] == request_id][-1]
        self.inbox.record_delivery(request_id, notice['delivery_id'], outcome)

    def snap(self, request_id: str) -> dict:
        return snap(self.root, request_id)

    def run_task(self, request_id: str, task_id='task-1', status='running', command=CMD1, spec_revision=1) -> None:
        """Link a Task the way the manager does, then let the Task sync record its view."""
        token = self.inbox.link_check(self.manager, request_id)['link_token']
        self.inbox.link_commit(self.manager, token, task_id, spec_revision, status)
        self.fake.tasks[task_id] = task(status, command)
        self.tick()

    def set_task(self, task_id='task-1', **fields) -> None:
        self.fake.tasks[task_id] = task(**fields)
        self.tick()

    # -- the scenario tables: (name, prepare before the token, change after it) --------------------------------------
    def closed_task(self, rid):
        self.set_task(status='closed', closed_reason='finished')  # a linked_task_closed round is now pending

    def ask(self, rid):
        self.inbox.review(self.manager, rid, 'ask', body='which data?')

    def excluded(self) -> list[tuple]:
        def notice_delivered(rid):
            self.tick()  # reserve + offer
            self.deliver(rid)

        def notice_rejected_then_retried(rid):
            self.tick()
            self.deliver(rid, 'rejected')  # system comment, pending again
            self.tick()
            self.deliver(rid, 'delivered')

        def notice_dropped(rid):
            self.tick()
            self.deliver(rid, 'dropped')

        def notice_answer_lost(rid):
            self.tick()
            self.deliver(rid, 'unknown')  # system comment

        def backend_restart(rid):
            self.tick()
            self.fake.identity_value = '200:9'  # the offered notice is requeued (system comment) and sent again
            self.tick()
            self.deliver(rid)

        def view_refresh(rid, **status):
            self.fake.tasks['task-1'] = task(command=dict(CMD1, label='python train.py --resume', state='stopping'),
                                             revision=2, runs=[{'run_id': 'run-1', 'status': 'running',
                                                               'exit_code': None}], **status)
            self.tick()

        def ledger_then_view_refresh(rid):  # the Task stays closed through both
            notice_delivered(rid)
            view_refresh(rid, status='closed', closed_reason='finished')

        return [('notice delivered', self.closed_task, notice_delivered),
                ('notice rejected then retried', self.closed_task, notice_rejected_then_retried),
                ('notice dropped', self.closed_task, notice_dropped),
                ('notice answer lost', self.closed_task, notice_answer_lost),
                ('backend restart requeues the notice', self.closed_task, backend_restart),
                ('view refresh keeping status, closed and command', lambda rid: None, view_refresh),
                ('notice then view refresh', self.closed_task, ledger_then_view_refresh)]

    def bound(self) -> list[tuple]:
        def task_id_changes(rid):  # NEEDS_INFO keeps the old Task; the manager links a new one
            token = self.inbox.link_check(self.manager, rid)['link_token']
            self.inbox.link_commit(self.manager, token, 'task-2', 2, 'running')
            self.fake.tasks['task-2'] = task()
            self.tick()

        def command_restarts(rid):  # the command ends, a retry starts under a new id
            self.set_task(command=None)
            self.set_task(command=CMD2)

        def in_place(rid, **fields):
            self.inbox.update(OWNER, rid, self.snap(rid)['version'], **fields)

        def no_command(rid):
            self.set_task(command=None)

        return [
            ('state: the manager asks', lambda rid: None, self.ask),
            ('revision: an edit while running', lambda rid: None,
             lambda rid: in_place(rid, body='use the new data')),
            ('title edited in place', self.ask, lambda rid: in_place(rid, title='A different title')),
            ('body edited in place', self.ask, lambda rid: in_place(rid, body='A different body')),
            ('owner comment', lambda rid: None, lambda rid: self.inbox.comment(OWNER, rid, 'one more thing')),
            ('manager progress comment', lambda rid: None,
             lambda rid: self.inbox.review(self.manager, rid, 'comment', body='halfway there')),
            ('task status', lambda rid: None, lambda rid: self.set_task(status='held')),
            ('task closed', lambda rid: None,
             lambda rid: self.set_task(status='closed', closed_reason='finished')),
            ('command id', lambda rid: None, lambda rid: self.set_task(command=CMD2)),
            ('command ended', lambda rid: None, no_command),
            ('command appears', no_command, lambda rid: self.set_task(command=CMD1)),
            ('command ends and a retry starts', lambda rid: None, command_restarts),
            ('cancel_state: another cancel', lambda rid: None, lambda rid: self.inbox.cancel(OWNER, rid)),
            ('task_id and spec_revision: a relink', self.ask, task_id_changes),
        ]

    def prepared(self, prepare) -> str:
        """A fresh RUNNING request with ``task-1`` / ``cmd-1`` (``prepare`` applied, view synced)."""
        rid = self.new_request(f'Job {len(self.fake.tasks)}-{id(prepare) % 9973}', 'Run the sweep')
        self.run_task(rid)
        prepare(rid)
        return rid


def slug(name: str) -> str:
    return ''.join(ch if ch.isalnum() else '_' for ch in name).strip('_')


def per_scenario(cls):
    """One test method per scenario, so each runs in a fresh project (the dispatcher keeps at most one notice offered
    per project: scenarios must not leave rounds behind for each other)."""
    probe = World()
    asynchronous = issubclass(cls, unittest.IsolatedAsyncioTestCase)
    for kind, table in (('excluded', probe.excluded()), ('bound', probe.bound())):
        for name, _, _ in table:
            if asynchronous:
                async def test(self, name=name, kind=kind):
                    await self.acheck(kind, name)
            else:
                def test(self, name=name, kind=kind):
                    getattr(self, f'check_{kind}')(name)
            test.__name__ = f'test_{"ledger_only_keeps_the_token" if kind == "excluded" else "bound_change_refuses"}__{slug(name)}'
            setattr(cls, test.__name__, test)
    for name in ('comment', 'status'):
        if asynchronous:
            async def noise(self, name=name):
                await self.acheck('noise', name)
        else:
            def noise(self, name=name):
                self.check_noise(name)
        noise.__name__ = f'test_ledger_noise_does_not_mask_a_real_{name}_change'
        setattr(cls, noise.__name__, noise)
    return cls


class Scenarios:
    """Transport-independent tests over ``issue`` / ``use`` / ``cancels`` of the concrete case."""

    def test_an_unchanged_request_confirms_once(self):
        rid = self.prepared(lambda rid: None)
        handle = self.issue(rid)
        self.assertEqual(self.cancels(), 0)
        self.assertEqual(self.use(rid, handle), 'ok')
        self.assertEqual(self.cancels(), 1)
        self.assertEqual(self.snap(rid)['cancel_state'], 'requested')

    def check_excluded(self, name):
        prepare, change = dict((n, (p, c)) for n, p, c in self.excluded())[name]
        rid = self.prepared(prepare)
        handle = self.issue(rid)
        before = self.snap(rid)
        change(rid)
        after = self.snap(rid)
        # the scenario really moved only the ledger / view detail ...
        self.assertGreater(after['version'], before['version'], 'the version moved')
        self.assertNotEqual(ledger(after), ledger(before), 'the ledger moved')
        self.assertEqual(user_state(after), user_state(before), 'no user-meaningful field moved')
        # ... and the honest confirm still goes through, once
        stops = self.cancels()
        self.assertEqual(self.use(rid, handle), 'ok')
        self.assertEqual(self.cancels(), stops + 1)
        self.assertEqual(self.snap(rid)['cancel_state'], 'requested')

    def check_bound(self, name):
        prepare, change = dict((n, (p, c)) for n, p, c in self.bound())[name]
        rid = self.prepared(prepare)
        handle = self.issue(rid)
        before = self.snap(rid)
        change(rid)
        changed = self.snap(rid)
        self.assertNotEqual(user_state(changed), user_state(before), 'a user-meaningful field moved')
        stops = self.cancels()
        self.assertEqual(self.use(rid, handle), 'token_mismatch')
        self.assertEqual(self.cancels(), stops)
        self.assertEqual(self.snap(rid), changed, 'a refused confirm writes nothing')

    def check_noise(self, name):
        change = {'comment': lambda rid: self.inbox.comment(OWNER, rid, 'one more thing'),
                  'status': lambda rid: self.set_task(status='held')}[name]
        rid = self.prepared(self.closed_task)
        handle = self.issue(rid)
        self.tick()
        self.deliver(rid)  # ledger moves first ...
        change(rid)  # ... then a real change
        stops = self.cancels()
        self.assertEqual(self.use(rid, handle), 'token_mismatch')
        self.assertEqual(self.cancels(), stops)

    def test_a_token_is_single_use(self):
        rid = self.prepared(lambda rid: None)
        handle = self.issue(rid)
        self.assertEqual(self.use(rid, handle), 'ok')
        self.assertNotEqual(self.use(rid, handle), 'ok')
        self.assertEqual(self.cancels(), 1)


# -- transport 1: the service (the writer-transaction compare) ----------------------------------------------------
@per_scenario
class ServiceCase(Scenarios, World, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-fp-svc-')
        self.addCleanup(self.temp.cleanup)
        root, project_id = make_project(Path(self.temp.name).resolve(), 'alpha')
        self.events: list = []
        self.service = InboxService(on_committed=self.events.append, backend_running=lambda project: True)
        self.addCleanup(self.service.close)
        self.service.set_projects([ProjectRef(project_id, root, 'alpha')])
        self.init_world(self.service, project_id, root)

    def new_request(self, title='Job', body='Body') -> str:
        return self.service.submit(OWNER, self.pid, title, body, None)['request_id']

    def issue(self, rid):
        return cancel_fingerprint(self.service.get(OWNER, rid))

    def use(self, rid, handle):
        try:
            self.service.cancel(OWNER, rid, base_fingerprint=handle)
        except InboxError as exc:
            return 'token_mismatch' if exc.code == 'conflict' else exc.code
        return 'ok'

    def cancels(self) -> int:
        return len([event for event in self.events if event.type == 'cancel_requested'])

    def test_the_writer_compare_is_not_skipped_by_an_empty_or_absent_fingerprint(self):
        rid = self.prepared(lambda rid: None)
        before = self.snap(rid)
        error = None
        try:
            self.service.cancel(OWNER, rid, base_fingerprint='')
        except InboxError as exc:
            error = exc
        self.assertIsNotNone(error)
        self.assertEqual(error.code, 'conflict')
        self.assertEqual(self.snap(rid), before)
        self.assertEqual(self.cancels(), 0)

    def test_a_stale_fingerprint_cannot_cancel_a_pre_run_request_either(self):
        rid = self.new_request('Plan it')
        stale = self.issue(rid)
        self.service.update(OWNER, rid, self.snap(rid)['version'], title='Plan it better')
        self.assertEqual(self.use(rid, stale), 'token_mismatch')
        self.assertEqual(self.snap(rid)['state'], 'NEW')

    def test_the_fingerprint_of_the_read_and_the_writer_row_agree_in_every_shape(self):
        # the value checked in the writer transaction is computed from the stored row, the token from ``get``: they
        # must agree whatever the request looks like (system comments, closed Task, no view, unicode, no Task)
        shapes = {}
        rid = self.prepared(self.closed_task)  # first: a pending notice would block the others' rounds
        self.tick()
        self.deliver(rid, 'rejected')
        self.tick()
        self.deliver(rid, 'delivered')
        shapes['closed task with system comments'] = rid
        shapes['pre-run, no task'] = self.new_request('계획 세우기', '본문 ✓')
        shapes['running'] = self.prepared(lambda rid: None)
        shapes['needs_info with task'] = self.prepared(self.ask)
        shapes['no command'] = self.prepared(lambda rid: self.set_task(command=None))
        for name, rid in shapes.items():
            with self.subTest(name):
                self.assertEqual(self.use(rid, self.issue(rid)), 'ok')


# -- the pure function --------------------------------------------------------------------------------------------
class FunctionCase(World, unittest.TestCase):
    """``cancel_fingerprint`` on ``InboxService.get`` output: each field, alone."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-fp-fn-')
        self.addCleanup(self.temp.cleanup)
        root, project_id = make_project(Path(self.temp.name).resolve(), 'alpha')
        self.service = InboxService(backend_running=lambda project: True)
        self.addCleanup(self.service.close)
        self.service.set_projects([ProjectRef(project_id, root, 'alpha')])
        self.init_world(self.service, project_id, root)
        self.rid = self.service.submit(OWNER, project_id, 'Job', 'Run the sweep', None)['request_id']
        self.run_task(self.rid)
        self.base = self.service.get(OWNER, self.rid)

    def new_request(self, title='Job', body='Body') -> str:
        return self.service.submit(OWNER, self.pid, title, body, None)['request_id']

    def mutated(self, change) -> str:
        detail = copy.deepcopy(self.base)
        change(detail)
        return cancel_fingerprint(detail)

    def test_the_view_shape_is_the_one_the_tests_assume(self):
        self.assertEqual(self.base['state'], 'RUNNING')
        self.assertEqual(self.base['task']['view']['command']['command_id'], 'cmd-1')
        self.assertTrue(self.base['comments'] == [] or all(c['kind'] != 'system' for c in self.base['comments']))

    def test_each_bound_field_alone_changes_it(self):
        def comment(kind):
            return lambda d: d['comments'].append({'kind': kind, 'body': 'x', 'comment_id': 'c', 'seq': 99})
        changes = {
            'state': lambda d: d.update(state='NEEDS_INFO'),
            'revision': lambda d: d.update(revision=d['revision'] + 1),
            'task_id': lambda d: d['task'].update(task_id='task-2'),
            'spec_revision': lambda d: d['task'].update(spec_revision=(d['task']['spec_revision'] or 0) + 1),
            'cancel_state': lambda d: d.update(cancel_state='requested'),
            'title': lambda d: d.update(title='Other'),
            'body': lambda d: d.update(body='Other'),
            'task status': lambda d: d['task']['view'].update(status='held'),
            'task closed': lambda d: d['task']['view'].update(closed=True),
            'command_id': lambda d: d['task']['view']['command'].update(command_id='cmd-9'),
            'command removed': lambda d: d['task']['view'].update(command=None),
        }
        for kind in ('comment', 'answer', 'question', 'progress', 'decision'):
            changes[f'{kind} comment'] = comment(kind)
        original = cancel_fingerprint(self.base)
        seen = {original}
        for name, change in changes.items():
            with self.subTest(name):
                changed = self.mutated(change)
                self.assertNotEqual(changed, original)
                seen.add(changed)
        self.assertGreaterEqual(len(seen), len(changes) - 4)  # distinct changes are not folded into one value

    def test_each_excluded_field_alone_leaves_it(self):
        changes = {
            'version': lambda d: d.update(version=d['version'] + 7),
            'updated_at': lambda d: d.update(updated_at=d['updated_at'] + 123.5),
            'delivery_state': lambda d: d.update(delivery_state='offered'),
            'delivery_id': lambda d: d.update(delivery_id='d-9'),
            'delivery_backend': lambda d: d.update(delivery_backend='1:1'),
            'review_round': lambda d: d.update(review_round=(d.get('review_round') or 0) + 3),
            'system comment': lambda d: d['comments'].append({'kind': 'system', 'body': 'x', 'comment_id': 's',
                                                              'seq': 98}),
            'view runs': lambda d: d['task']['view'].update(runs=[{'run_id': 'run-1', 'status': 'running'}]),
            'command label': lambda d: d['task']['view']['command'].update(label='another label'),
            'command state': lambda d: d['task']['view']['command'].update(state='stopping'),
            'view revision': lambda d: d['task']['view'].update(revision=9),
            'comment timestamps': lambda d: d.update(created_at=0.0),
        }
        original = cancel_fingerprint(self.base)
        for name, change in changes.items():
            with self.subTest(name):
                self.assertEqual(self.mutated(change), original)

    def test_key_order_of_the_view_does_not_matter(self):
        reordered = copy.deepcopy(self.base)
        view = reordered['task']['view']
        reordered['task']['view'] = dict(reversed(list(view.items())))
        self.assertEqual(cancel_fingerprint(reordered), cancel_fingerprint(self.base))

    def test_it_is_stable_across_processes_and_hash_seeds(self):
        code = ('import json,sys\nfrom workbench.hub.inbox.service import cancel_fingerprint\n'
                'print(cancel_fingerprint(json.loads(sys.argv[1])))')
        detail = copy.deepcopy(self.base)
        values = set()
        for seed in ('0', '1', '4242'):
            done = subprocess.run([sys.executable, '-c', code, json.dumps(detail)], capture_output=True,
                                  text=True, timeout=60, check=False, cwd=self.temp.name,
                                  env={'PYTHONHASHSEED': seed, 'PYTHONPATH': str(REPO / 'src'),
                                       'PYTHONDONTWRITEBYTECODE': '1', 'PATH': os.environ.get('PATH', '')})
            self.assertEqual(done.returncode, 0, done.stderr)
            values.add(done.stdout.strip())
        self.assertEqual(len(values), 1, values)
        self.assertEqual(values, {cancel_fingerprint(self.base)})

    def test_unicode_and_equal_looking_text_stay_distinct(self):
        original = self.mutated(lambda d: d.update(title='é'))
        decomposed = self.mutated(lambda d: d.update(title='é'))
        self.assertNotEqual(original, decomposed)  # what the user saw differs: not folded


# -- transport 2: REST ----------------------------------------------------------------------------------------------
@per_scenario
class RestCase(Scenarios, World, unittest.TestCase):
    def setUp(self):
        self.hub = RestHub(self)
        self.r1 = RestTools(self.hub.rest(self.hub.key('r1', 'requester', {'projects': [self.hub.a]})))
        self.ed = RestTools(self.hub.rest(self.hub.key('ed', 'editor', {'projects': [self.hub.a]})))
        self.ed2 = RestTools(self.hub.rest(self.hub.key('ed2', 'editor', {'projects': [self.hub.a]})))
        self.idem = 0
        self.init_world(self.hub.inbox, self.hub.a, self.hub.root_a)

    def advance_time(self) -> None:
        self.hub.now[0] += 3.0

    def new_request(self, title='Job', body='Body') -> str:
        self.idem += 1
        return self.r1.tool('inbox_submit', project_id=self.hub.a, title=title, body=body,
                            idempotency_key=f'k-{self.idem}')['request_id']

    def issue(self, rid, client=None):
        value = (client or self.r1).tool('inbox_cancel', request_id=rid)
        self.assertEqual(value['status'], 'confirm_required')
        return value['confirm_token']

    def use(self, rid, handle, client=None):
        reply = (client or self.r1).call('inbox_cancel', request_id=rid, confirm_token=handle)
        if reply.status < 400:
            self.assertEqual((reply.status, reply.json['status']), (202, 'cancel_requested'))  # SPEC: 202 on a token confirm
            return 'ok'
        self.assertEqual(reply.status, ERROR_STATUS[reply.error['code']])  # SPEC: token_mismatch is 409
        return reply.error['code']

    def cancels(self) -> int:
        return len(self.hub.cancels)

    def test_a_requesters_and_an_editors_comment_through_rest_refuse_the_token(self):
        for name, comment in (('requester', lambda rid: self.r1.tool('inbox_comment', request_id=rid, body='more')),
                              ('editor', lambda rid: self.ed.tool('inbox_comment', request_id=rid, body='more'))):
            with self.subTest(name):
                rid = self.prepared(lambda rid: None)
                handle = self.issue(rid)
                comment(rid)
                self.assertEqual(self.use(rid, handle), 'token_mismatch')
                self.assertEqual(self.cancels(), 0)

    def test_an_editors_update_through_rest_refuses_the_token(self):
        rid = self.prepared(lambda rid: None)
        handle = self.issue(rid)
        self.ed.tool('inbox_update', request_id=rid, base_version=self.snap(rid)['version'], title='Renamed')
        self.assertEqual(self.use(rid, handle), 'token_mismatch')
        self.assertEqual(self.cancels(), 0)

    def test_a_token_of_one_request_cannot_cancel_another_even_if_everything_else_is_equal(self):
        # the same title, body, Task id and command: only the request id differs
        first = self.new_request('Twin', 'Same body')
        second = self.new_request('Twin', 'Same body')
        self.run_task(first)
        self.run_task(second)
        self.assertEqual(user_state(self.snap(first)), user_state(self.snap(second)))
        handle = self.issue(first)
        before = self.snap(second)
        self.assertEqual(self.use(second, handle), 'token_mismatch')
        self.assertEqual(self.cancels(), 0)
        self.assertEqual(self.snap(second), before)
        self.assertEqual(self.snap(second)['cancel_state'], None)

    def test_a_token_is_bound_to_the_key_that_asked(self):
        rid = self.prepared(lambda rid: None)
        handle = self.issue(rid, self.ed)
        self.assertEqual(self.use(rid, handle, self.ed2), 'token_mismatch')  # another editor key
        self.assertEqual(self.use(rid, handle, self.r1), 'token_mismatch')  # the requester who owns the request
        self.assertEqual(self.cancels(), 0)
        self.assertEqual(self.snap(rid)['cancel_state'], None)
        handle = self.issue(rid, self.r1)
        self.assertEqual(self.use(rid, handle, self.ed), 'token_mismatch')  # and the other way round
        self.assertEqual(self.cancels(), 0)

    def test_a_ledger_change_between_the_check_and_the_commit_keeps_the_token(self):
        self.late(self.closed_task, lambda rid: (self.tick(), self.deliver(rid)), 'ok', 1)

    def test_a_real_change_between_the_check_and_the_commit_refuses_it(self):
        self.late(lambda rid: None, lambda rid: self.set_task(status='held'), 'token_mismatch', 0)
        self.late(lambda rid: None, lambda rid: self.inbox.comment(OWNER, rid, 'late'), 'token_mismatch', 0)

    def late(self, prepare, change, expected, cancels):
        rid = self.prepared(prepare)
        handle = self.issue(rid)
        real = self.inbox.cancel
        before = self.snap(rid)

        def late_change(*args, **kwargs):
            change(rid)  # after the server's own read and compare, before the writer transaction
            return real(*args, **kwargs)
        with mock.patch.object(self.inbox, 'cancel', side_effect=late_change):
            self.assertEqual(self.use(rid, handle), expected)
        self.assertEqual(self.cancels(), cancels)
        self.assertEqual(self.snap(rid)['cancel_state'], 'requested' if cancels else before['cancel_state'])


# -- transport 3: the web confirm page and POST ----------------------------------------------------------------------
@per_scenario
class WebCase(Scenarios, World, RequestsCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.backend_up = True
        self.init_world(self.inbox, self.a, self.root_a)

    # The shared (sync) scenario tests drive a web page, which needs the loop: the async tests below repeat the
    # loops with ``await``; the inherited sync versions are replaced here.
    test_an_unchanged_request_confirms_once = None
    test_a_token_is_single_use = None

    def new_request(self, title='Job', body='Body') -> str:
        return self.add(title, body)

    def cancels(self) -> int:
        return len([event for event in self.events if event.type == 'cancel_requested'])

    async def issue(self, rid, cookies=None):
        response = await self.get(f'/requests/{rid}/cancel', cookies=cookies or self.cookies)
        self.assertEqual(response.status, 200)
        form = Page(response.body_text).form(f'/requests/{rid}/cancel')
        self.assertIsNotNone(form)
        self.assertIn('token', form['fields'])
        return {name: value for name, value in form['fields'].items() if value is not None}

    async def use(self, rid, fields, cookies=None):
        response = await self.post(f'/requests/{rid}/cancel', fields, cookies=cookies or self.cookies)
        if response.status == 303:
            self.assertEqual(urlsplit(response.headers['Location']).query, 'notice=cancel_requested')
            return 'ok'
        self.assertEqual(response.status, 409, response.body_text)
        self.assertIn(MISMATCH_TEXT, response.body_text)
        return 'token_mismatch'

    async def test_an_unchanged_request_confirms_once_on_the_web(self):
        rid = self.prepared(lambda rid: None)
        fields = await self.issue(rid)
        self.assertEqual(await self.use(rid, fields), 'ok')
        self.assertEqual(self.cancels(), 1)
        self.assertEqual(self.snap(rid)['cancel_state'], 'requested')
        self.assertEqual(await self.use(rid, fields), 'token_mismatch')  # single use
        self.assertEqual(self.cancels(), 1)

    async def acheck(self, kind, name):
        if kind == 'excluded':
            prepare, change = dict((n, (p, c)) for n, p, c in self.excluded())[name]
        elif kind == 'bound':
            prepare, change = dict((n, (p, c)) for n, p, c in self.bound())[name]
        else:
            prepare = self.closed_task
            change = {'comment': lambda rid: self.inbox.comment(OWNER, rid, 'one more thing'),
                      'status': lambda rid: self.set_task(status='held')}[name]
        rid = self.prepared(prepare)
        fields = await self.issue(rid)
        before = self.snap(rid)
        if kind == 'noise':
            self.tick()
            self.deliver(rid)
        change(rid)
        after = self.snap(rid)
        stops = self.cancels()
        if kind == 'excluded':
            self.assertGreater(after['version'], before['version'])
            self.assertNotEqual(ledger(after), ledger(before))
            self.assertEqual(user_state(after), user_state(before))
            # the form still carries the old base_version: the linked path must not compare it
            self.assertNotEqual(int(fields['base_version']), after['version'])
            self.assertEqual(await self.use(rid, fields), 'ok')
            self.assertEqual(self.cancels(), stops + 1)
            self.assertEqual(self.snap(rid)['cancel_state'], 'requested')
        else:
            self.assertNotEqual(user_state(after), user_state(before))
            self.assertEqual(await self.use(rid, fields), 'token_mismatch')
            self.assertEqual(self.cancels(), stops)
            self.assertEqual(self.snap(rid), after)

    async def test_a_token_of_one_request_cannot_cancel_another_even_if_everything_else_is_equal(self):
        first = self.new_request('Twin', 'Same body')
        second = self.new_request('Twin', 'Same body')
        self.run_task(first)
        self.run_task(second)
        self.assertEqual(user_state(self.snap(first)), user_state(self.snap(second)))
        fields = await self.issue(first)
        before = self.snap(second)
        self.assertEqual(await self.use(second, fields), 'token_mismatch')
        self.assertEqual(self.cancels(), 0)
        self.assertEqual(self.snap(second), before)

    async def test_a_token_is_bound_to_the_session(self):
        rid = self.prepared(lambda rid: None)
        fields = await self.issue(rid)
        other_cookie = await self.session_cookie()
        other = {'wb_hub_session': other_cookie}
        response = await self.get('/requests/new', cookies=other)
        csrf = Page(response.body_text).form('/requests')['fields']['csrf']
        forged = dict(fields, csrf=csrf)
        self.assertEqual(await self.use(rid, forged, other), 'token_mismatch')
        self.assertEqual(self.cancels(), 0)
        self.assertEqual(self.snap(rid)['cancel_state'], None)

    async def test_a_ledger_change_between_the_check_and_the_commit_keeps_the_token(self):
        await self.late(self.closed_task, lambda rid: (self.tick(), self.deliver(rid)), 'ok', 1)

    async def test_a_real_change_between_the_check_and_the_commit_refuses_it(self):
        await self.late(lambda rid: None, lambda rid: self.set_task(status='held'), 'token_mismatch', 0)
        await self.late(lambda rid: None, lambda rid: self.inbox.comment(OWNER, rid, 'late'), 'token_mismatch', 0)

    async def late(self, prepare, change, expected, cancels):
        rid = self.prepared(prepare)
        fields = await self.issue(rid)
        before = self.snap(rid)
        real = self.inbox.cancel

        def late_change(*args, **kwargs):
            change(rid)
            return real(*args, **kwargs)
        self.inbox.cancel = late_change
        stops = self.cancels()
        try:
            self.assertEqual(await self.use(rid, fields), expected)
        finally:
            del self.inbox.cancel
        self.assertEqual(self.cancels(), stops + cancels)
        self.assertEqual(self.snap(rid)['cancel_state'], 'requested' if cancels else before['cancel_state'])

    async def test_a_ready_request_keeps_its_token_over_a_view_refresh_and_loses_it_when_the_task_starts(self):
        for starts in (False, True):
            with self.subTest(starts=starts):
                rid = self.new_request(f'Ready {starts}', 'b')
                token = self.inbox.link_check(self.manager, rid)['link_token']
                self.inbox.link_commit(self.manager, token, f'task-r{starts}', 1, 'dispatched')
                self.fake.tasks[f'task-r{starts}'] = task('dispatched', None)
                self.tick()
                self.assertEqual(self.snap(rid)['state'], 'READY')
                fields = await self.issue(rid)
                self.fake.tasks[f'task-r{starts}'] = task('running' if starts else 'dispatched', None,
                                                          revision=3, runs=[{'run_id': 'r', 'status': 'x',
                                                                             'exit_code': None}])
                self.tick()
                self.assertEqual(await self.use(rid, fields), 'token_mismatch' if starts else 'ok')

    async def test_a_pre_run_cancel_without_a_task_still_compares_base_version(self):
        # the contract keeps ``base_version`` here (nothing is stopped): any version move between the page and the POST
        for name, change in (('an edit', lambda rid: self.inbox.update(OWNER, rid, self.snap(rid)['version'],
                                                                      title='Plan it better')),
                             ('a ledger write', lambda rid: self.inbox.reserve_delivery(
                                 rid, 'd-1', self.snap(rid)['version']))):
            with self.subTest(name):
                rid = self.add(f'Plan {name}', 'b')
                response = await self.get(f'/requests/{rid}/cancel', cookies=self.cookies)
                form = Page(response.body_text).form(f'/requests/{rid}/cancel')
                self.assertNotIn('token', form['fields'])
                fields = {n: v for n, v in form['fields'].items() if v is not None}
                change(rid)
                response = await self.post(f'/requests/{rid}/cancel', fields, cookies=self.cookies)
                self.assertNotEqual(response.status, 303, 'the stale version must refuse the pre-run cancel')
                self.assertEqual(self.snap(rid)['state'], 'NEW')
                self.assertEqual(self.snap(rid)['outcome'], None)
                fresh = await self.issue_plain(rid)
                response = await self.post(f'/requests/{rid}/cancel', fresh, cookies=self.cookies)
                self.assertEqual(response.status, 303)
                self.assertEqual(self.snap(rid)['outcome'], 'cancelled')

    async def issue_plain(self, rid):
        response = await self.get(f'/requests/{rid}/cancel', cookies=self.cookies)
        form = Page(response.body_text).form(f'/requests/{rid}/cancel')
        return {n: v for n, v in form['fields'].items() if v is not None}


if __name__ == '__main__':
    unittest.main()
