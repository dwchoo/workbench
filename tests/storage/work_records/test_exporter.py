"""Markdown export behavior through the public snapshot exporter."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from workbench.storage.work_records.exporter import export_record
from workbench.storage.work_records.store import RecordStore
from workbench.tasks.repository import TaskRepository


class RecordExporterTests(unittest.TestCase):
    def test_selected_revision_exports_its_exact_body_and_source_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "records.sqlite3"
            with TaskRepository(root / "tasks.sqlite3") as tasks:
                tasks.create_task({"goal": "기록 export fixture"}, task_id="task-17")
                store = RecordStore(database, task_repository=tasks)
                with patch("workbench.storage.work_records.store._now", side_effect=[
                    "2026-10-20T01:02:03.000004Z", "2026-10-20T01:05:06.000007Z",
                ]), patch("workbench.storage.work_records.store.uuid4",
                         return_value=UUID("11111111-1111-4111-8111-111111111111")):
                    first = store.create(
                        title="이전 메모 😀", body='# 결정\r\n\n"한글" **원문**  \n😀 % _\n',
                        task_id="task-17", actor="manager",
                    )["record"]
                    store.revise(
                        first["record_id"], expected_revision=1,
                        title="수정본", body="후속 revision 본문", task_id="task-17", actor="worker",
                    )
                selected = store.get(first["record_id"], revision=1)["record"]
                database_before = database.read_bytes()
                destination = root / "내보낸 기록.md"

                result = export_record(selected, destination)

                self.assertEqual(result, {
                    "status": "exported", "path": str(destination),
                    "record_id": "11111111-1111-4111-8111-111111111111", "revision": 1,
                })
                self.assertEqual(destination.read_bytes(), (
                    "# 이전 메모 😀\n\n"
                    "- Record ID: `11111111-1111-4111-8111-111111111111`\n"
                    "- Revision: 1\n"
                    "- Created at: 2026-10-20T01:02:03.000004Z\n"
                    "- Recorded at: 2026-10-20T01:02:03.000004Z\n"
                    "- Actor: manager\n"
                    "- Task ID: `task-17`\n\n"
                    "---\n\n"
                    '# 결정\r\n\n"한글" **원문**  \n😀 % _\n'
                ).encode("utf-8"))
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
                self.assertEqual(database.read_bytes(), database_before)
                self.assertEqual(store.get(first["record_id"], 1)["record"], first)
                latest = store.get(first["record_id"])["record"]
                self.assertEqual(latest["revision"], 2)
                latest_path = root / "최신 기록.md"
                self.assertEqual(export_record(latest, latest_path), {
                    "status": "exported", "path": str(latest_path),
                    "record_id": "11111111-1111-4111-8111-111111111111", "revision": 2,
                })
                self.assertEqual(latest_path.read_bytes(), (
                    "# 수정본\n\n"
                    "- Record ID: `11111111-1111-4111-8111-111111111111`\n"
                    "- Revision: 2\n"
                    "- Created at: 2026-10-20T01:02:03.000004Z\n"
                    "- Recorded at: 2026-10-20T01:05:06.000007Z\n"
                    "- Actor: worker\n"
                    "- Task ID: `task-17`\n\n"
                    "---\n\n"
                    "후속 revision 본문"
                ).encode("utf-8"))
                self.assertEqual(database.read_bytes(), database_before)

    def test_existing_file_symlink_and_records_database_are_never_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = {
                "record_id": "11111111-1111-4111-8111-111111111111", "revision": 1,
                "title": "보존", "body": "원문", "task_id": None,
                "created_at": "2026-10-20T01:02:03Z", "recorded_at": "2026-10-20T01:02:03Z",
                "actor": "manager",
            }
            regular = root / "existing.md"
            regular.write_bytes(b"existing file sentinel\x00")
            self.assertEqual(export_record(snapshot, regular), {
                "status": "unavailable", "reason": "output_exists", "path": str(regular),
            })
            self.assertEqual(regular.read_bytes(), b"existing file sentinel\x00")

            target = root / "linked-target.md"
            target.write_bytes(b"symlink target sentinel\x00")
            link = root / "linked.md"
            link.symlink_to(target)
            self.assertEqual(export_record(snapshot, link), {
                "status": "unavailable", "reason": "output_exists", "path": str(link),
            })
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_bytes(), b"symlink target sentinel\x00")

            database = root / "records.sqlite3"
            saved = RecordStore(database).create(title="DB", body="source database", actor="worker")
            self.assertEqual(saved["status"], "saved")
            database_before = database.read_bytes()
            self.assertEqual(export_record(snapshot, database), {
                "status": "unavailable", "reason": "output_exists", "path": str(database),
            })
            self.assertEqual(database.read_bytes(), database_before)

    def test_partial_write_failure_never_unlinks_a_replacement_after_cleanup_stat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "partial.md"
            snapshot = {
                "record_id": "11111111-1111-4111-8111-111111111111", "revision": 1,
                "title": "partial", "body": "content that will be interrupted", "task_id": None,
                "created_at": "2026-10-20T01:02:03Z", "recorded_at": "2026-10-20T01:02:03Z",
                "actor": "manager",
            }
            actual_write = os.write
            actual_stat = os.stat
            wrote_partial = False
            replacement_injected = False

            def fail_after_short_write(fd, payload):
                nonlocal wrote_partial
                if not wrote_partial:
                    wrote_partial = True
                    return actual_write(fd, payload[:13])
                raise OSError("injected output failure")

            def replace_after_stat(path, *args, **kwargs):
                nonlocal replacement_injected
                observed = actual_stat(path, *args, **kwargs)
                if (not replacement_injected and os.fspath(path) == destination.name
                        and kwargs.get("dir_fd") is not None):
                    destination.unlink()
                    destination.write_bytes(b"replacement sentinel\x00")
                    replacement_injected = True
                return observed

            with patch("workbench.storage.work_records.exporter.os.write", side_effect=fail_after_short_write), \
                    patch("workbench.storage.work_records.exporter.os.stat", side_effect=replace_after_stat):
                result = export_record(snapshot, destination)

            self.assertTrue(wrote_partial)
            self.assertEqual(result, {
                "status": "unavailable", "reason": "output_cleanup_unknown", "path": str(destination),
            })
            if replacement_injected:
                self.assertEqual(destination.read_bytes(), b"replacement sentinel\x00")
            else:
                self.assertEqual(destination.read_bytes(), b"# partial\n\n- ")

    def test_partial_failure_does_not_remove_a_replacement_at_the_same_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "raced.md"
            snapshot = {
                "record_id": "11111111-1111-4111-8111-111111111111", "revision": 1,
                "title": "race", "body": "content", "task_id": None,
                "created_at": "2026-10-20T01:02:03Z", "recorded_at": "2026-10-20T01:02:03Z",
                "actor": "manager",
            }

            def replace_then_fail(fd, payload):
                destination.unlink()
                destination.write_bytes(b"replacement sentinel\x00")
                raise OSError("injected concurrent replacement")

            with patch("workbench.storage.work_records.exporter.os.write", side_effect=replace_then_fail):
                result = export_record(snapshot, destination)

            self.assertEqual(result, {
                "status": "unavailable", "reason": "output_cleanup_unknown", "path": str(destination),
            })
            self.assertEqual(destination.read_bytes(), b"replacement sentinel\x00")

    def test_missing_or_unavailable_parent_never_creates_a_fallback_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = {
                "record_id": "11111111-1111-4111-8111-111111111111", "revision": 1,
                "title": "parent", "body": "content", "task_id": None,
                "created_at": "2026-10-20T01:02:03Z", "recorded_at": "2026-10-20T01:02:03Z",
                "actor": "manager",
            }
            missing = root / "missing" / "requested.md"
            self.assertEqual(export_record(snapshot, missing), {
                "status": "unavailable", "reason": "output_unavailable", "path": str(missing),
            })
            self.assertFalse(missing.parent.exists())

            denied = root / "denied"
            denied.mkdir()
            target = denied / "requested.md"
            real_open = os.open

            def deny_parent(path, flags, *args, **kwargs):
                if os.fspath(path) == os.fspath(denied):
                    raise PermissionError("injected permission failure")
                return real_open(path, flags, *args, **kwargs)

            with patch("workbench.storage.work_records.exporter.os.open", side_effect=deny_parent):
                result = export_record(snapshot, target)

            self.assertEqual(result, {
                "status": "unavailable", "reason": "output_unavailable", "path": str(target),
            })
            self.assertFalse(target.exists())

    def test_file_close_failure_is_reported_without_claiming_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "close-failure.md"
            snapshot = {
                "record_id": "11111111-1111-4111-8111-111111111111", "revision": 1,
                "title": "close", "body": "content", "task_id": None,
                "created_at": "2026-10-20T01:02:03Z", "recorded_at": "2026-10-20T01:02:03Z",
                "actor": "manager",
            }
            actual_close = os.close
            failed = False

            def close_then_report_error(fd):
                nonlocal failed
                actual_close(fd)
                if not failed:
                    failed = True
                    raise OSError("injected close report failure")

            with patch("workbench.storage.work_records.exporter.os.close",
                       side_effect=close_then_report_error):
                result = export_record(snapshot, destination)

            self.assertEqual(result, {
                "status": "unavailable", "reason": "output_cleanup_unknown", "path": str(destination),
            })
            self.assertEqual(destination.read_bytes()[-len("content"):], b"content")
