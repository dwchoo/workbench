"""Public migration compatibility checks against historical source shapes."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


def inventory(root):
    result = {}
    for path in (root, *sorted(root.rglob("*"))):
        info = path.lstat()
        item = {"mode": stat.S_IMODE(info.st_mode), "size": info.st_size, "mtime_ns": info.st_mtime_ns}
        if stat.S_ISREG(info.st_mode):
            item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(info.st_mode):
            item["link"] = os.readlink(path)
        result[str(path.relative_to(root))] = item
    return result


class MigrationCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wi-migrate-compat-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "old-state"
        self.source.mkdir(mode=0o700)
        self.project = self.base / "research"
        self.project.mkdir()
        home = self.base / "home"
        home.mkdir(mode=0o700)
        config = self.base / "config"
        config.mkdir(mode=0o700)
        cache = self.base / "cache"
        cache.mkdir(mode=0o700)
        data = self.base / "data"
        data.mkdir(mode=0o700)
        runtime = self.base / "runtime"
        runtime.mkdir(mode=0o700)
        temp = self.base / "tmp"
        temp.mkdir(mode=0o700)
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(temp),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(cache),
            "XDG_DATA_HOME": str(data),
            "XDG_RUNTIME_DIR": str(runtime),
            "WORKBENCH_HUB_AUTOSTART": "0",
        }
        from workbench.backend.boot import read_boot_id
        from workbench.tasks.repository import TaskRepository

        self.boot_id = read_boot_id()
        self.database = self.source / "tasks.sqlite3"
        repository = TaskRepository(self.database)
        repository.create_task({"title": "kept historical task"}, task_id="task-fixed")
        repository.approve_scope("task-fixed", 1, {"command": "retain approved history"})
        repository.proceed("task-fixed", 1, "retain proceed decision")
        self.run_id = repository.start_run(
            "task-fixed", 1, inputs={"worktree_path": "/external/original"}
        )
        self.child = {"child_pid": 2_000_000_099, "child_start_ticks": 91}
        repository.record_shell_event(self.run_id, "started", self.child)
        repository.close()
        self.database.chmod(0o600)
        self._write_old_backend_record()
        (self.source / "backend.lock").touch(mode=0o600)

    def _write_backend_record(self, value):
        record_path = self.source / "backend.json"
        record_path.write_text(json.dumps(value))
        record_path.chmod(0o600)

    def _write_old_backend_record(self, project=None):
        # This is the shape emitted by Backend._record at the assignment's old HEAD.
        # Newer uid/runtime/project/socket fields are intentionally absent.
        project = project or self.project
        value = {
            "contract": {"name": "workbench.ui", "version": 1},
            "phase": "stopped",
            "reason": None,
            "data_dir": str(self.source),
            "ui_socket": str(self.source / "ui.sock"),
            "project_dir": str(project),
            "boot_id": self.boot_id,
            "omp_version": "fixture-legacy",
            "shell": {"kind": "bash", "executable": "/bin/bash"},
            "omp_isolation": {
                "state": "ok", "ok": True, "leaks": [], "warnings": [],
                "warning": None, "version_drift": False, "notes": [],
            },
            "processes": {
                name: {"pid": 2_000_000_000 + index, "start_ticks": 71 + index,
                       "owner_epoch": 1, "role": name}
                for index, name in enumerate(("backend", "manager_omp", "worker_omp", "host_shell"))
            },
            "restarts": [],
            "kills": [],
            "survivors": [],
            "survivor_stops": [],
            "startup": None,
        }
        self._write_backend_record(value)

    def migrate(self, project=None):
        project = project or self.project
        return subprocess.run(
            [sys.executable, "-m", "workbench", "migrate", "--from", str(self.source),
             "--project", str(project)],
            cwd=project,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_previous_producer_record_without_uid_migrates_active_task_history(self):
        old_record = json.loads((self.source / "backend.json").read_text())
        self.assertFalse(
            {"uid", "runtime_root", "project_id", "bridge_socket", "omp_runtime_root"}
            & old_record.keys()
        )
        invalid_uids = (None, os.geteuid() + 1, True, "current")
        for index, uid in enumerate(invalid_uids):
            project = self.base / f"bad-uid-{index}"
            project.mkdir()
            record = json.loads((self.source / "backend.json").read_text())
            record["project_dir"] = str(project)
            record["uid"] = uid
            self._write_backend_record(record)
            before_bad_uid = inventory(self.source)
            with self.subTest(uid=uid):
                result = self.migrate(project)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("owner_or_boot_unknown", result.stderr)
                self.assertEqual(inventory(self.source), before_bad_uid)
                self.assertFalse((project / ".wb/project.json").exists())

        for label, field, start_delta in (
            ("live", "manager_omp", 0),
            ("unknown", "worker_omp", 1),
        ):
            project = self.base / f"{label}-writer"
            project.mkdir()
            self._write_old_backend_record(project)
            record = json.loads((self.source / "backend.json").read_text())
            current_pid = os.getpid()
            fields = Path(f"/proc/{current_pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
            current_ticks = int(fields[19])
            record["processes"][field] = {
                "pid": current_pid,
                "start_ticks": current_ticks + start_delta,
                "owner_epoch": 1,
                "role": field,
            }
            self._write_backend_record(record)
            before_writer = inventory(self.source)
            with self.subTest(writer=label):
                result = self.migrate(project)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                expected_reason = (
                    "writer manager_omp is alive" if label == "live" else "writer worker_omp is unknown"
                )
                self.assertIn(expected_reason, result.stderr)
                self.assertEqual(inventory(self.source), before_writer)
                self.assertFalse((project / ".wb/project.json").exists())

        self._write_old_backend_record()
        before = inventory(self.source)
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(inventory(self.source), before)

        target = self.project / ".wb"
        self.assertTrue((target / "project.json").is_file())
        from workbench.tasks.repository import TaskRepository

        reader = TaskRepository(target / "tasks.sqlite3")
        try:
            self.assertEqual(
                reader.get_task_spec("task-fixed", 1)["spec"],
                {"title": "kept historical task"},
            )
            self.assertEqual(reader.get_shell_history(self.run_id)[0]["details"], self.child)
        finally:
            reader.close()

    def test_changed_same_name_immutable_trigger_blocks_completion(self):
        import sqlite3

        record = json.loads((self.source / "backend.json").read_text())
        record["uid"] = os.geteuid()
        self._write_backend_record(record)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TRIGGER task_specs_immutable_update")
            connection.execute(
                "CREATE TRIGGER task_specs_immutable_update BEFORE UPDATE ON task_specs BEGIN SELECT 1; END"
            )
            connection.commit()
        finally:
            connection.close()

        before = inventory(self.source)
        result = self.migrate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(inventory(self.source), before)
        self.assertFalse((self.project / ".wb/project.json").exists())

    def test_changed_check_constraint_with_same_table_info_blocks_completion(self):
        import sqlite3

        record = json.loads((self.source / "backend.json").read_text())
        record["uid"] = os.geteuid()
        self._write_backend_record(record)
        connection = sqlite3.connect(self.database)
        try:
            original = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='delivery_attempts'"
            ).fetchone()[0]
            changed = original.replace("CHECK (attempt_number > 0)", "CHECK (attempt_number >= 0)")
            self.assertNotEqual(changed, original)
            columns = connection.execute("PRAGMA table_info(delivery_attempts)").fetchall()
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='delivery_attempts'",
                (changed,),
            )
            schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
            connection.execute(f"PRAGMA schema_version={schema_version + 1}")
            connection.execute("PRAGMA writable_schema=OFF")
            connection.commit()
        finally:
            connection.close()

        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='delivery_attempts'"
                ).fetchone()[0],
                changed,
            )
            self.assertEqual(connection.execute("PRAGMA table_info(delivery_attempts)").fetchall(), columns)
        finally:
            connection.close()

        before = inventory(self.source)
        result = self.migrate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(inventory(self.source), before)
        self.assertFalse((self.project / ".wb/project.json").exists())

    def test_user_schema_name_starting_with_sqlitex_is_not_internal(self):
        import sqlite3

        record = json.loads((self.source / "backend.json").read_text())
        record["uid"] = os.geteuid()
        self._write_backend_record(record)
        connection = sqlite3.connect(self.database)
        try:
            columns = connection.execute("PRAGMA table_info(tasks)").fetchall()
            connection.execute(
                "CREATE TRIGGER sqlitex_extra BEFORE UPDATE ON tasks BEGIN SELECT 1; END"
            )
            self.assertEqual(connection.execute("PRAGMA table_info(tasks)").fetchall(), columns)
            connection.commit()
        finally:
            connection.close()

        before = inventory(self.source)
        result = self.migrate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(inventory(self.source), before)
        self.assertFalse((self.project / ".wb/project.json").exists())
