"""Independent DVL-12 source honesty and legacy isolation boundaries."""

import base64
from hashlib import sha256
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from uuid import uuid4

from workbench.storage.log_raw.port import DisabledSegmentedRawPort, raw_query, raw_usage
from workbench.storage.log_raw.segments import SegmentedRawStore
from workbench.storage.log_raw.store import RawLogStore


class Dvl12SourceBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dvl12-independent-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run_id = str(uuid4())
        (self.root / "workflow").mkdir(mode=0o700)
        (self.root / "workflow" / "runs").mkdir(mode=0o700)
        self.artifact = self.root / "workflow" / "runs" / self.run_id
        self.artifact.mkdir(mode=0o700)

    def _run_record(self, value):
        path = self.artifact / "run.json"
        path.write_text(json.dumps({"run_id": self.run_id, **value}))
        path.chmod(0o600)

    def test_legacy_file_bytes_and_separate_usage_survive_disabled_v2_activity(self):
        original = b"legacy output\x00kept\n"
        with RawLogStore(self.root / "raw-logs") as legacy:
            legacy.append(self.run_id, original)
        path = self.root / "raw-logs" / f"{self.run_id}.log"
        original_hash = sha256(path.read_bytes()).hexdigest()
        self._run_record({"raw_log": str(path)})
        store = SegmentedRawStore(self.root / "raw-v2", segment_limit=512, segment_count=2)
        self.addCleanup(store.close)
        port = DisabledSegmentedRawPort(store)
        another_run = str(uuid4())
        another_artifact = self.root / "workflow" / "runs" / another_run
        another_artifact.mkdir(mode=0o700)
        _, descriptor = port.prepare_task(another_artifact, str(uuid4()), 1, another_run)
        self.assertEqual(port.append(descriptor, b"new-v2-output")["stored_bytes"], len(b"new-v2-output"))

        legacy_read = raw_query(self.root, "run", self.run_id, limit=128)
        self.assertEqual(legacy_read["log_format"], "legacy-file")
        self.assertEqual(base64.b64decode(legacy_read["read"]["dataBase64"]), original)
        usage = raw_usage(self.root, segmented=port)
        self.assertEqual(usage["activation"], "disabled")
        self.assertEqual(usage["legacy"]["run_bytes"], len(original))
        self.assertEqual(usage["legacy"]["run_limit_bytes"], 64 * 1024 * 1024)
        self.assertEqual(usage["legacy"]["project_limit_bytes"], 512 * 1024 * 1024)
        self.assertEqual(usage["legacy"]["terminal_limit_bytes"], 64 * 1024 * 1024)
        self.assertGreater(usage["segmented_v1"]["raw_bytes"], 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(sha256(path.read_bytes()).hexdigest(), original_hash)

    def test_legacy_record_cannot_forward_an_arbitrary_private_path(self):
        unrelated = self.root / "private-user-file"
        unrelated.write_bytes(b"do not expose")
        unrelated.chmod(0o600)
        self._run_record({"raw_log": str(unrelated)})
        with self.assertRaisesRegex(ValueError, "not a known source"):
            raw_query(self.root, "run", self.run_id)

    def test_prepared_but_unobserved_source_is_not_reported_as_complete_output(self):
        store = SegmentedRawStore(self.root / "raw-v2", segment_limit=512, segment_count=2)
        self.addCleanup(store.close)
        port = DisabledSegmentedRawPort(store)
        _, descriptor = port.prepare_task(self.artifact, str(uuid4()), 1, self.run_id)
        self._run_record({"log_format": "segmented-v1", "raw_log_ref": descriptor["raw_log_ref"],
                          "raw_source": descriptor["source"]})
        result = raw_query(self.root, "run", self.run_id, segmented=port, limit=128)
        self.assertEqual(result["source_status"]["status"], "unknown")
        self.assertNotEqual(result["status"], "available", result)
        self.assertNotEqual(result["read"]["status"], "available", result)

    def test_confirmed_source_eof_is_an_available_empty_page(self):
        store = SegmentedRawStore(self.root / "raw-v2", segment_limit=512, segment_count=2)
        self.addCleanup(store.close)
        port = DisabledSegmentedRawPort(store)
        _, descriptor = port.prepare_task(self.artifact, str(uuid4()), 1, self.run_id)
        self._run_record({"log_format": "segmented-v1", "raw_log_ref": descriptor["raw_log_ref"],
                          "raw_source": descriptor["source"]})
        self.assertEqual(port.append(descriptor, b"known")["stored_bytes"], 5)
        result = raw_query(self.root, "run", self.run_id, segmented=port, offset=5, limit=128)
        self.assertEqual(result["source_status"]["status"], "available")
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["read"]["status"], "available")
        self.assertEqual(result["read"]["chunks"], [])
        self.assertEqual(result["read"]["missing"], [])
        self.assertIsNone(result["next_offset"])

    def test_returned_commands_read_run_and_command_from_quoted_private_root(self):
        # The command is executed only after its parsed argv selects this exact fixture root.
        private = self.root / "private data 'x'; touch PWNED; $(touch PWNED2)"
        private.mkdir(mode=0o700)
        (private / "workflow").mkdir(mode=0o700)
        (private / "workflow" / "runs").mkdir(mode=0o700)
        artifact = private / "workflow" / "runs" / self.run_id
        artifact.mkdir(mode=0o700)
        terminal = private / "workflow" / "terminal"
        terminal.mkdir(mode=0o700)
        command_id = str(uuid4())
        store = SegmentedRawStore(private / "raw-v2", segment_limit=512, segment_count=2)
        port = DisabledSegmentedRawPort(store)
        _, run_descriptor = port.prepare_task(artifact, str(uuid4()), 1, self.run_id)
        record = {"run_id": self.run_id, "log_format": "segmented-v1",
                  "raw_log_ref": run_descriptor["raw_log_ref"], "raw_source": run_descriptor["source"]}
        record_path = artifact / "run.json"
        record_path.write_text(json.dumps(record))
        record_path.chmod(0o600)
        _, command_descriptor = port.prepare_command(terminal, command_id)
        self.assertEqual(port.append(run_descriptor, b"DVL12-PRIVATE-RUN")["stored_bytes"], 17)
        self.assertEqual(port.append(command_descriptor, b"DVL12-PRIVATE-COMMAND")["stored_bytes"], 21)
        store.close()

        launcher = self.root / "bin"
        launcher.mkdir(mode=0o700)
        executable = launcher / "omp-workbench"
        executable.write_text("#!/usr/bin/env python3\nfrom workbench.backend.cli import main\n"
                              "raise SystemExit(main())\n")
        executable.chmod(0o700)
        project = Path(__file__).resolve().parents[2]
        environment = {"PATH": f"{launcher}:/usr/bin:/bin", "PYTHONPATH": str(project / "src"),
                       "HOME": str(self.root), "LANG": "C.UTF-8"}
        for kind, identity, payload in (("run", self.run_id, b"DVL12-PRIVATE-RUN"),
                                        ("command", command_id, b"DVL12-PRIVATE-COMMAND")):
            query = raw_query(private, kind, identity, limit=128)
            command = query["read_command"]
            expected = ["omp-workbench", "logs", "raw", f"--{kind}", identity,
                        "--data-dir", str(private)]
            self.assertEqual(shlex.split(command), expected, "refuse before shell on unexpected argv")
            completed = subprocess.run(["/usr/bin/bash", "-c", command], cwd=self.root,
                                       env=environment, capture_output=True, timeout=10, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(payload, completed.stdout)
        self.assertFalse((self.root / "PWNED").exists())
        self.assertFalse((self.root / "PWNED2").exists())


if __name__ == "__main__":
    unittest.main()
