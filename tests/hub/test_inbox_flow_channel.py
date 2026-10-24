"""Hub ↔ backend channel against a real backend (TI-09, seam T6 + T3b).

Real backends with fake OMPs (``tests/recovery_boot/fake_omp.py``: no model, no provider, no network) in the
``ProjectSandbox`` of ``test_project_control.py`` (temp HOME/XDG, a short private ``XDG_RUNTIME_DIR``; the OMP
ephemeral namespace under ``/tmp/omp-workbench-<uid>`` is this sandbox's own fresh path and is removed in its cleanup).
Expected values come from ``backend.json``, ``/proc``, ``tasks.sqlite3`` read with stdlib ``sqlite3``, the fake OMPs'
records (``$FAKE_FRAMES``/``$FAKE_NOTICES``), ``hub.log`` and the SPEC "Hub ↔ backend 요청 통로" text. Run with
``discover -s tests/hub``.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import time
import unittest
import uuid

from test_project_control import REPO, ProjectSandbox, wait_until

sys.path.insert(0, str(REPO / 'src'))

from workbench.backend.client import UiClient  # noqa: E402
from workbench.contracts.ui_v1 import ClientType  # noqa: E402

RUN_KEYS = {'run_id', 'status', 'exit_code', 'started_at', 'ended_at', 'stopped_by', 'log_sources'}
TASK_KEYS = {'task_id', 'exists', 'status', 'closed_reason', 'revision', 'cancel_requested_by', 'runs', 'commands'}


class ChannelCase(unittest.TestCase):
    def setUp(self):
        self.box = ProjectSandbox(self)

    def hub_client(self, root: Path) -> UiClient:
        pid, ticks, record = self.box.backend_ref(root)
        client = UiClient(Path(record['ui_socket']), name='workbench-hub', timeout=10,
                          expected_process={'pid': pid, 'start_ticks': ticks})
        self.addCleanup(client.close)
        return client

    def records(self, name: str) -> list[dict]:
        path = Path(self.box.env[name])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def manager_tool(self, ui: UiClient, tool: str, args: dict) -> dict:
        """One manager tool call as the fake manager OMP sends it on the real bridge; its tool result."""
        before = len(self.tool_results())
        answer = ui.request(ClientType.INPUT, f'tool {tool} {json.dumps(args)}\n'.encode(), pane='manager_omp')
        self.assertTrue(answer.get('ok'), answer)
        self.assertTrue(wait_until(lambda: len(self.tool_results()) > before, 30), 'no tool result')
        return self.tool_results()[before]['result']

    def tool_results(self) -> list[dict]:
        return [item['frame'] for item in self.records('FAKE_FRAMES')
                if item['role'] == 'manager' and item['frame'].get('kind') == 'tool_result']


class TaskStatusTests(ChannelCase):
    def test_task_status_reports_a_real_task_and_unknown_ids_without_model_or_bridge_traffic(self):
        root, project_id = self.box.project('alpha')
        self.box.power_on(root, project_id)
        ui = self.box.attach(root)
        made = self.manager_tool(ui, 'to_worker', {'kind': 'work', 'message': 'collect the facts TI09-MARK',
                                                   'spec': {'goal': 'facts', 'paths': []}})
        task_id = made.get('task_id')
        self.assertTrue(task_id, made)
        snapshot = self.box.status(root)
        self.assertEqual(snapshot['task']['task_id'], task_id, snapshot['task'])
        with sqlite3.connect(f'file:{root / ".wb" / "tasks.sqlite3"}?mode=ro', uri=True) as db:
            runs = sorted(row[0] for row in db.execute('SELECT run_id FROM runs WHERE task_id = ?', (task_id,)))
        unknown = str(uuid.uuid4())
        quiet = lambda: [item for item in self.records('FAKE_FRAMES') if item['frame'].get('kind') != 'deliver']
        frames, notices = len(quiet()), len(self.records('FAKE_NOTICES'))
        hub = self.hub_client(root)
        answer = hub.request(ClientType.TASK_STATUS, task_ids=[task_id, unknown], detail=False)
        self.assertTrue(answer['ok'], answer)
        known, missing = answer['tasks']
        self.assertEqual(missing, {'task_id': unknown, 'exists': False})
        self.assertEqual(set(known), TASK_KEYS, known)
        self.assertEqual((known['task_id'], known['exists'], known['status'], known['revision'],
                          known['closed_reason'], known['cancel_requested_by']),
                         (task_id, True, snapshot['task']['status'], snapshot['task']['revision'], None, None))
        self.assertEqual(sorted(run['run_id'] for run in known['runs']), runs)
        for run in known['runs']:
            self.assertEqual(set(run), RUN_KEYS, run)
        self.assertIsInstance(known['commands'], list)
        detailed = hub.request(ClientType.TASK_STATUS, task_ids=[task_id], detail=True)
        self.assertTrue(detailed['ok'], detailed)
        self.assertEqual(set(detailed['tasks'][0]), TASK_KEYS | {'reports'})
        self.assertIsInstance(detailed['tasks'][0]['reports'], list)
        time.sleep(1.0)
        self.assertEqual((len(quiet()), len(self.records('FAKE_NOTICES'))), (frames, notices),
                         'task_status sends nothing to an OMP')
        # an attached UI may not ask; the Hub's other two requests are not implemented by this backend yet
        refused = ui.request(ClientType.TASK_STATUS, task_ids=[task_id])
        self.assertEqual((refused['ok'], refused.get('reason')), (False, 'not_attached'), refused)
        for kind, fields in ((ClientType.INBOX_NOTICE, {
                'request_id': str(uuid.uuid4()), 'delivery_id': str(uuid.uuid4()), 'event': 'new',
                'fields': {'request_id': None, 'seq': 1, 'title': 't', 'origin': {}, 'state': 'NEW', 'excerpt': '',
                           'hint': None}}),
                             (ClientType.CANCEL_TASK, {'task_id': task_id, 'request_id': str(uuid.uuid4()),
                                                       'expect': {'status': 'dispatched', 'command_id': None},
                                                       'reason': None})):
            if kind is ClientType.INBOX_NOTICE:
                fields['fields']['request_id'] = fields['request_id']
            answer = hub.request(kind, **fields)
            self.assertEqual((answer['ok'], answer.get('reason')), (False, 'unsupported_type'), answer)


if __name__ == '__main__':
    unittest.main()
