"""TUI Hub list header for the 2026-10-27 user decision (multi-address ``bind``, ``wb hub reload``).

The header shows every listening URL, when the running ``hub.toml`` was loaded and the last refused reload, and the
disabled hint says ``wb hub reload`` (not a Hub restart). Expected values are the status fields the Hub sends
(``web.urls``, ``config.loaded_at``, ``config.reload_error``). No curses, no Hub: the model and ``screen_lines``.
Run with ``discover -s tests/ui -p 'test_hub_list*'``.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_hub_list_status import NOW, hub_text, model_with, status  # noqa: E402

LOADED = NOW - 600


def clock(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%H:%M")


class ReloadHeader(unittest.TestCase):
    def text(self, **parts) -> str:
        return " ".join(hub_text(model_with(status(**parts)), cols=200))

    def test_the_disabled_hint_says_wb_hub_reload(self):
        text = self.text(web={"state": "disabled", "config": "/h/hub.toml"})
        self.assertIn("wb hub reload", text)
        self.assertNotIn("재시작", text)

    def test_every_listening_url_is_shown(self):
        text = self.text(web={"state": "listening", "url": "http://127.0.0.1:8780",
                              "urls": ["http://127.0.0.1:8780", "http://100.64.1.2:8780"]})
        self.assertIn("web: listening http://127.0.0.1:8780 http://100.64.1.2:8780", text)

    def test_the_config_load_time_is_shown(self):
        text = self.text(config={"path": "/h/hub.toml", "loaded_at": LOADED, "state": "ok", "reload_error": None})
        self.assertIn(f"config {clock(LOADED)}", text)
        self.assertNotIn("reload 실패", text)

    def test_a_refused_reload_is_shown_with_its_reason(self):
        text = self.text(config={"path": "/h/hub.toml", "loaded_at": LOADED, "state": "ok",
                                 "reload_error": {"reason": "config_invalid:web.bind", "at": NOW - 5}})
        self.assertIn("reload 실패: config_invalid:web.bind", text)

    def test_an_older_hub_without_config_shows_no_config_part(self):
        text = self.text()
        self.assertNotIn("config ", text)
        self.assertEqual(len(hub_text(model_with(status()))), 2)


if __name__ == "__main__":
    unittest.main()
