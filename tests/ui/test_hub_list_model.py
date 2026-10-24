"""TUI Hub list model (HB-08, seam S7): a pure state machine fed hub_v1 ``list`` rows and key names.

Expected values come from BRIEF HB-AC-09/10/13/16 and the SPEC "TUI Hub 목록"/label table, and
from the rows each test builds. No curses, no Hub, no backend.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workbench.hub.presentation import row_cells  # noqa: E402
from workbench.ui.hub_list.model import (EMPTY_TEXT, HUB_LOST_TEXT, Attach, HubListModel,  # noqa: E402
                                         PowerOff, PowerOn, Quit, Refresh)


def row(name: str, power: str = "on", detail: str | None = None, **fields) -> dict:
    base = {"root": f"/work/{name}", "name": name, "project_id": f"id-{name}", "power": power,
            "power_detail": detail, "task": None, "command": None, "waits": [],
            "pending_requests": None, "last_activity": None, "backend": None, "observed_at": 0.0}
    base.update(fields)
    return base


def loaded(*rows: dict) -> HubListModel:
    model = HubListModel()
    model.set_rows(list(rows))
    return model


class RowsAndSelection(unittest.TestCase):
    def test_first_row_is_selected_and_moves_stop_at_the_ends(self):
        model = loaded(row("a"), row("b"), row("c"))
        self.assertEqual(model.selected_row["name"], "a")
        for key, expected in (("up", "a"), ("down", "b"), ("j", "c"), ("down", "c"), ("k", "b"), ("up", "a")):
            self.assertIsNone(model.press(key))
            self.assertEqual(model.selected_row["name"], expected, key)

    def test_selection_follows_the_project_across_a_refresh(self):
        model = loaded(row("a"), row("b"), row("c"))
        model.press("down")
        model.set_rows([row("c"), row("b"), row("z")])
        self.assertEqual(model.selected_row["name"], "b")

    def test_selection_is_clamped_when_the_selected_row_vanishes(self):
        model = loaded(row("a"), row("b"), row("c"))
        model.press("down")
        model.press("down")
        model.set_rows([row("a"), row("b")])
        self.assertEqual(model.selected_row["name"], "b")

    def test_empty_list_has_no_selection_and_keys_do_nothing(self):
        model = loaded()
        self.assertIsNone(model.selected_row)
        for key in ("up", "down", "enter", "o", "x"):
            self.assertIsNone(model.press(key))


class EnterOnAnOnRow(unittest.TestCase):
    def test_enter_on_an_on_row_attaches_that_project(self):
        model = loaded(row("a", "off"), row("b", "on"))
        model.press("down")
        action = model.press("enter")
        self.assertIsInstance(action, Attach)
        self.assertEqual((action.entry.root, action.entry.project_id, action.entry.name),
                         ("/work/b", "id-b", "b"))
        self.assertIsNone(model.modal)


class EnterOnAnOffRow(unittest.TestCase):
    def test_enter_asks_before_starting_and_y_starts_and_attaches(self):
        model = loaded(row("a", "on"), row("proj", "off"))
        model.press("down")
        self.assertIsNone(model.press("enter"))
        self.assertEqual(model.modal.text, '"proj"을(를) 켤까요? [y/N]')
        action = model.press("y")
        self.assertIsInstance(action, PowerOn)
        self.assertTrue(action.attach)
        self.assertEqual((action.entry.root, action.entry.project_id), ("/work/proj", "id-proj"))
        self.assertIsNone(model.modal)

    def test_modal_swallows_navigation_and_refresh_keys(self):
        model = loaded(row("a", "off"), row("b", "on"))
        model.press("enter")
        for key in ("down", "j", "r", "q", "o", "x"):
            self.assertIsNone(model.press(key), key)
            self.assertIsNone(model.modal, key)
            self.assertEqual(model.selected_row["name"], "a", key)
            model.press("enter")  # reopen for the next key
            self.assertIsNotNone(model.modal)


class ModalDecline(unittest.TestCase):
    def test_every_key_but_y_declines_and_keeps_the_selection(self):
        for key in ("n", "N", "esc", "enter", "q", "a", " ", "Y"):
            model = loaded(row("a", "on"), row("b", "off"))
            model.press("down")
            model.press("enter")
            self.assertIsNone(model.press(key), key)
            self.assertIsNone(model.modal, key)
            self.assertEqual(model.selected_row["name"], "b", key)

    def test_a_refresh_during_the_question_never_retargets_it(self):
        model = loaded(row("a", "off"), row("b", "off"))
        model.press("enter")
        model.set_rows([row("b", "off"), row("a", "off")])  # the list reorders under the question
        action = model.press("y")
        self.assertEqual(action.entry.root, "/work/a")

    def test_the_question_closes_when_its_row_vanishes(self):
        model = loaded(row("a", "off"), row("b", "off"))
        model.press("enter")
        model.set_rows([row("b", "off")])
        self.assertIsNone(model.modal)
        self.assertIn('"a"', model.notice)
        self.assertIsNone(model.press("y"))

    def test_the_question_stays_while_its_row_changes_state(self):
        model = loaded(row("a", "off"))
        model.press("enter")
        model.set_rows([row("a", "starting")])
        self.assertIsNotNone(model.modal)


class PowerKeys(unittest.TestCase):
    def test_o_on_an_off_row_asks_then_starts_without_attaching(self):
        model = loaded(row("a", "off"))
        self.assertIsNone(model.press("o"))
        self.assertEqual(model.modal.text, '"a"을(를) 켤까요? [y/N]')
        action = model.press("y")
        self.assertEqual(action, PowerOn(action.entry, False))
        self.assertEqual(action.entry.root, "/work/a")

    def test_x_on_an_on_row_powers_it_off(self):
        model = loaded(row("a", "off"), row("b", "on"))
        model.press("down")
        action = model.press("x")
        self.assertIsInstance(action, PowerOff)
        self.assertEqual(action.entry.root, "/work/b")

    def test_x_on_off_and_o_on_on_do_nothing(self):
        model = loaded(row("a", "off"), row("b", "on"))
        self.assertIsNone(model.press("x"))
        self.assertIsNone(model.modal)
        model.press("down")
        self.assertIsNone(model.press("o"))
        self.assertIsNone(model.modal)

    def test_r_refreshes_and_q_quits(self):
        model = loaded(row("a", "on"))
        self.assertEqual(model.press("r"), Refresh())
        self.assertEqual(model.press("q"), Quit())


class RefusedStates(unittest.TestCase):
    def test_no_state_but_on_and_off_acts_on_enter_o_or_x(self):
        cases = (("mismatch", "root_missing", ("불일치", "root_missing")),
                 ("unknown", None, ("연결 없음/불명",)),
                 ("starting", None, ("시작 중",)),
                 ("closing", None, ("끄는 중",)))
        for power, detail, words in cases:
            for key in ("enter", "o", "x"):
                model = loaded(row("a", power, detail))
                self.assertIsNone(model.press(key), (power, key))
                self.assertIsNone(model.modal, (power, key))
                for word in words:
                    self.assertIn(word, model.notice, (power, key))

    def test_the_notice_goes_away_with_the_next_key(self):
        model = loaded(row("a", "unknown"), row("b", "on"))
        model.press("enter")
        self.assertTrue(model.notice)
        model.press("down")
        self.assertEqual(model.notice, "")

    def test_x_on_off_and_o_on_on_explain_themselves(self):
        model = loaded(row("a", "off"), row("b", "on"))
        model.press("x")
        self.assertIn("꺼져", model.notice)
        model.press("down")
        model.press("o")
        self.assertIn("켜져", model.notice)


class HubConnectionLost(unittest.TestCase):
    def test_rows_stay_but_every_state_reads_unknown_and_nothing_acts(self):
        model = loaded(row("a", "on"), row("b", "off"))
        self.assertEqual(model.banner, "")
        model.set_disconnected()
        self.assertEqual(model.banner, "Hub 연결 없음")
        self.assertEqual(HUB_LOST_TEXT, "Hub 연결 없음")
        self.assertEqual([r["name"] for r in model.rows], ["a", "b"])
        self.assertEqual([row_cells(r, 0.0)[2] for r in model.rows], ["연결 없음/불명"] * 2)
        for key in ("enter", "o", "x"):
            self.assertIsNone(model.press(key), key)
            self.assertIsNone(model.modal, key)

    def test_a_good_poll_clears_the_banner_and_restores_the_states(self):
        model = loaded(row("a", "on"))
        model.set_disconnected()
        model.set_rows([row("a", "on")])
        self.assertEqual(model.banner, "")
        self.assertIsInstance(model.press("enter"), Attach)

    def test_a_loss_closes_an_open_question(self):
        model = loaded(row("a", "off"))
        model.press("enter")
        model.set_disconnected()
        self.assertIsNone(model.modal)

    def test_loss_before_the_first_poll_shows_the_banner_with_no_rows(self):
        model = HubListModel()
        model.set_disconnected()
        self.assertEqual(model.banner, "Hub 연결 없음")
        self.assertEqual(model.rows, [])


class EmptyAndLoading(unittest.TestCase):
    def test_the_empty_list_text_is_the_spec_guidance(self):
        self.assertEqual(EMPTY_TEXT, "등록된 WB 프로젝트가 없습니다. 'wb init <폴더>'로 만들거나 'wb hub add <폴더>'로 등록하세요.")
        model = loaded()
        self.assertEqual(model.body_text, EMPTY_TEXT)
        self.assertIsNone(model.press("enter"))

    def test_before_the_first_poll_the_body_says_it_is_loading(self):
        self.assertEqual(HubListModel().body_text, "불러오는 중…")
        self.assertEqual(loaded(row("a")).body_text, "")


class TableCells(unittest.TestCase):
    def test_header_and_cells_follow_the_spec_columns_and_labels(self):
        model = loaded(
            row("alpha", "on", task={"task_id": "t1", "summary": "고치기", "status": "running"},
                command={"command_id": "c1", "label": "pytest", "state": "running"},
                waits=[{"code": "paused"}], last_activity=100.0),
            row("beta", "mismatch", "root_missing"),
            row("gamma", "off"))
        table = model.table(now=130.0)
        self.assertEqual(table[0], ["이름", "경로", "상태", "Task·단계", "명령", "대기 사유", "요청", "마지막 활동"])
        self.assertEqual(table[1], ["alpha", "/work/alpha", "켜짐", "고치기 · running", "pytest (실행 중)",
                                    "일시정지", "—", "30초 전"])
        self.assertEqual(table[2], ["beta", "/work/beta", "불일치(root_missing)", "—", "—", "—", "—", "—"])
        self.assertEqual(table[3][2], "꺼짐")

    def test_pending_requests_never_shows_zero_before_task_inbox(self):
        self.assertEqual(loaded(row("a", pending_requests=None)).table(0.0)[1][6], "—")
        self.assertEqual(loaded(row("a", pending_requests=3)).table(0.0)[1][6], "3")

    def test_table_cells_equal_the_cli_listing_cells(self):
        from workbench.hub.presentation import render_table
        rows = [row("a", "on"), row("b", "starting")]
        model = loaded(*rows)
        lines = render_table(rows, 5.0).split("\n")
        self.assertEqual(["  ".join(cells).split() for cells in model.table(5.0)], [line.split() for line in lines])


class ScreenLines(unittest.TestCase):
    """The pure part of the view (``screen_lines``); curses drawing itself is the PTY e2e of HB-11."""

    def lines(self, model, rows=12, cols=100, now=0.0):
        from wcwidth import wcswidth
        from workbench.ui.hub_list.view import screen_lines
        lines = screen_lines(model, rows, cols, now)
        self.assertLessEqual(len(lines), rows)
        for line in lines:
            self.assertLessEqual(wcswidth(line.text), cols - 1, line.text)
        return lines

    def test_header_rows_and_the_selected_mark(self):
        model = loaded(row("alpha"), row("beta", "off"))
        model.press("down")
        lines = self.lines(model)
        texts = [line.text for line in lines]
        header = next(i for i, line in enumerate(lines) if line.kind == "header")
        self.assertTrue(texts[header].lstrip().startswith("이름"))
        self.assertIn("alpha", texts[header + 1])
        self.assertEqual([line.kind for line in lines[header + 1:header + 3]], ["row", "selected"])
        self.assertIn("꺼짐", texts[header + 2])

    def test_banner_is_above_the_header_and_rows_keep_their_places(self):
        model = loaded(row("alpha"))
        model.set_disconnected()
        lines = self.lines(model)
        kinds = [line.kind for line in lines]
        self.assertLess(kinds.index("banner"), kinds.index("header"))
        self.assertIn("Hub 연결 없음", next(line.text for line in lines if line.kind == "banner"))
        self.assertIn("연결 없음/불명", next(line.text for line in lines if line.kind == "selected"))

    def test_modal_and_notice_are_shown_near_the_bottom(self):
        model = loaded(row("proj", "off"))
        model.press("enter")
        lines = self.lines(model)
        self.assertEqual(lines[-2].kind, "modal")
        self.assertIn('"proj"을(를) 켤까요? [y/N]', lines[-2].text)
        model.press("n")
        model.press("x")
        lines = self.lines(model)
        self.assertEqual(lines[-2].kind, "notice")
        self.assertIn("이미 꺼져", lines[-2].text)

    def test_empty_body_text_is_shown(self):
        lines = self.lines(loaded())
        self.assertIn(EMPTY_TEXT[:20], " ".join(line.text for line in lines if line.kind == "body"))

    def test_narrow_terminal_keeps_name_and_state_and_never_overflows(self):
        model = loaded(row("프로젝트-하나", "on", task={"summary": "아주 긴 작업 이름" * 5, "status": "running"}),
                       row("b", "mismatch", "project_id_mismatch"))
        for cols in (20, 30, 40, 60):
            lines = self.lines(model, cols=cols)
            selected = next(line.text for line in lines if line.kind == "selected")
            self.assertIn("프로젝트", selected, cols)
        wide = next(line.text for line in self.lines(model, cols=60) if line.kind == "selected")
        self.assertIn("켜짐", wide)

    def test_a_long_list_scrolls_to_keep_the_selection_visible(self):
        model = loaded(*[row(f"p{i:02d}") for i in range(30)])
        for _ in range(25):
            model.press("down")
        lines = self.lines(model, rows=10)
        self.assertIn("p25", next(line.text for line in lines if line.kind == "selected"))

    def test_control_characters_in_a_name_are_not_drawn(self):
        model = loaded(row("evil\x1b[31m\x07name"))
        lines = self.lines(model)
        for line in lines:
            self.assertTrue(all(ch.isprintable() for ch in line.text), repr(line.text))


if __name__ == "__main__":
    unittest.main()
