"""Requirement-derived developer runner tests; no installed OMP is launched."""

from __future__ import annotations

from contextlib import redirect_stdout
import ctypes
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import dev_validate as runner


ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".workflow/dvl-local"
FIXTURES = "tests.dev_validation.fixture_items_independent.IndependentFixture"


class IndependentRunnerContract(unittest.TestCase):
    def setUp(self) -> None:
        LOCAL.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=LOCAL)
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.omp_placeholder = Path("/usr/bin/true")

    def item(self, name: str, method: str, *, expected_wait: float = 0) -> runner.Item:
        return runner.Item(name, ("DV-AC-02", "DV-AC-04"), (f"{FIXTURES}.{method}",),
                           "fixture", expected_wait_seconds=expected_wait)

    def run_item(self, name: str, method: str, *, idle: float | None = None,
                 expected_wait: float = 0) -> dict:
        item = self.item(name, method, expected_wait=expected_wait)
        if idle is None:
            return runner._run_item(item, 0, self.parent, self.omp_placeholder)
        with patch.object(runner, "SCENARIO_IDLE_SECONDS", idle), patch.object(runner, "CLEANUP_SECONDS", 1):
            return runner._run_item(item, 0, self.parent, self.omp_placeholder)

    def main_report(self, mode: str, item: runner.Item, *, prior: Path | None = None,
                    changed_input: bool = False, short_target: bool = False) -> tuple[int, dict]:
        report_dir = self.parent / f"report-{len(list(self.parent.iterdir()))}"
        report_dir.mkdir(mode=0o700)
        argv = ["--mode", mode, "--report-dir", str(report_dir), "--omp", str(self.omp_placeholder)]
        if prior is not None:
            argv += ["--prior-report", str(prior)]
        inputs = [{"sha256": "a" * 64, "fileCount": 1, "files": [{"path": "fixture", "sha256": "a" * 64}]},
                  {"sha256": "b" * 64, "fileCount": 1, "files": [{"path": "fixture", "sha256": "b" * 64}]}]
        versions = {"omp": "fixture 1", "python": "3.12", "kernel": "fixture"}
        targets = {"fast": 0, "precise": 600} if short_target else runner.TARGET_SECONDS
        capture = io.StringIO()
        with patch.object(runner, "FAST_ITEMS", (item,)), \
             patch.object(runner, "_input_manifest", side_effect=inputs if changed_input else [inputs[0], inputs[0]]), \
             patch.object(runner.harness, "tool_versions", return_value=versions), \
             patch.object(runner, "TARGET_SECONDS", targets), redirect_stdout(capture):
            exit_code = runner.main(argv)
        self.assertTrue(capture.getvalue().strip())
        return exit_code, json.loads((report_dir / "report.json").read_text())

    def test_modes_targets_pending_and_overrun_without_cutoff(self) -> None:
        self.assertEqual(runner.TARGET_SECONDS, {"fast": 120, "precise": 600})
        fixture = self.item("fixture-pass", "test_pass")
        fast_exit, fast = self.main_report("fast", fixture, short_target=True)
        self.assertEqual((fast_exit, fast["result"], fast["targetExceeded"]), (0, "passed", True))
        self.assertGreater(fast["elapsedSeconds"], fast["targetSeconds"])
        precise_exit, precise = self.main_report("precise", fixture)
        self.assertEqual((precise_exit, precise["result"]), (2, "inconclusive"))
        self.assertTrue(precise["firstFailure"])
        self.assertTrue(any(row["required"] and row["status"] == "not_run"
                            for row in precise["items"]))
        self.assertEqual(precise["scope"], "developer fast subset; not product compatibility pass")

    def test_zero_and_fully_skipped_collections_never_pass(self) -> None:
        empty = runner.Item("empty", ("DV-AC-04",),
                            ("tests.dev_validation.fixture_items.EmptyFixture",), "fixture")
        zero = runner._run_item(empty, 0, self.parent, self.omp_placeholder)
        skipped = self.run_item("all-skipped", "test_skip")
        for result in (zero, skipped):
            self.assertEqual(result["status"], "inconclusive")
            self.assertEqual(result["failureClass"], "collection")
            self.assertTrue(result["cleanup"]["namespaceRemoved"])
        self.assertEqual(zero["collectedTests"], 0)
        self.assertEqual((skipped["collectedTests"], skipped["skippedTests"]), (1, 1))

    def test_silent_watchdog_is_not_skip_or_success_and_expected_wait_is_separate(self) -> None:
        timed = self.run_item("silent", "test_silent_stall", idle=0.25)
        self.assertEqual((timed["status"], timed["failureClass"]), ("inconclusive", "watchdog"))
        self.assertEqual((timed["collectedTests"], timed["skippedTests"]), (0, 0))
        self.assertIn("watchdog", timed["reason"])
        self.assertTrue(timed["cleanup"]["namespaceRemoved"])
        waited = self.run_item("expected-wait", "test_silent_stall", idle=0.25, expected_wait=3)
        self.assertEqual((waited["status"], waited["collectedTests"]), ("passed", 1))
        self.assertEqual(waited["expectedWaitSeconds"], 3)

    def test_hostile_parent_values_absent_from_actual_child_and_both_streams_bounded(self) -> None:
        hostile = {"OPENAI_API_KEY": "PRIVATE-KEY-MARKER", "ANTHROPIC_API_KEY": "PRIVATE-KEY-MARKER",
                   "HOME": "/tmp/private-user-home", "TMUX": "PRIVATE-TMUX-MARKER",
                   "HERDR_SOCKET": "/tmp/private-herdr", "WORKBENCH_DATA_DIR": "/tmp/private-workbench",
                   "PI_CONFIG_DIR": "/tmp/private-pi", "SSH_AUTH_SOCK": "/tmp/private-ssh"}
        with patch.dict(os.environ, hostile):
            safe = self.run_item("hostile-env", "test_hostile_environment_is_replaced")
        self.assertEqual((safe["status"], safe["collectedTests"]), ("passed", 1))
        self.assertTrue(safe["cleanup"]["namespaceRemoved"])
        flood = self.run_item("bounded", "test_bounded_both_streams")
        self.assertEqual((flood["status"], flood["collectedTests"]), ("passed", 1))
        self.assertLessEqual(len(flood["stdoutTail"].encode()), runner.CAPTURE_BYTES)
        self.assertLessEqual(len(flood["stderrTail"].encode()), runner.CAPTURE_BYTES)
        self.assertGreater(flood["stdoutDroppedBytes"], 0)
        self.assertGreater(flood["stderrDroppedBytes"], 0)
        self.assertIn("STDOUT-END", flood["stdoutTail"])
        self.assertIn("STDERR-END", flood["stderrTail"])

    def test_identity_change_and_first_failure_attempt_chain_are_not_accepted_as_pass(self) -> None:
        changed_exit, changed = self.main_report("fast", self.item("fixture-pass", "test_pass"),
                                                  changed_input=True)
        self.assertEqual((changed_exit, changed["result"]), (2, "inconclusive"))
        self.assertNotEqual(changed["inputBefore"]["sha256"], changed["inputAfter"]["sha256"])
        self.assertEqual(changed["firstFailure"]["item"], "input_identity")
        first_exit, first = self.main_report("fast", self.item("fixture-fail", "test_fail"))
        self.assertEqual((first_exit, first["result"]), (1, "failed"))
        prior = Path(first["environment"]["privateNamespace"]) / "report.json"
        retry_exit, retry = self.main_report("fast", self.item("fixture-pass", "test_pass"), prior=prior)
        self.assertEqual((retry_exit, retry["result"]), (1, "failed"))
        self.assertEqual(retry["firstFailure"], first["firstFailure"])
        self.assertEqual([row["attemptId"] for row in retry["attemptChain"]],
                         [first["attemptId"], retry["attemptId"]])
        self.assertEqual(retry["items"][0]["status"], "passed")

    def test_spawn_failure_cleans_created_private_namespace(self) -> None:
        real_mkdtemp = tempfile.mkdtemp
        created: list[Path] = []

        def owned_namespace(*, prefix: str, dir: str) -> str:
            path = Path(real_mkdtemp(prefix=prefix, dir=self.parent))
            created.append(path)
            return str(path)

        with patch.object(runner.tempfile, "mkdtemp", side_effect=owned_namespace), \
             patch.object(runner.subprocess, "Popen", side_effect=OSError("injected spawn failure")):
            try:
                outcome = runner._run_item(self.item("spawn-failure", "test_pass"), 0,
                                           self.parent, self.omp_placeholder)
            except OSError:
                outcome = None
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists(), "spawn failure left a private namespace behind")
        self.assertIsNotNone(outcome, "spawn failure escaped without a reportable inconclusive item")
        self.assertEqual((outcome["status"], outcome["failureClass"]),
                         ("inconclusive", "probe_environment"))

    def test_chatter_is_not_readiness_progress(self) -> None:
        result = self.run_item("chatter", "test_chatter_without_predicate", idle=0.25)
        self.assertEqual((result["status"], result["failureClass"]), ("inconclusive", "watchdog"))
        self.assertTrue(result["cleanup"]["namespaceRemoved"])

    def test_descendant_holding_pipes_is_stopped_before_report(self) -> None:
        result = self.run_item("pipe-child", "test_descendant_holds_pipes_without_namespace_in_argv",
                               idle=0.3)
        match = re.search(r"OWNED_CHILD_PID=(\d+)", result["stdoutTail"])
        self.assertIsNotNone(match, result)
        child_pid = int(match.group(1))
        ticks = runner.harness.ticks(child_pid)
        try:
            self.assertEqual((result["status"], result["failureClass"]),
                             ("inconclusive", "watchdog"))
            self.assertFalse(runner.harness.alive(child_pid, ticks),
                             "owned pipe-holding child remained alive after runner reported cleanup")
            self.assertTrue(result["cleanup"]["namespaceRemoved"])
        finally:
            if ticks is not None:
                runner.harness.kill_exact(child_pid, ticks)

    def test_fake_import_summary_does_not_turn_zero_collection_into_pass(self) -> None:
        item = runner.Item("fake-zero", ("DV-AC-04",),
                           ("tests.dev_validation.fixture_items_independent.NoCollectedTests",),
                           "fixture", env={"WB_DVL_SPOOF": "zero"})
        result = runner._run_item(item, 0, self.parent, self.omp_placeholder)
        self.assertEqual((result["collectedTests"], result["status"], result["failureClass"]),
                         (0, "inconclusive", "collection"))
        self.assertTrue(result["cleanup"]["namespaceRemoved"])

    def test_fake_import_skip_count_does_not_turn_full_skip_into_pass(self) -> None:
        item = runner.Item("fake-skip", ("DV-AC-04",),
                           (f"{FIXTURES}.test_skip",), "fixture",
                           env={"WB_DVL_SPOOF": "skipped"})
        result = runner._run_item(item, 0, self.parent, self.omp_placeholder)
        self.assertEqual((result["collectedTests"], result["skippedTests"], result["status"]),
                         (1, 1, "inconclusive"))
        self.assertTrue(result["cleanup"]["namespaceRemoved"])

    def test_spoofed_stdout_stderr_unittest_markers_are_not_progress(self) -> None:
        result = self.run_item("fake-progress", "test_fake_unittest_progress_without_completed_assertions",
                               idle=0.25)
        self.assertEqual((result["status"], result["failureClass"]),
                         ("inconclusive", "watchdog"))
        self.assertTrue(result["cleanup"]["namespaceRemoved"])

    def test_detached_devnull_helper_is_owned_and_stopped_before_pass_report(self) -> None:
        result = self.run_item("detached-helper", "test_detached_devnull_helper_outside_namespace")
        match = re.search(r"DETACHED_CHILD_PID=(\d+)", result["stdoutTail"])
        self.assertIsNotNone(match, result)
        child_pid = int(match.group(1))
        ticks = runner.harness.ticks(child_pid)
        try:
            self.assertFalse(runner.harness.alive(child_pid, ticks),
                             "normal detached owned helper outlived runner's pass report")
            self.assertTrue(result["cleanup"]["namespaceRemoved"])
        finally:
            if ticks is not None:
                runner.harness.kill_exact(child_pid, ticks)

    def test_environment_setup_failure_cleans_namespace_before_reporting(self) -> None:
        real_mkdtemp = tempfile.mkdtemp
        real_base_env = runner._base_env
        created: list[Path] = []

        def owned_namespace(*, prefix: str, dir: str) -> str:
            path = Path(real_mkdtemp(prefix=prefix, dir=self.parent))
            created.append(path)
            return str(path)

        def fail_after_setup(home: Path, namespace: Path, omp: Path, item: runner.Item) -> dict:
            real_base_env(home, namespace, omp, item)
            raise OSError("injected environment setup failure")

        with patch.object(runner.tempfile, "mkdtemp", side_effect=owned_namespace), \
             patch.object(runner, "_base_env", side_effect=fail_after_setup):
            try:
                outcome = runner._run_item(self.item("setup-failure", "test_pass"), 0,
                                           self.parent, self.omp_placeholder)
            except OSError:
                outcome = None
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists(), "environment setup failure left a private namespace")
        self.assertIsNotNone(outcome, "setup failure escaped without reportable inconclusive item")
        self.assertEqual((outcome["status"], outcome["failureClass"]),
                         ("inconclusive", "probe_environment"))

    def test_product_doublefork_backend_is_stopped_without_signalling_unrelated_helper(self) -> None:
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, cwd="/", start_new_session=True,
                                     env=runner.harness.clean_base_env())
        unrelated_ticks = runner.harness.ticks(unrelated.pid)
        created: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp
        backend_pid = backend_ticks = None

        def owned_namespace(*, prefix: str, dir: str) -> str:
            path = Path(real_mkdtemp(prefix=prefix, dir=LOCAL))
            created.append(path)
            return str(path)

        try:
            with patch.object(runner.tempfile, "mkdtemp", side_effect=owned_namespace):
                result = self.run_item("product-doublefork", "test_product_doublefork_backend_waits_without_progress",
                                       idle=1.0)
            self.assertEqual(len(created), 1)
            receipt = re.search(r"BACKEND_READY_PID=(\d+) START_TICKS=(\d+)", result["stdoutTail"])
            self.assertIsNotNone(receipt, result)
            backend_pid, backend_ticks = map(int, receipt.groups())
            self.assertEqual(Path(result["namespace"]), created[0])
            self.assertTrue(runner.harness.alive(unrelated.pid, unrelated_ticks),
                            "unrelated helper was signalled during owned cleanup")
            self.assertFalse(runner.harness.alive(backend_pid, backend_ticks),
                             "owned product double-fork backend survived driver watchdog")
            self.assertEqual((result["status"], result["failureClass"]),
                             ("inconclusive", "watchdog"))
            self.assertTrue(result["cleanup"]["namespaceRemoved"])
        finally:
            if backend_pid is None:
                for namespace in created:
                    ready = namespace / "backend.ready"
                    if ready.is_file():
                        backend_pid, backend_ticks = map(int, ready.read_text().split())
                        break
            if backend_pid is not None and backend_ticks is not None:
                runner.harness.kill_exact(backend_pid, backend_ticks)
                deadline = runner.time.monotonic() + 1
                while runner.harness.alive(backend_pid, backend_ticks) and runner.time.monotonic() < deadline:
                    runner.time.sleep(0.02)
            for namespace in created:
                if namespace.exists():
                    shutil.rmtree(namespace)
            if unrelated_ticks is not None:
                runner.harness.kill_exact(unrelated.pid, unrelated_ticks)
            try:
                unrelated.wait(timeout=1)
            except subprocess.TimeoutExpired:
                unrelated.kill()
                unrelated.wait(timeout=1)

    def test_cli_exec_doublefork_backend_is_stopped_without_signalling_unrelated_helper(self) -> None:
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, cwd="/", start_new_session=True,
                                     env=runner.harness.clean_base_env())
        unrelated_ticks = runner.harness.ticks(unrelated.pid)
        created: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp
        backend_pid = backend_ticks = None

        def owned_namespace(*, prefix: str, dir: str) -> str:
            path = Path(real_mkdtemp(prefix=prefix, dir=LOCAL))
            created.append(path)
            return str(path)

        try:
            with patch.object(runner.tempfile, "mkdtemp", side_effect=owned_namespace):
                result = self.run_item("cli-doublefork", "test_cli_exec_doublefork_backend_waits_without_progress",
                                       idle=1.0)
            self.assertEqual(len(created), 1)
            receipt = re.search(r"CLI_BACKEND_READY_PID=(\d+) START_TICKS=(\d+)", result["stdoutTail"])
            self.assertIsNotNone(receipt, result)
            backend_pid, backend_ticks = map(int, receipt.groups())
            self.assertEqual(Path(result["namespace"]), created[0])
            self.assertTrue(runner.harness.alive(unrelated.pid, unrelated_ticks),
                            "unrelated helper was signalled during CLI backend cleanup")
            self.assertFalse(runner.harness.alive(backend_pid, backend_ticks),
                             "owned CLI-exec double-fork backend survived driver watchdog")
            self.assertEqual((result["status"], result["failureClass"]),
                             ("inconclusive", "watchdog"))
            self.assertTrue(result["cleanup"]["namespaceRemoved"])
        finally:
            if backend_pid is None:
                for namespace in created:
                    ready = namespace / "cli-backend.ready"
                    if ready.is_file():
                        backend_pid, backend_ticks = map(int, ready.read_text().split())
                        break
            if backend_pid is not None and backend_ticks is not None:
                runner.harness.kill_exact(backend_pid, backend_ticks)
                deadline = runner.time.monotonic() + 1
                while runner.harness.alive(backend_pid, backend_ticks) and runner.time.monotonic() < deadline:
                    runner.time.sleep(0.02)
            for namespace in created:
                if namespace.exists():
                    shutil.rmtree(namespace)
            if unrelated_ticks is not None:
                runner.harness.kill_exact(unrelated.pid, unrelated_ticks)
            try:
                unrelated.wait(timeout=1)
            except subprocess.TimeoutExpired:
                unrelated.kill()
                unrelated.wait(timeout=1)

    def test_early_native_zombie_is_exactly_reaped_without_touching_baseline_children(self) -> None:
        libc = ctypes.CDLL(None, use_errno=True)

        def subreaper_state() -> int:
            value = ctypes.c_int()
            self.assertEqual(libc.prctl(runner._PR_GET_CHILD_SUBREAPER,
                                        ctypes.byref(value), 0, 0, 0), 0)
            return value.value

        prior_subreaper = subreaper_state()
        unrelated_live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, cwd="/", start_new_session=True,
                                          env=runner.harness.clean_base_env())
        live_ticks = runner.harness.ticks(unrelated_live.pid)
        unrelated_z = subprocess.Popen([sys.executable, "-c", "pass"],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, cwd="/", start_new_session=True,
                                       env=runner.harness.clean_base_env())
        z_ticks = runner.harness.ticks(unrelated_z.pid)
        created: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp
        backend_pid = backend_ticks = None

        def owned_namespace(*, prefix: str, dir: str) -> str:
            path = Path(real_mkdtemp(prefix=prefix, dir=LOCAL))
            created.append(path)
            return str(path)

        try:
            deadline = runner.time.monotonic() + 2
            z_fields = runner.harness.stat_fields(unrelated_z.pid)
            while (z_fields is None or z_fields[0] != b"Z") and runner.time.monotonic() < deadline:
                runner.time.sleep(0.01)
                z_fields = runner.harness.stat_fields(unrelated_z.pid)
            self.assertIsNotNone(z_fields)
            self.assertEqual((z_fields[0], int(z_fields[1]), runner.harness.ticks(unrelated_z.pid)),
                             (b"Z", os.getpid(), z_ticks), "unrelated baseline child was not Z")
            with patch.object(runner.tempfile, "mkdtemp", side_effect=owned_namespace):
                result = self.run_item("early-native-z", "test_cli_exec_backend_is_zombie_before_suite_completion")
            receipt = re.search(r"EARLY_NATIVE_Z_PID=(\d+) START_TICKS=(\d+)", result["stdoutTail"])
            self.assertIsNotNone(receipt, result)
            backend_pid, backend_ticks = map(int, receipt.groups())
            self.assertEqual((result["status"], result["collectedTests"], result["skippedTests"]),
                             ("passed", 1, 0), result)
            self.assertIn(backend_pid, result["cleanup"]["adoptedZombiePids"])
            self.assertIn(backend_pid, result["cleanup"]["reapedPids"])
            self.assertIsNone(runner.harness.stat_fields(backend_pid),
                              "owned Z is non-alive but still occupies /proc until exact waitpid")
            self.assertNotIn(unrelated_live.pid, result["cleanup"].get("signalledPids", []))
            self.assertNotIn(unrelated_z.pid, result["cleanup"].get("reapedPids", []))
            self.assertTrue(runner.harness.alive(unrelated_live.pid, live_ticks))
            self.assertEqual(runner.harness.stat_fields(unrelated_z.pid)[0], b"Z")
            self.assertTrue(result["cleanup"]["namespaceRemoved"])
            self.assertEqual(subreaper_state(), prior_subreaper)
        finally:
            if backend_pid is None:
                for namespace in created:
                    ready = namespace / "early-exit.ready"
                    if ready.is_file():
                        backend_pid, backend_ticks = map(int, ready.read_text().split())
                        break
            if backend_pid is not None and backend_ticks is not None:
                if runner.harness.ticks(backend_pid) == backend_ticks:
                    runner.harness.kill_exact(backend_pid, backend_ticks)
                    deadline = runner.time.monotonic() + 1
                    while (runner.harness.ticks(backend_pid) == backend_ticks
                           and runner.time.monotonic() < deadline):
                        try:
                            os.waitpid(backend_pid, os.WNOHANG)
                        except ChildProcessError:
                            break
                        runner.time.sleep(0.01)
            for namespace in created:
                if namespace.exists():
                    shutil.rmtree(namespace)
            if live_ticks is not None:
                runner.harness.kill_exact(unrelated_live.pid, live_ticks)
            try:
                unrelated_live.wait(timeout=1)
            except subprocess.TimeoutExpired:
                unrelated_live.kill()
                unrelated_live.wait(timeout=1)
            try:
                unrelated_z.wait(timeout=1)
            except ChildProcessError:
                pass


if __name__ == "__main__":
    unittest.main()
