"""LG-D24 work-scope admission against a private durable ImportantLogStore."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from workbench.contracts.logs_v1 import LogContractError, summary_input_key
from workbench.storage.important_log import ImportantLogStore, SubjectLifecycle


AT = "2026-10-11T00:00:00Z"
END = "2026-10-11T00:00:01Z"
SKILL = "a" * 64


def subject(kind: str, identifier: str) -> dict:
    return {"schemaVersion": 1, "kind": kind, "id": identifier}


class WorkScopeIndependentTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="lg24-scope-")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "important"
        with ImportantLogStore.create(self.directory) as store:
            self.db_uuid = store.db_uuid
        self.life = SubjectLifecycle(self.directory, self.db_uuid)

    def request(self, owner: dict, decision: dict, spans: list[dict], *, revision: int | None = None,
                gaps: list[dict] | None = None, prior: str | None = None) -> dict:
        gaps = [] if gaps is None else gaps
        return {"schemaVersion": 1, "requestId": str(uuid4()), "subject": owner,
                "revision": revision, "authorizationRef": decision["authorization_ref"],
                "authorizationEpoch": decision["authorization_epoch"],
                "inputKey": summary_input_key(owner, revision, spans, gaps, SKILL),
                "skillName": "private-summary", "skillDigest": SKILL,
                "sources": spans, "sourceGaps": gaps, "priorCumulativeEntryId": prior,
                "dbHealth": {"schemaVersion": 1, "dbUuid": self.db_uuid, "measuredAt": AT,
                             "stale": False, "mainBytes": 1, "walBytes": 0, "shmBytes": 0,
                             "totalBytes": 1, "logicalLiveBytes": 1, "freelistBytes": 0,
                             "subjectStats": [], "cleanupCandidates": None, "lastWrite": None,
                             "lastLock": None, "lastMaintenance": None, "diskFreeBytes": None,
                             "lastQuickCheck": None, "metadataOutsideBudget": True},
                "requestedAt": AT}

    @staticmethod
    def span(command: str, raw: bytes, *, task: str | None = None, revision: int | None = None) -> dict:
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                  "commandId": command}
        if task is not None:
            source.update(taskId=task, revision=revision)
        return {"schemaVersion": 1, "source": source, "start": 0, "end": len(raw),
                "digest": sha256(raw).hexdigest()}

    def test_taskless_start_ref_epoch_survive_reopen_without_direct_proof(self) -> None:
        command = str(uuid4())
        owner = subject("command", command)
        denied = self.life.authority(owner, active=True, paused=False, revoked=False, has_new_input=True)
        self.assertFalse(denied["allowed"])
        self.assertFalse(self.life.record_terminal({"type": "run_ended", "command_id": command}))
        start = {"seq": 17, "type": "terminal_started", "command_id": command,
                 "task_id": None, "started_at": AT}
        self.assertTrue(self.life.record_terminal(start))
        first = self.life.authority(owner, active=True, paused=False, revoked=False, has_new_input=True)
        self.assertTrue(first["allowed"])
        self.assertEqual(first["authorization_epoch"], 17)
        with ImportantLogStore.open(self.directory, self.db_uuid) as store:
            row = store._connection.execute("""SELECT source_id FROM subject_events_v1
                WHERE event_key='terminal:17' AND event_kind='terminal_started'""").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(first["authorization_ref"], row[0])
        reopened = SubjectLifecycle(self.directory, self.db_uuid)
        self.assertEqual(reopened.authority(owner, active=True, paused=False, revoked=False,
                                            has_new_input=True), first)
        self.assertFalse(reopened.record_terminal(start))

    def test_request_needs_same_work_ref_and_recorded_source_subset(self) -> None:
        command, foreign = str(uuid4()), str(uuid4())
        owner = subject("command", command)
        self.life.record_terminal({"seq": 1, "type": "terminal_started", "command_id": command,
                                   "task_id": None, "started_at": AT})
        self.life.record_terminal({"seq": 2, "type": "terminal_started", "command_id": foreign,
                                   "task_id": None, "started_at": AT})
        observed = self.span(command, b"observed selected bytes")
        self.life.record_terminal_range(owner, str(uuid4()), observed, observed_at=AT)
        trailing = {**observed, "start": observed["end"],
                    "end": observed["end"] + len(b"later output"),
                    "digest": sha256(b"later output").hexdigest()}
        self.life.record_terminal_range(owner, str(uuid4()), trailing, observed_at=AT)
        grant = self.life.authority(owner, active=True, paused=False, revoked=False, has_new_input=True)
        request = self.request(owner, grant, [observed])
        self.assertTrue(self.life.authorize_summary_request(request, active=True, paused=False,
                                                             revoked=False, has_new_input=True)["allowed"])
        trailing_request = self.request(owner, grant, [trailing])
        self.assertTrue(self.life.authorize_summary_request(trailing_request, active=True,
                        paused=False, revoked=False, has_new_input=True)["allowed"])
        for changed in ({"authorizationRef": str(uuid4())},
                        {"authorizationEpoch": grant["authorization_epoch"] + 1},
                        {"priorCumulativeEntryId": str(uuid4())}):
            with self.subTest(changed=changed):
                self.assertFalse(self.life.authorize_summary_request({**request, **changed},
                    active=True, paused=False, revoked=False, has_new_input=True)["allowed"])
        other_span = self.span(foreign, b"observed selected bytes")
        invented = {**observed, "digest": "0" * 64}
        changed_range = {**observed, "end": observed["end"] + 1}
        for span in (other_span, invented, changed_range):
            candidate = {**request, "sources": [span],
                         "inputKey": summary_input_key(owner, None, [span], [], SKILL)}
            with self.subTest(span=span):
                try:
                    decision = self.life.authorize_summary_request(candidate, active=True,
                        paused=False, revoked=False, has_new_input=True)
                except LogContractError:
                    continue  # Cross-command source is rejected by the wire contract.
                self.assertFalse(decision["allowed"])
        gap = {"schemaVersion": 1, "source": observed["source"], "start": 0,
               "end": observed["end"], "reason": "collector_gap"}
        gap_request = {**request, "sources": [], "sourceGaps": [gap],
                       "inputKey": summary_input_key(owner, None, [], [gap], SKILL)}
        self.assertFalse(self.life.authorize_summary_request(gap_request, active=True,
                         paused=False, revoked=False, has_new_input=True)["allowed"])

    def test_task_grant_revision_and_postprocessing_are_bounded(self) -> None:
        task, command = str(uuid4()), str(uuid4())
        owner = subject("task", task)
        grant = str(uuid4())
        self.life.record_flow({"seq": 1, "at": AT, "type": "delegated", "task_id": task,
                               "revision": 2, "scope_decision_id": str(uuid4()),
                               "proceed_decision_id": grant})
        self.life.record_terminal({"seq": 2, "type": "terminal_started", "command_id": command,
                                   "task_id": task, "started_at": AT}, revision=2)
        observed = self.span(command, b"Task output", task=task, revision=2)
        self.life.record_terminal_range(owner, str(uuid4()), observed, observed_at=AT)
        decision = self.life.authority(owner, active=True, paused=False, revoked=False,
                                       has_new_input=True)
        self.assertEqual((decision["authorization_ref"], decision["revision"]), (grant, 2))
        request = self.request(owner, decision, [observed], revision=2)
        def admit(candidate: dict, **flags: bool) -> dict:
            return self.life.authorize_summary_request(candidate, active=flags.get("active", True),
                paused=flags.get("paused", False), revoked=flags.get("revoked", False),
                has_new_input=flags.get("has_new_input", True),
                closing_event=flags.get("closing_event", False))
        self.assertTrue(admit(request)["allowed"])
        for flags in ({"paused": True}, {"revoked": True}, {"active": False}, {"has_new_input": False}):
            with self.subTest(flags=flags):
                self.assertFalse(admit(request, **flags)["allowed"])
        self.assertFalse(admit({**request, "authorizationRef": str(uuid4())})["allowed"])
        with self.assertRaises(LogContractError):
            admit({**request, "revision": 3})
        self.life.record_flow({"seq": 3, "at": END, "type": "task_closed", "task_id": task,
                               "reason": "done"})
        self.assertFalse(admit(request)["allowed"])
        closing = self.life.authority(owner, active=False, paused=False, revoked=False,
                                      has_new_input=True, closing_event=True)
        self.assertEqual((closing["authorization_ref"], closing["revision"]), (grant, 2))
        closing_request = {**request, "authorizationEpoch": closing["authorization_epoch"]}
        self.assertTrue(admit(closing_request, active=False, closing_event=True)["allowed"])
        self.assertFalse(admit(closing_request, active=False, paused=True, closing_event=True)["allowed"])

    def test_actual_command_end_allows_one_closing_scope_then_expires(self) -> None:
        command = str(uuid4())
        owner = subject("command", command)
        self.life.record_terminal({"seq": 1, "type": "terminal_started", "command_id": command,
                                   "task_id": None, "started_at": AT})
        observed = self.span(command, b"command output")
        self.life.record_terminal_range(owner, str(uuid4()), observed, observed_at=AT)
        grant = self.life.authority(owner, active=True, paused=False, revoked=False,
                                    has_new_input=True)
        request = self.request(owner, grant, [observed])
        self.life.record_terminal({"seq": 2, "type": "terminal_ended", "command_id": command,
                                   "status": "unknown", "ended_at": END})
        self.assertFalse(self.life.authorize_summary_request(request, active=False,
                         paused=False, revoked=False, has_new_input=True, closing_event=True)["allowed"])
        self.life.record_terminal({"seq": 3, "type": "terminal_ended", "command_id": command,
                                   "status": "exited", "ended_at": END})
        self.assertFalse(self.life.authorize_summary_request(request, active=True,
                         paused=False, revoked=False, has_new_input=True)["allowed"])
        self.assertTrue(self.life.authorize_summary_request(request, active=False,
                        paused=False, revoked=False, has_new_input=True, closing_event=True)["allowed"])
        self.life.mark_command_end_summary_processed(command)
        self.assertFalse(self.life.authorize_summary_request(request, active=False,
                         paused=False, revoked=False, has_new_input=True, closing_event=True)["allowed"])


if __name__ == "__main__":
    unittest.main()
