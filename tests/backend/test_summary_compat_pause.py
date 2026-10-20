"""Private fixed-mode pause control with actual journal/HTTP and pure SDK boundary."""
from copy import deepcopy
from hashlib import sha256
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from uuid import uuid4

from workbench.backend import summary_compat_runtime as runtime
from workbench.backend.paths import DataLayout
from workbench.backend.summary_writer import store_summary
from workbench.contracts.logs_v1 import summary_input_key
from workbench.storage.important_log import ImportantLogCatalog
from workbench.tasks.repository import TaskRepository

ROOT = Path(__file__).resolve().parents[2]


class PauseControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "home").mkdir(mode=0o700)
        (self.root / "data").mkdir(mode=0o700)
        (self.root / "fixture.json").write_text(json.dumps({"caseControls": {}}))
        self.layout = DataLayout(self.root / "data")
        self.catalog = ImportantLogCatalog(self.layout.important_logs, create=True)
        with TaskRepository(self.layout.tasks):
            pass
        self.layout.tasks.chmod(0o600)
        subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        span = {"schemaVersion": 1, "source": {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
            "commandId": subject["id"]}, "start": 0, "end": 4, "digest": sha256(b"work").hexdigest()}
        self.request = {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject, "revision": None,
            "authorizationRef": str(uuid4()), "authorizationEpoch": 1,
            "inputKey": summary_input_key(subject, None, [span], [], "a" * 64), "skillName": "important-log-summary",
            "skillDigest": "a" * 64, "sources": [span], "sourceGaps": [], "priorCumulativeEntryId": None,
            "dbHealth": self.catalog.health_snapshot()["active"], "requestedAt": "2026-10-13T00:00:00Z"}
        self.environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(self.root / "home"),
                            "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}

    def mode(self, mode, request=None):
        return subprocess.run([sys.executable, "-m", "workbench.backend.summary_compat_runtime", mode, str(self.root)],
            input=json.dumps(request or self.request).encode(), capture_output=True, env=self.environment, timeout=8)

    def record(self, request=None):
        with TaskRepository(self.layout.tasks) as journal:
            return journal.get_summary_request((request or self.request)["requestId"])

    def sdk_no_send(self, *, metadata_available=True):
        # Run the actual pure dispatch seam with no native SDK/provider. The
        # owner check must return before child construction or prompt dispatch.
        fixture = self.root / "request.json"
        fixture.write_text(json.dumps(self.request))
        code = """
import { readFileSync } from 'node:fs';
import { dispatchVerificationSummarySDK } from '__SDK__';
const request=JSON.parse(readFileSync(process.argv[1],'utf8'));
const model={provider:'ownedverify',id:'scripted',api:'openai-completions',baseUrl:'http://127.0.0.1:1/v1',contextWindow:131072,maxTokens:8192};
const metadata={source:'parent_session_readback',parentSessionId:'private-parent',epoch:'same-control',model,thinkingLevel:'off',serviceTierByFamily:{},existingContextTokens:0,outputCap:8192,reasoningTokens:0,visibleResultTokens:8192,outputMeaning:'reasoning_and_visible',outputLimitEnforced:true,outputLimitSource:'test-scripted-only',estimator:'utf8_bytes_upper_bound'};
let predicateCalls=0,childCreates=0,promptStarts=0;
const observed=process.argv[2]==='true'?metadata:undefined;
const outcome=await dispatchVerificationSummarySDK({role:'manager',context:{model,sessionManager:{getSessionId:()=>metadata.parentSessionId},modelRegistry:{awaitInitialBackgroundRefresh:async()=>{}}},prepared:{request,metadata,selected:[],omitted:[],priorCumulative:null,definitionDigest:'b'.repeat(64)},writer:{},sdk:{async createAgentSession(){childCreates++;throw Error('unexpected child');}},current:()=>{predicateCalls++;return false;},observeMetadata:()=>observed,emit:event=>{if(event.event==='prompt_start')promptStarts++;}});
process.stdout.write(JSON.stringify({outcome,predicateCalls,childCreates,promptStarts}));
""".replace("__SDK__", (ROOT / "omp_bridge/g3/summary_sdk.ts").as_uri())
        node = Path("/home/dwchoo/.nvm/versions/node/v24.21.0/bin/node")
        self.assertTrue(node.is_file() and os.access(node, os.X_OK), "installed Node24 executable is required")
        completed = subprocess.run([str(node), "--input-type=module", "-e", code, str(fixture), str(metadata_available).lower()],
                                  capture_output=True, text=True, env=self.environment, timeout=8)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_hold_no_send_confirm_then_fresh_recovery_keeps_paused_request_unstored(self):
        self.assertEqual(self.mode("--hold").returncode, 0)
        self.assertEqual(self.record()["state"], "prepared")
        self.assertIsNone(self.record()["receipt"])
        no_send = self.sdk_no_send()
        self.assertGreaterEqual(no_send["predicateCalls"], 1)
        self.assertEqual(no_send["outcome"], {"status": "deferred", "sent": False, "protected": False, "reason": "model_metadata_unavailable"})
        self.assertEqual((no_send["childCreates"], no_send["promptStarts"]), (0, 0))
        self.assertEqual(self.mode("--confirm-not-sent").returncode, 0)
        self.assertEqual(self.record()["state"], "not_sent")
        fresh = deepcopy(self.request)
        fresh["requestId"] = str(uuid4())
        self.assertEqual(self.mode("--reserve", fresh).returncode, 0)
        self.assertEqual(self.record(fresh)["state"], "sending")
        prompt = json.dumps({"request": fresh, "selected": [], "omitted": [], "priorCumulative": None,
                             "resultByteLimit": 8192, "instruction": "Use db_summary_save exactly once, then stop."})
        system = "fixed owned pause recovery test"
        (self.root / "expected.json").write_text(json.dumps({"requestId": fresh["requestId"],
            "systemSha": sha256(system.encode()).hexdigest(), "promptSha256": sha256(prompt.encode()).hexdigest(), "promptBytes": len(prompt.encode())}))
        posts, violations = [], []
        control = runtime._RecoveryControl(self.root, pause_control=True)
        server = runtime._Server(("127.0.0.1", 0), runtime._handler(self.root, posts, violations, control))
        serve = threading.Thread(target=server.serve_forever)
        serve.start()
        try:
            for index in range(2):
                messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
                if index:
                    messages.append({"role": "tool", "content": "received", "tool_call_id": "owned-save-1"})
                body = {"model": "worker", "tools": [{"type": "function", "function": {"name": "db_summary_save"}}], "messages": messages}
                client = HTTPConnection(*server.server_address, timeout=2)
                try:
                    client.request("POST", "/v1/chat/completions", json.dumps(body).encode())
                    response = client.getresponse()
                    payload = response.read()
                    self.assertEqual(response.status, 200)
                    if not index:
                        frame = json.loads(next(line[6:] for line in payload.decode().splitlines() if line.startswith("data: {")))
                        params = json.loads(frame["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"])
                        self.assertEqual(self.record(fresh)["state"], "unknown")
                finally:
                    client.close()
            result = {name: deepcopy(fresh[name]) for name in ("schemaVersion", "requestId", "inputKey", "skillDigest", "sources")}
            result.update(params)
            result["usage"] = {"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None}
            written = store_summary(self.root / "data", fresh["requestId"], result)
            self.assertTrue(written["saved"])
            pause = {"control": "owner_predicate_false_control", "requestId": self.request["requestId"],
                "status": no_send["outcome"]["status"], "reason": no_send["outcome"]["reason"], "sent": False,
                "predicateCalls": no_send["predicateCalls"], "childCreates": 0, "promptStarts": 0,
                "noSendConfirmed": True, "positiveControlSameMetadata": True}
            control.after_result({"requestId": fresh["requestId"], "pauseControl": pause,
                                  "outcome": {"writer": written, "result": result}}, self.catalog)
            observed = control.observations["owner_predicate_false_control"]
            self.assertEqual(observed["state_order"], ["prepared", "not_sent"])
            self.assertEqual(observed["detail"], "owner_predicate_false_confirmed_before_send")
            self.assertEqual((observed["receipt_count"], observed["drop_count"]), (0, 0))
            self.assertTrue(observed["same_input_and_ranges"])
            self.assertEqual(control.observations["state_order"], ["prepared", "sending", "unknown", "stored"])
            self.assertEqual((control.observations["receipt_count"], control.observations["duplicate_drop_count"], control.observations["catalog_entries_after_duplicate"]), (1, 1, 1))
            self.assertEqual(len(posts), 2)
            self.assertEqual(violations, [])
            print("pause_control_observations=" + json.dumps(control.observations))
        finally:
            server.shutdown()
            server.shutdown_accepted()
            handlers = tuple(server._threads)
            server.server_close()
            serve.join(timeout=1)
            self.assertFalse(serve.is_alive())
            self.assertFalse(any(thread.is_alive() for thread in handlers))
            self.assertEqual(server.accepted_count(), 0)

    def test_confirmation_requires_same_strict_prepared_request(self):
        self.assertEqual(self.mode("--hold").returncode, 0)
        wrong = deepcopy(self.request)
        wrong["authorizationEpoch"] += 1
        self.assertNotEqual(self.mode("--confirm-not-sent", wrong).returncode, 0)
        injected = {**self.request, "state": "not_sent", "detail": "arbitrary"}
        self.assertNotEqual(self.mode("--confirm-not-sent", injected).returncode, 0)
        self.assertEqual(self.record()["state"], "prepared")
        self.assertIsNone(self.record()["receipt"])
        self.assertNotEqual(self.mode("--arbitrary-action").returncode, 0)
        with TaskRepository(self.layout.tasks) as journal:
            self.assertEqual(journal._connection.execute("SELECT COUNT(*) FROM summary_request_events_v1").fetchone()[0], 1)

    def test_confirmation_rejects_request_already_sending(self):
        self.assertEqual(self.mode("--reserve").returncode, 0)
        self.assertNotEqual(self.mode("--confirm-not-sent").returncode, 0)
        self.assertEqual(self.record()["state"], "sending")

    def test_uncalled_owner_predicate_leaves_hold_prepared_without_confirmation(self):
        self.assertEqual(self.mode("--hold").returncode, 0)
        no_send = self.sdk_no_send(metadata_available=False)
        self.assertEqual(no_send["predicateCalls"], 0)
        self.assertFalse(no_send["outcome"]["sent"])
        self.assertEqual((no_send["childCreates"], no_send["promptStarts"]), (0, 0))
        self.assertEqual(self.record()["state"], "prepared")
        self.assertIsNone(self.record()["receipt"])


if __name__ == "__main__":
    unittest.main()
