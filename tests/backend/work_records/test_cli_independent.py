"""Actual CLI subprocess checks independent of worker fixtures and backend startup."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from workbench.storage.work_records.store import RecordStore


class IndependentCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wrcli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.marker = self.root / "model-started"
        self.trap = self.root / "omp"
        self.trap.write_text('#!/bin/sh\nprintf x >> "$MODEL_TRAP_MARKER"\nexit 97\n')
        self.trap.chmod(0o700)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("WORKBENCH_", "OMP_"))}
        self.env.update(HOME=str(self.home), XDG_RUNTIME_DIR=str(self.root),
                        XDG_CONFIG_HOME=str(self.home / "config"), XDG_STATE_HOME=str(self.home / "state"),
                        PYTHONPATH=str(Path(__file__).resolve().parents[3] / "src"),
                        PYTHONDONTWRITEBYTECODE="1", WORKBENCH_OMP=str(self.trap), WORKBENCH_HUB_AUTOSTART="0",
                        MODEL_TRAP_MARKER=str(self.marker))

    def project(self, path, title):
        path.mkdir(mode=0o700, parents=True)
        data = path / ".wb"
        data.mkdir(mode=0o700)
        marker = data / "project.json"
        marker.write_text(json.dumps({"schema_version": 1, "project_id": str(uuid4()),
                                    "project_root": str(path), "state": "completed"}))
        marker.chmod(0o600)
        record = RecordStore(data / "records.sqlite3").create(title=title, body=f"body {title}", actor="manager")["record"]
        return data, record

    def run_cli(self, cwd, *args, data_env=None):
        env = dict(self.env)
        if data_env is not None:
            env["WORKBENCH_DATA_DIR"] = str(data_env)
        proc = subprocess.run([sys.executable, "-m", "workbench", "records", *args],
                              cwd=cwd, env=env, capture_output=True, text=True, timeout=15)
        return proc

    def assert_idle(self):
        self.assertFalse(self.marker.exists())
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertFalse(any(p.name in {"backend.json", "bridge.sock", "tasks.sqlite3"}
                             for p in self.root.rglob("*")))
        self.assertFalse((self.root / "omp-workbench").exists())

    def test_nested_nearest_project_and_environment_flag_precedence(self):
        outer = self.root / "outer"
        outer_data, _ = self.project(outer, "outer-canary")
        inner = outer / "inner"
        inner_data, _ = self.project(inner, "inner-canary")
        cwd = inner / "src" / "nested"
        cwd.mkdir(parents=True)
        cases = [((), None, "inner-canary"),
                 ((), outer_data, "outer-canary"),
                 (("--data-dir", str(inner_data)), outer_data, "inner-canary")]
        before = {str(p): p.read_bytes() for p in (outer_data / "records.sqlite3", inner_data / "records.sqlite3")}
        for flags, env, expected in cases:
            result = self.run_cli(cwd, "list", "--json", *flags, data_env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([r["title"] for r in json.loads(result.stdout)["records"]], [expected])
        self.assertEqual({p: Path(p).read_bytes() for p in before}, before)
        self.assert_idle()

    def test_search_pagination_selected_revision_and_failure_exit_json(self):
        cwd = self.root / "project"
        data, first = self.project(cwd, "old-only")
        store = RecordStore(data / "records.sqlite3")
        store.revise(first["record_id"], expected_revision=1, title="new", body="literal %_ ' OR 1=1 --", actor="worker")
        store.create(title="new second", body="literal %_", actor="manager")
        before = (data / "records.sqlite3").read_bytes()
        search = self.run_cli(cwd, "search", "%_", "--limit", "1", "--json")
        self.assertEqual(search.returncode, 0, search.stderr)
        page = json.loads(search.stdout)
        self.assertTrue(page["has_more"])
        other = self.run_cli(cwd, "search", "%_", "--page", page["next_after"], "--limit", "1", "--json")
        self.assertEqual(other.returncode, 0, other.stderr)
        rows = page["records"] + json.loads(other.stdout)["records"]
        self.assertEqual(len({r["record_id"] for r in rows}), 2)
        old = self.run_cli(cwd, "show", first["record_id"], "--revision", "1", "--json")
        self.assertEqual(old.returncode, 0, old.stderr)
        self.assertEqual(json.loads(old.stdout)["record"]["body"], "body old-only")
        empty = self.run_cli(cwd, "search", "old-only", "--json")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(json.loads(empty.stdout)["records"], [])
        failures = [(("list", "--limit", "101"), "rejected"),
                    (("list", "--page", "../../bad"), "rejected"),
                    (("show", str(uuid4())), "not_found"),
                    (("show", first["record_id"], "--revision", "99"), "not_found")]
        for args, status in failures:
            result = self.run_cli(cwd, *args, "--json")
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual(json.loads(result.stdout)["status"], status)
        usage = self.run_cli(cwd, "search", "--json")
        self.assertEqual(usage.returncode, 2)
        self.assertEqual((data / "records.sqlite3").read_bytes(), before)
        self.assert_idle()

    def test_corrupt_database_is_nonzero_json_without_repair(self):
        cwd = self.root / "project"
        data, record = self.project(cwd, "valid")
        db = data / "records.sqlite3"
        db.write_bytes(b"invalid existing database bytes")
        for args in (("list",), ("search", "anything"), ("show", record["record_id"]),
                     ("history", record["record_id"])):
            result = self.run_cli(cwd, *args, "--json")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["status"], "unavailable")
            self.assertEqual(db.read_bytes(), b"invalid existing database bytes")
        self.assert_idle()
