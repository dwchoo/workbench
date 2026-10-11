"""DVL-12 runtime gates over an actual private Bash pane and terminal collector."""

import base64
from contextlib import redirect_stdout
import errno
from io import StringIO
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from workbench.backend import cli
from workbench.backend.client import UiClient
from workbench.backend.flow_terminal import TerminalService
from workbench.backend.paths import DataLayout
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.backend.service import Backend
from workbench.backend.ui_server import UiServer
from workbench.contracts.ui_v1 import ClientType
from workbench.storage.log_raw.port import DisabledSegmentedRawPort, raw_query
from workbench.storage.log_raw.segments import SegmentedRawStore
from workbench.terminal.shell_g2.prototype import ShellChoice

import test_flow_terminal as fixture


class DisabledRawActualShell(fixture.ServiceFixture):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self.pane = ShellPane(ShellChoice("bash", "/usr/bin/bash"),
                              {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8"})
        self.screen = bytearray()
        self.stop_pump = threading.Event()

        def pump():
            while not self.stop_pump.is_set():
                for chunk in self.pane.pump():
                    self.screen.extend(chunk.data)
                time.sleep(0.01)

        self.pump_thread = threading.Thread(target=pump, name="dvl12-private-shell-pump")
        self.pump_thread.start()
        self.terminal.close()
        self.store = SegmentedRawStore(self.root / "raw-v2", segment_limit=512, segment_count=2)
        self.source_port = DisabledSegmentedRawPort(self.store)
        self.terminal = TerminalService(
            handoffs=self.handoffs, host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
            gate=self.gate, log_root=self.root / "workflow" / "terminal",
            automation=lambda: fixture.AUTOMATION, poll_interval=0.02,
            segmented_raw_port=self.source_port)
        self.assertTrue(fixture.wait_until(
            lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))

    def tearDown(self):
        try:
            self.terminal.close()
            self.stop_pump.set()
            self.pump_thread.join(5)
            self.assertFalse(self.pump_thread.is_alive())
            self.pane.close()
            self.store.close()
        finally:
            super().tearDown()

    def test_eviction_does_not_stop_the_host_or_present_a_complete_first_source(self):
        first = self.run_tool({"command": "printf '%04000d' 0; printf '\\nDVL12-FIRST\\n'", "wait": 20})
        self.assertEqual((first["status"], first["exit_code"]), ("exited", 0), first)
        self.assertTrue(fixture.wait_until(lambda: b"DVL12-FIRST" in self.screen, 5))
        self.assertIn("DVL12-FIRST", first["output_tail"])
        self.assertEqual(first["log_format"], "segmented-v1")
        self.assertNotIn(b"DVL12-FIRST", Path(first["log_path"]).read_bytes())

        second = self.run_tool({"command": "printf 'DVL12-SECOND\\n'", "wait": 20})
        self.assertEqual((second["status"], second["exit_code"]), ("exited", 0), second)
        self.assertTrue(fixture.wait_until(lambda: b"DVL12-SECOND" in self.screen, 5))
        self.assertNotEqual(first["raw_log_ref"], second["raw_log_ref"])
        first_page = raw_query(self.root, "command", first["command_id"],
                               segmented=self.source_port, limit=128)
        self.assertGreater(first_page["source_status"]["evicted_bytes"], 0)
        self.assertIn(first_page["read"]["status"], ("partial", "unavailable"))
        self.assertIn("evicted", {gap["reason"] for gap in first_page["read"]["missing"]})
        second_page = raw_query(self.root, "command", second["command_id"],
                                segmented=self.source_port, limit=512)
        self.assertEqual(second_page["read"]["status"], "available")
        self.assertIn(b"DVL12-SECOND", b"".join(base64.b64decode(chunk["dataBase64"])
                                                  for chunk in second_page["read"]["chunks"]))

        # Query the source from the actual running UI and read-only agent route.
        layout = DataLayout(self.root)
        backend = Backend.__new__(Backend)
        backend.layout = layout
        backend.segmented_raw_port = self.source_port
        before = {path.name: path.read_bytes() for path in layout.raw_v2.iterdir()}
        server = UiServer(layout.ui_socket, backend)
        stop_server = threading.Event()

        def serve():
            while not stop_server.is_set():
                server.poll(0.01)

        server_thread = threading.Thread(target=serve, name="dvl12-private-ui")
        server_thread.start()
        try:
            with UiClient(layout.ui_socket, name="workbench-cli") as client:
                response = client.request(ClientType.RAW_QUERY, source_kind="command",
                                          source_id=second["command_id"], offset=0, limit=512)
                self.assertTrue(response["ok"], response)
                self.assertEqual(response["result"]["read"]["status"], "available")
                status = client.request(ClientType.RAW_STATUS)
                self.assertEqual(status["result"]["activation"], "disabled")
            output = StringIO()
            with redirect_stdout(output):
                code = cli.main(["logs", "raw", "--command", second["command_id"],
                                 "--limit", "512", "--json", "--data-dir", str(self.root)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["raw_log_ref"], second["raw_log_ref"])

            peer = SimpleNamespace(role="worker")
            base = {"request_id": str(uuid4()), "tool_call_id": "runtime-raw",
                    "session_id": str(uuid4()), "generation": 1, "tool": "workbench_logs"}
            args = {"action": "raw", "subject_kind": "command", "subject_id": first["command_id"],
                    "limit": None, "page": None, "offset": 0, "length": 128}
            result = backend._tool_request(peer, {**base, "args": args})
            self.assertEqual(result["status"], "partial", result)
            self.assertIn("evicted", {gap["reason"] for gap in result["data"]["read"]["missing"]})
            for denied in ("activate", "delete-db", "maintenance"):
                self.assertEqual(backend._tool_request(peer, {**base, "args": {**args, "action": denied}})["status"],
                                 "rejected")
            self.assertEqual(before, {path.name: path.read_bytes() for path in layout.raw_v2.iterdir()})
            print("DVL12_EVICTION_RUNTIME=" + json.dumps({
                "source_count": 2,
                "first_observed": first_page["source_status"]["observed_bytes"],
                "first_retained": first_page["source_status"]["retained_bytes"],
                "first_evicted": first_page["source_status"]["evicted_bytes"],
                "first_missing": first_page["source_status"]["missing_bytes"],
                "second_observed": second_page["source_status"]["observed_bytes"],
                "second_retained": second_page["source_status"]["retained_bytes"],
                "ui_queries": 2, "cli_queries": 1, "agent_reads": 1, "agent_mutations_rejected": 3,
                "configured_model_clients": 0, "explicit_raw_cleanup_requests": 0,
                "explicit_activation_requests": 0,
            }, sort_keys=True))
        finally:
            stop_server.set()
            server_thread.join(3)
            self.assertFalse(server_thread.is_alive())
            server.close(flush_timeout=0.1)

    def test_storage_fault_marks_the_gap_while_real_output_and_next_command_continue(self):
        with patch.object(self.store, "_append_frame", side_effect=OSError(errno.ENOSPC, "fixture full")):
            failed = self.run_tool({"command": "printf 'DVL12-FAULT\\n'", "wait": 20})
        self.assertEqual((failed["status"], failed["exit_code"]), ("exited", 0), failed)
        self.assertTrue(fixture.wait_until(lambda: b"DVL12-FAULT" in self.screen, 5))
        self.assertIn("DVL12-FAULT", failed["output_tail"])
        page = raw_query(self.root, "command", failed["command_id"],
                         segmented=self.source_port, limit=512)
        self.assertGreater(page["source_status"]["missing_bytes"], 0)
        self.assertIn("storage_gap", {gap["reason"] for gap in page["read"]["missing"]})
        self.assertNotEqual(page["read"]["status"], "available")

        later = self.run_tool({"command": "printf 'DVL12-AFTER\\n'", "wait": 20})
        self.assertEqual((later["status"], later["exit_code"]), ("exited", 0), later)
        self.assertTrue(fixture.wait_until(lambda: b"DVL12-AFTER" in self.screen, 5))
        self.assertNotEqual(later["command_id"], failed["command_id"])
        print("DVL12_FAULT_RUNTIME=" + json.dumps({
            "failed_observed": page["source_status"]["observed_bytes"],
            "failed_retained": page["source_status"]["retained_bytes"],
            "failed_missing": page["source_status"]["missing_bytes"],
            "failed_evicted": page["source_status"]["evicted_bytes"],
            "next_command_exit": later["exit_code"], "configured_model_clients": 0,
            "explicit_raw_cleanup_requests": 0, "explicit_activation_requests": 0,
        }, sort_keys=True))

    def test_task_linked_terminal_source_is_exact_and_changed_task_refuses_before_typing(self):
        task_id = str(uuid4())
        current = SimpleNamespace(task_id=task_id, revision=7)
        self.terminal.close()
        self.terminal = TerminalService(
            handoffs=self.handoffs, host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
            gate=self.gate, log_root=self.root / "workflow" / "terminal",
            automation=lambda: fixture.AUTOMATION, poll_interval=0.02,
            segmented_raw_port=self.source_port, active_task=lambda: current)
        linked = self.run_tool({"command": "printf 'DVL12-LINKED\\n'", "wait": 20})
        self.assertEqual((linked["status"], linked["exit_code"]), ("exited", 0), linked)
        linked_source = json.loads(Path(linked["log_path"]).read_text())["source"]
        self.assertEqual(linked_source["taskId"], task_id)
        self.assertEqual(linked_source["revision"], 7)
        self.assertEqual(linked_source["commandId"], linked["command_id"])
        self.assertNotIn("runId", linked_source)
        page = raw_query(self.root, "command", linked["command_id"],
                         segmented=self.source_port, limit=512)
        self.assertEqual(page["source_status"]["source"], linked_source)
        self.assertEqual(linked["read_command"], page["read_command"])

        current = None
        unlinked = self.run_tool({"command": "printf 'DVL12-UNLINKED\\n'", "wait": 20})
        self.assertEqual((unlinked["status"], unlinked["exit_code"]), ("exited", 0), unlinked)
        unlinked_source = json.loads(Path(unlinked["log_path"]).read_text())["source"]
        self.assertEqual(unlinked_source["commandId"], unlinked["command_id"])
        self.assertNotIn("taskId", unlinked_source)
        self.assertNotIn("revision", unlinked_source)

        self.terminal.close()
        calls = 0

        def changed_task():
            nonlocal calls
            calls += 1
            return (SimpleNamespace(task_id=task_id, revision=7) if calls == 1
                    else SimpleNamespace(task_id=str(uuid4()), revision=8))

        self.terminal = TerminalService(
            handoffs=self.handoffs, host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
            gate=self.gate, log_root=self.root / "workflow" / "terminal",
            automation=lambda: fixture.AUTOMATION, poll_interval=0.02,
            segmented_raw_port=self.source_port, active_task=changed_task)
        prior = {path.name for path in (self.root / "workflow" / "terminal").glob("*.json")}
        refused = self.run_tool({"command": "printf 'DVL12-NEVER-TYPE\\n'", "wait": 20})
        self.assertEqual((refused["status"], refused["reason"]),
                         ("rejected", "source_identity_unavailable"), refused)
        self.assertEqual(prior, {path.name for path in (self.root / "workflow" / "terminal").glob("*.json")})
        self.assertNotIn(b"DVL12-NEVER-TYPE", self.screen)
        print("DVL12_LINKED_RUNTIME=" + json.dumps({"linked_task": True, "revision": 7,
                                                     "unlinked_command": True,
                                                     "changed_identity_refused": True,
                                                     "actual_bash_and_host_pane": True}, sort_keys=True))
