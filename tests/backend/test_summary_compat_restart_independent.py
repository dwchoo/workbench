"""Real owned Python/HTTP/SQLite restart fixtures, never native OMP proof."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from http.client import HTTPConnection, RemoteDisconnected
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.backend import summary_compat_runtime as runtime
from workbench.backend.paths import DataLayout
from workbench.contracts.logs_v1 import canonical_json, summary_input_key
from workbench.storage.important_log import ImportantLogCatalog
from workbench.tasks.repository import InvalidTransitionError, TaskRepository
from tests.backend.test_summary_compat_pause_independent import SDK_SCRIPT

ROOT = Path(__file__).resolve().parents[2]
NODE = "/home/dwchoo/.nvm/versions/node/v24.21.0/bin/node"
SYSTEM = "Owned independent restart fixture."

# Executed only at the external OMP launch seam. It imports the copied product
# runtime, uses actual fixed CLI/SDK no-send helpers and talks to the real server.
# HTTP is a fixture provider invocation, not an installed SDK/model request.
CHILD = r'''
import json, os, sys, threading, subprocess
from pathlib import Path
from hashlib import sha256
from http.client import HTTPConnection
from uuid import uuid4
root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'runtime/src'))
from workbench.backend.paths import DataLayout, write_private_json
from workbench.contracts.logs_v1 import canonical_json, summary_input_key
from workbench.tasks.repository import TaskRepository
from workbench.backend.summary_writer import store_summary
fixture = json.loads((root / 'fixture.json').read_text())
phase = fixture['phase']
original = b'owned C1 fixture source'
raw = original.replace(b'C1', b'C3') if phase == 'restart' else original
offset = len(original) if phase == 'restart' else 0
subject = {'schemaVersion': 1, 'kind': 'command', 'id': 'b4ef1d01-26aa-424f-a259-2283b77e3891'}
span = {'schemaVersion': 1, 'source': {'schemaVersion': 1, 'sourceKind': 'terminal',
    'collectorEpoch': 19, 'commandId': subject['id']}, 'start': offset, 'end': offset + len(raw),
    'digest': sha256(raw).hexdigest()}
skill = 'e' * 64
request = {'schemaVersion': 1, 'requestId': 'a40f9a18-a0fc-4d75-9260-7f83cb06622d' if phase == 'restart'
    else 'fc8416c2-7d8c-46d4-9615-b833b567f9fe', 'subject': subject, 'revision': None,
    'authorizationRef': '4968ce03-fbf6-4b44-8930-5febea0b47b5', 'authorizationEpoch': 1,
    'inputKey': summary_input_key(subject, None, [span], [], skill), 'skillName': 'important-log-summary',
    'skillDigest': skill, 'sources': [span], 'sourceGaps': [], 'priorCumulativeEntryId': None,
    'dbHealth': fixture['dbHealth'], 'requestedAt': '2026-10-13T00:00:00Z'}
metadata = {'source': 'parent_session_readback', 'parentSessionId': 'owned-parent', 'model': {
    'id': 'scripted', 'provider': 'ownedverify', 'api': 'openai-completions', 'baseUrl': fixture['endpoint'],
    'contextWindow': 131072, 'maxTokens': 8192}}
prepared = {'request': request, 'metadata': metadata, 'selected': [{'range': span, 'text': raw.decode()}],
    'omitted': [], 'priorCumulative': None, 'definitionDigest': 'a' * 64}
env = dict(os.environ, PYTHONPATH=str(root / 'runtime/src'))
def journal(mode, item):
    run = subprocess.run([sys.executable, '-m', 'workbench.backend.summary_compat_runtime', mode, str(root)],
        input=canonical_json(item), env=env, capture_output=True, timeout=5)
    assert run.returncode == 0, run.stderr.decode()
def sdk(item, prompt_only):
    run = subprocess.run([sys.argv[3], '--input-type=module', '-e', sys.argv[2],
        (root / 'runtime/omp_bridge/g3/summary_sdk.ts').as_uri(),
        (root / 'runtime/omp_bridge/contract/logs_v1.ts').as_uri()],
        input=canonical_json({'prepared': item, 'promptOnly': prompt_only, 'absentMetadata': False,
            'writer': {'python': sys.executable, 'moduleRoot': str(root / 'runtime/src'), 'dataRoot': str(root / 'data')}}),
        env=env, capture_output=True, timeout=7)
    assert run.returncode == 0, run.stderr.decode()
    return json.loads(run.stdout)
pause = None
if phase == 'restart':
    paused = dict(request, requestId=str(uuid4()))
    paused_prepared = dict(prepared, request=paused)
    journal('--hold', paused)
    observed = sdk(paused_prepared, False)
    assert observed['outcome']['status'] == 'deferred' and observed['outcome']['sent'] is False
    assert observed['counters']['predicateCalls'] >= 1
    assert observed['counters']['childCreates'] == observed['counters']['promptStarts'] == 0
    journal('--confirm-not-sent', paused)
    pause = {'control': 'owner_predicate_false_control', 'requestId': paused['requestId'],
        'status': observed['outcome']['status'], 'sent': observed['outcome']['sent'],
        **{key: observed['counters'][key] for key in ('predicateCalls','childCreates','promptStarts')},
        'noSendConfirmed': True, 'metadataSha256': observed['metadataSha256'],
        'positiveControlSameMetadata': paused_prepared['metadata'] == prepared['metadata']}
journal('--reserve', request)
prompt = sdk(prepared, True)['prompt']
write_private_json(root / 'expected.json', {'requestId': request['requestId'],
    'systemSha': sha256(b'Owned independent restart fixture.').hexdigest(),
    'promptSha256': sha256(prompt.encode()).hexdigest(), 'promptBytes': len(prompt.encode())})
body = {'model': 'scripted', 'tools': [{'function': {'name': 'db_summary_save'}}], 'messages': [
    {'role': 'system', 'content': 'Owned independent restart fixture.'}, {'role': 'user', 'content': [
        {'type': 'text', 'text': 'unattributed fixture reminder'}, {'type': 'text', 'text': prompt}]}]}
print(json.dumps({'fixture_phase': phase, 'source_bytes': len(raw), 'source_sha256': sha256(raw).hexdigest(),
    'input_key': request['inputKey']}), flush=True)
port = int(fixture['endpoint'].split(':')[2].split('/')[0])
def post(value):
    conn = HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        conn.request('POST', '/v1/chat/completions', canonical_json(value), {'Content-Type': 'application/json'})
        response = conn.getresponse()
        assert response.status == 200
        return response.read().decode()
    finally:
        conn.close()
payload = post(body)  # phase1 is held here and receives identity-checked SIGKILL.
assert phase == 'restart', 'disconnect response must never complete'
frames = [json.loads(line[6:]) for line in payload.splitlines() if line.startswith('data: ') and line != 'data: [DONE]']
tool = frames[0]['choices'][0]['delta']['tool_calls'][0]['function']
assert tool['name'] == 'db_summary_save'
typed = {key: request[key] for key in ('schemaVersion','requestId','inputKey','skillDigest','sources')}
typed.update(json.loads(tool['arguments']))
typed['usage'] = {'schemaVersion': 1, 'inputTokens': None, 'outputTokens': None, 'costUsd': None}
written = store_summary(root / 'data', request['requestId'], typed)
assert written['saved'] is True
continuation = json.loads(json.dumps(body))
continuation['messages'].append({'role': 'tool', 'content': 'bound receipt stored', 'tool_call_id': 'owned-save-1'})
post(continuation)
write_private_json(root / 'completed.json', {'requestId': request['requestId'], 'pauseControl': pause,
    'ownedPhase': phase, 'fixture_python_not_native_sdk': True,
    'outcome': {'status': 'summary_result', 'result': typed, 'writer': written},
    'exercised': ['Q01-route','Q01-tools','Q01-result'], 'childDisposed': True})
threading.Event().wait()  # Parent's real owned cleanup terminates this fixture process.
'''


@contextmanager
def server_for(root, control):
    posts, violations = [], []
    server = runtime._Server(("127.0.0.1", 0), runtime._handler(root, posts, violations, restart=control))
    serve = threading.Thread(target=server.serve_forever, name="independent-restart-http")
    serve.start()
    try:
        yield server, violations
    finally:
        control.release.set()
        server.shutdown()
        server.shutdown_accepted()
        handlers = tuple(server._threads) if isinstance(server._threads, list) else ()
        server.server_close()
        serve.join(timeout=2)
        assert not serve.is_alive() and all(not handler.is_alive() for handler in handlers)
        assert server.accepted_count() == 0


class RestartControlIndependentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_two_real_owned_python_phases_share_budget_and_reopen_before_replacement(self):
        original_popen, original_control = subprocess.Popen, runtime._RestartControl
        children, controls, launches = [], [], []
        def control(root, deadline):
            value = original_control(root, deadline)
            controls.append(value)
            return value
        def launch(argv, **kwargs):
            fixture = json.loads((self.root / "fixture.json").read_text())
            phase = fixture["phase"]
            config = {name: sha256((self.root / name).read_bytes()).hexdigest()
                      for name in ("agent/models.yml", "overlay.yml")}
            if phase == "restart":
                with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
                    old = journal.get_summary_request(runtime.DISCONNECT_REQUEST_ID)
                    self.assertEqual(old["state"], "unknown")
                    self.assertIsNone(old["receipt"])
                    self.assertTrue(any(row["request"]["requestId"] == runtime.DISCONNECT_REQUEST_ID
                                        for row in journal.list_unresolved_summary_requests()))
                    replay = deepcopy(old["request"])
                    replay["requestId"] = str(uuid4())
                    with self.assertRaises(InvalidTransitionError):
                        journal.reserve_summary_request(replay)
                    self.assertIsNone(journal.get_summary_request(replay["requestId"]))
                    self.assertIsNone(journal.get_summary_request(controls[0].replay_id))
                self.assertEqual(children[0].returncode, -signal.SIGKILL)
                self.assertEqual(runtime._remaining(children[0].pid), [])
            launches.append({"phase": phase, "config": config, "cwd": str(kwargs["cwd"]),
                             "env": kwargs["env"], "deadline": controls[0].deadline})
            child = original_popen([sys.executable, "-c", CHILD, str(self.root), SDK_SCRIPT, NODE], **kwargs)
            children.append(child)
            return child
        before = time.monotonic()
        try:
            with patch.object(runtime.subprocess, "Popen", side_effect=launch), \
                 patch.object(runtime, "_RestartControl", side_effect=control):
                result = runtime._execute(ROOT, Path(sys.executable).resolve(), self.root,
                    recovery_control=True, pause_control=True, restart_control=True)
            row = result["observations"][0]
            self.assertIsNone(row["error"])
            self.assertEqual(len(children), 2)
            self.assertEqual(len(controls), 1)
            self.assertGreaterEqual(controls[0].deadline - before, 90)
            self.assertLess(controls[0].deadline - before, 90.5)
            self.assertEqual([item["phase"] for item in launches], ["disconnect", "restart"])
            self.assertEqual(launches[0]["config"], launches[1]["config"])
            self.assertEqual(launches[0]["cwd"], launches[1]["cwd"])
            self.assertEqual(launches[0]["env"], launches[1]["env"])
            self.assertEqual(launches[0]["deadline"], launches[1]["deadline"])
            self.assertTrue(controls[0].handler_done.is_set())
            self.assertTrue(controls[0].release.is_set())
            self.assertTrue(row["cleanup_complete"])
            self.assertFalse(row["server_thread_alive"])
            self.assertEqual(row["http_handler_threads_alive"], 0)
            self.assertEqual(row["http_sockets_remaining"], 0)
            self.assertEqual([item["remaining_pgid_members"] for item in row["remaining_by_phase"]], [[], []])
            self.assertEqual([item["phase"] for item in row["owned_processes"]], ["disconnect", "restart"])
            for child in children:
                self.assertIsNotNone(child.returncode)
                self.assertEqual(runtime._remaining(child.pid), [])
            restart = row["restart_control"]
            self.assertEqual(restart["response_bytes"], 0)
            self.assertEqual(restart["state_before_unknown"], "sending")
            self.assertEqual(restart["catalog_before_unknown"], 0)
            self.assertEqual(restart["receipt_before_unknown"], 0)
            self.assertEqual(restart["drops_before_unknown"], 0)
            self.assertTrue(restart["completed_artifact_absent"])
            self.assertEqual(restart["phase_post_counts"], {"disconnect": 1, "between": 0, "restart": 2})
            self.assertEqual(restart["original_postrestart_posts"], 0)
            self.assertEqual(restart["original_state_order"], ["prepared", "sending", "unknown"])
            self.assertEqual(restart["child_disposal"], "not_applicable_process_killed")
            self.assertEqual(restart["post_OMP_restart_late"], "not_applicable_child_died_in_process")
            self.assertEqual(restart["actualBackend"], "not_run")
            self.assertEqual(restart["scheduler_restart"], "not_run")
            with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
                original = journal.get_summary_request(runtime.DISCONNECT_REQUEST_ID)
                fresh = journal.get_summary_request(runtime.RESTART_REQUEST_ID)
                self.assertEqual(original["state"], "unknown")
                self.assertIsNone(original["receipt"])
                self.assertEqual(fresh["state"], "stored")
                a, b = original["request"], fresh["request"]
                self.assertEqual(a["subject"], b["subject"])
                self.assertEqual(a["sources"][0]["end"], b["sources"][0]["start"])
                self.assertEqual(a["sources"][0]["end"]-a["sources"][0]["start"],
                                 b["sources"][0]["end"]-b["sources"][0]["start"])
                self.assertNotEqual(a["inputKey"], b["inputKey"])
                self.assertEqual(a["sources"][0]["digest"], sha256(b"owned C1 fixture source").hexdigest())
                self.assertEqual(b["sources"][0]["digest"], sha256(b"owned C3 fixture source").hexdigest())
            recovery = row["recovery_control"]
            self.assertIs(recovery["duplicate_receipt_accepted"], False)
            self.assertEqual((recovery["duplicate_drop_count"], recovery["receipt_count"], recovery["catalog_entries_after_duplicate"]), (1, 1, 1))
            self.assertTrue(recovery["receipt_unchanged"])
            self.assertEqual(recovery["owner_predicate_false_control"]["state_order"], ["prepared", "not_sent"])
            self.assertEqual(recovery["owner_predicate_false_control"]["predicateCalls"], 1)
            self.assertEqual(recovery["owner_predicate_false_control"]["childCreates"], 0)
            self.assertEqual(recovery["owner_predicate_false_control"]["promptStarts"], 0)
            for name, artifact in restart["phase1_artifacts"].items():
                data = (self.root / artifact["path"]).read_bytes()
                self.assertEqual(len(data), artifact["bytes"])
                self.assertEqual(sha256(data).hexdigest(), artifact["sha256"])
            self.assertEqual(restart["phase1_artifacts"]["expected.json"]["facts"]["requestId"], runtime.DISCONNECT_REQUEST_ID)
            self.assertEqual(restart["phase1_artifacts"]["fixture.json"]["facts"]["phase"], "disconnect")
            self.assertEqual(json.loads((self.root / "fixture.json").read_text())["phase"], "restart")
            self.assertEqual([post["expected_request_id"] for post in row["localhost_model_posts"]],
                             [runtime.DISCONNECT_REQUEST_ID, runtime.RESTART_REQUEST_ID, runtime.RESTART_REQUEST_ID])
            self.assertEqual(result["level"], "inconclusive")
            for key in ("auth_store_open", "settings_writes", "network_requests"):
                self.assertIsNone(result["io"][key])
            print("restart_fixture_observation=" + json.dumps({"fixture": "ordinary Python, not native OMP",
                "process_refs": row["owned_processes"], "phase_counts": restart["phase_post_counts"],
                "phase1_artifacts": restart["phase1_artifacts"], "cleanup_complete": row["cleanup_complete"]}))
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=3)

    def simple_request(self):
        (self.root / "data").mkdir(mode=0o700)
        catalog = ImportantLogCatalog(DataLayout(self.root / "data").important_logs, create=True)
        subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        span = {"schemaVersion": 1, "source": {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                "commandId": subject["id"]}, "start": 0, "end": 2, "digest": sha256(b"C1").hexdigest()}
        request = {"schemaVersion": 1, "requestId": runtime.DISCONNECT_REQUEST_ID, "subject": subject, "revision": None,
            "authorizationRef": str(uuid4()), "authorizationEpoch": 1, "inputKey": summary_input_key(subject, None, [span], [], "a"*64),
            "skillName": "important-log-summary", "skillDigest": "a"*64, "sources": [span], "sourceGaps": [],
            "priorCumulativeEntryId": None, "dbHealth": catalog.health_snapshot()["active"], "requestedAt": "2026-10-13T00:00:00Z"}
        with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
            journal.reserve_summary_request(request)
            journal.record_summary_state(request["requestId"], "sending")
        return request, catalog

    def test_default_restart_false_retains_one_owned_fixture_process_and_two_posts(self):
        original_popen = subprocess.Popen
        children = []
        def launch(argv, **kwargs):
            # Give the fixture its normal response-producing phase; the product
            # restart option remains omitted and has no phase controller.
            fixture = json.loads((self.root / "fixture.json").read_text())
            self.assertNotIn("restartControl", fixture)
            fixture["phase"] = "restart"
            (self.root / "fixture.json").write_bytes(canonical_json(fixture))
            child = original_popen([sys.executable, "-c", CHILD, str(self.root), SDK_SCRIPT, NODE], **kwargs)
            children.append(child)
            return child
        try:
            with patch.object(runtime.subprocess, "Popen", side_effect=launch):
                result = runtime._execute(ROOT, Path(sys.executable).resolve(), self.root,
                                          recovery_control=True, pause_control=True)
            row = result["observations"][0]
            self.assertIsNone(row["error"])
            self.assertEqual(len(children), 1)
            self.assertEqual(result["io"]["omp_children"], 1)  # Fixture process, not native OMP.
            self.assertEqual(len(row["localhost_model_posts"]), 2)
            self.assertNotIn("restart_control", row)
            self.assertTrue(row["cleanup_complete"])
            self.assertFalse(row["server_thread_alive"])
            self.assertEqual(row["http_handler_threads_alive"], 0)
            self.assertEqual(row["http_sockets_remaining"], 0)
            self.assertIsNotNone(children[0].returncode)
            self.assertEqual(runtime._remaining(children[0].pid), [])
            self.assertEqual(result["level"], "inconclusive")
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=3)

    def body(self, request, expected_id=None):
        prompt = json.dumps({"request": request})
        (self.root / "expected.json").write_bytes(canonical_json({"requestId": expected_id or request["requestId"],
            "systemSha": sha256(SYSTEM.encode()).hexdigest(), "promptSha256": sha256(prompt.encode()).hexdigest(), "promptBytes": len(prompt.encode())}))
        return {"model": "scripted", "tools": [{"function": {"name": "db_summary_save"}}],
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}

    def test_phase_or_request_binding_rejection_cannot_create_false_unknown(self):
        request, _ = self.simple_request()
        for kind in ("phase", "binding"):
            with self.subTest(kind=kind):
                control = runtime._RestartControl(self.root, time.monotonic()+5)
                changed = deepcopy(request)
                if kind == "binding":
                    changed["authorizationEpoch"] += 1
                body = self.body(changed, runtime.RESTART_REQUEST_ID if kind == "phase" else None)
                with server_for(self.root, control) as (server, violations):
                    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                    try:
                        conn.request("POST", "/v1/chat/completions", canonical_json(body))
                        response = conn.getresponse()
                        self.assertEqual(response.status, 400)
                        response.read()
                    finally:
                        conn.close()
                    self.assertTrue(violations)
                    self.assertFalse(control.post_seen.is_set())
                    with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
                        self.assertEqual(journal.get_summary_request(request["requestId"])["state"], "sending")
                        self.assertEqual(journal._connection.execute("SELECT COUNT(*) FROM summary_request_events_v1").fetchone()[0], 2)

    def test_expired_hold_has_no_response_and_normal_exit_cannot_confirm_unknown(self):
        request, catalog = self.simple_request()
        control = runtime._RestartControl(self.root, time.monotonic())
        body = self.body(request)
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                 stdin=subprocess.PIPE, env=runtime._environment(self.root), start_new_session=True)
        ref = {"pid": child.pid, "pgid": child.pid, "start_ticks": runtime._identity(child.pid)}
        try:
            with server_for(self.root, control) as (server, violations):
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                try:
                    conn.request("POST", "/v1/chat/completions", canonical_json(body))
                    with self.assertRaises(RemoteDisconnected):
                        conn.getresponse()
                finally:
                    conn.close()
                self.assertTrue(control.post_seen.is_set())
                self.assertTrue(control.handler_done.wait(timeout=1))
                self.assertIn("disconnect_hold_timeout", violations)
                self.assertEqual(control.observations["response_bytes"], 0)
                child.stdin.close()
                self.assertEqual(child.wait(timeout=2), 0)
                self.assertEqual(runtime._remaining(child.pid), [])
                with self.assertRaisesRegex(ValueError, "disconnect_no_response_proof_unavailable"):
                    control.confirm_unknown(child, ref, catalog)
                with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
                    self.assertEqual(journal.get_summary_request(request["requestId"])["state"], "sending")
                    self.assertEqual(journal._connection.execute("SELECT COUNT(*) FROM summary_result_receipts_v1").fetchone()[0], 0)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
