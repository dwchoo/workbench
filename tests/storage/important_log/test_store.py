from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from workbench.storage.important_log import ImportantLogStore, ImportantLogStoreError, decode_body, encode_body


class ImportantLogStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "important"

    def tearDown(self):
        self.temp.cleanup()

    def test_codec_bound_checksum_and_private_reopen(self):
        body = {"schemaVersion": 1, "facts": ["x" * 5000], "interpretations": [], "unknowns": []}
        encoded = encode_body(body)
        self.assertEqual(encoded.codec, "zlib-v1")
        self.assertEqual(decode_body(encoded.codec, encoded.payload, encoded.decoded_bytes, encoded.checksum), body)
        with self.assertRaises(ImportantLogStoreError):
            decode_body(encoded.codec, encoded.payload, 100, encoded.checksum)
        with self.assertRaises(ImportantLogStoreError):
            decode_body(encoded.codec, encoded.payload, encoded.decoded_bytes, "0" * 64)
        with self.assertRaises(ImportantLogStoreError):
            decode_body("bad", encoded.payload, encoded.decoded_bytes, encoded.checksum)

        with ImportantLogStore.create(self.root) as store:
            db_uuid = store.db_uuid
            subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
            source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                      "commandId": subject["id"]}
            span = {"schemaVersion": 1, "source": source, "start": 0, "end": 3,
                    "digest": sha256(b"abc").hexdigest()}
            entry = store.make_entry(subject, "start", [span], body)
            store.put_entry(entry)
            self.assertEqual(store.get_entry(entry["entryId"]), entry)
            self.assertEqual(store.list_entries(subject), [entry])
            self.assertEqual(store.health_snapshot()["subjectStats"][0]["entryCount"], 1)
            self.assertEqual(store._connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(store._connection.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / f"{db_uuid}.sqlite3").stat().st_mode & 0o777, 0o600)
        with ImportantLogStore.open(self.root, db_uuid) as writer:
            self.assertEqual(writer.get_entry(entry["entryId"]), entry)
        with ImportantLogStore.open(self.root, db_uuid, readonly=True) as reader:
            self.assertEqual(reader.get_entry(entry["entryId"]), entry)
            with self.assertRaises(ImportantLogStoreError):
                reader.put_entry(entry)

    def test_transaction_rollback_and_unsafe_paths(self):
        with ImportantLogStore.create(self.root) as store:
            subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
            entry = store.make_entry(subject, "event", [],
                                     {"schemaVersion": 1, "facts": ["one"],
                                      "interpretations": [], "unknowns": []})
            store._connection.execute("""CREATE TRIGGER fail_stats BEFORE INSERT ON subject_stats
                BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                store.put_entry(entry)
            self.assertIsNone(store.get_entry(entry["entryId"]))
            store._connection.execute("DROP TRIGGER fail_stats")
            store.put_entry(entry)
            self.assertEqual(len(store.list_entries(subject)), 1)
            db_uuid = store.db_uuid
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            ImportantLogStore.open(alias, db_uuid)
        other = self.root / f"{uuid4()}.sqlite3"
        os.mkfifo(other, 0o600)
        with self.assertRaises(ImportantLogStoreError):
            ImportantLogStore.open(self.root, other.stem)
        os.unlink(other)
        alias_file = self.root / f"{uuid4()}.sqlite3"
        os.link(self.root / f"{db_uuid}.sqlite3", alias_file)
        with self.assertRaises(ImportantLogStoreError):
            ImportantLogStore.open(self.root, db_uuid)

    def test_outer_event_transaction_rolls_back_entry_and_stats(self):
        with ImportantLogStore.create(self.root) as store:
            subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
            entry = store.make_entry(subject, "event", [],
                                     {"schemaVersion": 1, "facts": ["confirmed"],
                                      "interpretations": [], "unknowns": []})
            with self.assertRaisesRegex(RuntimeError, "after entry"):
                with store.transaction():
                    store.put_entry(entry)
                    raise RuntimeError("after entry")
            self.assertIsNone(store.get_entry(entry["entryId"]))
            self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM subject_stats").fetchone()[0], 0)
            with store.transaction():
                store.put_entry(entry)
            self.assertEqual(store.get_entry(entry["entryId"]), entry)

    def test_corrupt_blob_and_unsafe_sidecar_fail_closed(self):
        with ImportantLogStore.create(self.root) as store:
            subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
            entry = store.make_entry(subject, "event", [],
                                     {"schemaVersion": 1, "facts": ["one"],
                                      "interpretations": [], "unknowns": []})
            store.put_entry(entry)
            store._connection.execute("UPDATE entries SET body_payload=zeroblob(2000000) WHERE entry_id=?",
                                      (entry["entryId"],))
            with self.assertRaises(ImportantLogStoreError):
                store.get_entry(entry["entryId"])
            db_uuid = store.db_uuid
        target = Path(self.temp.name) / "target"
        target.write_bytes(b"not a WAL")
        (self.root / f"{db_uuid}.sqlite3-wal").symlink_to(target)
        with self.assertRaises(OSError):
            ImportantLogStore.open(self.root, db_uuid)

    def test_normal_put_rejects_foreign_origin_uuid(self):
        with ImportantLogStore.create(self.root) as first:
            subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
            body = {"schemaVersion": 1, "facts": ["fact"], "interpretations": [], "unknowns": []}
            entry = first.make_entry(subject, "event", [], body)
            with ImportantLogStore.create(Path(self.temp.name) / "second") as second:
                with self.assertRaises(ImportantLogStoreError):
                    second.put_entry(entry)
                self.assertIsNone(second.get_entry(entry["entryId"]))
            first.put_entry(entry)
            self.assertEqual(first.get_entry(entry["entryId"]), entry)
