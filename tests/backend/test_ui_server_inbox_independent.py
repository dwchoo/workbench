"""TI-09 independent checks (TI-T05, seam T7): the Hub-only ui_v1 requests ``inbox_notice``, ``task_status``,
``cancel_task`` on a real ``UiServer`` with a recording fake controller and the real ``UiClient``.

Expected values come from the ui_v1 contract text (``contracts/ui_v1.py`` "Hub requests") and the Task Inbox SPEC
"Hub -> backend": the three requests are served only to a connection whose hello ``client`` is ``workbench-hub`` and
whose server-side peer uid is the backend's euid, attached or not; every other connection (an attached UI included) is
``not_attached`` and the controller is not called; they never take, change or wait for the attach slot; ``task_ids`` is
1-``MAX_TASK_STATUS_IDS`` distinct canonical UUIDs. The controller call record is the evidence.
"""

from __future__ import annotations

import unittest

from test_ui_server_hub_control import (AutomationController, ForeignUidServer, HubControlFixture,
                                        UnreadablePeerServer)
from workbench.backend.ui_server import Held
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, Frame, Reason
from workbench.contracts.v1 import ContractError, PaneId

REQUEST_ID = "6f1b6a3e-2f40-4a3f-9d4e-0c1b2a3d4e5f"
DELIVERY_ID = "0b9c4f62-0d2a-4b8e-9b55-3f2c1d0e9a87"
TASK_ID = "2c5e8a1f-7b3d-4c9e-8f60-1a2b3c4d5e6f"
FIELDS = {"request_id": REQUEST_ID, "seq": 1, "title": "Collect logs", "origin": {"kind": "owner"}, "state": "NEW",
          "excerpt": "short", "hint": None}
NOTICE = {"request_id": REQUEST_ID, "delivery_id": DELIVERY_ID, "event": "new", "fields": FIELDS}
STATUS = {"task_ids": [TASK_ID], "detail": False}
CANCEL = {"task_id": TASK_ID, "request_id": REQUEST_ID, "expect": {"status": "running", "command_id": None},
          "reason": "owner"}
REQUESTS = ((ClientType.INBOX_NOTICE, NOTICE), (ClientType.TASK_STATUS, STATUS), (ClientType.CANCEL_TASK, CANCEL))
HUB_ONLY_TYPES = {"inbox_notice", "task_status", "cancel_task"}


def uuid_n(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012x}"


class Recorder(AutomationController):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []
        self.refuse: Held | None = None

    def _maybe_refuse(self):
        if self.refuse is not None:
            raise self.refuse

    def inbox_notice(self, request_id, delivery_id, event, fields):
        self.calls.append(("inbox_notice", request_id, delivery_id, event, fields))
        self._maybe_refuse()
        return {"queued": True, "notice_id": "n-1"}

    def task_status(self, task_ids, detail):
        self.calls.append(("task_status", list(task_ids), detail))
        self._maybe_refuse()
        return {"tasks": [{"task_id": task_id, "exists": False} for task_id in task_ids]}

    def cancel_task(self, task_id, request_id, expect, reason):
        self.calls.append(("cancel_task", task_id, request_id, expect, reason))
        self._maybe_refuse()
        return {"cancel": "cancel_requested", "stop": None}


class Fixture(HubControlFixture):
    def setUp(self):
        super().setUp()
        self.controller = Recorder()
        self.server.controller = self.controller

    def refused(self, client, kind, fields, reason):
        result = client.request(kind, **fields)
        self.assertEqual((result["ok"], result.get("reason")), (False, reason), (kind.value, result))


class ContractSurfaceTests(unittest.TestCase):
    def test_the_hub_only_types_are_distinct_request_types_and_not_pause_resume_style_gates(self):
        names = {kind.value for kind in ClientType}
        self.assertTrue(HUB_ONLY_TYPES <= names)
        self.assertEqual(ui_v1.NON_ATTACHED_CONTROL_CLIENTS, frozenset({"workbench-hub"}))
        self.assertEqual(ui_v1.MAX_TASK_STATUS_IDS, 64)  # SPEC "task_ids 상한 ≤ 64"
        self.assertEqual(ui_v1.VERSION, 1)  # additive: the version is unchanged

    def test_a_backend_without_a_real_inbox_notice_or_cancel_refuses_unsupported_type_not_ok(self):
        from workbench.backend.service import Backend
        for call, arguments in ((Backend.inbox_notice, (REQUEST_ID, DELIVERY_ID, "new", FIELDS)),
                                (Backend.cancel_task, (TASK_ID, REQUEST_ID, CANCEL["expect"], None))):
            with self.subTest(call=call.__name__):
                with self.assertRaises(Held) as raised:
                    call(object(), *arguments)
                self.assertEqual(raised.exception.reason, Reason.UNSUPPORTED_TYPE)


class GateTests(Fixture):
    def test_each_request_reaches_the_controller_once_with_the_parsed_arguments_for_the_hub(self):
        hub = self.client()
        answers = {ClientType.INBOX_NOTICE: ("queued", True), ClientType.TASK_STATUS: ("tasks", None),
                   ClientType.CANCEL_TASK: ("cancel", "cancel_requested")}
        for kind, fields in REQUESTS:
            with self.subTest(kind=kind.value):
                before = len(self.controller.calls)
                result = hub.request(kind, **fields)
                self.assertTrue(result["ok"], result)
                self.assertIn(answers[kind][0], result)
                self.assertEqual(len(self.controller.calls), before + 1)
        self.assertEqual([call[0] for call in self.controller.calls], ["inbox_notice", "task_status", "cancel_task"])

    def test_nobody_else_gets_them_whether_or_not_a_ui_is_attached(self):
        for attached in (False, True):
            if attached:
                self.attached_ui()
            for name in ("workbench-ui", "workbench-cli", "workbench-backend", "workbench-hub-x", "x"):
                other = self.client(name=name)
                for kind, fields in REQUESTS:
                    with self.subTest(attached=attached, name=name, kind=kind.value):
                        self.refused(other, kind, fields, "not_attached")
        self.assertEqual(self.controller.calls, [])

    def test_an_attached_ui_is_refused_on_its_own_attached_connection_and_stays_attached(self):
        ui = self.attached_ui()
        attached = self.server.attached
        for kind, fields in REQUESTS:
            with self.subTest(kind=kind.value):
                self.refused(ui, kind, fields, "not_attached")
        self.assertIs(self.server.attached, attached)
        self.assertEqual((len(self.controller.attaches), self.controller.detaches), (1, 0))
        self.assertTrue(ui.input(PaneId.MANAGER_OMP, b"x")["ok"], "the UI still controls the pane")
        self.assertEqual(self.controller.calls, [])

    def test_hub_requests_never_take_or_wait_for_the_attach_slot(self):
        hub = self.client()
        for kind, fields in REQUESTS * 3:
            self.assertTrue(hub.request(kind, **fields)["ok"])
        self.assertIsNone(self.server.attached, "a Hub request takes no attach slot")
        self.assertEqual(self.controller.attaches, [])
        ui = self.client(name="workbench-ui")
        self.assertTrue(ui.attach()["ok"], "a UI can still attach after Hub requests")
        # a UI is attached now: the Hub is still served, the UI keeps the slot and nothing detaches it
        attached = self.server.attached
        ui.states.clear()
        for kind, fields in REQUESTS:
            self.assertTrue(hub.request(kind, **fields)["ok"])
        ui.pump(0.2)
        self.assertEqual(ui.states, [], "no state push for Hub requests")
        self.assertIs(self.server.attached, attached)
        self.assertEqual((len(self.controller.attaches), self.controller.detaches), (1, 0))
        self.assertTrue(ui.input(PaneId.MANAGER_OMP, b"z")["ok"])
        taken = hub.request(ClientType.ATTACH)
        self.assertEqual((taken["ok"], taken.get("reason")), (False, "attached_elsewhere"), taken)

    def test_a_hub_name_requests_other_attach_needing_types_stay_gated_except_pause_resume(self):
        hub = self.client()
        for kind, fields in ((ClientType.DETACH, {}), (ClientType.RESTART_PANE, {"pane": PaneId.MANAGER_OMP.value}),
                             (ClientType.KILL_PANE, {"pane": PaneId.MANAGER_OMP.value})):
            with self.subTest(kind=kind.value):
                result = hub.request(kind, **fields)
                self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertEqual(self.controller.restarts, [])

    def test_a_controller_refusal_is_reported_and_the_connection_survives(self):
        hub = self.client()
        self.controller.refuse = Held(Reason.UNSUPPORTED_TYPE, "not yet")
        for kind, fields in REQUESTS:
            with self.subTest(kind=kind.value):
                self.refused(hub, kind, fields, "unsupported_type")
        self.controller.refuse = None
        self.assertTrue(hub.request(ClientType.TASK_STATUS, **STATUS)["ok"])
        self.assertIsNone(self.server.attached)


class ForeignUidTests(Fixture):
    server_class = ForeignUidServer

    def test_the_hub_name_from_another_uid_is_refused_even_with_an_attached_ui_present(self):
        self.attached_ui()
        attached = self.server.attached
        hub = self.client()
        for kind, fields in REQUESTS:
            with self.subTest(kind=kind.value):
                self.refused(hub, kind, fields, "not_attached")
        self.assertEqual(self.controller.calls, [])
        self.assertIs(self.server.attached, attached)


class UnreadablePeerTests(Fixture):
    server_class = UnreadablePeerServer

    def test_an_unreadable_peer_never_passes(self):
        hub = self.client()
        for kind, fields in REQUESTS:
            with self.subTest(kind=kind.value):
                self.refused(hub, kind, fields, "not_attached")
        self.assertEqual(self.controller.calls, [])


class TaskStatusPayloadTests(Fixture):
    def test_the_id_list_is_capped_at_64_distinct_canonical_uuids_in_request_order(self):
        hub = self.client()
        full = [uuid_n(number) for number in range(64)]
        result = hub.request(ClientType.TASK_STATUS, task_ids=list(reversed(full)), detail=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.controller.calls[-1], ("task_status", list(reversed(full)), True))
        self.assertEqual([item["task_id"] for item in result["tasks"]], list(reversed(full)))
        calls = len(self.controller.calls)
        for case, ids in (("65 ids", [uuid_n(number) for number in range(65)]), ("empty", []),
                          ("duplicate", [TASK_ID, TASK_ID]), ("duplicate in 64", full[:63] + full[:1]),
                          ("upper case", [TASK_ID.upper()]), ("no hyphens", [TASK_ID.replace("-", "")]),
                          ("braced", ["{" + TASK_ID + "}"]), ("urn", ["urn:uuid:" + TASK_ID]),
                          ("padded", [" " + TASK_ID]), ("not a uuid", ["task-1"]), ("int", [7]), ("none", [None]),
                          ("nested", [[TASK_ID]]), ("object", [{"id": TASK_ID}]), ("string instead of list", TASK_ID),
                          ("null", None), ("object instead of list", {"a": TASK_ID})):
            with self.subTest(case=case):
                self.refused(hub, ClientType.TASK_STATUS, {"task_ids": ids, "detail": False}, "invalid_message")
        self.refused(hub, ClientType.TASK_STATUS, {"detail": False}, "invalid_message")
        self.assertEqual(len(self.controller.calls), calls, "an invalid list never reaches the controller")

    def test_detail_is_a_boolean_and_false_when_absent(self):
        hub = self.client()
        self.assertTrue(hub.request(ClientType.TASK_STATUS, task_ids=[TASK_ID])["ok"])
        self.assertEqual(self.controller.calls[-1], ("task_status", [TASK_ID], False))
        self.assertTrue(hub.request(ClientType.TASK_STATUS, task_ids=[TASK_ID], detail=True)["ok"])
        self.assertEqual(self.controller.calls[-1], ("task_status", [TASK_ID], True))
        calls = len(self.controller.calls)
        for detail in ("true", 1, 0, None, [], {}, "yes"):
            with self.subTest(detail=detail):
                self.refused(hub, ClientType.TASK_STATUS, {"task_ids": [TASK_ID], "detail": detail}, "invalid_message")
        self.assertEqual(len(self.controller.calls), calls)

    def test_an_oversized_list_from_a_non_hub_connection_is_still_a_refusal_and_never_a_call(self):
        other = self.client(name="workbench-ui")
        result = other.request(ClientType.TASK_STATUS, task_ids=[uuid_n(number) for number in range(65)])
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.controller.calls, [])

    def test_the_contract_parser_alone_holds_the_cap(self):
        ids = [uuid_n(number) for number in range(65)]
        with self.assertRaises(ContractError):
            ui_v1.parse_client_frame(Frame({"v": 1, "type": "task_status", "id": "r", "task_ids": ids}, b"", 0), 1)
        parsed = ui_v1.parse_client_frame(Frame({"v": 1, "type": "task_status", "id": "r", "task_ids": ids[:64]},
                                                b"", 0), 1)
        self.assertEqual((parsed.type, len(parsed.fields["task_ids"])), (ClientType.TASK_STATUS, 64))


class OtherPayloadTests(Fixture):
    def notice(self, **changes):
        fields = {**FIELDS, **changes.pop("fields", {})}
        for key in changes.pop("drop_fields", ()):
            fields.pop(key)
        return {**NOTICE, "fields": fields, **changes}

    def test_inbox_notice_accepts_each_event_and_the_limits_and_refuses_the_rest(self):
        hub = self.client()
        for event in ("new", "answered", "updated", "revised", "cancelled", "linked_task_closed"):
            with self.subTest(event=event):
                self.assertTrue(hub.request(ClientType.INBOX_NOTICE, **self.notice(event=event))["ok"])
        for state in ("NEW", "REVIEWING", "NEEDS_INFO", "READY", "RUNNING", "CLOSED"):
            with self.subTest(state=state):
                self.assertTrue(hub.request(ClientType.INBOX_NOTICE, **self.notice(fields={"state": state}))["ok"])
        edge = self.notice(fields={"title": "t" * 200, "excerpt": "e" * 1000, "hint": "h" * 1000, "seq": 2 ** 40})
        self.assertTrue(hub.request(ClientType.INBOX_NOTICE, **edge)["ok"])
        self.assertTrue(hub.request(ClientType.INBOX_NOTICE, **self.notice(fields={"hint": "text"}))["ok"])
        calls = len(self.controller.calls)
        bad = {
            "event unknown": self.notice(event="started"), "event missing": {k: v for k, v in NOTICE.items()
                                                                              if k != "event"},
            "event not str": self.notice(event=1),
            "delivery id": self.notice(delivery_id="d-1"), "request id": self.notice(request_id="r-1"),
            "request id upper": self.notice(request_id=REQUEST_ID.upper()),
            "fields.request_id differs": self.notice(fields={"request_id": uuid_n(1)}),
            "fields not object": {**NOTICE, "fields": "x"},
            "fields missing key": self.notice(drop_fields=("hint",)),
            "fields extra key": self.notice(fields={"body": "full body must not be sent"}),
            "seq zero": self.notice(fields={"seq": 0}), "seq negative": self.notice(fields={"seq": -3}),
            "seq bool": self.notice(fields={"seq": True}), "seq str": self.notice(fields={"seq": "1"}),
            "seq float": self.notice(fields={"seq": 1.0}),
            "title empty": self.notice(fields={"title": ""}), "title long": self.notice(fields={"title": "t" * 201}),
            "title int": self.notice(fields={"title": 5}),
            "excerpt long": self.notice(fields={"excerpt": "e" * 1001}),
            "excerpt none": self.notice(fields={"excerpt": None}),
            "hint long": self.notice(fields={"hint": "h" * 1001}), "hint int": self.notice(fields={"hint": 3}),
            "origin list": self.notice(fields={"origin": []}), "origin none": self.notice(fields={"origin": None}),
            "state unknown": self.notice(fields={"state": "DONE"}),
            "state lower": self.notice(fields={"state": "new"}),
        }
        for case, fields in bad.items():
            with self.subTest(case=case):
                self.refused(hub, ClientType.INBOX_NOTICE, fields, "invalid_message")
        self.assertEqual(len(self.controller.calls), calls)

    def test_cancel_task_refuses_a_malformed_expect_or_ids_before_the_controller(self):
        hub = self.client()
        self.assertTrue(hub.request(ClientType.CANCEL_TASK, **{**CANCEL, "reason": None})["ok"])
        self.assertTrue(hub.request(ClientType.CANCEL_TASK, **{
            **CANCEL, "expect": {"status": "waiting_report", "command_id": TASK_ID}, "reason": "r" * 1000})["ok"])
        calls = len(self.controller.calls)
        bad = {
            "task id": {**CANCEL, "task_id": "t"}, "request id": {**CANCEL, "request_id": "r"},
            "expect missing key": {**CANCEL, "expect": {"status": "running"}},
            "expect extra key": {**CANCEL, "expect": {"status": "running", "command_id": None, "x": 1}},
            "expect not object": {**CANCEL, "expect": "running"},
            "status unknown": {**CANCEL, "expect": {"status": "weird", "command_id": None}},
            "status none": {**CANCEL, "expect": {"status": None, "command_id": None}},
            "command id": {**CANCEL, "expect": {"status": "running", "command_id": "c-1"}},
            "reason long": {**CANCEL, "reason": "r" * 1001}, "reason int": {**CANCEL, "reason": 1},
        }
        for case, fields in bad.items():
            with self.subTest(case=case):
                self.refused(hub, ClientType.CANCEL_TASK, fields, "invalid_message")
        self.assertEqual(len(self.controller.calls), calls)


if __name__ == "__main__":
    unittest.main()
