"""Actual CLI/TUI status rendering only; no operating model or verifier proof."""
from copy import deepcopy
from io import StringIO
import unittest

from support import FakeSender, snapshot
from workbench.backend.cli import _print_summary
from workbench.ui.product.model import ProductModel


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
                    for text in (cli, second):
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
        second = model.status_lines()[1]
        self.assertIn("미확인 · 아직 검사하지 않음", second)
        self.assertNotIn("검증 완료", second)
        self.assertIn("요약 오류", second)


if __name__ == "__main__":
    unittest.main()
