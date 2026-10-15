"""Host writer and pre-send ownership regressions; real private stores, Native0."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.backend import test_summary_scheduler as fixtures
from workbench.backend import service
from workbench.backend.paths import DataLayout
from workbench.tasks.repository import TaskRepository


class FixedWriterIndependentTests(unittest.TestCase):
    def test_default_backend_readback_cannot_supply_writer_paths(self):
        fixture = fixtures.SchedulerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        backend = service.Backend.__new__(service.Backend)
        backend.layout = DataLayout(fixture.root)
        backend.summary_runtime = None
        backend.bridge = SimpleNamespace(peer=Mock(return_value=SimpleNamespace(session_id="actual-parent", generation=7)))
        metadata = {"epoch": "actual-readback", "childSelection": {"writer": "/unowned"}}
        supplied = {"metadata": metadata, "expected_peer": ("actual-parent", 7),
                    "instructions": "owned task context", "writer": {"python": "/unowned/python",
                    "moduleRoot": "/unowned/module", "dataRoot": "/unowned/data"}}
        backend.summary_adapter = SimpleNamespace(read_runtime=Mock(return_value=deepcopy(supplied)))
        with patch("subprocess.Popen", side_effect=AssertionError("Native forbidden")):
            runtime = backend._summary_runtime()
        backend.summary_adapter.read_runtime.assert_called_once_with(expected_peer=("actual-parent", 7))
        self.assertEqual(runtime["writer"], {"python": sys.executable,
            "moduleRoot": str(Path(service.__file__).resolve().parents[2]), "dataRoot": str(fixture.root)})
        self.assertEqual(runtime["metadata"], metadata)
        self.assertEqual(runtime["expected_peer"], supplied["expected_peer"])
        self.assertEqual(runtime["instructions"], supplied["instructions"])
        self.assertEqual(supplied["writer"]["python"], "/unowned/python")

    def test_invalid_writer_defers_before_count_reserve_or_send_and_input_remains_pending(self):
        fixture = fixtures.SchedulerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        valid = deepcopy(fixture.runtime["writer"])
        fixture.scheduler.tick()
        fixture.settle()
        pending = None
        for index, bad in enumerate((None, {**valid, "python": "python"},
                                     {**valid, "moduleRoot": 1}, {**valid, "dataRoot": "/tmp/invalid\0root"}), 1):
            with self.subTest(writer=bad):
                if bad is None:
                    fixture.runtime.pop("writer", None)
                else:
                    fixture.runtime["writer"] = bad
                count = fixture.runtime["count_fragments"] = Mock(side_effect=AssertionError("count before writer validation"))
                fixture.now = index * 300
                with patch.object(TaskRepository, "reserve_summary_request", side_effect=AssertionError("reserved invalid writer")), \
                     patch.object(fixture.adapter, "dispatch_outcome", side_effect=AssertionError("sent invalid writer")) as dispatch, \
                     patch("socket.socket", side_effect=AssertionError("socket for invalid writer")), \
                     patch("subprocess.Popen", side_effect=AssertionError("Native forbidden")):
                    fixture.scheduler.tick()
                    status = fixture.settle()
                self.assertEqual((status["state"], status["reason"]), ("deferred", "startup_configuration_unavailable"))
                count.assert_not_called()
                dispatch.assert_not_called()
                self.assertEqual(fixture.rows(), [], "no prepared/sending/unknown row may create a false no-replay gap")
                with fixture.factory() as journal:
                    observed = journal.pending_summary_sources(fixture.subject)
                self.assertTrue(observed)
                pending = observed if pending is None else pending
                self.assertEqual(observed, pending)
        fixture.runtime["writer"] = valid
        fixture.runtime.pop("count_fragments")
        fixture.now = 1500
        with patch.object(fixture.adapter, "dispatch_outcome", wraps=fixture.adapter.dispatch_outcome) as dispatch:
            fixture.scheduler.tick()
            fixture.settle()
        self.assertEqual([row["state"] for row in fixture.rows()], ["stored"])
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(dispatch.call_args.kwargs["writer"], valid)
