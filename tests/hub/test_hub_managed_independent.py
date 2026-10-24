"""Independent verification of the managed-tree auto-start hold (HB-03, seam S9) and the HB-02 fixes.

Expected outcomes come from SPEC "WB 관리 process tree와 자동 시작 보류", the HB-03 ticket
(독립 초점) and the real argv shapes the product produces, not from the worker's tests:

* backend: ``python -I -c <bootstrap> _backend --data-dir … `` (``spawn_backend``) and
  ``maintenance.discover()``'s rule (an ``_backend`` element AND an element containing
  ``workbench``);
* supervisor: ``python -I …/terminal/shell_g2/lifecycle.py --supervisor <shell> <payload>``
  (``lifecycle.py`` shell hook) running as ``PR_SET_CHILD_SUBREAPER``.

Ancestors are real processes started here (a single stdlib script copied to the argv
shapes under test; the product backend/supervisor never run). Facts are read from the
kernel (``/proc/<pid>/stat`` ppid, ``/proc/locks``, ``/proc/<pid>/fd``). Every ``wb`` is a
real CLI subprocess in a temp HOME/XDG sandbox with the update fence relocated by a tiny
driver. Signals go only to process groups this module created (their leaders stay
unreaped until cleanup, so the pgid cannot be recycled) and to Hubs the sandbox started.

Run with ``discover -s tests/hub -p 'test_hub_managed_independent.py'``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import resource
import select
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

from test_hub_spawn_independent import (Box, DRIVER, alive, fd_targets, flock_holder, parent_of, reported_pid,
                                        ticks_of, wait_until)

from workbench.hub.process import UNVERIFIABLE, managed_ancestor

PROC_UNREADABLE_AUTOSTART_TEXT = "[workbench] 프로세스 정보를 읽을 수 없어 Hub 자동 시작을 보류했습니다."
PROC_UNREADABLE_START_TEXT = ("[workbench] 프로세스 정보를 읽을 수 없어 Hub를 시작하지 않았습니다. "
                              "/proc 접근(예: hidepid)을 확인하세요.")
MANAGED_TEXT = ("[workbench] WB가 관리하는 terminal 안에서는 Hub를 자동으로 시작하지 않습니다. "
                "외부 terminal에서 'wb hub start'를 실행하세요.")

# One stdlib script plays every ancestor: it optionally becomes a child subreaper, runs
# the command in WB_T_CMD ({"argv": [...], "env": {...}}), then reaps every descendant
# (like the WB supervisor) and writes {"main": <exit>, "reaped": {pid: exit}}.
ANCESTOR_BODY = textwrap.dedent("""\
    import ctypes, json, os, subprocess, sys
    if os.environ.get("WB_T_SUBREAPER") == "1":
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise SystemExit(99)
    spec = json.loads(os.environ["WB_T_CMD"])
    child = subprocess.Popen(spec["argv"], env={**os.environ, **spec.get("env", {})})
    reaped, main = {}, None
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            break
        code = os.waitstatus_to_exitcode(status)
        if pid == child.pid:
            main = code
        else:
            reaped[str(pid)] = code
    with open(os.environ["WB_T_REPORT"], "w") as out:
        json.dump({"main": main, "reaped": reaped}, out)
    """)
CODE_PLAIN = ANCESTOR_BODY
CODE_WB = "# the bootstrap names the package: workbench\n" + ANCESTOR_BODY
SLEEPER = {"argv": [sys.executable, "-c", "import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(60)\n"]}

# spawn_hub called directly, bypassing the caller pre-check (the test driver of the ticket).
# WB_T_LATE delays the spawn intermediate's exit so the Hub must wait to be reparented.
SPAWN_DIRECT = textwrap.dedent("""\
    import os, pathlib, time
    import workbench.backend.maintenance as maintenance
    maintenance.FENCE_BASE = pathlib.Path(os.environ.pop("WB_TEST_FENCE_BASE"))
    late = float(os.environ.pop("WB_T_LATE", "0"))
    if late:
        real_exit = os._exit
        def slow_exit(code):
            time.sleep(late)
            real_exit(code)
        os._exit = slow_exit
    from workbench.hub.process import spawn_hub
    print(spawn_hub(), flush=True)
    """)

# The real CLI entry (auto-start applies) with managed_ancestor reading a fake /proc.
FAKE_PROC_PRELUDE = textwrap.dedent("""\
    import os, pathlib, sys
    import workbench.backend.maintenance as maintenance
    maintenance.FENCE_BASE = pathlib.Path(os.environ.pop("WB_TEST_FENCE_BASE"))
    fake = pathlib.Path(os.environ.pop("WB_T_FAKE_ROOT"))
    mode = os.environ.pop("WB_T_FAKE")
    me = os.getpid()
    def entry(pid, cmd, ppid):
        directory = fake / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cmdline").write_bytes(b"\\0".join(cmd) + b"\\0")
        (directory / "stat").write_bytes(
            f"{pid} (x) S {ppid} 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 5 0 0\\n".encode())
    if mode == "plain":
        entry(me, [b"python", b"-c"], 500)
        entry(500, [b"sh"], 0)
    elif mode == "managed":
        entry(me, [b"python", b"-c"], 500)
        entry(500, [b"python", b"-I", b"-c", b"workbench", b"_backend"], 0)
    elif mode == "dangling":
        entry(me, [b"python", b"-c"], 500)  # the parent entry is gone
    from workbench.hub import process
    process.managed_ancestor.__kwdefaults__["proc"] = fake
    """)
FAKE_PROC_CLI = FAKE_PROC_PRELUDE + "from workbench.backend.cli import main\nraise SystemExit(main())\n"
FAKE_PROC_HUB = FAKE_PROC_PRELUDE + textwrap.dedent("""\
    from workbench.backend.cli import main
    ready_read, ready_write = os.pipe()
    code = main(["_hub", "--ready-fd", str(ready_write)])
    print(code, os.read(ready_read, 8).decode(), flush=True)
    """)

CROWD = textwrap.dedent("""\
    import os, resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(max(soft, 4096), hard), hard))
    marker = os.environ.pop("WB_T_MARKER")
    keep = [os.open(marker, os.O_RDONLY) for _ in range(1100)]
    """)


def drain(master: int) -> str:
    shown = b""
    while select.select([master], [], [], 0.3)[0]:
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        shown += chunk
    return shown.decode(errors="replace")


class Result:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err

    def __repr__(self):
        return f"Result(code={self.code}, out={self.out!r}, err={self.err!r})"


class ManagedCase(unittest.TestCase):
    """A sandbox, ancestor script copies at the argv shapes under test and owned process groups."""

    def setUp(self):
        self.box = Box(self)
        self.addCleanup(self.stop_groups)  # runs before Box.cleanup (LIFO)
        self.groups: list[tuple[subprocess.Popen, int]] = []
        self.anc = self.box.base / "anc"
        (self.anc / "terminal" / "shell_g2").mkdir(parents=True)
        (self.anc / "other").mkdir()
        (self.anc / "bin").mkdir()
        (self.anc / "workbench" / "bin").mkdir(parents=True)
        for path in ("terminal/shell_g2/lifecycle.py", "terminal/shell_g2/lifecycle.py.bak",
                     "terminal/shell_g2/lifecycle2.py", "other/lifecycle.py", "subreaper.py"):
            (self.anc / path).write_text(ANCESTOR_BODY)
        # argv[0] spellings: neutral, and containing "workbench" (a directory / a file name)
        for link in ("bin/py", "workbench/bin/python", "workbench-py"):
            (self.anc / link).symlink_to(sys.executable)
        self.py = str(self.anc / "bin" / "py")
        self.wb_dir_py = str(self.anc / "workbench" / "bin" / "python")
        self.wb_name_py = str(self.anc / "workbench-py")
        self.supervisor = str(self.anc / "terminal" / "shell_g2" / "lifecycle.py")
        self.counter = 0

    def stop_groups(self) -> None:
        for process, ticks in self.groups:
            self.terminate(process, ticks)

    @staticmethod
    def terminate(process: subprocess.Popen, ticks: int) -> None:
        # The leader is unreaped until here, so its pgid is still ours.
        try:
            if ticks_of(process.pid) == ticks:
                os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()

    def ancestor(self, argv: list[str], command: dict, *, subreaper: bool = False, cwd: Path | None = None,
                 **kwargs) -> tuple[subprocess.Popen, Path]:
        self.counter += 1
        report = self.box.base / f"report-{self.counter}.json"
        env = dict(self.box.env, WB_T_CMD=json.dumps(command), WB_T_REPORT=str(report),
                   WB_T_SUBREAPER="1" if subreaper else "0")
        options = dict(env=env, cwd=cwd or self.box.work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, start_new_session=True)
        options.update(kwargs)
        process = subprocess.Popen(argv, **options)
        self.groups.append((process, ticks_of(process.pid)))
        return process, report

    def forget(self, process: subprocess.Popen) -> None:
        for index, (candidate, ticks) in enumerate(self.groups):
            if candidate is process:
                self.terminate(candidate, ticks)
                del self.groups[index]
                return

    def sleeper_under(self, process: subprocess.Popen) -> int:
        line = process.stdout.readline().strip()
        self.assertTrue(line.isdigit(), line)
        return int(line)

    def shape_argv(self, shape: str) -> tuple[list[str], bool]:
        """Realistic managed argv shapes: (argv, is_subreaper)."""
        if shape == "backend":
            return [self.py, "-I", "-c", CODE_WB, "_backend", "--data-dir", "/x/y/project/.wb/data"], False
        return [self.py, "-I", self.supervisor, "--supervisor", "/bin/bash", "payload"], True

    @staticmethod
    def capture(outfile: Path, argv: list[str]) -> dict:
        """Run ``argv`` with stdout/stderr/exit code written to files (for ancestors that outlive it)."""
        return {"argv": ["/bin/sh", "-c", '"$@" >"$0" 2>"$0.err"; echo $? >"$0.rc"', str(outfile), *argv]}

    def captured(self, outfile: Path, timeout: float = 40) -> Result:
        rc = Path(f"{outfile}.rc")
        self.assertTrue(wait_until(lambda: rc.exists() and rc.read_text().endswith("\n"), timeout),
                        "the captured command did not finish")
        return Result(int(rc.read_text()), outfile.read_text(), Path(f"{outfile}.err").read_text())

    def assert_no_hub(self) -> None:
        self.assertEqual(self.box.hubs(), [])
        self.assertFalse(self.box.lock.exists() and flock_holder(self.box.lock) is not None)
        self.assertFalse(self.box.record_path.exists())

    def wait_no_hub(self) -> None:
        self.assertTrue(wait_until(lambda: not self.box.hubs(), 10), self.box.hubs())


class ManagedAncestorShapeTests(ManagedCase):
    """``managed_ancestor`` against real ancestor processes with argv variants."""

    X = "/x/y"

    def table(self) -> list[tuple[str, bool, list[str], bool, Path | None]]:
        """(label, managed, argv, subreaper, cwd)."""
        py, sup = self.py, self.supervisor
        real_tail = ["_backend", "--data-dir", self.X, "--project-dir", "/p", "--shell-kind", "bash",
                     "--shell-path", "/bin/bash", "--omp", "/o", "--omp-version", "1", "--bridge-extension", "/e"]
        return [
            # --- managed -----------------------------------------------------------------------
            ("backend: -I -c bootstrap naming the package", True, [py, "-I", "-c", CODE_WB, "_backend", "--data-dir", self.X], False, None),
            ("backend: no -I", True, [py, "-c", CODE_WB, "_backend", "--data-dir", self.X], False, None),
            ("backend: workbench only in the data dir", True,
             [py, "-I", "-c", CODE_PLAIN, "_backend", "--data-dir", "/home/u/.local/share/workbench/p"], False, None),
            ("backend: workbench only in the interpreter path", True,
             [self.wb_dir_py, "-I", "-c", CODE_PLAIN, "_backend"], False, None),
            ("backend: _backend after the options", True,
             [py, "-I", "-c", CODE_WB, "--data-dir", self.X, "_backend"], False, None),
            ("backend: the full spawn_backend option set", True, [py, "-I", "-c", CODE_WB, *real_tail], False, None),
            ("supervisor: real shape (-I, absolute helper, subreaper)", True,
             [py, "-I", sup, "--supervisor", "/bin/bash", "payload"], True, None),
            ("supervisor: without -I", True, [py, sup, "--supervisor", "/bin/sh", "payload"], True, None),
            ("supervisor: shape alone, not a subreaper", True, [py, "-I", sup, "--supervisor", "/bin/sh", "p"], False, None),
            ("supervisor: relative helper path", True,
             [py, "shell_g2/lifecycle.py", "--supervisor", "/bin/sh", "p"], True, self.anc / "terminal"),
            # --- outside -----------------------------------------------------------------------
            ("_backend element but no workbench anywhere", False,
             [py, "-I", "-c", CODE_PLAIN, "_backend", "--data-dir", self.X], False, None),
            ("workbench but no _backend element", False, [py, "-I", "-c", CODE_WB, "status"], False, None),
            ("near miss: _backend2", False, [py, "-I", "-c", CODE_WB, "_backend2", "--data-dir", self.X], False, None),
            ("near miss: --_backend=1", False, [py, "-I", "-c", CODE_WB, "--_backend=1"], False, None),
            ("near miss: _backend only inside a longer element", False,
             [py, "-I", "-c", CODE_WB, "--data-dir=/x/_backend", "x _backend y"], False, None),
            ("a Hub process", False, [py, "-I", "-c", CODE_WB, "_hub", "--ready-fd", "7"], False, None),
            ("process named like workbench", False, [self.wb_name_py, "-I", "-c", CODE_PLAIN, "status"], False, None),
            ("supervisor flag, other helper script", False,
             [py, "-I", str(self.anc / "other" / "lifecycle.py"), "--supervisor", "/bin/sh", "p"], True, None),
            ("shell_g2/lifecycle.py without --supervisor", False, [py, "-I", sup, "--plain"], True, None),
            ("helper suffix .py.bak", False,
             [py, "-I", str(self.anc / "terminal" / "shell_g2" / "lifecycle.py.bak"), "--supervisor"], True, None),
            ("helper lifecycle2.py", False,
             [py, "-I", str(self.anc / "terminal" / "shell_g2" / "lifecycle2.py"), "--supervisor"], True, None),
            ("generic subreaper", False, [py, "-I", str(self.anc / "subreaper.py")], True, None),
            ("generic subreaper that knows the word --supervisor", False,
             [py, "-I", str(self.anc / "subreaper.py"), "--supervisor"], True, None),
        ]

    def test_argv_shape_matrix(self):
        for label, managed, argv, subreaper, cwd in self.table():
            with self.subTest(label):
                process, _ = self.ancestor(argv, SLEEPER, subreaper=subreaper, cwd=cwd)
                pid = self.sleeper_under(process)
                self.assertEqual(parent_of(pid), process.pid)
                found = managed_ancestor(pid)
                if managed:
                    self.assertEqual(found, process.pid)
                else:
                    self.assertIsNone(found)
                self.forget(process)

    def test_the_walk_passes_plain_intermediates_and_stops_at_the_nearest_match(self):
        shells = {"argv": ["/bin/sh", "-c", '"$@"; exit $?', "sh", "/bin/sh", "-c", '"$@"; exit $?', "sh",
                           sys.executable, "-c", "import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(60)\n"]}
        for shape in ("backend", "supervisor"):
            with self.subTest(shape=shape):
                argv, subreaper = self.shape_argv(shape)
                process, _ = self.ancestor(argv, shells, subreaper=subreaper)
                pid = self.sleeper_under(process)
                first = parent_of(pid)
                self.assertNotEqual(first, process.pid)  # a plain shell sits in between
                self.assertEqual(managed_ancestor(pid), process.pid)
                self.assertEqual(managed_ancestor(process.pid), process.pid)  # a managed pid is its own answer
                self.forget(process)

    def test_the_nearest_managed_ancestor_wins_in_nested_trees(self):
        inner_argv, _ = self.shape_argv("backend")
        inner = {"argv": [*inner_argv],
                 "env": {"WB_T_CMD": json.dumps(SLEEPER), "WB_T_SUBREAPER": "0"}}
        outer_argv, subreaper = self.shape_argv("supervisor")
        process, _ = self.ancestor(outer_argv, inner, subreaper=subreaper)
        pid = self.sleeper_under(process)
        backend = parent_of(pid)
        self.assertNotEqual(backend, process.pid)
        self.assertEqual(parent_of(backend), process.pid)
        self.assertEqual(managed_ancestor(pid), backend)
        self.assertEqual(managed_ancestor(backend), backend)
        self.assertEqual(managed_ancestor(process.pid), process.pid)

    def test_a_pid_that_is_gone_counts_as_inside(self):
        child = subprocess.Popen(["/bin/sleep", "0"])
        child.wait()
        gone = child.pid
        self.assertFalse(Path(f"/proc/{gone}").exists())
        found = managed_ancestor(gone)
        self.assertIsNotNone(found)
        self.assertEqual(found, UNVERIFIABLE)

    def test_walking_a_process_that_dies_mid_walk_never_crashes_and_ends_inside(self):
        child = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        results: list[object] = []
        stop = threading.Event()
        errors: list[BaseException] = []

        def walk():
            while not stop.is_set():
                try:
                    results.append(managed_ancestor(child.pid))
                except BaseException as exc:  # pragma: no cover - the failure being looked for
                    errors.append(exc)
                    return

        thread = threading.Thread(target=walk)
        thread.start()
        try:
            wait_until(lambda: len(results) > 20, 5)
            os.kill(child.pid, signal.SIGKILL)  # the child this test started
            child.wait()
            wait_until(lambda: results and results[-1] is not None, 5)
            stop.set()
            thread.join(10)
        finally:
            stop.set()
            if child.poll() is None:
                child.kill()
                child.wait()
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive())
        self.assertTrue(all(result is None or isinstance(result, int) for result in results))
        self.assertIsNotNone(results[-1], 'after the process is gone the walk must say "inside"')


class FakeProc:
    """In-memory /proc for races: ``entries`` {pid: (cmdline, ppid)} and a per-read hook."""

    def __init__(self, entries, hook=None):
        self.entries, self.hook, self.parts = dict(entries), hook, ()

    def __truediv__(self, part):
        node = FakeProc(self.entries, self.hook)
        node.entries = self.entries  # shared table: the hook mutates it
        node.parts = (*self.parts, str(part))
        return node

    def read_bytes(self) -> bytes:
        pid, kind = int(self.parts[0]), self.parts[1]
        if self.hook is not None:
            self.hook(pid, kind, self.entries)
        if pid not in self.entries:
            raise FileNotFoundError(2, "No such process", f"/proc/{pid}/{kind}")
        cmdline, ppid = self.entries[pid]
        if cmdline is PermissionError:
            raise PermissionError(13, "denied")
        if kind == "cmdline":
            return b"\0".join(cmdline) + b"\0"
        comm = ppid[1] if isinstance(ppid, tuple) else b"x"
        parent = ppid[0] if isinstance(ppid, tuple) else ppid
        return b"%d (%s) S %d 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 5 0 0\n" % (pid, comm, parent)


class WalkRaceTests(unittest.TestCase):
    PLAIN, BACKEND = [b"python", b"-c"], [b"python", b"-I", b"-c", b"workbench", b"_backend"]

    def test_a_parent_that_exits_between_two_reads_is_inside(self):
        for vanishing_read in ("cmdline", "stat"):
            with self.subTest(vanishing_read=vanishing_read):
                def hook(pid, kind, entries, vanishing_read=vanishing_read):
                    if pid == 200 and kind == vanishing_read:
                        entries.pop(200, None)  # the intermediate exits just as we look at it

                proc = FakeProc({100: (self.PLAIN, 200), 200: (self.PLAIN, 300), 300: (self.PLAIN, 0)}, hook)
                self.assertIsNotNone(managed_ancestor(100, proc=proc))

    def test_a_transient_proc_failure_is_retried_and_a_clean_second_walk_is_outside(self):
        """Up to 3 walks: 1 or 2 transient failures still end "outside" (or the managed match); 3 stay inside."""
        for failures, plain_expected, managed_expected in ((1, None, 300), (2, None, 300), (3, "inside", "inside")):
            with self.subTest(failures=failures):
                for chain, expected in (({100: (self.PLAIN, 200), 200: (self.PLAIN, 300), 300: (self.PLAIN, 0)},
                                         plain_expected),
                                        ({100: (self.PLAIN, 200), 200: (self.PLAIN, 300), 300: (self.BACKEND, 0)},
                                         managed_expected)):
                    left = [failures]

                    def hook(pid, kind, entries, left=left):
                        if pid == 200 and kind == "cmdline" and left[0] > 0:
                            left[0] -= 1
                            raise FileNotFoundError(2, "an ancestor was exiting", f"/proc/{pid}/{kind}")

                    found = managed_ancestor(100, proc=FakeProc(chain, hook))
                    if expected == "inside":
                        self.assertIsNotNone(found)
                        self.assertEqual(found, UNVERIFIABLE)
                    else:
                        self.assertEqual(found, expected)

    def test_an_ancestor_that_exits_after_the_walk_passed_it_does_not_matter(self):
        def hook(pid, kind, entries):
            if pid == 300 and kind == "cmdline":
                entries.pop(200, None)  # already read; its death is irrelevant now

        proc = FakeProc({100: (self.PLAIN, 200), 200: (self.PLAIN, 300), 300: (self.PLAIN, 0)}, hook)
        self.assertIsNone(managed_ancestor(100, proc=proc))

    def test_a_managed_ancestor_is_still_found_when_a_later_one_vanishes(self):
        def hook(pid, kind, entries):
            if pid == 200 and kind == "stat":
                entries.pop(300, None)

        proc = FakeProc({100: (self.PLAIN, 200), 200: (self.BACKEND, 300), 300: (self.PLAIN, 0)}, hook)
        self.assertEqual(managed_ancestor(100, proc=proc), 200)  # match comes first: no later read happens

    def test_permission_errors_and_a_pid_cycle_are_inside_not_crashes(self):
        self.assertIsNotNone(managed_ancestor(100, proc=FakeProc({100: (PermissionError, 0)})))
        cycle = FakeProc({1: (self.PLAIN, 2), 2: (self.PLAIN, 1)})
        self.assertIsNotNone(managed_ancestor(1, proc=cycle))  # never reaches ppid 0: bounded, fail closed

    def test_a_zombie_ancestor_with_an_empty_cmdline_is_walked_through(self):
        plain = FakeProc({100: (self.PLAIN, 200), 200: ([], 300), 300: (self.PLAIN, 0)})
        self.assertIsNone(managed_ancestor(100, proc=plain))
        managed = FakeProc({100: (self.PLAIN, 200), 200: ([], 300), 300: (self.BACKEND, 0)})
        self.assertEqual(managed_ancestor(100, proc=managed), 300)

    def test_a_comm_that_imitates_stat_fields_cannot_redirect_the_walk(self):
        evil = b"x) S 999 (y"
        proc = FakeProc({100: (self.PLAIN, (200, evil)), 200: (self.BACKEND, 0)})
        self.assertEqual(managed_ancestor(100, proc=proc), 200)  # ppid is 200, not the 999 in comm
        self.assertIsNotNone(managed_ancestor(100, proc=FakeProc({100: (self.PLAIN, (999, evil))})))  # 999 unreadable


class AutoStartInsideTests(ManagedCase):
    """The real CLI under managed-shaped ancestors (auto-start before the handler, ``hub start``)."""

    SHAPES = ("backend", "supervisor")

    def baseline(self) -> Result:
        env = dict(self.box.env, WORKBENCH_HUB_AUTOSTART="0")
        done = self.box.wb("status", env=env)
        self.assertEqual(done.returncode, 3, done.stderr)
        return Result(done.returncode, done.stdout, done.stderr)

    def inside(self, shape: str, *args: str, stderr: str = "pipe", stdout: str = "pipe") -> Result:
        argv, subreaper = self.shape_argv(shape)
        masters, options = [], {}
        for name, mode in (("stdout", stdout), ("stderr", stderr)):
            if mode == "tty":
                master, slave = pty.openpty()
                masters.append((name, master, slave))
                options[name] = slave
            elif mode == "devfull":
                options[name] = os.open("/dev/full", os.O_WRONLY)
                masters.append((name, None, options[name]))
        process, report = self.ancestor(argv, {"argv": self.box.argv(*args)}, subreaper=subreaper, **options)
        for _name, _master, slave in masters:
            os.close(slave)
        out, err = process.communicate(timeout=60)
        shown = {name: drain(master) for name, master, _slave in masters if master is not None}
        for _name, master, _slave in masters:
            if master is not None:
                os.close(master)
        self.assertEqual(process.returncode, 0, (out, err))
        main = json.loads(report.read_text())["main"]
        return Result(main, shown.get("stdout", out), shown.get("stderr", err))

    def test_auto_start_is_held_silently_off_a_tty_and_the_command_still_runs(self):
        base = self.baseline()
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                done = self.inside(shape, "status")
                self.assertEqual((done.code, done.out, done.err), (base.code, base.out, ""))
                self.assert_no_hub()

    def test_auto_start_hold_shows_exactly_one_line_on_a_stderr_tty(self):
        base = self.baseline()
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                done = self.inside(shape, "status", stderr="tty")
                self.assertEqual((done.code, done.out), (base.code, base.out))  # stdout untouched by the notice
                self.assertEqual(done.err.replace("\r\n", "\n"), MANAGED_TEXT + "\n")
                self.assert_no_hub()

    def test_only_stderr_decides_whether_the_notice_is_shown(self):
        base = self.baseline()
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                done = self.inside(shape, "status", stdout="tty")
                self.assertEqual((done.code, done.err), (base.code, ""))
                self.assert_no_hub()

    def test_hub_start_refuses_with_exit_2_and_guidance_even_off_a_tty(self):
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                done = self.inside(shape, "hub", "start")
                self.assertEqual(done.code, 2, done)
                self.assertEqual(done.err.strip(), MANAGED_TEXT)
                self.assertEqual(done.out, "")
                self.assert_no_hub()

    def test_a_running_hub_is_still_used_from_inside_and_nothing_is_announced(self):
        hub = self.box.start_hub()  # started outside any managed tree
        ticks = ticks_of(hub)
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                status = self.inside(shape, "hub", "status")
                self.assertEqual(status.code, 0, status)
                self.assertIn(f"pid {hub}", status.out)
                start = self.inside(shape, "hub", "start")
                self.assertEqual(start.code, 0, start)
                self.assertIn(f"Hub 실행 중: pid {hub}", start.out)
                auto = self.inside(shape, "status", stderr="tty")
                self.assertEqual(auto.err, "", "a running Hub means nothing is being held")
                self.assertEqual(self.box.hubs(), [hub])
                self.assertEqual(ticks_of(hub), ticks)
                self.assertEqual(flock_holder(self.box.lock), hub)

    def test_look_alike_ancestors_are_not_over_refused(self):
        """Hub start succeeds under shapes that merely resemble a managed ancestor."""
        outsiders = {
            "_backend without workbench": ([self.py, "-I", "-c", CODE_PLAIN, "_backend", "--data-dir", "/x/y"], False),
            "workbench without _backend": ([self.py, "-I", "-c", CODE_WB, "status"], False),
            "named like workbench": ([self.wb_name_py, "-I", "-c", CODE_PLAIN, "status"], False),
            "generic subreaper": ([self.py, "-I", str(self.anc / "subreaper.py")], True),
            "other lifecycle.py with --supervisor": (
                [self.py, "-I", str(self.anc / "other" / "lifecycle.py"), "--supervisor", "/bin/sh", "p"], True),
        }
        for label, (argv, subreaper) in outsiders.items():
            with self.subTest(label):
                outfile = self.box.base / f"out-{self.counter + 1}"
                process, report = self.ancestor(argv, self.capture(outfile, self.box.argv("hub", "start")),
                                                subreaper=subreaper)
                done = self.captured(outfile)
                self.assertEqual(done.code, 0, done)
                self.assertIn("Hub를 시작했습니다", done.out)
                hub = reported_pid(subprocess.CompletedProcess([], 0, done.out, ""))
                self.assertEqual(flock_holder(self.box.lock), hub)
                if subreaper:
                    self.assertTrue(wait_until(lambda: parent_of(hub) == process.pid, 5))
                stopped = self.box.wb("hub", "stop", "--yes")
                self.assertEqual(stopped.returncode, 0, (stopped.stdout, stopped.stderr))
                self.wait_no_hub()
                process.wait(timeout=30)  # a subreaper ends once its orphan (the Hub) is reaped
                self.forget(process)


class SelfCheckAfterReparentTests(ManagedCase):
    """``spawn_hub`` called directly (pre-check bypassed): the Hub judges its post-reparent ancestry."""

    def spawn_direct(self, shape_argv: list[str], subreaper: bool, late: float):
        outfile = self.box.base / f"spawn-{self.counter + 1}"
        env = {"WB_T_LATE": str(late)} if late else {}
        command = self.capture(outfile, [sys.executable, "-c", SPAWN_DIRECT])
        command["env"] = env
        process, report = self.ancestor(shape_argv, command, subreaper=subreaper)
        return process, report, outfile

    def test_a_hub_reparented_into_a_supervisor_exits_before_the_lock(self):
        for late in (0.0, 1.0):
            with self.subTest(late=late):
                argv, subreaper = self.shape_argv("supervisor")
                process, report, outfile = self.spawn_direct(argv, subreaper, late)
                done = self.captured(outfile)
                self.assertEqual(done.out.strip(), "managed_tree", done)
                process.wait(timeout=30)  # ends only once nothing is left below it
                result = json.loads(report.read_text())
                self.assertEqual(result["main"], 0)
                self.assertEqual(list(result["reaped"].values()), [3], result)  # exactly the Hub, exit 3
                hub_pid = int(next(iter(result["reaped"])))
                self.assertFalse(Path(f"/proc/{hub_pid}").exists())
                self.assertFalse(self.box.lock.exists())
                self.assertFalse(self.box.record_path.exists())
                self.assert_no_hub()
                self.assertIn("inside a WB-managed process tree", self.box.log_path.read_text())
                self.forget(process)
                self.box.log_path.unlink()

    def test_a_backend_shaped_ancestor_is_not_a_subreaper_so_the_hub_escapes_and_starts(self):
        """The check looks at the tree after reparenting, not at the spawn-time parents."""
        for late in (0.0, 1.0):
            with self.subTest(late=late):
                argv, subreaper = self.shape_argv("backend")
                process, report, outfile = self.spawn_direct(argv, subreaper, late)
                done = self.captured(outfile)
                self.assertEqual(done.out.strip(), "started", done)
                (hub,) = self.box.hubs()
                self.assertEqual(flock_holder(self.box.lock), hub)
                self.assertNotIn(parent_of(hub), (process.pid,))
                self.assertIsNone(managed_ancestor(hub))
                process.wait(timeout=30)
                self.assertEqual(json.loads(report.read_text())["reaped"], {})
                stopped = self.box.wb("hub", "stop", "--yes")
                self.assertEqual(stopped.returncode, 0, (stopped.stdout, stopped.stderr))
                self.wait_no_hub()
                self.forget(process)

    def test_a_hub_reparented_into_a_plain_subreaper_starts(self):
        for late in (0.0, 1.0):
            with self.subTest(late=late):
                process, report, outfile = self.spawn_direct([self.py, "-I", str(self.anc / "subreaper.py")], True, late)
                done = self.captured(outfile)
                self.assertEqual(done.out.strip(), "started", done)
                (hub,) = self.box.hubs()
                self.assertEqual(flock_holder(self.box.lock), hub)
                self.assertTrue(wait_until(lambda: parent_of(hub) == process.pid, 5))
                stopped = self.box.wb("hub", "stop", "--yes")
                self.assertEqual(stopped.returncode, 0, (stopped.stdout, stopped.stderr))
                process.wait(timeout=30)
                self.assertEqual(list(json.loads(report.read_text())["reaped"].values()), [0])
                self.wait_no_hub()
                self.forget(process)


class UnreadableProcTests(ManagedCase):
    """``/proc`` problems are injected through ``managed_ancestor``'s ``proc`` default."""

    def fake(self, mode: str) -> dict:
        self.counter += 1
        root = self.box.base / f"fakeproc-{mode}-{self.counter}"
        root.mkdir()
        return dict(self.box.env, WB_T_FAKE=mode, WB_T_FAKE_ROOT=str(root))

    def cli(self, mode: str, *args: str, tty: bool = False) -> Result:
        kwargs = {}
        if tty:
            master, slave = pty.openpty()
            kwargs["stderr"] = slave
        process = subprocess.Popen([sys.executable, "-c", FAKE_PROC_CLI, *args], env=self.fake(mode),
                                   cwd=self.box.work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=kwargs.get("stderr", subprocess.PIPE), text=True)
        if tty:
            os.close(slave)
        out, err = process.communicate(timeout=60)
        if tty:
            err = drain(master)
            os.close(master)
        return Result(process.returncode, out, err)

    def test_an_unreadable_chain_holds_auto_start_and_refuses_hub_start(self):
        for mode in ("unreadable", "dangling"):
            with self.subTest(mode=mode):
                quiet = self.cli(mode, "status")
                self.assertEqual((quiet.code, quiet.err), (3, ""), quiet)
                loud = self.cli(mode, "status", tty=True)
                self.assertEqual((loud.code, loud.err.strip()), (3, PROC_UNREADABLE_AUTOSTART_TEXT), loud)
                refused = self.cli(mode, "hub", "start")
                self.assertEqual((refused.code, refused.err.strip()), (2, PROC_UNREADABLE_START_TEXT), refused)
                self.assert_no_hub()

    def test_a_managed_chain_in_the_fake_proc_is_held_too(self):
        refused = self.cli("managed", "hub", "start")
        self.assertEqual((refused.code, refused.err.strip()), (2, MANAGED_TEXT), refused)
        self.assert_no_hub()

    def test_a_fully_readable_plain_chain_starts_the_hub(self):
        """Control: the same injection with a clean chain is not refused."""
        done = self.cli("plain", "hub", "start")
        self.assertEqual(done.code, 0, done)
        self.assertEqual(self.box.hubs(), [reported_pid(subprocess.CompletedProcess([], 0, done.out, ""))])

    def test_the_hub_itself_exits_3_with_m_before_the_lock_when_proc_is_unreadable(self):
        for mode in ("unreadable", "dangling", "managed"):
            with self.subTest(mode=mode):
                done = subprocess.run([sys.executable, "-c", FAKE_PROC_HUB], env=self.fake(mode), cwd=self.box.work,
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
                self.assertEqual(done.stdout.split(), ["3", "M"], (done.stdout, done.stderr))
                self.assertFalse(self.box.state.exists(), "the self-check comes before any state is touched")
                self.assertFalse(self.box.lock.exists())
                self.assert_no_hub()


class Hb02FixTests(ManagedCase):
    """HB-02 review fixes: poll() instead of select(), unbreakable report, log rotation."""

    def crowded(self, *args: str) -> subprocess.CompletedProcess:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.assertTrue(hard == resource.RLIM_INFINITY or hard >= 1200, f"environment: RLIMIT_NOFILE hard={hard}")
        marker = self.box.base / "marker"
        marker.write_text("m")
        env = dict(self.box.env, WB_T_MARKER=str(marker))
        return subprocess.run([sys.executable, "-c", CROWD + DRIVER, *args], env=env, cwd=self.box.work,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def test_spawn_status_and_auto_start_work_with_fds_above_1024(self):
        started = self.crowded("hub", "start")  # the ready pipe is created at fd >= 1100: select() would fail
        self.assertEqual(started.returncode, 0, started.stderr)
        hub = reported_pid(started)
        self.assertEqual(self.box.hubs(), [hub])
        self.assertNotIn(str(self.box.base / "marker"), fd_targets(hub).values(), "the caller's fds leaked")
        status = self.crowded("hub", "status")  # HubClient waits on a socket above 1024
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn(f"pid {hub}", status.stdout)
        self.assertEqual(self.box.wb("hub", "stop", "--yes").returncode, 0)
        self.wait_no_hub()
        auto = self.crowded("status")  # auto-start from a crowded caller, then the command's own answer
        self.assertEqual(auto.returncode, 3, auto.stderr)
        self.assertIn("no backend is running", auto.stdout)
        self.assertEqual(auto.stderr, "")
        holder = flock_holder(self.box.lock)
        self.assertEqual(self.box.hubs(), [holder])

    def hold_fence(self) -> None:
        self.box.fence_base.mkdir(mode=0o700)
        self.box.hold_flock(self.box.fence_file)

    def test_a_broken_stderr_never_changes_the_commands_exit_code(self):
        # Failure line A: the update fence is held.  Failure line B: the Hub dies before ready.
        base = self.box.wb("status", env=dict(self.box.env, WORKBENCH_HUB_AUTOSTART="0"))
        self.assertEqual(base.returncode, 3)
        self.hold_fence()
        for label in ("closed pipe reader", "/dev/full"):
            with self.subTest(label):
                if label == "closed pipe reader":
                    read_end, write_end = os.pipe()
                    os.close(read_end)
                else:
                    write_end = os.open("/dev/full", os.O_WRONLY)
                try:
                    done = subprocess.run(self.box.argv("status"), env=self.box.env, cwd=self.box.work,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=write_end,
                                          text=True, timeout=60)
                finally:
                    os.close(write_end)
                self.assertEqual((done.returncode, done.stdout), (base.returncode, base.stdout))
                self.assertEqual(self.box.hubs(), [])

    def test_a_broken_stderr_with_a_start_failure_line_keeps_the_exit_code(self):
        self.box.socket_dir.write_text("not a directory")  # the Hub cannot bind: spawn_failed, a different line
        base = self.box.wb("status", env=dict(self.box.env, WORKBENCH_HUB_AUTOSTART="0"))
        for label in ("full", "closed fd 2", "hung-up tty"):
            with self.subTest(label):
                argv, kwargs, fd = self.box.argv("status"), {}, None
                if label == "full":
                    fd = kwargs["stderr"] = os.open("/dev/full", os.O_WRONLY)
                elif label == "closed fd 2":
                    argv = [sys.executable, "-c", "import os\nos.close(2)\n" + DRIVER, "status"]
                else:
                    master, fd = pty.openpty()
                    kwargs["stderr"] = fd
                    os.close(master)  # hang up before the child writes
                try:
                    done = subprocess.run(argv, env=self.box.env, cwd=self.box.work, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.PIPE, text=True, timeout=60, **kwargs)
                finally:
                    if fd is not None:
                        os.close(fd)
                self.assertEqual((done.returncode, done.stdout), (base.returncode, base.stdout), done.stderr)
                self.assertEqual(self.box.hubs(), [])

    def test_a_broken_stderr_also_keeps_the_managed_tree_notice_from_failing_the_command(self):
        base = self.box.wb("status", env=dict(self.box.env, WORKBENCH_HUB_AUTOSTART="0"))
        master, slave = pty.openpty()
        argv, subreaper = self.shape_argv("supervisor")
        process, report = self.ancestor(argv, {"argv": self.box.argv("status")}, subreaper=subreaper, stderr=slave)
        os.close(slave)
        os.close(master)  # nobody reads the terminal any more
        process.communicate(timeout=60)
        self.assertEqual(json.loads(report.read_text())["main"], base.returncode)

    # --- hub.log rotation -----------------------------------------------------------------

    def seed(self, name: str, size: int, byte: bytes) -> bytes:
        self.box.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.box.state / name
        path.write_bytes(byte * size)
        path.chmod(0o600)
        return path.read_bytes()

    def logs(self) -> list[str]:
        return sorted(path.name for path in self.box.state.iterdir() if path.name.startswith("hub.log"))

    def test_log_rotation_keeps_exactly_one_generation_across_restarts(self):
        first = self.seed("hub.log", 1024 * 1024 + 1, b"A")
        self.seed("hub.log.1", 10, b"Z")  # an older generation is replaced, not accumulated
        hub = self.box.start_hub()
        self.assertEqual((self.box.state / "hub.log.1").read_bytes(), first)
        self.assertEqual(self.logs(), ["hub.log", "hub.log.1"])
        self.assertLess(self.box.log_path.stat().st_size, 1024 * 1024)
        self.assertEqual({os.readlink(f"/proc/{hub}/fd/{fd}") for fd in (1, 2)}, {str(self.box.log_path)})
        self.assertEqual(self.box.wb("hub", "stop", "--yes").returncode, 0)
        self.wait_no_hub()
        with open(self.box.log_path, "ab") as grow:  # the second generation is the fresh log grown past 1 MiB
            grow.write(b"B" * (1024 * 1024 + 1))
        second = self.box.log_path.read_bytes()
        self.box.start_hub()
        self.assertEqual((self.box.state / "hub.log.1").read_bytes(), second)
        self.assertNotIn(b"A" * 64, (self.box.state / "hub.log.1").read_bytes())
        self.assertEqual(self.logs(), ["hub.log", "hub.log.1"])
        self.assertEqual(oct(self.box.log_path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((self.box.state / "hub.log.1").stat().st_mode & 0o777), "0o600")

    def test_a_log_of_exactly_1_mib_is_left_alone(self):
        data = self.seed("hub.log", 1024 * 1024, b"C")
        self.box.start_hub()
        self.assertEqual(self.logs(), ["hub.log"])
        self.assertEqual(self.box.log_path.read_bytes()[:len(data)], data)

    def test_a_rotation_that_cannot_happen_never_blocks_the_hub(self):
        data = self.seed("hub.log", 1024 * 1024 + 1, b"D")
        blocker = self.box.state / "hub.log.1"
        (blocker / "keep").mkdir(parents=True)  # a non-empty directory: os.replace(hub.log, hub.log.1) fails
        (blocker / "keep" / "x").write_text("x")
        hub = self.box.start_hub()
        self.assertEqual(flock_holder(self.box.lock), hub)
        self.assertEqual(self.box.log_path.read_bytes()[:len(data)], data)
        self.assertTrue((blocker / "keep" / "x").exists())
        self.assertIn(b"rotation skipped", self.box.log_path.read_bytes()[len(data):])
        self.assertEqual({os.readlink(f"/proc/{hub}/fd/{fd}") for fd in (1, 2)}, {str(self.box.log_path)})


if __name__ == "__main__":
    unittest.main()
