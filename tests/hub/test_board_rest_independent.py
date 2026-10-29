"""WBX-B5 independent verification (test_designer owned): the Board agent on the internal REST listener and the
``wb-inbox`` skill copy.

Expected values come from docs/features/web-board-ux: SPEC "Board agent 전환" and "외부 요청 API", BRIEF WBX-D11/D24/D28,
ADR-0006, AC-16 and PROBE-BOARD-SKILL (the overlay JSON is parsed from that document, the isolation list from
``omp_bridge/omp-isolation.yml``), never from the product's helpers or from the worker's tests.

Seams: S2 (a real key store, inbox and ``rest.InternalListener`` on a loopback port, stdlib client), the in-process
``BoardLauncher`` over a real key store (skill copy, overlay, env, isolation verdicts, the copied ``wb_inbox.py`` run as
a subprocess), and S3 (a real ``wb _hub --foreground`` with the fake ``omp`` of ``fixtures/fake_omp_board.py``, read
through ``/proc`` and the Hub's own files). Run with ``discover -s tests/hub -p 'test_board_rest*.py'``.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.web_sandbox import tcp_listening, wait_until  # noqa: E402
from support.rest_client import RestClient  # noqa: E402
from support.rest_hub import RestHub  # noqa: E402
from test_board_independent import Rig  # noqa: E402
from test_board_launch import LauncherCase  # noqa: E402
import test_web_control_independent as twc  # noqa: E402

from workbench.hub import auth, rest  # noqa: E402
from workbench.hub.board import launch  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SKILL_SOURCE = REPO / 'skills' / 'wb-inbox'
PROBE_DOC = REPO / 'docs' / 'features' / 'web-board-ux' / 'PROBE-BOARD-SKILL.md'
ROLE = REPO / 'omp_bridge' / 'board' / 'ROLE.md'
USER_KEY = 'wbk_aaaaaaaaaa_' + 'U' * 43
USER_URL = 'http://203.0.113.7:8780'
OLD_KEY = 'wbk_bbbbbbbbbb_' + 'O' * 43
INHERITED = {'WB_API_KEY': USER_KEY, 'WB_HUB_URL': USER_URL, 'WB_BOARD_MCP_KEY': OLD_KEY}


def environ_of(pid: int) -> dict[str, str]:
    raw = Path(f'/proc/{pid}/environ').read_bytes()
    return dict(item.decode(errors='replace').split('=', 1) for item in raw.split(b'\0') if b'=' in item)


def exchange(port: int, data: bytes, timeout: float = 5.0) -> bytes:
    """One raw TCP exchange with the listener: what the server answered before it closed (or timed out)."""
    with socket.create_connection(('127.0.0.1', port), timeout=timeout) as connection:
        connection.sendall(data)
        chunks = []
        try:
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except (socket.timeout, ConnectionResetError):
            pass
        return b''.join(chunks)


def status_of(raw: bytes) -> int:
    return int(raw.split(b' ', 2)[1]) if raw.startswith(b'HTTP/') else 0


def tree(root: Path) -> dict[str, bytes | None]:
    """Every path under ``root`` (relative) -> bytes (None for a directory); links are not followed."""
    found: dict[str, bytes | None] = {}
    for path in sorted(root.rglob('*')):
        key = str(path.relative_to(root))
        if path.is_symlink():
            found[key] = b'->' + os.readlink(path).encode()
        elif path.is_dir():
            found[key] = None
        else:
            found[key] = path.read_bytes()
    return found


# --- S2: the internal listener ------------------------------------------------------------------------------------

class InternalAdversarialCase(unittest.TestCase):
    """The internal listener with its own key store (own pending last-use map) and a 0.2 s flush."""

    def setUp(self):
        self.hub = RestHub(self, flush_interval=0.2)
        self.own = auth.ApiKeyStore(self.hub.db)
        self.listener = rest.InternalListener(self.own, self.hub.api, clock=lambda: self.hub.now[0],
                                              flush_interval=0.2)
        self.hub.run(self.listener.start())
        self.addCleanup(lambda: self.hub.run(self.listener.stop()))
        self.port = self.listener.port
        self.system = self.own.rotate_system_key('a-000000000001', now=self.hub.now[0]).raw

    def board(self, key='default', **kwargs) -> RestClient:
        return RestClient('127.0.0.1', self.port, self.system if key == 'default' else key, **kwargs)

    def last_used(self, key: str):
        rows = self.hub.db.query('SELECT last_used_at FROM api_keys WHERE key_id = ?', (key[4:14],))
        return rows[0][0] if rows else 'no row'

    def requests_stored(self) -> int:
        return sum(count for (count,) in self.hub.inbox_rows('SELECT count(*) FROM requests'))


class RefusedKeyTests(InternalAdversarialCase):
    """WBX-D24: only the current system key; a refused key is 401 and never marks a last use."""

    def test_every_other_kind_of_key_is_401_and_marks_no_last_use(self):
        owner_editor = self.hub.key('owner-e', 'editor')
        owner_requester = self.hub.key('owner-r', 'requester')
        scoped = self.hub.key('owner-s', 'editor', {'projects': [self.hub.a]})
        revoked = self.hub.key('owner-x', 'editor')
        self.hub.keys.revoke(revoked[4:14], now=self.hub.now[0])
        expiring = self.hub.key('owner-t', 'editor', expires_days=1)
        self.hub.now[0] += 2 * 86400  # past the expiry; the system key has none
        refused = {'owner editor': owner_editor, 'owner requester': owner_requester, 'scoped editor': scoped,
                   'revoked': revoked, 'expired': expiring}
        for label, key in refused.items():
            with self.subTest(key=label):
                for path in ('/requests', '/projects', '/nothing'):
                    reply = self.board(key).get(path)
                    self.assertEqual(reply.status, 401, (path, reply.body))
                    self.assertEqual(reply.json, {'error': {'code': 'unauthorized', 'message': 'unauthorized'}})
                    self.assertEqual(reply.header('www-authenticate'), 'Bearer realm="workbench-hub"')
                reply = self.board(key).post('/requests', {'project_id': self.hub.a, 'title': 't', 'body': 'b'})
                self.assertEqual(reply.status, 401)
        time.sleep(1.0)  # five flush intervals
        for label, key in refused.items():
            with self.subTest(last_use=label):
                self.assertIsNone(self.last_used(key))
        self.assertEqual(self.requests_stored(), 0)  # nothing was created
        # control: the same owner key is valid on the external listener and its last use is then written
        self.assertEqual(self.hub.rest(owner_editor).get('/requests').status, 200)
        self.assertTrue(wait_until(lambda: self.last_used(owner_editor) is not None, 5))

    def test_the_system_key_does_get_its_last_use_marked(self):
        self.assertIsNone(self.last_used(self.system))
        self.assertEqual(self.board().get('/requests').status, 200)
        self.assertTrue(wait_until(lambda: self.last_used(self.system) is not None, 5))

    def test_a_replaced_system_key_is_401_on_both_listeners_and_a_new_one_works(self):
        old = self.system
        new = self.own.rotate_system_key('a-000000000001', now=self.hub.now[0]).raw
        self.assertNotEqual(old, new)
        self.assertEqual(self.last_used(old), 'no row')  # the previous row is deleted (the name is unique)
        self.assertEqual(self.board(old).get('/requests').status, 401)
        self.assertEqual(self.hub.rest(old).get('/requests').status, 401)
        self.assertEqual(self.board(new).get('/requests').status, 200)
        self.assertEqual(self.hub.rest(new).get('/requests').status, 200)  # the external one takes it too

    def test_malformed_or_ambiguous_credentials_are_401(self):
        key = self.system
        cases = {
            'no header': {'Authorization': None},
            'cookie only': {'Authorization': None, 'Cookie': 'wb_hub_session=abc; wb_hub_login=def'},
            'two headers': {'Authorization': [f'Bearer {key}', f'Bearer {key}']},
            'basic scheme': {'Authorization': f'Basic {key}'},
            'bare key': {'Authorization': key},
            'key plus junk': {'Authorization': f'Bearer {key} extra'},
            'truncated key': {'Authorization': f'Bearer {key[:-1]}'},
            'altered last char': {'Authorization': f'Bearer {key[:-1]}{"A" if key[-1] != "A" else "B"}'},
            'empty bearer': {'Authorization': 'Bearer '},
        }
        for label, headers in cases.items():
            with self.subTest(case=label):
                reply = self.board(None).get('/requests', headers=headers)
                self.assertEqual(reply.status, 401, reply.body)
        # a cookie next to the right key changes nothing
        self.assertEqual(self.board().get('/requests', headers={'Cookie': 'wb_hub_session=abc'}).status, 200)

    def test_an_unknown_rest_path_is_401_without_a_key_and_404_not_found_with_the_system_key(self):
        for path in ('/api/v1/nothing', '/api/v1', '/api/v1/', '/api/v1/requests/x/y/z'):
            with self.subTest(path=path):
                self.assertEqual(self.board(None).raw('GET', path).status, 401)
                reply = self.board().raw('GET', path)
                self.assertEqual(reply.status, 404, reply.body)
                self.assertEqual(reply.json['error']['code'], 'not_found')


class RoutesAndHostTests(InternalAdversarialCase):
    def test_nothing_but_the_rest_api_answers_whatever_the_method_or_spelling(self):
        client = self.board()
        paths = ('/mcp', '/mcp/', '/MCP', '//mcp', '/./mcp', '/%6dcp', '/mcp?x=1', '/skill', '/skill/',
                 '/skill/README.md', '/skill/wb-inbox/SKILL.md', '/skill/wb-inbox/wb_inbox.py',
                 '/skill/wb-inbox/.env', '/skill/wb-inbox/.env.example', '/', '/login', '/logout', '/dashboard',
                 '/projects', '/requests', '/board', '/settings', '/settings/keys', '/settings/audit',
                 '/static/base.css', '/api/badge', '/api/dashboard', '/ws/board', '/ws', '/favicon.ico',
                 '/api/v2/requests', '/api/v1x')
        for method in ('GET', 'POST', 'HEAD', 'OPTIONS', 'PUT', 'DELETE', 'PATCH'):
            for path in paths:
                with self.subTest(method=method, path=path):
                    body = b'{}' if method in ('POST', 'PUT', 'PATCH') else None
                    self.assertEqual(client.raw(method, path, body, {'Content-Type': 'application/json'}).status, 404)
        for path in ('/mcp', '/skill/wb-inbox/SKILL.md', '/login', '/ws/board'):  # not even authentication
            with self.subTest(keyless=path):
                self.assertEqual(self.board(None).raw('GET', path).status, 404)

    def test_a_websocket_upgrade_to_the_board_socket_is_404_not_101(self):
        upgrade = {'Connection': 'Upgrade', 'Upgrade': 'websocket', 'Sec-WebSocket-Version': '13',
                   'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ=='}
        for origin in (None, f'http://127.0.0.1:{self.port}'):
            with self.subTest(origin=origin):
                headers = dict(upgrade, **({'Origin': origin} if origin else {}))
                self.assertEqual(self.board().raw('GET', '/ws/board', None, headers).status, 404)

    def test_other_methods_on_a_rest_path_are_405_and_there_is_no_cors_preflight(self):
        reply = self.board().raw('DELETE', '/api/v1/requests')
        self.assertEqual(reply.status, 405)
        self.assertIn('GET', reply.header('allow') or '')
        self.assertEqual(self.board().raw('OPTIONS', '/api/v1/requests').status, 405)

    def test_host_is_exactly_this_listener(self):
        bad = [f'localhost:{self.port}', f'LOCALHOST:{self.port}', f'127.0.0.1:{self.port + 1}', '127.0.0.1',
               f'127.0.0.1:0{self.port}', f'127.0.0.1.:{self.port}', f'user@127.0.0.1:{self.port}',
               f'127.0.0.1:{self.port}@evil.example', f'127.0.0.1:{self.port},evil.example', 'evil.example',
               f'[::1]:{self.port}', f'127.0.0.2:{self.port}', f'0.0.0.0:{self.port}']
        for host in bad:
            with self.subTest(host=host):
                self.assertEqual(self.board(host_header=host).get('/requests').status, 421)
        self.assertEqual(self.board(host_header=f'127.0.0.1:{self.port}').get('/requests').status, 200)
        empty = f'GET /api/v1/requests HTTP/1.1\r\nHost:\r\nAuthorization: Bearer {self.system}\r\nConnection: close\r\n\r\n'
        self.assertEqual(status_of(exchange(self.port, empty.encode())), 421)

    def test_two_host_headers_and_a_missing_host_are_refused(self):
        for first, second in ((f'127.0.0.1:{self.port}', 'evil.example'), ('evil.example', f'127.0.0.1:{self.port}')):
            request = (f'GET /api/v1/requests HTTP/1.1\r\nHost: {first}\r\nHost: {second}\r\n'
                       f'Authorization: Bearer {self.system}\r\nConnection: close\r\n\r\n').encode()
            self.assertIn(status_of(exchange(self.port, request)), (400, 421), (first, second))
        request = f'GET /api/v1/requests HTTP/1.0\r\nAuthorization: Bearer {self.system}\r\n\r\n'.encode()
        self.assertEqual(status_of(exchange(self.port, request)), 421)

    def test_origin_must_be_this_listeners_own_when_present(self):
        own = f'http://127.0.0.1:{self.port}'
        self.assertEqual(self.board().get('/requests', headers={'Origin': own}).status, 200)
        foreign = ('null', 'http://evil.example', f'http://localhost:{self.port}', f'http://127.0.0.1:{self.port + 1}',
                   'http://127.0.0.1', f'https://127.0.0.1:{self.port}', f'{own}.evil.example')
        for origin in foreign:
            with self.subTest(origin=origin):
                reply = self.board().get('/requests', headers={'Origin': origin})
                self.assertEqual(reply.status, 403)
                self.assertEqual(reply.json['error']['code'], 'origin_mismatch')
                reply = self.board().post('/requests', {'project_id': self.hub.a, 'title': 't', 'body': 'b'},
                                          headers={'Origin': origin})
                self.assertEqual(reply.status, 403)
        self.assertEqual(self.requests_stored(), 0)

    def test_responses_carry_the_security_headers_even_for_refusals(self):
        replies = (self.board().get('/requests'), self.board(None).get('/requests'),
                   self.board().raw('GET', '/api/v1/nothing'), self.board().raw('GET', '/mcp'),
                   self.board(host_header='evil.example').get('/requests'))
        for reply in replies:
            with self.subTest(status=reply.status):
                self.assertEqual(reply.header('x-content-type-options'), 'nosniff')
        for reply in replies[:3]:
            self.assertEqual(reply.header('cache-control'), 'no-store')


class BindAndBodyTests(InternalAdversarialCase):
    def test_the_listener_is_on_127_0_0_1_only(self):
        mine = [(address, port) for address, port in tcp_listening(os.getpid()) if port == self.port]
        self.assertEqual(mine, [('127.0.0.1', self.port)])
        for address, family in (('127.0.0.2', socket.AF_INET), ('::1', socket.AF_INET6)):
            with self.subTest(address=address):
                probe = socket.socket(family, socket.SOCK_STREAM)
                probe.settimeout(2)
                try:
                    with self.assertRaises(OSError):
                        probe.connect((address, self.port))
                finally:
                    probe.close()

    def test_a_body_over_256_kib_is_413_too_large_and_a_declared_length_is_enough(self):
        body = json.dumps({'project_id': self.hub.a, 'title': 't', 'body': 'x' * (256 * 1024)}).encode()
        self.assertGreater(len(body), 256 * 1024)
        reply = self.board().raw('POST', '/api/v1/requests', body, {'Content-Type': 'application/json'})
        self.assertEqual(reply.status, 413)
        self.assertEqual(reply.json['error']['code'], 'too_large')
        declared = (f'POST /api/v1/requests HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
                    f'Authorization: Bearer {self.system}\r\nContent-Type: application/json\r\n'
                    'Content-Length: 10000000\r\nConnection: close\r\n\r\n').encode()
        self.assertEqual(status_of(exchange(self.port, declared, timeout=5)), 413)
        wrong_type = self.board().raw('POST', '/api/v1/requests', b'{}', {'Content-Type': 'text/plain'})
        self.assertEqual(wrong_type.status, 415)
        self.assertEqual(self.requests_stored(), 0)

    def test_clients_that_vanish_mid_request_leave_the_listener_serving(self):
        pieces = (b'GET /api/v1/req',
                  f'GET /api/v1/requests HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'.encode(),
                  (f'POST /api/v1/requests HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
                   f'Authorization: Bearer {self.system}\r\nContent-Type: application/json\r\n'
                   'Content-Length: 100\r\n\r\n{"pro').encode())
        for data in pieces:
            connection = socket.create_connection(('127.0.0.1', self.port), timeout=5)
            connection.sendall(data)
            connection.close()
        time.sleep(0.3)
        self.assertEqual(self.board().get('/requests').status, 200)


class OriginRecordTests(InternalAdversarialCase):
    def test_a_board_request_is_recorded_as_board_agent_and_an_owner_key_is_not(self):
        fields = {'project_id': self.hub.a, 'title': 'From the Board', 'body': 'B'}
        made = self.board().post('/requests', dict(fields, idempotency_key='k-board'))
        self.assertEqual(made.status, 201, made.body)
        self.assertEqual(made.json['origin']['kind'], 'board_agent')
        owner = self.hub.key('owner-e', 'editor')
        other = self.hub.rest(owner).post('/requests', dict(fields, title='From an owner key', idempotency_key='k-own'))
        self.assertEqual(other.status, 201, other.body)
        self.assertNotEqual(other.json['origin']['kind'], 'board_agent')
        stored = self.hub.rest(owner).get(f"/requests/{made.json['request_id']}")  # read back through another door
        self.assertEqual(stored.status, 200)
        self.assertEqual(stored.json['request']['origin']['kind'], 'board_agent')


# --- the Board launcher: skill copy, overlay, env, isolation -----------------------------------------------------

def assert_skill_copy(case: unittest.TestCase, skills: Path) -> None:
    folder = skills / 'wb-inbox'
    case.assertEqual(sorted(os.listdir(skills)), ['wb-inbox'])
    case.assertEqual(sorted(os.listdir(folder)), ['SKILL.md', 'wb_inbox.py'])
    for directory in (skills, folder):
        info = os.lstat(directory)
        case.assertTrue(stat.S_ISDIR(info.st_mode), directory)
        case.assertEqual(stat.S_IMODE(info.st_mode), 0o700, directory)
    for name in ('SKILL.md', 'wb_inbox.py'):
        info = os.lstat(folder / name)
        case.assertTrue(stat.S_ISREG(info.st_mode), name)
        case.assertEqual(stat.S_IMODE(info.st_mode), 0o600, name)
        case.assertEqual(info.st_nlink, 1, name)
        case.assertEqual((folder / name).read_bytes(), (SKILL_SOURCE / name).read_bytes(), name)


class SkillCopyAdversarialTests(LauncherCase):
    def board(self) -> Path:
        return self.box.state / 'board-agent'

    def skills(self) -> Path:
        return self.board() / 'skills'

    @staticmethod
    def packaged(source: Path):
        """``canonical_path`` with the skill files read from ``source`` (everything else, the role file, as shipped)."""
        real = launch.canonical_path
        return lambda name: source / name.rpartition('/')[2] if name.startswith('skills/wb-inbox/') else real(name)

    def victim(self) -> Path:
        victim = self.box.root / 'victim'
        (victim / 'wb-inbox').mkdir(parents=True)
        (victim / 'wb-inbox' / 'SKILL.md').write_text('victim skill\n')
        (victim / 'keep.txt').write_text('keep\n')
        return victim

    def test_modes_are_fixed_under_umask_zero_and_the_copy_has_one_link(self):
        old = os.umask(0)
        try:
            self.prepare()
        finally:
            os.umask(old)
        assert_skill_copy(self, self.skills())

    def test_a_symlinked_skills_dir_or_skill_folder_is_never_written_through(self):
        victim = self.victim()
        before = tree(victim)
        self.prepare()
        shutil.rmtree(self.skills())
        self.skills().symlink_to(victim)
        try:
            self.prepare()
        except launch.BoardLaunchError:
            pass  # refusing is as good as replacing the link
        else:
            self.assertFalse(self.skills().is_symlink())
            assert_skill_copy(self, self.skills())
        self.assertEqual(tree(victim), before)
        self.prepare()
        assert_skill_copy(self, self.skills())
        shutil.rmtree(self.skills() / 'wb-inbox')
        (self.skills() / 'wb-inbox').symlink_to(victim / 'wb-inbox')
        try:
            self.prepare()
        except launch.BoardLaunchError:
            pass
        else:
            self.assertFalse((self.skills() / 'wb-inbox').is_symlink())
            assert_skill_copy(self, self.skills())
        self.assertEqual(tree(victim), before)

    def test_symlinked_and_hard_linked_skill_files_are_replaced_not_followed(self):
        victim = self.victim()
        before = tree(victim)
        self.prepare()
        folder = self.skills() / 'wb-inbox'
        (folder / 'SKILL.md').unlink()
        (folder / 'SKILL.md').symlink_to(victim / 'keep.txt')
        (folder / 'wb_inbox.py').unlink()
        os.link(victim / 'keep.txt', folder / 'wb_inbox.py')
        (folder / '.env').symlink_to(victim / 'keep.txt')
        (folder / '.env.example').write_text('WB_API_KEY=wbk_planted\n')
        self.prepare()
        assert_skill_copy(self, self.skills())
        self.assertEqual(tree(victim), before)

    def test_a_symlinked_board_dir_is_refused_and_nothing_lands_in_the_target(self):
        victim = self.box.root / 'victim-board'
        victim.mkdir()
        before = tree(victim)
        shutil.rmtree(self.board(), ignore_errors=True)
        self.board().symlink_to(victim)
        with self.assertRaises(launch.BoardLaunchError) as raised:
            self.prepare()
        self.assertEqual(raised.exception.reason, 'home_unavailable')
        self.assertEqual(tree(victim), before)

    def test_only_the_two_skill_files_are_copied_whatever_else_the_source_folder_holds(self):
        source = self.box.root / 'source-skill'
        source.mkdir()
        (source / 'SKILL.md').write_bytes(b'---\nname: wb-inbox\ndescription: x\n---\nbody\n')
        (source / 'wb_inbox.py').write_bytes(b'print("client")\n')
        (source / '.env').write_text('WB_API_KEY=wbk_dev_secret\n')
        (source / '.env.example').write_text('WB_API_KEY=\n')
        (source / 'README.md').write_text('readme\n')
        (source / '__pycache__').mkdir()
        (source / '__pycache__' / 'x.pyc').write_bytes(b'\0')
        with mock.patch.object(launch, 'canonical_path', self.packaged(source)):
            self.prepare()
        folder = self.skills() / 'wb-inbox'
        self.assertEqual(sorted(os.listdir(folder)), ['SKILL.md', 'wb_inbox.py'])
        self.assertEqual((folder / 'wb_inbox.py').read_bytes(), b'print("client")\n')
        self.assertEqual(sorted(os.listdir(self.skills())), ['wb-inbox'])
        for path, content in tree(self.board()).items():
            self.assertNotIn(b'wbk_dev_secret', content or b'', path)

    def test_a_missing_packaged_skill_file_refuses_the_start(self):
        source = self.box.root / 'half-skill'
        source.mkdir()
        (source / 'SKILL.md').write_bytes(b'---\nname: wb-inbox\ndescription: x\n---\n')
        with mock.patch.object(launch, 'canonical_path', self.packaged(source)):
            with self.assertRaises(launch.BoardLaunchError) as raised:
                self.prepare()
        self.assertEqual(raised.exception.reason, 'home_unavailable')
        self.assertEqual(self.box.records(), [])  # no OMP was run

    def test_every_kind_of_leftover_mcp_json_is_gone_after_a_start(self):
        self.prepare()
        mcp = self.board() / 'omp-root' / 'agent' / 'mcp.json'
        victim = self.victim()
        before = tree(victim)
        for label in ('file', 'dir', 'dangling', 'to-dir'):
            with self.subTest(kind=label):
                if label == 'file':
                    mcp.write_text('{"mcpServers": {"workbench-hub": {}}}')
                elif label == 'dir':
                    mcp.mkdir()
                    (mcp / 'inner').write_text('x')
                elif label == 'dangling':
                    mcp.symlink_to(self.box.root / 'does-not-exist')
                else:
                    mcp.symlink_to(victim)
                self.prepare()
                self.assertFalse(os.path.lexists(mcp))
        self.assertEqual(tree(victim), before)

    def test_the_overlay_equals_the_probe_documents_json_with_this_skills_dir(self):
        text = PROBE_DOC.read_text()
        after = text[text.index('Overlay(두 번째 `--config`):\n\n```json'):]
        block = after[after.index('```json') + 7:]
        expected = json.loads(block[:block.index('```')])
        started = self.prepare()
        self.assertEqual(expected['skills']['includeSkills'], ['wb-inbox'])
        expected['skills']['customDirectories'] = [str(self.skills())]
        overlay = json.loads((self.board() / 'omp-board.yml').read_text())
        self.assertEqual(overlay, expected)
        self.assertIn('native', overlay['disabledProviders'])
        isolation = (REPO / 'omp_bridge' / 'omp-isolation.yml').read_text()
        listed = re.search(r'^disabledProviders:\s*\[([^\]]*)\]', isolation, re.M).group(1)
        self.assertEqual(overlay['disabledProviders'], [item.strip() for item in listed.split(',') if item.strip()])
        argv = started.argv
        configs = [argv[i + 1] for i, item in enumerate(argv[:-1]) if item == '--config']
        self.assertEqual(configs, [str(REPO / 'omp_bridge' / 'omp-isolation.yml'), str(self.board() / 'omp-board.yml')])
        key = started.env['WB_API_KEY']  # in neither argv nor any file of the Board dir
        self.assertEqual([item for item in argv if key in item], [])
        for path, content in tree(self.board()).items():
            self.assertNotIn(key.encode(), content or b'', path)
        self.assertEqual([name for name, value in started.env.items() if key in value], ['WB_API_KEY'])


class NoProxyTests(LauncherCase):
    def env_for(self, **user) -> dict[str, str]:
        launcher = self.make_launcher(self.config)
        launcher.environment.update(user)
        return self.prepare(launcher).env

    def test_each_variable_keeps_its_own_entries_and_loopback_is_added_once(self):
        env = self.env_for(NO_PROXY='a.example,10.0.0.0/8', no_proxy='b.example,127.0.0.1')
        self.assertEqual(env['NO_PROXY'].split(','), ['a.example', '10.0.0.0/8', '127.0.0.1'])
        self.assertEqual(env['no_proxy'].split(','), ['b.example', '127.0.0.1'])

    def test_an_empty_or_odd_user_value_still_ends_with_loopback_and_nothing_user_made_is_lost(self):
        env = self.env_for(NO_PROXY='', no_proxy=' a.example , ,*')
        self.assertEqual(env['NO_PROXY'].split(','), ['127.0.0.1'])
        self.assertEqual(env['no_proxy'].split(','), ['a.example', '*', '127.0.0.1'])

    def test_the_shell_env_does_not_get_a_changed_no_proxy(self):
        launcher = self.make_launcher(self.config)
        launcher.environment['NO_PROXY'] = 'corp.example'
        self.prepare(launcher)
        shell = launcher.prepare_shell()
        self.assertEqual(shell.env.get('NO_PROXY'), 'corp.example')
        self.assertNotIn('no_proxy', shell.env)


class CopiedClientThroughAProxyTests(unittest.TestCase):
    """The env the Board agent gets lets the copied ``wb_inbox.py`` reach the loopback listener even when the user has
    a (dead) proxy; without ``NO_PROXY`` the same call fails, so the loopback entry is what makes it work."""

    def test_the_copied_client_bypasses_a_user_proxy_with_the_agents_env(self):
        hub = RestHub(self)
        listener = rest.InternalListener(hub.keys, hub.api, clock=lambda: hub.now[0])
        url = hub.run(listener.start())
        self.addCleanup(lambda: hub.run(listener.stop()))
        key = hub.keys.rotate_system_key('a-000000000001', now=hub.now[0]).raw
        root = Path(tempfile.mkdtemp(prefix='wbp-'))
        self.addCleanup(shutil.rmtree, root, True)
        home = root / 'home'
        (home / '.omp' / 'agent').mkdir(parents=True)
        (home / '.omp' / 'agent' / 'agent.db').write_bytes(b'')
        state = root / 'state'
        state.mkdir(mode=0o700)
        base = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'LANG': 'C.UTF-8',
                **{name: 'http://127.0.0.1:9' for name in ('http_proxy', 'HTTP_PROXY', 'all_proxy', 'ALL_PROXY')}}
        omp_home = launch.prepare_board_home(launch.board_dir(state), base)  # WBX-D1: an agent home
        env = launch.board_environment(base, omp_home, key, url)
        script = state / 'board-agent' / 'skills' / 'wb-inbox' / 'wb_inbox.py'

        def run(environment: dict) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, '-I', str(script), 'list'], env=environment, cwd=root,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        done = run(env)
        self.assertEqual(done.returncode, 0, done)
        self.assertIn('requests', json.loads(done.stdout))
        self.assertNotIn(key, done.stdout + done.stderr)
        control = run({name: value for name, value in env.items() if name.lower() != 'no_proxy'})
        self.assertEqual(control.returncode, 4, control)  # a connection error through the dead proxy
        self.assertNotIn(key, control.stdout + control.stderr)


class IsolationAdversarialTests(LauncherCase):
    def check(self, **fake_env) -> str | None:
        self.box.env.update(fake_env)
        self.launcher = self.make_launcher(self.config)
        started = self.prepare()
        return self.arun(self.launcher.check_agent(started))

    def test_lookalike_skill_names_are_not_wb_inbox(self):
        for names in (['wb-inbox-evil'], ['WB-INBOX'], ['wb-inbox ', 'x'], ['wb-inbox', 'wb-inbox-evil'], ['wb_inbox']):
            with self.subTest(skills=names):
                verdict = self.check(FAKE_BOARD_SKILLS=json.dumps(names))
                self.assertIsNotNone(verdict)
                self.assertRegex(verdict, r'^(leak:|skill_missing:)')

    def test_a_leak_wins_over_the_missing_skill_and_names_the_tool(self):
        verdict = self.check(FAKE_BOARD_SKILLS='[]', FAKE_BOARD_TOOLS=json.dumps(['mcp__workbench_hub_inbox_list']))
        self.assertTrue(verdict.startswith('leak:'), verdict)
        self.assertIn('mcp:mcp__workbench_hub_inbox_list', verdict)
        self.assertLessEqual(len(verdict), 256)  # the audit reason limit

    def test_the_check_runs_with_this_starts_key_and_url_and_without_the_old_names(self):
        self.box.env.update(INHERITED)
        self.launcher = self.make_launcher(self.config)
        started = self.prepare()
        self.assertIsNone(self.arun(self.launcher.check_agent(started)))
        row, = self.box.records('rpc')
        self.assertEqual(row['env']['WB_API_KEY'], started.env['WB_API_KEY'])
        self.assertEqual(row['env']['WB_HUB_URL'], f'http://127.0.0.1:{self.listener.port}')
        self.assertIsNone(row['env']['WB_BOARD_MCP_KEY'])
        self.assertNotIn(USER_KEY, json.dumps(row))
        self.assertNotIn(USER_URL, json.dumps(row))
        self.assertEqual(row['skills'], ['wb-inbox'])


# --- S3: a real Hub ------------------------------------------------------------------------------------------------

def api_rows(state: Path, sql: str, *args) -> list[tuple]:
    db = sqlite3.connect(f'file:{state / "hub.sqlite3"}?mode=ro', uri=True)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def get_status(port: int, path: str, key: str | None, host: str | None = None) -> int:
    headers = {'Host': host or f'127.0.0.1:{port}'}
    if key:
        headers['Authorization'] = f'Bearer {key}'
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


class LiveKeyHygieneTests(unittest.TestCase):
    """The system key lives only in the Board agent's own environment (the fake's record file aside)."""

    def test_no_trace_of_any_system_key_in_files_status_pages_audit_or_the_other_processes(self):
        rig = Rig(self)
        pids = rig.start_set()
        key1 = environ_of(pids['agent'])['WB_API_KEY']
        port = int(environ_of(pids['agent'])['WB_HUB_URL'].rpartition(':')[2])
        self.assertEqual(get_status(port, '/api/v1/requests', key1), 200)
        self.assertEqual(rig.restart('agent'), 200)
        rig.wait_state('agent', 'running')
        agent2 = rig.board()['agent']['pid']
        key2 = environ_of(agent2)['WB_API_KEY']
        self.assertNotEqual(key1, key2)
        self.assertEqual(get_status(port, '/api/v1/requests', key2), 200)
        outputs = {'status --json': rig.cli('hub', 'status', '--json').stdout, 'status': rig.cli('hub', 'status').stdout,
                   'hub log': rig.box.hub_log(rig.hub)}
        for path in ('/board', '/settings', '/settings/keys', '/settings/audit', '/dashboard', '/requests'):
            _, _, page = rig.browser.request('GET', path)
            outputs[path] = page if isinstance(page, str) else page.decode('utf-8', 'replace')
        for label, text in outputs.items():
            for key in (key1, key2):
                self.assertNotIn(key, text, label)
        for label, pid in (('hub', rig.hub.pid), ('shell', pids['shell'])):
            for name in ('environ', 'cmdline'):
                raw = Path(f'/proc/{pid}/{name}').read_bytes()
                for key in (key1, key2):
                    self.assertNotIn(key.encode(), raw, f'{label} {name}')
        self.assertNotIn(key2.encode(), Path(f'/proc/{agent2}/cmdline').read_bytes())
        self.assertEqual(rig.stop_via_sigterm(), 0)  # and again after the Hub has stopped (final flush and logs)
        self.assertNotIn(key1, rig.box.hub_log(rig.hub))
        self.assertNotIn(key2, rig.box.hub_log(rig.hub))
        for path, content in rig.every_file(exclude=(rig.record,)):
            for key in (key1, key2):
                self.assertNotIn(key.encode(), content, f'{path}')
        recorded = rig.record.read_text()  # the one place the agent's env is written on purpose
        self.assertIn(key1, recorded)
        self.assertIn(key2, recorded)

    def test_one_system_key_row_at_a_time_and_the_old_key_is_401_on_both_listeners(self):
        rig = Rig(self)
        pids = rig.start_set()
        env1 = environ_of(pids['agent'])
        old, port = env1['WB_API_KEY'], int(env1['WB_HUB_URL'].rpartition(':')[2])
        web = rig.browser.port
        for _ in range(2):
            self.assertEqual(rig.restart('agent'), 200)
            rig.wait_state('agent', 'running')
        new = environ_of(rig.board()['agent']['pid'])['WB_API_KEY']
        self.assertEqual(api_rows(rig.state, 'SELECT key_id FROM api_keys WHERE system = 1'), [(new[4:14],)])
        self.assertEqual([get_status(port, '/api/v1/requests', old), get_status(port, '/api/v1/requests', new)],
                         [401, 200])
        self.assertEqual(get_status(web, '/api/v1/requests', old, f'127.0.0.1:{web}'), 401)

    def test_user_values_reach_no_child_and_the_isolation_check_uses_this_starts_pair(self):
        rig = Rig(self, extra_env=dict(INHERITED, NO_PROXY='corp.example', no_proxy='lan.example'))
        pids = rig.start_set()
        agent, shell = environ_of(pids['agent']), environ_of(pids['shell'])
        for env in (agent, shell):
            self.assertNotEqual(env.get('WB_API_KEY'), USER_KEY)
            self.assertNotEqual(env.get('WB_HUB_URL'), USER_URL)
            self.assertNotIn('WB_BOARD_MCP_KEY', env)
        for name in ('WB_API_KEY', 'WB_HUB_URL'):
            self.assertNotIn(name, shell)
        self.assertEqual(agent['NO_PROXY'].split(','), ['corp.example', '127.0.0.1'])
        self.assertEqual(agent['no_proxy'].split(','), ['lan.example', '127.0.0.1'])
        self.assertEqual((shell['NO_PROXY'], shell['no_proxy']), ('corp.example', 'lan.example'))
        check, = rig.records('rpc')
        self.assertEqual(check['env']['WB_API_KEY'], agent['WB_API_KEY'])
        self.assertEqual(check['env']['WB_HUB_URL'], agent['WB_HUB_URL'])
        self.assertIsNone(check['env']['WB_BOARD_MCP_KEY'])
        self.assertEqual(check['skills'], ['wb-inbox'])
        tty, = rig.records('tty')
        self.assertEqual(tty['skills'], ['wb-inbox'])
        self.assertIsNone(tty['mcp_json'])


class LiveBoardHomeTests(unittest.TestCase):
    def test_the_skill_copy_is_rebuilt_at_every_start_with_exact_files_and_modes(self):
        rig = Rig(self)
        rig.start_set()
        skills = rig.state / 'board-agent' / 'skills'
        assert_skill_copy(self, skills)
        (skills / 'decoy').mkdir()
        for path in (skills / 'decoy' / 'SKILL.md', skills / 'wb-inbox' / '.env', skills / 'wb-inbox' / 'extra.py'):
            path.write_text('---\nname: decoy\ndescription: planted\n---\n')
        mcp = rig.state / 'board-agent' / 'omp-root' / 'agent' / 'mcp.json'
        mcp.write_text('{"mcpServers": {"workbench-hub": {"type": "http", "url": "http://127.0.0.1:1/mcp"}}}')
        self.assertEqual(rig.restart('agent'), 200)
        rig.wait_state('agent', 'running')  # the isolation check passed: the fake showed no decoy
        assert_skill_copy(self, skills)
        self.assertFalse(os.path.lexists(mcp))
        second = rig.records('tty')[-1]
        self.assertEqual((second['skills'], second['mcp_json']), (['wb-inbox'], None))
        for row in rig.records('rpc'):
            self.assertEqual(row['skills'], ['wb-inbox'])
            self.assertIsNone(row['mcp_json'])

    def test_the_fake_does_show_a_decoy_skill_so_the_rebuild_test_above_can_fail(self):
        rig = Rig(self, extra_env={'FAKE_BOARD_SKILLS': json.dumps(['decoy', 'wb-inbox'])})
        self.assertEqual(rig.restart('agent'), 200)
        agent = rig.wait_state('agent', 'isolation_failed')
        self.assertIn('skill:decoy', agent['detail'])


class LiveIsolationAdversarialTests(unittest.TestCase):
    def assert_agent_ended(self, rig: Rig) -> None:
        """The start-up check runs beside the starting agent (SPEC "격리 확인": it is ended afterwards): no agent
        process is left."""
        self.assertEqual(rig.board()['agent']['state'], 'isolation_failed')
        for row in rig.records('tty'):
            self.assertTrue(wait_until(lambda: not Path(f'/proc/{row["pid"]}').exists(), 10), row['pid'])

    def failed(self, **extra) -> tuple[Rig, dict]:
        rig = Rig(self, extra_env=extra)
        self.assertEqual(rig.restart('agent'), 200)
        return rig, rig.wait_state('agent', 'isolation_failed')

    def test_a_refused_start_never_runs_the_agent_and_leaks_no_key_into_state_audit_or_status(self):
        rig, agent = self.failed(FAKE_BOARD_SKILLS=json.dumps(['decoy', 'wb-inbox']))
        self.assertIn('skill:decoy', agent['detail'])
        self.assert_agent_ended(rig)
        key = rig.records('rpc')[0]['env']['WB_API_KEY']
        rows = json.dumps(api_rows(rig.state, 'SELECT event, actor, detail_json FROM audit'))
        self.assertIn('isolation_failed', rows)
        self.assertNotIn(key, rows)
        self.assertNotIn(key, json.dumps(rig.board()))
        self.assertNotIn(key, rig.box.hub_log(rig.hub))
        self.assertNotIn(key, rig.cli('hub', 'status').stdout)

    def test_an_mcp_tool_together_with_a_missing_skill_reports_the_leak_and_never_runs_the_agent(self):
        rig, agent = self.failed(FAKE_BOARD_SKILLS='[]', FAKE_BOARD_TOOLS=json.dumps(['mcp__decoy_user_x']))
        self.assertIn('mcp:mcp__decoy_user_x', agent['detail'])
        self.assert_agent_ended(rig)

    def test_the_hub_keeps_serving_after_a_refused_start(self):
        rig, agent = self.failed(FAKE_BOARD_SKILLS='[]')
        self.assertEqual(agent['detail'], 'skill_missing:wb-inbox')
        self.assertEqual([row['reason'] for row in rig.audit('board_stop')], ['isolation_failed'])
        self.assert_agent_ended(rig)
        self.assertIsNone(rig.hub.poll())
        self.assertEqual(rig.cli('hub', 'status', '--json').returncode, 0)


class LiveWebSwitchTests(unittest.TestCase):
    def test_web_enabled_false_closes_the_external_rest_but_not_the_running_boards_internal_one(self):
        rig = Rig(self)
        pids = rig.start_set()
        agent = environ_of(pids['agent'])
        key, port = agent['WB_API_KEY'], int(agent['WB_HUB_URL'].rpartition(':')[2])
        web = rig.browser.port
        db = auth.HubDb.open(rig.state)
        try:
            owner_key = auth.ApiKeyStore(db).issue('owner-cli', 'editor', {'all': True}, None, now=time.time()).raw
        finally:
            db.close()
        self.assertEqual(get_status(web, '/api/v1/requests', owner_key, f'127.0.0.1:{web}'), 200)
        rig.box.config_file.write_text(rig.box.config_file.read_text().replace('enabled = true', 'enabled = false', 1))
        reloaded = rig.cli('hub', 'reload')
        self.assertEqual(reloaded.returncode, 0, reloaded)
        self.assertTrue(wait_until(lambda: all(item == port for _, item in tcp_listening(rig.hub.pid)), 10),
                        tcp_listening(rig.hub.pid))
        for path in ('/api/v1/requests', '/skill/README.md', '/login'):
            with self.assertRaises(OSError):
                get_status(web, path, owner_key, f'127.0.0.1:{web}')
        self.assertEqual(get_status(port, '/api/v1/requests', key), 200)
        self.assertEqual(get_status(port, '/api/v1/requests', owner_key), 401)  # still the system key alone
        self.assertEqual(get_status(port, '/mcp', key), 404)
        self.assertEqual(get_status(port, '/skill/README.md', key), 404)
        self.assertEqual(rig.board()['agent']['pid'], pids['agent'])


# --- Hub-launched backends ------------------------------------------------------------------------------------------

class LiveBackendEnvironmentTests(twc.WebCase):
    """WBX-D28: a backend the Hub's project control starts (and its panes) inherits none of the Board variables."""

    async def test_board_variables_never_reach_the_backend_or_its_panes(self):
        root, project_id = self.registered()
        poison = {'WB_API_KEY': 'wbk_cccccccccc_' + 'P' * 43, 'WB_HUB_URL': 'http://198.51.100.9:1',
                  'WB_BOARD_MCP_KEY': 'wbk_dddddddddd_' + 'Q' * 43}
        self.control.environment.update(poison)
        session = await self.login()
        status, body = await self.act(session, project_id, 'power-on')
        self.assertTrue(body['ok'], body)
        pid, _ticks, _record = self.box.backend_ref(root)
        pids = {pid, *(ref[0] for ref in self.box.pane_processes(self.box.status(root)).values())}
        for one in pids:
            raw = Path(f'/proc/{one}/environ').read_bytes()
            for name, value in poison.items():
                self.assertNotIn(name.encode() + b'=', raw, f'pid {one}')
                self.assertNotIn(value.encode(), raw, f'pid {one}')


# --- ROLE.md --------------------------------------------------------------------------------------------------------

class RoleStaticTests(unittest.TestCase):
    SUBCOMMANDS = ('projects', 'list', 'get', 'submit', 'update', 'comment', 'set-state', 'cancel', 'result')

    def test_the_role_points_at_the_skill_and_its_real_commands_and_names_no_mcp_tool(self):
        text = ROLE.read_text()
        for needle in ('wb-inbox', 'skill://wb-inbox', 'wb_inbox.py', 'WB_HUB_URL', 'WB_API_KEY'):
            self.assertIn(needle, text)
        for pattern in (r'inbox_\w+', r'mcp__', r'workbench-hub', r'WB_BOARD_MCP_KEY', r'/mcp\b', r'mcp\.json'):
            self.assertIsNone(re.search(pattern, text, re.I), pattern)
        self.assertNotIn('wbk_', text)
        listed = set(re.findall(r'`([a-z][a-z-]*)`', text))
        self.assertTrue(set(self.SUBCOMMANDS) <= listed, set(self.SUBCOMMANDS) - listed)
        done = subprocess.run([sys.executable, '-I', str(SKILL_SOURCE / 'wb_inbox.py'), '--help'], capture_output=True,
                              text=True, timeout=30, env={'PATH': '/usr/bin:/bin'}, stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 0, done)
        for name in self.SUBCOMMANDS:  # every command the role names exists in the shipped client
            self.assertRegex(done.stdout, rf'\b{name}\b')

    def test_the_two_shared_resource_notices_survive_and_the_text_is_english(self):
        text = ROLE.read_text()
        self.assertEqual(text.count('계속할까요?'), 2)
        self.assertIn('nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader', text)
        self.assertEqual(text.count('`wb_inbox.py projects`'), 2)  # both notices read the projects output
        rest_text = text.replace('계속할까요?', '')
        self.assertTrue(rest_text.isascii(), [char for char in rest_text if not char.isascii()])
        self.assertIn('data', text.lower())  # request text is data, not instructions


if __name__ == '__main__':
    unittest.main()
