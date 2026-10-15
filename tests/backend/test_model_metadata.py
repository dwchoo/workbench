"""LG-D30 still requires a host output cap for the selected child model."""
import unittest
from workbench.backend.summary_adapter import SummaryDeferred, _resolved_metadata


class ModelMetadataTests(unittest.TestCase):
    def test_missing_host_output_cap_blocks_preflight_with_valid_child_selection(self):
        descriptor = {"source": "parent_session_readback", "epoch": "observed", "parentSessionId": "parent",
                      "model": {"id": "selected", "provider": "configured", "api": "openai-responses",
                                "baseUrl": "https://example.invalid/v1", "contextWindow": 100000, "maxTokens": 8192},
                      "thinkingLevel": "high", "serviceTierByFamily": {}, "existingContextTokens": None,
                      "outputCap": None, "reasoningTokens": None, "visibleResultTokens": None,
                      "outputLimitEnforced": None, "outputMeaning": "unknown"}
        descriptor["childSelection"] = {
            "source": "configured_worker_default", "modelTableDigest": "a" * 64,
            "descriptorSource": "manager_model_registry_getAll_all",
            "serviceTierSource": "parent_session_readback", "model": dict(descriptor["model"]),
            "selector": "configured/selected:high", "thinkingLevel": "high",
        }
        with self.assertRaisesRegex(SummaryDeferred, "output_budget_unavailable"):
            _resolved_metadata(descriptor)

    def test_profile_cannot_replace_parent_session_source(self):
        with self.assertRaisesRegex(SummaryDeferred, "model_metadata_unavailable"):
            _resolved_metadata({"source": "workbench_profile", "model": {"id": "anything"}})
