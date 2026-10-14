"""DVL-02 독립 registry/집계 시험. Pipe fixture이며 Native runtime 근거가 아니다."""

from contextlib import ExitStack, redirect_stdout
from hashlib import sha256
import ast
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import dev_validate as runner


COMPAT = "tests.integration.live_cw16_compat"
POLICY = "tests.integration.live_cw16_flow_policy.Cw16PolicyScenarios"
FAULT = "tests.integration.live_cw16_fault.Cw16FaultScenarios"
EXPECTED_IDS = (
    f"{COMPAT}.Cw16CompatMatrix.test_plain_bash",
    f"{COMPAT}.Cw16CompatMatrix.test_plain_dash",
    f"{COMPAT}.Cw16CompatMatrix.test_tmux_bash",
    f"{COMPAT}.Cw16CompatMatrix.test_tmux_dash",
    f"{COMPAT}.Cw16CompatMatrix.test_herdr_bash",
    f"{COMPAT}.Cw16CompatMatrix.test_herdr_dash",
    f"{COMPAT}.Cw16ShellSelection.test_sel1_bash_first_fake_zsh_never_executed",
    f"{COMPAT}.Cw16ShellSelection.test_sel2_no_bash_falls_back_to_dash",
    f"{COMPAT}.Cw16ShellSelection.test_sel3_neither_bash_nor_sh_guidance_nonzero_nothing_started",
    f"{POLICY}.test_p1_periodic_reviews_merge_and_p7_usage",
    f"{POLICY}.test_p2_pause_holds_automatic_work",
    f"{POLICY}.test_p3_pause_interrupts_manager_turn",
    f"{POLICY}.test_p6_restart_worker_stop_survivor_kill_host",
    f"{FAULT}.test_x1_detach_with_active_work",
    f"{FAULT}.test_x2_backend_sigkill_restart",
    f"{FAULT}.test_x3_model_fault_holds",
    f"{FAULT}.test_x4_shutdown_confirmation",
    f"{FAULT}.test_x6_storage_faults",
    f"{FAULT}.test_x7_takeover_during_delivery_restart",
)
EXPECTED_FAST = (
    ("harness_fixture", ("tests.integration.test_cw16_harness", "tests.dev_validation.test_runner")),
    ("launcher_skill_filter", ("tests.backend.test_paths_launcher", "tests.backend.test_workbench_skills")),
    ("installed_plain_bash", ("tests.dev_validation.test_fast_smoke",)),
)
FUTURE = {"q01_product_adapter_recheck", "new_storage_summary_raw_product_gates",
          "model_metadata", "product_full_dvl14"}


class PreciseRegistryIndependent(unittest.TestCase):
    """관측 경계: production selector/report consumer, process 대신 local pipe fixture."""

    def setUp(self):
        output = os.environ.get("WB_DVL_PRECISE_TEST_OUTPUT")
        if output:
            self.assertTrue(Path(output).is_absolute())
        temporary = tempfile.TemporaryDirectory(prefix="registry-fixture-", dir=output)
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.saved = self.parent / "saved"
        self.saved.mkdir(mode=0o700)
        self.commands = []
        self.raw_reports = {}

    def cell(self, outer="tmux", shell="bash"):
        return next(item for item in runner.PRECISE_ITEMS
                    if item.outer == outer and item.key == f"compat_{outer}_{shell}")

    def restored_legacy_scenarios(self):
        # Derive requirements from the existing legacy methods, independently of
        # runner.scenario_steps. X1's unittest delegates to X1Detach.steps.
        definitions = (
            (f"{POLICY}.test_p3_pause_interrupts_manager_turn", "policy-P3",
             "live_cw16_flow_policy.py", "Cw16PolicyScenarios", "test_p3_pause_interrupts_manager_turn", "step", 100),
            (f"{POLICY}.test_p6_restart_worker_stop_survivor_kill_host", "policy-P6",
             "live_cw16_flow_policy.py", "Cw16PolicyScenarios", "test_p6_restart_worker_stop_survivor_kill_host", "step", 0),
            (f"{FAULT}.test_x1_detach_with_active_work", "fault-x1",
             "live_cw16_fault.py", "X1Detach", "steps", "run", 515),
        )
        for test_id, scenario, filename, class_name, method_name, verb, wait in definitions:
            tree = ast.parse((runner.REPO / "tests" / "integration" / filename).read_text())
            cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
            method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == method_name)
            steps = tuple(node.value.args[0].value for node in method.body
                          if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                          and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == verb
                          and isinstance(node.value.args[0], ast.Constant))
            self.assertTrue(steps)
            item = next(item for item in runner.PRECISE_ITEMS if item.modules == (test_id,))
            yield item, scenario, steps, wait

    def fixture_item(self, item, index=0, report_dir=None, omp=None, invocation_id=None,
                     *, mutate=None, missing=False, collected=None, skipped=0, exit_code=0):
        """실제 selectors/control parser 사용. Popen은 process를 만들지 않는다."""
        report_dir = report_dir or self.saved
        real_mkdtemp = tempfile.mkdtemp

        def namespace(**kwargs):
            return real_mkdtemp(prefix="owned-", dir=self.parent)

        def popen(argv, **kwargs):
            self.commands.append((item.key, tuple(argv[8:])))
            invocation, key, nonce = argv[4:7]
            control_fd = kwargs["pass_fds"][0]
            count = len(item.modules) if collected is None else collected
            sequence = 0
            for _ in range(count):
                sequence += 1
                frame = {"version": 1, "invocation": invocation, "item": key, "nonce": nonce,
                         "sequence": sequence, "event": "completed"}
                os.write(control_fd, json.dumps(frame).encode() + b"\n")
            frame = {"version": 1, "invocation": invocation, "item": key, "nonce": nonce,
                     "sequence": sequence + 1, "event": "suite", "testsRun": count,
                     "skipped": skipped, "failures": int(exit_code == 1), "errors": 0,
                     "unexpectedSuccesses": 0}
            os.write(control_fd, json.dumps(frame).encode() + b"\n")
            owned = Path(kwargs["env"]["FIXTURE_NAMESPACE"])
            reports = owned / "reports" / item.key
            reports.mkdir(parents=True)
            for scenario, steps in item.scenario_steps.items():
                payload = {"run_id": item.key, "scenario": scenario, "result": "pass",
                           "steps": {step: {"status": "pass"} for step in steps},
                           "cleanup": {"root_removed": True, "shutdown_exit": 0},
                           "provider": {"errors": []}}
                if mutate:
                    mutate(payload)
                if not missing:
                    raw = json.dumps(payload).encode()
                    (reports / f"{scenario}.json").write_bytes(raw)
                    self.raw_reports[(item.key, scenario)] = raw

            class PipeProcess:
                pid = 99_999_999  # synthetic identity; never signalled or queried in /proc

                def __init__(self):
                    for name in ("stdout", "stderr"):
                        read_fd, write_fd = os.pipe()
                        os.close(write_fd)
                        setattr(self, name, os.fdopen(read_fd, "rb"))

                def poll(self):
                    return exit_code

                def wait(self, **kwargs):
                    return exit_code

            return PipeProcess()

        with ExitStack() as stack:
            stack.enter_context(patch.object(runner.tempfile, "mkdtemp", side_effect=namespace))
            stack.enter_context(patch.object(runner, "_base_env", side_effect=lambda home, ns, omp, it:
                                           {"FIXTURE_NAMESPACE": str(ns)}))
            stack.enter_context(patch.object(runner.subprocess, "Popen", side_effect=popen))
            stack.enter_context(patch.object(runner.harness, "ticks", return_value=1))
            stack.enter_context(patch.object(runner.harness, "processes_mentioning", return_value={}))
            stack.enter_context(patch.object(runner, "_owned_members", return_value={}))
            stack.enter_context(patch.object(runner, "_reap_adopted", return_value=[]))
            stack.enter_context(patch.object(runner, "_stop_owned", side_effect=AssertionError("no process exists")))
            return runner._run_item_scoped(item, index, report_dir, Path("/usr/bin/true"), invocation_id)

    def main_report(self, mode="precise", *, mutate=None, mutate_key="compat_tmux_bash", prior=None):
        report_dir = self.parent / f"main-{len(list(self.parent.iterdir()))}"

        def run(item, index, output, omp, invocation):
            return self.fixture_item(item, index, output, omp, invocation,
                                     mutate=mutate if item.key == mutate_key else None)

        args = ["--mode", mode, "--report-dir", str(report_dir), "--omp", "/usr/bin/true"]
        if prior:
            args.extend(("--prior-report", str(prior)))
        with patch.object(runner, "_run_item", side_effect=run), \
                patch.object(runner, "_input_manifest", return_value={"sha256": "a" * 64}), \
                patch.object(runner.harness, "tool_versions", return_value={"omp": "fixture-only"}), \
                patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "fixture-head")), \
                redirect_stdout(io.StringIO()):
            code = runner.main(args)
        return code, json.loads((report_dir / "report.json").read_text()), report_dir

    def test_precise_main_selects_exact_bash_sh_and_local_shell_fallback_legacy_ids(self):
        _, report, _ = self.main_report()
        launched = tuple(name for key, ids in self.commands if key not in dict(EXPECTED_FAST) for name in ids)
        self.assertEqual(launched, EXPECTED_IDS)
        self.assertEqual((len(launched), len(set(launched))), (19, 19))
        self.assertEqual(len(runner.PRECISE_ITEMS), 17)
        self.assertEqual(tuple(report["matrix"]["testIds"]), EXPECTED_IDS)
        self.assertEqual(report["matrix"]["shells"], ["bash", "dash"])
        self.assertEqual(report["matrix"]["shellLabels"], {"bash": "Bash", "dash": "sh(dash)"})
        self.assertEqual(report["matrix"]["selection"], ["SEL1", "SEL2", "SEL3"])
        self.assertEqual(report["matrix"]["decision"], "LG-D29")
        self.assertIn("historical failures/holds retained", report["matrix"]["shFallback"])
        self.assertNotIn("dashOuterCells", report["matrix"])

    def test_fast_main_keeps_existing_selection_and_excludes_precise_waits(self):
        code, report, _ = self.main_report("fast")
        self.assertEqual(tuple(self.commands), EXPECTED_FAST)
        self.assertEqual((code, report["result"]), (0, "passed"))
        self.assertNotIn("matrix", report)
        self.assertTrue(all(row["expectedWaitSeconds"] == 0 for row in report["items"]))

    def test_all_core_passes_cannot_close_future_product_gates(self):
        code, report, _ = self.main_report()
        self.assertEqual((code, report["result"]), (2, "inconclusive"))
        pending = [row for row in report["items"] if row["status"] == "not_run"]
        self.assertEqual({row["id"] for row in pending}, FUTURE)
        self.assertTrue(all(row["required"] and row["collectedTests"] == 0 for row in pending))
        core = [row for row in report["items"] if row["id"] not in FUTURE]
        self.assertEqual(len(core), 20)
        self.assertTrue(all(row["status"] == "passed" and row["collectedTests"] > 0 for row in core))
        self.assertEqual(report["firstFailure"]["item"], "q01_product_adapter_recheck")

    def test_precise_restored_legacy_steps_are_required_and_full_regression_remains_unverified(self):
        _, report, report_dir = self.main_report()
        omitted = {row["id"]: row for row in report["notRunCoverage"]}
        self.assertNotIn("X1", omitted)
        rows = {row["id"]: row for row in report["items"]}
        for item, scenario, steps, _ in self.restored_legacy_scenarios():
            with self.subTest(scenario=scenario):
                self.assertEqual(item.scenario_steps, {scenario: steps})
                self.assertEqual(item.allowed_na, ())
                row = rows[item.key]
                self.assertTrue(row["required"])
                self.assertEqual((row["status"], row["collectedTests"]), ("passed", 1))
                evidence, = row["scenarioEvidence"]
                self.assertEqual(evidence["scenario"], scenario)
                self.assertEqual(tuple(evidence["requiredSteps"]), steps)
                self.assertEqual(tuple(evidence["steps"]), steps)
                retained = Path(evidence["reportPath"])
                self.assertTrue(retained.is_relative_to(report_dir))
                self.assertEqual(evidence["sha256"], sha256(retained.read_bytes()).hexdigest())
        self.assertEqual(omitted["legacy_full_regression"]["status"], "not_run")
        self.assertEqual(omitted["VM_GUI_paid_model"]["status"], "not_run")
        self.assertIn("representative", report["scope"])
        self.assertEqual(report["targetSeconds"], 600)

    def test_c1_through_c7_consumed_and_fresh_report_retained_with_digest(self):
        for outer in ("plain", "tmux", "herdr"):
            for shell in ("bash", "dash"):
                with self.subTest(outer=outer, shell=shell):
                    item = self.cell(outer, shell)
                    row = self.fixture_item(item)
                    self.assertEqual((row["status"], row["collectedTests"], row["skippedTests"]), ("passed", 1, 0))
                    self.assertTrue(row["required"])
                    evidence, = row["scenarioEvidence"]
                    self.assertEqual(evidence["requiredSteps"], [f"C{i}" for i in range(1, 8)])
                    raw = self.raw_reports[(item.key, f"compat-{outer}-{shell}")]
                    self.assertEqual(Path(evidence["reportPath"]).read_bytes(), raw)
                    self.assertEqual(evidence["sha256"], sha256(raw).hexdigest())
                    self.assertTrue(row["cleanup"]["namespaceRemoved"])

    def test_expected_65_second_detach_and_review_wait_metadata_survive_execution(self):
        _, report, _ = self.main_report()
        rows = {row["id"]: row for row in report["items"]}
        for outer in ("plain", "tmux", "herdr"):
            for shell in ("bash", "dash"):
                item = self.cell(outer, shell)
                row = rows[item.key]
                self.assertEqual(item.env["WB_CW16_SHELLS"], shell)
                self.assertEqual(item.env["WB_CW16_DETACH_SECONDS"], "65")
                self.assertEqual(row["expectedWaitSeconds"], 65)
                self.assertGreaterEqual(row["watchdogSeconds"], 65 + 120)
        review = rows["review_busy_end"]
        self.assertGreaterEqual(review["expectedWaitSeconds"], 60)
        self.assertGreaterEqual(review["watchdogSeconds"], 120 + review["expectedWaitSeconds"])
        evidence, = review["scenarioEvidence"]
        self.assertIn("P1_reviews_60s_merge_when_busy", evidence["requiredSteps"])
        pause = rows["pause_end_no_replay"]
        self.assertEqual(pause["expectedWaitSeconds"], 90)
        self.assertGreaterEqual(pause["watchdogSeconds"], 120 + 90)
        for item, scenario, _, wait in self.restored_legacy_scenarios():
            with self.subTest(scenario=scenario):
                self.assertEqual(rows[item.key]["expectedWaitSeconds"], wait)
                self.assertEqual(rows[item.key]["watchdogSeconds"], 120 + wait)
                if scenario == "fault-x1":
                    self.assertEqual(wait, 320 + 3 * 65)
                    self.assertEqual(item.env["WB_CW16_DETACH_SECONDS"], "65")

    def test_restored_legacy_missing_required_step_cannot_pass(self):
        for item, scenario, steps, _ in self.restored_legacy_scenarios():
            with self.subTest(scenario=scenario):
                row = self.fixture_item(item, mutate=lambda payload: payload["steps"].pop(steps[1]))
                self.assertEqual((row["status"], row["collectedTests"]), ("inconclusive", 1))
                self.assertEqual(row["failureClass"], "legacy_evidence")

    def test_restored_legacy_failed_required_step_cannot_pass(self):
        for item, scenario, steps, _ in self.restored_legacy_scenarios():
            with self.subTest(scenario=scenario):
                row = self.fixture_item(item, mutate=lambda payload: payload["steps"][steps[1]].update(status="fail"))
                self.assertEqual((row["status"], row["collectedTests"]), ("failed", 1))
                self.assertEqual(row["failureClass"], "legacy_assertion_cleanup")

    def test_restored_legacy_unapproved_na_cannot_pass(self):
        for item, scenario, steps, _ in self.restored_legacy_scenarios():
            with self.subTest(scenario=scenario):
                row = self.fixture_item(item, mutate=lambda payload: payload["steps"][steps[1]].update(status="n/a"))
                self.assertEqual((row["status"], row["collectedTests"]), ("inconclusive", 1))
                self.assertEqual(row["failureClass"], "legacy_evidence")

    def test_restored_legacy_unknown_cleanup_cannot_pass(self):
        for item, scenario, _, _ in self.restored_legacy_scenarios():
            with self.subTest(scenario=scenario):
                row = self.fixture_item(item, mutate=lambda payload: payload.pop("cleanup"))
                self.assertEqual((row["status"], row["collectedTests"]), ("inconclusive", 1))
                self.assertEqual(row["failureClass"], "legacy_evidence")

    def assert_not_success(self, **kwargs):
        rows = []
        for shell in ("bash", "dash"):
            with self.subTest(shell=shell):
                row = self.fixture_item(self.cell(shell=shell), **kwargs)
                self.assertEqual(row["collectedTests"], 1)
                self.assertNotEqual(row["status"], "passed", row)
                self.assertTrue(row["cleanup"]["namespaceRemoved"])
                rows.append(row)
        self.assertEqual(rows[0]["status"], rows[1]["status"])
        return rows[-1]

    def test_successful_unittest_cannot_hide_c2_failure(self):
        row = self.assert_not_success(mutate=lambda p: p["steps"]["C2"].update(status="fail"))
        self.assertEqual((row["status"], row["failureClass"]), ("failed", "legacy_assertion_cleanup"))

    def test_successful_unittest_cannot_hide_missing_c2(self):
        row = self.assert_not_success(mutate=lambda p: p["steps"].pop("C2"))
        self.assertEqual(row["status"], "inconclusive")

    def test_successful_unittest_cannot_hide_not_run_c2(self):
        row = self.assert_not_success(mutate=lambda p: p["steps"]["C2"].update(status="not_run"))
        self.assertEqual(row["status"], "inconclusive")

    def test_successful_unittest_cannot_hide_missing_report(self):
        row = self.assert_not_success(missing=True)
        self.assertEqual(row["status"], "inconclusive")

    def test_successful_unittest_cannot_hide_cleanup_failure(self):
        row = self.assert_not_success(mutate=lambda p: p["cleanup"].update(shutdown_exit=1))
        self.assertEqual(row["status"], "failed")

    def test_successful_unittest_cannot_hide_scripted_provider_error(self):
        row = self.assert_not_success(mutate=lambda p: p["provider"].update(errors=["scripted provider disconnected"]))
        self.assertEqual(row["status"], "inconclusive")

    def test_plain_c6_na_does_not_allow_outer_c6_na(self):
        mutate = lambda p: p["steps"]["C6"].update(status="n/a")
        for shell in ("bash", "dash"):
            with self.subTest(shell=shell):
                plain = self.fixture_item(self.cell("plain", shell), mutate=mutate)
                outer = self.fixture_item(self.cell("tmux", shell), index=1, mutate=mutate)
                self.assertEqual(plain["status"], "passed")
                self.assertEqual(outer["status"], "inconclusive")

    def test_missing_outer_report_is_required_unknown_without_launch(self):
        for outer in ("tmux", "herdr"):
            for shell, label in (("bash", "Bash"), ("dash", "sh(dash)")):
                with self.subTest(outer=outer, shell=shell), \
                        patch.object(runner.harness, "find_tool", return_value=None), \
                        patch.object(runner, "_run_item_scoped", side_effect=AssertionError("must not launch")):
                    row = runner._run_item(self.cell(outer, shell), 0, self.saved, Path("/usr/bin/true"))
                self.assertEqual(row["id"], f"compat_{outer}_{shell}")
                self.assertEqual(row["reason"], f"required {outer}/{label} cell: outer is not installed")
                self.assertEqual((row["status"], row["collectedTests"]), ("not_run", 0))
                self.assertTrue(row["required"])
                self.assertEqual(runner._overall([row], None), ("inconclusive", 2))

    def test_unisolatable_tmux_keeps_failed_legacy_step_but_no_product_pass(self):
        def mutate(payload):
            payload["steps"]["C1"].update(status="fail", assertion="isolated tmux did not start: fixture refusal")
            for name in tuple(payload["steps"])[1:]:
                payload["steps"][name]["status"] = "not_run"
        for shell in ("bash", "dash"):
            row = self.fixture_item(self.cell(shell=shell), mutate=mutate, exit_code=1)
            self.assertEqual((row["status"], row["failureClass"]), ("inconclusive", "probe_environment"))
            self.assertEqual(row["scenarioEvidence"][0]["steps"]["C1"]["status"], "fail")

    def test_unisolatable_herdr_keeps_failed_legacy_step_but_no_product_pass(self):
        def mutate(payload):
            payload["steps"]["C1"].update(status="fail", assertion="herdr isolation preflight failed: fixture refusal")
        for shell in ("bash", "dash"):
            row = self.fixture_item(self.cell("herdr", shell), mutate=mutate, exit_code=1)
            self.assertEqual((row["status"], row["failureClass"]), ("inconclusive", "probe_environment"))

    def test_zero_collected_tests_cannot_pass_with_complete_legacy_report(self):
        for shell in ("bash", "dash"):
            row = self.fixture_item(self.cell(shell=shell), collected=0)
            self.assertEqual((row["status"], row["collectedTests"]), ("inconclusive", 0))
            self.assertEqual(row["failureClass"], "collection")

    def test_skipped_legacy_test_cannot_pass_with_complete_report(self):
        for shell in ("bash", "dash"):
            row = self.fixture_item(self.cell(shell=shell), skipped=1)
            self.assertEqual((row["status"], row["collectedTests"], row["skippedTests"]), ("inconclusive", 1, 1))

    def test_main_preserves_current_core_failure_ahead_of_future_pending(self):
        for shell in ("bash", "dash"):
            item_key = f"compat_tmux_{shell}"
            code, report, _ = self.main_report(mutate=lambda p: p["steps"]["C2"].update(status="fail"),
                                              mutate_key=item_key)
            self.assertEqual((code, report["result"]), (1, "failed"))
            self.assertEqual(report["firstFailure"]["item"], item_key)
            self.assertEqual(report["firstFailure"]["status"], "failed")

    def test_prior_first_failure_is_retained_when_current_core_is_green(self):
        _, first, _ = self.main_report(mutate=lambda p: p["steps"]["C2"].update(status="fail"))
        prior = self.parent / "prior.json"
        prior.write_text(json.dumps(first))
        code, current, _ = self.main_report(prior=prior)
        self.assertEqual(current["firstFailure"], first["firstFailure"])
        self.assertEqual((code, current["result"]), (1, "failed"))
        self.assertEqual(len(current["attemptChain"]), 2)
        self.assertEqual(current["attemptChain"][0]["attemptId"], first["attemptId"])


class PauseVisibleStateIndependent(unittest.TestCase):
    """P2 표시 조건만 검사하며 실제 pause 동작·시간은 runtime 시험에 남긴다."""

    def setUp(self):
        from tests.integration.live_cw16_flow_policy import PAUSED_UI_PATTERN
        from workbench.contracts.ui_v1 import ClientType
        from workbench.ui.product.model import ProductModel

        self.pattern = PAUSED_UI_PATTERN
        self.pause = ClientType.PAUSE
        self.model = ProductModel(object(), rows=40, cols=170)

    def screen_text(self):
        return "\n".join((*self.model.status_lines(), self.model.notice))

    def test_latest_paused_state_visible_despite_prior_pausing_response_notice(self):
        self.model._automation_result(self.pause, {"state": "pausing", "paused": True})
        self.assertIn("요청됨", self.model.notice)
        self.model.apply_snapshot({"automation": {"state": "paused", "paused": True}})
        visible = "\n".join((self.model.status_lines()[0], self.model.notice))
        self.assertIn("자동화: paused", self.model.status_lines()[0])
        self.assertIsNone(re.search(r"일시정지됨", visible))
        self.assertIsNotNone(re.search(self.pattern, visible))

    def test_requested_notice_and_pausing_state_do_not_confirm_completed_pause(self):
        self.model._automation_result(self.pause, {"state": "pausing", "paused": True})
        self.assertIn("요청됨", self.model.notice)
        self.assertIsNone(re.search(self.pattern, self.model.notice))
        self.assertIsNone(re.search(self.pattern, self.screen_text()))

    def test_paused_token_must_be_complete_current_automation_state(self):
        self.model._automation_result(self.pause, {"state": "pausing", "paused": True})
        for state in ("paused_error", "not_paused", "paused-pending", "paused.requested",
                      "unpaused", "pausing", "resuming", "active"):
            with self.subTest(state=state):
                self.model.apply_snapshot({"automation": {"state": state, "paused": True}})
                self.assertIsNone(re.search(self.pattern, self.screen_text()))
        self.assertIsNone(re.search(self.pattern, "자동화: paused"))

    def test_existing_korean_completed_pause_notice_is_still_accepted(self):
        self.model._automation_result(self.pause, {"state": "paused", "paused": True})
        self.assertEqual(self.model.notice, "자동화 일시정지됨")
        self.assertIsNotNone(re.search(self.pattern, self.model.notice))


if __name__ == "__main__":
    unittest.main()
