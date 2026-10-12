"""Real Unix count transport + durable scheduler admission, controlled ACKs only.

The peer is a protocol fixture, not an installed OMP metadata/accuracy proof.
"""
import json
from pathlib import Path
import select
import socket
from threading import Event, Thread
import unittest
from uuid import uuid4

from workbench.backend.summary_adapter import SDKSummaryAdapter
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer
from tests.backend import test_summary_scheduler as fixtures


class CountTransportAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.SchedulerTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.metadata["model"]["tokenizer"] = "claude-v3"
        token, session = str(uuid4()), str(uuid4())
        socket_path = Path(self.case.root) / "count.sock"
        self.server = G3BridgeServer(socket_path, {"manager": token, "worker": str(uuid4())})
        self.server.start()
        self.addCleanup(self.server.close)
        self.peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.peer.settimeout(2)
        self.peer.connect(str(socket_path))
        self.addCleanup(self.peer.close)
        self.reader = self.peer.makefile("rb")
        self.addCleanup(self.reader.close)
        self.send({"kind": "hello", "protocolVersion": 1, "role": "manager",
                   "ompSessionId": session, "generation": 4, "pid": 12345, "token": token})
        self.assertEqual(self.read()["kind"], "ready")
        self.case.runtime["expected_peer"] = (session, 4)
        self.case.adapter.count_fragments = SDKSummaryAdapter(self.server, timeout=1).count_fragments

    def send(self, frame):
        self.peer.sendall((json.dumps(frame) + "\n").encode())

    def read(self):
        return json.loads(self.reader.readline())

    def cycle(self):
        self.case.scheduler.tick()
        self.case.settle()
        self.case.now = 300
        self.case.scheduler.tick()
        return self.case.settle()

    def answer_count(self, response):
        errors, observed = [], []
        stopped = Event()

        def answer():
            try:
                while not stopped.is_set():
                    if not select.select([self.peer], [], [], .05)[0]:
                        continue
                    frame = self.read()
                    observed.append(frame)
                    self.assertEqual(frame["kind"], "summary_count")
                    session, generation = self.case.runtime["expected_peer"]
                    self.assertEqual(frame["expectedPeer"], {"sessionId": session, "generation": generation})
                    self.assertEqual(self.case.rows(), [], "count must precede durable reservation")
                    self.assertEqual(self.case.adapter.calls, [])
                    self.send({"kind": "api_ack", "requestId": frame["requestId"], **response})
            except Exception as error:
                errors.append(error)

        thread = Thread(target=answer, name="independent-count-peer")
        thread.start()
        self.addCleanup(lambda: thread.join(3))
        self.addCleanup(stopped.set)
        return thread, stopped, errors, observed

    def test_semantic_owner_rejection_reaches_scheduler_without_reservation_or_fallback(self):
        thread, stopped, errors, observed = self.answer_count({"status": "deferred", "sent": False,
                                                    "reason": "input_count_owner_mismatch"})
        outcome = self.cycle()
        stopped.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(observed), 1)
        self.assertEqual(outcome["state"], "deferred")
        self.assertEqual(outcome["reason"], "input_count_owner_mismatch")
        self.assertEqual(self.case.rows(), [])
        self.assertEqual(self.case.adapter.calls, [])

    def test_explicit_absent_observer_uses_honest_bytes_through_scheduler(self):
        thread, stopped, errors, observed = self.answer_count({"status": "count_unavailable", "sent": False,
                                                    "reason": "model_metadata_unavailable"})
        outcome = self.cycle()
        stopped.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(observed), 1)
        self.assertEqual(outcome["state"], "stored", outcome)
        prepared = self.case.adapter.calls[0]
        self.assertEqual(prepared["budget"]["estimator"], "utf8_bytes_upper_bound")
        self.assertEqual(prepared["budget"]["fallbackReason"], "count_port_unavailable")
        self.assertNotIn("countBinding", prepared)
        self.assertEqual(prepared["metadata"]["source"], "parent_session_readback")

    def test_settings_metadata_cannot_be_promoted_to_operating_evidence_by_count_port(self):
        self.case.metadata["source"] = "settings"
        outcome = self.cycle()
        self.assertEqual(outcome["state"], "deferred")
        self.assertEqual(outcome["reason"], "model_metadata_unavailable")
        self.assertEqual(self.case.rows(), [])
        self.assertEqual(self.case.adapter.calls, [])
        self.peer.settimeout(.05)
        with self.assertRaises(socket.timeout):
            self.peer.recv(1)


if __name__ == "__main__":
    unittest.main()
