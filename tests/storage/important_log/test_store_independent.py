"""Independent private-store and bounded-codec foundation tests."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.contracts.logs_v1 import canonical_json
from workbench.storage.important_log import ImportantLogStore, ImportantLogStoreError, decode_body, encode_body

ROOT = Path(__file__).resolve().parents[3]
LOCAL = ROOT / ".workflow/dvl-local"


class IndependentImportantStore(unittest.TestCase):
    def setUp(self) -> None:
        LOCAL.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=LOCAL)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.private = self.root / "important"
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}

    def body(self, value: str) -> dict:
        return {"schemaVersion": 1, "facts": [value], "interpretations": [], "unknowns": []}

    def test_private_uuid_wal_full_incremental_and_readonly_reopen(self) -> None:
        with ImportantLogStore.create(self.private) as store:
            identity = store.db_uuid
            entry = store.make_entry(self.subject, "start", [], self.body("observed"))
            store.put_entry(entry)
            self.assertEqual(store._connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(store._connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(store._connection.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
            health = store.health_snapshot()
            self.assertEqual(health["dbUuid"], identity)
            self.assertEqual(health["totalBytes"], health["mainBytes"] + health["walBytes"] + health["shmBytes"])
            self.assertTrue(health["metadataOutsideBudget"])
        db = self.private / f"{identity}.sqlite3"
        self.assertEqual(self.private.stat().st_mode & 0o777, 0o700)
        self.assertEqual(db.stat().st_mode & 0o777, 0o600)
        with ImportantLogStore.open(self.private, identity, readonly=True) as reader:
            self.assertEqual(reader.get_entry(entry["entryId"]), entry)
            self.assertEqual(reader.list_entries(self.subject), [entry])
            with self.assertRaises(ImportantLogStoreError):
                reader.put_entry(entry)
        with ImportantLogStore.open(self.private, identity) as writer:
            self.assertEqual(writer.get_entry(entry["entryId"]), entry)

    def test_codec_threshold_savings_bound_checksum_and_corrupt_read(self) -> None:
        under = self.body("x" * 3900)
        self.assertLess(len(canonical_json(under)), 4096)
        self.assertEqual(encode_body(under).codec, "identity-v1")
        large = self.body("x" * 5000)
        encoded = encode_body(large)
        self.assertEqual(encoded.codec, "zlib-v1")
        self.assertEqual(decode_body(encoded.codec, encoded.payload, encoded.decoded_bytes, encoded.checksum), large)
        with self.assertRaises(ImportantLogStoreError):
            decode_body(encoded.codec, encoded.payload, encoded.decoded_bytes - 1, encoded.checksum)
        with self.assertRaises(ImportantLogStoreError):
            decode_body(encoded.codec, encoded.payload, encoded.decoded_bytes, "0" * 64)
        with self.assertRaises(ImportantLogStoreError):
            decode_body("unknown-v2", encoded.payload, encoded.decoded_bytes, encoded.checksum)
        # A payload saving under 12.5% must remain identity, even over 4 KiB.
        with patch("workbench.storage.important_log.store.zlib.compress", return_value=b"z" * 4500):
            self.assertEqual(encode_body(large).codec, "identity-v1")
        with ImportantLogStore.create(self.private) as store:
            entry = store.make_entry(self.subject, "event", [], large)
            store.put_entry(entry)
            store._connection.execute("UPDATE entries SET body_sha256=? WHERE entry_id=?", ("0" * 64, entry["entryId"]))
            with self.assertRaises(ImportantLogStoreError):
                store.get_entry(entry["entryId"])

    def test_unsafe_directory_file_alias_symlink_and_fifo_fail_closed(self) -> None:
        with ImportantLogStore.create(self.private) as store:
            identity = store.db_uuid
        db = self.private / f"{identity}.sqlite3"
        os.chmod(self.private, 0o755)
        with self.assertRaises(ImportantLogStoreError):
            ImportantLogStore.open(self.private, identity)
        os.chmod(self.private, 0o700)
        alias_dir = self.root / "alias"
        alias_dir.symlink_to(self.private, target_is_directory=True)
        with self.assertRaises(OSError):
            ImportantLogStore.open(alias_dir, identity)
        alias_file = self.private / f"{uuid4()}.sqlite3"
        os.link(db, alias_file)
        with self.assertRaises(ImportantLogStoreError):
            ImportantLogStore.open(self.private, identity)
        alias_file.unlink()
        os.chmod(db, 0o644)
        with self.assertRaises(ImportantLogStoreError):
            ImportantLogStore.open(self.private, identity)
        os.chmod(db, 0o600)
        fifo_id = str(uuid4())
        fifo = self.private / f"{fifo_id}.sqlite3"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(ImportantLogStoreError):
            ImportantLogStore.open(self.private, fifo_id)

    def test_failed_insert_rolls_back_entry_and_stats(self) -> None:
        with ImportantLogStore.create(self.private) as store:
            entry = store.make_entry(self.subject, "start", [], self.body("once"))
            store._connection.execute("""CREATE TRIGGER fail_count BEFORE INSERT ON subject_stats
                BEGIN SELECT RAISE(ABORT, 'injected'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                store.put_entry(entry)
            self.assertIsNone(store.get_entry(entry["entryId"]))
            self.assertEqual(store.health_snapshot()["subjectStats"], [])
            store._connection.execute("DROP TRIGGER fail_count")
            store.put_entry(entry)
            self.assertEqual(store.health_snapshot()["subjectStats"][0]["entryCount"], 1)

    def test_ordinary_put_rejects_foreign_origin_without_changing_counts(self) -> None:
        with ImportantLogStore.create(self.private) as store:
            entry = store.make_entry(self.subject, "event", [], self.body("source backed"))
            foreign = {**entry, "originDbUuid": str(uuid4())}
            with self.assertRaises(ImportantLogStoreError):
                store.put_entry(foreign)
            self.assertIsNone(store.get_entry(entry["entryId"]))
            self.assertEqual(store.health_snapshot()["subjectStats"], [])
            store.put_entry(entry)
            self.assertEqual(store.get_entry(entry["entryId"])["originDbUuid"], store.db_uuid)


if __name__ == "__main__":
    unittest.main()
