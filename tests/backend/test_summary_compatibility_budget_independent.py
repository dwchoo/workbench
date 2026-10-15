"""LG-D30 persisted consumer fixtures, never actual MODEL/Q01 pass evidence.

Only the public private-record validator is under test. The verifier observation
boundary is mocked; no installed OMP, provider or user database is accessed.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from tests.backend import test_summary_compatibility_independent as records


INFORMATIONAL = ["current_context_unavailable", "output_ceiling_enforcement_unavailable",
                 "reasoning_budget_semantics_unavailable"]
# The eight obligations are independently fixed from SPEC's Q01 contract.
REQUIRED_Q01 = {"Q01-route", "Q01-tools", "Q01-skill", "Q01-result", "Q01-context",
                "Q01-priority", "Q01-recovery", "Q01-origin"}


class CompatibilityBudgetIndependentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = records.PersistedCompatibilityIndependentTests(
            "test_changed_log_bytes_must_invalidate_record")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service

    def persist(self, record):
        self.fixture.persist(record)

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.fixture.directory.iterdir()}

    def assert_rejected(self, record, label):
        self.persist(record)
        before = self.snapshot()
        self.assertFalse(self.service.admission(), label)
        self.assertFalse(self.service.verify_reference(record["id"]), label)
        self.assertEqual(self.snapshot(), before, "a rejected record must not be relabeled")

    def test_information_only_unknowns_admit_without_rewriting_budget_facts(self):
        baseline = self.fixture.record()
        self.assertEqual(set(baseline["items"]), REQUIRED_Q01)
        self.assertEqual(set(row["item"] for row in baseline["item_rows"]), REQUIRED_Q01)
        for reasons in ([INFORMATIONAL[0]], [INFORMATIONAL[1]], [INFORMATIONAL[2]], INFORMATIONAL):
            with self.subTest(reasons=reasons):
                record = deepcopy(baseline)
                record["model_metadata"]["unknowns"] = list(reasons)
                self.persist(record)
                before = self.snapshot()
                self.assertTrue(self.service.admission())
                self.assertTrue(self.service.verify_reference(record["id"]))
                self.assertEqual(self.snapshot(), before)
                persisted = json.loads((self.fixture.directory / (record["id"] + ".json")).read_text())
                self.assertEqual(persisted["model_metadata"], record["model_metadata"])
                self.assertEqual(persisted["model_metadata"]["status"], "inconclusive")
                for field in ("existingContextTokens", "outputLimitEnforced", "reasoningTokens"):
                    self.assertIsNone(persisted["model_metadata"][field])
                self.assertEqual(persisted["model_metadata"]["outputMeaning"], "unknown")

    def test_synthetic_known_pass_with_empty_unknowns_is_distinct_from_unknown_metadata(self):
        # Known values here are validator-only fixtures, not provider observations.
        record = self.fixture.record()
        metadata = record["model_metadata"]
        metadata.update(status="pass", unknowns=[], existingContextTokens=7,
                        outputLimitEnforced=True, reasoningTokens=1024, visibleResultTokens=3072,
                        outputMeaning="reasoning_and_visible")
        self.persist(record)
        before = self.snapshot()
        self.assertTrue(self.service.admission())
        self.assertTrue(self.service.verify_reference(record["id"]))
        self.assertEqual(self.snapshot(), before)

    def test_actual_parent_child_epoch_context_and_selection_sources_remain_required(self):
        baseline = self.fixture.record()
        changes = {
            "parent_descriptor_missing": lambda m: m.pop("model"),
            "parent_context_missing": lambda m: m["model"].pop("contextWindow"),
            "parent_epoch_missing": lambda m: m.pop("epoch"),
            "actual_parent_source_missing": lambda m: m.pop("source"),
            "child_selection_missing": lambda m: m.pop("childSelection"),
            "child_descriptor_missing": lambda m: m["childSelection"].pop("model"),
            "child_context_missing": lambda m: m["childSelection"]["model"].pop("contextWindow"),
            "registry_source_mismatch": lambda m: m["childSelection"].update(descriptorSource="settings_guess"),
            "tier_source_missing": lambda m: m["childSelection"].pop("serviceTierSource"),
            "startup_table_identity_missing": lambda m: m["childSelection"].pop("modelTableDigest"),
            "selector_descriptor_mismatch": lambda m: m["childSelection"]["model"].update(id="different-worker"),
            "selector_thinking_mismatch": lambda m: m["childSelection"].update(thinkingLevel="max"),
            "parent_cap_is_not_child_cap": lambda m: m.update(outputCap=8192),
            "host_default_missing": lambda m: m.pop("outputLimitSource"),
        }
        self.persist(baseline)
        self.assertTrue(self.service.admission(), "current synthetic baseline")
        for label, change in changes.items():
            with self.subTest(missing_or_mismatch=label):
                record = deepcopy(baseline)
                change(record["model_metadata"])
                self.assert_rejected(record, label)

    def test_metadata_status_and_unknowns_pairs_reject_malformed_or_other_reasons(self):
        baseline = self.fixture.record()
        shapes = [
            ("pass_with_unknown", "pass", [INFORMATIONAL[0]]),
            ("inconclusive_without_reason", "inconclusive", []),
            ("unsupported_status", "unavailable", list(INFORMATIONAL)),
            ("not_list", "inconclusive", INFORMATIONAL[0]),
            ("null", "inconclusive", None),
            ("nonstring", "inconclusive", [INFORMATIONAL[0], 0]),
            ("duplicate", "inconclusive", [INFORMATIONAL[0], INFORMATIONAL[0]]),
            ("other_unknown", "inconclusive", ["actual_child_descriptor_unavailable"]),
        ]
        for label, status, reasons in shapes:
            with self.subTest(shape=label):
                record = deepcopy(baseline)
                record["model_metadata"].update(status=status, unknowns=reasons)
                self.assert_rejected(record, label)
        record = deepcopy(baseline)
        record["model_metadata"].pop("unknowns")
        self.assert_rejected(record, "unknowns_absent")

    def test_unknown_parent_context_cannot_be_zero_or_absent(self):
        for shape in ("zero", "absent"):
            with self.subTest(context=shape):
                record = self.fixture.record()
                if shape == "zero":
                    record["model_metadata"]["existingContextTokens"] = 0
                else:
                    record["model_metadata"].pop("existingContextTokens")
                self.assert_rejected(record, "unknown parent context must remain null")

    def test_unknown_provider_output_and_reasoning_cannot_claim_true_or_zero(self):
        # Informational unknowns do not block admission; contradictory facts do.
        # Each case differs from an admitted truthful fixture by exactly one value.
        for field, value, reason in (
            ("outputLimitEnforced", True, "output_ceiling_enforcement_unavailable"),
            ("reasoningTokens", 0, "reasoning_budget_semantics_unavailable"),
        ):
            with self.subTest(field=field, fake_value=value):
                record = self.fixture.record()
                self.persist(record)
                self.assertTrue(self.service.admission(), "truthful unknown baseline")
                self.assertIn(reason, record["model_metadata"]["unknowns"])
                record["model_metadata"][field] = value
                self.assert_rejected(record, f"{reason} contradicts {field}={value!r}")

    def test_top_and_observation_unknowns_and_full_eight_are_not_informational_exceptions(self):
        baseline = self.fixture.record()
        changes = {
            "top_unknown": lambda r: r.update(unknowns=[INFORMATIONAL[0]]),
            "observation_unknown": lambda r: r["item_rows"][0].update(unknowns=[INFORMATIONAL[0]]),
            "missing_required_item": lambda r: r["items"].pop("Q01-origin"),
            "failed_required_item": lambda r: r["items"].update({"Q01-recovery": "fail"}),
            "missing_observation": lambda r: r["item_rows"].pop(),
            "duplicate_observation": lambda r: r["item_rows"].__setitem__(-1, deepcopy(r["item_rows"][0])),
        }
        for label, change in changes.items():
            with self.subTest(requirement=label):
                record = deepcopy(baseline)
                change(record)
                self.assert_rejected(record, label)

    def test_old_metadata_without_child_and_host_default_is_not_reinterpreted(self):
        for shape in ("child_absent", "old_output_source", "original_both"):
            with self.subTest(old_record=shape):
                record = self.fixture.record()
                metadata = record["model_metadata"]
                metadata.update(status="pass", unknowns=[], existingContextTokens=0, outputCap=8192,
                                reasoningTokens=1024, visibleResultTokens=7168,
                                outputMeaning="reasoning_and_visible", outputLimitEnforced=True)
                if shape in ("child_absent", "original_both"):
                    metadata.pop("childSelection")
                if shape in ("old_output_source", "original_both"):
                    metadata["outputLimitSource"] = "synthetic-validator-fixture-only"
                self.assert_rejected(record, "old record remains invalid without relabeling")

    def test_protection_still_blocks_complete_record_with_informational_unknowns(self):
        record = self.fixture.record()
        self.persist(record)
        self.assertTrue(self.service.admission())
        before = (self.fixture.directory / (record["id"] + ".json")).read_bytes()
        self.service.protect({"reason": "compatibility_failed", "episode": "budget-fixture"})
        self.assertFalse(self.service.admission())
        self.assertEqual(self.service.status()["state"], "protected")
        self.assertEqual(self.service.status()["reason"], "compatibility_failed")
        reopened = self.fixture.reopen()
        self.assertFalse(reopened.admission())
        self.assertEqual(reopened.status()["state"], "protected")
        self.assertEqual((self.fixture.directory / (record["id"] + ".json")).read_bytes(), before)

    def test_partial_scripted_verify_cannot_produce_admitted_record_or_clear_protection(self):
        record = self.fixture.record()
        self.persist(record)
        self.service.protect({"reason": "compatibility_failed", "episode": "partial-verifier"})
        observation = {"level": "inconclusive", "unknowns": ["remaining_Q01_unverified"],
                       "items": {"Q01-route": "inconclusive"},
                       "observations": [{"item": "Q01-route", "result": "inconclusive", "fixture": True}],
                       "model_metadata": deepcopy(record["model_metadata"]),
                       "io": {"omp_children": 0, "model_calls": 0, "network_requests": 0,
                              "auth_store_open": 0, "settings_writes": 0}}
        with patch("workbench.backend.summary_compatibility.ScriptedCompatibilityProvider.observe",
                   return_value=deepcopy(observation)) as provider, \
             patch("subprocess.Popen", side_effect=AssertionError("forbidden native/provider launch")) as launch:
            result = self.service.verify_scripted()
        provider.assert_called_once_with()
        launch.assert_not_called()
        self.assertEqual(result["result"], "inconclusive")
        self.assertEqual(result["model_metadata"], record["model_metadata"])
        self.assertEqual(set(result["items"]), REQUIRED_Q01)
        self.assertEqual(result["items"]["Q01-route"], "inconclusive")
        self.assertEqual({value for key, value in result["items"].items() if key != "Q01-route"}, {"not_run"})
        self.assertFalse(self.service.verify_reference(result["id"]))
        self.assertFalse(self.service.admission())
        self.assertEqual(self.service.status()["state"], "protected")
        reopened = self.fixture.reopen()
        self.assertFalse(reopened.admission())
        self.assertEqual(reopened.status()["reason"], "compatibility_failed")

    def test_unknown_facts_cannot_be_erased_by_pass_labels_or_missing_reasons(self):
        truthful = self.fixture.record()
        known = deepcopy(truthful)
        # Known non-enforcement is information, not a new requirement to enforce.
        known["model_metadata"].update(status="pass", unknowns=[], existingContextTokens=7,
                                       outputLimitEnforced=False, reasoningTokens=1024,
                                       visibleResultTokens=3072, outputMeaning="reasoning_and_visible")
        for label, control in (("truthful_inconclusive", truthful), ("known_no_enforcement", known)):
            with self.subTest(positive_control=label):
                self.persist(control)
                before = self.snapshot()
                self.assertEqual((self.service.admission(), self.service.verify_reference(control["id"])),
                                 (True, True), label)
                self.assertEqual(self.snapshot(), before)

        flags_only = deepcopy(truthful)
        flags_only["model_metadata"].update(status="pass", unknowns=[])
        fields_missing = deepcopy(known)
        for field in ("existingContextTokens", "outputLimitEnforced", "reasoningTokens",
                      "visibleResultTokens", "outputMeaning"):
            fields_missing["model_metadata"].pop(field)
        cases = [("flags_only_null_facts", flags_only), ("pass_with_facts_absent", fields_missing)]
        for label, record in cases:
            with self.subTest(reverse_label=label):
                self.persist(record)
                before = self.snapshot()
                observed = (self.service.admission(), self.service.verify_reference(record["id"]))
                self.assertEqual(self.snapshot(), before, "validation must not relabel unknown facts")
                self.assertEqual(observed, (False, False), label)


if __name__ == "__main__":
    unittest.main()
