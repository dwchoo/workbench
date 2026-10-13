"""Product core fixtures; no installed OMP or operating MODEL pass claim."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.backend.cli import main
from workbench.backend.paths import ensure_private_dir, write_private_json
from workbench.backend.summary_compatibility import (
    POLICY_PATH, Q01_ITEMS, SummaryCompatibility, _digest, collect_runtime_identity,
)

ROOT = Path(__file__).resolve().parents[2]


class CompatibilityCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ident = {"policy_version": 1, "unknowns": [], "policy_sha256": "1" * 64,
                      "closure_digest": "2" * 64, "runtime_digest": "3" * 64,
                      "stable_host": {"os_family": "Linux", "architecture": "x86_64", "python_minor": "3.12"}}
        self.omp = {"binary_sha256": "4" * 64, "version": "18.8.6", "bundled_sha256": "5" * 64}
        self.boundary = {"route": "sdk_session", "adoption_ref": "approved", "model_config": "one"}
        self.service = SummaryCompatibility(self.root / "compat", identity=lambda: deepcopy(self.ident),
            omp_identity=lambda: deepcopy(self.omp), boundary=lambda: deepcopy(self.boundary))

    def fixture_verified_record(self):
        """Synthetic persisted core fixture, deliberately not runtime verification."""
        ref = str(uuid4())
        ensure_private_dir(self.service.directory)
        log = self.service.directory / "observations.log"
        log.write_bytes(b"synthetic validator-only observations\n")
        log.chmod(0o600)
        record = {"id": ref, "schema_version": 1, "validator_version": 1, "result": "pass",
                  "created_at": "2026-10-12T00:00:00+00:00",
                  "identity": deepcopy(self.ident), "omp": deepcopy(self.omp),
                  "boundary": deepcopy(self.boundary), "unknowns": [],
                  "model_metadata": {"status": "pass", "unknowns": [], "source": "parent_session_readback",
                     "epoch": "synthetic-validator-only", "parentSessionId": "synthetic-parent",
                     "model": {"id": "fixture", "provider": "scripted", "api": "openai-completions",
                               "baseUrl": "http://127.0.0.1:1/v1", "contextWindow": 131072, "maxTokens": 8192},
                     "thinkingLevel": "off", "serviceTierByFamily": {}, "existingContextTokens": 0,
                     "outputCap": 8192, "reasoningTokens": 0, "visibleResultTokens": 8192,
                     "outputMeaning": "reasoning_and_visible", "outputLimitEnforced": True,
                     "outputLimitSource": "synthetic-validator-only", "estimator": "utf8_bytes_upper_bound"},
                  "observations": [{"item": name, "result": "pass", "fixture": True} for name in Q01_ITEMS],
                  "evidence": [{"path": log.name, "sha256": sha256(log.read_bytes()).hexdigest()}],
                  "items": {name: "pass" for name in Q01_ITEMS}}
        ensure_private_dir(self.service.directory)
        write_private_json(self.service.directory / (ref + ".json"), record)
        write_private_json(self.service.directory / "state.json", {
            "latest_record": ref, "record_sha256": _digest(record), "protection": None, "episode": 0})
        return ref, record

    def test_explicit_unavailable_verify_zero_launch_keeps_protection(self):
        self.service.protect({"episode": "existing", "reason": "compatibility_failed"})
        with patch("workbench.backend.summary_compat_runtime.shutil.which", return_value=None), \
             patch("subprocess.Popen", side_effect=AssertionError("unexpected launch")):
            result = self.service.verify_scripted()
        self.assertEqual(result["result"], "unavailable")
        self.assertEqual(set(result["items"].values()), {"not_run"})
        self.assertTrue(all(value == 0 for value in result["io"].values()))
        self.assertFalse(self.service.verify_reference(result["id"]))
        self.assertFalse(self.service.admission())
        self.assertEqual(self.service.status()["reason"], "compatibility_failed")
        record_path = self.service.directory / (result["id"] + ".json")
        self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.service.directory.stat().st_mode), 0o700)

    def test_version_only_drift_and_unrelated_identity_never_create_current_pass(self):
        ref, record = self.fixture_verified_record()
        self.assertTrue(self.service.verify_reference(ref))
        self.omp["version"] = "future"
        self.assertEqual(self.service.status()["state"], "version_drift_unverified")
        self.assertTrue(self.service.admission())
        self.assertFalse(self.service.verify_reference(ref))
        self.assertEqual(json.loads((self.service.directory / (ref + ".json")).read_text()), record)
        self.omp["version"] = "18.8.6"
        self.ident["runtime_digest"] = "6" * 64
        self.assertEqual(self.service.status()["state"], "unrelated_change_observed")
        self.ident["closure_digest"] = "7" * 64
        self.assertEqual(self.service.status()["state"], "revalidation_required")
        self.assertFalse(self.service.admission())

    def test_protection_persists_and_episode_notice_taken_once(self):
        self.fixture_verified_record()
        incident = {"episode": "one", "reason": "guard_violation_observed"}
        self.service.protect(incident)
        self.assertIsNotNone(self.service.take_notice())
        restarted = SummaryCompatibility(self.service.directory)
        restarted.protect(incident)
        self.assertIsNone(restarted.take_notice())
        restarted.protect({"episode": "one", "reason": "request_mismatch"})
        self.assertIsNone(restarted.take_notice())
        self.assertFalse(restarted.admission())
        self.omp["version"] = "future"
        self.assertEqual(self.service.status()["reason"], "guard_violation_observed")

    def test_missing_malformed_unowned_or_hardlinked_record_fail_closed(self):
        self.assertFalse(self.service.admission())
        self.assertFalse(self.service.verify_reference("../../arbitrary.json"))
        ref, record = self.fixture_verified_record()
        file = self.service.directory / (ref + ".json")
        file.write_text(json.dumps({**record, "items": {"Q01-tools": "pass"}}))
        self.assertFalse(self.service.admission())
        file.chmod(0o644)
        self.assertFalse(self.service.admission())
        file.chmod(0o600)
        os.link(file, self.service.directory / "alias.json")
        self.assertFalse(self.service.admission())

    def test_cli_status_does_not_create_and_verify_returns_nonzero_without_launch(self):
        directory = self.root / "data"
        with patch("builtins.print"):
            self.assertEqual(main(["logs", "summary-compat", "status", "--data-dir", str(directory), "--json"]), 0)
            self.assertFalse(directory.exists())
            self.assertEqual(main(["logs", "summary-compat", "verify", "--scripted", "--data-dir", str(directory), "--json"]), 1)
        self.assertFalse((directory / "raw-v2").exists())

    def persist_fixture(self, ref, record):
        write_private_json(self.service.directory / (ref + ".json"), record)
        write_private_json(self.service.directory / "state.json", {
            "latest_record": ref, "record_sha256": _digest(record), "protection": None, "episode": 0})

    def test_original_identity_and_omp_must_be_complete_before_any_drift_exception(self):
        ref, original = self.fixture_verified_record()
        changes = [
            lambda r: r["identity"].pop("runtime_digest"),
            lambda r: r["identity"].update(policy_version=True),
            lambda r: r["identity"].update(policy_version=2),
            lambda r: r["identity"].update(policy_sha256="A" * 64),
            lambda r: r["identity"].update(unknowns=None),
            lambda r: r["identity"].update(unknowns=["unknown"]),
            lambda r: r["identity"]["stable_host"].pop("os_family"),
            lambda r: r["identity"]["stable_host"].update(architecture=""),
            lambda r: r["omp"].pop("binary_sha256"),
            lambda r: r["omp"].update(version=""),
            lambda r: r["omp"].update(bundled_sha256="wrong"),
        ]
        old_ident, old_omp = deepcopy(self.ident), deepcopy(self.omp)
        for index, change in enumerate(changes):
            with self.subTest(original=index):
                bad = deepcopy(original)
                change(bad)
                self.persist_fixture(ref, bad)
                self.ident, self.omp = deepcopy(bad["identity"]), deepcopy(bad["omp"])
                self.assertFalse(self.service.verify_reference(ref))  # Even self-consistent malformed inputs fail.
                self.omp["version"] = "future"
                self.assertFalse(self.service.admission())  # No original validity => no drift exception.
        self.ident, self.omp = old_ident, old_omp
        self.persist_fixture(ref, original)
        for key in ("version", "binary_sha256", "bundled_sha256"):
            with self.subTest(current_omp=key):
                self.omp = deepcopy(old_omp)
                self.omp[key] = ""
                self.assertFalse(self.service.admission())
                self.assertFalse(self.service.verify_reference(ref))
        self.omp = old_omp

    def test_pass_metadata_requires_the_existing_resolved_output_contract(self):
        ref, original = self.fixture_verified_record()
        changes = [lambda m: m.pop("model"), lambda m: m.update(source="profile"),
                   lambda m: m.update(unknowns=None), lambda m: m.update(unknowns=["required_unknown"]),
                   lambda m: m.update(outputLimitEnforced=False), lambda m: m.update(outputMeaning="unknown"),
                   lambda m: m.update(outputCap=9000), lambda m: m.update(reasoningTokens=None),
                   lambda m: m.update(existingContextTokens=None), lambda m: m.update(estimator="unknown")]
        for index, change in enumerate(changes):
            with self.subTest(metadata=index):
                bad = deepcopy(original)
                change(bad["model_metadata"])
                self.persist_fixture(ref, bad)
                self.assertFalse(self.service.admission())
                self.assertFalse(self.service.verify_reference(ref))

    def test_malformed_private_state_and_public_reference_fail_closed_without_crash(self):
        ref, _ = self.fixture_verified_record()
        for public in (None, 1, [], {}, True):
            with self.subTest(public=public):
                self.assertFalse(self.service.verify_reference(public))
        good = json.loads((self.service.directory / "state.json").read_text())
        for change in [{}, {**good, "latest_record": []}, {**good, "record_sha256": 1},
                       {**good, "protection": {}}, {**good, "episode": True}, {**good, "episode": -1}]:
            with self.subTest(state=change):
                write_private_json(self.service.directory / "state.json", change)
                self.assertFalse(self.service.admission())
                self.assertFalse(self.service.verify_reference(ref))

    def test_evidence_path_and_unsafe_log_never_validate_a_self_consistent_record(self):
        ref, record = self.fixture_verified_record()
        outside = self.root / "outside.log"
        outside.write_bytes(b"outside")
        outside.chmod(0o600)
        unsafe = self.service.directory / "unsafe.log"
        unsafe.symlink_to(outside)
        hardlink = self.service.directory / "hardlinked.log"
        os.link(outside, hardlink)
        oversized = self.service.directory / "oversized.log"
        oversized.write_bytes(b"x" * (1 + (1 << 20)))
        oversized.chmod(0o600)
        fifo = self.service.directory / "pipe.log"
        os.mkfifo(fifo, 0o600)
        for name in (str(outside), "../outside.log", "unsafe.log", "hardlinked.log", "oversized.log", "pipe.log"):
            with self.subTest(path=name):
                bad = deepcopy(record)
                bad["evidence"] = [{"path": name, "sha256": sha256(b"outside").hexdigest()}]
                write_private_json(self.service.directory / (ref + ".json"), bad)
                write_private_json(self.service.directory / "state.json", {
                    "latest_record": ref, "record_sha256": _digest(bad), "protection": None, "episode": 0})
                self.assertFalse(self.service.admission())
                self.assertFalse(self.service.verify_reference(ref))

    def test_verify_missing_policy_and_corrupt_state_never_clear_or_fake_pass(self):
        unavailable = SummaryCompatibility(self.root / "missing", identity=lambda: (_ for _ in ()).throw(FileNotFoundError()))
        result = unavailable.verify_scripted()
        self.assertIn("runtime_identity_unavailable", result["unknowns"])
        directory = self.root / "bad-data"
        ensure_private_dir(directory)
        ensure_private_dir(directory / "summary-compatibility")
        state = directory / "summary-compatibility/state.json"
        state.write_text("broken")
        state.chmod(0o600)
        with patch("builtins.print"):
            self.assertEqual(main(["logs", "summary-compat", "verify", "--scripted", "--json", "--data-dir", str(directory)]), 1)
        self.assertEqual(state.read_text(), "broken")

    def test_runtime_only_copy_same_policy_and_closure_without_development_inputs(self):
        for relative in ("src/workbench", "omp_bridge"):
            shutil.copytree(ROOT / relative, self.root / relative, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "pyproject.toml", self.root / "pyproject.toml")
        original, isolated = collect_runtime_identity(ROOT), collect_runtime_identity(self.root)
        self.assertEqual(original["closure_entries"], isolated["closure_entries"])
        self.assertEqual(isolated["unknowns"], [])
        self.assertEqual(isolated["policy_sha256"], sha256((ROOT / POLICY_PATH).read_bytes()).hexdigest())
        self.assertFalse(any((self.root / name).exists() for name in ("tools", "tests", "docs", ".workflow")))
        file = self.root / "src/workbench/backend/new_unexplained.py"
        file.write_text("import importlib\ndef test(name): return importlib.import_module(name)\n")
        self.assertIn("unknown_dynamic_import:src/workbench/backend/new_unexplained.py", collect_runtime_identity(self.root)["unknowns"])
        file.write_text("import tests.backend.test_run\n")
        self.assertTrue(any(x.startswith("development_import:") for x in collect_runtime_identity(self.root)["unknowns"]))

    def test_runtime_only_checkout_cli_runs_without_original_assets_or_development_imports(self):
        for relative in ("src/workbench", "omp_bridge"):
            shutil.copytree(ROOT / relative, self.root / relative, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "pyproject.toml", self.root / "pyproject.toml")
        environment = {"PATH": str(self.root / "no-executables"), "PYTHONPATH": str(self.root / "src"),
                       "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(self.root / "home")}
        data = self.root / "data"
        result = subprocess.run([sys.executable, "-m", "workbench.backend.cli", "logs", "summary-compat", "verify",
                                 "--scripted", "--json", "--data-dir", str(data)],
                                cwd=self.root, env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["result"], "unavailable")
        self.assertTrue(all(value == 0 for value in response["io"].values()))
        self.assertFalse((self.root / "home").exists())
        self.assertFalse((data / "raw-v2").exists())
        self.assertFalse((data / "tasks.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
