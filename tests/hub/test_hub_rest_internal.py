"""WBX-B5: the Board agent's internal listener serves the ``/api/v1/`` REST API only, to this start's system key only.

Seam S2 (``support/rest_hub.py`` real key store and inbox + ``support/rest_client.py``) with the real
:class:`workbench.hub.rest.InternalListener` on its own loopback port. Expected values: web-board-ux SPEC "Board agent
전환" (REST only, Host ``127.0.0.1:<port>`` fixed, 16 connections, other paths 404, no ``/skill/``, only this start's
system key: other valid keys and the previous system key 401) and BRIEF WBX-D24.

Carried over from the MCP internal listener tests (``test_hub_mcp_transport.py`` ``InternalListenerTests``, 9 cases,
and ``test_hub_mcp.py`` ``LastUsedTests.test_internal_listener_writes_last_use``); each test names its MCP original.
Run with ``discover -s tests/hub -p 'test_hub_rest*.py'``.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import socket
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.web_sandbox import tcp_listening  # noqa: E402
from support.rest_client import RestClient  # noqa: E402
from support.rest_hub import RestHub  # noqa: E402
from workbench.hub import auth, rest  # noqa: E402


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


class InternalCase(unittest.TestCase):
    def setUp(self):
        self.hub = RestHub(self)
        self.listener = rest.InternalListener(self.hub.keys, self.hub.api, clock=lambda: self.hub.now[0])
        self.url = self.hub.run(self.listener.start())
        self.addCleanup(lambda: self.hub.run(self.listener.stop()))
        self.port = self.listener.port
        self.board_key = self.hub.keys.rotate_system_key(now=self.hub.now[0]).raw

    def board(self, key=None, **kwargs) -> RestClient:
        return RestClient('127.0.0.1', self.port, key or self.board_key, **kwargs)


class InternalListenerTests(InternalCase):
    def test_url_is_the_bare_loopback_origin_and_binds_only_loopback(self):  # MCP test_url_and_bind
        self.assertEqual(self.url, f'http://127.0.0.1:{self.port}')
        self.assertEqual(self.listener.url, self.url)
        self.assertNotEqual(self.port, self.hub.port)
        found = [(address, port) for address, port in tcp_listening(os.getpid()) if port == self.port]
        self.assertEqual(found, [('127.0.0.1', self.port)])

    def test_the_system_key_uses_the_rest_api(self):  # MCP test_system_key_uses_the_tools
        client = self.board()
        self.assertEqual(client.get('/projects').status, 200)
        reply = client.post('/requests', {'project_id': self.hub.a, 'title': 'From board', 'body': 'B',
                                          'idempotency_key': 'b1'})
        self.assertEqual(reply.status, 201, reply.body)
        self.assertEqual(reply.json['origin']['kind'], 'board_agent')
        listed = client.get('/requests')
        self.assertEqual(listed.status, 200)
        self.assertEqual([item['request_id'] for item in listed.json['requests']], [reply.json['request_id']])

    def test_a_key_is_still_required(self):  # MCP test_key_still_required
        reply = RestClient('127.0.0.1', self.port, None).get('/requests')
        self.assertEqual(reply.status, 401)
        self.assertEqual(reply.json['error']['code'], 'unauthorized')

    def test_host_is_fixed(self):  # MCP test_host_is_fixed
        for host in (f'localhost:{self.port}', f'127.0.0.1:{self.port + 1}', '127.0.0.1', f'[::1]:{self.port}'):
            with self.subTest(host=host):
                self.assertEqual(self.board(host_header=host).get('/requests').status, 421)
        self.assertEqual(self.board().get('/requests').status, 200)

    def test_other_routes_are_404_and_there_is_no_mcp(self):  # MCP test_other_routes_are_404
        client = self.board()
        for path in ('/', '/login', '/static/base.css', '/api/badge', '/mcp', '/mcp/x', '/skill/',
                     '/skill/wb-inbox/SKILL.md', '/board', '/settings'):
            with self.subTest(path=path):
                self.assertEqual(client.raw('GET', path).status, 404)
        reply = client.raw('POST', '/mcp', b'{"jsonrpc": "2.0", "id": 1, "method": "initialize"}',
                           {'Content-Type': 'application/json', 'Accept': 'application/json'})
        self.assertEqual(reply.status, 404)

    def test_foreign_origin_is_403(self):  # MCP test_foreign_origin_is_403
        reply = self.board().get('/requests', headers={'Origin': 'http://evil.example'})
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.json['error']['code'], 'origin_mismatch')

    def test_concurrent_starts_open_one_listener_and_stop_forgets_the_port(self):  # MCP same name
        listener = rest.InternalListener(self.hub.keys, self.hub.api)

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

    def test_connections_over_sixteen_are_closed_at_once(self):  # MCP same name (security review fix 5)
        held = [socket.create_connection(('127.0.0.1', self.port), timeout=5) for _ in range(16)]
        try:
            time.sleep(0.3)
            extra = socket.create_connection(('127.0.0.1', self.port), timeout=5)
            try:
                extra.sendall(b'GET /api/v1/requests HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
                try:
                    closed = extra.recv(65536) == b''
                except ConnectionResetError:
                    closed = True
                self.assertTrue(closed, 'the 17th connection was served')
            finally:
                extra.close()
            held[0].settimeout(0.3)
            with self.assertRaises(socket.timeout):
                held[0].recv(1)  # a held connection stays open
        finally:
            for connection in held:
                connection.close()
        time.sleep(0.3)
        self.assertEqual(self.board().get('/requests').status, 200)

    def test_stop_closes_the_port(self):  # MCP same name
        self.hub.run(self.listener.stop())
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=2).close()


class SystemKeyOnlyTests(InternalCase):
    """WBX-D24: the internal listener takes this start's system key only; the external REST takes every valid key."""

    def test_other_valid_keys_are_401_here_and_200_on_the_external_listener(self):
        for name, grade in (('owner-editor', 'editor'), ('owner-requester', 'requester')):
            with self.subTest(grade=grade):
                key = self.hub.key(name, grade)
                reply = self.board(key).get('/requests')
                self.assertEqual(reply.status, 401)
                self.assertEqual(reply.json['error']['code'], 'unauthorized')
                self.assertEqual(self.board(key).raw('GET', '/api/v1/nothing').status, 401)
                self.assertEqual(self.hub.rest(key).get('/requests').status, 200)

    def test_the_previous_system_key_is_401_after_a_rotation(self):
        old = self.board_key
        self.assertEqual(self.board(old).get('/requests').status, 200)
        new = self.hub.keys.rotate_system_key(now=self.hub.now[0]).raw
        self.assertEqual(self.board(old).get('/requests').status, 401)
        self.assertEqual(self.board(new).get('/requests').status, 200)

    def test_a_refused_owner_key_marks_no_last_use(self):
        hub = RestHub(self, flush_interval=0.2)
        own = auth.ApiKeyStore(hub.db)
        listener = rest.InternalListener(own, hub.api, clock=lambda: hub.now[0], flush_interval=0.2)
        hub.run(listener.start())
        self.addCleanup(lambda: hub.run(listener.stop()))
        owner = hub.key('owner', 'editor')
        self.assertEqual(RestClient('127.0.0.1', listener.port, owner).get('/requests').status, 401)
        time.sleep(1.0)
        self.assertIsNone(hub.key_row(owner)[0])


class InternalLastUsedTests(unittest.TestCase):
    """SPEC API key: ``last_used_at`` is gathered in memory and written every 10 s (shortened here)."""

    def test_internal_listener_writes_last_use(self):  # MCP test_hub_mcp.LastUsedTests, same name
        hub = RestHub(self, flush_interval=0.2)
        own = auth.ApiKeyStore(hub.db)  # its own pending last-use map: only its loop can write it
        listener = rest.InternalListener(own, hub.api, clock=lambda: hub.now[0], flush_interval=0.2)
        hub.run(listener.start())
        self.addCleanup(lambda: hub.run(listener.stop()))
        key = hub.keys.rotate_system_key(now=hub.now[0]).raw
        hub.now[0] += 5.0
        self.assertEqual(RestClient('127.0.0.1', listener.port, key).get('/projects').status, 200)
        self.assertTrue(wait_until(lambda: hub.key_row(key)[0] == hub.now[0]), hub.key_row(key))


if __name__ == '__main__':
    unittest.main()
