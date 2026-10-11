"""DVL-12: real private shell output remains evidence through raw storage failure."""

import errno
from hashlib import sha256
import json
import unittest
from unittest.mock import patch

from tests.workflow import test_run as fixture
from workbench.storage.log_raw.port import DisabledSegmentedRawPort
from workbench.storage.log_raw.segments import SegmentedRawStore
from workbench.workflow import TaskWorkflow


class DisabledRawWorkflowRuntime(unittest.TestCase):
    setUp = fixture.WorkflowRuntimeTests.setUp
    approved_task = fixture.WorkflowRuntimeTests.approved_task

    def test_raw_storage_failure_cannot_erase_observed_proof_or_end_the_shell(self):
        store = SegmentedRawStore(self.root / "raw-v2", segment_limit=512, segment_count=2)
        self.addCleanup(store.close)
        port = DisabledSegmentedRawPort(store)
        workflow = TaskWorkflow(self.repository, self.mailbox, segmented_raw_port=port)
        task = self.approved_task("printf 'PASS\\n'; printf PASS > outcome.txt")
        run = workflow.start(task, 1, worktree_path=self.root / "fault-run",
                             artifacts_root=self.artifacts, automation=fixture.AUTOMATION)
        self.addCleanup(run.close)
        with patch.object(store, "_append_frame", side_effect=OSError(errno.ENOSPC, "fixture full")):
            collected = run.collect(timeout=8)
        self.assertTrue(collected["exit_confirmed"], collected)
        self.assertEqual(collected["exit_status"], 0)
        self.assertEqual(collected["log_format"], "segmented-v1")
        self.assertGreater(collected["raw_log_status"]["missing_bytes"], 0)
        self.assertEqual(collected["raw_log_collected"]["log_format"], "segmented-v1")
        self.assertNotIn(b"PASS", run.raw_log.read_bytes(), "descriptor must not contain shell output")

        page = port.read(run.raw_descriptor, limit=512)
        self.assertIn("storage_gap", {item["reason"] for item in page["read"]["missing"]})
        matched = [proof for proof in self.repository.get_condition_proofs(run.run_id)
                   if proof["matched"]]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["condition_digest"], sha256(b"PASS").hexdigest())
        judgment = run.judge()["worker_judgment"]
        self.assertEqual(judgment["judgment"], "success", judgment)
        self.assertEqual(judgment["condition_evidence"]["matched_proof_id"], matched[0]["proof_id"])
        self.assertIsNone(judgment["raw_log_sha256"])
        self.assertIsNone(judgment["raw_log_excerpt"])
        self.assertIn("segmented raw source incomplete", " ".join(judgment["unknowns"]))
        print("DVL12_WORKFLOW_RUNTIME=" + json.dumps({
            "observed": collected["raw_log_status"]["observed_bytes"],
            "retained": collected["raw_log_status"]["stored_bytes"],
            "missing": collected["raw_log_status"]["missing_bytes"],
            "matched_proofs": len(matched), "exit_confirmed": collected["exit_confirmed"],
            "judgment": judgment["judgment"], "configured_model_clients": 0,
        }, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
