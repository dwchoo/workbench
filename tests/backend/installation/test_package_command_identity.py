"""Real-child ownership checks for the independent packaging command harness."""
import json
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_package_assets_independent as assets


class CommandIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='wb-command-identity-', dir=tempfile.gettempdir())
        self.root = Path(self.directory.name)
        self.env = {'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1'}
        for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME',
                    'XDG_RUNTIME_DIR', 'TMPDIR'):
            folder = self.root / key
            folder.mkdir(mode=0o700)
            self.env[key] = str(folder)
        self.runner = type('OwnedCommand', (assets.GitInstalledAssetsTests,), {
            'project': self.root, 'artifact_run': self.root, 'env': self.env})
        self.processes = []
        self.signals = []
        self.real_popen = subprocess.Popen
        self.real_waitid = os.waitid
        self.real_killpg = os.killpg
        self.descendant_files = []
        self.libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(self.libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.previous_subreaper = previous.value
        self.assertEqual(self.libc.prctl(36, 1, 0, 0, 0), 0)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(lambda: self.libc.prctl(36, self.previous_subreaper, 0, 0, 0))
        self.addCleanup(self.reap_owned)

    def start(self, *args, **kwargs):
        process = self.real_popen(*args, **kwargs)
        self.processes.append(process)
        return process

    def guarded_signal(self, pgid, sig):
        self.assertIn(pgid, [process.pid for process in self.processes])
        try:
            observation = self.real_waitid(os.P_PID, pgid,
                                          os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            self.fail('unsafe killpg attempted after owned leader was reaped')
        self.signals.append((pgid, sig, observation))
        return self.real_killpg(pgid, sig)

    def reap_owned(self):
        for process in self.processes:
            try:
                self.real_waitid(os.P_PID, process.pid,
                                 os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                continue  # Never signal a numeric identity already released.
            try:
                self.real_killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        for path in self.descendant_files:
            if not path.exists():
                continue
            pid = int(path.read_text())
            try:
                self.real_waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                continue
            os.kill(pid, signal.SIGKILL)  # Still an unreaped, adopted owned child.
            os.waitpid(pid, 0)

    def assert_reaped(self):
        self.assertTrue(self.signals, 'owned group cleanup was not exercised')
        for process in self.processes:
            with self.assertRaises(ChildProcessError):
                self.real_waitid(os.P_PID, process.pid,
                                 os.WEXITED | os.WNOHANG | os.WNOWAIT)

    def test_normal_exit_retains_leader_identity_until_cleanup_and_preserves_output(self):
        with patch('subprocess.Popen', side_effect=self.start), \
                patch('os.killpg', side_effect=self.guarded_signal):
            stdout = self.runner.command([sys.executable, '-I', '-c',
                                          "import sys; print('normal-out'); print('normal-err', file=sys.stderr)"])
        self.assertEqual(stdout, 'normal-out\n')
        entry = json.loads((self.root / 'commands.jsonl').read_text())
        self.assertEqual((entry['exit'], entry['stderr']), (0, 'normal-err\n'))
        self.assertTrue(all(observation is not None for _, _, observation in self.signals))
        self.assert_reaped()

    def descendant_script(self, *, leader_sleeps):
        path = self.root / 'descendant.pid'
        self.descendant_files.append(path)
        return ("import os, time\n"
                "pid = os.fork()\n"
                "if pid == 0:\n"
                "    time.sleep(5)\n"
                "    os._exit(0)\n"
                f"open({str(path)!r}, 'w').write(str(pid))\n"
                "print('leader-output', flush=True)\n"
                + ("time.sleep(5)\n" if leader_sleeps else ""))

    def assert_descendant_killed(self):
        pid = int(self.descendant_files[-1].read_text())
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            child, status = os.waitpid(pid, os.WNOHANG)
            if child:
                self.assertTrue(os.WIFSIGNALED(status))
                self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
                return
            time.sleep(0.01)
        self.fail('owned descendant survived command cleanup')

    def test_timeout_kills_owned_descendant_and_reaps_leader_with_bounded_cleanup(self):
        script = self.descendant_script(leader_sleeps=True)
        started = time.monotonic()
        with patch('subprocess.Popen', side_effect=self.start), \
                patch('os.killpg', side_effect=self.guarded_signal):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.runner.command([sys.executable, '-I', '-c', script], timeout=0.15)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assert_reaped()
        self.assert_descendant_killed()

    def test_exited_leader_with_descendant_output_does_not_delay_capture(self):
        script = self.descendant_script(leader_sleeps=False)
        started = time.monotonic()
        with patch('subprocess.Popen', side_effect=self.start), \
                patch('os.killpg', side_effect=self.guarded_signal):
            stdout = self.runner.command([sys.executable, '-I', '-c', script], timeout=0.3)
        self.assertEqual(stdout, 'leader-output\n')
        self.assertLess(time.monotonic() - started, 1.5)
        self.assert_reaped()
        self.assert_descendant_killed()

    def test_exception_after_leader_exit_preserves_error_and_cleans_owned_group(self):
        class MonitoringError(Exception):
            pass

        error = MonitoringError('owned output monitoring interrupted')
        script = self.descendant_script(leader_sleeps=False) + 'time.sleep(0.1)\n'

        def interrupt_after_exit(signum, frame):
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if self.real_waitid(os.P_PID, self.processes[0].pid,
                                    os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                    raise error
                time.sleep(0.005)
            self.fail('fixture leader did not exit before monitoring interruption')

        previous = signal.signal(signal.SIGALRM, interrupt_after_exit)
        started = time.monotonic()
        try:
            with patch('subprocess.Popen', side_effect=self.start), \
                    patch('os.killpg', side_effect=self.guarded_signal):
                signal.setitimer(signal.ITIMER_REAL, 0.02)
                with self.assertRaises(MonitoringError) as raised:
                    self.runner.command([sys.executable, '-I', '-c', script], timeout=1)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        self.assertIs(raised.exception, error)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assert_reaped()
        self.assert_descendant_killed()

    def test_excess_output_fails_bounded_and_reaps_owned_leader(self):
        for fd in (1, 2):
            with self.subTest(fd=fd), \
                    patch('subprocess.Popen', side_effect=self.start), \
                    patch('os.killpg', side_effect=self.guarded_signal):
                with self.assertRaisesRegex(AssertionError, 'command output exceeds'):
                    self.runner.command([sys.executable, '-I', '-c',
                                         f'import os; os.write({fd}, b"x" * (9 * 1024 * 1024))'],
                                        timeout=1)
            self.assert_reaped()


if __name__ == '__main__':
    unittest.main()
