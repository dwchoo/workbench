"""DVL-05: confirmed subject facts, archival state and bounded fixture authority."""

from pathlib import Path
from hashlib import sha256
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from workbench.storage.important_log import ImportantLogStore, SubjectLifecycle
from workbench.storage.important_log.store import ImportantLogStoreError


AT = "2026-10-09T12:00:00Z"
LATER = "2026-10-09T12:01:00Z"


class SubjectLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "important"
        with ImportantLogStore.create(self.root) as store:
            self.db_uuid = store.db_uuid
        self.life = SubjectLifecycle(self.root, self.db_uuid)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def subject(kind, identifier):
        return {"schemaVersion": 1, "kind": kind, "id": identifier}

    def delegated(self, task_id, seq=1):
        return {"seq": seq, "at": AT, "type": "delegated", "task_id": task_id, "revision": 1,
                "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())}

    def test_task_closure_preserves_reason_time_prior_grant_and_no_core_success(self):
        task_id = str(uuid4())
        subject = self.subject("task", task_id)
        delegated = self.delegated(task_id)
        self.assertTrue(self.life.record_flow(delegated))
        self.assertFalse(self.life.record_flow(delegated))
        status = {"seq": 2, "at": AT, "type": "task", "task": {"task_id": task_id,
                                                                     "revision": 1, "status": "finished"}}
        self.life.record_flow(status)
        self.assertEqual(self.life.state(subject)["archival_state"], "open")
        self.assertTrue(self.life.authority(subject, active=True, paused=False, revoked=False,
                                            has_new_input=True)["allowed"])
        closed = {"seq": 3, "at": LATER, "type": "task_closed", "task_id": task_id, "reason": "done"}
        self.life.record_flow(closed)
        state = self.life.state(subject)
        self.assertEqual((state["archival_at"], state["core_closed_reason"], state["event_cursor"]),
                         (LATER, "done", 3))
        self.assertEqual(state["prior_grant_ref"], delegated["proceed_decision_id"])
        self.assertTrue(self.life.authority(subject, active=False, paused=False, revoked=False,
                                            has_new_input=True, closing_event=True)["allowed"])
        self.assertFalse(self.life.authority(subject, active=True, paused=True, revoked=False,
                                             has_new_input=True, closing_event=True)["allowed"])
        self.assertFalse(self.life.authority(subject, active=True, paused=False, revoked=True,
                                             has_new_input=True, closing_event=True)["allowed"])
        self.assertFalse(self.life.authority(subject, active=True, paused=False, revoked=False,
                                             has_new_input=True)["allowed"])
        with ImportantLogStore.open(self.root, self.db_uuid) as store:
            self.assertEqual([entry["kind"] for entry in store.list_entries(subject)], ["event", "closure"])
        self.assertFalse(self.life.record_flow(closed))
        self.assertEqual(self.life.reconcile_flow([delegated, status, closed]), 0)

    def test_other_closed_reasons_collect_without_call_permission(self):
        for index, reason in enumerate(("cancelled", "approval_retired_c_d66", "not_started_before_restart",
                                        "superseded_by_new_task")):
            task_id = str(uuid4())
            subject = self.subject("task", task_id)
            self.life.record_flow(self.delegated(task_id, seq=100 + 2 * index))
            self.life.record_flow({"seq": 101 + 2 * index, "at": LATER, "type": "task_closed",
                                   "task_id": task_id, "reason": reason})
            self.assertEqual(self.life.state(subject)["archival_state"], "closed")
            self.assertFalse(self.life.authority(subject, active=False, paused=False, revoked=False,
                                                 has_new_input=True, closing_event=True)["allowed"])

    def test_taskless_command_start_scope_unknown_end_and_archival_reactivity(self):
        command_id = str(uuid4())
        subject = self.subject("command", command_id)
        started = {"seq": 1, "at": AT, "type": "terminal_started", "command_id": command_id,
                   "task_id": None, "started_at": AT}
        self.life.record_terminal(started)
        self.assertEqual(self.life.state(subject)["archival_state"], "open")
        admitted = self.life.authority(subject, active=True, paused=False, revoked=False,
                                       has_new_input=True)
        self.assertTrue(admitted["allowed"])
        self.assertEqual(admitted["authorization_epoch"], started["seq"])
        with self.assertRaises(ValueError):
            self.life.record_direct_origin(command_id, event_id=str(uuid4()), epoch=1, observed_at=AT,
                                           verified_direct_instruction=False)
        origin = str(uuid4())
        self.life.record_direct_origin(command_id, event_id=origin, epoch=1, observed_at=AT,
                                       verified_direct_instruction=True)
        unrelated = str(uuid4())
        self.life.record_terminal({"seq": 9, "at": AT, "type": "terminal_started",
                                   "command_id": unrelated, "task_id": None, "started_at": AT})
        with self.assertRaises(ValueError):
            self.life.record_direct_origin(unrelated, event_id=str(uuid4()), epoch=1,
                                           observed_at="2026-10-09T12:00:01Z",
                                           verified_direct_instruction=True)
        with self.assertRaises(sqlite3.IntegrityError):
            self.life.record_direct_origin(unrelated, event_id=origin, epoch=1, observed_at=AT,
                                           verified_direct_instruction=True)
        unrelated_scope = self.life.authority(self.subject("command", unrelated), active=True,
                                               paused=False, revoked=False, has_new_input=True)
        self.assertTrue(unrelated_scope["allowed"])
        self.assertNotEqual(unrelated_scope["authorization_ref"], admitted["authorization_ref"])
        self.assertEqual(self.life.authority(subject, active=True, paused=False, revoked=False,
                                              has_new_input=True)["authorization_ref"],
                         admitted["authorization_ref"])
        unknown = {"seq": 2, "at": LATER, "type": "terminal_ended", "command_id": command_id,
                   "status": "unknown", "ended_at": LATER}
        self.life.record_terminal(unknown)
        self.assertEqual(self.life.state(subject)["archival_state"], "open")
        self.assertFalse(self.life.authority(subject, active=False, paused=False, revoked=False,
                                             has_new_input=True, closing_event=True)["allowed"])
        self.life.archive_close(subject, at=LATER, user_confirmed=True)
        self.assertEqual(self.life.state(subject)["archival_reason"], "user_archival_close")
        self.assertFalse(self.life.authority(subject, active=False, paused=False, revoked=False,
                                             has_new_input=True, closing_event=True)["allowed"])

    def test_task_command_deduplicates_under_task_and_delivery_late_does_not_reopen_closed(self):
        task_id, command_id, message_id = str(uuid4()), str(uuid4()), str(uuid4())
        subject = self.subject("task", task_id)
        self.life.record_flow(self.delegated(task_id))
        start = {"seq": 1, "at": AT, "type": "terminal_started", "command_id": command_id,
                 "task_id": task_id, "started_at": AT}
        self.life.record_terminal(start, revision=1)
        self.life.record_terminal({"seq": 2, "at": LATER, "type": "terminal_ended", "command_id": command_id,
                                   "status": "exited", "ended_at": LATER})
        self.assertEqual(self.life.state(subject)["archival_state"], "open")
        self.assertEqual(self.life.state(self.subject("command", command_id)), {})
        self.life.record_flow({"seq": 2, "at": LATER, "type": "task_closed", "task_id": task_id,
                               "reason": "report_not_delivered"})
        self.life.record_delivery(subject, message_id=message_id, revision=1, kind="report",
                                  content="delivered report", delivered_at=LATER)
        self.assertEqual(self.life.state(subject)["archival_state"], "closed")
        self.assertFalse(self.life.record_delivery(subject, message_id=message_id, revision=1,
                                                   kind="report", content="delivered report", delivered_at=LATER))
        with ImportantLogStore.open(self.root, self.db_uuid) as store:
            self.assertEqual(len(store.list_entries(subject)), 5)

    def test_actual_command_end_allows_only_its_end_summary_then_expires(self):
        command_id = str(uuid4())
        subject = self.subject("command", command_id)
        self.life.record_terminal({"seq": 31, "at": AT, "type": "terminal_started",
                                   "command_id": command_id, "task_id": None, "started_at": AT})
        start_ref = self.life.authority(subject, active=True, paused=False, revoked=False,
                                        has_new_input=True)["authorization_ref"]
        origin = str(uuid4())
        self.life.record_direct_origin(command_id, event_id=origin, epoch=2, observed_at=AT,
                                       verified_direct_instruction=True)
        self.life.record_terminal({"seq": 32, "at": LATER, "type": "terminal_ended",
                                   "command_id": command_id, "status": "exited", "ended_at": LATER})
        state = self.life.state(subject)
        self.assertEqual((state["archival_state"], state["archival_at"], state["archival_reason"]),
                         ("closed", LATER, "actual_end"))
        self.assertFalse(self.life.authority(subject, active=True, paused=False, revoked=False,
                                             has_new_input=True)["allowed"])
        self.assertEqual(self.life.authority(subject, active=False, paused=False, revoked=False,
                                             has_new_input=True, closing_event=True)["authorization_ref"],
                         start_ref)
        self.life.mark_command_end_summary_processed(command_id)
        self.assertFalse(self.life.authority(subject, active=False, paused=False, revoked=False,
                                             has_new_input=True, closing_event=True)["allowed"])

    def test_user_archival_close_does_not_change_core_or_grant_and_confirmed_activity_reopens(self):
        task_id = str(uuid4())
        subject = self.subject("task", task_id)
        delegated = self.delegated(task_id, seq=41)
        self.life.record_flow(delegated)
        self.life.archive_close(subject, at=LATER, user_confirmed=True)
        before = self.life.state(subject)
        self.assertEqual(before["archival_state"], "closed")
        self.assertEqual(before["grant_ref"], delegated["proceed_decision_id"])
        self.assertEqual(before["core_status"], "dispatched")
        self.life.record_delivery(subject, message_id=str(uuid4()), revision=1,
                                  kind="manager_instruction", content="confirmed Task follow-up",
                                  delivered_at=LATER)
        after = self.life.state(subject)
        self.assertEqual(after["archival_state"], "open")
        self.assertIsNone(after["archival_at"])
        self.assertEqual(after["core_status"], "dispatched")

    def test_only_supplied_terminal_byte_range_is_linked_without_invented_output(self):
        command_id = str(uuid4())
        subject = self.subject("command", command_id)
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                  "commandId": command_id}
        span = {"schemaVersion": 1, "source": source, "start": 0, "end": 3,
                "digest": sha256(b"abc").hexdigest()}
        source_record_id = str(uuid4())
        with self.assertRaisesRegex(ValueError, "confirmed command start"):
            self.life.record_terminal_range(subject, source_record_id, span, observed_at=AT)
        self.life.record_terminal({"type": "terminal_started", "command_id": command_id,
                                   "task_id": None, "started_at": AT, "seq": 1})
        other = self.subject("command", str(uuid4()))
        with self.assertRaisesRegex(ValueError, "identify its command"):
            self.life.record_terminal_range(other, source_record_id, span, observed_at=AT)
        self.assertTrue(self.life.record_terminal_range(subject, source_record_id, span, observed_at=AT))
        self.assertFalse(self.life.record_terminal_range(subject, source_record_id, span, observed_at=AT))
        with ImportantLogStore.open(self.root, self.db_uuid) as store:
            entries = store.list_entries(subject)
        self.assertEqual(len(entries), 2)
        terminal = [entry for entry in entries if entry["sources"][0]["source"]["sourceKind"] == "terminal"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["sources"], [span])
        self.assertNotIn("abc", terminal[0]["body"]["facts"][0])
        self.assertEqual(self.life.state(subject)["archival_state"], "open")

    def test_failed_delegated_cursor_cannot_rewind_finished_on_restart(self):
        task_id = str(uuid4())
        subject = self.subject("task", task_id)
        delegated = self.delegated(task_id)
        finished = {"seq": 2, "at": LATER, "type": "task", "task": {
            "task_id": task_id, "revision": 1, "status": "finished"}}
        closed = {"seq": 3, "at": LATER, "type": "task_closed",
                  "task_id": task_id, "reason": "done"}
        with ImportantLogStore.open(self.root, self.db_uuid) as store:
            store._connection.execute("""CREATE TRIGGER fail_delegated_seen
                BEFORE INSERT ON subject_events_v1 WHEN NEW.event_kind='delegated'
                BEGIN SELECT RAISE(ABORT, 'injected cursor failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.life.record_flow(delegated)
        with ImportantLogStore.open(self.root, self.db_uuid) as store:
            self.assertEqual(store.list_entries(subject), [])
            self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM subject_stats").fetchone()[0], 0)
            self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM subject_events_v1").fetchone()[0], 0)
            store._connection.execute("DROP TRIGGER fail_delegated_seen")
        self.assertEqual(self.life.state(subject), {})
        self.assertTrue(self.life.record_flow(finished))
        reopened = SubjectLifecycle(self.root, self.db_uuid)
        with self.assertRaisesRegex(ImportantLogStoreError, "precedes durable subject cursor"):
            reopened.reconcile_flow([closed, finished, delegated])
        state = reopened.state(subject)
        self.assertEqual((state["core_status"], state["event_cursor"], state["core_closed_reason"]),
                         ("closed", 3, "done"))
        self.assertIsNone(state["grant_ref"])
        self.assertEqual(state["storage_unknown"], 1)
        self.assertEqual(reopened.authority(subject, active=True, paused=False, revoked=False,
                                            has_new_input=True)["reason"], "storage_unknown")
        with ImportantLogStore.open(self.root, self.db_uuid) as store:
            events = [row[0] for row in store._connection.execute(
                "SELECT event_key FROM subject_events_v1 ORDER BY event_key")]
        self.assertEqual(events, ["flow:2", "flow:3"])

    def test_reconcile_flow_orders_unseen_confirmed_events_by_journal_sequence(self):
        task_id = str(uuid4())
        subject = self.subject("task", task_id)
        delegated = self.delegated(task_id)
        finished = {"seq": 2, "at": AT, "type": "task", "task": {
            "task_id": task_id, "revision": 1, "status": "finished"}}
        closed = {"seq": 3, "at": LATER, "type": "task_closed",
                  "task_id": task_id, "reason": "done"}
        self.assertEqual(self.life.reconcile_flow([closed, finished, delegated]), 3)
        state = self.life.state(subject)
        self.assertEqual((state["core_status"], state["prior_grant_ref"], state["event_cursor"],
                          state["storage_unknown"]),
                         ("closed", delegated["proceed_decision_id"], 3, 0))

    def test_older_status_from_other_revision_cannot_override_newer_grant(self):
        task_id = str(uuid4())
        subject = self.subject("task", task_id)
        latest = {**self.delegated(task_id, seq=10), "revision": 2}
        self.life.record_flow(latest)
        stale = {"seq": 9, "at": LATER, "type": "task", "task": {
            "task_id": task_id, "revision": 1, "status": "finished"}}
        with self.assertRaisesRegex(ImportantLogStoreError, "precedes durable subject cursor"):
            self.life.record_flow(stale)
        state = self.life.state(subject)
        self.assertEqual((state["core_status"], state["grant_revision"], state["event_cursor"],
                          state["storage_unknown"]), ("dispatched", 2, 10, 1))
        self.assertEqual(self.life.authority(subject, active=True, paused=False, revoked=False,
                                             has_new_input=True)["reason"], "storage_unknown")

    def test_failed_subject_update_or_terminal_delivery_cursor_rolls_back_entry(self):
        task_id = str(uuid4())
        subject = self.subject("task", task_id)
        delegated = self.delegated(task_id)
        with ImportantLogStore.open(self.root, self.db_uuid) as store:
            store._connection.execute("""CREATE TRIGGER fail_grant_update
                BEFORE UPDATE ON subject_lifecycle_v1 WHEN NEW.core_status='dispatched'
                BEGIN SELECT RAISE(ABORT, 'injected grant update failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.life.record_flow(delegated)
        with ImportantLogStore.open(self.root, self.db_uuid) as store:
            self.assertEqual(store.list_entries(subject), [])
            self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM subject_stats").fetchone()[0], 0)
            store._connection.execute("DROP TRIGGER fail_grant_update")
        self.assertEqual(self.life.state(subject), {})
        self.assertTrue(self.life.record_flow(delegated))

        command_id = str(uuid4())
        command = self.subject("command", command_id)
        with ImportantLogStore.open(self.root, self.db_uuid) as store:
            store._connection.execute("""CREATE TRIGGER fail_terminal_seen
                BEFORE INSERT ON subject_events_v1 WHEN NEW.event_kind='terminal_started'
                BEGIN SELECT RAISE(ABORT, 'injected terminal cursor failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.life.record_terminal({"seq": 1, "type": "terminal_started", "command_id": command_id,
                                       "task_id": None, "started_at": AT})
        with ImportantLogStore.open(self.root, self.db_uuid) as store:
            self.assertEqual(store.list_entries(command), [])
            self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM subject_commands_v1").fetchone()[0], 0)
            store._connection.execute("DROP TRIGGER fail_terminal_seen")
        self.assertEqual(self.life.state(command), {})

        message_id = str(uuid4())
        with ImportantLogStore.open(self.root, self.db_uuid) as store:
            store._connection.execute("""CREATE TRIGGER fail_delivery_seen
                BEFORE INSERT ON subject_events_v1 WHEN NEW.event_kind='report@1'
                BEGIN SELECT RAISE(ABORT, 'injected delivery cursor failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.life.record_delivery(subject, message_id=message_id, revision=1, kind="report",
                                      content="confirmed report", delivered_at=LATER)
        with ImportantLogStore.open(self.root, self.db_uuid) as store:
            self.assertEqual(len(store.list_entries(subject)), 1)  # only the successful delegation
            store._connection.execute("DROP TRIGGER fail_delivery_seen")
        self.assertTrue(self.life.record_delivery(subject, message_id=message_id, revision=1, kind="report",
                                                  content="confirmed report", delivered_at=LATER))


if __name__ == "__main__":
    unittest.main()
