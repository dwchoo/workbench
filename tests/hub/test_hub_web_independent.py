"""TI-03 independent verification: a real ``wb _hub --foreground`` driven by an aiohttp client.

Expected values come from the SPEC (hub.toml schema, Web listener lifetime, authentication,
sessions, CSRF, login delay, security headers, audit), BRIEF D-09..D-12/D-30 and P2-AC-15..18, and
from the SPEC correction of 2026-10-25 (``Referrer-Policy: same-origin``), not from the
worker's tests. Every Hub binds 127.0.0.0/8 with port 0 in a temp HOME/XDG sandbox; client
addresses 127.0.0.2.. stand in for distinct LAN clients. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode

import aiohttp

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from workbench.hub import config  # noqa: E402
from workbench.hub.web import render  # noqa: E402
from fixtures.web_sandbox import PASSWORD, WebSandbox, tcp_listening, wait_until  # noqa: E402

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
WRONG = 'definitely-not-the-password'
NEW_PASSWORD = 'a brand new passphrase 2'
SESSION, LOGIN = 'wb_hub_session', 'wb_hub_login'
DEFAULT = object()  # "the Origin a same-origin browser sends"
NONE = object()  # "no such header"


def parse_set_cookie(headers) -> dict:
    """``{name: (value, [attributes...])}`` from every ``Set-Cookie`` header, attributes as sent."""
    cookies = {}
    for raw in headers.getall('Set-Cookie', []):
        first, *attributes = [part.strip() for part in raw.split(';')]
        name, _, value = first.partition('=')
        cookies[name] = (value, attributes)
    return cookies


class Reply:
    def __init__(self, status, headers, text, elapsed):
        self.status, self.headers, self.text, self.elapsed = status, headers, text, elapsed
        self.cookies = parse_set_cookie(headers)

    def hidden(self, name: str) -> str:
        found = re.search(rf'name="{name}" value="([^"]+)"', self.text)
        return found.group(1) if found else ''


class Client:
    """One browser-like client of the Hub: manual cookies, optional source address and scheme."""

    def __init__(self, port: int, *, ip: str = '127.0.0.1', scheme: str = 'http', source: str | None = None):
        self.port, self.ip, self.scheme = port, ip, scheme
        connector = aiohttp.TCPConnector(ssl=False, force_close=True, local_addr=(source, 0) if source else None)
        self.http = aiohttp.ClientSession(connector=connector, cookie_jar=aiohttp.DummyCookieJar(),
                                          timeout=aiohttp.ClientTimeout(total=60))
        self.session_cookie = ''
        self.csrf = ''

    @property
    def authority(self) -> str:
        return f'{self.ip}:{self.port}'

    @property
    def origin(self) -> str:
        return f'{self.scheme}://{self.authority}'

    async def close(self) -> None:
        await self.http.close()

    async def req(self, method: str, path: str, *, host=None, origin=DEFAULT, referer=None, headers=None,
                  data=None, cookies=None, raw_body=None) -> Reply:
        sent = dict(headers or {})
        if host is not None:
            sent['Host'] = host
        if method != 'GET' and origin is not NONE:
            sent['Origin'] = self.origin if origin is DEFAULT else origin
        if referer is not None:
            sent['Referer'] = referer
        if cookies is None:
            cookies = {SESSION: self.session_cookie} if self.session_cookie else {}
        if cookies:
            sent['Cookie'] = '; '.join(f'{name}={value}' for name, value in cookies.items())
        body = raw_body if raw_body is not None else (urlencode(data).encode() if data is not None else None)
        if body is not None and 'Content-Type' not in sent:
            sent['Content-Type'] = 'application/x-www-form-urlencoded'
        started = time.monotonic()
        async with self.http.request(method, f'{self.scheme}://{self.authority}{path}', headers=sent, data=body,
                                     allow_redirects=False) as response:
            text = await response.text(errors='replace')
            return Reply(response.status, response.headers, text, time.monotonic() - started)

    async def get_login_token(self) -> tuple[str, Reply]:
        reply = await self.req('GET', '/login')
        return reply.cookies.get(LOGIN, ('', []))[0], reply

    async def login(self, password: str = PASSWORD) -> Reply:
        token, _ = await self.get_login_token()
        return await self.req('POST', '/login', data={'login_token': token, 'password': password},
                              cookies={LOGIN: token})

    async def sign_in(self, password: str = PASSWORD) -> 'Client':
        reply = await self.login(password)
        assert reply.status == 303 and SESSION in reply.cookies, (reply.status, reply.text)
        self.session_cookie = reply.cookies[SESSION][0]
        self.csrf = (await self.req('GET', '/')).hidden('csrf')
        assert self.csrf
        return self


def self_signed(directory: Path, name: str) -> tuple[Path, Path]:
    cert, key = directory / f'{name}-cert.pem', directory / f'{name}-key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                    '-nodes', '-days', '2', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                    '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True, timeout=30)
    os.chmod(key, 0o600)
    return cert, key


def raw_exchange(port: int, payload: bytes, *, timeout: float = 10.0, host: str = '127.0.0.1') -> tuple[int, bytes]:
    """Send ``payload`` verbatim; return ``(status code or 0, whole response)``."""
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.sendall(payload)
        chunks = []
        try:
            while True:
                data = connection.recv(65536)
                if not data:
                    break
                chunks.append(data)
        except (socket.timeout, ConnectionResetError):
            pass
    blob = b''.join(chunks)
    match = re.match(rb'HTTP/1\.[01] (\d{3})', blob)
    return (int(match.group(1)) if match else 0), blob


class HubCase(unittest.IsolatedAsyncioTestCase):
    """A sandboxed real Hub per test; ``self.clients`` are closed after it."""

    async def asyncSetUp(self):
        self.box = WebSandbox(self)
        self.clients: list[Client] = []
        self.addAsyncCleanup(self.close_clients)

    async def close_clients(self):
        for client in self.clients:
            await client.close()

    def client(self, **kwargs) -> Client:
        client = Client(self.port, **kwargs)
        self.clients.append(client)
        return client

    def open_hub(self, *, password: bool = True, bind: str = '127.0.0.1', extra=(), raw: str | None = None):
        self.box.write_config(bind, 0, *extra, raw=raw)
        if password:
            self.box.set_password()
        self.hub = self.box.start_hub()
        if password and (raw is None):
            host, self.port = self.box.wait_listening(self.hub.pid)
            self.assert_not_wildcard()
            return host
        return None

    def assert_not_wildcard(self):
        for address, _port in tcp_listening(self.hub.pid):
            self.assertNotIn(address, ('0.0.0.0', '::', '0:0:0:0:0:0:0:0'), 'the Hub must never bind a wildcard here')

    def db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.box.state / 'hub.sqlite3', timeout=10)

    def audit(self) -> list[tuple]:
        with self.db() as connection:
            return connection.execute('SELECT event, actor, client_addr, detail_json FROM audit ORDER BY id').fetchall()

    def audit_events(self) -> list[str]:
        return [row[0] for row in self.audit()]

    def assert_closed(self, reason: str, hub=None, wait: float = 2.5):
        hub = hub or self.hub
        status = self.box.status()
        self.assertEqual(status['web']['state'], reason, status)
        self.assertEqual(status['registry']['state'], 'ok', 'the rest of the Hub keeps working')
        time.sleep(wait)  # the 2 s watchdog gets a full tick to (wrongly) open
        self.assertEqual(tcp_listening(hub.pid), [])
        return status


# --- listener lifetime ---------------------------------------------------------------------------


class ListenerLifetimeTests(HubCase):
    async def test_no_password_means_no_port_and_the_password_opens_it_without_a_restart(self):
        self.open_hub(password=False)
        time.sleep(0.5)
        self.assertEqual(tcp_listening(self.hub.pid), [], 'no TCP port before a password exists')
        status = self.assert_closed('password_not_set')
        self.assertIn('password_not_set', self.box.cli('hub', 'status').stdout)
        self.assertIsNone(self.hub.poll())
        pid = self.hub.pid
        self.box.set_password()
        _host, self.port = None, None
        self.assertTrue(wait_until(lambda: bool(tcp_listening(pid)), 5.0), 'listening within 5 s of passwd')
        _host, self.port = self.box.wait_listening(pid)
        self.assertEqual(self.hub.pid, pid)
        self.assertIsNone(self.hub.poll(), 'same Hub process, no restart')
        reply = await self.client().req('GET', '/login')
        self.assertEqual(reply.status, 200)
        web = self.box.status()['web']
        self.assertEqual((web['state'], web['tls'], web['insecure_warning']), ('listening', False, False))
        self.assertEqual(status['registry']['state'], 'ok')

    async def test_a_corrupt_auth_json_never_opens_and_a_real_password_repairs_it(self):
        self.box.write_config()
        auth_json = self.box.state / 'auth.json'
        auth_json.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        auth_json.write_text('{"schema_version": 1, "kdf": "scrypt", "hash": "AAAA", "salt": ')
        os.chmod(auth_json, 0o600)
        self.hub = self.box.start_hub()
        self.assert_closed('password_not_set')
        auth_json.write_bytes(b'')
        time.sleep(2.5)
        self.assertEqual(tcp_listening(self.hub.pid), [], 'an empty auth.json is not a password')
        self.box.set_password()
        self.assertTrue(wait_until(lambda: bool(tcp_listening(self.hub.pid)), 5.0))

    async def test_listener_binds_only_the_configured_address(self):
        self.open_hub(bind='127.0.0.2')
        self.assertEqual(tcp_listening(self.hub.pid), [('127.0.0.2', self.port)])
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=2).close()
        owner = self.client(ip='127.0.0.2')
        self.assertEqual((await owner.req('GET', '/login')).status, 200)
        self.assertEqual((await owner.req('GET', '/login', host=f'127.0.0.1:{self.port}')).status, 421,
                         'the bind IP is the Host, not any loopback address')
        self.assertEqual((await owner.req('GET', '/login', host=f'localhost:{self.port}')).status, 200)
        web = self.box.status()['web']
        self.assertEqual((web['bind'], web['port'], web['url']), ('127.0.0.2', self.port, f'http://127.0.0.2:{self.port}'))

    async def test_invalid_hub_toml_keeps_the_listener_closed_but_the_hub_alive(self):
        cases = {
            'unknown key': ('[web]\nbind = "127.0.0.1"\nport = 0\ncolour = "blue"\n', 'web.colour'),
            'unknown table': ('[web]\nport = 0\n[tls]\nx = 1\n', 'tls'),
            'hostname bind': ('[web]\nbind = "localhost"\nport = 0\n', 'web.bind'),
            'broken toml': ('[web\nport = 0', 'toml'),
            'bad type': ('[web]\nport = "0"\n', 'web.port'),
        }
        for label, (text, item) in cases.items():
            with self.subTest(label):
                box = WebSandbox(self)
                box.write_config(raw=text)
                box.set_password()
                hub = box.start_hub()
                status = box.status()
                self.assertEqual(status['web']['state'], 'config_invalid', status)
                self.assertEqual(status['web']['detail'], item)
                self.assertEqual(status['registry']['state'], 'ok')
                self.assertIn('config_invalid', box.cli('hub', 'status').stdout)
                time.sleep(2.5)
                self.assertEqual(tcp_listening(hub.pid), [])
                self.assertEqual(box.cli('hub', 'list', '--json').returncode, 0, 'list still works')
                box.stop_hub(hub)

    async def test_stop_closes_the_listener_and_leaves_no_process(self):
        self.open_hub()
        owner = await self.client().sign_in()
        idle = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        pid = self.hub.pid
        try:
            started = time.monotonic()
            self.assertEqual(self.box.stop_hub(self.hub), 0, self.box.hub_log(self.hub))
            self.assertLess(time.monotonic() - started, 10.0)
        finally:
            idle.close()
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=2).close()
        self.assertFalse((self.box.state / 'hub.json').exists())
        leftovers = [name for name in os.listdir('/proc') if name.isdigit() and self.same_session(int(name), pid)]
        self.assertEqual(leftovers, [])
        self.assertNotIn('Traceback', self.box.hub_log(self.hub))
        del owner

    @staticmethod
    def same_session(pid: int, session: int) -> bool:
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            return int(fields[3]) == session and fields[0] != 'Z'
        except (OSError, IndexError, ValueError):
            return False


# --- TLS -----------------------------------------------------------------------------------------


class TlsTests(HubCase):
    async def test_cert_and_key_serve_https_with_secure_cookies_and_no_plain_http(self):
        self.box.write_config()
        self.box.install_tls()
        self.box.set_password()
        self.hub = self.box.start_hub()
        _host, self.port = self.box.wait_listening(self.hub.pid)
        web = self.box.status()['web']
        self.assertEqual((web['state'], web['tls'], web['url']), ('listening', True, f'https://127.0.0.1:{self.port}'))
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as sock, \
                context.wrap_socket(sock, server_hostname='127.0.0.1') as tls:
            self.assertGreaterEqual(tls.version(), 'TLSv1.2')
        status, blob = raw_exchange(self.port, b'GET /login HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
        self.assertNotEqual(status, 200, 'plain HTTP must not be answered on the TLS port')
        self.assertNotIn(b'<form', blob)
        owner = self.client(scheme='https')
        token, page = await owner.get_login_token()
        for name in (LOGIN,):
            self.assertIn('Secure', page.cookies[name][1])
        self.assertEqual((await owner.req('GET', '/login', host='127.0.0.1')).status, 421, 'https default port 443')
        reply = await owner.req('POST', '/login', data={'login_token': token, 'password': PASSWORD}, cookies={LOGIN: token})
        self.assertEqual(reply.status, 303)
        attributes = reply.cookies[SESSION][1]
        self.assertIn('Secure', attributes)
        self.assertIn('HttpOnly', attributes)
        self.assertIn('SameSite=Strict', attributes)
        self.assertEqual(self.box.status()['web']['insecure_warning'], False)
        self.assertNotIn('평문 HTTP', page.text)
        # An http:// Origin is not this origin on an https listener.
        owner.session_cookie = reply.cookies[SESSION][0]
        csrf = (await owner.req('GET', '/')).hidden('csrf')
        refused = await owner.req('POST', '/logout', data={'csrf': csrf}, origin=f'http://127.0.0.1:{self.port}')
        self.assertEqual(refused.status, 403)

    async def test_bad_tls_never_falls_back_to_http(self):
        with tempfile.TemporaryDirectory(prefix='wb-tls-') as other:
            other_cert, other_key = self_signed(Path(other), 'other')
            for label in ('key only', 'cert only', 'garbage cert', 'mismatched pair', 'empty files'):
                with self.subTest(label):
                    box = WebSandbox(self)
                    box.write_config()
                    cert, key = box.install_tls()
                    if label == 'key only':
                        cert.unlink()
                    elif label == 'cert only':
                        key.unlink()
                    elif label == 'garbage cert':
                        cert.write_text('-----BEGIN CERTIFICATE-----\nnot a certificate\n-----END CERTIFICATE-----\n')
                    elif label == 'mismatched pair':
                        key.write_bytes(other_key.read_bytes())
                    else:
                        cert.write_bytes(b'')
                        key.write_bytes(b'')
                    box.set_password()
                    hub = box.start_hub()
                    status = box.status()
                    self.assertEqual(status['web']['state'], 'tls_invalid', (label, status))
                    self.assertEqual(status['registry']['state'], 'ok')
                    time.sleep(2.5)
                    self.assertEqual(tcp_listening(hub.pid), [], label)
                    box.stop_hub(hub)

    async def test_disallowing_insecure_http_without_tls_opens_nothing_but_with_tls_opens_https(self):
        self.box.write_config('127.0.0.1', 0, 'allow_insecure_http = false')
        self.box.set_password()
        self.hub = self.box.start_hub()
        status = self.assert_closed('insecure_http_disallowed')
        self.assertIn('insecure_http_disallowed', self.box.cli('hub', 'status').stdout)
        self.assertNotIn('url', status['web'])
        self.box.stop_hub(self.hub)
        # The same setting with a TLS pair is fine: HTTPS opens.
        self.box.install_tls()
        self.hub = self.box.start_hub()
        _host, self.port = self.box.wait_listening(self.hub.pid)
        self.assertTrue(self.box.status()['web']['tls'])


# --- insecure warning (function and page level; no real LAN bind) -------------------------------


class InsecureWarningTests(HubCase):
    def test_only_plain_http_off_loopback_warns(self):
        for bind, tls, expected in (('0.0.0.0', False, True), ('::', False, True), ('192.0.2.7', False, True),
                                    ('10.8.0.2', False, True), ('2001:db8::1', False, True),
                                    ('127.0.0.1', False, False), ('127.0.0.2', False, False),
                                    ('::1', False, False), ('0.0.0.0', True, False), ('10.8.0.2', True, False)):
            with self.subTest(bind=bind, tls=tls):
                self.assertIs(config.insecure_warning(bind, tls=tls), expected)

    def test_the_banner_is_the_brief_sentence_and_only_on_request(self):
        html = str(render.layout('x', 'body', insecure_warning=True))
        self.assertIn('평문 HTTP로 LAN에 열려 있습니다. VPN 등 네트워크 보호가 필요합니다.', html)
        self.assertNotIn('평문 HTTP', str(render.layout('x', 'body')))

    async def test_a_loopback_plain_http_hub_shows_no_warning_anywhere(self):
        self.open_hub()
        page = await self.client().req('GET', '/login')
        self.assertNotIn('평문 HTTP', page.text)
        self.assertNotIn('insecure', self.box.cli('hub', 'status').stdout.lower().replace('insecure_ok', ''))
        self.assertIs(self.box.status()['web']['insecure_warning'], False)


# --- Host / Origin / WebSocket -----------------------------------------------------------------


class HostOriginTests(HubCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.open_hub()
        self.owner = await self.client().sign_in()

    async def test_unknown_hosts_get_421_with_the_security_headers_even_with_a_session(self):
        port = self.port
        bad = ['evil.example', f'evil.example:{port}', f'127.0.0.1:{port + 1}', '127.0.0.1', f'127.0.0.1.evil.com:{port}',
               f'[::1]:{port}', f'0.0.0.0:{port}', f'127.0.0.2:{port}', f'localhost.:{port}', f'localhost:0{port}',
               f'127.0.0.1:{port}.evil', f'user@127.0.0.1:{port}', f'127.000.000.001:{port}',
               f'2130706433:{port}', f'0x7f.0.0.1:{port}', '', f'localhost:{port},evil.example']
        for host in bad:
            with self.subTest(host=host):
                try:
                    reply = await self.owner.req('GET', '/', host=host)
                except aiohttp.ClientError:
                    continue  # a transport-level refusal is also not a 200
                self.assertIn(reply.status, (400, 421), reply.text[:80])
                if reply.status == 421:
                    self.assertEqual(reply.headers.get('Content-Security-Policy'), CSP)
                self.assertNotIn(self.owner.csrf, reply.text)
        for host in (f'127.0.0.1:{port}', f'localhost:{port}'):
            self.assertEqual((await self.owner.req('GET', '/', host=host)).status, 200, host)

    async def test_two_host_headers_or_an_absolute_form_host_are_not_served(self):
        port = self.port
        twice = (f'GET /login HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nHost: evil.example\r\n\r\n').encode()
        status, _ = raw_exchange(port, twice)
        self.assertIn(status, (400, 421))
        status, _ = raw_exchange(port, b'GET /login HTTP/1.1\r\n\r\n')
        self.assertIn(status, (400, 421))

    async def test_state_changes_need_this_exact_origin_and_null_is_refused(self):
        port, csrf = self.port, self.owner.csrf
        wrong = ['null', 'http://evil.example', f'http://evil.example:{port}', f'http://127.0.0.1:{port + 1}',
                 f'https://127.0.0.1:{port}', f'http://localhost:{port}', f'http://127.0.0.1:{port}/',
                 f'http://127.0.0.1:{port}/x', f'HTTP://evil.example', '', ' ', 'file://', f'http://127.0.0.1:{port}@evil.example',
                 f'http://evil.example@127.0.0.1:{port}', 'http://127.0.0.1', f'http://127.0.0.1:{port} http://evil.example']
        for origin in wrong:
            with self.subTest(origin=origin):
                reply = await self.owner.req('POST', '/logout', data={'csrf': csrf}, origin=origin)
                self.assertEqual(reply.status, 403, origin)
        again = await self.owner.req('GET', '/')
        self.assertEqual(again.status, 200, 'every refusal left the session intact')
        self.assertEqual(self.audit_events().count('logout'), 0)
        # The exact origin passes the checks and reaches the handler (a wrong current password is audited).
        fine = await self.owner.req('POST', '/settings/password', data={'csrf': csrf, 'current': WRONG, 'new': NEW_PASSWORD,
                                                                        'confirm': NEW_PASSWORD},
                                    origin=f'http://127.0.0.1:{port}')
        self.assertEqual(fine.status, 403)
        self.assertEqual(self.audit_events().count('login_failure'), 1)

    async def test_two_origin_headers_and_foreign_referers_are_refused(self):
        port, csrf = self.port, self.owner.csrf
        own = f'http://127.0.0.1:{port}'
        payload = urlencode({'csrf': csrf}).encode()
        request = (f'POST /logout HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: {own}\r\nOrigin: http://evil.example\r\n'
                   f'Cookie: {SESSION}={self.owner.session_cookie}\r\nContent-Type: application/x-www-form-urlencoded\r\n'
                   f'Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n').encode() + payload
        self.assertEqual(raw_exchange(port, request)[0], 403)
        self.assertEqual((await self.owner.req('GET', '/')).status, 200)
        for referer in ('http://evil.example/page', f'http://evil.example:{port}/', 'null', f'http://127.0.0.1:{port + 1}/'):
            with self.subTest(referer=referer):
                reply = await self.owner.req('POST', '/logout', data={'csrf': csrf}, origin=NONE, referer=referer)
                self.assertEqual(reply.status, 403)
        self.assertEqual((await self.owner.req('GET', '/')).status, 200)
        # A same-origin Referer without Origin (some clients) is accepted.
        fine = await self.owner.req('POST', '/logout', data={'csrf': csrf}, origin=NONE, referer=f'{own}/settings')
        self.assertEqual((fine.status, fine.headers['Location']), (303, '/login'))

    async def test_login_post_is_origin_checked_too(self):
        guest = self.client()
        token, _ = await guest.get_login_token()
        for origin in ('null', 'http://evil.example'):
            reply = await guest.req('POST', '/login', data={'login_token': token, 'password': PASSWORD},
                                    cookies={LOGIN: token}, origin=origin)
            self.assertEqual(reply.status, 403, origin)
            self.assertNotIn(SESSION, reply.cookies)
        self.assertEqual(self.audit_events().count('login_success'), 1, 'only the setUp login')

    async def test_websocket_upgrades_from_a_foreign_origin_are_refused_before_any_session_check(self):
        port = self.port
        cookie = {'Cookie': f'{SESSION}={self.owner.session_cookie}'}
        for origin in ('http://evil.example', 'null', f'http://127.0.0.1:{port + 1}'):
            with self.subTest(origin=origin):
                with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                    await self.owner.http.ws_connect(f'http://127.0.0.1:{port}/ws/board', headers={'Origin': origin, **cookie})
                self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:  # same origin but no session
            await self.owner.http.ws_connect(f'http://127.0.0.1:{port}/ws/board', headers={'Origin': f'http://127.0.0.1:{port}'})
        self.assertIn(caught.exception.status, (401, 403, 404))
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:  # wrong Host
            await self.owner.http.ws_connect(f'http://127.0.0.1:{port}/ws/board',
                                             headers={'Host': 'evil.example', 'Origin': 'http://evil.example', **cookie})
        self.assertEqual(caught.exception.status, 421)


# --- sessions, CSRF, cookies, headers, audit ---------------------------------------------------


class SessionTests(HubCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.open_hub()

    async def test_cookies_are_httponly_samesite_strict_with_a_24h_max_age_and_no_secure_on_http(self):
        guest = self.client()
        token, page = await guest.get_login_token()
        login_attrs = page.cookies[LOGIN][1]
        self.assertIn('HttpOnly', login_attrs)
        self.assertIn('SameSite=Strict', login_attrs)
        self.assertNotIn('Secure', login_attrs)
        reply = await guest.req('POST', '/login', data={'login_token': token, 'password': PASSWORD}, cookies={LOGIN: token})
        self.assertEqual((reply.status, reply.headers['Location']), (303, '/'))
        value, attributes = reply.cookies[SESSION]
        self.assertGreaterEqual(len(value), 40)
        self.assertEqual(set(attributes) & {'HttpOnly', 'SameSite=Strict', 'Path=/', 'Max-Age=86400'},
                         {'HttpOnly', 'SameSite=Strict', 'Path=/', 'Max-Age=86400'}, attributes)
        self.assertNotIn('Secure', attributes)
        self.assertFalse([a for a in attributes if a.lower().startswith('domain')])
        # Only the digest of the token is stored.
        with self.db() as connection:
            rows = connection.execute('SELECT token_hash, created_at, expires_at FROM sessions').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0][0], value)
        self.assertAlmostEqual(rows[0][2] - rows[0][1], 86400, delta=2, msg='absolute 24 h expiry')
        self.assertAlmostEqual(rows[0][1], time.time(), delta=60)

    async def test_expiry_is_absolute_server_side_and_not_extended_by_use(self):
        owner = await self.client().sign_in()
        with self.db() as connection:
            before = connection.execute('SELECT expires_at FROM sessions').fetchone()[0]
        for _ in range(3):
            self.assertEqual((await owner.req('GET', '/settings')).status, 200)
        with self.db() as connection:
            self.assertEqual(connection.execute('SELECT expires_at FROM sessions').fetchone()[0], before)
            connection.execute('UPDATE sessions SET expires_at = ?', (time.time() - 1,))
            connection.commit()
        reply = await owner.req('GET', '/settings')
        self.assertEqual((reply.status, reply.headers['Location']), (303, '/login'))
        self.assertEqual((await owner.req('GET', '/')).status, 303)
        refused = await owner.req('POST', '/logout', data={'csrf': owner.csrf})
        self.assertIn(refused.status, (303, 401, 403), 'an expired session cannot act, even with its CSRF token')
        self.assertNotIn('logout', self.audit_events())

    async def test_a_forged_or_foreign_cookie_is_no_session(self):
        owner = await self.client().sign_in()
        probe = self.client()
        for value in ('', 'x', owner.session_cookie[:-1], owner.session_cookie + 'a', owner.session_cookie.upper(),
                      '../' + owner.session_cookie, 'null', 'A' * 4096):
            with self.subTest(value=value[:24]):
                reply = await probe.req('GET', '/settings', cookies={SESSION: value})
                self.assertEqual((reply.status, reply.headers.get('Location')), (303, '/login'))
        with self.db() as connection:  # the stored digest is not a usable cookie either
            digest = connection.execute('SELECT token_hash FROM sessions').fetchone()[0]
        reply = await probe.req('GET', '/settings', cookies={SESSION: digest})
        self.assertEqual(reply.status, 303)

    async def test_csrf_missing_wrong_foreign_or_misplaced_is_403_and_changes_nothing(self):
        alice = await self.client().sign_in()
        bob = await self.client(source='127.0.0.2').sign_in()
        self.assertNotEqual(alice.csrf, bob.csrf)
        before = len(self.audit())
        attempts = {
            'none': {}, 'empty': {'csrf': ''}, 'wrong': {'csrf': 'x' * len(alice.csrf)},
            'prefix': {'csrf': alice.csrf[:-1]}, 'longer': {'csrf': alice.csrf + 'x'},
            'other session': {'csrf': bob.csrf}, 'login token': {'login_token': alice.csrf},
        }
        for label, form in attempts.items():
            with self.subTest(label):
                reply = await alice.req('POST', '/logout', data=form)
                self.assertEqual(reply.status, 403, label)
                self.assertIn('csrf_failed', reply.text)
        # wrong place: query string, cookie, JSON body, header duplicated
        for label, kwargs in {
            'query': {'path': f'/logout?csrf={alice.csrf}'},
            'cookie': {'path': '/logout', 'cookies': {SESSION: alice.session_cookie, 'csrf': alice.csrf}},
            'json': {'path': '/logout', 'raw_body': json.dumps({'csrf': alice.csrf}).encode(),
                     'headers': {'Content-Type': 'application/json'}},
            'two fields': {'path': '/logout', 'raw_body': f'csrf={bob.csrf}&csrf={alice.csrf}'.encode()},
            'two headers': {'path': '/logout', 'headers': {'X-CSRF-Token': alice.csrf + ', ' + alice.csrf}},
        }.items():
            with self.subTest(label):
                path = kwargs.pop('path')
                reply = await alice.req('POST', path, **kwargs)
                self.assertEqual(reply.status, 403, label)
        for method in ('PUT', 'PATCH', 'DELETE'):
            with self.subTest(method):
                reply = await alice.req(method, '/settings/password')
                self.assertIn(reply.status, (403, 404, 405))
                if reply.status != 405 and reply.status != 404:
                    self.assertEqual(reply.status, 403)
        self.assertEqual((await alice.req('GET', '/settings')).status, 200, 'still logged in')
        self.assertEqual(len(self.audit()), before)
        # Header form works, and one session's token never works for another session.
        self.assertEqual((await bob.req('POST', '/logout', headers={'X-CSRF-Token': alice.csrf})).status, 403)
        self.assertEqual((await alice.req('POST', '/logout', headers={'X-CSRF-Token': alice.csrf})).status, 303)

    async def test_csrf_failure_on_the_password_form_leaves_the_password_alone(self):
        owner = await self.client().sign_in()
        form = {'current': PASSWORD, 'new': NEW_PASSWORD, 'confirm': NEW_PASSWORD}
        self.assertEqual((await owner.req('POST', '/settings/password', data=form)).status, 403)
        self.assertEqual((await owner.req('POST', '/settings/password', data={**form, 'csrf': 'nope'})).status, 403)
        self.assertEqual((await self.client().login(PASSWORD)).status, 303, 'old password still valid')
        self.assertEqual((await self.client().login(NEW_PASSWORD)).status, 403)
        self.assertNotIn('password_changed', self.audit_events()[1:])

    async def test_login_needs_a_matching_double_submit_token(self):
        guest = self.client()
        token, _ = await guest.get_login_token()
        cases = {
            'no cookie': dict(data={'login_token': token, 'password': PASSWORD}, cookies={}),
            'no field': dict(data={'password': PASSWORD}, cookies={LOGIN: token}),
            'mismatch': dict(data={'login_token': token + 'x', 'password': PASSWORD}, cookies={LOGIN: token}),
            'empty both': dict(data={'login_token': '', 'password': PASSWORD}, cookies={LOGIN: ''}),
            'session cookie as token': dict(data={'login_token': 'a', 'password': PASSWORD}, cookies={SESSION: 'a'}),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                reply = await guest.req('POST', '/login', **kwargs)
                self.assertEqual(reply.status, 403, label)
                self.assertNotIn(SESSION, reply.cookies)
        self.assertEqual([e for e in self.audit_events() if e.startswith('login')], [],
                         'a forged login form is neither a success nor a counted failure')
        self.assertEqual((await guest.req('POST', '/login', data={'login_token': token, 'password': PASSWORD},
                                          cookies={LOGIN: token})).status, 303)

    async def test_login_failure_shows_one_neutral_message_and_sets_no_session(self):
        guest = self.client()
        reply = await guest.login(WRONG)
        self.assertEqual(reply.status, 403)
        self.assertIn('비밀번호가 맞지 않습니다', reply.text)
        self.assertNotIn(SESSION, reply.cookies)
        self.assertNotIn(WRONG, reply.text)
        self.assertNotIn(PASSWORD, reply.text)

    async def test_logout_ends_the_session_clears_the_cookie_and_is_audited(self):
        owner = await self.client().sign_in()
        reply = await owner.req('POST', '/logout', data={'csrf': owner.csrf})
        self.assertEqual((reply.status, reply.headers['Location']), (303, '/login'))
        value, attributes = reply.cookies[SESSION]
        self.assertIn(value, ('', '""'))
        self.assertIn('Max-Age=0', attributes)
        self.assertEqual((await owner.req('GET', '/settings')).status, 303, 'old cookie is dead server-side')
        again = await owner.req('POST', '/logout', data={'csrf': owner.csrf})
        self.assertNotEqual(again.status, 200)
        with self.db() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
        self.assertEqual(self.audit_events().count('logout'), 1)

    async def test_security_headers_are_on_every_kind_of_response(self):
        owner = await self.client().sign_in()
        port = self.port
        replies = {
            'login page': await owner.req('GET', '/login', cookies={}),
            'home': await owner.req('GET', '/'),
            'settings': await owner.req('GET', '/settings'),
            'redirect': await owner.req('GET', '/settings', cookies={}),
            'login failure': await self.client().login(WRONG),
            '404': await owner.req('GET', '/nope'),
            '404 anonymous': await owner.req('GET', '/nope', cookies={}),
            '421': await owner.req('GET', '/', host='evil.example'),
            '403 origin': await owner.req('POST', '/logout', data={'csrf': owner.csrf}, origin='null'),
            '403 csrf': await owner.req('POST', '/logout', data={}),
            '413': await owner.req('POST', '/login', raw_body=b'a=' + b'x' * (300 * 1024)),
            'static': await owner.req('GET', '/static/base.css'),
            'static 404': await owner.req('GET', '/static/missing.js'),
            'method not allowed': await owner.req('PATCH', '/login', origin=NONE),
        }
        for label, reply in replies.items():
            with self.subTest(label):
                headers = reply.headers
                self.assertEqual(headers.get('Content-Security-Policy'), CSP)
                self.assertEqual(headers.get('X-Content-Type-Options'), 'nosniff')
                self.assertEqual(headers.get('X-Frame-Options'), 'DENY')
        for label in ('login page', 'home', 'settings', 'redirect', '404', '421', '403 csrf'):
            self.assertEqual(replies[label].headers.get('Cache-Control'), 'no-store', label)
        self.assertEqual(replies['login page'].status, 200)
        self.assertEqual(replies['413'].status, 413)
        self.assertEqual(replies['static'].status, 200)
        for label in ('login page', 'home', 'settings'):
            html = replies[label].text
            self.assertNotRegex(html, r'(?i)<script|<style|\sstyle=|\son[a-z]+=|javascript:', label)
            self.assertEqual(replies[label].headers['Content-Type'].split(';')[0], 'text/html')
        # The 413 is only a refusal: the Hub still answers and the session still works.
        self.assertEqual((await owner.req('GET', '/settings')).status, 200)

    async def test_referrer_policy_is_same_origin_so_form_posts_keep_their_origin(self):
        # SPEC correction 2026-10-25 (main ad9843c): `no-referrer` would turn same-origin form
        # POSTs into `Origin: null`, which the Origin check (rightly) refuses.
        owner = await self.client().sign_in()
        for path in ('/login', '/settings', '/nope', '/static/base.css'):
            with self.subTest(path):
                self.assertEqual((await owner.req('GET', path)).headers.get('Referrer-Policy'), 'same-origin')
        self.assertEqual((await owner.req('GET', '/', host='evil.example')).headers.get('Referrer-Policy'), 'same-origin')

    async def test_body_limit_applies_to_declared_and_chunked_bodies(self):
        port = self.port
        guest = self.client()
        token, _ = await guest.get_login_token()
        body = f'login_token={token}&password=' + 'x' * (300 * 1024)
        reply = await guest.req('POST', '/login', raw_body=body.encode(), cookies={LOGIN: token})
        self.assertEqual(reply.status, 413)
        async def chunks():
            for _ in range(40):
                yield b'x' * 32768
        reply = await guest.req('POST', '/login', raw_body=chunks(), cookies={LOGIN: token})
        self.assertEqual(reply.status, 413)
        request = (f'POST /login HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n'
                   'Content-Type: application/x-www-form-urlencoded\r\nContent-Length: 99999999999\r\nConnection: close\r\n\r\n').encode()
        self.assertEqual(raw_exchange(port, request)[0], 413)
        self.assertEqual((await guest.req('GET', '/login')).status, 200)

    async def test_static_cannot_read_outside_its_directory(self):
        port = self.port
        for target in ('/static/../../auth.json', '/static/%2e%2e/%2e%2e/auth.json', '/static/..%2f..%2fauth.json',
                       '/static//etc/passwd', '/static/%2fetc%2fpasswd', '/static/', '/static'):
            with self.subTest(target=target):
                status, blob = raw_exchange(port, f'GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n'.encode())
                self.assertNotIn(b'"kdf"', blob)
                self.assertNotIn(b'root:', blob)

    async def test_audit_rows_exist_and_never_hold_a_secret(self):
        guest = self.client()
        await guest.login(WRONG)
        owner = await self.client().sign_in()
        cookie = owner.session_cookie
        await owner.req('POST', '/logout', data={'csrf': owner.csrf})
        events = self.audit_events()
        self.assertEqual([e for e in events if e != 'password_changed'], ['login_failure', 'login_success', 'logout'])
        rows = {row[0]: row for row in self.audit()}
        self.assertEqual(rows['login_success'][1], 'owner')
        self.assertEqual(rows['login_failure'][2], '127.0.0.1')
        for row in self.audit():
            if row[0].startswith('login'):
                self.assertIn('delay_seconds', row[3])
        blob = b''.join(path.read_bytes() for path in self.box.state.glob('hub.sqlite3*'))
        log = self.box.hub_log(self.hub).encode()
        for label, secret in (('password', PASSWORD), ('wrong password', WRONG), ('session cookie', cookie)):
            self.assertFalse(secret.encode() in blob, f'{label} found in hub.sqlite3')
            self.assertFalse(secret.encode() in log, f'{label} found in the Hub log')
        self.assertFalse(owner.csrf.encode() in log, 'csrf token found in the Hub log')
        self.assertNotIn(PASSWORD.encode(), (self.box.state / 'auth.json').read_bytes())
        self.assertEqual(oct((self.box.state / 'hub.sqlite3').stat().st_mode & 0o777), '0o600')


# --- password change -------------------------------------------------------------------------------


class PasswordChangeTests(HubCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.open_hub()

    async def test_web_change_needs_the_current_password_and_ends_every_other_session(self):
        laptop = await self.client().sign_in()
        phone = await self.client(source='127.0.0.2').sign_in()
        form = {'csrf': laptop.csrf, 'new': NEW_PASSWORD, 'confirm': NEW_PASSWORD}
        refused = await laptop.req('POST', '/settings/password', data={**form, 'current': WRONG})
        self.assertEqual(refused.status, 403)
        self.assertIn('비밀번호가 맞지 않습니다', refused.text)
        self.assertEqual((await phone.req('GET', '/settings')).status, 200, 'a refused change ends nothing')
        self.assertEqual((await self.client(source='127.0.0.3').login(PASSWORD)).status, 303)
        self.assertIn('login_failure', self.audit_events(), 'a wrong current password is a login failure')
        for label, bad in {'mismatch': {'confirm': NEW_PASSWORD + 'x'}, 'short': {'new': 'short', 'confirm': 'short'},
                           'empty': {'new': '', 'confirm': ''}}.items():
            reply = await laptop.req('POST', '/settings/password', data={**form, 'current': PASSWORD, **bad})
            self.assertEqual(reply.status, 400, label)
        self.assertEqual((await self.client(source='127.0.0.3').login(PASSWORD)).status, 303, 'password unchanged so far')
        changed = await laptop.req('POST', '/settings/password', data={**form, 'current': PASSWORD})
        self.assertEqual((changed.status, changed.headers['Location']), (303, '/settings'))
        new_cookie, attributes = changed.cookies[SESSION]
        self.assertNotEqual(new_cookie, laptop.session_cookie)
        self.assertIn('HttpOnly', attributes)
        self.assertIn('SameSite=Strict', attributes)
        old = await phone.req('GET', '/settings')
        self.assertEqual((old.status, old.headers['Location']), (303, '/login'), 'other session is logged out')
        self.assertEqual((await laptop.req('GET', '/settings')).status, 303, 'the pre-change cookie of this browser is dead too')
        laptop.session_cookie = new_cookie
        self.assertEqual((await laptop.req('GET', '/settings')).status, 200, 'the reissued cookie of this browser lives')
        self.assertEqual((await self.client(source='127.0.0.4').login(PASSWORD)).status, 403, 'old password is gone')
        self.assertEqual((await self.client(source='127.0.0.5').login(NEW_PASSWORD)).status, 303)
        changes = [row for row in self.audit() if row[0] == 'password_changed' and '"web"' in row[3]]
        self.assertEqual(len(changes), 1, self.audit())
        self.assertIn('invalidated_sessions', changes[0][3])
        blob = b''.join(path.read_bytes() for path in self.box.state.glob('hub.sqlite3*'))
        for secret in (PASSWORD, NEW_PASSWORD):
            self.assertFalse(secret.encode() in blob, 'password found in hub.sqlite3')
            self.assertFalse(secret.encode() in self.box.hub_log(self.hub).encode(), 'password found in the Hub log')

    async def test_cli_passwd_ends_every_web_session(self):
        owner = await self.client().sign_in()
        self.assertEqual((await owner.req('GET', '/settings')).status, 200)
        self.box.set_password(NEW_PASSWORD)
        reply = await owner.req('GET', '/settings')
        self.assertEqual((reply.status, reply.headers['Location']), (303, '/login'))
        self.assertNotEqual((await owner.req('POST', '/logout', data={'csrf': owner.csrf})).status, 200)
        self.assertEqual((await self.client().login(NEW_PASSWORD)).status, 303)
        self.assertTrue(wait_until(lambda: 'password_changed' in self.audit_events(), 5.0), 'the CLI change is audited')
        self.assertEqual(tcp_listening(self.hub.pid)[0][1], self.port, 'no restart, same listener')


# --- login delay -----------------------------------------------------------------------------------


class ThrottleTests(HubCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.open_hub()

    async def attempt(self, client: Client, password: str, token: str = 'x' * 22) -> Reply:
        return await client.req('POST', '/login', data={'login_token': token, 'password': password}, cookies={LOGIN: token})

    async def test_repeated_failures_get_429_with_retry_after_at_once(self):
        attacker = self.client(source='127.0.0.3')
        codes = []
        for _ in range(4):
            codes.append((await self.attempt(attacker, WRONG)).status)
        self.assertEqual(codes, [403, 403, 403, 403], 'f(n) = 0 up to the third failure; the fourth is still verified')
        started = time.monotonic()
        limited = await self.attempt(attacker, PASSWORD)  # even the right password waits
        self.assertEqual(limited.status, 429)
        self.assertLess(time.monotonic() - started, 1.0, 'the wait is announced, never slept')
        retry = int(limited.headers['Retry-After'])
        self.assertTrue(1 <= retry <= 30, retry)
        self.assertNotIn(SESSION, limited.cookies)
        self.assertEqual(limited.headers['Content-Security-Policy'], CSP)
        delays = [json.loads(row[3]).get('delay_seconds') for row in self.audit() if row[0] == 'login_failure']
        self.assertEqual(delays[:3], [0, 0, 0])
        self.assertEqual(delays[3], 0)
        time.sleep(retry + 0.5)
        self.assertEqual((await self.attempt(attacker, WRONG)).status, 403)
        again = await self.attempt(attacker, PASSWORD)
        self.assertEqual(again.status, 429)
        self.assertGreater(int(again.headers['Retry-After']), 0)
        self.assertGreaterEqual(int(again.headers['Retry-After']) + 1, retry, 'the delay grows (1 s, 2 s, ...)')

    async def test_a_flood_of_bad_logins_neither_blocks_the_loop_nor_locks_out_the_owner(self):
        attacker = self.client(source='127.0.0.3')
        bystander = self.client(source='127.0.0.4')
        owner = self.client(source='127.0.0.2')
        probes: list[float] = []

        async def watch():
            while True:
                probes.append((await bystander.req('GET', '/login')).elapsed)
                await asyncio.sleep(0.02)

        watcher = asyncio.create_task(watch())
        try:
            replies = await asyncio.gather(*(self.attempt(attacker, f'{WRONG}{index}') for index in range(40)))
        finally:
            watcher.cancel()
        self.assertEqual({reply.status for reply in replies} - {403, 429}, set())
        self.assertLessEqual(sum(reply.status == 403 for reply in replies), 8, 'one address cannot keep guessing')
        self.assertLess(max(reply.elapsed for reply in replies), 5.0, 'no reply waited in a sleeping lock')
        self.assertTrue(probes)
        self.assertLess(max(probes), 1.0, f'a page request stayed fast while the flood ran: {max(probes):.2f}s')
        started = time.monotonic()
        reply = await owner.login(PASSWORD)
        self.assertEqual(reply.status, 303, 'the owner at another address is not locked out by one attacker address')
        self.assertLess(time.monotonic() - started, 3.0)
        print(f'[measure] flood: {sum(r.status == 403 for r in replies)}x403 {sum(r.status == 429 for r in replies)}x429, '
              f'slowest flood reply {max(r.elapsed for r in replies):.2f}s, slowest concurrent page {max(probes):.3f}s '
              f'({len(probes)} probes), owner login {time.monotonic() - started:.2f}s', file=sys.stderr)

    async def test_distributed_failures_delay_everyone_within_the_spec_cap_and_the_owner_gets_in_afterwards(self):
        for index in range(4):  # 4 addresses x 3 failures = 12 global; f(12 // 3) = f(4) = 1 s
            attacker = self.client(source=f'127.0.0.{10 + index}')
            for _ in range(3):
                self.assertEqual((await self.attempt(attacker, WRONG)).status, 403)
        owner = self.client(source='127.0.0.2')
        started = time.monotonic()
        first = await self.attempt(owner, PASSWORD)
        self.assertLess(time.monotonic() - started, 1.0, 'immediate answer, not a queue')
        self.assertEqual(first.status, 429)
        retry = int(first.headers['Retry-After'])
        self.assertTrue(1 <= retry <= 30, retry)
        time.sleep(retry + 0.5)
        self.assertEqual((await owner.login(PASSWORD)).status, 303)
        self.assertEqual((await self.client(source='127.0.0.12').login(PASSWORD)).status, 303,
                         'a success resets the counters')

    async def test_the_throttle_survives_a_hub_restart(self):
        attacker = self.client(source='127.0.0.3')
        for _ in range(4):
            await self.attempt(attacker, WRONG)
        self.assertEqual((await self.attempt(attacker, PASSWORD)).status, 429)
        self.box.stop_hub(self.hub)
        self.hub = self.box.start_hub()
        _host, port = self.box.wait_listening(self.hub.pid)
        self.port = port
        fresh = Client(port, source='127.0.0.3')
        self.clients.append(fresh)
        self.assertEqual((await self.attempt(fresh, PASSWORD)).status, 429)


if __name__ == '__main__':
    unittest.main()
