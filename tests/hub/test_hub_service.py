"""``wb hub enable|disable|passwd|tls``, ``wb --version`` and systemd mode (HB-10, seam S1).

Every ``wb`` is a real CLI subprocess in a temp HOME/XDG sandbox. ``PATH`` holds only a
temp directory with fake ``systemctl``/``loginctl`` scripts that record their argv (and
whether the unit file existed at call time) -- the real ``systemctl --user`` and the real
``~/.config/systemd/user`` are never touched. Expected values come from the fake's log,
the unit bytes the test reads back, and dist-info the test builds itself.
Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import unittest

from test_hub_process import REPO, flock_holder, wait_until
from test_hub_spawn import SpawnSandbox

FAKE_SYSTEMCTL = """#!/bin/sh
unit="$XDG_CONFIG_HOME/systemd/user/omp-workbench-hub.service"
state=absent
[ -e "$unit" ] && state=present
printf 'systemctl\\t%s\\t%s\\n' "$*" "$state" >> "$FAKE_LOG"
if [ -n "$FAKE_HANG" ]; then
  case "$*" in *"$FAKE_HANG"*) exec sleep 15 ;; esac
fi
if [ -n "$FAKE_FAIL" ]; then
  case "$*" in *"$FAKE_FAIL"*) echo "fake systemctl: $FAKE_FAIL failed" >&2; exit 1 ;; esac
fi
case "$*" in
  "--user is-enabled "*) echo "${FAKE_IS_ENABLED:-disabled}"; [ "${FAKE_IS_ENABLED:-disabled}" = enabled ] && exit 0; exit 1 ;;
esac
exit 0
"""

FAKE_LOGINCTL = """#!/bin/sh
printf 'loginctl\\t%s\\t-\\n' "$*" >> "$FAKE_LOG"
if [ "$FAKE_LINGER" = broken ]; then echo "no session" >&2; exit 1; fi
echo "Linger=${FAKE_LINGER:-no}"
"""

UNIT_NAME = 'omp-workbench-hub.service'
RESERVED_TEXT = 'Web Board 기능에서 제공 예정입니다'


def systemd_split(line: str) -> list[str]:
    """Independent reader of one systemd ExecStart= value (double quotes, backslash escapes)."""
    words, current, in_word, index = [], [], False, 0
    while index < len(line):
        char = line[index]
        if char == '"':
            in_word = True
            index += 1
            while line[index] != '"':
                if line[index] == '\\':
                    index += 1
                    current.append({'n': '\n', 't': '\t'}.get(line[index], line[index]))
                else:
                    current.append(line[index])
                index += 1
            index += 1
        elif char == ' ':
            if in_word:
                words.append(''.join(current))
                current, in_word = [], False
            index += 1
        else:
            in_word = True
            current.append(char)
            index += 1
    if in_word:
        words.append(''.join(current))
    return [word.replace('%%', '%').replace('$$', '$') for word in words]


class ServiceSandbox(SpawnSandbox):
    """SpawnSandbox whose PATH is only a fake-bin dir (no real systemctl is reachable)."""

    def __init__(self, case: unittest.TestCase, *, systemctl: bool = True):
        super().__init__(case)
        self.bin = self.base / 'fakebin'
        self.bin.mkdir()
        self.log = self.base / 'fake.log'
        self.log.touch()
        if systemctl:
            self.install('systemctl', FAKE_SYSTEMCTL)
        self.install('loginctl', FAKE_LOGINCTL)
        (self.bin / 'sleep').symlink_to('/bin/sleep')  # for FAKE_HANG
        self.env.update({'PATH': str(self.bin), 'FAKE_LOG': str(self.log)})

    def install(self, name: str, text: str) -> None:
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    @property
    def unit(self) -> Path:
        return self.config / 'systemd' / 'user' / UNIT_NAME

    def calls(self) -> list[tuple[str, str, str]]:
        return [tuple(line.split('\t')) for line in self.log.read_text().splitlines()]

    def systemctl_calls(self) -> list[tuple[str, str]]:
        return [(args, unit) for tool, args, unit in self.calls() if tool == 'systemctl']

    def with_env(self, **extra: str) -> dict:
        return dict(self.env, **extra)


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.box = ServiceSandbox(self)

    def make_dist(self, version: str, direct_url: str | None) -> dict:
        """A site dir whose ``workbench`` is the repo package plus a dist-info the test wrote."""
        site = self.box.base / 'site'
        site.mkdir()
        (site / 'workbench').symlink_to(REPO / 'src' / 'workbench')
        info = site / f'workbench-{version}.dist-info'
        info.mkdir()
        (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: workbench\nVersion: {version}\n')
        if direct_url is not None:
            (info / 'direct_url.json').write_text(direct_url)
        return self.box.with_env(PYTHONPATH=str(site))

    def test_version_prints_release_and_commit_prefix_and_starts_no_hub(self):
        commit = 'abcdef0123456789abcdef0123456789abcdef01'
        env = self.make_dist('1.2.3', '{"url": "https://example.invalid/wb", '
                                      f'"vcs_info": {{"vcs": "git", "commit_id": "{commit}"}}}}')
        result = self.box.wb('--version', env=env)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, f'wb 1.2.3 ({commit[:12]})\n', ''))
        self.assertEqual(self.box.hub_pids(), [])
        self.assertFalse(self.box.state.exists(), 'no Hub state is created')

    def test_version_of_an_editable_install(self):
        env = self.make_dist('0.9.0', '{"url": "file:///src", "dir_info": {"editable": true}}')
        result = self.box.wb('--version', env=env)
        self.assertEqual((result.returncode, result.stdout), (0, 'wb 0.9.0 (editable)\n'))

    def test_version_without_a_matching_distribution_is_unknown(self):
        result = self.box.wb('--version')  # PYTHONPATH=src: no dist-info describes this code
        self.assertEqual((result.returncode, result.stdout), (0, 'wb unknown (unknown)\n'))
        self.assertEqual(self.box.hub_pids(), [])


class ReservedCommandTests(unittest.TestCase):
    def setUp(self):
        self.box = ServiceSandbox(self)

    def test_passwd_and_tls_are_refused_by_name_and_start_nothing(self):
        for name in ('passwd', 'tls'):
            with self.subTest(name):
                result = self.box.wb('hub', name)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(RESERVED_TEXT, result.stdout + result.stderr)
        self.assertEqual(self.box.hub_pids(), [])
        self.assertFalse(self.box.state.exists())
        self.assertEqual(self.box.calls(), [], 'a reserved name never runs systemctl')


def unit_directives(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in text.splitlines():
        if '=' in line and not line.startswith('['):
            key, value = line.split('=', 1)
            values.setdefault(key, []).append(value)
    return values


class EnableTests(unittest.TestCase):
    def setUp(self):
        self.box = ServiceSandbox(self)

    def test_nothing_creates_a_unit_by_default(self):
        self.assertFalse(self.box.unit.parent.exists())
        for args in (('hub', 'status'), ('--version',), ('hub', 'passwd')):
            self.box.wb(*args)
        self.assertFalse(self.box.unit.parent.exists())
        self.assertNotIn('enable', ' '.join(args for args, _ in self.box.systemctl_calls()))

    def test_enable_calls_systemctl_in_order_around_the_unit_write(self):
        result = self.box.wb('hub', 'enable')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.box.systemctl_calls(),
                         [('--user show-environment', 'absent'),
                          ('--user daemon-reload', 'present'),
                          ('--user enable ' + UNIT_NAME, 'present')])
        self.assertEqual(self.box.hub_pids(), [])
        self.assertFalse(self.box.state.exists(), 'enable never starts a Hub')

    def test_unit_runs_this_python_hub_in_systemd_mode_and_restores_the_argv(self):
        self.box.wb('hub', 'enable')
        text = self.box.unit.read_text()
        directives = unit_directives(text)
        self.assertEqual(directives['Type'], ['simple'])
        self.assertEqual(directives['Restart'], ['on-failure'])
        self.assertEqual(directives['RestartSec'], ['5'])
        self.assertEqual(directives['RestartPreventExitStatus'], ['3'],
                         'a Hub that refuses permanently (managed tree, exit 3) is not restarted every 5 s')
        self.assertEqual(directives['WantedBy'], ['default.target'])
        self.assertEqual(len(directives['ExecStart']), 1)
        argv = systemd_split(directives['ExecStart'][0])
        self.assertEqual(argv[:3], [sys.executable, '-I', '-c'])
        self.assertTrue(argv[3].startswith('import sys; sys.path.insert(0, '), argv[3])
        self.assertIn(str(REPO / 'src'), argv[3])
        self.assertEqual(argv[4:], ['_hub', '--foreground', '--mode', 'systemd'])
        self.assertEqual(sorted(path.name for path in self.box.unit.parent.iterdir()), [UNIT_NAME],
                         'no temp file is left beside the unit')


class ExecStartQuotingTests(unittest.TestCase):
    """The rendered ExecStart round-trips through an independent systemd-style reader."""

    def test_awkward_arguments_survive_the_unit_text(self):
        sys.path.insert(0, str(REPO / 'src'))
        self.addCleanup(sys.path.remove, str(REPO / 'src'))
        from workbench.hub.service_unit import render_unit
        argv = ['/opt/my py/bin/python', '-I', '-c', 'import sys; print("a\\b", 100%, "$HOME", \'q\')\nx\ty',
                '_hub', '--mode', 'sys temd', '한글 경로', '%h/$USER', '']
        text = render_unit(argv)
        lines = [line for line in text.splitlines() if line.startswith('ExecStart=')]
        self.assertEqual(len(lines), 1, text)
        self.assertEqual(systemd_split(lines[0].removeprefix('ExecStart=')), argv)
        self.assertNotIn('%h', lines[0].replace('%%h', ''), 'a lone % specifier must not survive')
        self.assertNotIn('$USER', lines[0].replace('$$USER', ''), 'a lone $ substitution must not survive')


class EnableRefusalTests(unittest.TestCase):
    UNAVAILABLE = 'systemd 사용자 서비스 관리자를 쓸 수 없습니다. 설정은 바뀌지 않았습니다.'

    def test_no_systemctl_on_path_exits_2_and_writes_no_unit(self):
        box = ServiceSandbox(self, systemctl=False)
        result = box.wb('hub', 'enable')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(self.UNAVAILABLE, result.stdout + result.stderr)
        self.assertFalse((box.config / 'systemd').exists())
        self.assertEqual(box.calls(), [])

    def test_failing_show_environment_exits_2_and_writes_no_unit(self):
        box = ServiceSandbox(self)
        result = box.wb('hub', 'enable', env=box.with_env(FAKE_FAIL='show-environment'))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(self.UNAVAILABLE, result.stdout + result.stderr)
        self.assertFalse((box.config / 'systemd').exists())
        self.assertEqual(box.systemctl_calls(), [('--user show-environment', 'absent')])


class EnableRollbackTests(unittest.TestCase):
    def setUp(self):
        self.box = ServiceSandbox(self)

    def test_failing_enable_removes_a_new_unit_and_reports_stderr(self):
        result = self.box.wb('hub', 'enable', env=self.box.with_env(FAKE_FAIL='enable omp'))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('fake systemctl: enable omp failed', result.stderr)
        self.assertFalse(self.box.unit.exists())
        self.assertEqual(sorted(p.name for p in self.box.unit.parent.iterdir()), [], 'no temp file is left')
        self.assertEqual(self.box.hub_pids(), [])

    def test_failing_enable_puts_the_previous_unit_back_byte_for_byte(self):
        self.box.unit.parent.mkdir(parents=True)
        previous = b'[Service]\nExecStart=/bin/true # user edited\n'
        self.box.unit.write_bytes(previous)
        result = self.box.wb('hub', 'enable', env=self.box.with_env(FAKE_FAIL='enable omp'))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.box.unit.read_bytes(), previous)

    def test_failing_daemon_reload_also_restores_and_exits_1(self):
        result = self.box.wb('hub', 'enable', env=self.box.with_env(FAKE_FAIL='daemon-reload'))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('fake systemctl: daemon-reload failed', result.stderr)
        self.assertFalse(self.box.unit.exists())
        self.assertNotIn('--user enable ' + UNIT_NAME, [args for args, _ in self.box.systemctl_calls()])


class ExistingUnitTests(unittest.TestCase):
    def setUp(self):
        self.box = ServiceSandbox(self)
        self.box.unit.parent.mkdir(parents=True)

    def test_unit_path_that_is_a_directory_fails_without_a_traceback(self):
        self.box.unit.mkdir()
        result = self.box.wb('hub', 'enable')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn('Traceback', result.stderr)
        self.assertTrue(self.box.unit.is_dir())
        self.assertEqual(self.box.systemctl_calls(), [('--user show-environment', 'present')])

    def test_unreadable_unit_fails_without_a_traceback(self):
        self.box.unit.write_text('x')
        self.box.unit.chmod(0)
        self.addCleanup(self.box.unit.chmod, 0o600)
        if os.access(self.box.unit, os.R_OK):
            self.skipTest('permission bits are not enforced for this user')
        result = self.box.wb('hub', 'enable')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(self.box.unit.stat().st_mode & 0o777, 0)

    def test_failing_enable_restores_a_symlinked_unit_as_a_symlink(self):
        target = self.box.base / 'dotfiles' / 'hub.service'
        target.parent.mkdir()
        target.write_bytes(b'[Service]\nExecStart=/bin/true\n')
        self.box.unit.symlink_to(target)
        result = self.box.wb('hub', 'enable', env=self.box.with_env(FAKE_FAIL='enable omp'))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(self.box.unit.is_symlink())
        self.assertEqual(os.readlink(self.box.unit), str(target))
        self.assertEqual(target.read_bytes(), b'[Service]\nExecStart=/bin/true\n')


class LingerTests(unittest.TestCase):
    def setUp(self):
        self.box = ServiceSandbox(self)

    def test_linger_no_prints_guidance_and_never_changes_linger(self):
        result = self.box.wb('hub', 'enable', env=self.box.with_env(FAKE_LINGER='no'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("로그인 전(부팅 직후)에 시작하려면 'loginctl enable-linger'가 필요합니다. "
                      "이 명령은 그것을 바꾸지 않습니다.", result.stdout)
        loginctl = [args for tool, args, _ in self.box.calls() if tool == 'loginctl']
        self.assertEqual(loginctl, [f'show-user {os.geteuid()} -p Linger'])

    def test_linger_yes_is_reported_without_the_guidance(self):
        result = self.box.wb('hub', 'enable', env=self.box.with_env(FAKE_LINGER='yes'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('linger: yes', result.stdout)
        self.assertNotIn('enable-linger', result.stdout)

    def test_unreadable_linger_still_enables_and_says_so(self):
        result = self.box.wb('hub', 'enable', env=self.box.with_env(FAKE_LINGER='broken'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('linger: unknown', result.stdout)
        self.assertTrue(self.box.unit.exists())

    def test_enable_says_it_starts_after_login_unless_linger_is_on(self):
        result = self.box.wb('hub', 'enable', env=self.box.with_env(FAKE_LINGER='no'))
        self.assertIn('로그인 뒤', result.stdout)


class DisableTests(unittest.TestCase):
    def setUp(self):
        self.box = ServiceSandbox(self)

    def test_disable_reverts_enable_in_order_and_keeps_a_running_hub(self):
        self.box.wb('hub', 'enable')
        self.box.log.write_text('')
        hub = self.box.start_hub()
        result = self.box.wb('hub', 'disable')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.box.systemctl_calls(),
                         [('--user disable ' + UNIT_NAME, 'present'), ('--user daemon-reload', 'absent')])
        self.assertFalse(self.box.unit.exists())
        self.assertIn('Hub는 멈추지 않았습니다', result.stdout)
        self.assertEqual(self.box.hub_pids(), [hub.pid], 'disable leaves the running Hub alone')
        self.assertIsNone(hub.poll())

    def test_disable_when_already_off_exits_0_without_touching_systemd(self):
        result = self.box.wb('hub', 'disable')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('이미 꺼져 있습니다', result.stdout)
        self.assertEqual(self.box.calls(), [])

    def test_failing_disable_keeps_the_unit_and_exits_1(self):
        self.box.wb('hub', 'enable')
        result = self.box.wb('hub', 'disable', env=self.box.with_env(FAKE_FAIL='disable omp'))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('fake systemctl: disable omp failed', result.stderr)
        self.assertTrue(self.box.unit.exists())

    def test_disable_without_systemctl_keeps_the_unit_and_exits_2(self):
        self.box.wb('hub', 'enable')
        (self.box.bin / 'systemctl').unlink()
        result = self.box.wb('hub', 'disable')
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertTrue(self.box.unit.exists())


class StatusAutostartTests(unittest.TestCase):
    def setUp(self):
        self.box = ServiceSandbox(self)
        self.hub = self.box.start_hub()

    def status_line(self, **env: str) -> str:
        result = self.box.wb('hub', 'status', env=self.box.with_env(**env))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line.strip() for line in result.stdout.splitlines() if '부팅 자동 시작' in line]
        self.assertEqual(len(lines), 1, result.stdout)
        return lines[0]

    def test_status_shows_enabled_and_disabled_from_is_enabled(self):
        self.assertEqual(self.status_line(FAKE_IS_ENABLED='enabled'), '부팅 자동 시작: enabled')
        self.assertEqual(self.status_line(FAKE_IS_ENABLED='disabled'), '부팅 자동 시작: disabled')
        self.assertIn(('--user is-enabled ' + UNIT_NAME, 'absent'), self.box.systemctl_calls())

    def test_status_is_unknown_when_systemctl_cannot_say(self):
        self.assertEqual(self.status_line(FAKE_IS_ENABLED='indirect'), '부팅 자동 시작: unknown')
        (self.box.bin / 'systemctl').unlink()
        self.assertEqual(self.status_line(), '부팅 자동 시작: unknown')

    def test_no_answer_and_no_unit_file_is_disabled_not_unknown(self):
        # systemctl prints nothing for a missing unit; with no unit file there is nothing enabled.
        self.assertEqual(self.status_line(FAKE_FAIL='is-enabled'), '부팅 자동 시작: disabled')

    def test_no_answer_with_a_unit_file_stays_unknown(self):
        self.box.unit.parent.mkdir(parents=True)
        self.box.unit.write_text('[Service]\n')
        self.assertEqual(self.status_line(FAKE_FAIL='is-enabled'), '부팅 자동 시작: unknown')

    def test_hanging_is_enabled_gives_up_quickly_as_unknown(self):
        started = time.monotonic()
        line = self.status_line(FAKE_HANG='is-enabled')
        self.assertEqual(line, '부팅 자동 시작: unknown')
        self.assertLess(time.monotonic() - started, 9, 'status must not wait the full command timeout')

    def test_status_json_carries_the_same_state(self):
        result = self.box.wb('hub', 'status', '--json', env=self.box.with_env(FAKE_IS_ENABLED='enabled'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['boot_autostart'], 'enabled')


class SystemdModeHubTests(unittest.TestCase):
    def setUp(self):
        self.box = ServiceSandbox(self)

    def test_systemd_mode_hub_records_mode_systemd(self):
        hub = self.box.popen([sys.executable, '-m', 'workbench', '_hub', '--foreground', '--mode', 'systemd'])
        self.assertTrue(wait_until(lambda: self.box.record_path.exists(), 10), hub.poll())
        self.assertEqual(self.box.record()['mode'], 'systemd')
        self.assertEqual(self.box.record()['pid'], hub.pid)

    def test_systemd_mode_hub_losing_the_lock_exits_0_and_leaves_the_winner(self):
        winner = self.box.start_hub()
        loser = self.box.popen([sys.executable, '-m', 'workbench', '_hub', '--foreground', '--mode', 'systemd'])
        self.assertEqual(loser.wait(timeout=15), 0)
        self.assertEqual(flock_holder(self.box.lock), winner.pid)
        self.assertEqual(self.box.record()['mode'], 'foreground')


if __name__ == '__main__':
    unittest.main()
