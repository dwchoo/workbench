"""CS-O10: the real product UI on a PTY against a scripted ui_v1 server (no backend, no OMP, no model).

The command stop confirmation is drawn, a non-confirm key cancels it, ``x`` sends one ``stop_command`` with the id
shown, and the ack / final outcomes are drawn: ``exited`` as stopped, ``unknown`` as unconfirmed (never as stopped).
Expected values are this file's own (the user decision of 2026-10-24 and the ids and texts the test scripts).
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from independent_support_cw06 import ScriptedServer, UiPty, snap  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.ui.product.input import PREFIX  # noqa: E402

P = bytes([PREFIX])
COMMAND_ID = "11111111-2222-4333-8444-555555555555"
LABEL = "python3 raw_tty_job.py"


def snapshot_with_command(state="running"):
    snapshot = snap(owner="manager")
    snapshot["panes"]["host_shell"]["command"] = {"command_id": COMMAND_ID, "owner": "worker", "label": LABEL,
                                                  "state": state}
    return snapshot


class ProductPtyStopCommandTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="cso10-ui-", dir=tempfile.gettempdir()))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.server = ScriptedServer(
            snapshot=snapshot_with_command(),
            answers={"stop_command": lambda header, _payload: {"command_id": header["command_id"],
                                                               "stop": {"status": "stopping"}}})
        self.addCleanup(self.server.close)
        self.ui = UiPty(self.server.path, self.work, rows=30, cols=120)
        self.addCleanup(self.ui.close)
        self.assertTrue(self.server.attached.wait(10), "UI did not attach")
        self.assertTrue(self.ui.wait_text("manager", 10))

    def stops(self):
        return [f for f in self.server.frames if f.header.get("type") == "stop_command"]

    def final(self, request_id, stop):
        self.server.send(ui_v1.result(request_id, True, final=True, command_id=COMMAND_ID, stop=stop))

    def test_modal_cancel_confirm_and_outcomes_through_the_real_ui(self):
        ui, server = self.ui, self.server
        ui.send(P + b"x")
        self.assertTrue(ui.wait_text("Workbench 명령 중지 확인", 5), ui.screen_text())
        self.assertIn(LABEL, ui.screen_text())
        self.assertIn("worker", ui.screen_text())
        ui.send(b"k")  # not the confirm key: cancels, and the key is not sent to any pane
        self.assertTrue(ui.wait_for(lambda: "Workbench 명령 중지 확인" not in ui.screen_text(), 5), ui.screen_text())
        time.sleep(0.3)
        self.assertEqual([], self.stops())
        self.assertEqual(server.payloads("input") + server.payloads("paste"), b"")
        ui.send(P + b"\x18")
        self.assertTrue(ui.wait_text("Workbench 명령 중지 확인", 5))
        ui.send("ㅌ".encode())
        self.assertTrue(server.wait(lambda: len(self.stops()) == 1, 5), [f.header for f in server.frames])
        self.assertEqual(self.stops()[0].header["command_id"], COMMAND_ID)
        self.assertEqual(server.payloads("input") + server.payloads("paste"), b"")
        self.assertTrue(ui.wait_text("명령 중지 중", 5), ui.screen_text())
        request_id = self.stops()[0].header["id"]
        self.final(request_id, {"status": "unknown", "reason": "still running after SIGKILL"})
        self.assertTrue(ui.wait_text("결과 불명", 5), ui.screen_text())
        self.assertNotIn("명령 중지됨", ui.screen_text())
        self.final(request_id, {"status": "exited", "exit_code": 143, "signal": 15, "signal_name": "SIGTERM"})
        self.assertTrue(ui.wait_text("명령 중지됨", 5), ui.screen_text())
        self.assertIn("143", ui.screen_text())
        ui.send(P + b"q")
        self.assertTrue(ui.ui_done(10), "the UI did not detach")
        self.assertEqual(ui.status(), 0)

    def test_no_command_shows_the_notice_and_sends_nothing(self):
        ui, server = self.ui, self.server
        server.state(snap(owner="user"))
        time.sleep(0.3)
        ui.send(P + b"x")
        self.assertTrue(ui.wait_text("중지할 Workbench 명령이 없습니다", 5), ui.screen_text())
        self.assertNotIn("Workbench 명령 중지 확인", ui.screen_text())
        time.sleep(0.2)
        self.assertEqual([], self.stops())


if __name__ == "__main__":
    unittest.main()
