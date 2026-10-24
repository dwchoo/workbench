"""Shared harness of the HB-11 final Hub verification (``live_hub_*.py``).

Everything here is built on the HB-07 ``ProjectSandbox`` (``tests/hub/test_project_control.py``): a temp HOME with
a placeholder ``agent.db``, its own ``XDG_STATE_HOME``/``XDG_CONFIG_HOME``, a short private ``XDG_RUNTIME_DIR``
(so the Hub socket never falls back to ``/tmp/omp-workbench-hub-<uid>``), the fake OMP of
``tests/recovery_boot/fake_omp.py`` (no model, no provider, no network) and exact-identity cleanup of every
process that names the sandbox. The only thing outside the sandbox is the OMP ephemeral namespace
``/tmp/omp-workbench-<uid>/<hash>`` a real backend prepares by design: the sandbox checks that exact path absent
when a project is made and removes only that directory after every process of the sandbox has ended (never
opening anything inside).

The terminal is a ``pty.fork`` child read through a pyte screen. Expected values come from the SPEC/BRIEF
texts, ``/proc`` (pid, start ticks, ppid, ``/proc/locks``), the project snapshot read through ``wb status --json``
and the files the test itself wrote.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pty
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
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(REPO / 'tests' / 'hub'))

import pyte  # noqa: E402
from test_project_control import (  # noqa: E402,F401
    ProjectSandbox, alive, kill_exact, parent_and_session, start_ticks, wait_until)

ROWS, COLS = 30, 120
DETACH = b'\x1dq'  # Ctrl-] q


class HubBox(ProjectSandbox):
    """``ProjectSandbox`` whose own helper calls (``wb``, ``status``, ``start_hub``) never auto-start a Hub.

    ``self.env`` always has ``WORKBENCH_HUB_AUTOSTART=0``; ``self.loud`` is the same environment with auto-start on.
    ``default_env()`` is what a case that asked for ``autostart='1'`` hands to the commands and terminals under test."""

    def __init__(self, case: unittest.TestCase, autostart: str = '0'):
        super().__init__(case)
        self.autostart = autostart
        self.env['WORKBENCH_HUB_AUTOSTART'] = '0'
        self.loud = {**self.env, 'WORKBENCH_HUB_AUTOSTART': '1'}

    def default_env(self) -> dict:
        return self.loud if self.autostart == '1' else self.env

    def env_with(self, **extra: str) -> dict:
        return {**self.default_env(), **extra}

    def quiet(self) -> dict:
        return self.env

    def wb_env(self, *args: str, cwd: Path | None = None, env: dict | None = None,
               timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, '-m', 'workbench', *args], env=env or self.default_env(),
                              cwd=cwd or self.base, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)

    def hubs(self) -> dict[int, int]:
        """This sandbox's live ``_hub`` processes (pid -> start ticks), read from ``/proc``."""
        found = {}
        for pid, ticks in self.ours().items():
            try:
                argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            except OSError:
                continue
            if b'_hub' in argv:
                found[pid] = ticks
        return found

    def hub_lock_holder(self) -> int | None:
        path = self.hub_state / 'hub.lock'
        try:
            info = os.stat(path)
        except OSError:
            return None
        for line in Path('/proc/locks').read_text().splitlines():
            parts = line.split()
            fields = [part for part in parts if part.count(':') == 2]
            if fields and fields[0].rsplit(':', 1)[1] == str(info.st_ino):
                return int(parts[parts.index(fields[0]) - 1])
        return None

    def registry_bytes(self) -> bytes | None:
        try:
            return (self.hub_state / 'registry.json').read_bytes()
        except OSError:
            return None

    def tree_bytes(self, root: Path) -> dict[str, bytes]:
        """The bytes of every regular file under ``<root>/.wb`` except live runtime files."""
        out = {}
        for path in sorted((root / '.wb').rglob('*')):
            if path.is_file() and not path.is_symlink() and path.suffix not in ('.sock', '.lock'):
                out[str(path.relative_to(root))] = path.read_bytes()
        return out


class Terminal:
    """``python -m workbench ARGS`` on a pty read through a pyte screen (``pty.fork``)."""

    def __init__(self, case: unittest.TestCase, env: dict, *args: str, cwd: Path):
        self.case = case
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.raw = bytearray()
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(cwd)
                os.execve(sys.executable, [sys.executable, '-m', 'workbench', *args], env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self.ticks = start_ticks(pid)
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
            self.raw.extend(data)
            self.stream.feed(data)

    def text(self) -> str:
        return '\n'.join(line.rstrip() for line in self.screen.display)

    def until(self, predicate, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.1)
            if predicate():
                return True
        return predicate()

    def wait_for(self, needle: str, timeout: float = 30.0) -> str:
        if not self.until(lambda: needle in self.text(), timeout):
            self.case.fail(f'{needle!r} never appeared; screen:\n{self.text()}')
        return self.text()

    def wait_gone(self, needle: str, timeout: float = 15.0) -> None:
        if not self.until(lambda: needle not in self.text(), timeout):
            self.case.fail(f'{needle!r} never went away; screen:\n{self.text()}')

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def wait_exit(self, timeout: float = 20.0) -> int:
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
                if start_ticks(self.pid) == self.ticks:  # exact identity: only our own child
                    os.kill(self.pid, signal.SIGTERM)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        done, _status = os.waitpid(self.pid, os.WNOHANG)
                        if done:
                            break
                        time.sleep(0.05)
                    else:
                        os.kill(self.pid, signal.SIGKILL)
                        os.waitpid(self.pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
            self.status = -15
        try:
            os.close(self.fd)
        except OSError:
            pass


def snapshot_attached(box: HubBox, root: Path) -> bool | None:
    snapshot = box.status(root)
    return None if snapshot is None else snapshot.get('attached')


def json_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
