from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from workbench.backend.service import Backend
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog


class ImportantBackendHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(self.temp.cleanup)
        self.catalog = ImportantLogCatalog(Path(self.temp.name) / "important", create=True)
        self.backend = Backend.__new__(Backend)
        self.backend.important_catalog = self.catalog
        self.backend.subject_log = ActiveSubjectLog(self.catalog)
        self.backend.important_storage_status = {"state": "not_open"}
        self.backend._important_storage_at = 0.0
        self.backend.flow = SimpleNamespace(subject_capture_unknown=False)

    def test_backend_consumes_health_and_storage_probe_without_core_hold(self):
        self.backend._refresh_important_storage()
        result = self.backend.important_storage_status
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["active"], self.catalog.active_uuid())
        self.assertEqual(result["archiveCount"], 0)
        self.assertTrue(result["metadataOutsideBudget"]["outsideBudget"])
        self.backend.subject_log.last_failure = "ImportantLogStoreError"
        self.backend._important_storage_at = 0.0
        self.backend._refresh_important_storage()
        result = self.backend.important_storage_status
        self.assertEqual(result["state"], "capture_unknown")
        self.assertEqual(result["recovery"]["state"], "healthy")
        self.assertFalse(hasattr(self.backend, "hold"))

    def test_confirmed_capture_loss_stays_visible_after_storage_recovers(self):
        self.backend._mark_important_capture_unknown()
        self.backend._refresh_important_storage()
        self.assertEqual(self.backend.important_storage_status["state"], "capture_unknown")
        self.assertTrue(self.backend.important_storage_status["subjectCaptureUnknown"])
        self.assertIsNone(self.backend.important_storage_status["error"])
        self.backend._important_storage_at = 0.0
        self.backend._refresh_important_storage()
        self.assertEqual(self.backend.important_storage_status["state"], "capture_unknown")


if __name__ == "__main__":
    unittest.main()
