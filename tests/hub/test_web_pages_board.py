"""TI-18 Board page (seam T2): the real ``create_app`` with ``board_routes`` over a fake Board set.

Expected values are the SPEC/ticket strings (routes, labels, asset paths, CSP) and stdlib ``html.parser``
facts about the served HTML. The browser behavior (xterm rendering, WebSocket) is a target-host check; the
protocol state machine is covered by ``tests/hub/js`` (``test_board_state_js.py`` runs it here).
Run with ``discover -s tests/hub -p 'test_web_pages_board*.py'``.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

import test_hub_web_auth as web_auth  # noqa: E402  (module import: its test classes stay uncollected here)
from workbench.hub.web.app import board_routes  # noqa: E402

STATIC = REPO / 'src/workbench/hub/web/static'


class FakeBoard:
    """What the page reads: ``status()`` (hub_v1 ``status.board_agent``) and ``restart``."""

    def __init__(self):
        self.value = None
        self.restarts: list[tuple] = []
        self.lease = {'state': 'none', 'epoch': 0, 'since': None}

    def status(self):
        return self.value

    async def restart(self, channel, *, actor='owner', address=None, reason='restart'):
        self.restarts.append((channel, actor))
        return {}


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

    async def test_takeover_banner_and_retake_button_exist_but_start_hidden(self):
        _, dom = await self.page()
        banner = dom.by_id('board-banner')
        self.assertIsNotNone(banner)
        self.assertIn('hidden', banner)
        self.assertEqual(banner.get('role'), 'alert')
        self.assertIn('다른 곳에서 연결됨 — 다시 가져오기', dom.body_text)
        self.assertEqual(dom.by_id('board-retake').get('type'), 'button')
        reconnect = dom.by_id('board-reconnect')
        self.assertIsNotNone(reconnect)
        self.assertIn('hidden', reconnect)

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
        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
        self.assertNotIn("'unsafe-inline'", response.headers['Content-Security-Policy'])

    async def test_own_assets_and_xterm_assets_are_served_with_a_javascript_type_and_no_external_url(self):
        for path, kind in (('/static/board.js', 'javascript'), ('/static/board-state.js', 'javascript'),
                           ('/static/vendor/xterm/xterm.js', 'javascript'),
                           ('/static/vendor/xterm/addon-fit.js', 'javascript'),
                           ('/static/board.css', 'css'), ('/static/vendor/xterm/xterm.css', 'css')):
            response = await self.get(path)
            self.assertEqual(response.status, 200, path)
            self.assertIn(kind, response.headers['Content-Type'], path)
        for name in ('board.js', 'board-state.js', 'board.css'):
            text = (STATIC / name).read_text()
            self.assertEqual(re.findall(r'https?://', text), [], name)
            self.assertNotIn('eval(', text, name)

    async def test_page_needs_a_session(self):
        response = await self.get('/board')
        self.assertEqual((response.status, response.headers['Location']), (303, '/login'))
        response = await self.get('/board/restart?channel=agent')
        self.assertEqual((response.status, response.headers['Location']), (303, '/login'))


class ChannelStateTests(BoardPageCase):
    async def test_channel_states_show_running_exited_failed_and_isolation_reasons(self):
        self.board.value = {
            'agent': {'state': 'isolation_failed', 'pid': None, 'started_at': None, 'detail': 'mcp_leak',
                      'exit_status': None},
            'shell': {'state': 'exited', 'pid': None, 'started_at': 1.0, 'detail': None, 'exit_status': 3},
            'lease': {'state': 'none', 'epoch': 0, 'since': None},
        }
        _, dom = await self.page()
        self.assertIn('격리 실패: mcp_leak', dom.body_text)
        self.assertIn('종료됨 (종료 코드 3)', dom.body_text)
        self.board.value['agent'] = {'state': 'running', 'detail': None, 'exit_status': None}
        self.board.value['shell'] = {'state': 'failed', 'detail': 'spawn_failed', 'exit_status': None}
        _, dom = await self.page()
        self.assertIn('실행 중', dom.body_text)
        self.assertIn('실패: spawn_failed', dom.body_text)

    async def test_never_started_set_reads_not_started_and_page_does_not_start_it(self):
        _, dom = await self.page()
        self.assertEqual(dom.body_text.count('시작 전'), 2)
        self.assertEqual(self.board.restarts, [])

    async def test_unavailable_set_is_503_with_the_reason_and_no_terminal(self):
        self.board_ref[0] = None
        response, dom = await self.page()
        self.assertEqual(response.status, 503)
        self.assertIn('Board agent 세트를 사용할 수 없습니다', dom.body_text)
        self.assertIsNone(dom.by_id('term-agent'))


class RestartConfirmTests(BoardPageCase):
    async def test_each_tab_has_a_restart_link_to_its_confirm_page(self):
        _, dom = await self.page()
        links = {attrs['href'] for attrs in dom.tags('a') if 'href' in attrs}
        self.assertIn('/board/restart?channel=agent', links)
        self.assertIn('/board/restart?channel=shell', links)

    async def test_confirm_page_posts_with_csrf_and_cancel_goes_back_without_a_change(self):
        response, dom = await self.page('/board/restart?channel=agent')
        self.assertEqual(response.status, 200)
        self.assertIn('재시작', dom.body_text)
        forms = [attrs for tag, attrs in dom.elements if tag == 'form' and attrs.get('action') == '/board/restart']
        self.assertEqual([form.get('method') for form in forms], ['post'])
        fields = {attrs.get('name'): attrs.get('value') for tag, attrs in dom.elements if tag == 'input'}
        self.assertEqual(fields['channel'], 'agent')
        self.assertTrue(fields['csrf'])
        cancel = [attrs for attrs in dom.tags('a') if attrs.get('href') == '/board']
        self.assertTrue(cancel)
        self.assertIn('취소', dom.body_text)
        self.assertEqual(self.board.restarts, [], 'a GET changes nothing')

    async def test_confirm_post_restarts_that_channel_and_returns_to_the_board_page(self):
        _, dom = await self.page('/board/restart?channel=shell')
        csrf = {attrs.get('name'): attrs.get('value') for tag, attrs in dom.elements if tag == 'input'}['csrf']
        response = await self.post('/board/restart', {'csrf': csrf, 'channel': 'shell'}, cookies=self.cookies)
        self.assertEqual((response.status, response.headers['Location']), (303, '/board'))
        self.assertEqual(self.board.restarts, [('shell', 'owner')])
        page = await self.get('/board', cookies=self.cookies)
        self.assertEqual(page.status, 200)

    async def test_unknown_channel_is_400_and_unavailable_set_is_503(self):
        for query in ('channel=both', 'channel=', ''):
            response, _ = await self.page('/board/restart' + ('?' + query if query else ''))
            self.assertEqual(response.status, 400, query)
        self.board_ref[0] = None
        response, _ = await self.page('/board/restart?channel=agent')
        self.assertEqual(response.status, 503)


if __name__ == '__main__':
    unittest.main()
