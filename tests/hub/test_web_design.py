"""WBX-C1 CSS design system. Seams S1+S6 (the real Hub web app in-process, HTML read with stdlib ``html.parser``) for
what every screen's markup shares, and S5 (static reads of ``src/workbench/hub/web/static/*.css``) for tokens, dark
rules, contrast and "no external resource". How the screens look is the user's Chrome check (S7), not tested here.

Expected values come from the ticket (WBX-AC-19), SPEC s2 "트랙 C 선택" (token names taken from mockup A) and the WCAG
contrast formula, not from the implementation. Run with ``discover -s tests/hub -p 'test_web_design.py'``.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

import test_web_pages_board as board_tests  # noqa: E402
import test_web_pages_requests as requests_tests  # noqa: E402  (module import: its test classes stay uncollected here)
from workbench.hub.inbox.service import InboxService, ProjectRef  # noqa: E402
from workbench.hub.web.app import board_routes  # noqa: E402
from workbench.hub.web.pages import requests as request_pages  # noqa: E402

STATIC = REPO / 'src/workbench/hub/web/static'
SHARED_STYLESHEETS = ['/static/base.css', '/static/app.css']
BOARD_STYLESHEETS = ['/static/vendor/xterm/xterm.css', '/static/board.css']
VOID = frozenset({'meta', 'link', 'input', 'br', 'hr', 'img'})


class Dom(HTMLParser):
    """Every element with its attributes and ancestors (tag, classes), plus stylesheet links in order."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict]] = []
        self.elements: list[tuple[str, dict, tuple]] = []   # tag, attrs, ancestors (outermost first)
        self.stylesheets: list[str] = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs, tuple(self.stack)))
        if tag == 'link' and attrs.get('rel') == 'stylesheet':
            self.stylesheets.append(attrs['href'])
        if tag not in VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def find(self, tag: str) -> list[tuple[dict, tuple]]:
        return [(attrs, ancestors) for name, attrs, ancestors in self.elements if name == tag]


def classes(attrs: dict) -> set[str]:
    return set((attrs.get('class') or '').split())


class DesignCase(requests_tests.RequestsCase):
    """RequestsCase (inbox + request pages over two projects), with the Board page's route added."""

    insecure_warning = True

    async def asyncSetUp(self):
        self.base_temp = tempfile.TemporaryDirectory(prefix='wb-design-')
        self.addCleanup(self.base_temp.cleanup)
        base = Path(self.base_temp.name).resolve()
        self.root_a, self.a = requests_tests.make_project(base, 'alpha')
        self.root_b, self.b = requests_tests.make_project(base, 'beta')
        self.refs = [ProjectRef(self.a, self.root_a, 'alpha'), ProjectRef(self.b, self.root_b, 'beta')]
        self.events, self.backend_up = [], False
        self.inbox = InboxService(on_committed=self.events.append, backend_running=lambda project_id: self.backend_up)
        self.addCleanup(self.inbox.close)
        self.inbox.set_projects(self.refs)
        self.board = board_tests.FakeBoard()
        self.routes = (request_pages.routes(self.inbox, lambda: list(self.refs)), board_routes(lambda: self.board))
        await requests_tests.web_auth.WebAppCase.asyncSetUp(self)
        self.cookies = {'wb_hub_session': await self.session_cookie()}

    async def screens(self) -> dict[str, Dom]:
        """Every kind of screen: the login form, the dashboard, request pages, settings pages and the Board page."""
        request_id = self.add('Title', 'Body')
        self.services.audit.record('login_success', 'owner', '10.0.0.5', {'delay_seconds': 0}, now=self.now[0])
        await self.submit_key()
        out = {'/login': Dom((await self.get('/login')).body_text)}
        for path in ('/dashboard', '/requests', '/requests/new', f'/requests/{request_id}', '/settings', '/settings/keys',
                     '/settings/audit', '/board'):
            response = await self.get(path, cookies=self.cookies)
            self.assertEqual(response.status, 200, f'{path}: {response.body_text[:200]}')
            out[path] = Dom(response.body_text)
        return out

    async def submit_key(self):
        _, page = await self.page('/settings/keys')
        csrf = page.form('/settings/keys')['fields']['csrf']
        response = await self.post('/settings/keys', {'csrf': csrf, 'name': 'laptop', 'grade': 'requester',
                                                       'scope': 'all', 'expires_days': ''}, cookies=self.cookies)
        self.assertEqual(response.status, 200, response.body_text[:300])


class SharedMarkupTests(DesignCase):
    async def test_every_screen_loads_the_same_stylesheets_in_the_same_order(self):
        for path, dom in (await self.screens()).items():
            with self.subTest(path=path):
                extra = BOARD_STYLESHEETS if path == '/board' else []  # the Board terminal adds its own two
                self.assertEqual(dom.stylesheets, SHARED_STYLESHEETS + extra, path)

    async def test_every_table_is_a_data_table_inside_a_scroll_container(self):
        seen = 0
        for path, dom in (await self.screens()).items():
            for attrs, ancestors in dom.find('table'):
                seen += 1
                with self.subTest(path=path):
                    self.assertIn('data', classes(attrs), path)
                    parent_tag, parent_attrs = ancestors[-1]
                    self.assertEqual(parent_tag, 'div', path)
                    self.assertIn('table-wrap', classes(parent_attrs), path)
        self.assertGreaterEqual(seen, 3, 'request list, key list and audit list each have a table')

    async def test_every_submit_button_uses_the_button_component_and_no_link_keeps_the_old_button_class(self):
        submits = links = 0
        for path, dom in (await self.screens()).items():
            for name, attrs, _ in dom.elements:
                if name == 'button' and attrs.get('type') == 'submit':
                    submits += 1
                    with self.subTest(path=path, element='submit button'):
                        self.assertIn('btn', classes(attrs))
                if name == 'a' and classes(attrs) & {'btn', 'button'}:
                    links += 1
                    with self.subTest(path=path, element=attrs.get('href')):
                        self.assertIn('btn', classes(attrs))
                        self.assertNotIn('button', classes(attrs), 'the old a.button class was replaced by .btn')
        self.assertGreaterEqual(submits, 6)
        self.assertGreaterEqual(links, 3, 'the request page offers edit, cancel and full-view links')

    async def test_every_label_is_a_form_field_or_a_choice(self):
        seen = 0
        for path, dom in (await self.screens()).items():
            for attrs, _ in dom.find('label'):
                if 'nav-toggle-label' in classes(attrs):
                    continue
                seen += 1
                with self.subTest(path=path):
                    self.assertTrue(classes(attrs) & {'field', 'check'}, (path, attrs))
        self.assertGreaterEqual(seen, 6)

    async def test_the_login_screen_is_a_card_with_the_button_and_field_components(self):
        dom = (await self.screens())['/login']
        body = next(attrs for name, attrs, _ in dom.elements if name == 'body')
        self.assertIn('page-login', classes(body))
        cards = [(attrs, ancestors) for attrs, ancestors in dom.find('section') if 'card' in classes(attrs)]
        self.assertEqual(len(cards), 1)
        self.assertIn('login-card', classes(cards[0][0]))
        self.assertEqual(cards[0][1][-1][0], 'main')
        inside = [(name, attrs) for name, attrs, ancestors in dom.elements if cards[0][0] in [a for _, a in ancestors]]
        buttons = [attrs for name, attrs in inside if name == 'button']
        self.assertEqual([classes(attrs) >= {'btn', 'btn-primary', 'btn-block'} for attrs in buttons], [True])

    async def test_a_request_state_is_a_chip_with_its_state_kind(self):
        self.add('Title', 'Body')
        _, page = await self.page('/requests')
        chips = [attrs for tag, attrs in page.elements if tag == 'span' and 'chip' in classes(attrs)]
        self.assertEqual([classes(attrs) - {'chip'} for attrs in chips], [{'s-new'}])
        self.assertEqual(page.data_rows()[0][3], '신규')  # the visible text is unchanged


# -- S5: the stylesheets as text ---------------------------------------------------------------------------------
def strip_comments(text: str) -> str:
    return re.sub(r'/\*.*?\*/', '', text, flags=re.S)


def blocks(text: str) -> list[tuple[str, str]]:
    """``(prelude, body)`` of every top-level block of ``text``; an at-rule body is returned as written."""
    text, depth, start, head, out = strip_comments(text), 0, 0, '', []
    for index, char in enumerate(text):
        if char == '{':
            if depth == 0:
                head, opened = text[start:index].strip(), index + 1
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                out.append((head, text[opened:index]))
                start = index + 1
    return out


def declarations(body: str) -> dict[str, str]:
    return {name.strip(): value.strip() for name, value in
            (part.split(':', 1) for part in body.split(';') if ':' in part and '{' not in part)}


def all_rules(text: str) -> list[tuple[str, str]]:
    """Flat ``(selector, body)`` list, descending into ``@media`` blocks."""
    rules = []
    for head, body in blocks(text):
        if head.startswith('@media'):
            rules.extend(all_rules(body))
        elif not head.startswith('@'):
            rules.append((head, body))
    return rules


def own_css() -> dict[str, str]:
    return {path.name: path.read_text(encoding='utf-8') for path in sorted(STATIC.glob('*.css'))}


def token_blocks(css: str) -> dict[str, dict[str, str]]:
    """``{label: {--token: value}}`` for the light ``:root``, the dark media block and the ``data-theme="dark"`` block."""
    found = {'light': {}, 'media-dark': {}, 'attr-dark': {}}
    for head, body in blocks(css):
        if head == ':root':
            found['light'].update({k: v for k, v in declarations(body).items() if k.startswith('--')})
        elif head.startswith('@media') and 'prefers-color-scheme' in head and 'dark' in head:
            for inner_head, inner in blocks(body):
                if inner_head.startswith(':root'):
                    found['media-dark'].update({k: v for k, v in declarations(inner).items() if k.startswith('--')})
        elif re.fullmatch(r':root\[data-theme=["\']?dark["\']?\]', head):
            found['attr-dark'].update({k: v for k, v in declarations(body).items() if k.startswith('--')})
    return found


# Token names SPEC s2 "트랙 C 선택" takes over from mockup A (colour tokens come in a light and a dark value).
COLOUR_TOKENS = ['bg', 'surface', 'surface-2', 'text', 'muted', 'border', 'border-strong', 'accent', 'accent-fg',
                 'accent-hover', 'link', 'focus', 'ok', 'danger', 'err-bg', 'err-fg', 'err-bd', 'warn-bg', 'warn-fg',
                 'warn-bd', 'hdr-bg', 'hdr-fg', 'hdr-muted', 'hdr-bd', 'badge-bg', 'badge-fg', 'shadow'] + [
    f'chip-{kind}-{part}' for kind in ('new', 'reviewing', 'needs', 'ready', 'running', 'closed', 'unknown')
    for part in ('bg', 'fg')]
SCALE_TOKENS = ['font', 'mono', 'fs', 'gap', 'pad', 'gutter', 'radius', 'radius-lg', 'card-shadow', 'maxw']
HEX = re.compile(r'^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')


def luminance(value: str) -> float:
    digits = value.lstrip('#')
    digits = ''.join(c * 2 for c in digits) if len(digits) == 3 else digits
    channels = [int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(first: str, second: str) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# Text on a background, as the components pair them (WBX-AC: text contrast >= 4.5:1 in light and dark).
TEXT_PAIRS = [('text', 'bg'), ('text', 'surface'), ('text', 'surface-2'), ('muted', 'bg'), ('muted', 'surface'),
              ('muted', 'surface-2'), ('link', 'bg'), ('link', 'surface'), ('link', 'surface-2'),
              ('accent-fg', 'accent'), ('accent-fg', 'accent-hover'), ('accent', 'surface'), ('danger', 'surface'),
              ('danger', 'surface-2'), ('ok', 'surface'), ('err-fg', 'err-bg'), ('warn-fg', 'warn-bg'),
              ('hdr-fg', 'hdr-bg'), ('hdr-muted', 'hdr-bg'), ('badge-fg', 'badge-bg')] + [
    (f'chip-{kind}-fg', f'chip-{kind}-bg') for kind in ('new', 'reviewing', 'needs', 'ready', 'running', 'closed',
                                                         'unknown')]


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tokens = token_blocks(own_css()['base.css'])

    def test_base_css_defines_every_token_of_mockup_a(self):
        light = self.tokens['light']
        missing = [name for name in COLOUR_TOKENS + SCALE_TOKENS if f'--{name}' not in light]
        self.assertEqual(missing, [])

    def test_every_colour_token_has_a_dark_value_in_the_media_query_and_the_data_theme_override(self):
        for label in ('media-dark', 'attr-dark'):
            with self.subTest(label=label):
                self.assertTrue(self.tokens[label], f'no {label} token block in base.css')
                self.assertEqual([n for n in COLOUR_TOKENS if f'--{n}' not in self.tokens[label]], [])

    def test_the_two_dark_blocks_do_not_drift_apart(self):
        self.assertEqual(self.tokens['media-dark'], self.tokens['attr-dark'])

    def test_the_dark_media_query_leaves_an_explicit_light_choice_alone(self):
        for head, body in blocks(own_css()['base.css']):
            if head.startswith('@media') and 'prefers-color-scheme' in head:
                self.assertIn(':root:not([data-theme="light"])', body)
        light_root = {k: v for head, body in blocks(own_css()['base.css']) if head == ':root'
                      for k, v in declarations(body).items()}
        self.assertEqual(light_root.get('color-scheme'), 'light')

    def test_text_pairs_reach_4_5_to_1_in_light_and_dark(self):
        for label, tokens in (('light', self.tokens['light']),
                              ('dark', {**self.tokens['light'], **self.tokens['media-dark']})):
            for first, second in TEXT_PAIRS:
                with self.subTest(theme=label, pair=f'{first} on {second}'):
                    for name in (first, second):
                        self.assertRegex(tokens.get(f'--{name}', ''), HEX, f'--{name} must be a plain hex colour')
                    self.assertGreaterEqual(contrast(tokens[f'--{first}'], tokens[f'--{second}']), 4.5)


class ComponentCssTests(unittest.TestCase):
    def setUp(self):
        self.css = own_css()
        self.rules = [rule for text in self.css.values() for rule in all_rules(text)]
        self.selectors = ' , '.join(selector for selector, _ in self.rules)

    def test_the_component_classes_the_markup_uses_have_rules(self):
        for needle in ('.btn', '.btn-primary', '.btn-block', '.card', '.login-card', 'table.data', '.table-wrap',
                       '.chip', '.field', '.check', '.alert', '.notice', '.page-login'):
            with self.subTest(selector=needle):
                self.assertIn(needle, self.selectors)

    def test_a_table_overflows_inside_its_container_not_the_page(self):
        wrap = [declarations(body) for selector, body in self.rules if selector.strip() == '.table-wrap']
        self.assertTrue(any(d.get('overflow-x') == 'auto' for d in wrap), wrap)

    def test_keyboard_focus_is_visible_and_never_turned_off(self):
        self.assertTrue(any(':focus-visible' in selector and 'outline' in body for selector, body in self.rules))
        for name, text in self.css.items():
            for selector, body in all_rules(text):
                outline = declarations(body).get('outline', '')
                self.assertNotRegex(outline, r'^(none|0)\b', f'{name}: {selector.strip()} removes the focus outline')

    def test_colours_are_tokens_outside_the_token_blocks(self):
        literal = re.compile(r'#[0-9a-fA-F]{3,8}\b|\b(rgb|rgba|hsl|hsla)\(')
        for name, text in self.css.items():
            for head, body in blocks(text):
                if head.startswith(':root') or (head.startswith('@media') and 'prefers-color-scheme' in head):
                    continue
                self.assertIsNone(literal.search(head + body), f'{name}: colour literal outside the tokens: {head}')

    def test_no_external_resource_import_font_or_url(self):
        for name, text in self.css.items():
            with self.subTest(file=name):
                stripped = strip_comments(text)
                self.assertNotRegex(stripped, r'(?i)@import|@font-face|url\(|https?:|//[a-z0-9.-]+\.[a-z]')

    def test_the_theme_follows_the_system_unless_data_theme_says_otherwise(self):
        base = self.css['base.css']
        self.assertIn('prefers-color-scheme: dark', base)
        self.assertRegex(base, r':root\[data-theme="dark"\]')


if __name__ == '__main__':
    unittest.main()
