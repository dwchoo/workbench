"""DVL-04 fixture-only observation and proof checks (no OMP or model)."""

from hashlib import sha256
from unittest import TestCase
from unittest.mock import patch

import test_run as fixture
from workbench.storage.log_raw.admission import MetadataAdmissionGate
from workbench.workflow import TaskWorkflow, WorkflowHeld
from workbench.workflow.run import _ExactByteMatcher


class ConditionEvidenceTests(TestCase):
    setUp = fixture.WorkflowRuntimeTests.setUp
    approved_task = fixture.WorkflowRuntimeTests.approved_task

    def _start(self, command, name, *, workflow=None):
        task_id = self.approved_task(command)
        run = (workflow or self.workflow).start(
            task_id, 1, worktree_path=self.root / name,
            artifacts_root=self.artifacts, automation=fixture.AUTOMATION,
        )
        self.addCleanup(run.close)
        return run

    def test_exact_bytes_split_and_long_match_offsets(self):
        pattern = b"x" * 5000 + b"needle"
        matcher = _ExactByteMatcher(pattern)
        segments = []
        for chunk in (b"pre" + pattern[:2000], pattern[2000:4500], pattern[4500:] + b"post"):
            segments.extend(matcher.feed(chunk))
        segments.extend(matcher.finish())
        self.assertEqual(b"".join(segment[1] for segment in segments), b"pre" + pattern + b"post")
        self.assertEqual([(s, len(data)) for s, data, matched in segments if matched], [(3, len(pattern))])
        self.assertEqual([s for s, _, _ in segments], [0, 3, 3 + len(pattern)])

    def test_matched_proof_is_durable_and_raw_append_loss_is_not_collector_gap(self):
        run = self._start("printf 'PASS\\n'; printf PASS > outcome.txt", "matched-write-loss")

        class FailingRawStore:
            def append(self, _run_id, _data):
                raise OSError("fixture raw append unavailable")

        run.raw_store = FailingRawStore()
        self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
        proofs = self.repository.get_condition_proofs(run.run_id)
        self.assertEqual(len(proofs), 1)
        self.assertEqual(proofs[0]["condition_digest"], sha256(b"PASS").hexdigest())
        self.assertEqual((proofs[0]["match_start"], proofs[0]["match_end"]), (0, 4))
        self.assertEqual(proofs[0]["full_match_digest"], sha256(b"PASS").hexdigest())
        self.assertEqual(run.judge()["worker_judgment"]["judgment"], "success")

    def test_complete_negative_survives_raw_append_loss(self):
        run = self._start("printf 'WRONG\\n'; printf PASS > outcome.txt", "negative-write-loss")

        class FailingRawStore:
            def append(self, _run_id, _data):
                raise OSError("fixture raw append unavailable")

        run.raw_store = FailingRawStore()
        self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
        proof = self.repository.get_condition_proofs(run.run_id)[0]
        self.assertFalse(proof["matched"])
        self.assertTrue(proof["observation_complete"])
        self.assertEqual(run.judge()["worker_judgment"]["judgment"], "failure")

    def test_late_collector_gap_invalidates_negative_current_view(self):
        run = self._start("printf PASS > outcome.txt", "negative-late-gap")
        self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
        historical = self.repository.get_condition_proofs(run.run_id)
        self.assertEqual(len(historical), 1)
        self.assertEqual(historical[0]["observation_complete"], 1)
        self.repository.record_log_source(
            {"schemaVersion": 1, "kind": "task", "id": run.task_id},
            {"schemaVersion": 1, "source": run._source_ref, "start": 0, "end": 1,
             "digest": sha256(b"?").hexdigest()}, status="collector_gap",
        )
        current = self.repository.get_condition_proofs(run.run_id)
        self.assertEqual(current[0]["observation_complete"], 0)
        self.assertEqual(current[0]["invalidation_reason"], "coverage_source_gap")
        evidence = run.judge()["worker_judgment"]
        self.assertEqual(evidence["judgment"], "indeterminate")
        self.assertIn("condition_observation_incomplete", evidence["reasons"])

    def test_proof_write_failure_holds_next_run_but_current_collection_finishes(self):
        gate = MetadataAdmissionGate()
        workflow = TaskWorkflow(self.repository, self.mailbox, metadata_admission=gate)
        run = self._start("printf 'PASS\\n'; printf PASS > outcome.txt", "proof-fault", workflow=workflow)
        with patch.object(self.repository, "record_condition_proof", side_effect=OSError("fixture metadata fault")):
            collected = run.collect(timeout=8)
        self.assertTrue(collected["exit_confirmed"])
        self.assertFalse(gate.automatic_runs_allowed)
        self.assertEqual(run.judge()["worker_judgment"]["judgment"], "indeterminate")
        next_task = self.approved_task("printf PASS > outcome.txt")
        with self.assertRaises(WorkflowHeld):
            workflow.start(next_task, 1, worktree_path=self.root / "held-next",
                           artifacts_root=self.artifacts, automation=fixture.AUTOMATION)
