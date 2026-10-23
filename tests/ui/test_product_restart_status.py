"""While the host shell restarts, the status line says so instead of showing the stale shell mode."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from support import snapshot  # noqa: E402
from test_product_model import P, display, host_exited  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402

RESTARTING_MODE = "shell mode: 재시작 중 (shell 입력 받지 않음)"


def mode_part(model):
    return next(p for p in model.status_lines()[0].split(" | ") if p.startswith("shell mode:"))


def live_snapshot(generation=2, **kwargs):
    snap = snapshot(**kwargs)
    for info in snap["panes"].values():
        info["generation"] = generation
    return snap


class RestartStatusLineTests(unittest.TestCase):
    def restarting(self, **kwargs):
        model, sender = host_exited(**kwargs)
        self.assertEqual("shell mode: user_control", mode_part(model))
        model.handle_input(b"\r")
        return model, sender

    def test_status_line_shows_restarting_instead_of_the_stale_mode(self):
        model, _ = self.restarting()
        self.assertEqual(RESTARTING_MODE, mode_part(model))
        self.assertNotIn("user_control", model.status_lines()[0])
        self.assertIn("focus:", model.status_lines()[0])

    def test_the_status_names_the_shell_when_an_omp_pane_has_focus_and_omp_keys_still_go_through(self):
        model, sender = self.restarting()
        model.handle_input(P + b"1")
        self.assertEqual(PaneId.MANAGER_OMP, model.focus)
        self.assertEqual(RESTARTING_MODE, mode_part(model))
        self.assertIn("focus: MANAGER OMP", model.status_lines()[0])
        sender.sent.clear()
        model.handle_input(b"hello")
        self.assertEqual([("input", {"pane": "manager_omp"}, b"hello")], sender.of("input"))

    def test_keys_and_paste_during_the_restart_are_dropped_and_the_status_stays(self):
        model, sender = self.restarting()
        model.handle_input(b"ls\r")
        model.handle_input(b"\x1b[200~pasted\r\x1b[201~")
        model.handle_input(b"\x03")
        self.assertEqual(1, len(sender.of("restart_pane")))
        self.assertEqual([], sender.of("input"))
        self.assertEqual([], sender.of("paste"))
        self.assertEqual(RESTARTING_MODE, mode_part(model))

    def test_state_push_with_a_live_shell_restores_the_snapshot_mode(self):
        model, _ = self.restarting()
        model.apply_snapshot(live_snapshot(mode="manager_control"))
        self.assertEqual("shell mode: manager_control", mode_part(model))

    def test_new_generation_output_restores_the_snapshot_mode(self):
        model, _ = self.restarting(generation=1)
        model.state["panes"]["host_shell"]["session_id"] = "old"
        model.on_display(ui_v1.Frame({"pane": "host_shell", "session_id": "new", "generation": 2}, b"$ "))
        self.assertEqual("shell mode: user_control", mode_part(model))

    def test_refusal_restores_the_snapshot_mode(self):
        model, sender = self.restarting()
        model.on_result({"id": f"r{sender.count}", "ok": False, "reason": "restart_failed", "detail": "no shell"})
        self.assertEqual("shell mode: user_control", mode_part(model))

    def test_an_omp_pane_restart_does_not_touch_the_shell_mode(self):
        model, sender = host_exited(focus_host=False)
        model.state["panes"]["manager_omp"]["alive"] = False
        model.state["panes"]["manager_omp"]["exit_status"] = 1
        model.apply_snapshot(model.state)
        model.handle_input(b"\r")
        self.assertEqual(1, len(sender.of("restart_pane")))
        self.assertEqual("shell mode: user_control", mode_part(model))


if __name__ == "__main__":
    unittest.main()
