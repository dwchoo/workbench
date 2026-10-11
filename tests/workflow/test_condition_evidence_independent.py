"""Independent DVL-04 observations through a real temporary Git/Bash run."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import unittest

from tests.workflow import test_run as runtime_fixture
from workbench.storage.log_raw.store import RawLogStore
from workbench.tasks.repository import TaskRepository
from workbench.workflow import TaskWorkflow
from workbench.workflow.run import _ExactByteMatcher


class IndependentConditionEvidence(unittest.TestCase):
    setUp = runtime_fixture.WorkflowRuntimeTests.setUp
    approved_task = runtime_fixture.WorkflowRuntimeTests.approved_task

    def _run(self, command: str, name: str, *, per_run_limit: int = 0):
        store = RawLogStore(self.root / f"raw-{name}", per_run_limit=per_run_limit,
                            project_limit=per_run_limit)
        self.addCleanup(store.close)
        workflow = TaskWorkflow(self.repository, self.mailbox, raw_store=store)
        task = self.approved_task(command)
        run = workflow.start(task, 1, worktree_path=self.root / name,
                             artifacts_root=self.artifacts, automation=runtime_fixture.AUTOMATION)
        self.addCleanup(run.close)
        return run, store

    def test_overlap_prefix_pattern_is_exact_across_every_chunk_boundary(self) -> None:
        pattern = b"ABABA"
        for boundary in range(1, len(pattern)):
            with self.subTest(boundary=boundary):
                matcher = _ExactByteMatcher(pattern)
                observed = []
                observed.extend(matcher.feed(b"Q" + pattern[:boundary]))
                observed.extend(matcher.feed(pattern[boundary:] + b"Z"))
                observed.extend(matcher.finish())
                self.assertEqual([(offset, data) for offset, data, matched in observed if matched],
                                 [(1, pattern)])
                self.assertEqual(b"".join(data for _, data, _ in observed), b"QABABAZ")
        no_match = _ExactByteMatcher(pattern)
        negative = no_match.feed(b"ABABXABAB") + no_match.finish()
        self.assertFalse(any(matched for _, _, matched in negative))

    def test_long_match_proof_committed_before_judge_survives_raw_quota(self) -> None:
        pattern = "0" * 5000 + "NEEDLE"
        execution = {"source": str(self.source), "commit": self.commit,
                     "command": "printf '%05000dNEEDLE' 0; printf PASS > outcome.txt",
                     "criteria": {"log_contains": pattern, "result_file": "outcome.txt",
                                  "result_contains": "PASS"},
                     "environment": {"PATH": "/usr/bin:/bin", "TERM": "xterm"}, "shell": "bash"}
        task = self.repository.create_task({"goal": "large exact observed pattern", "execution": execution})
        self.repository.approve_scope(task, 1, {"execution": execution, "paths": ["outcome.txt"]})
        self.repository.proceed(task, 1, "approved fixture")
        store = RawLogStore(self.root / "raw-long", per_run_limit=32, project_limit=32)
        self.addCleanup(store.close)
        workflow = TaskWorkflow(self.repository, self.mailbox, raw_store=store)
        run = workflow.start(task, 1, worktree_path=self.root / "long-pattern",
                             artifacts_root=self.artifacts, automation=runtime_fixture.AUTOMATION)
        self.addCleanup(run.close)
        self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
        status = store.status(run.run_id)
        self.assertTrue(status.truncated)
        self.assertLessEqual(status.stored_bytes, 32)
        proof = self.repository.get_condition_proofs(run.run_id)[0]
        self.assertEqual((proof["task_id"], proof["revision"], proof["run_id"]), (task, 1, run.run_id))
        self.assertEqual(proof["condition_digest"], sha256(pattern.encode()).hexdigest())
        self.assertEqual(proof["match_end"] - proof["match_start"], len(pattern))
        self.assertEqual(proof["full_match_digest"], sha256(pattern.encode()).hexdigest())
        self.assertLessEqual(len(proof["excerpt"]), 4096)
        self.assertEqual(len(self.mailbox.messages), 1, "proof must precede the judgment/report")
        source = self.repository.get_log_sources({"schemaVersion": 1, "kind": "task", "id": task})
        self.assertIn(proof["source_id"], {item["source_id"] for item in source})
        with TaskRepository(self.root / "metadata.sqlite3") as reopened:
            self.assertEqual(reopened.get_condition_proofs(run.run_id)[0]["proof_id"], proof["proof_id"])
        judged = run.judge()["worker_judgment"]
        self.assertEqual((judged["judgment"], judged["condition_evidence"]["matched_proof_id"]),
                         ("success", proof["proof_id"]))

    def test_raw_zero_quota_negative_requires_current_gapfree_coverage(self) -> None:
        complete, store = self._run("printf WRONG; printf PASS > outcome.txt", "complete-negative")
        self.assertTrue(complete.collect(timeout=8)["exit_confirmed"])
        self.assertEqual(store.status(complete.run_id).stored_bytes, 0)
        proof = self.repository.get_condition_proofs(complete.run_id)[0]
        self.assertEqual((proof["matched"], proof["observation_complete"]), (0, 1))
        # A real observed byte range with an unrelated condition digest is not
        # evidence for the approved pattern of this Run.
        other = self.repository.get_log_sources({"schemaVersion": 1, "kind": "task", "id": complete.task_id})[0]
        self.repository.record_condition_proof(
            complete.task_id, 1, complete.run_id, sha256(b"different condition").hexdigest(),
            matched=True, observation_complete=False, source_id=other["source_id"],
            match_start=other["start"], match_end=other["end"],
            full_match_digest=other["digest"])
        self.assertEqual(complete.judge()["worker_judgment"]["judgment"], "failure")

        late, late_store = self._run("printf WRONG; printf PASS > outcome.txt", "late-gap-negative")
        self.assertTrue(late.collect(timeout=8)["exit_confirmed"])
        self.assertEqual(late_store.status(late.run_id).stored_bytes, 0)
        original = self.repository.get_condition_proofs(late.run_id)[0]
        self.assertTrue(original["observation_complete"])
        self.repository.record_log_source(
            {"schemaVersion": 1, "kind": "task", "id": late.task_id},
            {"schemaVersion": 1, "source": {**late._source_ref, "collectorEpoch": 2}, "start": 0, "end": 1,
             "digest": sha256(b"?").hexdigest()}, status="collector_gap")
        current = self.repository.get_condition_proofs(late.run_id)[0]
        self.assertEqual(current["proof_id"], original["proof_id"])
        self.assertEqual(current["observation_complete"], 0)
        judgment = late.judge()["worker_judgment"]
        self.assertEqual(judgment["judgment"], "indeterminate")
        self.assertIn("condition_observation_incomplete", judgment["reasons"])

    def test_confirmed_nonzero_exit_and_result_mutation_are_separate_failure_facts(self) -> None:
        failing, _ = self._run("printf WRONG; printf PASS > outcome.txt; exit 7", "nonzero")
        self.assertTrue(failing.collect(timeout=8)["exit_confirmed"])
        self.repository.record_log_source(
            {"schemaVersion": 1, "kind": "task", "id": failing.task_id},
            {"schemaVersion": 1, "source": failing._source_ref, "start": 0, "end": 1,
             "digest": sha256(b"?").hexdigest()}, status="collector_gap")
        evidence = failing.judge()["worker_judgment"]
        self.assertEqual((evidence["judgment"], evidence["reasons"]), ("failure", ["nonzero_exit"]))
        changed, _ = self._run("printf PASS; printf PASS > outcome.txt", "result-mutated")
        self.assertTrue(changed.collect(timeout=8)["exit_confirmed"])
        result_file = changed.worktree.path / "outcome.txt"
        result_file.write_bytes(b"different result")
        evidence = changed.judge()["worker_judgment"]
        self.assertEqual(evidence["judgment"], "indeterminate")
        self.assertIn("result_file", {row["source"] for row in evidence["evidence_errors"]})


if __name__ == "__main__":
    unittest.main()
