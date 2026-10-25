"""TI-06 smoke on a real ``wb _hub --foreground`` (seam T3): the request pages are served by the real Hub.

Password set with ``wb hub passwd``, a real initialized project registered with ``wb hub add``, then plain
HTTP over 127.0.0.1: log in, create a request, see it in the list and in ``/api/badge``. Run with
``discover -s tests/hub -p 'test_web_requests_hub.py'``.
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import re
import sys
import unittest
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.backend.project import initialize_project, resolve_project_context  # noqa: E402
from fixtures.web_sandbox import PASSWORD, WebSandbox  # noqa: E402


class Browser:
    """A tiny cookie-keeping HTTP client for one loopback Hub."""

    def __init__(self, host: str, port: int):
        self.host, self.port, self.cookies = host, port, {}

    def request(self, method: str, path: str, data: dict | None = None):
        headers = {}
        body = None
        if self.cookies:
            headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in self.cookies.items())
        if data is not None:
            body = urlencode(data)
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            headers['Origin'] = f'http://{self.host}:{self.port}'
        connection = http.client.HTTPConnection(self.host, self.port, timeout=15)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            text = response.read().decode('utf-8')
            for header in response.headers.get_all('Set-Cookie') or []:
                name, _, rest = header.partition('=')
                value = rest.split(';')[0]
                if value:
                    self.cookies[name] = value
                else:
                    self.cookies.pop(name, None)
            return response.status, response.headers, text
        finally:
            connection.close()


class RealHubRequestPages(unittest.TestCase):
    def test_login_create_list_and_badge(self):
        box = WebSandbox(self)
        box.write_config()
        box.set_password()
        root = box.base / 'work' / 'alpha'
        root.mkdir(parents=True)
        root = root.resolve()
        initialize_project(resolve_project_context(root, box.env), confirm=lambda _root: True, interactive=True,
                           environment=box.env)
        project_id = json.loads((root / '.wb' / 'project.json').read_text())['project_id']
        hub = box.start_hub()
        self.assertEqual(box.cli('hub', 'add', str(root)).returncode, 0)
        host, port = box.wait_listening(hub.pid)
        self.assertEqual(host, '127.0.0.1')
        browser = Browser(host, port)

        status, _, page = browser.request('GET', '/login')
        self.assertEqual(status, 200)
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        status, headers, _ = browser.request('POST', '/login', {'login_token': token, 'password': PASSWORD})
        self.assertEqual(status, 303, 'login')

        status, _, page = browser.request('GET', '/requests/new')
        self.assertEqual(status, 200, 'the Hub serves the create page')
        self.assertIn(f'value="{project_id}"', page)
        fields = {name: re.search(rf'name="{name}" value="([^"]+)"', page).group(1) for name in ('csrf', 'nonce')}
        status, headers, _ = browser.request('POST', '/requests', {
            **fields, 'project': project_id, 'title': 'Smoke request', 'body': 'created over real HTTP'})
        self.assertEqual(status, 303)
        self.assertRegex(headers['Location'], r'^/requests/[0-9a-f-]{36}$')

        status, _, page = browser.request('GET', headers['Location'])
        self.assertEqual(status, 200)
        self.assertIn('created over real HTTP', page)
        self.assertIn('소유자', page)

        status, _, page = browser.request('GET', '/requests')
        self.assertEqual(status, 200)
        self.assertIn('Smoke request', page)
        self.assertIn('alpha', page)

        status, headers, text = browser.request('GET', '/api/badge')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {'pending': 1, 'needs_info': 0})
        status, _, _ = Browser(host, port).request('GET', '/api/badge')
        self.assertEqual(status, 401)
        self.assertEqual(box.stop_hub(hub), 0)


if __name__ == '__main__':
    unittest.main()
