"""WR-05 records extension and authenticated host identity boundary, without OMP/provider."""
from pathlib import Path
import subprocess
import unittest
from uuid import uuid4

from tests.backend.work_records import test_service as support

ROOT = Path(__file__).resolve().parents[2]


class RecordsExtensionTests(unittest.TestCase):
    def test_stand_in_omp_records_tool_over_actual_uds(self):
        result = subprocess.run(
            ["node", "--experimental-strip-types", "--no-warnings", "--test",
             str(ROOT / "tests/bridge/work_records.test.ts")],
            cwd=ROOT, capture_output=True, text=True, timeout=35, check=False)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])

    def test_authenticated_peer_session_generation_and_model_identity_are_enforced(self):
        # Reuse only the disposable real-server harness, not its completed tests.
        case = support.RecordToolServiceTests("test_manager_create_saves_exact_body_in_host_project_without_task_db")
        case.setUp()
        self.addCleanup(case.doCleanups)
        create = support.arguments("create", title="identity sentinel", body="host-bound original\n")
        self.assertEqual(case.call("manager", create, sessionId=str(uuid4())),
                         {"status": "rejected", "reason": "session_mismatch"})
        self.assertEqual(case.call("worker", create, generation=2),
                         {"status": "rejected", "reason": "session_mismatch"})
        for injected in ({"actor": "worker"}, {"root": "/other/project"}, {"session_id": str(uuid4())}):
            with self.subTest(injected=list(injected)):
                self.assertEqual(case.call("manager", {**create, **injected}),
                                 {"status": "rejected", "reason": "invalid_arguments"})
        self.assertFalse(case.backend.layout.records.exists())
        result = case.call("worker", create)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["record"]["actor"], "worker")
