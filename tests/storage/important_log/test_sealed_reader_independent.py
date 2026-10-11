"""Independent LG-D20 sealed-page and WAL-fallback contracts."""

from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.storage.important_log import ImportantLogCatalog, ImportantLogReader, ImportantLogStore
import workbench.storage.important_log.reader as reader_module


BODY = {"schemaVersion": 1, "facts": ["sealed-page"], "interpretations": [], "unknowns": []}


class IndependentSealedReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "catalog"
        self.catalog = ImportantLogCatalog(self.directory, create=True,
                                           target_bytes=10_000_000, low_water_bytes=8_000_000)
        self.reader = ImportantLogReader(self.directory)
        self.subject = {"schemaVersion": 1, "kind": "task", "id": str(uuid4())}

    def _sealed(self):
        saved = self.catalog.append(self.subject, "event", [], BODY)
        self.assertEqual(saved["state"], "ok")
        old = self.catalog.active_uuid()
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with ImportantLogStore.open(self.directory, old, _catalog_owned=True) as store:
                rollover = self.catalog._rotate(fd, pointer, store)
        self.assertEqual(rollover["checkpoint"], "complete")
        self.assertEqual(self.catalog.catalog_snapshot()["archiveChecks"][old], "complete")
        return old, saved["entry"]["entryId"]

    def _inventory(self):
        return {item.name: (sha256(item.read_bytes()).hexdigest(),
                            stat.S_IMODE(item.stat().st_mode))
                for item in self.directory.iterdir() if item.is_file()}

    def test_sealed_absent_wal_direct_immutable_no_copy_and_ex_through_query_close(self):
        archived, entry_id = self._sealed()
        before = self._inventory()
        calls = []
        original_open = ImportantLogStore.open

        def track_open(directory, identity, **kwargs):
            calls.append((Path(directory), identity, kwargs.copy()))
            return original_open(directory, identity, **kwargs)

        with patch.object(ImportantLogStore, "open", side_effect=track_open), \
             patch.object(self.reader, "_copy_file", side_effect=AssertionError("sealed copied")), \
             patch.object(self.reader, "_verify_file", side_effect=AssertionError("sealed rehashed")), \
             patch.object(reader_module.tempfile, "TemporaryDirectory",
                          side_effect=AssertionError("sealed scratch allocated")):
            with self.reader._view(archived) as store:
                self.assertTrue(store.readonly)
                self.assertEqual(store._connection.execute("PRAGMA query_only").fetchone()[0], 1)
                self.assertEqual(store.list_entries(self.subject)[0]["entryId"], entry_id)
                blocked = self.catalog.append(self.subject, "event", [], BODY)
                self.assertEqual(blocked["state"], "busy")
                with self.assertRaisesRegex(sqlite3.OperationalError, "lease busy"):
                    with self.catalog._lease():
                        self.fail("rollover/delete lease entered during page")
                self.assertEqual(self._inventory(), before)
        self.assertEqual(calls, [(self.directory, archived,
                                  {"readonly": True, "immutable": True})])
        self.assertEqual(self._inventory(), before)
        self.assertEqual(self.catalog.append(self.subject, "event", [], BODY)["state"], "ok")

    def test_zero_byte_wal_remains_eligible_and_query_error_releases_ex(self):
        archived, _ = self._sealed()
        wal = self.directory / f"{archived}.sqlite3-wal"
        wal.touch(mode=0o600)
        self.assertEqual(wal.stat().st_size, 0)
        before = self._inventory()
        with patch.object(self.reader, "_copy_file", side_effect=AssertionError("zero WAL copied")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
                with self.reader._view(archived) as store:
                    store._connection.execute("SELECT * FROM no_such_fixture_table")
        self.assertEqual(self._inventory(), before)
        self.assertEqual(self.catalog.append(self.subject, "event", [], BODY)["state"], "ok")

    def test_complete_flag_with_real_nonempty_wal_falls_back_and_reads_latest(self):
        archived, _ = self._sealed()
        path = self.directory / f"{archived}.sqlite3"
        with ImportantLogStore.open(self.directory, archived, readonly=True) as pinned:
            pinned._connection.execute("BEGIN")
            pinned._connection.execute("SELECT COUNT(*) FROM entries").fetchone()
            connection = sqlite3.connect(path, isolation_level=None)
            self.addCleanup(connection.close)
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE synthetic_latest(payload TEXT)")
            marker = str(uuid4())
            connection.execute("INSERT INTO synthetic_latest VALUES(?)", (marker,))
            self.assertGreater((self.directory / f"{archived}.sqlite3-wal").stat().st_size, 0)
            self.assertTrue((self.directory / f"{archived}.sqlite3-shm").exists())
            before = self._inventory()
            copies = []
            original_copy = self.reader._copy_file

            def track_copy(fd, name, destination):
                copies.append(name)
                return original_copy(fd, name, destination)

            with patch.object(self.reader, "_copy_file", side_effect=track_copy):
                with self.reader._view(archived) as store:
                    self.assertEqual(store._connection.execute(
                        "SELECT payload FROM synthetic_latest").fetchone()[0], marker)
            self.assertEqual(set(copies), {f"{archived}.sqlite3",
                                            f"{archived}.sqlite3-wal",
                                            f"{archived}.sqlite3-shm"})
            self.assertEqual(self._inventory(), before)

    def test_symlinked_archive_sidecar_is_unavailable_without_following_target(self):
        archived, _ = self._sealed()
        outside = Path(self.temp.name) / "outside"
        outside.write_bytes(b"untouched")
        (self.directory / f"{archived}.sqlite3-wal").symlink_to(outside)
        before = self._inventory()
        response = self.reader.show(self.subject)
        self.assertFalse(response["complete"])
        # The catalog-wide sidecar safety check may reject before a DB page starts.
        self.assertEqual(response["unavailable"], [{"dbUuid": None, "reason": "unavailable"}])
        self.assertEqual(outside.read_bytes(), b"untouched")
        self.assertEqual(self._inventory(), before)
        self.assertEqual(list(Path(self.temp.name).glob("workbench-important-read-*")), [])


if __name__ == "__main__":
    unittest.main()
