"""WBX-B3 ``/skill/`` on a real Hub (seam S3 subset): ``wb _hub --foreground`` in the shared web sandbox.

Covers what the in-process app cannot: ``hub.toml [web] skill_repo_url`` reaching the served README (also after
``wb hub reload``), ``[web] enabled = false`` leaving no ``/skill/`` (nothing listens), an invalid value being
``config_invalid`` and the Hub's internal Board listener not serving ``/skill/``. Expected values: SPEC
"`/skill/` 제공, packaging, `skill_repo_url`". Run with ``discover -s tests/hub -p 'test_web_skill*.py'``.
"""
from __future__ import annotations

import asyncio
import http.client
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from workbench.hub import auth, mcp, server  # noqa: E402
from workbench.hub.paths import HubLayout  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices  # noqa: E402
from fixtures.web_sandbox import PASSWORD, WebSandbox, tcp_listening  # noqa: E402

REPO_LINE = 'Source repository: https://github.com/example/workbench-skills'


def fetch(port: int, path: str = '/skill/README.md') -> tuple[int, str]:
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        return response.status, response.read().decode('utf-8')
    finally:
        connection.close()


class RealHubTests(unittest.TestCase):
    def setUp(self):
        self.box = WebSandbox(self)

    def start(self, *lines: str, enabled: bool = True):
        self.box.write_config('127.0.0.1', 0, *lines, enabled=enabled)
        self.box.set_password()
        self.hub = self.box.start_hub()

    def test_skill_repo_url_from_hub_toml_shows_in_the_readme_and_follows_a_reload(self):
        self.start('skill_repo_url = "https://github.com/example/workbench-skills"')
        _host, port = self.box.wait_listening(self.hub.pid)
        status, text = fetch(port)
        self.assertEqual(status, 200)
        self.assertEqual(text.splitlines()[1], REPO_LINE)
        self.assertIn(f'WB_HUB_URL=http://127.0.0.1:{port}', text)
        self.box.write_config('127.0.0.1', port, 'skill_repo_url = "https://example.org/other"')
        result = self.box.cli('hub', 'reload')
        self.assertEqual(result.returncode, 0, result)
        status, text = fetch(port)
        self.assertEqual(status, 200)
        self.assertEqual(text.splitlines()[1], 'Source repository: https://example.org/other')

    def test_without_the_key_the_readme_has_no_repository_line(self):
        self.start()
        _host, port = self.box.wait_listening(self.hub.pid)
        text = fetch(port)[1]
        self.assertEqual(text.splitlines()[1], '')
        self.assertNotIn('Source repository', text)

    def test_web_disabled_serves_no_skill_files_at_all(self):
        self.start('skill_repo_url = "https://github.com/example/workbench-skills"', enabled=False)
        time.sleep(2.5)  # past one watchdog poll
        self.assertEqual(tcp_listening(self.hub.pid), [])
        self.assertEqual(self.box.status()['web']['state'], 'disabled')

    def test_an_invalid_skill_repo_url_is_config_invalid_and_opens_nothing(self):
        self.start('skill_repo_url = "ftp://example.com/x"')
        time.sleep(2.5)
        self.assertEqual(tcp_listening(self.hub.pid), [])
        web = self.box.status()['web']
        self.assertEqual((web['state'], web['detail']), ('config_invalid', 'web.skill_repo_url'))


class InternalListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_internal_board_listener_has_no_skill_route(self):
        base = Path(tempfile.mkdtemp(prefix='wb-skill-int-')).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        state = base / 'state'
        state.mkdir(mode=0o700)
        passwords = auth.PasswordStore(state)
        passwords.set_password(PASSWORD)
        db = auth.HubDb.open(state)
        self.addCleanup(db.close)
        services = WebServices(passwords, db, OriginPolicy('127.0.0.1'))
        hub = server.Hub(HubLayout(base / 'hub'), 'detached')
        self.addCleanup(hub.inbox.close)
        listener = mcp.InternalListener(services.api_keys, hub.mcp)
        await listener.start()
        self.addAsyncCleanup(listener.stop)
        for path in ('/skill/README.md', '/skill/wb-inbox/SKILL.md', '/skill/'):
            with self.subTest(path=path):
                status, body = await asyncio.get_running_loop().run_in_executor(None, fetch, listener.port, path)
                self.assertEqual(status, 404)
                self.assertNotIn('Workbench', body)


if __name__ == '__main__':
    unittest.main()
