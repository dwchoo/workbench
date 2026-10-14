"""Actual X2 shutdown AST seam; no Native module import or runtime claim.

PID/process observations and clock are controlled external ports. Original
shutdown assertions and the 25s positive bound execute unchanged.
"""
import ast
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/integration/live_cw16_fault.py"
TREE = ast.parse(SOURCE.read_text())
X2 = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "X2Restart")
STEPS = next(node for node in X2.body if isinstance(node, ast.FunctionDef) and node.name == "steps")
SHUTDOWN = next(node for node in STEPS.body if isinstance(node, ast.FunctionDef) and node.name == "shutdown")
HARNESS = ROOT / "tests/integration/cw16_harness.py"
PARSER = next(node for node in ast.parse(HARNESS.read_text()).body
              if isinstance(node, ast.FunctionDef) and node.name == "shutdown_result")


def extracted(function, namespace):
    body = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                           deepcopy(function)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(body), str(SOURCE), "exec"), namespace)
    return namespace[function.name]


class X2BackendExitIndependentTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.environ.get("WB_LIVE_CW16"), "1", "fixture-only invocation")
        for port in ("subprocess.Popen", "socket.socket", "socket.create_connection"):
            tripwire = patch(port, side_effect=AssertionError("Native/network forbidden"))
            tripwire.start()
            self.addCleanup(tripwire.stop)

    def fixture(self, *, frames=((False, ()),), known=(4311, 9022), negative=False,
                returncode=None, verified=None, problems=None):
        activity = {"shutdown": 0, "ui_closed": 0, "alive": [], "process_lists": [],
                    "ticks": [], "sleeps": [], "monotonic": [], "stage": 0, "elapsed": 0.0}
        def monotonic():
            if negative:
                raise AssertionError("positive clock wait entered intentional negative branch")
            activity["monotonic"].append(activity["elapsed"])
            return activity["elapsed"]
        def sleep(seconds):
            if negative:
                raise AssertionError("positive sleep entered intentional negative branch")
            activity["sleeps"].append(seconds)
            activity["elapsed"] += seconds
            activity["stage"] += 1
        def frame():
            return frames[min(activity["stage"], len(frames) - 1)]
        def alive(pid, ticks):
            activity["alive"].append((pid, ticks))
            if pid == 7788:
                self.assertEqual(ticks, 9911)
                return True
            self.assertEqual((pid, ticks), (4311, 9022), "same known restarted PID/startticks only")
            return frame()[0]
        def backend_processes():
            if negative:
                raise AssertionError("positive process wait entered intentional negative branch")
            result = list(frame()[1])
            activity["process_lists"].append(result)
            return result
        def ticks(pid):
            activity["ticks"].append(pid)
            return pid + 10000
        payload = {"verified": not negative if verified is None else verified,
                   "problems": (["previous_backend_survivors_alive"] if negative else []) if problems is None else problems,
                   "previous_survivors": [{"pid": 7788}] if negative else []}
        done = SimpleNamespace(returncode=(1 if negative else 0) if returncode is None else returncode,
                               stdout="owned shutdown\n" + json.dumps({"shutdown": payload}))
        def shutdown():
            activity["shutdown"] += 1
            return done
        parser = extracted(PARSER, {"json": json})
        sb = SimpleNamespace(known={} if known is None else {"backend": known}, shutdown=shutdown,
                             backend_processes=backend_processes)
        ui = SimpleNamespace(close=lambda: activity.__setitem__("ui_closed", activity["ui_closed"] + 1))
        owner = SimpleNamespace(ui=ui)
        state = {"survivors": [{"pid": 7788, "start_ticks": 9911}]} if negative else {"survivors": []}
        actual = extracted(SHUTDOWN, {"self": owner, "sb": sb, "state": state,
                                     "h": SimpleNamespace(alive=alive, ticks=ticks, shutdown_result=parser),
                                     "time": SimpleNamespace(monotonic=monotonic, sleep=sleep)})
        return SimpleNamespace(invoke=actual, activity=activity, owner=owner, payload=payload)

    def test_positive_requires_both_known_backend_dead_and_empty_process_list(self):
        f = self.fixture()
        out = f.invoke()
        self.assertEqual((out["exit"], out["verified"]), (0, True))
        self.assertEqual(out["backend_exit"], {"pid": 4311, "start_ticks": 9022, "alive": False,
                                              "remaining_backend_count": 0, "remaining_backends": []})
        self.assertEqual(f.activity["alive"], [(4311, 9022)])
        self.assertEqual(f.activity["process_lists"], [[]])
        self.assertEqual(f.activity["sleeps"], [])
        self.assertEqual(f.activity["shutdown"], 1)
        self.assertEqual(f.activity["ui_closed"], 1)
        self.assertIsNone(f.owner.ui)

    def test_either_lingering_condition_waits_until_both_are_empty(self):
        sequences = {
            "known_pid_only": ((True, ()), (True, ()), (False, ())),
            "process_list_only": ((False, (5566,)), (False, (5566,)), (False, ())),
        }
        for label, frames in sequences.items():
            with self.subTest(linger=label):
                f = self.fixture(frames=frames)
                out = f.invoke()
                self.assertEqual(f.activity["sleeps"], [0.1, 0.1])
                self.assertEqual(f.activity["process_lists"], [list(frame[1]) for frame in frames])
                self.assertEqual(f.activity["alive"], [(4311, 9022)] * 3)
                self.assertEqual(out["backend_exit"]["alive"], False)
                self.assertEqual(out["backend_exit"]["remaining_backend_count"], 0)
                self.assertEqual(f.activity["shutdown"], 1, "no shutdown retries")

    def test_timeout_fails_with_same_last_observed_identity_and_bounded_remaining_facts(self):
        processes = tuple(range(6000, 6020))
        f = self.fixture(frames=((True, processes),))
        with self.assertRaises(AssertionError) as failed:
            f.invoke()
        reason, out = failed.exception.args[0]
        self.assertIn("within 25 s", reason)
        evidence = deepcopy(out["backend_exit"])
        self.assertEqual((out["exit"], out["verified"]), (0, True), "exit facts do not relabel timeout as success")
        self.assertEqual((evidence["pid"], evidence["start_ticks"], evidence["alive"]), (4311, 9022, True))
        self.assertEqual(evidence["remaining_backend_count"], len(f.activity["process_lists"][-1]))
        self.assertEqual(evidence["remaining_backends"], [{"pid": pid, "start_ticks": pid + 10000}
                                                        for pid in processes[:16]])
        self.assertLessEqual(len(evidence["remaining_backends"]), 16)
        self.assertGreaterEqual(f.activity["elapsed"], 25)
        self.assertLessEqual(f.activity["elapsed"], 25.1)
        self.assertEqual(set(f.activity["sleeps"]), {0.1})
        self.assertEqual(f.activity["shutdown"], 1)
        self.assertEqual(set(f.activity["alive"]), {(4311, 9022)})
        self.assertLess(len(json.dumps(evidence).encode()), 2048)

    def test_positive_missing_known_identity_fails_before_any_process_wait(self):
        for known in (None, (4311,), (4311, None), (True, 9022)):
            with self.subTest(identity=known):
                f = self.fixture(known=known)
                with self.assertRaises(AssertionError) as failed:
                    f.invoke()
                reason, out = failed.exception.args[0]
                self.assertIn("known restarted backend identity required", reason)
                self.assertEqual((out["exit"], out["verified"]), (0, True))
                self.assertNotIn("backend_exit", out)
                for key in ("alive", "process_lists", "sleeps", "monotonic"):
                    self.assertEqual(f.activity[key], [])
                self.assertEqual(f.activity["shutdown"], 1)

    def test_intentional_alive_survivor_negative_preserves_verdict_without_positive_wait(self):
        f = self.fixture(negative=True, known=None)
        out = f.invoke()
        self.assertEqual((out["exit"], out["verified"], out["alive_survivors"]), (1, False, 1))
        self.assertIn("previous_backend_survivors_alive", out["problems"])
        self.assertEqual(out["previous_survivors"], [{"pid": 7788}])
        self.assertNotIn("backend_exit", out)
        self.assertEqual(f.activity["alive"], [(7788, 9911)])
        self.assertEqual(f.activity["process_lists"], [])
        self.assertEqual(f.activity["sleeps"], [])
        self.assertEqual(f.activity["monotonic"], [])
        for changes in ({"returncode": 0, "verified": True}, {"problems": []}):
            with self.subTest(invalid_negative=changes):
                f = self.fixture(negative=True, known=None, **changes)
                with self.assertRaises(AssertionError):
                    f.invoke()
                self.assertEqual(f.activity["process_lists"], [])
                self.assertEqual(f.activity["monotonic"], [])


if __name__ == "__main__":
    unittest.main()
