"""2026-10-27 사용자 결정: ``wb hub reload`` makes the running Hub re-read ``hub.toml`` with the strict parser.

Seams: a real ``wb _hub --foreground`` in the shared web sandbox (T3), driven through ``wb hub reload``/``wb hub
status`` and hub_v1 ``reload`` through ``HubClient``; observed through its LISTEN sockets (``/proc/net/tcp*``), plain
HTTP, ``/proc`` and the fixture backend's frame log. Expected values come from the user decision: an invalid file
keeps the running config (``config_invalid:<item>``, exit 1); a web change re-creates only the web listener; login
sessions survive; the Board agent set is never killed by a reload; WB projects are never touched.
Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import textwrap
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.web_sandbox import PASSWORD, WebSandbox, tcp_listening, wait_until  # noqa: E402


def refused(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=3).close()
    except ConnectionRefusedError:
        return True
    return False


def get(host: str, port: int, path: str = '/login', cookies: dict | None = None) -> tuple[int, dict, str]:
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in cookies.items())} if cookies else {}
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.headers), response.read().decode('utf-8', 'replace')
    finally:
        connection.close()


def login(host: str, port: int) -> str:
    """A logged-in session cookie value."""
    from urllib.parse import urlencode
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request('GET', '/login')
        response = connection.getresponse()
        page = response.read().decode()
        cookie = re.search(r'wb_hub_login=([^;]+)', response.headers['Set-Cookie']).group(1)
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
    finally:
        connection.close()
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request('POST', '/login', body=urlencode({'login_token': token, 'password': PASSWORD}),
                           headers={'Content-Type': 'application/x-www-form-urlencoded',
                                    'Origin': f'http://{host}:{port}', 'Cookie': f'wb_hub_login={cookie}'})
        response = connection.getresponse()
        response.read()
        assert response.status == 303, response.status
        return re.search(r'wb_hub_session=([^;]+)', ', '.join(response.headers.get_all('Set-Cookie'))).group(1)
    finally:
        connection.close()


class ReloadCase(unittest.TestCase):
    def setUp(self):
        self.box = WebSandbox(self)

    def start(self, *lines: str, enabled: bool = True):
        self.box.write_config('127.0.0.1', 0, *lines, enabled=enabled)
        self.box.set_password()
        self.hub = self.box.start_hub()
        return self.hub

    def reload(self) -> subprocess.CompletedProcess:
        return self.box.cli('hub', 'reload')

    def hub_reload(self) -> dict:
        from workbench.hub.client import HubClient
        from workbench.hub.paths import hub_layout
        with HubClient(hub_layout(self.box.env), timeout=30) as client:
            return client.request('reload')

    def listening(self) -> list:
        return sorted(tcp_listening(self.hub.pid))


class NoHubReloadTests(ReloadCase):
    def test_without_a_hub_reload_says_so_exits_3_and_starts_none(self):
        self.box.write_config('127.0.0.1', 0, enabled=True)
        result = self.reload()
        self.assertEqual(result.returncode, 3, result)
        self.assertIn('Hub 실행 중 아님', result.stderr)
        self.assertFalse((self.box.state / 'hub.json').exists())
        self.assertFalse((self.box.state / 'hub.log').exists())


class InvalidReloadTests(ReloadCase):
    def test_an_invalid_file_keeps_the_running_config_and_listener(self):
        self.start()
        host, port = self.box.wait_listening(self.hub.pid)
        before = self.box.status()['web']
        self.box.write_config(raw='[web]\nenabled = true\nbind = "localhost"\nport = 0\n')
        result = self.reload()
        self.assertEqual(result.returncode, 1, result)
        self.assertIn('web.bind', result.stderr)
        self.assertRegex(result.stderr, r'[가-힣]')
        reply = self.hub_reload()
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['reason'], 'config_invalid')
        self.assertEqual(reply['detail'], 'config_invalid:web.bind')
        self.assertEqual(reply['item'], 'web.bind')
        status = self.box.status()
        self.assertEqual(status['web'], before)
        self.assertEqual(self.listening(), [(host, port)])
        self.assertEqual(get(host, port)[0], 200)
        self.assertEqual(status['config']['reload_error']['reason'], 'config_invalid:web.bind')
        self.assertEqual(status['config']['state'], 'ok')  # the running config is the one loaded at start
        self.assertEqual(status['config']['path'], str(self.box.config_file))
        text = self.box.cli('hub', 'status').stdout
        self.assertIn(f'  config: loaded ', text)
        self.assertIn(f'({self.box.config_file})', text)
        self.assertIn("config: 마지막 'wb hub reload' 실패", text)
        self.assertIn('config_invalid:web.bind', text)
        self.assertIsNone(self.hub.poll())
        self.box.write_config('127.0.0.1', port, enabled=True)  # fixed: the error goes away
        self.assertEqual(self.reload().returncode, 0)
        self.assertIsNone(self.box.status()['config']['reload_error'])
        self.assertNotIn('실패', self.box.cli('hub', 'status').stdout.split('config:', 1)[1])


class WebChangeReloadTests(ReloadCase):
    def test_a_new_bind_and_port_answer_the_old_ones_refuse_and_the_session_survives(self):
        self.start()
        host, port = self.box.wait_listening(self.hub.pid)
        session = login(host, port)
        self.assertEqual(get(host, port, '/settings', {'wb_hub_session': session})[0], 200)
        holder = socket.socket()
        holder.bind(('127.0.0.2', 0))
        new_port = holder.getsockname()[1]
        holder.close()
        self.box.write_config('127.0.0.1', 0, enabled=True,
                              raw=f'[web]\nenabled = true\nbind = ["127.0.0.2", "127.0.0.3"]\nport = {new_port}\n')
        result = self.reload()
        self.assertEqual(result.returncode, 0, result)
        self.assertIn('web.bind', result.stdout)
        self.assertIn('web.port', result.stdout)
        self.assertIn(f'http://127.0.0.2:{new_port} http://127.0.0.3:{new_port}', result.stdout)
        self.assertTrue(wait_until(lambda: self.listening() == [('127.0.0.2', new_port), ('127.0.0.3', new_port)], 5),
                        self.listening())
        self.assertTrue(refused(host, port), 'the old address no longer listens')
        for address in ('127.0.0.2', '127.0.0.3'):
            status, _, _ = get(address, new_port, '/settings', {'wb_hub_session': session})
            self.assertEqual(status, 200, f'the login session survives the reload ({address})')
        web = self.box.status()['web']
        self.assertEqual(web['urls'], [f'http://127.0.0.2:{new_port}', f'http://127.0.0.3:{new_port}'])
        self.assertEqual(web['url'], f'http://127.0.0.2:{new_port}')
        text = self.box.cli('hub', 'status')
        self.assertIn(f'web: listening http://127.0.0.2:{new_port} http://127.0.0.3:{new_port}', text.stdout)
        self.assertIsNone(self.hub.poll())

    def test_nothing_changed_is_a_no_op_for_the_listener(self):
        self.start()
        host, port = self.box.wait_listening(self.hub.pid)
        inode = self.listening()
        result = self.reload()
        self.assertEqual(result.returncode, 0, result)
        self.assertIn('바뀐 항목이 없어', result.stdout)
        reply = self.hub_reload()
        self.assertEqual(reply['changed'], [])
        self.assertEqual(self.listening(), inode)
        self.assertEqual(get(host, port)[0], 200)

    def test_a_port_in_use_is_reported_and_the_hub_keeps_working(self):
        self.start()
        host, port = self.box.wait_listening(self.hub.pid)
        holder = socket.socket()
        holder.bind(('127.0.0.1', 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        busy = holder.getsockname()[1]
        self.box.write_config('127.0.0.1', busy, enabled=True)
        result = self.reload()
        self.assertEqual(result.returncode, 1, result)
        self.assertIn(f'bind_failed:EADDRINUSE 127.0.0.1:{busy}', result.stdout)
        self.assertRegex(result.stderr, r'[가-힣]')
        self.assertEqual(self.box.status()['web']['state'], 'bind_failed')
        self.assertTrue(refused(host, port), 'no rollback to the old port')
        self.assertIsNone(self.hub.poll())
        self.assertEqual(self.box.cli('hub', 'list', '--json').returncode, 0)


class AllowFromReloadTests(ReloadCase):
    def test_an_allow_from_change_takes_effect_for_new_requests(self):
        self.start()
        host, port = self.box.wait_listening(self.hub.pid)
        self.assertEqual(get(host, port)[0], 200)
        self.box.write_config('127.0.0.1', port, 'allow_from = ["10.0.0.0/8"]', enabled=True)
        result = self.reload()
        self.assertEqual(result.returncode, 0, result)
        self.assertEqual(json.loads(json.dumps(self.hub_reload()['changed'])), [])  # second reload: no change
        self.assertTrue(wait_until(lambda: not refused(host, port), 5))
        status, headers, body = get(host, port)
        self.assertEqual((status, body), (403, ''))
        self.assertNotIn('Set-Cookie', headers)
        self.box.write_config('127.0.0.1', port, 'allow_from = ["127.0.0.1", "::1"]', enabled=True)
        self.assertEqual(self.reload().returncode, 0)
        self.assertTrue(wait_until(lambda: not refused(host, port), 5))
        self.assertEqual(get(host, port)[0], 200)
        self.assertIn('allow_from: 127.0.0.1/32, ::1/128', self.box.cli('hub', 'status').stdout)


FAKE_OMP = Path(__file__).resolve().parent / 'fixtures' / 'fake_omp_board.py'


def alive(pid: int) -> bool:
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ('Z', 'X')


def descendants(pid: int) -> set[int]:
    """Every live process below ``pid`` (from ``/proc/<n>/stat`` parent links)."""
    parents = {}
    for name in os.listdir('/proc'):
        if name.isdigit():
            try:
                parents[int(name)] = int(Path(f'/proc/{name}/stat').read_text().rsplit(') ', 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
    found, frontier = set(), {pid}
    while frontier:
        frontier = {child for child, parent in parents.items() if parent in frontier and child not in found}
        found |= frontier
    return {child for child in found if alive(child)}


class BoardReloadTests(ReloadCase):
    """The fake ``omp`` as the Board agent and a sleeping script as ``board_agent.shell`` (as ``test_board_stop``)."""

    def board_config(self, enabled: bool) -> None:
        self.box.write_config(raw=textwrap.dedent(f'''\
            [web]
            enabled = {"true" if enabled else "false"}
            bind = "127.0.0.1"
            port = {self.port}
            [board_agent]
            shell = "{self.shell}"
            '''))

    def setUp(self):
        super().setUp()
        box = self.box
        store = box.home / '.omp' / 'agent' / 'agent.db'
        store.parent.mkdir(parents=True)
        store.write_bytes(b'')  # placeholder user auth store: linked, never opened
        bin_dir = box.base / 'bin'
        bin_dir.mkdir()
        omp = bin_dir / 'omp'
        omp.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_OMP} "$@"\n')
        omp.chmod(0o755)
        self.shell = bin_dir / 'board-shell'
        self.shell.write_text('#!/bin/sh\nexec sleep 600\n')
        self.shell.chmod(0o755)
        box.env.update({'WORKBENCH_OMP': str(omp), 'FAKE_BOARD_RECORD': str(box.base / 'board-record.jsonl')})
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        self.port = probe.getsockname()[1]
        probe.close()

    def browser(self):
        from test_web_requests_hub import Browser
        browser = Browser('127.0.0.1', self.port)
        _, _, page = browser.request('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        status, _, _ = browser.request('POST', '/login', {'login_token': token, 'password': PASSWORD})
        self.assertEqual(status, 303, 'login')
        return browser

    def start_set(self, browser) -> dict[str, int]:
        for channel in ('agent', 'shell'):
            _, _, page = browser.request('GET', '/settings')
            csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
            status, _, _ = browser.request('POST', '/board/restart', {'csrf': csrf, 'channel': channel})
            self.assertEqual(status, 303, channel)
        self.assertTrue(wait_until(lambda: all((self.box.status()['board_agent'] or {}).get(name, {}).get('state')
                                               == 'running' for name in ('agent', 'shell')), 15),
                        self.box.status()['board_agent'])
        board = self.box.status()['board_agent']
        return {name: board[name]['pid'] for name in ('agent', 'shell')}

    def test_enabling_by_reload_opens_the_listener_and_the_board_set(self):
        self.board_config(False)
        self.box.set_password()
        self.hub = self.box.start_hub()
        time.sleep(2.5)  # past a watchdog poll
        self.assertEqual(self.listening(), [])
        self.assertIsNone(self.box.status()['board_agent'])
        self.board_config(True)
        result = self.reload()
        self.assertEqual(result.returncode, 0, result)
        self.assertIn('web.enabled', result.stdout)
        self.assertEqual(self.box.wait_listening(self.hub.pid), ('127.0.0.1', self.port))
        pids = self.start_set(self.browser())  # 303, not 503 board_unavailable: the set exists
        self.assertTrue(all(alive(pid) for pid in pids.values()))

    def test_disabling_keeps_a_running_set_and_re_enabling_reuses_it(self):
        self.board_config(True)
        self.box.set_password()
        self.hub = self.box.start_hub()
        self.box.wait_listening(self.hub.pid)
        pids = self.start_set(self.browser())
        before = descendants(self.hub.pid)
        self.board_config(False)
        result = self.reload()
        self.assertEqual(result.returncode, 0, result)
        self.assertIn("'wb hub stop'", result.stdout)
        self.assertIn('web: disabled', result.stdout)
        # The Board agent's own internal REST listener (127.0.0.1, ephemeral) stays with the running set.
        self.assertTrue(wait_until(lambda: ('127.0.0.1', self.port) not in self.listening(), 5), self.listening())
        self.assertTrue(refused('127.0.0.1', self.port))
        internal = self.listening()
        self.assertEqual(len(internal), 1, internal)
        board = self.box.status()['board_agent']
        self.assertEqual({name: board[name]['pid'] for name in ('agent', 'shell')}, pids)
        self.assertEqual({board[name]['state'] for name in ('agent', 'shell')}, {'running'})
        self.assertTrue(all(alive(pid) for pid in pids.values()))
        self.board_config(True)
        reply = self.hub_reload()
        self.assertTrue(reply['ok'], reply)
        self.assertTrue(wait_until(lambda: self.listening() == sorted([*internal, ('127.0.0.1', self.port)]), 5),
                        self.listening())
        board = self.box.status()['board_agent']
        self.assertEqual({name: board[name]['pid'] for name in ('agent', 'shell')}, pids)
        self.assertEqual(descendants(self.hub.pid), before, 'no second Board agent set')
        self.assertIn(f'board_agent: agent running(pid {pids["agent"]}) · shell running(pid {pids["shell"]})',
                      self.box.cli('hub', 'status').stdout)

    def test_a_board_agent_change_is_said_to_apply_at_the_next_start(self):
        self.board_config(True)
        self.box.set_password()
        self.hub = self.box.start_hub()
        self.box.wait_listening(self.hub.pid)
        self.box.write_config(raw=self.box.config_file.read_text() + 'model = "other-model"\n')
        result = self.reload()
        self.assertEqual(result.returncode, 0, result)
        self.assertIn('board_agent.model', result.stdout)
        self.assertIn('다음 Board agent 시작', result.stdout)
        self.assertEqual(self.listening(), [('127.0.0.1', self.port)])  # the web listener stays as it was


CONTRACT_BACKEND = Path(__file__).resolve().parent / 'fixtures' / 'contract_backend.py'


def start_ticks(pid: int) -> int:
    return int(Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])


class ProjectsUntouchedTests(ReloadCase):
    """A registered "on" project (the contract fixture backend, which logs every frame it receives)."""

    def test_reload_never_contacts_starts_or_stops_a_project(self):
        from workbench.backend.project import initialize_project, resolve_project_context
        root = (self.box.base / 'work' / 'alpha')
        root.mkdir(parents=True)
        root = root.resolve()
        initialize_project(resolve_project_context(root, self.box.env), confirm=lambda _root: True,
                           interactive=True, environment=self.box.env)
        frames = self.box.base / 'frames.jsonl'
        backend = subprocess.Popen([sys.executable, str(CONTRACT_BACKEND), str(root), '--frames-log', str(frames)],
                                   env=self.box.env, cwd=self.box.base, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                   start_new_session=True)
        self.addCleanup(lambda: (backend.poll() is None and backend.kill(), backend.wait(5), backend.stdout.close()))
        self.assertEqual(backend.stdout.readline().strip(), 'ready')
        identity = (backend.pid, start_ticks(backend.pid))
        self.start()
        self.box.wait_listening(self.hub.pid)
        self.assertEqual(self.box.cli('hub', 'add', str(root)).returncode, 0)
        listed = json.loads(self.box.cli('hub', 'list', '--json').stdout)
        self.assertEqual([row['power'] for row in listed['rows']], ['on'])
        time.sleep(0.5)
        seen = frames.read_text().splitlines() if frames.exists() else []
        for raw in ('[web]\nenabled = true\nbind = "127.0.0.2"\nport = 0\n',
                    '[web]\nenabled = false\n', '[web]\nenabled = true\nbind = "127.0.0.1"\nport = 0\n',
                    '[web]\nbind = "localhost"\n'):
            with self.subTest(config=raw):
                self.box.write_config(raw=raw)
                self.reload()
        time.sleep(2.5)  # past a watchdog tick
        after = frames.read_text().splitlines() if frames.exists() else []
        self.assertEqual(after, seen, 'the Hub sent the project nothing during the reloads')
        self.assertIsNone(backend.poll())
        self.assertEqual((backend.pid, start_ticks(backend.pid)), identity)
        self.assertEqual(json.loads((root / '.wb' / 'backend.json').read_text())['processes']['backend']['pid'],
                         backend.pid)
        registry = json.loads((self.box.state / 'registry.json').read_text())
        self.assertEqual([entry['root'] for entry in registry['projects']], [str(root)])
        self.assertEqual([row['power'] for row in json.loads(self.box.cli('hub', 'list', '--json').stdout)['rows']],
                         ['on'])


if __name__ == '__main__':
    unittest.main()
