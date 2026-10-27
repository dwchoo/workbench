"""TI-14 independent verification: request results, raw pages and artifact confinement.

Expected outcomes come from the Task Inbox SPEC ("결과 조회", "입력 한도"), BRIEF D-22/D-42 and P2-AC-13/14/28/31,
TI-14 "독립 초점" and the Root decision "artifact scope = project root minus .wb/.git, no ``..``, no symlink anywhere
in the path, no hardlinked file, no FIFO/device/socket" - not from the worker's tests. The backend is this file's own
fake (``task_status``/``raw_query`` in the ui_v1 shapes of the SPEC "Hub -> backend"); files, symlinks, hard links,
FIFOs and sockets are the test's own. Real loopback Hub (``RestHub``, ``/api/v1/``) / real ``create_app`` + stdlib clients. Temp HOME
and XDG dirs, a short private ``XDG_RUNTIME_DIR``, ``WORKBENCH_HUB_AUTOSTART=0``; no real backend is started.
Run with ``discover -s tests/hub -p 'test_inbox_results*.py'``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support.rest_client import RestClient  # noqa: E402
from support.rest_hub import RestHub  # noqa: E402
import test_web_pages_requests as pages  # noqa: E402  (module import: its test classes stay uncollected here)
from workbench.hub import inbox_api, rest  # noqa: E402
from workbench.hub.inbox import results  # noqa: E402
from workbench.hub.inbox.backend_port import BackendPortError  # noqa: E402
from workbench.hub.inbox.limits import InboxError  # noqa: E402
from workbench.hub.inbox.service import Principal  # noqa: E402
from workbench.hub.web.pages import requests as request_pages  # noqa: E402

_SANDBOX: dict = {}


def setUpModule():
    base = Path(tempfile.mkdtemp(prefix='wbr-'))
    runtime = Path(tempfile.mkdtemp(prefix='x', dir=base))
    os.chmod(runtime, 0o700)
    env = {'HOME': str(base / 'home'), 'XDG_CONFIG_HOME': str(base / 'cfg'), 'XDG_STATE_HOME': str(base / 'state'),
           'XDG_DATA_HOME': str(base / 'data'), 'XDG_CACHE_HOME': str(base / 'cache'),
           'XDG_RUNTIME_DIR': str(runtime), 'WORKBENCH_HUB_AUTOSTART': '0'}
    for name, value in env.items():
        _SANDBOX[name] = os.environ.get(name)
        os.environ[name] = value
    for name in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME'):
        Path(env[name]).mkdir(parents=True, exist_ok=True)
    (Path(env['HOME']) / '.ssh').mkdir()
    (Path(env['HOME']) / '.ssh' / 'id_rsa').write_text('HOME-SSH-SECRET')
    _SANDBOX['_base'] = base


def tearDownModule():
    base = _SANDBOX.pop('_base', None)
    for name, value in _SANDBOX.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    _SANDBOX.clear()
    if base is not None:
        shutil.rmtree(base, ignore_errors=True)


TASK, OTHER_TASK, FOREIGN_TASK = (str(uuid.uuid4()) for _ in range(3))
RUN, OTHER_RUN, FOREIGN_RUN = (str(uuid.uuid4()) for _ in range(3))
COMMAND, OTHER_COMMAND, FOREIGN_COMMAND = (str(uuid.uuid4()) for _ in range(3))
REPORT = 'REPORT-TEXT-93: accuracy 0.93'
VIEW_LOG = 'VIEW-RAW-LOG-SECRET'
OUTSIDE = 'OUTSIDE-FILE-SECRET'

# 100,003 bytes of seeded pseudo-random data (mostly not UTF-8), the run log: pages cross the backend's 32 KiB reads.
_RNG = random.Random(1414)
RUN_LOG = bytes(_RNG.randrange(256) for _ in range(100003))
TEXT_LOG = b''.join(('line %05d 완료\n' % n).encode() for n in range(6000))  # ~ 100 KB UTF-8 text
COMMAND_LOG = b'$ python train.py\nepoch 1\n'
OTHER_LOG = b'OTHER-REQUEST-RUN-LOG-SECRET'
FOREIGN_LOG = b'OTHER-PROJECT-RUN-LOG-SECRET'
OTHER_COMMAND_LOG = b'OTHER-REQUEST-COMMAND-LOG-SECRET'


def _task(task_id, run_id, command_id, report=REPORT):
    return {'task_id': task_id, 'exists': True, 'status': 'finished', 'closed_reason': None, 'revision': 1,
            'cancel_requested_by': None,
            'runs': [{'run_id': run_id, 'status': 'finished', 'exit_code': 0, 'started_at': 1.0, 'ended_at': 2.0,
                      'stopped_by': None, 'log_sources': [{'source_kind': 'run', 'source_id': run_id}]}],
            'commands': [{'command_id': command_id, 'owner': 'worker', 'exit_code': 0}],
            'reports': [{'message_id': 'm-1', 'kind': 'result', 'at': 3.0, 'text': report}]}


class Backend:
    """The backend side (``task_status``/``raw_query``): it serves any id of the project like the real exact-id read,
    so only the Hub's own linkage check can keep a request away from another Task's log."""

    def __init__(self):
        self.down: str | Exception | None = None
        self.tasks = {TASK: _task(TASK, RUN, COMMAND), OTHER_TASK: _task(OTHER_TASK, OTHER_RUN, OTHER_COMMAND,
                                                                         'OTHER-REQUEST-REPORT-SECRET'),
                      FOREIGN_TASK: _task(FOREIGN_TASK, FOREIGN_RUN, FOREIGN_COMMAND, 'FOREIGN-REPORT-SECRET')}
        self.logs = {('run', RUN): RUN_LOG, ('command', COMMAND): COMMAND_LOG, ('run', OTHER_RUN): OTHER_LOG,
                     ('command', OTHER_COMMAND): OTHER_COMMAND_LOG, ('run', FOREIGN_RUN): FOREIGN_LOG}
        self.calls: list[tuple] = []

    def _fail(self):
        if isinstance(self.down, Exception):
            raise self.down
        if self.down:
            raise BackendPortError(self.down)

    def task_status(self, root, task_ids, *, detail=False, **_):
        self.calls.append(('task_status', Path(root), tuple(task_ids), detail))
        self._fail()
        items = []
        for task_id in task_ids:
            item = json.loads(json.dumps(self.tasks.get(task_id, {'task_id': task_id, 'exists': False})))
            if not detail:
                item.pop('reports', None)
            items.append(item)
        return items

    def raw_query(self, root, kind, source_id, offset, limit, **_):
        self.calls.append(('raw_query', Path(root), kind, source_id, offset, limit))
        self._fail()
        data = self.logs[(kind, source_id)]
        chunk = data[offset:offset + limit]
        return {'status': 'available', 'kind': kind, 'id': source_id, 'log_format': 'legacy-file',
                'observed_bytes': len(data),
                'read': {'start': offset, 'end': offset + len(chunk),
                         'dataBase64': base64.b64encode(chunk).decode('ascii'), 'missing': []},
                'next_offset': offset + len(chunk) if offset + len(chunk) < len(data) else None}

    def raw_calls(self):
        return [call for call in self.calls if call[0] == 'raw_query']


def page_bytes(page: dict) -> bytes:
    return page['text'].encode('utf-8') if 'text' in page else base64.b64decode(page['data_base64'])


class Tree:
    """The artifact attack surface under a project ``root``; ``base`` is the temp dir that holds the root."""

    def __init__(self, root: Path, base: Path, other_root: Path):
        self.root, self.base, self.other_root = root, base, other_root
        self.outside = base / 'outside.txt'
        self.outside.write_text(OUTSIDE)
        self.outside_single = base / 'outside-single.txt'  # one link: only the path rules can keep it out
        self.outside_single.write_text(OUTSIDE + ' single')
        (base / 'outside-dir').mkdir()
        (base / 'outside-dir' / 'secret.txt').write_text(OUTSIDE + ' in a dir')
        (base / 'outside-dir' / 'note.txt').write_text(OUTSIDE + ' note')
        (root / 'inside.txt').write_text('INSIDE-PUBLIC')
        (root / 'sub').mkdir()
        (root / 'sub' / 'note.txt').write_text('SUB-NOTE')
        (root / '.git').mkdir()
        (root / '.git' / 'config').write_text('GIT-CONFIG-SECRET')
        (root / '.git' / 'HEAD').write_text('GIT-HEAD-SECRET')
        (root / '.WB').mkdir()
        (root / '.WB' / 'x').write_text('UPPER-WB-SECRET')
        (root / '.Git').mkdir()
        (root / '.Git' / 'config').write_text('MIXED-GIT-SECRET')
        (root / '.wb' / 'agent.db').write_text('WB-AGENT-DB-SECRET')
        (root / 'link-out').symlink_to(self.outside)
        (root / 'link-in').symlink_to(root / 'inside.txt')
        (root / 'dlink').symlink_to(base / 'outside-dir')
        (root / 'passwd-link').symlink_to('/etc/passwd')
        (root / 'null-link').symlink_to('/dev/null')
        (root / 'zero-link').symlink_to('/dev/zero')
        (root / 'rel-link').symlink_to('../outside.txt')
        (root / 'link-single').symlink_to(self.outside_single)
        os.link(self.outside, root / 'hard-out')
        os.link(root / '.wb' / 'inbox.sqlite3', root / 'hard-db')
        (root / 'twin-a').write_text('TWIN')
        os.link(root / 'twin-a', root / 'twin-b')
        os.mkfifo(root / 'pipe')
        (root / 'pipe-link').symlink_to(root / 'pipe')
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(root / 'sock'))
        (root / '.gitignore').write_text('IGNORED\n')
        (root / '.github').mkdir()
        (root / '.github' / 'ci.yml').write_text('CI')
        (root / '.wbx').write_text('WBX')
        (root / 'wb').write_text('WB-PLAIN')

    def close(self):
        self.sock.close()

    SECRETS = (OUTSIDE, 'GIT-CONFIG-SECRET', 'GIT-HEAD-SECRET', 'UPPER-WB-SECRET', 'MIXED-GIT-SECRET',
               'WB-AGENT-DB-SECRET', 'SQLite format', 'HOME-SSH-SECRET', 'root:x:0:0', 'nobody')

    def attacks(self) -> dict[str, str]:
        root, other = self.root, self.other_root
        return {
            'parent dir': '../outside-single.txt', 'nested parent': 'sub/../../outside-single.txt', 'dotdot': '..',
            'sub dotdot': 'sub/..', 'dotdot inside file': 'inside.txt/..',
            'absolute outside': str(self.outside), 'etc passwd': '/etc/passwd', 'double slash': '//etc//passwd',
            'proc environ': '/proc/self/environ', 'proc root': '/proc/self/root' + str(self.outside),
            'absolute + dotdot': str(root) + '/../outside-single.txt', 'other project file': str(other / '.wb' / 'project.json'),
            'other project via dotdot': '../beta/.wb/project.json',
            'tilde': '~/.ssh/id_rsa', 'tilde user': '~root/.bashrc', 'env var': '$HOME/.ssh/id_rsa',
            'url-encoded dotdot': '%2e%2e/outside.txt', 'backslash dotdot': '..\\outside.txt',
            'symlink file out': 'link-out', 'symlink file in': 'link-in', 'symlink relative out': 'rel-link', 'symlink to single-link file': 'link-single',
            'symlink dir mid-path': 'dlink/secret.txt', 'symlink dir then dotdot': 'dlink/../inside.txt',
            'symlink to passwd': 'passwd-link', 'symlink to /dev/null': 'null-link',
            'symlink to /dev/zero': 'zero-link', 'symlink absolute spelling': str(root / 'link-out'),
            'hardlink to outside': 'hard-out', 'hardlink to inbox db': 'hard-db', 'hardlink pair a': 'twin-a',
            'hardlink pair b': 'twin-b', 'fifo': 'pipe', 'symlink to fifo': 'pipe-link', 'socket': 'sock',
            'inbox db': '.wb/inbox.sqlite3', 'inbox db wal': '.wb/inbox.sqlite3-wal', 'project json': '.wb/project.json',
            'wb dir': '.wb', 'wb dir slash': '.wb/', 'wb agent db': '.wb/agent.db',
            'git config': '.git/config', 'git head': '.git/HEAD', 'git dir': '.git', 'git dir slash': '.git/',
            'wb upper': '.WB/x', 'wb mixed': '.wB/inbox.sqlite3', 'git upper': '.GIT/config', 'git mixed': '.Git/config',
            'wb dot slash': './.wb/inbox.sqlite3', 'wb inner dot': '.wb/./inbox.sqlite3',
            'wb through dotdot': 'sub/./../.wb/inbox.sqlite3', 'wb absolute': str(root / '.wb' / 'inbox.sqlite3'),
            'wb absolute upper': str(root / '.WB' / 'x'), 'nested git': 'sub/.git/config',
            'fullwidth dot': '．wb/inbox.sqlite3', 'fullwidth letter': '.ｗb/inbox.sqlite3',
            'zero-width': '.wb​/inbox.sqlite3', 'dotless i git': '.gıt/config',
            'nul in name': 'inside.txt\x00.png', 'nul only': 'a\x00b',
            'long path': 'a' * 5000, 'long dirs': 'd/' * 2100 + 'x', 'long name': 'x' * 300,
            'trailing slash symlink': 'link-out/', 'trailing slash dlink': 'dlink/',
            'directory': 'sub', 'directory slash': 'sub/', 'directory dot': 'sub/.', 'dot': '.',
            'root absolute': str(root), 'empty': '', 'slash only': '/', 'whitespace': '   ',
            'lone surrogate': 'a\ud800b', 'escaped surrogate': 'a\udcffb',
        }


class Unblock:
    """A FIFO open that blocks is released after 4 s (a writer opens and closes it, repeatedly), so a defect shows as
    a slow call, never as a hung test run."""

    def __init__(self, fifo: Path):
        self.fifo, self.stop = fifo, threading.Event()

    def __enter__(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _run(self):
        if self.stop.wait(4.0):
            return
        while not self.stop.is_set():
            try:
                os.close(os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
            self.stop.wait(0.2)

    def __exit__(self, *exc):
        self.stop.set()


# SPEC "오류 → HTTP status": the status every error code must come with (``RestTools.tool_error`` checks it).
ERROR_STATUS = {'invalid_input': 400, 'unauthorized': 401, 'forbidden': 403, 'origin_mismatch': 403, 'not_found': 404,
                'conflict': 409, 'invalid_transition': 409, 'idempotency_conflict': 409, 'token_mismatch': 409,
                'backend_not_running': 409, 'too_large': 413, 'internal_error': 500, 'project_unavailable': 503,
                'inbox_busy': 503, 'inbox_mismatch': 503, 'backend_unreachable': 503, 'hub_stopping': 503,
                'busy': 503}
# SPEC endpoint table: operation -> (method, path); ``request_id`` is the path's id, the other arguments are the
# query (GET) or the JSON body, ``states`` is the repeated ``state`` query parameter.
ROUTES = {'inbox_projects': ('GET', '/projects'), 'inbox_submit': ('POST', '/requests'),
          'inbox_list': ('GET', '/requests'), 'inbox_get': ('GET', '/requests/{rid}'),
          'inbox_comment': ('POST', '/requests/{rid}/comments'), 'inbox_update': ('PATCH', '/requests/{rid}'),
          'inbox_set_state': ('POST', '/requests/{rid}/state'), 'inbox_cancel': ('POST', '/requests/{rid}/cancel'),
          'inbox_result': ('GET', '/requests/{rid}/result')}


class RestTools:
    """One key's REST calls, named by the operation (what the MCP tests called): ``call`` is the reply, ``tool`` the
    body of a 2xx reply, ``tool_error`` the ``error`` object of a refusal whose status is its code's SPEC status."""

    def __init__(self, client):
        self.client = client

    def call(self, name, **arguments):
        method, path = ROUTES[name]
        if 'request_id' in arguments:
            path = path.format(rid=quote(arguments.pop('request_id'), safe=''))
        if method == 'GET':
            if 'states' in arguments:
                arguments['state'] = arguments.pop('states')
            # Attack paths are sent as they are: '/' stays literal (a path of 2100 directories must fit the request
            # line) and a lone surrogate becomes its raw (invalid UTF-8) bytes, as the web tests do.
            parts = []
            for name, value in arguments.items():
                for item in value if isinstance(value, list) else [value]:
                    text = ('true' if item else 'false') if isinstance(item, bool) else str(item)
                    parts.append(f'{name}={quote(text, safe="/", errors="surrogatepass")}')
            return self.client.raw('GET', rest.PREFIX + path + ('?' + '&'.join(parts) if parts else ''))
        return self.client.call(method, path, arguments)

    def tool(self, name, **arguments):
        reply = self.call(name, **arguments)
        if not 200 <= reply.status < 300:
            raise AssertionError(f'{name} failed: {reply.status} {reply.body!r}')
        return reply.json

    def tool_error(self, name, **arguments):
        reply = self.call(name, **arguments)
        if reply.status < 400:
            raise AssertionError(f'{name} unexpectedly succeeded: {reply.status} {reply.body!r}')
        error = reply.error
        if reply.status != ERROR_STATUS[error['code']]:
            raise AssertionError(f'{name}: {error["code"]} came with HTTP {reply.status}')
        return error


class RestCase(unittest.TestCase):
    def setUp(self):
        self.hub = RestHub(self)
        self.backend = Backend()
        self.reader = results.Results(self.hub.inbox, port=self.backend)
        self.hub.api.results = self.reader
        self.keys = {name: self.hub.key(name, grade, scope) for name, grade, scope in (
            ('r1', 'requester', {'projects': [self.hub.a]}), ('r2', 'requester', {'projects': [self.hub.a]}),
            ('rb', 'requester', {'projects': [self.hub.b]}), ('ed-a', 'editor', {'projects': [self.hub.a]}),
            ('ed-b', 'editor', {'projects': [self.hub.b]}), ('ed-all', 'editor', {'all': True}))}
        self.c = {name: RestTools(self.hub.rest(key, timeout=10)) for name, key in self.keys.items()}
        self.rid = self.submit('r1', self.hub.a, 'one')
        self.rid2 = self.submit('r2', self.hub.a, 'two')
        self.rid_b = self.submit('rb', self.hub.b, 'three')
        self.hub.link_running(self.rid, task_id=TASK, view={'status': 'running', 'runs': [
            {'run_id': RUN, 'status': 'running', 'exit_code': None, 'log': VIEW_LOG, 'report': REPORT}]})
        self.link_second()
        self.hub.link_running(self.rid_b, self.hub.b, task_id=FOREIGN_TASK)

    def link_second(self):
        self.hub.link_running(self.rid2, task_id=OTHER_TASK)

    def submit(self, client, project, title):
        return self.c[client].tool('inbox_submit', project_id=project, title=title, body='b',
                                   idempotency_key=f'{client}-{title}')['request_id']

    def result(self, client='r1', rid=None, **arguments):
        return self.c[client].tool('inbox_result', request_id=rid or self.rid, **arguments)

    def error(self, client='r1', rid=None, **arguments):
        return self.c[client].tool_error('inbox_result', request_id=rid or self.rid, **arguments)

    def raw_bytes(self, run_id=RUN, **arguments) -> bytes:
        return page_bytes(self.result(full=True, run_id=run_id, **arguments)['raw'])


class SummaryDefaultTests(RestCase):
    def test_default_summary_has_no_raw_report_command_or_artifact_content(self):
        (self.hub.root_a / 'out.txt').write_text('ARTIFACT-CONTENT')
        for arguments in ({}, {'full': False}, {'full': False, 'run_id': RUN}, {'run_id': RUN, 'offset': 0, 'length': 5},
                          {'artifact_path': 'out.txt'}, {'run_id': COMMAND, 'artifact_path': 'out.txt'}):
            with self.subTest(arguments=arguments):
                value = self.result(**arguments)
                # requester-authored text: the summary may carry the untrusted marking (and nothing else new)
                self.assertEqual(set(value) - {'untrusted_text', 'note'}, {'summary', 'state', 'outcome', 'task'})
                self.assertLessEqual(set(value) & {'untrusted_text', 'note'}, {'untrusted_text', 'note'})
                text = json.dumps(value)
                for secret in (VIEW_LOG, REPORT, 'ARTIFACT-CONTENT', 'epoch 1', 'accuracy'):
                    self.assertNotIn(secret, text)
        self.assertEqual(self.backend.calls, [], 'the summary never asks the backend')

    def test_only_a_real_true_turns_the_full_view_on(self):
        # REST (SPEC "Query"): a boolean query value is exactly ``true`` or ``false``; every other spelling is a
        # 400 ``invalid_input`` (over MCP the JSON types ``1``, ``0``, ``null`` ... were the "not a real true").
        for flag in ('TRUE', 'True', '1', 'yes', 'full', '0', '', 'false ', ' true', 'tru'):
            with self.subTest(full=flag):
                call = self.c['r1'].call('inbox_result', request_id=self.rid, full=flag, run_id=RUN)
                self.assertEqual(call.status, 400, call.body)
                self.assertEqual(call.error['code'], 'invalid_input')
                text = call.body.decode()
                self.assertNotIn(REPORT, text)
                self.assertNotIn('"raw"', text)
                self.assertNotIn('"full"', text)
        call = self.c['r1'].call('inbox_result', request_id=self.rid, full=False, run_id=RUN)
        self.assertEqual(call.status, 200, call.body)
        text = call.body.decode()
        self.assertNotIn(REPORT, text)
        self.assertNotIn('"raw"', text)
        self.assertNotIn('"full"', text)
        self.assertFalse(self.backend.raw_calls())

    def test_closed_request_summary_is_the_managers_text_only(self):
        self.hub.inbox.review(Principal.manager(self.hub.a), self.rid, 'close', outcome='success', summary='Done 0.93')
        value = self.result()
        self.assertEqual((value['summary'], value['state'], value['outcome']), ('Done 0.93', 'CLOSED', 'success'))
        self.assertNotIn(VIEW_LOG, json.dumps(value))


class FullViewTests(RestCase):
    def test_full_view_shows_reports_commands_and_run_sources_but_no_log_bytes_or_host_paths(self):
        value = self.result(full=True)
        task = value['full']['task']
        self.assertEqual([run['run_id'] for run in task['runs']], [RUN])
        self.assertEqual([c['command_id'] for c in task['commands']], [COMMAND])
        self.assertEqual([r['text'] for r in task['reports']], [REPORT])
        text = json.dumps(value)
        self.assertNotIn('epoch 1', text)
        self.assertNotIn(base64.b64encode(COMMAND_LOG[:12]).decode(), text)
        self.assertNotIn(str(self.hub.root_a), text)
        self.assertNotIn(OTHER_TASK, text)
        self.assertNotIn('OTHER-REQUEST-REPORT-SECRET', text)

    def test_the_full_view_asks_for_this_requests_task_only(self):
        self.result(full=True)
        self.assertEqual(self.backend.calls, [('task_status', self.hub.root_a, (TASK,), True)])


class RawLinkageTests(RestCase):
    def test_runs_and_commands_of_other_tasks_are_not_found_and_never_read(self):
        foreign = [OTHER_RUN, OTHER_COMMAND, FOREIGN_RUN, FOREIGN_COMMAND, str(uuid.uuid4()), TASK, self.rid,
                   RUN.upper(), '{' + RUN + '}', RUN.replace('-', ''), 'urn:uuid:' + RUN, RUN + ' ', ' ' + RUN,
                   '../' + RUN, RUN + '/../' + OTHER_RUN, '', '0', '../../etc/passwd', 'a\x00b']
        for run_id in foreign:
            with self.subTest(run_id=run_id):
                self.assertEqual(self.error(full=True, run_id=run_id)['code'], 'not_found')
        self.assertEqual(self.backend.raw_calls(), [], 'no raw_query reaches the backend for a foreign id')

    def test_each_request_reaches_its_own_sources_only(self):
        self.assertEqual(self.raw_bytes(RUN, offset=0, length=10), RUN_LOG[:10])
        self.assertEqual(self.raw_bytes(COMMAND), COMMAND_LOG)
        self.assertEqual(page_bytes(self.result('r2', self.rid2, full=True, run_id=OTHER_RUN)['raw']), OTHER_LOG)
        self.assertEqual(self.error('r2', self.rid2, full=True, run_id=RUN)['code'], 'not_found')
        self.assertEqual(self.error('r1', self.rid, full=True, run_id=OTHER_COMMAND)['code'], 'not_found')
        # the other project's request links its own Task; its sources are not reachable from alpha's request either way
        self.assertEqual(self.error('ed-all', self.rid, full=True, run_id=FOREIGN_RUN)['code'], 'not_found')
        self.assertEqual(self.error('ed-all', self.rid_b, full=True, run_id=RUN)['code'], 'not_found')
        for call in self.backend.raw_calls():
            self.assertIn(call[1:4], ((self.hub.root_a, 'run', RUN), (self.hub.root_a, 'command', COMMAND),
                                      (self.hub.root_a, 'run', OTHER_RUN)))

    def test_a_request_without_a_task_has_no_raw_source(self):
        fresh = self.submit('r1', self.hub.a, 'untouched')
        for run_id in (RUN, OTHER_RUN, str(uuid.uuid4())):
            self.assertEqual(self.error(rid=fresh, full=True, run_id=run_id)['code'], 'not_found')
        self.assertEqual(self.backend.calls, [])

    def test_a_task_the_backend_does_not_list_or_confuses_is_not_a_source(self):
        del self.backend.tasks[TASK]
        self.assertEqual(self.error(full=True, run_id=RUN)['code'], 'not_found')
        self.backend.tasks[TASK] = {**_task(OTHER_TASK, OTHER_RUN, OTHER_COMMAND)}  # an answer for a different Task id
        self.assertEqual(self.error(full=True, run_id=OTHER_RUN)['code'], 'not_found')
        self.assertEqual(self.backend.raw_calls(), [])


class RawPageTests(RestCase):
    def test_pages_across_backend_read_boundaries_are_the_exact_bytes(self):
        step = 32 * 1024
        for offset, length in ((0, 1), (step - 1, 2), (step - 10, 20), (step, 1), (step + 1, step), (2 * step - 3, 6),
                               (0, 131072), (5, 100000), (65535, 34468), (len(RUN_LOG) - 1, 50), (len(RUN_LOG) - 7, 7),
                               (99999, 5000)):
            with self.subTest(offset=offset, length=length):
                page = self.result(full=True, run_id=RUN, offset=offset, length=length)['raw']
                expected = RUN_LOG[offset:offset + length]
                self.assertEqual(page_bytes(page), expected)
                self.assertEqual((page['offset'], page['length'], page['size']), (offset, len(expected), len(RUN_LOG)))
                end = offset + len(expected)
                self.assertEqual(page['next_offset'], end if end < len(RUN_LOG) else None)
        for call in self.backend.raw_calls():
            self.assertTrue(1 <= call[5] <= step and call[4] >= 0, call)

    def test_following_next_offset_reassembles_the_whole_log(self):
        for run_id, log, size in ((RUN, RUN_LOG, 7001), (COMMAND, COMMAND_LOG, 5)):
            got, offset, pages = b'', 0, 0
            while offset is not None:
                page = self.result(full=True, run_id=run_id, offset=offset, length=size)['raw']
                got += page_bytes(page)
                offset = page['next_offset']
                pages += 1
                self.assertLess(pages, 100)
            self.assertEqual(got, log)

    def test_utf8_text_comes_as_text_and_a_split_character_as_bytes(self):
        self.backend.logs[('run', RUN)] = TEXT_LOG
        whole = self.result(full=True, run_id=RUN, offset=0, length=len(TEXT_LOG))['raw']
        self.assertEqual(whole['text'].encode('utf-8'), TEXT_LOG)
        cut = TEXT_LOG.index('완'.encode()) + 1  # inside the 3-byte character
        left = self.result(full=True, run_id=RUN, offset=0, length=cut)['raw']
        right = self.result(full=True, run_id=RUN, offset=cut, length=20)['raw']
        self.assertEqual(page_bytes(left), TEXT_LOG[:cut])
        self.assertEqual(page_bytes(right), TEXT_LOG[cut:cut + 20])
        self.assertEqual(page_bytes(left) + page_bytes(self.result(full=True, run_id=RUN, offset=cut,
                                                                    length=len(TEXT_LOG) - cut)['raw']), TEXT_LOG)

    def test_offset_and_length_bounds(self):
        end = len(RUN_LOG)
        empty = self.result(full=True, run_id=RUN, offset=end, length=10)['raw']
        self.assertEqual((page_bytes(empty), empty['next_offset'], empty['length']), (b'', None, 0))
        for offset in (end + 1, end + 10 ** 6, 10 ** 30):
            with self.subTest(offset=offset):
                self.assertEqual(self.error(full=True, run_id=RUN, offset=offset, length=1)['code'], 'invalid_input')
        for arguments in ({'length': 0}, {'length': -1}, {'length': 131073}, {'length': 2 ** 40}, {'offset': -1},
                          {'length': True}, {'offset': False}, {'length': 1.5}, {'offset': 'x'}, {'length': '5x'},
                          {'length': ''}, {'offset': '+1'}, {'length': ' 5'}):
            # REST (SPEC "Query"): an integer is an unsigned decimal, so ``-1``, ``1.5``, ``true``, ``+1`` ... are
            # 400 ``invalid_input`` (over MCP the JSON types ``true``/``1.5``/``"5"`` were the wrong-type cases).
            with self.subTest(arguments=arguments):
                self.assertEqual(self.error(full=True, run_id=RUN, **arguments)['code'], 'invalid_input')
        self.assertEqual(self.raw_bytes(RUN, offset=0, length=131072), RUN_LOG, 'one page covers a 100 KB log')
        self.assertEqual(self.raw_bytes(RUN), RUN_LOG, 'the default page is 131072 bytes from the start')


class BackendOffTests(RestCase):
    def test_raw_is_backend_not_running_and_nothing_cached_leaks(self):
        self.assertEqual(self.raw_bytes(RUN, offset=0, length=50), RUN_LOG[:50])
        self.assertIn(REPORT, json.dumps(self.result(full=True)))
        self.backend.down = 'backend_not_running'
        call = self.c['r1'].call('inbox_result', request_id=self.rid, full=True, run_id=RUN)
        self.assertEqual((call.status, call.error['code']), (409, 'backend_not_running'))
        text = call.body.decode()
        self.assertNotIn(base64.b64encode(RUN_LOG[:8]).decode(), text)
        full = self.result(full=True)
        text = json.dumps(full)
        for secret in (REPORT, VIEW_LOG, 'epoch', COMMAND, base64.b64encode(RUN_LOG[:8]).decode()):
            self.assertNotIn(secret, text)
        self.assertEqual(full['full']['available'], False)
        self.assertEqual(full['full']['reason'], 'backend_not_running')
        self.assertEqual(self.error(full=True, run_id=COMMAND)['code'], 'backend_not_running')

    def test_backend_error_text_never_reaches_the_client(self):
        for failure in (BackendPortError('unknown', 'Traceback /home/secret/.wb/x.log'),
                        RuntimeError('/home/secret/.wb/x.log'), OSError(13, 'denied', '/home/secret/.wb/x.log')):
            self.backend.down = failure
            with self.subTest(failure=type(failure).__name__):
                error = self.error(full=True, run_id=RUN)
                self.assertEqual(error['code'], 'backend_unreachable')
                self.assertNotIn('/home/secret', json.dumps(error))
                full = self.result(full=True)['full']
                self.assertEqual((full['available'], full['reason']), (False, 'backend_unreachable'))
                self.assertNotIn('/home/secret', json.dumps(full))

    def test_artifacts_do_not_need_the_backend_but_the_rules_still_hold(self):
        tree = Tree(self.hub.root_a, self.hub.base, self.hub.root_b)
        self.addCleanup(tree.close)
        self.backend.down = 'backend_not_running'
        self.assertEqual(page_bytes(self.result(full=True, artifact_path='inside.txt')['artifact']), b'INSIDE-PUBLIC')
        self.assertEqual(self.error(full=True, artifact_path='link-out')['code'], 'forbidden')
        self.assertEqual(self.backend.calls, [])


class ArtifactAttackTests(RestCase):
    REJECT = {'forbidden', 'not_found', 'invalid_input'}

    def setUp(self):
        super().setUp()
        self.tree = Tree(self.hub.root_a, self.hub.base, self.hub.root_b)
        self.addCleanup(self.tree.close)

    def test_there_are_at_least_25_attack_variants(self):
        self.assertGreaterEqual(len(self.tree.attacks()), 25)

    def test_every_attack_is_refused_without_a_leak_and_without_a_server_error(self):
        base = str(self.hub.base)
        for label, path in self.tree.attacks().items():
            with self.subTest(label=label):
                started = time.monotonic()
                with Unblock(self.hub.root_a / 'pipe'):
                    call = self.c['r1'].call('inbox_result', request_id=self.rid, full=True, artifact_path=path)
                self.assertLess(time.monotonic() - started, 3.0, 'refused promptly (no FIFO/device blocking)')
                self.assertGreaterEqual(call.status, 400, call.body)
                text = call.body.decode()
                code = call.error['code']
                self.assertIn(code, self.REJECT, text)
                self.assertEqual(call.status, ERROR_STATUS[code], text)
                for secret in Tree.SECRETS:
                    self.assertNotIn(secret, text)
                self.assertNotIn(base, text, 'no host path in the error')
                self.assertNotIn('Traceback', text)

    def test_security_critical_cases_are_forbidden_not_just_missing(self):
        for label in ('parent dir', 'absolute outside', 'etc passwd', 'symlink file out', 'symlink dir mid-path',
                      'symlink to passwd', 'hardlink to outside', 'hardlink to inbox db', 'inbox db', 'git config',
                      'wb upper', 'git mixed', 'wb dot slash', 'symlink to /dev/null', 'symlink to fifo'):
            path = self.tree.attacks()[label]
            with self.subTest(label=label):
                self.assertEqual(self.error(full=True, artifact_path=path)['code'], 'forbidden')

    def test_fifo_returns_quickly_and_forbidden_or_invalid(self):
        for path in ('pipe', 'pipe-link', './pipe', str(self.hub.root_a / 'pipe')):
            with self.subTest(path=path):
                started = time.monotonic()
                with Unblock(self.hub.root_a / 'pipe'):
                    error = self.error(full=True, artifact_path=path)
                self.assertLess(time.monotonic() - started, 3.0)
                self.assertIn(error['code'], {'forbidden', 'invalid_input'})

    def test_legitimate_files_are_not_over_blocked(self):
        (self.hub.root_a / 'sub' / 'deep').mkdir()
        (self.hub.root_a / 'sub' / 'deep' / 'f.txt').write_bytes(b'\xff\xfe binary \x00')
        cases = {'inside.txt': b'INSIDE-PUBLIC', './sub/./note.txt': b'SUB-NOTE', 'sub//note.txt': b'SUB-NOTE',
                 str(self.hub.root_a / 'sub' / 'note.txt'): b'SUB-NOTE', '.gitignore': b'IGNORED\n',
                 '.github/ci.yml': b'CI', '.wbx': b'WBX', 'wb': b'WB-PLAIN', 'sub/deep/f.txt': b'\xff\xfe binary \x00'}
        for path, expected in cases.items():
            with self.subTest(path=path):
                page = self.result(full=True, artifact_path=path)['artifact']
                self.assertEqual(page_bytes(page), expected)

    def test_artifact_pages_are_exact_chunks(self):
        data = bytes(_RNG.randrange(256) for _ in range(300001))
        (self.hub.root_a / 'big.bin').write_bytes(data)
        got, offset = b'', 0
        while offset is not None:
            page = self.result(full=True, artifact_path='big.bin', offset=offset, length=131072)['artifact']
            self.assertEqual(page['size'], len(data))
            got += page_bytes(page)
            offset = page['next_offset']
        self.assertEqual(got, data)
        for offset, length in ((131071, 2), (0, 1), (len(data) - 1, 100), (131072, 131072)):
            self.assertEqual(page_bytes(self.result(full=True, artifact_path='big.bin', offset=offset,
                                                    length=length)['artifact']), data[offset:offset + length])
        end = self.result(full=True, artifact_path='big.bin', offset=len(data))['artifact']
        self.assertEqual((page_bytes(end), end['next_offset']), (b'', None))
        for bad in ({'offset': len(data) + 1}, {'offset': 2 ** 70}, {'length': 131073}, {'length': 0}, {'offset': -1}):
            self.assertEqual(self.error(full=True, artifact_path='big.bin', **bad)['code'], 'invalid_input')

    def test_empty_and_non_utf8_files(self):
        (self.hub.root_a / 'empty').write_bytes(b'')
        page = self.result(full=True, artifact_path='empty')['artifact']
        self.assertEqual((page_bytes(page), page['size'], page['next_offset']), (b'', 0, None))
        (self.hub.root_a / 'latin1.txt').write_bytes('caf\xe9'.encode('latin-1'))
        self.assertEqual(page_bytes(self.result(full=True, artifact_path='latin1.txt')['artifact']),
                         'caf\xe9'.encode('latin-1'))

    def test_a_file_replaced_by_a_link_after_a_good_read_is_refused_on_the_next(self):
        self.assertEqual(page_bytes(self.result(full=True, artifact_path='inside.txt')['artifact']), b'INSIDE-PUBLIC')
        os.unlink(self.hub.root_a / 'inside.txt')
        os.symlink(self.tree.outside, self.hub.root_a / 'inside.txt')
        self.assertEqual(self.error(full=True, artifact_path='inside.txt')['code'], 'forbidden')


class ArtifactRaceTests(RestCase):
    """TOCTOU: the target changes after the path judgement and before/while it is opened."""

    def setUp(self):
        super().setUp()
        self.tree = Tree(self.hub.root_a, self.hub.base, self.hub.root_b)
        self.addCleanup(self.tree.close)
        self.owner = Principal.owner()
        self.real_open = results.open_artifact

    def patched(self, before_open):
        def wrapper(root, parts):
            before_open(root, parts)
            return self.real_open(root, parts)
        patch = mock.patch.object(results, 'open_artifact', wrapper)
        patch.start()
        self.addCleanup(patch.stop)

    def read(self, path):
        return self.reader.artifact(self.owner, self.rid, path, 0, 4096)

    def test_the_leaf_swapped_for_a_symlink_between_judgement_and_open_is_refused(self):
        def swap(root, parts):
            os.unlink(self.hub.root_a / 'inside.txt')
            os.symlink(self.tree.outside, self.hub.root_a / 'inside.txt')
        self.patched(swap)
        with self.assertRaises(InboxError) as caught:
            self.read('inside.txt')
        self.assertEqual(caught.exception.code, 'forbidden')
        self.assertNotIn(OUTSIDE, str(caught.exception))

    def test_the_leaf_swapped_for_a_hardlink_to_an_outside_file_is_refused(self):
        def swap(root, parts):
            os.unlink(self.hub.root_a / 'inside.txt')
            os.link(self.tree.outside, self.hub.root_a / 'inside.txt')
        self.patched(swap)
        with self.assertRaises(InboxError) as caught:
            self.read('inside.txt')
        self.assertEqual(caught.exception.code, 'forbidden')

    def test_the_leaf_swapped_for_a_fifo_does_not_block(self):
        def swap(root, parts):
            os.unlink(self.hub.root_a / 'inside.txt')
            os.mkfifo(self.hub.root_a / 'inside.txt')
        self.patched(swap)
        started = time.monotonic()
        with Unblock(self.hub.root_a / 'inside.txt'), self.assertRaises(InboxError) as caught:
            self.read('inside.txt')
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertIn(caught.exception.code, {'forbidden', 'invalid_input', 'not_found'})

    def test_a_parent_directory_swapped_for_a_symlink_is_refused(self):
        def swap(root, parts):
            os.rename(self.hub.root_a / 'sub', self.hub.root_a / 'sub-held')
            os.symlink(self.hub.base / 'outside-dir', self.hub.root_a / 'sub')
        self.patched(swap)
        with self.assertRaises(InboxError) as caught:
            self.read('sub/note.txt')
        self.assertIn(caught.exception.code, {'forbidden', 'not_found'})
        self.assertNotIn(OUTSIDE, str(caught.exception))

    def test_a_file_swapped_for_a_dotwb_hardlink_is_refused(self):
        def swap(root, parts):
            os.unlink(self.hub.root_a / 'inside.txt')
            os.link(self.hub.root_a / '.wb' / 'inbox.sqlite3', self.hub.root_a / 'inside.txt')
        self.patched(swap)
        with self.assertRaises(InboxError) as caught:
            self.read('inside.txt')
        self.assertEqual(caught.exception.code, 'forbidden')

    def flip_and_read(self, flip, path, duration=3.0):
        """(bytes ever read, InboxError codes, other exception types) while ``flip`` runs in a thread for 3 s."""
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                try:
                    flip()
                except OSError:
                    pass
        flipper = threading.Thread(target=loop, daemon=True)
        flipper.start()
        seen, codes, other = set(), set(), set()
        try:
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                try:
                    seen.add(page_bytes(self.read(path)))
                except InboxError as exc:
                    codes.add(exc.code)
                except Exception as exc:  # noqa: BLE001 - the point of the check
                    other.add(f'{type(exc).__name__}: {exc}'.replace(str(self.hub.base), '<base>'))
        finally:
            stop.set()
            flipper.join(5)
        return seen, codes, other

    def file_flipper(self):
        swap, spare = self.hub.root_a / 'swap.txt', self.hub.root_a / 'swap.tmp'
        swap.write_text('SAFE')

        def flip():
            os.symlink(self.tree.outside, spare)
            os.replace(spare, swap)
            spare.write_text('SAFE')
            os.replace(spare, swap)
        return flip

    def directory_flipper(self):
        folder, held = self.hub.root_a / 'rd', self.hub.root_a / 'rd.held'
        folder.mkdir()
        (folder / 'note.txt').write_text('SAFE')

        def flip():
            os.rename(folder, held)
            os.symlink(self.hub.base / 'outside-dir', folder)
            os.unlink(folder)
            os.rename(held, folder)
        return flip

    def test_a_concurrent_file_flipper_never_exposes_the_outside_file(self):
        seen, codes, other = self.flip_and_read(self.file_flipper(), 'swap.txt')
        self.assertLessEqual(seen, {b'SAFE'}, 'only the regular file was ever read')
        self.assertLessEqual(codes, {'forbidden', 'not_found'})
        self.assertTrue(seen or codes or other)

    def test_a_concurrent_directory_flipper_never_exposes_the_outside_directory(self):
        seen, codes, other = self.flip_and_read(self.directory_flipper(), 'rd/note.txt')
        self.assertLessEqual(seen, {b'SAFE'})
        self.assertLessEqual(codes, {'forbidden', 'not_found'})
        self.assertTrue(seen or codes or other)

    def test_a_race_is_refused_with_an_inbox_error_never_an_unexpected_exception(self):
        """A changing file is refused as ``forbidden``/``not_found``; an OSError escaping the judgement would be an
        ``internal_error`` for the client."""
        _, _, other_file = self.flip_and_read(self.file_flipper(), 'swap.txt', 5.0)
        _, _, other_dir = self.flip_and_read(self.directory_flipper(), 'rd/note.txt', 5.0)
        self.assertEqual(other_file | other_dir, set())


class MalformedBackendTests(RestCase):
    """Known gaps from the security review: a malformed or limit-ignoring ``raw_query`` answer must end as a clean
    ``InboxError`` (never ``internal_error``) and must never make a page larger than the one asked for."""

    def serve(self, answer):
        real = self.backend.raw_query

        def raw_query(root, kind, source_id, offset, limit, **_):
            return answer(real, root, kind, source_id, offset, limit)
        self.backend.raw_query = raw_query

    BAD_ANSWERS = {
        'bad base64': lambda real, *a: {**real(*a), 'read': {'dataBase64': '!!not base64!!', 'missing': []}},
        'base64 not a string': lambda real, *a: {**real(*a), 'read': {'dataBase64': 12, 'chunks': 'x'}},
        'chunk without data': lambda real, *a: {'status': 'available', 'observed_bytes': 5, 'next_offset': None,
                                                'read': {'chunks': [{'range': {'start': 0, 'end': 5}}]}},
        'chunk bad base64': lambda real, *a: {'status': 'available', 'observed_bytes': 5, 'next_offset': None,
                                              'read': {'chunks': [{'range': {'start': 0, 'end': 5},
                                                                   'dataBase64': '***'}]}},
        'chunk range not a number': lambda real, *a: {'status': 'available', 'observed_bytes': 5,
                                                      'next_offset': None, 'read': {'chunks': [
                                                          {'range': {'start': 0, 'end': 'x'}, 'dataBase64': ''}]}},
        'range without start': lambda real, *a: {'status': 'available', 'observed_bytes': 5, 'next_offset': None,
                                                 'read': {'chunks': [{'range': {'end': 5}, 'dataBase64': ''}]}},
        'gap without bounds': lambda real, *a: {'status': 'partial', 'observed_bytes': 5, 'next_offset': None,
                                                'read': {'missing': [{'reason': 'rotated'}]}},
        'gap bounds not numbers': lambda real, *a: {'status': 'partial', 'observed_bytes': 5, 'next_offset': None,
                                                    'read': {'missing': [{'start': 'a', 'end': None}]}},
    }

    def test_malformed_raw_query_answers_are_a_clean_error_not_an_internal_error(self):
        for label, answer in self.BAD_ANSWERS.items():
            self.serve(answer)
            with self.subTest(label=label):
                call = self.c['r1'].call('inbox_result', request_id=self.rid, full=True, run_id=RUN, offset=0, length=5)
                text = call.body.decode()
                if call.status >= 400:
                    self.assertIn(call.error['code'], {'backend_unreachable', 'not_found', 'invalid_input'}, text)
                    self.assertEqual(call.status, ERROR_STATUS[call.error['code']], text)
                else:  # an empty/partial page is acceptable only if it carries no data that was not served
                    self.assertLessEqual(len(page_bytes(call.json['raw'])), 5)

    def test_a_backend_that_ignores_limit_cannot_oversize_a_page(self):
        calls = []

        def whole(real, root, kind, source_id, offset, limit):
            calls.append(limit)
            data = self.backend.logs[(kind, source_id)]  # the entire rest of the log whatever ``limit`` says
            return {'status': 'available', 'observed_bytes': len(data),
                    'read': {'dataBase64': base64.b64encode(data[offset:]).decode('ascii'), 'missing': []},
                    'next_offset': None}
        self.serve(whole)
        for offset, length in ((0, 10), (100, 1), (0, 4096), (5, 32768), (0, 65536)):
            with self.subTest(offset=offset, length=length):
                call = self.c['r1'].call('inbox_result', request_id=self.rid, full=True, run_id=RUN, offset=offset,
                                         length=length)
                self.assertEqual(call.status, 200, call.body)
                page = call.json['raw']
                data = page_bytes(page)
                self.assertLessEqual(len(data), length, 'a page is never longer than the length asked for')
                self.assertEqual(data, RUN_LOG[offset:offset + len(data)])
                self.assertEqual(page['length'], len(data))
        self.assertTrue(calls and max(calls) <= 32 * 1024)

    def test_a_backend_returning_more_than_a_page_still_obeys_the_hard_maximum(self):
        self.backend.logs[('run', RUN)] = b'x' * (1 << 20)
        self.serve(lambda real, root, kind, source_id, offset, limit: {
            'status': 'available', 'observed_bytes': 1 << 20,
            'read': {'dataBase64': base64.b64encode(b'x' * (1 << 20)).decode('ascii'), 'missing': []},
            'next_offset': None})
        page = self.result(full=True, run_id=RUN, offset=0, length=131072)['raw']
        self.assertLessEqual(len(page_bytes(page)), 131072)


class ArtifactEdgeTests(RestCase):
    def setUp(self):
        super().setUp()
        self.tree = Tree(self.hub.root_a, self.hub.base, self.hub.root_b)
        self.addCleanup(self.tree.close)

    def test_a_non_directory_used_as_a_directory_is_refused_quickly(self):
        for path in ('pipe/child', 'pipe/../inside.txt', 'inside.txt/x', 'sock/x', 'twin-a/x',
                     'link-out/x', 'pipe-link/x'):
            with self.subTest(path=path):
                started = time.monotonic()
                with Unblock(self.hub.root_a / 'pipe'):
                    call = self.c['r1'].call('inbox_result', request_id=self.rid, full=True, artifact_path=path)
                self.assertLess(time.monotonic() - started, 3.0)
                self.assertGreaterEqual(call.status, 400)
                self.assertIn(call.error['code'], {'forbidden', 'not_found', 'invalid_input'})
                self.assertEqual(call.status, ERROR_STATUS[call.error['code']])

    @unittest.skipIf(os.geteuid() == 0, 'root ignores file modes')
    def test_an_unreadable_file_is_a_clean_refusal(self):
        target = self.hub.root_a / 'private.txt'
        target.write_text('MODE-000-SECRET')
        target.chmod(0)
        self.addCleanup(target.chmod, 0o600)
        call = self.c['r1'].call('inbox_result', request_id=self.rid, full=True, artifact_path='private.txt')
        self.assertGreaterEqual(call.status, 400)
        self.assertIn(call.error['code'], {'forbidden', 'not_found'})
        self.assertEqual(call.status, ERROR_STATUS[call.error['code']])
        self.assertNotIn('MODE-000-SECRET', call.body.decode())

    def test_a_huge_sparse_file_is_read_page_wise_not_loaded(self):
        big = self.hub.root_a / 'sparse.bin'
        with open(big, 'wb') as handle:
            handle.truncate(5 << 30)
            handle.seek((5 << 30) - 4)
            handle.write(b'TAIL')
        started = time.monotonic()
        page = self.result(full=True, artifact_path='sparse.bin', offset=(5 << 30) - 8, length=8)['artifact']
        self.assertEqual(page_bytes(page), b'\0\0\0\0TAIL')
        self.assertEqual((page['size'], page['next_offset']), (5 << 30, None))
        head = self.result(full=True, artifact_path='sparse.bin', offset=0, length=131072)['artifact']
        self.assertEqual((len(page_bytes(head)), head['next_offset']), (131072, 131072))
        self.assertLess(time.monotonic() - started, 5.0)

    def test_a_page_of_a_file_does_not_include_bytes_past_the_page(self):
        (self.hub.root_a / 'secret-tail.txt').write_bytes(b'PUBLIC-HEAD' + b'-' * 100 + b'TAIL-SECRET')
        page = self.result(full=True, artifact_path='secret-tail.txt', offset=0, length=11)['artifact']
        self.assertEqual(page_bytes(page), b'PUBLIC-HEAD')
        self.assertNotIn('TAIL-SECRET', json.dumps(page))

    def test_a_file_that_grows_or_shrinks_between_pages_reports_its_current_size(self):
        target = self.hub.root_a / 'live.log'
        target.write_bytes(b'A' * 100)
        first = self.result(full=True, artifact_path='live.log', offset=0, length=60)['artifact']
        target.write_bytes(b'A' * 100 + b'B' * 50)
        second = self.result(full=True, artifact_path='live.log', offset=first['next_offset'], length=200)['artifact']
        self.assertEqual(page_bytes(second), b'A' * 40 + b'B' * 50)
        target.write_bytes(b'C' * 10)
        self.assertEqual(self.error(full=True, artifact_path='live.log', offset=60, length=10)['code'], 'invalid_input')

    def test_a_deleted_file_and_a_missing_name_are_not_found_without_the_host_path(self):
        for path in ('nope.txt', 'sub/nope.txt', 'nope/dir/file'):
            with self.subTest(path=path):
                error = self.error(full=True, artifact_path=path)
                self.assertEqual(error['code'], 'not_found')
                self.assertNotIn(str(self.hub.base), json.dumps(error))

    def test_a_file_name_with_odd_characters_is_read_as_a_name(self):
        for name in ('sp ace.txt', 'new\nline.txt', 'tab\t.txt', 'ünï.txt', '.wb.txt', 'a.git', 'x.wb'):
            (self.hub.root_a / name).write_bytes(name.encode())
            with self.subTest(name=name):
                self.assertEqual(page_bytes(self.result(full=True, artifact_path=name)['artifact']), name.encode())

    def test_artifact_arguments_that_are_not_one_string_are_a_clean_refusal(self):
        # REST has no JSON types in a query: a number or boolean is just a (missing) file name, and a repeated
        # ``artifact_path`` is the SPEC's 400 ``invalid_input`` (``state`` is the only repeatable parameter).
        for value in (1, True, 1.5, ['inside.txt', 'inside.txt'], ['inside.txt', 'sub/note.txt']):
            with self.subTest(value=value):
                call = self.c['r1'].call('inbox_result', request_id=self.rid, full=True, artifact_path=value)
                self.assertGreaterEqual(call.status, 400)
                self.assertNotEqual(call.error['code'], 'internal_error')
                self.assertLess(call.status, 500)
        repeated = self.c['r1'].call('inbox_result', request_id=self.rid, full=True,
                                     artifact_path=['inside.txt', 'inside.txt'])
        self.assertEqual((repeated.status, repeated.error['code']), (400, 'invalid_input'))


class VisibilityTests(RestCase):
    CALLS = ({'full': True}, {'full': True, 'run_id': RUN}, {'full': True, 'artifact_path': 'inside.txt'},
             {'full': True, 'artifact_path': '.wb/inbox.sqlite3'}, {'full': True, 'artifact_path': 'nope'},
             {'full': True, 'run_id': RUN, 'artifact_path': '../x'}, {})

    def setUp(self):
        super().setUp()
        (self.hub.root_a / 'inside.txt').write_text('INSIDE-PUBLIC')

    def test_a_requester_gets_the_full_view_artifact_and_raw_of_its_own_request(self):
        self.assertIn('full', self.result(full=True))
        self.assertEqual(self.raw_bytes(RUN, offset=0, length=9), RUN_LOG[:9])
        self.assertEqual(page_bytes(self.result(full=True, artifact_path='inside.txt')['artifact']), b'INSIDE-PUBLIC')

    def test_other_requesters_and_out_of_scope_keys_get_not_found_whatever_they_ask(self):
        for name in ('r2', 'rb', 'ed-b'):
            for arguments in self.CALLS:
                with self.subTest(client=name, arguments=arguments):
                    call = self.c[name].call('inbox_result', request_id=self.rid, **arguments)
                    self.assertEqual((call.status, call.error['code']), (404, 'not_found'))
                    text = call.body.decode()
                    for secret in ('INSIDE-PUBLIC', REPORT, VIEW_LOG):
                        self.assertNotIn(secret, text)
        self.assertEqual(self.backend.calls, [], 'a request the principal cannot see touches no backend')

    def test_an_attack_path_is_not_found_for_a_principal_that_cannot_see_the_request(self):
        for path in ('.wb/inbox.sqlite3', '../x', 'inside.txt', '/etc/passwd'):
            self.assertEqual(self.c['r2'].tool_error('inbox_result', request_id=self.rid, full=True,
                                                     artifact_path=path)['code'], 'not_found')

    def test_editors_in_scope_see_every_request_of_their_scope(self):
        for name in ('ed-a', 'ed-all'):
            self.assertEqual(page_bytes(self.result(name, full=True, artifact_path='inside.txt')['artifact']),
                             b'INSIDE-PUBLIC')
            self.assertEqual(page_bytes(self.result(name, self.rid2, full=True, run_id=OTHER_RUN)['raw']), OTHER_LOG)
        self.assertEqual(self.error('ed-a', self.rid_b, full=True)['code'], 'not_found')
        self.assertEqual(self.error('ed-b', self.rid, full=True)['code'], 'not_found')

    def test_an_unknown_request_id_is_not_found(self):
        for rid in (str(uuid.uuid4()), 'x', '../' + self.rid, self.rid.upper()):
            with self.subTest(rid=rid):
                call = self.c['r1'].call('inbox_result', request_id=rid, full=True)
                self.assertIn(call.error['code'], {'not_found', 'invalid_input'})
                self.assertEqual(call.status, ERROR_STATUS[call.error['code']])


# --- T2: web pages ---------------------------------------------------------------------------------

class WebCase(pages.RequestsCase):
    tree = None

    async def asyncSetUp(self):
        self.backend = Backend()
        await super().asyncSetUp()
        self.addCleanup(lambda: self.tree and self.tree.close())

    @property
    def routes(self):  # read by WebAppCase.asyncSetUp, after RequestsCase made the inbox
        self.reader = results.Results(self.inbox, port=self.backend)
        return (request_pages.routes(self.inbox, lambda: list(self.refs), results=self.reader),)

    @routes.setter
    def routes(self, value):
        pass

    def link(self, rid, task_id):
        manager = Principal.manager(self.a)
        self.inbox.review(manager, rid, 'begin_review')
        token = self.inbox.link_check(manager, rid)['link_token']
        self.inbox.link_commit(manager, token, task_id, 1, 'running')
        self.inbox.observe_task(rid, task_id, 'running', view={'status': 'running', 'runs': [
            {'run_id': RUN if task_id == TASK else OTHER_RUN, 'status': 'finished', 'exit_code': 0, 'log': VIEW_LOG,
             'report': REPORT}]})

    def linked(self):
        rid = self.add('Train', 'Run it')
        self.link(rid, TASK)
        if self.tree is None:  # after the first inbox use: the hard link to ``.wb/inbox.sqlite3`` needs the file
            self.tree = Tree(self.root_a, Path(self.base_temp.name).resolve(), self.root_b)
        return rid

    async def fetch(self, path, cookie=True):
        headers = {'Host': self.host()}
        if cookie:
            headers['Cookie'] = f'wb_hub_session={self.cookie}'
        async with self.http.get(self.base + path, headers=headers, allow_redirects=False) as response:
            return response.status, response.headers, await response.read()


class WebResultTests(WebCase):
    async def test_the_detail_page_and_full_page_never_contain_raw_log_or_artifact_bytes(self):
        rid = self.linked()
        (self.root_a / 'out.txt').write_text('ARTIFACT-CONTENT')
        status, _, body = await self.fetch(f'/requests/{rid}')
        self.assertEqual(status, 200)
        for secret in (VIEW_LOG, REPORT, 'ARTIFACT-CONTENT', 'epoch 1'):
            self.assertNotIn(secret.encode(), body, 'the default detail page is the summary')
        status, _, body = await self.fetch(f'/requests/{rid}/full')
        self.assertEqual(status, 200)
        self.assertIn(REPORT.encode(), body)
        for secret in (b'ARTIFACT-CONTENT', COMMAND_LOG.strip().split(b'\n')[0], b'OTHER-REQUEST', str(self.root_a).encode()):
            self.assertNotIn(secret, body)

    async def test_raw_and_artifact_pages_are_plain_text_with_nosniff_and_csp(self):
        rid = self.linked()
        html = b'<!doctype html><script>alert(1)</script><img src=x onerror=alert(2)>'
        (self.root_a / 'evil.html').write_bytes(html)
        (self.root_a / 'evil.svg').write_bytes(b'<svg onload=alert(3)></svg>')
        self.backend.logs[('run', RUN)] = html + RUN_LOG
        for path, body in ((f'/requests/{rid}/raw?run_id={RUN}&offset=0&length={len(html)}', html),
                           (f'/requests/{rid}/raw?run_id={COMMAND}', COMMAND_LOG),
                           (f'/requests/{rid}/artifact?path=evil.html', html),
                           (f'/requests/{rid}/artifact?path=evil.svg', b'<svg onload=alert(3)></svg>'),
                           (f'/requests/{rid}/artifact?path=evil.html&offset=15&length=8', html[15:23])):
            with self.subTest(path=path):
                status, headers, data = await self.fetch(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Content-Type'], 'text/plain; charset=utf-8')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
                self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
                self.assertTrue(headers['Content-Disposition'].startswith('inline'))
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(data, body)

    async def test_the_full_page_shows_escaped_report_text(self):
        rid = self.linked()
        self.backend.tasks[TASK]['reports'][0]['text'] = '<script>alert(9)</script>'
        _, headers, body = await self.fetch(f'/requests/{rid}/full')
        self.assertNotIn(b'<script>alert(9)', body)
        self.assertIn(b'&lt;script&gt;alert(9)', body)
        self.assertTrue(headers['Content-Type'].startswith('text/html'))

    async def test_raw_of_a_run_of_another_request_or_project_is_404_and_never_read(self):
        rid = self.linked()
        other = self.add('Other', 'b')
        self.link(other, OTHER_TASK)
        foreign_rid = self.add('Beta', 'b', project=self.b)
        for path in (f'/requests/{rid}/raw?run_id={OTHER_RUN}', f'/requests/{rid}/raw?run_id={OTHER_COMMAND}',
                     f'/requests/{rid}/raw?run_id={FOREIGN_RUN}', f'/requests/{rid}/raw?run_id=',
                     f'/requests/{rid}/raw', f'/requests/{rid}/raw?run_id={RUN.upper()}',
                     f'/requests/{rid}/raw?run_id=../{RUN}', f'/requests/{foreign_rid}/raw?run_id={RUN}'):
            with self.subTest(path=path):
                status, headers, data = await self.fetch(path)
                self.assertEqual(status, 404, data)
                self.assertTrue(headers['Content-Type'].startswith('text/html'))
                for secret in (b'OTHER-REQUEST', b'OTHER-PROJECT'):
                    self.assertNotIn(secret, data)
        self.assertEqual(self.backend.raw_calls(), [])

    async def test_raw_page_bounds_and_backend_off(self):
        rid = self.linked()
        for query, status in (('offset=-1', 400), ('length=0', 400), ('length=131073', 400), ('offset=abc', 400),
                              ('length=1.5', 400), (f'offset={len(RUN_LOG) + 1}', 400), ('length=99999999999999999999', 400)):
            with self.subTest(query=query):
                code, _, data = await self.fetch(f'/requests/{rid}/raw?run_id={RUN}&{query}')
                self.assertEqual(code, status, data)
        code, _, data = await self.fetch(f'/requests/{rid}/raw?run_id={RUN}&offset={len(RUN_LOG)}')
        self.assertEqual((code, data), (200, b''))
        self.backend.down = 'backend_not_running'
        code, _, data = await self.fetch(f'/requests/{rid}/raw?run_id={RUN}&offset=0&length=20')
        self.assertEqual(code, 409)
        self.assertNotIn(base64.b64encode(RUN_LOG[:9]), data)

    async def test_every_artifact_attack_is_refused_over_the_web(self):
        rid = self.linked()
        base = str(self.tree.base).encode()
        for label, path in self.tree.attacks().items():
            with self.subTest(label=label):
                started = time.monotonic()
                with Unblock(self.root_a / 'pipe'):
                    status, headers, data = await self.fetch(
                        f'/requests/{rid}/artifact?path={quote(path, safe="", errors="surrogatepass")}')
                self.assertLess(time.monotonic() - started, 3.0)
                self.assertIn(status, {400, 403, 404}, data[:200])
                self.assertTrue(headers['Content-Type'].startswith('text/html'))
                for secret in Tree.SECRETS:
                    self.assertNotIn(secret.encode(), data)
                self.assertNotIn(base, data)

    async def test_web_artifact_reads_legitimate_files_and_nothing_else_changes(self):
        rid = self.linked()
        for path, expected in (('inside.txt', b'INSIDE-PUBLIC'), ('sub/note.txt', b'SUB-NOTE'),
                               (str(self.root_a / 'sub' / 'note.txt'), b'SUB-NOTE'), ('.gitignore', b'IGNORED\n')):
            status, _, data = await self.fetch(f'/requests/{rid}/artifact?path={quote(path, safe="")}')
            self.assertEqual((status, data), (200, expected))

    async def test_result_pages_need_a_session_or_a_valid_one(self):
        rid = self.linked()
        for suffix in ('/full', f'/raw?run_id={RUN}', '/artifact?path=inside.txt'):
            status, headers, _ = await self.fetch(f'/requests/{rid}{suffix}', cookie=False)
            self.assertEqual((status, headers['Location']), (303, '/login'))
            headers = {'Host': self.host(), 'Cookie': 'wb_hub_session=forged'}
            async with self.http.get(self.base + f'/requests/{rid}{suffix}', headers=headers,
                                     allow_redirects=False) as response:
                self.assertEqual(response.status, 303)
        self.assertEqual(self.backend.calls, [])

    async def test_pages_of_a_request_that_does_not_exist_are_404(self):
        for suffix in ('/full', f'/raw?run_id={RUN}', '/artifact?path=inside.txt'):
            status, _, _ = await self.fetch(f'/requests/{uuid.uuid4()}{suffix}')
            self.assertEqual(status, 404)



class WebMalformedBackendTests(WebCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.rid = self.linked()

    async def test_malformed_raw_query_answers_are_not_a_server_error(self):
        for label, answer in MalformedBackendTests.BAD_ANSWERS.items():
            real = self.backend.raw_query
            self.backend.raw_query = (lambda a: lambda root, kind, sid, offset, limit, **_: a(real, root, kind, sid,
                                                                                              offset, limit))(answer)
            with self.subTest(label=label):
                status, headers, data = await self.fetch(f'/requests/{self.rid}/raw?run_id={RUN}&offset=0&length=5')
                self.assertNotEqual(status, 500, data[:200])
                self.assertIn(status, {200, 400, 404, 409, 502, 503})
                if status == 200:
                    self.assertLessEqual(len(data), 5)
                self.assertNotIn(b'Traceback', data)
            self.backend.raw_query = real

    async def test_a_backend_that_ignores_limit_cannot_oversize_a_web_page(self):
        def whole(root, kind, source_id, offset, limit, **_):
            data = self.backend.logs[(kind, source_id)]
            return {'status': 'available', 'observed_bytes': len(data),
                    'read': {'dataBase64': base64.b64encode(data[offset:]).decode('ascii'), 'missing': []},
                    'next_offset': None}
        self.backend.raw_query = whole
        for offset, length in ((0, 10), (7, 4096), (0, 65536)):
            with self.subTest(offset=offset, length=length):
                status, _, data = await self.fetch(f'/requests/{self.rid}/raw?run_id={RUN}&offset={offset}&length={length}')
                self.assertEqual(status, 200)
                self.assertLessEqual(len(data), length)
                self.assertEqual(data, RUN_LOG[offset:offset + len(data)])

    async def test_full_page_with_the_backend_off_shows_the_last_view_only(self):
        self.backend.down = 'backend_not_running'
        status, headers, body = await self.fetch(f'/requests/{self.rid}/full')
        self.assertEqual(status, 200)
        for secret in (REPORT.encode(), b'epoch 1', str(self.root_a).encode()):
            self.assertNotIn(secret, body)
        status, _, body = await self.fetch(f'/requests/{self.rid}/raw?run_id={RUN}')
        self.assertEqual(status, 409)
        self.assertNotIn(base64.b64encode(RUN_LOG[:9]), body)

    async def test_missing_path_missing_run_and_wrong_methods(self):
        status, _, _ = await self.fetch(f'/requests/{self.rid}/artifact')
        self.assertIn(status, {400, 404})
        status, _, _ = await self.fetch(f'/requests/{self.rid}/artifact?path=')
        self.assertIn(status, {400, 404})
        for suffix in ('/raw?run_id=' + RUN, '/artifact?path=inside.txt', '/full'):
            headers = {'Host': self.host(), 'Cookie': f'wb_hub_session={self.cookie}'}
            async with self.http.post(self.base + f'/requests/{self.rid}{suffix}', headers=headers,
                                      allow_redirects=False) as response:
                self.assertIn(response.status, {403, 404, 405})
        self.assertEqual(self.backend.raw_calls(), [])

    async def test_a_request_without_a_task_has_a_full_page_and_no_raw(self):
        fresh = self.add('Nothing linked', 'b')
        status, _, body = await self.fetch(f'/requests/{fresh}/full')
        self.assertEqual(status, 200)
        self.assertNotIn(REPORT.encode(), body)
        status, _, _ = await self.fetch(f'/requests/{fresh}/raw?run_id={RUN}')
        self.assertEqual(status, 404)
        self.assertEqual(self.backend.raw_calls(), [])


class IdenticalBytesTests(WebCase):
    """The same range through REST ``/result`` and the web page is the same bytes (and the oracle's)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.rest_api = inbox_api.InboxApi(self.inbox, results=self.reader)
        self.rid = self.linked()
        (self.root_a / 'data.bin').write_bytes(RUN_LOG[:70000])
        (self.root_a / 'text.txt').write_bytes(TEXT_LOG)
        self.backend.logs[('run', RUN)] = RUN_LOG

    async def test_same_range_same_bytes(self):
        # /api/v1/ is served by a second app over the same inbox, results and key store
        from aiohttp import test_utils
        from workbench.hub.web.app import create_app
        server = test_utils.TestServer(create_app(self.services, routes=[rest.routes(self.rest_api)]),
                                       host='127.0.0.1', port=0)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        key = self.services.api_keys.issue('ed', 'editor', {'all': True}, None, now=self.now[0]).raw
        client = RestTools(RestClient('127.0.0.1', server.port, key, timeout=10))
        cases = [('run_id', RUN, f'/requests/{self.rid}/raw?run_id={RUN}', RUN_LOG),
                 ('artifact_path', 'data.bin', f'/requests/{self.rid}/artifact?path=data.bin', RUN_LOG[:70000]),
                 ('artifact_path', 'text.txt', f'/requests/{self.rid}/artifact?path=text.txt', TEXT_LOG)]
        for field, value, url, data in cases:
            for offset, length in ((0, 1), (0, 131072), (32767, 3), (65535, 65537), (len(data) - 4, 100),
                                   (len(data), 10), (7, 4096), (len(data) - 1, 1)):
                with self.subTest(field=field, value=value, offset=offset, length=length):
                    status, headers, web = await self.fetch(f'{url}&offset={offset}&length={length}')
                    self.assertEqual(status, 200)
                    value_ = await asyncio.to_thread(client.tool, 'inbox_result', request_id=self.rid, full=True,
                                                     offset=offset, length=length, **{field: value})
                    page = value_['raw' if field == 'run_id' else 'artifact']
                    self.assertEqual(page_bytes(page), web)
                    self.assertEqual(web, data[offset:offset + length])


if __name__ == '__main__':
    unittest.main()
