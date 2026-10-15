"""LG-D31/32 validator and source-byte regressions; fixture evidence, Native0."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests.backend import test_summary_compatibility_independent as records
from workbench.backend.flow import HandoffJournal
from workbench.backend.paths import DataLayout, ensure_private_dir, write_private_json
from workbench.backend.summary_scheduler import SummarySourceCollector
from workbench.contracts.logs_v1 import canonical_json
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog
from workbench.storage.important_log.lifecycle import source_json
from workbench.tasks.repository import TaskRepository


EIGHT = {"Q01-route", "Q01-tools", "Q01-skill", "Q01-result", "Q01-context",
         "Q01-priority", "Q01-recovery", "Q01-origin"}


class CompletionEvidenceIndependentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = records.PersistedCompatibilityIndependentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service

    def write(self, record):
        # Deliberately bypass baseline metadata pairing: raw/top mismatch must
        # remain observable to the actual persisted consumer.
        write_private_json(self.fixture.directory / (record["id"] + ".json"), record)
        write_private_json(self.fixture.directory / "state.json", {
            "latest_record": record["id"], "record_sha256": records.digest(record),
            "protection": None, "episode": 0})

    def reject(self, record):
        self.write(record)
        self.assertFalse(self.service.admission())
        self.assertFalse(self.service.verify_reference(record["id"]))

    def test_eight_claims_require_raw_observations_and_exact_hashed_refs(self):
        baseline = self.fixture.record()
        self.write(baseline)
        self.assertTrue(self.service.admission())
        self.assertTrue(self.service.verify_reference(baseline["id"]))
        self.assertEqual({row["item"] for row in baseline["item_rows"]}, EIGHT)
        self.assertEqual(len(baseline["item_rows"]), 8)
        for row in baseline["item_rows"]:
            self.assertEqual(row["result"], "pass")
            self.assertEqual(row["unknowns"], [])
            for ref in row["evidence_refs"]:
                raw = baseline["observations"][ref["observation_index"]]
                self.assertEqual(ref["kind"], raw["kind"])
                self.assertEqual(ref["sha256"], records.digest(raw))
        changes = {
            "bare_flags": lambda r: r.pop("item_rows"),
            "raw_tool_violation": lambda r: r["observations"][0]["localhost_model_posts"][0].update(tools=["bash"]),
            "missing_ref": lambda r: r["item_rows"][0].update(evidence_refs=[]),
            "wrong_ref_hash": lambda r: r["item_rows"][0]["evidence_refs"][0].update(sha256="0" * 64),
        }
        for label, change in changes.items():
            with self.subTest(boundary=label):
                bad = deepcopy(baseline)
                change(bad)
                self.reject(bad)

    def test_configured_readback_and_only_closed_io_caveats_are_required(self):
        baseline = self.fixture.record()
        self.write(baseline)
        self.assertTrue(self.service.admission(), "three soft metadata unknowns and closed IO caveats")
        for label, change in (
            ("missing_readback", lambda r: r["observations"].pop()),
            ("different_epoch", lambda r: r["observations"][-1]["metadata"].update(epoch="other-peer-epoch")),
            ("unapproved_caveat", lambda r: r["caveats"].append("arbitrary_safety_unknown")),
        ):
            with self.subTest(boundary=label):
                bad = deepcopy(baseline)
                change(bad)
                self.reject(bad)

    def test_verifier_persists_explicit_rows_but_bare_flags_keep_protection(self):
        baseline = self.fixture.record()
        observed = {"level": "pass", "observations": baseline["observations"],
            "items": baseline["items"], "item_rows": baseline["item_rows"],
            "model_metadata": baseline["model_metadata"], "omp": baseline["omp"],
            "unknowns": [], "caveats": baseline["caveats"],
            "io": {"model_calls": None, "omp_children": None, "network_requests": None}}
        with patch("workbench.backend.summary_compat_provider.ScriptedCompatibilityProvider.observe", return_value=observed), \
             patch("subprocess.Popen", side_effect=AssertionError("Native forbidden")):
            result = self.service.verify_scripted()
        self.assertEqual(result["result"], "pass")
        self.assertEqual(result["item_rows"], baseline["item_rows"])
        persisted = json.loads((self.fixture.directory / (result["id"] + ".json")).read_bytes())
        self.assertEqual(persisted["observations"], baseline["observations"])
        self.assertIsNone(persisted["io"]["network_requests"])
        self.assertTrue(self.service.admission())
        self.service.protect({"reason": "prior_safety_failure"})
        bare = deepcopy(observed)
        bare.pop("item_rows")
        with patch("workbench.backend.summary_compat_provider.ScriptedCompatibilityProvider.observe", return_value=bare), \
             patch("subprocess.Popen", side_effect=AssertionError("Native forbidden")):
            partial = self.service.verify_scripted()
        self.assertNotEqual(partial["result"], "pass")
        self.assertEqual(set(partial["items"].values()), {"not_run"})
        self.assertFalse(self.service.admission())
        self.assertEqual(self.service.status()["reason"], "prior_safety_failure")

    def test_current_partial_omp_admission_does_not_forge_exact_activation(self):
        record = self.fixture.record()
        self.write(record)
        original_bytes = (self.fixture.directory / (record["id"] + ".json")).read_bytes()
        self.fixture.omp.update(version=None, bundled_sha256=None)
        status = self.service.status()
        self.assertTrue(self.service.admission())
        self.assertFalse(status["current_omp_exact"])
        self.assertEqual(status["current_omp"], self.fixture.omp)
        self.assertEqual(set(status["current_omp_unknowns"]), {"version_unavailable", "bundled_sha256_unavailable"})
        self.assertFalse(self.service.verify_reference(record["id"]))
        self.assertEqual((self.fixture.directory / (record["id"] + ".json")).read_bytes(), original_bytes)
        partial_omp = deepcopy(self.fixture.omp)
        for field, malformed in (("version", "   "), ("bundled_sha256", "not-hex")):
            with self.subTest(current_field=field, malformed=malformed):
                self.fixture.omp = {**partial_omp, field: malformed}
                self.assertFalse(self.service.admission(), "observed malformed values are not unknown")
                self.assertEqual(self.service.status()["state"], "unavailable")
                self.assertFalse(self.service.verify_reference(record["id"]))
        self.fixture.omp = partial_omp
        self.fixture.omp["version"] = "actually-observed-new-version"
        self.assertEqual(self.service.status()["state"], "version_drift_unverified")
        self.fixture.omp.pop("binary_sha256")
        self.assertFalse(self.service.admission())
        self.assertEqual(self.service.status()["state"], "unavailable")


class TerminalSourceBytesIndependentTests(unittest.TestCase):
    def test_finite_duration_survives_actual_journal_lifecycle_and_collector(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = DataLayout(root)
            ensure_private_dir(layout.workflow)
            sink = ActiveSubjectLog(ImportantLogCatalog(layout.important_logs, create=True))
            command = str(uuid4())
            subject = {"schemaVersion": 1, "kind": "command", "id": command}
            events = HandoffJournal(layout.workflow / "handoffs.jsonl")
            try:
                started = events.append({"type": "terminal_started", "command_id": command,
                    "task_id": None, "command": "owned float fixture", "started_at": "2026-10-15T00:00:00Z"})
                ended = events.append({"type": "terminal_ended", "command_id": command,
                    "status": "exited", "ended_at": "2026-10-15T00:00:01Z", "duration": 0.375})
                self.assertTrue(sink.record_terminal(started))
                self.assertTrue(sink.record_terminal(ended))
            finally:
                events.close()
            with TaskRepository(layout.tasks) as journal:
                for row in sink.summary_entries_page(None)["entries"]:
                    for span in row["entry"]["sources"]:
                        journal.ingest_summary_source(subject, span, row["entry"]["entryId"])
                pieces, gaps = SummarySourceCollector(root, lambda: sink).collect(
                    journal, subject, journal.pending_summary_sources(subject))
            expected = [json.dumps(value, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":"), allow_nan=False).encode() for value in (started, ended)]
            self.assertEqual([piece.text.encode() for piece in pieces], expected)
            self.assertEqual(gaps, [])
            self.assertIs(type(json.loads(pieces[-1].text)["duration"]), float)
            for piece, payload in zip(pieces, expected):
                self.assertEqual(piece.range["end"] - piece.range["start"], len(payload))
                self.assertEqual(piece.range["digest"], sha256(payload).hexdigest())
            self.assertEqual(expected[0], canonical_json(started), "old integer-only encoding unchanged")

    def test_nonfinite_source_rejected_without_relaxing_wire_canonical(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(duration=repr(value)):
                with self.assertRaises(ValueError):
                    source_json({"duration": value})
        self.assertEqual(source_json({"duration": 3}), canonical_json({"duration": 3}))
        self.assertEqual(source_json({"duration": 0.375}), b'{"duration":0.375}')
        with self.assertRaises(ValueError):
            canonical_json({"duration": 0.375})
