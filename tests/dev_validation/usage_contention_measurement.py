"""Disposable LG-D20 active/sealed-archive usage contention measurement.

Run with PYTHONPATH=src python tests/dev_validation/usage_contention_measurement.py.
The fixed schedule is recorded in the adjacent run's experiment-plan.json before use.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from snapshot_priority_measurement import (
    backend_seam, controlled, distribution, inventory, make_fixture)
from workbench.storage.important_log import (
    ActiveSubjectLog, ImportantLogCatalog, ImportantLogReader, ImportantLogStore)
from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.workflow.run import TaskWorkflow


RESULT_DIR = (Path(__file__).resolve().parents[2] / ".workflow/dev-validation-log-retention/"
              "runs/dvl07-sealed-20261010/impl01")
WINDOW = 35.0
WATCHDOG = 5.0
WRITES = 40
HUMAN_DUE = (4.0, 10.25, 24.0)
AGENT_DUE = (1.0, 5.0, 9.0, 13.0, 17.0, 21.0, 25.0, 29.0, 33.0)
STATUS_DUE = (2.0, 7.0, 12.0, 17.0, 22.0, 27.0, 32.0)


def archive_inventory(directory: Path, identity: str) -> dict:
    return {name: value for name, value in inventory(directory).items()
            if name.startswith(identity + ".sqlite3")}


def setup_fixture(directory: Path, scenario: str) -> tuple[dict, sqlite3.Connection]:
    old, connection, latest, logical = make_fixture(directory)
    subject = {"schemaVersion": 1, "kind": "task", "id": str(uuid4())}
    fixture = {"subject": subject, "logicalLiveBytes": logical,
               "largeDbUuid": old, "archiveUuid": None, "rollover": None,
               "seedEntryIds": [], "latestPayload": latest.decode()}
    if scenario == "active_only":
        fixture["activeUuid"] = old
        return fixture, connection

    catalog = ImportantLogCatalog(directory, target_bytes=500_000_000,
                                  low_water_bytes=300_000_000)
    for index in range(4):
        body = {"schemaVersion": 1, "facts": [f"archive-seed-{index}"],
                "interpretations": [], "unknowns": []}
        result = catalog.append(subject, "event", [], body)
        assert result["state"] == "ok", result
        fixture["seedEntryIds"].append(result["entry"]["entryId"])
    connection.close()
    with catalog._lease() as fd:
        pointer = catalog._current(fd)
        with ImportantLogStore.open(directory, old, _catalog_owned=True) as store:
            rollover = catalog._rotate(fd, pointer, store)
    assert rollover["checkpoint"] == "complete", rollover
    fixture["rollover"] = rollover
    fixture["archiveUuid"] = old
    fixture["activeUuid"] = rollover["to"]
    for index in range(2):
        body = {"schemaVersion": 1, "facts": [f"active-seed-{index}"],
                "interpretations": [], "unknowns": []}
        result = catalog.append(subject, "event", [], body)
        assert result["state"] == "ok", result
        fixture["seedEntryIds"].append(result["entry"]["entryId"])
    # Keep a genuinely latest WAL frame in the new active for a controlled read.
    active = fixture["activeUuid"]
    connection = sqlite3.connect(directory / f"{active}.sqlite3", timeout=10,
                                 isolation_level=None, check_same_thread=False)
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE synthetic_probe(id INTEGER PRIMARY KEY,payload BLOB NOT NULL)")
    latest_active = ("latest-active-wal-" + uuid4().hex).encode()
    connection.execute("INSERT INTO synthetic_probe(payload) VALUES(?)", (latest_active,))
    fixture["latestPayload"] = latest_active.decode()
    return fixture, connection


def controlled_archive(reader: ImportantLogReader, fixture: dict,
                       directory: Path, scratch: Path) -> dict:
    archive = fixture["archiveUuid"]
    if archive is None:
        return {"applicable": False}
    before = archive_inventory(directory, archive)
    with reader._view(archive) as store:
        latest = store._connection.execute(
            "SELECT payload FROM synthetic_probe ORDER BY id DESC LIMIT 1").fetchone()[0]
        seeded = store.list_entries(fixture["subject"], limit=20)
    after = archive_inventory(directory, archive)
    return {"applicable": True, "largeArchiveBytes": sum(x["bytes"] for x in before.values()),
            "latestWalReflectedAfterRollover": latest.decode().startswith("latest-wal-"),
            "seedEntriesVisible": len(seeded) == 4,
            "archiveInventoryAndBytesUnchanged": before == after,
            "scratchClean": not list(scratch.iterdir())}


def final_attempt_counts(directory: Path, subject: dict, entry_ids: list[str],
                         entry_markers: list[str], event_keys: list[str]) -> dict:
    """Verify all attempts through the same protected reader path as the workload.

    The earlier verifier opened a sealed source archive in ordinary ro mode and
    created WAL/SHM sidecars. The reader's private archive view avoids that.
    """
    snapshot = ImportantLogCatalog(directory, target_bytes=500_000_000,
                                   low_water_bytes=300_000_000).catalog_snapshot()
    reader = ImportantLogReader(directory)
    entry_counts: Counter[str] = Counter()
    marker_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    for identity in [snapshot["active"], *snapshot["archives"]]:
        with reader._view(identity) as store:
            for entry in store.list_entries(subject, limit=100):
                if entry["entryId"] in entry_ids:
                    entry_counts[entry["entryId"]] += 1
                for marker in entry["body"]["facts"]:
                    if marker in entry_markers:
                        marker_counts[marker] += 1
            if store._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='subject_events_v1'").fetchone():
                for row in store._connection.execute("SELECT event_key FROM subject_events_v1"):
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


def run_scenario(directory: Path, scratch: Path, scenario: str,
                 fixture: dict) -> dict:
    subject = fixture["subject"]
    archive = fixture["archiveUuid"]
    reader = ImportantLogReader(directory)
    catalog = ImportantLogCatalog(directory, target_bytes=500_000_000,
                                  low_water_bytes=300_000_000)
    sink = ActiveSubjectLog(catalog)
    backend = backend_seam(directory, 0.1)
    capture_callback_count = [0]

    def capture_failure() -> None:
        capture_callback_count[0] += 1
        backend._mark_important_capture_unknown()

    workflow = TaskWorkflow(None, None, subject_log=sink,
                            subject_capture_failure=capture_failure)
    original_flock = fcntl.flock
    original_copy = reader._copy_file
    original_view = reader._view
    archive_view_entered = threading.Event()
    overlap_arm = threading.Event()
    started = threading.Event()
    stop = threading.Event()
    lock = threading.Lock()
    local = threading.local()
    pending: dict[tuple[int, int], dict] = {}
    flock_spans: list[dict] = []
    copy_totals: Counter[str] = Counter()
    read_calls: list[dict] = []
    backend_calls: list[dict] = []
    monitor_lags: list[float] = []
    writer_calls: list[dict] = []
    writer_failures: list[str] = []
    entry_markers: list[str] = []
    entry_ids: list[str] = []
    event_keys: list[str] = []
    start_at = [0.0]

    def due(seconds: float) -> None:
        remaining = start_at[0] + seconds - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    @contextmanager
    def measured_view(identity):
        local.view_identity = identity
        try:
            with original_view(identity) as store:
                yield store
        finally:
            local.view_identity = None

    def measured_copy(fd, name, destination):
        value = original_copy(fd, name, destination)
        with lock:
            copy_totals[threading.current_thread().name] += destination.stat().st_size
        return value

    def measured_flock(fd, operation):
        name = threading.current_thread().name
        if not name.startswith("usage-"):
            return original_flock(fd, operation)
        key = (threading.get_ident(), fd)
        if operation & fcntl.LOCK_EX:
            with lock:
                pending.setdefault(key, {"role": name,
                                         "phase": "page" if getattr(local, "view_identity", None)
                                         else "catalog", "first": time.monotonic(), "acquired": None,
                                         "viewIdentity": getattr(local, "view_identity", None),
                                         "targetArchivePage": (name == "usage-human"
                                                               and getattr(local, "view_identity", None) == archive
                                                               and overlap_arm.is_set())})
            result = original_flock(fd, operation)
            with lock:
                pending[key]["acquired"] = time.monotonic()
                targeted = pending[key]["targetArchivePage"]
            if targeted:
                archive_view_entered.set()  # The targeted page has acquired catalog EX.
            return result
        if operation == fcntl.LOCK_UN:
            result = original_flock(fd, operation)
            ended = time.monotonic()
            with lock:
                item = pending.pop(key, None)
                if item:
                    acquired = item["acquired"]
                    flock_spans.append({"role": item["role"], "phase": item["phase"],
                                        "wait": (acquired or ended) - item["first"],
                                        "held": 0 if acquired is None else ended - acquired,
                                        "acquired": acquired is not None,
                                        "viewIdentity": item["viewIdentity"],
                                        "targetArchivePage": item["targetArchivePage"],
                                        "firstSeconds": item["first"] - start_at[0],
                                        "acquiredSeconds": None if acquired is None else acquired - start_at[0],
                                        "endedSeconds": ended - start_at[0]})
            return result
        return original_flock(fd, operation)

    def call_read(route: str, planned: float, call) -> dict:
        name = threading.current_thread().name
        with lock:
            before = copy_totals[name]
        begin = time.monotonic()
        try:
            response = call()
            unavailable = response.get("unavailable", [])
            item = {"route": route, "scheduledSeconds": planned,
                    "startedSeconds": begin - start_at[0],
                    "durationSeconds": time.monotonic() - begin,
                    "complete": response.get("complete"), "unavailable": unavailable,
                    "entryCount": len(response.get("entries", [])),
                    "subjectCount": len(response.get("subjects", [])),
                    "sourceDbUuids": sorted({uid for entry in response.get("entries", [])
                                              for uid in entry["sourceDbUuids"]}),
                    "nextAfter": response.get("nextAfter")}
        except Exception as exc:
            response = {}
            item = {"route": route, "scheduledSeconds": planned,
                    "startedSeconds": begin - start_at[0],
                    "durationSeconds": time.monotonic() - begin,
                    "complete": False, "exception": f"{type(exc).__name__}:{exc}"}
        with lock:
            item["copiedBytes"] = copy_totals[name] - before
            read_calls.append(item)
        return response

    def refresh(route: str, planned: float) -> None:
        due(planned)
        if route == "existing_after_30_seconds":
            due(max(planned, backend._important_storage_at - start_at[0] + 30.001))
        begin = time.monotonic()

        def fixture_catalog(path, **options):
            return ImportantLogCatalog(path, target_bytes=500_000_000,
                                       low_water_bytes=300_000_000, **options)

        previous_refresh_at = backend._important_storage_at
        with patch("workbench.backend.service.ImportantLogCatalog", side_effect=fixture_catalog):
            backend._refresh_important_storage()
        state = backend.important_storage_status
        with lock:
            backend_calls.append({"route": route, "scheduledSeconds": planned,
                                  "startedSeconds": begin - start_at[0],
                                  "durationSeconds": time.monotonic() - begin,
                                  "executed": backend._important_storage_at != previous_refresh_at,
                                  "state": state.get("state"),
                                  "subjectCaptureUnknown": state.get("subjectCaptureUnknown"),
                                  "checkpoint": ((state.get("maintenance") or {}).get("work") or {}).get("checkpoint"),
                                  "rollover": (state.get("maintenance") or {}).get("rollover")})

    def write_loop() -> None:
        started.wait()
        refresh("initial_catalog_open", 0.0)
        for index in range(WRITES):
            scheduled = 0.5 + index * 0.75
            due(scheduled)
            overlap_seen = None
            if archive is not None and index == 13:
                overlap_seen = archive_view_entered.wait(timeout=WATCHDOG)
            begin = time.monotonic()
            outcome = {"commitUnknown": False, "durableEntryId": None,
                       "rolloverStateUnknown": False}
            if index % 2:
                message_id = str(uuid4())
                key = "message:" + message_id
                event_keys.append(key)
                observed: dict = {"sinkCalled": False}
                original_record = sink.record_delivery

                def observe_record(*args, **kwargs):
                    observed["sinkCalled"] = True
                    try:
                        stored = original_record(*args, **kwargs)
                    except Exception as exc:
                        observed["sinkState"] = f"{type(exc).__name__}:{exc}"
                        observed["commitUnknown"] = bool(catalog.last_status.get("commitUnknown"))
                        raise
                    observed["sinkState"] = "stored" if stored else "not_stored"
                    return stored

                before_callback = capture_callback_count[0]
                before_unknown = workflow.subject_capture_unknown
                message = SimpleNamespace(task_id=subject["id"], revision=1,
                                          message_id=message_id,
                                          payload={"fixture": f"usage-{scenario}-{index}"})
                receipt = SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)
                with patch.object(sink, "record_delivery", side_effect=observe_record):
                    workflow._copy_message(message, receipt, "manager_instruction")
                state = observed.get("sinkState", "copy_skipped")
                outcome["commitUnknown"] = observed.get("commitUnknown", False)
                outcome["messageId"] = message_id
                outcome["eventKey"] = key
                outcome["sinkCalled"] = observed["sinkCalled"]
                outcome["callerUnknownBefore"] = before_unknown
                outcome["callerUnknownAfter"] = workflow.subject_capture_unknown
                outcome["captureCallbackDelta"] = capture_callback_count[0] - before_callback
            else:
                marker = f"attempt:{scenario}:{index}:{subject['id']}"
                entry_markers.append(marker)
                body = {"schemaVersion": 1, "facts": [marker],
                        "interpretations": [], "unknowns": []}
                try:
                    result = catalog.append(subject, "event", [], body)
                    state = result["state"]
                    outcome["durableEntryId"] = result.get("durableEntryId")
                    outcome["rolloverStateUnknown"] = bool(result.get("rolloverStateUnknown"))
                    if state == "ok":
                        entry_ids.append(result["entry"]["entryId"])
                except Exception as exc:
                    state = f"{type(exc).__name__}:{exc}"
            with lock:
                writer_calls.append({"index": index, "kind": "lifecycle" if index % 2 else "append",
                                     "scheduledSeconds": scheduled, "startedSeconds": begin - start_at[0],
                                     "durationSeconds": time.monotonic() - begin,
                                     "state": state,
                                     "overlapCopyStarted": False if overlap_seen is not None else None,
                                     "overlapArchiveViewLeaseEntered": overlap_seen,
                                     **outcome})
                if state not in ("ok", "stored"):
                    writer_failures.append(f"{index}:{state}")
        refresh("existing_after_30_seconds", 30.0)

    def human_loop() -> None:
        started.wait()
        for burst, planned in enumerate(HUMAN_DUE):
            due(planned)
            target_overlap = archive is not None and burst == 1
            if target_overlap:
                overlap_arm.set()
            attempts = 3 if target_overlap else 1
            first = {}
            for _ in range(attempts):
                first = call_read("human_show_page1", planned,
                                  lambda: reader.show(subject, limit=2))
                if not target_overlap or archive_view_entered.is_set():
                    break
                time.sleep(0.15)
            if target_overlap:
                overlap_arm.clear()
            token = first.get("nextAfter")
            if token:
                call_read("human_show_page2", planned,
                          lambda token=token: reader.show(subject, limit=2, after=token))
            call_read("human_list", planned, lambda: reader.list_subjects(limit=5))

    def agent_loop() -> None:
        started.wait()
        for planned in AGENT_DUE:
            due(planned)
            call_read("agent_show", planned, lambda: reader.show(subject, limit=5))

    def status_loop() -> None:
        started.wait()
        for planned in STATUS_DUE:
            due(planned)
            call_read("metadata_status", planned, reader.status)

    def monitor_loop() -> None:
        started.wait()
        next_due = start_at[0]
        while not stop.is_set():
            next_due += 0.2
            remaining = next_due - time.monotonic()
            if remaining > 0:
                stop.wait(remaining)
            if stop.is_set():
                break
            begin = time.monotonic()
            with backend._important_sink_lock():
                _ = backend.important_storage_status.get("state")
            with lock:
                monitor_lags.append(time.monotonic() - begin)

    threads = [threading.Thread(target=write_loop, name="usage-writer"),
               threading.Thread(target=human_loop, name="usage-human"),
               threading.Thread(target=agent_loop, name="usage-agent"),
               threading.Thread(target=status_loop, name="usage-status"),
               threading.Thread(target=monitor_loop, name="usage-monitor")]
    archive_before = archive_inventory(directory, archive) if archive else None
    with patch.object(reader, "_view", side_effect=measured_view), \
         patch.object(reader, "_copy_file", side_effect=measured_copy), \
         patch.object(fcntl, "flock", measured_flock):
        for thread in threads:
            thread.start()
        start_at[0] = time.monotonic()
        started.set()
        due(WINDOW)
        stop.set()
        for thread in threads:
            thread.join(timeout=WATCHDOG)
    alive_after_watchdog = [thread.name for thread in threads if thread.is_alive()]
    for thread in threads:
        if thread.is_alive():
            thread.join()  # return ownership only after every mutating thread exits

    archive_after_workload = archive_inventory(directory, archive) if archive else None
    counts = final_attempt_counts(directory, subject, entry_ids, entry_markers, event_keys)
    archive_after_verifier = archive_inventory(directory, archive) if archive else None
    archive_unchanged = (archive_before == archive_after_workload == archive_after_verifier
                         if archive else None)
    def spans(role: str, phase: str | None = None) -> list[dict]:
        return [x for x in flock_spans if x["role"] == role and
                (phase is None or x["phase"] == phase)]
    writer_spans = spans("usage-writer")
    reader_page_spans = [x for x in flock_spans if x["role"] in ("usage-human", "usage-agent")
                         and x["phase"] == "page"]
    targeted_pages = [x for x in reader_page_spans if x["targetArchivePage"] and x["acquired"]]
    writer_index_13 = next((x["startedSeconds"] for x in writer_calls if x["index"] == 13), None)
    actual_overlap = (None if archive is None or writer_index_13 is None else
                      any(x["acquiredSeconds"] <= writer_index_13 <= x["endedSeconds"]
                          for x in targeted_pages))
    return {"scenario": scenario, "subject": subject,
            "writeAttempts": writer_calls, "writerFailures": writer_failures,
            "final": counts, "backendCalls": backend_calls,
            "backendWatchdogExceeded": any(x["durationSeconds"] > WATCHDOG for x in backend_calls),
            "backendMonitorLockSeconds": distribution(monitor_lags),
            "backendMonitorWatchdogExceeded": any(x > WATCHDOG for x in monitor_lags),
            "backendSeamCaptureUnknown": bool(backend.important_storage_status.get("subjectCaptureUnknown")
                                              or backend.important_storage_status.get("state") == "capture_unknown"),
            "actualCallerCaptureUnknown": bool(workflow.subject_capture_unknown),
            "backendCaptureUnknownLatch": bool(backend._important_capture_unknown),
            "captureCallbackCount": capture_callback_count[0],
            "callerBoundary": "TaskWorkflow._copy_message confirmed delivery only; no start/OMP runtime",
            "sinkLastFailure": sink.last_failure,
            "writerExWaitSeconds": distribution([x["wait"] for x in writer_spans]),
            "writerExHeldSeconds": distribution([x["held"] for x in writer_spans if x["acquired"]]),
            "writerExFailedAcquisitions": sum(not x["acquired"] for x in writer_spans),
            "readerPageExWaitSeconds": distribution([x["wait"] for x in reader_page_spans]),
            "readerPageExHeldSeconds": distribution([x["held"] for x in reader_page_spans if x["acquired"]]),
            "readCalls": read_calls,
            "archiveCopyOverlapObserved": False if archive else None,
            "archiveViewLeaseEntryObserved": archive_view_entered.is_set() if archive else None,
            "targetedArchivePageExIntervals": [
                {"acquiredSeconds": x["acquiredSeconds"], "endedSeconds": x["endedSeconds"],
                 "heldSeconds": x["held"]} for x in targeted_pages],
            "writerIndex13BeginSeconds": writer_index_13,
            "writerIndex13DuringArchivePageEx": actual_overlap,
            "archiveBytesAndInventoryUnchanged": archive_unchanged,
            "archiveUnchangedBeforeVerifier": archive_before == archive_after_workload if archive else None,
            "archiveUnchangedAfterVerifier": archive_before == archive_after_verifier if archive else None,
            "scratchCleanAfterConcurrency": not list(scratch.iterdir()),
            "threadsAliveAfterWatchdog": alive_after_watchdog}


def main(result_dir: Path = RESULT_DIR) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    plan = result_dir / ("experiment-plan-v2.json" if result_dir.resolve() == RESULT_DIR.resolve()
                         else "experiment-plan.json")
    if not plan.exists():
        raise RuntimeError(f"{plan.name} must be fixed before execution")
    result_path = result_dir / "usage-measurement05.json"
    if result_path.exists():
        raise RuntimeError("usage-measurement05.json already exists; preserve original")
    result = {"planSha256": sha256(plan.read_bytes()).hexdigest(),
              "experimentOnly": True,
              "correctionFrom01To04": "Earlier raw files retained. The sealed archive page now uses immutable source read under catalog EX; targeted writer overlap observes actual archive page EX acquisition rather than copy start. Backend monitor uses the same watchdog. Schedule, fixture and product settings unchanged.",
              "integrationBoundary": "reduced real Backend._refresh_important_storage body and TaskWorkflow._copy_message confirmed-delivery boundary; synthetic reader calls model human/Agent/status; no TaskWorkflow start, actual CLI/Agent/TUI/OMP runtime",
              "unmeasured": ["full TaskWorkflow/OMP runtime", "cold cache", "slow storage",
                             "other filesystem", "actual product lookup frequency"],
              "scenarios": []}
    original_tempdir = tempfile.tempdir
    connection = None
    try:
        for scenario in ("active_only", "sealed_archive_after_catalog_rollover"):
            with tempfile.TemporaryDirectory(dir=result_dir, prefix="usage-fixture-") as fixture_dir:
                root = Path(fixture_dir)
                directory = root / "important"
                scratch = root / "read-scratch"
                scratch.mkdir(mode=0o700)
                tempfile.tempdir = str(scratch)
                fixture, connection = setup_fixture(directory, scenario)
                reader = ImportantLogReader(directory)
                active_controlled = controlled(reader, fixture["activeUuid"], directory,
                                               scratch, fixture["latestPayload"].encode())
                archive_control = controlled_archive(reader, fixture, directory, scratch)
                measured = run_scenario(directory, scratch, scenario, fixture)
                result["scenarios"].append({"fixture": {k: v for k, v in fixture.items()
                                                        if k != "subject"},
                                            "controlledActive": active_controlled,
                                            "controlledArchive": archive_control,
                                            "measured": measured})
                connection.close()
                connection = None
                result["scenarios"][-1]["scratchCleanAfterClose"] = not list(scratch.iterdir())
    except Exception as exc:
        result["harnessError"] = f"{type(exc).__name__}:{exc}"
    finally:
        if connection is not None:
            connection.close()
        tempfile.tempdir = original_tempdir
        result["fixtureDirectoriesRemoved"] = not any(result_dir.glob("usage-fixture-*"))
        result["measuredConditionsPassed"] = (
            "harnessError" not in result and len(result["scenarios"]) == 2
            and result["fixtureDirectoriesRemoved"]
            and all(item["controlledActive"]["logicalLatestWalReflected"]
                    and item["controlledActive"]["existingMainWalUnchanged"]
                    and item["controlledActive"]["newDatabaseCount"] == 0
                    and item["controlledActive"]["normalScratchClean"]
                    and item["measured"]["final"]["entriesExactlyOnce"]
                    and item["measured"]["final"]["eventsExactlyOnce"]
                    and not item["measured"]["writerFailures"]
                    and not item["measured"]["backendWatchdogExceeded"]
                    and not item["measured"]["backendMonitorWatchdogExceeded"]
                    and item["measured"]["backendMonitorLockSeconds"]["count"] > 0
                    and [(call["route"], call["executed"])
                         for call in item["measured"]["backendCalls"]]
                        == [("initial_catalog_open", True),
                            ("existing_after_30_seconds", True)]
                    and not item["measured"]["backendSeamCaptureUnknown"]
                    and not item["measured"]["actualCallerCaptureUnknown"]
                    and not item["measured"]["backendCaptureUnknownLatch"]
                    and item["measured"]["captureCallbackCount"] == 0
                    and not item["measured"]["threadsAliveAfterWatchdog"]
                    and item["measured"]["scratchCleanAfterConcurrency"]
                    and item["scratchCleanAfterClose"] for item in result["scenarios"])
            and (result["scenarios"][1]["controlledArchive"]["largeArchiveBytes"] >= 250_000_000
                 and result["scenarios"][1]["controlledArchive"]["archiveInventoryAndBytesUnchanged"]
                 and result["scenarios"][1]["controlledArchive"]["seedEntriesVisible"]
                 and result["scenarios"][1]["measured"]["archiveViewLeaseEntryObserved"]
                 and result["scenarios"][1]["measured"]["writerIndex13DuringArchivePageEx"]
                 and result["scenarios"][1]["measured"]["archiveBytesAndInventoryUnchanged"]))
        with result_path.open("x") as output:
            output.write(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(json.dumps({"result": str(result_path),
                      "measuredConditionsPassed": result["measuredConditionsPassed"],
                      "harnessError": result.get("harnessError")}, sort_keys=True))


def capture_caller_control() -> None:
    """Exercise the real confirmed-message failure hook without starting a run."""
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULT_DIR / "capture-caller-control.json"
    if path.exists():
        raise RuntimeError("capture-caller-control.json already exists; preserve original")
    result: dict = {"scope": "TaskWorkflow._copy_message and Backend capture hook only; "
                           "no TaskWorkflow start, real mailbox, CLI, Agent or model"}
    try:
        with tempfile.TemporaryDirectory(dir=RESULT_DIR, prefix="usage-capture-control-") as root:
            directory = Path(root) / "important"
            catalog = ImportantLogCatalog(directory, create=True,
                                          target_bytes=500_000_000,
                                          low_water_bytes=300_000_000)
            sink = ActiveSubjectLog(catalog)
            backend = backend_seam(directory, 0.1)
            backend.important_catalog = catalog
            backend.subject_log = sink
            workflow = TaskWorkflow(None, None, subject_log=sink,
                                    subject_capture_failure=backend._mark_important_capture_unknown)
            task_id = str(uuid4())
            receipt = SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)
            success_id = str(uuid4())
            success = SimpleNamespace(task_id=task_id, revision=1, message_id=success_id,
                                      payload={"fixture": "confirmed"})
            workflow._copy_message(success, receipt, "manager_instruction")
            result["beforeFailure"] = {
                "workflowCaptureUnknown": workflow.subject_capture_unknown,
                "backendCaptureUnknown": backend._important_capture_unknown}
            failed_id = str(uuid4())
            failed = SimpleNamespace(task_id=task_id, revision=1, message_id=failed_id,
                                     payload={"fixture": "injected lease busy"})
            with patch.object(sink, "record_delivery",
                              side_effect=sqlite3.OperationalError("important catalog lease busy")):
                workflow._copy_message(failed, receipt, "manager_instruction")
            result["afterFailure"] = {
                "workflowCaptureUnknown": workflow.subject_capture_unknown,
                "backendCaptureUnknown": backend._important_capture_unknown}
            backend._refresh_important_storage()
            result["backendStatusAfterFailure"] = backend.important_storage_status.get("state")
            identity = catalog.active_uuid()
            with ImportantLogStore.open(directory, identity, readonly=True) as store:
                keys = {row[0] for row in store._connection.execute(
                    "SELECT event_key FROM subject_events_v1")}
            result["successEventPresent"] = "message:" + success_id in keys
            result["failedEventAbsent"] = "message:" + failed_id not in keys
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}:{exc}"
    result["fixtureDirectoriesRemoved"] = not any(RESULT_DIR.glob("usage-capture-control-*"))
    result["passed"] = ("error" not in result and result["fixtureDirectoriesRemoved"]
                        and result["beforeFailure"] == {"workflowCaptureUnknown": False,
                                                         "backendCaptureUnknown": False}
                        and result["afterFailure"] == {"workflowCaptureUnknown": True,
                                                        "backendCaptureUnknown": True}
                        and result["backendStatusAfterFailure"] == "capture_unknown"
                        and result["successEventPresent"] and result["failedEventAbsent"])
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"result": str(path), "passed": result["passed"],
                      "error": result.get("error")}, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) == 1:
        main()
    elif len(sys.argv) == 3 and sys.argv[1] == "--result-dir":
        main(Path(sys.argv[2]))
    elif sys.argv == [sys.argv[0], "--capture-control"]:
        capture_caller_control()
    else:
        raise SystemExit("usage: usage_contention_measurement.py [--result-dir DIR|--capture-control]")
