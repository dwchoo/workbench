"""Bounded source-relative reads of the opt-in segmented raw store."""

from __future__ import annotations

import base64
from hashlib import sha256
import os
from typing import TYPE_CHECKING, Any

from workbench.contracts.logs_v1 import (
    MAX_SAFE_INTEGER, RawReadResult, SourceRef, parse_raw_read_result,
    parse_source_ref,
)
from workbench.storage.log_raw.segments import SegmentIntegrityError, _owned_open, _source_key

if TYPE_CHECKING:
    from workbench.storage.log_raw.segments import SegmentedRawStore


MAX_READ_BYTES = 128 * 1024
_MAX_ITEMS = 32


def _range(source: SourceRef, start: int, data: bytes) -> dict[str, Any]:
    return {"schemaVersion": 1, "source": source, "start": start,
            "end": start + len(data), "digest": sha256(data).hexdigest()}


def _gap(source: SourceRef, start: int, end: int, reason: str) -> dict[str, Any]:
    return {"schemaVersion": 1, "source": source,
            "start": start, "end": end, "reason": reason}


class SegmentedRawReader:
    """Reader tied to the singleton writer's synchronized, verified file view."""

    def __init__(self, store: SegmentedRawStore):
        self.store = store

    def read_raw(self, source: SourceRef, start: int, end: int) -> RawReadResult:
        normalized = parse_source_ref(source)
        if (type(start) is not int or type(end) is not int or start < 0
                or end <= start or end > MAX_SAFE_INTEGER or end - start > MAX_READ_BYTES):
            raise ValueError("raw read must be a nonempty bounded source interval")
        with self.store._mutex:
            root = self.store._ensure_open()
            entry = self.store._control["sources"].get(_source_key(normalized))
            if entry is None:
                result = {"schemaVersion": 1, "source": normalized, "status": "unknown",
                          "chunks": [], "available": [],
                          "missing": [_gap(normalized, start, end, "unknown")]}
                return parse_raw_read_result(result)
            high, evicted = entry["high"], entry["evicted"]
            pieces: list[tuple[int, bytes]] = []
            last_end = 0
            for seq in self.store._names():
                for meta, offset, length in self.store._scan(seq):
                    if meta["source"] != normalized:
                        continue
                    if meta["start"] < last_end or meta["end"] > high:
                        raise ValueError("raw source frame overlaps or exceeds observed bound")
                    last_end = meta["end"]
                    lo, hi = max(start, meta["start"]), min(end, meta["end"])
                    if lo >= hi:
                        continue
                    fd = self.store._ensure_open()
                    # A public name may change outside the writer mutex.
                    file_fd = _owned_open(fd, f"seg-{seq:016d}.raw", os.O_RDONLY)
                    try:
                        identity = os.fstat(file_fd)
                        if [identity.st_dev, identity.st_ino] != self.store._control["live"].get(str(seq)):
                            raise SegmentIntegrityError("raw read identity changed after scan")
                        full_frame = os.pread(file_fd, length, offset)
                    finally:
                        os.close(file_fd)
                    if len(full_frame) != length or sha256(full_frame).hexdigest() != meta["digest"]:
                        raise SegmentIntegrityError("raw frame changed after verified scan")
                    data = full_frame[lo - meta["start"]:hi - meta["start"]]
                    pieces.append((lo, data))
                    if len(pieces) > 4096:
                        raise ValueError("raw page has too many frames; use a smaller page")
            chunks: list[dict[str, Any]] = []
            missing: list[dict[str, Any]] = []
            cursor = start

            def add_gap(lo: int, hi: int) -> None:
                while lo < hi:
                    stored_gap = next((span for span in entry["storage_gaps"]
                                       if span[0] <= lo < span[1]), None)
                    next_gap = min((span[0] for span in entry["storage_gaps"] if span[0] > lo), default=hi)
                    if lo < entry["unknown_before"] and stored_gap is None:
                        reason, boundary = "unknown", min(hi, entry["unknown_before"], next_gap)
                    elif stored_gap is not None:
                        reason, boundary = "storage_gap", min(hi, stored_gap[1])
                    elif lo < evicted:
                        reason, boundary = "evicted", min(hi, evicted, next_gap)
                    elif lo < high:
                        reason, boundary = "storage_gap", min(hi, high, next_gap)
                    else:
                        reason, boundary = "unknown", hi
                    if boundary <= lo:
                        boundary = hi
                    if missing and missing[-1]["end"] == lo and missing[-1]["reason"] == reason:
                        missing[-1]["end"] = boundary
                    else:
                        missing.append(_gap(normalized, lo, boundary, reason))
                    lo = boundary

            for lo, data in pieces:
                if lo > cursor:
                    add_gap(cursor, lo)
                elif lo < cursor:
                    raise ValueError("raw source frame overlap")
                if chunks and chunks[-1]["range"]["end"] == lo and len(base64.b64decode(chunks[-1]["dataBase64"])) + len(data) <= MAX_READ_BYTES:
                    previous = base64.b64decode(chunks[-1]["dataBase64"])
                    combined = previous + data
                    chunks[-1] = {"schemaVersion": 1, "range": _range(normalized, chunks[-1]["range"]["start"], combined),
                                  "dataBase64": base64.b64encode(combined).decode("ascii")}
                else:
                    chunks.append({"schemaVersion": 1, "range": _range(normalized, lo, data),
                                   "dataBase64": base64.b64encode(data).decode("ascii")})
                cursor = lo + len(data)
            if cursor < end:
                add_gap(cursor, end)
            if len(chunks) > _MAX_ITEMS or len(missing) > _MAX_ITEMS:
                raise ValueError("raw interval has too many fragments; use a smaller page")
            available = [chunk["range"] for chunk in chunks]
            status = "available" if not missing else "partial" if chunks else "unavailable"
            result = {"schemaVersion": 1, "source": normalized, "status": status,
                      "chunks": chunks, "available": available, "missing": missing}
            return parse_raw_read_result(result)

    def source_status(self, source: SourceRef) -> dict[str, Any]:
        """Bounded source accounting; read_raw pages expose full range detail."""
        normalized = parse_source_ref(source)
        with self.store._mutex:
            self.store._ensure_open()
            entry = self.store._control["sources"].get(_source_key(normalized))
            if entry is None:
                return {"source": normalized, "status": "unknown", "observed_bytes": 0,
                        "retained_bytes": 0, "evicted_bytes": 0, "missing_bytes": 0,
                        "retained_ranges": [], "missing_ranges": [], "range_detail_truncated": False}
            retained = 0
            retained_ranges: list[list[int]] = []
            missing_ranges: list[dict[str, Any]] = []
            truncated = False
            cursor = 0

            def add_missing(lo: int, hi: int) -> None:
                nonlocal truncated
                while lo < hi:
                    gap = next((span for span in entry["storage_gaps"]
                                if span[0] <= lo < span[1]), None)
                    next_gap = min((span[0] for span in entry["storage_gaps"] if span[0] > lo), default=hi)
                    if gap is not None:
                        reason, stop = "storage_gap", min(hi, gap[1])
                    elif lo < entry["unknown_before"]:
                        reason, stop = "unknown", min(hi, entry["unknown_before"], next_gap)
                    elif lo < entry["evicted"]:
                        reason, stop = "evicted", min(hi, entry["evicted"], next_gap)
                    else:
                        reason, stop = "storage_gap", min(hi, next_gap)
                    if len(missing_ranges) < _MAX_ITEMS:
                        if missing_ranges and missing_ranges[-1]["end"] == lo and missing_ranges[-1]["reason"] == reason:
                            missing_ranges[-1]["end"] = stop
                        else:
                            missing_ranges.append(_gap(normalized, lo, stop, reason))
                    else:
                        truncated = True
                    lo = stop

            for seq in self.store._names():
                for meta, _, length in self.store._scan(seq):
                    if meta["source"] == normalized:
                        if meta["start"] < cursor or meta["end"] > entry["high"]:
                            raise ValueError("raw source frame order or bound mismatch")
                        if meta["start"] > cursor:
                            add_missing(cursor, meta["start"])
                        if retained_ranges and retained_ranges[-1][1] == meta["start"]:
                            retained_ranges[-1][1] = meta["end"]
                        elif len(retained_ranges) < _MAX_ITEMS:
                            retained_ranges.append([meta["start"], meta["end"]])
                        else:
                            truncated = True
                        cursor = meta["end"]
                        retained += length
            if cursor < entry["high"]:
                add_missing(cursor, entry["high"])
            high = entry["high"]
            if retained > high:
                raise ValueError("raw source accounting exceeds observed bytes")
            evicted_bytes = min(entry["evicted_bytes"], high - retained)
            return {"source": normalized, "status": "available" if retained else "unavailable", "observed_bytes": high,
                    "retained_bytes": retained, "evicted_bytes": evicted_bytes,
                    "missing_bytes": high - retained - evicted_bytes,
                    "retained_ranges": retained_ranges, "missing_ranges": missing_ranges,
                    "range_detail_truncated": truncated}
