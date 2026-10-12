"""Cursor-only summary scheduling in the existing backend.

The runtime/compatibility supplier is deliberately injected: an unavailable
parent epoch or output ceiling never becomes an inferred model configuration.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

from workbench.backend.summary_adapter import (
    COLLECTION_BYTES, SKILL_NAME, SDKSummaryAdapter, SourcePiece,
    SummaryDeferred, prepare_summary, skill_bytes,
)
from workbench.contracts.logs_v1 import canonical_json, parse_source_gap, summary_input_key
from workbench.storage.log_raw.port import raw_query

BUDGET_REASONS = frozenset(("input_budget_unavailable", "output_budget_unavailable"))


class SummarySourceCollector:
    """Read only confirmed ranges from fixed product stores, without a body cache."""
    def __init__(self, root: Path, subject_log: Callable[[], Any], *, segmented: Any = None):
        self.root, self.subject_log, self.segmented = Path(root), subject_log, segmented

    def _event(self, event_id: str) -> bytes | None:
        # The identity was minted by SubjectLifecycle from the durable seq.
        # Never take a pathname or an event identity from model output.
        for channel, name in (("flow", "tasks-flow.jsonl"), ("terminal", "handoffs.jsonl")):
            path = self.root / "workflow" / name
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
                    raise SummaryDeferred("unsafe_source")
                while line := stream.readline(COLLECTION_BYTES + 1):
                    if len(line) > COLLECTION_BYTES:
                        raise SummaryDeferred("collection_budget_exceeded")
                    value = json.loads(line)
                    seq = value.get("seq")
                    if type(seq) is int and str(uuid5(NAMESPACE_URL,
                            f"workbench-important-v1:{channel}:{seq}")) == event_id:
                        return canonical_json(value)
        return None

    def collect(self, journal: Any, subject: dict, records: list[dict]) -> tuple[list[SourcePiece], list[dict]]:
        pieces, gaps, collected = [], [], 0
        sink = self.subject_log()
        if sink is None:
            raise SummaryDeferred("subject_storage_unavailable")
        incomplete: dict[bytes, tuple[list[dict], bytes]] = {}
        for record in records:
            span = record["range"]
            size = span["end"] - span["start"]
            if collected + size > COLLECTION_BYTES:
                break  # remaining references stay pending for a later regular call
            source = span["source"]
            data = None
            missing_gaps = []
            partial = False
            if source["sourceKind"] == "terminal":
                kind = "run" if "runId" in source else "command"
                identity = source.get("runId", source.get("commandId"))
                chunks, offset = [], span["start"]
                try:
                    while offset < span["end"]:
                        end = min(span["end"], offset + 32768)
                        result = raw_query(self.root, kind, identity, offset=offset,
                                           limit=end - offset, segmented=self.segmented)
                        read = result["read"]
                        if result.get("log_format") == "segmented-v1":
                            if read.get("source") != source:
                                raise SummaryDeferred("source_attribution_unavailable")
                            coverage = []
                            for item in read["chunks"]:
                                chunk = base64.b64decode(item["dataBase64"], validate=True)
                                observed = item["range"]
                                if (observed["source"] != source or len(chunk) != observed["end"] - observed["start"]
                                        or sha256(chunk).hexdigest() != observed["digest"]):
                                    raise SummaryDeferred("source_digest_mismatch")
                                chunks.append(chunk)
                                coverage.append((observed["start"], observed["end"]))
                            for value in read["missing"]:
                                gap = parse_source_gap(value)
                                if gap["source"] != source:
                                    raise SummaryDeferred("source_attribution_unavailable")
                                missing_gaps.append(gap)
                                coverage.append((gap["start"], gap["end"]))
                            cursor = offset
                            for lo, hi in sorted(coverage):
                                if lo != cursor or not lo < hi <= end:
                                    raise SummaryDeferred("raw_range_unavailable")
                                cursor = hi
                            if cursor != end:
                                raise SummaryDeferred("raw_range_unavailable")
                            offset = end
                        else:
                            chunk = base64.b64decode(read["dataBase64"], validate=True)
                            if read["start"] != offset or read["end"] != offset + len(chunk):
                                raise SummaryDeferred("raw_range_unavailable")
                            chunks.append(chunk)
                            offset += len(chunk)
                            high = result["observed_bytes"]
                            if offset < span["end"] and offset >= high:
                                missing_gaps.append(parse_source_gap({"schemaVersion": 1, "source": source,
                                    "start": max(span["start"], high), "end": span["end"], "reason": "storage_gap"}))
                                break
                            if not chunk:
                                raise SummaryDeferred("raw_range_unavailable")
                    partial = bool(missing_gaps and any(chunks))
                    if not missing_gaps:
                        data = b"".join(chunks)
                except FileNotFoundError:
                    missing_gaps = [parse_source_gap({"schemaVersion": 1, "source": source,
                        "start": offset, "end": span["end"], "reason": "storage_gap"})]
                    partial = bool(any(chunks))
            elif source["sourceKind"] == "event":
                data = self._event(source["eventId"])
            else:
                identity = journal.summary_source_entry(record["source_id"])
                entry = sink.summary_entry(identity) if identity else None
                if entry is not None and entry["subject"] == subject:
                    facts = entry["body"]["facts"]
                    if len(facts) == 1:
                        data = facts[0].encode("utf-8")
            if (source["sourceKind"] == "decision" and "eventId" in source
                    and (data is None or len(data) != size or sha256(data).hexdigest() != span["digest"])):
                data = self._event(source["eventId"]) or data
            if missing_gaps:
                if canonical_json(source) in incomplete:
                    raise SummaryDeferred("source_encoding_unavailable")
                for gap in missing_gaps:
                    self._record_gap(sink, subject, span, gap, partial=partial)
                gaps.extend(missing_gaps)
                continue
            if data is None:
                raise SummaryDeferred("raw_missing")
            if len(data) != size or sha256(data).hexdigest() != span["digest"]:
                raise SummaryDeferred("source_digest_mismatch")
            collected += len(data)
            key = canonical_json(source)
            ranges = [span]
            if key in incomplete:
                ranges, prefix = incomplete.pop(key)
                if ranges[-1]["end"] != span["start"]:
                    raise SummaryDeferred("source_encoding_unavailable")
                ranges = [*ranges, span]
                data = prefix + data
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                if (source["sourceKind"] == "terminal" and exc.reason == "unexpected end of data"
                        and len(data) - exc.start <= 3):
                    incomplete[key] = (ranges, data)
                    continue  # the original references remain pending until a continuation arrives
                raise SummaryDeferred("source_encoding_unavailable") from None
            kind = ("terminal" if source["sourceKind"] == "terminal" else
                    "start_spec" if source["sourceKind"] in ("task_spec", "event") else
                    "report" if source["sourceKind"] == "report" else "decision")
            pieces.append(SourcePiece(ranges[0], text, kind, record["cursor"],
                                      tuple(ranges) if len(ranges) > 1 else ()))
        return pieces, gaps

    @staticmethod
    def _record_gap(sink: Any, subject: dict, span: dict, gap: dict, *, partial: bool = False) -> None:
        if gap["source"] != span["source"] or not span["start"] <= gap["start"] < gap["end"] <= span["end"]:
            raise SummaryDeferred("source_attribution_unavailable")
        # Confirm the exact attribution again before storing this read observation.
        # The gap is not a replacement for the original confirmed source span.
        with sink.catalog._lease() as fd:
            pointer = sink.catalog._current(fd)
            from contextlib import ExitStack
            from workbench.storage.important_log.store import ImportantLogStore
            found, observed = False, False
            with ExitStack() as stack:
                stores = [stack.enter_context(ImportantLogStore.open(sink.catalog.directory, pointer["active"], readonly=True))]
                stores += [stack.enter_context(sink.catalog._archive_source(fd, pointer, db)) for db in pointer["archives"]]
                for store in stores:
                    for row in store._connection.execute("SELECT sources_json,source_gaps_json FROM entries WHERE subject_kind=? AND subject_id=?",
                                                        (subject["kind"], subject["id"])):
                        found |= span in json.loads(row[0])
                        observed |= gap in json.loads(row[1])
            if not found:
                raise SummaryDeferred("source_attribution_unavailable")
        if observed:
            return
        written = sink.catalog.append(subject, "event", [], {"schemaVersion": 1,
            "facts": [], "interpretations": [], "unknowns": [
                "확인된 raw 범위를 읽을 수 없음" if not partial else
                "원 confirmed range 중 실제 available subrange가 원 observed digest로 검증 불가하여 생략됨"]}, source_gaps=[gap])
        if written.get("entry") is None:
            raise SummaryDeferred("raw_missing")


class SummaryScheduler:
    def __init__(self, *, repository_factory: Callable[[], Any], adapter: SDKSummaryAdapter,
                 subject_log: Callable[[], Any], collector: SummarySourceCollector,
                 authority_flags: Callable[[dict, dict | None], dict],
                 paused: Callable[[], bool], priority: Callable[[], bool],
                 runtime: Callable[[], dict | None] = lambda: None,
                 storage_ready: Callable[[], bool] = lambda: False,
                 dispatch_ready: Callable[[], bool] = lambda: True,
                 compatibility: Any = None, clock: Callable[[], float] = time.time):
        self.repository_factory, self.adapter = repository_factory, adapter
        self.subject_log, self.collector = subject_log, collector
        self.authority_flags, self.paused, self.priority = authority_flags, paused, priority
        self.runtime, self.storage_ready, self.compatibility, self.clock = runtime, storage_ready, compatibility, clock
        self.dispatch_ready = dispatch_ready
        self._lock = threading.Lock()
        self._job: threading.Thread | None = None
        self._closed = False
        self._status: dict[str, Any] = {"state": "unknown", "reason": "compatibility_unavailable",
                                      "latencySeconds": None, "inFlight": False, "newInfo": None,
                                      "usage": None, "lastHealthy": None}
        self._reconcile()

    def _reconcile(self) -> None:
        with self.repository_factory() as journal:
            after = None
            while rows := journal.list_unresolved_summary_requests(after=after):
                for row in rows:
                    request_id = row["request"]["requestId"]
                    if row["state"] == "prepared":
                        journal.record_summary_state(request_id, "not_sent", detail="restart before dispatch ownership")
                    elif row["state"] == "sending":
                        journal.record_summary_state(request_id, "unknown", detail="restart dispatch outcome unknown")
                after = rows[-1]["request"]["requestId"]
            self._apply_outcomes(journal)

    def _apply_outcomes(self, journal: Any) -> None:
        # A bounded observation page also sees late receipts during normal ticks.
        # Restart-only sending transitions are deliberately not repeated here.
        for outcome in journal.summary_unclassified_outcomes():
            kind = journal.summary_outcome_kind(outcome["state"], outcome["detail"])
            incident = journal.record_summary_control(outcome["request_id"], kind,
                                                      reason=outcome["detail"] or outcome["state"])
            if incident is not None and self.compatibility is not None:
                self.compatibility.protect(incident)


    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _admitted(self) -> bool:
        return (not self._closed and not self.paused() and not self.priority()
                and self.storage_ready() is True and self.compatibility is not None
                and self.compatibility.admission() is True)

    def _discover(self, journal: Any, now: float) -> None:
        sink = self.subject_log()
        if sink is None:
            return
        page = sink.summary_entries_page(journal.summary_ingest_cursor(), limit=32)
        for item in page["entries"]:
            entry, high = item["entry"], 0
            if entry["kind"] == "cumulative":
                continue
            for span in entry["sources"]:
                high = max(high, journal.ingest_summary_source(entry["subject"], span, entry["entryId"]))
            if high:
                end = None
                if entry["kind"] == "closure":
                    state = sink.state(entry["subject"])
                    authorization = sink.authority(entry["subject"], active=False, paused=False,
                                                   revoked=False, has_new_input=True, closing_event=True)
                    end = {"event_id": entry["entryId"], "input_cursor": high,
                        "authorization_ref": authorization.get("authorization_ref"),
                        "authorization_epoch": authorization.get("authorization_epoch"),
                        "reason": state.get("core_closed_reason") or state.get("command_end_status")}
                journal.observe_summary_cursor(entry["subject"], high, now=now, end_ref=end)
        journal.advance_summary_ingest(page["cursor"])

    def tick(self) -> dict:
        # Backend/60s review loop never waits for SQLite, source reads or model IO.
        with self._lock:
            if self._closed or self._job is not None and self._job.is_alive():
                return dict(self._status)
            self._status["busy"] = True
            self._job = threading.Thread(target=self._cycle, args=(self.clock(),),
                                         name="workbench-summary-request", daemon=True)
            self._job.start()
            return dict(self._status)

    def _cycle(self, now: float) -> None:
        try:
            with self.repository_factory() as journal:
                self._apply_outcomes(journal)
                self._discover(journal, now)
                control = journal.summary_control_state()
                if control["protection"]:
                    with self._lock:
                        self._status.update(state="protected", reason=control["protection"], inFlight=False)
                    return
                candidates = journal.summary_candidates(now=now, limit=32)
                if not candidates:
                    with self._lock:
                        self._status.update(state="idle", reason=None, inFlight=False)
                    return
                if not self._admitted():
                    with self._lock:
                        self._status.update(state="deferred", reason="priority_pause_or_compatibility", inFlight=False)
                    return
                candidate = candidates[0]
            with self._lock:
                self._status.update(state="running", reason=None, inFlight=True)
            self._execute(candidate)
        except Exception as exc:
            with self._lock:
                self._status.update(state="unknown", reason=type(exc).__name__, inFlight=False)
        finally:
            with self._lock:
                self._status["busy"] = False

    def _execute(self, candidate: dict) -> None:
        started, request_id, sent = time.monotonic(), None, False
        subject, end = candidate["subject"], candidate["end_ref"]
        reason, state = "summary_outcome_unknown", "unknown"
        try:
            with self.repository_factory() as journal:
                if not self._admitted():
                    raise SummaryDeferred("priority_pause_or_compatibility")
                sink = self.subject_log()
                if sink is None:
                    raise SummaryDeferred("subject_storage_unavailable")
                flags = self.authority_flags(subject, end)
                flags.update(paused=self.paused(), has_new_input=True, closing_event=end is not None)
                decision = sink.authority(subject, **flags)
                if decision.get("allowed") is not True:
                    raise SummaryDeferred(decision.get("reason", "authority_unavailable"))
                if end is not None and (end.get("authorization_ref") != decision["authorization_ref"]
                        or end.get("authorization_epoch") != decision["authorization_epoch"]):
                    raise SummaryDeferred("closing_authority_changed")
                records = journal.pending_summary_sources(subject, revision=decision.get("revision"))
                if not records:
                    journal.settle_summary_cursor(subject, now=self.clock(), pending=False, end_processed=True)
                    return
                pieces, gaps = self.collector.collect(journal, subject, records)
                runtime = self.runtime()
                if not isinstance(runtime, dict):
                    raise SummaryDeferred("model_metadata_unavailable")
                prior = journal.latest_summary_receipt(subject)
                prior_entry = sink.summary_entry(prior["entry_id"]) if prior else None
                if prior is not None and prior_entry is None:
                    raise SummaryDeferred("prior_cumulative_unavailable")
                digest = sha256(skill_bytes()).hexdigest()
                sources = [span for p in pieces for span in p.source_ranges]
                request = {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject,
                    "revision": decision.get("revision"), "authorizationRef": decision["authorization_ref"],
                    "authorizationEpoch": decision["authorization_epoch"], "skillName": SKILL_NAME,
                    "skillDigest": digest, "sources": sources, "sourceGaps": gaps,
                    "priorCumulativeEntryId": None if prior is None else prior["entry_id"],
                    "dbHealth": sink.catalog.health_snapshot()["active"],
                    "requestedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
                request["inputKey"] = summary_input_key(subject, request["revision"], sources, gaps, digest)
                prepared = prepare_summary(request, pieces, metadata=runtime.get("metadata"),
                    instructions=runtime.get("instructions"), prior_cumulative=None if prior is None else
                    {"entryId": prior["entry_id"], "body": prior_entry["body"]})
                request = prepared["request"]
                with self._lock:
                    self._status.update(newInfo={"sourceRanges": len(request["sources"]),
                        "sourceGaps": len(request["sourceGaps"]), "omittedRanges": len(prepared["omitted"])},
                        budget=dict(prepared["budget"]))
                def current() -> bool:
                    if not self._admitted():
                        return False
                    fresh = self.authority_flags(subject, end)
                    fresh.update(paused=self.paused(), has_new_input=True, closing_event=end is not None)
                    return sink.authorize_summary_request(request, **fresh).get("allowed") is True
                if self.dispatch_ready() is not True or current() is not True:
                    raise SummaryDeferred("authority_changed")
                journal.reserve_summary_request(request)
                request_id = request["requestId"]
                journal.record_summary_state(request_id, "sending")
                # Sending is deliberately persisted before the socket claim. Crash
                # here is unknown, never optimistically replayed after restart.
                token = SimpleNamespace(current=current, claim=current)
                response = self.adapter.dispatch_outcome(prepared, expected_peer=runtime["expected_peer"],
                                                        authority_token=token, writer=runtime["writer"])
                sent = response.get("sent") is not False  # absent evidence is unknown
                reason = str(response.get("reason", response.get("status", "summary_outcome_unknown")))[:256]
                budget = reason in BUDGET_REASONS
                row = journal.get_summary_request(request_id)
                if row["state"] == "stored":
                    state = "stored"
                    with self._lock:
                        self._status.update(lastHealthy=row["receipt"]["entry_id"],
                                            usage=(response.get("result") or {}).get("usage"))
                    incident = journal.record_summary_control(request_id, "stored", reason="stored")
                elif not sent:
                    journal.record_summary_state(request_id, "not_sent", detail=reason)
                    state, incident = "deferred", None
                    if budget or response.get("protected") is True:
                        incident = journal.record_summary_control(request_id, "transient" if budget else "safety", reason=reason)
                elif response.get("status") == "rejected":
                    journal.record_summary_state(request_id, "executed_observed")
                    journal.record_summary_state(request_id, "rejected", detail=reason)
                    state = "rejected"
                    incident = journal.record_summary_control(request_id,
                        journal.summary_outcome_kind("rejected", reason), reason=reason)
                elif response.get("status") == "storage_failed":
                    journal.record_summary_state(request_id, "executed_observed")
                    journal.record_summary_state(request_id, "storage_failed", detail=reason)
                    state = "storage_failed"
                    incident = journal.record_summary_control(request_id, "storage", reason=reason)
                else:
                    journal.record_summary_state(request_id, "unknown", detail=reason)
                    state = "unknown"
                    incident = journal.record_summary_control(request_id,
                        journal.summary_outcome_kind("unknown", reason), reason=reason)
                if incident is not None:
                    self.compatibility.protect(incident)
                pending = bool(journal.pending_summary_sources(subject, revision=decision.get("revision")))
                journal.settle_summary_cursor(subject, now=self.clock(), pending=pending, end_processed=end is not None)
                if sent and end is not None and subject["kind"] == "command":
                    sink.mark_command_end_summary_processed(subject["id"])
        except SummaryDeferred as exc:
            reason, state = str(exc)[:256], "deferred"
            with self.repository_factory() as journal:
                if request_id is not None and journal.get_summary_request(request_id)["state"] == "sending":
                    journal.record_summary_state(request_id, "not_sent", detail=reason)
                journal.settle_summary_cursor(subject, now=self.clock(), pending=True, end_processed=end is not None)
        except Exception as exc:
            reason = type(exc).__name__
            with self.repository_factory() as journal:
                if request_id is not None:
                    row = journal.get_summary_request(request_id)
                    if row["state"] == "sending":
                        journal.record_summary_state(request_id, "unknown", detail=reason)
                    # A writer may already have committed. Never append again.
                    state = row["state"] if row["state"] == "stored" else "unknown"
                journal.settle_summary_cursor(subject, now=self.clock(), pending=True, end_processed=end is not None)
        finally:
            with self._lock:
                self._status.update(state=state, reason=reason, inFlight=False,
                                    latencySeconds=time.monotonic() - started, requestId=request_id)

    def recover(self, verify_ref: str) -> dict:
        if self.compatibility is None or self.compatibility.verify_reference(verify_ref) is not True:
            raise SummaryDeferred("verified_boundary_reference_unavailable")
        with self.repository_factory() as journal:
            return journal.begin_summary_recovery(verify_ref, boundaries_verified=True)

    def close(self, timeout: float = 31) -> dict:
        with self._lock:
            self._closed = True
            job = self._job
        if job is not None:
            job.join(timeout)
        return {"stopped": job is None or not job.is_alive(), "inFlight": job is not None and job.is_alive()}
