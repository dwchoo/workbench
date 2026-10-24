"""CS-O10 independent ui_v1 server checks: ``stop_command`` is acknowledged at once, finishes later, never blocks.

Derived from the contract (FOLLOWUPS-BRIEF FU-D06, FU-D08, FU-AC-09 and the task packet), not from the implementer's
tests:

* protocol: ``command_id`` is required and a canonical UUID; a payload is refused; an attached client is required.
* the server answers immediately with ``stop.status == "stopping"`` while the controller's stop still runs, then sends
  a final ``result`` frame with the same id and ``final: true`` carrying the controller's outcome, then a state push.
  The ack always comes before its final. A controller that raises or returns junk still yields a final that is never
  a success; the server keeps serving.
* the server loop is never blocked by a stop: other clients and requests are served while a stop is waiting.
* concurrency is bounded (``queue_full``), a slot is released when the stop ends even if the client left, and a
  client that disconnected misses its final without harming the server.

The controller is a fake whose ``stop_command`` blocks on a gate this file owns; the server runs on a private UDS
in an owned temp dir, polled by one thread this file owns.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import unittest

from workbench.backend.client import ClientError, UiClient
from workbench.backend.ui_server import MAX_STOPS_IN_FLIGHT, UiServer
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType
from workbench.contracts.v1 import PaneId

CID = "5f1c2a9e-3b4d-4e6f-8a7b-0c1d2e3f4a5b"
OTHER = "6a2d3b0f-4c5e-4f70-9b8c-1d2e3f4a5b6c"
EXITED = {"status": "exited", "exit_code": 130, "signal": 2, "signal_name": "SIGINT", "command_id": CID,
          "requested_by": "user"}


class Controller:
    """The minimum ui_v1 controller; ``stop_command`` is scripted per test."""

    def __init__(self):
        self.calls: list[str] = []
        self.gates: list[threading.Event] = []
        self.lock = threading.Lock()
        self.outcome = dict(EXITED)
        self.exception: Exception | None = None
        self.block = False
        self.snapshots = 0
        self.focus = PaneId.MANAGER_OMP
        self.state_marker = 0

    def snapshot(self):
        self.snapshots += 1
        return {"focus": self.focus.value, "marker": self.state_marker, "calls": len(self.calls)}

    def stop_command(self, command_id):
        gate = threading.Event()
        with self.lock:
            self.calls.append(command_id)
            self.gates.append(gate)
        if self.block:
            gate.wait(30)
        if self.exception is not None:
            raise self.exception
        outcome = self.outcome
        return outcome(command_id) if callable(outcome) else outcome

    def release_all(self):
        self.block = False
        for gate in list(self.gates):
            gate.set()

    # the rest of the Controller protocol: never reached by these tests
    def on_attach(self, size):
        pass

    def on_detach(self):
        pass

    def replay(self):
        return []

    def admit(self, pane, data, kind):
        return None

    def resize(self, pane, rows, cols):
        pass

    def set_focus(self, pane):
        self.focus = pane


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="csO10-uis-", dir=tempfile.gettempdir()))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "ui.sock"
        self.controller = Controller()
        self.server = UiServer(self.path, self.controller)
        self.halt = threading.Event()
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown)
        self.clients: list[UiClient] = []
        self.buffer: dict = {}

    def _serve(self):
        try:
            while not self.halt.is_set():
                self.server.poll(0.01)
        except BaseException as exc:  # a poll that raises would silently end the real backend loop
            self.errors.append(exc)

    def _teardown(self):
        self.controller.release_all()
        self.halt.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        if not getattr(self, "closed", False):
            self.server.close(flush_timeout=0.5)
        for client in self.clients:
            client.close()
        self.assertEqual(self.errors, [], "the server loop raised")

    def client(self, *, attach=True) -> UiClient:
        client = UiClient(self.path, timeout=10)
        self.clients.append(client)
        if attach:
            self.assertTrue(client.attach((24, 80))["ok"])
        return client

    def send_stop(self, client, command_id=CID, **extra) -> str:
        request_id = f"stop-{time.monotonic_ns()}"
        client.send_frame({"v": 1, "type": "stop_command", "id": request_id, "command_id": command_id, **extra})
        return request_id

    def result_frames(self, client, request_id, count, timeout=10):
        """The next ``count`` result frames with ``request_id``; results of other ids are kept for later calls."""
        buffered = self.buffer.setdefault((id(client), request_id), [])
        deadline = time.monotonic() + timeout
        while len(buffered) < count:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, f"only {len(buffered)} of {count} result frames arrived: {buffered}")
            frame = client.next_frame(remaining)
            if frame.header.get("type") == "result":
                self.buffer.setdefault((id(client), frame.header.get("id")), []).append(frame.header)
        taken = buffered[:count]
        del buffered[:count]
        return taken

    def wait_for(self, predicate, what, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {what}")


class ProtocolTests(Fixture):
    def refused(self, client, **header):
        request_id = f"p-{time.monotonic_ns()}"
        payload = header.pop("_payload", b"")
        client.send_frame({"v": 1, "type": "stop_command", "id": request_id, **header}, payload)
        (answer,) = self.result_frames(client, request_id, 1)
        return {"reason": None, **answer}

    def test_command_id_must_be_present_and_a_canonical_uuid(self):
        client = self.client()
        bad = [None, "", " ", "not-a-uuid", CID.upper(), CID.replace("-", ""), f" {CID}", f"{CID} ", f"{CID}\n",
               f"urn:uuid:{CID}", f"{{{CID}}}", 12345, True, [CID], {"id": CID}, CID[:-1], CID + "0", "../../etc/passwd"]
        for value in bad:
            with self.subTest(command_id=value):
                answer = self.refused(client, command_id=value)
                self.assertEqual((answer["ok"], answer["reason"]), (False, "invalid_message"), answer)
                self.assertNotIn("final", answer)
        missing = self.refused(client)
        self.assertEqual((missing["ok"], missing["reason"]), (False, "invalid_message"), missing)
        self.assertEqual(self.controller.calls, [], "an invalid id reached the controller")
        self.assertIn("focus", client.snapshot(), "the connection survived")

    def test_a_payload_is_refused(self):
        client = self.client()
        answer = self.refused(client, command_id=CID, _payload=b"x")
        self.assertEqual((answer["ok"], answer["reason"]), (False, "invalid_message"), answer)
        self.assertEqual(self.controller.calls, [])

    def test_unknown_header_fields_are_ignored_and_the_controller_receives_only_the_command_id(self):
        """Root decision: ui_v1 requests ignore unknown header fields (like kill_pane, pause); stop_command too."""
        client = self.client()
        extras = ({"pane": "host_shell"}, {"reason": "because"}, {"signal": "SIGKILL"}, {"force": True},
                  {"pid": 123}, {"command_id_2": OTHER}, {"requested_by": "worker"})
        for extra in extras:
            with self.subTest(extra=extra):
                request_id = self.send_stop(client, CID, **extra)
                ack, final = self.result_frames(client, request_id, 2)
                self.assertEqual((ack["ok"], ack["stop"]), (True, {"status": "stopping"}), ack)
                self.assertIs(final["final"], True)
                for frame in (ack, final):
                    for key in extra:
                        self.assertNotIn(key, frame, "an unknown request field was echoed or acted upon")
        self.assertEqual(self.controller.calls, [CID] * len(extras), "the controller was asked about another id")

    def test_stop_requires_an_attached_client(self):
        client = self.client(attach=False)
        answer = self.refused(client, command_id=CID)
        self.assertEqual((answer["ok"], answer["reason"]), (False, "not_attached"), answer)
        other = self.client()
        answer = self.refused(client, command_id=CID)  # still not the attached connection
        self.assertEqual((answer["ok"], answer["reason"]), (False, "not_attached"), answer)
        self.assertEqual(self.controller.calls, [])
        self.assertIn("focus", other.snapshot())

    def test_the_client_type_exists_and_other_stop_like_types_do_not(self):
        self.assertEqual(ClientType("stop_command"), ClientType.STOP_COMMAND)
        client = self.client()
        for name in ("stop", "stop_commands", "kill_command", "interrupt_command"):
            client.send_frame({"v": 1, "type": name, "id": name, "command_id": CID})
            frame = client.next_frame()
            self.assertEqual((frame.header["id"], frame.header["reason"]), (name, "unsupported_type"))
        self.assertEqual(self.controller.calls, [])

    def test_an_id_that_looks_like_a_process_is_never_forwarded(self):
        client = self.client()
        for value in ("1", "-1", "0", "$$", "1234"):
            answer = self.refused(client, command_id=value)
            self.assertEqual(answer["reason"], "invalid_message")
        self.assertEqual(self.controller.calls, [])


class AckAndFinalTests(Fixture):
    def test_the_ack_comes_at_once_while_the_controller_is_still_working_then_the_final(self):
        self.controller.block = True
        client = self.client()
        started = time.monotonic()
        request_id = self.send_stop(client)
        (ack,) = self.result_frames(client, request_id, 1)
        self.assertLess(time.monotonic() - started, 3.0, "the ack waited for the controller")
        self.assertEqual((ack["ok"], ack["command_id"]), (True, CID), ack)
        self.assertEqual(ack["stop"], {"status": "stopping"})
        self.assertNotIn("final", ack)
        self.wait_for(lambda: self.controller.calls == [CID], "the controller to be asked")
        self.assertFalse(any(gate.is_set() for gate in self.controller.gates), "the stop is still running")
        self.controller.release_all()
        (final,) = self.result_frames(client, request_id, 1)
        self.assertEqual((final["ok"], final["final"], final["command_id"]), (True, True, CID), final)
        self.assertEqual(final["stop"], EXITED)

    def test_the_ack_always_precedes_its_final_even_when_the_controller_answers_instantly(self):
        client = self.client()
        for n in range(60):
            request_id = self.send_stop(client)
            first, second = self.result_frames(client, request_id, 2)
            self.assertNotIn("final", first, f"round {n}: the final overtook the ack")
            self.assertEqual(first["stop"], {"status": "stopping"})
            self.assertIs(second.get("final"), True)
            self.assertEqual(second["stop"], EXITED)

    def test_the_loop_serves_other_requests_while_a_stop_waits(self):
        self.controller.block = True
        first, second = self.client(), self.client(attach=False)
        request_id = self.send_stop(first)
        self.result_frames(first, request_id, 1)
        self.wait_for(lambda: self.controller.calls, "the stop to start")
        for _ in range(20):  # a blocked loop would stall every one of these for the whole stop
            started = time.monotonic()
            self.assertIn("focus", second.snapshot())
            self.assertLess(time.monotonic() - started, 1.0, "the server loop is blocked by the stop")
        # the attached client itself is served too
        self.assertTrue(first.input(PaneId.MANAGER_OMP, b"a")["ok"])
        self.assertFalse(any(gate.is_set() for gate in self.controller.gates), "the stop was still running")
        self.controller.release_all()
        (final,) = self.result_frames(first, request_id, 1)
        self.assertIs(final["final"], True)

    def test_every_outcome_is_forwarded_unchanged_and_never_dressed_up(self):
        outcomes = [
            {"status": "unknown", "reason": "still_alive_after_sigkill", "command_id": CID, "requested_by": "user"},
            {"status": "refused", "reason": "not_owner", "command_id": CID, "requested_by": "user"},
            {"status": "not_found", "reason": "unknown_command", "command_id": CID, "requested_by": "user"},
            {"status": "already_exited", "exit_code": 0, "command_id": CID, "requested_by": "user"},
            {"status": "stale", "reason": "identity_mismatch", "command_id": CID, "requested_by": "user"},
            {"status": "stopping", "command_id": CID, "requested_by": "user"},
            {"status": "refused", "reason": "command_stop_unavailable", "command_id": CID, "requested_by": "user"},
            EXITED]
        client = self.client()
        for outcome in outcomes:
            with self.subTest(status=outcome["status"], reason=outcome.get("reason")):
                self.controller.outcome = outcome
                request_id = self.send_stop(client)
                ack, final = self.result_frames(client, request_id, 2)
                self.assertEqual(ack["stop"], {"status": "stopping"})
                self.assertEqual(final["stop"], outcome)
                self.assertIs(final["final"], True)

    def test_a_state_push_follows_the_final_so_the_ui_sees_the_command_gone_at_once(self):
        client = self.client()
        client.states.clear()
        self.controller.state_marker = 41
        request_id = self.send_stop(client)
        self.result_frames(client, request_id, 2)
        self.wait_for(lambda: client.pump(0.05) is not None and client.states, "the state push")
        self.assertEqual(client.states[-1]["marker"], 41)
        self.assertEqual(client.states[-1]["calls"], 1, "the push was taken after the controller ran")

    def test_two_stops_by_one_client_get_their_own_ack_and_final(self):
        self.controller.block = True
        client = self.client()
        a = self.send_stop(client, CID)
        b = self.send_stop(client, OTHER)
        self.result_frames(client, a, 1)
        self.result_frames(client, b, 1)
        self.wait_for(lambda: len(self.controller.calls) == 2, "both stops to start")
        self.controller.outcome = lambda command_id: {"status": "exited", "command_id": command_id, "exit_code": 0}
        self.controller.release_all()
        (final_a,) = self.result_frames(client, a, 1)
        (final_b,) = self.result_frames(client, b, 1)
        self.assertEqual((final_a["command_id"], final_a["stop"]["command_id"]), (CID, CID))
        self.assertEqual((final_b["command_id"], final_b["stop"]["command_id"]), (OTHER, OTHER))
        self.assertEqual(sorted(self.controller.calls), sorted([CID, OTHER]))

    def test_a_snapshot_requested_between_ack_and_final_is_answered_in_order(self):
        self.controller.block = True
        client = self.client()
        request_id = self.send_stop(client)
        self.result_frames(client, request_id, 1)
        self.assertIn("focus", client.snapshot())
        self.controller.release_all()
        (final,) = self.result_frames(client, request_id, 1)
        self.assertIs(final["final"], True)


class ControllerFailureTests(Fixture):
    def final_of(self, client):
        request_id = self.send_stop(client)
        ack, final = self.result_frames(client, request_id, 2)
        self.assertEqual(ack["stop"], {"status": "stopping"})
        return final

    def test_a_controller_exception_becomes_an_error_final_and_the_server_keeps_serving(self):
        client = self.client()
        for exc in (RuntimeError("boom"), KeyError("x"), OSError(5, "io"), ValueError("v")):
            with self.subTest(exc=type(exc).__name__):
                self.controller.exception = exc
                final = self.final_of(client)
                self.assertIs(final["final"], True)
                self.assertEqual(final["stop"]["status"], "error", final)
                self.assertNotEqual(final["stop"]["status"], "exited")
                self.assertEqual(final["stop"].get("command_id"), CID)
                self.assertIn(type(exc).__name__, str(final["stop"]), "the user learns what failed")
                self.assertNotIn("Traceback", str(final["stop"]))
                self.assertIn("focus", client.snapshot())
        self.assertEqual(self.errors, [])

    def test_a_non_dict_outcome_never_kills_the_loop_and_never_reads_as_success(self):
        client = self.client()
        for outcome in (None, "exited", ["exited"], 7):
            with self.subTest(outcome=outcome):
                self.controller.outcome = outcome
                final = self.final_of(client)
                stop = final.get("stop")
                self.assertTrue(stop is None or not isinstance(stop, dict) or stop.get("status") != "exited", final)
                self.assertIn("focus", client.snapshot())
        self.assertEqual(self.errors, [])

    def test_an_outcome_that_cannot_be_encoded_ends_as_an_error_final_without_leaking_it(self):
        self.controller.outcome = {"status": "exited", "exit_code": 130, "secret": "LEAK-7731", "oops": object()}
        client = self.client()
        request_id = self.send_stop(client)
        ack, final = self.result_frames(client, request_id, 2)
        self.assertEqual(ack["stop"], {"status": "stopping"})
        self.assertIs(final["final"], True)
        self.assertEqual(final["command_id"], CID)
        self.assertEqual(final["stop"]["status"], "error", final)
        self.assertEqual(final["stop"]["reason"], "unencodable_outcome", final)
        self.assertNotEqual(final["stop"]["status"], "exited")
        self.assertNotIn("LEAK-7731", str(final), "the outcome's content leaked into the error frame")
        self.assertNotIn("exit_code", final["stop"])
        self.assertEqual(self.errors, [], "an unencodable outcome raised out of poll()")
        self.assertTrue(self.thread.is_alive(), "the serving thread died")
        self.assertIn("focus", client.snapshot())
        self.controller.outcome = dict(EXITED)  # and the slot was released: the next stop completes normally
        again = self.send_stop(client)
        _, final = self.result_frames(client, again, 2)
        self.assertEqual(final["stop"], EXITED)

    def test_a_controller_without_the_method_still_gives_a_final_that_is_not_a_success(self):
        class Bare(Controller):
            stop_command = None  # calling it raises TypeError, like a controller that lacks the method
        self.server.controller = Bare()
        client = self.client()
        request_id = self.send_stop(client)
        ack, final = self.result_frames(client, request_id, 2)
        self.assertNotEqual((final.get("stop") or {}).get("status"), "exited")
        self.assertIn("focus", client.snapshot())


class ConcurrencyAndDisconnectTests(Fixture):
    def test_the_in_flight_bound_refuses_with_queue_full_and_frees_slots_when_stops_end(self):
        self.controller.block = True
        client = self.client()
        ids = [self.send_stop(client, CID) for _ in range(MAX_STOPS_IN_FLIGHT)]
        for request_id in ids:
            (ack,) = self.result_frames(client, request_id, 1)
            self.assertTrue(ack["ok"])
        self.wait_for(lambda: len(self.controller.calls) == MAX_STOPS_IN_FLIGHT, "all stops to be running")
        extra = self.send_stop(client, OTHER)
        (refused,) = self.result_frames(client, extra, 1)
        self.assertEqual((refused["ok"], refused["reason"]), (False, "queue_full"), refused)
        self.assertNotIn("final", refused)
        time.sleep(0.2)
        self.assertEqual(len(self.controller.calls), MAX_STOPS_IN_FLIGHT, "the refused stop reached the controller")
        self.assertIn("focus", client.snapshot(), "the refusal left the loop running")
        self.controller.release_all()
        for request_id in ids:
            (final,) = self.result_frames(client, request_id, 1)
            self.assertIs(final["final"], True)
        again = self.send_stop(client, OTHER)
        ack, final = self.result_frames(client, again, 2)
        self.assertTrue(ack["ok"])
        self.assertIs(final["final"], True)

    def test_the_bound_is_sane(self):
        self.assertGreaterEqual(MAX_STOPS_IN_FLIGHT, 1)
        self.assertLessEqual(MAX_STOPS_IN_FLIGHT, 64)

    def test_a_client_that_leaves_misses_its_final_and_its_slot_is_still_released(self):
        self.controller.block = True
        leaver = self.client()
        for _ in range(MAX_STOPS_IN_FLIGHT):
            self.send_stop(leaver)
        self.wait_for(lambda: len(self.controller.calls) == MAX_STOPS_IN_FLIGHT, "the stops to run")
        leaver.close()
        self.wait_for(lambda: self.server.attached is None, "the server to notice the disconnect")
        self.controller.release_all()
        time.sleep(0.5)
        self.assertEqual(self.errors, [], "delivering to a closed connection raised")
        self.assertTrue(self.thread.is_alive())
        # every slot is free again: a new client can fill the bound once more
        self.controller.block = True
        survivor = self.client()
        ids = [self.send_stop(survivor) for _ in range(MAX_STOPS_IN_FLIGHT)]
        for request_id in ids:
            (ack,) = self.result_frames(survivor, request_id, 1)
            self.assertTrue(ack["ok"], f"a slot leaked after the client left: {ack}")
        self.controller.release_all()

    def test_a_new_client_never_receives_the_final_of_an_earlier_connections_stop(self):
        self.controller.block = True
        leaver = self.client()
        request_id = self.send_stop(leaver)
        self.result_frames(leaver, request_id, 1)
        self.wait_for(lambda: self.controller.calls, "the stop to start")
        leaver.close()
        self.wait_for(lambda: self.server.attached is None, "the detach")
        newcomer = self.client()
        self.controller.release_all()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            newcomer.pump(0.05)
        stray = [frame for frame in newcomer.pending if frame.header.get("final")]
        self.assertEqual(stray, [], "a stop's final reached a connection that did not ask for it")
        self.assertIn("focus", newcomer.snapshot())

    def test_a_detach_while_a_stop_runs_still_lets_the_stop_finish_in_the_controller(self):
        self.controller.block = True
        client = self.client()
        request_id = self.send_stop(client)
        self.result_frames(client, request_id, 1)
        self.wait_for(lambda: self.controller.calls, "the stop to start")
        self.assertTrue(client.detach()["detached"])
        self.controller.release_all()
        (final,) = self.result_frames(client, request_id, 1)  # still the same live connection: it gets its final
        self.assertIs(final["final"], True)

    def test_closing_the_server_while_a_stop_runs_does_not_hang_or_raise(self):
        self.controller.block = True
        client = self.client()
        request_id = self.send_stop(client)
        self.result_frames(client, request_id, 1)
        self.wait_for(lambda: self.controller.calls, "the stop to start")
        self.halt.set()
        self.thread.join(5)
        started = time.monotonic()
        self.server.close(flush_timeout=0.2)
        self.closed = True
        self.assertLess(time.monotonic() - started, 5.0, "close() waited for the stop thread")
        self.controller.release_all()
        time.sleep(0.3)  # the stop thread ends and queues its outcome for a server that no longer exists
        self.assertEqual(self.errors, [])

    def test_a_raw_socket_that_closes_right_after_sending_the_stop_cannot_hurt_the_server(self):
        self.controller.block = True
        client = self.client()
        sock = client.sock
        request_id = self.send_stop(client)
        sock.shutdown(socket.SHUT_RDWR)
        self.wait_for(lambda: self.controller.calls, "the stop to start")
        self.controller.release_all()
        time.sleep(0.3)
        self.assertEqual(self.errors, [])
        other = self.client()
        self.assertIn("focus", other.snapshot())
        self.assertIsNotNone(request_id)


if __name__ == "__main__":
    unittest.main()
