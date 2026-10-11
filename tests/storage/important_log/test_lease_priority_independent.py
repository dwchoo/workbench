"""Independent writer lease and commit-boundary checks."""

import fcntl
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.storage.important_log import ImportantLogCatalog, ImportantLogStore


BODY = {"schemaVersion": 1, "facts": ["one write"], "interpretations": [], "unknowns": []}


class IndependentLeasePriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "catalog"
        self.catalog = ImportantLogCatalog(self.directory, create=True, writer_lease_timeout_s=0.6)
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}

    def _under_held_lock(self, operation):
        with (self.directory / "maintenance.lock").open("rb") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            released = threading.Event()

            def release():
                time.sleep(0.15)
                fcntl.flock(held, fcntl.LOCK_UN)
                released.set()

            thread = threading.Thread(target=release)
            thread.start()
            try:
                started = time.monotonic()
                result = operation()
                elapsed = time.monotonic() - started
            finally:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
            self.assertTrue(released.is_set())
            self.assertGreaterEqual(elapsed, 0.12)
            self.assertLess(elapsed, 0.6)
            return result

    def test_catalog_open_and_writer_class_operations_wait_for_lease(self):
        reopened = self._under_held_lock(
            lambda: ImportantLogCatalog(self.directory, writer_lease_timeout_s=0.6))
        self.assertEqual(reopened.writer_lease_timeout_s, 0.6)
        result = self._under_held_lock(lambda: self.catalog.append(self.subject, "event", [], BODY))
        self.assertEqual(result["state"], "ok")
        health = self._under_held_lock(self.catalog.health_snapshot)
        self.assertIn("active", health)
        maintenance = self._under_held_lock(self.catalog.maintenance_step)
        self.assertIn(maintenance["state"], ("ok", "deferred"))
        recovery = self._under_held_lock(lambda: self.catalog.recovery_probe(force=True))
        self.assertIn(recovery["state"], ("healthy", "ok"))

    def test_short_default_wait_fails_before_mutation(self):
        short = ImportantLogCatalog(self.directory)
        before = short.catalog_snapshot()
        with (self.directory / "maintenance.lock").open("rb") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            result = short.append(self.subject, "event", [], BODY)
            elapsed = time.monotonic() - started
        self.assertEqual(result["state"], "busy")
        self.assertIsNone(result["durableEntryId"])
        self.assertLess(elapsed, 0.4)
        self.assertEqual(short.catalog_snapshot()["active"], before["active"])
        with ImportantLogStore.open(self.directory, before["active"], readonly=True) as store:
            self.assertEqual(store.list_entries(self.subject), [])

    def test_failure_after_commit_is_not_replayed(self):
        original_put = ImportantLogStore.put_entry
        put_count = 0

        def counted_put(store, entry):
            nonlocal put_count
            put_count += 1
            return original_put(store, entry)

        with patch.object(ImportantLogStore, "put_entry", counted_put), \
             patch.object(self.catalog, "_after_write", side_effect=sqlite3.OperationalError("after commit")):
            result = self.catalog.append(self.subject, "event", [], BODY)
        self.assertNotEqual(result["state"], "ok")
        self.assertTrue(result["rolloverStateUnknown"])
        self.assertIsNotNone(result["durableEntryId"])
        self.assertEqual(put_count, 1)
        with ImportantLogStore.open(self.directory, self.catalog.active_uuid(), readonly=True) as store:
            entries = store.list_entries(self.subject)
        self.assertEqual([entry["entryId"] for entry in entries], [result["durableEntryId"]])


if __name__ == "__main__":
    unittest.main()
