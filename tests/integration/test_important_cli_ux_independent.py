"""Owned real tiny catalog + parser/main subprocess, not Backend or model evidence."""
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from workbench.storage.important_log import ImportantLogCatalog, ImportantLogReader

REPOSITORY = Path(__file__).resolve().parents[2]
HOSTED = """
from contextlib import ExitStack
import sys
from tests.backend.test_storage_status_cli_independent import observational_only
from workbench.backend.cli import main
with ExitStack() as stack:
    observational_only(stack)
    raise SystemExit(main(sys.argv[1:]))
"""


def manifest(root):
    return {str(p.relative_to(root)): [stat.S_IMODE(p.stat().st_mode), sha256(p.read_bytes()).hexdigest()]
            for p in root.rglob("*") if p.is_file()}


class IndependentImportantCliRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.private = Path(tempfile.mkdtemp(prefix="iux-", dir="/tmp"))
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)
        self.identity = (self.private.stat().st_dev, self.private.stat().st_ino)
        self.root = self.private / "d"
        for name in ("h", "t"):
            (self.private / name).mkdir(mode=0o700)
        self.environment = {"HOME": str(self.private / "h"), "TMPDIR": str(self.private / "t"),
                            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                            "PYTHONPATH": os.pathsep.join((str(REPOSITORY / "src"), str(REPOSITORY)))}
        self.children = []
        self.calls = []

    def tearDown(self):
        self.assertTrue(all(child.poll() is not None for child in self.children))
        self.assertEqual((self.private.stat().st_dev, self.private.stat().st_ino), self.identity)
        shutil.rmtree(self.private)
        self.assertFalse(self.private.exists())
        evidence = os.environ.get("UX_INDEPENDENT_EVIDENCE")
        if evidence:
            directory = Path(evidence)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (self._testMethodName + ".json")).write_text(json.dumps({
                "calls": self.calls, "temp_root": str(self.private), "mode": "0700", "identity": self.identity,
                "removed": not self.private.exists(), "remaining_owned": [],
                "boundary": "actual tiny SQLite catalog/reader + hosted real parser/main, forbidden execution sentries; no Backend/model/auth"},
                ensure_ascii=False, indent=2))

    def cli(self, *args, code=0):
        # DataLayout enforces all actual socket paths; explicit precheck keeps the
        # whole absolute namespace below Linux sun_path, not only the basename.
        from workbench.backend.paths import DataLayout
        layout = DataLayout(self.root)
        layout.check_socket_paths()
        socket_paths = [str(layout.ui_socket), str(layout.bridge_socket)]
        self.assertTrue(all(len(value.encode()) <= 107 for value in socket_paths))
        argv = [sys.executable, "-c", HOSTED, "logs", "important", *args, "--data-dir", str(self.root)]
        child = subprocess.Popen(argv, cwd=REPOSITORY, env=self.environment, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(child)
        try:
            out, err = child.communicate(timeout=20)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        self.calls.append({"argv": argv, "environment": self.environment, "exit": child.returncode,
                           "pid": child.pid, "waited": True, "stdout": out, "stderr": err,
                           "stdout_sha256": sha256(out.encode()).hexdigest(), "stderr_sha256": sha256(err.encode()).hexdigest()})
        self.assertEqual(child.returncode, code, out + err)
        self.assertNotIn("Traceback", err)
        return out, err

    def test_actual_absent_catalogless_empty_and_flock_busy_status(self):
        before = manifest(self.private)
        out, err = self.cli("status", code=1)
        self.assertIn("경로가 없습니다", out)
        self.assertIn("처음 사용하는 환경이라면", out)
        data, err = self.cli("status", "--json", code=1)
        self.assertFalse(json.loads(data)["complete"])
        self.assertEqual(err, "")
        self.assertFalse(self.root.exists())
        self.assertEqual(manifest(self.private), before)
        (self.root / "important-logs").mkdir(parents=True, mode=0o700)
        self.root.chmod(0o700)
        before = manifest(self.private)
        out, _ = self.cli("status", code=1)
        self.assertIn("경로가 있지만", out)
        self.assertNotIn("처음 사용하는", out)
        self.assertEqual(manifest(self.private), before)
        catalog = ImportantLogCatalog(self.root / "important-logs", create=True)
        before = manifest(self.private)
        out, err = self.cli("status")
        self.assertIn("1개", out)
        self.assertEqual(err, "")
        data, _ = self.cli("status", "--json")
        self.assertEqual(json.loads(data), {**ImportantLogReader(catalog.directory).status(), "compatibility": None})
        self.assertEqual(manifest(self.private), before)
        with (catalog.directory / "maintenance.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out, err = self.cli("status", code=1)
            self.assertNotIn("처음 사용하는", out)
            self.assertIn("busy", err)
            self.assertIn("미확인", out)
        self.assertEqual(manifest(self.private), before)

    def test_actual_task_command_show_pages_json_and_preserved_db(self):
        self.root.mkdir(mode=0o700)
        catalog = ImportantLogCatalog(self.root / "important-logs", create=True)
        subjects = {kind: {"schemaVersion": 1, "kind": kind, "id": str(uuid4())} for kind in ("task", "command")}
        saved = {}
        for kind, subject in subjects.items():
            source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1}
            source.update({"taskId": subject["id"], "revision": 3, "runId": str(uuid4())} if kind == "task" else {"commandId": subject["id"]})
            span = {"schemaVersion": 1, "source": source, "start": 0, "end": 8, "digest": sha256(b"observed").hexdigest()}
            gap = {"schemaVersion": 1, "source": source, "start": 8, "end": 12, "reason": "storage_gap"}
            saved[kind] = []
            for number in range(3 if kind == "task" else 1):
                body = {"schemaVersion": 1, "facts": [f"{kind}-fact-{number}\n한국어 둘째 줄"],
                        "interpretations": [kind + "-interpretation"], "unknowns": [kind + "-unknown"]}
                item = catalog.append(subject, "cumulative", [span], body, source_gaps=[gap])
                self.assertEqual(item["state"], "ok", item)
                saved[kind].append(item["entry"])
        before = manifest(self.private)
        for kind in ("task", "command"):
            arguments = ["show", "--" + kind, subjects[kind]["id"], "--limit", "2"]
            expected = ImportantLogReader(catalog.directory).show(subjects[kind], limit=2)
            out, err = self.cli(*arguments)
            self.assertEqual(err, "")
            self.assertIn(kind + "-fact-", out)
            self.assertNotIn(("command" if kind == "task" else "task") + "-fact-", out)
            for text in (subjects[kind]["id"], "사실:", "해석:", "미확인:", "한국어 둘째 줄", "출처:",
                         "누락 범위:", "storage_gap", "[0, 8)", "[8, 12)", catalog.active_uuid()):
                self.assertIn(text, out)
            raw, err = self.cli(*arguments, "--json")
            self.assertEqual((json.loads(raw), err), (expected, ""))
            first_ids = [r["entry"]["entryId"] for r in expected["entries"]]
            for first, second in zip(first_ids, first_ids[1:]):
                self.assertLess(out.index(first), out.index(second))
            if kind == "task":
                cursor = expected["nextAfter"]
                self.assertTrue(cursor)
                self.assertIn("다음 page: " + cursor, out)
                next_raw, _ = self.cli(*arguments, "--page", cursor, "--json")
                next_data = json.loads(next_raw)
                second_ids = [r["entry"]["entryId"] for r in next_data["entries"]]
                self.assertFalse(set(first_ids) & set(second_ids))
                self.assertEqual(set(first_ids + second_ids), {item["entryId"] for item in saved[kind]})
        empty, err = self.cli("show", "--task", str(uuid4()))
        self.assertEqual((empty.strip(), err), ("기록 없음", ""))
        after = manifest(self.private)
        active = catalog.active_uuid()
        shm = "d/important-logs/" + active + ".sqlite3-shm"
        wal = "d/important-logs/" + active + ".sqlite3-wal"
        if wal not in before and wal in after:
            self.assertEqual(after.pop(wal), [0o600, sha256(b"").hexdigest()])
        self.assertEqual({k: v for k, v in before.items() if k != shm},
                         {k: v for k, v in after.items() if k != shm}, "only existing readonly sidecar rules, no DB/control mutation")


if __name__ == "__main__":
    unittest.main()
