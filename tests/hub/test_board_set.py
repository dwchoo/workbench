"""TI-16: the Hub-owned Board agent set (``BoardSet``): PTY channels, isolation failure, ring buffer, exit,
restart, lazy start (seam T6b: fake ``omp`` + real BoardSet, real PTYs and processes).

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
        self.arun(self.board.ensure_started())
        self.arun(self.board.settled())
        for name in ('agent', 'shell'):
            pid = (self.board.status() or {}).get(name, {}).get('pid')
            if pid:
                self.pids.add(pid)

    def tty_records(self) -> list[dict]:
        return self.box.records('tty')


class AgentChannelTests(SetCase):
    def test_agent_runs_as_a_hub_child_leading_its_own_session_on_a_pty(self):
        self.start()
        self.assertTrue(self.wait(lambda: len(self.tty_records()) == 1))
        record, = self.tty_records()
        agent = self.board.status()['agent']
        self.assertEqual((agent['state'], agent['pid']), ('running', record['pid']))
        self.assertEqual(record['ppid'], os.getpid())
        self.assertEqual(record['sid'], record['pid'])
        self.assertTrue(record['isatty'])
        self.assertIn(('board_start', 'owner', {'channel': 'agent', 'pid': record['pid'], 'reason': 'first_open'}),
                      self.audits)

    def test_an_isolation_leak_ends_the_agent_and_shows_isolation_failed(self):
        self.box.env['FAKE_BOARD_TOOLS'] = json.dumps(['mcp__workbench_hub_inbox_list', 'mcp__decoy_user_x'])
        self.launcher = self.make_launcher(self.config)
        self.board = self.make_board()
        self.start()
        agent = self.board.status()['agent']
        self.assertEqual(agent['state'], 'isolation_failed')
        self.assertIn('mcp__decoy_user_x', agent['detail'])
        started, = [detail['pid'] for event, _, detail in self.audits
                    if event == 'board_start' and detail['channel'] == 'agent']
        self.assertEqual(agent['pid'], started)
        self.assertFalse(alive(started))
        self.assertIn(('board_stop', 'hub', {'channel': 'agent', 'pid': started, 'reason': 'isolation_failed'}),
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
            self.arun(self.board.restart('shell'))
        shell = self.board.status()['shell']
        self.assertEqual((shell['state'], shell['detail']), ('failed', 'spawn_failed:Too many open files'))
        self.assertTrue(self.wait(lambda: child_pids() <= before, 5), child_pids() - before)

    def test_a_shell_that_cannot_start_notifies_the_watchers(self):
        self.box.env['SHELL'] = str(self.box.root / 'no-such-shell')
        self.launcher = self.make_launcher(self.config)
        self.board = self.make_board()
        changes = []
        self.board.watch(lambda: changes.append(self.board.status()['shell']['state']))
        self.arun(self.board.restart('shell'))
        self.assertEqual(self.board.status()['shell']['detail'], 'shell_unavailable')
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
    def test_shell_is_the_users_shell_in_home_leading_its_own_session_with_the_hub_environment(self):
        self.start()
        shell = self.board.status()['shell']
        self.assertEqual(shell['state'], 'running')
        pid = shell['pid']
        fields = proc_stat(pid)
        self.assertEqual((int(fields[1]), int(fields[2]), int(fields[3])), (os.getpid(), pid, pid))  # ppid, pgrp, session
        self.assertEqual(os.path.realpath(f'/proc/{pid}/exe'), os.path.realpath('/bin/sh'))
        self.assertEqual(os.readlink(f'/proc/{pid}/cwd'), str(self.box.home))
        env = environ(pid)
        self.assertEqual((env['HOME'], env['TERM']), (str(self.box.home), 'xterm-256color'))
        for name in ('WB_API_KEY', 'WB_HUB_URL', 'WB_BOARD_MCP_KEY', 'PI_CONFIG_DIR', 'PI_CODING_AGENT_DIR'):
            self.assertNotIn(name, env)
        self.assertIn(('board_start', 'owner', {'channel': 'shell', 'pid': pid, 'reason': 'first_open'}),
                      self.audits)

    def test_configured_shell_wins_over_SHELL(self):
        self.box.env['SHELL'] = '/bin/false'
        self.launcher = self.make_launcher(type(self.config)(shell='/bin/sh'))
        self.board = self.make_board()
        self.start()
        pid = self.board.status()['shell']['pid']
        self.assertEqual(os.path.realpath(f'/proc/{pid}/exe'), os.path.realpath('/bin/sh'))

    def test_shell_runs_even_when_the_agent_cannot_start(self):
        self.box.env['WORKBENCH_OMP'] = str(self.box.root / 'missing-omp')
        self.launcher = self.make_launcher(self.config)
        self.board = self.make_board()
        self.start()
        status = self.board.status()
        self.assertEqual((status['agent']['state'], status['agent']['detail']), ('failed', 'omp_unavailable'))
        self.assertEqual(status['shell']['state'], 'running')


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
        agent = self.board.status()['agent']
        self.assertEqual((agent['state'], agent['detail']), ('failed', 'omp_unavailable'))
        self.assertIn(str(local_bin), agent['message'])
        self.assertIn('group-writable', agent['message'])
        self.assertIn(f'chmod g-w {local_bin}', agent['message'])
        self.assertIn('unsafe_managed_ancestry', agent['message'])
        self.assertEqual(self.board.status()['shell']['state'], 'running')

    def test_a_plain_missing_override_has_no_message_and_other_views_keep_their_keys(self):
        self.box.env['WORKBENCH_OMP'] = str(self.box.root / 'missing-omp')
        self.launcher = self.make_launcher(self.config)
        self.board = self.make_board()
        self.start()
        status = self.board.status()
        self.assertEqual(status['agent']['detail'], 'omp_unavailable')
        self.assertEqual(set(status['shell']), {'state', 'pid', 'started_at', 'detail', 'exit_status'})


class ExitTests(SetCase):
    def test_a_channel_that_ends_by_itself_shows_exited_with_its_status_and_is_reaped(self):
        self.start()
        self.assertTrue(self.wait(lambda: b'ready' in self.board.replay('agent')))
        agent_pid, shell_pid = self.board.status()['agent']['pid'], self.board.status()['shell']['pid']
        self.board.write('agent', b'exit 7\n')
        self.board.write('shell', b'exit 3\n')
        self.assertTrue(self.wait(lambda: self.board.status()['agent']['state'] == 'exited'
                                  and self.board.status()['shell']['state'] == 'exited'))
        status = self.board.status()
        self.assertEqual((status['agent']['exit_status'], status['agent']['detail']), (7, 'exit_status:7'))
        self.assertEqual((status['agent']['pid'], status['shell']['pid']), (agent_pid, shell_pid))
        self.assertEqual(status['shell']['exit_status'], 3)
        self.assertIsNone(proc_stat(agent_pid))  # reaped, not a zombie
        self.assertIsNone(proc_stat(shell_pid))
        self.assertFalse(self.board.write('shell', b'echo late\n'))


class EventLogTests(SetCase):
    """WBX-B12: ``board-agent/omp-logs/board-events.log`` gets one JSON line per agent start (isolation verdict) and
    per agent end (OMP exit status) so a later login problem can be lined up with Board starts and stops."""

    def rows(self) -> list[dict]:
        path = self.box.state / 'board-agent' / 'omp-logs' / 'board-events.log'
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_start_exit_and_restart_are_recorded_with_the_exit_status(self):
        self.start()
        self.assertTrue(self.wait(lambda: b'ready' in self.board.replay('agent')))
        self.board.write('agent', b'exit 7\n')
        self.assertTrue(self.wait(lambda: self.board.status()['agent']['state'] == 'exited'))
        self.assertTrue(self.wait(lambda: any(row['event'] == 'exit' for row in self.rows())))
        self.arun(self.board.restart('agent'))
        self.arun(self.board.settled())
        self.pids.add(self.board.status()['agent']['pid'])
        self.arun(self.board.stop())
        events = [(row['event'], row.get('status'), row.get('isolation')) for row in self.rows()]
        self.assertEqual(events, [('start', None, 'ok'), ('exit', 7, None), ('start', None, 'ok'),
                                  ('stop', events[-1][1], None)])
        self.assertIsNotNone(events[-1][1])  # the stopped OMP's own exit status


class RestartTests(SetCase):
    def test_agent_restart_ends_the_whole_session_and_continues_the_previous_omp_session(self):
        self.start()
        self.assertTrue(self.wait(lambda: b'ready' in self.board.replay('agent')))
        old = self.board.status()['agent']['pid']
        self.board.write('agent', b'session\n')
        self.board.write('agent', b'bg\n')
        self.assertTrue(self.wait(lambda: self.box.records('bg')))
        background = self.box.records('bg')[0]['bg']
        self.pids.add(background)
        self.arun(self.board.restart('agent', address='10.0.0.5'))
        self.arun(self.board.settled())
        agent = self.board.status()['agent']
        self.pids.add(agent['pid'])
        self.assertEqual(agent['state'], 'running')
        self.assertNotEqual(agent['pid'], old)
        self.assertFalse(alive(old))
        self.assertFalse(alive(background))  # its own group, ignores SIGHUP/SIGTERM: SIGKILLed as a session member
        self.assertTrue(self.wait(lambda: len(self.tty_records()) == 2))
        self.assertNotIn('--continue', self.tty_records()[0]['argv'])
        self.assertIn('--continue', self.tty_records()[1]['argv'])
        self.assertIn(('board_restart', 'owner', {'channel': 'agent', 'pid': old, 'reason': 'owner'}), self.audits)
        self.assertEqual(self.board.status()['shell']['state'], 'running')  # the other channel is untouched

    def begin_restart(self, name: str):
        """A restart task stopped right after it began ending the old channel (its fd still open)."""
        channel = self.board.slots[name].channel
        task = self.loop.create_task(self.board.restart(name))
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
        self.assertFalse(self.board.write('shell', b'echo late\n'))
        self.arun(task)
        self.pids.add(self.board.status()['shell']['pid'])
        self.assertTrue(self.board.write('shell', b'echo fresh-$((2+3))\n'))
        self.assertTrue(self.wait(lambda: b'fresh-5' in self.board.replay('shell')))

    def test_a_cancelled_restart_still_ends_the_old_channel_and_starts_the_new_one(self):
        self.start()
        old = self.board.status()['shell']['pid']
        channel, task = self.begin_restart('shell')
        task.cancel()
        try:
            self.arun(task)
        except asyncio.CancelledError:
            pass
        self.assertTrue(self.wait(lambda: self.board.status()['shell']['pid'] != old
                                  and self.board.status()['shell']['state'] == 'running'))
        self.pids.add(self.board.status()['shell']['pid'])
        self.assertFalse(alive(old))

    def test_an_exited_shell_restarts_with_a_new_pid(self):
        self.start()
        old = self.board.status()['shell']['pid']
        self.board.write('shell', b'exit 0\n')
        self.assertTrue(self.wait(lambda: self.board.status()['shell']['state'] == 'exited'))
        self.arun(self.board.restart('shell'))
        shell = self.board.status()['shell']
        self.pids.add(shell['pid'])
        self.assertEqual(shell['state'], 'running')
        self.assertNotEqual(shell['pid'], old)
        self.assertTrue(alive(shell['pid']))


class LazyStartTests(SetCase):
    def test_nothing_runs_until_the_first_open_and_a_later_open_keeps_the_same_processes(self):
        self.assertIsNone(self.board.status())
        self.spin(0.3)
        self.assertEqual(self.box.records(), [])
        self.start()
        first = {name: self.board.status()[name]['pid'] for name in ('agent', 'shell')}
        self.board.write('shell', b'exit 0\n')
        self.assertTrue(self.wait(lambda: self.board.status()['shell']['state'] == 'exited'))
        self.start()  # another Board connection
        status = self.board.status()
        self.assertEqual(status['agent']['pid'], first['agent'])
        self.assertEqual((status['agent']['state'], status['shell']['state']), ('running', 'exited'))
        self.assertEqual(len(self.tty_records()), 1)
        self.assertEqual([event for event, _, _ in self.audits], ['board_start', 'board_start'])

    def test_a_stopped_set_never_starts_again(self):
        self.start()
        self.arun(self.board.stop())
        self.start()
        self.assertEqual({self.board.status()[name]['state'] for name in ('agent', 'shell')}, {'stopped'})


class ActiveStatusTests(SetCase):
    def test_active_lists_the_set_while_a_channel_lives_and_follows_channel_and_lease_changes(self):
        self.assertEqual(self.board.active(), [])
        self.start()
        agent, shell = self.board.status()['agent']['pid'], self.board.status()['shell']['pid']
        self.assertEqual(self.board.active(), [{'kind': 'board_agent_set', 'lease': 'none', 'channels': {
            'agent': {'pid': agent, 'state': 'running'}, 'shell': {'pid': shell, 'state': 'running'}}}])
        self.board.set_lease('connected', 3)
        self.assertEqual(self.board.active()[0]['lease'], 'connected')
        lease = self.board.status()['lease']
        self.assertEqual((lease['state'], lease['epoch']), ('connected', 3))
        self.assertIsInstance(lease['since'], float)
        self.board.write('agent', b'exit 0\n')
        self.assertTrue(self.wait(lambda: self.board.status()['agent']['state'] == 'exited'))
        self.assertEqual(self.board.active()[0]['channels']['agent'], {'pid': agent, 'state': 'exited'})
        self.board.write('shell', b'exit 0\n')
        self.assertTrue(self.wait(lambda: self.board.active() == []))

    def test_status_view_has_both_channels_and_the_lease(self):
        self.start()
        status = self.board.status()
        self.assertEqual(set(status), {'agent', 'shell', 'lease'})
        for name in ('agent', 'shell'):
            self.assertLessEqual({'state', 'pid', 'started_at', 'detail'}, set(status[name]))
        self.assertEqual(status['lease'], {'state': 'none', 'epoch': 0, 'since': None})


class TerminalIoTests(SetCase):
    def test_input_reaches_the_shell_and_its_output_goes_to_subscribers_and_the_replay(self):
        self.start()
        seen = bytearray()
        unsubscribe = self.board.subscribe('shell', seen.extend)
        self.assertTrue(self.board.write('shell', b'echo board-$((40+2))\n'))
        self.assertTrue(self.wait(lambda: b'board-42' in seen))
        self.assertIn(b'board-42', self.board.replay('shell'))
        unsubscribe()
        self.board.write('shell', b'echo after-$((1+1))\n')
        self.assertTrue(self.wait(lambda: b'after-2' in self.board.replay('shell')))
        self.assertNotIn(b'after-2', seen)

    def test_the_replay_keeps_the_newest_mebibyte(self):
        self.start()
        self.assertTrue(self.wait(lambda: b'ready' in self.board.replay('agent')))
        self.board.write('agent', b'flood 1500000\n')
        # The PTY's ONLCR may turn the fake's "\r\n" into "\r\r\n"; only the run of x's matters.
        self.assertTrue(self.wait(lambda: self.board.output_total('agent') > 1_500_000
                                  and self.board.replay('agent').rstrip(b'\r\n').endswith(b'x' * 1000)),
                        repr(self.board.replay('agent')[-60:]))
        replay = self.board.replay('agent')
        self.assertEqual(len(replay), 1 << 20)
        self.assertGreater(self.board.output_total('agent'), len(replay))

    def test_out_of_range_sizes_are_refused_and_never_break_a_restart(self):
        self.start()
        for rows, cols in ((0, 80), (24, 0), (1001, 80), (24, 1001), (-1, 80), (24, 70000), (True, 80), (24.0, 80)):
            with self.assertRaises(ValueError, msg=(rows, cols)):
                self.board.resize('shell', rows, cols)
        self.board.resize('shell', 1000, 1000)
        self.arun(self.board.restart('shell'))
        shell = self.board.status()['shell']
        self.pids.add(shell['pid'])
        self.assertEqual(shell['state'], 'running')
        self.board.write('shell', b'stty size\n')
        self.assertTrue(self.wait(lambda: b'1000 1000' in self.board.replay('shell')))

    def test_resize_changes_the_pty_window_size(self):
        self.start()
        self.board.resize('shell', 40, 132)
        self.board.write('shell', b'stty size\n')
        self.assertTrue(self.wait(lambda: b'40 132' in self.board.replay('shell')))


if __name__ == '__main__':
    unittest.main()
