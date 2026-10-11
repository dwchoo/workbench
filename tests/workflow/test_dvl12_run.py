"""Actual approved temporary Git/Bash run with disabled segmented collection."""

from hashlib import sha256
import unittest

from tests.workflow import test_run as fixture
from workbench.storage.log_raw.port import DisabledSegmentedRawPort
from workbench.storage.log_raw.segments import SegmentedRawStore
from workbench.workflow import TaskWorkflow


class Dvl12WorkflowRun(unittest.TestCase):
    setUp = fixture.WorkflowRuntimeTests.setUp
    approved_task = fixture.WorkflowRuntimeTests.approved_task

    def test_proof_before_eviction_and_descriptor_never_full_artifact(self):
        store = SegmentedRawStore(self.root / "raw-v2", segment_limit=512, segment_count=2)
        self.addCleanup(store.close)
        workflow = TaskWorkflow(self.repository, self.mailbox,
                                segmented_raw_port=DisabledSegmentedRawPort(store))
        task = self.approved_task("printf '%01000dPASS' 0; printf PASS > outcome.txt")
        run = workflow.start(task, 1, worktree_path=self.root / "segmented-run",
                             artifacts_root=self.artifacts, automation=fixture.AUTOMATION)
        self.addCleanup(run.close)
        self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
        self.assertEqual(run._record["log_format"], "segmented-v1")
        self.assertNotIn(b"PASS", run.raw_log.read_bytes())
        self.assertNotIn("sha256", run._record["raw_log_collected"])
        self.assertGreater(run._record["raw_log_collected"]["source_status"]["evicted_bytes"], 0)
        with self.assertRaisesRegex(ValueError, "source-relative"):
            run._read_raw()
        proof = next(proof for proof in self.repository.get_condition_proofs(run.run_id) if proof["matched"])
        self.assertEqual(proof["condition_digest"], sha256(b"PASS").hexdigest())
        judged = run.judge()["worker_judgment"]
        self.assertEqual(judged["judgment"], "success")
        self.assertEqual(judged["condition_evidence"]["matched_proof_id"], proof["proof_id"])
        self.assertEqual(judged["log_format"], "segmented-v1")
        self.assertIsNone(judged["raw_log_sha256"])
        self.assertIsNone(judged["raw_log_excerpt"])


if __name__ == "__main__":
    unittest.main()
