"""2026-10-27 사용자 요구: a stale form explains itself instead of a bare ``csrf_failed`` text.

- ``POST /login`` whose double-submit token does not match (cookie absent or another value): the login page again,
  403, a fresh token and cookie and a short Korean line. The password is never checked and nothing counts as a
  login failure (no throttle row, no audit row).
- A logged-in page form POST with a wrong session CSRF: a small Korean 403 page with a link back (same-origin
  ``Referer`` path, else ``/``); the reason code stays in it. API routes keep the JSON ``csrf_failed``.
- Both write a rate-limited hub.log line naming the peer and the cause, never a token value.

Seam T2: ``create_app(services)`` with aiohttp's ``TestServer`` (``WebAppCase`` of ``test_hub_web_auth``).
Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_hub_web_auth import PASSWORD, WebAppCase, set_cookie  # noqa: E402
from workbench.hub.web.pages.login import LOGIN_EXPIRED  # noqa: E402
from workbench.hub.web.refusals import RefusalLog  # noqa: E402

STALE_TEXT = '화면이 오래되었습니다. 새로 고친 뒤 다시 시도하세요'


class CsrfCase(WebAppCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.lines: list[str] = []
        self.services.refusals = RefusalLog(write=self.lines.append)

    def failures(self) -> int:
        return len(self.rows('SELECT key FROM login_state'))


class StaleLoginFormTests(CsrfCase):
    async def assert_expired_page(self, response, sent: set[str]) -> None:
        self.assertEqual(response.status, 403, response.body_text)
        self.assertIn(LOGIN_EXPIRED, response.body_text)
        self.assertNotIn('csrf_failed', response.body_text)  # no bare code page
        field = re.search(r'name="login_token" value="([^"]+)"', response.body_text).group(1)
        cookie = set_cookie(response, 'wb_hub_login')
        self.assertIsNotNone(cookie, 'a fresh login cookie')
        self.assertEqual(cookie['value'], field)
        self.assertNotIn(field, sent, 'the token is new')
        self.assertIsNone(set_cookie(response, 'wb_hub_session'))
        self.assertEqual(self.failures(), 0, 'not a counted login failure')
        self.assertEqual([row for row in self.audit() if row[0].startswith('login')], [])

    async def test_no_login_cookie_gives_the_login_page_again_with_a_fresh_token(self):
        _cookie, field = await self.login_form()
        response = await self.post('/login', {'login_token': field, 'password': 'wrong'})
        await self.assert_expired_page(response, {field})
        self.assertEqual(len(self.lines), 1, self.lines)
        self.assertIn('refused login form from 127.0.0.1', self.lines[0])
        self.assertIn('login cookie absent', self.lines[0])
        self.assertNotIn(field, self.lines[0])

    async def test_a_mismatched_token_gives_the_same_page_and_never_checks_the_password(self):
        cookie, field = await self.login_form()
        other = field[:-1] + ('A' if field[-1] != 'A' else 'B')
        response = await self.post('/login', {'login_token': other, 'password': PASSWORD},
                                   cookies={'wb_hub_login': cookie})
        await self.assert_expired_page(response, {cookie, other})
        self.assertEqual(len(self.lines), 1, self.lines)
        self.assertIn('login token mismatched', self.lines[0])
        for secret in (cookie, other, PASSWORD):
            self.assertNotIn(secret, self.lines[0])

    async def test_the_fresh_form_then_logs_in(self):
        _cookie, field = await self.login_form()
        response = await self.post('/login', {'login_token': field, 'password': PASSWORD})
        fresh = set_cookie(response, 'wb_hub_login')['value']
        ok = await self.post('/login', {'login_token': fresh, 'password': PASSWORD}, cookies={'wb_hub_login': fresh})
        self.assertEqual(ok.status, 303)


class StaleSessionFormTests(CsrfCase):
    async def logged_in(self) -> tuple[str, str]:
        token = await self.session_cookie()
        home = await self.get('/dashboard', cookies={'wb_hub_session': token})
        return token, re.search(r'name="csrf" value="([^"]+)"', home.body_text).group(1)

    async def test_a_page_post_with_a_stale_csrf_gets_a_korean_page_with_a_link_back(self):
        token, csrf = await self.logged_in()
        response = await self.post('/settings/password', {'csrf': 'stale-' + csrf, 'current': 'x', 'new': 'y'},
                                   cookies={'wb_hub_session': token},
                                   headers={'Referer': f'{self.origin}/settings?tab=1'})
        self.assertEqual(response.status, 403)
        self.assertIn(STALE_TEXT, response.body_text)
        self.assertIn('href="/settings?tab=1"', response.body_text)
        self.assertIn('csrf_failed', response.body_text)  # the reason code is still on the page
        self.assertEqual(response.headers.getall('Set-Cookie', []), [])
        self.assertEqual(len(self.lines), 1, self.lines)
        self.assertIn('refused form POST /settings/password from 127.0.0.1', self.lines[0])
        self.assertNotIn(csrf, self.lines[0])

    async def test_the_stale_page_of_a_live_session_shows_the_global_nav_and_a_working_logout(self):
        """2026-10-28 사용자 결정: with a valid session the stale-form page has the global nav, and its logout form
        carries this session's CSRF value (so Logout works from here)."""
        token, csrf = await self.logged_in()
        response = await self.post('/settings/password', {'csrf': 'stale-' + csrf, 'current': 'x', 'new': 'y'},
                                   cookies={'wb_hub_session': token})
        self.assertEqual(response.status, 403)
        text = response.body_text
        self.assertEqual(text.count('aria-label="주요"'), 1, text)
        self.assertIn('href="/dashboard"', text)
        self.assertRegex(text, r'<form[^>]*action="/logout"[^>]*>\s*<input[^>]*name="csrf" value="%s"' % re.escape(csrf))
        self.assertIn(STALE_TEXT, text)
        self.assertEqual(text.count('/static/session-check.js'), 1)
        logout = await self.post('/logout', {'csrf': csrf}, cookies={'wb_hub_session': token})
        self.assertEqual(logout.status, 303)

    async def test_without_a_same_origin_referer_the_link_goes_home(self):
        token, _csrf = await self.logged_in()
        for referer in (None, 'http://evil.example/settings', 'javascript:alert(1)'):
            with self.subTest(referer=referer):
                headers = {'Referer': referer} if referer else {}
                response = await self.post('/logout', {'csrf': 'nope'}, cookies={'wb_hub_session': token},
                                           headers=headers)  # Origin is ours: only the link differs
                self.assertEqual(response.status, 403)
                self.assertIn('href="/dashboard"', response.body_text)
                self.assertNotIn('evil.example', response.body_text)

    async def test_an_api_post_keeps_the_json_csrf_failed(self):
        token, _csrf = await self.logged_in()
        response = await self.post('/api/projects/00000000-0000-4000-8000-000000000000/pause', {'csrf': 'nope'},
                                   cookies={'wb_hub_session': token})
        self.assertEqual(response.status, 403)
        self.assertEqual(response.headers['Content-Type'].split(';')[0], 'application/json')
        self.assertIn('"csrf_failed"', response.body_text)


if __name__ == '__main__':
    unittest.main()
