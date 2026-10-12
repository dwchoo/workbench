"""Synthetic activation/cohort fixtures; not product verifier or operating MODEL evidence."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.backend.cli import main
from workbench.backend.flow_terminal import TerminalService
from workbench.backend.paths import DataLayout
from workbench.backend.raw_rotation import RawRotationPolicy
from workbench.backend.service import Backend
from workbench.backend.summary_compatibility import SummaryCompatibility
from workbench.backend.summary_scheduler import SummarySourceCollector
from workbench.storage.log_raw import port as raw_port
from workbench.storage.log_raw.port import DisabledSegmentedRawPort, command_source
from workbench.storage.log_raw.segments import SegmentedRawStore


class RawRotationTests(unittest.TestCase):
    def setUp(self):
        from tests.backend.test_summary_compatibility import CompatibilityCoreTests
        self.fixture = CompatibilityCoreTests("test_version_only_drift_and_unrelated_identity_never_create_current_pass")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.compat = self.fixture.root, self.fixture.service
        self.ref, self.record = self.fixture.fixture_verified_record()
        self.policy = RawRotationPolicy(DataLayout(self.root).raw_rotation, self.compat)

    def backend(self):
        backend = Backend.__new__(Backend)
        backend.layout = DataLayout(self.root)
        backend.segmented_raw_port = backend._owned_segmented_store = None
        backend._raw_port_lock = threading.RLock()
        backend._raw_rotation_error = None
        backend.raw_rotation = self.policy
        self.addCleanup(backend._close_owned_raw_store)
        return backend

    def terminal(self, factory):
        terminal = TerminalService.__new__(TerminalService)
        (self.root / "workflow").mkdir(mode=0o700, exist_ok=True)
        terminal._log_root = self.root / "workflow" / "terminal"
        terminal._segmented_raw_port = None
        terminal._segmented_raw_port_factory = factory
        terminal._repository_factory = None
        return terminal

    def test_default_unavailable_cli_and_noncanonical_reference_create_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "absent"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(["logs", "raw-rotation", "activate", "--yes", "--evidence", self.ref,
                             "--data-dir", str(root)])
            self.assertEqual(code, 1)
            self.assertFalse(root.exists())
        for reference in ("/tmp/pass.json", "pass=true", str(uuid4()), None):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                self.policy.activate(reference)
        self.assertFalse(self.policy.path.exists())
        self.assertFalse(self.policy.path.with_suffix(".lock").exists())

    def test_explicit_yes_required_and_valid_record_policy_persists_without_source_allocation(self):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(main(["logs", "raw-rotation", "activate", "--evidence", self.ref,
                                   "--data-dir", str(self.root)]), 1)
        activated = self.policy.activate(self.ref)
        self.assertEqual(activated["evidence"], self.ref)
        self.assertEqual(self.policy.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "raw-v2").exists())
        self.assertEqual(self.policy.activate(self.ref), activated)
        restored = RawRotationPolicy(self.policy.path, self.compat)
        self.assertTrue(restored.admits_new_source())
        self.assertEqual(restored.status()["activatedAt"], activated["activated_at"])

    def test_live_gate_not_cached_observation_and_verify_never_activates(self):
        self.compat.status()
        self.fixture.ident["closure_digest"] = "7" * 64
        self.assertEqual(self.compat.observed_status()["state"], "verified")
        with self.assertRaises(ValueError): self.policy.activate(self.ref)
        self.assertFalse(self.policy.path.exists())
        self.fixture.ident["closure_digest"] = self.record["identity"]["closure_digest"]
        self.compat.verify_scripted()
        self.assertFalse(self.policy.path.exists())
        self.assertFalse(self.policy.admits_new_source())

    def test_preliminary_pass_is_rechecked_under_compatibility_mutation(self):
        original = self.compat._mutation
        @contextmanager
        def interleave_protection():
            # Protection publishes between the preliminary public check and the guarded recheck.
            with original():
                state = self.compat._state()
                state["protection"] = "compatibility_failed"
                self.compat._save_state(state)
            with original(): yield
        with patch.object(self.compat, "_mutation", interleave_protection):
            with self.assertRaises(ValueError): self.policy.activate(self.ref)
        self.assertFalse(self.policy.path.exists())
        self.assertFalse((self.root / "raw-v2").exists())

    def test_activation_publication_and_protection_are_serialized(self):
        import workbench.backend.raw_rotation as module
        started, release, protected = threading.Event(), threading.Event(), threading.Event()
        original = module.write_private_json
        errors = []
        def publishing(path, value):
            started.set()
            if not release.wait(5): raise TimeoutError("test release")
            original(path, value)
        def activate():
            try: self.policy.activate(self.ref)
            except Exception as exc: errors.append(exc)
        def protect():
            try: self.compat.protect({"reason": "compatibility_failed"})
            except Exception as exc: errors.append(exc)
            finally: protected.set()
        with patch.object(module, "write_private_json", publishing):
            first = threading.Thread(target=activate)
            first.start()
            self.assertTrue(started.wait(5))
            second = threading.Thread(target=protect)
            second.start()
            try: self.assertFalse(protected.wait(0.1))
            finally:
                release.set()
                first.join(5); second.join(5)
            self.assertFalse(first.is_alive()); self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(self.policy.path.exists())
        self.assertFalse(self.policy.admits_new_source())

    def test_once_selected_command_cohorts_survive_activation_protection_and_version_drift(self):
        backend = self.backend()
        selected = []
        def choose():
            result = backend._new_raw_port()
            selected.append(result)
            return result
        terminal = self.terminal(choose)
        legacy = terminal._new_command("one", {}, None)
        self.assertIsNone(legacy.segmented_raw_port)
        self.policy.activate(self.ref)
        active = terminal._new_command("two", {}, None)
        self.assertIs(active.segmented_raw_port, backend.segmented_raw_port)
        self.assertEqual(active.raw_fields()["log_format"], "segmented-v1")
        self.assertEqual(backend._owned_segmented_store.segment_limit, 100_000_000)
        self.assertEqual(backend._owned_segmented_store.segment_count, 5)
        self.fixture.omp["version"] = "future"
        drift = terminal._new_command("three", {}, None)
        self.assertIs(drift.segmented_raw_port, active.segmented_raw_port)
        self.compat.protect({"reason": "compatibility_failed"})
        fallback = terminal._new_command("four", {}, None)
        self.assertIsNone(fallback.segmented_raw_port)
        legacy.write(b"legacy-after-activation")
        active.write(b"v2-after-protection")
        self.assertEqual(legacy.log_path.read_bytes(), b"legacy-after-activation")
        self.assertEqual(active.segmented_raw_port.status(active.raw_descriptor)["observed_bytes"], 19)
        self.assertEqual(len(selected), 4)
        self.assertIs(active.segmented_raw_port, drift.segmented_raw_port)
        status = backend.raw_status()["rotation"]
        self.assertTrue(status["activated"])
        self.assertEqual(status["newSourcePolicy"], "legacy-stop")

    def test_approved_task_run_selects_once_and_preserves_source_descriptor(self):
        from tests.workflow.test_run import WorkflowRuntimeTests, AUTOMATION
        from workbench.workflow.run import TaskWorkflow
        fixture = WorkflowRuntimeTests("test_four_exit_and_evidence_outcomes_on_real_host_shell")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        (self.root / "workflow").mkdir(mode=0o700)
        fixture.artifacts = self.root / "workflow" / "runs"
        fixture.artifacts.mkdir(mode=0o700)
        backend = self.backend()
        calls = []
        def choose():
            port = backend._new_raw_port()
            calls.append(port)
            return port
        workflow = TaskWorkflow(fixture.repository, fixture.mailbox, segmented_raw_port_factory=choose)
        task = fixture.approved_task("printf PASS; printf PASS > outcome.txt")
        legacy = workflow.start(task, 1, worktree_path=fixture.root / "first",
                                artifacts_root=fixture.artifacts, automation=AUTOMATION)
        self.addCleanup(legacy.close)
        self.assertTrue(legacy.collect(timeout=8)["exit_confirmed"])
        self.assertIsNone(legacy.segmented_raw_port)
        self.policy.activate(self.ref)
        task = fixture.approved_task("printf PASS; printf PASS > outcome.txt")
        active = workflow.start(task, 1, worktree_path=fixture.root / "second",
                                artifacts_root=fixture.artifacts, automation=AUTOMATION)
        self.addCleanup(active.close)
        self.compat.protect({"reason": "compatibility_failed"})
        self.assertTrue(active.collect(timeout=8)["exit_confirmed"])
        self.assertEqual(len(calls), 2)
        self.assertIs(active.segmented_raw_port, backend.segmented_raw_port)
        self.assertEqual(active.raw_descriptor["source"]["taskId"], task)
        self.assertEqual(active.raw_descriptor["source"]["revision"], 1)
        self.assertEqual(active.raw_descriptor["source"]["runId"], active.run_id)
        self.assertEqual(active._record["log_format"], "segmented-v1")
        self.assertIn("logs raw --run", active._record["read_command"])
        self.assertNotIn("log_format", legacy._record)
        self.assertEqual(backend.raw_query("run", active.run_id, 0, 4)["status"], "available")

    def test_injected_disabled_store_ownership_is_preserved(self):
        backend = self.backend()
        store = SegmentedRawStore(self.root / "injected", segment_limit=512, segment_count=2)
        self.addCleanup(store.close)
        backend.segmented_raw_port = DisabledSegmentedRawPort(store)
        with patch.object(self.policy, "admits_new_source", side_effect=AssertionError("disabled injection")):
            self.assertIs(backend._new_raw_port(), backend.segmented_raw_port)
        backend._close_owned_raw_store()
        self.assertIsNotNone(store._root)

    def test_corrupt_private_policy_falls_back_and_observational_status_never_checks_identity(self):
        self.policy.path.write_text('{"activated": true}')
        self.policy.path.chmod(0o600)
        self.assertFalse(self.policy.admits_new_source())
        with patch.object(self.compat, "admission", side_effect=AssertionError("display admission")), \
             patch.object(self.compat, "verify_reference", side_effect=AssertionError("display verify")), \
             patch.object(self.compat, "identity", side_effect=AssertionError("display hash")):
            status = self.policy.status()
        self.assertFalse(status["activated"])
        self.assertEqual(status["newSourcePolicy"], "legacy-stop")

    def prepare_previous_source(self, segmented):
        folder = self.root / "workflow" / "terminal"
        folder.parent.mkdir(mode=0o700, exist_ok=True)
        folder.mkdir(mode=0o700, exist_ok=True)
        identity = str(uuid4())
        body = b"previous raw"
        source = command_source(identity)
        if segmented:
            store = SegmentedRawStore(self.root / "raw-v2")
            port = DisabledSegmentedRawPort(store)
            _, descriptor = port.prepare_command(folder, identity)
            port.append(descriptor, body)
            store.close()
        else:
            path = folder / (identity + ".log")
            path.write_bytes(body); path.chmod(0o600)
        span = {"schemaVersion": 1, "source": source, "start": 0, "end": len(body),
                "digest": sha256(body).hexdigest()}
        return identity, [{"source_id": str(uuid4()), "cursor": 1, "range": span}]

    def test_collector_callback_previous_v2_and_legacy_return_same_pieces_and_missing_gaps(self):
        backend = self.backend()
        for segmented in (False, True):
            identity, records = self.prepare_previous_source(segmented)
            callbacks = [None, backend._raw_source_query]
            observed = []
            for callback in callbacks:
                collector = SummarySourceCollector(self.root, lambda: object(), raw_query_port=callback)
                observed.append(collector.collect(None, {"kind": "command", "id": identity}, records))
            self.assertEqual(observed[0], observed[1])
            self.assertEqual(observed[0][0][0].text, "previous raw")
        missing_id = str(uuid4())
        records = deepcopy(records)
        records[0]["range"]["source"] = command_source(missing_id)
        outcomes = []
        for callback in (None, backend._raw_source_query):
            collector = SummarySourceCollector(self.root, lambda: object(), raw_query_port=callback)
            with patch.object(collector, "_record_gap"):
                outcomes.append(collector.collect(None, {"kind": "command", "id": missing_id}, records))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][1][0]["reason"], "storage_gap")
        with self.assertRaises(FileNotFoundError):
            backend._raw_source_query("command", missing_id, offset=0, limit=12)

    def test_actual_readonly_flock_finishes_before_lazy_writer_opens(self):
        self.policy.activate(self.ref)
        backend = self.backend()
        identity, _ = self.prepare_previous_source(True)
        started, release = threading.Event(), threading.Event()
        original = raw_port.read_source
        errors, outputs = [], {}
        def held_read(*args, **kwargs):
            started.set()
            if not release.wait(5): raise TimeoutError("reader release")
            return original(*args, **kwargs)
        def read():
            try: outputs["read"] = backend._raw_source_query("command", identity, offset=0, limit=12)
            except Exception as exc: errors.append(exc)
        def open_writer():
            try: outputs["writer"] = backend._new_raw_port()
            except Exception as exc: errors.append(exc)
        with patch.object(raw_port, "read_source", held_read):
            reader = threading.Thread(target=read); reader.start()
            self.assertTrue(started.wait(5))
            writer = threading.Thread(target=open_writer); writer.start()
            try: self.assertIsNone(backend._owned_segmented_store)
            finally:
                release.set(); reader.join(5); writer.join(5)
            self.assertFalse(reader.is_alive()); self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outputs["read"]["status"], "available")
        self.assertIs(outputs["writer"], backend.segmented_raw_port)


if __name__ == "__main__": unittest.main()
