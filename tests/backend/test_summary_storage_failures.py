"""Actual observed stdout wiring; private fixtures, no SDK/model claim."""
from hashlib import sha256
from pathlib import Path
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.backend.flow_terminal import TerminalService, _Command
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.summary_scheduler import SummarySourceCollector
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog, ImportantLogStore
from workbench.storage.log_raw.port import task_source
from workbench.tasks.repository import TaskRepository
from workbench.workflow.run import WorkflowRun

AT = "2026-10-12T00:00:00Z"


class ObservedSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.layout = DataLayout(self.root)
        ensure_private_dir(self.layout.workflow / "terminal")
        self.catalog = ImportantLogCatalog(self.layout.important_logs, create=True)
        self.sink = ActiveSubjectLog(self.catalog)
        self.factory = lambda: TaskRepository(self.layout.tasks)
        with self.factory(): pass
        self.failures = []; self.seq = 0
        self.service = TerminalService.__new__(TerminalService)
        self.service._repository_factory = self.factory
        self.service._subject_log = self.sink
        self.service._mark_subject_unknown = lambda: self.failures.append(True)

    def command(self, *, started=True, echo=b""):
        identity = str(uuid4()); path = self.layout.workflow / "terminal" / (identity + ".log")
        path.touch(mode=0o600)
        command = _Command(identity, "fixture work", {}, None, path, echo=echo)
        if started:
            self.seq += 1
            self.sink.record_terminal({"seq": self.seq, "at": AT, "type": "terminal_started",
                "command_id": identity, "task_id": None, "started_at": AT})
        command.summary_started = started
        self.service._configure_summary_capture(command)
        return command

    @staticmethod
    def records(repository, subject):
        return [{**row, "range": {"schemaVersion": 1, **{key: row[key] for key in ("source", "start", "end", "digest")}}}
                for row in repository.get_log_sources(subject)]

    def entries(self, subject):
        with self.catalog._lease() as fd:
            with ImportantLogStore.open(self.catalog.directory, self.catalog._current(fd)["active"], readonly=True) as store:
                return [store.get_entry(row[0]) for row in store._connection.execute(
                    "SELECT entry_id FROM entries WHERE subject_kind=? AND subject_id=?", (subject["kind"], subject["id"]))]

    def test_taskless_echo_filtered_offset_digest_and_summary_input(self):
        command = self.command(echo=b"[worker] $ fixture\r\n")
        command.write(b"[worker] $ fi"); command.write(b"xture\r\nfirst\n"); command.write(b"second\n")
        subject = {"schemaVersion": 1, "kind": "command", "id": command.command_id}
        with self.factory() as repository:
            rows = self.records(repository, subject)
            self.assertEqual([(r["range"]["start"], r["range"]["end"]) for r in rows], [(0, 6), (6, 13)])
            self.assertEqual([r["range"]["digest"] for r in rows], [sha256(b"first\n").hexdigest(), sha256(b"second\n").hexdigest()])
            for r in rows: r["cursor"] = 1
            pieces, gaps = SummarySourceCollector(self.root, lambda: self.sink).collect(repository, subject, rows)
        self.assertEqual([p.text for p in pieces], ["first\n", "second\n"])
        self.assertEqual(gaps, []); self.assertEqual(command.log_path.read_bytes(), b"first\nsecond\n")
        self.assertEqual(len(self.entries(subject)), 3); self.assertFalse(command.summary_failed)
        span = rows[0]["range"]
        self.assertFalse(self.sink.record_terminal_range(subject, rows[0]["source_id"], span, observed_at=AT))
        foreign = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        with self.assertRaises(ValueError): self.sink.record_terminal_range(foreign, str(uuid4()), span, observed_at=AT)

    def test_start_unknown_or_sink_failure_latches_capture_without_losing_output(self):
        for started in (False, True):
            command = self.command(started=started)
            if started:
                def fail(*args, **kwargs): raise OSError("fixture sink failure")
                self.service._subject_log = type("FailingSink", (), {"record_terminal_range": staticmethod(fail)})()
                self.service._configure_summary_capture(command)
            command.write(b"one\n"); command.write(b"two\n")
            self.assertTrue(command.summary_failed)
            self.assertEqual(command.summary_observed, 8)
            self.assertEqual(command.log_path.read_bytes(), b"one\ntwo\n")
            self.assertEqual(command.tail()["output_tail"], "one\ntwo\n")
            subject = {"schemaVersion": 1, "kind": "command", "id": command.command_id}
            with self.factory() as repository:
                self.assertEqual(len(repository.get_log_sources(subject)), int(started))
        self.assertEqual(len(self.failures), 2)

    def test_start_copy_failure_never_attributes_later_stdout(self):
        command = self.command(started=False)
        record = {"seq": 1, "at": AT, "type": "terminal_started", "command_id": command.command_id,
                  "task_id": None, "started_at": AT}
        self.service._handoffs = type("Journal", (), {"record": staticmethod(lambda _: record)})()
        self.service._active_task = lambda: None
        self.service._subject_capture_failure = lambda: None
        def fail(*args, **kwargs): raise OSError("start copy failure")
        self.service._subject_log = type("Sink", (), {"record_terminal": staticmethod(fail)})()
        command.summary_started = self.service._journal(record, bound_revision=None) is not None
        self.service._configure_summary_capture(command)
        command.write(b"work"); command.write(b"next")
        self.assertFalse(command.summary_started); self.assertTrue(command.summary_failed)
        subject = {"schemaVersion": 1, "kind": "command", "id": command.command_id}
        with self.factory() as repository: self.assertEqual(repository.get_log_sources(subject), [])
        self.assertEqual(command.log_path.read_bytes(), b"worknext")

    def test_raw_failure_still_records_the_exact_observation(self):
        command = self.command(); command.log_path.unlink()
        command.write(b"work"); command.write(b"next")
        self.assertIsNotNone(command.log_error); self.assertFalse(command.summary_failed)
        subject = {"schemaVersion": 1, "kind": "command", "id": command.command_id}
        with self.factory() as repository:
            rows = self.records(repository, subject)
            for row in rows: row["cursor"] = 1
            pieces, gaps = SummarySourceCollector(self.root, lambda: self.sink).collect(repository, subject, rows)
        self.assertEqual(pieces, []); self.assertEqual([(g["start"], g["end"]) for g in gaps], [(0, 4), (4, 8)])
        self.assertEqual(command.tail()["output_tail"], "worknext")

    def test_legacy_cap_exact_tail_gap_and_full_available_digest(self):
        command = self.command()
        with patch("workbench.backend.flow_terminal.LOG_MAX", 4): command.write(b"abcdefgh")
        subject = {"schemaVersion": 1, "kind": "command", "id": command.command_id}
        with self.factory() as repository:
            rows = self.records(repository, subject)
            for row in rows: row["cursor"] = 1
            pieces, gaps = SummarySourceCollector(self.root, lambda: self.sink).collect(repository, subject, rows)
        self.assertEqual(pieces, []); self.assertEqual([(g["start"], g["end"]) for g in gaps], [(4, 8)])
        self.assertEqual(command.log_path.read_bytes(), b"abcd"); self.assertTrue(command.log_truncated)
        self.assertTrue(any("생략됨" in text for entry in self.entries(subject) for text in entry["body"]["unknowns"]))
        before = len(self.entries(subject))
        with self.factory() as repository: SummarySourceCollector(self.root, lambda: self.sink).collect(repository, subject, rows)
        self.assertEqual(len(self.entries(subject)), before)
        command = self.command(); command.write(b"verified")
        subject = {"schemaVersion": 1, "kind": "command", "id": command.command_id}
        with self.factory() as repository:
            rows = self.records(repository, subject)
            for row in rows: row["cursor"] = 1
            pieces, gaps = SummarySourceCollector(self.root, lambda: self.sink).collect(repository, subject, rows)
            self.assertEqual([p.text for p in pieces], ["verified"]); self.assertEqual(gaps, [])
            command.log_path.write_bytes(b"modified"); os.chmod(command.log_path, 0o600)
            from workbench.backend.summary_adapter import SummaryDeferred
            with self.assertRaisesRegex(SummaryDeferred, "source_digest_mismatch"):
                SummarySourceCollector(self.root, lambda: self.sink).collect(repository, subject, rows)

    def test_task_binding_and_segmented_end_mismatch_fail_closed(self):
        command = self.command(); command.task_id = str(uuid4()); command.summary_revision = None
        self.service._configure_summary_capture(command)
        command.write(b"work"); self.assertTrue(command.summary_failed)
        command = self.command()
        command.segmented_raw_port = type("BadEnd", (), {"append": staticmethod(lambda *_: {
            "end": 3, "stored_bytes": 4, "missing_bytes": 0, "storage_error": None})})()
        command.write(b"work"); command.write(b"next")
        self.assertTrue(command.summary_failed); self.assertEqual(command.tail()["output_tail"], "worknext")
        subject = {"schemaVersion": 1, "kind": "command", "id": command.command_id}
        with self.factory() as repository: self.assertEqual(repository.get_log_sources(subject), [])

    def test_actual_disabled_segmented_descriptor_reader_shape(self):
        from workbench.storage.log_raw.port import DisabledSegmentedRawPort
        from workbench.storage.log_raw.segments import SegmentedRawStore
        store = SegmentedRawStore(self.root / "raw-v2", segment_limit=4096)
        self.addCleanup(store.close)
        port = DisabledSegmentedRawPort(store)
        command = self.command()
        path, descriptor = port.prepare_command(command.log_path.parent, command.command_id)
        command.log_path, command.raw_descriptor, command.segmented_raw_port = path, descriptor, port
        command.write(b"segmented output")
        subject = {"schemaVersion": 1, "kind": "command", "id": command.command_id}
        with self.factory() as repository:
            rows = self.records(repository, subject)
            for row in rows: row["cursor"] = 1
            pieces, gaps = SummarySourceCollector(self.root, lambda: self.sink, segmented=port).collect(repository, subject, rows)
        self.assertEqual([p.text for p in pieces], ["segmented output"]); self.assertEqual(gaps, [])
        self.assertFalse(command.summary_failed)

    def test_workflow_more_than_100_drains_all_copied_without_backfill(self):
        with self.factory() as repository:
            task = repository.create_task({"goal": "observed fixture"})
            repository.approve_scope(task, 1, {"paths": ["fixture"]})
            repository.proceed(task, 1, "approved fixture")
            run = repository.start_run(task, 1)
        subject = {"schemaVersion": 1, "kind": "task", "id": task}
        self.sink.record_flow({"seq": 1, "at": AT, "type": "delegated", "task_id": task, "revision": 1,
            "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())})
        owner = WorkflowRun.__new__(WorkflowRun)
        with self.factory() as repository:
            owner.repository, owner.task_id, owner.revision, owner.run_id = repository, task, 1, run
            owner._source_ref = task_source(task, 1, run); owner._evidence_error = None; owner._source_ids = []
            owner.subject_log_source = lambda: self.sink; owner.subject_log = None
            owner.subject_capture_expected = True; owner.subject_capture_unknown = False; owner.subject_failure = lambda: self.failures.append(True)
            started = time.monotonic()
            owner._record_observed([(i * 4, b"work", False) for i in range(105)])
            elapsed = time.monotonic() - started
            self.assertEqual(len(owner._source_ids), 105)
        spans = [span for entry in self.entries(subject) for span in entry["sources"] if span["source"]["sourceKind"] == "terminal"]
        self.assertEqual(len(spans), 105); self.assertEqual({s["start"] for s in spans}, set(range(0, 420, 4)))
        self.assertFalse(owner.subject_capture_unknown); self.assertEqual(self.failures, [])
        print(f"observed_105_drains_seconds={elapsed:.6f}")


if __name__ == "__main__": unittest.main()
