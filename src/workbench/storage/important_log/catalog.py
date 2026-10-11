"""Crash-safe active UUID selection; archived SQLite files are never auto-deleted."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator
from uuid import uuid4

from workbench.contracts.logs_v1 import SubjectRef, SourceRange, parse_subject_ref
from .control import ControlError, parse_control, validate_pointer
from .lifecycle import SubjectLifecycle
from .maintenance import ImportantLogMaintenance, TARGET_BYTES, LOW_WATER_BYTES
from .store import (ImportantLogStore, ImportantLogStoreError, _canonical_uuid,
                    _check_file, _directory_fd)

_POINTER = "active.json"
_INTENT = "rollover.intent.json"
_DELETE_INTENT = "delete.intent.json"
_LEASE = "maintenance.lock"
_CONTROL_LIMIT = 1_000_000
_MAX_TRANSACTION_BYTES = 4_000_000
_MIN_FREE_BYTES = 16_000_000
_WRITER_LEASE_RECHECK_SECONDS = 0.01


def _read_control(fd: int, name: str) -> dict[str, Any] | None:
    info = _check_file(fd, name, required=False)
    if info is None:
        return None
    if info.st_size > _CONTROL_LIMIT:
        raise ImportantLogStoreError("important catalog control is oversized")
    desc = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        opened = os.fstat(desc)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ImportantLogStoreError("important catalog control changed during read")
        raw = os.read(desc, _CONTROL_LIMIT + 1)
    finally:
        os.close(desc)
    try:
        return parse_control(raw)
    except ControlError as exc:
        raise ImportantLogStoreError(str(exc)) from exc


def _write_control(fd: int, name: str, value: dict[str, Any]) -> None:
    _check_file(fd, name, required=False)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > _CONTROL_LIMIT:
        raise ImportantLogStoreError("important catalog control is oversized")
    temporary = "." + name + "." + uuid4().hex
    desc = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                   0o600, dir_fd=fd)
    try:
        with os.fdopen(desc, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def _unlink_control(fd: int, name: str) -> None:
    _check_file(fd, name, required=True)
    os.unlink(name, dir_fd=fd)
    os.fsync(fd)


def _pointer(value: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_pointer(value)
    except ControlError as exc:
        raise ImportantLogStoreError(str(exc)) from exc


def _intent(value: dict[str, Any]) -> dict[str, Any]:
    if (set(value) != {"version", "from", "to", "generation", "checkpoint"}
            or value["version"] != 1 or type(value["generation"]) is not int
            or value["generation"] < 2 or value["checkpoint"] not in ("complete", "busy", "error")):
        raise ImportantLogStoreError("invalid rollover intent")
    _canonical_uuid(value["from"])
    _canonical_uuid(value["to"])
    if value["from"] == value["to"]:
        raise ImportantLogStoreError("invalid rollover identity")
    return value


def _delete_intent(value: dict[str, Any]) -> dict[str, Any]:
    if (set(value) != {"version", "dbUuid", "active", "generation"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["generation"]) is not int or value["generation"] < 1):
        raise ImportantLogStoreError("invalid archive deletion intent")
    _canonical_uuid(value["dbUuid"])
    _canonical_uuid(value["active"])
    if value["dbUuid"] == value["active"]:
        raise ImportantLogStoreError("archive deletion intent targets active DB")
    return value


def _storage_reason(exc: BaseException) -> str:
    if isinstance(exc, sqlite3.OperationalError):
        code = getattr(exc, "sqlite_errorcode", None)
        if code is not None and code & 0xff == sqlite3.SQLITE_FULL:
            return "physical_full"
        detail = str(exc).lower()
        if "locked" in detail or "busy" in detail:
            return "busy"
        if "disk is full" in detail or "database is full" in detail:
            return "physical_full"
        if "readonly" in detail or "permission" in detail:
            return "permission"
        if "malformed" in detail or "corrupt" in detail:
            return "corrupt"
    if isinstance(exc, OSError):
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            return "physical_full"
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return "permission"
        return "io_error"
    if isinstance(exc, sqlite3.DatabaseError):
        return "sqlite_error"
    return "unsafe_or_corrupt"


class ImportantLogCatalog:
    """Serialized active write/maintenance port. No model or core-state authority."""

    def __init__(self, directory: Path, *, target_bytes: int = TARGET_BYTES,
                 low_water_bytes: int = LOW_WATER_BYTES, create: bool = False,
                 writer_lease_timeout_s: float = 0.1):
        if not (type(target_bytes) is int and type(low_water_bytes) is int
                and 0 < low_water_bytes < target_bytes):
            raise ValueError("invalid active DB target")
        if (type(writer_lease_timeout_s) not in (int, float)
                or not math.isfinite(writer_lease_timeout_s)
                or writer_lease_timeout_s < 0):
            raise ValueError("invalid writer lease timeout")
        self.directory = Path(directory)
        self.target_bytes = target_bytes
        self.low_water_bytes = low_water_bytes
        self.writer_lease_timeout_s = float(writer_lease_timeout_s)
        self.last_status: dict[str, Any] = {"state": "unknown"}
        self._last_probe_monotonic = 0.0
        with self._lease(create=create) as fd:
            pointer = _read_control(fd, _POINTER)
            if pointer is None:
                if not create or self._disk_uuids(fd):
                    raise ImportantLogStoreError("active identity missing; existing DBs preserved")
                with ImportantLogStore.create(self.directory) as store:
                    identity = store.db_uuid
                pointer = {"version": 1, "generation": 1, "active": identity,
                           "archives": [], "archiveChecks": {}}
                _write_control(fd, _POINTER, pointer)
            pointer = self._recover(fd, _pointer(pointer))
            with ImportantLogStore.open(self.directory, pointer["active"], _catalog_owned=True) as active:
                check = ImportantLogMaintenance(active, target_bytes=target_bytes,
                                                low_water_bytes=low_water_bytes).quick_check(
                                                    now=datetime.now(timezone.utc))
                if check["status"] not in ("ok", "cached"):
                    raise ImportantLogStoreError("active database startup check incomplete")

    @contextmanager
    def _lease(self, *, create: bool = False) -> Iterator[int]:
        fd = _directory_fd(self.directory, create=create)
        try:
            flags = os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
            if create:
                flags |= os.O_CREAT
            lock = os.open(_LEASE, flags, 0o600, dir_fd=fd)
            try:
                name_info = _check_file(fd, _LEASE, required=True)
                lock_info = os.fstat(lock)
                if ((name_info.st_dev, name_info.st_ino) != (lock_info.st_dev, lock_info.st_ino)):
                    raise ImportantLogStoreError("maintenance lease changed during open")
                deadline = time.monotonic() + self.writer_lease_timeout_s
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise sqlite3.OperationalError("important catalog lease busy")
                        time.sleep(_WRITER_LEASE_RECHECK_SECONDS)
                yield fd
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                os.close(lock)
        finally:
            os.close(fd)

    def _disk_uuids(self, fd: int) -> list[str]:
        result = []
        for name in os.listdir(fd):
            if not name.endswith(".sqlite3"):
                continue
            try:
                identity = _canonical_uuid(name[:-8])
            except ImportantLogStoreError:
                raise ImportantLogStoreError("unknown SQLite file in important catalog")
            _check_file(fd, name, required=True)
            for suffix in ("-wal", "-shm"):
                _check_file(fd, name + suffix, required=False)
            result.append(identity)
        return sorted(result)

    def _recover(self, fd: int, pointer: dict[str, Any]) -> dict[str, Any]:
        deletion = _read_control(fd, _DELETE_INTENT)
        if deletion is not None:
            if _read_control(fd, _INTENT) is not None:
                raise ImportantLogStoreError("conflicting important catalog intents")
            pointer = self._finish_delete(fd, pointer, _delete_intent(deletion))
        present = self._disk_uuids(fd)
        if pointer["active"] not in present or any(identity not in present for identity in pointer["archives"]):
            raise ImportantLogStoreError("active/archive catalog DB missing")
        intent = _read_control(fd, _INTENT)
        if intent is None:
            return pointer
        intent = _intent(intent)
        if intent["from"] != pointer["active"] and intent["to"] != pointer["active"]:
            raise ImportantLogStoreError("rollover intent disagrees with active identity")
        if intent["to"] not in present:
            raise ImportantLogStoreError("rollover target missing; active identity left unchanged")
        # Existing WAL is read by SQLite. Never use immutable=1 on this path.
        with ImportantLogStore.open(self.directory, intent["to"], readonly=True) as target:
            check = target._connection.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise ImportantLogStoreError("rollover target failed recovery quick_check")
        if pointer["active"] == intent["from"]:
            if pointer["generation"] + 1 != intent["generation"]:
                raise ImportantLogStoreError("rollover generation mismatch")
            pointer = {"version": 1, "generation": intent["generation"],
                       "active": intent["to"], "archives": [*pointer["archives"], intent["from"]],
                       "archiveChecks": {**pointer["archiveChecks"],
                                         intent["from"]: intent["checkpoint"]}}
            _write_control(fd, _POINTER, pointer)
        elif (pointer["generation"] != intent["generation"]
              or intent["from"] not in pointer["archives"]
              or pointer["archiveChecks"].get(intent["from"]) != intent["checkpoint"]):
            raise ImportantLogStoreError("published rollover generation mismatch")
        _unlink_control(fd, _INTENT)
        return pointer

    @staticmethod
    def _archive_files(fd: int, identity: str) -> list[str]:
        base = _canonical_uuid(identity) + ".sqlite3"
        return [base + suffix for suffix in ("-shm", "-wal", "")]

    def _finish_delete(self, fd: int, pointer: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        identity = intent["dbUuid"]
        if pointer["active"] != intent["active"] or pointer["active"] == identity:
            raise ImportantLogStoreError("archive deletion intent disagrees with active DB")
        if pointer["generation"] == intent["generation"]:
            if identity not in pointer["archives"]:
                raise ImportantLogStoreError("archive deletion intent is not catalogued")
            unpublished = True
        elif pointer["generation"] == intent["generation"] + 1:
            if identity in pointer["archives"]:
                raise ImportantLogStoreError("published archive deletion still catalogued")
            unpublished = False
        else:
            raise ImportantLogStoreError("archive deletion generation mismatch")
        # The intent was written only after protected anchors were verified under
        # this same lease. After a crash, completing exactly this UUID is safe.
        for name in self._archive_files(fd, identity):
            if _check_file(fd, name, required=False) is not None:
                os.unlink(name, dir_fd=fd)
                os.fsync(fd)
        if unpublished:
            pointer = {**pointer, "generation": pointer["generation"] + 1,
                       "archives": [item for item in pointer["archives"] if item != identity],
                       "archiveChecks": {key: value for key, value in pointer["archiveChecks"].items()
                                         if key != identity}}
            _write_control(fd, _POINTER, pointer)
        _unlink_control(fd, _DELETE_INTENT)
        return pointer

    def _current(self, fd: int) -> dict[str, Any]:
        value = _read_control(fd, _POINTER)
        if value is None:
            raise ImportantLogStoreError("active pointer unavailable")
        return self._recover(fd, _pointer(value))

    def catalog_snapshot(self) -> dict[str, Any]:
        with self._lease() as fd:
            pointer = self._current(fd)
            present = self._disk_uuids(fd)
            known = {pointer["active"], *pointer["archives"]}
            sizes: dict[str, int] = {}
            for identity in present:
                base = identity + ".sqlite3"
                sizes[identity] = sum(info.st_size if info is not None else 0
                    for suffix in ("", "-wal", "-shm")
                    for info in [_check_file(fd, base + suffix, required=suffix == "")])
            return {**pointer, "databaseCount": len(present), "physicalBytes": sum(sizes.values()),
                    "databaseBytes": sizes, "orphans": sorted(set(present) - known),
                    "cleanupNeeded": len(present) > 1}

    def active_uuid(self) -> str:
        with self._lease() as fd:
            return self._current(fd)["active"]

    @contextmanager
    def _archive_source(self, fd: int, pointer: dict[str, Any], db_uuid: str) -> Iterator[ImportantLogStore]:
        if db_uuid not in pointer["archives"]:
            raise ImportantLogStoreError("archive identity is not catalogued")
        base = db_uuid + ".sqlite3"
        wal = _check_file(fd, base + "-wal", required=False)
        shm = _check_file(fd, base + "-shm", required=False)
        sealed = pointer["archiveChecks"].get(db_uuid) == "complete" and (wal is None or wal.st_size == 0)
        if not sealed and (wal is None or shm is None):
            raise ImportantLogStoreError("uncheckpointed archive sidecars are unavailable")
        with ImportantLogStore.open(self.directory, db_uuid, readonly=True,
                                    immutable=sealed) as store:
            yield store

    def health_snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Detailed cached-fact view; metadata/proof/request usage is explicit unknown."""
        now = now or datetime.now(timezone.utc)
        with self._lease() as fd:
            pointer = self._current(fd)
            with ImportantLogStore.open(self.directory, pointer["active"], _catalog_owned=True) as store:
                maintenance = ImportantLogMaintenance(store, target_bytes=self.target_bytes,
                                                     low_water_bytes=self.low_water_bytes)
                health = store.health_snapshot()
                row = store._connection.execute("""SELECT last_quick_check,last_maintenance,last_result
                    FROM important_maintenance_v1 WHERE id=1""").fetchone()
                health["lastQuickCheck"] = row[0]
                health["lastMaintenance"] = row[2] if row[1] is not None else None
                health["cleanupCandidates"] = maintenance.candidate_count(
                    now=now, pressure=health["logicalLiveBytes"] >= self.target_bytes or
                    maintenance.pressure_active())
                health["lastWrite"] = self.last_status.get("state") if self.last_status["state"] != "unknown" else None
                health["lastLock"] = "held"  # this snapshot owns the lease
                from workbench.contracts.logs_v1 import parse_health_snapshot
                return {"active": parse_health_snapshot(health), "generation": pointer["generation"],
                        "archiveCount": len(pointer["archives"]),
                        "targetBytes": self.target_bytes, "lowWaterBytes": self.low_water_bytes,
                        "operatingMetadata": {"outsideBudget": True,
                                              "metadataBytes": None, "proofBytes": None,
                                              "requestJournalBytes": None, "status": "unknown"}}

    def _rotate(self, fd: int, pointer: dict[str, Any], old: ImportantLogStore) -> dict[str, Any]:
        if _read_control(fd, _INTENT) is not None:
            raise ImportantLogStoreError("unresolved rollover intent")
        try:
            row = old._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            checkpoint = "busy" if row[0] else "complete"
        except sqlite3.DatabaseError:
            checkpoint = "error"
        if checkpoint == "complete":
            wal = _check_file(fd, old.path.name + "-wal", required=False)
            if wal is not None and wal.st_size != 0:
                checkpoint = "busy"
        with ImportantLogStore.create(self.directory) as fresh:
            identity = fresh.db_uuid
            if fresh._connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ImportantLogStoreError("new active failed quick_check")
        intent = {"version": 1, "from": old.db_uuid, "to": identity,
                  "generation": pointer["generation"] + 1, "checkpoint": checkpoint}
        _write_control(fd, _INTENT, intent)
        newer = {"version": 1, "generation": intent["generation"], "active": identity,
                 "archives": [*pointer["archives"], old.db_uuid],
                 "archiveChecks": {**pointer["archiveChecks"], old.db_uuid: checkpoint}}
        _write_control(fd, _POINTER, newer)
        _unlink_control(fd, _INTENT)
        return {"from": old.db_uuid, "to": identity, "checkpoint": checkpoint}

    def _after_write(self, fd: int, pointer: dict[str, Any], store: ImportantLogStore,
                     *, now: datetime) -> dict[str, Any]:
        maintenance = ImportantLogMaintenance(store, target_bytes=self.target_bytes,
                                             low_water_bytes=self.low_water_bytes)
        post = store.health_snapshot(include_subject_stats=False)
        pressure = post["logicalLiveBytes"] >= self.target_bytes or maintenance.pressure_active()
        sweep = maintenance.prune_pass(now=now, pressure=pressure)
        rollover = None
        if pressure and sweep["logicalLiveBytes"] >= self.target_bytes and not sweep["remaining"]:
            rollover = self._rotate(fd, pointer, store)
        return {"post": store.health_snapshot(include_subject_stats=False),
                "prune": sweep, "rollover": rollover}

    def append(self, subject: SubjectRef, kind: str, sources: list[SourceRange], body: dict,
               *, now: datetime | None = None, source_gaps: list[dict] | None = None) -> dict[str, Any]:
        """One result and at most one rollover for a new write."""
        now = now or datetime.now(timezone.utc)
        item = parse_subject_ref(subject)
        committed_entry: str | None = None
        try:
            with self._lease() as fd:
                pointer = self._current(fd)
                with ImportantLogStore.open(self.directory, pointer["active"], _catalog_owned=True) as store:
                    maintenance = ImportantLogMaintenance(store, target_bytes=self.target_bytes,
                                                         low_water_bytes=self.low_water_bytes)
                    check = maintenance.quick_check(now=now)
                    if check["status"] not in ("ok", "cached"):
                        raise ImportantLogStoreError("active quick_check incomplete or corrupt")
                    pre = store.health_snapshot(include_subject_stats=False)
                    if pre["diskFreeBytes"] < _MIN_FREE_BYTES + _MAX_TRANSACTION_BYTES:
                        self.last_status = {"state": "physical_free", "pre": pre}
                        return self.last_status
                    entry = store.make_entry(item, kind, sources, body, source_gaps=source_gaps)
                    store.put_entry(entry)
                    committed_entry = entry["entryId"]
                    self.last_status = {"state": "ok", "entry": entry, "pre": pre,
                                        **self._after_write(fd, pointer, store, now=now)}
                    return self.last_status
        except (OSError, sqlite3.DatabaseError, ImportantLogStoreError) as exc:
            self.last_status = {"state": _storage_reason(exc), "error": type(exc).__name__,
                                "durableEntryId": committed_entry,
                                "rolloverStateUnknown": committed_entry is not None}
            return self.last_status

    def recovery_probe(self, *, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        current = time.monotonic()
        if not force and current - self._last_probe_monotonic < 30:
            return {"state": "deferred", "previous": self.last_status}
        self._last_probe_monotonic = current
        try:
            with self._lease() as fd:
                pointer = self._current(fd)
                with ImportantLogStore.open(self.directory, pointer["active"], _catalog_owned=True) as store:
                    check = ImportantLogMaintenance(store).quick_check(now=now, force=True)
                    if check["status"] != "ok":
                        return {"state": check["status"], "check": check}
                    store._connection.execute("BEGIN IMMEDIATE")
                    store._connection.execute("ROLLBACK")
                    health = store.health_snapshot()
                    if health["diskFreeBytes"] < _MIN_FREE_BYTES + _MAX_TRANSACTION_BYTES:
                        return {"state": "physical_free", "check": check}
                    self.last_status = {"state": "healthy", "check": check}
                    return self.last_status
        except (OSError, sqlite3.DatabaseError, ImportantLogStoreError) as exc:
            return {"state": _storage_reason(exc), "error": type(exc).__name__}

    def maintenance_step(self, *, now: datetime | None = None) -> dict[str, Any]:
        """One lower-priority bounded pass; a later tick can continue low-water work."""
        now = now or datetime.now(timezone.utc)
        try:
            with self._lease() as fd:
                pointer = self._current(fd)
                with ImportantLogStore.open(self.directory, pointer["active"],
                                            _catalog_owned=True) as store:
                    maintenance = ImportantLogMaintenance(store, target_bytes=self.target_bytes,
                                                         low_water_bytes=self.low_water_bytes)
                    check = maintenance.quick_check(now=now)
                    if check["status"] not in ("ok", "cached"):
                        return {"state": "integrity_unknown", "quickCheck": check}
                    pressure = (maintenance.logical_live_bytes() >= self.target_bytes
                                or maintenance.pressure_active())
                    work = maintenance.maintenance_pass(now=now, pressure=pressure)
                    sweep = work["prune"]
                    rollover = None
                    if pressure and sweep["logicalLiveBytes"] >= self.target_bytes and not sweep["remaining"]:
                        rollover = self._rotate(fd, pointer, store)
                    checkpoint = work["checkpoint"]
                    state = ("busy" if checkpoint.get("busy") else
                             "maintenance_incomplete" if (checkpoint.get("status") == "incomplete" or
                                                          work["shrink"].get("status") == "incomplete")
                             else "ok")
                    return {"state": state, "quickCheck": check, "work": work,
                            "rollover": rollover}
        except (OSError, sqlite3.DatabaseError, ImportantLogStoreError) as exc:
            return {"state": _storage_reason(exc), "error": type(exc).__name__}

    def _newest_archival_state(self, fd: int, pointer: dict[str, Any],
                               source: ImportantLogStore, target: ImportantLogStore,
                               kind: str, subject_id: str) -> str | None:
        """Follow the same active-then-newest-archive order as ActiveSubjectLog._archived_row."""
        def state(store: ImportantLogStore) -> str | None:
            if not self._has_table(store, "subject_lifecycle_v1"):
                return None
            row = store._connection.execute("""SELECT archival_state FROM subject_lifecycle_v1
                WHERE subject_kind=? AND subject_id=?""", (kind, subject_id)).fetchone()
            if row is None:
                return None
            if row[0] not in ("open", "closed"):
                raise ImportantLogStoreError("subject archival state is unavailable")
            return row[0]

        newest = state(target)
        if newest is not None:
            return newest
        for identity in reversed(pointer["archives"]):
            if identity == source.db_uuid:
                newest = state(source)
            else:
                with self._archive_source(fd, pointer, identity) as archive:
                    newest = state(archive)
            if newest is not None:
                return newest
        return None

    def _anchor_ids(self, fd: int, pointer: dict[str, Any], source: ImportantLogStore,
                    target: ImportantLogStore, *, after: str = "", limit: int = 100,
                    scan_budget: int = 100_000) -> tuple[list[str], int]:
        """Protect source-open and newest-known-open start/cumulative anchors."""
        source_state = self._has_table(source, "subject_lifecycle_v1")
        newest_cache: dict[tuple[str, str], str | None] = {}
        cursor, scanned, protected = after, 0, []
        while len(protected) < limit:
            if scanned >= scan_budget:
                raise ImportantLogStoreError("protected anchor scan bound")
            batch_limit = min(100, scan_budget - scanned)
            if source_state:
                rows = source._connection.execute("""SELECT e.entry_id,e.subject_kind,e.subject_id,
                    s.archival_state FROM entries e LEFT JOIN subject_lifecycle_v1 s
                    ON e.subject_kind=s.subject_kind AND e.subject_id=s.subject_id
                    WHERE e.kind IN ('start','cumulative') AND e.entry_id>?
                    ORDER BY e.entry_id LIMIT ?""", (cursor, batch_limit)).fetchall()
            else:
                rows = source._connection.execute("""SELECT entry_id,subject_kind,subject_id,
                    'open' AS archival_state FROM entries WHERE kind IN ('start','cumulative')
                    AND entry_id>? ORDER BY entry_id LIMIT ?""",
                    (cursor, batch_limit)).fetchall()
            if not rows:
                return protected, scanned
            for row in rows:
                scanned += 1
                cursor = row["entry_id"]
                if row["archival_state"] != "closed":
                    protected.append(cursor)
                    if len(protected) == limit:
                        return protected, scanned
                    continue
                subject = (row["subject_kind"], row["subject_id"])
                if subject not in newest_cache:
                    newest_cache[subject] = self._newest_archival_state(
                        fd, pointer, source, target, *subject)
                if newest_cache[subject] != "closed":
                    protected.append(cursor)
                    if len(protected) == limit:
                        return protected, scanned
            if len(rows) < batch_limit:
                return protected, scanned
        return protected, scanned

    def protected_anchor_ids(self, db_uuid: str, *, after: str = "", limit: int = 100) -> list[str]:
        """Conservative, paged deletion gate for open start/cumulative anchors."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("anchor page size must be 1..100")
        db_uuid = _canonical_uuid(db_uuid)
        with self._lease() as fd:
            pointer = self._current(fd)
            if db_uuid == pointer["active"] or db_uuid not in pointer["archives"]:
                raise ImportantLogStoreError("only a catalog archive can be considered for deletion")
            with self._archive_source(fd, pointer, db_uuid) as source, \
                 ImportantLogStore.open(self.directory, pointer["active"], readonly=True) as target:
                return self._anchor_ids(fd, pointer, source, target, after=after, limit=limit)[0]

    def _carry_verification(self, fd: int, pointer: dict[str, Any], source: ImportantLogStore,
                            target: ImportantLogStore) -> dict[str, Any]:
        after = ""
        checked = 0
        scanned = 0
        while checked < 100_000:
            if scanned >= 100_000:
                break
            try:
                page, examined = self._anchor_ids(fd, pointer, source, target, after=after,
                                                   scan_budget=100_000 - scanned)
            except ImportantLogStoreError as exc:
                if str(exc) != "protected anchor scan bound":
                    raise
                break
            scanned += examined
            if not page:
                return {"ready": True, "checked": checked, "missing": 0}
            for entry_id in page:
                if target.get_entry(entry_id) != source.get_entry(entry_id):
                    return {"ready": False, "checked": checked, "missing": 1}
            checked += len(page)
            after = page[-1]
        return {"ready": False, "checked": checked, "missing": None,
                "reason": "verification_bound"}

    @staticmethod
    def _has_table(store: ImportantLogStore, name: str) -> bool:
        return store._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    @staticmethod
    def _copy_metadata_row(target: ImportantLogStore, table: str, row: sqlite3.Row,
                           key_name: str) -> None:
        columns = tuple(row.keys())
        existing = target._connection.execute(
            f"SELECT * FROM {table} WHERE {key_name}=?", (row[key_name],)).fetchone()
        if existing is None:
            target._connection.execute(
                f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(row[column] for column in columns))
        elif table == "subject_commands_v1":
            if any(existing[key] != row[key] for key in ("command_id", "task_id", "revision", "started_at")):
                raise ImportantLogStoreError("carried command identity conflicts with active DB")
        elif dict(existing) != dict(row):
            raise ImportantLogStoreError("carried event identity conflicts with active DB")

    def _ensure_carry_subject_schema(self, active_uuid: str) -> None:
        """Create existing lifecycle tables only for an explicit, leased carry."""
        with ImportantLogStore.open(self.directory, active_uuid, _catalog_owned=True) as target:
            missing = any(not self._has_table(target, table) for table in
                          ("subject_lifecycle_v1", "subject_events_v1", "subject_commands_v1"))
        if missing:
            SubjectLifecycle(self.directory, active_uuid, _catalog_owned=True)

    def carry_event_keys(self, db_uuid: str, *, after: str = "") -> dict[str, Any]:
        """Carry one 100-row immutable event-key page, not archived log bodies."""
        db_uuid = _canonical_uuid(db_uuid)
        with self._lease() as fd:
            pointer = self._current(fd)
            if db_uuid not in pointer["archives"]:
                raise ImportantLogStoreError("only a catalog archive can be carried")
            with self._archive_source(fd, pointer, db_uuid) as source:
                if not self._has_table(source, "subject_events_v1"):
                    return {"nextAfter": None, "target": pointer["active"], "copied": 0}
                self._ensure_carry_subject_schema(pointer["active"])
                with ImportantLogStore.open(self.directory, pointer["active"], _catalog_owned=True) as target:
                    rows = source._connection.execute(
                        "SELECT * FROM subject_events_v1 WHERE event_key>? ORDER BY event_key LIMIT 100", (after,)
                    ).fetchall()
                    with target.transaction():
                        for row in rows:
                            self._copy_metadata_row(target, "subject_events_v1", row, "event_key")
                    return {"nextAfter": rows[-1]["event_key"] if len(rows) == 100 else None,
                            "target": target.db_uuid, "copied": len(rows)}

    def carry_open_subject_state(self, db_uuid: str) -> dict[str, Any]:
        """Carry bounded state/command rows needed after deleting an open subject's source."""
        db_uuid = _canonical_uuid(db_uuid)
        with self._lease() as fd:
            pointer = self._current(fd)
            if db_uuid not in pointer["archives"]:
                raise ImportantLogStoreError("only a catalog archive can be carried")
            with self._archive_source(fd, pointer, db_uuid) as source:
                if not self._has_table(source, "subject_lifecycle_v1"):
                    return {"ready": True, "target": pointer["active"], "copied": 0}
                self._ensure_carry_subject_schema(pointer["active"])
                with ImportantLogStore.open(self.directory, pointer["active"], _catalog_owned=True) as target:
                    states = source._connection.execute(
                        "SELECT * FROM subject_lifecycle_v1 WHERE archival_state='open' LIMIT 101").fetchall()
                    if len(states) > 100:
                        return {"ready": False, "target": target.db_uuid,
                                "reason": "open_subject_carry_bound"}
                    commands: list[sqlite3.Row] = []
                    if self._has_table(source, "subject_commands_v1"):
                        for row in states:
                            predicate, key = (("task_id=?", row["subject_id"]) if row["subject_kind"] == "task"
                                              else ("command_id=?", row["subject_id"]))
                            commands.extend(source._connection.execute(
                                f"SELECT * FROM subject_commands_v1 WHERE {predicate} LIMIT 101", (key,)).fetchall())
                            if len(commands) > 100:
                                return {"ready": False, "target": target.db_uuid,
                                        "reason": "open_command_carry_bound"}
                    with target.transaction():
                        for row in states:
                            existing = target._connection.execute(
                                "SELECT 1 FROM subject_lifecycle_v1 WHERE subject_kind=? AND subject_id=?",
                                (row["subject_kind"], row["subject_id"])).fetchone()
                            if existing is None:
                                columns = tuple(row.keys())
                                target._connection.execute(
                                    f"INSERT INTO subject_lifecycle_v1({','.join(columns)}) "
                                    f"VALUES({','.join('?' for _ in columns)})",
                                    tuple(row[column] for column in columns))
                        for row in commands:
                            self._copy_metadata_row(target, "subject_commands_v1", row, "command_id")
                    return {"ready": True, "target": target.db_uuid, "copied": len(states) + len(commands)}

    def _metadata_verification(self, source: ImportantLogStore,
                               target: ImportantLogStore) -> dict[str, Any]:
        if self._has_table(source, "subject_events_v1"):
            if not self._has_table(target, "subject_events_v1"):
                return {"ready": False, "reason": "active_subject_schema_missing"}
            after = ""
            for _ in range(1000):
                rows = source._connection.execute(
                    "SELECT * FROM subject_events_v1 WHERE event_key>? ORDER BY event_key LIMIT 100", (after,)
                ).fetchall()
                for row in rows:
                    found = target._connection.execute(
                        "SELECT * FROM subject_events_v1 WHERE event_key=?", (row["event_key"],)).fetchone()
                    if found is None or dict(found) != dict(row):
                        return {"ready": False, "reason": "event_key_not_carried",
                                "eventKey": row["event_key"]}
                if len(rows) < 100:
                    break
                after = rows[-1]["event_key"]
            else:
                return {"ready": False, "reason": "event_verification_bound"}
        if self._has_table(source, "subject_lifecycle_v1"):
            if not self._has_table(target, "subject_lifecycle_v1"):
                return {"ready": False, "reason": "active_subject_schema_missing"}
            states = source._connection.execute(
                "SELECT * FROM subject_lifecycle_v1 WHERE archival_state='open' LIMIT 101").fetchall()
            if len(states) > 100:
                return {"ready": False, "reason": "open_subject_verification_bound"}
            for row in states:
                found = target._connection.execute(
                    "SELECT * FROM subject_lifecycle_v1 WHERE subject_kind=? AND subject_id=?",
                    (row["subject_kind"], row["subject_id"])).fetchone()
                if found is None:
                    return {"ready": False, "reason": "open_subject_not_carried",
                            "subject": {"kind": row["subject_kind"], "id": row["subject_id"]}}
                if self._has_table(source, "subject_commands_v1"):
                    predicate, key = (("task_id=?", row["subject_id"]) if row["subject_kind"] == "task"
                                      else ("command_id=?", row["subject_id"]))
                    commands = source._connection.execute(
                        f"SELECT * FROM subject_commands_v1 WHERE {predicate} LIMIT 101", (key,)).fetchall()
                    if len(commands) > 100:
                        return {"ready": False, "reason": "open_command_verification_bound"}
                    if commands and not self._has_table(target, "subject_commands_v1"):
                        return {"ready": False, "reason": "active_subject_schema_missing"}
                    for command in commands:
                        current = target._connection.execute(
                            "SELECT * FROM subject_commands_v1 WHERE command_id=?",
                            (command["command_id"],)).fetchone()
                        if current is None or any(current[column] != command[column] for column in
                                                  ("command_id", "task_id", "revision", "started_at")):
                            return {"ready": False, "reason": "open_command_not_carried",
                                    "commandId": command["command_id"]}
        return {"ready": True}

    def deletion_protection_status(self, db_uuid: str) -> dict[str, Any]:
        """Read-only carry check; never grants deletion based only on a caller cursor."""
        db_uuid = _canonical_uuid(db_uuid)
        with self._lease() as fd:
            pointer = self._current(fd)
            if db_uuid == pointer["active"] or db_uuid not in pointer["archives"]:
                raise ImportantLogStoreError("only an archive can be considered for deletion")
            with self._archive_source(fd, pointer, db_uuid) as source, \
                 ImportantLogStore.open(self.directory, pointer["active"], readonly=True) as target:
                return self._carry_verification(fd, pointer, source, target)

    def carry_protected_anchors(self, db_uuid: str, *, max_rows: int = 100,
                                after: str = "") -> dict[str, Any]:
        """Explicit pre-deletion carry. Never deletes; caller must recheck before any deletion."""
        if type(max_rows) is not int or not 1 <= max_rows <= 100:
            raise ValueError("bounded carry must be 1..100")
        db_uuid = _canonical_uuid(db_uuid)
        with self._lease() as fd:
            pointer = self._current(fd)
            if db_uuid == pointer["active"] or db_uuid not in pointer["archives"]:
                raise ImportantLogStoreError("only an archive can be carried")
            with self._archive_source(fd, pointer, db_uuid) as source, \
                 ImportantLogStore.open(self.directory, pointer["active"], _catalog_owned=True) as target:
                ids, _ = self._anchor_ids(fd, pointer, source, target, after=after, limit=max_rows)
                copied = 0
                for entry_id in ids:
                    if target.get_entry(entry_id) is None:
                        if (target.health_snapshot(include_subject_stats=False)["logicalLiveBytes"]
                                + source.get_entry(entry_id)["storedBytes"] >= self.target_bytes):
                            break
                        target.import_verified_entry(source, entry_id)
                        copied += 1
                verified = self._carry_verification(fd, pointer, source, target)
                return {"source": db_uuid, "target": target.db_uuid, "copied": copied,
                        "nextAfter": ids[-1] if ids else None,
                        "remaining": verified["missing"], "safeToDelete": verified["ready"]}

    def delete_archive(self, db_uuid: str) -> dict[str, Any]:
        """Explicit user deletion of one UUID, with durable partial-deletion recovery.

        The caller can run bounded carry pages first. This final check and
        intent are under one lease; a failed check leaves every file untouched.
        """
        db_uuid = _canonical_uuid(db_uuid)
        with self._lease() as fd:
            pointer = self._current(fd)
            if db_uuid == pointer["active"] or db_uuid not in pointer["archives"]:
                raise ImportantLogStoreError("only a catalog archive can be deleted")
            with self._archive_source(fd, pointer, db_uuid) as source, \
                 ImportantLogStore.open(self.directory, pointer["active"], readonly=True) as target:
                protection = self._carry_verification(fd, pointer, source, target)
                metadata = self._metadata_verification(source, target)
            if not protection["ready"] or not metadata["ready"]:
                return {"state": "protected", "dbUuid": db_uuid, "protection": protection,
                        "metadata": metadata}
            before = self._disk_uuids(fd)
            if db_uuid not in before:
                raise ImportantLogStoreError("selected archive is missing")
            for name in self._archive_files(fd, db_uuid):
                _check_file(fd, name, required=name.endswith(".sqlite3"))
            intent = {"version": 1, "dbUuid": db_uuid, "active": pointer["active"],
                      "generation": pointer["generation"]}
            _write_control(fd, _DELETE_INTENT, intent)
            pointer = self._finish_delete(fd, pointer, intent)
            remaining = self._disk_uuids(fd)
            physical = sum(
                info.st_size for identity in remaining for suffix in ("", "-wal", "-shm")
                if (info := _check_file(fd, identity + ".sqlite3" + suffix, required=suffix == "")) is not None)
            return {"state": "deleted", "dbUuid": db_uuid, "generation": pointer["generation"],
                    "databaseCount": len(remaining), "physicalBytes": physical,
                    "cleanupNeeded": len(remaining) > 1, "remainingDbUuids": remaining,
                    "protection": protection, "metadata": metadata}

    def prepare_and_delete_archive(self, db_uuid: str) -> dict[str, Any]:
        """Carry bounded pages of protected entries, then recheck and delete.

        Each carry page has its own lease and transaction. The final check in
        ``delete_archive`` is authoritative; a capacity or proof failure keeps
        the selected archive catalogued.
        """
        db_uuid = _canonical_uuid(db_uuid)
        event_after = ""
        event_target = None
        for _ in range(1000):
            events = self.carry_event_keys(db_uuid, after=event_after)
            if events.get("reason"):
                return {"state": "protected", "dbUuid": db_uuid, "reason": events["reason"]}
            if event_target is not None and events["target"] != event_target:
                return {"state": "protected", "dbUuid": db_uuid,
                        "reason": "active_changed_during_carry"}
            event_target = events["target"]
            if not events["nextAfter"]:
                break
            event_after = events["nextAfter"]
        else:
            return {"state": "protected", "dbUuid": db_uuid, "reason": "event_carry_bound"}
        state = self.carry_open_subject_state(db_uuid)
        if not state["ready"]:
            return {"state": "protected", "dbUuid": db_uuid, "reason": state["reason"]}
        if event_target is not None and state["target"] != event_target:
            return {"state": "protected", "dbUuid": db_uuid,
                    "reason": "active_changed_during_carry"}
        after = ""
        for _ in range(1000):  # the verification port itself is bounded at 100,000 anchors
            status = self.deletion_protection_status(db_uuid)
            if status["ready"]:
                return self.delete_archive(db_uuid)
            carried = self.carry_protected_anchors(db_uuid, after=after)
            if carried["safeToDelete"]:
                return self.delete_archive(db_uuid)
            if not carried["nextAfter"]:
                return {"state": "protected", "dbUuid": db_uuid, "protection": status,
                        "reason": "protected_anchor_not_carried"}
            after = carried["nextAfter"]
        return {"state": "protected", "dbUuid": db_uuid,
                "reason": "bounded_carry_exhausted"}
