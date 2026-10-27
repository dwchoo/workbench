"""TUI Hub list ``X`` = stop every running project (2026-10-27 사용자 요청: 전체 끄기), seam S7 with fakes.

The model is pure (rows in, action out). The app side runs ``projects.power_off_all`` for real with
``power_off_request``/``power_off_confirm`` replaced by a fake backend table, ``input`` and the terminal
by fakes: the question text, how many times it is asked, which projects are confirmed, and the notice.
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workbench.hub import projects  # noqa: E402
from workbench.ui.hub_list import app  # noqa: E402
from workbench.ui.hub_list.model import HubListModel, PowerOffAll  # noqa: E402
from workbench.ui.hub_list.view import FOOTER  # noqa: E402

from test_hub_list_model import loaded, row  # noqa: E402


class XKeyInTheModel(unittest.TestCase):
    def test_X_collects_every_on_row_and_only_them(self):
        model = loaded(row("a", "on"), row("b", "off"), row("c", "on"))
        action = model.press("X")
        self.assertIsInstance(action, PowerOffAll)
        self.assertEqual([entry.name for entry in action.entries], ["a", "c"])
        self.assertEqual(action.held, ())

    def test_lowercase_x_still_stops_only_the_selected_project(self):
        model = loaded(row("a", "on"), row("c", "on"))
        self.assertNotIsInstance(model.press("x"), PowerOffAll)

    def test_X_with_nothing_on_says_so_and_does_nothing(self):
        model = loaded(row("a", "off"), row("b", "mismatch", "root_missing"))
        self.assertIsNone(model.press("X"))
        self.assertEqual(model.notice, "켜진 WB 프로젝트가 없습니다.")

    def test_unknown_rows_are_carried_as_unconfirmed_never_as_off(self):
        model = loaded(row("a", "on"), row("u", "unknown", "timeout"))
        action = model.press("X")
        self.assertEqual([entry.name for entry in action.entries], ["a"])
        self.assertEqual([(item.entry.name, item.status) for item in action.held], [("u", projects.UNCONFIRMED)])

    def test_only_unknown_rows_is_not_the_none_running_notice(self):
        model = loaded(row("u", "unknown", "timeout"))
        self.assertIsNone(model.press("X"))
        self.assertIn("상태 불명", model.notice)
        self.assertNotEqual(model.notice, "켜진 WB 프로젝트가 없습니다.")

    def test_X_while_the_hub_is_lost_is_not_a_stop(self):
        model = loaded(row("a", "on"))
        model.set_disconnected()
        self.assertIsNone(model.press("X"))

    def test_the_footer_names_the_key(self):
        self.assertIn("X 전체 끄기", FOOTER)


class FakeBackends:
    """``power_off_request``/``power_off_confirm`` over a table of name -> (active, verified)."""

    def __init__(self, table):
        self.table, self.confirmed = table, []

    def request(self, entry):
        active, _ = self.table[entry.name]
        return projects.Outcome(True, data={"token": f"t-{entry.name}", "active": active, "expires_in": 120,
                                            "backend": {"pid": 1, "start_ticks": 1}})

    def confirm(self, entry, token, out=None, backend=None):
        self.confirmed.append((entry.name, token))
        verified = self.table[entry.name][1]
        return projects.Outcome(True, data={"result": {"verified": True}}) if verified else projects.Outcome(
            False, projects.UNVERIFIED, "host_shell survived")


class XKeyInTheApp(unittest.TestCase):
    TABLE = {"alpha": ([{"kind": "task_run", "task_id": "T-alpha"}], True), "beta": ([], True),
             "bad": ([], False)}

    def run_action(self, answer, names=("alpha", "beta"), table=None):
        backends = FakeBackends(table or self.TABLE)
        model = HubListModel()
        model.set_rows([row(name, "on") for name in names])
        action = model.press("X")
        screen = io.StringIO()
        with contextlib.redirect_stdout(screen), mock.patch("builtins.input", return_value=answer) as asked, \
                mock.patch.object(projects, "power_off_request", side_effect=backends.request), \
                mock.patch.object(projects, "power_off_confirm", side_effect=backends.confirm), \
                mock.patch.object(app, "_wait_enter") as waited, mock.patch.object(app.termios, "tcflush"):
            app.execute(action, model)
        return model, screen.getvalue(), backends, asked, waited

    def test_y_asks_once_listing_every_project_and_its_work_then_stops_them_all(self):
        model, screen, backends, asked, waited = self.run_action("y")
        self.assertEqual(asked.call_count, 1)
        self.assertIn("[y/N]", asked.call_args.args[0])
        for text in ('"alpha"', '"beta"', "T-alpha"):
            self.assertIn(text, screen)
        self.assertEqual([name for name, _ in backends.confirmed], ["alpha", "beta"])
        self.assertIn('"alpha": 껐습니다', screen)
        self.assertIn('"beta": 껐습니다', screen)
        self.assertEqual(model.notice, "2개 프로젝트를 껐습니다")
        waited.assert_called_once()  # the result lines are read before the list redraws

    def test_n_stops_nothing(self):
        model, screen, backends, asked, _ = self.run_action("n")
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(backends.confirmed, [])
        self.assertIn("취소", model.notice)

    def test_any_other_answer_than_y_stops_nothing(self):
        for answer in ("", "yes", "no", "x"):
            with self.subTest(answer=answer):
                *_, backends, _, _ = self.run_action(answer)
                self.assertEqual(backends.confirmed, [])

    def test_a_failure_is_shown_the_others_still_stop_and_the_list_waits_for_enter(self):
        model, screen, backends, _, waited = self.run_action("y", names=("alpha", "bad", "beta"))
        self.assertEqual([name for name, _ in backends.confirmed], ["alpha", "bad", "beta"])
        self.assertIn('"bad": 끄지 못했습니다 (unverified: host_shell survived)', screen)
        waited.assert_called_once()  # the results are read before the list redraws
        self.assertIn("1개를 끄지 못했습니다", model.notice)
        self.assertIn("2개", model.notice)


if __name__ == "__main__":
    unittest.main()
