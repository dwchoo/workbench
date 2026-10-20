"""Requirement-derived query boundaries; real private SQLite and independent canaries."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.storage.work_records.store import RecordStore
from workbench.storage.work_records.exporter import export_record
from workbench.tasks.repository import TaskRepository


class IndependentQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="records-independent-query-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "records.sqlite3"
        self.store = RecordStore(self.db)

    def save(self, title, body="본문", **kwargs):
        result = self.store.create(title=title, body=body, actor="manager", **kwargs)
        self.assertEqual(result["status"], "saved", result)
        return result["record"]

    def ids(self, result):
        self.assertEqual(result["status"], "ok", result)
        return [row["record_id"] for row in result["records"]]

    def test_literal_unicode_sql_text_and_no_normalization(self):
        fixtures = [
            ("정확한", "Case 가 😀 100%_ ' OR 1=1 --"),
            ("대조", "case 가 100xx ' OR 2=2 --"),
            ("기타", "other unrelated"),
        ]
        records = [self.save(title, body) for title, body in fixtures]
        before = self.db.read_bytes()
        for query, expected in [("Case", [0]), ("case", [1]), ("가", [0]),
                                ("가", [1]), ("%_", [0]), ("😀", [0]),
                                ("' OR 1=1 --", [0]), ("' OR 9=9 --", [])]:
            with self.subTest(query=query):
                self.assertEqual(self.ids(self.store.search(query)),
                                 [records[index]["record_id"] for index in expected])
        self.assertEqual(self.db.read_bytes(), before)

    def test_default_and_maximum_page_sizes_and_history_complete_traversal(self):
        records = [self.save(f"needle {n}") for n in range(101)]
        self.assertEqual(len(self.store.list()["records"]), 20)
        self.assertEqual(len(self.store.search("needle")["records"]), 20)
        page = self.store.list(limit=100)
        self.assertEqual(len(page["records"]), 100)
        self.assertTrue(page["has_more"])
        tail = self.store.list(limit=100, after=page["next_after"])
        self.assertEqual(self.ids(page) + self.ids(tail),
                         [row["record_id"] for row in reversed(records)])
        self.assertFalse(tail["has_more"])
        record = records[0]
        with patch("workbench.storage.work_records.store._now", return_value="2099-01-01T00:00:00.000000Z"):
            for revision in range(2, 23):
                result = self.store.revise(record["record_id"], expected_revision=revision-1,
                                           title=f"rev {revision}", body="본문", actor="worker")
                self.assertEqual(result["status"], "saved", result)
        history = self.store.history(record["record_id"])
        self.assertEqual([r["revision"] for r in history["revisions"]], list(range(22, 2, -1)))
        tail = self.store.history(record["record_id"], after=history["next_after"])
        self.assertEqual([r["revision"] for r in tail["revisions"]], [2, 1])
        self.assertTrue(all("body" not in r for r in history["revisions"] + tail["revisions"]))
        for method in (self.store.list, lambda **kw: self.store.search("needle", **kw),
                       lambda **kw: self.store.history(record["record_id"], **kw)):
            for bad in (0, -1, True, 101):
                with self.subTest(method=method, limit=bad):
                    self.assertEqual(method(limit=bad)["status"], "rejected")

    def test_latest_task_filter_and_cursor_scope_remain_bound(self):
        with TaskRepository(self.root / "tasks.sqlite3") as tasks:
            for task in ("task-a", "task-b"):
                tasks.create_task({"goal": task}, task_id=task)
            self.store = RecordStore(self.db, task_repository=tasks)
            moved = self.save("needle moved", task_id="task-a")
            a1 = self.save("needle a1", task_id="task-a")
            a2 = self.save("needle a2", task_id="task-a")
            self.store.revise(moved["record_id"], expected_revision=1,
                              title="needle moved", body="current", task_id="task-b", actor="worker")
            self.assertEqual(set(self.ids(self.store.search("needle", task_id="task-a"))),
                             {a1["record_id"], a2["record_id"]})
            self.assertEqual(self.ids(self.store.list(task_id="task-b")), [moved["record_id"]])
            cursor = self.store.search("needle", task_id="task-a", limit=1)["next_after"]
            before = self.db.read_bytes()
            for kwargs in ({"task_id": "task-b"}, {"task_id": None}):
                self.assertEqual(self.store.search("needle", after=cursor, **kwargs),
                                 {"status": "rejected", "reason": "invalid_cursor"})
            other = RecordStore(self.root / "other.sqlite3")
            self.assertEqual(other.search("needle", task_id="task-a", after=cursor)["status"], "rejected")
            self.assertFalse(other.path.exists())
            self.assertEqual(self.db.read_bytes(), before)

    def test_search_query_length_bound_and_absent_reads_do_not_create(self):
        record_id = str(uuid4())
        self.assertEqual(self.store.search("가" * 256)["status"], "ok")
        self.assertEqual(self.store.search("가" * 257),
                         {"status": "rejected", "reason": "query_too_large"})
        self.assertEqual(self.store.search("")["status"], "rejected")
        self.assertEqual(self.store.get(record_id)["status"], "not_found")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unknown_history_is_not_found_with_or_without_database(self):
        # SPEC Agent tools show/history: unknown IDs must be distinguishable from success.
        unknown = str(uuid4())
        with self.subTest(database="absent"):
            self.assertEqual(self.store.history(unknown)["status"], "not_found")
        self.assertFalse(self.db.exists())
        self.save("present")
        before = self.db.read_bytes()
        with self.subTest(database="present"):
            self.assertEqual(self.store.history(unknown)["status"], "not_found")
        self.assertEqual(self.db.read_bytes(), before)

    def test_corrupt_existing_database_is_unavailable_not_empty(self):
        self.db.write_bytes(b"independent invalid SQLite sentinel")
        self.db.chmod(0o600)
        before = self.db.read_bytes()
        for result in (self.store.list(), self.store.search("needle"), self.store.history(str(uuid4()))):
            self.assertEqual(result["status"], "unavailable", result)
        self.assertEqual(self.db.read_bytes(), before)

    def test_search_to_selected_revision_export_keeps_full_originals(self):
        old_body = "# 과거 원문\r\n" + "가😀" * 200 + "\n끝 %_"
        first = self.save("old", old_body)
        latest_body = "latest canary\n" + "나" * 200
        revised = self.store.revise(first["record_id"], expected_revision=1,
                                   title="current", body=latest_body, actor="worker")
        self.assertEqual(revised["status"], "saved")
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        found = self.store.search("latest canary")["records"]
        self.assertEqual([(row["record_id"], row["revision"]) for row in found], [(first["record_id"], 2)])
        self.assertNotIn("body", found[0])
        old = self.store.get(found[0]["record_id"], revision=1)["record"]
        result = export_record(old, self.root / "old.md")
        self.assertEqual(result["status"], "exported", result)
        self.assertTrue((self.root / "old.md").read_bytes().endswith(old_body.encode("utf-8")))
        self.assertIn(b"- Revision: 1", (self.root / "old.md").read_bytes())
        self.assertEqual(self.store.get(first["record_id"])["record"]["body"], latest_body)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
