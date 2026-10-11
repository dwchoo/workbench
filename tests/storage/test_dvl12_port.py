"""DVL-12 disabled source port and stopped read-only query boundaries."""

import base64
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from workbench.storage.log_raw.port import DisabledSegmentedRawPort, raw_query, raw_usage
from workbench.storage.log_raw.segments import SegmentedRawStore


class DisabledRawPortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task_id = str(uuid4())
        self.run_id = str(uuid4())
        (self.root / "workflow").mkdir(mode=0o700)
        (self.root / "workflow" / "runs").mkdir(mode=0o700)
        self.artifact = self.root / "workflow" / "runs" / self.run_id
        self.artifact.mkdir(mode=0o700)

    def _create(self, store):
        port = DisabledSegmentedRawPort(store)
        path, descriptor = port.prepare_task(self.artifact, self.task_id, 1, self.run_id)
        record = {"run_id": self.run_id, "log_format": "segmented-v1",
                  "raw_log_ref": descriptor["raw_log_ref"], "raw_source": descriptor["source"]}
        (self.artifact / "run.json").write_text(json.dumps(record))
        (self.artifact / "run.json").chmod(0o600)
        return port, path, descriptor

    def test_descriptor_is_not_output_and_live_stopped_reads_match(self):
        store = SegmentedRawStore(self.root / "raw-v2", segment_limit=2048, segment_count=2)
        port, path, descriptor = self._create(store)
        try:
            port.append(descriptor, b"private-output\n")
            self.assertNotIn(b"private-output", path.read_bytes())
            live = raw_query(self.root, "run", self.run_id, segmented=port)
            self.assertEqual(live["log_format"], "segmented-v1")
            self.assertEqual(base64.b64decode(live["read"]["chunks"][0]["dataBase64"]), b"private-output\n")
            self.assertEqual(raw_usage(self.root, segmented=port)["legacy"]["run_bytes"], 0)
            self.assertNotEqual(path.read_bytes(), b"private-output\n")
        finally:
            store.close()
        before = {p.name: p.read_bytes() for p in (self.root / "raw-v2").iterdir()}
        stopped = raw_query(self.root, "run", self.run_id)
        self.assertEqual(stopped["read"], live["read"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root / "raw-v2").iterdir()})

    def test_active_writer_refuses_standalone_reader_and_spoofed_ref(self):
        with SegmentedRawStore(self.root / "raw-v2", segment_limit=1024) as store:
            port, _, descriptor = self._create(store)
            self.assertEqual(port.append(descriptor, b"visible")["stored_bytes"], 7)
            with self.assertRaisesRegex(RuntimeError, "writer active"):
                raw_query(self.root, "run", self.run_id)
            record_path = self.artifact / "run.json"
            record = json.loads(record_path.read_text())
            record["raw_log_ref"] = str(uuid4())
            record_path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "differ"):
                raw_query(self.root, "run", self.run_id, segmented=port)


if __name__ == "__main__":
    unittest.main()
