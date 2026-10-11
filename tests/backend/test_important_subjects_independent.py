"""Independent outbox checks for confirmed important-log delivery."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace
import tempfile
import unittest
from uuid import uuid4

from workbench.backend.flow import HandoffService, OutboundMessage
from workbench.backend.flow_tasks import TaskFlow
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.storage.important_log import ImportantLogStore, SubjectLifecycle


class _ReceiptMailbox:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.created = []

    def create_message(self, _task_id, _revision, _run_id, _sender, _target, _kind, payload,
                       *, in_reply_to_message_id=None):
        message = SimpleNamespace(message_id=str(uuid4()), session_id=str(uuid4()),
                                  session_generation=1, payload=dict(payload))
        self.created.append(message)
        return message

    def deliver(self, _message, *, timeout=20):
        status = next(self.statuses)
        reason = "target_session_changed" if status is MailboxStatus.REJECTED else None
        return SimpleNamespace(status=status, details={"reason": reason})


class IndependentPhysicalDelivery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dvl045-physical-", dir="/dev/shm")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "important"
        with ImportantLogStore.create(self.directory) as store:
            self.db_uuid = store.db_uuid
        self.subject_log = SubjectLifecycle(self.directory, self.db_uuid)
        self.flow = TaskFlow(self.root / "flow.jsonl", repository_factory=lambda: None,
                             handoffs=None, omp_idle=lambda _role: True,
                             subject_log=self.subject_log)
        self.addCleanup(self.flow.close)
        self.task_id, self.run_id = str(uuid4()), str(uuid4())
        self.subject = {"schemaVersion": 1, "kind": "task", "id": self.task_id}

    def _entries(self):
        with ImportantLogStore.open(self.directory, self.db_uuid) as store:
            return store.list_entries(self.subject)

    def _handoffs(self, statuses):
        mailbox = _ReceiptMailbox(statuses)
        handoffs = HandoffService(self.root / f"outbox-{uuid4()}.jsonl", mailbox=mailbox)
        handoffs.start()
        self.addCleanup(handoffs.close)
        return handoffs, mailbox

    def _send(self, handoffs, outbound):
        done = Event()
        events = []
        capture = self.flow._subject_listener(outbound, None)

        def listener(event, snapshot, *, delivered_message=None):
            capture(event, snapshot, delivered_message=delivered_message)
            events.append((event, dict(snapshot)))
            if event in {"delivered", "unknown", "rejected"}:
                done.set()

        self.assertEqual(handoffs.enqueue(outbound, origin="independent", listener=listener)["status"], "queued")
        self.assertTrue(done.wait(3), events)
        return events, listener

    def test_prepared_task_and_manager_instruction_record_only_created_payload(self):
        handoffs, mailbox = self._handoffs([MailboxStatus.OMP_PROCESSED] * 3)
        for kind, source, marker in ((MessageKind.TASK, "task_spec", "actual spec"),
                                     (MessageKind.QUESTION, "manager_instruction", "actual instruction")):
            outbound = OutboundMessage(self.task_id, 2, self.run_id, ActorRole.MANAGER, ActorRole.WORKER,
                                       kind, {"message": "stale instruction"},
                                       prepare=lambda payload, value=marker: {**payload, "message": value})
            events, _listener = self._send(handoffs, outbound)
            self.assertEqual(events[-1][0], "delivered")
            self.assertTrue(all("payload" not in snapshot for _, snapshot in events))
            matches = [entry for entry in self._entries()
                       if entry["sources"][0]["source"].get("messageId") == mailbox.created[-1].message_id]
            self.assertEqual(len(matches), 1)
            entry = matches[0]
            self.assertEqual(entry["sources"][0]["source"]["sourceKind"], source)
            self.assertEqual(entry["sources"][0]["source"]["messageId"], mailbox.created[-1].message_id)
            self.assertIn(marker, entry["body"]["facts"][0])
            self.assertNotIn("stale instruction", entry["body"]["facts"][0])

        generic_events = []
        generic_done = Event()

        def generic(event, snapshot, **extras):
            generic_events.append((event, dict(snapshot), extras))
            if event == "delivered":
                generic_done.set()

        outbound = OutboundMessage(self.task_id, 2, self.run_id, ActorRole.MANAGER, ActorRole.WORKER,
                                   MessageKind.QUESTION, {"message": "private dialogue"})
        self.assertEqual(handoffs.enqueue(outbound, origin="generic", listener=generic)["status"], "queued")
        self.assertTrue(generic_done.wait(3))
        self.assertTrue(all(not extras and "payload" not in snapshot
                            for _, snapshot, extras in generic_events))
        self.assertEqual(len(self._entries()), 2)
        self.assertFalse(self.flow.subject_capture_unknown)

    def test_requeue_uses_replacement_id_and_failures_create_no_subject_entry(self):
        handoffs, mailbox = self._handoffs([MailboxStatus.REJECTED, MailboxStatus.OMP_PROCESSED,
                                           MailboxStatus.UNKNOWN])
        prepare_count = 0

        def prepare(payload):
            nonlocal prepare_count
            prepare_count += 1
            return {**payload, "prepared_attempt": prepare_count}

        report = OutboundMessage(self.task_id, 3, self.run_id, ActorRole.WORKER, ActorRole.MANAGER,
                                 MessageKind.REPORT, {"message": "report"}, prepare=prepare)
        events, listener = self._send(handoffs, report)
        self.assertEqual(events[-1][0], "delivered")
        self.assertEqual(len(mailbox.created), 2)
        self.assertEqual(prepare_count, 2)
        self.assertEqual(events[-1][1]["message_id"], mailbox.created[1].message_id)
        entry = self._entries()[0]
        self.assertEqual(entry["sources"][0]["source"]["messageId"], mailbox.created[1].message_id)
        self.assertIn('"prepared_attempt":2', entry["body"]["facts"][0])
        listener("delivered", events[-1][1], delivered_message=mailbox.created[1])
        self.assertEqual(len(self._entries()), 1)

        uncertain = OutboundMessage(self.task_id, 3, self.run_id, ActorRole.WORKER, ActorRole.MANAGER,
                                    MessageKind.REPORT, {"message": "uncertain"}, prepare=prepare)
        uncertain_events, _ = self._send(handoffs, uncertain)
        self.assertEqual(uncertain_events[-1][0], "unknown")

        def broken_prepare(_payload):
            raise RuntimeError("fixture prepare error")

        failed = OutboundMessage(self.task_id, 3, self.run_id, ActorRole.WORKER, ActorRole.MANAGER,
                                 MessageKind.REPORT, {"message": "never created"}, prepare=broken_prepare)
        created_before = len(mailbox.created)
        failed_events, _ = self._send(handoffs, failed)
        self.assertEqual(failed_events[-1][0], "rejected")
        self.assertEqual(len(mailbox.created), created_before)
        self.assertEqual(len(self._entries()), 1)
        self.assertFalse(self.flow.subject_capture_unknown)

    def test_prepared_delivery_without_matching_physical_message_is_unknown(self):
        outbound = OutboundMessage(self.task_id, 1, self.run_id, ActorRole.MANAGER, ActorRole.WORKER,
                                   MessageKind.TASK, {"message": "stale"},
                                   prepare=lambda payload: {**payload, "message": "actual"})
        listener = self.flow._subject_listener(outbound, None)
        message_id = str(uuid4())
        listener("delivered", {"message_id": message_id})
        self.assertTrue(self.flow.subject_capture_unknown)
        self.assertEqual(self._entries(), [])
        other = SimpleNamespace(message_id=str(uuid4()), payload={"message": "actual"})
        listener("delivered", {"message_id": message_id}, delivered_message=other)
        self.assertEqual(self._entries(), [])

    def test_confirmed_prepared_delivery_survives_core_listener_error(self):
        handoffs, mailbox = self._handoffs([MailboxStatus.OMP_PROCESSED])
        outbound = OutboundMessage(self.task_id, 4, self.run_id, ActorRole.MANAGER, ActorRole.WORKER,
                                   MessageKind.TASK, {"message": "stale spec"},
                                   prepare=lambda payload: {**payload, "message": "physical spec"})
        core_error = OSError("core journal unavailable")
        core_events = []

        def core_listener(event, _snapshot):
            core_events.append(event)
            if event == "delivered":
                raise core_error

        capture = self.flow._subject_listener(outbound, core_listener)
        done = Event()
        observed_errors = []
        observed_snapshots = []

        def listener(event, snapshot, *, delivered_message=None):
            try:
                capture(event, snapshot, delivered_message=delivered_message)
            except Exception as exc:
                observed_errors.append(exc)
                raise
            finally:
                observed_snapshots.append((event, dict(snapshot)))
                if event == "delivered":
                    done.set()

        self.assertEqual(handoffs.enqueue(outbound, origin="independent", listener=listener)["status"], "queued")
        self.assertTrue(done.wait(3), observed_snapshots)
        self.assertEqual(core_events.count("delivered"), 1)
        self.assertEqual(observed_errors, [core_error])
        self.assertIs(observed_errors[0], core_error)
        self.assertEqual(len(mailbox.created), 1)
        self.assertEqual(mailbox.created[0].payload["message"], "physical spec")
        self.assertTrue(all("payload" not in snapshot for _, snapshot in observed_snapshots))
        entries = self._entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["sources"][0]["source"]["messageId"], mailbox.created[0].message_id)
        self.assertIn("physical spec", entries[0]["body"]["facts"][0])
        self.assertNotIn("stale spec", entries[0]["body"]["facts"][0])
        self.assertTrue(self.flow.subject_capture_unknown)

    def test_uncertain_and_rejected_outbox_events_with_core_error_do_not_claim_delivery(self):
        handoffs, mailbox = self._handoffs([MailboxStatus.UNKNOWN])
        core_error = OSError("core journal unavailable")

        def send(outbound, expected_event):
            done = Event()
            observed = []

            def core_listener(event, _snapshot):
                if event == expected_event:
                    raise core_error

            capture = self.flow._subject_listener(outbound, core_listener)

            def listener(event, snapshot, *, delivered_message=None):
                try:
                    capture(event, snapshot, delivered_message=delivered_message)
                except Exception as exc:
                    observed.append((event, exc))
                    raise
                finally:
                    if event == expected_event:
                        done.set()

            self.assertEqual(handoffs.enqueue(outbound, origin="independent", listener=listener)["status"],
                             "queued")
            self.assertTrue(done.wait(3), observed)
            self.assertEqual(observed, [(expected_event, core_error)])
            self.assertIs(observed[0][1], core_error)

        uncertain = OutboundMessage(self.task_id, 4, self.run_id, ActorRole.WORKER, ActorRole.MANAGER,
                                    MessageKind.REPORT, {"message": "possibly submitted"})
        send(uncertain, "unknown")
        self.assertEqual(len(mailbox.created), 1)
        self.assertEqual(self._entries(), [])

        def broken_prepare(_payload):
            raise RuntimeError("fixture prepare error")

        rejected = OutboundMessage(self.task_id, 4, self.run_id, ActorRole.WORKER, ActorRole.MANAGER,
                                   MessageKind.REPORT, {"message": "never submitted"}, prepare=broken_prepare)
        send(rejected, "rejected")
        self.assertEqual(len(mailbox.created), 1)
        self.assertEqual(self._entries(), [])


if __name__ == "__main__":
    unittest.main()
