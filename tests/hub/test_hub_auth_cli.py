"""``wb hub passwd [--stdin]`` (TI-02, seam T8): real CLI subprocesses in a temp HOME/XDG sandbox.

The sandbox keeps Hub auto-start *enabled* (``SpawnSandbox``) so "no Hub is started" is a real
check: ``passwd`` must be exempt. TTY runs use ``pty.fork`` so the child has a controlling
terminal and ``getpass`` reads ``/dev/tty`` with echo off. Expected values: the SPEC record
schema and a hash the test computes with ``hashlib.scrypt``; modes via ``os.lstat``.
Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import stat
import subprocess
import time
import unittest

from test_hub_spawn import SpawnSandbox

PASSWORD = 'a long enough passphrase'


def scrypt_b64(password: str, salt_b64: str) -> str:
    digest = hashlib.scrypt(password.encode('utf-8'), salt=base64.b64decode(salt_b64), n=32768, r=8, p=1,
                            dklen=32, maxmem=64 * 1024 * 1024)
    return base64.b64encode(digest).decode('ascii')


class PasswdSandbox(SpawnSandbox):
    @property
    def auth_path(self) -> Path:
        return self.state / 'auth.json'

    def auth(self) -> dict:
        return json.loads(self.auth_path.read_text())

    def passwd_stdin(self, data: bytes, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(self.wb_argv('hub', 'passwd', '--stdin', *extra), env=self.env, cwd=self.base,
                              input=data, capture_output=True, timeout=40)

    def passwd_tty(self, answers: list[str], timeout: float = 40) -> tuple[int, bytes]:
        """Run ``wb hub passwd`` on a PTY, answering each prompt in turn; returns (exit code, output)."""
        child, master = pty.fork()
        if child == 0:  # pragma: no cover - exec'd immediately
            try:
                os.chdir(self.base)
                os.execve(self.wb_argv()[0], self.wb_argv('hub', 'passwd'), self.env)
            finally:
                os._exit(127)
        output, pending = b'', list(answers)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 0.2)[0]:
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if pending and output.rstrip().endswith(b':'):
                    os.write(master, pending.pop(0).encode() + b'\n')
        finally:
            os.close(master)
        if time.monotonic() >= deadline:
            os.kill(child, signal.SIGKILL)
        _, status = os.waitpid(child, 0)
        return (os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1), output


class PasswdStdinTests(unittest.TestCase):
    def setUp(self):
        self.box = PasswdSandbox(self)

    def test_stdin_line_sets_a_private_scrypt_password_without_starting_a_hub(self):
        result = self.box.passwd_stdin(PASSWORD.encode() + b'\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.box.auth()
        self.assertEqual((record['kdf'], record['n'], record['r'], record['p'], record['dklen']),
                         ('scrypt', 32768, 8, 1, 32))
        self.assertEqual(record['hash'], scrypt_b64(PASSWORD, record['salt']))
        self.assertEqual(stat.S_IMODE(os.lstat(self.box.auth_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(self.box.state).st_mode), 0o700)
        self.assertNotIn(PASSWORD.encode(), result.stdout + result.stderr)
        first = record['generation']

        again = self.box.passwd_stdin(b'second password here')  # no trailing newline
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.box.auth()['generation'], first + 1)
        self.assertEqual(self.box.auth()['hash'], scrypt_b64('second password here', self.box.auth()['salt']))

        self.assertEqual(self.box.hub_pids(), [], 'passwd never starts a Hub')
        self.assertFalse(self.box.lock.exists(), 'no Hub lock was taken')
        self.assertFalse((self.box.state / 'hub.sqlite3').exists(), 'the CLI never writes the Hub DB')

    def test_empty_short_or_missing_input_is_refused_and_changes_nothing(self):
        for data in (b'', b'\n', b'short\n', b'123456789\n'):
            with self.subTest(data=data):
                result = self.box.passwd_stdin(data)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(self.box.auth_path.exists())
        self.assertEqual(self.box.passwd_stdin(PASSWORD.encode() + b'\n').returncode, 0)
        before = self.box.auth_path.read_bytes()
        for data in (b'\n', b'short\n', b'x' * 1025 + b'\n', b'\xff\xfe invalid utf-8 bytes\n'):
            with self.subTest(data=data[:20]):
                result = self.box.passwd_stdin(data)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(self.box.auth_path.read_bytes(), before)
        self.assertEqual(self.box.hub_pids(), [])

    def test_only_the_first_line_is_the_password(self):
        result = self.box.passwd_stdin(PASSWORD.encode() + b'\nsomething else\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.box.auth()['hash'], scrypt_b64(PASSWORD, self.box.auth()['salt']))

    def test_without_a_terminal_or_stdin_flag_nothing_is_asked_or_written(self):
        result = subprocess.run(self.box.wb_argv('hub', 'passwd'), env=self.box.env, cwd=self.box.base,
                                input=PASSWORD.encode() + b'\n' + PASSWORD.encode() + b'\n', capture_output=True,
                                timeout=40)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('--stdin', result.stderr.decode())
        self.assertFalse(self.box.auth_path.exists())
        self.assertFalse(self.box.state.exists(), 'no state is created')

    def test_the_password_is_never_taken_from_argv(self):
        result = subprocess.run(self.box.wb_argv('hub', 'passwd', PASSWORD), env=self.box.env, cwd=self.box.base,
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.box.auth_path.exists())


class PasswdTtyTests(unittest.TestCase):
    def setUp(self):
        self.box = PasswdSandbox(self)

    def test_two_matching_entries_set_the_password_without_echo(self):
        code, output = self.box.passwd_tty([PASSWORD, PASSWORD])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.box.auth()['hash'], scrypt_b64(PASSWORD, self.box.auth()['salt']))
        self.assertNotIn(PASSWORD.encode(), output, 'getpass turns echo off')
        self.assertEqual(self.box.hub_pids(), [])

    def test_mismatched_entries_are_refused_and_change_nothing(self):
        code, output = self.box.passwd_tty([PASSWORD, PASSWORD + 'x'])
        self.assertEqual(code, 1, output)
        self.assertFalse(self.box.auth_path.exists())
        self.assertEqual(self.box.passwd_stdin(PASSWORD.encode()).returncode, 0)
        before = self.box.auth_path.read_bytes()
        code, output = self.box.passwd_tty(['another password', 'another passworD'])
        self.assertEqual(code, 1, output)
        self.assertEqual(self.box.auth_path.read_bytes(), before)

    def test_a_short_tty_password_is_refused(self):
        code, output = self.box.passwd_tty(['short', 'short'])
        self.assertEqual(code, 1, output)
        self.assertFalse(self.box.auth_path.exists())


if __name__ == '__main__':
    unittest.main()
