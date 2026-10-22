"""Public setup results and real command entries; reuse the executable fixture only."""
import json
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_setup as fixtures


class CommandLinkTests(unittest.TestCase):
    setUp = fixtures.SetupTests.setUp
    executable = fixtures.SetupTests.executable
    cli = fixtures.SetupTests.cli

    def public_cli(self, *arguments):
        return subprocess.run([sys.executable, '-m', 'workbench.backend.cli', *arguments],
                              env=dict(self.env, PYTHONPATH=str(fixtures.ROOT / 'src'),
                                       PYTHONDONTWRITEBYTECODE='1'),
                              cwd=self.root / 'project', capture_output=True, text=True, timeout=10)

    def managed(self):
        """An earlier WB-managed installation with its original selection record."""
        binary = self.root / 'home/.local/share/omp-workbench/tools/88.2.3-saved/omp'
        for directory in reversed(binary.parent.parents):
            if directory.is_relative_to(self.root / 'home'):
                directory.mkdir(mode=0o700, exist_ok=True)
        binary.parent.mkdir(mode=0o700)
        self.executable('artifact', '88.2.3').rename(binary)
        code, output, error = self.cli('--omp', str(binary))
        self.assertEqual(code, 0, output + error)
        record = json.loads(self.record.read_text())
        record['method'] = 'official_binary'
        self.record.write_text(json.dumps(record))
        command = self.root / 'home/.local/bin/omp'
        command.unlink()
        return binary, command

    def test_saved_managed_installation_repairs_missing_command_and_reuses_matching_link(self):
        binary, command = self.managed()
        self.env['PATH'] = str(command.parent) + os.pathsep + self.env['PATH']
        for expected in ('created', 'reused'):
            code, output, error = self.cli()
            self.assertEqual(code, 0, output + error)
            result = json.loads(output)
            self.assertEqual((result['state'], result['status'], result['path']),
                             ('ready', 'reused', str(binary)))
            self.assertEqual(result['command_link']['status'], expected)
            self.assertTrue(result['command_link']['on_path'])
            self.assertEqual(os.readlink(command), str(binary))
        command.unlink()
        self.assertEqual(json.loads(self.cli()[1])['command_link']['status'], 'created')

    def test_existing_command_entries_are_preserved_with_nonfatal_warning(self):
        binary, command = self.managed()
        command.parent.mkdir(mode=0o700, exist_ok=True)
        other = self.executable('other')
        def immutable(info):
            # atime moves on any read of the entry; the conflict must leave the entry itself untouched
            return tuple(getattr(info, name) for name in
                         ('st_mode', 'st_ino', 'st_dev', 'st_size', 'st_mtime_ns'))
        for kind in ('file', 'symlink', 'dangling', 'directory'):
            with self.subTest(kind=kind):
                if kind == 'file':
                    command.write_bytes(b'user executable'); command.chmod(0o700)
                elif kind in ('symlink', 'dangling'):
                    command.symlink_to(other if kind == 'symlink' else self.root / 'absent')
                else:
                    command.mkdir(mode=0o700)
                try:
                    identity = immutable(command.lstat())
                    code, output, error = self.cli('--omp', str(binary))
                    self.assertEqual(code, 0, output + error)
                    result = json.loads(output)
                    self.assertEqual((result['state'], result['path']), ('ready', str(binary)))
                    self.assertEqual(result['command_link']['status'], 'warning')
                    self.assertEqual(result['command_link']['reason'], 'command_link_conflict')
                    self.assertIn(str(command), result['command_link']['warning'])
                    self.assertEqual(immutable(command.lstat()), identity)
                    if kind == 'file': self.assertEqual(command.read_bytes(), b'user executable')
                finally:
                    # a failing kind must not leave its entry behind to cascade into the next kind
                    if kind == 'directory': command.rmdir()
                    else: command.unlink(missing_ok=True)

    def test_path_missing_and_shadowed_command_give_export_and_absolute_login_guidance(self):
        binary, command = self.managed()
        for expected in (None, str(self.root / 'bin/omp')):
            with self.subTest(resolved_path=expected):
                if expected: self.executable()
                code, output, error = self.cli()
                self.assertEqual(code, 0, output + error)
                result = json.loads(output)
                self.assertFalse(result['command_link']['on_path'])
                self.assertEqual(result['command_link']['resolved_path'], expected)
                self.assertIn('export PATH=', result['command_link']['guidance'])
                self.assertIn(str(command.parent), result['command_link']['guidance'])
                self.assertIn('"$PATH"', result['command_link']['guidance'])
                self.assertIn(str(binary), result['login_guidance'])
                self.assertIn('/login', result['login_guidance'])
                self.assertIn('wb', result['login_guidance'])
                self.assertNotIn('Run omp ', result['login_guidance'])

    def test_unsafe_command_parent_and_permission_failure_leave_omp_ready_without_redirected_writes(self):
        binary, command = self.managed()
        outside = self.root / 'outside'
        outside.mkdir(mode=0o700)
        command.parent.rmdir()
        command.parent.symlink_to(outside, target_is_directory=True)
        code, output, error = self.cli()
        self.assertEqual(code, 0, output + error)
        self.assertEqual(json.loads(output)['command_link']['status'], 'warning')
        self.assertEqual(list(outside.iterdir()), [])
        command.parent.unlink()
        command.parent.mkdir(mode=0o700)
        command.parent.chmod(0o777)
        code, output, error = self.cli()
        self.assertEqual(code, 0, output + error)
        self.assertEqual(json.loads(output)['command_link']['reason'], 'unsafe_managed_ancestry')
        self.assertEqual(command.parent.stat().st_mode & 0o777, 0o777)
        command.parent.chmod(0o700)
        with patch('os.symlink', side_effect=PermissionError('fixture bin permission denied')):
            code, output, error = self.cli()
        self.assertEqual(code, 0, output + error)
        result = json.loads(output)
        self.assertEqual((result['state'], result['path']), ('ready', str(binary)))
        self.assertEqual(result['command_link']['reason'], 'PermissionError')
        self.assertIn(str(binary), result['login_guidance'])
        self.assertFalse(command.exists())
        self.assertEqual(json.loads(self.record.read_text())['state'], 'ready')

    def test_failed_managed_selection_commit_never_publishes_command(self):
        artifact = self.executable('download-fixture', '88.2.3').read_bytes()
        metadata = {'build': {'version': '88.2.3', 'channel': 'stable'},
                    'file': {'platform': 'linux', 'arch': 'x86_64', 'name': 'omp-linux-x64',
                             'sha256': sha256(artifact).hexdigest(), 'size': len(artifact)},
                    'download': 'https://build.stencil.so/fixture/omp'}
        def response(url, **kwargs):
            stream = BytesIO(json.dumps(metadata).encode() if '/latest/' in url else artifact)
            stream.geturl = lambda: url
            return stream
        actual_replace = os.replace
        def fail_selection(source, destination, **kwargs):
            if destination == 'setup.json': raise OSError('fixture selection commit failure')
            return actual_replace(source, destination, **kwargs)
        with patch('workbench.backend.install_recipe._open_https', side_effect=response), \
                patch('os.replace', side_effect=fail_selection):
            code, output, error = self.cli()
        self.assertEqual(code, 1, output + error)
        self.assertFalse(self.record.exists())
        self.assertFalse((self.root / 'home/.local/bin').exists())
        self.assertEqual(list((self.root / 'home/.local/share/omp-workbench/tools').iterdir()), [])

    def test_external_selection_creates_missing_link_without_modifying_executable_or_conflict(self):
        binary = self.executable('external')
        original = binary.read_bytes()
        command = self.root / 'home/.local/bin/omp'
        self.assertEqual(self.cli('--omp', str(binary))[0], 0)
        self.assertEqual(os.readlink(command), str(binary))
        self.assertEqual(binary.read_bytes(), original)
        command.unlink()
        command.symlink_to(self.root / 'unrelated-missing-program')
        before = command.lstat()
        code, output, error = self.cli('--omp', str(binary))
        self.assertEqual(code, 0, output + error)
        self.assertEqual(json.loads(output)['command_link']['status'], 'warning')
        after = command.lstat()
        for field in ('st_dev', 'st_ino', 'st_mode', 'st_mtime_ns', 'st_ctime_ns'):
            self.assertEqual(getattr(after, field), getattr(before, field))
        self.assertEqual(os.readlink(command), str(self.root / 'unrelated-missing-program'))

    def test_human_setup_output_reports_link_path_guidance_and_nonfatal_warning(self):
        binary, command = self.managed()
        result = self.public_cli('setup', '--omp', str(binary))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for expected in (str(command), 'created', 'export PATH=', str(binary), '/login', 'wb'):
            self.assertIn(expected, result.stdout)
        command.unlink()
        command.write_bytes(b'preserve')
        result = self.public_cli('setup', '--omp', str(binary))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('warning', result.stdout.lower())
        self.assertIn('command_link_conflict', result.stdout)
        self.assertEqual(command.read_bytes(), b'preserve')

    def test_start_without_auth_prints_setup_and_selected_absolute_login_command(self):
        binary, command = self.managed()
        self.assertEqual(self.cli()[0], 0)
        self.env['PATH'] += os.pathsep + os.defpath
        result = self.public_cli('start', '--no-attach')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        for expected in ('wb setup', str(command), "'omp'", '/login', 'wb', 'missing'):
            self.assertIn(expected, result.stderr)
        self.assertFalse((self.root / 'home/.omp').exists())
        self.assertFalse((self.root / 'project/.wb').exists())

    def test_runtime_defaults_to_literal_user_command_despite_stale_record_and_path_shadow(self):
        from workbench.backend.launcher import build_plan
        old, command = self.managed()
        current = self.executable('current', '42.0.0')
        command.parent.mkdir(mode=0o700, exist_ok=True)
        command.symlink_to(current)
        old.unlink()
        self.executable(version='11.0.0')
        self.env['PATH'] += os.pathsep + os.defpath
        plan = build_plan(self.env)
        self.assertEqual((plan.omp, plan.omp_version), (str(command), 'omp/42.0.0'))
        explicit = self.executable('explicit', '43.0.0')
        self.env['WORKBENCH_OMP'] = str(current)
        self.assertEqual(build_plan(self.env).omp, str(current))
        self.assertEqual(build_plan(self.env, omp=str(explicit)).omp, str(explicit))

    def test_missing_or_broken_default_command_stops_with_setup_guidance_without_saved_fallback(self):
        binary, command = self.managed()
        self.executable()
        self.env['PATH'] += os.pathsep + os.defpath
        for state in ('missing', 'broken'):
            with self.subTest(state=state):
                if state == 'broken':
                    command.parent.mkdir(mode=0o700, exist_ok=True)
                    command.symlink_to(self.executable('bad-default', broken=True))
                result = self.public_cli('start', '--no-attach')
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('wb setup', result.stderr)
                self.assertIn(str(command), result.stderr)
                self.assertNotIn(str(binary), result.stderr)
                self.assertFalse((self.root / 'project/.wb').exists())

    def test_setup_rechecks_current_user_command_before_stale_saved_path_and_path_shadow(self):
        old, command = self.managed()
        current = self.executable('current', '42.0.0')
        command.parent.mkdir(mode=0o700, exist_ok=True)
        command.symlink_to(current)
        old.unlink()
        self.executable(version='11.0.0')
        code, output, error = self.cli()
        self.assertEqual(code, 0, output + error)
        result = json.loads(output)
        self.assertEqual((result['path'], result['version']), (str(current), '42.0.0'))
        self.assertEqual(json.loads(self.record.read_text())['source'], 'user_bin')
        self.assertEqual(os.readlink(command), str(current))

    def test_saved_managed_command_repairs_after_data_home_changes(self):
        binary, command = self.managed()
        self.env['XDG_DATA_HOME'] = str(self.root / 'new-data-home')
        code, output, error = self.cli()
        self.assertEqual(code, 0, output + error)
        result = json.loads(output)
        self.assertEqual(result['command_link']['status'], 'created')
        self.assertEqual(os.readlink(command), str(binary))
        self.assertEqual(json.loads(self.record.read_text())['method'], 'official_binary')

    def test_existing_user_command_executable_is_reused_without_self_link_or_warning(self):
        command = self.root / 'home/.local/bin/omp'
        for directory in (command.parent.parent, command.parent): directory.mkdir(mode=0o700)
        self.executable('user-installed', '42.0.0').rename(command)
        before = command.lstat()
        code, output, error = self.cli()
        self.assertEqual(code, 0, output + error)
        result = json.loads(output)
        self.assertEqual((result['path'], result['command_link']['status']), (str(command), 'reused'))
        self.assertIsNone(result['command_link']['warning'])
        self.assertEqual(command.lstat(), before)
        self.assertFalse(command.is_symlink())

    def test_bare_omp_receives_only_user_argv_and_env_while_wb_child_gets_harness(self):
        from workbench.backend.launcher import build_plan, omp_command, omp_environment
        binary = self.executable('plain')
        observation = """if '--observe' in sys.argv or '--config' in sys.argv:
    print(json.dumps({'argv': sys.argv[1:], 'env': {key: os.environ.get(key) for key in (
        'HOME', 'PI_CONFIG_DIR', 'PI_CODING_AGENT_DIR', 'WORKBENCH_G3_ROLE',
        'WORKBENCH_G3_TOKEN', 'WORKBENCH_INSTALL_PYTHON')}}))
    raise SystemExit(0)
"""
        binary.write_text(binary.read_text().replace("if '--version' in sys.argv:",
                                                     observation + "if '--version' in sys.argv:"))
        self.assertEqual(self.cli('--omp', str(binary))[0], 0)
        command = self.root / 'home/.local/bin/omp'
        self.env['PATH'] = str(command.parent) + os.pathsep + self.env['PATH'] + os.pathsep + os.defpath
        self.env['PI_CONFIG_DIR'] = str(self.root / 'user-config')
        self.env['PI_CODING_AGENT_DIR'] = str(self.root / 'user-agent')
        unchanged = dict(self.env)
        bare = subprocess.run(['omp', '--observe', 'user-value'], env=self.env,
                              capture_output=True, text=True, timeout=5)
        self.assertEqual(bare.returncode, 0, bare.stderr)
        observed = json.loads(bare.stdout)
        self.assertEqual(observed['argv'], ['--observe', 'user-value'])
        self.assertEqual(observed['env'], {'HOME': self.env['HOME'],
            'PI_CONFIG_DIR': str(self.root / 'user-config'),
            'PI_CODING_AGENT_DIR': str(self.root / 'user-agent'), 'WORKBENCH_G3_ROLE': None,
            'WORKBENCH_G3_TOKEN': None, 'WORKBENCH_INSTALL_PYTHON': None})
        plan = build_plan(self.env)
        private_home = {'PI_CONFIG_DIR': str(self.root / 'private/config'),
                        'PI_CODING_AGENT_DIR': str(self.root / 'private/agent')}
        child_env = omp_environment(self.env, plan, role='worker', token='fixture-only',
                                    bridge_socket=self.root / 'bridge.sock', home=private_home)
        child = subprocess.run(omp_command(plan, self.root / 'omp-isolation-worker.yml'),
                               env=child_env, capture_output=True, text=True, timeout=5)
        self.assertEqual(child.returncode, 0, child.stderr)
        observed = json.loads(child.stdout)
        self.assertIn('--config', observed['argv'])
        self.assertIn('--extension', observed['argv'])
        self.assertIn(plan.bridge_extension, observed['argv'])
        self.assertEqual(observed['env']['PI_CODING_AGENT_DIR'], private_home['PI_CODING_AGENT_DIR'])
        self.assertEqual(observed['env']['WORKBENCH_G3_ROLE'], 'worker')
        self.assertEqual(observed['env']['WORKBENCH_G3_TOKEN'], 'fixture-only')
        self.assertEqual(self.env, unchanged)
        self.assertEqual(os.readlink(command), str(binary))
        self.assertFalse((self.root / 'home/.omp').exists())

    def test_runtime_rejects_unsafe_default_command_parent_before_executing_version(self):
        safe = self.executable('safe')
        self.assertEqual(self.cli('--omp', str(safe))[0], 0)
        command = self.root / 'home/.local/bin/omp'
        marker = self.root / 'attacker-executed'
        attacker = self.executable('attacker')
        attacker.write_text(attacker.read_text().replace('import json, os, sys, time',
            'import json, os, sys, time\nfrom pathlib import Path\n' + f'Path({str(marker)!r}).touch()'))
        self.env['PATH'] += os.pathsep + os.defpath
        for mode in (0o770, 0o777, 0o1777):
            with self.subTest(parent_mode=oct(mode)):
                command.unlink()
                command.symlink_to(attacker)
                command.parent.chmod(mode)
                try:
                    result = self.public_cli('start', '--no-attach')
                    self.assertFalse(marker.exists(), 'default --version executed the swapped command')
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn('wb setup', result.stderr)
                    self.assertIn(str(command), result.stderr)
                    self.assertFalse((self.root / 'project/.wb').exists())
                    self.assertEqual(command.parent.stat().st_mode & 0o7777, mode)
                finally:
                    marker.unlink(missing_ok=True)
                    command.parent.chmod(0o700)
