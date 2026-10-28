"""WBX-B5: the Board agent on the internal REST listener and the ``wb-inbox`` skill (no MCP).

Expected values come from the web-board-ux SPEC "Board agent 전환", BRIEF WBX-D11/D24/D28, AC-16 and the WBX-B4 probe
record (PROBE-BOARD-SKILL "WBX-B5가 쓸 구성"), never from the product's own helpers. Seams: S3 (a real
``wb _hub --foreground`` / ``wb hub start`` in a temp HOME/XDG sandbox with the fake ``omp`` of
``fixtures/fake_omp_board.py``, read through ``/proc``) and the in-process ``BoardLauncher`` over a real key store and a
real internal listener (``LauncherCase``). Run with ``discover -s tests/hub -p 'test_board_*.py'``.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import stat
import time
import unittest

from fixtures.web_sandbox import tcp_listening, wait_until
from test_board_independent import Rig
from test_board_launch import LauncherCase
from test_hub_spawn import SpawnSandbox, proc_environ, reported_pid

from workbench.hub import auth
from workbench.hub.process import hub_environment

REPO = Path(__file__).resolve().parents[2]
SKILL_SOURCE = REPO / 'skills' / 'wb-inbox'
SKILL_FILES = ['SKILL.md', 'wb_inbox.py']  # SPEC "Skill 배치": never ``.env`` or ``.env.example``


def isolation_disabled() -> list[str]:
    """``disabledProviders`` of ``omp_bridge/omp-isolation.yml``, read here without the product's list."""
    text = (REPO / 'omp_bridge' / 'omp-isolation.yml').read_text()
    match = re.search(r'^disabledProviders:\s*\[([^\]]*)\]', text, re.M)
    assert match
    return [item.strip() for item in match.group(1).split(',') if item.strip()]


# Values a user shell may hold (a client .env exported, an older Board shell): never inherited by the Hub side.
USER_KEY = 'wbk_aaaaaaaaaa_' + 'U' * 43
USER_URL = 'http://203.0.113.7:8780'
OLD_BOARD_KEY = 'wbk_bbbbbbbbbb_' + 'O' * 43
BOARD_NAMES = ('WB_API_KEY', 'WB_HUB_URL', 'WB_BOARD_MCP_KEY')


class HubEnvironmentTests(unittest.TestCase):
    """(1) Hub env hygiene: SPEC "Hub env 위생" (``process.PROJECT_ENV``), WBX-D28."""

    def test_hub_environment_drops_the_board_rest_variables_and_the_old_board_key(self):
        env = hub_environment({'PATH': '/usr/bin', 'KEEP': '1', 'WB_API_KEY': USER_KEY, 'WB_HUB_URL': USER_URL,
                               'WB_BOARD_MCP_KEY': OLD_BOARD_KEY})
        self.assertEqual(env, {'PATH': '/usr/bin', 'KEEP': '1'})

    def test_a_spawned_hub_does_not_inherit_the_users_key_or_hub_url(self):
        box = SpawnSandbox(self)
        env = dict(box.env, WB_API_KEY=USER_KEY, WB_HUB_URL=USER_URL, WB_BOARD_MCP_KEY=OLD_BOARD_KEY,
                   WBX_B5_KEEP='kept')
        pid = reported_pid(box.wb('hub', 'start', env=env))
        environ = proc_environ(pid)
        for name in BOARD_NAMES:
            self.assertNotIn(name, environ)
        self.assertEqual(environ.get('WBX_B5_KEEP'), 'kept')  # everything else is the caller's
        raw = open(f'/proc/{pid}/environ', 'rb').read()
        for value in (USER_KEY, USER_URL, OLD_BOARD_KEY):
            self.assertNotIn(value.encode(), raw)



def environ_of(pid: int) -> dict[str, str]:
    raw = Path(f'/proc/{pid}/environ').read_bytes()
    return dict(item.decode(errors='replace').split('=', 1) for item in raw.split(b'\0') if b'=' in item)


def no_proxy_hosts(value: str | None) -> list[str]:
    return [item.strip() for item in (value or '').split(',') if item.strip()]


INHERITED = {'WB_API_KEY': USER_KEY, 'WB_HUB_URL': USER_URL, 'WB_BOARD_MCP_KEY': OLD_BOARD_KEY}


class ShellEnvironmentTests(LauncherCase):
    """(2) Board shell tab: ``hub_environment(base)`` + ``TERM`` + locale; no ``WB_API_KEY``/``WB_HUB_URL``."""

    def setUp(self):
        super().setUp()
        self.box.env.update(INHERITED)
        self.launcher = self.make_launcher(self.config)

    def test_the_shell_gets_neither_the_board_names_nor_the_users_values(self):
        self.prepare()  # an agent start issued a system key first
        shell = self.launcher.prepare_shell()
        for name in BOARD_NAMES:
            self.assertNotIn(name, shell.env)
        self.assertFalse([name for name, value in shell.env.items() if 'wbk_' in value], 'a Hub key in the shell')


class AgentEnvironmentTests(LauncherCase):
    """(3) Board agent env: the system key of this start as ``WB_API_KEY``, the internal origin as ``WB_HUB_URL``,
    loopback in ``NO_PROXY``/``no_proxy``; the user's same-named values are overwritten."""

    def setUp(self):
        super().setUp()
        self.box.env.update(INHERITED, NO_PROXY='corp.example', no_proxy='corp.example')
        self.launcher = self.make_launcher(self.config)

    def test_the_agent_gets_this_starts_system_key_and_the_internal_origin(self):
        started = self.prepare()
        key = started.env['WB_API_KEY']
        principal = self.keys.verify(key, 1.0)
        self.assertEqual((principal.name, principal.grade, principal.scope, principal.system),
                         ('board-agent', 'editor', {'all': True}, True))
        self.assertEqual(started.env['WB_HUB_URL'], f'http://127.0.0.1:{self.listener.port}')
        self.assertNotIn('WB_BOARD_MCP_KEY', started.env)
        self.assertEqual([name for name, value in started.env.items() if key in value], ['WB_API_KEY'])
        for name in ('NO_PROXY', 'no_proxy'):
            self.assertIn('127.0.0.1', no_proxy_hosts(started.env[name]), name)
            self.assertIn('corp.example', no_proxy_hosts(started.env[name]), name)  # the user's entries stay

    def test_no_proxy_is_set_when_the_user_has_none(self):
        del self.box.env['NO_PROXY'], self.box.env['no_proxy']
        started = self.prepare(self.make_launcher(self.config))
        for name in ('NO_PROXY', 'no_proxy'):
            self.assertIn('127.0.0.1', no_proxy_hosts(started.env[name]), name)

    def test_each_start_gets_a_new_key_and_the_previous_one_stops_verifying(self):
        first = self.prepare().env['WB_API_KEY']
        second = self.prepare().env['WB_API_KEY']
        self.assertNotEqual(first, second)
        with self.assertRaises(auth.ApiKeyRefused):
            self.keys.verify(first, 2.0)


class LiveEnvironmentTests(unittest.TestCase):
    """(1)(2)(3) over S3: a foreground Hub started with the user's values; ``/proc/<pid>/environ`` of both channels."""

    def test_proc_environ_of_the_agent_and_the_shell(self):
        rig = Rig(self, extra_env=dict(INHERITED))
        pids = rig.start_set()
        agent, shell = environ_of(pids['agent']), environ_of(pids['shell'])
        key = agent['WB_API_KEY']
        self.assertNotEqual(key, USER_KEY)
        self.assertTrue(key.startswith('wbk_'))
        db = auth.HubDb.open(rig.state)
        try:
            self.assertTrue(auth.ApiKeyStore(db).verify(key, 1.0).system)
        finally:
            db.close()
        port = int(agent['WB_HUB_URL'].rpartition(':')[2])
        self.assertEqual(agent['WB_HUB_URL'], f'http://127.0.0.1:{port}')
        self.assertIn(('127.0.0.1', port), tcp_listening(rig.hub.pid))
        self.assertNotIn('WB_BOARD_MCP_KEY', agent)
        self.assertIn('127.0.0.1', no_proxy_hosts(agent.get('NO_PROXY')))
        self.assertIn('127.0.0.1', no_proxy_hosts(agent.get('no_proxy')))
        for name in BOARD_NAMES:
            self.assertNotIn(name, shell)
        raw_shell = Path(f'/proc/{pids["shell"]}/environ').read_bytes()
        for value in (key, USER_KEY, USER_URL, OLD_BOARD_KEY):
            self.assertNotIn(value.encode(), raw_shell)
        self.assertNotIn(key.encode(), Path(f'/proc/{rig.hub.pid}/environ').read_bytes())



def http_status(port: int, path: str, key: str | None, *, method: str = 'GET', host: str | None = None,
                body: bytes | None = None) -> int:
    headers = {'Host': host or f'127.0.0.1:{port}'}
    if key is not None:
        headers['Authorization'] = f'Bearer {key}'
    if body is not None:
        headers['Content-Type'] = 'application/json'
        headers['Accept'] = 'application/json, text/event-stream'
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def issue_owner_key(state: Path) -> str:
    """An owner-issued editor key written to the running Hub's ``hub.sqlite3`` (as ``/settings`` would)."""
    db = auth.HubDb.open(state)
    try:
        return auth.ApiKeyStore(db).issue('owner-cli', 'editor', {'all': True}, None, now=time.time()).raw
    finally:
        db.close()


INIT = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}})


class LiveInternalRestTests(unittest.TestCase):
    """(4) S3: the real Hub's internal listener serves REST to this start's system key only; no ``/mcp``."""

    def test_rest_with_the_system_key_only_and_no_mcp(self):
        rig = Rig(self)
        pids = rig.start_set()
        agent = environ_of(pids['agent'])
        key, port = agent['WB_API_KEY'], int(agent['WB_HUB_URL'].rpartition(':')[2])
        self.assertEqual(http_status(port, '/api/v1/requests', key), 200)
        self.assertEqual(http_status(port, '/api/v1/projects', key), 200)
        self.assertEqual(http_status(port, '/mcp', key, method='POST', body=INIT.encode()), 404)
        self.assertEqual(http_status(port, '/skill/', key), 404)
        self.assertEqual(http_status(port, '/api/v1/requests', key, host=f'localhost:{port}'), 421)
        self.assertEqual(http_status(port, '/api/v1/requests', None), 401)
        owner = issue_owner_key(rig.state)
        self.assertEqual(http_status(port, '/api/v1/requests', owner), 401)
        web_host, web_port = rig.browser.host, rig.browser.port  # the external web listener takes every valid key
        self.assertEqual(http_status(web_port, '/api/v1/requests', owner, host=f'{web_host}:{web_port}'), 200)
        self.assertEqual(rig.restart('agent'), 303)
        rig.wait_state('agent', 'running')
        new = environ_of(rig.board()['agent']['pid'])
        self.assertNotEqual(new['WB_API_KEY'], key)
        self.assertEqual(new['WB_HUB_URL'], agent['WB_HUB_URL'])  # one listener for every start of this Hub
        self.assertEqual((http_status(port, '/api/v1/requests', key),
                          http_status(port, '/api/v1/requests', new['WB_API_KEY'])), (401, 200))



class SkillCopyOverlayTests(LauncherCase):
    """(5) The Board skill copy rebuilt at every start, the WBX-B4 overlay, and no ``mcp.json`` (SPEC "Skill 배치")."""

    def board(self) -> Path:
        return self.box.state / 'board-agent'

    def skills(self) -> Path:
        return self.board() / 'skills'

    def agent_dir(self) -> Path:
        return self.board() / 'omp-root' / 'agent'

    def assert_copy(self) -> None:
        folder = self.skills() / 'wb-inbox'
        self.assertEqual(sorted(os.listdir(self.skills())), ['wb-inbox'])
        self.assertEqual(sorted(os.listdir(folder)), SKILL_FILES)
        for directory in (self.skills(), folder):
            info = os.lstat(directory)
            self.assertTrue(stat.S_ISDIR(info.st_mode), directory)
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o700, directory)
        for name in SKILL_FILES:
            info = os.lstat(folder / name)
            self.assertTrue(stat.S_ISREG(info.st_mode), name)
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600, name)
            self.assertEqual((folder / name).read_bytes(), (SKILL_SOURCE / name).read_bytes(), name)

    def test_the_skill_copy_holds_exactly_the_packaged_skill_files(self):
        self.prepare()
        self.assert_copy()

    def test_every_start_rebuilds_the_copy_and_never_writes_through_a_link(self):
        self.prepare()
        folder = self.skills() / 'wb-inbox'
        victim = self.box.root / 'victim.txt'
        victim.write_text('untouched\n')
        (folder / 'SKILL.md').write_text('---\nname: wb-inbox\ndescription: tampered\n---\n')
        (folder / 'wb_inbox.py').unlink()
        (folder / 'wb_inbox.py').symlink_to(victim)
        (folder / '.env').write_text('WB_API_KEY=wbk_planted\n')
        decoy = self.skills() / 'decoy'
        decoy.mkdir()
        (decoy / 'SKILL.md').write_text('---\nname: decoy\ndescription: x\n---\n')
        os.chmod(self.skills(), 0o755)
        self.prepare()
        self.assert_copy()
        self.assertEqual(victim.read_text(), 'untouched\n')

    def test_a_leftover_mcp_json_is_removed_at_start_and_none_is_written(self):
        self.prepare()
        self.assertFalse(os.path.lexists(self.agent_dir() / 'mcp.json'))
        stale = {'mcpServers': {'workbench-hub': {'type': 'http', 'url': 'http://127.0.0.1:9/mcp',
                                                  'headers': {'Authorization': 'Bearer ${WB_BOARD_MCP_KEY}'}}}}
        (self.agent_dir() / 'mcp.json').write_text(json.dumps(stale))
        self.prepare()
        self.assertFalse(os.path.lexists(self.agent_dir() / 'mcp.json'))
        victim = self.box.root / 'victim.json'
        victim.write_text('{}\n')
        (self.agent_dir() / 'mcp.json').symlink_to(victim)
        self.prepare()
        self.assertFalse(os.path.lexists(self.agent_dir() / 'mcp.json'))
        self.assertEqual(victim.read_text(), '{}\n')

    def test_the_overlay_is_the_probe_configuration(self):
        started = self.prepare()
        overlay = self.board() / 'omp-board.yml'
        self.assertEqual(stat.S_IMODE(overlay.stat().st_mode), 0o600)
        self.assertEqual(json.loads(overlay.read_text()), {
            'disabledProviders': isolation_disabled(), 'mcp': {'enableProjectConfig': False},
            'skills': {'customDirectories': [str(self.skills())], 'includeSkills': ['wb-inbox'],
                       'ignoredSkills': []},
            'task': {'disabledAgents': ['seance']}, 'memory': {'backend': 'local'}, 'memories': {'enabled': True}})
        self.assertNotIn(started.env['WB_API_KEY'], overlay.read_text())

    def test_the_board_home_config_names_the_skill_copy_and_every_isolation_provider(self):
        self.prepare()
        text = (self.agent_dir() / 'config.yml').read_text()
        config = json.loads(text[text.index('{'):])
        self.assertEqual(config['disabledProviders'], isolation_disabled())
        self.assertEqual(config['skills']['customDirectories'], [str(self.skills())])
        self.assertTrue((self.board() / 'empty').is_dir())
        self.assertEqual(os.listdir(self.board() / 'empty'), [])



class BoardIsolationTests(LauncherCase):
    """(6)-(9) The start-up isolation check: no leak and exactly the ``wb-inbox`` skill (SPEC "격리 검사")."""

    def check(self, **fake_env) -> str | None:
        self.box.env.update(fake_env)
        self.launcher = self.make_launcher(self.config)
        started = self.prepare()
        return self.arun(self.launcher.check_agent(started))

    def test_6_the_wb_inbox_skill_alone_and_no_mcp_tool_pass(self):
        self.assertIsNone(self.check())
        checked, = self.box.records('rpc')
        self.assertEqual(checked['skills'], ['wb-inbox'])  # what the fake OMP showed (read from the overlay)
        self.assertEqual(checked['argv'][-4:], ['--mode', 'rpc', '--no-session', '--no-title'])
        self.assertIsNone(checked['mcp_json'])

    def test_7_any_mcp_tool_is_a_leak_the_hub_tools_included(self):
        for tools in (['mcp__workbench_hub_inbox_list'], ['mcp__decoy_user_x']):
            with self.subTest(tools=tools):
                verdict = self.check(FAKE_BOARD_TOOLS=json.dumps(tools))
                self.assertIsNotNone(verdict)
                self.assertTrue(verdict.startswith('leak:'), verdict)
                self.assertIn(f'mcp:{tools[0]}', verdict)

    def test_8_another_skill_is_a_leak(self):
        verdict = self.check(FAKE_BOARD_SKILLS=json.dumps(['decoy-home-skill', 'wb-inbox']))
        self.assertIsNotNone(verdict)
        self.assertTrue(verdict.startswith('leak:'), verdict)
        self.assertIn('skill:decoy-home-skill', verdict)
        self.assertNotIn('skill:wb-inbox', verdict)

    def test_9_a_missing_wb_inbox_skill_fails_with_skill_missing(self):
        self.assertEqual(self.check(FAKE_BOARD_SKILLS='[]'), 'skill_missing:wb-inbox')

    def test_9_another_skill_instead_of_wb_inbox_names_both(self):
        verdict = self.check(FAKE_BOARD_SKILLS=json.dumps(['decoy']))
        self.assertIsNotNone(verdict)
        self.assertIn('skill:decoy', verdict)


class LiveIsolationTests(unittest.TestCase):
    """(7)(9) S3: the real Hub ends the agent as ``isolation_failed`` with the reason; the shell is untouched."""

    def run_agent(self, **extra) -> tuple[Rig, dict]:
        rig = Rig(self, extra_env=extra)
        self.assertEqual(rig.restart('agent'), 303)
        return rig, rig.wait_state('agent', 'isolation_failed')

    def test_a_hub_mcp_tool_now_fails_the_start(self):
        _, agent = self.run_agent(FAKE_BOARD_TOOLS=json.dumps(['mcp__workbench_hub_inbox_list']))
        self.assertIn('mcp__workbench_hub_inbox_list', agent['detail'])

    def test_a_missing_skill_fails_the_start_with_skill_missing(self):
        rig, agent = self.run_agent(FAKE_BOARD_SKILLS='[]')
        self.assertIn('skill_missing:wb-inbox', agent['detail'])
        self.assertIn('isolation_failed', [row['reason'] for row in rig.audit('board_stop')])



class LiveAllowFromTests(unittest.TestCase):
    """(10) ``allow_from`` without loopback closes the web listener to the test but not the Board agent's REST."""

    def test_board_rest_keeps_working_when_loopback_leaves_allow_from(self):
        rig = Rig(self)
        pids = rig.start_set()
        agent = environ_of(pids['agent'])
        key, port = agent['WB_API_KEY'], int(agent['WB_HUB_URL'].rpartition(':')[2])
        text = rig.box.config_file.read_text().replace('[web]\n', '[web]\nallow_from = ["10.0.0.0/8"]\n', 1)
        rig.box.config_file.write_text(text)
        reloaded = rig.cli('hub', 'reload')
        self.assertEqual(reloaded.returncode, 0, reloaded)
        def web_ports() -> list[int]:  # port 0 in hub.toml: the reloaded web listener may sit on a new port
            return [item for _, item in tcp_listening(rig.hub.pid) if item != port]
        self.assertTrue(wait_until(lambda: len(web_ports()) == 1, 10), tcp_listening(rig.hub.pid))
        web_port = web_ports()[0]
        def refused_by_allow_from() -> bool:
            try:
                return http_status(web_port, '/api/v1/requests', key, host=f'127.0.0.1:{web_port}') == 403
            except OSError:  # the listener is still rebinding
                return False
        self.assertTrue(wait_until(refused_by_allow_from, 10))
        self.assertEqual(http_status(port, '/api/v1/requests', key), 200)
        reply = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        try:
            body = json.dumps({'project_id': 'unknown', 'title': 't', 'body': 'b'})
            reply.request('POST', '/api/v1/requests', body=body, headers={
                'Host': f'127.0.0.1:{port}', 'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
            response = reply.getresponse()
            self.assertNotIn(response.status, (401, 403, 421), response.read())
        finally:
            reply.close()
        self.assertEqual(rig.board()['agent']['pid'], pids['agent'])


if __name__ == '__main__':
    unittest.main()
