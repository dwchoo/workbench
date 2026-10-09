"""Independent additive metadata, proof, and durable no-replay tests."""
from __future__ import annotations

import copy
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

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".workflow/dvl-local"
STAMP = "2026-10-09T01:02:03Z"


class IndependentLogMetadata(unittest.TestCase):
    def setUp(self) -> None:
        LOCAL.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=LOCAL)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "tasks.sqlite3"
        self.repo = TaskRepository(self.path)
        self.addCleanup(lambda: self.repo.close())

    def request(self, command_id: str, *, start: int = 0, end: int = 3, epoch: int = 1) -> dict:
        subject = {"schemaVersion": 1, "kind": "command", "id": command_id}
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": epoch,
                  "commandId": command_id}
        span = {"schemaVersion": 1, "source": source, "start": start, "end": end,
                "digest": sha256(b"abc").hexdigest()}
        skill = sha256(b"fixed skill bytes").hexdigest()
        health = {"schemaVersion": 1, "dbUuid": str(uuid4()), "measuredAt": STAMP,
                  "stale": False, "mainBytes": 1, "walBytes": 0, "shmBytes": 0,
                  "totalBytes": 1, "logicalLiveBytes": 1, "freelistBytes": 0,
                  "subjectStats": [], "cleanupCandidates": None, "lastWrite": None,
                  "lastLock": None, "lastMaintenance": None, "diskFreeBytes": None,
                  "lastQuickCheck": None, "metadataOutsideBudget": True}
        return {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject,
                "revision": None, "authorizationRef": str(uuid4()), "authorizationEpoch": 1,
                "inputKey": summary_input_key(subject, None, [span], [], skill),
                "skillName": "important-log", "skillDigest": skill, "sources": [span],
                "sourceGaps": [], "priorCumulativeEntryId": None, "dbHealth": health,
                "requestedAt": STAMP}

    def result(self, request: dict, *, fact: str = "PRIVATE-IMPORTANT-BODY-SENTINEL") -> dict:
        return {"schemaVersion": 1, "requestId": request["requestId"],
                "inputKey": request["inputKey"], "skillDigest": request["skillDigest"],
                "sources": request["sources"], "facts": [fact], "interpretations": [],
                "unknowns": [], "usage": {"schemaVersion": 1, "inputTokens": None,
                                         "outputTokens": None, "costUsd": None}}

    def test_unknown_input_range_remains_reserved_after_reopen_and_late_receipt(self) -> None:
        command = str(uuid4())
        request = self.request(command)
        self.repo.reserve_summary_request(request)
        self.repo.record_summary_state(request["requestId"], "sending")
        self.repo.record_summary_state(request["requestId"], "unknown", detail="transport outcome unavailable")
        self.repo.close()
        self.repo = TaskRepository(self.path)
        self.assertEqual(self.repo.get_summary_request(request["requestId"])["state"], "unknown")
        overlap = self.request(command, start=1, end=4, epoch=2)
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(overlap)
        exact = copy.deepcopy(request)
        exact["requestId"] = str(uuid4())
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(exact)
        wrong = self.result(request)
        wrong["skillDigest"] = "0" * 64
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_summary_result_receipt(wrong, str(uuid4()))
        self.assertEqual(self.repo.get_summary_request(request["requestId"])["state"], "unknown")
        entry_id = str(uuid4())
        self.assertTrue(self.repo.record_summary_result_receipt(self.result(request), entry_id))
        self.assertFalse(self.repo.record_summary_result_receipt(self.result(request), entry_id))
        stored = self.repo.get_summary_request(request["requestId"])
        self.assertEqual(stored["state"], "stored")
        self.assertEqual(stored["receipt"]["entry_id"], entry_id)
        self.assertEqual(self.repo.list_unresolved_summary_requests(), [])
        self.assertNotIn("PRIVATE-IMPORTANT-BODY-SENTINEL", str(stored))
        receipt = self.repo._connection.execute(
            "SELECT * FROM summary_result_receipts_v1 WHERE request_id=?", (request["requestId"],)
        ).fetchone()
        self.assertNotIn("PRIVATE-IMPORTANT-BODY-SENTINEL", str(tuple(receipt)))

    def test_confirmed_not_sent_can_be_replaced_but_atomic_insert_rolls_back(self) -> None:
        command = str(uuid4())
        first = self.request(command)
        self.repo.reserve_summary_request(first)
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_summary_state(first["requestId"], "not_sent")
        self.repo.record_summary_state(first["requestId"], "not_sent", detail="public send was not entered")
        replacement = self.request(command)
        self.repo.reserve_summary_request(replacement)
        self.repo._connection.execute("""CREATE TRIGGER fail_range BEFORE INSERT ON summary_request_ranges_v1
            BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        later = self.request(command, start=4, end=7)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.reserve_summary_request(later)
        self.assertIsNone(self.repo.get_summary_request(later["requestId"]))

    def test_source_proof_is_additive_and_core_task_history_survives_reopen(self) -> None:
        task = str(uuid4())
        self.repo.create_task({"goal": "history remains"}, task_id=task)
        self.repo.approve_scope(task, 1, {"paths": ["src/"]})
        self.repo.proceed(task, 1, "run")
        run = self.repo.start_run(task, 1)
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                  "taskId": task, "revision": 1, "runId": run}
        span = {"schemaVersion": 1, "source": source, "start": 0, "end": 3,
                "digest": sha256(b"abc").hexdigest()}
        source_id = self.repo.record_log_source({"schemaVersion": 1, "kind": "task", "id": task}, span)
        start = {"schemaVersion": 1, "eventId": str(uuid4()), "source": source,
                 "kind": "start", "offset": 0, "observedAt": STAMP}
        final = {**start, "eventId": str(uuid4()), "kind": "final_drain", "offset": 3,
                 "observedAt": "2026-10-09T01:02:04Z"}
        self.repo.record_observation_boundary(start)
        self.repo.record_observation_boundary(final)
        coverage = {"schemaVersion": 1, "coverageId": str(uuid4()), "source": source,
                    "startEventId": start["eventId"], "finalEventId": final["eventId"],
                    "finalOffset": 3, "sourceIds": [source_id]}
        self.repo.record_gapfree_coverage(coverage)
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_condition_proof(task, 1, run, sha256(b"condition").hexdigest(),
                                             matched=False, observation_complete=True)
        proof_id = self.repo.record_condition_proof(task, 1, run, sha256(b"condition").hexdigest(),
                                                    matched=True, observation_complete=True,
                                                    coverage_id=coverage["coverageId"],
                                                    source_id=source_id, match_start=0, match_end=3,
                                                    full_match_digest=sha256(b"abc").hexdigest(), excerpt=b"abc")
        before_spec = self.repo.get_task_spec(task)
        before_history = self.repo.get_run_history(run)
        self.repo.close()
        self.repo = TaskRepository(self.path)
        self.assertEqual(self.repo.get_task_spec(task), before_spec)
        self.assertEqual(self.repo.get_run_history(run), before_history)
        self.assertEqual(self.repo.get_condition_proofs(run)[0]["proof_id"], proof_id)
        self.assertEqual(self.repo.get_condition_proofs(run)[0]["coverage_id"], coverage["coverageId"])
        self.assertEqual(self.repo.get_log_sources({"schemaVersion": 1, "kind": "task", "id": task})[0]["source_id"], source_id)
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_condition_proof(task, 1, run, sha256(b"condition").hexdigest(),
                                             matched=False, observation_complete=False, excerpt=b"fake")

    def test_failed_v2_migration_rolls_back_and_preserves_core_data(self) -> None:
        task = str(uuid4())
        self.repo.create_task({"goal": "persist"}, task_id=task)
        self.repo.close()
        db = sqlite3.connect(self.path)
        for table in ("log_source_records", "log_condition_proofs", "summary_requests_v1",
                      "summary_request_ranges_v1", "summary_request_events_v1",
                      "summary_result_receipts_v1", "summary_result_drops_v1"):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=2")
        db.commit()
        db.close()
        with patch.object(repository_module, "MIGRATION_2_TO_3",
                          "BEGIN IMMEDIATE; CREATE TABLE rollback_marker(x); INVALID SQL; COMMIT;"):
            with self.assertRaises(sqlite3.OperationalError):
                TaskRepository(self.path)
        db = sqlite3.connect(self.path)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='rollback_marker'").fetchone())
        db.close()
        self.repo = TaskRepository(self.path)
        self.assertEqual(self.repo.get_task_spec(task)["spec"]["goal"], "persist")

    def test_stored_transition_requires_atomic_receipt(self) -> None:
        request = self.request(str(uuid4()))
        self.repo.reserve_summary_request(request)
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_summary_state(request["requestId"], "stored")
        self.repo.record_summary_state(request["requestId"], "sending")
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_summary_state(request["requestId"], "stored")
        self.repo.record_summary_state(request["requestId"], "unknown", detail="delivery unknown")
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_summary_state(request["requestId"], "stored")
        self.assertEqual(self.repo.get_summary_request(request["requestId"])["state"], "unknown")
        self.assertIsNone(self.repo.get_summary_request(request["requestId"])["receipt"])
        self.assertEqual(len(self.repo.list_unresolved_summary_requests()), 1)

    def test_gap_only_unknown_cannot_be_replayed_with_new_skill_reason_or_epoch(self) -> None:
        command = str(uuid4())
        request = self.request(command)
        source = request["sources"][0]["source"]
        request["sources"] = []
        request["sourceGaps"] = [{"schemaVersion": 1, "source": source,
                                  "start": 0, "end": 3, "reason": "collector_gap"}]
        request["inputKey"] = summary_input_key(request["subject"], None, [], request["sourceGaps"], request["skillDigest"])
        self.repo.reserve_summary_request(request)
        self.repo.record_summary_state(request["requestId"], "sending")
        self.repo.record_summary_state(request["requestId"], "unknown", detail="delivery unknown")
        self.repo.close()
        self.repo = TaskRepository(self.path)
        candidate = copy.deepcopy(request)
        candidate["requestId"] = str(uuid4())
        candidate["skillDigest"] = sha256(b"new skill version").hexdigest()
        candidate["sourceGaps"][0]["reason"] = "storage_gap"
        candidate["sourceGaps"][0]["source"]["collectorEpoch"] = 2
        candidate["inputKey"] = summary_input_key(candidate["subject"], None, [], candidate["sourceGaps"], candidate["skillDigest"])
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(candidate)
        self.assertIsNone(self.repo.get_summary_request(candidate["requestId"]))

    def test_distinct_structured_streams_share_run_offsets_but_one_physical_id_cannot_replay(self) -> None:
        task = str(uuid4())
        self.repo.create_task({"goal": "structured streams"}, task_id=task)
        self.repo.approve_scope(task, 1, {"paths": ["src/"]})
        self.repo.proceed(task, 1, "run")
        run1 = self.repo.start_run(task, 1)
        message = str(uuid4())
        report = str(uuid4())
        def structured(kind: str, key: str, identifier: str, run_id: str, revision: int) -> dict:
            request = self.request(str(uuid4()))
            subject = {"schemaVersion": 1, "kind": "task", "id": task}
            source = {"schemaVersion": 1, "sourceKind": kind, "collectorEpoch": 1,
                      "taskId": task, "revision": revision, "runId": run_id, key: identifier}
            span = {"schemaVersion": 1, "source": source, "start": 0, "end": 3,
                    "digest": sha256(b"abc").hexdigest()}
            request.update(subject=subject, revision=revision, sources=[span])
            request["inputKey"] = summary_input_key(subject, revision, [span], [], request["skillDigest"])
            return request
        manager_request = structured("manager_instruction", "messageId", message, run1, 1)
        report_request = structured("report", "eventId", report, run1, 1)
        self.repo.reserve_summary_request(manager_request)
        self.repo.record_summary_state(manager_request["requestId"], "sending")
        self.repo.record_summary_state(manager_request["requestId"], "unknown", detail="delivery unknown")
        # Both ranges start at zero within one Run; message/event IDs are distinct streams.
        self.repo.reserve_summary_request(report_request)
        self.repo.record_summary_state(report_request["requestId"], "sending")
        self.repo.record_summary_state(report_request["requestId"], "unknown", detail="delivery unknown")
        self.repo.close()
        self.repo = TaskRepository(self.path)
        replay = structured("manager_instruction", "messageId", message, run1, 1)
        replay["sources"][0]["source"]["collectorEpoch"] = 2
        replay["skillDigest"] = sha256(b"changed skill").hexdigest()
        replay["inputKey"] = summary_input_key(replay["subject"], 1, replay["sources"], [], replay["skillDigest"])
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(replay)
        self.repo.complete_run(run1)
        self.repo.revise_task(task, {"goal": "new revision"})
        self.repo.approve_scope(task, 2, {"paths": ["src/"]})
        self.repo.proceed(task, 2, "run again")
        run2 = self.repo.start_run(task, 2)
        replay_new_revision = structured("manager_instruction", "messageId", message, run2, 2)
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(replay_new_revision)
        fresh_message = structured("manager_instruction", "messageId", str(uuid4()), run2, 2)
        self.repo.reserve_summary_request(fresh_message)

    def test_run_revision_source_provenance_is_immutable_for_all_three_ports(self) -> None:
        task = str(uuid4())
        self.repo.create_task({"goal": "one"}, task_id=task)
        self.repo.approve_scope(task, 1, {"paths": ["src/"]})
        self.repo.proceed(task, 1, "run")
        run = self.repo.start_run(task, 1)
        self.repo.revise_task(task, {"goal": "two"})
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                  "taskId": task, "revision": 2, "runId": run}
        span = {"schemaVersion": 1, "source": source, "start": 0, "end": 3,
                "digest": sha256(b"abc").hexdigest()}
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_log_source({"schemaVersion": 1, "kind": "task", "id": task}, span)
        boundary = {"schemaVersion": 1, "eventId": str(uuid4()), "source": source,
                    "kind": "start", "offset": 0, "observedAt": STAMP}
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_observation_boundary(boundary)
        request = self.request(str(uuid4()))
        request["subject"] = {"schemaVersion": 1, "kind": "task", "id": task}
        request["revision"] = 1
        valid_span = copy.deepcopy(span)
        valid_span["source"]["revision"] = 1
        request["sources"] = [valid_span]
        request["inputKey"] = summary_input_key(request["subject"], 1, [valid_span], [], request["skillDigest"])
        self.repo.reserve_summary_request(request)
        self.repo.record_summary_state(request["requestId"], "sending")
        self.repo.record_summary_state(request["requestId"], "unknown", detail="delivery unknown")
        request["requestId"] = str(uuid4())
        request["revision"] = 2
        request["sources"] = [span]
        request["inputKey"] = summary_input_key(request["subject"], 2, [span], [], request["skillDigest"])
        with self.assertRaises(InvalidTransitionError):
            self.repo.reserve_summary_request(request)

    def test_gapfree_coverage_requires_exact_boundaries_and_contiguous_observed_tiles(self) -> None:
        task = str(uuid4())
        self.repo.create_task({"goal": "coverage"}, task_id=task)
        self.repo.approve_scope(task, 1, {"paths": ["src/"]})
        self.repo.proceed(task, 1, "run")
        run = self.repo.start_run(task, 1)
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                  "taskId": task, "revision": 1, "runId": run}
        subject = {"schemaVersion": 1, "kind": "task", "id": task}
        start = {"schemaVersion": 1, "eventId": str(uuid4()), "source": source,
                 "kind": "start", "offset": 0, "observedAt": STAMP}
        final = {**start, "eventId": str(uuid4()), "kind": "final_drain", "offset": 6,
                 "observedAt": "2026-10-09T01:02:04Z"}
        first = {"schemaVersion": 1, "source": source, "start": 0, "end": 3,
                 "digest": sha256(b"abc").hexdigest()}
        second = {"schemaVersion": 1, "source": source, "start": 3, "end": 6,
                  "digest": sha256(b"def").hexdigest()}
        first_id = self.repo.record_log_source(subject, first)
        second_id = self.repo.record_log_source(subject, second)
        coverage = {"schemaVersion": 1, "coverageId": str(uuid4()), "source": source,
                    "startEventId": start["eventId"], "finalEventId": final["eventId"],
                    "finalOffset": 6, "sourceIds": [first_id, second_id]}
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_gapfree_coverage(coverage)
        self.repo.record_observation_boundary(start)
        self.repo.record_observation_boundary(final)
        for mutate in (
            lambda row: row.update(sourceIds=[first_id]),
            lambda row: row.update(finalOffset=5),
            lambda row: row["source"].update(collectorEpoch=2),
        ):
            bad = copy.deepcopy(coverage)
            bad["coverageId"] = str(uuid4())
            mutate(bad)
            with self.subTest(bad=bad), self.assertRaises(InvalidTransitionError):
                self.repo.record_gapfree_coverage(bad)
        self.assertEqual(self.repo.record_gapfree_coverage(coverage), coverage["coverageId"])
        self.repo.close()
        self.repo = TaskRepository(self.path)
        unrelated_command = str(uuid4())
        self.repo.record_log_source({"schemaVersion": 1, "kind": "command", "id": unrelated_command},
                                    {"schemaVersion": 1,
                                     "source": {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                                                "commandId": unrelated_command},
                                     "start": 0, "end": 1, "digest": sha256(b"x").hexdigest()},
                                    status="collector_gap")
        proof = self.repo.record_condition_proof(task, 1, run, sha256(b"condition").hexdigest(),
                                                matched=False, observation_complete=True,
                                                coverage_id=coverage["coverageId"])
        self.assertEqual(self.repo.get_condition_proofs(run)[0]["proof_id"], proof)
        self.repo.record_log_source(subject, {**second, "start": 4, "end": 5}, status="collector_gap")
        new_coverage = {**coverage, "coverageId": str(uuid4())}
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_gapfree_coverage(new_coverage)
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_condition_proof(task, 1, run, sha256(b"late negative").hexdigest(),
                                             matched=False, observation_complete=True,
                                             coverage_id=coverage["coverageId"])
        matched = self.repo.record_condition_proof(task, 1, run, sha256(b"observed match").hexdigest(),
                                                   matched=True, observation_complete=False,
                                                   source_id=first_id, match_start=0, match_end=3,
                                                   full_match_digest=sha256(b"abc").hexdigest(), excerpt=b"abc")
        self.assertIn(matched, {row["proof_id"] for row in self.repo.get_condition_proofs(run)})

    def _completed_proofs_before_late_source(self) -> tuple[str, dict, dict, str, str, dict]:
        """Persist complete no-match and matched evidence before later journal records."""
        task = str(uuid4())
        self.repo.create_task({"goal": "late source must not preserve completeness"}, task_id=task)
        self.repo.approve_scope(task, 1, {"paths": ["src/"]})
        self.repo.proceed(task, 1, "run")
        run = self.repo.start_run(task, 1)
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                  "taskId": task, "revision": 1, "runId": run}
        subject = {"schemaVersion": 1, "kind": "task", "id": task}
        observed = {"schemaVersion": 1, "source": source, "start": 0, "end": 3,
                    "digest": sha256(b"abc").hexdigest()}
        source_id = self.repo.record_log_source(subject, observed)
        start = {"schemaVersion": 1, "eventId": str(uuid4()), "source": source,
                 "kind": "start", "offset": 0, "observedAt": STAMP}
        final = {**start, "eventId": str(uuid4()), "kind": "final_drain", "offset": 3,
                 "observedAt": "2026-10-09T01:02:04Z"}
        self.repo.record_observation_boundary(start)
        self.repo.record_observation_boundary(final)
        coverage = {"schemaVersion": 1, "coverageId": str(uuid4()), "source": source,
                    "startEventId": start["eventId"], "finalEventId": final["eventId"],
                    "finalOffset": 3, "sourceIds": [source_id]}
        self.repo.record_gapfree_coverage(coverage)
        negative_id = self.repo.record_condition_proof(task, 1, run, sha256(b"absent").hexdigest(),
                                                       matched=False, observation_complete=True,
                                                       coverage_id=coverage["coverageId"])
        matched_id = self.repo.record_condition_proof(task, 1, run, sha256(b"present").hexdigest(),
                                                      matched=True, observation_complete=True,
                                                      coverage_id=coverage["coverageId"], source_id=source_id,
                                                      match_start=0, match_end=3,
                                                      full_match_digest=sha256(b"abc").hexdigest(),
                                                      excerpt=b"abc")
        persisted = {row["proof_id"]: tuple(row) for row in self.repo._connection.execute(
            "SELECT * FROM log_condition_proofs WHERE run_id=?", (run,))}
        return run, subject, source, negative_id, matched_id, persisted

    def _assert_late_source_read_only_invalidation(self, run: str, proof_ids: tuple[str, str],
                                                    expected_reason: str, original: dict) -> None:
        # The append-only evidence stays intact; only the returned interpretation changes.
        self.repo.close()
        self.repo = TaskRepository(self.path)
        statements: list[str] = []
        self.repo._connection.set_trace_callback(statements.append)
        try:
            view = {row["proof_id"]: row for row in self.repo.get_condition_proofs(run)}
        finally:
            self.repo._connection.set_trace_callback(None)
        self.assertEqual(set(view), set(proof_ids))
        self.assertEqual({view[proof_id]["invalidation_reason"] for proof_id in proof_ids},
                         {expected_reason})
        for proof_id in proof_ids:
            self.assertEqual(view[proof_id]["observation_complete"], 0)
        self.assertEqual(view[proof_ids[0]]["matched"], 0)
        self.assertEqual(view[proof_ids[1]]["matched"], 1)
        self.assertEqual({row["proof_id"]: tuple(row) for row in self.repo._connection.execute(
            "SELECT * FROM log_condition_proofs WHERE run_id=?", (run,))}, original)
        self.assertTrue(statements)
        self.assertEqual({statement.lstrip().split(None, 1)[0].upper() for statement in statements},
                         {"BEGIN", "SELECT", "ROLLBACK"})

    def test_late_physical_gap_invalidates_stored_complete_proofs_on_read_only_reopen(self) -> None:
        run, subject, source, negative_id, matched_id, original = self._completed_proofs_before_late_source()
        unrelated = {"schemaVersion": 1, "sourceKind": "manager_instruction", "collectorEpoch": 1,
                     "taskId": subject["id"], "revision": 1, "runId": run,
                     "messageId": str(uuid4())}
        self.repo.record_log_source(subject,
                                    {"schemaVersion": 1, "source": unrelated, "start": 0, "end": 1,
                                     "digest": sha256(b"x").hexdigest()}, status="collector_gap")
        unaffected = {row["proof_id"]: row for row in self.repo.get_condition_proofs(run)}
        self.assertEqual({unaffected[proof_id]["observation_complete"]
                          for proof_id in (negative_id, matched_id)}, {1})
        self.assertTrue(all("invalidation_reason" not in unaffected[proof_id]
                            for proof_id in (negative_id, matched_id)))
        self.repo.record_log_source(subject,
                                    {"schemaVersion": 1, "source": source, "start": 1, "end": 2,
                                     "digest": sha256(b"b").hexdigest()}, status="collector_gap")
        self._assert_late_source_read_only_invalidation(
            run, (negative_id, matched_id), "coverage_source_gap", original)

    def test_late_observed_tile_beyond_final_drain_invalidates_stored_complete_proofs(self) -> None:
        run, subject, source, negative_id, matched_id, original = self._completed_proofs_before_late_source()
        self.repo.record_log_source(subject,
                                    {"schemaVersion": 1, "source": source, "start": 3, "end": 4,
                                     "digest": sha256(b"d").hexdigest()})
        self._assert_late_source_read_only_invalidation(
            run, (negative_id, matched_id), "coverage_source_uncovered", original)

    def test_matched_proof_refuses_gap_wrong_run_interval_and_digest(self) -> None:
        task = str(uuid4())
        self.repo.create_task({"goal": "match"}, task_id=task)
        self.repo.approve_scope(task, 1, {"paths": ["src/"]})
        self.repo.proceed(task, 1, "run")
        run = self.repo.start_run(task, 1)
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                  "taskId": task, "revision": 1, "runId": run}
        subject = {"schemaVersion": 1, "kind": "task", "id": task}
        span = {"schemaVersion": 1, "source": source, "start": 0, "end": 3,
                "digest": sha256(b"abc").hexdigest()}
        gap_id = self.repo.record_log_source(subject, span, status="collector_gap")
        observed_id = self.repo.record_log_source(subject, span)
        base = {"matched": True, "observation_complete": False,
                "match_start": 0, "match_end": 3, "full_match_digest": span["digest"],
                "excerpt": b"abc"}
        for source_id, changes in (
            (gap_id, {}),
            (observed_id, {"match_start": 1}),
            (observed_id, {"full_match_digest": "0" * 64}),
        ):
            with self.subTest(source_id=source_id, changes=changes), self.assertRaises(InvalidTransitionError):
                self.repo.record_condition_proof(task, 1, run, sha256(b"condition").hexdigest(),
                                                 source_id=source_id, **{**base, **changes})
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_condition_proof(task, 1, str(uuid4()), sha256(b"condition").hexdigest(),
                                             source_id=observed_id, **base)
        good = self.repo.record_condition_proof(task, 1, run, sha256(b"condition").hexdigest(),
                                                source_id=observed_id, **base)
        self.assertEqual(self.repo.get_condition_proofs(run)[0]["proof_id"], good)

    def test_empty_output_requires_two_persisted_boundaries_and_no_source_ids(self) -> None:
        task = str(uuid4())
        self.repo.create_task({"goal": "empty output"}, task_id=task)
        self.repo.approve_scope(task, 1, {"paths": ["src/"]})
        self.repo.proceed(task, 1, "run")
        run = self.repo.start_run(task, 1)
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                  "taskId": task, "revision": 1, "runId": run}
        start = {"schemaVersion": 1, "eventId": str(uuid4()), "source": source,
                 "kind": "start", "offset": 0, "observedAt": STAMP}
        final = {**start, "eventId": str(uuid4()), "kind": "final_drain",
                 "observedAt": "2026-10-09T01:02:04Z"}
        coverage = {"schemaVersion": 1, "coverageId": str(uuid4()), "source": source,
                    "startEventId": start["eventId"], "finalEventId": final["eventId"],
                    "finalOffset": 0, "sourceIds": []}
        with self.assertRaises(InvalidTransitionError):
            self.repo.record_gapfree_coverage(coverage)
        self.repo.record_observation_boundary(start)
        self.repo.record_observation_boundary(final)
        self.assertEqual(self.repo.record_gapfree_coverage(coverage), coverage["coverageId"])
        proof = self.repo.record_condition_proof(task, 1, run, sha256(b"condition").hexdigest(),
                                                matched=False, observation_complete=True,
                                                coverage_id=coverage["coverageId"])
        self.assertEqual(self.repo.get_condition_proofs(run)[0]["proof_id"], proof)


if __name__ == "__main__":
    unittest.main()
