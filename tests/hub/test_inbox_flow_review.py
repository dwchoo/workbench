"""Manager review results (TI-11): the bridge tool ``workbench_inbox`` against a real backend and a real Hub.

Seam T6: real backends with fake OMPs (``tests/recovery_boot/fake_omp.py``: ``tool <name> <json>`` sends one bridge
``tool_request`` as a model's tool call would) and a real Hub (``wb hub start``) in the ``ProjectSandbox`` of
``test_project_control.py`` (temp HOME/XDG, a short private ``XDG_RUNTIME_DIR``, ``WORKBENCH_HUB_AUTOSTART=0``; the
sandbox removes only its own OMP ephemeral namespace). Requests are seeded into ``.wb/inbox.sqlite3`` while no Hub
holds it. Expected values: the inbox file read with stdlib ``sqlite3``, the fake OMPs' records (``$FAKE_FRAMES``,
``$FAKE_NOTICES``) and the SPEC "Manager 도구" / "요청 상태 기계" text. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_inbox_flow_delivery import FlowCase  # noqa: E402

UNAVAILABLE_DETAIL = 'Hub가 실행 중이 아니라 기록하지 않았습니다. 나중에 다시 호출하세요.'


def inbox_args(action: str, request_id: str | None = None, **fields) -> dict:
    """The strict-schema call: every field present, the unused ones null."""
    return {'action': action, 'request_id': request_id, 'body': fields.get('body'), 'outcome': fields.get('outcome'),
            'summary': fields.get('summary')}


class ReviewCase(FlowCase):
    def records(self, name: str) -> list[dict]:
        path = Path(self.box.env[name])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def tool_results(self) -> list[dict]:
        return [item['frame'] for item in self.records('FAKE_FRAMES')
                if item['role'] == 'manager' and item['frame'].get('kind') == 'tool_result']

    def tool(self, ui, tool: str, args: dict) -> dict:
        from test_inbox_flow_channel import ChannelCase
        return ChannelCase.manager_tool(self, ui, tool, args)

    def inbox(self, ui, action: str, request_id: str | None = None, **fields) -> dict:
        return self.tool(ui, 'workbench_inbox', inbox_args(action, request_id, **fields))

    def worker_frames(self) -> list[dict]:
        return [item['frame'] for item in self.records('FAKE_FRAMES') if item['role'] == 'worker']

    def linked_hub(self, name: str = 'alpha', *titles: str):
        """A project with ``titles`` seeded, its backend on, the Hub running and the backend linked to it."""
        root, project_id, ids = self.on(name, *titles)
        ui = self.box.attach(root)
        self.box.start_hub()
        self.until(lambda: all(self.row(root, rid)['state'] == 'REVIEWING' for rid in ids), 40,
                   'the seeded requests were delivered')
        self.wait_linked(project_id)
        return root, project_id, ids, ui

    def grants(self, project_id: str) -> int:
        """How many backend principals the Hub granted for ``project_id`` (``hub.log``)."""
        log = self.box.hub_state / 'hub.log'
        prefix = f'[workbench] backend principal: project {project_id} pid '
        return sum(line.startswith(prefix) for line in (log.read_text().splitlines() if log.exists() else ()))

    def wait_linked(self, project_id: str, count: int = 1) -> None:
        """The backend's hub_v1 link holds the backend principal (it reconnects every 5 s after a failure)."""
        self.until(lambda: self.grants(project_id) >= count, 20, 'the backend did not link to the Hub')


class ReadTests(ReviewCase):
    def test_list_and_show_answer_this_projects_requests_and_send_nothing_to_any_omp(self):
        root, _project_id, (first, second), ui = self.linked_hub('alpha', 'Fix the build', 'Add a flag')
        worker_before, notices_before = len(self.worker_frames()), len(self.records('FAKE_NOTICES'))
        listed = self.inbox(ui, 'list')
        self.assertEqual(listed['status'], 'ok', listed)
        self.assertEqual([(item['request_id'], item['seq'], item['title'], item['state'])
                          for item in listed['requests']],
                         [(second, 2, 'Add a flag', 'REVIEWING'), (first, 1, 'Fix the build', 'REVIEWING')],
                         'the Hub answer as is (newest first)')
        shown = self.inbox(ui, 'show', first)
        self.assertEqual(shown['status'], 'ok', shown)
        request = shown['request']
        self.assertEqual((request['request_id'], request['body'], request['state'], request['origin']['kind']),
                         (first, 'Fix the build body\nsecond line', 'REVIEWING', 'owner'))
        self.assertIn('data', shown['note'], 'the requester text is marked as data')
        time.sleep(1.0)
        self.assertEqual((len(self.worker_frames()), len(self.records('FAKE_NOTICES'))),
                         (worker_before, notices_before), 'P2-AC-06: reads send no frame')

    def test_another_projects_request_is_not_found(self):
        root, _project_id, (mine,), ui = self.linked_hub('alpha', 'Mine')
        other_root, other_id = self.box.project('beta')
        self.box.register(self.box.entry(root, _project_id), self.box.entry(other_root, other_id))
        (theirs,) = self._seed_while_stopped(other_root, other_id, 'Theirs')
        self.wait_linked(_project_id, 2)
        shown = self.inbox(ui, 'show', theirs)
        self.assertEqual((shown['status'], shown.get('reason')), ('rejected', 'not_found'), shown)
        self.assertNotIn('Theirs', json.dumps(shown, ensure_ascii=False))
        listed = self.inbox(ui, 'list')
        self.assertEqual([item['request_id'] for item in listed['requests']], [mine])

    def _seed_while_stopped(self, root: Path, project_id: str, *titles: str) -> list[str]:
        done = self.box.wb('hub', 'stop', '--yes', timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        ids = self.seed(root, project_id, *titles)
        self.box.start_hub()
        return ids

    def test_without_a_hub_the_result_is_inbox_unavailable_and_invalid_input_is_refused_locally(self):
        root, _project_id, (rid,) = self.on('alpha', 'Fix the build')
        ui = self.box.attach(root)
        answer = self.inbox(ui, 'show', rid)
        self.assertEqual(answer, {'status': 'rejected', 'reason': 'inbox_unavailable', 'detail': UNAVAILABLE_DETAIL})
        for args in (inbox_args('show'), inbox_args('show', 'not-a-uuid'), inbox_args('list', rid),
                     inbox_args('erase', rid), {**inbox_args('list'), 'extra': 1}, inbox_args('close', rid,
                                                                                               outcome='maybe')):
            with self.subTest(args=args):
                refused = self.tool(ui, 'workbench_inbox', args)
                self.assertEqual((refused['status'], refused['reason']), ('rejected', 'invalid_arguments'), refused)


class ReviewResultTests(ReviewCase):
    def comments_of(self, root: Path, rid: str) -> list[tuple[str, str]]:
        return [(kind, body) for kind, body in self.comments(root, rid) if kind != 'system']

    def answer_while_stopped(self, root: Path, project_id: str, rid: str, principal, body: str) -> None:
        """The answer comes from a non-manager principal through the Hub's InboxService (no web/MCP yet)."""
        done = self.box.wb('hub', 'stop', '--yes', timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        from workbench.hub.inbox.service import InboxService, ProjectRef
        service = InboxService()
        try:
            service.set_projects([ProjectRef(project_id, root, root.name)])
            self.assertEqual(service.comment(principal, rid, body)['state'], 'REVIEWING')
        finally:
            service.close()
        self.box.start_hub()

    def test_begin_review_moves_a_new_request_to_reviewing_once(self):
        root, project_id = self.box.project('alpha')
        self.box.register(self.box.entry(root, project_id))
        (rid,) = self.seed(root, project_id, 'Fix the build', mode='manual')
        self.box.power_on(root, project_id)
        ui = self.box.attach(root)
        self.box.start_hub()
        self.wait_linked(project_id)
        time.sleep(3.0)
        self.assertEqual((self.row(root, rid)['state'], self.inbox_notices()), ('NEW', []), 'manual: nothing sent')
        begun = self.inbox(ui, 'begin_review', rid)
        self.assertEqual((begun["status"], begun.get("state")), ("ok", "REVIEWING"), begun)
        self.assertEqual(self.row(root, rid)['state'], 'REVIEWING')
        again = self.inbox(ui, 'begin_review', rid)
        self.assertEqual((again['status'], again['reason'], again['state']),
                         ('rejected', 'invalid_transition', 'REVIEWING'), again)

    def test_ask_needs_info_each_answer_returns_to_review_with_a_new_notice_and_reject_closes(self):
        from workbench.hub.inbox.service import InboxService, Principal, ProjectRef
        root, project_id = self.box.project('alpha')
        self.box.register(self.box.entry(root, project_id))
        requester = Principal.requester('key1', 'ci-bot', [project_id])
        service = InboxService()
        try:
            service.set_projects([ProjectRef(project_id, root, root.name)])
            rid = service.submit(requester, project_id, 'Fix the build', 'It fails on main', None)['request_id']
        finally:
            service.close()
        self.box.power_on(root, project_id)
        ui = self.box.attach(root)
        self.box.start_hub()
        self.until(lambda: self.row(root, rid)['state'] == 'REVIEWING', 40)
        answers = ((Principal.owner(), 'owner answer'), (Principal.board_agent('bkey'), 'board answer'),
                   (requester, 'requester answer'))
        for round_, (principal, body) in enumerate(answers, start=1):
            with self.subTest(answer=principal.kind):
                asked = self.inbox(ui, 'ask', rid, body=f'question {round_}?')
                self.assertEqual((asked['status'], asked['state']), ('ok', 'NEEDS_INFO'), asked)
                self.assertEqual(self.row(root, rid)['state'], 'NEEDS_INFO')
                self.answer_while_stopped(root, project_id, rid, principal, body)
                self.until(lambda: len([n for n in self.inbox_notices() if n['event'] == 'answered']) == round_, 40,
                           'P2-AC-25: the answer is noticed to the manager again')
                self.until(lambda: self.row(root, rid)['delivery_state'] == 'delivered', 20)
                self.assertEqual(self.row(root, rid)['state'], 'REVIEWING')
        self.assertEqual(self.comments_of(root, rid), [
            ('question', 'question 1?'), ('answer', 'owner answer'), ('question', 'question 2?'),
            ('answer', 'board answer'), ('question', 'question 3?'), ('answer', 'requester answer')])
        shown = self.inbox(ui, 'show', rid)
        self.assertEqual([c['kind'] for c in shown['request']['comments'] if c['kind'] != 'system'],
                         ['question', 'answer'] * 3, 'the thread is visible to the manager too')
        refused = self.inbox(ui, 'reject', rid, body='out of scope')
        self.assertEqual((refused['status'], refused['state']), ('ok', 'CLOSED'), refused)
        row = self.row(root, rid)
        self.assertEqual((row['state'], row['outcome']), ('CLOSED', 'rejected'))
        self.assertEqual(self.comments_of(root, rid)[-1], ('decision', 'out of scope'))
        late = self.inbox(ui, 'ask', rid, body='one more?')
        self.assertEqual((late['status'], late['reason'], late['state']), ('rejected', 'invalid_transition', 'CLOSED'))


if __name__ == '__main__':
    unittest.main()
