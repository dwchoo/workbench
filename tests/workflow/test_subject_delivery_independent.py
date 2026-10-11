"""Independent confirmed-message and verified-decision copy boundaries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from uuid import uuid4

from tests.workflow import test_run as runtime_fixture
from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.storage.important_log import ImportantLogStore, SubjectLifecycle
from workbench.storage.important_log.store import ImportantLogStoreError
from workbench.workflow.run import _copy_confirmed_message


AT = "2026-10-09T02:03:04Z"


class IndependentConfirmedDelivery(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="dvl045-delivery-", dir="/dev/shm")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "important"
        with ImportantLogStore.create(self.directory) as store:
            self.db_uuid = store.db_uuid
        self.sink = SubjectLifecycle(self.directory, self.db_uuid)
        self.task_id = str(uuid4())
        self.subject = {"schemaVersion": 1, "kind": "task", "id": self.task_id}

    def entries(self) -> list[dict]:
        with ImportantLogStore.open(self.directory, self.db_uuid) as store:
            return store.list_entries(self.subject)

    def test_only_processed_message_is_copied_and_same_physical_id_cannot_change_payload(self) -> None:
        message = SimpleNamespace(task_id=self.task_id, revision=1, message_id=str(uuid4()),
                                  payload={"stage": "execute", "instruction": "bounded Task instruction"})
        for status in (MailboxStatus.DEFERRED, MailboxStatus.UNKNOWN, MailboxStatus.API_RETURNED):
            _copy_confirmed_message(self.sink, message, SimpleNamespace(status=status), "manager_instruction")
        self.assertEqual(self.entries(), [])
        processed = SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)
        _copy_confirmed_message(self.sink, message, processed, "manager_instruction")
        first = self.entries()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["sources"][0]["source"]["messageId"], message.message_id)
        self.assertIn("bounded Task instruction", first[0]["body"]["facts"][0])
        self.assertNotIn("full TaskSpec", str(first))
        _copy_confirmed_message(self.sink, message, processed, "manager_instruction")
        self.assertEqual(self.entries(), first)
        changed = SimpleNamespace(**{**vars(message), "payload": {"stage": "execute", "instruction": "different"}})
        with self.assertRaises(ImportantLogStoreError):
            _copy_confirmed_message(self.sink, changed, processed, "manager_instruction")
        self.assertEqual(self.entries(), first)

    def test_delivery_port_rejects_non_task_chat_and_revision_rebinding(self) -> None:
        with self.assertRaises(ValueError):
            self.sink.record_delivery({"schemaVersion": 1, "kind": "command", "id": str(uuid4())},
                                      message_id=str(uuid4()), revision=1, kind="manager_instruction",
                                      content="private chat", delivered_at=AT)
        with self.assertRaises(ValueError):
            self.sink.record_delivery(self.subject, message_id=str(uuid4()), revision=1,
                                      kind="generic_user_chat", content="private chat", delivered_at=AT)
        message_id = str(uuid4())
        self.assertTrue(self.sink.record_delivery(self.subject, message_id=message_id, revision=1,
                                                  kind="task_spec", content="delivered approved spec", delivered_at=AT))
        with self.assertRaises(ImportantLogStoreError):
            self.sink.record_delivery(self.subject, message_id=message_id, revision=2,
                                      kind="task_spec", content="delivered approved spec", delivered_at=AT)
        self.assertEqual(len(self.entries()), 1)


class IndependentActualWorkflowDecision(unittest.TestCase):
    setUp = runtime_fixture.WorkflowRuntimeTests.setUp
    strict_task = runtime_fixture.WorkflowRuntimeTests.strict_task

    def test_unverified_worker_response_is_not_copied_as_decision(self) -> None:
        directory = self.root / "important"
        with ImportantLogStore.create(directory) as store:
            db_uuid = store.db_uuid
        sink = SubjectLifecycle(directory, db_uuid)
        workflow, _mailbox, worker_port, _state, task_id = self.strict_task()
        workflow.subject_log = sink
        worker_port.mismatch = True
        with self.assertRaises(ValueError):
            workflow.start(task_id, 1, worktree_path=self.root / "rejected-response",
                           artifacts_root=self.artifacts, automation=runtime_fixture.AUTOMATION,
                           environment_values={"PATH": "/usr/bin:/bin", "TERM": "xterm", "CW10_SECRET": "fixture"})
        with ImportantLogStore.open(directory, db_uuid) as store:
            entries = store.list_entries({"schemaVersion": 1, "kind": "task", "id": task_id})
        self.assertTrue(entries, "processed instruction should remain a confirmed capture")
        self.assertNotIn("decision", {entry["sources"][0]["source"]["sourceKind"] for entry in entries})
        self.assertNotIn("PRIVATE-CW10-CHAT", str(entries))


if __name__ == "__main__":
    unittest.main()
