"""LG-D18 active direct page and keyset cursor checks on disposable catalogs."""

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.storage.important_log import ImportantLogCatalog, ImportantLogReader, ImportantLogStore


BODY = {"schemaVersion": 1, "facts": ["page"], "interpretations": [], "unknowns": []}


class DirectReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "catalog"
        self.catalog = ImportantLogCatalog(self.directory, create=True,
                                           target_bytes=10_000_000, low_water_bytes=8_000_000)
        self.reader = ImportantLogReader(self.directory)
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}

    def test_active_page_opens_source_without_copy_and_keeps_main_wal(self):
        entry = self.catalog.append(self.subject, "event", [], BODY)["entry"]["entryId"]
        active = self.catalog.active_uuid()
        paths = [self.directory / f"{active}.sqlite3{suffix}" for suffix in ("", "-wal")]
        before = {path.name: sha256(path.read_bytes()).hexdigest() for path in paths if path.exists()}
        with patch.object(self.reader, "_copy_file", side_effect=AssertionError("active copied")):
            result = self.reader.show(self.subject, limit=10)
        after = {path.name: sha256(path.read_bytes()).hexdigest() for path in paths if path.exists()}
        self.assertTrue(result["complete"], result["unavailable"])
        self.assertEqual([x["entry"]["entryId"] for x in result["entries"]], [entry])
        self.assertTrue(all(after[name] == digest for name, digest in before.items()))
        if paths[1].name not in before:
            self.assertEqual(paths[1].stat().st_size, 0)
        self.assertEqual(len(list(self.directory.glob("*.sqlite3"))), 1)

    def test_keyset_page_skips_cursor_lookup_and_detects_generation_change(self):
        for _ in range(108):
            self.assertEqual(self.catalog.append(self.subject, "event", [], BODY)["state"], "ok")
        with ImportantLogStore.open(self.directory, self.catalog.active_uuid(), readonly=True) as store:
            first_plan = store._connection.execute(
                "EXPLAIN QUERY PLAN SELECT entry_id FROM entries WHERE subject_kind=? "
                "AND subject_id=? ORDER BY recorded_at,entry_id LIMIT ?",
                (self.subject["kind"], self.subject["id"], 18)).fetchall()
            later_plan = store._connection.execute(
                "EXPLAIN QUERY PLAN SELECT entry_id FROM entries WHERE subject_kind=? "
                "AND subject_id=? AND (recorded_at,entry_id)>(?,?) "
                "ORDER BY recorded_at,entry_id LIMIT ?",
                (self.subject["kind"], self.subject["id"], "2026-01-01T00:00:00Z", str(uuid4()), 18)
            ).fetchall()
        self.assertTrue(all("entries_by_subject" in row[3] for row in first_plan))
        self.assertTrue(all("entries_by_subject" in row[3] for row in later_plan))
        with patch.object(ImportantLogStore, "get_entry", side_effect=AssertionError("cursor lookup")), \
             patch.object(ImportantLogStore, "list_entries", side_effect=AssertionError("old full scan")):
            page = self.reader.show(self.subject, limit=17)
            self.assertTrue(page["complete"])
            token = page["nextAfter"]
            self.assertIsNotNone(token)
            next_page = self.reader.show(self.subject, limit=17, after=token)
            self.assertTrue(next_page["complete"])
        self.assertFalse(set(x["entry"]["entryId"] for x in page["entries"]) &
                         set(x["entry"]["entryId"] for x in next_page["entries"]))
        with self.assertRaises(ValueError):
            self.reader.show(self.subject, after="not-a-token")
        other = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        with self.assertRaises(ValueError):
            self.reader.show(other, after=token)
        old = self.catalog.active_uuid()
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with ImportantLogStore.open(self.directory, old, _catalog_owned=True) as store:
                self.catalog._rotate(fd, pointer, store)
        changed = self.reader.show(self.subject, limit=17, after=token)
        self.assertFalse(changed["complete"])
        self.assertIn({"dbUuid": None, "reason": "generation_changed"}, changed["unavailable"])

    def test_list_uses_subject_stats_and_reports_generation_change(self):
        self.catalog.append(self.subject, "event", [], BODY)
        sql = []
        original = self.reader._view

        @contextmanager
        def traced(identity):
            with original(identity) as store:
                store._connection.set_trace_callback(sql.append)
                try:
                    yield store
                finally:
                    store._connection.set_trace_callback(None)

        with patch.object(self.reader, "_view", side_effect=traced):
            result = self.reader.list_subjects(limit=1)
        self.assertTrue(result["complete"])
        self.assertEqual(result["subjects"][0]["entryCount"], 1)
        self.assertTrue(any("FROM subject_stats" in statement for statement in sql))
        self.assertFalse(any("GROUP BY" in statement.upper() for statement in sql))


if __name__ == "__main__":
    unittest.main()
