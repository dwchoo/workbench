"""Catalogued important-log reads with short active and sealed archive pages.

SQLite's ``mode=ro`` may update the active SHM reader marks or create sidecars.
Uncheckpointed archive bytes are copied and verified under the catalog lease.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Iterator

from workbench.contracts.logs_v1 import parse_subject_ref
from .catalog import _read_control, _pointer
from .store import ImportantLogStore, ImportantLogStoreError, _canonical_uuid, _check_file, _directory_fd


def _reason(exc: BaseException) -> str:
    """Keep a contended catalog distinct from other read failures."""
    if isinstance(exc, sqlite3.OperationalError) and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY:
        return "busy"
    if "busy" in str(exc).lower() or "locked" in str(exc).lower():
        return "busy"
    return "unavailable"


def _missing(identity: str | None, exc: BaseException) -> dict[str, str | None]:
    return {"dbUuid": identity, "reason": _reason(exc)}


def _page_token(subject: dict[str, Any], generation: int, entry: dict[str, Any]) -> str:
    value = {"v": 1, "kind": subject["kind"], "subjectId": subject["id"],
             "generation": generation, "recordedAt": entry["recordedAt"],
             "entryId": entry["entryId"]}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_page_token(token: str, subject: dict[str, Any]) -> tuple[tuple[str, str], int]:
    if type(token) is not str or not 1 <= len(token) <= 512:
        raise ValueError("invalid page cursor")
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
        if (type(data) is not dict
                or set(data) != {"v", "kind", "subjectId", "generation", "recordedAt", "entryId"}
                or type(data["v"]) is not int or data["v"] != 1
                or data["kind"] != subject["kind"]
                or data["subjectId"] != subject["id"]
                or type(data["generation"]) is not int or data["generation"] < 1
                or type(data["recordedAt"]) is not str
                or not data["recordedAt"].endswith("Z")
                or len(data["recordedAt"]) > 40
                or type(data["entryId"]) is not str):
            raise ValueError("invalid page cursor")
        _canonical_uuid(data["entryId"])
        if datetime.fromisoformat(data["recordedAt"].replace("Z", "+00:00")).utcoffset().total_seconds() != 0:
            raise ValueError("invalid page cursor")
        if _page_token(subject, data["generation"], data) != token:
            raise ValueError("invalid page cursor")
        return (data["recordedAt"], data["entryId"]), data["generation"]
    except (ValueError, TypeError, KeyError, UnicodeError, ImportantLogStoreError) as exc:
        raise ValueError("invalid page cursor") from exc


class ImportantLogReader:
    """Read active and archived UUIDs without changing their files."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    @contextmanager
    def _catalog(self) -> Iterator[tuple[int, dict[str, Any], list[str], dict[str, int]]]:
        fd = _directory_fd(self.directory, create=False)
        lock = None
        try:
            info = _check_file(fd, "maintenance.lock", required=True)
            lock = os.open("maintenance.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            opened = os.fstat(lock)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ImportantLogStoreError("important reader lease changed")
            deadline = time.monotonic() + 0.1
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ImportantLogStoreError("important catalog busy")
                    time.sleep(0.01)
            pointer = _read_control(fd, "active.json")
            if (pointer is None or _read_control(fd, "rollover.intent.json") is not None
                    or _read_control(fd, "delete.intent.json") is not None):
                raise ImportantLogStoreError("important catalog identity unavailable")
            pointer = _pointer(pointer)
            present: list[str] = []
            sizes: dict[str, int] = {}
            for name in os.listdir(fd):
                if not name.endswith(".sqlite3"):
                    continue
                identity = _canonical_uuid(name[:-8])
                base = identity + ".sqlite3"
                parts = [_check_file(fd, base + suffix, required=suffix == "")
                         for suffix in ("", "-wal", "-shm")]
                sizes[identity] = sum(part.st_size for part in parts if part is not None)
                present.append(identity)
            present.sort()
            if pointer["active"] not in present or any(x not in present for x in pointer["archives"]):
                raise ImportantLogStoreError("important catalog DB missing")
            yield fd, pointer, present, sizes
        finally:
            if lock is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
                os.close(lock)
            os.close(fd)

    @staticmethod
    def _copy_file(fd: int, name: str, destination: Path) -> str:
        expected = _check_file(fd, name, required=True)
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        try:
            target = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        except BaseException:
            os.close(source)
            raise
        digest = sha256()
        try:
            opened = os.fstat(source)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (expected.st_dev, expected.st_ino, expected.st_size):
                raise ImportantLogStoreError("important file changed before snapshot")
            while chunk := os.read(source, 1024 * 1024):
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(target, view):]
            if os.fstat(source).st_size != expected.st_size:
                raise ImportantLogStoreError("important file changed during snapshot")
        finally:
            os.close(source)
            os.close(target)
        return digest.hexdigest()

    @staticmethod
    def _verify_file(fd: int, name: str, expected_hash: str) -> None:
        _check_file(fd, name, required=True)
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        try:
            digest = sha256()
            while chunk := os.read(source, 1024 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != expected_hash:
                raise ImportantLogStoreError("important file changed during read snapshot")
        finally:
            os.close(source)

    @contextmanager
    def _view(self, identity: str) -> Iterator[ImportantLogStore]:
        identity = _canonical_uuid(identity)
        with self._catalog() as (fd, pointer, _present, _sizes):
            if identity not in {pointer["active"], *pointer["archives"]}:
                raise ImportantLogStoreError("important DB is not catalogued")
            if identity == pointer["active"]:
                # The SQLite connection stays inside the EX lease, including
                # the caller's single page query and close. Never use immutable.
                with ImportantLogStore.open(self.directory, identity, readonly=True) as store:
                    yield store
                return
            base = identity + ".sqlite3"
            wal = _check_file(fd, base + "-wal", required=False)
            shm = _check_file(fd, base + "-shm", required=False)
            sealed = (pointer["archiveChecks"].get(identity) == "complete"
                      and (wal is None or wal.st_size == 0))
            if sealed:
                # The catalog EX protects archive registration and the entire
                # immutable connection lifetime, including the caller's page.
                before = set(os.listdir(fd))
                protected = (base, base + "-wal", base + "-shm")

                def file_state() -> tuple[tuple[int, int, int, int, int] | None, ...]:
                    parts = (_check_file(fd, name, required=name == base) for name in protected)
                    return tuple(None if part is None else
                                 (part.st_dev, part.st_ino, part.st_size,
                                  part.st_mtime_ns, part.st_ctime_ns) for part in parts)

                before_state = file_state()
                try:
                    with ImportantLogStore.open(self.directory, identity, readonly=True,
                                                immutable=True) as store:
                        yield store
                finally:
                    if set(os.listdir(fd)) != before or file_state() != before_state:
                        raise ImportantLogStoreError("important catalog files changed during sealed read")
                return
            if wal is None or shm is None:
                raise ImportantLogStoreError("uncheckpointed archive sidecars unavailable")
            with tempfile.TemporaryDirectory(prefix="workbench-important-read-") as scratch:
                private = Path(scratch)
                before = set(os.listdir(fd))
                names = [base] + [base + suffix for suffix, part in (("-wal", wal), ("-shm", shm)) if part]
                hashes = {name: self._copy_file(fd, name, private / name) for name in names}
                for name, value in hashes.items():
                    self._verify_file(fd, name, value)
                if set(os.listdir(fd)) != before:
                    raise ImportantLogStoreError("important catalog files changed during snapshot")
                with ImportantLogStore.open(private, identity, readonly=True) as store:
                    yield store

    def status(self) -> dict[str, Any]:
        try:
            with self._catalog() as (_fd, pointer, present, sizes):
                known = {pointer["active"], *pointer["archives"]}
                return {"active": pointer["active"], "generation": pointer["generation"],
                        "databaseCount": len(present), "physicalBytes": sum(sizes.values()),
                        "cleanupNeeded": len(present) > 1,
                        "orphans": sorted(set(present) - known),
                        "databases": [{"dbUuid": identity,
                                       "role": "active" if identity == pointer["active"] else
                                               "archive" if identity in pointer["archives"] else "orphan",
                                       "physicalBytes": sizes[identity],
                                       "availability": "unknown" if identity in known else "unavailable",
                                       "lastChecked": None}
                                      for identity in present],
                        "unavailable": [], "complete": True}
        except (OSError, sqlite3.Error, ImportantLogStoreError) as exc:
            return {"active": None, "generation": None, "databases": [],
                    "unavailable": [_missing(None, exc)], "complete": False}

    def _end_generation(self, start: int, unavailable: list[dict[str, str | None]]) -> int | None:
        try:
            with self._catalog() as (_fd, pointer, _present, _sizes):
                end = pointer["generation"]
        except (OSError, sqlite3.Error, ImportantLogStoreError) as exc:
            unavailable.append(_missing(None, exc))
            return None
        if end != start:
            unavailable.append({"dbUuid": None, "reason": "generation_changed"})
        return end

    def list_subjects(self, *, limit: int = 50, after: str | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        cursor = None
        if after is not None:
            if type(after) is not str or after.count(":") != 1:
                raise ValueError("invalid page cursor")
            kind, subject_id = after.split(":")
            if kind not in ("command", "task"):
                raise ValueError("invalid page cursor")
            try:
                _canonical_uuid(subject_id)
            except ImportantLogStoreError as exc:
                raise ValueError("invalid page cursor") from exc
            cursor = (kind, subject_id)
        try:
            with self._catalog() as (_fd, pointer, _present, _sizes):
                identities = [pointer["active"], *pointer["archives"]]
                start = pointer["generation"]
        except (OSError, sqlite3.Error, ImportantLogStoreError) as exc:
            return {"subjects": [], "nextAfter": None,
                    "startGeneration": None, "endGeneration": None,
                    "unavailable": [_missing(None, exc)], "complete": False}
        combined: dict[tuple[str, str], dict[str, Any]] = {}
        unavailable: list[dict[str, str | None]] = []
        positions = {identity: cursor for identity in identities}
        pending = set(identities)
        while pending and len(combined) <= limit:
            for identity in identities:
                if identity not in pending:
                    continue
                try:
                    with self._view(identity) as store:
                        arguments: tuple[Any, ...] = () if positions[identity] is None else positions[identity]
                        predicate = "" if not arguments else "WHERE (subject_kind,subject_id)>(?,?)"
                        rows = store._connection.execute(
                            "SELECT subject_kind,subject_id,entry_count FROM subject_stats "
                            + predicate + " ORDER BY subject_kind,subject_id LIMIT ?",
                            (*arguments, limit + 1)).fetchall()
                    if rows:
                        positions[identity] = (rows[-1][0], rows[-1][1])
                    if len(rows) < limit + 1:
                        pending.remove(identity)
                    for row in rows:
                        key = (row[0], row[1])
                        found = combined.setdefault(key, {"subject": {"schemaVersion": 1, "kind": key[0],
                                                                      "id": key[1]}, "entryCount": 0,
                                                          "sourceDbUuids": []})
                        found["entryCount"] += row[2]
                        found["sourceDbUuids"].append(identity)
                except (OSError, sqlite3.Error, ImportantLogStoreError) as exc:
                    unavailable.append(_missing(identity, exc))
                    pending.remove(identity)
        ordered = sorted(combined)
        page = ordered[:limit]
        end = self._end_generation(start, unavailable)
        return {"subjects": [combined[key] for key in page],
                "nextAfter": ":".join(page[-1]) if len(ordered) > limit else None,
                "startGeneration": start, "endGeneration": end,
                "unavailable": unavailable, "complete": not unavailable}

    def show(self, subject: dict[str, Any], *, limit: int = 50,
             after: str | None = None) -> dict[str, Any]:
        item = parse_subject_ref(subject)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        cursor, token_generation = (None, None) if after is None else _decode_page_token(after, item)
        try:
            with self._catalog() as (_fd, pointer, _present, _sizes):
                identities = [pointer["active"], *pointer["archives"]]
                start = pointer["generation"]
        except (OSError, sqlite3.Error, ImportantLogStoreError) as exc:
            return {"subject": item, "entries": [], "nextAfter": None,
                    "startGeneration": None, "endGeneration": None,
                    "unavailable": [_missing(None, exc)], "complete": False}
        entries: dict[str, dict[str, Any]] = {}
        unavailable: list[dict[str, str | None]] = []
        if token_generation is not None and token_generation != start:
            unavailable.append({"dbUuid": None, "reason": "generation_changed"})
        positions = {identity: cursor for identity in identities}
        pending = set(identities)
        while pending and len(entries) <= limit:
            for identity in identities:
                if identity not in pending:
                    continue
                try:
                    with self._view(identity) as store:
                        rows = store._list_entries_keyset(
                            item, limit=limit + 1, after_key=positions[identity])
                    if rows:
                        positions[identity] = (rows[-1]["recordedAt"], rows[-1]["entryId"])
                    if len(rows) < limit + 1:
                        pending.remove(identity)
                    for entry in rows:
                        key = entry["entryId"]
                        if key in entries:
                            if entries[key]["entry"] != entry:
                                raise ImportantLogStoreError("duplicate entry identity differs")
                            entries[key]["sourceDbUuids"].append(identity)
                        else:
                            entries[key] = {"entry": entry, "sourceDbUuids": [identity]}
                except (OSError, sqlite3.Error, ImportantLogStoreError) as exc:
                    unavailable.append(_missing(identity, exc))
                    pending.remove(identity)
        ordered = sorted(entries.values(), key=lambda value: (value["entry"]["recordedAt"],
                                                            value["entry"]["entryId"]))
        page = ordered[:limit]
        end = self._end_generation(start, unavailable)
        return {"subject": item, "entries": page,
                "nextAfter": _page_token(item, start, page[-1]["entry"])
                if len(ordered) > limit else None,
                "startGeneration": start, "endGeneration": end,
                "unavailable": unavailable, "complete": not unavailable}
