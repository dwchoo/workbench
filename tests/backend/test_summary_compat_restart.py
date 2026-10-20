"""Real private HTTP/Event/SQLite plus owned non-OMP process identity checks."""
from copy import deepcopy
from hashlib import sha256
from http.client import HTTPConnection
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from workbench.backend import summary_compat_runtime as runtime
from workbench.backend.paths import DataLayout
from workbench.backend.summary_writer import store_summary
from workbench.contracts.logs_v1 import summary_input_key
from workbench.storage.important_log import ImportantLogCatalog
from workbench.tasks.repository import TaskRepository


class RestartControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "fixture.json").write_text(json.dumps({"caseControls": {}}))
        (self.root / "home").mkdir(mode=0o700)
        (self.root / "data").mkdir(mode=0o700)
        self.layout = DataLayout(self.root / "data")
        self.catalog = ImportantLogCatalog(self.layout.important_logs, create=True)
        with TaskRepository(self.layout.tasks):
            pass
        self.layout.tasks.chmod(0o600)
        self.text = "소유 private 환경에서 합성 입력의 C1 연결을 확인한다."
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        self.original = self.request(runtime.DISCONNECT_REQUEST_ID, self.text, 0)
        with TaskRepository(self.layout.tasks) as journal:
            journal.reserve_summary_request(self.original)
            journal.record_summary_state(self.original["requestId"], "sending")
        self.system = "owned restart test"
        self.posts, self.violations = [], []
        self.restart = runtime._RestartControl(self.root, time.monotonic() + 10)
        self.recovery = runtime._RecoveryControl(self.root)
        self.server = runtime._Server(("127.0.0.1", 0), runtime._handler(self.root, self.posts, self.violations, self.recovery, self.restart))
        self.serve = threading.Thread(target=self.server.serve_forever)
        self.serve.start()
        self.addCleanup(self.stop_server)
        self.process = None
        self.ref = None
        self.client = None
        self.client_thread = None
        self.response = {}
        self.addCleanup(self.stop_owned)

    def request(self, request_id, text, start):
        span = {"schemaVersion": 1, "source": {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
            "commandId": self.subject["id"]}, "start": start, "end": start + len(text.encode()), "digest": sha256(text.encode()).hexdigest()}
        return {"schemaVersion": 1, "requestId": request_id, "subject": self.subject, "revision": None,
            "authorizationRef": str(uuid4()), "authorizationEpoch": 1,
            "inputKey": summary_input_key(self.subject, None, [span], [], "a" * 64), "skillName": "important-log-summary",
            "skillDigest": "a" * 64, "sources": [span], "sourceGaps": [], "priorCumulativeEntryId": None,
            "dbHealth": self.catalog.health_snapshot()["active"], "requestedAt": "2026-10-13T00:00:00Z"}

    def body(self, request, text, *, continuation=False):
        prompt = json.dumps({"request": request, "selected": [{"range": request["sources"][0], "text": text}],
            "omitted": [], "priorCumulative": None, "resultByteLimit": 8192, "instruction": "Use db_summary_save exactly once, then stop."})
        (self.root / "expected.json").write_text(json.dumps({"requestId": request["requestId"],
            "systemSha": sha256(self.system.encode()).hexdigest(), "promptSha256": sha256(prompt.encode()).hexdigest(), "promptBytes": len(prompt.encode())}))
        messages = [{"role": "system", "content": self.system}, {"role": "user", "content": prompt}]
        if continuation:
            messages.append({"role": "tool", "content": "received", "tool_call_id": "owned-save-1"})
        return {"model": "worker", "tools": [{"type": "function", "function": {"name": "db_summary_save"}}], "messages": messages}

    def start_hold(self):
        self.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(self.root / "home"), "PYTHONDONTWRITEBYTECODE": "1"})
        self.ref = {"pid": self.process.pid, "pgid": self.process.pid, "start_ticks": runtime._identity(self.process.pid)}
        body = json.dumps(self.body(self.original, self.text)).encode()
        self.client = socket.create_connection(self.server.server_address, timeout=2)
        self.client.settimeout(4)
        self.client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        def receive():
            try:
                self.response["bytes"] = self.client.recv(4096)
            except Exception as error:
                self.response["error"] = type(error).__name__
        self.client_thread = threading.Thread(target=receive)
        self.client_thread.start()
        self.assertTrue(self.restart.post_seen.wait(timeout=2))

    def stop_owned(self):
        self.restart.release.set()
        if self.process is not None:
            runtime._stop_leader(self.process, self.ref)
        if self.client_thread is not None:
            self.client_thread.join(timeout=2)
            self.assertFalse(self.client_thread.is_alive())
        if self.client is not None:
            self.client.close()
        if self.ref is not None:
            self.assertEqual(runtime._remaining(self.ref["pgid"]), [])

    def stop_server(self):
        self.restart.release.set()
        self.server.shutdown()
        self.server.shutdown_accepted()
        handlers = tuple(self.server._threads) if isinstance(self.server._threads, list) else ()
        self.server.server_close()
        self.serve.join(timeout=1)
        self.assertFalse(self.serve.is_alive())
        self.assertFalse(any(thread.is_alive() for thread in handlers))
        self.assertEqual(self.server.accepted_count(), 0)

    def test_validated_response_free_post_kill_reopen_refusal_then_adjacent_late_result(self):
        self.start_hold()
        with TaskRepository(self.layout.tasks) as journal:
            self.assertEqual(journal.get_summary_request(self.original["requestId"])["state"], "sending")
        self.assertTrue(runtime._signal_leader(self.process, self.ref, signal.SIGKILL))
        self.process.wait(timeout=2)
        self.restart.release.set()
        self.restart.confirm_unknown(self.process, self.ref, self.catalog)
        self.client_thread.join(timeout=2)
        self.assertEqual(self.response, {"bytes": b""})
        self.assertEqual(self.process.returncode, -signal.SIGKILL)
        self.assertEqual(runtime._remaining(self.ref["pgid"]), [])
        self.assertTrue(self.restart.observations["reopened_unknown"])
        self.assertTrue(self.restart.observations["replay_reserve_refused"])
        self.assertFalse(self.restart.observations["replay_row_present"])
        fresh_text = self.text.replace("C1", "C3")
        self.assertEqual(len(fresh_text.encode()), len(self.text.encode()))
        fresh = self.request(runtime.RESTART_REQUEST_ID, fresh_text, len(self.text.encode()))
        with TaskRepository(self.layout.tasks) as journal:
            journal.reserve_summary_request(fresh)
            journal.record_summary_state(fresh["requestId"], "sending")
        self.restart.phase = "restart"
        for index in range(2):
            client = HTTPConnection(*self.server.server_address, timeout=2)
            try:
                client.request("POST", "/v1/chat/completions", json.dumps(self.body(fresh, fresh_text, continuation=bool(index))).encode())
                response = client.getresponse()
                payload = response.read()
                self.assertEqual(response.status, 200)
                if not index:
                    frame = json.loads(next(line[6:] for line in payload.decode().splitlines() if line.startswith("data: {")))
                    params = json.loads(frame["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"])
            finally:
                client.close()
        result = {name: deepcopy(fresh[name]) for name in ("schemaVersion", "requestId", "inputKey", "skillDigest", "sources")}
        result.update(params)
        result["usage"] = {"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None}
        written = store_summary(self.root / "data", fresh["requestId"], result)
        self.assertTrue(written["saved"])
        completed = {"requestId": fresh["requestId"], "outcome": {"writer": written, "result": result}}
        self.recovery.after_result(completed, self.catalog)
        self.restart.after_result(completed)
        facts = self.restart.observations
        self.assertEqual(facts["original_state_order"], ["prepared", "sending", "unknown"])
        self.assertEqual((facts["original_receipt_count"], facts["original_drop_count"]), (0, 0))
        self.assertTrue(facts["fresh_adjacent_distinct_input"])
        self.assertEqual(facts["phase_post_counts"], {"disconnect": 1, "between": 0, "restart": 2})
        self.assertEqual(facts["original_postrestart_posts"], 0)
        self.assertEqual(len(self.posts), 3)
        self.assertEqual(self.violations, [])
        self.assertEqual((self.recovery.observations["receipt_count"], self.recovery.observations["duplicate_drop_count"], self.recovery.observations["catalog_entries_after_duplicate"]), (1, 1, 1))
        print("restart_control_observations=" + json.dumps(facts))

    def test_identity_mismatch_releases_hold_without_unknown_or_replay(self):
        self.start_hold()
        wrong = {**self.ref, "start_ticks": "not-owned"}
        self.assertFalse(runtime._signal_leader(self.process, wrong, signal.SIGKILL))
        self.assertIsNone(self.process.poll())
        self.restart.release.set()
        self.assertTrue(self.restart.handler_done.wait(timeout=2))
        self.client_thread.join(timeout=2)
        self.assertEqual(self.response, {"bytes": b""})
        with TaskRepository(self.layout.tasks) as journal:
            record = journal.get_summary_request(self.original["requestId"])
            self.assertEqual(record["state"], "sending")
            self.assertIsNone(record["receipt"])
            self.assertEqual(journal._connection.execute("SELECT COUNT(*) FROM summary_requests_v1").fetchone()[0], 1)
        self.assertNotIn("reopened_unknown", self.restart.observations)

    def test_between_phase_post_refused_before_any_journal_mutation(self):
        self.restart.phase = "between"
        client = HTTPConnection(*self.server.server_address, timeout=2)
        try:
            client.request("POST", "/v1/chat/completions", json.dumps(self.body(self.original, self.text)).encode())
            response = client.getresponse()
            response.read()
            self.assertEqual(response.status, 400)
        finally:
            client.close()
        with TaskRepository(self.layout.tasks) as journal:
            self.assertEqual(journal.get_summary_request(self.original["requestId"])["state"], "sending")
        self.assertFalse(self.restart.post_seen.is_set())
        self.assertIn("between_phase_POST_refused", self.violations[0])


if __name__ == "__main__":
    unittest.main()
