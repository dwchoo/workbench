"""TI-05 independent verification of the Hub MCP endpoint ``/mcp`` (seam T4, own stdlib JSON-RPC client).

Expected values come from the SPEC ("MCP Streamable HTTP endpoint", "Principal과 권한", "취소", "결과 조회",
"입력 한도"), JSON-RPC 2.0, BRIEF D-12/D-14/D-22/D-26..D-29/D-33/D-41/D-42 and P2-AC-02/03/05/13/14/25..27/
31/41/42/45, and from the exact request sequence real OMP 18.12 sends (PROBE-OMP-MCP.md), not from the
worker's tests. The server side is the existing loopback ``McpHub`` harness (real ``create_app`` + real
``InboxService``); every client below is this file's own ``Rpc`` over ``http.client``. Loopback only, temp
HOME/XDG, ``WORKBENCH_HUB_AUTOSTART=0``. Run with ``discover -s tests/hub -p 'test_hub_mcp*.py'``.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from support.mcp_hub import PASSWORD, McpHub  # noqa: E402

_SANDBOX: dict = {}
SPEC_TOOLS = ['inbox_projects', 'inbox_submit', 'inbox_list', 'inbox_get', 'inbox_comment', 'inbox_update',
              'inbox_set_state', 'inbox_cancel', 'inbox_result']
EDITOR_ONLY = {'inbox_update', 'inbox_set_state'}
SPEC_CODES = {'invalid_input', 'too_large', 'not_found', 'forbidden', 'invalid_transition', 'conflict',
              'idempotency_conflict', 'confirm_required', 'token_mismatch', 'project_unavailable', 'inbox_busy',
              'inbox_mismatch', 'backend_not_running', 'backend_unreachable', 'hub_stopping', 'internal_error'}
COMMAND = {'command_id': 'cmd-1', 'label': 'python train.py', 'state': 'running'}
LEAK = re.compile(r'sqlite|Traceback|\.py\b|File "|/tmp|/home|/dev/shm|\.wb|inbox\.sqlite|OperationalError|errno',
                  re.IGNORECASE)


def setUpModule():
    base = Path(tempfile.mkdtemp(prefix='wbi-'))
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
        shutil.rmtree(base, True)


class Reply:
    def __init__(self, status: int, headers: dict, body: bytes):
        self.status, self.headers, self.body = status, headers, body

    @property
    def json(self):
        return json.loads(self.body.decode('utf-8'))


class Rpc:
    """Independent stdlib JSON-RPC client: a fresh connection per request, headers as OMP sends them."""

    def __init__(self, hub: McpHub, key: str | None, *, host: str | None = None):
        self.hub, self.key = hub, key
        self.host = host or f'127.0.0.1:{hub.port}'
        self.session: str | None = None
        self.version: str | None = None
        self.n = 0

    def send(self, method: str, body: bytes | None = None, headers: dict | None = None,
             path: str = '/mcp') -> Reply:
        sent = {'Host': self.host, 'Accept': 'application/json, text/event-stream',
                'Content-Type': 'application/json', 'User-Agent': 'omp/18.12.6'}
        if self.key is not None:
            sent['Authorization'] = f'Bearer {self.key}'
        if self.session:
            sent['Mcp-Session-Id'] = self.session
        if self.version:
            sent['MCP-Protocol-Version'] = self.version
        for name, value in (headers or {}).items():
            for old in [k for k in sent if k.lower() == name.lower()]:
                del sent[old]
            if value is not None:
                sent[name] = value
        connection = http.client.HTTPConnection('127.0.0.1', self.hub.port, timeout=30)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in sent.items():
                connection.putheader(name, value)
            if body is not None:
                connection.putheader('Content-Length', str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            return Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
        finally:
            connection.close()

    def post_obj(self, obj, headers: dict | None = None) -> Reply:
        return self.send('POST', json.dumps(obj).encode('utf-8'), headers)

    def rpc(self, method: str, params=None, headers: dict | None = None) -> Reply:
        self.n += 1
        message = {'jsonrpc': '2.0', 'id': self.n, 'method': method}
        if params is not None:
            message['params'] = params
        return self.post_obj(message, headers)

    def start(self) -> 'Rpc':
        reply = self.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                        'clientInfo': {'name': 'omp', 'version': '18.12.6'}})
        assert reply.status == 200, reply.body
        self.session = reply.headers['mcp-session-id']
        self.version = reply.json['result']['protocolVersion']
        assert self.post_obj({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).status == 202
        return self

    def call(self, name: str, **arguments) -> dict:
        reply = self.rpc('tools/call', {'name': name, 'arguments': arguments})
        assert reply.status == 200, (reply.status, reply.body)
        return reply.json['result']

    def ok(self, name: str, **arguments) -> dict:
        result = self.call(name, **arguments)
        assert result['isError'] is False, result
        return result['structuredContent']

    def err(self, name: str, **arguments) -> dict:
        result = self.call(name, **arguments)
        assert result['isError'] is True, result
        return result['structuredContent']['error']

    def tools(self) -> list[str]:
        return [tool['name'] for tool in self.rpc('tools/list').json['result']['tools']]


class Case(unittest.TestCase):
    hub_options: dict = {}

    def setUp(self):
        self.hub = McpHub(self, **self.hub_options)
        self.idem = 0

    def rpc(self, key: str | None, *, started: bool = True, **kwargs) -> Rpc:
        client = Rpc(self.hub, key, **kwargs)
        return client.start() if started and key is not None else client

    def new_key(self, name: str, grade: str = 'requester', projects: list | None = None) -> str:
        return self.hub.key(name, grade, {'projects': projects if projects is not None else [self.hub.a]})

    def submit(self, client: Rpc, title='T', body='B', project=None, key=None) -> dict:
        self.idem += 1
        return client.ok('inbox_submit', project_id=project or self.hub.a, title=title, body=body,
                         idempotency_key=key or f'k-{self.idem}')

    def request_rows(self, root=None) -> list:
        return self.hub.inbox_rows('SELECT request_id, state, version, title FROM requests ORDER BY seq', root=root)

    def row(self, rid: str, root=None) -> tuple:
        return self.hub.inbox_rows('SELECT state, version, outcome, cancel_state FROM requests WHERE request_id = ?',
                                   rid, root=root)[0]

    def count(self, table: str, root=None) -> int:
        return self.hub.inbox_rows(f'SELECT count(*) FROM {table}', root=root)[0][0]


# --- 1. authentication ---------------------------------------------------------------------------

class BearerOnlyTests(Case):
    def assert_401(self, reply: Reply):
        self.assertEqual(reply.status, 401, reply.body)
        self.assertEqual(reply.headers.get('www-authenticate'), 'Bearer realm="workbench-hub"')
        error = reply.json['error']
        self.assertEqual(error['code'], -32001)
        self.assertEqual(set(error), {'code', 'message'}, 'a 401 carries no detail')
        self.assertNotRegex(reply.body.decode(), LEAK)

    def test_every_rejected_credential_is_401_without_detail_on_every_method(self):
        good = self.new_key('good')
        revoked = self.new_key('revoked')
        self.hub.keys.revoke(revoked[4:14], now=self.hub.now[0])
        expiring = self.hub.key('exp', 'requester', {'all': True}, expires_days=1)
        self.hub.now[0] += 2 * 86400
        flipped = good[:-1] + ('A' if good[-1] != 'A' else 'B')
        unknown = 'wbk_' + 'a' * (len(good) - 4)
        for label, key in (('none', None), ('flipped', flipped), ('unknown', unknown), ('revoked', revoked),
                           ('expired', expiring), ('garbage', 'x' * 8)):
            for method in ('POST', 'GET', 'DELETE'):
                with self.subTest(key=label, method=method):
                    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}).encode()
                    self.assert_401(Rpc(self.hub, key).send(method, body if method == 'POST' else None))
        # a 401 never ran anything
        self.assertEqual(self.request_rows(), [])

    def test_tool_calls_with_dead_keys_do_not_execute(self):
        key = self.new_key('r')
        client = self.rpc(key)
        self.hub.keys.revoke(key[4:14], now=self.hub.now[0])
        reply = client.rpc('tools/call', {'name': 'inbox_submit', 'arguments': {
            'project_id': self.hub.a, 'title': 't', 'body': 'b', 'idempotency_key': 'k'}})
        self.assert_401(reply)
        self.assertEqual(self.request_rows(), [])

    def test_a_real_login_session_cookie_is_not_a_credential(self):
        cookie = self.login_cookie()
        for headers in ({'Cookie': cookie}, {'Cookie': cookie, 'Authorization': 'Bearer wbk_wrong'}):
            with self.subTest(headers=list(headers)):
                client = Rpc(self.hub, None)
                self.assert_401(client.rpc('initialize', {'protocolVersion': '2025-06-18'}, headers))
                self.assert_401(client.rpc('tools/list', None, headers))
                self.assert_401(client.send('GET', None, headers))
        reply = Rpc(self.hub, None).post_obj({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'inbox_submit', 'arguments': {'project_id': self.hub.a, 'title': 't', 'body': 'b',
                                                  'idempotency_key': 'k'}}}, {'Cookie': cookie})
        self.assert_401(reply)
        self.assertEqual(self.request_rows(), [])

    def test_a_cookie_next_to_a_valid_key_changes_nothing(self):
        # the key's grade decides, not the owner session riding along
        cookie = self.login_cookie()
        client = self.rpc(self.new_key('r'))
        names = client.rpc('tools/list', None, {'Cookie': cookie}).json['result']['tools']
        self.assertEqual(sorted(tool['name'] for tool in names), sorted(set(SPEC_TOOLS) - EDITOR_ONLY))
        self.submit(client)
        self.assertEqual(self.hub.inbox_rows('SELECT origin_kind, origin_name FROM requests'), [('api_key', 'r')])

    def login_cookie(self) -> str:
        """A real owner session through ``GET/POST /login`` (own http.client, no harness shortcut)."""
        def call(method, path, body=None, cookie=None):
            connection = http.client.HTTPConnection('127.0.0.1', self.hub.port, timeout=15)
            headers = {'Host': f'127.0.0.1:{self.hub.port}'}
            if cookie:
                headers['Cookie'] = cookie
            if body is not None:
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
            try:
                connection.request(method, path, body, headers)
                response = connection.getresponse()
                return response.status, response.getheaders(), response.read().decode()
            finally:
                connection.close()

        status, headers, page = call('GET', '/login')
        self.assertEqual(status, 200)
        jar = {}
        for name, value in headers:
            if name.lower() == 'set-cookie':
                pair = value.split(';', 1)[0]
                jar[pair.split('=', 1)[0]] = pair.split('=', 1)[1]
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        from urllib.parse import urlencode
        status, headers, _ = call('POST', '/login', urlencode({'login_token': token, 'password': PASSWORD}),
                                  '; '.join(f'{k}={v}' for k, v in jar.items()))
        self.assertEqual(status, 303)
        session = [value.split(';', 1)[0] for name, value in headers
                   if name.lower() == 'set-cookie' and value.startswith('wb_hub_session=')]
        self.assertTrue(session, 'login produced a session cookie')
        return session[0]


# --- 2. host / origin / the OMP handshake --------------------------------------------------------

class HostOriginTests(Case):
    def test_host_and_origin_checks_run_before_anything_executes(self):
        key = self.new_key('r')
        good = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'inbox_submit', 'arguments': {
            'project_id': self.hub.a, 'title': 'rebind', 'body': 'b', 'idempotency_key': 'k'}}}
        client = Rpc(self.hub, key)
        port = self.hub.port
        cases = [('host evil', {'Host': 'evil.example'}, 421), ('host evil:port', {'Host': f'evil.example:{port}'}, 421),
                 ('origin evil', {'Origin': 'http://evil.example'}, 403),
                 ('origin other port', {'Origin': f'http://127.0.0.1:{port + 1}'}, 403),
                 ('origin null', {'Origin': 'null'}, 403),
                 ('origin https', {'Origin': f'https://127.0.0.1:{port}'}, 403)]
        for label, headers, status in cases:
            with self.subTest(label):
                reply = client.post_obj(good, headers)
                self.assertEqual(reply.status, status, reply.body)
                self.assertNotIn('result', reply.body.decode())
        self.assertEqual(self.request_rows(), [], 'a rebinding-style request must store nothing')
        same = client.post_obj(good, {'Origin': f'http://127.0.0.1:{port}'})
        self.assertEqual(same.status, 200, same.body)
        self.assertEqual(len(self.request_rows()), 1)

    def test_foreign_host_and_origin_apply_to_get_and_delete(self):
        client = self.rpc(self.new_key('r'))
        for method in ('GET', 'DELETE'):
            self.assertEqual(client.send(method, None, {'Host': 'evil.example'}).status, 421, method)
            self.assertEqual(client.send(method, None, {'Origin': 'http://evil.example'}).status, 403, method)


class OmpHandshakeTests(Case):
    """The exact sequence real OMP 18.12.6 sent to a stub (PROBE-OMP-MCP.md ``stub_requests``)."""

    def test_replay_of_the_recorded_sequence(self):
        key = self.hub.keys.rotate_system_key(now=self.hub.now[0]).raw
        client = Rpc(self.hub, key)
        accept = 'application/json, text/event-stream'
        # 1. initialize: Bearer + Accept, no session id and no protocol header yet
        reply = client.send('POST', json.dumps({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'omp', 'version': '18.12.6'}}
        }).encode(), {'Accept': accept})
        self.assertEqual(reply.status, 200, reply.body)
        self.assertTrue(reply.headers['content-type'].startswith('application/json'))
        self.assertEqual(reply.json['id'], 0)
        result = reply.json['result']
        self.assertEqual(result['protocolVersion'], '2025-06-18')
        self.assertEqual(result['capabilities'], {'tools': {'listChanged': False}})
        self.assertEqual(result['serverInfo']['name'], 'workbench-hub')
        session = reply.headers['mcp-session-id']
        self.assertGreaterEqual(len(session), 16)
        client.session, client.version = session, '2025-06-18'
        # 2. notifications/initialized (session + protocol header): 202, empty
        note = client.send('POST', json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).encode())
        self.assertEqual((note.status, note.body), (202, b''))
        # 3. GET with only text/event-stream accepted: no server stream, 405 (a silent skip for OMP)
        stream = client.send('GET', None, {'Accept': 'text/event-stream'})
        self.assertEqual(stream.status, 405)
        self.assertIn('POST', stream.headers.get('allow', ''))
        # 4. tools/list
        listed = client.rpc('tools/list')
        self.assertEqual(listed.status, 200)
        self.assertEqual([tool['name'] for tool in listed.json['result']['tools']], SPEC_TOOLS)

    def test_a_content_type_with_charset_and_string_ids_are_fine(self):
        client = Rpc(self.hub, self.new_key('r'))
        reply = client.send('POST', json.dumps({'jsonrpc': '2.0', 'id': 'abc-1', 'method': 'initialize',
                                                'params': {'protocolVersion': '2025-03-26'}}).encode(),
                            {'Content-Type': 'application/json; charset=utf-8'})
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.json['id'], 'abc-1')
        self.assertEqual(reply.json['result']['protocolVersion'], '2025-03-26')

    def test_unknown_protocol_version_is_answered_with_the_newest_supported(self):
        client = Rpc(self.hub, self.new_key('r'))
        reply = client.rpc('initialize', {'protocolVersion': '1999-01-01'})
        self.assertEqual(reply.json['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(client.rpc('initialize', {'protocolVersion': 5}).json['result']['protocolVersion'],
                         '2025-06-18')
        self.assertEqual(client.rpc('initialize', {}).json['result']['protocolVersion'], '2025-06-18')

    def test_protocol_header_must_be_supported_on_every_method(self):
        client = self.rpc(self.new_key('r'))
        for method in ('POST', 'GET', 'DELETE'):
            with self.subTest(method=method):
                reply = client.send(method, b'{}' if method == 'POST' else None, {'MCP-Protocol-Version': '2020-01-01'})
                self.assertEqual(reply.status, 400)
        self.assertEqual(client.rpc('tools/list', None, {'MCP-Protocol-Version': '2025-03-26'}).status, 200)


# --- 3. session ids ------------------------------------------------------------------------------

class SessionTests(Case):
    def test_forged_other_keys_and_ended_session_ids_are_404_and_run_nothing(self):
        k1, k2 = self.new_key('a'), self.new_key('b')
        c1, c2 = self.rpc(k1), self.rpc(k2)
        call = {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call', 'params': {'name': 'inbox_submit', 'arguments': {
            'project_id': self.hub.a, 'title': 'via-forged-session', 'body': 'b', 'idempotency_key': 'z'}}}
        for label, session in (('random', 'A' * 32), ('other key', c2.session), ('empty-ish', ' ')):
            with self.subTest(label):
                reply = c1.post_obj(call, {'Mcp-Session-Id': session})
                self.assertEqual(reply.status, 404, reply.body)
                self.assertNotIn('result', reply.json)
        self.assertEqual(c1.send('DELETE', None, {'Mcp-Session-Id': c2.session}).status, 404)
        self.assertEqual(c2.rpc('tools/list').status, 200, "another key's DELETE must not end my session")
        self.assertEqual(c1.send('DELETE').status, 204)
        self.assertEqual(c1.rpc('tools/list').status, 404, 'an ended session is gone')
        self.assertEqual(c1.send('DELETE').status, 404)
        # a notification on a dead session is still refused, never silently accepted
        self.assertEqual(c1.post_obj({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).status, 404)
        self.assertEqual(self.request_rows(), [])

    def test_two_session_headers_are_refused(self):
        client = self.rpc(self.new_key('r'))
        connection = http.client.HTTPConnection('127.0.0.1', self.hub.port, timeout=15)
        try:
            connection.putrequest('POST', '/mcp', skip_host=True)
            for name, value in (('Host', client.host), ('Authorization', f'Bearer {client.key}'),
                                ('Accept', 'application/json'), ('Content-Type', 'application/json'),
                                ('Mcp-Session-Id', client.session), ('Mcp-Session-Id', 'forged')):
                connection.putheader(name, value)
            body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
            connection.putheader('Content-Length', str(len(body)))
            connection.endheaders(body)
            self.assertGreaterEqual(connection.getresponse().status, 400)
        finally:
            connection.close()

    def test_initialize_always_issues_a_fresh_unguessable_id(self):
        client = Rpc(self.hub, self.new_key('r'))
        ids = {client.rpc('initialize', {'protocolVersion': '2025-06-18'}).headers['mcp-session-id']
               for _ in range(5)}
        self.assertEqual(len(ids), 5)
        self.assertTrue(all(len(item) >= 22 for item in ids))


# --- 4. JSON-RPC framing -------------------------------------------------------------------------

class FramingTests(Case):
    def setUp(self):
        super().setUp()
        self.client = self.rpc(self.new_key('r'))

    def raw_error(self, body: bytes, status: int, code: int):
        reply = self.client.send('POST', body)
        self.assertEqual(reply.status, status, reply.body)
        self.assertEqual(reply.json['error']['code'], code, reply.body)
        self.assertNotRegex(reply.body.decode(), LEAK)
        return reply

    def test_malformed_and_hostile_json_is_a_clean_parse_error(self):
        deep = b'[' * 100000 + b']' * 100000
        deep_obj = b'{"a":' * 30000 + b'1' + b'}' * 30000
        for label, body in (('garbage', b'{not json'), ('empty', b''), ('truncated', b'{"jsonrpc":"2.0","id":'),
                            ('invalid utf-8', b'{"jsonrpc":"2.0","id":1,"method":"\xff\xfe"}'),
                            ('bom', b'\xef\xbb\xbf{"jsonrpc":"2.0","id":1,"method":"ping"}'),
                            ('deep array', deep), ('deep object', deep_obj),
                            ('NaN', b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}'),
                            ('huge exponent', b'{"jsonrpc":"2.0","id":1e999,"method":"ping"}'),
                            ('4300+ digit int', b'{"jsonrpc":"2.0","id":' + b'9' * 5000 + b',"method":"ping"}')):
            with self.subTest(label):
                self.raw_error(body, 400, -32700)
        self.assertEqual(self.client.rpc('ping').json['result'], {})  # the server is still fine

    def test_batches_are_invalid_request_and_run_nothing(self):
        call = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'inbox_submit', 'arguments': {
            'project_id': self.hub.a, 'title': 'batched', 'body': 'b', 'idempotency_key': 'bk'}}}
        note = {'jsonrpc': '2.0', 'method': 'tools/call', 'params': call['params']}
        for label, body in (('batch of calls', [call, call]), ('batch of one', [call]), ('empty batch', []),
                            ('batch of notes', [note]), ('scalar', 5), ('string', 'ping'), ('null', None)):
            with self.subTest(label):
                self.raw_error(json.dumps(body).encode(), 400, -32600)
        self.assertEqual(self.request_rows(), [])

    def test_notifications_never_execute_a_tool(self):
        args = {'project_id': self.hub.a, 'title': 'note', 'body': 'b', 'idempotency_key': 'nk'}
        for label, message in (
                ('no id', {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'inbox_submit', 'arguments': args}}),
                ('cancelled', {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 1}}),
                ('unknown note', {'jsonrpc': '2.0', 'method': 'notifications/whatever'}),
                ('client result', {'jsonrpc': '2.0', 'id': 4, 'result': {}}),
                ('client error', {'jsonrpc': '2.0', 'id': 4, 'error': {'code': 1, 'message': 'x'}})):
            with self.subTest(label):
                reply = self.client.post_obj(message)
                self.assertEqual((reply.status, reply.body), (202, b''))
        self.assertEqual(self.request_rows(), [])
        # id:null / boolean / object ids are not requests we can answer, and still run nothing
        for bad_id in (None, True, [1], {'a': 1}):
            with self.subTest(bad_id=bad_id):
                reply = self.client.post_obj({'jsonrpc': '2.0', 'id': bad_id, 'method': 'tools/call',
                                              'params': {'name': 'inbox_submit', 'arguments': args}})
                self.assertEqual(reply.status, 400)
                self.assertEqual(reply.json['error']['code'], -32600)
        self.assertEqual(self.request_rows(), [])

    def test_invalid_requests(self):
        for label, message in (('no jsonrpc', {'id': 1, 'method': 'ping'}),
                               ('jsonrpc 1.0', {'jsonrpc': '1.0', 'id': 1, 'method': 'ping'}),
                               ('jsonrpc number', {'jsonrpc': 2.0, 'id': 1, 'method': 'ping'}),
                               ('method missing', {'jsonrpc': '2.0', 'id': 1}),
                               ('method not str', {'jsonrpc': '2.0', 'id': 1, 'method': 5})):
            with self.subTest(label):
                self.raw_error(json.dumps(message).encode(), 400, -32600)

    def test_unknown_methods_and_bad_params_use_their_codes_and_echo_the_id(self):
        reply = self.client.post_obj({'jsonrpc': '2.0', 'id': 'xyz', 'method': 'resources/list'})
        self.assertEqual((reply.json['id'], reply.json['error']['code']), ('xyz', -32601))
        for params in ([], 'x', 5):
            reply = self.client.post_obj({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list', 'params': params})
            self.assertEqual(reply.json['error']['code'], -32602, params)
        for params in ({}, {'name': 5}, {'name': 'inbox_drop_everything'}, {'name': None},
                       {'name': ['inbox_get']}, {'name': 'INBOX_GET'}, {'name': '__init__'}):
            reply = self.client.rpc('tools/call', params)
            self.assertEqual((reply.status, reply.json['error']['code']), (200, -32602), params)
        self.assertEqual(self.client.post_obj({'jsonrpc': '2.0', 'id': 2 ** 70, 'method': 'ping'}).status, 200)

    def test_oversized_bodies_are_413_and_store_nothing(self):
        filler = 'x' * (300 * 1024)
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'inbox_submit', 'arguments': {'project_id': self.hub.a, 'title': 't', 'body': filler,
                                                  'idempotency_key': 'big'}}}).encode()
        self.assertEqual(self.client.send('POST', body).status, 413)
        # a body of exactly 256 KiB + 1 is refused as well, regardless of the JSON inside
        self.assertEqual(self.client.send('POST', b' ' * (256 * 1024 + 1)).status, 413)
        self.assertEqual(self.request_rows(), [])
        # body larger than 64 KiB but under the HTTP cap: too_large from the inbox, nothing stored
        self.assertEqual(self.client.err('inbox_submit', project_id=self.hub.a, title='t', body='x' * 65537,
                                         idempotency_key='b2')['code'], 'too_large')
        self.assertEqual(self.request_rows(), [])

    def test_wrong_methods_and_accept(self):
        for method in ('PUT', 'PATCH', 'OPTIONS'):
            self.assertIn(self.client.send(method, b'{}').status, (404, 405), method)
        self.assertEqual(self.client.send('GET').status, 405)
        self.assertEqual(self.client.send('POST', b'{}', {'Accept': 'text/event-stream'}).status, 406)
        self.assertEqual(self.client.send('POST', b'{}', {'Content-Type': 'text/plain'}).status, 415)
        self.assertEqual(self.client.send('POST', b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
                                          {'Accept': '*/*'}).status in (200, 406), True)


# --- 5. tools/list by grade, static surface -----------------------------------------------------

class ToolSurfaceTests(Case):
    def test_tool_list_per_grade_matches_the_spec_exactly(self):
        requester = self.rpc(self.new_key('r'))
        editor = self.rpc(self.new_key('e', 'editor'))
        board = self.rpc(self.hub.keys.rotate_system_key(now=self.hub.now[0]).raw)
        self.assertEqual(requester.tools(), [name for name in SPEC_TOOLS if name not in EDITOR_ONLY])
        self.assertEqual(editor.tools(), SPEC_TOOLS)
        self.assertEqual(board.tools(), SPEC_TOOLS)

    def test_schemas_are_closed_and_no_tool_reaches_approval_terminal_or_backend_control(self):
        tools = self.rpc(self.new_key('e', 'editor')).rpc('tools/list').json['result']['tools']
        text = json.dumps(tools).lower()
        for tool in tools:
            schema = tool['inputSchema']
            self.assertEqual(schema['type'], 'object')
            self.assertIs(schema['additionalProperties'], False, tool['name'])
            self.assertTrue(set(schema.get('required', [])) <= set(schema['properties']))
            for forbidden in ('approve', 'spec_revision', 'link_token', 'terminal', 'power', 'pause', 'resume',
                              'shutdown', 'backend', 'task_id', 'origin', 'key_id', 'principal', 'grade', 'owner'):
                self.assertNotIn(forbidden, schema['properties'], f'{tool["name"]} exposes {forbidden}')
        for forbidden in ('approve_spec', 'power_off', 'terminal', 'send_keys', 'run_command'):
            self.assertNotIn(forbidden, text)

    def test_requester_calling_a_hidden_tool_is_forbidden_and_changes_nothing(self):
        requester = self.rpc(self.new_key('r'))
        rid = self.submit(requester)['request_id']
        before = self.row(rid)
        for name, args in (('inbox_update', {'request_id': rid, 'base_version': before[1], 'title': 'hacked'}),
                           ('inbox_set_state', {'request_id': rid, 'base_version': before[1], 'to': 'NEEDS_INFO'})):
            self.assertEqual(requester.err(name, **args)['code'], 'forbidden', name)
        self.assertEqual(self.row(rid), before)
        self.assertEqual(self.request_rows()[0][3], 'T')

    def test_arguments_cannot_spoof_identity_or_origin(self):
        requester = self.rpc(self.new_key('r'))
        for extra in ({'origin': 'owner'}, {'origin_kind': 'owner'}, {'key_id': 'x'}, {'principal': 'editor'},
                      {'created_at': 1}):
            with self.subTest(extra=list(extra)):
                self.assertEqual(requester.err('inbox_submit', project_id=self.hub.a, title='t', body='b',
                                               idempotency_key='sp', **extra)['code'], 'invalid_input')
        self.assertEqual(self.request_rows(), [])
        created = self.submit(requester)
        stored = self.hub.inbox_rows('SELECT origin_kind, origin_name FROM requests')[0]
        self.assertEqual(stored, ('api_key', 'r'))
        self.assertIn('r', json.dumps(created['origin']))

    def test_argument_type_confusion_is_invalid_input_never_internal_error(self):
        editor = self.rpc(self.new_key('e', 'editor'))
        rid = self.submit(editor)['request_id']
        version = self.row(rid)[1]
        bad = [('inbox_submit', {'project_id': 5, 'title': 't', 'body': 'b', 'idempotency_key': 'k'}),
               ('inbox_submit', {'project_id': self.hub.a, 'title': ['t'], 'body': 'b', 'idempotency_key': 'k'}),
               ('inbox_submit', {'project_id': self.hub.a, 'title': 't', 'body': {'a': 1}, 'idempotency_key': 'k'}),
               ('inbox_submit', {'project_id': self.hub.a, 'title': 't', 'body': 'b', 'idempotency_key': 'bad key!'}),
               ('inbox_submit', {'project_id': self.hub.a, 'title': 't', 'body': 'b', 'idempotency_key': 'k' * 129}),
               ('inbox_submit', {'project_id': self.hub.a, 'title': 't', 'body': 'b'}),
               ('inbox_submit', {'project_id': self.hub.a, 'title': '', 'body': 'b', 'idempotency_key': 'k'}),
               ('inbox_submit', {'project_id': self.hub.a, 'title': 't\x00x', 'body': 'b', 'idempotency_key': 'k'}),
               ('inbox_submit', {'project_id': self.hub.a, 'title': 't\nx', 'body': 'b', 'idempotency_key': 'k'}),
               ('inbox_get', {'request_id': 5}), ('inbox_get', {}),
               ('inbox_update', {'request_id': rid, 'base_version': True, 'title': 'x'}),
               ('inbox_update', {'request_id': rid, 'base_version': '1', 'title': 'x'}),
               ('inbox_update', {'request_id': rid, 'base_version': 1.5, 'title': 'x'}),
               ('inbox_set_state', {'request_id': rid, 'base_version': version, 'to': 'CLOSED'}),
               ('inbox_set_state', {'request_id': rid, 'base_version': version, 'to': 'READY'}),
               ('inbox_list', {'limit': 0}), ('inbox_list', {'limit': 101}), ('inbox_list', {'limit': -1}),
               ('inbox_list', {'limit': '5'}), ('inbox_list', {'states': 'NEW'}), ('inbox_list', {'states': ['NOPE']}),
               ('inbox_list', {'query': 'q' * 201}),
               ('inbox_result', {'request_id': rid, 'length': 0}), ('inbox_result', {'request_id': rid, 'length': 131073}),
               ('inbox_result', {'request_id': rid, 'offset': -1}), ('inbox_result', {'request_id': rid, 'full': 'yes'}),
               ('inbox_comment', {'request_id': rid, 'body': ''}), ('inbox_comment', {'request_id': rid, 'body': 'x' * 16385})]
        for name, args in bad:
            with self.subTest(name=name, args=str(args)[:60]):
                error = editor.err(name, **args)
                self.assertIn(error['code'], {'invalid_input', 'too_large'}, error)
        # arguments that are not an object
        for arguments in ([], 'x', 5):
            reply = editor.rpc('tools/call', {'name': 'inbox_get', 'arguments': arguments})
            self.assertTrue(reply.json['result']['isError'])
            self.assertEqual(reply.json['result']['structuredContent']['error']['code'], 'invalid_input')
        self.assertEqual(self.count('requests'), 1)
        self.assertEqual(self.count('comments'), 0)
        self.assertEqual(self.row(rid)[:2], ('NEW', version))

    def test_text_content_mirrors_structured_content_for_successes_and_errors(self):
        editor = self.rpc(self.new_key('e', 'editor'))
        rid = self.submit(editor, title='안녕 \U0001F600 "q"', body='b \\')['request_id']
        calls = [('inbox_projects', {}), ('inbox_list', {}), ('inbox_get', {'request_id': rid}),
                 ('inbox_result', {'request_id': rid}), ('inbox_get', {'request_id': str(uuid.uuid4())}),
                 ('inbox_cancel', {'request_id': rid, 'confirm_token': 'nope'})]
        for name, args in calls:
            with self.subTest(name=name):
                result = editor.call(name, **args)
                self.assertEqual(len(result['content']), 1)
                self.assertEqual(result['content'][0]['type'], 'text')
                self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
                self.assertIsInstance(result['isError'], bool)
        got = editor.ok('inbox_get', request_id=rid)['request']
        self.assertEqual(got['title'], '안녕 \U0001F600 "q"')


# --- 6. submit: immediacy, idempotency ----------------------------------------------------------

class SubmitTests(Case):
    def test_submit_is_immediate_idempotent_and_conflicting_content_is_refused(self):
        client = self.rpc(self.new_key('r'))
        first = client.ok('inbox_submit', project_id=self.hub.a, title='same', body='body', idempotency_key='idem-1')
        self.assertEqual((first['state'], first['created']), ('NEW', True))
        self.assertEqual(self.row(first['request_id'])[0], 'NEW')
        again = client.ok('inbox_submit', project_id=self.hub.a, title='same', body='body', idempotency_key='idem-1')
        self.assertEqual((again['request_id'], again['seq'], again['created']),
                         (first['request_id'], first['seq'], False))
        for title, body in (('other', 'body'), ('same', 'other body'), ('same', 'body '), ('Same', 'body')):
            with self.subTest(title=title, body=body):
                error = client.err('inbox_submit', project_id=self.hub.a, title=title, body=body,
                                   idempotency_key='idem-1')
                self.assertEqual(error['code'], 'idempotency_conflict')
        self.assertEqual(len(self.request_rows()), 1)
        self.assertEqual(self.request_rows()[0][3], 'same')
        # an idempotency key of another caller is another request
        other = self.rpc(self.new_key('r2'))
        second = other.ok('inbox_submit', project_id=self.hub.a, title='same', body='body', idempotency_key='idem-1')
        self.assertNotEqual(second['request_id'], first['request_id'])
        self.assertTrue(second['created'])

    def test_parallel_same_key_submissions_create_exactly_one_request(self):
        key = self.new_key('r')
        replies: list = []
        gate = threading.Barrier(8)

        def worker():
            client = Rpc(self.hub, key)
            gate.wait(10)
            replies.append(client.call('inbox_submit', project_id=self.hub.a, title='race', body='b',
                                       idempotency_key='race-1'))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(replies), 8)
        self.assertTrue(all(reply['isError'] is False for reply in replies), replies)
        self.assertEqual({reply['structuredContent']['request_id'] for reply in replies}.__len__(), 1)
        self.assertEqual(sum(reply['structuredContent']['created'] for reply in replies), 1)
        self.assertEqual(len(self.request_rows()), 1)

    def test_scope_unknown_project_and_invalid_ids(self):
        client = self.rpc(self.new_key('r'))  # scope: alpha only
        self.assertEqual(client.err('inbox_submit', project_id=self.hub.b, title='t', body='b',
                                    idempotency_key='k1')['code'], 'forbidden')
        self.assertEqual(self.request_rows(root=self.hub.root_b), [])
        for project in (str(uuid.uuid4()), '../../etc', '', 'x' * 5000, "a'; DROP TABLE requests;--"):
            error = client.err('inbox_submit', project_id=project, title='t', body='b', idempotency_key='k2')
            self.assertIn(error['code'], {'forbidden', 'not_found', 'invalid_input'}, project[:20])
            self.assertNotRegex(json.dumps(error), LEAK)
        self.assertEqual(self.request_rows(), [])
        listed = client.err('inbox_list', project_id=self.hub.b)
        self.assertEqual(listed['code'], 'forbidden')

    def test_unicode_limits_count_code_points_and_bytes(self):
        client = self.rpc(self.new_key('r'))
        ok = client.ok('inbox_submit', project_id=self.hub.a, title='\U0001F600' * 200, body='é' * 32768,
                       idempotency_key='u1')
        self.assertEqual(ok['state'], 'NEW')
        self.assertEqual(client.err('inbox_submit', project_id=self.hub.a, title='\U0001F600' * 201, body='b',
                                    idempotency_key='u2')['code'], 'too_large')
        self.assertEqual(client.err('inbox_submit', project_id=self.hub.a, title='t', body='é' * 32769,
                                    idempotency_key='u3')['code'], 'too_large')
        self.assertEqual(len(self.request_rows()), 1)

    def test_lone_surrogates_do_not_break_the_endpoint(self):
        client = self.rpc(self.new_key('r'))
        raw = ('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"inbox_submit","arguments":'
               '{"project_id":"%s","title":"a\\ud800b","body":"c","idempotency_key":"sur"}}}' % self.hub.a)
        reply = client.send('POST', raw.encode())
        self.assertEqual(reply.status, 200, reply.body)
        result = reply.json['result']
        if result['isError']:
            self.assertEqual(result['structuredContent']['error']['code'], 'invalid_input', result)
        self.assertEqual(client.rpc('ping').status, 200)

    def test_two_hundred_submissions_in_a_row_are_all_accepted(self):
        client = self.rpc(self.new_key('r'))
        for number in range(200):
            self.assertEqual(client.ok('inbox_submit', project_id=self.hub.a, title=f't{number}', body='b',
                                       idempotency_key=f'n-{number}')['state'], 'NEW')
        self.assertEqual(len(self.request_rows()), 200)


# --- 7. requester vs editor visibility ----------------------------------------------------------

class VisibilityTests(Case):
    def setUp(self):
        super().setUp()
        self.r1 = self.rpc(self.new_key('r1'))
        self.r2 = self.rpc(self.new_key('r2'))
        self.ed = self.rpc(self.new_key('ed', 'editor'))
        self.mine = self.submit(self.r1, title='mine-secret-title')['request_id']
        self.theirs = self.submit(self.r2, title='theirs')['request_id']
        self.beta_key = self.hub.key('wide', 'editor', {'all': True})
        self.wide = self.rpc(self.beta_key)
        self.beta = self.submit(self.wide, project=self.hub.b, title='in-beta')['request_id']

    def test_foreign_and_missing_request_ids_are_indistinguishable(self):
        missing = str(uuid.uuid4())
        for tool, args in (('inbox_get', {}), ('inbox_comment', {'body': 'hi'}), ('inbox_cancel', {}),
                           ('inbox_result', {})):
            with self.subTest(tool=tool):
                foreign = self.r1.err(tool, request_id=self.theirs, **args)
                absent = self.r1.err(tool, request_id=missing, **args)
                self.assertEqual(foreign, absent)
                self.assertEqual(foreign['code'], 'not_found')
        self.assertEqual(self.count('comments'), 0, 'a foreign comment must not be stored')
        self.assertEqual(self.row(self.theirs)[0], 'NEW')
        self.assertEqual(self.row(self.theirs)[2:], (None, None))

    def test_list_and_search_never_show_other_requesters_requests(self):
        listed = self.r1.ok('inbox_list')
        self.assertEqual([item['request_id'] for item in listed['requests']], [self.mine])
        for query in ('theirs', 'in-beta', 'THEIRS'):
            self.assertEqual(self.r1.ok('inbox_list', query=query)['requests'], [], query)
        self.assertEqual(self.r1.ok('inbox_list', states=['NEW', 'CLOSED'], include_closed=True)['requests'].__len__(), 1)
        self.assertNotIn('theirs', json.dumps(self.r1.ok('inbox_list')))
        # a cursor minted for another principal does not widen what this one sees
        for index in range(3):
            self.submit(self.ed, title=f'ed-{index}')
        page = self.ed.ok('inbox_list', limit=1)
        self.assertIsNotNone(page['next_cursor'])
        borrowed = self.r1.call('inbox_list', cursor=page['next_cursor'])
        if not borrowed['isError']:
            ids = {item['request_id'] for item in borrowed['structuredContent']['requests']}
            self.assertLessEqual(ids, {self.mine}, 'a borrowed cursor leaked requests')
        else:
            self.assertIn(borrowed['structuredContent']['error']['code'], {'invalid_input', 'not_found'})

    def test_editor_acts_on_every_request_in_its_scope_and_only_there(self):
        titles = {item['title'] for item in self.ed.ok('inbox_list')['requests']}
        self.assertEqual(titles, {'mine-secret-title', 'theirs'})
        self.assertEqual(self.ed.ok('inbox_get', request_id=self.mine)['request']['title'], 'mine-secret-title')
        self.ed.ok('inbox_comment', request_id=self.theirs, body='from editor')
        version = self.row(self.mine)[1]
        self.assertEqual(self.ed.ok('inbox_update', request_id=self.mine, base_version=version, title='edited')['state'],
                         'NEW')
        version = self.row(self.theirs)[1]
        self.assertEqual(self.ed.ok('inbox_set_state', request_id=self.theirs, base_version=version,
                                    to='NEEDS_INFO')['state'], 'NEEDS_INFO')
        self.assertEqual(self.ed.ok('inbox_cancel', request_id=self.theirs)['state'], 'CLOSED')
        # the beta request is outside this editor's scope: the same answer as a missing one
        missing = str(uuid.uuid4())
        for tool, args in (('inbox_get', {}), ('inbox_comment', {'body': 'x'}), ('inbox_cancel', {}),
                           ('inbox_result', {}), ('inbox_update', {'base_version': 1, 'title': 'x'}),
                           ('inbox_set_state', {'base_version': 1, 'to': 'NEEDS_INFO'})):
            with self.subTest(tool=tool):
                self.assertEqual(self.ed.err(tool, request_id=self.beta, **args),
                                 self.ed.err(tool, request_id=missing, **args))
        self.assertEqual(self.row(self.beta, root=self.hub.root_b)[0], 'NEW')
        self.assertEqual(self.ed.err('inbox_list', project_id=self.hub.b)['code'], 'forbidden')
        self.assertNotIn('in-beta', json.dumps(self.ed.ok('inbox_list')))

    def test_projects_listing_hides_paths_and_status_from_requesters(self):
        listed = self.r1.ok('inbox_projects')['projects']
        self.assertEqual(listed, [{'project_id': self.hub.a, 'name': 'alpha'}])
        self.assertNotIn(str(self.hub.base), json.dumps(self.r1.ok('inbox_projects', query='alpha')))
        self.assertEqual(self.r1.ok('inbox_projects', query=str(self.hub.root_a))['projects'], [],
                         'a requester cannot probe by path')
        editor_view = self.ed.ok('inbox_projects')['projects']
        self.assertEqual([item['name'] for item in editor_view], ['alpha'])
        self.assertIn('root', editor_view[0])

    def test_answering_needs_info_and_replay_after_comment(self):
        version = self.row(self.mine)[1]
        self.ed.ok('inbox_set_state', request_id=self.mine, base_version=version, to='NEEDS_INFO')
        answer = self.r1.ok('inbox_comment', request_id=self.mine, body='here is the answer')
        self.assertEqual(answer['state'], 'REVIEWING')
        self.assertEqual(self.r2.err('inbox_comment', request_id=self.mine, body='intruder')['code'], 'not_found')
        self.assertEqual(self.count('comments'), 1)


# --- 8. editor writes: base_version binding -----------------------------------------------------

class EditorWriteTests(Case):
    def test_base_version_binding_and_a_serialized_race(self):
        ed = self.rpc(self.new_key('ed', 'editor'))
        rid = self.submit(ed)['request_id']
        version = self.row(rid)[1]
        self.assertEqual(ed.err('inbox_update', request_id=rid, base_version=version + 5, title='x')['code'], 'conflict')
        self.assertEqual(ed.err('inbox_set_state', request_id=rid, base_version=0, to='NEEDS_INFO')['code'], 'conflict')
        self.assertEqual(self.row(rid)[:2], ('NEW', version))
        results: list = []
        gate = threading.Barrier(6)

        def worker(number):
            client = Rpc(self.hub, ed.key)
            gate.wait(10)
            results.append(client.call('inbox_update', request_id=rid, base_version=version, title=f'w{number}'))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        wins = [r for r in results if not r['isError']]
        losses = [r for r in results if r['isError']]
        self.assertEqual(len(wins), 1, results)
        self.assertEqual({r['structuredContent']['error']['code'] for r in losses}, {'conflict'})
        self.assertEqual(self.row(rid)[1], version + 1)

    def test_editor_refuses_transitions_the_manager_owns(self):
        ed = self.rpc(self.new_key('ed', 'editor'))
        rid = self.submit(ed)['request_id']
        version = self.row(rid)[1]
        for target in ('REVIEWING', 'READY', 'RUNNING', 'CLOSED'):
            error = ed.err('inbox_set_state', request_id=rid, base_version=version, to=target)
            self.assertEqual(error['code'], 'invalid_input', target)
        self.assertEqual(self.row(rid)[:2], ('NEW', version))


# --- 9. two-step cancel -------------------------------------------------------------------------

class CancelTokenTests(Case):
    def setUp(self):
        super().setUp()
        self.r1 = self.rpc(self.new_key('r1'))
        self.ed = self.rpc(self.new_key('ed', 'editor'))
        self.ed2 = self.rpc(self.new_key('ed2', 'editor'))
        self.rid = self.submit(self.r1)['request_id']
        self.hub.link_running(self.rid, view={'command': COMMAND})

    def first(self, client=None, rid=None) -> dict:
        value = (client or self.r1).ok('inbox_cancel', request_id=rid or self.rid)
        self.assertEqual(value['status'], 'confirm_required')
        return value

    def confirm(self, token, client=None, rid=None) -> dict:
        return (client or self.r1).call('inbox_cancel', request_id=rid or self.rid, confirm_token=token)

    def assert_mismatch(self, result):
        self.assertTrue(result['isError'], result)
        self.assertEqual(result['structuredContent']['error']['code'], 'token_mismatch')
        self.assertNotRegex(json.dumps(result), LEAK)

    def assert_untouched(self):
        self.assertEqual(self.hub.cancels, [], 'the cancel port must not have been called')
        self.assertEqual(self.row(self.rid)[0], 'RUNNING')
        self.assertIsNone(self.row(self.rid)[3])

    def test_first_call_only_describes_the_impact(self):
        value = self.first()
        self.assertEqual(value['expires_in'], 60)
        self.assertEqual(value['impact'], {'task_id': 'task-1', 'task_status': 'running', 'command': COMMAND})
        self.assertNotIn(value['confirm_token'], json.dumps(self.r1.ok('inbox_get', request_id=self.rid)))
        self.assert_untouched()

    def test_tokens_are_unpredictable_and_distinct(self):
        tokens = {self.first()['confirm_token'] for _ in range(10)}
        self.assertEqual(len(tokens), 10)
        self.assertTrue(all(re.fullmatch(r'[A-Za-z0-9_-]{30,}', token) for token in tokens), tokens)

    def test_token_is_single_use_and_reuse_never_calls_the_port_again(self):
        token = self.first()['confirm_token']
        done = self.confirm(token)
        self.assertFalse(done['isError'], done)
        self.assertEqual(done['structuredContent']['status'], 'cancel_requested')
        self.assertEqual(len(self.hub.cancels), 1)
        for _ in range(3):
            self.assert_mismatch(self.confirm(token))
        self.assertEqual(len(self.hub.cancels), 1)

    def test_parallel_use_of_one_token_dispatches_exactly_once(self):
        token = self.first()['confirm_token']
        results: list = []
        gate = threading.Barrier(8)

        def worker():
            client = Rpc(self.hub, self.r1.key)
            gate.wait(10)
            results.append(client.call('inbox_cancel', request_id=self.rid, confirm_token=token))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sum(not r['isError'] for r in results), 1, results)
        self.assertEqual(len(self.hub.cancels), 1)

    def test_token_belongs_to_the_key_and_the_request(self):
        token = self.first(self.ed)['confirm_token']
        self.assert_mismatch(self.confirm(token, self.ed2))  # another editor key, same scope
        self.assert_mismatch(self.confirm(token, self.r1))  # a different grade
        self.assert_untouched()
        other = self.submit(self.ed)['request_id']
        self.hub.link_running(other, task_id='task-2', view={'command': COMMAND})
        self.assert_mismatch(self.confirm(token, self.ed, other))  # the same key, another request
        self.assertEqual(self.hub.cancels, [])
        # the owner's token still works afterwards (a foreign attempt does not burn it)
        self.assertFalse(self.confirm(token, self.ed)['isError'])
        self.assertEqual([event.request_id for event in self.hub.cancels], [self.rid])

    def test_session_does_not_matter_but_the_key_does(self):
        token = self.first(self.ed)['confirm_token']
        fresh = self.rpc(self.ed.key)  # a brand-new MCP session of the same key
        self.assertFalse(self.confirm(token, fresh)['isError'])

    def test_empty_garbage_and_tampered_tokens(self):
        token = self.first()['confirm_token']
        for bad in ('', ' ', 'x' * 32, token[:-1], token + 'A', token.upper(), token[::-1], 'null', '0'):
            if bad == token:
                continue
            with self.subTest(bad=bad[:12]):
                self.assert_mismatch(self.confirm(bad))
        self.assert_untouched()
        self.assertFalse(self.confirm(token)['isError'], 'the real token is unharmed by wrong guesses')

    def test_ttl_boundary(self):
        short = self.first()['confirm_token']
        self.hub.mono[0] += 59.0
        self.assertFalse(self.confirm(short)['isError'], 'inside 60 s')
        self.expired_token_on_another_request()

    def expired_token_on_another_request(self):
        rid = self.submit(self.r1)['request_id']
        self.hub.link_running(rid, task_id='task-9', view={'command': COMMAND})
        token = self.first(rid=rid)['confirm_token']
        self.hub.mono[0] += 60.5
        self.assert_mismatch(self.confirm(token, rid=rid))
        self.assertEqual(len(self.hub.cancels), 1, 'only the first request was dispatched')
        # the expired token does not block a fresh round
        again = self.first(rid=rid)['confirm_token']
        self.assertFalse(self.confirm(again, rid=rid)['isError'])

    def test_every_bound_value_change_invalidates_the_token(self):
        # 1. request version (a comment bumps it)
        token = self.first()['confirm_token']
        self.r1.ok('inbox_comment', request_id=self.rid, body='changed my mind')
        self.assert_mismatch(self.confirm(token))
        # 2. editor edits a running request (new revision, new version)
        token = self.first()['confirm_token']
        self.ed.ok('inbox_update', request_id=self.rid, base_version=self.row(self.rid)[1], title='revised')
        self.assert_mismatch(self.confirm(token))
        # 3. task status
        token = self.first()['confirm_token']
        self.hub.inbox.observe_task(self.rid, 'task-1', 'held', view={'command': COMMAND})
        self.assert_mismatch(self.confirm(token))
        # 4. command id
        token = self.first()['confirm_token']
        self.hub.inbox.observe_task(self.rid, 'task-1', 'held', view={'command': dict(COMMAND, command_id='cmd-2')})
        self.assert_mismatch(self.confirm(token))
        # 5. the command went away
        token = self.first()['confirm_token']
        self.hub.inbox.observe_task(self.rid, 'task-1', 'held', view={})
        self.assert_mismatch(self.confirm(token))
        self.assertEqual(self.hub.cancels, [])
        self.assertEqual(self.row(self.rid)[0], 'RUNNING')
        # and a fresh token on the new state works
        fresh = self.first()
        self.assertIsNone(fresh['impact']['command'])
        self.assertFalse(self.confirm(fresh['confirm_token'])['isError'])
        self.assertEqual(len(self.hub.cancels), 1)

    def test_a_token_survives_nothing_once_the_request_left_running(self):
        token = self.first()['confirm_token']
        self.hub.inbox.observe_task(self.rid, 'task-1', 'closed', closed_reason='done', view={})
        self.assert_mismatch(self.confirm(token))
        self.assertEqual(self.hub.cancels, [])

    def test_a_second_cancel_after_confirmation_does_not_mint_another_dispatch(self):
        self.assertFalse(self.confirm(self.first()['confirm_token'])['isError'])
        again = self.r1.call('inbox_cancel', request_id=self.rid)
        self.assertTrue(again['isError'])
        self.assertIn(again['structuredContent']['error']['code'], {'invalid_transition', 'token_mismatch'})
        self.assertEqual(len(self.hub.cancels), 1)

    def test_a_refused_confirmation_still_uses_the_token_up(self):
        token = self.first()['confirm_token']
        self.hub.backend_up = False
        refused = self.confirm(token)
        self.assertTrue(refused['isError'])
        self.assertEqual(refused['structuredContent']['error']['code'], 'backend_not_running')
        self.assertEqual(self.hub.cancels, [])
        self.hub.backend_up = True
        self.assert_mismatch(self.confirm(token))  # once presented, a token is gone (SPEC: 한 번 쓰면 사라진다)
        self.assert_untouched()
        self.assertFalse(self.confirm(self.first()['confirm_token'])['isError'])
        self.assertEqual(len(self.hub.cancels), 1)

    def test_ready_needs_no_token_and_pre_dispatch_cancels_at_once(self):
        ready = self.submit(self.r1)['request_id']
        self.hub.link_running(ready, task_id='task-3', status='dispatched')
        self.assertEqual(self.r1.ok('inbox_cancel', request_id=ready), {'status': 'cancel_requested'})
        self.assertEqual([event.request_id for event in self.hub.cancels], [ready])
        fresh = self.submit(self.r1)['request_id']
        self.assertEqual(self.r1.ok('inbox_cancel', request_id=fresh)['state'], 'CLOSED')
        self.assertEqual(len(self.hub.cancels), 1)

    def test_requester_cannot_cancel_or_mint_tokens_for_others(self):
        r2 = self.rpc(self.new_key('r2'))
        error = r2.err('inbox_cancel', request_id=self.rid)
        self.assertEqual(error['code'], 'not_found')
        foreign = self.confirm('anything', r2)  # not their request: no more than not_found, never a dispatch
        self.assertEqual(foreign['structuredContent']['error']['code'], 'not_found')
        self.assert_untouched()


# --- 10. results --------------------------------------------------------------------------------

class ResultTests(Case):
    def setUp(self):
        super().setUp()
        self.r1 = self.rpc(self.new_key('r1'))
        self.rid = self.submit(self.r1)['request_id']
        self.hub.link_running(self.rid, view={'runs': [
            {'run_id': 'run-1', 'status': 'finished', 'exit_code': 0, 'log': 'RAW-LOG-SECRET', 'report': 'WORKER-REPORT'}]})
        self.outside = self.hub.base / 'outside-secret.txt'
        self.outside.write_text('TOP-SECRET-OUTSIDE')
        (self.hub.root_a / 'inside.txt').write_text('INSIDE-PUBLIC')
        (self.hub.root_a / 'link').symlink_to(self.outside)
        (self.hub.root_a / '.wb' / 'private.txt').write_text('WB-PRIVATE')
        (self.hub.root_a / '.git').mkdir()
        (self.hub.root_a / '.git' / 'config').write_text('GIT-PRIVATE')

    SECRETS = ('RAW-LOG-SECRET', 'WORKER-REPORT', 'TOP-SECRET-OUTSIDE', 'WB-PRIVATE', 'GIT-PRIVATE', 'root:x:0:0')

    def assert_no_secret(self, result):
        text = json.dumps(result)
        for secret in self.SECRETS:
            self.assertNotIn(secret, text)

    def test_default_is_the_summary_only(self):
        result = self.r1.ok('inbox_result', request_id=self.rid)
        self.assertEqual(set(result), {'summary', 'state', 'outcome', 'task'})
        self.assertEqual(result['task']['runs'], [{'run_id': 'run-1', 'status': 'finished', 'exit_code': 0}])
        self.assertEqual(result, self.r1.ok('inbox_result', request_id=self.rid, full=False))
        self.assert_no_secret(result)

    def test_nothing_beyond_the_summary_without_an_explicit_full(self):
        for args in ({}, {'full': False}, {'length': 10, 'offset': 0}, {'run_id': 'run-1'},
                     {'artifact_path': 'inside.txt'}):
            with self.subTest(args=args):
                call = self.r1.call('inbox_result', request_id=self.rid, **args)
                self.assert_no_secret(call)
                self.assertNotIn('INSIDE-PUBLIC', json.dumps(call), 'artifact content needs full:true (TI-14)')

    def test_full_and_artifact_paths_never_reach_outside_the_project(self):
        paths = ['/etc/passwd', '../outside-secret.txt', '../../../../etc/passwd', 'link', './link', '.wb/private.txt',
                 '.git/config', '..', '', '/', '//etc//passwd', 'inside.txt/../../outside-secret.txt',
                 'a\x00b', str(self.outside), '~/.ssh/id_rsa', '%2e%2e/outside-secret.txt',
                 '....//outside-secret.txt', 'inside.txt\\..\\..\\outside-secret.txt']
        for path in paths:
            with self.subTest(path=path):
                call = self.r1.call('inbox_result', request_id=self.rid, full=True, artifact_path=path)
                self.assert_no_secret(call)
                if call['isError']:
                    self.assertIn(call['structuredContent']['error']['code'],
                                  {'invalid_input', 'forbidden', 'not_found'})
                    self.assertNotRegex(json.dumps(call), LEAK)

    def test_page_arguments_are_bounded(self):
        for args in ({'length': 0}, {'length': -1}, {'length': 131073}, {'length': 10 ** 12}, {'offset': -5},
                     {'length': True}, {'offset': 1.5}):
            with self.subTest(args=args):
                self.assertEqual(self.r1.err('inbox_result', request_id=self.rid, full=True, **args)['code'],
                                 'invalid_input')
        ok = self.r1.call('inbox_result', request_id=self.rid, full=True, length=131072, offset=0)
        self.assert_no_secret(ok)

    def test_full_with_the_backend_off_is_the_summary_plus_the_last_task_view_not_an_error(self):
        """TI-14: ``full:true`` is served; with no backend running it is the summary and the last ``task_view`` only."""
        result = self.r1.call('inbox_result', request_id=self.rid, full=True)
        self.assertFalse(result['isError'], result)
        value = result['structuredContent']
        self.assertIn('summary', value)
        self.assertEqual((value['full']['available'], value['full']['reason']), (False, 'backend_not_running'))
        self.assertNotIn('full_unavailable', value)
        self.assertEqual(value['full']['view']['runs'][0]['run_id'], 'run-1')
        self.assert_no_secret(result)

    def test_result_follows_the_visibility_rules(self):
        r2 = self.rpc(self.new_key('r2'))
        self.assertEqual(r2.err('inbox_result', request_id=self.rid, full=True)['code'], 'not_found')
        self.assertEqual(r2.err('inbox_result', request_id=self.rid)['code'], 'not_found')


# --- 11. errors never leak internals ------------------------------------------------------------

def sqlite_error(text: str) -> Exception:
    import sqlite3
    return sqlite3.OperationalError(text)


class ErrorHygieneTests(Case):
    def test_store_lock_and_backend_failures_read_as_fixed_sentences(self):
        """The inbox layer is made to fail with path/sqlite text; the client must never see it."""
        from workbench.hub.inbox.limits import InboxError
        client = self.rpc(self.new_key('r'))
        rid = self.submit(client)['request_id']
        secret = '/home/dwchoo/.wb/inbox.sqlite3: sqlite3.OperationalError: disk I/O error (Traceback File "x.py")'
        calls = [('inbox_get', 'get', {'request_id': rid}), ('inbox_list', 'list_requests', {}),
                 ('inbox_submit', 'submit', {'project_id': self.hub.a, 'title': 't', 'body': 'b',
                                             'idempotency_key': 'k9'}),
                 ('inbox_comment', 'comment', {'request_id': rid, 'body': 'x'})]
        failures = [InboxError(code, secret) for code in ('project_unavailable', 'inbox_busy', 'inbox_mismatch',
                                                           'backend_not_running', 'backend_unreachable',
                                                           'hub_stopping', 'internal_error', 'inbox_unavailable',
                                                           'totally_unknown_code')]
        failures += [RuntimeError(secret), OSError(5, secret), sqlite_error(secret)]
        for name, attribute, args in calls:
            original = getattr(self.hub.inbox, attribute)
            self.addCleanup(setattr, self.hub.inbox, attribute, original)
            for failure in failures:
                def boom(*a, _failure=failure, **k):
                    raise _failure
                setattr(self.hub.inbox, attribute, boom)
                with self.subTest(tool=name, failure=repr(failure)[:40]):
                    reply = client.rpc('tools/call', {'name': name, 'arguments': args})
                    self.assertEqual(reply.status, 200, reply.body)
                    self.assertNotIn('dwchoo', reply.body.decode())
                    self.assertNotRegex(reply.body.decode(), LEAK)
                    result = reply.json['result']
                    self.assertTrue(result['isError'])
                    error = result['structuredContent']['error']
                    self.assertIn(error['code'], SPEC_CODES)
                    self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
            setattr(self.hub.inbox, attribute, original)
        self.assertEqual(client.rpc('ping').status, 200)

    def test_input_errors_carry_no_paths_or_stack(self):
        ed = self.rpc(self.new_key('ed', 'editor'))
        probes = [('inbox_get', {'request_id': '../../etc/passwd'}), ('inbox_get', {'request_id': "x' OR '1'='1"}),
                  ('inbox_get', {'request_id': 'z' * 10000}), ('inbox_list', {'cursor': '../../etc/passwd'}),
                  ('inbox_list', {'cursor': 'x' * 10000}), ('inbox_list', {'project_id': '/etc'}),
                  ('inbox_result', {'request_id': str(uuid.uuid4()), 'artifact_path': '/etc/passwd', 'full': True}),
                  ('inbox_cancel', {'request_id': str(uuid.uuid4()), 'confirm_token': '/home/x'}),
                  ('inbox_submit', {'project_id': str(self.hub.root_a), 'title': 't', 'body': 'b',
                                    'idempotency_key': 'k'})]
        for name, args in probes:
            with self.subTest(name=name, args=str(args)[:50]):
                result = ed.call(name, **args)
                text = json.dumps(result)
                self.assertNotRegex(text, LEAK)
                if result['isError']:
                    error = result['structuredContent']['error']
                    self.assertIn(error['code'], SPEC_CODES - {'internal_error'}, error)
                    self.assertLessEqual(len(error['detail']), 600)
        for body in (b'{', b'[]', b'{"jsonrpc":"2.0","id":1,"method":5}'):
            self.assertNotRegex(ed.send('POST', body).body.decode(), LEAK)

    def test_every_tool_error_has_only_spec_keys(self):
        ed = self.rpc(self.new_key('ed', 'editor'))
        error = ed.err('inbox_get', request_id=str(uuid.uuid4()))
        self.assertEqual(set(error) - {'state', 'version'}, {'code', 'detail'})
        self.assertIn(error['code'], SPEC_CODES)


# --- 12. last_used_at ---------------------------------------------------------------------------

class LastUsedTests(Case):
    def test_last_use_is_flushed_within_about_ten_seconds_by_default(self):
        key = self.new_key('r')
        self.assertIsNone(self.hub.key_row(key)[0])
        self.rpc(key).rpc('ping')
        self.hub.now[0] += 3.0
        marker = self.hub.now[0]
        self.rpc(key).rpc('tools/list')
        deadline = time.monotonic() + 14.0
        seen = None
        while time.monotonic() < deadline:
            seen = self.hub.key_row(key)[0]
            if seen is not None and seen >= marker:
                break
            time.sleep(0.25)
        self.assertIsNotNone(seen, 'last_used_at was never written within 14 s')
        self.assertGreaterEqual(seen, marker)

    def test_refused_keys_do_not_mark_a_last_use(self):
        hub = McpHub(self, flush_interval=0.2)
        revoked = hub.key('gone')
        hub.keys.revoke(revoked[4:14], now=hub.now[0])
        hub.client(revoked).request('initialize', {'protocolVersion': '2025-06-18'})
        hub.client(revoked + 'x').request('initialize', {'protocolVersion': '2025-06-18'})
        time.sleep(1.0)
        self.assertIsNone(hub.key_row(revoked)[0])
        live = hub.key('live')
        hub.client(live).request('initialize', {'protocolVersion': '2025-06-18'})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and hub.key_row(live)[0] is None:
            time.sleep(0.1)
        self.assertIsNotNone(hub.key_row(live)[0])


if __name__ == '__main__':
    unittest.main()
