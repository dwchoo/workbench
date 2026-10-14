"""LG-D30 independent host boundaries; no OMP, provider or user DB access."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from workbench.backend.launcher import omp_environment
from workbench.backend.summary_adapter import (
    SourcePiece, SummaryDeferred, prepare_summary, skill_bytes, validate_result,
)
from workbench.contracts.logs_v1 import LogContractError, canonical_json, summary_input_key


PARENT = dict(id="parent", provider="parent-provider", api="parent-api",
              baseUrl="https://parent.invalid", contextWindow=4096,
              maxTokens=16384, tokenizer="claude-v5")
CHILD = dict(id="light", provider="child-provider", api="child-api",
             baseUrl="https://child.invalid", contextWindow=24000,
             maxTokens=1024, tokenizer="qwen3")
STAMP = "2026-10-14T00:00:00Z"
UID = "11111111-1111-4111-8111-111111111111"


def metadata():
    return dict(source="parent_session_readback", epoch="fixture-epoch", parentSessionId="parent-session",
                model=deepcopy(PARENT), thinkingLevel="high", serviceTierByFamily={"fixture": "standard"},
                childSelection=dict(source="configured_worker_default", selector="child-provider/light:low",
                                    modelTableDigest="a" * 64, model=deepcopy(CHILD), thinkingLevel="low",
                                    descriptorSource="manager_model_registry_getAll_all",
                                    serviceTierSource="parent_session_readback"),
                existingContextTokens=None, outputCap=1024, reasoningTokens=None,
                visibleResultTokens=None, outputMeaning="unknown", outputLimitEnforced=None,
                outputLimitSource="host_default", estimator="utf8_bytes_upper_bound",
                status="inconclusive", unknowns=["current_context_unavailable",
                  "output_ceiling_enforcement_unavailable", "reasoning_budget_semantics_unavailable"])


def request_and_piece(text="independent source"):
    subject = dict(schemaVersion=1, kind="command", id=UID)
    span = dict(schemaVersion=1, source=dict(schemaVersion=1, sourceKind="terminal", collectorEpoch=1,
                                           commandId=UID), start=0, end=len(text.encode()),
                digest=sha256(text.encode()).hexdigest())
    health = dict(schemaVersion=1, dbUuid=UID, measuredAt=STAMP, stale=False,
                  mainBytes=0, walBytes=0, shmBytes=0, totalBytes=0, logicalLiveBytes=0, freelistBytes=0,
                  subjectStats=[], cleanupCandidates=None, lastWrite=None, lastLock=None,
                  lastMaintenance=None, diskFreeBytes=None, lastQuickCheck=None, metadataOutsideBudget=True)
    digest = sha256(skill_bytes()).hexdigest()
    request = dict(schemaVersion=1, requestId=UID, subject=subject, revision=None,
                   authorizationRef=UID, authorizationEpoch=1, skillName="important-log-summary",
                   skillDigest=digest, sources=[span], sourceGaps=[], priorCumulativeEntryId=None,
                   inputKey=summary_input_key(subject, None, [span], [], digest), dbHealth=health,
                   requestedAt=STAMP)
    return request, SourcePiece(span, text)


def counted(query, tokens=(5500, 3500)):
    return dict(version=query["version"], ownerDigest=query["ownerDigest"],
                fragmentDigests=query["fragmentDigests"], encoding="Qwen3", exact=True,
                estimator="native_known_family_strict", tokens=list(tokens))


class SummaryModelBudgetIndependentTests(unittest.TestCase):
    def test_startup_snapshot_selects_worker_without_mutating_parent_table(self):
        table = {"manager": {"models": {"default": "parent-provider/parent:high"}},
                 "worker": {"models": {"default": "child-provider/light:low"}}}
        original = deepcopy(table)
        args = dict(base={"PATH": "/fixture/bin"}, plan=SimpleNamespace(omp_version="fixture"),
                    token="fixture-token", bridge_socket=Path("/fixture/socket"), models=table)
        first = json.loads(omp_environment(role="manager", **args)["WORKBENCH_G3_SUMMARY_SELECTION"])
        self.assertEqual(first["selector"], "child-provider/light:low")
        self.assertEqual(first["source"], "configured_worker_default")
        self.assertRegex(first["modelTableDigest"], r"^[0-9a-f]{64}$")
        self.assertEqual(table, original)
        self.assertNotIn("WORKBENCH_G3_SUMMARY_SELECTION", omp_environment(role="worker", **args))
        table["worker"]["models"]["default"] = "child-provider/light:max"
        second = json.loads(omp_environment(role="manager", **args)["WORKBENCH_G3_SUMMARY_SELECTION"])
        self.assertEqual(second["selector"], "child-provider/light:max")
        self.assertNotEqual(first["modelTableDigest"], second["modelTableDigest"])
        self.assertEqual(table["manager"], original["manager"])

    def test_child_context_and_native_count_allow_removed_cost_quota_with_unknowns(self):
        request, piece = request_and_piece()
        observed = []
        def counter(query):
            observed.append(deepcopy(query))
            return counted(query)
        meta = metadata()
        original = deepcopy(meta)
        prepared = prepare_summary(request, [piece], metadata=meta, instructions="DB-only fixture task",
                                   count_fragments=counter)
        self.assertEqual(prepared["request"]["sources"], [piece.range])
        self.assertEqual(prepared["omitted"], [])
        self.assertGreater(prepared["budget"]["inputEstimate"], 8192)
        self.assertEqual(prepared["budget"]["inputLimit"], 24000)
        self.assertEqual(prepared["budget"]["outputReserve"], 1024)
        self.assertEqual(prepared["countBinding"]["encoding"], "Qwen3")
        self.assertTrue(observed)
        for query in observed:
            self.assertEqual(query["owner"]["model"], CHILD)
            self.assertEqual(query["owner"]["parentSessionId"], "parent-session")
        self.assertEqual(prepared["metadata"], original)
        self.assertIsNone(prepared["metadata"]["existingContextTokens"])
        self.assertIsNone(prepared["metadata"]["outputLimitEnforced"])
        self.assertIsNone(prepared["metadata"]["reasoningTokens"])
        self.assertEqual(prepared["metadata"]["outputMeaning"], "unknown")
        self.assertEqual(meta, original)

    def test_missing_child_and_exceeded_child_context_never_fall_back_to_parent(self):
        request, piece = request_and_piece()
        for edit in (lambda m: m.pop("childSelection"),
                     lambda m: m["childSelection"]["model"].pop("contextWindow")):
            meta = metadata()
            edit(meta)
            with self.subTest(edit=edit), self.assertRaisesRegex(SummaryDeferred, "model_metadata_unavailable"):
                prepare_summary(request, [piece], metadata=meta, instructions="fixture")
        meta = metadata()
        meta["model"]["contextWindow"] = 1000000
        meta["childSelection"]["model"]["contextWindow"] = 6000
        with self.assertRaisesRegex(SummaryDeferred, "input_budget_unavailable"):
            prepare_summary(request, [piece], metadata=meta, instructions="fixture", count_fragments=counted)

    def test_wire_and_native_owner_binding_remain_independent_of_token_budget(self):
        request, piece = request_and_piece("x" * 40000)
        with self.assertRaisesRegex(SummaryDeferred, "input_budget_unavailable"):
            prepare_summary(request, [piece], metadata=metadata(), instructions="fixture",
                            count_fragments=lambda q: counted(q, (1, 1)))
        request, piece = request_and_piece()
        def parent_bound(query):
            answer = counted(query)
            answer["ownerDigest"] = sha256(canonical_json({**query["owner"], "model": PARENT})).hexdigest()
            return answer
        with self.assertRaisesRegex(SummaryDeferred, "input_count_binding_mismatch"):
            prepare_summary(request, [piece], metadata=metadata(), instructions="fixture",
                            count_fragments=parent_bound)

    def test_db_result_byte_cap_and_request_binding_are_still_required(self):
        request, _ = request_and_piece()
        result = dict(schemaVersion=1, requestId=request["requestId"], inputKey=request["inputKey"],
                      skillDigest=request["skillDigest"], sources=request["sources"], facts=["small fact"],
                      interpretations=[], unknowns=[],
                      usage=dict(schemaVersion=1, inputTokens=None, outputTokens=None, costUsd=None))
        self.assertEqual(validate_result(request, result), result)
        oversized = deepcopy(result)
        oversized["facts"] = ["x" * 9000]
        with self.assertRaises(LogContractError):
            validate_result(request, oversized)
        other = deepcopy(result)
        other["requestId"] = "22222222-2222-4222-8222-222222222222"
        with self.assertRaisesRegex(LogContractError, "binding mismatch"):
            validate_result(request, other)


if __name__ == "__main__":
    unittest.main()
