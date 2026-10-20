"""Public RecordStore query behavior against private disposable SQLite stores."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from workbench.storage.work_records.store import RecordStore


class RecordStoreQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="record-query-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RecordStore(self.root / "records.sqlite3")

    def test_missing_store_lists_empty_without_creating_a_database(self):
        result = self.store.list()

        self.assertEqual(result, {
            "status": "ok", "records": [], "has_more": False, "next_after": None,
        })
        self.assertFalse((self.root / "records.sqlite3").exists())

    def test_list_shows_only_latest_metadata_with_bounded_preview(self):
        original = self.store.create(title="초안", body="옛 본문", actor="manager")["record"]
        latest = self.store.revise(original["record_id"], expected_revision=1,
                                   title="최신 결정", body="가" * 161, actor="worker")["record"]
        second = self.store.create(title="두 번째", body="두 번째 원문", actor="manager")["record"]

        result = self.store.list(limit=20)

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_after"])
        rows = {row["record_id"]: row for row in result["records"]}
        self.assertEqual(set(rows), {original["record_id"], second["record_id"]})
        self.assertEqual(rows[original["record_id"]], {
            "record_id": original["record_id"], "revision": 2, "title": "최신 결정",
            "task_id": None, "created_at": original["created_at"],
            "recorded_at": latest["recorded_at"], "actor": "worker",
            "preview": "가" * 160, "preview_truncated": True,
        })
        self.assertEqual(rows[second["record_id"]]["preview"], "두 번째 원문")
        self.assertFalse(rows[second["record_id"]]["preview_truncated"])
        self.assertNotIn("body", rows[original["record_id"]])

    def test_list_task_filter_is_exact_and_none_means_no_filter(self):
        from workbench.tasks.repository import TaskRepository

        with TaskRepository(self.root / "tasks.sqlite3") as tasks:
            tasks.create_task({"goal": "A"}, task_id="task-a")
            tasks.create_task({"goal": "B"}, task_id="task-b")
            store = RecordStore(self.root / "records.sqlite3", task_repository=tasks)
            project_note = store.create(title="프로젝트", body="공유 메모", actor="manager")["record"]
            first = store.create(title="작업 A", body="메모 A", task_id="task-a", actor="worker")["record"]
            second = store.create(title="작업 B", body="메모 B", task_id="task-b", actor="manager")["record"]

            selected = store.list(task_id="task-a")
            all_records = store.list()

        self.assertEqual([row["record_id"] for row in selected["records"]], [first["record_id"]])
        self.assertEqual({row["record_id"] for row in all_records["records"]},
                         {project_note["record_id"], first["record_id"], second["record_id"]})

    def test_search_is_case_sensitive_literal_and_matches_latest_only(self):
        stale = self.store.create(title="obsolete token", body="old revision", actor="manager")["record"]
        self.store.revise(stale["record_id"], expected_revision=1,
                          title="replaced", body="현재 본문", actor="worker")
        literal = self.store.create(title="정확한 '제목 %_", body="한글 본문 😀", actor="manager")["record"]
        separated = self.store.create(title="퍼센트 % 와 밑줄 _", body="다른 내용", actor="worker")["record"]
        uppercase = self.store.create(title="Needle", body="다른 본문", actor="manager")["record"]

        punctuation = self.store.search("%_")
        quote = self.store.search("'제목")
        korean = self.store.search("한글")
        case_sensitive = self.store.search("needle")
        latest_only = self.store.search("obsolete")

        self.assertEqual(punctuation["status"], "ok")
        self.assertEqual([row["record_id"] for row in punctuation["records"]], [literal["record_id"]])
        self.assertEqual([row["record_id"] for row in quote["records"]], [literal["record_id"]])
        self.assertEqual([row["record_id"] for row in korean["records"]], [literal["record_id"]])
        self.assertEqual(case_sensitive["records"], [])
        self.assertEqual(latest_only["records"], [])
        self.assertNotIn(separated["record_id"], {row["record_id"] for row in punctuation["records"]})
        self.assertNotIn(uppercase["record_id"], {row["record_id"] for row in case_sensitive["records"]})
        self.assertNotIn(stale["record_id"], {row["record_id"] for row in latest_only["records"]})

    def test_history_unknown_valid_id_is_not_found_with_absent_or_existing_database(self):
        unknown_id = "11111111-1111-4111-8111-111111111111"
        database = self.root / "records.sqlite3"

        absent_database_result = self.store.history(unknown_id)
        self.assertFalse(database.exists())
        self.store.create(title="known", body="known record", actor="manager")
        existing_database_result = self.store.history(unknown_id)

        expected = {"status": "not_found", "reason": "not_found"}
        with self.subTest(database="absent"):
            self.assertEqual(absent_database_result, expected)
        with self.subTest(database="contains another record"):
            self.assertEqual(existing_database_result, expected)

    def test_history_returns_revision_metadata_without_revision_bodies(self):
        first = self.store.create(title="초안", body="첫 원문", actor="manager")["record"]
        second = self.store.revise(first["record_id"], expected_revision=1,
                                  title="검토", body="정정된 **원문**", actor="worker")["record"]
        third = self.store.revise(first["record_id"], expected_revision=2,
                                  title="확정", body="최종 원문 😀", actor="manager")["record"]

        result = self.store.history(first["record_id"])

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_after"])
        self.assertEqual(result["revisions"], [
            {"record_id": first["record_id"], "revision": 3, "title": "확정",
             "task_id": None, "created_at": first["created_at"],
             "recorded_at": third["recorded_at"], "actor": "manager",
             "preview": "최종 원문 😀", "preview_truncated": False},
            {"record_id": first["record_id"], "revision": 2, "title": "검토",
             "task_id": None, "created_at": first["created_at"],
             "recorded_at": second["recorded_at"], "actor": "worker",
             "preview": "정정된 **원문**", "preview_truncated": False},
            {"record_id": first["record_id"], "revision": 1, "title": "초안",
             "task_id": None, "created_at": first["created_at"],
             "recorded_at": first["recorded_at"], "actor": "manager",
             "preview": "첫 원문", "preview_truncated": False},
        ])
        self.assertNotIn("body", result["revisions"][0])

    def test_cursor_pages_use_recorded_time_then_sequence_as_stable_tie_break(self):
        stamp = "2026-10-20T01:02:03.000004Z"
        with patch("workbench.storage.work_records.store._now", return_value=stamp):
            first = self.store.create(title="첫째", body="one", actor="manager")["record"]
            second = self.store.create(title="둘째", body="two", actor="worker")["record"]
            self.store.revise(first["record_id"], expected_revision=1,
                              title="첫째 최신", body="one newer", actor="manager")
            third = self.store.create(title="셋째", body="three", actor="worker")["record"]

        first_page = self.store.list(limit=2)
        cursor = first_page["next_after"]
        second_page = self.store.list(limit=2, after=cursor)

        self.assertTrue(first_page["has_more"])
        self.assertIsInstance(cursor, str)
        self.assertEqual([row["record_id"] for row in first_page["records"]],
                         [third["record_id"], first["record_id"]])
        self.assertEqual([row["record_id"] for row in second_page["records"]], [second["record_id"]])
        self.assertFalse(second_page["has_more"])
        self.assertIsNone(second_page["next_after"])

    def test_cursor_rejects_malformed_and_foreign_filter_tokens(self):
        self.store.create(title="needle A", body="first", actor="manager")
        self.store.create(title="needle B", body="second", actor="worker")
        page = self.store.search("needle", limit=1)

        malformed = self.store.search("needle", after="../../not-a-cursor")
        changed_query = self.store.search("other", limit=1, after=page["next_after"])
        changed_action = self.store.list(limit=1, after=page["next_after"])

        self.assertTrue(page["has_more"])
        self.assertEqual(malformed, {"status": "rejected", "reason": "invalid_cursor"})
        self.assertEqual(changed_query, {"status": "rejected", "reason": "invalid_cursor"})
        self.assertEqual(changed_action, {"status": "rejected", "reason": "invalid_cursor"})

    def test_history_cursor_pages_revisions_of_its_record_only(self):
        first = self.store.create(title="one", body="one", actor="manager")["record"]
        second = self.store.revise(first["record_id"], expected_revision=1,
                                   title="two", body="two", actor="worker")["record"]
        third = self.store.revise(first["record_id"], expected_revision=2,
                                  title="three", body="three", actor="manager")["record"]

        page = self.store.history(first["record_id"], limit=1)
        next_page = self.store.history(first["record_id"], limit=1, after=page["next_after"])
        other_record = self.store.create(title="other", body="other", actor="worker")["record"]
        foreign = self.store.history(other_record["record_id"], limit=1, after=page["next_after"])

        self.assertEqual(page["revisions"][0]["revision"], 3)
        self.assertEqual(next_page["revisions"][0]["revision"], 2)
        self.assertTrue(page["has_more"])
        self.assertEqual(foreign, {"status": "rejected", "reason": "invalid_cursor"})
        self.assertEqual(third["revision"], 3)
        self.assertEqual(second["revision"], 2)

    def test_invalid_limits_and_overlong_queries_are_explicitly_rejected_without_db_creation(self):
        self.assertEqual(self.store.list(limit=101), {
            "status": "rejected", "reason": "limit_too_large",
        })
        self.assertEqual(self.store.search("x" * 257), {
            "status": "rejected", "reason": "query_too_large",
        })
        self.assertEqual(self.store.history("not-an-id"), {
            "status": "rejected", "reason": "invalid_argument",
        })
        self.assertFalse((self.root / "records.sqlite3").exists())

    def test_schema_valid_but_invalid_snapshot_is_unavailable_in_query_surfaces(self):
        first = self.store.create(title="valid", body="original body", actor="manager")["record"]
        database = self.root / "records.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                """INSERT INTO revisions
                   (record_id,revision,title,body,task_id,recorded_at,actor)
                   VALUES(?,?,?,?,?,?,?)""",
                (first["record_id"], 2, "damaged", sqlite3.Binary(b"invalid UTF-8 body"), None,
                 "2026-10-20T01:02:03Z", "worker"),
            )
        database_before = database.read_bytes()

        selected = self.store.get(first["record_id"], revision=2)
        listed = self.store.list()
        history = self.store.history(first["record_id"])

        unavailable = {"status": "unavailable", "reason": "store_unavailable"}
        self.assertEqual(selected, unavailable)
        self.assertEqual(listed, unavailable)
        self.assertEqual(history, unavailable)
        self.assertEqual(database.read_bytes(), database_before)
