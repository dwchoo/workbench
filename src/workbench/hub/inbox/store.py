"""``<root>/.wb/inbox.sqlite3``: one project's request store (SPEC "요청 저장소", ADR-0005).

The Hub is the only writer. An ``InboxStore`` is owned by its project's single executor thread
and is not thread-safe. While a connection is open the store holds ``inbox.lock`` (flock); a
store left unused for ``idle_seconds`` closes and releases the lock.
"""
from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Any, Callable

from workbench.backend.paths import DataDirError
from workbench.backend.project import read_project_marker

from .limits import InboxError

DB_NAME = "inbox.sqlite3"
LOCK_NAME = "inbox.lock"
IDLE_SECONDS = 60.0
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE requests (
    request_id TEXT PRIMARY KEY,
    seq INTEGER UNIQUE NOT NULL CHECK (seq > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    origin_kind TEXT NOT NULL CHECK (origin_kind IN ('owner', 'board_agent', 'api_key')),
    origin_key_id TEXT,
    origin_name TEXT,
    idem_key TEXT,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    state TEXT NOT NULL CHECK (state IN ('NEW', 'REVIEWING', 'NEEDS_INFO', 'READY', 'RUNNING', 'CLOSED')),
    outcome TEXT CHECK (outcome IN ('success', 'failed', 'rejected', 'cancelled')),
    review_round INTEGER NOT NULL CHECK (review_round > 0),
    delivery_state TEXT NOT NULL
        CHECK (delivery_state IN ('none', 'pending', 'offered', 'delivered', 'unknown', 'rejected')),
    delivery_id TEXT,
    delivery_backend TEXT,
    delivery_at REAL,
    task_id TEXT,
    spec_revision INTEGER,
    task_view TEXT,
    cancel_state TEXT CHECK (cancel_state IN ('requested', 'stop_requested')),
    result_summary TEXT,
    CHECK ((state = 'CLOSED') = (outcome IS NOT NULL))
);
CREATE UNIQUE INDEX requests_idempotency
    ON requests (origin_kind, ifnull(origin_key_id, ''), idem_key) WHERE idem_key IS NOT NULL;
CREATE TABLE request_revisions (
    request_id TEXT NOT NULL REFERENCES requests (request_id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    author_kind TEXT NOT NULL,
    author_name TEXT,
    PRIMARY KEY (request_id, revision)
);
CREATE TABLE comments (
    comment_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES requests (request_id),
    seq INTEGER NOT NULL,
    created_at REAL NOT NULL,
    author_kind TEXT NOT NULL,
    author_name TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('comment', 'answer', 'question', 'progress', 'decision', 'system')),
    body TEXT NOT NULL,
    UNIQUE (request_id, seq)
);
CREATE TABLE links (
    request_id TEXT NOT NULL REFERENCES requests (request_id),
    task_id TEXT NOT NULL,
    spec_revision INTEGER,
    linked_at REAL NOT NULL,
    state TEXT NOT NULL
);
CREATE TABLE events (
    event_id INTEGER PRIMARY KEY,
    request_id TEXT REFERENCES requests (request_id),
    at REAL NOT NULL,
    actor_kind TEXT NOT NULL,
    actor_name TEXT,
    type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    detail TEXT
);
"""


def _unavailable(detail: str) -> InboxError:
    return InboxError("inbox_unavailable", detail)


class InboxStore:
    def __init__(self, root: Path, project_id: str, *, clock: Callable[[], float] = time.time,
                 idle_seconds: float = IDLE_SECONDS):
        self.root = Path(root)
        self.project_id = project_id
        self.data_dir = self.root / ".wb"
        self.path = self.data_dir / DB_NAME
        self.lock_path = self.data_dir / LOCK_NAME
        self._clock = clock
        self._idle_seconds = idle_seconds
        self._connection: sqlite3.Connection | None = None
        self._lock_fd: int | None = None
        self._last_used = 0.0

    # -- public -----------------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._connection is not None

    def read(self, fn: Callable[[sqlite3.Connection], Any], *, missing: Any = None) -> Any:
        """``fn(connection)``; a missing or never-initialized file returns ``missing`` and creates nothing."""
        connection = self._open(create=False)
        try:
            if connection is None or not self._initialized(connection):
                return missing
            return fn(connection)
        except sqlite3.Error as exc:
            raise _unavailable(f"read failed: {exc}") from exc

    def write(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """``BEGIN IMMEDIATE`` → ``fn(connection)`` → ``COMMIT``; any exception rolls back everything."""
        connection = self._open(create=True)
        assert connection is not None
        try:
            if not self._initialized(connection):
                self._set_wal(connection)  # journal_mode cannot change inside a transaction
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise InboxError("internal_error", f"cannot begin a write: {exc}") from exc
        try:
            if not self._initialized(connection):
                self._create_schema(connection)
            result = fn(connection)
            connection.execute("COMMIT")
        except BaseException as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.Error):
                raise InboxError("internal_error", f"write failed: {exc}") from exc
            raise
        return result

    def close_if_idle(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        if self.is_open and now - self._last_used >= self._idle_seconds:
            self.close()
            return True
        return False

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._release_lock()

    # -- opening ----------------------------------------------------------------------------
    def _open(self, *, create: bool) -> sqlite3.Connection | None:
        self._last_used = self._clock()
        if self._connection is not None:
            return self._connection
        self._check_data_dir()
        if not create and not os.path.lexists(self.path):
            return None
        self._acquire_lock()
        try:
            connection = self._connect(create)
            try:
                connection.execute("PRAGMA busy_timeout=2000")
                connection.execute("PRAGMA foreign_keys=ON")
                if self._initialized(connection):
                    self._check_meta(connection)
                    self._set_wal(connection)
                elif create:
                    self._set_wal(connection)
                connection.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError as exc:
                connection.close()
                raise _unavailable(f"corrupt: {exc}") from exc
            except BaseException:
                connection.close()
                raise
        except BaseException:
            self._release_lock()
            raise
        self._connection = connection
        return connection

    def _check_data_dir(self) -> None:
        try:
            marker = read_project_marker(self.data_dir)
        except DataDirError as exc:
            raise InboxError("project_unavailable", str(exc)) from exc
        if marker.get("project_id") != self.project_id:
            raise InboxError("project_unavailable",
                             f"{self.data_dir} belongs to project {marker.get('project_id')}")

    def _acquire_lock(self) -> None:
        try:
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise _unavailable(f"cannot open {self.lock_path}: {exc.strerror}") from exc
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise _unavailable(f"{self.lock_path} is not a private lock file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise InboxError("inbox_busy", f"{self.lock_path} is held by another process") from exc
                raise _unavailable(f"cannot lock {self.lock_path}: {exc.strerror}") from exc
        except BaseException:
            os.close(descriptor)
            raise
        self._lock_fd = descriptor

    def _release_lock(self) -> None:
        descriptor, self._lock_fd = self._lock_fd, None
        if descriptor is not None:
            os.close(descriptor)  # closing releases the flock

    def _connect(self, create: bool) -> sqlite3.Connection:
        if create:
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     0o600)
                os.close(descriptor)
            except FileExistsError:
                pass
            except OSError as exc:
                raise _unavailable(f"cannot create {self.path}: {exc.strerror}") from exc
        try:
            info = os.lstat(self.path)
        except OSError as exc:
            raise _unavailable(f"cannot inspect {self.path}: {exc.strerror}") from exc
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise _unavailable(f"{self.path} is not a private regular file")
        try:
            connection = sqlite3.connect(f"{self.path.as_uri()}?mode=rw", uri=True, isolation_level=None,
                                         check_same_thread=True)
        except sqlite3.Error as exc:
            raise _unavailable(f"cannot open {self.path}: {exc}") from exc
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _set_wal(connection: sqlite3.Connection) -> None:
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if not mode or str(mode[0]).lower() != "wal":
            raise _unavailable(f"cannot enable WAL (journal_mode={mode and mode[0]})")

    @staticmethod
    def _initialized(connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()[0] == 1

    def _check_meta(self, connection: sqlite3.Connection) -> None:
        meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            raise _unavailable(f"unsupported inbox schema {meta.get('schema_version')!r}")
        if meta.get("project_id") != self.project_id:
            raise InboxError("inbox_mismatch",
                             f"{self.path} belongs to project {meta.get('project_id')}, not {self.project_id}")

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        if connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
            raise _unavailable(f"{self.path} has unknown tables")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", [
            ("schema_version", str(SCHEMA_VERSION)), ("project_id", self.project_id),
            ("review_mode", "auto"), ("created_at", repr(self._clock())),
        ])
