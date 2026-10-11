"""Failure-preserving, bounded registry/report fixture tests (no installed OMP)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import dev_validate as runner


class RunnerContractTests(unittest.TestCase):
    def test_normal_cross_exec_backend_zombie_is_reaped_without_unrelated_child(self):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        def subreaper_state():
            value = ctypes.c_int()
            self.assertEqual(libc.prctl(runner._PR_GET_CHILD_SUBREAPER,
                                        ctypes.byref(value), 0, 0, 0), 0)
            return value.value

        prior = subreaper_state()
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, cwd="/", start_new_session=True,
                                     env=runner.harness.clean_base_env())
        unrelated_start = runner.harness.ticks(unrelated.pid)
        try:
            with tempfile.TemporaryDirectory() as root:
                item = runner.Item("normal-cross-exec", ("DV-AC-04",),
                                   ("tests.dev_validation.fixture_items.RunnerFixture."
                                    "test_cross_exec_backend_finishes_before_suite",), "fixture")
                result = runner._run_item(item, 0, Path(root), Path("/usr/bin/true"))
            receipt = re.search(r"BACKEND_EXITED_PID=(\d+) (\d+)", result["stdoutTail"])
            self.assertIsNotNone(receipt, result)
            backend_pid, backend_start = map(int, receipt.groups())
            self.assertEqual((result["status"], result["collectedTests"]), ("passed", 1), result)
            self.assertIn(backend_pid, result["cleanup"]["adoptedZombiePids"])
            self.assertIn(backend_pid, result["cleanup"]["reapedPids"])
            self.assertNotEqual(runner.harness.ticks(backend_pid), backend_start)
            self.assertTrue(result["cleanup"]["namespaceRemoved"])
            self.assertTrue(runner.harness.alive(unrelated.pid, unrelated_start))
            self.assertEqual(subreaper_state(), prior)
        finally:
            if unrelated_start is not None:
                runner.harness.kill_exact(unrelated.pid, unrelated_start)
            unrelated.wait(timeout=2)

    def test_structured_result_channel_binds_invocation_and_rejects_missing_suite(self):
        def frame(sequence, event, **fields):
            return (json.dumps({"version": 1, "invocation": "inv", "item": "item",
                                "nonce": "nonce", "sequence": sequence, "event": event,
                                **fields}).encode() + b"\n")

        state = runner._ControlState("inv", "item", "nonce")
        state.bind_root(os.getpid(), runner.harness.ticks(os.getpid()))
        self.assertTrue(state.feed(frame(1, "completed")))
        self.assertFalse(state.valid(), "a completion is not a final suite result")
        self.assertFalse(state.feed(frame(2, "suite", testsRun=1, skipped=0,
                                          failures=2, errors=0, unexpectedSuccesses=0)))
        self.assertTrue(state.valid(), "unittest subtests may have more failures than testsRun")
        wrong = runner._ControlState("other-invocation", "item", "nonce")
        self.assertFalse(wrong.feed(frame(1, "completed")))
        self.assertFalse(wrong.valid())

    def test_mode_registry_and_no_precise_full_pass(self):
        self.assertEqual(set(runner.TARGET_SECONDS), {"fast", "precise"})
        self.assertTrue(runner.PRECISE_PENDING)
        self.assertEqual({item.evidence_level for item in runner.FAST_ITEMS}, {"fixture", "runtime"})
        items = [{"required": True, "status": "passed"}, {"required": True, "status": "not_run"}]
        self.assertEqual(runner._overall(items, None), ("inconclusive", 2))
        self.assertEqual(runner._overall(items[:1], None), ("passed", 0))
        self.assertEqual(runner._overall(items[:1], {"status": "failed"}), ("failed", 1))
        self.assertEqual(runner._overall(items[:1], {"status": "inconclusive"}), ("inconclusive", 2))

    def test_capture_bound_and_private_directory(self):
        buf = bytearray(b"before")
        dropped = runner._append_bounded(buf, b"x" * (runner.CAPTURE_BYTES * 2))
        self.assertEqual(len(buf), runner.CAPTURE_BYTES)
        self.assertEqual(dropped, len(b"before") + runner.CAPTURE_BYTES)
        with tempfile.TemporaryDirectory() as root:
            parent = Path(root)
            private = parent / "private"
            runner._private_dir(private)
            self.assertEqual(private.stat().st_mode & 0o777, 0o700)
            (private / "sentinel").write_text("keep")
            with self.assertRaises(ValueError):
                runner._private_dir(private)
            alias = parent / "alias"
            alias.symlink_to(private)
            with self.assertRaises(ValueError):
                runner._private_dir(alias)

    def test_hostile_parent_environment_not_inherited(self):
        with tempfile.TemporaryDirectory() as root:
            namespace = Path(root)
            omp = Path("/usr/bin/true")
            item = runner.Item("fixture", ("DV-AC-04",), (), "fixture")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sentinel", "TMUX": "user-socket",
                                      "WORKBENCH_DATA_DIR": "/home/user", "PI_CONFIG_DIR": "/home/user/pi",
                                      "HERDR_SOCKET": "/home/user/socket", "HOME": "/home/user"}):
                env = runner._base_env(namespace / "home", namespace, omp, item)
            for key in ("OPENAI_API_KEY", "TMUX", "WORKBENCH_DATA_DIR", "PI_CONFIG_DIR", "HERDR_SOCKET"):
                self.assertNotIn(key, env)
            self.assertEqual(env["HOME"], str(namespace / "home"))
            self.assertTrue(all(env[key] == runner.harness.BLOCKED_PROXY for key in runner.harness.PROXY_KEYS))

    def test_zero_collection_failure_and_watchdog_are_not_pass(self):
        with tempfile.TemporaryDirectory() as root:
            report_dir = Path(root)
            omp = Path("/usr/bin/true")
            empty = runner.Item("empty", ("DV-AC-04",),
                                ("tests.dev_validation.fixture_items.EmptyFixture",), "fixture")
            result = runner._run_item(empty, 0, report_dir, omp)
            self.assertEqual((result["status"], result["collectedTests"]), ("inconclusive", 0))
            self.assertTrue(result["cleanup"]["namespaceRemoved"])
            failing = runner.Item("failure", ("DV-AC-04",),
                                  ("tests.dev_validation.fixture_items.RunnerFixture.test_fail",), "fixture")
            result = runner._run_item(failing, 1, report_dir, omp)
            self.assertEqual(result["status"], "failed")
            hanging = runner.Item("hang", ("DV-AC-04",),
                                  ("tests.dev_validation.fixture_items.RunnerFixture.test_hang",), "fixture")
            with patch.object(runner, "SCENARIO_IDLE_SECONDS", 0.5):
                result = runner._run_item(hanging, 2, report_dir, omp)
            self.assertEqual(result["status"], "inconclusive")
            self.assertIn("watchdog", result["reason"])
            self.assertTrue(result["cleanup"]["namespaceRemoved"])

    def test_prior_first_failure_survives_successful_retry(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "prior.json"
            first = {"item": "installed_plain_bash", "status": "failed", "attemptId": "a1"}
            path.write_text(json.dumps({"schemaVersion": 1, "attemptChain": [{"attemptId": "a1"}],
                                        "firstFailure": first}))
            chain, failure = runner._prior(path)
            self.assertEqual(chain[0]["attemptId"], "a1")
            self.assertEqual(failure, first)
            self.assertEqual(runner._overall([{"required": True, "status": "passed"}], failure), ("failed", 1))
