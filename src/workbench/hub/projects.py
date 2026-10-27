"""Project control library (SPEC "프로젝트 제어 라이브러리"): one implementation of power on/off,
pause, resume and boot confirmation for the TUI list (HB-08) and the (A) web.

Every function takes a registry entry and first runs ``status.check_entry``: a mismatched entry
is refused (``mismatch``, detail = the check code) before anything is read from or sent to a
backend, and no process is started (HB-AC-13). Every function returns an :class:`Outcome`.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import io
from pathlib import Path
from typing import Any, Callable, TextIO

from . import registry
from .status import check_entry

MISMATCH = "mismatch"
START_REQUIREMENT = "start_requirement"  # cmd_start exit 2: a start requirement is missing, nothing started
START_FAILED = "start_failed"  # cmd_start exit 1 or a data dir error
ATTACH_FAILED = "attach_failed"  # started (or already running), but the attached session ended non-zero
NOT_RUNNING = "not_running"  # no verified running backend (nothing was sent)
UNREACHABLE = "unreachable"  # the verified backend could not be asked (connection, identity, timeout)
DECLINED = "declined"  # ``ask`` said no: nothing was stopped
WORK_KEPT_CHANGING = "work_kept_changing"  # the work changed on every confirm: nothing was stopped
NO_RESULT = "no_result"  # confirmed, but no shutdown result came (never a success, C-AC-22)
UNVERIFIED = "unverified"  # the shutdown result is not ``verified`` (never a success, C-AC-22)
BACKEND_TOO_OLD = "backend_too_old"  # pause/resume answered ``not_attached``: a backend before HB-06; restart it
BOOT_ID_UNAVAILABLE = "boot_id_unavailable"  # the snapshot has no current ``boot.boot_id`` to confirm
# SPEC "상태 전이": after a changed work list power_off asks again with the new list at most this often.
MAX_WORK_CHANGE_RETRIES = 5
WORK_CHANGED_TEXT = "작업이 바뀌었습니다"  # + " (<backend detail>)"
RE_ASK_TEXT = "새 목록을 다시 확인하세요."  # only when another ask follows
WORK_KEPT_CHANGING_TEXT = "작업이 계속 바뀌어 끄지 않았습니다"
REQUEST_TIMEOUT = 10.0


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


def backend_reference(entry: registry.Entry) -> dict | None:
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


class _Tee(io.TextIOBase):
    """``out`` plus a copy of everything written (``Outcome.data["output"]``)."""

    def __init__(self, out: TextIO):
        self.out, self.copy = out, io.StringIO()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self.copy.write(text)
        self.out.write(text)
        return len(text)

    def flush(self) -> None:
        self.out.flush()


def power_on(entry: registry.Entry, attach: bool, out: TextIO | None = None) -> Outcome:
    """``cmd_start`` with ``--no-attach --data-dir <root>/.wb`` (update fence, start requirements,
    ``project_initialization_lock``, ``spawn_backend``, ``_wait_started``); an already running
    backend is joined, never duplicated. The backend is an orphan in its own session, not a child
    of the caller. ``attach`` then attaches this process's terminal like ``wb start`` does.

    ``cmd_start`` prints its usual lines to this process's stdout/stderr; with ``out`` they go to
    ``out`` instead (``contextlib.redirect_stdout``/``redirect_stderr`` around the start only, not
    the attach) and ``data.output`` holds that text. The backend never inherits ``out``: it is
    detached onto ``backend.log``. The redirect swaps the process-wide ``sys.stdout``/``sys.stderr``,
    so a threaded caller (the (A) web, TI-08) runs this in a driver subprocess or passes streams
    another way. ``data.backend`` is the backend's ``{pid, start_ticks}``. ``KeyboardInterrupt``
    is the caller's to handle.
    """
    refused = _mismatch(entry)
    if refused is not None:
        return refused
    from workbench.backend import cli
    from workbench.backend.paths import DataDirError
    args = cli.parser().parse_args(["start", "--no-attach", "--data-dir", str(_data_dir(entry))])
    tee = None if out is None else _Tee(out)
    try:
        with contextlib.ExitStack() as stack:
            if tee is not None:
                stack.enter_context(contextlib.redirect_stdout(tee))
                stack.enter_context(contextlib.redirect_stderr(tee))
            status = cli.cmd_start(args)
    except DataDirError as exc:
        return Outcome(False, START_FAILED, f"data dir error: {exc}")
    except (OSError, ValueError) as exc:  # spawn (fork, pipes, the log) or the start-up records
        return Outcome(False, START_FAILED, f"{type(exc).__name__}: {exc}")
    if status != 0:
        return Outcome(False, START_REQUIREMENT if status == cli.EXIT_REQUIREMENT else START_FAILED,
                       f"wb start exit {status}")
    data = {"backend": backend_reference(entry)}
    if tee is not None:
        data["output"] = tee.copy.getvalue()
    if attach:
        try:
            status = cli._attach(cli._layout(args), args)
        except (cli.NotRunning, DataDirError) as exc:
            return Outcome(False, ATTACH_FAILED, str(exc), data)
        if status != 0:
            return Outcome(False, ATTACH_FAILED, f"attach exit {status}", data)
    return Outcome(True, data=data)


def _client(entry: registry.Entry, timeout: float = REQUEST_TIMEOUT):
    """A non-attached ``workbench-hub`` connection to the verified running backend (peer pid and
    start ticks checked against ``backend.json``); ``NotRunning`` when none runs."""
    from workbench.backend.client import NotRunning, UiClient
    from workbench.backend.paths import DataLayout
    from workbench.backend.process_identity import inspect_backend
    from .status import CLIENT_NAME
    identity = inspect_backend(DataLayout(_data_dir(entry)))
    if identity.state != "running":
        raise NotRunning(f"no verified backend is running for {identity.layout.root}")
    return UiClient(identity.layout.ui_socket, name=CLIENT_NAME, timeout=timeout,
                    expected_process=identity.record["processes"]["backend"])


def _connect(entry: registry.Entry):
    """``(client, None)`` or ``(None, Outcome)`` for a backend that cannot be asked."""
    from workbench.backend.client import ClientError, NotRunning
    from workbench.backend.paths import DataDirError
    from workbench.backend.process_identity import BackendIdentityError
    try:
        return _client(entry), None
    except NotRunning as exc:
        return None, Outcome(False, NOT_RUNNING, str(exc))
    except BackendIdentityError as exc:
        return None, Outcome(False, UNREACHABLE, f"identity:{exc.reason}")
    except (ClientError, DataDirError, OSError, ValueError) as exc:
        return None, Outcome(False, UNREACHABLE, f"{type(exc).__name__}: {exc}")


SHUTDOWN_TOKEN_MISMATCH = "shutdown_token_mismatch"  # power_off_confirm: the work changed or the token is stale


def _request_shutdown(client) -> Outcome:
    """``shutdown_request`` on ``client``: ``data`` = ``{token, active, expires_in, backend}``."""
    from workbench.backend.client import ClientError
    from workbench.contracts.ui_v1 import ClientType
    try:
        pending = client.request(ClientType.SHUTDOWN_REQUEST)
    except (OSError, TimeoutError, ClientError) as exc:
        return Outcome(False, UNREACHABLE, f"shutdown request failed (nothing was stopped): "
                                           f"{type(exc).__name__}: {exc}")
    if not pending.get("ok", True) or "token" not in pending:
        return Outcome(False, pending.get("reason") or UNREACHABLE, pending.get("detail"))
    return Outcome(True, data={"token": pending["token"], "active": pending.get("active", []),
                               "expires_in": pending.get("expires_in"),
                               "backend": (pending.get("processes") or {}).get("backend")})


def _confirm_shutdown(client, token: str, out: TextIO | None, backend: object) -> Outcome:
    """``shutdown_confirm`` on ``client`` and the ``closing`` result (``cli.confirm_shutdown``)."""
    from workbench.backend import cli
    from workbench.contracts.ui_v1 import Reason
    wait = cli.confirm_shutdown(client, token, progress=out)
    if wait.refused is not None:
        reason = wait.refused.get("reason")
        if reason == Reason.SHUTDOWN_TOKEN_MISMATCH.value:
            return Outcome(False, SHUTDOWN_TOKEN_MISMATCH, wait.refused.get("detail"))
        return Outcome(False, reason or UNREACHABLE, wait.refused.get("detail"))
    if wait.result is None:
        client.close()  # nothing more comes on it; watch the exact pid with the connection gone
        state = cli._backend_state(backend, cli.BACKEND_GONE_WAIT)
        return Outcome(False, NO_RESULT, wait.lost or "the backend sent no result", {"backend": state})
    if wait.result.get("verified") is not True:
        return Outcome(False, UNVERIFIED, ", ".join(wait.result.get("problems") or ["result unknown"]),
                       {"result": wait.result})
    return Outcome(True, data={"result": wait.result})


def power_off_request(entry: registry.Entry) -> Outcome:
    """Step 1 of a power off on its own connection: the backend's ``shutdown_request`` (nothing is
    stopped). ``data`` = ``{token, active, expires_in, backend}`` (``backend`` the ``{pid,
    start_ticks}`` the request names). The backend keeps the token for ``expires_in`` seconds
    whatever connection confirms it (the (A) web's two-page flow, TI-08)."""
    refused = _mismatch(entry)
    if refused is not None:
        return refused
    client, failed = _connect(entry)
    if failed is not None:
        return failed
    with client:
        return _request_shutdown(client)


def power_off_confirm(entry: registry.Entry, token: str, out: TextIO | None = None,
                      backend: object = None) -> Outcome:
    """Step 2 on its own connection: ``shutdown_confirm`` with a :func:`power_off_request` token
    and the ``closing`` result. ``shutdown_token_mismatch`` (detail = the backend's: changed work vs
    an expired or replaced token) stops nothing. Only ``verified is True`` is ``ok``
    (``data.result``); ``no_result`` (``data.backend`` = ``ended``/``alive``/``unknown`` of
    ``backend``, the request's ``data.backend``) and ``unverified`` are not."""
    refused = _mismatch(entry)
    if refused is not None:
        return refused
    client, failed = _connect(entry)
    if failed is not None:
        return failed
    with client:
        return _confirm_shutdown(client, token, out, backend)


def power_off(entry: registry.Entry, ask: Callable[[list], bool], out: TextIO) -> Outcome:
    """The backend's own full shutdown (``shutdown_request`` → ``ask(active)`` → ``shutdown_confirm``
    → the ``closing`` result, ``cli.confirm_shutdown``, shared with ``wb shutdown``), on one
    connection, through the same steps as :func:`power_off_request`/:func:`power_off_confirm`.

    ``ask`` gets the ``active`` list of that request; False stops nothing (``declined``). When the
    confirm is refused with ``shutdown_token_mismatch`` (the work changed, or the token expired or
    was replaced: the backend's detail tells which) ``out`` gets that detail and a new request asks
    again with the new list, at most :data:`MAX_WORK_CHANGE_RETRIES` times; after the last refusal
    nothing more is asked (``work_kept_changing``, detail = the last refusal's, ``data.refusals``
    all of them; nothing stopped). Only ``verified is True`` is ``ok`` (``data.result``); no
    result (``no_result``, ``data.backend`` = ``ended``/``alive``/``unknown``) and anything else
    (``unverified``) are not. Progress lines while waiting go to ``out``. A ``KeyboardInterrupt``
    raised by ``ask`` or before the ``shutdown_request`` is the caller's to handle (nothing was
    confirmed then); one while waiting for the result ends as ``no_result``.
    """
    refused = _mismatch(entry)
    if refused is not None:
        return refused
    client, failed = _connect(entry)
    if failed is not None:
        return failed
    refusals: list = []  # each refused confirm's backend detail (expired token vs changed work)
    with client:
        for attempt in range(1 + MAX_WORK_CHANGE_RETRIES):
            pending = _request_shutdown(client)
            if not pending.ok:
                return pending
            active = pending.data["active"]
            if not ask(active):
                return Outcome(False, DECLINED, data={"active": active})
            confirmed = _confirm_shutdown(client, pending.data["token"], out, pending.data["backend"])
            if confirmed.reason != SHUTDOWN_TOKEN_MISMATCH:
                return confirmed
            detail = confirmed.detail
            refusals.append(detail)
            if attempt < MAX_WORK_CHANGE_RETRIES:  # a new list follows only while asks remain
                cause = f" ({detail})" if isinstance(detail, str) and detail else ""
                print(f"{WORK_CHANGED_TEXT}{cause}. {RE_ASK_TEXT}", file=out, flush=True)
    print(WORK_KEPT_CHANGING_TEXT, file=out, flush=True)
    return Outcome(False, WORK_KEPT_CHANGING, refusals[-1], {"active": active, "refusals": refusals})


# -- stop all (2026-10-27 사용자 요청: 전체 끄기) ----------------------------------------------
STOPPED = "stopped"  # confirmed with a verified result
ALREADY_OFF = "already_off"  # listed on, but its backend is gone by the time it was asked
FAILED = "failed"  # asked but not stopped, or stopped without a verified result (reason/detail)
UNCONFIRMED = "unconfirmed"  # unknown/starting row: not asked, and not known to be off (never counted as off)
CLOSING = "closing"  # a shutdown is already in progress (someone else's)
MISMATCH_SKIPPED = "mismatch"  # the registry entry does not match its folder: nothing can be running from it
WORK_CHANGED = "work_changed"  # the work changed since the shown list (reason of a FAILED item)
STOP_ALL_FAILURES = (FAILED, UNCONFIRMED)  # the statuses that make a stop-all end non-zero


@dataclass(frozen=True)
class StopItem:
    """One project's line of a stop-all. ``active`` is the work list the backend showed (if asked)."""
    entry: registry.Entry
    status: str
    reason: str | None = None
    detail: str | None = None
    active: list = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status in STOP_ALL_FAILURES


def entry_from_row(row: dict) -> registry.Entry:
    """The registry entry of a Hub ``list`` row (what the TUI's per-project actions are built from)."""
    return registry.Entry(root=row["root"], project_id=row["project_id"], name=row["name"],
                          registered_at=0.0, source="tui")


def classify_rows(rows: list[dict]) -> tuple[list[registry.Entry], list[StopItem]]:
    """``(on entries to stop, held items)`` from Hub ``list`` rows. ``off`` rows drop out; ``unknown`` and
    ``starting`` rows are ``unconfirmed`` (unknown is never off); ``closing`` and ``mismatch`` are told as
    they are."""
    targets: list[registry.Entry] = []
    held: list[StopItem] = []
    for row in rows:
        power, detail = row.get("power"), row.get("power_detail")
        if power == "on":
            targets.append(entry_from_row(row))
        elif power == "off":
            continue
        elif power == "closing":
            held.append(StopItem(entry_from_row(row), CLOSING))
        elif power == "mismatch":
            held.append(StopItem(entry_from_row(row), MISMATCH_SKIPPED, detail=detail))
        else:  # unknown, starting, or a state this version does not know
            held.append(StopItem(entry_from_row(row), UNCONFIRMED, reason=str(power), detail=detail))
    return targets, held


def _failed_item(entry: registry.Entry, outcome: Outcome, active: list | None = None) -> StopItem:
    return StopItem(entry, FAILED, outcome.reason, outcome.detail, list(active or []))


def _not_running_item(entry: registry.Entry, outcome: Outcome, active: list | None = None) -> StopItem:
    """``not_running`` is "already off" only when a fresh look agrees; anything else is not off."""
    from .status import observe_entry
    try:
        row = observe_entry(entry)
    except Exception:
        row = {"power": "unknown"}
    if row.get("power") == "off":
        return StopItem(entry, ALREADY_OFF)
    return StopItem(entry, FAILED, outcome.reason, f"{outcome.detail} (지금 상태: {row.get('power')})",
                    list(active or []))


def _confirm_shown(entry: registry.Entry, pending: dict, out: TextIO | None) -> Outcome:
    """Confirm with the token of the list the user was shown. A refused token (expired while earlier
    projects were stopped, or work changed) asks once more and goes on only when the new list is exactly
    the shown one; otherwise nothing of this project is stopped (``work_changed``)."""
    token, shown, backend = pending["token"], pending["active"], pending["backend"]
    outcome = power_off_confirm(entry, token, out, backend)
    if outcome.reason != SHUTDOWN_TOKEN_MISMATCH:
        return outcome
    fresh = power_off_request(entry)
    if not fresh.ok:
        return fresh
    if fresh.data["active"] != shown or fresh.data["backend"] != backend:
        return Outcome(False, WORK_CHANGED, WORK_CHANGED_TEXT, {"active": fresh.data["active"]})
    return power_off_confirm(entry, fresh.data["token"], out, fresh.data["backend"])


def power_off_all(entries: list[registry.Entry], ask: Callable[[list[tuple[registry.Entry, list]]], bool],
                  out: TextIO | None = None) -> list[StopItem]:
    """Stop every entry in ``entries`` (the ``on`` ones) with the same two steps as :func:`power_off`
    (``power_off_request`` then ``power_off_confirm``), after ONE ``ask([(entry, active), ...])``.

    Step 1 asks every project's backend for its work list (nothing is stopped); an entry that cannot be
    asked is reported at once. A False ``ask`` stops nothing (every asked entry is ``declined``). Step 2
    confirms the projects one after the other with the token of the list that was shown; a result that
    is not ``verified`` is ``failed`` (never a success, C-AC-22), and one project's failure never stops
    the others from being stopped. Returns the items in ``entries`` order.
    """
    items: dict[str, StopItem] = {}
    pending: dict[str, dict] = {}
    for entry in entries:
        outcome = power_off_request(entry)
        if outcome.ok:
            pending[entry.root] = outcome.data
        elif outcome.reason == NOT_RUNNING:
            items[entry.root] = _not_running_item(entry, outcome)
        else:
            items[entry.root] = _failed_item(entry, outcome)
    asked = [entry for entry in entries if entry.root in pending]
    if asked:
        if not ask([(entry, pending[entry.root]["active"]) for entry in asked]):
            for entry in asked:
                items[entry.root] = StopItem(entry, DECLINED, active=pending[entry.root]["active"])
        else:
            for entry in asked:
                shown = pending[entry.root]["active"]
                outcome = _confirm_shown(entry, pending[entry.root], out)
                if outcome.ok:
                    items[entry.root] = StopItem(entry, STOPPED, active=shown)
                elif outcome.reason == NOT_RUNNING:
                    items[entry.root] = _not_running_item(entry, outcome, shown)
                else:
                    items[entry.root] = _failed_item(entry, outcome, shown)
    return [items[entry.root] for entry in entries]


def stop_item_line(item: StopItem) -> str:
    """The one result line of a project (CLI and TUI alike)."""
    name = f'"{item.entry.name}"'
    cause = f"{item.reason}: {item.detail}" if item.reason and item.detail else (item.reason or item.detail or "")
    if item.status == STOPPED:
        return f"{name}: 껐습니다"
    if item.status == ALREADY_OFF:
        return f"{name}: 이미 꺼져 있습니다"
    if item.status == DECLINED:
        return f"{name}: 취소했습니다 (끄지 않았습니다)"
    if item.status == CLOSING:
        return f"{name}: 이미 종료 중입니다"
    if item.status == MISMATCH_SKIPPED:
        return f"{name}: 불일치({item.detail})라 건너뜁니다"
    if item.status == UNCONFIRMED:
        return f"{name}: 상태 불명({cause})이라 끄지 않았고 꺼졌는지도 확인하지 못했습니다"
    return f"{name}: 끄지 못했습니다 ({cause})"


_REPLY_ENVELOPE = ("v", "id", "type", "ok")


def _request(entry: registry.Entry, kind, fields: dict | None = None, *, boot_id: bool = False) -> Outcome:
    """One non-attached request; ``ok`` replies carry their fields in ``data``, refusals their
    backend ``reason``/``detail`` as is (``not_attached`` → :data:`BACKEND_TOO_OLD`)."""
    refused = _mismatch(entry)
    if refused is not None:
        return refused
    from workbench.backend.client import ClientError
    from workbench.contracts.ui_v1 import ClientType, Reason
    client, failed = _connect(entry)
    if failed is not None:
        return failed
    fields = dict(fields or {})
    with client:
        try:
            if boot_id:
                current = (client.snapshot().get("boot") or {}).get("boot_id")
                if not isinstance(current, str) or not current:
                    return Outcome(False, BOOT_ID_UNAVAILABLE, "the snapshot has no current boot id")
                fields["boot_id"] = current
            reply = client.request(kind, **fields)
        except (OSError, TimeoutError, ClientError) as exc:
            return Outcome(False, UNREACHABLE, f"{kind.value} failed: {type(exc).__name__}: {exc}")
    if reply.get("ok") is True:
        return Outcome(True, data={key: value for key, value in reply.items() if key not in _REPLY_ENVELOPE})
    reason = reply.get("reason")
    if reason == Reason.NOT_ATTACHED.value and kind in (ClientType.PAUSE, ClientType.RESUME):
        return Outcome(False, BACKEND_TOO_OLD,
                       "this backend refuses pause/resume without attach; restart it with the current wb")
    return Outcome(False, reason if isinstance(reason, str) and reason else UNREACHABLE, reply.get("detail"))


def pause(entry: registry.Entry) -> Outcome:
    """ui_v1 ``pause`` without attaching (HB-06): an attached UI keeps its slot and gets ``state``."""
    from workbench.contracts.ui_v1 import ClientType
    return _request(entry, ClientType.PAUSE)


def resume(entry: registry.Entry) -> Outcome:
    """ui_v1 ``resume`` with ``reconciled: true`` without attaching (HB-06)."""
    from workbench.contracts.ui_v1 import ClientType
    return _request(entry, ClientType.RESUME, {"reconciled": True})


def confirm_boot(entry: registry.Entry) -> Outcome:
    """ui_v1 ``confirm_boot`` for the current snapshot's ``boot.boot_id``; the backend's refusal
    (``boot_confirmation_not_required``, ``boot_id_mismatch``, ...) is returned as is."""
    from workbench.contracts.ui_v1 import ClientType
    return _request(entry, ClientType.CONFIRM_BOOT, boot_id=True)
