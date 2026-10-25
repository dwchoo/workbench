"""TI-16 on a real ``wb _hub --foreground`` (seam T3): the Board agent set joins the two-phase Hub stop (TI-T07),
ends on SIGTERM without a question, keeps running with no browser attached (P2-AC-34) and ``wb hub status`` works
inside it (the Hub is no managed tree).

The set is started through ``POST /board/restart`` (session + CSRF) with the fake ``omp`` as the Board agent and a
small script as ``board_agent.shell``. Expected values come from ``/proc``, ``hub.sqlite3`` and the CLI output.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import textwrap
import time
import unittest

from fixtures.web_sandbox import PASSWORD, WebSandbox, wait_until
from test_web_requests_hub import Browser

FAKE_OMP = Path(__file__).resolve().parent / 'fixtures' / 'fake_omp_board.py'


def alive(pid: int) -> bool:
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ('Z', 'X')


def ppid(pid: int) -> int:
    return int(Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[1])


class BoardHub:
    """A WebSandbox Hub with the fake Board agent, a scripted shell and a logged-in browser."""

    def __init__(self, case: unittest.TestCase, shell_script: str = 'exec sleep 600\n'):
        self.case = case
        self.box = box = WebSandbox(case)
        store = box.home / '.omp' / 'agent' / 'agent.db'
        store.parent.mkdir(parents=True)
        store.write_bytes(b'')  # placeholder user auth store: linked, never opened
        self.bin = box.base / 'bin'
        self.bin.mkdir()
        omp = self.bin / 'omp'
        omp.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_OMP} "$@"\n')
        omp.chmod(0o755)
        self.shell = self.bin / 'board-shell'
        self.shell.write_text('#!/bin/sh\n' + shell_script)
        self.shell.chmod(0o755)
        self.record = box.base / 'board-record.jsonl'
        box.env.update({'WORKBENCH_OMP': str(omp), 'FAKE_BOARD_RECORD': str(self.record)})
        box.write_config(raw=textwrap.dedent(f'''\
            [web]
            bind = "127.0.0.1"
            port = 0
            [board_agent]
            shell = "{self.shell}"
            '''))
        box.set_password()
        self.hub = box.start_hub()
        host, port = box.wait_listening(self.hub.pid)
        self.browser = Browser(host, port)
        status, _, page = self.browser.request('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        status, _, _ = self.browser.request('POST', '/login', {'login_token': token, 'password': PASSWORD})
        case.assertEqual(status, 303, 'login')

    def csrf(self) -> str:
        status, _, page = self.browser.request('GET', '/settings')
        self.case.assertEqual(status, 200)
        return re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def restart(self, channel: str) -> int:
        status, _, _ = self.browser.request('POST', '/board/restart', {'csrf': self.csrf(), 'channel': channel})
        return status

    def board(self) -> dict | None:
        return self.box.status()['board_agent']

    def start_set(self) -> dict[str, int]:
        for channel in ('agent', 'shell'):
            self.case.assertEqual(self.restart(channel), 303, channel)
        self.case.assertTrue(wait_until(lambda: (self.board() or {}).get('agent', {}).get('state') == 'running', 15),
                             self.board())
        board = self.board()
        self.case.assertEqual(board['shell']['state'], 'running')
        return {name: board[name]['pid'] for name in ('agent', 'shell')}

    def audit(self, event: str) -> list[dict]:
        db = sqlite3.connect(f'file:{self.box.state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            rows = db.execute('SELECT actor, detail_json FROM audit WHERE event = ? ORDER BY id', (event,)).fetchall()
        finally:
            db.close()
        return [{'actor': actor, **json.loads(detail)} for actor, detail in rows]


class HubStopTests(unittest.TestCase):
    def test_stop_without_yes_and_without_a_terminal_stops_nothing_and_yes_ends_the_set_then_the_hub(self):
        hub = BoardHub(self)
        pids = hub.start_set()
        for pid in pids.values():
            self.assertEqual(ppid(pid), hub.hub.pid)  # the set is the Hub's own children

        refused = hub.box.cli('hub', 'stop')
        self.assertEqual(refused.returncode, 1, refused)
        self.assertIn('Board agent', refused.stdout)
        self.assertIn(f'agent running(pid {pids["agent"]})', refused.stdout)
        self.assertIn(f'shell running(pid {pids["shell"]})', refused.stdout)
        self.assertIsNone(hub.hub.poll())
        self.assertTrue(all(alive(pid) for pid in pids.values()))

        stopped = hub.box.cli('hub', 'stop', '--yes')
        self.assertEqual(stopped.returncode, 0, stopped)
        self.assertEqual(hub.hub.wait(timeout=20), 0)
        self.assertFalse(any(alive(pid) for pid in pids.values()))
        self.assertEqual(sorted((row['channel'], row['pid'], row['reason']) for row in hub.audit('board_stop')),
                         sorted((name, pid, 'hub_stop') for name, pid in pids.items()))

    def test_a_channel_that_ends_between_request_and_confirm_is_a_token_mismatch_and_nothing_stops(self):
        flag = Path(os.environ.get('TMPDIR', '/tmp')) / f'wb-board-flag-{os.getpid()}-{time.monotonic_ns()}'
        self.addCleanup(lambda: flag.unlink(missing_ok=True))
        hub = BoardHub(self, f'while [ ! -e "{flag}" ]; do sleep 0.05; done\nexit 5\n')
        pids = hub.start_set()
        client = textwrap.dedent('''\
            import json, pathlib, sys, time
            from workbench.hub.client import HubClient
            from workbench.hub.paths import hub_layout
            with HubClient(hub_layout(), name="ti16-test", timeout=5) as client:
                offer = client.request("stop_request")
                print(json.dumps(offer["active"]), flush=True)
                pathlib.Path(sys.argv[1]).touch()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if client.request("status")["board_agent"]["shell"]["state"] == "exited":
                        break
                    time.sleep(0.05)
                print(json.dumps(client.request("stop_confirm", token=offer["token"])), flush=True)
            ''')
        result = subprocess.run([sys.executable, '-c', client, str(flag)], env=hub.box.env, capture_output=True,
                                text=True, timeout=40)
        offered, reply = (json.loads(line) for line in result.stdout.splitlines())
        self.assertEqual(offered, [{'kind': 'board_agent_set', 'lease': 'none', 'channels': {
            'agent': {'pid': pids['agent'], 'state': 'running'}, 'shell': {'pid': pids['shell'], 'state': 'running'}}}])
        self.assertEqual((reply['ok'], reply['reason']), (False, 'stop_token_mismatch'))
        self.assertIsNone(hub.hub.poll())
        self.assertTrue(alive(pids['agent']))
        self.assertEqual(hub.board()['shell']['exit_status'], 5)

    def test_without_a_set_the_stop_asks_nothing(self):
        hub = BoardHub(self)
        stopped = hub.box.cli('hub', 'stop')
        self.assertEqual(stopped.returncode, 0, stopped)
        self.assertNotIn('Board agent', stopped.stdout)
        self.assertEqual(hub.hub.wait(timeout=20), 0)

    def test_sigterm_ends_the_set_without_a_question(self):
        hub = BoardHub(self)
        pids = hub.start_set()
        self.assertEqual(hub.box.stop_hub(hub.hub, timeout=20), 0)
        self.assertFalse(any(alive(pid) for pid in pids.values()))
        self.assertEqual({row['reason'] for row in hub.audit('board_stop')}, {'hub_stop'})


class SetLifetimeTests(unittest.TestCase):
    def test_the_set_keeps_running_with_no_connection_and_restart_gives_a_new_pid(self):
        hub = BoardHub(self)
        pids = hub.start_set()
        time.sleep(30)  # P2-AC-34: no browser attached for 30 s
        board = hub.board()
        self.assertEqual({name: board[name]['pid'] for name in ('agent', 'shell')}, pids)
        self.assertTrue(all(alive(pid) for pid in pids.values()))
        self.assertEqual(hub.restart('agent'), 303)
        self.assertTrue(wait_until(lambda: hub.board()['agent']['state'] == 'running', 15))
        new = hub.board()['agent']['pid']
        self.assertNotEqual(new, pids['agent'])
        self.assertFalse(alive(pids['agent']))
        self.assertEqual([(row['actor'], row['channel'], row['pid']) for row in hub.audit('board_restart')][-1],
                         ('owner', 'agent', pids['agent']))
        self.assertEqual(hub.box.stop_hub(hub.hub, timeout=20), 0)
        self.assertFalse(alive(new))

    def test_wb_hub_status_works_inside_the_board_shell_and_shows_the_set(self):
        out = Path(os.environ.get('TMPDIR', '/tmp')) / f'wb-board-status-{os.getpid()}-{time.monotonic_ns()}'
        self.addCleanup(lambda: out.unlink(missing_ok=True))
        hub = BoardHub(self, f'{sys.executable} -m workbench hub status > "{out}" 2>&1\n'
                             f'echo "rc=$?" >> "{out}"\nexec sleep 600\n')
        pids = hub.start_set()
        self.assertTrue(wait_until(lambda: out.exists() and 'rc=' in out.read_text(), 20))
        text = out.read_text()
        self.assertIn('rc=0', text)
        self.assertIn('Hub 실행 중', text)
        self.assertNotIn('관리', text.split('rc=')[0].split('Hub 실행 중')[0])
        shown = hub.box.cli('hub', 'status')
        self.assertIn(f'board_agent: agent running(pid {pids["agent"]}) · shell running(pid {pids["shell"]}) · 연결 없음',
                      shown.stdout)


if __name__ == '__main__':
    unittest.main()
