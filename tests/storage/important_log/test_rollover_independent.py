"""Durable rollover fault boundaries and archive preservation."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import errno
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.storage.important_log import (ImportantLogCatalog, ImportantLogStore,
                                            ImportantLogStoreError)
import workbench.storage.important_log.catalog as catalog_module


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
BODY = {"schemaVersion": 1, "facts": ["durable"], "interpretations": [], "unknowns": []}


class IndependentRolloverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "important"
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}

    def test_failure_before_intent_keeps_old_active_and_exposes_new_orphan(self):
        catalog = ImportantLogCatalog(self.directory, target_bytes=36_000,
                                      low_water_bytes=30_000, create=True)
        old = catalog.active_uuid()
        write_control = catalog_module._write_control

        def fail_intent(fd, name, value):
            if name == "rollover.intent.json":
                raise OSError(errno.EIO, "fault before durable intent")
            return write_control(fd, name, value)

        with patch.object(catalog_module, "_write_control", side_effect=fail_intent):
            result = catalog.append(self.subject, "start", [], BODY, now=NOW)
        self.assertEqual(result["state"], "io_error")
        self.assertIsNotNone(result["durableEntryId"])
        self.assertTrue(result["rolloverStateUnknown"])
        self.assertFalse((self.directory / "rollover.intent.json").exists())
        recovered = ImportantLogCatalog(self.directory).catalog_snapshot()
        self.assertEqual(recovered["active"], old)
        self.assertEqual(recovered["generation"], 1)
        self.assertEqual(recovered["archives"], [])
        self.assertEqual(len(recovered["orphans"]), 1)
        self.assertEqual(recovered["databaseCount"], 2)
        self.assertTrue((self.directory / f"{recovered['orphans'][0]}.sqlite3").exists())
        with ImportantLogStore.open(self.directory, old, readonly=True) as retained:
            self.assertIsNotNone(retained.get_entry(result["durableEntryId"]))

    def test_pending_durable_intent_blocks_old_direct_writer_until_catalog_recovery(self):
        catalog = ImportantLogCatalog(self.directory, target_bytes=36_000,
                                      low_water_bytes=30_000, create=True)
        old = catalog.active_uuid()
        pointer = self.directory / "active.json"
        before_pointer = pointer.read_bytes()
        write_control = catalog_module._write_control

        def fail_publish(fd, name, value):
            if name == "active.json" and value["generation"] == 2:
                raise OSError(errno.EIO, "fault before active pointer publish")
            return write_control(fd, name, value)

        with patch.object(catalog_module, "_write_control", side_effect=fail_publish):
            outcome = catalog.append(self.subject, "start", [], BODY, now=NOW)
        self.assertEqual(outcome["state"], "io_error")
        self.assertIsNotNone(outcome["durableEntryId"])
        self.assertEqual(pointer.read_bytes(), before_pointer)
        self.assertTrue((self.directory / "rollover.intent.json").is_file())
        with ImportantLogStore.open(self.directory, old, readonly=True) as retained:
            entries_before = retained._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            stats_before = retained._connection.execute("SELECT COUNT(*) FROM subject_stats").fetchone()[0]

        rejected = False
        try:
            with ImportantLogStore.open(self.directory, old) as direct:
                direct.put_entry(direct.make_entry(self.subject, "event", [], BODY))
        except ImportantLogStoreError:
            rejected = True
        recovered = ImportantLogCatalog(self.directory)
        current = recovered.active_uuid()
        self.assertNotEqual(current, old)
        self.assertFalse((self.directory / "rollover.intent.json").exists())
        self.assertIn(old, recovered.catalog_snapshot()["archives"])
        later = recovered.append(self.subject, "event", [], BODY, now=NOW)
        self.assertEqual(later["state"], "ok")
        self.assertEqual(recovered.active_uuid(), current)
        with ImportantLogStore.open(self.directory, current, readonly=True) as retained:
            self.assertIsNotNone(retained.get_entry(later["entry"]["entryId"]))
        with ImportantLogStore.open(self.directory, old, readonly=True) as retained:
            self.assertEqual(retained._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
                             entries_before)
            self.assertEqual(retained._connection.execute("SELECT COUNT(*) FROM subject_stats").fetchone()[0],
                             stats_before)
        self.assertTrue(rejected, "pending durable intent must block a direct writer")

    def test_successive_rollovers_leave_original_anchor_in_its_source_until_explicit_carry(self):
        catalog = ImportantLogCatalog(self.directory, target_bytes=36_000,
                                      low_water_bytes=30_000, create=True)
        first = catalog.append(self.subject, "start", [], BODY, now=NOW)
        self.assertEqual(first["state"], "ok")
        self.assertIsNotNone(first["rollover"])
        original = first["rollover"]["from"]
        second = catalog.append(self.subject, "event", [], BODY, now=NOW)
        self.assertEqual(second["state"], "ok")
        self.assertIsNotNone(second["rollover"])
        self.assertEqual(catalog.catalog_snapshot()["databaseCount"], 3)
        self.assertEqual(first["rollover"]["to"], second["rollover"]["from"])
        entry_id = first["entry"]["entryId"]
        with ImportantLogStore.open(self.directory, original, readonly=True) as source:
            self.assertIsNotNone(source.get_entry(entry_id))
        for identity in (second["rollover"]["from"], second["rollover"]["to"]):
            with ImportantLogStore.open(self.directory, identity, readonly=True) as later:
                self.assertIsNone(later.get_entry(entry_id))

    def test_active_maintenance_after_120_days_never_changes_archive_files(self):
        catalog = ImportantLogCatalog(self.directory, target_bytes=36_000,
                                      low_water_bytes=30_000, create=True)
        result = catalog.append(self.subject, "start", [], BODY, now=NOW)
        self.assertIsNotNone(result["rollover"])
        archived = result["rollover"]["from"]

        def archive_hashes():
            return {path.name: sha256(path.read_bytes()).hexdigest()
                    for path in self.directory.glob(f"{archived}.sqlite3*")}

        before = archive_hashes()
        self.assertIn(f"{archived}.sqlite3", before)
        catalog.maintenance_step(now=NOW + timedelta(days=121))
        self.assertEqual(archive_hashes(), before)
        self.assertIn(archived, catalog.catalog_snapshot()["archives"])

    def test_two_catalog_instances_serialize_concurrent_writes_and_rollovers(self):
        catalogs = [ImportantLogCatalog(self.directory, target_bytes=36_000,
                                        low_water_bytes=30_000, create=True),
                    ImportantLogCatalog(self.directory, target_bytes=36_000,
                                        low_water_bytes=30_000)]
        subjects = [{"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
                    for _ in catalogs]
        barrier = threading.Barrier(2)
        outcomes = [None, None]

        def write(index):
            barrier.wait(timeout=2)
            outcomes[index] = catalogs[index].append(subjects[index], "start", [], BODY, now=NOW)

        threads = [threading.Thread(target=write, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        for index, outcome in enumerate(outcomes):
            self.assertIsNotNone(outcome)
            if outcome["state"] == "busy":
                outcomes[index] = catalogs[index].append(subjects[index], "start", [], BODY, now=NOW)
        self.assertEqual([outcome["state"] for outcome in outcomes], ["ok", "ok"])
        self.assertTrue(all(outcome["rollover"] is not None for outcome in outcomes))
        snapshot = ImportantLogCatalog(self.directory).catalog_snapshot()
        self.assertEqual(snapshot["generation"], 3)
        self.assertEqual(snapshot["databaseCount"], 3)
        self.assertEqual(snapshot["orphans"], [])
        identities = [*snapshot["archives"], snapshot["active"]]
        for outcome in outcomes:
            matches = 0
            for identity in identities:
                with ImportantLogStore.open(self.directory, identity, readonly=True) as store:
                    matches += store.get_entry(outcome["entry"]["entryId"]) is not None
            self.assertEqual(matches, 1)

    def test_duplicate_control_identity_or_generation_fails_closed(self):
        catalog = ImportantLogCatalog(self.directory, create=True)
        pointer = self.directory / "active.json"
        original = pointer.read_text(encoding="utf-8")
        fields = json.loads(original)
        identity = fields["active"]
        generation = fields["generation"]
        active_field = f'"active":"{identity}"'
        generation_field = f'"generation":{generation}'
        self.assertIn(active_field, original)
        self.assertIn(generation_field, original)
        duplicates = {
            "active": original.replace(active_field,
                f'"active":"{uuid4()}",{active_field}', 1),
            "generation": original.replace(generation_field,
                f'"generation":{generation + 1},{generation_field}', 1),
        }
        for name, invalid in duplicates.items():
            with self.subTest(field=name):
                pointer.write_text(invalid, encoding="utf-8")
                try:
                    with self.assertRaises(ImportantLogStoreError):
                        catalog.active_uuid()
                finally:
                    pointer.write_text(original, encoding="utf-8")
        self.assertEqual(catalog.active_uuid(), identity)

    def test_duplicate_rollover_intent_member_does_not_publish_or_delete(self):
        catalog = ImportantLogCatalog(self.directory, create=True)
        old = catalog.active_uuid()
        pointer = self.directory / "active.json"
        before = pointer.read_bytes()
        with ImportantLogStore.create(self.directory) as target:
            proposed = target.db_uuid
        intent = {"version": 1, "from": old, "to": proposed,
                  "generation": 2, "checkpoint": "complete"}
        raw = json.dumps(intent, separators=(",", ":"))
        raw = raw.replace('"generation":2', '"generation":3,"generation":2')
        marker = self.directory / "rollover.intent.json"
        marker.write_text(raw, encoding="utf-8")
        marker.chmod(0o600)
        with self.assertRaises(ImportantLogStoreError):
            catalog.active_uuid()
        self.assertEqual(pointer.read_bytes(), before)
        self.assertTrue(marker.exists())
        self.assertTrue((self.directory / f"{old}.sqlite3").exists())
        self.assertTrue((self.directory / f"{proposed}.sqlite3").exists())

    def test_direct_writer_refuses_ambiguous_or_invalid_active_pointer_without_effects(self):
        cases = ("duplicate_active", "duplicate_generation", "malformed_json",
                 "wrong_active", "missing_generation", "wrong_version",
                 "boolean_generation", "noncanonical_active", "malformed_archive",
                 "archive_overlap", "unknown_archive_check", "nested_duplicate")
        for name in cases:
            with self.subTest(pointer=name):
                directory = Path(self.temp.name) / name
                catalog = ImportantLogCatalog(directory, create=True)
                identity = catalog.active_uuid()
                pointer = directory / "active.json"
                original = pointer.read_text(encoding="utf-8")
                fields = json.loads(original)
                active_field = f'"active":"{identity}"'
                generation_field = f'"generation":{fields["generation"]}'
                archive = str(uuid4())
                invalid = {
                    "duplicate_active": original.replace(active_field,
                        f'"active":"{uuid4()}",{active_field}', 1),
                    "duplicate_generation": original.replace(generation_field,
                        f'"generation":2,{generation_field}', 1),
                    "malformed_json": "{invalid json",
                    "wrong_active": original.replace(active_field,
                        f'"active":"{uuid4()}"', 1),
                    "missing_generation": original.replace(generation_field + ",", "", 1),
                    "wrong_version": json.dumps({**fields, "version": 2}),
                    "boolean_generation": json.dumps({**fields, "generation": True}),
                    "noncanonical_active": json.dumps({**fields, "active": identity.replace("-", "")}),
                    "malformed_archive": json.dumps({**fields, "archives": ["not-a-uuid"]}),
                    "archive_overlap": json.dumps({**fields, "archives": [identity]}),
                    "unknown_archive_check": json.dumps({**fields,
                        "archiveChecks": {archive: "complete"}}),
                    "nested_duplicate": (f'{{"version":1,"generation":1,"active":"{identity}",'
                                         f'"archives":["{archive}"],"archiveChecks":{{'
                                         f'"{archive}":"complete","{archive}":"busy"}}}}'),
                }[name]
                pointer.write_text(invalid, encoding="utf-8")
                wrote = False
                try:
                    with self.assertRaises(ImportantLogStoreError):
                        catalog.active_uuid()
                    try:
                        with ImportantLogStore.open(directory, identity) as direct:
                            entry = direct.make_entry(self.subject, "start", [], BODY)
                            direct.put_entry(entry)
                            wrote = True
                    except ImportantLogStoreError:
                        pass
                finally:
                    control_after = pointer.read_text(encoding="utf-8")
                    pointer.write_text(original, encoding="utf-8")
                self.assertEqual(control_after, invalid)
                self.assertFalse(wrote)
                with ImportantLogStore.open(directory, identity, readonly=True) as retained:
                    self.assertEqual(retained._connection.execute(
                        "SELECT COUNT(*) FROM entries").fetchone()[0], 0)
                    self.assertEqual(retained._connection.execute(
                        "SELECT COUNT(*) FROM subject_stats").fetchone()[0], 0)
                self.assertEqual(catalog.catalog_snapshot()["databaseCount"], 1)

    def test_missing_catalog_pointer_refuses_direct_writer_but_standalone_writer_works(self):
        catalog = ImportantLogCatalog(self.directory, create=True)
        identity = catalog.active_uuid()
        pointer = self.directory / "active.json"
        original = pointer.read_bytes()
        self.assertTrue((self.directory / "maintenance.lock").is_file())
        pointer.unlink()
        try:
            with self.assertRaises(ImportantLogStoreError):
                catalog.active_uuid()
            with self.assertRaises(ImportantLogStoreError):
                ImportantLogStore.open(self.directory, identity)
        finally:
            pointer.write_bytes(original)
            pointer.chmod(0o600)
        with ImportantLogStore.open(self.directory, identity, readonly=True) as retained:
            self.assertEqual(retained.list_entries(self.subject), [])

        standalone = Path(self.temp.name) / "pre-catalog"
        with ImportantLogStore.create(standalone) as store:
            standalone_identity = store.db_uuid
        with ImportantLogStore.open(standalone, standalone_identity) as direct:
            direct.put_entry(direct.make_entry(self.subject, "start", [], BODY))
        with ImportantLogStore.open(standalone, standalone_identity, readonly=True) as retained:
            self.assertEqual(len(retained.list_entries(self.subject)), 1)


if __name__ == "__main__":
    unittest.main()
