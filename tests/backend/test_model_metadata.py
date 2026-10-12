"""Incomplete operating observations must not become usable output budgets."""
import unittest
from workbench.backend.summary_adapter import SummaryDeferred, _resolved_metadata


class ModelMetadataTests(unittest.TestCase):
    def test_real_getter_shape_without_enforcement_semantics_blocks_preflight(self):
        descriptor = {"source": "parent_session_readback", "epoch": "observed", "parentSessionId": "parent",
                      "model": {"id": "selected", "provider": "configured", "api": "openai-responses",
                                "baseUrl": "https://example.invalid/v1", "contextWindow": 100000, "maxTokens": 8192},
                      "thinkingLevel": "high", "serviceTierByFamily": {}, "existingContextTokens": None,
                      "outputCap": None, "reasoningTokens": None, "visibleResultTokens": None,
                      "outputLimitEnforced": None, "outputMeaning": "unknown"}
        with self.assertRaisesRegex(SummaryDeferred, "output_budget_unavailable"):
            _resolved_metadata(descriptor)

    def test_profile_cannot_replace_parent_session_source(self):
        with self.assertRaisesRegex(SummaryDeferred, "model_metadata_unavailable"):
            _resolved_metadata({"source": "workbench_profile", "model": {"id": "anything"}})
