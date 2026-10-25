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
        for name in ('WB_BOARD_MCP_KEY', 'PI_CONFIG_DIR', 'PI_CODING_AGENT_DIR'):
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

    def test_resize_changes_the_pty_window_size(self):
        self.start()
        self.board.resize('shell', 40, 132)
        self.board.write('shell', b'stty size\n')
        self.assertTrue(self.wait(lambda: b'40 132' in self.board.replay('shell')))


if __name__ == '__main__':
    unittest.main()
