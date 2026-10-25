"""Korean (Hangul) project names in the TUI Hub list (``screen_lines``) and ``wb hub list`` (``render_table``).

Columns are laid out by terminal cells (a Hangul syllable is 2 cells; a decomposed syllable is a 2-cell leading
jamo plus 0-cell vowel/final jamo). Expected values: wcwidth cell counts of the test's own names, the state labels
"켜짐"/"꺼짐" (SPEC label table) and the rule that a cut never leaves part of a character.
Run with ``discover -s tests/ui -p 'test_hub_list*'``.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unicodedata
import unittest

from wcwidth import wcswidth

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workbench.hub.presentation import render_table  # noqa: E402
from workbench.ui.hub_list.model import HubListModel  # noqa: E402
from workbench.ui.hub_list.view import screen_lines  # noqa: E402

NAMES = ["가", "한글 프로젝트", unicodedata.normalize("NFD", "분해된 이름"), "mixed 한글 abc", "아주 긴 한국어 프로젝트 이름입니다"]


def row(name: str, power: str) -> dict:
    return {"root": f"/work/{name}", "name": name, "project_id": f"id-{name}", "power": power, "power_detail": None,
            "task": None, "command": None, "waits": [], "pending_requests": None, "last_activity": None,
            "backend": None, "observed_at": 0.0}


def rows() -> list[dict]:
    return [row(name, "on" if index % 2 else "off") for index, name in enumerate(NAMES)]


def state_column(text: str) -> int:
    """The cell where the state label starts in one drawn line."""
    index = min(i for i in (text.find("켜짐"), text.find("꺼짐")) if i >= 0)
    return wcswidth(text[:index])


class HubListHangulTests(unittest.TestCase):
    def lines(self, cols: int):
        model = HubListModel()
        model.set_rows(rows())
        drawn = screen_lines(model, 14, cols, 0.0)
        return [line.text for line in drawn if line.kind in ("row", "selected")]

    def test_the_state_column_lines_up_under_hangul_names_at_every_width(self):
        for cols in (24, 30, 40, 60, 100, 160):
            with self.subTest(cols=cols):
                texts = self.lines(cols)
                self.assertEqual(len(texts), len(NAMES))
                for text in texts:
                    self.assertLessEqual(wcswidth(text), cols - 1, text)
                self.assertEqual(len({state_column(text) for text in texts}), 1, texts)

    def test_a_cut_name_ends_on_a_whole_character(self):
        for cols in (24, 30):
            for text in self.lines(cols):
                cell = text[2:].split("  ")[0]
                self.assertEqual(unicodedata.normalize("NFC", cell), unicodedata.normalize("NFC", cell.rstrip()))
                if cell.endswith("…"):
                    # no lone leading jamo of a decomposed syllable right before the ellipsis
                    self.assertFalse(0x1100 <= ord(cell[-2]) <= 0x115F, ascii(cell))

    def test_wb_hub_list_aligns_columns_by_display_width(self):
        lines = render_table(rows(), 0.0).splitlines()
        self.assertEqual(len(lines), len(NAMES) + 1)
        self.assertEqual(len({state_column(line) for line in lines[1:]}), 1, lines)
        header_state = wcswidth(lines[0][:lines[0].index("상태")])
        self.assertEqual(header_state, state_column(lines[1]))


if __name__ == "__main__":
    unittest.main()
