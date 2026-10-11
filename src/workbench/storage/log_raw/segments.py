"""Opt-in, source-framed raw segments. Nothing constructs this store by default.

The control file contains offsets and loss accounting, never raw body bytes. A
write intent is durable before a frame; recovery treats every absent byte in
that intent as missing rather than replaying or inventing output.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import sys
from threading import RLock
from typing import Any, Iterator

from workbench.contracts.logs_v1 import (
    MAX_SAFE_INTEGER, SourceRef, canonical_json, parse_source_ref,
)


SEGMENT_LIMIT = 100_000_000
SEGMENT_COUNT = 5
MAX_RECORD_PAYLOAD = 64 * 1024
MAX_CONTROL_BYTES = 1_000_000
MAX_SOURCES = 1024
MAX_LOSS_RANGES = 256
_MAGIC = b"WBRAW2\0\0"
_FILE_HEADER = struct.Struct(">8sQ")
_FRAME_HEADER = struct.Struct(">4sHII")  # magic, metadata length, payload length, CRC32
_FRAME_MAGIC = b"FRM2"
_SEGMENT = re.compile(r"seg-([0-9]{16})\.raw\Z")
_TEMP = re.compile(r"\.control-[0-9a-f]{32}\.tmp\Z")
_QUARANTINE = re.compile(r"\.quarantine-([0-9a-f]{32})\.raw\Z")
_MARKER = ".workbench-segmented-raw-v1"
_CONTROL = ".control.json"
_LOCK = ".segment.lock"
_AT_EMPTY_PATH = 0x1000  # Linux linkat: publish a prepared O_TMPFILE inode.
_RENAME_NOREPLACE = 1


class SegmentIntegrityError(RuntimeError):
    """Owned raw tree, framing, or control data is unsafe."""


def _private_file(info: os.stat_result) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise SegmentIntegrityError("raw file must be owned, private and single-link")


def _private_dir(info: os.stat_result) -> None:
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise SegmentIntegrityError("raw directory must be owned and private")


def _root_fd(path: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in path.split("/")[1:]:
            if not part:
                continue
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        _private_dir(os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


def _owned_open(root: int, name: str, flags: int, *, create: bool = False) -> int:
    flags |= os.O_NOFOLLOW | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    before = None
    try:
        before = os.stat(name, dir_fd=root, follow_symlinks=False)
        _private_file(before)
    except FileNotFoundError:
        if not create:
            raise
    fd = os.open(name, flags, 0o600, dir_fd=root)
    try:
        now = os.fstat(fd)
        _private_file(now)
        if before is not None and (before.st_dev, before.st_ino) != (now.st_dev, now.st_ino):
            raise SegmentIntegrityError("raw file identity changed")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_all(fd: int, data: bytes | memoryview) -> None:
    view = memoryview(data)
    done = 0
    while done < len(view):
        count = os.write(fd, view[done:])
        if count <= 0:
            raise OSError("raw write made no progress")
        done += count


def _prepare_linux_segment(root: int, seq: int) -> tuple[int, os.stat_result]:
    """Build a full header in an invisible Linux inode; caller closes the fd."""
    if not sys.platform.startswith("linux") or not hasattr(os, "O_TMPFILE"):
        raise OSError(errno.EOPNOTSUPP, "unnamed raw segments require Linux O_TMPFILE")
    fd = os.open(".", os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600, dir_fd=root)
    try:
        identity = os.fstat(fd)
        if (not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 0
                or identity.st_uid != os.geteuid()
                or stat.S_IMODE(identity.st_mode) & 0o077):
            raise SegmentIntegrityError("prepared raw segment is not private")
        _write_all(fd, _FILE_HEADER.pack(_MAGIC, seq))
        os.fsync(fd)
        return fd, identity
    except BaseException:
        os.close(fd)
        raise


def _publish_prepared(fd: int, root: int, name: str) -> None:
    """Linux linkat atomically publishes only the complete prepared inode."""
    if not sys.platform.startswith("linux"):
        raise OSError(errno.EOPNOTSUPP, "atomic raw publication requires Linux linkat")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        linkat = libc.linkat
    except AttributeError as exc:
        raise OSError(errno.EOPNOTSUPP, "Linux linkat unavailable") from exc
    linkat.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_int)
    linkat.restype = ctypes.c_int
    if linkat(fd, b"", root, name.encode("ascii"), _AT_EMPTY_PATH) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), name)


def _capture_no_replace(root: int, source: str, quarantine: str) -> None:
    """Move the actual public entry into an unused private name atomically."""
    if not sys.platform.startswith("linux"):
        raise OSError(errno.EOPNOTSUPP, "quarantine capture requires Linux renameat2")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise OSError(errno.EOPNOTSUPP, "Linux renameat2 unavailable") from exc
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(root, source.encode("ascii"), root, quarantine.encode("ascii"),
                 _RENAME_NOREPLACE) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), quarantine)


def _fingerprint(fd: int, limit: int) -> str:
    info = os.fstat(fd)
    _private_file(info)
    if not _FILE_HEADER.size <= info.st_size <= limit:
        raise SegmentIntegrityError("victim size outside cap")
    digest = sha256()
    offset = 0
    while offset < info.st_size:
        block = os.pread(fd, min(1024 * 1024, info.st_size - offset), offset)
        if not block:
            raise SegmentIntegrityError("victim changed during fingerprint")
        digest.update(block)
        offset += len(block)
    if os.fstat(fd).st_size != info.st_size:
        raise SegmentIntegrityError("victim size changed during fingerprint")
    return digest.hexdigest()


def _source_key(source: SourceRef) -> str:
    return sha256(canonical_json(parse_source_ref(source))).hexdigest()


def _timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        raise ValueError("observed_at must be bounded UTC ISO-8601")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("observed_at must be UTC")
    return value


def _segment_name(seq: int) -> str:
    if not 1 <= seq <= 9_999_999_999_999_999:
        raise SegmentIntegrityError("segment sequence exhausted")
    return f"seg-{seq:016d}.raw"


class SegmentedRawStore:
    """Single writer for a dedicated root; readers use its synchronized scan.

    Application wiring is deliberately separate. Append returns a storage
    result and does not control a task, terminal, observation or model call.
    """

    def __init__(self, root: str | os.PathLike[str], *, segment_limit: int = SEGMENT_LIMIT,
                 segment_count: int = SEGMENT_COUNT):
        if type(segment_limit) is not int or segment_limit < 256 or segment_limit > SEGMENT_LIMIT:
            raise ValueError("segment_limit must be between 256 and 100000000")
        if type(segment_count) is not int or not 1 <= segment_count <= SEGMENT_COUNT:
            raise ValueError("segment_count must be between 1 and 5")
        path = os.fspath(root)
        if not isinstance(path, str) or not path or ".." in Path(path).parts:
            raise ValueError("root must be a dedicated path without parent traversal")
        self.root_path = os.path.abspath(path)
        if self.root_path == "/":
            raise ValueError("root must be a dedicated directory")
        self.segment_limit = segment_limit
        self.segment_count = segment_count
        self._mutex = RLock()
        self._root: int | None = None
        self._lock: int | None = None
        self._control: dict[str, Any] = {}
        self._faulted = False
        try:
            self._root = _root_fd(self.root_path)
            self._initialize()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> SegmentedRawStore:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._mutex:
            if self._lock is not None:
                os.close(self._lock)
                self._lock = None
            if self._root is not None:
                os.close(self._root)
                self._root = None

    def _ensure_open(self) -> int:
        if self._root is None or self._lock is None:
            raise RuntimeError("SegmentedRawStore is closed")
        held = os.fstat(self._root)
        _private_dir(held)
        try:
            named = os.stat(self.root_path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise SegmentIntegrityError("raw root path disappeared") from exc
        if (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino):
            raise SegmentIntegrityError("raw root path was replaced")
        try:
            lock_name = os.stat(_LOCK, dir_fd=self._root, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise SegmentIntegrityError("raw singleton lock disappeared") from exc
        _private_file(lock_name)
        lock_held = os.fstat(self._lock)
        if (lock_name.st_dev, lock_name.st_ino) != (lock_held.st_dev, lock_held.st_ino):
            raise SegmentIntegrityError("raw singleton lock was replaced")
        return self._root

    def _names(self) -> list[int]:
        root = self._ensure_open()
        seqs: list[int] = []
        for name in os.listdir(root):
            if name in {_MARKER, _CONTROL, _LOCK}:
                _private_file(os.stat(name, dir_fd=root, follow_symlinks=False))
            elif _TEMP.fullmatch(name):
                _private_file(os.stat(name, dir_fd=root, follow_symlinks=False))
                os.unlink(name, dir_fd=root)  # own interrupted atomic control update
            elif _QUARANTINE.fullmatch(name):
                _private_file(os.stat(name, dir_fd=root, follow_symlinks=False))
                pending = self._control.get("eviction")
                if pending is None or name != self._quarantine_name(pending):
                    raise SegmentIntegrityError("unclaimed raw quarantine")
            else:
                match = _SEGMENT.fullmatch(name)
                if match is None or int(match.group(1)) < 1:
                    raise SegmentIntegrityError("unexpected file in dedicated raw root")
                _private_file(os.stat(name, dir_fd=root, follow_symlinks=False))
                seqs.append(int(match.group(1)))
        if self._control:
            live = self._control["live"]
            creating = self._control["creating"]
            eviction = self._control["eviction"]
            allowed = {int(seq) for seq in live}
            if creating is not None:
                allowed.add(creating["seq"])
            if not set(seqs) <= allowed:
                raise SegmentIntegrityError("public raw name lacks durable authority")
            for seq in seqs:
                expected = live.get(str(seq))
                if expected is None and creating is not None and seq == creating["seq"]:
                    expected = [creating["dev"], creating["ino"]]
                info = os.stat(_segment_name(seq), dir_fd=root, follow_symlinks=False)
                if [info.st_dev, info.st_ino] != expected:
                    raise SegmentIntegrityError("public raw identity differs from durable authority")
            missing = {int(seq) for seq in live} - set(seqs)
            if missing and (eviction is None or missing != {eviction["seq"]}):
                raise SegmentIntegrityError("durable live segment disappeared")
        return sorted(seqs)

    def _initialize(self) -> None:
        root = self._root
        assert root is not None
        before = set(os.listdir(root))
        if _MARKER not in before and before:
            raise SegmentIntegrityError("unmarked raw root is not empty")
        if _MARKER not in before:
            fd = _owned_open(root, _MARKER, os.O_WRONLY, create=True)
            try:
                _write_all(fd, b"workbench-segmented-raw-v1\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(root)
        fd = _owned_open(root, _MARKER, os.O_RDONLY)
        try:
            if os.read(fd, 128) != b"workbench-segmented-raw-v1\n":
                raise SegmentIntegrityError("raw root marker mismatch")
        finally:
            os.close(fd)
        try:
            self._lock = _owned_open(root, _LOCK, os.O_RDWR, create=True)
        except FileExistsError:
            self._lock = _owned_open(root, _LOCK, os.O_RDWR)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SegmentIntegrityError("raw root already has a writer") from exc
        if _CONTROL in os.listdir(root):
            fd = _owned_open(root, _CONTROL, os.O_RDONLY)
            try:
                raw = os.read(fd, MAX_CONTROL_BYTES + 1)
            finally:
                os.close(fd)
            self._control = self._parse_control(raw)
            if (self._control["limit"] != self.segment_limit
                    or self._control["count"] != self.segment_count):
                raise SegmentIntegrityError("raw cap differs from durable control")
        else:
            if any(name != _MARKER and name != _LOCK for name in os.listdir(root)):
                raise SegmentIntegrityError("segments exist without control")
            self._control = {"version": 1, "limit": self.segment_limit,
                             "count": self.segment_count, "next": 1,
                             "active": None, "sources": {}, "intent": None,
                             "eviction": None, "creating": None, "live": {}}
            self._save()
        seqs = self._names()
        if len(seqs) > self.segment_count:
            raise SegmentIntegrityError("segment count exceeds configured cap")
        self._recover(seqs)

    def _parse_control(self, raw: bytes) -> dict[str, Any]:
        if len(raw) > MAX_CONTROL_BYTES:
            raise SegmentIntegrityError("control exceeds bound")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SegmentIntegrityError("invalid raw control") from exc
        try:
            if canonical_json(data) != raw:
                raise SegmentIntegrityError("raw control is not canonical")
        except ValueError as exc:
            raise SegmentIntegrityError("invalid raw control") from exc
        if (type(data) is not dict or set(data) != {"version", "limit", "count", "next", "active", "sources", "intent", "eviction", "creating", "live"}
                or data["version"] != 1 or type(data["limit"]) is not int
                or type(data["count"]) is not int or type(data["next"]) is not int
                or not 1 <= data["next"] <= 9_999_999_999_999_999
                or (data["active"] is not None and (type(data["active"]) is not int or data["active"] < 1))
                or type(data["sources"]) is not dict or len(data["sources"]) > MAX_SOURCES
                or type(data["live"]) is not dict or len(data["live"]) > SEGMENT_COUNT):
            raise SegmentIntegrityError("raw control schema mismatch")
        for seq, identity in data["live"].items():
            if (type(seq) is not str or not seq.isdecimal() or str(int(seq)) != seq
                    or int(seq) < 1 or type(identity) is not list or len(identity) != 2
                    or any(type(number) is not int or not 1 <= number <= MAX_SAFE_INTEGER
                           for number in identity)):
                raise SegmentIntegrityError("invalid live segment identity")
        for key, entry in data["sources"].items():
            if (type(key) is not str or type(entry) is not dict
                    or set(entry) != {"source", "high", "evicted", "evicted_bytes",
                                      "storage_gap_bytes", "storage_gaps", "unknown_before"}):
                raise SegmentIntegrityError("invalid source control")
            source = parse_source_ref(entry["source"])
            if (key != _source_key(source) or type(entry["high"]) is not int
                    or type(entry["evicted"]) is not int or not 0 <= entry["evicted"] <= entry["high"] <= MAX_SAFE_INTEGER):
                raise SegmentIntegrityError("source control mismatch")
            if (any(type(entry[field]) is not int or not 0 <= entry[field] <= entry["high"]
                    for field in ("evicted_bytes", "storage_gap_bytes", "unknown_before"))
                    or type(entry["storage_gaps"]) is not list or len(entry["storage_gaps"]) > MAX_LOSS_RANGES):
                raise SegmentIntegrityError("invalid source loss accounting")
            for span in entry["storage_gaps"]:
                if (type(span) is not list or len(span) != 2 or any(type(v) is not int for v in span)
                        or not 0 <= span[0] < span[1] <= entry["high"]):
                    raise SegmentIntegrityError("invalid source storage gap")
        intent = data["intent"]
        if intent is not None:
            if (type(intent) is not dict or set(intent) != {"key", "start", "end"}
                    or intent["key"] not in data["sources"] or type(intent["start"]) is not int
                    or type(intent["end"]) is not int or not 0 <= intent["start"] < intent["end"] <= MAX_SAFE_INTEGER
                    or data["sources"][intent["key"]]["high"] != intent["end"]):
                raise SegmentIntegrityError("invalid raw write intent")
        eviction = data["eviction"]
        if eviction is not None:
            if (type(eviction) is not dict or set(eviction) != {"seq", "sources", "dev", "ino", "digest", "nonce", "phase"}
                    or type(eviction["seq"]) is not int or eviction["seq"] < 1
                    or type(eviction["sources"]) is not dict
                    or len(eviction["sources"]) > MAX_SOURCES
                    or any(type(eviction[field]) is not int or not 1 <= eviction[field] <= MAX_SAFE_INTEGER
                           for field in ("dev", "ino"))
                    or type(eviction["digest"]) is not str or re.fullmatch(r"[0-9a-f]{64}", eviction["digest"]) is None
                    or type(eviction["nonce"]) is not str or re.fullmatch(r"[0-9a-f]{32}", eviction["nonce"]) is None
                    or eviction["phase"] not in {"planned", "captured", "armed"}
                    or data["live"].get(str(eviction["seq"])) != [eviction["dev"], eviction["ino"]]):
                raise SegmentIntegrityError("invalid rotation intent")
            for key, value in eviction["sources"].items():
                if (key not in data["sources"] or type(value) is not list or len(value) != 2
                        or any(type(v) is not int or v < 0 for v in value)):
                    raise SegmentIntegrityError("invalid rotation source")
        creating = data["creating"]
        if creating is not None:
            if (type(creating) is not dict or set(creating) != {"seq", "dev", "ino"}
                    or any(type(creating[field]) is not int or creating[field] < 1
                           or creating[field] > MAX_SAFE_INTEGER for field in ("seq", "dev", "ino"))
                    or creating["seq"] != data["next"]):
                raise SegmentIntegrityError("invalid new-segment identity")
        return data

    @staticmethod
    def _new_source(source: SourceRef) -> dict[str, Any]:
        return {"source": source, "high": 0, "evicted": 0, "evicted_bytes": 0,
                "storage_gap_bytes": 0, "storage_gaps": [], "unknown_before": 0}

    @staticmethod
    def _mark_gap(entry: dict[str, Any], start: int, end: int) -> None:
        if start >= end:
            return
        entry["storage_gap_bytes"] += end - start
        gaps = entry["storage_gaps"]
        if gaps and gaps[-1][1] == start:
            gaps[-1][1] = end
        else:
            gaps.append([start, end])
        if len(gaps) > MAX_LOSS_RANGES:
            expired = gaps.pop(0)
            entry["unknown_before"] = max(entry["unknown_before"], expired[1])

    @staticmethod
    def _quarantine_name(pending: dict[str, Any]) -> str:
        return f".quarantine-{pending['nonce']}.raw"

    def _finish_eviction(self, seqs: list[int]) -> bool:
        pending = self._control["eviction"]
        if pending is None:
            return False
        if pending["phase"] != "armed" or pending["seq"] in seqs:
            raise SegmentIntegrityError("eviction cannot be accounted before owned unlink")
        for key, (last, count) in pending["sources"].items():
            entry = self._control["sources"][key]
            entry["evicted"] = max(entry["evicted"], last)
            entry["evicted_bytes"] += count
        self._control["live"].pop(str(pending["seq"]))
        if self._control["active"] == pending["seq"]:
            self._control["active"] = None
        self._control["eviction"] = None
        return True

    def _resume_eviction(self) -> bool:
        """Complete a durable capture only when the exact victim is accounted for."""
        pending = self._control["eviction"]
        if pending is None:
            return False
        root = self._ensure_open()
        public = _segment_name(pending["seq"])
        quarantine = self._quarantine_name(pending)
        public_exists = public in os.listdir(root)
        captured = quarantine in os.listdir(root)
        if public_exists and captured:
            raise SegmentIntegrityError("public victim name reused after capture")
        if pending["phase"] == "planned" and not captured:
            if not public_exists:
                raise SegmentIntegrityError("planned victim disappeared without capture")
            fd = _owned_open(root, public, os.O_RDONLY)
            try:
                info = os.fstat(fd)
                if ((info.st_dev, info.st_ino) != (pending["dev"], pending["ino"])
                        or _fingerprint(fd, self.segment_limit) != pending["digest"]):
                    raise SegmentIntegrityError("planned victim identity changed")
                _capture_no_replace(root, public, quarantine)
                os.fsync(root)
                captured_info = os.stat(quarantine, dir_fd=root, follow_symlinks=False)
                if (captured_info.st_dev, captured_info.st_ino) != (info.st_dev, info.st_ino):
                    raise SegmentIntegrityError("captured entry differs from held victim")
            finally:
                os.close(fd)
            captured = True
            public_exists = public in os.listdir(root)
        if public_exists:
            raise SegmentIntegrityError("public victim name reused after capture")
        if not captured:
            if pending["phase"] != "armed":
                raise SegmentIntegrityError("unarmed quarantine disappeared")
        else:
            fd = _owned_open(root, quarantine, os.O_RDONLY)
            try:
                info = os.fstat(fd)
                if ((info.st_dev, info.st_ino) != (pending["dev"], pending["ino"])
                        or _fingerprint(fd, self.segment_limit) != pending["digest"]):
                    raise SegmentIntegrityError("captured quarantine identity changed")
                if pending["phase"] == "planned":
                    pending["phase"] = "captured"
                    self._save()
                if pending["phase"] == "captured":
                    pending["phase"] = "armed"
                    self._save()
                # Quarantine belongs exclusively to the transaction owner.
                # Its name cannot be substituted by another participant.
                os.unlink(quarantine, dir_fd=root)
                os.fsync(root)
            finally:
                os.close(fd)
        self._finish_eviction(self._names())
        self._save()
        return True

    def _save(self) -> None:
        root = self._ensure_open()
        data = canonical_json(self._control)
        if len(data) > MAX_CONTROL_BYTES:
            raise SegmentIntegrityError("raw metadata budget exhausted")
        name = f".control-{secrets.token_hex(16)}.tmp"
        fd = _owned_open(root, name, os.O_WRONLY, create=True)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        if _CONTROL in os.listdir(root):
            _private_file(os.stat(_CONTROL, dir_fd=root, follow_symlinks=False))
        os.replace(name, _CONTROL, src_dir_fd=root, dst_dir_fd=root)
        os.fsync(root)

    def _scan(self, seq: int, *, repair: bool = False,
              expected_identity: tuple[int, int] | None = None) -> Iterator[tuple[dict[str, Any], int, int]]:
        root = self._ensure_open()
        name = _segment_name(seq)
        if expected_identity is None:
            durable = self._control["live"].get(str(seq))
            if durable is None:
                raise SegmentIntegrityError("scanned segment lacks durable live identity")
            expected_identity = (durable[0], durable[1])
        fd = _owned_open(root, name, os.O_RDWR if repair else os.O_RDONLY)
        try:
            opened = os.fstat(fd)
            if expected_identity is not None and (opened.st_dev, opened.st_ino) != expected_identity:
                raise SegmentIntegrityError("scanned segment identity differs from publication")
            size = opened.st_size
            if size > self.segment_limit or size < _FILE_HEADER.size:
                raise SegmentIntegrityError("segment size outside cap")
            header = os.read(fd, _FILE_HEADER.size)
            if header != _FILE_HEADER.pack(_MAGIC, seq):
                raise SegmentIntegrityError("segment header mismatch")
            pos = _FILE_HEADER.size
            while pos < size:
                os.lseek(fd, pos, os.SEEK_SET)
                header = os.read(fd, _FRAME_HEADER.size)
                if len(header) < _FRAME_HEADER.size:
                    break
                magic, meta_len, payload_len, crc = _FRAME_HEADER.unpack(header)
                if (magic != _FRAME_MAGIC or not 1 <= meta_len <= 1024
                        or not 1 <= payload_len <= MAX_RECORD_PAYLOAD):
                    raise SegmentIntegrityError("invalid complete raw frame header")
                if pos + _FRAME_HEADER.size + meta_len + payload_len > size:
                    break
                meta_raw = os.read(fd, meta_len)
                if len(meta_raw) != meta_len:
                    raise SegmentIntegrityError("raw metadata short read")
                try:
                    meta = json.loads(meta_raw.decode("utf-8"))
                    if (type(meta) is not dict or set(meta) != {"source", "start", "end", "digest", "time"}
                            or parse_source_ref(meta["source"]) != meta["source"]
                            or type(meta["start"]) is not int or type(meta["end"]) is not int
                            or meta["start"] < 0 or meta["end"] - meta["start"] != payload_len
                            or meta["end"] > MAX_SAFE_INTEGER or not isinstance(meta["time"], str)
                            or len(meta["time"]) > 40):
                        raise SegmentIntegrityError("invalid raw frame metadata")
                    _timestamp(meta["time"])
                    if canonical_json(meta) != meta_raw:
                        raise SegmentIntegrityError("noncanonical raw frame metadata")
                except (ValueError, TypeError, KeyError) as exc:
                    raise SegmentIntegrityError("invalid raw frame metadata") from exc
                payload = os.read(fd, payload_len)
                if (len(payload) != payload_len or sha256(payload).hexdigest() != meta["digest"]
                        or (zlib_crc32(meta_raw, payload) != crc)):
                    raise SegmentIntegrityError("raw frame checksum mismatch")
                yield meta, pos + _FRAME_HEADER.size + meta_len, payload_len
                pos += _FRAME_HEADER.size + meta_len + payload_len
            if pos != size:
                if not repair:
                    raise SegmentIntegrityError("incomplete or corrupt segment frame")
                os.ftruncate(fd, pos)
                os.fsync(fd)
            if expected_identity is not None:
                named = os.stat(name, dir_fd=root, follow_symlinks=False)
                _private_file(named)
                scanned = os.fstat(fd)
                if ((named.st_dev, named.st_ino) != expected_identity
                        or (scanned.st_dev, scanned.st_ino) != expected_identity):
                    raise SegmentIntegrityError("scanned segment name changed")
        finally:
            os.close(fd)

    def _recover(self, seqs: list[int]) -> None:
        control = self._control
        changed = False
        if self._resume_eviction():
            seqs = self._names()
        creating = control["creating"]
        if creating is not None:
            seq = creating["seq"]
            if seq in seqs:
                root = self._ensure_open()
                name = _segment_name(seq)
                fd = _owned_open(root, name, os.O_RDONLY)
                try:
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) != (creating["dev"], creating["ino"]):
                        raise SegmentIntegrityError("new segment identity mismatch")
                    if info.st_size != _FILE_HEADER.size:
                        raise SegmentIntegrityError("published new segment is not a complete header")
                    actual = os.read(fd, _FILE_HEADER.size)
                    expected = _FILE_HEADER.pack(_MAGIC, seq)
                    if actual != expected:
                        raise SegmentIntegrityError("published new segment header mismatch")
                finally:
                    os.close(fd)
                control["live"][str(seq)] = [creating["dev"], creating["ino"]]
            control["creating"] = None
            changed = True
        if any(seq >= control["next"] and (creating is None or seq != creating["seq"])
               for seq in seqs):
            raise SegmentIntegrityError("future raw segment lacks publication authority")
        pending_eviction = control["eviction"]
        if (control["active"] is not None and control["active"] not in seqs
                and (pending_eviction is None or pending_eviction["seq"] != control["active"])):
            raise SegmentIntegrityError("active raw segment disappeared without rotation intent")
        if set(seqs) != {int(seq) for seq in control["live"]}:
            raise SegmentIntegrityError("public raw names differ from durable live catalog")
        intent = control["intent"]
        recovered_end = intent["start"] if intent is not None else 0
        for seq in seqs:
            expected_identity = ((creating["dev"], creating["ino"])
                                 if creating is not None and seq == creating["seq"]
                                 else tuple(control["live"][str(seq)]))
            for meta, _, _ in self._scan(seq, repair=True, expected_identity=expected_identity):
                key = _source_key(meta["source"])
                entry = control["sources"].get(key)
                if entry is None:
                    if len(control["sources"]) >= MAX_SOURCES:
                        raise SegmentIntegrityError("source catalog exhausted")
                    entry = self._new_source(meta["source"])
                    control["sources"][key] = entry
                    changed = True
                if intent is not None and key == intent["key"] and intent["start"] <= meta["start"] < intent["end"]:
                    recovered_end = max(recovered_end, meta["end"])
                if meta["end"] > entry["high"]:
                    entry["high"] = meta["end"]
                    changed = True
        if seqs:
            if control["active"] != seqs[-1]:
                control["active"] = seqs[-1]
                changed = True
            if control["next"] <= seqs[-1]:
                control["next"] = seqs[-1] + 1
                changed = True
        elif control["active"] is not None:
            control["active"] = None
            changed = True
        if intent is not None:
            entry = control["sources"][intent["key"]]
            recovered_end = max(recovered_end, min(entry["evicted"], intent["end"]))
            self._mark_gap(entry, recovered_end, intent["end"])
            control["intent"] = None
            changed = True
        if changed:
            self._save()

    def _rotate(self) -> None:
        root = self._ensure_open()
        seqs = self._names()
        if (len(seqs) > self.segment_count
                or any(seq >= self._control["next"] for seq in seqs)
                or self._control["creating"] is not None):
            raise SegmentIntegrityError("unexpected segment before rotation")
        seq = self._control["next"]
        name = _segment_name(seq)
        if set(seqs) != {int(value) for value in self._control["live"]}:
            raise SegmentIntegrityError("public raw names differ from durable live catalog")
        if len(seqs) >= self.segment_count:
            victim = seqs[0]
            victim_name = _segment_name(victim)
            held = _owned_open(root, victim_name, os.O_RDONLY)
            try:
                identity = os.fstat(held)
                if [identity.st_dev, identity.st_ino] != self._control["live"][str(victim)]:
                    raise SegmentIntegrityError("rotation victim identity changed")
                affected: dict[str, list[int]] = {}
                for meta, _, _ in self._scan(victim, expected_identity=(identity.st_dev, identity.st_ino)):
                    key = _source_key(meta["source"])
                    value = affected.setdefault(key, [0, 0])
                    value[0] = max(value[0], meta["end"])
                    value[1] += meta["end"] - meta["start"]
                digest = _fingerprint(held, self.segment_limit)
                named = os.stat(victim_name, dir_fd=root, follow_symlinks=False)
                if (named.st_dev, named.st_ino) != (identity.st_dev, identity.st_ino):
                    raise SegmentIntegrityError("rotation victim name changed")
                self._control["eviction"] = {
                    "seq": victim, "sources": affected, "dev": identity.st_dev,
                    "ino": identity.st_ino, "digest": digest,
                    "nonce": secrets.token_hex(16), "phase": "planned"}
                self._save()
            finally:
                os.close(held)
            self._resume_eviction()
        # An unnamed inode cannot leave a visible 0..15-byte segment behind if
        # the process dies while writing the header. Publication is one linkat.
        fd, identity = _prepare_linux_segment(root, seq)
        try:
            self._control["creating"] = {"seq": seq, "dev": identity.st_dev, "ino": identity.st_ino}
            self._save()  # durable identity binding precedes visible publication
            _publish_prepared(fd, root, name)
            os.fsync(root)
        finally:
            os.close(fd)
        self._control["active"] = seq
        self._control["next"] = seq + 1
        self._control["creating"] = None
        self._control["live"][str(seq)] = [identity.st_dev, identity.st_ino]
        self._save()

    def _append_frame(self, source: SourceRef, start: int, payload: memoryview, moment: str) -> int:
        root = self._ensure_open()
        while True:
            active = self._control["active"]
            if active is None:
                self._rotate()
                active = self._control["active"]
            assert active is not None
            name = _segment_name(active)
            fd = _owned_open(root, name, os.O_RDWR)
            try:
                info = os.fstat(fd)
                if [info.st_dev, info.st_ino] != self._control["live"].get(str(active)):
                    raise SegmentIntegrityError("active raw identity changed")
                size = info.st_size
                if size > self.segment_limit:
                    raise SegmentIntegrityError("segment exceeds cap")
                max_payload = min(len(payload), MAX_RECORD_PAYLOAD)
                # The digit count of end may change; recalculate the exact frame size.
                while max_payload:
                    piece = payload[:max_payload]
                    meta = canonical_json({"source": source, "start": start,
                                           "end": start + len(piece), "time": moment,
                                           "digest": sha256(piece).hexdigest()})
                    if len(meta) > 1024:
                        raise SegmentIntegrityError("raw frame metadata exceeds bound")
                    if size + _FRAME_HEADER.size + len(meta) + len(piece) <= self.segment_limit:
                        break
                    max_payload = max(0, min(max_payload - 1,
                                             self.segment_limit - size - _FRAME_HEADER.size - len(meta)))
                if max_payload:
                    frame = _FRAME_HEADER.pack(_FRAME_MAGIC, len(meta), len(piece), zlib_crc32(meta, piece))
                    os.lseek(fd, 0, os.SEEK_END)
                    try:
                        _write_all(fd, frame)
                        _write_all(fd, meta)
                        _write_all(fd, piece)
                        os.fsync(fd)
                    except OSError:
                        os.ftruncate(fd, size)
                        os.fsync(fd)
                        raise
                    try:
                        named = os.stat(name, dir_fd=root, follow_symlinks=False)
                    except FileNotFoundError as exc:
                        raise SegmentIntegrityError("active raw name disappeared after write") from exc
                    _private_file(named)
                    if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
                        raise SegmentIntegrityError("active raw name changed after write")
                    return len(piece)
            finally:
                os.close(fd)
            if size == _FILE_HEADER.size:
                raise SegmentIntegrityError("frame cannot fit configured segment cap")
            self._rotate()

    def append(self, source: SourceRef, payload: bytes, *, observed_at: str | None = None,
               source_offset: int | None = None) -> dict[str, Any]:
        """Observe bytes, persist whole frames, and report any unstored range.

        The caller owns observation/display/proof before this method. Storage
        failure is represented in the result and never requests host shutdown.
        """
        normalized = parse_source_ref(source)
        if type(payload) is not bytes:
            raise TypeError("payload must be bytes")
        moment = _timestamp(observed_at)
        with self._mutex:
            key = _source_key(normalized)
            entry = self._control["sources"].get(key)
            if entry is None:
                if len(self._control["sources"]) >= MAX_SOURCES:
                    if (source_offset is not None and (type(source_offset) is not int
                            or not 0 <= source_offset <= MAX_SAFE_INTEGER)):
                        raise ValueError("source_offset must be a safe nonnegative integer")
                    start = source_offset
                    end = None if start is None else start + len(payload)
                    if end is not None and end > MAX_SAFE_INTEGER:
                        raise ValueError("source offset exhausted")
                    return {"source": normalized, "start": start, "end": end,
                            "stored_bytes": 0, "missing_bytes": len(payload),
                            "storage_error": "source_catalog_full", "observation_continues": True}
                entry = self._new_source(normalized)
                self._control["sources"][key] = entry
            start = entry["high"]
            if source_offset is not None and (type(source_offset) is not int or source_offset != start):
                raise ValueError("source_offset must equal the next observed byte")
            end = start + len(payload)
            if end > MAX_SAFE_INTEGER:
                raise ValueError("source offset exhausted")
            if not payload:
                return {"source": normalized, "start": start, "end": end,
                        "stored_bytes": 0, "missing_bytes": 0, "storage_error": None,
                        "observation_continues": True}
            try:
                self._ensure_open()
            except (OSError, SegmentIntegrityError) as exc:
                self._faulted = True
                entry["high"] = end
                self._mark_gap(entry, start, end)
                return {"source": normalized, "start": start, "end": end,
                        "stored_bytes": 0, "missing_bytes": len(payload),
                        "storage_error": f"{type(exc).__name__}:{getattr(exc, 'errno', '')}"[:80],
                        "observation_continues": True}
            if self._control["intent"] is not None:
                raise SegmentIntegrityError("previous raw write intent unresolved")
            if self._faulted:
                entry["high"] = end
                self._mark_gap(entry, start, end)
                return {"source": normalized, "start": start, "end": end,
                        "stored_bytes": 0, "missing_bytes": len(payload),
                        "storage_error": "raw_store_uncertain", "observation_continues": True}
            self._control["intent"] = {"key": key, "start": start, "end": end}
            entry["high"] = end
            try:
                self._save()
            except (OSError, SegmentIntegrityError) as exc:
                self._faulted = True
                self._control["intent"] = None
                self._mark_gap(entry, start, end)
                return {"source": normalized, "start": start, "end": end,
                        "stored_bytes": 0, "missing_bytes": len(payload),
                        "storage_error": f"{type(exc).__name__}:{getattr(exc, 'errno', '')}"[:80],
                        "observation_continues": True}
            stored = 0
            error: str | None = None
            try:
                view = memoryview(payload)
                while stored < len(view):
                    stored += self._append_frame(normalized, start + stored, view[stored:], moment)
            except (OSError, SegmentIntegrityError) as exc:
                error = f"{type(exc).__name__}:{getattr(exc, 'errno', '')}"[:80]
                if self._control["eviction"] is not None or self._control["creating"] is not None:
                    self._faulted = True
            finally:
                self._mark_gap(entry, start + stored, end)
                self._control["intent"] = None
                try:
                    self._save()
                except (OSError, SegmentIntegrityError) as exc:
                    error = f"{type(exc).__name__}:{getattr(exc, 'errno', '')}"[:80]
                    self._faulted = True
            return {"source": normalized, "start": start, "end": end,
                    "stored_bytes": stored, "missing_bytes": len(payload) - stored,
                    "storage_error": error, "observation_continues": True}

    def segment_sizes(self) -> list[int]:
        with self._mutex:
            root = self._ensure_open()
            return [os.stat(_segment_name(seq), dir_fd=root, follow_symlinks=False).st_size
                    for seq in self._names()]

    def storage_usage(self) -> dict[str, int]:
        with self._mutex:
            root = self._ensure_open()
            raw = sum(self.segment_sizes())
            raw += sum(os.stat(name, dir_fd=root, follow_symlinks=False).st_size
                       for name in os.listdir(root) if _QUARANTINE.fullmatch(name))
            control = sum(os.stat(name, dir_fd=root, follow_symlinks=False).st_size
                          for name in (_MARKER, _CONTROL, _LOCK))
            return {"raw_bytes": raw, "metadata_bytes": control,
                    "raw_limit_bytes": self.segment_limit * self.segment_count}

    def read_raw(self, source: SourceRef, start: int, end: int) -> dict[str, Any]:
        from workbench.storage.log_raw.reader import SegmentedRawReader
        return SegmentedRawReader(self).read_raw(source, start, end)

    def source_status(self, source: SourceRef) -> dict[str, Any]:
        from workbench.storage.log_raw.reader import SegmentedRawReader
        return SegmentedRawReader(self).source_status(source)


class ReadonlySegmentedRawStore(SegmentedRawStore):
    """Stopped-store view. It never creates a root or performs writer recovery.

    The writer holds its flock for its lifetime, so an active store is reported
    busy rather than exposing a moving control/segment set to a standalone CLI.
    Backend requests use the already injected writer singleton instead.
    """

    def __init__(self, root: str | os.PathLike[str]):
        self.root_path = os.path.abspath(os.fspath(root))
        self._mutex = RLock()
        self._root = None
        self._lock = None
        self._control = {}
        self._faulted = False
        try:
            self._root = os.open(self.root_path, os.O_RDONLY | os.O_DIRECTORY |
                                 os.O_NOFOLLOW | os.O_CLOEXEC)
            _private_dir(os.fstat(self._root))
            marker = _owned_open(self._root, _MARKER, os.O_RDONLY)
            try:
                if os.read(marker, 128) != b"workbench-segmented-raw-v1\n":
                    raise SegmentIntegrityError("raw root marker mismatch")
            finally:
                os.close(marker)
            self._lock = _owned_open(self._root, _LOCK, os.O_RDONLY)
            try:
                fcntl.flock(self._lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SegmentIntegrityError("raw writer active; use the backend reader") from exc
            control = _owned_open(self._root, _CONTROL, os.O_RDONLY)
            try:
                self._control = self._parse_control(os.read(control, MAX_CONTROL_BYTES + 1))
            finally:
                os.close(control)
            self.segment_limit = self._control["limit"]
            self.segment_count = self._control["count"]
            if (not 256 <= self.segment_limit <= SEGMENT_LIMIT
                    or not 1 <= self.segment_count <= SEGMENT_COUNT):
                raise SegmentIntegrityError("raw control cap outside product bound")
            if (self._control["intent"] is not None or self._control["eviction"] is not None
                    or self._control["creating"] is not None):
                raise SegmentIntegrityError("raw recovery pending; read unavailable")
            self._names()
        except BaseException:
            self.close()
            raise

    def _names(self) -> list[int]:
        root = self._ensure_open()
        # The writer's implementation removes abandoned temp controls. A query
        # must leave them untouched and fail closed instead.
        if any(_TEMP.fullmatch(name) or _QUARANTINE.fullmatch(name)
               for name in os.listdir(root)):
            raise SegmentIntegrityError("raw root needs writer recovery")
        return super()._names()


def zlib_crc32(meta: bytes, payload: bytes | memoryview) -> int:
    import zlib
    return zlib.crc32(payload, zlib.crc32(meta)) & 0xFFFFFFFF
