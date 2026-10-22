"""tools/shm_tests.py: tmpfs TMPDIR wrapper and stale-root cleaner (temp dirs only)."""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools import shm_tests

GIB = 1024 ** 3
RECORD = ("import os,sys;"
          "open(sys.argv[1],'w').write('\\n'.join(os.environ.get(k,'') for k in ('TMPDIR','TEMP','TMP')))")


class ShmCase(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="shmtest-"))
        self.addCleanup(self._remove_work)
        self.shm = self.work / "fake-shm"
        self.tmp = self.work / "fake-tmp"
        self.out = self.work / "out"
        for p in (self.shm, self.tmp, self.out):
            p.mkdir()

    def _remove_work(self) -> None:
        for dirpath, dirnames, _files in os.walk(self.work):
            for name in dirnames:
                path = os.path.join(dirpath, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o700)
        shutil.rmtree(self.work, ignore_errors=True)

    def run_main(self, args, *, free=100 * GIB, env=None):
        err = io.StringIO()
        environ = {k: v for k, v in os.environ.items() if k != "WB_TEST_SHM_MIN_GB"}
        environ.update(env or {})
        def free_fn(path):
            if not Path(path).is_dir():
                raise FileNotFoundError(2, "No such file or directory", path)
            return free

        with patch.dict(os.environ, environ, clear=True), contextlib.redirect_stderr(err):
            rc = shm_tests.main(args, shm_dir=str(self.shm), tmp_dir=str(self.tmp),
                                free_fn=free_fn)
        return rc, err.getvalue()

    def record(self, name="rec"):
        return str(self.out / name)

    def recorded(self, name="rec"):
        return (self.out / name).read_text().split("\n")


class ChooseBaseTests(ShmCase):
    def test_uses_shm_when_enough_free_and_sets_tmpdir(self):
        rc, err = self.run_main(["--", sys.executable, "-c", RECORD, self.record()], free=9 * GIB)
        self.assertEqual(rc, 0, err)
        tmpdir, temp, tmp = self.recorded()
        self.assertEqual(Path(tmpdir).parent, self.shm)
        self.assertTrue(Path(tmpdir).name.startswith("wb-tests."))
        self.assertEqual((temp, tmp), (tmpdir, tmpdir))
        self.assertNotIn("warning", err)

    def test_falls_back_to_tmp_below_threshold_with_one_line_warning(self):
        rc, err = self.run_main(["--", sys.executable, "-c", RECORD, self.record()], free=7 * GIB)
        self.assertEqual(rc, 0, err)
        self.assertEqual(Path(self.recorded()[0]).parent, self.tmp)
        warnings = [l for l in err.splitlines() if "warning" in l]
        self.assertEqual(len(warnings), 1, err)
        self.assertIn(str(self.shm), warnings[0])

    def test_threshold_overridable_by_flag_and_env(self):
        cmd = ["--", sys.executable, "-c", RECORD, self.record()]
        self.run_main(["--min-free-gb", "2"] + cmd, free=4 * GIB)
        self.assertEqual(Path(self.recorded()[0]).parent, self.shm)
        self.run_main(cmd, free=4 * GIB, env={"WB_TEST_SHM_MIN_GB": "2"})
        self.assertEqual(Path(self.recorded()[0]).parent, self.shm)
        self.run_main(["--min-free-gb", "16"] + cmd, free=9 * GIB)
        self.assertEqual(Path(self.recorded()[0]).parent, self.tmp)

    def test_base_flag_overrides(self):
        other = self.work / "other"
        other.mkdir()
        self.run_main(["--base", str(other), "--", sys.executable, "-c", RECORD, self.record()], free=1)
        self.assertEqual(Path(self.recorded()[0]).parent, other)

    def test_missing_shm_falls_back(self):
        shutil.rmtree(self.shm)
        rc, err = self.run_main(["--", sys.executable, "-c", RECORD, self.record()])
        self.assertEqual(rc, 0, err)
        self.assertEqual(Path(self.recorded()[0]).parent, self.tmp)


class ExitCodeTests(ShmCase):
    def test_propagates_exit_code(self):
        rc, _ = self.run_main(["--", sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(rc, 7)

    def test_missing_command_is_127_and_cleans_up(self):
        rc, err = self.run_main(["--", str(self.work / "no-such-binary")])
        self.assertEqual(rc, 127, err)
        self.assertEqual(list(self.shm.iterdir()), [])

    def test_requires_command(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            shm_tests.main([], shm_dir=str(self.shm), tmp_dir=str(self.tmp))
        self.assertNotEqual(ctx.exception.code, 0)


FILL = ("import os,sys\n"
        "t=os.environ['TMPDIR']\n"
        "os.makedirs(t+'/a/b/c');open(t+'/a/b/c/f','w').write('x'*5000)\n"
        "os.makedirs(t+'/ro');open(t+'/ro/f','w').write('y');os.chmod(t+'/ro',0o500)\n"
        "os.symlink('/etc',t+'/link')\n"
        "sys.exit(int(sys.argv[1]))\n")


class CleanupTests(ShmCase):
    def test_root_removed_after_success_and_failure(self):
        for code in (0, 5):
            rc, err = self.run_main(["--", sys.executable, "-c", FILL, str(code)])
            self.assertEqual(rc, code, err)
            self.assertEqual(list(self.shm.iterdir()), [], err)
        self.assertTrue(Path("/etc/passwd").exists())

    def test_summary_line_on_stderr_before_removal(self):
        _, err = self.run_main(["--", sys.executable, "-c", FILL, "0"])
        lines = [l for l in err.splitlines() if l.startswith("shm_tests:") and "root=" in l]
        self.assertEqual(len(lines), 1, err)
        for key in ("base=", "root=", "size=", "peak=", "elapsed="):
            self.assertIn(key, lines[0])
        self.assertIn(str(self.shm), lines[0])

    def test_keep_keeps_root_and_prints_path(self):
        rc, err = self.run_main(["--keep", "--", sys.executable, "-c", FILL, "0"])
        self.assertEqual(rc, 0, err)
        roots = list(self.shm.iterdir())
        self.assertEqual(len(roots), 1)
        self.assertIn(str(roots[0]), err)
        self.assertTrue((roots[0] / "a/b/c/f").exists())
        os.chmod(roots[0] / "ro", 0o700)

    def test_cleanup_after_keyboard_interrupt_in_parent(self):
        real_wait = subprocess.Popen.wait
        calls = {"n": 0}

        def flaky(self_, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt
            return real_wait(self_, *a, **k)

        with patch.object(subprocess.Popen, "wait", flaky):
            rc, err = self.run_main(["--", sys.executable, "-c", FILL, "3"])
        self.assertEqual(rc, 3, err)
        self.assertEqual(list(self.shm.iterdir()), [], err)


MODE_ZERO_SUBDIR = ("import os,sys\n"
                    "t=os.environ['TMPDIR']\n"
                    "os.makedirs(t+'/z/inner');open(t+'/z/inner/f','w').write('x')\n"
                    "os.chmod(t+'/z',0)\n"
                    "sys.exit(4)\n")
MODE_ZERO_ROOT = ("import os,sys\n"
                  "t=os.environ['TMPDIR']\n"
                  "os.makedirs(t+'/a');open(t+'/a/f','w').write('x')\n"
                  "os.chmod(t,0)\n"
                  "sys.exit(6)\n")


class UnwritableTreeTests(ShmCase):
    def test_mode_zero_subdir_is_removed_and_exit_code_preserved(self):
        rc, err = self.run_main(["--", sys.executable, "-c", MODE_ZERO_SUBDIR])
        self.assertEqual(rc, 4, err)
        self.assertEqual(list(self.shm.iterdir()), [], err)

    def test_mode_zero_root_is_removed_and_exit_code_preserved(self):
        rc, err = self.run_main(["--", sys.executable, "-c", MODE_ZERO_ROOT])
        self.assertEqual(rc, 6, err)
        self.assertEqual(list(self.shm.iterdir()), [], err)

    def test_remove_failure_is_reported_but_does_not_change_exit_code(self):
        with patch.object(shm_tests, "remove_root", side_effect=OSError("boom")):
            rc, err = self.run_main(["--", sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(rc, 7, err)
        self.assertIn("boom", err)

    def test_unexpected_remove_error_does_not_change_exit_code(self):
        with patch.object(shm_tests, "remove_root", side_effect=TypeError("odd")):
            rc, err = self.run_main(["--", sys.executable, "-c", "raise SystemExit(9)"])
        self.assertEqual(rc, 9, err)
        self.assertIn("odd", err)

    def test_base_dir_mode_is_never_changed(self):
        base = self.work / "mybase"
        base.mkdir()
        os.chmod(base, 0o750)
        rc, err = self.run_main(["--base", str(base), "--", sys.executable, "-c", MODE_ZERO_ROOT])
        self.assertEqual(rc, 6, err)
        self.assertEqual(list(base.iterdir()), [], err)
        self.assertEqual(stat_mode(base), 0o750)


def stat_mode(path) -> int:
    return os.stat(path).st_mode & 0o777


class BaseFailureTests(ShmCase):
    def test_unwritable_default_base_falls_back_to_tmp_with_warning(self):
        os.chmod(self.shm, 0o500)
        rc, err = self.run_main(["--", sys.executable, "-c", RECORD, self.record()])
        self.assertEqual(rc, 0, err)
        self.assertEqual(Path(self.recorded()[0]).parent, self.tmp)
        self.assertIn("warning", err)
        self.assertIn(str(self.shm), err)
        self.assertNotIn("Traceback", err)

    def test_unwritable_explicit_base_is_a_clear_nonzero_error(self):
        locked = self.work / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        rc, err = self.run_main(["--base", str(locked), "--", sys.executable, "-c", RECORD,
                                 self.record()])
        self.assertNotEqual(rc, 0)
        self.assertIn(str(locked), err)
        self.assertNotIn("Traceback", err)
        self.assertFalse((self.out / "rec").exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_unwritable_fallback_too_is_a_clear_nonzero_error(self):
        os.chmod(self.shm, 0o500)
        os.chmod(self.tmp, 0o500)
        rc, err = self.run_main(["--", sys.executable, "-c", RECORD, self.record()])
        self.assertNotEqual(rc, 0)
        self.assertNotIn("Traceback", err)
        self.assertFalse((self.out / "rec").exists())


class SignalTests(ShmCase):
    def test_sigterm_to_tool_stops_child_removes_root_and_exits_143(self):
        ready = self.out / "ready"
        child = ("import pathlib,sys,time;"
                 "pathlib.Path(sys.argv[1]).write_text('x');time.sleep(60)")
        tool = Path(shm_tests.__file__)
        proc = subprocess.Popen(
            [sys.executable, str(tool), "--base", str(self.shm), "--",
             sys.executable, "-c", child, str(ready)],
            stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 20
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), "child never started")
            self.assertEqual(len(list(self.shm.iterdir())), 1)
            proc.send_signal(signal.SIGTERM)
            _out, err = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, err)
        self.assertEqual(list(self.shm.iterdir()), [], err)


class HelpTests(unittest.TestCase):
    def test_help_warns_ignore_unreadable_can_delete_a_root_still_in_use(self):
        text = " ".join(shm_tests.build_parser().format_help().split())
        self.assertIn("--ignore-unreadable", text)
        self.assertIn("same-uid process", text)
        self.assertIn("older than --older-than-hours", text)


def age(path: Path, hours: float) -> None:
    t = time.time() - hours * 3600
    os.utime(path, (t, t), follow_symlinks=False)


class StaleTests(ShmCase):
    def make(self, name, hours, payload=b"z" * 4096):
        d = self.shm / name
        d.mkdir()
        (d / "f").write_bytes(payload)
        age(d, hours)
        return d

    def clean(self, *extra):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = shm_tests.main(["--clean-stale", "--ignore-unreadable", "--base", str(self.shm), *extra],
                                shm_dir=str(self.shm), tmp_dir=str(self.tmp))
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def test_dry_run_lists_only_old_unused_roots_and_deletes_nothing(self):
        old = self.make("wb-tests.old", 48)
        new = self.make("wb-tests.new", 1)
        other = self.make("other.old", 48)
        text = self.clean()
        self.assertIn(str(old), text)
        self.assertNotIn(str(new), text)
        self.assertNotIn(str(other), text)
        self.assertTrue(old.exists() and new.exists() and other.exists())

    def test_older_than_hours_option(self):
        mid = self.make("wb-tests.mid", 5)
        self.assertNotIn(str(mid), self.clean())
        self.assertIn(str(mid), self.clean("--older-than-hours", "2"))

    def test_apply_removes_only_old_unused_roots(self):
        old = self.make("wb-tests.old", 48)
        ro = self.make("wb-tests.ro", 48)
        os.makedirs(ro / "d")
        (ro / "d" / "f").write_text("x")
        os.chmod(ro / "d", 0o500)
        age(ro, 48)
        new = self.make("wb-tests.new", 1)
        other = self.make("other.old", 48)
        self.clean("--apply")
        self.assertFalse(old.exists())
        self.assertFalse(ro.exists())
        self.assertTrue(new.exists() and other.exists())

    def test_apply_removes_mode_zero_subdir_and_continues_after_a_failure(self):
        bad = self.make("wb-tests.bad", 48)
        os.makedirs(bad / "z")
        (bad / "z" / "f").write_text("x")
        os.chmod(bad / "z", 0)
        age(bad, 48)
        good = self.make("wb-tests.good", 48)
        self.clean("--apply")
        self.assertFalse(bad.exists())
        self.assertFalse(good.exists())

    def test_apply_does_not_abort_when_one_removal_raises(self):
        first = self.make("wb-tests.a", 48)
        second = self.make("wb-tests.b", 48)
        real = shm_tests.remove_root

        def flaky(root):
            if root.endswith("wb-tests.a"):
                raise TypeError("odd")
            real(root)

        out, err = io.StringIO(), io.StringIO()
        with patch.object(shm_tests, "remove_root", flaky), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = shm_tests.main(["--clean-stale", "--ignore-unreadable", "--apply",
                                 "--base", str(self.shm)],
                                shm_dir=str(self.shm), tmp_dir=str(self.tmp))
        self.assertEqual(rc, 1)
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())
        self.assertIn("odd", err.getvalue())

    def test_roots_in_use_by_cwd_or_open_fd_are_skipped(self):
        by_cwd = self.make("wb-tests.cwd", 48)
        by_fd = self.make("wb-tests.fd", 48)
        idle = self.make("wb-tests.idle", 48)
        procs = [
            subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], cwd=by_cwd),
            subprocess.Popen([sys.executable, "-c",
                              "import sys,time;f=open(sys.argv[1]);print('r',flush=True);time.sleep(60)",
                              str(by_fd / "f")], stdout=subprocess.PIPE),
        ]
        try:
            self.assertEqual(procs[1].stdout.readline().strip(), b"r")
            time.sleep(0.2)
            text = self.clean("--apply")
            self.assertTrue(by_cwd.exists() and by_fd.exists())
            self.assertFalse(idle.exists())
            self.assertNotIn(str(by_cwd), text.split("skipped")[0])
        finally:
            for p in procs:
                p.kill()
                p.wait()
                if p.stdout:
                    p.stdout.close()

    def test_symlink_named_like_root_is_never_followed(self):
        victim = self.work / "victim"
        victim.mkdir()
        (victim / "keep").write_text("k")
        link = self.shm / "wb-tests.link"
        link.symlink_to(victim)
        age(link, 48)
        self.clean("--apply")
        self.assertTrue((victim / "keep").exists())
        self.assertTrue(link.is_symlink())

    def test_unreadable_process_makes_root_unverifiable_unless_ignored(self):
        old = self.make("wb-tests.old", 48)
        proc = self.work / "proc"
        (proc / "4242" / "fd").mkdir(parents=True)
        os.chmod(proc / "4242" / "fd", 0)
        self.addCleanup(os.chmod, proc / "4242" / "fd", 0o700)
        stale, skipped = shm_tests.find_stale([str(self.shm)], 24, proc_dir=str(proc))
        self.assertEqual(stale, [])
        self.assertIn("4242", skipped[0][1])
        stale, _ = shm_tests.find_stale([str(self.shm)], 24, proc_dir=str(proc),
                                        ignore_unreadable=True)
        self.assertEqual([r for r, _ in stale], [str(old)])

    def test_fake_proc_cwd_inside_root_blocks_even_when_unreadable_ignored(self):
        old = self.make("wb-tests.old", 48)
        proc = self.work / "proc"
        (proc / "7" / "fd").mkdir(parents=True)
        (proc / "7" / "cwd").symlink_to(old / "sub")
        stale, skipped = shm_tests.find_stale([str(self.shm)], 24, proc_dir=str(proc),
                                              ignore_unreadable=True)
        self.assertEqual(stale, [])
        self.assertIn("in use", skipped[0][1])


if __name__ == "__main__":
    unittest.main()
