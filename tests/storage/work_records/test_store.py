"""Record behavior through the public store, using disposable real SQLite files."""
from pathlib import Path
import tempfile
import unittest

from workbench.backend.paths import DataLayout
from workbench.storage.work_records.store import RecordStore


class RecordStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "records.sqlite3"

    def test_project_note_body_survives_store_reopen_without_docs_files(self):
        body = '# 결정\n\n"한글" **원문**\r\n줄 끝  \n😀 % _ \\n\n'
        saved = RecordStore(self.path).create(title="메모 😀", body=body, actor="manager")
        self.assertEqual(saved["status"], "saved")
        record_id = saved["record"]["record_id"]
        record = RecordStore(self.path).get(record_id)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["record"]["body"], body)
        self.assertEqual(record["record"]["title"], "메모 😀")
        self.assertEqual(record["record"]["revision"], 1)
        self.assertIsNone(record["record"]["task_id"])
        self.assertEqual(record["record"]["actor"], "manager")
        self.assertEqual(record["record"]["created_at"], record["record"]["recorded_at"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "docs").exists())
        self.assertFalse((self.root / "tasks.sqlite3").exists())
        self.assertEqual(DataLayout(self.root).records, self.path)

    def test_revision_preserves_previous_snapshot_and_rejects_stale_writer(self):
        store = RecordStore(self.path)
        first = store.create(title="이전", body="첫 원문\n", actor="manager")["record"]
        record_id = first["record_id"]
        revised = RecordStore(self.path).revise(record_id, expected_revision=1,
            title="최신", body="정정 **원문**\n", actor="worker")
        self.assertEqual(revised["status"], "saved")
        self.assertEqual(revised["record"]["revision"], 2)
        self.assertEqual(revised["record"]["created_at"], first["created_at"])
        self.assertEqual(store.get(record_id, 1)["record"], first)
        self.assertEqual(store.get(record_id)["record"]["body"], "정정 **원문**\n")
        conflict = store.revise(record_id, expected_revision=1, title="lost", body="lost", actor="manager")
        self.assertEqual(conflict, {"status": "rejected", "reason": "revision_conflict"})
        self.assertEqual(store.get(record_id)["record"], revised["record"])
        self.assertEqual(store.get(record_id, 1)["record"], first)

    def test_oversize_and_invalid_writes_are_rejected_without_changing_previous_record(self):
        store = RecordStore(self.path)
        body = "가" * 21845 + "a"  # exactly 65,536 UTF-8 bytes, independently worked
        first = store.create(title="😀" * 256, body=body, actor="manager")["record"]
        self.assertEqual(store.get(first["record_id"])["record"]["body"], body)
        for changed, reason in (({"body": body + "a"}, "body_too_large"),
                                ({"title": "😀" * 257}, "title_too_large"),
                                ({"body": ""}, "invalid_argument"),
                                ({"body": "\ud800"}, "invalid_argument"),
                                ({"actor": "unbound"}, "invalid_argument"),
                                ({"expected_revision": True}, "invalid_argument")):
            args = dict(expected_revision=1, title="new", body="new", actor="worker")
            args.update(changed)
            with self.subTest(changed=list(changed)):
                self.assertEqual(store.revise(first["record_id"], **args), {"status": "rejected", "reason": reason})
                self.assertEqual(store.get(first["record_id"])["record"], first)

    def test_missing_records_and_revisions_are_not_found_without_creating_db(self):
        store = RecordStore(self.path)
        missing = "11111111-1111-4111-8111-111111111111"
        self.assertEqual(store.get(missing), {"status": "not_found", "reason": "not_found"})
        self.assertFalse(self.path.exists())
        self.assertEqual(store.revise(missing, expected_revision=1, title="x", body="x", actor="worker"),
                         {"status": "not_found", "reason": "not_found"})
        self.assertFalse(self.path.exists())
        first = store.create(title="x", body="x", actor="manager")["record"]
        self.assertEqual(store.get(first["record_id"], 2), {"status": "not_found", "reason": "not_found"})
        self.assertEqual(store.get("../bad"), {"status": "rejected", "reason": "invalid_argument"})
        self.assertEqual(store.get(first["record_id"], True), {"status": "rejected", "reason": "invalid_argument"})

    def test_task_reference_is_checked_in_same_store_without_mutating_task_approval(self):
        from workbench.tasks.repository import TaskRepository
        with TaskRepository(self.root / "tasks.sqlite3") as tasks:
            tasks.create_task({"goal": "기존 Task"}, task_id="task-1")
            store = RecordStore(self.path, task_repository=tasks)
            self.assertEqual(store.create(title="bad", body="bad", task_id="unknown", actor="manager"),
                             {"status": "rejected", "reason": "task_not_found"})
            self.assertFalse(self.path.exists())
            before = tasks.get_task_spec("task-1")
            saved = store.create(title="실험", body="Task 원문\n", task_id="task-1", actor="worker")
            self.assertEqual(saved["status"], "saved")
            self.assertEqual(store.get(saved["record"]["record_id"])["record"]["task_id"], "task-1")
            self.assertEqual(tasks.get_task_spec("task-1"), before)
            self.assertIsNone(tasks.get_current_run("task-1"))
            with tempfile.TemporaryDirectory(dir=self.root) as other:
                self.assertEqual(RecordStore(Path(other) / "records.sqlite3", task_repository=tasks).create(
                    title="x", body="x", task_id="task-1", actor="worker"),
                    {"status": "unavailable", "reason": "task_lookup_unavailable"})
        self.assertEqual(RecordStore(self.path).create(title="x", body="x", task_id="task-1", actor="manager"),
                         {"status": "unavailable", "reason": "task_lookup_unavailable"})

    def test_unsafe_or_unknown_store_is_unavailable_and_never_overwritten(self):
        import os
        import sqlite3
        sentinel = b"do not replace existing user bytes\n"
        self.path.write_bytes(sentinel)
        self.path.chmod(0o600)
        store = RecordStore(self.path)
        self.assertEqual(store.create(title="x", body="x", actor="manager")["status"], "unavailable")
        self.assertEqual(store.get("11111111-1111-4111-8111-111111111111")["status"], "unavailable")
        self.assertEqual(self.path.read_bytes(), sentinel)
        self.path.unlink()
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA user_version=99")
            db.execute("CREATE TABLE sentinel(value TEXT)")
            db.execute("INSERT INTO sentinel VALUES('keep')")
        self.path.chmod(0o600)
        before = self.path.read_bytes()
        self.assertEqual(store.create(title="x", body="x", actor="manager")["status"], "unavailable")
        self.assertEqual(self.path.read_bytes(), before)
        self.path.unlink()
        target = self.root / "target"
        target.write_bytes(sentinel)
        target.chmod(0o600)
        self.path.symlink_to(target)
        self.assertEqual(store.create(title="x", body="x", actor="manager")["status"], "unavailable")
        self.assertEqual(target.read_bytes(), sentinel)
        self.path.unlink()
        self.root.chmod(0o755)
        self.assertEqual(store.create(title="x", body="x", actor="manager")["status"], "unavailable")
        self.assertFalse(self.path.exists())
        self.root.chmod(0o700)
        link = self.root / "linked-dir"
        plain = self.root / "plain-dir"
        plain.mkdir(mode=0o700)
        link.symlink_to(plain, target_is_directory=True)
        self.assertEqual(RecordStore(link / "records.sqlite3").create(title="x", body="x", actor="worker")["status"], "unavailable")
        self.assertFalse((plain / "records.sqlite3").exists())

    def test_database_refuses_mutation_or_replace_of_saved_revisions(self):
        import sqlite3
        store = RecordStore(self.path)
        first = store.create(title="보존", body="원본 sentinel\n", actor="manager")["record"]
        # Direct SQLite is deliberate tampering/failure injection, not the assertion seam.
        with sqlite3.connect(self.path) as db:
            for sql in (
                "UPDATE revisions SET body='tampered' WHERE record_id=?",
                "DELETE FROM revisions WHERE record_id=?",
                "INSERT OR REPLACE INTO revisions(record_id,revision,title,body,task_id,recorded_at,actor) VALUES(?,1,'tampered','tampered',NULL,'2026-10-20T00:00:00Z','worker')",
            ):
                with self.subTest(sql=sql[:30]), self.assertRaises(sqlite3.DatabaseError):
                    db.execute(sql, (first["record_id"],))
                db.rollback()
        self.assertEqual(store.get(first["record_id"])["record"], first)

    def test_tampered_schema_is_unavailable_without_silent_repair(self):
        import sqlite3
        store = RecordStore(self.path)
        first = store.create(title="keep", body="keep", actor="manager")["record"]
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TRIGGER revisions_immutable_update")
        before = self.path.read_bytes()
        self.assertEqual(store.get(first["record_id"])["status"], "unavailable")
        self.assertEqual(store.revise(first["record_id"], expected_revision=1, title="new", body="new", actor="worker")["status"], "unavailable")
        self.assertEqual(self.path.read_bytes(), before)

    def test_two_independent_writers_only_one_advances_expected_revision(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        store = RecordStore(self.path)
        first = store.create(title="first", body="first", actor="manager")["record"]
        ready = Barrier(2)
        def revise(body):
            ready.wait(timeout=3)
            return RecordStore(self.path).revise(first["record_id"], expected_revision=1,
                title="concurrent", body=body, actor="worker")
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.map(revise, ["writer A", "writer B"])
        self.assertEqual(sorted([a["status"], b["status"]]), ["rejected", "saved"])
        loser = a if a["status"] == "rejected" else b
        self.assertEqual(loser["reason"], "revision_conflict")
        self.assertEqual(store.get(first["record_id"])["record"]["revision"], 2)
        self.assertEqual(store.get(first["record_id"], 1)["record"], first)

    def test_real_busy_write_failure_is_bounded_and_preserves_previous_revision(self):
        import sqlite3
        import time
        store = RecordStore(self.path)
        first = store.create(title="first", body="sentinel", actor="manager")["record"]
        with sqlite3.connect(self.path) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            failed = store.revise(first["record_id"], expected_revision=1, title="bad", body="lost", actor="worker")
            self.assertEqual(failed, {"status": "unavailable", "reason": "store_unavailable"})
            self.assertLess(time.monotonic() - started, 2)
            blocker.rollback()
        self.assertEqual(RecordStore(self.path).get(first["record_id"])["record"], first)

    def test_explicit_sequence_replace_cannot_remove_preserved_revision(self):
        import sqlite3
        store = RecordStore(self.path)
        first = store.create(title="최초", body="revision 1 sentinel\n", actor="manager")["record"]
        second = store.revise(first["record_id"], expected_revision=1,
            title="후속", body="revision 2 sentinel\n", actor="worker")["record"]
        rejected = False
        # Failure injection uses the review's physical-key collision with recursive
        # triggers OFF, then observes durable snapshots through the public store.
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA recursive_triggers=OFF")
            try:
                db.execute("""INSERT OR REPLACE INTO revisions
                    (sequence,record_id,revision,title,body,task_id,recorded_at,actor)
                    SELECT sequence,record_id,3,title,'replacement',task_id,recorded_at,actor
                    FROM revisions WHERE record_id=? AND revision=1""", (first["record_id"],))
                db.commit()
            except sqlite3.DatabaseError:
                db.rollback()
                rejected = True
        self.assertEqual(store.get(first["record_id"], 1), {"status": "ok", "record": first})
        self.assertEqual(store.get(first["record_id"]), {"status": "ok", "record": second})
        self.assertTrue(rejected)

    def test_explicit_record_rowid_replace_cannot_remove_existing_record(self):
        import sqlite3
        store = RecordStore(self.path)
        first = store.create(title="보존 1", body="first record sentinel\n", actor="manager")["record"]
        second = store.create(title="보존 2", body="second record sentinel\n", actor="worker")["record"]
        rejected = False
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA recursive_triggers=OFF")
            try:
                db.execute("""INSERT OR REPLACE INTO records(rowid,record_id,created_at)
                    SELECT rowid,'11111111-1111-4111-8111-111111111111',created_at
                    FROM records WHERE record_id=?""", (first["record_id"],))
                db.commit()
            except sqlite3.DatabaseError:
                db.rollback()
                rejected = True
        self.assertEqual(store.get(first["record_id"]), {"status": "ok", "record": first})
        self.assertEqual(store.get(second["record_id"]), {"status": "ok", "record": second})
        self.assertTrue(rejected)
