"""TI-09 independent verification (TI-T04, seam T3b): the hub_v1 backend principal.

Derived from the Task Inbox SPEC "Hub <-> backend request channel" and the TI-09 ticket (independent focus: pid reuse,
start_ticks mismatch, a connection outliving its backend, hello without ``project_id``, attached-UI interference), not
from the worker's tests. A real ``wb _hub --foreground`` runs in a temp HOME/XDG sandbox with a short private
``XDG_RUNTIME_DIR``. "Backends" are fake peer processes this test starts: each holds ``.wb/backend.lock`` and publishes
a ``backend.json`` naming itself (pid and ``/proc`` start ticks), then speaks hub_v1 from its own pid. The test process
is a same-uid forger that is never a recorded backend. Expected values come from the SPEC text, ``/proc`` and the
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

from test_hub_process import REPO, HubSandbox, alive, proc_start_ticks, wait_until
from test_hub_protocol import Line

sys.path.insert(0, str(REPO / 'src'))

from workbench.backend.project import initialize_project, resolve_project_context  # noqa: E402

BACKEND = 'workbench-backend'
MISMATCH = {'v': 1, 'type': 'reject', 'reason': 'principal_mismatch'}

# Fake backend. stdin: one JSON command per line, stdout: one JSON answer per line. Connections are named (``conn``).
PEER = r'''
import fcntl, json, os, select, socket, sys, time
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
socks, buffers, link = {}, {}, None


def frame(conn, timeout):
    sock = socks[conn]
    deadline = time.monotonic() + timeout
    while b'\n' not in buffers[conn]:
        left = deadline - time.monotonic()
        if left <= 0:
            return {'timeout': True}
        if not select.select([sock], [], [], left)[0]:
            return {'timeout': True}
        try:
            chunk = sock.recv(65536)
        except OSError:
            return {'eof': True}
        if not chunk:
            return {'eof': True}
        buffers[conn] += chunk
    line, buffers[conn] = buffers[conn].split(b'\n', 1)
    return json.loads(line)


def loop():
    global link
    for line in sys.stdin:
        command = json.loads(line)
        op, conn = command['op'], command.get('conn', 'a')
        if op == 'hello':
            if conn in socks:
                socks[conn].close()
            state = Path(os.environ['XDG_STATE_HOME']) / 'omp-workbench' / 'hub' / 'hub.json'
            socks[conn] = socket.socket(socket.AF_UNIX)
            socks[conn].settimeout(10)
            socks[conn].connect(json.loads(state.read_text())['socket'])
            buffers[conn] = b''
            socks[conn].sendall(json.dumps(command['hello']).encode() + b'\n')
            answer = frame(conn, 10)
        elif op == 'send':
            try:
                socks[conn].sendall(json.dumps(command['frame']).encode() + b'\n')
                answer = frame(conn, command.get('timeout', 10))
            except OSError:
                answer = {'eof': True}
        elif op == 'wait':
            answer = frame(conn, command.get('timeout', 3))
        elif op == 'handover':
            # the connection outlives this backend: a child inherits the sockets, the recorded process exits
            sys.stdout.flush()
            if os.fork() != 0:
                os._exit(0)
            os.close(lock)
            answer = {'handed': True, 'pid': os.getpid(), 'start_ticks': int(
                Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])}
        elif op == 'link_start':
            from workbench.backend.inbox_link import InboxLink
            link = InboxLink(marker['project_id'], environment=dict(os.environ),
                             timeout=command.get('timeout', 5.0), reconnect_interval=command.get('interval', 0.3))
            link.start()
            answer = {'started': True}
        elif op == 'link_call':
            outcome = link.call(command['call'], **command.get('fields', {}))
            answer = {'status': outcome.status, 'reason': outcome.reason, 'value': outcome.value}
        elif op == 'link_state':
            answer = {'state': link.state, 'last_reason': link.last_reason}
        else:
            break
        print(json.dumps(answer), flush=True)
    if link is not None:
        link.close()


loop()
'''


def start_ticks_of(pid: int) -> int | None:
    try:
        return proc_start_ticks(pid)
    except (OSError, IndexError, ValueError):
        return None


class Sandbox(HubSandbox):
    """``HubSandbox`` with a short private runtime dir (a long one falls back to ``/tmp/omp-workbench-<uid>``), WB
    projects, a registry and fake backends. Every process it starts is killed by exact identity at cleanup."""

    def __init__(self, case: unittest.TestCase):
        super().__init__(case)
        self.run = Path(tempfile.mkdtemp(prefix='wbr', dir='/dev/shm')).resolve()
        os.chmod(self.run, 0o700)
        self.runtime = self.run
        self.socket_dir = self.run / 'omp-workbench-hub'
        self.env['XDG_RUNTIME_DIR'] = str(self.run)
        self.peer_script = self.base / 'peer.py'
        self.peer_script.write_text(PEER)
        self.entries: list[dict] = []
        self.peers: list['Peer'] = []

    def cleanup(self) -> None:
        try:
            for peer in self.peers:
                peer.release()
            super().cleanup()
        finally:
            shutil.rmtree(self.run, ignore_errors=True)

    def project(self, name: str) -> tuple[Path, str]:
        root = self.base / 'work' / name
        root.mkdir(parents=True)
        root = root.resolve()
        initialize_project(resolve_project_context(root, self.env), confirm=lambda _root: True, interactive=True,
                           environment=self.env)
        return root, json.loads((root / '.wb' / 'project.json').read_text())['project_id']

    def write_registry(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.state / 'registry.json'
        path.write_text(json.dumps({'schema_version': 1, 'projects': self.entries}))
        os.chmod(path, 0o600)

    def register(self, root: Path, project_id: str, *, entry_root: Path | None = None) -> None:
        self.entries.append({'root': str(entry_root or root), 'project_id': project_id, 'name': root.name,
                             'registered_at': 1.0, 'source': 'add'})
        self.write_registry()

    def unregister(self, project_id: str) -> None:
        self.entries = [entry for entry in self.entries if entry['project_id'] != project_id]
        self.write_registry()

    def peer(self, root: Path) -> 'Peer':
        process = subprocess.Popen([sys.executable, str(self.peer_script), str(root)], env=self.env, cwd=self.base,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.processes.append(process)
        peer = Peer(process, self.case)
        self.peers.append(peer)
        first = peer.read()
        self.case.assertTrue(first.get('ready'), first)
        peer.pid, peer.start_ticks = first['pid'], first['start_ticks']
        return peer


class Peer:
    def __init__(self, process: subprocess.Popen, case: unittest.TestCase):
        self.process, self.case = process, case
        self.pid = self.start_ticks = 0
        self.extra: list[tuple[int, int]] = []  # (pid, start ticks) of handed-over children

    def read(self) -> dict:
        line = self.process.stdout.readline()
        if not line:
            raise AssertionError(f'peer ended: {self.process.stderr.read()}')
        return json.loads(line)

    def ask(self, **command) -> dict:
        self.process.stdin.write(json.dumps(command) + '\n')
        self.process.stdin.flush()
        return self.read()

    def hello(self, project_id: object, client: str = BACKEND, conn: str = 'a', **extra) -> dict:
        hello = {'v': 1, 'type': 'hello', 'client': client, **extra}
        if project_id is not ...:
            hello['project_id'] = project_id
        return self.ask(op='hello', conn=conn, hello=hello)

    def request(self, frame_id: str, kind: str, conn: str = 'a', **fields) -> dict:
        return self.ask(op='send', conn=conn, frame={'v': 1, 'id': frame_id, 'type': kind, **fields})

    def handover(self) -> dict:
        """The recorded backend process exits; a child (new pid) keeps the connections and continues here."""
        answer = self.ask(op='handover')
        self.extra.append((answer['pid'], answer['start_ticks']))
        return answer

    def release(self) -> None:
        """End this peer's stdin (a handed-over child leaves on EOF), then kill the child by exact identity."""
        for stream in (self.process.stdin,):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        for pid, ticks in self.extra:
            deadline = time.monotonic() + 3
            while start_ticks_of(pid) == ticks and time.monotonic() < deadline:
                time.sleep(0.02)
            if start_ticks_of(pid) == ticks:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class Case(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)

    def connect(self) -> Line:
        line = Line(self.box.record()['socket'])
        self.addCleanup(line.close)
        return line

    def two_projects(self):
        alpha, alpha_id = self.box.project('alpha')
        beta, beta_id = self.box.project('beta')
        self.box.register(alpha, alpha_id)
        self.box.register(beta, beta_id)
        return (alpha, alpha_id), (beta, beta_id)

    def assert_denied(self, answer: dict, what: str = '') -> None:
        self.assertEqual(answer, MISMATCH, what)

    def failure(self, answer: dict, request_id: str, reason: str) -> None:
        self.assertEqual((answer.get('id'), answer.get('ok'), answer.get('reason')), (request_id, False, reason),
                         answer)


def rewrite_record(root: Path, **reference) -> None:
    path = root / '.wb' / 'backend.json'
    record = json.loads(path.read_text())
    record['processes']['backend'].update(reference)
    staged = path.with_name('test-record')
    staged.write_text(json.dumps(record))
    staged.chmod(0o600)
    staged.replace(path)


def seed(projects: list[tuple[Path, str]], plan: dict[str, list[str]]) -> dict[str, list[str]]:
    """Requests made through the product inbox before the Hub starts (single writer): titles per project id."""
    from workbench.hub.inbox.service import InboxService, Principal, ProjectRef
    service = InboxService()
    try:
        service.set_projects(ProjectRef(project_id, root, root.name) for root, project_id in projects)
        return {project_id: [service.submit(Principal.owner(), project_id, title, f'BODY-{title}')['request_id']
                             for title in titles] for project_id, titles in plan.items()}
    finally:
        service.close()


class GrantTests(Case):
    def test_only_the_recorded_backend_of_a_registered_project_is_granted_and_forgers_never_are(self):
        (alpha, alpha_id), (beta, beta_id) = self.two_projects()
        gamma, gamma_id = self.box.project('gamma')  # initialized but never registered
        hub = self.box.start_hub()
        peer_a, peer_b, peer_g = self.box.peer(alpha), self.box.peer(beta), self.box.peer(gamma)
        for peer, root in ((peer_a, alpha), (peer_b, beta)):
            record = json.loads((root / '.wb' / 'backend.json').read_text())
            self.assertEqual(record['processes']['backend'],
                             {'pid': peer.pid, 'start_ticks': proc_start_ticks(peer.pid)})
        self.assertNotIn(os.getpid(), (peer_a.pid, peer_b.pid))

        # same uid, not the recorded process: right id, wrong process
        line = self.connect()
        line.send({'v': 1, 'type': 'hello', 'client': BACKEND, 'project_id': alpha_id})
        self.assert_denied(line.recv(), 'a same-uid test process with the right project id')
        self.assertTrue(line.closed())
        # recorded backend of another project
        self.assert_denied(peer_a.hello(beta_id), 'alpha claims beta')
        self.assert_denied(peer_b.hello(alpha_id), 'beta claims alpha')
        # unregistered ids: a random uuid, and a real initialized project that is not in the registry
        self.assert_denied(peer_a.hello(str(uuid.uuid4())), 'random id')
        self.assert_denied(peer_g.hello(gamma_id), 'a running backend of an unregistered project')
        # no project id / a project id without the backend name: nothing is granted
        self.assert_denied(peer_a.hello(...), 'backend name without project_id')
        self.assert_denied(peer_a.hello(None), 'backend name with a null project_id')
        for client in ('workbench-cli', 'workbench-hub', 'workbench-ui', 'workbench-backend ', 'WORKBENCH-BACKEND'):
            self.assert_denied(peer_a.hello(alpha_id, client=client), f'client {client!r} with the right id')

        # none of the above hurt the real ones, and the Hub never stopped
        for peer, project_id in ((peer_a, alpha_id), (peer_b, beta_id)):
            self.assertEqual(peer.hello(project_id)['type'], 'welcome')
            listed = peer.request('l1', 'inbox_list')
            self.assertEqual((listed['ok'], listed['requests']), (True, []), listed)
        self.assertIsNone(hub.poll())

    def test_a_registry_entry_pointing_at_another_projects_root_cannot_borrow_that_backend(self):
        (alpha, alpha_id), (beta, beta_id) = self.two_projects()
        gamma, gamma_id = self.box.project('gamma')
        self.box.entries = []
        self.box.register(alpha, alpha_id, entry_root=gamma)  # alpha's id, gamma's root (a running backend)
        self.box.register(beta, beta_id)
        self.box.start_hub()
        peer_a, peer_g, peer_b = self.box.peer(alpha), self.box.peer(gamma), self.box.peer(beta)
        self.assert_denied(peer_g.hello(alpha_id), 'gamma backend, entry id of alpha but root of gamma')
        self.assert_denied(peer_a.hello(alpha_id), 'alpha backend, entry rooted elsewhere')
        self.assert_denied(peer_g.hello(gamma_id), 'gamma is not registered under its own id')
        self.assertEqual(peer_b.hello(beta_id)['type'], 'welcome')

    def test_a_record_that_does_not_name_the_peer_process_and_ticks_is_refused_and_restoring_it_grants(self):
        (alpha, alpha_id), _ = self.two_projects()
        self.box.start_hub()
        peer = self.box.peer(alpha)
        good = (alpha / '.wb' / 'backend.json').read_bytes()
        for label, change in (('start_ticks + 1', {'start_ticks': peer.start_ticks + 1}),
                              ('start_ticks - 1', {'start_ticks': peer.start_ticks - 1}),
                              ('start_ticks 0', {'start_ticks': 0}),
                              ('pid of the test process', {'pid': os.getpid(),
                                                           'start_ticks': proc_start_ticks(os.getpid())}),
                              ('pid 1', {'pid': 1, 'start_ticks': proc_start_ticks(1)}),
                              ('peer pid with the test process ticks', {'start_ticks': proc_start_ticks(os.getpid())})):
            with self.subTest(label):
                rewrite_record(alpha, **change)
                self.assert_denied(peer.hello(alpha_id), label)
        (alpha / '.wb' / 'backend.json').write_bytes(b'{not json')
        self.assert_denied(peer.hello(alpha_id), 'a corrupt record')
        (alpha / '.wb' / 'backend.json').unlink()
        self.assert_denied(peer.hello(alpha_id), 'no record')
        staged = alpha / '.wb' / 'restore'
        staged.write_bytes(good)
        staged.chmod(0o600)
        staged.replace(alpha / '.wb' / 'backend.json')
        self.assertEqual(peer.hello(alpha_id)['type'], 'welcome', 'the refusals were about the record, not the peer')

    def test_backend_mismatch_function_compares_pid_and_start_ticks_against_the_recorded_backend(self):
        from workbench.hub import registry
        from workbench.hub.server import backend_mismatch
        alpha, alpha_id = self.box.project('alpha')
        beta, beta_id = self.box.project('beta')
        peer = self.box.peer(alpha)
        entry = registry.Entry(str(alpha), alpha_id, 'alpha', 1.0, 'add')
        other = registry.Entry(str(beta), beta_id, 'beta', 1.0, 'add')
        self.assertIsNone(backend_mismatch([entry], alpha_id, (peer.pid, peer.start_ticks)))
        for label, argument in (('recycled ticks (+1)', (peer.pid, peer.start_ticks + 1)),
                                ('recycled ticks (-1)', (peer.pid, peer.start_ticks - 1)),
                                ('ticks unreadable', (peer.pid, None)), ('peer unreadable', None),
                                ('other pid, right ticks', (peer.pid + 1, peer.start_ticks)),
                                ('test pid', (os.getpid(), proc_start_ticks(os.getpid())))):
            with self.subTest(label):
                self.assertIsNotNone(backend_mismatch([entry], alpha_id, argument))
        self.assertIsNotNone(backend_mismatch([], alpha_id, (peer.pid, peer.start_ticks)), 'unregistered')
        self.assertIsNotNone(backend_mismatch([other], alpha_id, (peer.pid, peer.start_ticks)), 'other project only')
        self.assertIsNotNone(backend_mismatch([entry], beta_id, (peer.pid, peer.start_ticks)), 'other id asked')
        # the same process after it ended: the recorded backend is gone, its pid may be reused
        peer.process.stdin.close()
        self.assertTrue(wait_until(lambda: peer.process.poll() is not None, 5))
        self.assertIsNotNone(backend_mismatch([entry], alpha_id, (peer.pid, peer.start_ticks)))


class EveryRequestTests(Case):
    def test_a_connection_that_outlives_its_backend_loses_the_principal_on_the_next_request(self):
        (alpha, alpha_id), _ = self.two_projects()
        hub = self.box.start_hub()
        peer = self.box.peer(alpha)
        self.assertEqual(peer.hello(alpha_id)['type'], 'welcome')
        self.assertTrue(peer.request('l1', 'inbox_list')['ok'])
        old_pid = peer.pid
        child = peer.handover()  # the recorded pid exits; the connection is now held by another process
        self.assertNotEqual(child['pid'], old_pid)
        self.assertTrue(wait_until(lambda: not alive(old_pid), 5))
        refused = peer.request('l2', 'inbox_list')
        self.failure(refused, 'l2', 'principal_mismatch')
        self.assertNotIn('requests', refused)
        self.assertEqual(peer.request('l3', 'inbox_list'), {'eof': True}, 'the Hub closed the connection')
        # the new process cannot take the principal back by saying hello either: it is not the recorded backend
        self.assert_denied(peer.hello(alpha_id), 'handed-over child, record names the dead parent')
        self.assertIsNone(hub.poll())

    def test_a_replaced_backend_takes_the_principal_and_the_old_connection_loses_it(self):
        (alpha, alpha_id), _ = self.two_projects()
        self.box.start_hub()
        old = self.box.peer(alpha)
        self.assertEqual(old.hello(alpha_id)['type'], 'welcome')
        old.handover()
        self.assertTrue(wait_until(lambda: not alive(old.pid), 5))
        new = self.box.peer(alpha)  # the lock is free again: a restarted backend with a new record
        self.assertNotEqual(new.pid, old.pid)
        # the new backend has not said hello yet: the old connection is already not the recorded backend
        self.failure(old.request('o1', 'inbox_list'), 'o1', 'principal_mismatch')
        self.assertEqual(new.hello(alpha_id)['type'], 'welcome')
        self.assertTrue(new.request('n1', 'inbox_list')['ok'], 'the new backend is unaffected by the old refusal')

    def test_a_new_hello_of_the_replacement_closes_a_still_open_old_connection(self):
        (alpha, alpha_id), _ = self.two_projects()
        self.box.start_hub()
        old = self.box.peer(alpha)
        self.assertEqual(old.hello(alpha_id)['type'], 'welcome')
        old.handover()
        self.assertTrue(wait_until(lambda: not alive(old.pid), 5))
        new = self.box.peer(alpha)
        self.assertEqual(new.hello(alpha_id)['type'], 'welcome')
        reply = old.request('o1', 'inbox_list')
        self.assertTrue(reply == {'eof': True} or reply.get('reason') == 'principal_mismatch', reply)
        self.assertEqual(old.request('o2', 'inbox_list'), {'eof': True})
        self.assertTrue(new.request('n1', 'inbox_list')['ok'])

    def test_the_registry_and_the_record_are_read_again_for_each_request(self):
        (alpha, alpha_id), (beta, beta_id) = self.two_projects()
        self.box.start_hub()
        peer_a, peer_b = self.box.peer(alpha), self.box.peer(beta)
        for peer, project_id in ((peer_a, alpha_id), (peer_b, beta_id)):
            self.assertEqual(peer.hello(project_id)['type'], 'welcome')
        self.box.unregister(alpha_id)
        self.failure(peer_a.request('a1', 'inbox_list'), 'a1', 'principal_mismatch')
        self.assertEqual(peer_a.request('a2', 'inbox_list'), {'eof': True})
        self.assertTrue(peer_b.request('b1', 'inbox_list')['ok'], 'only the unregistered project lost its principal')
        # a tampered record (ticks that are not the peer's) also ends the principal
        rewrite_record(beta, start_ticks=peer_b.start_ticks + 1)
        self.failure(peer_b.request('b2', 'inbox_list'), 'b2', 'principal_mismatch')

    def test_a_withdrawn_principal_does_not_change_the_inbox(self):
        (alpha, alpha_id), _ = self.two_projects()
        made = seed([(alpha, alpha_id)], {alpha_id: ['first']})
        (request_id,) = made[alpha_id]
        self.box.start_hub()
        peer = self.box.peer(alpha)
        self.assertEqual(peer.hello(alpha_id)['type'], 'welcome')
        before = peer.request('s1', 'inbox_show', request_id=request_id)['request']
        self.assertEqual(before['state'], 'NEW')
        good = (alpha / '.wb' / 'backend.json').read_bytes()
        rewrite_record(alpha, start_ticks=peer.start_ticks + 1)
        refused = peer.request('r1', 'inbox_review', request_id=request_id, action='begin_review')
        self.failure(refused, 'r1', 'principal_mismatch')
        self.assertEqual(peer.request('r2', 'inbox_review', request_id=request_id, action='begin_review'),
                         {'eof': True})
        staged = alpha / '.wb' / 'restore'
        staged.write_bytes(good)
        staged.chmod(0o600)
        staged.replace(alpha / '.wb' / 'backend.json')
        self.assertEqual(peer.hello(alpha_id, conn='again')['type'], 'welcome')
        after = peer.request('s2', 'inbox_show', conn='again', request_id=request_id)['request']
        self.assertEqual((after['state'], after['version']), (before['state'], before['version']),
                         'the refused begin_review changed nothing')


class OneConnectionPerProjectTests(Case):
    def test_a_new_verified_connection_replaces_the_old_one_of_that_project_only(self):
        (alpha, alpha_id), (beta, beta_id) = self.two_projects()
        self.box.start_hub()
        peer_a, peer_b = self.box.peer(alpha), self.box.peer(beta)
        self.assertEqual(peer_a.hello(alpha_id, conn='first')['type'], 'welcome')
        self.assertEqual(peer_b.hello(beta_id)['type'], 'welcome')
        self.assertTrue(peer_a.request('f1', 'inbox_list', conn='first')['ok'])
        self.assertEqual(peer_a.hello(alpha_id, conn='second')['type'], 'welcome')
        self.assertEqual(peer_a.ask(op='wait', conn='first', timeout=5), {'eof': True}, 'the old one is closed')
        self.assertEqual(peer_a.request('f2', 'inbox_list', conn='first'), {'eof': True})
        self.assertTrue(peer_a.request('s1', 'inbox_list', conn='second')['ok'])
        self.assertTrue(peer_b.request('b1', 'inbox_list')['ok'], 'another project is untouched')
        self.assertEqual(peer_a.hello(alpha_id, conn='third')['type'], 'welcome')
        self.assertEqual(peer_a.ask(op='wait', conn='second', timeout=5), {'eof': True})
        self.assertTrue(peer_a.request('t1', 'inbox_list', conn='third')['ok'])

    def test_a_refused_hello_never_displaces_the_working_connection(self):
        (alpha, alpha_id), (beta, beta_id) = self.two_projects()
        self.box.start_hub()
        peer = self.box.peer(alpha)
        self.assertEqual(peer.hello(alpha_id, conn='good')['type'], 'welcome')
        self.assert_denied(peer.hello(beta_id, conn='bad'), 'wrong project id from the right process')
        line = self.connect()
        line.send({'v': 1, 'type': 'hello', 'client': BACKEND, 'project_id': alpha_id})
        self.assert_denied(line.recv(), 'a forger for the same project')
        self.assertTrue(peer.request('g1', 'inbox_list', conn='good')['ok'])

    def test_backend_connections_are_not_counted_in_the_pool_and_never_idle_out(self):
        (alpha, alpha_id), (beta, beta_id) = self.two_projects()
        self.box.start_hub('--idle-timeout', '1.5')
        peer_a, peer_b = self.box.peer(alpha), self.box.peer(beta)
        self.assertEqual(peer_a.hello(alpha_id)['type'], 'welcome')
        self.assertEqual(peer_b.hello(beta_id)['type'], 'welcome')
        from workbench.contracts.hub_v1 import MAX_CONNECTIONS
        locals_ = []
        for _ in range(MAX_CONNECTIONS):
            line = self.connect()
            self.assertEqual(line.hello()['type'], 'welcome', 'the pool is full of local connections only')
            locals_.append(line)
        extra = self.connect()
        self.assertEqual(extra.recv(), {'v': 1, 'type': 'reject', 'reason': 'connection_limit'})
        for peer, name in ((peer_a, 'a'), (peer_b, 'b')):
            self.assertTrue(peer.request(f'{name}1', 'inbox_list')['ok'])
        self.assertTrue(locals_[0].closed(8), 'a local connection idles out')
        time.sleep(0.5)
        for peer, name in ((peer_a, 'a'), (peer_b, 'b')):
            self.assertTrue(peer.request(f'{name}2', 'inbox_list')['ok'], 'a backend connection has no idle timeout')


class OpPermissionTests(Case):
    BACKEND_OPS = ('inbox_list', 'inbox_show', 'inbox_review', 'inbox_link_check', 'inbox_link_commit',
                   'inbox_delivery')
    NOT_FOR_BACKENDS = ('status', 'list', 'stop_request', 'stop_confirm', 'review_mode', 'power_on',
                        'power_off_request', 'power_off_confirm', 'pause', 'resume', 'confirm_boot', 'attach',
                        'task_status', 'inbox_notice', 'cancel_task', 'nonsense')

    def test_a_local_connection_gets_no_backend_op_and_cannot_upgrade_itself_with_a_second_hello(self):
        (alpha, alpha_id), _ = self.two_projects()
        made = seed([(alpha, alpha_id)], {alpha_id: ['a']})
        self.box.start_hub()
        line = self.connect()
        self.assertEqual(line.hello()['type'], 'welcome')
        for number, op in enumerate(self.BACKEND_OPS):
            with self.subTest(op=op):
                line.send({'v': 1, 'id': f'b{number}', 'type': op, 'request_id': made[alpha_id][0]})
                self.failure(line.recv(), f'b{number}', 'unsupported_type')
        # a second hello claiming the backend identity is not a request; it must not grant anything
        line.send({'v': 1, 'type': 'hello', 'client': BACKEND, 'project_id': alpha_id})
        answer = line.recv()
        self.assertNotEqual(answer and answer.get('type'), 'welcome', answer)
        self.assertNotEqual(answer and answer.get('ok'), True, answer)
        if answer is not None and not line.closed(1):
            line.send({'v': 1, 'id': 'x', 'type': 'inbox_list'})
            reply = line.recv()
            self.assertFalse(reply and reply.get('ok'), reply)

    def test_a_backend_principal_gets_nothing_a_local_or_reserved_or_ui_op_and_the_hub_keeps_running(self):
        (alpha, alpha_id), _ = self.two_projects()
        hub = self.box.start_hub()
        peer = self.box.peer(alpha)
        self.assertEqual(peer.hello(alpha_id)['type'], 'welcome')
        for number, op in enumerate(self.NOT_FOR_BACKENDS):
            with self.subTest(op=op):
                self.failure(peer.request(f'o{number}', op, root=str(alpha), token='t', set='manual'), f'o{number}',
                             'unsupported_type')
        self.assertIsNone(hub.poll(), 'no stop was offered, confirmed or executed')
        self.assertTrue(peer.request('l1', 'inbox_list')['ok'])
        # the same ops are still served to a local connection (control)
        line = self.connect()
        line.hello()
        self.assertTrue(line.request('s1', 'status')['ok'])

    def test_a_project_backend_sees_and_changes_only_its_own_projects_requests(self):
        (alpha, alpha_id), (beta, beta_id) = self.two_projects()
        made = seed([(alpha, alpha_id), (beta, beta_id)], {alpha_id: ['mine'], beta_id: ['theirs']})
        (mine,), (theirs,) = made[alpha_id], made[beta_id]
        self.box.start_hub()
        peer_a, peer_b = self.box.peer(alpha), self.box.peer(beta)
        self.assertEqual(peer_a.hello(alpha_id)['type'], 'welcome')
        self.assertEqual(peer_b.hello(beta_id)['type'], 'welcome')
        before = peer_b.request('b0', 'inbox_show', request_id=theirs)['request']
        for number, (op, fields) in enumerate((('inbox_show', {}), ('inbox_review', {'action': 'begin_review'}),
                                               ('inbox_review', {'action': 'close', 'outcome': 'failed',
                                                                 'summary': 's'}),
                                               ('inbox_link_check', {}),
                                               ('inbox_delivery', {'delivery_id': str(uuid.uuid4()),
                                                                   'outcome': 'delivered'}))):
            with self.subTest(op=op, fields=fields):
                reply = peer_a.request(f'x{number}', op, request_id=theirs, **fields)
                self.failure(reply, f'x{number}', 'not_found')
                self.assertNotIn('BODY-theirs', json.dumps(reply))
        listed = peer_a.request('l1', 'inbox_list', project_id=beta_id, states=['NEW', 'CLOSED'])
        self.assertEqual([item['request_id'] for item in listed['requests']], [mine], listed)
        after = peer_b.request('b1', 'inbox_show', request_id=theirs)['request']
        self.assertEqual((after['state'], after['version'], after['body']),
                         (before['state'], before['version'], 'BODY-theirs'), 'beta unchanged by alpha')
        self.assertEqual(peer_a.request('m1', 'inbox_show', request_id=mine)['request']['body'], 'BODY-mine')


class ReconnectTests(Case):
    def link_state(self, peer: Peer, wanted: str, timeout: float = 12.0) -> bool:
        return wait_until(lambda: peer.ask(op='link_state')['state'] == wanted, timeout, 0.1)

    def test_without_a_hub_the_link_is_unavailable_starts_none_and_connects_when_one_appears(self):
        (alpha, alpha_id), _ = self.two_projects()
        peer = self.box.peer(alpha)
        peer.ask(op='link_start', interval=0.3, timeout=3.0)
        none = peer.ask(op='link_call', call='inbox_list')
        self.assertEqual(none['status'], 'unavailable', none)
        self.assertFalse(self.box.record_path.exists(), 'the backend did not start a Hub')
        self.assertFalse(self.box.socket_dir.exists() and any(self.box.socket_dir.iterdir()))
        self.assertEqual(peer.ask(op='link_state')['state'], 'disconnected')
        self.box.start_hub()
        self.assertTrue(self.link_state(peer, 'connected'), 'the 0.3 s reconnect loop found the new Hub')
        listed = peer.ask(op='link_call', call='inbox_list')
        self.assertEqual((listed['status'], listed['value'].get('requests')), ('ok', []), listed)

    def test_after_a_hub_restart_the_same_backend_gets_the_principal_again_on_a_new_connection(self):
        (alpha, alpha_id), _ = self.two_projects()
        made = seed([(alpha, alpha_id)], {alpha_id: ['kept']})
        peer = self.box.peer(alpha)
        hub = self.box.start_hub()
        peer.ask(op='link_start', interval=0.3, timeout=3.0)
        first = peer.ask(op='link_call', call='inbox_show', fields={'request_id': made[alpha_id][0]})
        self.assertEqual((first['status'], first['value']['request']['title']), ('ok', 'kept'), first)
        hub.send_signal(signal.SIGTERM)
        self.assertEqual(hub.wait(timeout=10), 0)
        self.assertTrue(self.link_state(peer, 'disconnected', 6), 'the lost connection is noticed')
        down = peer.ask(op='link_call', call='inbox_list')
        self.assertEqual(down['status'], 'unavailable', down)
        self.box.start_hub()
        self.assertTrue(self.link_state(peer, 'connected'), 'reconnected with no backend restart')
        second = peer.ask(op='link_call', call='inbox_show', fields={'request_id': made[alpha_id][0]})
        self.assertEqual((second['status'], second['value']['request']['title']), ('ok', 'kept'), second)

    def test_a_link_in_a_process_that_is_not_the_recorded_backend_is_never_granted(self):
        (alpha, alpha_id), _ = self.two_projects()
        self.box.start_hub()
        from workbench.backend.inbox_link import InboxLink
        link = InboxLink(alpha_id, environment=dict(self.box.env), timeout=3.0, reconnect_interval=0.3)
        self.addCleanup(link.close)
        outcome = link.call('inbox_list')
        self.assertEqual((outcome.status, outcome.reason), ('unavailable', 'principal_mismatch'), outcome)
        self.assertEqual(link.state, 'disconnected')


if __name__ == '__main__':
    unittest.main()
