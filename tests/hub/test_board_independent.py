"""TI-16 independent verification of the Board agent set (test_designer owned).

Expected values are derived from the SPEC "Board agent 세트", ADR-0003, PROBE-OMP-MCP "TI-16이 쓸 구성" and the
BRIEF decisions (D-17/D-18/D-24..D-26/D-33/D-34), never from the product helpers or the worker's tests.

Seams: a fake ``omp`` (``fixtures/fake_omp_board.py``) behind a small sh wrapper of this file (fd listing, a
SIGHUP/SIGTERM-ignoring background child), a real ``wb _hub --foreground`` in a temp HOME/XDG sandbox, real PTYs,
``/proc`` for pid/ppid/session/fd/environ, and the real internal MCP listener over HTTP.

Hygiene: every process of a test carries ``WB_TI16_MARK=<token>`` in its environment, so a leftover is found by
scanning ``/proc/*/environ`` and ended by (pid, start ticks) through a pidfd; a leftover on a normal path fails
the test. ``wb hub ...`` calls retry when the product answers "not running" while the Hub is demonstrably alive
(``LockHolderReadRaceTests`` pins that defect separately), so it never hides in the other results.
"""
from __future__ import annotations

import asyncio
import errno
import http.client
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from fixtures.web_sandbox import PASSWORD, WebSandbox, tcp_listening, wait_until
from test_board_launch import LauncherCase
from test_web_requests_hub import Browser

from workbench.hub.board.set import BoardSet
from workbench.hub.config import BoardAgentConfig
from workbench.hub.process import lock_holder

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'tests' / 'backend' / 'maintenance'))  # the `wb update` fixture (module import only)
import test_update_hub as maintenance_hub  # noqa: E402
from test_update_hub import Tty  # noqa: E402
FAKE_OMP = Path(__file__).resolve().parent / 'fixtures' / 'fake_omp_board.py'
MARK = 'WB_TI16_MARK'
KEY_ENV = 'WB_BOARD_MCP_KEY'
CANARY = ('WB_TI16_USER_CANARY', 'user-owned-value')
INIT_BODY = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
    'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'ti16', 'version': '1'}}})


# --- /proc helpers (expected values come from the kernel) ---------------------------------------------

def stat_fields(pid: int) -> list[str] | None:
    """Fields after ``(comm) `` of ``/proc/<pid>/stat``: [0] state, [1] ppid, [2] pgrp, [3] session, [19] starttime."""
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
    except (OSError, IndexError):
        return None


def alive(pid: int) -> bool:
    fields = stat_fields(pid)
    return fields is not None and fields[0] not in ('Z', 'X')


def ticks(pid: int) -> int | None:
    fields = stat_fields(pid)
    return int(fields[19]) if fields else None


def marked(mark: str) -> dict[int, int]:
    """``pid -> start ticks`` of every live process whose environment carries this test's mark."""
    wanted = f'{MARK}={mark}\0'.encode()
    found = {}
    for name in os.listdir('/proc'):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            environment = Path(f'/proc/{name}/environ').read_bytes()
        except OSError:
            continue
        if wanted in environment and alive(int(name)):
            found[int(name)] = ticks(int(name))
    return found


def kill_exact(pid: int, start: int | None) -> None:
    """SIGKILL exactly the process (pid, start ticks): the pidfd pins it before the identity is re-read."""
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if start is not None and ticks(pid) == start:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(fd)


def reap_marked(case: unittest.TestCase, mark: str, expect_leftovers: bool = False) -> None:
    left = marked(mark)
    for pid, start in left.items():
        kill_exact(pid, start)
    wait_until(lambda: not marked(mark), 5)
    if left and not expect_leftovers:
        case.fail(f'processes left behind: {left}')


def session_of(pid: int) -> int | None:
    fields = stat_fields(pid)
    return int(fields[3]) if fields else None


def family(roots: list[int], mark: str) -> set[int]:
    """Marked processes that are a root, share its session, or descend from one (any group, any depth)."""
    candidates = marked(mark)
    members = {root for root in roots if root in candidates}
    changed = True
    while changed:
        changed = False
        for pid in candidates:
            if pid in members:
                continue
            fields = stat_fields(pid)
            if fields and (int(fields[3]) in roots or int(fields[1]) in members):
                members.add(pid)
                changed = True
    return members


def read_pids(*paths: Path) -> list[int]:
    return [int(path.read_text().strip()) for path in paths]


# --- HTTP to the internal listener ----------------------------------------------------------------------

def mcp_status(port: int, key: str | None, *, host: str | None = None, path: str = '/mcp') -> int:
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
               'Host': host or f'127.0.0.1:{port}'}
    if key is not None:
        headers['Authorization'] = f'Bearer {key}'
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        connection.request('POST', path, body=INIT_BODY, headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def port_of(url: str) -> int:
    match = re.fullmatch(r'http://127\.0\.0\.1:(\d+)/mcp', url)
    assert match, url
    return int(match.group(1))


def isolation_disabled() -> list[str]:
    text = (REPO / 'omp_bridge' / 'omp-isolation.yml').read_text()
    match = re.search(r'^disabledProviders:\s*\[([^\]]*)\]', text, re.M)
    assert match
    return [item.strip() for item in match.group(1).split(',') if item.strip()]


# --- LockHolderReadRaceTests: the flaky root cause, deterministic ---------------------------------------

class LockHolderReadRaceTests(unittest.TestCase):
    """``/proc/locks`` is a multi-chunk seq_file: while other processes take and drop flocks a read can skip
    the Hub's row (measured under churn: 0.3-1 % of reads). ``lock_holder`` then answers None and
    ``wb hub status`` prints "Hub 실행 중 아님" (rc 3) for a Hub that is running. The first test replays the skipped
    row deterministically; a lock that really is free must still end in None, quickly (second test).
    """

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='wbl-'))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.lock = self.directory / 'hub.lock'

    def hold(self) -> int:
        import fcntl
        stream = open(self.lock, 'w')
        self.addCleanup(stream.close)
        fcntl.flock(stream, fcntl.LOCK_EX)
        return os.getpid()

    @staticmethod
    def skipping(skipped_reads: int, inode: int):
        real = Path.read_text
        calls = {'n': 0}

        def read_text(path, *args, **kwargs):
            text = real(path, *args, **kwargs)
            if str(path) != '/proc/locks':
                return text
            calls['n'] += 1
            if calls['n'] > skipped_reads:
                return text
            rows = [row for row in text.splitlines()
                    if not (len(row.split()) >= 6 and row.split()[5].split(':')[-1] == str(inode))]
            return '\n'.join(rows) + '\n'
        return mock.patch.object(Path, 'read_text', read_text), calls

    def test_a_transiently_skipped_row_does_not_make_a_running_hub_not_running(self):
        pid = self.hold()
        patch, calls = self.skipping(1, os.stat(self.lock).st_ino)
        with patch:
            holder = lock_holder(self.lock)
        self.assertEqual(holder, pid, f'lock_holder missed a held lock after {calls["n"]} read(s) of /proc/locks')

    def test_a_free_lock_still_ends_in_none_without_a_long_wait(self):
        self.lock.write_text('')
        started = time.monotonic()
        self.assertIsNone(lock_holder(self.lock))
        self.assertLess(time.monotonic() - started, 2.0)


# --- overlay / argv / key (no process of the Board agent is started) ------------------------------------

class OverlayIndependentTests(LauncherCase):
    def overlay(self) -> Path:
        return self.box.state / 'board-agent' / 'omp-board.yml'

    def mcp_file(self) -> Path:
        return self.box.state / 'board-agent' / 'omp-root' / 'agent' / 'mcp.json'

    def test_overlay_re_enables_only_native_and_leaves_every_other_provider_and_project_mcp_off(self):
        self.prepare()
        isolation = isolation_disabled()
        self.assertIn('native', isolation)  # the premise: isolation turns everything off, native included
        overlay = json.loads(self.overlay().read_text())
        self.assertEqual(sorted(overlay['disabledProviders']), sorted(set(isolation) - {'native'}))
        self.assertEqual(len(overlay['disabledProviders']), len(set(overlay['disabledProviders'])))
        self.assertIs(overlay['mcp']['enableProjectConfig'], False)
        self.assertEqual(set(overlay['mcp']), {'enableProjectConfig'})  # no servers declared in the overlay
        self.assertTrue(set(overlay) <= {'disabledProviders', 'mcp', 'skills', 'task', 'memory', 'memories'})
        self.assertEqual(overlay['skills'], {'customDirectories': [], 'includeSkills': [], 'ignoredSkills': []})

    def test_mcp_json_declares_exactly_the_hub_server_on_the_internal_listener(self):
        started = self.prepare()
        config = json.loads(self.mcp_file().read_text())
        self.assertEqual(list(config), ['mcpServers'])
        self.assertEqual(list(config['mcpServers']), ['workbench-hub'])
        self.assertEqual(config['mcpServers']['workbench-hub'], {
            'type': 'http', 'url': f'http://127.0.0.1:{self.listener.port}/mcp',
            'headers': {'Authorization': 'Bearer ${WB_BOARD_MCP_KEY}'}})
        self.assertNotIn(started.env[KEY_ENV], self.mcp_file().read_text())

    def test_the_users_own_mcp_config_and_omp_files_never_reach_the_board_home(self):
        user_agent = self.box.home / '.omp' / 'agent'
        (user_agent / 'mcp.json').write_text(json.dumps(
            {'mcpServers': {'user-decoy': {'type': 'http', 'url': 'http://203.0.113.9/'}}}))
        (user_agent / 'config.yml').write_text('user-setting-canary: 1\n')
        started = self.prepare()
        base = self.box.state / 'board-agent'
        for path in base.rglob('*'):
            if path.is_file() and not path.is_symlink():
                text = path.read_text(errors='replace')
                self.assertNotIn('user-decoy', text, path)
                self.assertNotIn('user-setting-canary', text, path)
        links = sorted(str(p.relative_to(base)) for p in base.rglob('*') if p.is_symlink())
        self.assertEqual(links, ['omp-root/agent/agent.db'])  # the only link into the user's OMP dir
        self.assertNotIn(str(self.box.home / '.omp'), ' '.join(started.argv))

    def test_the_raw_key_exists_only_in_the_agent_environment(self):
        started = self.prepare()
        key = started.env[KEY_ENV]
        self.assertTrue(key.startswith('wbk_'), 'system key shape')
        self.assertEqual([name for name, value in started.env.items() if key in value], [KEY_ENV])
        self.assertFalse([item for item in started.argv if key in item])
        for path in self.box.root.rglob('*'):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(key.encode(), path.read_bytes(), f'raw key written to {path}')
        self.assertEqual(stat.S_IMODE(self.overlay().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.mcp_file().stat().st_mode), 0o600)

    def test_stale_or_hostile_files_are_replaced_at_every_start_never_written_through(self):
        self.prepare()
        victim = self.box.root / 'victim.txt'
        victim.write_text('untouched\n')
        self.mcp_file().unlink()
        self.mcp_file().symlink_to(victim)
        self.overlay().write_text('{"disabledProviders": []}\n')
        self.overlay().chmod(0o644)
        self.prepare()
        self.assertEqual(victim.read_text(), 'untouched\n')
        self.assertFalse(self.mcp_file().is_symlink())
        self.assertEqual(stat.S_IMODE(self.mcp_file().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.overlay().stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.overlay().read_text())['disabledProviders'][0], 'omp-managed')

    def probe(self, port, key, **kw):
        async def call():
            return await asyncio.get_running_loop().run_in_executor(None, lambda: mcp_status(port, key, **kw))
        return self.arun(call())

    def test_every_start_rotates_the_key_and_the_listener_refuses_the_old_one_over_http(self):
        first = self.prepare()
        port = self.listener.port
        self.assertEqual(self.probe(port, first.env[KEY_ENV]), 200)
        second = self.prepare()
        third = self.prepare()
        keys = [first.env[KEY_ENV], second.env[KEY_ENV], third.env[KEY_ENV]]
        self.assertEqual(len(set(keys)), 3)
        self.assertEqual(self.listener.port, port)  # one listener for all starts
        self.assertEqual([self.probe(port, key) for key in keys], [401, 401, 200])
        self.assertEqual(self.probe(port, None), 401)
        self.assertEqual(self.probe(port, 'wbk_' + 'x' * 40), 401)

    def test_the_internal_listener_is_loopback_only_fixed_host_and_serves_only_mcp(self):
        started = self.prepare()
        port, key = self.listener.port, started.env[KEY_ENV]
        self.assertEqual([item for item in tcp_listening(os.getpid()) if item[1] == port], [('127.0.0.1', port)])
        self.assertEqual(self.probe(port, key), 200)
        self.assertEqual(self.probe(port, key, host=f'localhost:{port}'), 421)
        self.assertEqual(self.probe(port, key, host='127.0.0.1:1'), 421)
        self.assertEqual(self.probe(port, key, host=f'evil.example:{port}'), 421)
        for path in ('/', '/board', '/login', '/static/x.js', '/mcp/x'):
            self.assertEqual(self.probe(port, key, path=path), 404, path)


# --- in-process BoardSet with real processes ------------------------------------------------------------

class MarkedSetCase(LauncherCase):
    """BoardSet over the fake omp where every process carries a mark; a leftover fails the test."""

    grace = 0.5

    def setUp(self):
        self.mark = f'{os.getpid()}-{time.monotonic_ns()}'
        super().setUp()
        self.addCleanup(self.check_clean)
        self.audits: list[tuple] = []
        self.rebuild(self.config)

    def rebuild(self, config: BoardAgentConfig, **env) -> None:
        self.box.env.update(env)
        self.box.env[MARK] = self.mark
        self.launcher = self.make_launcher(config)
        self.board = BoardSet(self.launcher, grace=self.grace, audit=lambda *row: self.audits.append(row))

    def check_clean(self):
        self.arun(self.board.stop())
        reap_marked(self, self.mark)

    def status(self):
        return self.board.status()

    def start(self):
        self.arun(self.board.ensure_started())
        self.arun(self.board.settled())

    def script(self, name: str, text: str) -> str:
        path = self.box.bin / name
        path.write_text('#!/bin/sh\n' + text)
        path.chmod(0o755)
        return str(path)


class BoardSetIndependentTests(MarkedSetCase):
    def test_simultaneous_first_opens_start_exactly_one_set(self):
        async def three():
            await asyncio.gather(*(self.board.ensure_started() for _ in range(3)))
            await self.board.settled()
        self.arun(three())
        self.assertEqual(len(self.box.records('tty')), 1)
        starts = [row for row in self.audits if row[0] == 'board_start']
        self.assertEqual(sorted(row[3]['channel'] for row in starts), ['agent', 'shell'])
        self.assertEqual({row[3]['reason'] for row in starts}, {'first_open'})

    def test_a_restart_during_the_start_up_check_leaves_one_agent_and_no_check_process_behind(self):
        self.rebuild(self.config, FAKE_BOARD_RPC_SLEEP='4')
        self.arun(self.board.ensure_started())
        first = self.status()['agent']
        self.assertEqual(first['state'], 'starting')  # the check is in flight
        self.assertTrue(wait_until(lambda: self.box.records('rpc'), 10))
        started = time.monotonic()
        self.arun(self.board.restart('agent'))
        self.assertLess(time.monotonic() - started, 3.0, 'the restart waited for the whole in-flight check')
        self.assertFalse(alive(first['pid']))
        self.arun(self.board.settled())
        agent = self.status()['agent']
        self.assertNotEqual(agent['pid'], first['pid'])
        self.assertEqual(agent['state'], 'running')
        checks = [pid for pid in marked(self.mark)
                  if b'rpc' in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')]
        self.assertFalse(checks, f'check processes still running: {checks}')
        self.assertEqual(len(self.box.records('tty')), 2)

    def test_stop_ends_stubborn_background_jobs_of_both_channels_in_any_group_without_orphans(self):
        out = self.box.root / 'out'
        out.mkdir()
        shell = self.script('stubborn-shell', textwrap.dedent(f'''\
            trap '' HUP TERM
            sleep 600 &
            echo $! > "{out}/bg1"
            {sys.executable} -c "import subprocess,sys; p=subprocess.Popen(['sleep','600'],process_group=0,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); open(sys.argv[1],'w').write(str(p.pid))" "{out}/bg2"
            echo ready > "{out}/ready"
            exec sleep 600
            '''))
        self.rebuild(BoardAgentConfig(shell=shell))
        self.start()
        self.assertTrue(wait_until(lambda: (out / 'ready').exists(), 10))
        pids = [self.status()['agent']['pid'], self.status()['shell']['pid']]
        bg = read_pids(out / 'bg1', out / 'bg2')
        members = family(pids, self.mark)
        self.assertTrue(set(pids + bg) <= members, (pids, bg, members))
        self.assertEqual(stat_fields(bg[1])[2], str(bg[1]))  # bg2 really leads its own group
        started = time.monotonic()
        self.arun(self.board.stop())
        self.assertLess(time.monotonic() - started, self.grace * 2 + 6)
        self.assertEqual({p: alive(p) for p in members}, {p: False for p in members})
        self.assertEqual(marked(self.mark), {})
        self.assertEqual((self.status()['agent']['state'], self.status()['shell']['state']), ('stopped', 'stopped'))
        self.assertEqual(self.board.active(), [])
        self.assertFalse(self.board.write('shell', b'x\n'))
        self.arun(self.board.ensure_started())  # the stopped set never starts again
        self.assertEqual(len(self.box.records('tty')), 1)

    def test_restart_of_one_channel_leaves_the_other_process_and_its_jobs_alone(self):
        out = self.box.root / 'out'
        out.mkdir()
        shell = self.script('job-shell', f'sleep 600 &\necho $! > "{out}/job"\nexec sleep 600\n')
        self.rebuild(BoardAgentConfig(shell=shell))
        self.start()
        self.assertTrue(wait_until(lambda: (out / 'job').exists(), 10))
        shell_pid = self.status()['shell']['pid']
        job = read_pids(out / 'job')[0]
        shell_ticks, job_ticks = ticks(shell_pid), ticks(job)
        agent = self.status()['agent']['pid']
        self.arun(self.board.restart('agent'))
        self.arun(self.board.settled())
        self.assertEqual((self.status()['shell']['pid'], ticks(shell_pid), ticks(job)), (shell_pid, shell_ticks, job_ticks))
        self.assertTrue(alive(job))
        self.assertFalse(alive(agent))
        agent2 = self.status()['agent']['pid']
        agent2_ticks = ticks(agent2)
        self.arun(self.board.restart('shell'))
        self.assertEqual((self.status()['agent']['pid'], ticks(agent2)), (agent2, agent2_ticks))
        self.assertFalse(alive(shell_pid))
        self.assertFalse(alive(job), 'the old shell session member survived the shell restart')

    def test_no_channel_holds_the_pty_master_of_another_channel(self):
        """``pty.fork`` leaves the master inheritable: the channel started second would keep the first one's
        master (and every job it starts), so neither a Hub kill nor an ended channel closes that terminal."""
        self.start()
        for name in ('agent', 'shell'):
            pid = self.status()[name]['pid']
            held = [os.readlink(f'/proc/{pid}/fd/{fd}') for fd in os.listdir(f'/proc/{pid}/fd')
                    if os.path.exists(f'/proc/{pid}/fd/{fd}')]
            self.assertNotIn('/dev/ptmx', held, f'the {name} channel holds a PTY master: {held}')
        self.arun(self.board.restart('agent'))
        self.arun(self.board.settled())
        pid = self.status()['agent']['pid']
        held = [os.readlink(f'/proc/{pid}/fd/{fd}') for fd in os.listdir(f'/proc/{pid}/fd')]
        self.assertNotIn('/dev/ptmx', held, 'a restarted agent holds the shell channel\'s PTY master')

    def test_active_lists_only_live_channels_and_changes_with_every_channel_state(self):
        self.start()
        first = self.board.active()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]['kind'], 'board_agent_set')
        self.assertEqual({name: item['state'] for name, item in first[0]['channels'].items()},
                         {'agent': 'running', 'shell': 'running'})
        self.board.write('shell', b'exit 0\n')
        self.assertTrue(wait_until(lambda: (self.arun(asyncio.sleep(0.05)), self.status()['shell']['state'])[1]
                                   == 'exited', 10))
        second = self.board.active()
        self.assertNotEqual(first, second)  # a confirm against the older offer cannot match
        self.assertEqual(second[0]['channels']['shell']['state'], 'exited')
        self.board.write('agent', b'exit 0\n')
        self.assertTrue(wait_until(lambda: (self.arun(asyncio.sleep(0.05)), self.status()['agent']['state'])[1]
                                   == 'exited', 10))
        self.assertEqual(self.board.active(), [])  # nothing left to end: the stop asks nothing


# --- live Hub ------------------------------------------------------------------------------------------

def sh_quote(text: object) -> str:
    return "'" + str(text).replace("'", "'\\''") + "'"


class Rig:
    """A real Hub in a WebSandbox with the fake Board agent, an instrumented shell and a logged-in browser."""

    def __init__(self, case: unittest.TestCase, *, leader_stubborn: bool = False, extra_env: dict | None = None,
                 expect_leftovers: bool = False):
        self.case = case
        self.mark = f'{os.getpid()}-{time.monotonic_ns()}'
        case.addCleanup(lambda: reap_marked(case, self.mark, expect_leftovers))  # LIFO: after the Hub is stopped
        self.box = box = WebSandbox(case)
        box.env[MARK] = self.mark
        box.env[CANARY[0]] = CANARY[1]
        box.env['SHELL'] = '/bin/sh'
        if extra_env:
            box.env.update(extra_env)
        self.user_store = box.home / '.omp' / 'agent' / 'agent.db'
        self.user_store.parent.mkdir(parents=True)
        self.user_store.write_bytes(b'')
        self.out = box.base / 'out'
        self.out.mkdir()
        self.record = box.base / 'record.jsonl'
        bin_dir = box.base / 'bin'
        bin_dir.mkdir()
        omp = bin_dir / 'omp'
        omp.write_text(textwrap.dedent(f'''\
            #!/bin/sh
            case " $* " in
              *" --version "*|*" --mode rpc "*) ;;
              *)
                for f in /proc/$$/fd/*; do readlink "$f"; done > "{self.out}/agent.fds" 2>&1
                (trap '' HUP TERM; exec sleep 600) </dev/null >/dev/null 2>&1 &
                echo $! > "{self.out}/agent.bg"
                ;;
            esac
            exec {sh_quote(sys.executable)} {sh_quote(FAKE_OMP)} "$@"
            '''))
        omp.chmod(0o755)
        shell = bin_dir / 'board-shell'
        shell.write_text(textwrap.dedent(f'''\
            #!/bin/sh
            env > "{self.out}/shell.env"
            for f in /proc/$$/fd/*; do readlink "$f"; done > "{self.out}/shell.fds"
            cat /proc/$$/stat > "{self.out}/shell.stat"
            pwd -P > "{self.out}/shell.pwd"
            echo $$ > "{self.out}/shell.pid"
            trap '' HUP TERM
            sleep 600 &
            echo $! > "{self.out}/shell.bg1"
            {sh_quote(sys.executable)} -c "import subprocess,sys; p=subprocess.Popen(['sleep','600'],process_group=0,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); open(sys.argv[1],'w').write(str(p.pid))" "{self.out}/shell.bg2"
            echo ready > "{self.out}/shell.ready"
            {'' if leader_stubborn else 'trap - HUP TERM'}
            exec sleep 600
            '''))
        shell.chmod(0o755)
        self.shell_path = shell
        box.env.update({'WORKBENCH_OMP': str(omp), 'FAKE_BOARD_RECORD': str(self.record)})
        box.write_config(raw=textwrap.dedent(f'''\
            [web]
            enabled = true
            bind = "127.0.0.1"
            port = 0
            [board_agent]
            shell = "{shell}"
            '''))
        box.set_password()
        self.hub: subprocess.Popen | None = None
        self.browser: Browser | None = None
        self.start_hub()

    # -- hub ----------------------------------------------------------------------------------------

    def start_hub(self) -> subprocess.Popen:
        box = self.box
        self.hub = box.start_hub()
        host, port = box.wait_listening(self.hub.pid)
        self.browser = Browser(host, port)
        _, _, page = self.browser.request('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        status, _, _ = self.browser.request('POST', '/login', {'login_token': token, 'password': PASSWORD})
        self.case.assertEqual(status, 303)
        return self.hub

    def cli(self, *args: str, **kw) -> subprocess.CompletedProcess:
        """``wb ...`` with a retry only for the "not running" answer while the Hub is alive (see module doc)."""
        for _ in range(6):
            result = self.box.cli(*args, **kw)
            if result.returncode != 3 or self.hub.poll() is not None:
                break
            time.sleep(0.2)
        return result

    def status(self) -> dict:
        result = self.cli('hub', 'status', '--json')
        self.case.assertEqual(result.returncode, 0, result)
        return json.loads(result.stdout)

    def board(self) -> dict | None:
        return self.status()['board_agent']

    def csrf(self) -> str:
        _, _, page = self.browser.request('GET', '/settings')
        return re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def restart(self, channel: str) -> int:
        status, _, _ = self.browser.request('POST', '/board/restart', {'csrf': self.csrf(), 'channel': channel})
        return status

    def wait_state(self, channel: str, state: str, timeout: float = 20) -> dict:
        self.case.assertTrue(wait_until(lambda: ((self.board() or {}).get(channel) or {}).get('state') == state,
                                        timeout), self.board())
        return self.board()[channel]

    def start_set(self) -> dict[str, int]:
        self.case.assertEqual(self.restart('agent'), 303)
        self.case.assertEqual(self.restart('shell'), 303)
        self.wait_state('agent', 'running')
        self.wait_state('shell', 'running')
        self.case.assertTrue(wait_until((self.out / 'shell.ready').exists, 10))
        self.case.assertTrue(wait_until((self.out / 'agent.bg').exists, 10))
        board = self.board()
        return {'agent': board['agent']['pid'], 'shell': board['shell']['pid']}

    # -- files --------------------------------------------------------------------------------------

    def records(self, mode: str | None = None) -> list[dict]:
        try:
            rows = [json.loads(line) for line in self.record.read_text().splitlines() if line.strip()]
        except FileNotFoundError:
            return []
        return [row for row in rows if mode is None or row['mode'] == mode]

    @property
    def state(self) -> Path:
        return self.box.state

    def audit(self, event: str) -> list[dict]:
        db = sqlite3.connect(f'file:{self.state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            rows = db.execute('SELECT actor, detail_json FROM audit WHERE event = ? ORDER BY id', (event,)).fetchall()
        finally:
            db.close()
        return [{'actor': actor, **json.loads(detail)} for actor, detail in rows]

    def background_jobs(self) -> list[int]:
        return read_pids(self.out / 'agent.bg', self.out / 'shell.bg1', self.out / 'shell.bg2')

    def tty_stop(self, answer: str | None, *args: str, timeout: float = 60) -> tuple[int, str]:
        """``wb hub stop`` on a real terminal (stdin and stdout are one PTY); answers the question once."""
        code, text = 3, ''
        for _ in range(6):
            master, slave = pty.openpty()
            process = subprocess.Popen([sys.executable, '-m', 'workbench', 'hub', 'stop', *args], env=self.box.env,
                                       cwd=self.box.base, stdin=slave, stdout=slave, stderr=slave,
                                       start_new_session=True)
            os.close(slave)
            data, answered = b'', False
            deadline = time.monotonic() + timeout
            try:
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.2)
                    if ready:
                        try:
                            chunk = os.read(master, 4096)
                        except OSError as exc:
                            if exc.errno == errno.EIO:
                                break
                            raise
                        if not chunk:
                            break
                        data += chunk
                    if answer is not None and not answered and b'[y/N]' in data:
                        os.write(master, answer.encode() + b'\n')
                        answered = True
                    if process.poll() is not None and not ready:
                        break
                code = process.wait(timeout=20)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                os.close(master)
            text = data.decode('utf-8', 'replace')
            if code != 3 or self.hub.poll() is not None:
                break
            time.sleep(0.2)
        return code, text

    def stop_via_sigterm(self) -> int:
        return self.box.stop_hub(self.hub, timeout=40)

    def every_file(self, exclude: tuple[Path, ...] = ()) -> list[tuple[Path, bytes]]:
        found = []
        for root in (self.box.base, self.box.runtime):
            for path in root.rglob('*'):
                if path in exclude or path.is_symlink() or not path.is_file():
                    continue
                try:
                    found.append((path, path.read_bytes()))
                except OSError:
                    pass
        return found


class LiveBoardTests(unittest.TestCase):
    def assert_only_own_terminal(self, rig: 'Rig', name: str, listing: Path, script: Path):
        """The channel holds its own slave (fd 0-2) and what the instrumentation itself opened; no Hub fd, and
        in particular no PTY master (``/dev/ptmx``) of the sibling channel."""
        fds = listing.read_text().split()
        allowed = (str(script), str(listing), '/dev/null')
        stray = [item for item in fds if not re.fullmatch(r'/dev/pts/\d+', item) and item not in allowed]
        self.assertEqual(stray, [], f'the {name} inherited descriptors of the Hub: {stray}')
        self.assertEqual(len([item for item in fds if item.startswith('/dev/pts/')]), 3, fds)


    def test_nothing_starts_with_the_hub(self):
        rig = Rig(self)
        self.assertIsNone(rig.board())
        self.assertFalse((rig.state / 'board-agent').exists(), 'the Board home exists before the first open')
        self.assertEqual(rig.records(), [])
        self.assertEqual(family([rig.hub.pid], rig.mark) - {rig.hub.pid}, set())
        stopped = rig.cli('hub', 'stop')  # nothing owned: no question
        self.assertEqual(stopped.returncode, 0, stopped)
        self.assertNotIn('Board agent', stopped.stdout)
        self.assertEqual(rig.hub.wait(timeout=20), 0)

    def test_restart_agent_does_not_touch_shell_and_restart_shell_does_not_touch_agent(self):
        rig = Rig(self)
        self.assertEqual(rig.restart('agent'), 303)
        agent = rig.wait_state('agent', 'running')
        self.assertEqual(rig.board()['shell']['state'], 'not_started')
        self.assertIsNone(rig.board()['shell']['pid'])
        self.assertEqual(rig.restart('shell'), 303)
        shell = rig.wait_state('shell', 'running')
        self.assertTrue(wait_until((rig.out / 'shell.ready').exists, 10))
        shell_ticks = ticks(shell['pid'])
        jobs = read_pids(rig.out / 'shell.bg1', rig.out / 'shell.bg2')
        self.assertEqual(rig.restart('agent'), 303)
        new_agent = rig.wait_state('agent', 'running')
        self.assertNotEqual(new_agent['pid'], agent['pid'])
        self.assertFalse(alive(agent['pid']))
        self.assertEqual((rig.board()['shell']['pid'], ticks(shell['pid'])), (shell['pid'], shell_ticks))
        self.assertTrue(all(alive(job) for job in jobs))  # the shell's own jobs are not the agent's business
        agent_ticks = ticks(new_agent['pid'])
        self.assertEqual(rig.restart('shell'), 303)
        new_shell = rig.wait_state('shell', 'running')
        self.assertNotEqual(new_shell['pid'], shell['pid'])
        self.assertEqual((rig.board()['agent']['pid'], ticks(new_agent['pid'])), (new_agent['pid'], agent_ticks))
        self.assertFalse(alive(shell['pid']))
        self.assertTrue(wait_until(lambda: not any(alive(job) for job in jobs), 6), 'old shell jobs survived')
        # unknown channel and a forged CSRF change nothing
        status, _, _ = rig.browser.request('POST', '/board/restart', {'csrf': rig.csrf(), 'channel': 'both'})
        self.assertIn(status, (400, 404, 422))
        status, _, _ = rig.browser.request('POST', '/board/restart', {'csrf': 'forged', 'channel': 'agent'})
        self.assertIn(status, (400, 403))
        self.assertEqual(rig.board()['agent']['pid'], new_agent['pid'])

    def test_agent_process_facts_overlay_and_environment(self):
        rig = Rig(self)
        pids = rig.start_set()
        record, = rig.records('tty')
        home = rig.box.home
        board_root = rig.state / 'board-agent' / 'omp-root'
        self.assertEqual((record['pid'], record['ppid']), (pids['agent'], rig.hub.pid))
        self.assertEqual((record['sid'], record['pgid']), (record['pid'], record['pid']))
        self.assertNotEqual(session_of(rig.hub.pid), record['sid'])
        self.assertTrue(record['isatty'])
        self.assertEqual(record['cwd'], str(home))
        argv = record['argv']
        configs = [argv[i + 1] for i, item in enumerate(argv[:-1]) if item == '--config']
        self.assertEqual(configs, [str(REPO / 'omp_bridge' / 'omp-isolation.yml'),
                                   str(rig.state / 'board-agent' / 'omp-board.yml')])
        for flag in ('--no-extensions', '--allow-home', '--no-title', '--append-system-prompt'):
            self.assertIn(flag, argv)
        self.assertNotIn('--extension', argv)
        self.assertNotIn('--continue', argv)  # a fresh home has no session to continue
        self.assertEqual(argv[argv.index('--append-system-prompt') + 1],
                         (REPO / 'omp_bridge' / 'board' / 'ROLE.md').read_text())
        overlay = json.loads(record['overlay'])
        self.assertEqual(sorted(overlay['disabledProviders']), sorted(set(isolation_disabled()) - {'native'}))
        mcp_json = json.loads(record['mcp_json'])
        self.assertEqual(list(mcp_json['mcpServers']), ['workbench-hub'])
        server = mcp_json['mcpServers']['workbench-hub']
        self.assertEqual(server['headers'], {'Authorization': 'Bearer ${WB_BOARD_MCP_KEY}'})
        env = record['env']
        self.assertEqual(env['HOME'], str(home))
        self.assertEqual(env['PI_CODING_AGENT_DIR'], str(board_root / 'agent'))
        self.assertEqual(os.path.realpath(os.path.join(home, env['PI_CONFIG_DIR'])), os.path.realpath(board_root))
        self.assertEqual(env['TERM'], 'xterm-256color')
        self.assertEqual(os.readlink(board_root / 'agent' / 'agent.db'), str(rig.user_store))
        for directory in (rig.state / 'board-agent', board_root):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        port = port_of(server['url'])
        hub_listeners = tcp_listening(rig.hub.pid)
        self.assertIn(('127.0.0.1', port), hub_listeners)
        self.assertTrue(all(host.startswith('127.') for host, _ in hub_listeners), hub_listeners)
        self.assert_only_own_terminal(rig, 'agent', rig.out / 'agent.fds', rig.box.base / 'bin' / 'omp')

    def test_the_raw_key_is_in_the_agent_environment_only_and_rotates_per_start_and_hub_start(self):
        rig = Rig(self)
        pids = rig.start_set()
        first = rig.records('tty')[0]
        first_key = first['env'][KEY_ENV]
        self.assertTrue(first_key.startswith('wbk_'))
        port = port_of(json.loads(first['mcp_json'])['mcpServers']['workbench-hub']['url'])
        self.assertEqual(mcp_status(port, first_key), 200)
        for path, data in rig.every_file(exclude=(rig.record,)):
            self.assertNotIn(first_key.encode(), data, f'raw key found in {path}')
        for pid in (rig.hub.pid, pids['agent'], pids['shell']):
            self.assertNotIn(first_key.encode(), Path(f'/proc/{pid}/cmdline').read_bytes(), pid)
        self.assertNotIn(first_key.encode(), Path(f'/proc/{pids["shell"]}/environ').read_bytes())
        self.assertNotIn(first_key.encode(), Path(f'/proc/{rig.hub.pid}/environ').read_bytes())
        for event in ('login', 'board_start', 'board_restart'):
            self.assertNotIn(first_key, json.dumps(rig.audit(event)))
        # a restart: a new key; the old one is refused over the real listener
        self.assertEqual(rig.restart('agent'), 303)
        rig.wait_state('agent', 'running')
        second = rig.records('tty')[-1]
        second_key = second['env'][KEY_ENV]
        self.assertNotEqual(first_key, second_key)
        self.assertEqual((mcp_status(port, first_key), mcp_status(port, second_key)), (401, 200))
        for path, data in rig.every_file(exclude=(rig.record,)):
            self.assertNotIn(second_key.encode(), data, f'raw key found in {path}')
        self.assertNotIn(second_key, second['mcp_json'])
        # a new Hub: another key again; every earlier key is refused by the new listener
        self.assertEqual(rig.cli('hub', 'stop', '--yes').returncode, 0)
        self.assertEqual(rig.hub.wait(timeout=30), 0)
        with self.assertRaises(OSError):  # the old internal listener is gone with the Hub
            socket.create_connection(('127.0.0.1', port), timeout=2).close()
        rig.start_hub()
        self.assertEqual(rig.restart('agent'), 303)
        rig.wait_state('agent', 'running')
        third = rig.records('tty')[-1]
        third_key = third['env'][KEY_ENV]
        new_port = port_of(json.loads(third['mcp_json'])['mcpServers']['workbench-hub']['url'])
        self.assertEqual(len({first_key, second_key, third_key}), 3)
        self.assertEqual([mcp_status(new_port, key) for key in (first_key, second_key, third_key)], [401, 401, 200])
        logs = [path.read_text(errors='replace') for path in rig.box.base.glob('hub-*.log')]
        hub_log = rig.state / 'hub.log'
        logs.append(hub_log.read_text(errors='replace') if hub_log.exists() else '')
        for key in (first_key, second_key, third_key):
            self.assertFalse([text for text in logs if key in text], 'raw key in a Hub log')

    def test_the_shell_tab_is_the_users_shell_without_hub_secrets(self):
        rig = Rig(self)
        pids = rig.start_set()
        env = dict(item.split('=', 1) for item in (rig.out / 'shell.env').read_text().splitlines() if '=' in item)
        home = rig.box.home
        self.assertEqual(env['HOME'], str(home))
        self.assertEqual(env['PATH'], rig.box.env['PATH'])
        self.assertEqual(env['XDG_STATE_HOME'], str(rig.box.state_home))  # the user's real XDG, not a board one
        self.assertEqual(env[CANARY[0]], CANARY[1])
        self.assertEqual(env['TERM'], 'xterm-256color')
        for name in (KEY_ENV, 'PI_CONFIG_DIR', 'PI_CODING_AGENT_DIR', 'WORKBENCH_DATA_DIR'):
            self.assertNotIn(name, env)
        flat = '\n'.join(env.values())
        self.assertNotIn(PASSWORD, flat)
        self.assertNotIn('wbk_', flat)
        self.assertEqual((rig.out / 'shell.pwd').read_text().strip(), str(home.resolve()))
        fields = (rig.out / 'shell.stat').read_text().rsplit(') ', 1)[1].split()
        self.assertEqual((int(fields[1]), int(fields[2]), int(fields[3])), (rig.hub.pid, pids['shell'], pids['shell']))
        self.assertEqual(read_pids(rig.out / 'shell.pid')[0], pids['shell'])
        self.assert_only_own_terminal(rig, 'shell', rig.out / 'shell.fds', rig.shell_path)

    def test_the_board_home_and_its_session_outlive_the_hub_and_the_users_omp_dir_is_not_written(self):
        rig = Rig(self)
        user_omp = rig.box.home / '.omp'

        def snapshot():
            return sorted((str(path.relative_to(rig.box.home)), path.lstat().st_mode, path.lstat().st_size,
                           path.lstat().st_mtime_ns) for path in user_omp.rglob('*'))
        before = snapshot()
        rig.start_set()
        agent_dir = rig.state / 'board-agent' / 'omp-root' / 'agent'
        sessions = agent_dir / 'sessions' / '-home'
        sessions.mkdir(parents=True)
        (sessions / 'previous.jsonl').write_text('{"turn": 1}\n')
        (agent_dir / 'memory-canary').write_text('persist\n')
        self.assertEqual(rig.restart('agent'), 303)  # same Hub: continue
        rig.wait_state('agent', 'running')
        self.assertIn('--continue', rig.records('tty')[-1]['argv'])
        code, text = rig.tty_stop('y')
        self.assertEqual(code, 0, text)
        self.assertEqual(rig.hub.wait(timeout=30), 0)
        self.assertEqual((sessions / 'previous.jsonl').read_text(), '{"turn": 1}\n')
        self.assertEqual((agent_dir / 'memory-canary').read_text(), 'persist\n')
        key_before = rig.records('tty')[-1]['env'][KEY_ENV]
        count_before = len(rig.records('tty'))
        rig.start_hub()  # a new Hub restores nothing (HB-O09)
        self.assertIsNone(rig.board())
        self.assertEqual(family([rig.hub.pid], rig.mark) - {rig.hub.pid}, set())
        self.assertEqual(len(rig.records('tty')), count_before)
        self.assertEqual(rig.restart('agent'), 303)  # the first open of this Hub
        rig.wait_state('agent', 'running')
        again = rig.records('tty')[-1]
        self.assertIn('--continue', again['argv'])
        self.assertNotEqual(again['env'][KEY_ENV], key_before)
        self.assertEqual(again['env']['PI_CODING_AGENT_DIR'], str(agent_dir))
        self.assertEqual(snapshot(), before, "the Board changed the user's ~/.omp")

    def test_a_leaking_agent_ends_alone_as_isolation_failed_and_the_shell_keeps_running(self):
        tools = json.dumps(['mcp__workbench_hub_inbox_list', 'mcp__user_private_tool'])
        rig = Rig(self, extra_env={'FAKE_BOARD_TOOLS': tools})
        self.assertEqual(rig.restart('shell'), 303)
        shell = rig.wait_state('shell', 'running')
        self.assertEqual(rig.restart('agent'), 303)
        agent = rig.wait_state('agent', 'isolation_failed')
        self.assertIn('mcp__user_private_tool', agent['detail'])
        self.assertNotIn('mcp__workbench_hub_inbox_list', agent['detail'])
        self.assertFalse(alive(agent['pid']))
        self.assertEqual(rig.board()['shell']['pid'], shell['pid'])
        self.assertTrue(alive(shell['pid']))
        self.assertIn(('agent', agent['pid'], 'isolation_failed'),
                      [(row['channel'], row['pid'], row['reason']) for row in rig.audit('board_stop')])
        text = rig.cli('hub', 'status').stdout
        self.assertIn('agent isolation_failed', text)
        self.assertIn(f'shell running(pid {shell["pid"]})', text)
        refused = rig.cli('hub', 'stop')  # the live shell still makes it a set the stop must ask about
        self.assertEqual(refused.returncode, 1, refused)
        self.assertIn('Board agent 세트', refused.stdout)
        self.assertIn(f'pid {shell["pid"]}', refused.stdout)


class LiveStopTests(unittest.TestCase):
    """TI-T07, P2-AC-33..37 on the Hub stop and the fate of every descendant."""

    def assert_everything_ended(self, rig: Rig, members: set[int], pids: dict[str, int]):
        self.assertTrue(members >= set(pids.values()) | set(rig.background_jobs()), members)
        self.assertEqual({pid: alive(pid) for pid in members}, {pid: False for pid in members})
        self.assertEqual(marked(rig.mark), {})

    def test_without_a_terminal_the_stop_lists_the_set_and_changes_nothing(self):
        rig = Rig(self)
        pids = rig.start_set()
        members = family(list(pids.values()), rig.mark)
        ticks_before = {pid: ticks(pid) for pid in members}
        result = rig.cli('hub', 'stop')
        self.assertEqual(result.returncode, 1, result)
        self.assertIn('Board agent 세트', result.stdout)
        self.assertIn(f'agent running(pid {pids["agent"]})', result.stdout)
        self.assertIn(f'shell running(pid {pids["shell"]})', result.stdout)
        self.assertIn('--yes', result.stderr)
        self.assertIsNone(rig.hub.poll())
        self.assertEqual({pid: ticks(pid) for pid in members}, ticks_before)
        self.assertEqual(rig.board()['agent']['pid'], pids['agent'])
        self.assertEqual(rig.audit('board_stop'), [])

    def test_declining_on_a_terminal_keeps_the_hub_and_every_process_of_the_set(self):
        rig = Rig(self)
        pids = rig.start_set()
        members = family(list(pids.values()), rig.mark)
        for answer in ('n', ''):
            code, text = rig.tty_stop(answer)
            self.assertEqual(code, 1, text)
            self.assertIn('Board agent 세트', text)
            self.assertIn(f'shell running(pid {pids["shell"]})', text)
            self.assertIsNone(rig.hub.poll())
            self.assertEqual({pid for pid in members if alive(pid)}, members)
            self.assertEqual(rig.board()['shell']['pid'], pids['shell'])
        self.assertEqual(rig.audit('board_stop'), [])

    def test_confirming_on_a_terminal_ends_the_whole_session_tree_then_the_hub(self):
        rig = Rig(self)
        pids = rig.start_set()
        members = family(list(pids.values()), rig.mark)
        self.assertEqual(len(rig.background_jobs()), 3)
        started = time.monotonic()
        code, text = rig.tty_stop('y')
        self.assertEqual(code, 0, text)
        self.assertEqual(rig.hub.wait(timeout=30), 0)
        self.assert_everything_ended(rig, members, pids)
        self.assertEqual(sorted((row['channel'], row['pid'], row['reason']) for row in rig.audit('board_stop')),
                         sorted((name, pid, 'hub_stop') for name, pid in pids.items()))
        self.assertLess(time.monotonic() - started, 30)

    def test_yes_ends_a_leader_that_ignores_hangup_and_term_after_the_grace_and_leaves_no_orphan(self):
        rig = Rig(self, leader_stubborn=True)
        pids = rig.start_set()
        members = family(list(pids.values()), rig.mark)
        result = rig.cli('hub', 'stop', '--yes')
        self.assertEqual(result.returncode, 0, result)
        self.assertEqual(rig.hub.wait(timeout=30), 0)
        self.assert_everything_ended(rig, members, pids)

    def test_sigterm_ends_the_set_without_a_question_including_stubborn_descendants(self):
        rig = Rig(self, leader_stubborn=True)
        pids = rig.start_set()
        members = family(list(pids.values()), rig.mark)
        self.assertEqual(rig.stop_via_sigterm(), 0)
        self.assert_everything_ended(rig, members, pids)

    def test_a_set_whose_channels_all_ended_by_themselves_is_not_asked_about_and_its_jobs_end_with_the_hub(self):
        rig = Rig(self)
        pids = rig.start_set()
        members = family(list(pids.values()), rig.mark)
        for pid in pids.values():
            kill_exact(pid, ticks(pid))
        self.assertTrue(wait_until(lambda: rig.board()['agent']['state'] == 'exited'
                                   and rig.board()['shell']['state'] == 'exited', 15), rig.board())
        result = rig.cli('hub', 'stop')
        self.assertEqual(result.returncode, 0, result)
        self.assertNotIn('Board agent', result.stdout)
        self.assertEqual(rig.hub.wait(timeout=20), 0)
        self.assertEqual({pid for pid in members if alive(pid)}, set(), 'jobs of ended channels outlive the Hub')

    def test_killing_the_hub_hangs_up_both_channel_leaders_and_a_new_hub_starts_clean(self):
        rig = Rig(self, expect_leftovers=True)  # jobs that ignore SIGHUP may stay: documented, not guaranteed
        pids = rig.start_set()
        members = family(list(pids.values()), rig.mark)
        leader_ticks = {pid: ticks(pid) for pid in pids.values()}
        rig.hub.kill()
        rig.hub.wait(timeout=10)
        self.assertTrue(wait_until(lambda: not any(alive(pid) and ticks(pid) == leader_ticks[pid]
                                                   for pid in pids.values()), 10),
                        'the PTY master closed with the Hub but a channel leader kept running')
        rig.start_hub()
        self.assertIsNone(rig.board())
        self.assertEqual(rig.restart('agent'), 303)
        agent = rig.wait_state('agent', 'running')
        self.assertNotIn(agent['pid'], members)
        self.assertEqual(rig.restart('shell'), 303)
        rig.wait_state('shell', 'running')
        self.assertEqual(rig.stop_via_sigterm(), 0)


class UpdateQuestionTests(maintenance_hub.UpdateHubFixture):
    """``wb update`` (fixture install, redirected fence, recording tool, never a real reinstall): the Board agent
    set joins the one question that also lists the project's active work."""

    PASSWORD = maintenance_hub.BoardSetUpdateTests.PASSWORD
    start_hub_with_board_shell = maintenance_hub.BoardSetUpdateTests.start_hub_with_board_shell
    ACTIVE = [{'kind': 'task_run', 'task_id': 'task-a', 'run_id': 'run-a'}]

    def test_one_question_names_the_active_work_the_hub_and_the_set_and_declining_changes_nothing(self):
        hub, shell = self.start_hub_with_board_shell()
        project, data, backend = self.recorded_project('project-a', self.ACTIVE)
        status, output, error = self.invoke(stdin=Tty('n\n'), tool=self.recording_tool)
        self.assertEqual(status, 1, output + error)
        self.assertEqual(output.count('[y/N]'), 1)
        question = output[:output.index('[y/N]')]
        for needle in ('task-a', 'Board agent 세트', f'shell running(pid {shell})', f'Hub(pid {hub.pid}'):
            self.assertIn(needle, question)
        self.assertEqual(self.events, [])  # no backend shutdown, no install
        self.assertIsNone(hub.poll())
        self.assertTrue(maintenance_hub.alive(shell))
        self.assertIsNone(backend.poll())

    def test_yes_ends_the_set_before_any_backend_is_touched_and_installs_exactly_once(self):
        hub, shell = self.start_hub_with_board_shell()
        project, data, backend = self.recorded_project('project-a', self.ACTIVE)
        status, output, error = self.invoke(stdin=Tty('y\n'), tool=self.recording_tool)
        self.assertEqual(status, 0, output + error)
        self.assertEqual(output.count('[y/N]'), 1)
        self.assertEqual(hub.wait(timeout=20), 0)
        self.assertFalse(maintenance_hub.alive(shell))
        self.assertEqual([name for name, _ in self.events], ['backend_shutdown', 'install'])
        for _name, view in self.events:
            self.assertEqual(view, {'lock_holder': None, 'hub_alive': []})


if __name__ == '__main__':
    unittest.main()
