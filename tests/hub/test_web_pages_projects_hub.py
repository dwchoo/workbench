"""TI-12 smoke on a real ``wb _hub --foreground`` (seam T3): the status board and project pages are served by the
real Hub over plain loopback HTTP.

Password set with ``wb hub passwd``, a real initialized project registered with ``wb hub add`` (backend off, so
nothing but the Hub runs), then: the board row, the project page, a power-on confirm page, the review mode form and
the manual "검토 시작" button. Run with ``discover -s tests/hub -p 'test_web_pages_projects*.py'``.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.backend.project import initialize_project, resolve_project_context  # noqa: E402
from fixtures.web_sandbox import PASSWORD, WebSandbox  # noqa: E402
from test_web_requests_hub import Browser  # noqa: E402


class RealHubStatusPages(unittest.TestCase):
    def test_board_project_page_confirm_page_review_mode_and_manual_review_button(self):
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
        browser = Browser(host, port)

        status, headers, _ = browser.request('GET', '/')
        self.assertEqual((status, headers['Location']), (303, '/login'))
        status, _, page = browser.request('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        status, _, _ = browser.request('POST', '/login', {'login_token': token, 'password': PASSWORD})
        self.assertEqual(status, 303)

        status, _, board = browser.request('GET', '/dashboard')
        self.assertEqual(status, 200)
        self.assertNotIn('http-equiv="refresh"', board)  # WBX-C2: the dashboard redraws through /api/dashboard
        self.assertIn('src="/static/dashboard.js"', board)
        self.assertIn('alpha', board)
        self.assertIn(f'href="/projects/{project_id}"', board)
        self.assertIn('꺼짐', board)  # a registered project whose backend is not running
        status, headers, body = browser.request('GET', '/api/dashboard')  # WBX-C2: the dashboard's session JSON
        self.assertEqual((status, headers['Content-Type'].split(';')[0], headers['Cache-Control']), (200, 'application/json', 'no-store'))
        dashboard = json.loads(body)
        self.assertEqual([(item['name'], item['power_kind']) for item in dashboard['projects']], [('alpha', 'off')])
        self.assertEqual(dashboard['summary']['projects']['closed'], 1)
        self.assertIn('자동', board)

        status, _, page = browser.request('GET', f'/projects/{project_id}')
        self.assertEqual(status, 200)
        self.assertIn(f'href="/projects/{project_id}/power-on"', page)
        self.assertNotIn('/power-off"', page)

        status, _, page = browser.request('GET', f'/projects/{project_id}/power-on')
        self.assertEqual(status, 200)
        self.assertIn(f'action="/projects/{project_id}/power-on"', page)
        self.assertEqual(box.status()['registry']['count'], 1)  # opening the confirm page started nothing
        self.assertFalse((root / '.wb' / 'backend.json').exists())

        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        status, headers, _ = browser.request('POST', f'/projects/{project_id}/review-mode',
                                             {'csrf': csrf, 'mode': 'manual'})
        self.assertEqual((status, headers['Location']), (303, f'/projects/{project_id}?notice=review_mode'))
        status, _, page = browser.request('GET', headers['Location'])
        self.assertIn('검토 방식을 바꿨습니다', page)
        self.assertRegex(page, r'<option value="manual" selected>')
        status, _, board = browser.request('GET', '/dashboard')
        self.assertIn('수동', board)

        status, _, page = browser.request('GET', '/requests/new')
        fields = {name: re.search(rf'name="{name}" value="([^"]+)"', page).group(1) for name in ('csrf', 'nonce')}
        status, headers, _ = browser.request('POST', '/requests', {
            **fields, 'project': project_id, 'title': 'Manual review', 'body': 'wait for the owner'})
        self.assertEqual(status, 303)
        detail = headers['Location']
        status, _, page = browser.request('GET', detail)
        self.assertIn(f'action="{detail}/start-review"', page)
        status, headers, _ = browser.request('POST', f'{detail}/start-review', {'csrf': fields['csrf']})
        self.assertEqual((status, headers['Location']), (303, f'{detail}?notice=review_started'))

        self.assertEqual(box.stop_hub(hub), 0)


if __name__ == '__main__':
    unittest.main()
