"""Hub list state machine (SPEC "TUI Hub 목록"): rows, selection, modal and notice.

Pure: no curses, no I/O. ``press(key)`` takes a key name (``up``/``down``/``enter``/``esc`` or a
single character) and returns an action for the app to run, or None. Rows are hub_v1 ``list``
rows. An entry in an action is built from the row at the moment the action is chosen, so a later
refresh never retargets it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from workbench.hub import registry
from workbench.hub.presentation import COLUMNS, power_text, row_cells


HUB_LOST_TEXT = "Hub 연결 없음"
LOADING_TEXT = "불러오는 중…"
HUB_UNKNOWN_TEXT = "Hub 상태 불명"
HUB_LOADING_TEXT = "Hub 상태 불러오는 중…"
UNKNOWN = "불명"
WEB_ON_HINT = "hub.toml의 [web] enabled = false를 true로 바꾸고 'wb hub reload'를 실행하세요"
INSECURE_MARK = "[!] 평문 HTTP"
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
class PowerOffAll:
    """``X`` (2026-10-27 사용자 요청: 전체 끄기): the on entries at the moment of the key and the rows
    that are neither on nor off (reported, never counted as stopped)."""
    entries: tuple
    held: tuple


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


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _uptime_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}일 {hours}시간"
    if hours:
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분"
    return f"{rest}초"


def web_text(web: object) -> str:
    """``web: ...`` from the status ``web`` object (the states ``wb hub status`` prints); anything
    unreadable is ``불명``, never ``disabled``."""
    state = _text(_dict(web).get("state"))
    if state is None:
        return f"web: {UNKNOWN}"
    web = _dict(web)
    detail = _text(web.get("detail"))
    if state == "listening":
        urls = [url for url in web.get("urls") or () if _text(url)] or [_text(web.get("url")) or UNKNOWN]
        return f"web: listening {' '.join(urls)}" + (f" {INSECURE_MARK}" if web.get("insecure_warning") else "")
    if state == "disabled":
        return f"web: disabled — {WEB_ON_HINT}"
    if state == "config_invalid":
        return f"web: config_invalid:{detail or UNKNOWN}"
    return f"web: not_listening: {state}" + (f":{detail}" if detail else "")


def config_text(config: object) -> str:
    """`` · config HH:MM`` (when the running ``hub.toml`` was loaded) and `` · reload 실패: <reason>`` after a refused
    ``wb hub reload`` (2026-10-27); empty for a Hub that does not send ``config``."""
    config = _dict(config)
    loaded = config.get("loaded_at")
    if not isinstance(loaded, (int, float)) or isinstance(loaded, bool):
        return ""
    text = f" · config {datetime.fromtimestamp(loaded).strftime('%H:%M')}"
    error = _dict(config.get("reload_error"))
    if _text(error.get("reason")):
        text += f" · reload 실패: {error['reason']}"
    return text


def board_text(status: dict) -> str:
    """``Board agent: <agent state>`` (+ ``shell <state>`` when it differs); ``없음`` is only the
    status's own ``board_agent: null`` (no set ever started), a missing key is ``불명``."""
    if "board_agent" not in status:
        return f"Board agent: {UNKNOWN}"
    board = status["board_agent"]
    if board is None:
        return "Board agent: 없음"
    agent = _text(_dict(_dict(board).get("agent")).get("state")) or UNKNOWN
    shell = _text(_dict(_dict(board).get("shell")).get("state")) or UNKNOWN
    return f"Board agent: {agent}" + ("" if shell == agent else f" (shell {shell})")


class HubListModel:
    def __init__(self) -> None:
        self._hub: dict | None = None  # the last good hub_v1 ``status`` reply; None = not known right now
        self._hub_asked = False
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

    def set_hub_status(self, reply: object) -> None:
        """A ``status`` reply; an unusable one (not ok, not a dict) is unknown, never a state."""
        if isinstance(reply, dict) and reply.get("ok"):
            self._hub = reply
        else:
            self._hub = None
        self._hub_asked = True

    def set_hub_unknown(self, reason: str = "") -> None:
        self._hub = None
        self._hub_asked = True

    def hub_lines(self, now: float) -> list[str]:
        """The Hub's own status, 1–2 lines: pid, version, uptime, registered count and Board agent;
        then the web state. Not fetched yet: loading; not fetchable: ``Hub 상태 불명``."""
        if not self._hub_asked:
            return [HUB_LOADING_TEXT]
        status = self._hub
        if status is None:
            return [HUB_UNKNOWN_TEXT]
        hub, registry_ = _dict(status.get("hub")), _dict(status.get("registry"))
        pid = hub.get("pid")
        parts = [f"Hub pid {pid}" if type(pid) is int else f"Hub pid {UNKNOWN}"]
        release = _text(_dict(hub.get("version")).get("release"))
        parts.append(f"wb {release}" if release else f"version {UNKNOWN}")
        count = registry_.get("count")
        if "registry" in status and registry_.get("state") not in (None, "ok"):
            parts.append("registry 손상")
        elif type(count) is int:
            parts.append(f"프로젝트 {count}개")
        else:
            parts.append(f"프로젝트 {UNKNOWN}")
        parts.append(board_text(status))
        started = hub.get("started_at")  # last: a narrow terminal cuts the least important part first
        if isinstance(started, (int, float)) and not isinstance(started, bool):
            parts.append(f"가동 {_uptime_text(now - started)}")
        if not all(key in status for key in ("hub", "registry", "web", "board_agent")):
            parts.insert(1, HUB_UNKNOWN_TEXT)  # a partial reply: the rest of what it lacks is unknown
        return [" · ".join(parts), web_text(status.get("web")) + config_text(status.get("config"))]

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

    def _power_off_all(self):
        """``X``: every on row at once (``projects.classify_rows``); none on is a notice, not an action."""
        from workbench.hub import projects
        targets, held = projects.classify_rows(self.rows)
        if targets:
            return PowerOffAll(tuple(targets), tuple(held))
        unconfirmed = [item for item in held if item.failed]
        if unconfirmed:
            self.notice = f"끌 수 있는 켜진 프로젝트가 없고 {len(unconfirmed)}개는 상태 불명입니다(꺼졌는지 확인하지 못했습니다)"
        else:
            self.notice = "켜진 WB 프로젝트가 없습니다."
        return None

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
        elif key == "X":
            return self._power_off_all()
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
