"""Requirement-derived public CLI admission checks; no real OMP claim from fixtures.

Set WB_INDEPENDENT_INSTALLED_PREFIX to exercise an existing noneditable artifact.
Real SDK lifecycle coverage is assessed separately from these controlled processes.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[3]
PROJECT_ID = '3610630d-86a3-4d4c-b65d-f5105e9a6dc9'


class PublicProjectCliIndependentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='wi27-', dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.project = self.root / 'research'
        self.project.mkdir()
        self.env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                    'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(REPO / 'src')}
        for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME',
                    'XDG_RUNTIME_DIR', 'TMPDIR'):
            path = self.root / key
            path.mkdir(mode=0o700)
            self.env[key] = str(path)
        self.command_number = 0

    @staticmethod
    def stop_unreaped(child):
        # WNOWAIT retains ownership of this numeric group identity until all signals.
        try:
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)

    def command(self, argv, *, cwd=None, env=None):
        self.command_number += 1
        out = self.root / f'command-{self.command_number}.out'
        err = self.root / f'command-{self.command_number}.err'
        with out.open('wb') as stdout, err.open('wb') as stderr:
            child = subprocess.Popen(argv, cwd=cwd or self.project, env=env or self.env,
                                     stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                     start_new_session=True)
            try:
                deadline = time.monotonic() + 15
                while True:
                    info = os.waitid(os.P_PID, child.pid,
                                     os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if info is not None:
                        code = info.si_status if info.si_code == os.CLD_EXITED else -info.si_status
                        break
                    if time.monotonic() >= deadline:
                        self.fail(f'CLI timeout: {argv!r}')
                    time.sleep(.01)
            finally:
                self.stop_unreaped(child)
        return subprocess.CompletedProcess(argv, code, out.read_text(), err.read_text())

    def cli(self, *args, cwd=None, env=None):
        return self.command([sys.executable, '-m', 'workbench', *args], cwd=cwd, env=env)

    @staticmethod
    def files(path):
        return {str(item.relative_to(path)): item.read_bytes()
                for item in path.rglob('*') if item.is_file()}

    def marker(self, project, project_id=PROJECT_ID):
        data = project / '.wb'
        data.mkdir(mode=0o700)
        path = data / 'project.json'
        path.write_text(json.dumps({'schema_version': 1, 'project_id': project_id,
                                    'project_root': str(project), 'state': 'completed'}))
        path.chmod(0o600)
        return data

    def test_passive_commands_and_missing_omp_leave_no_project_or_user_install(self):
        # Existing option placement is command-local, including nested log actions.
        queries = [('--help',), ('status', '--json'), ('attach', '--plain'),
                   ('shutdown', '--yes', '--json'), ('confirm-boot', '--json'),
                   ('logs', 'raw-status', '--json'),
                   ('logs', 'important', 'status', '--json'),
                   ('logs', 'summary-compat', 'status', '--json'),
                   ('logs', 'summary-compat', 'verify', '--scripted', '--json'),
                   (), ('start', '--no-attach')]
        for args in queries:
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertNotIn('Traceback', result.stderr)
                self.assertEqual(list(self.project.iterdir()), [], result.stdout + result.stderr)
                for key in ('XDG_RUNTIME_DIR', 'XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME'):
                    self.assertEqual(list(Path(self.env[key]).iterdir()), [], (args, key))
                if args in ((), ('start', '--no-attach')):
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('wb setup', result.stderr)
                elif args == ('--help',):
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_precedence_and_legacy_option_positions_use_fixed_expected_roots(self):
        nearest = self.marker(self.project)
        child = self.project / 'experiment'
        child.mkdir()
        explicit, environment = self.root / 'explicit-legacy', self.root / 'environment-legacy'
        env = dict(self.env, WORKBENCH_DATA_DIR=str(environment))
        for args, selected_env, expected in [
            (('status', '--json'), self.env, nearest),
            (('status', '--json'), env, environment),
            (('status', '--data-dir', str(explicit), '--json'), env, explicit),
            (('status', '--json', '--data-dir', str(explicit)), env, explicit),
        ]:
            with self.subTest(args=args, expected=expected):
                result = self.cli(*args, cwd=child, env=selected_env)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(json.loads(result.stdout), {'running': False, 'data_dir': str(expected)})
        self.assertFalse(explicit.exists())
        self.assertFalse(environment.exists())
        self.assertEqual(sorted(p.name for p in nearest.iterdir()), ['project.json'])
        self.assertFalse((child / '.wb').exists())
        self.assertFalse((self.project / '.gitignore').exists())

    def test_invalid_nearest_marker_halts_public_commands_without_parent_fallback(self):
        parent = self.marker(self.project)
        child = self.project / 'child'
        child.mkdir()
        invalid = child / '.wb'
        invalid.write_bytes(b'controlled invalid nearest project\n')
        before = self.files(self.project)
        for args in [(), ('status', '--json'), ('attach', '--plain'),
                     ('shutdown', '--yes'), ('logs', 'summary-compat', 'status', '--json')]:
            with self.subTest(args=args):
                result = self.cli(*args, cwd=child)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(str(invalid), result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertEqual(self.files(self.project), before)
        self.assertEqual(sorted(p.name for p in parent.iterdir()), ['project.json'])

    def owned_backend_fixture(self, data):
        """A held lock and observed socket belong to this unreaped disposable child."""
        ready, connections, signals = (self.root / name for name in ('ready', 'connections', 'signals'))
        code = r'''import fcntl,os,signal,socket,sys,time
from pathlib import Path
data,ready,connections,signals=map(Path,sys.argv[1:])
fd=os.open(data/'backend.lock',os.O_CREAT|os.O_RDWR,0o600)
fcntl.flock(fd,fcntl.LOCK_EX)
def received(number,frame):
    with signals.open('ab') as stream:stream.write(str(number).encode()+b'\n')
signal.signal(signal.SIGTERM,received);signal.signal(signal.SIGINT,received)
with socket.socket(socket.AF_UNIX) as server:
    server.bind(str(data/'ui.sock'));server.listen();server.settimeout(.05)
    ready.touch()
    while True:
        try:client,_=server.accept()
        except TimeoutError:continue
        with connections.open('ab') as stream:stream.write(b'connected\n')
        client.close()
'''
        child = subprocess.Popen([sys.executable, '-I', '-c', code, str(data), str(ready),
                                  str(connections), str(signals)], env=self.env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.stop_unreaped, child)
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists(), 'owned backend fixture not ready')
        fields = Path(f'/proc/{child.pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()
        return child, int(fields[19]), connections, signals

    def assert_held_commands(self, data, record, child, ticks, connections, signals):
        path = data / 'backend.json'
        content = json.dumps(record).encode()
        path.write_bytes(content)
        path.chmod(0o600)
        before = self.files(data)
        for args in [('status', '--json'), ('attach', '--plain'),
                     ('start', '--no-attach', '--timeout', '.1'), ('shutdown', '--yes', '--json')]:
            with self.subTest(args=args):
                result = self.cli(*args, '--data-dir', str(data))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('identity', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertEqual(self.files(data), before)
                self.assertFalse(connections.exists(), 'unverified endpoint was contacted')
                self.assertFalse(signals.exists(), 'unverified process was signaled')
                fields = Path(f'/proc/{child.pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()
                self.assertNotIn(fields[0], (b'Z', b'X'))
                self.assertEqual(int(fields[19]), ticks)

    def test_unknown_identity_holds_start_attach_status_shutdown_without_contact_or_signal(self):
        data = self.root / 'legacy'
        data.mkdir(mode=0o700)
        child, ticks, connections, signals = self.owned_backend_fixture(data)
        base = {'data_dir': str(data), 'project_dir': str(self.project),
                'ui_socket': str(data / 'ui.sock'), 'uid': os.geteuid(),
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'processes': {'backend': {'pid': child.pid, 'start_ticks': ticks}}}
        for change in [
            {'data_dir': str(self.root / 'other-data')},
            {'project_dir': str(self.root / 'missing-project')},
            {'uid': os.geteuid() + 1}, {'boot_id': None},
            {'boot_id': '00000000-0000-0000-0000-000000000000'},
            {'processes': {'backend': {'pid': child.pid, 'start_ticks': ticks + 1}}},
            {'processes': {'backend': {'pid': None, 'start_ticks': ticks}}},
        ]:
            with self.subTest(change=change):
                self.assert_held_commands(data, dict(base, **change), child, ticks, connections, signals)

    def test_live_marked_record_with_missing_runtime_is_held_without_recreation(self):
        data = self.marker(self.project)
        child, ticks, connections, signals = self.owned_backend_fixture(data)
        runtime = self.root / 'lost-runtime'
        record = {'data_dir': str(data), 'project_dir': str(self.project),
                  'project_id': PROJECT_ID, 'runtime_root': str(runtime),
                  'ui_socket': str(runtime / 'ui.sock'), 'bridge_socket': str(runtime / 'bridge.sock'),
                  'uid': os.geteuid(), 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  'processes': {'backend': {'pid': child.pid, 'start_ticks': ticks}}}
        self.assert_held_commands(data, record, child, ticks, connections, signals)
        self.assertFalse(runtime.exists())
        self.assertEqual(list(Path(self.env['XDG_RUNTIME_DIR']).iterdir()), [])

    def test_seeded_task_and_important_content_reopen_only_in_their_selected_project(self):
        # These are fixture writes, not OMP-produced Tasks or important summaries.
        from workbench.tasks.repository import TaskRepository, InvalidRevisionError
        from workbench.storage.important_log.catalog import ImportantLogCatalog
        other = self.root / 'research-B'
        other.mkdir()
        roots = [self.project, other]
        expected = [
            ('92fa4406-2b47-47e2-a706-a63813a4dafa', 'WI27_A_연구 자료', 'WI27_A_중요 원문'),
            ('fb8168e2-c421-436b-b753-467348837f0b', 'WI27_B_연구 자료', 'WI27_B_중요 원문'),
        ]
        for index, (project, (task_id, goal, fact)) in enumerate(zip(roots, expected)):
            self.marker(project, PROJECT_ID if index == 0 else '41453020-d9d9-4924-b203-6f8d93f2771f')
            with TaskRepository(project / '.wb/tasks.sqlite3') as repository:
                repository.create_task({'goal': goal}, task_id=task_id)
            writer = ImportantLogCatalog(project / '.wb/important-logs', create=True)
            written = writer.append({'schemaVersion': 1, 'kind': 'task', 'id': task_id},
                                    'start', [], {'schemaVersion': 1, 'facts': [fact],
                                                 'interpretations': [], 'unknowns': []})
            self.assertEqual(written['state'], 'ok', written)
        for index, (project, (task_id, goal, fact)) in enumerate(zip(roots, expected)):
            other_id, other_goal, other_fact = expected[1 - index]
            child = project / 'experiments'
            child.mkdir()
            with TaskRepository(project / '.wb/tasks.sqlite3') as repository:
                self.assertEqual(repository.get_task_spec(task_id)['spec'], {'goal': goal})
                with self.assertRaises(InvalidRevisionError):
                    repository.get_task_spec(other_id)
                self.assertIsNone(repository.get_current_run(task_id))
            result = self.cli('logs', 'important', 'show', '--task', task_id, '--json', cwd=child)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertTrue(value['complete'], value)
            self.assertEqual([row['entry']['body']['facts'] for row in value['entries']], [[fact]])
            self.assertNotIn(other_fact, result.stdout)
            missing = self.cli('logs', 'important', 'show', '--task', other_id, '--json', cwd=child)
            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertEqual(json.loads(missing.stdout)['entries'], [])
            raw = self.cli('logs', 'raw-status', '--json', cwd=child)
            self.assertEqual(raw.returncode, 0, raw.stderr)
            self.assertEqual(json.loads(raw.stdout)['activation'], 'disabled')
            self.assertFalse((project / '.wb/raw-rotation.json').exists())
            self.assertFalse((child / '.wb').exists())
        self.assertFalse((roots[0] / '.wb/tasks.sqlite3').samefile(roots[1] / '.wb/tasks.sqlite3'))

    @unittest.skipUnless(os.environ.get('WB_INDEPENDENT_INSTALLED_PREFIX'), 'existing noneditable artifact required')
    def test_installed_aliases_ignore_research_module_canaries_on_actual_public_entry(self):
        prefix = Path(os.environ['WB_INDEPENDENT_INSTALLED_PREFIX'])
        poison = self.root / 'research modules'
        poison.mkdir()
        package = poison / 'workbench'
        package.mkdir()
        marker = self.root / 'poison-imported'
        source = f'from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError("research import")\n'
        (package / '__init__.py').write_text(source)
        (poison / 'sitecustomize.py').write_text(source)
        research_bin = self.root / 'research bin'
        research_bin.mkdir()
        for name in ('python', 'python3'):
            path = research_bin / name
            path.write_text(f'#!/bin/sh\n: > "{marker}"\nexit 91\n')
            path.chmod(0o700)
        env = dict(self.env, PYTHONPATH=str(poison), PATH=str(research_bin) + ':/usr/bin:/bin',
                   VIRTUAL_ENV='/research/venv', CONDA_PREFIX='/research/conda')
        for alias in ('wb', 'omp-workbench'):
            for args, expected in [(('--help',), 0), (('status', '--json'), 3), ((), 2)]:
                with self.subTest(alias=alias, args=args):
                    result = self.command([str(prefix / 'bin' / alias), *args], env=env)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    self.assertFalse(marker.exists(), 'public entry executed a research Python import or command')
                    self.assertEqual(list(self.project.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
