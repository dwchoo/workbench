"""CS-O10: the user stops a Workbench command from the product UI (``prefix x``).

Seam: ``ProductModel`` with a recording sender (the pattern of ``test_product_model.py``'s host-terminal kill tests):
keys go in through ``handle_input``, the frames the UI sends and the notice/footer/modal lines it shows are what is
asserted. One PTY test runs the real product UI against a scripted ui_v1 server (no backend, no OMP).

Expected values are this file's own, from the user decision of 2026-10-24: ``prefix x`` / ``prefix Ctrl-x`` /
``prefix ㅌ`` opens a confirmation for the Workbench command the snapshot names (``panes.host_shell.command``); only a
single ``x`` (``X`` / ``Ctrl-x`` / ``ㅌ``) confirms and sends one ``stop_command`` with that command's id; anything
else cancels with a notice; with no such command a notice and no modal; ``unknown`` never reads as success and
``stopping`` reads as in progress.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender, snapshot  # noqa: E402
from workbench.ui.product.input import PREFIX  # noqa: E402
from workbench.ui.product.model import HELP_LINES, MENU_ITEMS, ProductModel  # noqa: E402

P = bytes([PREFIX])
COMMAND_ID = "11111111-2222-4333-8444-555555555555"
OTHER_ID = "66666666-7777-4888-8999-aaaaaaaaaaaa"
JAMO_X = "ㅌ".encode()
OPEN = {"x": P + b"x", "X": P + b"X", "Ctrl-x": P + b"\x18", "ㅌ Space": P + JAMO_X + b" "}
CONFIRM = {"x": b"x", "X": b"X", "Ctrl-x": b"\x18", "ㅌ": JAMO_X, "ㅌ Space": JAMO_X + b" ",
           "prefix x": P + b"x", "prefix Ctrl-x": P + b"\x18", "prefix ㅌ Space": P + JAMO_X + b" "}


def command(command_id=COMMAND_ID, owner="worker", label="python3 train.py --epochs 50", state="running"):
    return {"command_id": command_id, "owner": owner, "label": label, "state": state}


def make(cmd=None, rows=30, cols=120):
    sender = FakeSender()
    model = ProductModel(sender, rows, cols, clock=lambda: 1000.0)
    model.apply_snapshot(with_command(cmd))
    sender.sent.clear()
    return model, sender


def with_command(cmd, **kwargs):
    snap = snapshot(**kwargs)
    if cmd is not False:
        snap["panes"]["host_shell"]["command"] = cmd
    return snap


def stops(sender):
    return sender.of("stop_command")


def feed(model, data, now=0.0):
    model.handle_input(data, now=now)
    model.flush_input(now=now + 1.0)


class OpenTests(unittest.TestCase):
    def test_every_open_form_shows_the_confirmation_for_the_running_command_and_sends_nothing(self):
        for form, keys in OPEN.items():
            with self.subTest(form=form):
                model, sender = make(command())
                model.handle_input(keys)
                self.assertTrue(model.stop_confirm_open)
                self.assertEqual([], sender.sent)
                joined = "\n".join(model.stop_confirm_lines())
                self.assertIn("python3 train.py --epochs 50", joined)
                self.assertIn("worker", joined)
                self.assertIn("x: 중지 · Esc/다른 키: 취소", joined)
                self.assertIn("중지", model.footer())
                self.assertFalse(model.kill_confirm_open or model.menu_open or model.quit)

    def test_the_manager_experiment_command_is_shown_with_its_owner(self):
        model, _ = make(command(owner="manager", label="experiment 11111111"))
        model.handle_input(P + b"x")
        joined = "\n".join(model.stop_confirm_lines())
        self.assertIn("manager", joined)
        self.assertIn("experiment 11111111", joined)

    def test_a_long_label_is_cut_to_the_modal_width(self):
        model, _ = make(command(label="python3 " + "z" * 300))
        model.handle_input(P + b"x")
        for line in model.stop_confirm_lines():
            self.assertLessEqual(len(line), 100, line)
        self.assertIn("…", "\n".join(model.stop_confirm_lines()))

    def test_control_characters_in_the_label_and_owner_never_widen_or_break_the_modal(self):
        # wcswidth is -1 for a string with a control character, which would have skipped the clip
        label = "echo \x07\x1b[31m\x00 " + "z" * 300 + "\nsecond line"
        model, _ = make(command(label=label, owner="wor\x1bker"))
        model.handle_input(P + b"x")
        lines = model.stop_confirm_lines()
        for line in lines:
            self.assertLessEqual(len(line), 100, line)
            self.assertTrue(line.isprintable(), repr(line))
        joined = "\n".join(lines)
        self.assertIn("echo", joined)
        self.assertIn("…", joined)
        self.assertNotIn("second line", joined.replace("…", ""), "the cut text is not shown past the clip")

    def test_no_running_command_shows_a_notice_and_no_modal(self):
        cases = {"null": None, "missing field": False, "not a dict": "sleep 5",
                 "no id": {"owner": "worker", "label": "x", "state": "running"},
                 "id not text": command(command_id=7)}
        for name, cmd in cases.items():
            with self.subTest(case=name):
                model, sender = make(cmd)
                model.handle_input(P + b"x")
                self.assertFalse(model.stop_confirm_open)
                self.assertEqual([], sender.sent)
                self.assertIn("중지할 Workbench 명령이 없습니다", model.notice)
                self.assertIn("Ctrl+C", model.notice)  # the user's own programs are stopped there, not here

    def test_a_command_already_stopping_is_not_asked_for_again(self):
        model, sender = make(command(state="stopping"))
        model.handle_input(P + b"x")
        self.assertFalse(model.stop_confirm_open)
        self.assertIn("중지 진행 중", model.notice)
        self.assertEqual([], sender.sent)

    def test_opening_the_confirmation_drops_a_held_ctrl_d(self):
        model, sender = make(command())
        model.handle_input(b"\x04", now=1.0)  # the first Ctrl-d to the manager OMP is held, not sent
        model.handle_input(P + b"x", now=1.5)
        self.assertTrue(model.stop_confirm_open)
        self.assertIsNone(model._ctrl_d)


class ConfirmTests(unittest.TestCase):
    def opened(self, form="x", cmd=None):
        model, sender = make(cmd or command())
        model.handle_input(OPEN[form])
        sender.sent.clear()
        return model, sender

    def test_each_confirm_form_sends_exactly_one_stop_command_with_the_shown_id(self):
        for open_form in OPEN:
            for name, keys in CONFIRM.items():
                with self.subTest(open=open_form, confirm=name):
                    model, sender = self.opened(open_form)
                    model.handle_input(keys)
                    self.assertFalse(model.stop_confirm_open)
                    self.assertEqual([("stop_command", {"command_id": COMMAND_ID}, b"")], stops(sender))
                    self.assertEqual([], sender.of("input") + sender.of("paste"))
                    self.assertIn("중지 요청", model.notice)

    def test_the_id_sent_is_the_one_shown_even_if_the_snapshot_changes_meanwhile(self):
        model, sender = self.opened()
        model.apply_snapshot(with_command(command(command_id=OTHER_ID, label="another")))
        model.handle_input(b"x")
        self.assertEqual([("stop_command", {"command_id": COMMAND_ID}, b"")], stops(sender))
        model.apply_snapshot(with_command(None))
        model, sender = self.opened()
        model.apply_snapshot(with_command(None))  # the command ended while the modal was open
        model.handle_input(b"x")
        self.assertEqual([("stop_command", {"command_id": COMMAND_ID}, b"")], stops(sender),
                         "a finished command is answered by the backend, never silently re-targeted")

    def test_extra_bytes_with_the_confirm_key_cancel_and_send_nothing(self):
        extras = {"xx": b"xx", "xyz": b"xyz", "x Enter": b"x\r", "Ctrl-x Ctrl-x": b"\x18\x18", "x Ctrl-x": b"x\x18",
                  "prefix x x": P + b"xx", "unbracketed paste": b"xargs rm\n", "ㅌ x": JAMO_X + b"x",
                  "x wheel": b"x\x1b[<64;5;5M"}
        for name, keys in extras.items():
            with self.subTest(extra=name):
                model, sender = self.opened()
                model.handle_input(keys, now=0.0)
                model.flush_input(now=1.0)
                self.assertFalse(model.stop_confirm_open)
                self.assertEqual([], sender.sent, "nothing reaches a pane or the backend")
                self.assertIn("확인이 취소되었습니다", model.notice)
                self.assertIn("x 한 번만 눌러 확인", model.notice)

    def test_nothing_else_confirms_and_a_cancel_sends_nothing(self):
        cancels = {"Esc": b"\x1b", "Enter": b"\r", "Space": b" ", "Tab": b"\t", "Ctrl-c": b"\x03", "k": b"k",
                   "y": b"y", "digit": b"1", "q": b"q", "ㅂ": "ㅂ".encode(), "paste": b"\x1b[200~x\x1b[201~",
                   "wheel": b"\x1b[<64;5;5M", "click": b"\x1b[<0;5;5M", "prefix q": P + b"q",
                   "prefix k": P + b"k", "prefix Space": P + b" "}
        for name, keys in cancels.items():
            with self.subTest(cancel=name):
                model, sender = self.opened()
                model.handle_input(keys, now=0.0)
                model.flush_input(now=1.0)
                self.assertFalse(model.stop_confirm_open)
                self.assertEqual([], sender.sent, "nothing reaches a pane or the backend")
                self.assertFalse(model.quit or model.menu_open or model.kill_confirm_open)
                model.handle_input(b"ls\r")  # back to normal: the keys flow to the focus pane
                self.assertEqual([b"ls\r"], [s[2] for s in sender.of("input")])

    def test_a_window_resize_cancels_the_confirmation(self):
        for size in ((44, 160), (20, 70), (5, 10)):
            with self.subTest(size=size):
                model, sender = self.opened()
                model.resize(*size)
                self.assertFalse(model.stop_confirm_open)
                self.assertEqual([], stops(sender))
                self.assertEqual([], sender.of("input") + sender.of("paste"))
                model.resize(30, 120)
                model.handle_input(b"x")  # back to normal: a plain x flows to the focus pane, never stops
                self.assertEqual([], stops(sender))
                self.assertEqual([b"x"], [s[2] for s in sender.of("input")])

    def test_two_separate_x_keys_confirm_on_the_second_chunk(self):
        model, sender = self.opened()
        model.handle_input(P)  # a lone prefix leaves the confirmation open
        self.assertTrue(model.stop_confirm_open)
        model.handle_input(b"x")
        self.assertEqual(1, len(stops(sender)))

    def test_a_stop_in_flight_is_not_asked_for_again(self):
        model, sender = self.opened()
        model.handle_input(b"x")
        model.handle_input(P + b"x")
        self.assertFalse(model.stop_confirm_open)
        self.assertIn("중지 요청", model.notice)
        self.assertEqual(1, len(stops(sender)))

    def test_it_never_overlaps_the_other_confirmations(self):
        model, sender = self.opened()
        model.handle_input(P + b"k")  # the modal judges this key: not a confirm, so it cancels and runs nothing
        self.assertFalse(model.stop_confirm_open)
        self.assertFalse(model.kill_confirm_open)
        self.assertEqual([], sender.sent)
        model, sender = make(command())
        model.handle_input(P + b"k")
        model.handle_input(P + b"x")
        self.assertFalse(model.kill_confirm_open or model.stop_confirm_open)
        self.assertEqual([], sender.sent)


class OutcomeTests(unittest.TestCase):
    def sent(self):
        model, sender = make(command())
        model.handle_input(P + b"x")
        model.handle_input(b"x")
        return model, sender

    def ack(self, model, sender):
        model.on_result({"id": f"r{sender.count}", "ok": True, "command_id": COMMAND_ID,
                         "stop": {"status": "stopping"}})

    def final(self, model, sender, stop):
        model.on_result({"id": f"r{sender.count}", "ok": True, "final": True, "command_id": COMMAND_ID, "stop": stop})

    def test_the_ack_reads_as_in_progress_not_as_a_result(self):
        model, sender = self.sent()
        self.ack(model, sender)
        self.assertIn("중지 중", model.notice)
        for word in ("중지됨", "종료됨", "성공"):
            self.assertNotIn(word, model.notice)

    def test_exited_shows_the_exit_code_and_signal(self):
        model, sender = self.sent()
        self.ack(model, sender)
        self.final(model, sender, {"status": "exited", "exit_code": 143, "signal": 15, "signal_name": "SIGTERM",
                                   "command_id": COMMAND_ID})
        self.assertIn("명령 중지됨", model.notice)
        self.assertIn("143", model.notice)
        self.assertIn("SIGTERM", model.notice)
        model, sender = self.sent()
        self.final(model, sender, {"status": "exited", "exit_code": 0, "signal": None, "command_id": COMMAND_ID})
        self.assertIn("명령 중지됨", model.notice)
        self.assertIn("종료 코드 0", model.notice)

    def test_unknown_never_reads_as_success(self):
        model, sender = self.sent()
        self.final(model, sender, {"status": "unknown", "reason": "still running after SIGKILL"})
        self.assertIn("불명", model.notice)
        self.assertIn("still running after SIGKILL", model.notice)
        for word in ("중지됨", "종료됨", "성공"):
            self.assertNotIn(word, model.notice)

    def test_a_final_stopping_status_is_still_in_progress(self):
        model, sender = self.sent()
        self.final(model, sender, {"status": "stopping", "command_id": COMMAND_ID})
        self.assertIn("진행 중", model.notice)
        for word in ("중지됨", "종료됨", "성공"):
            self.assertNotIn(word, model.notice)

    def test_refused_not_found_already_exited_and_error_are_shown_with_their_reason(self):
        cases = ({"status": "refused", "reason": "not_owner"}, {"status": "refused", "reason": "backend_shutdown"},
                 {"status": "not_found", "reason": "unknown_command"},
                 {"status": "error", "reason": "internal_error", "detail": "stop failed: RuntimeError"})
        for stop in cases:
            with self.subTest(status=stop["status"], reason=stop["reason"]):
                model, sender = self.sent()
                self.final(model, sender, stop)
                self.assertIn(stop["reason"], model.notice)
                self.assertNotIn("명령 중지됨", model.notice)
        model, sender = self.sent()
        self.final(model, sender, {"status": "already_exited", "reason": "command_ended", "exit_code": 7})
        self.assertIn("이미 끝난", model.notice)

    def test_a_refused_request_is_shown_and_a_new_attempt_is_possible(self):
        for reason in ("not_attached", "invalid_message", "queue_full", "backend_shutdown"):
            with self.subTest(reason=reason):
                model, sender = self.sent()
                model.on_result({"id": f"r{sender.count}", "ok": False, "reason": reason, "detail": "why"})
                self.assertIn("명령 중지 거부", model.notice)
                self.assertIn(reason, model.notice)
                model.handle_input(P + b"x")
                self.assertTrue(model.stop_confirm_open)

    def test_a_final_outcome_does_not_need_its_request_to_be_pending(self):
        model, sender = make(command())
        model.on_result({"id": "never-sent", "ok": True, "final": True, "command_id": COMMAND_ID,
                         "stop": {"status": "exited", "exit_code": 130, "signal": 2, "signal_name": "SIGINT"}})
        self.assertIn("130", model.notice)

    def test_a_malformed_final_never_reads_as_success(self):
        for header in ({"ok": True, "final": True, "stop": None}, {"ok": True, "final": True, "stop": {}},
                       {"ok": True, "final": True, "stop": {"status": "weird"}},
                       {"ok": True, "final": True, "stop": "exited"}):
            with self.subTest(header=header):
                model, sender = self.sent()
                model.on_result({"id": f"r{sender.count}", **header})
                self.assertNotIn("명령 중지됨", model.notice)
                self.assertIn("명령 중지", model.notice)


class DiscoverabilityTests(unittest.TestCase):
    def test_help_lists_the_key_with_its_ctrl_and_jamo_forms(self):
        (line,) = [ln for ln in HELP_LINES if "prefix x" in ln]
        for needle in ("Workbench 명령", "Ctrl-x", "ㅌ", "확인"):
            self.assertIn(needle, line)

    def test_the_menu_has_a_stop_row_that_opens_the_confirmation_without_renumbering(self):
        model, sender = make(command())
        model.handle_input(P + b" ")
        rows = [ln for ln in model.menu_lines() if "Workbench 명령 중지" in ln]
        self.assertEqual(1, len(rows), model.menu_lines())
        self.assertIn("Ctrl-] x", rows[0])
        self.assertIn("Ctrl-] Ctrl-x", rows[0])
        self.assertEqual(10, len(MENU_ITEMS), "the ten numbered items keep their digits")
        model.handle_input(b"\x18")  # Ctrl-x inside the menu runs the row; the menu closes
        self.assertFalse(model.menu_open)
        self.assertTrue(model.stop_confirm_open)
        self.assertEqual([], sender.sent)

    def test_the_prefix_footer_mentions_the_key(self):
        model, _ = make(command())
        model.handle_input(P)
        self.assertIn("x(명령 중지)", model.footer())

    def test_the_menu_row_with_no_command_shows_the_notice(self):
        model, sender = make(None)
        model.handle_input(P + b" ")
        model.handle_input(b"\x18")
        self.assertFalse(model.stop_confirm_open)
        self.assertIn("중지할 Workbench 명령이 없습니다", model.notice)


if __name__ == "__main__":
    unittest.main()
