"""Project control library (SPEC "프로젝트 제어 라이브러리"): one implementation of power on/off,
pause, resume and boot confirmation for the TUI list (HB-08) and the (A) web.

Every function takes a registry entry and first runs ``status.check_entry``: a mismatched entry
is refused (``mismatch``, detail = the check code) before anything is read from or sent to a
backend, and no process is started (HB-AC-13). Every function returns an :class:`Outcome`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import registry
from .status import check_entry

MISMATCH = "mismatch"
START_REQUIREMENT = "start_requirement"  # cmd_start exit 2: a start requirement is missing, nothing started
START_FAILED = "start_failed"  # cmd_start exit 1 or a data dir error
ATTACH_FAILED = "attach_failed"  # started (or already running), but the attached session ended non-zero


@dataclass(frozen=True)
class Outcome:
    """``ok`` with ``reason``/``detail`` when not ok; ``data`` holds call-specific values."""
    ok: bool
    reason: str | None = None
    detail: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "detail": self.detail, "data": self.data}


def _mismatch(entry: registry.Entry) -> Outcome | None:
    detail = check_entry(entry)
    return None if detail is None else Outcome(False, MISMATCH, detail)


def _data_dir(entry: registry.Entry) -> Path:
    return Path(entry.root) / ".wb"


def _backend_reference(entry: registry.Entry) -> dict | None:
    """The verified ``running`` backend's ``{pid, start_ticks}`` (``inspect_backend``), else None."""
    from workbench.backend.paths import DataDirError, DataLayout
    from workbench.backend.process_identity import BackendIdentityError, inspect_backend
    try:
        identity = inspect_backend(DataLayout(_data_dir(entry)))
    except (BackendIdentityError, DataDirError, OSError, ValueError):
        return None
    if identity.state != "running":
        return None
    reference = identity.record["processes"]["backend"]
    return {"pid": reference["pid"], "start_ticks": reference["start_ticks"]}


def power_on(entry: registry.Entry, attach: bool) -> Outcome:
    """``cmd_start`` with ``--no-attach --data-dir <root>/.wb`` (update fence, start requirements,
    ``project_initialization_lock``, ``spawn_backend``, ``_wait_started``); an already running
    backend is joined, never duplicated. The backend is an orphan in its own session, not a child
    of the caller. ``attach`` then attaches this process's terminal like ``wb start`` does.

    ``cmd_start`` prints its usual lines to this process's stdout/stderr. ``data.backend`` is
    the backend's ``{pid, start_ticks}``.
    """
    refused = _mismatch(entry)
    if refused is not None:
        return refused
    from workbench.backend import cli
    from workbench.backend.paths import DataDirError
    args = cli.parser().parse_args(["start", "--no-attach", "--data-dir", str(_data_dir(entry))])
    try:
        status = cli.cmd_start(args)
    except DataDirError as exc:
        return Outcome(False, START_FAILED, f"data dir error: {exc}")
    if status != 0:
        return Outcome(False, START_REQUIREMENT if status == cli.EXIT_REQUIREMENT else START_FAILED,
                       f"wb start exit {status}")
    data = {"backend": _backend_reference(entry)}
    if attach:
        try:
            status = cli._attach(cli._layout(args), args)
        except (cli.NotRunning, DataDirError) as exc:
            return Outcome(False, ATTACH_FAILED, str(exc), data)
        if status != 0:
            return Outcome(False, ATTACH_FAILED, f"attach exit {status}", data)
    return Outcome(True, data=data)


def power_off(entry: registry.Entry, ask, out) -> Outcome:
    refused = _mismatch(entry)
    if refused is not None:
        return refused
    raise NotImplementedError
