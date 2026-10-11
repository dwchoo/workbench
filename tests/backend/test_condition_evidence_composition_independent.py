"""Independent DVL-04 C-D46 check across the backend factory and real collector."""

from __future__ import annotations

from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from tests.workflow import test_run as runtime_fixture
from workbench.app.recovery import AdmissionHold, METADATA_HOLD
from workbench.backend.service import Backend
from workbench.storage.log_raw.admission import MetadataAdmissionGate
from workbench.workflow import TaskWorkflow, WorkflowHeld


class IndependentBackendConditionComposition(unittest.TestCase):
    setUp = runtime_fixture.WorkflowRuntimeTests.setUp
    approved_task = runtime_fixture.WorkflowRuntimeTests.approved_task

    def test_factory_shared_metadata_fault_holds_next_auto_run_not_active_collection(self) -> None:
        backend = Backend.__new__(Backend)
        gate = MetadataAdmissionGate()
        backend.automation_loop = SimpleNamespace(metadata=gate, paused=lambda: False)
        backend.bridge = object()  # no OMP connection; local mailbox below is a controlled fixture
        backend.raw_logs = None
        backend.subject_log = None
        backend.hold = AdmissionHold()
        backend._metadata_lock = threading.Lock()
        backend._metadata_failures = {}
        backend._metadata_retry_at = None
        supplied = backend._make_workflow(self.repository)
        self.assertIs(supplied.metadata_admission, gate)
        self.assertIs(supplied.metadata_outcome.__self__, backend)
        workflow = TaskWorkflow(self.repository, self.mailbox,
                                metadata_admission=supplied.metadata_admission,
                                metadata_outcome=supplied.metadata_outcome,
                                automation_source=lambda: runtime_fixture.AUTOMATION)
        task = self.approved_task("printf PASS; printf PASS > outcome.txt")
        run = workflow.start(task, 1, worktree_path=self.root / "active-under-proof-fault",
                             artifacts_root=self.artifacts, automation=runtime_fixture.AUTOMATION)
        self.addCleanup(run.close)
        with patch.object(self.repository, "record_condition_proof", side_effect=OSError("fixture proof disk fault")):
            collected = run.collect(timeout=8)
        self.assertTrue(collected["exit_confirmed"], "metadata fault must not stop an ongoing experiment")
        self.assertFalse(gate.automatic_runs_allowed)
        self.assertEqual(backend.hold.reason_for(None), METADATA_HOLD)
        self.assertIn("condition_evidence", backend._metadata_failures)
        self.assertEqual(run.judge()["worker_judgment"]["judgment"], "indeterminate")
        next_task = self.approved_task("printf PASS > outcome.txt")
        with self.assertRaises(WorkflowHeld):
            workflow.start(next_task, 1, worktree_path=self.root / "new-auto-denied",
                           artifacts_root=self.artifacts, automation=runtime_fixture.AUTOMATION)
        self.assertFalse((self.root / "new-auto-denied").exists())


if __name__ == "__main__":
    unittest.main()
