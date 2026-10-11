"""DVL-05: durable journal first, optional important-log copy second."""

from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import time
import unittest
from uuid import uuid4

from workbench.backend.flow import HandoffJournal, HandoffService, OutboundMessage
from workbench.backend.flow_tasks import TaskFlow
from workbench.backend.flow_terminal import HostGate, TerminalService
from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.storage.important_log import ImportantLogStore, SubjectLifecycle
from workbench.contracts.v1 import ActorRole, MessageKind


AT = "2026-10-09T12:00:00Z"


class _Handoffs:
    def __init__(self, path):
        self.journal = HandoffJournal(path)

    def record(self, record):
        return self.journal.append(record)

    def close(self):
        self.journal.close()


class _FailingSubject:
    def reconcile_flow(self, _records):
        return 0

    def record_flow(self, _record):
        raise OSError("important DB unavailable")


class _ImmediateHandoffs:
    def __init__(self):
        self.message_id = str(uuid4())

    def enqueue(self, _outbound, *, origin, listener, **_kwargs):
        self.origin = origin
        listener("submitted", {"message_id": self.message_id})
        listener("delivered", {"message_id": self.message_id})
        return {"status": "queued"}


class _PhysicalMailbox:
    def __init__(self, receipts):
        self.receipts = list(receipts)
        self.created = []

    def create_message(self, task_id, revision, run_id, sender_role, target_role, kind, payload, *,
                       in_reply_to_message_id=None):
        message = SimpleNamespace(message_id=str(uuid4()), session_id=str(uuid4()), session_generation=1,
                                  payload=dict(payload))
        self.created.append(message)
        return message

    def deliver(self, message, *, timeout=20):
        status, reason = self.receipts.pop(0)
        return SimpleNamespace(status=status, details={"reason": reason})


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
        time.sleep(0.01)
    return predicate()


class ImportantSubjectSeamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with ImportantLogStore.create(self.root / "important") as store:
            self.db_uuid = store.db_uuid
        self.subjects = SubjectLifecycle(self.root / "important", self.db_uuid)

    def tearDown(self):
        self.temp.cleanup()

    def _flow(self, subject_log):
        return TaskFlow(self.root / "flow.jsonl", repository_factory=lambda: None,
                        handoffs=None, omp_idle=lambda _role: True, subject_log=subject_log)

    def test_flow_journal_survives_failed_copy_and_restart_copies_confirmed_fact(self):
        task_id = str(uuid4())
        scope_id, proceed_id = str(uuid4()), str(uuid4())
        flow = self._flow(_FailingSubject())
        flow._record({"type": "delegated", "task_id": task_id, "revision": 1,
                      "scope_decision_id": scope_id, "proceed_decision_id": proceed_id})
        self.assertTrue(flow.subject_capture_unknown)
        self.assertEqual(self.subjects.state({"schemaVersion": 1, "kind": "task", "id": task_id}), {})
        flow.close()
        restored = self._flow(self.subjects)
        state = self.subjects.state({"schemaVersion": 1, "kind": "task", "id": task_id})
        self.assertEqual(state["grant_ref"], proceed_id)
        self.assertEqual(state["event_cursor"], 1)
        self.assertFalse(restored.subject_capture_unknown)
        restored.close()

    def test_terminal_journal_copy_keeps_taskless_command_open_when_end_unknown(self):
        handoffs = _Handoffs(self.root / "terminal.jsonl")
        terminal = TerminalService(handoffs=handoffs, host_shell=lambda: None, gate=HostGate(),
                                   log_root=self.root / "logs", automation=lambda: {},
                                   subject_log=self.subjects)
        command_id = str(uuid4())
        terminal._journal({"type": "terminal_started", "command_id": command_id,
                           "task_id": None, "started_at": AT})
        terminal._journal({"type": "terminal_ended", "command_id": command_id,
                           "status": "unknown", "ended_at": AT})
        self.assertFalse(terminal.subject_capture_unknown)
        state = self.subjects.state({"schemaVersion": 1, "kind": "command", "id": command_id})
        self.assertEqual((state["archival_state"], state["command_end_status"]), ("open", "unknown"))
        handoffs.close()

    def test_unconfigured_standalone_ports_do_not_report_capture_loss(self):
        flow = self._flow(None)
        task_id = str(uuid4())
        flow._record({"type": "delegated", "task_id": task_id, "revision": 1,
                      "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())})
        outbound = OutboundMessage(task_id, 1, str(uuid4()), ActorRole.MANAGER, ActorRole.WORKER,
                                   MessageKind.QUESTION, {"message": "fixture"})
        flow._subject_listener(outbound, None)("delivered", {"message_id": str(uuid4())})
        self.assertFalse(flow.subject_capture_unknown)
        flow.close()

        handoffs = _Handoffs(self.root / "optional-terminal.jsonl")
        terminal = TerminalService(handoffs=handoffs, host_shell=lambda: None, gate=HostGate(),
                                   log_root=self.root / "optional-logs", automation=lambda: {})
        terminal._journal({"type": "terminal_started", "command_id": str(uuid4()),
                           "task_id": None, "started_at": AT})
        self.assertFalse(terminal.subject_capture_unknown)
        handoffs.close()

    def test_delivered_task_payload_is_copied_once_with_message_id_not_chat(self):
        task_id = str(uuid4())
        flow = self._flow(self.subjects)
        flow._record({"type": "delegated", "task_id": task_id, "revision": 1,
                      "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())})
        outbound = OutboundMessage(task_id, 1, str(uuid4()), ActorRole.MANAGER, ActorRole.WORKER,
                                   MessageKind.TASK, {"goal": "confirmed Task spec", "message": "instruction"})
        callbacks = []
        listener = flow._subject_listener(outbound, lambda event, _snapshot: callbacks.append(event))
        message_id = str(uuid4())
        listener("created", {"message_id": message_id})
        listener("submitted", {"message_id": message_id})
        with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
            self.assertEqual(len(store.list_entries({"schemaVersion": 1, "kind": "task", "id": task_id})), 1)
        listener("delivered", {"message_id": message_id})
        self.assertEqual(callbacks, ["created", "submitted", "delivered"])
        self.assertFalse(flow.subject_capture_unknown)
        subject = {"schemaVersion": 1, "kind": "task", "id": task_id}
        with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
            entries = store.list_entries(subject)
        self.assertEqual(len(entries), 2)
        delivered = [entry for entry in entries if entry["sources"][0]["source"].get("messageId") == message_id]
        self.assertEqual(len(delivered), 1)
        self.assertNotIn("user dialogue", delivered[0]["body"]["facts"][0])
        flow.close()

    def test_workbench_report_direct_enqueue_is_copied_only_on_delivery(self):
        task_id, run_id, original_message_id = str(uuid4()), str(uuid4()), str(uuid4())
        flow = self._flow(self.subjects)
        flow._record({"type": "delegated", "task_id": task_id, "revision": 1,
                      "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())})
        handoffs = _ImmediateHandoffs()
        flow._handoffs = handoffs
        flow._tell_manager(task_id, 1, run_id, original_message_id,
                           {"notice": "run_start_failed"}, "confirmed failure", "fixture")
        with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
            entries = store.list_entries({"schemaVersion": 1, "kind": "task", "id": task_id})
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[-1]["sources"][0]["source"]["messageId"], handoffs.message_id)
        self.assertEqual(handoffs.origin, "fixture")
        flow.close()

    def test_prepared_task_and_manager_instruction_copy_physical_payload_only_after_delivery(self):
        flow = self._flow(self.subjects)
        mailbox = _PhysicalMailbox([(MailboxStatus.OMP_PROCESSED, None)] * 3)
        handoffs = HandoffService(self.root / "outbox.jsonl", mailbox=mailbox)
        handoffs.start()
        self.addCleanup(handoffs.close)
        self.addCleanup(flow.close)
        task_id, run_id = str(uuid4()), str(uuid4())
        subject = {"schemaVersion": 1, "kind": "task", "id": task_id}
        for expected_count, (kind, actual) in enumerate(((MessageKind.TASK, "prepared Task"),
                                                        (MessageKind.QUESTION, "prepared Manager instruction")), 1):
            prepares = []

            def prepare(payload):
                prepares.append(dict(payload))
                return {**payload, "message": actual}

            outbound = OutboundMessage(task_id, 1, run_id, ActorRole.MANAGER, ActorRole.WORKER,
                                       kind, {"message": "stale"}, prepare=prepare)
            snapshots = []
            listener = flow._subject_listener(outbound, lambda event, snapshot: snapshots.append((event, snapshot)))
            self.assertEqual(handoffs.enqueue(outbound, origin="subject-test", listener=listener)["status"], "queued")
            self.assertTrue(_wait_until(lambda: handoffs.outbox_snapshot()[-1]["state"] == "delivered"))

            def copied():
                with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
                    return len(store.list_entries(subject)) == expected_count

            self.assertTrue(_wait_until(copied))
            self.assertEqual(prepares, [{"message": "stale"}])
            self.assertEqual(mailbox.created[-1].payload["message"], actual)
            self.assertTrue(all("payload" not in snapshot for _, snapshot in snapshots))
            self.assertTrue(all("payload" not in snapshot for snapshot in handoffs.outbox_snapshot()))
        with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
            entries = store.list_entries(subject)
        self.assertEqual(len(entries), 2)
        self.assertEqual({entry["sources"][0]["source"]["messageId"] for entry in entries},
                         {message.message_id for message in mailbox.created})
        self.assertTrue(all("stale" not in entry["body"]["facts"][0] for entry in entries))
        self.assertEqual({entry["sources"][0]["source"]["sourceKind"] for entry in entries},
                         {"task_spec", "manager_instruction"})
        generic_extras = []
        generic = OutboundMessage(task_id, 1, run_id, ActorRole.MANAGER, ActorRole.WORKER,
                                  MessageKind.QUESTION, {"message": "generic callback"})
        handoffs.enqueue(generic, origin="generic-test",
                         listener=lambda event, _snapshot, **extra: generic_extras.append((event, extra)))
        self.assertTrue(_wait_until(lambda: handoffs.outbox_snapshot()[-1]["state"] == "delivered"))
        self.assertTrue(_wait_until(lambda: any(event == "delivered" for event, _ in generic_extras)))
        self.assertTrue(all(extra == {} for _, extra in generic_extras))
        self.assertFalse(flow.subject_capture_unknown)

    def test_confirmed_physical_delivery_survives_core_listener_failure(self):
        flow = self._flow(self.subjects)
        mailbox = _PhysicalMailbox([(MailboxStatus.OMP_PROCESSED, None),
                                    (MailboxStatus.UNKNOWN, "uncertain")])
        handoffs = HandoffService(self.root / "outbox.jsonl", mailbox=mailbox)
        handoffs.start()
        self.addCleanup(handoffs.close)
        self.addCleanup(flow.close)
        task_id, run_id = str(uuid4()), str(uuid4())
        subject = {"schemaVersion": 1, "kind": "task", "id": task_id}
        events = []

        def failing_core(event, _snapshot):
            events.append(event)
            if event == "delivered":
                raise OSError("flow journal unavailable")

        outbound = OutboundMessage(task_id, 1, run_id, ActorRole.MANAGER, ActorRole.WORKER,
                                   MessageKind.TASK, {"message": "stale"},
                                   prepare=lambda payload: {**payload, "message": "physical Task"})
        self.assertEqual(handoffs.enqueue(outbound, origin="journal-failure-test",
                                          listener=flow._subject_listener(outbound, failing_core))["status"], "queued")
        self.assertTrue(_wait_until(lambda: handoffs.outbox_snapshot()[0]["state"] == "delivered"))

        def copied():
            with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
                return len(store.list_entries(subject)) == 1

        self.assertTrue(_wait_until(copied))
        with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
            entries = store.list_entries(subject)
        self.assertEqual(events[-1], "delivered")
        self.assertEqual(entries[0]["sources"][0]["source"]["messageId"], mailbox.created[0].message_id)
        self.assertIn("physical Task", entries[0]["body"]["facts"][0])
        self.assertNotIn("stale", entries[0]["body"]["facts"][0])
        self.assertTrue(flow.subject_capture_unknown)

        uncertain = OutboundMessage(task_id, 1, run_id, ActorRole.MANAGER, ActorRole.WORKER,
                                    MessageKind.TASK, {"message": "not confirmed"})
        self.assertEqual(handoffs.enqueue(uncertain, origin="unknown-test",
                                          listener=flow._subject_listener(uncertain, failing_core))["status"], "queued")
        self.assertTrue(_wait_until(lambda: handoffs.outbox_snapshot()[-1]["state"] == "unknown"))
        with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
            self.assertEqual(len(store.list_entries(subject)), 1)

    def test_core_listener_error_still_propagates_after_confirmed_capture(self):
        flow = self._flow(self.subjects)
        self.addCleanup(flow.close)
        task_id, message_id = str(uuid4()), str(uuid4())
        outbound = OutboundMessage(task_id, 1, str(uuid4()), ActorRole.MANAGER, ActorRole.WORKER,
                                   MessageKind.TASK, {"message": "stale"}, prepare=lambda payload: payload)

        def failing_core(_event, _snapshot):
            raise OSError("flow journal unavailable")

        listener = flow._subject_listener(outbound, failing_core)
        physical = SimpleNamespace(message_id=message_id, payload={"message": "confirmed physical"})
        with self.assertRaisesRegex(OSError, "flow journal unavailable"):
            listener("delivered", {"message_id": message_id}, delivered_message=physical)
        with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
            entries = store.list_entries({"schemaVersion": 1, "kind": "task", "id": task_id})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["sources"][0]["source"]["messageId"], message_id)
        self.assertTrue(flow.subject_capture_unknown)

    def test_prepared_requeue_copies_only_replacement_message_and_failed_delivery_copies_none(self):
        flow = self._flow(self.subjects)
        mailbox = _PhysicalMailbox([(MailboxStatus.REJECTED, "target_session_changed"),
                                    (MailboxStatus.OMP_PROCESSED, None),
                                    (MailboxStatus.UNKNOWN, "uncertain"),
                                    (MailboxStatus.REJECTED, "not_delivered")])
        handoffs = HandoffService(self.root / "outbox.jsonl", mailbox=mailbox)
        handoffs.start()
        self.addCleanup(handoffs.close)
        self.addCleanup(flow.close)
        task_id, run_id = str(uuid4()), str(uuid4())
        subject = {"schemaVersion": 1, "kind": "task", "id": task_id}
        attempts = []

        def prepare(payload):
            attempts.append(len(attempts) + 1)
            return {**payload, "attempt": attempts[-1]}

        outbound = OutboundMessage(task_id, 1, run_id, ActorRole.WORKER, ActorRole.MANAGER,
                                   MessageKind.REPORT, {"message": "report"}, prepare=prepare)
        listener = flow._subject_listener(outbound, None)
        self.assertEqual(handoffs.enqueue(outbound, origin="requeue-test", listener=listener)["status"], "queued")
        self.assertTrue(_wait_until(lambda: handoffs.outbox_snapshot()[0]["state"] == "delivered"))
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(len(mailbox.created), 2)

        def copied():
            with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
                return len(store.list_entries(subject)) == 1

        self.assertTrue(_wait_until(copied))
        with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
            entries = store.list_entries(subject)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["sources"][0]["source"]["messageId"], mailbox.created[1].message_id)
        self.assertIn('"attempt":2', entries[0]["body"]["facts"][0])
        listener("delivered", {"message_id": mailbox.created[1].message_id},
                 delivered_message=mailbox.created[1])
        with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
            self.assertEqual(len(store.list_entries(subject)), 1)
        failed = OutboundMessage(task_id, 1, run_id, ActorRole.WORKER, ActorRole.MANAGER,
                                 MessageKind.REPORT, {"message": "unknown"}, prepare=prepare)
        self.assertEqual(handoffs.enqueue(failed, origin="unknown-test",
                                          listener=flow._subject_listener(failed, None))["status"], "queued")
        self.assertTrue(_wait_until(lambda: handoffs.outbox_snapshot()[-1]["state"] == "unknown"))
        rejected = OutboundMessage(task_id, 1, run_id, ActorRole.WORKER, ActorRole.MANAGER,
                                   MessageKind.REPORT, {"message": "rejected"}, prepare=prepare)
        self.assertEqual(handoffs.enqueue(rejected, origin="rejected-test",
                                          listener=flow._subject_listener(rejected, None))["status"], "queued")
        self.assertTrue(_wait_until(lambda: handoffs.outbox_snapshot()[-1]["state"] == "rejected"))

        def fail_prepare(_payload):
            raise RuntimeError("fixture prepare failure")

        failed = OutboundMessage(task_id, 1, run_id, ActorRole.WORKER, ActorRole.MANAGER,
                                 MessageKind.REPORT, {"message": "failed prepare"}, prepare=fail_prepare)
        created_count = len(mailbox.created)
        self.assertEqual(handoffs.enqueue(failed, origin="prepare-failure-test",
                                          listener=flow._subject_listener(failed, None))["status"], "queued")
        self.assertTrue(_wait_until(lambda: handoffs.outbox_snapshot()[-1]["state"] == "rejected"))
        self.assertEqual(len(mailbox.created), created_count)
        with ImportantLogStore.open(self.root / "important", self.db_uuid) as store:
            self.assertEqual(len(store.list_entries(subject)), 1)
        self.assertFalse(flow.subject_capture_unknown)


if __name__ == "__main__":
    unittest.main()
