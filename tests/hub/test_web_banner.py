"""WBX-A3 plain-HTTP banner (seams S1 + S6 markup, S5 static CSS): the real Hub's web app in-process, HTML read with
stdlib ``html.parser``. Expected values come from the SPEC "평문 HTTP 배너 접기" bullets and BRIEF WBX-AC-05, not from
the implementation. The JS logic is ``tests/hub/js/insecure_banner.test.mjs`` (S8); a real browser is the user's (S7).
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

from workbench.hub import auth, server  # noqa: E402
from workbench.hub.paths import HubLayout  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402

PASSWORD = 'correct horse battery'
VOID = frozenset({'meta', 'link', 'input', 'br', 'hr', 'img'})
# The warning sentence `wb hub status` prints (BRIEF WBX-AC-05: unchanged), written out here on purpose.
WARNING = '평문 HTTP로 LAN에 열려 있습니다. VPN 등 네트워크 보호가 필요합니다.'
STATIC = REPO / 'src/workbench/hub/web/static'


class Dom(HTMLParser):
    """Banner ``div.banner.insecure``, chip ``span.insecure-chip``, close button and ``<script>`` placement."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.banners: list[dict] = []      # {attrs, text, where}
        self.chips: list[dict] = []        # {attrs, text, where}
        self.closes: list[dict] = []       # {attrs, text, in_banner}
        self.scripts: list[dict] = []      # {attrs, text, where}
        self.inline_styles = 0
        self._stack: list[str] = []
        self._current: list[dict] = []
        self._banner_depth = 0
        self.feed(text)

    def _where(self) -> str:
        for place in ('head', 'header', 'main', 'body'):
            if place in self._stack:
                return place
        return 'top'

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if any(key == 'style' for key in attrs) or tag == 'style':
            self.inline_styles += 1
        entry = None
        if tag == 'div' and 'banner' in classes and 'insecure' in classes:
            entry = {'attrs': attrs, 'text': '', 'where': self._where()}
            self.banners.append(entry)
            self._banner_depth += 1
        elif tag == 'span' and 'insecure-chip' in classes:
            entry = {'attrs': attrs, 'text': '', 'where': self._where()}
            self.chips.append(entry)
        elif tag == 'button' and 'banner-close' in classes:
            entry = {'attrs': attrs, 'text': '', 'in_banner': bool(self._banner_depth)}
            self.closes.append(entry)
        elif tag == 'script':
            entry = {'attrs': attrs, 'text': '', 'where': self._where()}
            self.scripts.append(entry)
        if tag not in VOID:
            self._stack.append(tag)
            self._current.append(entry)

    def handle_endtag(self, tag):
        if tag in VOID or tag not in self._stack:
            return
        while self._stack:
            popped = self._stack.pop()
            entry = self._current.pop()
            if popped == 'div' and entry is not None and entry in self.banners:
                self._banner_depth -= 1
            if popped == tag:
                break

    def handle_data(self, data):
        for entry in self._current:
            if entry is not None and 'text' in entry:
                entry['text'] += data


class BannerCase(unittest.IsolatedAsyncioTestCase):
    insecure = True

    async def asyncSetUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix='wb-banner-')).resolve()
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
        self.app = create_app(self.services, routes=self.hub.web_routes())
        self.server = test_utils.TestServer(self.app, host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.http = ClientSession(cookie_jar=DummyCookieJar())
        self.addAsyncCleanup(self.http.close)
        self.host = f'127.0.0.1:{self.server.port}'
        self.url = f'http://{self.host}'
        self.session = self.services.sessions.create('127.0.0.1', 'banner', time.time(), generation=self.generation)

    async def fetch(self, path: str, *, session: bool = True) -> tuple[int, str]:
        headers = {'Host': self.host}
        if session:
            headers['Cookie'] = f'wb_hub_session={self.session.token}'
        async with self.http.get(self.url + path, headers=headers, allow_redirects=False) as response:
            return response.status, await response.text()

    async def dom(self, path: str, *, session: bool = True) -> Dom:
        status, text = await self.fetch(path, session=session)
        self.assertEqual(status, 200, f'{path}: {text[:200]}')
        return Dom(text)


# A logged-in screen of each kind and the login screen (SPEC: "로그인 화면에도 같은 배너·chip이 있다").
SCREENS = ('/login', '/', '/requests', '/requests/new', '/settings', '/settings/keys', '/settings/audit')


class BannerShownTests(BannerCase):
    async def test_a_warning_shows_one_banner_with_a_hidden_close_button_and_one_chip_on_every_screen(self):
        for path in SCREENS:
            with self.subTest(path=path):
                dom = await self.dom(path, session=path != '/login')
                self.assertEqual(len(dom.banners), 1, path)
                self.assertEqual(dom.banners[0]['attrs'].get('role'), 'alert')
                self.assertEqual(dom.banners[0]['where'], 'body')
                self.assertIn(WARNING, dom.banners[0]['text'])
                self.assertEqual(len(dom.closes), 1, path)
                self.assertTrue(dom.closes[0]['in_banner'])
                self.assertIn('hidden', dom.closes[0]['attrs'])  # JS unhides it; without JS it stays inert
                self.assertEqual(dom.closes[0]['attrs'].get('type'), 'button')
                self.assertEqual(dom.closes[0]['text'].strip(), '닫기')
                self.assertEqual(len(dom.chips), 1, path)
                self.assertEqual(dom.chips[0]['where'], 'header')
                self.assertEqual(dom.chips[0]['text'].strip(), '평문 HTTP')
                self.assertEqual(dom.chips[0]['attrs'].get('tabindex'), '0')


class BannerAbsentTests(BannerCase):
    insecure = False

    async def test_without_a_warning_there_is_no_banner_chip_close_button_or_banner_script(self):
        for path in SCREENS:
            with self.subTest(path=path):
                status, text = await self.fetch(path, session=path != '/login')
                self.assertEqual(status, 200, path)
                dom = Dom(text)
                self.assertEqual((dom.banners, dom.chips, dom.closes), ([], [], []), path)
                self.assertNotIn('insecure-banner.js', text)
                self.assertNotIn(WARNING, text)


class ChipTitleTests(BannerCase):
    async def test_the_chip_title_is_the_whole_warning_sentence(self):
        for path in SCREENS:
            with self.subTest(path=path):
                dom = await self.dom(path, session=path != '/login')
                self.assertEqual(dom.chips[0]['attrs'].get('title'), WARNING, path)
                self.assertEqual(dom.banners[0]['text'].replace('닫기', '').strip(), WARNING, path)


class BannerScriptTests(BannerCase):
    async def test_the_banner_script_is_a_synchronous_external_script_in_head(self):
        for path in SCREENS:
            with self.subTest(path=path):
                dom = await self.dom(path, session=path != '/login')
                mine = [script for script in dom.scripts if script['attrs'].get('src') == '/static/insecure-banner.js']
                self.assertEqual(len(mine), 1, path)
                attrs = mine[0]['attrs']
                self.assertEqual(mine[0]['where'], 'head')
                # Synchronous so the <html> class is set before first paint (no flash of the wrong state).
                self.assertNotIn('defer', attrs)
                self.assertNotIn('async', attrs)
                self.assertNotEqual(attrs.get('type'), 'module')

    async def test_no_inline_script_or_style_exists_with_the_banner(self):
        for path in SCREENS:
            with self.subTest(path=path):
                dom = await self.dom(path, session=path != '/login')
                for script in dom.scripts:
                    self.assertIn('src', script['attrs'], path)
                    self.assertEqual(script['text'].strip(), '', path)
                self.assertEqual(dom.inline_styles, 0, path)

    async def test_the_script_is_served_from_static_under_the_page_csp(self):
        async with self.http.get(self.url + '/static/insecure-banner.js', headers={'Host': self.host}) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('javascript', response.headers['Content-Type'])
            self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
            self.assertNotIn('unsafe-inline', response.headers['Content-Security-Policy'])


def css_rules(text: str) -> list[tuple[str, dict]]:
    """``(selector, declarations)`` of the top-level rules of ``base.css``, in source order (``@media`` bodies skipped)."""
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    rules, depth, start, head, opened = [], 0, 0, '', 0
    for index, char in enumerate(text):
        if char == '{':
            if depth == 0:
                head, opened = text[start:index].strip(), index + 1
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                body = text[opened:index]
                if not head.startswith('@'):
                    declarations = dict(part.split(':', 1) for part in body.split(';') if ':' in part)
                    rules.append((head, {k.strip(): v.strip() for k, v in declarations.items()}))
                start = index + 1
    return rules


def compound(token: str) -> tuple[str, set[str]]:
    tag = re.match(r'[a-z]*', token).group(0)
    return tag, set(re.findall(r'\.([\w-]+)', token))


def displayed(rules, element: tuple[str, set[str]], ancestors: list[tuple[str, set[str]]]) -> bool:
    """Whether ``element`` (tag, classes) with these ``ancestors`` ends up with ``display`` other than ``none``."""
    best, shown = (-1, -1, -1), True
    for order, (selector_list, declarations) in enumerate(rules):
        if 'display' not in declarations:
            continue
        for selector in selector_list.split(','):
            tokens = selector.split()
            if any(re.search(r'[:\[>+~]', token) for token in tokens):
                continue  # pseudo-classes, attribute and combinator selectors are not this rule family
            tag, classes = compound(tokens[-1])
            if (tag and tag != element[0]) or not classes <= element[1]:
                continue
            remaining, ok = list(ancestors), True
            for token in reversed(tokens[:-1]):
                want_tag, want_classes = compound(token)
                while remaining and not ((not want_tag or want_tag == remaining[-1][0]) and want_classes <= remaining[-1][1]):
                    remaining.pop()
                if not remaining:
                    ok = False
                    break
                remaining.pop()
            if not ok:
                continue
            weight = (sum(len(compound(t)[1]) for t in tokens), sum(1 for t in tokens if compound(t)[0]), order)
            if weight > best:
                best, shown = weight, declarations['display'] != 'none'
    return shown


class BannerCssTests(unittest.TestCase):
    """S5: base.css makes exactly one of banner and chip visible, by the class the script puts on ``<html>``."""

    def setUp(self):
        self.rules = css_rules((STATIC / 'base.css').read_text(encoding='utf-8'))

    def visible(self, collapsed: bool) -> tuple[bool, bool]:
        html = ('html', {'wb-insecure-collapsed'} if collapsed else set())
        banner = displayed(self.rules, ('div', {'banner', 'insecure'}), [html, ('body', set())])
        chip = displayed(self.rules, ('span', {'insecure-chip'}), [html, ('body', set()), ('header', set())])
        return banner, chip

    def test_expanded_shows_the_banner_and_hides_the_chip(self):
        self.assertEqual(self.visible(False), (True, False))

    def test_collapsed_hides_the_banner_and_shows_the_chip(self):
        self.assertEqual(self.visible(True), (False, True))

    def test_the_two_states_never_show_both_or_neither(self):
        for collapsed in (False, True):
            banner, chip = self.visible(collapsed)
            self.assertNotEqual(banner, chip, collapsed)

    def test_without_the_script_the_page_is_the_expanded_state(self):
        # No class on <html> (no JS, blocked storage) is the "expanded" case computed above; the close button is
        # `hidden` in the markup, and `[hidden]` must not be overridden by an author `display` rule.
        for selector, declarations in self.rules:
            if 'banner-close' in selector:
                self.assertNotIn('display', declarations, selector)

    def test_hover_and_focus_show_the_whole_warning_as_a_css_popover(self):
        popovers = [(selector, d) for selector, d in self.rules
                    if 'insecure-chip' in selector and '::after' in selector]
        self.assertTrue(any(':hover' in selector and ':focus' in selector for selector, _ in popovers), popovers)
        self.assertTrue(all(d.get('content') == 'attr(title)' for _, d in popovers), popovers)


if __name__ == '__main__':
    unittest.main()
