"""CS-O10: ``stop_command`` over ui_v1 -- the UI server acknowledges at once and relays the final outcome later.

Seam: a real ``UiServer`` on a unix socket with a fake controller (the pattern of ``test_ui_server.py``) and the real
``UiClient``. The fake controller's ``stop_command`` blocks on an event the test holds, which is how the test knows
the server loop is not the thread that waits (a blocked loop could not answer the snapshot asked meanwhile).

Expected values are this file's own: the frame shapes the user decision names (an immediate ``stopping`` ack, then one
final ``result`` of the same request id with ``final: true`` and the stop outcome, then a state push).
"""

from __future__ import annotations

import threading
import time
import unittest
from uuid import uuid4

from test_ui_server import FakeController, ServerFixture
from workbench.contracts.ui_v1 import ClientType


class StopController(FakeController):
    def __init__(self):
        super().__init__()
        self.stop_calls: list[str] = []
        self.release = threading.Event()
        self.entered = threading.Event()
        self.outcome = None
        self.outcomes: dict[str, dict] = {}

    def stop_command(self, command_id):
        self.stop_calls.append(command_id)
        self.entered.set()
        self.release.wait(10)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return {**(self.outcome or {}), "command_id": command_id}


class StopCommandRelayTests(ServerFixture):
    def setUp(self):
        super().setUp()
        # ServerFixture builds its own FakeController; ours replaces it before the first request
        self.controller = StopController()
        self.server.controller = self.controller
        self.addCleanup(lambda: self.controller.release.set())

    def attached_client(self):
        client = self.client()
        client.attach()
        client.pump(0.05)
        return client

    def next_result(self, client, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = client.next_frame(max(0.0, deadline - time.monotonic()))
            if frame.header.get("type") == "result":
                return frame.header
        self.fail("no result frame")

    def send_stop(self, client, command_id):
        request_id = uuid4().hex[:16]
        client.send_frame({"v": client.version, "type": "stop_command", "id": request_id, "command_id": command_id})
        return request_id

    def test_the_ack_is_immediate_and_the_final_outcome_follows_without_blocking_the_loop(self):
        client = self.attached_client()
        command_id = str(uuid4())
        sent_at = time.monotonic()
        request_id = self.send_stop(client, command_id)
        ack = self.next_result(client)
        self.assertLess(time.monotonic() - sent_at, 2.0)
        self.assertEqual((ack["id"], ack["ok"], ack["command_id"], ack["stop"]["status"]),
                         (request_id, True, command_id, "stopping"), ack)
        self.assertNotIn("final", ack)
        self.assertTrue(self.controller.entered.wait(5), "the controller was asked off the loop")
        # the loop is free while the stop waits: another request is answered at once
        self.assertEqual(client.snapshot()["focus"], self.controller.focus.value)
        states_before = len(client.states)
        self.controller.outcome = {"status": "exited", "exit_code": 143, "signal": 15, "signal_name": "SIGTERM",
                                   "requested_by": "user"}
        self.controller.release.set()
        final = self.next_result(client)
        self.assertEqual((final["id"], final["ok"], final["final"], final["command_id"]),
                         (request_id, True, True, command_id), final)
        self.assertEqual((final["stop"]["status"], final["stop"]["exit_code"], final["stop"]["signal_name"]),
                         ("exited", 143, "SIGTERM"))
        deadline = time.monotonic() + 3
        while len(client.states) == states_before and time.monotonic() < deadline:
            client.pump(0.05)
        self.assertGreater(len(client.states), states_before, "a state push follows the final outcome")
        self.assertEqual(self.controller.stop_calls, [command_id])

    def test_a_refusal_or_unknown_outcome_is_relayed_as_is(self):
        client = self.attached_client()
        for outcome in ({"status": "unknown", "reason": "still running after SIGKILL"},
                        {"status": "refused", "reason": "not_owner"},
                        {"status": "not_found", "reason": "unknown_command"}):
            with self.subTest(status=outcome["status"]):
                self.controller.outcome = outcome
                self.controller.release.set()
                self.send_stop(client, str(uuid4()))
                self.next_result(client)  # ack
                final = self.next_result(client)
                self.assertEqual((final["final"], final["stop"]["status"], final["stop"]["reason"]),
                                 (True, outcome["status"], outcome["reason"]), final)

    def test_a_failing_controller_gives_a_final_error_outcome_and_the_server_lives(self):
        client = self.attached_client()
        self.controller.outcome = RuntimeError("boom")
        self.controller.release.set()
        self.send_stop(client, str(uuid4()))
        self.next_result(client)
        final = self.next_result(client)
        self.assertEqual((final["ok"], final["final"], final["stop"]["status"], final["stop"]["reason"]),
                         (True, True, "error", "internal_error"), final)
        self.assertNotIn("boom", str(final["stop"].get("detail", "")).replace("RuntimeError", ""), "no traceback text")
        self.assertEqual(client.snapshot()["focus"], self.controller.focus.value)

    def test_stop_requires_attachment_and_a_canonical_command_id(self):
        client = self.client()
        refused = client.request(ClientType.STOP_COMMAND, command_id=str(uuid4()))
        self.assertEqual((refused["ok"], refused["reason"]), (False, "not_attached"))
        client.attach()
        for bad in ("not-a-uuid", "", str(uuid4()).upper()):
            invalid = client.request(ClientType.STOP_COMMAND, command_id=bad)
            self.assertEqual((invalid["ok"], invalid["reason"]), (False, "invalid_message"), bad)
        self.assertEqual(self.controller.stop_calls, [], "nothing reached the controller")

    def test_too_many_stops_in_flight_are_refused_and_the_rest_still_finish(self):
        client = self.attached_client()
        ids = [self.send_stop(client, str(uuid4())) for _ in range(8)]
        for _ in ids:
            self.assertTrue(self.next_result(client)["ok"])  # eight acks
        refused = client.request(ClientType.STOP_COMMAND, command_id=str(uuid4()))
        self.assertEqual((refused["ok"], refused["reason"]), (False, "queue_full"))
        self.controller.outcome = {"status": "exited", "exit_code": 130}
        self.controller.release.set()
        finals = [self.next_result(client) for _ in ids]
        self.assertEqual(sorted(f["id"] for f in finals), sorted(ids))

    def test_an_outcome_that_cannot_be_encoded_becomes_a_final_error_and_the_loop_lives(self):
        client = self.attached_client()
        self.controller.outcome = {"status": "exited", "oops": object()}
        self.controller.release.set()
        request_id = self.send_stop(client, str(uuid4()))
        ack = self.next_result(client)
        self.assertEqual((ack["id"], ack["stop"]["status"]), (request_id, "stopping"), "the ack comes first")
        final = self.next_result(client)
        self.assertEqual((final["id"], final["ok"], final["final"]), (request_id, True, True), final)
        self.assertEqual((final["stop"]["status"], final["stop"]["reason"]), ("error", "unencodable_outcome"), final)
        self.assertNotIn("oops", str(final))
        self.assertEqual(client.snapshot()["focus"], self.controller.focus.value, "the server loop lives")
        self.controller.outcome = {"status": "exited", "exit_code": 0}  # the slot was freed: eight more still fit
        ids = [self.send_stop(client, str(uuid4())) for _ in range(8)]
        results = [self.next_result(client) for _ in range(16)]  # eight acks and eight finals, interleaved
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual(sum(1 for r in results if r.get("final")), 8)

    def test_a_client_that_left_before_the_final_outcome_does_not_hurt_the_server(self):
        client = self.attached_client()
        self.send_stop(client, str(uuid4()))
        self.next_result(client)
        client.close()
        self.controller.release.set()
        time.sleep(0.3)
        other = self.client()
        self.assertEqual(other.snapshot()["focus"], self.controller.focus.value)


if __name__ == "__main__":
    unittest.main()
