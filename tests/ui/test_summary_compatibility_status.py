"""DVL-14 status consumers only; synthetic records are not operating MODEL evidence."""
from copy import deepcopy
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
from support import FakeSender, snapshot
from workbench.backend.cli import _print_important_warning, main
from workbench.backend.service import Backend
from workbench.backend.summary_compatibility import SummaryCompatibility
from workbench.ui.product.model import ProductModel
from workbench.ui.status_workbench.projection import summary_compatibility_text

STAMP = "2026-10-12T01:00:00+00:00"


class CompatibilityStatusTests(unittest.TestCase):
    def test_unchecked_snapshot_is_read_only_without_runtime_hash_or_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "not-created"
            identity = Mock(side_effect=AssertionError("display must not hash runtime"))
            service = SummaryCompatibility(directory, identity=identity)
            backend = Backend.__new__(Backend)
            backend.summary_compatibility = service
            for _ in range(3):
                observed = backend._summary_compatibility_observation()
                self.assertEqual(observed["state"], "unavailable")
                self.assertIsNone(observed["lastCheckedAt"])
                self.assertNotIn("admitted", observed)
                observed["reason"] = "caller mutation"
            self.assertEqual(service.observed_status()["reason"], "compatibility_not_checked")
            self.assertFalse(directory.exists())
            identity.assert_not_called()

    def test_last_check_is_detached_and_admission_still_performs_live_check(self):
        # Reuse the full synthetic validator fixture without claiming a product verification pass.
        from tests.backend.test_summary_compatibility import CompatibilityCoreTests
        fixture = CompatibilityCoreTests("test_version_only_drift_and_unrelated_identity_never_create_current_pass")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture_verified_record()
        identity = Mock(side_effect=lambda: deepcopy(fixture.ident))
        fixture.service.identity = identity
        returned = fixture.service.status()
        checked = fixture.service.observed_status()["lastCheckedAt"]
        self.assertEqual(returned["state"], "verified")
        self.assertEqual(identity.call_count, 1)
        returned["verified_omp"]["version"] = "caller changed"
        observed = fixture.service.observed_status()
        self.assertEqual(observed["verified_omp"]["version"], "18.8.6")
        observed["current_omp"]["version"] = "another caller changed"
        fixture.ident["closure_digest"] = "7" * 64
        for _ in range(3):
            self.assertEqual(fixture.service.observed_status()["state"], "verified")
            self.assertEqual(fixture.service.observed_status()["lastCheckedAt"], checked)
        self.assertEqual(identity.call_count, 1)
        self.assertFalse(fixture.service.admission())
        self.assertEqual(identity.call_count, 2)
        self.assertEqual(fixture.service.observed_status()["state"], "revalidation_required")

    def test_protection_observation_does_not_consume_notice_or_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            identity = Mock(side_effect=AssertionError("protection display must not hash"))
            service = SummaryCompatibility(Path(temporary) / "compat", identity=identity)
            service.protect({"reason": "compatibility_failed"})
            before = (service.directory / "state.json").read_bytes()
            for _ in range(3):
                self.assertEqual(service.observed_status()["state"], "protected")
            self.assertEqual((service.directory / "state.json").read_bytes(), before)
            self.assertIsNotNone(service.take_notice())
            identity.assert_not_called()

    def test_backend_missing_observation_port_does_not_fall_back_to_admission(self):
        forbidden = Mock(side_effect=AssertionError("display must not use admission"))
        backend = Backend.__new__(Backend)
        backend.summary_compatibility = type("AdmissionOnly", (), {
            "status": forbidden, "admission": forbidden, "take_notice": forbidden})()
        self.assertIsNone(backend._summary_compatibility_observation()["lastCheckedAt"])
        forbidden.assert_not_called()

    def test_cli_tui_distinguish_six_observed_states_and_preserve_delay_warning(self):
        states = {"verified": "검증 완료", "version_drift_unverified": "현재 버전 미검증",
                  "unrelated_change_observed": "비관련 변경 관측", "revalidation_required": "재검증 필요",
                  "protected": "보호 상태", "unavailable": "확인 불가"}
        for state, label in states.items():
            with self.subTest(state=state):
                observation = {"state": state, "reason": "cause\n\x1b[31m", "lastCheckedAt": STAMP}
                important = {"countState": "known", "databaseCount": 2, "totalPhysicalBytes": 42,
                             "summary": {"state": "delayed"}, "compatibility": observation}
                sender = FakeSender()
                model = ProductModel(sender, 30, 160, clock=lambda: 1000.0)
                value = snapshot()
                value["important_logs"] = important
                model.apply_snapshot(value)
                sent_before = deepcopy(sender.sent)
                output = StringIO()
                _print_important_warning(value, output)
                cli = output.getvalue()
                tui = model.important_compatibility_text()
                self.assertIn(label, cli)
                self.assertIn(label, tui)
                self.assertIn("마지막 검사 " + STAMP, tui)
                self.assertIn("DB 2개", cli)
                self.assertIn("DB 2개", model.important_log_warning())
                self.assertIn("요약 지연", model.backend_recovery_text())
                self.assertNotIn("\x1b", tui)
                self.assertNotIn("\n", tui)
                self.assertNotIn("현재 사용 가능", tui)
                self.assertEqual(sender.sent, sent_before)
                if state == "version_drift_unverified":
                    self.assertNotIn("검증 완료", tui)
                    self.assertNotIn("verify --scripted", tui)
                if state in ("protected", "revalidation_required", "unavailable"):
                    self.assertIn("logs summary-compat verify --scripted", tui)

    def test_logs_status_reuses_existing_snapshot_and_offline_reports_unknown(self):
        from workbench.backend.paths import DataLayout
        for running in (None, {"important_logs": {"compatibility": {
                "state": "version_drift_unverified", "lastCheckedAt": STAMP}}}):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as temporary:
                layout = DataLayout(Path(temporary) / "not-created")
                output = StringIO()
                with patch("workbench.backend.cli._layout", return_value=layout), \
                     patch("workbench.backend.cli._running_snapshot", return_value=running), \
                     patch("workbench.storage.important_log.ImportantLogReader") as reader, \
                     patch.object(SummaryCompatibility, "admission", side_effect=AssertionError("admission")), \
                     patch.object(SummaryCompatibility, "status", side_effect=AssertionError("live hash")), \
                     patch.object(SummaryCompatibility, "verify_scripted", side_effect=AssertionError("verify")), \
                     patch.object(SummaryCompatibility, "take_notice", side_effect=AssertionError("notice")), \
                     patch("sys.stdout", output):
                    reader.return_value.status.return_value = {"complete": True, "databaseCount": 1,
                                                               "physicalBytes": 42, "active": "fixture"}
                    self.assertEqual(main(["logs", "important", "status"]), 0)
                self.assertIn("현재 버전 미검증" if running else "미확인 · 아직 검사하지 않음", output.getvalue())
                self.assertFalse(layout.root.exists())

    def test_no_timestamp_never_displays_verified(self):
        text = summary_compatibility_text({"state": "verified"})
        self.assertIn("미확인", text)
        self.assertNotIn("검증 완료", text)


if __name__ == "__main__":
    unittest.main()
