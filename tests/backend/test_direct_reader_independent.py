"""Backend storage refresh reports direct-reader lease contention and recovers."""

from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
import weakref

from workbench.backend.service import Backend
from workbench.storage.important_log import ImportantLogCatalog, ImportantLogReader


class IndependentBackendDirectReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "catalog"
        self.catalog = ImportantLogCatalog(self.directory, create=True)
        self.reader = ImportantLogReader(self.directory)
        self.backend = Backend.__new__(Backend)
        self.backend.layout = SimpleNamespace(important_logs=self.directory)
        self.backend.important_writer_lease_timeout_s = 0.1
        self.backend.important_catalog = None
        self.backend.subject_log = None
        self.backend.flow = None
        self.backend.terminal = None
        self.backend._important_workflows = weakref.WeakSet()
        self.backend._important_capture_unknown = False
        self.backend._important_storage_at = 0.0
        self.backend.important_storage_status = {"state": "unknown"}

    def _refresh_now(self):
        self.backend._important_storage_at = time.monotonic() - 31
        self.backend._refresh_important_storage()

    def test_initial_and_existing_refresh_report_busy_then_recover(self):
        identity = self.catalog.active_uuid()
        with self.reader._view(identity):
            self._refresh_now()
            self.assertIsNone(self.backend.important_catalog)
            self.assertEqual(self.backend.important_storage_status["state"], "unavailable")
            self.assertEqual(self.backend.important_storage_status["reason"], "OperationalError")
        self._refresh_now()
        self.assertIsNotNone(self.backend.important_catalog)
        sink = self.backend.subject_log
        self.assertIsNotNone(sink)
        self.assertEqual(self.backend.important_storage_status["state"], "ready")
        with self.reader._view(identity):
            self._refresh_now()
            self.assertEqual(self.backend.important_storage_status["state"], "unavailable")
            self.assertIs(self.backend.subject_log, sink)
        self._refresh_now()
        self.assertEqual(self.backend.important_storage_status["state"], "ready")
        self.assertEqual(len(list(self.directory.glob("*.sqlite3"))), 1)


if __name__ == "__main__":
    unittest.main()
