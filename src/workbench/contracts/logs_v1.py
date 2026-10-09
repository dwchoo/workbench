"""Strict JSON v1 contracts for important logs and summary control.

The wire names are shared with ``omp_bridge/contract/logs_v1.ts``. Raw bytes and
important bodies never belong in the durable request journal.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
import re
from typing import Any, Protocol, TypeAlias
from datetime import datetime
from uuid import UUID

SCHEMA_VERSION = 1
MAX_SAFE_INTEGER = (1 << 53) - 1
MAX_BODY_BYTES = 1_000_000
MAX_SUMMARY_RESULT_BYTES = 8_192

SubjectRef: TypeAlias = dict[str, Any]
SourceRef: TypeAlias = dict[str, Any]
SourceRange: TypeAlias = dict[str, Any]
RawReadResult: TypeAlias = dict[str, Any]
ImportantEntry: TypeAlias = dict[str, Any]
HealthSnapshot: TypeAlias = dict[str, Any]
SummaryRequest: TypeAlias = dict[str, Any]
SummaryResult: TypeAlias = dict[str, Any]
ObservationBoundary: TypeAlias = dict[str, Any]
ObservationCoverage: TypeAlias = dict[str, Any]

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_USD = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")
_SOURCE_KINDS = frozenset({"terminal", "task_spec", "manager_instruction", "worker_instruction", "report", "decision", "event"})
_ENTRY_KINDS = frozenset({"start", "event", "cumulative", "intermediate", "gap", "closure"})
_GAP_REASONS = frozenset({"collector_gap", "storage_gap", "evicted", "unknown"})


class LogContractError(ValueError):
    """A value is outside the versioned log contract."""

    code = "invalid_log_contract_v1"


def _object(value: object, name: str, required: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise LogContractError(f"{name} must be an object")
    data = value
    missing = required - data.keys()
    extra = data.keys() - required - optional
    if missing or extra:
        raise LogContractError(f"{name} fields missing={sorted(missing)} unsupported={sorted(extra)}")
    if data.get("schemaVersion") != SCHEMA_VERSION or type(data.get("schemaVersion")) is not int:
        raise LogContractError(f"{name} has unsupported schemaVersion")
    return data


def _uuid(value: object, name: str) -> str:
    if type(value) is not str:
        raise LogContractError(f"{name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise LogContractError(f"{name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise LogContractError(f"{name} must be a canonical UUID")
    return value


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise LogContractError(f"{name} must be a safe integer >= {minimum}")
    return value


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise LogContractError(f"{name} must be lowercase SHA-256 hex")
    return value


def _text(value: object, name: str, *, maximum: int = 4096) -> str:
    if type(value) is not str or not value:
        raise LogContractError(f"{name} must be nonempty bounded UTF-8 text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise LogContractError(f"{name} must be valid UTF-8 text") from exc
    if size > maximum:
        raise LogContractError(f"{name} must be nonempty bounded UTF-8 text")
    return value


def _time(value: object, name: str) -> str:
    if type(value) is not str or _UTC.fullmatch(value) is None:
        raise LogContractError(f"{name} must be a UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LogContractError(f"{name} is not a valid timestamp") from exc
    return value


def _array(value: object, name: str, *, maximum: int = 1024) -> list[Any]:
    if type(value) is not list or len(value) > maximum:
        raise LogContractError(f"{name} must be a bounded array")
    return value


def _strings(value: object, name: str, *, maximum: int = 128) -> list[str]:
    return [_text(item, name, maximum=MAX_BODY_BYTES) for item in _array(value, name, maximum=maximum)]


def canonical_json(value: object) -> bytes:
    """Canonical UTF-8 bytes; finite JSON and JS-safe integers only."""
    def check(item: object) -> None:
        if item is None or type(item) is bool:
            return
        if type(item) is str:
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise LogContractError("JSON string must be valid UTF-8") from exc
            return
        if type(item) is int:
            _integer(item, "JSON integer", -MAX_SAFE_INTEGER)
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for key, child in item.items():
                check(key)
                check(child)
            return
        raise LogContractError("JSON value must use finite shared types")
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def decode_json(raw: bytes | str, *, max_bytes: int = MAX_BODY_BYTES) -> object:
    try:
        payload = raw.encode("utf-8") if type(raw) is str else raw
    except UnicodeEncodeError as exc:
        raise LogContractError("invalid UTF-8 JSON") from exc
    if type(payload) is not bytes or len(payload) > max_bytes:
        raise LogContractError("JSON envelope exceeds its bound")
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise LogContractError(f"duplicate JSON field: {key}")
            result[key] = value
        return result
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(LogContractError("non-finite JSON number")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LogContractError("invalid UTF-8 JSON") from exc
    try:
        canonical_json(value)
    except UnicodeEncodeError as exc:
        raise LogContractError("invalid UTF-8 JSON") from exc
    return value


def parse_subject_ref(value: object) -> SubjectRef:
    data = _object(value, "SubjectRef", {"schemaVersion", "kind", "id"})
    if data["kind"] not in ("task", "command"):
        raise LogContractError("unsupported subject kind")
    _uuid(data["id"], "subject.id")
    return dict(data)


def parse_source_ref(value: object) -> SourceRef:
    data = _object(value, "SourceRef", {"schemaVersion", "sourceKind", "collectorEpoch"},
                   {"taskId", "revision", "runId", "commandId", "messageId", "eventId"})
    if type(data["sourceKind"]) is not str or data["sourceKind"] not in _SOURCE_KINDS:
        raise LogContractError("unsupported source kind")
    _integer(data["collectorEpoch"], "collectorEpoch", 1)
    for key in ("taskId", "runId", "commandId", "messageId", "eventId"):
        if key in data:
            _uuid(data[key], key)
    if ("taskId" in data) != ("revision" in data):
        raise LogContractError("taskId and revision must appear together")
    if "runId" in data and "taskId" not in data:
        raise LogContractError("runId requires its Task and revision")
    if "revision" in data:
        _integer(data["revision"], "revision", 1)
    if data["sourceKind"] == "terminal":
        if ("runId" in data) == ("commandId" in data) or "messageId" in data or "eventId" in data:
            raise LogContractError("terminal source requires exactly one run or command ID without aliases")
    elif ("messageId" in data) == ("eventId" in data):
        raise LogContractError("structured source requires exactly one message or event ID")
    return dict(data)


def parse_source_range(value: object) -> SourceRange:
    data = _object(value, "SourceRange", {"schemaVersion", "source", "start", "end", "digest"})
    parse_source_ref(data["source"])
    start = _integer(data["start"], "start")
    end = _integer(data["end"], "end")
    if end <= start:
        raise LogContractError("SourceRange must be nonempty [start,end)")
    _digest(data["digest"], "range digest")
    return dict(data)


def parse_source_gap(value: object) -> dict[str, Any]:
    data = _object(value, "SourceGap", {"schemaVersion", "source", "start", "end", "reason"})
    parse_source_ref(data["source"])
    start = _integer(data["start"], "start")
    end = _integer(data["end"], "end")
    if end <= start or type(data["reason"]) is not str or data["reason"] not in _GAP_REASONS:
        raise LogContractError("invalid source gap")
    return dict(data)


def parse_observation_boundary(value: object) -> ObservationBoundary:
    data = _object(value, "ObservationBoundary", {"schemaVersion", "eventId", "source", "kind", "offset", "observedAt"})
    _uuid(data["eventId"], "eventId")
    source = parse_source_ref(data["source"])
    if source["sourceKind"] != "terminal" or "runId" not in source:
        raise LogContractError("observation boundary requires a terminal Run")
    if data["kind"] not in ("start", "final_drain"):
        raise LogContractError("invalid observation boundary kind")
    _integer(data["offset"], "offset")
    if data["kind"] == "start" and data["offset"] != 0:
        raise LogContractError("start boundary must have offset zero")
    _time(data["observedAt"], "observedAt")
    return dict(data)


def parse_observation_coverage(value: object) -> ObservationCoverage:
    data = _object(value, "ObservationCoverage", {"schemaVersion", "coverageId", "source", "startEventId", "finalEventId", "finalOffset", "sourceIds"})
    for key in ("coverageId", "startEventId", "finalEventId"):
        _uuid(data[key], key)
    if data["startEventId"] == data["finalEventId"]:
        raise LogContractError("coverage boundaries must be distinct")
    source = parse_source_ref(data["source"])
    if source["sourceKind"] != "terminal" or "runId" not in source:
        raise LogContractError("observation coverage requires a terminal Run")
    _integer(data["finalOffset"], "finalOffset")
    ids = [_uuid(item, "sourceId") for item in _array(data["sourceIds"], "sourceIds")]
    if len(set(ids)) != len(ids):
        raise LogContractError("duplicate coverage source ID")
    return dict(data)


def _source_belongs(subject: SubjectRef, source: SourceRef, revision: int | None = None) -> None:
    if subject["kind"] == "task":
        if source.get("taskId") != subject["id"]:
            raise LogContractError("source belongs to another Task")
        if revision is not None and source.get("revision") != revision:
            raise LogContractError("source Task revision mismatch")
    elif source.get("commandId") != subject["id"] or "taskId" in source:
        raise LogContractError("command subject requires its own unlinked command source")


def _physical_source_key(source: SourceRef) -> bytes:
    stable = {"sourceKind": source["sourceKind"]}
    if source["sourceKind"] == "terminal":
        key = "runId" if "runId" in source else "commandId"
    else:
        key = "messageId" if "messageId" in source else "eventId"
    stable[key] = source[key]
    return canonical_json(stable)


def parse_raw_read_result(value: object) -> RawReadResult:
    data = _object(value, "RawReadResult", {"schemaVersion", "source", "status", "chunks", "available", "missing"})
    source = parse_source_ref(data["source"])
    if data["status"] not in ("available", "partial", "unavailable", "unknown"):
        raise LogContractError("invalid raw read status")
    for chunk in _array(data["chunks"], "chunks", maximum=32):
        part = _object(chunk, "RawChunk", {"schemaVersion", "range", "dataBase64"})
        span = parse_source_range(part["range"])
        if span["source"] != source or type(part["dataBase64"]) is not str:
            raise LogContractError("raw chunk source/base64 mismatch")
        try:
            raw = base64.b64decode(part["dataBase64"], validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise LogContractError("invalid raw chunk base64") from exc
        if (base64.b64encode(raw).decode("ascii") != part["dataBase64"]
                or len(raw) != span["end"] - span["start"]
                or sha256(raw).hexdigest() != span["digest"]):
            raise LogContractError("raw chunk length/checksum mismatch")
    for span in _array(data["available"], "available", maximum=32):
        if parse_source_range(span)["source"] != source:
            raise LogContractError("available range source mismatch")
    for gap in _array(data["missing"], "missing", maximum=32):
        if parse_source_gap(gap)["source"] != source:
            raise LogContractError("missing range source mismatch")
    if len(canonical_json(data)) > MAX_BODY_BYTES:
        raise LogContractError("raw read result exceeds bound")
    return dict(data)


def parse_important_body(value: object) -> dict[str, Any]:
    data = _object(value, "ImportantBody", {"schemaVersion", "facts", "interpretations", "unknowns"})
    for key in ("facts", "interpretations", "unknowns"):
        _strings(data[key], key)
    if len(canonical_json(data)) > MAX_BODY_BYTES:
        raise LogContractError("important body exceeds bound")
    return dict(data)


def parse_important_entry(value: object) -> ImportantEntry:
    data = _object(value, "ImportantEntry", {"schemaVersion", "entryId", "subject", "kind", "sources", "sourceGaps", "recordedAt", "body", "bodyCodec", "decodedBytes", "storedBytes", "bodySha256", "originDbUuid"})
    _uuid(data["entryId"], "entryId")
    subject = parse_subject_ref(data["subject"])
    if type(data["kind"]) is not str or data["kind"] not in _ENTRY_KINDS:
        raise LogContractError("invalid important entry kind")
    for source in _array(data["sources"], "sources", maximum=128):
        _source_belongs(subject, parse_source_range(source)["source"])
    for gap in _array(data["sourceGaps"], "sourceGaps", maximum=128):
        _source_belongs(subject, parse_source_gap(gap)["source"])
    if len(canonical_json(data["sources"])) + len(canonical_json(data["sourceGaps"])) > MAX_BODY_BYTES:
        raise LogContractError("entry source metadata exceeds bound")
    _time(data["recordedAt"], "recordedAt")
    body = parse_important_body(data["body"])
    if data["bodyCodec"] not in ("identity-v1", "zlib-v1"):
        raise LogContractError("invalid body codec")
    decoded = _integer(data["decodedBytes"], "decodedBytes")
    stored = _integer(data["storedBytes"], "storedBytes")
    if decoded != len(canonical_json(body)) or not 1 <= stored <= MAX_BODY_BYTES:
        raise LogContractError("entry body byte count mismatch")
    _digest(data["bodySha256"], "bodySha256")
    if data["bodySha256"] != sha256(canonical_json(body)).hexdigest():
        raise LogContractError("entry body checksum mismatch")
    _uuid(data["originDbUuid"], "originDbUuid")
    return dict(data)


def parse_health_snapshot(value: object) -> HealthSnapshot:
    data = _object(value, "HealthSnapshot", {"schemaVersion", "dbUuid", "measuredAt", "stale", "mainBytes", "walBytes", "shmBytes", "totalBytes", "logicalLiveBytes", "freelistBytes", "subjectStats", "cleanupCandidates", "lastWrite", "lastLock", "lastMaintenance", "diskFreeBytes", "lastQuickCheck", "metadataOutsideBudget"})
    _uuid(data["dbUuid"], "dbUuid")
    _time(data["measuredAt"], "measuredAt")
    if type(data["stale"]) is not bool or data["metadataOutsideBudget"] is not True:
        raise LogContractError("health flags invalid")
    for key in ("mainBytes", "walBytes", "shmBytes", "totalBytes", "logicalLiveBytes", "freelistBytes"):
        _integer(data[key], key)
    if data["cleanupCandidates"] is not None:
        _integer(data["cleanupCandidates"], "cleanupCandidates")
    if data["totalBytes"] != data["mainBytes"] + data["walBytes"] + data["shmBytes"]:
        raise LogContractError("physical size sum mismatch")
    for item in _array(data["subjectStats"], "subjectStats", maximum=1024):
        row = _object(item, "SubjectStat", {"schemaVersion", "subject", "entryCount", "storedBytes"})
        parse_subject_ref(row["subject"])
        _integer(row["entryCount"], "entryCount")
        _integer(row["storedBytes"], "storedBytes")
    for key in ("lastWrite", "lastLock", "lastMaintenance", "lastQuickCheck"):
        if data[key] is not None:
            _text(data[key], key, maximum=256)
    if data["diskFreeBytes"] is not None:
        _integer(data["diskFreeBytes"], "diskFreeBytes")
    return dict(data)


def parse_summary_request(value: object) -> SummaryRequest:
    data = _object(value, "SummaryRequest", {"schemaVersion", "requestId", "subject", "revision", "authorizationRef", "authorizationEpoch", "inputKey", "skillName", "skillDigest", "sources", "sourceGaps", "priorCumulativeEntryId", "dbHealth", "requestedAt"})
    _uuid(data["requestId"], "requestId")
    subject = parse_subject_ref(data["subject"])
    if subject["kind"] == "task":
        _integer(data["revision"], "revision", 1)
    elif data["revision"] is not None:
        raise LogContractError("command revision must be null")
    _uuid(data["authorizationRef"], "authorizationRef")
    _integer(data["authorizationEpoch"], "authorizationEpoch", 1)
    _digest(data["inputKey"], "inputKey")
    _text(data["skillName"], "skillName", maximum=128)
    _digest(data["skillDigest"], "skillDigest")
    for span in _array(data["sources"], "sources", maximum=128):
        _source_belongs(subject, parse_source_range(span)["source"], data["revision"])
    for gap in _array(data["sourceGaps"], "sourceGaps", maximum=128):
        _source_belongs(subject, parse_source_gap(gap)["source"], data["revision"])
    seen_ranges: list[tuple[bytes, int, int]] = []
    for span in [*data["sources"], *data["sourceGaps"]]:
        key = _physical_source_key(span["source"])
        if any(previous == key and start < span["end"] and end > span["start"]
               for previous, start, end in seen_ranges):
            raise LogContractError("summary request contains overlapping source ranges")
        seen_ranges.append((key, span["start"], span["end"]))
    if data["priorCumulativeEntryId"] is not None:
        _uuid(data["priorCumulativeEntryId"], "priorCumulativeEntryId")
    parse_health_snapshot(data["dbHealth"])
    _time(data["requestedAt"], "requestedAt")
    if not data["sources"] and not data["sourceGaps"]:
        raise LogContractError("summary request needs new source or gap")
    if data["inputKey"] != summary_input_key(subject, data["revision"], data["sources"], data["sourceGaps"], data["skillDigest"]):
        raise LogContractError("inputKey does not match immutable input")
    if len(canonical_json(data)) > MAX_BODY_BYTES:
        raise LogContractError("summary request exceeds 1MiB")
    return dict(data)


def summary_input_key(subject: SubjectRef, revision: int | None, sources: Sequence[SourceRange], gaps: Sequence[dict[str, Any]], skill_digest: str) -> str:
    parse_subject_ref(subject)
    _digest(skill_digest, "skillDigest")
    for span in sources:
        parse_source_range(span)
    for gap in gaps:
        parse_source_gap(gap)
    return sha256(canonical_json({"subject": subject, "revision": revision, "sources": list(sources), "sourceGaps": list(gaps), "skillDigest": skill_digest})).hexdigest()


def parse_summary_result(value: object) -> SummaryResult:
    data = _object(value, "SummaryResult", {"schemaVersion", "requestId", "inputKey", "skillDigest", "sources", "facts", "interpretations", "unknowns", "usage"})
    _uuid(data["requestId"], "requestId")
    _digest(data["inputKey"], "inputKey")
    _digest(data["skillDigest"], "skillDigest")
    for span in _array(data["sources"], "sources", maximum=128):
        parse_source_range(span)
    for key in ("facts", "interpretations", "unknowns"):
        _strings(data[key], key)
    usage = _object(data["usage"], "SummaryUsage", {"schemaVersion", "inputTokens", "outputTokens", "costUsd"})
    for key in ("inputTokens", "outputTokens"):
        if usage[key] is not None:
            _integer(usage[key], key)
    if usage["costUsd"] is not None and (type(usage["costUsd"]) is not str
                                        or len(usage["costUsd"]) > 32
                                        or _USD.fullmatch(usage["costUsd"]) is None):
        raise LogContractError("costUsd must be a bounded decimal string or null")
    if len(canonical_json(data)) > MAX_SUMMARY_RESULT_BYTES:
        raise LogContractError("summary result exceeds 8KiB")
    return dict(data)


class RawReader(Protocol):
    def read_raw(self, source: SourceRef, start: int, end: int) -> RawReadResult: ...


class SubjectEventSink(Protocol):
    def record_subject_event(self, subject: SubjectRef, event_id: str, source: SourceRef) -> None: ...


class ImportantStorePort(Protocol):
    def put_entry(self, entry: ImportantEntry) -> ImportantEntry: ...
    def get_entry(self, entry_id: str) -> ImportantEntry | None: ...
    def list_entries(self, subject: SubjectRef, *, limit: int = 100, after: str | None = None) -> list[ImportantEntry]: ...
    def health_snapshot(self) -> HealthSnapshot: ...


class SummaryAdapter(Protocol):
    def dispatch_summary(self, request: SummaryRequest) -> SummaryResult: ...


class RequestJournal(Protocol):
    def reserve_summary_request(self, request: SummaryRequest) -> None: ...
    def record_summary_state(self, request_id: str, state: str, *, detail: str | None = None) -> None: ...
    def record_summary_result_receipt(self, result: SummaryResult, entry_id: str) -> bool: ...
    def get_summary_request(self, request_id: str) -> dict[str, Any] | None: ...
    def list_unresolved_summary_requests(self, *, limit: int = 100, after: str | None = None) -> list[dict[str, Any]]: ...


class ObservationJournal(Protocol):
    def record_observation_boundary(self, boundary: ObservationBoundary) -> str: ...
    def record_gapfree_coverage(self, coverage: ObservationCoverage) -> str: ...
