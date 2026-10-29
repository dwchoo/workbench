"""WBX-A1 global nav, independent adversarial verification (seams S1 + S6).

The real Hub's web app in-process (``create_app(services, routes=Hub(...).web_routes())`` over ``aiohttp``'s
``TestServer``), real ``auth.json`` / ``hub.sqlite3`` / inbox in a temp dir, HTML read with a strict stdlib
``html.parser`` tree. Expected values come from SPEC s2 "전역 nav", ticket WBX-A1 and BRIEF WBX-AC-01, not from the
implementation: the route list is enumerated from ``test_web_route_audit.route_marks`` (not a hand list), nested paths
and POST result pages are walked with real requests/keys, and the nav markup is compared across every screen.
Run with ``discover -s tests/hub -p 'test_web_nav*.py'``.
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
from workbench.hub.inbox.service import Principal, ProjectRef  # noqa: E402
from workbench.hub.paths import HubLayout  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402

import test_web_pages_requests as request_support  # noqa: E402  (module import: its test classes stay uncollected)
import test_web_route_audit as audit  # noqa: E402

PASSWORD = 'correct horse battery'
LEAK = 'NAVLEAK'
HOSTILE = f'<script>{LEAK}</script>"><b onmouseover=x>'
VOID = frozenset({'meta', 'link', 'input', 'br', 'hr', 'img', 'base', 'col', 'area', 'source', 'wbr'})
NAV_LABEL_HREF = {'대시보드': '/dashboard', '요청': '/requests', '새 요청': '/requests/new', 'Board agent': '/board',
                  '비밀번호': '/settings', 'API key': '/settings/keys', '감사 기록': '/settings/audit'}
RAW_KEY = re.compile(r'wbk_[a-z2-7]{10}_[A-Za-z0-9_-]{43}')
CSRF_VALUE = re.compile(r'name="csrf" value="([^"]+)"')


class El:
    def __init__(self, tag: str, attrs: dict, parent: 'El | None'):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[El] = []
        self.text = ''

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, tag: str, **attrs) -> list['El']:
        return [e for e in self.walk() if e.tag == tag and all(e.attrs.get(k) == v for k, v in attrs.items())]

    def all_text(self) -> str:
        return self.text + ''.join(c.all_text() for c in self.children)

    def classes(self) -> list[str]:
        return (self.attrs.get('class') or '').split()


class Tree(HTMLParser):
    """Strict tree: records every unbalanced / unexpected end tag instead of repairing it silently."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.root = El('#root', {}, None)
        self.cur = self.root
        self.problems: list[str] = []
        self.inline_scripts = 0
        self.feed(text)
        self.close()
        if self.cur is not self.root:
            self.problems.append(f'unclosed <{self.cur.tag}>')

    def handle_starttag(self, tag, attrs):
        el = El(tag, dict(attrs), self.cur)
        self.cur.children.append(el)
        if tag not in VOID:
            self.cur = el

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(El(tag, dict(attrs), self.cur))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.cur.tag != tag:
            self.problems.append(f'end </{tag}> while <{self.cur.tag}> is open')
            probe = self.cur
            while probe is not None and probe.tag != tag:
                probe = probe.parent
            if probe is not None and probe.parent is not None:
                self.cur = probe.parent
            return
        self.cur = self.cur.parent

    def handle_data(self, data):
        self.cur.text += data

    # -- facts ---------------------------------------------------------------------------------
    def header(self) -> El | None:
        found = self.root.find('header')
        return found[0] if len(found) == 1 else None

    def navs(self) -> list[El]:
        return [e for e in self.root.walk() if e.tag == 'nav' and e.attrs.get('aria-label') == '주요']

    def current(self) -> list[El]:
        return [e for e in self.root.walk() if 'aria-current' in e.attrs]


def header_html(text: str) -> str:
    match = re.search(r'<header>.*?</header>', text, re.S)
    return match.group(0) if match else ''


class NavBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix='wb-navind-')).resolve()
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
        self.root_a, self.project = request_support.make_project(self.base_dir, 'alpha')
        self.hub.inbox.set_projects([ProjectRef(self.project, self.root_a, 'alpha')])
        self.app = create_app(self.services, routes=self.hub.web_routes())
        self.server = test_utils.TestServer(self.app, host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.http = ClientSession(cookie_jar=DummyCookieJar())
        self.addAsyncCleanup(self.http.close)
        self.host = f'127.0.0.1:{self.server.port}'
        self.url = f'http://{self.host}'
        self.session = self.services.sessions.create('127.0.0.1', 'navind', time.time(), generation=self.generation)

    def headers(self, session: bool = True, **extra) -> dict:
        headers = {'Host': self.host, **extra}
        if session:
            headers['Cookie'] = f'wb_hub_session={self.session.token}'
        return headers

    async def get(self, path: str, *, session: bool = True, **extra):
        async with self.http.get(self.url + path, headers=self.headers(session, **extra),
                                 allow_redirects=False) as response:
            return response.status, dict(response.headers), await response.text()

    async def post(self, path: str, data: dict, *, session: bool = True, origin: bool = True, **extra):
        if origin:
            extra.setdefault('Origin', self.url)
        async with self.http.post(self.url + path, data=data, headers=self.headers(session, **extra),
                                  allow_redirects=False) as response:
            return response.status, dict(response.headers), await response.text()

    async def csrf(self) -> str:
        return self.session.csrf

    def make_request(self, title: str = 'T', body: str = 'B') -> str:
        return self.hub.inbox.submit(Principal.owner(), self.project, title, body)['request_id']

    async def make_key(self, name: str = 'laptop') -> tuple[str, str]:
        status, _, text = await self.post('/settings/keys', {'csrf': await self.csrf(), 'name': name,
                                                              'grade': 'requester', 'scope': 'all', 'expires_days': ''})
        self.assertEqual(status, 200, text[:300])
        raw = RAW_KEY.search(text).group(0)
        return raw, raw.split('_')[1]

    def assert_full_nav(self, tree: Tree, where: str) -> El:
        self.assertEqual(tree.problems, [], where)
        header = tree.header()
        self.assertIsNotNone(header, f'{where}: exactly one <header>')
        navs = tree.navs()
        self.assertEqual(len(navs), 1, f'{where}: exactly one <nav aria-label="주요">')
        self.assertEqual([e for e in header.walk() if e.tag == 'nav'], navs, f'{where}: no other <nav> in the header')
        # (settings screens keep their own in-page tab <nav> in <main> - settings_nav() is unchanged by contract)
        self.assertIs(navs[0].parent, header, f'{where}: nav is a direct child of header')
        links = {a.all_text().strip(): a.attrs.get('href') for a in navs[0].find('a')}
        self.assertEqual(links, NAV_LABEL_HREF, where)
        self.assertEqual(len(navs[0].find('a')), 7, f'{where}: no duplicate link')
        return navs[0]


# -- scenario 1: every session GET page route, enumerated from the audit --------------------------------------
class EveryPageRouteTests(NavBase):
    async def test_every_session_get_route_of_the_audit_has_one_complete_nav_and_logout(self):
        rid = self.make_request()
        _, key_id = await self.make_key()
        params = {**audit.PARAMS, 'rid': rid, 'key_id': key_id, 'project_id': self.project, 'action': 'power-on'}
        walked, skipped = [], []
        for (method, route), access in sorted(audit.route_marks(self.app).items()):
            if method != 'GET' or access != audit.SESSION or route.startswith('/static'):
                continue
            path = route.format(**params)
            status, headers, text = await self.get(path)
            if not headers.get('Content-Type', '').startswith('text/html'):
                skipped.append((route, status, headers.get('Content-Type')))
                continue
            walked.append((route, status))
            with self.subTest(route=route, status=status):
                tree = Tree(text)
                self.assert_full_nav(tree, route)
                forms = tree.header().find('form', action='/logout')
                self.assertEqual(len(forms), 1, route)
                self.assertEqual(forms[0].attrs.get('method'), 'post')
                self.assertEqual([i.attrs.get('value') for i in forms[0].find('input', name='csrf')],
                                 [self.session.csrf], route)
                self.assertEqual(len(tree.root.find('span', id='wb-badge')), 1, route)
        self.assertEqual({route for route, _ in walked},
                         {r for (m, r), a in audit.route_marks(self.app).items()
                          if m == 'GET' and a == audit.SESSION and not r.startswith('/static')}
                         - {route for route, *_ in skipped}, 'every HTML GET route was walked')
        # raw / artifact downloads are the only routes allowed to be non-HTML, besides ``/`` (WBX-A6: 303 /dashboard).
        self.assertLessEqual({route for route, *_ in skipped}, {'/requests/{rid}/artifact', '/requests/{rid}/raw', '/'},
                             skipped)
        self.assertEqual([status for route, status, _ in skipped if route == '/'], [303])
        self.assertGreaterEqual(len(walked), 14, walked)


# -- scenario 2: nested paths and aria-current -----------------------------------------------------------------
class AriaCurrentTests(NavBase):
    async def current_of(self, text: str, where: str) -> tuple[list[str], list[str]]:
        tree = Tree(text)
        nav = self.assert_full_nav(tree, where)
        in_nav = sorted(e.all_text().strip() for e in nav.walk() if 'aria-current' in e.attrs)
        self.assertEqual(sorted(e.all_text().strip() for e in tree.current()), in_nav,
                         f'{where}: aria-current only inside the nav')
        self.assertEqual({e.attrs['aria-current'] for e in tree.current()}, {'page'}, where)
        return in_nav, [e.tag for e in nav.walk() if 'aria-current' in e.attrs]

    async def test_current_marks_for_nested_and_lookalike_paths(self):
        rid = self.make_request()
        _, key_id = await self.make_key()
        project = self.project
        cases = {
            '/dashboard': ['대시보드'],
            f'/projects/{project}': ['대시보드'],
            f'/projects/{uuid.uuid4()}': ['대시보드'],
            f'/projects/{project}/power-on': ['대시보드'],
            '/requests': ['요청'],
            '/requests?project=/board&q=/settings': ['요청'],
            f'/requests/{rid}': ['요청'],
            f'/requests/{rid}/edit': ['요청'],
            f'/requests/{rid}/cancel': ['요청'],
            f'/requests/{rid}/full': ['요청'],
            '/requests/newest': ['요청'],          # prefix pitfall: not the "새 요청" button
            '/requests/new': ['새 요청'],
            '/requests/new?next=/requests': ['새 요청'],
            '/board': ['Board agent'],  # WBX-D1: the restart confirm pages are gone
            '/board?tab=shell': ['Board agent'],
            '/settings': ['비밀번호', '설정'],
            '/settings/keys': ['API key', '설정'],
            f'/settings/keys/{key_id}/revoke': ['API key', '설정'],
            '/settings/audit': ['감사 기록', '설정'],
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                status, headers, text = await self.get(path)
                self.assertTrue(headers['Content-Type'].startswith('text/html'), (status, path))
                names, tags = await self.current_of(text, path)
                self.assertEqual(sorted(names), sorted(expected), path)
                if path.startswith('/settings'):
                    self.assertEqual(sorted(tags), ['a', 'summary'], path)
                else:
                    self.assertEqual(tags, ['a'], path)

    async def test_current_marks_on_post_result_pages(self):
        rid = self.make_request()
        csrf = await self.csrf()
        cases = [
            # (path, form, expected current, why)
            ('/requests', {'csrf': csrf, 'project': self.project, 'title': '', 'body': ''}, ['요청']),
            (f'/requests/{rid}/comments', {'csrf': csrf, 'body': ''}, ['요청']),
            (f'/requests/{rid}/edit', {'csrf': csrf, 'title': '', 'body': '', 'base_version': '1'}, ['요청']),
            ('/settings/password', {'csrf': csrf, 'current_password': 'nope', 'new_password': 'a',
                                     'confirm_password': 'b'}, ['비밀번호', '설정']),
            ('/settings/keys', {'csrf': csrf, 'name': '', 'grade': 'requester', 'scope': 'all'}, ['API key', '설정']),
        ]
        shown = 0
        for path, data, expected in cases:
            with self.subTest(path=path):
                status, headers, text = await self.post(path, data)
                if status == 303 or not headers.get('Content-Type', '').startswith('text/html'):
                    continue
                shown += 1
                names, _ = await self.current_of(text, f'POST {path} -> {status}')
                self.assertEqual(sorted(names), sorted(expected), path)
        self.assertGreaterEqual(shown, 3, 'enough POST result pages were rendered to be meaningful')

    async def test_successful_key_issue_page_is_a_settings_screen_with_the_nav(self):
        csrf = await self.csrf()
        status, headers, text = await self.post('/settings/keys', {'csrf': csrf, 'name': 'issued', 'grade': 'requester',
                                                                   'scope': 'all', 'expires_days': ''})
        self.assertEqual(status, 200)
        names, _ = await self.current_of(text, 'POST /settings/keys ok')
        self.assertEqual(sorted(names), ['API key', '설정'])
        self.assertIn('no-store', headers.get('Cache-Control', ''))  # unchanged cache rule for the key page


# -- scenario 3: no nav without a session ----------------------------------------------------------------------
class NoNavTests(NavBase):
    def assert_no_nav(self, text: str, where: str) -> None:
        tree = Tree(text)
        self.assertEqual(tree.navs(), [], where)
        self.assertEqual(tree.root.find('details'), [], where)
        self.assertEqual(tree.root.find('form', action='/logout'), [], where)
        for needle in ('aria-label="주요"', 'nav-toggle', 'wb-nav-toggle', 'nav-settings', 'class="dot'):
            self.assertNotIn(needle, text, where)

    async def test_login_and_every_login_failure_page_have_no_nav(self):
        status, headers, text = await self.get('/login', session=False)
        self.assertEqual(status, 200)
        self.assert_no_nav(text, 'GET /login')
        async with self.http.get(self.url + '/login', headers=self.headers(False)) as response:
            cookie = next(v.split(';')[0].split('=', 1)[1] for v in response.headers.getall('Set-Cookie')
                          if v.startswith('wb_hub_login='))
            text = await response.text()
        token = re.search(r'name="login_token" value="([^"]+)"', text).group(1)
        cases = {
            'wrong password': {'login_token': token, 'password': 'wrong password'},
            'wrong token': {'login_token': 'x' * 40, 'password': PASSWORD},
            'missing token': {'password': PASSWORD},
            'empty': {},
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                status, _, body = await self.post('/login', data, session=False, Cookie=f'wb_hub_login={cookie}')
                self.assertIn(status, (400, 403, 429), (label, status))
                self.assert_no_nav(body, label)

    async def test_session_less_page_gets_are_redirects_without_nav_markup(self):
        for path in ('/', '/dashboard', '/requests', '/requests/new', '/board', '/settings', '/settings/keys', '/settings/audit',
                     f'/requests/{uuid.uuid4()}', f'/projects/{self.project}', '/board?tab=shell'):
            with self.subTest(path=path):
                status, headers, text = await self.get(path, session=False)
                self.assertEqual((status, headers.get('Location')), (303, '/login'))
                self.assert_no_nav(text, path)

    async def test_forged_or_expired_session_cookie_is_the_same_as_no_session(self):
        status, headers, text = await self.get('/requests', session=False, Cookie='wb_hub_session=forged')
        self.assertEqual((status, headers.get('Location')), (303, '/login'))
        self.assert_no_nav(text, 'forged cookie')
        # After logout the old cookie draws nothing.
        status, _, _ = await self.post('/logout', {'csrf': await self.csrf()})
        self.assertEqual(status, 303)
        status, headers, text = await self.get('/requests')
        self.assertEqual((status, headers.get('Location')), (303, '/login'))
        self.assert_no_nav(text, 'after logout')

    async def test_refusals_421_foreign_origin_and_api_have_no_nav(self):
        status, _, text = await self.get('/requests', Host='evil.example')
        self.assertEqual(status, 421)
        self.assert_no_nav(text, '421')
        status, _, text = await self.get('/login', session=False, Host='evil.example')
        self.assertEqual(status, 421)
        self.assert_no_nav(text, '421 login')
        status, _, text = await self.post('/settings/password', {'csrf': await self.csrf()}, Origin='http://evil.example')
        self.assertEqual(status, 403)
        self.assert_no_nav(text, 'foreign origin 403')
        status, _, text = await self.get('/api/badge', session=False)
        self.assertEqual(status, 401)
        self.assert_no_nav(text, '401 api')
        status, _, text = await self.get('/no/such/page', session=False)
        self.assertNotIn(status, (200,))
        self.assert_no_nav(text, 'unknown path without session')

    async def test_a_login_page_reached_while_logged_in_still_has_no_nav(self):
        status, headers, text = await self.get('/login')
        if status == 200:
            self.assert_no_nav(text, 'GET /login with session')
        else:
            self.assertEqual(status, 303)  # redirected away: nothing to draw


# -- scenario 4: user-controlled text never enters the nav; nav markup is the same everywhere -------------------
class NavIsolationTests(NavBase):
    async def test_the_header_markup_is_identical_on_every_screen_up_to_aria_current(self):
        rid = self.make_request(HOSTILE, HOSTILE)
        _, key_id = await self.make_key('plain key')
        paths = ['/dashboard', f'/projects/{self.project}', '/requests', '/requests/new', f'/requests/{rid}',
                 f'/requests/{rid}/edit', f'/requests/{rid}/cancel', '/board', '/settings',
                 '/settings/keys', f'/settings/keys/{key_id}/revoke', '/settings/audit']  # WBX-D1: no restart page
        shapes = {}
        for path in paths:
            status, _, text = await self.get(path)
            head = header_html(text)
            self.assertTrue(head, path)
            self.assertNotIn(LEAK, head, path)
            shapes[path] = re.sub(r' aria-current="page"', '', head)
        self.assertEqual(len(set(shapes.values())), 1, {p: s[:120] for p, s in shapes.items()})

    async def test_query_cookie_and_header_values_do_not_reach_the_nav(self):
        for path in (f'/requests?q={LEAK}&project={LEAK}&state={LEAK}', f'/board?channel={LEAK}',
                     f'/settings/audit?page={LEAK}', f'/dashboard?x={LEAK}'):
            with self.subTest(path=path):
                status, _, text = await self.get(path, **{'User-Agent': LEAK, 'Referer': f'{self.url}/{LEAK}'})
                self.assertNotIn(LEAK, header_html(text), path)

    async def test_hostile_project_key_and_request_text_stays_in_main_not_header(self):
        self.make_request(HOSTILE, HOSTILE)
        status, _, text = await self.post('/settings/keys', {'csrf': await self.csrf(), 'name': HOSTILE,
                                                             'grade': 'requester', 'scope': 'all', 'expires_days': ''})
        for path in ('/requests', '/settings/keys', '/settings/audit', '/dashboard'):
            _, _, text = await self.get(path)
            self.assertNotIn(LEAK, header_html(text), path)
            tree = Tree(text)
            self.assertEqual(tree.problems, [], path)
            self.assertEqual([e for e in tree.root.walk() if e.tag == 'script' and not e.attrs.get('src')], [], path)

    async def test_board_state_can_not_break_a_page_whatever_it_returns(self):
        class Evil:
            def __str__(self):
                raise RuntimeError(LEAK)
        values = [['on'], {'on': 1}, {'x'}, 3, True, b'on', Evil(), 'ON', ' on', 'on\n', f'on"{LEAK}', '', 'error']
        for value in values:
            with self.subTest(value=repr(value)[:30]):
                self.services.board_state = lambda value=value: value
                status, _, text = await self.get('/requests')
                self.assertEqual(status, 200)
                tree = Tree(text)
                nav = self.assert_full_nav(tree, repr(value))
                dots = nav.find('span')
                dots = [d for d in dots if 'dot' in d.classes()]
                self.assertEqual(len(dots), 1)
                self.assertIn(dots[0].classes(), (['dot', 'dot-on'], ['dot', 'dot-error']))
                self.assertNotIn(LEAK, header_html(text))


# -- scenario 5: status dot ------------------------------------------------------------------------------------
class DotTests(NavBase):
    async def test_three_values_have_distinct_class_and_nonempty_korean_title_on_every_screen(self):
        titles = {}
        for state in ('on', 'off', 'error'):
            self.services.board_state = lambda state=state: state
            for path in ('/dashboard', '/requests', '/settings/audit', '/board'):
                status, _, text = await self.get(path)
                nav = self.assert_full_nav(Tree(text), path)
                dots = [e for e in nav.find('span') if 'dot' in e.classes()]
                self.assertEqual(len(dots), 1, (state, path))
                self.assertEqual(dots[0].classes(), ['dot', f'dot-{state}'], (state, path))
                title = dots[0].attrs.get('title')
                self.assertTrue(title and re.search(r'[가-힣]', title), (state, path, title))
                titles.setdefault(state, set()).add(title)
                # the dot sits inside the Board agent link, so the link's own text is unchanged
                self.assertEqual(dots[0].parent.attrs.get('href'), '/board')
        self.assertEqual({k: len(v) for k, v in titles.items()}, {'on': 1, 'off': 1, 'error': 1})
        self.assertEqual(len({next(iter(v)) for v in titles.values()}), 3, 'a distinct text per state, not colour only')

    async def test_the_dot_follows_the_state_at_render_time_not_at_app_creation(self):
        seen = []
        for state in ('on', 'error', 'off', 'on'):
            self.services.board_state = lambda state=state: state
            _, _, text = await self.get('/requests')
            seen.append(re.search(r'class="dot dot-(\w+)"', text).group(1))
        self.assertEqual(seen, ['on', 'error', 'off', 'on'])


# -- scenario 6: CSP-clean markup and well-formed HTML ---------------------------------------------------------
class MarkupTests(NavBase):
    async def pages(self) -> dict:
        rid = self.make_request()
        _, key_id = await self.make_key()
        paths = ['/dashboard', f'/projects/{self.project}', '/requests', '/requests/new', f'/requests/{rid}',
                 f'/requests/{rid}/edit', f'/requests/{rid}/cancel', '/board', '/settings',
                 '/settings/keys', f'/settings/keys/{key_id}/revoke', '/settings/audit']  # WBX-D1: no restart page
        found = {}
        for path in paths:
            status, headers, text = await self.get(path)
            found[path] = (status, headers, text)
        return found

    async def test_pages_are_well_formed_and_ids_unique_and_the_toggle_pairs_with_the_nav(self):
        for path, (status, headers, text) in (await self.pages()).items():
            with self.subTest(path=path):
                tree = Tree(text)
                self.assertEqual(tree.problems, [], path)
                ids = [e.attrs['id'] for e in tree.root.walk() if 'id' in e.attrs]
                self.assertEqual(sorted(i for i in set(ids) if ids.count(i) > 1), [], f'{path}: duplicate ids')
                header = tree.header()
                toggles = header.find('input', type='checkbox')
                self.assertEqual([t.attrs.get('id') for t in toggles], ['wb-nav-toggle'], path)
                labels = header.find('label')
                self.assertEqual([l.attrs.get('for') for l in labels], ['wb-nav-toggle'], path)
                self.assertTrue(labels[0].all_text().strip(), f'{path}: the toggle label has text')
                nav = tree.navs()[0]
                kids = header.children
                self.assertIs(toggles[0].parent, header)
                self.assertLess(kids.index(toggles[0]), kids.index(labels[0]), path)
                self.assertLess(kids.index(labels[0]), kids.index(nav), path)
                self.assertNotIn('checked', toggles[0].attrs, 'the nav starts unfolded state decided by CSS only')
                # badge and logout are header children after the nav, outside it (never folded)
                badge = header.find('span', id='wb-badge')[0]
                logout = header.find('form', action='/logout')[0]
                self.assertIs(badge.parent, header)
                self.assertIs(logout.parent, header)
                self.assertLess(kids.index(nav), kids.index(badge), path)
                self.assertLess(kids.index(nav), kids.index(logout), path)
                self.assertEqual(kids[0].tag, 'a', 'brand first')
                self.assertEqual(kids[0].attrs.get('href'), '/dashboard')
                self.assertEqual(logout.find('button')[0].attrs.get('type'), 'submit')

    async def test_no_inline_script_style_or_event_handler_anywhere_and_csp_is_strict(self):
        for path, (status, headers, text) in (await self.pages()).items():
            with self.subTest(path=path):
                tree = Tree(text)
                scripts = [e for e in tree.root.walk() if e.tag == 'script']
                self.assertTrue(all(e.attrs.get('src', '').startswith('/static/') and not e.all_text().strip()
                                    for e in scripts), f'{path}: {[(e.attrs, e.text[:30]) for e in scripts]}')
                header = tree.header()
                for e in header.walk():
                    self.assertNotIn('style', e.attrs, path)
                    self.assertEqual([k for k in e.attrs if k.startswith('on')], [], path)
                    self.assertNotIn('javascript:', (e.attrs.get('href') or '').lower(), path)
                    self.assertNotIn(e.tag, ('script', 'style'), path)
                self.assertEqual([e for e in tree.root.walk() if e.tag == 'style'], [], path)
                csp = headers.get('Content-Security-Policy', '')
                self.assertRegex(csp, r"script-src 'self'(;|$)", path)
                self.assertNotIn("script-src 'self' 'unsafe", csp)
                self.assertNotIn('unsafe-eval', csp)

    async def test_every_page_loads_base_css_and_badge_js_and_both_are_served(self):
        loaded = set()
        for path, (status, headers, text) in (await self.pages()).items():
            tree = Tree(text)
            hrefs = [e.attrs.get('href') for e in tree.root.find('link') if e.attrs.get('rel') == 'stylesheet']
            srcs = [e.attrs.get('src') for e in tree.root.find('script')]
            with self.subTest(path=path):
                self.assertIn('/static/base.css', hrefs)
            loaded |= set(hrefs) | set(srcs)
        for asset in sorted(loaded):
            status, headers, _ = await self.get(asset.split('?')[0], session=False)
            self.assertEqual(status, 200, asset)

    async def test_every_screen_with_the_badge_slot_loads_the_script_that_fills_it(self):
        # AC-01: the alert badge is on the right of every logged-in header; an empty slot without badge.js never fills.
        for path, (status, headers, text) in (await self.pages()).items():
            with self.subTest(path=path):
                tree = Tree(text)
                self.assertEqual(len(tree.root.find('span', id='wb-badge')), 1)
                self.assertIn('/static/badge.js', [e.attrs.get('src') for e in tree.root.find('script')])

    async def test_stylesheet_hides_the_nav_only_inside_the_45rem_query_and_not_the_badge_or_logout(self):
        css = (REPO / 'src/workbench/hub/web/static/base.css').read_text(encoding='utf-8')
        blocks = re.findall(r'@media\s*\(\s*max-width:\s*([\w.]+)\s*\)\s*\{(.*?)\n\}', css, re.S)
        self.assertEqual([w for w, _ in blocks], ['45rem'])
        inside = blocks[0][1]
        outside = css.replace(blocks[0][1], '')
        # outside the query: the label is hidden and the nav is not.
        self.assertRegex(outside, r'\.nav-toggle-label\s*\{[^}]*display:\s*none')
        for rule in re.findall(r'([^{}]+)\{([^}]*)\}', outside):
            selector, body = rule
            if 'nav' in selector and 'display' in body and 'none' in body.split('display')[1].split(';')[0]:
                self.assertIn('toggle-label', selector, f'nav hidden outside the narrow query: {selector.strip()}')
        # inside: the nav is hidden by default and the checked toggle shows it again; the badge/logout are untouched.
        self.assertRegex(inside, r'header nav\[aria-label\]\s*\{[^}]*display:\s*none')
        self.assertRegex(inside, r'\.nav-toggle:checked\s*~\s*nav\[aria-label\]\s*\{[^}]*display:\s*(flex|block)')
        self.assertNotIn('badge', inside)
        self.assertNotIn('logout', inside)
        # the checkbox stays focusable (not display:none), so keyboard users reach it.
        self.assertNotRegex(css, r'\.nav-toggle\s*\{[^}]*display:\s*none')


# -- scenario 7: the logout form still works with the page's CSRF -----------------------------------------------
class LogoutFormTests(NavBase):
    async def test_the_header_logout_form_posts_the_session_csrf_and_logs_out(self):
        _, _, text = await self.get('/requests')
        form = Tree(text).header().find('form', action='/logout')[0]
        fields = {i.attrs['name']: i.attrs.get('value') for i in form.find('input') if 'name' in i.attrs}
        self.assertEqual(fields, {'csrf': self.session.csrf})
        status, headers, _ = await self.post('/logout', fields)
        self.assertEqual((status, headers.get('Location')), (303, '/login'))
        status, headers, _ = await self.get('/requests')
        self.assertEqual((status, headers.get('Location')), (303, '/login'))

    async def test_logout_without_or_with_a_wrong_csrf_changes_nothing(self):
        for data in ({}, {'csrf': 'wrong'}):
            status, _, text = await self.post('/logout', data)
            self.assertEqual(status, 403, data)
            self.assertEqual(text.count('aria-label="주요"'), 1)  # 2026-10-28: a live session's stale page has the nav
            status, _, _ = await self.get('/requests')
            self.assertEqual(status, 200)

    async def test_stale_form_page_is_wellformed_and_leaks_nothing(self):
        # Observation target: a logged-in session whose form carries an old csrf gets the small stale page.
        stale = 'old-stale-form-token-4f9a'
        status, _, text = await self.post('/settings/password', {'csrf': stale})
        self.assertEqual(status, 403)
        tree = Tree(text)
        self.assertEqual(tree.problems, [])
        # 2026-10-28: the page carries exactly one CSRF value, this session's, inside the logout form; the stale
        # form's submitted token never comes back.
        self.assertEqual(text.count(self.session.csrf), 1)
        form = tree.root.find('form', action='/logout')[0]
        self.assertEqual([i.attrs.get('value') for i in form.find('input') if i.attrs.get('name') == 'csrf'],
                         [self.session.csrf])
        self.assertEqual(len(re.findall(r'name="csrf"', text)), 1)
        self.assertNotIn(stale, text)
        self.assertEqual(len(tree.navs()), len(tree.root.find('form', action='/logout')))  # both or neither


if __name__ == '__main__':
    unittest.main()
