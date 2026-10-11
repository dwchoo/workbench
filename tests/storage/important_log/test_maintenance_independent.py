"""Observable health accounting and bounded maintenance under a real WAL reader."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.contracts.logs_v1 import parse_health_snapshot
from workbench.storage.important_log import (ImportantLogCatalog, ImportantLogMaintenance,
                                            ImportantLogStore, SubjectLifecycle)


BODY = {"schemaVersion": 1, "facts": ["observed"], "interpretations": [], "unknowns": []}


class IndependentMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "important"
        self.catalog = ImportantLogCatalog(self.directory, create=True)
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}

    def test_append_reports_distinct_pre_post_logical_and_physical_health(self):
        result = self.catalog.append(self.subject, "start", [], BODY,
                                     now=datetime.now(timezone.utc))
        self.assertEqual(result["state"], "ok")
        self.assertIsNone(result["rollover"])
        for phase in ("pre", "post"):
            snapshot = result[phase]
            self.assertEqual(parse_health_snapshot(snapshot), snapshot)
            self.assertEqual(snapshot["totalBytes"], snapshot["mainBytes"]
                             + snapshot["walBytes"] + snapshot["shmBytes"])
            self.assertGreater(snapshot["diskFreeBytes"], 0)
            self.assertGreaterEqual(snapshot["freelistBytes"], 0)
            self.assertEqual(snapshot["dbUuid"], self.catalog.active_uuid())
        self.assertEqual(result["post"]["lastWrite"], "ok")
        detailed = self.catalog.health_snapshot()["active"]
        self.assertEqual(detailed["subjectStats"][0]["subject"], self.subject)
        self.assertEqual(detailed["subjectStats"][0]["entryCount"], 1)
        self.assertEqual(detailed["cleanupCandidates"], 0)
        self.assertEqual(detailed["lastWrite"], "ok")
        with ImportantLogStore.open(self.directory, self.catalog.active_uuid()) as store:
            inexpensive = store.health_snapshot(include_subject_stats=False)
        self.assertTrue(inexpensive["stale"])
        self.assertEqual(inexpensive["subjectStats"], [])

    def test_checkpoint_with_pinned_reader_does_not_claim_complete(self):
        identity = self.catalog.active_uuid()
        with ImportantLogStore.open(self.directory, identity, readonly=True) as reader:
            reader._connection.execute("BEGIN")
            reader._connection.execute("SELECT COUNT(*) FROM entries").fetchone()
            for _ in range(5):
                self.assertEqual(self.catalog.append(self.subject, "event", [], BODY)["state"], "ok")
            result = self.catalog.maintenance_step()
            checkpoint = result["work"]["checkpoint"]
            self.assertGreater(checkpoint["logPages"], checkpoint["checkpointedPages"])
            self.assertEqual(checkpoint["status"], "incomplete")
            self.assertEqual(result["state"], "maintenance_incomplete")
            self.assertEqual(self.catalog.health_snapshot()["active"]["lastMaintenance"],
                             "incomplete:checkpoint")
            reader._connection.execute("ROLLBACK")
            complete = self.catalog.maintenance_step()
            self.assertEqual(complete["work"]["checkpoint"]["status"], "ok")
            self.assertEqual(complete["state"], "ok")
            self.assertNotEqual(self.catalog.health_snapshot()["active"]["lastMaintenance"],
                                "incomplete:checkpoint")

    def test_sqlite_shrink_refusal_is_reported_then_cleared_on_recovery(self):
        open_store = ImportantLogStore.open

        def reject_shrink(directory, db_uuid, **options):
            store = open_store(directory, db_uuid, **options)

            def authorize(action, argument, _other, _database, _source):
                if action == sqlite3.SQLITE_PRAGMA and argument == "incremental_vacuum":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            store._connection.set_authorizer(authorize)
            return store

        with patch.object(ImportantLogStore, "open", side_effect=reject_shrink):
            failed = self.catalog.maintenance_step()
        self.assertEqual(failed["work"]["checkpoint"]["status"], "ok")
        self.assertEqual(failed["work"]["shrink"]["status"], "incomplete")
        self.assertEqual(failed["state"], "maintenance_incomplete")
        self.assertEqual(self.catalog.health_snapshot()["active"]["lastMaintenance"],
                         "incomplete:shrink")
        complete = self.catalog.maintenance_step()
        self.assertEqual(complete["state"], "ok")
        self.assertNotEqual(self.catalog.health_snapshot()["active"]["lastMaintenance"],
                            "incomplete:shrink")

    def test_fixed_100_row_prune_budget_does_not_scan_entire_closed_backlog(self):
        now = datetime(2026, 10, 9, tzinfo=timezone.utc)

        def candidate_work(entries):
            with tempfile.TemporaryDirectory(dir="/dev/shm") as temporary:
                directory = Path(temporary) / "important"
                subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
                with ImportantLogStore.create(directory) as store:
                    lifecycle = SubjectLifecycle(directory, store.db_uuid)
                    for index in range(entries):
                        store.put_entry(store.make_entry(subject, "event", [],
                            {"schemaVersion": 1, "facts": [str(index)],
                             "interpretations": [], "unknowns": []}))
                    lifecycle.archive_close(subject,
                        at=(now - timedelta(days=121)).isoformat().replace("+00:00", "Z"),
                        user_confirmed=True)
                    maintenance = ImportantLogMaintenance(store)
                    steps = 0
                    largest_select = 0
                    counting = False

                    def trace(statement):
                        nonlocal steps, largest_select, counting
                        if counting:
                            largest_select = max(largest_select, steps)
                        counting = statement.lstrip().upper().startswith("SELECT")
                        steps = 0

                    def progress():
                        nonlocal steps
                        if counting:
                            steps += 1
                        return 0

                    store._connection.set_trace_callback(trace)
                    store._connection.set_progress_handler(progress, 1)
                    try:
                        result = maintenance.prune_pass(now=now, pressure=False)
                    finally:
                        if counting:
                            largest_select = max(largest_select, steps)
                        store._connection.set_progress_handler(None, 0)
                        store._connection.set_trace_callback(None)
                    self.assertEqual(result["deleted"], 100)
                    return largest_select

        small = candidate_work(120)
        large = candidate_work(1200)
        self.assertGreater(small, 0)
        self.assertLessEqual(large, small * 3, (small, large))


if __name__ == "__main__":
    unittest.main()
