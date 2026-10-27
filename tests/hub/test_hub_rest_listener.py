"""WBX-B1 REST on the external listener: ``allow_from`` (seam S1) and a real Hub with ``[web] enabled`` (seam S3).

S1: ``create_app(services, routes=[rest.routes(api)])`` in-process on real loopback sockets (aiohttp ``TestServer``),
the client's source address chosen with ``local_addr``; expected values from the 2026-10-27 ``allow_from`` decision
(bare 403, no body, before any handler) and WBX-AC-11. S3: a real ``wb _hub --foreground`` in ``WebSandbox`` with a
key made directly in its ``hub.sqlite3`` before the Hub starts (WBX-AC-12: a key from before REST works with its
grade and scope) and ``[web] enabled = false`` (no listener, so no REST; WBX-AC-11).
Run with ``discover -s tests/hub -p 'test_hub_rest*.py'``.
"""
from __future__ import annotations

import ipaddress
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
import uuid

from aiohttp import ClientSession, DummyCookieJar, TCPConnector, test_utils

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.web_sandbox import WebSandbox, tcp_listening  # noqa: E402
from support.rest_client import RestClient  # noqa: E402
from workbench.hub import auth, rest  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402

PASSWORD = 'correct horse battery'


class _RecordingApi:
    """Stands in for the processing layer: counts key checks (a handler that ran) and refuses every key."""

    def __init__(self):
        self.authenticated = 0

    async def authenticate(self, request):
        self.authenticated += 1
        return None


class AllowFromTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, allow_from: tuple):
        temp = tempfile.TemporaryDirectory(prefix='wb-rest-allow-')
        self.addCleanup(temp.cleanup)
        state = Path(temp.name) / 'hub'
        passwords = auth.PasswordStore(state)
        passwords.set_password(PASSWORD)
        db = auth.HubDb.open(state)
        self.addCleanup(db.close)
        services = WebServices(passwords, db, OriginPolicy('127.0.0.1', allow_from=allow_from))
        self.api = _RecordingApi()
        server = test_utils.TestServer(create_app(services, routes=[rest.routes(self.api)]), host='127.0.0.1',
                                       port=0)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        self.base = f'http://127.0.0.1:{server.port}'
        self.http = ClientSession(cookie_jar=DummyCookieJar(), connector=TCPConnector(local_addr=('127.0.0.1', 0)))
        self.addAsyncCleanup(self.http.close)

    async def call(self, method: str, path: str, data: bytes | None = None):
        headers = {'Authorization': 'Bearer wbk_x', 'Content-Type': 'application/json'}
        async with self.http.request(method, self.base + path, headers=headers, data=data,
                                     allow_redirects=False) as response:
            return response.status, await response.read(), response.headers.getall('Set-Cookie', [])

    async def test_a_peer_outside_allow_from_gets_a_bare_403_before_any_rest_handler(self):
        await self.serve((ipaddress.ip_network('10.0.0.0/8'),))
        for method, path, data in (('GET', '/api/v1/projects', None), ('POST', '/api/v1/requests', b'{}'),
                                   ('PATCH', f'/api/v1/requests/{uuid.uuid4()}', b'{}'),
                                   ('GET', '/api/v1/nope', None), ('OPTIONS', '/api/v1/requests', None)):
            with self.subTest(method=method, path=path):
                self.assertEqual(await self.call(method, path, data), (403, b'', []))
        self.assertEqual(self.api.authenticated, 0, 'no REST handler ran')

    async def test_a_listed_peer_reaches_the_rest_handler(self):
        await self.serve((ipaddress.ip_network('127.0.0.1/32'),))
        status, body, _cookies = await self.call('GET', '/api/v1/projects')
        self.assertEqual(status, 401, body)
        self.assertEqual(self.api.authenticated, 1)


def unused_port() -> int:
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class RealHubTests(unittest.TestCase):
    def setUp(self):
        self.box = WebSandbox(self)

    def issue_before_start(self, *args) -> str:
        """A key made in ``hub.sqlite3`` by the key store itself, before the Hub (and its REST) runs."""
        db = auth.HubDb.open(self.box.state)
        try:
            return auth.ApiKeyStore(db).issue(*args, now=time.time()).raw
        finally:
            db.close()

    def test_an_existing_key_works_over_the_real_hubs_rest_with_its_grade_and_scope(self):
        self.box.write_config('127.0.0.1', 0, enabled=True)
        self.box.set_password()
        scoped = str(uuid.uuid4())
        requester = self.issue_before_start('old-requester', 'requester', {'projects': [scoped]}, None)
        editor = self.issue_before_start('old-editor', 'editor', {'all': True}, None)
        hub = self.box.start_hub()
        host, port = self.box.wait_listening(hub.pid)
        reply = RestClient(host, port, editor).get('/projects')
        self.assertEqual((reply.status, reply.json), (200, {'projects': []}), reply.body)
        rid = str(uuid.uuid4())
        reply = RestClient(host, port, requester).patch(f'/requests/{rid}', {'base_version': 1, 'title': 'x'})
        self.assertEqual((reply.status, reply.json['error']['code']), (403, 'forbidden'), reply.body)
        reply = RestClient(host, port, requester).post('/requests', {'project_id': str(uuid.uuid4()), 'title': 't',
                                                                    'body': 'b', 'idempotency_key': 'k'})
        self.assertEqual((reply.status, reply.json['error']['code']), (403, 'forbidden'), reply.body)
        reply = RestClient(host, port, None).get('/projects')
        self.assertEqual((reply.status, reply.json['error']['code']), (401, 'unauthorized'), reply.body)
        self.assertEqual(self.box.stop_hub(hub), 0)

    def test_enabled_false_has_no_rest(self):
        port = unused_port()
        self.box.write_config('127.0.0.1', port, enabled=False)
        self.box.set_password()
        key = self.issue_before_start('old-editor', 'editor', {'all': True}, None)
        hub = self.box.start_hub()
        time.sleep(2.5)  # past one watchdog poll: a wrong implementation would have opened by now
        self.assertEqual(tcp_listening(hub.pid), [])
        with self.assertRaises(ConnectionRefusedError):
            RestClient('127.0.0.1', port, key, timeout=2).get('/projects')
        self.assertEqual(self.box.stop_hub(hub), 0)


if __name__ == '__main__':
    unittest.main()
