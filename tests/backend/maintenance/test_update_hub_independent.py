"""HB-09 independent verification: ``wb update`` stops the Hub before any backend.

Expected values come from the contracts (SPEC "`wb update`와의 관계" and "정지 2단계",
HB-AC-18, WU-AC-02~03, ticket HB-09 "독립 초점"), never from the implementation:

* order: the Hub is gone (kernel: no ``/proc/locks`` holder, process dead) at every backend
  ``shutdown_confirm`` and at the first filesystem effect of the update (updates dir,
  manifest, ``update-resume.json``, installer); it is still alive while backends are only asked
  and while the user is asked;
* a declined or impossible confirmation, an unresponsive Hub that survives SIGTERM, and an
  ``unknown`` Hub leave Hub, backends and files untouched, and no other process gets a signal;
* a Hub that ends on its own between the check and the stop is ``not_running``;
* an auto-start / ``wb hub start`` during the update window is refused (``update_in_progress``);
* afterwards no Hub runs and the update did not start one.

Doubles: ``test_update_cli.InstalledFixture`` (installation, uv/git double, backend client, and the
relocated update fence). The Hub is a real ``wb _hub --foreground`` in the same temp HOME, or an
owned fake peer that logs every signal. Nothing here touches the real maintenance fence or HOME.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import pathlib
import signal
import subprocess
import sys
import textwrap
import time
import unittest
from unittest.mock import patch

import test_update_cli as base

REPO = Path(__file__).resolve().parents[3]
PYTHON = sys.executable  # invoke() patches sys.executable to the fixture install
TASK = [{'kind': 'task_run', 'task_id': 'task-a', 'run_id': 'run-a'}]
GONE = {'lock_holder': None, 'hub_alive': []}
NOTICE = '를 멈춥니다'


def flock_holder(path: Path) -> int | None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    wanted = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    for row in Path('/proc/locks').read_text().splitlines():
        parts = row.split()
        if len(parts) >= 8 and parts[1:4] == ['FLOCK', 'ADVISORY', 'WRITE']:
            major, minor, inode = parts[5].split(':')
            if (int(major, 16), int(minor, 16), int(inode)) == wanted:
                return int(parts[4])
    return None


def proc_state(pid: int) -> bytes | None:
    try:
        return Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def alive(pid: int) -> bool:
    return proc_state(pid) not in (None, b'Z', b'X')


def ticks_of(pid: int) -> int:
    return int(Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])


def wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def tree_hashes(*roots: Path) -> dict[str, str]:
    found = {}
    for root in roots:
        for path in sorted(root.rglob('*')):
            if path.is_file() and not path.is_symlink():
                found[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


class Tty(io.StringIO):
    def isatty(self): return True


# A peer that holds hub.lock (or not), optionally serves a silent socket and a valid-looking
# hub.json, and logs every signal it receives instead of dying on it (SIGTERM is ignored).
FAKE = textwrap.dedent('''
    import fcntl, json, os, signal, socket, sys, threading, time
    cfg = json.loads(sys.argv[1])
    log = lambda text: open(cfg['log'], 'a').write(text + '\\n')
    open(cfg['log'], 'a').close()
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2, signal.SIGQUIT):
        signal.signal(number, lambda n, f: log(signal.Signals(n).name))
    ticks = int(open('/proc/self/stat', 'rb').read().rsplit(b') ', 1)[1].split()[19])
    if cfg.get('lock'):
        os.makedirs(os.path.dirname(cfg['lock']), mode=0o700, exist_ok=True)
        fd = os.open(cfg['lock'], os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if cfg.get('socket'):
        server = socket.socket(socket.AF_UNIX)
        server.bind(cfg['socket']); server.listen(8)
        held = []
        def accept():
            while True:
                held.append(server.accept()[0])  # accepted, never answered
        threading.Thread(target=accept, daemon=True).start()
    if cfg.get('record'):
        record = {'schema_version': 1, 'pid': os.getpid(), 'start_ticks': ticks,
                  'boot_id': open('/proc/sys/kernel/random/boot_id').read().strip(),
                  'uid': os.geteuid(), 'socket': cfg['socket'], 'started_at': 0.0, 'mode': 'foreground',
                  'log': cfg['log'], 'version': {'release': 'x', 'commit': None, 'kind': 'unknown'}}
        fd = os.open(cfg['record'], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, json.dumps(record).encode()); os.close(fd)
    print('ready', os.getpid(), ticks, flush=True)
    while True:
        time.sleep(0.2)
''')

# Another ``wb`` trying to start the Hub while only the update fence is relocated.
FENCE_PRELUDE = ("import os, pathlib\n"
                 "from workbench.backend import maintenance\n"
                 "maintenance.FENCE_BASE = pathlib.Path(os.environ.pop('WB_TEST_FENCE_BASE'))\n")
ENSURE_DRIVER = FENCE_PRELUDE + ("from workbench.hub.process import ensure_hub\n"
                                 "print(ensure_hub(timeout=2).reason)\n")
HUB_START_DRIVER = FENCE_PRELUDE + ("from workbench.backend import cli\n"
                                    "raise SystemExit(cli.main(['hub', 'start']))\n")


def sandbox_hubs(home: Path) -> set[int]:
    """Live ``_hub`` processes whose environment names this sandbox HOME."""
    found = set()
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            argv = Path(f'/proc/{name}/cmdline').read_bytes().split(b'\0')
            environ = Path(f'/proc/{name}/environ').read_bytes().split(b'\0')
        except OSError:
            continue
        if b'_hub' in argv and b'HOME=' + os.fsencode(home) in environ and alive(int(name)):
            found.add(int(name))
    return found


class Fixture(unittest.TestCase):
    setUp_installed = base.InstalledFixture.setUp
    write_install = base.InstalledFixture.write_install
    invoke = base.InstalledFixture.invoke
    project = base.InstalledFixture.project

    def setUp(self):
        self.setUp_installed()
        self.state = self.home / 'state' / 'omp-workbench' / 'hub'
        self.hub_lock = self.state / 'hub.lock'
        self.hub_record = self.state / 'hub.json'
        self.events: list[tuple] = []
        self.owned: list[subprocess.Popen] = []
        self.hub_pids: list[int] = []
        self.disposable: list[subprocess.Popen] = []  # fakes that log and ignore SIGTERM
        self.data_dirs: list[Path] = []
        self.addCleanup(self.reap)
        self.addCleanup(self.assert_no_leak)

    # --- processes this test owns (Popen identity, never a bare pid) -----------------------

    def hub_env(self) -> dict:
        return dict(self.env, PYTHONPATH=str(REPO / 'src'), PYTHONDONTWRITEBYTECODE='1',
                    LANG='C.UTF-8', WORKBENCH_HUB_AUTOSTART='0')

    def popen(self, argv) -> subprocess.Popen:
        process = subprocess.Popen(argv, env=self.hub_env(), cwd=self.home, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                   start_new_session=True)
        self.owned.append(process)
        return process

    def start_hub(self) -> subprocess.Popen:
        hub = self.popen([sys.executable, '-m', 'workbench', '_hub', '--foreground'])

        def ready() -> bool:
            if hub.poll() is not None:
                return True
            try:
                record = json.loads(self.hub_record.read_text())
            except (OSError, ValueError):
                return False
            return record.get('pid') == hub.pid and Path(record.get('socket', '')).exists()

        self.assertTrue(wait_until(ready, 10) and hub.poll() is None, f'Hub did not start: {hub.poll()}')
        self.assertEqual(flock_holder(self.hub_lock), hub.pid)
        self.hub_pids.append(hub.pid)
        return hub

    def fake(self, name: str, *, lock=False, serve=False, record=False) -> tuple[subprocess.Popen, Path]:
        """An owned peer that logs signals; returns it and its signal log."""
        log = self.home / f'{name}.log'
        config = {'log': str(log)}
        if lock:
            self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
            config['lock'] = str(self.hub_lock)
        if serve:
            socket_dir = self.home / f'{name}-sock'
            socket_dir.mkdir(mode=0o700)
            config['socket'] = str(socket_dir / 'hub.sock')
        if record:
            config['record'] = str(self.hub_record)
        process = self.popen([sys.executable, '-c', FAKE, json.dumps(config)])
        self.disposable.append(process)
        line = process.stdout.readline().split()
        self.assertEqual(line[:1], ['ready'], f'{name} did not start')
        self.assertEqual((int(line[1]), int(line[2])), (process.pid, ticks_of(process.pid)))
        return process, log

    def write_record(self, pid: int, start_ticks: int, socket: str = '/nonexistent/hub.sock') -> None:
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        record = {'schema_version': 1, 'pid': pid, 'start_ticks': start_ticks,
                  'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  'uid': os.geteuid(), 'socket': socket, 'started_at': 0.0, 'mode': 'foreground',
                  'log': str(self.state / 'hub.log'), 'version': {'release': 'x', 'commit': None, 'kind': 'unknown'}}
        descriptor = os.open(self.hub_record, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(json.dumps(record))

    def reap(self) -> None:
        for process in self.owned:
            if process.poll() is None:
                try:
                    os.kill(process.pid, signal.SIGCONT)
                    if process in self.disposable:
                        process.kill()  # SIGTERM is deliberately ignored by these
                    else:
                        process.terminate()
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                except ProcessLookupError:
                    pass
            if process.stdout is not None:
                process.stdout.close()

    def assert_no_leak(self) -> None:
        self.reap()  # cleanups run LIFO: the check sees the state after reaping
        self.assertEqual(sandbox_hubs(self.home), set(), 'a Hub of this sandbox outlived the test')
        for process in self.owned:
            self.assertIsNotNone(process.poll())

    # --- recording doubles ------------------------------------------------------------------

    def view(self) -> dict:
        return {'lock_holder': flock_holder(self.hub_lock),
                'hub_alive': [pid for pid in self.hub_pids if alive(pid)]}

    def tool(self, argv, **kwargs):
        argv_text = list(map(str, argv))
        if argv_text[0].endswith('uv') and '--offline' in argv_text:
            self.events.append(('install', '', self.view()))
            if getattr(self, 'install_fails', False):
                return subprocess.CompletedProcess(argv, 1, '', 'installer failed')
        return base.InstalledFixture.tool(self, argv, **kwargs)

    def backend(self, name: str, active=(), *, on_request=None):
        """``InstalledFixture.project`` whose backend client records the Hub view per request."""
        before = set(self.clients)
        project, data, process = self.project(name, list(active))
        client, = (self.clients[key] for key in set(self.clients) - before)
        original = client.request
        self.data_dirs.append(data)

        def request(kind, **kwargs):
            from workbench.contracts.ui_v1 import ClientType
            label = {ClientType.SHUTDOWN_REQUEST: 'request', ClientType.SHUTDOWN_CONFIRM: 'shutdown'}.get(kind)
            if label:
                self.events.append((label, name, self.view()))
                if on_request and kind in on_request:
                    on_request[kind]()
            return original(kind, **kwargs)

        client.request = request
        return project, data, process

    def updates(self) -> Path:
        return self.home / 'state' / 'omp-workbench' / 'updates'

    def kinds(self, *names: str) -> list[str]:
        return [kind for kind, _name, _view in self.events if kind in names]

    @contextlib.contextmanager
    def watched_filesystem(self):
        """Record the Hub view at the update's first filesystem effects."""
        from workbench.backend import update
        updates, original_mkdir, original_write = str(self.updates()), pathlib.Path.mkdir, update.write_private_json

        def mkdir(path, *args, **kwargs):
            if str(path).startswith(updates):
                self.events.append(('mkdir', path.name, self.view()))
            return original_mkdir(path, *args, **kwargs)

        def write(path, *args, **kwargs):
            self.events.append(('write', Path(path).name, self.view()))
            return original_write(path, *args, **kwargs)

        with patch.object(pathlib.Path, 'mkdir', mkdir), patch.object(update, 'write_private_json', write):
            yield

    def nothing_changed(self, status, output, error, *backends):
        """Abort contract: exit 1, no shutdown confirm, no install, no backup/resume file."""
        self.assertEqual(status, 1, output + error)
        self.assertEqual(self.kinds('shutdown', 'install', 'mkdir', 'write'), [])
        for process in backends:
            self.assertIsNone(process.poll())
        self.assertFalse(self.applied.exists())
        self.assertFalse(self.updates().exists())
        for data in self.data_dirs:
            self.assertFalse((data / 'update-resume.json').exists())


class OrderingTests(Fixture):
    def test_hub_is_alive_while_backends_are_only_asked_and_gone_before_every_effect(self):
        hub = self.start_hub()
        _, data_a, backend_a = self.backend('project-a')
        _, data_b, backend_b = self.backend('project-b')
        with self.watched_filesystem():
            status, output, error = self.invoke(('update', '--ref', 'release-1'))
        self.assertEqual(status, 0, output + error)
        self.assertIn(f'Hub(pid {hub.pid}', output)
        asked = [event for event in self.events if event[0] == 'request']
        self.assertEqual([name for _k, name, _v in asked], ['project-a', 'project-b'])
        for _kind, _name, view in asked:  # not vacuous: the Hub did exist when update began
            self.assertEqual(view, {'lock_holder': hub.pid, 'hub_alive': [hub.pid]})
        effects = [event for event in self.events if event[0] != 'request']
        self.assertEqual(self.kinds('shutdown', 'install'), ['shutdown', 'shutdown', 'install'])
        self.assertTrue({'mkdir', 'write', 'shutdown', 'install'} <= {kind for kind, _n, _v in effects})
        for kind, name, view in effects:
            self.assertEqual(view, GONE, f'{kind} {name} saw a live Hub')
        # Every question to a backend precedes the first effect, and the first effect is the
        # updates dir (so nothing was written before the Hub stopped).
        last_ask = max(i for i, e in enumerate(self.events) if e[0] == 'request')
        first_effect = min(i for i, e in enumerate(self.events) if e[0] != 'request')
        self.assertLess(last_ask, first_effect)
        self.assertEqual(self.events[first_effect][0], 'mkdir')
        self.assertEqual(hub.wait(timeout=5), 0)
        self.assertIsNotNone(backend_a.poll())
        self.assertIsNotNone(backend_b.poll())
        # the resume files were written for both projects, after the Hub had gone
        self.assertEqual(sorted(name for kind, name, _v in effects if name == 'update-resume.json'),
                         ['update-resume.json'] * 2)

    def test_hub_with_no_project_at_all_is_still_stopped_before_the_installer(self):
        hub = self.start_hub()
        status, output, error = self.invoke()
        self.assertEqual(status, 0, output + error)
        self.assertEqual(self.kinds('install'), ['install'])
        self.assertEqual(self.events[0][2], GONE)
        self.assertEqual(hub.wait(timeout=5), 0)

    def test_consent_comes_first_and_the_hub_runs_until_then(self):
        hub = self.start_hub()
        _, data, backend = self.backend('project-a', TASK)
        prompts = []

        def answer(prompt=''):
            prompts.append({'prompt': prompt, 'printed': sys.stdout.getvalue(), 'view': self.view()})
            return 'y'

        with patch('builtins.input', answer), self.watched_filesystem():
            status, output, error = self.invoke(stdin=Tty('y\n'))
        self.assertEqual(status, 0, output + error)
        self.assertEqual(len(prompts), 1)
        asked, = prompts
        self.assertEqual(asked['view'], {'lock_holder': hub.pid, 'hub_alive': [hub.pid]})
        self.assertIn('task-a', asked['printed'])
        # The Hub line is part of the same confirmation list, after the work listing.
        self.assertIn(f'Hub(pid {hub.pid}', asked['printed'])
        self.assertLess(asked['printed'].index('task-a'), asked['printed'].index('Hub(pid'))
        self.assertEqual(output.count(NOTICE), 1)  # one question, one Hub line
        self.assertEqual(output.count('[y/N]') + asked['prompt'].count('[y/N]'), 1)
        for kind, name, view in self.events:
            if kind in ('mkdir', 'write', 'shutdown', 'install'):
                self.assertEqual(view, GONE, f'{kind} {name}')
        self.assertEqual(hub.wait(timeout=5), 0)
        self.assertIsNotNone(backend.poll())


class DeclineTests(Fixture):
    def test_a_declined_or_unanswerable_confirmation_leaves_hub_backend_and_files_untouched(self):
        hub = self.start_hub()
        project, data, backend = self.backend('project-a', TASK)
        files = tree_hashes(project, data, self.state)
        record_before = self.hub_record.read_bytes()
        for label, stdin in (('no', Tty('n\n')), ('eof', Tty('')), ('not a tty', io.StringIO('y\n'))):
            with self.subTest(label), self.watched_filesystem():
                self.events.clear()
                status, output, error = self.invoke(stdin=stdin)
                self.assertEqual(status, 1, output + error)
                self.assertIn('업데이트 취소', output)
                if label != 'not a tty':
                    self.assertIn('[y/N]', output)
                    self.assertLess(output.index('Hub(pid'), output.index('[y/N]'))
                self.assertEqual(self.kinds('shutdown', 'install', 'mkdir', 'write'), [])
                self.assertIsNone(hub.poll())
                self.assertEqual(flock_holder(self.hub_lock), hub.pid)
                self.assertEqual(self.hub_record.read_bytes(), record_before)
                self.assertIsNone(backend.poll())
                self.assertFalse(self.applied.exists())
                self.assertFalse(self.updates().exists())
                self.assertFalse((data / 'update-resume.json').exists())
                self.assertEqual(tree_hashes(project, data, self.state), files)
        # the Hub is still a working Hub: it answers a status request
        run = subprocess.run([PYTHON, '-m', 'workbench', 'hub', 'status'], env=self.hub_env(), cwd=self.home,
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn(str(hub.pid), run.stdout)
        self.assertIsNone(hub.poll())

    def test_without_a_hub_no_hub_line_is_in_the_confirmation(self):
        project, data, backend = self.backend('project-a', TASK)
        status, output, error = self.invoke(stdin=Tty('n\n'))
        self.assertEqual(status, 1, output + error)
        self.assertIn('task-a', output)
        self.assertNotIn('Hub', output + error)
        self.assertFalse(self.state.exists())


class UnresponsiveHubTests(Fixture):
    def test_a_stopped_hub_aborts_idle_update_and_gets_only_its_own_sigterm(self):
        hub = self.start_hub()
        project, data, backend = self.backend('project-a')
        bystander, log = self.fake('bystander')
        os.kill(hub.pid, signal.SIGSTOP)
        self.assertTrue(wait_until(lambda: proc_state(hub.pid) == b'T', 5))
        files = tree_hashes(project, data)
        with self.watched_filesystem():
            status, output, error = self.invoke()
        self.nothing_changed(status, output, error, backend)
        self.assertIn('wb hub stop', error)
        self.assertIn(str(hub.pid), error)
        self.assertEqual(flock_holder(self.hub_lock), hub.pid)
        self.assertEqual(log.read_text(), '')  # nobody else was signalled
        self.assertIsNone(bystander.poll())
        self.assertEqual(tree_hashes(project, data), files)
        os.kill(hub.pid, signal.SIGCONT)
        self.assertEqual(hub.wait(timeout=10), 0)  # the one SIGTERM was queued for exactly this Hub

    def test_a_stopped_hub_aborts_even_after_the_user_approved_active_work(self):
        hub = self.start_hub()
        project, data, backend = self.backend('project-a', TASK)
        os.kill(hub.pid, signal.SIGSTOP)
        self.assertTrue(wait_until(lambda: proc_state(hub.pid) == b'T', 5))
        files = tree_hashes(project, data)
        with self.watched_filesystem():
            status, output, error = self.invoke(stdin=Tty('y\n'))
        self.nothing_changed(status, output, error, backend)
        self.assertIn('wb hub stop', error)
        self.assertEqual(tree_hashes(project, data), files)
        os.kill(hub.pid, signal.SIGCONT)
        self.assertEqual(hub.wait(timeout=10), 0)

    def test_a_hub_that_ignores_sigterm_is_signalled_once_never_killed_and_nothing_changes(self):
        fake, log = self.fake('stuck-hub', lock=True, serve=True, record=True)
        bystander, bystander_log = self.fake('bystander')
        project, data, backend = self.backend('project-a')
        files = tree_hashes(project, data)
        with self.watched_filesystem():
            status, output, error = self.invoke()
        self.nothing_changed(status, output, error, backend)
        self.assertIn(str(fake.pid), error)
        self.assertIn('wb hub stop', error)
        self.assertEqual(log.read_text().split(), ['SIGTERM'])  # exactly one, and no other signal
        self.assertIsNone(fake.poll())  # no SIGKILL
        self.assertEqual(flock_holder(self.hub_lock), fake.pid)
        self.assertEqual(bystander_log.read_text(), '')
        self.assertEqual(tree_hashes(project, data), files)


class UnknownHubTests(Fixture):
    def assert_aborts_without_touching_anything(self, *peers):
        project, data, backend = self.backend('project-a', TASK)
        files = tree_hashes(project, data)

        def never_asked(prompt=''):
            raise AssertionError('the work question was asked although the Hub state is unknown')

        with patch('builtins.input', never_asked), self.watched_filesystem():
            status, output, error = self.invoke(stdin=Tty('y\n'))
        self.nothing_changed(status, output, error, backend)
        self.assertNotIn('[y/N]', output + error)
        self.assertIn('wb hub stop', error)
        self.assertEqual(self.kinds('request'), [])  # aborted before any backend was contacted
        self.assertEqual(tree_hashes(project, data), files)
        for process, log in peers:
            self.assertIsNone(process.poll())
            self.assertEqual(log.read_text(), '', 'a process that is not the Hub was signalled')

    def test_lock_held_without_a_record(self):
        holder, log = self.fake('holder', lock=True)
        self.assertFalse(self.hub_record.exists())
        self.assert_aborts_without_touching_anything((holder, log))
        self.assertEqual(flock_holder(self.hub_lock), holder.pid)

    def test_lock_held_with_a_corrupt_record(self):
        holder, log = self.fake('holder', lock=True)
        descriptor = os.open(self.hub_record, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(descriptor, b'{not json')
        os.close(descriptor)
        self.assert_aborts_without_touching_anything((holder, log))

    def test_record_names_a_live_bystander_while_another_process_holds_the_lock(self):
        holder, holder_log = self.fake('holder', lock=True)
        bystander, bystander_log = self.fake('bystander')
        self.write_record(bystander.pid, ticks_of(bystander.pid))
        self.assert_aborts_without_touching_anything((holder, holder_log), (bystander, bystander_log))

    def test_record_names_the_lock_holder_with_the_wrong_start_ticks(self):
        holder, log = self.fake('holder', lock=True)
        self.write_record(holder.pid, ticks_of(holder.pid) + 1)
        self.assert_aborts_without_touching_anything((holder, log))

    def test_record_names_a_serving_peer_with_a_different_socket_owner(self):
        # Lock holder and record agree on the pid, but the socket is another process's.
        holder, holder_log = self.fake('holder', lock=True)
        server, server_log = self.fake('server', serve=True)
        self.write_record(holder.pid, ticks_of(holder.pid), socket=str(self.home / 'server-sock' / 'hub.sock'))
        self.assert_aborts_without_touching_anything((holder, holder_log), (server, server_log))


class StaleRecordTests(Fixture):
    def test_a_stale_record_naming_a_live_process_is_not_a_hub_and_that_process_is_not_signalled(self):
        bystander, log = self.fake('bystander')
        self.write_record(bystander.pid, ticks_of(bystander.pid))  # no lock holder: stale
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.hub_lock.touch(mode=0o600)
        project, data, backend = self.backend('project-a')
        status, output, error = self.invoke()
        self.assertEqual(status, 0, output + error)
        self.assertNotIn('Hub(', output)
        self.assertEqual(self.kinds('shutdown', 'install'), ['shutdown', 'install'])
        self.assertIsNone(bystander.poll())
        self.assertEqual(log.read_text(), '')


class StopRaceTests(Fixture):
    def race(self, how: str):
        from workbench.contracts.ui_v1 import ClientType
        hub = self.start_hub()

        def vanish():
            # Runs after the update's Hub check and before its stop (backend question phase).
            os.kill(hub.pid, signal.SIGTERM if how == 'term' else signal.SIGKILL)
            hub.wait(timeout=10)

        project, data, backend = self.backend('project-a', on_request={ClientType.SHUTDOWN_REQUEST: vanish})
        status, output, error = self.invoke()
        return hub, status, output, error, backend

    def assert_update_proceeds(self, hub, status, output, error, backend):
        self.assertEqual(status, 0, output + error)
        self.assertIn('업데이트 완료', output)
        self.assertEqual(self.kinds('shutdown', 'install'), ['shutdown', 'install'])
        for kind, _name, view in self.events:
            if kind in ('shutdown', 'install'):
                self.assertEqual(view, GONE)
        self.assertIsNotNone(backend.poll())
        self.assertIsNone(flock_holder(self.hub_lock))
        self.assertEqual(sandbox_hubs(self.home), set())

    def test_hub_ending_on_its_own_after_the_check_does_not_abort_the_update(self):
        hub, status, output, error, backend = self.race('term')
        self.assert_update_proceeds(hub, status, output, error, backend)
        self.assertEqual(hub.returncode, 0)

    def test_hub_killed_after_the_check_leaves_a_stale_record_and_the_update_proceeds(self):
        hub, status, output, error, backend = self.race('kill')
        self.assert_update_proceeds(hub, status, output, error, backend)
        self.assertEqual(hub.returncode, -signal.SIGKILL)
        self.assertNotIn('중단', error)


class AutoStartDuringUpdateTests(Fixture):
    def attempt(self, results: list, when: str) -> None:
        env = dict(self.hub_env(), WB_TEST_FENCE_BASE=str(self.home / 'fence'))
        env.pop('WORKBENCH_HUB_AUTOSTART')
        ensured = subprocess.run([PYTHON, '-c', ENSURE_DRIVER], env=env, cwd=self.home, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=30)
        started = subprocess.run([PYTHON, '-c', HUB_START_DRIVER], env=env, cwd=self.home, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=30)
        results.append((when, ensured.stdout.strip(), started.returncode != 0,
                        '업데이트가 진행 중' in started.stdout + started.stderr,
                        flock_holder(self.hub_lock), sandbox_hubs(self.home)))

    def test_no_hub_can_be_started_while_the_update_runs(self):
        from workbench.contracts.ui_v1 import ClientType
        hub = self.start_hub()
        results: list = []
        self.backend('project-a', on_request={ClientType.SHUTDOWN_CONFIRM: lambda: self.attempt(results, 'shutdown')})

        def tool(argv, **kwargs):
            if list(map(str, argv))[0].endswith('uv') and '--offline' in list(map(str, argv)):
                self.attempt(results, 'install')
            return self.tool(argv, **kwargs)

        status, output, error = self.invoke(tool=tool)
        self.assertEqual(status, 0, output + error)
        self.assertEqual(results, [('shutdown', 'update_in_progress', True, True, None, set()),
                                   ('install', 'update_in_progress', True, True, None, set())])
        self.assertEqual(hub.wait(timeout=5), 0)


class AfterUpdateTests(Fixture):
    def test_no_hub_runs_afterwards_and_none_was_started(self):
        hub = self.start_hub()
        self.backend('project-a')
        status, output, error = self.invoke()
        self.assertEqual(status, 0, output + error)
        self.assertEqual(hub.wait(timeout=5), 0)
        self.assertIsNone(flock_holder(self.hub_lock))
        self.assertFalse(self.hub_record.exists())
        self.assertEqual(sandbox_hubs(self.home), set())
        self.assertNotIn('Hub를 시작', output + error)
        self.assertEqual(len(self.hub_pids), 1)

    def test_a_failed_installation_does_not_bring_the_hub_back(self):
        hub = self.start_hub()
        self.backend('project-a')
        self.install_fails = True
        status, output, error = self.invoke()
        self.assertEqual(status, 1, output + error)
        self.assertIn('업데이트 중단', error)
        self.assertEqual(hub.wait(timeout=5), 0)
        self.assertIsNone(flock_holder(self.hub_lock))
        self.assertEqual(sandbox_hubs(self.home), set())

    def test_a_hubless_update_starts_no_hub_and_creates_no_hub_state(self):
        self.backend('project-a')
        status, output, error = self.invoke()
        self.assertEqual(status, 0, output + error)
        self.assertNotIn('Hub', output + error)
        self.assertFalse(self.state.exists())
        self.assertEqual(sandbox_hubs(self.home), set())


class RefusalBeforeHubTests(Fixture):
    def test_a_managed_caller_refusal_leaves_the_running_hub_alone(self):
        hub = self.start_hub()
        project, data, backend = self.backend('managed-caller')
        record = json.loads((data / 'backend.json').read_text())
        record['processes']['host_shell'] = {'pid': os.getpid(), 'start_ticks': ticks_of(os.getpid())}
        (data / 'backend.json').write_text(json.dumps(record))
        status, output, error = self.invoke()
        self.assertEqual(status, 1, output + error)
        self.assertIn('외부 terminal', error)
        self.assertNotIn('Hub(', output + error)
        self.assertIsNone(hub.poll())
        self.assertEqual(flock_holder(self.hub_lock), hub.pid)
        self.assertIn(proc_state(hub.pid), (b'S', b'R'))
        self.assertEqual(self.kinds('shutdown', 'install'), [])
        self.assertIsNone(backend.poll())


if __name__ == '__main__':
    unittest.main()
