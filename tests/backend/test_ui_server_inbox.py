"""TI-09 (TI-T05): ui_v1 ``inbox_notice``/``task_status``/``cancel_task`` from the Hub without attaching.

Seam T7: a real ``UiServer`` on a unix socket with a fake controller and the real ``UiClient`` (the HB-06 fixture of
``test_ui_server_hub_control.py``; another peer uid is injected by overriding ``peer_identity``). Expected values come
from the ui_v1 contract text (Task Inbox SPEC "Hub → backend"): the three requests are served only to a connection
whose hello ``client`` is ``workbench-hub`` and whose server-side peer uid is the backend euid; anything else is
``not_attached`` and the controller is not called; they never take or change the attach slot and push no state.
"""

from __future__ import annotations

import unittest

from test_ui_server_hub_control import (AutomationController, ForeignUidServer, HubControlFixture,
                                        UnreadablePeerServer)
from workbench.contracts.ui_v1 import ClientType
from workbench.contracts.v1 import PaneId

REQUEST_ID = "6f1b6a3e-2f40-4a3f-9d4e-0c1b2a3d4e5f"
DELIVERY_ID = "0b9c4f62-0d2a-4b8e-9b55-3f2c1d0e9a87"
TASK_ID = "2c5e8a1f-7b3d-4c9e-8f60-1a2b3c4d5e6f"
FIELDS = {"request_id": REQUEST_ID, "seq": 1, "title": "Collect logs", "origin": {"kind": "owner"}, "state": "NEW",
          "excerpt": "short", "hint": None}
REQUESTS = (
    (ClientType.INBOX_NOTICE, {"request_id": REQUEST_ID, "delivery_id": DELIVERY_ID, "event": "new",
                               "fields": FIELDS}),
    (ClientType.TASK_STATUS, {"task_ids": [TASK_ID], "detail": False}),
    (ClientType.CANCEL_TASK, {"task_id": TASK_ID, "request_id": REQUEST_ID,
                              "expect": {"status": "running", "command_id": None}, "reason": "owner"}),
)


class InboxController(AutomationController):
    """Records the three Hub requests; each answers like the contract's success shape."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, tuple]] = []

    def inbox_notice(self, request_id, delivery_id, event, fields):
        self.calls.append(("inbox_notice", (request_id, delivery_id, event, fields)))
        return {"queued": True, "notice_id": "n-1"}

    def task_status(self, task_ids, detail):
        self.calls.append(("task_status", (task_ids, detail)))
        return {"tasks": [{"task_id": task_id, "exists": False} for task_id in task_ids]}

    def cancel_task(self, task_id, request_id, expect, reason):
        self.calls.append(("cancel_task", (task_id, request_id, expect, reason)))
        return {"cancel": "cancel_requested", "stop": None}


class InboxFixture(HubControlFixture):
    def setUp(self):
        super().setUp()
        self.controller = InboxController()
        self.server.controller = self.controller


class NonAttachedHubRequestTests(InboxFixture):
    def test_the_hub_gets_each_request_without_attaching_and_the_controller_runs_once(self):
        hub = self.client()
        expected = {
            ClientType.INBOX_NOTICE: ({"queued": True, "notice_id": "n-1"},
                                      (REQUEST_ID, DELIVERY_ID, "new", FIELDS)),
            ClientType.TASK_STATUS: ({"tasks": [{"task_id": TASK_ID, "exists": False}]}, ([TASK_ID], False)),
            ClientType.CANCEL_TASK: ({"cancel": "cancel_requested", "stop": None},
                                     (TASK_ID, REQUEST_ID, {"status": "running", "command_id": None}, "owner")),
        }
        for kind, fields in REQUESTS:
            with self.subTest(kind=kind.value):
                result = hub.request(kind, **fields)
                answer, arguments = expected[kind]
                self.assertTrue(result["ok"], result)
                self.assertEqual({key: result[key] for key in answer}, answer)
                self.assertEqual(self.controller.calls[-1], (kind.value, arguments))
        self.assertEqual(len(self.controller.calls), 3)
        self.assertIsNone(self.server.attached)
        self.assertEqual(self.controller.attaches, [])

    def test_other_client_names_are_not_attached_and_the_controller_is_not_called(self):
        for name in ("workbench-cli", "workbench-ui", "workbench-backend", "workbench-hub ", "Workbench-Hub", ""):
            other = self.client(name=name)
            for kind, fields in REQUESTS:
                with self.subTest(name=name, kind=kind.value):
                    result = other.request(kind, **fields)
                    self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertEqual(self.controller.calls, [])

    def test_an_attached_ui_cannot_send_them_and_stays_attached(self):
        ui = self.attached_ui()
        attached = self.server.attached
        for kind, fields in REQUESTS:
            with self.subTest(kind=kind.value):
                result = ui.request(kind, **fields)
                self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertEqual(self.controller.calls, [])
        self.assertIs(self.server.attached, attached)
        self.assertTrue(ui.input(PaneId.MANAGER_OMP, b"x")["ok"])

    def test_hub_requests_leave_an_attached_ui_and_the_attach_slot_alone(self):
        ui = self.attached_ui()
        attached = self.server.attached
        ui.states.clear()
        hub = self.client()
        for kind, fields in REQUESTS:
            self.assertTrue(hub.request(kind, **fields)["ok"])
        self.assertIs(self.server.attached, attached)
        self.assertEqual((len(self.controller.attaches), self.controller.detaches), (1, 0))
        ui.pump(0.2)
        hub.pump(0.2)
        self.assertEqual((ui.states, hub.states), ([], []), "no state push for these requests")
        self.assertTrue(ui.input(PaneId.MANAGER_OMP, b"y")["ok"])
        # the Hub cannot take the slot either
        refused = hub.request(ClientType.ATTACH)
        self.assertEqual((refused["ok"], refused.get("reason")), (False, "attached_elsewhere"), refused)

    def test_invalid_fields_are_refused_before_the_controller(self):
        hub = self.client()
        for kind, fields in ((ClientType.TASK_STATUS, {"task_ids": []}),
                             (ClientType.TASK_STATUS, {"task_ids": [TASK_ID] * 2}),
                             (ClientType.INBOX_NOTICE, {**REQUESTS[0][1], "event": "started"}),
                             (ClientType.CANCEL_TASK, {**REQUESTS[2][1], "expect": {"status": "running"}})):
            with self.subTest(kind=kind.value, fields=fields):
                result = hub.request(kind, **fields)
                self.assertEqual((result["ok"], result.get("reason")), (False, "invalid_message"), result)
        self.assertEqual(self.controller.calls, [])


class ForeignPeerInboxTests(InboxFixture):
    server_class = ForeignUidServer

    def test_a_hub_name_from_another_uid_is_not_attached(self):
        hub = self.client()
        for kind, fields in REQUESTS:
            result = hub.request(kind, **fields)
            self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertEqual(self.controller.calls, [])


class UnreadablePeerInboxTests(InboxFixture):
    server_class = UnreadablePeerServer

    def test_an_unreadable_peer_is_not_attached(self):
        hub = self.client()
        for kind, fields in REQUESTS:
            result = hub.request(kind, **fields)
            self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertEqual(self.controller.calls, [])


class HubPeerFixture(InboxFixture):
    """A ``UiServer`` given a ``hub_peer`` check (the backend passes the verified running Hub's pid check)."""

    hub_pids: set = set()

    def setUp(self):
        import os
        from workbench.backend.ui_server import UiServer
        original = UiServer.__init__
        allowed = {os.getpid()} if self.hub_pids == {"self"} else set()
        self.checked: list[int] = []

        def check(pid):
            self.checked.append(pid)
            return pid in allowed

        def init(server, path, controller, **kwargs):
            original(server, path, controller, hub_peer=check, **kwargs)
        self.server_class = type("CheckedServer", (UiServer,), {"__init__": init})
        super().setUp()


class NotTheHubTests(HubPeerFixture):
    hub_pids = set()

    def test_notice_and_cancel_need_the_running_hubs_pid_while_task_status_keeps_the_name_and_uid_rule(self):
        hub = self.client()
        for kind, fields in REQUESTS:
            with self.subTest(kind=kind.value):
                result = hub.request(kind, **fields)
                if kind is ClientType.TASK_STATUS:
                    self.assertTrue(result["ok"], result)
                else:
                    self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertEqual([call[0] for call in self.controller.calls], ["task_status"])
        self.assertTrue(self.checked, "the Hub check ran")


class TheHubTests(HubPeerFixture):
    hub_pids = {"self"}

    def test_the_running_hubs_pid_gets_notice_and_cancel(self):
        hub = self.client()
        for kind, fields in REQUESTS:
            self.assertTrue(hub.request(kind, **fields)["ok"], kind.value)
        self.assertEqual([call[0] for call in self.controller.calls], ["inbox_notice", "task_status", "cancel_task"])


class TaskStoreReadTests(unittest.TestCase):
    """``service.read_task_store``: the read-only open survives a data root with URI-special characters."""

    def test_a_root_with_question_mark_hash_and_percent_is_read(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from workbench.backend.service import read_task_store
        from workbench.tasks.repository import TaskRepository
        with tempfile.TemporaryDirectory(prefix="ti09-uri-") as base:
            data = Path(base) / "we?ird#root%41" / ".wb"
            data.mkdir(parents=True)
            path = data / "tasks.sqlite3"
            TaskRepository(path).close()
            run_id = "4b1d3c2a-9e8f-4a7b-8c6d-5e4f3a2b1c0d"
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO runs (run_id, task_id, revision, settings_revision, retry_count_used, "
                           "inputs_json, created_at) VALUES (?, ?, 1, 1, 0, '{}', '2026-10-25T00:00:00Z')",
                           (run_id, TASK_ID))
            db.close()
            stored = read_task_store(path, TASK_ID, detail=False)
            self.assertEqual([run["run_id"] for run in stored["runs"]], [run_id])
            self.assertFalse((Path(base) / "we").exists(), "nothing was created at a truncated path")


class TaskStatusBudgetTests(unittest.TestCase):
    """``Backend.task_status`` runs on the UI loop thread, so it has a total time budget: what is not read in time is
    ``{task_id, timeout: true}`` and the answer says ``timeout: true`` (a partial answer, never a stall)."""

    def stub(self, base, budget, delay):
        import time
        from pathlib import Path
        from types import SimpleNamespace
        from workbench.backend.service import Backend
        from workbench.tasks.repository import TaskRepository
        path = Path(base) / "tasks.sqlite3"
        TaskRepository(path).close()

        class Flow:
            tasks: dict = {}

            def status_view(self, task_id):
                time.sleep(delay)
                return {"task_id": task_id, "kind": "work", "status": "running", "revision": 1, "closed_reason": None}

        class Stub:
            flow, terminal, layout = Flow(), None, SimpleNamespace(tasks=path)
            task_status_budget = budget
            _task_status_item = Backend._task_status_item
        return Stub(), Backend.task_status

    def test_ids_past_the_budget_are_marked_timeout_and_the_answer_is_partial(self):
        import tempfile
        ids = [f"00000000-0000-4000-8000-{number:012x}" for number in range(1, 6)]
        with tempfile.TemporaryDirectory(prefix="ti09-budget-") as base:
            stub, task_status = self.stub(base, budget=0.5, delay=0.3)
            answer = task_status(stub, ids, False)
        self.assertIs(answer.get("timeout"), True, answer)
        done = [item for item in answer["tasks"] if not item.get("timeout")]
        late = [item for item in answer["tasks"] if item.get("timeout")]
        self.assertTrue(1 <= len(done) <= 2, answer)
        self.assertEqual([item["task_id"] for item in answer["tasks"]], ids, "every id, in order")
        self.assertEqual(late, [{"task_id": task_id, "timeout": True} for task_id in ids[len(done):]])

    def test_within_the_budget_there_is_no_timeout_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="ti09-budget-") as base:
            stub, task_status = self.stub(base, budget=2.0, delay=0.0)
            answer = task_status(stub, [TASK_ID], True)
        self.assertNotIn("timeout", answer)
        self.assertEqual((answer["tasks"][0]["task_id"], answer["tasks"][0]["exists"]), (TASK_ID, True))


class TaskStatusCommandTests(unittest.TestCase):
    """TI-13 (additive): each ``task_status`` item names the Task's running Workbench command as ``command``
    (``{command_id, owner, label, state}``, null when none), what the Hub's cancel confirmation shows and binds."""

    def answer(self, host_commands, running=None):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from workbench.backend.service import Backend
        from workbench.tasks.repository import TaskRepository

        class Flow:
            tasks: dict = {}

            def status_view(self, task_id):
                return {"task_id": task_id, "kind": "work", "status": "running", "revision": 1,
                        "closed_reason": None, "host_commands": host_commands}

        class Terminal:
            def runs_for_task(self, task_id):
                return []

            def running_host_command(self):
                return running

        with tempfile.TemporaryDirectory(prefix="ti13-status-") as base:
            path = Path(base) / "tasks.sqlite3"
            TaskRepository(path).close()

            class Stub:
                flow, terminal, layout = Flow(), Terminal(), SimpleNamespace(tasks=path)
                task_status_budget = 2.0
                _task_status_item = Backend._task_status_item
            return Backend.task_status(Stub(), [TASK_ID], False)["tasks"][0]

    def test_no_running_command_is_null(self):
        self.assertIsNone(self.answer([])["command"])

    def test_a_worker_terminal_command_has_its_label_and_state(self):
        command_id = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"
        item = self.answer([{"command_id": command_id, "kind": "terminal", "owner": "worker", "state": "stopping"}],
                           {"command_id": command_id, "owner": "worker", "label": "sleep 300", "state": "stopping"})
        self.assertEqual(item["command"], {"command_id": command_id, "owner": "worker", "label": "sleep 300",
                                           "state": "stopping"})

    def test_an_experiment_command_is_labelled_by_its_run(self):
        run_id = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
        item = self.answer([{"command_id": run_id, "kind": "experiment", "owner": "manager", "state": "unknown"}])
        self.assertEqual(item["command"], {"command_id": run_id, "owner": "manager", "label": "experiment 1a2b3c4d",
                                           "state": "unknown"})


class CancelTaskControllerTests(unittest.TestCase):
    """TI-13: ``Backend.cancel_task`` is ``TaskFlow.cancel_by_user``: ``{cancel, stop}`` or ``task_changed``."""

    def backend(self, answer):
        from types import SimpleNamespace
        calls = []

        class Flow:
            def cancel_by_user(self, task_id, reason, expect, *, request_id=None):
                calls.append((task_id, reason, expect, request_id))
                return answer
        return SimpleNamespace(flow=Flow()), calls

    def test_an_accepted_cancel_answers_cancel_and_stop(self):
        from workbench.backend.service import Backend
        stub, calls = self.backend({"status": "accepted", "cancel": "cancelled",
                                    "stop": {"status": "stopping", "command_id": TASK_ID}})
        expect = {"status": "running", "command_id": TASK_ID}
        self.assertEqual(Backend.cancel_task(stub, TASK_ID, REQUEST_ID, expect, "owner"),
                         {"cancel": "cancelled", "stop": {"status": "stopping", "command_id": TASK_ID}})
        self.assertEqual(calls, [(TASK_ID, "owner", expect, REQUEST_ID)])

    def test_a_changed_task_is_refused_as_task_changed(self):
        from workbench.backend.service import Backend
        from workbench.backend.ui_server import Held
        stub, _calls = self.backend({"status": "rejected", "reason": "task_changed", "detail": "changed",
                                     "task_status": "finished", "command_id": None})
        with self.assertRaises(Held) as raised:
            Backend.cancel_task(stub, TASK_ID, REQUEST_ID, {"status": "running", "command_id": None}, None)
        self.assertEqual(raised.exception.reason.value, "task_changed")


class CancelTaskRefusalTests(InboxFixture):
    def test_the_refusal_reaches_the_hub_as_reason_task_changed(self):
        from workbench.backend.ui_server import Held
        from workbench.contracts.ui_v1 import Reason

        def refuse(task_id, request_id, expect, reason):
            raise Held(Reason.TASK_CHANGED, "changed")
        self.controller.cancel_task = refuse
        answer = self.client().request(ClientType.CANCEL_TASK, **REQUESTS[2][1])
        self.assertEqual((answer["ok"], answer["reason"]), (False, "task_changed"), answer)


if __name__ == "__main__":
    unittest.main()
