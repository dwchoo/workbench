"""Synthetic auth/Task/DB fixtures only; no user auth, Native or paid SDK call."""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests.integration import live_real_summary as trial
from tests.integration import test_model_metadata_independent as metadata_fixture
from workbench.backend.omp_home import user_auth_store
from workbench.backend.paths import DataLayout
from workbench.backend.summary_writer import store_summary
from workbench.ipc.bridge_g3.mailbox import BridgePeer
from workbench.tasks.repository import InvalidTransitionError, TaskRepository


class RealSummaryIndependent(unittest.TestCase):
    CANARY = "SYNTHETIC-AUTH-CANARY-NEVER-REPORT"

    def setUp(self):
        output = os.environ.get("WB_DVL_PRECISE_TEST_OUTPUT")
        if output:
            self.assertTrue(Path(output).is_absolute() and Path(output).is_dir())
        temporary = tempfile.TemporaryDirectory(prefix="real-summary-fixture-", dir=output)
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.auth = self.parent / "synthetic-auth.db"
        connection = sqlite3.connect(self.auth)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE fake_auth(value TEXT)")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("INSERT INTO fake_auth VALUES (?)", (self.CANARY,))
        connection.commit()
        self.source_files = [self.auth, Path(str(self.auth) + "-wal"), Path(str(self.auth) + "-shm")]
        self.source_bytes = {p: p.read_bytes() for p in self.source_files}
        self.binary = self.parent / "fixture-omp"
        self.binary.write_bytes(b"fake executable identity; never launched")
        self.roots, self.processes = [], []
        self.dispatches = 0
        self.saved_result = None

    @contextmanager
    def external(self, mode="stored", cleanup_failure=False):
        owner = self
        real_mkdtemp, real_which = tempfile.mkdtemp, shutil.which

        def root(*args, **kwargs):
            path = Path(real_mkdtemp(*args, **kwargs))
            owner.roots.append(path)
            owner.addCleanup(shutil.rmtree, path, True)
            return str(path)

        class NoParentInput(io.BytesIO):
            def write(self, data):
                raise AssertionError("manual trial must not prompt the parent")

        class Native:
            pid = 99_999_992

            def __init__(self, argv, **kwargs):
                self.argv, self.options = argv, kwargs
                self.returncode, self.stdin = None, NoParentInput()
                self.selected_auth = user_auth_store(kwargs["env"])
                self.copied_auth = owner.roots[-1] / "agent/agent.db"
                self.copy_private_at_launch = self.copied_auth.is_file() and self.copied_auth.stat().st_mode & 0o777 == 0o600
                owner.processes.append(self)

            def poll(self):
                return self.returncode

        class Bridge:
            _thread = None

            def __init__(self, socket, tokens):
                self.socket = socket

            def start(self):
                self.socket.touch(mode=0o600)

            def peer(self, role, **kwargs):
                owner.assertEqual(role, "manager")
                return BridgePeer("manager", "synthetic-parent", 2, Native.pid)

            def close(self):
                self.socket.unlink(missing_ok=True)

        class Adapter:
            def __init__(self, bridge, **kwargs):
                pass

            def read_runtime(self, *, expected_peer):
                owner.assertEqual(expected_peer, ("synthetic-parent", 2))
                table = trial.load_model_table(trial.ROOT / "omp_bridge/omp-models.yml")
                descriptor = metadata_fixture.ModelReadbackIndependent("runTest")
                descriptor.manager_selector = table["manager"]["models"]["default"]
                descriptor.child_selector = table["worker"]["models"]["default"]
                descriptor.table_digest = sha256(json.dumps(table, sort_keys=True, ensure_ascii=False,
                                                          separators=(",", ":")).encode()).hexdigest()
                metadata = descriptor.metadata()
                metadata["parentSessionId"] = "synthetic-parent"
                return {"metadata": metadata, "instructions": "Synthetic parent definition"}

            def dispatch_outcome(self, prepared, *, expected_peer, authority_token, writer):
                owner.dispatches += 1
                owner.assertEqual(owner.dispatches, 1)
                owner.assertTrue(authority_token.current())
                if mode == "not_sent":
                    raise trial.SummaryDeferred("controlled not sent")
                owner.assertTrue(authority_token.claim())
                if mode == "sent_unknown":
                    return {"sent": True, "status": "unknown"}
                if mode == "unknown":
                    raise OSError("controlled SDK result unavailable")
                request = prepared["request"]
                result = {key: deepcopy(request[key]) for key in
                          ("schemaVersion", "requestId", "inputKey", "skillDigest", "sources")}
                result.update(facts=["합성 Task 입력을 저장 경로에서 확인했다."], interpretations=[], unknowns=[],
                              usage={"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None})
                owner.saved_result = result
                written = store_summary(Path(writer["dataRoot"]), request["requestId"], result)
                owner.assertTrue(written["saved"])
                return {"sent": True, "status": "summary_result", "protected": False,
                        "result": result, "writer": written}

        def stop(process, ref):
            owner.assertEqual(ref, {"pid": Native.pid, "pgid": Native.pid, "start_ticks": "20202"})
            if cleanup_failure:
                return "controlled_owned_cleanup_incomplete"
            process.returncode = 0
            return None

        with ExitStack() as stack:
            stack.enter_context(patch.object(trial.tempfile, "mkdtemp", side_effect=root))
            stack.enter_context(patch.object(trial.shutil, "which", side_effect=lambda name, *a, **kw:
                str(self.binary) if name == "omp" else real_which(name, *a, **kw)))
            stack.enter_context(patch.object(trial, "G3BridgeServer", Bridge))
            stack.enter_context(patch.object(trial, "SDKSummaryAdapter", Adapter))
            popen = stack.enter_context(patch.object(trial.subprocess, "Popen", side_effect=Native))
            stack.enter_context(patch.object(trial.rt, "_identity", return_value="20202"))
            stack.enter_context(patch.object(trial.rt, "_stop_leader", side_effect=stop))
            stack.enter_context(patch.object(trial.rt, "_remaining", return_value=[Native.pid] if cleanup_failure else []))
            yield popen

    def run_trial(self, name, *, mode="stored", cleanup_failure=False, auth=None):
        output = self.parent / name
        with self.external(mode, cleanup_failure) as popen:
            code, result = trial.run_real_summary(output, real_model=True, auth_source=auth or self.auth)
        self.assertEqual(json.loads((output / "result.json").read_bytes()), result)
        self.assertTrue(self.CANARY not in (output / "result.json").read_text(), "fake auth canary leaked into report")
        self.assertTrue(all(p.read_bytes() == data for p, data in self.source_bytes.items()), "synthetic auth source bytes changed")
        for process in self.processes:
            self.assertTrue(process.stdin.closed)
        return code, result, output, popen

    def test_opt_in_off_cannot_copy_auth_create_output_or_launch(self):
        output = self.parent / "off"
        with patch.object(trial, "_copy_auth", side_effect=AssertionError("auth access forbidden")) as copy, \
                patch.object(trial.subprocess, "Popen", side_effect=AssertionError("Native forbidden")) as native, \
                patch.object(trial, "SDKSummaryAdapter", side_effect=AssertionError("SDK forbidden")):
            code, result = trial.run_real_summary(output, auth_source=self.auth)
        self.assertEqual((code, result["status"], result["summary_requests"]), (2, "not_opted_in", 0))
        copy.assert_not_called()
        native.assert_not_called()
        self.assertFalse(output.exists())
        self.assertTrue(all(p.read_bytes() == data for p, data in self.source_bytes.items()), "synthetic auth source bytes changed")

    def test_fake_wal_blind_copy_is_private_and_invalid_copy_precedes_launch(self):
        root = self.parent / "copy"
        root.mkdir(mode=0o700)
        (root / "agent").mkdir(mode=0o700)
        connects, statements = [], []
        real_connect = sqlite3.connect

        def connect(path, **kwargs):
            connects.append(str(path))
            connection = real_connect(path, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(trial.sqlite3, "connect", side_effect=connect):
            trial._copy_auth(self.auth, root)
        copied = root / "agent/agent.db"
        self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
        self.assertFalse(copied.is_symlink() or (root / "auth-staging").exists())
        self.assertTrue(all(str(root) in path for path in connects))
        self.assertFalse(any("SELECT" in statement.upper() for statement in statements))
        with real_connect(copied) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM fake_auth").fetchone()[0], 1)
        self.assertTrue(all(p.read_bytes() == data for p, data in self.source_bytes.items()), "synthetic auth source bytes changed")
        invalid = self.parent / "invalid.db"
        invalid.write_bytes(b"not a SQLite database")
        code, result, _, popen = self.run_trial("invalid-copy", auth=invalid)
        self.assertEqual(code, 2)
        self.assertEqual((result["phase"], result["summary_requests"]), ("auth_snapshot", 0))
        popen.assert_not_called()
        self.assertEqual(self.dispatches, 0)
        self.assertTrue(all(not root.exists() for root in self.roots))

    def test_real_task_spec_authority_is_current_bound_and_claimed_once(self):
        layout = DataLayout(self.parent / "authority-data")
        layout.root.mkdir(mode=0o700)
        life, request, text, source, owned = trial._fixture(layout)
        authority = trial._Authority(layout, life, request, text, owned)
        self.assertFalse(authority.current())
        with TaskRepository(layout.tasks) as repo:
            repo.reserve_summary_request(request)
            repo.record_summary_state(request["requestId"], "sending")
            self.assertEqual(json.loads(text), repo.get_task_spec(request["subject"]["id"], 1)["spec"])
        self.assertEqual(source["sources"], request["sources"])
        self.assertTrue(authority.current())
        mismatched = deepcopy(request)
        mismatched["sources"][0]["digest"] = "f" * 64
        self.assertFalse(trial._Authority(layout, life, mismatched, text, owned).current())
        self.assertFalse(trial._Authority(layout, life, request, text, {**owned, "run_id": str(uuid4())}).current())
        self.assertTrue(authority.claim())
        self.assertFalse(authority.claim())
        with TaskRepository(layout.tasks) as repo:
            repo.record_summary_state(request["requestId"], "unknown")
        self.assertFalse(authority.current())

    def test_single_dispatch_stores_queries_same_task_and_selects_private_auth(self):
        code, result, output, _ = self.run_trial("stored")
        self.assertEqual((code, result["status"], self.dispatches), (0, "stored", 1))
        self.assertEqual(result["query"]["request_state"], "stored")
        self.assertTrue(result["query"]["response_digest_matches"] and result["query"]["prior_source_unchanged"])
        self.assertEqual(result["query"]["result_digest"], sha256(trial.canonical_json(self.saved_result)).hexdigest())
        with TaskRepository(DataLayout(output / "data").tasks) as repo:
            record = repo.get_summary_request(result["request_id"])
        self.assertEqual(record["request"]["subject"], {"schemaVersion": 1, "kind": "task", "id": result["task_id"]})
        self.assertEqual(record["request"]["sources"], result["sources"])
        process = self.processes[0]
        self.assertTrue(process.copy_private_at_launch)
        self.assertEqual(process.selected_auth, process.copied_auth,
                         "Native must select the private copied auth store, never an empty default location")
        self.assertEqual(result["overall_gate_status"], "inconclusive")
        self.assertFalse(result["raw_activation"])
        self.assertTrue(all(not root.exists() for root in self.roots))

    def test_not_sent_and_sent_unknown_are_journalled_without_automatic_replay(self):
        for mode, state in (("not_sent", "not_sent"), ("sent_unknown", "unknown"), ("unknown", "unknown")):
            with self.subTest(mode=mode):
                # Each trial has exactly one independently authorized admission.
                self.dispatches = 0
                code, result, output, _ = self.run_trial(mode, mode=mode)
                self.assertEqual((code, result["status"], self.dispatches), (2, state, 1))
                self.assertEqual(result["sent"], False if mode == "not_sent" else True if mode == "sent_unknown" else None)
                with TaskRepository(DataLayout(output / "data").tasks) as repo:
                    record = repo.get_summary_request(result["request_id"])
                    retry = {**record["request"], "requestId": str(uuid4())}
                    if state == "unknown":
                        with self.assertRaises(InvalidTransitionError):
                            repo.reserve_summary_request(retry)
                    else:
                        repo.reserve_summary_request(retry)
                self.assertEqual(record["state"], state)
                self.assertIsNone(record["receipt"])
                self.assertEqual(self.dispatches, 1)

    def test_cleanup_failure_removes_private_auth_and_reports_retained_owned_root(self):
        code, result, _, _ = self.run_trial("cleanup-failure", cleanup_failure=True)
        self.assertEqual((code, result["status"], self.dispatches), (2, "stored", 1))
        cleanup = result["cleanup"]
        self.assertEqual(cleanup["status"], "incomplete")
        self.assertTrue(cleanup["auth_copy_removed"])
        self.assertFalse(cleanup["private_root_removed"])
        retained = Path(cleanup["retained_private_root"])
        self.assertEqual(retained, self.roots[0])
        self.assertTrue(retained.is_dir())
        self.assertFalse((retained / "agent").exists())
        self.assertNotEqual(cleanup["remaining_owned"], [])

    def test_owner_path_binary_selection_precedes_auth_and_missing_never_copies(self):
        owner_bin = self.parent / "owner-bin"
        owner_bin.mkdir(mode=0o700)
        executable = owner_bin / "omp"
        executable.write_bytes(b"controlled owner PATH executable; must never execute")
        executable.chmod(0o700)
        real_which, real_copy = shutil.which, trial._copy_auth
        events = []

        def lookup(command, *args, **kwargs):
            selected = real_which(command, *args, **kwargs)
            if command == "omp":
                events.append(("binary", str(Path(selected).resolve()) if selected else None))
            return selected

        def copy_after_selection(source, root):
            self.assertEqual(events, [("binary", str(executable.resolve()))])
            events.append(("auth_copy", str(root)))
            real_copy(source, root)

        output = self.parent / "owner-path"
        with patch.dict(os.environ, {"PATH": str(owner_bin)}), self.external() as popen:
            # Override the general fixture's discovery double with real PATH semantics.
            with patch.object(trial.shutil, "which", side_effect=lookup), \
                    patch.object(trial, "_copy_auth", side_effect=copy_after_selection):
                code, result = trial.run_real_summary(output, real_model=True, auth_source=self.auth)
        self.assertEqual((code, result["status"], self.dispatches), (0, "stored", 1))
        popen.assert_called_once()
        process = self.processes[0]
        self.assertEqual(process.argv[0], str(executable.resolve()))
        self.assertTrue(Path(process.argv[0]).is_absolute())
        private_path = trial.rt._environment(self.roots[0])["PATH"]
        self.assertEqual(process.options["env"]["PATH"], private_path)
        self.assertNotIn(str(owner_bin), private_path.split(os.pathsep))
        self.assertEqual(result["binary_sha256"], sha256(executable.read_bytes()).hexdigest())
        self.assertEqual([event[0] for event in events], ["binary", "auth_copy"])
        self.assertTrue(process.stdin.closed)

        missing_bin = self.parent / "missing-bin"
        missing_bin.mkdir(mode=0o700)
        events.clear()
        with patch.dict(os.environ, {"PATH": str(missing_bin)}), self.external() as popen:
            with patch.object(trial.shutil, "which", side_effect=lookup), \
                    patch.object(trial, "_copy_auth", side_effect=AssertionError("missing binary must not access auth")) as copy:
                code, result = trial.run_real_summary(self.parent / "missing-binary", real_model=True, auth_source=self.auth)
        self.assertEqual((code, result["failure_reason"], result["summary_requests"]), (2, "omp_missing", 0))
        self.assertEqual(events, [("binary", None)])
        copy.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(self.dispatches, 1)
        self.assertTrue(all(p.read_bytes() == data for p, data in self.source_bytes.items()), "synthetic auth source bytes changed")
        self.assertTrue(all(not root.exists() for root in self.roots))


if __name__ == "__main__":
    unittest.main()
