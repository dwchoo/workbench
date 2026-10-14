"""X4 fixture callback lifetime, actual AST body/close loop, no Native import.

External UI/CLI/PID ports are controlled. OS signalling/identity reuse and the
whole X4 scenario remain existing kill_exact/Root runtime obligations.
"""
import ast
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(os.environ.get("WB_DVL_X4_TEST_OUTPUT", tempfile.gettempdir()))
SOURCE = ROOT / "tests/integration/live_cw16_fault.py"
TREE = ast.parse(SOURCE.read_text())
X4 = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "X4Shutdown")
STEPS = next(node for node in X4.body if isinstance(node, ast.FunctionDef) and node.name == "steps")
BODY = next(node for node in STEPS.body if isinstance(node, ast.FunctionDef) and node.name == "unverified")
HARNESS = ROOT / "tests/integration/cw16_harness.py"
SANDBOX = next(node for node in ast.parse(HARNESS.read_text()).body
               if isinstance(node, ast.ClassDef) and node.name == "Sandbox")
CLOSE = next(node for node in SANDBOX.body if isinstance(node, ast.FunctionDef) and node.name == "close")
CLEANUP_LOOP = next(node for node in CLOSE.body if isinstance(node, ast.For)
                    and isinstance(node.target, ast.Name) and node.target.id == "cleanup")


class X4FixtureCleanupIndependentTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.environ.get("WB_LIVE_CW16"), "1", "fixture-only invocation")
        for port in ("subprocess.Popen", "socket.socket", "socket.create_connection"):
            tripwire = patch(port, side_effect=AssertionError("Native/network forbidden"))
            tripwire.start()
            self.addCleanup(tripwire.stop)

    def fixture(self, *, error_at=None, unknown_at=None, invalid_negative=None):
        temporary = tempfile.TemporaryDirectory(prefix="x4-callback-", dir=OUTPUT)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        data = root / "data"
        data.mkdir(mode=0o700)
        identities = [(4011, 9021), (4012, 9022)]
        events, commands, kills = [], [], []
        class Callbacks(list):
            def append(inner, callback):
                events.append(("registered", callback.__defaults__))
                super().append(callback)
        callbacks = Callbacks()
        def type_to(_sb, pane, command):
            index = len(commands)
            self.assertEqual(pane, "host_shell")
            self.assertEqual(len(callbacks), index, "prior known identity registered before next command")
            if index == 1 and error_at == "second_command":
                events.append(("second_command_error",))
                raise RuntimeError("controlled downstream second command failure")
            commands.append(command)
            events.append(("command", index))
            path = root / ("x4-setsid.pid" if index == 0 else "x4-daemon.pid")
            path.write_text(str(identities[index][0]) + "\n")
        def ticks(pid):
            index = next(i for i, identity in enumerate(identities) if identity[0] == pid)
            events.append(("ticks", pid))
            return None if unknown_at == index + 1 else identities[index][1]
        def wait_file(path, *, pump, timeout):
            self.assertEqual(timeout, 15)
            return path.is_file()
        def kill_exact(pid, start):
            # Controlled signal port only; actual OS/reuse checks are not replaced
            # as product policy or claimed as validated by this fixture.
            self.assertIn((pid, start), identities, "only captured owned fixture pair")
            events.append(("cleanup", pid, start))
            kills.append((pid, start))
            return True
        def sleep(seconds):
            self.assertEqual(seconds, 1.5)
            self.assertEqual(len(callbacks), 2)
            self.assertEqual(kills, [], "registration must not pre-cleanup the negative observation")
            events.append(("settle", seconds))
        payload = {"verified": False, "problems": ["left_session"],
                   "left_running": [{"pid": 4011, "start_ticks": 9021, "state": "left_session"}]}
        code, stderr = 1, "종료 확인 실패"
        if invalid_negative == "exit_and_verified":
            code, payload["verified"] = 0, True
        elif invalid_negative == "failure_text":
            stderr = ""
        elif invalid_negative == "left_running":
            payload["left_running"] = []
        def cli(*args, timeout):
            self.assertEqual(args, ("shutdown", "--data-dir", str(data), "--yes"))
            self.assertEqual(timeout, 120)
            self.assertEqual(len(callbacks), 2)
            self.assertEqual(kills, [], "negative CLI must observe still-live owned fixtures")
            events.append(("cli",))
            if error_at == "cli":
                raise RuntimeError("controlled downstream CLI failure")
            return SimpleNamespace(returncode=code, stderr=stderr,
                                   stdout="left_session observed\nshutdown result: " + json.dumps(payload))
        def alive(pid, start):
            self.assertEqual((pid, start), identities[1])
            self.assertEqual(kills, [])
            events.append(("daemon_observed_alive", pid, start))
            return True
        sb = SimpleNamespace(tmp=root, data=data, cleanups=callbacks, cli=cli)
        owner = SimpleNamespace(ui=SimpleNamespace(close=lambda: events.append(("ui_close",))),
                                start=lambda _sb: None, open_ui=lambda _sb: None, type_to=type_to)
        namespace = {"self": owner, "b": sb, "json": json,
                     "h": SimpleNamespace(ticks=ticks, wait_file=wait_file, kill_exact=kill_exact, alive=alive),
                     "time": SimpleNamespace(sleep=sleep)}
        module = ast.Module(body=[deepcopy(BODY)], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)
        def cleanup_callbacks():
            result = {}
            code_ = ast.Module(body=[deepcopy(CLEANUP_LOOP)], type_ignores=[])
            exec(compile(ast.fix_missing_locations(code_), str(HARNESS), "exec"), {"self": sb, "result": result})
            self.assertEqual(result, {})
        return SimpleNamespace(invoke=namespace["unverified"], callbacks=callbacks, events=events,
                               commands=commands, kills=kills, identities=identities,
                               cleanup_callbacks=cleanup_callbacks)

    def test_original_negative_observations_and_assertions_precede_exact_registered_cleanup(self):
        f = self.fixture()
        out = f.invoke()
        self.assertEqual((out["exit"], out["verified"]), (1, False))
        self.assertIn("종료 확인 실패", out["stderr"])
        self.assertTrue(out["left_running"])
        self.assertEqual(out["owned_fixture_processes"], [{"pid": pid, "start_ticks": ticks} for pid, ticks in f.identities])
        self.assertEqual(out["observation_double_forked_daemon"], {
            "alive_after_shutdown": True, "in_left_running": False, "listed_as_active": False})
        self.assertEqual(f.kills, [])
        self.assertEqual([callback.__defaults__ for callback in f.callbacks], f.identities)
        first, second = f.commands
        self.assertIn("setsid -w sh -c 'echo $$ > ", first)
        self.assertIn("; exec sleep 900'", first)
        self.assertNotIn("$!", first)
        self.assertIn("setsid sh -c 'echo $$ > ", second)
        self.assertIn("; exec sleep 901'", second)
        observed_at = next(i for i, event in enumerate(f.events) if event[0] == "daemon_observed_alive")
        self.assertEqual(sum(event[0] == "registered" for event in f.events[:observed_at]), 2)
        f.cleanup_callbacks()
        self.assertEqual(f.kills, list(reversed(f.identities)))
        self.assertTrue(all(i > observed_at for i, event in enumerate(f.events) if event[0] == "cleanup"))

    def test_downstream_exceptions_preserve_callbacks_for_already_captured_identities(self):
        for stage, count in (("second_command", 1), ("cli", 2)):
            with self.subTest(error_at=stage):
                f = self.fixture(error_at=stage)
                with self.assertRaisesRegex(RuntimeError, "controlled downstream"):
                    f.invoke()
                self.assertEqual(len(f.callbacks), count)
                self.assertEqual([callback.__defaults__ for callback in f.callbacks], f.identities[:count])
                self.assertEqual(f.kills, [])
                f.cleanup_callbacks()
                self.assertEqual(f.kills, list(reversed(f.identities[:count])))

    def test_all_three_original_refusal_assertions_still_fail_before_registered_cleanup(self):
        for verdict in ("exit_and_verified", "failure_text", "left_running"):
            with self.subTest(invalid_negative=verdict):
                f = self.fixture(invalid_negative=verdict)
                with self.assertRaises(AssertionError):
                    f.invoke()
                self.assertEqual(len(f.callbacks), 2)
                self.assertEqual(f.kills, [])
                f.cleanup_callbacks()
                self.assertEqual(f.kills, list(reversed(f.identities)))

    def test_unknown_identity_is_not_registered_or_signalled_and_prior_known_callback_survives(self):
        for unknown_at in (1, 2):
            with self.subTest(unknown_at=unknown_at):
                f = self.fixture(unknown_at=unknown_at)
                with self.assertRaises(AssertionError) as failed:
                    f.invoke()
                self.assertIn("unknown owned", str(failed.exception))
                self.assertEqual(len(f.callbacks), unknown_at - 1)
                self.assertFalse(any(event[0] == "cli" for event in f.events))
                self.assertEqual(f.kills, [])
                f.cleanup_callbacks()
                self.assertEqual(f.kills, list(reversed(f.identities[:unknown_at - 1])))


if __name__ == "__main__":
    unittest.main()
