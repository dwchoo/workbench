"""Independent LG-D19 accounting, archive protection, and caller visibility tests."""

import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import usage_contention_measurement as usage
from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog, ImportantLogReader, ImportantLogStore
from workbench.workflow.run import TaskWorkflow


class IndependentUsageContentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "catalog"
        self.subject = {"schemaVersion": 1, "kind": "task", "id": str(uuid4())}
        self.catalog = ImportantLogCatalog(self.directory, create=True,
                                           target_bytes=500_000_000,
                                           low_water_bytes=300_000_000)

    def _append(self, marker):
        result = self.catalog.append(self.subject, "event", [],
                                     {"schemaVersion": 1, "facts": [marker],
                                      "interpretations": [], "unknowns": []})
        self.assertEqual(result["state"], "ok")
        return result["entry"]["entryId"]

    def test_all_attempts_denominator_exposes_missing_marker_and_event(self):
        stored_marker = "attempt:stored:" + str(uuid4())
        omitted_marker = "attempt:busy:" + str(uuid4())
        stored_entry_id = self._append(stored_marker)
        stored_message_id = str(uuid4())
        missing_message_id = str(uuid4())
        sink = ActiveSubjectLog(self.catalog)
        self.assertTrue(sink.record_delivery(
            self.subject, message_id=stored_message_id, revision=1,
            kind="manager_instruction", content="stored",
            delivered_at="2026-10-10T00:00:00Z"))
        counts = usage.final_attempt_counts(
            self.directory, self.subject, [stored_entry_id],
            [stored_marker, omitted_marker],
            ["message:" + stored_message_id, "message:" + missing_message_id])
        self.assertTrue(counts["returnedEntriesExactlyOnce"])
        self.assertFalse(counts["entriesExactlyOnce"])
        self.assertFalse(counts["eventsExactlyOnce"])
        self.assertEqual(counts["entryMissing"], [omitted_marker])
        self.assertEqual(counts["eventMissing"], ["message:" + missing_message_id])
        self.assertEqual(counts["entryCount"], 2)
        self.assertEqual(counts["returnedEntryCount"], 1)

    def test_final_counter_preserves_sealed_archive_inventory_and_cleans_scratch(self):
        marker = "attempt:archive:" + str(uuid4())
        entry_id = self._append(marker)
        archived = self.catalog.active_uuid()
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with ImportantLogStore.open(self.directory, archived, _catalog_owned=True) as store:
                rollover = self.catalog._rotate(fd, pointer, store)
        self.assertEqual(rollover["checkpoint"], "complete")
        scratch = Path(self.temp.name) / "read-scratch"
        scratch.mkdir(mode=0o700)
        before = usage.archive_inventory(self.directory, archived)
        with patch.object(tempfile, "tempdir", str(scratch)):
            counts = usage.final_attempt_counts(
                self.directory, self.subject, [entry_id], [marker], [])
        after = usage.archive_inventory(self.directory, archived)
        self.assertEqual(before, after)
        self.assertTrue(counts["entriesExactlyOnce"])
        self.assertEqual(counts["archiveCount"], 1)
        self.assertEqual(list(scratch.iterdir()), [])

    def test_real_reader_lease_busy_marks_workflow_and_backend_capture_unknown(self):
        backend = usage.backend_seam(self.directory, 0.1)
        backend.important_catalog = self.catalog
        sink = ActiveSubjectLog(self.catalog)
        backend.subject_log = sink
        workflow = TaskWorkflow(None, None, subject_log=sink,
                                subject_capture_failure=backend._mark_important_capture_unknown)
        message_id = str(uuid4())
        message = SimpleNamespace(task_id=self.subject["id"], revision=1,
                                  message_id=message_id, payload={"fixture": "confirmed"})
        receipt = SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)
        active = self.catalog.active_uuid()
        with ImportantLogReader(self.directory)._view(active):
            workflow._copy_message(message, receipt, "manager_instruction")
            self.assertTrue(workflow.subject_capture_unknown)
            self.assertTrue(backend._important_capture_unknown)
            self.assertIsNotNone(sink.last_failure)
        backend._important_storage_at = time.monotonic() - 31
        backend._refresh_important_storage()
        self.assertEqual(backend.important_storage_status["state"], "capture_unknown")
        self.assertTrue(backend.important_storage_status["subjectCaptureUnknown"])
        with ImportantLogReader(self.directory)._view(active) as store:
            table = store._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='subject_events_v1'").fetchone()
            found = (store._connection.execute(
                "SELECT 1 FROM subject_events_v1 WHERE event_key=?",
                ("message:" + message_id,)).fetchone() if table else None)
        self.assertIsNone(found)

    def test_normal_confirmed_caller_stores_once_without_capture_latch(self):
        backend = usage.backend_seam(self.directory, 0.1)
        backend.important_catalog = self.catalog
        sink = ActiveSubjectLog(self.catalog)
        backend.subject_log = sink
        callbacks = []

        def capture_failure():
            callbacks.append("unknown")
            backend._mark_important_capture_unknown()

        workflow = TaskWorkflow(None, None, subject_log=sink,
                                subject_capture_failure=capture_failure)
        message_id = str(uuid4())
        message = SimpleNamespace(task_id=self.subject["id"], revision=1,
                                  message_id=message_id, payload={"fixture": "confirmed"})
        receipt = SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)
        original_record = sink.record_delivery
        calls = []

        def delegated_record(*args, **kwargs):
            calls.append(message_id)
            return original_record(*args, **kwargs)

        with patch.object(sink, "record_delivery", side_effect=delegated_record):
            workflow._copy_message(message, receipt, "manager_instruction")
        self.assertEqual(calls, [message_id])
        self.assertFalse(workflow.subject_capture_unknown)
        self.assertFalse(backend._important_capture_unknown)
        self.assertEqual(callbacks, [])
        counts = usage.final_attempt_counts(
            self.directory, self.subject, [], [], ["message:" + message_id])
        self.assertTrue(counts["eventsExactlyOnce"])
        self.assertEqual(counts["eventStoredCount"], 1)

    def test_existing_backend_refresh_body_executes_after_guard_elapsed(self):
        backend = usage.backend_seam(self.directory, 0.1)
        backend.important_catalog = self.catalog
        backend.subject_log = ActiveSubjectLog(self.catalog)
        backend._important_storage_at = time.monotonic() - 30.01
        before = backend._important_storage_at
        with patch.object(self.catalog, "maintenance_step", wraps=self.catalog.maintenance_step) as maintenance, \
             patch.object(self.catalog, "health_snapshot", wraps=self.catalog.health_snapshot) as health:
            backend._refresh_important_storage()
        self.assertGreater(backend._important_storage_at, before)
        self.assertEqual(maintenance.call_count, 1)
        self.assertEqual(health.call_count, 1)
        self.assertEqual(backend.important_storage_status["state"], "ready")

    def _verdict_probe(self, *, monitor_exceeded, actual_overlap):
        # Keep every other verdict field valid, including an actual archive EX
        # interval and writer begin for the positive case.
        result_dir = Path(self.temp.name) / ("gate-" + str(uuid4()))
        result_dir.mkdir()
        (result_dir / "experiment-plan.json").write_text("{}")
        active_uuid = self.catalog.active_uuid()
        archive_uuid = str(uuid4())
        active = {"logicalLatestWalReflected": True, "existingMainWalUnchanged": True,
                  "newDatabaseCount": 0, "normalScratchClean": True}
        archive = {"largeArchiveBytes": 250_000_000,
                   "archiveInventoryAndBytesUnchanged": True, "seedEntriesVisible": True}
        measured = {"final": {"entriesExactlyOnce": True, "eventsExactlyOnce": True},
                    "writerFailures": [], "backendWatchdogExceeded": False,
                    "backendMonitorWatchdogExceeded": monitor_exceeded,
                    "backendMonitorLockSeconds": {"count": 1,
                                                  "maximum": usage.WATCHDOG + 0.01 if monitor_exceeded else 0.01},
                    "backendCalls": [{"route": "initial_catalog_open", "executed": True},
                                     {"route": "existing_after_30_seconds", "executed": True}],
                    "backendSeamCaptureUnknown": False, "actualCallerCaptureUnknown": False,
                    "backendCaptureUnknownLatch": False, "captureCallbackCount": 0,
                    "threadsAliveAfterWatchdog": [], "scratchCleanAfterConcurrency": True,
                    "archiveViewLeaseEntryObserved": True,
                    "writerIndex13DuringArchivePageEx": actual_overlap,
                    "archiveBytesAndInventoryUnchanged": True}

        class Connection:
            def close(self):
                pass

        def fixture_for(_directory, scenario):
            return ({"subject": self.subject, "activeUuid": active_uuid,
                     "archiveUuid": archive_uuid if scenario != "active_only" else None,
                     "latestPayload": "synthetic"}, Connection())

        with patch.object(usage, "setup_fixture", side_effect=fixture_for), \
             patch.object(usage, "controlled", return_value=active), \
             patch.object(usage, "controlled_archive", return_value=archive), \
             patch.object(usage, "run_scenario", return_value=measured):
            usage.main(result_dir)
        result = json.loads((result_dir / "usage-measurement05.json").read_text())
        self.assertEqual(len(result["scenarios"]), 2)
        self.assertNotIn("harnessError", result)
        return result

    def test_monitor_watchdog_rejects_otherwise_passing_measurement(self):
        result = self._verdict_probe(monitor_exceeded=True, actual_overlap=True)
        self.assertTrue(all(item["measured"]["backendMonitorWatchdogExceeded"]
                            for item in result["scenarios"]))
        self.assertTrue(result["scenarios"][1]["measured"]["writerIndex13DuringArchivePageEx"])
        self.assertFalse(result["measuredConditionsPassed"])

    def test_actual_archive_ex_overlap_required_beyond_lease_entry_event(self):
        negative = self._verdict_probe(monitor_exceeded=False, actual_overlap=False)
        archive = negative["scenarios"][1]["measured"]
        self.assertTrue(archive["archiveViewLeaseEntryObserved"])
        self.assertFalse(archive["writerIndex13DuringArchivePageEx"])
        self.assertFalse(negative["measuredConditionsPassed"])
        positive = self._verdict_probe(monitor_exceeded=False, actual_overlap=True)
        self.assertTrue(positive["scenarios"][1]["measured"]["archiveViewLeaseEntryObserved"])
        self.assertTrue(positive["scenarios"][1]["measured"]["writerIndex13DuringArchivePageEx"])
        self.assertTrue(positive["measuredConditionsPassed"])


if __name__ == "__main__":
    unittest.main()
