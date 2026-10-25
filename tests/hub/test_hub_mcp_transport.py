"""TI-05 MCP Streamable HTTP transport details (JSON-RPC 2.0 framing, session, headers, limits).

Real loopback Hub web app + raw stdlib ``http.client`` requests. Expected values: the SPEC "MCP
Streamable HTTP endpoint" transport bullets (status codes, headers, JSON-RPC codes) and JSON-RPC
2.0 rules (-32700 parse error, -32600 invalid request, -32601 method not found, -32602 invalid
params). Run with ``discover -s tests/hub -p 'test_hub_mcp*.py'``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from fixtures.web_sandbox import tcp_listening  # noqa: E402
from support.mcp_client import McpClient  # noqa: E402
from support.mcp_hub import McpHub  # noqa: E402
from workbench.hub import mcp  # noqa: E402


class TransportCase(unittest.TestCase):
    def setUp(self):
        self.hub = McpHub(self)
        self.key = self.hub.key('r1')
        self.client = self.hub.client(self.key)


class InitializeTests(TransportCase):
    def test_initialize_result_and_session_header(self):
        result = self.client.initialize('2025-06-18')
        self.assertEqual(result['protocolVersion'], '2025-06-18')
        self.assertEqual(result['capabilities'], {'tools': {'listChanged': False}})
        self.assertEqual(result['serverInfo']['name'], 'workbench-hub')
        self.assertIsInstance(result['serverInfo']['version'], str)
        self.assertTrue(result['serverInfo']['version'])
        self.assertIsNotNone(self.client.session_id)
        self.assertGreaterEqual(len(self.client.session_id), 16)
        other = self.hub.client(self.key)
        other.initialize()
        self.assertNotEqual(other.session_id, self.client.session_id)

    def test_version_negotiation(self):
        self.assertEqual(self.client.initialize('2025-03-26')['protocolVersion'], '2025-03-26')
        for asked in ('2024-11-05', '2099-01-01', 7):
            with self.subTest(asked=asked):
                self.assertEqual(self.hub.client(self.key).initialize(asked)['protocolVersion'], '2025-06-18')

    def test_reply_echoes_the_request_id(self):
        for request_id in (1, 'abc', 0):
            with self.subTest(request_id=request_id):
                reply = self.client.post({'jsonrpc': '2.0', 'id': request_id, 'method': 'initialize',
                                          'params': {'protocolVersion': '2025-06-18'}})
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.header('content-type').split(';')[0], 'application/json')
                self.assertEqual(reply.json['id'], request_id)
                self.assertEqual(reply.json['jsonrpc'], '2.0')

    def test_initialized_notification_is_202_without_body(self):
        self.client.initialize()
        reply = self.client.initialized()
        self.assertEqual(reply.status, 202)
        self.assertEqual(reply.body, b'')

    def test_ping(self):
        self.client.initialize()
        self.assertEqual(self.client.result('ping'), {})

    def test_requests_without_a_session_id_are_accepted(self):
        self.assertEqual(self.client.result('ping'), {})

    def test_unknown_session_id_is_404(self):
        self.client.initialize()
        self.client.session_id = 'forged-' + 'x' * 20
        self.assertEqual(self.client.request('ping').status, 404)

    def test_session_of_another_key_is_404(self):
        self.client.initialize()
        thief = self.hub.client(self.hub.key('r2'))
        thief.session_id = self.client.session_id
        self.assertEqual(thief.request('ping').status, 404)
        self.assertEqual(self.client.result('ping'), {})

    def test_delete_ends_the_session(self):
        self.client.initialize()
        reply = self.client.close_session()
        self.assertEqual(reply.status, 204)
        self.assertEqual(self.client.request('ping').status, 404)
        self.assertEqual(self.client.close_session().status, 404)

    def test_unsupported_protocol_version_header_is_400(self):
        self.client.initialize()
        self.assertEqual(self.client.request('ping', headers={'MCP-Protocol-Version': '1999-01-01'}).status, 400)
        self.assertEqual(self.client.request('ping', headers={'MCP-Protocol-Version': '2025-03-26'}).status, 200)

    def test_get_is_405(self):
        self.client.initialize()
        reply = self.client.raw('GET', headers={'Accept': 'text/event-stream'})
        self.assertEqual(reply.status, 405)


class FramingTests(TransportCase):
    def assert_rpc_error(self, reply, code, status, request_id=None):
        self.assertEqual(reply.status, status, reply.body)
        self.assertEqual(reply.json['jsonrpc'], '2.0')
        self.assertEqual(reply.json['error']['code'], code)
        self.assertEqual(reply.json['id'], request_id)
        self.assertNotIn('result', reply.json)

    def body(self, raw: bytes, **headers):
        return self.client.raw('POST', raw, headers)

    def test_unknown_method_is_32601(self):
        reply = self.client.post({'jsonrpc': '2.0', 'id': 9, 'method': 'resources/list'})
        self.assert_rpc_error(reply, -32601, 200, 9)

    def test_batch_is_refused_as_invalid_request(self):
        batch = [{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}]
        self.assert_rpc_error(self.client.post(batch), -32600, 400)
        self.assert_rpc_error(self.client.post([]), -32600, 400)

    def test_parse_errors_are_32700(self):
        for raw in (b'{', b'not json', b'\xff\xfe{}', b'{"jsonrpc":"2.0","id":1,"method":"ping","x":"\xe9"}',
                    b'{"jsonrpc":"2.0","id":1,"method":"ping","n":NaN}',
                    b'{"jsonrpc":"2.0","id":1,"method":"ping","n":Infinity}',
                    b'{"jsonrpc":"2.0","id":1,"method":"ping","n":1e999}',
                    b'{"jsonrpc":"2.0","id":1,"method":"ping","n":' + b'9' * 5000 + b'}',
                    b'[' * 100000 + b']' * 100000, b''):
            with self.subTest(raw=raw[:40]):
                self.assert_rpc_error(self.body(raw), -32700, 400)

    def test_invalid_requests_are_32600(self):
        for message in ('ping', 3, None, {'id': 1, 'method': 'ping'}, {'jsonrpc': '1.0', 'id': 1, 'method': 'ping'},
                        {'jsonrpc': '2.0', 'id': 1, 'method': 5}, {'jsonrpc': '2.0', 'id': 1},
                        {'jsonrpc': '2.0', 'id': {'a': 1}, 'method': 'ping'},
                        {'jsonrpc': '2.0', 'id': True, 'method': 'ping'},
                        {'jsonrpc': '2.0', 'id': None, 'method': 'ping'}):
            with self.subTest(message=message):
                self.assert_rpc_error(self.client.post(message), -32600, 400)

    def test_params_must_be_an_object(self):
        for params in ([1], 'x', 5):
            with self.subTest(params=params):
                reply = self.client.post({'jsonrpc': '2.0', 'id': 4, 'method': 'ping', 'params': params})
                self.assert_rpc_error(reply, -32602, 200, 4)

    def test_a_tool_call_without_an_id_runs_nothing(self):
        reply = self.client.post({'jsonrpc': '2.0', 'method': 'tools/call', 'params': {
            'name': 'inbox_submit', 'arguments': {'project_id': self.hub.a, 'title': 'T', 'body': 'B',
                                                  'idempotency_key': 'n1'}}})
        self.assertEqual((reply.status, reply.body), (202, b''))
        self.assertEqual(self.hub.inbox_rows('SELECT count(*) FROM requests'), [])

    def test_client_responses_and_notifications_are_202(self):
        for message in ({'jsonrpc': '2.0', 'id': 7, 'result': {}},
                        {'jsonrpc': '2.0', 'id': 7, 'error': {'code': 1, 'message': 'x'}},
                        {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 1}}):
            with self.subTest(message=message):
                reply = self.client.post(message)
                self.assertEqual(reply.status, 202)
                self.assertEqual(reply.body, b'')

    def test_accept_must_name_application_json(self):
        message = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}).encode()
        for accept in ('text/event-stream', 'text/html', None):
            with self.subTest(accept=accept):
                self.assertEqual(self.body(message, Accept=accept).status, 406)
        for accept in ('application/json', 'application/json;q=0.9, text/event-stream', 'Application/JSON'):
            with self.subTest(accept=accept):
                self.assertEqual(self.body(message, Accept=accept).status, 200)

    def test_content_type_must_be_json(self):
        message = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}).encode()
        for content_type in ('text/plain', 'application/x-www-form-urlencoded', None):
            with self.subTest(content_type=content_type):
                self.assertEqual(self.body(message, **{'Content-Type': content_type}).status, 415)
        self.assertEqual(self.body(message, **{'Content-Type': 'application/json; charset=utf-8'}).status, 200)

    def test_body_over_256_kib_is_413(self):
        padding = 'x' * (256 * 1024)
        message = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping', 'params': {'pad': padding}}).encode()
        self.assertEqual(self.body(message).status, 413)

    def test_chunked_body_over_256_kib_is_413(self):
        padding = 'x' * (256 * 1024)
        message = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping', 'params': {'pad': padding}}).encode()
        chunked = hex(len(message))[2:].encode() + b'\r\n' + message + b'\r\n0\r\n\r\n'
        reply = self.client.raw('POST', chunked, {'Transfer-Encoding': 'chunked', 'Content-Length': None})
        self.assertEqual(reply.status, 413)

    def test_413_is_a_json_rpc_error(self):
        message = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping',
                              'params': {'pad': 'x' * (256 * 1024)}}).encode()
        chunked = hex(len(message))[2:].encode() + b'\r\n' + message + b'\r\n0\r\n\r\n'
        for reply in (self.body(message),
                      self.client.raw('POST', chunked, {'Transfer-Encoding': 'chunked', 'Content-Length': None})):
            self.assert_rpc_error(reply, -32600, 413)

    def test_body_at_the_limit_is_read(self):
        head = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping', 'params': {'pad': ''}}).encode()
        message = head.replace(b'"pad": ""', b'"pad": "' + b'x' * (256 * 1024 - len(head)) + b'"')
        self.assertEqual(len(message), 256 * 1024)
        self.assertEqual(self.body(message).status, 200)

    def test_foreign_origin_is_403(self):
        for origin in ('http://evil.example', 'null', f'http://localhost:{self.hub.port + 1}'):
            with self.subTest(origin=origin):
                self.assertEqual(self.client.request('ping', headers={'Origin': origin}).status, 403)
                self.assertEqual(self.client.raw('GET', headers={'Origin': origin}).status, 403)
        own = f'http://127.0.0.1:{self.hub.port}'
        self.assertEqual(self.client.request('ping', headers={'Origin': own}).status, 200)

    def test_foreign_host_is_421(self):
        for host in ('evil.example', f'evil.example:{self.hub.port}', f'127.0.0.2:{self.hub.port}'):
            with self.subTest(host=host):
                reply = self.client.request('ping', headers={'Host': host})
                self.assertEqual(reply.status, 421)

    def test_errors_do_not_name_paths(self):
        reply = self.body(b'{')
        self.assertNotIn('/', reply.json['error']['message'])
        self.assertNotIn(str(self.hub.base).encode(), reply.body)


class SessionCapTests(TransportCase):
    """Memory bounds: one key's sessions are capped per key, so it cannot push other keys' sessions out."""

    def test_one_key_cannot_evict_another_keys_session(self):
        other = self.hub.client(self.hub.key('r2'))
        other.initialize()
        with mock.patch.object(mcp, 'SESSION_LIMIT', 4), mock.patch.object(mcp, 'SESSIONS_PER_KEY', 3):
            clients = [self.hub.client(self.key) for _ in range(6)]
            for client in clients:
                client.initialize()
            self.assertEqual(other.result('ping'), {})
            self.assertEqual([client.request('ping').status for client in clients], [404, 404, 404, 200, 200, 200])


class InternalListenerTests(TransportCase):
    """SPEC Board agent 세트: the internal listener serves ``/mcp`` only, on ``127.0.0.1``, Host fixed."""

    def setUp(self):
        super().setUp()
        self.listener = mcp.InternalListener(self.hub.keys, self.hub.server, clock=lambda: self.hub.now[0])
        self.url = self.hub.run(self.listener.start())
        self.addCleanup(lambda: self.hub.run(self.listener.stop()))
        self.port = self.listener.port
        self.board_key = self.hub.keys.rotate_system_key(now=self.hub.now[0]).raw

    def board(self, key=None, **kwargs):
        return McpClient('127.0.0.1', self.port, key or self.board_key, **kwargs)

    def test_url_and_bind(self):
        self.assertEqual(self.url, f'http://127.0.0.1:{self.port}/mcp')
        self.assertNotEqual(self.port, self.hub.port)
        found = [(address, port) for address, port in tcp_listening(os.getpid()) if port == self.port]
        self.assertEqual(found, [('127.0.0.1', self.port)])

    def test_system_key_uses_the_tools(self):
        client = self.board().start()
        self.assertEqual(len(client.list_tools()), 9)
        value = client.tool('inbox_submit', project_id=self.hub.a, title='From board', body='B', idempotency_key='b1')
        self.assertEqual(value['origin']['kind'], 'board_agent')

    def test_key_still_required(self):
        reply = McpClient('127.0.0.1', self.port, None).request('initialize', {'protocolVersion': '2025-06-18'})
        self.assertEqual(reply.status, 401)

    def test_host_is_fixed(self):
        client = self.board()
        for host in (f'localhost:{self.port}', f'127.0.0.1:{self.port + 1}', '127.0.0.1', f'[::1]:{self.port}'):
            with self.subTest(host=host):
                self.assertEqual(client.request('ping', headers={'Host': host}).status, 421)
        self.assertEqual(client.request('ping').status, 200)

    def test_other_routes_are_404(self):
        client = self.board()
        for path in ('/', '/login', '/static/base.css', '/api/badge', '/mcp/x'):
            with self.subTest(path=path):
                self.assertEqual(client.raw('GET', path=path).status, 404)

    def test_foreign_origin_is_403(self):
        self.assertEqual(self.board().request('ping', headers={'Origin': 'http://evil.example'}).status, 403)

    def test_concurrent_starts_open_one_listener_and_stop_forgets_the_port(self):
        listener = mcp.InternalListener(self.hub.keys, self.hub.server)

        async def both():
            return await asyncio.gather(listener.start(), listener.start())
        before = set(tcp_listening(os.getpid()))
        first, second = self.hub.run(both())
        self.addCleanup(lambda: self.hub.run(listener.stop()))
        self.assertEqual(first, second)
        self.assertEqual(len(set(tcp_listening(os.getpid())) - before), 1)
        self.hub.run(listener.stop())
        self.assertIsNone(listener.port)
        self.assertIsNone(listener.url)

    def test_stop_closes_the_port(self):
        self.hub.run(self.listener.stop())
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=2).close()


if __name__ == '__main__':
    unittest.main()
