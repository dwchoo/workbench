"""2026-10-27 hotfix: a real browser's login failed with ``csrf_failed``.

Root cause: the browser's automatic ``GET /favicon.ico`` was an unknown page path, so it was redirected to
``/login``, and that ``GET /login`` issued a new ``wb_hub_login`` cookie that no longer matched the token in the open
form. Now ``/favicon.ico`` is a public route that never redirects or sets a cookie, and ``GET /login`` reuses a
well-formed login cookie instead of replacing it.

Seam T2: ``create_app(services)`` in-process with aiohttp's ``TestServer`` and a client with a real cookie jar
(the way a browser keeps cookies). Expected values: the SPEC login flow (303 with a session cookie, the
WRONG_PASSWORD page) and the user's report. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys
import tempfile
import unittest

from aiohttp import ClientSession, CookieJar, test_utils

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.hub import auth  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402
from workbench.hub.web.pages.login import WRONG_PASSWORD  # noqa: E402

PASSWORD = 'correct horse battery'


class BrowserLoginCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(prefix='wb-login-')
        self.addCleanup(temp.cleanup)
        state = Path(temp.name) / 'hub'
        passwords = auth.PasswordStore(state)
        passwords.set_password(PASSWORD)
        db = auth.HubDb.open(state)
        self.addCleanup(db.close)
        self.services = WebServices(passwords, db, OriginPolicy('127.0.0.1'))
        self.server = test_utils.TestServer(create_app(self.services), host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.base = f'http://127.0.0.1:{self.server.port}'
        self.browser = ClientSession(cookie_jar=CookieJar(unsafe=True))  # keeps cookies like a browser
        self.addAsyncCleanup(self.browser.close)

    async def get(self, path: str, *, follow: bool = True):
        async with self.browser.get(self.base + path, allow_redirects=follow) as response:
            response.body_text = await response.text()
            return response

    async def login_token(self) -> str:
        response = await self.get('/login')
        self.assertEqual(response.status, 200)
        return re.search(r'name="login_token" value="([^"]+)"', response.body_text).group(1)

    async def post_login(self, token: str, password: str):
        async with self.browser.post(self.base + '/login', data={'login_token': token, 'password': password},
                                     headers={'Origin': self.base}, allow_redirects=False) as response:
            response.body_text = await response.text()
            return response


class FaviconTests(BrowserLoginCase):
    async def test_favicon_is_public_never_redirects_and_sets_no_cookie(self):
        response = await self.get('/favicon.ico', follow=False)
        self.assertIn(response.status, (200, 204))
        self.assertNotIn('Location', response.headers)
        self.assertEqual(response.headers.getall('Set-Cookie', []), [])
        self.assertIn('max-age=', response.headers.get('Cache-Control', ''))

    async def test_static_assets_set_no_login_cookie(self):
        for path in ('/static/app.css', '/static/no-such-file.css'):
            with self.subTest(path=path):
                response = await self.get(path, follow=False)
                self.assertNotEqual(response.status, 303)
                self.assertEqual(response.headers.getall('Set-Cookie', []), [])


class StableLoginTokenTests(BrowserLoginCase):
    async def test_a_favicon_fetch_after_the_login_page_does_not_break_the_form(self):
        token = await self.login_token()
        await self.get('/favicon.ico')  # what every browser does right after the page
        response = await self.post_login(token, 'wrong password')
        self.assertEqual(response.status, 403)
        self.assertNotIn('csrf_failed', response.body_text)
        self.assertIn(WRONG_PASSWORD, response.body_text)

    async def test_a_second_login_page_keeps_the_first_forms_token_valid(self):
        first = await self.login_token()
        second = await self.login_token()  # another tab, a prefetch or a redirect
        self.assertEqual(second, first)
        response = await self.post_login(first, PASSWORD)
        self.assertEqual(response.status, 303, response.body_text)
        self.assertEqual(response.headers['Location'], '/dashboard')
        cookies = response.headers.getall('Set-Cookie', [])
        self.assertTrue(any(value.startswith('wb_hub_session=') for value in cookies), cookies)

    async def test_a_malformed_login_cookie_is_replaced_by_a_fresh_token(self):
        self.browser.cookie_jar.update_cookies({'wb_hub_login': 'short'})
        response = await self.get('/login')
        token = re.search(r'name="login_token" value="([^"]+)"', response.body_text).group(1)
        self.assertNotEqual(token, 'short')
        self.assertRegex(token, r'^[A-Za-z0-9_-]{43}$')
        self.assertTrue(any(value.startswith(f'wb_hub_login={token}')
                            for value in response.headers.getall('Set-Cookie', [])))


if __name__ == '__main__':
    unittest.main()
