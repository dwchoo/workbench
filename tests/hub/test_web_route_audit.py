"""Route and permission audit of the Hub web app (TI-19 security review fix 6, seam T2).

Every route of the real Hub's web app (``create_app(services, routes=Hub(...).web_routes())``) is pinned here with its
access mark: ``public`` (no session), ``own`` (the handler authenticates by itself: ``/mcp`` with an API key),
``api`` (session, 401 refusals) or ``session`` (session, ``303 /login``). A route added without a pin fails the audit,
and so does a changed mark. Every unsafe-method route needs the session's CSRF token; the only exemptions are
``POST /login`` (pre-session double-submit login token) and ``/mcp`` (Bearer API key, cookies ignored), and both are
checked by behaviour too. Expected values come from the SPEC "웹 route와 화면" and "Principal과 권한" tables.
Run with ``discover -s tests/hub -p 'test_web_route_audit.py'``.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from aiohttp import ClientSession, DummyCookieJar, test_utils, web  # noqa: E402

from workbench.hub import auth, server  # noqa: E402
from workbench.hub.paths import HubLayout  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402

PASSWORD = 'correct horse battery'
UNSAFE = frozenset({'POST', 'PUT', 'PATCH', 'DELETE', '*'})
PUBLIC, OWN, API, SESSION = 'public', 'own', 'api', 'session'

# (method, path) -> access. GET routes also answer HEAD with the same mark (aiohttp adds it).
EXPECTED = {
    ('GET', '/static'): PUBLIC,
    ('GET', '/login'): PUBLIC,
    ('POST', '/login'): PUBLIC,
    ('POST', '/logout'): SESSION,
    ('GET', '/'): SESSION,
    ('GET', '/settings'): SESSION,
    ('POST', '/settings/password'): SESSION,
    ('GET', '/settings/keys'): SESSION,
    ('POST', '/settings/keys'): SESSION,
    ('GET', '/settings/keys/{key_id}/revoke'): SESSION,
    ('POST', '/settings/keys/{key_id}/revoke'): SESSION,
    ('GET', '/settings/audit'): SESSION,
    ('POST', '/api/projects/{project_id}/{action}'): API,
    ('GET', '/projects/{project_id}'): SESSION,
    ('GET', '/projects/{project_id}/{action}'): SESSION,
    ('POST', '/projects/{project_id}/review-mode'): SESSION,
    ('POST', '/projects/{project_id}/{action}'): SESSION,
    ('GET', '/requests'): SESSION,
    ('GET', '/api/badge'): API,
    ('GET', '/requests/new'): SESSION,
    ('POST', '/requests'): SESSION,
    ('GET', '/requests/{rid}'): SESSION,
    ('GET', '/requests/{rid}/edit'): SESSION,
    ('POST', '/requests/{rid}/edit'): SESSION,
    ('POST', '/requests/{rid}/comments'): SESSION,
    ('POST', '/requests/{rid}/state'): SESSION,
    ('GET', '/requests/{rid}/cancel'): SESSION,
    ('POST', '/requests/{rid}/cancel'): SESSION,
    ('POST', '/requests/{rid}/start-review'): SESSION,
    ('GET', '/requests/{rid}/full'): SESSION,
    ('GET', '/requests/{rid}/raw'): SESSION,
    ('GET', '/requests/{rid}/artifact'): SESSION,
    ('*', '/mcp'): OWN,
    ('POST', '/board/restart'): SESSION,
    ('GET', '/ws/board'): API,
    ('GET', '/board'): SESSION,
    ('GET', '/board/restart'): SESSION,
}
# Unsafe routes that do not use the session CSRF token, and why (each is checked by behaviour below).
CSRF_EXEMPT = {('POST', '/login'): 'pre-session login token', ('*', '/mcp'): 'Bearer API key; cookies ignored'}
PARAMS = {'key_id': 'abcdefghij', 'project_id': str(uuid.uuid4()), 'action': 'power-on', 'rid': str(uuid.uuid4())}


def route_marks(app: web.Application) -> dict:
    """``{(method, path): access}`` of every route, the way ``middleware.access_of`` decides it."""
    found = {}
    for route in app.router.routes():
        info = route.resource.get_info()
        path = info.get('path') or info.get('formatter') or info.get('prefix')
        if path.startswith('/static'):
            access = PUBLIC  # the static prefix is public whatever the handler
        else:
            access = getattr(route.handler, 'wb_access', None) or (
                API if path.startswith(('/api/', '/ws/')) else SESSION)
        found[(route.method, path)] = access
    return found


def audit(app: web.Application) -> list[str]:
    """Problems: a route without a pin, a pinned route missing, a changed mark, an unsafe route without CSRF."""
    found = route_marks(app)
    expected = dict(EXPECTED)
    expected.update({('HEAD', path): access for (method, path), access in EXPECTED.items() if method == 'GET'})
    problems = [f'unpinned route {method} {path} ({access})' for (method, path), access in sorted(found.items())
                if (method, path) not in expected]
    problems += [f'missing route {method} {path}' for method, path in sorted(expected) if (method, path) not in found]
    problems += [f'changed mark {method} {path}: {found[(method, path)]} != {access}'
                 for (method, path), access in sorted(expected.items())
                 if (method, path) in found and found[(method, path)] != access]
    problems += [f'unsafe route without CSRF {method} {path} ({access})'
                 for (method, path), access in sorted(found.items())
                 if method in UNSAFE and access not in (API, SESSION) and (method, path) not in CSRF_EXEMPT]
    return problems


class RouteAuditCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix='wb-audit-')).resolve()
        self.addCleanup(shutil.rmtree, self.base_dir, True)
        state = self.base_dir / 'state'
        state.mkdir(mode=0o700)
        self.passwords = auth.PasswordStore(state)
        self.generation = self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(state)
        self.addCleanup(self.db.close)
        self.services = WebServices(self.passwords, self.db, OriginPolicy('127.0.0.1'))
        self.hub = server.Hub(HubLayout(self.base_dir / 'hub'), 'detached')
        self.addCleanup(self.hub.inbox.close)
        self.app = create_app(self.services, routes=self.hub.web_routes())


class StaticAuditTests(RouteAuditCase):
    def test_every_route_of_the_real_hub_is_pinned_with_its_access_mark(self):
        self.assertEqual(audit(self.app), [])

    def test_the_default_app_routes_are_a_pinned_subset(self):
        defaults = route_marks(create_app(self.services))
        for (method, path), access in defaults.items():
            with self.subTest(route=(method, path)):
                key = ('GET', path) if method == 'HEAD' else (method, path)
                self.assertEqual(EXPECTED.get(key), access)

    def test_a_new_unmarked_or_unsafe_public_route_fails_the_audit(self):
        from workbench.hub.web.middleware import public

        async def handler(request):
            return web.Response(text='x')

        async def open_handler(request):
            return web.Response(text='x')

        def extra(app):
            app.router.add_post('/new-thing', handler)
            app.router.add_post('/open-thing', public(open_handler))
        problems = audit(create_app(self.services, routes=[*self.hub.web_routes(), extra]))
        self.assertIn('unpinned route POST /new-thing (session)', problems)
        self.assertIn('unpinned route POST /open-thing (public)', problems)
        self.assertIn('unsafe route without CSRF POST /open-thing (public)', problems)


class BehaviourAuditTests(RouteAuditCase):
    """Each unsafe session/API route refuses a logged-in request without the CSRF token, before its handler."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.server = test_utils.TestServer(self.app, host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.http = ClientSession(cookie_jar=DummyCookieJar())
        self.addAsyncCleanup(self.http.close)
        self.host = f'127.0.0.1:{self.server.port}'
        self.url = f'http://{self.host}'
        self.session = self.services.sessions.create('127.0.0.1', 'audit', time.time(), generation=self.generation)

    async def send(self, method: str, path: str, *, cookie: bool = True, headers: dict | None = None,
                   data: dict | None = None) -> tuple[int, str]:
        sent = {'Host': self.host, 'Origin': self.url, **(headers or {})}
        if cookie:
            sent['Cookie'] = f'wb_hub_session={self.session.token}'
        async with self.http.request(method, self.url + path, headers=sent, data=data or {},
                                     allow_redirects=False) as response:
            return response.status, await response.text()

    async def test_unsafe_session_routes_need_the_csrf_token(self):
        checked = 0
        for (method, path), access in sorted(route_marks(self.app).items()):
            if method not in UNSAFE or (method, path) in CSRF_EXEMPT:
                continue
            concrete = path.format(**PARAMS)
            with self.subTest(route=(method, path)):
                status, text = await self.send(method, concrete)
                self.assertEqual(status, 403, text)
                self.assertIn('csrf_failed', text)
                status, text = await self.send(method, concrete, headers={'X-CSRF-Token': 'not-the-token'})
                self.assertEqual(status, 403, text)
                status, _text = await self.send(method, concrete, cookie=False)
                self.assertEqual(status, 401 if access == API else 303)
                checked += 1
        self.assertEqual(checked, 14)  # every unsafe route of EXPECTED but the two exemptions

    async def test_mcp_ignores_the_session_cookie(self):
        status, text = await self.send('POST', '/mcp', headers={'Content-Type': 'application/json',
                                                                'Accept': 'application/json'},
                                       data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
        self.assertEqual(status, 401, text)

    async def test_login_post_needs_its_own_login_token(self):
        status, text = await self.send('POST', '/login', cookie=False, data={'password': PASSWORD})
        self.assertNotEqual(status, 303, text)
        status, text = await self.send('POST', '/login', cookie=False, data={'password': PASSWORD,
                                                                             'login_token': 'forged'})
        self.assertNotEqual(status, 303, text)


if __name__ == '__main__':
    unittest.main()
