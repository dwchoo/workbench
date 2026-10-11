"""Independent checks for the bounded in-process SDK child feasibility probe."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SDK_PROBE = ROOT / "tests/backend/q01_script_route_probe"
WRITER = ROOT / "tests/backend/q01_script_only_probe/fixed_writer.py"
SUMMARY = "literal: touch /tmp/q01-should-not-run; DROP TABLE summaries; -- data only"
ACCEPTED = {"positive", "bash", "write", "edit", "inject_command", "inject_sql", "inject_path"}
BLOCKED = {"missing_allow", "extra_tool", "stale_session", "reused_request"}
SDK_IMPORT = "import { createAgentSession, SessionManager } from \"@oh-my-pi/pi-coding-agent\";"


def _load_env(name: str, test: unittest.TestCase) -> dict:
    location = os.environ.get(name)
    if location is None:
        test.skipTest(f"{name} is required for actual OMP observation")
    return json.loads(Path(location).read_text())


def _source_hashes(directory: Path, names: tuple[str, ...]) -> dict[str, str]:
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names}


def _events(trace: list[dict], event: str) -> list[dict]:
    return [item for item in trace if item.get("event") == event]


def _rows(db: Path) -> list[tuple[int, str]]:
    if not db.exists():
        return []
    with sqlite3.connect(db) as con:
        return con.execute("SELECT id, summary FROM summaries ORDER BY id").fetchall()


def _direct_tool_boundary(tmp_path: Path, scenario: str) -> tuple[dict, list[tuple[int, str]], list[dict]]:
    """Execute the real tool function in Node, without creating an SDK or OMP session."""
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("Node is unavailable for the TypeScript boundary fixture")
    source = (SDK_PROBE / "sdk_child_extension.ts").read_text()
    if source.count(SDK_IMPORT) != 1:
        raise AssertionError("cannot isolate the tool function from its SDK import")
    isolated = source.replace(SDK_IMPORT, "") + "\nexport { makeSaveTool };\n"
    (tmp_path / "tool.mts").write_text(isolated)
    writer = tmp_path / "fixed_writer.py"
    shutil.copyfile(WRITER, writer)
    script = tmp_path / "boundary.mjs"
    script.write_text(
        "import { makeSaveTool } from './tool.mts';\n"
        "const scenario = process.argv[2];\n"
        "let output;\n"
        "if (scenario === 'extra_then_single_use') {\n"
        "  const bad = makeSaveTool('extra', 'r-extra', { current: { id: 'r-extra', sessionId: 's-extra' } });\n"
        "  const extra = await bad.execute('call-extra', { summary: 'safe', command: 'touch sentinel' });\n"
        "  const good = makeSaveTool('valid', 'r-valid', { current: { id: 'r-valid', sessionId: 's-valid' } });\n"
        "  const first = await good.execute('call-first', { summary: 'safe' });\n"
        "  const second = await good.execute('call-second', { summary: 'safe' });\n"
        "  output = { extra, first, second };\n"
        "} else if (scenario === 'oversize_result') {\n"
        "  const id = '한'.repeat(3000);\n"
        "  const tool = makeSaveTool('oversize', id, { current: { id, sessionId: 's' } });\n"
        "  output = { result: await tool.execute('call-oversize', { summary: 'safe' }) };\n"
        "} else { throw new Error('unknown scenario'); }\n"
        "process.stdout.write(JSON.stringify(output));\n"
    )
    trace_path = tmp_path / "trace.jsonl"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "Q01_FIXED_WRITER": str(writer),
        "Q01_FIXED_PYTHON": sys.executable,
        "Q01_CHILD_TRACE": str(trace_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    run = subprocess.run(
        [node, "--experimental-strip-types", str(script), scenario],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if run.returncode != 0:
        raise AssertionError(f"controlled Node boundary failed: {run.stderr[:2000]}")
    trace = [json.loads(line) for line in trace_path.read_text().splitlines()]
    return json.loads(run.stdout), _rows(tmp_path / "summary.sqlite3"), trace


class Q01SdkChildIndependentTests(unittest.TestCase):
    def test_actual_parent_import_resolves_public_sdk_without_model_post(self) -> None:
        observed = _load_env("Q01_SDK_IMPORT_EVIDENCE", self)
        self.assertEqual(observed["source_sha256"], _source_hashes(
            SDK_PROBE, ("sdk_import_extension.ts", "run_sdk_import.py")
        ))
        self.assertEqual(observed["module_specifier"], "@oh-my-pi/pi-coding-agent")
        imported = [item for item in observed["module_trace"] if item.get("event") == "import_result"]
        self.assertEqual(len(imported), 1)
        self.assertIs(imported[0]["ok"], True)
        self.assertEqual(imported[0]["createAgentSessionType"], "function")
        self.assertEqual(imported[0]["sessionManagerInMemoryType"], "function")
        self.assertEqual(observed["rpc_sent"], [{"id": "sdk-import-state", "type": "get_state"}])
        self.assertEqual(observed["model_prompts_sent"], 0)
        self.assertEqual(observed["provider_posts"], [])
        self.assertIs(observed["timed_out"], False)
        self.assertIs(observed["process"]["stdout_pump_stopped"], True)
        self.assertEqual(observed["process"]["remaining_owned"], [])

    def test_actual_child_admission_and_effects(self) -> None:
        observed = _load_env("Q01_SDK_CHILD_EVIDENCE", self)
        expected_sources = _source_hashes(SDK_PROBE, ("sdk_child_extension.ts", "run_sdk_child.py"))
        expected_sources["fixed_writer.py"] = hashlib.sha256(WRITER.read_bytes()).hexdigest()
        self.assertEqual(observed["source_sha256"], expected_sources)
        self.assertEqual(observed["parent_rpc_sent"], [
            {"id": "q01-child-parent-state", "type": "get_state"}
        ])
        self.assertIs(observed["timed_out"], False)
        self.assertIsNone(observed["stop_reason"])
        trace = observed["child_trace"]
        self.assertEqual(len(_events(trace, "run_begin")), 1)
        self.assertEqual(set(_events(trace, "run_begin")[0]["cases"]), ACCEPTED | BLOCKED)
        self.assertEqual(_events(trace, "run_done")[-1]["ok"], True)
        created = _events(trace, "child_create_start")
        readback = _events(trace, "child_readback")
        disposed = _events(trace, "child_disposed")
        self.assertEqual((len(created), len(readback), len(disposed)), (11, 11, 11))
        self.assertEqual({item["case"] for item in created}, ACCEPTED | BLOCKED)
        self.assertEqual({item["case"] for item in readback}, ACCEPTED | BLOCKED)
        self.assertEqual({item["case"] for item in disposed}, ACCEPTED | BLOCKED)
        self.assertEqual(len({item["sessionId"] for item in readback}), len(ACCEPTED | BLOCKED))
        for item in created:
            self.assertIs(item["allowRestrictedCustomTools"], item["case"] != "missing_allow")
            self.assertEqual(item["toolNames"],
                             ["db_summary_save", "other_probe"] if item["case"] == "extra_tool"
                             else ["db_summary_save"])
        admitted = {item["case"]: item for item in _events(trace, "admission_accepted")}
        rejected = {item["case"]: item for item in _events(trace, "admission_rejected")}
        self.assertEqual(set(admitted), ACCEPTED)
        self.assertEqual(set(rejected), BLOCKED)
        self.assertEqual({item["case"] for item in _events(trace, "prompt_start")}, ACCEPTED)
        self.assertEqual({item["case"] for item in _events(trace, "prompt_done")}, ACCEPTED)
        self.assertEqual(rejected["missing_allow"]["active"], [])
        self.assertEqual(rejected["extra_tool"]["active"], ["db_summary_save", "other_probe"])
        self.assertEqual(rejected["stale_session"]["reason"], "stale_session")
        self.assertEqual(rejected["reused_request"]["reason"], "request_reused")

        posts = observed["provider_posts"]
        self.assertEqual(Counter(item["case"] for item in posts), Counter({name: 2 for name in ACCEPTED}))
        self.assertTrue(all(item["path"] == "/v1/chat/completions" for item in posts))
        self.assertTrue(all(item["tools"] == ["db_summary_save"] for item in posts))
        self.assertTrue(all(item["pre_prompt"] is False for item in posts))
        self.assertEqual(Counter(item["case"] for item in observed["provider_outbound"]),
                         Counter({name: 2 for name in ACCEPTED}))
        positive_calls = [call for post in observed["provider_outbound"]
                          if post["case"] == "positive" for call in post["calls"]]
        self.assertEqual([call["function"]["name"] for call in positive_calls],
                         ["db_summary_save", "db_summary_save"])
        for name in ("bash", "write", "edit"):
            calls = [item for post in observed["provider_outbound"] if post["case"] == name
                     for item in post["calls"]]
            self.assertEqual([item["function"]["name"] for item in calls], [name])
            self.assertEqual(observed["provider_receipts"][name], [f"Tool {name} not found"])

        received = {name: [item for item in _events(trace, "execute_received")
                           if item["case"] == name]
                    for name in {"positive", "inject_command", "inject_sql", "inject_path"}}
        self.assertEqual(set(received), {"positive", "inject_command", "inject_sql", "inject_path"})
        invoked = _events(trace, "writer_invoked")
        self.assertEqual(len(invoked), 1)
        self.assertEqual(invoked[0]["case"], "positive")
        self.assertEqual(Path(invoked[0]["argv"][0]).name, "fixed_writer.py")
        self.assertEqual(json.loads(invoked[0]["argv"][1]), {"summary": SUMMARY})
        saved = _events(trace, "execute_saved")
        self.assertEqual(len(saved), 1)
        self.assertLessEqual(saved[0]["resultBytes"], 8192)
        self.assertEqual([item["params"] for item in received["positive"]],
                         [{"summary": SUMMARY}, {"summary": SUMMARY}])
        self.assertEqual([item["used"] for item in received["positive"]], [False, True])
        positive_receipts = [json.loads(item) for item in observed["provider_receipts"]["positive"]]
        self.assertEqual(len(positive_receipts), 2)
        self.assertIs(positive_receipts[0]["saved"], True)
        self.assertEqual(positive_receipts[0]["request_id"], admitted["positive"]["requestId"])
        self.assertEqual(positive_receipts[0]["session_id"], admitted["positive"]["sessionId"])
        self.assertIs(positive_receipts[1]["saved"], False)
        self.assertEqual(positive_receipts[1]["reason"], "missing_identity_or_used")
        for name in ("inject_command", "inject_sql", "inject_path"):
            calls = [item for post in observed["provider_outbound"] if post["case"] == name
                     for item in post["calls"]]
            self.assertEqual(len(calls), 1)
            raw = json.loads(calls[0]["function"]["arguments"])
            self.assertEqual(set(raw), {"summary", name.removeprefix("inject_")})
            self.assertEqual([item["params"] for item in received[name]], [raw])
            failures = [item for item in _events(trace, "execute_rejected") if item["case"] == name]
            self.assertEqual([item["reason"] for item in failures], ["invalid_data"])
            receipt = [json.loads(item) for item in observed["provider_receipts"][name]]
            self.assertEqual([(item["saved"], item["reason"]) for item in receipt],
                             [(False, "invalid_data")])
        self.assertEqual(observed["final_db_rows"], [[1, SUMMARY]])
        self.assertEqual(observed["sentinels"], {
            "command-sentinel": "ORIGINAL", "file-sentinel": "ORIGINAL"
        })
        self.assertEqual(observed["permit_files_remaining"], [])
        self.assertIs(observed["process"]["stdout_pump_stopped"], True)
        self.assertEqual(observed["process"]["remaining_owned"], [])

    def test_controlled_tool_rejects_extra_data_and_second_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output, rows, trace = _direct_tool_boundary(Path(directory), "extra_then_single_use")
        self.assertIs(output["extra"]["isError"], True)
        self.assertNotIn("isError", output["first"])
        self.assertIs(output["second"]["isError"], True)
        self.assertEqual(rows, [(1, "safe")])
        self.assertEqual([item["case"] for item in _events(trace, "writer_invoked")], ["valid"])

    def test_controlled_tool_checks_utf8_result_limit_before_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output, rows, trace = _direct_tool_boundary(Path(directory), "oversize_result")
        self.assertIs(output["result"]["isError"], True)
        self.assertEqual([item["reason"] for item in _events(trace, "execute_rejected")], ["result_bytes"])
        self.assertEqual(rows, [], "oversize result must fail before the fixed writer mutates DB")
        self.assertEqual(_events(trace, "writer_invoked"), [])
        receipt = output["result"]["content"]
        self.assertTrue(receipt)
        self.assertTrue(all(part["type"] == "text" for part in receipt))
        self.assertLessEqual(sum(len(part["text"].encode("utf-8")) for part in receipt), 8192)


if __name__ == "__main__":
    unittest.main()
