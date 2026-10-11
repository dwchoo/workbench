"""DVL-04/05: 실제 backend factory의 포트 전달만 검사한다."""

import threading
import unittest
from types import SimpleNamespace

from workbench.app.recovery import AdmissionHold, METADATA_HOLD
from workbench.backend.service import Backend
from workbench.storage.log_raw.admission import MetadataAdmissionGate
from workbench.workflow.run import WorkflowRun


class FactoryCompositionTests(unittest.TestCase):
    def test_shared_gate_and_metadata_fault_callback_hold_new_work(self):
        backend = Backend.__new__(Backend)
        gate = MetadataAdmissionGate()
        backend.automation_loop = SimpleNamespace(metadata=gate)
        backend.bridge = object()
        backend.raw_logs = None
        backend.subject_log = object()  # 주입 포트만; 활성 DB를 만들지 않는다.
        backend.hold = AdmissionHold()
        backend._metadata_lock = threading.Lock()
        backend._metadata_failures = {}
        workflow = backend._make_workflow(object())
        self.assertIs(workflow.metadata_admission, gate)
        self.assertIs(workflow.subject_log, backend.subject_log)
        workflow.metadata_outcome("condition_evidence", OSError("fixture persistence fault"))
        self.assertFalse(gate.automatic_runs_allowed)
        self.assertEqual(backend.hold.reason_for(None), METADATA_HOLD)
        self.assertIn("condition_evidence", backend._metadata_failures)

    def test_collection_fault_reports_to_shared_backend_port_without_stopping_collector(self):
        backend = Backend.__new__(Backend)
        gate = MetadataAdmissionGate()
        backend.automation_loop = SimpleNamespace(metadata=gate)
        backend.hold = AdmissionHold()
        backend._metadata_lock = threading.Lock()
        backend._metadata_failures = {}
        run = WorkflowRun.__new__(WorkflowRun)
        run._evidence_error = None
        run._record = {}
        run.metadata_admission = gate
        run.metadata_outcome = backend._metadata_event
        run._metadata_failure(OSError("fixture persistence fault"))
        self.assertEqual(run._evidence_error, "OSError")
        self.assertFalse(gate.automatic_runs_allowed)
        self.assertEqual(backend.hold.reason_for(None), METADATA_HOLD)


if __name__ == "__main__":
    unittest.main()
