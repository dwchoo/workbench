"""Approved-spec link (TI-11): ``to_worker`` with ``request_id`` against a real backend and a real Hub.

Seam T6 as in ``test_inbox_flow_review.py`` (real backend + fake OMPs + real Hub in the ``ProjectSandbox``).
Independent expected values: the TASK messages the worker OMP was offered (``$FAKE_FRAMES``), ``tasks.sqlite3`` read
with stdlib ``sqlite3`` (tasks and decision rows), the inbox file and the SPEC "Manager 도구" link steps (check →
Task → commit → delegate) and "요청 상태 기계" table. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_inbox_flow_review import ReviewCase  # noqa: E402


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()

WORK = {'kind': 'work', 'message': 'collect the facts TI11-MARK', 'spec': {'goal': 'facts', 'paths': []}}


class LinkCase(ReviewCase):
    def tasks(self, root: Path) -> list[dict]:
        path = root / '.wb' / 'tasks.sqlite3'
        if not path.exists():
            return []
        connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute('SELECT * FROM tasks')]
        finally:
            connection.close()

    def worker_tasks(self) -> list[dict]:
        """Task messages offered to the worker OMP (its deliver frames; the fake OMP keeps no ids)."""
        return [frame for frame in self.worker_frames() if frame.get('kind') == 'deliver']

    def decisions(self, root: Path, task_id: str) -> list[tuple[str, int, dict]]:
        connection = sqlite3.connect(f'file:{root / ".wb" / "tasks.sqlite3"}?mode=ro', uri=True)
        try:
            return [(kind, revision, json.loads(details)) for kind, revision, details in connection.execute(
                'SELECT kind, revision, details_json FROM task_decisions WHERE task_id = ? ORDER BY created_at',
                (task_id,))]
        finally:
            connection.close()

    def spec_of(self, root: Path, task_id: str, revision: int) -> dict:
        connection = sqlite3.connect(f'file:{root / ".wb" / "tasks.sqlite3"}?mode=ro', uri=True)
        try:
            (spec,) = connection.execute('SELECT spec_json FROM task_specs WHERE task_id = ? AND revision = ?',
                                         (task_id, revision)).fetchone()
            return json.loads(spec)
        finally:
            connection.close()

    def view(self, root: Path, rid: str) -> dict:
        value = self.row(root, rid)['task_view']
        return json.loads(value) if value else {}

    def to_worker(self, ui, request_id: str | None, **extra) -> dict:
        return self.tool(ui, 'to_worker', {**WORK, **extra, 'request_id': request_id})


class LinkRefusedTests(LinkCase):
    def test_a_closed_request_refuses_the_link_and_no_task_is_made_or_sent(self):
        root, _project_id, (rid,), ui = self.linked_hub('alpha', 'Fix the build')
        self.assertEqual(self.inbox(ui, 'reject', rid, body='no')['state'], 'CLOSED')
        answer = self.to_worker(ui, rid)
        self.assertEqual((answer['status'], answer['reason'], answer.get('state')),
                         ('rejected', 'inbox_link_refused', 'CLOSED'), answer)
        self.assertNotIn('task_id', answer)
        time.sleep(1.0)
        self.assertEqual((self.tasks(root), self.worker_tasks()), ([], []), 'no Task, nothing to the worker')
        unknown = self.to_worker(ui, str(uuid.uuid4()))
        self.assertEqual((unknown['status'], unknown['reason']), ('rejected', 'inbox_link_refused'), unknown)
        self.assertEqual(unknown.get('hub_reason'), 'not_found')
        self.assertEqual(self.tasks(root), [])

    def test_request_id_must_be_a_canonical_uuid(self):
        root, _project_id = self.box.project('alpha')
        self.box.power_on(root, _project_id)
        ui = self.box.attach(root)
        answer = self.to_worker(ui, 'R-1')
        self.assertEqual((answer['status'], answer['reason']), ('rejected', 'invalid_arguments'), answer)
        self.assertTrue(any(error.startswith('request_id:') for error in answer['errors']), answer)
        self.assertEqual(self.tasks(root), [])


class LinkedTaskTests(LinkCase):
    def test_a_linked_task_is_ready_then_running_and_only_the_managers_close_ends_the_request(self):
        root, _project_id, (rid,), ui = self.linked_hub('alpha', 'Fix the build')
        answer = self.to_worker(ui, rid)
        self.assertEqual(answer['status'], 'dispatched', answer)
        task_id = answer['task_id']
        self.assertEqual(answer['inbox'], {'request_id': rid, 'state': 'READY'}, 'P2-AC-26: linked before dispatch')
        row = self.row(root, rid)
        self.assertEqual((row['task_id'], row['spec_revision']), (task_id, 1))
        self.assertIn(row['state'], ('READY', 'RUNNING'))
        self.assertEqual(self.spec_of(root, task_id, 1)['inbox_request_id'], rid, 'the spec names the request')
        kinds = [(kind, revision) for kind, revision, _ in self.decisions(root, task_id)]
        self.assertEqual(kinds, [('scope_approved', 1), ('proceed', 1)], 'P2-AC-28: standing delegation, no approval')
        for _kind, _revision, details in self.decisions(root, task_id):
            self.assertEqual((details['actor'], details['authority'], details['inbox_request_id']),
                             ('user_standing_delegation', 'C-D66', rid))
        self.until(lambda: self.worker_tasks(), 30, 'the TASK went to the worker after the link')
        self.until(lambda: self.row(root, rid)['state'] == 'RUNNING', 30, 'Task sync: READY -> RUNNING once started')
        self.assertEqual(self.view(root, rid)['status'], 'running')
        # the Task ends without the manager closing the request: the request stays RUNNING (P2-AC-09)
        cancelled = self.tool(ui, 'to_worker', {'kind': 'work', 'message': 'stop it', 'task_id': task_id,
                                                'cancel': True})
        self.assertIn(cancelled['status'], ('cancelled', 'cancel_requested'), cancelled)
        self.until(lambda: self.view(root, rid).get('closed') is True, 30, 'the Hub saw the Task closed')
        self.until(lambda: [n for n in self.inbox_notices() if n['request_id'] == rid
                            and n['event'] == 'linked_task_closed'], 40, 'linked_task_closed notice once')
        time.sleep(3.0)
        self.assertEqual(self.row(root, rid)['state'], 'RUNNING', 'a Task end alone never closes the request')
        self.assertEqual(len([n for n in self.inbox_notices() if n['event'] == 'linked_task_closed']), 1)
        missing = self.inbox(ui, 'close', rid, outcome='success', summary=None)
        self.assertEqual((missing['status'], missing['reason']), ('rejected', 'invalid_arguments'), missing)
        closed = self.inbox(ui, 'close', rid, outcome='success', summary='Built and verified.')
        self.assertEqual((closed['status'], closed['state']), ('ok', 'CLOSED'), closed)
        row = self.row(root, rid)
        self.assertEqual((row['state'], row['outcome'], row['result_summary']),
                         ('CLOSED', 'success', 'Built and verified.'))

    def links(self, root: Path, rid: str) -> list[tuple[str, int, str]]:
        connection = sqlite3.connect(f'file:{root / ".wb" / "inbox.sqlite3"}?mode=ro', uri=True)
        try:
            return connection.execute('SELECT task_id, spec_revision, state FROM links WHERE request_id = ? '
                                      'ORDER BY linked_at', (rid,)).fetchall()
        finally:
            connection.close()

    def test_asking_while_running_keeps_the_link_and_a_follow_up_with_the_request_links_it_again(self):
        from workbench.hub.inbox.service import Principal
        root, project_id, (rid,), ui = self.linked_hub('alpha', 'Fix the build')
        answer = self.to_worker(ui, rid)
        task_id = answer['task_id']
        self.until(lambda: self.row(root, rid)['state'] == 'RUNNING', 30)
        asked = self.inbox(ui, 'ask', rid, body='Which branch?')
        self.assertEqual((asked['status'], asked['state']), ('ok', 'NEEDS_INFO'), asked)
        self.assertEqual(self.row(root, rid)['task_id'], task_id, 'P2-AC-27: the link stays')
        grants = self.grants(project_id)
        self.answer_while_stopped(root, project_id, rid, Principal.owner(), 'main')
        self.wait_linked(project_id, grants + 1)
        self.until(lambda: self.row(root, rid)['state'] == 'REVIEWING', 30)
        delivered = len(self.worker_tasks())
        follow = self.tool(ui, 'to_worker', {'kind': 'work', 'message': 'use main', 'task_id': task_id,
                                             'request_id': rid})
        self.assertEqual((follow['status'], follow['task_id'], follow['inbox']),
                         ('queued', task_id, {'request_id': rid, 'state': 'RUNNING'}), follow)
        self.assertEqual(self.row(root, rid)['state'], 'RUNNING')
        self.assertEqual(self.links(root, rid), [(task_id, 1, 'superseded'), (task_id, 1, 'active')])
        self.until(lambda: len(self.worker_tasks()) > delivered, 30, 'the follow-up went to the worker')

    def test_three_origins_take_the_same_path_with_standing_delegation_only(self):
        from workbench.hub.inbox.service import InboxService, Principal, ProjectRef
        root, project_id = self.box.project('alpha')
        self.box.register(self.box.entry(root, project_id))
        service = InboxService()
        try:
            service.set_projects([ProjectRef(project_id, root, root.name)])
            ids = [service.submit(principal, project_id, title, f'{title} body', None)['request_id']
                   for principal, title in ((Principal.owner(), 'from owner'),
                                            (Principal.board_agent('kboard0001'), 'from board'),
                                            (Principal.editor('keditor001', 'ext-agent', [project_id]), 'from key'))]
        finally:
            service.close()
        self.box.power_on(root, project_id)
        ui = self.box.attach(root)
        self.box.start_hub()
        self.until(lambda: all(self.row(root, rid)['state'] == 'REVIEWING' for rid in ids), 60)
        self.wait_linked(project_id)
        origins = []
        for rid in ids:
            answer = self.to_worker(ui, rid)
            self.assertEqual((answer['status'], answer['inbox']['state']), ('dispatched', 'READY'), answer)
            task_id = answer['task_id']
            self.assertEqual([(kind, details['actor'], details['inbox_request_id'])
                              for kind, _revision, details in self.decisions(root, task_id)],
                             [('scope_approved', 'user_standing_delegation', rid),
                              ('proceed', 'user_standing_delegation', rid)], 'P2-AC-28: no owner approval step')
            row = self.row(root, rid)
            origins.append((row['origin_kind'], row['origin_name']))
            done = self.tool(ui, 'to_worker', {'kind': 'work', 'message': 'next', 'task_id': task_id, 'cancel': True})
            self.assertIn(done['status'], ('cancelled', 'cancel_requested'), done)
        self.assertEqual(origins, [('owner', None), ('board_agent', None), ('api_key', 'ext-agent')])


class HubAbsentTests(LinkCase):
    def test_with_the_hub_stopped_to_worker_with_a_request_creates_and_sends_nothing(self):
        root, _project_id, (rid,), ui = self.linked_hub('alpha', 'Fix the build')
        done = self.box.wb('hub', 'stop', '--yes', timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        answer = self.to_worker(ui, rid)
        self.assertEqual((answer['status'], answer['reason'], answer['dispatched']),
                         ('rejected', 'inbox_unavailable', False), answer)
        time.sleep(1.0)
        self.assertEqual((self.tasks(root), self.worker_tasks()), ([], []))
        plain = self.to_worker(ui, None)
        self.assertEqual(plain['status'], 'dispatched', 'a to_worker without request_id needs no Hub (P2-AC-01)')
        self.assertNotIn('inbox', plain)


class HubPort:
    """``TaskFlow.inbox_call`` backed by a real ``InboxService`` as the Hub serves it to the backend principal.
    ``after[op]`` runs right after that op was served (a change committed in between); ``lose[op]`` answers that op
    ``unknown`` after the Hub served it (the answer was lost)."""

    def __init__(self, service, project_id: str):
        from workbench.hub.inbox.service import Principal
        self.service, self.principal = service, Principal.manager(project_id)
        self.after: dict = {}
        self.lose: set[str] = set()
        self.calls: list[str] = []
        self.last: dict = {}  # the fields of the last call

    def __call__(self, op: str, *, call_timeout: float, **fields):
        from workbench.backend.inbox_link import LinkResult
        from workbench.hub.inbox.limits import InboxError
        self.calls.append(op)
        self.last = dict(fields)
        try:
            if op == 'inbox_link_check':
                value = self.service.link_check(self.principal, fields['request_id'])
            elif op == 'inbox_link_commit':
                value = self.service.link_commit(self.principal, fields['link_token'], fields['task_id'],
                                                 fields['spec_revision'], fields['task_status'])
            else:
                raise AssertionError(op)
        except InboxError as exc:
            extra = {key: value for key, value in (('state', exc.state), ('version', exc.version)) if value is not None}
            result = LinkResult('rejected', exc.code, exc.detail, {'reason': exc.code, **extra})
        else:
            result = LinkResult('ok', value=value)
        if op in self.after:
            self.after[op]()
        return LinkResult('unknown', 'no_answer') if op in self.lose else result


class LinkSeamCase(unittest.TestCase):
    """Seam: ``TaskFlow`` inside the real ``HandoffService`` with the fake mailbox of ``tests/backend/test_task_flow``
    (every message the worker would get is in ``mailbox.created``) and the link port on a real ``InboxService`` (the
    same transitions the Hub runs for the backend principal). It places a Hub commit exactly between the link check
    and the link commit, which the T6 setup cannot."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
        from test_task_flow import FlowFixture
        from test_inbox_service import make_project
        from workbench.hub.inbox.service import InboxService, Principal, ProjectRef
        self.fixture = FlowFixture('run')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.flow = self.fixture.flow
        base = self.fixture.root / 'hub'
        base.mkdir()
        self.root, self.project_id = make_project(base, 'alpha')
        self.service = InboxService(backend_running=lambda project_id: True)
        self.addCleanup(self.service.close)
        self.service.set_projects([ProjectRef(self.project_id, self.root, 'alpha')])
        self.owner, self.manager = Principal.owner(), Principal.manager(self.project_id)
        self.rid = self.service.submit(self.owner, self.project_id, 'Fix the build', 'body', None)['request_id']
        self.service.review(self.manager, self.rid, 'begin_review')
        self.port = HubPort(self.service, self.project_id)
        self.flow.inbox_call = self.port

    def to_worker(self, **extra) -> dict:
        return self.fixture.to_worker({**WORK, 'request_id': self.rid, **extra})

    def request(self) -> dict:
        return self.service.get(self.owner, self.rid)


class RaceTests(LinkSeamCase):
    def test_a_cancel_committed_after_the_check_wins_and_the_task_closes_without_delivery(self):
        self.port.after['inbox_link_check'] = lambda: self.service.cancel(self.owner, self.rid)
        answer = self.to_worker()
        self.assertEqual((answer['status'], answer['reason'], answer['hub_reason'], answer['state']),
                         ('rejected', 'inbox_link_refused', 'invalid_transition', 'CLOSED'), answer)
        self.assertEqual(self.port.calls, ['inbox_link_check', 'inbox_link_commit'])
        task_id = answer['task_id']
        self.assertEqual((answer['task_status'], answer['closed_reason']), ('closed', 'inbox_link_refused'))
        time.sleep(0.3)
        self.assertEqual(self.fixture.mailbox.created, [], 'P2-AC-43: no TASK frame for the worker')
        self.assertEqual(self.fixture.decisions(task_id, 1), [], 'never delegated: no scope_approved/proceed')
        self.assertEqual((self.request()['state'], self.request()['outcome']), ('CLOSED', 'cancelled'))
        self.assertIsNone(self.flow.active())

    def test_an_unknown_commit_answer_sends_nothing_and_the_task_sync_voids_a_committed_link(self):
        from workbench.hub.inbox.dispatch import Dispatcher
        from workbench.hub.inbox.service import ProjectRef
        self.port.lose.add('inbox_link_commit')
        answer = self.to_worker()
        self.assertEqual((answer['status'], answer['reason']), ('unknown', 'inbox_link_unconfirmed'), answer)
        self.assertEqual((answer['task_status'], answer['closed_reason']), ('closed', 'inbox_link_unconfirmed'))
        task_id = answer['task_id']
        time.sleep(0.3)
        self.assertEqual(self.fixture.mailbox.created, [], 'nothing went to the worker')
        self.assertEqual(self.fixture.decisions(task_id, 1), [])
        self.assertEqual(self.request()['state'], 'READY', 'the Hub did commit the link')
        flow = self.flow

        class Port:
            identity = staticmethod(lambda root: '1:1')
            inbox_notice = staticmethod(lambda *args, **kwargs: {'queued': True, 'notice_id': 'n'})

            @staticmethod
            def task_status(root, task_ids, **kwargs):
                return [{'task_id': tid, 'exists': True, **{key: flow.status_view(tid)[key] for key in
                                                             ('status', 'closed_reason', 'revision')},
                         'cancel_requested_by': None, 'runs': [], 'commands': []} for tid in task_ids]
        dispatcher = Dispatcher(self.service, lambda: [ProjectRef(self.project_id, self.root, "alpha")], port=Port(),
                                sync_workers=0)
        dispatcher.tick()
        request = self.request()
        self.assertEqual((request['state'], request['task']['task_id']), ('REVIEWING', None), 'link voided')
        self.assertIn('Task 연결이 확인되지 않아', [c['body'] for c in request['comments'] if c['kind'] == 'system'][-1])

    def test_a_linked_request_is_never_linked_to_a_second_task(self):
        first = self.to_worker()
        self.assertEqual(first['inbox']['state'], 'READY', first)
        cancelled = self.fixture.to_worker({'kind': 'work', 'message': 'stop', 'task_id': first['task_id'],
                                            'cancel': True})
        self.assertIn(cancelled['status'], ('cancelled', 'cancel_requested'), cancelled)
        created = len(self.fixture.mailbox.created)
        second = self.to_worker()
        self.assertEqual((second['status'], second['reason'], second['state']),
                         ('rejected', 'inbox_link_refused', 'READY'), second)
        self.assertNotIn('task_id', second, 'refused at the check: no second Task')
        self.assertEqual(len(self.fixture.mailbox.created), created)
        self.assertEqual(self.request()['task']['task_id'], first['task_id'])

    def close_in_window(self, reason: str = 'shutdown_closing') -> None:
        """Between the Hub's commit and the dispatch, something else in the backend closes the Task."""
        def close():
            with self.flow._lock:
                self.flow._close(self.flow.tasks[self.port.last['task_id']], reason)
        self.port.after['inbox_link_commit'] = close

    def test_a_task_closed_between_the_commit_and_the_dispatch_is_not_dispatched(self):
        self.close_in_window()
        answer = self.to_worker()
        self.assertEqual((answer['status'], answer['reason'], answer['dispatched']),
                         ('rejected', 'task_changed_before_dispatch', False), answer)
        self.assertEqual((answer['task_status'], answer['closed_reason']), ('closed', 'shutdown_closing'))
        self.assertEqual(answer['inbox'], {'request_id': self.rid, 'state': 'READY'}, 'the Hub link was committed')
        time.sleep(0.3)
        self.assertEqual(self.fixture.mailbox.created, [], 'no TASK frame for the worker')
        self.assertEqual(self.fixture.decisions(answer['task_id'], 1), [], 'never delegated')

    def test_a_follow_up_whose_task_closed_in_the_window_sends_nothing(self):
        first = self.to_worker()
        task_id = first['task_id']
        self.assertTrue(wait_until(lambda: self.flow.task_view()['status'] == 'running'
                                   and len(self.fixture.mailbox.created) >= 1), self.flow.task_view())
        self.service.observe_task(self.rid, task_id, 'running')  # the Hub's Task sync
        self.service.review(self.manager, self.rid, 'ask', body='which branch?')
        self.service.comment(self.owner, self.rid, 'main')
        created = len(self.fixture.mailbox.created)
        self.close_in_window('cancelled')
        follow = self.fixture.to_worker({'kind': 'work', 'message': 'use main', 'task_id': task_id,
                                         'request_id': self.rid})
        self.assertEqual((follow['status'], follow['reason']), ('rejected', 'task_changed_before_dispatch'), follow)
        time.sleep(0.3)
        self.assertEqual(len(self.fixture.mailbox.created), created, 'no follow-up message')

    def test_a_refused_commit_leaves_the_previous_inactive_task_untouched(self):
        old = self.fixture.finished_experiment()
        self.port.after['inbox_link_check'] = lambda: self.service.cancel(self.owner, self.rid)
        answer = self.to_worker()
        self.assertEqual(answer['reason'], 'inbox_link_refused', answer)
        self.assertEqual((self.flow.tasks[old].status, self.flow.tasks[old].closed_reason), ('finished', None),
                         'the old Task is ended only when the new one is dispatched')
        other = self.service.submit(self.owner, self.project_id, 'Other', 'body', None)['request_id']
        self.service.review(self.manager, other, 'begin_review')
        self.port.after.clear()
        done = self.fixture.to_worker({**WORK, 'request_id': other})
        self.assertEqual(done['status'], 'dispatched', done)
        self.assertEqual((self.flow.tasks[old].status, self.flow.tasks[old].closed_reason),
                         ('closed', 'superseded_by_new_task'))

    def test_without_a_hub_link_nothing_is_created(self):
        self.flow.inbox_call = None
        answer = self.to_worker()
        self.assertEqual((answer['status'], answer['reason']), ('rejected', 'inbox_unavailable'), answer)
        self.assertNotIn('task_id', answer)
        self.assertIsNone(self.flow.active())
        self.assertEqual(self.fixture.mailbox.created, [])

    def test_a_follow_up_with_another_or_no_request_id_never_relinks_and_a_plain_to_worker_is_unchanged(self):
        first = self.to_worker()
        self.assertEqual((first['status'], first['inbox']['state']), ('dispatched', 'READY'), first)
        plain = self.fixture.to_worker({'kind': 'work', 'message': 'another', 'spec': {'goal': 'g', 'paths': []}})
        self.assertEqual(plain['status'], 'worker_busy', plain)
        self.assertNotIn('inbox', plain)
        other = self.service.submit(self.owner, self.project_id, 'Other', 'body', None)['request_id']
        self.service.review(self.manager, other, 'begin_review')
        self.port.calls.clear()
        busy = self.fixture.to_worker({**WORK, 'request_id': other})
        self.assertEqual(busy['status'], 'worker_busy', busy)
        self.assertEqual(self.port.calls, ['inbox_link_check'], 'no commit without a Task to link')
        self.assertEqual(self.service.get(self.owner, other)['state'], 'REVIEWING', 'the other request is not linked')
        self.assertEqual(self.service.get(self.owner, other)['task']['task_id'], None)


class TaskSyncTests(unittest.TestCase):
    """Dispatcher Task sync against a real ``InboxService`` and a fake backend port (SPEC "Task 동기화")."""

    def setUp(self):
        import tempfile
        from test_inbox_service import make_project
        from workbench.hub.inbox.service import InboxService, ProjectRef
        temp = tempfile.TemporaryDirectory(prefix='wb-inbox-sync-')
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.projects = [make_project(base, name) for name in ('alpha', 'beta')]
        self.service = InboxService()
        self.addCleanup(self.service.close)
        self.refs = [ProjectRef(project_id, root, root.name) for root, project_id in self.projects]
        self.service.set_projects(self.refs)

    def linked(self, project_id: str, count: int) -> dict[str, str]:
        """``count`` READY requests of ``project_id``, each linked to its own Task: request id -> task id."""
        from workbench.hub.inbox.service import Principal
        owner, manager = Principal.owner(), Principal.manager(project_id)
        found = {}
        for number in range(count):
            rid = self.service.submit(owner, project_id, f'request {number}', 'body', None)['request_id']
            token = self.service.link_check(manager, rid)['link_token']
            task_id = str(uuid.uuid4())
            self.service.link_commit(manager, token, task_id, 1, 'dispatched')
            found[rid] = task_id
        return found

    def dispatcher(self, port, **kwargs):
        from workbench.hub.inbox.dispatch import Dispatcher
        dispatcher = Dispatcher(self.service, lambda: self.refs, port=port, **kwargs)
        self.addCleanup(dispatcher.close)
        return dispatcher

    @staticmethod
    def running(task_ids):
        return [{'task_id': tid, 'exists': True, 'status': 'running', 'closed_reason': None, 'revision': 1,
                 'cancel_requested_by': None, 'runs': [], 'commands': []} for tid in task_ids]

    def state(self, rid: str) -> str:
        from workbench.hub.inbox.service import Principal
        return self.service.get(Principal.owner(), rid)['state']

    def test_task_ids_go_in_chunks_of_the_ui_v1_limit(self):
        from workbench.contracts.ui_v1 import MAX_TASK_STATUS_IDS
        (_root, alpha), _beta = self.projects
        links = self.linked(alpha, MAX_TASK_STATUS_IDS + 6)
        asked = []
        test = self

        class Port:
            identity = staticmethod(lambda root: '1:1' if root == test.refs[0].root else None)
            inbox_notice = staticmethod(lambda *args, **kwargs: {'queued': True, 'notice_id': 'n'})

            @staticmethod
            def task_status(root, task_ids, **kwargs):
                asked.append(list(task_ids))
                return TaskSyncTests.running(task_ids)
        self.dispatcher(Port(), sync_workers=0).tick()
        self.assertEqual([len(chunk) for chunk in asked], [MAX_TASK_STATUS_IDS, 6])
        self.assertEqual(sorted(tid for chunk in asked for tid in chunk), sorted(links.values()))
        self.assertTrue(all(self.state(rid) == 'RUNNING' for rid in links))

    def test_an_unresponsive_backend_does_not_hold_the_tick_or_another_projects_sync(self):
        import threading
        (_ra, alpha), (_rb, beta) = self.projects
        (stuck_rid,) = self.linked(alpha, 1)
        (other_rid,) = self.linked(beta, 1)
        release = threading.Event()
        self.addCleanup(release.set)
        test = self

        class Port:
            identity = staticmethod(lambda root: '1:1')
            inbox_notice = staticmethod(lambda *args, **kwargs: {'queued': True, 'notice_id': 'n'})

            @staticmethod
            def task_status(root, task_ids, **kwargs):
                if root == test.refs[0].root:
                    release.wait(30)  # this backend never answers in time
                return TaskSyncTests.running(task_ids)
        dispatcher = self.dispatcher(Port())
        started = time.monotonic()
        dispatcher.tick()
        dispatcher.tick()
        self.assertLess(time.monotonic() - started, 1.0, 'the tick waited for a backend')
        self.assertTrue(wait_until(lambda: self.state(other_rid) == 'RUNNING', 5), 'the other project was synced')
        self.assertEqual(self.state(stuck_rid), 'READY')
        release.set()
        dispatcher.tick()
        self.assertTrue(wait_until(lambda: self.state(stuck_rid) == 'RUNNING', 5))


if __name__ == '__main__':
    unittest.main()
