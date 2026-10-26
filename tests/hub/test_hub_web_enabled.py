"""2026-10-26 user decision: the web surface is off until ``hub.toml`` says ``[web] enabled = true``.

Seams: the ``hub.toml`` parse API, and a real ``wb _hub --foreground`` in the shared web sandbox (T3)
observed through its own LISTEN sockets (``/proc/net/tcp*``), a plain TCP connect, its child
processes and ``wb hub status``/``wb hub passwd`` output. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from workbench.hub import config  # noqa: E402
from fixtures.web_sandbox import PASSWORD, WebSandbox, tcp_listening  # noqa: E402


def children(pid: int) -> list[int]:
    """Pids whose parent is ``pid`` (from ``/proc/<pid>/stat``)."""
    found = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            text = Path(f'/proc/{name}/stat').read_text()
        except OSError:
            continue
        fields = text[text.rindex(')') + 2:].split()
        if int(fields[1]) == pid:
            found.append(int(name))
    return found


def unused_port() -> int:
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class WebEnabledConfigTests(unittest.TestCase):
    def parse(self, text: str) -> config.HubConfig:
        return config.parse_config(text.encode('utf-8'))

    def test_web_is_disabled_by_default(self):
        self.assertIs(config.HubConfig().web.enabled, False)
        self.assertIs(self.parse('').web.enabled, False)
        self.assertIs(self.parse('[web]\nbind = "127.0.0.1"\nport = 0\n').web.enabled, False)

    def test_true_enables_and_false_is_accepted(self):
        self.assertIs(self.parse('[web]\nenabled = true\n').web.enabled, True)
        self.assertIs(self.parse('[web]\nenabled = false\n').web.enabled, False)

    def test_a_non_bool_enabled_is_config_invalid(self):
        for value in ('"true"', '1', '0', '"yes"', '[]', '{}'):
            with self.subTest(value=value):
                with self.assertRaises(config.ConfigInvalid) as caught:
                    self.parse(f'[web]\nenabled = {value}\n')
                self.assertEqual(caught.exception.reason, 'config_invalid:web.enabled')


class WebDisabledHubTests(unittest.TestCase):
    """A real foreground Hub with a password set (T3)."""

    def setUp(self):
        self.box = WebSandbox(self)

    def assert_web_closed(self, hub, port: int) -> None:
        time.sleep(2.5)  # past one watchdog poll: a wrong implementation would have opened by now
        self.assertEqual(tcp_listening(hub.pid), [])
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(('127.0.0.1', port), timeout=2).close()

    def test_without_the_enabled_key_the_web_port_stays_closed_and_no_board_set_exists(self):
        port = unused_port()
        self.box.write_config('127.0.0.1', port, enabled=None)
        self.box.set_password()
        hub = self.box.start_hub()
        self.assert_web_closed(hub, port)
        status = self.box.status()
        self.assertEqual(status['web']['state'], 'disabled')
        self.assertIsNone(status['board_agent'])
        self.assertEqual(children(hub.pid), [])
        self.assertEqual(status['registry']['state'], 'ok')  # the rest of the Hub works
        listed = self.box.cli('hub', 'list', '--json')
        self.assertEqual(listed.returncode, 0, listed)
        self.assertIsNone(hub.poll())

    def test_no_hub_toml_at_all_keeps_the_web_closed(self):
        self.box.set_password()
        self.box.config_file.unlink()  # passwd created the template (2026-10-26); start with no hub.toml at all
        hub = self.box.start_hub()
        time.sleep(2.5)
        self.assertEqual(tcp_listening(hub.pid), [])
        status = self.box.status()
        self.assertEqual(status['web']['state'], 'disabled')
        self.assertIsNone(status['board_agent'])

    def test_enabled_false_keeps_the_web_closed(self):
        port = unused_port()
        self.box.write_config('127.0.0.1', port, enabled=False)
        self.box.set_password()
        hub = self.box.start_hub()
        self.assert_web_closed(hub, port)
        self.assertEqual(self.box.status()['web']['state'], 'disabled')

    def test_enabled_true_with_a_password_opens_the_listener(self):
        self.box.write_config('127.0.0.1', 0, enabled=True)
        self.box.set_password()
        hub = self.box.start_hub()
        host, port = self.box.wait_listening(hub.pid)
        self.assertEqual(host, '127.0.0.1')
        socket.create_connection(('127.0.0.1', port), timeout=5).close()
        status = self.box.status()
        self.assertEqual(status['web']['state'], 'listening')
        self.assertEqual(status['web']['url'], f'http://127.0.0.1:{port}')
        self.assertIsNone(hub.poll())



ENABLE_KEY = '[web] enabled = true'


class WebStatusTextTests(unittest.TestCase):
    """``wb hub status`` (text and JSON) of a real Hub (T3)."""

    def setUp(self):
        self.box = WebSandbox(self)

    def test_disabled_web_says_so_and_how_to_enable_it(self):
        self.box.write_config('127.0.0.1', 0, enabled=None)
        self.box.set_password()
        self.box.start_hub()
        self.assertEqual(self.box.status()['web'], {'state': 'disabled', 'config': str(self.box.config_file)})
        text = self.box.cli('hub', 'status')
        self.assertEqual(text.returncode, 0, text)
        lines = [line for line in text.stdout.splitlines() if line.strip().startswith('web:')]
        self.assertEqual(len(lines), 1, text.stdout)
        line = lines[0]
        self.assertTrue(line.strip().startswith('web: disabled'), line)
        for part in (str(self.box.config_file), ENABLE_KEY, "'wb hub stop'", "'wb hub start'", '재시작'):
            self.assertIn(part, line)
        self.assertNotIn('listening', line)

    def test_enabled_web_shows_the_listener_line_and_no_hint(self):
        self.box.write_config('127.0.0.1', 0, enabled=True)
        self.box.set_password()
        hub = self.box.start_hub()
        _host, port = self.box.wait_listening(hub.pid)
        text = self.box.cli('hub', 'status')
        self.assertEqual(text.returncode, 0, text)
        self.assertIn(f'  web: listening http://127.0.0.1:{port}\n', text.stdout)
        self.assertNotIn(ENABLE_KEY, text.stdout)
        self.assertNotIn('web: disabled', text.stdout)



class PasswdHintTests(unittest.TestCase):
    """``wb hub passwd --stdin`` (no Hub needed) in the web sandbox."""

    def setUp(self):
        self.box = WebSandbox(self)

    def test_with_web_disabled_the_password_is_set_and_one_hint_line_says_web_is_off(self):
        for label, write in (('no hub.toml', lambda: None),
                             ('no enabled key', lambda: self.box.write_config('127.0.0.1', 0, enabled=None)),
                             ('enabled false', lambda: self.box.write_config('127.0.0.1', 0, enabled=False))):
            with self.subTest(label):
                write()
                result = self.box.set_password()
                self.assertTrue((self.box.state / 'auth.json').is_file())
                self.assertIn('비밀번호를 정했습니다.', result.stdout)
                hints = [line for line in result.stdout.splitlines() if ENABLE_KEY in line]
                self.assertEqual(len(hints), 1, result.stdout)
                for part in (str(self.box.config_file), "'wb hub stop'", "'wb hub start'"):
                    self.assertIn(part, hints[0])
                self.assertNotIn('재시작 없이 웹 listener가 열립니다', result.stdout)

    def test_with_web_enabled_no_hint_is_printed(self):
        self.box.write_config('127.0.0.1', 0, enabled=True)
        result = self.box.set_password()
        self.assertIn('비밀번호를 정했습니다.', result.stdout)
        self.assertIn('Hub가 실행 중이면 재시작 없이 웹 listener가 열립니다.', result.stdout)
        self.assertNotIn(ENABLE_KEY, result.stdout)


if __name__ == '__main__':
    unittest.main()
