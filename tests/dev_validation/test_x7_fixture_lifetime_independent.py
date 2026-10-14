"""X7 captured fixture identities/recipe; no original whole-scenario pass claim."""
import ast
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import dev_validate as runner

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/integration/live_cw16_fault.py"
TREE = ast.parse(SOURCE.read_text())
X7 = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "X7TakeoverRestart")
STEPS = next(node for node in X7.body if isinstance(node, ast.FunctionDef) and node.name == "steps")
RESTART = next(node for node in STEPS.body if isinstance(node, ast.FunctionDef) and node.name == "restart")
SHUTDOWN = next(node for node in STEPS.body if isinstance(node, ast.FunctionDef) and node.name == "shutdown")
STOP_OLD = next(node for node in ast.walk(RESTART) if isinstance(node, ast.FunctionDef) and node.name == "stop_old")
H_TREE = ast.parse((ROOT / "tests/integration/cw16_harness.py").read_text())
SESSION_MEMBERS = next(node for node in H_TREE.body if isinstance(node, ast.FunctionDef) and node.name == "session_members")
SHUTDOWN_RESULT = next(node for node in H_TREE.body if isinstance(node, ast.FunctionDef) and node.name == "shutdown_result")
SANDBOX = next(node for node in H_TREE.body if isinstance(node, ast.ClassDef) and node.name == "Sandbox")
CLOSE = next(node for node in SANDBOX.body if isinstance(node, ast.FunctionDef) and node.name == "close")
CLEANUP_LOOP = next(node for node in CLOSE.body if isinstance(node, ast.For)
                    and isinstance(node.target, ast.Name) and node.target.id == "cleanup")


def code(body):
    return compile(ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *deepcopy(body)], type_ignores=[])), str(SOURCE), "exec")


class X7FixtureLifetimeIndependentTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.environ.get("WB_LIVE_CW16"), "1")
        for boundary in ("subprocess.Popen", "socket.socket", "socket.create_connection"):
            tripwire = patch(boundary, side_effect=AssertionError("Native/network forbidden"))
            tripwire.start()
            self.addCleanup(tripwire.stop)

    def test_actual_old_session_snapshot_freezes_distinct_pairs_before_kill_and_survives_rebind(self):
        old_pairs = [(6111, 8111), (6222, 8222)]
        events, kills, roster_reads = [], [], []
        proc = {}
        for pid, session, ticks in ((6111, 501, 8111), (6222, 502, 8222), (7333, 503, 9333)):
            fields = [b"0"] * 20
            fields[0], fields[3], fields[19] = b"S", str(session).encode(), str(ticks).encode()
            proc[pid] = fields
        def listdir(path):
            self.assertEqual(path, "/proc")
            roster_reads.append(path)
            return ["self", "6111", "6222", "7333"]
        snapshot_namespace = {"os": SimpleNamespace(listdir=listdir), "stat_fields": lambda pid: proc.get(pid)}
        exec(code([SESSION_MEMBERS]), snapshot_namespace)
        actual_members = snapshot_namespace["session_members"]
        class Callbacks(list):
            def append(inner, callback):
                events.append(("registered", callback.__defaults__))
                super().append(callback)
        class Sandbox:
            def __init__(inner):
                inner.old_sessions = {501, 502}
                inner.current_sessions = inner.old_sessions
                inner.cleanups = Callbacks()
                inner.rebound = False
            @property
            def sessions(inner):
                self.assertFalse(inner.rebound, "new session roster must not be queried by callbacks")
                events.append(("sessions_read", tuple(sorted(inner.current_sessions))))
                return inner.current_sessions
            def start(inner):
                self.assertEqual(kills, [])
                events.append(("start",))
                return SimpleNamespace(returncode=0, stdout="controlled startup")
            def wait_ready(inner):
                self.assertEqual(kills, [])
                return {"startup": {"classification": "same_boot_crash", "survivors": []},
                        "task": {"task_id": "task-owned", "status": "held", "held_reason": "backend_restarted"}}
        sb = Sandbox()
        def kill_backend(actual_sb):
            self.assertIs(actual_sb, sb)
            self.assertEqual([callback.__defaults__ for callback in sb.cleanups], old_pairs)
            self.assertEqual(kills, [], "cleanup is deferred past restart observations")
            events.append(("backend_kill",))
            # Both mutation and rebind occur after the actual snapshot/default capture.
            sb.old_sessions.clear()
            sb.old_sessions.add(503)
            sb.current_sessions = {503}
            sb.rebound = True
            return {"signalled": True, "gone": True}
        def kill_exact(pid, ticks):
            self.assertIn((pid, ticks), old_pairs)
            events.append(("cleanup", pid, ticks))
            kills.append((pid, ticks))
            return True
        alive_reads = {pair: 0 for pair in old_pairs}
        clock = SimpleNamespace(elapsed=0.0)
        def alive(pid, ticks):
            self.assertIn((pid, ticks), old_pairs)
            self.assertIn((pid, ticks), kills, "physical death polling only after exact signal")
            pending = alive_reads[(pid, ticks)] < 2
            alive_reads[(pid, ticks)] += 1
            events.append(("physical_state", pid, ticks, pending))
            return pending
        def sleep(seconds):
            if seconds == 2:
                self.assertEqual(kills, [])
            else:
                self.assertEqual(seconds, 0.1)
                self.assertTrue(kills)
            clock.elapsed += seconds
        namespace = {"self": SimpleNamespace(kill_backend=kill_backend), "sb": sb, "state": {"task_id": "task-owned"},
                     "h": SimpleNamespace(session_members=actual_members, kill_exact=kill_exact, alive=alive),
                     "time": SimpleNamespace(sleep=sleep, monotonic=lambda: clock.elapsed)}
        exec(code([RESTART]), namespace)
        out = namespace["restart"]()
        self.assertEqual(out["classification"], "same_boot_crash")
        self.assertEqual(kills, [])
        self.assertEqual(roster_reads, ["/proc"])
        self.assertEqual([event for event in events if event[0] == "sessions_read"], [("sessions_read", (501, 502))])
        self.assertEqual([callback.__defaults__ for callback in sb.cleanups], old_pairs)
        self.assertEqual(events.index(("backend_kill",)), 3, "snapshot then two registrations before kill")
        result = {}
        exec(code([CLEANUP_LOOP]), {"self": sb, "result": result})
        self.assertEqual(result, {})
        self.assertEqual(kills, list(reversed(old_pairs)))
        self.assertEqual(alive_reads, {pair: 3 for pair in old_pairs})
        for pid, ticks in old_pairs:
            self.assertEqual([event[-1] for event in events if event[:3] == ("physical_state", pid, ticks)],
                             [True, True, False], "signal acceptance is not physical death")
        self.assertNotIn(7333, [pid for pid, _ in kills])
        self.assertEqual(roster_reads, ["/proc"], "no fresh roster scan during callback execution")

    def test_x7_declared_wait_matches_original_terminal_and_observation_recipe(self):
        item = next(item for item in runner.PRECISE_ITEMS if item.key == "takeover_restart_no_replay")
        legacy = next(cls for cls in TREE.body if isinstance(cls, ast.ClassDef) and any(
            isinstance(node, ast.FunctionDef) and node.name == "test_x7_takeover_during_delivery_restart" for node in cls.body))
        self.assertEqual(item.modules, ("tests.integration.live_cw16_fault." + legacy.name + ".test_x7_takeover_during_delivery_restart",))
        self.assertEqual(item.env, {"WB_LIVE_CW16": "1", "WB_CW16_FAULTS": "X7"})
        self.assertEqual(item.evidence_level, "runtime")
        self.assertIn("X7", item.assertion_ids)
        self.assertIsNone(item.outer)
        steps = tuple(node.value.args[0].value for node in STEPS.body if isinstance(node, ast.Expr)
                      and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                      and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "r"
                      and node.value.func.attr == "run")
        self.assertEqual(steps, ("X7.command_delivered", "X7.takeover_holds_new_sends", "X7.backend_restart",
                                 "X7.old_command_unknown_no_replay", "X7.shutdown"))
        self.assertEqual(item.scenario_steps, {"fault-x7": steps})
        takeover = next(node for node in STEPS.body if isinstance(node, ast.FunctionDef) and node.name == "takeover")
        waits = [node.args[1].value for node in ast.walk(takeover) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "new_result"]
        self.assertEqual(waits, [150, 150], "original readiness deadlines remain distinct from fixed terminal wait")
        terminal_tree = ast.parse((ROOT / "src/workbench/backend/flow_terminal.py").read_text())
        terminal_wait = next(node.value.value for node in terminal_tree.body if isinstance(node, ast.Assign)
                             and any(isinstance(target, ast.Name) and target.id == "WAIT_SECONDS" for target in node.targets))
        self.assertEqual(terminal_wait, 120)
        unknown = next(node for node in STEPS.body if isinstance(node, ast.FunctionDef) and node.name == "unknown_no_replay")
        observation = [node.args[0].value for node in ast.walk(unknown) if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                       and node.func.value.id == "time" and node.func.attr == "sleep"]
        self.assertEqual(observation, [15])
        self.assertEqual(item.expected_wait_seconds, 135)
        self.assertEqual(item.expected_wait_seconds, terminal_wait + observation[0])

    def shutdown_fixture(self, states=(True, True, False), known=(7444, 9444)):
        calls = {"shutdown": 0, "alive": [], "sleeps": [], "elapsed": 0.0}
        def forbidden(*_args):
            raise AssertionError("current backend wait must not query roster or signal")
        def alive(pid, ticks):
            self.assertEqual((pid, ticks), (7444, 9444))
            index = min(len(calls["alive"]), len(states) - 1)
            calls["alive"].append((pid, ticks))
            return states[index]
        def sleep(seconds):
            self.assertEqual(seconds, 0.1)
            calls["sleeps"].append(seconds)
            calls["elapsed"] += seconds
        def shutdown():
            calls["shutdown"] += 1
            return SimpleNamespace(returncode=1, stdout=json.dumps({"shutdown": {
                "verified": False, "problems": ["previous_survivors_alive"]}}))
        class Sandbox:
            @property
            def sessions(_self):
                return forbidden()
        sb = Sandbox()
        sb.known = {} if known is None else {"backend": known}
        sb.shutdown, sb.backend_processes = shutdown, forbidden
        parser = {"json": json}
        exec(code([SHUTDOWN_RESULT]), parser)
        namespace = {"self": SimpleNamespace(ui=None), "sb": sb,
                     "h": SimpleNamespace(alive=alive, session_members=forbidden, kill_exact=forbidden,
                                          shutdown_result=parser["shutdown_result"]),
                     "time": SimpleNamespace(monotonic=lambda: calls["elapsed"], sleep=sleep)}
        exec(code([SHUTDOWN]), namespace)
        return namespace["shutdown"], calls

    def test_current_exact_backend_pending_then_dead_preserves_unverified_original_result(self):
        actual, calls = self.shutdown_fixture()
        out = actual()
        self.assertEqual((out["exit"], out["verified"]), (1, False))
        self.assertEqual(out["problems"], ["previous_survivors_alive"])
        self.assertEqual(out["backend_exit"], {"pid": 7444, "start_ticks": 9444, "alive": False})
        self.assertEqual(calls["alive"], [(7444, 9444)] * 3)
        self.assertEqual(calls["sleeps"], [0.1, 0.1])
        self.assertEqual(calls["shutdown"], 1)

    def test_current_backend_timeout_or_missing_identity_fails_with_no_roster_or_signal(self):
        actual, calls = self.shutdown_fixture(states=(True,))
        with self.assertRaises(AssertionError) as failed:
            actual()
        reason, out = failed.exception.args[0]
        self.assertIn("within 25 s", reason)
        self.assertEqual((out["exit"], out["verified"]), (1, False))
        self.assertEqual(out["backend_exit"], {"pid": 7444, "start_ticks": 9444, "alive": True})
        self.assertGreaterEqual(calls["elapsed"], 25)
        self.assertLessEqual(calls["elapsed"], 25.1)
        self.assertEqual(set(calls["alive"]), {(7444, 9444)})
        self.assertEqual(calls["shutdown"], 1)
        actual, calls = self.shutdown_fixture(known=None)
        with self.assertRaises(AssertionError) as failed:
            actual()
        reason, out = failed.exception.args[0]
        self.assertIn("known restarted backend identity required", reason)
        self.assertEqual((out["exit"], out["verified"]), (1, False))
        self.assertNotIn("backend_exit", out)
        self.assertEqual(calls["alive"], [])
        self.assertEqual(calls["sleeps"], [])

    def test_old_exact_callback_signal_without_death_is_existing_cleanup_error(self):
        pair = (6111, 8111)
        calls = {"signals": [], "alive": [], "elapsed": 0.0}
        def kill_exact(pid, ticks):
            self.assertEqual((pid, ticks), pair)
            calls["signals"].append((pid, ticks))
            return True  # Successful asynchronous signal, not observed death.
        def alive(pid, ticks):
            self.assertEqual((pid, ticks), pair)
            self.assertEqual(calls["signals"], [pair])
            calls["alive"].append((pid, ticks))
            return True
        def sleep(seconds):
            self.assertEqual(seconds, 0.1)
            calls["elapsed"] += seconds
        namespace = {"pid": pair[0], "ticks": pair[1],
                     "h": SimpleNamespace(kill_exact=kill_exact, alive=alive),
                     "time": SimpleNamespace(monotonic=lambda: calls["elapsed"], sleep=sleep)}
        exec(code([STOP_OLD]), namespace)
        callback = namespace["stop_old"]
        self.assertEqual(callback.__defaults__, pair)
        result = {}
        exec(code([CLEANUP_LOOP]), {"self": SimpleNamespace(cleanups=[callback]), "result": result})
        self.assertEqual(calls["signals"], [pair])
        self.assertEqual(set(calls["alive"]), {pair})
        self.assertGreaterEqual(calls["elapsed"], 25)
        self.assertLessEqual(calls["elapsed"], 25.1)
        self.assertEqual(len(result["cleanup_errors"]), 1)
        self.assertIn("old X7 process did not exit within 25 s", result["cleanup_errors"][0])
        self.assertIn("6111, 8111", result["cleanup_errors"][0])


if __name__ == "__main__":
    unittest.main()
