"""TI-03 web app (seam T2): ``create_app(services)`` in-process with aiohttp ``TestServer``/``TestClient``.

Real ``auth.json``/``hub.sqlite3`` in a temp state dir, an injected clock. Expected values are the
SPEC strings (header values, cookie attributes, status codes, reasons) and rows the test reads
back with stdlib ``sqlite3``. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest

from aiohttp import ClientSession, DummyCookieJar, test_utils

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.hub import auth  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402

PASSWORD = 'correct horse battery'
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
COMMON_HEADERS = {'Content-Security-Policy': CSP, 'X-Content-Type-Options': 'nosniff',
                  'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'DENY'}


class WebAppCase(unittest.IsolatedAsyncioTestCase):
    bind = '127.0.0.1'
    tls = False
    server_names: tuple = ()
    insecure_warning = False
    routes: tuple = ()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-app-')
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'hub'
        self.passwords = auth.PasswordStore(self.state)
        self.generation = self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(self.state)
        self.addCleanup(self.db.close)
        self.now = [1_000_000.0]
        self.services = WebServices(self.passwords, self.db, OriginPolicy(self.bind, self.tls, self.server_names),
                                    insecure_warning=self.insecure_warning, clock=lambda: self.now[0])
        self.server = test_utils.TestServer(create_app(self.services, routes=self.routes), host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.port = self.server.port
        self.http = ClientSession(cookie_jar=DummyCookieJar())  # cookies are sent by hand
        self.addAsyncCleanup(self.http.close)
        self.base = f'http://127.0.0.1:{self.port}'

    def host(self, name: str | None = None) -> str:
        return f'{name or "127.0.0.1"}:{self.port}'

    @property
    def origin(self) -> str:
        return f'{"https" if self.tls else "http"}://{self.host()}'

    async def get(self, path: str, *, host: str | None = None, cookies: dict | None = None, **kwargs):
        headers = {'Host': host or self.host(), **kwargs.pop('headers', {})}
        if cookies:
            headers['Cookie'] = '; '.join(f'{name}={value}' for name, value in cookies.items())
        async with self.http.get(self.base + path, headers=headers, allow_redirects=False, **kwargs) as response:
            response.body_text = await response.text()
            return response

    async def post(self, path: str, data: dict | None = None, *, cookies: dict | None = None,
                   origin: str | None = '', headers: dict | None = None):
        headers = {'Host': self.host(), **(headers or {})}
        if origin is not None:
            headers['Origin'] = origin or self.origin
        if cookies:
            headers['Cookie'] = '; '.join(f'{name}={value}' for name, value in cookies.items())
        async with self.http.post(self.base + path, data=data or {}, headers=headers,
                                  allow_redirects=False) as response:
            response.body_text = await response.text()
            return response

    async def login_form(self) -> tuple[str, str]:
        """(``wb_hub_login`` cookie, hidden ``login_token``) from ``GET /login``."""
        response = await self.get('/login')
        cookie = set_cookie(response, 'wb_hub_login')
        field = re.search(r'name="login_token" value="([^"]+)"', response.body_text)
        self.assertIsNotNone(cookie)
        self.assertIsNotNone(field)
        return cookie['value'], field.group(1)

    async def login(self, password: str = PASSWORD):
        cookie, field = await self.login_form()
        return await self.post('/login', {'login_token': field, 'password': password},
                               cookies={'wb_hub_login': cookie})

    async def session_cookie(self) -> str:
        response = await self.login()
        self.assertEqual(response.status, 303, response.body_text)
        return set_cookie(response, 'wb_hub_session')['value']

    def rows(self, sql: str, args: tuple = ()) -> list:
        connection = sqlite3.connect(self.state / 'hub.sqlite3')
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def audit(self) -> list[tuple]:
        return [(event, actor, addr, json.loads(detail)) for event, actor, addr, detail in
                self.rows('SELECT event, actor, client_addr, detail_json FROM audit ORDER BY id')]


def set_cookie(response, name: str) -> dict | None:
    """The raw ``Set-Cookie`` for ``name``: ``value`` plus lower-cased attribute names."""
    for header in response.headers.getall('Set-Cookie', []):
        first, *attributes = [part.strip() for part in header.split(';')]
        key, _, value = first.partition('=')
        if key == name:
            parsed = {'value': value, 'raw': header}
            for attribute in attributes:
                attr, _, attr_value = attribute.partition('=')
                parsed[attr.lower()] = attr_value if attr_value else True
            return parsed
    return None


class HostCheckTests(WebAppCase):
    async def test_bind_ip_and_localhost_on_the_listening_port_are_accepted(self):
        for host in (self.host(), self.host('localhost'), self.host('LocalHost')):
            self.assertNotEqual((await self.get('/login', host=host)).status, 421, host)

    async def test_other_hosts_and_ports_are_misdirected(self):
        for host in (self.host('evil.example'), f'127.0.0.1:{self.port + 1}', '127.0.0.1', 'localhost',
                     self.host('127.0.0.2'), self.host('localhost.'), f'127.0.0.1:0{self.port}', self.host('[::1]'),
                     f'evil.example@127.0.0.1:{self.port}'):
            self.assertEqual((await self.get('/login', host=host)).status, 421, host)

    async def raw(self, request: bytes) -> bytes:
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        try:
            writer.write(request)
            await writer.drain()
            return await asyncio.wait_for(reader.read(65536), 5)
        finally:
            writer.close()

    async def test_missing_or_repeated_host_is_misdirected(self):
        self.assertRegex(await self.raw(b'GET /login HTTP/1.0\r\n\r\n'), rb'^HTTP/1\.[01] 421 ')
        twice = f'GET /login HTTP/1.1\r\nHost: {self.host()}\r\nHost: {self.host()}\r\nConnection: close\r\n\r\n'
        self.assertRegex(await self.raw(twice.encode()), rb'^HTTP/1\.[01] (400|421) ')  # the parser may refuse first


class ServerNameHostTests(WebAppCase):
    server_names = ('box.vpn',)

    async def test_server_names_are_accepted_case_insensitively(self):
        for host in (self.host('box.vpn'), self.host('BOX.vpn')):
            self.assertNotEqual((await self.get('/login', host=host)).status, 421, host)
        self.assertEqual((await self.get('/login', host=self.host('other.vpn'))).status, 421)


class AnyAddressHostTests(WebAppCase):
    bind = '0.0.0.0'

    async def test_any_ip_literal_and_server_names_but_not_localhost(self):
        for host in (self.host('10.8.0.2'), self.host('127.0.0.1'), self.host('[::1]'), self.host('[fd00::1]')):
            self.assertNotEqual((await self.get('/login', host=host)).status, 421, host)
        for host in (self.host('localhost'), self.host('evil.example'), '10.8.0.2'):
            self.assertEqual((await self.get('/login', host=host)).status, 421, host)


class Ipv6LoopbackHostTests(WebAppCase):
    bind = '::1'

    async def test_bracketed_literal_is_compared_as_an_address(self):
        for host in (self.host('[::1]'), self.host('[0:0::1]'), self.host('localhost')):
            self.assertNotEqual((await self.get('/login', host=host)).status, 421, host)
        for host in (self.host('::1'), self.host('127.0.0.1'), f'[::1]:{self.port + 1}'):
            self.assertEqual((await self.get('/login', host=host)).status, 421, host)


class SecurityHeaderTests(WebAppCase):
    def assert_common(self, response, what: str) -> None:
        for name, value in COMMON_HEADERS.items():
            self.assertEqual(response.headers.get(name), value, (what, name))

    async def test_every_response_carries_the_spec_headers(self):
        cases = {'login page': await self.get('/login'), 'misdirected': await self.get('/login', host='evil:1'),
                 'not found': await self.get('/no/such/page'), 'static': await self.get('/static/base.css')}
        for what, response in cases.items():
            self.assert_common(response, what)
        self.assertEqual(cases['login page'].status, 200)
        self.assertEqual(cases['misdirected'].status, 421)

    async def test_auth_pages_are_no_store_and_static_is_cacheable(self):
        self.assertEqual((await self.get('/login')).headers.get('Cache-Control'), 'no-store')
        static = await self.get('/static/base.css')
        self.assertEqual(static.status, 200)
        self.assertEqual(static.headers.get('Cache-Control'), 'public, max-age=3600')
        self.assertTrue(static.headers['Content-Type'].startswith('text/css'))

    async def test_layout_has_no_inline_script_or_style(self):
        async with self.http.get(self.base + '/login', headers={'Host': self.host()}) as response:
            body = await response.text()
        self.assertIn('<link rel="stylesheet" href="/static/base.css">', body)
        self.assertNotRegex(body, r'(?i)<script(?![^>]*\bsrc=)')
        self.assertNotRegex(body, r'(?i)<style|\sstyle=|\son[a-z]+=|javascript:')
        self.assertIn('<form method="post" action="/login"', body)
        self.assertIn('type="password"', body)


class LoginSuccessTests(WebAppCase):
    async def test_login_form_sets_a_double_submit_token(self):
        response = await self.get('/login')
        cookie = set_cookie(response, 'wb_hub_login')
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Strict')
        self.assertNotIn('secure', cookie)
        self.assertIn(f'name="login_token" value="{cookie["value"]}"', response.body_text)
        self.assertGreaterEqual(len(cookie['value']), 32)

    async def test_login_sets_the_session_cookie_records_the_session_and_audits(self):
        response = await self.login()
        self.assertEqual(response.status, 303, response.body_text)
        self.assertEqual(response.headers['Location'], '/')
        cookie = set_cookie(response, 'wb_hub_session')
        self.assertIsNotNone(cookie, response.headers.getall('Set-Cookie', []))
        self.assertTrue(cookie['httponly'])
        self.assertEqual((cookie['samesite'], cookie['path'], cookie['max-age']), ('Strict', '/', '86400'))
        self.assertNotIn('secure', cookie)
        token_hash = hashlib.sha256(cookie['value'].encode()).hexdigest()
        rows = self.rows('SELECT token_hash, created_at, expires_at, password_generation, client_addr FROM sessions')
        self.assertEqual(rows, [(token_hash, self.now[0], self.now[0] + 86400, self.generation, '127.0.0.1')])
        self.assertEqual(self.audit(), [('login_success', 'owner', '127.0.0.1', {'delay_seconds': 0})])
        home = await self.get('/', cookies={'wb_hub_session': cookie['value']})
        self.assertEqual(home.status, 200)
        self.assertIn('action="/logout"', home.body_text)

    async def test_without_a_session_a_page_redirects_to_login(self):
        response = await self.get('/')
        self.assertEqual((response.status, response.headers.get('Location')), (303, '/login'))


class TlsCookieTests(WebAppCase):
    tls = True

    async def test_session_and_login_cookies_are_secure_under_tls(self):
        cookie, field = await self.login_form()
        response = await self.post('/login', {'login_token': field, 'password': PASSWORD},
                                   cookies={'wb_hub_login': cookie})
        self.assertEqual(response.status, 303, response.body_text)
        self.assertTrue(set_cookie(response, 'wb_hub_session')['secure'])
        self.assertTrue(set_cookie(await self.get('/login'), 'wb_hub_login')['secure'])


class LoginFailureTests(WebAppCase):
    async def test_wrong_password_is_refused_without_a_session_and_audited(self):
        response = await self.login('wrong password!')
        self.assertEqual(response.status, 403)
        self.assertIn('비밀번호가 맞지 않습니다', response.body_text)
        self.assertNotIn('wrong password!', response.body_text)
        self.assertIsNone(set_cookie(response, 'wb_hub_session'))
        self.assertEqual(self.rows('SELECT count(*) FROM sessions'), [(0,)])
        self.assertEqual(self.audit(), [('login_failure', 'anonymous', '127.0.0.1', {'delay_seconds': 0})])
        self.assertNotIn('wrong password', json.dumps(self.rows('SELECT * FROM audit')))

    async def test_repeated_failures_make_the_next_attempt_wait_without_sleeping(self):
        for _ in range(4):
            self.assertEqual((await self.login('wrong password!')).status, 403)
        loop = asyncio.get_running_loop()
        started = loop.time()
        waiting = await self.login(PASSWORD)  # even the right password is not checked yet
        self.assertLess(loop.time() - started, 1.0)
        self.assertEqual(waiting.status, 429)
        self.assertEqual(waiting.headers['Retry-After'], '1')  # f(4) = 2^0
        self.assertIsNone(set_cookie(waiting, 'wb_hub_session'))
        self.now[0] += 1.0
        self.assertEqual((await self.login('wrong password!')).status, 403)
        self.assertEqual(self.audit()[-1][3], {'delay_seconds': 1})
        again = await self.login(PASSWORD)
        self.assertEqual((again.status, again.headers['Retry-After']), (429, '2'))  # f(5) = 2
        self.assertEqual(self.rows('SELECT count(*) FROM sessions'), [(0,)])
        self.now[0] += 2.0
        ok = await self.login(PASSWORD)
        self.assertEqual(ok.status, 303)
        self.assertEqual(self.audit()[-1][:2], ('login_success', 'owner'))
        self.assertEqual(self.audit()[-1][3], {'delay_seconds': 2})
        self.assertEqual(self.rows("SELECT count(*) FROM login_state WHERE key LIKE 'addr:%' OR key = 'global'"),
                         [(0,)])

    async def test_hostile_addresses_raising_the_global_delay_do_not_lock_out_the_owner(self):
        for _ in range(3):
            for host in range(1, 10):
                self.services.throttle.record_failure(f'10.66.0.{host}', now=self.now[0])
        self.assertEqual(self.services.throttle.check('10.8.0.7', self.now[0]), 30)
        response = await self.login(PASSWORD)  # this client is 127.0.0.1 (loopback)
        self.assertEqual(response.status, 303, response.body_text)
        self.assertEqual(self.rows("SELECT updated_at FROM login_state WHERE key = 'ok:127.0.0.1'"),
                         [(self.now[0],)])


class SessionValidityTests(WebAppCase):
    async def test_a_session_ends_24_hours_after_login_without_extension(self):
        token = await self.session_cookie()
        self.now[0] += 86399.0
        self.assertEqual((await self.get('/', cookies={'wb_hub_session': token})).status, 200)
        self.now[0] += 1.0
        expired = await self.get('/', cookies={'wb_hub_session': token})
        self.assertEqual((expired.status, expired.headers.get('Location')), (303, '/login'))
        cleared = set_cookie(expired, 'wb_hub_session')
        self.assertIn(cleared['value'], ('', '""'))
        self.assertEqual(cleared['max-age'], '0')
        api = await self.get('/api/badge', cookies={'wb_hub_session': token})
        self.assertEqual(api.status, 401)
        self.assertEqual(json.loads(api.body_text), {'ok': False, 'reason': 'unauthorized'})

    async def test_a_new_password_generation_ends_the_session(self):
        token = await self.session_cookie()
        self.passwords.set_password('another good password')  # ``wb hub passwd``
        response = await self.get('/', cookies={'wb_hub_session': token})
        self.assertEqual((response.status, response.headers.get('Location')), (303, '/login'))
        self.assertEqual((await self.get('/api/badge', cookies={'wb_hub_session': token})).status, 401)

    async def test_unknown_or_junk_tokens_are_not_sessions(self):
        for token in ('x' * 43, 'a' * 300, ''):
            self.assertEqual((await self.get('/', cookies={'wb_hub_session': token})).status, 303, token)
        self.assertEqual((await self.get('/api/badge')).status, 401)


class CsrfOriginTests(WebAppCase):
    async def logged_in(self) -> tuple[str, str]:
        """(session token, its CSRF value read from the logout form)."""
        token = await self.session_cookie()
        home = await self.get('/', cookies={'wb_hub_session': token})
        return token, re.search(r'name="csrf" value="([^"]+)"', home.body_text).group(1)

    async def assert_still_logged_in(self, token: str) -> None:
        self.assertEqual((await self.get('/', cookies={'wb_hub_session': token})).status, 200)
        self.assertNotIn('logout', [row[0] for row in self.audit()])

    async def test_state_change_without_or_with_a_wrong_csrf_is_refused_and_changes_nothing(self):
        token, csrf = await self.logged_in()
        other_token, other_csrf = await self.logged_in()
        for data in ({}, {'csrf': ''}, {'csrf': csrf[:-1] + ('A' if csrf[-1] != 'A' else 'B')}, {'csrf': other_csrf}):
            response = await self.post('/logout', data, cookies={'wb_hub_session': token})
            self.assertEqual((response.status, response.body_text), (403, 'csrf_failed'), data)
        await self.assert_still_logged_in(token)
        await self.assert_still_logged_in(other_token)

    async def test_csrf_in_the_form_or_the_header_is_accepted(self):
        token, csrf = await self.logged_in()
        response = await self.post('/logout', {'csrf': csrf}, cookies={'wb_hub_session': token})
        self.assertEqual((response.status, response.headers['Location']), (303, '/login'))
        self.assertEqual((await self.get('/', cookies={'wb_hub_session': token})).status, 303)
        self.assertEqual(self.audit()[-1], ('logout', 'owner', '127.0.0.1', {'delay_seconds': 0}))
        token, csrf = await self.logged_in()
        response = await self.post('/logout', {}, cookies={'wb_hub_session': token}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status, 303)

    async def test_a_foreign_origin_or_referer_is_refused_even_with_the_right_csrf(self):
        token, csrf = await self.logged_in()
        for origin, referer in (('http://evil.example', None), ('null', None), (f'https://{self.host()}', None),
                                (f'http://127.0.0.1:{self.port + 1}', None), (f'{self.origin}/path', None),
                                (None, 'http://evil.example/page'), (None, f'http://user@{self.host()}/')):
            headers = {'Referer': referer} if referer else {}
            response = await self.post('/logout', {'csrf': csrf}, cookies={'wb_hub_session': token},
                                       origin=origin, headers=headers)
            self.assertEqual(response.status, 403, (origin, referer))
        await self.assert_still_logged_in(token)
        response = await self.post('/logout', {'csrf': csrf}, cookies={'wb_hub_session': token}, origin=None,
                                   headers={'Referer': f'{self.origin}/settings'})
        self.assertEqual(response.status, 303)

    async def test_login_needs_the_double_submit_token_and_a_matching_origin(self):
        cookie, field = await self.login_form()
        other_cookie, _ = await self.login_form()
        cases = [({'password': PASSWORD}, {'wb_hub_login': cookie}, ''),
                 ({'password': PASSWORD, 'login_token': field}, {}, ''),
                 ({'password': PASSWORD, 'login_token': field}, {'wb_hub_login': other_cookie}, ''),
                 ({'password': PASSWORD, 'login_token': field}, {'wb_hub_login': cookie}, 'http://evil.example')]
        for data, cookies, origin in cases:
            response = await self.post('/login', data, cookies=cookies, origin=origin)
            self.assertEqual(response.status, 403, (data, cookies, origin))
            self.assertIsNone(set_cookie(response, 'wb_hub_session'))
        self.assertEqual(self.rows('SELECT count(*) FROM sessions'), [(0,)])
        self.assertEqual(self.audit(), [])

    async def test_an_oversized_body_is_413(self):
        token, csrf = await self.logged_in()
        response = await self.post('/logout', {'csrf': csrf, 'pad': 'x' * (256 * 1024 + 1)},
                                   cookies={'wb_hub_session': token})
        self.assertEqual(response.status, 413)
        await self.assert_still_logged_in(token)


class InsecureBannerTests(WebAppCase):
    insecure_warning = True

    async def test_plain_http_off_loopback_shows_the_banner_on_pages(self):
        login = await self.get('/login')
        self.assertIn('평문 HTTP로 LAN에 열려 있습니다. VPN 등 네트워크 보호가 필요합니다.', login.body_text)
        home = await self.get('/', cookies={'wb_hub_session': await self.session_cookie()})
        self.assertIn('평문 HTTP로 LAN에 열려 있습니다.', home.body_text)


class NoBannerTests(WebAppCase):
    async def test_loopback_pages_have_no_banner(self):
        self.assertNotIn('평문 HTTP', (await self.get('/login')).body_text)


class WebPasswordChangeTests(WebAppCase):
    NEW = 'a brand new password'

    async def setup_sessions(self) -> tuple[str, str, str]:
        token = await self.session_cookie()
        other = await self.session_cookie()
        page = await self.get('/settings', cookies={'wb_hub_session': token})
        self.assertEqual(page.status, 200)
        self.assertIn('action="/settings/password"', page.body_text)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.body_text).group(1)
        return token, other, csrf

    async def change(self, token: str, csrf: str, current: str, new: str, confirm: str | None = None):
        return await self.post('/settings/password', {'csrf': csrf, 'current': current, 'new': new,
                                                      'confirm': new if confirm is None else confirm},
                               cookies={'wb_hub_session': token})

    async def test_success_keeps_only_this_browser_logged_in_and_audits(self):
        token, other, csrf = await self.setup_sessions()
        response = await self.change(token, csrf, PASSWORD, self.NEW)
        self.assertEqual((response.status, response.headers.get('Location')), (303, '/settings'))
        fresh = set_cookie(response, 'wb_hub_session')
        self.assertNotEqual(fresh['value'], token)
        self.assertTrue(fresh['httponly'])
        self.assertEqual(fresh['samesite'], 'Strict')
        self.assertEqual(self.passwords.verify(self.NEW), self.generation + 1)
        self.assertIsNone(self.passwords.verify(PASSWORD))
        self.assertEqual((await self.get('/settings', cookies={'wb_hub_session': fresh['value']})).status, 200)
        for old in (token, other):
            self.assertEqual((await self.get('/settings', cookies={'wb_hub_session': old})).status, 303)
        self.assertEqual(self.rows('SELECT count(*) FROM sessions'), [(1,)])
        changed = [row for row in self.audit() if row[0] == 'password_changed']
        self.assertEqual(changed, [('password_changed', 'owner', '127.0.0.1',
                                    {'source': 'web', 'invalidated_sessions': 1})])
        self.assertNotIn(self.NEW, json.dumps(self.rows('SELECT * FROM audit')))

    async def test_wrong_current_password_is_a_login_failure_and_changes_nothing(self):
        token, other, csrf = await self.setup_sessions()
        response = await self.change(token, csrf, 'not the password', self.NEW)
        self.assertEqual(response.status, 403)
        self.assertIn('비밀번호가 맞지 않습니다', response.body_text)
        self.assertEqual(self.passwords.verify(PASSWORD), self.generation)
        self.assertEqual(self.audit()[-1], ('login_failure', 'owner', '127.0.0.1', {'delay_seconds': 0}))
        self.assertEqual(self.rows("SELECT failures FROM login_state WHERE key = 'addr:127.0.0.1'"), [(1,)])
        for live in (token, other):
            self.assertEqual((await self.get('/settings', cookies={'wb_hub_session': live})).status, 200)

    async def test_mismatched_or_short_new_password_changes_nothing(self):
        token, _other, csrf = await self.setup_sessions()
        self.assertEqual((await self.change(token, csrf, PASSWORD, self.NEW, self.NEW + 'x')).status, 400)
        self.assertEqual((await self.change(token, csrf, PASSWORD, 'short')).status, 400)
        self.assertEqual(self.passwords.verify(PASSWORD), self.generation)
        self.assertNotIn('password_changed', [row[0] for row in self.audit()])

    async def test_a_throttled_change_re_renders_the_settings_page(self):
        token, _other, csrf = await self.setup_sessions()
        for _ in range(4):
            self.assertEqual((await self.change(token, csrf, 'not the password', self.NEW)).status, 403)
        response = await self.change(token, csrf, PASSWORD, self.NEW)
        self.assertEqual((response.status, response.headers.get('Retry-After')), (429, '1'))
        self.assertIn('action="/settings/password"', response.body_text)
        self.assertNotIn('action="/login"', response.body_text)
        self.assertIsNone(set_cookie(response, 'wb_hub_login'))
        self.assertEqual(self.passwords.verify(PASSWORD), self.generation)

    async def test_change_needs_csrf(self):
        token, _other, _csrf = await self.setup_sessions()
        response = await self.change(token, '', PASSWORD, self.NEW)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.passwords.verify(PASSWORD), self.generation)


def _ws_route(app):
    from aiohttp import web
    from workbench.hub.web.app import public

    async def handler(request):
        socket_ = web.WebSocketResponse()
        await socket_.prepare(request)
        await socket_.close()
        return socket_
    app.router.add_get('/ws/echo', public(handler))


class WebSocketOriginTests(WebAppCase):
    routes = (_ws_route,)

    async def test_a_websocket_upgrade_from_a_foreign_origin_is_refused(self):
        from aiohttp import WSServerHandshakeError
        url = f'{self.base}/ws/echo'
        async with self.http.ws_connect(url, headers={'Host': self.host(), 'Origin': self.origin}):
            pass
        with self.assertRaises(WSServerHandshakeError) as caught:
            async with self.http.ws_connect(url, headers={'Host': self.host(), 'Origin': 'http://evil.example'}):
                pass
        self.assertEqual(caught.exception.status, 403)


if __name__ == '__main__':
    unittest.main()
