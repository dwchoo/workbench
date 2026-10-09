"""Transactional, append-only task metadata backed by SQLite."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from threading import local
from typing import Any
import uuid

from workbench.storage.metadata_sqlite.schema import (
    APPEND_ONLY_TABLES,
    MIGRATION_1_TO_2,
    MIGRATION_2_TO_3,
    SCHEMA,
    SCHEMA_VERSION,
)
from workbench.contracts.logs_v1 import (
    ObservationBoundary, ObservationCoverage, SummaryRequest, SummaryResult,
    canonical_json, parse_observation_boundary, parse_observation_coverage, parse_source_ref,
    parse_source_range,
    parse_subject_ref, parse_summary_request, parse_summary_result,
)


class RepositoryClosedError(RuntimeError):
    """Raised when the repository is used after close()."""


class InvalidRevisionError(ValueError):
    """Raised when a Task or TaskSpec revision does not exist."""


class AuthorizationError(RuntimeError):
    """Raised when persisted approval and proceed are insufficient."""


class ActiveRunError(RuntimeError):
    """Raised when a Task already has a current run."""


class InvalidTransitionError(ValueError):
    """Raised when a requested metadata transition violates its contract."""


_run_guard_local = local()


@contextmanager
def persisted_run_guard(path: str):
    """Serialize active-run retirement with automatic dispatch opening.

    SQLite's write lock cannot span transport because the mailbox persists its
    attempt before sending. This inode lock also works across processes.
    """
    if path == ":memory:":
        yield
        return
    key = os.path.realpath(path)
    held = getattr(_run_guard_local, "held", None)
    if held is None:
        held = _run_guard_local.held = {}
    if key in held:
        held[key][1] += 1
        try:
            yield
        finally:
            held[key][1] -= 1
        return
    descriptor = os.open(key, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        held[key] = [descriptor, 1]
        try:
            yield
        finally:
            del held[key]
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identifier(value: str | None, *, name: str) -> str:
    if value is None:
        return str(uuid.uuid4())
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InvalidTransitionError(f"{name} must be a non-empty string")
    return value


def _log_uuid(value: str, *, name: str) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError(value)
    except ValueError as exc:
        raise InvalidTransitionError(f"{name} must be a canonical UUID") from exc
    return value


def _sha256_hex(value: str, *, name: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(ch not in "0123456789abcdef" for ch in value)):
        raise InvalidTransitionError(f"{name} must be lowercase SHA-256 hex")
    return value


def _summary_source_overlap_key(source: Mapping[str, Any]) -> str:
    # 물리 원본 ID/offset은 revision, epoch, skill, gap 사유와 무관하게 재사용 금지.
    stable = {"sourceKind": source["sourceKind"]}
    if source["sourceKind"] == "terminal":
        key = "runId" if "runId" in source else "commandId"
    else:
        key = "messageId" if "messageId" in source else "eventId"
    stable[key] = source[key]
    return sha256(canonical_json(stable)).hexdigest()


def _positive_integer(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidTransitionError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidTransitionError(f"{name} must be a non-negative integer")
    return value


def _json(value: Mapping[str, Any], *, name: str) -> str:
    if not isinstance(value, Mapping):
        raise InvalidTransitionError(f"{name} must be a mapping")
    try:
        return json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise InvalidTransitionError(f"{name} must be JSON serializable") from exc


def _decoded(value: str) -> Any:
    return json.loads(value)


def _dict(row: sqlite3.Row | None, fields: tuple[str, ...]) -> dict[str, Any] | None:
    if row is None:
        return None
    return {field: row[field] for field in fields}


class TaskRepository:
    """A single-connection SQLite repository with explicit transactions.

    History tables reject UPDATE and DELETE at the database level. Mutable state is
    limited to the active-run pointer; durable run and decision facts are append-only.
    SQLite errors propagate to the caller, and a failed transaction is rolled back.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        # SQLite's REPLACE conflict algorithm deletes the conflicting row before
        # inserting its replacement. Delete triggers only run for that implicit
        # delete when recursive triggers are enabled, so append-only guards would
        # otherwise be bypassable with INSERT OR REPLACE.
        self._connection.execute("PRAGMA recursive_triggers = ON")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA synchronous = FULL")
        user_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if user_version not in (0, 1, 2, SCHEMA_VERSION):
            self._connection.close()
            self._closed = True
            raise InvalidTransitionError(f"unsupported metadata schema version: {user_version}")
        try:
            if user_version == 1:
                self._connection.executescript(MIGRATION_1_TO_2)
            if user_version in (1, 2):
                self._connection.executescript(MIGRATION_2_TO_3)
            self._connection.executescript(SCHEMA)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            self._connection.close()
            self._closed = True
            raise
        try:
            triggers = "\n".join(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable_update
                    BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS {table}_immutable_delete
                    BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"""
                for table in APPEND_ONLY_TABLES
            )
            self._connection.executescript("BEGIN IMMEDIATE;\n" + triggers + "\nCOMMIT;")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            self._connection.close()
            self._closed = True
            raise
        self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._closed = False
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO session_settings(revision, settings_json, created_at) VALUES (1, '{}', ?)",
                (_now(),),
            )

    def __enter__(self) -> TaskRepository:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RepositoryClosedError("TaskRepository is closed")

    @contextmanager
    def _transaction(self):
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def _require_spec(self, connection: sqlite3.Connection, task_id: str, revision: int) -> None:
        if not isinstance(task_id, str) or not task_id.strip() or task_id != task_id.strip():
            raise InvalidRevisionError("task_id must be a non-empty string")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise InvalidRevisionError("revision must be a positive integer")
        row = connection.execute(
            "SELECT 1 FROM task_specs WHERE task_id = ? AND revision = ?",
            (task_id, revision),
        ).fetchone()
        if row is None:
            raise InvalidRevisionError(f"unknown TaskSpec revision: {task_id}@{revision}")

    def _require_run(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise InvalidTransitionError(f"unknown run: {run_id}")
        return row

    @staticmethod
    def _settings_revision(connection: sqlite3.Connection) -> int:
        return int(connection.execute("SELECT MAX(revision) FROM session_settings").fetchone()[0])

    @staticmethod
    def _insert_decision(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        revision: int,
        kind: str,
        settings_revision: int,
        retry_count_used: int,
        details_json: str,
        run_id: str | None = None,
    ) -> str:
        decision_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO task_decisions(
                decision_id, task_id, revision, run_id, settings_revision,
                retry_count_used, kind, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id,
                task_id,
                revision,
                run_id,
                settings_revision,
                retry_count_used,
                kind,
                details_json,
                _now(),
            ),
        )
        return decision_id

    @staticmethod
    def _latest_retry_count(connection: sqlite3.Connection, run: sqlite3.Row) -> int:
        row = connection.execute(
            "SELECT MAX(retry_count_used) FROM task_decisions WHERE run_id = ?",
            (run["run_id"],),
        ).fetchone()
        return max(int(run["retry_count_used"]), int(row[0] or 0))

    def create_task(self, spec: Mapping[str, Any], *, task_id: str | None = None) -> str:
        task_id = _identifier(task_id, name="task_id")
        spec_json = _json(spec, name="TaskSpec")
        now = _now()
        with self._transaction() as connection:
            connection.execute("INSERT INTO tasks(task_id, created_at) VALUES (?, ?)", (task_id, now))
            connection.execute(
                "INSERT INTO task_specs(task_id, revision, spec_json, created_at) VALUES (?, 1, ?, ?)",
                (task_id, spec_json, now),
            )
        return task_id

    def revise_task(self, task_id: str, spec: Mapping[str, Any]) -> int:
        spec_json = _json(spec, name="TaskSpec")
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone() is None:
                raise InvalidRevisionError(f"unknown Task: {task_id}")
            revision = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 FROM task_specs WHERE task_id = ?",
                    (task_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO task_specs(task_id, revision, spec_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, revision, spec_json, _now()),
            )
        return revision

    def get_task_spec(self, task_id: str, revision: int | None = None) -> dict[str, Any]:
        self._ensure_open()
        if revision is not None:
            try:
                _positive_integer(revision, name="revision")
            except InvalidTransitionError as exc:
                raise InvalidRevisionError(str(exc)) from exc
        if revision is None:
            row = self._connection.execute(
                "SELECT * FROM task_specs WHERE task_id = ? ORDER BY revision DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT * FROM task_specs WHERE task_id = ? AND revision = ?",
                (task_id, revision),
            ).fetchone()
        if row is None:
            raise InvalidRevisionError(f"unknown TaskSpec revision: {task_id}@{revision}")
        return {
            "task_id": row["task_id"],
            "revision": row["revision"],
            "spec": _decoded(row["spec_json"]),
            "created_at": row["created_at"],
        }

    def approve_scope(
        self,
        task_id: str,
        revision: int,
        scope: Mapping[str, Any],
        *,
        retry_count_used: int = 0,
    ) -> str:
        scope_json = _json(scope, name="delegation scope")
        if not scope:
            raise InvalidTransitionError("delegation scope approval must not be empty")
        retry_count_used = _nonnegative_integer(retry_count_used, name="retry_count_used")
        with self._transaction() as connection:
            self._require_spec(connection, task_id, revision)
            if connection.execute(
                "SELECT 1 FROM invalidated_revisions WHERE task_id = ? AND revision = ?",
                (task_id, revision),
            ).fetchone():
                raise AuthorizationError("an invalidated TaskSpec revision cannot be re-approved")
            return self._insert_decision(
                connection,
                task_id=task_id,
                revision=revision,
                kind="scope_approved",
                settings_revision=self._settings_revision(connection),
                retry_count_used=retry_count_used,
                details_json=scope_json,
            )

    def proceed(
        self,
        task_id: str,
        revision: int,
        instruction: str | Mapping[str, Any],
        *,
        retry_count_used: int = 0,
    ) -> str:
        if isinstance(instruction, str):
            if not instruction.strip():
                raise InvalidTransitionError("proceed instruction must not be empty")
            details = {"instruction": instruction}
        elif isinstance(instruction, Mapping):
            details = dict(instruction)
            if not details:
                raise InvalidTransitionError("proceed instruction must not be empty")
        else:
            raise InvalidTransitionError("proceed instruction must be text or a mapping")
        details_json = _json(details, name="proceed instruction")
        retry_count_used = _nonnegative_integer(retry_count_used, name="retry_count_used")
        with self._transaction() as connection:
            self._require_spec(connection, task_id, revision)
            if connection.execute(
                "SELECT 1 FROM invalidated_revisions WHERE task_id = ? AND revision = ?",
                (task_id, revision),
            ).fetchone():
                raise AuthorizationError("an invalidated TaskSpec revision cannot authorize a run")
            approved = connection.execute(
                """SELECT 1 FROM task_decisions
                   WHERE task_id = ? AND revision = ? AND kind = 'scope_approved'
                   LIMIT 1""",
                (task_id, revision),
            ).fetchone()
            if approved is None:
                raise AuthorizationError("delegation scope must be persisted before proceed")
            return self._insert_decision(
                connection,
                task_id=task_id,
                revision=revision,
                kind="proceed",
                settings_revision=self._settings_revision(connection),
                retry_count_used=retry_count_used,
                details_json=details_json,
            )

    def start_run(
        self,
        task_id: str,
        revision: int,
        *,
        inputs: Mapping[str, Any] | None = None,
    ) -> str:
        inputs_json = _json({} if inputs is None else inputs, name="run inputs")
        with self._transaction() as connection:
            self._require_spec(connection, task_id, revision)
            if connection.execute(
                "SELECT 1 FROM invalidated_revisions WHERE task_id = ? AND revision = ?",
                (task_id, revision),
            ).fetchone():
                raise AuthorizationError("an invalidated TaskSpec revision cannot start a run")
            if connection.execute("SELECT 1 FROM active_runs WHERE task_id = ?", (task_id,)).fetchone():
                raise ActiveRunError(f"Task already has a current run: {task_id}")
            approved = connection.execute(
                """SELECT 1 FROM task_decisions
                   WHERE task_id = ? AND revision = ? AND kind = 'scope_approved'
                   LIMIT 1""",
                (task_id, revision),
            ).fetchone()
            if approved is None:
                raise AuthorizationError("persisted delegation scope approval is required")
            proceed = connection.execute(
                """SELECT decision_id, retry_count_used FROM task_decisions d
                   WHERE d.task_id = ? AND d.revision = ? AND d.kind = 'proceed'
                     AND NOT EXISTS (
                         SELECT 1 FROM authorization_consumption c
                         WHERE c.decision_id = d.decision_id
                     )
                   ORDER BY d.rowid DESC LIMIT 1""",
                (task_id, revision),
            ).fetchone()
            if proceed is None:
                raise AuthorizationError("a persisted, unused proceed instruction is required")

            settings_revision = self._settings_revision(connection)
            retry_count_used = int(proceed["retry_count_used"])
            run_id = str(uuid.uuid4())
            now = _now()
            connection.execute(
                """INSERT INTO runs(
                    run_id, task_id, revision, settings_revision,
                    retry_count_used, inputs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, task_id, revision, settings_revision, retry_count_used, inputs_json, now),
            )
            connection.execute(
                """INSERT INTO authorization_consumption(
                    decision_id, task_id, revision, run_id, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (proceed["decision_id"], task_id, revision, run_id, now),
            )
            connection.execute(
                """INSERT INTO run_events(
                    event_id, run_id, kind, settings_revision,
                    retry_count_used, details_json, created_at
                ) VALUES (?, ?, 'started', ?, ?, '{}', ?)""",
                (str(uuid.uuid4()), run_id, settings_revision, retry_count_used, now),
            )
            connection.execute(
                "INSERT INTO active_runs(task_id, run_id) VALUES (?, ?)", (task_id, run_id)
            )
            return run_id

    def get_run(self, run_id: str) -> dict[str, Any]:
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise InvalidTransitionError(f"unknown run: {run_id}")
        return {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "revision": row["revision"],
            "settings_revision": row["settings_revision"],
            "retry_count_used": row["retry_count_used"],
            "inputs": _decoded(row["inputs_json"]),
            "created_at": row["created_at"],
        }

    def get_current_run(self, task_id: str) -> dict[str, Any] | None:
        self._ensure_open()
        row = self._connection.execute(
            """SELECT r.* FROM active_runs a JOIN runs r ON r.run_id = a.run_id
               WHERE a.task_id = ?""",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "revision": row["revision"],
            "settings_revision": row["settings_revision"],
            "retry_count_used": row["retry_count_used"],
            "inputs": _decoded(row["inputs_json"]),
            "created_at": row["created_at"],
        }

    def get_run_history(self, run_id: str) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "settings_revision": row["settings_revision"],
                "retry_count_used": row["retry_count_used"],
                "details": _decoded(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _terminal_run(self, run_id: str, kind: str, details: Mapping[str, Any]) -> None:
        details_json = _json(details, name="run event details")
        with persisted_run_guard(self.path), self._transaction() as connection:
            run = self._require_run(connection, run_id)
            active = connection.execute(
                "SELECT 1 FROM active_runs WHERE task_id = ? AND run_id = ?",
                (run["task_id"], run_id),
            ).fetchone()
            if active is None:
                raise InvalidTransitionError(f"run is not current: {run_id}")
            retry_count_used = self._latest_retry_count(connection, run)
            settings_revision = self._settings_revision(connection)
            decision_kind = "cancelled" if kind == "cancelled" else None
            decision_id = None
            if decision_kind:
                decision_id = self._insert_decision(
                    connection,
                    task_id=run["task_id"],
                    revision=run["revision"],
                    run_id=run_id,
                    kind=decision_kind,
                    settings_revision=settings_revision,
                    retry_count_used=retry_count_used,
                    details_json=details_json,
                )
            connection.execute(
                """INSERT INTO run_events(
                    event_id, run_id, kind, settings_revision,
                    retry_count_used, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()), run_id, kind, settings_revision,
                    retry_count_used, details_json, _now(),
                ),
            )
            if decision_id is not None:
                connection.execute(
                    """INSERT INTO invalidated_revisions(
                        task_id, revision, decision_id, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (run["task_id"], run["revision"], decision_id, str(details.get("reason", kind)), _now()),
                )
            connection.execute("DELETE FROM active_runs WHERE task_id = ?", (run["task_id"],))

    def cancel_run(self, run_id: str, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidTransitionError("cancellation reason must not be empty")
        self._terminal_run(run_id, "cancelled", {"reason": reason})

    def complete_run(self, run_id: str, result: Mapping[str, Any] | None = None) -> None:
        self._terminal_run(run_id, "completed", {} if result is None else result)

    def fail_run(self, run_id: str, failure: Mapping[str, Any] | None = None) -> None:
        self._terminal_run(run_id, "failed", {} if failure is None else failure)

    def revoke_authority(self, task_id: str, revision: int, reason: str) -> str:
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidTransitionError("revocation reason must not be empty")
        details_json = _json({"reason": reason}, name="revocation details")
        with persisted_run_guard(self.path), self._transaction() as connection:
            self._require_spec(connection, task_id, revision)
            if connection.execute(
                "SELECT 1 FROM invalidated_revisions WHERE task_id = ? AND revision = ?",
                (task_id, revision),
            ).fetchone():
                raise AuthorizationError("TaskSpec revision authority is already invalidated")
            active = connection.execute(
                """SELECT r.* FROM active_runs a JOIN runs r ON r.run_id = a.run_id
                   WHERE a.task_id = ? AND r.revision = ?""",
                (task_id, revision),
            ).fetchone()
            settings_revision = self._settings_revision(connection)
            retry_count_used = 0 if active is None else self._latest_retry_count(connection, active)
            decision_id = self._insert_decision(
                connection,
                task_id=task_id,
                revision=revision,
                run_id=None if active is None else active["run_id"],
                kind="revoked",
                settings_revision=settings_revision,
                retry_count_used=retry_count_used,
                details_json=details_json,
            )
            connection.execute(
                """INSERT INTO invalidated_revisions(
                    task_id, revision, decision_id, reason, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (task_id, revision, decision_id, reason, _now()),
            )
            if active is not None:
                connection.execute(
                    """INSERT INTO run_events(
                        event_id, run_id, kind, settings_revision,
                        retry_count_used, details_json, created_at
                    ) VALUES (?, ?, 'revoked', ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()), active["run_id"], settings_revision,
                        retry_count_used, details_json, _now(),
                    ),
                )
                connection.execute("DELETE FROM active_runs WHERE task_id = ?", (task_id,))
            return decision_id

    def set_session_settings(self, settings: Mapping[str, Any]) -> int:
        settings_json = _json(settings, name="session settings")
        with self._transaction() as connection:
            revision = int(
                connection.execute("SELECT MAX(revision) + 1 FROM session_settings").fetchone()[0]
            )
            connection.execute(
                "INSERT INTO session_settings(revision, settings_json, created_at) VALUES (?, ?, ?)",
                (revision, settings_json, _now()),
            )
            return revision

    def get_session_settings(self, revision: int | None = None) -> dict[str, Any]:
        self._ensure_open()
        if revision is None:
            row = self._connection.execute(
                "SELECT * FROM session_settings ORDER BY revision DESC LIMIT 1"
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT * FROM session_settings WHERE revision = ?", (revision,)
            ).fetchone()
        if row is None:
            raise InvalidTransitionError(f"unknown session settings revision: {revision}")
        return {
            "revision": row["revision"],
            "settings": _decoded(row["settings_json"]),
            "created_at": row["created_at"],
        }

    def record_decision(
        self,
        task_id: str,
        revision: int,
        run_id: str,
        kind: str,
        *,
        retry_count_used: int,
        details: Mapping[str, Any] | None = None,
    ) -> str:
        if kind not in {"retry", "continue", "blocked"}:
            raise InvalidTransitionError(f"unsupported run decision kind: {kind}")
        retry_count_used = _nonnegative_integer(retry_count_used, name="retry_count_used")
        details_json = _json({} if details is None else details, name="decision details")
        with self._transaction() as connection:
            self._require_spec(connection, task_id, revision)
            run = self._require_run(connection, run_id)
            if (run["task_id"], run["revision"]) != (task_id, revision):
                raise InvalidTransitionError("decision Task/revision must match its run")
            active = connection.execute(
                "SELECT 1 FROM active_runs WHERE task_id = ? AND run_id = ?",
                (task_id, run_id),
            ).fetchone()
            if active is None:
                raise InvalidTransitionError("decisions can only be linked to the current run")
            previous_count = self._latest_retry_count(connection, run)
            if retry_count_used < previous_count:
                raise InvalidTransitionError("retry count cannot be reset during a run")
            if kind == "retry" and retry_count_used <= previous_count:
                raise InvalidTransitionError("a retry decision must increase the used retry count")
            return self._insert_decision(
                connection,
                task_id=task_id,
                revision=revision,
                run_id=run_id,
                kind=kind,
                settings_revision=self._settings_revision(connection),
                retry_count_used=retry_count_used,
                details_json=details_json,
            )

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT * FROM task_decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            raise InvalidTransitionError(f"unknown decision: {decision_id}")
        return {
            "decision_id": row["decision_id"],
            "task_id": row["task_id"],
            "revision": row["revision"],
            "run_id": row["run_id"],
            "settings_revision": row["settings_revision"],
            "retry_count_used": row["retry_count_used"],
            "kind": row["kind"],
            "details": _decoded(row["details_json"]),
            "created_at": row["created_at"],
        }

    def get_decisions(self, task_id: str, revision: int) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT decision_id FROM task_decisions WHERE task_id = ? AND revision = ? ORDER BY rowid",
            (task_id, revision),
        ).fetchall()
        return [self.get_decision(row["decision_id"]) for row in rows]

    def create_message(
        self,
        task_id: str,
        revision: int,
        run_id: str,
        content: Mapping[str, Any],
        *,
        message_id: str | None = None,
    ) -> str:
        message_id = _identifier(message_id, name="message_id")
        content_json = _json(content, name="logical message content")
        with self._transaction() as connection:
            self._require_spec(connection, task_id, revision)
            run = self._require_run(connection, run_id)
            if (run["task_id"], run["revision"]) != (task_id, revision):
                raise InvalidTransitionError("message Task/revision must match its run")
            connection.execute(
                """INSERT INTO messages(
                    message_id, task_id, revision, run_id, content_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (message_id, task_id, revision, run_id, content_json, _now()),
            )
        return message_id

    def get_message(self, message_id: str) -> dict[str, Any]:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT * FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise InvalidTransitionError(f"unknown message: {message_id}")
        return {
            "message_id": row["message_id"],
            "task_id": row["task_id"],
            "revision": row["revision"],
            "run_id": row["run_id"],
            "content": _decoded(row["content_json"]),
            "created_at": row["created_at"],
        }

    def create_delivery_attempt(self, message_id: str, *, attempt_id: str | None = None) -> str:
        attempt_id = _identifier(attempt_id, name="attempt_id")
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone() is None:
                raise InvalidTransitionError(f"unknown message: {message_id}")
            number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM delivery_attempts WHERE message_id = ?",
                    (message_id,),
                ).fetchone()[0]
            )
            now = _now()
            connection.execute(
                """INSERT INTO delivery_attempts(
                    attempt_id, message_id, attempt_number, created_at
                ) VALUES (?, ?, ?, ?)""",
                (attempt_id, message_id, number, now),
            )
            connection.execute(
                """INSERT INTO delivery_status_events(
                    attempt_id, sequence, status, details_json, created_at
                ) VALUES (?, 1, 'attempted', '{}', ?)""",
                (attempt_id, now),
            )
        return attempt_id

    def record_delivery_status(
        self,
        attempt_id: str,
        status: str,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        if status not in {"api_returned", "omp_processed", "failed", "unknown"}:
            raise InvalidTransitionError(f"unsupported delivery status: {status}")
        details_json = _json({} if details is None else details, name="delivery event details")
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM delivery_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone() is None:
                raise InvalidTransitionError(f"unknown delivery attempt: {attempt_id}")
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM delivery_status_events WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """INSERT INTO delivery_status_events(
                    attempt_id, sequence, status, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (attempt_id, sequence, status, details_json, _now()),
            )
            return int(cursor.lastrowid)

    def get_delivery_history(self, attempt_id: str) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self._connection.execute(
            """SELECT * FROM delivery_status_events WHERE attempt_id = ? ORDER BY sequence""",
            (attempt_id,),
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "attempt_id": row["attempt_id"],
                "sequence": row["sequence"],
                "status": row["status"],
                "details": _decoded(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def record_shell_event(
        self, run_id: str, kind: str, details: Mapping[str, Any] | None = None
    ) -> str:
        if kind not in {"sent", "accepted", "started", "ended", "failed"}:
            raise InvalidTransitionError(f"unsupported shell lifecycle event: {kind}")
        details_json = _json({} if details is None else details, name="shell event details")
        event_id = str(uuid.uuid4())
        with self._transaction() as connection:
            self._require_run(connection, run_id)
            connection.execute(
                """INSERT INTO shell_events(event_id, run_id, kind, details_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, run_id, kind, details_json, _now()),
            )
        return event_id

    def get_shell_history(self, run_id: str) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT * FROM shell_events WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "details": _decoded(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def request_takeover(
        self, run_id: str, details: Mapping[str, Any] | None = None
    ) -> str:
        details_json = _json({} if details is None else details, name="takeover request details")
        event_id = str(uuid.uuid4())
        with self._transaction() as connection:
            self._require_run(connection, run_id)
            connection.execute(
                """INSERT INTO takeover_events(
                    event_id, run_id, request_id, kind, details_json, created_at
                ) VALUES (?, ?, NULL, 'requested', ?, ?)""",
                (event_id, run_id, details_json, _now()),
            )
        return event_id

    def confirm_takeover(
        self, request_id: str, details: Mapping[str, Any] | None = None
    ) -> str:
        details_json = _json({} if details is None else details, name="takeover confirmation details")
        event_id = str(uuid.uuid4())
        with self._transaction() as connection:
            request = connection.execute(
                "SELECT * FROM takeover_events WHERE event_id = ?", (request_id,)
            ).fetchone()
            if request is None or request["kind"] != "requested":
                raise InvalidTransitionError("takeover confirmation requires a request event")
            connection.execute(
                """INSERT INTO takeover_events(
                    event_id, run_id, request_id, kind, details_json, created_at
                ) VALUES (?, ?, ?, 'confirmed', ?, ?)""",
                (event_id, request["run_id"], request_id, details_json, _now()),
            )
        return event_id

    def get_takeover_history(self, run_id: str) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT * FROM takeover_events WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "request_id": row["request_id"],
                "kind": row["kind"],
                "details": _decoded(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def record_log_source(self, subject: dict[str, Any], source_range: dict[str, Any], *,
                          status: str = "observed", source_id: str | None = None) -> str:
        """Persist a source/range identity without copying raw or summary body."""
        owner = parse_subject_ref(subject)
        span = parse_source_range(source_range)
        if status not in {"observed", "storage_gap", "collector_gap", "evicted", "unknown"}:
            raise InvalidTransitionError("unsupported source status")
        if owner["kind"] == "task" and span["source"].get("taskId") != owner["id"]:
            raise InvalidTransitionError("source does not belong to Task")
        if owner["kind"] == "command" and (span["source"].get("commandId") != owner["id"]
                                            or "taskId" in span["source"]):
            raise InvalidTransitionError("source does not belong to command")
        identity = _log_uuid(source_id or str(uuid.uuid4()), name="source_id")
        source = span["source"]
        with self._transaction() as connection:
            if owner["kind"] == "task":
                self._require_spec(connection, owner["id"], source["revision"])
                if "runId" in source:
                    run = self._require_run(connection, source["runId"])
                    if run["task_id"] != owner["id"] or run["revision"] != source["revision"]:
                        raise InvalidTransitionError("source run/revision mismatch")
            connection.execute(
                """INSERT INTO log_source_records(source_id,subject_kind,subject_id,revision,
                   source_json,span_start,span_end,digest,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (identity, owner["kind"], owner["id"], source.get("revision"),
                 canonical_json(source).decode("utf-8"), span["start"], span["end"],
                 span["digest"], status, _now()),
            )
        return identity

    def get_log_sources(self, subject: dict[str, Any], *, limit: int = 100) -> list[dict[str, Any]]:
        owner = parse_subject_ref(subject)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidTransitionError("limit must be 1..100")
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT * FROM log_source_records WHERE subject_kind=? AND subject_id=? ORDER BY created_at,source_id LIMIT ?",
            (owner["kind"], owner["id"], limit),
        ).fetchall()
        return [{"source_id": row["source_id"], "subject": owner,
                 "source": _decoded(row["source_json"]), "start": row["span_start"],
                 "end": row["span_end"], "digest": row["digest"], "status": row["status"],
                 "created_at": row["created_at"]} for row in rows]

    @staticmethod
    def _require_log_run(connection: sqlite3.Connection, source: Mapping[str, Any]) -> None:
        if "runId" not in source:
            return
        row = connection.execute("SELECT task_id,revision FROM runs WHERE run_id=?", (source["runId"],)).fetchone()
        if row is None or row["task_id"] != source.get("taskId") or row["revision"] != source.get("revision"):
            raise InvalidTransitionError("log source does not match immutable Run Task/revision")

    def record_observation_boundary(self, boundary: ObservationBoundary) -> str:
        """기록된 collector 경계만 보존한다. 실제 수집 이벤트 생성은 호출자 책임이다."""
        item = parse_observation_boundary(boundary)
        source = item["source"]
        with self._transaction() as connection:
            self._require_log_run(connection, source)
            connection.execute(
                """INSERT INTO log_observation_boundaries
                   (event_id,task_id,revision,run_id,source_json,kind,offset,observed_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (item["eventId"], source["taskId"], source["revision"], source["runId"],
                 canonical_json(source).decode("utf-8"), item["kind"], item["offset"],
                 item["observedAt"], _now()),
            )
        return item["eventId"]

    def record_gapfree_coverage(self, coverage: ObservationCoverage) -> str:
        """시작부터 최종 drain까지 실제 기록된 byte 구간의 무공백 증거를 검증한다."""
        item = parse_observation_coverage(coverage)
        source = item["source"]
        physical = _summary_source_overlap_key(source)
        with self._transaction() as connection:
            self._require_log_run(connection, source)
            boundaries = []
            for event_id, expected in ((item["startEventId"], "start"), (item["finalEventId"], "final_drain")):
                row = connection.execute("SELECT rowid,* FROM log_observation_boundaries WHERE event_id=?", (event_id,)).fetchone()
                if (row is None or row["kind"] != expected or row["task_id"] != source["taskId"]
                        or row["revision"] != source["revision"] or row["run_id"] != source["runId"]
                        or _decoded(row["source_json"]) != source):
                    raise InvalidTransitionError("coverage boundary source/run/epoch mismatch")
                boundaries.append(row)
            if (boundaries[0]["offset"] != 0 or boundaries[1]["offset"] != item["finalOffset"]
                    or boundaries[0]["observed_at"] > boundaries[1]["observed_at"]
                    or boundaries[0]["rowid"] >= boundaries[1]["rowid"]):
                raise InvalidTransitionError("coverage boundaries are out of order or offset")
            spans = []
            for source_id in item["sourceIds"]:
                row = connection.execute("SELECT * FROM log_source_records WHERE source_id=?", (source_id,)).fetchone()
                if (row is None or row["status"] != "observed" or row["subject_kind"] != "task"
                        or row["subject_id"] != source["taskId"] or row["revision"] != source["revision"]
                        or _decoded(row["source_json"]) != source):
                    raise InvalidTransitionError("coverage requires observed ranges from the same source/run/epoch")
                spans.append((row["span_start"], row["span_end"]))
            spans.sort()
            offset = 0
            for start, end in spans:
                if start != offset:
                    raise InvalidTransitionError("coverage has a gap or overlap")
                offset = end
            if offset != item["finalOffset"]:
                raise InvalidTransitionError("coverage does not reach final drain offset")
            selected = set(item["sourceIds"])
            for row in connection.execute("SELECT source_id,source_json,span_start,span_end,status FROM log_source_records WHERE subject_kind='task' AND subject_id=? AND revision=?", (source["taskId"], source["revision"])):
                other = _decoded(row["source_json"])
                if _summary_source_overlap_key(other) != physical:
                    continue
                if row["status"] != "observed":
                    raise InvalidTransitionError("coverage crosses a recorded source gap")
                if row["source_id"] not in selected:
                    raise InvalidTransitionError("coverage omits recorded source bytes")
            connection.execute(
                """INSERT INTO log_observation_coverage
                   (coverage_id,task_id,revision,run_id,source_json,start_event_id,final_event_id,final_offset,source_ids_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item["coverageId"], source["taskId"], source["revision"], source["runId"],
                 canonical_json(source).decode("utf-8"), item["startEventId"], item["finalEventId"],
                 item["finalOffset"], canonical_json(item["sourceIds"]).decode("utf-8"), _now()),
            )
        return item["coverageId"]

    @staticmethod
    def _coverage_invalid_reason(connection: sqlite3.Connection, coverage: sqlite3.Row) -> str | None:
        """Evaluate a minted witness against the current append-only journal."""
        try:
            covered_source = parse_source_ref(_decoded(coverage["source_json"]))
            physical = _summary_source_overlap_key(covered_source)
            selected = set(_decoded(coverage["source_ids_json"]))
            spans = []
            seen = set()
            for row in connection.execute(
                """SELECT source_id,source_json,span_start,span_end,status FROM log_source_records
                   WHERE subject_kind='task' AND subject_id=? AND revision=?""",
                (coverage["task_id"], coverage["revision"]),
            ):
                if _summary_source_overlap_key(_decoded(row["source_json"])) != physical:
                    continue
                if row["status"] != "observed":
                    return "coverage_source_gap"
                if row["source_id"] not in selected:
                    return "coverage_source_uncovered"
                seen.add(row["source_id"])
                spans.append((row["span_start"], row["span_end"]))
            offset = 0
            for start, end in sorted(spans):
                if start != offset:
                    return "coverage_range_mismatch"
                offset = end
            if seen != selected or offset != coverage["final_offset"]:
                return "coverage_range_mismatch"
            return None
        except (ValueError, TypeError, KeyError):
            return "coverage_metadata_invalid"

    def record_condition_proof(self, task_id: str, revision: int, run_id: str,
                               condition_digest: str, *, matched: bool,
                               observation_complete: bool, source_id: str | None = None,
                               coverage_id: str | None = None,
                               match_start: int | None = None, match_end: int | None = None,
                               full_match_digest: str | None = None,
                               excerpt: bytes = b"", proof_id: str | None = None) -> str:
        """Append durable completion evidence; a gap is never a negative proof."""
        _sha256_hex(condition_digest, name="condition_digest")
        _positive_integer(revision, name="revision")
        if type(matched) is not bool or type(observation_complete) is not bool:
            raise InvalidTransitionError("proof flags must be booleans")
        if type(excerpt) is not bytes or len(excerpt) > 4096:
            raise InvalidTransitionError("proof excerpt must be <= 4KiB")
        if matched:
            if source_id is None:
                raise InvalidTransitionError("matched proof requires a persisted source")
            _nonnegative_integer(match_start, name="match_start")
            _positive_integer(match_end, name="match_end")
            if match_end <= match_start:
                raise InvalidTransitionError("match interval must be nonempty")
            _sha256_hex(full_match_digest, name="full_match_digest")
        elif any(value is not None for value in (match_start, match_end, full_match_digest)) or excerpt:
            raise InvalidTransitionError("unmatched proof cannot claim match content")
        if source_id is not None:
            _log_uuid(source_id, name="source_id")
        if observation_complete != (coverage_id is not None):
            raise InvalidTransitionError("complete proof requires a gap-free coverage ID")
        if coverage_id is not None:
            _log_uuid(coverage_id, name="coverage_id")
        identity = _log_uuid(proof_id or str(uuid.uuid4()), name="proof_id")
        with self._transaction() as connection:
            run = self._require_run(connection, run_id)
            if run["task_id"] != task_id or run["revision"] != revision:
                raise InvalidTransitionError("proof run/revision mismatch")
            if source_id is not None:
                source = connection.execute("SELECT * FROM log_source_records WHERE source_id=?", (source_id,)).fetchone()
                if source is None or source["subject_kind"] != "task" or source["subject_id"] != task_id:
                    raise InvalidTransitionError("proof source does not belong to Task")
                if source["revision"] != revision:
                    raise InvalidTransitionError("proof source revision mismatch")
                source_ref = parse_source_ref(_decoded(source["source_json"]))
                if source["status"] != "observed" or source_ref.get("runId") != run_id:
                    raise InvalidTransitionError("proof requires observed bytes from the exact Run")
                if matched and (match_start != source["span_start"] or match_end != source["span_end"]
                                or full_match_digest != source["digest"]):
                    raise InvalidTransitionError("proof match must equal observed byte range and digest")
            if coverage_id is not None:
                coverage = connection.execute("SELECT * FROM log_observation_coverage WHERE coverage_id=?", (coverage_id,)).fetchone()
                if (coverage is None or coverage["task_id"] != task_id or coverage["revision"] != revision
                        or coverage["run_id"] != run_id):
                    raise InvalidTransitionError("proof coverage does not match Run/revision")
                if source_id is not None and source_id not in _decoded(coverage["source_ids_json"]):
                    raise InvalidTransitionError("proof source is outside complete coverage")
                invalid_reason = self._coverage_invalid_reason(connection, coverage)
                if invalid_reason is not None:
                    raise InvalidTransitionError(f"proof coverage is no longer complete: {invalid_reason}")
            connection.execute(
                """INSERT INTO log_condition_proofs(proof_id,task_id,revision,run_id,
                   condition_digest,source_id,coverage_id,matched,observation_complete,match_start,match_end,
                   full_match_digest,excerpt,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identity, task_id, revision, run_id, condition_digest, source_id, coverage_id,
                 int(matched), int(observation_complete), match_start, match_end,
                 full_match_digest, excerpt, _now()),
            )
        return identity

    def get_condition_proofs(self, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self._ensure_open()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidTransitionError("limit must be 1..100")
        # A deferred transaction gives proof and journal reads one snapshot
        # without acquiring the write reservation used by _transaction().
        own_snapshot = not self._connection.in_transaction
        if own_snapshot:
            self._connection.execute("BEGIN")
        try:
            rows = self._connection.execute(
                "SELECT * FROM log_condition_proofs WHERE run_id=? ORDER BY created_at,proof_id LIMIT ?",
                (run_id, limit),
            ).fetchall()
            view = []
            for row in rows:
                proof = {key: row[key] for key in row.keys()}
                if proof["observation_complete"]:
                    coverage = self._connection.execute(
                        "SELECT * FROM log_observation_coverage WHERE coverage_id=?",
                        (proof["coverage_id"],),
                    ).fetchone()
                    if (coverage is None or coverage["task_id"] != proof["task_id"]
                            or coverage["revision"] != proof["revision"]
                            or coverage["run_id"] != proof["run_id"]):
                        reason = "coverage_metadata_mismatch"
                    else:
                        reason = self._coverage_invalid_reason(self._connection, coverage)
                    if reason is not None:
                        proof["observation_complete"] = 0
                        proof["invalidation_reason"] = reason
                view.append(proof)
            return view
        finally:
            if own_snapshot:
                self._connection.execute("ROLLBACK")

    @staticmethod
    def _summary_state(connection: sqlite3.Connection, request_id: str) -> str | None:
        row = connection.execute(
            "SELECT state FROM summary_request_events_v1 WHERE request_id=? ORDER BY event_id DESC LIMIT 1",
            (request_id,),
        ).fetchone()
        return None if row is None else row[0]

    def reserve_summary_request(self, request: SummaryRequest) -> None:
        """Atomically reject exact or overlapping previously sent/unknown input."""
        item = parse_summary_request(request)
        subject = item["subject"]
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT request_id FROM summary_requests_v1 WHERE subject_kind=? AND subject_id=? AND input_key=?",
                (subject["kind"], subject["id"], item["inputKey"]),
            ).fetchall()
            if any(self._summary_state(connection, row[0]) != "not_sent" for row in existing):
                raise InvalidTransitionError("summary input already reserved or sent")
            for span in [*item["sources"], *item["sourceGaps"]]:
                self._require_log_run(connection, span["source"])
                source_key = _summary_source_overlap_key(span["source"])
                rows = connection.execute(
                    """SELECT r.request_id FROM summary_request_ranges_v1 r
                       WHERE r.source_key=?
                         AND r.span_start<? AND r.span_end>?""",
                    (source_key, span["end"], span["start"]),
                ).fetchall()
                if any(self._summary_state(connection, row[0]) != "not_sent" for row in rows):
                    raise InvalidTransitionError("summary source range overlaps reserved or sent input")
            connection.execute(
                """INSERT INTO summary_requests_v1(request_id,subject_kind,subject_id,revision,
                   input_key,skill_digest,request_json,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (item["requestId"], subject["kind"], subject["id"], item["revision"],
                 item["inputKey"], item["skillDigest"], canonical_json(item).decode("utf-8"), _now()),
            )
            for index, span in enumerate([*item["sources"], *item["sourceGaps"]]):
                connection.execute(
                    "INSERT INTO summary_request_ranges_v1(request_id,range_index,range_kind,source_key,span_start,span_end) VALUES(?,?,?,?,?,?)",
                    (item["requestId"], index, "source" if index < len(item["sources"]) else "gap",
                     _summary_source_overlap_key(span["source"]),
                     span["start"], span["end"]),
                )
            connection.execute(
                "INSERT INTO summary_request_events_v1(request_id,state,created_at) VALUES(?,'prepared',?)",
                (item["requestId"], _now()),
            )

    def record_summary_state(self, request_id: str, state: str, *, detail: str | None = None) -> None:
        _log_uuid(request_id, name="request_id")
        transitions = {
            "prepared": {"sending", "not_sent"},
            "sending": {"executed_observed", "unknown", "not_sent"},
            "executed_observed": {"rejected", "storage_failed"},
            "unknown": {"rejected", "storage_failed"},
        }
        if detail is not None and (not isinstance(detail, str) or not detail or len(detail.encode("utf-8")) > 256):
            raise InvalidTransitionError("summary state detail must be bounded text")
        if state == "not_sent" and detail is None:
            raise InvalidTransitionError("not_sent requires confirmation detail")
        with self._transaction() as connection:
            previous = self._summary_state(connection, request_id)
            if state not in transitions.get(previous, set()):
                raise InvalidTransitionError(f"invalid summary transition: {previous} -> {state}")
            connection.execute(
                "INSERT INTO summary_request_events_v1(request_id,state,detail,created_at) VALUES(?,?,?,?)",
                (request_id, state, detail, _now()),
            )

    def get_summary_request(self, request_id: str) -> dict[str, Any] | None:
        _log_uuid(request_id, name="request_id")
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM summary_requests_v1 WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        receipt = self._connection.execute("SELECT * FROM summary_result_receipts_v1 WHERE request_id=?", (request_id,)).fetchone()
        return {"request": parse_summary_request(_decoded(row["request_json"])),
                "state": self._summary_state(self._connection, request_id),
                "receipt": None if receipt is None else {key: receipt[key] for key in receipt.keys()}}

    def list_unresolved_summary_requests(self, *, limit: int = 100,
                                         after: str | None = None) -> list[dict[str, Any]]:
        """Bounded restart reconciliation; never starts or retries a model call."""
        self._ensure_open()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidTransitionError("limit must be 1..100")
        cursor = 0
        if after is not None:
            _log_uuid(after, name="after")
            row = self._connection.execute("SELECT rowid FROM summary_requests_v1 WHERE request_id=?", (after,)).fetchone()
            if row is None:
                raise InvalidTransitionError("unknown summary request cursor")
            cursor = row[0]
        rows = self._connection.execute(
            """SELECT q.request_id FROM summary_requests_v1 q
               JOIN summary_request_events_v1 e ON e.event_id=(
                   SELECT MAX(event_id) FROM summary_request_events_v1 WHERE request_id=q.request_id
               )
               WHERE q.rowid>? AND e.state NOT IN ('stored','not_sent')
               ORDER BY q.rowid LIMIT ?""",
            (cursor, limit),
        ).fetchall()
        return [self.get_summary_request(row[0]) for row in rows]

    def record_summary_result_receipt(self, result: SummaryResult, entry_id: str) -> bool:
        """Commit the first valid result reference; later duplicates are logged."""
        item = parse_summary_result(result)
        _log_uuid(entry_id, name="entry_id")
        with self._transaction() as connection:
            row = connection.execute("SELECT request_json FROM summary_requests_v1 WHERE request_id=?", (item["requestId"],)).fetchone()
            if row is None:
                raise InvalidTransitionError("unknown summary request")
            request = parse_summary_request(_decoded(row[0]))
            if (item["inputKey"] != request["inputKey"] or item["skillDigest"] != request["skillDigest"]
                    or item["sources"] != request["sources"]):
                raise InvalidTransitionError("summary result source/skill/input mismatch")
            result_digest = sha256(canonical_json(item)).hexdigest()
            if connection.execute("SELECT 1 FROM summary_result_receipts_v1 WHERE request_id=?", (item["requestId"],)).fetchone():
                connection.execute(
                    "INSERT INTO summary_result_drops_v1(drop_id,request_id,result_digest,reason,created_at) VALUES(?,?,?,'duplicate',?)",
                    (str(uuid.uuid4()), item["requestId"], result_digest, _now()),
                )
                return False
            state = self._summary_state(connection, item["requestId"])
            if state not in {"sending", "executed_observed", "unknown", "storage_failed"}:
                raise InvalidTransitionError(f"result cannot be stored from state {state}")
            connection.execute(
                "INSERT INTO summary_result_receipts_v1(receipt_id,request_id,entry_id,result_digest,created_at) VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()), item["requestId"], entry_id, result_digest, _now()),
            )
            connection.execute(
                "INSERT INTO summary_request_events_v1(request_id,state,detail,created_at) VALUES(?,'stored',?,?)",
                (item["requestId"], entry_id, _now()),
            )
            return True

    def _durable_counts(self) -> dict[str, int]:
        """Private, test-only read helper for rollback assertions."""
        self._ensure_open()
        tables = (
            "tasks", "task_specs", "session_settings", "runs", "task_decisions",
            "authorization_consumption", "active_runs", "run_events",
            "invalidated_revisions", "messages", "delivery_attempts",
            "delivery_status_events", "shell_events", "takeover_events",
        )
        return {
            table: int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
