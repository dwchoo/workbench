"""DVL-07 warning is tied to the latest observed catalog count, not acknowledgement."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from support import FakeSender, snapshot  # noqa: E402
from workbench.ui.product.model import ProductModel  # noqa: E402


def model_with(important: dict) -> ProductModel:
    model = ProductModel(FakeSender(), 30, 160, clock=lambda: 1000.0)
    value = snapshot()
    value["important_logs"] = important
    model.apply_snapshot(value)
    return model


class Dvl07WarningContract(unittest.TestCase):
    def test_known_multiple_count_persists_across_snapshot_ack_and_reattach(self) -> None:
        important = {"countState": "known", "databaseCount": 3,
                     "totalPhysicalBytes": 123456, "cleanupNeeded": True,
                     "summary": {"state": "unknown"}}
        model = model_with(important)
        first = model.important_log_warning()
        self.assertIn("3개", first)
        self.assertIn("123456", first)
        self.assertIn("delete-db --db UUID", first)
        self.assertIn("중요 로그 DB 3개", model.status_lines()[0])
        refreshed = snapshot()
        refreshed["important_logs"] = dict(important)
        model.apply_snapshot(refreshed)
        self.assertEqual(model.important_log_warning(), first)
        self.assertEqual(model_with(important).important_log_warning(), first)
        self.assertIn("미확인", model.important_summary_text())

    def test_unknown_is_not_one_and_known_one_is_the_only_clear_state(self) -> None:
        unknown = {"countState": "unknown", "databaseCount": None,
                   "totalPhysicalBytes": None, "cleanupNeeded": None,
                   "summary": {"state": "unknown"}}
        model = model_with(unknown)
        self.assertIn("미확인", model.important_log_warning())
        self.assertNotIn("DB 1개", model.important_log_warning())
        known_one = snapshot()
        known_one["important_logs"] = {"countState": "known", "databaseCount": 1,
                                       "totalPhysicalBytes": 500, "cleanupNeeded": False,
                                       "summary": {"state": "unknown"}}
        model.apply_snapshot(known_one)
        self.assertEqual(model.important_log_warning(), "")
        unknown_again = snapshot()
        unknown_again["important_logs"] = unknown
        model.apply_snapshot(unknown_again)
        self.assertIn("미확인", model.important_log_warning())

    def test_summary_error_and_delay_are_visible_without_inventing_health(self) -> None:
        for state, expected in (("error", "오류"), ("delayed", "지연")):
            with self.subTest(state=state):
                model = model_with({"countState": "known", "databaseCount": 1,
                                    "totalPhysicalBytes": 100, "summary": {"state": state}})
                self.assertIn(expected, model.important_summary_text())
        self.assertIn("미확인", model_with({"countState": "known", "databaseCount": 1,
                                           "totalPhysicalBytes": 100,
                                           "summary": {"state": "unknown"}}).important_summary_text())


if __name__ == "__main__":
    unittest.main()
