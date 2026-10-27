"""WBX-A3 independent verification (test_designer): scenarios derived from the web-board-ux SPEC "평문 HTTP 배너 접기",
BRIEF WBX-D03 / WBX-AC-05 and the ticket, before the implementation was read. Seams: S1/S6 (the real Hub web app in
process, HTML read with stdlib ``html.parser``), S5 (static CSS/JS text) and the ``wb hub status`` line (unchanged).
Run with ``discover -s tests/hub -p 'test_web_banner*.py'``.
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

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from aiohttp import ClientSession, DummyCookieJar, test_utils  # noqa: E402

from workbench.hub import auth, config, server  # noqa: E402
from workbench.hub.cli import web_status_lines  # noqa: E402
from workbench.hub.paths import HubLayout  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402

PASSWORD = 'correct horse battery'
WARNING = '평문 HTTP로 LAN에 열려 있습니다. VPN 등 네트워크 보호가 필요합니다.'  # written out on purpose
STATIC = REPO / 'src/workbench/hub/web/static'
VOID = frozenset({'meta', 'link', 'input', 'br', 'hr', 'img'})


class Page(HTMLParser):
    """Everything the banner contract talks about, collected from one HTML response."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.text = text
        self.banners: list[dict] = []
        self.chips: list[dict] = []
        self.closes: list[dict] = []
        self.scripts: list[dict] = []
        self.styles: list[dict] = []
        self.html_attrs: dict = {}
        self.event_attrs: list[str] = []
        self.style_attrs = 0
        self.warning_text_outside: list[str] = []
        self._stack: list[tuple[str, dict | None]] = []
        self.feed(text)

    def where(self) -> str:
        names = [name for name, _ in self._stack]
        return next((p for p in ('head', 'header', 'main', 'body') if p in names), 'top')

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if tag == 'html':
            self.html_attrs = attrs
        self.event_attrs += [k for k in attrs if k.startswith('on')]
        self.style_attrs += 'style' in attrs
        entry = None
        if tag == 'div' and {'banner', 'insecure'} <= set(classes):
            entry = {'attrs': attrs, 'text': '', 'where': self.where()}
            self.banners.append(entry)
        elif 'insecure-chip' in classes:
            entry = {'tag': tag, 'attrs': attrs, 'text': '', 'where': self.where()}
            self.chips.append(entry)
        elif tag == 'button' and 'banner-close' in classes:
            entry = {'attrs': attrs, 'text': '', 'where': self.where()}
            self.closes.append(entry)
        elif tag == 'script':
            entry = {'attrs': attrs, 'text': '', 'where': self.where()}
            self.scripts.append(entry)
        elif tag == 'style':
            entry = {'attrs': attrs, 'text': '', 'where': self.where()}
            self.styles.append(entry)
        if tag not in VOID:
            self._stack.append((tag, entry))

    def handle_endtag(self, tag):
        names = [name for name, _ in self._stack]
        if tag in VOID or tag not in names:
            return
        while self._stack:
            if self._stack.pop()[0] == tag:
                break

    def handle_data(self, data):
        for _, entry in self._stack:
            if entry is not None:
                entry['text'] += data
        if WARNING in data and not any(e is not None and e in self.banners for _, e in self._stack):
            self.warning_text_outside.append(self.where())


class Base(unittest.IsolatedAsyncioTestCase):
    insecure = True

    async def asyncSetUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix='wb-banner-ind-')).resolve()
        self.addCleanup(shutil.rmtree, self.base_dir, True)
        state = self.base_dir / 'state'
        state.mkdir(mode=0o700)
        self.passwords = auth.PasswordStore(state)
        self.generation = self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(state)
        self.addCleanup(self.db.close)
        self.services = WebServices(self.passwords, self.db, OriginPolicy('127.0.0.1'), insecure_warning=self.insecure)
        self.hub = server.Hub(HubLayout(self.base_dir / 'hub'), 'detached')
        self.addCleanup(self.hub.inbox.close)
        self.server = test_utils.TestServer(create_app(self.services, routes=self.hub.web_routes()),
                                            host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.http = ClientSession(cookie_jar=DummyCookieJar())
        self.addAsyncCleanup(self.http.close)
        self.host = f'127.0.0.1:{self.server.port}'
        self.url = f'http://{self.host}'
        self.session = self.services.sessions.create('127.0.0.1', 'banner', time.time(), generation=self.generation)

    async def request(self, method: str, path: str, *, session: bool = True, data: dict | None = None):
        headers = {'Host': self.host}
        if session:
            headers['Cookie'] = f'wb_hub_session={self.session.token}'
        if method == 'POST':
            headers['Origin'] = self.url
        async with self.http.request(method, self.url + path, headers=headers, data=data,
                                     allow_redirects=False) as response:
            return response.status, dict(response.headers), await response.text()


# every HTML screen a signed-in owner or the login form can get that carries the layout
GET_SCREENS = ('/login', '/', '/requests', '/requests/new', '/settings', '/settings/keys', '/settings/audit',
               '/board', '/projects')


async def collect(case: Base) -> dict[str, tuple[int, dict, str]]:
    """label -> (status, headers, body) for every layout-bearing HTML response we can provoke."""
    out = {}
    for path in GET_SCREENS:
        out[f'GET {path}'] = await case.request('GET', path, session=path != '/login')
    # stale form: a signed-in POST whose CSRF value is wrong -> small 403 page (middleware.stale_form_page)
    out['POST stale /settings/password'] = await case.request(
        'POST', '/settings/password', data={'csrf': 'not-the-session-token', 'current': 'x', 'new': 'y', 'confirm': 'y'})
    out['POST stale /logout'] = await case.request('POST', '/logout', data={'csrf': 'not-the-session-token'})
    # a login attempt with a wrong password re-renders the login form
    out['POST bad /login'] = await case.request('POST', '/login', session=False,
                                                data={'password': 'wrong password!', 'csrf': 'x'})
    return out


class WarningPresentEverywhereTests(Base):
    async def test_every_html_page_that_shows_the_warning_text_has_exactly_one_banner_and_chip(self):
        pages = await collect(self)
        shown = 0
        for label, (status, headers, body) in pages.items():
            if 'text/html' not in headers.get('Content-Type', ''):
                continue
            page = Page(body)
            with self.subTest(label=label, status=status):
                self.assertEqual(len(page.banners), 1)
                self.assertEqual(len(page.chips), 1)
                self.assertEqual(len(page.closes), 1)
                shown += 1
        self.assertGreaterEqual(shown, 8, [(k, v[0]) for k, v in pages.items()])

    async def test_the_stale_form_403_and_the_board_pages_are_among_the_pages_with_the_pair(self):
        pages = await collect(self)
        stale = pages['POST stale /settings/password']
        self.assertEqual(stale[0], 403, stale[2][:300])
        self.assertIn('text/html', stale[1].get('Content-Type', ''))
        page = Page(stale[2])
        self.assertEqual((len(page.banners), len(page.chips)), (1, 1))
        board = pages['GET /board']
        self.assertIn(board[0], (200, 503), board[2][:300])  # 503: detached Hub has no Board agent; same layout
        page = Page(board[2])
        self.assertEqual((len(page.banners), len(page.chips)), (1, 1))

    async def test_the_banner_and_chip_always_come_and_go_together_with_the_warning_text(self):
        # Invariant over everything we can provoke: the warning sentence in a body <=> banner AND chip AND script.
        for label, (status, headers, body) in (await collect(self)).items():
            if 'text/html' not in headers.get('Content-Type', ''):
                continue
            page = Page(body)
            with self.subTest(label=label):
                self.assertEqual(WARNING in body, bool(page.banners), 'warning sentence without a banner or vice versa')
                self.assertEqual(bool(page.banners), bool(page.chips))
                self.assertEqual(bool(page.banners), bool(page.closes))
                self.assertEqual(bool(page.banners), any(s['attrs'].get('src') == '/static/insecure-banner.js'
                                                         for s in page.scripts))


class WarningAbsentTests(Base):
    insecure = False

    async def test_no_page_has_any_trace_of_the_banner_when_the_condition_is_false(self):
        for label, (status, headers, body) in (await collect(self)).items():
            with self.subTest(label=label):
                for needle in (WARNING, 'insecure-chip', 'banner-close', 'insecure-banner', 'wb-insecure', '평문 HTTP',
                               'wb.insecureBanner'):
                    self.assertNotIn(needle, body)


class WarningConditionMatrixTests(Base):
    """The condition (``config.insecure_warning``) decides the markup. TLS or loopback-only binds -> nothing."""

    MATRIX = (
        # (binds, tls, warns)
        (['127.0.0.1'], False, False),
        (['::1'], False, False),
        (['127.0.0.1', '::1'], False, False),
        (['0.0.0.0'], False, True),
        (['::'], False, True),
        (['192.168.0.10'], False, True),
        (['10.8.0.2'], False, True),
        (['127.0.0.1', '10.8.0.2'], False, True),      # several binds: one LAN address is enough
        (['10.8.0.2', '127.0.0.1'], False, True),
        (['0.0.0.0'], True, False),                    # TLS: never
        (['127.0.0.1', '10.8.0.2'], True, False),
        (['10.8.0.2'], True, False),
    )

    async def test_markup_follows_the_condition_for_each_bind_and_tls_combination(self):
        for binds, tls, warns in self.MATRIX:
            with self.subTest(binds=binds, tls=tls):
                self.assertIs(config.insecure_warning(binds, tls=tls), warns)
                self.services.insecure_warning = config.insecure_warning(binds, tls=tls)
                for path in ('/login', '/', '/settings'):
                    status, _, body = await self.request('GET', path, session=path != '/login')
                    self.assertEqual(status, 200, path)
                    page = Page(body)
                    self.assertEqual((len(page.banners), len(page.chips)), ((1, 1) if warns else (0, 0)), path)
                    self.assertEqual(WARNING in body, warns, path)

    async def test_the_flag_is_read_per_request_so_a_restart_into_tls_drops_the_banner(self):
        status, _, body = await self.request('GET', '/login', session=False)
        self.assertEqual(len(Page(body).banners), 1)
        self.services.insecure_warning = False
        status, _, body = await self.request('GET', '/login', session=False)
        self.assertEqual(Page(body).banners, [])


class WarningTextTests(Base):
    async def test_the_warning_sentence_is_unchanged_and_shown_in_full_by_banner_and_chip(self):
        self.assertEqual(config.INSECURE_WARNING_TEXT, WARNING)
        for label, (status, headers, body) in (await collect(self)).items():
            if 'text/html' not in headers.get('Content-Type', ''):
                continue
            page = Page(body)
            with self.subTest(label=label):
                self.assertEqual(page.banners[0]['text'].replace('닫기', '').strip(), WARNING)
                self.assertEqual(page.chips[0]['attrs'].get('title'), WARNING)   # whole sentence, not shortened
                self.assertEqual(page.chips[0]['text'].strip(), '평문 HTTP')
                self.assertEqual(page.warning_text_outside, [])                  # the sentence is not scattered

    async def test_the_chip_is_in_the_header_and_the_banner_is_outside_the_header(self):
        status, _, body = await self.request('GET', '/requests')
        page = Page(body)
        self.assertEqual(page.chips[0]['where'], 'header')
        self.assertEqual(page.banners[0]['where'], 'body')
        self.assertLess(body.index('class="banner insecure"'), body.index('<header'))  # banner stays above the header

    async def test_the_chip_is_focusable_without_being_a_link_or_form_control(self):
        status, _, body = await self.request('GET', '/')
        chip = Page(body).chips[0]
        self.assertEqual(chip['attrs'].get('tabindex'), '0')    # reachable by Tab; hover and focus show the popover
        self.assertNotIn('hidden', chip['attrs'])               # the CSS, not the markup, hides it when expanded
        self.assertNotIn('disabled', chip['attrs'])


class NoJavaScriptTests(Base):
    async def test_the_markup_alone_is_the_expanded_state(self):
        for path in ('/login', '/', '/requests'):
            status, _, body = await self.request('GET', path, session=path != '/login')
            page = Page(body)
            with self.subTest(path=path):
                self.assertNotIn('wb-insecure-collapsed', page.html_attrs.get('class', ''))  # never pre-collapsed
                self.assertIn('hidden', page.closes[0]['attrs'])      # inert without JS
                self.assertNotIn('hidden', page.banners[0]['attrs'])
                self.assertNotIn('style', page.banners[0]['attrs'])
                self.assertNotIn('style', page.chips[0]['attrs'])
                self.assertNotIn('noscript', body.lower())            # nothing to repair: it is complete already

    def test_no_stylesheet_gives_the_hidden_close_button_a_display_value(self):
        # An author `display` on the button would beat the UA `[hidden]` rule and show a dead button without JS.
        for css in sorted(STATIC.glob('*.css')):
            text = re.sub(r'/\*.*?\*/', '', css.read_text(encoding='utf-8'), flags=re.S)
            for selector, body in re.findall(r'([^{}]+)\{([^{}]*)\}', text):
                if 'banner-close' in selector or re.search(r'(^|[\s,])button(?![\w-])', selector):
                    self.assertNotIn('display', body, f'{css.name}: {selector.strip()}')

    def test_no_stylesheet_other_than_the_collapsed_rules_displays_or_hides_banner_or_chip(self):
        found = []
        for css in sorted(STATIC.glob('*.css')):
            text = re.sub(r'/\*.*?\*/', '', css.read_text(encoding='utf-8'), flags=re.S)
            for selector, body in re.findall(r'([^{}]+)\{([^{}]*)\}', text):
                if re.search(r'display\s*:', body) and re.search(r'\.banner|\.insecure|\.banner-close', selector):
                    found.append((css.name, ' '.join(selector.split()), re.search(r'display\s*:\s*([\w-]+)', body).group(1)))
        self.assertCountEqual(found, [
            ('base.css', '.insecure-chip', 'none'),
            ('base.css', '.wb-insecure-collapsed .insecure-chip', 'inline-block'),
            ('base.css', '.wb-insecure-collapsed .banner.insecure', 'none'),
        ])


class CspAndMarkupHygieneTests(Base):
    async def test_no_inline_script_style_or_event_handler_and_the_banner_script_is_external_in_head(self):
        for label, (status, headers, body) in (await collect(self)).items():
            if 'text/html' not in headers.get('Content-Type', ''):
                continue
            page = Page(body)
            with self.subTest(label=label):
                for script in page.scripts:
                    self.assertIn('src', script['attrs'])
                    self.assertEqual(script['text'].strip(), '')
                self.assertEqual(page.event_attrs, [])
                self.assertEqual(page.style_attrs, 0)
                mine = [s for s in page.scripts if s['attrs'].get('src') == '/static/insecure-banner.js']
                self.assertEqual(len(mine), 1)
                self.assertEqual(mine[0]['where'], 'head')
                self.assertTrue({'defer', 'async', 'type'}.isdisjoint(mine[0]['attrs']))
                self.assertEqual([s for s in page.styles if s['text'].strip()] and
                                 [s for s in page.styles if 'nonce' not in s['attrs']], [])  # only nonce'd <style> (Board)

    async def test_the_response_csp_is_not_loosened_by_the_banner(self):
        with_banner = await collect(self)
        self.services.insecure_warning = False
        without = await collect(self)
        for label, (status, headers, body) in with_banner.items():
            csp = headers.get('Content-Security-Policy', '')
            with self.subTest(label=label):
                self.assertIn("script-src 'self'", csp)
                self.assertNotIn("script-src 'self' 'unsafe", csp)
                self.assertNotIn('unsafe-inline', csp)
                self.assertNotIn('unsafe-eval', csp)
                other = re.sub(r"'nonce-[^']*'", "'nonce-X'", without[label][1].get('Content-Security-Policy', ''))
                self.assertEqual(re.sub(r"'nonce-[^']*'", "'nonce-X'", csp), other)

    async def test_the_script_and_stylesheet_are_served_without_a_session(self):
        for path in ('/static/insecure-banner.js', '/static/base.css'):
            status, headers, body = await self.request('GET', path, session=False)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertIn('wb-insecure-collapsed', body)

    def test_the_script_source_only_uses_the_banner_key_and_makes_no_network_or_eval_calls(self):
        source = (STATIC / 'insecure-banner.js').read_text(encoding='utf-8')
        code = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
        for forbidden in ('sessionStorage', 'document.cookie', 'fetch(', 'XMLHttpRequest', 'eval(', 'new Function',
                          'innerHTML', 'document.write', 'localStorage.clear', '.clear('):
            self.assertNotIn(forbidden, code)
        self.assertEqual(re.findall(r'"wb\.[A-Za-z.]+"', code), ['"wb.insecureBanner"'])


class StatusLineUnchangedTests(unittest.TestCase):
    def test_wb_hub_status_prints_the_same_warning_line(self):
        listening = {'state': 'listening', 'url': 'http://10.8.0.2:8780', 'urls': ['http://10.8.0.2:8780']}
        self.assertEqual(web_status_lines({**listening, 'insecure_warning': True}),
                         ['web: listening http://10.8.0.2:8780', f'경고: {WARNING}'])
        self.assertEqual(web_status_lines({**listening, 'insecure_warning': False}),
                         ['web: listening http://10.8.0.2:8780'])


if __name__ == '__main__':
    unittest.main()
