"""Requirement-derived developer runner tests; no installed OMP is launched."""

from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import ast
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
from types import SimpleNamespace
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
        # Executable feature IDs use ordinary skipped unit fixtures here, never
        # their Native/300-second/volume entrypoints. Unknowns still aggregate.
        feature_items = tuple(runner.Item(key, ("DV-AC-04",), (f"{FIXTURES}.test_skip",), "fixture")
                              for key in ("new_storage_summary_raw_product_gates", "q01_product_adapter_recheck",
                                          "model_metadata", "product_full_dvl14"))
        capture = io.StringIO()
        with patch.object(runner, "FAST_ITEMS", (item,)), \
             patch.object(runner, "PRECISE_ITEMS", ()), \
             patch.object(runner, "FEATURE_ITEMS", feature_items), \
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
        self.assertTrue(any(row["required"] and row["status"] == "inconclusive"
                            for row in precise["items"]))
        self.assertEqual(precise["scope"],
                         "developer core Bash/sh precise + explicit owned feature runtime items; completion requires full gate evidence")
        self.assertEqual(precise["firstFailure"]["item"], "new_storage_summary_raw_product_gates")
        self.assertEqual({row["id"] for row in precise["items"]},
                         {"fixture-pass", "q01_product_adapter_recheck", "new_storage_summary_raw_product_gates",
                          "model_metadata", "product_full_dvl14"})
        self.assertEqual(precise["matrix"]["testIds"], [])
        for row in precise["items"][1:]:
            self.assertEqual((row["status"], row["collectedTests"], row["skippedTests"]),
                             ("inconclusive", 1, 1))

    def test_module_entry_registry_preserves_core_and_matches_parallel_selection(self) -> None:
        core_keys = ("harness_fixture", "launcher_skill_filter", "installed_plain_bash",
            "compat_plain_bash", "compat_plain_dash", "compat_tmux_bash", "compat_tmux_dash",
            "compat_herdr_bash", "compat_herdr_dash", "shell_selection", "review_busy_end",
            "pause_end_no_replay", "pause_interrupts_manager_turn", "restart_worker_survivor_host",
            "detach_active_work", "backend_restart_no_replay", "scripted_model_fault_holds",
            "shutdown_confirmation", "core_metadata_raw_faults", "takeover_restart_no_replay")
        feature_keys = ("new_storage_summary_raw_product_gates", "q01_product_adapter_recheck",
                        "model_metadata", "product_full_dvl14")
        selected = runner.FAST_ITEMS + runner.PRECISE_ITEMS + runner.FEATURE_ITEMS
        self.assertEqual(tuple(item.key for item in selected), core_keys + feature_keys)
        self.assertEqual(len({item.key for item in selected}), 24)
        before = [deepcopy(vars(item)) for item in selected]
        tree = ast.parse(Path(runner.__file__).read_text())
        entry, = [node for node in tree.body if isinstance(node, ast.If)
                  and any(isinstance(part, ast.Name) and part.id == "__name__"
                          for part in ast.walk(node.test))]
        branch = compile(ast.Module(body=[entry], type_ignores=[]), runner.__file__, "exec")
        calls = []
        def cell(*args):
            calls.append(args)
            return 79  # AST-controlled dispatch receipt, not a scenario result.
        for index, item in enumerate(selected):
            with self.subTest(item=item.key):
                argv = ["tools.dev_validate", "--_cell-worker", item.key, str(index),
                    "owned-entry-invocation", "owned-entry-nonce", str(os.getpid()), "1", "41", "42",
                    str(self.parent / "namespace"), str(self.parent), str(self.omp_placeholder)]
                namespace = {**vars(runner), "__name__": "__main__", "sys": SimpleNamespace(argv=argv),
                             "_cell_worker_main": cell}
                with patch("subprocess.Popen", side_effect=AssertionError("AST entry must not launch")):
                    with self.assertRaises(SystemExit) as ended:
                        exec(branch, namespace)
                self.assertEqual(ended.exception.code, 79)
                self.assertIs(calls[-1][0], item)
                self.assertEqual(calls[-1][1:], (index, "owned-entry-invocation", "owned-entry-nonce",
                    os.getpid(), 1, 41, 42, self.parent / "namespace", self.parent, self.omp_placeholder))
                self.assertEqual(tuple(namespace["registry"]), core_keys + feature_keys)
        scheduled = []
        batches = []
        def batch(items, *args):
            batches.append([item.key for _, item in items])
            scheduled.extend(items)
            return [{"id": item.key, "status": "inconclusive"} for _, item in items], False
        with patch.object(runner, "_run_cell_batch", side_effect=batch):
            rows = runner._parallel_items(runner.PRECISE_ITEMS + runner.FEATURE_ITEMS,
                                         self.parent, self.omp_placeholder, "owned-entry", 3)
        self.assertEqual([index for index, _ in scheduled], list(range(3, 24)))
        self.assertEqual([row["id"] for row in rows], list((core_keys + feature_keys)[3:]))
        self.assertEqual([keys for keys in batches if len(keys) == 2],
                         [["compat_tmux_bash", "compat_tmux_dash"]])
        for (_, actual), expected in zip(scheduled, selected[3:]):
            self.assertIs(actual, expected)
        self.assertEqual([vars(item) for item in selected], before)
        print("module_entry_ast=" + json.dumps({"coreKeys": core_keys, "featureKeys": feature_keys,
            "parallelKeys": [row["id"] for row in rows], "boundary": "actual_entry_AST_external_cell_launch_double"}))

    def test_fresh_module_entry_transports_feature_refusals_and_rejects_unknown_key(self) -> None:
        # run_module executes the real complete module as __main__ in a fresh
        # interpreter. Only a downstream Popen is prohibited at its external
        # boundary; the real worker/parent guard, kernel readback and pipes run.
        bootstrap = """
import runpy, subprocess, sys
def no_scenario_process(*args, **kwargs):
    raise AssertionError('fixture forbids downstream driver/Native launch')
subprocess.Popen = no_scenario_process
sys.argv = ['tools.dev_validate', *sys.argv[1:]]
runpy.run_module('tools.dev_validate', run_name='__main__')
"""
        features = ("new_storage_summary_raw_product_gates", "q01_product_adapter_recheck",
                    "model_metadata", "product_full_dvl14")
        receipts = []
        for index, key in enumerate((*features, "unregistered-owned-fixture")):
            with self.subTest(item=key):
                control_read, control_write = os.pipe()
                result_read, result_write = os.pipe()
                child = None
                try:
                    argv = [sys.executable, "-B", "-c", bootstrap, "--_cell-worker", key, str(index),
                        "owned-process-invocation", "owned-process-nonce", str(os.getpid()), "0",
                        str(control_write), str(result_write), str(self.parent / "unused-namespace"),
                        str(self.parent), str(self.omp_placeholder)]
                    child = subprocess.Popen(argv, cwd=ROOT, env=os.environ.copy(),
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        pass_fds=(control_write, result_write), start_new_session=True)
                    os.close(control_write); control_write = None
                    os.close(result_write); result_write = None
                    stdout, stderr = child.communicate(timeout=10)
                    raw = os.read(result_read, 65_537)
                    self.assertEqual(os.read(control_read, 65_537), b"")
                    self.assertEqual(stdout, b"")
                    if key not in features:
                        self.assertNotEqual(child.returncode, 0)
                        self.assertEqual(raw, b"")
                        receipts.append({"item": key, "exitCode": child.returncode, "envelope": False})
                        continue
                    self.assertEqual((child.returncode, stderr), (0, b""))
                    envelope = json.loads(raw)
                    self.assertEqual((envelope["item"], envelope["invocation"], envelope["nonce"]),
                                     (key, "owned-process-invocation", "owned-process-nonce"))
                    self.assertEqual(envelope["workerPid"], child.pid)
                    self.assertGreater(envelope["workerStartTicks"], 0)
                    row = envelope["result"]
                    self.assertEqual((row["id"], row["status"], row["collectedTests"], row["skippedTests"]),
                                     (key, "inconclusive", 0, 0))
                    self.assertIn("parent identity changed before start", row["reason"])
                    self.assertEqual(row["cleanup"], {"remaining": [], "namespaceRemoved": None})
                    self.assertFalse((self.parent / "unused-namespace").exists())
                    self.assertFalse(Path(f"/proc/{child.pid}").exists())
                    receipts.append({"item": key, "exitCode": child.returncode,
                        "workerPid": child.pid, "workerStartTicks": envelope["workerStartTicks"],
                        "status": row["status"], "collected": 0})
                finally:
                    if child is not None and child.poll() is None:
                        child.kill()
                        child.communicate(timeout=2)
                    for fd in (control_read, control_write, result_read, result_write):
                        if fd is not None: os.close(fd)
        print("module_entry_real_process=" + json.dumps({"receipts": receipts,
            "boundary": "fresh_python_actual_module_entry_IPC_parent_refusal_not_scenario_execution"}))

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

    def test_quiet_driver_observes_owned_orphan_group_absent_before_exit(self) -> None:
        # The external driver is ordinary Python. Its private orphan must be
        # reaped while both driver streams are quiet, not in final cleanup.
        receipt = self.parent / "quiet-orphans.json"
        observed = self.parent / "quiet-observed.json"
        code = r'''
import json, os, pathlib, sys, time
receipt, observed, namespace = map(pathlib.Path, sys.argv[1:4])
control_fd = int(sys.argv[4])
def fields(pid):
    try: return pathlib.Path('/proc/%s/stat' % pid).read_text().rsplit(') ', 1)[1].split()
    except FileNotFoundError: return None
broker = os.fork()
if broker == 0:
    os.close(control_fd)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    os.close(null)
    os.setsid()
    zombie = os.fork()
    if zombie == 0:
        time.sleep(.15)
        os._exit(0)
    live = os.fork()
    if live == 0:
        os.setsid()
        os.execv(sys.executable, [sys.executable, '-c', 'import time; time.sleep(20)', str(namespace)])
    until = time.monotonic() + 1
    while int(fields(live)[2]) != live and time.monotonic() < until: time.sleep(.001)
    rows = {str(pid): {'pid': pid, 'startTicks': int(fields(pid)[19]),
        'pgrp': int(fields(pid)[2]), 'session': int(fields(pid)[3])} for pid in (zombie, live)}
    receipt.write_text(json.dumps({'zombie': rows[str(zombie)], 'live': rows[str(live)]}))
    os._exit(0)
os.waitpid(broker, 0)
until = time.monotonic() + 4
while not receipt.exists() and time.monotonic() < until: time.sleep(.005)
rows = json.loads(receipt.read_text())
while fields(rows['zombie']['pid']) is not None and time.monotonic() < until: time.sleep(.01)
live = fields(rows['live']['pid'])
try:
    os.killpg(rows['zombie']['pgrp'], 0)
    group_absent = False
except ProcessLookupError:
    group_absent = True
observed.write_text(json.dumps({'driver': os.getpid(), 'driverExitAfterNs': time.monotonic_ns(),
    'groupAbsent': group_absent, 'zombieAbsent': fields(rows['zombie']['pid']) is None,
    'liveStillRunning': live is not None and live[0] != 'Z'}))
'''
        real_popen = subprocess.Popen
        unrelated = real_popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                              stdin=subprocess.PIPE, start_new_session=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        baseline = real_popen([sys.executable, "-c", "pass"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        unrelated_ticks = runner.harness.ticks(unrelated.pid)
        try:
            deadline = runner.time.monotonic() + 2
            while runner.time.monotonic() < deadline:
                fields = runner.harness.stat_fields(baseline.pid)
                if fields is not None and fields[0] == b"Z": break
                runner.time.sleep(.005)
            self.assertEqual(runner.harness.stat_fields(baseline.pid)[0], b"Z")
            def launch(argv, **kwargs):
                return real_popen([sys.executable, "-B", "-c", code, str(receipt), str(observed),
                                   kwargs["env"]["WB_DVL_NAMESPACE"], str(kwargs["pass_fds"][0])], **kwargs)
            with patch.object(runner.subprocess, "Popen", side_effect=launch):
                result = self.run_item("quiet-orphans", "test_pass")
            rows, view = json.loads(receipt.read_text()), json.loads(observed.read_text())
            self.assertTrue(view["zombieAbsent"], result)
            self.assertTrue(view["groupAbsent"], result)
            self.assertTrue(view["liveStillRunning"], result)
            zombie, live = rows["zombie"], rows["live"]
            self.assertIn(zombie["pid"], result["cleanup"]["monitorReapedPids"])
            readback = next(row for row in result["cleanup"]["adoptedProcessReadback"]
                            if row["pid"] == zombie["pid"])
            for key in ("pid", "startTicks", "pgrp", "session"):
                self.assertEqual(readback[key], zombie[key])
            self.assertEqual(readback["ppid"], os.getpid())
            self.assertLess(readback["observedMonotonicNs"], view["driverExitAfterNs"])
            self.assertNotIn(live["pid"], result["cleanup"]["monitorReapedPids"])
            self.assertNotIn(view["driver"], result["cleanup"]["monitorReapedPids"])
            self.assertNotIn(baseline.pid, result["cleanup"]["reapedPids"])
            self.assertNotIn(unrelated.pid, result["cleanup"]["signalledPids"])
            self.assertEqual(runner.harness.stat_fields(baseline.pid)[0], b"Z")
            self.assertTrue(runner.harness.alive(unrelated.pid, unrelated_ticks))
            self.assertEqual(result["cleanup"]["remaining"], [])
            self.assertTrue(result["cleanup"]["namespaceRemoved"])
            print("quiet_owned_readback=" + json.dumps({"captured": rows, "observed": view,
                "readback": readback, "monitorReapedPids": result["cleanup"]["monitorReapedPids"]}, sort_keys=True))
        finally:
            if receipt.exists():
                for row in json.loads(receipt.read_text()).values():
                    if runner.harness.ticks(row["pid"]) == row["startTicks"]:
                        runner.harness.kill_exact(row["pid"], row["startTicks"])
                    try: os.waitpid(row["pid"], 0)
                    except ChildProcessError: pass
            unrelated.stdin.close()
            unrelated.wait(timeout=2)
            baseline.wait(timeout=2)

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
