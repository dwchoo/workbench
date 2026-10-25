"""Korean (Hangul) text in the Hub web pages and MCP tools (seams T2 and T4).

Pages: the real ``create_app`` with the request pages over a real inbox store (``test_web_pages_requests``), the
settings pages (``test_web_pages_settings``) and the login page (``test_hub_web_auth``). MCP: the real loopback
Hub and the stdlib MCP client (``test_hub_mcp``). Expected values are the test's own Hangul text, the SPEC
"입력 한도" table (title 1~200 Unicode code points, body <= 64 KiB and comment <= 16 KiB of UTF-8 bytes) and rows
read back with stdlib ``sqlite3``. Run with ``discover -s tests/hub -p 'test_web_hangul.py'``.
"""
from __future__ import annotations

import json
import unicodedata
import unittest
from urllib.parse import urlencode

import aiohttp

import test_hub_mcp as mcp_case  # module imports: their test classes stay uncollected here
import test_hub_web_auth as web_auth
import test_web_pages_requests as request_case
import test_web_pages_settings as settings_case

Page = request_case.Page
TITLE_MAX = 200
BODY_MAX = 64 * 1024
COMMENT_MAX = 16 * 1024


def hangul_bytes(size: int) -> str:
    """Hangul syllables (3 UTF-8 bytes each) padded with ASCII to exactly ``size`` UTF-8 bytes."""
    text = '한' * (size // 3) + 'a' * (size % 3)
    assert len(text.encode('utf-8')) == size
    return text


class HangulRequestPageTests(request_case.RequestsCase):
    async def create(self, title: str, body: str, *, multipart: bool = False):
        _, page = await self.page('/requests/new')
        form = page.form('/requests')
        fields = {'csrf': form['fields']['csrf'], 'project': self.a, 'nonce': form['fields']['nonce'],
                  'title': title, 'body': body}
        if not multipart:
            return await self.post('/requests', fields, cookies=self.cookies)
        data = aiohttp.FormData()
        for name, value in fields.items():
            data.add_field(name, value, content_type='text/plain; charset=utf-8')
        return await self.post('/requests', data, cookies=self.cookies)

    def stored(self) -> list[tuple]:
        return self.db_rows('SELECT title, body FROM requests ORDER BY seq')

    async def test_hangul_title_and_body_are_stored_and_shown_exactly_from_both_form_encodings(self):
        cases = [('urlencoded', False, '한글 제목 ㄱㄴ \u1112\u1161\u11ab 👍', '본문 첫 줄\r\n둘째 줄 😀'),
                 ('multipart', True, '멀티파트 제목 \u3164', '여러\r\n줄 본문')]
        for name, multipart, title, body in cases:
            with self.subTest(name):
                response = await self.create(title, body, multipart=multipart)
                self.assertEqual(response.status, 303, response.body_text[:500])
                self.assertEqual(self.stored()[-1], (title, body.replace('\r\n', '\n')))
                shown, page = await self.page(response.headers['Location'])
                self.assertIn(title, page.body_text)
                self.assertIn(body.replace('\r\n', '\n'), page.pre)
                self.assertEqual(shown.headers['Content-Type'], 'text/html; charset=utf-8')

    async def test_the_title_limit_counts_code_points_at_the_exact_boundary(self):
        exact = '가' * (TITLE_MAX - 2) + '😀' + '\u1100'  # 200 code points, 201 UTF-16 units
        response = await self.create(exact, 'b')
        self.assertEqual(response.status, 303, response.body_text[:500])
        self.assertEqual(self.stored()[-1][0], exact)
        over = '가' * TITLE_MAX + '나'
        response = await self.create(over, '본문은 그대로')
        page = Page(response.body_text)
        self.assertEqual(response.status, 400)
        self.assertIn('입력이 한도를 넘었습니다', page.body_text)
        self.assertEqual(page.form('/requests')['fields']['title'], over)
        self.assertIn('본문은 그대로', page.body_text)
        self.assertEqual(len(self.stored()), 1)

    async def test_the_body_limit_counts_utf8_bytes_at_the_exact_boundary(self):
        response = await self.create('경계', hangul_bytes(BODY_MAX))
        self.assertEqual(response.status, 303, response.body_text[:300])
        self.assertEqual(self.stored()[-1][1], hangul_bytes(BODY_MAX))
        over = hangul_bytes(BODY_MAX - 2) + '한'  # 65537 bytes: one Hangul syllable past the limit
        response = await self.create('넘침', over)
        page = Page(response.body_text)
        self.assertEqual(response.status, 400)
        self.assertIn('입력이 한도를 넘었습니다', page.body_text)
        self.assertEqual(page.form('/requests')['fields']['title'], '넘침')
        self.assertIn(over, page.body_text)
        self.assertEqual(len(self.stored()), 1)

    async def test_hangul_filler_characters_are_letters_not_refused_controls(self):
        for title in ('\u3164 채움', '\u115f\u1160 초성 중성 채움', '\uffa0반각 채움'):
            with self.subTest(title=title):
                response = await self.create(title, 'b')
                self.assertEqual(response.status, 303, response.body_text[:300])
                self.assertEqual(self.stored()[-1][0], title)

    async def test_a_zero_width_joiner_emoji_sequence_is_a_title_but_other_format_characters_are_not(self):
        family = '가족 \U0001f468\u200d\U0001f469\u200d\U0001f467 한글'
        response = await self.create(family, 'b')
        self.assertEqual(response.status, 303, response.body_text[:300])
        self.assertEqual(self.stored()[-1][0], family)
        for bad in ('\u200b', '\u202e', '\u2066', '\ufeff', '\u200e', '\u2028'):
            with self.subTest(bad=ascii(bad)):
                response = await self.create(f'제목{bad}끝', 'b')
                self.assertEqual(response.status, 400)
                self.assertIn('입력이 올바르지 않습니다', Page(response.body_text).body_text)
        self.assertEqual(len(self.stored()), 1)

    async def test_the_title_fields_do_not_cap_utf16_units_below_the_code_point_limit(self):
        # HTML maxlength counts UTF-16 code units: maxlength=200 would stop 200 code points with an emoji.
        request_id = self.add('제목', '본문')
        for path, action in (('/requests/new', '/requests'), (f'/requests/{request_id}/edit',
                                                                f'/requests/{request_id}/edit')):
            with self.subTest(path):
                _, page = await self.page(path)
                titles = [attrs for attrs in page.tags('input') if attrs.get('name') == 'title']
                self.assertEqual(len(titles), 1)
                maxlength = titles[0].get('maxlength')
                self.assertTrue(maxlength is None or int(maxlength) >= 2 * TITLE_MAX, maxlength)

    async def test_edit_and_comment_keep_hangul_and_the_comment_limit_counts_bytes(self):
        request_id = self.add('원래 제목', '원래 본문')
        _, page = await self.page(f'/requests/{request_id}/edit')
        form = page.form(f'/requests/{request_id}/edit')
        response = await self.post(f'/requests/{request_id}/edit',
                                   {'csrf': form['fields']['csrf'], 'base_version': form['fields']['base_version'],
                                    'title': '바뀐 제목 😀', 'body': '바뀐 본문'}, cookies=self.cookies)
        self.assertEqual(response.status, 303, response.body_text[:300])
        self.assertEqual(self.stored()[-1], ('바뀐 제목 😀', '바뀐 본문'))
        exact = hangul_bytes(COMMENT_MAX)
        response = await self.submit(f'/requests/{request_id}/comments', {'body': exact})
        self.assertEqual(response.status, 303, response.body_text[:300])
        over = hangul_bytes(COMMENT_MAX - 2) + '글'
        response = await self.submit(f'/requests/{request_id}/comments', {'body': over})
        self.assertEqual(response.status, 400)
        self.assertIn(over, response.body_text)
        self.assertEqual(self.db_rows('SELECT body FROM comments WHERE request_id = ?', request_id), [(exact,)])

    async def test_every_page_declares_utf8(self):
        request_id = self.add('제목', '본문')
        for path in ('/requests', '/requests/new', f'/requests/{request_id}', f'/requests/{request_id}/edit',
                     '/requests?q=%ED%95%9C%EA%B8%80'):
            with self.subTest(path):
                response, page = await self.page(path)
                self.assertEqual(response.headers['Content-Type'], 'text/html; charset=utf-8')
                self.assertIn({'charset': 'utf-8'}, page.tags('meta'))

    async def test_search_finds_hangul_also_when_one_side_is_decomposed(self):
        self.add('한글 검색 대상', '본문')
        self.add('other', 'body')
        for query in ('검색', unicodedata.normalize('NFD', '검색')):
            with self.subTest(query=ascii(query)):
                _, page = await self.page('/requests?' + urlencode({'q': query}))
                titles = [row[2] for row in page.data_rows()]
                self.assertEqual(len(titles), 1, titles)
                self.assertIn('한글 검색 대상', titles[0])
        self.add(unicodedata.normalize('NFD', '분해된 제목'), '본문')
        _, page = await self.page('/requests?' + urlencode({'q': '분해'}))
        self.assertEqual(len(page.data_rows()), 1)


class HangulSettingsTests(settings_case.SettingsCase):
    async def test_a_hangul_key_name_is_refused_with_the_spec_reason_in_utf8_and_issues_nothing(self):
        # SPEC "API key": the name is 1~64 of [A-Za-z0-9 ._-] (it becomes the ``key:<name>`` origin label).
        response = await self.issue(name='노트북 키')
        self.assertEqual(response.status, 400)
        self.assertEqual(response.headers['Content-Type'], 'text/html; charset=utf-8')
        self.assertIn('영문·숫자·공백·점·밑줄·하이픈만 쓸 수 있습니다', response.body_text)
        self.assertEqual(self.rows('SELECT name FROM api_keys'), [])


class HangulPasswordTests(web_auth.WebAppCase):
    async def test_a_hangul_password_logs_in(self):
        self.passwords.set_password('한글 비밀번호 열쇠')
        self.assertEqual((await self.login('한글 비밀번호 열쇠')).status, 303)
        self.assertNotEqual((await self.login('한글 비밀번호 열쇄')).status, 303)


class HangulMcpTests(mcp_case.ToolCase):
    def test_hangul_round_trips_through_mcp_json(self):
        title, body = '한글 요청 \u1112\u1161\u11ab ㄱ', '본문 😀\n둘째 줄'
        value = self.submit(title=title, body=body)
        shown = self.r1.tool('inbox_get', request_id=value['request_id'])['request']
        self.assertEqual((shown['title'], shown['body']), (title, body))
        self.assertEqual(self.requests()[-1][4], title)
        result = self.r1.call('inbox_get', request_id=value['request_id'])
        self.assertEqual(json.loads(result['content'][0]['text'])['request']['title'], title)
        found = self.r1.tool('inbox_list', query='한글 요청')['requests']
        self.assertEqual([item['request_id'] for item in found], [value['request_id']])

    def test_inbox_projects_query_matches_hangul_names_composed_or_decomposed(self):
        board = self.hub.client(self.hub.keys.rotate_system_key(now=self.hub.now[0]).raw).start()
        self.hub.rows[0]['name'] = '알파 프로젝트'
        self.hub.rows[1]['name'] = unicodedata.normalize('NFD', '베타 실험')
        for query, expected in (('알파', '알파 프로젝트'), (unicodedata.normalize('NFD', '알파'), '알파 프로젝트'),
                                ('베타', unicodedata.normalize('NFD', '베타 실험'))):
            with self.subTest(query=ascii(query)):
                names = [item['name'] for item in board.tool('inbox_projects', query=query)['projects']]
                self.assertEqual(names, [expected])


if __name__ == '__main__':
    unittest.main()
