"""WBX-B1 independent adversarial verification of ``/api/v1/`` (test_designer; scenarios derived from the contract).

Contracts: web-board-ux SPEC "외부 요청 API" (endpoint table, error -> status table, auth, body/query/limit rules,
idempotency, two-step cancel, result paging), BRIEF WBX-D05..D07/D24/D25, ADR-0006, task-inbox SPEC "Principal과 권한",
"입력 한도", "취소", "결과 조회". Expected values are written down here from those texts, not from ``rest.py``.

Seams: S2 (``support/rest_hub.py`` real loopback Hub web app + ``support/rest_client.py``, plus raw sockets for
transport abuse) and S1 (aiohttp ``TestServer`` over the real ``Hub.web_routes()``). The worker's own tests are not
reused. Run with ``discover -s tests/hub -p 'test_hub_rest_independent.py'``.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support.rest_client import Reply  # noqa: E402
from support.rest_hub import RestHub  # noqa: E402
from workbench.hub.inbox.limits import InboxError  # noqa: E402

WWW_AUTHENTICATE = 'Bearer realm="workbench-hub"'
# SPEC "오류 -> HTTP status" table, as written in the SPEC.
SPEC_STATUS = {
    'invalid_input': 400, 'unauthorized': 401, 'forbidden': 403, 'origin_mismatch': 403, 'not_found': 404,
    'conflict': 409, 'invalid_transition': 409, 'idempotency_conflict': 409, 'token_mismatch': 409,
    'backend_not_running': 409, 'too_large': 413, 'internal_error': 500,
    'project_unavailable': 503, 'inbox_busy': 503, 'inbox_mismatch': 503, 'backend_unreachable': 503,
    'hub_stopping': 503, 'busy': 503,
}
LEAK_MARKERS = ('Traceback', 'File "', 'sqlite3', 'OperationalError', 'KeyError', 'ValueError', 'TypeError',
                'RecursionError', 'JSONDecodeError', 'UnicodeEncodeError', 'UnicodeDecodeError', 'ParseError',
                'InboxError', 'RuntimeError', 'OverflowError', '.py"', '.py:', '/workbench/', '/home/', '/dev/shm',
                '/tmp/', '/proc/')
UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


class Conn:
    """One raw HTTP/1.1 connection (lying lengths, malformed headers, pipelining)."""

    def __init__(self, port: int, timeout: float = 8.0):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=timeout)
        self.buf = b''

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read_reply(self) -> Reply | None:
        """The next response, or ``None`` when the server closed without one."""
        while b'\r\n\r\n' not in self.buf:
            try:
                chunk = self.sock.recv(65536)
            except ConnectionResetError:
                return None
            if not chunk:
                return None
            self.buf += chunk
        head, _, rest = self.buf.partition(b'\r\n\r\n')
        lines = head.decode('latin-1').split('\r\n')
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(':')
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get('content-length', 0))
        while len(rest) < length:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            rest += chunk
        self.buf = rest[length:]
        return Reply(status, headers, rest[:length])


def http_bytes(hub: RestHub, method: str, path: str, key: str | None, *, body: bytes = b'',
               length: str | None = None, ctype: str | None = 'application/json',
               extra: tuple = ()) -> bytes:
    lines = [f'{method} {path} HTTP/1.1', f'Host: 127.0.0.1:{hub.port}']
    if key is not None:
        lines.append(f'Authorization: Bearer {key}')
    if ctype is not None:
        lines.append(f'Content-Type: {ctype}')
    if length is not None:
        lines.append(f'Content-Length: {length}')
    lines.extend(f'{name}: {value}' for name, value in extra)
    return ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1') + body


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class Base(unittest.TestCase):
    hub_options: dict = {}

    def setUp(self):
        self.hub = RestHub(self, **self.hub_options)
        self.r1, self.r2 = self.hub.key('r1'), self.hub.key('r2')
        self.ed = self.hub.key('e1', 'editor')

    # -- helpers --------------------------------------------------------------------------------
    def count(self, root: Path | None = None) -> int:
        rows = self.hub.inbox_rows('SELECT count(*) FROM requests', root=root)
        return rows[0][0] if rows else 0

    def leak_markers(self) -> tuple:
        return LEAK_MARKERS + (str(self.hub.base), str(self.hub.root_a), str(self.hub.root_b))

    def assert_clean(self, reply: Reply) -> None:
        text = reply.body.decode('utf-8', 'replace')
        for marker in self.leak_markers():
            self.assertNotIn(marker, text, f'{marker!r} leaked in {text[:300]}')

    def err(self, reply: Reply, status: int | None = None, code: str | None = None, clean: bool = True) -> dict:
        """An error reply: JSON error shape, the SPEC status of its code, no-store, no leak, no cookie."""
        self.assertEqual(reply.header('content-type').split(';')[0], 'application/json', reply.body[:200])
        error = reply.json['error']
        self.assertTrue(set(error) <= {'code', 'message', 'state', 'version'}, error)
        self.assertIsInstance(error['message'], str)
        if error['code'] == 'invalid_input' and reply.status in (405, 415):  # SPEC: transport errors use this code
            pass
        else:
            self.assertEqual(reply.status, SPEC_STATUS[error['code']], reply.body)
        if status is not None:
            self.assertEqual(reply.status, status, reply.body)
        if code is not None:
            self.assertEqual(error['code'], code, reply.body)
        self.assertEqual(reply.header('cache-control'), 'no-store')
        self.assertIsNone(reply.header('set-cookie'))
        if clean:
            self.assert_clean(reply)
        return error

    def submit(self, key: str | None = None, project: str | None = None, title: str = 't', body: str = 'b',
               idem: str = 'k1', **headers) -> Reply:
        return self.hub.rest(key or self.r1).post('/requests', {
            'project_id': project or self.hub.a, 'title': title, 'body': body, 'idempotency_key': idem},
            headers or None)

    def make(self, key: str | None = None, project: str | None = None, **kwargs) -> str:
        reply = self.submit(key, project, **kwargs)
        self.assertIn(reply.status, (200, 201), reply.body)
        return reply.json['request_id']

    def raw_post(self, path: str, body: bytes, key: str | None = None, ctype: str = 'application/json',
                 method: str = 'POST') -> Reply:
        return self.hub.rest(key or self.r1).raw(method, '/api/v1' + path, body, {'Content-Type': ctype})

    def submit_json(self, **fields) -> bytes:
        value = {'project_id': self.hub.a, 'title': 't', 'body': 'b', 'idempotency_key': 'k1', **fields}
        return json.dumps(value).encode()


# ================================================================================================
# 1. Authentication: cookie + Bearer mixes, oracles, ordering
# ================================================================================================

class MixedAuthTests(Base):
    """AC-08: the session cookie is never read; a cookie next to a Bearer key neither helps nor escalates."""

    def cookie(self) -> dict:
        issued = self.hub.services.sessions.create('127.0.0.1', 'ua', self.hub.now[0], generation=1)
        return {'Cookie': f'wb_hub_session={issued.token}', 'X-CSRF-Token': issued.csrf}

    def test_an_owner_session_cookie_does_not_raise_a_requester_key_to_owner_or_editor(self):
        rid = self.make(self.ed)  # an editor's request: a requester must not see it
        headers = self.cookie()
        client = self.hub.rest(self.r1)
        self.err(client.patch(f'/requests/{rid}', {'base_version': 1, 'title': 'x'}, headers), 403, 'forbidden')
        self.err(client.post(f'/requests/{rid}/state', {'base_version': 1, 'to': 'NEEDS_INFO'}, headers),
                 403, 'forbidden')
        self.err(client.get(f'/requests/{rid}', headers), 404, 'not_found')
        self.assertEqual(client.get('/requests', headers).json['requests'], [])
        projects = client.get('/projects', headers).json['projects']
        self.assertTrue(projects and all(set(item) == {'project_id', 'name'} for item in projects), projects)
        self.assertEqual(self.hub.inbox_rows('SELECT title, version FROM requests WHERE request_id = ?', rid),
                         [('t', 1)])

    def test_a_cookie_does_not_rescue_a_revoked_expired_or_wrong_key(self):
        headers = self.cookie()
        revoked = self.hub.key('gone')
        expiring = self.hub.key('soon', expires_days=1)
        self.hub.keys.revoke(revoked[4:14], now=self.hub.now[0])
        self.hub.now[0] += 2 * 86400.0
        for key in (revoked, expiring, 'wbk_nope', None):
            with self.subTest(key=(key or 'none')[:12]):
                self.err(self.hub.rest(key).get('/projects', headers), 401, 'unauthorized')

    def test_a_cookie_with_a_good_key_stores_the_keys_origin_not_the_owners(self):
        reply = self.submit(self.r1, headers_=None) if False else self.hub.rest(self.r1).post(
            '/requests', json.loads(self.submit_json()), self.cookie())
        self.assertEqual(reply.status, 201, reply.body)
        self.assertEqual(reply.json['origin']['kind'], 'api_key')
        self.assertEqual(self.hub.inbox_rows('SELECT origin_kind FROM requests')[0][0], 'api_key')

    def test_a_valid_cookie_never_authenticates_without_a_bearer_on_any_method(self):
        headers = self.cookie()
        rid = '00000000-0000-4000-8000-000000000000'
        client = self.hub.rest(None)
        for method in ('GET', 'POST', 'PATCH', 'PUT', 'DELETE', 'OPTIONS', 'HEAD'):
            for path in ('/projects', '/requests', f'/requests/{rid}', f'/requests/{rid}/cancel', '/nope'):
                with self.subTest(method=method, path=path):
                    reply = client.raw(method, '/api/v1' + path, b'{}' if method in ('POST', 'PATCH', 'PUT') else None,
                                       {**headers, 'Content-Type': 'application/json'})
                    self.assertEqual(reply.status, 401, reply.body)
                    self.assertEqual(reply.header('www-authenticate'), WWW_AUTHENTICATE)
                    if method != 'HEAD':
                        self.err(reply, 401, 'unauthorized')


class AuthOracleTests(Base):
    """Every kind of bad credential answers the same bytes (no oracle for "key exists / revoked / expired")."""

    def bad_credentials(self) -> dict:
        real = self.hub.key('real')
        revoked = self.hub.key('old')
        self.hub.keys.revoke(revoked[4:14], now=self.hub.now[0])
        expiring = self.hub.key('soon', expires_days=1)
        forged_secret = real[:-1] + ('A' if real[-1] != 'A' else 'B')
        forged_id = 'wbk_' + ('A' * 10) + real[14:]
        self.hub.now[0] += 2 * 86400.0
        return {'none': None, 'revoked': f'Bearer {revoked}', 'expired': f'Bearer {expiring}',
                'forged secret': f'Bearer {forged_secret}', 'unknown id': f'Bearer {forged_id}',
                'junk': 'Bearer junk', 'basic': f'Basic {real}', 'bare': real, 'empty': 'Bearer ',
                'long': 'Bearer ' + 'x' * 7000}

    def test_all_bad_credentials_give_identical_401_answers(self):
        seen = {}
        for name, value in self.bad_credentials().items():
            headers = {'Authorization': value} if value is not None else {}
            reply = self.hub.rest(None).get('/projects', headers)
            seen[name] = (reply.status, reply.body, reply.header('www-authenticate'), reply.header('content-type'),
                          reply.header('cache-control'))
        self.assertEqual(len(set(seen.values())), 1, seen)
        status, body, challenge, _ctype, cache = next(iter(seen.values()))
        self.assertEqual((status, challenge, cache), (401, WWW_AUTHENTICATE, 'no-store'))
        self.assertEqual(json.loads(body), {'error': {'code': 'unauthorized', 'message': 'unauthorized'}})

    def test_the_key_in_the_query_or_the_body_is_never_credentials(self):
        key = self.r1
        client = self.hub.rest(None)
        for param in ('access_token', 'api_key', 'key', 'token', 'authorization'):
            with self.subTest(param=param):
                self.err(client.get('/projects', **{param: key}), 401, 'unauthorized')
        reply = self.hub.rest(None).raw('POST', '/api/v1/requests',
                                        self.submit_json(api_key=key), {'Content-Type': 'application/json'})
        self.err(reply, 401, 'unauthorized')
        with_header = self.hub.rest(key).post('/requests', json.loads(self.submit_json(api_key=key)))
        self.err(with_header, 400, 'invalid_input')  # an unknown field, never a second credential
        self.assertEqual(self.count(), 0)

    def test_authentication_comes_before_method_type_size_json_and_route_checks(self):
        client = self.hub.rest(None)
        rid = str(uuid.uuid4())
        probes = [
            ('PUT', '/api/v1/requests', b'{}', {'Content-Type': 'application/json'}),  # would be 405
            ('POST', '/api/v1/requests', b'{}', {'Content-Type': 'text/plain'}),  # would be 415
            ('POST', '/api/v1/requests', b'\xff\xfe', {'Content-Type': 'application/json'}),  # would be 400
            ('GET', '/api/v1/nowhere', None, {}),  # would be 404
            ('GET', f'/api/v1/requests/{rid}/nope', None, {}),
            ('GET', '/api/v1/projects?limit=banana&x=1', None, {}),  # would be 400
        ]
        for method, path, body, headers in probes:
            with self.subTest(method=method, path=path):
                reply = client.raw(method, path, body, headers)
                self.err(reply, 401, 'unauthorized')

    def test_a_declared_oversize_body_without_a_key_is_401_not_413(self):
        conn = Conn(self.hub.port)
        self.addCleanup(conn.close)
        conn.send(http_bytes(self.hub, 'POST', '/api/v1/requests', None, length='400000', body=b'{'))
        reply = conn.read_reply()
        self.assertIsNotNone(reply)
        self.err(reply, 401, 'unauthorized')


# ================================================================================================
# 2. Authorization: key isolation and grade/scope
# ================================================================================================

class IsolationTests(Base):
    """A requester sees and acts on only its own requests; an editor only inside its scope; 404 hides existence."""

    def setUp(self):
        super().setUp()
        self.rid = self.make(self.r1, title='secret-title-xyzzy', body='secret-body-plugh', idem='ka')
        self.ghost = str(uuid.uuid4())

    def sentinel(self, reply: Reply) -> None:
        text = reply.body.decode('utf-8', 'replace')
        self.assertNotIn('secret-title-xyzzy', text)
        self.assertNotIn('secret-body-plugh', text)

    def test_another_requester_gets_the_same_404_as_for_an_id_that_never_existed(self):
        client = self.hub.rest(self.r2)
        operations = [
            lambda rid: client.get(f'/requests/{rid}'),
            lambda rid: client.post(f'/requests/{rid}/comments', {'body': 'hi'}),
            lambda rid: client.post(f'/requests/{rid}/cancel', {}),
            lambda rid: client.get(f'/requests/{rid}/result'),
            lambda rid: client.get(f'/requests/{rid}/result', full=True),
            lambda rid: client.get(f'/requests/{rid}/result', full=True, run_id=str(uuid.uuid4())),
            lambda rid: client.get(f'/requests/{rid}/result', full=True, artifact_path='x.txt'),
        ]
        for index, operation in enumerate(operations):
            with self.subTest(operation=index):
                mine, ghost = operation(self.rid), operation(self.ghost)
                self.err(mine, 404, 'not_found')
                self.sentinel(mine)
                self.assertEqual((mine.status, mine.json), (ghost.status, ghost.json), 'existence oracle')
        self.assertEqual(self.hub.inbox_rows('SELECT count(*) FROM comments WHERE request_id = ?', self.rid)[0][0], 0)

    def test_a_requester_cannot_list_search_filter_or_cursor_into_other_keys_requests(self):
        for index in range(4):
            self.make(self.r1, title=f'mine {index}', idem=f'kk{index}')
        other = self.make(self.r2, title='theirs', idem='kz')
        client = self.hub.rest(self.r2)
        self.assertEqual([item['request_id'] for item in client.get('/requests').json['requests']], [other])
        for query in ({'query': 'secret-title-xyzzy'}, {'query': 'mine'}, {'state': 'NEW', 'query': 'mine'},
                      {'project_id': self.hub.a, 'query': 'plugh'}):
            with self.subTest(query=query):
                found = client.get('/requests', **query).json['requests']
                self.assertEqual([item['request_id'] for item in found if item['request_id'] != other], [])
        first = self.hub.rest(self.r1).get('/requests', limit=1)
        cursor = first.json['next_cursor']
        self.assertIsNotNone(cursor)
        stolen = client.get('/requests', cursor=cursor, limit=100)
        self.assertIn(stolen.status, (200, 400), stolen.body)
        if stolen.status == 200:
            self.assertEqual([item['request_id'] for item in stolen.json['requests'] if item['request_id'] != other], [])
        self.sentinel(stolen)

    def test_a_requester_cannot_edit_or_move_state_and_cannot_tell_whether_the_id_exists(self):
        client = self.hub.rest(self.r1)
        for rid in (self.rid, self.ghost):
            with self.subTest(rid='mine' if rid == self.rid else 'ghost'):
                self.err(client.patch(f'/requests/{rid}', {'base_version': 1, 'title': 'x'}), 403, 'forbidden')
                self.err(client.post(f'/requests/{rid}/state', {'base_version': 1, 'to': 'NEEDS_INFO'}),
                         403, 'forbidden')
        other = self.hub.rest(self.r2)
        self.err(other.patch(f'/requests/{self.rid}', {'base_version': 1, 'body': 'x'}), 403, 'forbidden')
        self.assertEqual(self.hub.inbox_rows('SELECT state, version, title FROM requests WHERE request_id = ?',
                                             self.rid), [('NEW', 1, 'secret-title-xyzzy')])

    def test_requester_project_list_has_no_paths_status_or_path_search_oracle(self):
        client = self.hub.rest(self.r1)
        projects = client.get('/projects').json
        self.assertEqual({tuple(sorted(item)) for item in projects['projects']}, {('name', 'project_id')})
        self.assertNotIn(str(self.hub.base), json.dumps(projects))
        for needle in ('wb-rest', str(self.hub.root_a.parent)[-12:]):  # the temp dir name is in editors' root only
            self.assertEqual(client.get('/projects', query=needle).json['projects'], [], needle)
        editor = self.hub.rest(self.ed).get('/projects', query='wb-rest').json['projects']
        self.assertEqual(len(editor), 2)  # the control: an editor's path search does match

    def test_scoped_keys_stay_inside_their_projects(self):
        beta_rid = self.make(self.ed, project=self.hub.b, idem='kb')
        scoped_editor = self.hub.key('e2', 'editor', {'projects': [self.hub.a]})
        scoped_requester = self.hub.key('r3', scope={'projects': [self.hub.a]})
        for key in (scoped_editor, scoped_requester):
            client = self.hub.rest(key)
            names = {item['project_id'] for item in client.get('/projects').json['projects']}
            self.assertEqual(names, {self.hub.a})
            self.err(self.submit(key, project=self.hub.b, idem='kx'), 403, 'forbidden')
            self.err(client.get('/requests', project_id=self.hub.b), 403, 'forbidden')
            self.err(client.get(f'/requests/{beta_rid}'), 404, 'not_found')
            self.err(client.post(f'/requests/{beta_rid}/comments', {'body': 'x'}), 404, 'not_found')
            self.err(client.get(f'/requests/{beta_rid}/result'), 404, 'not_found')
            self.err(client.post(f'/requests/{beta_rid}/cancel', {}), 404, 'not_found')
        editor = self.hub.rest(scoped_editor)
        self.err(editor.patch(f'/requests/{beta_rid}', {'base_version': 1, 'title': 'x'}), 404, 'not_found')
        self.err(editor.post(f'/requests/{beta_rid}/state', {'base_version': 1, 'to': 'NEEDS_INFO'}),
                 404, 'not_found')
        self.assertEqual(self.count(self.hub.root_b), 1)
        self.assertEqual(self.hub.inbox_rows('SELECT title, state, version FROM requests', root=self.hub.root_b),
                         [('t', 'NEW', 1)])
        # the scoped editor does see and edit alpha requests of other keys (editor grade)
        self.assertEqual(editor.patch(f'/requests/{self.rid}', {'base_version': 1, 'title': 'edited'}).status, 200)

    def test_an_editor_can_only_make_the_two_allowed_state_moves(self):
        client = self.hub.rest(self.ed)
        for target in ('REVIEWING', 'READY', 'RUNNING', 'CLOSED', 'new', '', 'NEEDS_INFO ', None, 3, ['NEW']):
            with self.subTest(to=target):
                reply = client.post(f'/requests/{self.rid}/state', {'base_version': 1, 'to': target})
                self.err(reply, 400, 'invalid_input')
        self.assertEqual(self.hub.inbox_rows('SELECT state, version FROM requests WHERE request_id = ?', self.rid),
                         [('NEW', 1)])
        ok = client.post(f'/requests/{self.rid}/state', {'base_version': 1, 'to': 'NEEDS_INFO', 'reason': 'why'})
        self.assertEqual((ok.status, ok.json['state']), (200, 'NEEDS_INFO'))

    def test_base_version_conflict_and_hostile_versions(self):
        client = self.hub.rest(self.ed)
        stale = client.patch(f'/requests/{self.rid}', {'base_version': 99, 'title': 'x'})
        error = self.err(stale, 409, 'conflict')
        self.assertEqual((error.get('state'), error.get('version')), ('NEW', 1))
        for value in (0, -1, 2 ** 31, 2 ** 63, 2 ** 64, 10 ** 30, 1.5, 1.0, '1', True, None, [1], {}):
            with self.subTest(base_version=value):
                reply = client.patch(f'/requests/{self.rid}', {'base_version': value, 'title': 'x'})
                self.assertIn(reply.status, (400, 409), reply.body)  # never 5xx
                self.err(reply)
        self.assertEqual(self.hub.inbox_rows('SELECT title, version FROM requests WHERE request_id = ?', self.rid),
                         [('secret-title-xyzzy', 1)])

    def test_pagination_walks_every_visible_request_once_newest_first(self):
        ids = [self.rid] + [self.make(self.r1, title=f'p{index}', idem=f'pg{index}') for index in range(6)]
        client = self.hub.rest(self.r1)
        seen, cursor = [], None
        for _ in range(10):
            page = client.get('/requests', limit=3, cursor=cursor).json
            seen += [item['request_id'] for item in page['requests']]
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertEqual(sorted(seen), sorted(ids))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(seen, list(reversed(ids)))
        for bad in ('garbage', 'a' * 5000, '../..', '%00'):
            with self.subTest(cursor=bad[:8]):
                self.assertEqual(client.get('/requests', cursor=bad).status, 400)
        for limit in (0, 101, 'x', -1, '', '1.5', '٣', '+3', ' 3', '0x3', '9' * 30):
            with self.subTest(limit=limit):
                self.err(client.get('/requests', limit=limit), 400, 'invalid_input')
        self.assertEqual(client.get('/requests', limit=100).status, 200)

    def test_closed_requests_are_hidden_unless_asked_for(self):
        client = self.hub.rest(self.r1)
        self.assertEqual(client.post(f'/requests/{self.rid}/cancel', {}).json, {'status': 'cancelled', 'state': 'CLOSED'})
        self.assertEqual(client.get('/requests').json['requests'], [])
        self.assertEqual(len(client.get('/requests', include_closed=True).json['requests']), 1)
        self.assertEqual(len(client.get('/requests', state='CLOSED').json['requests']), 1)
        self.assertEqual(len(client.get('/requests', state=['NEW', 'CLOSED']).json['requests']), 1)
        self.err(client.get('/requests', state='closed'), 400, 'invalid_input')
        self.err(client.get('/requests', include_closed='1'), 400, 'invalid_input')
        self.err(client.get('/requests', include_closed='True'), 400, 'invalid_input')


# ================================================================================================
# 3. Idempotency
# ================================================================================================

class IdempotencyTests(Base):
    def test_a_new_request_is_201_with_location_and_a_retry_is_200_with_the_same_id(self):
        first = self.submit(idem='same')
        self.assertEqual(first.status, 201, first.body)
        self.assertEqual(first.header('location'), f"/api/v1/requests/{first.json['request_id']}")
        self.assertEqual(set(first.json), {'request_id', 'project_id', 'seq', 'state', 'origin', 'created'})
        self.assertIs(first.json['created'], True)
        self.assertEqual(self.hub.rest(self.r1).get(first.header('location')[len('/api/v1'):]).status, 200)
        retry = self.submit(idem='same')
        self.assertEqual(retry.status, 200, retry.body)
        self.assertEqual(retry.json['request_id'], first.json['request_id'])
        self.assertEqual((retry.json['seq'], retry.json['project_id']), (first.json['seq'], first.json['project_id']))
        self.assertIs(retry.json['created'], False)
        self.assertEqual(self.count(), 1)

    def test_different_title_or_body_with_the_same_key_is_409_idempotency_conflict_and_stores_nothing(self):
        first = self.submit(title='A', body='B', idem='k')
        for fields in ({'title': 'A2', 'body': 'B'}, {'title': 'A', 'body': 'B2'}, {'title': 'A ', 'body': 'B'},
                       {'title': 'a', 'body': 'B'}):
            with self.subTest(fields=fields):
                error = self.err(self.submit(idem='k', **fields), 409, 'idempotency_conflict')
                self.assertEqual(error.get('state'), 'NEW')
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.hub.inbox_rows('SELECT title, body FROM requests'), [('A', 'B')])
        self.assertEqual(first.status, 201)

    def test_the_same_key_in_another_project_is_a_new_request_there(self):
        in_a = self.submit(project=self.hub.a, idem='shared')
        in_b = self.submit(project=self.hub.b, idem='shared')
        self.assertEqual((in_a.status, in_b.status), (201, 201), (in_a.body, in_b.body))
        self.assertNotEqual(in_a.json['request_id'], in_b.json['request_id'])
        self.assertEqual((in_a.json['project_id'], in_b.json['project_id']), (self.hub.a, self.hub.b))
        self.assertEqual((self.count(), self.count(self.hub.root_b)), (1, 1))
        # a retry in B is B's request, a conflicting body in B is B's conflict
        self.assertEqual(self.submit(project=self.hub.b, idem='shared').json['request_id'], in_b.json['request_id'])
        self.err(self.submit(project=self.hub.b, idem='shared', title='other'), 409, 'idempotency_conflict')

    def test_another_key_reusing_my_idempotency_key_gets_its_own_request_and_no_conflict(self):
        mine = self.submit(self.r1, title='mine', idem='common')
        theirs = self.submit(self.r2, title='different title', idem='common')
        self.assertEqual(theirs.status, 201, theirs.body)  # no conflict, no 200 (no oracle on others' keys)
        self.assertNotEqual(theirs.json['request_id'], mine.json['request_id'])
        editor = self.submit(self.ed, title='mine', idem='common')
        self.assertEqual(editor.status, 201, editor.body)  # same content, another origin: still new
        self.assertEqual(self.count(), 3)
        text = self.hub.rest(self.r2).get(f"/requests/{theirs.json['request_id']}").body.decode()
        self.assertNotIn(mine.json['request_id'], text)

    def test_parallel_identical_submits_make_exactly_one_request(self):
        replies: list = []
        gate = threading.Barrier(8)

        def worker():
            gate.wait(10)
            replies.append(self.submit(self.r1, idem='race', title='race', body='b'))
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(replies), 8)
        self.assertEqual(sorted(reply.status for reply in replies), [200] * 7 + [201], [r.body for r in replies])
        self.assertEqual(len({reply.json['request_id'] for reply in replies}), 1)
        self.assertEqual(self.count(), 1)

    def test_idempotency_key_format_is_enforced_and_missing_keys_are_refused(self):
        self.assertEqual(self.submit(idem='a' * 128).status, 201)
        for bad in ('a' * 129, '', 'has space', 'slash/x', 'ü', 'a\nb', 'a;b', '키'):
            with self.subTest(key=bad[:12]):
                self.err(self.submit(idem=bad), 400, 'invalid_input')
        for body in (b'{"project_id":"%s","title":"t","body":"b"}' % self.hub.a.encode(),
                     b'{"project_id":"%s","title":"t","body":"b","idempotency_key":null}' % self.hub.a.encode(),
                     b'{"project_id":"%s","title":"t","body":"b","idempotency_key":5}' % self.hub.a.encode()):
            self.err(self.raw_post('/requests', body), 400, 'invalid_input')
        self.assertEqual(self.count(), 1)

    def test_text_comes_back_as_ascii_json_unchanged_and_marked_untrusted(self):
        text = '한글 제목 😀 \\ " <script>'
        rid = self.make(title=text, body='본문\n😀', idem='uni')
        reply = self.hub.rest(self.ed).get(f'/requests/{rid}')
        self.assertTrue(reply.body.isascii(), reply.body[:120])
        request = reply.json['request']
        self.assertEqual((request['title'], request['body']), (text, '본문\n😀'))
        self.assertIs(reply.json['untrusted_text'], True)
        self.assertTrue(reply.json['note'])
        listing = self.hub.rest(self.ed).get('/requests').json
        self.assertIs(listing['untrusted_text'], True)
        self.assertIsNone(listing['next_cursor'])


# ================================================================================================
# 4. Limits: refused with a reason and nothing stored
# ================================================================================================

class LimitTests(Base):
    def stored(self) -> tuple:
        return (self.count(), self.hub.inbox_rows('SELECT count(*) FROM request_revisions')[0][0]
                if self.count() else 0)

    def refused(self, reply: Reply, status: int, code: str, before: int = 0) -> None:
        self.err(reply, status, code)
        self.assertEqual(self.count(), before)

    def test_title_limits(self):
        self.assertEqual(self.submit(title='a' * 200, idem='t1').status, 201)
        self.assertEqual(self.submit(title='한' * 200, idem='t2').status, 201)  # 600 bytes, 200 code points
        self.assertEqual(self.submit(title='😀' * 200, idem='t3').status, 201)
        self.assertEqual(self.count(), 3)
        for title, status, code in (('a' * 201, 413, 'too_large'), ('😀' * 201, 413, 'too_large')):
            self.refused(self.submit(title=title, idem='t9'), status, code, 3)
        for title in ('', 'a\x00b', 'a\x07b', 'a\nb', 'a\tb', 'a‮b', '\ud800', 'a\udfffb'):
            with self.subTest(title=repr(title)):
                reply = self.hub.rest(self.r1).raw('POST', '/api/v1/requests', json.dumps({
                    'project_id': self.hub.a, 'title': title, 'body': 'b', 'idempotency_key': 't9'}).encode('ascii'),
                    {'Content-Type': 'application/json'})
                self.assertIn(reply.status, (400, 413), reply.body)
                self.err(reply)
        self.assertEqual(self.count(), 3)

    def test_body_limit_is_64_kib_in_utf8_bytes(self):
        self.assertEqual(self.submit(body='a' * 65536, idem='b1').status, 201)
        self.assertEqual(self.submit(body='한' * 21845, idem='b2').status, 201)  # 65535 bytes
        for body in ('a' * 65537, '한' * 21846, '😀' * 16385):
            self.refused(self.submit(body=body, idem='b9'), 413, 'too_large', 2)
        self.err(self.submit(body='', idem='b8'), None)  # empty: refused (400 or 413)
        self.assertEqual(self.count(), 2)

    def test_comment_limit_is_1_byte_to_16_kib(self):
        rid = self.make()
        client = self.hub.rest(self.r1)
        self.assertEqual(client.post(f'/requests/{rid}/comments', {'body': 'x'}).status, 201)
        self.assertEqual(client.post(f'/requests/{rid}/comments', {'body': 'a' * 16384}).status, 201)
        for body in ('a' * 16385, '한' * 5462):
            self.err(client.post(f'/requests/{rid}/comments', {'body': body}), 413, 'too_large')
        self.err(client.post(f'/requests/{rid}/comments', {'body': ''}))
        count = self.hub.inbox_rows('SELECT count(*) FROM comments WHERE request_id = ?', rid)[0][0]
        self.assertEqual(count, 2)

    def test_search_query_limit_is_200_characters(self):
        for path in ('/projects', '/requests'):
            client = self.hub.rest(self.ed)
            self.assertEqual(client.get(path, query='q' * 200).status, 200)
            self.err(client.get(path, query='q' * 201), 400, 'invalid_input')

    def test_the_http_body_limit_is_256_kib_by_declared_length_and_by_actual_bytes(self):
        base = json.dumps({'project_id': self.hub.a, 'title': 't', 'body': 'b', 'idempotency_key': 'big'})
        exact = (base + ' ' * (262144 - len(base))).encode()
        self.assertEqual(len(exact), 262144)
        ok = self.raw_post('/requests', exact)
        self.assertEqual(ok.status, 201, ok.body)
        over = (base + ' ' * (262145 - len(base))).encode()
        self.refused(self.raw_post('/requests', over), 413, 'too_large', 1)
        self.refused(self.raw_post('/requests', b'x' * 5_000_000), 413, 'too_large', 1)

    def test_chunked_bodies_obey_the_same_limit(self):
        payload = self.submit_json(idempotency_key='chunk')
        conn = Conn(self.hub.port)
        self.addCleanup(conn.close)
        head = http_bytes(self.hub, 'POST', '/api/v1/requests', self.r1, extra=(('Transfer-Encoding', 'chunked'),))
        conn.send(head + f'{len(payload):x}\r\n'.encode() + payload + b'\r\n0\r\n\r\n')
        small = conn.read_reply()
        self.assertEqual(small.status, 201, small.body)
        big = Conn(self.hub.port)
        self.addCleanup(big.close)
        big.send(head)
        chunk = b'a' * 65536
        with contextlib.suppress(OSError):
            for _ in range(6):
                big.send(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
            big.send(b'0\r\n\r\n')
        with contextlib.suppress(OSError):
            reply = big.read_reply()
            if reply is not None:
                self.err(reply, 413, 'too_large')
        self.assertEqual(self.count(), 1)


# ================================================================================================
# 5. Strict JSON bodies
# ================================================================================================

class StrictBodyTests(Base):
    def assert_alive(self):
        self.assertEqual(self.hub.rest(self.r1).get('/projects').status, 200)

    def refuse(self, body: bytes, status: int = 400, code: str = 'invalid_input', path: str = '/requests',
               key: str | None = None, method: str = 'POST') -> None:
        self.err(self.raw_post(path, body, key, method=method), status, code)
        self.assertEqual(self.count(), 0)

    def test_non_strict_or_non_object_json_is_400(self):
        good = self.submit_json()
        bodies = [b'', b' ', b'null', b'true', b'123', b'"str"', b'[]', b'[' + good + b']', good + b' x', good + good,
                  b'{', good[:-1], b"{'project_id': 'x'}", b'{"a":1,}', good.replace(b'"t"', b'NaN'),
                  b'\xef\xbb\xbf' + good,  # a BOM is not strict JSON text
                  b'\xff\xfe{\x00}\x00', good.replace(b'"t"', b'"\xff"'), good.replace(b'"t"', b'"\xc0\xaf"'),
                  good.replace(b'"t"', b'"\xe4\xb8"'), good + b'\xff', good.replace(b'"b"', b'"\xed\xa0\x80"')]
        for body in bodies:
            with self.subTest(body=body[:40]):
                self.refuse(body)
        self.assert_alive()

    def test_nan_infinity_and_overflowing_numbers_are_400_everywhere(self):
        for literal in (b'NaN', b'Infinity', b'-Infinity', b'1e999', b'-1e999', b'1' + b'0' * 5000, b'1.5e400'):
            with self.subTest(literal=literal[:12]):
                body = b'{"base_version": ' + literal + b', "title": "x"}'
                self.refuse(body, path=f'/requests/{uuid.uuid4()}', key=self.ed, method='PATCH')
        self.assert_alive()

    def test_deep_nesting_is_400_and_the_server_stays_up(self):
        for depth in (1000, 50000, 130000):
            for body in (b'{"title":' + b'[' * depth + b']' * depth + b'}',
                         b'{"a":' * depth + b'1' + b'}' * depth,
                         b'[' * depth):
                if len(body) > 262144:
                    continue
                with self.subTest(depth=depth, size=len(body)):
                    self.refuse(body)
        self.assert_alive()

    def test_types_unknown_fields_and_nulls(self):
        self.refuse(self.submit_json(extra='x'))
        self.refuse(self.submit_json(request_id=str(uuid.uuid4())))
        for field, value in (('title', 5), ('title', ['a']), ('body', {}), ('project_id', 7), ('idempotency_key', True),
                             ('title', None), ('body', None), ('project_id', None)):
            with self.subTest(field=field, value=value):
                self.refuse(self.submit_json(**{field: value}))
        rid = self.make(idem='ok')
        client = self.hub.rest(self.r1)
        self.err(client.post(f'/requests/{rid}/comments', {'body': 'x', 'request_id': rid}), 400, 'invalid_input')
        self.err(client.post(f'/requests/{rid}/comments', {'body': None}), 400, 'invalid_input')
        self.err(client.post(f'/requests/{rid}/comments', {}), 400, 'invalid_input')
        # null for an optional field means "absent"
        edit = self.hub.rest(self.ed).patch(f'/requests/{rid}', {'base_version': 1, 'title': 'new', 'body': None})
        self.assertEqual(edit.status, 200, edit.body)
        self.assertEqual(self.hub.inbox_rows('SELECT title, body FROM requests WHERE request_id = ?', rid),
                         [('new', 'b')])
        cancelled = client.post(f'/requests/{rid}/cancel', {'confirm_token': None, 'reason': None})
        self.assertEqual(cancelled.status, 200, cancelled.body)

    def test_content_type_rules(self):
        body = self.submit_json()
        for ctype in ('text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data; boundary=x',
                      'application/jsonx', 'application/json5', 'text/json', 'application/octet-stream'):
            with self.subTest(ctype=ctype):
                self.err(self.raw_post('/requests', body, ctype=ctype), 415, 'invalid_input')
        missing = self.hub.rest(self.r1).raw('POST', '/api/v1/requests', body, {'Content-Type': None})
        self.err(missing, 415, 'invalid_input')
        self.assertEqual(self.count(), 0)
        for index, ctype in enumerate(('application/json; charset=utf-8', 'application/json;charset=UTF-8',
                                       'APPLICATION/JSON')):
            with self.subTest(accepted=ctype):
                reply = self.raw_post('/requests', self.submit_json(idempotency_key=f'ct{index}'), ctype=ctype)
                self.assertEqual(reply.status, 201, reply.body)

    def test_methods_other_than_the_documented_ones_are_405_with_allow(self):
        rid = str(uuid.uuid4())
        allowed = {'/projects': 'GET', '/requests': 'GET, POST', f'/requests/{rid}': 'GET, PATCH',
                   f'/requests/{rid}/comments': 'POST', f'/requests/{rid}/state': 'POST',
                   f'/requests/{rid}/cancel': 'POST', f'/requests/{rid}/result': 'GET'}
        client = self.hub.rest(self.ed)
        for path, allow in allowed.items():
            for method in ('PUT', 'DELETE', 'OPTIONS', 'HEAD', 'TRACE', 'PROPFIND', 'GET', 'POST', 'PATCH'):
                if method in allow.split(', '):
                    continue
                with self.subTest(method=method, path=path):
                    reply = client.raw(method, '/api/v1' + path, None if method in ('HEAD', 'GET') else b'{}',
                                       {'Content-Type': 'application/json',
                                        'Origin': f'http://127.0.0.1:{self.hub.port}'})
                    self.assertEqual(reply.status, 405, reply.body)
                    self.assertEqual(sorted(reply.header('allow').split(', ')), sorted(allow.split(', ')))
                    self.assertFalse([name for name in reply.headers if name.startswith('access-control')])
                    self.assertEqual(reply.header('cache-control'), 'no-store')
                    if method != 'HEAD':
                        self.err(reply, 405, 'invalid_input')
        self.assertEqual(self.count(), 0)

    def test_no_cors_headers_on_any_kind_of_answer(self):
        probes = [self.hub.rest(self.ed).get('/projects', headers={'Origin': f'http://127.0.0.1:{self.hub.port}'}),
                  self.hub.rest(None).get('/projects', headers={'Origin': 'http://evil.example'}),
                  self.hub.rest(self.ed).get('/projects', headers={'Origin': 'http://evil.example'}),
                  self.hub.rest(None).raw('OPTIONS', '/api/v1/requests', None,
                                          {'Origin': 'http://evil.example',
                                           'Access-Control-Request-Method': 'POST',
                                           'Access-Control-Request-Headers': 'authorization'})]
        for reply in probes:
            self.assertFalse([name for name in reply.headers if name.startswith('access-control')], reply.headers)
        self.assertIn(probes[2].status, (403,))


class QueryAndPathTests(Base):
    def test_query_parameter_rules(self):
        client = self.hub.rest(self.ed)
        bad = [('limit', '1.5'), ('limit', '-1'), ('limit', '+1'), ('limit', '١٢'), ('limit', ' 1'), ('limit', '1 '),
               ('limit', '0x1'), ('limit', '1e1'), ('limit', ''), ('limit', '000000000000000000000000001'),
               ('include_closed', 'TRUE'), ('include_closed', 'yes'), ('include_closed', '1'), ('include_closed', ''),
               ('state', 'NOPE'), ('state', 'new'), ('state', ''), ('project_id', None)]
        for name, value in bad:
            if value is None:
                continue
            with self.subTest(name=name, value=value):
                reply = client.get('/requests', **{name: value})
                self.err(reply, 400, 'invalid_input')
        for name, value in (('limit', 5), ('include_closed', 'false'), ('state', 'NEW')):
            self.assertEqual(client.get('/requests', **{name: value}).status, 200)
        # a parameter given twice (other than state) is refused, not "first wins" or "last wins"
        for path in ('/api/v1/requests?limit=1&limit=2', '/api/v1/requests?include_closed=true&include_closed=false',
                     '/api/v1/requests?query=a&query=b', '/api/v1/projects?query=a&query=b'):
            with self.subTest(path=path):
                self.err(client.raw('GET', path), 400, 'invalid_input')
        self.assertEqual(client.raw('GET', '/api/v1/requests?state=NEW&state=READY&state=CLOSED').status, 200)

    def test_hostile_percent_encodings_never_5xx(self):
        client = self.hub.rest(self.ed)
        rid = self.make()
        paths = ['/api/v1/projects?query=%ff%fe', '/api/v1/requests?query=%00', '/api/v1/requests?query=%ED%A0%80',
                 '/api/v1/requests?cursor=%ff', f'/api/v1/requests/{rid}?x=%',
                 f'/api/v1/requests/{rid}/result?full=true&artifact_path=%ff', '/api/v1/requests/%ff',
                 '/api/v1/requests/%00', '/api/v1/requests/' + 'a' * 6000, '/api/v1/requests/%2e%2e',
                 '/api/v1/requests/..%2f..%2fetc%2fpasswd', '/api/v1/requests/a%2Fb/comments',
                 '/api/v1/requests/' + rid + '%0a', '/api/v1/requests/' + rid.upper(),
                 '/api/v1/requests/%E4%B8%AD', '/api/v1/projects/../requests', '/api/v1/requests/./' + rid]
        for path in paths:
            with self.subTest(path=path[:60]):
                reply = client.raw('GET', path)
                self.assertLess(reply.status, 500, reply.body)
                if reply.status != 200:
                    self.err(reply)

    def test_unknown_paths_and_ids_are_404_after_authentication(self):
        client = self.hub.rest(self.ed)
        for path in ('/api/v1/', '/api/v1/nowhere', '/api/v1/requests/', '/api/v1/Requests', '/api/v1/requests//comments',
                     f'/api/v1/requests/{uuid.uuid4()}/unknown', '/api/v1/projects/x', '/api/v1/requests/a/b/c/d',
                     '/api/v1/mcp', '/api/v1/terminal', '/api/v1/keys', '/api/v1/ws/board', '/api/v1/skill/x'):
            with self.subTest(path=path):
                self.err(client.raw('GET', path), 404, 'not_found')
                self.err(client.raw('POST', path, b'{}', {'Content-Type': 'application/json'}), 404, 'not_found')
        for rid in ('not-a-uuid', str(uuid.uuid4()), '0' * 36, 'x' * 300):
            self.err(client.get(f'/requests/{rid}'), 404, 'not_found')
            self.err(client.post(f'/requests/{rid}/comments', {'body': 'x'}), 404, 'not_found')
            self.err(client.get(f'/requests/{rid}/result'), 404, 'not_found')
            self.err(client.post(f'/requests/{rid}/cancel', {}), 404, 'not_found')

    def test_every_answer_has_the_security_headers_and_no_set_cookie(self):
        rid = self.make()
        client = self.hub.rest(self.r1)
        replies = [client.get('/projects'), client.get(f'/requests/{rid}'), client.get('/requests'),
                   self.submit(idem='h2'), client.get('/nowhere'), client.get('/requests/nope'),
                   self.hub.rest(None).get('/projects'), client.raw('PUT', '/api/v1/projects'),
                   client.raw('POST', '/api/v1/requests', b'{', {'Content-Type': 'application/json'}),
                   client.raw('POST', '/api/v1/requests', b'{}', {'Content-Type': 'text/plain'}),
                   client.get('/projects', headers={'Origin': 'http://x.example'}),
                   client.get('/projects', headers={'Cookie': 'wb_hub_session=garbage'}),
                   client.post(f'/requests/{rid}/comments', {'body': 'c'})]
        for reply in replies:
            with self.subTest(status=reply.status, body=reply.body[:40]):
                self.assertEqual(reply.header('content-type').split(';')[0], 'application/json')
                self.assertEqual(reply.header('cache-control'), 'no-store')
                self.assertEqual(reply.header('x-content-type-options'), 'nosniff')
                self.assertIsNone(reply.header('set-cookie'))
                self.assertTrue(reply.body.isascii())


# ================================================================================================
# 6. Lying Content-Length and stalled bodies
# ================================================================================================

class LyingLengthTests(Base):
    def conn(self) -> Conn:
        conn = Conn(self.hub.port)
        self.addCleanup(conn.close)
        return conn

    def alive(self):
        self.assertEqual(self.hub.rest(self.r1).get('/projects').status, 200)

    def test_a_declared_oversize_length_is_413_before_any_body_arrives(self):
        conn = self.conn()
        conn.send(http_bytes(self.hub, 'POST', '/api/v1/requests', self.r1, length='300000', body=b'{"project'))
        started = time.monotonic()
        reply = conn.read_reply()
        self.assertIsNotNone(reply)
        self.err(reply, 413, 'too_large')
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.count(), 0)
        self.alive()

    def test_a_length_longer_than_the_body_then_close_stores_nothing_and_logs_no_traceback(self):
        capture = LogCapture()
        logging.getLogger().addHandler(capture)
        self.addCleanup(logging.getLogger().removeHandler, capture)
        old_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().setLevel, old_level)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            conn = self.conn()
            body = self.submit_json()
            conn.send(http_bytes(self.hub, 'POST', '/api/v1/requests', self.r1, length=str(len(body) + 400),
                                 body=body))
            conn.sock.shutdown(socket.SHUT_WR)
            reply = conn.read_reply()
            time.sleep(0.3)
        if reply is not None:
            self.assertEqual(reply.status, 400, reply.body)
        self.assertEqual(self.count(), 0)
        self.alive()
        bad = [record for record in capture.records
               if record.exc_info or (record.levelno >= logging.ERROR and record.name.startswith('aiohttp'))]
        self.assertEqual([(record.name, record.getMessage()[:600]) for record in bad], [])
        self.assertNotIn('Traceback', err.getvalue())

    def test_a_length_shorter_than_the_body_never_runs_the_cut_off_or_extra_bytes(self):
        conn = self.conn()
        body = self.submit_json()
        conn.send(http_bytes(self.hub, 'POST', '/api/v1/requests', self.r1, length='10', body=body))
        reply = conn.read_reply()
        self.assertIsNotNone(reply)
        self.assertEqual(reply.status, 400, reply.body)  # truncated JSON: nothing stored
        self.assertEqual(self.count(), 0)
        self.alive()

    def test_pipelined_requests_after_an_exact_length_do_not_desync(self):
        conn = self.conn()
        body = self.submit_json(idempotency_key='pipe')
        conn.send(http_bytes(self.hub, 'POST', '/api/v1/requests', self.r1, length=str(len(body)), body=body)
                  + http_bytes(self.hub, 'GET', '/api/v1/projects', self.r1, ctype=None))
        first, second = conn.read_reply(), conn.read_reply()
        self.assertEqual((first.status, second.status), (201, 200), (first.body, second.body))
        self.assertEqual(self.count(), 1)

    def test_malformed_or_ambiguous_length_headers_are_refused_and_store_nothing(self):
        body = self.submit_json()
        cases = [('abc', ()), ('-1', ()), ('+5', ()), ('1 2', ()), ('0x10', ()), ('', ()),
                 (str(len(body)), (('Content-Length', str(len(body) + 5)),)),  # two different lengths
                 (str(len(body)), (('Transfer-Encoding', 'chunked'),)),  # request smuggling shape
                 (str(len(body)), (('Transfer-Encoding', 'identity, chunked'),))]
        for length, extra in cases:
            with self.subTest(length=length, extra=extra):
                conn = Conn(self.hub.port)
                try:
                    conn.send(http_bytes(self.hub, 'POST', '/api/v1/requests', self.r1, length=length, body=body,
                                         extra=extra))
                    reply = conn.read_reply()
                finally:
                    conn.close()
                if reply is not None:
                    self.assertEqual(reply.status, 400, reply.body)
                self.assertEqual(self.count(), 0)
        self.alive()

    def test_stalled_bodies_hold_no_api_slot_and_do_not_block_others(self):
        stalled = []
        for _ in range(10):  # more than the 4 slots per key and the 8 workers
            conn = self.conn()
            conn.send(http_bytes(self.hub, 'POST', '/api/v1/requests', self.r1, length='5000', body=b'{"a"'))
            stalled.append(conn)
        time.sleep(0.3)
        started = time.monotonic()
        self.assertEqual(self.hub.rest(self.r1).get('/projects').status, 200)
        self.assertEqual(self.submit(self.r1, idem='during').status, 201)
        self.assertLess(time.monotonic() - started, 5)
        for conn in stalled:
            conn.close()
        time.sleep(0.2)
        self.assertEqual(self.count(), 1)
        self.alive()


# ================================================================================================
# 7. Two-step cancel
# ================================================================================================

COMMAND = {'command_id': 'cmd-1', 'label': 'train', 'state': 'running'}


class CancelTokenTests(Base):
    def setUp(self):
        super().setUp()
        self.rid = self.make(self.r1, idem='c1')
        self.hub.link_running(self.rid, view={'status': 'running', 'command': COMMAND})

    def cancel(self, key=None, rid=None, **body) -> Reply:
        return self.hub.rest(key or self.r1).post(f'/requests/{rid or self.rid}/cancel', body)

    def first(self, key=None, rid=None) -> str:
        reply = self.cancel(key, rid)
        self.assertEqual((reply.status, reply.json['status']), (200, 'confirm_required'), reply.body)
        return reply.json['confirm_token']

    def row(self, rid=None) -> tuple:
        return self.hub.inbox_rows('SELECT state, outcome, cancel_state FROM requests WHERE request_id = ?',
                                   rid or self.rid)[0]

    def untouched(self, rid=None):
        self.assertEqual(self.hub.cancels, [])
        self.assertEqual(self.row(rid), ('RUNNING', None, None))

    def test_the_first_call_changes_nothing_and_the_right_token_cancels_exactly_once(self):
        reply = self.cancel()
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.json['expires_in'], 60)
        self.assertEqual(reply.json['impact']['task_id'], 'task-1')
        token = reply.json['confirm_token']
        self.assertGreaterEqual(len(token), 32)
        self.untouched()
        confirmed = self.cancel(confirm_token=token)
        self.assertEqual(confirmed.status, 202, confirmed.body)
        self.assertEqual(confirmed.json['status'], 'cancel_requested')
        self.assertEqual([event.request_id for event in self.hub.cancels], [self.rid])
        again = self.cancel(confirm_token=token)  # reuse
        self.err(again, 409, 'token_mismatch')
        self.assertEqual(len(self.hub.cancels), 1)

    def test_unknown_malformed_and_empty_tokens_are_token_mismatch(self):
        self.first()
        for token in ('', 'x', 'A' * 32, secrets_like(), ' ', '../..', 'a' * 5000):
            with self.subTest(token=token[:10]):
                self.err(self.cancel(confirm_token=token), 409, 'token_mismatch')
        for token in (5, True, [], {}, 1.5):
            with self.subTest(token=token):
                self.err(self.cancel(confirm_token=token), 400, 'invalid_input')
        self.untouched()

    def test_another_key_another_request_and_a_revoked_key_cannot_use_the_token(self):
        token = self.first()
        other_editor = self.hub.key('e2', 'editor')
        self.err(self.cancel(self.ed, confirm_token=token), 409, 'token_mismatch')
        self.err(self.cancel(other_editor, confirm_token=token), 409, 'token_mismatch')
        self.err(self.cancel(self.r2, confirm_token=token), 404, 'not_found')  # r2 cannot even see the request
        second = self.make(self.r1, idem='c2')
        self.hub.link_running(second, task_id='task-2', view={'status': 'running', 'command': COMMAND})
        self.err(self.cancel(rid=second, confirm_token=token), 409, 'token_mismatch')
        self.untouched()
        self.untouched(second)
        # a token issued to a key dies with the key
        mine = self.hub.key('r9')
        rid3 = self.make(mine, idem='c3')
        self.hub.link_running(rid3, task_id='task-3', view={'status': 'running', 'command': COMMAND})
        token3 = self.first(mine, rid3)
        self.hub.keys.revoke(mine[4:14], now=self.hub.now[0])
        self.err(self.cancel(mine, rid=rid3, confirm_token=token3), 401, 'unauthorized')
        self.assertEqual(self.hub.cancels, [])

    def test_expiry_uses_the_injected_monotonic_clock(self):
        token = self.first()
        self.hub.mono[0] += 59.0
        self.hub.now[0] += 1_000_000.0  # wall time is irrelevant to the TTL
        confirmed = self.cancel(confirm_token=token)
        self.assertEqual(confirmed.status, 202, confirmed.body)
        self.hub.cancels.clear()
        # a fresh run: a token one tick past 60 s is dead
        rid = self.make(self.r1, idem='c4')
        self.hub.link_running(rid, task_id='task-4', view={'status': 'running', 'command': COMMAND})
        late = self.first(rid=rid)
        self.hub.mono[0] += 61.0
        self.err(self.cancel(rid=rid, confirm_token=late), 409, 'token_mismatch')
        self.assertEqual(self.hub.cancels, [])
        self.assertEqual(self.row(rid), ('RUNNING', None, None))

    def test_any_user_visible_change_between_the_calls_kills_the_token(self):
        token = self.first()
        self.assertEqual(self.hub.rest(self.ed).post(f'/requests/{self.rid}/comments', {'body': 'wait'}).status, 201)
        self.err(self.cancel(confirm_token=token), 409, 'token_mismatch')
        self.untouched()
        token = self.first()  # a fresh token for the new state works
        self.assertEqual(self.cancel(confirm_token=token).status, 202)

    def test_edit_of_a_running_request_changes_the_fingerprint(self):
        token = self.first()
        version = self.hub.rest(self.ed).get(f'/requests/{self.rid}').json['request']['version']
        edit = self.hub.rest(self.ed).patch(f'/requests/{self.rid}', {'base_version': version, 'title': 'changed'})
        self.assertEqual(edit.status, 200, edit.body)
        self.err(self.cancel(confirm_token=token), 409, 'token_mismatch')
        self.assertEqual(self.hub.cancels, [])

    def test_a_token_never_cancels_a_request_that_no_longer_needs_two_steps(self):
        rid = self.make(self.r1, idem='c5')  # NEW, no task: one step
        for token in (self.first(), 'x'):
            self.err(self.cancel(rid=rid, confirm_token=token), 409, 'token_mismatch')
        self.assertEqual(self.hub.inbox_rows('SELECT state FROM requests WHERE request_id = ?', rid), [('NEW',)])

    def test_at_most_32_live_tokens_per_key_the_oldest_is_dropped(self):
        tokens = [self.first() for _ in range(33)]
        self.err(self.cancel(confirm_token=tokens[0]), 409, 'token_mismatch')
        self.assertEqual(self.hub.cancels, [])
        self.assertEqual(self.cancel(confirm_token=tokens[-1]).status, 202)

    def test_the_token_is_not_shown_by_other_endpoints_and_the_first_call_is_not_cached(self):
        token = self.first()
        for reply in (self.hub.rest(self.ed).get(f'/requests/{self.rid}'), self.hub.rest(self.ed).get('/requests'),
                      self.hub.rest(self.r1).get(f'/requests/{self.rid}/result', full=True)):
            self.assertNotIn(token, reply.body.decode())
            self.assertEqual(reply.header('cache-control'), 'no-store')
        self.assertNotEqual(self.first(), token)

    def test_backend_off_refuses_the_confirmation_and_changes_nothing(self):
        token = self.first()
        self.hub.backend_up = False
        self.err(self.cancel(confirm_token=token), 409, 'backend_not_running')
        self.untouched()
        self.hub.backend_up = True
        self.assertEqual(self.cancel(confirm_token=self.first()).status, 202)

    def test_ready_and_before_dispatch_need_no_token_and_do_not_issue_one(self):
        before = self.make(self.r1, idem='c6')
        reply = self.cancel(rid=before)
        self.assertEqual((reply.status, reply.json), (200, {'status': 'cancelled', 'state': 'CLOSED'}))
        ready = self.make(self.r1, idem='c7')
        self.hub.link_running(ready, task_id='task-7', status='dispatched')
        reply = self.cancel(rid=ready)
        self.assertEqual((reply.status, reply.json['status']), (202, 'cancel_requested'))
        self.assertNotIn('confirm_token', reply.json)
        self.err(self.cancel(rid=before), 409)  # already closed: a conflict class error, never a second success

    def test_a_cancel_of_someone_elses_request_by_an_editor_marks_the_origin_correctly(self):
        token = self.first(self.ed)
        self.assertEqual(self.cancel(self.ed, confirm_token=token).status, 202)
        self.assertEqual(len(self.hub.cancels), 1)


def secrets_like() -> str:
    import secrets
    return secrets.token_urlsafe(24)


# ================================================================================================
# 8. Results: raw / artifact path traversal and paging
# ================================================================================================

class ResultPathTests(Base):
    def setUp(self):
        super().setUp()
        import test_inbox_results as fixtures  # module import only: its test classes stay uncollected
        from workbench.hub.inbox import results
        self.fx = fixtures
        self.backend = fixtures.FakeBackend()
        self.hub.api.results = results.Results(self.hub.inbox, port=self.backend)
        self.rid = self.make(self.r1, idem='res')
        self.hub.link_running(self.rid, task_id=fixtures.TASK, view={'status': 'running', 'runs': [
            {'run_id': fixtures.RUN, 'status': 'running', 'exit_code': None}]})
        root = self.hub.root_a
        outside = self.hub.base / 'outside.txt'
        outside.write_text('OUTSIDE-SECRET')
        (self.hub.root_b / 'beta.txt').write_text('BETA-SECRET')
        (root / 'out.txt').write_text('ARTIFACT-OK')
        (root / 'sub').mkdir()
        (root / 'sub' / 'inner.txt').write_text('INNER-OK')
        (root / '.env').write_text('SECRET-ENV')
        (root / 'deploy.pem').write_text('SECRET-PEM')
        (root / '.git').mkdir()
        (root / '.git' / 'config').write_text('GIT-CONFIG-MARK')
        (root / 'sub' / '.git').mkdir()
        (root / 'sub' / '.git' / 'config').write_text('GIT-CONFIG-MARK')
        os.symlink('out.txt', root / 'ln_file')
        os.symlink(outside, root / 'ln_out')
        os.symlink('../outside.txt', root / 'ln_rel_out')
        os.symlink(self.hub.base, root / 'ln_dir')
        os.symlink('.wb/inbox.sqlite3', root / 'ln_wb')
        os.symlink('sub', root / 'ln_sub')
        os.link(outside, root / 'hard.txt')
        os.mkfifo(root / 'fifo')
        self.big = bytes((index * 7 + index // 251) % 256 for index in range(300_000))
        (root / 'big.bin').write_bytes(self.big)
        self.client = self.hub.rest(self.r1)

    FORBIDDEN_TEXT = ('OUTSIDE-SECRET', 'BETA-SECRET', 'SECRET-ENV', 'SECRET-PEM', 'GIT-CONFIG-MARK', 'SQLite format',
                      'root:x:')

    def artifact(self, path, key=None, **query) -> Reply:
        client = self.hub.rest(key) if key else self.client
        return client.get(f'/requests/{self.rid}/result', full=True, artifact_path=path, **query)

    def assert_refused(self, reply: Reply) -> None:
        self.assertIn(reply.status, (400, 403, 404), reply.body[:300])
        self.err(reply)
        text = reply.body.decode('utf-8', 'replace')
        for marker in self.FORBIDDEN_TEXT:
            self.assertNotIn(marker, text)
            self.assertNotIn(base64.b64encode(marker.encode()).decode()[:12], text)

    def test_control_the_legitimate_files_are_readable(self):
        for path, want in (('out.txt', 'ARTIFACT-OK'), ('sub/inner.txt', 'INNER-OK'), ('./out.txt', 'ARTIFACT-OK')):
            with self.subTest(path=path):
                reply = self.artifact(path)
                self.assertEqual(reply.status, 200, reply.body)
                self.assertEqual(reply.json['artifact']['text'], want)

    def test_traversal_absolute_excluded_and_symlink_paths_are_refused(self):
        root = self.hub.root_a
        cases = ['..', '../outside.txt', '../../etc/passwd', 'sub/../../outside.txt', 'sub/../out.txt', './../outside.txt',
                 'sub/..', '..%2foutside.txt', '%2e%2e/outside.txt', '....//outside.txt', '/etc/passwd', '/proc/self/environ',
                 '/dev/zero', '/dev/null', str(self.hub.base / 'outside.txt'), str(self.hub.root_b / 'beta.txt'),
                 str(root / '..' / 'outside.txt'), '//etc/passwd', 'file:///etc/passwd', '~/.ssh/id_rsa',
                 '.wb/inbox.sqlite3', '.wb', '.wb/', '.WB/inbox.sqlite3', './.wb/inbox.sqlite3', 'sub/../.wb/x',
                 str(root / '.wb' / 'inbox.sqlite3'), '.git/config', '.git', '.GIT/config', 'sub/.git/config',
                 str(root / '.git' / 'config'), '.env', 'deploy.pem', 'sub/../.env', 'ln_file', 'ln_out',
                 'ln_rel_out', 'ln_dir/outside.txt', 'ln_dir', 'ln_wb', 'ln_sub/inner.txt', 'hard.txt',
                 'out.txt/..', 'out.txt/x', 'sub', 'sub/', '.', './', '\\..\\outside.txt', 'a' * 5000,
                 'sub/' * 500 + 'x']
        for path in cases:
            with self.subTest(path=path[:70]):
                self.assert_refused(self.artifact(path))

    def test_nul_empty_and_missing_paths_are_refused_without_a_leak(self):
        for path in ('', 'a\x00b', 'out.txt\x00.png', 'missing.txt', 'sub/missing.txt', '%2e%2e/%2e%2e/etc/passwd'):
            with self.subTest(path=path[:20]):
                self.assert_refused(self.artifact(path))
        self.assert_refused(self.client.raw('GET', f'/api/v1/requests/{self.rid}/result?full=true&artifact_path=a%00b'))
        self.assert_refused(self.client.raw('GET', f'/api/v1/requests/{self.rid}/result?full=true&artifact_path=%ff%fe'))

    def test_a_fifo_is_refused_quickly_not_blocked_on(self):
        started = time.monotonic()
        self.assert_refused(self.artifact('fifo'))
        self.assertLess(time.monotonic() - started, 5)

    def test_missing_and_protected_look_alike_in_what_they_reveal(self):
        existing, missing = self.artifact('.wb/inbox.sqlite3'), self.artifact('.wb/definitely-missing')
        self.assertEqual((existing.status, existing.json), (missing.status, missing.json))
        existing, missing = self.artifact('.env'), self.artifact('.env.nope')
        self.assertIn(existing.status, (403, 404))

    def test_run_id_cannot_name_a_file_or_a_foreign_source(self):
        for run_id in ('../../etc/passwd', '/etc/passwd', '', 'x' * 5000, str(uuid.uuid4()), self.fx.RUN + '/../x',
                       self.fx.RUN.upper(), self.fx.RUN + ' ', 'a\x00b'):
            with self.subTest(run_id=run_id[:30]):
                reply = self.client.get(f'/requests/{self.rid}/result', full=True, run_id=run_id)
                self.assertIn(reply.status, (400, 404), reply.body)
                self.err(reply)
        self.assertEqual(self.backend.calls[-1][0] if self.backend.calls else 'task_status', 'task_status')
        self.assertEqual([call for call in self.backend.calls if call[0] == 'raw_query'], [])

    def test_other_keys_cannot_read_artifacts_or_raw_pages_of_my_request(self):
        for key in (self.r2, self.hub.key('e9', 'editor', {'projects': [self.hub.b]})):
            self.err(self.artifact('out.txt', key), 404, 'not_found')
            self.err(self.hub.rest(key).get(f'/requests/{self.rid}/result', full=True, run_id=self.fx.RUN),
                     404, 'not_found')
        self.assertEqual(self.artifact('out.txt', self.ed).status, 200)  # an editor in scope may

    def test_a_request_without_a_linked_task_has_no_artifacts(self):
        bare = self.make(self.r1, idem='bare')
        reply = self.client.get(f'/requests/{bare}/result', full=True, artifact_path='out.txt')
        self.err(reply, 404, 'not_found')

    def test_artifact_paging_reassembles_the_file_exactly(self):
        data, offset, pages = b'', 0, 0
        while offset is not None:
            reply = self.artifact('big.bin', offset=offset)
            self.assertEqual(reply.status, 200, reply.body)
            page = reply.json['artifact']
            self.assertEqual((page['offset'], page['size']), (offset, len(self.big)))
            self.assertLessEqual(page['length'], 131072)
            data += base64.b64decode(page['data_base64']) if 'data_base64' in page else page['text'].encode()
            offset = page['next_offset']
            pages += 1
            self.assertLess(pages, 10)
        self.assertEqual(data, self.big)
        self.assertEqual(pages, 3)

    def test_paging_arguments_are_checked(self):
        for query in ({'length': 0}, {'length': 131073}, {'offset': 300_001}, {'offset': 99_999_999_999_999_999},
                      {'offset': 9_223_372_036_854_775_807}, {'offset': 10 ** 30}, {'length': '9' * 30},
                      {'offset': '-1'}, {'offset': 'x'}, {'length': 1.5}):
            with self.subTest(query=query):
                reply = self.artifact('big.bin', **query)
                self.err(reply, 400, 'invalid_input')
        tail = self.artifact('big.bin', offset=300_000)
        self.assertEqual(tail.status, 200, tail.body)
        self.assertEqual((tail.json['artifact']['length'], tail.json['artifact']['next_offset']), (0, None))
        one = self.artifact('big.bin', offset=299_999, length=1)
        self.assertEqual((one.status, one.json['artifact']['length']), (200, 1))

    def test_full_view_does_not_show_backend_host_paths(self):
        reply = self.client.get(f'/requests/{self.rid}/result', full=True)
        self.assertEqual(reply.status, 200, reply.body)
        text = reply.body.decode()
        for marker in ('/home/someone', 'log_path', '.wb/raw-logs'):
            self.assertNotIn(marker, text)
        raw = self.client.get(f'/requests/{self.rid}/result', full=True, run_id=self.fx.RUN, length=64)
        self.assertEqual(raw.status, 200, raw.body)
        self.assertNotIn('/home/someone', raw.body.decode())
        self.assertIs(raw.json['untrusted_text'], True)

    def test_the_default_summary_is_marked_untrusted_and_full_needs_the_flag(self):
        summary = self.client.get(f'/requests/{self.rid}/result')
        self.assertNotIn('full', summary.json)
        self.assertEqual(self.backend.calls, [])
        self.assertIs(summary.json['untrusted_text'], True)


# ================================================================================================
# 9. Error normalisation: no internal sentence, path or stack reaches the client or the log
# ================================================================================================

PASSTHROUGH_CODES = {'invalid_input', 'too_large', 'not_found', 'forbidden', 'invalid_transition', 'conflict',
                     'idempotency_conflict', 'token_mismatch'}  # SPEC: these keep the inbox's own sentence


class ErrorLeakTests(Base):
    SECRET = '/home/victim/.wb/inbox.sqlite3 sentinel-4711'

    def raise_from(self, error: BaseException, target: str = 'list_requests') -> tuple[Reply, str]:
        err = io.StringIO()
        capture = LogCapture()
        logging.getLogger().addHandler(capture)
        try:
            with mock.patch.object(self.hub.inbox, target, side_effect=error), contextlib.redirect_stderr(err):
                reply = self.hub.rest(self.ed).get('/requests')
        finally:
            logging.getLogger().removeHandler(capture)
        logged = err.getvalue() + ' '.join(record.getMessage() for record in capture.records)
        return reply, logged

    def test_unexpected_exceptions_are_500_internal_error_with_a_fixed_sentence(self):
        errors = [RuntimeError(self.SECRET), OSError(13, 'Permission denied', self.SECRET), KeyError(self.SECRET),
                  sqlite3.OperationalError(f'unable to open database file {self.SECRET}'),
                  UnicodeEncodeError('utf-8', self.SECRET, 0, 1, 'surrogates not allowed'),
                  RecursionError(self.SECRET), ZeroDivisionError(self.SECRET), AssertionError(self.SECRET),
                  InboxError('some_unknown_code', self.SECRET), InboxError('internal_error', self.SECRET),
                  InboxError('', self.SECRET)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                reply, logged = self.raise_from(error)
                self.err(reply, 500, 'internal_error')
                self.assertNotIn('sentinel-4711', reply.body.decode())
                self.assertNotIn('victim', reply.body.decode())
                self.assertNotIn('sentinel-4711', logged)
                self.assertNotIn(self.ed, logged)
        self.assertEqual(self.hub.rest(self.ed).get('/projects').status, 200)

    def test_every_documented_code_has_its_spec_status_and_store_failures_get_fixed_sentences(self):
        fixed = {'project_unavailable', 'inbox_busy', 'inbox_mismatch', 'backend_unreachable', 'hub_stopping',
                 'backend_not_running', 'internal_error'}
        for code, status in SPEC_STATUS.items():
            if code in ('unauthorized', 'origin_mismatch', 'busy'):
                continue  # not raised by an operation
            with self.subTest(code=code):
                reply, logged = self.raise_from(InboxError(code, self.SECRET, state='NEW', version=3))
                error = self.err(reply, status, code, clean=code not in PASSTHROUGH_CODES)
                if code in fixed:
                    self.assertNotIn('victim', error['message'])
                    self.assertNotIn('sentinel-4711', reply.body.decode())
                self.assertNotIn('sentinel-4711', logged)
        reply, _ = self.raise_from(InboxError('inbox_unavailable', self.SECRET))  # the documented alias
        self.err(reply, 503, 'project_unavailable')
        self.assertNotIn('victim', reply.body.decode())

    def test_state_and_version_ride_along_only_for_visible_conflicts(self):
        reply, _ = self.raise_from(InboxError('conflict', 'changed', state='REVIEWING', version=7))
        error = self.err(reply, 409, 'conflict')
        self.assertEqual((error['state'], error['version']), ('REVIEWING', 7))
        ghost = self.hub.rest(self.r1).get(f'/requests/{uuid.uuid4()}')
        error = self.err(ghost, 404, 'not_found')
        self.assertNotIn('state', error)
        self.assertNotIn('version', error)

    def test_operation_that_fails_in_a_write_path_leaks_nothing_either(self):
        rid = self.make(idem='w')
        with mock.patch.object(self.hub.inbox, 'comment', side_effect=sqlite3.DatabaseError(self.SECRET)):
            reply = self.hub.rest(self.r1).post(f'/requests/{rid}/comments', {'body': 'x'})
        self.err(reply, 500, 'internal_error')
        with mock.patch.object(self.hub.inbox, 'submit', side_effect=OSError(28, 'No space left', self.SECRET)):
            reply = self.submit(idem='w2')
        self.err(reply, 500, 'internal_error')
        self.assertNotIn('victim', reply.body.decode())

    def test_a_hostile_request_battery_never_writes_keys_or_internals_to_stderr_or_logs(self):
        err = io.StringIO()
        capture = LogCapture()
        root = logging.getLogger()
        root.addHandler(capture)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            with contextlib.redirect_stderr(err):
                client = self.hub.rest(self.r1)
                client.raw('POST', '/api/v1/requests', b'\xff{', {'Content-Type': 'application/json'})
                client.raw('POST', '/api/v1/requests', b'[' * 100000, {'Content-Type': 'application/json'})
                self.hub.rest('wbk_AAAAAAAAAA_' + 'B' * 43).get('/projects')
                client.get('/requests', cursor='zz', limit='x')
                client.get('/requests/%ff')
                conn = Conn(self.hub.port)
                conn.send(http_bytes(self.hub, 'POST', '/api/v1/requests', self.r1, length='999', body=b'{'))
                conn.sock.shutdown(socket.SHUT_WR)
                conn.read_reply()
                conn.close()
                time.sleep(0.3)
        finally:
            root.removeHandler(capture)
            root.setLevel(old)
        everything = err.getvalue() + '\n'.join(record.getMessage() for record in capture.records)
        for secret in (self.r1, self.r1.split('_')[-1], self.ed):
            self.assertNotIn(secret, everything)
        self.assertNotIn('Traceback', everything)
        self.assertEqual([r.getMessage() for r in capture.records if r.exc_info], [])


# ================================================================================================
# 10. Per-key slots and busy
# ================================================================================================

class BusyFairnessTests(Base):
    def test_a_key_that_fills_its_four_slots_gets_busy_after_the_wait_and_other_keys_are_unaffected(self):
        self.hub.api.busy_wait = 0.6
        gate = threading.Event()
        entered = threading.Semaphore(0)
        self.addCleanup(gate.set)

        def blocked(*args, **kwargs):
            entered.release()
            gate.wait(30)
            return {'requests': [], 'next_cursor': None}
        replies: list = []

        def call():
            replies.append(self.hub.rest(self.ed).get('/requests'))
        with mock.patch.object(self.hub.inbox, 'list_requests', side_effect=blocked):
            threads = [threading.Thread(target=call) for _ in range(4)]
            for thread in threads:
                thread.start()
            for _ in range(4):
                self.assertTrue(entered.acquire(timeout=10))
            started = time.monotonic()
            fifth = self.hub.rest(self.ed).get('/requests')
            waited = time.monotonic() - started
            self.err(fifth, 503, 'busy')
            self.assertGreaterEqual(waited, 0.5)  # it waited for a slot; it was not refused at once
            self.assertLess(waited, 8)
            other = self.hub.rest(self.r1).get('/projects')
            self.assertEqual(other.status, 200, other.body)
            self.assertEqual(self.hub.rest(self.r1).get('/projects').status, 200)
            gate.set()
            for thread in threads:
                thread.join(15)
        self.assertEqual(sorted(reply.status for reply in replies), [200] * 4)
        self.assertEqual(self.hub.rest(self.ed).get('/requests').status, 200)  # the slots came back


# ================================================================================================
# 11. S1: a Bearer key opens no session route of the real Hub; REST has no forbidden endpoint
# ================================================================================================

class RealHubRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import ClientSession, DummyCookieJar, test_utils
        from workbench.hub import auth, server
        from workbench.hub.paths import HubLayout
        from workbench.hub.web.app import OriginPolicy, WebServices, create_app
        self.base = Path(tempfile.mkdtemp(prefix='wb-restind-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        state = self.base / 'state'
        state.mkdir(mode=0o700)
        passwords = auth.PasswordStore(state)
        passwords.set_password('correct horse battery')
        db = auth.HubDb.open(state)
        self.addCleanup(db.close)
        self.services = WebServices(passwords, db, OriginPolicy('127.0.0.1'))
        self.hub = server.Hub(HubLayout(self.base / 'hub'), 'detached')
        self.addCleanup(self.hub.inbox.close)
        self.app = create_app(self.services, routes=self.hub.web_routes())
        self.server = test_utils.TestServer(self.app, host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.http = ClientSession(cookie_jar=DummyCookieJar())
        self.addAsyncCleanup(self.http.close)
        self.host = f'127.0.0.1:{self.server.port}'
        self.key = self.services.api_keys.issue('ed', 'editor', {'all': True}, None, now=time.time()).raw

    def routes(self):
        found = []
        for route in self.app.router.routes():
            info = route.resource.get_info()
            path = info.get('path') or info.get('formatter') or info.get('prefix')
            found.append((route.method, path, getattr(route.handler, 'wb_access', None)))
        return found

    async def send(self, method, path, **headers):
        sent = {'Host': self.host, 'Origin': f'http://{self.host}', 'Authorization': f'Bearer {self.key}', **headers}
        async with self.http.request(method, f'http://{self.host}{path}', headers=sent, allow_redirects=False,
                                     data=b'{}' if method != 'GET' else None) as response:
            return response.status, await response.text()

    async def test_a_bearer_key_is_refused_by_every_session_route(self):
        params = {'key_id': 'abcdefghij', 'project_id': str(uuid.uuid4()), 'action': 'power-on',
                  'rid': str(uuid.uuid4()), 'tail': 'x', 'terminal_id': 'a-0123456789ab',  # WBX-D1 Board routes
                  'pane_id': f'{uuid.uuid4()}:host'}  # WBX-E2 pane actions
        checked = 0
        for method, path, access in sorted(self.routes(), key=str):
            if access in ('public', 'own') or path.startswith(('/static', '/api/v1')):
                continue
            concrete = path.format(**params)
            for verb in (('GET', 'POST') if method in ('*',) else (method,)):
                if verb == 'HEAD':
                    continue
                with self.subTest(route=(verb, path)):
                    status, text = await self.send(verb, concrete)
                    self.assertIn(status, (303, 401, 403, 405), text[:200])
                    self.assertNotIn(self.key, text)
                checked += 1
        self.assertGreater(checked, 20)

    async def test_the_rest_routes_are_exactly_the_documented_nine_operations_plus_the_catch_all(self):
        rest = sorted((method, path) for method, path, _ in self.routes() if path.startswith('/api/v1'))
        self.assertEqual({path for _m, path in rest}, {
            '/api/v1/projects', '/api/v1/requests', '/api/v1/requests/{rid}', '/api/v1/requests/{rid}/comments',
            '/api/v1/requests/{rid}/state', '/api/v1/requests/{rid}/cancel', '/api/v1/requests/{rid}/result',
            '/api/v1/{tail}', '/api/v1'})  # '/api/v1': the bare prefix is the catch-all too (coordinator fix)
        for _method, path, access in self.routes():
            if path.startswith('/api/v1'):
                self.assertEqual(access, 'own', path)
        forbidden = ('terminal', 'ws', 'board', 'link', 'power', 'backend', 'review', 'start', 'key', 'password',
                     'audit', 'settings', 'login', 'session', 'confirm', 'pause', 'resume', 'boot', 'skill', 'mcp')
        for _method, path in rest:
            for word in forbidden:
                self.assertNotIn(word, [segment for segment in path.split('/') if segment], path)

    async def test_the_session_endpoints_named_in_the_spec_are_unreachable_with_a_key(self):
        rid, pid = str(uuid.uuid4()), str(uuid.uuid4())
        for method, path in (('GET', '/api/badge'), ('GET', f'/api/projects/{pid}'), ('POST', f'/api/projects/{pid}/power-on'),
                             ('POST', f'/requests/{rid}/start-review'), ('GET', '/ws/board'), ('GET', '/board'),
                             ('GET', '/settings/keys'), ('POST', '/settings/keys'), ('GET', '/'),
                             ('POST', f'/requests/{rid}/cancel'), ('GET', f'/requests/{rid}')):
            with self.subTest(route=(method, path)):
                status, text = await self.send(method, path)
                self.assertIn(status, (303, 401, 403, 404, 405), text[:200])

    async def test_the_real_hub_rest_answers_with_a_key_and_not_without(self):
        status, text = await self.send('GET', '/api/v1/projects')
        self.assertEqual(status, 200, text)
        self.assertEqual(json.loads(text), {'projects': []})
        status, text = await self.send('GET', '/api/v1/projects', Authorization='Bearer nope')
        self.assertEqual((status, json.loads(text)['error']['code']), (401, 'unauthorized'))
        status, text = await self.send('GET', '/api/v1/nowhere')
        self.assertEqual((status, json.loads(text)['error']['code']), (404, 'not_found'))


if __name__ == '__main__':
    unittest.main()
