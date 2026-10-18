"""Focused human and JSON output checks for storage status CLI commands."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench.backend.cli import main


class _FakeUiClient:
    def __init__(self, answer):
        self.answer = answer

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def request(self, *_args, **_kwargs):
        return self.answer


class StorageStatusCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / "data"

    def run_raw(self, result, *, json_output=False):
        output = StringIO()
        args = ["logs", "raw-status", "--data-dir", str(self.data_dir)]
        if json_output:
            args.append("--json")
        with patch("workbench.backend.cli.UiClient",
                   return_value=_FakeUiClient({"ok": True, "result": result})), \
                redirect_stdout(output):
            code = main(args)
        return code, output.getvalue()

    def test_raw_human_output_separates_legacy_limits_and_unallocated_v2(self):
        result = {
            "legacy": {
                "project_limit_bytes": 512 * 1024 * 1024,
                "run_limit_bytes": 64 * 1024 * 1024,
                "terminal_limit_bytes": 64 * 1024 * 1024,
                "run_bytes": 0,
                "terminal_bytes": 27459,
                "state": "available",
            },
            "segmented_v1": {"state": "not_allocated"},
            "activation": "disabled",
            "rotation": {
                "activated": False,
                "newSourcePolicy": "legacy-stop",
                "reason": "activation_required",
            },
        }

        code, output = self.run_raw(result)

        self.assertEqual(code, 0)
        self.assertIn("활성화 결정: 비활성", output)
        self.assertIn("실제 신규 source 정책: legacy-stop", output)
        self.assertIn("activation_required", output)
        self.assertIn("legacy 실험", output)
        self.assertIn("536,870,912 B", output)
        self.assertIn("67,108,864 B/run", output)
        self.assertIn("legacy terminal", output)
        self.assertIn("27,459 B", output)
        self.assertIn("명령당 67,108,864 B", output)
        self.assertIn("segmented-v1 저장소: 미생성", output)
        self.assertIn("500,000,000 B", output)
        self.assertIn("metadata 사용량: 미확인", output)
        self.assertIn("전체 용량 상한이 아닙니다", output)
        self.assertIn("logs important status", output)
        self.assertFalse(self.data_dir.exists())

    def test_raw_human_output_keeps_partial_and_protected_policy_explicit(self):
        result = {
            "legacy": {
                "project_limit_bytes": 512 * 1024 * 1024,
                "run_limit_bytes": 64 * 1024 * 1024,
                "terminal_limit_bytes": 64 * 1024 * 1024,
                "run_bytes": 123,
                "terminal_bytes": 5,
                "state": "partial",
            },
            "segmented_v1": {"state": "unavailable", "reason": "PermissionError"},
            "activation": "enabled",
            "rotation": {
                "activated": True,
                "newSourcePolicy": "legacy-stop",
                "reason": "revalidation_required",
            },
        }

        code, output = self.run_raw(result)

        self.assertEqual(code, 0)
        self.assertIn("활성화 결정: 활성화됨", output)
        self.assertIn("실제 신규 source 정책: legacy-stop", output)
        self.assertIn("revalidation_required", output)
        self.assertIn("legacy 조회 상태: partial", output)
        self.assertIn("확인된 123 B", output)
        self.assertIn("segmented-v1 조회 상태: unavailable", output)
        self.assertIn("raw 사용량: 미확인", output)
        self.assertNotIn("raw 0 B", output)

    def test_raw_json_output_remains_the_original_response(self):
        result = {"status": "unknown", "legacy": {"state": "partial", "run_bytes": 7},
                  "segmented_v1": {"state": "not_allocated"},
                  "activation": "disabled"}

        code, output = self.run_raw(result, json_output=True)

        self.assertEqual(code, 1)
        self.assertEqual(output, json.dumps(result, sort_keys=True, ensure_ascii=False) + "\n")

    def run_important(self, result, *, json_output=False):
        output = StringIO()
        errors = StringIO()
        args = ["logs", "important", "status", "--data-dir", str(self.data_dir)]
        if json_output:
            args.append("--json")
        reader = type("FakeReader", (), {"status": lambda _self: result})()
        with patch("workbench.storage.important_log.ImportantLogReader", return_value=reader), \
                patch("workbench.backend.cli._running_snapshot", return_value=None), \
                redirect_stdout(output), redirect_stderr(errors):
            code = main(args)
        return code, output.getvalue() + errors.getvalue()

    def test_important_human_output_lists_db_roles_and_preserves_unknown_orphan(self):
        active = "00000000-0000-4000-8000-000000000001"
        archive = "00000000-0000-4000-8000-000000000002"
        orphan = "00000000-0000-4000-8000-000000000003"
        result = {
            "active": active,
            "generation": 1,
            "databaseCount": 3,
            "physicalBytes": 196608,
            "cleanupNeeded": True,
            "orphans": [orphan],
            "databases": [
                {"dbUuid": active, "role": "active", "physicalBytes": 65536,
                 "availability": "unknown"},
                {"dbUuid": archive, "role": "archive", "physicalBytes": 65536,
                 "availability": "unknown"},
                {"dbUuid": orphan, "role": "orphan", "physicalBytes": 65536,
                 "availability": "unavailable"},
            ],
            "unavailable": [],
            "complete": True,
        }

        code, output = self.run_important(result)

        self.assertEqual(code, 0)
        self.assertIn("important DB 3개", output)
        self.assertIn("총 물리 크기 196,608 B", output)
        for identity, role in ((active, "active"), (archive, "archive"), (orphan, "orphan")):
            self.assertIn(identity, output)
            self.assertIn(f"role={role}", output)
            self.assertIn("65,536 B", output)
        self.assertIn("250,000,000 B 논리 용량 목표", output)
        self.assertIn("전체 물리 용량 상한이 아닙니다", output)
        self.assertIn("120일", output)
        self.assertIn("보관 종료 시점", output)
        self.assertIn("용량 압박 시 더 일찍", output)
        self.assertIn("archive DB는 자동 삭제되지 않습니다", output)
        self.assertIn("delete-db --db UUID", output)
        self.assertIn("orphan은 확인 불가 상태", output)
        self.assertNotIn("orphan 삭제 후보", output)

    def test_important_partial_status_does_not_invent_zero_bytes(self):
        result = {"active": None, "generation": None, "databases": [],
                  "unavailable": [{"dbUuid": None, "reason": "busy"}], "complete": False}

        code, output = self.run_important(result)

        self.assertEqual(code, 1)
        self.assertIn("important DB 상태: 일부 또는 확인 불가", output)
        self.assertIn("물리 크기: 미확인", output)
        self.assertIn("조회 실패", output)
        self.assertNotIn("물리 크기 0 B", output)

    def test_important_json_output_remains_the_original_response(self):
        result = {"active": "00000000-0000-4000-8000-000000000001",
                  "databaseCount": 1, "physicalBytes": 42, "cleanupNeeded": False,
                  "databases": [], "unavailable": [], "complete": True,
                  "compatibility": None}

        code, output = self.run_important(result, json_output=True)

        self.assertEqual(code, 0)
        self.assertEqual(output, json.dumps(result, sort_keys=True, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    unittest.main()
