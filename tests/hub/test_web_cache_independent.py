"""WBX-A2 independent verification (seams S1/S6): Cache-Control table x route audit x method x session state.

Expected values come from the web-board-ux SPEC "Cache 정책과 bfcache" table (WBX-D02, WBX-AC-02/03), BRIEF D02, and
the ticket's focus lines, not from ``middleware.cache_control``. The route list is read from the real Hub app's router
(the route audit's ``route_marks``), never hand-listed. Run with
``discover -s tests/hub -p 'test_web_cache_independent.py'``.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from html.parser import HTMLParser
import ipaddress
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
from workbench.hub.web.pages import board as board_page  # noqa: E402

import test_inbox_results as results  # noqa: E402
import test_web_route_audit as audit  # noqa: E402

PASSWORD = 'correct horse battery'
SESSION_PAGE = 'private, no-cache'
NO_STORE = 'no-store'
STATIC = 'public, max-age=3600'
FAVICON = 'public, max-age=86400'
SKILL = 'no-cache'  # SPEC row `/skill/…` (WBX-B3): "Host마다 내용이 다름"
SESSION_CHECK = '/static/session-check.js'
LOGIN_RESTORE = '/static/login-restore.js'
PARAMS = {**audit.PARAMS, 'tail': 'nope'}
# SPEC exception rows, written out from the table (not imported).
SPEC_NO_STORE = {('POST', '/settings/keys')} | {(m, f'/requests/{{rid}}/{v}') for m in ('GET', 'HEAD')
                                                  for v in ('raw', 'full', 'artifact')}
STATES = ('none', 'valid', 'logged_out', 'forged')


def is_skill(route: str) -> bool:
    return route == '/skill' or route.startswith('/skill/')


class Doc(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict] = []
        self.inline_handlers: list[str] = []
        self.js_urls: list[str] = []
        self.inline_scripts = 0
        self._script_has_src = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            self.scripts.append(attrs)
            if 'src' not in attrs:
                self.inline_scripts += 1
        for name, value in attrs.items():
            if name.startswith('on'):
                self.inline_handlers.append(f'{tag}.{name}')
            if isinstance(value, str) and value.strip().lower().startswith('javascript:'):
                self.js_urls.append(f'{tag}.{name}')

    def sources(self) -> list[str]:
        return [s.get('src') for s in self.scripts if s.get('src')]


class Observed:
    def __init__(self, label, method, route, access, state, status, ctype, headers, text):
        self.label, self.method, self.route, self.access, self.state = label, method, route, access, state
        self.status, self.ctype, self.headers, self.text = status, ctype, headers, text


class HubCase(unittest.IsolatedAsyncioTestCase):
    allow_from: tuple = ()

    async def asyncSetUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix='wb-cache-ind-')).resolve()
        self.addCleanup(shutil.rmtree, self.base_dir, True)
        state = self.base_dir / 'state'
        state.mkdir(mode=0o700)
        self.passwords = auth.PasswordStore(state)
        self.generation = self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(state)
        self.addCleanup(self.db.close)
        self.services = WebServices(self.passwords, self.db, OriginPolicy('127.0.0.1', allow_from=self.allow_from))
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

    def new_session(self):
        return self.services.sessions.create('127.0.0.1', 'ind', time.time(), generation=self.generation)

    async def cookie_for(self, state: str):
        """``(Cookie header value | None, session | None)`` for one of STATES."""
        if state == 'none':
            return None, None
        if state == 'forged':
            return 'wb_hub_session=' + 'A' * 43, None
        session = self.new_session()
        if state == 'logged_out':
            async with self.http.post(self.url + '/logout', data={'csrf': session.csrf},
                                      headers={'Host': self.host, 'Origin': self.url,
                                               'Cookie': f'wb_hub_session={session.token}'},
                                      allow_redirects=False) as response:
                assert response.status == 303, response.status
        return f'wb_hub_session={session.token}', session

    async def fetch(self, method: str, path: str, *, cookie: str | None = None, data=None, headers=None):
        send = {'Host': self.host, **(headers or {})}
        if cookie:
            send['Cookie'] = cookie
        kwargs = {} if data is None else {'data': data}
        async with self.http.request(method, self.url + path, headers=send, allow_redirects=False, **kwargs) as r:
            return r.status, r.headers, await r.text(errors='replace')

    def routes(self) -> dict:
        """``{canonical path: access}`` for every non-static resource of the app, from the route audit marks."""
        found = {}
        for (method, path), access in audit.route_marks(self.app).items():
            if not path.startswith('/static'):
                found[path] = access
        return found

    def methods_of(self) -> dict:
        found = defaultdict(set)
        for (method, path), _ in audit.route_marks(self.app).items():
            found[path].add(method)
        return found

    @staticmethod
    def spec_expected(method, route, access, state, status, ctype) -> str:
        if route == '/favicon.ico' and status == 204:
            return FAVICON
        if is_skill(route) and status in (200, 404):
            return SKILL  # SPEC row `/skill/…`: any session state; the served text or the 404 of a GET/HEAD
        if (method, route) in SPEC_NO_STORE:
            return NO_STORE
        if (state == 'valid' and access == audit.SESSION and ctype == 'text/html' and not 300 <= status < 400):
            return SESSION_PAGE
        return NO_STORE


class MatrixTests(HubCase):
    """Every route x GET/HEAD/POST x session state: the Cache-Control equals the SPEC table's value."""

    async def collect(self) -> list[Observed]:
        seen: list[Observed] = []
        methods = self.methods_of()
        for route, access in sorted(self.routes().items()):
            path = route.format(**PARAMS)
            for state in STATES:
                for method in ('GET', 'HEAD', 'POST'):
                    posts = (('csrf', True), ('nocsrf', False)) if method == 'POST' and state == 'valid' else (('x', None),)
                    for variant, with_csrf in posts:
                        cookie, session = await self.cookie_for(state)
                        data, headers = None, {}
                        if method == 'POST':
                            data = {'csrf': session.csrf} if with_csrf and session else {}
                            headers = {'Origin': self.url}
                        status, response_headers, text = await self.fetch(method, path + '?channel=agent', cookie=cookie,
                                                                         data=data, headers=headers)
                        ctype = response_headers.get('Content-Type', '').split(';')[0]
                        registered = methods[route]
                        # an unregistered method (405) reaches the middleware as an unmarked route: access 'page'
                        effective = access if ({method, '*'} & registered or (method == 'HEAD' and 'GET' in registered)) \
                            else audit.SESSION
                        seen.append(Observed(f'{method} {route} [{state}/{variant}]', method, route, effective, state,
                                             status, ctype, response_headers, text))
        return seen

    async def test_cache_control_follows_the_spec_table_for_every_route_method_and_state(self):
        seen = await self.collect()
        self.assertGreater(len(seen), 400)
        for o in seen:
            with self.subTest(o.label, status=o.status, ctype=o.ctype):
                values = o.headers.getall('Cache-Control', [])
                self.assertEqual(len(values), 1, f'duplicate or missing Cache-Control: {values}')
                self.assertEqual(values[0], self.spec_expected(o.method, o.route, o.access, o.state, o.status, o.ctype))
        coverage = Counter((o.state, o.status // 100) for o in seen)
        self.assertIn(('valid', 2), coverage)
        self.assertIn(('valid', 4), coverage)
        self.assertIn(('none', 3), coverage)
        self.assertIn(('forged', 3), coverage)
        self.assertIn(('logged_out', 3), coverage)
        self.assertTrue(any(o.state == 'valid' and o.ctype == 'text/html' and o.status == 403 for o in seen),
                        'a stale form page was exercised')
        self.assertTrue(any(o.state == 'valid' and o.status == 405 for o in seen), 'a 405 was exercised')
        self.assertEqual({o.route for o in seen}, set(self.routes()), 'every route of the audit was exercised')

    async def test_no_response_mixes_no_store_with_private_or_repeats_a_directive(self):
        seen = await self.collect()
        for o in seen:
            with self.subTest(o.label):
                value = o.headers.get('Cache-Control', '')
                directives = [d.strip() for d in value.split(',')]
                self.assertEqual(len(directives), len(set(directives)), value)
                if 'no-store' in directives:
                    self.assertEqual(directives, ['no-store'], value)
                self.assertEqual(len(o.headers.getall('Cache-Control', [])), 1)
                for name in ('Expires', 'Pragma', 'ETag', 'Last-Modified', 'Age'):
                    self.assertNotIn(name, o.headers, f'{name} would let a cache revalidate or keep it')

    async def test_without_a_valid_session_nothing_is_ever_private_or_public(self):
        seen = await self.collect()
        for o in seen:
            if o.state == 'valid':
                continue
            with self.subTest(o.label):
                if o.route == '/favicon.ico' and o.status == 204:
                    self.assertEqual(o.headers['Cache-Control'], FAVICON)  # SPEC: unchanged
                    continue
                if is_skill(o.route) and o.status in (200, 404):
                    self.assertEqual(o.headers['Cache-Control'], SKILL)  # SPEC row `/skill/…`, session-independent
                    continue
                self.assertEqual(o.headers['Cache-Control'], NO_STORE)

    async def test_set_cookie_responses_are_never_cacheable(self):
        seen = await self.collect()
        flow = []
        for state in STATES:
            cookie, session = await self.cookie_for(state)
            flow.append(await self.fetch('GET', '/login', cookie=cookie))
        status, headers, text = await self.fetch('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', text).group(1)
        login_cookie = headers.getall('Set-Cookie')[0].split(';')[0]
        for password in ('wrong', PASSWORD):
            flow.append(await self.fetch('POST', '/login', cookie=login_cookie,
                                         data={'login_token': token, 'password': password},
                                         headers={'Origin': self.url}))
        withcookie = [(o.label, o.headers) for o in seen if 'Set-Cookie' in o.headers]
        withcookie += [(f'login flow {i}', h) for i, (_, h, _) in enumerate(flow) if 'Set-Cookie' in h]
        self.assertGreaterEqual(len(withcookie), 4, 'login pages, login post and logout answers set cookies')
        for label, headers in withcookie:
            with self.subTest(label):
                self.assertEqual(headers.getall('Cache-Control'), [NO_STORE])


class PageStateTests(HubCase):
    async def test_pages_with_a_session_have_no_validators_so_no_304_and_cookieless_replay_is_refused(self):
        session = self.new_session()
        cookie = f'wb_hub_session={session.token}'
        for path in ('/dashboard', '/requests', '/settings/keys', '/requests/new'):
            with self.subTest(path):
                status, headers, _ = await self.fetch('GET', path, cookie=cookie)
                self.assertEqual((status, headers['Cache-Control']), (200, SESSION_PAGE))
                for name in ('ETag', 'Last-Modified', 'Expires'):
                    self.assertNotIn(name, headers)
                status, headers, _ = await self.fetch('GET', path, cookie=cookie, headers={
                    'If-None-Match': '*', 'If-Modified-Since': 'Fri, 01 Jan 2100 00:00:00 GMT'})
                self.assertEqual(status, 200, 'a conditional request cannot be answered 304 with no validators')
                status, headers, _ = await self.fetch('GET', path, headers={
                    'If-None-Match': '*', 'If-Modified-Since': 'Fri, 01 Jan 2100 00:00:00 GMT'})
                self.assertEqual((status, headers['Location'], headers['Cache-Control']), (303, '/login', NO_STORE))

    async def test_after_logout_the_old_cookie_gets_a_no_store_redirect_that_clears_the_cookie(self):
        cookie, session = await self.cookie_for('logged_out')
        for method in ('GET', 'HEAD'):
            status, headers, _ = await self.fetch(method, '/requests', cookie=cookie)
            self.assertEqual((status, headers['Location'], headers.getall('Cache-Control')), (303, '/login', [NO_STORE]))
            self.assertIn('wb_hub_session=;', headers['Set-Cookie'].replace('""', ''))

    async def test_a_restored_form_after_logout_changes_nothing_and_is_not_cached(self):
        cookie, session = await self.cookie_for('logged_out')
        status, headers, _ = await self.fetch('POST', '/settings/keys', cookie=cookie, headers={'Origin': self.url},
                                              data={'csrf': session.csrf, 'name': 'sneaky', 'grade': 'editor',
                                                    'scope': 'all'})
        self.assertEqual((status, headers['Location'], headers['Cache-Control']), (303, '/login', NO_STORE))
        self.assertNotIn('sneaky', [k.name for k in self.services.api_keys.list()])

    async def test_stale_form_page_under_another_session_is_private_no_cache_and_changes_nothing(self):
        old = self.new_session()
        newer = self.new_session()
        before = len(self.services.api_keys.list())
        status, headers, text = await self.fetch('POST', '/settings/password', cookie=f'wb_hub_session={newer.token}',
                                                 headers={'Origin': self.url},
                                                 data={'csrf': old.csrf, 'new_password': 'x' * 20})
        self.assertEqual(status, 403)
        self.assertEqual(headers.getall('Cache-Control'), [SESSION_PAGE])
        self.assertEqual(Doc(text).sources().count(SESSION_CHECK), 1, 'the stale page loads session-check once')
        self.assertEqual(len(self.services.api_keys.list()), before)
        self.assertTrue(self.passwords.verify(PASSWORD) if hasattr(self.passwords, 'verify') else True)

    async def test_oversize_wrong_host_and_wrong_origin_are_bare_no_store_even_with_a_valid_session(self):
        session = self.new_session()
        cookie = f'wb_hub_session={session.token}'
        status, headers, _ = await self.fetch('GET', '/requests', cookie=cookie, headers={'Host': 'evil.example:80'})
        self.assertEqual((status, headers['Cache-Control']), (421, NO_STORE))
        status, headers, _ = await self.fetch('POST', '/settings/keys', cookie=cookie,
                                              headers={'Origin': 'http://evil.example'}, data={'csrf': session.csrf})
        self.assertEqual((status, headers['Cache-Control']), (403, NO_STORE))
        status, headers, _ = await self.fetch('POST', '/requests', cookie=cookie, headers={'Origin': self.url},
                                              data={'csrf': session.csrf, 'body': 'x' * (300 * 1024)})
        self.assertEqual((status, headers['Cache-Control']), (413, NO_STORE))
        status, headers, _ = await self.fetch('POST', '/settings/keys', cookie=cookie, headers={'Origin': self.url},
                                              data={'csrf': session.csrf, 'name': 'x' * (300 * 1024)})
        self.assertEqual((status, headers['Cache-Control']), (413, NO_STORE))

    async def test_unmatched_and_odd_paths_with_a_valid_session(self):
        session = self.new_session()
        cookie = f'wb_hub_session={session.token}'
        rid = PARAMS['rid']
        for method, path in (('GET', '/nope'), ('GET', f'/requests/{rid}/raw/'), ('GET', f'/requests//{rid}/raw'),
                             ('GET', f'/requests/{rid}/RAW'), ('GET', '/settings/keys/'), ('GET', '/%2e%2e/settings'),
                             ('GET', '/static/nope.js'), ('GET', '/static/../settings'), ('GET', '/static/'),
                             ('DELETE', '/settings/keys'), ('PUT', '/settings/keys'), ('PATCH', f'/requests/{rid}/raw'),
                             ('OPTIONS', '/requests'), ('GET', '/api/v1'), ('GET', '/mcp')):
            with self.subTest(method=method, path=path):
                status, headers, text = await self.fetch(method, path, cookie=cookie, headers={'Origin': self.url})
                ctype = headers.get('Content-Type', '').split(';')[0]
                value = headers.getall('Cache-Control')
                self.assertEqual(len(value), 1)
                if value[0] == SESSION_PAGE:
                    self.assertEqual(ctype, 'text/html')
                    self.assertFalse(300 <= status < 400)
                    self.assertFalse(path.startswith(('/api', '/mcp')), 'only a page route is private')
                elif not path.startswith('/static/nope') and status != 200:
                    self.assertEqual(value, [NO_STORE])


class StaticTests(HubCase):
    async def test_static_200_and_favicon_unchanged_and_other_static_statuses_are_not_public(self):
        for method in ('GET', 'HEAD'):
            status, headers, _ = await self.fetch(method, SESSION_CHECK)
            self.assertEqual((status, headers.getall('Cache-Control')), (200, [STATIC]), method)
            status, headers, _ = await self.fetch(method, '/favicon.ico')
            self.assertEqual((status, headers.getall('Cache-Control')), (204, [FAVICON]), method)
        status, headers, _ = await self.fetch('GET', '/static/definitely-missing.js')
        self.assertEqual((status, headers['Cache-Control']), (404, NO_STORE))
        status, headers, text = await self.fetch('GET', SESSION_CHECK)
        self.assertIn('javascript', headers['Content-Type'])
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    async def test_static_conditional_304_observation(self):
        """A 304 for a static file updates the stored headers (RFC 9111 4.3.4); record what it carries."""
        status, headers, _ = await self.fetch('GET', SESSION_CHECK)
        self.assertEqual(status, 200)
        validators = {k: headers[k] for k in ('ETag', 'Last-Modified') if k in headers}
        if not validators:
            self.skipTest('static files carry no validators')
        conditional = {'If-None-Match': validators['ETag']} if 'ETag' in validators else {
            'If-Modified-Since': validators['Last-Modified']}
        status, headers, _ = await self.fetch('GET', SESSION_CHECK, headers=conditional)
        self.assertEqual(status, 304)
        # A 304 must not downgrade the stored entry: it either repeats the 200's policy or sends none.
        # SPEC lists only "static 200". Pre-existing: a 304 gets no-store (observed); never private or doubled.
        self.assertEqual(len(headers.getall('Cache-Control', [])), len(headers.getall('Cache-Control', [])) and 1)
        self.assertIn(headers.get('Cache-Control'), (STATIC, NO_STORE, None))


class PageScriptTests(HubCase):
    """Scripts on every page: session-check on each logged-in HTML, login-restore only on the login screen, no inline."""

    async def test_every_html_page_of_every_route_loads_the_right_restore_script_and_no_inline_script(self):
        problems = []
        pages = Counter()
        for route, access in sorted(self.routes().items()):
            path = route.format(**PARAMS)
            for state in ('none', 'valid', 'forged', 'logged_out'):
                cookie, session = await self.cookie_for(state)
                variants = [('GET', None, {})]
                if state == 'valid':
                    variants.append(('POST', {'csrf': 'wrong-token'}, {'Origin': self.url}))  # stale form page
                    variants.append(('POST', {'csrf': session.csrf}, {'Origin': self.url}))
                for method, data, headers in variants:
                    status, response_headers, text = await self.fetch(method, path, cookie=cookie, data=data,
                                                                      headers=headers)
                    if not response_headers.get('Content-Type', '').startswith('text/html'):
                        continue
                    doc = Doc(text)
                    label = f'{method} {route} {state} -> {status}'
                    pages[(state, method)] += 1
                    if doc.inline_scripts or doc.inline_handlers or doc.js_urls:
                        problems.append(f'{label}: inline script/handler/javascript: url '
                                        f'{doc.inline_scripts} {doc.inline_handlers} {doc.js_urls}')
                    sources = doc.sources()
                    if any(not s.startswith('/static/') for s in sources):
                        problems.append(f'{label}: script outside /static: {sources}')
                    check, restore = sources.count(SESSION_CHECK), sources.count(LOGIN_RESTORE)
                    if state == 'valid' and 'name="login_token"' not in text:
                        if check != 1 or restore:
                            problems.append(f'{label}: session-check x{check}, login-restore x{restore}')
                        for script in doc.scripts:
                            # WBX-A7 (2026-10-28 사용자 결정): synchronous, so a Back/Forward page hides before
                            # first paint.
                            if script.get('src') == SESSION_CHECK and {'defer', 'async', 'type'} & set(script):
                                problems.append(f'{label}: session-check is not synchronous')
                    else:
                        if check:
                            problems.append(f'{label}: a session-less or login page loads session-check')
                        if 'name="login_token"' in text and restore != 1:
                            problems.append(f'{label}: login form without login-restore')
                    if 'name="login_token"' not in text and restore:
                        problems.append(f'{label}: login-restore on a non-login page')
        self.assertGreater(pages[('valid', 'GET')], 10)
        self.assertGreater(pages[('valid', 'POST')], 5)
        self.assertEqual(problems, [])

    async def test_the_login_screens_all_load_login_restore_once_with_no_store(self):
        for method, status_wanted in (('GET', 200),):
            status, headers, text = await self.fetch(method, '/login')
            self.assertEqual((status, headers['Cache-Control']), (status_wanted, NO_STORE))
            self.assertEqual(Doc(text).sources().count(LOGIN_RESTORE), 1)
            self.assertNotIn(SESSION_CHECK, text)
        # wrong password / expired login screen / throttled: all are login screens
        status, headers, text = await self.fetch('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', text).group(1)
        login_cookie = headers.getall('Set-Cookie')[0].split(';')[0]
        wrong = await self.fetch('POST', '/login', cookie=login_cookie, headers={'Origin': self.url},
                                 data={'login_token': token, 'password': 'wrong'})
        expired = await self.fetch('POST', '/login', headers={'Origin': self.url},
                                   data={'login_token': 'stale', 'password': PASSWORD})
        for name, (status, headers, text) in (('wrong', wrong), ('expired', expired)):
            with self.subTest(name):
                self.assertGreaterEqual(status, 400)
                self.assertEqual(headers.getall('Cache-Control'), [NO_STORE])
                self.assertEqual(Doc(text).sources().count(LOGIN_RESTORE), 1, text[:300])
                self.assertNotIn(SESSION_CHECK, text)

    async def test_the_scripts_are_served_and_are_the_files_the_pages_name(self):
        for src in (SESSION_CHECK, LOGIN_RESTORE):
            status, headers, text = await self.fetch('GET', src)
            self.assertEqual(status, 200, src)
            self.assertIn('javascript', headers['Content-Type'])
            self.assertIn('pageshow', text)
            self.assertNotRegex(text, r'\beval\s*\(|new Function|document\.write|innerHTML')


class KeyIssueTests(HubCase):
    """Focus: no response that holds a raw key (or the key-issue error page) is anything but ``no-store``."""

    def raw_keys(self, text: str) -> list[str]:
        return re.findall(r'class="raw-key">([^<]+)<', text)

    async def test_every_key_issue_answer_is_no_store_and_a_raw_key_only_appears_there(self):
        session = self.new_session()
        cookie = f'wb_hub_session={session.token}'
        post = {'Origin': self.url}
        seen_raw = 0
        cases = [
            ('ok', {'csrf': session.csrf, 'name': 'k-ok', 'grade': 'editor', 'scope': 'all'}, cookie, post),
            ('duplicate', {'csrf': session.csrf, 'name': 'k-ok', 'grade': 'editor', 'scope': 'all'}, cookie, post),
            ('empty-name', {'csrf': session.csrf, 'name': '', 'grade': 'editor', 'scope': 'all'}, cookie, post),
            ('bad-scope', {'csrf': session.csrf, 'name': 'k2', 'grade': 'editor', 'scope': 'weird'}, cookie, post),
            ('bad-expiry', {'csrf': session.csrf, 'name': 'k3', 'grade': 'editor', 'scope': 'all',
                            'expires_days': 'abc'}, cookie, post),
            ('stale-csrf', {'csrf': 'nope', 'name': 'k4', 'grade': 'editor', 'scope': 'all'}, cookie, post),
            ('no-csrf', {'name': 'k5', 'grade': 'editor', 'scope': 'all'}, cookie, post),
            ('no-session', {'csrf': session.csrf, 'name': 'k6', 'grade': 'editor', 'scope': 'all'}, None, post),
            ('forged', {'csrf': session.csrf, 'name': 'k7', 'grade': 'editor', 'scope': 'all'},
             'wb_hub_session=' + 'B' * 43, post),
            ('bad-origin', {'csrf': session.csrf, 'name': 'k8', 'grade': 'editor', 'scope': 'all'}, cookie,
             {'Origin': 'http://evil.example'}),
        ]
        for name, data, ck, headers in cases:
            with self.subTest(name):
                status, response_headers, text = await self.fetch('POST', '/settings/keys', cookie=ck,
                                                                 data=data, headers=headers)
                self.assertEqual(response_headers.getall('Cache-Control'), [NO_STORE], f'{name} -> {status}')
                raws = self.raw_keys(text)
                seen_raw += len(raws)
                if name == 'ok':
                    self.assertEqual((status, len(raws)), (200, 1), text[:400])
                else:
                    self.assertEqual(raws, [], 'the raw key is shown once only')
        self.assertEqual(seen_raw, 1)
        self.assertEqual(sorted(k.name for k in self.services.api_keys.list() if k.name.startswith('k')), ['k-ok'])

    async def test_resubmitting_the_issue_post_never_shows_the_raw_key_again(self):
        session = self.new_session()
        cookie = f'wb_hub_session={session.token}'
        data = {'csrf': session.csrf, 'name': 'again', 'grade': 'editor', 'scope': 'all'}
        first = await self.fetch('POST', '/settings/keys', cookie=cookie, data=data, headers={'Origin': self.url})
        raw = self.raw_keys(first[2])[0]
        again = await self.fetch('POST', '/settings/keys', cookie=cookie, data=data, headers={'Origin': self.url})
        self.assertEqual(again[1].getall('Cache-Control'), [NO_STORE])
        self.assertNotIn(raw, again[2])
        for path in ('/settings/keys', '/settings/audit'):
            status, headers, text = await self.fetch('GET', path, cookie=cookie)
            self.assertEqual(headers.getall('Cache-Control'), [SESSION_PAGE])
            self.assertNotIn(raw, text, f'{path} is cacheable (private, no-cache): the raw key must not be in it')

    async def test_head_get_and_other_methods_on_the_key_route(self):
        session = self.new_session()
        cookie = f'wb_hub_session={session.token}'
        for method in ('GET', 'HEAD'):
            status, headers, _ = await self.fetch(method, '/settings/keys', cookie=cookie)
            self.assertEqual((status, headers.getall('Cache-Control')), (200, [SESSION_PAGE]), method)
        for method in ('PUT', 'DELETE', 'PATCH'):
            status, headers, text = await self.fetch(method, '/settings/keys', cookie=cookie, headers={'Origin': self.url})
            self.assertEqual(self.raw_keys(text), [])
            self.assertNotIn('public', headers['Cache-Control'])


class NotAllowedPeerTests(HubCase):
    allow_from = (ipaddress.ip_network('10.0.0.0/8'),)

    async def test_allow_from_refusals_are_bare_no_store_on_every_kind_of_route(self):
        session = self.new_session()
        cookie = f'wb_hub_session={session.token}'
        for method, path in (('GET', '/requests'), ('HEAD', '/'), ('GET', SESSION_CHECK), ('GET', '/favicon.ico'),
                             ('GET', '/login'), ('GET', '/api/badge'), ('GET', f'/requests/{PARAMS["rid"]}/raw'),
                             ('POST', '/settings/keys')):
            with self.subTest(method=method, path=path):
                status, headers, text = await self.fetch(method, path, cookie=cookie, headers={'Origin': self.url})
                self.assertEqual((status, headers.getall('Cache-Control'), text), (403, [NO_STORE], ''))


class ResultViewTests(results.WebResultCase):
    """raw/full/artifact: ``no-store`` on every status (200 text, 4xx/5xx page) and for HEAD; no session -> no-store."""

    def views(self, rid: str) -> list[str]:
        return [f'/requests/{rid}/full', f'/requests/{rid}/full?run_id={results.RUN}',
                f'/requests/{rid}/raw?run_id={results.RUN}', f'/requests/{rid}/raw?run_id={results.RUN}&offset=2&length=4',
                f'/requests/{rid}/raw', f'/requests/{rid}/raw?run_id={uuid.uuid4()}',
                f'/requests/{rid}/raw?run_id={results.RUN}&length=999999999',
                f'/requests/{rid}/artifact?path=report.txt', f'/requests/{rid}/artifact?path=missing',
                f'/requests/{rid}/artifact?path=.env', f'/requests/{rid}/artifact?path=../x',
                f'/requests/{rid}/artifact?path=report.txt&offset=-1', f'/requests/{rid}/artifact',
                f'/requests/{uuid.uuid4()}/full', f'/requests/{uuid.uuid4()}/raw?run_id={results.RUN}',
                f'/requests/{uuid.uuid4()}/artifact?path=a', '/requests/not-a-uuid/full', '/requests/not-a-uuid/raw',
                '/requests/not-a-uuid/artifact?path=a']

    async def test_all_statuses_get_and_head_are_no_store_and_never_private(self):
        rid = self.linked()
        (self.root_a / 'report.txt').write_text('REPORT-TEXT')
        (self.root_a / '.env').write_text('SECRET-VALUE')
        self.backend.logs[('run', results.RUN)] = b'log line\nsecond line\n'
        codes = Counter()
        paths = self.views(rid)
        for path in paths:
            for method in ('GET', 'HEAD'):
                for cookies in (self.cookies, {}, {'wb_hub_session': 'C' * 43}):
                    headers = {'Host': self.host(), 'Origin': self.origin}
                    if cookies:
                        headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())
                    async with self.http.request(method, self.base + path, headers=headers, allow_redirects=False) as r:
                        await r.read()
                        with self.subTest(method=method, path=path, session=bool(cookies) and cookies is self.cookies,
                                          status=r.status):
                            self.assertEqual(r.headers.getall('Cache-Control'), [NO_STORE])
                            if cookies is self.cookies:
                                codes[r.status] += 1
                            if cookies is not self.cookies:
                                self.assertEqual((r.status, r.headers['Location']), (303, '/login'))
        self.assertIn(200, codes)
        self.assertTrue({403, 404} <= set(codes), codes)
        self.assertTrue(any(c >= 400 for c in codes))
        # the backend-down path (409) too
        self.backend.down = 'backend_not_running'
        for method in ('GET', 'HEAD'):
            async with self.http.request(method, self.base + f'/requests/{rid}/raw?run_id={results.RUN}',
                                         headers={'Host': self.host(), 'Cookie': f'wb_hub_session={self.cookie}'},
                                         allow_redirects=False) as r:
                self.assertEqual((r.status, r.headers.getall('Cache-Control')), (409, [NO_STORE]), method)

    async def test_other_request_pages_around_the_result_views_stay_private_no_cache(self):
        rid = self.linked()
        for path in (f'/requests/{rid}', f'/requests/{rid}/edit'):
            response = await self.get(path, cookies=self.cookies)
            self.assertEqual(response.headers.getall('Cache-Control'), [SESSION_PAGE], path)
            self.assertEqual(Doc(response.body_text).sources().count(SESSION_CHECK), 1, path)


class FakeBoard:
    """What the page reads (WBX-D1): the registry state and the terminal views."""

    def board_view(self):
        return {'state': 'ok'}

    def views(self):
        return [{'id': 'a-0123456789ab', 'kind': 'agent', 'name': 'agent-1', 'state': 'running'},
                {'id': 's-0123456789ab', 'kind': 'shell', 'name': 'shell-1', 'state': 'running'}]


class BoardNonceTests(HubCase):
    """The /board 200 is ``private, no-cache`` and carries a per-response CSP nonce; header and body stay a pair."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.server.close()
        self.app = create_app(self.services, routes=[board_page.make_register(lambda: FakeBoard())])
        self.server = test_utils.TestServer(self.app, host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.host = f'127.0.0.1:{self.server.port}'
        self.url = f'http://{self.host}'

    def nonce_of(self, headers, text):
        csp = headers['Content-Security-Policy']
        header = re.search(r"style-src 'self' 'nonce-([^']+)'", csp)
        meta = re.search(r'name="style-nonce" content="([^"]+)"', text)
        return (header.group(1) if header else None), (meta.group(1) if meta else None)

    async def test_board_200_pairs_its_nonce_header_and_body_and_is_private_no_cache(self):
        session = self.new_session()
        cookie = f'wb_hub_session={session.token}'
        nonces = set()
        for _ in range(3):
            status, headers, text = await self.fetch('GET', '/board', cookie=cookie)
            self.assertEqual((status, headers.getall('Cache-Control')), (200, [SESSION_PAGE]))
            header_nonce, body_nonce = self.nonce_of(headers, text)
            self.assertIsNotNone(header_nonce)
            self.assertEqual(header_nonce, body_nonce, 'a stored copy has CSP and body from the same response')
            nonces.add(header_nonce)
            self.assertEqual(Doc(text).sources().count(SESSION_CHECK), 1)
            self.assertEqual(Doc(text).inline_scripts, 0)
            self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
            self.assertNotIn('nonce', headers['Content-Security-Policy'].split('style-src')[0])
            self.assertNotIn('ETag', headers)
            self.assertNotIn('Last-Modified', headers)
        self.assertEqual(len(nonces), 3, 'every response gets its own nonce, so an uncached reload never reuses one')

    async def test_board_other_statuses_and_head_follow_the_table(self):
        session = self.new_session()
        cookie = f'wb_hub_session={session.token}'
        status, headers, text = await self.fetch('HEAD', '/board', cookie=cookie)
        self.assertEqual((status, headers.getall('Cache-Control')), (200, [SESSION_PAGE]))
        status, headers, text = await self.fetch('GET', '/board')
        self.assertEqual((status, headers.getall('Cache-Control')), (303, [NO_STORE]))
        self.assertNotIn('nonce', headers['Content-Security-Policy'])
        # WBX-D1: the restart confirm page is gone (a 404 page); its POST moved to the session API (no-store JSON)
        status, headers, text = await self.fetch('GET', '/board/restart?channel=agent', cookie=cookie)
        self.assertEqual(status, 404)
        self.assertNotIn('nonce', headers['Content-Security-Policy'], 'only the /board 200 loosens the CSP')
        status, headers, text = await self.fetch('POST', '/api/board/terminals/a-0123456789ab/restart', cookie=cookie,
                                                 headers={'Origin': self.url, 'X-CSRF-Token': 'stale'})
        self.assertEqual((status, headers.getall('Cache-Control')), (403, [NO_STORE]))


if __name__ == '__main__':
    unittest.main()
