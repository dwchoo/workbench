"""Read-only records commands use the caller's data-dir without starting WB."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RecordsCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wb-records-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir(mode=0o700)
        self.data = self.project / "explicit-data"
        self.data.mkdir(mode=0o700)
        self.marker = self.root / "omp-started"
        self.trap = self.root / "omp-trap"
        self.trap.write_text('#!/bin/sh\nprintf x >> "$WB_TEST_OMP_MARKER"\nexit 1\n')
        self.trap.chmod(0o700)
        self.source = Path(__file__).resolve().parents[3] / "src"

    def run_wb(self, cwd, *arguments):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.source) + os.pathsep + environment.get("PYTHONPATH", "")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["WORKBENCH_OMP"] = str(self.trap)
        environment["WB_TEST_OMP_MARKER"] = str(self.marker)
        return subprocess.run([sys.executable, "-m", "workbench", *arguments], cwd=cwd,
                              env=environment, text=True, capture_output=True, timeout=15)

    def make_project(self, name, title, body):
        from uuid import uuid4
        from workbench.storage.work_records.store import RecordStore

        project = self.root / name
        project.mkdir(mode=0o700)
        data = project / ".wb"
        data.mkdir(mode=0o700)
        marker = {"schema_version": 1, "project_id": str(uuid4()),
                  "project_root": str(project.resolve()), "state": "completed"}
        marker_path = data / "project.json"
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        marker_path.chmod(0o600)
        RecordStore(data / "records.sqlite3").create(title=title, body=body, actor="manager")
        return project, data

    def test_explicit_empty_data_dir_lists_without_creating_db_or_starting_backend(self):
        result = self.run_wb(self.project, "records", "list", "--data-dir", "explicit-data", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "status": "ok", "records": [], "has_more": False, "next_after": None,
        })
        self.assertFalse((self.data / "records.sqlite3").exists())
        self.assertFalse((self.data / "backend.json").exists())
        self.assertFalse(self.marker.exists())

    def test_nearest_project_and_explicit_data_dir_keep_two_projects_separate(self):
        project_a, _data_a = self.make_project("project-a", "sentinel-A", "body-A")
        project_b, data_b = self.make_project("project-b", "sentinel-B", "body-B")
        nested = project_a / "src" / "nested"
        nested.mkdir(parents=True, mode=0o700)

        nearest = self.run_wb(nested, "records", "list", "--json")
        explicit = self.run_wb(project_a, "records", "list", "--data-dir", str(data_b), "--json")

        self.assertEqual(nearest.returncode, 0, nearest.stderr)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual([row["title"] for row in json.loads(nearest.stdout)["records"]], ["sentinel-A"])
        self.assertEqual([row["title"] for row in json.loads(explicit.stdout)["records"]], ["sentinel-B"])
        self.assertFalse((project_a / ".wb" / "backend.json").exists())
        self.assertFalse((project_b / ".wb" / "backend.json").exists())
        self.assertFalse(self.marker.exists())

    def test_uninitialized_default_project_stays_absent_and_reports_domain_status(self):
        result = self.run_wb(self.project, "records", "list", "--json")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {
            "status": "unavailable", "reason": "project_not_initialized",
        })
        self.assertFalse((self.project / ".wb").exists())
        self.assertFalse(self.marker.exists())

    def test_invalid_private_data_root_stays_unavailable_in_json(self):
        self.data.chmod(0o755)

        result = self.run_wb(self.project, "records", "list", "--data-dir", "explicit-data", "--json")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {
            "status": "unavailable", "reason": "store_unavailable",
        })
        self.assertFalse(self.marker.exists())

    def test_show_returns_full_selected_body_and_history_returns_metadata(self):
        from uuid import uuid4
        from workbench.storage.work_records.store import RecordStore

        data = self.project / ".wb"
        data.mkdir(mode=0o700)
        marker = {"schema_version": 1, "project_id": str(uuid4()),
                  "project_root": str(self.project.resolve()), "state": "completed"}
        marker_path = data / "project.json"
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        marker_path.chmod(0o600)
        store = RecordStore(data / "records.sqlite3")
        first = store.create(title="초안", body="# 원문\n한글 😀 %_\n", actor="manager")["record"]
        latest = store.revise(first["record_id"], expected_revision=1,
                              title="확정", body="전체 본문\n", actor="worker")["record"]

        shown = self.run_wb(self.project, "records", "show", first["record_id"], "--json")
        history = self.run_wb(self.project, "records", "history", first["record_id"], "--json")

        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["record"]["body"], "전체 본문\n")
        self.assertEqual(json.loads(shown.stdout)["record"]["revision"], latest["revision"])
        self.assertEqual(history.returncode, 0, history.stderr)
        revisions = json.loads(history.stdout)["revisions"]
        self.assertEqual([row["revision"] for row in revisions], [2, 1])
        self.assertEqual([row["title"] for row in revisions], ["확정", "초안"])
        self.assertTrue(all("body" not in row for row in revisions))
        self.assertFalse((data / "backend.json").exists())
        self.assertFalse(self.marker.exists())

    def test_logically_corrupt_snapshot_returns_unavailable_json_without_repair(self):
        import sqlite3

        project, data = self.make_project("logical-corruption", "valid title", "valid body")
        database = data / "records.sqlite3"
        with sqlite3.connect(database) as connection:
            record_id = connection.execute("SELECT record_id FROM records").fetchone()[0]
            connection.execute(
                """INSERT INTO revisions
                   (record_id,revision,title,body,task_id,recorded_at,actor)
                   VALUES(?,?,?,?,?,?,?)""",
                (record_id, 2, "damaged snapshot", sqlite3.Binary(b"invalid UTF-8 body"), None,
                 "2026-10-20T01:02:03Z", "worker"),
            )
        database_before = database.read_bytes()

        result = self.run_wb(project, "records", "list", "--json")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {
            "status": "unavailable", "reason": "store_unavailable",
        })
        self.assertEqual(result.stderr, "")
        self.assertEqual(database.read_bytes(), database_before)
        self.assertFalse(self.marker.exists())
