"""WBX-B3 independent adversarial verification of ``/skill/`` (seams S1, S3 subset, S5 subset).

Expected values come from the web-board-ux SPEC "`/skill/` 제공, packaging, `skill_repo_url`", the Cache table row for
``/skill/…``, WBX-D10/D19 and the SPEC Host rule, never from ``pages/skill.py``:

* the Hub address placed in the files is the request's ``Host`` value that passed the Host check, and nothing else
  (spoofed Host values, forwarded headers, absolute-form targets, IPv6 literals, ports, https);
* exactly four paths are served; no request path can make the server open any other file (``canonical_path`` is
  spied on and a tree holding ``.env`` files, a symlink and a sibling secret is mounted as the asset root);
* no key or secret is served, none is echoed back from request headers;
* ``skill_repo_url`` only ever shows up as one README line, whatever characters the (valid) value holds;
* ``allow_from``, ``[web] enabled = false`` and the internal Board listener on a real Hub;
* a wheel built straight from a source tree holding ``.env`` files carries the four files and no ``.env``.

Run with ``discover -s tests/hub -p 'test_web_skill*.py'``.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aiohttp import ClientSession, DummyCookieJar, test_utils  # noqa: E402

from workbench.hub import auth, config, server  # noqa: E402
from workbench.hub.paths import HubLayout  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402
from workbench.hub.web.pages import skill as skill_page  # noqa: E402
from fixtures.web_sandbox import PASSWORD, WebSandbox, tcp_listening  # noqa: E402

PLACEHOLDER = 'http://YOUR-HUB-HOST:8780'
MARKER = '<!-- wb:skill-repo -->'
SKILLS = REPO / 'skills'
NAMES = ('README.md', 'wb-inbox/SKILL.md', 'wb-inbox/wb_inbox.py', 'wb-inbox/.env.example')
INSTALLED = {f'skills/{name}' for name in NAMES}
SECRETS = ('SECRET_ENV_VALUE', 'OUTSIDE_SECRET', 'TMP_PYPROJECT_SECRET')
# a Host value the server may legitimately echo into a file: DNS name or IP literal, optional port, nothing else
SAFE_HOST = re.compile(r'^(?:[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$')


def expected_bytes(name: str, origin: str, repo_url: str = '') -> bytes:
    """What SPEC says the file must be: the repo file with the address (and the README marker line) replaced."""
    data = (SKILLS / name).read_bytes()
    if name.endswith('.py'):
        return data
    data = data.replace(PLACEHOLDER.encode(), origin.encode())
    if name == 'README.md':
        data = data.replace(MARKER.encode(), (f'Source repository: {repo_url}' if repo_url else '').encode())
    return data


def raw(port: int, request: bytes, *, host: str = '127.0.0.1', timeout: float = 5.0):
    """One request on a fresh socket, written exactly as given. -> (status | None, [(name, value)], body)."""
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.sendall(request)
        chunks = []
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except (socket.timeout, ConnectionResetError):
            pass
    data = b''.join(chunks)
    head, _, body = data.partition(b'\r\n\r\n')
    lines = head.split(b'\r\n')
    if not lines or not lines[0].startswith(b'HTTP/'):
        return None, [], data
    status = int(lines[0].split()[1])
    headers = [tuple(line.decode('latin-1').split(':', 1)) for line in lines[1:] if b':' in line]
    return status, [(n.strip().lower(), v.strip()) for n, v in headers], body


def request_bytes(path: bytes | str, host: bytes | str | None, *extra: bytes | str, method: str = 'GET',
                  version: str = 'HTTP/1.1') -> bytes:
    def b(value):
        return value if isinstance(value, bytes) else value.encode('latin-1')
    lines = [b(f'{method} ') + b(path) + b(f' {version}')]
    if host is not None:
        lines.append(b'Host: ' + b(host))
    lines += [b(line) for line in extra]
    lines.append(b'Connection: close')
    return b'\r\n'.join(lines) + b'\r\n\r\n'


def header(headers, name: str) -> list[str]:
    return [value for key, value in headers if key == name.lower()]


class SkillCase(unittest.IsolatedAsyncioTestCase):
    async def serve(self, *, tls: bool = False, server_names: tuple[str, ...] = (), allow_from: tuple = (),
                    skill_repo_url: str = '', bind: str = '127.0.0.1'):
        self.base_dir = Path(tempfile.mkdtemp(prefix='wb-skill-ind-')).resolve()
        self.addCleanup(shutil.rmtree, self.base_dir, True)
        state = self.base_dir / 'state'
        state.mkdir(mode=0o700)
        self.passwords = auth.PasswordStore(state)
        self.generation = self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(state)
        self.addCleanup(self.db.close)
        self.services = WebServices(self.passwords, self.db,
                                    OriginPolicy(bind, tls, server_names, allow_from=allow_from,
                                                 skill_repo_url=skill_repo_url))
        self.hub = server.Hub(HubLayout(self.base_dir / 'hub'), 'detached')
        self.addCleanup(self.hub.inbox.close)
        self.app = create_app(self.services, routes=self.hub.web_routes())
        self.server = test_utils.TestServer(self.app, host=bind, port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.port = self.server.port
        self.bind = bind
        self.http = ClientSession(cookie_jar=DummyCookieJar())
        self.addAsyncCleanup(self.http.close)

    async def raw(self, request: bytes):
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: raw(self.port, request, host=self.bind))

    def spy_on_assets(self, root: Path | None = None) -> list[str]:
        """Record every installed path the page asks the resolver for; optionally mount ``root`` as the asset root."""
        calls: list[str] = []
        real = skill_page.canonical_path

        def spy(relative, *args, **kwargs):
            calls.append(relative)
            return real(relative, *args, **kwargs)
        patcher = mock.patch.object(skill_page, 'canonical_path', spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        if root is not None:
            mounted = mock.patch('workbench.assets.runtime_root', return_value=root)
            mounted.start()
            self.addCleanup(mounted.stop)
        return calls


class HostMixin:
    """The address in the files is the Host value that passed the Host check; nothing else may reach them."""

    HOSTILE = (
        'evil.example', 'evil.example:{p}', '127.0.0.1', '127.0.0.1:{q}', '127.0.0.1:0{p}', '127.0.0.1:{p}@evil.example',
        'evil.example@127.0.0.1:{p}', '127.0.0.1:{p}/evil', '127.0.0.1:{p} evil.example', '127.0.0.1:{p},evil.example',
        '127.0.0.1:{p}\tx', '127.0.0.1.:{p}', '0x7f.0.0.1:{p}', '2130706433:{p}', '127.1:{p}', '127.000.000.001:{p}',
        '[::1]:{p}', 'localhost.:{p}', 'localhost:{p}@evil.example', 'localhost:{p}:{p}', '127.0.0.1:{p}<script>',
        '127.0.0.1:{p}%0d%0aX-Evil:1', '127.0.0.1:{p}\\evil.example', '*', '', ' ', '127.0.0.1:{p}#evil',
        '127.0.0.1:{p}?evil', 'evil.example:{p}.127.0.0.1', 'LOCALHOST.evil.example:{p}', 'localhost:+{p}',
        'localhost:{p}.0', 'localhost:-{p}', 'null',
    )

    async def assert_host(self, host: bytes, allowed: set[str], *, path='/skill/wb-inbox/.env.example'):
        """Property: 200 only for an allowed Host and then with exactly that Host as the address; otherwise nothing
        of the file (or of the hostile value) is in the answer."""
        status, headers, body = await self.raw(request_bytes(path, host))
        text = body.decode('latin-1')
        if status == 200:
            shown = host.decode('latin-1')
            self.assertIn(shown.lower(), allowed, f'served for a Host the listener must refuse: {host!r}')
            self.assertRegex(shown, SAFE_HOST, f'unsafe characters would land in the file: {host!r}')
            self.assertIn(f'WB_HUB_URL=http://{shown}\n', text)
            self.assertNotIn('YOUR-HUB-HOST', text)
            self.assertEqual(text.count('http://'), 1)
        else:
            self.assertIn(status, (421, 400, None), f'{host!r} -> {status}')
            self.assertNotIn('WB_HUB_URL', text)
            self.assertNotIn('wbk_replace_me', text)
            hostile = host.decode('latin-1').strip()
            if hostile and 'evil' in hostile and status == 421:  # (a 400 is aiohttp's own parser text, not this feature)
                self.assertNotIn('evil', text.lower())
        return status, body

    async def test_hostile_host_values_are_refused_and_never_reflected(self):
        await self.serve()
        p = self.port
        allowed = {f'127.0.0.1:{p}', f'localhost:{p}'}
        for template in self.HOSTILE:
            host = template.format(p=p, q=p + 1).encode('latin-1')
            with self.subTest(host=host):
                await self.assert_host(host, allowed)
        # the two legitimate ones really are served (the property above is not vacuous)
        for host in allowed:
            status, body = await self.assert_host(host.encode(), allowed)
            self.assertEqual(status, 200)

    async def test_non_ascii_and_control_bytes_in_host_never_crash_the_page(self):
        await self.serve()
        p = self.port
        allowed = {f'127.0.0.1:{p}', f'localhost:{p}'}
        for host in (f'loc\xe4lhost:{p}'.encode('latin-1'), f'localhost:{p}'.encode() + b'\x00', b'local\x7fhost:%d' % p,
                     'ex\u00e4mple.com'.encode('utf-8') + b':%d' % p, f'localhost:{p}'.encode() + '\u202e'.encode('utf-8'),
                     b'\xff\xfe', b'localhost:%d\x0b' % p, b'localhost:%d\x1b[2J' % p):
            with self.subTest(host=host):
                status, body = await self.assert_host(host, allowed)
                self.assertNotEqual(status, 500)
                self.assertNotIn(b'Traceback', body)

    async def test_missing_or_repeated_host_is_refused(self):
        await self.serve()
        p = self.port
        for label, request in (
                ('http/1.0 without Host', request_bytes('/skill/README.md', None, version='HTTP/1.0')),
                ('http/1.1 without Host', request_bytes('/skill/README.md', None)),
                ('two Host headers, same', request_bytes('/skill/README.md', f'127.0.0.1:{p}', f'Host: 127.0.0.1:{p}')),
                ('two Host headers, one hostile', request_bytes('/skill/README.md', f'127.0.0.1:{p}', 'Host: evil.example')),
                ('hostile first', request_bytes('/skill/README.md', 'evil.example', f'Host: 127.0.0.1:{p}')),
                ('folded Host', request_bytes('/skill/README.md', f'127.0.0.1:{p}', ' evil.example'))):
            with self.subTest(label):
                status, _, body = await self.raw(request)
                self.assertIn(status, (421, 400, None))
                self.assertNotIn(b'Workbench', body)
                if status == 421:
                    self.assertNotIn(b'evil', body.lower())

    async def test_forwarded_and_absolute_form_hints_never_choose_the_address(self):
        await self.serve()
        host = f'127.0.0.1:{self.port}'
        for extra in ('X-Forwarded-Host: evil.example', 'X-Forwarded-Host: evil.example:80', 'X-Host: evil.example',
                      'Forwarded: host=evil.example;proto=https', 'X-Forwarded-Server: evil.example',
                      'X-Original-Host: evil.example', 'X-Forwarded-Proto: https', 'X-Forwarded-Port: 443',
                      'X-Forwarded-For: 203.0.113.9', 'Origin: https://evil.example', 'Referer: https://evil.example/',
                      'X-HTTP-Method-Override: POST', 'Front-End-Https: on', 'X-Forwarded-Ssl: on'):
            with self.subTest(extra=extra):
                status, headers, body = await self.raw(request_bytes('/skill/wb-inbox/.env.example', host, extra))
                self.assertEqual(status, 200)
                self.assertEqual(body, expected_bytes('wb-inbox/.env.example', f'http://{host}'))
        # absolute-form request target: the Host header (checked) still decides, the URI authority never does
        status, headers, body = await self.raw(request_bytes('http://evil.example/skill/README.md', host))
        self.assertNotIn(b'evil.example', body)
        if status == 200:
            self.assertEqual(body, expected_bytes('README.md', f'http://{host}'))

    async def test_server_names_are_the_only_extra_names_and_the_name_is_echoed_verbatim(self):
        await self.serve(server_names=('hub.example',))
        p = self.port
        allowed = {f'127.0.0.1:{p}', f'localhost:{p}', f'hub.example:{p}'}
        for template in ('hub.example:{p}', 'HUB.EXAMPLE:{p}', 'Hub.Example:{p}', 'hub.example:{q}', 'hub.example',
                         'hub.example.:{p}', 'xhub.example:{p}', 'hub.example.evil:{p}', 'evil.hub.example:{p}',
                         'hub.example:{p}@evil.example', 'hub_example:{p}', 'hub.example:80', 'hub.example:{p}.',
                         'sub.hub.example:{p}', '.hub.example:{p}'):
            host = template.format(p=p, q=p + 1).encode()
            with self.subTest(host=host):
                await self.assert_host(host, allowed)
        status, body = await self.assert_host(f'hub.example:{p}'.encode(), allowed)
        self.assertEqual(status, 200)
        self.assertEqual(body, expected_bytes('wb-inbox/.env.example', f'http://hub.example:{p}'))

    async def test_https_listener_fills_https_and_a_forwarded_proto_cannot_downgrade_it(self):
        await self.serve(tls=True)
        host = f'127.0.0.1:{self.port}'
        for name in NAMES:
            with self.subTest(name=name):
                status, _, body = await self.raw(request_bytes(f'/skill/{name}', host, 'X-Forwarded-Proto: http'))
                self.assertEqual(status, 200)
                self.assertEqual(body, expected_bytes(name, f'https://{host}'))
                if not name.endswith('.py'):
                    self.assertNotIn(b'http://127.0.0.1', body)
        # https default port 443 is implied by a Host without a port: the listener is not on 443
        status, _, body = await self.raw(request_bytes('/skill/README.md', '127.0.0.1'))
        self.assertEqual(status, 421)
        self.assertEqual(body, b'misdirected_request')

    async def test_http_listener_never_claims_https(self):
        await self.serve()
        host = f'127.0.0.1:{self.port}'
        status, _, body = await self.raw(request_bytes('/skill/README.md', host, 'X-Forwarded-Proto: https'))
        self.assertEqual(status, 200)
        self.assertNotIn(b'https://' + host.encode(), body)
        self.assertIn(f'WB_HUB_URL=http://{host}'.encode(), body)

    async def test_wildcard_bind_takes_only_ip_literals_with_the_listening_port(self):
        await self.serve(bind='0.0.0.0')
        p = self.port
        status, _, body = await self.raw(request_bytes('/skill/wb-inbox/.env.example', f'10.1.2.3:{p}'), )
        self.assertEqual(status, 200)  # SPEC Host rule: an unspecified bind takes every IP literal
        self.assertEqual(body, expected_bytes('wb-inbox/.env.example', f'http://10.1.2.3:{p}'))
        for host in ('evil.example:%d' % p, '10.1.2.3', '10.1.2.3:%d' % (p + 1), '10.1.2.3:%d@evil.example' % p):
            with self.subTest(host=host):
                status, _, body = await self.raw(request_bytes('/skill/wb-inbox/.env.example', host))
                self.assertEqual(status, 421)
                self.assertNotIn(b'WB_HUB_URL', body)


class HostValueTests(HostMixin, SkillCase):
    pass


@unittest.skipUnless(socket.has_ipv6, 'no IPv6 in this environment')
class Ipv6HostTests(HostMixin, SkillCase):
    HOSTILE = ()

    async def test_ipv6_literal_host_is_echoed_in_brackets_with_the_port(self):
        await self.serve(bind='::1')
        p = self.port
        status, _, body = await self.raw(request_bytes('/skill/wb-inbox/.env.example', f'[::1]:{p}'))
        self.assertEqual(status, 200)
        self.assertEqual(body, expected_bytes('wb-inbox/.env.example', f'http://[::1]:{p}'))
        status, _, body = await self.raw(request_bytes('/skill/README.md', f'[::1]:{p}'))
        self.assertIn(f'WB_HUB_URL=http://[::1]:{p}\n'.encode(), body)
        allowed = {f'[::1]:{p}', f'localhost:{p}', f'[0:0:0:0:0:0:0:1]:{p}', f'[0000:0:0:0:0:0:0:0001]:{p}'}
        for host in (f'[::1]', f'[::1]:{p + 1}', f'[::1%25lo]:{p}', f'[::1%lo]:{p}', f'::1:{p}', f'[::1]:{p}x', f'[::1:{p}',
                     f'[::1]{p}', f'[::1]:{p}@evil.example', f'[::ffff:127.0.0.1]:{p}', f'127.0.0.1:{p}', f'[::2]:{p}',
                     f'[evil]:{p}', f'[::1]:{p}/x', f'[[::1]]:{p}', f'[::1]:{p}:{p}', f'[]:{p}', f'[::g]:{p}',
                     f'[0:0:0:0:0:0:0:1]:{p}', f'[0000:0:0:0:0:0:0:0001]:{p}', f'localhost:{p}'):
            with self.subTest(host=host):
                await self.assert_host(host.encode(), allowed)


class ServedContentTests(SkillCase):
    async def test_every_file_is_the_repo_file_with_only_the_two_documented_substitutions(self):
        await self.serve(skill_repo_url='https://example.org/org/skills')
        host = f'127.0.0.1:{self.port}'
        for name in NAMES:
            with self.subTest(name=name):
                status, headers, body = await self.raw(request_bytes(f'/skill/{name}', host))
                self.assertEqual(status, 200)
                self.assertEqual(body, expected_bytes(name, f'http://{host}', 'https://example.org/org/skills'))
        # the substitution is real (guards the helper against being the identity)
        self.assertIn(PLACEHOLDER.encode(), (SKILLS / 'README.md').read_bytes())
        self.assertIn(MARKER.encode(), (SKILLS / 'README.md').read_bytes())

    async def test_python_client_is_byte_identical_even_if_it_contained_the_placeholder_or_marker(self):
        await self.serve(skill_repo_url='https://example.org/r')
        original = (SKILLS / 'wb-inbox' / 'wb_inbox.py').read_bytes()
        self.assertNotIn(PLACEHOLDER.encode(), original, 'precondition: wb_inbox.py has no placeholder today')
        root = self.base_dir / 'assets'
        shutil.copytree(SKILLS, root / 'skills')
        tainted = original + f'\n# {PLACEHOLDER} {MARKER}\n'.encode()
        (root / 'skills' / 'wb-inbox' / 'wb_inbox.py').write_bytes(tainted)
        self.spy_on_assets(root)
        status, _, body = await self.raw(request_bytes('/skill/wb-inbox/wb_inbox.py', f'127.0.0.1:{self.port}'))
        self.assertEqual((status, body), (200, tainted))

    async def test_line_count_is_preserved_and_the_marker_line_is_the_only_changed_line_besides_the_address(self):
        await self.serve(skill_repo_url='https://example.org/r')
        host = f'127.0.0.1:{self.port}'
        _, _, body = await self.raw(request_bytes('/skill/README.md', host))
        served = body.decode().split('\n')
        original = (SKILLS / 'README.md').read_text().split('\n')
        self.assertEqual(len(served), len(original))
        for index, (new, old) in enumerate(zip(served, original)):
            if old == MARKER:
                self.assertEqual(new, 'Source repository: https://example.org/r')
            else:
                self.assertEqual(new, old.replace(PLACEHOLDER, f'http://{host}'), f'line {index}')

    async def test_no_key_or_secret_is_in_any_file_and_request_credentials_are_not_echoed(self):
        await self.serve(skill_repo_url='https://example.org/r')
        issued = self.services.api_keys.issue('skill-ind', 'requester', {'all': True}, None, now=1.0)
        session = self.services.sessions.create('127.0.0.1', 'ind', time.time(), generation=self.generation)
        host = f'127.0.0.1:{self.port}'
        extras = (f'Authorization: Bearer {issued.raw}', f'X-Api-Key: {issued.raw}', f'Cookie: wb_hub_session={session.token}',
                  f'Cookie: wb_hub_session={session.token}; wb_key={issued.raw}')
        for name in NAMES:
            plain_status, plain_headers, plain = await self.raw(request_bytes(f'/skill/{name}', host))
            for extra in extras:
                with self.subTest(name=name, extra=extra.split(':')[0]):
                    status, headers, body = await self.raw(request_bytes(f'/skill/{name}', host, extra))
                    self.assertEqual(status, 200)
                    self.assertEqual(body, plain, 'public route: credentials change nothing')
                    blob = (b'\n'.join(f'{k}: {v}'.encode() for k, v in headers) + body)
                    self.assertNotIn(issued.raw.encode(), blob)
                    self.assertNotIn(issued.key_id.encode(), blob)
                    self.assertNotIn(session.token.encode(), blob)
                    self.assertNotIn(session.csrf.encode(), blob)
                    self.assertEqual(header(headers, 'set-cookie'), [])
            self.assertEqual(plain_status, 200)
            # what the files hold as a key is only the documented placeholder
            text = plain.decode()
            self.assertEqual([m for m in re.findall(r'wbk_[A-Za-z0-9_\-]{8,}', text) if m != 'wbk_replace_me'], [])

    async def test_head_has_the_get_headers_and_no_body_and_other_methods_serve_nothing(self):
        await self.serve()
        host = f'127.0.0.1:{self.port}'
        for name in NAMES:
            with self.subTest(name=name):
                g_status, g_headers, g_body = await self.raw(request_bytes(f'/skill/{name}', host))
                h_status, h_headers, h_body = await self.raw(request_bytes(f'/skill/{name}', host, method='HEAD'))
                self.assertEqual((g_status, h_status, h_body), (200, 200, b''))
                self.assertEqual(header(h_headers, 'content-length'), [str(len(g_body))])
                for key in ('content-type', 'cache-control', 'content-disposition', 'x-content-type-options'):
                    self.assertEqual(header(h_headers, key), header(g_headers, key), key)
        for method in ('POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'TRACE', 'CONNECT', 'PROPFIND'):
            with self.subTest(method=method):
                status, headers, body = await self.raw(request_bytes(
                    '/skill/README.md', host, f'Origin: http://{host}', 'Content-Length: 0', method=method))
                self.assertNotEqual(status, 200)
                self.assertNotIn(b'Workbench', body)
                self.assertEqual(header(headers, 'set-cookie'), [])

    async def test_range_and_conditional_requests_cannot_make_a_partial_or_stale_answer(self):
        await self.serve()
        host = f'127.0.0.1:{self.port}'
        full = expected_bytes('README.md', f'http://{host}')
        for extra in ('Range: bytes=0-9', 'If-None-Match: *', 'If-Modified-Since: Wed, 21 Oct 2037 07:28:00 GMT',
                      'If-Range: x', 'Accept-Encoding: gzip, br'):
            with self.subTest(extra=extra):
                status, headers, body = await self.raw(request_bytes('/skill/README.md', host, extra))
                self.assertEqual((status, body), (200, full))
                self.assertEqual(header(headers, 'content-encoding'), [])

    async def test_headers_text_plain_inline_nosniff_no_cache_and_a_404_is_revalidated_too(self):
        await self.serve()
        host = f'127.0.0.1:{self.port}'
        for path in [f'/skill/{n}' for n in NAMES] + ['/skill/nothing', '/skill', '/skill/']:
            with self.subTest(path=path):
                status, headers, body = await self.raw(request_bytes(path, host))
                self.assertEqual(header(headers, 'cache-control'), ['no-cache'])
                self.assertEqual(header(headers, 'x-content-type-options'), ['nosniff'])
                self.assertEqual(header(headers, 'content-type'), ['text/plain; charset=utf-8'])
                self.assertEqual(header(headers, 'set-cookie'), [])
                if status == 200:
                    self.assertEqual(header(headers, 'content-disposition'), ['inline'])
                    self.assertEqual(len(header(headers, 'content-security-policy')), 1)
                    for forbidden in ('etag', 'last-modified', 'expires', 'pragma', 'location'):
                        self.assertEqual(header(headers, forbidden), [], forbidden)


class PathTests(SkillCase):
    """Nothing but the fixed list: no request path reaches any other file."""

    HOSTILE_PATHS = (
        '/skill', '/skill/', '/skill/.env', '/skill/wb-inbox/.env', '/skill/wb-inbox/.env.local', '/skill/wb-inbox/link.md',
        '/skill/secret.txt', '/skill/wb-inbox/.env.example/', '/skill/wb-inbox/.env.example/.',
        '/skill/wb-inbox/.env.example.', '/skill/wb-inbox/.env.example%20', '/skill/wb-inbox/.env.example%00',
        '/skill/wb-inbox/.env.example%00.md', '/skill/wb-inbox/.env%00.example', '/skill/wb-inbox/%2eenv',
        '/skill/wb-inbox/%2Eenv', '/skill/wb-inbox/.en%76', '/skill/wb-inbox/%2e%65nv', '/skill/%2e%2e/pyproject.toml',
        '/skill/%2e%2e%2fpyproject.toml', '/skill/..%2fpyproject.toml', '/skill/..%5cpyproject.toml',
        '/skill/%252e%252e/pyproject.toml', '/skill/wb-inbox/../../pyproject.toml', '/skill/../pyproject.toml',
        '/skill/wb-inbox/%2e%2e/%2e%2e/pyproject.toml', '/skill/wb-inbox/..%2f..%2fpyproject.toml',
        '/skill/wb-inbox/..%252f..%252fpyproject.toml', '/skill//pyproject.toml', '/skill/wb-inbox//..//..//pyproject.toml',
        '/skill/\\README.md', '/skill/wb-inbox\\SKILL.md', '/skill/wb-inbox/SKILL.md\\',
        '/skill/README.md%00', '/skill/README.md%00.txt', '/skill/README.md%2f',
        '/skill/README.md/', '/skill/README.md/..', '/skill/README.md/.', '/skill/readme.md', '/skill/README.MD',
        '/skill/%C0%AE%C0%AE/pyproject.toml', '/skill/%u002e%u002e/pyproject.toml',
        '/skill/%EF%BC%B2EADME.md', '/skill/README.md%E2%80%8B', '/skill/wb-inbox', '/skill/wb-inbox/',
        '/skill/wb-inbox/wb_inbox.py.orig', '/skill/wb-inbox/wb_inbox.pyc', '/skill/wb-inbox/__pycache__/x.pyc',
        '/skill/README.md.bak', '/skill/README.md~', '/skill/wb-inbox/SKILL.md.swp', '/skill/wb-inbox/.git/config',
        '/skill/.git/config', '/skill/<script>alert(1)</script>', '/skill/' + 'a' * 5000, '/skill/' + '../' * 40 + 'etc/passwd',
        '/skill/wb-inbox/SKILL.md;.env', '/skill/wb-inbox/SKILL.md;x=1', '/skill/.env;/../README.md',
        '/skill/other/SKILL.md', '/skill/wb-inbox/SKILL.md/x', '/skill/wb-inbox/SKILL.md/../.env',
        '/skill/wb-inbox/./.env', '/skill/./.env', '/skill/*', '/skill/?', '/skill/{tail}', '/skill/{tail:.*}',
    )

    def mount_tree(self) -> Path:
        """A copy of the installed layout that also holds secrets in every place a bad path could reach."""
        root = self.base_dir / 'assets'
        shutil.copytree(SKILLS, root / 'skills')
        for target in (root / 'skills' / '.env', root / 'skills' / 'wb-inbox' / '.env',
                       root / 'skills' / 'wb-inbox' / '.env.local', root / 'skills' / 'secret.txt'):
            target.write_text('WB_API_KEY=wbk_SECRET_ENV_VALUE\n')
        (root / 'pyproject.toml').write_text('# TMP_PYPROJECT_SECRET\n')
        outside = self.base_dir / 'outside.txt'
        outside.write_text('OUTSIDE_SECRET\n')
        (root / 'skills' / 'wb-inbox' / 'link.md').symlink_to(outside)
        (root / 'skills' / 'wb-inbox' / '__pycache__').mkdir()
        (root / 'skills' / 'wb-inbox' / '__pycache__' / 'x.pyc').write_text('SECRET_ENV_VALUE')
        return root

    async def test_no_request_path_opens_anything_but_the_four_installed_files(self):
        await self.serve()
        calls = self.spy_on_assets(self.mount_tree())
        host = f'127.0.0.1:{self.port}'
        expected_by_path = {f'/skill/{n}': expected_bytes(n, f'http://{host}') for n in NAMES}
        # (guard) the mounted tree is the one in use: a legitimate path is served from it
        status, _, body = await self.raw(request_bytes('/skill/README.md', host))
        self.assertEqual((status, body), (200, expected_by_path['/skill/README.md']))
        calls.clear()
        for path in self.HOSTILE_PATHS:
            with self.subTest(path=path[:80]):
                before = len(calls)
                status, headers, body = await self.raw(request_bytes(path, host))
                text = body.decode('latin-1')
                for secret in SECRETS:
                    self.assertNotIn(secret, text)
                self.assertNotIn('root:x:', text)
                self.assertTrue(set(calls[before:]) <= INSTALLED, calls[before:])
                if status == 200:
                    # only possible when the client-side/yarl dot-segment normalisation names a listed path exactly
                    self.assertIn(body, expected_by_path.values(), f'{path} answered 200 with unknown content')
                else:
                    self.assertIn(status, (404, 400, None), f'{path} -> {status}')
                    if status == 404:
                        self.assertEqual(body, b'not_found')  # no echo of the path, no directory listing
                    self.assertEqual(header(headers, 'location'), [])
                    self.assertEqual(header(headers, 'set-cookie'), [])

    async def test_other_skill_paths_holding_an_encoded_newline_are_404_too(self):
        # SPEC: "그 밖의 /skill/…은 404". A `{tail:.*}` pattern does not match "\n" (regex dot), so check explicitly.
        await self.serve()
        host = f'127.0.0.1:{self.port}'
        for path in ('/skill/%0a', '/skill/a%0ab', '/skill/README.md%0a', '/skill/wb-inbox/SKILL.md%0d%0a',
                     '/skill/README.md%0d%0aX:y', '/skill/wb-inbox/%0a.env', '/skill/%0a/README.md'):
            with self.subTest(path=path):
                status, headers, body = await self.raw(request_bytes(path, host))
                self.assertEqual((status, body), (404, b'not_found'))
                self.assertEqual(header(headers, 'location'), [])

    async def test_outside_the_lowercase_skill_prefix_nothing_of_the_skill_is_served(self):
        await self.serve()
        host = f'127.0.0.1:{self.port}'
        for path in ('/Skill/README.md', '/SKILL/README.md', '/skill\\README.md', '/skills/README.md', '/_skill/README.md'):
            with self.subTest(path=path):
                status, headers, body = await self.raw(request_bytes(path, host))
                self.assertIn(status, (303, 404))
                if status == 303:
                    self.assertEqual(header(headers, 'location'), ['/login'])
                self.assertNotIn(b'Workbench', body)

    async def test_the_four_files_are_the_only_200s_under_a_wide_sweep_of_names(self):
        await self.serve()
        host = f'127.0.0.1:{self.port}'
        calls = self.spy_on_assets()
        candidates = {f'/skill/{n}' for n in NAMES}
        wide = [f'/skill/{folder}{name}{suffix}' for folder in ('', 'wb-inbox/', 'wb_inbox/', 'wb-inbox/wb-inbox/')
                for name in ('README.md', 'SKILL.md', 'wb_inbox.py', '.env.example', '.env', 'readme.md', 'LICENSE', 'index.html',
                             'index', 'pyproject.toml', 'client.env')
                for suffix in ('', '/', '.txt', '%20')]
        for path in wide:
            status, _, body = await self.raw(request_bytes(path, host))
            with self.subTest(path=path):
                self.assertEqual(status == 200, path in candidates, f'{path} -> {status}')
        self.assertTrue(set(calls) <= INSTALLED, set(calls) - INSTALLED)
        self.assertEqual(sorted(set(calls)), sorted(INSTALLED))

    async def test_query_strings_do_not_change_what_is_served_and_cannot_pick_a_file(self):
        await self.serve()
        host = f'127.0.0.1:{self.port}'
        calls = self.spy_on_assets()
        full = expected_bytes('README.md', f'http://{host}')
        for query in ('?x=1', '?file=.env', '?path=../pyproject.toml', '?a=%00', '?host=evil.example', '?WB_HUB_URL=evil',
                      '?name=wb-inbox/.env', '?repo=https://evil.example'):
            with self.subTest(query=query):
                status, _, body = await self.raw(request_bytes('/skill/README.md' + query, host))
                self.assertEqual((status, body), (200, full))
        self.assertEqual(set(calls), {'skills/README.md'})

    async def test_an_unreadable_installed_file_is_a_bare_404_that_names_no_path(self):
        await self.serve()
        host = f'127.0.0.1:{self.port}'
        for error in (FileNotFoundError('/secret/dir/README.md'), PermissionError(13, 'Permission denied', '/secret/x'),
                      IsADirectoryError('/secret/y'), OSError('boom /secret/z')):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(skill_page, 'canonical_path', side_effect=error):
                    status, headers, body = await self.raw(request_bytes('/skill/README.md', host))
                self.assertEqual((status, body), (404, b'not_found'))
                self.assertNotIn(b'secret', body)

    async def test_a_file_replaced_by_a_directory_or_symlink_in_the_mount_does_not_leak(self):
        await self.serve()
        root = self.base_dir / 'assets'
        shutil.copytree(SKILLS, root / 'skills')
        (root / 'skills' / 'wb-inbox' / 'SKILL.md').unlink()
        (root / 'skills' / 'wb-inbox' / 'SKILL.md').mkdir()
        self.spy_on_assets(root)
        status, _, body = await self.raw(request_bytes('/skill/wb-inbox/SKILL.md', f'127.0.0.1:{self.port}'))
        self.assertEqual((status, body), (404, b'not_found'))


class SkillRepoUrlRenderingTests(SkillCase):
    """A valid but nasty ``skill_repo_url`` becomes exactly one README line and changes nothing else."""

    VALID_NASTY = (
        'https://x.example/a)[click](javascript:alert(1))', 'https://x.example/<img/src=x/onerror=alert(1)>',
        'https://x.example/\\1\\g<0>\\\\', 'https://x.example/%s%(x)s{0}{x}$&$1', 'https://x.example/`code`**b**|t|',
        f'https://x.example/{PLACEHOLDER}', 'https://x.example/%0a%0dInjected:%20line', 'https://x.example/#' + '"' * 3,
        "https://x.example/';DROP-TABLE-x;--", 'http://[::1]:8443/r?a=b&c=d#e', 'HTTPS://X.EXAMPLE/Mixed/Case',
    )

    async def test_values_that_pass_validation_stay_on_the_one_marker_line(self):
        host_for = {}
        for url in self.VALID_NASTY:
            with self.subTest(url=url[:50]):
                parsed = config.parse_config(f'[web]\nskill_repo_url = {json.dumps(url)}\n'.encode())
                self.assertEqual(parsed.web.skill_repo_url, url)
                await self.serve(skill_repo_url=url)
                host = f'127.0.0.1:{self.port}'
                host_for[url] = host
                status, _, body = await self.raw(request_bytes('/skill/README.md', host))
                self.assertEqual(status, 200)
                original = (SKILLS / 'README.md').read_text().split('\n')
                served = body.decode('ascii').split('\n')
                self.assertEqual(len(served), len(original), 'the value must not add lines')
                for index, (new, old) in enumerate(zip(served, original)):
                    if old == MARKER:
                        self.assertEqual(new, f'Source repository: {url}')
                    else:
                        self.assertEqual(new, old.replace(PLACEHOLDER, f'http://{host}'), f'line {index}')
                for name in NAMES[1:]:
                    _, _, other = await self.raw(request_bytes(f'/skill/{name}', host))
                    self.assertEqual(other, expected_bytes(name, f'http://{host}'))
                self.assertNotIn(b'wb:skill-repo', body)
                await self.server.close()
                await self.http.close()

    def test_validation_rejects_what_could_break_the_line_or_change_the_target(self):
        bad = ['https://x.example/\r', 'https://x.example/\x0b', 'https://x.example/\x0c', 'https://x.example/\x1b[31m',
               'https://x.example/\x85', 'https://x.example/\u2028', 'https://x.example/\u2029', 'https://x.example/\u202e',
               'https://x.example/\u200b', 'https://exa\u200bmple.com/', 'https://x.example\\@evil.example/',
               'https://x.example:80:80/', 'https://[::1/', 'https://[::1]x/', 'https://x.example/\u3000',
               'https://x.example/\xa0', 'JAVASCRIPT:alert(1)', 'javascript://x.example/%0aalert(1)', 'data:text/html,hi',
               'https:\\\\x.example', 'https:/x.example', 'git@github.com:org/repo.git', 'ssh://git@github.com/org/repo',
               'https://:80/', 'https://x.example:-1/', 'https://x.example:65536/', '\nhttps://x.example/',
               'https://x.example/\nSource repository: https://evil.example']
        for value in bad:
            with self.subTest(value=value[:60]):
                with self.assertRaises(config.ConfigInvalid) as caught:
                    config.parse_config(f'[web]\nskill_repo_url = {json.dumps(value)}\n'.encode())
                self.assertEqual(caught.exception.reason, 'config_invalid:web.skill_repo_url')


class AllowFromAndListenerTests(SkillCase):
    async def test_a_peer_outside_allow_from_never_sees_any_skill_text_or_cookie(self):
        await self.serve(allow_from=(ipaddress.ip_network('10.9.8.7/32'),))
        host = f'127.0.0.1:{self.port}'
        for path in [f'/skill/{n}' for n in NAMES] + ['/skill/.env', '/skill', '/skill/', '/skill/x']:
            for method in ('GET', 'HEAD'):
                with self.subTest(path=path, method=method):
                    status, headers, body = await self.raw(request_bytes(path, host, method=method))
                    self.assertEqual((status, body), (403, b''))
                    self.assertEqual(header(headers, 'set-cookie'), [])
                    self.assertNotEqual(header(headers, 'cache-control'), ['no-cache'])
        # refusal comes before the Host check: a spoofed Host from the refused peer learns nothing new
        status, _, body = await self.raw(request_bytes('/skill/README.md', 'evil.example'))
        self.assertEqual((status, body), (403, b''))

    async def test_a_listed_peer_is_served_and_x_forwarded_for_cannot_stand_in_for_the_peer(self):
        await self.serve(allow_from=(ipaddress.ip_network('10.9.8.7/32'),))
        host = f'127.0.0.1:{self.port}'
        for extra in ('X-Forwarded-For: 10.9.8.7', 'X-Real-IP: 10.9.8.7', 'Forwarded: for=10.9.8.7', 'Client-IP: 10.9.8.7'):
            with self.subTest(extra=extra):
                status, _, body = await self.raw(request_bytes('/skill/README.md', host, extra))
                self.assertEqual((status, body), (403, b''))
        await self.server.close()
        await self.http.close()
        await self.serve(allow_from=(ipaddress.ip_network('127.0.0.1/32'),))
        host = f'127.0.0.1:{self.port}'
        status, _, body = await self.raw(request_bytes('/skill/README.md', host, 'X-Forwarded-For: 10.9.8.7'))
        self.assertEqual((status, body), (200, expected_bytes('README.md', f'http://{host}')))

    async def test_all_four_routes_are_marked_public_in_the_live_router_and_nothing_else_is_under_skill(self):
        await self.serve()
        from workbench.hub.web.middleware import ACCESS_PUBLIC  # noqa: PLC0415
        found = {}
        for resource in self.app.router.resources():
            info = resource.get_info()
            path = info.get('path') or info.get('formatter')
            if path and (path == '/skill' or path.startswith('/skill/')):
                for route in resource:
                    found[(route.method, path)] = getattr(route.handler, 'wb_access', None)
        self.assertTrue(found)
        methods = {method for method, _ in found}
        self.assertLessEqual(methods, {'GET', 'HEAD'}, 'GET (and aiohttp\'s implied HEAD) only')
        for key, access in found.items():
            with self.subTest(route=key):
                self.assertEqual(access, ACCESS_PUBLIC)


class RealHubTests(unittest.TestCase):
    """Seam S3 subset: the real ``wb _hub --foreground`` in the shared web sandbox."""

    def setUp(self):
        self.box = WebSandbox(self)

    def start(self, *lines: str, enabled: bool = True):
        self.box.write_config('127.0.0.1', 0, *lines, enabled=enabled)
        self.box.set_password()
        self.hub = self.box.start_hub()

    def get(self, port: int, path: str, host: str, method: str = 'GET', *extra: str):
        return raw(port, request_bytes(path, host, *extra, method=method))

    def test_real_listener_echoes_only_checked_hosts_and_the_configured_server_name(self):
        self.start('server_names = ["hub.example"]', 'skill_repo_url = "https://example.org/skills"')
        _host, port = self.box.wait_listening(self.hub.pid)
        for host in (f'127.0.0.1:{port}', f'localhost:{port}', f'hub.example:{port}'):
            with self.subTest(host=host):
                status, headers, body = self.get(port, '/skill/README.md', host)
                self.assertEqual(status, 200)
                self.assertEqual(body, expected_bytes('README.md', f'http://{host}', 'https://example.org/skills'))
                self.assertEqual(header(headers, 'cache-control'), ['no-cache'])
        for host in ('evil.example', f'evil.example:{port}', f'hub.example:{port + 1}', f'127.0.0.1:{port}@evil.example'):
            with self.subTest(host=host):
                status, _, body = self.get(port, '/skill/README.md', host)
                self.assertIn(status, (421, 400))
                self.assertNotIn(b'Workbench', body)
        status, _, body = self.get(port, '/skill/wb-inbox/.env', f'127.0.0.1:{port}')
        self.assertEqual((status, body), (404, b'not_found'))
        # every other listening socket of the Hub (the internal Board listener, when it exists) has no /skill
        for address, other in tcp_listening(self.hub.pid):
            if other != port:
                with self.subTest(internal=other):
                    status, _, body = raw(other, request_bytes('/skill/README.md', f'127.0.0.1:{other}'))
                    self.assertNotEqual(status, 200)
                    self.assertNotIn(b'Workbench', body)

    def test_real_listener_refuses_a_peer_outside_allow_from_with_a_bare_403(self):
        self.start('allow_from = ["10.9.8.7/32"]')
        _host, port = self.box.wait_listening(self.hub.pid)
        for path in ('/skill/README.md', '/skill/wb-inbox/SKILL.md', '/skill/.env'):
            status, headers, body = self.get(port, path, f'127.0.0.1:{port}')
            self.assertEqual((status, body), (403, b''), path)
            self.assertNotEqual(header(headers, 'cache-control'), ['no-cache'])

    def test_web_disabled_leaves_no_listener_that_answers_skill(self):
        self.start('skill_repo_url = "https://example.org/skills"', enabled=False)
        time.sleep(2.5)
        listening = tcp_listening(self.hub.pid)
        for _address, port in listening:  # whatever else the Hub listens on (internal listener) has no /skill/
            status, _, body = raw(port, request_bytes('/skill/README.md', f'127.0.0.1:{port}'))
            self.assertNotEqual(status, 200, port)
            self.assertNotIn(b'Source repository', body)
        self.assertEqual(self.box.status()['web']['state'], 'disabled')

    def test_a_wildcard_free_default_hub_never_serves_for_a_changed_bind_after_reload_to_disabled(self):
        self.start()
        _host, port = self.box.wait_listening(self.hub.pid)
        self.assertEqual(self.get(port, '/skill/README.md', f'127.0.0.1:{port}')[0], 200)
        self.box.write_config('127.0.0.1', port, enabled=False)
        self.assertEqual(self.box.cli('hub', 'reload').returncode, 0)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and any(p == port for _, p in tcp_listening(self.hub.pid)):
            time.sleep(0.1)
        self.assertNotIn(port, [p for _, p in tcp_listening(self.hub.pid)])
        with self.assertRaises(OSError):
            raw(port, request_bytes('/skill/README.md', f'127.0.0.1:{port}'))


class WheelTests(unittest.TestCase):
    """Seam S5 subset: a wheel built *straight from a source tree* (not through an sdist, which MANIFEST.in filters)
    exercises ``BuildPy``'s own ``.env`` exclusion."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='wb-skill-wheel-')).resolve()
        cls.source = cls.tmp / 'source'
        cls.source.mkdir()
        for name in ('src', 'omp_bridge', 'build_support', 'skills'):
            shutil.copytree(REPO / name, cls.source / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', '_assets'))
        for name in ('pyproject.toml', 'MANIFEST.in'):
            shutil.copy2(REPO / name, cls.source / name)
        skills = cls.source / 'skills'
        (skills / 'wb-inbox' / 'deep' / 'er').mkdir(parents=True)
        for folder in (skills, skills / 'wb-inbox', skills / 'wb-inbox' / 'deep' / 'er'):
            (folder / '.env').write_text('WB_API_KEY=wbk_wheel_leak_canary\n')
        (skills / 'wb-inbox' / '__pycache__').mkdir()
        (skills / 'wb-inbox' / '__pycache__' / 'wb_inbox.cpython-312.pyc').write_bytes(b'pyc')
        env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
        for key, name in (('HOME', 'home'), ('UV_CACHE_DIR', 'cache'), ('TMPDIR', 'tmp')):
            (cls.tmp / name).mkdir()
            env[key] = str(cls.tmp / name)
        env.update(PYTHONDONTWRITEBYTECODE='1', UV_PYTHON_DOWNLOADS='never', UV_NO_PROGRESS='1')
        result = subprocess.run(['uv', 'build', '--wheel', '--out-dir', str(cls.tmp / 'dist'), str(cls.source)],
                                cwd=cls.tmp / 'tmp', env=env, text=True, capture_output=True, timeout=240)
        if result.returncode:
            raise AssertionError(f'uv build failed: {result.stdout}\n{result.stderr}')
        cls.wheel = next((cls.tmp / 'dist').glob('*.whl'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_wheel_has_exactly_the_four_skill_files_and_no_env_at_any_depth(self):
        with zipfile.ZipFile(self.wheel) as archive:
            names = archive.namelist()
            skills = sorted(n for n in names if n.startswith('workbench/_assets/skills/'))
            self.assertEqual(skills, sorted(f'workbench/_assets/skills/{n}' for n in NAMES))
            self.assertEqual([n for n in names if n.rsplit('/', 1)[-1] == '.env'], [])
            for name in NAMES:
                self.assertEqual(archive.read(f'workbench/_assets/skills/{name}'), (SKILLS / name).read_bytes(), name)
            self.assertNotIn(b'wbk_wheel_leak_canary', b''.join(archive.read(n) for n in names))
            self.assertFalse([n for n in names if n.endswith('.pyc') and '/_assets/skills/' in n])

    def test_the_installed_layout_in_the_wheel_serves_through_the_resolver_like_the_repo(self):
        with zipfile.ZipFile(self.wheel) as archive:
            root = self.tmp / 'unzipped'
            archive.extractall(root)
        assets = root / 'workbench' / '_assets'
        for name in NAMES:
            self.assertEqual((assets / 'skills' / name).read_bytes(), (SKILLS / name).read_bytes(), name)
        self.assertEqual(sorted(p.name for p in (assets / 'skills' / 'wb-inbox').iterdir()),
                         sorted(['.env.example', 'SKILL.md', 'wb_inbox.py']))


if __name__ == '__main__':
    unittest.main()
