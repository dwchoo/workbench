"""User cancel of a READY/RUNNING request (TI-13): the Hub's cancel port, the Task sync close and the real path.

Two seams:

- ``InboxService`` with a recording cancel port (``cancel_port(ref, task_id, request_id, expect, reason)``, the
  port the Hub wires to ``backend_port.cancel_task``): what the Hub commits around the backend's answer. Expected
  values: SPEC "취소" (``cancel_state=requested`` committed before the backend call, ``task_changed`` → reverted and
  refused like ``token_mismatch``, an unknown answer is surfaced and never a success; the request closes only when
  the Task sync sees the user-cancelled Task closed and its command ended; a stop that ended ``unknown`` is a system
  comment, not a success) and the rows read back with stdlib ``sqlite3``.
- T6 (``LiveCancelTests``): real backend + fake OMPs + real Hub (``wb hub start``) in the ``ProjectSandbox``; the
  worker's terminal command is a host ``sleep``. Expected values: ``/proc`` (the command pid ends), the backend's
  command stop records (``requested_by: user``), ``tasks.sqlite3``, ``$FAKE_FRAMES`` and the inbox file.

Run with ``discover -s tests/hub -p 'test_inbox_flow_cancel*.py'``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_inbox_service import OWNER, ServiceCase  # noqa: E402
from workbench.hub.inbox.service import InboxError, InboxService, ProjectRef  # noqa: E402

COMMAND = {'command_id': '9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a', 'owner': 'worker', 'label': 'sleep 300',
           'state': 'running'}


class PortCase(ServiceCase):
    def setUp(self):
        super().setUp()
        self.calls: list[tuple] = []
        self.answers: list[dict] = []
        self.service.close()
        self.service = InboxService(on_committed=self.events.append, backend_running=lambda project_id: self.backend_up,
                                    cancel_port=self.port)
        self.addCleanup(self.service.close)
        self.service.set_projects([ProjectRef(self.a, self.root, 'alpha'), ProjectRef(self.b, self.root_b, 'beta')])

    def port(self, ref, task_id, request_id, expect, reason):
        self.calls.append((ref.project_id, task_id, request_id, expect, reason))
        return self.answers.pop(0) if self.answers else {'outcome': 'accepted', 'cancel': 'cancelled', 'stop': None}

    def row(self, rid: str) -> tuple:
        return self.rows('SELECT state, outcome, cancel_state FROM requests WHERE request_id = ?', rid)[0]

    def system_comments(self, rid: str) -> list[str]:
        return [body for (body,) in self.rows("SELECT body FROM comments WHERE request_id = ? AND kind = 'system' "
                                               "ORDER BY seq", rid)]

    def running_with_command(self, command=COMMAND) -> str:
        rid = self.make('RUNNING')
        self.service.observe_task(rid, 'task-1', 'running', view={'command': dict(command)})
        return rid


class CancelPortTests(PortCase):
    def test_a_ready_cancel_commits_requested_then_asks_the_backend_with_the_expected_task(self):
        rid = self.make('READY')
        result = self.service.cancel(OWNER, rid, reason='not needed')
        self.assertEqual(self.calls, [(self.a, 'task-1', rid, {'status': 'dispatched', 'command_id': None},
                                       'not needed')])
        self.assertEqual((result['status'], result['stop']), ('cancel_requested', None), result)
        self.assertEqual(self.row(rid), ('READY', None, 'requested'), 'accepted is not closed (P2-AC-30)')
        types = [event.type for event in self.events]
        self.assertLess(types.index('cancel_requested'), types.index('cancel_accepted'))
        self.assertEqual(len(self.system_comments(rid)), 1, self.system_comments(rid))

    def test_a_running_cancel_binds_the_command_and_a_started_stop_is_stop_requested(self):
        rid = self.running_with_command()
        self.answers.append({'outcome': 'accepted', 'cancel': 'cancelled',
                             'stop': {'status': 'stopping', 'command_id': COMMAND['command_id']}})
        result = self.service.cancel(OWNER, rid)
        self.assertEqual(self.calls[0][3], {'status': 'running', 'command_id': COMMAND['command_id']})
        self.assertEqual(result['stop'], {'status': 'stopping', 'command_id': COMMAND['command_id']})
        self.assertEqual(self.row(rid), ('RUNNING', None, 'stop_requested'))
        self.assertEqual(self.code(self.service.cancel, OWNER, rid).code, 'invalid_transition', 'asked once')
        self.assertEqual(len(self.calls), 1)

    def test_task_changed_reverts_the_cancel_and_is_refused_as_token_mismatch(self):
        rid = self.running_with_command()
        self.answers.append({'outcome': 'refused', 'reason': 'task_changed'})
        error = self.code(self.service.cancel, OWNER, rid)
        self.assertEqual((error.code, error.state), ('token_mismatch', 'RUNNING'))
        self.assertEqual(self.row(rid), ('RUNNING', None, None), 'nothing stays requested')
        self.assertEqual(self.service.cancel(OWNER, rid)['status'], 'cancel_requested', 'a new cancel may follow')
        self.assertEqual(len(self.calls), 2)

    def test_a_backend_that_could_not_be_asked_reverts_the_cancel_with_its_reason(self):
        for reason in ('backend_not_running', 'backend_unreachable', 'backend_too_old'):
            with self.subTest(reason=reason):
                rid = self.make('READY')
                self.answers.append({'outcome': 'refused', 'reason': reason})
                self.assertEqual(self.code(self.service.cancel, OWNER, rid).code, reason)
                self.assertEqual(self.row(rid), ('READY', None, None))

    def test_an_unknown_answer_is_surfaced_and_stays_requested_never_a_success(self):
        rid = self.running_with_command()
        self.answers.append({'outcome': 'unknown'})
        result = self.service.cancel(OWNER, rid)
        self.assertEqual((result['status'], result['cancel']), ('cancel_requested', 'unknown'), result)
        self.assertEqual(self.row(rid), ('RUNNING', None, 'requested'))
        self.assertTrue(any('알 수 없' in body for body in self.system_comments(rid)), self.system_comments(rid))

    def test_pre_dispatch_cancel_does_not_ask_the_backend(self):
        rid = self.make('REVIEWING')
        self.assertEqual(self.service.cancel(OWNER, rid)['status'], 'cancelled')
        self.assertEqual(self.calls, [])


class ReviewCorrectionTests(PortCase):
    def test_a_request_left_requested_may_be_cancelled_again_and_is_sent_again(self):
        """Review P1: after an unknown answer (or a Hub restart) the request is not stuck in ``requested``."""
        rid = self.running_with_command()
        self.answers.append({'outcome': 'unknown'})
        self.assertEqual(self.service.cancel(OWNER, rid)['cancel'], 'unknown')
        self.service.observe_task(rid, 'task-1', 'cancelling', view={'command': dict(COMMAND, state='stopping')})
        self.answers.append({'outcome': 'accepted', 'cancel': 'cancel_requested', 'stop': None, 'repeat': True})
        again = self.service.cancel(OWNER, rid)
        self.assertEqual(again['status'], 'cancel_requested', again)
        self.assertEqual(self.calls[1][3], {'status': 'cancelling', 'command_id': COMMAND['command_id']},
                         'expect is computed again from the current view')
        self.answers.append({'outcome': 'accepted', 'cancel': 'cancelled',
                             'stop': {'status': 'stopping', 'command_id': COMMAND['command_id']}})
        self.service.cancel(OWNER, rid)
        self.assertEqual(self.row(rid)[2], 'stop_requested')
        self.assertEqual(self.code(self.service.cancel, OWNER, rid).code, 'invalid_transition',
                         'only a started stop refuses another cancel')
        self.assertEqual(len(self.calls), 3)

    def test_a_stop_refused_at_once_is_not_reported_as_stopping(self):
        rid = self.running_with_command()
        self.answers.append({'outcome': 'accepted', 'cancel': 'cancelled',
                             'stop': {'status': 'refused', 'reason': 'not_owner',
                                      'command_id': COMMAND['command_id']}})
        result = self.service.cancel(OWNER, rid)
        self.assertEqual(result['stop']['status'], 'refused')
        self.assertEqual(self.row(rid)[2], 'requested', 'no stop under way')
        comments = self.system_comments(rid)
        self.assertFalse(any('멈추는 중' in body for body in comments), comments)
        self.assertTrue(any('not_owner' in body for body in comments), comments)

    def test_a_survivor_of_a_previous_backend_keeps_the_request_open_with_one_comment(self):
        rid = self.running_with_command()
        self.answers.append({'outcome': 'accepted', 'cancel': 'cancelled',
                             'stop': {'status': 'stopping', 'command_id': COMMAND['command_id']}})
        self.service.cancel(OWNER, rid)
        before = len(self.system_comments(rid))
        for _ in range(3):
            self.service.observe_task(rid, 'task-1', 'closed', closed_reason='cancelled', user_cancelled=True,
                                      view={'command': dict(COMMAND, state='survivor')})
        comments = self.system_comments(rid)[before:]
        self.assertEqual(comments, ['명령이 아직 실행 중일 수 있습니다 — 확인 필요'])
        self.assertEqual(self.row(rid), ('RUNNING', None, 'stop_requested'), 'never closed over a live command')


class TaskSyncCloseTests(PortCase):
    def cancelled(self) -> str:
        rid = self.running_with_command()
        self.answers.append({'outcome': 'accepted', 'cancel': 'cancelled',
                             'stop': {'status': 'stopping', 'command_id': COMMAND['command_id']}})
        self.service.cancel(OWNER, rid)
        return rid

    def test_the_request_closes_only_after_the_task_closed_and_its_command_ended(self):
        rid = self.cancelled()
        stopping = dict(COMMAND, state='stopping')
        self.service.observe_task(rid, 'task-1', 'closed', closed_reason='cancelled', user_cancelled=True,
                                  view={'command': stopping})
        self.assertEqual(self.row(rid), ('RUNNING', None, 'stop_requested'), 'the command still runs')
        self.assertNotIn('linked_task_closed', [event.type for event in self.events])
        self.service.observe_task(rid, 'task-1', 'closed', closed_reason='cancelled', user_cancelled=True,
                                  view={'command': None})
        self.assertEqual(self.row(rid), ('CLOSED', 'cancelled', None))
        delivery = self.rows('SELECT delivery_state, delivery_id FROM requests WHERE request_id = ?', rid)[0]
        self.assertEqual(delivery, ('pending', None), 'the manager gets the cancelled notice')
        view = self.service.delivery_view(self.a)['requests']
        self.assertEqual([(item['request_id'], item['event']) for item in view], [(rid, 'cancelled')])

    def test_a_stop_that_ended_unknown_is_a_system_comment_once_and_no_close(self):
        rid = self.cancelled()
        before = len(self.system_comments(rid))
        unknown = dict(COMMAND, state='unknown')
        for _ in range(3):
            self.service.observe_task(rid, 'task-1', 'closed', closed_reason='cancelled', user_cancelled=True,
                                      view={'command': unknown})
        comments = self.system_comments(rid)[before:]
        self.assertEqual(len(comments), 1, comments)
        self.assertIn('unknown', comments[0])
        self.assertEqual(self.row(rid)[:2], ('RUNNING', None), 'never a success while the stop is unknown')


class SyncViewTests(PortCase):
    """The dispatcher's Task sync keeps the backend's ``task_status.command`` in ``task_view.command`` (what the
    cancel confirmation shows and binds) and closes a user-cancelled request once that command is gone."""

    def test_the_running_command_reaches_the_view_and_its_end_closes_the_cancelled_request(self):
        from workbench.hub.inbox.dispatch import Dispatcher
        rid = self.running_with_command()
        status = {'task_id': 'task-1', 'exists': True, 'status': 'running', 'closed_reason': None, 'revision': 1,
                  'cancel_requested_by': None, 'runs': [], 'commands': [], 'command': dict(COMMAND)}

        class Port:
            @staticmethod
            def identity(root):
                return '100:5'

            @staticmethod
            def inbox_notice(*_args, **_kwargs):
                return {'queued': True, 'notice_id': 'n'}

            @staticmethod
            def task_status(root, task_ids, **_kwargs):
                return [dict(status, task_id=task_id) for task_id in task_ids]
        refs = [ProjectRef(self.a, self.root, 'alpha')]
        dispatcher = Dispatcher(self.service, lambda: refs, port=Port(), sync_workers=0)
        dispatcher.tick()
        view = self.service.get(OWNER, rid)['task']['view']
        self.assertEqual(view['command'], COMMAND)
        self.service.cancel(OWNER, rid)
        status.update(status='closed', closed_reason='cancelled', cancel_requested_by='user',
                      command=dict(COMMAND, state='stopping'))
        dispatcher.tick()
        self.assertEqual(self.row(rid)[0], 'RUNNING', 'the command is still being stopped')
        status['command'] = None
        dispatcher.tick()
        self.assertEqual(self.row(rid)[:2], ('CLOSED', 'cancelled'))


def _live_base():
    from test_inbox_flow_link import LinkCase
    return LinkCase


class LiveCancelTests(_live_base()):
    """T6: a real backend (fake OMPs), its worker running a host ``sleep`` for a linked Task, and the real Hub with
    its web listener, ``/api/v1/`` and an editor API key."""

    def hub_with_web(self, *titles: str):
        import subprocess
        import time as _time
        from workbench.hub import auth
        config = Path(self.box.env['XDG_CONFIG_HOME']) / 'omp-workbench'
        config.mkdir(parents=True, exist_ok=True, mode=0o700)
        (config / 'hub.toml').write_text('[web]\nenabled = true\nbind = "127.0.0.1"\nport = 0\n')
        done = subprocess.run([sys.executable, '-m', 'workbench', 'hub', 'passwd', '--stdin'], env=self.box.env,
                              cwd=self.box.base, input='correct horse battery\n', capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        root, project_id, ids, ui = self.linked_hub('alpha', *titles)
        from fixtures.web_sandbox import tcp_listening, wait_until as web_wait
        pid = json.loads((self.box.hub_state / 'hub.json').read_text())['pid']
        found: list = []
        web_wait(lambda: bool(found.extend(tcp_listening(pid)) or found), 15)
        self.assertEqual(len(found), 1, found)
        self.host, self.port = found[0]
        db = auth.HubDb.open(self.box.hub_state)
        try:
            self.key = auth.ApiKeyStore(db).issue('ti13', 'editor', {'projects': [project_id]}, None,
                                                  now=_time.time()).raw
        finally:
            db.close()
        return root, project_id, ids, ui

    def rest(self):
        from support.rest_client import RestClient
        from test_inbox_results_independent import RestTools  # REST calls named by operation
        return RestTools(RestClient(self.host, self.port, self.key))

    def running_command(self, ui, root: Path, rid: str, seconds: str) -> tuple[str, int]:
        """A linked Task whose worker runs ``sleep SECONDS`` in the host shell; its Task id and the sleep pid."""
        answer = self.to_worker(ui, rid)
        self.assertEqual(answer['status'], 'dispatched', answer)
        self.until(lambda: self.worker_tasks(), 30, 'the TASK went to the worker')
        self.until(lambda: self.row(root, rid)['state'] == 'RUNNING', 30)
        sent = ui.request(self.input_type(), f'tool terminal {json.dumps({"command": f"sleep {seconds}"})}\n'.encode(),
                          pane='worker_omp')
        self.assertTrue(sent.get('ok'), sent)
        from test_project_control import wait_until as proc_wait
        pids: list = []
        proc_wait(lambda: bool(pids.extend(sleeps(seconds)) or pids), 30)
        self.assertEqual(len(pids), 1, pids)
        self.until(lambda: (self.view(root, rid).get('command') or {}).get('label') == f'sleep {seconds}', 30,
                   'the Hub sees the running command')
        return answer['task_id'], pids[0]

    @staticmethod
    def input_type():
        from workbench.contracts.ui_v1 import ClientType
        return ClientType.INPUT

    def stop_records(self, root: Path) -> list[dict]:
        found = []
        for path in (root / '.wb').rglob('*.jsonl'):
            for line in path.read_text(errors='replace').splitlines():
                if '"command_stop_requested"' in line:
                    found.append(json.loads(line))
        return found

    def flow_cancels(self, root: Path) -> list[dict]:
        found = []
        for path in (root / '.wb').rglob('tasks-flow.jsonl'):
            found += [json.loads(line) for line in path.read_text().splitlines()
                      if '"cancel_requested"' in line]
        return [item for item in found if item.get('type') == 'cancel_requested']

    def test_rest_two_step_cancel_stops_the_command_and_closes_only_after_it_ended(self):
        root, _project_id, (rid,), ui = self.hub_with_web('Run the long job')
        task_id, pid = self.running_command(ui, root, rid, '714.1')
        client = self.rest()
        first = client.tool('inbox_cancel', request_id=rid)
        self.assertEqual(first['status'], 'confirm_required', first)
        command = first['impact']['command']
        self.assertEqual((first['impact']['task_id'], command['label'], command['state']),
                         (task_id, 'sleep 714.1', 'running'))
        self.assertTrue(os.path.exists(f'/proc/{pid}'), 'the first call stops nothing')
        done = client.tool('inbox_cancel', request_id=rid, confirm_token=first['confirm_token'])
        self.assertEqual(done['status'], 'cancel_requested', done)
        self.assertEqual(done['stop'], {'status': 'stopping', 'command_id': command['command_id']})
        self.assertNotEqual(self.row(root, rid)['state'], 'CLOSED', 'accepted is not cancelled (P2-AC-30)')
        self.until(lambda: not os.path.exists(f'/proc/{pid}'), 30, 'the command ended')
        self.until(lambda: self.row(root, rid)['state'] == 'CLOSED', 40, 'Task sync closed the request')
        row = self.row(root, rid)
        self.assertEqual((row['outcome'], row['cancel_state']), ('cancelled', None))
        requested = self.stop_records(root)
        self.assertEqual([(item['command_id'], item['requested_by']) for item in requested],
                         [(command['command_id'], 'user')], 'one user stop of exactly that command')
        self.assertEqual([(item['task_id'], item['by']) for item in self.flow_cancels(root)], [(task_id, 'user')])

    def test_a_task_that_changed_before_the_confirmation_is_refused_and_nothing_is_stopped(self):
        root, _project_id, (rid,), ui = self.hub_with_web('Short job')
        task_id, pid = self.running_command(ui, root, rid, '4.1')
        client = self.rest()
        first = client.tool('inbox_cancel', request_id=rid)
        self.assertEqual(first['status'], 'confirm_required', first)
        self.until(lambda: not os.path.exists(f'/proc/{pid}'), 30, 'the command ended on its own')
        # the Task has changed as far as the Hub knows (else the confirmation races the backend noticing the exit)
        self.until(lambda: not (self.view(root, rid).get('command') or {}).get('command_id'), 30,
                   'the Hub saw the command end')
        refused = client.call('inbox_cancel', request_id=rid, confirm_token=first['confirm_token'])
        self.assertEqual((refused.status, refused.error['code']), (409, 'token_mismatch'), refused.body)
        self.assertEqual(self.stop_records(root), [], 'P2-AC-41: no command stop record')
        self.assertEqual(self.flow_cancels(root), [], 'the Task was not cancelled')
        row = self.row(root, rid)
        self.assertEqual((row['state'], row['cancel_state']), ('RUNNING', None))

    def test_web_confirm_page_cancels_a_running_task_without_a_command(self):
        import re
        from test_web_requests_hub import Browser
        root, _project_id, (rid,), ui = self.hub_with_web('Plain work')
        answer = self.to_worker(ui, rid)
        task_id = answer['task_id']
        self.until(lambda: self.row(root, rid)['state'] == 'RUNNING', 30)
        browser = Browser(self.host, self.port)
        _status, _headers, page = browser.request('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        status, _headers, _ = browser.request('POST', '/login', {'login_token': token,
                                                                 'password': 'correct horse battery'})
        self.assertEqual(status, 303)
        status, _headers, page = browser.request('GET', f'/requests/{rid}/cancel')
        self.assertEqual(status, 200, page)
        self.assertIn(task_id, page, 'the confirm page shows the Task')
        fields = {name: re.search(rf'name="{name}" value="([^"]+)"', page).group(1)
                  for name in ('csrf', 'base_version', 'token')}
        status, headers, _ = browser.request('POST', f'/requests/{rid}/cancel', fields)
        self.assertEqual(status, 303)
        self.assertIn('notice=cancel_requested', headers['Location'])
        self.until(lambda: self.row(root, rid)['state'] == 'CLOSED', 40)
        self.assertEqual(self.row(root, rid)['outcome'], 'cancelled')
        self.assertEqual([(item['task_id'], item['by']) for item in self.flow_cancels(root)], [(task_id, 'user')])
        self.assertEqual(self.stop_records(root), [])
        self.until(lambda: [n for n in self.inbox_notices() if n['request_id'] == rid and n['event'] == 'cancelled'],
                   40, 'the manager gets the cancelled notice')


def sleeps(seconds: str) -> list[int]:
    found = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            argv = Path(f'/proc/{name}/cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if argv[:2] == [b'sleep', seconds.encode()]:
            found.append(int(name))
    return found


if __name__ == '__main__':
    unittest.main()
