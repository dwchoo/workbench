"""Original X6 exit evidence seams; AST-only, no legacy Native module import.

The actual helper and both caller bodies run unchanged. UI/status/wait/CLI ports
are controlled; this does not execute the 70MiB flood or storage fault runtime.
"""
import ast
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(os.environ.get("WB_DVL_X6_TEST_OUTPUT", tempfile.gettempdir()))
SOURCE = ROOT / "tests/integration/live_cw16_fault.py"
TREE = ast.parse(SOURCE.read_text())
X6 = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "X6Storage")
HELPER = next(node for node in X6.body if isinstance(node, ast.FunctionDef) and node.name == "run_exit_evidence")
STEPS = next(node for node in X6.body if isinstance(node, ast.FunctionDef) and node.name == "steps")
CALLERS = {node.name: node for node in STEPS.body if isinstance(node, ast.FunctionDef)
           and node.name in ("cap", "raw_log_store_fault")}
assert set(CALLERS) == {"cap", "raw_log_store_fault"}
MIB = 1024 * 1024


def module(body):
    return ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *body], type_ignores=[]))


class X6ExitEvidenceIndependentTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.environ.get("WB_LIVE_CW16"), "1", "fixture-only invocation")
        for port in ("subprocess.Popen", "socket.socket", "socket.create_connection"):
            tripwire = patch(port, side_effect=AssertionError("Native/network launch forbidden"))
            tripwire.start()
            self.addCleanup(tripwire.stop)
        self.recent = None

    def prepared(self, caller, *, changes=None, artifact="valid", reference=None, decoy_compact=False):
        temporary = tempfile.TemporaryDirectory(prefix="x6-exit-", dir=OUTPUT)
        self.addCleanup(temporary.cleanup)
        data = Path(temporary.name)
        run_id, task_id = "run-owned", "task-owned"
        path = data / "workflow/runs" / run_id / "run.json"
        path.parent.mkdir(parents=True, mode=0o700)
        (data / "raw-logs").mkdir(mode=0o700)
        record = {"task_id": task_id, "run_id": run_id, "shell_state": "exited",
                  "exit_confirmed": True, "exit_status": 0,
                  "unrelated_large_payload": "x" * 16384}
        record.update(changes or {})
        if artifact == "missing_field":
            record.pop("exit_status")
        raw = json.dumps(record, indent=2).encode() + b"\n"
        if artifact == "invalid":
            raw = b'{"run_id":'
        if artifact != "missing":
            path.write_bytes(raw)
            path.chmod(0o600)
        other = data / "workflow/runs/run-other/run.json"
        other.parent.mkdir(mode=0o700)
        other.write_text(json.dumps({"task_id": "other-task", "run_id": "run-other",
                                     "shell_state": "exited", "exit_confirmed": True, "exit_status": 0}))
        initial = {"task": {"task_id": task_id, "run_id": run_id, "status": "running"}}
        compact = {"run_id": "run-other" if decoy_compact else run_id, "exit_status": 0,
                   "raw_log_excerpt": "compact Core result without judged"}
        done = {"task": {"task_id": task_id, "run_id": None, "status": "finished", "last_result": compact},
                "panes": {"host_shell": {"shell": {"parent_mode": "manual_prompt"}}},
                "faults": {"raw_log": {"stored_bytes": 64 * MIB, "truncated": True, "cap_source": "run",
                    "text": "64 MiB; raw log 저장 장애", "storage_error": "owned fixture storage fault"}}}
        if reference in ("run_missing", "run_invalid"):
            value = None if reference == "run_missing" else "../run-other"
            if caller == "cap":
                initial["task"]["run_id"] = value
            else:
                done["task"]["last_result"]["run_id"] = value
        if reference == "task_missing":
            initial["task"]["task_id"] = None
            done["task"]["task_id"] = None
        if reference == "cap_terminal_task_mismatch":
            done["task"]["task_id"] = "other-task"
        waits = []
        def wait_status(predicate, timeout, what, **_options):
            snapshot = deepcopy(done)
            self.assertTrue(predicate(snapshot), "actual caller wait predicate: " + what)
            waits.append((timeout, what))
            return snapshot
        sb = SimpleNamespace(data=data, wait_status=wait_status,
                             cli=lambda *_args: SimpleNamespace(stdout="raw log 64 MiB\n"))
        namespace = {"Path": Path, "json": json, "sha256": sha256}
        cls = ast.ClassDef(name="ExtractedX6", bases=[], keywords=[], body=[deepcopy(HELPER)], decorator_list=[])
        exec(compile(module([cls]), str(SOURCE), "exec"), namespace)
        probe = namespace["ExtractedX6"]()
        probe.report = SimpleNamespace(data={})
        probe.ui = SimpleNamespace(close=lambda: None)
        probe.start = probe.open_ui = lambda _sb: None
        probe.type_to = lambda *_args: None
        probe.run_task = lambda _sb: deepcopy(initial)
        probe.raw_log = lambda _sb, _run: data / "controlled-size-only.raw"
        probe.size = lambda _path: 64 * MIB
        globals_ = {"self": probe, "a": sb, "b": sb, "state": {}, "os": os, "MIB": MIB}
        exec(compile(module([deepcopy(CALLERS[caller])]), str(SOURCE), "exec"), globals_)
        fixture = SimpleNamespace(data=data, path=path, raw=raw, probe=probe, sb=sb, waits=waits,
                                  run_id=run_id, task_id=task_id, invoke=globals_[caller], cleanup=temporary.cleanup)
        self.recent = fixture
        return fixture

    def test_compact_core_result_uses_actual_both_callers_and_retains_same_read_projection_after_cleanup(self):
        for caller in CALLERS:
            with self.subTest(caller=caller):
                f = self.prepared(caller, decoy_compact=(caller == "cap"))
                reads = []
                original = Path.read_bytes
                def observed_read(path):
                    reads.append(path)
                    return original(path)
                with patch.object(Path, "read_bytes", observed_read):
                    out = f.invoke()
                self.assertNotIn("judged", out["task"]["last_result"])
                evidence = deepcopy(out["run_exit"])
                self.assertEqual(reads, [f.path], "one actual canonical read, never search another run")
                self.assertEqual(evidence, f.probe.report.data["run_exit_evidence"][0])
                self.assertEqual(set(evidence), {"record_ref", "record_sha256", "expected_task_id", "expected_run_id",
                                                 "task_id", "run_id", "shell_state", "exit_confirmed", "exit_status"})
                self.assertEqual(evidence["record_ref"], "workflow/runs/run-owned/run.json")
                self.assertEqual(evidence["record_sha256"], sha256(f.raw).hexdigest())
                self.assertEqual((evidence["expected_task_id"], evidence["task_id"]), (f.task_id, f.task_id))
                self.assertEqual((evidence["expected_run_id"], evidence["run_id"]), (f.run_id, f.run_id))
                self.assertEqual((evidence["shell_state"], evidence["exit_confirmed"], evidence["exit_status"]),
                                 ("exited", True, 0))
                self.assertLess(len(json.dumps(evidence).encode()), 1024)
                self.assertEqual([timeout for timeout, _ in f.waits], [900] if caller == "cap" else [60, 120, 120])
                self.assertEqual((f.data / "raw-logs").stat().st_mode & 0o777, 0o700)
                retained = deepcopy(f.probe.report.data)
                f.cleanup()
                self.assertFalse(f.path.exists())
                self.assertEqual(retained["run_exit_evidence"][0], evidence)

    def test_missing_invalid_or_wrong_owned_record_fails_actual_both_callers(self):
        cases = [("missing", {}), ("invalid", {}), ("valid", {"task_id": "other-task"}),
                 ("valid", {"run_id": "other-run"})]
        for caller in CALLERS:
            for artifact, changes in cases:
                with self.subTest(caller=caller, artifact=artifact, changes=changes):
                    f = self.prepared(caller, artifact=artifact, changes=changes)
                    with self.assertRaises((AssertionError, FileNotFoundError, json.JSONDecodeError)):
                        f.invoke()
                    if changes:
                        evidence = f.probe.report.data["run_exit_evidence"][0]
                        self.assertEqual(evidence["record_sha256"], sha256(f.raw).hexdigest())
                        self.assertEqual(evidence["expected_run_id"], "run-owned")
                        self.assertEqual(evidence["expected_task_id"], "task-owned")
                        for key, value in changes.items():
                            self.assertEqual(evidence[key], value)

    def test_helper_identity_does_not_hide_unknown_unconfirmed_or_nonzero_caller_exit_failure(self):
        cases = [("valid", {"shell_state": "unknown", "exit_status": None}),
                 ("valid", {"exit_confirmed": False}), ("valid", {"exit_confirmed": 1}),
                 ("valid", {"exit_status": 1}), ("valid", {"exit_status": False}), ("missing_field", {})]
        for caller in CALLERS:
            for artifact, changes in cases:
                with self.subTest(caller=caller, artifact=artifact, changes=changes):
                    f = self.prepared(caller, artifact=artifact, changes=changes)
                    # The helper only binds identity. Preserve its actual behavior,
                    # then exercise the real caller assertion, not a cloned predicate.
                    observed = f.probe.run_exit_evidence(f.sb, f.task_id, f.run_id)
                    self.assertEqual(observed["task_id"], f.task_id)
                    self.assertEqual(observed["run_id"], f.run_id)
                    with self.assertRaises(AssertionError):
                        f.invoke()
                    self.assertEqual(f.probe.report.data["run_exit_evidence"][-1], observed)

    def test_actual_snapshot_references_are_required_without_guessing_from_other_run_directories(self):
        for caller in CALLERS:
            for reference in ("run_missing", "run_invalid", "task_missing"):
                with self.subTest(caller=caller, reference=reference):
                    f = self.prepared(caller, reference=reference)
                    self.assertTrue(f.path.exists())
                    self.assertTrue((f.data / "workflow/runs/run-other/run.json").exists())
                    with self.assertRaises(AssertionError):
                        f.invoke()
        f = self.prepared("cap", reference="cap_terminal_task_mismatch")
        with self.assertRaises(AssertionError):
            f.invoke()
        self.assertEqual(f.probe.report.data, {}, "same Task check precedes canonical evidence read")


if __name__ == "__main__":
    unittest.main()
