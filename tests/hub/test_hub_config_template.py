"""2026-10-26 user request: ``hub.toml`` is created automatically (a commented template) when absent.

Seams: the ``hub.toml`` parse/template API (``workbench.hub.config``), and a real ``wb _hub --foreground``
plus ``wb hub passwd``/``wb hub status`` in the shared web sandbox (T3: temp HOME/XDG_CONFIG_HOME, short
private XDG_RUNTIME_DIR, AUTOSTART=0). Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from workbench.hub import config  # noqa: E402
from fixtures.web_sandbox import PASSWORD, WebSandbox, tcp_listening  # noqa: E402


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


WEB_KEYS = {'bind': '"127.0.0.1"', 'port': '8780', 'tls_cert': '""', 'tls_key': '""',
            'allow_insecure_http': 'true', 'server_names': '[]', 'allow_from': '[]'}  # allow_from: 2026-10-27
BOARD_KEYS = {'model': '""', 'role_prompt_file': '""', 'shell': '""'}
COMMENTED_KEY = re.compile(r'^# ?([a-z_]+) = (.+)$')


class TemplateContentTests(unittest.TestCase):
    """The template text itself (one constant, parsed by the strict reader)."""

    def setUp(self):
        self.text = config.CONFIG_TEMPLATE

    def lines(self) -> list[str]:
        return self.text.splitlines()

    def test_it_parses_to_exactly_the_defaults(self):
        self.assertEqual(config.parse_config(self.text.encode('utf-8')), config.HubConfig())
        self.assertLessEqual(len(self.text.encode('utf-8')), config.CONFIG_FILE_LIMIT)

    def test_web_enabled_false_is_an_active_line_in_the_web_table(self):
        active = [line for line in self.lines() if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual(active, ['[web]', 'enabled = false', '[board_agent]'])

    def test_every_other_key_is_commented_with_its_default(self):
        commented = {}
        table = None
        for line in self.lines():
            if line.startswith('['):
                table = line
            match = COMMENTED_KEY.match(line)
            if match and match.group(1) in {**WEB_KEYS, **BOARD_KEYS}:
                commented[(table, match.group(1))] = match.group(2).split('  #')[0].strip()
        self.assertEqual(commented, {**{('[web]', key): value for key, value in WEB_KEYS.items()},
                                     **{('[board_agent]', key): value for key, value in BOARD_KEYS.items()}})

    def test_the_list_bind_and_allow_from_examples_parse_and_name_loopback_and_a_vpn(self):
        examples = [re.sub(r'^#\s+예[^:]*:\s*', '', line) for line in self.lines() if re.match(r'^#\s+예', line)]
        self.assertEqual(len(examples), 2, examples)
        for example in examples:
            parsed = config.parse_config(f'[web]\n{example}\n'.encode('utf-8'))
            self.assertNotEqual(parsed, config.HubConfig(), example)
        self.assertIn('bind = [', examples[0])
        self.assertIn('100.64.0.0/10', examples[1])
        comments = '\n'.join(self.lines())
        self.assertIn('Tailscale', comments)
        self.assertIn('"127.0.0.1"과 "::1"', comments)  # loopback is not allowed implicitly

    def test_uncommenting_every_key_still_gives_the_defaults(self):
        uncommented = '\n'.join(re.sub(r'^# ?(?=[a-z_]+ = )', '', line) for line in self.lines()) + '\n'
        self.assertNotEqual(uncommented, self.text)
        self.assertEqual(config.parse_config(uncommented.encode('utf-8')), config.HubConfig())

    def test_korean_comments_say_how_to_enable_and_warn_about_lan_and_plain_http(self):
        comments = '\n'.join(line for line in self.lines() if line.lstrip().startswith('#'))
        self.assertRegex(comments, r'[가-힣]')
        # 2026-10-27: changes are applied with 'wb hub reload' (was: 'wb hub stop' then 'wb hub start')
        for part in ('enabled = true', 'wb hub passwd', "'wb hub reload'", 'LAN', 'HTTP'):
            self.assertIn(part, comments)
        for key in (*WEB_KEYS, *BOARD_KEYS, 'enabled'):
            described = [line for line in self.lines() if line.startswith('#') and key in line
                         and re.search(r'[가-힣]', line) and '기본' in line]
            self.assertTrue(described, f'no Korean line with its default describes {key}')


class HubStartCreatesTemplateTests(unittest.TestCase):
    def setUp(self):
        self.box = WebSandbox(self)

    def test_hub_start_without_hub_toml_creates_it_0600_in_a_0700_dir(self):
        self.assertFalse(self.box.config_dir.exists())
        self.box.start_hub()
        self.assertTrue(self.box.config_file.is_file())
        self.assertFalse(self.box.config_file.is_symlink())
        self.assertEqual(mode_of(self.box.config_file), 0o600)
        self.assertEqual(mode_of(self.box.config_dir), 0o700)
        self.assertEqual(os.stat(self.box.config_file).st_uid, os.geteuid())

    def test_the_created_file_is_the_template_and_the_web_stays_disabled(self):
        self.box.start_hub()
        self.assertEqual(self.box.config_file.read_text(encoding='utf-8'), config.CONFIG_TEMPLATE)
        self.assertEqual(self.box.status()['web'], {'state': 'disabled', 'config': str(self.box.config_file)})
        self.assertEqual([name for name in os.listdir(self.box.config_dir)], ['hub.toml'])  # no temp file left


def snapshot(path: Path) -> tuple:
    info = os.lstat(path)
    return (path.read_bytes() if not stat.S_ISLNK(info.st_mode) else os.readlink(path),
            info.st_ino, stat.S_IMODE(info.st_mode), info.st_mtime_ns, info.st_size)


class ExistingFileIsNeverTouchedTests(unittest.TestCase):
    def setUp(self):
        self.box = WebSandbox(self)

    def test_an_existing_file_is_byte_for_byte_unchanged_after_hub_start(self):
        for label, raw, mode, web in (
                ('invalid', 'this is = = not toml\n', 0o600, {'state': 'config_invalid', 'detail': 'toml'}),
                ('unknown key', '[web]\nenabeld = true\n', 0o644, {'state': 'config_invalid', 'detail': 'web.enabeld'}),
                ('empty', '', 0o600, None),
                ('user edited', '# mine\n[web]\nenabled = false\nport = 0\n', 0o640, None)):
            with self.subTest(label):
                box = self.box = WebSandbox(self)
                box.write_config(raw=raw)
                os.chmod(box.config_file, mode)
                before = snapshot(box.config_file)
                hub = box.start_hub()
                status = box.status()['web']
                self.assertEqual(status, web or {'state': 'disabled', 'config': str(box.config_file)})
                self.assertEqual(snapshot(box.config_file), before)
                self.assertEqual(sorted(os.listdir(box.config_dir)), ['hub.toml'])
                self.assertNotIn('hub.toml template', box.hub_log(hub))
                box.stop_hub(hub)


class SymlinkAndUnsafeDirTests(unittest.TestCase):
    """Never written through a symlink; an unsafe or unwritable dir: one hub.log line, the Hub works."""

    def setUp(self):
        self.box = WebSandbox(self)
        self.box.config_dir.mkdir(mode=0o700)
        self.outside = self.box.base / 'outside'
        self.outside.mkdir(mode=0o700)

    def test_a_dangling_symlink_is_not_followed_and_the_web_stays_disabled(self):
        target = self.outside / 'target.toml'
        self.box.config_file.symlink_to(target)
        before = snapshot(self.box.config_file)
        hub = self.box.start_hub()
        # the strict reader follows a symlink to read; a dangling one reads as "absent": the defaults
        self.assertEqual(self.box.status()['web'], {'state': 'disabled', 'config': str(self.box.config_file)})
        self.assertFalse(os.path.lexists(target))
        self.assertEqual(snapshot(self.box.config_file), before)
        self.assertEqual(os.listdir(self.outside), [])
        self.assertEqual(sorted(os.listdir(self.box.config_dir)), ['hub.toml'])
        self.assertNotIn('hub.toml template', self.box.hub_log(hub))

    def test_a_symlink_to_a_file_leaves_both_unchanged(self):
        target = self.outside / 'target.toml'
        target.write_text('[web]\nenabled = false\n')
        self.box.config_file.symlink_to(target)
        before, before_target = snapshot(self.box.config_file), snapshot(target)
        self.box.start_hub()
        self.assertEqual(self.box.status()['web']['state'], 'disabled')
        self.assertEqual((snapshot(self.box.config_file), snapshot(target)), (before, before_target))

    def assert_skipped_with_one_log_line(self, hub, reason: str) -> None:
        status = self.box.status()
        self.assertEqual(status['web']['state'], 'disabled')
        self.assertEqual(status['registry']['state'], 'ok')
        self.assertIsNone(hub.poll())
        lines = [line for line in self.box.hub_log(hub).splitlines() if 'hub.toml template' in line]
        self.assertEqual(len(lines), 1, self.box.hub_log(hub))
        self.assertIn(reason, lines[0])

    def test_a_symlinked_config_dir_is_not_written_through(self):
        self.box.config_dir.rmdir()
        self.box.config_dir.symlink_to(self.outside, target_is_directory=True)
        hub = self.box.start_hub()
        self.assertEqual(os.listdir(self.outside), [])
        self.assert_skipped_with_one_log_line(hub, '(dir)')

    def test_a_group_writable_config_dir_is_skipped(self):
        os.chmod(self.box.config_dir, 0o770)
        hub = self.box.start_hub()
        self.assertEqual(os.listdir(self.box.config_dir), [])
        self.assertEqual(mode_of(self.box.config_dir), 0o770)  # an existing dir's mode is left alone
        self.assert_skipped_with_one_log_line(hub, '(dir)')

    def test_an_unwritable_config_dir_is_skipped(self):
        os.chmod(self.box.config_dir, 0o500)
        self.addCleanup(os.chmod, self.box.config_dir, 0o700)
        hub = self.box.start_hub()
        self.assertEqual(os.listdir(self.box.config_dir), [])
        self.assert_skipped_with_one_log_line(hub, '(EACCES)')


class PasswdCreatesTemplateTests(unittest.TestCase):
    def setUp(self):
        self.box = WebSandbox(self)

    def test_passwd_without_hub_toml_creates_the_template_the_hint_points_at(self):
        self.assertFalse(self.box.config_dir.exists())
        result = self.box.set_password()  # no Hub running
        self.assertEqual(self.box.config_file.read_text(encoding='utf-8'), config.CONFIG_TEMPLATE)
        self.assertEqual(mode_of(self.box.config_file), 0o600)
        self.assertEqual(mode_of(self.box.config_dir), 0o700)
        hints = [line for line in result.stdout.splitlines() if '[web] enabled = true' in line]
        self.assertEqual(len(hints), 1, result.stdout)
        self.assertIn(str(self.box.config_file), hints[0])
        self.assertEqual(result.stderr, '')

    def test_passwd_leaves_an_existing_file_unchanged(self):
        self.box.write_config(raw='[web]\nenabled = false\n# keep me\n')
        before = snapshot(self.box.config_file)
        self.box.set_password()
        self.assertEqual(snapshot(self.box.config_file), before)


def _racer(environment: dict, barrier, results) -> None:
    barrier.wait(10)
    try:
        results.put(config.write_template(environment))
    except BaseException as exc:  # write_template never raises; report it if it does
        results.put(f'raised:{exc!r}')


class ConcurrentCreateTests(unittest.TestCase):
    RACERS, ROUNDS = 8, 15

    def test_concurrent_creators_make_one_valid_file_and_no_error(self):
        context = multiprocessing.get_context('fork')
        for round_number in range(self.ROUNDS):
            base = Path(tempfile.mkdtemp(prefix='wb-tpl-'))
            self.addCleanup(shutil.rmtree, base, True)
            environment = {'XDG_CONFIG_HOME': str(base / 'config'), 'HOME': str(base / 'home')}
            barrier, results = context.Barrier(self.RACERS), context.Queue()
            racers = [context.Process(target=_racer, args=(environment, barrier, results))
                      for _ in range(self.RACERS)]
            for racer in racers:
                racer.start()
            outcomes = sorted(results.get(timeout=20) for _ in racers)
            for racer in racers:
                racer.join(10)
                self.assertEqual(racer.exitcode, 0)
            with self.subTest(round=round_number):
                self.assertEqual(outcomes, ['created'] + ['exists'] * (self.RACERS - 1))
                path = config.config_path(environment)
                self.assertEqual(path.read_text(encoding='utf-8'), config.CONFIG_TEMPLATE)
                self.assertEqual(mode_of(path), 0o600)
                self.assertEqual(os.listdir(path.parent), ['hub.toml'])

    def test_concurrent_passwd_runs_all_succeed_with_one_template(self):
        box = WebSandbox(self)
        runs = [subprocess.Popen([sys.executable, '-m', 'workbench', 'hub', 'passwd', '--stdin'], env=box.env,
                                 cwd=box.base, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) for _ in range(4)]
        finished = [run.communicate(PASSWORD + '\n', timeout=60) for run in runs]
        for run, (stdout, stderr) in zip(runs, finished):
            self.assertEqual(run.returncode, 0, (stdout, stderr))
        self.assertEqual(box.config_file.read_text(encoding='utf-8'), config.CONFIG_TEMPLATE)
        self.assertEqual(os.listdir(box.config_dir), ['hub.toml'])


class EnablingTheGeneratedFileTests(unittest.TestCase):
    def setUp(self):
        self.box = WebSandbox(self)

    def test_editing_enabled_to_true_and_restarting_opens_the_listener(self):
        hub = self.box.start_hub()
        text = self.box.cli('hub', 'status')
        line = next(line for line in text.stdout.splitlines() if 'web:' in line)
        for part in ('web: disabled', str(self.box.config_file), '[web] enabled = true', "'wb hub reload'"):  # 2026-10-27
            self.assertIn(part, line)
        self.assertEqual(self.box.stop_hub(hub), 0)
        template = self.box.config_file.read_text(encoding='utf-8')
        self.assertEqual(template.count('\nenabled = false\n'), 1)
        self.assertEqual(template.count('\n# port = 8780\n'), 1)
        # the user's one edit, plus port 0 so the test never takes the real default port 8780
        self.box.config_file.write_text(template.replace('\nenabled = false\n', '\nenabled = true\n')
                                        .replace('\n# port = 8780\n', '\nport = 0\n'), encoding='utf-8')
        edited = self.box.config_file.read_bytes()
        self.box.set_password()
        hub = self.box.start_hub()
        host, port = self.box.wait_listening(hub.pid)
        self.assertEqual(host, '127.0.0.1')
        web = self.box.status()['web']
        self.assertEqual((web['state'], web['url']), ('listening', f'http://127.0.0.1:{port}'))
        self.assertEqual(self.box.config_file.read_bytes(), edited)
        self.assertEqual(self.box.stop_hub(hub), 0)
        self.assertEqual(tcp_listening(hub.pid), [])


if __name__ == '__main__':
    unittest.main()
