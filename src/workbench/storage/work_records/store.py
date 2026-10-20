"""SQLite persistence for explicit WB records; every operation owns its connection."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Iterator
from urllib.parse import quote
from uuid import uuid4

from workbench.contracts.records_v1 import (
    RecordContractError, parse_record, positive, record_id as validate_id, validate_snapshot,
)
from workbench.tasks.repository import InvalidRevisionError, RepositoryClosedError, TaskRepository

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE db_meta (id INTEGER PRIMARY KEY CHECK(id=1), kind TEXT NOT NULL);
INSERT INTO db_meta VALUES(1, 'workbench-records-v1');
CREATE TABLE records (record_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE revisions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL REFERENCES records(record_id),
    revision INTEGER NOT NULL CHECK(revision > 0),
    title TEXT NOT NULL, body TEXT NOT NULL, task_id TEXT,
    recorded_at TEXT NOT NULL, actor TEXT NOT NULL,
    UNIQUE(record_id, revision)
);
PRAGMA user_version=1;
"""
# INSERT guards also reject REPLACE when an external SQLite connection has
# recursive_triggers disabled (SQLite's default for implicit replacement deletes).
# Guard physical identities as well as logical keys: either conflict can delete
# an existing row before SQLite inserts the replacement.
for _table, _duplicate in (("records", "record_id=NEW.record_id OR rowid=NEW.rowid"),
                           ("revisions", "sequence=NEW.sequence OR (record_id=NEW.record_id AND revision=NEW.revision)"),
                           ("db_meta", "id=NEW.id")):
    _SCHEMA += f"""
CREATE TRIGGER {_table}_immutable_update BEFORE UPDATE ON {_table}
BEGIN SELECT RAISE(ABORT, 'append-only records'); END;
CREATE TRIGGER {_table}_immutable_delete BEFORE DELETE ON {_table}
BEGIN SELECT RAISE(ABORT, 'append-only records'); END;
CREATE TRIGGER {_table}_no_replace BEFORE INSERT ON {_table}
WHEN EXISTS(SELECT 1 FROM {_table} WHERE {_duplicate})
BEGIN SELECT RAISE(ABORT, 'append-only records'); END;
"""

_READ = """SELECT r.record_id,r.created_at,v.revision,v.title,v.body,
    v.task_id,v.recorded_at,v.actor FROM records r JOIN revisions v USING(record_id)
    WHERE r.record_id=? AND (? IS NULL OR v.revision=?) ORDER BY v.revision DESC LIMIT 1"""
_INSERT = """INSERT INTO revisions(record_id,revision,title,body,task_id,recorded_at,actor)
    VALUES(?,?,?,?,?,?,?)"""


_SCHEMA_QUERY = "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"


@lru_cache(maxsize=1)
def _expected_schema() -> tuple:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        return tuple(connection.execute(_SCHEMA_QUERY).fetchall())
    finally:
        connection.close()


class RecordStoreError(RuntimeError):
    """Unsafe private path, schema, or storage failure; content is not diagnostic text."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _rejected(exc: RecordContractError) -> dict:
    reason = str(exc) if str(exc) in {"body_too_large", "title_too_large"} else "invalid_argument"
    return {"status": "rejected", "reason": reason}


def _not_found() -> dict:
    return {"status": "not_found", "reason": "not_found"}


def _unavailable() -> dict:
    return {"status": "unavailable", "reason": "store_unavailable"}


def _private_file(dir_fd: int, name: str) -> os.stat_result | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise RecordStoreError("record file must be owned single-link mode 0600")
        return info
    finally:
        os.close(descriptor)


def _directory(path: Path) -> int:
    """Walk without symlinks; require the existing final data root to be private."""
    raw = os.fspath(path)
    if not path.is_absolute() or os.path.normpath(raw) != raw or raw == "/":
        raise RecordStoreError("normalized private absolute root required")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RecordStoreError("record root must be owned mode 0700")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class RecordStore:
    """Bound to one host-selected DB path; no shared or persistent SQLite connection.

    Results are domain dictionaries. `saved`/`ok` include a full immutable snapshot in
    `record`; failures carry `status` and a content-free `reason`. Reads never create
    a DB. A non-null Task requires an injected repository bound to this root's tasks.
    """
    def __init__(self, path: Path, *, task_repository: TaskRepository | None = None):
        self.path = Path(path)
        self.task_repository = task_repository

    @contextmanager
    def _connection(self, *, write: bool = False, create: bool = False) -> Iterator[sqlite3.Connection | None]:
        directory = _directory(self.path.parent)
        connection = None
        locked = False
        try:
            if write:
                deadline = time.monotonic() + 0.25
                while True:
                    try:
                        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RecordStoreError("record writer busy")
                        time.sleep(0.01)
            original = _private_file(directory, self.path.name)
            new = original is None
            if new and not create:
                yield None
                return
            if new:
                descriptor = os.open(self.path.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
                os.close(descriptor)
                original = _private_file(directory, self.path.name)
            for suffix in ("-journal", "-wal", "-shm"):
                _private_file(directory, self.path.name + suffix)
            # The live directory fd binds SQLite's DB and journal to the checked root.
            bound = f"/proc/self/fd/{directory}/{self.path.name}"
            uri = f"file:{quote(bound, safe='/')}?mode={'rw' if write else 'ro'}"
            connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0.25)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=250")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA recursive_triggers=ON")
            if write:
                connection.execute("PRAGMA synchronous=FULL")
            else:
                connection.execute("PRAGMA query_only=ON")
            if new:
                connection.executescript("BEGIN IMMEDIATE;" + _SCHEMA + "COMMIT;")
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise RecordStoreError("unsupported record schema")
            if tuple(tuple(row) for row in connection.execute(_SCHEMA_QUERY)) != _expected_schema():
                raise RecordStoreError("record schema was changed")
            metadata = connection.execute("SELECT id,kind FROM db_meta").fetchall()
            if [tuple(row) for row in metadata] != [(1, "workbench-records-v1")]:
                raise RecordStoreError("wrong record schema identity")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise RecordStoreError("unsupported record journal mode")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RecordStoreError("record integrity failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RecordStoreError("record references invalid")
            current = _private_file(directory, self.path.name)
            if current is None or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                raise RecordStoreError("record file changed during open")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            if locked:
                fcntl.flock(directory, fcntl.LOCK_UN)
            os.close(directory)

    def _check_task(self, task_id: str | None) -> dict | None:
        if task_id is None:
            return None
        repository = self.task_repository
        if repository is None or Path(os.path.abspath(repository.path)) != self.path.parent / "tasks.sqlite3":
            return {"status": "unavailable", "reason": "task_lookup_unavailable"}
        try:
            repository.get_task_spec(task_id)
        except InvalidRevisionError:
            return {"status": "rejected", "reason": "task_not_found"}
        except (sqlite3.Error, RepositoryClosedError, ValueError, OSError):
            return {"status": "unavailable", "reason": "task_lookup_unavailable"}
        return None

    def create(self, *, title: str, body: str, task_id: str | None = None, actor: str) -> dict:
        try:
            validate_snapshot(title, body, task_id, actor)
        except RecordContractError as exc:
            return _rejected(exc)
        failure = self._check_task(task_id)
        if failure is not None:
            return failure
        record_id, now = str(uuid4()), _now()
        snapshot = dict(record_id=record_id, revision=1, title=title, body=body,
                        task_id=task_id, created_at=now, recorded_at=now, actor=actor)
        try:
            with self._connection(write=True, create=True) as connection:
                connection.execute("INSERT INTO records VALUES(?,?)", (record_id, now))
                connection.execute(_INSERT, (record_id, 1, title, body, task_id, now, actor))
            # Exiting the context confirms COMMIT before this success is observable.
            return {"status": "saved", "record": snapshot}
        except (OSError, sqlite3.Error, RecordStoreError, RecordContractError):
            return _unavailable()

    def get(self, record_id: str, revision: int | None = None) -> dict:
        try:
            validate_id(record_id)
            if revision is not None:
                positive(revision, "revision")
        except RecordContractError as exc:
            return _rejected(exc)
        try:
            with self._connection() as connection:
                if connection is None:
                    return _not_found()
                row = connection.execute(_READ, (record_id, revision, revision)).fetchone()
                return _not_found() if row is None else {"status": "ok", "record": parse_record(dict(row))}
        except (OSError, sqlite3.Error, RecordStoreError, RecordContractError):
            return _unavailable()

    def revise(self, record_id: str, *, expected_revision: int, title: str,
               body: str, task_id: str | None = None, actor: str) -> dict:
        try:
            validate_id(record_id)
            positive(expected_revision, "expected_revision")
            validate_snapshot(title, body, task_id, actor)
        except RecordContractError as exc:
            return _rejected(exc)
        failure = self._check_task(task_id)
        if failure is not None:
            return failure
        try:
            with self._connection(write=True) as connection:
                if connection is None:
                    return _not_found()
                row = connection.execute(_READ, (record_id, None, None)).fetchone()
                if row is None:
                    return _not_found()
                previous = parse_record(dict(row))
                if previous["revision"] != expected_revision:
                    return {"status": "rejected", "reason": "revision_conflict"}
                now = _now()
                connection.execute(_INSERT, (record_id, expected_revision + 1, title, body, task_id, now, actor))
                snapshot = dict(record_id=record_id, revision=expected_revision + 1, title=title,
                                body=body, task_id=task_id, created_at=previous["created_at"],
                                recorded_at=now, actor=actor)
            return {"status": "saved", "record": snapshot}
        except (OSError, sqlite3.Error, RecordStoreError, RecordContractError):
            return _unavailable()

    def list(self, *, task_id: str | None = None, limit: int = 20,
             after: str | None = None) -> dict:
        from .query import list_records
        return list_records(self, task_id=task_id, limit=limit, after=after)

    def search(self, query: str, *, task_id: str | None = None, limit: int = 20,
               after: str | None = None) -> dict:
        from .query import search_records
        return search_records(self, query, task_id=task_id, limit=limit, after=after)

    def history(self, record_id: str, *, limit: int = 20, after: str | None = None) -> dict:
        from .query import history_records
        return history_records(self, record_id, limit=limit, after=after)
