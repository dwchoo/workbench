"""WBX-D1 behaviours 4-12 and 14: several Board agents and shells in one ``BoardSet`` (web-board-ux SPEC s4 "트랙 D").

Seam: the existing Board seam -- a real :class:`BoardSet` over the real ``BoardLauncher`` with the fake ``omp``
(``fixtures/fake_omp_board.py``), a temp ``<hub state>`` with a placeholder user ``agent.db`` (never opened), a real
``hub.sqlite3`` key store and the real internal REST listener (seam T6b of ``test_board_set.py``).

Expected values: SPEC s4 (registry, limits, names, per-agent home/key, shell rules, error codes, audit details),
read back from the fake's records (argv, environment), ``/proc``, ``sqlite3`` (``api_keys``, inbox ``requests``),
the registry file bytes and the file system (inodes and contents of symlink targets).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
import unittest

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support.rest_hub import make_project  # noqa: E402
from test_board_launch import LauncherCase  # noqa: E402

from workbench.hub import auth  # noqa: E402
from workbench.hub.board.set import BoardError, BoardSet  # noqa: E402
from workbench.hub.config import BoardAgentConfig  # noqa: E402
from workbench.hub.inbox.service import ProjectRef  # noqa: E402


def alive(pid: int) -> bool:
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
    except (OSError, IndexError):
        return False
    return fields[0] not in ('Z', 'X')


class WorkspaceCase(LauncherCase):
    grace = 1.0

    def setUp(self):
        super().setUp()
        self.audits: list[tuple[str, str, dict]] = []
        self.pids: set[int] = set()
        self.board = self.make_board()
        self.addCleanup(self.final_stop)

    def make_board(self) -> BoardSet:
        def audit(event, actor, address, detail):
            auth.AuditLog._payload(event, actor, address, detail)  # the Hub's audit table takes exactly this row
            self.audits.append((event, actor, detail))
        board = BoardSet(self.launcher, grace=self.grace, audit=audit)
        self.api.board_agents = board.agent_for_key
        return board

    def final_stop(self):
        self.arun(self.board.stop())
        for pid in self.pids:
            self.assertFalse(alive(pid), f'pid {pid} left running')

    def wait(self, predicate, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            self.arun(asyncio.sleep(0.05))
        return bool(predicate())

    def settle(self):
        self.arun(self.board.settled())
        for view in self.board.views():
            if view['pid']:
                self.pids.add(view['pid'])

    def by_name(self, name: str) -> dict:
        found = [view for view in self.board.views() if view['name'] == name]
        self.assertEqual(len(found), 1, self.board.views())
        return found[0]

    def agents(self) -> list[dict]:
        return [view for view in self.board.views() if view['kind'] == 'agent']

    def shells(self) -> list[dict]:
        return [view for view in self.board.views() if view['kind'] == 'shell']

    def registry_file(self) -> dict:
        return json.loads((self.box.state / 'board-agents' / 'registry.json').read_text(encoding='utf-8'))

    def tty_records(self) -> list[dict]:
        return self.box.records('tty')

    def record_of(self, pid: int) -> dict:
        self.assertTrue(self.wait(lambda: any(row['pid'] == pid for row in self.tty_records())))
        return [row for row in self.tty_records() if row['pid'] == pid][-1]

    def system_rows(self) -> list[tuple[str, str]]:
        db = sqlite3.connect(f'file:{self.box.state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            return sorted(db.execute('SELECT name, key_id FROM api_keys WHERE system = 1').fetchall())
        finally:
            db.close()

    def refused(self, coroutine) -> str:
        with self.assertRaises(BoardError) as caught:
            self.arun(coroutine)
        return caught.exception.reason

    def open_agent_1(self) -> dict:
        agent = self.agents()[0]
        self.arun(self.board.open([agent['id']]))
        self.settle()
        return self.by_name('agent-1')


class AgentCreateTests(WorkspaceCase):  # behaviour 4
    def test_agents_get_default_names_up_to_the_limit_then_limit_reached_and_nothing_starts(self):
        self.assertEqual([(view['name'], view['state']) for view in self.agents()], [('agent-1', 'not_started')])
        made = [self.arun(self.board.create_agent()) for _ in range(3)]
        self.settle()
        self.assertEqual([view['name'] for view in made], ['agent-2', 'agent-3', 'agent-4'])
        self.assertEqual([view['name'] for view in self.agents()], ['agent-1', 'agent-2', 'agent-3', 'agent-4'])
        self.assertEqual({view['name']: view['state'] for view in self.agents()},
                         {'agent-1': 'not_started', 'agent-2': 'running', 'agent-3': 'running', 'agent-4': 'running'})
        for view in made:
            self.assertRegex(view['id'], r'^a-[0-9a-f]{12}$')
        records = len(self.tty_records())
        self.assertEqual(self.refused(self.board.create_agent()), 'limit_reached')
        self.assertEqual(len(self.agents()), 4)
        self.assertEqual(len(self.registry_file()['agents']), 4)
        self.arun(asyncio.sleep(0.3))
        self.assertEqual(len(self.tty_records()), records)
        opened = [detail for event, _, detail in self.audits if event == 'board_terminal_open']
        self.assertEqual([(d['kind'], d['name'], d['reason']) for d in opened],
                         [('agent', 'agent-2', 'create'), ('agent', 'agent-3', 'create'), ('agent', 'agent-4', 'create')])

    def test_a_failed_registry_write_refuses_the_create_and_starts_no_process(self):
        directory = self.box.state / 'board-agents'
        before = (directory / 'registry.json').read_bytes()
        os.chmod(directory, 0o500)
        self.addCleanup(os.chmod, directory, 0o700)
        self.assertEqual(self.refused(self.board.create_agent()), 'registry_write_failed')
        os.chmod(directory, 0o700)
        self.assertEqual((directory / 'registry.json').read_bytes(), before)
        self.assertEqual([view['name'] for view in self.agents()], ['agent-1'])
        self.arun(asyncio.sleep(0.3))
        self.assertEqual(self.tty_records(), [])

    def test_an_invalid_registry_starts_nothing_and_the_file_stays(self):
        self.arun(self.board.stop())
        path = self.box.state / 'board-agents' / 'registry.json'
        path.write_text('{"broken": ')
        board = self.board = self.make_board()
        self.assertEqual(board.board_view()['state'], 'registry_invalid')
        self.assertIn(str(path), board.board_view()['message'])
        self.assertEqual(board.views(), [])
        for attempt in (board.open([]), board.create_agent(), board.create_shell()):
            self.assertEqual(self.refused(attempt), 'registry_invalid')
        self.assertEqual(path.read_text(), '{"broken": ')
        self.assertEqual(self.tty_records(), [])


class AgentHomeTests(WorkspaceCase):  # behaviour 5
    def test_each_agent_has_its_own_home_linked_to_the_one_user_store_and_its_own_logs(self):
        first = self.open_agent_1()
        second = self.arun(self.board.create_agent())
        self.settle()
        homes = {'agent-1': self.box.state / 'board-agent',
                 'agent-2': self.box.state / 'board-agents' / second['id']}
        for name, home in homes.items():
            view = self.by_name(name)
            record = self.record_of(view['pid'])
            self.assertEqual(record['env']['PI_CODING_AGENT_DIR'], str(home / 'omp-root' / 'agent'), name)
            link = home / 'omp-root' / 'agent' / 'agent.db'
            self.assertTrue(link.is_symlink(), name)
            self.assertEqual(os.readlink(link), str(self.box.user_store), name)
            self.assertEqual(stat.S_IMODE(os.lstat(home).st_mode), 0o700, name)
            self.assertTrue((home / 'skills' / 'wb-inbox' / 'SKILL.md').is_file(), name)
            events = home / 'omp-logs' / 'board-events.log'
            self.assertTrue(self.wait(lambda: events.exists()), name)
            self.assertNotIn('--continue', record['argv'])
        self.assertEqual(first['name'], 'agent-1')
        self.assertEqual(self.box.user_store.read_bytes(), b'')

    def test_continue_only_when_that_agents_home_has_a_session(self):
        first = self.open_agent_1()
        second = self.arun(self.board.create_agent())
        self.settle()
        self.assertTrue(self.board.write(first['id'], b'session\n'))
        self.assertTrue(self.wait(lambda: any((self.box.state / 'board-agent' / 'omp-root' / 'agent' / 'sessions')
                                              .rglob('*.jsonl'))))
        for view in (first, second):
            self.arun(self.board.restart(view['id']))
        self.settle()
        again = {view['name']: self.record_of(view['pid']) for view in self.agents()}
        self.assertIn('--continue', again['agent-1']['argv'])
        self.assertNotIn('--continue', again['agent-2']['argv'])


class AgentKeyTests(WorkspaceCase):  # behaviour 6
    def test_each_running_agent_has_its_own_system_key_row_and_env_and_the_shell_has_none(self):
        self.db.write(lambda execute: execute(  # an older Hub's single system key row
            "INSERT INTO api_keys (key_id, name, grade, scope_json, secret_hash, created_at, system) "
            "VALUES ('zzzzzzzzzz', 'board-agent', 'editor', '{\"all\": true}', '00', 0, 1)"))
        first = self.open_agent_1()
        second = self.arun(self.board.create_agent())
        shell = self.arun(self.board.create_shell())
        self.settle()
        names = {view['id']: f"board-agent.{view['id']}" for view in (first, second)}
        rows = self.system_rows()
        self.assertEqual(sorted(name for name, _ in rows), sorted(names.values()))
        self.assertNotIn('zzzzzzzzzz', [key_id for _, key_id in rows])
        for view in (first, self.by_name('agent-2')):
            key = self.record_of(view['pid'])['env']['WB_API_KEY']
            principal = self.keys.verify(key, time.time())
            self.assertEqual((principal.name, principal.system), (names[view['id']], True))
        shell_env = Path(f"/proc/{shell['pid']}/environ").read_bytes()
        self.assertNotIn(b'WB_API_KEY=', shell_env)
        self.arun(self.board.stop_terminal(second['id']))
        self.assertEqual([name for name, _ in self.system_rows()], [names[first['id']]])

    def test_people_cannot_issue_board_agent_key_names(self):
        for name in ('board-agent', 'board-agent.a-0123456789ab', 'board-agent.x'):
            with self.subTest(name), self.assertRaises(auth.ApiKeyRejected) as caught:
                self.keys.issue(name, 'editor', {'all': True}, None, now=time.time())
            self.assertEqual(caught.exception.reason, 'name_reserved')
        self.keys.issue('board-agents-notes', 'editor', {'all': True}, None, now=time.time())  # only the prefix

    def test_an_agent_that_ends_by_itself_loses_its_key_row(self):
        first = self.open_agent_1()
        self.assertEqual(len(self.system_rows()), 1)
        self.board.write(first['id'], b'exit 3\n')
        self.assertTrue(self.wait(lambda: self.by_name('agent-1')['state'] == 'exited'))
        self.assertTrue(self.wait(lambda: self.system_rows() == []))


class InternalListenerTests(WorkspaceCase):  # behaviour 7
    def status(self, key: str) -> int:
        async def get() -> int:
            async with aiohttp.ClientSession() as http:
                async with http.get(self.listener.url + '/api/v1/projects',
                                    headers={'Authorization': f'Bearer {key}'}) as response:
                    return response.status
        return self.arun(get())

    def key_of(self, view: dict) -> str:
        return self.record_of(view['pid'])['env']['WB_API_KEY']

    def test_only_the_current_key_of_a_running_agent_passes(self):
        first = self.open_agent_1()
        second = self.arun(self.board.create_agent())
        self.settle()
        second = self.by_name('agent-2')
        one, two = self.key_of(first), self.key_of(second)
        self.assertEqual((self.status(one), self.status(two)), (200, 200))
        human = self.keys.issue('people', 'editor', {'all': True}, None, now=time.time()).raw
        self.assertEqual(self.status(human), 401)
        self.arun(self.board.restart(first['id']))
        self.settle()
        self.assertEqual(self.status(one), 401, 'the previous start of agent-1')
        self.assertEqual(self.status(self.key_of(self.by_name('agent-1'))), 200)
        self.arun(self.board.stop_terminal(second['id']))
        self.assertEqual(self.status(two), 401)
        # A system row of a stopped agent (left by a crash) still names no running agent: 401.
        leftover = self.keys.rotate_system_key(second['id'], now=time.time()).raw
        self.assertEqual(self.status(leftover), 401)


class OriginTests(WorkspaceCase):  # behaviour 8
    def test_a_request_records_the_agent_name_at_submit_time(self):
        root, project = make_project(self.box.root, 'alpha')
        self.inbox.set_projects([ProjectRef(project, root, 'alpha')])
        first = self.open_agent_1()
        key = self.record_of(first['pid'])['env']['WB_API_KEY']
        self.arun(self.board.rename(first['id'], '전처리'))

        async def submit(title: str) -> dict:
            async with aiohttp.ClientSession() as http:
                async with http.post(self.listener.url + '/api/v1/requests', headers={'Authorization': f'Bearer {key}'},
                                     json={'project_id': project, 'title': title, 'body': 'b',
                                           'idempotency_key': f'k-{title}'}) as response:
                    self.assertEqual(response.status, 201, await response.text())
                    return await response.json()
        made = self.arun(submit('one'))
        self.assertEqual(made['origin']['label'], 'Board agent · 전처리')
        self.arun(self.board.rename(first['id'], '나중 이름'))
        later = self.arun(submit('two'))
        self.assertEqual(later['origin']['label'], 'Board agent · 나중 이름')
        db = sqlite3.connect(f'file:{root / ".wb" / "inbox.sqlite3"}?mode=ro', uri=True)
        try:
            rows = db.execute('SELECT title, origin_kind, origin_name FROM requests ORDER BY seq').fetchall()
            authors = db.execute('SELECT author_kind, author_name FROM request_revisions ORDER BY rowid').fetchall()
        finally:
            db.close()
        self.assertEqual(rows, [('one', 'board_agent', '전처리'), ('two', 'board_agent', '나중 이름')])
        self.assertEqual(authors, [('board_agent', '전처리'), ('board_agent', '나중 이름')])


class StopTests(WorkspaceCase):  # behaviour 9
    def test_stop_keeps_the_memory_open_does_not_restart_it_and_start_continues(self):
        first = self.open_agent_1()
        self.board.write(first['id'], b'session\n')
        sessions = self.box.state / 'board-agent' / 'omp-root' / 'agent' / 'sessions'
        self.assertTrue(self.wait(lambda: any(sessions.rglob('*.jsonl'))))
        stopped = self.arun(self.board.stop_terminal(first['id']))
        self.assertEqual(stopped['state'], 'stopped')
        self.assertFalse(alive(first['pid']))
        self.assertTrue(any(sessions.rglob('*.jsonl')))
        self.arun(self.board.open([first['id']]))
        self.arun(asyncio.sleep(0.3))
        self.assertEqual(self.by_name('agent-1')['state'], 'stopped')
        self.assertEqual(len(self.tty_records()), 1)
        again = self.arun(self.board.start(first['id']))
        self.settle()
        self.assertIn('--continue', self.record_of(self.by_name('agent-1')['pid'])['argv'])
        self.assertIn(again['state'], ('starting', 'running'))
        stops = [detail for event, _, detail in self.audits if event == 'board_stop']
        self.assertEqual(stops[0], {'terminal_id': first['id'], 'kind': 'agent', 'name': 'agent-1',
                                    'pid': first['pid'], 'reason': 'owner'})

    def test_wrong_kind_and_bad_state_and_unknown_terminal(self):
        first = self.agents()[0]
        shell = self.arun(self.board.create_shell())
        self.settle()
        self.assertEqual(self.refused(self.board.stop_terminal(shell['id'])), 'wrong_kind')
        self.assertEqual(self.refused(self.board.delete(shell['id'])), 'wrong_kind')
        self.assertEqual(self.refused(self.board.close(first['id'])), 'wrong_kind')
        self.assertEqual(self.refused(self.board.stop_terminal(first['id'])), 'bad_state')  # never started
        self.assertEqual(self.refused(self.board.start(shell['id'])), 'bad_state')  # running
        self.assertEqual(self.refused(self.board.start('a-ffffffffffff')), 'unknown_terminal')


class DeleteTests(WorkspaceCase):  # behaviour 10
    def test_delete_removes_that_home_and_its_registry_entry_and_never_the_user_store(self):
        first = self.open_agent_1()
        second = self.arun(self.board.create_agent())
        self.settle()
        user = os.stat(self.box.user_store)
        other = self.box.state / 'board-agent' / 'omp-root'
        other_before = sorted(str(path.relative_to(other)) for path in other.rglob('*'))
        home = self.box.state / 'board-agents' / second['id']
        self.assertTrue(home.is_dir())
        self.assertEqual(self.arun(self.board.delete(second['id'])), None)
        self.assertFalse(os.path.lexists(home))
        self.assertEqual([agent['id'] for agent in self.registry_file()['agents']], [first['id']])
        self.assertEqual([view['name'] for view in self.agents()], ['agent-1'])
        after = os.stat(self.box.user_store)
        self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns), (user.st_ino, user.st_size, user.st_mtime_ns))
        self.assertEqual(sorted(str(path.relative_to(other)) for path in other.rglob('*')), other_before)
        closed = [detail for event, _, detail in self.audits if event == 'board_terminal_close']
        self.assertEqual(closed, [{'terminal_id': second['id'], 'kind': 'agent', 'name': 'agent-2',
                                   'memory_removed': True}])
        # The migrated agent-1 goes too; a later agent then lives under board-agents/.
        self.arun(self.board.delete(first['id']))
        self.assertFalse(os.path.lexists(self.box.state / 'board-agent'))
        third = self.arun(self.board.create_agent())
        self.settle()
        self.assertEqual(third['name'], 'agent-1')
        self.assertTrue((self.box.state / 'board-agents' / third['id'] / 'omp-root').is_dir())

    def test_a_home_that_is_a_symlink_is_not_followed_and_the_delete_fails(self):
        second = self.arun(self.board.create_agent())
        self.settle()
        self.arun(self.board.stop_terminal(second['id']))
        home = self.box.state / 'board-agents' / second['id']
        outside = self.box.root / 'outside'
        outside.mkdir()
        (outside / 'precious.txt').write_text('keep')
        os.rename(home, self.box.root / 'moved-home')
        home.symlink_to(outside)
        self.assertEqual(self.refused(self.board.delete(second['id'])), 'delete_failed')
        self.assertEqual((outside / 'precious.txt').read_text(), 'keep')
        self.assertTrue(home.is_symlink())
        view = self.by_name('agent-2')
        self.assertEqual((view['state'], view['detail']), ('failed', 'delete_failed'))
        self.assertIn(str(home), view['message'])
        self.assertIn(second['id'], [agent['id'] for agent in self.registry_file()['agents']])

    def test_a_partial_delete_keeps_the_entry_and_a_second_delete_finishes(self):
        second = self.arun(self.board.create_agent())
        self.settle()
        home = self.box.state / 'board-agents' / second['id']
        locked = home / 'omp-root' / 'locked'
        locked.mkdir()
        (locked / 'inside').write_text('x')
        os.chmod(locked, 0o500)
        self.addCleanup(lambda: os.path.isdir(locked) and os.chmod(locked, 0o700))
        self.assertEqual(self.refused(self.board.delete(second['id'])), 'delete_failed')
        self.assertFalse(alive(second['pid']))
        self.assertEqual(self.by_name('agent-2')['detail'], 'delete_failed')
        self.assertIn(second['id'], [agent['id'] for agent in self.registry_file()['agents']])
        os.chmod(locked, 0o700)
        self.arun(self.board.delete(second['id']))
        self.assertFalse(os.path.lexists(home))
        self.assertEqual([view['name'] for view in self.agents()], ['agent-1'])

    def test_the_user_agent_db_symlink_is_removed_as_a_link_only(self):
        second = self.arun(self.board.create_agent())
        self.settle()
        link = self.box.state / 'board-agents' / second['id'] / 'omp-root' / 'agent' / 'agent.db'
        self.assertTrue(link.is_symlink())
        self.box.user_store.write_bytes(b'user login')
        before = os.stat(self.box.user_store)
        self.arun(self.board.delete(second['id']))
        self.assertEqual(self.box.user_store.read_bytes(), b'user login')
        self.assertEqual(os.stat(self.box.user_store).st_ino, before.st_ino)


class ShellTests(WorkspaceCase):  # behaviour 11
    def test_the_first_open_makes_shell_1_once_per_hub_run(self):
        self.arun(self.board.open([]))
        self.settle()
        self.assertEqual([(view['name'], view['state']) for view in self.shells()], [('shell-1', 'running')])
        self.arun(self.board.open([]))
        self.assertEqual(len(self.shells()), 1)
        made = self.arun(self.board.create_shell())
        self.settle()
        self.assertEqual(made['name'], 'shell-2')
        self.assertRegex(made['id'], r'^s-[0-9a-f]{12}$')
        first_pid = self.shells()[0]['pid']
        for view in self.shells():
            self.assertIsNone(self.arun(self.board.close(view['id'])))
        self.assertFalse(alive(first_pid))
        self.assertEqual(self.shells(), [])
        self.arun(self.board.open([]))
        self.assertEqual(self.shells(), [], 'this run already made a shell')
        opened = [(d['name'], d['reason']) for event, _, d in self.audits if event == 'board_terminal_open']
        self.assertEqual(opened, [('shell-1', 'first_open'), ('shell-2', 'create')])
        closed = [d for event, _, d in self.audits if event == 'board_terminal_close']
        self.assertEqual([(d['kind'], d['name'], 'memory_removed' in d) for d in closed],
                         [('shell', 'shell-1', False), ('shell', 'shell-2', False)])
        # A new Hub run: no shell is left, the agents are, and the first open makes shell-1 again.
        self.arun(self.board.stop())
        self.board = self.make_board()
        self.assertEqual(self.shells(), [])
        self.assertEqual([view['name'] for view in self.agents()], ['agent-1'])
        self.arun(self.board.open([]))
        self.settle()
        self.assertEqual([view['name'] for view in self.shells()], ['shell-1'])

    def test_shell_limit_and_an_exited_shell_restarts(self):
        self.arun(self.board.set_options(shell_limit=2))
        a = self.arun(self.board.create_shell())
        self.arun(self.board.create_shell())
        self.settle()
        self.assertEqual(self.refused(self.board.create_shell()), 'limit_reached')
        self.board.write(a['id'], b'exit 4\n')
        self.assertTrue(self.wait(lambda: self.by_name('shell-1')['state'] == 'exited'))
        self.assertEqual(self.by_name('shell-1')['exit_status'], 4)
        again = self.arun(self.board.start(a['id']))
        self.settle()
        self.assertEqual(again['state'], 'running')
        self.assertNotEqual(again['pid'], a['pid'])


class RenameTests(WorkspaceCase):  # names through the set (behaviour 1 rules, stored per kind)
    def test_rename_trims_refuses_bad_or_taken_names_per_kind_and_stores_agent_names(self):
        first = self.agents()[0]
        shell = self.arun(self.board.create_shell())
        renamed = self.arun(self.board.rename(first['id'], '  전처리 '))
        self.assertEqual(renamed['name'], '전처리')
        self.assertEqual(self.registry_file()['agents'][0]['name'], '전처리')
        self.assertEqual(self.refused(self.board.rename(first['id'], 'a\x07')), 'name_invalid')
        second = self.arun(self.board.create_agent())
        self.assertEqual(second['name'], 'agent-1', 'the renamed number is free again')
        self.assertEqual(self.refused(self.board.rename(second['id'], '전처리')), 'name_taken')
        self.assertEqual(self.arun(self.board.rename(shell['id'], '전처리'))['name'], '전처리', 'another kind')
        rows = [d for event, _, d in self.audits if event == 'board_rename']
        self.assertEqual(rows[0], {'terminal_id': first['id'], 'kind': 'agent', 'old': 'agent-1', 'new': '전처리'})


class OptionsTests(WorkspaceCase):  # behaviour 12
    def test_limits_are_range_checked_never_below_the_count_and_kept_across_hub_runs(self):
        for change in ({'agent_limit': 0}, {'agent_limit': 9}, {'shell_limit': 0}, {'shell_limit': 33},
                       {'agent_limit': True}, {'shell_limit': '5'}):
            with self.subTest(change):
                self.assertEqual(self.refused(self.board.set_options(**change)), 'limit_out_of_range')
        self.arun(self.board.create_agent())
        self.arun(self.board.create_shell())
        self.assertEqual(self.refused(self.board.set_options(agent_limit=1)), 'limit_below_count')
        self.assertEqual(self.arun(self.board.set_options(agent_limit=8, shell_limit=32)),
                         {'agent_limit': 8, 'shell_limit': 32})
        self.assertEqual(self.arun(self.board.set_options(shell_limit=1)), {'agent_limit': 8, 'shell_limit': 1})
        self.assertEqual(self.registry_file()['options'], {'agent_limit': 8, 'shell_limit': 1})
        rows = [d for event, _, d in self.audits if event == 'board_options']
        self.assertEqual(rows[-1], {'before': {'agent_limit': 8, 'shell_limit': 32},
                                    'after': {'agent_limit': 8, 'shell_limit': 1}})
        self.arun(self.board.stop())
        self.board = self.make_board()
        self.assertEqual(self.board.options(), {'agent_limit': 8, 'shell_limit': 1})
        for _ in range(6):
            self.arun(self.board.create_agent())
        self.assertEqual(len(self.agents()), 8)
        self.assertEqual(self.refused(self.board.create_agent()), 'limit_reached')


class TerminalLockTests(WorkspaceCase):  # behaviour 14
    grace = 2.0

    def setUp(self):
        script = self.box_script = Path(self.make_temp()) / 'stubborn.sh'
        script.write_text("#!/bin/sh\ntrap '' HUP TERM\necho stubborn-ready\nwhile :; do sleep 0.2; done\n")
        script.chmod(0o755)
        self.config = BoardAgentConfig(shell=str(script))
        super().setUp()

    def make_temp(self) -> str:
        import tempfile
        import shutil
        path = tempfile.mkdtemp(prefix='wbstub-')
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def test_a_slow_end_of_one_terminal_does_not_hold_up_the_others(self):
        slow = self.arun(self.board.create_shell())
        self.settle()
        self.assertTrue(self.wait(lambda: b'stubborn-ready' in self.board.replay(slow['id'])))
        closing = self.loop.create_task(self.board.close(slow['id']))
        self.assertTrue(self.wait(lambda: self.board.find(slow['id']) is not None
                                  and self.board.view(slow['id'])['busy'], 2))
        started = time.monotonic()
        other = self.arun(self.board.create_shell())
        renamed = self.arun(self.board.rename(self.agents()[0]['id'], 'quick'))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual((other['state'], renamed['name']), ('running', 'quick'))
        self.assertEqual(self.refused(self.board.restart(slow['id'])), 'terminal_busy')
        self.arun(closing)
        self.assertGreaterEqual(time.monotonic() - started, self.grace - 0.5)
        self.assertIsNone(self.board.find(slow['id']))
        self.settle()


if __name__ == '__main__':
    unittest.main()
