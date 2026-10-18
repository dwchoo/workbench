"""Hosted Python CLI over owned stores; no Backend/model or rollout proof."""

import base64
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from workbench.storage.important_log import ImportantLogCatalog, ImportantLogReader, ImportantLogStore
from workbench.storage.log_raw.port import DisabledSegmentedRawPort, raw_usage
from workbench.storage.log_raw.segments import SegmentedRawStore


REPOSITORY = Path(__file__).resolve().parents[2]
HOSTED_CLI = """
from contextlib import ExitStack
import sys
from tests.backend.test_storage_status_cli_independent import observational_only
from workbench.backend.cli import main
with ExitStack() as stack:
    observational_only(stack)
    raise SystemExit(main(sys.argv[1:]))
"""


def file_manifest(root):
    """Observe all owned fixture files, including catalog/WAL/config, not just raw."""
    return {str(path.relative_to(root)): (path.stat().st_mode & 0o777, sha256(path.read_bytes()).hexdigest())
            for path in root.rglob("*") if path.is_file()}


class IndependentStorageOperationsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="lso-store-")
        self.addCleanup(temp.cleanup)
        self.private = Path(temp.name)
        self.root = self.private / "data"
        for name in ("home", "tmp"):
            (self.private / name).mkdir(mode=0o700)
        self.environment = {"HOME": str(self.private / "home"), "TMPDIR": str(self.private / "tmp"),
                            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                            "PYTHONPATH": os.pathsep.join((str(REPOSITORY / "src"), str(REPOSITORY)))}

    def cli(self, *args, expected_code=0):
        result = subprocess.run([sys.executable, "-c", HOSTED_CLI, "logs", *args,
                                 "--data-dir", str(self.root)], cwd=REPOSITORY,
                                env=self.environment, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result.stdout, result.stderr

    def test_absent_stores_remain_absent_after_human_and_json_queries(self):
        before = file_manifest(self.private)
        raw, _ = self.cli("raw-status")
        self.assertIn("미생성", raw)
        raw_json, _ = self.cli("raw-status", "--json")
        self.assertEqual(json.loads(raw_json)["segmented_v1"], {"state": "not_allocated"})
        self.assertEqual(json.loads(raw_json)["rotation"]["newSourcePolicy"], "legacy-stop")
        important, _ = self.cli("important", "status", expected_code=1)
        self.assertIn("물리 크기: 미확인", important)
        important_json, _ = self.cli("important", "status", "--json", expected_code=1)
        self.assertFalse(json.loads(important_json)["complete"])
        self.assertFalse(self.root.exists())
        self.assertEqual(file_manifest(self.private), before)

    def test_mixed_real_stores_cli_reports_actual_budget_roles_and_preserves_all_files(self):
        self.root.mkdir(mode=0o700)
        legacy = self.root / "raw-logs"
        terminal = self.root / "workflow" / "terminal"
        run_id, command_id, task_id = str(uuid4()), str(uuid4()), str(uuid4())
        run = self.root / "workflow" / "runs" / run_id
        for directory in (legacy, terminal, run):
            directory.mkdir(parents=True, mode=0o700)
        for directory in (self.root / "workflow", self.root / "workflow" / "runs"):
            directory.chmod(0o700)
        for path, body in ((legacy / (str(uuid4()) + ".log"), b"legacy-run-kept"),
                           (terminal / (str(uuid4()) + ".log"), b"legacy-command-kept")):
            path.write_bytes(body)
            path.chmod(0o600)
        subject = {"schemaVersion": 1, "kind": "task", "id": task_id}
        catalog = ImportantLogCatalog(self.root / "important-logs", create=True,
                                      target_bytes=36_000, low_water_bytes=30_000)
        body = {"schemaVersion": 1, "facts": ["private-summary-sentinel"],
                "interpretations": [], "unknowns": []}
        with SegmentedRawStore(self.root / "raw-v2", segment_limit=1024, segment_count=2) as store:
            port = DisabledSegmentedRawPort(store)
            _, task = port.prepare_task(run, task_id, 2, run_id)
            _, command = port.prepare_command(terminal, command_id)
            start_bytes = b"task-result-and-required-evidence"
            self.assertEqual(port.append(task, start_bytes)["stored_bytes"], len(start_bytes))
            span = {"schemaVersion": 1, "source": task["source"], "start": 0,
                    "end": len(start_bytes), "digest": sha256(start_bytes).hexdigest()}
            saved = catalog.append(subject, "start", [span], body)
            self.assertEqual(saved["state"], "ok")
            self.assertIsNotNone(saved["rollover"])
            for _ in range(10):
                self.assertEqual(port.append(task, b"T" * 400)["stored_bytes"], 400)
                self.assertEqual(port.append(command, b"C" * 400)["stored_bytes"], 400)
            gap = port.read(task, 0, len(start_bytes))["read"]["missing"]
            self.assertEqual([(item["start"], item["end"], item["reason"]) for item in gap],
                             [(0, len(start_bytes), "evicted")])
            tail = port.append(command, b"last-command-output")
            self.assertEqual(tail["stored_bytes"], len(b"last-command-output"))
        # A real valid DB outside the pointer is an orphan, never a disposable archive.
        with ImportantLogStore.create(self.root / "important-logs") as orphan_store:
            orphan = orphan_store.db_uuid
        expected_raw = raw_usage(self.root)
        expected_important = ImportantLogReader(self.root / "important-logs").status()
        before = file_manifest(self.private)
        raw, _ = self.cli("raw-status")
        self.assertIn("2,048 B", raw)
        self.assertIn(f"metadata: {expected_raw['segmented_v1']['metadata_bytes']:,} B", raw)
        raw_json, _ = self.cli("raw-status", "--json")
        actual_raw = json.loads(raw_json)
        self.assertEqual(actual_raw["legacy"], expected_raw["legacy"])
        self.assertEqual(actual_raw["segmented_v1"], expected_raw["segmented_v1"])
        important, _ = self.cli("important", "status")
        self.assertIn("role=active", important)
        self.assertIn("role=archive", important)
        self.assertIn(f"DB {orphan} · role=orphan", important)
        self.assertIn("orphan은 확인 불가", important)
        self.assertIn("여러 중요 로그 DB", important)
        important_json, _ = self.cli("important", "status", "--json")
        self.assertEqual(json.loads(important_json), {**expected_important, "compatibility": None})
        self.assertNotIn("private-summary-sentinel", raw + important + raw_json + important_json)
        self.assertEqual(file_manifest(self.private), before, "status must preserve every owned file")
        # Reads expose exact source/range and retained summary without replaying a producer.
        shown, _ = self.cli("important", "show", "--task", task_id, "--json")
        entry = json.loads(shown)["entries"][0]["entry"]
        self.assertEqual(entry, saved["entry"])
        read, _ = self.cli("raw", "--command", command_id, "--json")
        chunks = json.loads(read)["read"]["chunks"]
        self.assertTrue(base64.b64decode(chunks[-1]["dataBase64"]).endswith(b"last-command-output"))
        after = file_manifest(self.private)
        active_shm = "data/important-logs/" + expected_important["active"] + ".sqlite3-shm"
        active_wal = "data/important-logs/" + expected_important["active"] + ".sqlite3-wal"
        if active_wal not in before and active_wal in after:
            self.assertEqual(after.pop(active_wal), (0o600, sha256(b"").hexdigest()),
                             "an absent active WAL may only appear as an empty private read sidecar")
        self.assertEqual({name: value for name, value in after.items() if name != active_shm},
                         {name: value for name, value in before.items() if name != active_shm},
                         "show permits active SHM read marks, never DB/WAL/config/raw changes")

    def test_busy_owned_writer_and_catalog_report_unavailable_without_recovery(self):
        self.root.mkdir(mode=0o700)
        catalog = ImportantLogCatalog(self.root / "important-logs", create=True)
        with SegmentedRawStore(self.root / "raw-v2", segment_limit=1024, segment_count=2):
            before = file_manifest(self.private)
            raw, _ = self.cli("raw-status")
            self.assertIn("unavailable", raw)
            self.assertIn("raw 사용량: 미확인", raw)
            raw_json, _ = self.cli("raw-status", "--json")
            self.assertEqual(json.loads(raw_json)["segmented_v1"]["state"], "unavailable")
            self.assertEqual(file_manifest(self.private), before)
        with (catalog.directory / "maintenance.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = file_manifest(self.private)
            important, error = self.cli("important", "status", expected_code=1)
            self.assertIn("미확인", important)
            self.assertIn("busy", error)
            result, _ = self.cli("important", "status", "--json", expected_code=1)
            self.assertFalse(json.loads(result)["complete"])
            self.assertEqual(file_manifest(self.private), before)


if __name__ == "__main__":
    unittest.main()
