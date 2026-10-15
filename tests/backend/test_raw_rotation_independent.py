"""Independent activation/policy fixtures, never actual verifier/MODEL pass."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests.backend import test_summary_compatibility as compatibility_fixture
from workbench.backend.cli import main
from workbench.backend.paths import DataLayout
from workbench.backend.raw_rotation import RawRotationPolicy
from workbench.backend.service import Backend
from workbench.storage.log_raw.port import DisabledSegmentedRawPort
from workbench.storage.log_raw.segments import SegmentedRawStore


def validator_fixture(test):
    fixture = compatibility_fixture.CompatibilityCoreTests(
        "test_version_only_drift_and_unrelated_identity_never_create_current_pass")
    fixture.setUp()
    test.addCleanup(fixture.doCleanups)
    reference, record = fixture.fixture_verified_record()
    return fixture, reference, record


def backend_fixture(test, fixture):
    backend = Backend.__new__(Backend)
    backend.layout = DataLayout(fixture.root)
    backend.segmented_raw_port = backend._owned_segmented_store = None
    backend._raw_port_lock = threading.RLock()
    backend._raw_rotation_error = None
    backend.raw_rotation = RawRotationPolicy(backend.layout.raw_rotation, fixture.service)
    test.addCleanup(backend._close_owned_raw_store)
    return backend


class RawActivationIndependentTests(unittest.TestCase):
    def test_actual_cli_warning_and_both_live_gates_publish_only_policy(self):
        fixture, reference, _ = validator_fixture(self)
        backend = backend_fixture(self, fixture)
        output, error = StringIO(), StringIO()
        # CLI owns its newly constructed service in production. This fixture
        # returns an existing service, so restore its identity callback on exit.
        with patch.object(fixture.service, "omp_identity", fixture.service.omp_identity), \
             patch("workbench.backend.summary_compatibility.SummaryCompatibility",
                   return_value=fixture.service), \
             patch("workbench.backend.cli.current_omp_identity", side_effect=lambda: deepcopy(fixture.omp)), \
             patch("workbench.backend.summary_compat_identity.observe_configured", return_value={
                 "result": "pass", "cleanup_complete": True, "omp": deepcopy(fixture.omp)}) as getter, \
             patch("subprocess.Popen", side_effect=AssertionError("Native forbidden in policy fixture")), \
             patch.object(fixture.service, "verify_reference", wraps=fixture.service.verify_reference) as verify, \
             patch.object(fixture.service, "admission", wraps=fixture.service.admission) as admission, \
             redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(main(["logs", "raw-rotation", "activate", "--yes", "--evidence",
                                   reference, "--data-dir", str(fixture.root)]), 0)
        self.assertIn("경고", error.getvalue())
        self.assertIn("100MB × 5", error.getvalue())
        self.assertIn("삭제될 수", error.getvalue())
        self.assertIn("기존 source와 legacy 파일은 전환하거나 삭제하지 않습니다", error.getvalue())
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(admission.call_count, 2)
        getter.assert_called_once()
        self.assertEqual(backend.raw_rotation.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(backend.layout.raw_v2.exists())
        self.assertIn(reference, output.getvalue())
        activated = backend.raw_rotation.path.read_bytes()
        restored = RawRotationPolicy(backend.raw_rotation.path, fixture.service)
        self.assertTrue(restored.admits_new_source())
        with patch.object(fixture.service, "admission", side_effect=AssertionError("status admission")), \
             patch.object(fixture.service, "verify_reference", side_effect=AssertionError("status verify")):
            status = restored.status()
        self.assertTrue(status["observationOnly"])
        self.assertEqual(status["evidence"], reference)
        self.assertEqual(backend.raw_rotation.path.read_bytes(), activated)

    def test_admitted_version_drift_cannot_first_activate_without_exact_reference(self):
        fixture, reference, _ = validator_fixture(self)
        backend = backend_fixture(self, fixture)
        fixture.omp["version"] = "future"
        self.assertTrue(fixture.service.admission(), "operating drift is distinct from first activation")
        self.assertFalse(fixture.service.verify_reference(reference))
        with patch.object(fixture.service, "_mutation", side_effect=AssertionError("refusal allocated lock")):
            with self.assertRaises(ValueError):
                backend.raw_rotation.activate(reference)
        self.assertFalse(backend.raw_rotation.path.exists())
        self.assertFalse(backend.raw_rotation.path.with_suffix(".lock").exists())
        self.assertIsNone(backend._new_raw_port())
        self.assertFalse(backend.layout.raw_v2.exists())

    def test_shared_small_store_keeps_selected_task_command_and_legacy_cohorts(self):
        fixture, reference, _ = validator_fixture(self)
        backend = backend_fixture(self, fixture)
        terminal = fixture.root / "workflow" / "terminal"
        terminal.mkdir(parents=True, mode=0o700)
        terminal.parent.chmod(0o700)
        legacy = terminal / (str(uuid4()) + ".log")
        legacy.write_bytes(b"legacy remains"); legacy.chmod(0o600)
        self.assertIsNone(backend._new_raw_port())
        backend.raw_rotation.activate(reference)
        # Supported tiny engine seam; substantive eviction without a 500MB fixture.
        with patch("workbench.backend.service.SegmentedRawStore",
                   side_effect=lambda root: SegmentedRawStore(root, segment_limit=1024, segment_count=2)):
            selected = backend._new_raw_port()
        second = backend._new_raw_port()
        self.assertIs(selected, second)
        task_id, run_id, command_id = str(uuid4()), str(uuid4()), str(uuid4())
        run = fixture.root / "workflow" / "runs" / run_id
        run.mkdir(parents=True, mode=0o700); run.parent.chmod(0o700)
        _, task = selected.prepare_task(run, task_id, 3, run_id)
        _, command = second.prepare_command(terminal, command_id)
        self.assertEqual(task["source"]["revision"], 3)
        self.assertEqual(task["source"]["runId"], run_id)
        selected.append(task, b"task first")
        selected.append(command, b"command first")
        fixture.service.protect({"reason": "compatibility_failed"})
        self.assertIsNone(backend._new_raw_port())
        for _ in range(8):
            appended = selected.append(task, b"x" * 512)
            self.assertNotEqual(appended.get("status"), "storage_fault")
        second.append(command, b"command after protection")
        self.assertGreater(second.status(command)["observed_bytes"], len(b"command first"))
        old = selected.read(task, 0, len(b"task first"))
        self.assertTrue(old["read"]["missing"])
        self.assertEqual({g["reason"] for g in old["read"]["missing"]}, {"evicted"})
        self.assertEqual(legacy.read_bytes(), b"legacy remains")
        self.assertEqual(backend.raw_status()["rotation"]["newSourcePolicy"], "legacy-stop")
        backend._close_owned_raw_store()
        restored = backend_fixture(self, fixture)
        self.assertTrue(restored.raw_rotation.status()["activated"])
        self.assertIsNone(restored._new_raw_port(), "protection survives a policy/backend reopen")


if __name__ == "__main__":
    unittest.main()
