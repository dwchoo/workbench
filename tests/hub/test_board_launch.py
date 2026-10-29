"""TI-16: Board agent home, system key, internal listener, overlay/argv and the start-up isolation check (seam T6b).

The Board agent's OMP is a fake ``omp`` (``fixtures/fake_omp_board.py``) that records its argv, environment, the
overlay it was given and the skills it shows, and answers the isolation check's RPC dump with a tool list the test
chooses. Expected values come from the SPEC "Board agent 세트" section, the web-board-ux SPEC "Board agent 전환" and
PROBE-BOARD-SKILL "WBX-B5가 쓸 구성" (WBX-B5: REST and the ``wb-inbox`` skill instead of MCP), never from the
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
from unittest import mock

from workbench.hub import auth, rest
from workbench.hub.board import launch
from workbench.hub.config import BoardAgentConfig
from workbench.hub.inbox.service import InboxService
from workbench.hub.inbox_api import InboxApi

REPO = Path(__file__).resolve().parents[2]
FAKE_OMP = Path(__file__).resolve().parent / 'fixtures' / 'fake_omp_board.py'
ISOLATION = REPO / 'omp_bridge' / 'omp-isolation.yml'
# PROBE-BOARD-SKILL "WBX-B5가 쓸 구성": the whole isolation list, ``native`` included (no MCP file to read).
BOARD_DISABLED = ['native', 'omp-managed', 'skillshare', 'agents-md', 'agents', 'claude-md', 'claude-plugins', 'claude',
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
        home = launch.prepare_board_home(launch.board_dir(self.box.state), self.box.env)  # WBX-D1: an agent home
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
    """A ``BoardLauncher`` over a real ``hub.sqlite3`` key store and a real internal REST listener."""

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
        self.api = InboxApi(self.inbox)
        self.addCleanup(self.api.close)
        self.listener = rest.InternalListener(self.keys, self.api)
        self.addCleanup(lambda: self.arun(self.listener.stop()))
        self.launcher = self.make_launcher(self.config)

    def make_launcher(self, config: BoardAgentConfig) -> launch.BoardLauncher:
        return launch.BoardLauncher(self.box.state, config, api_keys=lambda: self.keys, listener=self.listener,
                                    environment=self.box.env)

    def arun(self, coroutine):
        return self.loop.run_until_complete(coroutine)

    AGENT = 'a-000000000001'  # WBX-D1: the migrated agent-1 (its home is board-agent/)

    def spec(self, agent_id: str | None = None, home: str = 'board-agent') -> launch.AgentSpec:
        return launch.AgentSpec(agent_id or self.AGENT, 'agent-1', self.box.state / home)

    def prepare(self, launcher=None, spec=None) -> launch.AgentLaunch:
        return self.arun((launcher or self.launcher).prepare_agent(spec or self.spec()))


class OmpSelectionDetailTests(LauncherCase):
    def test_a_refused_command_link_carries_the_directory_and_the_fix_but_keeps_the_reason_code(self):
        del self.box.env['WORKBENCH_OMP']
        local_bin = self.box.home / '.local' / 'bin'
        local_bin.mkdir(parents=True)
        for private in (self.box.home, self.box.home / '.local'):
            os.chmod(private, 0o700)  # only the command directory is unsafe
        os.chmod(local_bin, 0o775)
        with self.assertRaises(launch.BoardLaunchError) as raised:
            self.prepare(self.make_launcher(self.config))  # the launcher copies the environment at construction
        error = raised.exception
        self.assertEqual(error.reason, 'omp_unavailable')
        self.assertIn(str(local_bin), error.message)
        self.assertIn('group-writable', error.message)
        self.assertIn(f'chmod g-w {local_bin}', error.message)

    def test_an_error_without_a_selection_detail_has_no_message(self):
        self.assertIsNone(launch.BoardLaunchError('shell_unavailable', '/x').message)


class SystemKeyTests(LauncherCase):
    def test_each_start_issues_a_new_system_editor_key_and_the_previous_one_stops_verifying(self):
        first = self.prepare()
        principal = self.keys.verify(first.env['WB_API_KEY'], 1.0)
        self.assertEqual((principal.name, principal.grade, principal.scope, principal.system),
                         ('board-agent.a-000000000001', 'editor', {'all': True}, True))  # WBX-D58: per agent
        second = self.prepare()
        self.assertNotEqual(first.env['WB_API_KEY'], second.env['WB_API_KEY'])
        with self.assertRaises(auth.ApiKeyRefused):
            self.keys.verify(first.env['WB_API_KEY'], 2.0)
        self.assertTrue(self.keys.verify(second.env['WB_API_KEY'], 2.0).system)

    def test_the_internal_listener_is_open_on_loopback_after_prepare(self):
        self.prepare()
        url = self.listener.url
        self.assertRegex(url, r'^http://127\.0\.0\.1:\d+$')  # WBX-B5: the bare origin (REST), no /mcp
        import socket
        with socket.create_connection(('127.0.0.1', self.listener.port), timeout=2):
            pass



class InheritedBoardKeyTests(LauncherCase):
    """Security review fix 4: a Board key in the Hub's own environment (a Board shell starting ``wb``: the older
    ``WB_BOARD_MCP_KEY`` or, since WBX-B5, ``WB_API_KEY``) never reaches a spawned Hub or a Board shell; only the
    Board agent gets the key issued for it."""

    def setUp(self):
        self.box_env_key = 'wbk_AAAAAAAAAA_' + 'x' * 43
        super().setUp()
        self.box.env['WB_BOARD_MCP_KEY'] = self.box_env_key
        self.box.env['WB_API_KEY'] = self.box_env_key
        self.launcher = self.make_launcher(self.config)

    def test_hub_environment_drops_the_board_key(self):
        from workbench.hub.process import hub_environment
        env = hub_environment({'PATH': '/usr/bin', 'WB_BOARD_MCP_KEY': self.box_env_key})
        self.assertEqual(env, {'PATH': '/usr/bin'})

    def test_board_shell_does_not_inherit_the_board_key(self):
        shell = self.launcher.prepare_shell()
        self.assertNotIn('WB_BOARD_MCP_KEY', shell.env)
        self.assertNotIn('WB_API_KEY', shell.env)

    def test_board_agent_gets_its_own_new_key_not_the_inherited_one(self):
        started = self.prepare()
        self.assertNotEqual(started.env['WB_API_KEY'], self.box_env_key)
        self.assertTrue(self.keys.verify(started.env['WB_API_KEY'], 1.0).system)
        self.assertNotIn('WB_BOARD_MCP_KEY', started.env)


def manager_default_selector() -> str:
    """``manager: default:`` of ``omp_bridge/omp-models.yml``, read here without the product parser."""
    section = None
    for line in (REPO / 'omp_bridge' / 'omp-models.yml').read_text().splitlines():
        if line and not line.startswith((' ', '#')):
            section = line.rstrip(':')
        elif section == 'manager' and line.strip().startswith('default:'):
            return line.split(':', 1)[1].split('#', 1)[0].strip()
    raise AssertionError('no manager default selector')


class ShellProgramTests(LauncherCase):
    """WBX-D4: a new Board shell runs ``[board_agent] shell`` (absolute) if set, else bash (``/bin/bash``,
    ``/usr/bin/bash``, ``bash`` on the Hub's PATH), else ``/bin/sh``. ``$SHELL`` is never consulted."""

    def exe(self, name: str) -> str:
        path = self.box.root / name
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o755)
        return str(path)

    def program(self, *, shell_env=None, config='', fixed=(), path=None) -> list:
        env = dict(self.box.env)
        env.pop('SHELL', None)
        if shell_env is not None:
            env['SHELL'] = shell_env
        if path is not None:
            env['PATH'] = path
        self.box.env.clear()
        self.box.env.update(env)
        launcher = self.make_launcher(BoardAgentConfig(shell=config))
        with mock.patch.object(launch, 'BASH_CANDIDATES', tuple(fixed)), mock.patch.object(launch, 'DEFAULT_SHELL', '/bin/sh'):
            return launcher.prepare_shell().argv

    def test_the_configured_shell_wins_over_everything(self):
        mine, bash = self.exe('myshell'), self.exe('bash1')
        self.assertEqual(self.program(config=mine, shell_env='/bin/sh', fixed=[bash]), [mine])

    def test_a_hub_without_shell_or_with_sh_gets_bash(self):
        bash = self.exe('bash1')
        self.assertEqual(self.program(shell_env=None, fixed=[bash]), [bash])
        self.assertEqual(self.program(shell_env='/bin/sh', fixed=[bash]), [bash])

    def test_shell_environment_is_not_consulted_even_when_it_is_a_real_shell(self):
        bash, zsh = self.exe('bash1'), self.exe('zsh')
        self.assertEqual(self.program(shell_env=zsh, fixed=[bash]), [bash])

    def test_bash_candidates_are_tried_in_order_and_skip_missing_ones(self):
        first, second = self.exe('bash1'), self.exe('bash2')
        self.assertEqual(self.program(fixed=[str(self.box.root / 'missing'), second, first]), [second])

    def test_bash_on_the_hubs_path_is_next(self):
        folder = self.box.root / 'onpath'
        folder.mkdir()
        (folder / 'bash').write_text('#!/bin/sh\nexit 0\n')
        (folder / 'bash').chmod(0o755)
        self.assertEqual(self.program(path=str(folder), fixed=[]), [str(folder / 'bash')])

    def test_without_any_bash_it_is_bin_sh(self):
        self.assertEqual(self.program(shell_env=self.exe('zsh'), path=str(self.box.root / 'empty'), fixed=[]), ['/bin/sh'])

    def test_a_configured_shell_that_is_unusable_still_fails_loudly(self):
        for bad in ('relative-shell', str(self.box.root / 'absent')):
            with self.assertRaises(launch.BoardLaunchError) as caught:
                self.program(config=bad, fixed=[self.exe('bash1')])
            self.assertEqual(caught.exception.reason, 'shell_unavailable')


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

    def test_overlay_keeps_every_provider_off_loads_only_the_wb_inbox_copy_and_keeps_memory(self):
        started = self.prepare()
        overlay = self.overlay_path()
        self.assertEqual(stat.S_IMODE(overlay.stat().st_mode), 0o600)
        self.assertEqual(json.loads(overlay.read_text()), {
            'disabledProviders': BOARD_DISABLED, 'mcp': {'enableProjectConfig': False},
            'skills': {'customDirectories': [str(self.box.state / 'board-agent' / 'skills')],
                       'includeSkills': ['wb-inbox'], 'ignoredSkills': []},
            'task': {'disabledAgents': ['seance']}, 'memory': {'backend': 'local'}, 'memories': {'enabled': True}})
        self.assertNotIn(started.env['WB_API_KEY'], overlay.read_text())

    def test_no_mcp_json_is_written(self):  # WBX-B5: was the Hub MCP server entry
        self.prepare()
        self.assertFalse(os.path.lexists(self.mcp_path()))

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
        self.line_with('nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader',
                       'wb_inbox.py projects', '계속할까요?')

    def test_editing_an_active_projects_code_is_preceded_by_the_question(self):
        self.line_with('wb_inbox.py projects', 'root', '계속할까요?')
        self.assertGreaterEqual(self.text.count('계속할까요?'), 2)

    def test_no_lock_or_arbitration(self):
        line = self.line_with('lock')
        self.assertRegex(line.lower(), r'\b(no|never|do not|don\'t)\b')

    def test_the_wb_inbox_skill_replaces_the_mcp_tools(self):  # WBX-B5, SPEC "Board agent 전환"
        line = self.line_with('skill://wb-inbox', 'wb_inbox.py')
        for command in ('projects', 'list', 'get', 'submit', 'update', 'comment', 'set-state', 'cancel', 'result'):
            self.assertIn(f'`{command}`', line)
        self.assertNotIn('mcp', self.text.lower())
        self.assertNotIn('inbox_projects', self.text)

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

    def test_only_the_wb_inbox_skill_passes(self):  # WBX-B5: was "only Hub MCP tools pass"
        self.assertIsNone(self.check())
        checked, = self.box.records('rpc')
        # The check is the same command in RPC mode without a session (never --continue).
        self.assertEqual(checked['argv'][-4:], ['--mode', 'rpc', '--no-session', '--no-title'])
        self.assertNotIn('--continue', checked['argv'])
        self.assertIn('--no-extensions', checked['argv'])

    def test_any_mcp_tool_fails_the_check_and_names_it(self):  # WBX-B5: Hub MCP tools are leaks too
        verdict = self.check(FAKE_BOARD_TOOLS=json.dumps(['mcp__workbench_hub_inbox_list', 'mcp__decoy_user_x']))
        self.assertIsNotNone(verdict)
        self.assertIn('mcp__decoy_user_x', verdict)
        self.assertIn('mcp__workbench_hub_inbox_list', verdict)

    def test_any_other_isolation_leak_fails_the_check(self):
        verdict = self.check(FAKE_BOARD_PROMPT_EXTRA='<file path="/somewhere/AGENTS.md">\nx\n</file>\n')
        self.assertIsNotNone(verdict)
        self.assertIn('context_file', verdict)

    def test_a_check_that_cannot_run_fails_closed(self):
        self.box.omp.write_text('#!/bin/sh\n[ "$1" = --version ] && echo omp/18.12.6 && exit 0\nexit 3\n')
        self.assertIsNotNone(self.check())


class BoardOmpLogTests(LauncherCase):
    """WBX-B12: OMP keeps few of its own logs (``<config root>/logs/omp.<date>.<pid>.log``, pruned by OMP); every Board
    agent start keeps a copy in the persistent Board home (``board-agent/omp-logs``) so a later login problem can be
    diagnosed. Bounded, private, and never with a key in it."""

    def omp_logs(self) -> Path:
        return self.box.state / 'board-agent' / 'omp-root' / 'logs'

    def archive(self) -> Path:
        return self.box.state / 'board-agent' / 'omp-logs'

    def plant(self, name: str, text: str, mtime: float | None = None) -> Path:
        self.omp_logs().mkdir(parents=True, exist_ok=True)
        path = self.omp_logs() / name
        path.write_text(text)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_omp_logs_survive_board_restarts_without_keys(self):
        first = self.prepare()
        key = first.env['WB_API_KEY']
        old = self.plant('omp.2026-10-27.111.log',
                         f'oauth refresh failed: invalid_grant\nAuthorization: Bearer {key}\nmcp key {key}\n'
                         '{"refresh_token": "rt-SECRET-VALUE", "access": "at-SECRET-VALUE"}\n')
        self.prepare()  # restart: the previous run's log is kept
        old.unlink()  # OMP pruned its own copy
        self.plant('omp.2026-10-28.222.log', 'second run\n')
        self.prepare()
        archive = self.archive()
        self.assertEqual(sorted(path.name for path in archive.iterdir()),
                         ['omp.2026-10-27.111.log', 'omp.2026-10-28.222.log'])
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o700)
        kept = (archive / 'omp.2026-10-27.111.log').read_text()
        self.assertIn('invalid_grant', kept)
        for secret in (key, key[len('wbk_') + 11:], 'rt-SECRET-VALUE'):
            self.assertNotIn(secret, kept)
        for path in archive.iterdir():
            self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o600, path)
            self.assertTrue(stat.S_ISREG(path.lstat().st_mode), path)

    def test_the_kept_logs_are_bounded_in_number_and_size(self):
        self.prepare()
        for index in range(25):
            self.plant(f'omp.2026-10-01.{1000 + index}.log', f'run {index}\n', mtime=1_700_000_000 + index)
        big = 'H' * (2 << 20) + 'TAIL-MARK\n'
        self.plant('omp.2026-10-02.9999.log', 'HEAD-MARK\n' + big, mtime=1_700_000_100)
        self.prepare()
        names = sorted(path.name for path in self.archive().iterdir())
        self.assertEqual(len(names), launch.OMP_LOG_KEEP)
        self.assertIn('omp.2026-10-02.9999.log', names)  # the newest stay
        self.assertNotIn('omp.2026-10-01.1000.log', names)  # the oldest go
        kept = (self.archive() / 'omp.2026-10-02.9999.log').read_text()
        self.assertLessEqual(len(kept.encode()), launch.OMP_LOG_FILE_LIMIT + 256)
        self.assertTrue(kept.startswith('HEAD-MARK') and kept.endswith('TAIL-MARK\n'))  # start-up and the end

    def test_a_symlinked_log_is_not_followed(self):
        self.prepare()
        outside = self.box.root / 'outside.txt'
        outside.write_text('NOT-A-LOG\n')
        self.omp_logs().mkdir(parents=True, exist_ok=True)
        (self.omp_logs() / 'omp.2026-10-28.5.log').symlink_to(outside)
        self.prepare()
        self.assertFalse((self.archive() / 'omp.2026-10-28.5.log').exists())


if __name__ == '__main__':
    unittest.main()
