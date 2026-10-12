"""Independent display-boundary tests; synthetic records are not Q01 evidence."""
import sys
import unittest
from unittest.mock import Mock

from tests.backend import test_summary_compatibility_independent as validator_fixture
from workbench.backend.service import Backend
from workbench.backend.summary_compatibility import RECORD_LIMIT


class CompatibilityObservationIndependentTests(unittest.TestCase):
    def test_snapshot_missing_observation_port_is_unknown_without_live_fallback(self):
        # An older external port has no display getter; it must not trigger a
        # live check. Arbitrary exceptions from a pure cache getter are not its contract.
        forbidden = Mock(side_effect=AssertionError("display invoked a live operation"))
        port = type("MissingObservation", (), {
            "status": forbidden, "admission": forbidden,
            "verify_scripted": forbidden, "take_notice": forbidden})()
        backend = Backend.__new__(Backend)
        backend.summary_compatibility = port
        observed = backend._summary_compatibility_observation()
        self.assertEqual(observed["state"], "unavailable")
        self.assertIsNone(observed["lastCheckedAt"])
        self.assertNotIn("admitted", observed)
        forbidden.assert_not_called()

    def test_private_descriptor_copy_failure_stays_inside_public_status_boundary(self):
        # Reuse the complete validator-only fixture; the public getter supplies
        # its descriptor directly, without an artificial deepcopy before status.
        fixture = validator_fixture.PersistedCompatibilityIndependentTests(
            "test_changed_log_bytes_must_invalidate_record")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record = fixture.record()
        fixture.persist(record)
        self.assertTrue(fixture.service.admission(), "complete synthetic baseline")
        self.assertTrue(fixture.service.verify_reference(record["id"]))
        original_limit = sys.getrecursionlimit()
        nested = "extra"
        for _ in range(600):
            nested = [nested]
        record["omp"]["extra"] = nested
        fixture.persist(record)
        self.assertLessEqual((fixture.directory / (record["id"] + ".json")).stat().st_size,
                             RECORD_LIMIT)
        # Also cover a normal current descriptor without the old extra field:
        # a drift result still contains verified_omp from the private record.
        for shape, current in (("matched_extra", record["omp"]),
                               ("current_without_old_extra", fixture.omp)):
            with self.subTest(current=shape):
                fixture.service.omp_identity = lambda: current
                status = fixture.service.status()
                self.assertEqual(status["state"], "unavailable")
                self.assertEqual(status["reason"], "compatibility_record_invalid")
                self.assertFalse(status["admitted"])
                cached = fixture.service.observed_status()
                self.assertEqual(cached["state"], "unavailable")
                self.assertEqual(cached["reason"], "compatibility_record_invalid")
                self.assertIsNotNone(cached["lastCheckedAt"])
                self.assertNotIn("admitted", cached)
                cached["reason"] = "caller mutation"
                self.assertEqual(fixture.service.observed_status()["reason"],
                                 "compatibility_record_invalid")
                self.assertFalse(fixture.service.admission())
        self.assertEqual(sys.getrecursionlimit(), original_limit)


if __name__ == "__main__":
    unittest.main()
