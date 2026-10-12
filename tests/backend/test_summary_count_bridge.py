"""Count-only bridge admission; synthetic proofs do not establish native accuracy."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from workbench.backend.summary_adapter import SDKSummaryAdapter, SummaryDeferred, prepare_summary
from workbench.ipc.bridge_g3.mailbox import BridgeBoundMismatch
from tests.backend import test_summary_scheduler as fixtures
from tests.backend.test_summary_native_budget_independent import native_case, controlled_count, DEFINITION


class CountAdapterTests(unittest.TestCase):
    def setUp(self):
        self.query = {}
        request, metadata, pieces = native_case()
        def capture(query):
            self.query = deepcopy(query)
            return controlled_count(query)
        prepare_summary(request, pieces, metadata=metadata, instructions=DEFINITION, count_fragments=capture)
        self.peer = ("channel-parent", 3)
        self.token = SimpleNamespace(current=lambda: True, claim=lambda: True)

    def test_existing_bound_request_port_and_detached_proof(self):
        calls, proof = [], controlled_count(self.query)
        def request(role, frame, **ports):
            calls.append((role, frame, ports))
            return {"status": "summary_count", "proof": proof}
        adapter = SDKSummaryAdapter(SimpleNamespace(request=request), timeout=2)
        actual = adapter.count_fragments(self.query, expected_peer=self.peer, authority_token=self.token)
        role, frame, ports = calls[0]
        self.assertEqual(role, "manager")
        self.assertEqual(frame, {"kind": "summary_count", "query": self.query,
                                 "expectedPeer": {"sessionId": self.peer[0], "generation": self.peer[1]}})
        self.assertEqual(ports, {"timeout": 2, "expected_peer": self.peer, "authority_token": self.token})
        self.assertEqual(actual, proof)
        proof["tokens"][0] += 1
        self.assertNotEqual(actual, proof)

    def test_authority_changed_peer_and_rejected_count_stop_before_dispatch(self):
        calls = []
        adapter = SDKSummaryAdapter(SimpleNamespace(request=lambda *a, **k: calls.append(a)))
        self.token.current = lambda: False
        with self.assertRaisesRegex(SummaryDeferred, "authority_unavailable"):
            adapter.count_fragments(self.query, expected_peer=self.peer, authority_token=self.token)
        self.assertEqual(calls, [])
        self.token.current = lambda: True
        def changed(*a, **k): raise BridgeBoundMismatch("peer changed")
        adapter.bridge.request = changed
        with self.assertRaisesRegex(SummaryDeferred, "authority_changed_before_submission"):
            adapter.count_fragments(self.query, expected_peer=self.peer, authority_token=self.token)
        for response in ({"status": "deferred", "reason": "paused"},
                         {"status": "summary_count", "proof": {"exact": True}},
                         {"status": "unknown"}):
            adapter.bridge.request = lambda *a, **k: response
            with self.assertRaises(SummaryDeferred):
                adapter.count_fragments(self.query, expected_peer=self.peer, authority_token=self.token)

    def test_unavailable_observer_is_explicit_port_fallback(self):
        bridge = SimpleNamespace(request=lambda *a, **k: {"status": "count_unavailable",
            "sent": False, "reason": "model_metadata_unavailable"})
        self.assertIsNone(SDKSummaryAdapter(bridge).count_fragments(
            self.query, expected_peer=self.peer, authority_token=self.token))

    def test_wire_impossible_range_is_omitted_without_large_count_ipc(self):
        request, metadata, pieces = native_case(("x" * 900000, "confirmed decision"))
        queries = []
        def count(query):
            queries.append(query)
            return controlled_count(query)
        actual = prepare_summary(request, pieces, metadata=metadata, instructions=DEFINITION, count_fragments=count)
        self.assertEqual(actual["budget"]["selectedBytes"], len("confirmed decision"))
        self.assertEqual(len(actual["omitted"]), 1)
        self.assertTrue(queries)
        self.assertTrue(all(len(q["fragments"][1].encode()) < 32768 for q in queries))


class CountSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.SchedulerTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.metadata["model"]["tokenizer"] = "claude-v3"

    def cycle(self):
        self.case.scheduler.tick(); self.case.settle()
        self.case.now += 300
        self.case.scheduler.tick()
        return self.case.settle()

    def test_injected_adapter_count_precedes_journal_reserve_with_current_authority(self):
        calls = []
        def count(query, **ports):
            self.assertEqual(self.case.rows(), [])
            self.assertEqual(ports["expected_peer"], self.case.runtime["expected_peer"])
            self.assertIs(ports["authority_token"].current(), True)
            self.assertIs(ports["authority_token"].claim(), True)
            calls.append(query)
            return controlled_count(query)
        self.case.adapter.count_fragments = count
        self.assertEqual(self.cycle()["state"], "stored")
        self.assertTrue(calls)
        self.assertEqual(self.case.adapter.calls[0]["budget"]["estimator"], "native_known_family_strict")

    def test_explicit_runtime_port_retained_and_fake_adapter_without_port_uses_bytes(self):
        self.case.adapter.count_fragments = lambda *a, **k: self.fail("explicit port replaced")
        self.case.runtime["count_fragments"] = controlled_count
        self.assertEqual(self.cycle()["state"], "stored")
        self.assertEqual(self.case.adapter.calls[0]["budget"]["estimator"], "native_known_family_strict")

    def test_pause_or_priority_after_count_stops_before_reserve_and_model_dispatch(self):
        for flag in ("paused", "priority", "revoked"):
            with self.subTest(flag=flag):
                setattr(self.case, flag, False)
                def count(query, **ports):
                    setattr(self.case, flag, True)
                    return controlled_count(query)
                self.case.adapter.count_fragments = count
                self.assertEqual(self.cycle()["state"], "deferred")
                self.assertEqual(self.case.rows(), [])
                self.assertEqual(self.case.adapter.calls, [])
                setattr(self.case, flag, False)

    def test_no_adapter_count_port_preserves_existing_byte_behavior(self):
        self.assertEqual(self.cycle()["state"], "stored")
        self.assertEqual(self.case.adapter.calls[0]["budget"]["estimator"], "utf8_bytes_upper_bound")

    def test_bound_peer_is_snapshotted_for_count_and_dispatch(self):
        original = self.case.runtime["expected_peer"]
        dispatch = self.case.adapter.dispatch_outcome
        def count(query, **ports):
            self.assertEqual(ports["expected_peer"], original)
            self.case.runtime["expected_peer"] = ("changed-runtime-peer", 99)
            return controlled_count(query)
        def checked_dispatch(prepared, **ports):
            self.assertEqual(ports["expected_peer"], original)
            return dispatch(prepared, **ports)
        self.case.adapter.count_fragments = count
        self.case.adapter.dispatch_outcome = checked_dispatch
        self.assertEqual(self.cycle()["state"], "stored")

    def test_count_proof_mismatch_defers_before_reserve_or_model_dispatch(self):
        self.case.adapter.count_fragments = lambda query, **ports: {
            **controlled_count(query), "ownerDigest": "0" * 64}
        status = self.cycle()
        self.assertEqual(status["state"], "deferred")
        self.assertEqual(status["reason"], "input_count_binding_mismatch")
        self.assertEqual(self.case.rows(), [])
        self.assertEqual(self.case.adapter.calls, [])


if __name__ == "__main__":
    unittest.main()
