"""Requirement-derived retention checks against the public storage ports."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from workbench.storage.important_log import ImportantLogMaintenance, ImportantLogStore, SubjectLifecycle


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def body(value: str) -> dict:
    return {"schemaVersion": 1, "facts": [value], "interpretations": [], "unknowns": []}


class IndependentRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "important"

    def subject(self, kind: str = "task") -> dict:
        return {"schemaVersion": 1, "kind": kind, "id": str(uuid4())}

    def test_reactivity_resets_the_closed_clock_and_restores_open_protection(self):
        subject = self.subject()
        cutoff = NOW - timedelta(days=120)
        with ImportantLogStore.create(self.directory) as store:
            lifecycle = SubjectLifecycle(self.directory, store.db_uuid)
            original = store.make_entry(subject, "start", [], body("first"),
                                        recorded_at=stamp(cutoff - timedelta(days=1)))
            store.put_entry(original)
            lifecycle.archive_close(subject, at=stamp(cutoff - timedelta(seconds=1)),
                                    user_confirmed=True)
            lifecycle.record_delivery(subject, message_id=str(uuid4()), revision=1,
                                      kind="manager_instruction", content="confirmed again",
                                      delivered_at=stamp(NOW - timedelta(days=1)))
            maintenance = ImportantLogMaintenance(store)
            self.assertEqual(lifecycle.state(subject)["archival_state"], "open")
            self.assertEqual(maintenance.prune_pass(now=NOW, pressure=False)["deleted"], 0)
            self.assertIsNotNone(store.get_entry(original["entryId"]))

            lifecycle.archive_close(subject, at=stamp(cutoff + timedelta(seconds=1)),
                                    user_confirmed=True)
            self.assertEqual(maintenance.prune_pass(now=NOW, pressure=False)["deleted"], 0)
            self.assertIsNotNone(store.get_entry(original["entryId"]))
            at_boundary = NOW + timedelta(seconds=1)
            self.assertEqual(maintenance.prune_pass(now=at_boundary, pressure=False)["deleted"], 2)
            self.assertEqual(store.list_entries(subject), [])

    def test_pressure_orders_expired_then_recent_closed_then_open_intermediate(self):
        old, recent, ongoing = self.subject(), self.subject(), self.subject()
        with ImportantLogStore.create(self.directory) as store:
            lifecycle = SubjectLifecycle(self.directory, store.db_uuid)
            old_entry = store.make_entry(old, "start", [], body("expired"))
            recent_entry = store.make_entry(recent, "start", [], body("recent"))
            anchor = store.make_entry(ongoing, "start", [], body("anchor"))
            middle = store.make_entry(ongoing, "intermediate", [], body("replaceable"))
            latest = store.make_entry(ongoing, "cumulative", [], body("latest"))
            for entry in (old_entry, recent_entry, anchor, middle, latest):
                store.put_entry(entry)
            lifecycle.archive_close(old, at=stamp(NOW - timedelta(days=121)), user_confirmed=True)
            lifecycle.archive_close(recent, at=stamp(NOW - timedelta(days=3)), user_confirmed=True)
            maintenance = ImportantLogMaintenance(store)
            first = maintenance.prune_pass(now=NOW, pressure=True)
            self.assertEqual((first["mode"], first["deleted"]), ("expired", 1))
            self.assertIsNotNone(store.get_entry(recent_entry["entryId"]))
            self.assertIsNotNone(store.get_entry(middle["entryId"]))
            second = maintenance.prune_pass(now=NOW, pressure=True)
            self.assertEqual((second["mode"], second["deleted"]), ("closed_pressure", 1))
            self.assertIsNotNone(store.get_entry(middle["entryId"]))
            third = maintenance.prune_pass(now=NOW, pressure=True)
            self.assertEqual((third["mode"], third["deleted"]), ("open_intermediate", 1))
            self.assertIsNone(store.get_entry(middle["entryId"]))
            self.assertIsNotNone(store.get_entry(anchor["entryId"]))
            self.assertIsNotNone(store.get_entry(latest["entryId"]))
            self.assertEqual(len([entry for entry in store.list_entries(ongoing)
                                  if entry["kind"] == "gap"]), 1)

    def test_bounded_prune_continues_across_subjects_with_capped_candidate_count(self):
        older, newer = self.subject("command"), self.subject("command")
        with ImportantLogStore.create(self.directory) as store:
            lifecycle = SubjectLifecycle(self.directory, store.db_uuid)
            first = store.make_entry(older, "event", [], body("oldest"))
            store.put_entry(first)
            for index in range(120):
                store.put_entry(store.make_entry(newer, "event", [], body(str(index))))
            lifecycle.archive_close(older, at=stamp(NOW - timedelta(days=125)),
                                    user_confirmed=True)
            lifecycle.archive_close(newer, at=stamp(NOW - timedelta(days=124)),
                                    user_confirmed=True)
            maintenance = ImportantLogMaintenance(store)
            self.assertEqual(maintenance.candidate_count(now=NOW, pressure=False), 101)
            passes = [maintenance.prune_pass(now=NOW, pressure=False) for _ in range(3)]
            self.assertEqual([item["deleted"] for item in passes], [1, 100, 20])
            self.assertEqual([item["remaining"] for item in passes], [True, True, False])
            self.assertIsNone(store.get_entry(first["entryId"]))
            self.assertEqual(store.list_entries(newer), [])
            self.assertEqual(maintenance.candidate_count(now=NOW, pressure=False), 0)


if __name__ == "__main__":
    unittest.main()
