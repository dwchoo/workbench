"""Disabled raw source through CLI and agent read-only routes."""

import base64
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import uuid4

from workbench.backend import cli
from workbench.backend.paths import DataLayout
from workbench.backend.service import Backend
from workbench.backend.ui_server import UiServer
from workbench.backend.client import UiClient
from workbench.contracts.ui_v1 import ClientType
from workbench.storage.log_raw.port import DisabledSegmentedRawPort
from workbench.storage.log_raw.segments import SegmentedRawStore


class Dvl12Routes(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.layout = DataLayout(self.root)
        self.run_id, self.task_id = str(uuid4()), str(uuid4())
        (self.root / "workflow").mkdir(mode=0o700)
        (self.root / "workflow" / "runs").mkdir(mode=0o700)
        artifact = self.root / "workflow" / "runs" / self.run_id
        artifact.mkdir(mode=0o700)
        self.store = SegmentedRawStore(self.layout.raw_v2, segment_limit=1024, segment_count=2)
        self.addCleanup(self.store.close)
        self.port = DisabledSegmentedRawPort(self.store)
        self.descriptor_path, self.descriptor = self.port.prepare_task(artifact, self.task_id, 1, self.run_id)
        record = {"run_id": self.run_id, "log_format": "segmented-v1",
                  "raw_log_ref": self.descriptor["raw_log_ref"], "raw_source": self.descriptor["source"]}
        (artifact / "run.json").write_text(json.dumps(record))
        (artifact / "run.json").chmod(0o600)
        self.assertEqual(self.port.append(self.descriptor, b"live bytes")["stored_bytes"], 10)

    def test_backend_and_agent_return_bounded_source_and_reject_mutation(self):
        backend = Backend.__new__(Backend)
        backend.layout = self.layout
        backend.segmented_raw_port = self.port
        direct = backend.raw_query("run", self.run_id, 0, 10)
        self.assertEqual(base64.b64decode(direct["read"]["chunks"][0]["dataBase64"]), b"live bytes")
        peer = SimpleNamespace(role="worker")
        base = {"request_id": str(uuid4()), "tool_call_id": "call-raw", "session_id": str(uuid4()),
                "generation": 1, "tool": "workbench_logs"}
        args = {"action": "raw", "subject_kind": "run", "subject_id": self.run_id,
                "limit": None, "page": None, "offset": 0, "length": 10}
        answer = backend._tool_request(peer, {**base, "args": args})
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(answer["data"]["raw_log_ref"], self.descriptor["raw_log_ref"])
        for action in ("activate", "delete-db", "maintenance"):
            self.assertEqual(backend._tool_request(peer, {**base, "args": {**args, "action": action}})["status"],
                             "rejected")

    def test_stopped_cli_reads_without_writer_constructor_or_source_changes(self):
        self.store.close()
        before = {p.name: p.read_bytes() for p in self.layout.raw_v2.iterdir()}
        output = StringIO()
        with redirect_stdout(output):
            code = cli.main(["logs", "raw", "--run", self.run_id, "--offset", "0", "--limit", "10",
                             "--json", "--data-dir", str(self.root)])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "available")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.layout.raw_v2.iterdir()})

    def test_running_cli_uses_readonly_ui_request_on_existing_singleton(self):
        backend = Backend.__new__(Backend)
        backend.layout = self.layout
        backend.segmented_raw_port = self.port
        server = UiServer(self.layout.ui_socket, backend)
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                server.poll(0.01)

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            with UiClient(self.layout.ui_socket, name="workbench-cli") as client:
                response = client.request(ClientType.RAW_QUERY, source_kind="run", source_id=self.run_id,
                                          offset=0, limit=10)
                self.assertTrue(response["ok"])
                self.assertEqual(response["result"]["status"], "available")
            output = StringIO()
            # The CLI now requires a verified backend identity; this test serves a bare UiServer, so
            # point the verified-client seam at the test's own socket and keep the read-only path real.
            with redirect_stdout(output), mock.patch.object(
                    cli, "_verified_client",
                    lambda _layout, **kwargs: UiClient(self.layout.ui_socket, **kwargs)):
                code = cli.main(["logs", "raw", "--run", self.run_id, "--limit", "10",
                                 "--json", "--data-dir", str(self.root)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["read"]["chunks"][0]["range"]["end"], 10)
        finally:
            stop.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            server.close(flush_timeout=0.1)


if __name__ == "__main__":
    unittest.main()
