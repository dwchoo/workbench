"""Independent optional count-port contracts; synthetic counts are not native accuracy proof."""
from copy import deepcopy
from hashlib import sha256
import unittest

from workbench.backend.summary_adapter import SourcePiece, SummaryDeferred, prepare_summary, skill_bytes
from workbench.contracts.logs_v1 import canonical_json
from tests.backend.test_summary_adapter_independent import contract_fixture

DEFINITION = "Independent existing task definition."


def controlled_count(query):
    return {"version": query["version"], "ownerDigest": query["ownerDigest"],
            "fragmentDigests": deepcopy(query["fragmentDigests"]), "encoding": "ClaudeV3",
            "tokens": [(len(text.encode()) + 7) // 8 for text in query["fragments"]],
            "exact": True, "estimator": "native_known_family_strict"}


def native_case(texts=("작업 목적과 검증 결과",)):
    request, metadata, _ = contract_fixture(texts)
    metadata["model"]["tokenizer"] = "qwen3"
    metadata["childSelection"]["model"]["tokenizer"] = "claude-v3"
    pieces = [SourcePiece(span, text, sequence=i) for i, (span, text) in
              enumerate(zip(request["sources"], texts))]
    return request, metadata, pieces


class NativeBudgetIndependentTests(unittest.TestCase):
    def prepare(self, request, metadata, pieces, port=None):
        return prepare_summary(request, pieces, metadata=metadata, instructions=DEFINITION,
                               count_fragments=port)

    def test_fallback_preserves_byte_selection_omissions_and_observer_truth(self):
        request, metadata, pieces = native_case(("t" * 24000, "사용자 결정", "검증 보고"))
        pieces[1] = SourcePiece(pieces[1].range, pieces[1].text, "decision", 1)
        metadata["estimator"] = "native_known_family_strict"
        before = deepcopy(metadata)
        baseline = self.prepare(request, metadata, pieces)
        self.assertTrue(baseline["omitted"])
        for port, reason in [(None, "count_port_absent"),
                             (lambda q: None, "count_port_unavailable"),
                             (lambda q: {**controlled_count(q), "exact": False, "encoding": None},
                              "native_count_unavailable")]:
            with self.subTest(reason=reason):
                actual = self.prepare(request, metadata, list(reversed(pieces)), port)
                for key in ("request", "selected", "omitted"):
                    self.assertEqual(actual[key], baseline[key])
                self.assertEqual(actual["budget"]["inputEstimate"], baseline["budget"]["inputEstimate"])
                self.assertEqual(actual["budget"]["estimator"], "utf8_bytes_upper_bound")
                self.assertEqual(actual["budget"]["fallbackReason"], reason)
                self.assertNotIn("countBinding", actual)
                self.assertEqual(actual["metadata"], before)
        self.assertEqual(metadata, before)

    def test_unknown_family_never_invokes_port_and_is_explicit_bytes(self):
        for family in (None, "o200k_base", "future-family", 1):
            request, metadata, pieces = native_case()
            metadata["childSelection"]["model"]["tokenizer"] = family
            with self.subTest(family=family):
                actual = self.prepare(request, metadata, pieces, lambda q: self.fail("unknown tokenizer used port"))
                self.assertEqual(actual["budget"]["fallbackReason"], "tokenizer_family_unavailable")
                self.assertEqual(actual["budget"]["estimator"], "utf8_bytes_upper_bound")
                self.assertNotIn("countBinding", actual)

    def test_ordered_fragments_owner_and_response_are_detached(self):
        request, metadata, pieces = native_case(("한글\\\"\n" * 350, "final decision"))
        before = deepcopy(metadata)
        queries, responses = [], []
        def port(query):
            queries.append(deepcopy(query))
            proof = controlled_count(query)
            responses.append(proof)
            query["owner"]["model"]["tokenizer"] = "host callback mutation"
            query["fragments"].reverse()
            return proof
        actual = self.prepare(request, metadata, pieces, port)
        final = queries[-1]
        payload = {key: actual[key] for key in ("request", "selected", "omitted", "priorCumulative", "resultByteLimit")}
        expected = [DEFINITION + "\n\n" + skill_bytes().decode(), canonical_json(payload).decode()]
        self.assertEqual(final["fragments"], expected)
        self.assertEqual(final["fragmentDigests"], [sha256(t.encode()).hexdigest() for t in expected])
        owner = {"epoch": before["epoch"], "parentSessionId": before["parentSessionId"], "model": before["childSelection"]["model"]}
        self.assertEqual(final["ownerDigest"], sha256(canonical_json(owner)).hexdigest())
        self.assertEqual(actual["countBinding"]["tokens"], [(len(t.encode()) + 7) // 8 for t in expected])
        self.assertNotEqual(*actual["countBinding"]["tokens"])
        self.assertEqual(actual["budget"]["inputEstimate"], sum(actual["countBinding"]["tokens"]) + 2048)
        frozen = deepcopy(actual)
        responses[-1]["tokens"][0] = 99999
        responses[-1]["fragmentDigests"].reverse()
        metadata["childSelection"]["model"]["tokenizer"] = "qwen3"
        self.assertEqual(actual, frozen)
        self.assertEqual(actual["metadata"], before)

    def test_malformed_or_mismatched_proof_is_a_predispatch_failure(self):
        changes = [{"version": "other"}, {"ownerDigest": "0" * 64},
                   {"encoding": None}, {"encoding": "o200k_base"}, {"estimator": "utf8_bytes_upper_bound"},
                   {"tokens": [1]}, {"tokens": [1, -1]}, {"tokens": [True, 1]},
                   {"tokens": [1.5, 1]}, {"tokens": [1, 1, 1]}, {"exact": 1}]
        for change in changes:
            request, metadata, pieces = native_case()
            with self.subTest(change=change), self.assertRaisesRegex(SummaryDeferred, "^input_count_binding_mismatch$"):
                self.prepare(request, metadata, pieces, lambda q: {**controlled_count(q), **change})
        request, metadata, pieces = native_case()
        def reversed_proof(query):
            proof = controlled_count(query)
            proof["fragmentDigests"].reverse()
            return proof
        with self.assertRaisesRegex(SummaryDeferred, "input_count_binding_mismatch"):
            self.prepare(request, metadata, pieces, reversed_proof)
        with self.assertRaisesRegex(SummaryDeferred, "input_count_unavailable"):
            self.prepare(request, metadata, pieces, lambda q: (_ for _ in ()).throw(ValueError("counter unavailable")))

    def test_child_context_boundary_uses_fragment_sum_without_old_cost_quota(self):
        request, metadata, pieces = native_case()
        metadata["childSelection"]["model"]["contextWindow"] = 30000
        def cap_count(total):
            return lambda q: {**controlled_count(q), "tokens": [100, total - 100]}
        accepted = self.prepare(request, metadata, pieces, cap_count(10000))
        self.assertGreater(accepted["budget"]["inputEstimate"], 8192)
        self.assertEqual(accepted["budget"]["inputLimit"], 30000)
        with self.assertRaisesRegex(SummaryDeferred, "input_budget_unavailable"):
            self.prepare(request, metadata, pieces, cap_count(30000))

    def test_tiny_native_counts_cannot_bypass_wire_or_two_output_reserves(self):
        request, metadata, pieces = native_case(("한" * 9000,))
        tiny = lambda q: {**controlled_count(q), "tokens": [1, 1]}
        with self.assertRaisesRegex(SummaryDeferred, "input_budget_unavailable"):
            self.prepare(request, metadata, pieces, tiny)
        request, metadata, pieces = native_case()
        accepted = self.prepare(request, metadata, pieces, tiny)
        budget = accepted["budget"]
        self.assertEqual(budget["previousOutputReserve"], 8192)
        self.assertEqual(budget["nextOutputReserve"], 8192)
        self.assertEqual(budget["mandatoryArgumentsBytes"], 8192)
        self.assertGreater(budget["wireInputEstimateBytes"], budget["inputEstimate"])
        metadata["existingContextTokens"] = None
        unchanged = self.prepare(request, metadata, pieces, tiny)
        self.assertIsNone(unchanged["metadata"]["existingContextTokens"])
        metadata["existingContextTokens"] = 999999
        self.assertEqual(self.prepare(request, metadata, pieces, tiny)["selected"], accepted["selected"])
        metadata["childSelection"]["model"]["contextWindow"] = 16384
        with self.assertRaisesRegex(SummaryDeferred, "input_budget_unavailable"):
            self.prepare(request, metadata, pieces, tiny)
