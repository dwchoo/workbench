"""2026-10-27 사용자 결정: ``[web] allow_from`` refuses every client whose TCP peer address is not listed.

Seam T2: ``create_app(services)`` in-process with aiohttp's ``TestServer`` on real loopback sockets. The client's
source address is chosen with ``local_addr`` (127.0.0.1 or 127.0.0.2), so the peer the Hub sees is known.
Expected values come from the user decision: a plain 403 with no body and no ``Set-Cookie``, before Host, session,
CSRF, throttle and the route handler, on every route; ``X-Forwarded-For`` is never trusted; IPv4-mapped peers
match their IPv4 form. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import ipaddress
from pathlib import Path
import sys
import tempfile
import unittest

from aiohttp import ClientSession, DummyCookieJar, TCPConnector, test_utils

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.hub import auth, rest  # noqa: E402
from workbench.hub.web import middleware  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, board_routes, create_app  # noqa: E402

PASSWORD = 'correct horse battery'


def nets(*texts: str) -> tuple:
    return tuple(ipaddress.ip_network(text) for text in texts)


class _RecordingApi:
    """Stands in for ``InboxApi`` under the real ``/api/v1/`` handlers: the first thing a REST handler does is
    ``authenticate`` (a key check, or the 401), so ``calls`` counts the requests that reached a handler."""

    def __init__(self):
        self.calls = 0

    async def authenticate(self, request):
        self.calls += 1
        return None


class AllowFromCase(unittest.IsolatedAsyncioTestCase):
    async def serve(self, *, bind: str = '127.0.0.1', allow_from: tuple = (), source: str = '127.0.0.1'):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-allow-')
        self.addCleanup(self.temp.cleanup)
        state = Path(self.temp.name) / 'hub'
        self.passwords = auth.PasswordStore(state)
        self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(state)
        self.addCleanup(self.db.close)
        self.services = WebServices(self.passwords, self.db, OriginPolicy(bind, allow_from=allow_from))
        self.verified = 0
        verify = self.services.sessions.verify

        def counting_verify(*args):
            self.verified += 1
            return verify(*args)
        self.services.sessions.verify = counting_verify
        self.entered = 0
        enter = self.services.throttle.try_enter

        def counting_enter():
            self.entered += 1
            return enter()
        self.services.throttle.try_enter = counting_enter
        self.api = _RecordingApi()
        app = create_app(self.services, routes=[rest.routes(self.api), board_routes(lambda: None)])
        self.server = test_utils.TestServer(app, host=bind, port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.bind, self.port = bind, self.server.port
        self.http = ClientSession(cookie_jar=DummyCookieJar(), connector=TCPConnector(local_addr=(source, 0)))
        self.addAsyncCleanup(self.http.close)

    async def call(self, method: str, path: str, *, headers: dict | None = None, data=None):
        sent = {'Host': f'{self.bind}:{self.port}', **(headers or {})}
        async with self.http.request(method, f'http://{self.bind}:{self.port}{path}', headers=sent, data=data,
                                     allow_redirects=False) as response:
            response.body = await response.read()
            return response

    def assert_refused(self, response) -> None:
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body, b'')
        self.assertEqual(response.headers.getall('Set-Cookie', []), [])


class RefusedClientTests(AllowFromCase):
    WS_HEADERS = {'Upgrade': 'websocket', 'Connection': 'Upgrade', 'Sec-WebSocket-Version': '13',
                  'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ=='}

    async def test_a_client_outside_the_list_gets_a_bare_403_on_every_route(self):
        await self.serve(allow_from=nets('10.0.0.0/8', '::1'))
        cases = [('GET', '/login', {}, None), ('POST', '/login', {}, {'login_token': 'x', 'password': PASSWORD}),
                 ('GET', '/static/app.css', {}, None), ('GET', '/', {}, None), ('GET', '/settings', {}, None),
                 ('POST', '/api/v1/requests', {'Authorization': 'Bearer wbk_x', 'Content-Type': 'application/json'},
                  b'{}'),
                 ('GET', '/api/v1/projects', {'Authorization': 'Bearer wbk_x'}, None),
                 ('GET', '/api/v1/no/such/endpoint', {'Authorization': 'Bearer wbk_x'}, None),
                 ('GET', '/ws/board', self.WS_HEADERS, None), ('GET', '/no/such/page', {}, None),
                 ('GET', '/login', {'Host': 'evil.example'}, None),
                 ('GET', '/login', {'Cookie': f'{middleware.SESSION_COOKIE}=abc'}, None)]
        for method, path, headers, data in cases:
            with self.subTest(method=method, path=path, headers=headers):
                self.assert_refused(await self.call(method, path, headers=headers, data=data))
        self.assertEqual(self.api.calls, 0, 'no /api/v1/ handler ran')
        self.assertEqual(self.verified, 0, 'no session lookup')
        self.assertEqual(self.entered, 0, 'no login throttle')

    async def test_x_forwarded_for_and_similar_headers_are_never_trusted(self):
        await self.serve(allow_from=nets('10.0.0.0/8'))
        for header in ('X-Forwarded-For', 'X-Real-IP', 'Forwarded'):
            value = 'for=10.1.2.3' if header == 'Forwarded' else '10.1.2.3'
            with self.subTest(header=header):
                self.assert_refused(await self.call('GET', '/login', headers={header: value}))


class AllowedClientTests(AllowFromCase):
    async def test_a_listed_client_is_served_as_without_allow_from(self):
        await self.serve(allow_from=nets('127.0.0.1'))
        response = await self.call('GET', '/login')
        self.assertEqual(response.status, 200)
        self.assertTrue(any(value.startswith('wb_hub_login=') for value in response.headers.getall('Set-Cookie')))
        self.assertEqual((await self.call('GET', '/login', headers={'Host': 'evil.example'})).status, 421)

    async def test_a_listed_client_reaches_the_rest_handlers(self):
        # the positive control of the refused client's ``api.calls == 0``: a listed client does reach a handler
        await self.serve(allow_from=nets('127.0.0.1'))
        response = await self.call('GET', '/api/v1/projects', headers={'Authorization': 'Bearer wbk_x'})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.api.calls, 1)

    async def test_no_allow_from_serves_everyone(self):
        await self.serve(allow_from=())
        self.assertEqual((await self.call('GET', '/login')).status, 200)


class PeerAddressTests(AllowFromCase):
    """A listener on 127.0.0.2 reached from source 127.0.0.2: the list decides on that peer, not on the bind."""

    async def test_the_peer_127_0_0_2_is_allowed_when_listed(self):
        await self.serve(bind='127.0.0.2', allow_from=nets('127.0.0.2/32'), source='127.0.0.2')
        self.assertEqual((await self.call('GET', '/login')).status, 200)

    async def test_the_peer_127_0_0_2_is_refused_when_only_127_0_0_1_is_listed(self):
        await self.serve(bind='127.0.0.2', allow_from=nets('127.0.0.1'), source='127.0.0.2')
        self.assert_refused(await self.call('GET', '/login'))

    async def test_the_source_address_decides_on_the_same_listener(self):
        await self.serve(bind='127.0.0.2', allow_from=nets('127.0.0.1'), source='127.0.0.1')
        self.assertEqual((await self.call('GET', '/login')).status, 200)


class PeerMatchUnitTests(unittest.TestCase):
    def test_ipv4_mapped_peers_match_their_ipv4_form(self):
        self.assertTrue(middleware.peer_allowed(nets('127.0.0.1'), '::ffff:127.0.0.1'))
        self.assertTrue(middleware.peer_allowed(nets('100.64.0.0/10'), '::ffff:100.100.1.2'))
        self.assertFalse(middleware.peer_allowed(nets('10.0.0.0/8'), '::ffff:127.0.0.1'))

    def test_ipv6_and_unknown_peers(self):
        self.assertTrue(middleware.peer_allowed(nets('::1'), '::1'))
        self.assertFalse(middleware.peer_allowed(nets('127.0.0.1'), '::1'))
        for unknown in (None, '', 'garbage', 'fe80::1%eth0'):
            with self.subTest(peer=unknown):
                self.assertFalse(middleware.peer_allowed(nets('127.0.0.1', '::/0', '0.0.0.0/0'), unknown))

    def test_an_empty_list_allows_every_peer_even_an_unknown_one(self):
        self.assertTrue(middleware.peer_allowed((), None))
        self.assertTrue(middleware.peer_allowed((), '203.0.113.9'))


if __name__ == '__main__':
    unittest.main()
