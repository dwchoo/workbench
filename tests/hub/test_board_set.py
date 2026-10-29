"""TI-16 / WBX-D1: the Hub-owned Board terminals (``BoardSet``): PTY sessions, isolation failure, ring buffer, exit,
restart, start on open (seam T6b: fake ``omp`` + real BoardSet, real PTYs and processes). Migrated from the two fixed
channels to terminal ids: ``agent`` is ``agent-1`` (migrated, started by the first open), ``shell`` is ``shell-1``
(made by the first open); views and audit details carry ``terminal_id``/``kind``/``name`` instead of ``channel``.

Expected values come from ``/proc`` (pid, ppid, session), the fake's own records and the SPEC "Board agent 세트".
Every process a test starts is ended by the set's stop in cleanup and checked gone.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import time
import unittest

from test_board_launch import LauncherCase

from workbench.hub.board.set import BoardSet


def proc_stat(pid: int) -> list[str] | None:
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
    except (OSError, IndexError):
        return None


def alive(pid: int) -> bool:
    fields = proc_stat(pid)
    return fields is not None and fields[0] not in ('Z', 'X')


class SetCase(LauncherCase):
    def setUp(self):
        super().setUp()
        self.audits: list[tuple[str, str, dict]] = []
        self.pids: set[int] = set()
        self.board = self.make_board()
        self.addCleanup(self.final_stop)

    def make_board(self) -> BoardSet:
        return BoardSet(self.launcher, audit=lambda event, actor, address, detail:
                        self.audits.append((event, actor, detail)))

    def final_stop(self):
        self.arun(self.board.stop())
        for pid in self.pids:
            self.assertFalse(alive(pid), f'pid {pid} left running')

    def spin(self, seconds: float = 0.05):
        self.arun(asyncio.sleep(seconds))

    def wait(self, predicate, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            self.spin()
        return bool(predicate())

    def start(self):
        """The page's first open (v1: the first Board connection): agent-1 starts and shell-1 is made."""
        agent = self.board.agents and next(iter(self.board.agents))
        self.arun(self.board.open([agent] if agent else []))
        self.arun(self.board.settled())
        for view in self.board.views():
            if view['pid']:
                self.pids.add(view['pid'])

    def view(self, kind: str) -> dict:
        """The first terminal of ``kind`` (agent-1 / shell-1)."""
        return [view for view in self.board.views() if view['kind'] == kind][0]

    def tid(self, kind: str) -> str:
        return self.view(kind)['id']

    def who(self, kind: str, **more) -> dict:
        view = self.view(kind)
        return {'terminal_id': view['id'], 'kind': kind, 'name': view['name'], **more}

    def tty_records(self) -> list[dict]:
        return self.box.records('tty')


class AgentChannelTests(SetCase):
    def test_agent_runs_as_a_hub_child_leading_its_own_session_on_a_pty(self):
        self.start()
        self.assertTrue(self.wait(lambda: len(self.tty_records()) == 1))
        record, = self.tty_records()
        agent = self.view('agent')
        self.assertEqual((agent['state'], agent['pid']), ('running', record['pid']))
        self.assertEqual(record['ppid'], os.getpid())
        self.assertEqual(record['sid'], record['pid'])
        self.assertTrue(record['isatty'])
        self.assertIn(('board_start', 'owner', self.who('agent', pid=record['pid'], reason='first_open')),
                      self.audits)

    def test_an_isolation_leak_ends_the_agent_and_shows_isolation_failed(self):
        self.box.env['FAKE_BOARD_TOOLS'] = json.dumps(['mcp__workbench_hub_inbox_list', 'mcp__decoy_user_x'])
        self.launcher = self.make_launcher(self.config)
        self.board = self.make_board()
        self.start()
        agent = self.view('agent')
        self.assertEqual(agent['state'], 'isolation_failed')
        self.assertIn('mcp__decoy_user_x', agent['detail'])
        started, = [detail['pid'] for event, _, detail in self.audits
                    if event == 'board_start' and detail['kind'] == 'agent']
        self.assertEqual(agent['pid'], started)
        self.assertFalse(alive(started))
        self.assertIn(('board_stop', 'hub', self.who('agent', pid=started, reason='isolation_failed')),
                      self.audits)


def environ(pid: int) -> dict[str, str]:
    raw = Path(f'/proc/{pid}/environ').read_bytes()
    return dict(item.split('=', 1) for item in raw.decode().split('\0') if '=' in item)


def child_pids() -> set[int]:
    found: set[int] = set()
    for task in os.listdir('/proc/self/task'):
        try:
            found.update(int(item) for item in Path(f'/proc/self/task/{task}/children').read_text().split())
        except OSError:
            pass
    return found


class FailurePathTests(SetCase):
    def test_a_failed_pidfd_after_fork_leaves_no_child_and_the_channel_failed(self):
        from unittest import mock
        before = child_pids()
        with mock.patch('os.pidfd_open', side_effect=OSError(24, 'Too many open files')):
            self.arun(self.board.create_shell())  # v1: a restart of the fixed shell channel
        shell = self.view('shell')
        self.assertEqual((shell['state'], shell['detail']), ('failed', 'spawn_failed:Too many open files'))
        self.assertTrue(self.wait(lambda: child_pids() <= before, 5), child_pids() - before)

    def test_a_shell_that_cannot_start_notifies_the_watchers(self):
        self.launcher = self.make_launcher(type(self.config)(shell=str(self.box.root / 'no-such-shell')))  # WBX-D4: not $SHELL
        self.board = self.make_board()
        changes = []
        self.board.watch(lambda terminal_id, event: changes.append(self.board.view(terminal_id)['state']))
        self.arun(self.board.create_shell())  # v1: a restart of the fixed shell channel
        self.assertEqual(self.view('shell')['detail'], 'shell_unavailable')
        self.assertIn('failed', changes)

    def test_the_launch_repr_never_shows_the_key(self):
        started = self.prepare()
        self.assertNotIn(started.env['WB_API_KEY'], repr(started))

    def test_a_reset_rest_connection_is_at_most_one_log_line_not_a_traceback(self):  # WBX-B5: was /mcp
        import contextlib
        import io
        import socket
        import struct
        started = self.prepare()
        port = self.listener.port
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertNoLogs('aiohttp.server', level='ERROR'):
            sock = socket.create_connection(('127.0.0.1', port))
            sock.sendall((f'POST /api/v1/requests HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                          f'Content-Type: application/json\r\n'
                          f'Authorization: Bearer {started.env["WB_API_KEY"]}\r\n'
                          'Content-Length: 1000\r\n\r\n{"project_id"').encode())
            self.spin(0.3)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            sock.close()  # RST while the body is being read
            self.spin(0.5)
        self.assertNotIn('Traceback', err.getvalue())
        # REST reads the body itself and answers a reset body as 400 without a line; the listener's own catch logs one
        self.assertLessEqual(len([line for line in err.getvalue().splitlines() if 'reset' in line.lower()]), 1,
                             err.getvalue())


class ShellChannelTests(SetCase):
    def test_shell_is_bash_by_default_in_home_leading_its_own_session_with_the_hub_environment(self):  # WBX-D4: not $SHELL
        self.start()
        shell = self.view('shell')
        self.assertEqual(shell['state'], 'running')
        pid = shell['pid']
        fields = proc_stat(pid)
        self.assertEqual((int(fields[1]), int(fields[2]), int(fields[3])), (os.getpid(), pid, pid))  # ppid, pgrp, session
        bash = next((path for path in ('/bin/bash', '/usr/bin/bash') if os.access(path, os.X_OK)), None)
        self.assertEqual(os.path.realpath(f'/proc/{pid}/exe'), os.path.realpath(bash or '/bin/sh'))  # $SHELL here is /bin/sh
        self.assertEqual(os.readlink(f'/proc/{pid}/cwd'), str(self.box.home))
        env = environ(pid)
        self.assertEqual((env['HOME'], env['TERM']), (str(self.box.home), 'xterm-256color'))
        for name in ('WB_API_KEY', 'WB_HUB_URL', 'WB_BOARD_MCP_KEY', 'PI_CONFIG_DIR', 'PI_CODING_AGENT_DIR'):
            self.assertNotIn(name, env)
        self.assertIn(('board_start', 'owner', self.who('shell', pid=pid, reason='first_open')),
                      self.audits)

    def test_configured_shell_wins_over_SHELL(self):
        self.box.env['SHELL'] = '/bin/false'
        self.launcher = self.make_launcher(type(self.config)(shell='/bin/sh'))
        self.board = self.make_board()
        self.start()
        pid = self.view('shell')['pid']
        self.assertEqual(os.path.realpath(f'/proc/{pid}/exe'), os.path.realpath('/bin/sh'))

    def test_shell_runs_even_when_the_agent_cannot_start(self):
        self.box.env['WORKBENCH_OMP'] = str(self.box.root / 'missing-omp')
        self.launcher = self.make_launcher(self.config)
        self.board = self.make_board()
        self.start()
        self.assertEqual((self.view('agent')['state'], self.view('agent')['detail']), ('failed', 'omp_unavailable'))
        self.assertEqual(self.view('shell')['state'], 'running')


class OmpSelectionFailureTests(SetCase):
    """An unusable ``~/.local/bin/omp`` fails the agent with the same directory and fix ``wb`` prints (bcbba31)."""

    def group_writable_local_bin(self) -> Path:
        del self.box.env['WORKBENCH_OMP']  # no override: the default user command link decides
        local_bin = self.box.home / '.local' / 'bin'
        local_bin.mkdir(parents=True, exist_ok=True)
        for private in (self.box.home, self.box.home / '.local'):
            os.chmod(private, 0o700)  # only the command directory is unsafe
        os.chmod(local_bin, 0o775)
        self.launcher = self.make_launcher(self.config)
        self.board = self.make_board()
        return local_bin

    def test_the_agent_failure_names_the_directory_and_the_chmod_while_the_reason_code_stays(self):
        local_bin = self.group_writable_local_bin()
        self.start()
        agent = self.view('agent')
        self.assertEqual((agent['state'], agent['detail']), ('failed', 'omp_unavailable'))
        self.assertIn(str(local_bin), agent['message'])
        self.assertIn('group-writable', agent['message'])
        self.assertIn(f'chmod g-w {local_bin}', agent['message'])
        self.assertIn('unsafe_managed_ancestry', agent['message'])
        self.assertEqual(self.view('shell')['state'], 'running')

    def test_a_plain_missing_override_has_no_message_and_other_views_keep_their_keys(self):
        self.box.env['WORKBENCH_OMP'] = str(self.box.root / 'missing-omp')
        self.launcher = self.make_launcher(self.config)
        self.board = self.make_board()
        self.start()
        self.assertEqual(self.view('agent')['detail'], 'omp_unavailable')
        # WBX-D1: a terminal view is id, kind, name, the v1 slot keys, clients and busy (no message without one)
        self.assertEqual(set(self.view('shell')), {'id', 'kind', 'name', 'state', 'pid', 'started_at', 'detail',
                                                   'exit_status', 'clients', 'busy'})


class ExitTests(SetCase):
    def test_a_channel_that_ends_by_itself_shows_exited_with_its_status_and_is_reaped(self):
        self.start()
        self.assertTrue(self.wait(lambda: b'ready' in self.board.replay(self.tid('agent'))))
        agent_pid, shell_pid = self.view('agent')['pid'], self.view('shell')['pid']
        self.board.write(self.tid('agent'), b'exit 7\n')
        self.board.write(self.tid('shell'), b'exit 3\n')
        self.assertTrue(self.wait(lambda: self.view('agent')['state'] == 'exited'
                                  and self.view('shell')['state'] == 'exited'))
        agent, shell = self.view('agent'), self.view('shell')
        self.assertEqual((agent['exit_status'], agent['detail']), (7, 'exit_status:7'))
        self.assertEqual((agent['pid'], shell['pid']), (agent_pid, shell_pid))
        self.assertEqual(shell['exit_status'], 3)
        self.assertIsNone(proc_stat(agent_pid))  # reaped, not a zombie
        self.assertIsNone(proc_stat(shell_pid))
        self.assertFalse(self.board.write(self.tid('shell'), b'echo late\n'))


class EventLogTests(SetCase):
    """WBX-B12: ``board-agent/omp-logs/board-events.log`` gets one JSON line per agent start (isolation verdict) and
    per agent end (OMP exit status) so a later login problem can be lined up with Board starts and stops."""

    def rows(self) -> list[dict]:
        path = self.box.state / 'board-agent' / 'omp-logs' / 'board-events.log'
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_start_exit_and_restart_are_recorded_with_the_exit_status(self):
        self.start()
        self.assertTrue(self.wait(lambda: b'ready' in self.board.replay(self.tid('agent'))))
        self.board.write(self.tid('agent'), b'exit 7\n')
        self.assertTrue(self.wait(lambda: self.view('agent')['state'] == 'exited'))
        self.assertTrue(self.wait(lambda: any(row['event'] == 'exit' for row in self.rows())))
        self.arun(self.board.restart(self.tid('agent')))
        self.arun(self.board.settled())
        self.pids.add(self.view('agent')['pid'])
        self.arun(self.board.stop())
        events = [(row['event'], row.get('status'), row.get('isolation')) for row in self.rows()]
        self.assertEqual(events, [('start', None, 'ok'), ('exit', 7, None), ('start', None, 'ok'),
                                  ('stop', events[-1][1], None)])
        self.assertIsNotNone(events[-1][1])  # the stopped OMP's own exit status


class RestartTests(SetCase):
    def test_agent_restart_ends_the_whole_session_and_continues_the_previous_omp_session(self):
        self.start()
        self.assertTrue(self.wait(lambda: b'ready' in self.board.replay(self.tid('agent'))))
        old = self.view('agent')['pid']
        self.board.write(self.tid('agent'), b'session\n')
        self.board.write(self.tid('agent'), b'bg\n')
        self.assertTrue(self.wait(lambda: self.box.records('bg')))
        background = self.box.records('bg')[0]['bg']
        self.pids.add(background)
        self.arun(self.board.restart(self.tid('agent'), address='10.0.0.5'))
        self.arun(self.board.settled())
        agent = self.view('agent')
        self.pids.add(agent['pid'])
        self.assertEqual(agent['state'], 'running')
        self.assertNotEqual(agent['pid'], old)
        self.assertFalse(alive(old))
        self.assertFalse(alive(background))  # its own group, ignores SIGHUP/SIGTERM: SIGKILLed as a session member
        self.assertTrue(self.wait(lambda: len(self.tty_records()) == 2))
        self.assertNotIn('--continue', self.tty_records()[0]['argv'])
        self.assertIn('--continue', self.tty_records()[1]['argv'])
        self.assertIn(('board_restart', 'owner', self.who('agent', pid=old, reason='owner')), self.audits)
        self.assertEqual(self.view('shell')['state'], 'running')  # the other channel is untouched

    def begin_restart(self, name: str):
        """A restart task stopped right after it began ending the old channel (its fd still open)."""
        channel = self.board.find(self.tid(name)).channel
        task = self.loop.create_task(self.board.restart(self.tid(name)))
        for _ in range(200):
            self.loop.run_until_complete(asyncio.sleep(0))
            if channel.ending:
                break
        self.assertTrue(channel.ending)
        return channel, task

    def test_writes_are_refused_while_a_channel_is_ending(self):
        self.start()
        channel, task = self.begin_restart('shell')
        self.assertFalse(channel.write(b'echo late\n'))
        self.assertFalse(self.board.write(self.tid('shell'), b'echo late\n'))
        self.arun(task)
        self.pids.add(self.view('shell')['pid'])
        self.assertTrue(self.board.write(self.tid('shell'), b'echo fresh-$((2+3))\n'))
        self.assertTrue(self.wait(lambda: b'fresh-5' in self.board.replay(self.tid('shell'))))

    def test_a_cancelled_restart_still_ends_the_old_channel_and_starts_the_new_one(self):
        self.start()
        old = self.view('shell')['pid']
        channel, task = self.begin_restart('shell')
        task.cancel()
        try:
            self.arun(task)
        except asyncio.CancelledError:
            pass
        self.assertTrue(self.wait(lambda: self.view('shell')['pid'] != old
                                  and self.view('shell')['state'] == 'running'))
        self.pids.add(self.view('shell')['pid'])
        self.assertFalse(alive(old))

    def test_an_exited_shell_restarts_with_a_new_pid(self):
        self.start()
        old = self.view('shell')['pid']
        self.board.write(self.tid('shell'), b'exit 0\n')
        self.assertTrue(self.wait(lambda: self.view('shell')['state'] == 'exited'))
        self.arun(self.board.restart(self.tid('shell')))
        shell = self.view('shell')
        self.pids.add(shell['pid'])
        self.assertEqual(shell['state'], 'running')
        self.assertNotEqual(shell['pid'], old)
        self.assertTrue(alive(shell['pid']))


class LazyStartTests(SetCase):
    def test_nothing_runs_until_the_first_open_and_a_later_open_keeps_the_same_processes(self):
        """v1 "first connection" is WBX-D56's first open; ``status()`` is never None now (it lists agent-1)."""
        self.assertEqual([(view['name'], view['state']) for view in self.board.views()], [('agent-1', 'not_started')])
        self.spin(0.3)
        self.assertEqual(self.box.records(), [])
        self.start()
        first = {kind: self.view(kind)['pid'] for kind in ('agent', 'shell')}
        self.board.write(self.tid('shell'), b'exit 0\n')
        self.assertTrue(self.wait(lambda: self.view('shell')['state'] == 'exited'))
        self.start()  # the page opened again
        self.assertEqual(self.view('agent')['pid'], first['agent'])
        self.assertEqual((self.view('agent')['state'], self.view('shell')['state']), ('running', 'exited'))
        self.assertEqual(len(self.tty_records()), 1)
        self.assertEqual(len(self.board.shells), 1, 'no second shell-1 in the same run')
        self.assertEqual(sorted(event for event, _, _ in self.audits),
                         ['board_start', 'board_start', 'board_terminal_open'])

    def test_a_stopped_set_never_starts_again(self):
        """v1: a later open after the Hub stop started nothing; v2: it is refused (``board_unavailable``)."""
        from workbench.hub.board.set import BoardError
        self.start()
        self.arun(self.board.stop())
        with self.assertRaises(BoardError) as caught:
            self.start()
        self.assertEqual(caught.exception.reason, 'board_unavailable')
        self.assertEqual((self.view('agent')['state'], self.view('shell')['state']), ('stopped', 'exited'))


class ActiveStatusTests(SetCase):
    def test_active_lists_every_live_terminal_and_follows_their_state_changes(self):
        """v1 listed both channels and the lease; v2 lists the live terminals (no connection state: WBX-D1)."""
        self.assertEqual(self.board.active(), [])
        self.start()
        agent, shell = self.view('agent'), self.view('shell')
        self.assertEqual(self.board.active(), [{'kind': 'board_agent_set', 'terminals': [
            {'id': agent['id'], 'kind': 'agent', 'name': 'agent-1', 'pid': agent['pid'], 'state': 'running'},
            {'id': shell['id'], 'kind': 'shell', 'name': 'shell-1', 'pid': shell['pid'], 'state': 'running'}]}])
        offer = self.board.active()
        self.board.set_clients(shell['id'], 3)
        self.assertEqual(self.board.active(), offer, 'connections never change it')
        self.board.write(agent['id'], b'exit 0\n')
        self.assertTrue(self.wait(lambda: self.view('agent')['state'] == 'exited'))
        self.assertEqual([item['kind'] for item in self.board.active()[0]['terminals']], ['shell'])
        self.board.write(shell['id'], b'exit 0\n')
        self.assertTrue(self.wait(lambda: self.board.active() == []))

    def test_status_view_has_the_registry_state_the_limits_and_every_terminal(self):
        self.start()
        status = self.board.status()
        self.assertEqual(set(status), {'state', 'options', 'agents', 'shells'})
        self.assertEqual((status['state'], status['options']), ('ok', {'agent_limit': 4, 'shell_limit': 10}))
        for view in status['agents'] + status['shells']:
            self.assertLessEqual({'id', 'kind', 'name', 'state', 'pid', 'started_at', 'detail', 'clients', 'busy'},
                                 set(view))
        self.assertEqual(([view['name'] for view in status['agents']], [view['name'] for view in status['shells']]),
                         (['agent-1'], ['shell-1']))


class TerminalIoTests(SetCase):
    def test_input_reaches_the_shell_and_its_output_goes_to_subscribers_and_the_replay(self):
        self.start()
        seen = bytearray()
        unsubscribe = self.board.subscribe(self.tid('shell'), seen.extend)
        self.assertTrue(self.board.write(self.tid('shell'), b'echo board-$((40+2))\n'))
        self.assertTrue(self.wait(lambda: b'board-42' in seen))
        self.assertIn(b'board-42', self.board.replay(self.tid('shell')))
        unsubscribe()
        self.board.write(self.tid('shell'), b'echo after-$((1+1))\n')
        self.assertTrue(self.wait(lambda: b'after-2' in self.board.replay(self.tid('shell'))))
        self.assertNotIn(b'after-2', seen)

    def test_the_replay_keeps_the_newest_mebibyte(self):
        self.start()
        self.assertTrue(self.wait(lambda: b'ready' in self.board.replay(self.tid('agent'))))
        self.board.write(self.tid('agent'), b'flood 1500000\n')
        # The PTY's ONLCR may turn the fake's "\r\n" into "\r\r\n"; only the run of x's matters.
        self.assertTrue(self.wait(lambda: self.board.output_total(self.tid('agent')) > 1_500_000
                                  and self.board.replay(self.tid('agent')).rstrip(b'\r\n').endswith(b'x' * 1000)),
                        repr(self.board.replay(self.tid('agent'))[-60:]))
        replay = self.board.replay(self.tid('agent'))
        self.assertEqual(len(replay), 1 << 20)
        self.assertGreater(self.board.output_total(self.tid('agent')), len(replay))

    def test_out_of_range_sizes_are_refused_and_never_break_a_restart(self):
        self.start()
        # WBX-D1: the bound is 500 (SPEC s4 "WebSocket v2"; v1 took 1..1000)
        for rows, cols in ((0, 80), (24, 0), (501, 80), (24, 501), (-1, 80), (24, 70000), (True, 80), (24.0, 80)):
            with self.assertRaises(ValueError, msg=(rows, cols)):
                self.board.resize(self.tid('shell'), rows, cols)
        self.board.resize(self.tid('shell'), 500, 500)
        self.arun(self.board.restart(self.tid('shell')))
        shell = self.view('shell')
        self.pids.add(shell['pid'])
        self.assertEqual(shell['state'], 'running')
        self.board.write(self.tid('shell'), b'stty size\n')
        self.assertTrue(self.wait(lambda: b'500 500' in self.board.replay(self.tid('shell'))))

    def test_resize_changes_the_pty_window_size(self):
        self.start()
        self.board.resize(self.tid('shell'), 40, 132)
        self.board.write(self.tid('shell'), b'stty size\n')
        self.assertTrue(self.wait(lambda: b'40 132' in self.board.replay(self.tid('shell'))))


if __name__ == '__main__':
    unittest.main()
