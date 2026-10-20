"""Independent WR-02 public-seam checks derived from approved storage requirements."""
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from workbench.storage.work_records.store import RecordStore
from workbench.tasks.repository import TaskRepository


class IndependentStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "records.sqlite3"

    def test_other_process_reads_exact_committed_markdown_and_unicode_snapshot(self):
        body = "# 결과\r\n\r\n> '한글' e\u0301 👩🏽‍💻\n```sql\nDROP TABLE records; -- % _\n```\n끝\n"
        created = RecordStore(self.db).create(title="e\u0301 실험", body=body, actor="manager")
        self.assertEqual(created["status"], "saved")
        record = created["record"]
        # A fresh interpreter cannot reuse a connection or an in-memory snapshot.
        program = """import json, sys
from pathlib import Path
from workbench.storage.work_records.store import RecordStore
print(json.dumps(RecordStore(Path(sys.argv[1])).get(sys.argv[2]), ensure_ascii=False))
"""
        child = subprocess.run([sys.executable, "-c", program, str(self.db), record["record_id"]],
                               capture_output=True, text=True, timeout=10, check=True)
        observed = json.loads(child.stdout)
        self.assertEqual(observed, {"status": "ok", "record": record})
        self.assertEqual(observed["record"]["body"].encode("utf-8"), body.encode("utf-8"))
        self.assertEqual(observed["record"]["title"], "e\u0301 실험")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["records.sqlite3"])

    def test_task_link_is_a_full_revision_snapshot_and_null_uses_no_repository(self):
        with TaskRepository(self.root / "tasks.sqlite3") as tasks:
            tasks.create_task({"goal": "실험 A"}, task_id="experiment-a")
            tasks.create_task({"goal": "실험 B"}, task_id="experiment-b")
            original_tasks = [tasks.get_task_spec(t) for t in ("experiment-a", "experiment-b")]
            linked = RecordStore(self.db, task_repository=tasks).create(
                title="첫 연결", body="원문 A", task_id="experiment-a", actor="worker")
            self.assertEqual(linked["status"], "saved")
            first = linked["record"]
            # Null Task remains usable even without a TaskRepository object.
            store = RecordStore(self.db)
            second = store.revise(first["record_id"], expected_revision=1,
                                  title="프로젝트 메모", body="원문 B", task_id=None, actor="manager")
            self.assertEqual(second["status"], "saved")
            third = RecordStore(self.db, task_repository=tasks).revise(first["record_id"], expected_revision=2,
                title="다른 작업 연결", body="원문 C", task_id="experiment-b", actor="worker")
            self.assertEqual(third["status"], "saved")
            self.assertEqual([store.get(first["record_id"], r)["record"]["task_id"] for r in (1, 2, 3)],
                             ["experiment-a", None, "experiment-b"])
            self.assertEqual(store.get(first["record_id"])["record"], third["record"])
            self.assertEqual(store.get(first["record_id"], 1)["record"], first)
            self.assertEqual([tasks.get_task_spec(t) for t in ("experiment-a", "experiment-b")], original_tasks)
            failed = RecordStore(self.db, task_repository=tasks).revise(first["record_id"], expected_revision=3,
                title="bad", body="bad", task_id="missing-task", actor="manager")
            self.assertEqual(failed, {"status": "rejected", "reason": "task_not_found"})
            self.assertEqual(store.get(first["record_id"])["record"], third["record"])

    def test_existing_insecure_or_hardlinked_db_is_unavailable_and_bytes_unchanged(self):
        store = RecordStore(self.db)
        first = store.create(title="보존", body="정상 sentinel", actor="manager")["record"]
        original = self.db.read_bytes()
        self.db.chmod(0o644)
        self.assertEqual(store.get(first["record_id"])["status"], "unavailable")
        self.assertEqual(store.revise(first["record_id"], expected_revision=1,
            title="lost", body="lost", actor="worker")["status"], "unavailable")
        self.assertEqual(self.db.read_bytes(), original)
        self.db.chmod(0o600)
        alias = self.root / "alias.sqlite3"
        os.link(self.db, alias)
        self.assertEqual(store.get(first["record_id"])["status"], "unavailable")
        self.assertEqual(store.create(title="lost", body="lost", actor="worker")["status"], "unavailable")
        self.assertEqual(alias.read_bytes(), original)
        alias.unlink()
        self.assertEqual(store.get(first["record_id"])["record"], first)

    def test_initial_oversized_input_does_not_create_store_or_other_files(self):
        store = RecordStore(self.db)
        for title, body in (("😀" * 257, "body"), ("title", "한" * 21845 + "xy")):
            with self.subTest(title_chars=len(title), body_bytes=len(body.encode("utf-8"))):
                self.assertEqual(store.create(title=title, body=body, actor="manager")["status"], "rejected")
                self.assertEqual(list(self.root.iterdir()), [])
        body = "한" * 21845 + "x"
        saved = store.create(title="😀" * 256, body=body, actor="worker")
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(RecordStore(self.db).get(saved["record"]["record_id"])["record"]["body"], body)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
