"""HB-09: ``wb update`` stops the Hub before any backend (seam S1, HB-AC-18, HB-T06).

The update fixture of ``test_update_cli.InstalledFixture`` (installation, git/uv tool and
backend doubles, relocated update fence) runs ``cli.main(['update'])`` in process. The Hub
is a real ``wb _hub --foreground`` subprocess in the same temp HOME/XDG sandbox. Expected
values come from the kernel (``/proc/locks`` holder, ``/proc/<pid>/status``), the order
the doubles record and file existence, never from the product's identity helpers.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import test_update_cli as base

REPO = Path(__file__).resolve().parents[3]
PYTHON = sys.executable  # invoke() patches sys.executable to the fixture install


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


def alive(pid: int) -> bool:
    try:
        state = Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in (b'Z', b'X')


def sigterm_pending(pid: int) -> bool:
    for row in Path(f'/proc/{pid}/status').read_text().splitlines():
        if row.startswith('ShdPnd:'):
            return bool(int(row.split()[1], 16) & (1 << (signal.SIGTERM - 1)))
    return False


def wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class Tty(io.StringIO):
    def isatty(self): return True


class UpdateHubFixture(unittest.TestCase):
    # InstalledFixture's helpers without inheriting (and re-running) its tests.
    setUp_installed = base.InstalledFixture.setUp
    write_install = base.InstalledFixture.write_install
    invoke = base.InstalledFixture.invoke
    project = base.InstalledFixture.project

    def setUp(self):
        self.setUp_installed()
        # A short private runtime base: backend runtimes must never fall back to
        # /tmp/omp-workbench-<uid> because the socket path got too long.
        self.runtime = Path(tempfile.mkdtemp(prefix='r'))
        self.runtime.chmod(0o700)
        self.addCleanup(shutil.rmtree, self.runtime, ignore_errors=True)
        self.env['XDG_RUNTIME_DIR'] = str(self.runtime)
        self.use_state_home(self.home / 'state')
        self.events: list[tuple[str, dict]] = []
        self.owned: list[subprocess.Popen] = []
        self.hub_pids: list[int] = []
        self.addCleanup(self.reap)

    def use_state_home(self, state_home: Path) -> None:
        self.env['XDG_STATE_HOME'] = str(state_home)
        self.state = state_home / 'omp-workbench' / 'hub'
        self.hub_lock = self.state / 'hub.lock'
        self.hub_record = self.state / 'hub.json'
        self.events: list[tuple[str, dict]] = []
        self.owned: list[subprocess.Popen] = []
        self.hub_pids: list[int] = []
        self.addCleanup(self.reap)

    # --- the Hub and plain lock holders this test owns ---------------------------------

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

    def holder(self, path: Path) -> subprocess.Popen:
        process = self.popen([sys.executable, '-c',
                              'import fcntl,os,sys,time\n'
                              'fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600)\n'
                              'fcntl.flock(fd,fcntl.LOCK_EX)\n'
                              'print("held",flush=True)\n'
                              'time.sleep(120)\n', str(path)])
        self.assertEqual(process.stdout.readline().strip(), 'held')
        return process

    def reap(self) -> None:
        for process in self.owned:
            if process.poll() is None:
                try:
                    os.kill(process.pid, signal.SIGCONT)
                    process.terminate()
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                except ProcessLookupError:
                    pass
            if process.stdout is not None:
                process.stdout.close()

    # --- doubles that record what the kernel shows at each step ------------------------

    def hub_view(self) -> dict:
        return {'lock_holder': flock_holder(self.hub_lock),
                'hub_alive': [pid for pid in self.hub_pids if alive(pid)]}

    def recording_tool(self, argv, **kwargs):
        argv_text = list(map(str, argv))
        if argv_text[0].endswith('uv') and '--offline' in argv_text:
            self.events.append(('install', self.hub_view()))
        return self.tool(argv, **kwargs)

    def tool(self, argv, **kwargs):
        return base.InstalledFixture.tool(self, argv, **kwargs)

    def recorded_project(self, name: str, active: list):
        """``InstalledFixture.project`` whose shutdown confirm records the Hub view."""
        before = set(self.clients)
        project, data, backend = self.project(name, active)
        client, = (self.clients[key] for key in set(self.clients) - before)
        original = client.request

        def request(kind, **kwargs):
            from workbench.contracts.ui_v1 import ClientType
            if kind == ClientType.SHUTDOWN_CONFIRM:
                self.events.append(('backend_shutdown', self.hub_view()))
            return original(kind, **kwargs)

        client.request = request
        return project, data, backend

    def updates(self) -> Path:
        return self.home / 'state' / 'omp-workbench' / 'updates'


class NoHubTests(UpdateHubFixture):
    def test_without_a_hub_update_runs_the_existing_path_and_starts_no_hub(self):
        status, output, error = self.invoke(('update', '--ref', 'release-1'), tool=self.recording_tool)
        self.assertEqual(status, 0, output + error)
        self.assertIn('업데이트 완료', output)
        self.assertNotIn('Hub', output + error)
        self.assertEqual([name for name, _ in self.events], ['install'])
        self.assertEqual(self.events[0][1]['lock_holder'], None)
        self.assertFalse(self.state.exists())


class RunningHubTests(UpdateHubFixture):
    def test_a_running_hub_is_stopped_before_backend_shutdown_and_install(self):
        hub = self.start_hub()
        project, data, backend = self.recorded_project('project-a', [])
        status, output, error = self.invoke(tool=self.recording_tool)
        self.assertEqual(status, 0, output + error)
        self.assertIn(f'Hub(pid {hub.pid}', output)
        self.assertIn('를 멈춥니다', output)
        self.assertEqual([name for name, _ in self.events], ['backend_shutdown', 'install'])
        for _name, view in self.events:
            self.assertEqual(view, {'lock_holder': None, 'hub_alive': []})
        self.assertEqual(hub.wait(timeout=5), 0)  # a normal stop, not a kill
        self.assertIsNotNone(backend.poll())

    def test_the_hub_alone_adds_no_question_when_no_work_is_active(self):
        hub = self.start_hub()
        self.recorded_project('project-a', [])
        # A TTY with no answer: any question would read EOF and cancel the update.
        status, output, error = self.invoke(stdin=Tty(''), tool=self.recording_tool)
        self.assertEqual(status, 0, output + error)
        self.assertNotIn('[y/N]', output + error)
        self.assertIn(f'Hub(pid {hub.pid}', output)
        self.assertEqual([name for name, _ in self.events], ['backend_shutdown', 'install'])

    def test_active_work_question_lists_the_hub_and_a_decline_keeps_it_running(self):
        hub = self.start_hub()
        project, data, backend = self.recorded_project(
            'project-a', [{'kind': 'task_run', 'task_id': 'task-a', 'run_id': 'run-a'}])
        status, output, error = self.invoke(stdin=Tty('n\n'), tool=self.recording_tool)
        self.assertEqual(status, 1, output + error)
        question = output[:output.index('[y/N]')]
        self.assertIn('task-a', question)
        self.assertIn(f'Hub(pid {hub.pid}', question)
        self.assertEqual(self.events, [])
        self.assertIsNone(hub.poll())
        self.assertEqual(flock_holder(self.hub_lock), hub.pid)
        self.assertIsNone(backend.poll())
        self.assertFalse(self.updates().exists())


class HubStopFailureTests(UpdateHubFixture):
    def assert_nothing_changed(self, status, output, error, backend, data):
        self.assertEqual(status, 1, output + error)
        self.assertIn('wb hub stop', error)
        self.assertEqual(self.events, [])  # no backend shutdown, no install
        self.assertIsNone(backend.poll())
        self.assertFalse(self.applied.exists())
        self.assertFalse(self.updates().exists())
        self.assertFalse((data / 'update-resume.json').exists())

    def test_a_hub_that_neither_answers_nor_ends_on_sigterm_aborts_before_any_backend(self):
        hub = self.start_hub()
        project, data, backend = self.recorded_project('project-a', [])
        os.kill(hub.pid, signal.SIGSTOP)  # no answer, and a SIGTERM stays pending
        self.assertTrue(wait_until(lambda: Path(f'/proc/{hub.pid}/stat').read_bytes()
                                   .rsplit(b') ', 1)[1].split()[0] == b'T', 5))
        status, output, error = self.invoke(tool=self.recording_tool)
        self.assert_nothing_changed(status, output, error, backend, data)
        self.assertIn(str(hub.pid), error)
        self.assertTrue(sigterm_pending(hub.pid))  # SIGTERM went to exactly the recorded Hub
        self.assertEqual(flock_holder(self.hub_lock), hub.pid)
        os.kill(hub.pid, signal.SIGCONT)
        self.assertEqual(hub.wait(timeout=10), 0)

    def unknown_hub(self) -> subprocess.Popen:
        """``hub.lock`` held by a plain process with no ``hub.json``: Hub ``unknown``."""
        self.state.mkdir(mode=0o700, parents=True)
        holder = self.holder(self.hub_lock)
        self.assertFalse(self.hub_record.exists())
        return holder

    def test_an_unknown_hub_aborts_before_any_backend_and_is_not_signalled(self):
        holder = self.unknown_hub()
        project, data, backend = self.recorded_project('project-a', [])
        status, output, error = self.invoke(tool=self.recording_tool)
        self.assert_nothing_changed(status, output, error, backend, data)
        self.assertIsNone(holder.poll())
        self.assertFalse(sigterm_pending(holder.pid))
        self.assertEqual(flock_holder(self.hub_lock), holder.pid)

    def test_an_unknown_hub_aborts_before_asking_about_active_work(self):
        holder = self.unknown_hub()
        project, data, backend = self.recorded_project(
            'project-a', [{'kind': 'task_run', 'task_id': 'task-a', 'run_id': 'run-a'}])
        status, output, error = self.invoke(stdin=Tty('y\n'), tool=self.recording_tool)
        self.assert_nothing_changed(status, output, error, backend, data)
        self.assertNotIn('[y/N]', output + error)
        self.assertIsNone(holder.poll())

    def test_a_backup_root_inside_the_project_source_aborts_with_the_hub_untouched(self):
        project, data, backend = self.recorded_project('project-a', [])
        # XDG_STATE_HOME (updates/ and the Hub state) inside the project source.
        self.use_state_home(project / 'xdg-state')
        hub = self.start_hub()
        status, output, error = self.invoke(tool=self.recording_tool)
        self.assertEqual(status, 1, output + error)
        self.assertIn('backup root가 프로젝트 source와 겹칩니다', error)
        self.assertEqual(self.events, [])
        self.assertIsNone(hub.poll())
        self.assertEqual(flock_holder(self.hub_lock), hub.pid)
        self.assertIsNone(backend.poll())
        self.assertFalse(self.applied.exists())
        self.assertFalse((project / 'xdg-state' / 'omp-workbench' / 'updates').exists())


HUB_STOPPED_TEXT = 'Hub는 멈춘 상태입니다. 다음 `wb` 실행 때 다시 시작됩니다.'


class FailureAfterHubStopTests(UpdateHubFixture):
    def test_an_install_failure_after_the_hub_stopped_says_the_hub_stays_stopped(self):
        import subprocess as sp
        hub = self.start_hub()
        self.recorded_project('project-a', [])

        def failing(argv, **kwargs):
            argv_text = list(map(str, argv))
            if argv_text[0].endswith('uv') and '--offline' in argv_text:
                self.events.append(('install', self.hub_view()))
                return sp.CompletedProcess(argv_text, 2, '', '')
            return self.tool(argv, **kwargs)

        status, output, error = self.invoke(tool=failing)
        self.assertEqual(status, 1, output + error)
        self.assertIn('업데이트 중단', error)
        self.assertIn(HUB_STOPPED_TEXT, error)
        self.assertEqual(hub.wait(timeout=5), 0)

    def test_a_backend_shutdown_failure_after_the_hub_stopped_says_the_hub_stays_stopped(self):
        hub = self.start_hub()
        before = set(self.clients)
        self.project('project-a', [])
        client, = (self.clients[key] for key in set(self.clients) - before)
        original = client.request

        def request(kind, **kwargs):
            from workbench.contracts.ui_v1 import ClientType
            if kind == ClientType.SHUTDOWN_CONFIRM:
                return {'ok': False}
            return original(kind, **kwargs)

        client.request = request
        status, output, error = self.invoke(tool=self.recording_tool)
        self.assertEqual(status, 1, output + error)
        self.assertIn(HUB_STOPPED_TEXT, error)
        self.assertFalse(self.applied.exists())
        self.assertEqual(hub.wait(timeout=5), 0)

    def test_a_failure_with_no_hub_running_does_not_mention_the_hub(self):
        import subprocess as sp
        self.recorded_project('project-a', [])

        def failing(argv, **kwargs):
            argv_text = list(map(str, argv))
            if argv_text[0].endswith('uv') and '--offline' in argv_text:
                return sp.CompletedProcess(argv_text, 2, '', '')
            return self.tool(argv, **kwargs)

        status, output, error = self.invoke(tool=failing)
        self.assertEqual(status, 1, output + error)
        self.assertNotIn('Hub', output + error)


# Another ``wb``'s auto-start (``ensure_hub``) with only the update fence relocated.
ENSURE_DRIVER = ("import os, pathlib\n"
                 "from workbench.backend import maintenance\n"
                 "maintenance.FENCE_BASE = pathlib.Path(os.environ.pop('WB_TEST_FENCE_BASE'))\n"
                 "from workbench.hub.process import ensure_hub\n"
                 "print(ensure_hub(timeout=2).reason)\n")


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


class AfterUpdateTests(UpdateHubFixture):
    def test_update_leaves_no_hub_and_auto_start_during_the_fence_starts_none(self):
        hub = self.start_hub()
        self.recorded_project('project-a', [])
        attempts = []

        def tool(argv, **kwargs):
            argv_text = list(map(str, argv))
            if argv_text[0].endswith('uv') and '--offline' in argv_text:
                env = dict(self.hub_env(), WB_TEST_FENCE_BASE=str(self.home / 'fence'))
                env.pop('WORKBENCH_HUB_AUTOSTART')
                run = subprocess.run([PYTHON, '-c', ENSURE_DRIVER], env=env, cwd=self.home,
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                attempts.append((run.stdout.strip(), flock_holder(self.hub_lock), sandbox_hubs(self.home)))
            return self.recording_tool(argv, **kwargs)

        status, output, error = self.invoke(tool=tool)
        self.assertEqual(status, 0, output + error)
        self.assertEqual(attempts, [('update_in_progress', None, set())])
        self.assertEqual(hub.wait(timeout=5), 0)
        self.assertIsNone(flock_holder(self.hub_lock))
        self.assertFalse(self.hub_record.exists())
        self.assertEqual(sandbox_hubs(self.home), set())


class MaintenanceScanTests(UpdateHubFixture):
    """HB-T06: a running Hub is neither a backend for ``discover()`` nor a writer."""

    def test_discover_and_require_no_writers_ignore_a_running_hub(self):
        import argparse
        from unittest.mock import patch
        from workbench.backend import maintenance
        hub = self.start_hub()
        project, data, backend = self.project('project-a', [])
        # The scanned /proc holds exactly the real Hub (its cmdline, uid and fds).
        (self.proc / str(hub.pid)).symlink_to(f'/proc/{hub.pid}')
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(maintenance, 'FALLBACK_RUNTIME', self.home / 'fallback'), \
                patch.object(maintenance, 'PROC', self.proc):
            identities = maintenance.discover(argparse.Namespace(data_dir=None))
            maintenance.require_no_writers(data)
        self.assertEqual([identity.layout.root for identity in identities], [data])
        # Only the project's runtime binding lives under omp-workbench/; the Hub socket does not.
        self.assertEqual(len(list((self.runtime / 'omp-workbench').iterdir())), 1)
        socket = Path(json.loads(self.hub_record.read_text())['socket'])
        self.assertFalse(socket.is_relative_to(self.runtime / 'omp-workbench'))
        self.assertEqual(flock_holder(self.hub_lock), hub.pid)


class BoardSetUpdateTests(UpdateHubFixture):
    """TI-T07: a running Board agent set joins the one update question (``stop_request.active``)."""

    PASSWORD = 'correct horse battery staple'

    def start_hub_with_board_shell(self) -> tuple[subprocess.Popen, int]:
        """A real Hub whose Board shell channel runs (started through ``POST /board/restart``)."""
        import http.client
        import re
        from urllib.parse import urlencode
        shell = self.home / 'board-shell'
        shell.write_text('#!/bin/sh\nexec /bin/sleep 600\n')
        shell.chmod(0o755)
        config = self.home / '.config' / 'omp-workbench'
        config.mkdir(parents=True, mode=0o700, exist_ok=True)
        (config / 'hub.toml').write_text(f'[web]\nbind = "127.0.0.1"\nport = 0\n[board_agent]\nshell = "{shell}"\n')
        passwd = subprocess.run([sys.executable, '-m', 'workbench', 'hub', 'passwd', '--stdin'], env=self.hub_env(),
                                input=self.PASSWORD + '\n', capture_output=True, text=True, timeout=40)
        self.assertEqual(passwd.returncode, 0, passwd)
        hub = self.start_hub()

        def web_status() -> dict:
            result = subprocess.run([sys.executable, '-m', 'workbench', 'hub', 'status', '--json'],
                                    env=self.hub_env(), capture_output=True, text=True, timeout=40)
            return json.loads(result.stdout)

        self.assertTrue(wait_until(lambda: web_status()['web'].get('state') == 'listening', 15))
        host, port = '127.0.0.1', web_status()['web']['port']
        cookies: dict[str, str] = {}

        def request(method, path, data=None):
            headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in cookies.items())} if cookies else {}
            body = None
            if data is not None:
                body = urlencode(data)
                headers.update({'Content-Type': 'application/x-www-form-urlencoded',
                                'Origin': f'http://{host}:{port}'})
            connection = http.client.HTTPConnection(host, port, timeout=15)
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                text = response.read().decode()
                for header in response.headers.get_all('Set-Cookie') or []:
                    name, _, rest = header.partition('=')
                    value = rest.split(';')[0]
                    if value:
                        cookies[name] = value
                return response.status, text
            finally:
                connection.close()

        _, page = request('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        self.assertEqual(request('POST', '/login', {'login_token': token, 'password': self.PASSWORD})[0], 303)
        _, page = request('GET', '/settings')
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        self.assertEqual(request('POST', '/board/restart', {'csrf': csrf, 'channel': 'shell'})[0], 303)
        board = web_status()['board_agent']
        self.assertEqual(board['shell']['state'], 'running')
        return hub, board['shell']['pid']

    def test_the_question_lists_the_set_and_a_decline_keeps_hub_and_set(self):
        hub, shell = self.start_hub_with_board_shell()
        project, data, backend = self.recorded_project('project-a', [])
        status, output, error = self.invoke(stdin=Tty('n\n'), tool=self.recording_tool)
        self.assertEqual(status, 1, output + error)
        question = output[:output.index('[y/N]')]
        self.assertIn('Board agent 세트', question)
        self.assertIn(f'shell running(pid {shell})', question)
        self.assertIn(f'Hub(pid {hub.pid}', question)
        self.assertEqual(self.events, [])
        self.assertIsNone(hub.poll())
        self.assertTrue(alive(shell))
        self.assertIsNone(backend.poll())

    def test_without_a_terminal_the_update_is_cancelled_and_nothing_stops(self):
        hub, shell = self.start_hub_with_board_shell()
        project, data, backend = self.recorded_project('project-a', [])
        status, output, error = self.invoke(tool=self.recording_tool)
        self.assertEqual(status, 1, output + error)
        self.assertIn('업데이트 취소', output)
        self.assertEqual(self.events, [])
        self.assertIsNone(hub.poll())
        self.assertTrue(alive(shell))

    def test_a_confirmed_question_stops_the_hub_with_the_set_before_backends_and_install(self):
        hub, shell = self.start_hub_with_board_shell()
        project, data, backend = self.recorded_project('project-a', [])
        status, output, error = self.invoke(stdin=Tty('y\n'), tool=self.recording_tool)
        self.assertEqual(status, 0, output + error)
        self.assertEqual(hub.wait(timeout=20), 0)
        self.assertFalse(alive(shell))
        self.assertEqual([name for name, _ in self.events], ['backend_shutdown', 'install'])
        for _name, view in self.events:
            self.assertEqual(view, {'lock_holder': None, 'hub_alive': []})


if __name__ == '__main__':
    unittest.main()
