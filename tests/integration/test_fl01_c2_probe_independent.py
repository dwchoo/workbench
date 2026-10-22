"""Independent, local checks for the FL-01 C2 diagnostic probe.

These tests exercise the private instrumentation and its fail-closed preflight.
They never start Workbench, tmux, or an installed OMP process.
"""

from __future__ import annotations

import copy
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUTPUT = REPO / ".workflow/dev-validation-log-retention/runs/blockers-20261010/c2-test01"
DEPENDENCIES = os.environ.get("WB_DVL_C2_TEST_DEPS")
SOURCE_PATH = os.pathsep.join(path for path in (str(REPO / "src"), DEPENDENCIES) if path)
sys.path.insert(0, str(HERE))

import probe_dvl02_c2_escape as probe  # noqa: E402

ESC_TRACE_FIXTURE = HERE / "fixtures/c2_manager_esc_trace.json"


def _original_esc_attempt(*, include_open=False):
    """Execute the actual Esc send/predicate/capture and original assertion only.

    Other C2 input stages require a live terminal and are deliberately not run.
    """
    tree = ast.parse(Path(probe.live.__file__).read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "CompatRun")
    c2 = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "c2")
    loop = next(node for node in c2.body if isinstance(node, ast.For))
    capture = next(i for i, node in enumerate(loop.body) if isinstance(node, ast.Expr)
                   and isinstance(node.value, ast.Call)
                   and getattr(node.value.func, "attr", None) == "_retain_esc_failure")
    function = ast.FunctionDef(
        name="attempt", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg=name)
            for name in ("self", "pane", "r")], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=loop.body[capture - (3 if include_open else 2):capture + 1] + [loop.body[-1]],
        decorator_list=[])
    namespace = {"_hotkeys_completion_visible": probe.live._hotkeys_completion_visible}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 probe.live.__file__, "exec"), namespace)
    return namespace["attempt"]


def _rendered_menu_ui(text):
    """The real owner emulator, fed fixture bytes without opening a PTY."""
    ui = probe.h.Ui.__new__(probe.h.Ui)
    screen, stream = probe.h.screen_classes()
    ui.screen = screen(160, 12)
    ui.stream = stream(ui.screen)
    ui.raw, ui.raw_limit, ui.total_bytes = bytearray(), 1 << 20, 0
    ui.osc52, ui._osc_tail = [], b""
    ui._feed(text.encode())
    return ui


class C2ProbeIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        OUTPUT.mkdir(mode=0o700, parents=True, exist_ok=True)

    def test_menu_predicates_distinguish_command_row_from_retained_tip(self) -> None:
        # Command row shape is from pinned public OMP source, not a Native capture.
        tip = "Tip: see /hotkeys for your exit key"
        row = "> hotkeys  Show all keyboard shortcuts"
        states = [("menu", row + "\r\n/hot", True, False),
                  ("row_only", row, True, False),
                  ("retained_tip_and_draft", tip + "\r\n/hot", False, True),
                  ("tip_only", tip, False, True),
                  ("tip_and_menu", tip + "\r\n" + row + "\r\n/hot", True, False)]
        attempt = _original_esc_attempt(include_open=True)
        for name, text, opened, closed in states:
            with self.subTest(state=name):
                ui = _rendered_menu_ui(text)
                waits = []
                ui.wait = lambda predicate, timeout: (waits.append(timeout), predicate())[1]
                ui.keys = mock.Mock()
                report = SimpleNamespace(data={})
                owner = SimpleNamespace(ui=ui, report=report, outer_name="plain", shell="dash")
                owner._retain_esc_failure = probe.live.CompatRun._retain_esc_failure.__get__(owner)
                flags = dict.fromkeys(("typed_visible", "alt_enter_newline", "ctrl_c_cleared",
                                       "hangul_composer", "alive"), True)
                # A static closed screen cannot also prove that opening succeeded.
                with self.assertRaises(AssertionError):
                    attempt(owner, "manager_omp", flags)
                self.assertIs(flags["slash_menu"], opened)
                self.assertIs(flags["esc_closed_menu"], closed)
                self.assertEqual(waits, [5, 3])
                ui.keys.assert_called_once_with(b"\x1b", gap=1.0)

    def test_esc_requires_menu_row_to_disappear_despite_retained_tip(self) -> None:
        attempt = _original_esc_attempt(include_open=True)
        tip = "Tip: see /hotkeys for your exit key\r\n"
        menu = "> hotkeys  Show all keyboard shortcuts\r\n"
        for remains, tip_and_draft in ((False, True), (True, True), (False, False), (True, False)):
            with self.subTest(menu_remains=remains, tip_and_draft=tip_and_draft):
                prefix, draft = (tip, "/hot") if tip_and_draft else ("", "")
                ui = _rendered_menu_ui(prefix + menu + draft)
                waits = []
                ui.wait = lambda predicate, timeout: (waits.append(timeout), predicate())[1]
                def esc(*args, **kwargs):
                    self.assertEqual((args, kwargs), ((b"\x1b",), {"gap": 1.0}))
                    ui._feed(("\x1b[2J\x1b[H" + prefix + (menu if remains else "") + draft).encode())
                ui.keys = esc
                with mock.patch.object(probe.h, "_first_line", return_value="fixture-no-native"):
                    report = probe.h.ScenarioReport("menu-transition-fixture")
                owner = SimpleNamespace(ui=ui, report=report, outer_name="plain", shell="dash")
                owner._retain_esc_failure = probe.live.CompatRun._retain_esc_failure.__get__(owner)
                flags = dict.fromkeys(("typed_visible", "alt_enter_newline", "ctrl_c_cleared",
                                       "hangul_composer", "alive"), True)
                runner = probe.h.StepRunner(report)
                self.assertEqual(runner.run("C2", lambda: attempt(owner, "manager_omp", flags)),
                                 probe.h.FAIL if remains else probe.h.PASS)
                self.assertTrue(flags["slash_menu"])
                self.assertIs(flags["esc_closed_menu"], not remains)
                self.assertEqual(waits, [5, 3])
                if remains:
                    self.assertIn("esc_closed_menu", report.data["steps"]["C2"]["assertion"])
                    self.assertEqual(report.data["c2_esc_failure"]["omp_input_delivery"], "unknown")
                else:
                    self.assertNotIn("c2_esc_failure", report.data)

    def test_successful_esc_adds_no_buffer_observation_or_diagnostic_io(self) -> None:
        attempt = _original_esc_attempt()
        ui = mock.Mock(spec=["keys", "wait", "text"])
        ui.text.return_value = "menu closed"
        ui.wait.side_effect = lambda predicate, timeout: predicate()
        report = SimpleNamespace(data={"unknowns": ["retained prior unknown"]})
        owner = SimpleNamespace(ui=ui, report=report)
        owner._retain_esc_failure = probe.live.CompatRun._retain_esc_failure.__get__(owner)
        before = copy.deepcopy(report.data)
        flags = dict.fromkeys(("typed_visible", "alt_enter_newline", "ctrl_c_cleared",
                               "slash_menu", "hangul_composer", "alive"), True)
        with (mock.patch.object(probe.live.time, "monotonic_ns", side_effect=AssertionError("clock read")),
              mock.patch.object(Path, "write_text", side_effect=AssertionError("file write")),
              mock.patch("builtins.open", side_effect=AssertionError("file open"))):
            attempt(owner, "manager_omp", flags)
        self.assertTrue(flags["esc_closed_menu"])
        ui.keys.assert_called_once_with(b"\x1b", gap=1.0)
        self.assertEqual(ui.wait.call_args.args[1], 3)
        ui.text.assert_called_once_with()
        self.assertEqual(report.data, before)

    def test_failed_esc_retains_bounded_outer_buffers_in_normal_failed_report(self) -> None:
        attempt = _original_esc_attempt()
        ui = probe.h.Ui.__new__(probe.h.Ui)
        ui.screen = SimpleNamespace(display=["unrelated fixture text", "/hot"] +
                                     [f"hotkeys  Show all keyboard shortcuts {i} " + "x" * 200
                                      for i in range(12)])
        ui.raw = bytearray(bytes(range(256)) * 20)
        ui.total_bytes = len(ui.raw) + 1000
        events = []
        ui.keys = lambda *args, **kwargs: events.append(("keys", args, kwargs))
        def wait(predicate, timeout):
            events.append(("predicate", timeout))
            return predicate()
        ui.wait = wait
        excerpt = ui.excerpt
        ui.excerpt = lambda *args, **kwargs: (events.append(("snapshot",)), excerpt(*args, **kwargs))[1]
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary:
            with mock.patch.object(probe.h, "_first_line", return_value="fixture-no-native"):
                report = probe.h.ScenarioReport("c2-buffer-fixture", run_id="owned-fixture")
            report.unknown("OMP input delivery remains unknown")
            owner = SimpleNamespace(ui=ui, report=report, outer_name="tmux", shell="dash")
            owner._retain_esc_failure = probe.live.CompatRun._retain_esc_failure.__get__(owner)
            flags = dict.fromkeys(("typed_visible", "alt_enter_newline", "ctrl_c_cleared",
                                   "slash_menu", "hangul_composer", "alive"), True)
            runner = probe.h.StepRunner(report)
            with mock.patch.object(Path, "write_text", side_effect=AssertionError("early file write")):
                self.assertEqual(runner.run("C2", lambda: attempt(owner, "manager_omp", flags)), probe.h.FAIL)
            self.assertEqual(events, [("keys", (b"\x1b",), {"gap": 1.0}),
                                      ("predicate", 3), ("snapshot",)])
            self.assertEqual(list(Path(temporary).iterdir()), [])
            with mock.patch.object(probe.h, "REPORT_DIR", Path(temporary)):
                persisted = json.loads(report.write().read_text())
            self.assertEqual(persisted["result"], probe.h.FAIL)
            self.assertEqual(persisted["steps"]["C2"]["status"], probe.h.FAIL)
            self.assertIn("esc_closed_menu", persisted["steps"]["C2"]["assertion"])
            self.assertNotIn("error", persisted["steps"]["C2"])
            self.assertEqual(persisted["unknowns"], ["OMP input delivery remains unknown"])
            evidence = persisted["c2_esc_failure"]
            self.assertEqual((evidence["scope"], evidence["omp_input_delivery"]),
                             ("outer_client_buffers_only", "unknown"))
            self.assertEqual((evidence["pane"], evidence["outer"], evidence["host_shell"]),
                             ("manager_omp", "tmux", "dash"))
            self.assertEqual(evidence["rendered_bytes"], ui.total_bytes)
            self.assertEqual(evidence["screen_excerpt"], excerpt("/hot", "hotkeys", width=120)[:8])
            self.assertEqual(len(evidence["screen_excerpt"]), 8)
            self.assertTrue(all(len(line) <= 120 for line in evidence["screen_excerpt"]))
            self.assertNotIn("unrelated fixture text", json.dumps(evidence))
            self.assertEqual(bytes.fromhex(evidence["raw_tail_hex"]), bytes(ui.raw[-2048:]))

    def test_private_directory_requires_owned_absolute_unlinked_0700_path(self) -> None:
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary:
            base = Path(temporary)
            private = base / "private"
            private.mkdir(mode=0o700)
            probe._private(private)

            private.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "unsafe private directory"):
                probe._private(private)
            private.chmod(0o700)

            linked = base / "linked"
            linked.symlink_to(private, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "unsafe private directory"):
                probe._private(linked)
            with self.assertRaisesRegex(ValueError, "unsafe private directory"):
                probe._private(private.relative_to(REPO))

    def test_invalid_cell_selection_stops_before_diagnostic_files_exist(self) -> None:
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary:
            root = Path(temporary)
            namespace = root / "namespace"
            namespace.mkdir(mode=0o700)
            report = root / "report"
            report.mkdir(mode=0o700)
            with (mock.patch.object(probe.live, "LIVE", True),
                  mock.patch.object(probe.live, "SKIP", None),
                  mock.patch.object(probe.live, "OUTERS", ["plain"]),
                  mock.patch.object(probe.live, "SHELLS", ["dash"]),
                  mock.patch.dict(os.environ, {"WB_DVL_NAMESPACE": str(namespace),
                                               "WB_DVL_C2_OUTPUT": str(report)})):
                with self.assertRaisesRegex(ValueError, "tmux-only"):
                    probe.main()
            self.assertEqual(list(namespace.iterdir()), [])
            self.assertEqual(list(report.iterdir()), [])

    def test_overlong_socket_namespace_stops_before_diagnostic_files_exist(self) -> None:
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary:
            root = Path(temporary)
            namespace = root / "namespace"
            namespace.mkdir(mode=0o700)
            report = root / "report"
            report.mkdir(mode=0o700)
            self.assertGreaterEqual(len(str(namespace / "wbc16tmda-XXXXXXXX" / "x.sock").encode()), 100)
            with (mock.patch.object(probe.live, "LIVE", True),
                  mock.patch.object(probe.live, "SKIP", None),
                  mock.patch.object(probe.live, "OUTERS", ["tmux"]),
                  mock.patch.object(probe.live, "SHELLS", ["dash"]),
                  mock.patch.object(probe.live, "DETACH_SECONDS", 65),
                  mock.patch.dict(os.environ, {"WB_DVL_NAMESPACE": str(namespace),
                                               "WB_DVL_C2_OUTPUT": str(report)})):
                with self.assertRaisesRegex(ValueError, "AF_UNIX socket path"):
                    probe.main()
            self.assertEqual(list(namespace.iterdir()), [])
            self.assertEqual(list(report.iterdir()), [])

    def test_inputparser_hook_preserves_esc_and_plain_input_with_sanitized_trace(self) -> None:
        # A separate Python process loads the real sitecustomize hook, as a child
        # Workbench process would. The no-hook run is the behavior baseline.
        script = '''import json
from workbench.ui.product.input import InputParser
p = InputParser()
def show(events):
    return [[type(e).__name__, getattr(e, "data", b"").hex()] for e in events]
print(json.dumps([show(p.feed(b"/hot", now=10)),
                  show(p.feed(b"\\x1b", now=11)),
                  show(p.flush(now=11.02)),
                  show(p.flush(now=11.06)),
                  show(p.feed(b"private-secret-text", now=12))]))
'''
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary:
            base = Path(temporary)
            hook = base / "hook"
            trace = base / "trace"
            hook.mkdir(mode=0o700)
            trace.mkdir(mode=0o700)
            site = hook / "sitecustomize.py"
            site.write_text(probe.SITE_HOOK)
            site.chmod(0o600)
            env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": SOURCE_PATH}

            def run(with_hook: bool) -> list:
                child_env = dict(env)
                if with_hook:
                    child_env["PYTHONPATH"] = f"{hook}{os.pathsep}{env['PYTHONPATH']}"
                    child_env["WB_DVL_C2_HOOK"] = str(trace)
                completed = subprocess.run([sys.executable, "-c", script], cwd=REPO, env=child_env,
                                           capture_output=True, text=True, timeout=15, check=True)
                return json.loads(completed.stdout)

            original = run(False)
            instrumented = run(True)
            self.assertEqual(instrumented, original)
            self.assertEqual(instrumented[1:4], [[], [], [["Passthrough", "1b"]]])
            child_files = list(trace.glob("child-*.jsonl"))
            self.assertEqual(len(child_files), 1)
            self.assertEqual(child_files[0].stat().st_mode & 0o777, 0o600)
            content = child_files[0].read_text()
            events = [json.loads(line) for line in content.splitlines()]
            self.assertTrue(any(item["event"] == "parser_feed" and item["hex"] == "1b"
                                and item["events"] == [] for item in events))
            self.assertTrue(any(item["event"] == "parser_flush" and item["events"] == ["Passthrough:1b"]
                                for item in events))
            self.assertNotIn("private-secret-text", content)
            self.assertNotIn("/hot", content)

    def test_unavailable_trace_path_does_not_change_parser_result(self) -> None:
        script = '''import json
from workbench.ui.product.input import InputParser
p = InputParser()
print(json.dumps([len(p.feed(b"\\x1b", now=20)),
                  [e.data.hex() for e in p.flush(now=20.1)]]))
'''
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary:
            base = Path(temporary)
            hook = base / "hook"
            hook.mkdir(mode=0o700)
            (hook / "sitecustomize.py").write_text(probe.SITE_HOOK)
            env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": f"{hook}{os.pathsep}{SOURCE_PATH}",
                   "WB_DVL_C2_HOOK": str(base / "absent-trace")}
            completed = subprocess.run([sys.executable, "-c", script], cwd=REPO, env=env,
                                       capture_output=True, text=True, timeout=15, check=True)
            self.assertEqual(json.loads(completed.stdout), [0, ["1b"]])
            self.assertFalse((base / "absent-trace").exists())

    def test_original_cell_success_without_manager_trace_is_diagnostic_insufficient(self) -> None:
        # Supply only the unittest/provider boundary. probe.main still executes
        # its real preflight, hook setup, trace assessment, report and exit path.
        with tempfile.TemporaryDirectory(prefix="dvc2test-", dir=tempfile.gettempdir()) as namespace_tmp, \
                tempfile.TemporaryDirectory(dir=OUTPUT) as output_tmp:
            namespace = Path(namespace_tmp)
            report_dir = Path(output_tmp)
            scenario_root = report_dir / "scenario"
            run_id = "c2-test03-controlled"
            self.assertLess(len(str(namespace / "wbc16tmda-XXXXXXXX" / "x.sock").encode()), 100)
            scenario = {"versions": {"omp": "controlled-no-omp"},
                        "steps": {step: {"status": "pass"} for step in probe.live.STEPS},
                        "cleanup": {"residue_after_fallback": {}}}

            def run_controlled(_suite):
                target = scenario_root / run_id / "compat-tmux-dash.json"
                target.parent.mkdir(mode=0o700, parents=True)
                target.write_text(json.dumps(scenario))
                return SimpleNamespace(wasSuccessful=lambda: True, testsRun=1)

            old_umask = os.umask(0o077)
            os.umask(old_umask)
            old_sandbox_init = probe.h.Sandbox.__init__
            old_ui_send = probe.h.Ui.send
            try:
                with (mock.patch.object(probe.live, "LIVE", True),
                      mock.patch.object(probe.live, "SKIP", None),
                      mock.patch.object(probe.live, "OUTERS", ["tmux"]),
                      mock.patch.object(probe.live, "SHELLS", ["dash"]),
                      mock.patch.object(probe.live, "DETACH_SECONDS", 65),
                      mock.patch.object(probe.live, "RUN_ID", run_id),
                      mock.patch.object(probe.h, "REPORT_DIR", scenario_root),
                      mock.patch.object(probe.h, "find_omp", return_value="controlled-no-omp"),
                      mock.patch.object(probe.unittest.defaultTestLoader, "loadTestsFromName",
                                        return_value=object()) as load_suite,
                      mock.patch.object(probe.unittest, "TextTestRunner",
                                        side_effect=lambda **_kwargs: SimpleNamespace(run=run_controlled)),
                      mock.patch.dict(os.environ, {"WB_DVL_NAMESPACE": str(namespace),
                                                   "WB_DVL_C2_OUTPUT": str(report_dir),
                                                   "WB_DVL_C2_CANDIDATE": "controlled"})):
                    process_code = probe.main()
            finally:
                os.umask(old_umask)
            load_suite.assert_called_once_with("live_cw16_compat.Cw16CompatMatrix.test_tmux_dash")
            self.assertIs(probe.h.Sandbox.__init__, old_sandbox_init)
            self.assertIs(probe.h.Ui.send, old_ui_send)
            self.assertEqual(process_code, 2)
            payload = json.loads((report_dir / "result.json").read_text())
            self.assertEqual(payload["original_cell_exit_code"], 0)
            self.assertEqual(payload["steps"]["C2"]["status"], "pass")
            self.assertEqual(payload["diagnostic_exit_code"], 2)
            self.assertEqual(payload["process_exit_code"], process_code)
            self.assertEqual(payload["diagnostic"]["status"], "insufficient")
            self.assertIn("manager_esc_window", payload["diagnostic"]["missing"])
            self.assertIn("unknown", payload["diagnostic"]["interpretation"])
            self.assertEqual(payload["harness_events"], 0)
            self.assertEqual(payload["child_events"], 0)

    def test_retained_manager_trace_requires_related_request_frame_and_window(self) -> None:
        fixture = json.loads(ESC_TRACE_FIXTURE.read_text())
        self.assertEqual(fixture["fixture_kind"], "sanitized_recorded_c2_manager_esc_window")
        self.assertEqual(fixture["evidence_level"], "fixture")
        self.assertEqual(fixture["provenance"]["source_run_id"], "block-c2-original-01")
        self.assertEqual(fixture["provenance"]["source_cell"], "Cw16CompatMatrix.test_tmux_dash")
        self.assertEqual(fixture["provenance"]["source_step_status"], "pass")
        self.assertFalse(fixture["provenance"]["fresh_runtime"])
        events = fixture["events"]
        complete = probe.assess_manager_esc_trace(events)
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(complete["missing"], [])
        start, end = complete["window_start_ns"], complete["window_end_ns"]

        def in_manager_window(event: dict) -> bool:
            return isinstance(event.get("t_ns"), int) and start <= event["t_ns"] <= end

        variants = {}
        variants["no_manager_window"] = [e for e in events
                                         if not (e.get("event") == "esc_before"
                                                 and e.get("pane") == "manager_omp")]
        variants["missing_product_stdin"] = [e for e in events
                                             if not (e.get("event") == "product_stdin_read"
                                                     and in_manager_window(e))]
        unrelated_request = copy.deepcopy(events)
        for event in unrelated_request:
            if event.get("event") == "backend_result" and in_manager_window(event):
                event["request_id"] = "unrelated-request"
        variants["unrelated_request"] = unrelated_request
        unrelated_frame = copy.deepcopy(events)
        for event in unrelated_frame:
            if event.get("event") == "manager_display_frame" and in_manager_window(event):
                event["sequence"] = 999999999
        variants["unrelated_frame"] = unrelated_frame
        outside_window = copy.deepcopy(events)
        for event in outside_window:
            if event.get("event") == "product_stdin_read" and in_manager_window(event):
                event["t_ns"] = end + 1
        variants["outside_window"] = outside_window

        expected_missing = {"no_manager_window": "manager_esc_window",
                            "missing_product_stdin": "product_stdin",
                            "unrelated_request": "backend_result_same_request",
                            "unrelated_frame": "matching_manager_frame",
                            "outside_window": "product_stdin"}
        for name, altered in variants.items():
            with self.subTest(name=name):
                assessment = probe.assess_manager_esc_trace(altered)
                self.assertEqual(assessment["status"], "insufficient")
                self.assertIn(expected_missing[name], assessment["missing"])
                self.assertIn("unknown", assessment["interpretation"])


if __name__ == "__main__":
    unittest.main()
