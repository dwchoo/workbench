"""Independent check of the /board CSP nonce contract (2026-10-26 사용자 결정), derived from the contract text only.

Contract:
- A ``/board`` 200 response has ``style-src 'self' 'nonce-<N>'; style-src-attr 'unsafe-inline'`` and every other
  directive identical to the strict policy. ``N`` is fresh per response, unguessable (``token_urlsafe(16)``) and
  equal to the page's ``<meta name="style-nonce">``. ``style-src`` has no ``'unsafe-inline'``.
- Every other page, every static asset and every non-200 ``/board`` response keep the strict CSP: no nonce, no
  ``style-src-attr``.
- ``static/board.js`` installs the nonce wrapper before xterm opens (behaviour is in ``tests/hub/js``).

The strict policy below is written out from the SPEC "보안 header" line, not imported from the code under test.
The app is the real ``create_app`` with the real route sets over a fake Board set, a fake control and a real inbox
store (in-process aiohttp ``TestServer``; no OMP, no Hub process). Run with
``discover -s tests/hub -p 'test_web_board_csp_independent.py'``.
"""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from aiohttp import test_utils

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

import test_web_pages_projects as projects  # noqa: E402  (module import: its test classes stay uncollected here)
from test_web_pages_board import FakeBoard  # noqa: E402
from workbench.hub.web.app import board_routes, create_app  # noqa: E402
from workbench.hub.web.pages import control as control_pages  # noqa: E402
from workbench.hub.web.pages import projects as project_pages  # noqa: E402
from workbench.hub.web.pages import requests as request_pages  # noqa: E402

STATIC = REPO / 'src/workbench/hub/web/static'

# SPEC "보안 header": the strict policy every response carries, as {directive: sources}.
STRICT = {
    'default-src': ["'self'"],
    'script-src': ["'self'"],
    'style-src': ["'self'"],
    'img-src': ["'self'", 'data:'],
    'connect-src': ["'self'"],
    'object-src': ["'none'"],
    'base-uri': ["'none'"],
    'frame-ancestors': ["'none'"],
    'form-action': ["'self'"],
}
STRICT_HEADER = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                 "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                 "form-action 'self'")
NONCE_SOURCE = re.compile(r"^'nonce-([^']+)'$")
META = re.compile(r'<meta\b[^>]*\bname="style-nonce"[^>]*>', re.IGNORECASE)


def parse_csp(header: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for part in header.split(';'):
        words = part.split()
        if not words:
            continue
        if words[0] in directives:
            raise AssertionError(f'repeated CSP directive {words[0]!r} in {header!r}')
        directives[words[0]] = words[1:]
    return directives


def header_nonce(header: str) -> str | None:
    """The one nonce of a CSP header (``None`` when there is none); more than one is an error."""
    found = [NONCE_SOURCE.match(item).group(1) for sources in parse_csp(header).values() for item in sources
             if NONCE_SOURCE.match(item)]
    if len(found) > 1:
        raise AssertionError(f'more than one nonce in {header!r}')
    return found[0] if found else None


def meta_nonce(body: str) -> str:
    tags = META.findall(body)
    if len(tags) != 1:
        raise AssertionError(f'expected exactly one style-nonce meta, found {len(tags)}')
    contents = re.findall(r'\bcontent="([^"]*)"', tags[0])
    if len(contents) != 1:
        raise AssertionError(f'style-nonce meta without exactly one content: {tags[0]!r}')
    return contents[0]


def entropy_bytes(nonce: str) -> int:
    return len(base64.urlsafe_b64decode(nonce + '=' * (-len(nonce) % 4)))


class CspCase(projects.ProjectsCase):
    """The pages of every route set, over one logged-in owner session."""

    async def asyncSetUp(self):
        # Short private runtime dir and a temp HOME: nothing here may reach a real Hub or backend runtime.
        self.runtime = tempfile.mkdtemp(prefix='r', dir='/tmp')
        self.addCleanup(shutil.rmtree, self.runtime, True)
        home = tempfile.mkdtemp(prefix='wb-csp-home-')
        self.addCleanup(shutil.rmtree, home, True)
        patch = mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.runtime, 'HOME': home,
                                             'XDG_STATE_HOME': home, 'WORKBENCH_HUB_AUTOSTART': '0'})
        patch.start()
        self.addCleanup(patch.stop)
        await super().asyncSetUp()
        self.board = FakeBoard()
        self.board_ref = [self.board]
        await self.server.close()
        self.routes = (control_pages.use(self.control), project_pages.routes(self.hub_list, self.inbox, self.hub_one),
                       request_pages.routes(self.inbox, lambda: list(self.refs)),
                       board_routes(lambda: self.board_ref[0]))
        self.server = test_utils.TestServer(create_app(self.services, routes=self.routes), host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.port = self.server.port
        self.base = f'http://127.0.0.1:{self.port}'

    async def head(self, path: str, *, cookies: dict | None = None, host: str | None = None):
        headers = {'Host': host or self.host()}
        if cookies:
            headers['Cookie'] = '; '.join(f'{name}={value}' for name, value in cookies.items())
        async with self.http.head(self.base + path, headers=headers, allow_redirects=False) as response:
            response.body_text = ''
            return response

    async def options(self, path: str, *, cookies: dict | None = None):
        headers = {'Host': self.host()}
        if cookies:
            headers['Cookie'] = '; '.join(f'{name}={value}' for name, value in cookies.items())
        async with self.http.options(self.base + path, headers=headers, allow_redirects=False) as response:
            response.body_text = await response.text()
            return response

    async def board_get(self):
        response = await self.get('/board', cookies=self.cookies)
        self.assertEqual(response.status, 200, response.body_text)
        return response

    @staticmethod
    def other_headers(response) -> list[tuple[str, str]]:
        return [(name, value) for name, value in response.headers.items() if name.lower() != 'content-security-policy']


class Board200PolicyTests(CspCase):
    async def test_twenty_responses_have_distinct_unguessable_nonces_equal_to_their_own_meta(self):
        seen = []
        for _ in range(20):
            response = await self.board_get()
            nonce = header_nonce(response.headers['Content-Security-Policy'])
            self.assertIsNotNone(nonce, response.headers['Content-Security-Policy'])
            self.assertEqual(nonce, meta_nonce(response.body_text), 'header nonce == the page meta nonce')
            self.assertRegex(nonce, r'^[A-Za-z0-9_-]+$', 'a CSP nonce source is base64url text')
            self.assertGreaterEqual(entropy_bytes(nonce), 16, 'at least 128 bits of entropy')
            seen.append(nonce)
        self.assertEqual(len(set(seen)), 20, 'no nonce is ever reused')

    async def test_concurrent_requests_never_share_or_swap_nonces(self):
        async def one():
            response = await self.get('/board', cookies=self.cookies)
            return response.status, header_nonce(response.headers['Content-Security-Policy']), \
                meta_nonce(response.body_text), response.body_text

        results = await asyncio.gather(*(one() for _ in range(20)))
        self.assertEqual({status for status, *_ in results}, {200})
        for _, from_header, from_meta, body in results:
            self.assertEqual(from_header, from_meta, 'a response never carries another response\'s nonce')
            self.assertEqual(body.count(from_header), 1)
        self.assertEqual(len({nonce for _, nonce, _, _ in results}), 20)

    async def test_nonces_differ_across_sessions_as_well(self):
        other = await self.session_cookie()
        first = await self.get('/board', cookies=self.cookies)
        second = await self.get('/board', cookies={'wb_hub_session': other})
        self.assertNotEqual(header_nonce(first.headers['Content-Security-Policy']),
                            header_nonce(second.headers['Content-Security-Policy']))

    async def test_directive_set_equals_the_strict_policy_except_style_src_and_style_src_attr(self):
        for _ in range(3):
            response = await self.board_get()
            raw = response.headers['Content-Security-Policy']
            directives = parse_csp(raw)
            nonce = header_nonce(raw)
            self.assertEqual(set(directives), set(STRICT) | {'style-src-attr'}, raw)
            for name, sources in STRICT.items():
                if name != 'style-src':
                    self.assertEqual(directives[name], sources, name)
            self.assertEqual(directives['style-src'], ["'self'", f"'nonce-{nonce}'"], raw)
            self.assertEqual(directives['style-src-attr'], ["'unsafe-inline'"], raw)
            self.assertNotIn("'unsafe-inline'", directives['style-src'])
            self.assertEqual(raw.count("'unsafe-inline'"), 1, 'the only unsafe-inline is the style-src-attr one')
            self.assertEqual(raw.count('nonce-'), 1)
            for forbidden in ("'unsafe-eval'", "'strict-dynamic'", "'unsafe-hashes'", '*', 'http:', 'https:'):
                self.assertNotIn(forbidden, raw)
            # style-src-elem / script-src-elem / script-src-attr would silently override style-src / script-src
            for extra in ('style-src-elem', 'script-src-elem', 'script-src-attr', 'worker-src', 'child-src'):
                self.assertNotIn(extra, directives)

    async def test_the_response_is_not_cacheable_and_keeps_the_other_security_headers(self):
        response = await self.board_get()
        # WBX-A2 (web-board-ux SPEC cache table): a logged-in page is private, no-cache; never shared, always
        # revalidated, and without validators every load is a fresh response with a fresh nonce.
        self.assertEqual(response.headers['Cache-Control'], 'private, no-cache')
        for name in ('ETag', 'Last-Modified', 'Expires'):
            self.assertNotIn(name, response.headers)
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertEqual(response.headers['Referrer-Policy'], 'same-origin')

    async def test_the_nonce_appears_nowhere_in_the_body_but_the_meta_and_in_no_other_header(self):
        for _ in range(5):
            response = await self.board_get()
            nonce = header_nonce(response.headers['Content-Security-Policy'])
            body = response.body_text
            self.assertEqual(body.count(nonce), 1, 'only the meta carries it')
            self.assertIn(nonce, META.findall(body)[0])
            self.assertNotIn('nonce=', body.lower().replace('name="style-nonce"', ''),
                             'no element carries a nonce attribute')
            for name, value in self.other_headers(response):
                self.assertNotIn(nonce, value, f'{name} reflects the nonce')
            self.assertEqual(len(META.findall(body)), 1)
            self.assertRegex(META.findall(body)[0], r'content="[A-Za-z0-9_-]+"', 'meta content is plain, escaped text')

    async def test_a_nonce_never_shows_up_in_a_later_response_of_any_route(self):
        issued = header_nonce((await self.board_get()).headers['Content-Security-Policy'])
        probes = ['/board', '/board/restart?channel=agent', '/dashboard', '/requests', '/settings', '/login',
                  '/static/board.js', '/static/style-nonce.js', '/static/base.css', '/static/board.css']
        for path in probes:
            response = await self.get(path, cookies=self.cookies)
            self.assertNotIn(issued, response.body_text, path)
            for name, value in self.other_headers(response):
                self.assertNotIn(issued, value, f'{path} {name}')
            if path != '/board':
                self.assertNotIn(issued, response.headers['Content-Security-Policy'], path)
        self.assertNotEqual(issued, header_nonce((await self.board_get()).headers['Content-Security-Policy']))

    async def test_head_board_200_follows_the_same_policy_and_is_never_cacheable(self):
        response = await self.head('/board', cookies=self.cookies)
        self.assertEqual(response.status, 200)
        raw = response.headers['Content-Security-Policy']
        directives = parse_csp(raw)
        nonce = header_nonce(raw)
        self.assertIsNotNone(nonce, 'a 200 for /board gets the board policy whatever the method')
        self.assertEqual(directives['style-src'], ["'self'", f"'nonce-{nonce}'"])
        self.assertEqual(directives['style-src-attr'], ["'unsafe-inline'"])
        self.assertGreaterEqual(entropy_bytes(nonce), 16)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-cache')  # WBX-A2, as the GET
        self.assertNotEqual(nonce, header_nonce((await self.board_get()).headers['Content-Security-Policy']),
                            'a HEAD nonce is not shared with the next GET')
        again = await self.head('/board', cookies=self.cookies)
        self.assertNotEqual(nonce, header_nonce(again.headers['Content-Security-Policy']))


class StrictElsewhereTests(CspCase):
    def assert_strict(self, response, what: str) -> None:
        raw = response.headers['Content-Security-Policy']
        self.assertEqual(raw, STRICT_HEADER, what)
        self.assertEqual(parse_csp(raw), STRICT, what)
        self.assertNotIn('nonce', raw, what)
        self.assertNotIn('style-src-attr', raw, what)
        self.assertNotIn('unsafe-inline', raw, what)

    async def test_every_other_page_keeps_the_strict_policy_and_has_no_style_nonce_meta(self):
        pages = {
            'login': ('/login', None),
            'status board': ('/dashboard', self.cookies),
            'requests': ('/requests', self.cookies),
            'new request': ('/requests/new', self.cookies),
            'settings': ('/settings', self.cookies),
            'keys': ('/settings/keys', self.cookies),
            'audit': ('/settings/audit', self.cookies),
            'project': (f'/projects/{self.a}', self.cookies),
            'control confirm': (f'/projects/{self.a}/pause', self.cookies),
            'board restart confirm': ('/board/restart?channel=agent', self.cookies),
            'board restart confirm shell': ('/board/restart?channel=shell', self.cookies),
        }
        ok = 0
        for what, (path, cookies) in pages.items():
            response = await self.get(path, cookies=cookies)
            self.assert_strict(response, f'{what} {path} -> {response.status}')
            self.assertNotIn('style-nonce', response.body_text, what)
            # WBX-A2 (SPEC cache table): the login screen stays no-store, logged-in pages are private, no-cache.
            self.assertEqual(response.headers['Cache-Control'], 'no-store' if cookies is None else 'private, no-cache',
                             what)
            ok += response.status == 200
        self.assertGreaterEqual(ok, 9, 'most of these must be real 200 pages, or the check proves little')

    async def test_requests_settings_control_login_and_project_pages_answer_200_with_the_strict_policy(self):
        for path in ('/requests', '/settings', f'/projects/{self.a}/pause',
                     f'/projects/{self.a}', '/board/restart?channel=agent'):
            response = await self.get(path, cookies=self.cookies)
            self.assertEqual(response.status, 200, path)
            self.assert_strict(response, path)
        response = await self.get('/login')  # a logged-out visitor; a session cookie would redirect away
        self.assertEqual(response.status, 200)
        self.assert_strict(response, '/login')

    async def test_static_assets_are_strict_whatever_their_type(self):
        paths = ['/static/board.js', '/static/board-state.js', '/static/style-nonce.js', '/static/badge.js',
                 '/static/base.css', '/static/board.css', '/static/vendor/xterm/xterm.js',
                 '/static/vendor/xterm/xterm.css', '/static/vendor/xterm/addon-fit.js']
        for path in paths:
            for cookies in (None, self.cookies):
                response = await self.get(path, cookies=cookies)
                self.assertEqual(response.status, 200, path)
                self.assert_strict(response, path)
                self.assertEqual(response.headers['Cache-Control'], 'public, max-age=3600', path)
            head = await self.head(path)
            self.assertEqual(head.status, 200, path)
            self.assert_strict(head, f'HEAD {path}')
        missing = await self.get('/static/nope.js')
        self.assertEqual(missing.status, 404)
        self.assert_strict(missing, '404 static')

    async def test_cached_static_files_hold_no_nonce_value_or_meta(self):
        board = await self.board_get()
        nonce = header_nonce(board.headers['Content-Security-Policy'])
        for name in ('board.js', 'style-nonce.js', 'board-state.js', 'base.css', 'board.css'):
            self.assertNotIn(nonce, (STATIC / name).read_text())
        # the cacheable static copies are constant: a nonce is never baked into a file served as public
        first = (await self.get('/static/style-nonce.js')).body_text
        second = (await self.get('/static/style-nonce.js')).body_text
        self.assertEqual(first, second)

    async def test_every_non_200_board_response_is_strict_with_no_nonce_anywhere(self):
        cases = {}
        cases['303 no cookie'] = await self.get('/board')
        cases['303 stale cookie'] = await self.get('/board', cookies={'wb_hub_session': 'stale'})
        cases['421 bad host'] = await self.get('/board', host='evil.example', cookies=self.cookies)
        cases['403 POST'] = await self.post('/board', {}, cookies=self.cookies)
        cases['403 POST no origin match'] = await self.post('/board', {}, cookies=self.cookies,
                                                           origin='http://evil.example')
        cases['405 OPTIONS'] = await self.options('/board', cookies=self.cookies)
        cases['404 sibling'] = await self.get('/board/', cookies=self.cookies)
        cases['404 deeper'] = await self.get('/board/x', cookies=self.cookies)
        cases['HEAD 303'] = await self.head('/board')
        cases['HEAD 421'] = await self.head('/board', host='evil.example', cookies=self.cookies)
        self.board_ref[0] = None
        cases['503 no board'] = await self.get('/board', cookies=self.cookies)
        cases['HEAD 503'] = await self.head('/board', cookies=self.cookies)
        expected = {'303 no cookie': 303, '303 stale cookie': 303, '421 bad host': 421, '403 POST': 403,
                    '503 no board': 503, 'HEAD 303': 303, 'HEAD 421': 421, 'HEAD 503': 503}
        for what, status in expected.items():
            self.assertEqual(cases[what].status, status, what)
        self.assertEqual(cases['403 POST no origin match'].status, 403)
        self.assertIn(cases['405 OPTIONS'].status, (403, 405))
        for what, response in cases.items():
            self.assert_strict(response, what)
            self.assertNotIn('style-nonce', getattr(response, 'body_text', ''), what)

    async def test_the_error_path_after_a_nonce_exists_still_gets_the_strict_policy(self):
        # board.status() raising happens before the page renders; render.page raising happens after the nonce
        # was made (frame() builds the metas first). Both must answer a 500 with the strict policy.
        class Boom(FakeBoard):
            def status(self):
                raise RuntimeError('boom')

        self.board_ref[0] = Boom()
        before = await self.get('/board', cookies=self.cookies)
        self.assertEqual(before.status, 500)
        self.assert_strict(before, '500 status() raised')
        self.board_ref[0] = self.board
        with mock.patch('workbench.hub.web.render.page', side_effect=RuntimeError('late failure')):
            late = await self.get('/board', cookies=self.cookies)
        self.assertEqual(late.status, 500)
        self.assert_strict(late, '500 after the nonce was made')
        self.assertNotIn('nonce', late.body_text.lower())
        # and the next good response is fine and fresh
        good = await self.board_get()
        self.assertIsNotNone(header_nonce(good.headers['Content-Security-Policy']))

    async def test_a_ws_board_refusal_is_strict(self):
        for cookies in (None, self.cookies):
            response = await self.get('/ws/board', cookies=cookies)
            self.assertIn(response.status, (400, 401, 403, 426), response.status)
            self.assert_strict(response, '/ws/board without an upgrade')


class BoardJsInstallsBeforeXtermOpensTests(unittest.TestCase):
    """Static order in ``board.js`` (the browser-less behaviour check is in ``tests/hub/js``)."""

    def test_board_js_imports_the_helper_reads_the_meta_and_installs_before_any_terminal_exists(self):
        text = re.sub(r'(?m)^\s*//.*$|(?<=\s)//[^\n]*$', '', (STATIC / 'board.js').read_text())  # code, not comments
        self.assertRegex(text, r'import\s*\{[^}]*\binstallStyleNonce\b[^}]*\}\s*from\s*"\./style-nonce\.js"')
        self.assertIn('meta[name="style-nonce"]', text)
        installs = [match.start() for match in re.finditer(r'(?<![\w$])installStyleNonce\s*\(', text)]
        self.assertEqual(len(installs), 1, 'installed exactly once')
        first_terminal = min(index for index in (text.find('new TerminalClass'), text.find('.open(')) if index >= 0)
        self.assertLess(installs[0], first_terminal, 'before the first terminal is made or opened')

    def test_the_page_script_set_loads_the_helper_only_through_board_js(self):
        text = (STATIC / 'style-nonce.js').read_text()
        self.assertIn('export function installStyleNonce', text)
        self.assertEqual(re.findall(r'https?://', text), [])
        self.assertNotIn('eval(', text)


if __name__ == '__main__':
    unittest.main()
