"""Independent WR-04 boundary tests; expectations derive from approved SPEC s1."""
from pathlib import Path
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from workbench.storage.work_records.exporter import export_record
from workbench.storage.work_records.store import RecordStore


class IndependentExporterTests(unittest.TestCase):
    def snapshot(self):
        return {
            "record_id": "22222222-2222-4222-8222-222222222222", "revision": 3,
            "title": "독립 경계", "body": "한글 😀\r\n`본문` 끝", "task_id": None,
            "created_at": "2026-10-20T02:00:00Z",
            "recorded_at": "2026-10-20T03:00:00Z", "actor": "worker",
        }

    def test_positive_short_writes_complete_raw_body_under_restrictive_umask(self):
        # Real SQLite source, not a formatter-derived expected document.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "records.sqlite3"
            body = "줄바꿈\r\n\r\n```md\n# 원문 😀\n```\r\n끝  "
            store = RecordStore(database)
            saved = store.create(title="직접 정한 제목", body=body, actor="manager")
            self.assertEqual(saved["status"], "saved")
            snapshot = store.get(saved["record"]["record_id"])["record"]
            original_database = database.read_bytes()
            destination = root / "한글 출력.md"
            real_write = os.write
            write_sizes = []

            def short_write(fd, payload):
                count = real_write(fd, payload[:7])
                write_sizes.append(count)
                return count

            old_umask = os.umask(0o777)
            try:
                with patch("workbench.storage.work_records.exporter.os.write", side_effect=short_write):
                    result = export_record(snapshot, destination)
            finally:
                os.umask(old_umask)

            self.assertEqual(result, {
                "status": "exported", "path": str(destination),
                "record_id": snapshot["record_id"], "revision": 1,
            })
            self.assertGreater(len(write_sizes), 2)
            self.assertTrue(all(0 < count <= 7 for count in write_sizes))
            prefix, raw_body = destination.read_bytes().split(b"\n\n---\n\n", 1)
            self.assertEqual(raw_body, body.encode("utf-8"))
            self.assertIn("직접 정한 제목".encode(), prefix)
            self.assertIn(snapshot["record_id"].encode(), prefix)
            self.assertIn(snapshot["created_at"].encode(), prefix)
            self.assertNotIn(b"Task ID", prefix)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(database.read_bytes(), original_database)
            # Editing the one-way snapshot cannot write back to the record.
            destination.write_bytes(b"independently edited markdown")
            self.assertEqual(store.get(snapshot["record_id"])["record"]["body"], body)
            self.assertEqual(database.read_bytes(), original_database)

    def test_dangling_symlink_is_a_collision_without_creating_its_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing_target = root / "must-not-be-created.md"
            destination = root / "dangling.md"
            destination.symlink_to(missing_target)
            before = destination.lstat()
            result = export_record(self.snapshot(), destination)
            self.assertEqual(result, {
                "status": "unavailable", "reason": "output_exists", "path": str(destination),
            })
            self.assertTrue(destination.is_symlink())
            self.assertEqual(destination.lstat().st_ino, before.st_ino)
            self.assertEqual(destination.readlink(), missing_target)
            self.assertFalse(missing_target.exists())
            self.assertEqual(set(root.iterdir()), {destination})

    def test_zero_progress_write_fails_with_explicit_partial_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "no-progress.md"
            with patch("workbench.storage.work_records.exporter.os.write", return_value=0) as write:
                result = export_record(self.snapshot(), destination)
            self.assertEqual(write.call_count, 1)
            self.assertEqual(result, {
                "status": "unavailable", "reason": "output_cleanup_unknown", "path": str(destination),
            })
            # SPEC permits explicit cleanup uncertainty; deleting every partial is
            # not required when safe ownership at unlink cannot be guaranteed.
            self.assertEqual(destination.read_bytes(), b"")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(set(destination.parent.iterdir()), {destination})

    def test_replacement_after_successful_write_is_not_reported_exported_or_unlinked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "replaced.md"
            sentinel = root / "other-file.md"
            sentinel.write_bytes(b"independent external file\x00")
            real_fsync = os.fsync

            def replace_with_symlink(fd):
                real_fsync(fd)
                destination.unlink()
                destination.symlink_to(sentinel)

            with patch("workbench.storage.work_records.exporter.os.fsync", side_effect=replace_with_symlink):
                result = export_record(self.snapshot(), destination)
            self.assertEqual(result, {
                "status": "unavailable", "reason": "output_cleanup_unknown", "path": str(destination),
            })
            self.assertTrue(destination.is_symlink())
            self.assertEqual(destination.readlink(), sentinel)
            self.assertEqual(sentinel.read_bytes(), b"independent external file\x00")

    def test_interrupted_write_is_explicit_and_retains_the_reported_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "partial-remains.md"
            real_write = os.write
            calls = 0

            def write_then_fail(fd, payload):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_write(fd, b"owned partial output")
                raise OSError("injected write failure")

            with patch("workbench.storage.work_records.exporter.os.write", side_effect=write_then_fail):
                result = export_record(self.snapshot(), destination)
            self.assertEqual(result, {
                "status": "unavailable", "reason": "output_cleanup_unknown", "path": str(destination),
            })
            self.assertEqual(calls, 2)
            self.assertEqual(destination.read_bytes(), b"owned partial output")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(set(destination.parent.iterdir()), {destination})

    def test_relative_destination_is_rejected_before_filesystem_access(self):
        # Resolving a user's relative path belongs to the later CLI/bridge adapters.
        with patch("workbench.storage.work_records.exporter.os.open") as open_file:
            result = export_record(self.snapshot(), Path("caller-context.md"))
        self.assertEqual(result, {"status": "rejected", "reason": "invalid_output_path"})
        open_file.assert_not_called()

    def test_parent_regular_file_is_not_replaced_or_bypassed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "not-a-directory"
            parent.write_bytes(b"parent file sentinel")
            destination = parent / "requested.md"
            result = export_record(self.snapshot(), destination)
            self.assertEqual(result, {
                "status": "unavailable", "reason": "output_unavailable", "path": str(destination),
            })
            self.assertEqual(parent.read_bytes(), b"parent file sentinel")
            self.assertEqual(set(root.iterdir()), {parent})


if __name__ == "__main__":
    unittest.main()
