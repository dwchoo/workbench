"""A Board launcher stand-in for the in-process Board tests (WebSocket v2, session API): every terminal runs the
recorder below on its PTY instead of OMP or the owner's shell, and an agent's isolation check passes at once.

The recorder (raw mode) writes every byte it reads from its terminal to ``<base>/<terminal id>.stdin`` and echoes it as
``<bytes>``; ``SIZE`` answers ``size:<rows>,<cols>;`` (the PTY's window size as the program sees it) and
``FLOOD<n>;`` writes ``n`` x 64 KiB. Its first output is ``ready:<terminal id>``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
import textwrap

RECORDER = textwrap.dedent('''\
    import os, sys, tty
    record = sys.argv[1]
    try:
        tty.setraw(0)
    except Exception:
        pass
    os.write(1, b"ready:" + sys.argv[2].encode() + b"\\r\\n")
    out = os.open(record, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    while True:
        data = os.read(0, 65536)
        if not data:
            break
        os.write(out, data)
        if data.startswith(b"SIZE"):
            size = os.get_terminal_size(0)
            os.write(1, b"size:%d,%d;" % (size.lines, size.columns))
            continue
        if data.startswith(b"FLOOD"):
            chunk = b"x" * 65536
            for _ in range(int(data[5:].split(b";")[0])):
                os.write(1, chunk)
            continue
        os.write(1, b"<" + data + b">")
    ''')


@dataclass
class Started:
    argv: list
    env: dict
    cwd: str
    key_id: str = ''
    home: Path | None = None


@dataclass
class RecorderLauncher:
    """``prepare_agent(spec)``/``prepare_shell()`` like ``BoardLauncher``; shells are told apart by their start order
    (``shell-<n>`` record names until the set gives them an id: the shell's record is ``<base>/shell<n>.stdin``)."""

    base: Path
    state_dir: Path = None
    shells: int = 0
    agent_starts: list = field(default_factory=list)

    def __post_init__(self):
        self.base = Path(self.base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.script = self.base / 'recorder.py'
        self.script.write_text(RECORDER)
        if self.state_dir is None:
            self.state_dir = self.base / 'hub'
        Path(self.state_dir).mkdir(parents=True, exist_ok=True, mode=0o700)

    def started(self, label: str) -> Started:
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.base), 'TERM': 'xterm-256color'}
        return Started([sys.executable, '-I', str(self.script), str(self.base / f'{label}.stdin'), label], env,
                       str(self.base))

    def prepare_shell(self):
        self.shells += 1
        return self.started(f'shell{self.shells}')

    async def prepare_agent(self, spec):
        self.agent_starts.append(spec.id)
        return self.started(spec.id)

    async def check_agent(self, started, cancel=None):
        return None

    async def close(self):
        pass


def board_api(browser, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    """The Board session API as the page calls it, through a logged-in ``test_web_requests_hub.Browser``: JSON, the
    session cookie, this Origin and the session's CSRF token (read from ``/settings``) as ``X-CSRF-Token``."""
    import http.client
    import json
    import re
    _, _, page = browser.request('GET', '/settings')
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in browser.cookies.items()),
               'Origin': f'http://{browser.host}:{browser.port}', 'X-CSRF-Token': csrf}
    data = None
    if body is not None:
        data = json.dumps(body)
        headers['Content-Type'] = 'application/json'
    connection = http.client.HTTPConnection(browser.host, browser.port, timeout=30)
    try:
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        text = response.read().decode('utf-8')
        try:
            return response.status, json.loads(text)
        except ValueError:
            return response.status, None
    finally:
        connection.close()


def first_open(browser) -> tuple[int, dict | None]:
    """The page's first ``POST /api/board/open``: agent-1 visible (it starts) and this run's shell-1 made."""
    status, board = board_api(browser, 'GET', '/api/board')
    if status != 200:
        return status, board
    agents = [view['id'] for view in board['terminals'] if view['kind'] == 'agent'][:1]
    return board_api(browser, 'POST', '/api/board/open', {'visible': agents})


def terminal_of(board: dict | None, kind: str) -> dict:
    """The first agent or shell of a hub_v1 ``status.board_agent`` (``{}`` when there is none)."""
    items = (board or {}).get(kind + 's') or []
    return items[0] if items else {}
