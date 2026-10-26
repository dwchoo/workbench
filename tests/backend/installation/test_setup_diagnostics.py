"""Actionable diagnostics for an unusable ~/.local/bin/omp command link.

The security check is unchanged; these tests pin only what the user is told: the offending
directory, why it is unsafe, and how to fix it, on every surface (library, `wb`, `wb setup`,
`wb doctor`). Fake OMP fixtures come from test_setup; HOME is always a temp directory.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_setup as fixtures
from workbench.backend.setup import SetupError, resolve_runtime_selection


class DiagnosticsBase(unittest.TestCase):
    setUp_fixture = fixtures.SetupTests.setUp
    executable = fixtures.SetupTests.executable

    def setUp(self):
        self.setUp_fixture()
        self.runtime = Path(tempfile.mkdtemp(prefix='r', dir='/tmp'))
        self.addCleanup(shutil.rmtree, self.runtime, True)
        self.home = self.root / 'home'
        self.local = self.home / '.local'
        self.bin = self.local / 'bin'
        self.command = self.bin / 'omp'

    def make_bin(self, *, home=0o700, local=0o700, bin=0o700):
        self.home.chmod(home)
        self.local.mkdir()
        self.bin.mkdir()
        self.local.chmod(local)
        self.bin.chmod(bin)

    def public(self, *arguments, env=None, cwd=None):
        environment = dict(self.env, PYTHONPATH=str(fixtures.ROOT / 'src'), PYTHONDONTWRITEBYTECODE='1',
                           WORKBENCH_HUB_AUTOSTART='0', XDG_RUNTIME_DIR=str(self.runtime))
        environment.update(env or {})
        return subprocess.run([sys.executable, '-m', 'workbench', *arguments], env=environment,
                              cwd=cwd or self.root / 'project', capture_output=True, text=True, timeout=30)

    def selection_error(self):
        with self.assertRaises(SetupError) as raised:
            resolve_runtime_selection(environment=self.env)
        return raised.exception


class UnsafeAncestryDetail(DiagnosticsBase):
    def test_group_writable_command_dir_is_named_with_mode_reason_and_chmod_hint(self):
        self.make_bin(bin=0o775)
        error = self.selection_error()
        self.assertEqual(error.code, 'unsafe_managed_ancestry')
        self.assertEqual(str(error), 'unsafe_managed_ancestry')
        self.assertIn(str(self.bin), error.detail)
        self.assertIn('775', error.detail)
        self.assertIn('group-writable', error.detail)
        self.assertEqual(error.fix, f'chmod g-w {self.bin}')

    def test_world_writable_command_dir_gets_the_other_write_hint(self):
        self.make_bin(bin=0o757)
        error = self.selection_error()
        self.assertIn('world-writable', error.detail)
        self.assertNotIn('group-writable', error.detail)
        self.assertEqual(error.fix, f'chmod o-w {self.bin}')

    def test_group_and_world_writable_gets_one_combined_hint(self):
        self.make_bin(bin=0o777)
        error = self.selection_error()
        self.assertIn('group- and world-writable', error.detail)
        self.assertEqual(error.fix, f'chmod go-w {self.bin}')

    def test_group_writable_ancestor_is_named_not_the_command_path(self):
        self.make_bin(local=0o775)
        error = self.selection_error()
        self.assertEqual(error.code, 'unsafe_managed_ancestry')
        self.assertIn(str(self.local), error.detail)
        self.assertNotIn(str(self.bin), error.detail)
        self.assertIn('775', error.detail)
        self.assertEqual(error.fix, f'chmod g-w {self.local}')

    def test_group_writable_home_is_named(self):
        self.make_bin(home=0o775)
        error = self.selection_error()
        self.assertIn(str(self.home), error.detail)
        self.assertEqual(error.fix, f'chmod g-w {self.home}')

    def test_foreign_owner_says_owner_must_be_you_or_root_and_has_no_chmod_hint(self):
        self.make_bin()
        with patch('workbench.backend.setup.os.geteuid', return_value=os.geteuid() + 4242):
            error = self.selection_error()
        self.assertEqual(error.code, 'unsafe_managed_ancestry')
        self.assertIn('owned by uid', error.detail)
        self.assertIn('you or root', error.detail)
        self.assertIsNone(error.fix)

    def test_symlinked_command_parent_names_the_component_and_cause_keeping_the_code(self):
        self.local.mkdir()
        self.local.chmod(0o700)
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir(mode=0o700)
        self.bin.symlink_to(elsewhere, target_is_directory=True)
        error = self.selection_error()
        self.assertEqual(error.code, 'unusable_user_command_parent')
        self.assertIn(str(self.bin), error.detail)
        self.assertIn('symbolic link', error.detail)

    def test_missing_command_parent_names_the_missing_component(self):
        self.local.mkdir()
        self.local.chmod(0o700)
        error = self.selection_error()
        self.assertEqual(error.code, 'unusable_user_command_parent')
        self.assertIn(str(self.bin), error.detail)
        self.assertIn('does not exist', error.detail)


class PlainWbStartFailure(DiagnosticsBase):
    def public(self, *arguments):
        # start needs a host shell, which the fixture PATH (one fake-OMP directory) does not offer
        return super().public(*arguments, env={'PATH': self.env['PATH'] + ':/usr/bin:/bin'})

    def project_with_marker(self):
        from workbench.backend.project import initialize_project, resolve_project_context
        env = dict(self.env, XDG_RUNTIME_DIR=str(self.runtime))
        initialize_project(resolve_project_context(self.root / 'project', env), confirm=lambda _: True,
                           interactive=True, environment=env)

    def test_plain_wb_names_directory_reason_fix_and_setup_in_the_failure_line(self):
        self.make_bin(bin=0o775)
        self.project_with_marker()
        result = self.public()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        text = result.stderr
        self.assertIn('OMP selection failed', text)
        self.assertIn('unsafe_managed_ancestry', text)
        self.assertIn(str(self.bin), text)
        self.assertIn('group-writable', text)
        self.assertIn(f'chmod g-w {self.bin}', text)
        self.assertIn('then run wb setup', text)
        self.assertIn('No backend was started', text)

    def test_wb_start_in_an_uninitialized_folder_reports_the_same_detail(self):
        self.make_bin(local=0o775)
        result = self.public('start')
        self.assertIn(str(self.local), result.stderr)
        self.assertIn(f'chmod g-w {self.local}', result.stderr)


class SetupWhenLinkCannotBePrepared(DiagnosticsBase):
    def prepare(self):
        self.make_bin(bin=0o775)
        return self.executable('chosen')

    def test_setup_text_states_plain_wb_will_not_start_with_detail_fix_and_temporary_alternative(self):
        binary = self.prepare()
        result = self.public('setup', '--omp', str(binary))
        out = result.stdout
        self.assertIn(str(self.bin), out)
        self.assertIn('group-writable', out)
        self.assertIn(f'chmod g-w {self.bin}', out)
        self.assertIn('plain `wb` will not start', out)
        self.assertIn('wb setup', out)
        self.assertIn(f'WORKBENCH_OMP={binary}', out)
        self.assertIn('unsafe_managed_ancestry', out)

    def test_setup_json_carries_machine_readable_link_failure_fields(self):
        binary = self.prepare()
        result = self.public('setup', '--omp', str(binary), '--json')
        link = json.loads(result.stdout)['command_link']
        self.assertEqual(link['status'], 'warning')
        self.assertEqual(link['reason'], 'unsafe_managed_ancestry')
        self.assertIs(link['plain_wb_ready'], False)
        self.assertIn(str(self.bin), link['detail'])
        self.assertEqual(link['fix'], f'chmod g-w {self.bin}')

    def test_exit_status_stays_zero_because_the_documented_contract_is_a_nonfatal_warning(self):
        binary = self.prepare()
        self.assertEqual(self.public('setup', '--omp', str(binary)).returncode, 0)

    def test_healthy_link_reports_plain_wb_ready_and_no_failure_text(self):
        self.make_bin()
        binary = self.executable('chosen')
        result = self.public('setup', '--omp', str(binary), '--json')
        link = json.loads(result.stdout)['command_link']
        self.assertEqual(link['status'], 'created')
        self.assertIs(link['plain_wb_ready'], True)
        self.assertIsNone(link['detail'])
        text = self.public('setup', '--omp', str(binary)).stdout
        self.assertNotIn('will not start', text)

    def test_setup_that_cannot_proceed_at_all_names_the_offending_ancestor(self):
        # No OMP anywhere: setup stops before any link, in an ancestor of the managed prefix.
        self.make_bin(local=0o775)
        result = self.public('setup')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('unsafe_managed_ancestry', result.stdout)
        self.assertIn(str(self.local), result.stdout)
        self.assertIn(f'chmod g-w {self.local}', result.stdout)
        failed = json.loads(self.public('setup', '--json').stdout)
        self.assertEqual(failed['fix'], f'chmod g-w {self.local}')
        self.assertIn(str(self.local), failed['detail'])


class DoctorOmpSelection(DiagnosticsBase):
    def omp_check(self, *flags):
        result = self.public('doctor', *flags)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        return result.stdout

    def test_doctor_text_shows_reason_code_detail_and_fix_instead_of_only_unknown(self):
        self.make_bin(bin=0o775)
        out = self.omp_check()
        omp = out[out.index('] omp:'):].split('\n[', 1)[0]
        self.assertIn('unsafe_managed_ancestry', omp)
        self.assertIn(str(self.bin), omp)
        self.assertIn('group-writable', omp)
        self.assertIn(f'chmod g-w {self.bin}', omp)
        self.assertNotIn('확인 불가', omp)

    def test_doctor_json_carries_the_same_reason_in_the_omp_check(self):
        self.make_bin(bin=0o775)
        checks = {item['id']: item for item in json.loads(self.omp_check('--json'))['checks']}
        omp = checks['omp']
        self.assertEqual(omp['status'], 'error')
        self.assertIn('unsafe_managed_ancestry', omp['summary'])
        self.assertIn(str(self.bin), omp['summary'])
        self.assertIn(f'chmod g-w {self.bin}', omp['hint'])
        self.assertEqual(set(omp), {'id', 'status', 'summary', 'hint'})


if __name__ == '__main__':
    unittest.main()
