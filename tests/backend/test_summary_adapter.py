from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from workbench.backend.summary_adapter import (
    SKILL_NAME, SDKSummaryAdapter, SourcePiece, SummaryDeferred, SummaryUnknown,
    prepare_summary, skill_bytes, validate_result,
)
from workbench.contracts.logs_v1 import LogContractError, canonical_json, summary_input_key
from workbench.storage.important_log import ImportantLogCatalog


def request_fixture(directory: Path, texts: list[str]):
    catalog = ImportantLogCatalog(directory / "logs", create=True)
    subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
    source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
              "commandId": subject["id"]}
    cursor = 0
    spans = []
    for text in texts:
        size = len(text.encode())
        spans.append({"schemaVersion": 1, "source": source, "start": cursor,
                      "end": cursor + size, "digest": sha256(text.encode()).hexdigest()})
        cursor += size
    digest = sha256(skill_bytes()).hexdigest()
    request = {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject,
               "revision": None, "authorizationRef": str(uuid4()), "authorizationEpoch": 1,
               "skillName": SKILL_NAME, "skillDigest": digest, "sources": spans,
               "sourceGaps": [], "priorCumulativeEntryId": None,
               "dbHealth": catalog.health_snapshot()["active"], "requestedAt": "2026-10-12T00:00:00Z"}
    request["inputKey"] = summary_input_key(subject, None, spans, [], digest)
    metadata = {"source": "parent_session_readback", "epoch": "observed-1",
                "parentSessionId": str(uuid4()),
                "model": {"provider": "scripted", "id": "test", "api": "openai-completions",
                          "baseUrl": "http://127.0.0.1:1/v1", "contextWindow": 131072, "maxTokens": 8192},
                "thinkingLevel": "off", "serviceTierByFamily": {}, "existingContextTokens": 0,
                "outputCap": 8192, "reasoningTokens": 0, "visibleResultTokens": 8192,
                "outputMeaning": "reasoning_and_visible", "outputLimitEnforced": True,
                "outputLimitSource": "scripted-provider-test-contract", "estimator": "utf8_bytes_upper_bound"}
    return request, metadata


class SummaryAdapterTests(unittest.TestCase):
    def test_omission_is_explicit_and_request_binds_only_selected_complete_ranges(self):
        with TemporaryDirectory() as name:
            texts = ["x" * 7000, "승인된 변경과 시험 성공"]
            request, metadata = request_fixture(Path(name), texts)
            pieces = [SourcePiece(request["sources"][0], texts[0]),
                      SourcePiece(request["sources"][1], texts[1], "report")]
            prepared = prepare_summary(request, pieces, metadata=metadata, instructions="Existing task prompt")
            self.assertEqual(prepared["request"]["sources"], [request["sources"][1]])
            self.assertEqual(prepared["omitted"][0]["range"], request["sources"][0])
            self.assertNotEqual(prepared["request"]["inputKey"], request["inputKey"])
            self.assertEqual(request["sources"], [p.range for p in pieces])
            self.assertLessEqual(prepared["budget"]["inputEstimate"], 8192)

    def test_utf8_group_uses_original_ranges_once_and_is_omitted_whole(self):
        with TemporaryDirectory() as name:
            for text, omitted in (("가\n", False), ("가" + "x" * 6997, True)):
                with self.subTest(omitted=omitted):
                    request, metadata = request_fixture(Path(name) / str(omitted), [text, "report"])
                    original = request["sources"][0]
                    raw = text.encode()
                    ranges = [{**original, "end": 1, "digest": sha256(raw[:1]).hexdigest()},
                              {**original, "start": 1, "digest": sha256(raw[1:]).hexdigest()}]
                    request["sources"] = [*ranges, request["sources"][1]]
                    request["inputKey"] = summary_input_key(request["subject"], None,
                        request["sources"], [], request["skillDigest"])
                    grouped = SourcePiece(ranges[0], text, "terminal", 2, tuple(ranges))
                    prepared = prepare_summary(request, [grouped, SourcePiece(request["sources"][-1], "report", "report")],
                                               metadata=metadata, instructions="task")
                    if omitted:
                        self.assertEqual([r["range"] for r in prepared["omitted"]], ranges)
                        self.assertEqual(prepared["request"]["sources"], request["sources"][-1:])
                    else:
                        self.assertEqual(prepared["selected"][-1], {"ranges": ranges, "text": text})
                        self.assertEqual(prepared["request"]["sources"], [request["sources"][-1], *ranges])
                    bad = deepcopy(ranges); bad[1]["digest"] = "0" * 64
                    with self.assertRaises(LogContractError):
                        prepare_summary({**request, "sources": [*bad, request["sources"][-1]],
                            "inputKey": summary_input_key(request["subject"], None, [*bad, request["sources"][-1]], [], request["skillDigest"])},
                            [SourcePiece(bad[0], text, "terminal", 2, tuple(bad)),
                             SourcePiece(request["sources"][-1], "report", "report")],
                            metadata=metadata, instructions="task")

    def test_bad_source_bytes_and_missing_piece_are_rejected(self):
        with TemporaryDirectory() as name:
            request, metadata = request_fixture(Path(name), ["known"])
            for pieces in ([], [SourcePiece(request["sources"][0], "wrong")]):
                with self.assertRaises(LogContractError):
                    prepare_summary(request, pieces, metadata=metadata, instructions="task")

    def test_unknown_parent_or_output_semantics_prevents_dispatch(self):
        with TemporaryDirectory() as name:
            request, metadata = request_fixture(Path(name), ["new fact"])
            piece = SourcePiece(request["sources"][0], "new fact")
            for update, reason in (({"source": "profile"}, "model_metadata_unavailable"),
                                   ({"outputMeaning": "unknown"}, "output_budget_unavailable"),
                                   ({"outputLimitEnforced": False}, "output_budget_unavailable")):
                with self.subTest(update=update), self.assertRaisesRegex(SummaryDeferred, reason):
                    prepare_summary(request, [piece], metadata={**metadata, **update}, instructions="task")

    def test_mandatory_continuation_reserves_two_full_outputs_without_double_counting_args(self):
        with TemporaryDirectory() as name:
            request, metadata = request_fixture(Path(name), ["new fact"])
            piece = SourcePiece(request["sources"][0], "new fact")
            prepared = prepare_summary(request, [piece], metadata=metadata, instructions="task")
            budget = prepared["budget"]
            self.assertEqual(budget["mandatoryArgumentsBytes"], 8192)
            self.assertEqual(budget["previousOutputReserve"], 8192)
            self.assertEqual(budget["nextOutputReserve"], 8192)
            self.assertEqual(budget["mandatoryWireBytes"], budget["mandatoryContinuationEstimate"] + 8192)
            self.assertEqual(budget["dynamicExtrasReserve"], 32768 - budget["mandatoryWireBytes"])
            # LG-D26: initial+one cap can fit while the mandatory next turn cannot.
            metadata["existingContextTokens"] = 116000
            self.assertLessEqual(budget["inputEstimate"] + 116000 + 8192, 131072)
            with self.assertRaisesRegex(SummaryDeferred, "input_budget_unavailable"):
                prepare_summary(request, [piece], metadata=metadata, instructions="task")

    def test_selection_accounts_for_next_turn_context_before_sending(self):
        with TemporaryDirectory() as name:
            request, metadata = request_fixture(Path(name), ["x" * 1000, "short report"])
            metadata["existingContextTokens"] = 107000
            prepared = prepare_summary(request, [SourcePiece(request["sources"][0], "x" * 1000),
                SourcePiece(request["sources"][1], "short report", "report")],
                metadata=metadata, instructions="task")
            self.assertEqual(prepared["request"]["sources"], [request["sources"][1]])
            self.assertEqual(prepared["omitted"][0]["range"], request["sources"][0])
            self.assertLessEqual(prepared["budget"]["inputEstimate"] + prepared["budget"]["fixedContinuationEstimate"]
                + metadata["existingContextTokens"] + 2 * metadata["outputCap"], metadata["model"]["contextWindow"])

    @staticmethod
    def contract_counter(query):
        # Contract stand-in only: actual native values are checked by the owned
        # installed-SDK conformance probe, not inferred from this unit fixture.
        return {"version": query["version"], "ownerDigest": query["ownerDigest"],
            "fragmentDigests": list(query["fragmentDigests"]), "encoding": "ClaudeV3",
            "tokens": [100, 100], "exact": True, "estimator": "native_known_family_strict"}

    def test_absent_and_unavailable_ports_keep_byte_selection_and_parent_metadata_truth(self):
        with TemporaryDirectory() as name:
            request, metadata = request_fixture(Path(name), ["x" * 7000, "report"])
            pieces = [SourcePiece(request["sources"][0], "x" * 7000),
                      SourcePiece(request["sources"][1], "report", "report")]
            baseline = prepare_summary(request, pieces, metadata=metadata, instructions="task")
            metadata["estimator"] = "native_known_family_strict"
            metadata["model"]["tokenizer"] = "claude-v3"
            original = deepcopy(metadata)
            for port in (None, lambda q: None, lambda q: {**self.contract_counter(q), "exact": False}):
                value = prepare_summary(request, pieces, metadata=metadata, instructions="task", count_fragments=port)
                self.assertEqual(value["selected"], baseline["selected"])
                self.assertEqual(value["omitted"], baseline["omitted"])
                self.assertEqual(value["budget"]["inputEstimate"], baseline["budget"]["inputEstimate"])
                self.assertEqual(value["budget"]["estimator"], "utf8_bytes_upper_bound")
                self.assertTrue(value["budget"]["fallbackReason"])
                self.assertNotIn("countBinding", value)
                self.assertEqual(value["metadata"], original)
                self.assertEqual(metadata, original)

    def test_native_port_binds_exact_ordered_shared_fragments_and_keeps_wire_bytes(self):
        with TemporaryDirectory() as name:
            request, metadata = request_fixture(Path(name), ["x" * 7000])
            metadata["model"]["tokenizer"] = "claude-v3"
            seen = []
            def port(query):
                seen.append(deepcopy(query))
                response = self.contract_counter(query)
                query["owner"]["model"]["tokenizer"] = "mutated port input"
                return response
            value = prepare_summary(request, [SourcePiece(request["sources"][0], "x" * 7000)],
                                    metadata=metadata, instructions="task", count_fragments=port)
            self.assertEqual(value["budget"]["inputEstimate"], 2248)
            self.assertEqual(value["countBinding"]["tokens"], [100, 100])
            self.assertEqual(value["metadata"]["model"]["tokenizer"], "claude-v3")
            self.assertEqual(seen[-1]["fragments"][0], "task\n\n" + skill_bytes().decode())
            payload = {key: value[key] for key in ("request", "selected", "omitted", "priorCumulative", "resultByteLimit")}
            self.assertEqual(seen[-1]["fragments"][1], canonical_json(payload).decode())
            self.assertEqual(value["countBinding"]["fragmentDigests"],
                             [sha256(text.encode()).hexdigest() for text in seen[-1]["fragments"]])
            self.assertGreater(value["budget"]["wireInputEstimateBytes"], value["budget"]["inputEstimate"])
            self.assertEqual(value["budget"]["mandatoryWireBytes"], value["budget"]["wireInputEstimateBytes"]
                + value["budget"]["mandatoryArgumentsBytes"] + value["budget"]["fixedContinuationEstimate"] + 8192)
            self.assertEqual(value["budget"]["previousOutputReserve"], 8192)
            self.assertEqual(value["budget"]["nextOutputReserve"], 8192)

    def test_bad_native_provenance_or_owner_fragment_proof_defers_before_dispatch(self):
        with TemporaryDirectory() as name:
            request, metadata = request_fixture(Path(name), ["fact"])
            metadata["model"]["tokenizer"] = "claude-v3"
            for change in ({"ownerDigest": "wrong"}, {"fragmentDigests": []}, {"encoding": "o200k_base"},
                           {"tokens": [-1, 0]}, {"tokens": [True, 0]}, {"estimator": "approximate"}):
                with self.subTest(change=change), self.assertRaisesRegex(SummaryDeferred, "input_count_binding_mismatch"):
                    prepare_summary(request, [SourcePiece(request["sources"][0], "fact")],
                        metadata=metadata, instructions="task", count_fragments=lambda q: {**self.contract_counter(q), **change})
            metadata["model"]["tokenizer"] = "unknown-family"
            value = prepare_summary(request, [SourcePiece(request["sources"][0], "fact")],
                metadata=metadata, instructions="task", count_fragments=lambda q: self.fail("unknown family must not call port"))
            self.assertEqual(value["budget"]["estimator"], "utf8_bytes_upper_bound")

    def test_native_selection_still_enforces_wire_and_two_full_output_reserves(self):
        with TemporaryDirectory() as name:
            request, metadata = request_fixture(Path(name), ["x" * 22000, "report"])
            metadata["model"]["tokenizer"] = "claude-v3"
            pieces = [SourcePiece(request["sources"][0], "x" * 22000),
                      SourcePiece(request["sources"][1], "report", "report")]
            value = prepare_summary(request, pieces, metadata=metadata, instructions="task", count_fragments=self.contract_counter)
            self.assertEqual(value["request"]["sources"], [request["sources"][1]])
            self.assertEqual(value["omitted"][0]["range"], request["sources"][0])
            metadata["existingContextTokens"] = 116000
            with self.assertRaisesRegex(SummaryDeferred, "input_budget_unavailable"):
                prepare_summary(request, pieces, metadata=metadata, instructions="task", count_fragments=self.contract_counter)

    def test_result_cannot_move_to_another_work_request_or_source(self):
        with TemporaryDirectory() as name:
            request, _ = request_fixture(Path(name), ["fact"])
            result = {"schemaVersion": 1, "requestId": request["requestId"],
                      "inputKey": request["inputKey"], "skillDigest": request["skillDigest"],
                      "sources": request["sources"], "facts": ["fact"], "interpretations": [], "unknowns": [],
                      "usage": {"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None}}
            self.assertEqual(validate_result(request, result), result)
            for key, value in (("requestId", str(uuid4())), ("sources", []), ("skillDigest", "0" * 64)):
                bad = deepcopy(result)
                bad[key] = value
                with self.subTest(key=key), self.assertRaises(LogContractError):
                    validate_result(request, bad)

    def test_bridge_distinguishes_not_sent_from_unknown_without_retry(self):
        class Authority:
            def current(self): return True
        class Bridge:
            calls = 0
            def request(self, *args, **kwargs):
                self.calls += 1
                return self.response
        bridge = Bridge()
        adapter = SDKSummaryAdapter(bridge)
        for response, error in (({"status": "deferred", "sent": False, "reason": "paused"}, SummaryDeferred),
                                ({"status": "unknown", "sent": True}, SummaryUnknown)):
            bridge.response = response
            with self.assertRaises(error):
                adapter.dispatch_prepared({}, expected_peer=(str(uuid4()), 1), authority_token=Authority(), writer={})
        self.assertEqual(bridge.calls, 2)

    def test_product_recheck_cannot_accept_an_arbitrary_fact_envelope(self):
        from workbench.backend.summary_compat_adapter import prepare_q01_context
        for value in ({"observations": [], "unknowns": []}, [{"id": "Q01-tools", "passed": True}], None):
            with self.assertRaisesRegex(RuntimeError, "product_runtime_transport_unavailable"):
                prepare_q01_context({"product_runtime": value})


if __name__ == "__main__":
    unittest.main()
