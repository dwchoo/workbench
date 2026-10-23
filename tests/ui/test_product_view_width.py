"""Lines drawn by ``product.view.draw`` are clipped by terminal cell width, not by character count."""
import curses
import sys
import unittest
from pathlib import Path
from unittest import mock

from wcwidth import wcswidth

sys.path.insert(0, str(Path(__file__).parent))

from test_product_model import host_exited, make  # noqa: E402
from workbench.ui.product import view  # noqa: E402

RESTARTING_MODE = "shell mode: 재시작 중 · 입력 받지 않음"
_ACS = {name: ord("+") for name in ("ACS_ULCORNER", "ACS_LLCORNER", "ACS_URCORNER", "ACS_LRCORNER",
                                    "ACS_HLINE", "ACS_VLINE")}


class FakeWindow:
    """Records what a curses window would show: ``addnstr`` keeps at most ``n`` characters, as curses does."""

    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.drawn: list[tuple[int, int, str]] = []

    def getmaxyx(self):
        return self.rows, self.cols

    def addnstr(self, y, x, text, n, attr=0):
        self.drawn.append((y, x, text[:n]))

    def addstr(self, *args):
        pass

    def addch(self, *args):
        pass

    def erase(self):
        pass

    def refresh(self):
        pass

    def move(self, *args):
        pass

    def line(self, y):
        """The text drawn at column 0 of row ``y`` (a status line or the footer)."""
        return next(text for row, x, text in self.drawn if row == y and x == 0)


def render(model, rows, cols):
    model.resize(rows, cols)
    win = FakeWindow(rows, cols)
    with mock.patch.multiple(curses, create=True, **_ACS), \
            mock.patch.object(view, "_attributes", return_value=0), \
            mock.patch.object(curses, "curs_set", side_effect=curses.error):
        view.draw(win, model, object())
    return win


def restarting_model():
    model, _ = host_exited()
    model.handle_input(b"\r")
    return model


class DrawnLineWidthTests(unittest.TestCase):
    def test_restarting_status_line_fits_an_80_column_terminal(self):
        model = restarting_model()
        self.assertIn(RESTARTING_MODE, model.status_lines()[0])
        win = render(model, 30, 80)
        line1 = win.line(0)
        self.assertLessEqual(wcswidth(line1), 79, f"{wcswidth(line1)} cells: {line1!r}")
        self.assertTrue(line1.startswith("focus:"), line1)

    def test_a_wide_character_straddling_the_limit_is_dropped_not_split(self):
        win = FakeWindow(5, 80)
        for text, limit, expected in (("가나다", 5, "가나"),   # 6 cells would need 2+2+2; the third is dropped
                                      ("abc가", 4, "abc"),     # the wide char would end at cell 5
                                      ("a가b", 2, "a"),
                                      ("가", 1, "")):
            with self.subTest(text=text, limit=limit):
                win.drawn.clear()
                view._put(win, 0, 0, text, limit)
                shown = win.drawn[0][2] if win.drawn else ""
                self.assertEqual(expected, shown)
                self.assertLessEqual(wcswidth(shown), limit)

    def test_zero_width_and_combining_characters_cost_no_cell(self):
        win = FakeWindow(5, 80)
        text = "ééé"  # three combining pairs: 3 cells
        view._put(win, 0, 0, text, 3)
        self.assertEqual(text, win.drawn[0][2])

    def test_ascii_lines_are_drawn_unchanged(self):
        win = FakeWindow(5, 80)
        view._put(win, 0, 0, "focus: MANAGER OMP | shell mode: user_control", 79)
        self.assertEqual("focus: MANAGER OMP | shell mode: user_control", win.drawn[0][2])
        win.drawn.clear()
        view._put(win, 1, 2, "x" * 100, 79)
        self.assertEqual("x" * 79, win.drawn[0][2])

    def test_no_drawn_line_exceeds_its_limit_at_narrow_widths(self):
        for cols in (80, 60, 40):
            with self.subTest(cols=cols):
                model = restarting_model()
                win = render(model, 30, cols)
                for y, x, text in win.drawn:
                    self.assertLessEqual(x + wcswidth(text), cols - 1 if x == 0 else cols,
                                         f"row {y} col {x}: {text!r}")

    def test_normal_status_line_fits_an_80_column_terminal(self):
        model, _ = make()
        win = render(model, 30, 80)
        for y in (0, 1):
            self.assertLessEqual(wcswidth(win.line(y)), 79, win.line(y))


if __name__ == "__main__":
    unittest.main()
