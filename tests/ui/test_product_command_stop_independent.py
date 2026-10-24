"""CS-O10 independent UI checks: ``prefix x`` / Ctrl-x / ㅌ stops the running Workbench command after a confirmation.

Derived from the contract (docs/features/command-stop/FOLLOWUPS-BRIEF.md FU-D06, FU-D08, FU-AC-07/09/10 and the task
packet), not from the implementer's tests:

* ``prefix x``, ``prefix Ctrl-x`` and the Hangul jamo ``ㅌ`` open a confirmation only when the UI snapshot names a
  running Workbench command (``panes.host_shell.command``); the modal shows its owner and label and nothing reaches
  any pane meanwhile.
* one ``x`` / Ctrl-x / ㅌ on its own confirms and sends exactly one ``stop_command`` carrying the command_id the modal
  showed, never a newer one; every other key, multi-byte input, paste and a resize cancel and send nothing.
* no running command: a notice, no modal, nothing sent.
* the final outcome arrives as a second ``result`` frame (``final: true``); ``unknown``/``error``/unparseable are
  never shown as a success, ``stopping`` is shown as in progress, and an ack that arrives after the final does not
  overwrite it.

The model tests use the repo's recording sender. One PTY test runs the real product UI against a scripted ui_v1
server (no backend, no OMP, no model).
"""
from __future__ import annotations

import copy
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from independent_support_cw06 import RecordingSender, ScriptedServer, UiPty, snap  # noqa: E402
from workbench.contracts import ui_v1  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.ui.product.input import PARTIAL_HOLD_SECONDS, PREFIX  # noqa: E402
from workbench.ui.product.model import ProductModel  # noqa: E402

P = bytes([PREFIX])
HOLD = PARTIAL_HOLD_SECONDS + 0.001
PASTE = b"\x1b[200~%s\x1b[201~"
JAMO_X = "ㅌ".encode()
JAMO_K = "ㅏ".encode()
JAMO_P = "ㅔ".encode()
ID_A = "5f1c2a9e-3b4d-4e6f-8a7b-0c1d2e3f4a5b"
ID_B = "6a2d3b0f-4c5e-4f70-9b8c-1d2e3f4a5b6c"
WORKER_CMD = {"command_id": ID_A, "owner": "worker", "label": "pytest tests/slow -k heavy_marker", "state": "running"}
MANAGER_CMD = {"command_id": ID_B, "owner": "manager", "label": "experiment 6a2d3b0f", "state": "running"}

OPEN_FORMS = {"prefix_x": P + b"x", "prefix_ctrl_x": P + b"\x18", "prefix_jamo": P + JAMO_X,
              "prefix_jamo_space": P + JAMO_X + b" "}
CONFIRM_FORMS = {"x": b"x", "ctrl_x": b"\x18", "jamo": JAMO_X, "jamo_space": JAMO_X + b" ",
                 "prefix_x": P + b"x", "prefix_ctrl_x": P + b"\x18", "prefix_jamo": P + JAMO_X}
CANCEL_FORMS = {"esc": b"\x1b", "enter": b"\r", "lf": b"\n", "y": b"y", "j": b"j", "space": b" ", "ctrl_c": b"\x03",
                "digit": b"1", "q": b"q", "tab": b"\t", "up": b"\x1b[A", "backspace": b"\x7f",
                "hangul_syllable": "한".encode(), "syllable_starting_with_tieut": "택".encode(),
                "other_jamo": "ㅇ".encode(), "alt_x": b"\x1bx", "k": b"k", "ctrl_k": b"\x0b", "p": b"p",
                "ctrl_p": b"\x10", "jamo_k": JAMO_K, "paste_with_x": PASTE % b"x", "paste_text": PASTE % b"rm -rf x\r",
                "paste_ctrl_x": PASTE % b"\x18", "click": b"\x1b[<0;10;10M", "wheel_up": b"\x1b[<64;10;10M",
                "shift_pgup": b"\x1b[5;2~", "prefix_q": P + b"q", "prefix_space": P + b" ", "prefix_1": P + b"1",
                # more than one key in one read: auto-repeat, typing, an unbracketed paste
                "xx": b"xx", "x_enter": b"x\r", "xyz": b"xyz", "ctrl_x_twice": b"\x18\x18", "jamo_jamo": JAMO_X * 2,
                "x_then_jamo": b"x" + JAMO_X, "stop_word": b"stop"}


def make(*, command=WORKER_CMD, focus: str = "manager_omp", extra=None):
    sender = RecordingSender()
    model = ProductModel(sender, 40, 150, clock=lambda: 1000.0)
    model.apply_snapshot(snapshot_with(command, focus=focus, extra=extra), adopt_focus=True)
    sender.sent.clear()
    return model, sender


def snapshot_with(command, *, focus: str = "manager_omp", extra=None):
    snapshot = snap(owner="user", focus=focus)
    if command is not ...:
        snapshot["panes"]["host_shell"]["command"] = copy.deepcopy(command)
    if extra:
        snapshot["panes"]["host_shell"].update(extra)
    return snapshot


def feed(model, data, t: float = 0.0) -> float:
    for part in (data if isinstance(data, list) else [data]):
        model.handle_input(part, now=t)
        model.flush_input(now=t + HOLD)
        t += 1.0
    return t


def pane_frames(sender) -> list:
    return [s for s in sender.sent if s[0] in ("input", "paste")]


def stops(sender) -> list:
    return sender.of("stop_command")


def ack(model, sender, command_id=ID_A, *, index=-1):
    request_id = stops(sender)[index][3]
    model.on_result({"v": 1, "type": "result", "id": request_id, "ok": True, "command_id": command_id,
                     "stop": {"status": "stopping"}})
    return request_id


def final(model, request_id, stop, command_id=ID_A, **extra):
    header = {"v": 1, "type": "result", "id": request_id, "ok": True, "final": True, "command_id": command_id,
              "stop": stop, **extra}
    model.on_result(header)


class OpenTests(unittest.TestCase):
    def test_every_open_form_shows_the_command_and_sends_nothing_from_any_focus(self):
        for name, data in OPEN_FORMS.items():
            for focus in ("manager_omp", "worker_omp", "host_shell"):
                for command in (WORKER_CMD, MANAGER_CMD):
                    with self.subTest(form=name, focus=focus, owner=command["owner"]):
                        m, s = make(command=command, focus=focus)
                        feed(m, data)
                        self.assertTrue(m.stop_confirm_open, f"{name} did not open the confirmation")
                        text = "\n".join(m.stop_confirm_lines())
                        self.assertIn(command["label"], text)
                        self.assertIn(command["owner"], text)
                        self.assertEqual(s.sent, [], "opening the modal sent something")
                        self.assertFalse(m.kill_confirm_open or m.pause_confirm_open)

    def test_the_modal_is_drawn_with_the_label_and_owner_of_a_worker_and_a_manager_command(self):
        for command in (WORKER_CMD, MANAGER_CMD):
            m, _ = make(command=command)
            feed(m, P + b"x")
            lines = m.stop_confirm_lines()
            self.assertTrue(any(command["label"] in line and command["owner"] in line for line in lines), lines)
            self.assertIn("x", m.footer(), "the footer must tell which key confirms")

    def test_a_long_or_wide_label_is_shortened_not_overflowing_the_box(self):
        from wcwidth import wcswidth
        for label in ("x" * 400, "한글명령" * 60, "echo " + "ㅌ" * 120, "a\tb" * 50):
            with self.subTest(label=label[:12]):
                m, _ = make(command={**WORKER_CMD, "label": label})
                feed(m, P + b"x")
                for line in m.stop_confirm_lines():
                    self.assertLessEqual(wcswidth(line), 150 - 4, f"line wider than the terminal: {line!r}")

    def test_without_the_prefix_ctrl_x_x_and_jamo_are_ordinary_pane_input(self):
        for focus in ("manager_omp", "worker_omp", "host_shell"):
            with self.subTest(focus=focus):
                m, s = make(focus=focus)
                feed(m, b"\x18")  # readline / emacs prefix in the host shell
                feed(m, b"x", 2.0)
                feed(m, JAMO_X, 4.0)
                self.assertFalse(m.stop_confirm_open)
                self.assertEqual(stops(s), [])
                self.assertEqual(s.payloads("input", focus), b"\x18x" + JAMO_X)

    def test_scroll_mode_letters_never_open_but_the_prefix_still_does(self):
        m, s = make()
        t = feed(m, P + b"[")
        t = feed(m, b"x" + JAMO_X + b"\x18", t)
        self.assertFalse(m.stop_confirm_open)
        self.assertEqual((stops(s), pane_frames(s)), ([], []))
        feed(m, P + b"x", t)
        self.assertTrue(m.stop_confirm_open)
        self.assertEqual((stops(s), pane_frames(s)), ([], []))

    def test_no_command_gives_a_notice_and_sends_nothing(self):
        cases = {"command_null": None, "command_key_missing": ..., "empty_dict": {},
                 "no_id": {"owner": "worker", "label": "x", "state": "running"},
                 "empty_id": {**WORKER_CMD, "command_id": ""}, "id_not_str": {**WORKER_CMD, "command_id": 42},
                 "id_none": {**WORKER_CMD, "command_id": None}, "not_a_dict": "sleep 5",
                 "a_list": [WORKER_CMD]}
        for name, command in cases.items():
            for form, data in OPEN_FORMS.items():
                with self.subTest(case=name, form=form):
                    m, s = make(command=command)
                    feed(m, data)
                    self.assertFalse(m.stop_confirm_open, "a modal for a command that is not there")
                    self.assertTrue(m.notice, "the user is told nothing runs")
                    self.assertEqual(s.sent, [])
                    feed(m, b"x", 5.0)  # a stray confirm key must not send anything either
                    self.assertEqual(stops(s), [])

    def test_a_command_that_is_already_stopping_opens_no_second_modal(self):
        m, s = make(command={**WORKER_CMD, "state": "stopping"})
        feed(m, P + b"x")
        self.assertFalse(m.stop_confirm_open)
        self.assertTrue(m.notice)
        self.assertEqual(s.sent, [])

    def test_an_unknown_state_command_can_still_be_stopped(self):
        """``unknown`` means the end is unconfirmed: the user must be able to ask again."""
        m, s = make(command={**WORKER_CMD, "state": "unknown"})
        feed(m, P + b"x")
        self.assertTrue(m.stop_confirm_open)
        feed(m, b"x", 3.0)
        self.assertEqual([item[:3] for item in stops(s)], [("stop_command", {"command_id": ID_A}, b"")])

    def test_a_state_push_while_the_command_runs_is_followed_by_the_modal(self):
        m, s = make(command=None)
        feed(m, P + b"x")
        self.assertFalse(m.stop_confirm_open)
        m.on_state(snapshot_with(WORKER_CMD))
        feed(m, P + b"x", 3.0)
        self.assertTrue(m.stop_confirm_open)


class ConfirmTests(unittest.TestCase):
    def test_every_confirm_form_sends_exactly_one_stop_with_the_shown_id_and_no_pane_bytes(self):
        for name, data in CONFIRM_FORMS.items():
            for command in (WORKER_CMD, MANAGER_CMD):
                with self.subTest(form=name, owner=command["owner"]):
                    m, s = make(command=command)
                    t = feed(m, P + b"x")
                    self.assertTrue(m.stop_confirm_open)
                    feed(m, data, t)
                    self.assertEqual([item[:3] for item in stops(s)],
                                     [("stop_command", {"command_id": command["command_id"]}, b"")])
                    self.assertFalse(m.stop_confirm_open)
                    self.assertEqual(pane_frames(s), [])
                    self.assertEqual(len(s.sent), 1, "nothing but the stop_command was sent")
                    self.assertTrue(m.notice, "the user sees that the stop was requested")

    def test_every_other_input_cancels_sends_nothing_and_keeps_the_pane_clean(self):
        for name, data in CANCEL_FORMS.items():
            with self.subTest(form=name):
                m, s = make()
                t = feed(m, P + b"x")
                self.assertTrue(m.stop_confirm_open)
                feed(m, data, t)
                self.assertFalse(m.stop_confirm_open, f"{name} left the modal open")
                self.assertEqual(stops(s), [], f"{name} confirmed the stop")
                self.assertEqual(s.of("kill_pane"), [], f"{name} killed the shell")
                self.assertEqual(s.of("pause") + s.of("resume"), [], f"{name} paused automation")
                self.assertEqual(pane_frames(s), [], f"{name} leaked to a pane")

    def test_multi_key_reads_that_start_with_a_confirm_key_cancel_with_a_notice(self):
        for name in ("xx", "x_enter", "xyz", "ctrl_x_twice", "jamo_jamo", "x_then_jamo"):
            with self.subTest(form=name):
                m, s = make()
                t = feed(m, P + b"x")
                feed(m, CANCEL_FORMS[name], t)
                self.assertEqual(stops(s), [])
                self.assertTrue(m.notice, "a cancelled confirmation says so")

    def test_a_lone_prefix_leaves_the_modal_open_and_the_next_key_decides(self):
        m, s = make()
        t = feed(m, P + b"x")
        t = feed(m, P, t)
        self.assertTrue(m.stop_confirm_open)
        feed(m, b"x", t)
        self.assertEqual(len(stops(s)), 1)

    def test_a_resize_cancels_and_never_confirms(self):
        m, s = make()
        t = feed(m, P + b"x")
        m.resize(44, 160)
        self.assertFalse(m.stop_confirm_open)
        self.assertEqual((stops(s), pane_frames(s)), ([], []))
        feed(m, b"x", t)  # now an ordinary key for the focused pane
        self.assertEqual(stops(s), [])

    def test_cross_confirmation_between_modals_is_impossible(self):
        # the stop modal is not confirmed by the kill/pause keys and neither of those modals by the stop keys
        for stop_modal_key in (b"k", b"\x0b", JAMO_K, b"p", b"\x10", JAMO_P):
            with self.subTest(stop_modal_key=stop_modal_key):
                m, s = make()
                t = feed(m, P + b"x")
                feed(m, stop_modal_key, t)
                self.assertEqual(stops(s) + s.of("kill_pane") + s.of("pause") + s.of("resume"), [])
        for opener in (P + b"k", P + b"\x0b", P + b"p", P + b"\x10"):
            for confirm_key in (b"x", b"\x18", JAMO_X):
                with self.subTest(opener=opener, confirm_key=confirm_key):
                    m, s = make()
                    t = feed(m, opener)
                    feed(m, confirm_key, t)
                    self.assertEqual(stops(s) + s.of("kill_pane") + s.of("pause") + s.of("resume"), [])

    def test_the_command_changing_between_open_and_confirm_stops_the_one_that_was_shown(self):
        for newer in (MANAGER_CMD, None, {**WORKER_CMD, "command_id": ID_B, "label": "rm -rf build"},
                      {**WORKER_CMD, "state": "stopping"}):
            with self.subTest(newer=newer):
                m, s = make(command=WORKER_CMD)
                t = feed(m, P + b"x")
                shown_before = list(m.stop_confirm_lines())
                m.on_state(snapshot_with(newer))
                self.assertTrue(m.stop_confirm_open, "a state push closed the modal")
                self.assertEqual(m.stop_confirm_lines(), shown_before, "the modal changed what it asks")
                self.assertEqual(s.sent, [])
                feed(m, b"x", t)
                self.assertEqual([item[1] for item in stops(s)], [{"command_id": ID_A}])

    def test_a_command_that_finished_while_the_modal_was_open_is_still_asked_about_by_id(self):
        """The backend answers a stale id with already_exited/not_found and signals nothing; the UI must not guess."""
        m, s = make(command=WORKER_CMD)
        t = feed(m, P + b"x")
        m.on_state(snapshot_with(None))
        feed(m, b"x", t)
        self.assertEqual([item[1] for item in stops(s)], [{"command_id": ID_A}])

    def test_the_second_modal_after_a_confirm_asks_about_the_command_of_that_moment(self):
        m, s = make(command=WORKER_CMD)
        t = feed(m, P + b"x")
        t = feed(m, b"x", t)
        request_id = ack(m, s)
        final(m, request_id, {"status": "exited", "exit_code": 130, "signal_name": "SIGINT"})
        m.on_state(snapshot_with(MANAGER_CMD))
        t = feed(m, P + b"x", t)
        self.assertTrue(m.stop_confirm_open)
        self.assertIn("manager", "\n".join(m.stop_confirm_lines()))
        feed(m, b"x", t)
        self.assertEqual([item[1] for item in stops(s)], [{"command_id": ID_A}, {"command_id": ID_B}])

    def test_a_second_prefix_x_while_the_first_stop_is_unanswered_opens_nothing_and_sends_nothing(self):
        m, s = make()
        t = feed(m, P + b"x")
        t = feed(m, b"x", t)
        self.assertEqual(len(stops(s)), 1)
        t = feed(m, P + b"x", t)
        self.assertFalse(m.stop_confirm_open, "a modal for a stop whose answer is still pending")
        self.assertTrue(m.notice)
        feed(m, b"x", t)
        self.assertEqual(len(stops(s)), 1, "double confirmation sent a second stop")

    def test_an_ordinary_x_after_a_confirm_is_pane_input_not_another_stop(self):
        m, s = make()
        t = feed(m, P + b"x")
        t = feed(m, b"x", t)
        feed(m, b"x", t)
        self.assertEqual(len(stops(s)), 1)
        self.assertEqual(s.payloads("input", "manager_omp"), b"x")

    def test_the_stop_request_id_is_tracked_so_a_refusal_is_attributed_to_the_stop(self):
        m, s = make()
        t = feed(m, P + b"x")
        feed(m, b"x", t)
        request_id = stops(s)[0][3]
        m.on_result(ui_v1.result(request_id, False, reason=ui_v1.Reason.QUEUE_FULL, detail="too many stops"))
        self.assertIn("queue_full", m.notice)
        self.assertNotIn("중지됨", m.notice)
        # the refusal ended the request: a new attempt is possible
        feed(m, P + b"x", 9.0)
        self.assertTrue(m.stop_confirm_open)

    def test_a_not_attached_refusal_is_visible(self):
        m, s = make()
        t = feed(m, P + b"x")
        feed(m, b"x", t)
        m.on_result(ui_v1.result(stops(s)[0][3], False, reason=ui_v1.Reason.NOT_ATTACHED, detail="attach first"))
        self.assertIn("not_attached", m.notice)


class OutcomeTests(unittest.TestCase):
    """The final outcome is shown by what it says, and only ``exited`` reads as a stop."""

    def confirm(self, command=WORKER_CMD):
        m, s = make(command=command)
        t = feed(m, P + b"x")
        feed(m, b"x", t)
        return m, s, stops(s)[0][3]

    def test_the_ack_shows_the_stop_in_progress(self):
        m, s, request_id = self.confirm()
        before = m.notice
        ack(m, s)
        self.assertTrue(m.notice)
        self.assertNotEqual(m.notice, before, "the user is told the stop is under way")
        self.assertNotIn("중지됨", m.notice, "an ack is not a completed stop")
        self.assertNotIn("불명", m.notice)

    def test_exited_is_the_only_status_shown_as_a_completed_stop(self):
        results = {}
        for status, extra in (("exited", {"exit_code": 130, "signal": 2, "signal_name": "SIGINT"}),
                              ("unknown", {"reason": "still_alive_after_sigkill"}),
                              ("unknown", {"reason": "permission_denied"}),
                              ("error", {"reason": "internal_error", "detail": "stop failed: RuntimeError"}),
                              ("refused", {"reason": "not_owner"}),
                              ("refused", {"reason": "command_stop_unavailable"}),
                              ("stopping", {}),
                              ("not_found", {"reason": "unknown_command"}),
                              ("already_exited", {"exit_code": 0}),
                              ("stale", {"reason": "identity_mismatch"}),
                              ("exited ", {}), ("EXITED", {}), ("done", {}), ("", {}), (None, {}), (7, {}),
                              (["exited"], {})):
            m, s, request_id = self.confirm()
            ack(m, s)
            final(m, request_id, {"status": status, **extra})
            results[(str(status), tuple(sorted(extra.items(), key=str)))] = m.notice
        success = results[("exited", (("exit_code", 130), ("signal", 2), ("signal_name", "SIGINT")))]
        self.assertIn("130", success)
        for key, notice in results.items():
            if key[0] == "exited" and key[1]:
                continue
            with self.subTest(status=key):
                self.assertTrue(notice, "every outcome says something")
                self.assertNotEqual(notice, success, "a non-exited outcome reads as the success notice")
                self.assertNotIn("중지됨", notice, f"{key} is shown as a completed stop: {notice!r}")

    def test_unknown_and_error_mention_the_uncertainty_and_the_reason(self):
        m, s, request_id = self.confirm()
        ack(m, s)
        final(m, request_id, {"status": "unknown", "reason": "still_alive_after_sigkill"})
        self.assertIn("still_alive_after_sigkill", m.notice)
        m, s, request_id = self.confirm()
        ack(m, s)
        final(m, request_id, {"status": "error", "reason": "internal_error", "detail": "stop failed: KeyError"})
        self.assertIn("internal_error", m.notice)
        self.assertIn("KeyError", m.notice)

    def test_refused_names_the_reason_and_stopping_reads_as_in_progress(self):
        m, s, request_id = self.confirm()
        ack(m, s)
        final(m, request_id, {"status": "refused", "reason": "not_owner"})
        self.assertIn("not_owner", m.notice)
        m, s, request_id = self.confirm()
        ack(m, s)
        final(m, request_id, {"status": "stopping", "command_id": ID_A})
        self.assertTrue(m.notice)
        self.assertNotIn("중지됨", m.notice)
        self.assertNotIn("실패", m.notice)

    def test_an_exited_notice_carries_the_exit_code_and_signal(self):
        m, s, request_id = self.confirm()
        ack(m, s)
        final(m, request_id, {"status": "exited", "exit_code": 137, "signal": 9, "signal_name": "SIGKILL"})
        self.assertIn("137", m.notice)
        self.assertIn("SIGKILL", m.notice)

    def test_malformed_final_frames_never_crash_and_never_read_as_success(self):
        bad = ({"id": None, "ok": True, "final": True}, {"final": True, "stop": None}, {"final": True, "stop": "exited"},
               {"final": True, "stop": []}, {"final": True, "stop": {"status": {"x": 1}}},
               {"final": True, "stop": {"status": "exited", "exit_code": {"nested": 1}}},
               {"final": True, "ok": False, "reason": "internal_error", "detail": "boom"})
        for header in bad:
            with self.subTest(header=header):
                m, s, request_id = self.confirm()
                ack(m, s)
                m.on_result({"v": 1, "type": "result", **({"id": request_id} if "id" not in header else {}), **header})
                self.assertTrue(m.notice)
                if not (isinstance(header.get("stop"), dict) and header["stop"].get("status") == "exited"):
                    self.assertNotIn("중지됨", m.notice)

    def test_a_final_that_arrives_before_its_ack_is_not_overwritten_by_the_ack(self):
        m, s, request_id = self.confirm()
        final(m, request_id, {"status": "exited", "exit_code": 130, "signal_name": "SIGINT"})
        shown = m.notice
        self.assertIn("130", shown)
        ack(m, s)
        self.assertEqual(m.notice, shown, "a late ack replaced the final outcome")

    def test_a_final_and_a_later_unrelated_result_keep_the_final_notice_until_something_else_says_more(self):
        m, s, request_id = self.confirm()
        ack(m, s)
        final(m, request_id, {"status": "unknown", "reason": "left_session_descendants"})
        shown = m.notice
        m.on_result({"v": 1, "type": "result", "id": "unrelated", "ok": True})
        self.assertEqual(m.notice, shown)

    def test_a_final_without_a_pending_stop_still_shows_its_outcome(self):
        """After a reconnect the UI may no longer track the id; the user must still see the result."""
        m, s = make()
        final(m, "gone", {"status": "unknown", "reason": "pidfd_unavailable"})
        self.assertIn("pidfd_unavailable", m.notice)
        self.assertNotIn("중지됨", m.notice)

    def test_the_stop_in_progress_notice_is_not_a_success_while_the_snapshot_says_stopping(self):
        m, s, request_id = self.confirm()
        ack(m, s)
        m.on_state(snapshot_with({**WORKER_CMD, "state": "stopping"}))
        self.assertNotIn("중지됨", m.notice)
        feed(m, P + b"x", 5.0)
        self.assertFalse(m.stop_confirm_open)

    def test_a_state_push_after_the_final_does_not_erase_the_outcome(self):
        m, s, request_id = self.confirm()
        ack(m, s)
        final(m, request_id, {"status": "unknown", "reason": "still_alive_after_sigkill"})
        shown = m.notice
        m.on_state(snapshot_with(None))
        self.assertEqual(m.notice, shown)


class RealUiTests(unittest.TestCase):
    """The real product UI on a PTY against a scripted server: modal drawn, confirm sent, outcome drawn."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="csO10-ui-", dir=tempfile.gettempdir()))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.server = ScriptedServer(snapshot=snapshot_with(WORKER_CMD, extra={"input_owner": "manager"}),
                                     answers={"stop_command": self.answer})
        self.addCleanup(self.server.close)
        self.ui = UiPty(self.server.path, self.work, rows=30, cols=140)
        self.addCleanup(self.ui.close)
        self.assertTrue(self.server.attached.wait(10), "UI did not attach")
        self.assertTrue(self.ui.wait_text("manager", 10))

    @staticmethod
    def answer(header, payload):
        return {"command_id": header.get("command_id"), "stop": {"status": "stopping"}}

    def kinds(self):
        return [f.header.get("type") for f in self.server.frames]

    def test_modal_confirm_ack_final_and_cancel_through_the_real_ui(self):
        ui, server = self.ui, self.server
        ui.send(P + b"x")
        self.assertTrue(ui.wait_text("heavy_marker", 5), ui.screen_text())
        self.assertIn("worker", ui.screen_text())
        ui.send(b"k")  # any other key cancels
        self.assertTrue(ui.wait_for(lambda: "heavy_marker" not in ui.screen_text(), 5), ui.screen_text())
        time.sleep(0.3)
        self.assertNotIn("stop_command", self.kinds())
        self.assertEqual(server.payloads("input") + server.payloads("paste"), b"")
        ui.send(P + "ㅌ".encode())
        self.assertTrue(ui.wait_text("heavy_marker", 5))
        time.sleep(0.8)  # the shared IME run window (0.5 s after a jamo command) swallows a key typed inside it
        ui.send(b"\x18")
        self.assertTrue(server.wait(lambda: "stop_command" in self.kinds(), 5), self.kinds())
        (frame,) = [f for f in server.frames if f.header.get("type") == "stop_command"]
        self.assertEqual(frame.header.get("command_id"), ID_A)
        self.assertEqual(frame.payload, b"")
        self.assertEqual(server.payloads("input") + server.payloads("paste"), b"")
        server.send(ui_v1.result(frame.header["id"], True, final=True, command_id=ID_A,
                                 stop={"status": "unknown", "reason": "still_alive_after_sigkill"}))
        self.assertTrue(ui.wait_text("still_alive_after_sigkill", 5), ui.screen_text())
        self.assertNotIn("중지됨", ui.screen_text())

    def test_no_command_shows_the_notice_on_screen_and_sends_nothing(self):
        ui, server = self.ui, self.server
        server.state(snapshot_with(None, extra={"input_owner": "manager"}))
        time.sleep(0.3)
        ui.send(P + b"x")
        self.assertTrue(ui.wait_text("중지할 Workbench 명령이 없습니다", 5), ui.screen_text())
        time.sleep(0.2)
        self.assertNotIn("stop_command", self.kinds())

    def test_an_ack_then_a_successful_final_reads_as_stopped(self):
        ui, server = self.ui, self.server
        ui.send(P + b"x")
        self.assertTrue(ui.wait_text("heavy_marker", 5))
        ui.send(b"x")
        self.assertTrue(server.wait(lambda: "stop_command" in self.kinds(), 5))
        frame = [f for f in server.frames if f.header.get("type") == "stop_command"][0]
        server.send(ui_v1.result(frame.header["id"], True, final=True, command_id=ID_A,
                                 stop={"status": "exited", "exit_code": 130, "signal": 2, "signal_name": "SIGINT"}))
        self.assertTrue(ui.wait_text("130", 5), ui.screen_text())


if __name__ == "__main__":
    unittest.main()
