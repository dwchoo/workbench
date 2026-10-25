"""Hub list state machine (SPEC "TUI Hub 목록"): rows, selection, modal and notice.

Pure: no curses, no I/O. ``press(key)`` takes a key name (``up``/``down``/``enter``/``esc`` or a
single character) and returns an action for the app to run, or None. Rows are hub_v1 ``list``
rows. An entry in an action is built from the row at the moment the action is chosen, so a later
refresh never retargets it.
"""
from __future__ import annotations

from dataclasses import dataclass

from workbench.hub import registry
from workbench.hub.presentation import COLUMNS, power_text, row_cells


HUB_LOST_TEXT = "Hub 연결 없음"
LOADING_TEXT = "불러오는 중…"
EMPTY_TEXT = "등록된 WB 프로젝트가 없습니다. 'wb init <폴더>'로 만들거나 'wb hub add <폴더>'로 등록하세요."


@dataclass(frozen=True)
class Attach:
    entry: registry.Entry


@dataclass(frozen=True)
class PowerOn:
    entry: registry.Entry
    attach: bool


@dataclass(frozen=True)
class PowerOff:
    entry: registry.Entry


@dataclass(frozen=True)
class Refresh:
    pass


@dataclass(frozen=True)
class Quit:
    pass


@dataclass(frozen=True)
class Modal:
    """A pending "켤까요?" question, bound to the entry chosen when it opened."""
    entry: registry.Entry
    attach: bool

    @property
    def text(self) -> str:
        return f'"{self.entry.name}"을(를) 켤까요? [y/N]'


def entry_of(row: dict) -> registry.Entry:
    return registry.Entry(root=row["root"], project_id=row["project_id"], name=row["name"],
                          registered_at=0.0, source="tui")


class HubListModel:
    def __init__(self) -> None:
        self._rows: list[dict] = []  # as the last good poll gave them
        self.selected = 0
        self.loaded = False
        self.connected = True
        self.notice = ""
        self.modal: Modal | None = None

    @property
    def rows(self) -> list[dict]:
        """What is shown: while the Hub is unreachable every row reads ``unknown`` (never a stale ``on``)."""
        if self.connected:
            return self._rows
        return [{**row, "power": "unknown", "power_detail": None} for row in self._rows]

    @property
    def banner(self) -> str:
        return "" if self.connected else HUB_LOST_TEXT

    @property
    def body_text(self) -> str:
        if self.rows:
            return ""
        return EMPTY_TEXT if self.loaded else LOADING_TEXT

    def table(self, now: float) -> list[list[str]]:
        """Header cells and one list of cells per row: the very cells ``wb hub list`` prints."""
        return [list(COLUMNS), *(row_cells(row, now) for row in self.rows)]

    def set_disconnected(self) -> None:
        self.connected = False
        self.modal = None

    @property
    def selected_row(self) -> dict | None:
        return self.rows[self.selected] if 0 <= self.selected < len(self.rows) else None

    def set_rows(self, rows: list[dict]) -> None:
        keep = self.selected_row["root"] if self.selected_row else None
        self._rows = list(rows)
        self.loaded = True
        self.connected = True
        roots = [candidate["root"] for candidate in self.rows]
        if keep in roots:
            self.selected = roots.index(keep)
        else:
            self.selected = max(0, min(self.selected, len(self.rows) - 1))
        if self.modal is not None and self.modal.entry.root not in roots:
            self.notice = f'"{self.modal.entry.name}"이(가) 목록에서 사라져 켜기 확인을 닫았습니다'
            self.modal = None

    def _refuse(self, row: dict, what: str) -> None:
        self.notice = f'"{row["name"]}": {power_text(row)} 상태라 {what}'

    def press(self, key: str):
        self.notice = ""
        if self.modal is not None:  # any key closes it; only y starts, and only the entry it was opened for
            modal, self.modal = self.modal, None
            return PowerOn(modal.entry, modal.attach) if key == "y" else None
        row = self.selected_row
        power = row["power"] if row else None
        if key in ("up", "k"):
            self.selected = max(0, self.selected - 1)
        elif key in ("down", "j"):
            self.selected = max(0, min(len(self.rows) - 1, self.selected + 1))
        elif key == "r":
            return Refresh()
        elif key == "q":
            return Quit()
        elif key == "enter" and power == "on":
            return Attach(entry_of(row))
        elif key in ("enter", "o") and power == "off":
            self.modal = Modal(entry_of(row), attach=key == "enter")
        elif key == "x" and power == "on":
            return PowerOff(entry_of(row))
        elif row is not None and key in ("enter", "o", "x"):
            if power == "off":  # x
                self.notice = f'"{row["name"]}"은(는) 이미 꺼져 있습니다'
            elif power == "on":  # o
                self.notice = f'"{row["name"]}"은(는) 이미 켜져 있습니다'
            elif key == "x":
                self._refuse(row, "끌 수 없습니다")
            else:
                self._refuse(row, "켜거나 들어갈 수 없습니다")
        return None
