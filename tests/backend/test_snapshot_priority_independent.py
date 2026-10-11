"""Independent backend refresh checks for writer lease configuration."""

import fcntl
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import patch

from workbench.backend.service import Backend
from workbench.storage.important_log import ImportantLogCatalog


class IndependentBackendSnapshotPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "catalog"
        self.backend = Backend.__new__(Backend)
        self.backend.layout = SimpleNamespace(important_logs=self.directory)
        self.backend.important_writer_lease_timeout_s = 0.6
        self.backend._important_storage_at = 0.0
        self.backend.important_catalog = None
        self.backend.subject_log = None
        self.backend.flow = None
        self.backend.terminal = None
        self.backend._important_workflows = weakref.WeakSet()
        self.backend._important_capture_unknown = False
        self.backend.important_storage_status = {"state": "unknown"}

    def _refresh_under_reader_lease(self):
        with (self.directory / "maintenance.lock").open("rb") as held:
            fcntl.flock(held, fcntl.LOCK_EX)

            def release():
                time.sleep(0.2)
                fcntl.flock(held, fcntl.LOCK_UN)

            thread = threading.Thread(target=release)
            thread.start()
            try:
                started = time.monotonic()
                self.backend._refresh_important_storage()
                elapsed = time.monotonic() - started
            finally:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(elapsed, 0.16)
        self.assertLess(elapsed, 0.8)

    def test_initial_refresh_reconnects_sink_after_reader_releases_lease(self):
        ImportantLogCatalog(self.directory, create=True)
        self._refresh_under_reader_lease()
        self.assertIsNotNone(self.backend.important_catalog)
        self.assertIsNotNone(self.backend.subject_log)
        self.assertEqual(self.backend.important_catalog.writer_lease_timeout_s, 0.6)
        self.assertEqual(self.backend.important_storage_status["state"], "ready")
        self.assertEqual(self.backend.important_storage_status["active"],
                         self.backend.important_catalog.active_uuid())

    def test_existing_catalog_refresh_waits_for_reader_before_health(self):
        self.backend.important_catalog = ImportantLogCatalog(
            self.directory, create=True, writer_lease_timeout_s=0.6)
        self._refresh_under_reader_lease()
        self.assertEqual(self.backend.important_storage_status["state"], "ready")
        self.assertEqual(self.backend.important_storage_status["active"],
                         self.backend.important_catalog.active_uuid())

    def test_actual_backend_open_passes_configured_wait_to_catalog(self):
        from tests.backend import test_omp_home as home_fixture

        fixture = home_fixture.BackendHomeTests(
            "test_both_panes_and_checks_use_the_home_and_no_user_config_is_read")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.user_store()
        env = {**fixture.env, "FAKE_RECORD": str(fixture.record), "OMP_PROFILE": "work"}
        plan = home_fixture.LaunchPlan(
            home_fixture.ShellChoice("bash", "/usr/bin/bash"), str(fixture.fake),
            "omp/18.4.5", "/x/bridge.ts")
        backend = Backend(home_fixture.DataLayout(fixture.data), plan,
                          project_dir=str(fixture.project), environment=env,
                          important_writer_lease_timeout_s=0.6)
        self.addCleanup(lambda: self.assertTrue(backend._close()["verified"]))
        catalog_class = ImportantLogCatalog
        options_seen = []

        def real_catalog(directory, **options):
            options_seen.append(options)
            return catalog_class(directory, **options)

        with patch("workbench.backend.service.ImportantLogCatalog", side_effect=real_catalog):
            backend._open()
        self.assertTrue(options_seen)
        self.assertTrue(options_seen[0]["create"])
        self.assertEqual(options_seen[0]["writer_lease_timeout_s"], 0.6)
        self.assertIsNotNone(backend.subject_log)
        self.assertEqual(backend.important_storage_status["state"], "ready")


if __name__ == "__main__":
    unittest.main()
