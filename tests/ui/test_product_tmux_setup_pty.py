"""The real product UI on a PTY inside a private tmux server: the setup prompt runs before the UI, its notice is
the footer. HOME, XDG_CONFIG_HOME and TMUX all point into a temp dir: the user's tmux and config are never touched."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from support import FixtureServer  # noqa: E402
from test_product_pty import CODE, SRC, UiProcess  # noqa: E402


class TmuxUiProcess(UiProcess):
    def __init__(self, sock_path, env, size=(30, 120)):
        import pyte  # noqa: F401
        from support import rep_screen_classes
        master, slave = os.openpty()
        self.set_size(master, size)
        self.proc = subprocess.Popen([sys.executable, "-c", CODE, str(sock_path)], stdin=slave, stdout=slave,
                                     stderr=slave, env=env, start_new_session=True, close_fds=True)
        os.close(slave)
        self.fd = master
        screen_class, stream_class = rep_screen_classes()
        self.screen = screen_class(size[1], size[0])
        self.stream = stream_class(self.screen)
        self.raw = bytearray()


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxSetupInProductUiTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tmux-ui-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.sock = str(self.root / "sock")
        base = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "xterm-256color"}
        self.addCleanup(lambda: subprocess.run(["tmux", "-S", self.sock, "kill-server"], env=base, timeout=10,
                                               capture_output=True))
        subprocess.run(["tmux", "-S", self.sock, "-f", "/dev/null", "new-session", "-d", "-s", "t"], env=base,
                       check=True, timeout=10, capture_output=True)
        pid = self.tmux("display-message", "-p", "#{pid}").strip()
        self.env = {**base, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(self.home),
                    "XDG_CONFIG_HOME": str(self.home / ".config"), "TMUX": f"{self.sock},{pid},0"}

    def tmux(self, *args):
        return subprocess.run(["tmux", "-S", self.sock, *args], env={"PATH": "/usr/bin:/bin"}, check=True,
                              timeout=10, capture_output=True, text=True).stdout

    def start(self, env):
        server = FixtureServer()
        self.addCleanup(server.close)
        ui = TmuxUiProcess(server.path, env)
        self.addCleanup(ui.close)
        return server, ui

    def test_b8_prompt_comes_before_the_ui_and_the_notice_is_the_footer(self):
        server, ui = self.start(self.env)
        self.assertTrue(ui.until(lambda: "[y/N]" in ui.text()), ui.text())
        self.assertIn("tmux 설정이 없습니다: mouse, allow-passthrough", ui.text())
        self.assertFalse(server.attached.is_set(), "the UI must not connect before the prompt is answered")
        ui.send(b"y\r")
        self.assertTrue(server.wait_for(server.attached.is_set), "attach not received")
        self.assertTrue(ui.until(lambda: "tmux 설정 적용: mouse on, allow-passthrough on" in ui.text()), ui.text())
        self.assertIn("(~/.tmux.conf에 추가)", ui.text())
        self.assertEqual("on\n", self.tmux("show", "-gv", "mouse"))
        self.assertEqual("on\n", self.tmux("show", "-gv", "allow-passthrough"))
        self.assertIn("set -g mouse on", (self.home / ".tmux.conf").read_text())

    def test_b8_declining_still_shows_the_notice_and_leaves_the_config_alone(self):
        server, ui = self.start(self.env)
        self.assertTrue(ui.until(lambda: "[y/N]" in ui.text()), ui.text())
        ui.send(b"\r")
        self.assertTrue(ui.until(lambda: "tmux 설정 적용: mouse on, allow-passthrough on" in ui.text()), ui.text())
        self.assertNotIn("에 추가", ui.text())
        self.assertEqual("on\n", self.tmux("show", "-gv", "mouse"))
        self.assertEqual([], list(self.home.iterdir()))

    def test_b8_outside_tmux_nothing_is_asked(self):
        env = {k: v for k, v in self.env.items() if k != "TMUX"}
        server, ui = self.start(env)
        self.assertTrue(server.wait_for(server.attached.is_set), "attach not received")
        self.assertTrue(ui.until(lambda: "MANAGER OMP" in ui.text()), ui.text())
        self.assertNotIn("tmux 설정", ui.text())
        self.assertEqual("off\n", self.tmux("show", "-gv", "mouse"))


if __name__ == "__main__":
    unittest.main()
