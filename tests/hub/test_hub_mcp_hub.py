"""TI-05 smoke on a real ``wb _hub --foreground`` (seam T4): the Hub itself serves ``/mcp``.

Password set with ``wb hub passwd``, a real initialized project registered with ``wb hub add``, an API key
issued straight into the Hub's ``hub.sqlite3`` through ``ApiKeyStore`` (the issue page is the owner's way),
then the stdlib MCP client over 127.0.0.1: initialize → initialized → tools/list → ``inbox_projects`` →
``inbox_submit`` → ``inbox_get``. Run with ``discover -s tests/hub -p 'test_hub_mcp*.py'``.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.backend.project import initialize_project, resolve_project_context  # noqa: E402
from workbench.hub import auth  # noqa: E402
from fixtures.web_sandbox import WebSandbox  # noqa: E402
from support.mcp_client import McpClient  # noqa: E402


class RealHubMcp(unittest.TestCase):
    def test_handshake_projects_submit_get(self):
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

        db = auth.HubDb.open(box.state)
        try:
            key = auth.ApiKeyStore(db).issue('smoke', 'requester', {'projects': [project_id]}, None,
                                             now=time.time()).raw
        finally:
            db.close()

        client = McpClient(host, port, key)
        self.assertEqual(client.initialize()['serverInfo']['name'], 'workbench-hub')
        self.assertIsNotNone(client.session_id)
        self.assertEqual(client.initialized().status, 202)
        self.assertIn('inbox_submit', {tool['name'] for tool in client.list_tools()})
        self.assertEqual(client.tool('inbox_projects'), {'projects': [{'project_id': project_id, 'name': 'alpha'}]})
        submitted = client.tool('inbox_submit', project_id=project_id, title='From MCP', body='Smoke body',
                                idempotency_key='smoke-1')
        self.assertEqual((submitted['state'], submitted['created']), ('NEW', True))
        request = client.tool('inbox_get', request_id=submitted['request_id'])['request']
        self.assertEqual((request['title'], request['body'], request['origin']['label']),
                         ('From MCP', 'Smoke body', 'key:smoke'))
        self.assertEqual(McpClient(host, port, None).request('ping').status, 401)

        self.assertEqual(box.stop_hub(hub), 0)
        connection = sqlite3.connect(f'file:{root / ".wb" / "inbox.sqlite3"}?mode=ro', uri=True)
        try:
            rows = connection.execute('SELECT request_id, origin_name FROM requests').fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [(submitted['request_id'], 'smoke')])


if __name__ == '__main__':
    unittest.main()
