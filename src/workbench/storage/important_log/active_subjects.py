"""Route confirmed subject facts to the catalog's current active UUID."""

from __future__ import annotations

from contextlib import ExitStack
import json
from datetime import datetime, timezone
import errno
from typing import Any, Callable, Mapping

from workbench.contracts.logs_v1 import canonical_json, parse_summary_request, parse_subject_ref
from .catalog import ImportantLogCatalog, _storage_reason
from .lifecycle import SubjectLifecycle, _CLOSE_CALL_REASONS, _StaleFlowEvent, authorize_summary_sources
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
            return {"allowed": True, "authorization_ref": ref,
                    "authorization_epoch": state["event_cursor"], "revision": state["grant_revision"]}
        if state["direct_expired"]:
            return {"allowed": False, "reason": "command_summary_scope_expired"}
        if not closing_event and state["command_end_status"] is not None:
            return {"allowed": False, "reason": "command_already_ended"}
        if closing_event and state["command_end_status"] != "exited":
            return {"allowed": False, "reason": "command_end_unknown"}
        starts = set()
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with ExitStack() as stack:
                stores = [stack.enter_context(ImportantLogStore.open(self.catalog.directory, pointer["active"], readonly=True))]
                stores += [stack.enter_context(self.catalog._archive_source(fd, pointer, identity))
                           for identity in pointer["archives"]]
                for store in stores:
                    if _table(store._connection, "subject_events_v1"):
                        starts.update(tuple(row) for row in store._connection.execute("""SELECT e.event_key,e.source_id
                            FROM subject_events_v1 e JOIN subject_commands_v1 c
                            ON c.command_id=e.subject_id AND c.task_id IS NULL
                            WHERE e.subject_kind='command' AND e.subject_id=?
                              AND e.event_kind='terminal_started'""", (item["id"],)))
        if len(starts) != 1:
            return {"allowed": False, "reason": "no_confirmed_command_start"}
        key, ref = starts.pop()
        try:
            epoch = int(key.removeprefix("terminal:"))
        except ValueError:
            return {"allowed": False, "reason": "command_start_epoch_unknown"}
        if key != f"terminal:{epoch}" or epoch < 1:
            return {"allowed": False, "reason": "command_start_epoch_unknown"}
        return {"allowed": True, "authorization_ref": ref,
                "authorization_epoch": epoch, "command_id": item["id"]}

    def authorize_summary_request(self, request: Mapping[str, Any], **flags: Any) -> dict[str, Any]:
        parsed = parse_summary_request(dict(request))
        decision = self.authority(parsed["subject"], **flags)
        if not decision["allowed"]:
            return decision
        if (parsed["authorizationRef"] != decision["authorization_ref"]
                or parsed["authorizationEpoch"] != decision["authorization_epoch"]
                or (parsed["subject"]["kind"] == "task" and parsed["revision"] != decision["revision"])):
            return {"allowed": False, "reason": "work_scope_authorization_mismatch"}
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with ExitStack() as stack:
                stores = [stack.enter_context(ImportantLogStore.open(self.catalog.directory, pointer["active"], readonly=True))]
                stores += [stack.enter_context(self.catalog._archive_source(fd, pointer, identity))
                           for identity in pointer["archives"]]
                return authorize_summary_sources(parsed, decision, [store._connection for store in stores])

    def summary_entries_page(self, cursor: dict[str, Any] | None, *, limit: int = 32) -> dict[str, Any]:
        """Append-order references for the scheduler; no persisted body copies."""
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("summary discovery page must be 1..32")
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            identities = [*pointer["archives"], pointer["active"]]
            identity = identities[0] if cursor is None else cursor["db_uuid"]
            if identity not in identities:
                raise ImportantLogStoreError("summary discovery cursor DB unavailable")
            after = 0 if cursor is None else cursor["rowid"]
            if type(after) is not int or after < 0:
                raise ValueError("invalid summary cursor")
            context = (ImportantLogStore.open(self.catalog.directory, identity, readonly=True)
                       if identity == pointer["active"] else self.catalog._archive_source(fd, pointer, identity))
            with context as store:
                if cursor is not None and after:
                    previous = store._connection.execute("SELECT entry_id FROM entries WHERE rowid=?", (after,)).fetchone()
                    if previous is None or previous[0] != cursor.get("entry_id"):
                        after = 0  # prune may recycle SQLite rowids; reindex only references
                rows = store._connection.execute("SELECT rowid,entry_id,kind,subject_kind,subject_id,sources_json,source_gaps_json,recorded_at FROM entries WHERE rowid>? ORDER BY rowid LIMIT ?",
                                                 (after, limit)).fetchall()
                entries = [{"cursor": row["rowid"], "entry": {"entryId": row["entry_id"],
                    "kind": row["kind"], "subject": {"schemaVersion": 1, "kind": row["subject_kind"], "id": row["subject_id"]},
                    "sources": json.loads(row["sources_json"]), "sourceGaps": json.loads(row["source_gaps_json"]),
                    "recordedAt": row["recorded_at"]}} for row in rows]
            next_cursor = {"db_uuid": identity, "rowid": rows[-1][0] if rows else after}
            if rows:
                next_cursor["entry_id"] = rows[-1]["entry_id"]
            elif cursor is not None and after:
                next_cursor["entry_id"] = cursor["entry_id"]
            if not rows and identity != pointer["active"]:
                next_cursor = {"db_uuid": identities[identities.index(identity) + 1], "rowid": 0}
            return {"entries": entries, "cursor": next_cursor}

    def summary_entry(self, entry_id: str) -> dict[str, Any] | None:
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with ImportantLogStore.open(self.catalog.directory, pointer["active"], readonly=True) as store:
                entry = store.get_entry(entry_id)
                if entry is not None:
                    return entry
            for identity in reversed(pointer["archives"]):
                with self.catalog._archive_source(fd, pointer, identity) as store:
                    entry = store.get_entry(entry_id)
                    if entry is not None:
                        return entry
        return None
