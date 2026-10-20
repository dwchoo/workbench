"""Setup public CLI/filesystem behaviors with an external executable stand-in."""
from contextlib import redirect_stdout, redirect_stderr
from hashlib import sha256
from io import StringIO, BytesIO
import json
import errno
import os
import signal
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from workbench.backend.cli import main

ROOT = Path(__file__).resolve().parents[3]


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='wb-setup-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ('home', 'project', 'bin', 'tmp'):
            (self.root / name).mkdir(mode=0o700)
        self.env = {'HOME': str(self.root / 'home'), 'PATH': str(self.root / 'bin'),
                    'TMPDIR': str(self.root / 'tmp'), 'LANG': 'C.UTF-8'}
        self.record = self.root / 'home/.config/omp-workbench/setup.json'

    def executable(self, name='omp', version='99.4.1', *, sdk=True, broken=False, reject_secret=False):
        path = self.root / 'bin' / name
        program = f'''#!{sys.executable}
import json, os, sys, time
if {reject_secret!r} and os.environ.get('OPENAI_API_KEY'): raise SystemExit(23)
if '--version' in sys.argv:
    print({'broken' if broken else 'omp/' + version!r})
    raise SystemExit({1 if broken else 0})
print(json.dumps({{'type':'workbench_setup_ready', 'nonce':os.environ.get('WORKBENCH_SETUP_NONCE'),
    'version':{version!r}, 'sdk':{sdk!r}}}), flush=True)
time.sleep(30)
'''
        path.write_text(program)
        path.chmod(0o700)
        return path

    def cli(self, *arguments):
        stdout, stderr = StringIO(), StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.root / 'project')
            with patch.dict(os.environ, self.env, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    code = main(['setup', *arguments, '--json'])
                except SystemExit as error:
                    code = error.code
        finally:
            os.chdir(previous)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_explicit_existing_version_is_sdk_checked_and_reused_without_project_or_auth_changes(self):
        explicit = self.executable('chosen')
        self.executable(broken=True)
        self.env['WORKBENCH_OMP'] = str(self.root / 'missing-env')
        before = explicit.read_bytes()
        code, output, error = self.cli('--omp', str(explicit))
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertEqual((result['status'], result['version'], result['path']), ('reused', '99.4.1', str(explicit)))
        self.assertEqual(result['sdk']['status'], 'loaded')
        self.assertIn('login', result['login_guidance'].lower())
        self.assertEqual(result['real_models'], 'unverified')
        record = json.loads(self.record.read_text())
        self.assertEqual((record['state'], record['path'], record['binary_sha256']),
                         ('ready', str(explicit), sha256(before).hexdigest()))
        self.assertEqual(explicit.read_bytes(), before)
        self.assertEqual(list((self.root / 'project').iterdir()), [])
        self.assertFalse((self.root / 'home/.omp').exists())
        self.assertEqual(list((self.root / 'tmp').iterdir()), [])


    def test_environment_then_saved_selection_precedes_path_and_selection_read_is_passive(self):
        prior = self.executable('environment-choice', '77.0.0')
        self.executable(version='88.0.0')
        self.env['WORKBENCH_OMP'] = str(prior)
        code, output, error = self.cli()
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['path'], str(prior))
        self.env.pop('WORKBENCH_OMP')
        saved = self.record.read_bytes()
        from workbench.backend.setup import resolve_selection
        selection = resolve_selection(environment=self.env)
        self.assertEqual((selection.path, selection.source), (prior, 'setup'))
        self.assertEqual(self.record.read_bytes(), saved)
        code, output, error = self.cli()
        self.assertEqual(code, 0, error)
        self.assertEqual((json.loads(output)['path'], json.loads(output)['version']), (str(prior), '77.0.0'))


    def test_absent_omp_downloads_one_pinned_stable_into_private_versioned_prefix(self):
        artifact = self.executable('download-fixture', '88.2.3').read_bytes()
        expected_hash = sha256(artifact).hexdigest()
        download_url = 'https://build.stencil.so/fixture/omp'
        metadata = {'build': {'version': '88.2.3', 'channel': 'stable'},
                    'file': {'platform': 'linux', 'arch': 'x86_64', 'name': 'omp-linux-x64',
                             'sha256': expected_hash, 'size': len(artifact)}, 'download': download_url}
        urls = []
        def response(url, **kwargs):
            urls.append(url)
            stream = BytesIO(json.dumps(metadata).encode() if '/latest/' in url else artifact)
            stream.geturl = lambda: url
            return stream
        with patch('workbench.backend.install_recipe._open_https', side_effect=response):
            code, output, error = self.cli()
        self.assertEqual(code, 0, error + output)
        result = json.loads(output)
        self.assertEqual((result['status'], result['version']), ('installed', '88.2.3'))
        binary = Path(result['path'])
        self.assertTrue(binary.is_relative_to(self.root / 'home/.local/share/omp-workbench/tools'))
        self.assertIn('88.2.3', binary.parent.name)
        self.assertEqual(binary.read_bytes(), artifact)
        self.assertEqual(binary.stat().st_mode & 0o777, 0o700)
        record = json.loads(self.record.read_text())
        self.assertEqual((record['binary_sha256'], record['recipe']['version'], record['recipe']['sha256']),
                         (expected_hash, '88.2.3', expected_hash))
        self.assertEqual(len([url for url in urls if '/latest/' in url]), 1)
        self.assertEqual(urls[-1], download_url)
        self.assertEqual(list((self.root / 'project').iterdir()), [])


    def test_both_executable_and_sdk_checks_strip_credentials_and_user_home(self):
        binary = self.executable('guarded', reject_secret=True)
        self.env['OPENAI_API_KEY'] = 'fixture-only-not-a-real-key'
        code, output, error = self.cli('--omp', str(binary))
        self.assertEqual(code, 0, output + error)
        self.assertEqual(json.loads(output)['sdk']['status'], 'loaded')
        self.assertFalse((self.root / 'home/.omp').exists())


    def test_broken_selected_candidate_reports_why_and_preserves_prior_ready(self):
        prior = self.executable('prior')
        self.assertEqual(self.cli('--omp', str(prior))[0], 0)
        saved = self.record.read_bytes()
        bad = self.executable('bad', broken=True)
        self.executable()
        code, output, error = self.cli('--omp', str(bad))
        self.assertEqual(code, 1, error)
        result = json.loads(output)
        self.assertEqual((result['state'], result['path'], result['detail']), ('failed', str(bad), 'broken'))
        self.assertEqual(self.record.read_bytes(), saved)


    def test_shared_program_prefix_lock_serializes_different_config_locations(self):
        binary = self.executable('blocking')
        marker, release = self.root / 'checking', self.root / 'release'
        text = binary.read_text().replace("if '--version' in sys.argv:",
            "if '--version' in sys.argv:\n    from pathlib import Path\n"
            + f"    Path({str(marker)!r}).touch()\n    while not Path({str(release)!r}).exists(): time.sleep(.01)")
        binary.write_text(text)
        other = self.executable('other')
        common = dict(self.env, PYTHONPATH=str(ROOT / 'src'), PYTHONDONTWRITEBYTECODE='1',
                      XDG_DATA_HOME=str(self.root / 'shared-data'))
        first_env = dict(common, XDG_CONFIG_HOME=str(self.root / 'config-a'))
        second_env = dict(common, XDG_CONFIG_HOME=str(self.root / 'config-b'))
        argv = [sys.executable, '-m', 'workbench.backend.cli', 'setup', '--json', '--omp']
        first = subprocess.Popen([*argv, str(binary)], env=first_env, cwd=self.root / 'project',
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline and first.poll() is None:
                time.sleep(.01)
            self.assertTrue(marker.exists(), 'first setup never reached executable check')
            second = subprocess.run([*argv, str(other)], env=second_env, cwd=self.root / 'project',
                                    capture_output=True, text=True, timeout=10)
            release.touch()
            out, err = first.communicate(timeout=15)
            self.assertEqual(first.returncode, 0, out + err)
            self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
            self.assertEqual(json.loads(second.stdout)['reason'], 'setup_in_progress')
            self.assertFalse((self.root / 'config-b/omp-workbench/setup.json').exists())
        finally:
            release.touch()
            if first.poll() is None:
                os.killpg(first.pid, signal.SIGKILL)
                first.wait(timeout=5)
            first.stdout.close(); first.stderr.close()


    def test_selection_sync_failure_restores_exact_previous_ready_record(self):
        prior = self.executable('prior')
        self.assertEqual(self.cli('--omp', str(prior))[0], 0)
        saved = self.record.read_bytes()
        replacement = self.executable('replacement', '99.5.0')
        actual_fsync = os.fsync
        fired = False
        def fail_after_publication(fd):
            nonlocal fired
            if self.record.exists() and self.record.read_bytes() != saved and not fired:
                fired = True
                raise OSError(errno.EIO, 'fixture publication sync failure')
            return actual_fsync(fd)
        with patch('os.fsync', side_effect=fail_after_publication):
            code, output, error = self.cli('--omp', str(replacement))
        self.assertTrue(fired)
        self.assertEqual(code, 1, output + error)
        self.assertEqual(self.record.read_bytes(), saved)


    @unittest.skipUnless(sys.platform == 'linux' and hasattr(os, 'waitid'), 'Linux waitid ownership observation')
    def test_readiness_signals_only_our_still_unreaped_actual_children(self):
        binary = self.executable('owned')
        real_killpg = os.killpg
        ownership = []
        def owned_signal(pgid, signum):
            try:
                observed = os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                ownership.append({'pid': pgid, 'unreaped_child': False})
                raise AssertionError('blocked signal to PID/PGID without unreaped child ownership')
            ownership.append({'pid': pgid, 'unreaped_child': True, 'exited': observed is not None})
            return real_killpg(pgid, signum)
        with patch('os.killpg', side_effect=owned_signal):
            code, output, error = self.cli('--omp', str(binary))
        self.assertEqual(code, 0, output + error)
        self.assertTrue(ownership)
        self.assertTrue(all(item['unreaped_child'] for item in ownership), ownership)
        self.assertEqual(json.loads(self.record.read_text())['state'], 'ready')
        for item in ownership:
            with self.assertRaises(ChildProcessError):
                os.waitid(os.P_PID, item['pid'], os.WEXITED | os.WNOHANG | os.WNOWAIT)


    def test_user_private_config_rejects_shared_nonsticky_ancestry_before_writing(self):
        binary = self.executable('chosen')
        shared = self.root / 'shared'
        shared.mkdir(mode=0o777)
        shared.chmod(0o777)
        self.env['XDG_CONFIG_HOME'] = str(shared / 'config')
        code, output, error = self.cli('--omp', str(binary))
        self.assertEqual(code, 1, output + error)
        self.assertEqual(json.loads(output)['reason'], 'unsafe_managed_ancestry')
        self.assertEqual(list(shared.iterdir()), [])


    def test_selected_binary_in_shared_nonsticky_ancestry_is_never_executed(self):
        binary = self.executable('unsafe')
        marker = self.root / 'executed'
        binary.write_text(binary.read_text().replace('import json, os, sys, time',
            'import json, os, sys, time\nfrom pathlib import Path\n' + f'Path({str(marker)!r}).touch()'))
        shared = self.root / 'shared-bin'
        shared.mkdir(mode=0o777); shared.chmod(0o777)
        binary = binary.rename(shared / 'omp')
        code, output, error = self.cli('--omp', str(binary))
        self.assertEqual(code, 1, output + error)
        self.assertEqual(json.loads(output)['reason'], 'unsafe_managed_ancestry')
        self.assertFalse(marker.exists())
        self.assertFalse(self.record.exists())


    def test_interrupted_download_reports_failure_and_removes_unpublished_staging(self):
        artifact = self.executable('artifact', '88.2.3').read_bytes()
        metadata = {'build': {'version': '88.2.3', 'channel': 'stable'},
                    'file': {'platform': 'linux', 'arch': 'x86_64', 'name': 'omp-linux-x64',
                             'sha256': sha256(artifact).hexdigest(), 'size': len(artifact)},
                    'download': 'https://build.stencil.so/fixture/omp'}
        class InterruptedBody(BytesIO):
            def read1(self, size):
                if self.tell():
                    raise KeyboardInterrupt()
                return self.read(8)
        def response(url, **kwargs):
            stream = BytesIO(json.dumps(metadata).encode()) if '/latest/' in url else InterruptedBody(artifact)
            stream.geturl = lambda: url
            return stream
        with patch('workbench.backend.install_recipe._open_https', side_effect=response):
            code, output, error = self.cli()
        self.assertEqual(code, 1, error)
        self.assertTrue(output, 'interrupted setup must explain its failure')
        self.assertEqual(json.loads(output)['reason'], 'interrupted')
        self.assertFalse(self.record.exists())
        tools = self.root / 'home/.local/share/omp-workbench/tools'
        self.assertEqual(list(tools.iterdir()), [])


    def test_invalid_metadata_checksum_and_network_failures_leave_no_selection_or_installation(self):
        artifact = self.executable('artifact', '88.2.3').read_bytes()
        for case in ('metadata', 'checksum', 'network'):
            with self.subTest(case=case):
                metadata = {'build': {'version': '88.2.3', 'channel': 'stable'},
                    'file': {'platform': 'linux', 'arch': 'x86_64', 'name': 'omp-linux-x64',
                             'sha256': sha256(artifact).hexdigest(), 'size': len(artifact)},
                    'download': 'https://build.stencil.so/fixture/omp'}
                if case == 'metadata': metadata['file'].pop('sha256')
                if case == 'checksum': metadata['file']['sha256'] = '0' * 64
                def response(url, **kwargs):
                    if case == 'network': raise URLError('fixture network unavailable')
                    stream = BytesIO(json.dumps(metadata).encode() if '/latest/' in url else artifact)
                    stream.geturl = lambda: url
                    return stream
                with patch('workbench.backend.install_recipe._open_https', side_effect=response):
                    code, output, error = self.cli()
                self.assertEqual(code, 1, error)
                self.assertEqual(json.loads(output)['state'], 'failed')
                self.assertEqual(json.loads(output)['reason'], {'metadata':'invalid_metadata', 'checksum':'artifact_checksum_mismatch', 'network':'URLError'}[case])
                self.assertFalse(self.record.exists())
                self.assertEqual(list((self.root / 'home/.local/share/omp-workbench/tools').iterdir()), [])

    def test_failed_sdk_and_missing_selected_program_preserve_ready_without_replacement(self):
        prior = self.executable('prior')
        self.assertEqual(self.cli('--omp', str(prior))[0], 0)
        saved = self.record.read_bytes()
        bad = self.executable('no-sdk', sdk=False)
        self.executable()
        for path, reason in ((bad, 'sdk_load_failed'), (self.root / 'missing', 'existing_omp_missing')):
            with self.subTest(path=path):
                with patch('workbench.backend.install_recipe._open_https', side_effect=AssertionError('unexpected replacement download')):
                    code, output, error = self.cli('--omp', str(path))
                self.assertEqual(code, 1, error)
                self.assertEqual(json.loads(output)['reason'], reason)
                self.assertEqual(self.record.read_bytes(), saved)

    def test_symlink_record_and_insecure_managed_directory_are_refused_without_repairing_user_files(self):
        binary = self.executable('chosen')
        self.record.parent.mkdir(parents=True, mode=0o700)
        sentinel = self.root / 'sentinel'
        sentinel.write_bytes(b'preserve this user file')
        self.record.symlink_to(sentinel)
        code, output, error = self.cli('--omp', str(binary))
        self.assertEqual(code, 1, error)
        self.assertTrue(self.record.is_symlink())
        self.assertEqual(sentinel.read_bytes(), b'preserve this user file')
        self.record.unlink()
        owned = self.root / 'home/.local/share/omp-workbench'
        owned.chmod(0o755)
        code, output, error = self.cli('--omp', str(binary))
        self.assertEqual(code, 1, error)
        self.assertEqual(json.loads(output)['reason'], 'unsafe_managed_directory')
        self.assertEqual(owned.stat().st_mode & 0o777, 0o755)
        self.assertFalse(self.record.exists())

    def test_absolute_xdg_and_passive_absent_lookup_and_root_owned_program_inspection(self):
        from workbench.backend.setup import resolve_selection
        self.assertIsNone(resolve_selection(environment=self.env))
        self.assertFalse((self.root / 'home/.config').exists())
        self.env.update(XDG_CONFIG_HOME=str(self.root / 'xdg-config'), XDG_DATA_HOME=str(self.root / 'xdg-data'))
        binary = self.executable('chosen')
        self.assertEqual(self.cli('--omp', str(binary))[0], 0)
        self.assertTrue((self.root / 'xdg-config/omp-workbench/setup.json').is_file())
        self.assertFalse((self.root / 'home/.config').exists())
        # A root-owned read-only executable reaches version-format inspection;
        # it is not refused merely because its owner differs from the WB user.
        code, output, error = self.cli('--omp', '/usr/bin/python3')
        self.assertEqual(code, 1, error)
        self.assertEqual(json.loads(output)['reason'], 'existing_omp_broken')


    def test_path_only_reuse_and_missing_saved_or_environment_choice_never_trigger_replacement(self):
        binary = self.executable(version='67.3.1')
        with patch('workbench.backend.install_recipe._open_https', side_effect=AssertionError('unexpected download')):
            code, output, error = self.cli()
        self.assertEqual(code, 0, error)
        self.assertEqual((json.loads(output)['path'], json.loads(output)['version']), (str(binary), '67.3.1'))
        saved = self.record.read_bytes()
        self.env['WORKBENCH_OMP'] = str(self.root / 'missing-environment')
        code, output, error = self.cli()
        self.assertEqual(code, 1, error)
        self.assertEqual(json.loads(output)['path'], self.env['WORKBENCH_OMP'])
        self.assertEqual(self.record.read_bytes(), saved)
        self.env.pop('WORKBENCH_OMP')
        binary.unlink()
        self.executable('alternative')
        code, output, error = self.cli()
        self.assertEqual(code, 1, error)
        self.assertEqual(json.loads(output)['path'], str(binary))
        self.assertEqual(self.record.read_bytes(), saved)


if __name__ == '__main__':
    unittest.main()
