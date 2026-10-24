"""``wb hub add/remove``, ``wb init`` and ``wb start`` registration (HB-04, seams S1 + S3).

Every ``wb`` here is a real subprocess in a temp HOME/XDG sandbox (never the real
``~/.local/state``). Projects are made with ``initialize_project`` in the test process; the
"on" state comes from ``fixtures/contract_backend.py``. Expected values are the test's own
paths, the project_id read from the marker file, and registry/.wb bytes and modes.
Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

REPO = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'contract_backend.py'
sys.path.insert(0, str(REPO / 'src'))

from workbench.backend.project import initialize_project, resolve_project_context  # noqa: E402


class CliCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-hubcli-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        for name in ('home', 'state'):
            (self.base / name).mkdir(mode=0o700)
        # A short private XDG_RUNTIME_DIR so the runtime root never falls back to /tmp/omp-workbench-<uid>
        # (a long TMPDIR such as the shm_tests root would push bridge.sock past the socket path limit).
        self.run = Path(tempfile.mkdtemp(prefix='r')).resolve()
        self.addCleanup(shutil.rmtree, self.run, ignore_errors=True)
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.base / 'home'), 'LANG': 'C.UTF-8',
                    'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'XDG_STATE_HOME': str(self.base / 'state'), 'XDG_RUNTIME_DIR': str(self.run),
                    'WORKBENCH_HUB_AUTOSTART': '0'}
        self.registry_path = self.base / 'state' / 'omp-workbench' / 'hub' / 'registry.json'
        self.children: list[subprocess.Popen] = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if child.stdout:
                child.stdout.close()

    def wb(self, *args, cwd=None, env=None, timeout=60):
        return subprocess.run([sys.executable, '-m', 'workbench', *args], cwd=cwd or self.base,
                              env=env or self.env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout)

    def folder(self, name: str) -> Path:
        path = self.base / 'work' / name
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def project(self, name: str) -> Path:
        """An initialized WB project folder."""
        root = self.folder(name)
        initialize_project(resolve_project_context(root, self.env), confirm=lambda _root: True,
                           interactive=True, environment=self.env)
        return root

    def marker_id(self, root: Path) -> str:
        return json.loads((root / '.wb' / 'project.json').read_text())['project_id']

    def registry(self) -> dict:
        return json.loads(self.registry_path.read_text())

    def registered(self) -> list[dict]:
        return self.registry()['projects'] if self.registry_path.exists() else []

    def seed_registry(self, projects: list[dict]) -> bytes:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.registry_path.write_text(json.dumps({'schema_version': 1, 'projects': projects}))
        os.chmod(self.registry_path, 0o600)
        return self.registry_path.read_bytes()

    def entry(self, root: Path, project_id: str, source: str = 'add') -> dict:
        return {'root': str(root), 'project_id': project_id, 'name': root.name,
                'registered_at': 1.0, 'source': source}


class HubAddTests(CliCase):
    def test_add_registers_an_initialized_project_with_its_marker_id(self):
        root = self.project('alpha')
        result = self.wb('hub', 'add', str(root))
        self.assertEqual(result.returncode, 0, result.stderr)
        entries = self.registered()
        self.assertEqual(len(entries), 1)
        self.assertEqual({k: entries[0][k] for k in ('root', 'project_id', 'name', 'source')},
                         {'root': str(root), 'project_id': self.marker_id(root), 'name': 'alpha', 'source': 'add'})
        self.assertEqual(stat.S_IMODE(os.lstat(self.registry_path).st_mode), 0o600)

    def test_add_accepts_a_relative_path_and_adding_twice_keeps_one_entry(self):
        root = self.project('alpha')
        self.assertEqual(self.wb('hub', 'add', '.', cwd=root).returncode, 0)
        before = self.registry_path.read_bytes()
        again = self.wb('hub', 'add', str(root))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.registry_path.read_bytes(), before)
        self.assertEqual(len(self.registered()), 1)

    def test_add_of_an_uninitialized_folder_points_to_init_and_creates_nothing(self):
        folder = self.folder('plain')
        result = self.wb('hub', 'add', str(folder))
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'wb init {folder}', result.stderr)
        self.assertFalse(os.path.lexists(folder / '.wb'))
        self.assertFalse(self.registry_path.exists())

    def test_add_of_a_subfolder_names_the_project_root(self):
        root = self.project('alpha')
        sub = root / 'src' / 'deep'
        sub.mkdir(parents=True)
        result = self.wb('hub', 'add', str(sub))
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(root), result.stderr)
        self.assertFalse(os.path.lexists(sub / '.wb'))
        self.assertFalse(self.registry_path.exists())

    def test_add_of_a_missing_path_fails_cleanly(self):
        result = self.wb('hub', 'add', str(self.base / 'nope'))
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)

    def test_add_refuses_a_root_registered_under_another_id_and_an_id_under_another_root(self):
        alpha, beta = self.project('alpha'), self.project('beta')
        before = self.seed_registry([self.entry(alpha, str(uuid.uuid4())),
                                     self.entry(self.folder('moved'), self.marker_id(beta))])
        same_root = self.wb('hub', 'add', str(alpha))
        self.assertEqual(same_root.returncode, 1)
        self.assertIn('wb hub remove', same_root.stderr)
        same_id = self.wb('hub', 'add', str(beta))
        self.assertEqual(same_id.returncode, 1)
        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_add_with_a_damaged_registry_fails_and_keeps_its_bytes(self):
        root = self.project('alpha')
        for label, payload, mode in (('json', b'{"projects": [', 0o600),
                                     ('schema', b'{"schema_version": 9, "projects": []}', 0o600),
                                     ('mode', b'{"schema_version": 1, "projects": []}', 0o644)):
            with self.subTest(label):
                self.registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.registry_path.unlink(missing_ok=True)
                self.registry_path.write_bytes(payload)
                os.chmod(self.registry_path, mode)
                result = self.wb('hub', 'add', str(root))
                self.assertEqual(result.returncode, 1)
                self.assertIn(str(self.registry_path), result.stderr)
                self.assertEqual(self.registry_path.read_bytes(), payload)
                self.assertEqual(stat.S_IMODE(os.lstat(self.registry_path).st_mode), mode)

    def test_four_concurrent_adds_of_different_roots_all_land(self):
        roots = [self.project(f'p{i}') for i in range(4)]
        children = [subprocess.Popen([sys.executable, '-m', 'workbench', 'hub', 'add', str(root)], env=self.env,
                                     cwd=self.base, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True) for root in roots]
        self.addCleanup(lambda: [child.kill() for child in children if child.poll() is None])
        for child in children:
            _out, err = child.communicate(timeout=60)
            self.assertEqual(child.returncode, 0, err)
        self.assertEqual(sorted(e['root'] for e in self.registered()), sorted(str(r) for r in roots))


def tree_state(directory: Path) -> dict:
    """Every path below ``directory``: type, mode, mtime and (for files) bytes."""
    state = {}
    for path in [directory, *sorted(directory.rglob('*'))]:
        info = os.lstat(path)
        state[str(path.relative_to(directory))] = (
            stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_mtime_ns,
            path.read_bytes() if stat.S_ISREG(info.st_mode) else None)
    return state


class CliWithBackendCase(CliCase):
    def run_contract_backend(self, root: Path, *extra: str, umask: int | None = None) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, str(FIXTURE), str(root), *extra], env=self.env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, preexec_fn=None if umask is None else (lambda: os.umask(umask)))
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), 'ready', child.poll())
        return child


class ContractBackendFixtureTests(CliWithBackendCase):
    def test_runtime_directories_it_creates_are_private_under_any_umask(self):
        for umask in (0o000, 0o022, 0o077):
            with self.subTest(umask=oct(umask)):
                root = self.project(f'umask{umask:o}')
                self.run_contract_backend(root, umask=umask)
                runtime = Path(json.loads((root / '.wb' / 'backend.json').read_text())['runtime_root'])
                below_run = runtime.relative_to(self.run).parts
                self.assertTrue(below_run)
                for depth in range(1, len(below_run) + 1):
                    directory = self.run / Path(*below_run[:depth])
                    self.assertEqual(stat.S_IMODE(os.lstat(directory).st_mode), 0o700, directory)


class HubRemoveTests(CliWithBackendCase):
    def test_remove_drops_the_entry_keeps_every_wb_byte_and_re_add_keeps_the_id(self):
        root, other = self.project('alpha'), self.project('beta')
        self.assertEqual(self.wb('hub', 'add', str(root)).returncode, 0)
        self.assertEqual(self.wb('hub', 'add', str(other)).returncode, 0)
        project_id, before = self.marker_id(root), tree_state(root / '.wb')
        result = self.wb('hub', 'remove', str(root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([e['root'] for e in self.registered()], [str(other)])
        self.assertEqual(tree_state(root / '.wb'), before)
        self.assertEqual(self.wb('hub', 'add', str(root)).returncode, 0)
        readded = [e for e in self.registered() if e['root'] == str(root)]
        self.assertEqual([e['project_id'] for e in readded], [project_id])

    def test_remove_of_an_unregistered_path_fails_and_leaves_the_registry(self):
        root, other = self.project('alpha'), self.project('beta')
        self.assertEqual(self.wb('hub', 'add', str(other)).returncode, 0)
        before = self.registry_path.read_bytes()
        result = self.wb('hub', 'remove', str(root))
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(root), result.stderr)
        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_remove_of_a_running_project_is_refused_until_it_is_off(self):
        root = self.project('alpha')
        self.assertEqual(self.wb('hub', 'add', str(root)).returncode, 0)
        before = self.registry_path.read_bytes()
        backend = self.run_contract_backend(root)
        refused = self.wb('hub', 'remove', str(root))
        self.assertEqual(refused.returncode, 1)
        self.assertIn('먼저 끄세요', refused.stderr)
        self.assertEqual(self.registry_path.read_bytes(), before)
        backend.send_signal(signal.SIGTERM)
        backend.wait(timeout=10)
        allowed = self.wb('hub', 'remove', str(root))
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(self.registered(), [])

    def test_remove_of_a_mismatched_or_vanished_entry_is_allowed(self):
        alpha, gone = self.project('alpha'), self.folder('gone')
        self.seed_registry([self.entry(alpha, str(uuid.uuid4())), self.entry(gone, str(uuid.uuid4()))])
        self.assertEqual(self.wb('hub', 'remove', str(alpha)).returncode, 0)
        self.assertEqual(self.wb('hub', 'remove', str(gone)).returncode, 0)
        self.assertEqual(self.registered(), [])

    def test_remove_with_a_damaged_registry_fails_and_keeps_its_bytes(self):
        root = self.project('alpha')
        self.registry_path.parent.mkdir(parents=True, mode=0o700)
        self.registry_path.write_bytes(b'not json')
        os.chmod(self.registry_path, 0o600)
        result = self.wb('hub', 'remove', str(root))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.registry_path.read_bytes(), b'not json')


def run_in_pty(argv: list[str], *, env: dict, cwd: Path, answer: str | None, timeout: float = 60):
    """Run ``argv`` on a pseudo terminal; type ``answer`` when a ``[y/N]`` prompt appears."""
    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover - child side
        try:
            os.chdir(cwd)
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    output = b''
    answered = answer is None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b''
                if not chunk:
                    break
                output += chunk
                if not answered and b'[y/N]' in output:
                    os.write(master, answer.encode() + b'\n')
                    answered = True
        else:
            os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(master)
    return os.waitstatus_to_exitcode(status), output.decode(errors='replace')


class InitTests(CliWithBackendCase):
    def init_in_pty(self, path: Path | None, answer: str | None, *, cwd: Path | None = None):
        argv = [sys.executable, '-m', 'workbench', 'init', *([str(path)] if path else [])]
        return run_in_pty(argv, env=self.env, cwd=cwd or self.base, answer=answer)

    def test_init_with_y_creates_wb_registers_it_and_starts_no_backend(self):
        folder = self.folder('fresh')
        code, output = self.init_in_pty(folder, 'y')
        self.assertEqual(code, 0, output)
        self.assertIn(str(folder / '.wb'), output)
        self.assertTrue((folder / '.wb' / 'project.json').exists())
        entries = self.registered()
        self.assertEqual([(e['root'], e['source'], e['project_id']) for e in entries],
                         [(str(folder), 'init', self.marker_id(folder))])
        self.assertFalse((folder / '.wb' / 'backend.json').exists())
        self.assertFalse((folder / '.wb' / 'backend.lock').exists())

    def test_init_defaults_to_the_current_directory(self):
        folder = self.folder('here')
        code, output = self.init_in_pty(None, 'y', cwd=folder)
        self.assertEqual(code, 0, output)
        self.assertEqual([e['root'] for e in self.registered()], [str(folder)])

    def test_init_declined_or_eof_exits_2_and_creates_nothing(self):
        for label, answer in (('n', 'n'), ('eof', '\x04')):
            with self.subTest(label):
                folder = self.folder(f'declined-{label}')
                code, output = self.init_in_pty(folder, answer)
                self.assertEqual(code, 2, output)
                self.assertNotIn('Traceback', output)
                self.assertFalse(os.path.lexists(folder / '.wb'))
                self.assertFalse(self.registry_path.exists())

    def test_init_of_a_file_path_fails_before_prompting(self):
        plain = self.folder('plain') / 'file.txt'
        plain.write_text('x')
        code, output = self.init_in_pty(plain, 'y')
        self.assertEqual(code, 1, output)
        self.assertNotIn('[y/N]', output)
        self.assertNotIn('Traceback', output)
        self.assertIn(str(plain), output)
        self.assertFalse(os.path.lexists(plain.parent / '.wb'))
        self.assertFalse(self.registry_path.exists())

    def test_init_without_a_terminal_exits_2_and_creates_nothing(self):
        folder = self.folder('piped')
        result = self.wb('init', str(folder))
        self.assertEqual(result.returncode, 2)
        self.assertIn('terminal', result.stderr)
        self.assertFalse(os.path.lexists(folder / '.wb'))
        self.assertFalse(self.registry_path.exists())

    def test_init_of_a_subfolder_is_refused_without_nesting(self):
        root = self.project('alpha')
        sub = root / 'sub'
        sub.mkdir()
        code, output = self.init_in_pty(sub, 'y')
        self.assertEqual(code, 1, output)
        self.assertIn(str(root), output)
        self.assertFalse(os.path.lexists(sub / '.wb'))
        self.assertFalse(self.registry_path.exists())

    def test_init_of_an_existing_project_only_registers_it(self):
        root = self.project('alpha')
        before = tree_state(root / '.wb')
        result = self.wb('init', str(root))  # no terminal needed: nothing to confirm
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tree_state(root / '.wb'), before)
        self.assertEqual([(e['root'], e['source']) for e in self.registered()], [(str(root), 'init')])

    def test_init_with_a_damaged_registry_still_initializes_and_warns(self):
        folder = self.folder('fresh')
        self.registry_path.parent.mkdir(parents=True, mode=0o700)
        self.registry_path.write_bytes(b'not json')
        os.chmod(self.registry_path, 0o600)
        code, output = self.init_in_pty(folder, 'y')
        self.assertEqual(code, 0, output)
        self.assertTrue((folder / '.wb' / 'project.json').exists())
        self.assertIn(str(self.registry_path), output)
        self.assertEqual(self.registry_path.read_bytes(), b'not json')


class StartRegistrationTests(CliWithBackendCase):
    def start(self, root: Path, *extra: str, env: dict | None = None):
        return self.wb('start', '--no-attach', *extra, cwd=root, env=env)

    def test_start_joining_a_running_project_registers_it_even_without_a_prior_registry(self):
        root = self.project('alpha')
        self.run_contract_backend(root)
        self.assertFalse(self.registry_path.exists())
        result = self.start(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([(e['root'], e['source'], e['project_id']) for e in self.registered()],
                         [(str(root), 'start', self.marker_id(root))])

    def test_start_keeps_an_existing_entry_and_its_source(self):
        root = self.project('alpha')
        self.assertEqual(self.wb('hub', 'add', str(root)).returncode, 0)
        before = self.registry_path.read_bytes()
        self.run_contract_backend(root)
        self.assertEqual(self.start(root).returncode, 0)
        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_start_continues_with_one_warning_when_the_registry_is_damaged(self):
        root = self.project('alpha')
        self.registry_path.parent.mkdir(parents=True, mode=0o700)
        self.registry_path.write_bytes(b'not json')
        os.chmod(self.registry_path, 0o600)
        self.run_contract_backend(root)
        result = self.start(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        warnings = [line for line in result.stderr.splitlines() if str(self.registry_path) in line]
        self.assertEqual(len(warnings), 1, result.stderr)
        self.assertEqual(self.registry_path.read_bytes(), b'not json')

    def test_start_warns_and_keeps_the_entry_when_the_root_has_another_id(self):
        root = self.project('alpha')
        before = self.seed_registry([self.entry(root, str(uuid.uuid4()))])
        self.run_contract_backend(root)
        result = self.start(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('wb hub remove', result.stderr)
        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_start_with_a_data_dir_that_is_not_a_dot_wb_registers_nothing(self):
        root = self.project('alpha')
        store = root / 'store'
        shutil.copytree(root / '.wb', store)
        backend_env = dict(self.env, WORKBENCH_DATA_DIR=str(store))
        child = subprocess.Popen([sys.executable, str(FIXTURE), str(root)], env=backend_env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), 'ready', child.poll())
        result = self.start(root, '--data-dir', str(store))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.registry_path.exists())


if __name__ == '__main__':
    unittest.main()
