"""보관 종결과 요약 호출 권한의 제한된 영속 포트.

Flow/terminal journal의 확인된 record만 복사한다. 이 모듈은 모델 호출, core 상태 변경,
사용자 대화 수집 또는 새 Task 생성을 하지 않는다.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import UUID, uuid5, NAMESPACE_URL

from workbench.contracts.logs_v1 import (canonical_json, parse_source_range,
                                         parse_subject_ref, parse_summary_request)
from .store import ImportantLogStore, ImportantLogStoreError


_SCHEMA = """
CREATE TABLE IF NOT EXISTS subject_lifecycle_v1 (
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('task','command')),
    subject_id TEXT NOT NULL,
    archival_state TEXT NOT NULL CHECK(archival_state IN ('open','closed')),
    archival_at TEXT, archival_reason TEXT,
    core_status TEXT, core_closed_at TEXT, core_closed_reason TEXT,
    command_end_status TEXT, command_ended_at TEXT,
    last_activity_at TEXT, event_cursor INTEGER, terminal_cursor INTEGER,
    grant_ref TEXT, grant_revision INTEGER, prior_grant_ref TEXT,
    direct_origin_ref TEXT, direct_origin_epoch INTEGER, direct_origin_at TEXT,
    direct_expired INTEGER NOT NULL DEFAULT 0,
    storage_unknown INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(subject_kind,subject_id)
);
CREATE TABLE IF NOT EXISTS subject_events_v1 (
    event_key TEXT PRIMARY KEY, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
    source_id TEXT NOT NULL UNIQUE, entry_id TEXT, event_kind TEXT NOT NULL,
    observed_at TEXT NOT NULL, payload_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subject_commands_v1 (
    command_id TEXT PRIMARY KEY, task_id TEXT, revision INTEGER,
    started_at TEXT NOT NULL, end_status TEXT, ended_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS subject_direct_origin_once_v1
    ON subject_lifecycle_v1(direct_origin_ref) WHERE direct_origin_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS subject_closed_archive_order_v1
    ON subject_lifecycle_v1(archival_state,julianday(archival_at),subject_kind,subject_id);
"""
_CLOSE_CALL_REASONS = frozenset({"done", "report_outcome_unknown", "report_not_delivered"})
_DELIVERY_KINDS = frozenset({"task_spec", "manager_instruction", "worker_instruction", "report", "decision"})


class _StaleFlowEvent(ImportantLogStoreError):
    """A confirmed older event cannot overwrite a newer durable subject cursor."""


def _uuid(value: object) -> str:
    if type(value) is not str:
        raise ValueError("canonical UUID required")
    try:
        if str(UUID(value)) != value:
            raise ValueError("canonical UUID required")
    except ValueError as exc:
        raise ValueError("canonical UUID required") from exc
    return value


def _time(value: object) -> str:
    if type(value) is not str:
        raise ValueError("timestamp required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("UTC timestamp required")
    except (AttributeError, ValueError) as exc:
        raise ValueError("UTC timestamp required") from exc
    # Core HandoffJournal writes +00:00; JSON v1 writes Z. Preserve its precision.
    if value.endswith("+00:00"):
        return value[:-6] + "Z"
    if value.endswith("Z"):
        return value
    raise ValueError("UTC timestamp required")


def _subject(kind: str, identifier: str) -> dict[str, Any]:
    return parse_subject_ref({"schemaVersion": 1, "kind": kind, "id": _uuid(identifier)})


def _event_key(channel: str, record: Mapping[str, Any]) -> tuple[str, str]:
    seq = record.get("seq")
    if type(seq) is not int or seq < 1:
        raise ValueError("durable journal sequence required")
    key = f"{channel}:{seq}"
    return key, str(uuid5(NAMESPACE_URL, "workbench-important-v1:" + key))


class SubjectLifecycle:
    """An explicit existing-DB dependency; no implicit active DB or model authority."""

    def __init__(self, directory: Path, db_uuid: str, *, _catalog_owned: bool = False):
        self.directory, self.db_uuid = Path(directory), _uuid(db_uuid)
        self._catalog_owned = _catalog_owned
        self.last_failure: str | None = None
        with self._open() as store:
            store._connection.executescript(_SCHEMA)

    def _open(self) -> ImportantLogStore:
        return ImportantLogStore.open(self.directory, self.db_uuid,
                                      _catalog_owned=self._catalog_owned)

    @staticmethod
    def _ensure(conn: sqlite3.Connection, subject: Mapping[str, Any]) -> None:
        conn.execute("""INSERT OR IGNORE INTO subject_lifecycle_v1
            (subject_kind,subject_id,archival_state) VALUES(?,?,'open')""",
                     (subject["kind"], subject["id"]))

    @staticmethod
    def _seen(conn: sqlite3.Connection, key: str, subject: Mapping[str, Any], source_id: str,
              kind: str, at: str, payload: bytes, entry_id: str | None = None) -> bool:
        digest = sha256(payload).hexdigest()
        row = conn.execute("SELECT subject_kind,subject_id,event_kind,payload_sha256 FROM subject_events_v1 WHERE event_key=?",
                           (key,)).fetchone()
        if row is not None:
            if (row[0], row[1], row[2], row[3]) != (subject["kind"], subject["id"], kind, digest):
                raise ImportantLogStoreError("journal event identity changed")
            return True
        conn.execute("""INSERT INTO subject_events_v1
            (event_key,subject_kind,subject_id,source_id,entry_id,event_kind,observed_at,payload_sha256)
            VALUES(?,?,?,?,?,?,?,?)""",
                     (key, subject["kind"], subject["id"], source_id, entry_id, kind, at, digest))
        return False

    def _entry(self, store: ImportantLogStore, subject: dict[str, Any], kind: str, source_kind: str,
               source_id: str, payload: bytes, facts: list[str], at: str, *, revision: int | None = None) -> str:
        source: dict[str, Any] = {"schemaVersion": 1, "sourceKind": source_kind, "collectorEpoch": 1}
        if subject["kind"] == "task":
            if type(revision) is not int or revision < 1:
                raise ValueError("Task source revision required")
            source.update(taskId=subject["id"], revision=revision)
        else:
            source["commandId"] = subject["id"]
        if source_kind == "terminal":
            raise ValueError("journal events cannot claim terminal output bytes")
        elif source_kind in ("task_spec", "manager_instruction", "worker_instruction", "report"):
            source["messageId"] = source_id
        else:
            source["eventId"] = source_id
        span = {"schemaVersion": 1, "source": source, "start": 0, "end": len(payload),
                "digest": sha256(payload).hexdigest()}
        body = {"schemaVersion": 1, "facts": facts, "interpretations": [], "unknowns": []}
        entry_id = str(uuid5(NAMESPACE_URL, "workbench-important-entry-v1:" + source_id))
        entry = store.make_entry(subject, kind, [span], body, entry_id=entry_id, recorded_at=at)
        try:
            store.put_entry(entry)
        except sqlite3.IntegrityError:
            if store.get_entry(entry_id) != entry:
                raise ImportantLogStoreError("existing event entry differs")
        return entry_id

    def record_flow(self, record: Mapping[str, Any]) -> bool:
        """Copy a confirmed flow-journal record; false means irrelevant/already copied."""
        kind = record.get("type")
        if kind not in ("delegated", "task_closed", "task"):
            return False
        if kind == "task":
            data = record.get("task")
            if type(data) is not dict or data.get("status") not in ("finished", "blocked", "held"):
                return False
            task_id, revision = data.get("task_id"), data.get("revision")
            status = data["status"]
        else:
            task_id, revision = record.get("task_id"), record.get("revision")
            status = None
        subject = _subject("task", task_id)
        key, source_id = _event_key("flow", record)
        at = _time(record.get("at"))
        payload = canonical_json(dict(record))
        stale_event = False
        with self._open() as store, store.transaction():
            conn = store._connection
            self._ensure(conn, subject)
            if conn.execute("SELECT 1 FROM subject_events_v1 WHERE event_key=?", (key,)).fetchone():
                self._seen(conn, key, subject, source_id, str(kind), at, payload)
                return False
            state = dict(conn.execute("""SELECT * FROM subject_lifecycle_v1
                WHERE subject_kind='task' AND subject_id=?""", (task_id,)).fetchone())
            if state["event_cursor"] is not None and record["seq"] <= state["event_cursor"]:
                # 과거의 미복사 사실은 최신 core 상태/권한을 되감지 않는다.
                conn.execute("""UPDATE subject_lifecycle_v1 SET storage_unknown=1
                    WHERE subject_kind='task' AND subject_id=?""", (task_id,))
                stale_event = True
            elif kind == "task_closed":
                reason = record.get("reason")
                if type(reason) is not str or not reason or len(reason) > 128:
                    raise ValueError("closed reason required")
                revision = state.get("grant_revision")
                facts = [f"task_closed reason={reason}", f"closed_at={at}",
                         f"event_cursor={record['seq']}", f"prior_grant_ref={state.get('grant_ref') or 'unknown'}"]
                entry_id = (self._entry(store, subject, "closure", "event", source_id, payload, facts, at,
                                        revision=revision) if revision is not None else None)
                conn.execute("""UPDATE subject_lifecycle_v1 SET archival_state='closed',archival_at=?,
                    archival_reason=?,core_status='closed',core_closed_at=?,core_closed_reason=?,
                    prior_grant_ref=grant_ref,event_cursor=MAX(COALESCE(event_cursor,0),?),
                    storage_unknown=MAX(storage_unknown,?)
                    WHERE subject_kind='task' AND subject_id=?""",
                             (at, reason, at, reason, record["seq"], int(revision is None), task_id))
            elif kind == "delegated":
                if type(revision) is not int or revision < 1:
                    raise ValueError("delegation revision required")
                if state["core_status"] == "closed":
                    raise ImportantLogStoreError("closed core Task cannot acquire a new grant")
                scope, proceed = _uuid(record.get("scope_decision_id")), _uuid(record.get("proceed_decision_id"))
                entry_id = self._entry(store, subject, "event", "decision", source_id, payload,
                                       [f"scope_decision_id={scope}", f"proceed_decision_id={proceed}"], at,
                                       revision=revision)
                conn.execute("""UPDATE subject_lifecycle_v1 SET archival_state='open',archival_at=NULL,
                    archival_reason=NULL,core_status='dispatched',grant_ref=?,grant_revision=?,last_activity_at=?,
                    event_cursor=MAX(COALESCE(event_cursor,0),?),
                    storage_unknown=storage_unknown WHERE subject_kind='task' AND subject_id=?""",
                             (proceed, revision, at, record["seq"], task_id))
            else:
                if type(revision) is not int or revision < 1:
                    raise ValueError("Task status revision required")
                entry_id = None
                conn.execute("""UPDATE subject_lifecycle_v1 SET core_status=?,last_activity_at=?,
                    event_cursor=MAX(COALESCE(event_cursor,0),?) WHERE subject_kind='task' AND subject_id=?""",
                             (status, at, record["seq"], task_id))
            if not stale_event:
                self._seen(conn, key, subject, source_id, str(kind), at, payload, entry_id)
        if stale_event:
            raise _StaleFlowEvent("unseen flow event precedes durable subject cursor")
        return True

    def record_terminal(self, record: Mapping[str, Any], *, revision: int | None = None) -> bool:
        """Only actual terminal_started/terminal_ended, never a mere run_ended notice."""
        kind = record.get("type")
        if kind not in ("terminal_started", "terminal_ended"):
            return False
        command_id = _uuid(record.get("command_id"))
        key, source_id = _event_key("terminal", record)
        at = _time(record.get("started_at") if kind == "terminal_started" else record.get("ended_at"))
        payload = canonical_json(dict(record))
        with self._open() as store, store.transaction():
            conn = store._connection
            if kind == "terminal_started":
                task_id = record.get("task_id")
                if task_id is not None:
                    _uuid(task_id)
                    if type(revision) is not int or revision < 1:
                        raise ValueError("Task command revision required")
                subject = _subject("task" if task_id else "command", task_id or command_id)
                if task_id:
                    current = conn.execute("""SELECT core_status FROM subject_lifecycle_v1
                        WHERE subject_kind='task' AND subject_id=?""", (task_id,)).fetchone()
                    if current is not None and current[0] == "closed":
                        raise ImportantLogStoreError("closed core Task cannot start a command")
            else:
                row = conn.execute("SELECT task_id,revision FROM subject_commands_v1 WHERE command_id=?",
                                   (command_id,)).fetchone()
                if row is None:
                    raise ImportantLogStoreError("terminal end without durable start is unknown")
                task_id, revision = row[0], row[1]
                subject = _subject("task" if task_id else "command", task_id or command_id)
            self._ensure(conn, subject)
            if conn.execute("SELECT 1 FROM subject_events_v1 WHERE event_key=?", (key,)).fetchone():
                self._seen(conn, key, subject, source_id, str(kind), at, payload)
                return False
            if kind == "terminal_started":
                facts = [f"command_started={command_id}"]
                entry_kind = "start"
                entry_id = self._entry(store, subject, entry_kind, "event", source_id, payload, facts, at,
                                       revision=revision)
                conn.execute("""INSERT INTO subject_commands_v1(command_id,task_id,revision,started_at)
                    VALUES(?,?,?,?)""", (command_id, task_id, revision, at))
                conn.execute("""UPDATE subject_lifecycle_v1 SET archival_state='open',archival_at=NULL,
                    archival_reason=NULL,last_activity_at=?,terminal_cursor=? WHERE subject_kind=? AND subject_id=?""",
                             (at, record["seq"], subject["kind"], subject["id"]))
            else:
                status = record.get("status")
                if status not in ("exited", "unknown"):
                    raise ValueError("actual terminal end status required")
                facts = [f"command_end_status={status}", f"command_id={command_id}"]
                entry_kind = "closure" if task_id is None and status == "exited" else "event"
                entry_id = self._entry(store, subject, entry_kind, "event", source_id, payload, facts, at,
                                       revision=revision)
                conn.execute("UPDATE subject_commands_v1 SET end_status=?,ended_at=? WHERE command_id=?",
                             (status, at, command_id))
                if task_id is None:
                    conn.execute("""UPDATE subject_lifecycle_v1 SET command_end_status=?,command_ended_at=?,
                        archival_state=?,archival_at=?,archival_reason=?,terminal_cursor=?
                        WHERE subject_kind='command' AND subject_id=?""",
                                 (status, at, "closed" if status == "exited" else "open",
                                  at if status == "exited" else None,
                                  "actual_end" if status == "exited" else None, record["seq"], command_id))
            # The journal record is a structured event; actual terminal byte ranges are DVL-04's source.
            self._seen(conn, key, subject, source_id, str(kind), at, payload, entry_id)
        return True

    def record_delivery(self, subject: Mapping[str, Any], *, message_id: str, revision: int,
                        kind: str, content: str, delivered_at: str) -> bool:
        """Caller supplies a confirmed delivered message ID; no generic chat ingestion."""
        item = parse_subject_ref(subject)
        if item["kind"] != "task" or kind not in _DELIVERY_KINDS or type(revision) is not int or revision < 1:
            raise ValueError("bounded Task delivery required")
        message_id, at = _uuid(message_id), _time(delivered_at)
        if type(content) is not str or not content or len(content.encode("utf-8")) > 250_000:
            raise ValueError("delivered content must be bounded")
        payload = content.encode("utf-8")
        key = "message:" + message_id
        event_kind = f"{kind}@{revision}"
        with self._open() as store, store.transaction():
            conn = store._connection
            self._ensure(conn, item)
            if conn.execute("SELECT 1 FROM subject_events_v1 WHERE event_key=?", (key,)).fetchone():
                self._seen(conn, key, item, message_id, event_kind, at, payload)
                return False
            entry_id = self._entry(store, item, "event", kind, message_id, payload, [content], at,
                                   revision=revision)
            conn.execute("""UPDATE subject_lifecycle_v1 SET archival_state='open',archival_at=NULL,
                archival_reason=NULL,last_activity_at=? WHERE subject_kind='task' AND subject_id=?
                AND COALESCE(core_status,'')!='closed'""",
                         (at, item["id"]))
            self._seen(conn, key, item, message_id, event_kind, at, payload, entry_id)
        return True

    def record_terminal_range(self, subject: Mapping[str, Any], source_record_id: str,
                              span: Mapping[str, Any], *, observed_at: str) -> bool:
        """DVL-04가 확인한 byte range만 연결한다. 출력 bytes를 재구성하지 않는다."""
        item = parse_subject_ref(subject)
        source_record_id, at = _uuid(source_record_id), _time(observed_at)
        source_range = parse_source_range(span)
        source = source_range["source"]
        if source["sourceKind"] != "terminal":
            raise ValueError("observed terminal SourceRange required")
        if item["kind"] == "task":
            if source.get("taskId") != item["id"]:
                raise ValueError("terminal range must identify its Task")
        elif source.get("commandId") != item["id"] or "taskId" in source:
            raise ValueError("Taskless range must identify its command only")
        payload = canonical_json(source_range)
        key = "terminal_range:" + source_record_id
        entry_id = str(uuid5(NAMESPACE_URL, "workbench-important-entry-v1:" + key))
        body = {"schemaVersion": 1,
                "facts": [f"terminal range [{source_range['start']},{source_range['end']}) observed"],
                "interpretations": [], "unknowns": []}
        with self._open() as store, store.transaction():
            conn = store._connection
            if "commandId" in source:
                command = conn.execute("SELECT task_id,revision FROM subject_commands_v1 WHERE command_id=?",
                                       (source["commandId"],)).fetchone()
                expected = (item["id"], source.get("revision")) if item["kind"] == "task" else (None, None)
                if command is None or tuple(command) != expected:
                    raise ValueError("terminal range lacks a matching confirmed command start")
            self._ensure(conn, item)
            if conn.execute("SELECT 1 FROM subject_events_v1 WHERE event_key=?", (key,)).fetchone():
                self._seen(conn, key, item, source_record_id, "terminal_range", at, payload)
                return False
            entry = store.make_entry(item, "event", [source_range], body, entry_id=entry_id, recorded_at=at)
            try:
                store.put_entry(entry)
            except sqlite3.IntegrityError:
                if store.get_entry(entry_id) != entry:
                    raise ImportantLogStoreError("existing terminal range entry differs")
            self._seen(conn, key, item, source_record_id, "terminal_range", at, payload, entry_id)
        return True

    def archive_close(self, subject: Mapping[str, Any], *, at: str, user_confirmed: bool) -> None:
        """DVL-07 CLI-only port; does not touch core status or grant."""
        item = parse_subject_ref(subject)
        if user_confirmed is not True:
            raise ValueError("explicit user archival-close confirmation required")
        at = _time(at)
        with self._open() as store:
            self._ensure(store._connection, item)
            store._connection.execute("""UPDATE subject_lifecycle_v1 SET archival_state='closed',
                archival_at=?,archival_reason='user_archival_close' WHERE subject_kind=? AND subject_id=?""",
                                      (at, item["kind"], item["id"]))

    def record_direct_origin(self, command_id: str, *, event_id: str, epoch: int, observed_at: str,
                             verified_direct_instruction: bool) -> None:
        """Fixture-injectable origin port; caller provenance is not inferred from user role."""
        command_id, event_id, observed_at = _uuid(command_id), _uuid(event_id), _time(observed_at)
        if verified_direct_instruction is not True or type(epoch) is not int or epoch < 1:
            raise ValueError("verified direct instruction origin required")
        with self._open() as store:
            conn = store._connection
            started = conn.execute("SELECT started_at FROM subject_commands_v1 WHERE command_id=? AND task_id IS NULL",
                                   (command_id,)).fetchone()
            if started is None:
                raise ValueError("origin can only bind one existing Taskless command")
            if datetime.fromisoformat(observed_at.replace("Z", "+00:00")) > datetime.fromisoformat(started[0].replace("Z", "+00:00")):
                raise ValueError("direct instruction must precede command start")
            row = conn.execute("""SELECT direct_origin_ref,direct_origin_epoch,direct_origin_at FROM subject_lifecycle_v1
                WHERE subject_kind='command' AND subject_id=?""", (command_id,)).fetchone()
            if row[0] not in (None, event_id) or (row[0] is not None and (row[1], row[2]) != (epoch, observed_at)):
                raise ValueError("direct origin cannot be replaced")
            conn.execute("""UPDATE subject_lifecycle_v1 SET direct_origin_ref=?,direct_origin_epoch=?,direct_origin_at=?
                WHERE subject_kind='command' AND subject_id=?""", (event_id, epoch, observed_at, command_id))

    def mark_command_end_summary_processed(self, command_id: str) -> None:
        command_id = _uuid(command_id)
        with self._open() as store:
            store._connection.execute("""UPDATE subject_lifecycle_v1 SET direct_expired=1
                WHERE subject_kind='command' AND subject_id=? AND command_end_status='exited'""", (command_id,))

    def state(self, subject: Mapping[str, Any]) -> dict[str, Any]:
        item = parse_subject_ref(subject)
        with self._open() as store:
            row = store._connection.execute("""SELECT * FROM subject_lifecycle_v1
                WHERE subject_kind=? AND subject_id=?""", (item["kind"], item["id"])).fetchone()
            return {} if row is None else dict(row)

    def authority(self, subject: Mapping[str, Any], *, active: bool, paused: bool,
                  revoked: bool, has_new_input: bool, closing_event: bool = False) -> dict[str, Any]:
        """Bounded preflight fact only; sending/recheck belongs to DVL-09/10."""
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
        # A confirmed core terminal start is the command work-scope identity. An
        # optional historical direct-origin record is retained but is not a grant.
        with self._open() as store:
            starts = store._connection.execute("""SELECT e.event_key,e.source_id
                FROM subject_events_v1 e JOIN subject_commands_v1 c
                ON c.command_id=e.subject_id AND c.task_id IS NULL
                WHERE e.subject_kind='command' AND e.subject_id=?
                  AND e.event_kind='terminal_started'""", (item["id"],)).fetchall()
        if len(starts) != 1:
            return {"allowed": False, "reason": "no_confirmed_command_start"}
        key, ref = starts[0]
        try:
            epoch = int(key.removeprefix("terminal:"))
        except ValueError:
            return {"allowed": False, "reason": "command_start_epoch_unknown"}
        if key != f"terminal:{epoch}" or epoch < 1:
            return {"allowed": False, "reason": "command_start_epoch_unknown"}
        return {"allowed": True, "authorization_ref": ref,
                "authorization_epoch": epoch, "command_id": item["id"]}

    def authorize_summary_request(self, request: Mapping[str, Any], *, active: bool,
                                  paused: bool, revoked: bool, has_new_input: bool,
                                  closing_event: bool = False) -> dict[str, Any]:
        """Preflight one canonical request against the confirmed work and source ranges.

        This does not reserve or send a model turn. DVL-09/10 own the dispatch
        recheck and durable no-replay journal.
        """
        parsed = parse_summary_request(dict(request))
        subject = parsed["subject"]
        decision = self.authority(subject, active=active, paused=paused,
                                  revoked=revoked, has_new_input=has_new_input,
                                  closing_event=closing_event)
        if not decision["allowed"]:
            return decision
        if (parsed["authorizationRef"] != decision["authorization_ref"]
                or parsed["authorizationEpoch"] != decision["authorization_epoch"]
                or (subject["kind"] == "task" and parsed["revision"] != decision["revision"])):
            return {"allowed": False, "reason": "work_scope_authorization_mismatch"}
        with self._open() as store:
            return authorize_summary_sources(parsed, decision, [store._connection])

    def reconcile_flow(self, records: list[dict[str, Any]]) -> int:
        """Restart copies only already-fsynced journal facts; never dispatches or replays."""
        copied = 0
        stale: _StaleFlowEvent | None = None
        # 입력 목록 순서와 무관하게 journal seq 순서를 우선한다. 이미 최신 사실이
        # 저장된 경우 오래된 미복사 위임은 재적용하지 않고 unknown을 surface한다.
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

    def reconcile_terminal(self, records: list[dict[str, Any]], *, task_revisions: Mapping[str, int]) -> int:
        copied = 0
        for record in records:
            if record.get("type") not in ("terminal_started", "terminal_ended"):
                continue
            revision = task_revisions.get(record.get("task_id")) if record.get("type") == "terminal_started" else None
            if self.record_terminal(record, revision=revision):
                copied += 1
        return copied

def authorize_summary_sources(parsed: Mapping[str, Any], decision: Mapping[str, Any], connections: Any) -> dict[str, Any]:
    """Shared confirmed-source check; connections are held under the owner's lease."""
    subject = parsed["subject"]
    sources = {canonical_json(span) for span in parsed["sources"]}
    gaps = {canonical_json(gap) for gap in parsed["sourceGaps"]}
    prior = parsed["priorCumulativeEntryId"]
    prior_found = prior is None
    for conn in connections:
        if prior is not None and conn.execute("""SELECT 1 FROM entries
            WHERE entry_id=? AND subject_kind=? AND subject_id=? AND kind='cumulative'""",
                (prior, subject["kind"], subject["id"])).fetchone() is not None:
            prior_found = True
        for row in conn.execute("""SELECT sources_json,source_gaps_json FROM entries
            WHERE subject_kind=? AND subject_id=?""", (subject["kind"], subject["id"])):
            sources.difference_update(canonical_json(span) for span in json.loads(row[0]))
            gaps.difference_update(canonical_json(gap) for gap in json.loads(row[1]))
            if not sources and not gaps:
                break
    if not prior_found:
        return {"allowed": False, "reason": "prior_work_context_unrelated"}
    if sources or gaps:
        return {"allowed": False, "reason": "unrelated_or_unobserved_source_range"}
    return {**decision, "request_id": parsed["requestId"], "input_key": parsed["inputKey"],
            "source_range_count": len(parsed["sources"]), "source_gap_count": len(parsed["sourceGaps"])}
