from datetime import datetime, timezone
import errno
import json
from hashlib import sha256
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.storage.important_log import ImportantLogCatalog, ImportantLogStore, ImportantLogStoreError
from workbench.contracts.logs_v1 import parse_health_snapshot
import workbench.storage.important_log.catalog as catalog_module


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
BODY = {"schemaVersion": 1, "facts": ["one"], "interpretations": [], "unknowns": []}


class StorageFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "important"
        self.catalog = ImportantLogCatalog(self.directory, create=True)
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}

    def test_disk_full_is_storage_fault_and_recovery_probe_does_not_resume_policy(self):
        with patch.object(ImportantLogStore, "put_entry", side_effect=OSError(errno.ENOSPC, "full")):
            status = self.catalog.append(self.subject, "start", [], BODY, now=NOW)
        self.assertEqual(status["state"], "physical_full")
        self.assertIsNone(status["durableEntryId"])
        recovered = self.catalog.recovery_probe(force=True)
        self.assertEqual(recovered["state"], "healthy")
        self.assertNotIn("approval", recovered)
        self.assertNotIn("unpaused", recovered)
        self.assertEqual(self.catalog.recovery_probe()["state"], "deferred")

    def test_catalog_contention_reports_busy_within_short_bound(self):
        entered = threading.Event()
        release = threading.Event()

        def owner():
            with self.catalog._lease():
                entered.set()
                release.wait(1)

        thread = threading.Thread(target=owner)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            self.assertEqual(self.catalog.append(self.subject, "start", [], BODY)["state"], "busy")
        finally:
            release.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_parallel_appends_are_serialized_without_lost_rows(self):
        outcome = []

        def writer():
            subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
            outcome.append(self.catalog.append(subject, "start", [], BODY)["state"])

        workers = [threading.Thread(target=writer) for _ in range(4)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in workers))
        self.assertEqual(outcome, ["ok"] * 4)
        with ImportantLogStore.open(self.directory, self.catalog.active_uuid()) as store:
            self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 4)

    def test_open_direct_writer_prevents_rollover_until_it_releases_shared_lease(self):
        old = self.catalog.active_uuid()
        with ImportantLogStore.open(self.directory, old) as direct:
            self.assertIsNotNone(direct._lease_fd)
            result = self.catalog.append(self.subject, "start", [], BODY)
            self.assertEqual(result["state"], "busy")
        self.assertEqual(self.catalog.append(self.subject, "start", [], BODY)["state"], "ok")

    def test_catalog_and_direct_writer_reject_same_invalid_pointer_identifiers(self):
        identity = self.catalog.active_uuid()
        pointer = self.directory / "active.json"
        original = pointer.read_text(encoding="utf-8")
        fields = json.loads(original)
        cases = {
            "unknown_version": {**fields, "version": 2},
            "boolean_generation": {**fields, "generation": True},
            "malformed_archive": {**fields, "archives": ["not-a-uuid"]},
            "archive_overlap": {**fields, "archives": [identity]},
            "unknown_archive_check": {**fields, "archiveChecks": {str(uuid4()): "complete"}},
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                pointer.write_text(json.dumps(value), encoding="utf-8")
                try:
                    with self.assertRaises(ImportantLogStoreError):
                        self.catalog.active_uuid()
                    with self.assertRaises(ImportantLogStoreError):
                        ImportantLogStore.open(self.directory, identity)
                finally:
                    pointer.write_text(original, encoding="utf-8")
        archive = str(uuid4())
        nested_duplicate = (f'{{"version":1,"generation":1,"active":"{identity}",'
                            f'"archives":["{archive}"],"archiveChecks":{{'
                            f'"{archive}":"complete","{archive}":"busy"}}}}')
        pointer.write_text(nested_duplicate, encoding="utf-8")
        try:
            with self.assertRaises(ImportantLogStoreError):
                self.catalog.active_uuid()
            with self.assertRaises(ImportantLogStoreError):
                ImportantLogStore.open(self.directory, identity)
        finally:
            pointer.write_text(original, encoding="utf-8")
        self.assertEqual(self.catalog.active_uuid(), identity)

    def test_missing_catalog_pointer_refuses_direct_write_but_pre_catalog_store_opens(self):
        identity = self.catalog.active_uuid()
        pointer = self.directory / "active.json"
        original = pointer.read_bytes()
        pointer.unlink()
        try:
            with self.assertRaises(ImportantLogStoreError):
                ImportantLogStore.open(self.directory, identity)
        finally:
            pointer.write_bytes(original)
            pointer.chmod(0o600)

        standalone = Path(self.temp.name) / "standalone"
        with ImportantLogStore.create(standalone) as store:
            plain_identity = store.db_uuid
        with ImportantLogStore.open(standalone, plain_identity) as store:
            self.assertEqual(store.db_uuid, plain_identity)

    def test_pointer_published_then_intent_cleanup_failure_recovers(self):
        small = ImportantLogCatalog(self.directory, target_bytes=36_000,
                                    low_water_bytes=30_000)
        old = small.active_uuid()
        with patch.object(catalog_module, "_unlink_control", side_effect=OSError(errno.EIO, "injected")):
            result = small.append(self.subject, "start", [], BODY, now=NOW)
        self.assertEqual(result["state"], "io_error")
        self.assertIsNotNone(result["durableEntryId"])
        self.assertTrue(result["rolloverStateUnknown"])
        recovered = ImportantLogCatalog(self.directory).catalog_snapshot()
        self.assertEqual(recovered["archives"], [old])
        self.assertEqual(recovered["databaseCount"], 2)
        self.assertFalse((self.directory / "rollover.intent.json").exists())

    def test_physical_wal_size_is_not_logical_capacity(self):
        subject = self.subject
        old = self.catalog.active_uuid()
        with ImportantLogStore.open(self.directory, old, readonly=True) as reader:
            reader._connection.execute("BEGIN")
            reader._connection.execute("SELECT COUNT(*) FROM entries").fetchone()
            for _ in range(5):
                result = self.catalog.append(subject, "event", [], BODY, now=NOW)
                self.assertEqual(result["state"], "ok")
                self.assertIsNone(result["rollover"])
            health = self.catalog.health_snapshot()["active"]
            self.assertEqual(parse_health_snapshot(health), health)
            self.assertTrue(self.catalog.health_snapshot()["operatingMetadata"]["outsideBudget"])
            self.assertGreaterEqual(health["walBytes"], 0)
            self.assertLess(health["logicalLiveBytes"], 250_000_000)
            self.assertEqual(self.catalog.active_uuid(), old)
            reader._connection.execute("ROLLBACK")

    def test_incomplete_passive_checkpoint_is_visible_in_health(self):
        identity = self.catalog.active_uuid()
        with ImportantLogStore.open(self.directory, identity, readonly=True) as reader:
            reader._connection.execute("BEGIN")
            reader._connection.execute("SELECT COUNT(*) FROM entries").fetchone()
            for _ in range(5):
                self.assertEqual(self.catalog.append(self.subject, "event", [], BODY,
                                                     now=NOW)["state"], "ok")

            result = self.catalog.maintenance_step(now=NOW)
            checkpoint = result["work"]["checkpoint"]
            self.assertFalse(checkpoint["busy"])
            self.assertGreater(checkpoint["logPages"], checkpoint["checkpointedPages"])
            self.assertEqual(checkpoint["status"], "incomplete")
            self.assertEqual(result["state"], "maintenance_incomplete")

            health = self.catalog.health_snapshot(now=NOW)["active"]
            self.assertEqual(parse_health_snapshot(health), health)
            self.assertEqual(health["lastMaintenance"], "incomplete:checkpoint")
            self.assertFalse(health["stale"])
            self.assertEqual(self.catalog.active_uuid(), identity)
            reader._connection.execute("ROLLBACK")

            complete = self.catalog.maintenance_step(now=NOW)
            self.assertEqual(complete["work"]["checkpoint"]["status"], "ok")
            self.assertEqual(complete["state"], "ok")
            refreshed = self.catalog.health_snapshot(now=NOW)["active"]
            self.assertEqual(parse_health_snapshot(refreshed), refreshed)
            self.assertEqual(refreshed["lastMaintenance"], "none:0")
            self.assertFalse(refreshed["stale"])


if __name__ == "__main__":
    unittest.main()
