"""Real owned HTTP/SQLite recovery control; no OMP/model or disconnect proof."""
from copy import deepcopy
from hashlib import sha256
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.backend import summary_compat_runtime as runtime
from workbench.backend.paths import DataLayout
from workbench.backend.summary_compatibility import SummaryCompatibility
from workbench.backend.summary_compat_provider import ScriptedCompatibilityProvider
from workbench.backend.summary_writer import store_summary
from workbench.contracts.logs_v1 import canonical_json, summary_input_key
from workbench.storage.important_log import ImportantLogCatalog, ImportantLogStore
from workbench.tasks.repository import TaskRepository


class RecoveryControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir(mode=0o700)
        self.layout = DataLayout(self.root / "data")
        self.catalog = ImportantLogCatalog(self.layout.important_logs, create=True)
        with TaskRepository(self.layout.tasks):
            pass
        self.layout.tasks.chmod(0o600)
        subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1, "commandId": subject["id"]}
        span = {"schemaVersion": 1, "source": source, "start": 0, "end": 4, "digest": sha256(b"work").hexdigest()}
        self.request = {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject, "revision": None,
            "authorizationRef": str(uuid4()), "authorizationEpoch": 1, "inputKey": summary_input_key(subject, None, [span], [], "a" * 64),
            "skillName": "important-log-summary", "skillDigest": "a" * 64, "sources": [span], "sourceGaps": [],
            "priorCumulativeEntryId": None, "dbHealth": self.catalog.health_snapshot()["active"], "requestedAt": "2026-10-13T00:00:00Z"}
        with TaskRepository(self.layout.tasks) as journal:
            journal.reserve_summary_request(self.request)
            journal.record_summary_state(self.request["requestId"], "sending")
        self.system = "Fixed owned recovery test system"
        (self.root / "expected.json").write_text(json.dumps({"requestId": self.request["requestId"],
            "systemSha": sha256(self.system.encode()).hexdigest()}))
        self.set_expected_prompt(self.prompt_text())
        self.posts, self.violations = [], []
        self.control = runtime._RecoveryControl(self.root)
        self.server = runtime._Server(("127.0.0.1", 0), runtime._handler(self.root, self.posts, self.violations, self.control))
        self.serve = threading.Thread(target=self.server.serve_forever)
        self.serve.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.shutdown_accepted()
        handlers = tuple(self.server._threads) if isinstance(self.server._threads, list) else ()
        self.server.server_close()
        self.serve.join(timeout=1)
        self.assertFalse(self.serve.is_alive())
        self.assertFalse(any(thread.is_alive() for thread in handlers))
        self.assertEqual(self.server.accepted_count(), 0)

    def prompt_text(self, request=None):
        prompt = {"request": request or self.request, "selected": [{"range": self.request["sources"][0], "text": "work"}],
            "omitted": [], "priorCumulative": None, "resultByteLimit": 8192,
            "instruction": "Use db_summary_save exactly once, then stop."}
        return json.dumps(prompt, ensure_ascii=False)

    def set_expected_prompt(self, prompt):
        expected = json.loads((self.root / "expected.json").read_text())
        raw = prompt.encode("utf-8")
        expected.update(promptSha256=sha256(raw).hexdigest(), promptBytes=len(raw))
        (self.root / "expected.json").write_text(json.dumps(expected))

    def post(self, request=None, *, continuation=False, system=None, content=None):
        body = {"model": "scripted", "tools": [{"type": "function", "function": {"name": "db_summary_save"}}],
            "messages": [{"role": "system", "content": system or self.system},
                         {"role": "user", "content": self.prompt_text(request) if content is None else content}]}
        if continuation:
            body["messages"].append({"role": "tool", "content": "received", "tool_call_id": "owned-save-1"})
        client = HTTPConnection(*self.server.server_address, timeout=2)
        try:
            client.request("POST", "/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
            response = client.getresponse()
            return response.status, response.read()
        finally:
            client.close()

    def record(self):
        with TaskRepository(self.layout.tasks) as journal:
            return journal.get_summary_request(self.request["requestId"])

    def test_unknown_before_response_refuses_replay_and_late_duplicate_is_singleton(self):
        status, response = self.post()
        self.assertEqual(status, 200)
        self.assertIn(b"db_summary_save", response)
        frame = json.loads(next(line[6:] for line in response.decode().splitlines() if line.startswith("data: {")))
        params = json.loads(frame["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(self.record()["state"], "unknown")
        self.assertTrue(self.control.observations["request_binding_matches"])
        self.assertTrue(self.control.observations["unknown_before_response"])
        self.assertTrue(self.control.observations["replay_reserve_refused"])
        self.assertFalse(self.control.observations["replay_row_present"])
        with TaskRepository(self.layout.tasks) as journal:
            self.assertIsNone(journal.get_summary_request(self.control.observations["replay_request_id"]))
            details = journal._connection.execute(
                "SELECT detail FROM summary_request_events_v1 WHERE request_id=? AND state='unknown'", (self.request["requestId"],)).fetchall()
            self.assertEqual([row[0] for row in details], ["owner_controlled_unknown_before_response"])
        # Continuation belongs to this same request; reserve refusal adds no POST.
        self.assertEqual(len(self.posts), 1)
        status, _ = self.post(continuation=True)
        self.assertEqual(status, 200)
        result = {name: deepcopy(self.request[name]) for name in ("schemaVersion", "requestId", "inputKey", "skillDigest", "sources")}
        result.update(params)
        result["usage"] = {"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None}
        writer = store_summary(self.root / "data", self.request["requestId"], result)
        self.assertTrue(writer["saved"])
        first_receipt = self.record()["receipt"]
        completed = {"requestId": self.request["requestId"], "outcome": {"result": result, "writer": writer}}
        self.control.after_result(completed, self.catalog)
        facts = self.control.observations
        self.assertEqual(facts["state_order"], ["prepared", "sending", "unknown", "stored"])
        self.assertEqual(facts["state_after_duplicate"], "stored")
        self.assertFalse(facts["duplicate_receipt_accepted"])
        self.assertTrue(facts["receipt_unchanged"])
        self.assertEqual((facts["duplicate_drop_count"], facts["receipt_count"], facts["catalog_entries_after_duplicate"]), (1, 1, 1))
        self.assertEqual(self.record()["receipt"], first_receipt)
        self.assertEqual(first_receipt["result_digest"], sha256(canonical_json(result)).hexdigest())
        with ImportantLogStore.open(self.layout.important_logs, self.catalog.active_uuid(), readonly=True) as store:
            self.assertEqual(store.get_entry(writer["entryId"])["body"], {
                "schemaVersion": 1, "facts": ["소유 private scripted fixture의 연결을 확인했다."],
                "interpretations": [], "unknowns": ["실제 MODEL 및 전체 Q01은 미검증이다."]})
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.violations, [])
        self.assertEqual(facts["pause_restart"], "not_run")
        print("recovery_control_observations=" + json.dumps(facts))

    def test_mismatched_prompt_request_never_mutates_journal(self):
        wrong = deepcopy(self.request)
        wrong["authorizationEpoch"] += 1
        self.set_expected_prompt(self.prompt_text(wrong))
        status, _ = self.post(wrong)
        self.assertEqual(status, 400)
        self.assertEqual(self.record()["state"], "sending")
        with TaskRepository(self.layout.tasks) as journal:
            self.assertEqual(journal._connection.execute("SELECT COUNT(*) FROM summary_requests_v1").fetchone()[0], 1)
            self.assertEqual(journal._connection.execute("SELECT COUNT(*) FROM summary_request_events_v1").fetchone()[0], 2)
        self.assertEqual(self.control.observations["prompt_matching_parts"], 1)
        self.assertNotIn("unknown_before_response", self.control.observations)
        self.assertIn("recovery_prompt_binding_mismatch", self.violations[0])

    def test_sdk_reminder_and_prompt_parts_match_exactly_one_without_flattening(self):
        reminder = "<system-reminder>user date at /owned/private/cwd</system-reminder>"
        prompt = self.prompt_text()
        status, _ = self.post(content=[{"type": "text", "text": reminder}, {"type": "text", "text": prompt}])
        self.assertEqual(status, 200)
        self.assertEqual(self.record()["state"], "unknown")
        self.assertEqual(self.control.observations["prompt_matching_parts"], 1)
        self.assertEqual(self.control.observations["sdk_envelope_parts_unattributed"], [{
            "type": "text", "bytes": len(reminder.encode()), "sha256": sha256(reminder.encode()).hexdigest()}])
        self.assertNotIn(reminder, json.dumps(self.control.observations))
        self.assertNotIn("/owned/private/cwd", json.dumps(self.control.observations))

    def test_zero_exact_matches_fail_closed_without_trim_or_mutation(self):
        status, _ = self.post(content=[{"type": "text", "text": self.prompt_text() + " "}])
        self.assertEqual(status, 400)
        self.assertEqual(self.record()["state"], "sending")
        self.assertEqual(self.control.observations["prompt_matching_parts"], 0)
        self.assertIn("prompt_envelope_unrecognized", self.violations[0])
        with TaskRepository(self.layout.tasks) as journal:
            self.assertEqual(journal._connection.execute("SELECT COUNT(*) FROM summary_request_events_v1").fetchone()[0], 2)

    def test_two_exact_matching_parts_fail_closed_without_mutation(self):
        part = {"type": "text", "text": self.prompt_text()}
        status, _ = self.post(content=[part, part])
        self.assertEqual(status, 400)
        self.assertEqual(self.record()["state"], "sending")
        self.assertEqual(self.control.observations["prompt_matching_parts"], 2)
        self.assertIn("prompt_envelope_unrecognized", self.violations[0])
        with TaskRepository(self.layout.tasks) as journal:
            self.assertEqual(journal._connection.execute("SELECT COUNT(*) FROM summary_requests_v1").fetchone()[0], 1)
            self.assertEqual(journal._connection.execute("SELECT COUNT(*) FROM summary_request_events_v1").fetchone()[0], 2)

    def test_existing_system_guard_runs_before_recovery_mutation(self):
        status, _ = self.post(system="wrong system")
        self.assertEqual(status, 400)
        self.assertEqual(self.record()["state"], "sending")
        self.assertEqual(self.control.observations, {})

    def test_journal_api_error_does_not_fall_back_or_serve_success(self):
        with patch.object(TaskRepository, "record_summary_state", side_effect=sqlite3.OperationalError("owned fixture busy")):
            status, _ = self.post()
        self.assertEqual(status, 400)
        self.assertEqual(self.record()["state"], "sending")
        self.assertNotIn("unknown_before_response", self.control.observations)
        self.assertIn("OperationalError:owned fixture busy", self.violations[0])

    def test_partial_recovery_record_retains_protection_and_never_passes(self):
        identity = {"policy_version": 1, "unknowns": [], "policy_sha256": "1" * 64,
            "closure_digest": "2" * 64, "runtime_digest": "3" * 64,
            "stable_host": {"os_family": "Linux", "architecture": "x86_64", "python_minor": "3.12"}}
        service = SummaryCompatibility(self.root / "compat", identity=lambda: identity)
        service.protect({"episode": "existing", "reason": "compatibility_failed"})
        observed = runtime.unavailable("whole_q01_unverified")
        observed.update(level="inconclusive", items={"Q01-recovery": "inconclusive"},
            observations=[{"kind": "owner_controlled_recovery", "result": "inconclusive", "pause_restart": "not_run"}],
            io={"omp_children": 1, "model_calls": 2, "network_requests": None, "auth_store_open": None, "settings_writes": None})
        with patch.object(ScriptedCompatibilityProvider, "observe", return_value=observed):
            result = service.verify_scripted()
        self.assertEqual(result["result"], "inconclusive")
        self.assertEqual(result["items"]["Q01-recovery"], "inconclusive")
        self.assertEqual(result["items"]["Q01-priority"], "not_run")
        self.assertFalse(service.verify_reference(result["id"]))
        self.assertFalse(service.admission())
        self.assertEqual(service.status()["reason"], "compatibility_failed")
        self.assertIn("output_budget_unavailable", result["model_metadata"]["unknowns"])


if __name__ == "__main__":
    unittest.main()
