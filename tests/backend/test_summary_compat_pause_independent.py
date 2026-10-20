"""Owner-predicate no-send controls; no native OMP or actual pause-store proof."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from http.client import HTTPConnection
import inspect
import json
from pathlib import Path
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
from workbench.backend.summary_compatibility import SummaryCompatibility
from workbench.backend.summary_writer import store_summary
from workbench.contracts.logs_v1 import canonical_json, summary_input_key
from workbench.storage.important_log import ImportantLogCatalog, ImportantLogStore
from workbench.tasks.repository import TaskRepository

ROOT = Path(__file__).resolve().parents[2]
NODE = "/home/dwchoo/.nvm/versions/node/v24.21.0/bin/node"
DETAIL = "owner_predicate_false_confirmed_before_send"
SYSTEM = "Owned independent no-send and late-result fixture."

# The actual dispatch is imported without any native SDK loader. Native classes
# are counted doubles, while catalog preparation is an immediately settled seam.
SDK_SCRIPT = """
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
const { buildSummaryPrompt, dispatchVerificationSummarySDK } = await import(process.argv[1]);
const { canonicalJson } = await import(process.argv[2]);
const input = JSON.parse(readFileSync(0, 'utf8'));
const counters = { predicateCalls: 0, childCreates: 0, promptStarts: 0, registryRefresh: 0,
                   globalReads: 0, settingsCalls: 0 };
const globalState = { registry: ['existing-parent'], settings: { theme: 'original' } };
const before = JSON.stringify(globalState);
class AgentRegistry { static global() { counters.globalReads++; return globalState.registry; } }
const sdk = { AgentRegistry,
  SessionManager: { inMemory() { throw Error('unexpected native session'); } },
  Settings: { isolated() { counters.settingsCalls++; throw Error('unexpected settings'); } },
  createAgentSession: async () => { counters.childCreates++; return { session: {
    prompt: async () => { counters.promptStarts++; } } }; },
  Tokenizer: class {}, fitOutputTokensToContextWindow() {}, toolWireSchema() {},
  getBundledAgent() { throw Error('unexpected bundled lookup after no-send'); } };
const events = [];
const context = { model: input.prepared.metadata.model,
  sessionManager: { getSessionId: () => 'owned-parent' },
  modelRegistry: { awaitInitialBackgroundRefresh: async () => { counters.registryRefresh++; } } };
const outcome = input.promptOnly ? null : await dispatchVerificationSummarySDK({
  role: 'manager', context, prepared: input.prepared, writer: input.writer, sdk,
  observeMetadata: () => input.absentMetadata ? undefined : input.prepared.metadata,
  current: () => { counters.predicateCalls++; return false; }, emit: event => events.push(event) });
counters.promptStarts += events.filter(event => event.event === 'prompt_start').length;
process.stdout.write(JSON.stringify({ outcome, counters,
  globalsUnchanged: JSON.stringify(globalState) === before,
  metadataSha256: createHash('sha256').update(canonicalJson(input.prepared.metadata)).digest('hex'),
  prompt: buildSummaryPrompt(input.prepared) }));
"""


@contextmanager
def owned_server(root, control):
    posts, violations = [], []
    server = runtime._Server(("127.0.0.1", 0), runtime._handler(root, posts, violations, control))
    serve = threading.Thread(target=server.serve_forever, name="independent-pause-http")
    serve.start()
    try:
        yield server, posts, violations
    finally:
        server.shutdown()
        server.shutdown_accepted()
        handlers = tuple(server._threads) if isinstance(server._threads, list) else ()
        server.server_close()
        serve.join(timeout=2)
        assert not serve.is_alive() and all(not handler.is_alive() for handler in handlers)
        assert server.accepted_count() == 0


class PauseControlIndependentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        for name in ("home", "data", "tmp"):
            (self.root / name).mkdir(mode=0o700)
        self.layout = DataLayout(self.root / "data")
        self.catalog = ImportantLogCatalog(self.layout.important_logs, create=True)
        with TaskRepository(self.layout.tasks):
            pass
        self.layout.tasks.chmod(0o600)
        subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        text = b"owned pause source"
        span = {"schemaVersion": 1, "source": {"schemaVersion": 1, "sourceKind": "terminal",
                "collectorEpoch": 13, "commandId": subject["id"]}, "start": 0,
                "end": len(text), "digest": sha256(text).hexdigest()}
        digest = "d" * 64
        self.request = {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject, "revision": None,
                        "authorizationRef": str(uuid4()), "authorizationEpoch": 1,
                        "inputKey": summary_input_key(subject, None, [span], [], digest),
                        "skillName": "important-log-summary", "skillDigest": digest, "sources": [span],
                        "sourceGaps": [], "priorCumulativeEntryId": None,
                        "dbHealth": self.catalog.health_snapshot()["active"], "requestedAt": "2026-10-13T00:00:00Z"}
        self.metadata = {"source": "parent_session_readback", "parentSessionId": "owned-parent",
                         "model": {"id": "scripted", "provider": "ownedverify", "api": "openai-completions",
                                   "baseUrl": "http://127.0.0.1:1/v1", "contextWindow": 131072, "maxTokens": 8192}}
        self.cli_commands = []

    def prepared(self, request):
        return {"request": request, "metadata": deepcopy(self.metadata), "selected": [],
                "omitted": [], "priorCumulative": None, "definitionDigest": "a" * 64}

    def cli(self, mode, request, *, wrong_home=False):
        env = runtime._environment(self.root)
        env["PYTHONPATH"] = str(ROOT / "src")
        if wrong_home:
            env["HOME"] = str(self.root / "wrong-home")
        argv = [sys.executable, "-m", "workbench.backend.summary_compat_runtime", mode, str(self.root)]
        self.cli_commands.append(argv)
        return subprocess.run(argv, input=canonical_json(request), env=env, cwd=self.root,
                              capture_output=True, timeout=10)

    def dispatch(self, request, *, absent_metadata=False, prompt_only=False):
        prepared = self.prepared(request)
        result = subprocess.run([NODE, "--input-type=module", "-e", SDK_SCRIPT,
                                 (ROOT / "omp_bridge/g3/summary_sdk.ts").as_uri(),
                                 (ROOT / "omp_bridge/contract/logs_v1.ts").as_uri()],
                                input=canonical_json({"prepared": prepared, "absentMetadata": absent_metadata,
                                    "promptOnly": prompt_only, "writer": {"python": sys.executable,
                                    "moduleRoot": str(ROOT / "src"), "dataRoot": str(self.layout.root)}}),
                                env=runtime._environment(self.root), cwd=self.root, capture_output=True, timeout=7)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return json.loads(result.stdout)

    def snapshot(self):
        with TaskRepository(self.layout.tasks) as journal:
            return {table: [tuple(row) for row in journal._connection.execute("SELECT * FROM " + table)]
                    for table in ("summary_requests_v1", "summary_request_events_v1",
                                  "summary_result_receipts_v1", "summary_result_drops_v1")}

    def record(self, request):
        with TaskRepository(self.layout.tasks) as journal:
            return journal.get_summary_request(request["requestId"])

    def assert_no_send(self, observed, *, called):
        self.assertEqual(observed["outcome"]["status"], "deferred")
        self.assertEqual(observed["outcome"]["reason"], "model_metadata_unavailable")
        self.assertIs(observed["outcome"]["sent"], False)
        self.assertEqual(observed["counters"]["childCreates"], 0)
        self.assertEqual(observed["counters"]["promptStarts"], 0)
        self.assertEqual(observed["counters"]["registryRefresh"], 1)
        self.assertEqual(observed["counters"]["globalReads"], 0)
        self.assertEqual(observed["counters"]["settingsCalls"], 0)
        self.assertTrue(observed["globalsUnchanged"])
        if called:
            self.assertGreaterEqual(observed["counters"]["predicateCalls"], 1)
        else:
            self.assertEqual(observed["counters"]["predicateCalls"], 0)

    def test_actual_no_send_then_confirm_allows_same_input_reserve_and_one_late_duplicate(self):
        (self.root / "fixture.json").write_bytes(canonical_json({"caseControls": {}}))
        held = self.cli("--hold", self.request)
        self.assertEqual(held.returncode, 0, held.stderr.decode())
        self.assertEqual(self.record(self.request)["state"], "prepared")
        observed = self.dispatch(self.request)
        self.assert_no_send(observed, called=True)
        confirmed = self.cli("--confirm-not-sent", self.request)
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr.decode())
        self.assertEqual(self.record(self.request)["state"], "not_sent")
        self.assertIsNone(self.record(self.request)["receipt"])
        fresh = deepcopy(self.request)
        fresh["requestId"] = str(uuid4())
        reserved = self.cli("--reserve", fresh)
        self.assertEqual(reserved.returncode, 0, reserved.stderr.decode())
        self.assertEqual(self.record(fresh)["state"], "sending")
        for key in ("inputKey", "sources", "sourceGaps"):
            self.assertEqual(self.record(self.request)["request"][key], self.record(fresh)["request"][key])
        positive = self.dispatch(fresh, prompt_only=True)
        self.assertEqual(positive["metadataSha256"], observed["metadataSha256"])
        prompt = positive["prompt"]
        (self.root / "expected.json").write_bytes(canonical_json({"requestId": fresh["requestId"],
            "systemSha": sha256(SYSTEM.encode()).hexdigest(), "promptSha256": sha256(prompt.encode()).hexdigest(),
            "promptBytes": len(prompt.encode())}))
        control = runtime._RecoveryControl(self.root, pause_control=True)
        with owned_server(self.root, control) as (server, posts, violations):
            body = {"model": "worker", "tools": [{"function": {"name": "db_summary_save"}}],
                    "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": [
                        {"type": "text", "text": "unattributed owned reminder"}, {"type": "text", "text": prompt}]}]}
            http = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                http.request("POST", "/v1/chat/completions", canonical_json(body), {"Content-Type": "application/json"})
                response = http.getresponse()
                self.assertEqual(response.status, 200)
                payload = response.read().decode()
            finally:
                http.close()
            self.assertEqual(violations, [])
            self.assertEqual(len(posts), 1)
            self.assertEqual(self.record(fresh)["state"], "unknown")
            frames = [json.loads(line[6:]) for line in payload.splitlines()
                      if line.startswith("data: ") and line != "data: [DONE]"]
            tool = frames[0]["choices"][0]["delta"]["tool_calls"][0]["function"]
            self.assertEqual(tool["name"], "db_summary_save")
            typed = {key: deepcopy(fresh[key]) for key in ("schemaVersion", "requestId", "inputKey", "skillDigest", "sources")}
            typed.update(json.loads(tool["arguments"]))
            typed["usage"] = {"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None}
            written = store_summary(self.layout.root, fresh["requestId"], typed)
            self.assertTrue(written["saved"])
            receipt = self.record(fresh)["receipt"]
            pause = {"control": "owner_predicate_false_control", "requestId": self.request["requestId"],
                     "status": observed["outcome"]["status"], "reason": observed["outcome"]["reason"],
                     "sent": observed["outcome"]["sent"], **{key: observed["counters"][key]
                     for key in ("predicateCalls", "childCreates", "promptStarts")},
                     "noSendConfirmed": confirmed.returncode == 0,
                     "metadataSha256": observed["metadataSha256"],
                     "positiveControlSameMetadata": positive["metadataSha256"] == observed["metadataSha256"]}
            control.after_result({"requestId": fresh["requestId"], "pauseControl": pause,
                                  "outcome": {"result": typed, "writer": written}}, self.catalog)
            self.assertEqual(self.record(fresh)["receipt"], receipt)
            self.assertIs(control.observations["duplicate_receipt_accepted"], False)
            self.assertEqual(control.observations["receipt_count"], 1)
            self.assertEqual(control.observations["duplicate_drop_count"], 1)
            self.assertEqual(control.observations["catalog_entries_after_duplicate"], 1)
            self.assertEqual(control.observations["state_order"], ["prepared", "sending", "unknown", "stored"])
            paused = control.observations["owner_predicate_false_control"]
            self.assertEqual(paused["state_order"], ["prepared", "not_sent"])
            self.assertEqual(paused["detail"], DETAIL)
            self.assertEqual(paused["receipt_count"], 0)
            self.assertEqual(paused["drop_count"], 0)
            self.assertTrue(paused["same_input_and_ranges"])
            for key in ("actualAutomationController", "pause_store", "restart"):
                self.assertEqual(paused[key], "not_run")
            with TaskRepository(self.layout.tasks) as journal:
                actual_history = [tuple(row) for row in journal._connection.execute(
                    "SELECT state,detail FROM summary_request_events_v1 WHERE request_id=? ORDER BY event_id",
                    (self.request["requestId"],))]
                self.assertEqual(actual_history, [("prepared", None), ("not_sent", DETAIL)])
                self.assertEqual(journal._connection.execute("SELECT COUNT(*) FROM summary_result_receipts_v1").fetchone()[0], 1)
                self.assertEqual(journal._connection.execute("SELECT COUNT(*) FROM summary_result_drops_v1").fetchone()[0], 1)
            with ImportantLogStore.open(self.layout.important_logs, self.catalog.active_uuid(), readonly=True) as store:
                self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)
        partial = runtime.unavailable("operating_model_unverified")
        partial.update(level="inconclusive", items={"Q01-recovery": "inconclusive"},
                       observations=[control.observations], io=dict.fromkeys(runtime.IO_KEYS))
        identity = {"policy_version": 1, "unknowns": [], "policy_sha256": "1" * 64, "closure_digest": "2" * 64,
                    "runtime_digest": "3" * 64, "stable_host": {"os_family": "Linux", "architecture": "x86_64", "python_minor": "3.12"}}
        service = SummaryCompatibility(self.root / "compat", identity=lambda: deepcopy(identity))
        service.protect({"episode": "existing", "reason": "guard_confirmation_unavailable"})
        with patch("workbench.backend.summary_compat_provider.observe_owned", return_value=partial):
            result = service.verify_scripted()
        self.assertEqual(result["result"], "inconclusive")
        self.assertEqual(result["items"]["Q01-priority"], "not_run")
        self.assertEqual(result["model_metadata"]["status"], "inconclusive")
        for key in ("auth_store_open", "settings_writes", "network_requests"):
            self.assertIsNone(result["io"][key])
        self.assertFalse(service.verify_reference(result["id"]))
        self.assertFalse(service.admission())
        self.assertEqual(service.status()["reason"], "guard_confirmation_unavailable")

    def test_uncalled_predicate_leaves_prepared_without_confirmation_or_new_send(self):
        held = self.cli("--hold", self.request)
        self.assertEqual(held.returncode, 0, held.stderr.decode())
        before = self.snapshot()
        observed = self.dispatch(self.request, absent_metadata=True)
        self.assert_no_send(observed, called=False)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.record(self.request)["state"], "prepared")
        self.assertFalse(any("--confirm-not-sent" in argv for argv in self.cli_commands))
        fresh = deepcopy(self.request)
        fresh["requestId"] = str(uuid4())
        rejected = self.cli("--reserve", fresh)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIsNone(self.record(fresh))
        self.assertEqual(self.snapshot(), before)

    def test_confirm_requires_exact_request_owned_home_and_no_stdin_state_action_detail(self):
        self.assertEqual(self.cli("--hold", self.request).returncode, 0)
        before = self.snapshot()
        mismatch = deepcopy(self.request)
        mismatch["authorizationEpoch"] += 1
        rejected = self.cli("--confirm-not-sent", mismatch)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(b"pause_confirmation_binding_mismatch", rejected.stderr)
        self.assertEqual(self.snapshot(), before)
        arbitrary = {**self.request, "state": "stored", "action": "confirm", "detail": "untrusted"}
        self.assertNotEqual(self.cli("--confirm-not-sent", arbitrary).returncode, 0)
        self.assertEqual(self.snapshot(), before)
        wrong_home = self.cli("--confirm-not-sent", self.request, wrong_home=True)
        self.assertNotEqual(wrong_home.returncode, 0)
        self.assertIn(b"unsafe_verification_root", wrong_home.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_optional_pause_keyword_defaults_and_product_selection_preserve_old_port(self):
        self.assertIs(inspect.signature(runtime._execute).parameters["pause_control"].default, False)
        self.assertIs(inspect.signature(runtime.observe_owned).parameters["pause_control"].default, False)
        unavailable = runtime.unavailable("owned_runtime_unavailable")
        with patch("workbench.backend.summary_compat_provider.observe_owned", return_value=unavailable) as observe:
            self.assertEqual(ScriptedCompatibilityProvider().observe(), unavailable)
        observe.assert_called_once_with(ROOT, recovery_control=True, pause_control=True, restart_control=True, cases_control=True)
        for pause_control in (False, True):
            with self.subTest(pause_control=pause_control):
                with patch.object(runtime.shutil, "which", return_value=str(Path(sys.executable).resolve())), \
                     patch.object(runtime, "_execute", return_value=unavailable) as execute:
                    self.assertEqual(runtime.observe_owned(ROOT, recovery_control=True, pause_control=pause_control), unavailable)
                self.assertEqual(execute.call_count, 1)
                self.assertEqual(execute.call_args.kwargs,
                                 {"recovery_control": True, **({"pause_control": True} if pause_control else {})})


if __name__ == "__main__":
    unittest.main()
