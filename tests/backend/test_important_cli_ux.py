"""Human-facing important-log CLI behavior at the parser/main boundary."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench.backend.cli import main


TASK_ID = "00000000-0000-4000-8000-000000000001"
ENTRY_ID = "00000000-0000-4000-8000-000000000002"
DB_ID = "00000000-0000-4000-8000-000000000003"
MESSAGE_ID = "00000000-0000-4000-8000-000000000004"
COMMAND_ID = "00000000-0000-4000-8000-000000000005"


class ImportantCliUxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="uxcli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.important_path = self.root / "important-logs"

    def invoke(self, action, response, *, subject=None, json_output=False):
        stdout, stderr = StringIO(), StringIO()
        args = ["logs", "important", action, "--data-dir", str(self.root)]
        if subject is not None:
            args += ["--task", subject]
        if json_output:
            args.append("--json")
        with ExitStack() as stack:
            reader = stack.enter_context(
                patch("workbench.storage.important_log.ImportantLogReader"))
            if action == "status":
                reader.return_value.status.return_value = response
            elif action == "show":
                reader.return_value.show.return_value = response
            elif action == "list":
                reader.return_value.list_subjects.return_value = response
            else:
                self.fail(f"unsupported action in test helper: {action}")
            stack.enter_context(patch("workbench.backend.cli._running_snapshot", return_value=None))
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(args)
        return code, stdout.getvalue(), stderr.getvalue(), reader

    def test_missing_status_path_gets_conditional_first_use_hint_without_zero_claim(self):
        response = {"active": None, "generation": None, "databases": [],
                    "databaseCount": None, "physicalBytes": None,
                    "unavailable": [{"dbUuid": None, "reason": "unavailable"}],
                    "complete": False}

        code, output, error, reader = self.invoke("status", response)

        self.assertEqual(code, 1)
        reader.return_value.status.assert_called_once_with()
        self.assertIn("저장소 경로가 없습니다", output)
        self.assertIn("처음 사용하는 환경이라면", output)
        self.assertIn("첫 작업 기록이 저장된 뒤", output)
        self.assertIn("DB 수: 미확인", output)
        self.assertIn("물리 크기: 미확인", output)
        self.assertIn("DB 미확인 · 사유 unavailable", error)
        self.assertFalse(self.root.exists(), "status must not create a data directory")

    def test_present_status_path_is_not_mislabeled_as_first_use(self):
        self.important_path.mkdir(parents=True)
        response = {"active": None, "generation": None, "databases": [],
                    "databaseCount": None, "physicalBytes": None,
                    "unavailable": [{"dbUuid": DB_ID, "reason": "busy"}],
                    "complete": False}

        code, output, error, _ = self.invoke("status", response)

        self.assertEqual(code, 1)
        self.assertIn("저장소 경로가 있지만 DB 정보를 조회하지 못했습니다", output)
        self.assertNotIn("처음 사용하는 환경이라면", output)
        self.assertIn(f"DB {DB_ID} · 사유 busy", error)

    def test_human_status_path_observation_failure_stays_unknown(self):
        response = {"active": None, "generation": None, "databases": [],
                    "databaseCount": None, "physicalBytes": None,
                    "unavailable": [{"dbUuid": None, "reason": "unavailable"}],
                    "complete": False}
        with patch.object(Path, "lstat", side_effect=PermissionError("fixture")):
            code, output, _error, _reader = self.invoke("status", response)

        self.assertEqual(code, 1)
        self.assertIn("저장소 경로 존재 여부를 확인하지 못했습니다", output)
        self.assertNotIn("저장소 경로가 없습니다", output)

    def test_json_status_skips_extra_path_observation_and_keeps_original_envelope(self):
        response = {"active": None, "generation": None, "databases": [],
                    "databaseCount": None, "physicalBytes": None,
                    "unavailable": [{"dbUuid": None, "reason": "busy"}],
                    "complete": False, "compatibility": None}

        with patch.object(Path, "lstat", side_effect=AssertionError("extra path observation")):
            code, output, error, _reader = self.invoke("status", response, json_output=True)

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), response)
        self.assertEqual(error, "")

    def test_complete_human_status_skips_extra_path_observation(self):
        response = {"active": DB_ID, "generation": 1, "databaseCount": 1,
                    "physicalBytes": 12, "databases": [], "unavailable": [],
                    "complete": True}

        with patch.object(Path, "lstat", side_effect=AssertionError("extra path observation")):
            code, output, error, _reader = self.invoke("status", response)

        self.assertEqual((code, error), (0, ""))
        self.assertIn("important DB 1개", output)

    def test_show_formats_subject_body_sources_gaps_and_database_without_json_lines(self):
        task_source = {"schemaVersion": 1, "sourceKind": "task_spec", "collectorEpoch": 1,
                       "taskId": TASK_ID, "revision": 2, "messageId": MESSAGE_ID}
        response = {
            "subject": {"schemaVersion": 1, "kind": "task", "id": TASK_ID},
            "entries": [{
                "entry": {"schemaVersion": 1, "entryId": ENTRY_ID,
                          "subject": {"schemaVersion": 1, "kind": "task", "id": TASK_ID},
                          "kind": "cumulative", "recordedAt": "2026-10-18T10:00:00Z",
                          "sources": [{"source": task_source, "start": 0, "end": 10,
                                       "digest": "a" * 64}],
                          "sourceGaps": [{"source": task_source, "start": 10, "end": 15,
                                          "reason": "storage_gap"}],
                          "body": {"schemaVersion": 1,
                                   "facts": ["첫 줄\n둘째 줄", "다른 사실"],
                                   "interpretations": ["해석 내용"],
                                   "unknowns": ["미확인 내용"]}},
                "sourceDbUuids": [DB_ID],
            }],
            "nextAfter": None, "unavailable": [], "complete": True,
        }

        code, output, error, reader = self.invoke("show", response, subject=TASK_ID)

        self.assertEqual((code, error), (0, ""))
        reader.return_value.show.assert_called_once()
        self.assertIn(f"Task {TASK_ID}", output)
        self.assertIn("cumulative", output)
        self.assertIn("2026-10-18T10:00:00Z", output)
        self.assertIn(f"기록 ID: {ENTRY_ID}", output)
        self.assertIn("사실:\n  - 첫 줄\n    둘째 줄", output)
        self.assertIn("해석:\n  - 해석 내용", output)
        self.assertIn("미확인:\n  - 미확인 내용", output)
        self.assertIn("task_spec", output)
        self.assertIn(f"Task ID={TASK_ID}", output)
        self.assertIn("revision=2", output)
        self.assertIn(f"message ID={MESSAGE_ID}", output)
        self.assertIn("범위 [0, 10)", output)
        self.assertIn("storage_gap", output)
        self.assertIn("범위 [10, 15)", output)
        self.assertIn(f"DB UUID: {DB_ID}", output)
        self.assertFalse(output.lstrip().startswith("{"))

    def test_show_labels_command_subject_and_source_identity(self):
        command_source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                          "commandId": COMMAND_ID}
        response = {
            "subject": {"schemaVersion": 1, "kind": "command", "id": COMMAND_ID},
            "entries": [{
                "entry": {"schemaVersion": 1, "entryId": ENTRY_ID,
                          "subject": {"schemaVersion": 1, "kind": "command", "id": COMMAND_ID},
                          "kind": "event", "recordedAt": "2026-10-18T10:00:00Z",
                          "sources": [{"source": command_source, "start": 0, "end": 3,
                                       "digest": "b" * 64}],
                          "sourceGaps": [],
                          "body": {"schemaVersion": 1, "facts": ["command 결과"],
                                   "interpretations": [], "unknowns": []}},
                "sourceDbUuids": [DB_ID],
            }],
            "nextAfter": None, "unavailable": [], "complete": True,
        }

        code, output, error, reader = self.invoke("show", response, subject=COMMAND_ID)

        self.assertEqual((code, error), (0, ""))
        reader.return_value.show.assert_called_once()
        self.assertIn(f"command {COMMAND_ID}", output)
        self.assertIn("terminal", output)
        self.assertIn(f"command ID={COMMAND_ID}", output)
        self.assertIn("command 결과", output)

    def test_show_json_keeps_full_existing_response_and_partial_warning_is_human_readable(self):
        response = {"subject": {"schemaVersion": 1, "kind": "task", "id": TASK_ID},
                    "entries": [], "nextAfter": "next-token",
                    "unavailable": [{"dbUuid": DB_ID, "reason": "busy"}],
                    "complete": False}

        code, output, error, _reader = self.invoke("show", response, subject=TASK_ID,
                                                   json_output=True)

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), response)
        self.assertEqual(error, "")

        code, output, error, _reader = self.invoke("show", response, subject=TASK_ID)
        self.assertEqual(code, 1)
        self.assertIn("다음 page: next-token", output)
        self.assertIn("부분 결과/조회 실패", error)
        self.assertIn(f"DB {DB_ID} · 사유 busy", error)
        self.assertNotIn("{\"dbUuid\"", error)

    def test_list_partial_keeps_rows_and_formats_db_reason_without_claiming_empty(self):
        response = {"subjects": [{"subject": {"schemaVersion": 1, "kind": "task", "id": TASK_ID},
                                  "entryCount": 1, "sourceDbUuids": [DB_ID]}],
                    "nextAfter": None,
                    "unavailable": [{"dbUuid": DB_ID, "reason": "busy"}],
                    "complete": False}

        code, output, error, reader = self.invoke("list", response)

        self.assertEqual(code, 1)
        reader.return_value.list_subjects.assert_called_once()
        self.assertIn(f"task {TASK_ID} · 1 entries · DB {DB_ID}", output)
        self.assertNotIn("기록 없음", output)
        self.assertIn(f"DB {DB_ID} · 사유 busy", error)


if __name__ == "__main__":
    unittest.main()
