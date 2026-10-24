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


if __name__ == "__main__":
    unittest.main()
