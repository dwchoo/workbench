"""Independent controlled-fixture boundaries; no operating/native accuracy claim.

Launch tests observe the configuration handed to the existing launcher without
starting OMP. Owner-file tests observe transport and binding, using synthetic
proofs only. The raw result-key test calls the actual product TypeScript parser.
The separately authorized opt-in live run supplies actual SDK count evidence.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.integration.live_summary_scheduler_independent import (
    OwnerCountPort,
    scripted_summary_params,
    start_native_phase,
)
from tests.backend.test_summary_native_budget_independent import native_case
from workbench.backend.summary_adapter import NATIVE_ESTIMATOR, SummaryDeferred, prepare_summary


class NativeCompositionIndependentTests(unittest.TestCase):
    def test_normal_model_configuration_is_present_before_launch_without_parent_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)
            (private / "agent").mkdir()
            config = private / "agent/models.yml"
            original = "providers:\n  currentproduct:\n    models:\n      - id: scripted\n        reasoning: true\n"
            parent_model = {"provider": "currentproduct", "id": "scripted"}
            before_parent = deepcopy(parent_model)
            observations = []
            phase = SimpleNamespace(private=private)

            def launch(argv, **kwargs):
                observations.append((list(argv), kwargs["cwd"], config.read_text()))
                return object()

            def start():
                config.write_text(original)
                subprocess.Popen(["omp", "--model", "currentproduct/scripted"], cwd=private / "project")
                # Resolved readback is the fixture boundary, not parent mutation.
                phase.metadata = {"model": {**parent_model, "tokenizer": "claude-v3"}}

            phase.start = start
            with patch("tests.backend.q01_product_adapter_probe.runtime.subprocess.Popen", launch):
                start_native_phase(phase)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0][2], original.replace(
                "        reasoning: true\n", "        reasoning: true\n        tokenizer: claude-v3\n"))
            self.assertEqual(phase.native_configuration["sha256"], sha256(config.read_bytes()).hexdigest())
            self.assertEqual(phase.native_configuration["family"], "claude-v3")
            self.assertEqual(parent_model, before_parent)
            self.assertEqual(phase.metadata["model"]["tokenizer"], "claude-v3")

    def test_missing_or_wrong_resolved_family_stops_before_count(self):
        for family in (None, "qwen3"):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as temporary:
                private = Path(temporary)
                phase = SimpleNamespace(private=private, metadata={"model": {"tokenizer": family}})
                phase.start = lambda: None
                with self.assertRaises(AssertionError):
                    start_native_phase(phase)
                self.assertFalse(hasattr(phase, "native_configuration"))
                self.assertEqual(list(private.iterdir()), [])

    def test_owner_file_port_preserves_proof_and_python_rejects_wrong_binding(self):
        request, metadata, pieces = native_case()
        metadata["estimator"] = "utf8_bytes_upper_bound"
        metadata_before = deepcopy(metadata)
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)
            phase = SimpleNamespace(private=private, stop_reason=None, events=lambda: [])
            # The transport must not repair an exact proof belonging to another
            # owner. This is synthetic transport evidence, not tokenizer evidence.
            proof = {"version": "summary-host-input-v1", "ownerDigest": "0" * 64,
                     "fragmentDigests": ["1" * 64, "2" * 64], "tokens": [4, 5],
                     "encoding": "ClaudeV3", "exact": True, "estimator": NATIVE_ESTIMATOR}
            (private / "count-reply-0.json").write_text(json.dumps(proof))
            port = OwnerCountPort(phase)
            with self.assertRaisesRegex(SummaryDeferred, "^input_count_binding_mismatch$"):
                prepare_summary(request, pieces, metadata=metadata, instructions="existing definition",
                                count_fragments=port)
            self.assertEqual(port.rows[0]["proof"], proof)
            query = json.loads((private / "count-query-0.json").read_text())
            self.assertNotEqual(query["ownerDigest"], proof["ownerDigest"])
            self.assertEqual(query["fragmentDigests"], [sha256(text.encode()).hexdigest()
                                                       for text in query["fragments"]])
            self.assertFalse((private / "count-query-0.json.tmp").exists())
            self.assertEqual(metadata, metadata_before)
            self.assertEqual(metadata["estimator"], "utf8_bytes_upper_bound")

    def test_count_only_port_refuses_bytes_fallback_as_native_evidence(self):
        for proof in ({"exact": False, "estimator": "utf8_bytes_upper_bound"},
                      {"exact": True, "estimator": "utf8_bytes_upper_bound"}):
            with self.subTest(proof=proof), tempfile.TemporaryDirectory() as temporary:
                private = Path(temporary)
                phase = SimpleNamespace(private=private, stop_reason=None, events=lambda: [])
                (private / "count-reply-0.json").write_text(json.dumps(proof))
                with self.assertRaisesRegex(RuntimeError, "^controlled_native_count_unavailable$"):
                    OwnerCountPort(phase)({"fragments": ["unchanged input"]})
                self.assertEqual(json.loads((private / "count-query-0.json").read_text()),
                                 {"fragments": ["unchanged input"]})

    def test_contract_negative_is_raw_extra_key_not_2001_character_limit(self):
        normal = scripted_summary_params("task_positive")
        negatives = [scripted_summary_params(case) for case in
                     ("contract1", "contract2", "contract3", "contract_then_bash")]
        valid_long = {key: value for key, value in negatives[0].items() if key != "fixtureUnexpected"}
        parser = (Path(__file__).resolve().parents[2] / "omp_bridge/g3/summary_sdk.ts").as_uri()
        script = f'''import {{validateSummaryParams}} from {json.dumps(parser)};
let raw="";for await(const chunk of process.stdin)raw+=chunk;
const inputs=JSON.parse(raw);
const outcomes=inputs.map(value=>{{try{{validateSummaryParams(value);return "accepted";}}
catch(error){{return error.message;}}}});
process.stdout.write(JSON.stringify(outcomes));'''
        completed = subprocess.run(["node", "--input-type=module", "-e", script],
                                   input=json.dumps([normal, valid_long, *negatives]), text=True,
                                   capture_output=True, timeout=15, check=True)
        self.assertEqual(json.loads(completed.stdout), ["accepted", "accepted"] +
                         ["invalid_result_parameters"] * 4)
        self.assertEqual(len(valid_long["facts"][0]), 2001)
        self.assertLess(len(json.dumps(valid_long).encode()), 8192)
        self.assertTrue(all(value["fixtureUnexpected"] is True for value in negatives))


if __name__ == "__main__":
    unittest.main()
