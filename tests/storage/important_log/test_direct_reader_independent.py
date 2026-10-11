"""Independent B1 active-page, archive, and pagination contracts."""

from contextlib import contextmanager
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


BODY = {"schemaVersion": 1, "facts": ["B1"], "interpretations": [], "unknowns": []}


class IndependentDirectReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "catalog"
        self.catalog = ImportantLogCatalog(self.directory, create=True,
                                           target_bytes=10_000_000, low_water_bytes=8_000_000)
        self.reader = ImportantLogReader(self.directory)
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}

    def _append(self, count):
        saved = []
        for _ in range(count):
            result = self.catalog.append(self.subject, "event", [], BODY)
            self.assertEqual(result["state"], "ok")
            saved.append(result["entry"]["entryId"])
        return saved

    def _rotate(self):
        old = self.catalog.active_uuid()
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with ImportantLogStore.open(self.directory, old, _catalog_owned=True) as store:
                self.catalog._rotate(fd, pointer, store)
        return old

    def test_active_page_lease_covers_ro_open_query_close_and_serializes_mutation(self):
        active = self.catalog.active_uuid()
        self._append(1)
        with self.reader._view(active) as store:
            self.assertTrue(store.readonly)
            self.assertEqual(store._connection.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(len(store.list_entries(self.subject)), 1)
            blocked = self.catalog.append(self.subject, "event", [], BODY)
            self.assertEqual(blocked["state"], "busy")
            self.assertIsNone(blocked["durableEntryId"])
            with self.assertRaisesRegex(sqlite3.OperationalError, "lease busy"):
                with self.catalog._lease():
                    self.fail("rollover/delete mutation entered while reader held EX")
        self.assertEqual(self.catalog.append(self.subject, "event", [], BODY)["state"], "ok")
        old = self._rotate()
        self.assertEqual(old, active)
        self.assertEqual(self.catalog.catalog_snapshot()["generation"], 2)

    def test_latest_wal_visible_with_unchanged_main_wal_and_private_sidecars(self):
        active = self.catalog.active_uuid()
        main = self.directory / f"{active}.sqlite3"
        wal = self.directory / f"{active}.sqlite3-wal"
        shm = self.directory / f"{active}.sqlite3-shm"
        with ImportantLogStore.open(self.directory, active, readonly=True) as pinned:
            pinned._connection.execute("BEGIN")
            pinned._connection.execute("SELECT COUNT(*) FROM entries").fetchone()
            entry = self._append(1)[0]
            self.assertGreater(wal.stat().st_size, 0)
            before = {name: sha256(path.read_bytes()).hexdigest()
                      for name, path in (("main", main), ("wal", wal))}
            with patch.object(self.reader, "_copy_file", side_effect=AssertionError("active copied")):
                result = self.reader.show(self.subject)
            self.assertTrue(result["complete"], result["unavailable"])
            self.assertEqual([item["entry"]["entryId"] for item in result["entries"]], [entry])
            self.assertEqual({"main": sha256(main.read_bytes()).hexdigest(),
                              "wal": sha256(wal.read_bytes()).hexdigest()}, before)
            for sidecar in (wal, shm):
                info = sidecar.stat()
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                self.assertEqual(info.st_uid, os.getuid())
                self.assertEqual(info.st_nlink, 1)
            self.assertEqual(len(list(self.directory.glob("*.sqlite3"))), 1)

    def test_missing_sidecars_may_be_created_privately_and_writer_reopens(self):
        active = self.catalog.active_uuid()
        main = self.directory / f"{active}.sqlite3"
        before_main = sha256(main.read_bytes()).hexdigest()
        sidecars = [self.directory / f"{active}.sqlite3{suffix}" for suffix in ("-wal", "-shm")]
        self.assertTrue(all(not path.exists() for path in sidecars))
        empty = self.reader.show(self.subject)
        self.assertTrue(empty["complete"], empty["unavailable"])
        self.assertEqual(empty["entries"], [])
        self.assertEqual(sha256(main.read_bytes()).hexdigest(), before_main)
        for path in sidecars:
            if path.exists():
                info = path.stat()
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                self.assertEqual(info.st_uid, os.getuid())
                self.assertEqual(info.st_nlink, 1)
        self.assertEqual(len(list(self.directory.glob("*.sqlite3"))), 1)
        self.assertEqual(self.catalog.append(self.subject, "event", [], BODY)["state"], "ok")

    def test_token_keyset_survives_deleted_cursor_and_rejects_wrong_subject(self):
        identities = self._append(108)
        with patch.object(ImportantLogStore, "get_entry", side_effect=AssertionError("cursor lookup")), \
             patch.object(ImportantLogStore, "list_entries", side_effect=AssertionError("legacy scan")):
            first = self.reader.show(self.subject, limit=17)
            self.assertTrue(first["complete"])
            token = first["nextAfter"]
            self.assertIsNotNone(token)
        first_ids = [row["entry"]["entryId"] for row in first["entries"]]
        self.assertEqual(len(first_ids), 17)
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with ImportantLogStore.open(self.directory, pointer["active"], _catalog_owned=True) as store:
                store._connection.execute("DELETE FROM entries WHERE entry_id=?", (first_ids[-1],))
        with patch.object(ImportantLogStore, "get_entry", side_effect=AssertionError("cursor lookup")):
            second = self.reader.show(self.subject, limit=17, after=token)
        self.assertTrue(second["complete"], second["unavailable"])
        second_ids = {row["entry"]["entryId"] for row in second["entries"]}
        self.assertEqual(len(second_ids), 17)
        self.assertFalse(second_ids.intersection(first_ids))
        self.assertTrue(second_ids.issubset(set(identities)))
        wrong = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        for item, cursor in ((wrong, token), (self.subject, "%%malformed%%")):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                self.reader.show(item, after=cursor)

    def test_generation_switch_between_database_pages_is_partial(self):
        self._append(2)
        original = self.reader._view
        switched = False

        @contextmanager
        def rotate_after_first_page(identity):
            nonlocal switched
            with original(identity) as store:
                yield store
            if not switched:
                switched = True
                self._rotate()

        with patch.object(self.reader, "_view", side_effect=rotate_after_first_page):
            result = self.reader.show(self.subject, limit=1)
        self.assertTrue(switched)
        self.assertFalse(result["complete"])
        self.assertEqual(result["startGeneration"], 1)
        self.assertEqual(result["endGeneration"], 2)
        self.assertIn({"dbUuid": None, "reason": "generation_changed"}, result["unavailable"])
        self.assertEqual(len(result["entries"]), 1)

    def test_list_queries_subject_stats_and_status_never_opens_sqlite(self):
        self._append(2)
        statements = []
        original = self.reader._view

        @contextmanager
        def trace_page(identity):
            with original(identity) as store:
                store._connection.set_trace_callback(statements.append)
                try:
                    yield store
                finally:
                    store._connection.set_trace_callback(None)

        with patch.object(self.reader, "_view", side_effect=trace_page):
            listed = self.reader.list_subjects(limit=10)
        self.assertTrue(listed["complete"], listed["unavailable"])
        self.assertEqual(listed["subjects"][0]["entryCount"], 2)
        self.assertTrue(any("FROM subject_stats" in query for query in statements))
        self.assertFalse(any("FROM entries" in query or "GROUP BY" in query.upper()
                             for query in statements))
        with patch.object(ImportantLogStore, "open", side_effect=AssertionError("status opened SQLite")):
            status = self.reader.status()
        self.assertTrue(status["complete"])
        self.assertEqual(status["databases"][0]["availability"], "unknown")


if __name__ == "__main__":
    unittest.main()
