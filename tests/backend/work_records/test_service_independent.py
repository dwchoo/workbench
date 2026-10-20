"""Independent WR-05 adapter gaps through actual authenticated G3 sockets."""
from pathlib import Path
import sqlite3
import unittest

from tests.backend.work_records import test_service as support
from workbench.tasks.repository import TaskRepository


def request(action, **fields):
    # Hand-written public records-v1 shape, without model-supplied host identity.
    value = {
        "action": action, "record_id": None, "revision": None,
        "expected_revision": None, "title": None, "body": None,
        "task_id": None, "query": None, "limit": None, "after": None,
        "output_path": None,
    }
    value.update(fields)
    return value


class IndependentRecordAdapterTests(unittest.TestCase):
    def setUp(self):
        # Reuse only disposable real-server transport setup, not worker assertions.
        self.fixture = support.RecordToolServiceTests(
            "test_manager_create_saves_exact_body_in_host_project_without_task_db")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_relative_export_preserves_dangling_leaf_and_exports_selected_revision(self):
        case = self.fixture
        old_body = "독립 이전본\r\n```md\n# 본문 😀\n```\r\n끝  "
        first = case.call("manager", request("create", title="이전 제목", body=old_body))
        self.assertEqual(first["status"], "saved")
        record_id = first["record"]["record_id"]
        revised = case.call("worker", request("revise", record_id=record_id,
            expected_revision=1, title="최신 제목", body="다른 최신본"))
        self.assertEqual(revised["status"], "saved")
        source_before = case.backend.layout.records.read_bytes()
        output_dir = case.project / "출력"
        output_dir.mkdir()
        relative_output = "출력/선택본.md"
        output = case.project / relative_output
        self.assertNotEqual(Path.cwd(), case.project)
        selected = case.call("worker", request("export", record_id=record_id,
            revision=1, output_path=relative_output))
        self.assertEqual(selected, {
            "status": "exported", "path": str(output), "record_id": record_id, "revision": 1,
        })
        metadata, body = output.read_bytes().split(b"\n\n---\n\n", 1)
        self.assertEqual(body, old_body.encode("utf-8"))
        self.assertIn(record_id.encode(), metadata)
        self.assertIn("이전 제목".encode(), metadata)
        self.assertIn(b"Revision: 1", metadata)

        # A leaf symlink to a missing target must stay a collision. Resolving the
        # leaf before passing to exporter would incorrectly create this target.
        outside = case.root / "outside-project"
        outside.mkdir()
        target = outside / "must-stay-absent.md"
        link = output_dir / "dangling.md"
        link.symlink_to(target)
        link_inode = link.lstat().st_ino
        collision = case.call("manager", request("export", record_id=record_id,
            output_path="출력/dangling.md"))
        self.assertEqual(collision, {
            "status": "unavailable", "reason": "output_exists", "path": str(link),
        })
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.lstat().st_ino, link_inode)
        self.assertEqual(link.readlink(), target)
        self.assertFalse(target.exists())
        self.assertEqual(case.backend.layout.records.read_bytes(), source_before)
        # An explicitly requested absolute path outside the project is supported.
        explicit = outside / "explicit.md"
        result = case.call("manager", request("export", record_id=record_id,
            revision=1, output_path=str(explicit)))
        self.assertEqual(result["status"], "exported")
        self.assertEqual(result["path"], str(explicit))
        self.assertEqual(explicit.read_bytes().split(b"\n\n---\n\n", 1)[1], old_body.encode())
        self.assertEqual(case.backend.layout.records.read_bytes(), source_before)

    def test_task_present_only_in_other_project_is_not_a_valid_host_task(self):
        case = self.fixture
        with TaskRepository(case.backend.layout.tasks) as host:
            host.create_task({"goal": "host sentinel"}, task_id="local-task")
        other_state = case.root / "other-project-state"
        other_state.mkdir(mode=0o700)
        foreign_db = other_state / "tasks.sqlite3"
        with TaskRepository(foreign_db) as foreign:
            foreign.create_task({"goal": "foreign sentinel"}, task_id="foreign-task")
        # Existing TaskRepository opens reapply trigger DDL, so SQLite header
        # counters are not a supported byte-identity guarantee. Preserve actual
        # schema and all Task/authorization rows, read without repository effects.
        def logical_contents(database):
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                return tuple(connection.iterdump())

        host_before = logical_contents(case.backend.layout.tasks)
        foreign_before = foreign_db.read_bytes()

        result = case.call("worker", request("create", title="잘못된 연결", body="저장되면 안 되는 본문",
            task_id="foreign-task"))

        self.assertEqual(result, {"status": "rejected", "reason": "task_not_found"})
        self.assertEqual(logical_contents(case.backend.layout.tasks), host_before)
        self.assertEqual(foreign_db.read_bytes(), foreign_before)
        self.assertFalse(case.backend.layout.records.exists())
        self.assertFalse((other_state / "records.sqlite3").exists())
        self.assertFalse((case.project / "docs").exists())


if __name__ == "__main__":
    unittest.main()
