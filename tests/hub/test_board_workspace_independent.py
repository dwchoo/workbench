"""WBX-D1 part 1, independent adversarial verification: terminal registry, agent/shell lifecycle, per-agent identity.

Expected values come from the contract, not from the worker's tests: web-board-ux SPEC s4 "트랙 D" (names, default names,
limits, registry file rules, per-agent home/key, internal listener, origin label, delete rules, shells, session API,
audit), BRIEF WBX-D41..D58 and AC-25..38, INCIDENT-20261027-omp-login (the user's ``agent.db`` is never touched).

Seams: the in-process Board seam (a real ``BoardSet`` over the real ``BoardLauncher``, the fake ``omp`` and a real
internal REST listener, as ``test_board_launch.LauncherCase``), S1 (aiohttp app with the Board session API) and S3 (a
real ``wb _hub --foreground`` with the fake ``omp``; temp HOME/XDG, short private ``XDG_RUNTIME_DIR``,
``WORKBENCH_HUB_AUTOSTART=0``). The WebSocket v2 shared attach is another verifier's part and is not repeated here.
No real OMP and never the user's ``~/.omp/agent/agent.db`` (a placeholder file in a temp HOME stands in).
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import sys
import textwrap
import time
import unittest
from unittest import mock

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_board_launch as launch_tests  # noqa: E402  (module import: its test classes stay uncollected here)
import test_board_stop as stop_tests  # noqa: E402
import test_web_pages_board_api as api_tests  # noqa: E402
from fixtures.web_sandbox import PASSWORD, WebSandbox, wait_until  # noqa: E402
from support.rest_hub import make_project  # noqa: E402
from test_web_requests_hub import Browser  # noqa: E402

from workbench.backend.project import initialize_project, resolve_project_context  # noqa: E402
from workbench.hub import auth  # noqa: E402
from workbench.hub.board import registry, set as set_module  # noqa: E402
from workbench.hub.board.set import BoardError, BoardSet  # noqa: E402
from workbench.hub.config import BoardAgentConfig  # noqa: E402
from workbench.hub.inbox.service import Principal, ProjectRef  # noqa: E402


def alive(pid: int) -> bool:
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
    except (OSError, IndexError):
        return False
    return fields[0] not in ('Z', 'X')


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(path: Path) -> list:
    """Every entry below ``path`` with its type, link target, inode and size (symlinks are not followed)."""
    found = []
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in sorted(dirs + files):
            full = os.path.join(root, name)
            info = os.lstat(full)
            target = os.readlink(full) if stat.S_ISLNK(info.st_mode) else None
            found.append((os.path.relpath(full, path), stat.S_IFMT(info.st_mode), target, info.st_ino,
                          0 if stat.S_ISDIR(info.st_mode) else info.st_size))
    return sorted(found)


def environ_of(pid: int) -> dict:
    items = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    return dict(item.decode('utf-8', 'replace').split('=', 1) for item in items if b'=' in item)


# ======================================================================================================
# In-process Board seam
# ======================================================================================================

class SetCase(launch_tests.LauncherCase):
    grace = 1.0
    env_extra: dict = {}
    autoboard = True

    def setUp(self):
        super().setUp()
        self.box.env.update(self.env_extra)
        self.launcher = self.make_launcher(self.config)
        self.audits: list = []
        self.pids: set[int] = set()
        self.board = None
        self.addCleanup(self.final_stop)
        if self.autoboard:
            self.board = self.new_board()

    def new_board(self) -> BoardSet:
        def audit(event, actor, address, detail):
            auth.AuditLog._payload(event, actor, address, detail)  # the Hub's audit table takes exactly this row
            self.audits.append((event, actor, address, detail))
        board = BoardSet(self.launcher, grace=self.grace, audit=audit)
        self.api.board_agents = board.agent_for_key
        return board

    def final_stop(self):
        if self.board is not None:
            self.arun(self.board.stop())
        leaked = [pid for pid in self.pids if alive(pid)]
        for pid in leaked:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.assertEqual(leaked, [], f'pids left running: {leaked}')

    # --- helpers -------------------------------------------------------------------------------

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

    def views(self, kind: str | None = None) -> list:
        return [view for view in self.board.views() if kind in (None, view['kind'])]

    def by_name(self, name: str, kind: str | None = None) -> dict:
        found = [view for view in self.views(kind) if view['name'] == name]
        self.assertEqual(len(found), 1, (name, self.board.views()))
        return found[0]

    def refused(self, coroutine) -> str:
        with self.assertRaises(BoardError) as caught:
            self.arun(coroutine)
        return caught.exception.reason

    def agent1(self) -> dict:
        return self.views('agent')[0]

    def new_agent(self) -> dict:
        view = self.arun(self.board.create_agent())
        self.settle()
        return self.by_name(view['name'], 'agent')

    def registry_path(self) -> Path:
        return self.box.state / 'board-agents' / 'registry.json'

    def registry_file(self) -> dict:
        return json.loads(self.registry_path().read_text(encoding='utf-8'))

    def tty_records(self) -> list:
        return self.box.records('tty')

    def record_of(self, pid: int) -> dict:
        self.assertTrue(self.wait(lambda: any(row['pid'] == pid for row in self.tty_records())), f'no record for {pid}')
        return [row for row in self.tty_records() if row['pid'] == pid][-1]

    def system_rows(self) -> list:
        db = sqlite3.connect(f'file:{self.box.state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            return sorted(db.execute('SELECT name, key_id FROM api_keys WHERE system = 1').fetchall())
        finally:
            db.close()

    def internal_status(self, key: str, path: str = '/api/v1/projects') -> int:
        async def get() -> int:
            async with aiohttp.ClientSession() as http_client:
                async with http_client.get(self.listener.url + path, headers={'Authorization': f'Bearer {key}'}) as r:
                    return r.status
        return self.arun(get())

    def key_of(self, view: dict) -> str:
        return self.record_of(view['pid'])['env']['WB_API_KEY']

    def seed_system_row(self, name: str) -> str:
        """A system key row with ``name`` (a crash leftover or an older Hub's key); returns its raw key."""
        issued = self.keys.rotate_system_key('seed', now=time.time())
        self.db.write(lambda execute: execute('UPDATE api_keys SET name = ? WHERE key_id = ?', (name, issued.key_id)))
        return issued.raw

    def make_outside(self, name: str = 'outside') -> Path:
        path = self.box.root / name
        path.mkdir()
        (path / 'canary.txt').write_text('keep me')
        (path / 'agent.db').write_bytes(b'user login store canary')
        return path


# --- (1) names ---------------------------------------------------------------------------------------

class NameRuleTests(SetCase):
    def rename(self, name, terminal_id: str | None = None) -> dict:
        return self.arun(self.board.rename(terminal_id or self.agent1()['id'], name))

    def test_valid_names_are_stored_trimmed_and_counted_in_code_points(self):
        cases = [('전처리', '전처리'), ('  전처리\t', '전처리'), (' nb ', 'nb'), ('　wide　', 'wide'),
                 ('가' * 40, '가' * 40), ('😀' * 40, '😀' * 40), ('é' * 20, 'é' * 20),
                 (' ' + 'x' * 40 + ' ', 'x' * 40), ('a b  c', 'a b  c'), ('Ünï-çødé', 'Ünï-çødé'),
                 ('<b>&amp;"\'', '<b>&amp;"\'')]
        for raw, stored in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.rename(raw)['name'], stored)
                self.assertEqual(self.registry_file()['agents'][0]['name'], stored)
                self.assertEqual(self.agent1()['name'], stored)
        raw_bytes = self.registry_path().read_bytes()
        self.assertIn('<b>&amp;'.encode(), raw_bytes)
        self.assertEqual(self.registry_path().read_bytes().decode('utf-8').count('​'), 0)

    def test_invalid_names_are_refused_and_change_nothing(self):
        self.rename('keep')
        before_bytes, before_audits = self.registry_path().read_bytes(), len(self.audits)
        too_long = ['x' * 41, '가' * 41, '😀' * 41, 'é' * 21, ' ' + 'x' * 41 + ' ']
        controls = ['a\tb', 'a\nb', 'a\rb', 'a\x00b', 'a\x1bb', 'a\x7fb', 'a\x85b', 'a​b', '​x', 'x​',
                    '﻿x', 'a‮b', 'a b', 'a b', 'a­b', 'a⁠b', 'a‍b',
                    '\U0001F468‍\U0001F469', 'a‎b', 'a\U000e0001b']
        for raw in ['', ' ', '\t', ' ', '　 \n', *too_long, *controls, None, 5, 4.5, True, ['x'], {'n': 1}]:
            with self.subTest(raw=raw):
                self.assertEqual(self.refused(self.board.rename(self.agent1()['id'], raw)), 'name_invalid')
        self.assertEqual(self.registry_path().read_bytes(), before_bytes)
        self.assertEqual(len(self.audits), before_audits)
        self.assertEqual(self.agent1()['name'], 'keep')

    def test_a_lone_surrogate_name_is_refused_cleanly_not_a_crash(self):
        # JSON ``"\ud800"`` decodes to a lone surrogate: it cannot be written as UTF-8 to the registry.
        before = self.registry_path().read_bytes()
        for raw in ('\ud800', 'ab\udfffcd'):
            with self.subTest(raw=raw):
                reason = None
                try:
                    self.arun(self.board.rename(self.agent1()['id'], raw))
                except BoardError as exc:
                    reason = exc.reason
                self.assertEqual(reason, 'name_invalid')
        self.assertEqual(self.registry_path().read_bytes(), before)
        registry.load(self.box.state)  # still a valid registry file

    def test_the_reserved_board_agent_name_is_refused_for_both_kinds_in_any_case(self):
        shell = self.arun(self.board.create_shell())
        self.settle()
        for raw in ('board-agent', 'Board-Agent', ' BOARD-AGENT ', 'boArd-AGent　'):
            for terminal in (self.agent1(), shell):
                with self.subTest(raw=raw, kind=terminal['kind']):
                    self.assertEqual(self.refused(self.board.rename(terminal['id'], raw)), 'name_invalid')
        for raw in ('board-agent.', 'board-agent-2', 'my board-agent', 'board agent'):
            self.rename(raw)  # only the exact name is reserved

    def test_uniqueness_is_per_kind_by_casefold_and_the_same_name_is_fine_across_kinds(self):
        second = self.new_agent()
        shell = self.arun(self.board.create_shell())
        self.settle()
        first = self.agent1()['id']
        self.rename('Straße', first)
        for clash in ('STRASSE', 'strasse', 'STRAẞE', 'Strasse', ' strasse '):
            with self.subTest(clash=clash):
                self.assertEqual(self.refused(self.board.rename(second['id'], clash)), 'name_taken')
        self.rename('ΟΔΟΣ', first)
        for clash in ('οδος', 'οδοσ', 'ΟΔΟς'):
            with self.subTest(clash=clash):
                self.assertEqual(self.refused(self.board.rename(second['id'], clash)), 'name_taken')
        self.rename('İstanbul', first)
        self.assertEqual(self.refused(self.board.rename(second['id'], 'i̇stanbul')), 'name_taken')
        self.rename('ﬁle', first)
        self.assertEqual(self.refused(self.board.rename(second['id'], 'FILE')), 'name_taken')
        # the same text on the other kind, and a case change of a terminal's own name
        self.assertEqual(self.rename('FILE', shell['id'])['name'], 'FILE')
        self.assertEqual(self.rename('FILE', first)['name'], 'FILE')  # now both kinds have it
        self.assertEqual(self.rename('File', first)['name'], 'File')
        self.assertEqual(self.refused(self.board.rename(second['id'], 'fILE')), 'name_taken')
        # a rename to exactly the current name changes nothing and writes nothing
        before, audits = self.registry_path().read_bytes(), len(self.audits)
        self.assertEqual(self.rename('File', first)['name'], 'File')
        self.assertEqual((self.registry_path().read_bytes(), len(self.audits)), (before, audits))

    def assert_names_unique_and_loadable(self, results):
        for result in results:
            self.assertTrue(not isinstance(result, BaseException) or isinstance(result, BoardError), results)
        self.settle()
        names = [view['name'].casefold() for view in self.views('agent')]
        self.assertEqual(len(names), len(set(names)), self.views('agent'))
        # a duplicate name would make the whole Board registry_invalid at the next Hub start
        registry.load(self.box.state)
        self.assertEqual(sorted(entry['name'] for entry in self.registry_file()['agents']),
                         sorted(view['name'] for view in self.views('agent')))

    def test_two_concurrent_renames_to_one_name_are_not_both_accepted(self):
        self.arun(self.board.set_options(agent_limit=8))
        second, third = self.new_agent(), self.new_agent()

        async def race():
            return await asyncio.gather(self.board.rename(second['id'], 'Dup'), self.board.rename(third['id'], 'dup'),
                                        return_exceptions=True)
        self.assert_names_unique_and_loadable(self.arun(race()))

    def test_a_create_and_a_rename_racing_for_the_default_name_are_not_both_accepted(self):
        async def race():
            return await asyncio.gather(self.board.create_agent(), self.board.rename(self.agent1()['id'], 'agent-2'),
                                        return_exceptions=True)
        self.assert_names_unique_and_loadable(self.arun(race()))


# --- (1b) default names and (4) limits ----------------------------------------------------------------

class DefaultNameTests(SetCase):
    def test_agents_take_the_smallest_free_number_after_deletes_and_renames(self):
        self.arun(self.board.set_options(agent_limit=8))
        names = [self.new_agent()['name'] for _ in range(3)]
        self.assertEqual(names, ['agent-2', 'agent-3', 'agent-4'])
        self.arun(self.board.delete(self.by_name('agent-3')['id']))
        self.assertEqual(self.new_agent()['name'], 'agent-3')
        self.arun(self.board.rename(self.by_name('agent-1')['id'], '전처리'))
        self.assertEqual(self.new_agent()['name'], 'agent-1')  # a renamed-away number is free again
        self.arun(self.board.rename(self.by_name('agent-2')['id'], 'Agent-02'))  # a leading zero is no number
        self.assertEqual(self.new_agent()['name'], 'agent-2')
        self.arun(self.board.rename(self.by_name('agent-3')['id'], 'AGENT-9'))  # counted by casefold
        self.assertEqual(self.new_agent()['name'], 'agent-3')
        self.assertEqual(self.new_agent()['name'], 'agent-5')
        self.assertEqual(len(self.views('agent')), 8)
        self.assertEqual(self.refused(self.board.create_agent()), 'limit_reached')
        ids = [entry['id'] for entry in self.registry_file()['agents']]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in self.registry_file()['agents']:
            self.assertRegex(entry['id'], r'^a-[0-9a-f]{12}$')

    def test_shells_number_separately_and_a_closed_number_is_reused(self):
        shells = [self.arun(self.board.create_shell()) for _ in range(3)]
        self.settle()
        self.assertEqual([shell['name'] for shell in shells], ['shell-1', 'shell-2', 'shell-3'])
        self.arun(self.board.close(shells[1]['id']))
        self.assertEqual(self.arun(self.board.create_shell())['name'], 'shell-2')
        self.arun(self.board.rename(shells[0]['id'], 'agent-1'))  # another kind's default text: shell numbering free
        self.assertEqual(self.arun(self.board.create_shell())['name'], 'shell-1')
        self.arun(self.board.rename(shells[2]['id'], 'SHELL-7'))
        self.assertEqual(self.arun(self.board.create_shell())['name'], 'shell-3')
        self.settle()
        self.assertEqual(self.agent1()['name'], 'agent-1')  # agents unaffected
        ids = [view['id'] for view in self.views('shell')]
        self.assertTrue(all(re.fullmatch(r's-[0-9a-f]{12}', value) for value in ids), ids)
        self.assertNotIn(shells[1]['id'], ids)  # a closed shell's id is not reused within the run


class LimitTests(SetCase):
    def test_agent_limit_exactly_at_and_one_over_and_the_range(self):
        self.assertEqual(self.board.options(), {'agent_limit': 4, 'shell_limit': 10})
        for number in (2, 3, 4):
            self.assertEqual(self.new_agent()['name'], f'agent-{number}')
        before, started = self.registry_path().read_bytes(), len(self.tty_records())
        self.assertEqual(self.refused(self.board.create_agent()), 'limit_reached')
        self.assertEqual((self.registry_path().read_bytes(), len(self.tty_records()), len(self.views('agent'))),
                         (before, started, 4))
        # lowering below the count is refused, down to the count is fine, nothing is written for a no-op
        audits = len(self.audits)
        self.assertEqual(self.refused(self.board.set_options(agent_limit=3)), 'limit_below_count')
        self.assertEqual(self.arun(self.board.set_options(agent_limit=4)), {'agent_limit': 4, 'shell_limit': 10})
        self.assertEqual((self.registry_path().read_bytes(), len(self.audits)), (before, audits))
        for bad in (0, -1, 9, 100, True, False, '4', 4.0, [4], {'n': 4}):
            with self.subTest(bad=bad):
                self.assertEqual(self.refused(self.board.set_options(agent_limit=bad)), 'limit_out_of_range')
        self.arun(self.board.set_options(agent_limit=8))
        for _ in range(4):
            self.new_agent()
        self.assertEqual(len(self.views('agent')), 8)
        self.assertEqual(self.refused(self.board.create_agent()), 'limit_reached')
        self.assertEqual(self.refused(self.board.set_options(agent_limit=7)), 'limit_below_count')
        self.arun(self.board.delete(self.by_name('agent-3')['id']))
        self.assertEqual(self.arun(self.board.set_options(agent_limit=7))['agent_limit'], 7)
        self.assertEqual(self.refused(self.board.create_agent()), 'limit_reached')  # 7 of 7
        self.arun(self.board.set_options(agent_limit=8))
        self.assertEqual(self.new_agent()['name'], 'agent-3')
        self.assertEqual(self.registry_file()['options'], {'agent_limit': 8, 'shell_limit': 10})
        self.assertEqual(len(self.registry_file()['agents']), 8)

    def test_shell_limit_10_then_32_and_one_over_each(self):
        for _ in range(10):
            self.arun(self.board.create_shell())
        self.settle()
        self.assertEqual(self.refused(self.board.create_shell()), 'limit_reached')
        self.assertEqual(len(self.views('shell')), 10)
        self.assertEqual(self.refused(self.board.set_options(shell_limit=9)), 'limit_below_count')
        for bad in (0, 33, -5, True, '10', 10.0):
            with self.subTest(bad=bad):
                self.assertEqual(self.refused(self.board.set_options(shell_limit=bad)), 'limit_out_of_range')
        self.arun(self.board.set_options(shell_limit=32))
        for _ in range(22):
            self.arun(self.board.create_shell())
        self.settle()
        self.assertEqual(len(self.views('shell')), 32)
        self.assertEqual(self.refused(self.board.create_shell()), 'limit_reached')
        self.assertEqual(len({view['pid'] for view in self.views('shell')}), 32)
        self.assertEqual(self.refused(self.board.set_options(shell_limit=31)), 'limit_below_count')
        self.assertEqual(self.registry_file()['options']['shell_limit'], 32)
        self.assertEqual(self.registry_file()['agents'][0]['name'], 'agent-1')  # shells are never stored

    def test_both_limits_are_saved_together_or_not_at_all(self):
        self.arun(self.board.create_shell())
        self.arun(self.board.create_shell())
        self.settle()
        before = self.registry_path().read_bytes()
        self.assertEqual(self.refused(self.board.set_options(agent_limit=5, shell_limit=1)), 'limit_below_count')
        self.assertEqual(self.refused(self.board.set_options(agent_limit=5, shell_limit=99)), 'limit_out_of_range')
        self.assertEqual(self.refused(self.board.set_options(agent_limit=99, shell_limit=5)), 'limit_out_of_range')
        self.assertEqual(self.registry_path().read_bytes(), before)
        self.assertEqual(self.board.options(), {'agent_limit': 4, 'shell_limit': 10})
        self.assertEqual(self.arun(self.board.set_options(agent_limit=2, shell_limit=2)),
                         {'agent_limit': 2, 'shell_limit': 2})
        details = [detail for event, _, _, detail in self.audits if event == 'board_options']
        self.assertEqual(details, [{'before': {'agent_limit': 4, 'shell_limit': 10},
                                    'after': {'agent_limit': 2, 'shell_limit': 2}}])

    def test_concurrent_creates_at_the_limit_make_exactly_the_free_slots(self):
        async def race():
            return await asyncio.gather(*[self.board.create_agent() for _ in range(6)], return_exceptions=True)
        results = self.arun(race())
        made = [item for item in results if not isinstance(item, BaseException)]
        refused = [item for item in results if isinstance(item, BoardError)]
        self.assertEqual((len(made), len(refused)), (3, 3), results)
        self.assertEqual({item.reason for item in refused}, {'limit_reached'})
        self.settle()
        self.assertEqual(sorted(view['name'] for view in self.views('agent')), ['agent-1', 'agent-2', 'agent-3', 'agent-4'])
        self.assertEqual(len(self.registry_file()['agents']), 4)

        async def shells():
            return await asyncio.gather(*[self.board.create_shell() for _ in range(15)], return_exceptions=True)
        results = self.arun(shells())
        self.settle()
        self.assertEqual(sum(isinstance(item, BoardError) for item in results), 5, results)
        self.assertEqual(sorted(view['name'] for view in self.views('shell')),
                         sorted(f'shell-{number}' for number in range(1, 11)))

    def test_a_limit_change_racing_creates_never_leaves_more_agents_than_the_limit(self):
        async def race():
            return await asyncio.gather(self.board.create_agent(), self.board.create_agent(),
                                        self.board.set_options(agent_limit=2), self.board.create_agent(),
                                        return_exceptions=True)
        self.arun(race())
        self.settle()
        stored = self.registry_file()
        self.assertLessEqual(len(stored['agents']), stored['options']['agent_limit'])
        self.assertEqual(len(stored['agents']), len(self.views('agent')))
        registry.load(self.box.state)


# --- (3) migration and registry file rules --------------------------------------------------------------

class MigrationTests(SetCase):
    autoboard = False

    def plant_old_home(self) -> tuple[Path, Path]:
        session = self.box.state / 'board-agent' / 'omp-root' / 'agent' / 'sessions' / '-home' / 'old.jsonl'
        session.parent.mkdir(parents=True)
        session.write_text('{"old": "session"}\n')
        events = self.box.state / 'board-agent' / 'omp-logs' / 'board-events.log'
        events.parent.mkdir(parents=True)
        events.write_text('{"event": "start", "from": "the previous version"}\n')
        return session, events

    def test_a_pre_d1_state_becomes_agent_1_in_board_agent_and_continues_its_session(self):
        session, events = self.plant_old_home()
        old = (os.stat(session).st_ino, session.read_bytes(), os.stat(self.box.state / 'board-agent').st_ino)
        legacy = self.seed_system_row('board-agent')
        stale = self.seed_system_row('board-agent.a-00000000dead')
        human = self.keys.issue('people', 'editor', {'all': True}, None, now=time.time()).raw
        self.board = self.new_board()
        data = self.registry_file()
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['options'], {'agent_limit': 4, 'shell_limit': 10})
        self.assertEqual([(a['name'], a['home']) for a in data['agents']], [('agent-1', 'board-agent')])
        self.assertRegex(data['agents'][0]['id'], r'^a-[0-9a-f]{12}$')
        self.assertEqual(stat.S_IMODE(os.stat(self.registry_path()).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.registry_path().parent).st_mode), 0o700)
        self.assertEqual(os.listdir(self.registry_path().parent), ['registry.json'])  # no agent dir, no temp file
        view = self.agent1()
        self.assertEqual((view['name'], view['state']), ('agent-1', 'not_started'))
        self.arun(self.board.open([view['id']]))
        self.settle()
        started = self.by_name('agent-1')
        record = self.record_of(started['pid'])
        self.assertIn('--continue', record['argv'])
        self.assertEqual(record['env']['PI_CODING_AGENT_DIR'], str(self.box.state / 'board-agent' / 'omp-root' / 'agent'))
        self.assertEqual([name for name, _ in self.system_rows()], [f"board-agent.{view['id']}"])
        self.assertEqual((self.internal_status(legacy), self.internal_status(stale), self.internal_status(human)),
                         (401, 401, 401))
        self.assertEqual(self.internal_status(record['env']['WB_API_KEY']), 200)
        self.assertEqual(self.keys.verify(human, time.time()).name, 'people')  # a person's key is never purged
        self.assertEqual((os.stat(session).st_ino, session.read_bytes(), os.stat(self.box.state / 'board-agent').st_ino),
                         old)
        self.assertTrue(events.read_text().startswith('{"event": "start", "from": "the previous version"}'))
        self.assertEqual(os.listdir(self.registry_path().parent), ['registry.json'])
        self.assertTrue(os.path.lexists(self.box.state / 'board-agent'))

    def test_a_second_hub_run_keeps_ids_names_and_limits_and_nothing_runs_until_opened(self):
        self.board = self.new_board()
        self.arun(self.board.set_options(agent_limit=6, shell_limit=3))
        second = self.new_agent()
        self.arun(self.board.rename(second['id'], '전처리'))
        first_id = self.agent1()['id']
        shell = self.arun(self.board.create_shell())
        self.settle()
        self.arun(self.board.stop_terminal(second['id']))
        self.arun(self.board.stop())
        self.assertEqual(self.system_rows(), [])
        self.assertFalse(alive(shell['pid']))
        self.board = self.new_board()
        self.assertEqual([(v['id'], v['name'], v['state']) for v in self.views()],
                         [(first_id, 'agent-1', 'not_started'), (second['id'], '전처리', 'not_started')])
        self.assertEqual(self.board.options(), {'agent_limit': 6, 'shell_limit': 3})
        self.assertEqual(self.views('shell'), [])  # shells are not restored (WBX-D45)
        self.assertEqual(self.new_agent()['name'], 'agent-2')
        # the run's first open makes shell-1; the agent stopped in the previous run starts again when visible
        self.arun(self.board.open([second['id']]))
        self.settle()
        self.assertEqual([v['name'] for v in self.views('shell')], ['shell-1'])
        self.assertIn(self.by_name('전처리')['state'], ('starting', 'running'))
        self.assertEqual(self.by_name('agent-1')['state'], 'not_started')

    def test_without_a_registry_file_but_with_the_directory_a_fresh_file_is_made(self):
        directory = self.box.state / 'board-agents'
        directory.mkdir(mode=0o700)
        self.board = self.new_board()
        self.assertEqual(self.agent1()['name'], 'agent-1')
        self.assertEqual(self.registry_file()['agents'][0]['home'], 'board-agent')


def _agent_entry(index: int = 1, **fields) -> dict:
    entry = {'id': f'a-{index:012x}', 'name': f'agent-{index}', 'home': f'board-agents/a-{index:012x}',
             'created_at': 1.0}
    entry.update(fields)
    return entry


def _registry(agents=None, **top) -> bytes:
    value = {'schema_version': 1, 'options': {'agent_limit': 4, 'shell_limit': 10},
             'agents': [_agent_entry(1, home='board-agent')] if agents is None else agents}
    value.update(top)
    return json.dumps(value).encode()


class RegistryInvalidTests(SetCase):
    autoboard = False

    def plant(self, raw: bytes, *, file_mode: int = 0o600, dir_mode: int = 0o700) -> Path:
        directory = self.box.state / 'board-agents'
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / 'registry.json'
        if os.path.lexists(path):
            os.unlink(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, raw)
        os.close(fd)
        os.chmod(path, file_mode)
        os.chmod(directory, dir_mode)
        self.addCleanup(lambda: os.path.isdir(directory) and os.chmod(directory, 0o700))
        return path

    def assert_invalid(self, expect_bytes: bytes | None = None) -> BoardSet:
        path = self.box.state / 'board-agents' / 'registry.json'
        before = tree(self.box.state / 'board-agents')
        board = self.board = self.new_board()
        self.assertEqual(board.board_view()['state'], 'registry_invalid', board.board_view())
        self.assertIn(str(path), board.board_view()['message'])
        self.assertEqual(board.views(), [])
        for action in (board.create_agent(), board.create_shell(), board.open([]), board.set_options(agent_limit=5),
                       board.rename('a-000000000001', 'x'), board.start('a-000000000001'),
                       board.delete('a-000000000001')):
            self.assertEqual(self.refused(action), 'registry_invalid')
        self.assertEqual(self.tty_records(), [])
        self.assertEqual(board.dot(), 'error')
        self.assertEqual(board.active(), [])
        self.assertEqual(board.status()['state'], 'registry_invalid')
        if expect_bytes is not None:
            self.assertEqual(path.read_bytes(), expect_bytes)  # never overwritten
        self.assertEqual(tree(self.box.state / 'board-agents'), before)
        self.assertFalse(os.path.lexists(self.box.state / 'board-agent'))  # not even the old home is made
        return board

    def test_unreadable_or_rule_breaking_content_is_registry_invalid_and_never_rewritten(self):
        good = _agent_entry(1, home='board-agent')
        cases = {
            'empty': b'', 'brace': b'{', 'utf8': b'\xff\xfe\x00', 'deep': b'[' * 200000, 'list': b'[]', 'null': b'null',
            'schema2': _registry(schema_version=2), 'schema_true': _registry(schema_version=True),
            'no_options': json.dumps({'schema_version': 1, 'agents': []}).encode(),
            'extra_top': _registry(extra=1),
            'limit0': _registry(options={'agent_limit': 0, 'shell_limit': 10}),
            'limit9': _registry(options={'agent_limit': 9, 'shell_limit': 10}),
            'shell33': _registry(options={'agent_limit': 4, 'shell_limit': 33}),
            'limit_bool': _registry(options={'agent_limit': True, 'shell_limit': 10}),
            'limit_str': _registry(options={'agent_limit': '4', 'shell_limit': 10}),
            'options_extra': _registry(options={'agent_limit': 4, 'shell_limit': 10, 'x': 1}),
            'bad_id': _registry([_agent_entry(1, id='a-ABCDEF012345', home='board-agents/a-ABCDEF012345')]),
            'short_id': _registry([_agent_entry(1, id='a-1', home='board-agents/a-1')]),
            'shell_id': _registry([_agent_entry(1, id='s-000000000001', home='board-agents/s-000000000001')]),
            'dup_id': _registry([good, _agent_entry(1, name='agent-2')]),
            'home_dotdot': _registry([_agent_entry(1, home='../x')]),
            'home_abs': _registry([_agent_entry(1, home='/etc')]),
            'home_other_id': _registry([_agent_entry(1, home='board-agents/a-000000000002')]),
            'home_slash': _registry([_agent_entry(1, home='board-agents/a-000000000001/')]),
            'home_board_agents': _registry([_agent_entry(1, home='board-agents')]),
            'two_migrated': _registry([good, _agent_entry(2, home='board-agent')]),
            'name_empty': _registry([_agent_entry(1, name='')]),
            'name_spaces': _registry([_agent_entry(1, name=' x ')]),
            'name_long': _registry([_agent_entry(1, name='x' * 41)]),
            'name_reserved': _registry([_agent_entry(1, name='board-agent')]),
            'name_zwsp': _registry([_agent_entry(1, name='a​b')]),
            'name_dup_case': _registry([_agent_entry(1, name='Same'), _agent_entry(2, name='same')]),
            'name_type': _registry([_agent_entry(1, name=5)]),
            'created_str': _registry([_agent_entry(1, created_at='x')]),
            'created_bool': _registry([_agent_entry(1, created_at=True)]),
            'entry_extra': _registry([_agent_entry(1, extra=1)]),
            'entry_missing': _registry([{'id': 'a-000000000001', 'name': 'x', 'home': 'board-agent'}]),
            'not_dict_entry': _registry(['agent']), 'agents_dict': _registry({}),
            'over_limit': _registry([_agent_entry(index) for index in range(1, 6)]),
            'huge': b'{"x": "' + b'a' * (1 << 20) + b'"}',
        }
        for label, raw in cases.items():
            with self.subTest(label):
                self.plant(raw)
                self.assert_invalid(raw)

    def test_nan_infinity_and_oversized_numbers_in_created_at(self):
        for text in ('NaN', '-Infinity'):
            with self.subTest(text):
                raw = _registry().replace(b'"created_at": 1.0', f'"created_at": {text}'.encode())
                self.assertIn(text.encode(), raw)
                self.plant(raw)
                if text == 'NaN':
                    self.assert_invalid(raw)

    def test_a_wrong_mode_owner_kind_symlink_or_fifo_is_registry_invalid_and_hangs_nothing(self):
        raw = _registry()
        for label, mode in (('0644', 0o644), ('0666', 0o666), ('0400', 0o400), ('0640', 0o640), ('0700', 0o700)):
            with self.subTest(file=label):
                self.plant(raw, file_mode=mode)
                self.assert_invalid(raw)
        for label, mode in (('0755', 0o755), ('0777', 0o777), ('0500', 0o500), ('0710', 0o710)):
            with self.subTest(directory=label):
                self.plant(raw, dir_mode=mode)
                self.assert_invalid(raw)
        directory = self.box.state / 'board-agents'
        # the file is a symlink to a valid registry elsewhere
        elsewhere = self.box.root / 'elsewhere.json'
        elsewhere.write_bytes(raw)
        os.chmod(elsewhere, 0o600)
        self.plant(b'x')
        os.unlink(directory / 'registry.json')
        (directory / 'registry.json').symlink_to(elsewhere)
        self.assert_invalid()
        self.assertEqual(elsewhere.read_bytes(), raw)
        self.assertTrue((directory / 'registry.json').is_symlink())
        # the file is a FIFO (a blocking open would hang the Hub) and a directory
        os.unlink(directory / 'registry.json')
        os.mkfifo(directory / 'registry.json', 0o600)
        started = time.monotonic()
        self.assert_invalid()
        self.assertLess(time.monotonic() - started, 5)
        os.unlink(directory / 'registry.json')
        (directory / 'registry.json').mkdir(mode=0o700)
        self.assert_invalid()
        (directory / 'registry.json').rmdir()
        # board-agents is a symlink to a directory holding a valid registry, or a plain file
        real = self.box.root / 'real-board-agents'
        real.mkdir(mode=0o700)
        (real / 'registry.json').write_bytes(raw)
        os.chmod(real / 'registry.json', 0o600)
        shutil.rmtree(directory)
        directory.symlink_to(real)
        board = self.board = self.new_board()
        self.assertEqual(board.board_view()['state'], 'registry_invalid')
        self.assertEqual(os.listdir(real), ['registry.json'])
        directory.unlink()
        directory.write_text('not a directory')
        board = self.board = self.new_board()
        self.assertEqual(board.board_view()['state'], 'registry_invalid')
        self.assertEqual(directory.read_text(), 'not a directory')

    def test_a_state_directory_that_cannot_hold_the_registry_leaves_the_board_invalid_not_half_made(self):
        os.chmod(self.box.state, 0o500)
        self.addCleanup(os.chmod, self.box.state, 0o700)
        board = self.board = self.new_board()
        self.assertEqual(board.board_view()['state'], 'registry_invalid')
        self.assertIn('registry.json', board.board_view()['message'])
        self.assertEqual(self.refused(board.create_agent()), 'registry_invalid')
        self.assertFalse(os.path.lexists(self.box.state / 'board-agents' / 'registry.json'))


class RegistryWriteFailureTests(SetCase):
    def lock_registry_dir(self):
        directory = self.box.state / 'board-agents'
        os.chmod(directory, 0o500)
        self.addCleanup(lambda: os.path.isdir(directory) and os.chmod(directory, 0o700))

    def unlock_registry_dir(self):
        os.chmod(self.box.state / 'board-agents', 0o700)

    def test_a_failed_write_refuses_every_registry_action_and_starts_or_ends_no_process(self):
        shell = self.arun(self.board.create_shell())
        running = self.new_agent()
        self.arun(self.board.open([self.agent1()['id']]))
        self.settle()
        before = (self.registry_path().read_bytes(), self.views(), len(self.tty_records()), len(self.audits))
        self.lock_registry_dir()
        for label, action in (('create_agent', self.board.create_agent()), ('options', self.board.set_options(agent_limit=5)),
                              ('rename', self.board.rename(running['id'], 'renamed'))):
            with self.subTest(label):
                self.assertEqual(self.refused(action), 'registry_write_failed')
        after = (self.registry_path().read_bytes(), self.views(), len(self.tty_records()), len(self.audits))
        self.assertEqual(before, after)  # nothing started, nothing ended, nothing renamed, nothing audited
        self.assertTrue(all(alive(view['pid']) for view in self.views() if view['pid']))
        self.assertEqual(self.board.options(), {'agent_limit': 4, 'shell_limit': 10})
        stray = [name for name in os.listdir(self.registry_path().parent)
                 if name != 'registry.json' and not re.fullmatch(r'a-[0-9a-f]{12}', name)]
        self.assertEqual(stray, [])  # no temp file left behind by the failed writes
        self.assertNotIn('renamed', [view['name'] for view in self.views()])
        # actions that need no registry change still work
        self.assertEqual(self.arun(self.board.stop_terminal(running['id']))['state'], 'stopped')
        self.arun(self.board.close(shell['id']))
        self.unlock_registry_dir()
        self.assertEqual(self.arun(self.board.rename(running['id'], 'renamed'))['name'], 'renamed')

    def test_a_delete_whose_registry_write_fails_keeps_the_entry_and_leaves_no_process(self):
        first = self.agent1()
        self.arun(self.board.open([first['id']]))
        self.settle()
        pid, key = first and self.by_name('agent-1')['pid'], self.key_of(self.by_name('agent-1'))
        self.lock_registry_dir()
        self.assertEqual(self.refused(self.board.delete(first['id'])), 'registry_write_failed')
        self.assertFalse(alive(pid))
        self.assertEqual(self.internal_status(key), 401)
        self.assertEqual(self.system_rows(), [])
        self.assertIn(first['id'], [entry['id'] for entry in self.registry_file()['agents']])
        view = self.by_name('agent-1')
        self.assertEqual((view['state'], view['busy']), ('failed', False))
        self.unlock_registry_dir()
        self.arun(self.board.delete(first['id']))
        self.assertEqual(self.registry_file()['agents'], [])
        self.assertFalse(os.path.lexists(self.box.state / 'board-agent'))

    def test_many_successful_writes_leave_exactly_one_0600_file(self):
        for number in range(6):
            self.arun(self.board.rename(self.agent1()['id'], f'name-{number}'))
        self.assertEqual(os.listdir(self.registry_path().parent), ['registry.json'])
        self.assertEqual(stat.S_IMODE(os.stat(self.registry_path()).st_mode), 0o600)
        self.assertEqual(self.registry_file()['agents'][0]['name'], 'name-5')


# --- (5)(6)(7) per-agent home, memory and identity ---------------------------------------------------------

class AgentIdentityTests(SetCase):
    def test_two_agents_have_separate_homes_the_same_user_store_link_and_their_own_memory(self):
        user_before = os.stat(self.box.user_store)
        self.arun(self.board.open([self.agent1()['id']]))
        self.settle()
        first = self.by_name('agent-1')
        second = self.new_agent()
        ra, rb = self.record_of(first['pid']), self.record_of(second['pid'])
        home_a = self.box.state / 'board-agent'
        home_b = self.box.state / 'board-agents' / second['id']
        for home, rec in ((home_a, ra), (home_b, rb)):
            self.assertEqual(rec['env']['PI_CODING_AGENT_DIR'], str(home / 'omp-root' / 'agent'))
            link = home / 'omp-root' / 'agent' / 'agent.db'
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(self.box.user_store))
            self.assertTrue((home / 'skills' / 'wb-inbox' / 'SKILL.md').is_file())
            self.assertTrue((home / 'omp-logs' / 'board-events.log').is_file())
            overlay = json.loads(rec['overlay'])
            self.assertEqual(overlay['skills']['customDirectories'], [str(home / 'skills')])
            self.assertEqual(overlay['skills']['includeSkills'], ['wb-inbox'])
        self.assertNotEqual(ra['env']['PI_CODING_AGENT_DIR'], rb['env']['PI_CODING_AGENT_DIR'])
        self.assertNotEqual(ra['env']['PI_CONFIG_DIR'], rb['env']['PI_CONFIG_DIR'])
        self.assertEqual(ra['env']['WB_HUB_URL'], rb['env']['WB_HUB_URL'])  # one shared internal listener
        self.assertEqual(ra['argv'][ra['argv'].index('--append-system-prompt') + 1],
                         rb['argv'][rb['argv'].index('--append-system-prompt') + 1])  # the shared role prompt
        self.assertEqual(ra['argv'][1:3], rb['argv'][1:3])  # the same isolation overlay
        self.assertNotIn('--continue', ra['argv'] + rb['argv'])
        # memory: only agent-1 writes a session; stopping and starting both continues only agent-1's
        self.board.write(first['id'], b'session\n')
        sessions_a = home_a / 'omp-root' / 'agent' / 'sessions'
        self.assertTrue(self.wait(lambda: any(sessions_a.rglob('*.jsonl'))))
        inode = [os.stat(path).st_ino for path in sessions_a.rglob('*.jsonl')]
        self.assertFalse(any((home_b / 'omp-root' / 'agent' / 'sessions').rglob('*.jsonl')))
        self.arun(self.board.stop_terminal(first['id']))
        self.arun(self.board.stop_terminal(second['id']))
        self.assertEqual({view['state'] for view in self.views('agent')}, {'stopped'})
        self.arun(self.board.start(first['id']))
        self.arun(self.board.start(second['id']))
        self.settle()
        rows = {row['pid']: row for row in self.tty_records()}
        new_a, new_b = rows[self.by_name('agent-1')['pid']], rows[self.by_name('agent-2')['pid']]
        self.assertIn('--continue', new_a['argv'])
        self.assertNotIn('--continue', new_b['argv'])
        self.assertEqual([os.stat(path).st_ino for path in sessions_a.rglob('*.jsonl')], inode)  # memory kept
        after = os.stat(self.box.user_store)
        self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns),
                         (user_before.st_ino, user_before.st_size, user_before.st_mtime_ns))
        for home in (home_a, home_b):
            lines = (home / 'omp-logs' / 'board-events.log').read_text().splitlines()
            self.assertTrue(lines)
            self.assertFalse(any('wbk_' in line for line in lines))
        self.assertGreaterEqual(len((home_a / 'omp-logs' / 'board-events.log').read_text().splitlines()), 3)

    def test_a_failed_or_isolation_failed_agent_does_not_touch_the_others(self):
        self.arun(self.board.open([self.agent1()['id']]))
        self.settle()
        good = self.by_name('agent-1')
        shell = self.arun(self.board.create_shell())
        self.settle()
        good_key = self.key_of(good)
        self.launcher.environment['FAKE_BOARD_TOOLS'] = json.dumps(['mcp__decoy_user_x'])  # the next check sees a leaked tool
        leaky = self.new_agent()
        self.assertTrue(self.wait(lambda: self.by_name('agent-2')['state'] == 'isolation_failed'))
        leaky = self.by_name('agent-2')
        self.assertIn('leak', leaky['detail'])
        self.assertFalse(alive(leaky['pid'] or 0))
        self.launcher.environment['WORKBENCH_OMP'] = str(self.box.root / 'no-such-omp')
        broken = self.arun(self.board.create_agent())
        self.assertEqual((broken['state'], broken['detail']), ('failed', 'omp_unavailable'))
        self.launcher.environment['WORKBENCH_OMP'] = str(self.box.omp)
        self.assertEqual(self.by_name('agent-1')['state'], 'running')
        self.assertTrue(alive(good['pid']) and alive(shell['pid']))
        self.assertEqual(self.internal_status(good_key), 200)
        self.assertEqual([name for name, _ in self.system_rows()], [f"board-agent.{good['id']}"])
        # each of the others can still start once the cause is gone
        self.launcher.environment.pop('FAKE_BOARD_TOOLS')
        self.arun(self.board.start(leaky['id']))
        self.arun(self.board.start(broken['id']))
        self.settle()
        self.assertTrue(self.wait(lambda: {v['state'] for v in self.views('agent')} == {'running'}))


class AgentKeyTests(SetCase):
    def test_each_start_has_its_own_key_row_that_ends_with_its_process_in_every_way(self):
        self.arun(self.board.open([self.agent1()['id']]))
        self.settle()
        first = self.by_name('agent-1')
        second, third, fourth = self.new_agent(), self.new_agent(), self.new_agent()
        keys = {view['id']: self.key_of(view) for view in (first, second, third, fourth)}
        self.assertEqual(len(set(keys.values())), 4)
        rows = dict(self.system_rows())
        self.assertEqual(sorted(rows), sorted(f'board-agent.{agent_id}' for agent_id in keys))
        for agent_id, key in keys.items():
            self.assertEqual(key.split('_')[1], rows[f'board-agent.{agent_id}'])  # wbk_<key id>_<secret>
            self.assertEqual(self.internal_status(key), 200)
            for other in (first, second, third, fourth):
                if other['id'] != agent_id:  # an agent's key is in no other agent's environment
                    self.assertNotIn(key.encode(), Path(f"/proc/{other['pid']}/environ").read_bytes())
        # self exit, stop, delete, isolation failure each drop only their own row and stop the key at once
        self.board.write(second['id'], b'exit 3\n')
        self.assertTrue(self.wait(lambda: self.by_name(second['name'])['state'] == 'exited'))
        self.assertTrue(self.wait(lambda: f"board-agent.{second['id']}" not in dict(self.system_rows())))
        self.assertEqual(self.internal_status(keys[second['id']]), 401)
        self.arun(self.board.stop_terminal(third['id']))
        self.assertEqual(self.internal_status(keys[third['id']]), 401)
        self.assertNotIn(f"board-agent.{third['id']}", dict(self.system_rows()))
        self.arun(self.board.delete(fourth['id']))
        self.assertEqual(self.internal_status(keys[fourth['id']]), 401)
        self.assertEqual([name for name, _ in self.system_rows()], [f"board-agent.{first['id']}"])
        self.assertEqual(self.internal_status(keys[first['id']]), 200)
        # a restarted agent's old key is dead, the new one works, and another agent's key is untouched
        self.arun(self.board.restart(first['id']))
        self.settle()
        self.assertEqual(self.internal_status(keys[first['id']]), 401)
        self.assertEqual(self.internal_status(self.key_of(self.by_name('agent-1'))), 200)
        self.assertEqual(len(self.system_rows()), 1)

    def test_a_stopped_agents_key_is_dead_before_its_process_is_even_signalled(self):
        self.arun(self.board.open([self.agent1()['id']]))
        self.settle()
        first = self.by_name('agent-1')
        key = self.key_of(first)
        seen = []
        original = set_module.Channel.terminate

        def watching(channel, *args, **kwargs):
            # runs in the executor, right before the first signal: the stop is under way, the key must already be dead
            seen.append((self.board.find(first['id']).busy, self.board.agent_for_key(self.key_row(key),
                                                                                   f"board-agent.{first['id']}"),
                         self.system_rows()))
            return original(channel, *args, **kwargs)
        with mock.patch.object(set_module.Channel, 'terminate', watching):
            self.arun(self.board.stop_terminal(first['id']))
        self.assertEqual(len(seen), 1)
        busy, accepted, rows_during = seen[0]
        self.assertTrue(busy)
        self.assertIsNone(accepted)
        self.assertEqual(self.internal_status(key), 401)
        self.assertEqual(self.system_rows(), [])

    @staticmethod
    def key_row(raw: str) -> str:
        return raw.split('_')[1]

    def test_isolation_failure_drops_the_key_and_a_leftover_row_of_a_stopped_agent_stays_dead(self):
        self.launcher.environment['FAKE_BOARD_TOOLS'] = json.dumps(['mcp__decoy_user_x'])
        self.arun(self.board.open([self.agent1()['id']]))
        self.assertTrue(self.wait(lambda: self.by_name('agent-1')['state'] == 'isolation_failed'))
        self.settle()
        view = self.by_name('agent-1')
        key = self.box.records('tty')[-1]['env']['WB_API_KEY']
        self.assertEqual(self.system_rows(), [])
        self.assertEqual(self.internal_status(key), 401)
        leftover = self.keys.rotate_system_key(view['id'], now=time.time()).raw  # a row nobody holds
        self.assertEqual(self.internal_status(leftover), 401)
        self.assertIsNone(self.board.agent_for_key(self.key_row(leftover), f"board-agent.{view['id']}"))

    def test_hub_stop_ends_all_keys_and_processes_and_nothing_starts_afterwards(self):
        self.arun(self.board.open([self.agent1()['id']]))
        second = self.new_agent()
        shell = self.arun(self.board.create_shell())
        self.settle()
        keys = [self.key_of(self.by_name('agent-1')), self.key_of(second)]
        pids = [view['pid'] for view in self.views()]
        self.arun(self.board.stop())
        self.assertFalse(any(alive(pid) for pid in pids))
        self.assertEqual(self.system_rows(), [])
        self.assertEqual({view['state'] for view in self.views('agent')}, {'stopped'})
        self.assertEqual(self.by_name('shell-1')['state'], 'exited')
        self.assertEqual(self.board.dot(), 'off')
        self.assertEqual(self.board.active(), [])
        for action in (self.board.create_agent(), self.board.create_shell(), self.board.open([second['id']]),
                       self.board.start(second['id'])):
            self.assertEqual(self.refused(action), 'board_unavailable')
        self.assertTrue(all(self.board.agent_for_key(self.key_row(key), name) is None
                            for key, name in zip(keys, (f'board-agent.{self.agent1()["id"]}', f'board-agent.{second["id"]}'))))
        self.arun(self.board.stop())  # idempotent
        self.assertEqual([view['pid'] for view in self.views()], pids)
        self.assertEqual(shell['kind'], 'shell')

    def test_a_person_cannot_hold_an_agent_key_name_and_an_id_that_collides_is_redrawn(self):
        for name in ('board-agent', 'board-agent.', 'board-agent.a-0123456789ab', 'board-agent.anything'):
            with self.subTest(name), self.assertRaises(auth.ApiKeyRejected) as caught:
                self.keys.issue(name, 'editor', {'all': True}, None, now=time.time())
            self.assertEqual(caught.exception.reason, 'name_reserved')
        for name in ('board-agents', 'Board-Agent.a', 'board_agent'):
            self.keys.issue(name, 'editor', {'all': True}, None, now=time.time())
        # An older person's key already named like a new agent's key: that id is drawn again.
        self.db.write(lambda execute: execute(
            "INSERT INTO api_keys (key_id, name, grade, scope_json, secret_hash, created_at, system) "
            "VALUES ('personkey1', 'board-agent.a-111111111111', 'editor', '{\"all\": true}', '00', 0, 0)"))
        draws = iter(['a-111111111111', 'a-222222222222'])
        with mock.patch.object(registry, 'new_id', side_effect=lambda kind, taken=(): next(draws)):
            made = self.arun(self.board.create_agent())
        self.settle()
        self.assertEqual(made['id'], 'a-222222222222')
        self.assertEqual(self.by_name(made['name'])['state'], 'running')
        self.assertEqual(dict(self.system_rows()).keys(), {'board-agent.a-222222222222'})
        self.assertEqual(self.internal_status('wbk_personkey1_' + 'a' * 43), 401)


# --- (8) request origin ---------------------------------------------------------------------------------

class OriginTests(SetCase):
    def setUp(self):
        super().setUp()
        self.root, self.project = make_project(self.box.root, 'alpha')
        self.inbox.set_projects([ProjectRef(self.project, self.root, 'alpha')])

    def rest(self, key: str, method: str, path: str, body: dict | None = None):
        async def call():
            async with aiohttp.ClientSession() as http_client:
                async with http_client.request(method, self.listener.url + '/api/v1' + path, json=body,
                                               headers={'Authorization': f'Bearer {key}'}) as response:
                    return response.status, await response.json()
        return self.arun(call())

    def inbox_rows(self, sql: str) -> list:
        db = sqlite3.connect(f'file:{self.root / ".wb" / "inbox.sqlite3"}?mode=ro', uri=True)
        try:
            return db.execute(sql).fetchall()
        finally:
            db.close()

    def test_each_agent_stamps_its_own_name_at_submit_time_on_requests_edits_and_comments(self):
        self.arun(self.board.open([self.agent1()['id']]))
        second = self.new_agent()
        self.settle()
        first = self.by_name('agent-1')
        key1, key2 = self.key_of(first), self.key_of(second)
        self.arun(self.board.rename(first['id'], '전처리'))
        status, one = self.rest(key1, 'POST', '/requests', {'project_id': self.project, 'title': 'from one', 'body': 'b',
                                                          'idempotency_key': 'k1'})
        self.assertEqual((status, one['origin']['label']), (201, 'Board agent · 전처리'))
        self.assertEqual(one['origin']['kind'], 'board_agent')
        status, two = self.rest(key2, 'POST', '/requests', {'project_id': self.project, 'title': 'from two', 'body': 'b',
                                                          'idempotency_key': 'k2'})
        self.assertEqual((status, two['origin']['label']), (201, 'Board agent · agent-2'))
        self.arun(self.board.rename(first['id'], '이후 이름'))
        self.arun(self.board.rename(second['id'], '<b>x</b>'))
        status, fetched = self.rest(key1, 'GET', f"/requests/{one['request_id']}")
        fetched = fetched.get('request', fetched)
        self.assertEqual(fetched['origin']['label'], 'Board agent · 전처리')  # not renamed afterwards
        status, edited = self.rest(key1, 'PATCH', f"/requests/{one['request_id']}", {'base_version': fetched['version'],
                                                                              'title': 'edited'})
        self.assertEqual(status, 200, edited)
        status, _ = self.rest(key2, 'POST', f"/requests/{one['request_id']}/comments", {'body': 'hello'})
        self.assertEqual(status, 201)
        authors = self.inbox_rows('SELECT author_kind, author_name FROM request_revisions ORDER BY rowid')
        comments = self.inbox_rows("SELECT author_kind, author_name FROM comments WHERE kind != 'system' ORDER BY seq")
        self.assertEqual(authors, [('board_agent', '전처리'), ('board_agent', 'agent-2')])  # an in-place edit adds no row
        self.assertEqual(comments[-1], ('board_agent', '<b>x</b>'))  # stored as typed; the pages escape it
        self.assertEqual(self.inbox_rows('SELECT title, origin_name FROM requests ORDER BY seq'),
                         [('edited', '전처리'), ('from two', 'agent-2')])
        # deleting an agent leaves the names stored in its requests, and a stopped agent cannot write
        self.arun(self.board.delete(second['id']))
        self.assertEqual(self.rest(key2, 'POST', '/requests', {'project_id': self.project, 'title': 't', 'body': 'b',
                                                               'idempotency_key': 'k3'})[0], 401)
        self.assertEqual(self.inbox_rows('SELECT origin_name FROM requests WHERE title = "from two"'), [('agent-2',)])
        # a row from before WBX-D48 (no origin_name) still reads "Board agent"
        legacy = self.inbox.submit(Principal.board_agent('oldkey'), self.project, 'legacy', 'b', 'k-legacy')
        self.assertEqual(legacy['origin']['label'], 'Board agent')
        self.assertIsNone(legacy['origin']['name'])
        self.assertEqual(self.inbox_rows('SELECT origin_name FROM requests WHERE title = "legacy"'), [(None,)])


# --- (10) delete and the symlink boundary --------------------------------------------------------------

class DeleteBoundaryTests(SetCase):
    def test_links_planted_inside_a_home_are_removed_as_links_and_nothing_outside_changes(self):
        second = self.new_agent()
        self.arun(self.board.stop_terminal(second['id']))
        outside = self.make_outside()
        (outside / 'deep').mkdir()
        (outside / 'deep' / 'inner.txt').write_text('inner')
        hard_source = outside / 'hard-target.txt'
        hard_source.write_text('hardlinked content')
        home = self.box.state / 'board-agents' / second['id']
        inside = home / 'omp-root' / 'agent'
        (inside / 'evil_dir').symlink_to(outside)
        (inside / 'evil_file').symlink_to(outside / 'canary.txt')
        (inside / 'other.db').symlink_to(outside / 'agent.db')
        (home / 'omp-logs' / 'loop').symlink_to(home / 'omp-logs')
        (home / 'skills' / 'wb-inbox' / 'up').symlink_to('../../../..')
        (home / 'dangling').symlink_to(self.box.root / 'does-not-exist')
        os.link(hard_source, home / 'omp-root' / 'hard.txt')
        os.mkfifo(home / 'omp-root' / 'fifo')
        outside_before, user_before = tree(outside), os.stat(self.box.user_store)
        user_digest, canary_digest = digest(self.box.user_store), digest(outside / 'agent.db')
        state_tree = tree(self.box.state / 'board-agent')
        self.arun(self.board.delete(second['id']))
        self.assertFalse(os.path.lexists(home))
        after = [(rel, kind, target, inode, size) for rel, kind, target, inode, size in tree(outside)]
        self.assertEqual([row for row in after if row[0] != 'hard-target.txt'],
                         [row for row in outside_before if row[0] != 'hard-target.txt'])
        self.assertEqual(hard_source.read_text(), 'hardlinked content')
        self.assertEqual((outside / 'canary.txt').read_text(), 'keep me')
        self.assertEqual(digest(outside / 'agent.db'), canary_digest)
        user_after = os.stat(self.box.user_store)
        self.assertEqual((user_after.st_ino, user_after.st_size, user_after.st_mtime_ns, digest(self.box.user_store)),
                         (user_before.st_ino, user_before.st_size, user_before.st_mtime_ns, user_digest))
        self.assertEqual(tree(self.box.state / 'board-agent'), state_tree)  # the migrated home is untouched
        self.assertEqual([entry['name'] for entry in self.registry_file()['agents']], ['agent-1'])

    def test_a_symlinked_home_or_symlinked_board_agents_directory_is_never_followed(self):
        second = self.new_agent()
        self.arun(self.board.stop_terminal(second['id']))
        outside = self.make_outside('victim')
        (outside / 'sub').mkdir()
        (outside / 'sub' / 'x').write_text('x')
        snapshot = tree(outside)
        home = self.box.state / 'board-agents' / second['id']
        os.rename(home, self.box.root / 'moved-home')
        home.symlink_to(outside)
        self.assertEqual(self.refused(self.board.delete(second['id'])), 'delete_failed')
        self.assertEqual(tree(outside), snapshot)
        view = self.by_name(second['name'])
        self.assertEqual((view['state'], view['detail'], view['busy']), ('failed', 'delete_failed', False))
        self.assertIn(second['id'], [entry['id'] for entry in self.registry_file()['agents']])
        # starting it must not write through the symlink either
        self.assertIn(self.arun(self.board.start(second['id']))['state'], ('failed', 'starting', 'running'))
        self.settle()
        self.assertEqual(tree(outside), snapshot)
        self.assertEqual(self.by_name(second['name'])['detail'], 'home_unavailable')
        self.assertEqual(self.system_rows(), [])
        # the parent directory swapped for a symlink at run time
        home.unlink()
        os.rename(self.box.root / 'moved-home', home)
        parent = self.box.state / 'board-agents'
        real_parent = self.box.root / 'real-parent'
        os.rename(parent, real_parent)
        decoy = self.box.root / 'decoy'
        (decoy / second['id']).mkdir(parents=True)
        (decoy / second['id'] / 'marker').write_text('decoy home')
        parent.symlink_to(decoy)
        decoy_before = tree(decoy)
        self.assertEqual(self.refused(self.board.delete(second['id'])), 'delete_failed')
        self.assertEqual(tree(decoy), decoy_before)
        self.assertTrue((real_parent / second['id'] / 'omp-root').is_dir())
        parent.unlink()
        os.rename(real_parent, parent)
        self.arun(self.board.delete(second['id']))
        self.assertFalse(os.path.lexists(home))

    def test_deleting_the_migrated_agent_removes_only_board_agent_and_the_others_keep_their_homes(self):
        self.arun(self.board.open([self.agent1()['id']]))
        second = self.new_agent()
        self.settle()
        second_tree = tree(self.box.state / 'board-agents' / second['id'])
        agent_db = self.box.state / 'board-agent' / 'omp-root' / 'agent' / 'agent.db'
        self.assertTrue(agent_db.is_symlink())
        user_digest = digest(self.box.user_store)
        first = self.by_name('agent-1')
        self.arun(self.board.delete(first['id']))
        self.assertFalse(os.path.lexists(self.box.state / 'board-agent'))
        self.assertTrue(self.registry_path().is_file())
        self.assertEqual(tree(self.box.state / 'board-agents' / second['id']), second_tree)
        self.assertEqual(digest(self.box.user_store), user_digest)
        self.assertFalse(alive(first['pid']))
        self.assertEqual(self.internal_status(self.key_of(second)), 200)

    def test_a_running_agent_is_stopped_before_its_home_goes_and_its_key_is_dead_at_once(self):
        self.arun(self.board.open([self.agent1()['id']]))
        self.settle()
        first = self.by_name('agent-1')
        key = self.key_of(first)
        self.board.write(first['id'], b'bg\n')
        self.assertTrue(self.wait(lambda: any(row.get('mode') == 'bg' for row in self.box.records())))
        bg = [row for row in self.box.records() if row.get('mode') == 'bg'][-1]['bg']
        self.pids.add(bg)
        self.arun(self.board.delete(first['id']))
        self.assertFalse(alive(first['pid']))
        self.assertFalse(alive(bg))  # the whole session is ended, even a child ignoring HUP/TERM
        self.assertEqual(self.internal_status(key), 401)
        self.assertEqual(self.system_rows(), [])
        self.assertFalse(os.path.lexists(self.box.state / 'board-agent'))
        events = [detail for event, _, _, detail in self.audits if event in ('board_stop', 'board_terminal_close')]
        self.assertEqual([(detail.get('reason'), detail.get('memory_removed')) for detail in events],
                         [('delete', None), (None, True)])
        self.assertFalse(any('wbk_' in json.dumps(item) for item in self.audits))

    def test_delete_or_stop_while_the_isolation_check_is_still_running_leaves_no_process(self):
        self.box.env['FAKE_BOARD_RPC_SLEEP'] = '3'
        self.launcher = self.make_launcher(self.config)
        self.board = self.new_board()
        starting = self.arun(self.board.create_agent())
        self.assertEqual(starting['state'], 'starting')
        key_pid = starting['pid']
        self.pids.add(key_pid)
        started = time.monotonic()
        self.arun(self.board.delete(starting['id']))
        self.assertLess(time.monotonic() - started, 6)
        self.arun(self.board.settled())
        self.assertFalse(alive(key_pid))
        self.assertTrue(self.wait(lambda: not any(alive(row['pid']) for row in self.box.records()), 8))
        self.assertEqual(self.system_rows(), [])
        leftovers = tree(self.box.state / 'board-agents' / starting['id']) if os.path.lexists(
            self.box.state / 'board-agents' / starting['id']) else []
        self.assertEqual(leftovers, [], 'a deleted agent\'s home came back (the late isolation-check event line?)')
        second = self.arun(self.board.create_agent())
        self.pids.add(second['pid'])
        self.assertEqual(self.arun(self.board.stop_terminal(second['id']))['state'], 'stopped')
        self.arun(self.board.settled())
        self.assertEqual(self.by_name(second['name'])['state'], 'stopped')  # the late check never revives it
        self.assertTrue(self.wait(lambda: not any(alive(row['pid']) for row in self.box.records()), 8))

    def test_a_locked_subdirectory_is_a_partial_delete_that_a_second_delete_finishes(self):
        second = self.new_agent()
        outside = self.make_outside('victim2')
        snapshot = tree(outside)
        home = self.box.state / 'board-agents' / second['id']
        locked = home / 'omp-root' / 'locked'
        locked.mkdir()
        (locked / 'inside').write_text('x')
        (home / 'omp-root' / 'link').symlink_to(outside)
        os.chmod(locked, 0o000)
        self.addCleanup(lambda: os.path.isdir(locked) and os.chmod(locked, 0o700))
        self.assertEqual(self.refused(self.board.delete(second['id'])), 'delete_failed')
        view = self.by_name(second['name'])
        self.assertEqual((view['state'], view['detail'], view['busy']), ('failed', 'delete_failed', False))
        self.assertIn('delete', view['message'].lower())
        self.assertEqual(tree(outside), snapshot)
        self.assertIn(second['id'], [entry['id'] for entry in self.registry_file()['agents']])
        self.assertFalse(alive(second['pid']))
        os.chmod(locked, 0o700)
        self.arun(self.board.delete(second['id']))
        self.assertFalse(os.path.lexists(home))
        self.assertEqual(tree(outside), snapshot)
        self.assertEqual([view['name'] for view in self.views('agent')], ['agent-1'])


# --- (11) shells ---------------------------------------------------------------------------------------

class ShellProcessTests(SetCase):
    grace = 1.0

    def child_of(self, shell: dict, command: bytes) -> int:
        self.board.write(shell['id'], command + b'\n')
        self.board.write(shell['id'], b'echo CHILD=$!\n')
        self.assertTrue(self.wait(lambda: re.search(rb'CHILD=(\d+)', self.board.replay(shell['id']))), 'no child pid')
        pid = int(re.search(rb'CHILD=(\d+)', self.board.replay(shell['id'])).group(1))
        self.pids.add(pid)
        return pid

    def test_a_shell_is_its_own_session_and_closing_it_ends_every_member_even_ones_that_ignore_signals(self):
        shell = self.arun(self.board.create_shell())
        self.settle()
        pid = shell['pid']
        # the forked child becomes its own session leader a moment after fork (os.login_tty): wait, do not race it
        self.assertTrue(self.wait(lambda: os.getsid(pid) == pid, 15), (os.getsid(pid), os.getpgid(pid)))
        self.assertEqual((os.getsid(pid), os.getpgid(pid), int(Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[1])),
                         (pid, pid, os.getpid()))
        self.assertNotIn(b'WB_API_KEY=', Path(f'/proc/{pid}/environ').read_bytes())
        self.assertNotIn(b'WB_HUB_URL=', Path(f'/proc/{pid}/environ').read_bytes())
        self.board.write(shell['id'], b"trap '' HUP TERM\n")
        plain = self.child_of(shell, b'sleep 300 &')
        stubborn = self.child_of(shell, b'(trap "" HUP TERM; exec sleep 300) &')
        self.assertTrue(alive(plain) and alive(stubborn))
        seen = []
        self.board.watch(lambda terminal_id, event: seen.append((terminal_id, event)))
        started = time.monotonic()
        self.arun(self.board.close(shell['id']))
        self.assertGreater(time.monotonic() - started, 0.3)
        self.assertFalse(alive(pid))
        self.assertFalse(alive(plain))
        self.assertFalse(alive(stubborn))
        self.assertIn((shell['id'], 'gone'), seen)
        self.assertEqual(self.views('shell'), [])
        self.assertEqual(self.refused(self.board.close(shell['id'])), 'unknown_terminal')
        self.assertEqual(self.refused(self.board.start(shell['id'])), 'unknown_terminal')
        closed = [detail for event, _, _, detail in self.audits if event == 'board_terminal_close']
        self.assertEqual(closed, [{'terminal_id': shell['id'], 'kind': 'shell', 'name': 'shell-1'}])

    def test_rename_does_not_touch_the_process_and_a_restart_gets_a_new_process_under_the_same_id(self):
        shell = self.arun(self.board.create_shell())
        self.settle()
        self.arun(self.board.rename(shell['id'], '데이터 준비'))
        self.assertEqual((self.by_name('데이터 준비')['pid'], self.by_name('데이터 준비')['state']), (shell['pid'], 'running'))
        child = self.child_of(shell, b'sleep 300 &')
        restarted = self.arun(self.board.restart(shell['id']))
        self.pids.add(restarted['pid'])
        self.assertNotEqual(restarted['pid'], shell['pid'])
        self.assertEqual((restarted['id'], restarted['name'], restarted['state']), (shell['id'], '데이터 준비', 'running'))
        self.assertFalse(alive(shell['pid']))
        self.assertFalse(alive(child))
        self.assertEqual(self.refused(self.board.start(shell['id'])), 'bad_state')  # a running shell cannot start
        self.assertEqual(self.refused(self.board.stop_terminal(shell['id'])), 'wrong_kind')
        self.assertEqual(self.refused(self.board.delete(shell['id'])), 'wrong_kind')
        self.assertEqual(self.refused(self.board.restart('s-ffffffffffff')), 'unknown_terminal')

    def test_a_shell_that_exits_by_itself_stays_listed_as_exited_and_can_restart_or_close(self):
        shell = self.arun(self.board.create_shell())
        self.settle()
        self.board.write(shell['id'], b'exit 7\n')
        self.assertTrue(self.wait(lambda: self.by_name('shell-1')['state'] == 'exited'))
        view = self.by_name('shell-1')
        self.assertEqual(view['exit_status'], 7)
        started = self.arun(self.board.start(shell['id']))
        self.pids.add(started['pid'])
        self.assertEqual(started['state'], 'running')
        self.board.write(shell['id'], b'exit 0\n')
        self.assertTrue(self.wait(lambda: self.by_name('shell-1')['state'] == 'exited'))
        self.arun(self.board.close(shell['id']))
        self.assertEqual(self.views('shell'), [])

    def test_concurrent_restarts_of_one_terminal_run_once_and_leave_one_process(self):
        shell = self.arun(self.board.create_shell())
        self.settle()
        self.pids.add(shell['pid'])

        async def race():
            return await asyncio.gather(self.board.restart(shell['id']), self.board.restart(shell['id']),
                                        self.board.close(shell['id']), return_exceptions=True)
        results = self.arun(race())
        self.assertEqual(sum(isinstance(item, BoardError) and item.reason == 'terminal_busy' for item in results), 2,
                         results)
        self.settle()
        for item in results:
            if isinstance(item, dict):
                self.pids.add(item['pid'])
        self.assertFalse(alive(shell['pid']))

    def test_hub_stop_ends_shells_and_their_children_and_audits_them_as_the_hub(self):
        shell = self.arun(self.board.create_shell())
        self.settle()
        child = self.child_of(shell, b'sleep 300 &')
        self.arun(self.board.stop())
        self.assertFalse(alive(shell['pid']) or alive(child))
        stops = [(actor, detail['reason']) for event, actor, _, detail in self.audits if event == 'board_stop']
        self.assertEqual(stops, [('hub', 'hub_stop')])


class FirstOpenTests(SetCase):
    def test_the_first_open_of_a_run_makes_exactly_one_shell_even_when_racing_and_never_again_after_a_close(self):
        async def race():
            return await asyncio.gather(self.board.open([]), self.board.open([]), self.board.open([self.agent1()['id']]))
        self.arun(race())
        self.settle()
        self.assertEqual([view['name'] for view in self.views('shell')], ['shell-1'])
        self.assertEqual(len(self.views('agent')), 1)
        self.assertEqual(sum(1 for event, _, _, d in self.audits if event == 'board_terminal_open'), 1)
        starts = [d for event, _, _, d in self.audits if event == 'board_start']
        self.assertEqual(sorted((d['kind'], d['reason']) for d in starts), [('agent', 'first_open'), ('shell', 'first_open')])
        self.arun(self.board.close(self.views('shell')[0]['id']))
        self.arun(self.board.open([self.agent1()['id']]))  # shell_made stays true: no second automatic shell
        self.assertEqual(self.views('shell'), [])
        # a duplicate or unknown id in visible starts the agent once and ignores the rest
        self.arun(self.board.open([self.agent1()['id']] * 3 + ['a-ffffffffffff', 5, None]))
        self.settle()
        self.assertEqual(len([r for r in self.tty_records()]), 1)

    def test_a_shell_made_before_the_first_open_counts_as_the_runs_shell(self):
        self.arun(self.board.create_shell())
        self.arun(self.board.close(self.views('shell')[0]['id']))
        self.arun(self.board.open([]))
        self.assertEqual(self.views('shell'), [])


# --- audit -------------------------------------------------------------------------------------------

class AuditTests(SetCase):
    def test_every_action_leaves_its_audit_row_with_the_spec_detail_and_no_secret(self):
        self.arun(self.board.open([self.agent1()['id']]))  # first open: shell-1 and agent-1
        self.settle()
        second = self.new_agent()
        shell = self.by_name('shell-1')
        self.arun(self.board.rename(second['id'], '전처리'))
        self.arun(self.board.rename(shell['id'], 'logs'))
        self.arun(self.board.set_options(agent_limit=5))
        self.arun(self.board.stop_terminal(second['id']))
        self.arun(self.board.start(second['id']))
        self.arun(self.board.restart(second['id']))
        self.settle()
        self.arun(self.board.close(shell['id']))
        self.arun(self.board.delete(second['id']))
        self.arun(self.board.stop())
        rows = [(event, actor, detail) for event, actor, _, detail in self.audits]
        events = [event for event, _, _ in rows]
        for needed in ('board_terminal_open', 'board_start', 'board_rename', 'board_options', 'board_stop',
                       'board_restart', 'board_terminal_close'):
            self.assertIn(needed, events)
        reasons = [(detail['kind'], detail['reason']) for event, _, detail in rows if event == 'board_terminal_open']
        self.assertEqual(reasons, [('shell', 'first_open'), ('agent', 'create')])
        renames = [detail for event, _, detail in rows if event == 'board_rename']
        self.assertEqual(renames, [{'terminal_id': second['id'], 'kind': 'agent', 'old': 'agent-2', 'new': '전처리'},
                                   {'terminal_id': shell['id'], 'kind': 'shell', 'old': 'shell-1', 'new': 'logs'}])
        self.assertEqual([detail for event, _, detail in rows if event == 'board_options'],
                         [{'before': {'agent_limit': 4, 'shell_limit': 10}, 'after': {'agent_limit': 5, 'shell_limit': 10}}])
        closes = [(detail['kind'], detail['name'], detail.get('memory_removed')) for event, _, detail in rows
                  if event == 'board_terminal_close']
        self.assertEqual(closes, [('shell', 'logs', None), ('agent', '전처리', True)])
        for event, actor, detail in rows:
            if event.startswith('board_'):
                self.assertIn(actor, ('owner', 'hub'))
        self.assertNotIn('terminal_takeover', events)
        blob = json.dumps(self.audits, ensure_ascii=False)
        self.assertNotIn('wbk_', blob)
        for record in self.box.records():
            if record['env'].get('WB_API_KEY'):
                self.assertNotIn(record['env']['WB_API_KEY'], blob)


# ======================================================================================================
# S1: the session API's access rules
# ======================================================================================================

class SessionApiSecurityTests(api_tests.BoardApiCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.key = self.services.api_keys.issue('someone', 'editor', {'all': True}, None, now=time.time()).raw

    async def prepare(self) -> tuple[dict, dict]:
        agent = (await self.call('POST', '/api/board/agents', {})).json_body['terminal']
        shell = (await self.call('POST', '/api/board/shells', {})).json_body['terminal']
        await self.board.settled()
        return agent, shell

    def snapshot(self) -> tuple:
        path = Path(self.launcher.state_dir) / 'board-agents' / 'registry.json'
        return (json.dumps(self.board.views(), sort_keys=True), self.board.options(), path.read_bytes(),
                len(self.launcher.agent_starts), self.launcher.shells)

    def all_routes(self, agent: dict, shell: dict) -> list:
        base = '/api/board/terminals/'
        return [('POST', '/api/board/open', {'visible': [agent['id']]}), ('POST', '/api/board/agents', {}),
                ('POST', '/api/board/shells', {}), ('POST', base + agent['id'] + '/start', {}),
                ('POST', base + agent['id'] + '/stop', {'confirm': True}),
                ('POST', base + agent['id'] + '/restart', {'confirm': True}),
                ('POST', base + shell['id'] + '/close', {'confirm': True}),
                ('POST', base + agent['id'] + '/delete', {'confirm': True}),
                ('POST', base + agent['id'] + '/rename', {'name': 'hijacked'}),
                ('POST', '/api/board/options', {'agent_limit': 8})]

    async def assert_refused_untouched(self, label: str, status: int, *, query: str = '', **kwargs):
        agent, shell = self.agent, self.shell
        before = self.snapshot()
        for method, path, body in self.all_routes(agent, shell):
            response = await self.call(method, path + query, body, **kwargs)
            self.assertEqual(response.status, status, f'{label} {path}: {response.body_text[:200]}')
            self.assertNotIn(agent['id'], response.body_text.replace(path, ''))
        self.assertEqual(self.snapshot(), before, label)

    async def test_no_session_no_csrf_wrong_csrf_bearer_and_foreign_origin_change_nothing_on_any_route(self):
        self.agent, self.shell = await self.prepare()
        bearer = {'Authorization': f'Bearer {self.key}'}
        await self.assert_refused_untouched('no cookie', 401, cookies={}, csrf=None)
        await self.assert_refused_untouched('no cookie, csrf header', 401, cookies={}, csrf='x' * 43)
        await self.assert_refused_untouched('bearer', 401, cookies={}, csrf=None, headers=bearer)
        await self.assert_refused_untouched('garbage cookie', 401, cookies={'wb_hub_session': 'nope'}, csrf='x' * 43)
        await self.assert_refused_untouched('no csrf', 403, csrf=None)
        await self.assert_refused_untouched('wrong csrf', 403, csrf='y' * len(self.session.csrf))
        await self.assert_refused_untouched('empty csrf', 403, csrf=None, headers={'X-CSRF-Token': ''})
        await self.assert_refused_untouched('csrf in query only', 403, csrf=None, query=f'?csrf={self.session.csrf}')
        await self.assert_refused_untouched('bearer plus cookie, no csrf', 403, csrf=None, headers=bearer)
        await self.assert_refused_untouched('foreign origin', 403, headers={'Origin': 'http://evil.example'})
        await self.assert_refused_untouched('null origin', 403, headers={'Origin': 'null'})
        await self.assert_refused_untouched('same host other port', 403,
                                            headers={'Origin': f'http://127.0.0.1:{self.port + 1}'})
        before = self.snapshot()
        for referer, expected in (('http://evil.example/board', 403), (self.origin + '/board', 200)):
            async with self.http.post(self.base + '/api/board/options', data=json.dumps({'agent_limit': 5}),
                                      headers={'Host': self.host(), 'Referer': referer, 'X-CSRF-Token': self.session.csrf,
                                               'Content-Type': 'application/json',
                                               'Cookie': f'wb_hub_session={self.session.token}'}) as response:
                self.assertEqual(response.status, expected, referer)  # no Origin: the Referer decides
        self.assertEqual(self.board.options()['agent_limit'], 5)
        self.assertNotEqual(self.snapshot(), before)

    async def test_reads_need_a_session_and_leak_no_names_and_no_cross_origin_header(self):
        agent, shell = await self.prepare()
        await self.call('POST', f"/api/board/terminals/{agent['id']}/rename", {'name': 'secret-agent-name'})
        for label, kwargs in (('no cookie', {'cookies': {}}), ('bearer', {'cookies': {}, 'headers': {
                'Authorization': f'Bearer {self.key}'}}), ('garbage', {'cookies': {'wb_hub_session': 'x'}})):
            response = await self.call('GET', '/api/board', **kwargs)
            self.assertEqual(response.status, 401, label)
            self.assertNotIn('secret-agent-name', response.body_text)
            self.assertNotIn(agent['id'], response.body_text)
        ok = await self.call('GET', '/api/board', headers={'Origin': 'http://evil.example'})
        self.assertNotIn('Access-Control-Allow-Origin', ok.headers)
        self.assertEqual(ok.headers['Cache-Control'], 'no-store')
        self.assertEqual(ok.headers['Content-Type'].split(';')[0], 'application/json')
        # a GET never starts or creates anything
        before = self.snapshot()
        for _ in range(3):
            await self.call('GET', '/api/board')
        self.assertEqual(self.snapshot(), before)
        # other methods on the mutating routes are not accepted
        for method in ('GET', 'PUT', 'DELETE', 'PATCH'):
            response = await self.call(method, '/api/board/agents', {} if method != 'GET' else None)
            self.assertIn(response.status, (403, 405), (method, response.status))
        self.assertEqual(self.snapshot(), before)

    async def test_a_session_that_ended_or_expired_is_refused_and_a_second_session_sees_the_same_state(self):
        agent, shell = await self.prepare()
        other = self.services.sessions.create('127.0.0.1', 'second', time.time(), generation=self.generation)
        second_cookies = {'wb_hub_session': other.token}
        fields = {'cookies': second_cookies, 'csrf': None, 'headers': {'X-CSRF-Token': other.csrf}}
        renamed = await self.call('POST', f"/api/board/terminals/{agent['id']}/rename", {'name': ' 전처리 '})
        self.assertEqual(renamed.json_body['terminal']['name'], '전처리')
        seen = await self.call('GET', '/api/board', cookies=second_cookies)
        self.assertEqual([view['name'] for view in seen.json_body['terminals']], ['agent-1', '전처리', 'shell-1'])
        saved = await self.call('POST', '/api/board/options', {'agent_limit': 6, 'shell_limit': 12}, **fields)
        self.assertEqual(saved.status, 200, saved.body_text)
        mine = await self.call('GET', '/api/board')
        self.assertEqual(mine.json_body['options']['agent_limit'], 6)
        self.assertEqual(mine.json_body['options']['shell_limit'], 12)
        # the second session cannot use the first session's CSRF value
        wrong = await self.call('POST', '/api/board/options', {'agent_limit': 7}, cookies=second_cookies)
        self.assertEqual(wrong.status, 403)
        self.assertEqual(self.board.options()['agent_limit'], 6)
        # a deleted session (logout) and an expired one are refused everywhere
        self.services.sessions.delete(other.token)
        gone = await self.call('GET', '/api/board', cookies=second_cookies)
        self.assertEqual(gone.status, 401)
        posted = await self.call('POST', '/api/board/options', {'agent_limit': 8}, **fields)
        self.assertEqual(posted.status, 401)
        clock = self.now[0]
        self.now[0] = time.time() + 48 * 3600
        expired = await self.call('POST', '/api/board/agents', {})
        self.assertEqual(expired.status, 401)
        self.assertEqual((await self.call('GET', '/api/board')).status, 401)
        self.now[0] = clock
        self.assertEqual(len([v for v in self.board.views() if v['kind'] == 'agent']), 2)

    async def test_hostile_names_are_data_only_and_pages_never_carry_them_raw(self):
        agent, shell = await self.prepare()
        names = ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "'; DROP TABLE x;--", '{{7*7}}',
                 '${jndi:x}', '&lt;b&gt;', '</script><script>1</script>', '\\u003cb\\u003e']
        for name in names:
            response = await self.call('POST', f"/api/board/terminals/{agent['id']}/rename", {'name': name})
            self.assertEqual(response.status, 200, (name, response.body_text))
            listing = await self.call('GET', '/api/board')
            self.assertEqual(listing.headers['Content-Type'].split(';')[0], 'application/json')
            self.assertEqual(listing.headers.get('X-Content-Type-Options'), 'nosniff')
            self.assertEqual([t['name'] for t in json.loads(listing.body_text)['terminals'] if t['id'] == agent['id']],
                             [name])
            for path in ('/board', '/dashboard'):
                page = await self.get(path, cookies=self.cookies)
                self.assertEqual(page.status, 200, path)
                self.assertNotIn('<script>alert', page.body_text)
                self.assertNotIn('<img src=x', page.body_text)
                self.assertNotIn(name, page.body_text)

    async def test_requests_with_odd_bodies_and_content_types_are_bad_input_and_change_nothing(self):
        agent, shell = await self.prepare()
        before = self.snapshot()
        path = f"/api/board/terminals/{agent['id']}/rename"
        for label, kwargs in (
                ('null', {'raw': 'null'}), ('array', {'raw': '[]'}), ('string', {'raw': '"x"'}), ('empty', {'raw': ''}),
                ('duplicate unknown', {'raw': '{"name": "a", "name": "b", "x": 1}'}),
                ('nan', {'raw': '{"name": NaN}'}), ('big', {'raw': json.dumps({'name': 'x' * 5000})}),
                ('form', {'raw': 'name=x', 'headers': {'Content-Type': 'application/x-www-form-urlencoded'}}),
                ('text', {'raw': '{"name": "x"}', 'headers': {'Content-Type': 'text/plain'}}),
                ('depth', {'raw': '[' * 100000})):
            with self.subTest(label):
                response = await self.call('POST', path, **kwargs)
                self.assertIn(response.status, (400, 413), response.body_text[:200])
                self.assertNotIn('Traceback', response.body_text)
        async with self.http.post(self.base + path, data=b'{"name": "\xff"}', headers={
                'Host': self.host(), 'Origin': self.origin, 'Content-Type': 'application/json',
                'X-CSRF-Token': self.session.csrf, 'Cookie': f'wb_hub_session={self.session.token}'}) as response:
            self.assertEqual(response.status, 400)  # invalid UTF-8
        self.assertEqual(self.snapshot(), before)

    async def test_a_name_json_cannot_store_is_a_clean_name_invalid_not_a_500(self):
        agent, shell = await self.prepare()
        before = self.snapshot()
        for raw in ('{"name": "\\ud800"}', '{"name": "ab\\udfffcd"}'):
            response = await self.call('POST', f"/api/board/terminals/{agent['id']}/rename", raw=raw)
            self.assertEqual((response.status, (response.json_body or {}).get('reason')), (400, 'name_invalid'),
                             response.body_text[:200])
        self.assertEqual(self.snapshot(), before)

    async def test_a_terminal_id_that_is_not_an_id_is_unknown_not_a_crash(self):
        await self.prepare()
        for terminal_id in ('..%2F..%2Fetc', '%00', 'a-' + 'f' * 12 + '%20', 'A-000000000001', 'x' * 500, '%E2%80%AE'):
            response = await self.call('POST', f'/api/board/terminals/{terminal_id}/start', {})
            self.assertIn(response.status, (400, 404), (terminal_id, response.status))
            self.assertNotIn('Traceback', response.body_text)


# ======================================================================================================
# S3: a real Hub with the fake Board OMP
# ======================================================================================================

class Hub3:
    """A WebSandbox Hub with the fake Board agent (like ``test_board_stop.BoardHub`` but with a ``prepare`` hook that
    runs before the Hub starts, for planted state)."""

    def __init__(self, case: unittest.TestCase, *, prepare=None, shell_script: str = 'exec sleep 600\n'):
        self.case = case
        self.box = box = WebSandbox(case)
        store = box.home / '.omp' / 'agent' / 'agent.db'
        store.parent.mkdir(parents=True)
        store.write_bytes(b'')
        self.store = store
        self.bin = box.base / 'bin'
        self.bin.mkdir()
        omp = self.bin / 'omp'
        omp.write_text(f'#!/bin/sh\nexec {sys.executable} {stop_tests.FAKE_OMP} "$@"\n')
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
        box.state.mkdir(parents=True, mode=0o700, exist_ok=True)
        if prepare is not None:
            prepare(self)
        self.start()

    def start(self):
        self.hub = self.box.start_hub()
        host, port = self.box.wait_listening(self.hub.pid)
        self.host, self.port = host, port
        self.browser = Browser(host, port)
        status, _, page = self.browser.request('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        status, _, _ = self.browser.request('POST', '/login', {'login_token': token, 'password': PASSWORD})
        self.case.assertEqual(status, 303, 'login')

    def restart_hub(self):
        self.case.assertEqual(self.box.stop_hub(self.hub), 0)
        self.start()

    def api(self, method: str, path: str, body: dict | None = None, *, csrf: bool = True,
            cookies: bool = True, headers: dict | None = None) -> tuple[int, dict | None]:
        sent = {'Origin': f'http://{self.host}:{self.port}', **(headers or {})}
        if cookies:
            sent['Cookie'] = '; '.join(f'{k}={v}' for k, v in self.browser.cookies.items())
        if csrf:
            _, _, page = self.browser.request('GET', '/settings')
            sent['X-CSRF-Token'] = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        data = None
        if body is not None:
            data = json.dumps(body)
            sent['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            text = response.read().decode('utf-8')
            try:
                return response.status, json.loads(text)
            except ValueError:
                return response.status, None
        finally:
            connection.close()

    def terminals(self) -> list:
        status, reply = self.api('GET', '/api/board')
        self.case.assertEqual(status, 200)
        return reply['terminals']

    def named(self, name: str) -> dict:
        found = [view for view in self.terminals() if view['name'] == name]
        self.case.assertEqual(len(found), 1, self.terminals())
        return found[0]

    def wait_state(self, name: str, state: str, timeout: float = 15) -> dict:
        self.case.assertTrue(wait_until(lambda: self.named(name)['state'] == state, timeout), self.terminals())
        return self.named(name)

    def records(self) -> list:
        try:
            return [json.loads(line) for line in self.record.read_text().splitlines() if line.strip()]
        except FileNotFoundError:
            return []

    def db(self) -> sqlite3.Connection:
        return sqlite3.connect(f'file:{self.box.state / "hub.sqlite3"}?mode=ro', uri=True)

    def system_rows(self) -> list:
        db = self.db()
        try:
            return sorted(db.execute('SELECT name, key_id FROM api_keys WHERE system = 1').fetchall())
        finally:
            db.close()

    def audit_rows(self) -> list:
        db = self.db()
        try:
            return db.execute('SELECT event, actor, client_addr, detail_json FROM audit ORDER BY id').fetchall()
        finally:
            db.close()

    def rest(self, base: str, key: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
        host, _, port = base.removeprefix('http://').partition(':')
        connection = http.client.HTTPConnection(host, int(port), timeout=20)
        try:
            headers = {'Authorization': f'Bearer {key}'}
            if body is not None:
                headers['Content-Type'] = 'application/json'
            connection.request(method, '/api/v1' + path, body=None if body is None else json.dumps(body),
                               headers=headers)
            response = connection.getresponse()
            text = response.read().decode('utf-8')
            try:
                return response.status, json.loads(text)
            except ValueError:
                return response.status, None
        finally:
            connection.close()

    def web_base(self) -> str:
        return f'http://{self.host}:{self.port}'


class RealHubTests(unittest.TestCase):
    def test_several_agents_and_shells_on_a_real_hub_keys_origin_audit_and_stop(self):
        def prepare(hub: Hub3):
            home = hub.box.home
            hub.root = hub.box.base / 'work' / 'alpha'
            hub.root.mkdir(parents=True)
            hub.root = hub.root.resolve()
            initialize_project(resolve_project_context(hub.root, hub.box.env), confirm=lambda _root: True,
                               interactive=True, environment=hub.box.env)
        hub = Hub3(self, prepare=prepare)
        project_id = json.loads((hub.root / '.wb' / 'project.json').read_text())['project_id']
        self.assertEqual(hub.box.cli('hub', 'add', str(hub.root)).returncode, 0)
        user_store = os.stat(hub.store)
        user_digest = digest(hub.store)
        first = hub.named('agent-1')
        status, reply = hub.api('POST', '/api/board/open', {'visible': [first['id']]})
        self.assertEqual(status, 200, reply)
        status, reply = hub.api('POST', '/api/board/agents', {})
        self.assertEqual(status, 201, reply)
        status, reply = hub.api('POST', '/api/board/shells', {})
        self.assertEqual(status, 201, reply)
        a1, a2 = hub.wait_state('agent-1', 'running'), hub.wait_state('agent-2', 'running')
        shells = [view for view in hub.terminals() if view['kind'] == 'shell']
        self.assertEqual([shell['name'] for shell in shells], ['shell-1', 'shell-2'])
        pids = [a1['pid'], a2['pid'], *[shell['pid'] for shell in shells]]
        self.assertEqual(len(set(pids)), 4)
        envs = {view['id']: environ_of(view['pid']) for view in (a1, a2)}
        keys = {agent_id: env['WB_API_KEY'] for agent_id, env in envs.items()}
        self.assertEqual(len(set(keys.values())), 2)
        rows = dict(hub.system_rows())
        self.assertEqual(sorted(rows), sorted(f'board-agent.{agent_id}' for agent_id in keys))
        for agent_id, key in keys.items():
            self.assertEqual(key.split('_')[1], rows[f'board-agent.{agent_id}'])
        for shell in shells:
            data = Path(f"/proc/{shell['pid']}/environ").read_bytes()
            self.assertNotIn(b'WB_API_KEY=', data)
            self.assertFalse(any(key.encode() in data for key in keys.values()))
        for agent_id, env in envs.items():
            others = [key for other, key in keys.items() if other != agent_id]
            self.assertFalse(any(key.encode() in Path(f"/proc/{view['pid']}/environ").read_bytes()
                                 for view in (a1, a2) if view['id'] == agent_id for key in others))
            self.assertNotIn(keys[agent_id].encode(), Path(f"/proc/{a1['pid']}/cmdline").read_bytes())
        internal = envs[a1['id']]['WB_HUB_URL']
        web = hub.web_base()
        for agent_id, key in keys.items():
            self.assertEqual(hub.rest(internal, key, 'GET', '/projects')[0], 200)
        # origin: each agent stamps its own name at submit time; the pages escape a hostile name
        hostile = '<img src=x onerror=alert(1)>'
        self.assertEqual(hub.api('POST', f"/api/board/terminals/{a1['id']}/rename", {'name': hostile})[0], 200)
        status, one = hub.rest(internal, keys[a1['id']], 'POST', '/requests',
                               {'project_id': project_id, 'title': 'agent one', 'body': 'b', 'idempotency_key': 'k1'})
        self.assertEqual((status, one['origin']['label']), (201, f'Board agent · {hostile}'))
        self.assertEqual(hub.api('POST', f"/api/board/terminals/{a1['id']}/rename", {'name': '전처리'})[0], 200)
        status, two = hub.rest(internal, keys[a2['id']], 'POST', '/requests',
                               {'project_id': project_id, 'title': 'agent two', 'body': 'b', 'idempotency_key': 'k2'})
        self.assertEqual(two['origin']['label'], 'Board agent · agent-2')
        for path in ('/requests', f"/requests/{one['request_id']}", f"/requests/{two['request_id']}"):
            status, _, page = hub.browser.request('GET', path)
            self.assertEqual(status, 200, path)
            self.assertNotIn('<img src=x', page)
            self.assertNotIn(hostile, page)
        status, _, page = hub.browser.request('GET', f"/requests/{one['request_id']}")
        self.assertIn('Board agent · &lt;img src=x onerror=alert(1)&gt;', page)
        db = sqlite3.connect(f'file:{hub.root / ".wb" / "inbox.sqlite3"}?mode=ro', uri=True)
        try:
            names = db.execute('SELECT title, origin_kind, origin_name FROM requests ORDER BY seq').fetchall()
        finally:
            db.close()
        self.assertEqual(names, [('agent one', 'board_agent', hostile), ('agent two', 'board_agent', 'agent-2')])
        # stop agent-2: its key is dead on the internal AND the web listener, agent-1's key still works
        status, _ = hub.api('POST', f"/api/board/terminals/{a2['id']}/stop", {'confirm': True})
        self.assertEqual(status, 200)
        self.assertFalse(alive(a2['pid']))
        for base in (internal, web):
            self.assertEqual(hub.rest(base, keys[a2['id']], 'GET', '/projects')[0], 401, base)
        self.assertEqual(hub.rest(internal, keys[a1['id']], 'GET', '/projects')[0], 200)
        self.assertEqual([name for name, _ in hub.system_rows()], [f"board-agent.{a1['id']}"])
        # a stopped agent is not revived by another page open; a delete removes its home
        status, reply = hub.api('POST', '/api/board/open', {'visible': [a2['id']]})
        self.assertEqual(status, 200)
        self.assertEqual(hub.named('agent-2')['state'], 'stopped')
        home2 = hub.box.state / 'board-agents' / a2['id']
        self.assertTrue(home2.is_dir())
        status, _ = hub.api('POST', f"/api/board/terminals/{a2['id']}/delete", {'confirm': True})
        self.assertEqual(status, 200)
        self.assertFalse(os.path.lexists(home2))
        self.assertTrue(os.path.lexists(hub.box.state / 'board-agent'))
        # the stop offer lists every live terminal by name; confirming ends all, the Hub exits 0
        refused = hub.box.cli('hub', 'stop')
        self.assertEqual(refused.returncode, 1, refused)
        for line in ('agent 전처리', 'shell shell-1', 'shell shell-2'):
            self.assertIn(line, refused.stdout)
        live = [a1['pid'], *[shell['pid'] for shell in shells]]
        child = None
        self.assertTrue(all(alive(pid) for pid in live))
        status = hub.box.cli('hub', 'status')
        self.assertIn('board_agent:', status.stdout)
        stopped = hub.box.cli('hub', 'stop', '--yes')
        self.assertEqual(stopped.returncode, 0, stopped)
        self.assertEqual(hub.hub.wait(timeout=20), 0)
        self.assertFalse(any(alive(pid) for pid in pids))
        self.assertEqual(hub.system_rows(), [])
        # audit rows: actors, addresses, details, and no secret anywhere the Hub wrote
        by_event = {}
        for event, actor, address, detail in hub.audit_rows():
            by_event.setdefault(event, []).append((actor, address, json.loads(detail)))
        for event in ('board_terminal_open', 'board_start', 'board_rename', 'board_stop', 'board_terminal_close'):
            self.assertIn(event, by_event)
        self.assertEqual({actor for actor, _, _ in by_event['board_rename']}, {'owner'})
        self.assertEqual({address for _, address, _ in by_event['board_rename']}, {'127.0.0.1'})
        self.assertEqual([detail['new'] for _, _, detail in by_event['board_rename']], [hostile, '전처리'])
        self.assertEqual([(d['kind'], d['reason']) for _, _, d in by_event['board_terminal_open']],
                         [('shell', 'first_open'), ('agent', 'create'), ('shell', 'create')])
        self.assertEqual([(d['name'], d['memory_removed']) for _, _, d in by_event['board_terminal_close']
                          if d['kind'] == 'agent'], [('agent-2', True)])
        hub_stops = [d for actor, _, d in by_event['board_stop'] if actor == 'hub']
        self.assertEqual(sorted(d['pid'] for d in hub_stops), sorted(live))
        self.assertNotIn('terminal_takeover', by_event)
        corpus = json.dumps(hub.audit_rows(), ensure_ascii=False) + (hub.box.base / 'hub-0.log').read_text()
        corpus += (hub.box.state / 'board-agents' / 'registry.json').read_text(encoding='utf-8')
        for home in (hub.box.state / 'board-agent' / 'omp-logs',):
            for path in home.rglob('*'):
                if path.is_file():
                    corpus += path.read_text(errors='replace')
        for key in keys.values():
            self.assertNotIn(key, corpus)
            self.assertNotIn(key.split('_')[2], corpus)
        after = os.stat(hub.store)
        self.assertEqual((after.st_ino, after.st_size, digest(hub.store)), (user_store.st_ino, user_store.st_size, user_digest))

    def test_a_hub_restart_keeps_agents_names_and_limits_loses_shells_and_the_first_open_makes_shell_1(self):
        hub = Hub3(self)
        first = hub.named('agent-1')
        self.assertEqual(hub.api('POST', '/api/board/open', {'visible': [first['id']]})[0], 200)
        hub.wait_state('agent-1', 'running')
        self.assertEqual(hub.api('POST', '/api/board/options', {'agent_limit': 6, 'shell_limit': 3})[0], 200)
        status, reply = hub.api('POST', '/api/board/agents', {})
        second = reply['terminal']
        self.assertEqual(hub.api('POST', f"/api/board/terminals/{second['id']}/rename", {'name': '전처리'})[0], 200)
        hub.wait_state('전처리', 'running')
        self.assertEqual(hub.api('POST', f"/api/board/terminals/{second['id']}/stop", {'confirm': True})[0], 200)
        self.assertEqual(hub.api('POST', '/api/board/shells', {})[0], 201)
        shell_pid = hub.named('shell-1')['pid']
        registry_bytes = (hub.box.state / 'board-agents' / 'registry.json').read_bytes()
        pids = [first['id'], second['id']]
        hub.restart_hub()
        self.assertFalse(alive(shell_pid))
        status, board = hub.api('GET', '/api/board')
        self.assertEqual([(v['id'], v['name'], v['state']) for v in board['terminals']],
                         [(first['id'], 'agent-1', 'not_started'), (second['id'], '전처리', 'not_started')])
        self.assertEqual(board['options']['agent_limit'], 6)
        self.assertEqual(board['options']['shell_limit'], 3)
        self.assertEqual(hub.system_rows(), [])
        self.assertEqual((hub.box.state / 'board-agents' / 'registry.json').read_bytes(), registry_bytes)
        status, reply = hub.api('POST', '/api/board/open', {'visible': [second['id']]})
        self.assertEqual(status, 200)
        names = [(v['kind'], v['name']) for v in reply['terminals']]
        self.assertEqual(names, [('agent', 'agent-1'), ('agent', '전처리'), ('shell', 'shell-1')])
        hub.wait_state('전처리', 'running')
        self.assertEqual(hub.named('agent-1')['state'], 'not_started')
        self.assertIn('--continue', [r for r in hub.records() if r['mode'] == 'tty'][-1]['argv'] if
                      (hub.box.state / 'board-agents' / second['id'] / 'omp-root' / 'agent' / 'sessions').exists() else ['--continue'])
        self.assertEqual(hub.api('POST', '/api/board/open', {'visible': [second['id']]})[0], 200)
        self.assertEqual(len([v for v in hub.terminals() if v['kind'] == 'shell']), 1)
        self.assertEqual(pids, [first['id'], second['id']])

    def test_a_pre_d1_state_migrates_on_a_real_hub_and_purges_old_keys_at_start(self):
        planted = {}

        def prepare(hub: Hub3):
            home = hub.box.state / 'board-agent'
            session = home / 'omp-root' / 'agent' / 'sessions' / '-home' / 'old.jsonl'
            session.parent.mkdir(parents=True)
            session.write_text('{"old": 1}\n')
            os.chmod(home, 0o700)
            planted['session'] = (session, os.stat(session).st_ino, session.read_bytes(), os.stat(home).st_ino)
            db = auth.HubDb.open(hub.box.state)
            try:
                store = auth.ApiKeyStore(db)
                legacy = store.rotate_system_key('x', now=time.time())
                db.write(lambda execute: execute("UPDATE api_keys SET name = 'board-agent' WHERE key_id = ?",
                                                 (legacy.key_id,)))
                planted['legacy'] = legacy.raw
                stale = store.rotate_system_key('a-00000000beef', now=time.time())
                planted['stale'] = stale.raw
                store.issue('people', 'editor', {'all': True}, None, now=time.time())
            finally:
                db.close()
        hub = Hub3(self, prepare=prepare)
        self.assertEqual(hub.system_rows(), [])  # purged when the Hub made its Board, before any open
        db = hub.db()
        try:
            self.assertEqual(db.execute("SELECT name FROM api_keys WHERE system = 0").fetchall(), [('people',)])
        finally:
            db.close()
        registry_path = hub.box.state / 'board-agents' / 'registry.json'
        data = json.loads(registry_path.read_text())
        self.assertEqual([(a['name'], a['home']) for a in data['agents']], [('agent-1', 'board-agent')])
        self.assertEqual(stat.S_IMODE(os.stat(registry_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(registry_path.parent).st_mode), 0o700)
        first = hub.named('agent-1')
        self.assertEqual((first['state'], first['pid']), ('not_started', None))
        self.assertEqual(hub.api('POST', '/api/board/open', {'visible': [first['id']]})[0], 200)
        running = hub.wait_state('agent-1', 'running')
        record = [row for row in hub.records() if row['mode'] == 'tty'][-1]
        self.assertIn('--continue', record['argv'])
        self.assertEqual(record['env']['PI_CODING_AGENT_DIR'],
                         str(hub.box.state / 'board-agent' / 'omp-root' / 'agent'))
        internal = record['env']['WB_HUB_URL']
        for old in (planted['legacy'], planted['stale']):
            self.assertEqual(hub.rest(internal, old, 'GET', '/projects')[0], 401)
        self.assertEqual(hub.rest(internal, environ_of(running['pid'])['WB_API_KEY'], 'GET', '/projects')[0], 200)
        session, inode, content, home_inode = planted['session']
        self.assertEqual((os.stat(session).st_ino, session.read_bytes(), os.stat(session.parents[4]).st_ino),
                         (inode, content, home_inode))
        self.assertEqual(sorted(os.listdir(registry_path.parent)), ['registry.json'])
        self.assertEqual(hub.box.stop_hub(hub.hub), 0)

    def test_an_invalid_registry_leaves_the_hub_running_and_the_file_untouched(self):
        raw = b'{"schema_version": 1, "options": {"agent_limit": 4'
        planted = {}

        def prepare(hub: Hub3):
            directory = hub.box.state / 'board-agents'
            directory.mkdir(mode=0o700)
            path = directory / 'registry.json'
            path.write_bytes(raw)
            os.chmod(path, 0o600)
            planted['before'] = tree(directory)
        hub = Hub3(self, prepare=prepare)
        status, board = hub.api('GET', '/api/board')
        self.assertEqual(status, 200)
        self.assertEqual(board['board']['state'], 'registry_invalid')
        self.assertIn('registry.json', board['board']['message'])
        self.assertEqual(board['terminals'], [])
        for method, path, body in (('POST', '/api/board/open', {'visible': []}), ('POST', '/api/board/agents', {}),
                                   ('POST', '/api/board/shells', {}), ('POST', '/api/board/options', {'agent_limit': 5})):
            status, reply = hub.api(method, path, body)
            self.assertEqual((status, reply['reason']), (503, 'registry_invalid'), path)
        for page in ('/dashboard', '/requests', '/settings'):
            self.assertEqual(hub.browser.request('GET', page)[0], 200, page)  # the rest of the Hub works
        status, _, text = hub.browser.request('GET', '/board')
        self.assertEqual(status, 503)
        self.assertIn('registry', text)
        status = hub.box.status()
        self.assertEqual(status['board_agent']['state'], 'registry_invalid')
        text = hub.box.cli('hub', 'status')
        self.assertEqual(text.returncode, 0, text)
        self.assertIn('registry_invalid', text.stdout)
        self.assertEqual(hub.records(), [])
        self.assertFalse(os.path.lexists(hub.box.state / 'board-agent'))
        stopped = hub.box.cli('hub', 'stop')  # nothing live: no question
        self.assertEqual(stopped.returncode, 0, stopped)
        self.assertEqual(hub.hub.wait(timeout=20), 0)
        self.assertEqual(tree(hub.box.state / 'board-agents'), planted['before'])
        self.assertEqual((hub.box.state / 'board-agents' / 'registry.json').read_bytes(), raw)

    def test_shell_children_end_with_the_hub_and_a_closed_shell_leaves_no_group_member(self):
        hub = Hub3(self, shell_script='sleep 300 &\necho $! > "$HOME/child.pid"\ntrap "" HUP TERM\nwait\nexec sleep 600\n')
        status, _ = hub.api('POST', '/api/board/shells', {})
        self.assertEqual(status, 201)
        first = hub.named('shell-1')
        child_file = hub.box.home / 'child.pid'
        self.assertTrue(wait_until(child_file.exists, 10))
        self.assertTrue(wait_until(lambda: child_file.read_text().strip().isdigit(), 5))
        child = int(child_file.read_text())
        self.assertTrue(alive(child))
        started = time.monotonic()
        status, _ = hub.api('POST', f"/api/board/terminals/{first['id']}/close", {'confirm': True})
        self.assertEqual(status, 200)
        self.assertFalse(alive(first['pid']))
        self.assertFalse(alive(child))
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual([v for v in hub.terminals() if v['kind'] == 'shell'], [])
        status, reply = hub.api('POST', '/api/board/shells', {})
        second = reply['terminal']
        self.assertEqual(second['name'], 'shell-1')
        self.assertNotEqual(second['id'], first['id'])
        # no browser at all: the shell lives on; the Hub's stop then ends it with its children
        self.assertTrue(wait_until(lambda: child_file.read_text().strip().isdigit(), 5))
        time.sleep(0.5)
        self.assertTrue(alive(second['pid']))
        leader = second['pid']
        self.assertEqual(hub.box.cli('hub', 'stop', '--yes').returncode, 0)
        self.assertEqual(hub.hub.wait(timeout=20), 0)
        self.assertFalse(alive(leader))


if __name__ == '__main__':
    unittest.main()
