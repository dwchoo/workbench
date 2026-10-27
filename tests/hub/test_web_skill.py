"""WBX-B3 ``/skill/`` serving (seam S1): the real Hub's web app in-process over ``TestServer``.

``create_app(services, routes=Hub(...).web_routes())`` with real ``auth.json`` / ``hub.sqlite3`` in a temp dir. Expected
values are derived from the web-board-ux SPEC "`/skill/` 제공, packaging, `skill_repo_url`" and the repo ``skills/`` files
read straight from disk (never through the product's asset resolver). Run with ``discover -s tests/hub -p 'test_web_skill*.py'``.
"""
from __future__ import annotations

import ipaddress
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from aiohttp import ClientSession, DummyCookieJar, TCPConnector, test_utils  # noqa: E402
from yarl import URL  # noqa: E402

from workbench.hub import auth, server  # noqa: E402
from workbench.hub.paths import HubLayout  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402

PASSWORD = 'correct horse battery'
PLACEHOLDER = 'http://YOUR-HUB-HOST:8780'
MARKER = '<!-- wb:skill-repo -->'
SKILLS = REPO / 'skills'
# The fixed list of served paths (SPEC "Route") -> the repo file behind each.
SERVED = {'/skill/README.md': SKILLS / 'README.md',
          '/skill/wb-inbox/SKILL.md': SKILLS / 'wb-inbox' / 'SKILL.md',
          '/skill/wb-inbox/wb_inbox.py': SKILLS / 'wb-inbox' / 'wb_inbox.py',
          '/skill/wb-inbox/.env.example': SKILLS / 'wb-inbox' / '.env.example'}


class SkillCase(unittest.IsolatedAsyncioTestCase):
    async def serve(self, *, tls: bool = False, server_names: tuple[str, ...] = (), allow_from: tuple = (),
                    skill_repo_url: str = '', bind: str = '127.0.0.1', source: str | None = None):
        self.base_dir = Path(tempfile.mkdtemp(prefix='wb-skill-')).resolve()
        self.addCleanup(shutil.rmtree, self.base_dir, True)
        state = self.base_dir / 'state'
        state.mkdir(mode=0o700)
        self.passwords = auth.PasswordStore(state)
        self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(state)
        self.addCleanup(self.db.close)
        self.services = WebServices(self.passwords, self.db,
                                    OriginPolicy(bind, tls, server_names, allow_from=allow_from,
                                                 skill_repo_url=skill_repo_url))
        self.hub = server.Hub(HubLayout(self.base_dir / 'hub'), 'detached')
        self.addCleanup(self.hub.inbox.close)
        self.app = create_app(self.services, routes=self.hub.web_routes())
        self.server = test_utils.TestServer(self.app, host=bind, port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        connector = TCPConnector(local_addr=(source, 0)) if source else None
        self.http = ClientSession(cookie_jar=DummyCookieJar(), connector=connector)
        self.addAsyncCleanup(self.http.close)
        self.port = self.server.port
        self.host = f'{bind}:{self.port}'

    async def get(self, path: str, *, host: str | None = None, method: str = 'GET', headers: dict | None = None):
        sent = {'Host': host or self.host, **(headers or {})}
        # encoded=True: the path goes on the wire as written (no client-side dot-segment or %-normalisation).
        async with self.http.request(method, URL(f'http://{self.host}{path}', encoded=True), headers=sent,
                                     allow_redirects=False) as response:
            response.body = await response.read()
            return response


class ReadmeTests(SkillCase):
    async def test_the_readme_is_served_without_login_with_the_hub_address_filled_in(self):
        await self.serve()
        response = await self.get('/skill/README.md')
        self.assertEqual(response.status, 200)
        original = (SKILLS / 'README.md').read_bytes()
        self.assertIn(PLACEHOLDER.encode(), original)
        origin = f'http://{self.host}'
        # Only the address and the marker line differ from the installed file.
        expected = original.replace(PLACEHOLDER.encode(), origin.encode()).replace(MARKER.encode(), b'')
        self.assertEqual(response.body, expected)
        self.assertNotIn(b'YOUR-HUB-HOST', response.body)


class AllFilesTests(SkillCase):
    async def test_each_file_is_the_installed_bytes_with_only_the_address_changed(self):
        await self.serve()
        origin = f'http://{self.host}'
        for path, source in SERVED.items():
            with self.subTest(path=path):
                response = await self.get(path)
                self.assertEqual(response.status, 200)
                original = source.read_bytes()
                if path.endswith('.py'):
                    self.assertEqual(response.body, original, 'wb_inbox.py is code: no substitution')
                    continue
                self.assertIn(PLACEHOLDER.encode(), original)
                expected = original.replace(PLACEHOLDER.encode(), origin.encode()).replace(MARKER.encode(), b'')
                self.assertEqual(response.body, expected)
                self.assertIn(origin.encode(), response.body)

    async def test_head_answers_like_get_without_a_body(self):
        await self.serve()
        for path in SERVED:
            with self.subTest(path=path):
                got, head = await self.get(path), await self.get(path, method='HEAD')
                self.assertEqual(head.status, 200)
                self.assertEqual(head.body, b'')
                self.assertEqual(head.headers['Content-Length'], got.headers['Content-Length'])

    async def test_the_address_follows_the_request_host_and_the_listener_scheme(self):
        await self.serve(server_names=('box.vpn',))
        for host in (f'127.0.0.1:{self.port}', f'localhost:{self.port}', f'box.vpn:{self.port}'):
            with self.subTest(host=host):
                body = (await self.get('/skill/wb-inbox/.env.example', host=host)).body
                self.assertIn(f'WB_HUB_URL=http://{host}'.encode(), body)
                self.assertNotIn(b'YOUR-HUB-HOST', body)

    async def test_a_tls_listener_fills_in_https(self):
        await self.serve(tls=True)
        body = (await self.get('/skill/README.md')).body
        self.assertIn(f'WB_HUB_URL=https://{self.host}'.encode(), body)
        self.assertNotIn(b'http://127.0.0.1', body)

    async def test_no_response_holds_an_issued_key_or_sets_a_cookie(self):
        await self.serve()
        issued = self.services.api_keys.issue('skill-check', 'requester', {'all': True}, None, now=1.0)
        for path in SERVED:
            response = await self.get(path)
            self.assertEqual(response.headers.getall('Set-Cookie', []), [])
            self.assertNotIn(issued.raw.encode(), response.body)
            self.assertNotIn(issued.key_id.encode(), response.body)

class UnlistedPathTests(SkillCase):
    PATHS = ('/skill', '/skill/', '/skill/.env', '/skill/wb-inbox/.env', '/skill/wb-inbox/', '/skill/wb-inbox',
             '/skill/README.md/', '/skill/readme.md', '/skill/wb-inbox/wb_inbox.pyc', '/skill/wb-inbox/../README.md',
             '/skill/%2e%2e/README.md', '/skill/wb-inbox/%2e%2e/%2e%2e/pyproject.toml', '/skill//README.md',
             '/skill/wb-inbox/SKILL.md/extra', '/skill/other-skill/SKILL.md', '/skill/..%2fREADME.md',
             '/skill/wb-inbox/.env.example.bak', '/skill/%00', '/skill/wb-inbox/%2eenv', '/skill/wb-inbox/.ENV')

    async def test_anything_but_the_four_files_is_404_without_login_and_never_a_redirect(self):
        await self.serve()
        for path in self.PATHS:
            with self.subTest(path=path):
                response = await self.get(path)
                self.assertEqual(response.status, 404)
                self.assertNotIn('Location', response.headers)
                self.assertEqual(response.headers.getall('Set-Cookie', []), [])
                self.assertEqual(response.body, b'not_found')

    async def test_a_real_env_file_next_to_the_skill_is_never_served(self):
        await self.serve()
        root = self.base_dir / 'assets'  # a copy of the installed layout that also holds a developer's .env
        shutil.copytree(SKILLS, root / 'skills')
        (root / 'skills' / 'wb-inbox' / '.env').write_text('WB_API_KEY=wbk_secret_for_test\n')
        (root / 'skills' / '.env').write_text('WB_API_KEY=wbk_secret_for_test\n')
        with mock.patch('workbench.assets.runtime_root', return_value=root):
            served = await self.get('/skill/wb-inbox/SKILL.md')
            self.assertEqual(served.status, 200, 'the patched root is the one in use')
            for path in ('/skill/wb-inbox/.env', '/skill/.env'):
                response = await self.get(path)
                self.assertEqual(response.status, 404)
                self.assertNotIn(b'secret_for_test', response.body)

class HeaderTests(SkillCase):
    async def test_files_are_inline_plain_text_that_is_revalidated_every_time(self):
        await self.serve()
        for path in SERVED:
            for method in ('GET', 'HEAD'):
                with self.subTest(path=path, method=method):
                    response = await self.get(path, method=method)
                    self.assertEqual(response.headers['Content-Type'], 'text/plain; charset=utf-8')
                    self.assertEqual(response.headers['Content-Disposition'], 'inline')
                    self.assertEqual(response.headers['Cache-Control'], 'no-cache')
                    self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
                    self.assertIn("default-src 'self'", response.headers['Content-Security-Policy'])

    async def test_a_404_under_skill_is_revalidated_too(self):
        await self.serve()
        response = await self.get('/skill/.env')
        self.assertEqual(response.headers['Cache-Control'], 'no-cache')


class RepositoryLineTests(SkillCase):
    async def test_the_marker_line_becomes_the_source_repository_line_when_configured(self):
        await self.serve(skill_repo_url='https://github.com/example/workbench-skills')
        text = (await self.get('/skill/README.md')).body.decode()
        lines = text.splitlines()
        self.assertEqual(lines[0], (SKILLS / 'README.md').read_text().splitlines()[0])
        self.assertEqual(lines[1], 'Source repository: https://github.com/example/workbench-skills')
        self.assertNotIn('wb:skill-repo', text)
        # the other documents have no such line and are not affected
        for path in ('/skill/wb-inbox/SKILL.md', '/skill/wb-inbox/.env.example', '/skill/wb-inbox/wb_inbox.py'):
            self.assertNotIn(b'example/workbench-skills', (await self.get(path)).body)

    async def test_the_marker_line_is_empty_when_not_configured(self):
        await self.serve()
        lines = (await self.get('/skill/README.md')).body.decode().splitlines()
        self.assertEqual(lines[1], '')
        self.assertNotIn('Source repository', '\n'.join(lines))


def nets(*texts: str) -> tuple:
    return tuple(ipaddress.ip_network(text) for text in texts)


class AllowFromTests(SkillCase):
    """AC-11 (skill part): ``allow_from`` is the web's rule, applied before the skill route like on every route."""

    async def test_a_client_outside_allow_from_gets_a_bare_403_on_every_skill_path(self):
        await self.serve(allow_from=nets('127.0.0.1'), source='127.0.0.2')
        for path in (*SERVED, '/skill/.env', '/skill/', '/skill'):
            with self.subTest(path=path):
                response = await self.get(path)
                self.assertEqual(response.status, 403)
                self.assertEqual(response.body, b'')
                self.assertEqual(response.headers.getall('Set-Cookie', []), [])
                self.assertNotEqual(response.headers['Cache-Control'], 'no-cache')

    async def test_a_listed_client_is_served(self):
        await self.serve(allow_from=nets('127.0.0.2'), source='127.0.0.2')
        for path in SERVED:
            with self.subTest(path=path):
                self.assertEqual((await self.get(path)).status, 200)

    async def test_a_host_the_listener_does_not_accept_gets_the_web_421_not_the_file(self):
        await self.serve()
        response = await self.get('/skill/README.md', host='evil.example')
        self.assertEqual(response.status, 421)
        self.assertNotIn(b'Workbench', response.body)


if __name__ == '__main__':
    unittest.main()
