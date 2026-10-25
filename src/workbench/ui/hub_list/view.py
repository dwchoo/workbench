"""Hub list drawing: ``screen_lines`` is the pure layout, ``draw`` paints it with curses.

The columns are the model's table (the cells ``wb hub list`` prints). A narrow terminal first
shrinks the wide columns, then drops the right-hand ones (never 이름 or 상태) so the selected
project and its state stay readable. Untrusted text (names, paths, task summaries) goes through
the product UI's cell clipper, which blanks control and bidi characters.
"""
from __future__ import annotations

import curses
from dataclasses import dataclass

from wcwidth import wcswidth

from workbench.ui.hub_list.model import HubListModel
from workbench.ui.product.view import _clip_cells

TITLE = "Workbench Hub"
FOOTER = "↑↓/jk 이동  Enter 진입  o 켜기  x 끄기  r 새로 고침  q 종료"
NAME, STATE = 0, 2  # columns that stay visible
DROP_ORDER = (7, 6, 5, 4, 3, 1)  # 마지막 활동, 요청, 대기 사유, 명령, Task·단계, 경로
MIN_WIDTH = 6  # a shrunk column keeps at least this much (이름 and 상태: ``FLOOR``)
FLOOR = {NAME: 10, STATE: 6}
SEPARATOR = 2
MARK = "> "


@dataclass(frozen=True)
class Line:
    text: str
    kind: str  # title banner header row selected body notice modal footer blank


def _width(text: str) -> int:
    return max(wcswidth(text), 0)


def _fit(text: str, width: int) -> str:
    """``text`` cut to ``width`` cells, ending in ``…`` when something was cut."""
    text = _clip_cells(text, 10 ** 6)
    if _width(text) <= width:
        return text
    return _clip_cells(text, max(0, width - 1)) + "…"


def _pad(text: str, width: int) -> str:
    return text + " " * (width - _width(text))


def _column_widths(table: list[list[str]], columns: list[int], room: int) -> list[int] | None:
    """Natural widths shrunk (widest first, 이름/상태 last) to fit ``room``; None when they cannot."""
    widths = {c: max(_width(_clip_cells(line[c], 10 ** 6)) for line in table) for c in columns}
    room -= SEPARATOR * (len(columns) - 1)
    floor = {c: min(widths[c], FLOOR.get(c, MIN_WIDTH)) for c in columns}
    while sum(widths.values()) > room:
        shrinkable = [c for c in columns if widths[c] > floor[c]]
        if not shrinkable:
            return None
        pool = [c for c in shrinkable if c not in (NAME, STATE)] or shrinkable
        widest = max(pool, key=lambda c: widths[c])
        widths[widest] -= 1
    return [widths[c] for c in columns]


def _format(table: list[list[str]], limit: int) -> tuple[list[str], list[list[str]]]:
    """Header text and row texts laid out within ``limit`` cells."""
    columns = list(range(len(table[0])))
    room = limit - len(MARK)
    for drop in (None, *DROP_ORDER):
        if drop is not None:
            columns.remove(drop)
        widths = _column_widths(table, columns, room)
        if widths is not None:
            break
    else:
        widths = [max(MIN_WIDTH, room - FLOOR[STATE] - SEPARATOR), FLOOR[STATE]]
    texts = ["  ".join(_pad(_fit(line[c], w), w) for c, w in zip(columns, widths)).rstrip() for line in table]
    return texts[0], texts[1:]


def screen_lines(model: HubListModel, rows: int, cols: int, now: float) -> list[Line]:
    """The screen top to bottom: title, banner, header, project rows (scrolled to keep the selection
    in view), then the question/notice line and the key line as the last two."""
    limit = max(1, cols - 1)
    top = [Line(_fit(TITLE, limit), "title")]
    if model.banner:
        top.append(Line(_fit(model.banner, limit), "banner"))
    table = model.table(now)
    header, bodies = _format(table, limit)
    top.append(Line(MARK.replace(">", " ") + header, "header"))
    room = rows - len(top) - 2
    if model.body_text:
        top.append(Line(_fit(model.body_text, limit), "body"))
    else:
        start = max(0, min(model.selected - room // 2, len(bodies) - room)) if room > 0 else 0
        for index in range(start, min(len(bodies), start + max(room, 0))):
            selected = index == model.selected
            top.append(Line(_fit((MARK if selected else "  ") + bodies[index], limit),
                            "selected" if selected else "row"))
    if model.modal is not None:
        status = Line(_fit(model.modal.text, limit), "modal")
    else:
        status = Line(_fit(model.notice, limit), "notice")
    top = top[:max(0, rows - 2)]
    return top + [Line("", "blank")] * max(0, rows - 2 - len(top)) + [status, Line(_fit(FOOTER, limit), "footer")][
        :rows]


_ATTRIBUTES = {"title": curses.A_BOLD, "banner": curses.A_BOLD | curses.A_BLINK, "header": curses.A_UNDERLINE,
               "selected": curses.A_REVERSE, "modal": curses.A_BOLD | curses.A_REVERSE, "notice": curses.A_BOLD,
               "footer": curses.A_DIM}


def draw(win: curses.window, model: HubListModel, now: float) -> None:
    rows, cols = win.getmaxyx()
    win.erase()
    for y, line in enumerate(screen_lines(model, rows, cols, now)):
        if not line.text:
            continue
        try:
            win.addnstr(y, 0, line.text, len(line.text), _ATTRIBUTES.get(line.kind, 0))
        except curses.error:
            pass
    win.refresh()
