"""Storage failure reasons and the Backend's health observation boundary."""

from datetime import datetime, timezone
import errno
from pathlib import Path
import random
import sqlite3
import tempfile
from threading import Event, RLock
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.backend.flow import OutboundMessage
from workbench.backend.service import Backend
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.storage.important_log import (ActiveSubjectLog, ImportantLogCatalog,
                                            ImportantLogStore, ImportantLogStoreError)


BODY = {"schemaVersion": 1, "facts": ["observation"], "interpretations": [], "unknowns": []}


class IndependentStorageFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "important"
        self.catalog = ImportantLogCatalog(self.directory, target_bytes=36_000,
                                           low_water_bytes=30_000, create=True)
        self.subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}

    def _owned_backend(self, *, initial_catalog_fault: bool) -> Backend:
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
                          project_dir=str(fixture.project), environment=env)
        self.addCleanup(lambda: self.assertTrue(backend._close()["verified"]))
        if initial_catalog_fault:
            catalog_class = ImportantLogCatalog
            attempts = []

            def fail_first_open(directory, **options):
                attempts.append(str(directory))
                if len(attempts) == 1:
                    raise OSError(errno.EIO, "initial important catalog unavailable")
                return catalog_class(directory, **options)

            with patch("workbench.backend.service.ImportantLogCatalog", side_effect=fail_first_open):
                backend._open()
            self.assertEqual(attempts, [str(backend.layout.important_logs)])
            self.assertEqual(backend.important_storage_status["state"], "unavailable")
            self.assertIsNone(backend.subject_log)
        else:
            backend._open()
            self.assertIsInstance(backend.subject_log, ActiveSubjectLog)
        backend._important_storage_at = time.monotonic()
        fixture.wait(backend, lambda: backend.phase == "ready" and
                     backend.omp_isolation["state"] != "pending")
        return backend

    def _recover_owned_backend(self, backend: Backend) -> str:
        self.assertIsNone(backend.important_catalog)
        backend._refresh_important_storage()
        self.assertIsNone(backend.important_catalog, "no probe before 30 seconds")
        backend._important_storage_at = time.monotonic() - 31
        backend._refresh_important_storage()
        self.assertIsNotNone(backend.important_catalog)
        identity = backend.important_catalog.active_uuid()
        self.assertEqual(backend.important_storage_status["active"], identity)
        self.assertIs(backend.flow._subject_log, backend.subject_log)
        self.assertIs(backend.terminal._subject_log, backend.subject_log)
        return identity

    def _owned_entries(self, backend: Backend, identity: str, kind: str, subject_id: str) -> list[dict]:
        with ImportantLogStore.open(backend.layout.important_logs, identity, readonly=True) as store:
            return store.list_entries({"schemaVersion": 1, "kind": kind, "id": subject_id})

    def _assert_owned_missing_visible(self, backend: Backend, entries: list[dict]) -> None:
        health = backend.important_storage_status
        marked = (bool(health.get("subjectCaptureUnknown"))
                  or any(token in str(health.get("state", "")).lower()
                         for token in ("capture_unknown", "missing", "gap"))
                  or any(item["kind"] == "gap" for item in entries))
        self.assertTrue(marked, f"confirmed capture loss hidden after recovery: {health}")

    @staticmethod
    def _started_record(command_id: str) -> dict:
        return {"type": "terminal_started", "command_id": command_id, "task_id": None,
                "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}

    def test_physical_and_permission_failures_never_look_like_normal_rollover(self):
        original = self.catalog.active_uuid()
        for code, expected in ((errno.ENOSPC, "physical_full"),
                               (errno.EDQUOT, "physical_full"),
                               (errno.EACCES, "permission"),
                               (errno.EROFS, "permission"),
                               (errno.EIO, "io_error")):
            with self.subTest(errno=code):
                with patch.object(ImportantLogStore, "put_entry", side_effect=OSError(code, "injected")):
                    outcome = self.catalog.append(self.subject, "start", [], BODY,
                                                  now=datetime.now(timezone.utc))
                self.assertEqual(outcome["state"], expected)
                self.assertIsNone(outcome["durableEntryId"])
                self.assertEqual(self.catalog.active_uuid(), original)
                self.assertEqual(self.catalog.catalog_snapshot()["databaseCount"], 1)
        normal = self.catalog.append(self.subject, "start", [], BODY)
        self.assertEqual(normal["state"], "ok")
        self.assertIsNotNone(normal["rollover"])

    def test_unknown_pointer_identity_refuses_write_and_preserves_database(self):
        old = self.catalog.active_uuid()
        pointer = self.directory / "active.json"
        pointer.write_text("{invalid json", encoding="utf-8")
        outcome = self.catalog.append(self.subject, "start", [], BODY)
        self.assertEqual(outcome["state"], "unsafe_or_corrupt")
        self.assertIsNone(outcome["durableEntryId"])
        self.assertTrue((self.directory / f"{old}.sqlite3").exists())

    def test_actual_sqlite_full_is_physical_failure_without_logical_rollover(self):
        catalog = ImportantLogCatalog(self.directory)
        old = catalog.active_uuid()
        open_store = ImportantLogStore.open
        sqlite_errors = []

        class ObservedConnection:
            def __init__(self, real):
                self.real = real

            def __getattr__(self, name):
                return getattr(self.real, name)

            def execute(self, *args, **options):
                try:
                    return self.real.execute(*args, **options)
                except sqlite3.DatabaseError as exc:
                    sqlite_errors.append((exc.sqlite_errorcode, str(exc)))
                    raise

        def limited_open(directory, db_uuid, **options):
            store = open_store(directory, db_uuid, **options)
            pages = store._connection.execute("PRAGMA page_count").fetchone()[0]
            limit = store._connection.execute(f"PRAGMA max_page_count={pages + 1}").fetchone()[0]
            self.assertLessEqual(limit, pages + 1)
            store._connection = ObservedConnection(store._connection)
            return store

        large_body = {"schemaVersion": 1,
                      "facts": [random.Random(19).randbytes(80_000).hex()],
                      "interpretations": [], "unknowns": []}
        with patch.object(ImportantLogStore, "open", side_effect=limited_open):
            outcome = catalog.append(self.subject, "start", [], large_body)
        self.assertIn(sqlite3.SQLITE_FULL, [code for code, _ in sqlite_errors], sqlite_errors)
        self.assertEqual(outcome["state"], "physical_full")
        self.assertIsNone(outcome["durableEntryId"])
        self.assertEqual(catalog.active_uuid(), old)
        self.assertEqual(catalog.catalog_snapshot()["databaseCount"], 1)
        with ImportantLogStore.open(self.directory, old, readonly=True) as retained:
            self.assertEqual(retained._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)
            self.assertEqual(retained._connection.execute("SELECT COUNT(*) FROM subject_stats").fetchone()[0], 0)

    def test_thirty_second_health_refresh_does_not_change_core_policy(self):
        backend = Backend.__new__(Backend)
        backend.important_catalog = self.catalog
        backend.subject_log = ActiveSubjectLog(self.catalog)
        backend.important_storage_status = {"state": "not_open"}
        backend._important_storage_at = 0.0
        backend.flow = SimpleNamespace(subject_capture_unknown=False)
        approval = object()
        hold = object()
        backend.approval = approval
        backend.hold = hold
        self.catalog.last_status = {"state": "physical_full"}
        with patch("workbench.backend.service.time.monotonic", return_value=100.0):
            backend._refresh_important_storage()
        self.assertEqual(backend.important_storage_status["recovery"]["state"], "healthy")
        backend.subject_log.last_failure = "fault after first refresh"
        with patch("workbench.backend.service.time.monotonic", return_value=129.9):
            backend._refresh_important_storage()
        self.assertNotEqual(backend.important_storage_status["state"], "capture_unknown")
        with patch("workbench.backend.service.time.monotonic", return_value=130.0):
            backend._refresh_important_storage()
        self.assertEqual(backend.important_storage_status["state"], "capture_unknown")
        self.assertIs(backend.approval, approval)
        self.assertIs(backend.hold, hold)

    def test_initial_catalog_failure_recovers_and_rebinds_existing_sinks(self):
        # Keep the already-started run attached to the Backend-owned TaskWorkflow.
        # Import as a module so its unrelated TestCase methods are not collected here.
        from tests.workflow import test_run as workflow_fixture

        fixture = workflow_fixture.WorkflowRuntimeTests(
            "test_four_exit_and_evidence_outcomes_on_real_host_shell")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, mailbox, worker_port, automation, task_id = fixture.strict_task()
        late_directory = Path(self.temp.name) / "late-important"
        late_directory.write_text("temporarily not a directory", encoding="utf-8")
        with self.assertRaises((OSError, ImportantLogStoreError)):
            ImportantLogCatalog(late_directory, create=True)
        late_directory.unlink()

        backend = Backend.__new__(Backend)
        backend.layout = SimpleNamespace(important_logs=late_directory)
        backend._raw_port_lock = RLock()
        backend.segmented_raw_port = None
        backend._owned_segmented_store = None
        backend.raw_rotation = None
        backend.important_catalog = None
        backend.subject_log = None
        backend.important_storage_status = {"state": "unavailable", "reason": "NotADirectoryError"}
        backend._important_storage_at = 100.0
        backend.flow = SimpleNamespace(_subject_log=None, subject_capture_unknown=False)
        backend.terminal = SimpleNamespace(_subject_log=None)
        backend.bridge = object()
        backend.raw_logs = None
        backend.automation_loop = None
        backend._automation_port = lambda: automation
        approval, hold = object(), object()
        backend.approval, backend.hold = approval, hold
        with (patch("workbench.backend.service.TaskMailbox", return_value=mailbox),
              patch("workbench.backend.service.G3WorkerResponsePort", return_value=worker_port)):
            running_workflow = backend._make_workflow(fixture.repository)
        run = running_workflow.start(
            task_id, 1, worktree_path=fixture.root / "storage-recovery-execution",
            artifacts_root=fixture.artifacts, automation=automation,
            environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm", "CW10_SECRET": ""})
        self.addCleanup(run.close)
        self.assertEqual(fixture.repository.get_current_run(task_id)["run_id"], run.run_id)
        self.assertIsNone(running_workflow.subject_log)
        self.assertFalse(late_directory.exists(), "confirmed pre-recovery events have no storage")
        prior_id, delivered_id, decision_id = str(uuid4()), str(uuid4()), str(uuid4())
        receipt = SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)
        message = lambda identity: SimpleNamespace(task_id=task_id, revision=1,
                                                  message_id=identity, payload={"text": identity})
        run._copy_message(message(prior_id), receipt, "manager_instruction")

        with patch("workbench.backend.service.time.monotonic", return_value=129.9):
            backend._refresh_important_storage()
        self.assertIsNone(backend.important_catalog)
        self.assertFalse(late_directory.exists(), "no store or replay before recovery probe")
        with patch("workbench.backend.service.time.monotonic", return_value=130.0):
            backend._refresh_important_storage()
        self.assertIsNotNone(backend.important_catalog)
        self.assertIsInstance(backend.subject_log, ActiveSubjectLog)
        self.assertIs(backend.flow._subject_log, backend.subject_log)
        self.assertIs(backend.terminal._subject_log, backend.subject_log)
        self.assertIs(running_workflow.subject_log, backend.subject_log)
        run._copy_message(message(delivered_id), receipt, "manager_instruction")
        run._copy_decision({"response_id": decision_id, "decision": "accepted"})
        self.assertEqual(backend.important_storage_status["active"],
                         backend.important_catalog.active_uuid())
        with ImportantLogStore.open(late_directory, backend.important_catalog.active_uuid(),
                                    readonly=True) as store:
            entries = store.list_entries({"schemaVersion": 1, "kind": "task", "id": task_id})
            confirmed = [item for item in entries if item["kind"] != "gap"]
            self.assertEqual(len(confirmed), 2)
            source_ids = {item["sources"][0]["source"].get("messageId") or
                          item["sources"][0]["source"].get("eventId") for item in confirmed}
            self.assertEqual(source_ids, {delivered_id, decision_id})
        self.assertIs(backend.approval, approval)
        self.assertIs(backend.hold, hold)
        health = backend.important_storage_status
        health_loss = (any(token in str(health.get("state", "")).lower()
                           for token in ("capture_unknown", "missing", "gap"))
                       or any(value and any(token in key.lower()
                                            for token in ("captureunknown", "missing", "gap"))
                              for key, value in health.items()))
        self.assertTrue(
            health_loss or any(item["kind"] == "gap" for item in entries),
            ("recovery must retain an observable missing-input indication for confirmed events lost "
             f"before it: run={run.subject_capture_unknown}, "
             f"workflow={running_workflow.subject_capture_unknown}, "
             f"flow={backend.flow.subject_capture_unknown}, "
             f"storage_state={health['state']}, health_loss={health_loss}"),
        )

    def test_intentionally_optional_subject_sink_does_not_report_capture_loss(self):
        from tests.workflow import test_run as workflow_fixture

        fixture = workflow_fixture.WorkflowRuntimeTests(
            "test_four_exit_and_evidence_outcomes_on_real_host_shell")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        workflow, _, _, automation, task_id = fixture.strict_task()
        self.assertIsNone(workflow.subject_log)
        run = workflow.start(
            task_id, 1, worktree_path=fixture.root / "optional-subject-capture",
            artifacts_root=fixture.artifacts, automation=automation,
            environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm", "CW10_SECRET": ""})
        self.addCleanup(run.close)
        self.assertFalse(workflow.subject_capture_unknown)
        self.assertFalse(run.subject_capture_unknown)

    def test_backend_flow_record_loss_remains_visible_after_initial_catalog_recovery(self):
        backend = self._owned_backend(initial_catalog_fault=True)
        hold_before = backend.hold.view()
        lost_task, later_task = str(uuid4()), str(uuid4())
        backend.flow._record({"type": "delegated", "task_id": lost_task, "revision": 1,
                              "scope_decision_id": str(uuid4()),
                              "proceed_decision_id": str(uuid4())})
        identity = self._recover_owned_backend(backend)
        backend.flow._record({"type": "delegated", "task_id": later_task, "revision": 1,
                              "scope_decision_id": str(uuid4()),
                              "proceed_decision_id": str(uuid4())})
        self.assertEqual(self._owned_entries(backend, identity, "task", lost_task), [])
        self.assertEqual(len(self._owned_entries(backend, identity, "task", later_task)), 1)
        backend._important_storage_at = time.monotonic() - 31
        backend._refresh_important_storage()
        self._assert_owned_missing_visible(backend, self._owned_entries(backend, identity, "task", lost_task))
        self.assertEqual(backend.hold.view(), hold_before)

    def test_backend_confirmed_delivery_loss_remains_visible_after_initial_catalog_recovery(self):
        from tests.backend.test_important_subjects_independent import _ReceiptMailbox

        backend = self._owned_backend(initial_catalog_fault=True)
        hold_before = backend.hold.view()
        task_id, run_id = str(uuid4()), str(uuid4())
        mailbox = _ReceiptMailbox([MailboxStatus.OMP_PROCESSED] * 2)
        # The real Backend owns this outbox. Only its external peer/mailbox seams
        # are replaced so no OMP call or terminal submission occurs.
        backend.handoffs._mailbox_factory = lambda: (mailbox, lambda: None)
        backend.handoffs._peer_lookup = None
        backend.handoffs._hold = lambda _role: None
        backend.handoffs._paused = lambda: False

        def delivered(marker: str) -> str:
            outbound = OutboundMessage(task_id, 1, run_id, ActorRole.MANAGER, ActorRole.WORKER,
                                       MessageKind.QUESTION, {"message": marker})
            done, events = Event(), []
            capture = backend.flow._subject_listener(outbound, None)

            def listener(event, snapshot, *, delivered_message=None):
                capture(event, snapshot, delivered_message=delivered_message)
                events.append(event)
                if event in {"delivered", "unknown", "rejected"}:
                    done.set()

            self.assertEqual(backend.handoffs.enqueue(outbound, origin="independent", listener=listener)["status"],
                             "queued")
            self.assertTrue(done.wait(3), events)
            self.assertEqual(events[-1], "delivered")
            return mailbox.created[-1].message_id

        lost_id = delivered("before recovery")
        identity = self._recover_owned_backend(backend)
        later_id = delivered("after recovery")
        entries = self._owned_entries(backend, identity, "task", task_id)
        confirmed = [item for item in entries if item["kind"] != "gap"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["sources"][0]["source"]["messageId"], later_id)
        backend._important_storage_at = time.monotonic() - 31
        backend._refresh_important_storage()
        self._assert_owned_missing_visible(backend, entries)
        self.assertEqual(backend.hold.view(), hold_before)

    def test_backend_terminal_start_loss_remains_visible_after_initial_catalog_recovery(self):
        backend = self._owned_backend(initial_catalog_fault=True)
        hold_before = backend.hold.view()
        lost_command, later_command = str(uuid4()), str(uuid4())
        backend.terminal._journal(self._started_record(lost_command))
        identity = self._recover_owned_backend(backend)
        backend.terminal._journal(self._started_record(later_command))
        self.assertEqual(self._owned_entries(backend, identity, "command", lost_command), [])
        self.assertEqual(len(self._owned_entries(backend, identity, "command", later_command)), 1)
        backend._important_storage_at = time.monotonic() - 31
        backend._refresh_important_storage()
        self._assert_owned_missing_visible(backend,
                                           self._owned_entries(backend, identity, "command", lost_command))
        self.assertEqual(backend.hold.view(), hold_before)

    def test_backend_health_keeps_terminal_capture_loss_after_write_recovers(self):
        backend = self._owned_backend(initial_catalog_fault=False)
        hold_before = backend.hold.view()
        identity = backend.important_catalog.active_uuid()
        lost_command, later_command = str(uuid4()), str(uuid4())
        with patch.object(ImportantLogStore, "put_entry", side_effect=OSError(errno.EIO, "injected")):
            backend.terminal._journal(self._started_record(lost_command))
        self.assertTrue(backend.terminal.subject_capture_unknown)
        backend.terminal._journal(self._started_record(later_command))
        self.assertIsNone(backend.subject_log.last_failure)
        self.assertEqual(self._owned_entries(backend, identity, "command", lost_command), [])
        self.assertEqual(len(self._owned_entries(backend, identity, "command", later_command)), 1)
        backend._important_storage_at = time.monotonic() - 31
        backend._refresh_important_storage()
        self._assert_owned_missing_visible(backend,
                                           self._owned_entries(backend, identity, "command", lost_command))
        self.assertEqual(backend.hold.view(), hold_before)

    def test_real_backend_open_recovers_transient_catalog_failure_with_fake_panes(self):
        # Import the existing fixture as a module: importing its TestCase class into this
        # module's globals would make unittest discover its unrelated test methods here.
        from tests.backend import test_omp_home as home_fixture

        fixture = home_fixture.BackendHomeTests(
            "test_both_panes_and_checks_use_the_home_and_no_user_config_is_read")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.user_store()
        before_home = home_fixture.inventory(fixture.home)
        env = {**fixture.env, "FAKE_RECORD": str(fixture.record), "OMP_PROFILE": "work"}
        plan = home_fixture.LaunchPlan(
            home_fixture.ShellChoice("bash", "/usr/bin/bash"), str(fixture.fake),
            "omp/18.4.5", "/x/bridge.ts")
        backend = Backend(home_fixture.DataLayout(fixture.data), plan,
                          project_dir=str(fixture.project), environment=env)
        closed = False
        close_result = None

        def close_owned_backend():
            nonlocal closed, close_result
            if not closed:
                closed = True
                close_result = backend._close()
            return close_result

        self.addCleanup(close_owned_backend)
        catalog_class = ImportantLogCatalog
        attempts = []

        def one_transient_fault(directory, **options):
            attempts.append(str(directory))
            if len(attempts) == 1:
                raise OSError(errno.EIO, "transient important catalog open failure")
            return catalog_class(directory, **options)

        with patch("workbench.backend.service.ImportantLogCatalog", side_effect=one_transient_fault):
            backend._open()
            self.assertEqual(backend.important_storage_status["state"], "unavailable")
            self.assertIsNone(backend.subject_log)
            self.assertIsNone(backend.flow._subject_log)
            self.assertIsNone(backend.terminal._subject_log)
            holds_before = backend.hold.view()
            backend._important_storage_at = time.monotonic()
            fixture.wait(backend, lambda: backend.phase == "ready" and
                         backend.omp_isolation["state"] != "pending")
            self.assertEqual(len(attempts), 1)
            self.assertIsNone(backend.important_catalog)
            self.assertEqual(len(fixture.records()), 2)
            backend._important_storage_at = time.monotonic() - 31
            backend._refresh_important_storage()
            self.assertEqual(len(attempts), 2)
            self.assertIsNotNone(backend.important_catalog)
            self.assertIs(backend.flow._subject_log, backend.subject_log)
            self.assertIs(backend.terminal._subject_log, backend.subject_log)
            self.assertEqual(backend.hold.view(), holds_before)
            self.assertEqual(home_fixture.inventory(fixture.home), before_home)
        self.assertTrue(close_owned_backend()["verified"])


if __name__ == "__main__":
    unittest.main()
