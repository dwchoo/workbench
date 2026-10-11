"""Small contract checks for the bounded private reader continuation."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.storage.important_log import ImportantLogCatalog, ImportantLogReader, ImportantLogStore
import workbench.storage.important_log.reader as reader_module


BODY = {"schemaVersion": 1, "facts": ["kept"], "interpretations": [], "unknowns": []}


class ReaderPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "important"
        self.catalog = ImportantLogCatalog(self.directory, create=True)
        self.reader = ImportantLogReader(self.directory)
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}

    def test_status_is_metadata_only_and_unknown_when_unchecked(self):
        with patch.object(self.reader, "_view", side_effect=AssertionError("snapshot started")):
            result = self.reader.status()
        self.assertTrue(result["complete"])
        self.assertEqual(result["databases"][0]["availability"], "unknown")
        self.assertIsNone(result["databases"][0]["lastChecked"])

    def test_show_pages_past_one_hundred_across_two_databases(self):
        first_entry = None
        for _ in range(125):
            result = self.catalog.append(self.subject, "event", [], BODY)
            self.assertEqual(result["state"], "ok")
            first_entry = first_entry or result["entry"]["entryId"]
        old = self.catalog.active_uuid()
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with ImportantLogStore.open(self.directory, old, _catalog_owned=True) as store:
                self.catalog._rotate(fd, pointer, store)
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with self.catalog._archive_source(fd, pointer, old) as source, \
                 ImportantLogStore.open(self.directory, pointer["active"], _catalog_owned=True) as target:
                target.import_verified_entry(source, first_entry)
        for _ in range(125):
            self.assertEqual(self.catalog.append(self.subject, "event", [], BODY)["state"], "ok")
        found = []
        after = None
        while True:
            result = self.reader.show(self.subject, limit=41, after=after)
            self.assertTrue(result["complete"], result["unavailable"])
            found.extend(item["entry"]["entryId"] for item in result["entries"])
            after = result["nextAfter"]
            if after is None:
                break
        self.assertEqual(len(found), 250)
        self.assertEqual(len(set(found)), 250)
        first = self.reader.show(self.subject, limit=100)
        carried = next(item for item in first["entries"] if item["entry"]["entryId"] == first_entry)
        self.assertEqual(len(carried["sourceDbUuids"]), 2)

    def test_busy_is_structured_even_when_outer_catalog_unavailable(self):
        with patch.object(self.reader, "_catalog", side_effect=reader_module.ImportantLogStoreError(
                "important catalog busy")):
            for result in (self.reader.status(), self.reader.list_subjects(), self.reader.show(self.subject)):
                self.assertFalse(result["complete"])
                self.assertEqual(result["unavailable"], [{"dbUuid": None, "reason": "busy"}])


if __name__ == "__main__":
    unittest.main()
