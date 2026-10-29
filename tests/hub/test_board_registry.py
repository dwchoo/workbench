"""WBX-D1 behaviours 1-3: terminal names, the terminal registry file and the migration of the single Board agent
(web-board-ux SPEC s4 "데이터 모델: terminal registry", "이행과 되돌리기"; seam: the Board modules over a temp
``<hub state>``, like ``test_board_set.py``/``test_board_launch.py``).

Expected values: the SPEC name rule (strip, 1..40 code points, no Cc/Cf/Zl/Zp, unique per kind by casefold), the
default name rule (smallest unused ``<kind>-N``), the registry JSON shape and its file mode/owner/symlink rules, and
the file bytes read back from disk.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest

from workbench.hub.board import registry


class NameRuleTests(unittest.TestCase):
    def test_a_name_is_trimmed_and_kept_when_it_has_1_to_40_code_points(self):
        self.assertEqual(registry.check_name('  전처리  ', []), '전처리')
        self.assertEqual(registry.check_name('a', []), 'a')
        self.assertEqual(registry.check_name('가' * 40, []), '가' * 40)
        self.assertEqual(registry.check_name('agent 1 · 로그 보기', []), 'agent 1 · 로그 보기')

    def test_empty_too_long_non_string_or_control_character_names_are_name_invalid(self):
        for value in ('', '   ', '가' * 41, 'x' * 41, None, 7, ['a'], 'a\nb', 'a\tb', 'a\x00', 'a​b',
                      'a b', 'a b', '\x1b[31mred', 'a‮b', 'a\x7f'):
            with self.subTest(value=value), self.assertRaises(registry.NameRefused) as caught:
                registry.check_name(value, [])
            self.assertEqual(caught.exception.reason, 'name_invalid')

    def test_the_legacy_board_agent_name_is_reserved_in_any_case_and_spacing(self):
        """Orchestrator decision 2026-10-29: ``board-agent`` (the pre-Track-D author name) is refused as name_invalid."""
        for value in ('board-agent', ' Board-Agent ', 'BOARD-AGENT'):
            with self.subTest(value=value), self.assertRaises(registry.NameRefused) as caught:
                registry.check_name(value, [])
            self.assertEqual(caught.exception.reason, 'name_invalid')
        self.assertEqual(registry.check_name('board-agent-2', []), 'board-agent-2')

    def test_a_name_another_terminal_of_the_kind_has_in_any_case_is_name_taken(self):
        for value, others in (('agent-1', ['Agent-1']), ('ß', ['SS']), (' Build ', ['build']), ('전처리', ['전처리'])):
            with self.subTest(value=value), self.assertRaises(registry.NameRefused) as caught:
                registry.check_name(value, others)
            self.assertEqual(caught.exception.reason, 'name_taken')
        self.assertEqual(registry.check_name('agent-2', ['agent-1']), 'agent-2')

    def test_the_default_name_is_the_smallest_unused_kind_number(self):
        self.assertEqual(registry.default_name('agent', []), 'agent-1')
        self.assertEqual(registry.default_name('agent', ['agent-1', 'agent-3']), 'agent-2')
        self.assertEqual(registry.default_name('shell', ['SHELL-1', 'shell-2']), 'shell-3')
        # A renamed number is free again; names that only look like numbers do not count.
        self.assertEqual(registry.default_name('agent', ['전처리', 'agent-2']), 'agent-1')
        self.assertEqual(registry.default_name('agent', ['agent-01', 'agent-1x', 'agent-0', 'agent-1']), 'agent-2')
        self.assertEqual(registry.default_name('shell', ['shell-1', 'shell-2', 'shell-3', 'shell-4']), 'shell-5')


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='wbreg-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state = self.root / 'hub'
        self.state.mkdir(mode=0o700)
        self.directory = self.state / 'board-agents'
        self.path = self.directory / 'registry.json'

    def write_raw(self, value, mode: int = 0o600) -> None:
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.path.write_text(value if isinstance(value, str) else json.dumps(value))
        os.chmod(self.path, mode)

    def valid(self, **change) -> dict:
        value = {'schema_version': 1, 'options': {'agent_limit': 4, 'shell_limit': 10},
                 'agents': [{'id': 'a-0123456789ab', 'name': 'agent-1', 'home': 'board-agent', 'created_at': 1.5}]}
        value.update(change)
        return value


class RegistryFileTests(RegistryCase):
    def test_a_saved_registry_is_a_0600_file_in_a_0700_directory_and_reads_back_the_same(self):
        old = os.umask(0)
        try:
            data = registry.Registry(options=registry.Options(3, 12), agents=[
                registry.AgentEntry('a-0123456789ab', 'agent-1', 'board-agent', 10.0),
                registry.AgentEntry('a-ba9876543210', '전처리', 'board-agents/a-ba9876543210', 11.0)])
            registry.save(self.state, data)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), {
            'schema_version': 1, 'options': {'agent_limit': 3, 'shell_limit': 12},
            'agents': [{'id': 'a-0123456789ab', 'name': 'agent-1', 'home': 'board-agent', 'created_at': 10.0},
                       {'id': 'a-ba9876543210', 'name': '전처리', 'home': 'board-agents/a-ba9876543210',
                        'created_at': 11.0}]})
        self.assertEqual(registry.load(self.state), data)

    def test_every_unreadable_or_out_of_rule_file_is_registry_invalid_and_never_rewritten(self):
        cases = {
            'json': '{not json',
            'schema': self.valid(schema_version=2),
            'not object': '[]',
            'agent limit': self.valid(options={'agent_limit': 9, 'shell_limit': 10}),
            'shell limit': self.valid(options={'agent_limit': 4, 'shell_limit': 0}),
            'limit type': self.valid(options={'agent_limit': True, 'shell_limit': 10}),
            'home outside': self.valid(agents=[{'id': 'a-0123456789ab', 'name': 'agent-1', 'home': '../x',
                                                'created_at': 1}]),
            'home other id': self.valid(agents=[{'id': 'a-0123456789ab', 'name': 'agent-1',
                                                 'home': 'board-agents/a-ffffffffffff', 'created_at': 1}]),
            'bad id': self.valid(agents=[{'id': 'agent', 'name': 'agent-1', 'home': 'board-agent', 'created_at': 1}]),
            'bad name': self.valid(agents=[{'id': 'a-0123456789ab', 'name': 'a\nb', 'home': 'board-agent',
                                            'created_at': 1}]),
            'duplicate id': self.valid(agents=[
                {'id': 'a-0123456789ab', 'name': 'agent-1', 'home': 'board-agent', 'created_at': 1},
                {'id': 'a-0123456789ab', 'name': 'agent-2', 'home': 'board-agents/a-0123456789ab', 'created_at': 1}]),
            'duplicate name': self.valid(agents=[
                {'id': 'a-0123456789ab', 'name': 'agent-1', 'home': 'board-agent', 'created_at': 1},
                {'id': 'a-0123456789ac', 'name': 'AGENT-1', 'home': 'board-agents/a-0123456789ac', 'created_at': 1}]),
            'more agents than the limit': self.valid(options={'agent_limit': 1, 'shell_limit': 10}, agents=[
                {'id': 'a-0123456789ab', 'name': 'agent-1', 'home': 'board-agent', 'created_at': 1},
                {'id': 'a-0123456789ac', 'name': 'agent-2', 'home': 'board-agents/a-0123456789ac', 'created_at': 1}]),
            'missing field': self.valid(agents=[{'id': 'a-0123456789ab', 'name': 'agent-1', 'home': 'board-agent'}]),
        }
        for label, value in cases.items():
            with self.subTest(label):
                self.write_raw(value)
                before = self.path.read_bytes()
                with self.assertRaises(registry.RegistryInvalid) as caught:
                    registry.load(self.state)
                self.assertIn(str(self.path), caught.exception.message)
                self.assertEqual(self.path.read_bytes(), before)

    def test_a_wide_mode_a_symlink_or_a_wide_directory_is_registry_invalid(self):
        self.write_raw(self.valid(), mode=0o644)
        with self.assertRaises(registry.RegistryInvalid):
            registry.load(self.state)
        os.chmod(self.path, 0o600)
        os.chmod(self.directory, 0o755)
        with self.assertRaises(registry.RegistryInvalid):
            registry.load(self.state)
        os.chmod(self.directory, 0o700)
        target = self.root / 'elsewhere.json'
        target.write_text(json.dumps(self.valid()))
        os.chmod(target, 0o600)
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(registry.RegistryInvalid):
            registry.load(self.state)
        self.assertEqual(json.loads(target.read_text()), self.valid())

    def test_a_missing_file_is_none_and_a_failed_write_leaves_the_old_file(self):
        self.assertIsNone(registry.load(self.state))
        first = registry.Registry(options=registry.Options(4, 10),
                                  agents=[registry.AgentEntry('a-0123456789ab', 'agent-1', 'board-agent', 1.0)])
        registry.save(self.state, first)
        before = self.path.read_bytes()
        os.chmod(self.directory, 0o500)  # no new file can be made here (a full disk, EIO alike)
        self.addCleanup(os.chmod, self.directory, 0o700)
        with self.assertRaises(registry.RegistryWriteFailed):
            registry.save(self.state, registry.Registry(options=registry.Options(5, 10), agents=first.agents))
        os.chmod(self.directory, 0o700)
        self.assertEqual(self.path.read_bytes(), before)


class MigrationTests(RegistryCase):
    def test_without_a_file_the_first_board_is_agent_1_in_the_existing_board_agent_home(self):
        old_home = self.state / 'board-agent'
        (old_home / 'omp-root' / 'agent' / 'sessions').mkdir(parents=True)
        marker = old_home / 'omp-root' / 'agent' / 'sessions' / 'kept.jsonl'
        marker.write_text('{"memory": 1}\n')
        data = registry.load_or_create(self.state, now=lambda: 42.0)
        self.assertEqual(data.options, registry.Options(4, 10))
        self.assertEqual(len(data.agents), 1)
        agent = data.agents[0]
        self.assertEqual((agent.name, agent.home, agent.created_at), ('agent-1', 'board-agent', 42.0))
        self.assertRegex(agent.id, r'^a-[0-9a-f]{12}$')
        on_disk = json.loads(self.path.read_text())
        self.assertEqual(on_disk['agents'], [{'id': agent.id, 'name': 'agent-1', 'home': 'board-agent',
                                              'created_at': 42.0}])
        self.assertEqual(marker.read_text(), '{"memory": 1}\n')
        self.assertEqual(sorted(path.name for path in self.state.iterdir()), ['board-agent', 'board-agents'])
        # A second Hub start reads the same file: the same agent, nothing new.
        self.assertEqual(registry.load_or_create(self.state, now=lambda: 99.0), data)

    def test_a_fresh_install_gets_the_same_agent_1_and_an_invalid_file_is_not_replaced(self):
        data = registry.load_or_create(self.state, now=lambda: 1.0)
        self.assertEqual([(agent.name, agent.home) for agent in data.agents], [('agent-1', 'board-agent')])
        self.path.write_text('{broken')
        with self.assertRaises(registry.RegistryInvalid):
            registry.load_or_create(self.state, now=lambda: 2.0)
        self.assertEqual(self.path.read_text(), '{broken')


if __name__ == '__main__':
    unittest.main()
