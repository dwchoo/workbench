"""Records-v1 identity alone must not admit corrupted migration input."""
import json
import sqlite3
import unittest

from tests.backend.installation import test_migration as support
from workbench.storage.work_records.store import RecordStore


class IndependentRecordsMigrationTests(unittest.TestCase):
    def test_v1_schema_or_snapshot_corruption_is_refused_without_changing_source(self):
        # Both sources have the supported user_version. The new migration boundary
        # must check actual definitions and snapshot content beyond that number.
        for corruption in ("missing_preservation_trigger", "invalid_actor_snapshot"):
            with self.subTest(corruption=corruption):
                case = support.MigrationTests(
                    "test_nonempty_rollback_journal_is_explicitly_unsupported_without_source_writes")
                case.setUp()
                self.addCleanup(case.doCleanups)
                database = case.source / "records.sqlite3"
                first = RecordStore(database).create(
                    title="보존된 첫 기록", body="정상 원문\n", actor="manager")["record"]
                with sqlite3.connect(database) as connection:
                    if corruption == "missing_preservation_trigger":
                        connection.execute("DROP TRIGGER revisions_immutable_delete")
                    else:
                        # External corruption can append a physically valid row
                        # without satisfying the public host-role contract.
                        connection.execute("""INSERT INTO revisions
                            (record_id,revision,title,body,task_id,recorded_at,actor)
                            VALUES(?,2,?,?,NULL,?,?)""", (
                            first["record_id"], "외부 손상", "잘못된 actor 원문",
                            "2026-10-20T10:00:00Z", "untrusted-actor"))
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone(), (1,))
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                untouched = support.inventory(case.source)

                result = case.migrate()

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("migration", result.stderr.lower())
                self.assertNotIn("Traceback", result.stderr)
                if corruption == "missing_preservation_trigger":
                    self.assertIn("records schema definitions differ", result.stderr)
                self.assertEqual(support.inventory(case.source), untouched)
                target = case.project / ".wb"
                self.assertFalse((target / "project.json").exists())
                self.assertFalse((target / "migration-manifest.json").exists())
                if (target / "migration.json").exists():
                    self.assertEqual(json.loads((target / "migration.json").read_text())["state"], "failed")


if __name__ == "__main__":
    unittest.main()
