from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from workbench.backend.summary_adapter import SKILL_NAME, skill_bytes
from workbench.contracts.logs_v1 import LogContractError, summary_input_key
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog, ImportantLogStore, SubjectLifecycle

AT = "2026-10-12T00:00:00Z"
FLAGS = dict(active=True, paused=False, revoked=False, has_new_input=True)


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.catalog = ImportantLogCatalog(Path(self.temp.name) / "important", create=True)
        self.sink = ActiveSubjectLog(self.catalog)
        self.identity = self.catalog.health_snapshot()["active"]["dbUuid"]
        self.life = SubjectLifecycle(self.catalog.directory, self.identity, _catalog_owned=True)

    def command(self, seq=1):
        command = str(uuid4()); subject = {"schemaVersion": 1, "kind": "command", "id": command}
        self.sink.record_terminal({"seq": seq, "at": AT, "type": "terminal_started",
                                  "command_id": command, "task_id": None, "started_at": AT})
        return subject

    def request(self, subject, **flags):
        grant = self.sink.authority(subject, **{**FLAGS, **flags})
        with ImportantLogStore.open(self.catalog.directory, self.identity, readonly=True) as store:
            sources = [span for entry in store.list_entries(subject) for span in entry["sources"]]
        digest = sha256(skill_bytes()).hexdigest()
        request = {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject,
            "revision": grant.get("revision"), "authorizationRef": grant["authorization_ref"],
            "authorizationEpoch": grant["authorization_epoch"], "skillName": SKILL_NAME, "skillDigest": digest,
            "sources": sources, "sourceGaps": [], "priorCumulativeEntryId": None,
            "dbHealth": self.catalog.health_snapshot()["active"], "requestedAt": AT}
        request["inputKey"] = summary_input_key(subject, request["revision"], sources, [], digest)
        return request

    def test_start_identity_matches_lifecycle_and_never_uses_legacy_origin(self):
        subject = self.command(31)
        self.assertEqual(self.sink.authority(subject, **FLAGS), self.life.authority(subject, **FLAGS))
        self.sink.record_direct_origin(subject["id"], event_id=str(uuid4()), epoch=99,
                                       observed_at=AT, verified_direct_instruction=True)
        self.assertEqual(self.sink.authority(subject, **FLAGS), self.life.authority(subject, **FLAGS))
        self.assertEqual(self.sink.authority(subject, **FLAGS)["authorization_epoch"], 31)
        for flags in ({"paused": True}, {"revoked": True}, {"active": False}, {"has_new_input": False}):
            self.assertEqual(self.sink.authority(subject, **{**FLAGS, **flags}),
                             self.life.authority(subject, **{**FLAGS, **flags}))
        unknown = {**subject, "id": str(uuid4())}
        self.assertFalse(self.sink.authority(unknown, **FLAGS)["allowed"])

    def test_foreign_command_range_and_gap_are_rejected(self):
        first, second = self.command(1), self.command(2)
        request = self.request(first)
        self.assertTrue(self.sink.authorize_summary_request(request, **FLAGS)["allowed"])
        foreign = deepcopy(request)
        foreign["sources"] = self.request(second)["sources"]
        foreign["inputKey"] = summary_input_key(first, None, foreign["sources"], [], foreign["skillDigest"])
        with self.assertRaises(LogContractError): self.sink.authorize_summary_request(foreign, **FLAGS)
        fake = deepcopy(request); fake["sources"][0]["digest"] = "f" * 64
        fake["inputKey"] = summary_input_key(first, None, fake["sources"], [], fake["skillDigest"])
        self.assertEqual(self.sink.authorize_summary_request(fake, **FLAGS)["reason"], "unrelated_or_unobserved_source_range")

    def test_task_epoch_and_closing_reason_parity(self):
        subject = {"schemaVersion": 1, "kind": "task", "id": str(uuid4())}
        delegated = {"seq": 4, "at": AT, "type": "delegated", "task_id": subject["id"], "revision": 1,
                     "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())}
        self.sink.record_flow(delegated)
        self.assertEqual(self.sink.authority(subject, **FLAGS), self.life.authority(subject, **FLAGS))
        self.assertTrue(self.sink.authorize_summary_request(self.request(subject), **FLAGS)["allowed"])
        self.sink.record_flow({"seq": 5, "at": AT, "type": "task_closed", "task_id": subject["id"], "reason": "done"})
        flags = {**FLAGS, "active": False, "closing_event": True}
        self.assertEqual(self.sink.authority(subject, **flags), self.life.authority(subject, **flags))
        self.assertEqual(self.sink.authority(subject, **flags)["authorization_epoch"], 5)
        self.assertTrue(self.sink.authorize_summary_request(self.request(subject, closing_event=True, active=False), **flags)["allowed"])

    def test_rollover_preserves_confirmed_command_start_lookup(self):
        subject = self.command(11)
        request = self.request(subject)
        with self.catalog._lease() as fd:
            pointer = self.catalog._current(fd)
            with ImportantLogStore.open(self.catalog.directory, pointer["active"], _catalog_owned=True) as store:
                self.catalog._rotate(fd, pointer, store)
        self.assertEqual(self.sink.authority(subject, **FLAGS)["authorization_epoch"], 11)
        self.assertTrue(self.sink.authorize_summary_request(request, **FLAGS)["allowed"])


if __name__ == "__main__": unittest.main()
