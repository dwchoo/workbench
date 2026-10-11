"""One actual temporary host shell through the disabled segmented source port."""

import base64
from pathlib import Path
import threading
import time

from workbench.backend.flow_terminal import TerminalService
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.storage.log_raw.port import DisabledSegmentedRawPort, raw_query
from workbench.storage.log_raw.segments import SegmentedRawStore
from workbench.terminal.shell_g2.prototype import ShellChoice

import test_flow_terminal as tf


class Dvl12LiveTerminal(tf.ServiceFixture):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self.pane = ShellPane(ShellChoice("bash", "/usr/bin/bash"),
                              {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8"})
        self.ui = bytearray()
        self.stop = threading.Event()

        def pump():
            while not self.stop.is_set():
                for chunk in self.pane.pump():
                    self.ui.extend(chunk.data)
                time.sleep(0.01)

        self.loop = threading.Thread(target=pump)
        self.loop.start()
        self.terminal.close()
        self.store = SegmentedRawStore(self.root / "raw-v2", segment_limit=1024, segment_count=2)
        self.terminal = TerminalService(
            handoffs=self.handoffs, host_shell=lambda: HostShellPort(self.pane, lambda: self.pane),
            gate=self.gate, log_root=self.root / "workflow" / "terminal",
            automation=lambda: tf.AUTOMATION, poll_interval=0.02,
            segmented_raw_port=DisabledSegmentedRawPort(self.store))
        self.assertTrue(tf.wait_until(lambda: self.pane.state["parent_mode"] == "manual_prompt", 10))

    def tearDown(self):
        self.terminal.close()
        self.stop.set()
        self.loop.join(5)
        self.assertFalse(self.loop.is_alive())
        self.pane.close()
        self.store.close()
        super().tearDown()

    def test_real_display_and_collector_continue_with_descriptor(self):
        result = self.run_tool({"command": "printf 'dvl12-shell\\n'", "wait": 20})
        self.assertEqual((result.get("status"), result.get("exit_code")), ("exited", 0), result)
        self.assertEqual(result["log_format"], "segmented-v1")
        self.assertIn(b"dvl12-shell", bytes(self.ui))
        self.assertIn("dvl12-shell", result["output_tail"])
        descriptor = Path(result["log_path"])
        self.assertNotIn(b"dvl12-shell", descriptor.read_bytes())
        command_id = result["command_id"]
        live = raw_query(self.root, "command", command_id, segmented=self.terminal._segmented_raw_port)
        self.assertEqual(base64.b64decode(live["read"]["chunks"][0]["dataBase64"]), b"dvl12-shell\r\n")
        self.store.close()
        stopped = raw_query(self.root, "command", command_id)
        self.assertEqual(stopped["read"], live["read"])


if __name__ == "__main__":
    import unittest
    unittest.main()
