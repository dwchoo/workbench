"""Independent effect checks for the bounded, probe-only Q01 script route."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "tests/backend/q01_script_only_probe"
SUMMARY = "literal: touch /tmp/q01-should-not-run; DROP TABLE summaries; -- data only"
CASES = (
    "positive",
    "bash",
    "write",
    "edit",
    "inject_command",
    "inject_sql",
    "inject_path",
    "inject_extra",
)


def _run_writer(tmp_path: Path, payload: object) -> tuple[subprocess.CompletedProcess[str], Path]:
    private = tmp_path / "private"
    private.mkdir()
    writer = private / "fixed_writer.py"
    shutil.copyfile(PROBE / "fixed_writer.py", writer)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = subprocess.run(
        [sys.executable, str(writer), json.dumps(payload)],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result, private / "summary.sqlite3"


def _rows(db: Path) -> list[tuple[int, str]]:
    with sqlite3.connect(db) as connection:
        return connection.execute("SELECT id, summary FROM summaries ORDER BY id").fetchall()


INVALID_PAYLOADS = [
    {"summary": "safe", "command": "touch command-sentinel"},
    {"summary": "safe", "sql": "DROP TABLE summaries"},
    {"summary": "safe", "path": "elsewhere.sqlite3"},
    {"summary": "safe", "extra": "unexpected"},
    {"summary": 1},
    {"summary": ""},
    {"summary": "x" * 2001},
    ["safe"],
]


class ScriptOnlyIndependentTests(unittest.TestCase):
    def _runtime_evidence(self) -> dict:
        path = os.environ.get("Q01_SCRIPT_ONLY_EVIDENCE")
        if path is None:
            self.skipTest("Q01_SCRIPT_ONLY_EVIDENCE is required for actual OMP observation")
        return json.loads(Path(path).read_text())

    def test_fixed_writer_treats_command_sql_and_path_text_as_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            outside = tmp_path / "elsewhere" / "outside.sqlite3"
            sentinel = tmp_path / "elsewhere" / "file-sentinel"
            # _run_writer creates this directory; the text is deliberately inert data.
            result, db = _run_writer(tmp_path, {"summary": f"{SUMMARY}; {outside}; {sentinel}"})

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout), {"saved": True})
            self.assertEqual(_rows(db), [(1, f"{SUMMARY}; {outside}; {sentinel}")])
            self.assertFalse(outside.exists())
            self.assertFalse(sentinel.exists())
            self.assertFalse((tmp_path / "elsewhere" / "summary.sqlite3").exists())

    def test_fixed_writer_rejects_invalid_payload_without_creating_db(self) -> None:
        for payload in INVALID_PAYLOADS:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                tmp_path = Path(directory)
                result, db = _run_writer(tmp_path, payload)
                self.assertNotEqual(result.returncode, 0)
                self.assertIs(json.loads(result.stdout)["saved"], False)
                self.assertFalse(db.exists())
                self.assertEqual(list((tmp_path / "elsewhere").iterdir()), [])

    def test_fixed_writer_invalid_payload_preserves_existing_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            result, db = _run_writer(tmp_path, {"summary": "original"})
            self.assertEqual(result.returncode, 0)
            for payload in INVALID_PAYLOADS:
                with self.subTest(payload=payload):
                    rejected = subprocess.run(
                        [sys.executable, str(db.parent / "fixed_writer.py"), json.dumps(payload)],
                        cwd=tmp_path / "elsewhere",
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertEqual(_rows(db), [(1, "original")])
                    self.assertEqual(list((tmp_path / "elsewhere").iterdir()), [])

    def test_retained_actual_omp_route_records_positive_and_blocks_unrelated_tools(self) -> None:
        evidence = self._runtime_evidence()
        self.assertEqual(evidence["source_sha256"], {
            name: hashlib.sha256((PROBE / name).read_bytes()).hexdigest()
            for name in ("fixed_writer.py", "script_only_extension.ts")
        })
        self.assertEqual(set(evidence["cases"]), set(CASES))
        self.assertTrue(evidence["omp_version"].startswith("omp/"))
        self.assertIn("--no-tools", evidence["argv"])
        self.assertIn("--extension", evidence["argv"])
        self.assertEqual(evidence["state_response"]["type"], "response")
        self.assertIs(evidence["process"]["stdout_pump_stopped"], True)
        self.assertEqual(evidence["process"]["remaining_owned"], [])
        self.assertTrue(evidence["all_provider_posts"])
        self.assertEqual({tuple(post["tools"]) for post in evidence["all_provider_posts"]}, {
            ("db_summary_save",)
        })
        self.assertEqual({post["path"] for post in evidence["all_provider_posts"]}, {
            "/v1/chat/completions"
        })
        positive = evidence["cases"]["positive"]
        self.assertIs(positive["agent_end"], True)
        self.assertEqual(positive["db_rows"], [[1, SUMMARY]])
        self.assertEqual(positive["tool_ends"][0]["name"], "db_summary_save")
        self.assertIs(positive["tool_ends"][0]["isError"], False)
        for name in ("bash", "write", "edit"):
            case = evidence["cases"][name]
            self.assertIs(case["agent_end"], True)
            self.assertTrue(case["posts"] and case["provider_receipts"])
            self.assertEqual(case["tool_ends"][0]["name"], name)
            self.assertIs(case["tool_ends"][0]["isError"], True)
            self.assertEqual(case["db_rows"], [[1, SUMMARY]])
            self.assertEqual(case["sentinels_unchanged"], {
                "file-sentinel": True,
                "command-sentinel": True,
            })

    def test_retained_actual_omp_extra_properties_cannot_save_additional_rows(self) -> None:
        evidence = self._runtime_evidence()
        self.assertEqual(set(evidence["cases"]), set(CASES))
        outbound = {item["case"]: item["call"] for item in evidence["outbound_calls"]}
        self.assertEqual(set(outbound), set(CASES))
        trace = evidence["execute_trace"]
        received = {item["id"]: item["params"] for item in trace if item["event"] == "received"}
        self.assertEqual(set(received), {
            "q01-positive", "q01-inject_command", "q01-inject_sql",
            "q01-inject_path", "q01-inject_extra",
        })
        invoked = [item for item in trace if item["event"] == "writer_invoked"]
        self.assertEqual(len(invoked), 1)
        self.assertEqual(invoked[0]["id"], "q01-positive")
        self.assertEqual(len(invoked[0]["argv"]), 3)
        self.assertEqual(Path(invoked[0]["argv"][1]).name, "fixed_writer.py")
        self.assertEqual(json.loads(invoked[0]["argv"][2]), {"summary": SUMMARY})
        self.assertEqual(received["q01-positive"], {"summary": SUMMARY})
        for name in ("inject_command", "inject_sql", "inject_path", "inject_extra"):
            with self.subTest(case=name):
                case = evidence["cases"][name]
                extra = name.removeprefix("inject_")
                outbound_args = json.loads(outbound[name]["function"]["arguments"])
                self.assertEqual(set(outbound_args), {"summary", extra})
                self.assertEqual(received[f"q01-{name}"], outbound_args)
                self.assertEqual(
                    [item["writerInvoked"] for item in trace
                     if item["id"] == f"q01-{name}" and item["event"] == "rejected"],
                    [False],
                )
                self.assertIs(case["agent_end"], True)
                self.assertTrue(case["posts"] and case["provider_receipts"])
                self.assertEqual(case["sentinels_unchanged"], {
                    "file-sentinel": True,
                    "command-sentinel": True,
                })
                self.assertEqual(case["db_rows"], [[1, SUMMARY]], name)
                self.assertIs(case["tool_ends"][0]["isError"], True, name)
        self.assertEqual(evidence["final_db_rows"], [[1, SUMMARY]])
        self.assertTrue(all(evidence["checks"].values()))


if __name__ == "__main__":
    unittest.main()
