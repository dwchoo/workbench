"""Owned recovery-control fixtures; no native OMP or operating MODEL proof.

These tests use real loopback HTTP/SSE, journals and the fixed writer. The SDK
content shape is reproduced with an unattributed reminder and exact prompt part.
"""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from http.client import HTTPConnection
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.backend import summary_compat_runtime as runtime
from workbench.backend.paths import DataLayout
from workbench.backend.summary_compat_provider import ScriptedCompatibilityProvider
from workbench.backend.summary_compatibility import Q01_ITEMS, SummaryCompatibility
from workbench.backend.summary_writer import store_summary
from workbench.contracts.logs_v1 import canonical_json, summary_input_key
from workbench.storage.important_log import ImportantLogCatalog, ImportantLogStore
from workbench.tasks.repository import InvalidTransitionError, TaskRepository


ROOT = Path(__file__).resolve().parents[2]
SYSTEM = "Owned recovery fixture: use the bound DB tool only."
REMINDER = "SDK reminder fixture: unrelated context must remain unattributed."
FIXED_BODY = {"facts": ["소유 private scripted fixture의 연결을 확인했다."],
              "interpretations": [], "unknowns": ["실제 MODEL 및 전체 Q01은 미검증이다."]}


def product_prompt(root, request):
    """Execute the actual SDK-free host prompt helper, without mirroring it."""
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node24 prompt helper runtime unavailable")
    prepared = {"request": dict(reversed(list(request.items()))), "selected": [],
                "omitted": [], "priorCumulative": None}
    script = ("import { readFileSync } from 'node:fs'; "
              "const { buildSummaryPrompt } = await import(process.argv[1]); "
              "process.stdout.write(buildSummaryPrompt(JSON.parse(readFileSync(0, 'utf8'))));")
    completed = subprocess.run([node, "--input-type=module", "-e", script,
                                (ROOT / "omp_bridge/g3/summary_sdk.ts").as_uri()],
                               input=json.dumps(prepared, ensure_ascii=False).encode(),
                               env=runtime._environment(root), cwd=root,
                               capture_output=True, timeout=5)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.decode())
    return completed.stdout.decode("utf-8")


def expected_prompt(root, request, prompt):
    (root / "expected.json").write_bytes(canonical_json({
        "requestId": request["requestId"], "systemSha": sha256(SYSTEM.encode()).hexdigest(),
        "promptSha256": sha256(prompt.encode()).hexdigest(), "promptBytes": len(prompt.encode())}))


def prepared_request(root):
    layout = DataLayout(root / "data")
    layout.root.mkdir(mode=0o700, exist_ok=True)
    catalog = ImportantLogCatalog(layout.important_logs, create=True)
    subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
    raw = "independent recovery source".encode()
    span = {"schemaVersion": 1, "source": {"schemaVersion": 1, "sourceKind": "terminal",
            "collectorEpoch": 11, "commandId": subject["id"]}, "start": 0,
            "end": len(raw), "digest": sha256(raw).hexdigest()}
    skill = "c" * 64
    request = {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject,
               "revision": None, "authorizationRef": str(uuid4()), "authorizationEpoch": 1,
               "inputKey": summary_input_key(subject, None, [span], [], skill),
               "skillName": "important-log-summary", "skillDigest": skill,
               "sources": [span], "sourceGaps": [], "priorCumulativeEntryId": None,
               "dbHealth": catalog.health_snapshot()["active"], "requestedAt": "2026-10-13T00:00:00Z"}
    with TaskRepository(layout.tasks) as journal:
        journal.reserve_summary_request(request)
        journal.record_summary_state(request["requestId"], "sending")
    layout.tasks.chmod(0o600)
    expected_prompt(root, request, product_prompt(root, request))
    return request, catalog


def first_body(root, request):
    return {"model": "scripted", "max_completion_tokens": 8192,
            "tools": [{"type": "function", "function": {"name": "db_summary_save"}}],
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": [
                             {"type": "text", "text": REMINDER},
                             {"type": "text", "text": product_prompt(root, request)}]}]}


def post(port, body, path="/v1/chat/completions"):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("POST", path, canonical_json(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def result_from_sse(request, payload):
    frames = [json.loads(line[6:]) for line in payload.decode().splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    tool = frames[0]["choices"][0]["delta"]["tool_calls"][0]["function"]
    if tool["name"] != "db_summary_save":
        raise AssertionError("unexpected typed tool")
    result = {key: deepcopy(request[key]) for key in
              ("schemaVersion", "requestId", "inputKey", "skillDigest", "sources")}
    result.update(json.loads(tool["arguments"]))
    result["usage"] = {"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None}
    return result


def journal_snapshot(root):
    with TaskRepository(DataLayout(root / "data").tasks) as journal:
        return {table: [tuple(row) for row in journal._connection.execute("SELECT * FROM " + table)]
                for table in ("summary_requests_v1", "summary_request_ranges_v1", "summary_request_events_v1",
                              "summary_result_receipts_v1", "summary_result_drops_v1")}


@contextmanager
def owned_http(root, recovery):
    posts, violations = [], []
    response_states = []
    product_handler = runtime._handler(root, posts, violations, recovery)

    class ObservedResponse(product_handler):
        def send_response(self, code, message=None):
            if code == 200 and recovery is not None:
                with TaskRepository(DataLayout(root / "data").tasks) as journal:
                    response_states.append(journal.get_summary_request(recovery.request_id)["state"])
            super().send_response(code, message)

    server = runtime._Server(("127.0.0.1", 0), ObservedResponse)
    server.response_states = response_states
    serve = threading.Thread(target=server.serve_forever, name="independent-recovery-http")
    serve.start()
    try:
        yield server, posts, violations
    finally:
        server.shutdown()
        server.shutdown_accepted()
        handlers = tuple(server._threads) if isinstance(server._threads, list) else ()
        server.server_close()
        serve.join(timeout=2)
        assert not serve.is_alive()
        assert all(not thread.is_alive() for thread in handlers)
        assert server.accepted_count() == 0


class RecoveryControlIndependentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.request, self.catalog = prepared_request(self.root)

    def state(self):
        with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
            return journal.get_summary_request(self.request["requestId"])

    def test_bound_http_sse_unknown_late_fixed_writer_and_duplicate_are_durable_once(self):
        recovery = runtime._RecoveryControl(self.root)
        with owned_http(self.root, recovery) as (server, posts, violations):
            status, payload = post(server.server_port, first_body(self.root, self.request))
            self.assertEqual(status, 200)
            self.assertEqual(violations, [])
            self.assertEqual(len(posts), 1)
            self.assertEqual(server.response_states, ["unknown"])
            self.assertEqual(self.state()["state"], "unknown")
            self.assertTrue(recovery.observations["request_binding_matches"])
            self.assertEqual(recovery.observations["prompt_matching_parts"], 1)
            self.assertEqual(recovery.observations["sdk_envelope_parts_unattributed"], [
                {"type": "text", "bytes": len(REMINDER.encode()), "sha256": sha256(REMINDER.encode()).hexdigest()}])
            self.assertNotIn(REMINDER, json.dumps(recovery.observations))
            self.assertEqual(recovery.observations["unknown_detail"], "owner_controlled_unknown_before_response")
            replay = deepcopy(self.request)
            replay["requestId"] = str(uuid4())
            with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
                with self.assertRaises(InvalidTransitionError):
                    journal.reserve_summary_request(replay)
                self.assertIsNone(journal.get_summary_request(replay["requestId"]))
                self.assertIsNone(journal.get_summary_request(recovery.observations["replay_request_id"]))
            typed = result_from_sse(self.request, payload)
            self.assertEqual({key: typed[key] for key in FIXED_BODY}, FIXED_BODY)
            written = store_summary(self.root / "data", self.request["requestId"], typed)
            self.assertTrue(written["saved"])
            first = self.state()["receipt"]
            self.assertEqual(first["result_digest"], sha256(canonical_json(typed)).hexdigest())
            completed = {"requestId": self.request["requestId"], "outcome": {
                "result": typed, "writer": written}}
            recovery.after_result(completed, self.catalog)
            self.assertIs(recovery.observations["duplicate_receipt_accepted"], False)
            self.assertEqual(recovery.observations["pause_restart"], "not_run")
            self.assertEqual(self.state()["receipt"], first)
            snapshot = journal_snapshot(self.root)
            self.assertEqual(len(snapshot["summary_requests_v1"]), 1)
            self.assertEqual(len(snapshot["summary_result_receipts_v1"]), 1)
            self.assertEqual(len(snapshot["summary_result_drops_v1"]), 1)
            with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
                states = [tuple(row) for row in journal._connection.execute(
                    "SELECT state,detail FROM summary_request_events_v1 ORDER BY event_id")]
                self.assertEqual([row[0] for row in states], ["prepared", "sending", "unknown", "stored"])
                self.assertEqual(states[2][1], "owner_controlled_unknown_before_response")
                self.assertEqual(journal._connection.execute(
                    "SELECT reason FROM summary_result_drops_v1").fetchone()[0], "duplicate")
            with ImportantLogStore.open(DataLayout(self.root / "data").important_logs,
                                       self.catalog.active_uuid(), readonly=True) as store:
                self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)
                self.assertEqual(store.get_entry(written["entryId"])["body"], {"schemaVersion": 1, **FIXED_BODY})
            self.assertFalse(store_summary(self.root / "data", self.request["requestId"], typed)["saved"])
            self.assertEqual(journal_snapshot(self.root), snapshot)

    def test_malformed_or_mismatched_user_prompt_cannot_mutate_any_journal_table(self):
        for kind in ("malformed", "mismatched"):
            with self.subTest(kind=kind):
                recovery = runtime._RecoveryControl(self.root)
                body = first_body(self.root, self.request)
                if kind == "malformed":
                    body["messages"][1]["content"][1]["text"] = '{"request":'
                else:
                    mismatch = deepcopy(self.request)
                    mismatch["authorizationEpoch"] += 1
                    prompt = product_prompt(self.root, mismatch)
                    body["messages"][1]["content"][1]["text"] = prompt
                    expected_prompt(self.root, self.request, prompt)
                before = journal_snapshot(self.root)
                with owned_http(self.root, recovery) as (server, _, violations):
                    status, _ = post(server.server_port, body)
                    self.assertEqual(status, 400)
                    self.assertTrue(violations)
                    self.assertNotIn("unknown_before_response", recovery.observations)
                    self.assertEqual(recovery.observations["prompt_matching_parts"], 0 if kind == "malformed" else 1)
                    expected_error = "prompt_envelope_unrecognized" if kind == "malformed" else "recovery_prompt_binding_mismatch"
                    self.assertTrue(any(expected_error in error for error in violations))
                    self.assertNotIn(REMINDER, json.dumps(recovery.observations))
                    self.assertIsNone(recovery.request_id)
                    self.assertEqual(journal_snapshot(self.root), before)

    def test_existing_path_tool_system_guards_run_before_recovery_mutation(self):
        for kind in ("path", "tool", "system"):
            with self.subTest(kind=kind):
                recovery = runtime._RecoveryControl(self.root)
                body = first_body(self.root, self.request)
                path = "/v1/chat/completions"
                if kind == "path":
                    path = "/not-the-owned-route"
                elif kind == "tool":
                    body["tools"][0]["function"]["name"] = "unapproved_tool"
                else:
                    body["messages"][0]["content"] += " wrong"
                before = journal_snapshot(self.root)
                with owned_http(self.root, recovery) as (server, _, violations):
                    status, _ = post(server.server_port, body, path)
                    self.assertEqual(status, 400)
                    self.assertTrue(any("unexpected_loopback_POST" in error for error in violations))
                    self.assertEqual(recovery.observations, {})
                    self.assertEqual(journal_snapshot(self.root), before)

    def test_default_c1_http_does_not_perform_recovery_and_product_selects_explicit_control(self):
        before = journal_snapshot(self.root)
        with owned_http(self.root, None) as (server, _, violations):
            status, payload = post(server.server_port, first_body(self.root, self.request))
            self.assertEqual(status, 200)
            self.assertEqual(violations, [])
            self.assertEqual(result_from_sse(self.request, payload)["facts"], FIXED_BODY["facts"])
            self.assertEqual(journal_snapshot(self.root), before)
        for function in (runtime._execute, runtime.observe_owned):
            self.assertIs(inspect.signature(function).parameters["recovery_control"].default, False)
        unavailable = runtime.unavailable("owned_runtime_unavailable")
        with patch("workbench.backend.summary_compat_provider.observe_owned", return_value=unavailable) as observe:
            self.assertEqual(ScriptedCompatibilityProvider().observe(), unavailable)
        observe.assert_called_once_with(ROOT, recovery_control=True, pause_control=True)

    def test_product_partial_recovery_observation_never_admits_or_clears_protection(self):
        owned = self.root / "runtime-owned"
        owned.mkdir(mode=0o700)
        original_launch = subprocess.Popen
        children = []

        def launch(argv, **kwargs):
            # Node executes the real pure helper; only native OMP's boundary is
            # substituted with the owned sleeping Python child.
            if argv[0] != str(Path(sys.executable).resolve()):
                return original_launch(argv, **kwargs)
            child = original_launch([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            request, _ = prepared_request(owned)
            endpoint = json.loads((owned / "fixture.json").read_text())["endpoint"]
            port = int(endpoint.split(":")[2].split("/")[0])
            status, payload = post(port, first_body(owned, request))
            self.assertEqual(status, 200)
            typed = result_from_sse(request, payload)
            written = store_summary(owned / "data", request["requestId"], typed)
            self.assertTrue(written["saved"])
            (owned / "completed.json").write_bytes(canonical_json({"requestId": request["requestId"],
                "outcome": {"status": "summary_result", "result": typed, "writer": written},
                "exercised": ["Q01-route", "Q01-tools", "Q01-result"], "childDisposed": True}))
            return child

        try:
            with patch.object(runtime.subprocess, "Popen", side_effect=launch) as seam:
                observed = runtime._execute(ROOT, Path(sys.executable).resolve(), owned, recovery_control=True)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].returncode)
            self.assertEqual(runtime._remaining(children[0].pid), [])
            row = observed["observations"][0]
            self.assertIsNone(row["error"])
            self.assertTrue(row["cleanup_complete"])
            self.assertFalse(row["server_thread_alive"])
            self.assertEqual(row["http_handler_threads_alive"], 0)
            self.assertEqual(row["http_sockets_remaining"], 0)
            self.assertEqual(row["recovery_control"]["state_order"], ["prepared", "sending", "unknown", "stored"])
            self.assertEqual(row["recovery_control"]["result"], "inconclusive")
            self.assertEqual(row["recovery_control"]["pause_restart"], "not_run")
            self.assertEqual(row["recovery_control"]["prompt_matching_parts"], 1)
            self.assertNotIn(REMINDER, json.dumps(row["recovery_control"]))
            self.assertEqual(observed["level"], "inconclusive")
            self.assertEqual(observed["items"]["Q01-recovery"], "inconclusive")
            self.assertNotIn("Q01-priority", observed["items"])
            for key in ("auth_store_open", "settings_writes", "network_requests"):
                self.assertIsNone(observed["io"][key])
            identity = {"policy_version": 1, "unknowns": [], "policy_sha256": "1" * 64,
                        "closure_digest": "2" * 64, "runtime_digest": "3" * 64,
                        "stable_host": {"os_family": "Linux", "architecture": "x86_64", "python_minor": "3.12"}}
            service = SummaryCompatibility(self.root / "compat", identity=lambda: deepcopy(identity))
            service.protect({"episode": "existing", "reason": "guard_confirmation_unavailable"})
            with patch("workbench.backend.summary_compat_provider.observe_owned", return_value=observed):
                result = service.verify_scripted()
            self.assertEqual(result["result"], "inconclusive")
            self.assertEqual(set(result["items"]), set(Q01_ITEMS))
            self.assertEqual(result["items"]["Q01-priority"], "not_run")
            self.assertEqual(result["items"]["Q01-recovery"], "inconclusive")
            self.assertFalse(service.verify_reference(result["id"]))
            self.assertFalse(service.admission())
            self.assertEqual(service.status()["reason"], "guard_confirmation_unavailable")
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=3)

    def test_zero_or_two_exact_parts_and_trim_or_substring_variants_cannot_mutate_journal(self):
        for kind in ("zero", "two", "trim", "substring"):
            with self.subTest(kind=kind):
                recovery = runtime._RecoveryControl(self.root)
                body = first_body(self.root, self.request)
                parts = body["messages"][1]["content"]
                if kind == "zero":
                    del parts[1]
                elif kind == "two":
                    parts.append(deepcopy(parts[1]))
                elif kind == "trim":
                    parts[1]["text"] = " \n" + parts[1]["text"] + "\n "
                else:
                    parts[1]["text"] = REMINDER + parts[1]["text"]
                before = journal_snapshot(self.root)
                with owned_http(self.root, recovery) as (server, _, violations):
                    status, _ = post(server.server_port, body)
                    self.assertEqual(status, 400)
                    self.assertTrue(any("prompt_envelope_unrecognized" in error for error in violations))
                    self.assertEqual(recovery.observations["prompt_matching_parts"], 2 if kind == "two" else 0)
                    self.assertIsNone(recovery.request_id)
                    self.assertNotIn("unknown_before_response", recovery.observations)
                    self.assertEqual(journal_snapshot(self.root), before)
                    expected_others = [part for part in parts if kind != "two" or part["text"] == REMINDER]
                    self.assertEqual(recovery.observations["sdk_envelope_parts_unattributed"], [
                        {"type": part["type"], "bytes": len(part["text"].encode()),
                         "sha256": sha256(part["text"].encode()).hexdigest()} for part in expected_others])
                    self.assertNotIn(REMINDER, json.dumps(recovery.observations))


if __name__ == "__main__":
    unittest.main()
