"""Disposable 250 MB LG-D18 direct-read and writer contention experiment.

Run with PYTHONPATH=src python tests/dev_validation/snapshot_priority_measurement.py.
The JSON result is intentionally retained even when a criterion fails. The fixture is
made below the run's private result directory and removed on normal/error exit.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import weakref

from workbench.backend.service import Backend
from workbench.storage.important_log import (
    ActiveSubjectLog, ImportantLogCatalog, ImportantLogReader, ImportantLogStore)
import workbench.storage.important_log.catalog as catalog_module


RESULT_DIR = (Path(__file__).resolve().parents[2] / ".workflow/dev-validation-log-retention/runs/"
              "dvl07-direct-20261010/fix01")
WRITER_LEASE_VALUES = (0.1, 2.0)  # existing default and experiment; no product limit adoption
READER_LEASE_S = 0.1
K_VALUES = (1, 2)
WRITES_PER_K = 40
WINDOW_S = 30.0
BACKEND_WATCHDOG_S = 5.0
TARGET_LOGICAL_BYTES = 250_000_000
BODY = {"schemaVersion": 1, "facts": ["measurement"], "interpretations": [], "unknowns": []}


def inventory(directory: Path) -> dict[str, dict[str, int | str]]:
    result = {}
    for path in sorted(directory.iterdir()):
        if path.is_file():
            digest = sha256()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            result[path.name] = {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}
    return result


def make_fixture(directory: Path) -> tuple[str, sqlite3.Connection, bytes, int]:
    ImportantLogCatalog(directory, create=True, target_bytes=500_000_000,
                        low_water_bytes=300_000_000)
    identity = json.loads((directory / "active.json").read_text())["active"]
    connection = sqlite3.connect(directory / (identity + ".sqlite3"), timeout=10,
                                 isolation_level=None, check_same_thread=False)
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE synthetic_probe(id INTEGER PRIMARY KEY,payload BLOB NOT NULL)")
    payload = os.urandom(1_000_000)
    logical = 0
    while logical < TARGET_LOGICAL_BYTES:
        connection.execute("BEGIN IMMEDIATE")
        for _ in range(8):
            connection.execute("INSERT INTO synthetic_probe(payload) VALUES(?)", (payload,))
        connection.execute("COMMIT")
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        freelist = connection.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        logical = (pages - freelist) * page_size
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    latest = ("latest-wal-" + uuid4().hex).encode()
    connection.execute("INSERT INTO synthetic_probe(payload) VALUES(?)", (latest,))
    assert (directory / (identity + ".sqlite3-wal")).stat().st_size > 0
    return identity, connection, latest, logical


def controlled(reader: ImportantLogReader, identity: str, directory: Path,
               scratch: Path, latest: bytes) -> dict:
    before = inventory(directory)
    status = reader.status()
    with patch.object(reader, "_copy_file", side_effect=AssertionError("active snapshot copied")):
        with reader._view(identity) as store:
            found = store._connection.execute(
                "SELECT payload FROM synthetic_probe ORDER BY id DESC LIMIT 1").fetchone()[0]
    clean_after_read = not list(scratch.iterdir())
    after = inventory(directory)
    original_content_unchanged = all(after[name]["sha256"] == value["sha256"]
                                     for name, value in before.items()
                                     if name.startswith(identity + ".sqlite3") and
                                     (name.endswith(".sqlite3") or name.endswith(".sqlite3-wal")))
    return {"logicalLatestWalReflected": found == latest,
            "existingMainWalUnchanged": original_content_unchanged,
            "newDatabaseCount": sum(name.endswith(".sqlite3") for name in after)
                                - sum(name.endswith(".sqlite3") for name in before),
            "activeCopiedBytes": 0,
            "normalScratchClean": clean_after_read,
            "statusUnknown": status["databases"][0]["availability"] == "unknown",
            "statusComplete": status["complete"],
            "sourcePhysicalBytes": sum(value["bytes"] for name, value in before.items()
                                       if name.startswith(identity + ".sqlite3"))}


def backend_seam(directory: Path, writer_timeout: float) -> Backend:
    # Calls the real _refresh_important_storage body, including its catalog open,
    # maintenance, recovery decision and health call. Full _open is a separate gate.
    backend = Backend.__new__(Backend)
    backend.layout = SimpleNamespace(important_logs=directory)
    backend.important_writer_lease_timeout_s = writer_timeout
    backend.important_catalog = None
    backend.subject_log = None
    backend.important_storage_status = {"state": "not_open"}
    backend._important_storage_at = 0.0
    backend._important_capture_unknown = False
    backend._important_binding_lock = threading.Lock()
    backend._important_workflows = weakref.WeakSet()
    backend.flow = None
    backend.terminal = None
    return backend


def distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {"count": len(values), "minimum": ordered[0],
            "median": statistics.median(ordered),
            "p95": ordered[min(len(ordered)-1, int(0.95 * len(ordered)))],
            "maximum": ordered[-1]}


def final_counts(directory: Path, subject: dict, entry_ids: list[str],
                 entry_markers: list[str], event_keys: list[str], writer_timeout: float) -> dict:
    snapshot = ImportantLogCatalog(directory, target_bytes=500_000_000,
                                   low_water_bytes=300_000_000,
                                   writer_lease_timeout_s=writer_timeout).catalog_snapshot()
    entry_counts: Counter[str] = Counter()
    marker_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    for identity in [snapshot["active"], *snapshot["archives"]]:
        with ImportantLogStore.open(directory, identity, readonly=True) as store:
            conn = store._connection
            for entry in store.list_entries(subject, limit=100):
                if entry["entryId"] in entry_ids:
                    entry_counts[entry["entryId"]] += 1
                for marker in entry["body"]["facts"]:
                    if marker in entry_markers:
                        marker_counts[marker] += 1
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='subject_events_v1'").fetchone():
                for row in conn.execute("SELECT event_key FROM subject_events_v1"):
                    if row[0] in event_keys:
                        event_counts[row[0]] += 1
    return {"entriesExactlyOnce": all(marker_counts[x] == 1 for x in entry_markers),
            "returnedEntriesExactlyOnce": all(entry_counts[x] == 1 for x in entry_ids),
            "eventsExactlyOnce": all(event_counts[x] == 1 for x in event_keys),
            "entryCount": len(entry_markers), "eventCount": len(event_keys),
            "returnedEntryCount": len(entry_ids),
            "entryStoredCount": sum(marker_counts.values()),
            "eventStoredCount": sum(event_counts.values()),
            "entryMissing": [x for x in entry_markers if marker_counts[x] == 0],
            "entryDuplicate": [x for x in entry_markers if marker_counts[x] > 1],
            "eventMissing": [x for x in event_keys if event_counts[x] == 0],
            "eventDuplicate": [x for x in event_keys if event_counts[x] > 1],
            "generation": snapshot["generation"], "archiveCount": len(snapshot["archives"])}


def run_case(k: int, writer_timeout: float, directory: Path, identity: str, scratch: Path) -> dict:
    reader = ImportantLogReader(directory)
    writer = ImportantLogCatalog(directory, target_bytes=500_000_000,
                                 low_water_bytes=300_000_000,
                                 writer_lease_timeout_s=writer_timeout)
    sink = ActiveSubjectLog(writer)
    backend = backend_seam(directory, writer_timeout)
    subject = {"schemaVersion": 1, "kind": "task", "id": str(uuid4())}
    start = threading.Event()
    stop = threading.Event()
    lock = threading.Lock()
    reader_completed: list[float] = []
    reader_busy = []
    reader_errors = []
    waits: list[dict] = []
    reader_leases: list[dict] = []
    flock_spans: list[dict] = []
    pending_flocks: dict[tuple[int, int], dict] = {}
    flock_lock = threading.Lock()
    original_flock = fcntl.flock
    view_role = threading.local()
    responses = []
    entry_ids: list[str] = []
    entry_markers: list[str] = []
    event_keys: list[str] = []
    writer_failures = []
    backend_calls = []
    original_lease = ImportantLogCatalog._lease
    original_reader_catalog = reader._catalog
    original_reader_view = reader._view
    wal_path = directory / f"{identity}.sqlite3-wal"
    wal_before = wal_path.stat().st_size if wal_path.exists() else 0

    @contextmanager
    def measured_reader_view(view_identity):
        view_role.identity = view_identity
        try:
            with original_reader_view(view_identity) as store:
                yield store
        finally:
            view_role.identity = None

    @contextmanager
    def measured_reader_catalog():
        t0 = time.monotonic()
        acquired = False
        role = "page" if getattr(view_role, "identity", None) is not None else "catalog"
        try:
            with original_reader_catalog() as state:
                acquired = True
                t1 = time.monotonic()
                try:
                    yield state
                finally:
                    reader_leases.append({"role": role, "wait": t1-t0,
                                          "held": time.monotonic()-t1, "acquired": True})
        except Exception:
            if not acquired:
                reader_leases.append({"role": role, "wait": time.monotonic()-t0,
                                      "held": 0.0, "acquired": False})
            raise

    @contextmanager
    def measured_lease(self, *, create=False):
        t0 = time.monotonic()
        acquired = False
        try:
            with original_lease(self, create=create) as fd:
                acquired = True
                waits.append({"seconds": time.monotonic() - t0, "acquired": True})
                yield fd
        except Exception:
            if not acquired:
                waits.append({"seconds": time.monotonic() - t0, "acquired": False})
            raise

    def measured_flock(fd, operation):
        name = threading.current_thread().name
        if not (name.startswith("reader-") or name.startswith("writer-")):
            return original_flock(fd, operation)
        key = (threading.get_ident(), fd)
        if operation & fcntl.LOCK_EX:
            with flock_lock:
                pending_flocks.setdefault(key, {
                    "role": "page" if name.startswith("reader-") and
                    getattr(view_role, "identity", None) is not None else
                    "catalog" if name.startswith("reader-") else "writer",
                    "firstAttempt": time.monotonic(), "acquiredAt": None})
            result = original_flock(fd, operation)
            with flock_lock:
                pending_flocks[key]["acquiredAt"] = time.monotonic()
            return result
        if operation == fcntl.LOCK_UN:
            result = original_flock(fd, operation)
            released = time.monotonic()
            with flock_lock:
                pending = pending_flocks.pop(key, None)
                if pending is not None:
                    acquired = pending["acquiredAt"]
                    flock_spans.append({"role": pending["role"],
                                        "wait": (acquired or released) - pending["firstAttempt"],
                                        "held": 0.0 if acquired is None else released - acquired,
                                        "acquired": acquired is not None})
            return result
        return original_flock(fd, operation)

    def read_loop():
        start.wait()
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                page = reader.show(subject, limit=10)
                if page["complete"]:
                    with lock:
                        reader_completed.append(time.monotonic() - t0)
                else:
                    reasons = [x["reason"] for x in page["unavailable"]]
                    with lock:
                        (reader_busy if reasons and all(x == "busy" for x in reasons)
                         else reader_errors).append(str(reasons))
            except Exception as exc:
                with lock:
                    (reader_busy if "busy" in str(exc).lower() else reader_errors).append(
                        type(exc).__name__ + ":" + str(exc))

    def refresh(label: str):
        backend._important_storage_at = 0.0
        t0 = time.monotonic()
        def fixture_catalog(path, **options):
            return ImportantLogCatalog(path, target_bytes=500_000_000,
                                       low_water_bytes=300_000_000, **options)
        with patch("workbench.backend.service.ImportantLogCatalog", side_effect=fixture_catalog):
            backend._refresh_important_storage()
        duration = time.monotonic() - t0
        backend_calls.append({"route": label, "seconds": duration,
                              "state": backend.important_storage_status.get("state"),
                              "maintenance": (backend.important_storage_status.get("maintenance") or {}).get("state"),
                              "rollover": bool((backend.important_storage_status.get("maintenance") or {}).get("rollover")),
                              "checkpoint": ((backend.important_storage_status.get("maintenance") or {})
                                             .get("work") or {}).get("checkpoint")})

    def write_loop():
        start.wait()
        refresh("real_backend_refresh_initial_catalog_open")
        for index in range(WRITES_PER_K):
            due = started_at[0] + 0.5 + index * 0.65
            if due > time.monotonic():
                time.sleep(due - time.monotonic())
            if index == 20:
                refresh("real_backend_refresh_existing_catalog")
            t0 = time.monotonic()
            try:
                if index % 2:
                    message_id = str(uuid4())
                    event_keys.append("message:" + message_id)
                    stored = sink.record_delivery(
                        subject, message_id=message_id, revision=1,
                        kind="manager_instruction", content=f"known-{k}-{index}",
                        delivered_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
                    if not stored:
                        writer_failures.append(f"event_{index}_not_stored")
                else:
                    marker = f"attempt:{writer_timeout}:{k}:{index}:{subject['id']}"
                    entry_markers.append(marker)
                    body = {**BODY, "facts": [marker]}
                    result = writer.append(subject, "event", [], body)
                    if result["state"] == "ok":
                        entry_ids.append(result["entry"]["entryId"])
                    else:
                        writer_failures.append(f"append_{index}:{result['state']}")
            except Exception as exc:
                writer_failures.append(f"write_{index}:{type(exc).__name__}:{exc}")
            responses.append(time.monotonic() - t0)

    threads = [threading.Thread(target=read_loop, name=f"reader-{k}-{i}") for i in range(k)]
    threads.append(threading.Thread(target=write_loop, name=f"writer-{k}"))
    started_at = [0.0]
    with patch.object(ImportantLogCatalog, "_lease", measured_lease), \
         patch.object(reader, "_catalog", side_effect=measured_reader_catalog), \
         patch.object(reader, "_view", side_effect=measured_reader_view), \
         patch.object(fcntl, "flock", measured_flock):
        for thread in threads:
            thread.start()
        started_at[0] = time.monotonic()
        start.set()
        deadline = started_at[0] + WINDOW_S
        if deadline > time.monotonic():
            time.sleep(deadline - time.monotonic())
        stop.set()
        for thread in threads:
            thread.join(timeout=BACKEND_WATCHDOG_S)
    thread_alive = [thread.name for thread in threads if thread.is_alive()]
    if thread_alive:
        # Do not return workspace ownership with a live mutating writer.
        for thread in threads:
            thread.join()
    counts = final_counts(directory, subject, entry_ids, entry_markers,
                          event_keys, writer_timeout)
    wal_after = wal_path.stat().st_size if wal_path.exists() else 0
    page_leases = [x for x in reader_leases if x["role"] == "page"]
    page_flocks = [x for x in flock_spans if x["role"] == "page"]
    catalog_flocks = [x for x in flock_spans if x["role"] == "catalog"]
    writer_flocks = [x for x in flock_spans if x["role"] == "writer"]
    return {"k": k, "writerTimeoutSeconds": writer_timeout,
            "observedSeconds": WINDOW_S, "writesRequested": WRITES_PER_K,
            "subject": subject,
            "writesReturned": len(responses), "writerFailures": writer_failures,
            "writerLeaseWaitSeconds": distribution([x["seconds"] for x in waits]),
            "writerLeaseAcquisitionFailures": sum(not x["acquired"] for x in waits),
            "writerCallSeconds": distribution(responses),
            "readerPageLeaseWaitSeconds": distribution([x["wait"] for x in page_leases]),
            "readerPageLeaseHeldSeconds": distribution([x["held"] for x in page_leases if x["acquired"]]),
            "readerPageLeaseAcquisitionFailures": sum(not x["acquired"] for x in page_leases),
            "readerCatalogLeaseWaitSeconds": distribution([x["wait"] for x in reader_leases if x["role"] == "catalog"]),
            "readerCatalogLeaseHeldSeconds": distribution([x["held"] for x in reader_leases
                                                              if x["role"] == "catalog" and x["acquired"]]),
            "actualPageExWaitSeconds": distribution([x["wait"] for x in page_flocks]),
            "actualPageExHeldSeconds": distribution([x["held"] for x in page_flocks if x["acquired"]]),
            "actualPageExAcquisitionFailures": sum(not x["acquired"] for x in page_flocks),
            "actualCatalogExWaitSeconds": distribution([x["wait"] for x in catalog_flocks]),
            "actualCatalogExHeldSeconds": distribution([x["held"] for x in catalog_flocks if x["acquired"]]),
            "actualWriterExWaitSeconds": distribution([x["wait"] for x in writer_flocks]),
            "actualWriterExHeldSeconds": distribution([x["held"] for x in writer_flocks if x["acquired"]]),
            "actualWriterExAcquisitionFailures": sum(not x["acquired"] for x in writer_flocks),
            "backendCalls": backend_calls,
            "backendCaptureUnknown": bool(backend.important_storage_status.get("subjectCaptureUnknown")
                                          or backend.important_storage_status.get("state") == "capture_unknown"),
            "standaloneSinkLastFailure": sink.last_failure,
            "backendWatchdogExceeded": any(x["seconds"] > BACKEND_WATCHDOG_S for x in backend_calls),
            "readerCompleted": len(reader_completed), "readerBusy": len(reader_busy),
            "readerOtherErrors": reader_errors,
            "readerViewSeconds": distribution(reader_completed),
            "threadsAliveAfterWatchdog": thread_alive,
            "scratchCleanAfterConcurrency": not list(scratch.iterdir()),
            "walBytesBefore": wal_before, "walBytesAfter": wal_after,
            "final": counts}


def multi_db_cost(directory: Path, scratch: Path) -> dict:
    catalog = ImportantLogCatalog(directory, create=True, target_bytes=1_000_000,
                                  low_water_bytes=500_000)
    subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
    for _ in range(2):
        assert catalog.append(subject, "event", [], BODY)["state"] == "ok"
    old = catalog.active_uuid()
    rollover_start = time.monotonic()
    with catalog._lease() as fd:
        pointer = catalog._current(fd)
        with ImportantLogStore.open(directory, old, _catalog_owned=True) as store:
            rollover = catalog._rotate(fd, pointer, store)
    rollover_seconds = time.monotonic() - rollover_start
    for _ in range(2):
        assert catalog.append(subject, "event", [], BODY)["state"] == "ok"
    archive_before = {name: value for name, value in inventory(directory).items()
                      if name.startswith(old + ".sqlite3")}
    reader = ImportantLogReader(directory)
    copied = []
    original_copy = reader._copy_file

    def measured_copy(fd, name, destination):
        value = original_copy(fd, name, destination)
        copied.append(destination.stat().st_size)
        return value

    def timed(call):
        copied.clear()
        t0 = time.monotonic()
        value = call()
        return value, {"seconds": time.monotonic()-t0,
                       "copiedBytes": sum(copied)}

    with patch.object(reader, "_copy_file", side_effect=measured_copy):
        status, status_cost = timed(reader.status)
        subjects, list_cost = timed(lambda: reader.list_subjects(limit=1))
        first, first_cost = timed(lambda: reader.show(subject, limit=2))
        second, second_cost = timed(lambda: reader.show(
            subject, limit=2, after=first["nextAfter"]))
    archive_after = {name: value for name, value in inventory(directory).items()
                     if name.startswith(old + ".sqlite3")}
    failed_archive_copy = False
    with patch.object(reader, "_copy_file", side_effect=OSError("injected archive copy failure")):
        failed = reader.show(subject, limit=2)
        failed_archive_copy = (not failed["complete"] and
                               any(item["dbUuid"] == old for item in failed["unavailable"]))
    return {"databaseCount": status["databaseCount"],
            "status": status_cost, "list": list_cost,
            "showPage1": first_cost, "showPage2": second_cost,
            "entriesReturned": len(first["entries"]) + len(second["entries"]),
            "allComplete": all(x["complete"] for x in (status, subjects, first, second)),
            "archiveBytesAndInventoryUnchanged": archive_after == archive_before,
            "archiveCopyFailureObserved": failed_archive_copy,
            "rolloverSeconds": rollover_seconds,
            "rolloverCheckpoint": rollover.get("checkpoint"),
            "scratchClean": not list(scratch.iterdir())}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("--fix01-probe", "--fix01-final"):
        raise SystemExit("usage: snapshot_priority_measurement.py --fix01-probe|--fix01-final")
    probe_only = sys.argv[1] == "--fix01-probe"
    selected = ([(0.1, 2, 0.01), (0.1, 2, 0.001)] if probe_only else
                [(writer_timeout, k, catalog_module._WRITER_LEASE_RECHECK_SECONDS)
                 for writer_timeout in WRITER_LEASE_VALUES for k in K_VALUES])
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    result_path = RESULT_DIR / ("poll-probe01.json" if probe_only else "final-measurement01.json")
    result: dict = {"experiment": {"writerLeaseSeconds": [item[0] for item in selected],
        "readerLeaseSeconds": READER_LEASE_S, "sqliteBusySeconds": 0.1,
        "readerCounts": [item[1] for item in selected],
        "writerPollSeconds": [item[2] for item in selected],
        "writesPerCase": WRITES_PER_K,
        "windowSeconds": WINDOW_S, "backendWatchdogSeconds": BACKEND_WATCHDOG_S,
        "targetLogicalBytes": TARGET_LOGICAL_BYTES},
        "backendCoverage": "real _refresh_important_storage body: initial catalog open and existing refresh; fixture target 500MB injected via constructor factory; full Backend._open not exercised in this reduced seam",
        "baseline": "same 250MB fixture and reader loop pacing, R40/T30/watchdog5; original impl01 JSONs remain unchanged",
        "flockTiming": "fcntl.flock successful EX return to LOCK_UN return includes pre-yield catalog metadata and active open/page/close; first EX attempt to success is wait",
        "probeOnly": probe_only,
        "unmeasured": ["cold cache", "slow storage", "other filesystems", "hung reader"],
        "cases": []}
    original_tempdir = tempfile.tempdir
    connection = None
    try:
        with tempfile.TemporaryDirectory(dir=RESULT_DIR, prefix="priority-fixture-") as fixture:
            root = Path(fixture)
            directory = root / "important"
            scratch = root / "read-scratch"
            scratch.mkdir(mode=0o700)
            tempfile.tempdir = str(scratch)
            identity, connection, latest, logical = make_fixture(directory)
            result["logicalLiveBytes"] = logical
            result["controlled"] = controlled(ImportantLogReader(directory), identity,
                                               directory, scratch, latest)
            for writer_timeout, k, poll_seconds in selected:
                with patch.object(catalog_module, "_WRITER_LEASE_RECHECK_SECONDS", poll_seconds):
                    case = run_case(k, writer_timeout, directory, identity, scratch)
                case["writerPollSeconds"] = poll_seconds
                result["cases"].append(case)
            reader = ImportantLogReader(directory)
            t0 = time.monotonic()
            status = reader.status()
            result["statusCall"] = {"seconds": time.monotonic()-t0,
                "complete": status["complete"], "unknown": all(x["availability"] == "unknown"
                for x in status["databases"] if x["role"] != "orphan")}
            copied = []
            original_copy = reader._copy_file

            def measured_copy(fd, name, destination):
                value = original_copy(fd, name, destination)
                copied.append(destination.stat().st_size)
                return value

            page_calls = []
            after = None
            with patch.object(reader, "_copy_file", side_effect=measured_copy):
                while True:
                    t0 = time.monotonic()
                    page = reader.show(result["cases"][-1]["subject"], limit=10, after=after)
                    page_calls.append({"seconds": time.monotonic()-t0,
                                       "copiedBytes": sum(copied),
                                       "entries": len(page["entries"]),
                                       "complete": page["complete"]})
                    copied.clear()
                    after = page["nextAfter"]
                    if after is None or len(page_calls) > 10:
                        break
            result["pageCost"] = {"calls": page_calls,
                "entriesReturned": sum(x["entries"] for x in page_calls),
                "scratchClean": not list(scratch.iterdir())}
            result["multiDbCost"] = multi_db_cost(root / "multi", scratch)
            result["scratchCleanFinal"] = not list(scratch.iterdir())
            connection.close()
            connection = None
    except Exception as exc:
        result["harnessError"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if connection is not None:
            connection.close()
        tempfile.tempdir = original_tempdir
        result["fixtureCleaned"] = not any(RESULT_DIR.glob("priority-fixture-*"))
        result["passedMeasuredConditions"] = None if probe_only else (
            "harnessError" not in result
            and all(result.get("controlled", {}).get(name) for name in (
                "logicalLatestWalReflected", "existingMainWalUnchanged",
                "normalScratchClean", "statusUnknown"))
            and result.get("controlled", {}).get("newDatabaseCount") == 0
            and result.get("controlled", {}).get("activeCopiedBytes") == 0
            and len(result["cases"]) == len(selected)
            and result.get("statusCall", {}).get("unknown")
            and result.get("pageCost", {}).get("entriesReturned") == sum((
                result["cases"][-1]["final"]["entryCount"],
                result["cases"][-1]["final"]["eventCount"]))
            and result.get("pageCost", {}).get("scratchClean")
            and all(call["complete"] for call in result.get("pageCost", {}).get("calls", []))
            and result.get("multiDbCost", {}).get("databaseCount") == 2
            and result.get("multiDbCost", {}).get("entriesReturned") == 4
            and result.get("multiDbCost", {}).get("allComplete")
            and result.get("multiDbCost", {}).get("archiveBytesAndInventoryUnchanged")
            and result.get("multiDbCost", {}).get("archiveCopyFailureObserved")
            and result.get("multiDbCost", {}).get("scratchClean")
            and all(case["writesReturned"] == WRITES_PER_K
                and not case["writerFailures"] and not case["backendWatchdogExceeded"]
                and not case["backendCaptureUnknown"]
                and not case["readerOtherErrors"] and not case["threadsAliveAfterWatchdog"]
                and case["scratchCleanAfterConcurrency"]
                and all(call["state"] == "ready" for call in case["backendCalls"])
                and case["final"]["entriesExactlyOnce"]
                and case["final"]["eventsExactlyOnce"] for case in result["cases"]))
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"result": str(result_path), "passedMeasuredConditions": result["passedMeasuredConditions"],
                      "harnessError": result.get("harnessError")}, sort_keys=True))


if __name__ == "__main__":
    main()
