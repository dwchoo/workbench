"""Private UUID SQLite store for important log records."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import time
from hashlib import sha256
from urllib.parse import quote
from uuid import UUID, uuid4
import zlib

from workbench.contracts.logs_v1 import (
    MAX_BODY_BYTES, SCHEMA_VERSION, ImportantEntry, LogContractError,
    SubjectRef, SourceRange, canonical_json, decode_json, parse_important_body,
    parse_important_entry, parse_source_range, parse_subject_ref,
)
from .control import ControlError, parse_control, validate_pointer

_STORE_SCHEMA_VERSION = 1
_READ_COLUMNS = """entry_id,subject_kind,subject_id,kind,recorded_at,
    substr(sources_json,1,1000001) AS sources_json,
    length(sources_json) AS sources_size,
    substr(source_gaps_json,1,1000001) AS source_gaps_json,
    length(source_gaps_json) AS gaps_size,
    body_codec,decoded_bytes,stored_bytes,body_sha256,
    substr(body_payload,1,1000001) AS body_payload,
    length(body_payload) AS payload_size,origin_db_uuid"""
_SCHEMA = """
CREATE TABLE db_meta (id INTEGER PRIMARY KEY CHECK(id=1), db_uuid TEXT NOT NULL);
CREATE TABLE entries (
    entry_id TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('task','command')),
    subject_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('start','event','cumulative','intermediate','gap','closure')),
    recorded_at TEXT NOT NULL,
    sources_json BLOB NOT NULL,
    source_gaps_json BLOB NOT NULL,
    body_codec TEXT NOT NULL CHECK(body_codec IN ('identity-v1','zlib-v1')),
    decoded_bytes INTEGER NOT NULL CHECK(decoded_bytes > 0 AND decoded_bytes <= 1000000),
    stored_bytes INTEGER NOT NULL CHECK(stored_bytes > 0 AND stored_bytes <= 1000000),
    body_sha256 TEXT NOT NULL,
    body_payload BLOB NOT NULL,
    origin_db_uuid TEXT NOT NULL
);
CREATE INDEX entries_by_subject ON entries(subject_kind, subject_id, recorded_at, entry_id);
CREATE TABLE subject_stats (
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    entry_count INTEGER NOT NULL CHECK(entry_count >= 0),
    stored_bytes INTEGER NOT NULL CHECK(stored_bytes >= 0),
    PRIMARY KEY(subject_kind, subject_id)
);
"""


class ImportantLogStoreError(RuntimeError):
    """Private path, schema, codec, or storage integrity failed."""


def _canonical_uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError(value)
    except ValueError as exc:
        raise ImportantLogStoreError("database UUID must be canonical") from exc
    return value


def _owned_private_dir(info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ImportantLogStoreError("important-log directory must be owned mode 0700")


def _owned_private_file(info: os.stat_result) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise ImportantLogStoreError("important-log file must be owned single-link mode 0600")


def _directory_fd(path: Path, *, create: bool) -> int:
    raw = os.fspath(path)
    if not os.path.isabs(raw) or os.path.normpath(raw) != raw or raw == "/":
        raise ImportantLogStoreError("important-log directory must be a normalized dedicated absolute path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for component in Path(raw).parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=fd)
                child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        _owned_private_dir(os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_file(dir_fd: int, name: str, *, required: bool) -> os.stat_result | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        if required:
            raise ImportantLogStoreError(f"missing important-log file: {name}")
        return None
    try:
        info = os.fstat(fd)
        _owned_private_file(info)
        return info
    finally:
        os.close(fd)


def _assert_active_writer(dir_fd: int, identity: str) -> None:
    """A catalog pointer, when present, prohibits writes to every other UUID."""
    if _check_file(dir_fd, "rollover.intent.json", required=False) is not None:
        raise ImportantLogStoreError("rollover identity is pending recovery")
    info = _check_file(dir_fd, "active.json", required=False)
    if info is None:
        if (_check_file(dir_fd, "maintenance.lock", required=False) is not None
                or _check_file(dir_fd, "rollover.intent.json", required=False) is not None):
            raise ImportantLogStoreError("active identity missing from catalog")
        return  # explicit pre-catalog v1 stores remain valid
    if info.st_size > 1_000_000:
        raise ImportantLogStoreError("active pointer is oversized")
    descriptor = os.open("active.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        if (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) != (info.st_dev, info.st_ino):
            raise ImportantLogStoreError("active pointer changed during open")
        raw = os.read(descriptor, 1_000_001)
    finally:
        os.close(descriptor)
    try:
        pointer = validate_pointer(parse_control(raw))
    except ControlError as exc:
        raise ImportantLogStoreError("active identity unavailable") from exc
    if pointer["active"] != identity:
        raise ImportantLogStoreError("archived important database is read-only")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class EncodedBody:
    codec: str
    payload: bytes
    decoded_bytes: int
    stored_bytes: int
    checksum: str


def encode_body(body: object) -> EncodedBody:
    data = canonical_json(parse_important_body(body))
    if len(data) > MAX_BODY_BYTES:
        raise ImportantLogStoreError("important body exceeds decoded bound")
    zipped = zlib.compress(data) if len(data) >= 4096 else b""
    if zipped and len(zipped) * 8 <= len(data) * 7:
        return EncodedBody("zlib-v1", zipped, len(data), len(zipped), sha256(data).hexdigest())
    return EncodedBody("identity-v1", data, len(data), len(data), sha256(data).hexdigest())


def decode_body(codec: str, payload: bytes, decoded_bytes: int, checksum: str) -> dict:
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_BODY_BYTES:
        raise ImportantLogStoreError("stored body exceeds bound")
    if not isinstance(decoded_bytes, int) or isinstance(decoded_bytes, bool) or not 0 < decoded_bytes <= MAX_BODY_BYTES:
        raise ImportantLogStoreError("decoded body size is invalid")
    if codec == "identity-v1":
        data = payload
    elif codec == "zlib-v1":
        inflater = zlib.decompressobj()
        try:
            data = inflater.decompress(payload, decoded_bytes + 1)
            if len(data) > decoded_bytes or inflater.unconsumed_tail or not inflater.eof or inflater.unused_data:
                raise ImportantLogStoreError("compressed body exceeds bound or has trailing data")
            data += inflater.flush(decoded_bytes + 1 - len(data))
        except zlib.error as exc:
            raise ImportantLogStoreError("invalid compressed body") from exc
    else:
        raise ImportantLogStoreError("unknown important body codec")
    if len(data) != decoded_bytes or sha256(data).hexdigest() != checksum:
        raise ImportantLogStoreError("important body length/checksum mismatch")
    body = decode_json(data, max_bytes=MAX_BODY_BYTES)
    return parse_important_body(body)


def decode_entry_row(row: sqlite3.Row) -> ImportantEntry:
    if (row["sources_size"] > MAX_BODY_BYTES or row["gaps_size"] > MAX_BODY_BYTES
            or row["payload_size"] > MAX_BODY_BYTES or row["payload_size"] != row["stored_bytes"]):
        raise ImportantLogStoreError("important entry has oversized or inconsistent stored columns")
    body = decode_body(row["body_codec"], row["body_payload"], row["decoded_bytes"], row["body_sha256"])
    sources = decode_json(row["sources_json"], max_bytes=MAX_BODY_BYTES)
    gaps = decode_json(row["source_gaps_json"], max_bytes=MAX_BODY_BYTES)
    return parse_important_entry({
        "schemaVersion": SCHEMA_VERSION, "entryId": row["entry_id"],
        "subject": {"schemaVersion": SCHEMA_VERSION, "kind": row["subject_kind"], "id": row["subject_id"]},
        "kind": row["kind"], "sources": sources, "sourceGaps": gaps, "recordedAt": row["recorded_at"],
        "body": body, "bodyCodec": row["body_codec"], "decodedBytes": row["decoded_bytes"],
        "storedBytes": row["stored_bytes"], "bodySha256": row["body_sha256"],
        "originDbUuid": row["origin_db_uuid"],
    })


class ImportantLogStore:
    """One explicit UUID database. Catalog policy is a separate owner."""

    def __init__(self, directory: Path, db_uuid: str, *, create: bool = False, readonly: bool = False,
                 immutable: bool = False, _catalog_owned: bool = False):
        if (create and readonly) or (immutable and not readonly):
            raise ValueError("cannot create a read-only important database")
        self.directory = Path(directory)
        self.db_uuid = _canonical_uuid(db_uuid)
        self.path = self.directory / f"{self.db_uuid}.sqlite3"
        self.readonly = readonly
        self._closed = True
        self._last_write: str | None = None
        self._last_quick_check: str | None = None
        self._lease_fd: int | None = None
        fd = _directory_fd(self.directory, create=create)
        try:
            name = self.path.name
            if create:
                descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
                os.close(descriptor)
            original = _check_file(fd, name, required=True)
            sidecars_before = {suffix: _check_file(fd, name + suffix, required=False)
                               for suffix in ("-wal", "-shm")}
            if not readonly and not create:
                if not _catalog_owned and _check_file(fd, "active.json", required=False) is not None:
                    lock_info = _check_file(fd, "maintenance.lock", required=True)
                    lease = os.open("maintenance.lock", os.O_RDONLY | os.O_NONBLOCK |
                                    os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                    opened = os.fstat(lease)
                    if (opened.st_dev, opened.st_ino) != (lock_info.st_dev, lock_info.st_ino):
                        os.close(lease)
                        raise ImportantLogStoreError("maintenance lease changed during writer open")
                    self._lease_fd = lease
                    deadline = time.monotonic() + 0.1
                    while True:
                        try:
                            fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise sqlite3.OperationalError("important writer lease busy")
                            time.sleep(0.01)
                _assert_active_writer(fd, self.db_uuid)
            uri = (f"file:{quote(str(self.path), safe='/')}?mode={'ro' if readonly else 'rw'}"
                   + ("&immutable=1" if immutable else ""))
            connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0.1)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=100")
                if create:
                    connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA synchronous=FULL")
                    connection.executescript(
                        "BEGIN IMMEDIATE;" + _SCHEMA
                        + f"INSERT INTO db_meta(id,db_uuid) VALUES(1,'{self.db_uuid}');"
                        + f"PRAGMA user_version={_STORE_SCHEMA_VERSION};COMMIT;"
                    )
                if readonly:
                    connection.execute("PRAGMA query_only=ON")
                if connection.execute("PRAGMA user_version").fetchone()[0] != _STORE_SCHEMA_VERSION:
                    raise ImportantLogStoreError("unsupported important store schema")
                row = connection.execute("SELECT db_uuid FROM db_meta WHERE id=1").fetchone()
                if row is None or row[0] != self.db_uuid:
                    raise ImportantLogStoreError("important database UUID mismatch")
                if not immutable and connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                    raise ImportantLogStoreError("important database is not WAL")
                if connection.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                    raise ImportantLogStoreError("important database is not INCREMENTAL")
                if not readonly and connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                    connection.execute("PRAGMA synchronous=FULL")
                if create:
                    result = connection.execute("PRAGMA quick_check").fetchone()[0]
                    if result != "ok":
                        raise ImportantLogStoreError(f"initial quick_check failed: {result}")
                    self._last_quick_check = _now()
                current = _check_file(fd, name, required=True)
                if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                    raise ImportantLogStoreError("important database path changed during open")
                for suffix in ("-wal", "-shm"):
                    side = name + suffix
                    if create or (not readonly and sidecars_before[suffix] is None):
                        try:
                            os.chmod(side, 0o600, dir_fd=fd, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                    _check_file(fd, side, required=False)
            except BaseException:
                connection.close()
                raise
            self._connection = connection
            self._closed = False
        except BaseException:
            if self._lease_fd is not None:
                fcntl.flock(self._lease_fd, fcntl.LOCK_UN)
                os.close(self._lease_fd)
                self._lease_fd = None
            raise
        finally:
            os.close(fd)

    @classmethod
    def create(cls, directory: Path, *, db_uuid: str | None = None) -> ImportantLogStore:
        return cls(directory, db_uuid or str(uuid4()), create=True)

    @classmethod
    def open(cls, directory: Path, db_uuid: str, *, readonly: bool = False,
             immutable: bool = False, _catalog_owned: bool = False) -> ImportantLogStore:
        return cls(directory, db_uuid, readonly=readonly, immutable=immutable,
                   _catalog_owned=_catalog_owned)

    def __enter__(self) -> ImportantLogStore:
        self._require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True
        if self._lease_fd is not None:
            fcntl.flock(self._lease_fd, fcntl.LOCK_UN)
            os.close(self._lease_fd)
            self._lease_fd = None

    def _require_open(self, *, write: bool = False) -> None:
        if self._closed:
            raise ImportantLogStoreError("important store is closed")
        if write and self.readonly:
            raise ImportantLogStoreError("important store is read-only")

    @contextmanager
    def transaction(self):
        """한 source event의 entry, subject facts, cursor를 함께 확정한다."""
        self._require_open(write=True)
        if self._connection.in_transaction:
            raise ImportantLogStoreError("nested important transaction is not supported")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.execute("COMMIT")
            if self._last_write == "pending":
                self._last_write = "ok"
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if self._last_write == "pending":
                self._last_write = "error:transaction_rollback"
            raise

    def make_entry(self, subject: SubjectRef, kind: str, sources: list[SourceRange], body: dict, *, source_gaps: list[dict] | None = None, entry_id: str | None = None, recorded_at: str | None = None) -> ImportantEntry:
        self._require_open(write=True)
        encoded = encode_body(body)
        entry = {
            "schemaVersion": SCHEMA_VERSION, "entryId": entry_id or str(uuid4()),
            "subject": parse_subject_ref(subject), "kind": kind,
            "sources": [parse_source_range(item) for item in sources],
            "sourceGaps": source_gaps or [],
            "recordedAt": recorded_at or _now(), "body": body,
            "bodyCodec": encoded.codec, "decodedBytes": encoded.decoded_bytes,
            "storedBytes": encoded.stored_bytes, "bodySha256": encoded.checksum,
            "originDbUuid": self.db_uuid,
        }
        return parse_important_entry(entry)

    def put_entry(self, entry: ImportantEntry) -> ImportantEntry:
        return self._put_entry(entry)

    def import_verified_entry(self, source: ImportantLogStore, entry_id: str) -> ImportantEntry:
        """Carry one checksum-verified protected anchor without changing provenance."""
        self._require_open(write=True)
        source._require_open()
        if source.directory != self.directory or source.db_uuid == self.db_uuid:
            raise ImportantLogStoreError("anchor source must be another DB in the same catalog")
        entry = source.get_entry(entry_id)
        if entry is None or entry["originDbUuid"] != source.db_uuid:
            raise ImportantLogStoreError("verified source anchor is unavailable")
        current = self.get_entry(entry_id)
        if current is not None:
            if current != entry:
                raise ImportantLogStoreError("carried anchor identity changed")
            return current
        return self._put_entry(entry, verified_source=source.db_uuid)

    def _put_entry(self, entry: ImportantEntry, *, verified_source: str | None = None) -> ImportantEntry:
        self._require_open(write=True)
        item = parse_important_entry(entry)
        if item["originDbUuid"] != (verified_source or self.db_uuid):
            raise ImportantLogStoreError("foreign originDbUuid requires a separately validated import path")
        encoded = encode_body(item["body"])
        if (item["bodyCodec"], item["decodedBytes"], item["storedBytes"], item["bodySha256"]) != (encoded.codec, encoded.decoded_bytes, encoded.stored_bytes, encoded.checksum):
            raise ImportantLogStoreError("entry codec metadata disagrees with body")
        subject = item["subject"]
        outer_transaction = self._connection.in_transaction
        # SAVEPOINT는 독립 put에서 commit하고, lifecycle의 외부 transaction 안에서는
        # entry/stat 쓰기만 확정 대기시킨다.
        self._connection.execute("SAVEPOINT important_entry_v1")
        try:
            self._connection.execute(
                """INSERT INTO entries(entry_id,subject_kind,subject_id,kind,recorded_at,sources_json,source_gaps_json,
                   body_codec,decoded_bytes,stored_bytes,body_sha256,body_payload,origin_db_uuid)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item["entryId"], subject["kind"], subject["id"], item["kind"], item["recordedAt"],
                 canonical_json(item["sources"]), canonical_json(item["sourceGaps"]), encoded.codec, encoded.decoded_bytes,
                 encoded.stored_bytes, encoded.checksum, encoded.payload, item["originDbUuid"]),
            )
            self._connection.execute(
                """INSERT INTO subject_stats(subject_kind,subject_id,entry_count,stored_bytes)
                   VALUES(?,?,1,?) ON CONFLICT(subject_kind,subject_id) DO UPDATE SET
                   entry_count=entry_count+1, stored_bytes=stored_bytes+excluded.stored_bytes""",
                (subject["kind"], subject["id"], encoded.stored_bytes),
            )
            self._connection.execute("RELEASE SAVEPOINT important_entry_v1")
            self._last_write = "pending" if outer_transaction else "ok"
        except BaseException as exc:
            self._last_write = f"error:{type(exc).__name__}"
            # SQLITE_FULL may abort the entire transaction, removing the
            # savepoint. Cleanup must not replace the original SQLite code.
            if self._connection.in_transaction:
                try:
                    self._connection.execute("ROLLBACK TO SAVEPOINT important_entry_v1")
                    self._connection.execute("RELEASE SAVEPOINT important_entry_v1")
                except sqlite3.DatabaseError as cleanup_error:
                    exc.add_note(f"savepoint cleanup: {type(cleanup_error).__name__}")
            raise
        return item

    def _entry_from_row(self, row: sqlite3.Row) -> ImportantEntry:
        return decode_entry_row(row)

    def get_entry(self, entry_id: str) -> ImportantEntry | None:
        self._require_open()
        _canonical_uuid(entry_id)
        row = self._connection.execute(f"SELECT {_READ_COLUMNS} FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
        return None if row is None else self._entry_from_row(row)

    def list_entries(self, subject: SubjectRef, *, limit: int = 100, after: str | None = None) -> list[ImportantEntry]:
        self._require_open()
        item = parse_subject_ref(subject)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise LogContractError("limit must be 1..100")
        if after is not None:
            _canonical_uuid(after)
            cursor = self._connection.execute("SELECT recorded_at,entry_id FROM entries WHERE entry_id=?", (after,)).fetchone()
            if cursor is None:
                raise ImportantLogStoreError("unknown page cursor")
            rows = self._connection.execute(
                f"""SELECT {_READ_COLUMNS} FROM entries WHERE subject_kind=? AND subject_id=?
                   AND (recorded_at,entry_id)>(?,?) ORDER BY recorded_at,entry_id LIMIT ?""",
                (item["kind"], item["id"], cursor[0], cursor[1], limit),
            ).fetchall()
        else:
            rows = self._connection.execute(
                f"SELECT {_READ_COLUMNS} FROM entries WHERE subject_kind=? AND subject_id=? ORDER BY recorded_at,entry_id LIMIT ?",
                (item["kind"], item["id"], limit),
            ).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def _list_entries_keyset(self, subject: SubjectRef, *, limit: int,
                             after_key: tuple[str, str] | None = None) -> list[ImportantEntry]:
        """One indexed page for the catalog reader; cursor carries the sort key."""
        self._require_open()
        item = parse_subject_ref(subject)
        if type(limit) is not int or not 1 <= limit <= 101:
            raise LogContractError("reader page limit must be 1..101")
        if after_key is not None:
            if (type(after_key) is not tuple or len(after_key) != 2
                    or type(after_key[0]) is not str):
                raise LogContractError("invalid reader keyset")
            _canonical_uuid(after_key[1])
            rows = self._connection.execute(
                f"""SELECT {_READ_COLUMNS} FROM entries WHERE subject_kind=? AND subject_id=?
                    AND (recorded_at,entry_id)>(?,?) ORDER BY recorded_at,entry_id LIMIT ?""",
                (item["kind"], item["id"], after_key[0], after_key[1], limit),
            ).fetchall()
        else:
            rows = self._connection.execute(
                f"""SELECT {_READ_COLUMNS} FROM entries WHERE subject_kind=? AND subject_id=?
                    ORDER BY recorded_at,entry_id LIMIT ?""",
                (item["kind"], item["id"], limit),
            ).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def health_snapshot(self, *, include_subject_stats: bool = True) -> dict:
        self._require_open()
        page_size = self._connection.execute("PRAGMA page_size").fetchone()[0]
        pages = self._connection.execute("PRAGMA page_count").fetchone()[0]
        freelist = self._connection.execute("PRAGMA freelist_count").fetchone()[0]
        fd = _directory_fd(self.directory, create=False)
        try:
            sizes = []
            for name in (self.path.name, self.path.name + "-wal", self.path.name + "-shm"):
                info = _check_file(fd, name, required=name == self.path.name)
                sizes.append(info.st_size if info else 0)
            statvfs = os.fstatvfs(fd)
        finally:
            os.close(fd)
        rows = (self._connection.execute("SELECT * FROM subject_stats ORDER BY subject_kind,subject_id LIMIT 1025").fetchall()
                if include_subject_stats else [])
        stats = [{"schemaVersion": SCHEMA_VERSION, "subject": {"schemaVersion": SCHEMA_VERSION, "kind": row["subject_kind"], "id": row["subject_id"]}, "entryCount": row["entry_count"], "storedBytes": row["stored_bytes"]} for row in rows[:1024]]
        from workbench.contracts.logs_v1 import parse_health_snapshot
        return parse_health_snapshot({
            "schemaVersion": SCHEMA_VERSION, "dbUuid": self.db_uuid, "measuredAt": _now(),
            "stale": not include_subject_stats or len(rows) > 1024,
            "mainBytes": sizes[0], "walBytes": sizes[1],
            "shmBytes": sizes[2], "totalBytes": sum(sizes),
            "logicalLiveBytes": (pages - freelist) * page_size, "freelistBytes": freelist * page_size,
            "subjectStats": stats, "cleanupCandidates": None, "lastWrite": self._last_write,
            "lastLock": None, "lastMaintenance": None,
            "diskFreeBytes": statvfs.f_bavail * statvfs.f_frsize,
            "lastQuickCheck": self._last_quick_check, "metadataOutsideBudget": True,
        })
