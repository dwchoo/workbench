"""Route confirmed subject facts to the catalog's current active UUID."""

from __future__ import annotations

from datetime import datetime, timezone
import errno
from typing import Any, Callable, Mapping

from workbench.contracts.logs_v1 import parse_subject_ref
from .catalog import ImportantLogCatalog, _storage_reason
from .lifecycle import SubjectLifecycle, _CLOSE_CALL_REASONS, _StaleFlowEvent
from .maintenance import ImportantLogMaintenance
from .store import ImportantLogStore, ImportantLogStoreError


def _table(conn: Any, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _copy_row(target: Any, table: str, row: Any, key_columns: tuple[str, ...]) -> None:
    if row is None:
        return
    columns = tuple(row.keys())
    if table not in ("subject_lifecycle_v1", "subject_events_v1", "subject_commands_v1"):
        raise ImportantLogStoreError("unsupported subject state carry")
    names = ",".join(columns)
    marks = ",".join("?" for _ in columns)
    target.execute(f"INSERT OR IGNORE INTO {table}({names}) VALUES({marks})",
                   tuple(row[name] for name in columns))
    where = " AND ".join(f"{name}=?" for name in key_columns)
    existing = target.execute(f"SELECT * FROM {table} WHERE {where}",
                              tuple(row[name] for name in key_columns)).fetchone()
    if existing is None or dict(existing) != dict(row):
        raise ImportantLogStoreError("carried subject state conflicts with active DB")


class ActiveSubjectLog:
    """The DVL-05 sink, rebound under one catalog lease per confirmed event.

    Only a subject's small lifecycle/command row and the exact replayed event
    key are copied on demand. Original entry bodies remain in their source DB.
    """

    def __init__(self, catalog: ImportantLogCatalog):
        self.catalog = catalog
        self.last_failure: str | None = None

    def _archived_row(self, fd: int, pointer: dict[str, Any], table: str,
                      where: str, params: tuple[Any, ...]) -> Any:
        for identity in reversed(pointer["archives"]):
            with self.catalog._archive_source(fd, pointer, identity) as source:
                conn = source._connection
                if not _table(conn, table):
                    continue
                row = conn.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
                if row is not None:
                    return dict(row)
        return None

    def _prime(self, fd: int, pointer: dict[str, Any], store: ImportantLogStore,
               subject: Mapping[str, Any], event_key: str | None,
               command_id: str | None) -> None:
        conn = store._connection
        kind, identity = subject["kind"], subject["id"]
        state = conn.execute("""SELECT * FROM subject_lifecycle_v1
            WHERE subject_kind=? AND subject_id=?""", (kind, identity)).fetchone()
        if state is None:
            state = self._archived_row(fd, pointer, "subject_lifecycle_v1",
                                       "subject_kind=? AND subject_id=?", (kind, identity))
        command = None
        if command_id is not None:
            command = conn.execute("SELECT * FROM subject_commands_v1 WHERE command_id=?",
                                   (command_id,)).fetchone()
            if command is None:
                command = self._archived_row(fd, pointer, "subject_commands_v1",
                                             "command_id=?", (command_id,))
        event = None
        if event_key is not None:
            event = conn.execute("SELECT * FROM subject_events_v1 WHERE event_key=?",
                                 (event_key,)).fetchone()
            if event is None:
                event = self._archived_row(fd, pointer, "subject_events_v1",
                                           "event_key=?", (event_key,))
        if state is None and command is None and event is None:
            return
        with store.transaction():
            _copy_row(conn, "subject_lifecycle_v1", state, ("subject_kind", "subject_id"))
            _copy_row(conn, "subject_commands_v1", command, ("command_id",))
            _copy_row(conn, "subject_events_v1", event, ("event_key",))

    def _perform(self, subject: Mapping[str, Any] | None, event_key: str | None,
                 command_id: str | None, action: Callable[[SubjectLifecycle], Any]) -> Any:
        committed = False
        try:
            with self.catalog._lease() as fd:
                pointer = self.catalog._current(fd)
                with ImportantLogStore.open(self.catalog.directory, pointer["active"],
                                            _catalog_owned=True) as store:
                    now = datetime.now(timezone.utc)
                    maintenance = ImportantLogMaintenance(store, target_bytes=self.catalog.target_bytes,
                                                         low_water_bytes=self.catalog.low_water_bytes)
                    check = maintenance.quick_check(now=now)
                    if check["status"] not in ("ok", "cached"):
                        raise ImportantLogStoreError("active quick_check incomplete")
                    pre = store.health_snapshot(include_subject_stats=False)
                    if pre["diskFreeBytes"] < 20_000_000:
                        raise OSError(errno.ENOSPC, "important storage physical free below transaction budget")
                    lifecycle = SubjectLifecycle(self.catalog.directory, pointer["active"],
                                                 _catalog_owned=True)
                    if subject is None:
                        if command_id is None:
                            raise ValueError("subject or command required")
                        command = store._connection.execute(
                            "SELECT * FROM subject_commands_v1 WHERE command_id=?", (command_id,)).fetchone()
                        if command is None:
                            command = self._archived_row(fd, pointer, "subject_commands_v1",
                                                         "command_id=?", (command_id,))
                        if command is None:
                            raise ImportantLogStoreError("command subject is not durable")
                        subject = {"schemaVersion": 1, "kind": "task" if command["task_id"] else "command",
                                   "id": command["task_id"] or command_id}
                    subject = parse_subject_ref(subject)
                    self._prime(fd, pointer, store, subject, event_key, command_id)
                    result = action(lifecycle)
                    committed = True  # the action may return None/False after a durable state change
                    outcome = self.catalog._after_write(fd, pointer, store, now=now)
                    self.catalog.last_status = {"state": "ok", "pre": pre, **outcome}
                    self.last_failure = None
                    return result
        except Exception as exc:
            self.last_failure = type(exc).__name__
            self.catalog.last_status = {"state": _storage_reason(exc),
                                        "error": type(exc).__name__, "commitUnknown": committed}
            raise

    def record_flow(self, record: Mapping[str, Any]) -> bool:
        kind = record.get("type")
        if kind not in ("delegated", "task_closed", "task"):
            return False
        task_id = (record.get("task") or {}).get("task_id") if kind == "task" else record.get("task_id")
        subject = {"schemaVersion": 1, "kind": "task", "id": task_id}
        return self._perform(subject, f"flow:{record.get('seq')}", None,
                             lambda life: life.record_flow(record))

    def reconcile_flow(self, records: list[dict[str, Any]]) -> int:
        copied = 0
        stale: _StaleFlowEvent | None = None
        ordered = sorted(records, key=lambda record: record.get("seq")
                         if type(record.get("seq")) is int else -1)
        for record in ordered:
            try:
                if self.record_flow(record):
                    copied += 1
            except _StaleFlowEvent as exc:
                stale = exc
        if stale is not None:
            raise stale
        return copied

    def record_terminal(self, record: Mapping[str, Any], *, revision: int | None = None) -> bool:
        kind = record.get("type")
        if kind not in ("terminal_started", "terminal_ended"):
            return False
        command_id = record.get("command_id")
        task_id = record.get("task_id") if kind == "terminal_started" else None
        subject = ({"schemaVersion": 1, "kind": "task" if task_id else "command",
                    "id": task_id or command_id} if kind == "terminal_started" else None)
        return self._perform(subject, f"terminal:{record.get('seq')}", command_id,
                             lambda life: life.record_terminal(record, revision=revision))

    def record_delivery(self, subject: Mapping[str, Any], *, message_id: str, revision: int,
                        kind: str, content: str, delivered_at: str) -> bool:
        return self._perform(subject, f"message:{message_id}", None,
            lambda life: life.record_delivery(subject, message_id=message_id, revision=revision,
                                              kind=kind, content=content, delivered_at=delivered_at))

    def record_terminal_range(self, subject: Mapping[str, Any], source_record_id: str,
                              span: Mapping[str, Any], *, observed_at: str) -> bool:
        source = span.get("source") or {}
        return self._perform(subject, f"terminal_range:{source_record_id}",
            source.get("commandId"),
            lambda life: life.record_terminal_range(subject, source_record_id, span,
                                                    observed_at=observed_at))

    def archive_close(self, subject: Mapping[str, Any], *, at: str,
                      user_confirmed: bool) -> None:
        item = parse_subject_ref(subject)

        def close_known(life: SubjectLifecycle) -> None:
            if not life.state(item):
                raise ImportantLogStoreError("subject has no durable lifecycle state")
            life.archive_close(item, at=at, user_confirmed=user_confirmed)

        self._perform(item, None, None, close_known)

    def record_direct_origin(self, command_id: str, *, event_id: str, epoch: int,
                             observed_at: str, verified_direct_instruction: bool) -> None:
        subject = {"schemaVersion": 1, "kind": "command", "id": command_id}
        self._perform(subject, None, command_id,
            lambda life: life.record_direct_origin(
                command_id, event_id=event_id, epoch=epoch, observed_at=observed_at,
                verified_direct_instruction=verified_direct_instruction))

    def mark_command_end_summary_processed(self, command_id: str) -> None:
        subject = {"schemaVersion": 1, "kind": "command", "id": command_id}
        self._perform(subject, None, command_id,
            lambda life: life.mark_command_end_summary_processed(command_id))

    def reconcile_terminal(self, records: list[dict[str, Any]], *,
                           task_revisions: Mapping[str, int]) -> int:
        copied = 0
        for record in records:
            if record.get("type") not in ("terminal_started", "terminal_ended"):
                continue
            revision = (task_revisions.get(record.get("task_id"))
                        if record.get("type") == "terminal_started" else None)
            if self.record_terminal(record, revision=revision):
                copied += 1
        return copied

    def state(self, subject: Mapping[str, Any]) -> dict[str, Any]:
        """Read the newest subject state without copying it or starting maintenance."""
        item = parse_subject_ref(subject)
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with ImportantLogStore.open(self.catalog.directory, pointer["active"], readonly=True) as store:
                if _table(store._connection, "subject_lifecycle_v1"):
                    row = store._connection.execute("""SELECT * FROM subject_lifecycle_v1
                        WHERE subject_kind=? AND subject_id=?""", (item["kind"], item["id"])).fetchone()
                    if row is not None:
                        return dict(row)
            return self._archived_row(fd, pointer, "subject_lifecycle_v1",
                                      "subject_kind=? AND subject_id=?",
                                      (item["kind"], item["id"])) or {}

    def authority(self, subject: Mapping[str, Any], *, active: bool, paused: bool,
                  revoked: bool, has_new_input: bool,
                  closing_event: bool = False) -> dict[str, Any]:
        """The same bounded fact as SubjectLifecycle, using the latest DB state."""
        item = parse_subject_ref(subject)
        state = self.state(item)
        if not state or paused or revoked or not has_new_input or (not closing_event and active is not True):
            return {"allowed": False, "reason": "inactive_or_unknown"}
        if state["storage_unknown"]:
            return {"allowed": False, "reason": "storage_unknown"}
        if item["kind"] == "task":
            ref = state["prior_grant_ref"] if closing_event else state["grant_ref"]
            if not ref or (closing_event and state["core_closed_reason"] not in _CLOSE_CALL_REASONS):
                return {"allowed": False, "reason": "no_matching_task_grant"}
            if not closing_event and state["core_status"] == "closed":
                return {"allowed": False, "reason": "task_closed"}
            return {"allowed": True, "authorization_ref": ref, "revision": state["grant_revision"]}
        if not state["direct_origin_ref"] or state["direct_expired"]:
            return {"allowed": False, "reason": "no_direct_command_origin"}
        if not closing_event and state["command_end_status"] is not None:
            return {"allowed": False, "reason": "command_already_ended"}
        if closing_event and state["command_end_status"] != "exited":
            return {"allowed": False, "reason": "command_end_unknown"}
        return {"allowed": True, "authorization_ref": state["direct_origin_ref"],
                "authorization_epoch": state["direct_origin_epoch"]}
