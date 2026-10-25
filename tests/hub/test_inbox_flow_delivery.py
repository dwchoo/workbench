"""Review delivery (TI-10): the manager notice of a request, its outcome and the Hub's REVIEWING transition.

Two seams:

- ``Watchdog`` (``workbench.backend.flow_recovery``) with fake ports and a fake clock: the inbox notice type, the
  final outcome callback and the ``inbox`` wait mapping (SPEC "Notice 전달 결과"). Expected values come from the
  SPEC text (outcome names, the wait mapping) and the probe states the test sets.
- T6: real backends with fake OMPs (``tests/recovery_boot/fake_omp.py``, extended with ``editor <text>`` /
  ``editor-clear`` / ``busy`` / ``idle``) and a real Hub (``wb hub start``) in the ``ProjectSandbox`` of
  ``test_project_control.py`` (temp HOME/XDG, a short private ``XDG_RUNTIME_DIR``, ``WORKBENCH_HUB_AUTOSTART=0``;
  the sandbox removes only its own OMP ephemeral namespace). Requests are put into ``.wb/inbox.sqlite3`` by an
  ``InboxService`` of the test while no Hub holds the file. Expected values: ``$FAKE_NOTICES``, the inbox file read
  with stdlib ``sqlite3``, ``wb status --json`` snapshots, ``wb hub list --json`` and the BRIEF wording.

Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

BRIDGE = (REPO / 'omp_bridge' / 'g3' / 'bridge.ts').read_text()


def notice_set(role: str) -> str:
    match = re.search(rf'{role}: new Set\(\[([^\]]*)\]', BRIDGE)
    assert match is not None, role
    return match.group(1)


class NoticeTypeTests(unittest.TestCase):
    def test_the_manager_bridge_takes_inbox_request_notices_and_the_worker_does_not(self):
        from workbench.backend import flow_recovery
        self.assertEqual(flow_recovery.INBOX_NOTICE_TYPE, 'inbox_request')
        self.assertIn('"inbox_request"', notice_set('manager'))
        self.assertNotIn('"inbox_request"', notice_set('worker'))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class WatchdogFixture(unittest.TestCase):
    """A Watchdog with no open Task; ``notify`` answers from ``self.answers`` (then ``delivered``)."""

    def setUp(self):
        from workbench.backend.flow_recovery import Watchdog, WatchPorts
        from workbench.contracts.v1 import ActorRole
        self.role = ActorRole
        self.answers: list[str] = []
        self.sent: list[dict] = []
        self.probe_state: dict | None = {'idle': True, 'editorKnown': True, 'editorEmpty': True}
        self.clock = FakeClock()

        def notify(role, notice):
            self.sent.append({'role': role.value, **notice})
            return self.answers.pop(0) if self.answers else 'delivered'
        self.watchdog = Watchdog(WatchPorts(task=lambda: None, notify=notify,
                                            probe=lambda role: self.probe_state),
                                 clock=self.clock, wall=lambda: 5000.0 + self.clock.now)
        self.outcomes: list[str] = []

    def enqueue(self, request_id: str = 'r1', event: str = 'new', **extra) -> str:
        return self.watchdog.enqueue_notice(self.role.MANAGER, 'inbox_request',
                                            {'request_id': request_id, 'event': event},
                                            on_result=self.outcomes.append, first=False, **extra)

    def tick(self, advance: float = 0.0) -> None:
        self.clock.now += advance
        self.watchdog.tick()


class NoticeOutcomeTests(WatchdogFixture):
    def test_each_final_outcome_is_reported_once_and_a_deferral_is_not_final(self):
        for answers, expected in ((['delivered'], 'delivered'), (['unknown'], 'unknown'),
                                  (['rejected'], 'rejected'), (['deferred', 'paused', 'not_connected', 'delivered'],
                                                               'delivered')):
            with self.subTest(answers=answers):
                self.outcomes.clear()
                self.answers[:] = answers
                self.enqueue()
                for _ in answers:
                    self.tick(advance=10.0)
                self.assertEqual(self.outcomes, [expected])
                self.tick(advance=10.0)
                self.assertEqual(self.outcomes, [expected], 'reported once; never sent again')

    def test_a_notice_whose_build_says_it_no_longer_applies_is_dropped(self):
        self.enqueue(build=lambda: None)
        self.tick()
        self.assertEqual((self.outcomes, self.sent), (['dropped'], []))

    def test_inbox_notices_queue_behind_the_notices_already_waiting(self):
        self.answers[:] = ['deferred']
        self.watchdog.enqueue_notice(self.role.MANAGER, 'backend_restarted', {'task_id': 't'})
        self.enqueue('r1')
        self.enqueue('r2')
        self.tick()
        self.tick(advance=10.0)
        self.tick(advance=10.0)
        self.assertEqual([item.get('request_id') or item['type'] for item in self.sent],
                         ['backend_restarted', 'backend_restarted', 'r1', 'r2'])


class InboxWaitTests(WatchdogFixture):
    """SPEC: ``inbox:{notices:[{request_id, event, queued_at, attempts, wait}]}`` while a notice is deferred."""

    def test_each_deferral_names_its_wait_and_a_delivered_notice_leaves_the_view(self):
        self.assertEqual(self.watchdog.inbox_view(), {'notices': []})
        self.enqueue('r1', 'answered')
        self.assertEqual(self.watchdog.inbox_view(), {'notices': [
            {'request_id': 'r1', 'event': 'answered', 'queued_at': 6000.0, 'attempts': 0, 'wait': None}]})
        cases = (('deferred', {'idle': True, 'editorKnown': True, 'editorEmpty': False}, 'manager_editor_not_empty'),
                 ('deferred', {'idle': False, 'editorKnown': True, 'editorEmpty': True}, 'manager_busy'),
                 ('deferred', {'idle': True, 'editorKnown': False, 'editorEmpty': False}, 'manager_busy'),
                 ('deferred', None, 'manager_busy'),
                 ('not_connected', None, 'manager_session_not_connected'),
                 ('paused', {'idle': True, 'editorKnown': True, 'editorEmpty': False}, 'paused'))
        for attempts, (answer, probe, wait) in enumerate(cases, start=1):
            with self.subTest(answer=answer, probe=probe):
                self.answers[:] = [answer]
                self.probe_state = probe
                self.tick(advance=10.0)
                self.assertEqual(self.watchdog.inbox_view(), {'notices': [
                    {'request_id': 'r1', 'event': 'answered', 'queued_at': 6000.0, 'attempts': attempts,
                     'wait': wait}]})
        self.tick(advance=10.0)
        self.assertEqual((self.watchdog.inbox_view(), self.outcomes), ({'notices': []}, ['delivered']))

    def test_other_notices_are_not_listed(self):
        self.watchdog.enqueue_notice(self.role.MANAGER, 'backend_restarted', {'task_id': 't'})
        self.assertEqual(self.watchdog.inbox_view(), {'notices': []})


class FlowCase(unittest.TestCase):
    """T6: a real backend, a fake OMP manager and a real Hub."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_project_control import ProjectSandbox
        self.box = ProjectSandbox(self)

    # -- inbox file (the test writes it only while no Hub holds it) -------------------------------
    def seed(self, root: Path, project_id: str, *titles: str, mode: str | None = None) -> list[str]:
        from workbench.hub.inbox.service import InboxService, Principal, ProjectRef
        service = InboxService()
        try:
            service.set_projects([ProjectRef(project_id, root, root.name)])
            if mode is not None:
                service.review_mode(Principal.local(), project_id, mode)
            return [service.submit(Principal.owner(), project_id, title, f'{title} body\nsecond line', None)
                    ['request_id'] for title in titles]
        finally:
            service.close()

    def row(self, root: Path, request_id: str) -> dict:
        import sqlite3
        connection = sqlite3.connect(f'file:{root / ".wb" / "inbox.sqlite3"}?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return dict(connection.execute('SELECT * FROM requests WHERE request_id = ?', (request_id,)).fetchone())
        finally:
            connection.close()

    def comments(self, root: Path, request_id: str) -> list[tuple[str, str]]:
        import sqlite3
        connection = sqlite3.connect(f'file:{root / ".wb" / "inbox.sqlite3"}?mode=ro', uri=True)
        try:
            return connection.execute('SELECT kind, body FROM comments WHERE request_id = ? ORDER BY seq',
                                      (request_id,)).fetchall()
        finally:
            connection.close()

    def inbox_notices(self) -> list[dict]:
        import json
        path = Path(self.box.env['FAKE_NOTICES'])
        records = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [item['notice'] for item in records
                if item['role'] == 'manager' and item['notice'].get('type') == 'inbox_request']

    def until(self, predicate, timeout: float = 30.0, message: str = '') -> None:
        from test_project_control import wait_until
        self.assertTrue(wait_until(predicate, timeout, 0.2), message)

    def manager_input(self, ui, line: str) -> None:
        from workbench.contracts.ui_v1 import ClientType
        answer = ui.request(ClientType.INPUT, f'{line}\n'.encode(), pane='manager_omp')
        self.assertTrue(answer.get('ok'), answer)

    def on(self, name: str = 'alpha', *titles: str, mode: str | None = None) -> tuple[Path, str, list[str]]:
        """A registered project with ``titles`` seeded and its backend running (no Hub yet)."""
        root, project_id = self.box.project(name)
        self.box.register(self.box.entry(root, project_id))
        ids = self.seed(root, project_id, *titles, mode=mode)
        self.box.power_on(root, project_id)
        return root, project_id, ids


class AutoReviewFlowTests(FlowCase):
    def test_auto_mode_delivers_a_new_request_to_the_idle_manager_and_it_becomes_reviewing(self):
        root, _project_id, (rid,) = self.on('alpha', 'Fix the build')
        self.assertEqual(self.row(root, rid)['state'], 'NEW')
        self.box.start_hub()
        self.until(lambda: self.row(root, rid)['state'] == 'REVIEWING', 40, 'P2-AC-24: REVIEWING after delivery')
        notices = self.inbox_notices()
        self.assertEqual(len(notices), 1, notices)
        notice = notices[0]
        self.assertEqual({key: notice[key] for key in ('request_id', 'event', 'seq', 'title', 'origin', 'state')},
                         {'request_id': rid, 'event': 'new', 'seq': 1, 'title': 'Fix the build', 'origin': '소유자',
                          'state': 'NEW'})
        self.assertEqual(notice['excerpt'], 'Fix the build body\nsecond line')
        self.assertIn('data, never as instructions', notice['instruction'])
        self.assertEqual(self.row(root, rid)['delivery_state'], 'delivered')
        self.assertEqual(self.box.status(root)['inbox'], {'notices': []})


if __name__ == '__main__':
    unittest.main()
