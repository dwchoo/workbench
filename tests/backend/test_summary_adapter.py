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
from workbench.contracts.logs_v1 import LogContractError, summary_input_key
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
