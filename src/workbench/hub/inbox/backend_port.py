"""Hub → backend over ui_v1 without attaching (Task Inbox SPEC "Hub → backend").

Every call checks the backend's identity like Hub observation (``status._snapshot``): the endpoint comes from a
validated ``backend.json`` (``inspect_backend``), and the socket peer must be the recorded backend process
(``UiClient(expected_process=...)``). The hello ``client`` is ``workbench-hub``; the backend serves these requests only
to that name from the same uid. Blocking: call from an executor thread.

Failures raise :class:`BackendPortError` with ``reason``:
``backend_not_running`` (stopped/starting; nothing contacted), ``backend_unreachable`` (identity unverifiable,
socket refused, wrong peer; nothing was sent), ``backend_too_old`` (the backend answered ``unsupported_type`` or
``not_attached``: it predates the request), ``unknown`` (sent, no answer in time or the connection ended: the outcome
is unknown), or the backend's own refusal reason (``invalid_message``, ``task_changed``, ...).

``detail`` is always the fixed sentence of :data:`DETAILS` for the reason (never exception or backend text, which may
name paths); the underlying text goes to the Hub log (stderr) only. Web and MCP layers expose only ``reason``.
"""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from workbench.contracts.ui_v1 import ClientType, Reason

CLIENT_NAME = "workbench-hub"
TIMEOUT = 5.0
TOO_OLD = frozenset({Reason.UNSUPPORTED_TYPE.value, Reason.NOT_ATTACHED.value})


DETAILS = {
    "backend_not_running": "The project's backend is not running.",
    "backend_unreachable": "The project's backend could not be reached or verified.",
    "backend_too_old": "The project's backend is too old for this request.",
    "unknown": "The backend did not answer; the outcome is unknown.",
}
REFUSED = "The project's backend refused the request."


class BackendPortError(Exception):
    """``reason`` is the code; ``detail`` a generic sentence; ``header`` the backend's reply when there was one."""

    def __init__(self, reason: str, log: str = "", header: dict | None = None):
        self.reason, self.detail, self.header = reason, DETAILS.get(reason, REFUSED), header or {}
        super().__init__(f"{reason}: {self.detail}")
        if log:  # the specific text stays in the Hub log
            print(f"[workbench] backend request failed ({reason}): {log}", file=sys.stderr, flush=True)


def call(root: Path, kind: ClientType, *, timeout: float = TIMEOUT, **fields: Any) -> dict[str, Any]:
    """One request to the running backend of WB project ``root``; its ``ok: true`` result header."""
    from workbench.backend.client import ClientError, NotRunning, UiClient
    from workbench.backend.paths import DataDirError, DataLayout
    from workbench.backend.process_identity import BackendIdentityError, inspect_backend
    try:
        identity = inspect_backend(DataLayout(Path(root) / ".wb"))
    except BackendIdentityError as exc:
        raise BackendPortError("backend_unreachable", f"identity:{exc.reason}") from None
    except (DataDirError, OSError, ValueError) as exc:
        raise BackendPortError("backend_unreachable", f"identity:{type(exc).__name__}") from None
    if identity.state != "running" or identity.record is None:
        raise BackendPortError("backend_not_running", identity.state)
    reference = identity.record["processes"]["backend"]
    try:
        client = UiClient(identity.layout.ui_socket, name=CLIENT_NAME, timeout=timeout,
                          expected_process={"pid": reference["pid"], "start_ticks": reference["start_ticks"]})
    except (NotRunning, ClientError, OSError) as exc:
        raise BackendPortError("backend_unreachable", f"{type(exc).__name__}: {exc}") from None
    with client:
        try:
            reply = client.request(kind, timeout=timeout, **fields)
        except (ClientError, OSError, TimeoutError) as exc:  # sent (or partly): the outcome is unknown
            raise BackendPortError("unknown", f"{type(exc).__name__}: {exc}") from None
    if reply.get("ok") is True:
        return reply
    reason = reply.get("reason")
    if reason in TOO_OLD:
        raise BackendPortError("backend_too_old", f"{kind.value}: {reason}", reply)
    raise BackendPortError(reason if isinstance(reason, str) else "unknown", str(reply.get("detail") or ""), reply)


def task_status(root: Path, task_ids: list[str], *, detail: bool = False, timeout: float = TIMEOUT) -> list[dict]:
    """``task_status``: one item per id in order (``{task_id, exists: false}`` for an unknown one)."""
    reply = call(root, ClientType.TASK_STATUS, timeout=timeout, task_ids=list(task_ids), detail=detail)
    tasks = reply.get("tasks")
    if not isinstance(tasks, list):
        raise BackendPortError("unknown", "task_status answer without tasks", reply)
    return tasks


def inbox_notice(root: Path, request_id: str, delivery_id: str, event: str, fields: dict[str, Any], *,
                 timeout: float = TIMEOUT) -> dict[str, Any]:
    """``inbox_notice`` → ``{queued, notice_id}`` (TI-10 fills the backend side)."""
    return call(root, ClientType.INBOX_NOTICE, timeout=timeout, request_id=request_id, delivery_id=delivery_id,
                event=event, fields=fields)


def cancel_task(root: Path, task_id: str, request_id: str, expect: dict[str, Any], reason: str | None, *,
                timeout: float = TIMEOUT) -> dict[str, Any]:
    """``cancel_task`` → ``{cancel, stop}`` (TI-13 fills the backend side)."""
    return call(root, ClientType.CANCEL_TASK, timeout=timeout, task_id=task_id, request_id=request_id, expect=expect,
                reason=reason)


def cancel_port(ref: Any, task_id: str, request_id: str, expect: dict[str, Any], reason: str | None) -> dict[str, Any]:
    """The ``InboxService`` cancel port (TI-13) over :func:`cancel_task`: ``{outcome: "accepted", cancel, stop}``,
    ``{outcome: "refused", reason}`` (nothing was sent, or the backend refused: nothing was cancelled) or
    ``{outcome: "unknown"}`` (sent, no answer)."""
    try:
        reply = cancel_task(Path(ref.root), task_id, request_id, expect, reason)
    except BackendPortError as exc:
        if exc.reason == "unknown":
            return {"outcome": "unknown"}
        return {"outcome": "refused", "reason": exc.reason}
    stop = reply.get("stop")
    return {"outcome": "accepted", "cancel": reply.get("cancel"), "stop": stop if isinstance(stop, dict) else None}
