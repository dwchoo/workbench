"""WBX-E2 (8) with the real TUI: the product UI (curses, ``run_product`` on a PTY, Hub SPEC S8) and web pane
WebSockets (T6) on the same real backend in one test (user confirmation Q62, 2026-10-29; BRIEF WBX-O10).

The harness is ``test_board_pane.PaneHubCase`` (real Hub with web, real backend with fake OMPs, the sandbox rules
there); the TUI runs as ``python -c run_product(<ui.sock>)`` on a PTY the test owns (killed in cleanup) and its screen
is read with pyte (``tests/ui/support.rep_screen_classes``, loaded by path). Expected values: the backend's pane sizes
(``wb status --json``), the web ``attached``/``state``/``size`` messages, the bytes each side sees.
Run with ``discover -s tests/hub -p 'test_board_pane*.py'``.
"""
from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from test_board_pane import PaneHubCase  # noqa: E402

_spec = importlib.util.spec_from_file_location('ui_test_support', REPO / 'tests' / 'ui' / 'support.py')
ui_support = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui_support)

CODE = ("import sys; from pathlib import Path; from workbench.ui.product import run_product; "
        "raise SystemExit(run_product(Path(sys.argv[1])))")
PREFIX = b'\x1d'


class Tui:
    """The real product UI on a PTY of ``size`` attached to ``socket_path``."""

    def __init__(self, socket_path: str, env: dict, size=(30, 120)):
        master, slave = os.openpty()
        self.resize_pty(master, size)
        self.proc = subprocess.Popen([sys.executable, '-c', CODE, socket_path], stdin=slave, stdout=slave,
                                     stderr=slave, env={**env, 'TERM': 'xterm-256color'}, start_new_session=True,
                                     close_fds=True)
        os.close(slave)
        self.fd = master
        screen_class, stream_class = ui_support.rep_screen_classes()
        self.screen = screen_class(size[1], size[0])
        self.stream = stream_class(self.screen)

    @staticmethod
    def resize_pty(fd: int, size) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', size[0], size[1], 0, 0))

    def resize(self, size) -> None:
        self.resize_pty(self.fd, size)
        self.screen.resize(size[0], size[1])
        os.killpg(self.proc.pid, signal.SIGWINCH)

    def drain(self, timeout: float = 0.05) -> None:
        try:
            if select.select([self.fd], [], [], timeout)[0]:
                self.stream.feed(os.read(self.fd, 65536))
        except OSError:
            pass

    async def until(self, predicate, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain(0.0)
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return predicate()

    def text(self) -> str:
        return '\n'.join(self.screen.display)

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def close(self) -> None:
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)  # our own child in its own session
            self.proc.wait(5)
        os.close(self.fd)


class TuiAndWebTests(PaneHubCase):
    """The TUI owns the pane size while attached; the web follows it and types into the same host shell."""

    async def tui(self, root: Path, size=(30, 120)) -> Tui:
        _pid, _ticks, record = self.box.backend_ref(root)
        tui = Tui(record['ui_socket'], self.box.env, size)
        self.addCleanup(tui.close)
        self.assertTrue(await tui.until(lambda: 'MANAGER OMP' in tui.text()), tui.text())
        self.assertTrue(await self.eventually(lambda: self.box.status(root)['attached'] is True))
        return tui

    def host_size(self, root: Path) -> tuple[int, int]:
        info = self.box.status(root)['panes']['host_shell']
        return info['rows'], info['cols']

    async def test_the_tui_sets_the_pane_size_and_the_web_follows_without_resizing(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        tui = await self.tui(root, (30, 120))
        tui_size = self.host_size(root)
        tab = await self.attach(f'{project_id}:host', size=(40, 150))
        view = [item for item in tab.events if item.get('terminal')][-1]['terminal']
        self.assertTrue(view['ui_attached'], view)
        self.assertEqual(tuple(view['size']), tui_size)
        await tab.send({'t': 'resize', 'rows': 45, 'cols': 160})
        await self.take_host(project_id)
        await tab.send_input(b'true\r')
        await asyncio.sleep(1.5)
        self.assertEqual(self.host_size(root), tui_size, 'a web resize never wins over the TUI')
        tui.resize((40, 140))
        self.assertTrue(await self.eventually(lambda: self.host_size(root) != tui_size, 15), 'the TUI resized')
        new_size = self.host_size(root)
        message = await tab.receive_until(lambda item: isinstance(item, dict) and item.get('t') == 'size'
                                          and (item['rows'], item['cols']) == new_size, 15)
        self.assertEqual(message, {'t': 'size', 'rows': new_size[0], 'cols': new_size[1]})
        await asyncio.sleep(1.0)
        self.assertEqual(self.host_size(root), new_size)

    async def test_web_and_tui_type_into_the_same_host_shell_and_each_sees_the_other(self):
        root, project_id = self.project('alpha')
        self.start_hub()
        await self.take_host(project_id)
        tui = await self.tui(root)
        tab = await self.attach(f'{project_id}:host')
        await tab.send_input(b'echo WEB-$((40+2))\r')
        self.assertTrue(await tui.until(lambda: 'WEB-42' in tui.text()), tui.text())
        tui.send(PREFIX + b'3')  # focus the host shell
        self.assertTrue(await tui.until(lambda: 'focus: HOST SHELL' in tui.text()), tui.text())
        tui.send(b'echo TUI-$((40+3))\r')
        await tab.wait_output(b'TUI-43', 15)
        self.assertEqual(self.box.status(root)['shared_attachments'], 1)
        self.assertIs(self.box.status(root)['attached'], True)


if __name__ == '__main__':
    unittest.main()
