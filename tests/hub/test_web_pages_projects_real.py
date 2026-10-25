"""TI-12 power-off confirm flow on a real backend (seam T6): the real ``create_app`` with ``projects.routes`` over
a real ``Lister``, a real ``ProjectControl`` and a real fake-OMP backend (``ProjectSandbox`` of
``test_project_control``: temp HOME/XDG, a short private ``XDG_RUNTIME_DIR``, a placeholder ``agent.db``,
``WORKBENCH_HUB_AUTOSTART=0``). Expected values come from ``/proc`` (pid, start ticks) and the page texts of the
ticket. Run with ``discover -s tests/hub -p 'test_web_pages_projects*.py'``.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

from aiohttp import test_utils

import test_web_control as web_control_tests
from test_project_control import alive, wait_until

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.hub.inbox.service import InboxService  # noqa: E402
from workbench.hub.paths import hub_layout  # noqa: E402
from workbench.hub.status import Lister  # noqa: E402
from workbench.hub.web.app import create_app  # noqa: E402
from workbench.hub.web.pages import control as control_pages  # noqa: E402
from workbench.hub.web.pages import projects as project_pages  # noqa: E402


class RealPowerOffFlow(web_control_tests.WebControlCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.server.close()  # rebuild the app with the pages over a real Lister (its shutdown closed the control)
        self.control = self.web_control.ProjectControl(environment=self.box.env, audit=self.services.audit)
        self.lister = Lister(hub_layout(self.box.env))
        self.addCleanup(self.lister.close)
        self.inbox = InboxService()
        self.addCleanup(self.inbox.close)
        self.server = test_utils.TestServer(
            create_app(self.services, routes=[control_pages.use(self.control),
                                              project_pages.routes(self.lister.list, self.inbox)]),
            host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.base = f'http://127.0.0.1:{self.server.port}'
        self.host = f'127.0.0.1:{self.server.port}'

    async def running(self, name: str = 'alpha'):
        root, project_id = self.registered(name)
        pid, ticks = self.box.power_on(root, project_id)
        self.box.attach_client = self.box.attach(root)
        return root, project_id, pid, ticks

    async def fetch(self, path: str, session: tuple[str, str]) -> tuple[int, str]:
        async with self.http.get(self.base + path, headers={'Host': self.host, 'Cookie': f'wb_hub_session={session[0]}'},
                                 allow_redirects=False) as response:
            return response.status, await response.text()

    async def submit(self, path: str, session: tuple[str, str], data: dict):
        headers = {'Host': self.host, 'Origin': 'http://' + self.host, 'Cookie': f'wb_hub_session={session[0]}'}
        async with self.http.post(self.base + path, data={'csrf': session[1], **data}, headers=headers,
                                  allow_redirects=False) as response:
            return response.status, response.headers.get('Location'), await response.text()

    async def test_cancel_changes_nothing_and_a_changed_work_list_is_asked_again_before_the_backend_stops(self):
        root, project_id, pid, ticks = await self.running()
        session = await self.login()
        status, board = await self.fetch('/', session)
        self.assertEqual(status, 200)
        self.assertIn('켜짐', board)
        status, page = await self.fetch(f'/projects/{project_id}/power-off', session)
        self.assertEqual(status, 200, page)
        first_token = re.search(r'name="token" value="([^"]+)"', page).group(1)
        panes = self.box.pane_processes(self.box.status(root))

        status, project = await self.fetch(f'/projects/{project_id}', session)  # what the cancel link opens
        self.assertEqual(status, 200)
        self.assertTrue(alive(pid, ticks))
        self.assertEqual(self.box.pane_processes(self.box.status(root)), panes)

        sleeper = self.box.sleeper(root, 301)  # work starts after the page was shown
        status, location, page = await self.submit(f'/projects/{project_id}/power-off', session, {'token': first_token})
        self.assertEqual((status, location), (200, None), page)
        self.assertIn('작업이 바뀌었습니다', page)
        self.assertIn(str(sleeper), page)  # the new list names the new work
        second_token = re.search(r'name="token" value="([^"]+)"', page).group(1)
        self.assertNotEqual(second_token, first_token)
        self.assertTrue(alive(pid, ticks), 'a changed list stops nothing')
        self.assertTrue(alive(sleeper))

        status, location, page = await self.submit(f'/projects/{project_id}/power-off', session, {'token': second_token})
        self.assertEqual((status, location), (303, f'/projects/{project_id}?notice=power_off'), page)
        self.assertTrue(wait_until(lambda: not alive(pid, ticks), 20), 'a verified shutdown ended the backend')


if __name__ == '__main__':
    unittest.main()
