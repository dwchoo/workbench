"""Requirement-derived status CLI checks at the real parser/main boundary."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbench.backend.cli import main
from workbench.contracts.ui_v1 import ClientType


def observational_only(stack):
    """Fail if a status query crosses a public execution or mutation boundary."""
    for target in (
        "subprocess.Popen",
        "workbench.backend.summary_compat_identity.observe_configured",
        "workbench.backend.summary_compatibility.SummaryCompatibility.verify_scripted",
        "workbench.backend.summary_compatibility.SummaryCompatibility.verify_reference",
        "workbench.backend.summary_compatibility.SummaryCompatibility.admission",
        "workbench.backend.raw_rotation.RawRotationPolicy.activate",
        "workbench.storage.important_log.catalog.ImportantLogCatalog.prepare_and_delete_archive",
        "workbench.storage.important_log.maintenance.ImportantLogMaintenance.maintenance_pass",
    ):
        stack.enter_context(patch(target, side_effect=AssertionError("status execution: " + target)))


class IndependentStorageStatusCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="lso-cli-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "data"

    def invoke(self, command, response, *, json_output=False):
        stdout, stderr = StringIO(), StringIO()
        original = deepcopy(response)
        with ExitStack() as stack:
            observational_only(stack)
            if command == "raw-status":
                client = Mock()
                client.__enter__ = Mock(return_value=client)
                client.__exit__ = Mock(return_value=False)
                client.request.return_value = {"ok": True, "result": response}
                stack.enter_context(patch("workbench.backend.cli.UiClient", return_value=client))
                args = ["logs", command]
            else:
                reader = stack.enter_context(patch("workbench.storage.important_log.ImportantLogReader"))
                reader.return_value.status.return_value = response
                stack.enter_context(patch("workbench.backend.cli._running_snapshot", return_value={
                    "important_logs": {"compatibility": response.get("compatibility")}}))
                args = ["logs", "important", "status"]
            args += ["--data-dir", str(self.root)] + (["--json"] if json_output else [])
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(args)
            if command == "raw-status":
                client.request.assert_called_once_with(ClientType.RAW_STATUS)
            else:
                reader.return_value.status.assert_called_once_with()
                reader.return_value.list_subjects.assert_not_called()
                reader.return_value.show.assert_not_called()
        self.assertEqual(response, original, "formatting must not rewrite the response")
        self.assertFalse(self.root.exists(), "status must not allocate the data root")
        return code, stdout.getvalue(), stderr.getvalue()

    def test_live_raw_uses_observed_limits_and_keeps_protected_activation_distinct(self):
        response = {
            "activation": "enabled",
            "rotation": {"activated": True, "newSourcePolicy": "legacy-stop",
                         "reason": "confirmed_safety_failure", "observationOnly": True},
            "legacy": {"state": "available", "run_bytes": 37, "terminal_bytes": 41,
                       "project_limit_bytes": 701, "run_limit_bytes": 129, "terminal_limit_bytes": 257},
            "segmented_v1": {"state": "available", "raw_bytes": 83,
                             "metadata_bytes": 97, "raw_limit_bytes": 4096},
        }
        code, output, error = self.invoke("raw-status", response)
        self.assertEqual((code, error), (0, ""))
        for expected in ("활성화 결정: 활성화됨", "신규 source 정책: legacy-stop",
                         "confirmed_safety_failure", "legacy 실험: 37 B", "project 한도 701 B",
                         "129 B/run", "legacy terminal: 41 B", "명령당 257 B",
                         "83 B / 4,096 B", "metadata: 97 B", "전체 용량 상한이 아닙니다",
                         "logs important status"):
            self.assertIn(expected, output)
        self.assertNotIn("500,000,000", output, "observed limits take priority over defaults")
        terminal = next(line for line in output.splitlines() if line.startswith("legacy terminal:"))
        self.assertNotIn("합계 한도", terminal)
        self.assertNotIn("project 한도", terminal)
        code, output, error = self.invoke("raw-status", response, json_output=True)
        self.assertEqual((code, json.loads(output), error), (0, response, ""))

    def test_backend_refusal_keeps_status_reason_visible_and_json_exit_unchanged(self):
        # A real RAW_STATUS refusal is converted by cmd_raw_read, not injected
        # into the formatter: its original failure must remain visible.
        for as_json in (True, False):
            with self.subTest(json=as_json), ExitStack() as stack:
                observational_only(stack)
                client = Mock()
                client.__enter__ = Mock(return_value=client)
                client.__exit__ = Mock(return_value=False)
                client.request.return_value = {"ok": False, "reason": "internal_error"}
                stack.enter_context(patch("workbench.backend.cli.UiClient", return_value=client))
                stdout, stderr = StringIO(), StringIO()
                args = ["logs", "raw-status", "--data-dir", str(self.root)]
                if as_json:
                    args.append("--json")
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(args)
                client.request.assert_called_once_with(ClientType.RAW_STATUS)
                self.assertEqual((code, stderr.getvalue()), (1, ""))
                self.assertFalse(self.root.exists())
                if as_json:
                    self.assertEqual(json.loads(stdout.getvalue()),
                                     {"status": "unavailable", "reason": "internal_error"})
                else:
                    self.assertIn("internal_error", stdout.getvalue())
                    self.assertIn("unavailable", stdout.getvalue())

    def test_raw_missing_store_has_default_budget_without_zero_usage_claim(self):
        response = {"activation": "disabled", "rotation": {"activated": False,
                    "newSourcePolicy": "legacy-stop", "reason": "activation_required"},
                    "legacy": {"state": "available", "run_bytes": 0, "terminal_bytes": 0},
                    "segmented_v1": {"state": "not_allocated"}}
        code, output, _ = self.invoke("raw-status", response)
        self.assertEqual(code, 0)
        self.assertIn("미생성", output)
        self.assertIn("500,000,000 B", output)
        self.assertIn("metadata 사용량: 미확인", output)
        self.assertNotIn("segmented-v1 raw: 0", output)

    def test_partial_unknown_raw_keeps_known_bytes_and_existing_failure_exit(self):
        response = {"status": "unknown", "legacy": {"state": "partial", "run_bytes": 19,
                    "terminal_bytes": None}, "segmented_v1": {"state": "unavailable",
                    "reason": "SegmentIntegrityError"}, "rotation": {}}
        for as_json in (False, True):
            with self.subTest(json=as_json):
                code, output, _ = self.invoke("raw-status", response, json_output=as_json)
                self.assertEqual(code, 1)
                if as_json:
                    self.assertEqual(json.loads(output), response)
                else:
                    for expected in ("partial", "확인된 19 B", "legacy terminal: 미확인",
                                     "unavailable", "raw 사용량: 미확인", "metadata 사용량: 미확인",
                                     "SegmentIntegrityError", "활성화 결정: 미확인"):
                        self.assertIn(expected, output)
                    self.assertNotIn("raw 사용량: 0 B", output)
                    self.assertNotIn("legacy terminal: 0 B", output)

    def test_important_db_roles_physical_sizes_and_retention_meanings(self):
        response = {"active": "active-id", "databaseCount": 3, "physicalBytes": 720,
                    "cleanupNeeded": True, "complete": True, "compatibility": None,
                    "databases": [{"dbUuid": identity, "role": role, "physicalBytes": size,
                                   "availability": availability}
                                  for identity, role, size, availability in (
                                      ("active-id", "active", 100, "unknown"),
                                      ("archive-id", "archive", 620, "unknown"),
                                      ("orphan-id", "orphan", None, "unavailable"))]}
        code, output, error = self.invoke("important", response)
        self.assertEqual((code, error), (0, ""))
        for expected in ("3개", "총 물리 크기 720 B", "role=active", "role=archive", "role=orphan",
                         "DB orphan-id · role=orphan · 물리 크기 미확인", "상태=unknown",
                         "여러 중요 로그 DB", "250,000,000 B 논리 용량 목표", "전체 물리 용량 상한이 아닙니다",
                         "120일", "보관 종료 시점", "용량 압박 시 더 일찍", "archive DB는 자동 삭제되지",
                         "delete-db --db UUID", "orphan은 확인 불가 상태"):
            self.assertIn(expected, output)
        self.assertNotIn("논리 사용량: 100", output)
        code, output, error = self.invoke("important", response, json_output=True)
        self.assertEqual((code, json.loads(output), error), (0, response, ""))

    def test_partial_important_has_no_invented_count_or_bytes_and_json_exit_unchanged(self):
        response = {"active": None, "generation": None, "databases": [], "complete": False,
                    "unavailable": [{"dbUuid": None, "reason": "busy"}], "compatibility": None}
        for as_json in (False, True):
            with self.subTest(json=as_json):
                code, output, error = self.invoke("important", response, json_output=as_json)
                self.assertEqual(code, 1)
                if as_json:
                    self.assertEqual(json.loads(output), response)
                    self.assertEqual(error, "")
                else:
                    self.assertIn("DB 수: 미확인 · 물리 크기: 미확인", output)
                    self.assertIn("busy", error)
                    self.assertNotIn("0개", output)
                    self.assertNotIn("물리 크기: 0 B", output)


if __name__ == "__main__":
    unittest.main()
