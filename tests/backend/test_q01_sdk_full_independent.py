"""Independent, artifact-bound checks for the local Q01 collector.

These tests consume an installed-OMP recording. They never launch OMP or a model.
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

from tests.backend.q01_sdk_full_probe import full_probe, run_full, run_prepare


EIGHT = ("Q01-route", "Q01-tools", "Q01-skill", "Q01-result", "Q01-context",
         "Q01-priority", "Q01-recovery", "Q01-origin")


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _event(trace: list[dict], case: str, name: str) -> dict:
    found = [e for e in trace if e.get("case") == case and e.get("event") == name]
    assert len(found) == 1, (case, name, len(found))
    return found[0]


def _owned_group_stopped(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


class FullCollectorIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_FULL_RESULT")
        output = os.environ.get("Q01_SDK_FULL_TEST_DIR")
        if not supplied or not output:
            raise AssertionError("explicit actual result and private test output are required")
        cls.result_path = Path(supplied).resolve(strict=True)
        cls.result = json.loads(cls.result_path.read_text())
        cls.paths = {key: Path(path).resolve(strict=True)
                     for key, path in cls.result["artifacts"].items()}
        cls.runtime = json.loads(cls.paths["runtime_evidence"].read_text())
        cls.prepare = json.loads(cls.paths["prepare_evidence"].read_text())
        cls.envelope = json.loads(cls.paths["collector_envelope"].read_text())
        cls.backend = json.loads(cls.paths["backend_controls"].read_text())
        cls.frozen = json.loads(cls.paths["frozen_log"].read_text())
        cls.output = Path(output).resolve(strict=True)

    def _collect_counterfactual(self, change) -> dict:
        """Run the real mapper with one changed recorded input and no OMP execution."""
        recorded = deepcopy(self.runtime)
        change(recorded)
        with tempfile.TemporaryDirectory(dir=self.output, prefix="collector-input-") as tmp:
            out = Path(tmp) / "runtime"

            def supply(path: Path) -> int:
                self.assertEqual(path, out)
                private = out / "private"
                private.mkdir(parents=True)
                (out / "runtime-evidence.json").write_text(json.dumps(recorded))
                (private / "trace.jsonl").write_text(
                    "".join(json.dumps(e) + "\n" for e in recorded["trace"]))
                shutil.copyfile(self.paths["runtime_evidence"].parent / "private/summary.sqlite3",
                                private / "summary.sqlite3")
                return 0

            prepared = {
                "closed": False, "runtime_output": str(out),
                "prepare_model": {key: self.prepare["trace"][0]["model"][key]
                                  for key in ("provider", "id", "api", "contextWindow", "maxTokens")},
                "prepare_thinking": self.prepare["trace"][0].get("thinkingLevel"),
                "prepare_tier": self.prepare["trace"][0].get("serviceTierByFamily"),
                "route_identity": {"selection_epoch": self.prepare["trace"][0]["sessionId"]},
            }
            with ExitStack() as stack:
                stack.enter_context(patch.object(full_probe.run_full, "main", side_effect=supply))
                stack.enter_context(patch.object(full_probe, "_backend_result_controls",
                                         return_value=self.backend))
                return full_probe.collect(prepared)

    def test_prepare_has_no_transport_and_runtime_has_distinct_child_epochs(self) -> None:
        p, r = self.prepare, self.runtime
        self.assertEqual(p["provider_posts"], [])
        self.assertEqual([e["event"] for e in p["trace"]], ["readback", "done", "disposed"])
        readback = p["trace"][0]
        self.assertEqual(readback["active"], ["db_summary_save"])
        self.assertTrue(readback["privateHas"] and not readback["globalHas"])
        self.assertEqual(p["process"]["remaining_owned"], [])
        self.assertTrue(p["process"]["pump_stopped"])
        self.assertTrue(_owned_group_stopped(p["process"]["pid"]))
        self.assertTrue(all(_digest(Path(path)) == digest for path, digest in
                            p["source_sha"].items()))
        self.assertEqual(_digest(Path(p["argv"][0])), p["omp_binary_sha256"])
        child_models = [e for e in r["trace"] if e.get("event") == "child_model_readback"]
        self.assertTrue(child_models)
        self.assertTrue(all(e["sessionId"] != readback["sessionId"] for e in child_models))
        selected = {key: readback["model"][key]
                    for key in ("provider", "id", "api", "contextWindow", "maxTokens")}
        self.assertTrue(all(e["model"] == selected for e in child_models))
        self.assertEqual(r["process"]["remaining_owned"], [])
        self.assertTrue(r["process"]["pump_stopped"])
        self.assertTrue(_owned_group_stopped(r["process"]["pid"]))

    def test_actual_custody_eight_rows_and_incomplete_verdicts(self) -> None:
        r = self.runtime
        for name, expected in self.result["source_sha256"].items():
            self.assertEqual(_digest(full_probe.ROOT / name), expected, name)
        for name, expected in self.result["artifact_sha256"].items():
            path = Path(name)
            self.assertEqual(_digest(path if path.is_absolute() else full_probe.ROOT / path),
                             expected, name)
        self.assertEqual(full_probe._verified_runtime(r, self.paths["runtime_evidence"]), [])
        self.assertEqual(_digest(Path(r["argv"][0])), r["omp_binary_sha"])
        with sqlite3.connect("file:" + str(self.paths["runtime_evidence"].parent /
                                            "private/summary.sqlite3") + "?mode=ro", uri=True) as conn:
            rows = [list(x) for x in conn.execute("SELECT id, summary FROM summaries ORDER BY id")]
        self.assertEqual(rows, r["db_rows"])
        self.assertEqual(len(rows), 8)
        self.assertEqual(r["sentinels"], {"command-sentinel": "ORIGINAL", "file-sentinel": "ORIGINAL"})
        observations = self.envelope["observations"]
        self.assertEqual(tuple(x["id"] for x in observations), EIGHT)
        route = next(x for x in observations if x["id"] == "Q01-route")
        context = next(x for x in observations if x["id"] == "Q01-context")
        self.assertNotIn("product_existing_manager_route_not_integrated", route["unknowns"])
        self.assertIn("typed_SummaryResult_child_return_unobserved", route["unknowns"])
        self.assertFalse(any("operating_model" in x for x in context["unknowns"]))
        self.assertIn("collector_source_selection_1MiB_unobserved", context["unknowns"])
        self.assertEqual(set(self.frozen["verdicts"]), set(EIGHT))
        self.assertTrue(all(v["result"] == "inconclusive" for v in self.frozen["verdicts"].values()))
        self.assertTrue(all(x["unknowns"] for x in observations))
        self.assertEqual(Counter(p["case"] for p in r["provider_posts"])["repeat_fail"], 0)
        self.assertEqual(Counter(p["case"] for p in r["provider_posts"])["bash"], 1)
        self.assertFalse(self.result["full_q01_pass"])

    def test_closed_context_and_obviously_bad_source_digest_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            full_probe.collect({"closed": True})
        broken = deepcopy(self.runtime)
        source = next(iter(broken["source_sha"]))
        broken["source_sha"][source] = "0" * 64
        self.assertTrue(any(x.startswith("source_identity_changed:") for x in
                            full_probe._verified_runtime(broken, self.paths["runtime_evidence"])))

    def test_missing_source_manifest_must_not_pass_custody(self) -> None:
        broken = deepcopy(self.runtime)
        broken.pop("source_sha")
        self.assertTrue(any("source" in issue for issue in
                            full_probe._verified_runtime(broken, self.paths["runtime_evidence"])))
        broken = deepcopy(self.runtime)
        broken["source_sha"]["/private/unexpected-source.py"] = "0" * 64
        self.assertTrue(any("source_manifest" in issue for issue in
                            full_probe._verified_runtime(broken, self.paths["runtime_evidence"])))

    def test_missing_raw_trace_or_sqlite_rows_are_custody_gaps(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.output, prefix="missing-custody-") as tmp:
            path = Path(tmp) / "runtime-evidence.json"
            self.assertIn("raw_trace_mismatch", full_probe._verified_runtime(self.runtime, path))
            self.assertIn("private_db_missing", full_probe._verified_runtime(self.runtime, path))

    def test_db_posts_cannot_substitute_for_request_bound_receipt(self) -> None:
        def mismatch_request_receipt(recorded: dict) -> None:
            receipt = _event(recorded["trace"], "normal", "tool_end")
            receipt["result"] = json.dumps({**json.loads(receipt["result"]),
                                            "request_id": "unrelated-request"})
            # Provider posts remain sole-DB; the successful tool receipt is for another request.

        observed = self._collect_counterfactual(mismatch_request_receipt)
        tools = next(x for x in observed["observations"] if x["id"] == "Q01-tools")
        self.assertIsNot(tools["facts"].get("receipt_request_bound"), True)

    def test_rebuild_success_cannot_substitute_for_epoch_bound_receipt(self) -> None:
        def break_epoch(recorded: dict) -> None:
            _event(recorded["trace"], "normal", "before_prompt")["receipt"]["epoch"] = "stale-epoch"

        observed = self._collect_counterfactual(break_epoch)
        tools = next(x for x in observed["observations"] if x["id"] == "Q01-tools")
        self.assertIsNot(tools["facts"].get("session_turn_epoch_bound"), True)

    def test_private_roster_cannot_substitute_for_existing_agent_identity(self) -> None:
        def remove_agent_identity(recorded: dict) -> None:
            recorded["checks"]["system_exact"] = False
            recorded["trace"][0].pop("bodySha", None)
            recorded["trace"][0].pop("skillSha", None)

        observed = self._collect_counterfactual(remove_agent_identity)
        route = next(x for x in observed["observations"] if x["id"] == "Q01-route")
        self.assertIsNot(route["facts"].get("no_new_agent"), True)

    def test_fixture_self_consistency_does_not_prove_installed_agent_identity(self) -> None:
        route = next(x for x in self.envelope["observations"] if x["id"] == "Q01-route")
        self.assertTrue(route["facts"]["fixture_system_bytes_exact"])
        self.assertTrue(all(post["system_exact"] for post in self.runtime["provider_posts"]))
        self.assertNotIn("existing_agent", route["facts"])
        self.assertNotIn("no_new_agent", route["facts"])
        self.assertIn("authoritative_installed_task_definition_and_no_new_agent_unobserved",
                      route["unknowns"])

    def test_child_receipt_and_synthetic_result_do_not_prove_parent_channels(self) -> None:
        rows = {x["id"]: x for x in self.envelope["observations"]}
        context, result, tools = rows["Q01-context"], rows["Q01-result"], rows["Q01-tools"]
        child_receipts = [e["result"].encode("utf-8") for e in self.runtime["trace"]
                          if e.get("event") == "tool_end" and e.get("toolName") == "db_summary_save"
                          and e.get("isError") is False]
        self.assertGreaterEqual(len(child_receipts), 3)
        self.assertLessEqual(max(map(len, child_receipts)), 4096)
        self.assertTrue(context["facts"]["child_tool_receipt_within_4k"])
        self.assertNotIn("parent_receipt_within_4k", context["facts"])
        self.assertNotIn("no_main_raw_accumulation", context["facts"])
        self.assertNotIn("result_within_8k", context["facts"])
        self.assertIn("parent_receipt_bytes_and_transcript_delta_unobserved", context["unknowns"])
        self.assertIn("actual_typed_summary_result_utf8_8k_unobserved", context["unknowns"])
        self.assertEqual(result["source"], "backend_event")
        self.assertEqual(Path(result["evidence_ref"]), self.paths["backend_controls"])
        self.assertIn("synthetic typed contract", self.backend["source"])
        self.assertTrue(result["facts"]["synthetic_contract_result_within_8k"])
        self.assertNotIn("typed_channel", result["facts"])
        self.assertIn("typed_SummaryResult_from_OMP_child_unobserved", result["unknowns"])
        self.assertIn("upper_turn_permissions_and_transcript_unobserved", tools["unknowns"])

    def test_missing_omp_does_not_allocate_provider_or_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.output, prefix="prepare-missing-") as tmp:
            output = Path(tmp) / "unused"
            with patch.object(run_prepare.shutil, "which", return_value=None), \
                 patch.object(run_prepare, "ThreadingHTTPServer", side_effect=AssertionError("allocated")):
                with self.assertRaisesRegex(RuntimeError, "installed OMP not found"):
                    run_prepare.run(output)
            self.assertFalse(output.exists())

    def test_prepare_version_and_popen_failures_close_owned_provider(self) -> None:
        class Provider:
            server_port = 32321

            def __init__(self, *_args: object) -> None:
                self.shutdown_called = False
                self.close_called = False

            def serve_forever(self) -> None:
                raise AssertionError("fake provider should not be served")

            def shutdown(self) -> None:
                self.shutdown_called = True

            def server_close(self) -> None:
                self.close_called = True

        class Thread:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.started = False
                self.joined = False

            def start(self) -> None:
                self.started = True

            def join(self, _timeout: int) -> None:
                self.joined = True

        omp = str(Path(self.prepare["argv"][0]))
        for boundary in ("version", "popen"):
            with self.subTest(boundary=boundary):
                providers: list[Provider] = []
                threads: list[Thread] = []

                def provider(*args: object) -> Provider:
                    item = Provider(*args)
                    providers.append(item)
                    return item

                def thread(*args: object, **kwargs: object) -> Thread:
                    item = Thread(*args, **kwargs)
                    threads.append(item)
                    return item

                with tempfile.TemporaryDirectory(dir=self.output, prefix="prepare-failure-") as tmp:
                    output = Path(tmp) / "run"
                    version = subprocess.CompletedProcess([omp, "--version"], 0, "omp/18.8.6\n", "")
                    with patch.object(run_prepare.shutil, "which", return_value=omp), \
                         patch.object(run_prepare, "ThreadingHTTPServer", side_effect=provider), \
                         patch.object(run_prepare.threading, "Thread", side_effect=thread), \
                         patch.object(run_prepare.subprocess, "run",
                                      side_effect=RuntimeError("version failure") if boundary == "version"
                                      else None, return_value=version), \
                         patch.object(run_prepare.subprocess, "Popen",
                                      side_effect=RuntimeError("Popen failure")) as popen:
                        with self.assertRaisesRegex(RuntimeError, "version failure|Popen failure"):
                            run_prepare.run(output)
                    self.assertEqual(len(providers), 1)
                    self.assertEqual(len(threads), 1)
                    self.assertTrue(threads[0].started and threads[0].joined)
                    self.assertTrue(providers[0].shutdown_called and providers[0].close_called)
                    self.assertEqual(popen.call_count, 0 if boundary == "version" else 1)

    def test_full_runtime_prelaunch_failures_close_provider_and_thread(self) -> None:
        class Provider:
            server_port = 32322

            def __init__(self, _private: Path) -> None:
                self.shutdown_called = False
                self.close_called = False

            def serve_forever(self) -> None:
                raise AssertionError("fake provider should not be served")

            def shutdown(self) -> None:
                self.shutdown_called = True

            def server_close(self) -> None:
                self.close_called = True

        class Thread:
            ident = 1

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.started = False
                self.joined = False

            def start(self) -> None:
                self.started = True

            def is_alive(self) -> bool:
                return self.started and not self.joined

            def join(self, _timeout: int) -> None:
                self.joined = True

        omp = str(Path(self.runtime["argv"][0]))
        for boundary in ("missing_omp", "version", "popen"):
            with self.subTest(boundary=boundary):
                providers: list[Provider] = []
                threads: list[Thread] = []

                def provider(private: Path) -> Provider:
                    item = Provider(private)
                    providers.append(item)
                    return item

                def thread(*args: object, **kwargs: object) -> Thread:
                    item = Thread(*args, **kwargs)
                    threads.append(item)
                    return item

                with tempfile.TemporaryDirectory(dir=self.output, prefix="full-prelaunch-") as tmp:
                    out = Path(tmp) / "run"
                    version = subprocess.CompletedProcess([omp, "--version"], 0, "omp/18.8.6\n", "")
                    with patch.object(run_full, "OUT", None), \
                         patch.object(run_full, "Provider", side_effect=provider), \
                         patch.object(run_full.threading, "Thread", side_effect=thread), \
                         patch.object(run_full.shutil, "which",
                                      return_value=None if boundary == "missing_omp" else omp), \
                         patch.object(run_full.subprocess, "run",
                                      side_effect=RuntimeError("version failure") if boundary == "version"
                                      else None, return_value=version) as version_call, \
                         patch.object(run_full.subprocess, "Popen",
                                      side_effect=RuntimeError("Popen failure")) as popen:
                        with self.assertRaisesRegex(RuntimeError,
                                                    "installed OMP not found|version failure|Popen failure"):
                            run_full.main(out)
                    self.assertEqual(len(providers), 1)
                    self.assertEqual(len(threads), 1)
                    self.assertTrue(threads[0].started and threads[0].joined)
                    self.assertTrue(providers[0].shutdown_called and providers[0].close_called)
                    self.assertEqual(version_call.call_count, 0 if boundary == "missing_omp" else 1)
                    self.assertEqual(popen.call_count, 1 if boundary == "popen" else 0)


if __name__ == "__main__":
    unittest.main()
