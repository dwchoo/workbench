from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.contracts.logs_v1 import summary_input_key
from workbench.tasks.repository import InvalidTransitionError, TaskRepository
import workbench.tasks.repository as repository_module


class LogMetadataTests(unittest.TestCase):
    def test_structured_stream_no_replay_uses_message_identity_not_run(self):
        task, run = self.task_run()
        first = self.request(task, run)
        source = first["sources"][0]["source"]
        message_a, message_b = str(uuid4()), str(uuid4())
        source.update(sourceKind="manager_instruction", messageId=message_a)
        first["inputKey"] = summary_input_key(first["subject"], 1, first["sources"], [], first["skillDigest"])
        self.repo.reserve_summary_request(first)
        second = self.request(task, run)
        second["sources"][0]["source"].update(sourceKind="manager_instruction", messageId=message_b)
        second["inputKey"] = summary_input_key(second["subject"], 1, second["sources"], [], second["skillDigest"])
        self.repo.reserve_summary_request(second)
        replay = self.request(task, run)
        replay["sources"][0]["source"].update(sourceKind="manager_instruction", messageId=message_a, collectorEpoch=2)
        replay["skillDigest"] = sha256(b"new skill").hexdigest()
        replay["inputKey"] = summary_input_key(replay["subject"], 1, replay["sources"], [], replay["skillDigest"])
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(replay)
        self.repo.complete_run(run)
        self.repo.revise_task(task, {"goal": "revision two"})
        self.repo.approve_scope(task, 2, {"paths": ["src/"]})
        self.repo.proceed(task, 2, "run")
        next_run = self.repo.start_run(task, 2)
        cross_revision = self.request(task, next_run)
        cross_revision["revision"] = 2
        cross_revision["sources"][0]["source"].update(
            sourceKind="manager_instruction", messageId=message_a, revision=2)
        cross_revision["inputKey"] = summary_input_key(
            cross_revision["subject"], 2, cross_revision["sources"], [], cross_revision["skillDigest"])
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(cross_revision)
        event = self.request(task, run)
        event["sources"][0]["source"].update(sourceKind="report", eventId=str(uuid4()))
        event["inputKey"] = summary_input_key(event["subject"], 1, event["sources"], [], event["skillDigest"])
        self.repo.reserve_summary_request(event)

    def test_negative_proof_rechecks_later_gap_in_same_physical_stream(self):
        task, run = self.task_run()
        request = self.request(task, run)
        source = request["sources"][0]["source"]
        observed = self.repo.record_log_source(request["subject"], request["sources"][0])
        coverage_id = self.coverage(source, [observed], 3)
        unrelated = {**source, "sourceKind": "event", "eventId": str(uuid4())}
        self.repo.record_log_source(request["subject"], {**request["sources"][0], "source": unrelated},
                                    status="collector_gap")
        condition = sha256(b"condition").hexdigest()
        proof = self.repo.record_condition_proof(task, 1, run, condition, matched=False,
                                                 observation_complete=True, coverage_id=coverage_id)
        self.assertEqual(self.repo.get_condition_proofs(run)[0]["proof_id"], proof)
        self.assertEqual(self.repo.get_condition_proofs(run)[0]["observation_complete"], 1)
        matched_complete = self.repo.record_condition_proof(
            task, 1, run, condition, matched=True, observation_complete=True,
            source_id=observed, coverage_id=coverage_id, match_start=0, match_end=3,
            full_match_digest=sha256(b"abc").hexdigest(), excerpt=b"abc")
        late_gap = {**request["sources"][0], "source": {**source, "collectorEpoch": 2}}
        self.repo.record_log_source(request["subject"], late_gap, status="collector_gap")
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_condition_proof(task, 1, run, condition, matched=False,
                                             observation_complete=True, coverage_id=coverage_id)
        view = self.repo.get_condition_proofs(run)
        self.assertEqual(len(view), 2)
        by_id = {row["proof_id"]: row for row in view}
        self.assertEqual(by_id[proof]["observation_complete"], 0)
        self.assertEqual(by_id[proof]["invalidation_reason"], "coverage_source_gap")
        self.assertEqual(by_id[matched_complete]["matched"], 1)
        self.assertEqual(by_id[matched_complete]["observation_complete"], 0)
        raw = self.repo._connection.execute(
            "SELECT observation_complete FROM log_condition_proofs WHERE proof_id=?", (proof,)).fetchone()
        self.assertEqual(raw[0], 1)
        self.repo.close()
        self.repo = TaskRepository(self.path)
        reopened = self.repo.get_condition_proofs(run)
        reopened_by_id = {row["proof_id"]: row for row in reopened}
        self.assertEqual(reopened_by_id[proof]["observation_complete"], 0)
        self.assertEqual(reopened_by_id[proof]["invalidation_reason"], "coverage_source_gap")
        matched = self.repo.record_condition_proof(
            task, 1, run, condition, matched=True, observation_complete=False,
            source_id=observed, match_start=0, match_end=3,
            full_match_digest=sha256(b"abc").hexdigest(), excerpt=b"abc")
        matched_view = next(row for row in self.repo.get_condition_proofs(run) if row["proof_id"] == matched)
        self.assertEqual(matched_view["matched"], 1)
        self.assertEqual(matched_view["observation_complete"], 0)

    def test_late_observed_interval_invalidates_old_complete_view_without_dml(self):
        task, run = self.task_run()
        request = self.request(task, run)
        source = request["sources"][0]["source"]
        observed = self.repo.record_log_source(request["subject"], request["sources"][0])
        coverage_id = self.coverage(source, [observed], 3)
        proof = self.repo.record_condition_proof(task, 1, run, sha256(b"condition").hexdigest(),
                                                 matched=False, observation_complete=True,
                                                 coverage_id=coverage_id)
        self.repo.record_log_source(request["subject"], {
            **request["sources"][0], "start": 3, "end": 4}, status="observed")
        statements = []
        self.repo._connection.set_trace_callback(statements.append)
        try:
            view = self.repo.get_condition_proofs(run)
        finally:
            self.repo._connection.set_trace_callback(None)
        self.assertEqual(view[0]["proof_id"], proof)
        self.assertEqual(view[0]["observation_complete"], 0)
        self.assertEqual(view[0]["invalidation_reason"], "coverage_source_uncovered")
        self.assertFalse(any(statement.lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN IMMEDIATE")) for statement in statements))
        stored = self.repo._connection.execute(
            "SELECT matched,observation_complete FROM log_condition_proofs WHERE proof_id=?",
            (proof,)).fetchone()
        self.assertEqual(tuple(stored), (0, 1))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.repo = TaskRepository(self.path)

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def task_run(self):
        task = str(uuid4())
        self.repo.create_task({"goal": "test"}, task_id=task)
        self.repo.approve_scope(task, 1, {"paths": ["src/"]})
        self.repo.proceed(task, 1, "run")
        return task, self.repo.start_run(task, 1)

    def request(self, task, run, *, start=0, end=3):
        subject = {"schemaVersion": 1, "kind": "task", "id": task}
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                  "taskId": task, "revision": 1, "runId": run}
        span = {"schemaVersion": 1, "source": source, "start": start, "end": end,
                "digest": sha256(b"abc").hexdigest()}
        health = {"schemaVersion": 1, "dbUuid": str(uuid4()), "measuredAt": "2026-10-09T01:02:03Z",
                  "stale": False, "mainBytes": 1, "walBytes": 0, "shmBytes": 0,
                  "totalBytes": 1, "logicalLiveBytes": 1, "freelistBytes": 0,
                  "subjectStats": [], "cleanupCandidates": None, "lastWrite": None, "lastLock": None,
                  "lastMaintenance": None, "diskFreeBytes": None,
                  "lastQuickCheck": None, "metadataOutsideBudget": True}
        skill = sha256(b"skill").hexdigest()
        return {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject,
                "revision": 1, "authorizationRef": str(uuid4()), "authorizationEpoch": 1,
                "inputKey": summary_input_key(subject, 1, [span], [], skill),
                "skillName": "workbench-important-log", "skillDigest": skill,
                "sources": [span], "sourceGaps": [], "priorCumulativeEntryId": None,
                "dbHealth": health, "requestedAt": "2026-10-09T01:02:03Z"}

    def coverage(self, source, source_ids, final_offset):
        start = {"schemaVersion": 1, "eventId": str(uuid4()), "source": source,
                 "kind": "start", "offset": 0, "observedAt": "2026-10-09T01:02:03Z"}
        final = {"schemaVersion": 1, "eventId": str(uuid4()), "source": source,
                 "kind": "final_drain", "offset": final_offset, "observedAt": "2026-10-09T01:02:04Z"}
        self.repo.record_observation_boundary(start)
        self.repo.record_observation_boundary(final)
        witness = {"schemaVersion": 1, "coverageId": str(uuid4()), "source": source,
                   "startEventId": start["eventId"], "finalEventId": final["eventId"],
                   "finalOffset": final_offset, "sourceIds": source_ids}
        self.repo.record_gapfree_coverage(witness)
        return witness["coverageId"]

    def test_source_proof_request_no_replay_first_result_and_reopen(self):
        task, run = self.task_run()
        request = self.request(task, run)
        source_id = self.repo.record_log_source(request["subject"], request["sources"][0])
        coverage_id = self.coverage(request["sources"][0]["source"], [source_id], 3)
        proof_id = self.repo.record_condition_proof(
            task, 1, run, sha256(b"condition").hexdigest(), matched=True,
            observation_complete=True, source_id=source_id, coverage_id=coverage_id, match_start=0, match_end=3,
            full_match_digest=sha256(b"abc").hexdigest(), excerpt=b"abc")
        self.assertEqual(self.repo.get_condition_proofs(run)[0]["proof_id"], proof_id)
        self.assertEqual(self.repo.get_log_sources(request["subject"])[0]["source_id"], source_id)
        self.repo.reserve_summary_request(request)
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request({**request, "requestId": str(uuid4())})
        self.repo.record_summary_state(request["requestId"], "sending")
        self.repo.record_summary_state(request["requestId"], "unknown", detail="transport result unknown")
        overlap = self.request(task, run, start=1, end=4)
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(overlap)
        later_epoch = self.request(task, run, start=0, end=3)
        later_epoch["sources"][0]["source"]["collectorEpoch"] = 2
        later_epoch["inputKey"] = summary_input_key(later_epoch["subject"], 1,
                                                     later_epoch["sources"], [], later_epoch["skillDigest"])
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(later_epoch)

        self.repo.close()
        self.repo = TaskRepository(self.path)
        self.assertEqual(self.repo.get_summary_request(request["requestId"])["state"], "unknown")
        self.assertEqual([item["request"]["requestId"] for item in self.repo.list_unresolved_summary_requests()],
                         [request["requestId"]])
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(overlap)
        result = {"schemaVersion": 1, "requestId": request["requestId"],
                  "inputKey": request["inputKey"], "skillDigest": request["skillDigest"],
                  "sources": request["sources"], "facts": ["ok"],
                  "interpretations": [], "unknowns": [],
                  "usage": {"schemaVersion": 1, "inputTokens": None,
                            "outputTokens": None, "costUsd": None}}
        entry_id = str(uuid4())
        self.assertTrue(self.repo.record_summary_result_receipt(result, entry_id))
        self.assertFalse(self.repo.record_summary_result_receipt(result, entry_id))
        self.assertEqual(self.repo.get_summary_request(request["requestId"])["receipt"]["entry_id"], entry_id)
        self.assertEqual(self.repo.list_unresolved_summary_requests(), [])
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_summary_state(request["requestId"], "sending")
        self.assertEqual(self.repo._connection.execute("SELECT COUNT(*) FROM summary_result_drops_v1").fetchone()[0], 1)

    def test_confirmed_unsent_can_merge_and_failed_insert_rolls_back(self):
        task, run = self.task_run()
        request = self.request(task, run)
        self.repo.reserve_summary_request(request)
        self.repo.record_summary_state(request["requestId"], "not_sent", detail="dispatch never started")
        next_request = {**request, "requestId": str(uuid4())}
        self.repo.reserve_summary_request(next_request)
        self.repo._connection.execute("""CREATE TRIGGER fail_range BEFORE INSERT ON summary_request_ranges_v1
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        different = self.request(task, run, start=3, end=6)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.reserve_summary_request(different)
        self.assertIsNone(self.repo.get_summary_request(different["requestId"]))

    def test_v2_migration_and_rollback_on_failed_migration(self):
        self.repo.close()
        database = sqlite3.connect(self.path)
        database.execute("PRAGMA foreign_keys=OFF")
        for table in ("log_source_records", "log_observation_boundaries", "log_observation_coverage",
                      "log_condition_proofs", "summary_requests_v1",
                      "summary_request_ranges_v1", "summary_request_events_v1",
                      "summary_result_receipts_v1", "summary_result_drops_v1"):
            database.execute(f"DROP TABLE {table}")
        database.execute("PRAGMA user_version=2")
        database.commit()
        database.close()
        with patch.object(repository_module, "MIGRATION_2_TO_3", "BEGIN IMMEDIATE; CREATE TABLE rollback_marker(x); INVALID SQL; COMMIT;"):
            with self.assertRaises(sqlite3.OperationalError):
                TaskRepository(self.path)
        database = sqlite3.connect(self.path)
        self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertIsNone(database.execute("SELECT name FROM sqlite_master WHERE name='rollback_marker'").fetchone())
        database.close()
        self.repo = TaskRepository(self.path)
        self.assertEqual(self.repo._connection.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertIsNotNone(self.repo._connection.execute("SELECT name FROM sqlite_master WHERE name='log_source_records'").fetchone())
        self.assertIsNotNone(self.repo._connection.execute("SELECT name FROM sqlite_master WHERE name='log_observation_coverage'").fetchone())

    def test_proof_requires_observed_exact_run_and_coverage(self):
        task, run = self.task_run()
        request = self.request(task, run)
        source = request["sources"][0]["source"]
        condition = sha256(b"condition").hexdigest()
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_condition_proof(task, 1, run, condition, matched=False, observation_complete=True)
        gap_id = self.repo.record_log_source(request["subject"], request["sources"][0], status="collector_gap")
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_condition_proof(task, 1, run, condition, matched=True, observation_complete=False,
                source_id=gap_id, match_start=0, match_end=3, full_match_digest=sha256(b"abc").hexdigest())
        observed_id = self.repo.record_log_source(request["subject"],
            {**request["sources"][0], "start": 3, "end": 6}, status="observed")
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_condition_proof(task, 1, run, condition, matched=True, observation_complete=False,
                source_id=observed_id, match_start=3, match_end=6, full_match_digest=sha256(b"wrong").hexdigest())
        with self.assertRaises(InvalidTransitionError):
            self.coverage(source, [], 3)
        start = {"schemaVersion": 1, "eventId": str(uuid4()), "source": source,
                 "kind": "start", "offset": 0, "observedAt": "2026-10-09T01:02:03Z"}
        final = {**start, "eventId": str(uuid4()), "kind": "final_drain"}
        self.repo.record_observation_boundary(start)
        self.repo.record_observation_boundary(final)
        foreign_epoch = {**source, "collectorEpoch": 2}
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_gapfree_coverage({"schemaVersion": 1, "coverageId": str(uuid4()),
                "source": foreign_epoch, "startEventId": start["eventId"],
                "finalEventId": final["eventId"], "finalOffset": 0, "sourceIds": []})
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_observation_boundary({**start, "eventId": str(uuid4()),
                "source": {**source, "revision": 2}})
        self.repo.close()
        self.repo = TaskRepository(self.path)
        empty_task, empty_run = self.task_run()
        empty_source = self.request(empty_task, empty_run)["sources"][0]["source"]
        self.assertEqual(self.coverage(empty_source, [], 0), self.repo._connection.execute(
            "SELECT coverage_id FROM log_observation_coverage ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        coverage_id = self.repo._connection.execute("SELECT coverage_id FROM log_observation_coverage ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        proof_id = self.repo.record_condition_proof(empty_task, 1, empty_run, condition, matched=False,
            observation_complete=True, coverage_id=coverage_id)
        self.assertEqual(self.repo.get_condition_proofs(empty_run)[0]["proof_id"], proof_id)

    def test_stored_requires_receipt_and_gaps_are_journaled(self):
        task, run = self.task_run()
        request = self.request(task, run)
        source = request["sources"][0]["source"]
        gap = {"schemaVersion": 1, "source": source, "start": 0, "end": 3, "reason": "collector_gap"}
        request["sources"] = []
        request["sourceGaps"] = [gap]
        request["inputKey"] = summary_input_key(request["subject"], 1, [], [gap], request["skillDigest"])
        self.repo.reserve_summary_request(request)
        self.assertEqual(self.repo._connection.execute("SELECT range_kind FROM summary_request_ranges_v1").fetchone()[0], "gap")
        self.repo.record_summary_state(request["requestId"], "sending")
        self.repo.record_summary_state(request["requestId"], "unknown")
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_summary_state(request["requestId"], "stored")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo._connection.execute("INSERT INTO summary_request_events_v1(request_id,state,detail,created_at) VALUES(?,'stored',?,?)",
                (request["requestId"], str(uuid4()), "2026-10-09T01:02:05Z"))
        retry = self.request(task, run)
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(retry)
        gap2 = {**gap, "reason": "unknown"}
        retry["sources"] = []
        retry["sourceGaps"] = [gap2]
        retry["skillDigest"] = sha256(b"different skill").hexdigest()
        retry["inputKey"] = summary_input_key(retry["subject"], 1, [], [gap2], retry["skillDigest"])
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(retry)
        bad_revision = self.request(task, run)
        bad_revision["revision"] = 2
        bad_revision["sources"][0]["source"]["revision"] = 2
        bad_revision["inputKey"] = summary_input_key(bad_revision["subject"], 2,
            bad_revision["sources"], [], bad_revision["skillDigest"])
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(bad_revision)
