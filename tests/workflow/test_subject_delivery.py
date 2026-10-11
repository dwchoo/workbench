"""DVL-05: experiment mailbox receipt와 중요 기록의 경계."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from uuid import uuid4

from workbench.ipc.bridge_g3.mailbox import MailboxStatus
from workbench.storage.important_log import ImportantLogStore, SubjectLifecycle
from workbench.workflow.run import _copy_confirmed_decision, _copy_confirmed_message
from workbench.workflow import TaskWorkflow
import test_run as fixture


class ExperimentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "important"
        with ImportantLogStore.create(self.directory) as store:
            self.db_uuid = store.db_uuid
        self.sink = SubjectLifecycle(self.directory, self.db_uuid)
        self.task_id = str(uuid4())
        self.subject = {"schemaVersion": 1, "kind": "task", "id": self.task_id}

    def _entries(self):
        with ImportantLogStore.open(self.directory, self.db_uuid) as store:
            return store.list_entries(self.subject)

    def test_only_processed_receipt_and_verified_decision_are_idempotent(self):
        message = SimpleNamespace(task_id=self.task_id, revision=2, message_id=str(uuid4()),
                                  payload={"stage": "execute", "instruction": "approved run"})
        for status in (MailboxStatus.DEFERRED, MailboxStatus.UNKNOWN, MailboxStatus.API_RETURNED):
            _copy_confirmed_message(self.sink, message, SimpleNamespace(status=status), "manager_instruction")
        self.assertEqual(self._entries(), [])
        receipt = SimpleNamespace(status=MailboxStatus.OMP_PROCESSED)
        _copy_confirmed_message(self.sink, message, receipt, "manager_instruction")
        _copy_confirmed_message(self.sink, message, receipt, "manager_instruction")
        response = {"response_id": str(uuid4()), "decision": "execute"}
        _copy_confirmed_decision(self.sink, self.task_id, 2, response)
        _copy_confirmed_decision(self.sink, self.task_id, 2, response)
        entries = self._entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual({entry["sources"][0]["source"]["sourceKind"] for entry in entries},
                         {"manager_instruction", "decision"})


class ActualExperimentDeliveryTests(unittest.TestCase):
    setUp = fixture.WorkflowRuntimeTests.setUp
    approved_task = fixture.WorkflowRuntimeTests.approved_task

    def test_processed_experiment_task_and_report_copy_without_core_result_change(self):
        directory = self.root / "important"
        with ImportantLogStore.create(directory) as store:
            db_uuid = store.db_uuid
        sink = SubjectLifecycle(directory, db_uuid)
        workflow = TaskWorkflow(self.repository, self.mailbox, subject_log=sink)
        task_id = self.approved_task("printf 'PASS\\n'; printf PASS > outcome.txt")
        run = workflow.start(task_id, 1, worktree_path=self.root / "experiment",
                             artifacts_root=self.artifacts, automation=fixture.AUTOMATION)
        self.addCleanup(run.close)
        self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
        self.assertEqual(run.judge()["worker_judgment"]["judgment"], "success")
        self.assertFalse(workflow.subject_capture_unknown)
        self.assertFalse(run.subject_capture_unknown)
        with ImportantLogStore.open(directory, db_uuid) as store:
            entries = store.list_entries({"schemaVersion": 1, "kind": "task", "id": task_id})
        self.assertEqual({entry["sources"][0]["source"]["sourceKind"] for entry in entries},
                         {"manager_instruction", "report"})


if __name__ == "__main__":
    unittest.main()
