"""Independent partial-verifier boundaries; no OMP or operating MODEL launch.

The external launch seam substitutes an owned sleeping Python process. SQLite,
writer, source copy, private reserve helper and /proc cleanup remain real.
"""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
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
from workbench.backend.summary_compatibility import Q01_ITEMS, SummaryCompatibility
from workbench.backend.summary_writer import store_summary
from workbench.contracts.logs_v1 import canonical_json, summary_input_key
from workbench.storage.important_log import ImportantLogCatalog
from workbench.tasks.repository import TaskRepository


ROOT = Path(__file__).resolve().parents[2]
BODY = {"schemaVersion": 1, "facts": ["소유 private scripted fixture의 연결을 확인했다."],
        "interpretations": [], "unknowns": ["실제 MODEL 및 전체 Q01은 미검증이다."]}


def pending_http_observation(root, kind):
    """Observe real blocking read boundaries; an outer process bounds joins.

    The reader only emits events and delegates unchanged bytes to the product
    handler. The client stays open with incomplete input through server close.
    """
    reading = threading.Event()
    handler_done = threading.Event()
    product_handler = runtime._handler(root, [], [])

    class Reader:
        def __init__(self, original):
            self.original = original
            self.lines = 0

        def __getattr__(self, name):
            return getattr(self.original, name)

        def readline(self, *args, **kwargs):
            self.lines += 1
            if kind == "header" and self.lines == 2:
                reading.set()
            return self.original.readline(*args, **kwargs)

        def read(self, *args, **kwargs):
            if kind == "body":
                reading.set()
            return self.original.read(*args, **kwargs)

    class ObservedHandler(product_handler):
        def setup(self):
            super().setup()
            self.rfile = Reader(self.rfile)

        def finish(self):
            try:
                super().finish()
            finally:
                handler_done.set()

    server = runtime._Server(("127.0.0.1", 0), ObservedHandler)
    serve = threading.Thread(target=server.serve_forever, name="independent-http-serve")
    serve.start()
    client = socket.create_connection(server.server_address, timeout=1)
    pending = (b"POST /v1/chat/completions HTTP/1.1\r\nX-Incomplete: " if kind == "header" else
               b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 32768\r\n\r\n{")
    try:
        client.sendall(pending)
        if not reading.wait(timeout=2):
            raise AssertionError("product handler never entered pending " + kind + " read")
        if handler_done.is_set() or server.accepted_count() != 1:
            raise AssertionError("pending input was not held in an owned accepted socket")
        started = time.monotonic()
        server.shutdown()
        server.shutdown_accepted()
        handlers = tuple(server._threads)
        server.server_close()
        serve.join(timeout=1)
        elapsed = time.monotonic() - started
        result = {"case": kind, "cleanup_seconds": elapsed,
                  "handler_read_observed": reading.is_set(), "handler_finish_observed": handler_done.is_set(),
                  "handler_threads_alive": sum(thread.is_alive() for thread in handlers),
                  "serve_thread_alive": serve.is_alive(), "accepted_sockets_remaining": server.accepted_count()}
        assert elapsed < 2, result
        assert handler_done.is_set() and not serve.is_alive(), result
        assert result["handler_threads_alive"] == 0 and result["accepted_sockets_remaining"] == 0, result
        return result
    finally:
        client.close()
        server.shutdown()
        server.shutdown_accepted()
        server.server_close()
        serve.join(timeout=1)


def request_for(health):
    subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
    text = b"owned independent source"
    source = {"schemaVersion": 1, "source": {"schemaVersion": 1,
              "sourceKind": "terminal", "collectorEpoch": 7, "commandId": subject["id"]},
              "start": 0, "end": len(text), "digest": sha256(text).hexdigest()}
    skill = "7" * 64
    return {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject,
            "revision": None, "authorizationRef": str(uuid4()), "authorizationEpoch": 1,
            "inputKey": summary_input_key(subject, None, [source], [], skill),
            "skillName": "important-log-summary", "skillDigest": skill,
            "sources": [source], "sourceGaps": [], "priorCumulativeEntryId": None,
            "dbHealth": health, "requestedAt": "2026-10-13T00:00:00Z"}


class PartialProviderIndependentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def owned(self, name="owned"):
        root = self.base / name
        root.mkdir(mode=0o700)
        return root

    def completion(self, root, *, wrong_binding=False):
        health = json.loads((root / "fixture.json").read_text())["dbHealth"]
        request = request_for(health)
        result = {name: deepcopy(request[name]) for name in
                  ("schemaVersion", "requestId", "inputKey", "skillDigest", "sources")}
        result.update({name: deepcopy(BODY[name]) for name in ("facts", "interpretations", "unknowns")})
        result["usage"] = {"schemaVersion": 1, "inputTokens": None,
                           "outputTokens": None, "costUsd": None}
        with TaskRepository(DataLayout(root / "data").tasks) as journal:
            journal.reserve_summary_request(request)
            journal.record_summary_state(request["requestId"], "sending")
        writer = store_summary(root / "data", request["requestId"], result)
        self.assertTrue(writer["saved"])
        if wrong_binding:
            result["inputKey"] = "8" * 64
        completed = {"requestId": request["requestId"], "outcome": {
            "status": "summary_result", "writer": writer, "result": result},
            "exercised": ["Q01-route", "Q01-skill", "Q01-tools", "Q01-result", "Q01-context", "Q01-origin"],
            "parentUnchanged": True, "registryUnchanged": True,
            "settingsUnchanged": True, "childDisposed": True}
        (root / "completed.json").write_bytes(canonical_json(completed))
        return request

    def execute_with_local_child(self, *, wrong_binding=False):
        root = self.owned()
        original_launch = subprocess.Popen
        children, captured = [], []

        def launch(argv, **kwargs):
            captured.append((argv, kwargs))
            child = original_launch([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            self.completion(root, wrong_binding=wrong_binding)
            return child

        try:
            with patch.object(runtime.subprocess, "Popen", side_effect=launch) as seam:
                observed = runtime._execute(ROOT, Path(sys.executable).resolve(), root)
            self.assertEqual(seam.call_count, 1)
            self.assertIsNotNone(children[0].returncode)
            self.assertEqual(runtime._remaining(children[0].pid), [])
            return root, captured[0], observed
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=3)

    def test_real_database_receipt_and_cleanup_still_produce_only_inconclusive(self):
        root, (argv, kwargs), observed = self.execute_with_local_child()
        row = observed["observations"][0]
        writer = row["completed"]["writerObserved"]
        self.assertIsNone(row["error"])
        self.assertTrue(row["cleanup_complete"])
        self.assertFalse(row["server_thread_alive"])
        self.assertEqual(row["http_handler_threads_alive"], 0)
        self.assertEqual(row["http_sockets_remaining"], 0)
        self.assertEqual(writer["actual_body"], BODY)
        self.assertEqual(writer["catalog_entries"], 1)
        self.assertEqual(writer["request_state"], "stored")
        self.assertTrue(writer["receipt_matches"])
        self.assertTrue(writer["request_binding_matches"])
        self.assertEqual(observed["level"], "inconclusive")
        self.assertEqual(set(observed["items"].values()), {"inconclusive"})
        for key in ("network_requests", "auth_store_open", "settings_writes"):
            self.assertIsNone(observed["io"][key])
        self.assertEqual(observed["io"]["omp_children"], 1)
        self.assertEqual(kwargs["cwd"], root / "project")
        self.assertTrue(kwargs["start_new_session"])
        self.assertIn(str(root / "runtime/omp_bridge/g3/summary_verification.ts"), argv)
        self.assertEqual(json.loads((root / "observation.json").read_text()), observed)

        identity = {"policy_version": 1, "unknowns": [], "policy_sha256": "1" * 64,
                    "closure_digest": "2" * 64, "runtime_digest": "3" * 64,
                    "stable_host": {"os_family": "Linux", "architecture": "x86_64", "python_minor": "3.12"}}
        service = SummaryCompatibility(self.base / "compat", identity=lambda: deepcopy(identity))
        service.protect({"episode": "before", "reason": "guard_confirmation_unavailable"})
        with patch("workbench.backend.summary_compat_provider.observe_owned", return_value=observed), \
             patch("workbench.backend.summary_compat_provider.observe_priority", return_value={
                 "kind": "owned_product_priority_partial", "native_launches": 0,
                 "errors": [], "cleanup_complete": True}):
            result = service.verify_scripted()
        self.assertEqual(result["result"], "inconclusive")
        self.assertEqual(set(result["items"]), set(Q01_ITEMS))
        self.assertEqual(result["items"]["Q01-priority"], "not_run")
        self.assertEqual(result["items"]["Q01-recovery"], "not_run")
        self.assertFalse(service.verify_reference(result["id"]))
        self.assertFalse(service.admission())
        self.assertEqual(service.status()["reason"], "guard_confirmation_unavailable")
        self.assertEqual(result["model_metadata"]["status"], "inconclusive")
        self.assertFalse((self.base / "raw-v2").exists())

    def test_completed_result_cannot_substitute_another_binding_for_the_stored_request(self):
        _, _, observed = self.execute_with_local_child(wrong_binding=True)
        row = observed["observations"][0]
        self.assertIn("owned_writer_observation_mismatch", row["error"])
        self.assertFalse(row["completed"]["writerObserved"]["request_binding_matches"])
        self.assertFalse(row["completed"]["writerObserved"]["receipt_matches"])
        self.assertTrue(row["cleanup_complete"])
        self.assertEqual(observed["level"], "inconclusive")

    def test_launch_failure_is_unavailable_without_retry_under_the_90_second_budget(self):
        root = self.owned()
        with patch.object(runtime.subprocess, "Popen", side_effect=OSError("owned launch unavailable")) as launch:
            observed = runtime._execute(ROOT, Path(sys.executable).resolve(), root)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(runtime.DEADLINE_SECONDS, 90)
        self.assertEqual(observed["level"], "unavailable")
        self.assertIn("OSError:owned launch unavailable", observed["unknowns"])

    def test_private_reserve_cli_writes_real_journal_and_rejects_oversized_input(self):
        root = self.owned()
        (root / "home").mkdir(mode=0o700)
        (root / "data").mkdir(mode=0o700)
        catalog = ImportantLogCatalog(root / "data/important-logs", create=True)
        request = request_for(catalog.health_snapshot()["active"])
        copied = runtime._copy_runtime(ROOT, root)
        env = runtime._environment(root)
        env["PYTHONPATH"] = str(copied / "src")
        argv = [sys.executable, "-m", "workbench.backend.summary_compat_runtime", "--reserve", str(root)]
        accepted = subprocess.run(argv, input=canonical_json(request), env=env, cwd=root,
                                  capture_output=True, timeout=10)
        self.assertEqual(accepted.returncode, 0, accepted.stderr.decode())
        with TaskRepository(DataLayout(root / "data").tasks) as journal:
            record = journal.get_summary_request(request["requestId"])
            self.assertEqual(record["request"], request)
            self.assertEqual(record["state"], "sending")
            self.assertIsNone(record["receipt"])
        rejected = subprocess.run(argv, input=b" " * 32769, env=env, cwd=root,
                                  capture_output=True, timeout=10)
        self.assertNotEqual(rejected.returncode, 0)
        with TaskRepository(DataLayout(root / "data").tasks) as journal:
            self.assertEqual(journal.get_summary_request(request["requestId"]), record)

    def test_constructed_environment_and_source_copy_exclude_user_and_development_inputs(self):
        root = self.owned()
        dirty = {name: "unowned-fixture" for name in (
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "NODE_OPTIONS", "BUN_OPTIONS", "LD_PRELOAD", "PYTHONPATH", "PI_CONFIG_DIR", "OMP_PROFILE")}
        with patch.dict(os.environ, dirty):
            env = runtime._environment(root)
        self.assertFalse((set(dirty) - {"PI_CONFIG_DIR"}) & set(env))
        self.assertEqual(env["PI_CONFIG_DIR"], str(root / "config"))
        self.assertEqual(env["HOME"], str(root / "home"))
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        copied = runtime._copy_runtime(ROOT, root)
        self.assertEqual({p.name for p in copied.iterdir()}, {"src", "omp_bridge", "pyproject.toml"})
        self.assertFalse(any(p.is_symlink() or p.name == "__pycache__" or p.suffix == ".pyc"
                             for p in copied.rglob("*")))
        for path in copied.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)


    def test_pending_header_and_body_cleanup_finish_before_inactivity_timeout_without_client_close(self):
        observations = []
        for kind in ("header", "body"):
            with self.subTest(kind=kind):
                root = self.owned(kind)
                environment = runtime._environment(root)
                environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + str(ROOT)
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                         "--pending-http", str(root), kind],
                                        env=environment, cwd=root, capture_output=True,
                                        text=True, timeout=7, start_new_session=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                observation = json.loads(result.stdout)
                self.assertEqual(observation["case"], kind)
                self.assertTrue(observation["handler_read_observed"])
                self.assertTrue(observation["handler_finish_observed"])
                self.assertLess(observation["cleanup_seconds"], 2)
                self.assertFalse(observation["serve_thread_alive"])
                self.assertEqual(observation["handler_threads_alive"], 0)
                self.assertEqual(observation["accepted_sockets_remaining"], 0)
                observations.append(observation)
        print("independent_pending_http=" + json.dumps(observations))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--pending-http":
        print(json.dumps(pending_http_observation(Path(sys.argv[2]), sys.argv[3])))
    else:
        unittest.main()
