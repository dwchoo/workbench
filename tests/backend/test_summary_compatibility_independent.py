"""Independent persisted-validator contract tests, never actual Q01 pass proof.

Synthetic records exercise validation/admission only. No installed OMP, trusted
verification producer or operating model is exercised by these tests.
"""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.backend.paths import ensure_private_dir, write_private_json
from workbench.backend.summary_compatibility import Q01_ITEMS, RECORD_LIMIT, SummaryCompatibility


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode()).hexdigest()


class PersistedCompatibilityIndependentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "compat"
        self.identity = {"unknowns": [], "policy_version": 1,
                         "policy_sha256": "1" * 64, "runtime_digest": "2" * 64,
                         "closure_digest": "3" * 64,
                         "stable_host": {"os_family": "Linux", "architecture": "x86_64",
                                         "python_minor": "3.12"},
                         "volatile_host": {"kernel_release": "original", "python_patch": "3.12.1"}}
        self.omp = {"version": "18.8.6", "binary_sha256": "4" * 64,
                    "bundled_sha256": "5" * 64}
        self.boundary = {"route": "sdk_session", "role": "manager", "adoption_ref": "LG-D22",
                         "agent_definition_sha256": "6" * 64, "skill_sha256": "7" * 64,
                         "contract_sha256": "8" * 64, "model_config_sha256": "9" * 64}
        self.service = self.reopen()

    def reopen(self):
        return SummaryCompatibility(self.directory, identity=lambda: deepcopy(self.identity),
                                    omp_identity=lambda: deepcopy(self.omp),
                                    boundary=lambda: deepcopy(self.boundary))

    def persist(self, record):
        ensure_private_dir(self.directory)
        write_private_json(self.directory / (record["id"] + ".json"), record)
        write_private_json(self.directory / "state.json", {
            "latest_record": record["id"], "record_sha256": digest(record),
            "protection": None, "episode": 0})

    def record(self):
        # This is a synthetic complete-shaped validator fixture. It does not
        # authorize production use and is not an observed verifier pass.
        ensure_private_dir(self.directory)
        log = self.directory / "fixture.log"
        log.write_bytes(b"synthetic validator-only observations\n")
        log.chmod(0o600)
        return {"id": str(uuid4()), "schema_version": 1, "validator_version": 1,
                "created_at": "2026-10-12T00:00:00+00:00", "result": "pass",
                "identity": deepcopy(self.identity), "omp": deepcopy(self.omp),
                "boundary": deepcopy(self.boundary), "unknowns": [],
                "model_metadata": {"status": "inconclusive", "unknowns": ["current_context_unavailable",
                                   "output_ceiling_enforcement_unavailable", "reasoning_budget_semantics_unavailable"],
                                   "source": "parent_session_readback",
                                   "parentSessionId": "synthetic-parent", "epoch": "synthetic-epoch",
                                   "model": {"id": "synthetic", "provider": "fixture",
                                             "api": "openai-responses", "baseUrl": "https://example.invalid/v1",
                                             "contextWindow": 131072, "maxTokens": 16384,
                                             "tokenizer": "claude-v3"},
                                   "childSelection": {"source": "configured_worker_default",
                                       "selector": "fixture/worker-light:low", "modelTableDigest": "a" * 64,
                                       "thinkingLevel": "low",
                                       "descriptorSource": "manager_model_registry_getAll_all",
                                       "serviceTierSource": "parent_session_readback",
                                       "model": {"id": "worker-light", "provider": "fixture",
                                           "api": "openai-responses", "baseUrl": "https://example.invalid/v1",
                                           "contextWindow": 32768, "maxTokens": 4096, "tokenizer": "qwen3"}},
                                   "thinkingLevel": "high", "serviceTierByFamily": {},
                                   "existingContextTokens": None, "outputCap": 4096,
                                   "reasoningTokens": None, "visibleResultTokens": None,
                                   "outputMeaning": "unknown", "outputLimitEnforced": None,
                                   "outputLimitSource": "host_default",
                                   "estimator": "utf8_bytes_upper_bound"},
                "items": {name: "pass" for name in Q01_ITEMS},
                "observations": [{"item": name, "result": "pass", "fixture": True}
                                 for name in Q01_ITEMS],
                "evidence": [{"path": log.name, "sha256": sha256(log.read_bytes()).hexdigest()}]}

    def test_incomplete_records_cannot_admit_even_with_self_consistent_digest(self):
        record = self.record()
        self.persist(record)
        self.assertTrue(self.service.admission(), "complete-shaped synthetic baseline")
        self.assertTrue(self.service.verify_reference(record["id"]))
        # SPEC 8.1 requires time, eight observations, adoption and evidence/log
        # digests. Do not enforce a newly invented evidence field name: remove
        # ALL evidence/digest entries and check the semantic absence.
        changes = {
            "creation_time_absent": lambda r: r.pop("created_at"),
            "eight_observations_absent": lambda r: r.update(observations=[{"fixture": True}]),
            "adoption_absent": lambda r: r["boundary"].pop("adoption_ref"),
            "all_log_evidence_absent": lambda r: r.pop("evidence"),
        }
        for name, change in changes.items():
            with self.subTest(absence=name):
                bad = deepcopy(record)
                change(bad)
                self.persist(bad)
                # Keep current boundary aligned in adoption case so the test
                # checks original record validity, not unrelated drift.
                old_boundary = deepcopy(self.boundary)
                self.boundary = deepcopy(bad["boundary"])
                try:
                    self.assertFalse(self.service.admission(), name)
                    self.assertFalse(self.service.verify_reference(bad["id"]), name)
                finally:
                    self.boundary = old_boundary

    def test_changed_log_bytes_must_invalidate_record(self):
        record = self.record()
        self.persist(record)
        self.assertTrue(self.service.admission())
        (self.directory / "fixture.log").write_bytes(b"changed evidence\n")
        self.assertFalse(self.service.admission(), "SPEC 8.1 actual log hash verification")
        self.assertFalse(self.service.verify_reference(record["id"]))

    def test_incomplete_original_identity_cannot_use_version_drift_exception(self):
        record = self.record()
        self.persist(record)
        self.assertTrue(self.service.admission())
        self.assertTrue(self.service.verify_reference(record["id"]))
        record["identity"].pop("runtime_digest")
        self.persist(record)
        self.omp["version"] = "new-unverified-version"
        self.assertFalse(self.service.admission())
        self.assertFalse(self.service.verify_reference(record["id"]))

    def test_equal_unknown_identity_is_not_a_verified_reference(self):
        original_identity = deepcopy(self.identity)
        for shape in ("nonempty", "missing", "wrong_type"):
            with self.subTest(unknowns=shape):
                self.identity = deepcopy(original_identity)
                record = self.record()
                self.persist(record)
                self.assertTrue(self.service.admission())
                self.assertTrue(self.service.verify_reference(record["id"]))
                if shape == "missing":
                    self.identity.pop("unknowns")
                    record["identity"].pop("unknowns")
                else:
                    value = ["required_source_missing"] if shape == "nonempty" else None
                    self.identity["unknowns"] = deepcopy(value)
                    record["identity"]["unknowns"] = deepcopy(value)
                self.persist(record)
                self.assertFalse(self.service.admission())
                self.assertFalse(self.service.verify_reference(record["id"]))
        self.identity = original_identity

    def test_original_and_current_omp_require_digests_before_drift_admission(self):
        original_omp = deepcopy(self.omp)
        for owner in ("original", "current"):
            for field in ("binary_sha256", "bundled_sha256"):
                with self.subTest(owner=owner, missing=field):
                    self.omp = deepcopy(original_omp)
                    record = self.record()
                    self.persist(record)
                    self.assertTrue(self.service.admission())
                    self.assertTrue(self.service.verify_reference(record["id"]))
                    target = record["omp"] if owner == "original" else self.omp
                    target.pop(field)
                    self.persist(record)
                    self.omp["version"] = "new-unverified-version"
                    self.assertFalse(self.service.admission())
                    self.assertFalse(self.service.verify_reference(record["id"]))
        self.omp = original_omp

    def test_metadata_pass_label_cannot_replace_required_output_and_source_fields(self):
        for field in ("unknowns", "outputCap", "source", "outputLimitSource", "model"):
            with self.subTest(metadata=field):
                record = self.record()
                self.persist(record)
                self.assertTrue(self.service.admission())
                self.assertTrue(self.service.verify_reference(record["id"]))
                if field == "unknowns":
                    record["model_metadata"][field] = ["output_semantics_unknown"]
                else:
                    record["model_metadata"].pop(field)
                self.persist(record)
                self.assertFalse(self.service.admission())
                self.assertFalse(self.service.verify_reference(record["id"]))

    def test_corrupt_state_types_and_bad_reference_arguments_fail_closed_without_crash(self):
        for field, value in (("latest_record", []), ("latest_record", 1),
                             ("episode", True), ("episode", -1),
                             ("episode", "1"), ("episode", None)):
            with self.subTest(field=field, value=value):
                record = self.record()
                self.persist(record)
                self.assertTrue(self.service.admission())
                self.assertTrue(self.service.verify_reference(record["id"]))
                state_path = self.directory / "state.json"
                state = json.loads(state_path.read_text())
                state[field] = value
                write_private_json(state_path, state)
                observed = self.service.status()
                self.assertFalse(observed["admitted"])
                self.assertEqual(observed["state"], "unavailable")
                self.assertFalse(self.service.verify_reference(record["id"]))
        self.persist(self.record())
        for reference in (None, [], 1, {}):
            with self.subTest(reference=reference):
                self.assertFalse(self.service.verify_reference(reference))

    def test_bounded_deep_metadata_fails_closed_on_copy_recursion(self):
        # This reproduces the observed metadata deepcopy exception, not a new
        # depth policy or guard tamper protocol. Recursion limits stay intact.
        record = self.record()
        self.persist(record)
        self.assertTrue(self.service.admission())
        self.assertTrue(self.service.verify_reference(record["id"]))
        extra = 0
        for _ in range(600):
            extra = [extra]
        record["model_metadata"]["extra"] = extra
        # Normal private writer succeeds; public readback fails closed rather
        # than raising while copying this bounded validator-only input.
        self.persist(record)
        path = self.directory / (record["id"] + ".json")
        self.assertLess(path.stat().st_size, RECORD_LIMIT)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        observed = self.service.status()
        self.assertFalse(observed["admitted"])
        self.assertEqual(observed["state"], "unavailable")
        self.assertEqual(observed["reason"], "compatibility_record_invalid")
        self.assertFalse(self.service.verify_reference(record["id"]))

    def test_record_corruption_unsafe_files_and_size_bound_reject(self):
        record = self.record()
        self.persist(record)
        path = self.directory / (record["id"] + ".json")
        for contents in (b"broken", b" " * (RECORD_LIMIT + 1)):
            with self.subTest(size=len(contents)):
                path.write_bytes(contents)
                self.assertFalse(self.service.admission())
        self.persist(record)
        original = path.read_bytes()
        path.unlink()
        target = self.directory / "outside-record.json"
        target.write_bytes(original)
        target.chmod(0o600)
        path.symlink_to(target)
        self.assertFalse(self.service.admission())
        path.unlink()
        self.persist(record)
        self.directory.chmod(0o755)
        self.assertFalse(self.service.admission())

    def test_nonregular_state_fifo_is_rejected_without_blocking_reader(self):
        # Owned nonregular input must fail closed before a blocking read. The
        # timeout is a test bound, not a new product policy or retry limit.
        ensure_private_dir(self.directory)
        state = self.directory / "state.json"
        os.mkfifo(state, 0o600)
        script = (
            "import json,sys; from pathlib import Path; "
            "from workbench.backend.summary_compatibility import SummaryCompatibility; "
            "print(json.dumps(SummaryCompatibility(Path(sys.argv[1])).status()))"
        )
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.directory)], cwd=root,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root / "src"),
                 "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=3,
        )
        # subprocess.run reaps the owned child on return (and kills/reaps on
        # timeout); a FIFO hang therefore fails instead of leaking a reader.
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertFalse(observed["admitted"])
        self.assertEqual(observed["state"], "unavailable")
        self.assertEqual(observed["reason"], "compatibility_record_invalid")

    def test_drift_admission_and_exact_reference_are_distinct_without_record_mutation(self):
        record = self.record()
        self.persist(record)
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        self.identity["volatile_host"]["kernel_release"] = "new-patch"
        self.assertTrue(self.service.admission())
        self.assertFalse(self.service.verify_reference(record["id"]))
        self.omp["version"] = "next"
        self.assertEqual(self.service.status()["state"], "version_drift_unverified")
        self.assertTrue(self.service.admission())
        self.identity["stable_host"]["python_minor"] = "3.13"
        self.assertEqual(self.service.status()["state"], "revalidation_required")
        self.assertFalse(self.service.admission())
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_protection_survives_unavailable_verify_and_notice_is_durable(self):
        self.service.protect({"reason": "guard_confirmation_unavailable", "episode": "one"})
        self.assertEqual(self.service.take_notice()["episode"], 1)
        reopened = self.reopen()
        with patch("workbench.backend.summary_compat_runtime.shutil.which", return_value=None):
            result = reopened.verify_scripted()
        self.assertEqual(result["result"], "unavailable")
        self.assertEqual(set(result["items"].values()), {"not_run"})
        self.assertEqual(result["model_metadata"]["status"], "inconclusive")
        self.assertFalse(reopened.admission())
        reopened.protect({"reason": "guard_confirmation_unavailable", "episode": "one"})
        self.assertIsNone(reopened.take_notice())
        self.assertEqual(reopened.status()["reason"], "guard_confirmation_unavailable")


if __name__ == "__main__":
    unittest.main()
