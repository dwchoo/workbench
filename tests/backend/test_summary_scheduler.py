"""DVL-10 contract fixtures: real journals/stores, no model/runtime claim."""
import base64
import json
from unittest.mock import patch
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import os
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from workbench.backend.flow import HandoffJournal
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.summary_adapter import SummaryDeferred, prepare_summary
from workbench.backend.summary_scheduler import SummaryScheduler, SummarySourceCollector
from workbench.backend.summary_writer import store_summary
from workbench.contracts.logs_v1 import summary_input_key
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog, SubjectLifecycle
from workbench.tasks.repository import InvalidTransitionError, TaskRepository
from workbench.storage.log_raw.port import task_source
from workbench.storage.important_log.store import ImportantLogStore
from workbench.workflow.run import WorkflowRun, _ExactByteMatcher
from tests.backend.test_summary_adapter import request_fixture

AT = "2026-10-12T00:00:00Z"


class Compat:
    def __init__(self): self.incidents, self.allowed = [], True
    def admission(self): return self.allowed
    def protect(self, event): self.incidents.append(event)
    def verify_reference(self, reference): return reference == "explicit-scripted-verify"


class Adapter:
    def __init__(self, root): self.root, self.calls, self.response = root, [], None
    def dispatch_outcome(self, prepared, **ports):
        self.calls.append(deepcopy(prepared))
        if self.response is not None:
            return self.response
        request = prepared["request"]
        result = {"schemaVersion": 1, "requestId": request["requestId"],
            "inputKey": request["inputKey"], "skillDigest": request["skillDigest"],
            "sources": request["sources"], "facts": ["work summary"], "interpretations": [], "unknowns": [],
            "usage": {"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None}}
        written = store_summary(self.root, request["requestId"], result)
        if not written["saved"]:
            raise RuntimeError(str(written))
        return {"status": "summary_result", "sent": True, "protected": False, "result": result, "writer": written}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.layout = DataLayout(self.root)
        ensure_private_dir(self.layout.workflow)
        self.catalog = ImportantLogCatalog(self.layout.important_logs, create=True)
        self.sink = ActiveSubjectLog(self.catalog)
        self.command = str(uuid4())
        self.subject = {"schemaVersion": 1, "kind": "command", "id": self.command}
        self.events = HandoffJournal(self.layout.workflow / "handoffs.jsonl")
        self.addCleanup(self.events.close)
        self.start()
        _, self.metadata = request_fixture(self.root / "fixture", ["work"])
        self.runtime = {"metadata": self.metadata, "instructions": "existing task definition",
                        "expected_peer": (str(uuid4()), 1), "writer": {}}
        self.factory = lambda: TaskRepository(self.layout.tasks)
        self.compat = Compat()
        self.adapter = Adapter(self.root)
        self.now, self.paused, self.priority, self.active, self.revoked = 0., False, False, True, False
        self.scheduler = self.make_scheduler()
        os.chmod(self.layout.tasks, 0o600)
        self.addCleanup(self.scheduler.close)

    def start(self):
        confirmed = self.events.append({"type": "terminal_started", "command_id": self.command,
            "task_id": None, "command": "check approved work", "started_at": AT})
        self.sink.record_terminal(confirmed)

    def end(self, status="exited"):
        self.sink.record_terminal(self.events.append({"type": "terminal_ended",
            "command_id": self.command, "status": status, "ended_at": AT}))
        self.active = False

    def make_scheduler(self, **overrides):
        values = dict(repository_factory=self.factory, adapter=self.adapter, subject_log=lambda: self.sink,
            collector=SummarySourceCollector(self.root, lambda: self.sink),
            authority_flags=lambda _subject, _end: {"active": self.active, "revoked": self.revoked},
            paused=lambda: self.paused, priority=lambda: self.priority, runtime=lambda: self.runtime,
            storage_ready=lambda: True, compatibility=self.compat, clock=lambda: self.now)
        return SummaryScheduler(**{**values, **overrides})

    def settle(self):
        deadline = time.monotonic() + 4
        while (self.scheduler.status().get("busy") or self.scheduler.status()["inFlight"]) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.scheduler.status().get("busy") or self.scheduler.status()["inFlight"], self.scheduler.status())
        return self.scheduler.status()

    def rows(self):
        with self.factory() as journal:
            return [journal.get_summary_request(row[0]) for row in
                journal._connection.execute("SELECT request_id FROM summary_requests_v1 ORDER BY rowid")]

    def test_299_300_no_health_only_recall_and_single_writer(self):
        self.scheduler.tick(); self.settle()
        self.now = 299
        self.scheduler.tick(); self.settle()
        self.assertEqual(self.adapter.calls, [])
        self.now = 300
        self.scheduler.tick()
        self.assertEqual(self.settle()["state"], "stored")
        self.assertEqual(self.rows()[0]["state"], "stored")
        self.now = 900
        self.scheduler.tick(); self.settle()
        self.assertEqual(len(self.adapter.calls), 1)
        with self.catalog._lease() as fd:
            from workbench.storage.important_log.store import ImportantLogStore
            with ImportantLogStore.open(self.catalog.directory, self.catalog._current(fd)["active"], readonly=True) as store:
                self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM entries WHERE kind='cumulative'").fetchone()[0], 1)

    def test_end_coalesces_before_300_then_expires_command_scope(self):
        self.scheduler.tick(); self.settle()
        self.end()
        self.scheduler.tick()
        self.assertEqual(self.settle()["state"], "stored")
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(len(self.adapter.calls[0]["request"]["sources"]), 2)
        self.assertFalse(self.sink.authority(self.subject, active=False, paused=False, revoked=False,
                                           has_new_input=True, closing_event=True)["allowed"])

    def test_pause_priority_and_revocation_prevent_new_dispatch(self):
        self.scheduler.tick(); self.settle(); self.now = 300
        self.paused = True; self.scheduler.tick()
        self.settle(); self.priority = True; self.paused = False; self.scheduler.tick(); self.settle()
        self.assertEqual(self.adapter.calls, [])
        self.priority = False; self.revoked = True; self.scheduler.tick()
        self.assertEqual(self.settle()["state"], "deferred")
        self.assertEqual(self.adapter.calls, [])

    def test_unknown_runtime_never_reserves_or_dispatches(self):
        self.scheduler.tick(); self.settle(); self.now = 300; self.runtime = None
        self.scheduler.tick()
        self.assertEqual(self.settle()["reason"], "model_metadata_unavailable")
        self.assertEqual(self.rows(), [])

    def test_not_sent_merges_next_regular_period(self):
        self.adapter.response = {"status": "deferred", "sent": False, "protected": False, "reason": "paused"}
        self.scheduler.tick(); self.settle(); self.now = 300; self.scheduler.tick(); self.settle()
        self.assertEqual(self.rows()[0]["state"], "not_sent")
        self.adapter.response = None; self.now = 599; self.scheduler.tick(); self.settle()
        self.assertEqual(len(self.adapter.calls), 1)
        self.now = 600; self.scheduler.tick(); self.settle()
        self.assertEqual([row["state"] for row in self.rows()], ["not_sent", "stored"])

    def test_sent_unknown_never_replays_even_after_restart(self):
        self.adapter.response = {"status": "unknown", "sent": True, "protected": False}
        self.scheduler.tick(); self.settle(); self.now = 300; self.scheduler.tick(); self.settle()
        self.assertEqual(self.rows()[0]["state"], "unknown")
        self.scheduler.close(); self.scheduler = self.make_scheduler(); self.now = 900
        self.scheduler.tick(); self.settle(); self.settle()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_single_inflight_does_not_block_tick(self):
        entered, release = threading.Event(), threading.Event()
        original = self.adapter.dispatch_outcome
        def held(*args, **kwargs):
            entered.set(); release.wait(3); return original(*args, **kwargs)
        self.adapter.dispatch_outcome = held
        self.scheduler.tick(); self.settle(); self.now = 300; self.scheduler.tick()
        self.assertTrue(entered.wait(2))
        before = time.monotonic(); self.scheduler.tick()
        self.assertLess(time.monotonic() - before, .1)
        self.paused = True  # already sent valid late result is still stored
        release.set(); self.assertEqual(self.settle()["state"], "stored")
        self.assertEqual(len(self.adapter.calls), 1)

    def test_source_io_is_off_backend_tick_and_one_job(self):
        entered, release = threading.Event(), threading.Event()
        original = self.scheduler._discover
        def held(*args):
            entered.set(); release.wait(2); return original(*args)
        self.scheduler._discover = held
        before = time.monotonic(); self.scheduler.tick()
        self.assertLess(time.monotonic() - before, .1); self.assertTrue(entered.wait(1))
        job = self.scheduler._job
        before = time.monotonic(); self.scheduler.tick()
        self.assertLess(time.monotonic() - before, .1); self.assertIs(self.scheduler._job, job)
        release.set(); self.settle(); self.assertEqual(self.adapter.calls, [])

    def test_invalid_result_is_contract_rejection_not_immediate_safety(self):
        self.adapter.response = {"status": "rejected", "sent": True, "protected": True, "reason": "invalid_result"}
        self.scheduler.tick(); self.settle(); self.now = 300; self.scheduler.tick(); self.settle()
        with self.factory() as journal:
            state = journal.summary_control_state()
        self.assertEqual(state["streak"], 1); self.assertIsNone(state["protection"])
        self.assertEqual(self.rows()[0]["state"], "rejected")

    def last_detail(self, request):
        with self.factory() as journal:
            return journal._connection.execute("SELECT detail FROM summary_request_events_v1 WHERE request_id=? ORDER BY event_id DESC LIMIT 1",
                                               (request["request"]["requestId"],)).fetchone()[0]

    def test_budget_outcomes_preserve_reason_streak_and_no_replay_on_restart(self):
        self.scheduler.tick(); self.settle(); self.now = 300
        for reason, sent in (("input_budget_unavailable", False), ("output_budget_unavailable", False),
                             ("input_budget_unavailable", True)):
            self.adapter.response = {"status": "rejected" if sent else "deferred", "sent": sent,
                                     "protected": True, "reason": reason}
            self.scheduler.tick(); self.settle()
            self.assertEqual(self.rows()[-1]["state"], "rejected" if sent else "not_sent")
            self.assertEqual(self.last_detail(self.rows()[-1]), reason)
            with self.factory() as journal:
                control = journal.summary_control_state()
            self.assertEqual(control["streak"], 0); self.assertIsNone(control["protection"])
            self.assertEqual(self.compat.incidents, [])
            self.now += 300
        count = len(self.adapter.calls)
        self.scheduler.close(); self.scheduler = self.make_scheduler()
        self.scheduler.tick(); self.settle()
        self.assertEqual(len(self.adapter.calls), count)
        with self.factory() as journal: self.assertIsNone(journal.summary_control_state()["protection"])

    def test_sent_output_budget_does_not_reset_existing_contract_streak(self):
        self.adapter.response = {"status": "rejected", "sent": True, "protected": True, "reason": "invalid_result"}
        self.scheduler.tick(); self.settle(); self.now = 300; self.scheduler.tick(); self.settle()
        self.end()
        self.adapter.response = {"status": "rejected", "sent": True, "protected": True, "reason": "output_budget_unavailable"}
        self.scheduler.tick(); self.settle()
        self.assertEqual(self.last_detail(self.rows()[-1]), "output_budget_unavailable")
        with self.factory() as journal:
            state = journal.summary_control_state()
        self.assertEqual(state["streak"], 1); self.assertIsNone(state["protection"])
        self.assertEqual(self.compat.incidents, [])
        self.scheduler.close(); self.scheduler = self.make_scheduler()
        with self.factory() as journal:
            state = journal.summary_control_state()
        self.assertEqual(state["streak"], 1); self.assertIsNone(state["protection"])

    def test_safety_protects_immediately_and_reports_once(self):
        self.adapter.response = {"status": "rejected", "sent": True, "protected": True,
                                 "reason": "compatibility_failed"}
        self.scheduler.tick(); self.settle(); self.now = 300; self.scheduler.tick(); self.settle()
        self.now = 900; self.scheduler.tick(); self.settle()
        self.assertEqual(len(self.compat.incidents), 1)
        self.assertEqual(self.scheduler.status()["state"], "protected")

    def test_storage_failure_preserves_no_replay_and_does_not_increment_streak(self):
        self.adapter.response = {"status": "storage_failed", "sent": True, "protected": False,
                                 "reason": "receipt_outcome_unknown", "writer": {"durableEntryId": str(uuid4())}}
        self.scheduler.tick(); self.settle(); self.now = 300; self.scheduler.tick(); self.settle()
        self.assertEqual(self.rows()[0]["state"], "storage_failed")
        with self.factory() as journal:
            self.assertEqual(journal.summary_control_state()["streak"], 0)
        self.now = 900; self.scheduler.tick(); self.settle()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_confirmed_missing_raw_becomes_deduplicated_explicit_gap(self):
        span = {"schemaVersion": 1, "source": {"schemaVersion": 1, "sourceKind": "terminal",
            "collectorEpoch": 1, "commandId": self.command}, "start": 0, "end": 4,
            "digest": sha256(b"work").hexdigest()}
        self.sink.record_terminal_range(self.subject, str(uuid4()), span, observed_at=AT)
        self.scheduler.tick(); self.settle(); self.now = 300; self.scheduler.tick(); self.settle()
        request = self.rows()[0]["request"]
        self.assertEqual(request["sourceGaps"], [{"schemaVersion": 1, "source": span["source"],
            "start": 0, "end": 4, "reason": "storage_gap"}])
        self.assertEqual(len(request["sources"]), 1)

    def test_exact_partial_missing_consumes_original_without_partial_text(self):
        for missing_start, missing_end, reason in ((4, 8, "storage_gap"), (0, 4, "evicted")):
            command = str(uuid4()); subject = {"schemaVersion": 1, "kind": "command", "id": command}
            self.sink.record_terminal(self.events.append({"type": "terminal_started", "command_id": command,
                "task_id": None, "started_at": AT}))
            source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1, "commandId": command}
            span = {"schemaVersion": 1, "source": source, "start": 0, "end": 8, "digest": sha256(b"abcdefgh").hexdigest()}
            self.sink.record_terminal_range(subject, str(uuid4()), span, observed_at=AT)
            lo, data = (0, b"abcd") if missing_start == 4 else (4, b"efgh")
            available = {**span, "start": lo, "end": lo+4, "digest": sha256(data).hexdigest()}
            gap = {"schemaVersion": 1, "source": source, "start": missing_start, "end": missing_end, "reason": reason}
            read = {"source": source, "chunks": [{"range": available, "dataBase64": base64.b64encode(data).decode()}], "missing": [gap]}
            self.scheduler.tick(); self.settle(); self.now += 300
            with patch("workbench.backend.summary_scheduler.raw_query", return_value={"log_format": "segmented-v1", "read": read}):
                self.scheduler.tick(); self.settle()
                # The oldest previously idle command may run first; continue regular ticks for this subject.
                for _ in range(3):
                    if any(call["request"]["subject"] == subject for call in self.adapter.calls): break
                    self.scheduler.tick(); self.settle()
            selected = [call for call in self.adapter.calls if call["request"]["subject"] == subject]
            self.assertEqual(len(selected), 1, self.scheduler.status())
            request = selected[0]["request"]
            self.assertEqual(request["sourceGaps"], [gap])
            self.assertFalse(any(value["source"]["sourceKind"] == "terminal" for value in request["sources"]))
            self.assertNotIn(data.decode(), str(selected[0]))
            self.assertTrue(self.sink.authorize_summary_request(request, active=True, paused=False, revoked=False, has_new_input=True)["allowed"])
            with self.factory() as journal:
                self.assertEqual(journal.pending_summary_sources(subject), [])
            self.now += 300; self.scheduler.tick(); self.settle()
            self.assertEqual(len([call for call in self.adapter.calls if call["request"]["subject"] == subject]), 1)


class JournalControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request, _ = request_fixture(self.root, ["work"])
        self.journal = TaskRepository(self.root / "tasks.sqlite3"); self.addCleanup(self.journal.close)
        self.counter = 0

    def reserve(self):
        value = deepcopy(self.request); self.counter += 1
        value["requestId"] = str(uuid4()); span = value["sources"][0]
        span["start"], span["end"] = self.counter * 4, self.counter * 4 + 4
        value["inputKey"] = summary_input_key(value["subject"], None, value["sources"], [], value["skillDigest"])
        self.journal.reserve_summary_request(value); self.journal.record_summary_state(value["requestId"], "sending")
        return value

    def reject(self):
        value = self.reserve(); rid = value["requestId"]
        self.journal.record_summary_state(rid, "executed_observed")
        self.journal.record_summary_state(rid, "rejected", detail="invalid_result")
        return value, self.journal.record_summary_control(rid, "contract_rejected", reason="invalid_result")

    def stored(self, value):
        result = {"schemaVersion": 1, "requestId": value["requestId"], "inputKey": value["inputKey"],
            "skillDigest": value["skillDigest"], "sources": value["sources"], "facts": ["work"],
            "interpretations": [], "unknowns": [],
            "usage": {"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None}}
        self.journal.record_summary_result_receipt(result, str(uuid4()))
        self.journal.record_summary_control(value["requestId"], "stored", reason="stored")

    def test_three_rejections_survive_restart_and_incident_once(self):
        self.assertIsNone(self.reject()[1]); self.assertIsNone(self.reject()[1])
        self.assertIsNotNone(self.reject()[1])
        self.journal.close(); self.journal = TaskRepository(self.root / "tasks.sqlite3")
        self.assertEqual(self.journal.summary_control_state()["protection"], "summary_contract_failed")
        self.assertIsNone(self.reject()[1])

    def test_transient_and_storage_neither_increment_nor_reset(self):
        self.reject()
        for outcome in ("transient", "storage"):
            value = self.reserve(); self.journal.record_summary_control(value["requestId"], outcome, reason=outcome)
        self.assertEqual(self.journal.summary_control_state()["streak"], 1)
        self.stored(self.reserve()); self.assertEqual(self.journal.summary_control_state()["streak"], 0)

    def test_old_late_stored_cannot_reset_latest_streak(self):
        old = self.reserve(); self.reject(); self.stored(old)
        self.assertEqual(self.journal.summary_control_state()["streak"], 1)

    def test_recovery_requires_verify_and_rejects_once_with_new_episode(self):
        for _ in range(3): self.reject()
        old_episode = self.journal.summary_control_state()["episode"]
        with self.assertRaises(InvalidTransitionError):
            self.journal.begin_summary_recovery("unverified", boundaries_verified=False)
        attempt = self.journal.begin_summary_recovery("explicit-scripted-verify", boundaries_verified=True)
        self.assertEqual(attempt["real_model_contract"], "unverified")
        value, event = self.reject()
        self.assertIsNotNone(event); self.assertNotEqual(event["episode"], old_episode)
        self.assertEqual(self.journal.summary_control_state()["protection"], "summary_contract_failed")

    def test_recovery_old_late_and_storage_do_not_end_attempt_but_latest_stored_does(self):
        old = self.reserve()
        for _ in range(3): self.reject()
        attempt = self.journal.begin_summary_recovery("verify", boundaries_verified=True)
        self.stored(old)
        self.assertEqual(self.journal.summary_control_state()["recovery_attempt"], attempt)
        current = self.reserve(); self.journal.record_summary_control(current["requestId"], "storage", reason="full")
        self.assertEqual(self.journal.summary_control_state()["recovery_attempt"], attempt)
        self.stored(current); self.assertIsNone(self.journal.summary_control_state()["recovery_attempt"])

    def tick_without_dispatch(self, scheduler):
        scheduler.tick()
        deadline = time.monotonic() + 4
        while scheduler.status().get("busy") and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(scheduler.status().get("busy"))

    def control_only_scheduler(self):
        scheduler = SummaryScheduler(repository_factory=lambda: TaskRepository(self.root / "tasks.sqlite3"),
            adapter=None, subject_log=lambda: None, collector=None, authority_flags=lambda *_: {},
            paused=lambda: True, priority=lambda: True, compatibility=Compat())
        self.addCleanup(scheduler.close)
        return scheduler

    def test_late_receipt_applies_on_ordinary_tick_without_changing_live_sending(self):
        for _ in range(3): self.reject()
        self.journal.begin_summary_recovery("verify", boundaries_verified=True)
        scheduler = self.control_only_scheduler()
        live = self.reserve()
        late = self.reserve()
        self.journal.record_summary_state(late["requestId"], "unknown", detail="summary_outcome_unknown")
        result = {"schemaVersion": 1, "requestId": late["requestId"], "inputKey": late["inputKey"],
            "skillDigest": late["skillDigest"], "sources": late["sources"], "facts": ["valid late work"],
            "interpretations": [], "unknowns": [],
            "usage": {"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None}}
        self.journal.record_summary_result_receipt(result, str(uuid4()))
        self.tick_without_dispatch(scheduler)
        state = self.journal.summary_control_state()
        self.assertEqual(state["streak"], 0); self.assertIsNone(state["recovery_attempt"])
        self.assertIsNone(state["protection"])
        self.assertEqual(self.journal.get_summary_request(live["requestId"])["state"], "sending")
        count = self.journal._connection.execute("SELECT COUNT(*) FROM summary_control_events_v1").fetchone()[0]
        self.tick_without_dispatch(scheduler)
        self.assertEqual(self.journal._connection.execute("SELECT COUNT(*) FROM summary_control_events_v1").fetchone()[0], count)
        self.assertEqual(self.journal.get_summary_request(live["requestId"])["state"], "sending")

    def test_unknown_safety_protects_without_changing_no_replay_state(self):
        value = self.reserve()
        self.journal.record_summary_state(value["requestId"], "unknown", detail="request_mismatch")
        scheduler = self.control_only_scheduler()
        self.assertEqual(self.journal.get_summary_request(value["requestId"])["state"], "unknown")
        self.assertEqual(self.journal.summary_control_state()["protection"], "request_mismatch")
        self.assertEqual(self.journal.summary_unclassified_outcomes(), [])
        self.assertEqual(len(scheduler.compatibility.incidents), 1)
        overlap = deepcopy(value); overlap["requestId"] = str(uuid4())
        with self.assertRaises(InvalidTransitionError): self.journal.reserve_summary_request(overlap)
        count = self.journal._connection.execute("SELECT COUNT(*) FROM summary_control_events_v1").fetchone()[0]
        self.tick_without_dispatch(scheduler)
        self.assertEqual(self.journal._connection.execute("SELECT COUNT(*) FROM summary_control_events_v1").fetchone()[0], count)
        self.assertEqual(len(scheduler.compatibility.incidents), 1)

    def test_restart_classification_matches_sql_and_no_tool_completion_counts_three(self):
        self.reject()
        for reason in ("summary_outcome_unknown", "input_budget_unavailable", "output_budget_unavailable"):
            request = self.reserve()
            state = "unknown" if reason == "summary_outcome_unknown" else "rejected"
            if state == "rejected": self.journal.record_summary_state(request["requestId"], "executed_observed")
            self.journal.record_summary_state(request["requestId"], state, detail=reason)
        scheduler = self.control_only_scheduler()
        self.assertEqual(self.journal.summary_control_state()["streak"], 1)
        self.assertIsNone(self.journal.summary_control_state()["protection"])
        for _ in range(2):
            request = self.reserve()
            self.journal.record_summary_state(request["requestId"], "executed_observed")
            self.journal.record_summary_state(request["requestId"], "rejected", detail="result_unavailable")
            self.tick_without_dispatch(scheduler)
        self.assertEqual(self.journal.summary_control_state()["streak"], 3)
        self.assertEqual(self.journal.summary_control_state()["protection"], "summary_contract_failed")
        self.assertEqual(self.journal.summary_unclassified_outcomes(), [])
        count = self.journal._connection.execute("SELECT COUNT(*) FROM summary_control_events_v1").fetchone()[0]
        scheduler.close(); self.control_only_scheduler()
        self.assertEqual(self.journal._connection.execute("SELECT COUNT(*) FROM summary_control_events_v1").fetchone()[0], count)

    def test_physical_range_no_replay_ignores_epoch_and_skill_changes(self):
        value = self.reserve(); self.journal.record_summary_state(value["requestId"], "unknown")
        changed = deepcopy(value); changed["requestId"] = str(uuid4())
        changed["sources"][0]["source"]["collectorEpoch"] += 1
        changed["skillDigest"] = "a" * 64
        changed["inputKey"] = summary_input_key(changed["subject"], None, changed["sources"], [], changed["skillDigest"])
        with self.assertRaises(InvalidTransitionError): self.journal.reserve_summary_request(changed)

    def test_oldest_due_page_is_32_and_repeat_health_is_not_dirty(self):
        owners = [{"schemaVersion": 1, "kind": "command", "id": str(uuid4())} for _ in range(40)]
        for i, owner in enumerate(owners): self.journal.observe_summary_cursor(owner, 1, now=i)
        for owner in owners: self.journal.observe_summary_cursor(owner, 1, now=100)
        rows = self.journal.summary_candidates(400)
        self.assertEqual(len(rows), 32); self.assertEqual(rows[0]["due_at"], 300)


class ConfirmedSourceTests(unittest.TestCase):
    """Use existing producers and readers; the original bytes/ranges remain facts."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.layout = DataLayout(self.root)
        ensure_private_dir(self.layout.workflow)
        self.catalog = ImportantLogCatalog(self.layout.important_logs, create=True)
        self.sink = ActiveSubjectLog(self.catalog)
        self.journal = TaskRepository(self.layout.tasks); self.addCleanup(self.journal.close)
        task = self.journal.create_task({"goal": "confirmed source regression"})
        self.journal.approve_scope(task, 1, {"paths": ["fixture"]})
        self.journal.proceed(task, 1, "private fixture")
        run = self.journal.start_run(task, 1)
        self.subject = {"schemaVersion": 1, "kind": "task", "id": task}
        flow = HandoffJournal(self.layout.workflow / "tasks-flow.jsonl"); self.addCleanup(flow.close)
        self.grant = flow.append({"at": AT, "type": "delegated", "task_id": task,
            "revision": 1, "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())})
        self.sink.record_flow(self.grant)
        owner = self.owner = WorkflowRun.__new__(WorkflowRun)
        owner.repository, owner.task_id, owner.revision, owner.run_id = self.journal, task, 1, run
        owner._source_ref, owner._source_ids, owner._evidence_error = task_source(task, 1, run), [], None
        owner._record = {}; owner.subject_log_source = lambda: self.sink; owner.subject_log = None
        owner.subject_capture_expected, owner.subject_capture_unknown, owner.subject_failure = True, False, None
        directory = self.root / "workflow" / "runs" / run; ensure_private_dir(directory)
        self.raw = directory / "raw.log"
        record = directory / "run.json"
        record.write_text(json.dumps({"run_id": run, "raw_log": str(self.raw)})); record.chmod(0o600)
        self.collector = SummarySourceCollector(self.root, lambda: self.sink)

    def ingest_kind(self, kind):
        with self.catalog._lease() as fd:
            with ImportantLogStore.open(self.catalog.directory, self.catalog._current(fd)["active"], readonly=True) as store:
                entries = [store.get_entry(row[0]) for row in store._connection.execute(
                    "SELECT entry_id FROM entries WHERE subject_kind=? AND subject_id=? ORDER BY rowid",
                    (self.subject["kind"], self.subject["id"]))]
        for entry in entries:
            for span in entry["sources"]:
                if span["source"]["sourceKind"] == kind:
                    self.journal.ingest_summary_source(self.subject, span, entry["entryId"])
        return self.journal.pending_summary_sources(self.subject, revision=1)

    def request(self, pieces):
        request, metadata = request_fixture(self.root / "metadata", ["fixture"])
        authority = self.sink.authority(self.subject, active=True, paused=False, revoked=False, has_new_input=True)
        sources = [span for piece in pieces for span in piece.source_ranges]
        request.update(subject=self.subject, revision=1, sources=sources,
            authorizationRef=authority["authorization_ref"], authorizationEpoch=authority["authorization_epoch"])
        request["inputKey"] = summary_input_key(self.subject, 1, sources, [], request["skillDigest"])
        self.assertTrue(self.sink.authorize_summary_request(request, active=True, paused=False,
            revoked=False, has_new_input=True)["allowed"])
        return request, metadata

    def test_decision_fact_uses_important_copy_while_event_uses_jsonl(self):
        response = {"response_id": str(uuid4()), "decision": "승인 범위의 변경 확인"}
        self.owner._copy_decision(response)
        pieces, gaps = self.collector.collect(self.journal, self.subject, self.ingest_kind("decision"))
        expected = [json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    for value in (self.grant, response)]
        self.assertEqual([p.kind for p in pieces], ["decision", "decision"])
        self.assertEqual([p.text for p in pieces], expected)
        for piece in pieces:
            original = piece.text.encode("utf-8")
            self.assertEqual(piece.range["end"] - piece.range["start"], len(original))
            self.assertEqual(piece.range["digest"], sha256(original).hexdigest())
        self.assertEqual(gaps, []); self.request(pieces)
        command = str(uuid4()); subject = {"schemaVersion": 1, "kind": "command", "id": command}
        events = HandoffJournal(self.layout.workflow / "handoffs.jsonl"); self.addCleanup(events.close)
        event = events.append({"type": "terminal_started", "command_id": command, "task_id": None,
            "command": "source regression", "started_at": AT})
        self.sink.record_terminal(event)
        page = self.sink.summary_entries_page(None)
        for row in page["entries"]:
            if row["entry"]["subject"] == subject:
                for span in row["entry"]["sources"]:
                    self.journal.ingest_summary_source(subject, span, row["entry"]["entryId"])
        pieces, gaps = self.collector.collect(self.journal, subject, self.journal.pending_summary_sources(subject))
        self.assertEqual([json.loads(p.text) for p in pieces], [event]); self.assertEqual(gaps, [])

    def test_only_successful_important_observations_are_eligible(self):
        data = b"one\nmissnext"; self.raw.write_bytes(data); self.raw.chmod(0o600)
        real = self.sink.record_terminal_range
        def copy(*args, **kwargs):
            if args[2]["start"] == 4: raise OSError("private important copy failure")
            return real(*args, **kwargs)
        with patch.object(self.sink, "record_terminal_range", side_effect=copy):
            self.owner._record_observed([(0, data[:4], False), (4, data[4:8], False), (8, data[8:], False)])
        self.assertEqual(len(self.journal.get_log_sources(self.subject)), 3)
        self.assertTrue(self.owner.subject_capture_unknown)
        records = self.ingest_kind("terminal")
        self.assertEqual([r["range"]["start"] for r in records], [0, 8])
        pieces, gaps = self.collector.collect(self.journal, self.subject, records)
        self.assertEqual([p.text for p in pieces], ["one\n", "next"]); self.assertEqual(gaps, [])
        self.request(pieces)

    def test_actual_matcher_utf8_split_is_pending_then_grouped_without_changing_spans(self):
        data = "가\n".encode(); self.raw.write_bytes(data); self.raw.chmod(0o600)
        matcher = _ExactByteMatcher(b"DONE")
        emitted = matcher.feed(data); self.owner._record_observed(emitted)
        records = self.ingest_kind("terminal")
        self.assertEqual([(r["range"]["start"], r["range"]["end"]) for r in records], [(0, 1)])
        self.assertEqual(self.collector.collect(self.journal, self.subject, records), ([], []))
        self.assertEqual(len(self.journal.pending_summary_sources(self.subject, revision=1)), 1)
        self.owner._record_observed(matcher.finish())
        records = self.ingest_kind("terminal")
        pieces, gaps = self.collector.collect(self.journal, self.subject, records)
        originals = [r["range"] for r in records]
        self.assertEqual([(r["start"], r["end"]) for r in originals], [(0, 1), (1, 4)])
        self.assertEqual([p.text for p in pieces], ["가\n"]); self.assertEqual(gaps, [])
        self.assertEqual(list(pieces[0].source_ranges), originals)
        request, metadata = self.request(pieces)
        prepared = prepare_summary(request, pieces, metadata=metadata, instructions="task")
        self.assertEqual(prepared["request"]["sources"], originals)
        self.assertEqual(prepared["selected"], [{"ranges": originals, "text": "가\n"}])
        self.raw.write_bytes(data[:1])
        with self.assertRaisesRegex(SummaryDeferred, "source_encoding_unavailable"):
            self.collector.collect(self.journal, self.subject, records)


if __name__ == "__main__":
    unittest.main()
