"""hub_v1 backend principal (TI-09, seam T3b): a backend's hello on the Hub socket.

A real ``wb _hub --foreground`` in a temp HOME/XDG sandbox with a short private ``XDG_RUNTIME_DIR``. The "backend"
is a fake peer process this test starts (``PEER`` below): it holds ``.wb/backend.lock`` and publishes a
``backend.json`` naming itself like a real backend does, then speaks hub_v1 from its own pid, so the Hub's
``SO_PEERCRED`` check sees the recorded backend. The test process itself is never the recorded backend, which makes
it a forger. Expected values come from the SPEC "Hub ↔ backend 요청 통로" text, ``/proc/<pid>/stat`` and the
``backend.json`` the peer wrote. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from test_hub_process import REPO, HubSandbox, proc_start_ticks, wait_until
from test_hub_protocol import Line

sys.path.insert(0, str(REPO / 'src'))

from workbench.backend.project import initialize_project, resolve_project_context  # noqa: E402

BACKEND = 'workbench-backend'

# The fake backend peer. argv: PROJECT_DIR. stdin: one JSON command per line; stdout: one JSON answer per line.
PEER = r'''
import fcntl, json, os, socket, sys
from pathlib import Path
from workbench.backend.project import resolve_project_context
from workbench.backend.runtime_paths import ensure_runtime_root

context = resolve_project_context(Path(sys.argv[1]), dict(os.environ))
data, runtime = context.data_root, context.runtime_root
lock = os.open(data / 'backend.lock', os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX)
ticks = int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])
marker = json.loads((data / 'project.json').read_text())
ensure_runtime_root(runtime, data, marker['project_id'])
record = {'data_dir': str(data), 'project_dir': str(context.project_root), 'runtime_root': str(runtime),
          'project_id': marker['project_id'], 'uid': os.geteuid(),
          'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
          'ui_socket': str(runtime / 'ui.sock'), 'bridge_socket': str(runtime / 'bridge.sock'),
          'processes': {'backend': {'pid': os.getpid(), 'start_ticks': ticks}}}
staged = data / 'peer-record'
staged.write_text(json.dumps(record))
staged.chmod(0o600)
staged.replace(data / 'backend.json')
print(json.dumps({'ready': True, 'pid': os.getpid(), 'start_ticks': ticks}), flush=True)
sock, buffer, link = None, b'', None

def reply():
    global buffer
    while b'\n' not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            return {'eof': True}
        buffer += chunk
    line, buffer = buffer.split(b'\n', 1)
    return json.loads(line)

for line in sys.stdin:
    command = json.loads(line)
    if command['op'] == 'hello':
        state = Path(os.environ['XDG_STATE_HOME']) / 'omp-workbench' / 'hub' / 'hub.json'
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(10)
        sock.connect(json.loads(state.read_text())['socket'])
        buffer = b''
        sock.sendall(json.dumps(command['hello']).encode() + b'\n')
        answer = reply()
    elif command['op'] == 'send':
        try:
            sock.sendall(json.dumps(command['frame']).encode() + b'\n')
            answer = reply()
        except OSError:  # the Hub closed the connection
            answer = {'eof': True}
    elif command['op'] == 'link':
        from workbench.backend.inbox_link import InboxLink
        if link is None:
            link = InboxLink(marker['project_id'], environment=dict(os.environ), timeout=command.get('timeout', 5.0))
        outcome = link.call(command['call'], **command.get('fields', {}))
        answer = {'status': outcome.status, 'reason': outcome.reason, 'value': outcome.value}
    else:
        break
    print(json.dumps(answer), flush=True)
'''


class PrincipalSandbox(HubSandbox):
    """``HubSandbox`` plus a short private runtime dir (a long one would fall back to ``/tmp/omp-workbench-<uid>``),
    WB projects, a registry and fake backend peers."""

    def __init__(self, case: unittest.TestCase):
        super().__init__(case)
        self.run = Path(tempfile.mkdtemp(prefix='r', dir='/tmp')).resolve()
        os.chmod(self.run, 0o700)
        self.runtime = self.run
        self.socket_dir = self.run / 'omp-workbench-hub'
        self.env['XDG_RUNTIME_DIR'] = str(self.run)
        self.peer_script = self.base / 'peer.py'
        self.peer_script.write_text(PEER)
        self.entries: list[dict] = []

    def cleanup(self) -> None:
        try:
            super().cleanup()
        finally:
            shutil.rmtree(self.run, ignore_errors=True)

    def project(self, name: str) -> tuple[Path, str]:
        root = (self.base / 'work' / name)
        root.mkdir(parents=True)
        root = root.resolve()
        initialize_project(resolve_project_context(root, self.env), confirm=lambda _root: True, interactive=True,
                           environment=self.env)
        project_id = json.loads((root / '.wb' / 'project.json').read_text())['project_id']
        return root, project_id

    def register(self, root: Path, project_id: str) -> None:
        self.entries.append({'root': str(root), 'project_id': project_id, 'name': root.name, 'registered_at': 1.0,
                             'source': 'add'})
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.state / 'registry.json'
        path.write_text(json.dumps({'schema_version': 1, 'projects': self.entries}))
        os.chmod(path, 0o600)

    def peer(self, root: Path) -> 'Peer':
        process = subprocess.Popen([sys.executable, str(self.peer_script), str(root)], env=self.env, cwd=self.base,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.processes.append(process)
        peer = Peer(process)
        first = peer.read()
        self.case.assertTrue(first.get('ready'), first)
        peer.pid, peer.start_ticks = first['pid'], first['start_ticks']
        return peer


class Peer:
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.pid = self.start_ticks = 0

    def read(self) -> dict:
        line = self.process.stdout.readline()
        if not line:
            raise AssertionError(f'peer ended: {self.process.stderr.read()}')
        return json.loads(line)

    def ask(self, **command) -> dict:
        self.process.stdin.write(json.dumps(command) + '\n')
        self.process.stdin.flush()
        return self.read()

    def hello(self, project_id: object, client: str = BACKEND) -> dict:
        return self.ask(op='hello', hello={'v': 1, 'type': 'hello', 'client': client, 'project_id': project_id})

    def request(self, frame_id: str, kind: str, **fields) -> dict:
        return self.ask(op='send', frame={'v': 1, 'id': frame_id, 'type': kind, **fields})


class BackendPrincipalCase(unittest.TestCase):
    def setUp(self):
        self.box = PrincipalSandbox(self)

    def connect(self) -> Line:
        line = Line(self.box.record()['socket'])
        self.addCleanup(line.close)
        return line


class HelloProjectIdTests(BackendPrincipalCase):
    def test_a_backend_hello_with_a_malformed_project_id_is_a_bad_request_and_closed(self):
        self.box.start_hub()
        for project_id in ('x', 7, True, [], str(uuid.uuid4()).upper(), '{' + str(uuid.uuid4()) + '}', ''):
            with self.subTest(project_id=project_id):
                line = self.connect()
                line.send({'v': 1, 'type': 'hello', 'client': BACKEND, 'project_id': project_id})
                self.assertEqual(line.recv(), {'v': 1, 'type': 'reject', 'reason': 'bad_request'})
                self.assertTrue(line.closed())


class PrincipalGrantTests(BackendPrincipalCase):
    def test_the_recorded_backend_of_a_registered_project_gets_the_backend_principal(self):
        root, project_id = self.box.project('alpha')
        self.box.register(root, project_id)
        hub = self.box.start_hub()
        peer = self.box.peer(root)
        record = json.loads((root / '.wb' / 'backend.json').read_text())
        self.assertEqual(record['processes']['backend'], {'pid': peer.pid, 'start_ticks': proc_start_ticks(peer.pid)})
        welcome = peer.hello(project_id)
        self.assertEqual((welcome['type'], welcome['hub']['pid']), ('welcome', hub.pid), welcome)
        listed = peer.request('l1', 'inbox_list')
        self.assertEqual({key: listed.get(key) for key in ('id', 'type', 'ok', 'requests', 'next_cursor')},
                         {'id': 'l1', 'type': 'result', 'ok': True, 'requests': [], 'next_cursor': None}, listed)


class ForgeryTests(BackendPrincipalCase):
    """Anything but the recorded backend of that registered project is ``reject``/``principal_mismatch``."""

    MISMATCH = {'v': 1, 'type': 'reject', 'reason': 'principal_mismatch'}

    def assert_mismatch(self, line: Line, hello: dict) -> None:
        line.send(hello)
        self.assertEqual(line.recv(), self.MISMATCH, hello)
        self.assertTrue(line.closed())

    def rewrite_record(self, root: Path, **reference) -> None:
        path = root / '.wb' / 'backend.json'
        record = json.loads(path.read_text())
        record['processes']['backend'].update(reference)
        staged = path.with_name('test-record')
        staged.write_text(json.dumps(record))
        staged.chmod(0o600)
        staged.replace(path)

    def test_a_process_that_is_not_the_recorded_backend_is_refused(self):
        root, project_id = self.box.project('alpha')
        self.box.register(root, project_id)
        self.box.start_hub()
        peer = self.box.peer(root)
        self.assertNotEqual(peer.pid, os.getpid())
        self.assert_mismatch(self.connect(), {'v': 1, 'type': 'hello', 'client': BACKEND, 'project_id': project_id})
        # the real backend is still served afterwards
        self.assertEqual(peer.hello(project_id)['type'], 'welcome')

    def test_the_backend_of_one_project_cannot_take_another_projects_principal(self):
        root, project_id = self.box.project('alpha')
        other, other_id = self.box.project('beta')
        self.box.register(root, project_id)
        self.box.register(other, other_id)
        self.box.start_hub()
        peer = self.box.peer(root)
        self.assertEqual(peer.hello(other_id), self.MISMATCH)
        self.assertEqual(peer.hello(str(uuid.uuid4())), self.MISMATCH, 'an unregistered project id')

    def test_an_unregistered_project_is_refused_even_for_its_recorded_backend(self):
        root, project_id = self.box.project('alpha')
        self.box.start_hub()
        peer = self.box.peer(root)
        self.assertEqual(peer.hello(project_id), self.MISMATCH)

    def test_a_backend_hello_without_project_id_or_a_project_id_without_the_backend_name_is_refused(self):
        root, project_id = self.box.project('alpha')
        self.box.register(root, project_id)
        self.box.start_hub()
        peer = self.box.peer(root)
        self.assertEqual(peer.ask(op='hello', hello={'v': 1, 'type': 'hello', 'client': BACKEND}), self.MISMATCH)
        self.assertEqual(peer.ask(op='hello', hello={'v': 1, 'type': 'hello', 'client': BACKEND,
                                                     'project_id': None}), self.MISMATCH)
        for client in ('workbench-cli', 'workbench-hub', 'workbench-backend ', 'Workbench-Backend'):
            with self.subTest(client=client):
                self.assertEqual(peer.hello(project_id, client=client), self.MISMATCH)

    def test_a_record_whose_start_ticks_are_not_the_peers_is_refused(self):
        root, project_id = self.box.project('alpha')
        self.box.register(root, project_id)
        self.box.start_hub()
        peer = self.box.peer(root)
        self.rewrite_record(root, start_ticks=peer.start_ticks + 1)
        self.assertEqual(peer.hello(project_id), self.MISMATCH)

    def test_the_principal_is_checked_again_on_every_request_and_a_failed_check_closes(self):
        root, project_id = self.box.project('alpha')
        self.box.register(root, project_id)
        self.box.start_hub()
        peer = self.box.peer(root)
        self.assertEqual(peer.hello(project_id)['type'], 'welcome')
        self.assertTrue(peer.request('l1', 'inbox_list')['ok'])
        # another live process becomes the recorded backend (a restart the old connection outlived)
        self.rewrite_record(root, pid=os.getpid(), start_ticks=proc_start_ticks(os.getpid()))
        refused = peer.request('l2', 'inbox_list')
        self.assertEqual((refused.get('id'), refused.get('ok'), refused.get('reason')),
                         ('l2', False, 'principal_mismatch'), refused)
        self.assertEqual(peer.ask(op='send', frame={'v': 1, 'id': 'l3', 'type': 'inbox_list'}), {'eof': True})


BACKEND_OPS = ('inbox_list', 'inbox_show', 'inbox_review', 'inbox_link_check', 'inbox_link_commit', 'inbox_delivery')
LOCAL_OPS = ('status', 'list', 'stop_request', 'stop_confirm', 'review_mode')
RESERVED = ('power_on', 'power_off_request', 'power_off_confirm', 'pause', 'resume', 'confirm_boot')


class OpPermissionTests(BackendPrincipalCase):
    def unsupported(self, reply: dict, request_id: str) -> None:
        self.assertEqual({key: reply.get(key) for key in ('id', 'ok', 'reason')},
                         {'id': request_id, 'ok': False, 'reason': 'unsupported_type'}, reply)

    def test_a_local_connection_gets_no_backend_op_and_stays_open(self):
        self.box.start_hub()
        line = self.connect()
        self.assertEqual(line.hello()['type'], 'welcome')
        for number, op in enumerate(BACKEND_OPS):
            with self.subTest(op=op):
                line.send({'v': 1, 'id': f'b{number}', 'type': op, 'request_id': str(uuid.uuid4())})
                self.unsupported(line.recv(), f'b{number}')
        self.assertTrue(line.request('s1', 'status')['ok'])

    def test_a_backend_principal_gets_no_local_or_reserved_op(self):
        root, project_id = self.box.project('alpha')
        self.box.register(root, project_id)
        hub = self.box.start_hub()
        peer = self.box.peer(root)
        self.assertEqual(peer.hello(project_id)['type'], 'welcome')
        for number, op in enumerate(LOCAL_OPS + RESERVED):
            with self.subTest(op=op):
                self.unsupported(peer.request(f'o{number}', op, root=str(root), token='x', set='manual'),
                                 f'o{number}')
        self.assertIsNone(hub.poll(), 'no stop was offered or confirmed')
        self.assertTrue(peer.request('l1', 'inbox_list')['ok'])

    def test_review_mode_is_a_local_op_on_a_registered_root(self):
        root, project_id = self.box.project('alpha')
        self.box.register(root, project_id)
        self.box.start_hub()
        line = self.connect()
        line.hello()
        self.assertEqual({key: line.request('r1', 'review_mode', root=str(root)).get(key) for key in
                          ('ok', 'review_mode')}, {'ok': True, 'review_mode': 'auto'})
        changed = line.request('r2', 'review_mode', root=str(root), set='manual')
        self.assertEqual((changed.get('ok'), changed.get('review_mode')), (True, 'manual'), changed)
        self.assertEqual(line.request('r3', 'review_mode', root=str(root))['review_mode'], 'manual')
        refused = line.request('r4', 'review_mode', root=str(root), set='sometimes')
        self.assertEqual((refused.get('ok'), refused.get('reason')), (False, 'invalid_input'), refused)
        missing = line.request('r5', 'review_mode', root=str(self.box.base / 'work' / 'nowhere'))
        self.assertEqual((missing.get('ok'), missing.get('reason')), (False, 'not_found'), missing)


def seed_requests(projects: list[tuple[Path, str]], plan: dict[str, list[tuple[str, str, bool]]]) -> dict[str, list]:
    """Write requests through the product's own inbox before the Hub starts (the inbox has one writer at a time):
    ``plan[project_id]`` = ``[(title, body, cancelled)]``; returns the request ids per project."""
    from workbench.hub.inbox.service import InboxService, Principal, ProjectRef
    service = InboxService()
    try:
        service.set_projects(ProjectRef(project_id, root, root.name) for root, project_id in projects)
        made = {}
        for project_id, items in plan.items():
            made[project_id] = []
            for title, body, cancelled in items:
                request_id = service.submit(Principal.owner(), project_id, title, body)['request_id']
                if cancelled:
                    service.cancel(Principal.owner(), request_id)
                made[project_id].append(request_id)
        return made
    finally:
        service.close()


class InboxReadTests(BackendPrincipalCase):
    """``inbox_list``/``inbox_show``: the manager principal sees its own project's requests only."""

    def test_list_and_show_cover_this_project_only(self):
        alpha, alpha_id = self.box.project('alpha')
        beta, beta_id = self.box.project('beta')
        self.box.register(alpha, alpha_id)
        self.box.register(beta, beta_id)
        made = seed_requests([(alpha, alpha_id), (beta, beta_id)], {
            alpha_id: [('open one', 'BODY-ALPHA-1', False), ('dropped', 'BODY-ALPHA-2', True)],
            beta_id: [('other project', 'BODY-BETA', False)]})
        (open_id, closed_id), (beta_request,) = made[alpha_id], made[beta_id]
        self.box.start_hub()
        peer = self.box.peer(alpha)
        self.assertEqual(peer.hello(alpha_id)['type'], 'welcome')
        listed = peer.request('l1', 'inbox_list')
        self.assertTrue(listed['ok'], listed)
        self.assertEqual([(item['request_id'], item['project_id'], item['state'], item['title'])
                          for item in listed['requests']], [(open_id, alpha_id, 'NEW', 'open one')])
        closed = peer.request('l2', 'inbox_list', states=['CLOSED'])
        self.assertEqual([(item['request_id'], item['state'], item['outcome']) for item in closed['requests']],
                         [(closed_id, 'CLOSED', 'cancelled')], closed)
        shown = peer.request('s1', 'inbox_show', request_id=open_id)
        self.assertTrue(shown['ok'], shown)
        self.assertEqual((shown['request']['request_id'], shown['request']['body'], shown['request']['state'],
                          shown['request']['project_id']), (open_id, 'BODY-ALPHA-1', 'NEW', alpha_id))
        for number, hidden in enumerate((beta_request, str(uuid.uuid4()), 7)):
            with self.subTest(hidden=hidden):
                refused = peer.request(f'h{number}', 'inbox_show', request_id=hidden)
                self.assertEqual((refused['ok'], refused['reason']), (False, 'not_found'), refused)
                self.assertNotIn('BODY-BETA', json.dumps(refused))
        bad = peer.request('b1', 'inbox_list', states='NEW')
        self.assertEqual((bad['ok'], bad['reason']), (False, 'invalid_input'), bad)
        bad = peer.request('b2', 'inbox_list', limit=0)
        self.assertEqual((bad['ok'], bad['reason']), (False, 'invalid_input'), bad)


class InboxLinkResultTests(BackendPrincipalCase):
    """``workbench.backend.inbox_link.InboxLink`` run inside the fake backend peer: ok / rejected / unavailable /
    unknown are told apart (SPEC "Manager 도구" result classes)."""

    def link(self, peer: Peer, call: str, timeout: float = 5.0, **fields) -> dict:
        return peer.ask(op='link', call=call, fields=fields, timeout=timeout)

    def test_without_a_hub_a_call_is_unavailable(self):
        root, project_id = self.box.project('alpha')
        self.box.register(root, project_id)
        peer = self.box.peer(root)
        answer = self.link(peer, 'inbox_list')
        self.assertEqual(answer['status'], 'unavailable', answer)

    def test_answers_are_ok_or_rejected_and_a_refused_principal_is_unavailable(self):
        root, project_id = self.box.project('alpha')
        other, _other_id = self.box.project('beta')
        self.box.register(root, project_id)
        self.box.start_hub()
        peer = self.box.peer(root)
        listed = self.link(peer, 'inbox_list')
        self.assertEqual((listed['status'], listed['value'].get('requests')), ('ok', []), listed)
        missing = self.link(peer, 'inbox_show', request_id=str(uuid.uuid4()))
        self.assertEqual((missing['status'], missing['reason']), ('rejected', 'not_found'), missing)
        stranger = self.box.peer(other)  # beta is not registered: the Hub refuses its principal
        refused = self.link(stranger, 'inbox_list')
        self.assertEqual((refused['status'], refused['reason']), ('unavailable', 'principal_mismatch'), refused)

    def test_a_sent_request_without_an_answer_is_unknown(self):
        root, project_id = self.box.project('alpha')
        self.box.register(root, project_id)
        hub = self.box.start_hub()
        peer = self.box.peer(root)
        self.assertEqual(self.link(peer, 'inbox_list', timeout=1.0)['status'], 'ok')
        os.kill(hub.pid, signal.SIGSTOP)  # our own Hub; the sandbox cleanup continues it before ending it
        try:
            answer = self.link(peer, 'inbox_list', timeout=1.0)
        finally:
            os.kill(hub.pid, signal.SIGCONT)
        self.assertEqual(answer['status'], 'unknown', answer)


CONTRACT_BACKEND =Path(__file__).resolve().parent / 'fixtures' / 'contract_backend.py'
TASK_ID = '2c5e8a1f-7b3d-4c9e-8f60-1a2b3c4d5e6f'


class BackendPortTests(BackendPrincipalCase):
    """The Hub's ui_v1 helper (``workbench.hub.inbox.backend_port``) against a contract backend fixture."""

    def contract_backend(self, root: Path, replies: dict | None = None) -> subprocess.Popen:
        frames = self.box.base / f'frames-{root.name}.jsonl'
        process = self.box.popen([sys.executable, str(CONTRACT_BACKEND), str(root), '--frames-log', str(frames),
                                  '--replies-json', json.dumps(replies or {})])
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        return process

    def frames(self, root: Path) -> list[dict]:
        path = self.box.base / f'frames-{root.name}.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def environment(self):
        """The helper runs in this test process: point its environment at the sandbox (restored afterwards)."""
        saved = {key: os.environ.get(key) for key in self.box.env}
        os.environ.update(self.box.env)

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)

    def test_a_running_backend_answers_through_a_verified_hub_connection(self):
        from workbench.hub.inbox import backend_port
        root, _project_id = self.box.project('alpha')
        self.environment()
        process = self.contract_backend(root, {'task_status': {'tasks': [{'task_id': TASK_ID, 'exists': False}]}})
        tasks = backend_port.task_status(root, [TASK_ID], detail=True)
        self.assertEqual(tasks, [{'task_id': TASK_ID, 'exists': False}])
        hello, request = [item['header'] for item in self.frames(root)]
        self.assertEqual((hello['type'], hello['client']), ('hello', 'workbench-hub'))
        self.assertEqual((request['type'], request['task_ids'], request['detail']), ('task_status', [TASK_ID], True))
        self.assertIsNone(process.poll())

    def test_an_older_backend_is_backend_too_old(self):
        from workbench.hub.inbox import backend_port
        self.environment()
        for reason in ('unsupported_type', 'not_attached'):
            with self.subTest(reason=reason):
                root, _project_id = self.box.project(reason)
                self.contract_backend(root, {'task_status': {'ok': False, 'reason': reason}})
                with self.assertRaises(backend_port.BackendPortError) as raised:
                    backend_port.task_status(root, [TASK_ID])
                self.assertEqual(raised.exception.reason, 'backend_too_old')

    def test_a_stopped_project_is_backend_not_running_and_nothing_is_contacted(self):
        from workbench.hub.inbox import backend_port
        root, _project_id = self.box.project('alpha')
        self.environment()
        with self.assertRaises(backend_port.BackendPortError) as raised:
            backend_port.task_status(root, [TASK_ID])
        self.assertEqual(raised.exception.reason, 'backend_not_running')
        self.assertEqual(self.frames(root), [])


if __name__ == '__main__':
    unittest.main()
