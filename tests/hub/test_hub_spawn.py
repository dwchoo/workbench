"""Hub spawn, auto-start rules and single instance (HB-02, seam S1).

Every ``wb`` here is a real CLI subprocess in a temp HOME/XDG sandbox. The user
update fence (normally ``/tmp/omp-workbench-maintenance-<uid>``) is redirected to a
temp directory by a tiny driver that sets ``maintenance.FENCE_BASE`` and then runs the
unchanged ``workbench.backend.cli.main()``, so no test touches the real user fence.
Expected values come from the kernel (``/proc/locks`` holder, ``/proc/<pid>/stat``
session/ppid, ``/proc/<pid>/environ``/``cwd``/``fd``) and the BRIEF HB-D11 lists.
Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import unittest

from test_hub_process import HubSandbox, alive, flock_holder, wait_until

DRIVER = ("import os, pathlib, sys\n"
          "import workbench.backend.maintenance as maintenance\n"
          "maintenance.FENCE_BASE = pathlib.Path(os.environ.pop('WB_TEST_FENCE_BASE'))\n"
          "from workbench.backend.cli import main\n"
          "raise SystemExit(main())\n")


def proc_stat(pid: int) -> list[bytes]:
    return Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()


def session_of(pid: int) -> int:
    return int(proc_stat(pid)[3])


def parent_of(pid: int) -> int:
    return int(proc_stat(pid)[1])


def proc_environ(pid: int) -> dict[str, str]:
    raw = Path(f'/proc/{pid}/environ').read_bytes()
    return dict(item.decode(errors='replace').split('=', 1) for item in raw.split(b'\0') if b'=' in item)


class SpawnSandbox(HubSandbox):
    """HubSandbox with auto-start enabled and a private update fence."""

    def __init__(self, case: unittest.TestCase):
        super().__init__(case)
        self.fence_base = self.base / 'fence'
        self.env.pop('WORKBENCH_HUB_AUTOSTART')
        self.env['WB_TEST_FENCE_BASE'] = str(self.fence_base)
        case.addCleanup(self.stop_hubs)  # runs before the temp dir is removed

    def wb_argv(self, *args: str) -> list[str]:
        return [sys.executable, '-c', DRIVER, *args]

    def wb(self, *args: str, timeout: float = 40, env: dict | None = None, cwd: Path | None = None
           ) -> subprocess.CompletedProcess:
        return subprocess.run(self.wb_argv(*args), env=self.env if env is None else env,
                              cwd=self.base if cwd is None else cwd, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)

    def hub_pids(self) -> list[int]:
        """Live ``_hub`` processes whose environment names this sandbox's state home."""
        found = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                argv = (entry / 'cmdline').read_bytes().split(b'\0')
                if b'_hub' not in argv or not alive(pid):
                    continue
                if proc_environ(pid).get('XDG_STATE_HOME') == str(self.state_home):
                    found.append(pid)
            except OSError:
                continue
        return sorted(found)

    def stop_hubs(self) -> None:
        """SIGTERM (then SIGKILL) every Hub this sandbox started; then require none left."""
        for pid in self.hub_pids():
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if not wait_until(lambda: not self.hub_pids(), 10):
            for pid in self.hub_pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            wait_until(lambda: not self.hub_pids(), 5)
        self.case.assertEqual(self.hub_pids(), [], 'a Hub outlived its test')


def reported_pid(result: subprocess.CompletedProcess) -> int:
    match = re.search(r'pid (\d+)', result.stdout)
    if match is None:
        raise AssertionError(f'no pid in output: rc={result.returncode} out={result.stdout!r} err={result.stderr!r}')
    return int(match.group(1))


class HubStartCommandTests(unittest.TestCase):
    def setUp(self):
        self.box = SpawnSandbox(self)

    def test_hub_start_spawns_a_detached_hub_that_holds_the_lock(self):
        result = self.box.wb('hub', 'start')
        self.assertEqual(result.returncode, 0, result.stderr)
        pid = reported_pid(result)
        self.assertEqual(flock_holder(self.box.lock), pid)
        self.assertEqual(self.box.hub_pids(), [pid])
        self.assertNotEqual(session_of(pid), os.getsid(0))
        self.assertEqual(self.box.record()['mode'], 'spawned')

    def test_hub_gets_a_clean_environment_cwd_and_fds(self):
        env = dict(self.box.env, WORKBENCH_DATA_DIR=str(self.box.base / 'legacy'), WORKBENCH_G3_PROBE='1',
                   WORKBENCH_G3_OTHER='x', WORKBENCH_AGENT_ROLES='manager', WORKBENCH_OMP_RUNTIME_ROOT='/x',
                   HB02_KEEP='kept')
        leaked_read, leaked_write = os.pipe()
        try:
            caller = subprocess.Popen(self.box.wb_argv('hub', 'start'), env=env, cwd=self.box.base,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, pass_fds=(leaked_write,))
            out, err = caller.communicate(timeout=40)
        finally:
            os.close(leaked_write)
        leaked = os.readlink(f'/proc/self/fd/{leaked_read}')
        os.close(leaked_read)
        self.assertEqual(caller.returncode, 0, err)
        pid = int(re.search(r'pid (\d+)', out).group(1))
        environ = proc_environ(pid)
        for name in ('WORKBENCH_DATA_DIR', 'WORKBENCH_G3_PROBE', 'WORKBENCH_G3_OTHER', 'WORKBENCH_AGENT_ROLES',
                     'WORKBENCH_OMP_RUNTIME_ROOT'):
            self.assertNotIn(name, environ)
        self.assertEqual(environ.get('HB02_KEEP'), 'kept')
        self.assertEqual(environ.get('HOME'), str(self.box.home))
        self.assertEqual(os.readlink(f'/proc/{pid}/cwd'), '/')
        fds = {int(name): os.readlink(f'/proc/{pid}/fd/{name}') for name in os.listdir(f'/proc/{pid}/fd')}
        self.assertEqual(fds[0], '/dev/null')
        log = str(self.box.state / 'hub.log')
        self.assertEqual((fds[1], fds[2]), (log, log))
        self.assertNotIn(leaked, fds.values())
        self.assertEqual([target for target in fds.values() if target.startswith('pipe:')], [], fds)
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)
        self.assertNotEqual(session_of(pid), os.getsid(0))
        self.assertNotEqual(session_of(pid), caller.pid)

    def test_hub_start_works_when_the_caller_already_uses_more_than_1024_fds(self):
        crowded = ("import os, resource\n"
                   "soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)\n"
                   "resource.setrlimit(resource.RLIMIT_NOFILE, (min(max(soft, 4096), hard), hard))\n"
                   "keep = [os.open(os.devnull, os.O_RDONLY) for _ in range(1100)]\n" + DRIVER)
        soft, hard = __import__('resource').getrlimit(__import__('resource').RLIMIT_NOFILE)
        if hard != -1 and hard < 1200:
            self.skipTest(f'RLIMIT_NOFILE hard limit {hard} cannot hold 1100 extra fds')
        result = subprocess.run([sys.executable, '-c', crowded, 'hub', 'start'], env=self.box.env,
                                cwd=self.box.base, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.box.hub_pids(), [reported_pid(result)])

    def seed_log(self, size: int) -> bytes:
        self.box.state.mkdir(mode=0o700, parents=True)
        log = self.box.state / 'hub.log'
        data = (b'old hub log line\n' * (size // 17 + 1))[:size]
        log.write_bytes(data)
        log.chmod(0o600)
        return data

    def test_a_log_over_1_mib_is_rotated_once_at_start(self):
        old = self.seed_log(1024 * 1024 + 1)
        (self.box.state / 'hub.log.1').write_bytes(b'older generation')
        pid = reported_pid(self.box.wb('hub', 'start'))
        log, rotated = self.box.state / 'hub.log', self.box.state / 'hub.log.1'
        self.assertEqual(rotated.read_bytes(), old)
        self.assertLess(log.stat().st_size, 1024 * 1024)
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(rotated.stat().st_mode), 0o600)
        for fd in (1, 2):
            self.assertEqual(os.readlink(f'/proc/{pid}/fd/{fd}'), str(log))

    def test_the_rotated_log_is_made_private(self):
        old = self.seed_log(1024 * 1024 + 1)
        (self.box.state / 'hub.log').chmod(0o644)  # e.g. a log left by an older install
        pid = reported_pid(self.box.wb('hub', 'start'))
        rotated = self.box.state / 'hub.log.1'
        self.assertEqual(rotated.read_bytes(), old)
        self.assertEqual(stat.S_IMODE(rotated.stat().st_mode), 0o600)
        self.assertEqual(os.readlink(f'/proc/{pid}/fd/1'), str(self.box.state / 'hub.log'))
        self.assertEqual(sorted(path.name for path in self.box.state.iterdir() if path.name.startswith('hub.log')),
                         ['hub.log', 'hub.log.1'])

    def test_a_log_at_or_under_1_mib_is_kept(self):
        old = self.seed_log(1024 * 1024)
        pid = reported_pid(self.box.wb('hub', 'start'))
        log = self.box.state / 'hub.log'
        self.assertFalse((self.box.state / 'hub.log.1').exists())
        self.assertEqual(log.read_bytes()[:len(old)], old)
        self.assertEqual(os.readlink(f'/proc/{pid}/fd/1'), str(log))

    def test_hub_start_reuses_a_running_hub(self):
        first = reported_pid(self.box.wb('hub', 'start'))
        start_ticks = proc_stat(first)[19]
        again = self.box.wb('hub', 'start')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(reported_pid(again), first)
        self.assertIn('Hub 실행 중', again.stdout)
        self.assertEqual(self.box.hub_pids(), [first])
        self.assertEqual(proc_stat(first)[19], start_ticks)

    def test_eight_concurrent_hub_starts_leave_one_hub_with_one_pid(self):
        callers = [subprocess.Popen(self.box.wb_argv('hub', 'start'), env=self.box.env, cwd=self.box.base,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True) for _ in range(8)]
        results = []
        for caller in callers:
            out, err = caller.communicate(timeout=40)
            results.append(subprocess.CompletedProcess(caller.args, caller.returncode, out, err))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        pids = {reported_pid(result) for result in results}
        self.assertEqual(len(pids), 1, results)
        (pid,) = pids
        self.assertEqual(flock_holder(self.box.lock), pid)
        # Losers exit right after ``L``; only the lock holder may remain.
        self.assertTrue(wait_until(lambda: self.box.hub_pids() == [pid], 5), self.box.hub_pids())

    def test_hub_survives_closing_the_pty_it_was_started_from(self):
        command = shlex.join(self.box.wb_argv('hub', 'start')) + '; echo HUB-START-DONE; exec sleep 60'
        child, master = pty.fork()
        if child == 0:  # pragma: no cover - exec'd immediately
            try:
                os.chdir(self.box.base)
                signal.signal(signal.SIGHUP, signal.SIG_DFL)  # a nohup'd runner must not make the shell immune
                os.execve('/bin/sh', ['/bin/sh', '-c', command], self.box.env)
            finally:
                os._exit(127)
        output = b''
        deadline = time.monotonic() + 40
        try:
            while b'HUB-START-DONE' not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.5)[0]:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
        finally:
            os.close(master)  # hang up the terminal: SIGHUP to its session
        _, status = os.waitpid(child, 0)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGHUP, status)
        match = re.search(rb'pid (\d+)', output)
        self.assertIsNotNone(match, output)
        pid = int(match.group(1))
        self.assertNotEqual(session_of(pid), child)
        time.sleep(0.5)
        self.assertTrue(alive(pid))
        self.assertEqual(flock_holder(self.box.lock), pid)
        status = self.box.wb('hub', 'status')
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn(f'pid {pid}', status.stdout)


class AutoStartTests(unittest.TestCase):
    def setUp(self):
        self.box = SpawnSandbox(self)

    def test_wb_status_without_a_backend_starts_the_hub_and_still_answers(self):
        result = self.box.wb('status')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('no backend is running', result.stdout)
        holder = flock_holder(self.box.lock) if self.box.lock.exists() else None
        self.assertIsNotNone(holder)
        self.assertEqual(self.box.hub_pids(), [holder])
        self.assertEqual(self.box.record()['pid'], holder)

    def test_bare_wb_in_a_project_subfolder_starts_the_hub_before_the_start_path(self):
        from workbench.backend.project import initialize_project, resolve_project_context
        project = self.box.base / 'project'
        project.mkdir()
        initialize_project(resolve_project_context(project, self.box.env), confirm=lambda _: True,
                           interactive=True, environment=self.box.env)
        nested = project / 'a' / 'b'
        nested.mkdir(parents=True)
        result = self.box.wb(cwd=nested)  # no OMP in this sandbox: the start requirement fails
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('No backend was started', result.stderr)
        holder = flock_holder(self.box.lock) if self.box.lock.exists() else None
        self.assertIsNotNone(holder)
        self.assertEqual(self.box.hub_pids(), [holder])

    # BRIEF HB-D11 / HB-AC-15: commands after which no Hub may exist (args, expected exit).
    EXEMPT = (
        (('hub', 'stop'), 3),
        (('hub', 'status'), 3),
        (('setup', '--omp', '/nonexistent/omp-for-hub-test'), 1),
        (('update',), 1),  # refused: this is a development checkout, not a uv Git install
        (('doctor',), None),
        (('--help',), 0),
        (('--no-such-option',), 2),
        (('status', '--no-such-option'), 2),
    )

    def test_exempt_commands_leave_no_hub(self):
        for args, expected in self.EXEMPT:
            with self.subTest(args=args):
                result = self.box.wb(*args)
                if expected is not None:
                    self.assertEqual(result.returncode, expected, (result.stdout, result.stderr))
                self.assertFalse(self.box.lock.exists() and flock_holder(self.box.lock) is not None)
                self.assertEqual(self.box.hub_pids(), [])
                self.assertFalse(self.box.record_path.exists())

    # The diagnostic ``wb logs ...`` kinds are read-only like ``doctor``: no Hub, nothing under HOME's Hub state.
    LOGS = (
        ('logs', 'summary-compat', 'verify', '--scripted'),
        ('logs', 'summary-compat', 'status'),
        ('logs', 'important', 'status'),
        ('logs', 'raw-status'),
        ('logs', 'raw', '--run', '00000000-0000-4000-8000-000000000000'),
        ('logs', 'raw-rotation', 'activate', '--yes', '--evidence', '00000000-0000-4000-8000-000000000000'),
    )

    def test_logs_commands_start_no_hub_and_create_no_hub_state(self):
        default_state = self.box.home / '.local' / 'state' / 'omp-workbench' / 'hub'
        without_xdg = {key: value for key, value in self.box.env.items() if key != 'XDG_STATE_HOME'}
        for args in self.LOGS:
            for env in (self.box.env, without_xdg):
                with self.subTest(args=args, xdg_state='XDG_STATE_HOME' in env):
                    self.box.wb(*args, env=env)
                    self.assertEqual(self.box.hub_pids(), [])
                    self.assertFalse(self.box.state.exists(), 'Hub state dir created')
                    self.assertFalse(default_state.exists(), 'HOME Hub state dir created')

    def hold_update_fence(self) -> None:
        """Hold the exclusive update fence the way ``wb update`` does."""
        self.box.fence_base.mkdir(mode=0o700)
        self.box.holder(self.box.fence_base / 'update.lock')

    def test_a_held_update_fence_skips_auto_start_but_the_command_runs(self):
        self.hold_update_fence()
        result = self.box.wb('status')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('no backend is running', result.stdout)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
        self.assertEqual(self.box.hub_pids(), [])
        self.assertFalse(self.box.lock.exists() and flock_holder(self.box.lock) is not None)

    def test_an_unwritable_stderr_for_the_failure_line_does_not_break_the_command(self):
        self.hold_update_fence()  # auto-start fails and wants to print one stderr line
        read_end, write_end = os.pipe()
        os.close(read_end)  # writing the line raises BrokenPipeError
        try:
            caller = subprocess.run(self.box.wb_argv('status'), env=self.box.env, cwd=self.box.base,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=write_end,
                                    text=True, timeout=40)
        finally:
            os.close(write_end)
        self.assertEqual(caller.returncode, 3)
        self.assertIn('no backend is running', caller.stdout)

    def test_a_held_update_fence_makes_hub_start_fail_with_a_reason(self):
        self.hold_update_fence()
        result = self.box.wb('hub', 'start')
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn('업데이트', result.stderr)
        self.assertEqual(self.box.hub_pids(), [])

    def test_a_hub_that_dies_before_ready_fails_start_but_not_the_command(self):
        self.box.socket_dir.write_text('not a directory')  # the Hub cannot bind and exits before R
        started = self.box.wb('hub', 'start')
        self.assertEqual(started.returncode, 1, (started.stdout, started.stderr))
        self.assertIn('spawn_failed', started.stderr)
        self.assertIn(str(self.box.state / 'hub.log'), started.stderr)
        result = self.box.wb('status')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('no backend is running', result.stdout)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
        self.assertEqual(self.box.hub_pids(), [])
        self.assertIn('Hub start failed', (self.box.state / 'hub.log').read_text())

    def test_opt_out_env_disables_only_auto_start(self):
        env = dict(self.box.env, WORKBENCH_HUB_AUTOSTART='0')
        result = self.box.wb('status', env=env)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(self.box.hub_pids(), [])
        started = self.box.wb('hub', 'start', env=env)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(self.box.hub_pids(), [reported_pid(started)])

    def test_a_killed_hub_is_replaced_by_the_next_auto_start_command(self):
        first = reported_pid(self.box.wb('hub', 'start'))
        os.kill(first, signal.SIGKILL)  # the Hub this test started
        self.assertTrue(wait_until(lambda: not alive(first), 5))
        self.assertTrue(self.box.record_path.exists())  # a stale record stays behind
        result = self.box.wb('status')
        self.assertEqual(result.returncode, 3, result.stderr)
        holder = flock_holder(self.box.lock)
        self.assertIsNotNone(holder)
        self.assertNotEqual(holder, first)
        self.assertEqual(self.box.hub_pids(), [holder])
        self.assertEqual(self.box.record()['pid'], holder)

    def test_a_spawned_hub_exits_when_its_state_directory_disappears(self):
        pid = reported_pid(self.box.wb('hub', 'start'))
        shutil.rmtree(self.box.state_home)  # what removing a test's temp HOME does
        self.assertTrue(wait_until(lambda: not alive(pid), 10), 'Hub outlived its state directory')

    def test_a_spawned_hub_exits_when_its_state_directory_is_replaced(self):
        pid = reported_pid(self.box.wb('hub', 'start'))
        self.box.state.rename(self.box.base / 'old-hub-state')
        self.box.state.mkdir(mode=0o700)
        self.assertTrue(wait_until(lambda: not alive(pid), 10), 'Hub outlived the replacement of its state directory')


if __name__ == '__main__':
    unittest.main()
