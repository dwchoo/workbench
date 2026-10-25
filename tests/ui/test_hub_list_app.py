"""TUI Hub list app (HB-08 review fixes, seam S7 with fakes): the key map, the screen loop's failure
handling, the poller thread and what happens around ``run_product`` / ``power_on`` / the stop prompt.

Curses, the Hub, ``run_product`` and ``projects.power_on`` are replaced by small fakes; the real thing is
exercised end to end by ``tests/hub/test_hub_cli_bare.py`` (pty). Expected values are the review items'
sentences and SPEC "TUI Hub 목록".
"""
from __future__ import annotations

import contextlib
import curses
import io
from pathlib import Path
import sys
import termios
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workbench.hub import projects  # noqa: E402
from workbench.ui.hub_list import app  # noqa: E402
from workbench.ui.hub_list.model import Attach, HubListModel, PowerOn, Quit, entry_of  # noqa: E402

from test_hub_list_model import loaded, row  # noqa: E402


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class KeyNames(unittest.TestCase):
    def test_resize_is_no_key_at_all(self):
        self.assertEqual(app.key_name(curses.KEY_RESIZE), "")

    def test_korean_two_set_jamo_are_their_letter_keys(self):
        for jamo, letter in (("ㅛ", "y"), ("ㅜ", "n"), ("ㅐ", "o"), ("ㅌ", "x"), ("ㄱ", "r"), ("ㅂ", "q")):
            self.assertEqual(app.key_name(jamo), letter, jamo)

    def test_the_plain_keys_keep_their_names(self):
        self.assertEqual(app.key_name(curses.KEY_UP), "up")
        self.assertEqual(app.key_name("\n"), "enter")
        self.assertEqual(app.key_name("\x1b"), "esc")
        self.assertEqual(app.key_name("j"), "j")


class FakeScreen:
    """Enough of a curses window for ``draw`` and ``_screen``; ``keys`` are served by ``get_wch``."""

    def __init__(self, keys):
        self.keys = list(keys)

    def getmaxyx(self):
        return 24, 100

    def erase(self): pass

    def addnstr(self, *args): pass

    def refresh(self): pass

    def keypad(self, flag): pass

    def timeout(self, ms): pass

    def get_wch(self):
        item = self.keys.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakePoller:
    def __init__(self):
        self.calls = []

    def drain(self): return []

    def refresh(self, fresh=True): self.calls.append(("refresh", fresh))

    def start(self): self.calls.append("start")

    def stop(self): self.calls.append("stop")

    def join(self, timeout=None): pass

    def pause(self): self.calls.append("pause")

    def resume(self): self.calls.append("resume")


class ScreenLoop(unittest.TestCase):
    def test_a_resize_does_not_cancel_the_question(self):
        model = loaded(row("a", "off"))
        model.press("enter")
        self.assertIsNotNone(model.modal)
        action = app._screen(FakeScreen([curses.KEY_RESIZE, "y"]), model, FakePoller())
        self.assertIsInstance(action, PowerOn)

    def test_a_jamo_answers_the_question(self):
        model = loaded(row("a", "off"))
        model.press("enter")
        self.assertIsInstance(app._screen(FakeScreen(["ㅛ"]), model, FakePoller()), PowerOn)

    def test_a_dead_terminal_ends_the_loop_instead_of_spinning(self):
        screen = FakeScreen([curses.error("no input")] * 500)
        with self.assertRaises(curses.error):
            app._screen(screen, loaded(row("a")), FakePoller())
        self.assertGreater(len(screen.keys), 400)  # it gave up early, not after draining everything

    def test_a_few_quick_errors_are_survived(self):
        screen = FakeScreen([curses.error("no input")] * 5 + ["q"])
        self.assertIsInstance(app._screen(screen, loaded(row("a")), FakePoller()), Quit)

    def test_slow_timeouts_never_count_as_a_dead_terminal(self):
        screen = FakeScreen([curses.error("no input")] * (app.MAX_QUICK_ERRORS * 2) + ["q"])
        clock = iter(range(0, 10 ** 6, 1))  # every get_wch "waited" a whole second
        with mock.patch.object(app.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertIsInstance(app._screen(screen, loaded(row("a")), FakePoller()), Quit)


class RunHubList(unittest.TestCase):
    def run_list(self, wrapper, tty=(True, True, False), execute=None):
        poller = FakePoller()
        ttys = iter(tty)
        with mock.patch.object(app.curses, "wrapper", side_effect=wrapper) as wrap, \
                mock.patch.object(app, "_tty_ok", side_effect=lambda *_: next(ttys)), \
                mock.patch.object(app, "execute", side_effect=execute or (lambda *_: None)):
            code = app.run_hub_list(poller=poller)
        return code, wrap.call_count, poller

    def test_a_terminal_that_is_gone_after_an_action_ends_the_app_with_1(self):
        entry = loaded(row("a")).selected_row
        actions = iter([Attach(entry_of(entry)), Quit()])
        code, wrapped, poller = self.run_list(lambda *_: next(actions), tty=(True, False))
        self.assertEqual((code, wrapped), (1, 1))
        self.assertIn("stop", poller.calls)

    def test_a_still_valid_terminal_returns_to_the_list(self):
        actions = iter([Attach(entry_of(row("a"))), Quit()])
        code, wrapped, _ = self.run_list(lambda *_: next(actions), tty=(True, True))
        self.assertEqual((code, wrapped), (0, 2))

    def test_a_curses_error_ends_the_app_with_1(self):
        def broken(*_):
            raise curses.error("setupterm failed")
        code, _, poller = self.run_list(broken)
        self.assertEqual(code, 1)
        self.assertIn("stop", poller.calls)


class PollerThread(unittest.TestCase):
    def poller(self, fetch, interval=60.0):
        poller = app.HubPoller(fetch=fetch, interval=interval)
        poller.start()
        self.addCleanup(lambda: (poller.stop(), poller.join(timeout=2)))
        return poller

    def test_a_failing_fetch_becomes_a_lost_event_and_the_thread_lives_on(self):
        calls = []

        def fetch(fresh):
            calls.append(fresh)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return ("rows", [], "")
        poller = self.poller(fetch)
        events = []
        self.assertTrue(wait_until(lambda: events.extend(poller.drain()) or events))
        self.assertEqual(events[0], ("lost", "RuntimeError"))
        poller.refresh()
        self.assertTrue(wait_until(lambda: events.extend(poller.drain()) or len(events) > 1))
        self.assertEqual(events[1][0], "rows")

    def test_a_refresh_during_a_fetch_is_not_lost(self):
        started, release, calls = threading.Event(), threading.Event(), []

        def fetch(fresh):
            calls.append(fresh)
            if len(calls) == 1:
                started.set()
                release.wait(3)
            return ("rows", [], "")
        poller = self.poller(fetch)
        self.assertTrue(started.wait(3))
        poller.refresh(fresh=True)  # while the first fetch is still running
        release.set()
        self.assertTrue(wait_until(lambda: len(calls) >= 2), calls)
        self.assertEqual(calls[1], True)


class AroundRunProduct(unittest.TestCase):
    def attach(self, status):
        model = loaded(row("a"))
        identity = types.SimpleNamespace(state="running", layout=types.SimpleNamespace(ui_socket="/s"),
                                         record={"processes": {"backend": {"pid": 1, "start_ticks": 2}}})
        with mock.patch.object(app, "check_entry", return_value=None), \
                mock.patch("workbench.backend.process_identity.inspect_backend", return_value=identity), \
                mock.patch("workbench.ui.product.run_product", return_value=status), \
                mock.patch.object(app, "_wait_enter") as wait:
            code = app._attach(entry_of(model.selected_row), model)
        return code, model.notice, wait

    def test_a_closed_or_failed_session_waits_for_enter_so_the_cause_can_be_read(self):
        code, notice, wait = self.attach(1)
        self.assertIsNone(code)
        wait.assert_called_once()
        self.assertIn("연결이 끝났습니다", notice)

    def test_a_detach_returns_at_once(self):
        code, notice, wait = self.attach(0)
        self.assertIsNone(code)
        wait.assert_not_called()
        self.assertIn("detach했습니다", notice)

    def test_no_tty_for_the_product_ui_ends_the_app_with_2(self):
        self.assertEqual(self.attach(2)[0], 2)


class PowerOnOutput(unittest.TestCase):
    def power_on(self, outcome, text="starting backend: shell bash\nnote: later\n"):
        seen = {}

        def fake(entry, attach, out=None):
            seen["out"] = out
            if out is not None:
                out.write(text)
            return outcome(text) if callable(outcome) else outcome
        model = HubListModel()
        screen = io.StringIO()
        with contextlib.redirect_stdout(screen), mock.patch.object(projects, "power_on", side_effect=fake), \
                mock.patch.object(app, "_wait_enter"):
            code = app._power_on(PowerOn(entry_of(row("a", "off")), False), model)
        return code, model, screen.getvalue(), seen

    def test_the_start_output_is_shown_to_the_user_as_it_is_written(self):
        code, model, screen, seen = self.power_on(lambda text: projects.Outcome(True, data={"output": text}))
        self.assertIsNone(code)
        self.assertIsNotNone(seen["out"])  # the user sees it: a real stream, not None
        self.assertIn("starting backend: shell bash", screen)
        self.assertIn("note: later", screen)
        self.assertIn("starting backend: shell bash", model.notice)

    def test_a_failed_start_is_not_printed_twice(self):
        outcome = projects.Outcome(False, projects.START_REQUIREMENT, "wb start exit 2", {})
        _, model, screen, _ = self.power_on(outcome, text="OMP selection failed\n")
        self.assertEqual(screen.count("OMP selection failed"), 1)
        self.assertIn("켜지 못했습니다", screen)


class StaleInput(unittest.TestCase):
    def test_typeahead_is_flushed_before_the_stop_prompt_and_the_enter_wait(self):
        with mock.patch.object(app.termios, "tcflush") as flush, \
                mock.patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(app._ask_stop([]))
            flush.assert_called_with(0, termios.TCIFLUSH)
            flush.reset_mock()
            app._wait_enter()
            flush.assert_called_with(0, termios.TCIFLUSH)

    def test_a_flush_error_is_ignored(self):
        with mock.patch.object(app.termios, "tcflush", side_effect=termios.error(25, "not a tty")), \
                mock.patch("builtins.input", return_value=""):
            app._wait_enter()


if __name__ == "__main__":
    unittest.main()
