"""Hub list labels and columns: the one place the TUI (HB-08), ``wb hub list`` and (A) web use.

Input is a hub_v1 ``list`` row (SPEC "상태 집계"); output is display text only. An
unknown code is shown as the code itself, and a missing value as :data:`EMPTY`.
"""
from __future__ import annotations

from wcwidth import wcswidth

EMPTY = "—"
COLUMNS = ("이름", "경로", "상태", "Task·단계", "명령", "대기 사유", "요청", "마지막 활동")

POWER_LABELS = {"on": "켜짐", "off": "꺼짐", "starting": "시작 중", "closing": "끄는 중",
                "unknown": "연결 없음/불명", "mismatch": "불일치"}
WAIT_LABELS = {"paused": "일시정지",
               "boot_confirmation_required": "재부팅 확인 전 보류",
               "manager_editor_not_empty": "manager 입력창에 글이 있어 알림 대기",
               "manager_session_not_connected": "manager 세션 연결 없음(알림 대기)",
               "manager_busy": "manager 작업 중이라 요청 전달 대기",
               "metadata_unavailable": "metadata 저장 장애로 보류",
               "model_hold": "모델 오류로 보류",
               "task_held": "Task 보류"}
COMMAND_STATE_LABELS = {"running": "실행 중", "stopping": "stop 중", "unknown": "불명"}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def power_text(row: dict) -> str:
    """State label; ``mismatch`` names its detail (``불일치(root_missing)``)."""
    power = row.get("power")
    label = POWER_LABELS.get(power, _text(power) or EMPTY)
    detail = _text(row.get("power_detail"))
    return f"{label}({detail})" if power == "mismatch" and detail else label


def task_text(row: dict) -> str:
    """``<summary or task_id> · <status>`` (the status is the stage)."""
    task = row.get("task")
    if not isinstance(task, dict):
        return EMPTY
    name = _text(task.get("summary")) or _text(task.get("task_id")) or EMPTY
    status = _text(task.get("status"))
    return f"{name} · {status}" if status else name


def command_text(row: dict) -> str:
    """``<label> (<state label>)`` of the host-shell Workbench command."""
    command = row.get("command")
    if not isinstance(command, dict):
        return EMPTY
    name = _text(command.get("label")) or _text(command.get("command_id")) or EMPTY
    state = _text(command.get("state"))
    return f"{name} ({COMMAND_STATE_LABELS.get(state, state)})" if state else name


def waits_text(row: dict) -> str:
    labels = [WAIT_LABELS.get(wait.get("code"), _text(wait.get("code")) or EMPTY)
              for wait in row.get("waits") or () if isinstance(wait, dict)]
    return ", ".join(labels) if labels else EMPTY


def requests_text(row: dict) -> str:
    """``pending_requests`` is ``null`` until (A); it shows ``—``, never 0 (HB-AC-19)."""
    count = row.get("pending_requests")
    return EMPTY if not isinstance(count, int) or isinstance(count, bool) else str(count)


def relative_time(moment: object, now: float) -> str:
    """``N초 전``/``N분 전``/``N시간 전``/``N일 전``; a future time is ``방금``."""
    if isinstance(moment, bool) or not isinstance(moment, (int, float)):
        return EMPTY
    seconds = int(now - moment)
    if seconds < 0:
        return "방금"
    for unit, size in (("일", 86400), ("시간", 3600), ("분", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} 전"
    return f"{seconds}초 전"


def row_cells(row: dict, now: float) -> list[str]:
    """One row's cells in :data:`COLUMNS` order."""
    return [_text(row.get("name")) or EMPTY, _text(row.get("root")) or EMPTY, power_text(row), task_text(row),
            command_text(row), waits_text(row), requests_text(row), relative_time(row.get("last_activity"), now)]


def _width(text: str) -> int:
    width = wcswidth(text)
    return len(text) if width < 0 else width


def _pad(text: str, width: int) -> str:
    return text + " " * (width - _width(text))


def render_table(rows: list[dict], now: float) -> str:
    """:data:`COLUMNS` header and one line per row, columns aligned by display width."""
    lines = [list(COLUMNS), *(row_cells(row, now) for row in rows)]
    widths = [max(_width(line[index]) for line in lines) for index in range(len(COLUMNS))]
    return "\n".join("  ".join(_pad(cell, width) for cell, width in zip(line, widths)).rstrip()
                     for line in lines)
