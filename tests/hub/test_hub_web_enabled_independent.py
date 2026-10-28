"""Independent checks of the 2026-10-26 decision: the Hub opens no web server unless ``[web] enabled = true``.

Derived from the contract only (not from the implementation):

* ``enabled`` must be a TOML bool, default false (also with no ``hub.toml``);
* while disabled the Hub owns no TCP/UDP socket, no child process (no Board agent set, no internal REST
  listener), whatever else happens (password set after the start, ``wb hub tls``, an edited ``hub.toml``);
* the rest of the Hub (status, list) keeps working; ``hub.toml`` is read once at start;
* ``wb hub status`` says ``web: disabled`` with a hint, ``--json`` gives ``{state, config}``;
* enabling and restarting opens the listener with the previous behaviour.

Seams: the ``hub.toml`` parse API, an in-process ``WebListener`` on a real loop, and a real
``wb _hub --foreground`` in the shared web sandbox observed through ``/proc`` and the CLI.
Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from workbench.hub import config  # noqa: E402
from fixtures.web_sandbox import WebSandbox, _socket_inodes, tcp_listening, wait_until  # noqa: E402

POLL_WAIT = 5.0  # > two watchdog ticks (WATCHDOG_INTERVAL = 2 s) and far below BIND_RETRY (30 s)
NET_TABLES = ('tcp', 'tcp6', 'udp', 'udp6', 'raw', 'raw6')


def descendants(pid: int) -> list[int]:
    """Every live descendant of ``pid`` (from ``/proc/<n>/stat`` parent links)."""
    parents = {}
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            text = Path(f'/proc/{name}/stat').read_text()
        except OSError:
            continue
        parents[int(name)] = int(text[text.rindex(')') + 2:].split()[1])
    found, frontier = [], [pid]
    while frontier:
        current = frontier.pop()
        for child, parent in parents.items():
            if parent == current and child not in found:
                found.append(child)
                frontier.append(child)
    return found


def inet_sockets(pid: int) -> list[tuple[str, str]]:
    """``(table, local address)`` of every IP socket (any state, TCP/UDP/raw, v4/v6) ``pid`` holds."""
    inodes = _socket_inodes(pid)
    found = []
    for table in NET_TABLES:
        try:
            rows = Path(f'/proc/{pid}/net/{table}').read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            parts = row.split()
            if len(parts) >= 10 and int(parts[9]) in inodes:
                found.append((table, parts[1]))
    return found


def unused_port() -> int:
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class EnabledKeyParseTests(unittest.TestCase):
    def parse(self, text: str) -> config.HubConfig:
        return config.parse_config(text.encode('utf-8'))

    def test_default_is_false_for_every_way_of_not_saying_it(self):
        self.assertIs(config.HubConfig().web.enabled, False)
        self.assertIs(config.WebConfig().enabled, False)
        for text in ('', '[web]\n', '[board_agent]\nmodel = "x"\n', '[web]\nport = 7\n'):
            with self.subTest(text=text):
                self.assertIs(self.parse(text).web.enabled, False)

    def test_only_a_real_bool_is_accepted(self):
        self.assertIs(self.parse('[web]\nenabled = true\n').web.enabled, True)
        self.assertIs(self.parse('[web]\nenabled = false\n').web.enabled, False)
        for value in ('"true"', '"false"', '1', '0', '1.0', '[true]', '{ a = true }', '2026-10-26', '"on"'):
            with self.subTest(value=value):
                with self.assertRaises(config.ConfigInvalid) as caught:
                    self.parse(f'[web]\nenabled = {value}\n')
                self.assertEqual(caught.exception.reason, 'config_invalid:web.enabled')
                self.assertEqual(caught.exception.item, 'web.enabled')

    def test_enabled_outside_the_web_table_does_not_enable_anything(self):
        for text in ('enabled = true\n', '[board_agent]\nenabled = true\n', '[web]\nEnabled = true\n'):
            with self.subTest(text=text):
                with self.assertRaises(config.ConfigInvalid):
                    self.parse(text)

    def test_enabled_does_not_change_the_other_web_keys(self):
        loaded = self.parse('[web]\nenabled = true\nbind = "127.0.0.2"\nport = 12\nallow_insecure_http = false\n')
        self.assertEqual((loaded.web.bind, loaded.web.port, loaded.web.allow_insecure_http), ('127.0.0.2', 12, False))
        self.assertIs(loaded.web.enabled, True)
        default = self.parse('')
        self.assertEqual((default.web.bind, default.web.port, default.web.allow_insecure_http),
                         (config.WebConfig().bind, config.WebConfig().port, True))  # HTTP allowed by default


class InProcessDisabledListenerTests(unittest.IsolatedAsyncioTestCase):
    """``WebListener`` with a disabled config on a real loop: nothing is bound, whatever poll() sees."""

    async def asyncSetUp(self):
        from workbench.hub import auth
        from workbench.hub.web.listener import WebListener
        self.temp = tempfile.TemporaryDirectory(prefix='wb-dis-')
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'hub'
        self.auth = auth
        self.WebListener = WebListener
        self.now = [1000.0]

    def listener(self, text: str):
        loaded = config.parse_config(text.encode())
        decision = config.decide_tls(loaded.web, Path('/nonexistent/c'), Path('/nonexistent/k'))
        web = self.WebListener(self.state, config=loaded, config_error=None, tls=decision,
                               monotonic=lambda: self.now[0], bind_retry=30.0)
        self.addAsyncCleanup(web.stop)
        return web

    async def test_start_and_poll_never_bind_even_with_a_password_and_a_free_port(self):
        port = unused_port()
        for label, text in (('default', f'[web]\nbind = "127.0.0.1"\nport = {port}\n'),
                            ('false', f'[web]\nenabled = false\nbind = "127.0.0.1"\nport = {port}\n')):
            with self.subTest(label):
                self.auth.PasswordStore(self.state).set_password('correct horse battery')
                web = self.listener(text)
                await web.start()
                self.assertEqual(web.status()['state'], 'disabled')
                self.assertFalse(web.listening)
                for _ in range(3):
                    self.now[0] += 31.0  # past the bind retry interval
                    self.auth.PasswordStore(self.state).set_password('another good password')
                    await web.poll()
                    self.assertEqual(web.status()['state'], 'disabled')
                self.assertIsNone(web.runner)
                with self.assertRaises(ConnectionRefusedError):
                    socket.create_connection(('127.0.0.1', port), timeout=2).close()

    async def test_the_same_config_enabled_does_listen(self):
        self.auth.PasswordStore(self.state).set_password('correct horse battery')
        web = self.listener('[web]\nenabled = true\nbind = "127.0.0.1"\nport = 0\n')
        await web.start()
        self.assertEqual(web.status()['state'], 'listening')

    async def test_invalid_config_stays_config_invalid_and_binds_nothing(self):
        self.auth.PasswordStore(self.state).set_password('correct horse battery')
        with self.assertRaises(config.ConfigInvalid) as caught:
            config.parse_config(b'[web]\nenabled = "true"\nport = 0\n')
        web = self.WebListener(self.state, config=None, config_error=caught.exception, tls=None)
        self.addAsyncCleanup(web.stop)
        await web.start()
        await web.poll()
        self.assertEqual(web.status(), {'state': 'config_invalid', 'detail': 'web.enabled'})
        self.assertIsNone(web.runner)


class InProcessHubDisabledTests(unittest.TestCase):
    """``Hub.load_web`` with a real ``hub.toml`` (a scratch ``XDG_CONFIG_HOME``): what the Hub builds at start.

    The Board agent set and its internal REST listener are not visible through ``hub_v1`` while nothing ever
    started them, so their absence is pinned here at the Hub object."""

    def hub(self, toml: str | None):
        from workbench.hub import server
        from workbench.hub.paths import HubLayout
        base = Path(tempfile.mkdtemp(prefix='wb-ih-')).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        environment = {'XDG_CONFIG_HOME': str(base / 'config'), 'HOME': str(base / 'home')}
        if toml is not None:
            (base / 'config' / 'omp-workbench').mkdir(parents=True, mode=0o700)
            (base / 'config' / 'omp-workbench' / 'hub.toml').write_text(toml)
        (base / 'hub').mkdir(mode=0o700)
        hub = server.Hub(HubLayout(base / 'hub'), 'detached')
        self.addCleanup(hub.inbox.close)
        hub.load_web(environment)
        return hub, base / 'config' / 'omp-workbench' / 'hub.toml'

    def test_disabled_builds_no_board_set_and_no_internal_listener(self):
        for label, toml in (('no file', None), ('empty', ''), ('false', '[web]\nenabled = false\n'),
                            ('board table only', '[board_agent]\nshell = "/bin/sh"\n')):
            with self.subTest(label):
                hub, path = self.hub(toml)
                self.assertIsNone(hub.board)
                self.assertIsNone(hub.internal_mcp)
                self.assertEqual(hub.active(), [])
                self.assertEqual(hub.web.stop_hooks, [])
                asyncio.run(hub.web.start())
                self.assertEqual(hub.web.status(), {'state': 'disabled', 'config': str(path)})
                self.assertIsNone(hub.web.runner)

    def test_invalid_enabled_builds_no_board_set_and_no_internal_listener(self):
        for value in ('"true"', '1'):
            with self.subTest(value):
                hub, _path = self.hub(f'[web]\nenabled = {value}\n')
                self.assertIsNone(hub.board)
                self.assertIsNone(hub.internal_mcp)
                asyncio.run(hub.web.start())
                self.assertEqual(hub.web.status(), {'state': 'config_invalid', 'detail': 'web.enabled'})
                self.assertIsNone(hub.web.runner)

    def test_enabled_does_build_the_board_set(self):
        hub, _path = self.hub('[web]\nenabled = true\n')
        self.assertIsNotNone(hub.board)
        self.assertEqual(hub.board.status(), None)  # nothing started yet
        self.assertIsNone(hub.internal_mcp)  # lazily, with the first Board start
        self.assertEqual(len(hub.web.stop_hooks), 1)


class HubProcessCase(unittest.TestCase):
    def setUp(self):
        self.box = WebSandbox(self)

    def assert_no_network_and_no_children(self, hub, label: str = '') -> None:
        self.assertEqual(inet_sockets(hub.pid), [], f'{label}: IP sockets of the Hub')
        self.assertEqual(tcp_listening(hub.pid), [], label)
        self.assertEqual(descendants(hub.pid), [], f'{label}: child processes of the Hub')
        self.assertIsNone(hub.poll(), f'{label}: Hub still alive')

    def assert_rest_of_hub_works(self) -> None:
        status = self.box.status()
        self.assertEqual(status['registry']['state'], 'ok')
        self.assertIsNone(status['board_agent'])
        listed = self.box.cli('hub', 'list', '--json')
        self.assertEqual(listed.returncode, 0, listed)

    def stop_and_wait(self, hub) -> None:
        self.assertEqual(self.box.stop_hub(hub), 0)
        self.assertTrue(wait_until(lambda: not (self.box.state / 'hub.json').exists(), 5))


class DisabledHubTests(HubProcessCase):
    def test_password_and_no_hub_toml_owns_no_socket_and_no_child(self):
        self.box.set_password()
        # 2026-10-26 user request: ``wb hub passwd`` creates the hub.toml template; remove it so the Hub
        # still starts with no hub.toml at all (it creates the template itself, web stays disabled).
        self.box.config_file.unlink()
        self.assertFalse(self.box.config_file.exists())
        hub = self.box.start_hub()
        time.sleep(POLL_WAIT)
        self.assert_no_network_and_no_children(hub)
        status = self.box.status()
        self.assertEqual(status['web'], {'state': 'disabled', 'config': str(self.box.config_file)})
        self.assert_rest_of_hub_works()
        self.assertTrue(list(self.box.runtime.rglob('*')), 'the Hub still serves its unix socket')

    def test_password_set_after_the_start_opens_nothing_even_past_the_poll_interval(self):
        for label, prepare in (('no hub.toml', lambda: None),
                               ('enabled false', lambda: self.box.write_config('127.0.0.1', 0, enabled=False)),
                               ('key absent', lambda: self.box.write_config('127.0.0.1', 0, enabled=None))):
            with self.subTest(label):
                box = WebSandbox(self)
                self.box = box
                prepare()
                hub = box.start_hub()
                self.assertEqual(box.status()['web']['state'], 'disabled')
                box.set_password()  # after the Hub started
                time.sleep(POLL_WAIT)
                self.assert_no_network_and_no_children(hub, label)
                self.assertEqual(box.status()['web']['state'], 'disabled', label)
                box.set_password('another good password')  # a second generation changes auth.json again
                time.sleep(POLL_WAIT)
                self.assert_no_network_and_no_children(hub, label)
                self.assert_rest_of_hub_works()
                self.stop_and_wait(hub)

    def test_editing_hub_toml_to_enabled_while_running_changes_nothing_until_restart(self):
        port = unused_port()
        self.box.write_config('127.0.0.1', port, enabled=False)
        self.box.set_password()
        hub = self.box.start_hub()
        # ``hub.json`` is published before ``hub.toml`` is read: wait until the Hub answers (config is loaded).
        self.assertEqual(self.box.status()['web']['state'], 'disabled')
        self.box.write_config('127.0.0.1', port, enabled=True)  # read once at start: this is ignored
        time.sleep(POLL_WAIT)
        self.assert_no_network_and_no_children(hub)
        self.assertEqual(self.box.status()['web']['state'], 'disabled')
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(('127.0.0.1', port), timeout=2).close()

    def test_disabled_with_tls_pair_a_board_agent_table_and_insecure_flag_still_opens_nothing(self):
        self.box.install_tls()
        self.box.write_config(raw='[web]\nenabled = false\nbind = "127.0.0.1"\nport = 0\n'
                                  'allow_insecure_http = true\n[board_agent]\nshell = "/bin/sh"\n')
        self.box.set_password()
        hub = self.box.start_hub()
        time.sleep(POLL_WAIT)
        self.assert_no_network_and_no_children(hub)
        self.assertEqual(self.box.status()['web']['state'], 'disabled')

    def test_cli_commands_that_touch_web_settings_do_not_open_it(self):
        self.box.write_config('127.0.0.1', 0, enabled=False)
        hub = self.box.start_hub()
        self.box.set_password()
        tls = self.box.cli('hub', 'tls', '--self-signed')
        self.assertEqual(tls.returncode, 0, tls)
        self.box.cli('hub', 'status')
        self.box.cli('hub', 'list')
        time.sleep(POLL_WAIT)
        self.assert_no_network_and_no_children(hub)
        self.assertEqual(self.box.status()['web']['state'], 'disabled')

    def test_stop_of_a_disabled_hub_is_clean(self):
        self.box.set_password()
        hub = self.box.start_hub()
        result = self.box.cli('hub', 'stop', '--yes')
        self.assertEqual(result.returncode, 0, result)
        self.assertEqual(hub.wait(timeout=15), 0)
        self.assertFalse((self.box.state / 'hub.json').exists())


class DisabledStatusTextTests(HubProcessCase):
    def test_text_status_hint_names_the_path_the_key_and_a_restart_in_korean(self):
        self.box.set_password()
        self.box.start_hub()
        text = self.box.cli('hub', 'status')
        self.assertEqual(text.returncode, 0, text)
        lines = [line for line in text.stdout.splitlines() if 'web:' in line]
        self.assertEqual(len(lines), 1, text.stdout)
        line = lines[0]
        self.assertIn('web: disabled', line)
        self.assertIn(str(self.box.config_file), line)  # default path, no hub.toml exists
        self.assertIn('[web] enabled = true', line)
        self.assertIn("'wb hub reload'", line)  # 2026-10-27 contract: applied by reload (was: '재시작')
        self.assertRegex(line, r'[가-힣]')
        self.assertNotIn('not_listening', text.stdout)
        self.assertNotIn('password_not_set', text.stdout)  # the password is set, and disabled wins anyway

    def test_json_status_is_state_and_config_only(self):
        self.box.start_hub()  # without a password too: disabled comes first
        self.assertEqual(self.box.status()['web'], {'state': 'disabled', 'config': str(self.box.config_file)})

    def test_passwd_saves_the_password_and_prints_the_hint_while_disabled(self):
        result = self.box.set_password()
        self.assertTrue((self.box.state / 'auth.json').is_file())
        self.assertIn('[web] enabled = true', result.stdout)
        self.assertIn(str(self.box.config_file), result.stdout)
        self.assertIn("'wb hub reload'", result.stdout)  # 2026-10-27 contract: applied by reload (was: '재시작')

    def test_passwd_hint_goes_away_when_enabled(self):
        self.box.write_config('127.0.0.1', 0, enabled=True)
        result = self.box.set_password()
        self.assertNotIn('[web] enabled = true', result.stdout)
        self.assertNotIn('웹은 꺼져 있습니다', result.stdout)


class InvalidEnabledHubTests(HubProcessCase):
    def test_a_string_or_an_int_is_config_invalid_and_the_web_stays_closed(self):
        for label, value in (('string', '"true"'), ('string false', '"false"'), ('int 1', '1'), ('int 0', '0'),
                             ('float', '1.0')):
            with self.subTest(label):
                box = WebSandbox(self)
                self.box = box
                port = unused_port()
                box.write_config(raw=f'[web]\nenabled = {value}\nbind = "127.0.0.1"\nport = {port}\n')
                box.set_password()
                hub = box.start_hub()
                time.sleep(POLL_WAIT)
                status = box.status()
                self.assertEqual(status['web']['state'], 'config_invalid', status)
                self.assertEqual(status['web']['detail'], 'web.enabled', status)
                self.assertIn('config_invalid', box.cli('hub', 'status').stdout)
                self.assertIn('web.enabled', box.cli('hub', 'status').stdout)
                self.assert_no_network_and_no_children(hub, label)
                with self.assertRaises(ConnectionRefusedError):
                    socket.create_connection(('127.0.0.1', port), timeout=2).close()
                self.assert_rest_of_hub_works()
                self.stop_and_wait(hub)

    def test_a_password_set_later_does_not_open_an_invalid_config_either(self):
        self.box.write_config(raw='[web]\nenabled = 1\nport = 0\n')
        hub = self.box.start_hub()
        self.box.set_password()
        time.sleep(POLL_WAIT)
        self.assertEqual(self.box.status()['web']['state'], 'config_invalid')
        self.assert_no_network_and_no_children(hub)

    def test_passwd_still_saves_the_password_when_enabled_is_invalid(self):
        self.box.write_config(raw='[web]\nenabled = "yes"\n')
        result = self.box.cli('hub', 'passwd', '--stdin', input_text='correct horse battery staple\n')
        self.assertEqual(result.returncode, 0, result)
        self.assertTrue((self.box.state / 'auth.json').is_file())


class EnableAndRestartTests(HubProcessCase):
    def test_enabling_in_hub_toml_and_restarting_opens_the_listener(self):
        self.box.write_config('127.0.0.1', 0, enabled=None)
        self.box.set_password()
        first = self.box.start_hub()
        time.sleep(POLL_WAIT)
        self.assert_no_network_and_no_children(first, 'before')
        self.stop_and_wait(first)

        self.box.write_config('127.0.0.1', 0, enabled=True)
        second = self.box.start_hub()
        host, port = self.box.wait_listening(second.pid)
        self.assertEqual(host, '127.0.0.1')
        socket.create_connection(('127.0.0.1', port), timeout=5).close()
        status = self.box.status()['web']
        self.assertEqual((status['state'], status['url']), ('listening', f'http://127.0.0.1:{port}'))
        self.assertTrue(status['insecure_warning'] in (True, False))
        text = self.box.cli('hub', 'status').stdout
        self.assertNotIn('web: disabled', text)
        self.assertNotIn('[web] enabled = true', text)

    def test_enabled_keeps_the_previous_password_rules(self):
        self.box.write_config('127.0.0.1', 0, enabled=True)
        hub = self.box.start_hub()
        time.sleep(POLL_WAIT)
        self.assertEqual(tcp_listening(hub.pid), [])
        status = self.box.status()['web']
        self.assertEqual(status['state'], 'password_not_set')
        self.assertIn('wb hub passwd', self.box.cli('hub', 'status').stdout)
        self.box.set_password()  # opens without a restart, as before
        host, port = self.box.wait_listening(hub.pid, timeout=12)
        self.assertEqual(self.box.status()['web']['state'], 'listening')
        socket.create_connection((host, port), timeout=5).close()

    def test_enabled_with_a_tls_pair_serves_https_as_before(self):
        self.box.install_tls()
        self.box.write_config('127.0.0.1', 0, enabled=True)
        self.box.set_password()
        hub = self.box.start_hub()
        _host, port = self.box.wait_listening(hub.pid)
        status = self.box.status()['web']
        self.assertEqual(status['state'], 'listening')
        self.assertIs(status['tls'], True)
        self.assertEqual(status['url'], f'https://127.0.0.1:{port}')


if __name__ == '__main__':
    unittest.main()
