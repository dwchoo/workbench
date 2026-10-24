"""Requirement-derived migration gaps; private fixtures and public CLI/readers only."""
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]


def source_snapshot(root):
    rows = {}
    for path in (root, *sorted(root.rglob('*'))):
        info = path.lstat()
        row = [stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_size,
               info.st_mtime_ns]
        if stat.S_ISREG(info.st_mode):
            row.append(hashlib.sha256(path.read_bytes()).hexdigest())
        elif stat.S_ISLNK(info.st_mode):
            row.append(os.readlink(path))
        rows[str(path.relative_to(root))] = row
    return rows


class IndependentMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wi-i36-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'legacy'
        self.source.mkdir(mode=0o700)
        self.project = self.base / 'project'
        self.project.mkdir(mode=0o700)
        self.env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                    'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'WORKBENCH_HUB_AUTOSTART': '0'}
        for variable in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME',
                         'XDG_RUNTIME_DIR', 'TMPDIR'):
            directory = self.base / variable.lower()
            directory.mkdir(mode=0o700)
            self.env[variable] = str(directory)
        from workbench.tasks.repository import TaskRepository
        repo = TaskRepository(self.source / 'tasks.sqlite3')
        repo.create_task({'title': 'independent original ' + str(self.source)},
                         task_id='i36-original')
        repo.close()
        (self.source / 'tasks.sqlite3').chmod(0o600)
        record = {'contract': {'name': 'workbench.ui', 'version': 1}, 'phase': 'stopped',
                  'data_dir': str(self.source), 'project_dir': str(self.project),
                  'runtime_root': str(self.source), 'ui_socket': str(self.source / 'ui.sock'),
                  'bridge_socket': str(self.source / 'bridge.sock'), 'uid': os.geteuid(),
                  'boot_id': '00000000-0000-4000-8000-000000000001',
                  'processes': {name: {'pid': 2000000100 + index, 'start_ticks': 91 + index,
                                       'owner_epoch': 1, 'role': name}
                                for index, name in enumerate(('backend', 'manager_omp',
                                                              'worker_omp', 'host_shell'))},
                  'survivors': [], 'shutdown': {'verified': True, 'left_running': [],
                    'omp_children': {'alive': [], 'observed': []}, 'previous_survivors': []}}
        self.write_json(self.source / 'backend.json', record)
        (self.source / 'backend.lock').touch(mode=0o600)

    def write_json(self, path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def cli(self, *arguments):
        return subprocess.run([sys.executable, '-m', 'workbench', *map(str, arguments)],
                              env=self.env, cwd=self.project, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=25)

    def migrate(self):
        return self.cli('migrate', '--from', self.source, '--project', self.project)

    def model_file(self):
        agent = self.source / 'omp-root/agent'
        agent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return agent / 'models.yml'

    def test_multiple_known_providers_models_remain_active_exact_bytes(self):
        content = (b'startup:\n  setupWizard: false\nproviders:\n'
                   b'  astronomy-lab:\n    baseUrl: https://lab.example.invalid:8443/v2/models\n'
                   b'    api: anthropic-messages\n    auth: none\n    models:\n'
                   b'      - id: stellar/v2:small\n        name: "Stellar \\u03b1"\n'
                   b'        contextWindow: 8192\n        maxTokens: 127\n'
                   b'      - id: stellar/v2:large\n'
                   b'  second-lab:\n    baseUrl: http://127.0.0.1:49199/api\n'
                   b'    api: google-generative-ai\n    auth: none\n    models:\n'
                   b'      - id: local-model_7\n        contextWindow: 16384\n')
        self.model_file().write_bytes(content)
        before = source_snapshot(self.source)
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        target = self.project / '.wb'
        self.assertEqual((target / 'omp-root/agent/models.yml').read_bytes(), content)
        self.assertFalse((target / 'migration-history/omp-root/agent/models.yml').exists())
        self.assertEqual(source_snapshot(self.source), before)

    def test_config_whole_input_nested_duplicates_types_and_url_fragments_refuse_before_copy(self):
        valid = (b'providers:\n  spectra-lab:\n    baseUrl: https://lab.example.invalid/v1\n'
                 b'    api: openai-completions\n    auth: none\n    models:\n'
                 b'      - id: fixed-model\n        maxTokens: 255\n')
        private = b'I36_PRIVATE_VALUE'
        variants = {
            'duplicate-root': valid + valid,
            'duplicate-model-field': valid + b'        id: I36_PRIVATE_VALUE\n',
            'duplicate-model-id': valid + b'      - id: fixed-model\n',
            'nested-unknown': valid + b'        executable: I36_PRIVATE_VALUE\n',
            'boolean-token-limit': valid.replace(b'maxTokens: 255', b'maxTokens: true'),
            'fragment-carrier': valid.replace(b'/v1\n', b'/v1#I36_PRIVATE_VALUE\n'),
            'orphan-indented-tail': valid + b'          I36_PRIVATE_VALUE\n',
            'second-document-end': valid + b'...\nI36_PRIVATE_VALUE\n',
        }
        model = self.model_file()
        for label, content in variants.items():
            with self.subTest(label=label):
                model.write_bytes(content)
                before = source_snapshot(self.source)
                result = self.migrate()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('models.yml', result.stderr)
                self.assertNotIn(private.decode(), result.stdout + result.stderr)
                self.assertFalse((self.project / '.wb').exists())
                self.assertEqual(source_snapshot(self.source), before)

    def test_same_pid_distinct_real_flock_is_a_competing_writer(self):
        from workbench.backend.cli import main
        before = source_snapshot(self.source)
        with (self.source / 'backend.lock').open('rb') as competing:
            fcntl.flock(competing.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.dict(os.environ, self.env, clear=True):
                status = main(['migrate', '--from', str(self.source), '--project', str(self.project)])
            self.assertNotEqual(status, 0)
            self.assertFalse((self.project / '.wb').exists())
            self.assertEqual(source_snapshot(self.source), before)
            fcntl.flock(competing.fileno(), fcntl.LOCK_UN)

    def test_visible_marker_then_fsync_failure_holds_public_start_and_status(self):
        from workbench.backend.cli import main
        target = self.project / '.wb'
        original_fsync = os.fsync
        injected = []
        def fail_after_publication(fd):
            if (not injected and stat.S_ISDIR(os.fstat(fd).st_mode)
                    and Path(os.readlink(f'/proc/self/fd/{fd}')) == target
                    and (target / 'project.json').exists()):
                injected.append(json.loads((target / 'project.json').read_text()))
                raise OSError(errno.EIO, 'independent publication failure')
            return original_fsync(fd)
        before = source_snapshot(self.source)
        with patch.dict(os.environ, self.env, clear=True), patch('os.fsync', side_effect=fail_after_publication):
            status = main(['migrate', '--from', str(self.source), '--project', str(self.project)])
        self.assertEqual(status, 1)
        self.assertEqual(len(injected), 1)
        self.assertEqual(injected[0]['state'], 'completed')
        self.assertTrue((target / 'tasks.sqlite3').is_file(), 'incomplete history retained')
        self.assertEqual(json.loads((target / 'migration.json').read_text())['state'], 'failed')
        for arguments in (('status', '--json'), ('start', '--no-attach')):
            result = self.cli(*arguments)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn('invalid project marker', result.stderr)
            self.assertFalse((target / 'backend.json').exists())
        self.assertEqual(source_snapshot(self.source), before)

    @unittest.skipUnless(shutil.which('strace'), 'strace unavailable; no syscall claim')
    def test_public_cli_source_database_opens_readonly_and_wal_row_reopens(self):
        # os._exit leaves committed WAL bytes without checkpoint/connection cleanup.
        seed = """import os,sys
from workbench.tasks.repository import TaskRepository
r=TaskRepository(sys.argv[1]);r._connection.execute('PRAGMA journal_mode=WAL')
r._connection.execute('PRAGMA wal_autocheckpoint=0')
r.create_task({'title':'independent WAL payload'},task_id='i36-wal-only');os._exit(0)
"""
        subprocess.run([sys.executable, '-c', seed, str(self.source / 'tasks.sqlite3')],
                       env=self.env, check=True, timeout=15)
        self.assertGreater((self.source / 'tasks.sqlite3-wal').stat().st_size, 0)
        before = source_snapshot(self.source)
        trace = self.base / 'opens.trace'
        result = subprocess.run(['strace', '-f', '-yy', '-s', '4096', '-e',
                                 'trace=open,openat,openat2', '-o', str(trace), sys.executable,
                                 '-m', 'workbench', 'migrate', '--from', str(self.source),
                                 '--project', str(self.project)], env=self.env, cwd=self.project,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = [line for line in trace.read_text().splitlines()
                 if str(self.source) in line and re.search(r'"(?:[^"\n]*/)?tasks\.sqlite3(?:-(?:wal|shm|journal))?"', line)]
        self.assertGreater(len(lines), 0, 'trace must observe actual source DB opens')
        self.assertTrue(any('tasks.sqlite3-wal' in line for line in lines), lines)
        for line in lines:
            self.assertIn('O_RDONLY', line)
            self.assertNotRegex(line, r'O_RDWR|O_WRONLY|O_CREAT|O_TRUNC')
        self.assertEqual(source_snapshot(self.source), before)
        from workbench.tasks.repository import TaskRepository
        repo = TaskRepository(self.project / '.wb/tasks.sqlite3')
        try:
            self.assertEqual(repo.get_task_spec('i36-wal-only', 1)['spec'],
                             {'title': 'independent WAL payload'})
            self.assertEqual(repo.get_task_spec('i36-original', 1)['spec'],
                             {'title': 'independent original ' + str(self.source)})
        finally:
            repo.close()
        evidence = os.environ.get('WI_I36_EVIDENCE_DIR')
        if evidence:
            Path(evidence, 'source-opens.trace').write_bytes(trace.read_bytes())
            Path(evidence, 'source-open-audit.json').write_text(json.dumps({
                'source': str(self.source), 'observed_source_database_opens': lines,
                'before': before, 'after': source_snapshot(self.source),
                'wal_public_reader': 'i36-wal-only: independent WAL payload'}, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
