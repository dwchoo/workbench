"""Failure-preserving, bounded registry/report fixture tests (no installed OMP)."""

from __future__ import annotations

import ast
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools import dev_validate as runner


def fixture_worker_argv(item, index, invocation, nonce, parent_pid, parent_start,
                        control_fd, result_fd, namespace, report_dir, omp):
    """Fixed unit-only Items use the same cell worker without adding production registry IDs."""
    definition = (f"r.Item({item.key!r}, {item.assertion_ids!r}, {item.modules!r}, {item.evidence_level!r}, "
                  f"env={item.env!r}, outer={item.outer!r})")
    script = ("from pathlib import Path; from tools import dev_validate as r; "
              f"raise SystemExit(r._cell_worker_main({definition}, {index!r}, {invocation!r}, {nonce!r}, "
              f"{parent_pid!r}, {parent_start!r}, {control_fd!r}, {result_fd!r}, "
              f"Path({str(namespace)!r}), Path({str(report_dir)!r}), Path({str(omp)!r})))")
    return [sys.executable, "-B", "-c", script]


class RunnerContractTests(unittest.TestCase):
    def test_two_python_cells_overlap_with_private_env_and_complete_owned_cleanup(self):
        items = tuple(runner.Item(key, ("DV-AC-04",),
                                 ("tests.dev_validation.fixture_items.RunnerFixture.test_hang",), "fixture",
                                 env={"WB_CW16_SHELLS": shell}, outer="tmux")
                      for key, shell in (("cell-a", "bash"), ("cell-b", "dash")))
        with tempfile.TemporaryDirectory() as root, patch.object(runner, "_worker_argv", fixture_worker_argv):
            rows = runner._parallel_items(items, Path(root), Path("/usr/bin/true"), "unit-overlap", 0)
        self.assertEqual([row["id"] for row in rows], ["cell-a", "cell-b"])
        self.assertEqual([row["status"] for row in rows], ["passed", "passed"], rows)
        first, second = (row["cellWorker"] for row in rows)
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertNotEqual(first["namespace"], second["namespace"])
        self.assertGreater(min(first["finishedMonotonic"], second["finishedMonotonic"])
                           - max(first["startedMonotonic"], second["startedMonotonic"]), 0)
        for row in rows:
            worker = row["cellWorker"]
            self.assertEqual(worker["remaining"], [])
            self.assertTrue(worker["namespaceRemoved"])
            self.assertEqual(worker["sentinelBeforeSha256"], worker["sentinelAfterSha256"])
            self.assertTrue(row["cleanup"]["namespaceRemoved"])
            self.assertTrue(worker["ownedIdentities"])

    def test_parent_death_signal_runs_cell_finally_for_doublefork_backend(self):
        script = '''
import os,sys,time
from pathlib import Path
from tools import dev_validate as r
root=Path(sys.argv[1])
namespace=root/'worker'; namespace.mkdir(mode=0o700)
item=r.Item('death-fixture', ('DV-AC-04',), ('tests.dev_validation.fixture_items_independent.IndependentFixture.test_product_doublefork_backend_waits_without_progress',), 'fixture')
control_read,control_write=os.pipe(); result_read,result_write=os.pipe()
child_code="""
import sys
from pathlib import Path
from tools import dev_validate as r
item=r.Item('death-fixture', ('DV-AC-04',), ('tests.dev_validation.fixture_items_independent.IndependentFixture.test_product_doublefork_backend_waits_without_progress',), 'fixture')
root=Path(sys.argv[1])
original=r.tempfile.mkdtemp
def private_item(*args, **kwargs):
    if kwargs.get('prefix', '').startswith('dvl01-'):
        path=root/'item'; path.mkdir(mode=0o700); return str(path)
    return original(*args, **kwargs)
r.tempfile.mkdtemp=private_item
raise SystemExit(r._cell_worker_main(item,0,'unit-death','unit-nonce',int(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4]),int(sys.argv[5]),root/'worker',root,Path('/usr/bin/true')))
"""
proc=r.subprocess.Popen([sys.executable,'-B','-c',child_code,str(root),str(os.getpid()),str(r.harness.ticks(os.getpid())),str(control_write),str(result_write)],env=r._base_env(namespace/'home',namespace,Path('/usr/bin/true'),item),stdin=r.subprocess.DEVNULL,stdout=r.subprocess.DEVNULL,stderr=r.subprocess.DEVNULL,pass_fds=(control_write,result_write),start_new_session=True)
print(proc.pid, r.harness.ticks(proc.pid), flush=True)
while True: time.sleep(0.05)
'''
        with tempfile.TemporaryDirectory() as temporary, runner._owned_subreaper():
            root = Path(temporary)
            supervisor = subprocess.Popen([sys.executable, "-B", "-c", script, str(root)], cwd=runner.REPO,
                                          env=runner.harness.clean_base_env(PYTHONPATH=str(runner.REPO / "src")),
                                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          start_new_session=True)
            supervisor_start = runner.harness.ticks(supervisor.pid)
            worker_pid = worker_start = backend_pid = backend_start = None
            try:
                worker_pid, worker_start = map(int, supervisor.stdout.readline().split())
                ready = root / "item" / "backend.ready"
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "controlled backend never became ready")
                backend_pid, backend_start = map(int, ready.read_text().split())
                self.assertTrue(runner.harness.alive(backend_pid, backend_start))
                self.assertTrue(runner.harness.kill_exact(supervisor.pid, supervisor_start))
                supervisor.wait(timeout=2)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and (runner.harness.alive(worker_pid, worker_start)
                                                      or runner.harness.alive(backend_pid, backend_start)):
                    time.sleep(0.02)
                self.assertFalse(runner.harness.alive(worker_pid, worker_start), "cell ignored parent-death signal")
                self.assertFalse(runner.harness.alive(backend_pid, backend_start), "cell finally orphaned backend")
                self.assertFalse((root / "item").exists(), "interrupted item namespace survived")
            finally:
                for pid, start in ((supervisor.pid, supervisor_start), (worker_pid, worker_start),
                                   (backend_pid, backend_start)):
                    if pid is not None:
                        runner.harness.kill_exact(pid, start)
                supervisor.wait(timeout=2)
                runner._reap_adopted({pid: start for pid, start in ((worker_pid, worker_start), (backend_pid, backend_start))
                                     if pid is not None and start is not None})
                supervisor.stdout.close()
                supervisor.stderr.close()

    def test_strict_cost_identity_and_failed_zero_cleanup_baseline_rejection(self):
        item = runner.Item("cost-fixture", ("DV-AC-04",),
                           ("tests.dev_validation.fixture_items.RunnerFixture.test_pass",), "fixture")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = runner._run_item(item, 0, root, Path("/usr/bin/true"))
            original = {"schemaVersion": 1, "attemptId": "old", "inputBefore": {"sha256": "same"},
                        "inputAfter": {"sha256": "same"}, "versionsBefore": {"omp": "fixture"},
                        "versionsAfter": {"omp": "fixture"}, "mode": "precise", "cwd": str(runner.REPO),
                        "ompPath": "/usr/bin/true", "costIdentity": runner._cost_identity((item,), Path("/usr/bin/true")),
                        "execution": {"jobs": 1}, "elapsedSeconds": 3, "result": "inconclusive", "items": [row],
                        "firstFailure": {"item": runner.PRECISE_PENDING[0], "status": "not_run"}}
            current = deepcopy(original)
            current.update(attemptId="new", execution={"jobs": 2}, elapsedSeconds=2)
            path = root / "baseline.json"
            path.write_text(json.dumps(original))
            comparison = runner._compare_cost(current, path)
            self.assertEqual(comparison["status"], "comparable", comparison)
            self.assertEqual(comparison["wallDeltaSeconds"], -1)
            self.assertEqual((comparison["baselineJobs"], comparison["currentJobs"]), (1, 2))
            variants = []
            for field, value in (("inputBefore", {"sha256": "changed"}), ("ompPath", "/elsewhere/omp")):
                variant = deepcopy(original); variant[field] = value; variants.append(variant)
            for field, value in (("status", "failed"), ("collectedTests", 0)):
                variant = deepcopy(original); variant["items"][0][field] = value; variants.append(variant)
            variant = deepcopy(original); variant["items"][0]["cleanup"]["namespaceRemoved"] = False; variants.append(variant)
            variant = deepcopy(original); variant["costIdentity"]["selectedItems"][0]["modules"] = ["changed"]; variants.append(variant)
            for index, variant in enumerate(variants):
                with self.subTest(index=index):
                    path.write_text(json.dumps(variant))
                    self.assertEqual(runner._compare_cost(current, path)["status"], "unknown")

    def test_jobs_two_fast_still_calls_direct_serial_items(self):
        def passed(item, *args):
            return {"id": item.key, "required": True, "status": "passed", "durationSeconds": 0,
                    "expectedWaitSeconds": item.expected_wait_seconds}
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(runner, "_run_item", side_effect=passed) as run_item, \
                 patch.object(runner, "_parallel_items", side_effect=AssertionError("FAST must stay serial")), \
                 patch.object(runner, "_input_manifest", return_value={"sha256": "fixture"}), \
                 patch.object(runner.harness, "tool_versions", return_value={"omp": "fixture"}), \
                 patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "fixture-head")):
                code = runner.main(["--mode", "fast", "--jobs", "2", "--report-dir", str(Path(temporary) / "report"),
                                    "--omp", "/usr/bin/true"])
            self.assertEqual(code, 0)
            self.assertEqual([call.args[0] for call in run_item.call_args_list], list(runner.FAST_ITEMS))

    def test_precise_registry_resolves_exact_legacy_ids_without_live_execution(self):
        ids = [name for item in runner.PRECISE_ITEMS for name in item.modules]
        self.assertEqual((len(runner.PRECISE_ITEMS), len(ids), len(set(ids))), (17, 19, 19))
        keys = [item.key for item in runner.PRECISE_ITEMS]
        p2_index = keys.index("pause_end_no_replay")
        self.assertEqual(keys[p2_index + 1:p2_index + 4], [
            "pause_interrupts_manager_turn", "restart_worker_survivor_host", "detach_active_work"])
        for test_id in ids:
            module, class_name, method = test_id.rsplit(".", 2)
            tree = ast.parse((runner.REPO / (module.replace(".", "/") + ".py")).read_text())
            cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
            self.assertIn(method, {node.name for node in cls.body if isinstance(node, ast.FunctionDef)})
        cells = [item for item in runner.PRECISE_ITEMS if item.outer]
        self.assertEqual([(item.outer, item.env["WB_CW16_SHELLS"]) for item in cells],
                         [(outer, shell) for outer in ("plain", "tmux", "herdr") for shell in ("bash", "dash")])
        for item in cells:
            self.assertEqual(item.env["WB_CW16_DETACH_SECONDS"], "65")
            self.assertEqual(item.expected_wait_seconds, 65)
            self.assertTrue(all(tuple(steps) == tuple(f"C{i}" for i in range(1, 8))
                                for steps in item.scenario_steps.values()))
            scenario, = item.scenario_steps
            self.assertEqual(item.allowed_na, ((scenario, "C6"),) if item.outer == "plain" else ())
        self.assertEqual(next(i for i in runner.PRECISE_ITEMS if i.key == "review_busy_end").expected_wait_seconds, 200)
        self.assertEqual(next(i for i in runner.PRECISE_ITEMS if i.key == "pause_end_no_replay").expected_wait_seconds, 90)
        by_key = {item.key: item for item in runner.PRECISE_ITEMS}
        expected = {
            "pause_interrupts_manager_turn": (
                100, {"WB_LIVE_CW16": "1", "WB_CW16_SCENARIOS": "P3"},
                {"policy-P3": ("start", "P3_pause_aborts_streaming_manager_turn",
                                "P3_running_manager_tool_recorded_unknown", "shutdown")}),
            "restart_worker_survivor_host": (
                0, {"WB_LIVE_CW16": "1", "WB_CW16_SCENARIOS": "P6"},
                {"policy-P6": ("start", "P6_restart_worker_verified", "P6_stop_survivor_unknown_refused",
                                "P6_host_terminal_force_kill", "shutdown")}),
            "detach_active_work": (
                515, {"WB_LIVE_CW16": "1", "WB_CW16_FAULTS": "X1", "WB_CW16_DETACH_SECONDS": "65"},
                {"fault-x1": ("X1.start_run", "X1.detach_active_review_continues", "X1.takeover_requested",
                               "X1.detach_takeover_state_kept", "X1.detach_paused_pause_kept",
                               "X1.resume_no_replay", "X1.shutdown")}),
        }
        for key, (wait_seconds, env, scenario_steps) in expected.items():
            with self.subTest(item=key):
                item = by_key[key]
                self.assertEqual(item.expected_wait_seconds, wait_seconds)
                self.assertEqual(item.env, env)
                self.assertEqual(item.scenario_steps, scenario_steps)

    def test_legacy_report_unknown_assertions_and_cleanup_never_pass(self):
        item = runner.Item("legacy", ("DV-AC-03",), (), "runtime",
                           scenario_steps={"scenario": ("required",)})
        cases = [
            ("passed", {"required": {"status": "pass"}}, {}, {}),
            ("inconclusive", {}, {}, {}),
            ("inconclusive", {"required": {"status": "not_run", "reason": "unisolatable"}}, {}, {}),
            ("inconclusive", {"required": {"status": "n/a"}}, {}, {}),
            ("failed", {"required": {"status": "fail"}}, {}, {}),
            ("failed", {"required": {"status": "pass"}}, {"residue_before_fallback": {"pid": 123}}, {}),
            ("failed", {"required": {"status": "pass"}}, {"root_removed": False}, {}),
            ("inconclusive", {"required": {"status": "pass"}}, {}, {"errors": ["scripted error"]}),
        ]
        with tempfile.TemporaryDirectory() as root:
            namespace = Path(root) / "namespace"
            reports = namespace / "reports" / item.key
            reports.mkdir(parents=True)
            output = Path(root) / "retained"
            output.mkdir()
            for index, (expected, steps, cleanup, provider) in enumerate(cases):
                with self.subTest(index=index):
                    payload = {"run_id": item.key, "scenario": "scenario", "result": "pass", "steps": steps,
                               "cleanup": {"root_removed": True, **cleanup}, "provider": provider}
                    path = reports / "scenario.json"
                    path.write_text(json.dumps(payload))
                    row, = runner._scenario_evidence(item, index, namespace, output)
                    self.assertEqual(row["status"], expected, row)
                    self.assertEqual(json.loads(Path(row["reportPath"]).read_text()), payload)
            path.unlink()
            row, = runner._scenario_evidence(item, 20, namespace, output)
            self.assertEqual(row["status"], "inconclusive")
            path.symlink_to(Path(root) / "outside.json")
            row, = runner._scenario_evidence(item, 21, namespace, output)
            self.assertEqual(row["status"], "inconclusive")

    def test_missing_outer_is_explicit_required_not_run(self):
        for item in (item for item in runner.PRECISE_ITEMS if item.outer == "tmux"):
            with self.subTest(item=item.key), patch.object(runner.harness, "find_tool", return_value=None), \
                    patch.object(runner, "_owned_subreaper", side_effect=AssertionError("must not launch")):
                result = runner._run_item(item, 0, Path("/unused"), Path("/usr/bin/true"))
            self.assertEqual((result["status"], result["collectedTests"]), ("not_run", 0))
            self.assertTrue(result["required"])
            self.assertIn("sh(dash)" if item.env["WB_CW16_SHELLS"] == "dash" else "Bash", result["reason"])
            self.assertEqual(runner._overall([result], None), ("inconclusive", 2))

    def test_precise_main_executes_core_registry_but_future_gates_remain_pending(self):
        def passed(item, *args):
            return {"id": item.key, "required": True, "status": "passed",
                    "durationSeconds": 0, "expectedWaitSeconds": item.expected_wait_seconds}

        with tempfile.TemporaryDirectory() as root:
            report_dir = Path(root) / "report"
            with patch.object(runner, "_run_item", side_effect=passed) as run_item, \
                    patch.object(runner, "_input_manifest", return_value={"sha256": "fixture"}), \
                    patch.object(runner.harness, "tool_versions", return_value={"omp": "fixture"}), \
                    patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "fixture-head")):
                exit_code = runner.main(["--mode", "precise", "--report-dir", str(report_dir), "--omp", "/usr/bin/true"])
            self.assertEqual([call.args[0] for call in run_item.call_args_list],
                             list(runner.FAST_ITEMS + runner.PRECISE_ITEMS))
            report = json.loads((report_dir / "report.json").read_text())
            self.assertEqual((exit_code, report["result"]), (2, "inconclusive"))
            self.assertEqual(report["matrix"]["shells"], ["bash", "dash"])
            self.assertEqual(report["matrix"]["shellLabels"]["dash"], "sh(dash)")
            self.assertNotIn("dashOuterCells", report["matrix"])
            self.assertEqual(report["scope"], "developer fast + representative core Bash/sh precise; DVL-13/product gates pending")
            self.assertEqual(len(report["matrix"]["testIds"]), 19)
            self.assertEqual({item["id"] for item in report["items"] if item["status"] == "not_run"},
                             set(runner.PRECISE_PENDING))
            self.assertEqual(report["firstFailure"]["item"], runner.PRECISE_PENDING[0])
            self.assertEqual({item["id"] for item in report["notRunCoverage"]},
                             {"legacy_full_regression", "VM_GUI_paid_model"})

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
                sh_item = next(i for i in runner.PRECISE_ITEMS if i.key == "compat_plain_dash")
                sh_env = runner._base_env(namespace / "sh-home", namespace, omp, sh_item)
            for child_env in (env, sh_env):
                for key in ("OPENAI_API_KEY", "TMUX", "WORKBENCH_DATA_DIR", "PI_CONFIG_DIR", "HERDR_SOCKET"):
                    self.assertNotIn(key, child_env)
            self.assertEqual(env["HOME"], str(namespace / "home"))
            self.assertTrue(all(env[key] == runner.harness.BLOCKED_PROXY for key in runner.harness.PROXY_KEYS))
            self.assertEqual(sh_env["HOME"], str(namespace / "sh-home"))
            self.assertEqual(sh_env["WB_CW16_SHELLS"], "dash")
            self.assertEqual(sh_env["WB_CW16_RUN_ID"], "compat_plain_dash")
            self.assertEqual(sh_env["WB_CW16_REPORT_DIR"], str(namespace / "reports"))
            self.assertTrue(all(sh_env[key] == runner.harness.BLOCKED_PROXY for key in runner.harness.PROXY_KEYS))

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
