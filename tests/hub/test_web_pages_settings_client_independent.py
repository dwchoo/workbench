"""WBX-A4 independent verification (seams S1 + S6): the key-issue page's client setup blocks.

Expectations come from the web-board-ux SPEC "Key 발급 결과의 client 설정 안내", BRIEF WBX-D04/D17/D19/D26 and
the real ``skills/wb-inbox/wb_inbox.py`` parser, not from ``test_web_pages_settings_client.py``. Focus: the raw key is
shown exactly once (the ``.env`` block) and nowhere else; the Hub origin is only ever the validated ``Host``.
Run with ``discover -s tests/hub -p 'test_web_pages_settings*.py'``.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
from html.parser import HTMLParser
import importlib.util
import io
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

import test_web_pages_settings as settings  # noqa: E402

from workbench.hub.web.app import OriginPolicy  # noqa: E402

RAW_KEY = settings.RAW_KEY
AGENT_LINE = ('Install the wb-inbox skill by following the install section for your agent type '
              '(Claude Code, Codex or OMP) in {origin}/skill/README.md, then use it; its settings '
              '(WB_HUB_URL, WB_API_KEY) are in <skill folder>/.env or ~/.config/omp-workbench/client.env.')


def load_wb_inbox():
    spec = importlib.util.spec_from_file_location('wb_inbox_under_test', REPO / 'skills' / 'wb-inbox' / 'wb_inbox.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Dom(HTMLParser):
    """Everything about one page that the assertions need, read with the stdlib parser."""

    VOID = {'meta', 'link', 'br', 'hr', 'input', 'img'}

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.pre: dict[str, str] = {}
        self.ids: list[str] = []
        self.buttons: list[dict] = []
        self.anchors: list[tuple[str, str]] = []
        self.scripts: list[dict] = []
        self.attr_names: set[str] = set()
        self.tags: list[str] = []
        self._pre_id: str | None = None
        self._anchor: list | None = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append(tag)
        self.attr_names.update(attrs)
        if attrs.get('id'):
            self.ids.append(attrs['id'])
        if tag == 'pre':
            self._pre_id = attrs.get('id') or ''
            self.pre[self._pre_id] = ''
        elif tag == 'button':
            self.buttons.append(attrs)
        elif tag == 'a':
            self._anchor = [attrs.get('href', ''), '']
        elif tag == 'script':
            self.scripts.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'pre':
            self._pre_id = None
        elif tag == 'a' and self._anchor is not None:
            self.anchors.append((self._anchor[0], self._anchor[1]))
            self._anchor = None

    def handle_data(self, data):
        if self._pre_id is not None:
            self.pre[self._pre_id] += data
        if self._anchor is not None:
            self._anchor[1] += data


def secret_of(raw: str) -> str:
    return raw.split('_', 2)[2]


class Case(settings.SettingsCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.wb_inbox = load_wb_inbox()

    async def issue_ok(self, **fields):
        response = await self.issue(**fields)
        self.assertEqual(response.status, 200, response.body_text)
        raw = RAW_KEY.search(response.body_text).group(0)
        return response, raw, Dom(response.body_text)

    async def issue_from(self, host: str, scheme: str, **fields):
        data = {'csrf': await self.csrf(), 'name': 'laptop', 'grade': 'requester', 'scope': 'all',
                'expires_days': '', **fields}
        return await self.post('/settings/keys', data, cookies=self.cookies, origin=f'{scheme}://{host}',
                               headers={'Host': host})

    def assert_env_parses(self, env_text: str, raw: str, origin: str) -> None:
        """The block must be accepted by the real client: its ``.env`` parser, mode check and URL validator."""
        self.assertEqual(env_text.splitlines(), [f'WB_HUB_URL={origin}', f'WB_API_KEY={raw}'])
        self.assertEqual(self.wb_inbox.parse_env_text(env_text), {'WB_HUB_URL': origin, 'WB_API_KEY': raw})
        self.assertEqual(self.wb_inbox.validate_hub_url(origin), origin)
        with tempfile.TemporaryDirectory(prefix='wb-a4-env-') as folder:
            skill = Path(folder) / 'skill'
            skill.mkdir()
            path = skill / '.env'
            path.write_text(env_text + '\n', encoding='utf-8')
            path.chmod(0o600)
            found = self.wb_inbox.Settings({'XDG_CONFIG_HOME': str(Path(folder) / 'cfg'), 'HOME': folder},
                                           str(skill / 'wb_inbox.py'))
            self.assertEqual(found.find('WB_HUB_URL'), (origin, str(path)))
            self.assertEqual(found.find('WB_API_KEY'), (raw, str(path)))
            self.assertEqual(found.warnings, [])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class RawKeyExposureTests(Case):
    async def test_the_secret_is_in_the_issue_response_exactly_once_in_any_encoding(self):
        response, raw, _ = await self.issue_ok()
        secret = secret_of(raw)
        body = response.body_text
        self.assertEqual(body.count(secret), 1)
        self.assertEqual(html.unescape(body).count(secret), 1)
        self.assertEqual(len(RAW_KEY.findall(body)), 1)
        self.assertEqual(len(re.findall(r'wbk_', body)), 1)
        self.assertNotIn(secret, ''.join(f'{k}: {v}\n' for k, v in response.headers.items()))
        self.assertIsNone(response.headers.get('Location'))

    async def test_the_raw_key_is_only_ever_inside_the_env_block(self):
        _, raw, dom = await self.issue_ok()
        holders = [name for name, text in dom.pre.items() if secret_of(raw) in text]
        self.assertEqual(holders, ['client-env'])

    async def test_no_other_page_or_later_request_shows_the_secret(self):
        sink = io.StringIO()
        with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink), self.assertNoLogs(level=0):
            response, raw, _ = await self.issue_ok()
            secret = secret_of(raw)
            seen = {}
            for path in ('/settings/keys', '/settings/audit', '/settings', '/login', '/static/copy.js',
                         '/nonexistent'):
                got = await self.get(path, cookies=self.cookies)
                seen[path] = got.body_text + ''.join(f'{k}: {v}' for k, v in got.headers.items())
            again = await self.issue()  # same form: refused, name UNIQUE
            seen['again'] = again.body_text
            seen['bad-name'] = (await self.issue(name='bad/name')).body_text
            seen['bad-grade'] = (await self.issue(grade='owner')).body_text
            seen['no-csrf'] = (await self.post('/settings/keys', {'name': 'x'}, cookies=self.cookies)).body_text
            seen['evil-origin'] = (await self.post('/settings/keys', {'csrf': await self.csrf(), 'name': 'y',
                                                                      'grade': 'requester', 'scope': 'all'},
                                                   cookies=self.cookies, origin='http://evil.example')).body_text
            seen['evil-host'] = (await self.get('/settings/keys', host='evil.example', cookies=self.cookies)).body_text
            seen['unauth'] = (await self.get('/settings/keys')).body_text
        for name, text in seen.items():
            self.assertNotIn(secret, text, name)
            self.assertNotIn(raw, text, name)
        self.assertNotIn(secret, sink.getvalue())  # stdout/stderr (hub.log lines for refusals) stay clean
        self.assertEqual(again.status, 400)

    async def test_the_secret_is_not_stored_or_audited(self):
        _, raw, _ = await self.issue_ok()
        secret = secret_of(raw)
        for table in ('audit', 'api_keys'):
            self.assertNotIn(secret, repr(self.rows(f'SELECT * FROM {table}')), table)
        self.db.close()  # flush
        for file in Path(self.state).iterdir():
            if file.is_file():
                self.assertNotIn(secret.encode(), file.read_bytes(), file.name)

    async def test_two_issues_each_show_only_their_own_key(self):
        _, first, _ = await self.issue_ok(name='one')
        second_response, second, _ = await self.issue_ok(name='two')
        self.assertNotEqual(first, second)
        self.assertNotIn(secret_of(first), second_response.body_text)

    async def test_two_concurrent_identical_submissions_show_one_key_in_total(self):
        csrf = await self.csrf()
        data = {'csrf': csrf, 'name': 'race', 'grade': 'requester', 'scope': 'all', 'expires_days': ''}
        one, two = await asyncio.gather(self.post('/settings/keys', data, cookies=self.cookies),
                                        self.post('/settings/keys', data, cookies=self.cookies))
        self.assertEqual(sorted((one.status, two.status)), [200, 400], (one.body_text, two.body_text))
        both = one.body_text + two.body_text
        self.assertEqual(len(RAW_KEY.findall(both)), 1)
        self.assertEqual(self.rows('SELECT count(*) FROM api_keys'), [(1,)])


class NoStoreTests(Case):
    async def test_every_post_response_of_the_issue_route_is_no_store_and_never_public(self):
        ok = await self.issue()
        dup = await self.issue()
        bad = await self.issue(expires_days='0')
        nocsrf = await self.post('/settings/keys', {'name': 'x'}, cookies=self.cookies)
        evil = await self.post('/settings/keys', {'csrf': await self.csrf(), 'name': 'q', 'grade': 'requester',
                                                  'scope': 'all'}, cookies=self.cookies, origin='http://evil.example')
        anon = await self.post('/settings/keys', {'name': 'x'})
        for label, response in (('200', ok), ('dup400', dup), ('bad400', bad), ('csrf403', nocsrf),
                                ('origin403', evil), ('anon303', anon)):
            with self.subTest(label):
                control = response.headers.get('Cache-Control', '')
                self.assertNotRegex(control, r'public|max-age=[1-9]')
                if response.status in (200, 400):
                    self.assertEqual(control, 'no-store')
        self.assertEqual((ok.status, dup.status, bad.status), (200, 400, 400))
        self.assertEqual(ok.headers['Cache-Control'], 'no-store')

    async def test_the_issue_response_never_sets_a_cookie_with_the_key(self):
        response, raw, _ = await self.issue_ok()
        for value in response.headers.getall('Set-Cookie', []):
            self.assertNotIn(secret_of(raw), value)

    async def test_the_key_page_is_not_cacheable_by_shared_caches(self):
        listing = await self.get('/settings/keys', cookies=self.cookies)
        control = listing.headers.get('Cache-Control', '')
        self.assertNotRegex(control, r'public|max-age=[1-9]')
        self.assertTrue(control, 'the list page declares a Cache-Control')


class EnvBlockTests(Case):
    async def test_env_block_is_exactly_two_lines_the_real_client_parser_accepts(self):
        _, raw, dom = await self.issue_ok()
        self.assert_env_parses(dom.pre['client-env'], raw, self.origin)

    async def test_env_block_is_accepted_for_https_servername_ipv6_and_upper_case_hosts(self):
        cases = []
        self.services.policy = OriginPolicy(self.bind, True, ('hub.lan', 'hub.example.org'))
        cases += [(f'hub.lan:{self.port}', 'https'), (f'hub.example.org:{self.port}', 'https'),
                  (self.host(), 'https')]
        self.services.policy = OriginPolicy('::', False, ())
        cases += [(f'[::1]:{self.port}', 'http'), (f'[2001:db8::7]:{self.port}', 'http')]
        for index, (host, scheme) in enumerate(cases):
            with self.subTest(host=host, scheme=scheme):
                self.services.policy = OriginPolicy('::' if host.startswith('[') else self.bind,
                                                    scheme == 'https', ('hub.lan', 'hub.example.org'))
                response = await self.issue_from(host, scheme, name=f'k{index}')
                self.assertEqual(response.status, 200, response.body_text)
                raw = RAW_KEY.search(response.body_text).group(0)
                dom = Dom(response.body_text)
                self.assert_env_parses(dom.pre['client-env'], raw, f'{scheme}://{host}')
                self.assertEqual(dom.pre['client-readme'], f'{scheme}://{host}/skill/README.md')
                self.assertEqual(dom.pre['client-agent'], AGENT_LINE.format(origin=f'{scheme}://{host}'))

    async def test_a_mixed_case_server_name_host_still_yields_a_url_the_client_accepts(self):
        self.services.policy = OriginPolicy(self.bind, False, ('hub.lan',))
        host = f'HUB.Lan:{self.port}'
        response = await self.issue_from(host, 'http')
        if response.status == 200:  # accepted by the Host check: the URL must still be a valid one
            raw = RAW_KEY.search(response.body_text).group(0)
            self.assert_env_parses(Dom(response.body_text).pre['client-env'], raw, f'http://{host}')
        else:  # otherwise nothing may be issued
            self.assertIn(response.status, (403, 421))
            self.assertEqual(self.rows('SELECT count(*) FROM api_keys'), [(0,)])

    async def test_the_key_text_needs_no_quoting_or_escaping_in_the_env_block(self):
        _, raw, dom = await self.issue_ok()
        self.assertRegex(raw, r'^[A-Za-z0-9_-]+$')
        self.assertNotIn('&', dom.pre['client-env'])


class OriginIsTheValidatedHostTests(Case):
    BAD_HOSTS = ('evil.example', 'evil.example:{p}', '127.0.0.1', '127.0.0.1:{q}', '127.0.0.2:{p}', 'localhost.:{p}',
                 '127.0.0.1:0{p}', '[::1]:{p}', 'evil.example@127.0.0.1:{p}', '127.0.0.1:{p}@evil.example',
                 '127.0.0.1.evil.example:{p}', '127.0.0.1:{p}"><script>alert(1)</script>',
                 '127.0.0.1:{p}/x', "127.0.0.1:{p}'onmouseover=1", '127.0.0.1:{p}\\evil',
                 '127.0.0.1:{p}#x', '127.0.0.1:{p}?x')

    def hosts(self):
        return [h.format(p=self.port, q=self.port + 1) for h in self.BAD_HOSTS]

    async def test_a_spoofed_host_gets_no_issue_page_and_issues_nothing(self):
        for host in self.hosts():
            with self.subTest(host=host):
                try:
                    response = await self.issue_from(host, 'http')
                except Exception as exc:  # the client may refuse to even send a malformed Host
                    self.assertNotIsInstance(exc, AssertionError)
                    continue
                self.assertEqual(response.status, 421, response.body_text)
                self.assertIsNone(RAW_KEY.search(response.body_text))
                self.assertNotIn('evil', response.body_text)
        self.assertEqual(self.rows('SELECT count(*) FROM api_keys'), [(0,)])
        self.assertNotIn('api_key_issued', [row[0] for row in self.audit()])

    async def test_a_spoofed_host_on_the_list_page_gets_no_template_either(self):
        for host in self.hosts():
            with self.subTest(host=host):
                try:
                    response = await self.get('/settings/keys', host=host, cookies=self.cookies)
                except Exception:
                    continue
                self.assertEqual(response.status, 421)
                self.assertNotIn('WB_HUB_URL', response.body_text)
                self.assertNotIn('evil', response.body_text)

    async def test_forwarded_headers_and_a_spoofed_origin_do_not_change_the_url(self):
        data = {'csrf': await self.csrf(), 'name': 'fwd', 'grade': 'requester', 'scope': 'all'}
        response = await self.post('/settings/keys', data, cookies=self.cookies,
                                   headers={'X-Forwarded-Host': 'evil.example', 'X-Forwarded-Proto': 'https',
                                            'Forwarded': 'host=evil.example;proto=https', 'X-Original-URL': 'http://e/'})
        self.assertEqual(response.status, 200)
        self.assertNotIn('evil', response.body_text)
        self.assertEqual(Dom(response.body_text).pre['client-readme'], f'{self.origin}/skill/README.md')

    async def test_the_scheme_is_the_listeners_not_the_requests(self):
        for tls in (False, True):
            with self.subTest(tls=tls):
                self.services.policy = OriginPolicy(self.bind, tls, ())
                response = await self.issue_from(self.host(), 'https' if tls else 'http', name=f'tls{int(tls)}')
                self.assertEqual(response.status, 200, response.body_text)
                scheme = 'https' if tls else 'http'
                self.assertTrue(Dom(response.body_text).pre['client-env'].startswith(
                    f'WB_HUB_URL={scheme}://{self.host()}\n'))
                self.assertNotIn(f'{"http" if tls else "https"}://{self.host()}', response.body_text)


class GithubLinkTests(Case):
    async def test_not_configured_means_no_github_link_on_either_page(self):
        for value in ('',):
            self.services.policy = OriginPolicy(self.bind, False, (), skill_repo_url=value)
            response, _, dom = await self.issue_ok(name=f'n{len(value)}')
            listing = Dom((await self.get('/settings/keys', cookies=self.cookies)).body_text)
            for page in (dom, listing):
                self.assertEqual([t for _, t in page.anchors if t == 'GitHub'], [])
                self.assertEqual([h for h, _ in page.anchors if 'github' in h.lower()], [])
            self.assertNotIn('GitHub', response.body_text)
            self.assertIn((f'{self.origin}/skill/README.md', f'{self.origin}/skill/README.md'), dom.anchors)

    async def test_configured_adds_exactly_one_github_link_keeping_the_hub_link(self):
        url = 'https://github.com/example/skills'
        self.services.policy = OriginPolicy(self.bind, False, (), skill_repo_url=url)
        _, _, dom = await self.issue_ok()
        self.assertEqual([h for h, t in dom.anchors if t == 'GitHub'], [url])
        self.assertIn((f'{self.origin}/skill/README.md', f'{self.origin}/skill/README.md'), dom.anchors)

    async def test_the_url_is_escaped_in_html_and_round_trips_through_the_parser(self):
        hostile = 'https://example.com/a?x=1&y="><script>alert(1)</script>&z=\'q\''
        self.services.policy = OriginPolicy(self.bind, False, (), skill_repo_url=hostile)
        response, _, dom = await self.issue_ok()
        self.assertNotIn('<script>alert(1)', response.body_text)
        self.assertEqual([h for h, t in dom.anchors if t == 'GitHub'], [hostile])
        self.assertEqual([s for s in dom.scripts if 'src' not in s], [])
        listing = (await self.get('/settings/keys', cookies=self.cookies)).body_text
        self.assertNotIn('<script>alert(1)', listing)
        self.assertEqual([h for h, t in Dom(listing).anchors if t == 'GitHub'], [hostile])

    async def test_the_github_url_never_enters_the_env_block_or_the_agent_line(self):
        url = 'https://github.com/example/skills'
        self.services.policy = OriginPolicy(self.bind, False, (), skill_repo_url=url)
        _, _, dom = await self.issue_ok()
        self.assertNotIn(url, dom.pre['client-env'])
        self.assertNotIn(url, dom.pre['client-agent'])


class AgentLineTests(Case):
    async def test_english_ascii_only_no_key_points_to_the_hub_readme(self):
        response, raw, dom = await self.issue_ok()
        line = dom.pre['client-agent']
        self.assertEqual(line, AGENT_LINE.format(origin=self.origin))
        self.assertTrue(line.isascii())
        self.assertNotRegex(line, r'[가-힣]')
        self.assertNotIn('wbk_', line)
        self.assertNotIn(secret_of(raw), line)
        self.assertNotIn('=', line.replace('WB_HUB_URL, WB_API_KEY', ''))  # no assignment of any value
        self.assertIn(f'{self.origin}/skill/README.md', line)
        self.assertEqual(len(line.splitlines()), 1)

    async def test_the_line_depends_only_on_the_origin_not_on_the_key_or_its_name(self):
        _, _, one = await self.issue_ok(name='alpha')
        _, _, two = await self.issue_ok(name='beta key')
        self.assertEqual(one.pre['client-agent'], two.pre['client-agent'])


class ResubmitAndPlaceholderTests(Case):
    async def test_resubmitting_shows_no_secret_and_no_env_with_a_real_key(self):
        _, raw, _ = await self.issue_ok()
        again = await self.issue()
        self.assertEqual(again.status, 400)
        self.assertEqual(again.headers['Cache-Control'], 'no-store')
        self.assertNotIn(secret_of(raw), again.body_text)
        env = Dom(again.body_text).pre.get('client-env', '')
        self.assertNotIn('wbk_', env)

    async def test_the_list_page_template_has_the_placeholder_never_a_real_key(self):
        _, raw, _ = await self.issue_ok()
        text = (await self.get('/settings/keys', cookies=self.cookies)).body_text
        dom = Dom(text)
        self.assertEqual(dom.pre['client-env'], f'WB_HUB_URL={self.origin}\nWB_API_KEY=<YOUR_KEY>')
        self.assertIn('&lt;YOUR_KEY&gt;', text)  # escaped, so it is text and not an element
        self.assertNotIn('<YOUR_KEY>', text)
        self.assertIsNone(RAW_KEY.search(text))
        self.assertNotIn(secret_of(raw), text)
        self.assertEqual(self.wb_inbox.parse_env_text(dom.pre['client-env'])['WB_API_KEY'], '<YOUR_KEY>')
        self.assertEqual(dom.pre['client-agent'], AGENT_LINE.format(origin=self.origin))


class CopyMarkupTests(Case):
    async def pages(self):
        _, _, issue = await self.issue_ok()
        listing = await self.get('/settings/keys', cookies=self.cookies)
        return {'issue': issue, 'list': Dom(listing.body_text)}, listing

    async def test_buttons_are_hidden_buttons_pointing_at_existing_unique_pre_ids(self):
        pages, _ = await self.pages()
        for name, dom in pages.items():
            with self.subTest(name):
                copies = [b for b in dom.buttons if 'copy' in (b.get('class') or '').split()]
                self.assertEqual(len(copies), 3)
                self.assertEqual({b.get('data-copy-target') for b in copies},
                                 {'client-env', 'client-readme', 'client-agent'})
                for button in copies:
                    self.assertIn('hidden', button)  # without JS the button does not show
                    self.assertEqual(button.get('type'), 'button')
                    self.assertIn(button['data-copy-target'], dom.pre)
                self.assertEqual(len(dom.ids), len(set(dom.ids)), 'duplicate ids')

    async def test_script_is_external_same_origin_and_there_is_no_inline_code(self):
        pages, listing = await self.pages()
        for name, dom in pages.items():
            with self.subTest(name):
                self.assertIn('/static/copy.js', [s.get('src') for s in dom.scripts])
                self.assertTrue(all('src' in s and s['src'].startswith('/static/') for s in dom.scripts))
                self.assertFalse([a for a in dom.attr_names if a.startswith('on')], dom.attr_names)
                self.assertNotIn('style', dom.attr_names)
                self.assertNotIn('style', dom.tags)
        self.assertIn("script-src 'self'", listing.headers.get('Content-Security-Policy', ''))

    async def test_a_js_less_page_still_contains_the_text_to_copy_by_hand(self):
        _, raw, dom = await self.issue_ok()
        self.assertIn(raw, dom.pre['client-env'])


class ListPageNeighboursStillWorkTests(Case):
    async def test_the_refusal_page_for_an_invalid_form_keeps_the_placeholder_template(self):
        response = await self.issue(name='')
        self.assertEqual(response.status, 400)
        dom = Dom(response.body_text)
        self.assertEqual(dom.pre.get('client-env'), f'WB_HUB_URL={self.origin}\nWB_API_KEY=<YOUR_KEY>')
        self.assertIsNone(RAW_KEY.search(response.body_text))


if __name__ == '__main__':
    unittest.main()
