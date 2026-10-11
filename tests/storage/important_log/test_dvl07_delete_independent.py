"""DVL-07 deletion must remain exact, durable, and honest after a partial unlink."""

from __future__ import annotations

from datetime import datetime, timezone
import errno
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.storage.important_log import ImportantLogCatalog, ImportantLogReader
from workbench.storage.important_log.store import ImportantLogStoreError


BODY = {"schemaVersion": 1, "facts": ["persisted"], "interpretations": [], "unknowns": []}


class Dvl07DeletionFaults(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="dvl07-delete-", dir="/dev/shm")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "important"
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        catalog = ImportantLogCatalog(self.directory, create=True,
                                      target_bytes=36_000, low_water_bytes=30_000)
        result = catalog.append(self.subject, "start", [], BODY,
                                now=datetime(2026, 10, 10, tzinfo=timezone.utc))
        self.assertEqual(result["state"], "ok")
        self.assertIsNotNone(result["rollover"])
        self.archive = result["rollover"]["from"]
        self.active = result["rollover"]["to"]

    def test_active_uuid_and_noncanonical_alias_never_delete_any_file(self) -> None:
        catalog = ImportantLogCatalog(self.directory)
        before = catalog.catalog_snapshot()
        for value in (self.active, self.archive.upper(), "../" + self.archive,
                      str(self.directory / (self.archive + ".sqlite3")), str(uuid4())):
            with self.subTest(value=value):
                with self.assertRaises(ImportantLogStoreError):
                    catalog.delete_archive(value)
                self.assertEqual(catalog.catalog_snapshot()["archives"], before["archives"])
                self.assertEqual(catalog.catalog_snapshot()["active"], before["active"])
                self.assertTrue((self.directory / (self.archive + ".sqlite3")).exists())

    def test_partial_unlink_leaves_intent_and_recovery_deletes_only_selected_uuid(self) -> None:
        catalog = ImportantLogCatalog(self.directory)
        base = self.archive + ".sqlite3"
        original_unlink = os.unlink
        failed = False

        def interrupt_selected(name, *args, **kwargs):
            nonlocal failed
            if name == base and not failed:
                failed = True
                raise OSError(errno.EIO, "synthetic unlink fault")
            return original_unlink(name, *args, **kwargs)

        with patch("workbench.storage.important_log.catalog.os.unlink", side_effect=interrupt_selected):
            with self.assertRaises(OSError):
                catalog.prepare_and_delete_archive(self.archive)
        self.assertTrue(failed)
        self.assertTrue((self.directory / "delete.intent.json").exists())
        self.assertTrue((self.directory / base).exists())
        self.assertTrue((self.directory / (self.active + ".sqlite3")).exists())
        # A read before recovery must not report one healthy DB while intent is pending.
        unreadable = ImportantLogReader(self.directory).status()
        self.assertFalse(unreadable["complete"])
        self.assertTrue(unreadable["unavailable"])
        recovered = ImportantLogCatalog(self.directory).catalog_snapshot()
        self.assertEqual((recovered["active"], recovered["archives"], recovered["databaseCount"]),
                         (self.active, [], 1))
        self.assertFalse((self.directory / "delete.intent.json").exists())
        self.assertFalse((self.directory / base).exists())
        self.assertTrue((self.directory / (self.active + ".sqlite3")).exists())

    def test_unverified_source_symlink_blocks_cleanup(self) -> None:
        outside = self.directory.parent / "standin"
        outside.write_bytes(b"fixture only")
        outside.chmod(0o600)
        sidecar = self.directory / (self.archive + ".sqlite3-shm")
        sidecar.symlink_to(outside)
        with self.assertRaises((OSError, ImportantLogStoreError)):
            ImportantLogCatalog(self.directory).prepare_and_delete_archive(self.archive)
        self.assertTrue(sidecar.is_symlink())
        self.assertTrue((self.directory / (self.archive + ".sqlite3")).exists())
        self.assertTrue((self.directory / (self.active + ".sqlite3")).exists())

    def test_open_archive_page_serializes_user_deletion(self) -> None:
        catalog = ImportantLogCatalog(self.directory)
        reader = ImportantLogReader(self.directory)
        with reader._view(self.archive) as source:
            self.assertIsNotNone(source.list_entries(self.subject))
            with self.assertRaises(sqlite3.OperationalError):
                catalog.prepare_and_delete_archive(self.archive)
            self.assertTrue((self.directory / (self.archive + ".sqlite3")).exists())
            self.assertFalse((self.directory / "delete.intent.json").exists())
        result = catalog.prepare_and_delete_archive(self.archive)
        self.assertEqual((result["state"], result["databaseCount"]), ("deleted", 1))


if __name__ == "__main__":
    unittest.main()
