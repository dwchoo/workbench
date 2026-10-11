"""Independent reader contracts for the bounded DVL-07 continuation."""

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.storage.important_log import ImportantLogCatalog, ImportantLogReader, ImportantLogStore
from workbench.storage.important_log.store import ImportantLogStoreError
import workbench.storage.important_log.reader as reader_module


BODY = {"schemaVersion": 1, "facts": ["independent"], "interpretations": [], "unknowns": []}


class IndependentReaderPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "catalog"
        self.catalog = ImportantLogCatalog(self.directory, create=True, target_bytes=5_000_000,
                                           low_water_bytes=4_000_000)
        self.reader = ImportantLogReader(self.directory)
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}

    def _append(self, count):
        result = []
        for _ in range(count):
            saved = self.catalog.append(self.subject, "event", [], BODY)
            self.assertEqual(saved["state"], "ok")
            result.append(saved["entry"]["entryId"])
        return result

    def _rollover(self):
        archived = self.catalog.active_uuid()
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with ImportantLogStore.open(self.directory, archived, _catalog_owned=True) as store:
                self.catalog._rotate(fd, pointer, store)
        self.assertIn(archived, self.catalog.catalog_snapshot()["archives"])
        return archived

    def _archive_hashes(self, identity):
        return {path.name: sha256(path.read_bytes()).hexdigest()
                for path in self.directory.glob(f"{identity}.sqlite3*")}

    def test_page_cursor_covers_more_than_one_hundred_entries_per_database(self):
        archive_ids = self._append(103)
        archived = self._rollover()
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with self.catalog._archive_source(fd, pointer, archived) as source, \
                 ImportantLogStore.open(self.directory, pointer["active"], _catalog_owned=True) as target:
                target.import_verified_entry(source, archive_ids[0])
        active_ids = self._append(107)
        expected = set(archive_ids + active_ids)
        seen = []
        cursor = None
        for _ in range(20):
            result = self.reader.show(self.subject, limit=17, after=cursor)
            self.assertTrue(result["complete"], result["unavailable"])
            self.assertFalse(result["unavailable"])
            seen.extend(row["entry"]["entryId"] for row in result["entries"])
            self.assertLessEqual(len(result["entries"]), 17)
            cursor = result["nextAfter"]
            if cursor is None:
                break
        else:
            self.fail("page cursor did not terminate")
        self.assertEqual(set(seen), expected)
        self.assertEqual(len(seen), len(expected))
        archive_rows = self.reader.show(self.subject, limit=100)["entries"]
        self.assertTrue(any(archived in row["sourceDbUuids"] for row in archive_rows))
        carried = next(row for row in archive_rows if row["entry"]["entryId"] == archive_ids[0])
        self.assertEqual(len(carried["sourceDbUuids"]), 2)

    def test_per_database_failure_keeps_available_rows_and_reports_missing_uuid(self):
        archived_entry = self._append(1)[0]
        archived = self._rollover()
        active_entry = self._append(1)[0]
        original = self.reader._view

        @contextmanager
        def interrupted(identity):
            if identity == archived:
                raise ImportantLogStoreError("important catalog busy")
            with original(identity) as store:
                yield store

        with patch.object(self.reader, "_view", side_effect=interrupted):
            page = self.reader.show(self.subject)
            subjects = self.reader.list_subjects()
        self.assertFalse(page["complete"])
        self.assertEqual(page["unavailable"], [{"dbUuid": archived, "reason": "busy"}])
        self.assertEqual({row["entry"]["entryId"] for row in page["entries"]}, {active_entry})
        self.assertNotIn(archived_entry, {row["entry"]["entryId"] for row in page["entries"]})
        self.assertFalse(subjects["complete"])
        self.assertEqual(subjects["unavailable"], [{"dbUuid": archived, "reason": "busy"}])
        self.assertEqual(len(subjects["subjects"]), 1)

    def test_copy_error_cleans_scratch_and_keeps_source_bytes(self):
        archived = self.catalog.active_uuid()
        # A pinned pre-write snapshot makes the real rollover checkpoint busy.
        # Its nonempty WAL and SHM require the private-copy branch.
        with ImportantLogStore.open(self.directory, archived, readonly=True) as pinned:
            pinned._connection.execute("BEGIN")
            pinned._connection.execute("SELECT COUNT(*) FROM entries").fetchone()
            entry_id = self._append(1)[0]
            with self.catalog._lease() as fd:
                pointer = self.catalog._current(fd)
                with ImportantLogStore.open(self.directory, archived, _catalog_owned=True) as store:
                    rollover = self.catalog._rotate(fd, pointer, store)
            self.assertEqual(rollover["checkpoint"], "busy")
            self.assertGreater((self.directory / f"{archived}.sqlite3-wal").stat().st_size, 0)
            self.assertTrue((self.directory / f"{archived}.sqlite3-shm").exists())
            before = self._archive_hashes(archived)
            original_temp = tempfile.TemporaryDirectory
            scratch = []

            def tracked_temp(*args, **kwargs):
                kwargs["dir"] = self.temp.name
                item = original_temp(*args, **kwargs)
                scratch.append(Path(item.name))
                return item

            original_copy = self.reader._copy_file
            calls = 0

            def fail_after_copy(fd, name, destination):
                nonlocal calls
                result = original_copy(fd, name, destination)
                calls += 1
                if calls == 1:
                    raise OSError("injected copy failure")
                return result

            with patch.object(reader_module.tempfile, "TemporaryDirectory", side_effect=tracked_temp), \
                 patch.object(self.reader, "_copy_file", side_effect=fail_after_copy):
                failed = self.reader.show(self.subject)
            self.assertFalse(failed["complete"])
            self.assertEqual(failed["unavailable"][0]["reason"], "unavailable")
            self.assertEqual(calls, 1)
            self.assertTrue(scratch)
            self.assertTrue(all(not path.exists() for path in scratch))
            self.assertEqual(self._archive_hashes(archived), before)
            recovered = self.reader.show(self.subject)
            self.assertTrue(recovered["complete"])
            self.assertEqual(recovered["entries"][0]["entry"]["entryId"], entry_id)
            self.assertEqual(self._archive_hashes(archived), before)

    def test_committed_wal_is_visible_without_changing_source_files(self):
        identity = self.catalog.active_uuid()
        with ImportantLogStore.open(self.directory, identity, readonly=True) as pinned:
            pinned._connection.execute("BEGIN")
            pinned._connection.execute("SELECT COUNT(*) FROM entries").fetchone()
            saved = self.catalog.append(self.subject, "event", [], BODY)
            self.assertEqual(saved["state"], "ok")
            wal = self.directory / f"{identity}.sqlite3-wal"
            self.assertGreater(wal.stat().st_size, 0)
            before = {path.name: sha256(path.read_bytes()).hexdigest()
                      for path in (self.directory / f"{identity}.sqlite3", wal)}
            page = self.reader.show(self.subject)
            self.assertTrue(page["complete"], page["unavailable"])
            self.assertEqual([row["entry"]["entryId"] for row in page["entries"]],
                             [saved["entry"]["entryId"]])
            self.assertEqual({path.name: sha256(path.read_bytes()).hexdigest()
                              for path in (self.directory / f"{identity}.sqlite3", wal)}, before)

    def test_status_does_not_snapshot_or_claim_unknown_database_is_healthy(self):
        with patch.object(self.reader, "_view", side_effect=AssertionError("full copy started")), \
             patch.object(ImportantLogStore, "open", side_effect=AssertionError("SQLite opened")):
            result = self.reader.status()
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["databases"]), 1)
        self.assertEqual(result["databases"][0]["availability"], "unknown")
        self.assertIsNone(result["databases"][0]["lastChecked"])

    def test_unregistered_symlinked_database_is_rejected_before_read(self):
        outside = Path(self.temp.name) / "outside"
        outside.write_bytes(b"sentinel")
        (self.directory / f"{uuid4()}.sqlite3").symlink_to(outside)
        before = outside.read_bytes()
        status = self.reader.status()
        page = self.reader.show(self.subject)
        self.assertFalse(status["complete"])
        self.assertFalse(page["complete"])
        self.assertEqual(status["unavailable"][0]["reason"], "unavailable")
        self.assertEqual(page["unavailable"][0]["reason"], "unavailable")
        self.assertEqual(outside.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
