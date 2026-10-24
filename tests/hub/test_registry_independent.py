"""Independent verification of the Hub registry, ``wb init`` and ``wb hub add/remove`` (HB-04).

Seams: S3 (``workbench.hub.registry`` API plus the bytes it leaves on disk, multi-process) and S1
(the ``wb`` CLI as a subprocess or on a pseudo terminal). Expected values come from SPEC
"Registry 계약"/"CLI 계약" and the HB-04 acceptance criteria, never from the implementation:
the tests' own paths and uuids, project_id read from the marker file, file bytes, modes,
inodes and mtimes.

Everything runs in a temp HOME/XDG sandbox (never the real ``~/.local/state``); every child
process is owned by the test and reaped on cleanup. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import fcntl
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
from workbench.hub import registry  # noqa: E402
from workbench.hub.paths import HubLayout  # noqa: E402


def snapshot(path: Path) -> tuple:
    """Identity of one path without following symlinks: type, mode, inode, mtime_ns, content."""
    info = os.lstat(path)
    content = None
    if stat.S_ISREG(info.st_mode):
        content = Path(path).read_bytes()
    elif stat.S_ISLNK(info.st_mode):
        content = os.readlink(path)
    return (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_ino, info.st_mtime_ns, content)


def tree(directory: Path) -> dict:
    """Every path under ``directory`` (itself included), by relative name."""
    result = {'.': snapshot(directory)}
    for path in sorted(directory.rglob('*')):
        result[str(path.relative_to(directory))] = snapshot(path)
    return result


def registry_doc(projects: list[dict]) -> bytes:
    return json.dumps({'schema_version': 1, 'projects': projects}).encode()


def entry_dict(root: Path, project_id: str, source: str = 'add') -> dict:
    return {'root': str(root), 'project_id': project_id, 'name': Path(root).name,
            'registered_at': 1.0, 'source': source}


class Sandbox(unittest.TestCase):
    """Temp HOME/state/runtime dirs, a ``wb`` runner and helpers shared by all cases."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-regind-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        for name in ('home', 'state', 'run', 'work'):
            (self.base / name).mkdir(mode=0o700)
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.base / 'home'), 'LANG': 'C.UTF-8',
                    'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'XDG_STATE_HOME': str(self.base / 'state'), 'XDG_RUNTIME_DIR': str(self.base / 'run'),
                    'WORKBENCH_HUB_AUTOSTART': '0'}
        self.state_root = self.base / 'state' / 'omp-workbench'
        self.state_dir = self.state_root / 'hub'
        self.registry_path = self.state_dir / 'registry.json'
        self.layout = HubLayout(self.state_dir)
        self.children: list[subprocess.Popen] = []
        self.addCleanup(self.reap_children)

    # -- process ownership ------------------------------------------------------------------

    def reap_children(self):
        for child in self.children:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            for stream in (child.stdout, child.stderr, child.stdin):
                if stream:
                    stream.close()

    def popen(self, argv, **kwargs) -> subprocess.Popen:
        kwargs.setdefault('env', self.env)
        kwargs.setdefault('stdin', subprocess.DEVNULL)
        kwargs.setdefault('stdout', subprocess.PIPE)
        kwargs.setdefault('stderr', subprocess.PIPE)
        kwargs.setdefault('text', True)
        child = subprocess.Popen(argv, **kwargs)
        self.children.append(child)
        return child

    def wb(self, *args, cwd=None, env=None, timeout=60, stdin=subprocess.DEVNULL):
        return subprocess.run([sys.executable, '-m', 'workbench', *args], cwd=cwd or self.base,
                              env=env or self.env, stdin=stdin, capture_output=True, text=True,
                              timeout=timeout)

    # -- projects ---------------------------------------------------------------------------

    def folder(self, name: str) -> Path:
        path = self.base / 'work' / name
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def project(self, name: str) -> Path:
        root = self.folder(name)
        initialize_project(resolve_project_context(root, self.env), confirm=lambda _root: True,
                           interactive=True, environment=self.env)
        return root

    def marker_id(self, root: Path) -> str:
        return json.loads((Path(root) / '.wb' / 'project.json').read_text())['project_id']

    def registered(self) -> list[dict]:
        if not self.registry_path.exists():
            return []
        return json.loads(self.registry_path.read_text())['projects']

    def seed_state_dir(self) -> None:
        self.state_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.state_dir.exists():
            self.state_dir.mkdir(mode=0o700)

    def clear_state_dir(self) -> None:
        self.seed_state_dir()
        for stale in self.state_dir.iterdir():
            if stale.is_dir() and not stale.is_symlink():
                stale.rmdir()
            else:
                os.chmod(stale, 0o600) if not stale.is_symlink() and not stale.is_fifo() else None
                stale.unlink()

    def seed_registry(self, payload: bytes | list[dict], mode: int = 0o600) -> bytes:
        self.seed_state_dir()
        data = registry_doc(payload) if isinstance(payload, list) else payload
        self.registry_path.write_bytes(data)
        os.chmod(self.registry_path, mode)
        return data

    def run_contract_backend(self, root: Path) -> subprocess.Popen:
        child = self.popen([sys.executable, str(FIXTURE), str(root)])
        self.assertEqual(child.stdout.readline().strip(), 'ready', child.poll())
        return child

    def hold_flock(self, path: Path) -> subprocess.Popen:
        code = ("import fcntl,os,sys,time\n"
                "fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600)\n"
                "fcntl.flock(fd,fcntl.LOCK_EX)\nprint('held',flush=True)\ntime.sleep(120)\n")
        child = self.popen([sys.executable, '-c', code, str(path)])
        self.assertEqual(child.stdout.readline().strip(), 'held')
        return child

    def wb_pty(self, args: list[str], *, answer: bytes | None, cwd: Path | None = None,
               timeout: float = 60) -> tuple[int, str]:
        """Run ``wb`` on a pseudo terminal; type ``answer`` (raw bytes) at the first ``[y/N]``."""
        argv = [sys.executable, '-m', 'workbench', *args]
        pid, master = pty.fork()
        if pid == 0:  # pragma: no cover - child side
            try:
                os.chdir(cwd or self.base)
                os.execve(argv[0], argv, self.env)
            finally:
                os._exit(127)
        output, answered, deadline = b'', answer is None, time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if not ready:
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b''
                if not chunk:
                    break
                output += chunk
                if not answered and b'[y/N]' in output:
                    os.write(master, answer)
                    answered = True
            else:
                os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
        finally:
            os.close(master)
        return os.waitstatus_to_exitcode(status), output.decode(errors='replace')

    def assert_no_traceback(self, result):
        self.assertNotIn('Traceback', result.stderr)


# --- S3: registry file under concurrency and crashes ----------------------------------------

WRITER = '''
import sys, time, uuid
from pathlib import Path
from workbench.hub import registry
from workbench.hub.paths import HubLayout
layout, gate, tag, count, projects = HubLayout(Path(sys.argv[1])), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), Path(sys.argv[5])
while not gate.exists():
    time.sleep(0.001)
for number in range(count):
    root = projects / f"{tag}-{number}"
    root.mkdir(parents=True, exist_ok=True)
    registry.ensure(layout, root, str(uuid.uuid4()), "add")
'''

READER = '''
import json, sys
from pathlib import Path
from workbench.hub import registry
from workbench.hub.paths import HubLayout
layout, stop = HubLayout(Path(sys.argv[1])), Path(sys.argv[2])
reads = failures = 0
while not stop.exists():
    reads += 1
    try:
        registry.read(layout)
        json.loads((layout.state_dir / "registry.json").read_bytes())
    except Exception as exc:  # a torn or unreadable file is the failure being hunted
        failures += 1
        print("FAIL", type(exc).__name__, str(exc)[:200], flush=True)
print("DONE", reads, failures, flush=True)
'''

SAME_ROOT = '''
import sys, time
from pathlib import Path
from workbench.hub import registry
from workbench.hub.paths import HubLayout
layout, gate, root, project_id = HubLayout(Path(sys.argv[1])), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
while not gate.exists():
    time.sleep(0.001)
try:
    result = registry.ensure(layout, root, project_id, "add")
    print("ADDED" if result.added else "SAME", flush=True)
except registry.RegistryError as exc:
    print("REFUSED", exc.reason, flush=True)
'''

REMOVER = '''
import sys, time
from pathlib import Path
from workbench.hub import registry
from workbench.hub.paths import HubLayout
layout, gate = HubLayout(Path(sys.argv[1])), Path(sys.argv[2])
while not gate.exists():
    time.sleep(0.001)
for victim in sys.argv[3:]:
    registry.remove(layout, Path(victim))
'''

CRASHER = '''
import os, signal, sys, uuid
from pathlib import Path
point = sys.argv[3]
def die(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)
if point == "fsync":
    os.fsync = die
elif point == "replace":
    os.replace = die
    os.rename = die
elif point == "enospc":
    def full(*_args, **_kwargs):
        raise OSError(28, "No space left on device")
    os.fsync = full
from workbench.hub import registry
from workbench.hub.paths import HubLayout
try:
    registry.ensure(HubLayout(Path(sys.argv[1])), Path(sys.argv[2]), str(uuid.uuid4()), "add")
except Exception as exc:
    print("RAISED", type(exc).__name__, flush=True)
    sys.exit(7)
'''


class RegistryWriteSafetyTests(Sandbox):
    def python(self, code: str, *args: str) -> subprocess.Popen:
        return self.popen([sys.executable, '-c', code, *args])

    def finish(self, child: subprocess.Popen, timeout: float = 90) -> tuple[int, str, str]:
        out, err = child.communicate(timeout=timeout)
        return child.returncode, out, err

    def test_six_writers_and_a_reader_lose_nothing_and_never_see_a_torn_file(self):
        projects = self.base / 'projects'
        seeded = [entry_dict(self.base / 'seed' / f's{i}', str(uuid.uuid4())) for i in range(400)]
        self.seed_registry(seeded)
        gate, stop = self.base / 'go', self.base / 'stop'
        reader = self.python(READER, str(self.state_dir), str(stop))
        writers = [self.python(WRITER, str(self.state_dir), str(gate), f'p{i}', '12', str(projects))
                   for i in range(6)]
        time.sleep(0.3)
        gate.touch()
        for writer in writers:
            code, _out, err = self.finish(writer)
            self.assertEqual(code, 0, err)
        stop.touch()
        code, out, err = self.finish(reader, timeout=30)
        self.assertEqual(code, 0, err)
        done = [line for line in out.splitlines() if line.startswith('DONE')]
        self.assertEqual(len(done), 1, out)
        _tag, reads, failures = done[0].split()
        self.assertGreater(int(reads), 20, 'reader barely ran; the torn-read check proved nothing')
        self.assertEqual(int(failures), 0, out)
        roots = {entry['root'] for entry in self.registered()}
        expected = {str(projects / f'p{i}-{n}') for i in range(6) for n in range(12)}
        expected |= {entry['root'] for entry in seeded}
        self.assertEqual(roots, expected)
        self.assertEqual(len(self.registered()), 400 + 72)
        self.assertEqual(stat.S_IMODE(os.lstat(self.registry_path).st_mode), 0o600)

    def test_one_root_raced_by_nine_processes_ends_as_a_single_entry(self):
        root = self.folder('shared')
        gate = self.base / 'go'
        project_id = str(uuid.uuid4())
        same = [self.python(SAME_ROOT, str(self.state_dir), str(gate), str(root), project_id) for _ in range(6)]
        rivals = [self.python(SAME_ROOT, str(self.state_dir), str(gate), str(root), str(uuid.uuid4()))
                  for _ in range(3)]
        time.sleep(0.3)
        gate.touch()
        outcomes = [self.finish(child)[1].strip() for child in same + rivals]
        entries = self.registered()
        self.assertEqual(len(entries), 1, entries)
        self.assertEqual(entries[0]['root'], str(root))
        self.assertEqual(outcomes.count('ADDED'), 1, outcomes)
        self.assertTrue(all(o in ('ADDED', 'SAME', 'REFUSED registry_mismatch') for o in outcomes), outcomes)
        if entries[0]['project_id'] == project_id:
            self.assertEqual(outcomes[6:], ['REFUSED registry_mismatch'] * 3, outcomes)
            self.assertNotIn('REFUSED registry_mismatch', outcomes[:6])

    def test_removals_racing_additions_neither_resurrect_nor_drop_entries(self):
        victims = [self.base / 'victims' / f'v{i}' for i in range(6)]
        keepers = [entry_dict(self.base / 'keep' / f'k{i}', str(uuid.uuid4())) for i in range(5)]
        self.seed_registry([entry_dict(v, str(uuid.uuid4())) for v in victims] + keepers)
        gate, projects = self.base / 'go', self.base / 'projects'
        children = [self.python(REMOVER, str(self.state_dir), str(gate), *map(str, victims))]
        children += [self.python(WRITER, str(self.state_dir), str(gate), f'p{i}', '6', str(projects))
                     for i in range(3)]
        time.sleep(0.3)
        gate.touch()
        for child in children:
            code, _out, err = self.finish(child)
            self.assertEqual(code, 0, err)
        roots = {entry['root'] for entry in self.registered()}
        self.assertTrue(roots.isdisjoint(str(v) for v in victims), roots)
        self.assertTrue({k['root'] for k in keepers} <= roots)
        self.assertTrue({str(projects / f'p{i}-{n}') for i in range(3) for n in range(6)} <= roots)
        self.assertEqual(len(roots), 5 + 18)

    def test_a_sigkilled_lock_holder_does_not_block_later_writers(self):
        registry.ensure(self.layout, self.folder('alpha'), str(uuid.uuid4()), 'init')
        holder = self.hold_flock(self.state_dir / 'registry.lock')
        with self.assertRaises(registry.RegistryError) as raised:
            registry.ensure(self.layout, self.folder('beta'), str(uuid.uuid4()), 'add', lock_timeout=0.2)
        self.assertEqual(raised.exception.reason, 'registry_busy')
        holder.kill()
        holder.wait()
        started = time.monotonic()
        registry.ensure(self.layout, self.folder('beta'), str(uuid.uuid4()), 'add')
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual([Path(e['root']).name for e in self.registered()], ['alpha', 'beta'])

    def test_a_writer_killed_before_the_rename_leaves_the_old_bytes_and_later_writers_recover(self):
        alpha = self.folder('alpha')
        registry.ensure(self.layout, alpha, str(uuid.uuid4()), 'init')
        before = snapshot(self.registry_path)
        for point in ('fsync', 'replace'):
            with self.subTest(point):
                child = self.python(CRASHER, str(self.state_dir), str(self.folder('crash-' + point)), point)
                code, out, err = self.finish(child)
                self.assertEqual(code, -signal.SIGKILL, (code, out, err))
                self.assertEqual(snapshot(self.registry_path), before)
                self.assertEqual([e['root'] for e in self.registered()], [str(alpha)])
                self.assertEqual([e.root for e in registry.read(self.layout)], [str(alpha)])
        beta = self.folder('beta')  # the dead writers' flock died with them
        registry.ensure(self.layout, beta, str(uuid.uuid4()), 'add')
        self.assertEqual([e['root'] for e in self.registered()], [str(alpha), str(beta)])

    def test_a_failing_disk_write_keeps_the_old_bytes_and_leaves_no_temp_file(self):
        registry.ensure(self.layout, self.folder('alpha'), str(uuid.uuid4()), 'init')
        before = snapshot(self.registry_path)
        child = self.python(CRASHER, str(self.state_dir), str(self.folder('full')), 'enospc')
        code, out, err = self.finish(child)
        self.assertEqual(code, 7, (out, err))
        self.assertIn('RAISED', out)
        self.assertEqual(snapshot(self.registry_path), before)
        leftovers = sorted(p.name for p in self.state_dir.iterdir()
                           if p.name not in ('registry.json', 'registry.lock'))
        self.assertEqual(leftovers, [])


# --- S3: refusal without modification ---------------------------------------------------------

def good_entry(name: str = 'alpha') -> dict:
    return entry_dict(Path('/nonexistent/hb04') / name, str(uuid.uuid4()))


def with_entry(**changes) -> bytes:
    return registry_doc([good_entry() | changes])


def duplicate_root() -> bytes:
    a = good_entry('a')
    return registry_doc([a, good_entry('b') | {'root': a['root']}])


def duplicate_id() -> bytes:
    a = good_entry('a')
    return registry_doc([a, good_entry('b') | {'project_id': a['project_id']}])


DAMAGED_CONTENT = {
    'garbage': b'this is not json',
    'empty': b'',
    'truncated': registry_doc([good_entry()])[:-9],
    'nul-bytes': b'\x00' * 64,
    'invalid-utf8': b'{"schema_version": 1, "projects": [\xff\xfe]}',
    'top-level-list': b'[]',
    'future-schema': json.dumps({'schema_version': 2, 'projects': []}).encode(),
    'schema-as-string': json.dumps({'schema_version': '1', 'projects': []}).encode(),
    'projects-not-a-list': json.dumps({'schema_version': 1, 'projects': {}}).encode(),
    'missing-projects': json.dumps({'schema_version': 1}).encode(),
    'relative-root': with_entry(root='relative/alpha'),
    'unknown-source': with_entry(source='hand-edited'),
    'bad-uuid': with_entry(project_id='not-a-uuid'),
    'uppercase-uuid': with_entry(project_id=str(uuid.uuid4()).upper()),
    'bool-registered-at': with_entry(registered_at=True),
    'string-registered-at': with_entry(registered_at='yesterday'),
    'extra-key': with_entry(extra='x'),
    'missing-key': registry_doc([{k: v for k, v in good_entry().items() if k != 'name'}]),
    'duplicate-root': duplicate_root(),
    'duplicate-project-id': duplicate_id(),
    'oversize': b'{"schema_version": 1, "projects": [], "pad": "' + b'x' * (5 << 20) + b'"}',
}


class RefusalTests(Sandbox):
    """A registry or state dir that is not the contract shape is reported and never rewritten."""

    def assert_refused_untouched(self, call, *, reason='registry_invalid'):
        before_file = snapshot(self.registry_path)
        with self.assertRaises(registry.RegistryError) as raised:
            call()
        self.assertEqual(raised.exception.reason, reason)
        self.assertIn(str(self.registry_path), raised.exception.detail)
        self.assertEqual(snapshot(self.registry_path), before_file)
        extras = sorted(p.name for p in self.state_dir.iterdir()
                        if p.name not in ('registry.json', 'registry.lock'))
        self.assertEqual(extras, [])

    def calls(self):
        root = self.folder('victim')
        return {'read': lambda: registry.read(self.layout),
                'ensure': lambda: registry.ensure(self.layout, root, str(uuid.uuid4()), 'add'),
                'remove': lambda: registry.remove(self.layout, root)}

    def test_damaged_content_is_reported_by_every_entry_point_and_never_rewritten(self):
        for label, payload in DAMAGED_CONTENT.items():
            for name, call in self.calls().items():
                with self.subTest(label=label, call=name):
                    self.clear_state_dir()
                    self.seed_registry(payload)
                    self.assert_refused_untouched(call)

    def test_a_registry_that_is_not_exactly_0600_is_refused_even_when_its_content_is_valid(self):
        good = registry_doc([entry_dict(self.folder('alpha'), str(uuid.uuid4()))])
        for mode in (0o644, 0o640, 0o660, 0o666, 0o400, 0o700):
            for name, call in self.calls().items():
                with self.subTest(mode=oct(mode), call=name):
                    self.clear_state_dir()
                    self.seed_registry(good, mode)
                    self.assert_refused_untouched(call)

    def test_a_symlinked_registry_file_is_refused_and_its_target_never_touched(self):
        self.seed_state_dir()
        target = self.base / 'elsewhere.json'
        target.write_bytes(registry_doc([]))
        os.chmod(target, 0o600)
        self.registry_path.symlink_to(target)
        before_target = snapshot(target)
        for name, call in self.calls().items():
            with self.subTest(call=name):
                with self.assertRaises(registry.RegistryError) as raised:
                    call()
                self.assertEqual(raised.exception.reason, 'registry_invalid')
                self.assertTrue(self.registry_path.is_symlink())
                self.assertEqual(snapshot(target), before_target)

    def test_a_dangling_registry_symlink_does_not_create_its_target(self):
        self.seed_state_dir()
        target = self.base / 'will-not-exist.json'
        self.registry_path.symlink_to(target)
        for name, call in self.calls().items():
            with self.subTest(call=name):
                with self.assertRaises(registry.RegistryError):
                    call()
                self.assertFalse(os.path.lexists(target))
                self.assertTrue(self.registry_path.is_symlink())

    def test_a_registry_that_is_a_fifo_or_a_directory_is_refused_without_blocking(self):
        for kind in ('fifo', 'directory'):
            for name, call in self.calls().items():
                with self.subTest(kind=kind, call=name):
                    self.clear_state_dir()
                    if kind == 'fifo':
                        os.mkfifo(self.registry_path, 0o600)
                    else:
                        self.registry_path.mkdir(mode=0o700)
                    started = time.monotonic()
                    with self.assertRaises(registry.RegistryError) as raised:
                        call()
                    self.assertLess(time.monotonic() - started, 5)
                    self.assertEqual(raised.exception.reason, 'registry_invalid')
                    self.assertEqual(stat.S_IFMT(os.lstat(self.registry_path).st_mode),
                                     stat.S_IFIFO if kind == 'fifo' else stat.S_IFDIR)

    def test_a_symlinked_state_directory_is_refused_and_the_real_directory_is_left_alone(self):
        real = self.base / 'real-state'
        real.mkdir(mode=0o700)
        (real / 'registry.json').write_bytes(registry_doc([]))
        os.chmod(real / 'registry.json', 0o600)
        self.state_dir.parent.mkdir(parents=True, mode=0o700)
        self.state_dir.symlink_to(real, target_is_directory=True)
        before = tree(real)
        for name, call in self.calls().items():
            with self.subTest(call=name):
                with self.assertRaises(registry.RegistryError) as raised:
                    call()
                self.assertEqual(raised.exception.reason, 'registry_invalid')
                self.assertEqual(tree(real), before)
                self.assertTrue(self.state_dir.is_symlink())

    def test_a_state_path_that_is_a_regular_file_is_refused_and_kept(self):
        self.state_dir.parent.mkdir(parents=True, mode=0o700)
        self.state_dir.write_bytes(b'not a directory')
        before = snapshot(self.state_dir)
        for name, call in self.calls().items():
            with self.subTest(call=name):
                with self.assertRaises(registry.RegistryError):
                    call()
                self.assertEqual(snapshot(self.state_dir), before)

    def test_a_symlinked_lock_file_is_refused_without_following_it(self):
        self.seed_registry([])
        target = self.base / 'lock-target'
        target.write_bytes(b'precious')
        os.chmod(target, 0o640)
        (self.state_dir / 'registry.lock').symlink_to(target)
        before, registry_before = snapshot(target), snapshot(self.registry_path)
        with self.assertRaises(registry.RegistryError) as raised:
            registry.ensure(self.layout, self.folder('a'), str(uuid.uuid4()), 'add')
        self.assertEqual(raised.exception.reason, 'registry_busy')
        self.assertEqual(snapshot(target), before)
        self.assertEqual(snapshot(self.registry_path), registry_before)

    def test_a_loose_state_directory_never_ends_with_a_world_readable_registry(self):
        self.seed_state_dir()
        os.chmod(self.state_dir, 0o755)
        mode_before = stat.S_IMODE(os.lstat(self.state_dir).st_mode)
        self.assertEqual(registry.read(self.layout), [])  # reading never changes anything
        self.assertEqual(stat.S_IMODE(os.lstat(self.state_dir).st_mode), mode_before)
        try:
            registry.ensure(self.layout, self.folder('alpha'), str(uuid.uuid4()), 'add')
        except registry.RegistryError:
            self.assertFalse(self.registry_path.exists())
            return
        # SPEC: the state dir is 0700; a writer that accepts the directory must have made it private
        self.assertEqual(stat.S_IMODE(os.lstat(self.state_dir).st_mode) & 0o077, 0)
        self.assertEqual(stat.S_IMODE(os.lstat(self.registry_path).st_mode), 0o600)

    def test_read_of_a_missing_registry_is_empty_and_creates_nothing(self):
        self.assertEqual(registry.read(self.layout), [])
        self.assertFalse(os.path.lexists(self.state_root))


class CanonicalRootTests(Sandbox):
    """The API itself resolves roots, so no caller can register one project under two spellings."""

    def test_ensure_and_remove_resolve_symlinks_dots_and_relative_spellings(self):
        real = self.folder('real')
        (real / 'sub').mkdir()
        link = self.base / 'alias'
        link.symlink_to(real, target_is_directory=True)
        project_id = str(uuid.uuid4())
        first = registry.ensure(self.layout, link, project_id, 'add')
        self.assertTrue(first.added)
        self.assertEqual((first.entry.root, first.entry.name), (str(real), 'real'))
        before = self.registry_path.read_bytes()
        for spelling in (real, Path(str(real) + '/'), real / 'sub' / '..', link / '.',
                         Path(os.path.relpath(real, Path.cwd()))):
            with self.subTest(spelling=str(spelling)):
                again = registry.ensure(self.layout, spelling, project_id, 'init')
                self.assertFalse(again.added)
                self.assertEqual(self.registry_path.read_bytes(), before)
                with self.assertRaises(registry.RegistryMismatch):
                    registry.ensure(self.layout, spelling, str(uuid.uuid4()), 'add')
        removed = registry.remove(self.layout, real / 'sub' / '..')
        self.assertEqual(removed.root, str(real))
        self.assertEqual(self.registered(), [])
        self.assertIsNone(registry.remove(self.layout, link))


class CliRefusalTests(Sandbox):
    """The same damage seen through ``wb``: add/remove fail with exit 1, auto registration warns."""

    def damaged_variants(self):
        good = registry_doc([entry_dict(self.folder('other'), str(uuid.uuid4()))])
        return {'garbage': (b'{{{', 0o600), 'future-schema': (DAMAGED_CONTENT['future-schema'], 0o600),
                'mode-0644': (good, 0o644), 'duplicate-root': (DAMAGED_CONTENT['duplicate-root'], 0o600)}

    def test_add_and_remove_fail_with_exit_1_naming_the_file_and_keep_every_byte(self):
        root = self.project('alpha')
        for label, (payload, mode) in self.damaged_variants().items():
            for args in (('hub', 'add', str(root)), ('hub', 'remove', str(root))):
                with self.subTest(label=label, command=args[1]):
                    self.clear_state_dir()
                    self.seed_registry(payload, mode)
                    before = snapshot(self.registry_path)
                    result = self.wb(*args)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(str(self.registry_path), result.stderr)
                    self.assert_no_traceback(result)
                    self.assertEqual(snapshot(self.registry_path), before)

    def test_symlinked_state_directory_refuses_add_but_never_blocks_start_or_init(self):
        root = self.project('alpha')
        real = self.base / 'real-state'
        real.mkdir(mode=0o700)
        self.state_dir.parent.mkdir(parents=True, mode=0o700)
        self.state_dir.symlink_to(real, target_is_directory=True)
        before = tree(real)
        add = self.wb('hub', 'add', str(root))
        self.assertEqual(add.returncode, 1)
        self.assert_no_traceback(add)
        self.run_contract_backend(root)
        start = self.wb('start', '--no-attach', cwd=root)
        self.assertEqual(start.returncode, 0, start.stderr)
        warnings = [line for line in start.stderr.splitlines() if 'registry' in line.lower()]
        self.assertEqual(len(warnings), 1, start.stderr)
        fresh = self.folder('fresh')
        code, output = self.wb_pty(['init', str(fresh)], answer=b'y\n')
        self.assertEqual(code, 0, output)  # initialization is not blocked by a registry problem
        self.assertTrue((fresh / '.wb' / 'project.json').exists())
        self.assertEqual(tree(real), before)

    def test_start_with_a_0644_registry_warns_once_and_keeps_going(self):
        root = self.project('alpha')
        payload = self.seed_registry([entry_dict(self.folder('other'), str(uuid.uuid4()))], 0o644)
        self.run_contract_backend(root)
        before = snapshot(self.registry_path)
        result = self.wb('start', '--no-attach', cwd=root)
        self.assertEqual(result.returncode, 0, result.stderr)
        warnings = [line for line in result.stderr.splitlines() if str(self.registry_path) in line]
        self.assertEqual(len(warnings), 1, result.stderr)
        self.assertEqual(self.registry_path.read_bytes(), payload)
        self.assertEqual(snapshot(self.registry_path), before)

    def test_start_with_the_registry_lock_held_warns_busy_and_does_not_change_the_registry(self):
        root = self.project('alpha')
        self.seed_registry([])
        before = snapshot(self.registry_path)
        self.run_contract_backend(root)
        self.hold_flock(self.state_dir / 'registry.lock')
        started = time.monotonic()
        result = self.wb('start', '--no-attach', cwd=root, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn('registry_busy', result.stderr)
        self.assertEqual(snapshot(self.registry_path), before)


# --- S1: wb hub add ----------------------------------------------------------------------------

class HubAddTests(Sandbox):
    def test_uninitialized_path_is_refused_with_init_guidance_and_nothing_is_created(self):
        folder = self.folder('plain')
        (folder / 'notes.txt').write_text('mine')
        before_work = tree(self.base / 'work')
        result = self.wb('hub', 'add', str(folder))
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'wb init {folder}', result.stderr)
        self.assert_no_traceback(result)
        self.assertFalse(os.path.lexists(folder / '.wb'))
        self.assertEqual(tree(self.base / 'work'), before_work)
        self.assertFalse(os.path.lexists(self.state_root), 'no state may be created')

    def test_missing_path_and_missing_argument_create_nothing(self):
        ghost = self.base / 'work' / 'ghost' / 'deeper'
        result = self.wb('hub', 'add', str(ghost))
        self.assertEqual(result.returncode, 1)
        self.assert_no_traceback(result)
        self.assertFalse(os.path.lexists(self.base / 'work' / 'ghost'))
        self.assertEqual(self.wb('hub', 'add').returncode, 2)
        self.assertEqual(self.registered(), [])
        self.assertFalse(os.path.lexists(self.state_root))

    def test_every_spelling_of_one_root_resolves_to_a_single_canonical_entry(self):
        root = self.project('alpha')
        (root / 'sub').mkdir()
        link = self.base / 'links' / 'to-alpha'
        link.parent.mkdir()
        link.symlink_to(root, target_is_directory=True)
        spellings = [(str(root), self.base), (str(root) + '/', self.base), (str(root) + '//', self.base),
                     (f'{root}/./', self.base), (f'{root}/sub/..', self.base), (str(link), self.base),
                     (str(link) + '/', self.base), ('.', root), ('..', root / 'sub'),
                     (os.path.relpath(root, self.base), self.base)]
        first_bytes = None
        for spelling, cwd in spellings:
            with self.subTest(spelling=spelling):
                result = self.wb('hub', 'add', spelling, cwd=cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                entries = self.registered()
                self.assertEqual(len(entries), 1, entries)
                self.assertEqual({k: entries[0][k] for k in ('root', 'name', 'source', 'project_id')},
                                 {'root': str(root), 'name': 'alpha', 'source': 'add',
                                  'project_id': self.marker_id(root)})
                if first_bytes is None:
                    first_bytes = self.registry_path.read_bytes()
                self.assertEqual(self.registry_path.read_bytes(), first_bytes, 'idempotent add rewrote the file')

    def test_a_symlink_with_another_name_registers_the_real_root_and_name(self):
        root = self.project('real-name')
        link = self.base / 'alias'
        link.symlink_to(root, target_is_directory=True)
        self.assertEqual(self.wb('hub', 'add', str(link)).returncode, 0)
        entry = self.registered()[0]
        self.assertEqual((entry['root'], entry['name']), (str(root), 'real-name'))

    def test_subfolders_of_a_project_are_refused_naming_the_root_by_any_route(self):
        root = self.project('alpha')
        sub = root / 'src' / 'deep'
        sub.mkdir(parents=True)
        link = self.base / 'to-sub'
        link.symlink_to(sub, target_is_directory=True)
        self.seed_registry([entry_dict(self.folder('other'), str(uuid.uuid4()))])
        before = snapshot(self.registry_path)
        for spelling in (str(sub), str(sub) + '/', str(link), f'{root}/src/../src/deep'):
            with self.subTest(spelling=spelling):
                result = self.wb('hub', 'add', spelling)
                self.assertEqual(result.returncode, 1)
                self.assertIn(str(root), result.stderr)
                self.assertFalse(os.path.lexists(sub / '.wb'))
                self.assertEqual(snapshot(self.registry_path), before)

    def test_a_project_nested_below_a_registered_root_can_neither_be_made_nor_added(self):
        root = self.project('alpha')
        self.assertEqual(self.wb('hub', 'add', str(root)).returncode, 0)
        nested = root / 'sub' / 'inner'
        nested.mkdir(parents=True)
        before = snapshot(self.registry_path)
        code, output = self.wb_pty(['init', str(nested)], answer=b'y\n')
        self.assertEqual(code, 1, output)
        self.assertNotIn('[y/N]', output)
        self.assertFalse(os.path.lexists(nested / '.wb'))
        self.assertEqual(self.wb('hub', 'add', str(nested)).returncode, 1)
        self.assertEqual(snapshot(self.registry_path), before)

    def test_a_dot_wb_that_does_not_belong_to_the_folder_is_never_registered(self):
        alpha = self.project('alpha')
        copied = self.folder('copied')
        shutil.copytree(alpha / '.wb', copied / '.wb', symlinks=True)  # marker still names alpha
        linked = self.folder('linked')
        (linked / '.wb').symlink_to(alpha / '.wb', target_is_directory=True)
        empty = self.folder('empty')
        (empty / '.wb').mkdir(mode=0o700)
        broken = self.folder('broken')
        (broken / '.wb').mkdir(mode=0o700)
        (broken / '.wb' / 'project.json').write_text('{not json')
        dangling = self.folder('dangling')
        (dangling / '.wb').symlink_to(self.base / 'nowhere')
        names = ('copied', 'linked', 'empty', 'broken', 'dangling')
        guarded = {name: tree(self.base / 'work' / name) for name in names}
        alpha_before = tree(alpha)
        for name in names:
            with self.subTest(name):
                result = self.wb('hub', 'add', str(self.base / 'work' / name))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assert_no_traceback(result)
                self.assertEqual(self.registered(), [])
                self.assertEqual(tree(self.base / 'work' / name), guarded[name])
        self.assertEqual(tree(alpha), alpha_before)
        self.assertFalse(os.path.lexists(self.base / 'nowhere'))

    def test_the_same_project_id_under_another_root_is_rejected_and_the_registry_kept(self):
        first = self.project('first')
        self.assertEqual(self.wb('hub', 'add', str(first)).returncode, 0)
        second = self.project('second')
        marker = json.loads((second / '.wb' / 'project.json').read_text())
        marker['project_id'] = self.marker_id(first)
        (second / '.wb' / 'project.json').write_text(json.dumps(marker))
        before = snapshot(self.registry_path)
        result = self.wb('hub', 'add', str(second))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(snapshot(self.registry_path), before)

    def test_a_root_registered_under_another_id_is_not_overwritten(self):
        root = self.project('alpha')
        self.seed_registry([entry_dict(root, str(uuid.uuid4()))])
        before = snapshot(self.registry_path)
        result = self.wb('hub', 'add', str(root))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('wb hub remove', result.stderr)
        self.assertEqual(snapshot(self.registry_path), before)

    def test_lookup_starts_at_the_path_not_at_workbench_data_dir(self):
        alpha, beta = self.project('alpha'), self.project('beta')
        env = dict(self.env, WORKBENCH_DATA_DIR=str(alpha / '.wb'))
        result = self.wb('hub', 'add', str(beta), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([(e['root'], e['project_id']) for e in self.registered()],
                         [(str(beta), self.marker_id(beta))])

    def test_adding_leaves_every_project_byte_mode_and_mtime_alone_even_while_it_runs(self):
        root = self.project('alpha')
        (root / 'user.txt').write_text('hello')
        self.run_contract_backend(root)
        before = tree(root)
        result = self.wb('hub', 'add', str(root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tree(root), before)
        self.assertEqual(stat.S_IMODE(os.lstat(self.registry_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(self.state_dir).st_mode), 0o700)

    def test_ten_concurrent_add_processes_keep_every_root(self):
        roots = [self.project(f'p{i}') for i in range(8)]
        argvs = [[sys.executable, '-m', 'workbench', 'hub', 'add', str(root)] for root in roots]
        argvs += [[sys.executable, '-m', 'workbench', 'hub', 'add', str(roots[0])]] * 2
        children = [self.popen(argv, cwd=self.base) for argv in argvs]
        for child in children:
            _out, err = child.communicate(timeout=90)
            self.assertEqual(child.returncode, 0, err)
        entries = self.registered()
        self.assertEqual({e['root']: e['project_id'] for e in entries},
                         {str(r): self.marker_id(r) for r in roots})
        self.assertEqual(len(entries), 8)


# --- S1: wb hub remove -------------------------------------------------------------------------

class HubRemoveTests(Sandbox):
    def rich_project(self, name: str) -> Path:
        root = self.project(name)
        data = root / '.wb'
        (data / 'records').mkdir(mode=0o750)
        (data / 'records' / 'a.jsonl').write_text('{"k":1}\n')
        os.chmod(data / 'records' / 'a.jsonl', 0o640)
        (data / 'notes.txt').write_text('history')
        os.chmod(data / 'notes.txt', 0o444)
        (data / 'alias').symlink_to('notes.txt')
        old = time.time() - 86400 * 30
        for path in (data / 'records' / 'a.jsonl', data / 'notes.txt'):
            os.utime(path, (old, old))
        os.utime(data / 'records', (old, old))
        (root / 'main.py').write_text('print(1)\n')
        return root

    def test_remove_changes_no_byte_mode_inode_or_mtime_in_the_project_and_re_add_keeps_the_id(self):
        root, other = self.rich_project('alpha'), self.project('beta')
        for target in (root, other):
            self.assertEqual(self.wb('hub', 'add', str(target)).returncode, 0)
        project_id = self.marker_id(root)
        others_before = [e for e in self.registered() if e['root'] == str(other)]
        before = tree(root)
        result = self.wb('hub', 'remove', str(root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tree(root), before)
        self.assertEqual(self.registered(), others_before)
        self.assertEqual(stat.S_IMODE(os.lstat(self.registry_path).st_mode), 0o600)
        self.assertEqual(self.wb('hub', 'add', str(root)).returncode, 0)
        readded = [e for e in self.registered() if e['root'] == str(root)]
        self.assertEqual([(e['project_id'], e['source']) for e in readded], [(project_id, 'add')])
        self.assertEqual(tree(root), before)

    def test_remove_accepts_every_spelling_and_leaves_other_entries_byte_equal(self):
        keep = [entry_dict(self.base / 'keep' / f'k{i}', str(uuid.uuid4())) for i in range(3)]
        root = self.project('alpha')
        link = self.base / 'alias'
        link.symlink_to(root, target_is_directory=True)
        for spelling in (str(root), str(root) + '/', str(link), f'{root}/.'):
            with self.subTest(spelling=spelling):
                self.seed_registry([keep[0], entry_dict(root, self.marker_id(root)), keep[1], keep[2]])
                result = self.wb('hub', 'remove', spelling)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.registered(), keep)

    def test_remove_of_unregistered_paths_fails_and_creates_no_state(self):
        root = self.project('alpha')
        sub = root / 'sub'
        sub.mkdir()
        result = self.wb('hub', 'remove', str(root))
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(root), result.stderr)
        self.assert_no_traceback(result)
        self.assertFalse(os.path.lexists(self.state_root))
        self.assertEqual(self.wb('hub', 'remove').returncode, 2)
        self.assertEqual(self.wb('hub', 'add', str(root)).returncode, 0)
        before = snapshot(self.registry_path)
        self.assertEqual(self.wb('hub', 'remove', str(sub)).returncode, 1, 'a subfolder is not the registered root')
        self.assertEqual(snapshot(self.registry_path), before)

    def test_a_running_project_is_refused_with_first_shut_it_down_and_nothing_changes(self):
        root = self.rich_project('alpha')
        other = self.project('beta')
        self.assertEqual(self.wb('hub', 'add', str(root)).returncode, 0)
        self.assertEqual(self.wb('hub', 'add', str(other)).returncode, 0)
        backend = self.run_contract_backend(root)
        registry_before, project_before = snapshot(self.registry_path), tree(root)
        refused = self.wb('hub', 'remove', str(root))
        self.assertEqual(refused.returncode, 1)
        self.assertIn('먼저 끄세요', refused.stderr)
        self.assert_no_traceback(refused)
        self.assertEqual(snapshot(self.registry_path), registry_before)
        self.assertEqual(tree(root), project_before)
        backend.send_signal(signal.SIGTERM)
        backend.wait(timeout=10)
        allowed = self.wb('hub', 'remove', str(root))
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual([e['root'] for e in self.registered()], [str(other)])

    def test_a_project_whose_lock_is_held_without_identity_counts_as_unknown_and_is_refused(self):
        root = self.project('alpha')
        self.assertEqual(self.wb('hub', 'add', str(root)).returncode, 0)
        holder = self.hold_flock(root / '.wb' / 'backend.lock')
        before_registry = snapshot(self.registry_path)
        refused = self.wb('hub', 'remove', str(root))
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn('먼저 끄세요', refused.stderr)
        self.assertEqual(snapshot(self.registry_path), before_registry)
        holder.kill()
        holder.wait()
        self.assertEqual(self.wb('hub', 'remove', str(root)).returncode, 0)

    def test_a_backend_killed_hard_is_off_and_its_leftover_files_survive_the_removal(self):
        root = self.project('alpha')
        self.assertEqual(self.wb('hub', 'add', str(root)).returncode, 0)
        backend = self.run_contract_backend(root)
        backend.kill()
        backend.wait()
        leftovers = tree(root)
        self.assertIn('.wb/backend.json', leftovers)
        result = self.wb('hub', 'remove', str(root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tree(root), leftovers)
        self.assertEqual(self.registered(), [])

    def test_a_vanished_project_folder_and_a_changed_marker_can_be_unregistered(self):
        gone, changed = self.base / 'work' / 'gone', self.project('changed')
        self.seed_registry([entry_dict(gone, str(uuid.uuid4())), entry_dict(changed, str(uuid.uuid4()))])
        before = tree(changed)
        self.assertEqual(self.wb('hub', 'remove', str(gone)).returncode, 0)
        self.assertEqual(self.wb('hub', 'remove', str(changed)).returncode, 0)
        self.assertEqual(self.registered(), [])
        self.assertEqual(tree(changed), before)
        self.assertFalse(os.path.lexists(gone), 'remove must not create the vanished folder')


# --- S1: wb init -------------------------------------------------------------------------------

class InitTests(Sandbox):
    def assert_backend_never_started(self, folder: Path):
        data = folder / '.wb'
        self.assertFalse(os.path.lexists(data / 'backend.json'))
        self.assertFalse(os.path.lexists(data / 'runtime.json'))
        lock = data / 'backend.lock'
        if lock.exists():
            fd = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if a backend holds it
            finally:
                os.close(fd)
        self.assertEqual(list(self.base.rglob('*.sock')), [], 'a backend socket exists')
        for proc in Path('/proc').iterdir():
            if proc.name.isdigit():
                try:
                    cmdline = (proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
                except OSError:
                    continue
                self.assertFalse('_backend' in cmdline and str(folder) in cmdline, cmdline)

    def test_y_creates_wb_registers_with_source_init_and_starts_no_backend(self):
        folder = self.folder('fresh')
        code, output = self.wb_pty(['init', str(folder)], answer=b'y\n')
        self.assertEqual(code, 0, output)
        self.assertIn('[y/N]', output)
        self.assertEqual(stat.S_IMODE(os.lstat(folder / '.wb').st_mode), 0o700)
        entries = self.registered()
        self.assertEqual([(e['root'], e['project_id'], e['source'], e['name']) for e in entries],
                         [(str(folder), self.marker_id(folder), 'init', 'fresh')])
        self.assert_backend_never_started(folder)

    def test_only_an_affirmative_answer_creates_anything(self):
        answers = {'n': b'n\n', 'N': b'N\n', 'enter': b'\n', 'no': b'no\n', 'junk': b'maybe\n',
                   'eof': b'\x04', 'space': b' \n'}
        for label, answer in answers.items():
            with self.subTest(label):
                folder = self.folder(f'declined-{label}')
                code, output = self.wb_pty(['init', str(folder)], answer=answer)
                self.assertNotEqual(code, 0, output)
                self.assertFalse(os.path.lexists(folder / '.wb'))
                self.assertFalse(os.path.lexists(self.state_root))
                self.assertEqual([p.name for p in folder.iterdir()], [])

    def test_without_a_terminal_it_exits_2_and_creates_nothing_even_if_y_is_piped(self):
        folder = self.folder('piped')
        with self.subTest('devnull'):
            result = self.wb('init', str(folder))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assert_no_traceback(result)
            self.assertFalse(os.path.lexists(folder / '.wb'))
        with self.subTest('pipe with y'):
            child = self.popen([sys.executable, '-m', 'workbench', 'init', str(folder)], stdin=subprocess.PIPE)
            _out, err = child.communicate('y\ny\n', timeout=60)
            self.assertEqual(child.returncode, 2, err)
            self.assertFalse(os.path.lexists(folder / '.wb'))
        with self.subTest('terminal stdin but piped stdout'):
            master, slave = pty.openpty()
            try:
                child = self.popen([sys.executable, '-m', 'workbench', 'init', str(folder)], stdin=slave)
                os.write(master, b'y\ny\n')
                _out, err = child.communicate(timeout=60)
            finally:
                os.close(slave)
                os.close(master)
            self.assertEqual(child.returncode, 2, err)
            self.assertFalse(os.path.lexists(folder / '.wb'))
        self.assertFalse(os.path.lexists(self.state_root))

    def test_a_subfolder_of_a_project_is_refused_before_any_prompt_and_nothing_is_nested(self):
        root = self.project('alpha')
        sub = root / 'a' / 'b'
        sub.mkdir(parents=True)
        link = self.base / 'to-sub'
        link.symlink_to(sub, target_is_directory=True)
        root_before = tree(root)
        for args, cwd in ((['init', str(sub)], self.base), (['init'], sub), (['init', str(link)], self.base),
                          (['init', '..'], sub)):
            with self.subTest(args=args):
                code, output = self.wb_pty(args, answer=b'y\n', cwd=cwd)
                self.assertEqual(code, 1, output)
                self.assertNotIn('[y/N]', output)
                self.assertIn(str(root), output)
        self.assertEqual(tree(root), root_before)
        self.assertFalse(os.path.lexists(sub / '.wb'))
        self.assertFalse(os.path.lexists(self.state_root))

    def test_an_existing_project_is_only_registered_without_a_terminal_and_without_touching_it(self):
        root = self.project('alpha')
        (root / 'user.txt').write_text('mine')
        before = tree(root)
        result = self.wb('init', str(root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tree(root), before)
        self.assertEqual([(e['root'], e['source']) for e in self.registered()], [(str(root), 'init')])
        again_bytes = self.registry_path.read_bytes()
        self.assertEqual(self.wb('init', str(root)).returncode, 0)
        self.assertEqual(self.registry_path.read_bytes(), again_bytes)

    def test_an_existing_registration_keeps_its_original_source_and_timestamp(self):
        root = self.project('alpha')
        self.assertEqual(self.wb('hub', 'add', str(root)).returncode, 0)
        before = self.registry_path.read_bytes()
        self.assertEqual(self.wb('init', str(root)).returncode, 0)
        self.assertEqual(self.registry_path.read_bytes(), before)
        self.assertEqual([e['source'] for e in self.registered()], ['add'])

    def test_missing_path_and_a_file_path_fail_without_creating_anything(self):
        ghost = self.base / 'work' / 'ghost'
        code, output = self.wb_pty(['init', str(ghost)], answer=b'y\n')
        self.assertEqual(code, 1, output)
        self.assertFalse(os.path.lexists(ghost))
        plain = self.folder('plain') / 'file.txt'
        plain.write_text('x')
        code, output = self.wb_pty(['init', str(plain)], answer=b'y\n')
        self.assertNotEqual(code, 0, output)
        self.assertFalse(os.path.lexists(plain.parent / '.wb'))
        self.assertFalse(os.path.lexists(self.state_root))

    def test_a_dangling_dot_wb_symlink_is_refused_without_creating_its_target(self):
        folder = self.folder('dangling')
        (folder / '.wb').symlink_to(self.base / 'nowhere')
        code, output = self.wb_pty(['init', str(folder)], answer=b'y\n')
        self.assertNotEqual(code, 0, output)
        self.assertNotIn('Traceback', output)
        self.assertFalse(os.path.lexists(self.base / 'nowhere'))
        self.assertTrue((folder / '.wb').is_symlink())
        self.assertEqual(self.registered(), [])

    def test_symlink_and_trailing_slash_paths_initialize_and_register_the_real_root(self):
        real = self.folder('real')
        link = self.base / 'alias'
        link.symlink_to(real, target_is_directory=True)
        code, output = self.wb_pty(['init', str(link) + '/'], answer=b'y\n')
        self.assertEqual(code, 0, output)
        marker = json.loads((real / '.wb' / 'project.json').read_text())
        self.assertEqual(marker['project_root'], str(real))
        self.assertEqual([(e['root'], e['name']) for e in self.registered()], [(str(real), 'real')])

    def test_workbench_data_dir_in_the_environment_does_not_redirect_init(self):
        other = self.project('other')
        folder = self.folder('fresh')
        self.env['WORKBENCH_DATA_DIR'] = str(other / '.wb')
        code, output = self.wb_pty(['init', str(folder)], answer=b'y\n')
        self.assertEqual(code, 0, output)
        self.assertTrue((folder / '.wb' / 'project.json').exists())
        self.assertEqual([e['root'] for e in self.registered()], [str(folder)])

    def test_initialization_still_succeeds_when_the_registry_is_damaged_and_warns_once(self):
        payload = self.seed_registry(b'garbage')
        folder = self.folder('fresh')
        code, output = self.wb_pty(['init', str(folder)], answer=b'y\n')
        self.assertEqual(code, 0, output)
        self.assertTrue((folder / '.wb' / 'project.json').exists())
        self.assertEqual(self.registry_path.read_bytes(), payload)
        self.assertEqual(len([line for line in output.splitlines() if str(self.registry_path) in line]), 1, output)


# --- S1: wb start registration ----------------------------------------------------------------

class StartTests(Sandbox):
    def start(self, cwd: Path, *extra: str, env=None):
        return self.wb('start', '--no-attach', *extra, cwd=cwd, env=env)

    def test_joining_a_running_project_registers_its_root_with_source_start(self):
        root = self.project('alpha')
        sub = root / 'src' / 'deep'
        sub.mkdir(parents=True)
        self.run_contract_backend(root)
        self.assertFalse(os.path.lexists(self.state_root))
        result = self.start(sub)  # started from a subfolder: the project root is what is registered
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([(e['root'], e['project_id'], e['source'], e['name']) for e in self.registered()],
                         [(str(root), self.marker_id(root), 'start', 'alpha')])
        after_first = self.registry_path.read_bytes()
        self.assertEqual(self.start(root).returncode, 0)
        self.assertEqual(self.registry_path.read_bytes(), after_first, 'second start must be idempotent')

    def test_start_through_a_symlinked_cwd_registers_the_canonical_root(self):
        root = self.project('alpha')
        link = self.base / 'alias'
        link.symlink_to(root, target_is_directory=True)
        self.run_contract_backend(root)
        result = self.start(link)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([e['root'] for e in self.registered()], [str(root)])

    def test_an_explicit_dot_wb_data_dir_registers_the_project(self):
        root = self.project('alpha')
        self.run_contract_backend(root)
        result = self.start(self.base, '--data-dir', str(root / '.wb'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([(e['root'], e['source']) for e in self.registered()], [(str(root), 'start')])

    def test_an_earlier_init_registration_keeps_its_source_when_started(self):
        root = self.project('alpha')
        self.assertEqual(self.wb('init', str(root)).returncode, 0)
        before = self.registry_path.read_bytes()
        self.run_contract_backend(root)
        self.assertEqual(self.start(root).returncode, 0)
        self.assertEqual(self.registry_path.read_bytes(), before)
        self.assertEqual([e['source'] for e in self.registered()], ['init'])

    def test_a_start_that_cannot_proceed_registers_nothing(self):
        folder = self.folder('plain')
        result = self.start(folder)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(os.path.lexists(folder / '.wb'))
        self.assertFalse(os.path.lexists(self.state_root))

    def test_commands_other_than_start_init_and_add_do_not_register(self):
        root = self.project('alpha')
        self.run_contract_backend(root)
        self.wb('status', cwd=root, timeout=60)
        self.assertEqual(self.registered(), [])
        self.assertFalse(os.path.lexists(self.registry_path))


if __name__ == '__main__':
    unittest.main()
