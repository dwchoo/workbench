"""WBX-A1 global nav (seams S1 + S6): the real Hub's web app in-process, the HTML read with stdlib ``html.parser``.

``create_app(services, routes=Hub(...).web_routes())`` over ``aiohttp``'s ``TestServer``, real ``auth.json`` /
``hub.sqlite3`` in a temp dir. Expected values come from the SPEC "전역 nav" bullets and BRIEF WBX-AC-01, not
from the implementation. Run with ``discover -s tests/hub -p 'test_web_nav*.py'``.
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

import test_web_route_audit as audit  # noqa: E402

PASSWORD = 'correct horse battery'
VOID = frozenset({'meta', 'link', 'input', 'br', 'hr', 'img'})

# SPEC "전역 nav": the one link set every logged-in screen carries (label -> href).
NAV_LINKS = {'대시보드': '/dashboard', '요청': '/requests', '새 요청': '/requests/new', 'Board agent': '/board',
             '비밀번호': '/settings', 'API key': '/settings/keys', '감사 기록': '/settings/audit'}


class Dom(HTMLParser):
    """The header and its ``<nav aria-label="주요">``: links with their text/attributes, the settings
    ``<details>``, the status dot, the narrow-screen toggle, the badge slot and the logout form."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.has_header = False
        self.nav_labelled = 0
        self.links: list[dict] = []          # {href, text, attrs, in_details}
        self.summaries: list[dict] = []      # {text, attrs}
        self.details: list[dict] = []        # attrs of every <details> inside the nav
        self.dots: list[dict] = []           # attrs of every span.dot inside the nav
        self.hidden_labels: list[str] = []   # text of every .visually-hidden inside the nav
        self._hidden = None
        self.toggles: list[dict] = []        # header checkbox.nav-toggle attrs
        self.labels: list[dict] = []         # header label attrs (+ text)
        self.badges: list[dict] = []
        self.logout_forms: list[dict] = []
        self.main_text = ''
        self.header_text = ''
        self.order: list[str] = []           # header landmarks in document order
        self.header_attrs: list[dict] = []   # attributes of every element inside the header
        self._stack: list[str] = []
        self._in_header = 0
        self._in_nav = 0
        self._in_details = 0
        self._current: dict | None = None
        self._in_main = 0
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'header':
            self.has_header = True
            self._in_header += 1
        if self._in_header and tag != 'header':
            self.header_attrs.append(attrs)
        if self._in_header:
            if tag == 'nav' and attrs.get('aria-label') == '주요':
                self.nav_labelled += 1
                self._in_nav += 1
            if tag == 'nav' and attrs.get('aria-label') == '주요':
                self.order.append('nav')
            if tag == 'input' and attrs.get('type') == 'checkbox' and 'nav-toggle' in attrs.get('class', '').split():
                self.toggles.append(attrs)
                self.order.append('toggle')
            if attrs.get('id') == 'wb-badge':
                self.order.append('badge')
            if tag == 'form' and attrs.get('action') == '/logout':
                self.order.append('logout')
            if tag == 'label':
                self.order.append('label')
                self._current = {'attrs': attrs, 'text': ''}
                self.labels.append(self._current)
            if attrs.get('id') == 'wb-badge':
                self.badges.append(attrs)
            if tag == 'form' and attrs.get('action') == '/logout':
                self.logout_forms.append(attrs)
        if self._in_nav:
            if tag == 'details':
                self._in_details += 1
                self.details.append(attrs)
            if tag == 'a' and 'href' in attrs:
                self._current = {'href': attrs['href'], 'text': '', 'attrs': attrs, 'in_details': bool(self._in_details)}
                self.links.append(self._current)
            if tag == 'summary':
                self._current = {'text': '', 'attrs': attrs}
                self.summaries.append(self._current)
            if tag == 'span' and 'dot' in attrs.get('class', '').split():
                self.dots.append(attrs)
            if 'visually-hidden' in attrs.get('class', '').split():
                self.hidden_labels.append('')
                self._hidden = len(self.hidden_labels) - 1
        if tag == 'main':
            self._in_main += 1
        if tag not in VOID:
            self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag == 'header':
            self._in_header -= 1
        if tag == 'nav' and self._in_nav:
            self._in_nav -= 1
        if tag == 'details' and self._in_details:
            self._in_details -= 1
        if tag == 'main':
            self._in_main -= 1
        if tag in ('a', 'summary', 'label'):
            self._current = None
        if self._hidden is not None and tag == 'span':
            self._hidden = None
        if tag in self._stack:
            while self._stack and self._stack.pop() != tag:
                pass

    def handle_data(self, data):
        if self._hidden is not None:
            self.hidden_labels[self._hidden] += data
        if self._current is not None:
            self._current['text'] += data
        if self._in_main:
            self.main_text += data
        if self._in_header:
            self.header_text += data

    # -- facts ---------------------------------------------------------------------------------
    def nav_hrefs(self) -> set[str]:
        return {link['href'] for link in self.links}

    def link_by_href(self, href: str) -> list[dict]:
        return [link for link in self.links if link['href'] == href]

    def current(self) -> list[str]:
        """Text of every nav link carrying ``aria-current``."""
        return [link['text'].strip() for link in self.links if 'aria-current' in link['attrs']]

    def summary_current(self) -> list[str]:
        return [summary['text'].strip() for summary in self.summaries if 'aria-current' in summary['attrs']]


class NavCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix='wb-nav-')).resolve()
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
        self.session = self.services.sessions.create('127.0.0.1', 'nav', time.time(), generation=self.generation)

    async def fetch(self, path: str, *, session: bool = True) -> tuple[int, str, str]:
        """``(status, content type, text)`` of a GET."""
        headers = {'Host': self.host}
        if session:
            headers['Cookie'] = f'wb_hub_session={self.session.token}'
        async with self.http.get(self.url + path, headers=headers, allow_redirects=False) as response:
            return response.status, response.headers.get('Content-Type', ''), await response.text()

    async def dom(self, path: str, *, session: bool = True) -> Dom:
        status, _, text = await self.fetch(path, session=session)
        self.assertEqual(status, 200, f'{path}: {text[:200]}')
        return Dom(text)


# Screens a logged-in owner reaches from the nav and from each other (SPEC "웹 route와 화면").
SCREENS = ('/dashboard', '/requests', '/requests/new', '/board', '/settings', '/settings/keys', '/settings/audit')


class NavLinkSetTests(NavCase):
    async def test_every_login_screen_carries_the_same_nav_link_set(self):
        for path in SCREENS:
            with self.subTest(path=path):
                dom = await self.dom(path) if path != '/board' else await self._board()
                self.assertEqual(dom.nav_labelled, 1, path)
                self.assertEqual(dom.nav_hrefs(), set(NAV_LINKS.values()), path)
                self.assertEqual({link['text'].strip() for link in dom.links}, set(NAV_LINKS), path)

    async def _board(self) -> Dom:
        # No Board set on this Hub: the page is the 503 "unavailable" page, still a logged-in screen.
        status, _, text = await self.fetch('/board')
        self.assertIn(status, (200, 503))
        return Dom(text)


class NavCurrentTests(NavCase):
    async def test_only_the_link_of_the_current_screen_carries_aria_current(self):
        rid, project = str(uuid.uuid4()), str(uuid.uuid4())
        expected = {
            '/dashboard': ['대시보드'],
            f'/projects/{project}': ['대시보드'],  # unknown project: the 404 page is still a logged-in screen
            '/requests': ['요청'],
            f'/requests/{rid}': ['요청'],
            '/requests/new': ['새 요청'],
            '/board': ['Board agent'],  # WBX-D1: the restart confirm page is gone
            '/settings': ['비밀번호'],
            '/settings/keys': ['API key'],
            '/settings/audit': ['감사 기록'],
        }
        for path, current in expected.items():
            with self.subTest(path=path):
                status, content_type, text = await self.fetch(path)
                self.assertTrue(content_type.startswith('text/html'), (status, content_type, text[:100]))
                dom = Dom(text)
                self.assertEqual(sorted(dom.current()), sorted(current), path)
                for link in dom.links:
                    if 'aria-current' in link['attrs']:
                        self.assertEqual(link['attrs']['aria-current'], 'page')


class NavSettingsMenuTests(NavCase):
    async def test_settings_links_live_in_one_details_menu_and_only_there(self):
        for path in SCREENS:
            with self.subTest(path=path):
                status, _, text = await self.fetch(path)
                dom = Dom(text)
                self.assertEqual(len(dom.details), 1, path)
                self.assertEqual(dom.details[0].get('class', '').split(), ['nav-settings'], path)
                self.assertEqual([summary['text'].strip() for summary in dom.summaries], ['설정'], path)
                inside = {link['text'].strip(): link['href'] for link in dom.links if link['in_details']}
                self.assertEqual(inside, {'비밀번호': '/settings', 'API key': '/settings/keys',
                                          '감사 기록': '/settings/audit'}, path)
                outside = {link['text'].strip(): link['href'] for link in dom.links if not link['in_details']}
                self.assertEqual(outside, {'대시보드': '/dashboard', '요청': '/requests', '새 요청': '/requests/new',
                                           'Board agent': '/board'}, path)

    async def test_the_settings_summary_is_current_on_settings_screens_only(self):
        for path in SCREENS:
            with self.subTest(path=path):
                dom = Dom((await self.fetch(path))[2])
                self.assertEqual(dom.summary_current(), ['설정'] if path.startswith('/settings') else [], path)


class NavAbsentWithoutSessionTests(NavCase):
    async def test_login_screens_have_no_nav_and_no_logout(self):
        status, _, text = await self.fetch('/login', session=False)
        self.assertEqual(status, 200)
        dom = Dom(text)
        self.assertEqual((dom.has_header, dom.nav_labelled, dom.links, dom.logout_forms), (True, 0, [], []))
        # A refused login (wrong password) re-renders the login screen: still no nav.
        login_cookie, token = await self._login_form()
        async with self.http.post(self.url + '/login', data={'login_token': token, 'password': 'wrong password'},
                                  headers={'Host': self.host, 'Origin': self.url,
                                           'Cookie': f'wb_hub_login={login_cookie}'}, allow_redirects=False) as response:
            self.assertEqual(response.status, 403)
            dom = Dom(await response.text())
        self.assertEqual((dom.nav_labelled, dom.links, dom.logout_forms), (0, [], []))

    async def _login_form(self) -> tuple[str, str]:
        async with self.http.get(self.url + '/login', headers={'Host': self.host}) as response:
            cookie = next(value.split(';')[0].split('=', 1)[1] for value in response.headers.getall('Set-Cookie')
                          if value.startswith('wb_hub_login='))
            return cookie, re.search(r'name="login_token" value="([^"]+)"', await response.text()).group(1)

    async def test_a_page_route_without_a_session_redirects_instead_of_drawing_a_nav(self):
        status, _, text = await self.fetch('/requests', session=False)
        self.assertEqual(status, 303)
        self.assertNotIn('aria-label="주요"', text)


class NavEveryPageRouteTests(NavCase):
    """Walk the route list (not a hand-written URL list): every logged-in HTML page route carries the nav."""

    async def test_every_html_page_route_has_the_nav_and_no_user_text_in_it(self):
        params = {**audit.PARAMS, 'rid': str(uuid.uuid4())}
        seen = set()
        for (method, route), access in sorted(audit.route_marks(self.app).items()):
            if method != 'GET' or access != audit.SESSION or route.startswith('/static'):
                continue
            path = route.format(**params) + '?channel=agent&project=NAVLEAK&q=NAVLEAK'
            with self.subTest(route=route):
                status, content_type, text = await self.fetch(path)
                if not content_type.startswith('text/html'):
                    continue  # raw/artifact downloads and bare-text refusals are not page frames
                seen.add(route)
                dom = Dom(text)
                self.assertEqual(dom.nav_labelled, 1, (status, route))
                self.assertEqual(dom.nav_hrefs(), set(NAV_LINKS.values()), route)
                self.assertNotIn('NAVLEAK', dom.header_text, route)
                self.assertNotIn('NAVLEAK', repr(dom.header_attrs), route)
        # '/' (303) and '/dashboard', '/settings*' (3), '/projects/*' (2), '/requests*' (new, list, one, edit, cancel, full), '/board*' (2)
        self.assertGreaterEqual(len(seen), 14, sorted(seen))

    async def test_the_nav_frame_has_the_brand_badge_slot_and_logout(self):
        dom = await self.dom('/requests')
        self.assertEqual(len(dom.badges), 1)
        self.assertEqual(len(dom.logout_forms), 1)


DOTS = {'on': '실행 중', 'off': '중지', 'error': '오류'}  # SPEC "Board agent 상태 점": class value -> title


class NavBoardDotTests(NavCase):
    async def dot_of(self, path: str = '/requests') -> list[dict]:
        return (await self.dom(path)).dots

    async def test_the_dot_shows_the_state_the_service_reports(self):
        for state, title in DOTS.items():
            self.services.board_state = lambda state=state: state
            for path in ('/requests', '/settings'):
                with self.subTest(state=state, path=path):
                    dots = await self.dot_of(path)
                    self.assertEqual(len(dots), 1)
                    self.assertEqual(dots[0].get('class', '').split(), ['dot', f'dot-{state}'])
                    self.assertEqual(dots[0].get('title'), title)

    async def test_the_dot_sits_in_the_board_agent_link(self):
        self.services.board_state = lambda: 'on'
        dom = await self.dom('/dashboard')
        board = dom.link_by_href('/board')
        self.assertEqual(len(board), 1)
        self.assertEqual(board[0]['text'].strip(), 'Board agent')
        self.assertEqual(len(dom.dots), 1)  # one dot in the whole nav

    async def test_without_a_board_the_dot_reads_off(self):
        dots = await self.dot_of()
        self.assertEqual([(dot.get('class'), dot.get('title')) for dot in dots], [('dot dot-off', '중지')])

    async def test_a_failing_or_unknown_state_reads_error_and_never_reaches_the_markup(self):
        def broken() -> str:
            raise RuntimeError('boom NAVLEAK')
        for source in (broken, lambda: 'running"><script>NAVLEAK', lambda: None):
            self.services.board_state = source
            dom = await self.dom('/requests')
            self.assertEqual([dot.get('class') for dot in dom.dots], ['dot dot-error'])
            self.assertNotIn('NAVLEAK', repr(dom.header_attrs) + dom.header_text)


class FakeBoard:
    """What ``Hub.board`` offers the nav (WBX-D1): ``BoardSet.dot()``, here over real ``BoardSet`` views."""

    def __init__(self, dot):
        self.value = dot

    def dot(self):
        return self.value


def board_with(base: Path, agents: list[str], shells: list[str] = (), invalid: bool = False):
    """A real ``BoardSet`` (no process) whose agents and shells are in the given states."""
    from support.board_stub import RecorderLauncher
    from workbench.hub.board import registry
    from workbench.hub.board.set import BoardSet, Terminal
    launcher = RecorderLauncher(base / f'board-{len(list(base.iterdir()))}')
    if invalid:
        (Path(launcher.state_dir) / 'board-agents').mkdir(mode=0o700)
        (Path(launcher.state_dir) / 'board-agents' / 'registry.json').write_text('[]')
    board = BoardSet(launcher)
    if not invalid:
        board.agents.clear()
        for index, state in enumerate(agents):
            agent_id = registry.new_id('agent')
            board.agents[agent_id] = Terminal(agent_id, 'agent', f'agent-{index + 1}', home=registry.agent_home(agent_id),
                                              state=state)
        for index, state in enumerate(shells):
            shell_id = registry.new_id('shell')
            board.shells[shell_id] = Terminal(shell_id, 'shell', f'shell-{index + 1}', state=state)
    return board


class HubBoardStateTests(unittest.TestCase):
    """web-board-ux SPEC s4 "Hub 상태·정지·nav": error when an agent is failed/isolation_failed or the registry is
    invalid; on while an agent is starting/running; else off. Shells never change the dot. (v1: the one agent
    channel decided; an unknown state was error, now only the two failure states are.)"""

    def setUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix='wb-navhub-')).resolve()
        self.addCleanup(shutil.rmtree, self.base_dir, True)
        self.hub = server.Hub(HubLayout(self.base_dir / 'hub'), 'detached')
        self.addCleanup(self.hub.inbox.close)

    def state_with(self, *agents: str, shells: tuple = ('not_started',), invalid: bool = False) -> str:
        self.hub.board = board_with(self.base_dir, list(agents), list(shells), invalid)
        return self.hub.board_state()

    def test_no_set_and_a_never_started_set_are_off(self):
        self.assertIsNone(self.hub.board)
        self.assertEqual(self.hub.board_state(), 'off')
        self.assertEqual(self.state_with('not_started'), 'off')
        self.assertEqual(self.state_with(), 'off', 'no agent at all')

    def test_starting_and_running_agent_are_on(self):
        self.assertEqual(self.state_with('starting'), 'on')
        self.assertEqual(self.state_with('running'), 'on')
        self.assertEqual(self.state_with('stopped', 'running'), 'on', 'any live agent')

    def test_stopped_exited_and_not_started_agent_are_off(self):
        for state in ('not_started', 'stopped', 'exited'):
            with self.subTest(state=state):
                self.assertEqual(self.state_with(state, shells=('running',)), 'off')

    def test_failures_are_error(self):
        for state in ('isolation_failed', 'failed'):
            with self.subTest(state=state):
                self.assertEqual(self.state_with(state), 'error')
                self.assertEqual(self.state_with('running', state), 'error', 'one failed agent among running ones')
        self.assertEqual(self.state_with(invalid=True), 'error', 'registry_invalid')

    def test_the_shells_do_not_change_the_agent_dot(self):
        self.assertEqual(self.state_with('running', shells=('failed',)), 'on')
        self.assertEqual(self.state_with('stopped', shells=('running', 'failed')), 'off')

    def test_an_invalid_board_config_is_error(self):
        self.hub.config_info = {'state': 'config_invalid'}
        self.assertEqual(self.hub.board_state(), 'error')


class HubWiresBoardStateIntoTheWebServicesTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_listener_services_report_the_hub_board_state(self):
        base = Path(tempfile.mkdtemp(prefix='wb-navwire-')).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        config_home = base / 'cfg'
        (config_home / 'omp-workbench').mkdir(parents=True, mode=0o700)
        (config_home / 'omp-workbench' / 'hub.toml').write_text('[web]\nenabled = true\nbind = "127.0.0.1"\nport = 0\n')
        hub = server.Hub(HubLayout(base / 'hub'), 'detached')
        self.addCleanup(hub.inbox.close)
        auth.PasswordStore(hub.layout.state_dir).set_password(PASSWORD)
        hub.load_web({'XDG_CONFIG_HOME': str(config_home), 'HOME': str(base)})
        self.addAsyncCleanup(hub.web.stop)
        await hub.web.start()
        self.assertEqual(hub.web.services.board_state(), 'off')
        hub.board = FakeBoard('on')
        self.assertEqual(hub.web.services.board_state(), 'on')


BASE_CSS = REPO / 'src/workbench/hub/web/static/base.css'


class NavNarrowScreenTests(NavCase):
    """JS-free collapse: a checkbox + label before the nav (CSS ``:checked ~ nav``). Rendering itself is the
    user's browser check (pending); here only the markup and the stylesheet text are pinned."""

    async def test_a_checkbox_toggle_and_its_label_come_before_the_nav(self):
        for path in ('/dashboard', '/settings/keys'):
            with self.subTest(path=path):
                dom = await self.dom(path)
                self.assertEqual([toggle.get('id') for toggle in dom.toggles], ['wb-nav-toggle'])
                self.assertEqual([label['attrs'].get('for') for label in dom.labels], ['wb-nav-toggle'])
                self.assertEqual(dom.order.index('toggle') < dom.order.index('label') < dom.order.index('nav'), True)

    async def test_badge_and_logout_are_outside_the_collapsing_nav(self):
        dom = await self.dom('/requests')
        self.assertEqual(sorted(dom.order), ['badge', 'label', 'logout', 'nav', 'toggle'])
        self.assertFalse({'wb-badge'} & {link['attrs'].get('id') for link in dom.links})
        self.assertEqual(dom.logout_forms[0].get('method'), 'post')
        self.assertNotIn('로그아웃', ' '.join(link['text'] for link in dom.links))

    def test_the_stylesheet_collapses_the_nav_at_45rem_without_javascript(self):
        css = BASE_CSS.read_text(encoding='utf-8')
        block = re.search(r'@media\s*\(max-width:\s*45rem\)\s*\{(.*?)\n\}', css, re.S)
        self.assertIsNotNone(block, 'no @media (max-width: 45rem) block')
        self.assertIn('.nav-toggle:checked', block.group(1))
        self.assertIn('nav', block.group(1))


    async def test_the_state_is_also_text_so_the_dot_is_not_colour_only(self):
        """D3 (accessibility): a visually hidden Korean label with the same words as the dot's title."""
        for state, title in DOTS.items():
            self.services.board_state = lambda state=state: state
            with self.subTest(state=state):
                dom = await self.dom('/requests')
                link = dom.link_by_href('/board')[0]
                self.assertEqual(link['text'].strip(), 'Board agent')  # the label sits next to the link
                self.assertEqual(dom.hidden_labels, [title])

    def test_the_stylesheet_defines_the_visually_hidden_utility(self):
        css = BASE_CSS.read_text(encoding='utf-8')
        rule = re.search(r'\.visually-hidden\s*\{([^}]*)\}', css)
        self.assertIsNotNone(rule, 'no .visually-hidden rule')
        self.assertNotIn('display: none', rule.group(1))
        self.assertNotIn('visibility: hidden', rule.group(1))
        self.assertIn('position: absolute', rule.group(1))


if __name__ == '__main__':
    unittest.main()
