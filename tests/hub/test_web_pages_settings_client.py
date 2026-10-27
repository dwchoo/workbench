"""WBX-A4 (seams S1 + S6): the key-issue result page and the key list page carry the client setup help.

``create_app(services)`` in-process (the settings test base); HTML is read with stdlib ``html.parser``. Expected
values are the web-board-ux SPEC "Key 발급 결과의 client 설정 안내" strings and BRIEF WBX-D04·D17·D19·D26, not the
implementation's. Run with ``discover -s tests/hub -p 'test_web_pages_settings*.py'``.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

import test_web_pages_settings as settings  # noqa: E402

from workbench.hub.web.app import OriginPolicy  # noqa: E402

RAW_KEY = settings.RAW_KEY
AGENT_LINE = ('Install the wb-inbox skill by following {origin}/skill/README.md, then use it; its settings '
              '(WB_HUB_URL, WB_API_KEY) are in <skill folder>/.env or ~/.config/omp-workbench/client.env.')
REPO_URL = 'https://github.com/example/workbench-skills'


class Page(HTMLParser):
    """pre texts by id, buttons, anchors, details and script sources of one page."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.pre: dict[str, str] = {}
        self.buttons: list[dict] = []
        self.links: list[tuple[str, str]] = []
        self.details: list[dict] = []
        self.scripts: list[dict] = []
        self.inline_scripts = 0
        self._stack: list[str] = []
        self._link: list | None = None
        self._open_details: list[dict] = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'pre':
            self._stack.append(attrs.get('id') or '')
            self.pre.setdefault(attrs.get('id') or '', '')
        elif self._stack:
            self._stack.append('')
        if tag == 'button':
            self.buttons.append(attrs)
        elif tag == 'a':
            self._link = [attrs.get('href', ''), '']
        elif tag == 'details':
            entry = {'attrs': attrs, 'pre': []}
            self.details.append(entry)
            self._open_details.append(entry)
        elif tag == 'script':
            self.scripts.append(attrs)
            if 'src' not in attrs:
                self.inline_scripts += 1
        if tag == 'pre' and self._open_details:
            self._open_details[-1]['pre'].append(attrs.get('id') or '')

    def handle_endtag(self, tag):
        if self._stack:
            self._stack.pop()
        if tag == 'a' and self._link is not None:
            self.links.append((self._link[0], self._link[1]))
            self._link = None
        elif tag == 'details' and self._open_details:
            self._open_details.pop()

    def handle_data(self, data):
        for name in self._stack[:1]:
            self.pre[name] += data
        if self._link is not None:
            self._link[1] += data


class ClientSetupCase(settings.SettingsCase):
    async def issue_page(self, origin: str | None = None, **fields) -> tuple[Page, str, object]:
        if origin is None:
            response = await self.issue(**fields)
        else:  # the Origin check follows the listener's scheme
            data = {'csrf': await self.csrf(), 'name': 'laptop', 'grade': 'requester', 'scope': 'all',
                    'expires_days': '', **fields}
            response = await self.post('/settings/keys', data, cookies=self.cookies, origin=origin)
        self.assertEqual(response.status, 200, response.body_text)
        return Page(response.body_text), RAW_KEY.search(response.body_text).group(0), response


class EnvBlockTests(ClientSetupCase):
    async def test_issue_result_has_the_env_block_with_hub_url_and_the_raw_key(self):
        page, raw, _ = await self.issue_page()
        self.assertEqual(page.pre['client-env'], f'WB_HUB_URL={self.origin}\nWB_API_KEY={raw}')


class OriginTests(ClientSetupCase):
    async def test_hub_url_is_the_origin_the_browser_used_http_and_https(self):
        for tls, scheme in ((False, 'http'), (True, 'https')):
            with self.subTest(scheme=scheme):
                self.services.policy = OriginPolicy(self.bind, tls, self.server_names)
                origin = f'{scheme}://{self.host()}'
                page, raw, _ = await self.issue_page(origin, name=f'k-{scheme}')
                self.assertEqual(page.pre['client-env'], f'WB_HUB_URL={origin}\nWB_API_KEY={raw}')

    async def test_hub_url_follows_a_server_name_host(self):
        self.services.policy = OriginPolicy(self.bind, False, ('hub.lan',))
        host = f'hub.lan:{self.port}'
        data = {'csrf': await self.csrf(), 'name': 'laptop', 'grade': 'requester', 'scope': 'all'}
        response = await self.post('/settings/keys', data, cookies=self.cookies, origin=f'http://{host}',
                                   headers={'Host': host})
        self.assertEqual(response.status, 200, response.body_text)
        page, raw = Page(response.body_text), RAW_KEY.search(response.body_text).group(0)
        self.assertEqual(page.pre['client-env'], f'WB_HUB_URL=http://{host}\nWB_API_KEY={raw}')


class ReadmeLinkTests(ClientSetupCase):
    async def test_the_hub_readme_link_is_always_there_and_github_only_when_configured(self):
        page, _, _ = await self.issue_page()
        self.assertEqual(page.pre['client-readme'], f'{self.origin}/skill/README.md')
        self.assertIn((f'{self.origin}/skill/README.md', f'{self.origin}/skill/README.md'), page.links)
        self.assertEqual([href for href, _ in page.links if 'github' in href], [])
        self.assertNotIn('GitHub', [text for _, text in page.links])

    async def test_a_configured_skill_repo_url_adds_a_github_link(self):
        self.services.policy = OriginPolicy(self.bind, False, self.server_names, skill_repo_url=REPO_URL)
        page, _, _ = await self.issue_page()
        self.assertIn((REPO_URL, 'GitHub'), page.links)
        self.assertIn((f'{self.origin}/skill/README.md', f'{self.origin}/skill/README.md'), page.links)


class AgentLineTests(ClientSetupCase):
    async def test_the_agent_line_is_the_english_sentence_without_the_key(self):
        page, raw, response = await self.issue_page()
        self.assertEqual(page.pre['client-agent'], AGENT_LINE.format(origin=self.origin))
        self.assertNotIn(raw[15:], page.pre['client-agent'])
        self.assertNotIn('wbk_', page.pre['client-agent'])
        self.assertEqual(len(RAW_KEY.findall(response.body_text)), 1)  # the .env block is the only place


class ResubmitTests(ClientSetupCase):
    async def test_the_same_form_again_is_refused_and_shows_no_key(self):
        page, raw, _ = await self.issue_page()
        again = await self.issue()
        self.assertEqual(again.status, 400, again.body_text)
        self.assertEqual(again.headers['Cache-Control'], 'no-store')
        self.assertNotIn(raw[15:], again.body_text)
        self.assertIsNone(RAW_KEY.search(again.body_text))
        env = Page(again.body_text).pre.get('client-env', '')  # the refusal is the list page: placeholder only
        self.assertFalse(env.endswith(raw), env)


class KeyListPlaceholderTests(ClientSetupCase):
    async def listing(self) -> tuple[Page, str]:
        response = await self.get('/settings/keys', cookies=self.cookies)
        self.assertEqual(response.status, 200)
        return Page(response.body_text), response.body_text

    async def test_the_list_page_has_a_folded_details_with_your_key_placeholder(self):
        self.services.policy = OriginPolicy(self.bind, False, self.server_names, skill_repo_url=REPO_URL)
        page, text = await self.listing()
        self.assertEqual(page.pre['client-env'], f'WB_HUB_URL={self.origin}\nWB_API_KEY=<YOUR_KEY>')
        self.assertEqual(page.pre['client-readme'], f'{self.origin}/skill/README.md')
        self.assertEqual(page.pre['client-agent'], AGENT_LINE.format(origin=self.origin))
        mine = [item for item in page.details if item['pre']]  # the nav menu is a <details> too
        self.assertEqual(len(mine), 1)
        self.assertNotIn('open', mine[0]['attrs'])
        self.assertEqual(set(mine[0]['pre']), {'client-env', 'client-readme', 'client-agent'})
        self.assertIn((REPO_URL, 'GitHub'), page.links)
        self.assertIsNone(RAW_KEY.search(text))

    async def test_the_list_page_never_shows_an_issued_key_next_to_the_placeholder(self):
        _, raw, _ = await self.issue_page()
        page, text = await self.listing()
        self.assertNotIn(raw[15:], text)
        self.assertIn('<YOUR_KEY>', page.pre['client-env'])


class CopyButtonTests(ClientSetupCase):
    BLOCKS = {'client-env', 'client-readme', 'client-agent'}

    def assert_copy_markup(self, page: Page, text: str) -> None:
        copies = [button for button in page.buttons if button.get('class') == 'copy']
        self.assertEqual({button.get('data-copy-target') for button in copies}, self.BLOCKS)
        self.assertEqual(len(copies), 3)
        for button in copies:
            self.assertEqual(button.get('type'), 'button')  # never submits a form
            self.assertIn('hidden', button)  # inert until copy.js shows it
        self.assertEqual([s.get('src') for s in page.scripts if s.get('src') == '/static/copy.js'],
                         ['/static/copy.js'])
        self.assertEqual(page.inline_scripts, 0)
        self.assertNotIn(' style=', text)

    async def test_the_issue_result_has_a_copy_button_per_block_and_loads_copy_js(self):
        page, _, response = await self.issue_page()
        self.assert_copy_markup(page, response.body_text)

    async def test_the_list_page_has_the_same_buttons(self):
        response = await self.get('/settings/keys', cookies=self.cookies)
        self.assert_copy_markup(Page(response.body_text), response.body_text)

    async def test_copy_js_is_served_as_a_static_script(self):
        response = await self.get('/static/copy.js')
        self.assertEqual(response.status, 200)
        self.assertIn('javascript', response.headers['Content-Type'])
        self.assertNotRegex(response.body_text, r'\beval\s*\(|new Function|document\.write|innerHTML')


if __name__ == '__main__':
    unittest.main()
