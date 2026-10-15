"""DVL09 contract tests. Metadata here is a fixture, not an operating observation."""
from copy import deepcopy
from hashlib import sha256
import unittest
from uuid import uuid4

from workbench.backend.summary_adapter import (
    SDKSummaryAdapter, SourcePiece, SummaryDeferred, SummaryUnknown,
    prepare_summary, skill_bytes, validate_result,
)
from workbench.contracts.logs_v1 import LogContractError, canonical_json, summary_input_key


def contract_fixture(texts=("작업 목적과 검증 결과",)):
    command = str(uuid4())
    subject = {"schemaVersion": 1, "kind": "command", "id": command}
    source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
              "commandId": command}
    spans = []
    offset = 0
    for text in texts:
        data = text.encode()
        spans.append({"schemaVersion": 1, "source": deepcopy(source), "start": offset,
                      "end": offset + len(data), "digest": sha256(data).hexdigest()})
        offset += len(data)
    health = {"schemaVersion": 1, "dbUuid": str(uuid4()), "measuredAt": "2026-10-12T00:00:00Z",
              "stale": True, "mainBytes": 0, "walBytes": 0, "shmBytes": 0, "totalBytes": 0,
              "logicalLiveBytes": 0, "freelistBytes": 0, "subjectStats": [], "cleanupCandidates": None,
              "lastWrite": None, "lastLock": None, "lastMaintenance": None, "diskFreeBytes": None,
              "lastQuickCheck": None, "metadataOutsideBudget": True}
    digest = sha256(skill_bytes()).hexdigest()
    request = {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject, "revision": None,
               "authorizationRef": str(uuid4()), "authorizationEpoch": 1,
               "skillName": "important-log-summary", "skillDigest": digest, "sources": spans,
               "sourceGaps": [], "priorCumulativeEntryId": None, "dbHealth": health,
               "requestedAt": "2026-10-12T00:00:00Z"}
    rebind(request)
    metadata = {"source": "parent_session_readback", "epoch": "fixture-parent-epoch",
                "parentSessionId": str(uuid4()),
                "model": {"id": "fixture", "provider": "scripted", "api": "openai-completions",
                          "baseUrl": "http://127.0.0.1:1/v1", "contextWindow": 131072, "maxTokens": 8192},
                "thinkingLevel": "off", "serviceTierByFamily": {}, "existingContextTokens": 0,
                "outputCap": 8192, "reasoningTokens": 0, "visibleResultTokens": 8192,
                "outputMeaning": "reasoning_and_visible", "outputLimitEnforced": True,
                "outputLimitSource": "host_default", "estimator": "utf8_bytes_upper_bound"}
    metadata["childSelection"] = {"source": "configured_worker_default", "modelTableDigest": "a" * 64,
        "descriptorSource": "manager_model_registry_getAll_all", "serviceTierSource": "parent_session_readback",
        "model": {**deepcopy(metadata["model"]), "id": "worker"},
        "thinkingLevel": "high", "selector": "scripted/worker:high"}
    result = bound_result(request)
    return request, metadata, result


def rebind(request):
    request["inputKey"] = summary_input_key(request["subject"], request["revision"],
                                          request["sources"], request["sourceGaps"], request["skillDigest"])


def bound_result(request):
    return {"schemaVersion": 1, **{key: deepcopy(request[key]) for key in
            ("requestId", "inputKey", "skillDigest", "sources")}, "facts": ["검증한 사실"],
            "interpretations": ["해석"], "unknowns": ["실모델 품질 미확인"],
            "usage": {"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None}}


class IndependentSummaryPreparationTests(unittest.TestCase):
    def test_selection_is_permutation_independent_and_keeps_whole_ranges(self):
        texts = ("terminal" * 3000, "작업 시작 명세", "사용자 결정", "검증 보고")
        request, metadata, _ = contract_fixture(texts)
        pieces = [SourcePiece(span, text, kind, index) for index, (span, text, kind) in
                  enumerate(zip(request["sources"], texts, ("terminal", "start_spec", "decision", "report")))]
        before = deepcopy(request)
        a = prepare_summary(request, pieces, metadata=metadata, instructions="Existing task definition")
        b = prepare_summary(request, list(reversed(pieces)), metadata=metadata,
                            instructions="Existing task definition")
        self.assertEqual(canonical_json(a), canonical_json(b))
        self.assertEqual(a["request"]["sources"], request["sources"][1:])
        self.assertEqual(a["omitted"], [{"range": request["sources"][0], "reason": "input_budget",
                                         "estimatedBytes": len(texts[0].encode())}])
        self.assertEqual(request, before)
        self.assertEqual(a["budget"]["collectedBytes"], sum(len(t.encode()) for t in texts))
        self.assertEqual(a["budget"]["outputReserve"], 8192)
        self.assertLessEqual(a["budget"]["inputEstimate"], a["budget"]["inputLimit"])
        validate_result(a["request"], bound_result(a["request"]))
        with self.assertRaises(LogContractError):
            validate_result(a["request"], bound_result(request))

    def test_unknown_semantics_remain_information_but_missing_selection_and_cap_defer(self):
        request, metadata, _ = contract_fixture()
        piece = SourcePiece(request["sources"][0], "작업 목적과 검증 결과")
        cases = [(None, "model_metadata_unavailable"),
                 ({**metadata, "source": "profile"}, "model_metadata_unavailable"),
                 ({**metadata, "epoch": None}, "model_metadata_unavailable"),
                 ({**metadata, "childSelection": None}, "model_metadata_unavailable"),
                 ({**metadata, "outputCap": 8193}, "output_budget_unavailable")]
        for value, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(SummaryDeferred, reason):
                prepare_summary(request, [piece], metadata=value, instructions="task")
        for enforcement in (None, False):
            unknown = {**metadata, "existingContextTokens": None, "outputLimitEnforced": enforcement,
                       "outputMeaning": "unknown", "reasoningTokens": None, "visibleResultTokens": None}
            actual = prepare_summary(request, [piece], metadata=unknown, instructions="task")
            self.assertEqual(actual["metadata"], unknown)

    def test_utf8_group_quota_omission_keeps_every_original_range_and_digest(self):
        large="가\n"+"x"*24000
        request, metadata, _=contract_fixture((large,"확인한 결정"))
        original=request["sources"][0];raw=large.encode()
        first={**deepcopy(original),"end":1,"digest":sha256(raw[:1]).hexdigest()}
        second={**deepcopy(original),"start":1,"digest":sha256(raw[1:]).hexdigest()}
        decision=request["sources"][1]
        request["sources"]=[first,second,decision];rebind(request)
        before=deepcopy(request)
        pieces=[SourcePiece(first,large,"terminal",0,(first,second)),
                SourcePiece(decision,"확인한 결정","decision",1)]
        prepared=prepare_summary(request,pieces,metadata=metadata,instructions="task")
        self.assertEqual(request,before)
        self.assertEqual(prepared["request"]["sources"],[decision])
        self.assertEqual([value["range"] for value in prepared["omitted"]],[first,second])
        self.assertTrue(all(value["reason"]=="input_budget" for value in prepared["omitted"]))
        self.assertEqual(prepared["budget"]["inputLimit"], metadata["childSelection"]["model"]["contextWindow"])
        self.assertEqual(prepared["budget"]["resultByteLimit"],8192)
        self.assertLessEqual(prepared["budget"]["mandatoryWireBytes"],32768)

    def test_child_context_fit_reserves_output_without_reserving_parent_context(self):
        request, metadata, _ = contract_fixture(("short",))
        piece = SourcePiece(request["sources"][0], "short")
        metadata["existingContextTokens"] = None
        metadata["model"]["contextWindow"] = 4096
        low, high = 1, metadata["childSelection"]["model"]["contextWindow"]
        while low + 1 < high:
            middle = (low + high) // 2
            trial = deepcopy(metadata)
            trial["childSelection"]["model"]["contextWindow"] = middle
            try:
                prepare_summary(request, [piece], metadata=trial, instructions="task")
                high = middle
            except SummaryDeferred:
                low = middle
        accepted_metadata = deepcopy(metadata)
        accepted_metadata["childSelection"]["model"]["contextWindow"] = high
        accepted = prepare_summary(request, [piece], metadata=accepted_metadata, instructions="task")
        self.assertIsNone(accepted["metadata"]["existingContextTokens"])
        self.assertEqual(accepted["budget"]["inputLimit"], high)
        self.assertEqual(accepted["budget"]["previousOutputReserve"], 8192)
        self.assertEqual(accepted["budget"]["nextOutputReserve"], 8192)
        self.assertEqual(accepted["request"]["sources"], request["sources"])
        self.assertLessEqual(accepted["budget"]["mandatoryWireBytes"], 32768)
        accepted_metadata["childSelection"]["model"]["contextWindow"] = low
        with self.assertRaisesRegex(SummaryDeferred, "input_budget_unavailable"):
            prepare_summary(request, [piece], metadata=accepted_metadata, instructions="task")

    def test_collection_cap_is_separate_from_small_selected_input(self):
        texts = ("x" * 1_048_576, "decision")
        request, metadata, _ = contract_fixture(texts)
        with self.assertRaisesRegex(SummaryDeferred, "collection_budget_exceeded"):
            prepare_summary(request, [SourcePiece(s, t, "terminal") for s, t in
                                     zip(request["sources"], texts)], metadata=metadata, instructions="task")

    def test_wrong_same_size_bytes_duplicate_and_missing_source_are_rejected(self):
        request, metadata, _ = contract_fixture(("known",))
        piece = SourcePiece(request["sources"][0], "known")
        for pieces in ([], [SourcePiece(piece.range, "wrong")], [piece, piece]):
            with self.subTest(pieces=pieces), self.assertRaises(LogContractError):
                prepare_summary(request, pieces, metadata=metadata, instructions="task")

    def test_missing_raw_is_only_an_explicit_gap_and_cannot_move_to_another_work(self):
        request, metadata, _ = contract_fixture(("lost",))
        source = request["sources"][0]["source"]
        request["sources"] = []
        request["sourceGaps"] = [{"schemaVersion": 1, "source": source, "start": 0,
                                  "end": 4, "reason": "evicted"}]
        rebind(request)
        prepared = prepare_summary(request, [], metadata=metadata, instructions="task")
        self.assertEqual(prepared["request"]["sourceGaps"], request["sourceGaps"])
        self.assertEqual(prepared["selected"], [])
        bad = deepcopy(request)
        bad["sourceGaps"][0]["source"]["commandId"] = str(uuid4())
        rebind(bad)
        with self.assertRaises(LogContractError):
            prepare_summary(bad, [], metadata=metadata, instructions="task")

    def test_utf8_result_envelope_bound_and_host_binding(self):
        request, _, result = contract_fixture()
        self.assertEqual(validate_result(request, result)["usage"]["costUsd"], None)
        for key, value in (("requestId", str(uuid4())), ("inputKey", "f" * 64),
                           ("skillDigest", "f" * 64), ("sources", []), ("path", "/outside")):
            bad = deepcopy(result)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(LogContractError):
                validate_result(request, bad)
        bad = {**result, "facts": ["한" * 2700]}
        self.assertLess(len(bad["facts"][0]), 8192)
        self.assertGreater(len(canonical_json(bad)), 8192)
        with self.assertRaises(LogContractError):
            validate_result(request, bad)

    def test_authority_is_checked_before_bridge_and_sent_failures_are_not_retried(self):
        class Authority:
            def __init__(self, active): self.active = active
            def current(self): return self.active
        class Bridge:
            calls = []
            def request(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return self.response
        request, metadata, result = contract_fixture()
        bridge = Bridge()
        adapter = SDKSummaryAdapter(bridge)
        peer = (str(uuid4()), 5)
        with self.assertRaisesRegex(SummaryDeferred, "authority_unavailable"):
            adapter.dispatch_prepared({}, expected_peer=peer, authority_token=Authority(False), writer={})
        self.assertEqual(bridge.calls, [])
        bridge.response = {"status": "deferred", "sent": True, "reason": "ambiguous"}
        with self.assertRaises(SummaryUnknown):
            adapter.dispatch_prepared({}, expected_peer=peer, authority_token=Authority(True), writer={})
        self.assertEqual(len(bridge.calls), 1)
        bridge.response = {"status": "summary_result", "protected": False, "result": result}
        actual = adapter.dispatch_prepared({"request": request, "metadata": metadata}, expected_peer=peer,
                                           authority_token=Authority(True), writer={})
        self.assertEqual(actual, result)
        self.assertEqual(bridge.calls[-1][1]["expected_peer"], peer)
        self.assertEqual(bridge.calls[-1][0][0], "manager")


if __name__ == "__main__":
    unittest.main()
