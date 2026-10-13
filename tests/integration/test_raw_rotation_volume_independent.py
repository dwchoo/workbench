"""Default 100MB x 5 real-byte producer/reader test; policy admission is fixtureproof.

Controlled display drains replace the shell, not raw writes, proof capture or readers.
No scheduler/model dispatch is present: urgent calls and full-product mixed-run pass
remain untested. The 500MB private raw store is removed, never saved as evidence.
"""
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import resource
import shutil
import time
import unittest
from uuid import uuid4

from tests.backend import test_raw_rotation_independent as rotation_fixtures
from tests.backend import test_summary_scheduler_independent as producer_fixtures
from workbench.backend.flow import HandoffJournal
from workbench.backend.flow_terminal import _Command, TAIL_BYTES, TerminalService
from workbench.backend.paths import DataLayout, ensure_private_dir, write_private_json
from workbench.backend.summary_scheduler import SummarySourceCollector
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog, ImportantLogStore
from workbench.tasks.repository import TaskRepository
from workbench.workflow.run import _ExactByteMatcher


VOLUME_TIMEOUT_SECONDS = 600


class OneDrain:
    """One bounded owned display chunk; no queue or child process."""
    data = b""

    def display_bytes(self):
        data, self.data = self.data, b""
        return data


class RawRotationVolumeIndependentTests(unittest.TestCase):
    def test_default_volume_mixed_producers_preserve_proof_gaps_and_legacy(self):
        started = time.monotonic()
        report = {"boundary": "default store + Backend singleton/read port + controlled WorkflowRun drain and Command.write + real collector",
                  "policy_evidence": "synthetic validator fixtureproof, not actual Q01/MODEL pass",
                  "urgent_model_call_count": "unknown/untested: no scheduler or model dispatch in this boundary",
                  "time_limit_seconds": VOLUME_TIMEOUT_SECONDS, "STOP": [], "owned_processes": [], "owned_threads": []}
        fixture, reference, _ = rotation_fixtures.validator_fixture(self)
        backend = rotation_fixtures.backend_fixture(self, fixture)
        self.root, self.layout = fixture.root, DataLayout(fixture.root)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertGreater(shutil.disk_usage(self.root).free, 1_000_000_000)
        ensure_private_dir(self.layout.workflow / "terminal")
        self.catalog = ImportantLogCatalog(self.layout.important_logs, create=True)
        self.sink = ActiveSubjectLog(self.catalog)
        self.factory = lambda: TaskRepository(self.layout.tasks)
        self.failures = []
        owner = events = None
        body_completed = False
        try:
            # A pre-existing source stays legacy; the fixture activates only this private root.
            legacy = self.layout.workflow / "terminal" / (str(uuid4()) + ".log")
            legacy.write_bytes(b"legacy cohort must remain byte-identical\n"); legacy.chmod(0o600)
            legacy_before = sha256(legacy.read_bytes()).hexdigest()
            self.assertIsNone(backend._new_raw_port())
            backend.raw_rotation.activate(reference)
            port = backend._new_raw_port()
            self.assertIs(port, backend._new_raw_port())
            self.assertEqual((port.store.segment_limit, port.store.segment_count), (100_000_000, 5))
            owner, task_subject = producer_fixtures.ProducerCaptureBoundaryTests.task_run(self)
            run_dir = self.layout.workflow / "runs" / owner.run_id
            ensure_private_dir(run_dir)
            owner.raw_log, owner.raw_descriptor = port.prepare_task(run_dir, owner.task_id, 1, owner.run_id)
            owner.segmented_raw_port, owner.shell = port, OneDrain()
            marker = b"PASS-volume-proof"
            owner._matcher = _ExactByteMatcher(marker)
            write_private_json(run_dir / "run.json", {"run_id": owner.run_id, "log_format": "segmented-v1",
                "raw_log_ref": owner.raw_descriptor["raw_log_ref"], "raw_source": owner.raw_descriptor["source"]})
            command_id = str(uuid4())
            events = HandoffJournal(self.layout.workflow / "handoffs.jsonl")
            self.sink.record_terminal(events.append({"type": "terminal_started", "command_id": command_id,
                "task_id": None, "command": "private volume fixture", "started_at": "2026-10-13T00:00:00Z"}))
            command_path, descriptor = port.prepare_command(self.layout.workflow / "terminal", command_id)
            command = _Command(command_id, "private volume fixture", {}, None, command_path)
            command.segmented_raw_port, command.raw_descriptor, command.summary_started = port, descriptor, True
            terminal = TerminalService.__new__(TerminalService)
            terminal._repository_factory, terminal._subject_log = self.factory, self.sink
            terminal._mark_subject_unknown = lambda: self.failures.append("capture_unknown")
            terminal._configure_summary_capture(command)
            command_subject = {"schemaVersion": 1, "kind": "command", "id": command_id}

            # Small actual API probe before bulk generation, including proof and fixed-root reads.
            prefix = marker + b"r" * (65536 - len(marker))
            owner.shell.data = prefix; owner._drain()
            command.write(b"c" * 65536)
            proof_before = owner.repository.get_condition_proofs(owner.run_id)
            self.assertEqual(len(proof_before), 1); self.assertTrue(proof_before[0]["matched"])
            self.assertEqual(proof_before[0]["condition_digest"], sha256(marker).hexdigest())
            initial_run = backend._raw_source_query("run", owner.run_id, offset=0, limit=len(marker))
            self.assertEqual(base64.b64decode(initial_run["read"]["chunks"][0]["dataBase64"]), marker)
            self.assertEqual(backend._raw_source_query("command", command_id, offset=0, limit=65536)["status"], "available")
            self.assertIsNone(owner._evidence_error); self.assertFalse(command.summary_failed)
            report["small_probe"] = "passed"

            total, chunk_bound = 2 * 65536, 4 * 1024 * 1024
            pattern = {"run": b"r" * chunk_bound, "command": b"c" * chunk_bound}
            written = {"run": 65536, "command": 65536}
            peak_raw, peak_files, max_segment = 0, 0, 0
            buffers = {"recent": 0, "since": 0, "unseen": 0, "matcher_pending": 0}
            index = 0
            while total < 500_000_000:
                self.assertLess(time.monotonic() - started, VOLUME_TIMEOUT_SECONDS, "owned volume time bound")
                kind = "run" if index % 2 == 0 else "command"
                size = min(chunk_bound, 500_000_000 - total)
                data = pattern[kind] if size == chunk_bound else pattern[kind][:size]
                if kind == "run":
                    owner.shell.data = data; owner._drain()
                    self.assertIsNone(owner._record["raw_log_status"]["storage_error"])
                else:
                    command.write(data); self.assertIsNone(command.log_error)
                written[kind] += size; total += size; index += 1
                sizes = port.store.segment_sizes()
                self.assertLessEqual(len(sizes), 5); self.assertLessEqual(max(sizes), 100_000_000)
                self.assertLessEqual(sum(sizes), 500_000_000)
                peak_raw = max(peak_raw, sum(sizes)); peak_files = max(peak_files, len(sizes))
                max_segment = max(max_segment, max(sizes))
                for name in ("recent", "since", "unseen"):
                    buffers[name] = max(buffers[name], len(getattr(command, name)))
                    self.assertLessEqual(buffers[name], 4 * TAIL_BYTES)
                buffers["matcher_pending"] = max(buffers["matcher_pending"], len(owner._matcher.pending))
                self.assertLessEqual(buffers["matcher_pending"], len(marker) - 1)
            self.assertEqual(total, 500_000_000); self.assertEqual(peak_files, 5)
            self.assertGreater(peak_raw, 490_000_000); self.assertEqual(max_segment, 100_000_000)
            self.assertEqual(self.failures, []); self.assertFalse(owner.subject_capture_unknown)
            self.assertIsNone(owner._evidence_error); self.assertFalse(command.summary_failed)
            accounting = {}
            for kind, identity, desc in (("run", owner.run_id, owner.raw_descriptor), ("command", command_id, descriptor)):
                status = port.status(desc); accounting[kind] = status
                self.assertEqual(status["observed_bytes"], written[kind]); self.assertGreater(status["evicted_bytes"], 0)
                self.assertEqual(status["missing_bytes"], 0)
                self.assertEqual(status["retained_bytes"] + status["evicted_bytes"], written[kind])
                self.assertEqual({gap["reason"] for gap in status["missing_ranges"]}, {"evicted"})
                self.assertGreater(status["retained_bytes"], 0)
                page = backend._raw_source_query(kind, identity, offset=written[kind] - 65536, limit=65536)
                self.assertEqual(page["status"], "available")
                actual = b"".join(base64.b64decode(item["dataBase64"]) for item in page["read"]["chunks"])
                expected = (b"r" if kind == "run" else b"c") * 65536
                self.assertEqual(actual, expected)
                for item in page["read"]["chunks"]:
                    self.assertEqual(item["range"]["digest"], sha256(base64.b64decode(item["dataBase64"])).hexdigest())

            report.update(payload_bytes=total, source_payload_bytes=written,
                peak_raw_file_bytes=peak_raw, peak_segment_count=peak_files, largest_segment_bytes=max_segment,
                final_usage=port.store.storage_usage(), source_accounting=accounting,
                observed_buffer_bytes=buffers, producer_chunk_bound=chunk_bound,
                rss_peak_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

            collector = SummarySourceCollector(self.root, lambda: self.sink, raw_query_port=backend._raw_source_query)
            collected_gaps = {}
            for subject, source, end, body in ((task_subject, owner.raw_descriptor["source"], len(marker), marker),
                    (command_subject, descriptor["source"], 65536, b"c" * 65536)):
                # The span is actual producer evidence, not fabricated summary/model output.
                span = {"schemaVersion": 1, "source": source, "start": 0, "end": end, "digest": sha256(body).hexdigest()}
                source_id = next(row["source_id"] for row in owner.repository.get_log_sources(subject)
                                 if all(row[key] == span[key] for key in ("source", "start", "end", "digest")))
                record = {"source_id": source_id, "cursor": 1, "range": span}
                pieces, gaps = collector.collect(None, subject, [record])
                self.assertEqual(pieces, [])
                self.assertTrue(gaps)
                cursor = 0
                for gap in gaps:
                    self.assertEqual(gap["schemaVersion"], 1)
                    self.assertEqual(gap["source"], source)
                    self.assertEqual(gap["reason"], "evicted")
                    self.assertGreaterEqual(gap["start"], 0)
                    self.assertLess(gap["start"], gap["end"])
                    self.assertLessEqual(gap["end"], end)
                    self.assertEqual(gap["start"], cursor)
                    cursor = gap["end"]
                self.assertEqual(cursor, end)
                collected_gaps[subject["kind"]] = gaps
            self.assertEqual(owner.repository.get_condition_proofs(owner.run_id), proof_before,
                             "raw eviction and summary read gaps must not rewrite observed proof")
            with self.catalog._lease() as fd:
                with ImportantLogStore.open(self.catalog.directory, self.catalog._current(fd)["active"], readonly=True) as db:
                    persisted = [gap for row in db._connection.execute("SELECT source_gaps_json FROM entries") for gap in json.loads(row[0])]
            self.assertCountEqual(persisted, [gap for values in collected_gaps.values() for gap in values])
            self.assertEqual(sha256(legacy.read_bytes()).hexdigest(), legacy_before)
            self.assertEqual(legacy.stat().st_mode & 0o777, 0o600)
            body_completed = True
        finally:
            close_ok = False
            try:
                try:
                    backend._close_owned_raw_store()
                    if events is not None: events.close()
                    if owner is not None: owner.repository.close()
                    close_ok = True
                except BaseException:
                    # Keep an existing body failure primary; record only observed cleanup status.
                    if body_completed: raise
            finally:
                cleanup_ok = fixture.doCleanups()
                removed = not self.root.exists()
                report.update(body_completed=body_completed, close_ok=close_ok, cleanup_ok=cleanup_ok,
                    elapsed_seconds=round(time.monotonic() - started, 3), owned_temp_removed=removed)
                output = os.environ.get("DVL14_VOLUME_RESULT")
                if output:
                    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        self.assertTrue(close_ok)
        self.assertTrue(cleanup_ok)
        self.assertTrue(removed)
        report.update(volume_result="passed", proof_preserved=True,
            collector_gaps=collected_gaps, legacy_sha256=legacy_before)
        if output:
            Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
