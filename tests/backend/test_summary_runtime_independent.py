"""Bound runtime readback through real G3 transport; no native OMP/model boot.

Peer ACK metadata is a controlled protocol fixture, never operating evidence.
"""
from copy import deepcopy
from pathlib import Path
import select
import socket
import sys
from tempfile import TemporaryDirectory, gettempdir
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4
import json

from workbench.backend.launcher import LaunchPlan
from workbench.backend.paths import DataLayout
from workbench.backend.service import Backend
from workbench.backend.summary_adapter import SDKSummaryAdapter, SummaryDeferred
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer
from workbench.terminal.shell_g2.prototype import ShellChoice
from tests.backend import test_summary_scheduler as scheduler_fixtures


class RuntimePortTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="dvl14-runtime-", dir=gettempdir())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        token, session = str(uuid4()), str(uuid4())
        self.expected = (session, 4)
        self.server = G3BridgeServer(self.root / "runtime.sock", {"manager": token, "worker": str(uuid4())})
        self.server.start(); self.addCleanup(self.server.close)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(2); self.socket.connect(str(self.root / "runtime.sock"))
        self.addCleanup(self.socket.close)
        self.reader = self.socket.makefile("rb"); self.addCleanup(self.reader.close)
        self.send({"kind": "hello", "protocolVersion": 1, "role": "manager", "ompSessionId": session,
                   "generation": 4, "pid": 12345, "token": token})
        self.assertEqual(self.read()["kind"], "ready")
        self.adapter = SDKSummaryAdapter(self.server, timeout=1)
        self.metadata = {"source": "parent_session_readback", "parentSessionId": session, "epoch": "controlled-epoch",
            "model": {"id": "owned", "provider": "fixture", "api": "openai-completions",
                      "baseUrl": "http://127.0.0.1:1/v1", "contextWindow": 131072, "maxTokens": 8192},
            "childSelection": {"source": "configured_worker_default", "selector": "fixture/owned:low",
                "modelTableDigest": "0" * 64, "descriptorSource": "manager_model_registry_getAll_all",
                "thinkingLevel": "low", "serviceTierSource": "parent_session_readback",
                "model": {"id": "owned", "provider": "fixture", "api": "openai-completions",
                          "baseUrl": "http://127.0.0.1:1/v1", "contextWindow": 131072, "maxTokens": 8192}},
            "thinkingLevel": "off", "serviceTierByFamily": {}, "existingContextTokens": None, "outputCap": None,
            "reasoningTokens": None, "visibleResultTokens": None, "outputLimitEnforced": None,
            "outputMeaning": "unknown", "outputLimitSource": "unavailable", "estimator": "utf8_bytes_upper_bound",
            "status": "inconclusive", "unknowns": ["current_context_unavailable", "output_ceiling_enforcement_unavailable",
                                                      "reasoning_budget_semantics_unavailable"]}
        self.instructions = "  Existing bundled task bytes\n한글\n"

    def send(self, frame):
        self.socket.sendall((json.dumps(frame) + "\n").encode())

    def read(self):
        return json.loads(self.reader.readline())

    def read_reply(self, callable=None, **changes):
        errors, frames = [], []
        response = {"status": "summary_runtime", "sent": False, "protected": False,
                    "sessionId": self.expected[0], "generation": self.expected[1],
                    "runtime": {"metadata": self.metadata, "instructions": self.instructions}, **changes}
        def answer():
            try:
                frame = self.read(); frames.append(frame)
                self.assertEqual(frame["kind"], "summary_runtime")
                self.assertEqual(frame["expectedPeer"], {"sessionId": self.expected[0], "generation": 4})
                self.assertNotIn("prepared", frame)
                self.send({"kind": "api_ack", "requestId": frame["requestId"], **response})
            except Exception as error:
                errors.append(error)
        thread = Thread(target=answer, name="independent-runtime-peer")
        thread.start()
        try:
            return (callable or (lambda: self.adapter.read_runtime(expected_peer=self.expected)))()
        finally:
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(frames), 1)

    def test_bound_runtime_keeps_partial_metadata_and_preflight_has_no_reserve_or_dispatch(self):
        value = self.read_reply()
        self.assertEqual(value["instructions"], self.instructions)
        self.assertEqual(value["expected_peer"], self.expected)
        self.assertEqual(value["metadata"], self.metadata)
        self.metadata["model"]["id"] = "changed-after-reply"
        self.metadata["unknowns"].append("later")
        self.assertNotEqual(value["metadata"], self.metadata)
        case = scheduler_fixtures.SchedulerTests("runTest"); case.setUp()
        self.addCleanup(lambda: self.assertTrue(case.doCleanups()))
        case.runtime = {**value, "writer": {"python": sys.executable,
                                            "moduleRoot": str(Path(__file__).resolve().parents[2] / "src"),
                                            "dataRoot": str(case.root)}}
        case.scheduler.tick(); case.settle(); case.now = 300
        case.scheduler.tick(); outcome = case.settle()
        self.assertEqual(outcome["state"], "deferred")
        self.assertEqual(outcome["reason"], "output_budget_unavailable")
        self.assertEqual(case.rows(), [])
        self.assertEqual(case.adapter.calls, [])

    def test_transport_rejects_stale_expected_peer_without_a_runtime_frame(self):
        with self.assertRaisesRegex(SummaryDeferred, "authority_changed_before_submission"):
            self.adapter.read_runtime(expected_peer=(str(uuid4()), 4))
        self.assertEqual(select.select([self.socket], [], [], 0)[0], [])

    def test_reply_identity_and_integer_generation_are_validated(self):
        for changes in ({"sessionId": str(uuid4())}, {"generation": True}):
            with self.subTest(changes=changes), self.assertRaisesRegex(SummaryDeferred, "model_metadata_unavailable"):
                self.read_reply(**changes)

    def backend(self, supplier=None):
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/owned/not-launched-omp", "fixture",
                          "/owned/not-loaded-bridge.ts", ())
        backend = Backend(DataLayout(self.root), plan, project_dir=str(self.root),
                          environment={"HOME": str(self.root)}, boot_source=lambda: "owned-fixture-boot",
                          summary_runtime=supplier)
        backend.bridge = self.server
        backend.summary_adapter = self.adapter
        return backend

    def test_backend_default_none_reads_the_current_bound_manager_port(self):
        backend = self.backend()
        value = self.read_reply(backend._summary_runtime)
        self.assertEqual(value["expected_peer"], self.expected)
        self.assertEqual(value["metadata"]["outputCap"], None)

    def test_explicit_supplier_none_noncallable_and_exception_have_no_live_fallback(self):
        backend = self.backend(lambda: None)
        peer = Mock(side_effect=AssertionError("explicit supplier reached live bridge"))
        read = Mock(side_effect=AssertionError("explicit supplier reached live adapter"))
        backend.bridge = SimpleNamespace(peer=peer)
        backend.summary_adapter = SimpleNamespace(read_runtime=read)
        self.assertIsNone(backend._summary_runtime())
        backend.summary_runtime = {"metadata": "not a callable supplier"}
        self.assertIsNone(backend._summary_runtime())
        def failed():
            raise RuntimeError("owned supplier failure")
        backend.summary_runtime = failed
        with self.assertRaisesRegex(RuntimeError, "owned supplier failure"):
            backend._summary_runtime()
        peer.assert_not_called(); read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
