"""SPEC s1-derived CLI contracts, independently authored at parser/main boundary."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.backend.cli import main
from tests.backend.test_storage_status_cli_independent import observational_only


class ObservedPath:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = 0

    def lstat(self):
        self.calls += 1
        if self.failure:
            raise self.failure
        return object()


def row(kind="task", text="첫 줄\n둘째 줄"):
    identity = str(uuid4())
    source = {"schemaVersion": 1, "sourceKind": "event", "collectorEpoch": 1,
              "eventId": str(uuid4()), "taskId": str(uuid4()), "revision": 7}
    return {"entry": {"subject": {"schemaVersion": 1, "kind": kind, "id": identity},
            "kind": "cumulative", "recordedAt": "2026-10-18T00:00:00.000Z",
            "entryId": str(uuid4()), "body": {"facts": [text],
            "interpretations": ["판단 그대로"], "unknowns": ["불명 그대로"]},
            "sources": [{"source": item, "start": 2, "end": 19} for item in (source,
                {"sourceKind": "terminal", "taskId": source["taskId"], "revision": 7, "runId": str(uuid4())},
                {"sourceKind": "terminal", "commandId": str(uuid4())},
                {"sourceKind": "instruction", "messageId": str(uuid4())})],
            "sourceGaps": [{"source": source, "start": 19, "end": 24, "reason": "evicted"}]},
            "sourceDbUuids": [str(uuid4()), str(uuid4())]}


class IndependentImportantCliUxTests(unittest.TestCase):
    def invoke(self, action, result, *, path=None, json_output=False, kind="task", limit=2, page=None):
        before = deepcopy(result)
        stdout, stderr = StringIO(), StringIO()
        with ExitStack() as stack:
            observational_only(stack)
            reader = stack.enter_context(patch("workbench.storage.important_log.ImportantLogReader"))
            method = {"status": "status", "show": "show", "list": "list_subjects"}[action]
            getattr(reader.return_value, method).return_value = result
            stack.enter_context(patch("workbench.backend.cli._layout", return_value=SimpleNamespace(important_logs=path)))
            stack.enter_context(patch("workbench.backend.cli._running_snapshot", return_value={}))
            args = ["logs", "important", action]
            if action != "status":
                args += ["--limit", str(limit)] + (["--page", page] if page else [])
            subject = result.get("subject", {"kind": kind, "id": str(uuid4())})
            if action == "show":
                args += ["--task" if kind == "task" else "--command", subject["id"]]
            if json_output:
                args += ["--json"]
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(args)
            if action == "show":
                reader.return_value.show.assert_called_once_with(
                    {"schemaVersion": 1, "kind": kind, "id": subject["id"]}, limit=limit, after=page)
            elif action == "list":
                reader.return_value.list_subjects.assert_called_once_with(limit=limit, after=page)
            else:
                reader.return_value.status.assert_called_once_with()
        self.assertEqual(result, before, "human formatter must not rewrite response")
        return code, stdout.getvalue(), stderr.getvalue()

    def test_missing_is_conditional_unknown_and_metadata_is_once(self):
        path = ObservedPath(FileNotFoundError())
        response = {"complete": False, "databases": [], "unavailable": [{"dbUuid": None, "reason": "unavailable"}], "compatibility": None}
        code, out, err = self.invoke("status", response, path=path)
        self.assertEqual((code, path.calls), (1, 1))
        for expected in ("경로가 없습니다", "처음 사용하는 환경이라면", "첫 작업 기록", "DB 수: 미확인", "물리 크기: 미확인"):
            self.assertIn(expected, out)
        self.assertNotIn("0개", out)
        self.assertIn("DB 미확인", err)
        self.assertIn("unavailable", err)

    def test_present_busy_and_metadata_failure_never_claim_first_use(self):
        response = {"complete": False, "databaseCount": 1, "physicalBytes": 77,
                    "unavailable": [{"dbUuid": "known-db", "reason": "busy"}], "compatibility": None}
        for failure, expected in ((None, "경로가 있지만"), (PermissionError(), "존재 여부를 확인하지 못"),
                                  (OSError("unknown"), "존재 여부를 확인하지 못")):
            with self.subTest(failure=type(failure).__name__):
                path = ObservedPath(failure)
                code, out, err = self.invoke("status", response, path=path)
                self.assertEqual((code, path.calls), (1, 1))
                self.assertIn(expected, out)
                self.assertIn("77 B", out)
                self.assertNotIn("처음 사용하는", out)
                self.assertIn("DB known-db · 사유 busy", err)
                self.assertNotIn('[{"', err)

    def test_json_and_complete_status_do_not_observe_extra_path(self):
        for complete in (False, True):
            response = {"complete": complete, "databaseCount": 1, "physicalBytes": 0,
                        "active": "db", "databases": [], "compatibility": None,
                        "unavailable": [] if complete else [{"reason": "unavailable", "dbUuid": None}]}
            for as_json in (False, True):
                if not complete and not as_json:
                    continue
                path = ObservedPath(AssertionError("metadata query forbidden"))
                code, out, err = self.invoke("status", response, path=path, json_output=as_json)
                self.assertEqual(path.calls, 0)
                self.assertEqual(code, 0 if complete else 1)
                self.assertEqual(err, "")
                if as_json:
                    self.assertEqual(json.loads(out), response)
                else:
                    self.assertIn("1개", out)
                    self.assertIn("0 B", out)

    def test_show_row_binding_all_sections_identifiers_gaps_and_long_newlines(self):
        rows = [row("task", "A" * 5000 + "\n후행\n\n끝\x1b\t내용"), row("command", "COMMAND-ONLY")]
        result = {"complete": True, "entries": rows, "nextAfter": "cursor-original"}
        code, out, err = self.invoke("show", result)
        self.assertEqual((code, err), (0, ""))
        self.assertLess(out.index(rows[0]["entry"]["entryId"]), out.index(rows[1]["entry"]["entryId"]))
        self.assertIn("command " + rows[1]["entry"]["subject"]["id"], out)
        task_text, command_text = out.split("command " + rows[1]["entry"]["subject"]["id"])
        self.assertNotIn("COMMAND-ONLY", task_text)
        self.assertNotIn("A" * 5000, command_text)
        self.assertIn("A" * 5000, task_text)
        self.assertIn("후행\n    \n    끝", task_text)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\t", out)
        for item in rows:
            entry = item["entry"]
            for text in (entry["subject"]["id"], entry["kind"], entry["recordedAt"], entry["entryId"],
                         "사실:", "해석:", "미확인:", "출처:", "누락 범위:", "evicted", "[2, 19)", "[19, 24)"):
                self.assertIn(text, out)
            for span in entry["sources"]:
                for name, value in span["source"].items():
                    if name not in ("schemaVersion", "collectorEpoch"):
                        self.assertIn(str(value), out)
            for identity in item["sourceDbUuids"]:
                self.assertIn(identity, out)
        self.assertIn("다음 page: cursor-original", out)

    def test_partial_rows_and_empty_never_claim_record_absence(self):
        for action in ("list", "show"):
            for has_rows in (False, True):
                with self.subTest(action=action, rows=has_rows):
                    data = {"complete": False, "unavailable": [{"dbUuid": "broken-db", "reason": "unavailable"}]}
                    data["entries" if action == "show" else "subjects"] = ([row()] if action == "show" else [
                        {"subject": {"kind": "command", "id": "known-command"}, "entryCount": 1, "sourceDbUuids": ["good-db"]}]) if has_rows else []
                    code, out, err = self.invoke(action, data)
                    self.assertEqual(code, 1)
                    self.assertNotIn("기록 없음", out)
                    self.assertIn("DB broken-db · 사유 unavailable", err)
                    self.assertNotIn("broken-db", out)
                    if has_rows:
                        self.assertTrue(out.strip())

    def test_json_envelope_order_page_exit_channels_and_complete_empty(self):
        for kind in ("task", "command"):
            result = {"complete": False, "subject": {"kind": kind, "id": str(uuid4())},
                      "entries": [row(kind), row(kind)], "nextAfter": "opaque-page",
                      "unavailable": [{"dbUuid": "failed", "reason": "busy"}], "extra": {"keep": [1, None]}}
            code, out, err = self.invoke("show", result, kind=kind, json_output=True, limit=3, page="prior-page")
            self.assertEqual((code, json.loads(out), err), (1, result, ""))
        for action, key in (("show", "entries"), ("list", "subjects")):
            result = {"complete": True, key: [], "nextAfter": None, "unavailable": []}
            code, out, err = self.invoke(action, result)
            self.assertEqual((code, out.strip(), err), (0, "기록 없음", ""))

    def test_usage_explains_full_envelope_migration(self):
        usage = (Path(__file__).resolve().parents[2] / "docs/features/dev-validation-log-retention/storage-operations/USAGE.md").read_text()
        for text in ("더 이상 JSON lines", "--json", "entries[]", "sourceDbUuids", "nextAfter", "별도", "활성화"):
            self.assertIn(text, usage)


if __name__ == "__main__":
    unittest.main()
