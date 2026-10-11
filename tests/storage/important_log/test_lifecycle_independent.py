"""Independent DVL-05 archival and authority boundaries, without model/runtime gates."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.backend.flow_tasks import TaskFlow
from workbench.storage.important_log import ImportantLogStore, SubjectLifecycle
from workbench.storage.important_log.store import ImportantLogStoreError


EARLY = "2026-10-09T01:00:00Z"
CLOSED = "2026-10-09T02:00:00Z"
AGAIN = "2026-10-09T03:00:00Z"


class IndependentSubjectLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="dvl045-life-", dir="/dev/shm")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "important"
        with ImportantLogStore.create(self.directory) as store:
            self.db_uuid = store.db_uuid
        self.life = SubjectLifecycle(self.directory, self.db_uuid)

    @staticmethod
    def subject(kind: str, identifier: str) -> dict:
        return {"schemaVersion": 1, "kind": kind, "id": identifier}

    @staticmethod
    def delegated(task: str, seq: int) -> dict:
        return {"seq": seq, "at": EARLY, "type": "delegated", "task_id": task, "revision": 1,
                "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())}

    def test_closed_reasons_copy_time_but_only_approved_finish_reasons_have_postprocessing_ref(self) -> None:
        reasons = ("done", "report_outcome_unknown", "report_not_delivered", "cancelled",
                   "approval_retired_c_d66", "not_started_before_restart")
        for index, reason in enumerate(reasons):
            with self.subTest(reason=reason):
                task = str(uuid4())
                owner = self.subject("task", task)
                grant = self.delegated(task, 10 * index + 1)
                self.assertTrue(self.life.record_flow(grant))
                status = {"seq": 10 * index + 2, "at": EARLY, "type": "task",
                          "task": {"task_id": task, "revision": 1, "status": "finished"}}
                self.assertTrue(self.life.record_flow(status))
                self.assertEqual(self.life.state(owner)["archival_state"], "open")
                close = {"seq": 10 * index + 3, "at": CLOSED, "type": "task_closed",
                         "task_id": task, "reason": reason}
                self.assertTrue(self.life.record_flow(close))
                saved = self.life.state(owner)
                self.assertEqual((saved["archival_state"], saved["archival_at"],
                                  saved["core_closed_reason"], saved["event_cursor"]),
                                 ("closed", CLOSED, reason, close["seq"]))
                self.assertEqual(saved["prior_grant_ref"], grant["proceed_decision_id"])
                allowed = self.life.authority(owner, active=False, paused=False, revoked=False,
                                              has_new_input=True, closing_event=True)
                self.assertEqual(allowed["allowed"], reason in reasons[:3])
                self.assertFalse(self.life.authority(owner, active=True, paused=False, revoked=False,
                                                     has_new_input=True)["allowed"])
                self.assertFalse(self.life.record_flow(close))

    def test_blocked_held_and_archival_close_do_not_invent_core_success_or_end_trigger(self) -> None:
        for index, status in enumerate(("blocked", "held")):
            task = str(uuid4())
            owner = self.subject("task", task)
            grant = self.delegated(task, index * 10 + 1)
            self.life.record_flow(grant)
            self.life.record_flow({"seq": index * 10 + 2, "at": CLOSED, "type": "task",
                                   "task": {"task_id": task, "revision": 1, "status": status}})
            before = self.life.state(owner)
            self.assertEqual((before["archival_state"], before["core_status"]), ("open", status))
            self.life.archive_close(owner, at=CLOSED, user_confirmed=True)
            after = self.life.state(owner)
            self.assertEqual((after["core_status"], after["grant_ref"], after["core_closed_reason"]),
                             (status, grant["proceed_decision_id"], None))
            self.assertFalse(self.life.authority(owner, active=False, paused=False, revoked=False,
                                                 has_new_input=True, closing_event=True)["allowed"])
            self.assertTrue(self.life.record_delivery(owner, message_id=str(uuid4()), revision=1,
                                                      kind="manager_instruction", content="confirmed follow-up",
                                                      delivered_at=AGAIN))
            self.assertEqual((self.life.state(owner)["archival_state"],
                              self.life.state(owner)["core_status"]), ("open", status))

    def test_taskless_confirmed_start_scope_unknown_and_actual_end(self) -> None:
        first, other = str(uuid4()), str(uuid4())
        for seq, command in ((1, first), (2, other)):
            self.life.record_terminal({"seq": seq, "type": "terminal_started", "at": EARLY,
                                       "command_id": command, "task_id": None, "started_at": EARLY})
        owner, stranger = self.subject("command", first), self.subject("command", other)
        first_scope = self.life.authority(owner, active=True, paused=False, revoked=False,
                                          has_new_input=True)
        other_scope = self.life.authority(stranger, active=True, paused=False, revoked=False,
                                          has_new_input=True)
        self.assertTrue(first_scope["allowed"])
        self.assertTrue(other_scope["allowed"])
        self.assertNotEqual(first_scope["authorization_ref"], other_scope["authorization_ref"])
        origin = str(uuid4())
        with self.assertRaises(ValueError):
            self.life.record_direct_origin(first, event_id=origin, epoch=1, observed_at=EARLY,
                                           verified_direct_instruction=False)
        self.life.record_direct_origin(first, event_id=origin, epoch=1, observed_at=EARLY,
                                       verified_direct_instruction=True)
        self.assertEqual(self.life.authority(owner, active=True, paused=False, revoked=False,
                                             has_new_input=True)["authorization_ref"],
                         first_scope["authorization_ref"])
        self.assertFalse(self.life.authority(owner, active=True, paused=True, revoked=False,
                                             has_new_input=True)["allowed"])
        self.assertFalse(self.life.authority(owner, active=True, paused=False, revoked=True,
                                             has_new_input=True)["allowed"])
        self.life.record_terminal({"seq": 3, "type": "terminal_ended", "at": CLOSED,
                                   "command_id": first, "status": "unknown", "ended_at": CLOSED})
        self.assertEqual(self.life.state(owner)["archival_state"], "open")
        self.assertFalse(self.life.authority(owner, active=False, paused=False, revoked=False,
                                             has_new_input=True, closing_event=True)["allowed"])
        self.life.record_terminal({"seq": 4, "type": "terminal_ended", "at": CLOSED,
                                   "command_id": other, "status": "exited", "ended_at": CLOSED})
        self.assertEqual(self.life.state(stranger)["archival_state"], "closed")
        self.assertEqual(self.life.authority(stranger, active=False, paused=False, revoked=False,
                                             has_new_input=True, closing_event=True)["authorization_ref"],
                         other_scope["authorization_ref"])

    def test_reopen_reconciles_only_confirmed_journal_rows_without_replay(self) -> None:
        task = str(uuid4())
        owner = self.subject("task", task)
        grant = self.delegated(task, 1)
        close = {"seq": 2, "at": CLOSED, "type": "task_closed", "task_id": task,
                 "reason": "report_not_delivered"}
        self.assertEqual(self.life.reconcile_flow([grant, close]), 2)
        with ImportantLogStore.open(self.directory, self.db_uuid) as store:
            before = [entry["entryId"] for entry in store.list_entries(owner)]
        reopened = SubjectLifecycle(self.directory, self.db_uuid)
        self.assertEqual(reopened.reconcile_flow([grant, close]), 0)
        self.assertEqual(reopened.state(owner)["core_closed_reason"], "report_not_delivered")
        with ImportantLogStore.open(self.directory, self.db_uuid) as store:
            self.assertEqual([entry["entryId"] for entry in store.list_entries(owner)], before)
        changed = {**close, "reason": "done"}
        with self.assertRaises(ImportantLogStoreError):
            reopened.reconcile_flow([changed])
        self.assertEqual(reopened.state(owner)["core_closed_reason"], "report_not_delivered")

    def test_task_command_is_one_task_subject_until_core_close(self) -> None:
        task, command = str(uuid4()), str(uuid4())
        owner = self.subject("task", task)
        self.life.record_flow(self.delegated(task, 1))
        self.life.record_terminal({"seq": 1, "at": EARLY, "type": "terminal_started",
                                   "command_id": command, "task_id": task, "started_at": EARLY}, revision=1)
        self.life.record_terminal({"seq": 2, "at": CLOSED, "type": "terminal_ended",
                                   "command_id": command, "status": "exited", "ended_at": CLOSED})
        self.assertEqual(self.life.state(self.subject("command", command)), {})
        self.assertEqual(self.life.state(owner)["archival_state"], "open")
        with ImportantLogStore.open(self.directory, self.db_uuid) as store:
            self.assertEqual(store.list_entries(self.subject("command", command)), [])
            self.assertEqual(len(store.list_entries(owner)), 3)
        self.life.record_flow({"seq": 3, "at": AGAIN, "type": "task_closed",
                               "task_id": task, "reason": "done"})
        self.assertEqual((self.life.state(owner)["archival_state"], self.life.state(owner)["archival_at"]),
                         ("closed", AGAIN))

    def test_confirmed_flow_close_survives_failed_important_copy_and_restart_reconciles_once(self) -> None:
        task = str(uuid4())
        owner = self.subject("task", task)
        journal = self.directory.parent / "flow.jsonl"

        class FailCloseCopy:
            def __init__(self, target: SubjectLifecycle):
                self.target = target

            def reconcile_flow(self, records: list[dict]) -> int:
                return self.target.reconcile_flow(records)

            def record_flow(self, record: dict) -> bool:
                if record["type"] == "task_closed":
                    raise OSError("simulated Important DB copy failure after journal fsync")
                return self.target.record_flow(record)

        flow = TaskFlow(journal, repository_factory=lambda: None, handoffs=None,
                        omp_idle=lambda _role: True, subject_log=FailCloseCopy(self.life))
        try:
            # HandoffJournal owns seq/at. A caller-supplied seq would overwrite its
            # monotonic sequence and create an impossible confirmed journal.
            grant = self.delegated(task, 1)
            flow._record({key: value for key, value in grant.items() if key not in ("seq", "at")})
            flow._record({"type": "task_closed", "task_id": task, "reason": "report_not_delivered"})
            self.assertTrue(flow.subject_capture_unknown)
            self.assertEqual(self.life.state(owner)["archival_state"], "open")
        finally:
            flow.close()

        confirmed = journal.read_text(encoding="utf-8")
        self.assertIn('"reason":"report_not_delivered"', confirmed)
        self.assertEqual([row["seq"] for row in flow._ledger.read_all()], [1, 2])
        restarted = TaskFlow(journal, repository_factory=lambda: None, handoffs=None,
                             omp_idle=lambda _role: True, subject_log=self.life)
        try:
            recovered = self.life.state(owner)
            self.assertEqual((recovered["archival_state"], recovered["core_closed_reason"]),
                             ("closed", "report_not_delivered"))
            self.assertFalse(restarted.subject_capture_unknown)
            with ImportantLogStore.open(self.directory, self.db_uuid) as store:
                first_ids = [entry["entryId"] for entry in store.list_entries(owner)]
        finally:
            restarted.close()
        reopened = TaskFlow(journal, repository_factory=lambda: None, handoffs=None,
                            omp_idle=lambda _role: True, subject_log=self.life)
        try:
            with ImportantLogStore.open(self.directory, self.db_uuid) as store:
                self.assertEqual([entry["entryId"] for entry in store.list_entries(owner)], first_ids)
            self.assertEqual(journal.read_text(encoding="utf-8"), confirmed)
        finally:
            reopened.close()

    def test_cursor_insert_fault_rolls_back_entry_and_grant_then_later_status_cannot_be_rewound(self) -> None:
        task = str(uuid4())
        owner = self.subject("task", task)
        grant = self.delegated(task, 1)
        finished = {"seq": 2, "at": CLOSED, "type": "task",
                    "task": {"task_id": task, "revision": 1, "status": "finished"}}

        # The fault happens after the entry and lifecycle update, at the seen
        # cursor insert. None of those writes may survive independently.
        with patch.object(SubjectLifecycle, "_seen", side_effect=OSError("cursor insert fault")):
            with self.assertRaisesRegex(OSError, "cursor insert fault"):
                self.life.record_flow(grant)
        self.assertIsNone(self.life.state(owner).get("event_cursor"))
        with ImportantLogStore.open(self.directory, self.db_uuid) as store:
            self.assertEqual(store.list_entries(owner), [])
            self.assertEqual(store._connection.execute(
                "SELECT COUNT(*) FROM subject_events_v1 WHERE subject_id=?", (task,)).fetchone()[0], 0)

        self.assertTrue(self.life.record_flow(finished))
        before = self.life.state(owner)
        self.assertEqual((before["core_status"], before["event_cursor"], before["grant_ref"]),
                         ("finished", 2, None))
        reopened = SubjectLifecycle(self.directory, self.db_uuid)
        with self.assertRaises(ImportantLogStoreError):
            reopened.reconcile_flow([finished, grant])  # unordered caller input, journal order is seq
        after = reopened.state(owner)
        self.assertEqual((after["core_status"], after["event_cursor"], after["grant_ref"]),
                         ("finished", 2, None))
        self.assertEqual(after["storage_unknown"], 1)
        self.assertFalse(reopened.authority(owner, active=True, paused=False, revoked=False,
                                            has_new_input=True)["allowed"])
        with ImportantLogStore.open(self.directory, self.db_uuid) as store:
            self.assertEqual(store.list_entries(owner), [])
            self.assertEqual(store._connection.execute(
                "SELECT event_key FROM subject_events_v1 WHERE subject_id=?", (task,)).fetchall()[0][0],
                "flow:2")


if __name__ == "__main__":
    unittest.main()
