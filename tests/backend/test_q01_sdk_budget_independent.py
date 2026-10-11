"""Independent checks for the isolated installed-OMP budget capability record.

Actual OMP evidence is an explicit input. This module never starts OMP or a model.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from tests.backend.q01_sdk_budget_probe import run_budget
from workbench.contracts.logs_v1 import canonical_json, parse_summary_request, parse_summary_result


ROOT = run_budget.ROOT


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def events(trace: list[dict], name: str) -> list[dict]:
    return [row for row in trace if row.get("event") == name]


class BudgetIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_BUDGET_RESULT")
        output = os.environ.get("Q01_SDK_BUDGET_TEST_DIR")
        if not supplied or not output:
            raise AssertionError("explicit actual result and private test output required")
        cls.result_path = Path(supplied).resolve(strict=True)
        cls.result = json.loads(cls.result_path.read_text())
        cls.output = Path(output).resolve(strict=True)
        cls.full_path = Path(cls.result["actual_run"]["path"]).resolve(strict=True)
        cls.full = json.loads(cls.full_path.read_text())
        cls.private = cls.full_path.parent / "private"
        cls.wire_path = Path(cls.result["actual_run"]["raw_wire_validation"]).resolve(strict=True)
        cls.wire = json.loads(cls.wire_path.read_text())
        cls.old_result = json.loads(Path(cls.result["historical_preserved"]["old_result"]["path"]).read_text())

    def test_source_artifact_binary_trace_and_owned_process_custody(self) -> None:
        for relative, expected in self.result["source_sha256"].items():
            self.assertEqual(digest(ROOT / relative), expected)
        self.assertEqual(digest(self.full_path), self.result["actual_run"]["sha256"])
        self.assertEqual(digest(self.wire_path), self.result["actual_run"]["raw_wire_validation_sha256"])
        self.assertEqual(self.full["source_sha256"],
                         {Path(name).name: digest(ROOT / name)
                          for name in self.result["source_sha256"]})
        self.assertEqual(digest(Path(self.full["argv"][0])), self.full["omp_sha256"])
        self.assertEqual(self.full["omp_sha256"], self.result["actual_run"]["omp_sha256"])
        self.assertEqual(self.full["omp_version"], self.result["actual_run"]["omp_version"])
        self.assertEqual([json.loads(line) for line in
                          (self.private / "trace.jsonl").read_text().splitlines()], self.full["trace"])
        self.assertEqual(digest(self.private / "fixed_writer.py"),
                         digest(run_budget.HERE.parent / "q01_script_only_probe/fixed_writer.py"))
        self.assertTrue(self.full["provider_stopped"])
        self.assertEqual(self.full["process"]["remaining_owned"], [])
        with self.assertRaises(ProcessLookupError):
            os.killpg(self.full["process"]["pgid"], 0)
        for historical in self.result["historical_preserved"].values():
            self.assertEqual(digest(Path(historical["path"])), historical["sha256"])

    def test_one_mib_collected_text_is_distinct_from_serialized_fixture_file(self) -> None:
        path = self.private / "segments.json"
        segments = json.loads(path.read_bytes())
        text_bytes = sum(len(piece["text"].encode("utf-8")) for piece in segments)
        self.assertEqual(text_bytes, 1_048_576)
        self.assertEqual(len(path.read_bytes()), 1_079_722)
        self.assertGreater(len(path.read_bytes()), text_bytes)
        selection = events(self.full["trace"], "selection")
        self.assertEqual(len(selection), 1)
        selected = selection[0]["selected"]
        omitted = selection[0]["omitted"]
        self.assertEqual(selection[0]["collectedBytes"], text_bytes)
        self.assertEqual(selection[0]["selectedBytes"] + selection[0]["omittedBytes"], text_bytes)
        self.assertEqual([(piece["source"], piece["start"], piece["end"])
                          for piece in selected],
                         [("cumulative", 0, 120), ("start-spec", 0, 120), ("decisions", 0, 120)])
        self.assertEqual([(piece["source"], piece["start"], piece["end"], piece["reason"])
                          for piece in omitted],
                         [("reports", 0, 120, "input_budget"),
                          ("terminal", 0, 1_048_096, "input_budget")])
        for chosen in selected:
            source = next(piece for piece in segments
                          if (piece["source"], piece["revision"], piece["start"], piece["end"])
                          == (chosen["source"], chosen["revision"], chosen["start"], chosen["end"]))
            self.assertEqual(chosen["textSha"], sha256(source["text"].encode()).hexdigest())
            self.assertEqual(chosen["textBytes"], len(source["text"].encode()))
        self.assertEqual(sum(piece["estimatedBytes"] for piece in omitted),
                         selection[0]["omittedBytes"])

    def test_selection_helper_prioritizes_required_ranges_and_rejects_over_collection_cap(self) -> None:
        module = str(run_budget.HERE / "budget_support.ts")
        script = f'''import {{selectRanges}} from {json.dumps(module)};
const part=(source,kind,sequence,start,end,text)=>({{source,kind,sequence,start,end,text,revision:1}});
const cum=part("cum","cumulative",1,0,3,"CCC");
const spec=part("spec","start_spec",1,0,3,"SSS");
const decision=part("decision","decision",1,0,3,"DDD");
const report=part("report","report",1,0,3,"RRR");
const old=part("terminal", "terminal",1,0,3,"TTT");
const newest=part("terminal", "terminal",2,3,6,"NNN");
const cap=Buffer.byteLength(JSON.stringify({{selected:[cum,spec,decision]}}));
const ordered=selectRanges([old,report,decision,newest,spec,cum],cap);
const terminalCap=Buffer.byteLength(JSON.stringify({{selected:[newest]}}));
const terminals=selectRanges([old,newest],terminalCap);
let over="";try{{selectRanges([part("x","terminal",1,0,1048577,"A".repeat(1048577))],200)}}catch(e){{over=String(e.message)}}
let bad="";try{{selectRanges([part("x","terminal",1,5,3,"A")],200)}}catch(e){{bad=String(e.message)}}
process.stdout.write(JSON.stringify({{ordered,terminals,over,bad}}));'''
        proc = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                              text=True, capture_output=True, check=True)
        value = json.loads(proc.stdout)
        self.assertEqual([x["source"] for x in value["ordered"]["selected"]],
                         ["cum", "spec", "decision"])
        self.assertEqual([x["source"] for x in value["ordered"]["omitted"]],
                         ["report", "terminal"])
        self.assertEqual([x["start"] for x in value["terminals"]["selected"]], [3])
        self.assertEqual(value["over"], "collection_over_1MiB")
        self.assertEqual(value["bad"], "invalid_source_range")

    def test_shared_budget_admission_directly_accepts_dispatch_and_denies_three_boundaries(self) -> None:
        selection = events(self.full["trace"], "selection")[0]
        negatives = events(self.full["trace"], "negative_preflight")
        dispatch = events(self.full["trace"], "dispatch_preflight")
        base = {"inputLimit": selection["inputLimit"],
                "roundTripAllowance": selection["roundTripAllowance"],
                "contextWindow": 32768, "outputCap": selection["outputCap"],
                "fittedCap": selection["outputCap"], "outputMeaningKnown": True}
        inputs = [base | {"inputBytes": selection["preliminaryBytes"]}]
        inputs.extend(base | {"inputBytes": row["inputBytes"],
                              "fittedCap": row["fittedCap"],
                              "outputMeaningKnown": row["outputMeaningKnown"]}
                      for row in negatives)
        inputs.extend(base | {"inputBytes": row["contextBytes"],
                              "roundTripAllowance": 0,
                              "fittedCap": row["fittedCap"]}
                      for row in dispatch)
        module = str(run_budget.HERE / "budget_support.ts")
        script = (f'import {{budgetAdmission}} from {json.dumps(module)};'
                  f'const inputs={json.dumps(inputs)};'
                  'const edge={...inputs[0],inputBytes:inputs[0].inputLimit-'
                  'inputs[0].roundTripAllowance};'
                  'process.stdout.write(JSON.stringify({decisions:inputs.map(budgetAdmission),'
                  'atLimit:budgetAdmission(edge),aboveLimit:budgetAdmission({...edge,inputBytes:edge.inputBytes+1})}));')
        proc = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                              text=True, capture_output=True, check=True)
        value = json.loads(proc.stdout)
        expected = [{"admitted": True, "reason": None},
                    {"admitted": False, "reason": "input_budget_unavailable"},
                    {"admitted": False, "reason": "output_budget_unavailable"},
                    {"admitted": False, "reason": "output_budget_unavailable"},
                    {"admitted": True, "reason": None},
                    {"admitted": True, "reason": None}]
        self.assertEqual(value["decisions"], expected)
        self.assertEqual(value["atLimit"], expected[0])
        self.assertEqual(value["aboveLimit"], expected[1])
        observed = events(self.full["trace"], "preliminary_admission") + negatives + dispatch
        self.assertEqual([{"admitted": row["admitted"], "reason": row.get("reason")}
                          for row in observed], expected)

    def test_raw_wire_matches_system_skill_tool_schema_and_output_ceiling(self) -> None:
        posts = self.full["posts"]
        self.assertEqual(len(posts), 2)
        skill = (run_budget.HERE.parent / "q01_sdk_private_probe/summary-skill.txt").read_text()
        startup = events(self.full["trace"], "startup")
        self.assertEqual(len(startup), 1)
        self.assertTrue(startup[0]["admitted"])
        self.assertEqual(startup[0]["active"], ["db_summary_save"])
        self.assertEqual(startup[0]["skillSha"], sha256(skill.encode()).hexdigest())
        self.assertEqual((startup[0]["source"], startup[0]["filePath"]),
                         ("bundled", "embedded:task.md"))
        raw_bodies = []
        for index, post in enumerate(posts, 1):
            path = Path(post["raw_body_path"]).resolve(strict=True)
            self.assertEqual(path.name, f"provider-post-{index:03d}.body")
            raw = path.read_bytes()
            raw_bodies.append(raw)
            self.assertEqual(digest(path), post["sha256"])
            self.assertEqual(len(raw), post["bytes"])
            self.assertLessEqual(len(raw), 32768)
            body = json.loads(raw)
            self.assertEqual(body["model"], "scripted")
            self.assertEqual(body["max_completion_tokens"], 8192)
            self.assertEqual([tool["function"]["name"] for tool in body["tools"]],
                             ["db_summary_save"])
            self.assertEqual(body["tools"][0]["function"]["parameters"], {
                "type": "object", "properties": {"summary": {"type": "string",
                "minLength": 1, "maxLength": 2000}}, "required": ["summary"],
                "additionalProperties": True})
            system = body["messages"][0]["content"]
            self.assertTrue(system.endswith("\n\n" + skill))
            self.assertEqual(sha256(system[:-(len(skill) + 2)].encode()).hexdigest(),
                             startup[0]["bodySha"])
        self.assertEqual(json.loads(raw_bodies[0])["tools"], json.loads(raw_bodies[1])["tools"])
        self.assertEqual([m["role"] for m in json.loads(raw_bodies[1])["messages"]],
                         ["system", "user", "assistant", "tool"])

    def test_shared_preflight_records_conservative_estimates_and_denied_calls_zero(self) -> None:
        trace = self.full["trace"]
        selection = events(trace, "selection")[0]
        self.assertIsNone(selection["tokenizerEncoding"])
        self.assertFalse(selection["nativeDiagnostic"]["exact"])
        self.assertEqual(selection["nativeDiagnostic"]["tokens"], selection["preliminaryBytes"])
        self.assertEqual(selection["inputLimit"], 3276)
        self.assertEqual(selection["outputCap"], 8192)
        self.assertEqual(selection["outputReserve"], 8256)
        self.assertLess(selection["preliminaryBytes"] + selection["roundTripAllowance"],
                        selection["inputLimit"])
        preflights = events(trace, "dispatch_preflight")
        self.assertEqual(len(preflights), 2)
        preliminary = events(trace, "preliminary_admission")
        self.assertEqual(len(preliminary), 1)
        self.assertTrue(preliminary[0]["admitted"])
        self.assertIsNone(preliminary[0]["reason"])
        self.assertEqual((preliminary[0]["promptCount"], preliminary[0]["originalStreamCount"]), (0, 0))
        for preflight, post in zip(preflights, self.full["posts"]):
            self.assertEqual(preflight["names"], ["db_summary_save"])
            self.assertTrue(preflight["admitted"])
            self.assertIsNone(preflight.get("reason"))
            self.assertEqual(preflight["outputCap"], 8192)
            self.assertEqual(preflight["outputReserve"], 8256)
            self.assertEqual(preflight["fittedCap"], 8192)
            self.assertLessEqual(preflight["contextBytes"], preflight["inputLimit"])
            self.assertLessEqual(preflight["conservativeWireBound"], 32768)
            self.assertEqual(preflight["nativeDiagnostic"]["tokens"], preflight["contextBytes"])
            self.assertFalse(preflight["nativeDiagnostic"]["exact"])
            self.assertEqual(post["max_completion_tokens"], 8192)
        negatives = events(trace, "negative_preflight")
        self.assertEqual([(e["case"], e["reason"], e["promptCount"]) for e in negatives],
                         [("mandatory_oversize", "input_budget_unavailable", 0),
                          ("fitter_lowered", "output_budget_unavailable", 0),
                          ("meaning_unknown", "output_budget_unavailable", 0)])
        self.assertLess(max(trace.index(e) for e in negatives),
                        trace.index(events(trace, "startup")[0]))
        self.assertGreater(negatives[0]["inputBytes"], selection["inputLimit"])
        self.assertLess(negatives[1]["fittedCap"], selection["outputCap"])
        self.assertTrue(all(not e["admitted"] and e["originalStreamCount"] == 0 for e in negatives))
        self.assertEqual([e["outputMeaningKnown"] for e in negatives], [True, True, False])
        self.assertEqual(events(trace, "done")[0]["originalStreamCount"], 2)
        self.assertEqual(len(self.full["posts"]), 2)

    def test_host_summary_request_result_receipt_and_fixed_sqlite_row_are_bound(self) -> None:
        request = parse_summary_request(json.loads((self.private / "request.json").read_text()))
        raw = (self.private / "typed-result.json").read_bytes()
        typed = parse_summary_result(json.loads(raw))
        self.assertLessEqual(len(raw), 8192)
        self.assertEqual(len(raw), 1295)
        self.assertEqual(self.full["typed_contract"]["sha256"], sha256(raw).hexdigest())
        for key in ("requestId", "inputKey", "skillDigest", "sources"):
            self.assertEqual(typed[key], request[key])
        self.assertEqual(typed["facts"], ["Q01 budget synthetic summary"])
        self.assertEqual(typed["usage"], {"schemaVersion": 1, "inputTokens": None,
                                          "outputTokens": None, "costUsd": None})
        with sqlite3.connect(f"file:{self.private / 'summary.sqlite3'}?mode=ro", uri=True) as db:
            rows = [list(row) for row in db.execute("SELECT id, summary FROM summaries ORDER BY id")]
        self.assertEqual(rows, [[1, "Q01 budget synthetic summary"]])
        with sqlite3.connect(f"file:{self.private / 'backend.sqlite3'}?mode=ro", uri=True) as db:
            stored = db.execute("SELECT request_json FROM summary_requests_v1").fetchall()
            receipts = db.execute("SELECT request_id,result_digest FROM summary_result_receipts_v1").fetchall()
            states = [row[0] for row in db.execute("SELECT state FROM summary_request_events_v1 ORDER BY event_id")]
        self.assertEqual(len(stored), 1)
        self.assertEqual(parse_summary_request(json.loads(stored[0][0])), request)
        self.assertEqual(receipts, [(request["requestId"],
                                     sha256(canonical_json(typed)).hexdigest())])
        self.assertEqual(states, ["prepared", "sending", "stored"])
        self.assertEqual(len(events(self.full["trace"], "saved")), 1)
        self.assertEqual(len(events(self.full["trace"], "disposed")), 1)

    def test_historical_continuation_failure_and_near_eight_kib_limit_remain_unproven(self) -> None:
        full03 = next(row for row in self.old_result["historical_runs"]
                      if "/full03/" in row["path"])
        old = json.loads(Path(full03["path"]).read_text())
        self.assertEqual(len(old["posts"]), 1)
        self.assertFalse(old["checks"]["positive_db_only_two_posts"])
        old_preflights = events(old["trace"], "dispatch_preflight")
        self.assertEqual(len(old_preflights), 2)
        self.assertEqual(old_preflights[1]["reason"], "budget")
        self.assertGreater(old_preflights[1]["contextBytes"], old_preflights[1]["inputLimit"])
        self.assertTrue(events(old["trace"], "done")[0]["protected"])
        self.assertEqual(len(self.full["posts"]), 2)
        self.assertEqual(self.full["typed_contract"]["bytes"], 1295)
        self.assertLess(self.full["typed_contract"]["bytes"], 8192)
        self.assertNotEqual(self.full["typed_contract"]["bytes"], 8192)

    def test_prelaunch_provider_and_process_failures_close_owned_resources(self) -> None:
        for boundary in ("thread_start", "missing_omp", "popen"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(
                    dir=self.output, prefix="budget-fault-") as tmp:
                target = Path(tmp) / "evidence"
                server = MagicMock(); server.server_port = 12345
                thread = MagicMock()
                if boundary == "thread_start":
                    thread.start.side_effect = RuntimeError("thread fault")
                with patch.object(run_budget, "ThreadingHTTPServer", return_value=server), \
                     patch.object(run_budget.threading, "Thread", return_value=thread), \
                     patch.object(run_budget.shutil, "which", return_value=(
                         None if boundary == "missing_omp" else "/bin/true")), \
                     patch.object(run_budget.subprocess, "run", return_value=SimpleNamespace(stdout="omp/18.8.6")) as version, \
                     patch.object(run_budget.subprocess, "Popen", side_effect=(
                         OSError("popen fault") if boundary == "popen" else None)) as popen, \
                     patch.object(run_budget.sys, "argv", ["run_budget.py", str(target)]):
                    expected = {"thread_start": RuntimeError, "missing_omp": RuntimeError,
                                "popen": OSError}[boundary]
                    with self.assertRaises(expected):
                        run_budget.run(target)
                    server.server_close.assert_called_once()
                    if boundary == "thread_start":
                        server.shutdown.assert_not_called()
                        thread.join.assert_not_called()
                        version.assert_not_called(); popen.assert_not_called()
                    else:
                        server.shutdown.assert_called_once()
                        thread.join.assert_called_once()
                    if boundary == "missing_omp":
                        version.assert_not_called(); popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
