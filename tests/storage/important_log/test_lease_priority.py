"""Writer lease wait is explicit and affects acquisition only."""

import fcntl
from pathlib import Path
import tempfile
import threading
import time
import unittest

from workbench.storage.important_log import ImportantLogCatalog


class LeasePriorityTests(unittest.TestCase):
    def test_default_wait_stays_short_and_injected_wait_can_acquire(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "important"
            short = ImportantLogCatalog(directory, create=True)
            longer = ImportantLogCatalog(directory, writer_lease_timeout_s=0.5)
            with (directory / "maintenance.lock").open("rb") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                t0 = time.monotonic()
                with self.assertRaisesRegex(Exception, "busy"):
                    short.active_uuid()
                self.assertLess(time.monotonic() - t0, 0.3)
                timer = threading.Timer(0.2, lambda: fcntl.flock(held, fcntl.LOCK_UN))
                timer.start()
                try:
                    self.assertEqual(longer.active_uuid(), short.catalog_snapshot()["active"])
                finally:
                    timer.join()

    def test_unbounded_or_boolean_timeout_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            for value in (True, -1, float("inf"), float("nan")):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    ImportantLogCatalog(Path(temp) / "important", create=True,
                                        writer_lease_timeout_s=value)


if __name__ == "__main__":
    unittest.main()
