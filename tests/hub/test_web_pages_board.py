"""TI-18 Board page (seam T2): the real ``create_app`` with ``board_routes`` over a fake Board set.

Expected values are the SPEC/ticket strings (routes, labels, asset paths, CSP) and stdlib ``html.parser``
facts about the served HTML. The browser behavior (xterm rendering, WebSocket) is a target-host check; the
protocol state machine is covered by ``tests/hub/js`` (``test_board_state_js.py`` runs it here).
Run with ``discover -s tests/hub -p 'test_web_pages_board*.py'``.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import json
import re
import sys
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

import test_hub_web_auth as web_auth  # noqa: E402  (module import: its test classes stay uncollected here)
import test_web_design as design  # noqa: E402
from workbench.hub.web.app import board_routes  # noqa: E402
from workbench.hub.web.pages.board import channel_label  # noqa: E402

STATIC = REPO / 'src/workbench/hub/web/static'


def csp_directives(header: str) -> dict[str, list[str]]:
    """``{directive: [source, ...]}`` of one CSP header value, in order; a repeated directive is an error."""
    directives: dict[str, list[str]] = {}
    for part in header.split(';'):
        words = part.split()
        if not words:
            continue
        if words[0] in directives:
            raise AssertionError(f'repeated CSP directive {words[0]!r} in {header!r}')
        directives[words[0]] = words[1:]
    return directives


class FakeBoard:
    """What the page reads (WBX-D1): ``board_view()`` and ``views()`` (the terminal views, agents first), and
    ``closed``/``invalid``/``find`` for the session API and WebSocket routes it shares the app with."""

    closed = False
    invalid = None

    def __init__(self):
        self.terminals = [{'id': 'a-000000000001', 'kind': 'agent', 'name': 'agent-1', 'state': 'not_started',
                           'pid': None, 'started_at': None, 'detail': None, 'exit_status': None, 'clients': 0,
                           'busy': False}]
        self.started: list = []

    def board_view(self):
        return {'state': 'ok'} if self.invalid is None else {'state': 'registry_invalid', 'message': self.invalid}

    def views(self):
        return [dict(view) for view in self.terminals]

    def find(self, terminal_id):
        return None

    def watch(self, callback):
        return lambda: None

    def set(self, kind, **view):
        found = [item for item in self.terminals if item['kind'] == kind]
        if found:
            found[0].update(view)
        else:
            self.terminals.append({'id': f'{kind[0]}-00000000000{len(self.terminals) + 1}', 'kind': kind,
                                   'name': f'{kind}-1', 'pid': None, 'started_at': None, 'detail': None,
                                   'exit_status': None, 'clients': 0, 'busy': False, **view})


class Dom(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict]] = []
        self.text: list[str] = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, {key: value for key, value in attrs}))

    def handle_data(self, data):
        self.text.append(data)

    def tags(self, name):
        return [attrs for tag, attrs in self.elements if tag == name]

    def by_id(self, ident):
        return next((attrs for _, attrs in self.elements if attrs.get('id') == ident), None)

    @property
    def body_text(self):
        return ''.join(self.text)


class BoardPageCase(web_auth.WebAppCase):
    async def asyncSetUp(self):
        self.board = FakeBoard()
        self.board_ref = [self.board]
        self.routes = (board_routes(lambda: self.board_ref[0]),)
        await super().asyncSetUp()
        self.cookies = {'wb_hub_session': await self.session_cookie()}

    async def page(self, path='/board', **kwargs):
        response = await self.get(path, cookies=self.cookies, **kwargs)
        return response, Dom(response.body_text)


class PageStructureTests(BoardPageCase):
    async def test_page_has_two_tabs_for_the_agent_and_the_shell_and_a_connection_state(self):
        response, dom = await self.page()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers['Content-Type'].startswith('text/html'))
        tabs = [attrs for attrs in dom.tags('button') if attrs.get('role') == 'tab']
        self.assertEqual([attrs.get('data-channel') for attrs in tabs], ['agent', 'shell'])
        self.assertIn('Board agent', dom.body_text)
        self.assertIn('Shell', dom.body_text)
        for channel in ('agent', 'shell'):
            self.assertIsNotNone(dom.by_id(f'term-{channel}'), channel)
        self.assertIsNotNone(dom.by_id('board-connection'))
        self.assertIn('연결 중', dom.body_text)
        # WBX-D1: the agent tab names its terminal (the page's script opens /ws/board/<id>); this run's first shell
        # does not exist before the page's POST /api/board/open.
        self.assertEqual(dom.by_id('panel-agent').get('data-terminal'), 'a-000000000001')
        self.assertIsNone(dom.by_id('panel-shell').get('data-terminal'))
        self.assertIn('agent-1', dom.body_text)

    async def test_there_is_no_takeover_banner_and_reconnect_and_login_start_hidden(self):
        """v1 "takeover banner and retake button": shared connections have no takeover (WBX-D54)."""
        _, dom = await self.page()
        self.assertIsNone(dom.by_id('board-banner'))
        self.assertIsNone(dom.by_id('board-retake'))
        self.assertNotIn('다시 가져오기', dom.body_text)
        reconnect = dom.by_id('board-reconnect')
        self.assertIsNotNone(reconnect)
        self.assertIn('hidden', reconnect)
        login = dom.by_id('board-login')
        self.assertIsNotNone(login, 'shown by the client after close 4401 (the login session ended)')
        self.assertEqual(login.get('href'), '/login')
        self.assertIn('hidden', login)
        self.assertIn('다시 로그인', dom.body_text)

    async def test_csrf_token_is_in_a_meta_tag_and_equals_the_session_token(self):
        _, dom = await self.page()
        metas = [attrs for attrs in dom.tags('meta') if attrs.get('name') == 'csrf-token']
        self.assertEqual(len(metas), 1)
        token = metas[0]['content']
        self.assertTrue(token)
        self.assertEqual(token, self.services.sessions.verify(self.cookies['wb_hub_session'], 1_000_000.0).csrf)

    async def test_assets_are_the_vendored_xterm_files_and_the_boards_own_modules_in_order(self):
        _, dom = await self.page()
        scripts = [(attrs.get('src'), attrs.get('type')) for attrs in dom.tags('script')]
        sources = [src for src, _ in scripts]
        self.assertIn('/static/vendor/xterm/xterm.js', sources)
        self.assertIn('/static/vendor/xterm/addon-fit.js', sources)
        self.assertLess(sources.index('/static/vendor/xterm/xterm.js'), sources.index('/static/board.js'))
        self.assertLess(sources.index('/static/vendor/xterm/addon-fit.js'), sources.index('/static/board.js'))
        self.assertIn(('/static/board.js', 'module'), scripts)
        hrefs = [attrs.get('href') for attrs in dom.tags('link') if attrs.get('rel') == 'stylesheet']
        self.assertIn('/static/vendor/xterm/xterm.css', hrefs)
        self.assertIn('/static/board.css', hrefs)

    async def test_no_inline_script_style_event_handler_or_external_url(self):
        response, dom = await self.page()
        text = response.body_text
        self.assertTrue(all(attrs.get('src') for attrs in dom.tags('script')), 'every script has a src')
        self.assertFalse(any(tag == 'script' and attrs.get('src') is None for tag, attrs in dom.elements))
        self.assertEqual(dom.tags('style'), [])
        for tag, attrs in dom.elements:
            for name in attrs:
                self.assertFalse(name.startswith('on'), f'<{tag} {name}>')
            self.assertNotIn('style', attrs, tag)
        self.assertEqual(re.findall(r'(?:src|href|action)="(?:https?:)?//[^"]*"', text), [])
        self.assertIn("script-src 'self';", response.headers['Content-Security-Policy'])
        self.assertNotIn("script-src 'self' 'unsafe-inline'", response.headers['Content-Security-Policy'])

    async def test_own_assets_and_xterm_assets_are_served_with_a_javascript_type_and_no_external_url(self):
        for path, kind in (('/static/board.js', 'javascript'), ('/static/board-state.js', 'javascript'),
                           ('/static/style-nonce.js', 'javascript'),
                           ('/static/vendor/xterm/xterm.js', 'javascript'),
                           ('/static/vendor/xterm/addon-fit.js', 'javascript'),
                           ('/static/board.css', 'css'), ('/static/vendor/xterm/xterm.css', 'css')):
            response = await self.get(path)
            self.assertEqual(response.status, 200, path)
            self.assertIn(kind, response.headers['Content-Type'], path)
        for name in ('board.js', 'board-state.js', 'style-nonce.js', 'board.css'):
            text = (STATIC / name).read_text()
            self.assertEqual(re.findall(r'https?://', text), [], name)
            self.assertNotIn('eval(', text, name)

    async def test_board_page_alone_allows_nonced_style_elements_and_inline_style_attributes_only(self):
        # xterm 5.5's DOM renderer inserts <style> elements (allowed by this response's nonce) and writes
        # truecolor cells through setAttribute("style") (allowed by style-src-attr). 2026-10-26 사용자 결정.
        strict = csp_directives(web_auth.COMMON_HEADERS['Content-Security-Policy'])
        response, _ = await self.page()
        self.assertEqual(response.status, 200)
        csp = response.headers['Content-Security-Policy']
        directives = csp_directives(csp)
        style = directives['style-src']
        nonces = [item for item in style if item.startswith("'nonce-")]
        self.assertEqual(len(nonces), 1, csp)
        self.assertRegex(nonces[0], r"^'nonce-[A-Za-z0-9_-]{22,}'$")
        self.assertEqual([item for item in style if item not in nonces], ["'self'"], csp)
        self.assertNotIn("'unsafe-inline'", style)
        self.assertEqual(directives['style-src-attr'], ["'unsafe-inline'"])
        others = {name: value for name, value in directives.items() if name not in ('style-src', 'style-src-attr')}
        self.assertEqual(others, {name: value for name, value in strict.items() if name != 'style-src'})
        self.assertEqual(directives['script-src'], ["'self'"])
        self.assertNotIn("'unsafe-eval'", csp)
        self.assertEqual(csp.count("'unsafe-inline'"), 1, csp)
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    async def test_style_nonce_is_fresh_per_response_and_matches_the_style_nonce_meta_of_the_body(self):
        seen = []
        for _ in range(2):
            response, dom = await self.page()
            self.assertEqual(response.status, 200)
            nonces = [item for item in csp_directives(response.headers['Content-Security-Policy'])['style-src']
                      if item.startswith("'nonce-")]
            self.assertEqual(len(nonces), 1)
            metas = [attrs for attrs in dom.tags('meta') if attrs.get('name') == 'style-nonce']
            self.assertEqual(len(metas), 1, 'exactly one style-nonce meta')
            self.assertEqual(f"'nonce-{metas[0]['content']}'", nonces[0])
            seen.append(metas[0]['content'])
        self.assertNotEqual(seen[0], seen[1], 'a nonce is never reused across responses')

    async def test_every_other_page_and_every_non_200_board_response_keeps_the_strict_csp(self):
        strict = web_auth.COMMON_HEADERS['Content-Security-Policy']
        self.assertNotIn("'unsafe-inline'", strict)
        self.assertNotIn('nonce-', strict)
        self.assertNotIn('style-src-attr', strict)
        responses = []
        for path, cookies in (('/board/restart?channel=agent', self.cookies), ('/board/restart?channel=x', self.cookies),
                              ('/login', None), ('/dashboard', self.cookies), ('/board', None), ('/nope', self.cookies),
                              ('/static/board.js', None), ('/static/style-nonce.js', None),
                              ('/ws/board', self.cookies)):
            responses.append((path, await self.get(path, cookies=cookies)))
        responses.append(('/board bad Host', await self.get('/board', host='evil.example', cookies=self.cookies)))
        responses.append(('/board stale cookie', await self.get('/board', cookies={'wb_hub_session': 'stale'})))
        responses.append(('POST /board', await self.post('/board', {}, cookies=self.cookies)))
        statuses = dict((path, response.status) for path, response in responses)
        self.assertEqual((statuses['/board'], statuses['/board bad Host'], statuses['/board stale cookie']),
                         (303, 421, 303))
        self.assertEqual(statuses['POST /board'], 403)
        self.assertEqual(statuses['/static/style-nonce.js'], 200)
        self.board_ref[0] = None
        response, _ = await self.page()
        self.assertEqual(response.status, 503)
        responses.append(('/board 503: the page has no terminal', response))
        for path, response in responses:
            csp = response.headers['Content-Security-Policy']
            self.assertEqual(csp, strict, path)
            self.assertNotIn('nonce-', csp, path)
            self.assertNotIn('style-src-attr', csp, path)

    async def test_page_needs_a_session(self):
        response = await self.get('/board')
        self.assertEqual((response.status, response.headers['Location']), (303, '/login'))
        # WBX-D1: the restart confirm page is gone (an unknown page: 404 with a session).
        response = await self.get('/board/restart?channel=agent', cookies=self.cookies)
        self.assertEqual(response.status, 404)


class HelperTextareaCssTests(unittest.TestCase):
    """xterm's hidden helper <textarea> takes the IME input. During a composition xterm.js sets its inline width to
    the composition's width; its parent ``.xterm-helpers`` is an absolute box of width 0, so the page's generic
    ``textarea { max-width: 100% }`` (base.css) clamps it to 0 and Chrome then commits a Hangul syllable with the
    caret at 0: the next syllable goes in front of it and xterm reads back the wrong text ("안녕하세요" sent as
    "안안안안안"; seen with CDP ``Input.imeSetComposition``/``insertText`` on 2026-10-29). Static read (seam S5)."""

    def rules(self, name: str) -> list[tuple[str, dict[str, str]]]:
        text = (STATIC / name).read_text(encoding='utf-8')
        return [(selector, design.declarations(body)) for selector, body in design.all_rules(text)]

    def test_a_page_wide_textarea_max_width_is_undone_for_the_xterm_helper_textarea(self):
        page_wide = [(name, selector) for name in ('base.css', 'app.css') for selector, props in self.rules(name)
                     if 'max-width' in props and 'textarea' in [part.strip() for part in selector.split(',')]]
        self.assertTrue(page_wide, 'base.css no longer limits every textarea: drop this test and the board.css rule')
        undone = [props.get('max-width') for selector, props in self.rules('board.css')
                  if any(re.fullmatch(r'\.xterm\s+\.xterm-helper-textarea', part.strip()) for part in selector.split(','))]
        self.assertIn('none', undone, page_wide)


class ChannelLabelParityTests(unittest.TestCase):
    def test_python_channel_label_matches_the_table_the_js_channelLabel_is_tested_with(self):
        table = json.loads((REPO / 'tests/hub/js/channel_labels.json').read_text())
        self.assertGreaterEqual(len(table), 10)
        for row in table:
            self.assertEqual(channel_label(row['info']), row['label'], row['info'])


class ChannelStateTests(BoardPageCase):
    async def test_channel_states_show_running_exited_failed_stopped_and_isolation_reasons(self):
        self.board.set('agent', state='isolation_failed', detail='mcp_leak')
        self.board.set('shell', state='exited', started_at=1.0, exit_status=3)
        _, dom = await self.page()
        self.assertIn('격리 실패: mcp_leak', dom.body_text)
        self.assertIn('종료됨 (종료 코드 3)', dom.body_text)
        self.board.set('agent', state='running')
        self.board.set('shell', state='failed', detail='spawn_failed')
        _, dom = await self.page()
        self.assertIn('실행 중', dom.body_text)
        self.assertIn('실패: spawn_failed', dom.body_text)
        self.board.set('agent', state='stopped')
        _, dom = await self.page()
        self.assertIn('중지됨', dom.body_text)

    async def test_never_started_terminals_read_not_started_and_the_page_starts_nothing(self):
        _, dom = await self.page()
        self.assertEqual(dom.body_text.count('시작 전'), 2)
        self.assertEqual(self.board.started, [])

    async def test_a_terminal_name_is_text_never_markup(self):  # AC-38
        self.board.set('agent', name='<img src=x onerror=alert(1)>&')
        response, dom = await self.page()
        self.assertNotIn('<img src=x', response.body_text)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;&amp;', response.body_text)
        self.assertEqual(dom.tags('img'), [])

    async def test_unavailable_set_is_503_with_the_reason_and_no_terminal(self):
        self.board_ref[0] = None
        response, dom = await self.page()
        self.assertEqual(response.status, 503)
        self.assertIn('Board agent 세트를 사용할 수 없습니다', dom.body_text)
        self.assertIsNone(dom.by_id('term-agent'))

    async def test_an_invalid_registry_is_503_with_its_message_and_no_terminal(self):
        self.board.invalid = 'Board registry /x/board-agents/registry.json is unusable (not JSON).'
        response, dom = await self.page()
        self.assertEqual(response.status, 503)
        self.assertIn('registry.json', dom.body_text)
        self.assertIsNone(dom.by_id('term-agent'))


class RestartTests(BoardPageCase):
    """v1 "restart link to its confirm page" and the confirm form: WBX-D1 replaces them with a restart button per tab
    (the page's script asks first, then ``POST /api/board/terminals/{id}/restart`` with ``confirm: true``)."""

    async def test_each_tab_has_a_restart_button_and_no_confirm_page_link(self):
        _, dom = await self.page()
        for channel in ('agent', 'shell'):
            button = dom.by_id(f'restart-{channel}')
            self.assertEqual((button.get('type'), button.get('data-channel')), ('button', channel))
        links = {attrs['href'] for attrs in dom.tags('a') if 'href' in attrs}
        self.assertFalse({'/board/restart?channel=agent', '/board/restart?channel=shell'} & links)

    async def test_the_confirm_page_and_its_form_post_are_gone(self):
        response, _ = await self.page('/board/restart?channel=agent')
        self.assertEqual(response.status, 404)
        _, dom = await self.page()
        csrf = [attrs for attrs in dom.tags('meta') if attrs.get('name') == 'csrf-token'][0]['content']
        response = await self.post('/board/restart', {'csrf': csrf, 'channel': 'shell'}, cookies=self.cookies)
        self.assertIn(response.status, (404, 405))
        self.assertEqual(self.board.started, [])
