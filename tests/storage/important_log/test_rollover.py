from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.storage.important_log import (ActiveSubjectLog, ImportantLogCatalog, ImportantLogStore,
                                            ImportantLogStoreError, SubjectLifecycle)
import workbench.storage.important_log.catalog as catalog_module


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
BODY = {"schemaVersion": 1, "facts": ["retained"], "interpretations": [], "unknowns": []}
LARGE_BODY = {"schemaVersion": 1, "facts": [random.Random(23).randbytes(20_000).hex()],
              "interpretations": [], "unknowns": []}


class RolloverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "important"
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}

    def test_publish_recovery_preserves_old_and_carries_anchor_before_deletion(self):
        catalog = ImportantLogCatalog(self.directory, target_bytes=50_000,
                                      low_water_bytes=40_000, create=True)
        old = catalog.active_uuid()
        start = catalog.append(self.subject, "start", [], BODY, now=NOW)
        self.assertIsNone(start["rollover"])
        result = catalog.append(self.subject, "event", [], LARGE_BODY, now=NOW)
        self.assertEqual(result["state"], "ok")
        self.assertIsNotNone(result["rollover"])
        current = catalog.catalog_snapshot()
        self.assertEqual(current["archives"], [old])
        self.assertEqual(current["databaseCount"], 2)
        self.assertEqual(current["orphans"], [])
        self.assertTrue(current["cleanupNeeded"])
        files = [self.directory / (old + ".sqlite3" + suffix) for suffix in ("", "-wal", "-shm")]
        before = {path.name: sha256(path.read_bytes()).hexdigest() for path in files if path.exists()}
        with self.assertRaises(ImportantLogStoreError):
            with ImportantLogStore.open(self.directory, old):
                pass
        with self.assertRaises(ImportantLogStoreError):
            SubjectLifecycle(self.directory, old)
        self.assertEqual(catalog.protected_anchor_ids(old), [start["entry"]["entryId"]])
        carried = catalog.carry_protected_anchors(old)
        self.assertTrue(carried["safeToDelete"])
        self.assertEqual(carried["copied"], 1)
        with ImportantLogStore.open(self.directory, current["active"], readonly=True) as target:
            self.assertEqual(target.get_entry(start["entry"]["entryId"]), start["entry"])
        self.assertTrue((self.directory / f"{old}.sqlite3").exists())
        self.assertEqual(ImportantLogCatalog(self.directory).catalog_snapshot()["databaseCount"], 2)
        after = {path.name: sha256(path.read_bytes()).hexdigest() for path in files if path.exists()}
        self.assertEqual(after, before)

    def test_crash_after_intent_before_pointer_recovers_without_deleting(self):
        catalog = ImportantLogCatalog(self.directory, target_bytes=50_000,
                                      low_water_bytes=40_000, create=True)
        old = catalog.active_uuid()
        catalog.append(self.subject, "start", [], BODY, now=NOW)
        actual_write = catalog_module._write_control

        def fail_pointer(fd, name, value):
            if name == "active.json" and value["generation"] == 2:
                raise OSError("injected after durable intent")
            return actual_write(fd, name, value)

        with patch.object(catalog_module, "_write_control", side_effect=fail_pointer):
            result = catalog.append(self.subject, "event", [], LARGE_BODY, now=NOW)
        self.assertEqual(result["state"], "io_error")
        self.assertTrue((self.directory / "rollover.intent.json").exists())
        recovered = ImportantLogCatalog(self.directory).catalog_snapshot()
        self.assertEqual(recovered["generation"], 2)
        self.assertEqual(recovered["archives"], [old])
        self.assertEqual(recovered["databaseCount"], 2)
        self.assertFalse((self.directory / "rollover.intent.json").exists())
        self.assertTrue((self.directory / f"{old}.sqlite3").exists())

    def test_missing_pointer_with_db_fails_closed_and_preserves_it(self):
        catalog = ImportantLogCatalog(self.directory, create=True)
        identity = catalog.active_uuid()
        (self.directory / "active.json").unlink()
        with self.assertRaises(ImportantLogStoreError):
            ImportantLogCatalog(self.directory, create=True)
        self.assertTrue((self.directory / f"{identity}.sqlite3").exists())

    def test_confirmed_subject_facts_route_to_new_active_with_lazy_state_and_replay_key(self):
        catalog = ImportantLogCatalog(self.directory, create=True)
        sink = ActiveSubjectLog(catalog)
        task = {"schemaVersion": 1, "kind": "task", "id": str(uuid4())}
        first_id, second_id = str(uuid4()), str(uuid4())
        self.assertTrue(sink.record_delivery(task, message_id=first_id, revision=1,
                                             kind="manager_instruction", content="first",
                                             delivered_at="2026-10-09T00:00:00Z"))
        old = catalog.active_uuid()
        with catalog._lease() as fd:
            pointer = catalog._current(fd)
            with ImportantLogStore.open(self.directory, old, _catalog_owned=True) as store:
                catalog._rotate(fd, pointer, store)
        new = catalog.active_uuid()
        self.assertNotEqual(old, new)
        self.assertEqual(sink.state(task)["archival_state"], "open")
        with ImportantLogStore.open(self.directory, new, readonly=True) as active_before:
            self.assertEqual(active_before._connection.execute("""SELECT COUNT(*) FROM sqlite_master
                WHERE type='table' AND name='subject_lifecycle_v1'""").fetchone()[0], 0)
        self.assertTrue(sink.record_delivery(task, message_id=second_id, revision=1,
                                             kind="manager_instruction", content="second",
                                             delivered_at="2026-10-09T00:01:00Z"))
        self.assertFalse(sink.record_delivery(task, message_id=first_id, revision=1,
                                              kind="manager_instruction", content="first",
                                              delivered_at="2026-10-09T00:00:00Z"))
        with ImportantLogStore.open(self.directory, old, readonly=True, immutable=True) as archived:
            self.assertEqual(archived._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)
        with ImportantLogStore.open(self.directory, new, readonly=True) as active:
            self.assertEqual(active._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)
            self.assertEqual(active._connection.execute("SELECT COUNT(*) FROM subject_events_v1").fetchone()[0], 2)

    def test_terminal_end_after_rollover_uses_original_durable_start(self):
        catalog = ImportantLogCatalog(self.directory, create=True)
        sink = ActiveSubjectLog(catalog)
        command_id = str(uuid4())
        start = {"type": "terminal_started", "seq": 1, "command_id": command_id,
                 "task_id": None, "started_at": "2026-10-09T00:00:00Z"}
        self.assertTrue(sink.record_terminal(start))
        old = catalog.active_uuid()
        with catalog._lease() as fd:
            pointer = catalog._current(fd)
            with ImportantLogStore.open(self.directory, old, _catalog_owned=True) as store:
                catalog._rotate(fd, pointer, store)
        end = {"type": "terminal_ended", "seq": 2, "command_id": command_id,
               "status": "exited", "ended_at": "2026-10-09T00:01:00Z"}
        self.assertTrue(sink.record_terminal(end))
        state = SubjectLifecycle(self.directory, catalog.active_uuid()).state(
            {"schemaVersion": 1, "kind": "command", "id": command_id})
        self.assertEqual(state["archival_state"], "closed")

    def test_busy_reader_rollover_keeps_wal_and_archive_read_does_not_mutate_it(self):
        catalog = ImportantLogCatalog(self.directory, create=True)
        old = catalog.active_uuid()
        first = catalog.append(self.subject, "start", [], BODY, now=NOW)
        with ImportantLogStore.open(self.directory, old, readonly=True) as reader:
            reader._connection.execute("BEGIN")
            reader._connection.execute("SELECT COUNT(*) FROM entries").fetchone()
            catalog.append(self.subject, "event", [], LARGE_BODY, now=NOW)
            with catalog._lease() as fd:
                pointer = catalog._current(fd)
                with ImportantLogStore.open(self.directory, old, _catalog_owned=True) as writer:
                    rollover = catalog._rotate(fd, pointer, writer)
            self.assertEqual(rollover["checkpoint"], "busy")
            files = [self.directory / (old + ".sqlite3" + suffix) for suffix in ("", "-wal", "-shm")]
            before = {path.name: sha256(path.read_bytes()).hexdigest() for path in files if path.exists()}
            self.assertIn(old + ".sqlite3-wal", before)
            self.assertEqual(catalog.protected_anchor_ids(old), [first["entry"]["entryId"]])
            after = {path.name: sha256(path.read_bytes()).hexdigest() for path in files if path.exists()}
            self.assertEqual(after, before)
            reader._connection.execute("ROLLBACK")


if __name__ == "__main__":
    unittest.main()
