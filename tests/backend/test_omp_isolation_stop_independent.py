"""WBX-B12 independent: the graceful stop of the isolation-check OMP (stdin EOF -> SIGTERM -> SIGKILL).

Expected values come from WBX-B12.md / SPEC s3 "공유 OMP 로그인 완화" (EOF first, SIGTERM only after the grace,
SIGKILL only after the SIGTERM wait, group fully gone, bounded) and from the parameters the worker chose, never from
the worker's own tests.  The fake OMP records every catchable signal it receives (SIGKILL cannot be caught: it shows as
return code -9) and its own pid / group child pid with start ticks so leftovers are removed by identity.

Fake flags (joined with ``+``): hang (never answers), ignore_eof (sleeps after EOF), ignore_term (survives SIGTERM),
flood (writes to stdout after EOF), closestdout, child (SIGTERM-proof group member), plainchild (``sleep`` member).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from workbench.backend import cli, launcher

FAKE = r'''#!{python}
import json, os, signal, subprocess, sys, time
flags = set(os.environ["FAKE_MODE"].split("+"))
record = os.environ["FAKE_RECORD"]
marks = record + ".marks"


def note(text):
    with open(marks, "a") as stream:
        stream.write(text + "\n")


def ticks(pid):
    with open(f"/proc/{{pid}}/stat", "rb") as stream:
        return stream.read().rsplit(b") ", 1)[1].split()[19].decode()


def term(signum, _frame):
    note(signal.Signals(signum).name)
    if "ignore_term" not in flags:
        os._exit(143)


for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGUSR1, signal.SIGUSR2):
    signal.signal(sig, term)
child = None
if "child" in flags:  # a group member that shrugs off SIGTERM
    child = subprocess.Popen([sys.executable, "-c",
                              "import signal,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);time.sleep(120)"])
elif "plainchild" in flags:
    child = subprocess.Popen(["sleep", "120"])
with open(record, "w") as stream:
    json.dump({{"pid": os.getpid(), "ticks": ticks(os.getpid()),
               "child": child.pid if child else None, "child_ticks": ticks(child.pid) if child else None}}, stream)
print(json.dumps({{"type": "ready"}}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if "hang" in flags:
        continue
    if request["type"] == "get_state":
        data = {{"systemPrompt": ["You are omp's assistant"], "dumpTools": [], "model": {{"provider": "p", "id": "m"}}}}
    else:
        data = {{"commands": []}}
    print(json.dumps({{"type": "response", "id": request["id"], "command": request["type"],
                      "success": True, "data": data}}), flush=True)
note("eof")
if "closestdout" in flags:
    os.close(1)
if "flood" in flags:
    block = ("x" * 65535 + "\n").encode()
    try:
        for _ in range(10**9 if "ignore_eof" in flags else 64):  # endless, or 4 MiB
            os.write(1, block)
    except OSError:
        pass
if "ignore_eof" in flags:
    time.sleep(120)
time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
note("exited")
sys.exit(0)
'''


def _alive(pid, ticks) -> bool:
    """Same process (pid + start ticks) still running (a zombie counts as gone)."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
    except (OSError, IndexError):
        return False
    return fields[19].decode() == ticks and fields[0] not in {b"Z", b"X"}


class StopCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="b12-stop-")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.fake = self.root / "omp"
        self.fake.write_text(FAKE.format(python=sys.executable))
        self.fake.chmod(0o700)
        self.record = self.root / "record.json"
        self.addCleanup(self.reap_leftovers)

    def reap_leftovers(self):
        """Remove by identity whatever the product left behind (the test asserting it already failed)."""
        try:
            seen = json.loads(self.record.read_text())
        except (OSError, ValueError):
            return
        for pid, ticks in ((seen.get("pid"), seen.get("ticks")), (seen.get("child"), seen.get("child_ticks"))):
            if pid and _alive(pid, ticks):
                os.kill(pid, signal.SIGKILL)

    def fresh(self):
        self.record.unlink(missing_ok=True)
        Path(str(self.record) + ".marks").unlink(missing_ok=True)

    def run_check(self, mode, *, timeout=10.0, cancel=None, delay=None, grace=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin"), "FAKE_MODE": mode, "FAKE_RECORD": str(self.record),
               "HOME": str(self.root / "home")}
        if delay is not None:
            env["FAKE_DELAY"] = str(delay)
        self.fresh()
        shipped = launcher.ISOLATION_STOP_GRACE
        started = time.monotonic()
        with mock.patch.object(launcher, "ISOLATION_STOP_GRACE", shipped if grace is None else grace):
            result = launcher.check_isolation([str(self.fake)], cwd=self.root, environment=env, role="worker",
                                              allowed_skills=(), omp_version="omp/0-test", timeout=timeout,
                                              cancel=cancel)
        self.elapsed = time.monotonic() - started
        return result

    def marks(self):
        try:
            return Path(str(self.record) + ".marks").read_text().split()
        except FileNotFoundError:
            return []

    def assert_group_gone(self, result):
        seen = json.loads(self.record.read_text())
        self.assertFalse(_alive(seen["pid"], seen["ticks"]), "leader survived")
        if seen.get("child"):
            self.assertFalse(_alive(seen["child"], seen["child_ticks"]), "group child survived")
        self.assertEqual(result["cleanup"]["group_left"], [])
        self.assertEqual(result["cleanup"]["state"], "dead")


class GracefulStopTests(StopCase):
    def test_leader_that_exits_within_the_grace_gets_no_signal_and_the_wait_is_for_it(self):
        result = self.run_check("x", delay=1.5, grace=5.0)
        self.assertEqual((result["state"], result["ok"], result["error"]), ("ok", True, None))
        self.assertEqual(self.marks(), ["eof", "exited"])  # no SIGTERM/HUP/INT/... caught
        self.assertEqual(result["cleanup"]["returncode"], 0)  # and not killed
        self.assertGreaterEqual(self.elapsed, 1.5)
        self.assertLess(self.elapsed, 5.0)  # it did not wait the whole grace
        self.assert_group_gone(result)

    def test_leader_exiting_at_once_does_not_pay_the_grace(self):
        result = self.run_check("x", delay=0, grace=5.0)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(self.marks(), ["eof", "exited"])
        self.assertLess(self.elapsed, 2.5)
        self.assert_group_gone(result)

    def test_term_exit_after_the_grace_never_sees_sigkill(self):
        result = self.run_check("ignore_eof", grace=1.0)
        self.assertEqual(self.marks(), ["eof", "SIGTERM"])
        self.assertEqual(result["cleanup"]["returncode"], 143)  # its own exit, not -9
        self.assertGreaterEqual(self.elapsed, 1.0)  # the whole EOF grace was waited first
        self.assertLess(self.elapsed, 1.0 + 3.0 + 2.0)
        self.assert_group_gone(result)

    def test_nothing_is_signalled_before_the_grace_is_over(self):
        stamps = {}
        original = os.killpg

        def spy(pgid, sig):
            stamps.setdefault(sig, time.monotonic())
            return original(pgid, sig)

        began = time.monotonic()
        with mock.patch.object(launcher.os, "killpg", spy):
            self.run_check("ignore_eof", grace=1.5)
        self.assertGreaterEqual(stamps[signal.SIGTERM] - began, 1.5)
        self.assertNotIn(signal.SIGKILL, stamps)

    def test_ignoring_eof_and_sigterm_ends_with_sigkill_inside_the_bound(self):
        result = self.run_check("ignore_eof+ignore_term", grace=1.0)
        self.assertEqual(self.marks(), ["eof", "SIGTERM"])  # SIGTERM was delivered once, then ignored
        self.assertEqual(result["cleanup"]["returncode"], -signal.SIGKILL)
        self.assertGreaterEqual(self.elapsed, 1.0 + 3.0 - 0.2)  # EOF grace, then the SIGTERM wait
        self.assertLess(self.elapsed, 1.0 + 3.0 + 3.0 + 2.0)  # + SIGKILL wait: bounded
        self.assert_group_gone(result)

    def test_group_child_ignoring_sigterm_is_killed_and_nothing_is_left(self):
        result = self.run_check("child", delay=0, grace=1.0)
        self.assertEqual(result["state"], "ok")
        self.assert_group_gone(result)
        self.assertLess(self.elapsed, 1.0 + 3.0 + 3.0 + 2.0)

    def test_plain_group_child_of_a_leader_that_exited_is_terminated(self):
        result = self.run_check("plainchild", delay=0, grace=1.0)
        self.assert_group_gone(result)
        self.assertEqual(self.marks(), ["eof", "exited"])  # the leader itself was not signalled

    def test_a_full_stdout_pipe_does_not_keep_the_leader_from_exiting_in_the_grace(self):
        result = self.run_check("flood", grace=6.0)  # 4 MiB after EOF: only exits if somebody reads
        self.assertEqual(self.marks(), ["eof", "exited"])
        self.assertEqual(result["cleanup"]["returncode"], 0)
        self.assertLess(self.elapsed, 4.0)
        self.assert_group_gone(result)

    def test_endless_flood_and_no_reaction_to_eof_is_still_terminated_within_the_bound(self):
        result = self.run_check("flood+ignore_eof", grace=1.0)
        self.assertEqual(self.marks()[:2], ["eof", "SIGTERM"])
        self.assertLess(self.elapsed, 1.0 + 3.0 + 3.0 + 2.0)
        self.assert_group_gone(result)

    def test_leader_that_closed_stdout_early_is_still_waited_for(self):
        result = self.run_check("closestdout", delay=1.0, grace=5.0)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(self.marks(), ["eof", "exited"])
        self.assertGreaterEqual(self.elapsed, 1.0)
        self.assertLess(self.elapsed, 3.5)
        self.assert_group_gone(result)

    def test_timeout_path_uses_the_same_order(self):
        result = self.run_check("hang", timeout=0.8, grace=5.0)  # never answers; exits at EOF
        self.assertIn("timed out", result["error"])
        self.assertEqual(self.marks(), ["eof", "exited"])  # EOF first, no signal
        self.assertEqual(result["cleanup"]["returncode"], 0)
        self.assert_group_gone(result)

    def test_timeout_path_with_a_leader_that_ignores_eof_gets_term_after_the_grace(self):
        result = self.run_check("hang+ignore_eof", timeout=0.5, grace=1.0)
        self.assertIn("timed out", result["error"])
        self.assertEqual(self.marks(), ["eof", "SIGTERM"])
        self.assertLess(self.elapsed, 0.5 + 1.0 + 3.0 + 2.0)
        self.assert_group_gone(result)

    def test_cancel_path_uses_the_same_order(self):
        cancel = threading.Event()
        timer = threading.Timer(0.5, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        result = self.run_check("hang", timeout=10.0, cancel=cancel, grace=5.0)
        self.assertFalse(result["ok"])
        self.assertEqual(self.marks(), ["eof", "exited"])
        self.assertLess(self.elapsed, 3.0)
        self.assert_group_gone(result)

    def test_verdicts_are_unchanged(self):
        slow = self.run_check("x", delay=0.5, grace=3.0)
        fast = self.run_check("x", delay=0, grace=3.0)
        for key in ("state", "ok", "leaks", "warnings", "error", "extension_errors"):
            self.assertEqual(slow[key], fast[key], key)
        failed = self.run_check("hang", timeout=0.5, grace=2.0)
        self.assertEqual((failed["state"], failed["ok"]), ("failed", False))

    def test_cleanup_reports_the_stage_that_ended_it_and_how_long_it_took(self):
        # WBX-B12.md s.1: `cleanup` carries the finishing stage (eof / sigterm / sigkill) and the elapsed time.
        for mode, stage, kwargs in (("x", "eof", {"delay": 0.3}), ("ignore_eof", "sigterm", {}),
                                    ("ignore_eof+ignore_term", "sigkill", {})):
            with self.subTest(stage=stage):
                cleanup = self.run_check(mode, grace=1.0, **kwargs)["cleanup"]
                self.assertIn(stage, [value for value in cleanup.values() if isinstance(value, str)], cleanup)
                times = [value for key, value in cleanup.items()
                         if isinstance(value, float) and key != "start_ticks"]
                self.assertTrue(times and 0 < max(times) <= self.elapsed + 0.1, cleanup)


class BoundTests(StopCase):
    def test_isolation_wait_covers_both_checks_worst_case(self):
        # worst case of one check: timeout + EOF grace + SIGTERM wait (3) + SIGKILL wait (3); the CLI waits for two
        one = launcher.ISOLATION_CHECK_TIMEOUT + launcher.ISOLATION_STOP_GRACE + 3.0 + 3.0
        self.assertGreaterEqual(cli.ISOLATION_WAIT, 2 * one,
                                f"ISOLATION_WAIT {cli.ISOLATION_WAIT} < manager+worker worst case {2 * one}")

    def test_real_default_grace_totals_inside_the_bound_for_a_leader_that_ignores_everything(self):
        # not mocked: with the shipped constants an OMP ignoring EOF and SIGTERM ends by SIGKILL (~16 s)
        result = self.run_check("ignore_eof+ignore_term", timeout=5.0)
        self.assertGreaterEqual(self.elapsed, launcher.ISOLATION_STOP_GRACE)
        self.assertLess(self.elapsed, launcher.ISOLATION_STOP_GRACE + 3.0 + 3.0 + 2.0)
        self.assertEqual(result["cleanup"]["returncode"], -signal.SIGKILL)
        self.assert_group_gone(result)


if __name__ == "__main__":
    unittest.main()
