"""SPEC 8.3 / LG-AC-01,22 / DVL-14 p5 actual core pause fixture.

One unchanged pending producer event crosses durable pause, two actual controller
constructions and scheduler reconciliation, then an explicit unbound resume.
Runtime metadata, adapter outcome, compatibility and priority are controlled
external seams; this does not boot Backend/TUI or exercise OMP/model I/O.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import unittest

from tests.backend import test_summary_scheduler_independent as scheduler_fixture
from workbench.app.lifecycle import LifecycleJournal
from workbench.app.recovery import PauseStore
from workbench.backend.automation import AutomationController
from workbench.backend.paths import ensure_private_dir
from workbench.backend.service import Backend


class ActualPauseRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("DVL10_INDEPENDENT_OUTPUT") is None:
            raise unittest.SkipTest("DVL10_INDEPENDENT_OUTPUT required; skipped tests are not a gate pass")

    def test_durable_and_malformed_pause_hold_same_range_until_explicit_resume(self):
        evidence = scheduler_fixture.EVIDENCE
        self.assertTrue(evidence.is_absolute(), "owned evidence must precede module import")
        evidence.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.assertEqual(evidence.stat().st_uid, os.getuid())
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o700)
        fixture = scheduler_fixture.SchedulerBoundaryTests(methodName="runTest")
        controllers, schedulers, closed_controllers = [], [], set()
        observation = {"boundary": "actual_core_components_fixture", "phases": [],
                       "runtime_calls": 0, "body_completed": False}
        initial_threads = set(threading.enumerate())

        def cleanup():
            # Every closer runs even after another closer fails. doCleanups False
            # is recorded and asserted; a body exception remains the primary cause.
            errors = []
            for scheduler in schedulers:
                try:
                    if scheduler.close(timeout=3)["stopped"] is not True:
                        errors.append("scheduler_not_stopped")
                except Exception as error:
                    errors.append("scheduler_close:" + type(error).__name__)
            for controller in controllers:
                if id(controller) in closed_controllers:
                    continue
                try:
                    controller.close(timeout=3)
                    closed_controllers.add(id(controller))
                except Exception as error:
                    errors.append("controller_close:" + type(error).__name__)
            fixture_cleanup = fixture.doCleanups()
            if fixture_cleanup is False:
                errors.append("fixture_doCleanups_returned_false:failure_cause_unknown")
            threads = [thread.name for thread in threading.enumerate()
                       if thread not in initial_threads and thread.is_alive()]
            root_removed = not getattr(fixture, "root", evidence / "absent").exists()
            if threads:
                errors.append("owned_threads_live")
            if not root_removed:
                errors.append("fixture_root_retained")
            observation["cleanup"] = {"fixture_doCleanups": fixture_cleanup,
                "errors": errors, "owned_threads": threads, "owned_processes": 0,
                "owned_sockets": 0, "root_removed": root_removed,
                "pause_store_close": "stateless path wrapper; no close method or persistent handle"}
            print("actual_pause_observation=" + json.dumps(observation, sort_keys=True))
            if observation["body_completed"]:
                self.assertEqual(errors, [], "cleanup failed after successful body")

        self.addCleanup(cleanup)
        fixture.setUp()
        schedulers.append(fixture.scheduler)
        root_stat = fixture.root.stat()
        self.assertEqual((root_stat.st_uid, root_stat.st_mode & 0o777), (os.getuid(), 0o700))
        observation["fixture_root_private_same_uid"] = True
        pause_path = fixture.layout.root / "automation.json"
        backend = Backend.__new__(Backend)

        def runtime():
            observation["runtime_calls"] += 1
            return fixture.runtime

        def controller():
            store = PauseStore(pause_path)
            value = AutomationController(
                bridge=None, database=fixture.layout.tasks,
                journal=LifecycleJournal(fixture.layout.lifecycle_journal), raw=None,
                shell_pane=lambda: None, project_dir=fixture.root,
                artifacts_root=ensure_private_dir(fixture.layout.workflow / "runs"),
                boot_marker=lambda: "independent-private-boot", pause_store=store)
            controllers.append(value)
            backend.automation_loop = value
            return value, store

        def stop_controller(value):
            self.assertTrue(value.wait_idle(timeout=3))
            value.close(timeout=3)
            closed_controllers.add(id(value))
            self.assertEqual(value._transitions, [])
            self.assertIsNone(value._transition)
            self.assertFalse(value._transition_thread and value._transition_thread.is_alive())
            self.assertFalse(value._thread and value._thread.is_alive())

        def recreate_scheduler():
            self.assertTrue(fixture.scheduler.close(timeout=3)["stopped"])
            fixture.scheduler = fixture.make_scheduler(paused=backend._automation_paused, runtime=runtime)
            schedulers.append(fixture.scheduler)
            self.assertFalse(fixture.scheduler._closed)

        def pending():
            with fixture.factory() as journal:
                return deepcopy(journal.pending_summary_sources(fixture.subject))

        def fixed_controls():
            return {"closed": fixture.scheduler._closed, "priority": fixture.priority,
                    "storage_ready": fixture.healthy, "compatibility_admission": fixture.compat.admission(),
                    "dispatch_ready": fixture.dispatch_ready, "active": fixture.active, "revoked": fixture.revoked}

        expected_controls = {"closed": False, "priority": False, "storage_ready": True,
            "compatibility_admission": True, "dispatch_ready": True, "active": True, "revoked": False}
        first, first_store = controller()
        self.assertIs(first.paused(), False)
        self.assertIs(backend._automation_paused(), False)
        recreate_scheduler()
        fixture.cycle(0)  # Real producer/sink event -> journal cursor; not yet due.
        original = pending()
        self.assertEqual(len(original), 1)
        ranges = [deepcopy(record["range"]) for record in original]
        self.assertEqual(ranges[0]["source"]["sourceKind"], "event")
        self.assertEqual(fixture.rows(), [])
        self.assertEqual(fixed_controls(), expected_controls)
        first.request_pause()
        self.assertTrue(first.wait_idle(timeout=3))
        self.assertIs(first.paused(), True)
        self.assertIs(first_store.load(), True)
        self.assertIs(json.loads(pause_path.read_text())["paused"], True)
        self.assertEqual(pause_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(pending(), original)
        stop_controller(first)

        def assert_held(value, store, now, phase):
            self.assertIs(value.paused(), True)
            self.assertIs(value.restored_pause, True)
            self.assertIs(store.load(), True)
            self.assertIs(backend._automation_paused(), True)
            self.assertEqual(fixed_controls(), expected_controls)
            status = fixture.cycle(now)
            self.assertEqual(status["state"], "deferred", status)
            self.assertEqual(observation["runtime_calls"], 0)
            self.assertEqual(fixture.adapter.calls, [])
            self.assertEqual(fixture.rows(), [])
            self.assertEqual(pending(), original)
            self.assertEqual(fixed_controls(), expected_controls)
            observation["phases"].append({"phase": phase, "paused": True,
                "restored": True, "backend_paused": True, "runtime": 0, "adapter": 0,
                "request_rows": 0, "pending_unchanged": True, "status": status["state"]})

        second, second_store = controller()
        recreate_scheduler()  # Actual constructor runs _reconcile against the same DB.
        assert_held(second, second_store, 300, "durable_restore")
        stop_controller(second)
        self.assertTrue(fixture.scheduler.close(timeout=3)["stopped"])
        pause_path.write_text('{"paused":"yes"}', encoding="utf-8")
        os.chmod(pause_path, 0o600)
        third, third_store = controller()
        recreate_scheduler()
        assert_held(third, third_store, 600, "malformed_fail_closed_restore")
        third.request_resume()
        self.assertTrue(third.wait_idle(timeout=3))
        self.assertIs(third.paused(), False)
        self.assertIs(third_store.load(), False)
        self.assertIs(backend._automation_paused(), False)
        self.assertEqual(third.status()["resume"]["outcome"], "resumed")
        self.assertEqual(pending(), original)
        self.assertEqual(fixed_controls(), expected_controls)
        status = fixture.cycle(900)  # One due tick, same subject/input/threshold/range.
        self.assertEqual(status["state"], "stored", status)
        self.assertEqual(observation["runtime_calls"], 1)
        self.assertEqual(len(fixture.adapter.calls), 1)
        rows = fixture.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "stored")
        request = rows[0]["request"]
        self.assertEqual(request["subject"], fixture.subject)
        self.assertEqual(request["sources"], ranges)
        self.assertEqual(fixture.adapter.calls[0]["request"]["sources"], ranges)
        self.assertEqual(request["sourceGaps"], [])
        self.assertIsNotNone(rows[0]["receipt"])
        self.assertEqual(pending(), [])
        self.assertEqual(fixed_controls(), expected_controls)
        observation.update(source_ranges=ranges, fixed_controls=expected_controls,
            subject_unchanged=True, resume_outcome="resumed", adapter_calls=1,
            request_rows=1, journal_state="stored", source_gaps=[], body_completed=True)


if __name__ == "__main__":
    unittest.main()
