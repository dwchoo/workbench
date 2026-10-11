from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from workbench.storage.important_log import ImportantLogMaintenance, ImportantLogStore, SubjectLifecycle


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def body(text: str) -> dict:
    return {"schemaVersion": 1, "facts": [text], "interpretations": [], "unknowns": []}


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "important"
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}

    def test_expired_closed_only_and_120_day_boundary(self):
        with ImportantLogStore.create(self.directory) as store:
            lifecycle = SubjectLifecycle(self.directory, store.db_uuid)
            first = store.make_entry(self.subject, "start", [], body("closed"))
            store.put_entry(first)
            lifecycle.archive_close(self.subject, at="2026-06-11T00:00:00Z", user_confirmed=True)
            maintenance = ImportantLogMaintenance(store)
            result = maintenance.prune_pass(now=NOW, pressure=False)
            self.assertEqual(result["mode"], "expired")
            self.assertEqual(result["deleted"], 1)
            self.assertIsNone(store.get_entry(first["entryId"]))
            opened = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
            second = store.make_entry(opened, "start", [], body("open"))
            store.put_entry(second)
            self.assertEqual(maintenance.prune_pass(now=NOW, pressure=True)["deleted"], 0)
            self.assertEqual(store.get_entry(second["entryId"]), second)

    def test_pressure_evicts_intermediate_but_protects_start_and_cumulative_and_marks_gap(self):
        with ImportantLogStore.create(self.directory) as store:
            SubjectLifecycle(self.directory, store.db_uuid)
            start = store.make_entry(self.subject, "start", [], body("start"))
            source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                      "commandId": self.subject["id"]}
            span = {"schemaVersion": 1, "source": source, "start": 0, "end": 3,
                    "digest": sha256(b"abc").hexdigest()}
            middle = store.make_entry(self.subject, "intermediate", [span], body("middle"))
            latest = store.make_entry(self.subject, "cumulative", [], body("latest"))
            for entry in (start, middle, latest):
                store.put_entry(entry)
            maintenance = ImportantLogMaintenance(store)
            result = maintenance.prune_pass(now=NOW, pressure=True)
            self.assertEqual((result["mode"], result["deleted"]), ("open_intermediate", 1))
            self.assertIsNone(store.get_entry(middle["entryId"]))
            self.assertEqual(store.get_entry(start["entryId"]), start)
            self.assertEqual(store.get_entry(latest["entryId"]), latest)
            gaps = [item for item in store.list_entries(self.subject) if item["kind"] == "gap"]
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["sourceGaps"], [{"schemaVersion": 1, "source": source,
                "start": 0, "end": 3, "reason": "evicted"}])

    def test_quick_check_cached_and_bounded_maintenance(self):
        with ImportantLogStore.create(self.directory) as store:
            maintenance = ImportantLogMaintenance(store)
            self.assertEqual(maintenance.quick_check(now=NOW)["status"], "ok")
            self.assertEqual(maintenance.quick_check(now=NOW + timedelta(hours=23))["status"], "cached")
            self.assertEqual(maintenance.quick_check(now=NOW + timedelta(hours=24))["status"], "ok")
            result = maintenance.maintenance_pass(now=NOW)
            self.assertEqual(result["prune"]["deleted"], 0)
            self.assertEqual(result["shrink"]["maxPages"], 256)

    def test_one_prune_transaction_never_exceeds_100_rows(self):
        with ImportantLogStore.create(self.directory) as store:
            lifecycle = SubjectLifecycle(self.directory, store.db_uuid)
            for number in range(101):
                store.put_entry(store.make_entry(self.subject, "event", [], body(f"row {number}")))
            lifecycle.archive_close(self.subject, at="2026-06-10T00:00:00Z", user_confirmed=True)
            maintenance = ImportantLogMaintenance(store)
            first = maintenance.prune_pass(now=NOW, pressure=False)
            self.assertEqual(first["deleted"], 100)
            self.assertTrue(first["remaining"])
            self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)
            self.assertEqual(maintenance.prune_pass(now=NOW, pressure=False)["deleted"], 1)

    def test_oldest_archived_subject_first_and_candidate_count_spans_subjects(self):
        newer = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        with ImportantLogStore.create(self.directory) as store:
            lifecycle = SubjectLifecycle(self.directory, store.db_uuid)
            older_entries = [store.make_entry(self.subject, "event", [], body(str(index)))
                             for index in range(2)]
            newer_entry = store.make_entry(newer, "event", [], body("newer"))
            for entry in (*older_entries, newer_entry):
                store.put_entry(entry)
            lifecycle.archive_close(self.subject, at="2026-06-01T00:00:00Z", user_confirmed=True)
            lifecycle.archive_close(newer, at="2026-06-02T00:00:00Z", user_confirmed=True)
            maintenance = ImportantLogMaintenance(store)
            self.assertEqual(maintenance.candidate_count(now=NOW, pressure=False), 3)
            first = maintenance.prune_pass(now=NOW, pressure=False)
            self.assertEqual((first["mode"], first["deleted"], first["remaining"]),
                             ("expired", 2, True))
            self.assertIsNotNone(store.get_entry(newer_entry["entryId"]))
            self.assertEqual(maintenance.candidate_count(now=NOW, pressure=False), 1)
            self.assertEqual(maintenance.prune_pass(now=NOW, pressure=False)["deleted"], 1)


if __name__ == "__main__":
    unittest.main()
