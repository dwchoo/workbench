"""TI-14 request results: summary by default, full view, raw pages and artifact pages (P2-AC-31, TI-T10, P2-AC-13).

Seams: T4 (MCP ``inbox_result`` through a real loopback Hub web app and the stdlib MCP client), T2 (the web request
pages through the real ``create_app``) and T6 (the Hub's raw page read through a real backend's ui_v1 ``raw_query``).
In T4/T2 the backend is the test's port object: ``task_status``/``raw_query`` answer what the test chose, in the
shapes of the ui_v1 contract (Task Inbox SPEC "Hub → backend"), so the expected values are the test's own bytes,
files and symlinks. Run with ``discover -s tests/hub -p 'test_inbox_results*.py'``.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
import unittest
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support.mcp_hub import McpHub  # noqa: E402
import test_web_pages_requests as pages  # noqa: E402  (module import: its test classes stay uncollected here)
from workbench.hub.inbox import results  # noqa: E402
from workbench.hub.inbox.backend_port import BackendPortError  # noqa: E402
from workbench.hub.inbox.service import Principal  # noqa: E402
from workbench.hub.web.pages import requests as request_pages  # noqa: E402

TASK = str(uuid.uuid4())
RUN = str(uuid.uuid4())
COMMAND = str(uuid.uuid4())
RUN_LOG = b'epoch 1 loss 0.5\nepoch 2 loss 0.25\n' + bytes(range(256)) * 4
COMMAND_LOG = 'python train.py\n\xec\x99\x84\xeb\xa3\x8c \xed\x95\x99\xec\x8a\xb5\n'.encode('latin-1')
REPORT = 'Worker report: accuracy 0.93\nall checks passed'


class FakeBackend:
    """The backend side of ``results`` (``task_status``/``raw_query``) with the test's own Task, runs and logs."""

    def __init__(self):
        self.down: str | None = None  # a BackendPortError reason every call raises
        self.tasks = {TASK: {'task_id': TASK, 'exists': True, 'status': 'finished', 'closed_reason': None,
                             'revision': 1, 'cancel_requested_by': None,
                             'runs': [{'run_id': RUN, 'status': 'finished', 'exit_code': 0, 'started_at': 10.0,
                                       'ended_at': 20.0, 'stopped_by': None,
                                       'log_sources': [{'source_kind': 'run', 'source_id': RUN}]}],
                             'commands': [{'command_id': COMMAND, 'owner': 'worker', 'exit_code': 0,
                                           'log_path': '/home/someone/.wb/workflow/terminal/x.log'}],
                             'reports': [{'message_id': 'm-1', 'kind': 'result', 'at': 30.0, 'text': REPORT}]}}
        self.logs = {('run', RUN): RUN_LOG, ('command', COMMAND): COMMAND_LOG}
        self.calls: list[tuple] = []

    def task_status(self, root, task_ids, *, detail=False, **_):
        self.calls.append(('task_status', Path(root), tuple(task_ids), detail))
        if self.down:
            raise BackendPortError(self.down)
        items = []
        for task_id in task_ids:
            item = json.loads(json.dumps(self.tasks.get(task_id, {'task_id': task_id, 'exists': False})))
            if not detail:
                item.pop('reports', None)
            items.append(item)
        return items

    def raw_query(self, root, kind, source_id, offset, limit, **_):
        """The legacy-file answer of ``workbench.storage.log_raw.port.raw_query`` (page ≤ 32 KiB)."""
        self.calls.append(('raw_query', Path(root), kind, source_id, offset, limit))
        if self.down:
            raise BackendPortError(self.down)
        assert 1 <= limit <= 32 * 1024 and offset >= 0
        data = self.logs[(kind, source_id)]
        chunk = data[offset:offset + limit]
        return {'status': 'available', 'kind': kind, 'id': source_id, 'log_format': 'legacy-file',
                'log_path': f'/home/someone/.wb/raw-logs/{source_id}.log', 'observed_bytes': len(data),
                'read': {'start': offset, 'end': offset + len(chunk),
                         'dataBase64': base64.b64encode(chunk).decode('ascii'), 'missing': []},
                'next_offset': offset + len(chunk) if offset + len(chunk) < len(data) else None}


# --- T4: MCP ---------------------------------------------------------------------------------------

class McpResultCase(unittest.TestCase):
    def setUp(self):
        self.hub = McpHub(self)
        self.backend = FakeBackend()
        self.hub.server.results = results.Results(self.hub.inbox, port=self.backend)
        self.r1 = self.hub.client(self.hub.key('r1', 'requester', {'projects': [self.hub.a]})).start()
        self.r2 = self.hub.client(self.hub.key('r2', 'requester', {'projects': [self.hub.a]})).start()
        self.rid = self.r1.tool('inbox_submit', project_id=self.hub.a, title='Train', body='Run it',
                                idempotency_key='k-1')['request_id']

    def linked(self, rid: str | None = None, view: dict | None = None) -> str:
        rid = rid or self.rid
        self.hub.link_running(rid, task_id=TASK, view=view or {'status': 'running', 'runs': [
            {'run_id': RUN, 'status': 'running', 'exit_code': None}]})
        return rid


class SummaryTests(McpResultCase):
    def test_default_is_the_summary_and_full_false_is_the_same(self):
        self.linked()
        value = self.r1.tool('inbox_result', request_id=self.rid)
        self.assertEqual(set(value), {'summary', 'state', 'outcome', 'task'})
        self.assertEqual(value['task'], {'status': 'running', 'runs': [{'run_id': RUN, 'status': 'running',
                                                                        'exit_code': None}]})
        self.assertIn(f'run {RUN}: running', value['summary'])
        self.assertEqual(self.r1.tool('inbox_result', request_id=self.rid, full=False), value)
        self.assertEqual(self.backend.calls, [], 'the summary needs no backend call')

    def test_summary_of_a_closed_request_is_the_managers_close_summary(self):
        self.linked()
        self.hub.inbox.review(Principal.manager(self.hub.a), self.rid, 'close', outcome='success',
                              summary='Done: accuracy 0.93')
        value = self.r1.tool('inbox_result', request_id=self.rid)
        self.assertEqual((value['summary'], value['state'], value['outcome']),
                         ('Done: accuracy 0.93', 'CLOSED', 'success'))

    def test_page_arguments_without_full_add_nothing(self):
        self.linked()
        (self.hub.root_a / 'out.txt').write_text('ARTIFACT-TEXT')
        value = self.r1.tool('inbox_result', request_id=self.rid, run_id=RUN, artifact_path='out.txt')
        self.assertEqual(set(value), {'summary', 'state', 'outcome', 'task'})
        self.assertNotIn('ARTIFACT-TEXT', json.dumps(value))


class FullTests(McpResultCase):
    def test_full_has_the_runs_worker_reports_and_commands_of_the_linked_task(self):
        self.linked()
        value = self.r1.tool('inbox_result', request_id=self.rid, full=True)
        self.assertEqual((value['state'], value['summary'] is not None), ('RUNNING', True))
        full = value['full']
        self.assertEqual(full['available'], True)
        task = full['task']
        self.assertEqual((task['task_id'], task['status'], task['closed_reason']), (TASK, 'finished', None))
        self.assertEqual(task['runs'], [{'run_id': RUN, 'status': 'finished', 'exit_code': 0, 'started_at': 10.0,
                                         'ended_at': 20.0, 'stopped_by': None,
                                         'raw': [{'source_kind': 'run', 'source_id': RUN}]}])
        self.assertEqual(task['commands'], [{'command_id': COMMAND, 'owner': 'worker', 'exit_code': 0,
                                             'raw': [{'source_kind': 'command', 'source_id': COMMAND}]}])
        self.assertEqual(task['reports'], [{'message_id': 'm-1', 'kind': 'result', 'at': 30.0, 'text': REPORT}])
        self.assertNotIn('/home/someone', json.dumps(value), 'no host log path reaches the client')
        self.assertEqual(self.backend.calls, [('task_status', self.hub.root_a, (TASK,), True)])

    def test_full_of_a_request_without_a_task_has_no_task_and_calls_no_backend(self):
        value = self.r1.tool('inbox_result', request_id=self.rid, full=True)
        self.assertEqual(value['full'], {'available': True, 'task': None})
        self.assertEqual(self.backend.calls, [])

    def test_a_task_the_backend_no_longer_knows_shows_the_last_view(self):
        self.linked()
        del self.backend.tasks[TASK]
        full = self.r1.tool('inbox_result', request_id=self.rid, full=True)['full']
        self.assertEqual((full['available'], full['reason']), (False, 'task_not_found'))
        self.assertEqual(full['view']['runs'][0]['run_id'], RUN)


class RawTests(McpResultCase):
    def raw(self, **arguments) -> dict:
        return self.r1.tool('inbox_result', request_id=self.rid, full=True, **arguments)['raw']

    def test_a_run_log_page_is_the_bytes_at_offset_and_length(self):
        self.linked()
        page = self.raw(run_id=RUN, offset=0, length=17)
        self.assertEqual(page, {'run_id': RUN, 'source_kind': 'run', 'offset': 0, 'length': 17,
                                'size': len(RUN_LOG), 'next_offset': 17, 'text': RUN_LOG[:17].decode(),
                                'missing': []})
        page = self.raw(run_id=RUN, offset=40, length=300)
        self.assertEqual(base64.b64decode(page['data_base64']), RUN_LOG[40:340], 'non-UTF-8 bytes come as base64')
        self.assertNotIn('text', page)
        self.assertEqual(page['next_offset'], 340)

    def test_the_default_page_is_131072_bytes_from_the_start_over_several_backend_reads(self):
        self.linked()
        data = (b'0123456789abcdef' * 9000)[:140000]
        self.backend.logs[('run', RUN)] = data
        page = self.raw(run_id=RUN)
        self.assertEqual(page['text'].encode(), data[:131072])
        self.assertEqual((page['offset'], page['length'], page['next_offset'], page['size']),
                         (0, 131072, 131072, 140000))
        reads = [call for call in self.backend.calls if call[0] == 'raw_query']
        self.assertEqual([(call[4], call[5]) for call in reads], [(0, 32768), (32768, 32768), (65536, 32768),
                                                                  (98304, 32768)])
        last = self.raw(run_id=RUN, offset=131072)
        self.assertEqual((last['text'].encode(), last['next_offset']), (data[131072:], None))

    def test_a_command_log_of_the_linked_task_is_read_by_its_command_id(self):
        self.linked()
        page = self.raw(run_id=COMMAND)
        self.assertEqual(page['source_kind'], 'command')
        self.assertEqual(page['text'].encode(), COMMAND_LOG)
        self.assertEqual(self.backend.calls[-1][1:4], (self.hub.root_a, 'command', COMMAND))

    def test_offset_at_the_end_is_an_empty_page_and_past_it_is_refused(self):
        self.linked()
        end = self.raw(run_id=RUN, offset=len(RUN_LOG))
        self.assertEqual((end['length'], end['text'], end['next_offset']), (0, '', None))
        for offset in (len(RUN_LOG) + 1, 10 ** 15):
            with self.subTest(offset=offset):
                error = self.r1.tool_error('inbox_result', request_id=self.rid, full=True, run_id=RUN, offset=offset)
                self.assertEqual(error['code'], 'invalid_input')

    def test_a_run_of_another_task_or_project_is_not_found_and_never_read(self):
        self.linked()
        other_task, other_run = str(uuid.uuid4()), str(uuid.uuid4())
        self.backend.tasks[other_task] = {**self.backend.tasks[TASK], 'task_id': other_task,
                                          'runs': [{'run_id': other_run, 'status': 'finished', 'exit_code': 0}],
                                          'commands': []}
        self.backend.logs[('run', other_run)] = b'OTHER-PROJECT-LOG'
        editor = self.hub.client(self.hub.key('ed', 'editor')).start()
        theirs = editor.tool('inbox_submit', project_id=self.hub.b, title='B', body='b', idempotency_key='b-1')
        self.hub.link_running(theirs['request_id'], self.hub.b, task_id=other_task)
        for run_id in (other_run, str(uuid.uuid4()), 'not-a-uuid', '../../etc/passwd'):
            with self.subTest(run_id=run_id):
                error = self.r1.tool_error('inbox_result', request_id=self.rid, full=True, run_id=run_id)
                self.assertEqual(error['code'], 'not_found')
        self.assertFalse([call for call in self.backend.calls if call[0] == 'raw_query'])

    def test_a_request_without_a_task_has_no_raw_source(self):
        error = self.r1.tool_error('inbox_result', request_id=self.rid, full=True, run_id=RUN)
        self.assertEqual(error['code'], 'not_found')
        self.assertEqual(self.backend.calls, [])


class ArtifactCase(McpResultCase):
    def artifact(self, path: str, **arguments) -> dict:
        return self.r1.tool('inbox_result', request_id=self.rid, full=True, artifact_path=path, **arguments)

    def artifact_error(self, path: str, **arguments) -> dict:
        return self.r1.tool_error('inbox_result', request_id=self.rid, full=True, artifact_path=path, **arguments)


class ArtifactTests(ArtifactCase):
    def test_a_regular_file_under_the_root_is_read_by_relative_or_absolute_path(self):
        (self.hub.root_a / 'out').mkdir()
        data = 'metrics: accuracy 0.93\n결과\n'.encode() * 3
        (self.hub.root_a / 'out' / 'metrics.txt').write_bytes(data)
        for path in ('out/metrics.txt', str(self.hub.root_a / 'out' / 'metrics.txt'), './out/./metrics.txt',
                     'out//metrics.txt'):
            with self.subTest(path=path):
                value = self.artifact(path)
                self.assertEqual(value['artifact'], {'path': 'out/metrics.txt', 'offset': 0, 'length': len(data),
                                                     'size': len(data), 'next_offset': None, 'text': data.decode()})
                self.assertEqual(value['state'], 'NEW')

    def test_pages_by_offset_and_length_and_binary_as_base64(self):
        data = bytes(range(256)) * 1024  # 256 KiB, not UTF-8
        (self.hub.root_a / 'model.bin').write_bytes(data)
        first = self.artifact('model.bin')['artifact']
        self.assertEqual((first['offset'], first['length'], first['size'], first['next_offset']),
                         (0, 131072, len(data), 131072))
        self.assertEqual(base64.b64decode(first['data_base64']), data[:131072])
        middle = self.artifact('model.bin', offset=1000, length=10)['artifact']
        self.assertEqual(base64.b64decode(middle['data_base64']), data[1000:1010])
        self.assertEqual(middle['next_offset'], 1010)
        last = self.artifact('model.bin', offset=len(data) - 5)['artifact']
        self.assertEqual((base64.b64decode(last['data_base64']), last['next_offset']), (data[-5:], None))
        end = self.artifact('model.bin', offset=len(data))['artifact']
        self.assertEqual((end['length'], end['text'], end['next_offset']), (0, '', None))
        self.assertEqual(self.artifact_error('model.bin', offset=len(data) + 1)['code'], 'invalid_input')

    def test_an_artifact_needs_no_backend_call(self):
        (self.hub.root_a / 'a.txt').write_text('A')
        self.linked()
        self.backend.down = 'backend_not_running'
        self.assertEqual(self.artifact('a.txt')['artifact']['text'], 'A')
        self.assertEqual(self.backend.calls, [])


class BackendOffTests(McpResultCase):
    def test_full_with_the_backend_off_is_the_last_task_view_and_raw_is_backend_not_running(self):
        self.linked()
        self.backend.down = 'backend_not_running'
        value = self.r1.tool('inbox_result', request_id=self.rid, full=True)
        self.assertIn(f'run {RUN}: running', value['summary'])
        self.assertEqual(value['full'], {'available': False, 'reason': 'backend_not_running', 'view': {
            'status': 'running', 'closed': None, 'closed_reason': None,
            'runs': [{'run_id': RUN, 'status': 'running', 'exit_code': None, 'started_at': None, 'ended_at': None}]}})

    def test_the_last_view_shows_only_the_task_and_run_fields(self):
        self.linked(view={'status': 'running', 'runs': [{'run_id': RUN, 'status': 'running', 'exit_code': None,
                                                         'log': 'RAW-LOG-TEXT'}], 'note': 'OTHER-FIELD'})
        self.backend.down = 'backend_not_running'
        full = self.r1.tool('inbox_result', request_id=self.rid, full=True)['full']
        self.assertNotIn('RAW-LOG-TEXT', json.dumps(full))
        self.assertNotIn('OTHER-FIELD', json.dumps(full))
        error = self.r1.tool_error('inbox_result', request_id=self.rid, full=True, run_id=RUN)
        self.assertEqual(error['code'], 'backend_not_running')

    def test_other_backend_failures_read_as_unreachable(self):
        self.linked()
        for reason in ('backend_unreachable', 'backend_too_old', 'unknown', 'invalid_message'):
            with self.subTest(reason=reason):
                self.backend.down = reason
                full = self.r1.tool('inbox_result', request_id=self.rid, full=True)['full']
                self.assertEqual((full['available'], full['reason']), (False, 'backend_unreachable'))
                error = self.r1.tool_error('inbox_result', request_id=self.rid, full=True, run_id=RUN)
                self.assertEqual(error['code'], 'backend_unreachable')

    def test_the_real_port_on_a_project_whose_backend_is_off(self):
        self.hub.server.results = results.Results(self.hub.inbox)
        self.linked()
        (self.hub.root_a / 'a.txt').write_text('A')
        full = self.r1.tool('inbox_result', request_id=self.rid, full=True)['full']
        self.assertEqual((full['available'], full['reason']), (False, 'backend_not_running'))
        self.assertEqual(self.r1.tool_error('inbox_result', request_id=self.rid, full=True, run_id=RUN)['code'],
                         'backend_not_running')
        self.assertEqual(self.r1.tool('inbox_result', request_id=self.rid, full=True,
                                      artifact_path='a.txt')['artifact']['text'], 'A')


class PermissionTests(McpResultCase):
    CALLS = ({'full': True}, {'full': True, 'run_id': RUN}, {'full': True, 'artifact_path': 'a.txt'}, {})

    def setUp(self):
        super().setUp()
        self.linked()
        (self.hub.root_a / 'a.txt').write_text('A-ARTIFACT')

    def test_the_requester_gets_the_full_view_of_its_own_request(self):
        for arguments in self.CALLS:
            with self.subTest(arguments=arguments):
                self.r1.tool('inbox_result', request_id=self.rid, **arguments)

    def test_another_requester_and_an_out_of_scope_editor_get_not_found_and_nothing_is_read(self):
        outside = self.hub.client(self.hub.key('ed-b', 'editor', {'projects': [self.hub.b]})).start()
        for client in (self.r2, outside):
            for arguments in self.CALLS:
                with self.subTest(arguments=arguments):
                    call = client.call('inbox_result', request_id=self.rid, **arguments)
                    self.assertEqual(call['structuredContent']['error']['code'], 'not_found')
                    self.assertNotIn('A-ARTIFACT', json.dumps(call))
        self.assertEqual(self.backend.calls, [])

    def test_editors_in_scope_and_the_board_agent_see_every_request_in_scope(self):
        editor = self.hub.client(self.hub.key('ed-a', 'editor', {'projects': [self.hub.a]})).start()
        board = self.hub.client(self.hub.keys.rotate_system_key(now=self.hub.now[0]).raw).start()
        clients = [editor, board]
        for client in clients:
            value = client.tool('inbox_result', request_id=self.rid, full=True, artifact_path='a.txt')
            self.assertEqual(value['artifact']['text'], 'A-ARTIFACT')
            self.assertEqual(client.tool('inbox_result', request_id=self.rid, full=True, run_id=RUN)['raw']['size'],
                             len(RUN_LOG))


LEAK = ('/dev/shm', '/tmp', '/home', '.wb', 'Traceback', 'Errno', 'OUTSIDE-SECRET', 'WB-PRIVATE', 'GIT-PRIVATE')


class ArtifactRefusalTests(ArtifactCase):
    def setUp(self):
        super().setUp()
        root = self.hub.root_a
        self.outside = self.hub.base / 'outside-secret.txt'
        self.outside.write_text('OUTSIDE-SECRET')
        (self.hub.base / 'outside-dir').mkdir()
        (self.hub.base / 'outside-dir' / 'inside.txt').write_text('OUTSIDE-SECRET in a dir')
        (root / 'inside.txt').write_text('INSIDE-PUBLIC')
        (root / 'sub').mkdir()
        (root / 'sub' / 'note.txt').write_text('SUB-NOTE')
        (root / '.wb' / 'agent.db').write_text('WB-PRIVATE')
        (root / '.git').mkdir()
        (root / '.git' / 'config').write_text('GIT-PRIVATE')
        (root / 'sub' / '.git').mkdir()
        (root / 'sub' / '.git' / 'config').write_text('GIT-PRIVATE nested')
        (root / '.WB').mkdir()
        (root / '.WB' / 'x').write_text('WB-PRIVATE case')

    def assert_refused(self, path: str, code: str) -> None:
        error = self.artifact_error(path)
        self.assertEqual(error['code'], code, (path, error))
        text = json.dumps(error)
        for word in LEAK:
            self.assertNotIn(word, text)

    def test_paths_out_of_the_root_are_forbidden(self):
        for path in ('../outside-secret.txt', 'sub/../../outside-secret.txt', str(self.outside), '/etc/passwd',
                     '/', '..', str(self.hub.root_b / '.wb' / 'project.json'),
                     str(self.hub.root_a) + '/../outside-secret.txt'):
            with self.subTest(path=path):
                self.assert_refused(path, 'forbidden')

    def test_symlinks_are_refused_wherever_they_point(self):
        root = self.hub.root_a
        (root / 'escape').symlink_to(self.outside)
        (root / 'escape-dir').symlink_to(self.hub.base / 'outside-dir')
        (root / 'alias').symlink_to(root / 'inside.txt')
        (root / 'sub-alias').symlink_to(root / 'sub')
        (root / 'loop').symlink_to('loop')
        for path in ('escape', 'escape-dir/inside.txt', 'alias', 'sub-alias/note.txt', 'escape-dir/../inside.txt',
                     'sub-alias/../inside.txt', 'loop', str(root / 'escape')):
            with self.subTest(path=path):
                self.assert_refused(path, 'forbidden')

    def test_the_data_and_git_directories_are_excluded(self):
        for path in ('.wb/agent.db', '.wb/project.json', './.wb/./agent.db', 'sub/../.wb/agent.db', '.wb',
                     str(self.hub.root_a / '.wb' / 'agent.db'), '.git/config', '.git', 'sub/.git/config',
                     '.WB/x', '.Git/config'):
            with self.subTest(path=path):
                self.assert_refused(path, 'forbidden')

    def test_a_hardlink_to_another_file_is_refused(self):
        os.link(self.outside, self.hub.root_a / 'hard.txt')
        self.assert_refused('hard.txt', 'forbidden')

    def test_directories_and_special_files_are_refused_without_opening_them(self):
        import socket
        import threading
        root = self.hub.root_a
        os.mkfifo(root / 'pipe')
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(root / 'sock'))

        def unblock():  # an implementation that opened the FIFO would block until a writer comes
            try:
                descriptor = os.open(root / 'pipe', os.O_WRONLY)
                os.close(descriptor)
            except OSError:
                pass
        timer = threading.Timer(5.0, unblock)
        timer.daemon = True
        timer.start()
        self.addCleanup(timer.cancel)
        for path in ('sub', '.', str(root), 'pipe', 'sock'):
            with self.subTest(path=path):
                self.assert_refused(path, 'invalid_input')

    def test_missing_files_bad_paths_and_the_other_project(self):
        for path, code in (('missing.txt', 'not_found'), ('sub/missing/x', 'not_found'), ('inside.txt/x', 'not_found'),
                           ('', 'invalid_input'), ('a\x00b', 'invalid_input'), ('x' * 5000, 'invalid_input'),
                           ('%2e%2e/outside-secret.txt', 'not_found'), ('~/.ssh/id_rsa', 'not_found')):
            with self.subTest(path=path[:40]):
                self.assert_refused(path, code)
        self.assertEqual(self.artifact('inside.txt')['artifact']['text'], 'INSIDE-PUBLIC', 'still readable')

    def test_a_component_swapped_for_a_symlink_after_the_judgement_is_not_followed(self):
        """``open_artifact`` gets components the judgement already allowed; the files change before it opens."""
        from workbench.hub.inbox.limits import InboxError
        root = str(self.hub.root_a)
        parts = results.artifact_parts(root, root, 'sub/note.txt')
        os.rename(self.hub.root_a / 'sub', self.hub.root_a / 'sub-old')
        (self.hub.root_a / 'sub').symlink_to(self.hub.base / 'outside-dir')
        (self.hub.base / 'outside-dir' / 'note.txt').write_text('OUTSIDE-SECRET swapped')
        with self.assertRaises(InboxError) as caught:
            results.open_artifact(root, parts)
        self.assertEqual(caught.exception.code, 'forbidden')
        leaf = results.artifact_parts(root, root, 'inside.txt')
        os.unlink(self.hub.root_a / 'inside.txt')
        (self.hub.root_a / 'inside.txt').symlink_to(self.outside)
        with self.assertRaises(InboxError) as caught:
            results.open_artifact(root, leaf)
        self.assertEqual(caught.exception.code, 'forbidden')


# --- T2: web pages ---------------------------------------------------------------------------------

class WebResultCase(pages.RequestsCase):
    async def asyncSetUp(self):
        self.backend = FakeBackend()
        await super().asyncSetUp()

    @property
    def routes(self):  # read by WebAppCase.asyncSetUp, after RequestsCase made the inbox
        return (request_pages.routes(self.inbox, lambda: list(self.refs),
                                     results=results.Results(self.inbox, port=self.backend)),)

    @routes.setter
    def routes(self, value):
        pass

    def linked(self, view: dict | None = None) -> str:
        rid = self.add('Train', 'Run it')
        manager = Principal.manager(self.a)
        self.inbox.review(manager, rid, 'begin_review')
        token = self.inbox.link_check(manager, rid)['link_token']
        self.inbox.link_commit(manager, token, TASK, 1, 'running')
        self.inbox.observe_task(rid, TASK, 'running', view=view or {'status': 'running', 'runs': [
            {'run_id': RUN, 'status': 'finished', 'exit_code': 0}]})
        return rid

    async def raw_get(self, path: str):
        """(status, headers, body bytes) of a GET with the session cookie."""
        headers = {'Host': self.host(), 'Cookie': f'wb_hub_session={self.cookie}'}
        async with self.http.get(self.base + path, headers=headers, allow_redirects=False) as response:
            return response.status, response.headers, await response.read()


class WebSummaryTests(WebResultCase):
    async def test_detail_shows_the_task_progress_summary_and_a_full_view_link(self):
        rid = self.linked()
        _, page = await self.page(f'/requests/{rid}')
        result = page.sections['result']
        self.assertIn(f'Task {TASK}: running', result)
        self.assertIn(f'run {RUN}: finished (exit 0)', result)
        self.assertIn(f'/requests/{rid}/full', page.links())

    async def test_detail_of_a_closed_request_shows_the_managers_summary(self):
        rid = self.linked()
        self.inbox.review(Principal.manager(self.a), rid, 'close', outcome='success', summary='Done: 0.93')
        _, page = await self.page(f'/requests/{rid}')
        self.assertIn('Done: 0.93', page.sections['result'])
        self.assertNotIn(f'Task {TASK}: running', page.sections['result'])


class WebFullTests(WebResultCase):
    async def test_full_page_lists_runs_reports_commands_and_raw_links(self):
        rid = self.linked()
        response, page = await self.page(f'/requests/{rid}/full')
        self.assertEqual(response.status, 200, response.body_text)
        self.assertIn(REPORT, page.pre)
        text = page.body_text
        self.assertIn(RUN, text)
        self.assertIn(COMMAND, text)
        links = page.links()
        self.assertIn(f'/requests/{rid}/raw?run_id={RUN}', links)
        self.assertIn(f'/requests/{rid}/raw?run_id={COMMAND}', links)
        self.assertIsNotNone(page.form(f'/requests/{rid}/artifact'), 'an artifact path form')
        self.assertNotIn('/home/someone', response.body_text)

    async def test_full_page_with_the_backend_off_shows_the_last_view(self):
        rid = self.linked()
        self.backend.down = 'backend_not_running'
        response, page = await self.page(f'/requests/{rid}/full')
        self.assertEqual(response.status, 200)
        self.assertIn('backend', page.body_text)
        self.assertIn(RUN, page.body_text)

    async def test_raw_and_artifact_pages_are_inline_plain_text(self):
        rid = self.linked()
        self.backend.logs[('run', RUN)] = b'<script>alert(1)</script>\nlog line\n'
        (self.root_a / 'report.html').write_bytes(b'<html><script>alert(2)</script></html>')
        for path, body in ((f'/requests/{rid}/raw?run_id={RUN}', b'<script>alert(1)</script>\nlog line\n'),
                           (f'/requests/{rid}/raw?run_id={RUN}&offset=8&length=6', b'alert('),
                           (f'/requests/{rid}/artifact?path=report.html', b'<html><script>alert(2)</script></html>'),
                           (f'/requests/{rid}/artifact?path=report.html&offset=6&length=8', b'<script>')):
            with self.subTest(path=path):
                status, headers, data = await self.raw_get(path)
                self.assertEqual(status, 200, data)
                self.assertEqual(headers['Content-Type'], 'text/plain; charset=utf-8')
                self.assertEqual(headers['Content-Disposition'], 'inline')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertEqual(data, body)

    async def test_raw_and_artifact_refusals_are_error_pages(self):
        rid = self.linked()
        (self.root_a / '.wb' / 'agent.db').write_text('WB-PRIVATE')
        for path, status in ((f'/requests/{rid}/artifact?path=.wb/agent.db', 403),
                             (f'/requests/{rid}/artifact?path=../x', 403),
                             (f'/requests/{rid}/artifact?path=missing', 404),
                             (f'/requests/{rid}/artifact?path=a&length=131073', 400),
                             (f'/requests/{rid}/artifact?path=a&offset=-1', 400),
                             (f'/requests/{rid}/raw?run_id={uuid.uuid4()}', 404),
                             (f'/requests/{uuid.uuid4()}/raw?run_id={RUN}', 404),
                             (f'/requests/{uuid.uuid4()}/full', 404)):
            with self.subTest(path=path):
                code, headers, data = await self.raw_get(path)
                self.assertEqual(code, status, data)
                self.assertNotIn(b'WB-PRIVATE', data)
                self.assertTrue(headers['Content-Type'].startswith('text/html'))
        self.backend.down = 'backend_not_running'
        code, _, _ = await self.raw_get(f'/requests/{rid}/raw?run_id={RUN}')
        self.assertEqual(code, 409)

    async def test_result_pages_need_a_session(self):
        rid = self.linked()
        for suffix in ('/full', f'/raw?run_id={RUN}', '/artifact?path=a'):
            with self.subTest(suffix=suffix):
                response = await self.get(f'/requests/{rid}{suffix}')
                self.assertEqual((response.status, response.headers['Location']), (303, '/login'))


# --- T6: the raw page through a real backend's ui_v1 raw_query --------------------------------------

def _bytes(page: dict) -> bytes:
    return page['text'].encode() if 'text' in page else base64.b64decode(page['data_base64'])


class RealBackendRawTests(unittest.TestCase):
    """A real backend (fake OMPs) in the ``ProjectSandbox`` (temp HOME/XDG, short private ``XDG_RUNTIME_DIR``,
    ``WORKBENCH_HUB_AUTOSTART=0``; it removes only its own OMP namespace). The command log is the test's own bytes in
    the backend's legacy terminal-log place; ``task_status`` is asked of the real backend too, but the linked Task's
    command list is the test's (no worker runs a command here)."""

    def setUp(self):
        from test_project_control import ProjectSandbox
        from workbench.hub.inbox.service import InboxService, ProjectRef
        self.box = ProjectSandbox(self)
        self.root, self.project_id = self.box.project('alpha')
        self.box.power_on(self.root, self.project_id)
        self.inbox = InboxService()
        self.addCleanup(self.inbox.close)
        self.inbox.set_projects([ProjectRef(self.project_id, self.root, 'alpha')])
        self.rid = self.inbox.submit(Principal.owner(), self.project_id, 'Train', 'Run it')['request_id']
        manager = Principal.manager(self.project_id)
        self.inbox.review(manager, self.rid, 'begin_review')
        token = self.inbox.link_check(manager, self.rid)['link_token']
        self.inbox.link_commit(manager, token, TASK, 1, 'running')

    def place_command_log(self, command_id: str, data: bytes) -> None:
        directory = self.root / '.wb' / 'workflow' / 'terminal'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in (self.root / '.wb' / 'workflow', directory):
            os.chmod(path, 0o700)
        descriptor = os.open(directory / f'{command_id}.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)

    def test_a_command_log_page_comes_from_the_backends_raw_query(self):
        from workbench.hub.inbox.limits import InboxError
        command_id = str(uuid.uuid4())
        data = b'python train.py --epochs 3\n' + 'epoch 1 loss 0.5 결과\n'.encode() * 8000
        self.place_command_log(command_id, data)
        real = results.BackendResults()
        task = {'task_id': TASK, 'exists': True, 'status': 'finished', 'runs': [],
                'commands': [{'command_id': command_id, 'owner': 'worker', 'exit_code': 0}]}
        case = self

        class Port:
            @staticmethod
            def task_status(root, task_ids, *, detail=False):
                case.assertEqual(real.task_status(root, task_ids, detail=detail),
                                 [{'task_id': TASK, 'exists': False}], 'the real backend has no such Task')
                return [task]
            raw_query = staticmethod(real.raw_query)
        reader = results.Results(self.inbox, port=Port())
        page = reader.raw(Principal.owner(), self.rid, command_id, 0, 131072)
        self.assertEqual((page['source_kind'], page['size'], page['offset'], page['next_offset']),
                         ('command', len(data), 0, 131072))
        self.assertEqual(_bytes(page), data[:131072])
        tail = reader.raw(Principal.owner(), self.rid, command_id, 131072, 131072)
        self.assertEqual((_bytes(tail), tail['next_offset']), (data[131072:262144], None if len(data) <= 262144
                                                                else 262144))
        with self.assertRaises(InboxError) as caught:
            reader.raw(Principal.owner(), self.rid, command_id, len(data) + 1, 10)
        self.assertEqual(caught.exception.code, 'invalid_input')

    def test_a_task_the_real_backend_does_not_know_has_no_raw_source(self):
        from workbench.hub.inbox.limits import InboxError
        reader = results.Results(self.inbox)
        with self.assertRaises(InboxError) as caught:
            reader.raw(Principal.owner(), self.rid, RUN, 0, 10)
        self.assertEqual(caught.exception.code, 'not_found')
        full = reader.full(Principal.owner(), self.rid)['full']
        self.assertEqual((full['available'], full['reason']), (False, 'task_not_found'))
