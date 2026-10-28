"""WBX-A2 cache policy (seam S1, with S6 markup reads): the real Hub's web app in-process over ``TestServer``.

``create_app(services, routes=Hub(...).web_routes())`` with real ``auth.json`` / ``hub.sqlite3`` in a temp dir. The
expected ``Cache-Control`` values come from the web-board-ux SPEC table "Cache 정책과 bfcache" (WBX-D02, WBX-AC-02),
not from the implementation. Run with ``discover -s tests/hub -p 'test_web_cache*.py'``.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import unittest
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from aiohttp import ClientSession, DummyCookieJar, test_utils  # noqa: E402

from workbench.hub import auth, server  # noqa: E402
from workbench.hub.paths import HubLayout  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402

import test_inbox_results as results  # noqa: E402
import test_web_route_audit as audit  # noqa: E402

PASSWORD = 'correct horse battery'
# SPEC table rows.
SESSION_PAGE = 'private, no-cache'
NO_STORE = 'no-store'
STATIC = 'public, max-age=3600'
FAVICON = 'public, max-age=86400'
# SPEC exception rows: these resources answer ``no-store`` whatever their status (raw text, key material).
NO_STORE_GETS = ('/requests/{rid}/raw', '/requests/{rid}/full', '/requests/{rid}/artifact')


class Scripts(HTMLParser):
    """``(src, attrs)`` of every ``<script>`` element."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str | None, dict]] = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            attrs = dict(attrs)
            self.found.append((attrs.get('src'), attrs))

    def sources(self) -> list[str | None]:
        return [src for src, _ in self.found]


class CacheCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix='wb-cache-')).resolve()
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
        self.server = test_utils.TestServer(self.app, host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.http = ClientSession(cookie_jar=DummyCookieJar())
        self.addAsyncCleanup(self.http.close)
        self.host = f'127.0.0.1:{self.server.port}'
        self.url = f'http://{self.host}'
        self.session = self.services.sessions.create('127.0.0.1', 'cache', time.time(), generation=self.generation)

    def headers(self, session: bool, extra: dict | None = None) -> dict:
        headers = {'Host': self.host}
        if session:
            headers['Cookie'] = f'wb_hub_session={self.session.token}'
        headers.update(extra or {})
        return headers

    async def get(self, path: str, *, session: bool = True, method: str = 'GET', headers: dict | None = None):
        """``(status, headers, text)``; redirects are not followed."""
        async with self.http.request(method, self.url + path, headers=self.headers(session, headers),
                                     allow_redirects=False) as response:
            return response.status, response.headers, await response.text()

    async def post(self, path: str, data: dict, *, session: bool = True, csrf: bool = True,
                   headers: dict | None = None):
        if session and csrf:
            data = {'csrf': self.session.csrf, **data}
        extra = {'Origin': self.url, **(headers or {})}
        async with self.http.post(self.url + path, data=data, headers=self.headers(session, extra),
                                  allow_redirects=False) as response:
            return response.status, response.headers, await response.text()

    def assert_cache(self, headers, expected: str, what: str) -> None:
        self.assertEqual(headers.getall('Cache-Control', []), [expected], what)


class SessionPageTests(CacheCase):
    """Behavior 1: a logged-in page route's HTML response (200, 4xx page, stale form 403) is ``private, no-cache``."""

    async def test_every_get_page_route_html_response_is_private_no_cache(self):
        params = {**audit.PARAMS, 'rid': str(uuid.uuid4())}
        seen = {}
        for (method, route), access in sorted(audit.route_marks(self.app).items()):
            if method != 'GET' or access != audit.SESSION or route.startswith('/static') or route in NO_STORE_GETS:
                continue
            with self.subTest(route=route):
                status, headers, _ = await self.get(route.format(**params) + '?channel=agent')
                if not headers.get('Content-Type', '').startswith('text/html'):
                    self.assert_cache(headers, NO_STORE, f'{route} -> {status} {headers.get("Content-Type")}')
                    continue
                seen[route] = status
                self.assert_cache(headers, SESSION_PAGE, f'{route} -> {status}')
        self.assertGreaterEqual(len(seen), 12, seen)
        self.assertTrue(any(status == 200 for status in seen.values()), seen)
        self.assertTrue(any(400 <= status < 500 for status in seen.values()), seen)

    async def test_named_screens_200_and_a_404_page(self):
        for path in ('/dashboard', '/requests', '/requests/new', '/settings', '/settings/keys', '/settings/audit'):
            status, headers, _ = await self.get(path)
            self.assertEqual(status, 200, path)
            self.assert_cache(headers, SESSION_PAGE, path)
        status, headers, _ = await self.get(f'/requests/{uuid.uuid4()}')
        self.assertEqual((status, headers['Content-Type'].split(';')[0]), (404, 'text/html'))
        self.assert_cache(headers, SESSION_PAGE, '404 request page')

    async def test_head_of_a_page_follows_the_get_rule(self):
        status, headers, _ = await self.get('/requests', method='HEAD')
        self.assertEqual(status, 200)
        self.assert_cache(headers, SESSION_PAGE, 'HEAD /requests')

    async def test_a_post_that_renders_an_error_page_is_private_no_cache(self):
        status, headers, _ = await self.post('/settings/password', {'current': 'x', 'new': 'aaaaaaaaaaaa',
                                                                    'confirm': 'bbbbbbbbbbbb'})
        self.assertEqual((status, headers['Content-Type'].split(';')[0]), (400, 'text/html'))
        self.assert_cache(headers, SESSION_PAGE, 'POST /settings/password 400')

    async def test_the_stale_form_page_is_private_no_cache(self):
        for path in ('/settings/password', '/logout', '/requests'):
            status, headers, text = await self.post(path, {'csrf': 'old'}, csrf=False)
            self.assertEqual((status, headers['Content-Type'].split(';')[0]), (403, 'text/html'), path)
            self.assertIn('csrf_failed', text)
            self.assert_cache(headers, SESSION_PAGE, f'stale {path}')


RAW_KEY = re.compile(r'wbk_[a-z2-7]{10}_[A-Za-z0-9_-]{43}')
KEY_FORM = {'name': 'cache-key', 'grade': 'requester', 'scope': 'all', 'expires_days': ''}


class KeyIssueTests(CacheCase):
    """Behavior 2: every response of ``POST /settings/keys`` (the raw key page, a refusal page) is ``no-store``."""

    async def test_the_raw_key_page_is_no_store(self):
        status, headers, text = await self.post('/settings/keys', KEY_FORM)
        self.assertEqual(status, 200, text[:300])
        self.assertEqual(len(RAW_KEY.findall(text)), 1, 'the page shows the raw key')
        self.assert_cache(headers, NO_STORE, 'POST /settings/keys 200')

    async def test_an_issue_refusal_page_is_no_store(self):
        await self.post('/settings/keys', KEY_FORM)
        for form in (KEY_FORM, {**KEY_FORM, 'name': ''}, {**KEY_FORM, 'name': 'other', 'grade': 'nope'}):
            status, headers, text = await self.post('/settings/keys', form)
            self.assertEqual((status, headers['Content-Type'].split(';')[0]), (400, 'text/html'), form)
            self.assertEqual(RAW_KEY.findall(text), [])
            self.assert_cache(headers, NO_STORE, f'POST /settings/keys 400 {form}')

    async def test_a_stale_form_on_the_issue_route_is_no_store(self):
        status, headers, _ = await self.post('/settings/keys', {**KEY_FORM, 'csrf': 'old'}, csrf=False)
        self.assertEqual(status, 403)
        self.assert_cache(headers, NO_STORE, 'stale POST /settings/keys')

    async def test_the_key_list_page_itself_is_a_normal_session_page(self):
        await self.post('/settings/keys', KEY_FORM)
        status, headers, text = await self.get('/settings/keys')
        self.assertEqual(status, 200)
        self.assertEqual(RAW_KEY.findall(text), [], 'the list never shows a raw key')
        self.assert_cache(headers, SESSION_PAGE, 'GET /settings/keys')



class ResultTextUnknownRequestTests(CacheCase):
    """Behavior 3 (error side): ``/raw``, ``/full``, ``/artifact`` stay ``no-store`` also for their error pages."""

    async def test_unknown_request_answers_are_no_store(self):
        rid = uuid.uuid4()
        for path in (f'/requests/{rid}/full', f'/requests/{rid}/raw?run_id=r', f'/requests/{rid}/artifact?path=a'):
            status, headers, _ = await self.get(path)
            self.assertGreaterEqual(status, 400, path)
            self.assert_cache(headers, NO_STORE, f'{path} -> {status} {headers.get("Content-Type")}')

    async def test_head_of_the_full_page_is_no_store(self):
        status, headers, _ = await self.get(f'/requests/{uuid.uuid4()}/full', method='HEAD')
        self.assert_cache(headers, NO_STORE, f'HEAD /full -> {status}')


class ResultTextTests(results.WebResultCase):
    """Behavior 3 (200 side): the full view, a raw log page and an artifact page of a linked request."""

    async def test_full_raw_and_artifact_200_are_no_store(self):
        rid = self.linked()
        self.backend.logs[('run', results.RUN)] = b'log line\n'
        (self.root_a / 'report.txt').write_bytes(b'report\n')
        for path in (f'/requests/{rid}/full', f'/requests/{rid}/raw?run_id={results.RUN}',
                     f'/requests/{rid}/artifact?path=report.txt'):
            status, headers, body = await self.raw_get(path)
            self.assertEqual(status, 200, (path, body[:200]))
            self.assertEqual(headers.getall('Cache-Control', []), [NO_STORE], path)



class NoStoreKeptTests(CacheCase):
    """Behavior 4: login, logout, redirects, JSON/REST/WebSocket and bare refusals stay ``no-store``; nothing without a
    verified session (or on a public route) gets the private page rule."""

    async def login_form(self) -> tuple[str, str]:
        status, headers, text = await self.get('/login', session=False)
        cookie = next(value.split(';')[0].split('=', 1)[1] for value in headers.getall('Set-Cookie')
                      if value.startswith('wb_hub_login='))
        return cookie, re.search(r'name="login_token" value="([^"]+)"', text).group(1)

    async def test_login_screens_are_no_store(self):
        status, headers, _ = await self.get('/login', session=False)
        self.assertEqual(status, 200)
        self.assert_cache(headers, NO_STORE, 'GET /login')
        cookie, token = await self.login_form()
        for session in (False, True):  # a session cookie on the public route changes nothing
            status, headers, _ = await self.post('/login', {'login_token': token, 'password': 'wrong password'},
                                                 session=session, csrf=False,
                                                 headers={'Cookie': f'wb_hub_login={cookie}' + (
                                                     f'; wb_hub_session={self.session.token}' if session else '')})
            self.assertEqual((status, headers['Content-Type'].split(';')[0]), (403, 'text/html'))
            self.assert_cache(headers, NO_STORE, f'POST /login wrong password session={session}')
        status, headers, _ = await self.post('/login', {'login_token': 'x' * 43, 'password': 'p'}, session=False)
        self.assertEqual(status, 403)
        self.assert_cache(headers, NO_STORE, 'POST /login stale token')

    async def test_redirects_are_no_store(self):
        cases = {'GET /login with a session': await self.get('/login'),
                 'page without a session': await self.get('/requests', session=False),
                 'page with a dead session': await self.get('/requests', session=False,
                                                            headers={'Cookie': 'wb_hub_session=dead'})}
        status, headers, text = await self.post('/logout', {})
        cases['POST /logout'] = (status, headers, text)
        for what, (status, headers, _) in cases.items():
            self.assertEqual(status, 303, what)
            self.assert_cache(headers, NO_STORE, what)
        status, headers, _ = await self.get('/requests')
        self.assertEqual(status, 303, 'logged out')

    async def test_json_rest_and_websocket_answers_are_no_store(self):
        raw = self.services.api_keys.issue('cache-rest', 'requester', {'all': True}, None, now=time.time()).raw
        bearer = {'Authorization': f'Bearer {raw}'}
        cases = {
            'badge 200': (await self.get('/api/badge'), 200),
            'badge 401': (await self.get('/api/badge', session=False), 401),
            'control api 403 csrf': (await self.post(f'/api/projects/{uuid.uuid4()}/power-on', {}, csrf=False), 403),
            'rest 200': (await self.get('/api/v1/projects', session=False, headers=bearer), 200),
            'rest 200 with a session cookie': (await self.get('/api/v1/projects', headers=bearer), 200),
            'rest 401 with only a session cookie': (await self.get('/api/v1/projects'), 401),
            'rest 404': (await self.get('/api/v1/nope', headers=bearer), 404),
            'ws without upgrade': (await self.get('/ws/board'), None),
        }
        for what, ((status, headers, _), expected) in cases.items():
            if expected is not None:
                self.assertEqual(status, expected, what)
            self.assert_cache(headers, NO_STORE, f'{what} -> {status} {headers.get("Content-Type")}')

    async def test_bare_refusals_are_no_store(self):
        cases = {
            '421': await self.get('/requests', headers={'Host': 'evil.example'}),
            '403 origin': await self.post('/requests', {}, headers={'Origin': 'http://evil.example'}),
            '413': await self.post('/requests', {'body': 'x' * (300 * 1024)}),
            '404 unknown path': await self.get('/nope'),
            '405': await self.get('/settings/password'),
        }
        for what, (status, headers, _) in cases.items():
            self.assertFalse(headers.get('Content-Type', '').startswith('text/html'), (what, status))
            self.assert_cache(headers, NO_STORE, f'{what} -> {status}')



class StaticKeptTests(CacheCase):
    """Behavior 5: ``/static/…`` 200 is ``public, max-age=3600``, ``/favicon.ico`` 204 ``public, max-age=86400``, with
    or without a session; a missing static file is ``no-store``."""

    async def test_static_and_favicon(self):
        for session in (False, True):
            for path in ('/static/base.css', '/static/badge.js'):
                status, headers, _ = await self.get(path, session=session)
                self.assertEqual(status, 200, path)
                self.assert_cache(headers, STATIC, f'{path} session={session}')
            status, headers, _ = await self.get('/favicon.ico', session=session)
            self.assertEqual(status, 204)
            self.assert_cache(headers, FAVICON, f'/favicon.ico session={session}')
            status, headers, _ = await self.get('/static/missing.js', session=session)
            self.assertEqual(status, 404)
            self.assert_cache(headers, NO_STORE, f'/static/missing.js session={session}')



SESSION_CHECK = '/static/session-check.js'
LOGIN_RESTORE = '/static/login-restore.js'


class RestoreScriptTests(CacheCase):
    """Behavior 6: every logged-in screen loads ``session-check.js`` (synchronous in ``<head>`` since WBX-A7, see
    :class:`HiddenUntilCheckedTests`); the login screen loads ``login-restore.js`` (defer) instead. Both are served as
    JavaScript."""

    async def test_every_logged_in_html_screen_loads_session_check_and_not_login_restore(self):
        params = {**audit.PARAMS, 'rid': str(uuid.uuid4())}
        seen = 0
        for (method, route), access in sorted(audit.route_marks(self.app).items()):
            if method != 'GET' or access != audit.SESSION or route.startswith('/static'):
                continue
            status, headers, text = await self.get(route.format(**params) + '?channel=agent')
            if not headers.get('Content-Type', '').startswith('text/html'):
                continue
            with self.subTest(route=route, status=status):
                seen += 1
                scripts = Scripts(text)
                found = [attrs for src, attrs in scripts.found if src == SESSION_CHECK]
                self.assertEqual(len(found), 1, scripts.sources())
                self.assertNotIn('defer', found[0])  # WBX-A7: synchronous, so it can hide before first paint
                self.assertNotIn(LOGIN_RESTORE, scripts.sources())
        self.assertGreaterEqual(seen, 14)

    async def test_post_result_pages_and_the_stale_form_page_load_session_check(self):
        pages = {'key issued': await self.post('/settings/keys', KEY_FORM),
                 'password 400': await self.post('/settings/password', {'new': 'a', 'confirm': 'b'}),
                 'stale form': await self.post('/requests', {'csrf': 'old'}, csrf=False)}
        for what, (status, headers, text) in pages.items():
            self.assertTrue(headers['Content-Type'].startswith('text/html'), what)
            self.assertEqual(Scripts(text).sources().count(SESSION_CHECK), 1, (what, status))

    async def test_the_login_screen_loads_login_restore_only(self):
        status, headers, text = await self.get('/login', session=False)
        self.assertEqual(status, 200)
        scripts = Scripts(text)
        found = [attrs for src, attrs in scripts.found if src == LOGIN_RESTORE]
        self.assertEqual(len(found), 1, scripts.sources())
        self.assertIn('defer', found[0])
        self.assertNotIn(SESSION_CHECK, scripts.sources())
        status, _, text = await self.post('/login', {'login_token': 'x' * 43, 'password': 'p'}, session=False)
        self.assertEqual(status, 403)
        self.assertEqual(Scripts(text).sources(), [LOGIN_RESTORE], 'the refused login screen too')

    async def test_both_scripts_are_served_as_javascript(self):
        for path in (SESSION_CHECK, LOGIN_RESTORE):
            status, headers, text = await self.get(path, session=False)
            self.assertEqual(status, 200, path)
            self.assertIn('javascript', headers['Content-Type'], path)
            self.assertIn('pageshow', text, path)



class HeadOrder(HTMLParser):
    """The start tags in document order with the section (``head``/``body``) each sits in, and ``<html>``'s attrs."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, str, dict]] = []
        self.html: dict = {}
        self.section = 'none'
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'html':
            self.html = attrs
        if tag in ('head', 'body'):
            self.section = tag
        self.tags.append((tag, self.section, attrs))

    def index(self, predicate) -> int | None:
        return next((i for i, (tag, _, attrs) in enumerate(self.tags) if predicate(tag, attrs)), None)


class HiddenUntilCheckedTests(CacheCase):
    """WBX-A7 (2026-10-28 사용자 결정, Q39 a): a logged-in screen shown again by Back/Forward is hidden until the
    session check answers. ``session-check.js`` is a synchronous script in ``<head>`` before the stylesheets (so its
    ``<html>`` class is set before first paint); base.css hides ``<body>`` under ``html.wb-checking``. The server never
    sends the class (a normal load and a browser without JavaScript show the page); the login screen does not load it."""

    def assert_head_script(self, text: str, where):
        doc = HeadOrder(text)
        found = [(i, section, attrs) for i, (tag, section, attrs) in enumerate(doc.tags)
                 if tag == 'script' and attrs.get('src') == SESSION_CHECK]
        self.assertEqual(len(found), 1, where)
        index, section, attrs = found[0]
        self.assertEqual(section, 'head', where)
        self.assertTrue({'defer', 'async', 'type'}.isdisjoint(attrs), (where, attrs))
        first_css = doc.index(lambda tag, a: tag == 'link' and a.get('rel') == 'stylesheet')
        self.assertIsNotNone(first_css, where)
        self.assertLess(index, first_css, f'{where}: session-check before the stylesheets')
        self.assertNotIn('wb-checking', doc.html.get('class', ''), where)

    async def test_every_logged_in_html_screen_loads_session_check_synchronously_in_head(self):
        params = {**audit.PARAMS, 'rid': str(uuid.uuid4())}
        seen = 0
        for (method, route), access in sorted(audit.route_marks(self.app).items()):
            if method != 'GET' or access != audit.SESSION or route.startswith('/static'):
                continue
            status, headers, text = await self.get(route.format(**params) + '?channel=agent')
            if not headers.get('Content-Type', '').startswith('text/html'):
                continue
            with self.subTest(route=route, status=status):
                seen += 1
                self.assert_head_script(text, route)
        self.assertGreaterEqual(seen, 14)

    async def test_post_result_pages_and_the_stale_form_page_load_it_synchronously_in_head(self):
        pages = {'key issued': await self.post('/settings/keys', KEY_FORM),
                 'password 400': await self.post('/settings/password', {'new': 'a', 'confirm': 'b'}),
                 'stale form': await self.post('/requests', {'csrf': 'old'}, csrf=False)}
        for what, (status, headers, text) in pages.items():
            self.assert_head_script(text, (what, status))

    async def test_base_css_hides_the_body_while_checking(self):
        status, _, text = await self.get('/static/base.css', session=False)
        self.assertEqual(status, 200)
        rules = {}
        for selectors, block in re.findall(r'([^{}]+)\{([^}]*)\}', re.sub(r'/\*.*?\*/', '', text, flags=re.S)):
            for selector in selectors.split(','):
                rules.setdefault(' '.join(selector.split()), []).append(block)
        # The body and everything in it: a descendant with its own ``visibility: visible`` (board.css
        # ``.panel.active``) must not show through, hence ``!important`` on the descendants too.
        for selector in ('html.wb-checking body', 'html.wb-checking body *'):
            self.assertIn(selector, rules, selector)
            self.assertTrue(any(re.search(r'visibility\s*:\s*hidden\s*!important', block) for block in rules[selector]),
                            (selector, rules[selector]))

    async def test_the_login_screen_is_untouched(self):
        for status_wanted, (status, _, text) in ((200, await self.get('/login', session=False)),
                                                 (403, await self.post('/login', {'login_token': 'x' * 43,
                                                                                  'password': 'p'}, session=False))):
            self.assertEqual(status, status_wanted)
            doc = HeadOrder(text)
            self.assertNotIn(SESSION_CHECK, [a.get('src') for tag, _, a in doc.tags if tag == 'script'])
            self.assertNotIn('wb-checking', text)


class RestoredFormTests(CacheCase):
    """WBX-AC-03 (server side): a form posted from a screen restored after logout or from another session changes
    nothing: ``303 /login`` without a live session, the stale form page (403) with another session's cookie."""

    def key_names(self) -> list[str]:
        return [info.name for info in self.services.api_keys.list() if not info.system]

    async def test_a_restored_form_after_logout_is_redirected_and_changes_nothing(self):
        old_csrf = self.session.csrf
        status, _, _ = await self.post('/logout', {})
        self.assertEqual(status, 303)
        status, headers, _ = await self.post('/settings/keys', {**KEY_FORM, 'csrf': old_csrf}, csrf=False)
        self.assertEqual((status, headers.get('Location')), (303, '/login'))
        self.assert_cache(headers, NO_STORE, 'restored POST after logout')
        self.assertEqual(self.key_names(), [])

    async def test_a_restored_form_under_a_new_session_is_the_stale_page_and_changes_nothing(self):
        old_csrf = self.session.csrf
        await self.post('/logout', {})
        self.session = self.services.sessions.create('127.0.0.1', 'cache', time.time(), generation=self.generation)
        status, _, text = await self.post('/settings/keys', {**KEY_FORM, 'csrf': old_csrf}, csrf=False)
        self.assertEqual(status, 403)
        self.assertIn('csrf_failed', text)
        self.assertEqual(self.key_names(), [])


if __name__ == '__main__':
    unittest.main()
