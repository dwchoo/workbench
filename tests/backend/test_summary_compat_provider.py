"""Narrow owned-C1 contracts; launch doubles never issue a runtime Q01 pass."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import signal
import socket
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
from workbench.backend.summary_compat_provider import ScriptedCompatibilityProvider
from workbench.backend.summary_compatibility import SummaryCompatibility, Q01_ITEMS
from workbench.backend.summary_writer import store_summary
from workbench.contracts.logs_v1 import summary_input_key
from workbench.storage.important_log import ImportantLogStore
from workbench.tasks.repository import TaskRepository

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_BODY = {"schemaVersion": 1, "facts": ["소유 private scripted fixture의 연결을 확인했다."],
                 "interpretations": [], "unknowns": ["실제 MODEL 및 전체 Q01은 미검증이다."]}


def slow_http_case():
    """Real product server/handler; the parent test imposes an 8-second deadline."""
    observations = []
    for kind in ("header", "body"):
        with tempfile.TemporaryDirectory() as temporary:
            server = runtime._Server(("127.0.0.1", 0), runtime._handler(Path(temporary), [], []))
            serve = threading.Thread(target=server.serve_forever)
            serve.start()
            client = socket.create_connection(server.server_address, timeout=1)
            if kind == "header":
                client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nX-Slow: ")
            else:
                client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 32768\r\n\r\n{")
            stop = threading.Event()
            def trickle():
                while not stop.is_set():
                    try:
                        client.sendall(b" ")
                    except OSError:
                        return
                    stop.wait(0.05)
            sender = threading.Thread(target=trickle)
            sender.start()
            deadline = time.monotonic() + 1
            while server.accepted_count() != 1:
                if time.monotonic() > deadline:
                    raise AssertionError("owned HTTP accept deadline")
                time.sleep(0.005)
            time.sleep(0.1)
            started = time.monotonic()
            server.shutdown()
            server.shutdown_accepted()
            handlers = tuple(server._threads)
            server.server_close()
            serve.join(timeout=1)
            elapsed = time.monotonic() - started
            stop.set()
            sender.join(timeout=1)
            client.close()
            alive = sum(thread.is_alive() for thread in handlers)
            assert elapsed < 2.5, elapsed
            assert not serve.is_alive() and not sender.is_alive() and alive == 0
            assert server.accepted_count() == 0
            observations.append({"case": kind, "cleanup_seconds": elapsed, "serve_thread_alive": False,
                                 "handler_threads_alive": alive, "accepted_sockets_remaining": 0})
    return observations


def proc_stat(ticks="owned-ticks", state="S"):
    fields = [state] + ["0"] * 18 + [ticks]
    return "876543 (owned fixture) " + " ".join(fields)


class Process:
    pid = 876543

    def __init__(self):
        self.returncode = None
        self.stdin = BytesIO()
        self.polls = 0
        self.waits = 0
        self.timeout_once = False

    def poll(self):
        self.polls += 1
        return self.returncode

    def wait(self, timeout):
        self.waits += 1
        if self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired("owned", timeout)
        self.returncode = 0
        return 0


class OwnedC1Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "omp"
        self.binary.write_bytes(b"synthetic-external-launch-double")
        self.binary.chmod(0o700)

    def private(self):
        root = self.root / "owned"
        root.mkdir(mode=0o700)
        return root

    def completion(self, root):
        """Actual fixed writer/DB, but no OMP/model and no product PASS claim."""
        health = json.loads((root / "fixture.json").read_text())["dbHealth"]
        subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        text = "독립 저장 검증 fixture"
        span = {"schemaVersion": 1, "source": {"schemaVersion": 1, "sourceKind": "terminal",
            "collectorEpoch": 1, "commandId": subject["id"]}, "start": 0,
            "end": len(text.encode()), "digest": sha256(text.encode()).hexdigest()}
        digest = "a" * 64
        request = {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject, "revision": None,
            "authorizationRef": str(uuid4()), "authorizationEpoch": 1,
            "inputKey": summary_input_key(subject, None, [span], [], digest),
            "skillName": "important-log-summary", "skillDigest": digest, "sources": [span],
            "sourceGaps": [], "priorCumulativeEntryId": None, "dbHealth": health,
            "requestedAt": "2026-10-13T00:00:00Z"}
        result = {key: deepcopy(request[key]) for key in ("schemaVersion", "requestId", "inputKey", "skillDigest", "sources")}
        result.update(facts=EXPECTED_BODY["facts"], interpretations=[], unknowns=EXPECTED_BODY["unknowns"],
            usage={"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None})
        with TaskRepository(DataLayout(root / "data").tasks) as journal:
            journal.reserve_summary_request(request)
            journal.record_summary_state(request["requestId"], "sending")
        written = store_summary(root / "data", request["requestId"], result)
        self.assertTrue(written["saved"])
        completed = {"requestId": request["requestId"], "outcome": {"status": "summary_result",
            "sent": True, "writer": written, "result": result},
            "exercised": ["Q01-route", "Q01-tools", "Q01-result"], "childDisposed": True}
        (root / "completed.json").write_text(json.dumps(completed))

    def run_double(self, *, completed=True, store_override=None):
        root, process = self.private(), Process()
        captured = {}
        def launch(argv, **kwargs):
            captured.update(argv=argv, **kwargs)
            if completed:
                self.completion(root)
            return process
        def stop(value, ref):
            self.assertIs(value, process)
            process.returncode = 0
            return None
        with patch.object(runtime.subprocess, "Popen", side_effect=launch), \
             patch.object(runtime, "_identity", return_value="owned-ticks"), \
             patch.object(runtime, "_remaining", return_value=[]), \
             patch.object(runtime, "_stop_leader", side_effect=stop), \
             patch.object(runtime, "DEADLINE_SECONDS", 0):
            if store_override:
                original = ImportantLogStore.get_entry
                def entry(store, entry_id):
                    return store_override(original(store, entry_id))
                with patch.object(ImportantLogStore, "get_entry", entry):
                    observed = runtime._execute(ROOT, self.binary, root)
            else:
                observed = runtime._execute(ROOT, self.binary, root)
        return root, captured, observed

    def test_isolated_copy_and_launch_environment_excludes_inherited_secrets(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-a-real-secret", "HTTP_PROXY": "unowned",
                                     "BUN_OPTIONS": "unowned-hook", "PI_CODING_AGENT_DIR": "/unowned"}):
            root, launched, observed = self.run_double()
        env = launched["env"]
        self.assertFalse({"OPENAI_API_KEY", "HTTP_PROXY", "BUN_OPTIONS"} & set(env))
        self.assertEqual(env["HOME"], str(root / "home"))
        self.assertEqual(env["PI_CODING_AGENT_DIR"], str(root / "agent"))
        self.assertTrue(launched["start_new_session"])
        self.assertEqual(launched["cwd"], root / "project")
        self.assertEqual((root.stat().st_mode & 0o777), 0o700)
        self.assertTrue((root / "runtime/src/workbench").is_dir())
        self.assertFalse(any((root / "runtime" / name).exists() for name in ("tests", "tools", "docs", ".workflow")))
        fixture = json.loads((root / "fixture.json").read_text())
        self.assertTrue(fixture["endpoint"].startswith("http://127.0.0.1:"))
        self.assertEqual(observed["level"], "inconclusive")
        self.assertEqual(observed["io"]["auth_store_open"], None)
        self.assertEqual(observed["io"]["settings_writes"], None)
        self.assertEqual(observed["io"]["network_requests"], None)

    def test_fixed_writer_body_is_exact_and_durable_receipt_is_observed(self):
        _, _, observed = self.run_double()
        facts = observed["observations"][0]
        self.assertIsNone(facts["error"])
        writer = facts["completed"]["writerObserved"]
        self.assertEqual(writer["actual_body"], EXPECTED_BODY)
        self.assertEqual(writer["catalog_entries"], 1)
        self.assertEqual(writer["request_state"], "stored")
        self.assertEqual(writer["receipt"]["request_id"], facts["completed"]["requestId"])
        self.assertEqual(writer["receipt"]["entry_id"], facts["completed"]["outcome"]["writer"]["entryId"])
        self.assertTrue(writer["request_binding_matches"])
        self.assertTrue(writer["receipt_matches"])
        self.assertTrue(facts["cleanup_complete"])
        self.assertEqual(set(observed["items"].values()), {"inconclusive"})

    def test_body_comparison_rejects_extra_field_without_subset_relaxation(self):
        def extra(entry):
            entry["body"]["extra"] = "not-in-contract"
            return entry
        _, _, observed = self.run_double(store_override=extra)
        self.assertIn("owned_writer_observation_mismatch", observed["observations"][0]["error"])
        self.assertEqual(observed["level"], "inconclusive")

    def test_timeout_keeps_partial_io_unknown_and_stops_owned_leader(self):
        _, _, observed = self.run_double(completed=False)
        facts = observed["observations"][0]
        self.assertIn("owned_verification_timeout", facts["error"])
        self.assertTrue(facts["cleanup_complete"])
        self.assertFalse(facts["server_thread_alive"])
        self.assertEqual(observed["items"], {})
        self.assertIsNone(observed["io"]["auth_store_open"])

    def test_unsafe_root_refuses_before_any_write_or_launch(self):
        target = self.root / "outside"
        target.mkdir(mode=0o755)
        link = self.root / "linked"
        link.symlink_to(target, target_is_directory=True)
        with patch.object(runtime.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "unsafe_verification_root"):
                runtime._execute(ROOT, self.binary, link)
        launch.assert_not_called()
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_provider_uses_product_runtime_and_unavailable_never_launches(self):
        expected = runtime.unavailable("owned_runtime_unavailable")
        with patch("workbench.backend.summary_compat_provider.observe_owned", return_value=expected) as observer:
            self.assertEqual(ScriptedCompatibilityProvider().observe(), expected)
        observer.assert_called_once_with(ROOT)
        with patch.object(runtime.shutil, "which", return_value=None), patch.object(runtime.subprocess, "Popen") as launch:
            self.assertEqual(runtime.observe_owned(ROOT)["level"], "unavailable")
        launch.assert_not_called()

    def test_ambiguous_cleanup_preserves_temp_evidence(self):
        observed = runtime.unavailable("cleanup")
        observed.update(level="inconclusive", observations=[{"retained_private_root": "owned"}])
        with patch.object(runtime.shutil, "which", return_value=str(self.binary)), \
             patch.object(runtime.tempfile, "mkdtemp", return_value=str(self.private())), \
             patch.object(runtime, "_execute", return_value=observed), \
             patch.object(runtime.shutil, "rmtree") as delete:
            runtime.observe_owned(ROOT)
        delete.assert_not_called()

    def test_signal_never_targets_reaped_missing_reused_or_zombie_leader(self):
        for state, ticks, returncode in (("S", "new-ticks", None), ("Z", "owned-ticks", None),
                                          ("S", "owned-ticks", 0)):
            process = Process()
            process.returncode = returncode
            with patch.object(Path, "read_text", return_value=proc_stat(ticks, state)), patch.object(runtime.os, "killpg") as kill:
                self.assertFalse(runtime._signal_leader(process, {"start_ticks": "owned-ticks"}, signal.SIGTERM))
            kill.assert_not_called()
        with patch.object(Path, "read_text", side_effect=FileNotFoundError), patch.object(runtime.os, "killpg") as kill:
            self.assertFalse(runtime._signal_leader(Process(), {"start_ticks": "owned-ticks"}, signal.SIGKILL))
        kill.assert_not_called()

    def test_term_and_timeout_kill_each_require_immediate_live_identity(self):
        process, calls = Process(), []
        process.timeout_once = True
        def read(_path, *args, **kwargs):
            calls.append("identity")
            return proc_stat()
        def kill(_pid, sig):
            calls.append(sig)
        with patch.object(Path, "read_text", read), patch.object(runtime.os, "killpg", side_effect=kill):
            self.assertIsNone(runtime._stop_leader(process, {"start_ticks": "owned-ticks"}))
        self.assertEqual(calls, ["identity", signal.SIGTERM, "identity", signal.SIGKILL])
        self.assertEqual(process.polls, 1)
        self.assertEqual(process.waits, 2)

    def test_exited_leader_and_timeout_identity_loss_do_not_signal_numeric_group(self):
        process = Process()
        process.returncode = 0
        with patch.object(runtime.os, "killpg") as kill:
            self.assertIsNone(runtime._stop_leader(process, {"start_ticks": "owned-ticks"}))
        kill.assert_not_called()
        process = Process()
        process.timeout_once = True
        with patch.object(Path, "read_text", side_effect=[proc_stat(), proc_stat("new-ticks")]), \
             patch.object(runtime.os, "killpg") as kill:
            self.assertEqual(runtime._stop_leader(process, {"start_ticks": "owned-ticks"}), "owned_process_identity_unavailable")
        self.assertEqual(kill.call_count, 1)
        self.assertEqual(kill.call_args.args[1], signal.SIGTERM)

    def test_partial_aggregation_retains_protection_and_cannot_be_admitted(self):
        identity = {"policy_version": 1, "unknowns": [], "policy_sha256": "1" * 64,
            "closure_digest": "2" * 64, "runtime_digest": "3" * 64,
            "stable_host": {"os_family": "Linux", "architecture": "x86_64", "python_minor": "3.12"}}
        service = SummaryCompatibility(self.root / "compat", identity=lambda: deepcopy(identity))
        service.protect({"episode": "old", "reason": "compatibility_failed"})
        observed = runtime.unavailable("complete_q01_unverified")
        observed.update(level="inconclusive", items={"Q01-route": "inconclusive", "Q01-tools": "pass"},
                        observations=[{"kind": "owned_C1_partial", "result": "inconclusive"}],
                        io={"omp_children": 1, "model_calls": 2, "network_requests": None,
                            "auth_store_open": None, "settings_writes": None})
        with patch.object(ScriptedCompatibilityProvider, "observe", return_value=observed):
            result = service.verify_scripted()
        self.assertEqual(result["result"], "inconclusive")
        self.assertEqual(result["items"]["Q01-route"], "inconclusive")
        self.assertEqual(result["items"]["Q01-tools"], "not_run")
        self.assertEqual(result["items"]["Q01-priority"], "not_run")
        self.assertEqual(result["items"]["Q01-recovery"], "not_run")
        self.assertEqual(set(result["items"]), set(Q01_ITEMS))
        self.assertFalse(service.verify_reference(result["id"]))
        self.assertFalse(service.admission())
        self.assertEqual(service.status()["reason"], "compatibility_failed")
        self.assertIn("output_budget_unavailable", result["model_metadata"]["unknowns"])

    def test_real_slow_header_and_body_cleanup_are_bounded_and_join_handlers(self):
        completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--slow-http-case"],
            capture_output=True, text=True, timeout=8, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        observations = json.loads(completed.stdout)
        self.assertEqual([row["case"] for row in observations], ["header", "body"])
        for row in observations:
            self.assertLess(row["cleanup_seconds"], 2.5)
            self.assertFalse(row["serve_thread_alive"])
            self.assertEqual(row["handler_threads_alive"], 0)
            self.assertEqual(row["accepted_sockets_remaining"], 0)
        print("slow_http_observations=" + json.dumps(observations))


if __name__ == "__main__":
    if sys.argv[1:] == ["--slow-http-case"]:
        print(json.dumps(slow_http_case()))
    else:
        unittest.main()
