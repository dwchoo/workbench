"""``wb hub passwd`` with a non-ASCII (Hangul) password (seam T8: real CLI subprocesses, a PTY for the terminal).

Browsers often turn the IME off in a password field, so a Hangul password set here may be impossible to type at
the web login. The terminal flow warns and asks again; ``--stdin`` (no one to ask) warns and goes on. The hash is
unchanged (scrypt over UTF-8). Expected values: the warning text of the orchestrator decision and a hash the test
computes. Run with ``discover -s tests/hub -p 'test_hub_auth_cli_hangul.py'``.
"""
from __future__ import annotations

import unittest

import test_hub_auth_cli as cli_case  # module import: its test classes stay uncollected here

WARNING = '웹 브라우저의 비밀번호 칸은 한글 입력(IME)을 끄는 경우가 많아 로그인하지 못할 수 있습니다. 계속할까요?'
HANGUL = '한글 비밀번호 열쇠'


class PasswdHangulTests(unittest.TestCase):
    def setUp(self):
        self.box = cli_case.PasswdSandbox(self)

    def test_a_hangul_password_on_the_terminal_is_set_only_after_a_yes(self):
        code, output = self.box.passwd_tty([HANGUL, HANGUL, 'n'])
        self.assertEqual(code, 1, output.decode())
        self.assertIn(WARNING.encode(), output)
        self.assertFalse(self.box.auth_path.exists())
        code, output = self.box.passwd_tty([HANGUL, HANGUL, 'y'])
        self.assertEqual(code, 0, output.decode())
        self.assertIn(WARNING.encode(), output)
        self.assertEqual(self.box.auth()['hash'], cli_case.scrypt_b64(HANGUL, self.box.auth()['salt']))

    def test_an_ascii_password_is_not_asked_about(self):
        code, output = self.box.passwd_tty([cli_case.PASSWORD, cli_case.PASSWORD])
        self.assertEqual(code, 0, output.decode())
        self.assertNotIn('IME'.encode(), output)

    def test_stdin_warns_and_sets_the_password(self):
        done = self.box.passwd_stdin((HANGUL + '\n').encode())
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        self.assertIn(WARNING.split(' 계속할까요?')[0], done.stderr.decode())
        self.assertEqual(self.box.auth()['hash'], cli_case.scrypt_b64(HANGUL, self.box.auth()['salt']))


if __name__ == '__main__':
    unittest.main()
