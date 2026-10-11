"""DVL-07 public read and user cleanup behavior on private synthetic catalogs."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.backend import cli
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.service import Backend
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog, ImportantLogStore
from workbench.storage.important_log.maintenance import ImportantLogMaintenance
import workbench.storage.important_log.lifecycle as lifecycle_module


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
BODY = {"schemaVersion": 1, "facts": ["confirmed source"], "interpretations": [], "unknowns": []}


class Dvl07PublicRoutes(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="dvl07-route-", dir="/dev/shm")
        self.addCleanup(temporary.cleanup)
        self.root = ensure_private_dir(Path(temporary.name) / "d")
        self.layout = DataLayout(self.root)
        self.directory = self.layout.important_logs
        self.task_id = str(uuid4())
        self.subject = {"schemaVersion": 1, "kind": "task", "id": self.task_id}

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(["logs", "important", *args, "--data-dir", str(self.root)])
        return result, stdout.getvalue(), stderr.getvalue()

    def catalog_files(self) -> dict[str, str]:
        if not self.directory.exists():
            return {}
        return {p.name: sha256(p.read_bytes()).hexdigest() for p in self.directory.iterdir()
                if p.is_file() and p.name.endswith((".sqlite3", ".sqlite3-wal"))}

    def test_stopped_cli_readonly_routes_do_not_allocate_or_start_maintenance(self) -> None:
        before = sorted(self.root.iterdir())
        for action in ("status", "list"):
            code, _out, err = self.run_cli(action, "--json")
            self.assertNotEqual(code, 0)
            self.assertTrue(err or _out)
        self.assertEqual(sorted(self.root.iterdir()), before)

        catalog = ImportantLogCatalog(self.directory, create=True)
        saved = catalog.append(self.subject, "start", [], BODY, now=NOW)
        self.assertEqual(saved["state"], "ok")
        source_before = self.catalog_files()
        db_before = tuple(sorted(self.directory.glob("*.sqlite3")))
        # An important read may cooperate through active SHM, but it cannot
        # run storage health/quick_check or allocate another DB.
        with patch.object(ImportantLogMaintenance, "quick_check", side_effect=AssertionError("maintenance")), \
             patch.object(ImportantLogStore, "health_snapshot", side_effect=AssertionError("health")):
            status_code, status_text, _ = self.run_cli("status", "--json")
            list_code, list_text, _ = self.run_cli("list", "--json")
            show_code, show_text, _ = self.run_cli("show", "--task", self.task_id, "--json")
        self.assertEqual((status_code, list_code, show_code), (0, 0, 0))
        status, listed, shown = map(json.loads, (status_text, list_text, show_text))
        self.assertEqual(status["databaseCount"], 1)
        self.assertEqual(status["databases"][0]["availability"], "unknown")
        self.assertTrue(listed["complete"])
        self.assertEqual(listed["subjects"][0]["sourceDbUuids"], [catalog.active_uuid()])
        self.assertEqual(shown["entries"][0]["entry"]["entryId"], saved["entry"]["entryId"])
        self.assertEqual(shown["entries"][0]["sourceDbUuids"], [catalog.active_uuid()])
        self.assertEqual(tuple(sorted(self.directory.glob("*.sqlite3"))), db_before)
        after = self.catalog_files()
        for name, digest in source_before.items():
            self.assertEqual(after[name], digest)
        self.assertTrue(set(after) - set(source_before) <=
                        {catalog.active_uuid() + ".sqlite3-wal"})

    def test_exact_archive_delete_carries_open_lifecycle_and_event_replay_key(self) -> None:
        catalog = ImportantLogCatalog(self.directory, create=True, target_bytes=36_000,
                                      low_water_bytes=30_000)
        log = ActiveSubjectLog(catalog)
        delegated = {"seq": 1, "at": "2026-10-10T01:00:00Z", "type": "delegated",
                     "task_id": self.task_id, "revision": 1,
                     "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())}
        self.assertTrue(log.record_flow(delegated))
        archived = catalog.catalog_snapshot()["archives"]
        self.assertEqual(len(archived), 1, "the fixture must exercise an actual rollover")
        original, active = archived[0], catalog.active_uuid()
        before = self.catalog_files()
        refused, _, refusal = self.run_cli("delete-db", "--db", active, "--yes", "--json")
        self.assertNotEqual(refused, 0)
        self.assertIn("거부", refusal)
        self.assertEqual(self.catalog_files(), before)
        for alias in (original.upper(), "../" + original, str(self.directory / (original + ".sqlite3"))):
            with self.subTest(alias=alias):
                code, _, _ = self.run_cli("delete-db", "--db", alias, "--yes", "--json")
                self.assertNotEqual(code, 0)
                self.assertIn(original, catalog.catalog_snapshot()["archives"])
        code, output, error = self.run_cli("delete-db", "--db", original, "--yes", "--json")
        self.assertEqual(code, 0, error + output)
        result = json.loads(output)
        self.assertEqual((result["state"], result["databaseCount"], result["remainingDbUuids"]),
                         ("deleted", 1, [active]))
        self.assertFalse((self.directory / (original + ".sqlite3")).exists())
        self.assertEqual(catalog.catalog_snapshot()["archives"], [])
        with ImportantLogStore.open(self.directory, active, readonly=True) as store:
            state = store._connection.execute("SELECT archival_state,event_cursor FROM subject_lifecycle_v1 "
                                              "WHERE subject_kind='task' AND subject_id=?", (self.task_id,)).fetchone()
            event = store._connection.execute("SELECT event_key FROM subject_events_v1").fetchall()
            self.assertEqual(tuple(state), ("open", 1))
            self.assertEqual([row[0] for row in event], ["flow:1"])
        # The confirmed source may be replayed after restart, and its key must
        # suppress a duplicate even though the original DB has gone.
        self.assertFalse(ActiveSubjectLog(ImportantLogCatalog(self.directory)).record_flow(delegated))

    def test_stopped_cli_archival_close_keeps_core_state_and_reactivity(self) -> None:
        catalog = ImportantLogCatalog(self.directory, create=True, target_bytes=36_000,
                                      low_water_bytes=30_000)
        log = ActiveSubjectLog(catalog)
        delegated = {"seq": 1, "at": "2026-10-10T01:00:00Z", "type": "delegated",
                     "task_id": self.task_id, "revision": 1,
                     "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())}
        log.record_flow(delegated)
        self.assertEqual(len(catalog.catalog_snapshot()["archives"]), 1)
        code, output, error = self.run_cli("archival-close", "--task", self.task_id, "--yes", "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["state"], "closed")
        state = ActiveSubjectLog(ImportantLogCatalog(self.directory)).state(self.subject)
        self.assertEqual(state["archival_state"], "closed")
        self.assertIsNone(state["core_closed_reason"])
        self.assertEqual(state["core_status"], "dispatched")
        self.assertTrue(log.record_delivery(self.subject, message_id=str(uuid4()), revision=1,
                                            kind="manager_instruction", content="confirmed follow-up",
                                            delivered_at="2026-10-10T02:00:00Z"))
        state = ActiveSubjectLog(ImportantLogCatalog(self.directory)).state(self.subject)
        self.assertEqual((state["archival_state"], state["core_status"]), ("open", "dispatched"))

    def test_schema_failure_during_explicit_carry_keeps_selected_archive_and_active_identity(self) -> None:
        catalog = ImportantLogCatalog(self.directory, create=True, target_bytes=36_000,
                                      low_water_bytes=30_000)
        log = ActiveSubjectLog(catalog)
        event = {"seq": 1, "at": "2026-10-10T01:00:00Z", "type": "delegated",
                 "task_id": self.task_id, "revision": 1,
                 "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())}
        self.assertTrue(log.record_flow(event))
        before = catalog.catalog_snapshot()
        archive, active = before["archives"][0], before["active"]

        def active_has_lifecycle() -> bool:
            with ImportantLogStore.open(self.directory, active, readonly=True) as store:
                return store._connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                                 "AND name='subject_lifecycle_v1'").fetchone() is not None

        self.assertFalse(active_has_lifecycle())
        for action in (("status",), ("list",), ("show", "--task", self.task_id)):
            code, _, error = self.run_cli(*action, "--json")
            self.assertEqual(code, 0, error)
        self.assertFalse(active_has_lifecycle(), "read-only routes must not repair schema")
        # Malformed DDL makes the real SQLite schema initializer fail during
        # explicit carry; deletion authority must remain unissued.
        with patch.object(lifecycle_module, "_SCHEMA", "CREATE TABLE broken ("):
            with self.assertRaises(sqlite3.DatabaseError):
                ImportantLogCatalog(self.directory).prepare_and_delete_archive(archive)
        self.assertFalse((self.directory / "delete.intent.json").exists())
        after = ImportantLogCatalog(self.directory).catalog_snapshot()
        self.assertEqual((after["active"], after["archives"], after["databaseCount"]),
                         (active, [archive], 2))
        self.assertTrue((self.directory / (archive + ".sqlite3")).exists())

    def test_agent_tool_reads_catalog_but_rejects_mutation_and_excessive_pages(self) -> None:
        catalog = ImportantLogCatalog(self.directory, create=True)
        saved = catalog.append(self.subject, "start", [], BODY, now=NOW)
        self.assertEqual(saved["state"], "ok")
        backend = Backend.__new__(Backend)
        backend.layout = self.layout
        peer = SimpleNamespace(role="worker")
        base = {"request_id": str(uuid4()), "tool_call_id": "call-1",
                "session_id": str(uuid4()), "generation": 1, "tool": "workbench_logs"}

        def invoke(action: str, *, kind=None, identity=None, limit=None, page=None, **extra):
            args = {"action": action, "subject_kind": kind, "subject_id": identity,
                    "limit": limit, "page": page, **extra}
            return backend._tool_request(peer, {**base, "args": args})

        source_before = self.catalog_files()
        for action in ("status", "list"):
            answer = invoke(action)
            self.assertEqual(answer["status"], "ok")
        shown = invoke("show", kind="task", identity=self.task_id, limit=1)
        self.assertEqual(shown["status"], "ok")
        self.assertEqual(shown["data"]["entries"][0]["entry"]["entryId"], saved["entry"]["entryId"])
        for action in ("delete-db", "archival-close", "maintenance", "activate", "summary"):
            with self.subTest(action=action):
                self.assertEqual(invoke(action, kind="task", identity=self.task_id)["status"], "rejected")
        for limit in (0, 11, True, "10"):
            with self.subTest(limit=limit):
                self.assertEqual(invoke("list", limit=limit)["status"], "rejected")
        self.assertEqual(invoke("show", kind="task", identity=self.task_id,
                                unexpected="delete")["status"], "rejected")
        self.assertEqual(self.catalog_files()[catalog.active_uuid() + ".sqlite3"],
                         source_before[catalog.active_uuid() + ".sqlite3"])
        self.assertEqual(len(list(self.directory.glob("*.sqlite3"))), 1)

    def _closed_archive_with_anchors(self):
        catalog = ImportantLogCatalog(self.directory, create=True, target_bytes=1_000_000,
                                      low_water_bytes=900_000)
        grant = {"seq": 1, "at": "2026-10-10T01:00:00Z", "type": "delegated",
                 "task_id": self.task_id, "revision": 1,
                 "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())}
        self.assertTrue(ActiveSubjectLog(catalog).record_flow(grant))
        first = catalog.append(self.subject, "start", [], BODY, now=NOW)
        latest = catalog.append(self.subject, "cumulative", [],
                                {**BODY, "facts": ["latest cumulative"]}, now=NOW)
        self.assertEqual((first["state"], latest["state"]), ("ok", "ok"))
        self.assertEqual(catalog.catalog_snapshot()["archives"], [])
        original_entries = {item["entry"]["entryId"]: item["entry"] for item in
                            (first, latest)}
        code, _, error = self.run_cli("archival-close", "--task", self.task_id, "--yes", "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(ActiveSubjectLog(catalog).state(self.subject)["archival_state"], "closed")
        # Force the real rollover protocol without capacity pruning the
        # deliberately closed subject before the deletion scenario begins.
        with catalog._lease() as fd:
            pointer = catalog._current(fd)
            with ImportantLogStore.open(self.directory, pointer["active"], _catalog_owned=True) as old:
                rotated = catalog._rotate(fd, pointer, old)
        archive, active = rotated["from"], rotated["to"]
        with ImportantLogStore.open(self.directory, archive, readonly=True, immutable=True) as source:
            self.assertEqual({entry_id: source.get_entry(entry_id) for entry_id in original_entries},
                             original_entries)
        return archive, active, original_entries

    def test_closed_archive_reopened_in_new_active_carries_original_start_and_latest_before_delete(self) -> None:
        archive, active, original_entries = self._closed_archive_with_anchors()
        current = ActiveSubjectLog(ImportantLogCatalog(self.directory))
        self.assertTrue(current.record_delivery(self.subject, message_id=str(uuid4()), revision=1,
                                                kind="manager_instruction", content="confirmed later activity",
                                                delivered_at="2026-10-10T02:00:00Z"))
        self.assertEqual(current.state(self.subject)["archival_state"], "open")
        before, shown, error = self.run_cli("show", "--task", self.task_id, "--json")
        self.assertEqual(before, 0, error)
        sources = {row["entry"]["entryId"]: row["sourceDbUuids"] for row in json.loads(shown)["entries"]}
        for entry_id in original_entries:
            self.assertIn(archive, sources[entry_id])

        code, output, error = self.run_cli("delete-db", "--db", archive, "--yes", "--json")
        self.assertEqual(code, 0, error + output)
        self.assertEqual(json.loads(output)["remainingDbUuids"], [active])
        with ImportantLogStore.open(self.directory, active, readonly=True) as target:
            for entry_id, expected in original_entries.items():
                self.assertEqual(target.get_entry(entry_id), expected)
        self.assertEqual(ActiveSubjectLog(ImportantLogCatalog(self.directory)).state(self.subject)["archival_state"],
                         "open")
        after, shown, error = self.run_cli("show", "--task", self.task_id, "--json")
        self.assertEqual(after, 0, error)
        rows = {row["entry"]["entryId"]: row for row in json.loads(shown)["entries"]}
        for entry_id, expected in original_entries.items():
            self.assertEqual(rows[entry_id]["entry"], expected)
            self.assertEqual(rows[entry_id]["sourceDbUuids"], [active])

    def test_closed_archive_without_reactivity_does_not_carry_unneeded_anchors(self) -> None:
        archive, active, original_entries = self._closed_archive_with_anchors()
        code, output, error = self.run_cli("delete-db", "--db", archive, "--yes", "--json")
        self.assertEqual(code, 0, error + output)
        self.assertEqual(json.loads(output)["remainingDbUuids"], [active])
        with ImportantLogStore.open(self.directory, active, readonly=True) as target:
            for entry_id in original_entries:
                self.assertIsNone(target.get_entry(entry_id))

    def test_source_open_still_protects_anchors_when_current_active_is_closed(self) -> None:
        catalog = ImportantLogCatalog(self.directory, create=True, target_bytes=1_000_000,
                                      low_water_bytes=900_000)
        grant = {"seq": 1, "at": "2026-10-10T01:00:00Z", "type": "delegated",
                 "task_id": self.task_id, "revision": 1,
                 "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())}
        self.assertTrue(ActiveSubjectLog(catalog).record_flow(grant))
        start = catalog.append(self.subject, "start", [], BODY, now=NOW)["entry"]
        with catalog._lease() as fd:
            pointer = catalog._current(fd)
            with ImportantLogStore.open(self.directory, pointer["active"], _catalog_owned=True) as old:
                rotated = catalog._rotate(fd, pointer, old)
        archive, active = rotated["from"], rotated["to"]
        code, _, error = self.run_cli("archival-close", "--task", self.task_id, "--yes", "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(ActiveSubjectLog(ImportantLogCatalog(self.directory)).state(self.subject)["archival_state"],
                         "closed")
        code, output, error = self.run_cli("delete-db", "--db", archive, "--yes", "--json")
        self.assertEqual(code, 0, error + output)
        with ImportantLogStore.open(self.directory, active, readonly=True) as target:
            self.assertEqual(target.get_entry(start["entryId"]), start)

    def _reopened_then_rolled_again(self):
        first, second, anchors = self._closed_archive_with_anchors()
        current = ActiveSubjectLog(ImportantLogCatalog(self.directory))
        self.assertTrue(current.record_delivery(self.subject, message_id=str(uuid4()), revision=1,
                                                kind="manager_instruction", content="confirmed reactivation",
                                                delivered_at="2026-10-10T02:00:00Z"))
        self.assertEqual(current.state(self.subject)["archival_state"], "open")
        catalog = ImportantLogCatalog(self.directory)
        with catalog._lease() as fd:
            pointer = catalog._current(fd)
            self.assertEqual(pointer["active"], second)
            with ImportantLogStore.open(self.directory, second, _catalog_owned=True) as old:
                rotated = catalog._rotate(fd, pointer, old)
        self.assertEqual(rotated["from"], second)
        third = rotated["to"]
        with ImportantLogStore.open(self.directory, third, readonly=True) as newest:
            self.assertIsNone(newest._connection.execute("SELECT 1 FROM sqlite_master WHERE "
                                                          "name='subject_lifecycle_v1'").fetchone())
        return first, second, third, anchors

    def test_three_databases_preserve_reopened_subject_anchors_when_new_active_lacks_state(self) -> None:
        first, second, third, anchors = self._reopened_then_rolled_again()
        code, output, error = self.run_cli("delete-db", "--db", first, "--yes", "--json")
        self.assertEqual(code, 0, error + output)
        result = json.loads(output)
        self.assertEqual((result["state"], result["databaseCount"], result["remainingDbUuids"]),
                         ("deleted", 2, sorted([second, third])))
        with ImportantLogStore.open(self.directory, third, readonly=True) as target:
            for entry_id, expected in anchors.items():
                self.assertEqual(target.get_entry(entry_id), expected)
        self.assertEqual(ActiveSubjectLog(ImportantLogCatalog(self.directory)).state(self.subject)["archival_state"],
                         "open")
        code, shown, error = self.run_cli("show", "--task", self.task_id, "--json")
        self.assertEqual(code, 0, error)
        by_id = {row["entry"]["entryId"]: row for row in json.loads(shown)["entries"]}
        for entry_id, expected in anchors.items():
            self.assertEqual(by_id[entry_id]["entry"], expected)
            self.assertEqual(by_id[entry_id]["sourceDbUuids"], [third])

    def test_newest_closed_state_makes_old_closed_anchors_unnecessary(self) -> None:
        first, _second, third, anchors = self._reopened_then_rolled_again()
        code, _, error = self.run_cli("archival-close", "--task", self.task_id, "--yes", "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(ActiveSubjectLog(ImportantLogCatalog(self.directory)).state(self.subject)["archival_state"],
                         "closed")
        code, output, error = self.run_cli("delete-db", "--db", first, "--yes", "--json")
        self.assertEqual(code, 0, error + output)
        with ImportantLogStore.open(self.directory, third, readonly=True) as target:
            for entry_id in anchors:
                self.assertIsNone(target.get_entry(entry_id))

    def test_unreadable_newer_archive_blocks_old_archive_deletion_without_intent(self) -> None:
        first, second, third, _anchors = self._reopened_then_rolled_again()
        # A checkpoint-incomplete archive cannot be read without its WAL/SHM.
        # This synthetic pointer fault makes the newer B state uninspectable.
        pointer_file = self.directory / "active.json"
        pointer = json.loads(pointer_file.read_text())
        pointer["archiveChecks"][second] = "busy"
        pointer_file.write_text(json.dumps(pointer, sort_keys=True, separators=(",", ":")))
        pointer_file.chmod(0o600)
        code, output, error = self.run_cli("delete-db", "--db", first, "--yes", "--json")
        self.assertNotEqual(code, 0)
        self.assertIn("거부", error)
        self.assertFalse((self.directory / "delete.intent.json").exists())
        self.assertTrue((self.directory / (first + ".sqlite3")).exists())
        self.assertEqual(ImportantLogCatalog(self.directory).catalog_snapshot()["active"], third)


if __name__ == "__main__":
    unittest.main()
