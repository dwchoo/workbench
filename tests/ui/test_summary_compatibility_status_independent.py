"""Actual CLI/TUI status rendering only; no operating model or verifier proof."""
from copy import deepcopy
from io import StringIO
import unittest

from support import FakeSender, snapshot
from workbench.backend.cli import _print_summary
from workbench.ui.product.model import ProductModel, _clip
from workbench.ui.status_workbench.projection import summary_compatibility_text


class StatusConsumerIndependentTests(unittest.TestCase):
    def test_actual_status_lines_and_cli_summary_preserve_observations_and_warnings(self):
        states = {
            "verified": "검증 완료",
            "version_drift_unverified": "현재 버전 미검증",
            "unrelated_change_observed": "비관련 변경 관측",
            "revalidation_required": "재검증 필요",
            "protected": "보호 상태",
            "unavailable": "확인 불가",
        }
        full_labels = {**states, "version_drift_unverified": "버전 변경 · 현재 버전 미검증",
                       "protected": "보호 상태 · 요약 보류"}
        for state, label in states.items():
            with self.subTest(state=state):
                value = snapshot()
                value["important_logs"] = {
                    "countState": "known", "databaseCount": 2, "totalPhysicalBytes": 42,
                    "summary": {"state": "delayed"},
                    "compatibility": {
                        "state": state, "reason": "cause\n\x1b[2J\rnext",
                        "lastCheckedAt": "2026-10-12T01:00:00+00:00\n\x1b[H",
                    },
                }
                before = deepcopy(value)
                sender = FakeSender()
                model = ProductModel(sender, 30, 200, clock=lambda: 1000.0)
                model.apply_snapshot(value)
                sent_before = deepcopy(sender.sent)
                for _ in range(3):
                    output = StringIO()
                    _print_summary(value, output)
                    cli = output.getvalue()
                    first, second = model.status_lines()
                    full = model.important_compatibility_text()
                    expected_full = (f"중요 로그 요약 호환: {full_labels[state]} · "
                                     "마지막 검사 2026-10-12T01:00:00+00:00 [H · 사유 cause [2J next")
                    if state in ("protected", "revalidation_required", "unavailable"):
                        expected_full += " · 명시 검증: logs summary-compat verify --scripted"
                    self.assertEqual(full, expected_full)
                    self.assertEqual(summary_compatibility_text(value["important_logs"]["compatibility"]),
                                     expected_full)
                    self.assertIn(expected_full, model.backend_recovery_text())
                    self.assertIn(expected_full, model.recovery_text())
                    self.assertIn(expected_full, cli)
                    for text in (cli, full):
                        self.assertIn(label, text)
                        self.assertIn("마지막 검사 2026-10-12T01:00:00+00:00", text)
                        self.assertIn("사유 cause", text)
                        self.assertNotIn("\x1b", text)
                        self.assertNotIn("\r", text)
                        self.assertNotIn("현재 사용 가능", text)
                        if state == "version_drift_unverified":
                            self.assertNotIn("검증 완료", text)
                            self.assertNotIn("verify --scripted", text)
                        if state in ("protected", "revalidation_required", "unavailable"):
                            self.assertIn("logs summary-compat verify --scripted", text)
                    for text in (first, second):
                        self.assertNotIn("\x1b", text)
                        self.assertNotIn("\r", text)
                        self.assertNotIn("현재 사용 가능", text)
                    self.assertIn(label, second)
                    if state == "version_drift_unverified":
                        self.assertNotIn("검증 완료", second)
                        self.assertNotIn("verify --scripted", second)
                    self.assertNotIn("\n", second)
                    self.assertIn("DB 2개", first)
                    self.assertIn("DB 2개", cli)
                    self.assertIn("요약 지연", second)
                self.assertEqual(value, before)
                self.assertEqual(sender.sent, sent_before)

    def test_actual_status_lines_keep_summary_error_and_unchecked_state(self):
        value = snapshot()
        value["important_logs"] = {
            "summary": {"state": "error", "reason": "fixture_error"},
            "compatibility": {"state": "verified", "lastCheckedAt": None},
        }
        sender = FakeSender()
        model = ProductModel(sender, 30, 200, clock=lambda: 1000.0)
        model.apply_snapshot(value)
        before = deepcopy(value)
        sent_before = deepcopy(sender.sent)
        second = model.status_lines()[1]
        expected_full = "중요 로그 요약 호환: 미확인 · 아직 검사하지 않음 · 마지막 검사 없음"
        full = model.important_compatibility_text()
        self.assertEqual(full, expected_full)
        self.assertEqual(summary_compatibility_text(value["important_logs"]["compatibility"]), expected_full)
        self.assertIn(expected_full, model.backend_recovery_text())
        self.assertIn(expected_full, model.recovery_text())
        output = StringIO()
        _print_summary(value, output)
        self.assertIn(expected_full, output.getvalue())
        self.assertIn("요약 호환: 미확인", second)
        self.assertNotIn("검증 완료", second)
        self.assertNotIn("검증 완료", full)
        self.assertNotIn("검증 완료", output.getvalue())
        self.assertNotIn("현재 사용 가능", second)
        self.assertIn("요약 오류", second)
        self.assertEqual(value, before)
        self.assertEqual(sender.sent, sent_before)

    def test_170_display_columns_keep_waiting_report_pause_and_seven_compatibility_observations(self):
        states = {
            "verified": "검증 완료", "version_drift_unverified": "현재 버전 미검증",
            "unrelated_change_observed": "비관련 변경 관측", "revalidation_required": "재검증 필요",
            "protected": "보호 상태", "unavailable": "확인 불가", "unchecked": "미확인",
        }
        for state, label in states.items():
            with self.subTest(state=state):
                value = snapshot(automation="paused")
                value["task"] = {"kind": "experiment", "summary": "P2 ticking run",
                                 "status": "waiting_report", "held_reason": "paused"}
                value["usage"] = {"retries_used": 0, "retry_limit": 3, "review_count": 2,
                                  "model": {"tokens_observed": "unknown", "tokens_estimated": "unknown"}}
                value["important_logs"] = {
                    "countState": "known", "databaseCount": 1,
                    "compatibility": {"state": state if state != "unchecked" else "verified",
                                      "lastCheckedAt": "2026-10-12T01:00:00+00:00" if state != "unchecked" else None,
                                      "reason": "long diagnostic reason " * 20},
                }
                before = deepcopy(value)
                sender = FakeSender()
                model = ProductModel(sender, 30, 170, clock=lambda: 1000.0)
                model.apply_snapshot(value)
                sent_before = deepcopy(sender.sent)
                first, second = model.status_lines()
                # Same product display-column clip and cols - 1 budget as the
                # renderer; asserting the unclipped raw line would hide this regression.
                visible_first = _clip(first, model.cols - 1)
                visible_second = _clip(second, model.cols - 1)
                self.assertIn("자동화: paused", visible_first)
                self.assertIn("보고 대기", visible_second)
                self.assertIn("일시정지됨", visible_second)
                self.assertIn(label, visible_second)
                self.assertNotIn("현재 사용 가능", visible_first + visible_second)
                if state in ("version_drift_unverified", "unchecked"):
                    self.assertNotIn("검증 완료", visible_second)
                self.assertEqual(value, before)
                self.assertEqual(model.state, before)
                self.assertEqual(sender.sent, sent_before)


if __name__ == "__main__":
    unittest.main()
