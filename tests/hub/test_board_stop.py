"""TI-16 / WBX-D1 on a real ``wb _hub --foreground`` (seam S3): the Board terminals join the two-phase Hub stop
(TI-T07), end on SIGTERM without a question, keep running with no browser attached (P2-AC-34) and ``wb hub status``
works inside them (the Hub is no managed tree).

The terminals are started through the session API (``POST /api/board/open``, session + ``X-CSRF-Token``) with the
fake ``omp`` as the Board agent and a small script as ``board_agent.shell``. Expected values come from ``/proc``,
``hub.sqlite3`` and the CLI output (web-board-ux SPEC s4 "Hub 상태·정지·nav").
"""
from __future__ import annotations

import http.client
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
            enabled = true
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

    def api(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        """The Board session API as the page calls it (JSON, the session cookie, ``X-CSRF-Token``, this Origin)."""
        headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in self.browser.cookies.items()),
                   'Origin': f'http://{self.browser.host}:{self.browser.port}', 'X-CSRF-Token': self.csrf()}
        data = None
        if body is not None:
            data = json.dumps(body)
            headers['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection(self.browser.host, self.browser.port, timeout=30)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode('utf-8') or 'null')
        finally:
            connection.close()

    def view(self, kind: str, index: int = 0) -> dict:
        return (self.board() or {})[kind + 's'][index]

    def restart(self, kind: str) -> int:
        status, _ = self.api('POST', f"/api/board/terminals/{self.view(kind)['id']}/restart", {'confirm': True})
        return status

    def board(self) -> dict | None:
        return self.box.status()['board_agent']

    def start_set(self) -> dict:
        """``POST /api/board/open`` with agent-1 visible (the page's first open): agent-1 starts, shell-1 is made.
        Returns both pids (``self.ids``: both ids)."""
        agent = self.view('agent')
        status, reply = self.api('POST', '/api/board/open', {'visible': [agent['id']]})
        self.case.assertEqual(status, 200, reply)
        self.case.assertTrue(wait_until(lambda: self.view('agent')['state'] == 'running', 15), self.board())
        shell = self.view('shell')
        self.case.assertEqual(shell['state'], 'running')
        self.ids = {'agent': agent['id'], 'shell': shell['id']}
        return {'agent': self.view('agent')['pid'], 'shell': shell['pid']}

    def audit(self, event: str) -> list[dict]:
        db = sqlite3.connect(f'file:{self.box.state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            rows = db.execute('SELECT actor, detail_json FROM audit WHERE event = ? ORDER BY id', (event,)).fetchall()
        finally:
            db.close()
        return [{'actor': actor, **json.loads(detail)} for actor, detail in rows]


class LockReadRaceTests(unittest.TestCase):
    """``/proc/locks`` is not read atomically: a read can skip a held lock's row under flock churn. Holder
    checks must not take one skipped row for a free lock (Hub status rc=3, a false 'released', a backend's
    ``startup_lock_owner_mismatch``)."""

    def setUp(self):
        import fcntl
        import tempfile
        self.directory = Path(tempfile.mkdtemp(prefix='wbr-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.directory, ignore_errors=True))
        self.lock = self.directory / 'x.lock'
        stream = open(self.lock, 'w')
        self.addCleanup(stream.close)
        fcntl.flock(stream, fcntl.LOCK_EX)
        os.chmod(self.lock, 0o600)

    def skip_rows(self, reads: int):
        from unittest import mock
        real, inode, calls = Path.read_text, os.stat(self.lock).st_ino, {'n': 0}

        def read_text(path, *args, **kwargs):
            text = real(path, *args, **kwargs)
            if str(path) != '/proc/locks':
                return text
            calls['n'] += 1
            if calls['n'] > reads:
                return text
            return '\n'.join(row for row in text.splitlines()
                             if not (len(row.split()) >= 6 and row.split()[5].split(':')[-1] == str(inode))) + '\n'
        return mock.patch.object(Path, 'read_text', read_text)

    def test_instance_lock_held_by_survives_a_skipped_row(self):
        from workbench.backend.paths import InstanceLock
        with self.skip_rows(2):
            self.assertTrue(InstanceLock(self.lock).held_by(os.getpid()))

    def test_lock_holder_with_the_recorded_pid_survives_every_read_skipping_the_row(self):
        from workbench.hub.process import lock_holder
        with self.skip_rows(1000):  # /proc/<pid>/fdinfo proves the recorded candidate
            self.assertEqual(lock_holder(self.lock, os.getpid()), os.getpid())

    def held_in_child(self, path: Path):
        holder = subprocess.Popen([sys.executable, '-c', 'import fcntl,sys,time\n'
                                   'f=open(sys.argv[1],"w"); fcntl.flock(f, fcntl.LOCK_EX)\n'
                                   'print("held", flush=True); time.sleep(60)\n', str(path)],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        return holder

    def test_held_elsewhere_is_a_read_only_probe(self):
        """``startup_lock_owner_mismatch`` root cause: a probe that briefly took LOCK_EX|LOCK_NB made a starting
        backend's own acquire fail and showed the prober as the holder. The probe must never take the lock."""
        from unittest import mock
        from workbench.backend import paths
        other = self.directory / 'backend.lock'
        other.write_text('')
        os.chmod(other, 0o600)
        free = self.directory / 'free.lock'
        free.write_text('')
        self.held_in_child(other)
        with mock.patch.object(paths.fcntl, 'flock', side_effect=AssertionError('the probe took the lock')):
            self.assertTrue(paths.InstanceLock(other).held_elsewhere())
            self.assertFalse(paths.InstanceLock(free).held_elsewhere())
            self.assertFalse(paths.InstanceLock(self.directory / 'missing.lock').held_elsewhere())

    def test_held_elsewhere_survives_a_skipped_row(self):
        from workbench.backend.paths import InstanceLock
        other = self.directory / 'backend.lock'
        other.write_text('')
        self.held_in_child(other)
        real, inode, calls = Path.read_text, os.stat(other).st_ino, {'n': 0}
        from unittest import mock

        def read_text(path, *args, **kwargs):
            text = real(path, *args, **kwargs)
            if str(path) == '/proc/locks':
                calls['n'] += 1
                if calls['n'] <= 2:
                    return '\n'.join(row for row in text.splitlines()
                                     if not (len(row.split()) >= 6 and row.split()[5].split(':')[-1] == str(inode)))
            return text
        with mock.patch.object(Path, 'read_text', read_text):
            self.assertTrue(InstanceLock(other).held_elsewhere())

    def test_a_free_lock_is_not_held_by_anyone(self):
        from workbench.backend.paths import InstanceLock
        from workbench.hub.process import lock_holder
        free = self.directory / 'free.lock'
        free.write_text('')
        self.assertIsNone(lock_holder(free, os.getpid()))
        self.assertFalse(InstanceLock(free).held_by(os.getpid()))


class HubStopTests(unittest.TestCase):
    def test_stop_without_yes_and_without_a_terminal_stops_nothing_and_yes_ends_the_set_then_the_hub(self):
        hub = BoardHub(self)
        pids = hub.start_set()
        for pid in pids.values():
            self.assertEqual(ppid(pid), hub.hub.pid)  # the set is the Hub's own children

        refused = hub.box.cli('hub', 'stop')
        self.assertEqual(refused.returncode, 1, refused)
        self.assertIn('Board terminal', refused.stdout)  # WBX-D1: every live terminal with its name and state
        self.assertIn(f'agent agent-1 running(pid {pids["agent"]})', refused.stdout)
        self.assertIn(f'shell shell-1 running(pid {pids["shell"]})', refused.stdout)
        self.assertIsNone(hub.hub.poll())
        self.assertTrue(all(alive(pid) for pid in pids.values()))

        stopped = hub.box.cli('hub', 'stop', '--yes')
        self.assertEqual(stopped.returncode, 0, stopped)
        self.assertEqual(hub.hub.wait(timeout=20), 0)
        self.assertFalse(any(alive(pid) for pid in pids.values()))
        self.assertEqual(sorted((row['kind'], row['name'], row['pid'], row['reason']) for row in hub.audit('board_stop')),
                         sorted((kind, f'{kind}-1', pid, 'hub_stop') for kind, pid in pids.items()))

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
                    if client.request("status")["board_agent"]["shells"][0]["state"] == "exited":
                        break
                    time.sleep(0.05)
                print(json.dumps(client.request("stop_confirm", token=offer["token"])), flush=True)
            ''')
        result = subprocess.run([sys.executable, '-c', client, str(flag)], env=hub.box.env, capture_output=True,
                                text=True, timeout=40)
        offered, reply = (json.loads(line) for line in result.stdout.splitlines())
        self.assertEqual(offered, [{'kind': 'board_agent_set', 'terminals': [
            {'id': hub.ids['agent'], 'kind': 'agent', 'name': 'agent-1', 'pid': pids['agent'], 'state': 'running'},
            {'id': hub.ids['shell'], 'kind': 'shell', 'name': 'shell-1', 'pid': pids['shell'], 'state': 'running'}]}])
        self.assertEqual((reply['ok'], reply['reason']), (False, 'stop_token_mismatch'))
        self.assertIsNone(hub.hub.poll())
        self.assertTrue(alive(pids['agent']))
        self.assertEqual(hub.view('shell')['exit_status'], 5)

    def test_without_a_set_the_stop_asks_nothing(self):
        hub = BoardHub(self)
        stopped = hub.box.cli('hub', 'stop')
        self.assertEqual(stopped.returncode, 0, stopped)
        self.assertNotIn('Board terminal', stopped.stdout)
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
        self.assertEqual({kind: hub.view(kind)['pid'] for kind in ('agent', 'shell')}, pids)
        self.assertTrue(all(alive(pid) for pid in pids.values()))
        self.assertEqual(hub.restart('agent'), 200)
        self.assertTrue(wait_until(lambda: hub.view('agent')['state'] == 'running', 15))
        new = hub.view('agent')['pid']
        self.assertNotEqual(new, pids['agent'])
        self.assertFalse(alive(pids['agent']))
        self.assertEqual([(row['actor'], row['kind'], row['name'], row['pid']) for row in hub.audit('board_restart')][-1],
                         ('owner', 'agent', 'agent-1', pids['agent']))
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
        self.assertIn('board_agent: agent 1/4 실행 중 · shell 1 · 연결 0', shown.stdout)  # WBX-D1 status line


if __name__ == '__main__':
    unittest.main()
