"""Bare ``wb`` (HB-08, seams S1 + S8): outside a WB project it opens the TUI Hub list, inside it keeps
starting/attaching as before, and without a TTY outside it exits 2 with ``wb hub list`` guidance.

Every ``wb`` here is a real subprocess (a pty for the list itself) in a temp HOME/XDG sandbox with a short
private ``XDG_RUNTIME_DIR``; nothing touches the real ``~/.local/state`` or a tmux server (``TMUX`` is unset
and the pty runs the list only). Expected values are the SPEC/BRIEF sentences, the test's own paths and the
project_id read back from the marker. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pyte  # noqa: E402

from workbench.backend.project import initialize_project, resolve_project_context  # noqa: E402

GUIDANCE = "Hub 목록은 terminal이 필요합니다. 'wb hub list'를 쓰세요"
ROWS, COLS = 24, 110


class BareCase(unittest.TestCase):
    autostart = '0'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-hubbare-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        for name in ('home', 'state', 'config'):
            (self.base / name).mkdir(mode=0o700)
        self.run = Path(tempfile.mkdtemp(prefix='r', dir='/tmp')).resolve()
        os.chmod(self.run, 0o700)
        self.addCleanup(shutil.rmtree, self.run, ignore_errors=True)
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.base / 'home'), 'LANG': 'C.UTF-8', 'TERM': 'xterm',
                    'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'XDG_STATE_HOME': str(self.base / 'state'), 'XDG_RUNTIME_DIR': str(self.run),
                    'XDG_CONFIG_HOME': str(self.base / 'config'), 'WORKBENCH_HUB_AUTOSTART': self.autostart}
        self.hub_state = self.base / 'state' / 'omp-workbench' / 'hub'
        self.addCleanup(self.stop_hub)

    def stop_hub(self):
        """Any Hub this case spawned (a ``wb`` with auto-start on) ends with it."""
        if (self.hub_state / 'hub.json').exists():
            self.wb('hub', 'stop', '--yes', env=dict(self.env, WORKBENCH_HUB_AUTOSTART='0'))

    def wb(self, *args, cwd=None, env=None, timeout=60):
        return subprocess.run([sys.executable, '-m', 'workbench', *args], cwd=cwd or self.base,
                              env=env or self.env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout)

    def folder(self, name: str) -> Path:
        path = self.base / 'work' / name
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def project(self, name: str) -> Path:
        root = self.folder(name)
        initialize_project(resolve_project_context(root, self.env), confirm=lambda _root: True,
                           interactive=True, environment=self.env)
        return root


class NoTty(BareCase):
    def test_outside_a_project_without_a_tty_exits_2_with_hub_list_guidance(self):
        outside = self.folder('plain')
        result = self.wb(cwd=outside)
        self.assertEqual(result.returncode, 2, result)
        self.assertIn(GUIDANCE, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertFalse((outside / '.wb').exists())

    def test_inside_a_project_it_is_still_start(self):
        root = self.project('proj')
        inner = root / 'deep' / 'er'
        inner.mkdir(parents=True)
        before = sorted(os.listdir(root / '.wb'))
        result = self.wb(cwd=inner)
        # `start` without a usable OMP login stops at its own requirement, never at the list guidance.
        self.assertNotIn('wb hub list', result.stderr + result.stdout)
        self.assertIn('No backend was started', result.stderr + result.stdout)
        self.assertEqual(sorted(os.listdir(root / '.wb')), before)

    def test_workbench_data_dir_counts_as_inside(self):
        outside = self.folder('plain')
        env = dict(self.env, WORKBENCH_DATA_DIR=str(outside / 'legacy'))
        result = self.wb(cwd=outside, env=env)
        self.assertNotIn('wb hub list', result.stderr + result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_an_explicit_start_outside_keeps_asking_to_initialize(self):
        outside = self.folder('plain')
        result = self.wb('start', cwd=outside)
        self.assertNotIn('wb hub list', result.stderr + result.stdout)
        self.assertFalse((outside / '.wb').exists())


class NoTtyAutostart(BareCase):
    autostart = '1'

    def test_the_no_tty_refusal_does_not_start_a_hub(self):
        result = self.wb(cwd=self.folder('plain'))
        self.assertEqual(result.returncode, 2, result)
        self.assertFalse((self.hub_state / 'hub.json').exists())
        self.assertFalse((self.hub_state / 'hub.lock').exists())


class Terminal:
    """``python -m workbench`` on a pty, read through a pyte screen."""

    def __init__(self, case: BareCase, *args: str, cwd: Path, env: dict | None = None):
        self.case = case
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(cwd)
                os.execve(sys.executable, [sys.executable, '-m', 'workbench', *args], env or case.env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLS, 0, 0))
        self.status: int | None = None
        case.addCleanup(self.close)

    def pump(self, seconds: float = 0.2) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if not ready:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            self.stream.feed(data)

    def text(self) -> str:
        return '\n'.join(line.rstrip() for line in self.screen.display)

    def wait_for(self, needle: str, timeout: float = 20.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.1)
            if needle in self.text():
                return self.text()
        self.case.fail(f'{needle!r} never appeared; screen:\n{self.text()}')

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def wait_exit(self, timeout: float = 15.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.1)
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.status = os.waitstatus_to_exitcode(status)
                return self.status
        self.case.fail(f'process did not exit; screen:\n{self.text()}')

    def close(self) -> None:
        if self.status is None:
            try:
                os.kill(self.pid, signal.SIGTERM)
                os.waitpid(self.pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
            self.status = -15
        try:
            os.close(self.fd)
        except OSError:
            pass


class NoHub(BareCase):
    def test_outside_a_project_on_a_tty_without_a_hub_exits_1_and_says_so(self):
        term = Terminal(self, cwd=self.folder('plain'))
        self.assertEqual(term.wait_exit(), 1, term.text())
        self.assertIn('Hub 실행 중 아님', term.text())


class ListOnATty(BareCase):
    autostart = '1'

    def test_the_list_shows_registered_projects_and_q_leaves(self):
        alpha, beta = self.project('alpha'), self.project('beta')
        for root in (alpha, beta):
            added = self.wb('hub', 'add', str(root), env=dict(self.env, WORKBENCH_HUB_AUTOSTART='0'))
            self.assertEqual(added.returncode, 0, added)
        term = Terminal(self, cwd=self.folder('plain'))
        screen = term.wait_for('beta')
        self.assertIn('Workbench Hub', screen)
        self.assertIn('이름', screen)
        self.assertIn('꺼짐', screen)
        term.send(b'q')
        self.assertEqual(term.wait_exit(), 0, term.text())

    def test_enter_on_an_off_row_asks_and_anything_but_y_stays_on_the_list(self):
        alpha = self.project('alpha')
        self.assertEqual(self.wb('hub', 'add', str(alpha), env=dict(self.env, WORKBENCH_HUB_AUTOSTART='0')).returncode, 0)
        term = Terminal(self, cwd=self.folder('plain'))
        term.wait_for('꺼짐')
        term.send(b'\r')
        term.wait_for('"alpha"을(를) 켤까요? [y/N]')
        term.send(b'n')
        deadline = time.monotonic() + 5
        while '켤까요' in term.text() and time.monotonic() < deadline:
            term.pump(0.1)
        self.assertNotIn('켤까요', term.text())
        self.assertIn('alpha', term.text())
        self.assertFalse((alpha / '.wb' / 'backend.json').exists())
        term.send(b'q')
        self.assertEqual(term.wait_exit(), 0, term.text())

    def test_a_stopped_hub_shows_the_banner_and_keeps_the_rows_unknown(self):
        alpha = self.project('alpha')
        quiet = dict(self.env, WORKBENCH_HUB_AUTOSTART='0')
        self.assertEqual(self.wb('hub', 'add', str(alpha), env=quiet).returncode, 0)
        term = Terminal(self, cwd=self.folder('plain'))
        term.wait_for('꺼짐')
        self.assertEqual(self.wb('hub', 'stop', '--yes', env=quiet).returncode, 0)
        screen = term.wait_for('Hub 연결 없음', timeout=40)
        self.assertIn('alpha', screen)
        self.assertIn('연결 없음/불명', screen)
        self.assertNotIn('꺼짐', screen)
        term.send(b'\r')
        term.wait_for('연결 없음/불명 상태라')  # the refusal notice
        term.send(b'q')
        self.assertEqual(term.wait_exit(), 0, term.text())


class RealBackendFlow(unittest.TestCase):
    """The whole loop on a pty with a real backend (fake OMP, no provider): ``o`` asks then starts, ``Enter``
    attaches through ``run_product``, detach comes back to the list with the project still ``켜짐``, ``x`` asks and
    stops it. Processes and the OMP ephemeral dir are the sandbox's (see test_project_control)."""

    def setUp(self):
        from test_project_control import ProjectSandbox
        self.box = ProjectSandbox(self)
        self.root, project_id = self.box.project('alpha')
        self.box.register(self.box.entry(self.root, project_id))
        self.env = dict(self.box.env, WORKBENCH_HUB_AUTOSTART='1')  # the list needs the Hub: bare wb starts it

    def test_start_attach_detach_and_stop_from_the_list(self):
        term = Terminal(self, cwd=self.box.folder('plain'), env=self.env)
        term.wait_for('꺼짐')
        term.send(b'o')
        term.wait_for('"alpha"을(를) 켤까요? [y/N]')
        term.send(b'y')
        term.wait_for('켰습니다', timeout=120)
        self.assertIn('켜짐', term.wait_for('켜짐', timeout=20))  # the refresh after the action
        self.assertEqual(self.box.hub_row(self.root)['power'], 'on')
        term.send(b'\r')  # Enter on the running project: run_product on this terminal
        term.pump(2.0)
        self.assertNotIn('Workbench Hub', term.text())  # the product UI owns the screen now
        term.send(b'\x1dq')  # Ctrl-] q
        screen = term.wait_for('detach했습니다', timeout=30)
        self.assertIn('Workbench Hub', screen)
        self.assertIn('켜짐', screen)
        self.assertEqual(self.box.hub_row(self.root)['power'], 'on')
        term.send(b'x')
        term.wait_for('[y/N]', timeout=30)  # the backend's own question, on the plain terminal
        term.send(b'y\r')
        term.wait_for('껐습니다', timeout=120)
        term.wait_for('꺼짐', timeout=20)  # the refresh after the action
        term.send(b'q')
        self.assertEqual(term.wait_exit(), 0, term.text())
        self.assertEqual(self.box.backends(), {})


if __name__ == '__main__':
    unittest.main()
