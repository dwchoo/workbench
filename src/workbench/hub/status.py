"""Hub status aggregation: ``check_entry``, per-row observation and the ``list`` cache.

SPEC "상태 집계": on every ``list`` the Hub re-reads the registry and observes each entry
(at most :data:`MAX_WORKERS` threads, :data:`OBSERVE_TIMEOUT` per entry) through a
non-attached ui_v1 connection. An observation failure is "연결 없음/불명" (``unknown``),
never ``off``. Rows are cached :data:`CACHE_SECONDS`; ``fresh`` observes again. Nothing
here writes a file: the cache is memory only.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import time

from . import registry
from .paths import HubLayout

OBSERVE_TIMEOUT = 2.0
CACHE_SECONDS = 2.0
MAX_WORKERS = 8
OBSERVE_SLACK = 1.0  # loop-side bound beyond the thread's own deadline (queueing, a stuck call)
# The TUI list (HB-08) polls ``list`` this often.
POLL_INTERVAL = 3.0
# The hello ``client`` of every observation; ui_v1 also accepts non-attached pause/resume from it.
CLIENT_NAME = "workbench-hub"


def _row(entry: registry.Entry, power: str, detail: str | None = None, **fields) -> dict:
    """A row with SPEC defaults: every field not observed is ``null``/``[]``."""
    row = {"root": entry.root, "name": entry.name, "project_id": entry.project_id, "power": power,
           "power_detail": detail, "task": None, "command": None, "waits": [], "pending_requests": None,
           "review_mode": None, "last_activity": None, "backend": None, "observed_at": time.time()}
    row.update(fields)
    return row


def _mtime(path: Path) -> float | None:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return None


MISMATCH_DETAILS = ("root_missing", "not_initialized", "marker_invalid", "project_id_mismatch")


def check_entry(entry: registry.Entry) -> str | None:
    """The first SPEC mismatch of a registry entry (a ``MISMATCH_DETAILS`` code), or None.

    Read-only: nothing is created, and the marker is validated by ``read_project_marker``.
    """
    from workbench.backend.paths import DataDirError
    from workbench.backend.project import read_project_marker
    root = Path(entry.root)
    if not os.path.lexists(root):
        return "root_missing"
    data = root / ".wb"
    if not os.path.lexists(data):
        return "not_initialized"
    try:
        marker = read_project_marker(data)
    except (DataDirError, OSError, ValueError):
        return "marker_invalid"
    if marker.get("project_id") != entry.project_id:
        return "project_id_mismatch"
    return None


def observe_entry(entry: registry.Entry, timeout: float = OBSERVE_TIMEOUT) -> dict:
    """One registry entry's row (SPEC ``power`` order); blocking, so it runs in an executor thread.

    The runtime root comes from the validated ``backend.json`` (``inspect_backend``), never
    from this process's environment.
    """
    from workbench.backend.paths import DataDirError, DataLayout
    from workbench.backend.process_identity import BackendIdentityError, inspect_backend
    mismatch = check_entry(entry)
    if mismatch is not None:
        return _row(entry, "mismatch", mismatch)
    layout = DataLayout(Path(entry.root) / ".wb")
    try:
        identity = inspect_backend(layout)
    except BackendIdentityError as exc:
        return _row(entry, "unknown", f"identity:{exc.reason}")
    except (DataDirError, OSError, ValueError):
        return _row(entry, "unknown", "identity:unavailable")
    if identity.state == "stopped":
        return _row(entry, "off", last_activity=_mtime(layout.record))
    if identity.state == "starting":
        return _row(entry, "starting")
    try:
        snapshot = _snapshot(identity, time.monotonic() + timeout)
    except _SnapshotFailure as exc:
        if exc.detail == "closing":  # the verified backend said it is shutting down
            reference = identity.record["processes"]["backend"]
            return _row(entry, "closing",
                        backend={"pid": reference["pid"], "start_ticks": reference["start_ticks"]})
        return _row(entry, "unknown", exc.detail)
    reference = identity.record["processes"]["backend"]
    return _row(entry, _snapshot_power(snapshot), **_snapshot_fields(snapshot),
                backend={"pid": reference["pid"], "start_ticks": reference["start_ticks"]})


def _snapshot_power(snapshot: dict) -> str:
    """``closing`` (a ``shutdown_closing`` hold) before ``starting`` (phase) before ``on``."""
    if any(isinstance(item, dict) and item.get("reason") == "shutdown_closing"
           for item in snapshot.get("holds") or ()):
        return "closing"
    return "starting" if snapshot.get("phase") == "starting" else "on"


def _epoch(value: object) -> float | None:
    """Epoch seconds from a number or an ISO-8601 time (``...Z`` allowed), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    return None


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


TASK_FIELDS = ("task_id", "kind", "status", "summary")
COMMAND_FIELDS = ("command_id", "owner", "label", "state")


def _snapshot_fields(snapshot: dict) -> dict:
    """``task``, ``command`` and ``last_activity`` of an observed row (SPEC "필드 출처")."""
    task = snapshot.get("task")
    shown = ({key: task.get(key) for key in TASK_FIELDS}
             if isinstance(task, dict) and task.get("status") != "closed" else None)
    command = _dict(_dict(snapshot.get("panes")).get("host_shell")).get("command")
    command = {key: command.get(key) for key in COMMAND_FIELDS} if isinstance(command, dict) else None
    times = [moment for moment in (_epoch(_dict(snapshot.get("backend")).get("started_at")),
                                   _epoch(_dict(task).get("since"))) if moment is not None]
    return {"task": shown, "command": command, "waits": _waits(snapshot),
            "last_activity": max(times) if times else None}


# SPEC "대기 사유" order; the codes are presentation's WAIT_LABELS keys.
WAIT_CODES = ("paused", "boot_confirmation_required", "manager_editor_not_empty", "manager_session_not_connected",
              "manager_busy", "metadata_unavailable", "model_hold", "task_held")
REPORT_WAITS = ("manager_editor_not_empty", "manager_session_not_connected")
# Task Inbox (TI-10): the waits of request notices (snapshot ``inbox.notices[].wait``); ``paused`` is shown once
INBOX_WAITS = ("manager_editor_not_empty", "manager_session_not_connected", "manager_busy")
MODEL_HOLD_PREFIX = "model_hold:"


def _wait(code: str, since: object = None, detail: object = None) -> dict:
    return {"code": code, "since": _epoch(since), "detail": detail if isinstance(detail, str) else None}


def _waits(snapshot: dict) -> list[dict]:
    """``[{code, since, detail}]`` in :data:`WAIT_CODES` order, each code at most once."""
    holds = [item for item in snapshot.get("holds") or () if isinstance(item, dict)
             and isinstance(item.get("reason"), str)]

    def hold(match) -> dict | None:
        return next((item for item in holds if match(item["reason"])), None)

    waits = []
    if _dict(snapshot.get("automation")).get("paused") is True:
        waits.append(_wait("paused"))
    if (found := hold(lambda reason: reason == "boot_confirmation_required")) is not None:
        waits.append(_wait("boot_confirmation_required", found.get("since"), found.get("detail")))
    report = _dict(_dict(snapshot.get("recovery")).get("report_wait"))
    if report.get("reason") in REPORT_WAITS:
        waits.append(_wait(report["reason"], report.get("since")))
    notices = [item for item in _dict(snapshot.get("inbox")).get("notices") or () if isinstance(item, dict)]
    for code in INBOX_WAITS:
        since = [item.get("queued_at") for item in notices if item.get("wait") == code]
        if since and all(wait["code"] != code for wait in waits):
            moments = [moment for moment in map(_epoch, since) if moment is not None]
            waits.append(_wait(code, min(moments) if moments else None))
    if any(item.get("wait") == "paused" for item in notices) and all(wait["code"] != "paused" for wait in waits):
        waits.insert(0, _wait("paused"))
    if (found := hold(lambda reason: reason == "metadata_unavailable")) is not None:
        waits.append(_wait("metadata_unavailable", found.get("since"), found.get("detail")))
    if (found := hold(lambda reason: reason.startswith(MODEL_HOLD_PREFIX))) is not None:
        waits.append(_wait("model_hold", found.get("since"), found.get("detail")))
    task = _dict(snapshot.get("task"))
    if task.get("status") == "held":
        waits.append(_wait("task_held", task.get("since"), task.get("held_reason")))
    return waits


class _SnapshotFailure(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail  # unreachable | timeout | snapshot_mismatch | closing


def _snapshot(identity, deadline: float) -> dict:
    """ui_v1 ``snapshot`` over a non-attached ``workbench-hub`` connection, bounded by ``deadline``.

    The socket peer must be the recorded backend process (``expected_process``) and the
    snapshot must name it (``check_snapshot_identity``, shared with ``wb status``).
    """
    from workbench.backend.client import ClientError, UiClient
    from workbench.backend.cli import check_snapshot_identity
    from workbench.backend.process_identity import BackendIdentityError
    from workbench.contracts.ui_v1 import ClientType, Reason, ServerType
    record = identity.record
    try:
        with UiClient(identity.layout.ui_socket, name=CLIENT_NAME, timeout=max(0.001, deadline - time.monotonic()),
                      expected_process=record["processes"]["backend"]) as client:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("observation deadline passed")
            reply = client.request(ClientType.SNAPSHOT, timeout=remaining)
    except TimeoutError as exc:  # socket.timeout included
        raise _SnapshotFailure("timeout") from exc
    except ClientError as exc:
        # A ``closing`` frame (during hello or instead of the result) from the peer-verified
        # backend: ``backend_shutdown`` means the backend is closing; any other reason is ours.
        closing = exc.header.get("type") == ServerType.CLOSING.value
        shutdown = exc.header.get("reason") == Reason.BACKEND_SHUTDOWN.value
        raise _SnapshotFailure("closing" if closing and shutdown else "unreachable") from exc
    except Exception as exc:  # refused, gone, wrong peer, closed, bad frame: never "off"
        raise _SnapshotFailure("unreachable") from exc
    snapshot = reply.get("snapshot")
    if reply.get("ok") is not True or not isinstance(snapshot, dict):
        raise _SnapshotFailure("unreachable")
    try:
        check_snapshot_identity(identity, snapshot, identity.layout.root)
    except (BackendIdentityError, AttributeError, TypeError, KeyError) as exc:
        raise _SnapshotFailure("snapshot_mismatch") from exc
    return snapshot


def _guarded(entry: registry.Entry, timeout: float) -> dict:
    """No observation failure may end the ``list`` reply or read as ``off``."""
    try:
        return observe_entry(entry, timeout)
    except Exception:
        return _row(entry, "unknown", "observation_error")


class Lister:
    """The ``list`` op: registry re-read, parallel observation off the event loop, row cache.

    Each observation runs in its own daemon thread; at most ``workers`` run within their
    deadline at once. A thread still running past ``timeout`` + :data:`OBSERVE_SLACK` (a
    hung mount, a stuck call) gives its slot back and its row reads ``unknown/timeout``;
    no second thread starts for that root until it ends, so threads stay bounded by the
    registry and :meth:`close` never waits for them.
    """

    def __init__(self, layout: HubLayout, *, workers: int = MAX_WORKERS, timeout: float = OBSERVE_TIMEOUT,
                 cache_seconds: float = CACHE_SECONDS, clock=time.monotonic, requests=None):
        """``requests(entries)`` (TI-10, blocking, run in an executor): ``{root: {pending_requests, review_mode}}``
        from the project inboxes, laid over the observed rows (never cached with them)."""
        self.layout = layout
        self.requests = requests
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.clock = clock
        self.slots = asyncio.Semaphore(workers)
        self.cache: dict[registry.Entry, tuple[float, dict]] = {}  # entry -> (monotonic done, row)
        self.inflight: dict[registry.Entry, asyncio.Future] = {}  # observations within their deadline
        self.running: dict[str, asyncio.Future] = {}  # root -> result of a thread that has not ended

    async def list(self, fresh: bool = False) -> dict:
        """``{observed_at, registry:{state, path}, rows}``; the registry is read on every call."""
        loop = asyncio.get_running_loop()
        summary = {"state": "ok", "path": str(self.layout.registry)}
        try:
            entries = await loop.run_in_executor(None, registry.read, self.layout)
        except (registry.RegistryError, OSError):
            self.cache.clear()
            return {"observed_at": time.time(), "registry": {**summary, "state": "invalid"}, "rows": []}
        for stale in set(self.cache) - set(entries):
            del self.cache[stale]
        rows = await asyncio.gather(*(self.row(entry, fresh) for entry in entries))
        rows = await self._with_requests(entries, rows)
        return {"observed_at": time.time(), "registry": summary, "rows": list(rows)}

    async def _with_requests(self, entries: list, rows: list) -> list:
        if self.requests is None:
            return list(rows)
        try:
            extra = await asyncio.get_running_loop().run_in_executor(None, self.requests, entries)
        except Exception:  # an inbox problem leaves the counts unknown, never the list
            extra = {}
        return [{**row, **extra.get(row["root"], {})} for row in rows]

    async def list_one(self, project_id: str, fresh: bool = True) -> dict:
        """``list`` for one registered project: ``{observed_at, registry, rows}`` with at most that project's row,
        observed afresh (sharing an observation in progress) while the other projects are not touched."""
        loop = asyncio.get_running_loop()
        summary = {"state": "ok", "path": str(self.layout.registry)}
        try:
            entries = await loop.run_in_executor(None, registry.read, self.layout)
        except (registry.RegistryError, OSError):
            self.cache.clear()
            return {"observed_at": time.time(), "registry": {**summary, "state": "invalid"}, "rows": []}
        wanted = [entry for entry in entries if entry.project_id == project_id]
        rows = await asyncio.gather(*(self.row(entry, fresh) for entry in wanted))
        rows = await self._with_requests(wanted, rows)
        return {"observed_at": time.time(), "registry": summary, "rows": rows}

    async def row(self, entry: registry.Entry, fresh: bool) -> dict:
        """A cached row younger than ``cache_seconds`` unless ``fresh``; an observation in
        progress is shared (``fresh`` included); one thread per root at a time."""
        if not fresh:
            cached = self.cache.get(entry)
            if cached is not None and self.clock() - cached[0] < self.cache_seconds:
                return cached[1]
        current = self.inflight.get(entry)
        if current is not None and not current.done():
            return await asyncio.shield(current)
        if entry.root in self.running:  # the last thread outlived its deadline and is still blocked
            row = _row(entry, "unknown", "timeout")
            self.cache[entry] = (self.clock(), row)
            return row
        future = asyncio.ensure_future(self._observe(entry))
        self.inflight[entry] = future
        future.add_done_callback(lambda done: self.inflight.get(entry) is done and self.inflight.pop(entry))
        return await asyncio.shield(future)

    async def _observe(self, entry: registry.Entry) -> dict:
        loop = asyncio.get_running_loop()
        # Waiting for a slot is not part of a row's budget; once the thread runs it bounds
        # itself by ``timeout`` and the slack only covers a call stuck beyond it.
        async with self.slots:
            result = loop.create_future()
            root = entry.root
            self.running[root] = result
            result.add_done_callback(lambda done: self.running.get(root) is done and self.running.pop(root))

            def deliver(row: dict) -> None:
                if not result.done():
                    result.set_result(row)

            def run() -> None:
                row = _guarded(entry, self.timeout)
                try:
                    loop.call_soon_threadsafe(deliver, row)
                except RuntimeError:  # the loop is closed: the Hub is stopping
                    pass

            threading.Thread(target=run, name="hub-observe", daemon=True).start()
            try:
                row = await asyncio.wait_for(asyncio.shield(result), self.timeout + OBSERVE_SLACK)
            except asyncio.TimeoutError:
                row = _row(entry, "unknown", "timeout")
        self.cache[entry] = (self.clock(), row)
        return row

    def close(self) -> None:
        """Observation threads are daemons: a stopping Hub never waits for a blocked one."""
