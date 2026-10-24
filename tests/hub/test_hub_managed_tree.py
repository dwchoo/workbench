"""No Hub auto-start inside a WB-managed process tree (HB-03, seams S9 and S1).

Fixtures are real ancestor processes: a ``python -c`` whose exec argv has the backend
shape (``_backend --data-dir /x/workbench-fixture``) and a ``PR_SET_CHILD_SUBREAPER``
helper exec'd as ``… tests/hub/fixtures/shell_g2/lifecycle.py --supervisor …``. Expected
values are the fixture pids the test started, ``/proc/<pid>/stat`` ppid and
``/proc/locks``. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import unittest

from test_hub_process import flock_holder, wait_until
from test_hub_spawn import DRIVER, SpawnSandbox, parent_of, reported_pid

from workbench.hub.process import managed_ancestor

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
BACKEND_SHAPE = ('import subprocess, sys\n'
                 'raise SystemExit(subprocess.call(sys.argv[sys.argv.index("--") + 1:]))\n')
SLEEPER = 'import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(60)\n'
SPAWN_DIRECT = DRIVER.replace('from workbench.backend.cli import main\nraise SystemExit(main())\n',
                              'from workbench.hub.process import spawn_hub\nprint(spawn_hub(), flush=True)\n')


class ManagedTreeCase(unittest.TestCase):
    def setUp(self):
        self.box = SpawnSandbox(self)
        self.addCleanup(self.stop_fixtures)
        self.fixtures: list[subprocess.Popen] = []

    def stop_fixtures(self) -> None:
        """SIGTERM each fixture's process group (fixtures and their non-Hub children)."""
        for fixture in self.fixtures:
            try:
                os.killpg(fixture.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for fixture in self.fixtures:
            try:
                fixture.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(fixture.pid, signal.SIGKILL)
                fixture.wait(timeout=5)

    def start(self, argv: list[str], **kwargs) -> subprocess.Popen:
        options = dict(env=self.box.env, cwd=self.box.base, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, start_new_session=True)
        options.update(kwargs)
        process = subprocess.Popen(argv, **options)
        self.box.processes.append(process)
        self.fixtures.append(process)
        return process

    def backend_shaped(self, *command: str, **kwargs) -> subprocess.Popen:
        return self.start([sys.executable, '-c', BACKEND_SHAPE, '_backend', '--data-dir', '/x/workbench-fixture',
                           '--', *command], **kwargs)

    def subreaper(self, shape: str, *command: str, **kwargs) -> tuple[subprocess.Popen, Path]:
        report = self.box.base / f'subreaper-{len(self.fixtures)}.json'
        script = FIXTURES / ('shell_g2/lifecycle.py' if shape == '--supervisor' else 'subreaper.py')
        return self.start([sys.executable, '-I', str(script), shape, str(report), *command], **kwargs), report

    def sleeper_under(self, fixture: subprocess.Popen) -> int:
        line = fixture.stdout.readline().strip()
        self.assertTrue(line.isdigit(), line)
        pid = int(line)
        self.assertEqual(parent_of(pid), fixture.pid)
        return pid


class ManagedAncestorTests(ManagedTreeCase):
    def test_finds_a_backend_shaped_ancestor(self):
        fixture = self.backend_shaped(sys.executable, '-c', SLEEPER)
        self.assertEqual(managed_ancestor(self.sleeper_under(fixture)), fixture.pid)

    def test_finds_a_supervisor_shaped_subreaper_ancestor(self):
        fixture, _ = self.subreaper('--supervisor', sys.executable, '-c', SLEEPER)
        self.assertEqual(managed_ancestor(self.sleeper_under(fixture)), fixture.pid)

    def test_an_unreadable_proc_counts_as_inside(self):
        with tempfile.TemporaryDirectory() as fake:
            proc = Path(fake)
            self.assertIsNotNone(managed_ancestor(os.getpid(), proc=proc))  # no entry at all
            entry = proc / '100'
            entry.mkdir()
            (entry / 'cmdline').write_bytes(b'/usr/bin/python3\0-c\0pass\0')
            (entry / 'stat').write_bytes(b'100 (python3) S 200 100 100 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 5 0 0\n')
            self.assertIsNotNone(managed_ancestor(100, proc=proc))  # the parent's entry is missing
            (entry / 'stat').write_bytes(b'garbage')
            self.assertIsNotNone(managed_ancestor(100, proc=proc))
            parent = proc / '200'
            parent.mkdir()
            (parent / 'cmdline').write_bytes(b'/bin/sh\0')
            (parent / 'stat').write_bytes(b'200 (sh) S 0 200 200 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 5 0 0\n')
            (entry / 'stat').write_bytes(b'100 (python3) S 200 100 100 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 5 0 0\n')
            self.assertIsNone(managed_ancestor(100, proc=proc))  # a fully readable plain chain is outside

    def test_ordinary_ancestors_and_a_plain_subreaper_are_outside(self):
        fixture, _ = self.subreaper('--plain', sys.executable, '-c', SLEEPER)
        self.assertIsNone(managed_ancestor(self.sleeper_under(fixture)))

    def test_hub_start_under_a_plain_subreaper_starts_normally(self):
        output = self.box.base / 'hub-start.out'
        fixture, report = self.subreaper('--plain', '/bin/sh', '-c', 'exec "$@" > "$0"', str(output),
                                         *self.box.wb_argv('hub', 'start'))
        self.assertTrue(wait_until(lambda: output.exists() and 'pid ' in output.read_text(), 20))
        hub = int(output.read_text().split('pid ', 1)[1].split(',')[0])
        self.assertEqual(flock_holder(self.box.lock), hub)
        self.assertTrue(wait_until(lambda: parent_of(hub) == fixture.pid, 5), 'Hub not reparented to the subreaper')
        os.kill(hub, signal.SIGTERM)  # the Hub this test started; the subreaper then exits
        fixture.wait(timeout=20)
        result = json.loads(report.read_text())
        self.assertEqual(result['main'], 0)
        self.assertEqual(result['reaped'].get(str(hub)), 0)


MANAGED_TEXT = ("[workbench] WB가 관리하는 terminal 안에서는 Hub를 자동으로 시작하지 않습니다. "
                "외부 terminal에서 'wb hub start'를 실행하세요.")


class AutoStartHoldTests(ManagedTreeCase):
    SHAPES = ('backend', '--supervisor')

    def under(self, shape: str, *command: str, **kwargs) -> tuple[subprocess.Popen, Path | None]:
        if shape == 'backend':
            return self.backend_shaped(*command, **kwargs), None
        return self.subreaper(shape, *command, **kwargs)

    def assert_no_hub(self) -> None:
        self.assertEqual(self.box.hub_pids(), [])
        self.assertFalse(self.box.lock.exists() and flock_holder(self.box.lock) is not None)

    def test_auto_start_is_held_silently_without_a_tty(self):
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                fixture, _ = self.under(shape, *self.box.wb_argv('status'))
                out, err = fixture.communicate(timeout=40)
                self.assertEqual(fixture.returncode, 0 if shape != 'backend' else 3, err)
                self.assertNotIn('Hub', err)
                self.assert_no_hub()

    def test_auto_start_is_held_with_one_line_on_a_tty(self):
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                master, slave = pty.openpty()
                try:
                    fixture, _ = self.under(shape, *self.box.wb_argv('status'), stderr=slave)
                    os.close(slave)
                    slave = None
                    fixture.wait(timeout=40)
                    shown = b''
                    while select.select([master], [], [], 0.2)[0]:
                        try:
                            chunk = os.read(master, 4096)
                        except OSError:
                            break
                        if not chunk:
                            break
                        shown += chunk
                finally:
                    os.close(master)
                    if slave is not None:
                        os.close(slave)
                self.assertEqual(shown.decode().strip(), MANAGED_TEXT)
                self.assert_no_hub()

    def test_hub_start_refuses_with_exit_2(self):
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                driver = [sys.executable, '-c', 'import subprocess, sys\n'
                          'r = subprocess.run(sys.argv[1:], capture_output=True, text=True)\n'
                          'print(r.returncode); print(r.stderr.strip())\n', *self.box.wb_argv('hub', 'start')]
                fixture, _ = self.under(shape, *driver)
                out, _err = fixture.communicate(timeout=40)
                code, text = out.split('\n', 1)
                self.assertEqual(int(code), 2, out)
                self.assertEqual(text.strip(), MANAGED_TEXT)
                self.assert_no_hub()

    def test_a_running_hub_is_used_inside_the_managed_tree(self):
        hub = reported_pid(self.box.wb('hub', 'start'))  # started outside any managed tree
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                fixture, _ = self.under(shape, *self.box.wb_argv('hub', 'status'))
                out, err = fixture.communicate(timeout=40)
                self.assertIn(f'pid {hub}', out, err)
                started, _ = self.under(shape, *self.box.wb_argv('hub', 'start'))
                out, err = started.communicate(timeout=40)
                self.assertIn(f'Hub 실행 중: pid {hub}', out, err)
                self.assertEqual(self.box.hub_pids(), [hub])

    def test_a_hub_reparented_to_a_supervisor_refuses_itself_before_the_lock(self):
        # The driver bypasses the caller pre-check and calls spawn_hub directly.
        fixture, report = self.subreaper('--supervisor', sys.executable, '-c', SPAWN_DIRECT)
        out, err = fixture.communicate(timeout=40)
        self.assertEqual(out.strip(), 'managed_tree', err)
        result = json.loads(report.read_text())
        self.assertEqual(result['main'], 0)
        # Exactly the spawned Hub was reparented to the subreaper, exited 3 and was reaped:
        # the fixture only ends once no descendant is left.
        self.assertEqual(list(result['reaped'].values()), [3], result)
        self.assertFalse(self.box.lock.exists())
        self.assertFalse(self.box.record_path.exists())
        self.assert_no_hub()
        self.assertIn('inside a WB-managed process tree', (self.box.state / 'hub.log').read_text())


if __name__ == '__main__':
    unittest.main()
