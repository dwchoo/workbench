"""TI-18 / WBX-D2 Board page (seams S1+S6): the real ``create_app`` with ``board_routes`` over a fake Board set.

WBX-D2: ``/board`` is an empty frame (panel + grid workspace + confirm dialog); the terminal list, the widgets and the
names come from ``GET /api/board`` and are drawn by ``static/board.js`` (tests/hub/js).

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
    async def test_page_is_a_panel_and_an_empty_grid_workspace_with_no_tabs(self):
        response, dom = await self.page()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers['Content-Type'].startswith('text/html'))
        panel = dom.by_id('board-panel')
        self.assertEqual(panel.get('class'), 'board-panel')
        workspace = dom.by_id('board-workspace')
        self.assertEqual(workspace.get('class'), 'board-workspace')
        self.assertEqual([tag for tag, attrs in dom.elements if tag == 'aside'], ['aside'])
        self.assertFalse([attrs for attrs in dom.tags('button') if attrs.get('role') == 'tab'], 'the tab UI is gone')
        for old in ('tabs', 'panels', 'term-agent', 'term-shell', 'panel-agent', 'panel-shell', 'restart-agent'):
            self.assertIsNone(dom.by_id(old), old)
            self.assertFalse([1 for _, attrs in dom.elements if old in (attrs.get('class') or '').split()], old)
        # the server draws no terminal: the list, the names and the states come from GET /api/board
        self.assertEqual(self.board.started, [])
        self.assertNotIn('agent-1', response.body_text)
        self.assertNotIn('시작 전', dom.body_text)
        self.assertIn('Board', dom.body_text)

    async def test_page_without_javascript_says_it_needs_javascript(self):
        response, _ = await self.page()
        self.assertRegex(response.body_text, r'<noscript>[^<]*<[^>]*>이 화면은 JavaScript가 필요합니다')

    async def test_panel_has_the_rows_buttons_limit_form_and_toggle_the_script_binds_to(self):
        _, dom = await self.page()
        toggle = dom.by_id('board-panel-toggle')
        self.assertEqual((toggle.get('type'), toggle.get('aria-controls'), toggle.get('aria-expanded')),
                         ('button', 'board-panel', 'true'))
        for ident, tag in (('board-agents', 'ul'), ('board-shells', 'ul')):
            self.assertIn(tag, [t for t, attrs in dom.elements if attrs.get('id') == ident], ident)
        for ident, text in (('board-new-agent', '새 Board agent'), ('board-new-shell', '새 shell'),
                            ('board-reset-layout', '배치 초기화')):
            self.assertEqual(dom.by_id(ident).get('type'), 'button', ident)
            self.assertIn(text, dom.body_text, ident)
        self.assertIn('이 브라우저만', dom.body_text)
        form = dom.by_id('board-options')
        self.assertIsNotNone(form)
        self.assertIn('novalidate', form, 'the page shows its own explanation (the Hub\'s sentence), not the browser bubble')
        numbers = {attrs.get('id'): attrs for attrs in dom.tags('input') if attrs.get('type') == 'number'}
        self.assertEqual(set(numbers), {'board-agent-limit', 'board-shell-limit'})
        self.assertEqual((numbers['board-agent-limit'].get('min'), numbers['board-agent-limit'].get('max')), ('1', '8'))
        self.assertEqual((numbers['board-shell-limit'].get('min'), numbers['board-shell-limit'].get('max')), ('1', '32'))
        self.assertEqual(dom.by_id('board-options-error').get('role'), 'alert')
        self.assertIn('hidden', dom.by_id('board-options-error'))
        notice = dom.by_id('board-notice')
        self.assertEqual(notice.get('role'), 'status')
        self.assertIn('hidden', notice)

    async def test_confirm_goes_through_a_dialog_element_with_a_cancel_and_an_ok_button(self):
        _, dom = await self.page()
        self.assertEqual(len(dom.tags('dialog')), 1)
        self.assertEqual(dom.by_id('board-confirm').get('aria-labelledby'), 'board-confirm-text')
        self.assertIsNotNone(dom.by_id('board-confirm-text'))
        self.assertEqual(dom.by_id('board-confirm-cancel').get('type'), 'button')
        self.assertEqual(dom.by_id('board-confirm-ok').get('type'), 'button')
        self.assertNotIn('open', dom.by_id('board-confirm'), 'closed until the script asks')

    async def test_there_is_no_takeover_banner_and_no_page_wide_reconnect_control(self):
        """v1 "takeover banner and retake button": shared connections have no takeover (WBX-D54); the reconnect and
        login actions are per widget now (drawn by the script)."""
        _, dom = await self.page()
        for old in ('board-banner', 'board-retake', 'board-reconnect', 'board-login', 'board-connection'):
            self.assertIsNone(dom.by_id(old), old)
        self.assertNotIn('다시 가져오기', dom.body_text)

    async def test_board_body_class_lets_the_workspace_use_the_full_width(self):
        response, _ = await self.page()
        self.assertRegex(response.body_text, r'<body class="page-board">')

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
                           ('/static/board-layout.js', 'javascript'), ('/static/board-panel.js', 'javascript'),
                           ('/static/style-nonce.js', 'javascript'),
                           ('/static/vendor/xterm/xterm.js', 'javascript'),
                           ('/static/vendor/xterm/addon-fit.js', 'javascript'),
                           ('/static/board.css', 'css'), ('/static/vendor/xterm/xterm.css', 'css')):
            response = await self.get(path)
            self.assertEqual(response.status, 200, path)
            self.assertIn(kind, response.headers['Content-Type'], path)
        for name in ('board.js', 'board-state.js', 'board-layout.js', 'board-panel.js', 'style-nonce.js', 'board.css'):
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


class PageIsAFrameTests(BoardPageCase):
    async def test_the_page_starts_nothing_and_shows_no_terminal_state_or_name(self):
        self.board.set('agent', state='isolation_failed', detail='mcp_leak', name='<img src=x onerror=alert(1)>&')
        self.board.set('shell', state='exited', started_at=1.0, exit_status=3)
        response, dom = await self.page()
        self.assertEqual(self.board.started, [])
        for text in ('격리 실패', '종료됨', 'mcp_leak', 'onerror', 'img src'):
            self.assertNotIn(text, response.body_text, text)
        self.assertEqual(dom.tags('img'), [])

    async def test_unavailable_set_is_503_with_the_reason_and_no_workspace(self):
        self.board_ref[0] = None
        response, dom = await self.page()
        self.assertEqual(response.status, 503)
        self.assertIn('Board agent 세트를 사용할 수 없습니다', dom.body_text)
        self.assertIsNone(dom.by_id('board-workspace'))
        self.assertIsNone(dom.by_id('board-panel'))
        self.assertFalse([1 for _, attrs in dom.elements if (attrs.get('src') or '').endswith('board.js')])

    async def test_an_invalid_registry_is_503_with_its_message_and_no_workspace(self):
        self.board.invalid = 'Board registry /x/board-agents/registry.json is unusable (not JSON).'
        response, dom = await self.page()
        self.assertEqual(response.status, 503)
        self.assertIn('registry.json', dom.body_text)
        self.assertIsNone(dom.by_id('board-workspace'))


class RestartPageTests(BoardPageCase):
    """WBX-D1: the restart confirm page and its form post are gone; a restart is ``POST /api/board/terminals/{id}/restart``
    (the widget asks through the dialog first)."""

    async def test_the_confirm_page_and_its_form_post_are_gone(self):
        response, _ = await self.page('/board/restart?channel=agent')
        self.assertEqual(response.status, 404)
        _, dom = await self.page()
        csrf = [attrs for attrs in dom.tags('meta') if attrs.get('name') == 'csrf-token'][0]['content']
        response = await self.post('/board/restart', {'csrf': csrf, 'channel': 'shell'}, cookies=self.cookies)
        self.assertIn(response.status, (404, 405))
        self.assertEqual(self.board.started, [])
        links = {attrs['href'] for attrs in dom.tags('a') if 'href' in attrs}
        self.assertFalse({'/board/restart?channel=agent', '/board/restart?channel=shell'} & links)


class ScriptSourceTests(unittest.TestCase):
    """Static reads of the page scripts (seam S5). A terminal name is the owner's text: it must reach the DOM through
    ``textContent`` only (AC-38); no library, CDN or dynamic code (WBX-D52)."""

    FILES = ('board.js', 'board-layout.js', 'board-panel.js', 'board-state.js')

    def code(self, name: str) -> str:
        return re.sub(r'(?m)^\s*//.*$|(?<=\s)//[^\n]*$', '', (STATIC / name).read_text(encoding='utf-8'))

    def test_no_markup_is_built_from_text(self):
        for name in self.FILES:
            text = self.code(name)
            for needle in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'createContextualFragment',
                           'DOMParser', 'new Function', 'eval('):
                self.assertNotIn(needle, text, f'{name}: {needle}')

    def test_no_external_url_import_or_library(self):
        for name in self.FILES:
            text = self.code(name)
            self.assertEqual(re.findall(r'https?://', text), [], name)
            for spec in re.findall(r'from\s+"([^"]+)"', text) + re.findall(r'import\(\s*"([^"]+)"', text):
                self.assertTrue(spec.startswith('./'), f'{name} imports {spec}')
            self.assertNotRegex(text, r'(?i)\b(sortable|interact\.?js|gridstack|jquery|react)\b', name)

    def test_positions_are_written_through_cssom_never_through_the_style_attribute_or_cssText(self):
        text = self.code('board.js')
        self.assertIn('style.setProperty', text)
        for needle in ("setAttribute('style'", 'setAttribute("style"', 'cssText', '.style =', 'insertRule', 'createElement("style")',
                       "createElement('style')"):
            self.assertNotIn(needle, text, needle)

    def test_the_page_script_keeps_the_nonce_helper_and_ime_rule(self):
        self.assertIn('installStyleNonce', self.code('board.js'))
        css = (STATIC / 'board.css').read_text(encoding='utf-8')
        self.assertRegex(css, r'\.xterm\s+\.xterm-helper-textarea\s*\{\s*max-width:\s*none')


if __name__ == '__main__':
    unittest.main()
