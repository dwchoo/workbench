"""TI-16: Board agent home, system key, internal listener, overlay/argv and the start-up isolation check (seam T6b).

The Board agent's OMP is a fake ``omp`` (``fixtures/fake_omp_board.py``) that records its argv, environment and the
overlay/``mcp.json`` it was given, and answers the isolation check's RPC dump with a tool list the test chooses.
Expected values come from the SPEC "Board agent 세트" section and PROBE-OMP-MCP "TI-16이 쓸 구성", never from the
product's own helpers.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest

from workbench.hub import auth, mcp
from workbench.hub.board import launch
from workbench.hub.config import BoardAgentConfig
from workbench.hub.inbox.service import InboxService

REPO = Path(__file__).resolve().parents[2]
FAKE_OMP = Path(__file__).resolve().parent / 'fixtures' / 'fake_omp_board.py'
ISOLATION = REPO / 'omp_bridge' / 'omp-isolation.yml'
# PROBE-OMP-MCP "TI-16이 쓸 구성": the isolation list without ``native``.
BOARD_DISABLED = ['omp-managed', 'skillshare', 'agents-md', 'agents', 'claude-md', 'claude-plugins', 'claude',
                  'cline', 'codex', 'cursor', 'gemini', 'github', 'mcp-json', 'omp-plugins', 'opencode', 'ssh-json',
                  'vscode', 'windsurf', 'agent-plugins', 'builtin-defaults']


class BoardSandbox:
    """Temp HOME with a placeholder user ``agent.db`` (never opened), Hub state dir and a fake ``omp``."""

    def __init__(self, case: unittest.TestCase):
        self.root = Path(tempfile.mkdtemp(prefix='wbb-'))
        case.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / 'home'
        self.user_store = self.home / '.omp' / 'agent' / 'agent.db'
        self.user_store.parent.mkdir(parents=True)
        self.user_store.write_bytes(b'')
        self.state = self.home / '.local' / 'state' / 'omp-workbench' / 'hub'
        self.state.mkdir(parents=True, mode=0o700)
        self.record = self.root / 'record.jsonl'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.omp = self.bin / 'omp'
        self.omp.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_OMP} "$@"\n')
        self.omp.chmod(0o755)
        self.env = {'PATH': f'{self.bin}:/usr/bin:/bin', 'HOME': str(self.home), 'LANG': 'C.UTF-8',
                    'WORKBENCH_OMP': str(self.omp), 'FAKE_BOARD_RECORD': str(self.record),
                    'WORKBENCH_HUB_AUTOSTART': '0', 'SHELL': '/bin/sh'}

    def records(self, mode: str | None = None) -> list[dict]:
        try:
            rows = [json.loads(line) for line in self.record.read_text().splitlines() if line.strip()]
        except FileNotFoundError:
            return []
        return [row for row in rows if mode is None or row['mode'] == mode]


class BoardHomeTests(unittest.TestCase):
    def setUp(self):
        self.box = BoardSandbox(self)

    def test_home_is_under_hub_state_with_agent_db_linked_to_the_user_store(self):
        home = launch.prepare_board_home(self.box.state, self.box.env)
        root = self.box.state / 'board-agent' / 'omp-root'
        self.assertEqual(home.root, root)
        self.assertEqual(home.agent_dir, root / 'agent')
        link = root / 'agent' / 'agent.db'
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(self.box.user_store))
        for directory in (self.box.state / 'board-agent', root, root / 'agent'):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        # PI_CONFIG_DIR is relative to HOME and names the board root (OMP joins it to HOME).
        config_dir = home.environment()['PI_CONFIG_DIR']
        self.assertFalse(os.path.isabs(config_dir))
        self.assertEqual(os.path.realpath(os.path.join(self.box.home, config_dir)), os.path.realpath(root))
        self.assertEqual(home.environment()['PI_CODING_AGENT_DIR'], str(root / 'agent'))


class LauncherCase(unittest.TestCase):
    """A ``BoardLauncher`` over a real ``hub.sqlite3`` key store and a real internal MCP listener."""

    config = BoardAgentConfig()

    def setUp(self):
        self.box = BoardSandbox(self)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.db = auth.HubDb.open(self.box.state)
        self.addCleanup(self.db.close)
        self.keys = auth.ApiKeyStore(self.db)
        self.inbox = InboxService()
        self.addCleanup(self.inbox.close)
        self.listener = mcp.InternalListener(self.keys, mcp.McpServer(self.inbox))
        self.addCleanup(lambda: self.arun(self.listener.stop()))
        self.launcher = self.make_launcher(self.config)

    def make_launcher(self, config: BoardAgentConfig) -> launch.BoardLauncher:
        return launch.BoardLauncher(self.box.state, config, api_keys=lambda: self.keys, listener=self.listener,
                                    environment=self.box.env)

    def arun(self, coroutine):
        return self.loop.run_until_complete(coroutine)

    def prepare(self, launcher=None) -> launch.AgentLaunch:
        return self.arun((launcher or self.launcher).prepare_agent())


class SystemKeyTests(LauncherCase):
    def test_each_start_issues_a_new_system_editor_key_and_the_previous_one_stops_verifying(self):
        first = self.prepare()
        principal = self.keys.verify(first.env['WB_BOARD_MCP_KEY'], 1.0)
        self.assertEqual((principal.name, principal.grade, principal.scope, principal.system),
                         ('board-agent', 'editor', {'all': True}, True))
        second = self.prepare()
        self.assertNotEqual(first.env['WB_BOARD_MCP_KEY'], second.env['WB_BOARD_MCP_KEY'])
        with self.assertRaises(auth.ApiKeyRefused):
            self.keys.verify(first.env['WB_BOARD_MCP_KEY'], 2.0)
        self.assertTrue(self.keys.verify(second.env['WB_BOARD_MCP_KEY'], 2.0).system)

    def test_the_internal_listener_is_open_on_loopback_after_prepare(self):
        self.prepare()
        url = self.listener.url
        self.assertRegex(url, r'^http://127\.0\.0\.1:\d+/mcp$')
        import socket
        with socket.create_connection(('127.0.0.1', self.listener.port), timeout=2):
            pass



class InheritedBoardKeyTests(LauncherCase):
    """Security review fix 4: a ``WB_BOARD_MCP_KEY`` in the Hub's own environment (a Board shell starting ``wb``)
    never reaches a spawned Hub or a Board shell; only the Board agent gets the key issued for it."""

    def setUp(self):
        self.box_env_key = 'wbk_AAAAAAAAAA_' + 'x' * 43
        super().setUp()
        self.box.env['WB_BOARD_MCP_KEY'] = self.box_env_key
        self.launcher = self.make_launcher(self.config)

    def test_hub_environment_drops_the_board_key(self):
        from workbench.hub.process import hub_environment
        env = hub_environment({'PATH': '/usr/bin', 'WB_BOARD_MCP_KEY': self.box_env_key})
        self.assertEqual(env, {'PATH': '/usr/bin'})

    def test_board_shell_does_not_inherit_the_board_key(self):
        shell = self.launcher.prepare_shell()
        self.assertNotIn('WB_BOARD_MCP_KEY', shell.env)

    def test_board_agent_gets_its_own_new_key_not_the_inherited_one(self):
        started = self.prepare()
        self.assertNotEqual(started.env['WB_BOARD_MCP_KEY'], self.box_env_key)
        self.assertTrue(self.keys.verify(started.env['WB_BOARD_MCP_KEY'], 1.0).system)


def manager_default_selector() -> str:
    """``manager: default:`` of ``omp_bridge/omp-models.yml``, read here without the product parser."""
    section = None
    for line in (REPO / 'omp_bridge' / 'omp-models.yml').read_text().splitlines():
        if line and not line.startswith((' ', '#')):
            section = line.rstrip(':')
        elif section == 'manager' and line.strip().startswith('default:'):
            return line.split(':', 1)[1].split('#', 1)[0].strip()
    raise AssertionError('no manager default selector')


class OverlayArgvTests(LauncherCase):
    def overlay_path(self) -> Path:
        return self.box.state / 'board-agent' / 'omp-board.yml'

    def mcp_path(self) -> Path:
        return self.box.state / 'board-agent' / 'omp-root' / 'agent' / 'mcp.json'

    def test_argv_has_isolation_then_board_overlay_no_extensions_role_prompt_allow_home_and_manager_model(self):
        started = self.prepare()
        role = (REPO / 'omp_bridge' / 'board' / 'ROLE.md').read_text()
        self.assertEqual(started.argv, [
            str(self.box.omp), '--config', str(ISOLATION), '--config', str(self.overlay_path()), '--no-extensions',
            '--append-system-prompt', role, '--no-title', '--allow-home', '--model', manager_default_selector()])
        self.assertNotIn('--extension', started.argv)
        self.assertNotIn('--continue', started.argv)
        self.assertEqual(started.cwd, str(self.box.home))

    def test_overlay_turns_on_only_the_native_provider_and_keeps_memory(self):
        started = self.prepare()
        overlay = self.overlay_path()
        self.assertEqual(stat.S_IMODE(overlay.stat().st_mode), 0o600)
        self.assertEqual(json.loads(overlay.read_text()), {
            'disabledProviders': BOARD_DISABLED, 'mcp': {'enableProjectConfig': False},
            'skills': {'customDirectories': [], 'includeSkills': [], 'ignoredSkills': []},
            'task': {'disabledAgents': ['seance']}, 'memory': {'backend': 'local'}, 'memories': {'enabled': True}})
        self.assertNotIn(started.env['WB_BOARD_MCP_KEY'], overlay.read_text())

    def test_mcp_json_names_exactly_the_hub_server_on_the_internal_listener_with_an_env_key_reference(self):
        started = self.prepare()
        path = self.mcp_path()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text()), {'mcpServers': {'workbench-hub': {
            'type': 'http', 'url': self.listener.url,
            'headers': {'Authorization': 'Bearer ${WB_BOARD_MCP_KEY}'}}}})
        self.assertNotIn(started.env['WB_BOARD_MCP_KEY'], path.read_text())

    def test_environment_points_omp_at_the_board_home_and_keeps_the_user_home(self):
        started = self.prepare()
        root = self.box.state / 'board-agent' / 'omp-root'
        self.assertEqual(started.env['HOME'], str(self.box.home))
        self.assertEqual(started.env['PI_CODING_AGENT_DIR'], str(root / 'agent'))
        self.assertEqual(os.path.realpath(os.path.join(self.box.home, started.env['PI_CONFIG_DIR'])),
                         os.path.realpath(root))
        self.assertEqual(started.env['TERM'], 'xterm-256color')

    def test_configured_model_and_extra_role_prompt_file(self):
        extra = self.box.root / 'extra-role.md'
        extra.write_text('EXTRA BOARD RULES\n')
        started = self.prepare(self.make_launcher(BoardAgentConfig(model='anthropic/claude-x:high',
                                                                   role_prompt_file=str(extra))))
        prompt = started.argv[started.argv.index('--append-system-prompt') + 1]
        role = (REPO / 'omp_bridge' / 'board' / 'ROLE.md').read_text()
        self.assertTrue(prompt.startswith(role.rstrip('\n')))
        self.assertTrue(prompt.rstrip('\n').endswith('EXTRA BOARD RULES'))
        self.assertEqual(started.argv[started.argv.index('--model') + 1], 'anthropic/claude-x:high')

    def test_continue_when_the_board_home_already_holds_a_session(self):
        self.prepare()
        sessions = self.box.state / 'board-agent' / 'omp-root' / 'agent' / 'sessions' / '-home'
        sessions.mkdir(parents=True)
        (sessions / 'one.jsonl').write_text('{}\n')
        started = self.prepare()
        self.assertIn('--continue', started.argv)
        self.assertTrue((sessions / 'one.jsonl').is_file())


class RolePromptTests(unittest.TestCase):
    """T9 (P2-AC-36, D-27): the default role prompt's guidance, read as text."""

    def setUp(self):
        self.text = (REPO / 'omp_bridge' / 'board' / 'ROLE.md').read_text()
        self.paragraphs = [block for block in self.text.split('\n') if block.strip()]

    def line_with(self, *needles: str) -> str:
        found = [line for line in self.paragraphs if all(needle in line for needle in needles)]
        self.assertTrue(found, f'no ROLE.md line with {needles}')
        return found[0]

    def test_gpu_use_is_preceded_by_the_nvidia_smi_check_and_the_question(self):
        self.line_with('nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader', 'inbox_projects',
                       '계속할까요?')

    def test_editing_an_active_projects_code_is_preceded_by_the_question(self):
        self.line_with('inbox_projects', 'root', '계속할까요?')
        self.assertGreaterEqual(self.text.count('계속할까요?'), 2)

    def test_no_lock_or_arbitration(self):
        line = self.line_with('lock')
        self.assertRegex(line.lower(), r'\b(no|never|do not|don\'t)\b')

    def test_role_limits_are_stated(self):
        self.line_with('approve')
        self.line_with('worker')
        self.line_with('manager', 'review')


class IsolationCheckTests(LauncherCase):
    def check(self, **fake_env) -> str | None:
        self.box.env.update(fake_env)
        self.launcher = self.make_launcher(self.config)
        started = self.prepare()
        return self.arun(self.launcher.check_agent(started))

    def test_only_hub_mcp_tools_pass(self):
        self.assertIsNone(self.check(FAKE_BOARD_TOOLS=json.dumps(['mcp__workbench_hub_inbox_list',
                                                                 'mcp__workbench_hub_inbox_get'])))
        checked, = self.box.records('rpc')
        # The check is the same command in RPC mode without a session (never --continue).
        self.assertEqual(checked['argv'][-4:], ['--mode', 'rpc', '--no-session', '--no-title'])
        self.assertNotIn('--continue', checked['argv'])
        self.assertIn('--no-extensions', checked['argv'])

    def test_another_mcp_tool_fails_the_check_and_names_it(self):
        verdict = self.check(FAKE_BOARD_TOOLS=json.dumps(['mcp__workbench_hub_inbox_list', 'mcp__decoy_user_x']))
        self.assertIsNotNone(verdict)
        self.assertIn('mcp__decoy_user_x', verdict)
        self.assertNotIn('mcp__workbench_hub_inbox_list', verdict)

    def test_any_other_isolation_leak_fails_the_check(self):
        verdict = self.check(FAKE_BOARD_PROMPT_EXTRA='<file path="/somewhere/AGENTS.md">\nx\n</file>\n')
        self.assertIsNotNone(verdict)
        self.assertIn('context_file', verdict)

    def test_a_check_that_cannot_run_fails_closed(self):
        self.box.omp.write_text('#!/bin/sh\n[ "$1" = --version ] && echo omp/18.12.6 && exit 0\nexit 3\n')
        self.assertIsNotNone(self.check())


if __name__ == '__main__':
    unittest.main()
