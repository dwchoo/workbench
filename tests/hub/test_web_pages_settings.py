"""TI-07 settings pages (seam T2): API keys, audit log, settings navigation.

``create_app(services)`` (``services.projects`` = registry view) in-process with aiohttp's
``TestServer``; real ``auth.json``/``hub.sqlite3`` in a temp dir. Expected values are the SPEC
strings and rows read back with stdlib ``sqlite3``. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest
import unittest.mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

import test_hub_web_auth as base  # noqa: E402

from workbench.hub import auth  # noqa: E402

PROJECTS = [('p-alpha', 'Alpha'), ('p-beta', 'Beta')]
CSRF = re.compile(r'name="csrf" value="([^"]+)"')
RAW_KEY = re.compile(r'wbk_[a-z2-7]{10}_[A-Za-z0-9_-]{43}')


class SettingsCase(base.WebAppCase):
    projects = PROJECTS

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.services.projects = lambda: list(self.projects)  # the Hub supplies the registry view
        self.session = await self.session_cookie()
        self.cookies = {'wb_hub_session': self.session}

    async def csrf(self, path: str = '/settings/keys') -> str:
        page = await self.get(path, cookies=self.cookies)
        self.assertEqual(page.status, 200, page.body_text)
        return CSRF.search(page.body_text).group(1)

    async def issue(self, **fields):
        data = {'csrf': await self.csrf(), 'name': 'laptop', 'grade': 'requester', 'scope': 'all',
                'expires_days': '', **fields}
        return await self.post('/settings/keys', data, cookies=self.cookies)


class SettingsNavigationTests(SettingsCase):
    async def test_settings_page_links_to_keys_and_audit(self):
        page = await self.get('/settings', cookies=self.cookies)
        self.assertEqual(page.status, 200)
        for href in ('/settings/keys', '/settings/audit'):
            self.assertIn(f'href="{href}"', page.body_text)

    async def test_settings_subpages_need_a_session(self):
        for path in ('/settings/keys', '/settings/audit', '/settings/keys/abcdefghij/revoke'):
            response = await self.get(path)
            self.assertEqual((response.status, response.headers.get('Location')), (303, '/login'), path)
        response = await self.post('/settings/keys', {'name': 'x'})
        self.assertEqual((response.status, response.headers.get('Location')), (303, '/login'))


class KeyIssueTests(SettingsCase):
    async def test_form_lists_registry_projects_and_posts_with_csrf(self):
        page = await self.get('/settings/keys', cookies=self.cookies)
        self.assertEqual(page.status, 200)
        self.assertIn('action="/settings/keys"', page.body_text)
        self.assertRegex(page.body_text, r'name="csrf" value="[^"]+"')
        for project_id, name in PROJECTS:
            self.assertIn(f'value="{project_id}"', page.body_text)
            self.assertIn(name, page.body_text)
        for field in ('name="name"', 'name="grade"', 'name="scope"', 'name="expires_days"'):
            self.assertIn(field, page.body_text)

    async def test_form_defaults_to_chosen_projects_with_none_preselected(self):
        """Security review fix 3: a new key is not "every project" unless the owner picks that."""
        import re
        page = (await self.get('/settings/keys', cookies=self.cookies)).body_text
        radios = {match.group(1): 'checked' in match.group(0)
                  for match in re.finditer(r'<input[^>]*name="scope"[^>]*value="([a-z]+)"[^>]*>', page)}
        self.assertEqual(radios, {'all': False, 'projects': True}, radios)
        boxes = re.findall(r'<input[^>]*name="project"[^>]*>', page)
        self.assertEqual(len(boxes), len(PROJECTS))
        self.assertFalse(any('checked' in box for box in boxes), boxes)

    async def test_issue_shows_the_raw_key_once_and_only_there(self):
        response = await self.issue()
        self.assertEqual(response.status, 200, response.body_text)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        raw = RAW_KEY.search(response.body_text).group(0)
        self.assertEqual(len(RAW_KEY.findall(response.body_text)), 1)
        self.assertIsNone(response.headers.get('Location'))
        self.assertNotIn(raw, ''.join(f'{k}: {v}' for k, v in response.headers.items()))
        principal = self.services.api_keys.verify(raw, self.now[0])
        self.assertEqual((principal.name, principal.grade, principal.scope), ('laptop', 'requester', {'all': True}))
        listing = await self.get('/settings/keys', cookies=self.cookies)
        self.assertNotIn(raw, listing.body_text)
        self.assertNotIn(raw[15:], listing.body_text)
        self.assertNotIn(raw, json.dumps(self.rows('SELECT * FROM audit')))
        self.assertIn(raw.split('_')[1], listing.body_text)  # the key id is shown, the secret is not

    async def test_issue_is_audited_without_the_raw_key(self):
        response = await self.issue(grade='editor', scope='projects', project='p-alpha', expires_days='30')
        raw = RAW_KEY.search(response.body_text).group(0)
        issued = [row for row in self.audit() if row[0] == 'api_key_issued']
        self.assertEqual(len(issued), 1)
        event, actor, addr, detail = issued[0]
        self.assertEqual((actor, addr), ('owner', '127.0.0.1'))
        self.assertEqual(detail, {'key_id': raw.split('_')[1], 'name': 'laptop', 'grade': 'editor',
                                  'scope': {'projects': ['p-alpha']}, 'expires_at': self.now[0] + 30 * 86400.0})
        self.assertNotIn(raw[15:], json.dumps(self.rows('SELECT * FROM audit')))


class KeyIssueRefusalTests(SettingsCase):
    async def assert_refused(self, response, text: str) -> None:
        self.assertEqual(response.status, 400, response.body_text)
        self.assertIn(text, response.body_text)
        self.assertIsNone(RAW_KEY.search(response.body_text))
        self.assertEqual(self.rows('SELECT count(*) FROM api_keys'), [(0,)])
        self.assertNotIn('api_key_issued', [row[0] for row in self.audit()])

    async def test_invalid_input_issues_nothing(self):
        await self.assert_refused(await self.issue(name=''), '이름은 1~64자')
        await self.assert_refused(await self.issue(name='bad/name'), '이름은 1~64자')
        await self.assert_refused(await self.issue(name='board-agent'), '시스템이 쓰는 이름')
        await self.assert_refused(await self.issue(grade='owner'), '등급을 고르세요')
        await self.assert_refused(await self.issue(expires_days='0'), '1~3650일')
        await self.assert_refused(await self.issue(expires_days='3651'), '1~3650일')
        await self.assert_refused(await self.issue(expires_days='ten'), '1~3650일')

    async def test_scope_must_name_registered_projects(self):
        await self.assert_refused(await self.issue(scope='projects'), '범위를 고르세요')
        await self.assert_refused(await self.issue(scope='projects', project='p-unknown'), '범위를 고르세요')
        await self.assert_refused(await self.issue(scope='everything'), '범위를 고르세요')
        accepted = await self.issue(scope='projects', project=['p-alpha', 'p-beta'])
        self.assertEqual(accepted.status, 200)
        self.assertEqual(self.services.api_keys.list()[0].scope, {'projects': ['p-alpha', 'p-beta']})

    async def test_duplicate_name_is_refused_and_the_form_does_not_echo_a_key(self):
        self.assertEqual((await self.issue()).status, 200)
        again = await self.issue()
        self.assertEqual(again.status, 400)
        self.assertIn('같은 이름', again.body_text)
        self.assertIsNone(RAW_KEY.search(again.body_text))
        self.assertEqual(self.rows('SELECT count(*) FROM api_keys'), [(1,)])

    async def test_issue_without_csrf_is_refused_and_changes_nothing(self):
        response = await self.post('/settings/keys', {'name': 'laptop', 'grade': 'requester', 'scope': 'all'},
                                   cookies=self.cookies)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.rows('SELECT count(*) FROM api_keys'), [(0,)])

    async def test_a_foreign_origin_is_refused(self):
        data = {'csrf': await self.csrf(), 'name': 'laptop', 'grade': 'requester', 'scope': 'all'}
        response = await self.post('/settings/keys', data, cookies=self.cookies, origin='http://evil.example')
        self.assertEqual(response.status, 403)
        self.assertEqual(self.rows('SELECT count(*) FROM api_keys'), [(0,)])


def table_rows(page: str) -> list[str]:
    return re.findall(r'<tr>.*?</tr>', page)


class KeyListTests(SettingsCase):
    def row_of(self, page: str, needle: str) -> str:
        return next(row for row in table_rows(page) if needle in row)

    async def test_list_shows_name_grade_scope_last_use_expiry_and_status(self):
        raw = RAW_KEY.search((await self.issue(name='ci bot', grade='editor', scope='projects',
                                               project='p-beta', expires_days='10')).body_text).group(0)
        self.now[0] += 60
        self.services.api_keys.verify(raw, self.now[0])
        self.services.api_keys.flush()
        row = self.row_of((await self.get('/settings/keys', cookies=self.cookies)).body_text, 'ci bot')
        for text in ('ci bot', raw.split('_')[1], 'editor', 'p-beta', '1970-01-12 13:47:40 UTC',
                     '1970-01-22 13:46:40 UTC', '사용 중'):
            self.assertIn(text, row)
        self.assertNotIn('폐기됨', row)

    async def test_list_shows_never_used_and_no_expiry_as_dashes_and_none(self):
        await self.issue(name='fresh')
        row = self.row_of((await self.get('/settings/keys', cookies=self.cookies)).body_text, 'fresh')
        self.assertIn('<td>-</td>', row)
        self.assertIn('<td>없음</td>', row)
        self.assertIn('<td>전체</td>', row)

    async def test_expired_keys_are_marked_expired(self):
        self.services.api_keys.issue('short', 'requester', {'all': True}, 1, now=self.now[0] - 2 * 86400)
        row = self.row_of((await self.get('/settings/keys', cookies=self.cookies)).body_text, 'short')
        self.assertIn('만료됨', row)

    async def test_system_key_is_labelled_and_has_no_revoke_link(self):
        issued = self.services.api_keys.rotate_system_key(now=self.now[0])
        page = (await self.get('/settings/keys', cookies=self.cookies)).body_text
        row = self.row_of(page, 'Board agent(시스템)')
        self.assertIn(issued.key_id, row)
        self.assertNotIn('/revoke', row)
        self.assertNotIn(issued.raw, page)
        self.assertNotIn('>board-agent<', page)

    async def test_user_supplied_text_is_escaped(self):
        self.projects = [('p-x', '<script>alert(1)</script>')]
        page = (await self.get('/settings/keys', cookies=self.cookies)).body_text
        self.assertNotIn('<script>alert', page)
        self.assertIn('&lt;script&gt;', page)


class KeyRevokeTests(SettingsCase):
    async def make(self, name: str = 'laptop') -> tuple[str, str]:
        raw = RAW_KEY.search((await self.issue(name=name)).body_text).group(0)
        return raw, raw.split('_')[1]

    async def test_confirm_page_shows_the_key_and_posts_with_csrf_but_changes_nothing(self):
        raw, key_id = await self.make()
        page = await self.get(f'/settings/keys/{key_id}/revoke', cookies=self.cookies)
        self.assertEqual(page.status, 200)
        self.assertIn('laptop', page.body_text)
        self.assertIn(f'action="/settings/keys/{key_id}/revoke"', page.body_text)
        self.assertRegex(page.body_text, r'name="csrf" value="[^"]+"')
        self.assertIn('href="/settings/keys"', page.body_text)  # cancel
        self.assertNotIn(raw, page.body_text)
        self.services.api_keys.verify(raw, self.now[0])  # still valid

    async def test_revoke_takes_effect_at_once_and_is_audited(self):
        raw, key_id = await self.make()
        csrf = await self.csrf(f'/settings/keys/{key_id}/revoke')
        response = await self.post(f'/settings/keys/{key_id}/revoke', {'csrf': csrf}, cookies=self.cookies)
        self.assertEqual((response.status, response.headers.get('Location')), (303, '/settings/keys'))
        with self.assertRaises(auth.ApiKeyRefused) as refused:
            self.services.api_keys.verify(raw, self.now[0])
        self.assertEqual(refused.exception.reason, 'revoked')
        revoked = [row for row in self.audit() if row[0] == 'api_key_revoked']
        self.assertEqual(revoked, [('api_key_revoked', 'owner', '127.0.0.1',
                                    {'key_id': key_id, 'name': 'laptop', 'grade': 'requester',
                                     'scope': {'all': True}, 'expires_at': None})])
        row = next(row for row in table_rows((await self.get('/settings/keys', cookies=self.cookies)).body_text)
                   if key_id in row)
        self.assertIn('폐기됨', row)
        self.assertNotIn('/revoke', row)

    async def test_revoke_without_csrf_or_by_get_changes_nothing(self):
        raw, key_id = await self.make()
        self.assertEqual((await self.post(f'/settings/keys/{key_id}/revoke', {}, cookies=self.cookies)).status, 403)
        await self.get(f'/settings/keys/{key_id}/revoke', cookies=self.cookies)
        self.services.api_keys.verify(raw, self.now[0])
        self.assertNotIn('api_key_revoked', [row[0] for row in self.audit()])

    async def test_unknown_system_and_repeated_revocations_are_refused_without_audit(self):
        _raw, key_id = await self.make()
        system = self.services.api_keys.rotate_system_key(now=self.now[0])
        csrf = await self.csrf()
        revoke = '/settings/keys/{}/revoke'
        for missing in ('abcdefghij', 'x', 'ABCDEFGHIJ', '..%2f'):
            self.assertEqual((await self.get(revoke.format(missing), cookies=self.cookies)).status, 404, missing)
            self.assertEqual((await self.post(revoke.format(missing), {'csrf': csrf}, cookies=self.cookies)).status,
                             404, missing)
        self.assertEqual((await self.get(revoke.format(system.key_id), cookies=self.cookies)).status, 403)
        self.assertEqual((await self.post(revoke.format(system.key_id), {'csrf': csrf}, cookies=self.cookies)).status,
                         403)
        self.services.api_keys.verify(system.raw, self.now[0])
        self.assertEqual((await self.post(revoke.format(key_id), {'csrf': csrf}, cookies=self.cookies)).status, 303)
        self.assertEqual((await self.post(revoke.format(key_id), {'csrf': csrf}, cookies=self.cookies)).status, 409)
        self.assertEqual((await self.get(revoke.format(key_id), cookies=self.cookies)).status, 409)
        self.assertEqual([row[0] for row in self.audit()].count('api_key_revoked'), 1)


class AuditPageTests(SettingsCase):
    def record(self, event: str, detail: dict, *, actor: str = 'owner', addr: str | None = '10.0.0.5') -> None:
        self.now[0] += 1
        self.services.audit.record(event, actor, addr, detail, now=self.now[0])

    async def test_newest_first_with_event_time_actor_address_and_detail(self):
        self.record('login_failure', {'delay_seconds': 4}, actor='anonymous')
        self.record('password_changed', {'source': 'web', 'invalidated_sessions': 2})
        page = await self.get('/settings/audit', cookies=self.cookies)
        self.assertEqual(page.status, 200)
        rows = table_rows(page.body_text)
        self.assertEqual(len(rows), 1 + 3)  # header, two recorded rows (newest first), the login from setUp
        first, second = rows[1], rows[2]
        self.assertIn('password_changed', first)
        for text in ('owner', '10.0.0.5', '1970-01-12 13:46:42 UTC', 'invalidated_sessions', '2'):
            self.assertIn(text, first)
        self.assertIn('login_failure', second)
        self.assertIn('anonymous', second)
        self.assertEqual(page.headers['Cache-Control'], 'private, no-cache')  # WBX-A2 (SPEC cache table)

    async def test_pages_of_fifty_with_links_and_junk_page_numbers(self):
        for number in range(120):
            self.record('login_failure', {'delay_seconds': number}, actor='anonymous')
        counts = {}
        junk = ('?page=--5', '?page=+2', '?page=%202', '?page=2%20', '?page=%EF%BC%92', '?page=%D9%A3', '?page=2.0',
                '?page=', '?page=99999999999999999999')
        for query in ('', '?page=1', '?page=2', '?page=3', '?page=9', '?page=0', '?page=-4', '?page=x', '?page=1e9',
                      *junk):
            page = await self.get('/settings/audit' + query, cookies=self.cookies)
            self.assertEqual(page.status, 200, query)
            counts[query] = len(table_rows(page.body_text)) - 1
        self.assertEqual((counts[''], counts['?page=1'], counts['?page=2'], counts['?page=3']), (50, 50, 50, 21))
        self.assertEqual((counts['?page=0'], counts['?page=-4'], counts['?page=x'], counts['?page=1e9']),
                         (50, 50, 50, 50))
        self.assertEqual(counts['?page=9'], 0)
        for query in junk:  # never a 500: anything but plain ASCII digits means page 1
            self.assertEqual(counts[query], 50, query)
        first = (await self.get('/settings/audit', cookies=self.cookies)).body_text
        self.assertIn('href="/settings/audit?page=2"', first)
        self.assertNotIn('page=0', first)
        last = (await self.get('/settings/audit?page=3', cookies=self.cookies)).body_text
        self.assertIn('href="/settings/audit?page=2"', last)
        self.assertNotIn('page=4', last)

    async def test_key_events_show_on_the_page_without_the_raw_key(self):
        raw = RAW_KEY.search((await self.issue()).body_text).group(0)
        key_id = raw.split('_')[1]
        await self.post(f'/settings/keys/{key_id}/revoke', {'csrf': await self.csrf()}, cookies=self.cookies)
        text = (await self.get('/settings/audit', cookies=self.cookies)).body_text
        self.assertIn('api_key_issued', text)
        self.assertIn('api_key_revoked', text)
        self.assertIn(key_id, text)
        self.assertNotIn(raw, text)
        self.assertNotIn(raw[15:], text)

    async def test_detail_is_escaped_text(self):
        self.record('web_control', {'project_id': '<img src=x onerror=1>', 'action': 'a', 'result': 'r'})
        text = (await self.get('/settings/audit', cookies=self.cookies)).body_text
        self.assertNotIn('<img src=x', text)
        self.assertIn('&lt;img src=x', text)


class AuditFailureTests(SettingsCase):
    async def broken_audit(self):
        def broken(*args, **kwargs):
            raise auth.HubDbError('disk full')
        return unittest.mock.patch.object(self.services.audit, 'record', broken)

    async def test_a_failed_audit_write_still_shows_the_raw_key_once_and_logs_the_failure(self):
        import io
        from contextlib import redirect_stderr
        stderr = io.StringIO()
        with redirect_stderr(stderr), await self.broken_audit():
            response = await self.issue()
        self.assertEqual(response.status, 200, response.body_text)
        raw = RAW_KEY.search(response.body_text).group(0)
        self.assertEqual(len(RAW_KEY.findall(response.body_text)), 1)
        self.services.api_keys.verify(raw, self.now[0])  # the key exists and works
        self.assertIn('api_key_issued', stderr.getvalue())
        self.assertNotIn(raw[15:], stderr.getvalue())
        self.assertIn('감사', response.body_text)  # the page says the audit row is missing

    async def test_a_failed_audit_write_on_revoke_still_revokes(self):
        import io
        from contextlib import redirect_stderr
        raw = RAW_KEY.search((await self.issue()).body_text).group(0)
        key_id = raw.split('_')[1]
        csrf = await self.csrf()
        stderr = io.StringIO()
        with redirect_stderr(stderr), await self.broken_audit():
            response = await self.post(f'/settings/keys/{key_id}/revoke', {'csrf': csrf}, cookies=self.cookies)
        self.assertEqual(response.status, 303)
        with self.assertRaises(auth.ApiKeyRefused):
            self.services.api_keys.verify(raw, self.now[0])
        self.assertIn('api_key_revoked', stderr.getvalue())


class NoRawKeyLeakTests(SettingsCase):
    async def test_the_raw_key_appears_nowhere_after_the_issue_response(self):
        response = await self.issue()
        raw = RAW_KEY.search(response.body_text).group(0)
        secret = raw[15:]
        for path in ('/settings', '/settings/keys', '/settings/audit', '/'):
            page = await self.get(path, cookies=self.cookies)
            self.assertNotIn(secret, page.body_text, path)
            self.assertNotIn(secret, ''.join(str(item) for item in page.headers.items()), path)
        for table in ('audit', 'api_keys', 'sessions', 'login_state'):
            self.assertNotIn(secret, json.dumps(self.rows(f'SELECT * FROM {table}'), default=str), table)
        for path in self.state.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), str(path))


if __name__ == '__main__':
    unittest.main()
