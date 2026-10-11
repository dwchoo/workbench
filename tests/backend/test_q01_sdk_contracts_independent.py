"""Independent boundaries for the local installed-OMP bundled Task discriminator.

The recorded OMP run is an input, never launched by this test module.
"""

from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.backend.q01_sdk_full_probe import full_probe, run_contract
from workbench.contracts.logs_v1 import canonical_json, parse_summary_result


GATES = ("Q01-route", "Q01-tools", "Q01-skill", "Q01-result", "Q01-context",
         "Q01-priority", "Q01-recovery", "Q01-origin")
SUMMARY = "Literal command and SQL: touch /tmp/NO_EXECUTE; DROP TABLE summaries; --"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def events(trace: list[dict], case: str, event: str) -> list[dict]:
    return [x for x in trace if x.get("case") == case and x.get("event") == event]


def one(trace: list[dict], case: str, event: str) -> dict:
    found = events(trace, case, event)
    assert len(found) == 1, (case, event, len(found))
    return found[0]


class ContractIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_CONTRACT_RESULT")
        output = os.environ.get("Q01_SDK_CONTRACT_TEST_DIR")
        if not supplied or not output:
            raise AssertionError("explicit actual result and private test output required")
        cls.result_path = Path(supplied).resolve(strict=True)
        cls.result = json.loads(cls.result_path.read_text())
        cls.paths = {Path(p).name: Path(p).resolve(strict=True)
                     for p in cls.result["artifact_sha256"]}
        cls.contract_path = cls.paths["contract-evidence.json"]
        cls.contract = json.loads(cls.contract_path.read_text())
        cls.runtime = json.loads(cls.paths["runtime-evidence.json"].read_text())
        cls.prepare = json.loads(cls.paths["prepare-evidence.json"].read_text())
        cls.envelope = json.loads(cls.paths["collector-envelope.json"].read_text())
        cls.frozen = json.loads(cls.paths["observations.json"].read_text())
        cls.output = Path(output).resolve(strict=True)

    def _collect_with_contract(self, change, *, omit_typed: bool = False,
                               change_prepare=None, change_runtime=None) -> dict:
        """Feed changed recorded contract evidence into the real collector without OMP."""
        contract = deepcopy(self.contract)
        change(contract)
        prepare = deepcopy(self.prepare)
        runtime = deepcopy(self.runtime)
        if change_prepare:
            change_prepare(prepare)
        if change_runtime:
            change_runtime(runtime)
        with tempfile.TemporaryDirectory(dir=self.output, prefix="contract-input-") as tmp:
            out = Path(tmp) / "runtime"
            prepare_path = Path(tmp) / "prepare-evidence.json"
            prepare_path.write_text(json.dumps(prepare))
            contract_private = Path(tmp) / "contract/private"
            contract_private.mkdir(parents=True)
            if not omit_typed:
                shutil.copyfile(self.contract_path.parent / "private/typed-positive.json",
                                contract_private / "typed-positive.json")

            def supply(path: Path) -> int:
                self.assertEqual(path, out)
                private = out / "private"
                private.mkdir(parents=True)
                (out / "runtime-evidence.json").write_text(json.dumps(runtime))
                (private / "trace.jsonl").write_text("".join(json.dumps(e) + "\n"
                                                             for e in runtime["trace"]))
                shutil.copyfile(self.paths["runtime-evidence.json"].parent / "private/summary.sqlite3",
                                private / "summary.sqlite3")
                return 0

            readback = next(x for x in self.prepare["trace"] if x.get("event") == "readback")
            model = readback["model"]
            prepared = {
                "closed": False, "runtime_output": str(out),
                "prepare_evidence": str(prepare_path),
                "contract_runtime_output": str(Path(tmp) / "contract"),
                "prepare_model": {key: model[key]
                                  for key in ("provider", "id", "api", "contextWindow", "maxTokens")},
                "prepare_thinking": readback.get("thinkingLevel"),
                "prepare_tier": readback.get("serviceTierByFamily"),
                "route_identity": {"selection_epoch": readback["sessionId"],
                                   "agent_definition_sha256": self.contract["bundle_body_sha"]},
            }
            backend = json.loads((self.paths["runtime-evidence.json"].parent /
                                  "backend-controls.json").read_text())
            with ExitStack() as stack:
                stack.enter_context(patch.object(full_probe.run_full, "main", side_effect=supply))
                stack.enter_context(patch.object(run_contract, "run", return_value=contract))
                stack.enter_context(patch.object(full_probe, "_backend_result_controls",
                                         return_value=backend))
                return full_probe.collect(prepared)

    def test_current_source_artifact_trace_sqlite_and_owned_process_custody(self) -> None:
        for name, expected in self.result["source_sha256"].items():
            self.assertEqual(digest(full_probe.ROOT / name), expected, name)
        for name, expected in self.result["artifact_sha256"].items():
            path = Path(name)
            self.assertEqual(digest(path if path.is_absolute() else full_probe.ROOT / path),
                             expected, name)
        c, private = self.contract, self.contract_path.parent / "private"
        runtime_path = self.paths["runtime-evidence.json"]
        self.assertEqual(full_probe._verified_runtime(self.runtime, runtime_path), [])
        self.assertEqual([json.loads(line) for line in
                          (runtime_path.parent / "private/trace.jsonl").read_text().splitlines()],
                         self.runtime["trace"])
        self.assertEqual(self.prepare["provider_posts"], [])
        for observed in (self.prepare, self.runtime, c):
            self.assertTrue(observed["process"]["pump_stopped"])
            self.assertEqual(observed["process"]["remaining_owned"], [])
            with self.assertRaises(ProcessLookupError):
                os.killpg(observed["process"]["pid"], 0)
        self.assertEqual([json.loads(line) for line in (private / "trace.jsonl").read_text().splitlines()],
                         c["trace"])
        self.assertEqual(digest(private / "role-inputs.json"), c["role_inputs_sha"])
        self.assertEqual(json.loads((private / "role-inputs.json").read_text()), c["role_inputs"])
        self.assertEqual(digest(private / "bundle-body.txt"), c["bundle_body_sha"])
        self.assertEqual(digest(private / "fixed_writer.py"),
                         c["source_sha"][str(full_probe.HERE.parent /
                                              "q01_script_only_probe/fixed_writer.py")])
        self.assertEqual(set(c["source_sha"]), {str(full_probe.HERE / name) for name in
                                               ("contract_extension.ts", "role_admission.ts",
                                                "run_contract.py", "full_probe.py")} |
                         {str(full_probe.BODY), str(full_probe.SKILL),
                          str(full_probe.HERE.parent / "q01_script_only_probe/fixed_writer.py")})
        self.assertTrue(all(digest(Path(p)) == sha for p, sha in c["source_sha"].items()))
        with sqlite3.connect(f"file:{private / 'summary.sqlite3'}?mode=ro", uri=True) as db:
            rows = [list(x) for x in db.execute("SELECT id, summary FROM summaries ORDER BY id")]
        self.assertEqual(rows, c["db_rows"])
        self.assertEqual(rows, [[1, SUMMARY], [2, SUMMARY]])
        self.assertEqual(c["sentinels"], {"command-sentinel": "ORIGINAL", "file-sentinel": "ORIGINAL"})
        self.assertEqual(c["stop_reason"], None)

    def test_actual_bundled_body_and_host_skill_are_exact_provider_context(self) -> None:
        c = self.contract
        begin = next(x for x in c["trace"] if x.get("event") == "begin")
        body = (self.contract_path.parent / "private/bundle-body.txt").read_bytes()
        skill = full_probe.SKILL.read_bytes()
        self.assertEqual(begin["definition"]["name"], "task")
        self.assertEqual(begin["definition"]["source"], "bundled")
        self.assertEqual(begin["definition"]["filePath"], "embedded:task.md")
        self.assertEqual(begin["definition"]["bodySha"], sha256(body).hexdigest())
        self.assertEqual(begin["definition"]["bodyUtf8Bytes"], len(body))
        self.assertEqual(begin["definition"]["skillSha"], sha256(skill).hexdigest())
        self.assertNotEqual(sha256(body).hexdigest(), digest(full_probe.BODY))
        expected_system = sha256(body + b"\n\n" + skill).hexdigest()
        self.assertEqual(Counter(x["case"] for x in c["provider_posts"]),
                         {"positive": 2, "saved_then_bash": 2})
        for post in c["provider_posts"]:
            self.assertEqual(post["system_sha"], expected_system)
            self.assertEqual(post["tools"], ["db_summary_save"])
            self.assertTrue(post["permit"] and post["system_exact"])

    def test_generic_role_guard_and_actual_pre_sdk_worker_rejection(self) -> None:
        module = str(full_probe.HERE / "role_admission.ts")
        script = ("import { admitSummaryRole } from " + json.dumps(module) + ";" +
                  "process.stdout.write(JSON.stringify([" +
                  "admitSummaryRole('Manager','task'),admitSummaryRole('Worker','task')," +
                  "admitSummaryRole('Manager','other'),admitSummaryRole(undefined,'task')]))")
        proc = subprocess.run(["node", "--input-type=module", "-e", script], cwd=full_probe.ROOT,
                              text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(proc.stdout), [
            {"admitted": True, "reason": "approved_task_manager"},
            {"admitted": False, "reason": "role_not_allowed"},
            {"admitted": False, "reason": "agent_not_allowed"},
            {"admitted": False, "reason": "role_not_allowed"},
        ])
        c = self.contract
        self.assertEqual(c["role_inputs"], {
            "positive": {"role": "Manager", "agentName": "task"},
            "saved_then_bash": {"role": "Manager", "agentName": "task"},
            "worker_role": {"role": "Worker", "agentName": "task"},
        })
        worker = one(c["trace"], "worker_role", "role_rejected")
        self.assertEqual(worker["reason"], "role_not_allowed")
        self.assertFalse(worker["childCreated"])
        self.assertEqual(worker["promptCount"], 0)
        self.assertEqual(events(c["trace"], "worker_role", "session_created"), [])
        self.assertEqual(events(c["trace"], "worker_role", "prompt_start"), [])
        self.assertEqual([p for p in c["provider_posts"] if p["case"] == "worker_role"], [])
        for case in ("positive", "saved_then_bash"):
            self.assertTrue(one(c["trace"], case, "role_admission")["admitted"])
            self.assertEqual(len(events(c["trace"], case, "session_created")), 1)

    def test_actual_child_typed_bytes_bind_to_request_and_parent_persistence(self) -> None:
        c = self.contract
        raw = (self.contract_path.parent / "private/typed-positive.json").read_bytes()
        actual = parse_summary_result(json.loads(raw))
        request = c["requests"]["positive"]
        backend = c["backend_controls"]
        self.assertLessEqual(len(raw), 8192)
        self.assertLessEqual(len(canonical_json(actual)), 8192)
        self.assertEqual(backend["actual_result_sha"], sha256(raw).hexdigest())
        self.assertEqual(backend["actual_result_bytes"], len(raw))
        self.assertEqual(backend["result"], actual)
        for key in ("requestId", "inputKey", "skillDigest", "sources"):
            self.assertEqual(actual[key], request[key], key)
        self.assertEqual(actual["facts"], [SUMMARY])
        self.assertTrue(all(backend["checks"].values()))
        saved = one(c["trace"], "positive", "saved")
        accepted = one(c["trace"], "positive", "result_accepted")
        self.assertEqual(saved["resultSha"], sha256(raw).hexdigest())
        self.assertEqual(accepted["resultSha"], sha256(raw).hexdigest())
        self.assertEqual(saved["resultBytes"], len(raw))
        self.assertEqual(accepted["resultBytes"], len(raw))
        parent = one(c["trace"], "positive", "parent_receipt")
        receipt = parent["parentReceipt"].encode("utf-8")
        self.assertEqual(parent["bytes"], len(receipt))
        self.assertLessEqual(len(receipt), 4096)
        self.assertEqual(parent["sha"], sha256(receipt).hexdigest())
        self.assertEqual(json.loads(receipt)["requestId"], request["requestId"])
        self.assertEqual(json.loads(receipt)["resultSha"], sha256(raw).hexdigest())
        self.assertFalse(parent["entryContainsRawSummary"])
        self.assertEqual(parent["parentEntriesAfter"], parent["parentEntriesBefore"] + 1)

    def test_saved_then_forbidden_is_provisional_and_not_parent_accepted(self) -> None:
        c = self.contract
        trace = c["trace"]
        saved = one(trace, "saved_then_bash", "saved")
        rejected = one(trace, "saved_then_bash", "result_rejected")
        bash = [e for e in events(trace, "saved_then_bash", "tool_end") if e["toolName"] == "bash"]
        self.assertEqual(len(bash), 1)
        self.assertTrue(bash[0]["isError"])
        self.assertEqual(bash[0]["result"], "Tool bash not found")
        self.assertLess(trace.index(saved), trace.index(bash[0]))
        self.assertLess(trace.index(bash[0]), trace.index(one(trace, "saved_then_bash", "abort_completed")))
        self.assertLess(trace.index(one(trace, "saved_then_bash", "abort_completed")), trace.index(rejected))
        self.assertTrue(rejected["saved"] and rejected["forbiddenRefused"])
        self.assertEqual(events(trace, "saved_then_bash", "result_accepted"), [])
        self.assertEqual(events(trace, "saved_then_bash", "parent_receipt"), [])
        self.assertFalse((self.contract_path.parent / "private/typed-saved_then_bash.json").exists())
        self.assertFalse(one(trace, "saved_then_bash", "case_done")["accepted"])
        self.assertEqual(len([p for p in c["provider_posts"] if p["case"] == "saved_then_bash"]), 2)
        self.assertTrue(any(f.get("id") == "contract-parent-after" and f.get("error") is None
                            for f in c["rpc_frames"]))

    def test_three_local_passes_do_not_close_remaining_five_gates(self) -> None:
        rows = self.envelope["observations"]
        self.assertEqual(tuple(x["id"] for x in rows), GATES)
        verdicts = self.frozen["verdicts"]
        self.assertEqual({k for k, v in verdicts.items() if v["result"] == "passed"},
                         {"Q01-route", "Q01-skill", "Q01-result"})
        self.assertEqual({k for k, v in verdicts.items() if v["result"] == "inconclusive"},
                         set(GATES) - {"Q01-route", "Q01-skill", "Q01-result"})
        context = next(x for x in rows if x["id"] == "Q01-context")
        self.assertIn("parent_model_context_receipt_delivery_unobserved", context["unknowns"])
        self.assertIn("three_turn_parent_transcript_delta_unobserved", context["unknowns"])
        self.assertNotIn("parent_receipt_within_4k", context["facts"])
        self.assertTrue(context["facts"]["parent_persistence_receipt_within_4k"])
        self.assertFalse(self.result["full_q01_pass"])

    def test_missing_or_extra_contract_source_manifest_cannot_pass_typed_route(self) -> None:
        for name in ("missing", "extra"):
            with self.subTest(change=name):
                def change(c: dict) -> None:
                    if name == "missing":
                        c["source_sha"].pop(next(iter(c["source_sha"])))
                    else:
                        c["source_sha"]["/private/unexpected.ts"] = "0" * 64
                rows = self._collect_with_contract(change)["observations"]
                route = next(x for x in rows if x["id"] == "Q01-route")
                self.assertIsNot(route["facts"].get("typed_return"), True)

    def test_mismatched_contract_receipt_cannot_pass_result_binding(self) -> None:
        def change(c: dict) -> None:
            receipt = one(c["trace"], "positive", "parent_receipt")
            receipt["requestId"] = "unrelated-request"
        rows = self._collect_with_contract(change)["observations"]
        result = next(x for x in rows if x["id"] == "Q01-result")
        self.assertIsNot(result["facts"].get("typed_channel"), True)

    def test_missing_child_typed_file_cannot_pass_typed_channel(self) -> None:
        rows = self._collect_with_contract(lambda _recorded: None, omit_typed=True)["observations"]
        result = next(x for x in rows if x["id"] == "Q01-result")
        self.assertIsNot(result["facts"].get("typed_channel"), True)

    def test_oversize_typed_result_cannot_pass_eight_kib_bound(self) -> None:
        def oversize(c: dict) -> None:
            c["backend_controls"]["actual_result_bytes"] = 8193
        context = next(x for x in self._collect_with_contract(oversize)["observations"]
                       if x["id"] == "Q01-context")
        self.assertIsNot(context["facts"].get("result_within_8k"), True)

    def test_provisional_forbidden_acceptance_cannot_pass_protection(self) -> None:
        def provisional_accepted(c: dict) -> None:
            c["trace"].append({"event": "result_accepted", "case": "saved_then_bash",
                               "resultSha": one(c["trace"], "saved_then_bash", "saved")["resultSha"]})
        tools = next(x for x in self._collect_with_contract(provisional_accepted)["observations"]
                     if x["id"] == "Q01-tools")
        self.assertIsNot(tools["facts"].get("post_receipt_violation_rejected"), True)

    def test_structured_tool_arguments_and_trusted_host_closure_are_distinct_provenance(self) -> None:
        c = self.contract
        request = c["requests"]["positive"]
        outbound = [x for x in c["provider_outbound"] if x["case"] == "positive"
                    and x.get("call") and x["call"]["function"]["name"] == "db_summary_save"]
        self.assertEqual(len(outbound), 1)
        args = json.loads(outbound[0]["call"]["function"]["arguments"])
        received = one(c["trace"], "positive", "execute_received")
        self.assertEqual(args, {"summary": SUMMARY})
        self.assertEqual(received["params"], args)
        self.assertEqual(set(args), {"summary"})
        typed = json.loads((self.contract_path.parent / "private/typed-positive.json").read_text())
        self.assertEqual(typed["facts"], [args["summary"]])
        self.assertEqual({k: typed[k] for k in ("requestId", "inputKey", "skillDigest", "sources")},
                         {k: request[k] for k in ("requestId", "inputKey", "skillDigest", "sources")})
        self.assertEqual(c["backend_controls"]["result"], typed)
        route = next(x for x in self.envelope["observations"] if x["id"] == "Q01-route")
        self.assertTrue(route["facts"]["host_assembled_SummaryResult_from_structured_tool"])
        self.assertNotIn("child_emitted_full_SummaryResult", route["facts"])

    def test_startup_rebuilds_distinct_children_before_prompt_or_defers_without_post(self) -> None:
        trace, posts = self.contract["trace"], self.contract["provider_posts"]
        for case, fault, reason in (("startup_missing", "missing", "guard_api_or_missing"),
                                    ("startup_noop", "noop", "ineffective_guard"),
                                    ("startup_stale", "stale", "stale_or_invalid_valid_case")):
            with self.subTest(case=case):
                created = events(trace, case, "session_created")
                checked = events(trace, case, "startup_selfcheck")
                disposed = events(trace, case, "disposed")
                self.assertEqual([e["attempt"] for e in created], [1, 2])
                self.assertEqual([e["attempt"] for e in disposed], [1, 2])
                self.assertNotEqual(created[0]["sessionId"], created[1]["sessionId"])
                self.assertEqual([e["sessionId"] for e in checked],
                                 [e["sessionId"] for e in created])
                self.assertEqual([e["admitted"] for e in checked], [False, True])
                self.assertEqual(checked[0]["fault"], fault)
                self.assertEqual(checked[1]["checks"], {
                    "config": True, "api": True, "wrongRoster": "roster_mismatch",
                    "stale": "stale_binding"})
                self.assertEqual(one(trace, case, "startup_rejected")["reason"], reason)
                self.assertLess(trace.index(disposed[0]), trace.index(created[1]))
                self.assertLess(trace.index(checked[1]), trace.index(one(trace, case, "startup_admitted")))
                done = one(trace, case, "startup_control_done")
                self.assertTrue(done["recovered"])
                self.assertEqual(done["promptCount"], 0)
                self.assertEqual(events(trace, case, "prompt_start"), [])
                self.assertEqual([p for p in posts if p["case"] == case], [])
        case = "startup_defer"
        self.assertEqual([e["attempt"] for e in events(trace, case, "session_created")], [1, 2])
        self.assertEqual([e["attempt"] for e in events(trace, case, "disposed")], [1, 2])
        self.assertNotEqual(*[e["sessionId"] for e in events(trace, case, "session_created")])
        self.assertEqual([e["admitted"] for e in events(trace, case, "startup_selfcheck")],
                         [False, False])
        self.assertEqual(len(events(trace, case, "startup_rejected")), 2)
        self.assertEqual(len(events(trace, case, "summary_deferred")), 1)
        self.assertFalse(one(trace, case, "startup_control_done")["recovered"])
        self.assertEqual(one(trace, case, "summary_deferred")["promptCount"], 0)
        self.assertEqual(events(trace, case, "prompt_start"), [])
        self.assertEqual([p for p in posts if p["case"] == case], [])
        for case in ("positive", "saved_then_bash"):
            checked = one(trace, case, "startup_selfcheck")
            admitted = one(trace, case, "startup_admitted")
            self.assertTrue(checked["admitted"])
            self.assertLess(trace.index(checked), trace.index(admitted))
            self.assertLess(trace.index(admitted), trace.index(one(trace, case, "prompt_start")))

    def test_actual_stage_provenance_matches_selected_capabilities(self) -> None:
        stage = json.loads(self.paths["stage-provenance.json"].read_text())
        expected_binary = self.result["omp_binary_sha256"]
        expected_version = self.result["omp_version"]
        self.assertEqual(digest(Path(self.runtime["argv"][0]).resolve(strict=True)), expected_binary)
        self.assertEqual(stage["binary_sha256"], expected_binary)
        self.assertEqual(stage["version"], expected_version)
        self.assertEqual(self.prepare["omp_binary_sha256"], expected_binary)
        self.assertEqual(self.runtime["omp_binary_sha"], expected_binary)
        self.assertEqual(self.contract["omp_binary_sha"], expected_binary)
        self.assertEqual({self.prepare["omp_version"], self.runtime["omp_version"],
                          self.contract["omp_version"]}, {expected_version})
        selected = next(e["model"] for e in self.prepare["trace"] if e["event"] == "readback")
        capabilities = {k: selected[k] for k in ("id", "api", "contextWindow", "maxTokens")}
        self.assertEqual(stage["common_model_capabilities"], capabilities)
        for record in (self.runtime, self.contract):
            for e in events(record["trace"], "positive" if record is self.contract else "normal",
                            "child_model_readback"):
                self.assertEqual({k: e["model"][k] for k in capabilities}, capabilities)
        self.assertEqual(stage["prepare"]["provider_label"], selected["provider"])
        self.assertEqual(stage["contract"]["provider_label"], "q01contract")
        self.assertFalse(stage["operating_model_metadata_claimed"])

    def test_stage_binary_version_and_model_capability_mismatches_reject_typed_claim(self) -> None:
        def verdict(change, **kwargs):
            rows = self._collect_with_contract(change, **kwargs)["observations"]
            result = next(x for x in rows if x["id"] == "Q01-result")
            return result["facts"].get("typed_channel")

        self.assertIs(verdict(lambda _c: None), True, "unchanged frozen fixture must pass")
        for field, value in (("omp_binary_sha256", "0" * 64),
                             ("omp_version", "omp/other-version")):
            with self.subTest(stage="prepare", field=field):
                self.assertIsNot(verdict(lambda _c: None,
                                         change_prepare=lambda p: p.__setitem__(field, value)), True)
        for field, value in (("omp_binary_sha", "0" * 64),
                             ("omp_version", "omp/other-version")):
            with self.subTest(stage="full", field=field):
                self.assertIsNot(verdict(lambda _c: None,
                                         change_runtime=lambda r: r.__setitem__(field, value)), True)
        for field, value in (("omp_binary_sha", "0" * 64),
                             ("omp_version", "omp/other-version")):
            with self.subTest(stage="contract", field=field):
                self.assertIsNot(verdict(lambda c: c.__setitem__(field, value)), True)
        for field, value in (("id", "different-model"), ("api", "different-api"),
                             ("contextWindow", 16384), ("maxTokens", 4096)):
            for stage in ("full", "contract"):
                with self.subTest(stage=stage, field=field):
                    def mismatch(record, field=field, value=value):
                        next(e for e in record["trace"] if e["event"] == "child_model_readback")["model"][field] = value
                    if stage == "full":
                        self.assertIsNot(verdict(lambda _c: None, change_runtime=mismatch), True)
                    else:
                        self.assertIsNot(verdict(mismatch), True)

    def test_controlled_pump_start_failure_reaps_actual_owned_process(self) -> None:
        fault = json.loads(self.paths["pump-start-fault.json"].read_text())
        self.assertEqual(fault["fault"], "controlled_pump_start_failure")
        self.assertEqual(fault["caught"], fault["fault"])
        self.assertEqual(fault["source_sha256"], digest(full_probe.HERE / "run_contract.py"))
        self.assertFalse(fault["contract_evidence_created"])
        self.assertEqual(len(fault["owned_processes"]), 1)
        for proc in fault["owned_processes"]:
            self.assertFalse(proc["group_alive"])
            with self.assertRaises(ProcessLookupError):
                os.killpg(proc["pid"], 0)


if __name__ == "__main__":
    unittest.main()
