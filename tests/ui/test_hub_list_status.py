"""TUI Hub list: the Hub's own status header (2026-10-26 user request).

The status is the hub_v1 ``status`` reply ``wb hub status`` prints (``hub``, ``registry``, ``web``,
``board_agent``). Expected strings are the request's sentences: pid/version/registered count, the four
web states (disabled with the hub.toml hint, ``listening <url>`` + plain-HTTP marker, ``not_listening:
<reason>``, ``config_invalid:<item>``), the Board agent state, "Hub 상태 불명" for anything not fetched
(never ``disabled``), no line wider than the terminal, and a refresh on ``r`` and on the poll timer.
No curses, no Hub: the model, ``screen_lines`` and the poller with fake fetches.
Run with ``discover -s tests/ui -p 'test_hub_list*'``.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

from wcwidth import wcswidth

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workbench.ui.hub_list import app  # noqa: E402
from workbench.ui.hub_list.model import HubListModel  # noqa: E402
from workbench.ui.hub_list.view import screen_lines  # noqa: E402

from test_hub_list_app import FakePoller, FakeScreen, wait_until  # noqa: E402
from test_hub_list_model import loaded, row  # noqa: E402

NOW = 1_000_000.0
UNKNOWN = "Hub 상태 불명"


def status(**parts) -> dict:
    """A full ``status`` reply; keyword parts replace the defaults."""
    reply = {"ok": True,
             "hub": {"pid": 4242, "mode": "background", "started_at": NOW - 3 * 3600 - 125,
                     "version": {"release": "0.3.1", "commit": "abcdef0123456789", "kind": "git"}},
             "registry": {"state": "ok", "count": 5, "path": "/h/registry.json"},
             "web": {"state": "disabled", "config": "/h/hub.toml"},
             "board_agent": None}
    reply.update(parts)
    return reply


def model_with(reply: dict | None) -> HubListModel:
    model = loaded(row("alpha"))
    if reply is None:
        model.set_hub_unknown("not_running")
    else:
        model.set_hub_status(reply)
    return model


def hub_text(model: HubListModel, rows: int = 14, cols: int = 120) -> list[str]:
    return [line.text for line in screen_lines(model, rows, cols, NOW) if line.kind == "hub"]


class HeaderFacts(unittest.TestCase):
    def test_pid_version_uptime_and_registered_count_come_from_the_status(self):
        text = " ".join(hub_text(model_with(status())))
        self.assertIn("pid 4242", text)
        self.assertIn("0.3.1", text)
        self.assertIn("5", text.split("프로젝트")[1][:6])
        self.assertIn("3시간", text)

    def test_the_header_is_above_the_project_table(self):
        kinds = [line.kind for line in screen_lines(model_with(status()), 14, 120, NOW)]
        self.assertLess(kinds.index("hub"), kinds.index("header"))
        self.assertLessEqual(kinds.count("hub"), 2)


class WebStates(unittest.TestCase):
    def web(self, web: dict) -> str:
        return " ".join(hub_text(model_with(status(web=web))))

    def test_disabled_says_how_to_turn_it_on(self):
        text = self.web({"state": "disabled", "config": "/h/hub.toml"})
        self.assertIn("web: disabled", text)
        self.assertIn("hub.toml", text)
        # hub.toml always exists with `enabled = false` (2026-10-26): the hint says to flip it, not to add a line.
        self.assertIn("enabled = false", text)
        self.assertIn("true", text)
        self.assertIn("재시작", text)
        self.assertNotIn("넣고", text)

    def test_listening_shows_the_url_without_a_warning_when_secure(self):
        text = self.web({"state": "listening", "url": "https://h:8443", "insecure_warning": False})
        self.assertIn("web: listening https://h:8443", text)
        self.assertNotIn("평문", text)

    def test_listening_over_plain_http_carries_the_warning_marker(self):
        text = self.web({"state": "listening", "url": "http://0.0.0.0:8080", "insecure_warning": True})
        self.assertIn("http://0.0.0.0:8080", text)
        self.assertIn("[!]", text)
        self.assertIn("평문 HTTP", text)

    def test_not_listening_shows_the_reason(self):
        self.assertIn("web: not_listening: password_not_set", self.web({"state": "password_not_set"}))
        text = self.web({"state": "bind_failed", "detail": "address in use"})
        self.assertIn("web: not_listening: bind_failed:address in use", text)

    def test_config_invalid_shows_the_item(self):
        self.assertIn("web: config_invalid:web.port", self.web({"state": "config_invalid", "detail": "web.port"}))


class BoardAgent(unittest.TestCase):
    def board(self, board: object) -> str:
        return " ".join(hub_text(model_with(status(board_agent=board))))

    def test_no_board_agent_set_is_none(self):
        self.assertIn("Board agent: 없음", self.board(None))

    def test_running_and_stopped_come_from_the_agent_channel(self):
        running = {"agent": {"state": "running", "pid": 7}, "shell": {"state": "running", "pid": 8},
                   "lease": {"state": "none"}}
        self.assertIn("Board agent: running", self.board(running))
        stopped = {"agent": {"state": "stopped", "pid": None}, "shell": {"state": "stopped", "pid": None},
                   "lease": {"state": "none"}}
        text = self.board(stopped)
        self.assertIn("Board agent: stopped", text)
        self.assertNotIn("running", text)

    def test_a_shell_that_differs_is_named(self):
        text = self.board({"agent": {"state": "running"}, "shell": {"state": "stopped"}})
        self.assertIn("running", text)
        self.assertIn("shell stopped", text)


class UnknownAndPartial(unittest.TestCase):
    def test_before_any_status_the_header_says_loading_not_off(self):
        text = " ".join(hub_text(loaded(row("alpha"))))
        self.assertIn("불러오는 중", text)
        self.assertNotIn("disabled", text)

    def test_an_unavailable_status_is_unknown_never_disabled_and_the_list_still_draws(self):
        model = model_with(None)
        text = " ".join(hub_text(model))
        self.assertIn(UNKNOWN, text)
        self.assertNotIn("disabled", text)
        self.assertNotIn("pid", text)
        self.assertTrue(any(line.kind == "selected" for line in screen_lines(model, 14, 100, NOW)))

    def test_a_status_after_an_unknown_replaces_it_and_vice_versa(self):
        model = model_with(None)
        model.set_hub_status(status())
        self.assertNotIn(UNKNOWN, " ".join(hub_text(model)))
        model.set_hub_unknown("timeout")
        self.assertIn(UNKNOWN, " ".join(hub_text(model)))

    def test_a_failed_reply_is_unknown(self):
        model = loaded(row("alpha"))
        model.set_hub_status({"ok": False, "reason": "denied"})
        self.assertIn(UNKNOWN, " ".join(hub_text(model)))

    def test_a_partial_status_shows_what_is_known_and_unknown_for_the_rest(self):
        model = loaded(row("alpha"))
        model.set_hub_status({"ok": True, "hub": {"pid": 77}})  # no version, registry, web or board_agent key
        text = " ".join(hub_text(model))
        self.assertIn("pid 77", text)
        self.assertIn(UNKNOWN, text)
        self.assertIn("web: 불명", text)
        self.assertNotIn("disabled", text)
        self.assertNotIn("Board agent: 없음", text)  # a missing key is not "no Board agent"
        self.assertIn("Board agent: 불명", text)

    def test_garbage_shapes_never_crash(self):
        for reply in ({"ok": True}, {"ok": True, "hub": "x", "registry": 3, "web": [], "board_agent": "x"},
                      {"ok": True, "hub": {"pid": "7", "started_at": "now", "version": 5}, "web": {"state": 1}},
                      {"ok": True, "hub": {}, "web": {}, "registry": {"count": "many"}}, [], "s", 5):
            model = loaded(row("alpha"))
            model.set_hub_status(reply)
            for cols in (20, 120):
                hub_text(model, cols=cols)

    def test_an_unreadable_registry_is_not_a_count_of_zero(self):
        text = " ".join(hub_text(model_with(status(registry={"state": "invalid", "count": 0, "path": "/p"}))))
        self.assertNotIn("프로젝트 0", text)
        self.assertIn("registry 손상", text)

    def test_text_from_the_hub_has_no_control_characters(self):
        web = {"state": "listening", "url": "http://x\x1b[31m\x07", "insecure_warning": True}
        for text in hub_text(model_with(status(web=web))):
            self.assertTrue(all(ch.isprintable() for ch in text), repr(text))


class Width(unittest.TestCase):
    def test_no_header_line_is_wider_than_the_terminal(self):
        replies = (status(web={"state": "listening", "url": "http://매우긴호스트이름.example.com:8080/아주/긴/경로",
                               "insecure_warning": True},
                          board_agent={"agent": {"state": "running", "pid": 7}, "shell": {"state": "실패함"}}),
                   status(web={"state": "bind_failed", "detail": "주소가 이미 사용 중입니다" * 3}),
                   status(), None)
        for reply in replies:
            for cols in (12, 20, 40, 60, 120):
                with self.subTest(cols=cols, web=(reply or {}).get("web")):
                    model = model_with(reply)
                    texts = hub_text(model, cols=cols)  # the project table's own 12-column overflow is not this header
                    self.assertTrue(texts)
                    for text in texts:
                        self.assertLessEqual(wcswidth(text), cols - 1, text)
                    if cols >= 20:  # and above that the whole screen fits
                        for line in screen_lines(model, 14, cols, NOW):
                            self.assertLessEqual(wcswidth(line.text), cols - 1, line.text)

    def test_a_cut_line_ends_in_an_ellipsis(self):
        texts = hub_text(model_with(status()), cols=40)
        self.assertTrue(any(text.endswith("…") for text in texts), texts)

    def test_a_short_terminal_still_leaves_the_selected_row(self):
        model = model_with(status())
        lines = screen_lines(model, 8, 100, NOW)
        self.assertLessEqual(len(lines), 8)
        self.assertTrue(any(line.kind == "selected" for line in lines))


class Refresh(unittest.TestCase):
    def test_the_screen_applies_status_events_and_r_asks_the_poller_to_refresh(self):
        events = [[("rows", [row("alpha")], ""), ("hub", status(), "")]]

        class Poller(FakePoller):
            def drain(self_inner):
                return events.pop(0) if events else []
        model, poller = HubListModel(), Poller()
        app._screen(FakeScreen(["r", "q"]), model, poller)
        self.assertIn(("refresh", True), poller.calls)
        self.assertIn("web: disabled", " ".join(hub_text(model)))

    def test_a_later_status_event_updates_the_header_and_a_lost_one_makes_it_unknown(self):
        batches = [[("hub", status(), "")],
                   [("hub", status(web={"state": "listening", "url": "http://h:1", "insecure_warning": True}), "")],
                   [("hub", None, "timeout")]]
        seen = []

        class Poller(FakePoller):
            def drain(self_inner):
                return batches.pop(0) if batches else []

        class Screen(FakeScreen):
            def get_wch(self_inner):
                seen.append(" ".join(hub_text(model)))
                return super().get_wch()
        model = HubListModel()
        app._screen(Screen([app.curses.error("t"), app.curses.error("t"), "q"]), model, Poller())
        self.assertIn("web: disabled", seen[0])
        self.assertIn("web: listening http://h:1", seen[1])
        self.assertIn(UNKNOWN, seen[2])
        self.assertNotIn("disabled", seen[2])


class PollerStatusCycle(unittest.TestCase):
    def test_the_poller_fetches_the_status_on_each_cycle_and_on_refresh(self):
        replies = [status(), status(web={"state": "listening", "url": "http://h:1", "insecure_warning": False})]
        calls = []

        def fetch_hub():
            calls.append("hub")
            return ("hub", replies[min(len(calls) - 1, 1)], "")
        poller = app.HubPoller(fetch=lambda fresh: ("rows", [], ""), fetch_hub=fetch_hub, interval=60.0)
        poller.start()
        self.addCleanup(lambda: (poller.stop(), poller.join(timeout=2)))
        events = []
        self.assertTrue(wait_until(lambda: events.extend(poller.drain()) or any(e[0] == "hub" for e in events)))
        poller.refresh()
        self.assertTrue(wait_until(lambda: events.extend(poller.drain()) or
                                   sum(1 for e in events if e[0] == "hub") >= 2))
        hubs = [e for e in events if e[0] == "hub"]
        self.assertEqual(hubs[0][1]["web"]["state"], "disabled")
        self.assertEqual(hubs[1][1]["web"]["state"], "listening")

    def test_a_status_fetch_that_raises_is_an_unknown_event_and_the_list_still_polls(self):
        def fetch_hub():
            raise RuntimeError("boom")
        poller = app.HubPoller(fetch=lambda fresh: ("rows", [], ""), fetch_hub=fetch_hub, interval=60.0)
        poller.start()
        self.addCleanup(lambda: (poller.stop(), poller.join(timeout=2)))
        events = []
        self.assertTrue(wait_until(lambda: events.extend(poller.drain()) or len(events) >= 2))
        self.assertIn(("hub", None, "RuntimeError"), events)
        self.assertTrue(any(e[0] == "rows" for e in events))

    def test_a_poller_with_a_custom_list_fetch_asks_no_hub_unless_told_to(self):
        poller = app.HubPoller(fetch=lambda fresh: ("rows", [], ""), interval=60.0)
        self.assertIsNone(poller.fetch_hub)
        self.assertIs(app.HubPoller().fetch_hub, app.fetch_hub_status)

    def test_fetch_hub_status_maps_failures_to_unknown(self):
        from unittest import mock
        from workbench.hub.process import HubNotRunning, HubUnresponsive

        class Client:
            def __init__(self, reply=None, error=None):
                self.reply, self.error = reply, error

            def __enter__(self_inner):
                if self_inner.error:
                    raise self_inner.error
                return self_inner

            def __exit__(self, *exc): return False

            def request(self, op, **fields):
                assert op == "status"
                return self.reply
        cases = ((Client(error=HubNotRunning()), ("hub", None, "not_running")),
                 (Client(error=HubUnresponsive("timeout")), ("hub", None, "timeout")),
                 (Client(reply={"ok": False, "reason": "denied"}), ("hub", None, "denied")),
                 (Client(reply={"ok": True, "hub": {"pid": 1}}), ("hub", {"ok": True, "hub": {"pid": 1}}, "")))
        for client, expected in cases:
            with mock.patch.object(app, "HubClient", return_value=client), mock.patch.object(app, "hub_layout"):
                self.assertEqual(app.fetch_hub_status(), expected)


if __name__ == "__main__":
    unittest.main()
