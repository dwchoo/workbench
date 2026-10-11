"""Independent byte and timing checks on one retained scripted OMP priority run."""

from __future__ import annotations

import base64
import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from tests.backend.q01_sdk_full_probe import current_probe


def load(path: Path):
    return json.loads(path.read_text())


def hash_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


class MeasurementIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        names = ("Q01_CURRENT_BATCH", "Q01_CURRENT_REJUDGE", "Q01_CURRENT_TEST_DIR")
        if any(not os.environ.get(name) for name in names):
            raise AssertionError(f"explicit {names} required")
        cls.base = Path(os.environ[names[0]]).resolve(strict=True)
        cls.rejudge = load(Path(os.environ[names[1]]).resolve(strict=True))
        cls.output = Path(os.environ[names[2]]).resolve(strict=True)
        cls.index = load(cls.base / "stage-index.json")
        cls.paths = {name: Path(record["artifact"]) for name, record in cls.index["stages"].items()}
        cls.paths["priority_runtime"] = Path(cls.rejudge["new_priority_artifact"])
        cls.stages = {name: load(path) for name, path in cls.paths.items()}
        binding_path = Path(cls.index["stages"]["contract"]["task_binding"]["artifact"])
        binding = load(binding_path)
        cls.stages["contract"]["task_binding"] = {
            "path": str(binding_path), "checks": binding["checks"],
            "bindings": binding["bindings"], "role_only": binding["role_only"]}
        cls.priority = cls.stages["priority_runtime"]
        cls.private = cls.paths["priority_runtime"].parent / "private"

    def compose(self, stages: dict) -> dict:
        with tempfile.TemporaryDirectory(prefix="measurement-copy-", dir=self.output) as folder:
            readback = next(row for row in self.stages["prepare"]["trace"]
                            if row.get("event") == "readback")
            observed = current_probe._compose({"isolated_root": folder, "prepared_readback": readback},
                                              stages, self.paths, [])
            observed["boundaries"] = load(Path(folder) / "capability-boundaries.json")
            return observed

    def test_actual_wire_and_persisted_result(self) -> None:
        priority = self.priority
        self.assertEqual(hash_file(self.paths["priority_runtime"]),
                         self.rejudge["new_priority_artifact_sha256"])
        self.assertEqual(self.rejudge["current_collector_sha256"],
                         hash_file(Path(current_probe.__file__)))
        for path, expected in priority["source_sha256"].items():
            self.assertEqual(hash_file(Path(path)), expected)
        binary = Path(priority["processes"]["manager"]["argv"][2])
        self.assertEqual(hash_file(binary), priority["omp_binary_sha256"])
        self.assertEqual(priority["omp_binary_sha256"], self.rejudge["binary_sha256"])

        posts = [(role, post) for role, provider in priority["providers"].items()
                 for post in provider["posts"]]
        self.assertEqual(len(posts), 4)
        children = []
        for role, post in posts:
            path = Path(post["raw_body_path"])
            raw = path.read_bytes()
            self.assertEqual((len(raw), sha256(raw).hexdigest()), (post["bytes"], post["sha256"]))
            payload = json.loads(raw)
            self.assertEqual(payload["model"], "scripted")
            self.assertEqual({tool["function"]["name"] for tool in payload["tools"]},
                             set(post["tools"]))
            self.assertEqual([message["role"] for message in payload["messages"]], post["roles"])
            if post["stage"].startswith("child"):
                self.assertEqual((role, post["tools"]), ("manager", ["db_summary_save"]))
                self.assertLessEqual(len(raw), 32768)
                children.append(post)
            else:
                self.assertGreater(len(raw), 32768)
                self.assertIsNotNone(post["notice_id"])
        self.assertEqual(len(children), 2)
        for post in self.stages["budget"]["posts"]:
            raw = Path(post["raw_body_path"]).read_bytes()
            self.assertEqual((len(raw), sha256(raw).hexdigest()), (post["bytes"], post["sha256"]))
            self.assertLessEqual(len(raw), 32768)

        request = load(self.private / "request.json")
        typed_path = self.private / "typed-result.json"
        typed = load(typed_path)
        self.assertEqual(typed["requestId"], request["requestId"])
        self.assertEqual(typed["inputKey"], request["inputKey"])
        self.assertEqual(typed["skillDigest"], request["skillDigest"])
        self.assertEqual(typed["sources"], request["sources"])
        self.assertEqual((typed_path.stat().st_size, hash_file(typed_path)),
                         (priority["typed_contract"]["bytes"], priority["typed_contract"]["sha256"]))
        with sqlite3.connect(f"file:{self.private / 'summary.sqlite3'}?mode=ro", uri=True) as db:
            rows = db.execute("SELECT id, summary FROM summaries ORDER BY id").fetchall()
        self.assertEqual(rows, [(1, typed["facts"][0])])
        self.assertEqual(priority["db_rows"], [list(row) for row in rows])

    def test_actual_timing_sources_and_limited_overlap(self) -> None:
        priority = self.priority
        timing = priority["timing"]
        self.assertEqual(timing["clock"], "time.monotonic_ns")
        hold, release = timing["child_hold_started_ns"], timing["child_release_signal_ns"]
        self.assertEqual((hold, release), (priority["child_hold_started_ns"],
                                           priority["child_release_signal_ns"]))
        self.assertLess(hold, release)
        self.assertLessEqual(hold, timing["draft_input_before_write_ns"])
        self.assertLess(timing["draft_input_before_write_ns"], timing["draft_input_after_write_ns"])
        self.assertLess(timing["draft_input_after_write_ns"], timing["draft_state_observed_ns"])
        self.assertFalse(priority["manager_draft_state"]["editorEmpty"])
        self.assertEqual(priority["manager_draft_ack"]["status"], "deferred")
        self.assertLess(timing["end_deferred_enqueue_ns"], timing["end_deferred_ack_ns"])
        self.assertLess(timing["end_deferred_ack_ns"], timing["due_clock_before_injection_ns"])
        self.assertLess(timing["due_clock_before_injection_ns"], timing["due_clock_after_injection_ns"])
        self.assertLess(timing["due_clock_after_injection_ns"], timing["draft_clear_before_write_ns"])
        self.assertLess(timing["draft_clear_before_write_ns"], timing["clear_state_observed_ns"])
        self.assertTrue(priority["manager_clear_state"]["editorEmpty"])
        self.assertEqual(priority["manager_accept_ack"]["status"], "api_accepted")

        wire = []
        for line in (self.private / "bridge-wire.jsonl").read_text().splitlines():
            record = json.loads(line)
            payload = base64.b64decode(record["raw_b64"], validate=True)
            self.assertEqual(sha256(payload).hexdigest(), record["sha256"])
            wire.append({**record, "frame": json.loads(payload)})
        self.assertEqual(len(wire), 41)
        deferred = [row for row in priority["bridge"]["requests"]
                    if row["role"] == "manager" and
                    row["frame"].get("notice", {}).get("type") == "worker_terminal_done" and
                    row["ack"]["status"] == "deferred"]
        self.assertEqual(len(deferred), 1)
        deferred_id = deferred[0]["ack"]["requestId"]
        deferred_frame = next(row for row in wire if row["direction"] == "host_to_omp"
                              and row["frame"].get("requestId") == deferred_id)
        deferred_ack = next(row for row in wire if row["direction"] == "omp_to_host"
                            and row["frame"].get("requestId") == deferred_id)
        self.assertEqual(deferred_ack["frame"]["status"], "deferred")
        self.assertLess(timing["draft_state_observed_ns"], deferred_frame["monotonic_ns"])
        self.assertLess(deferred_frame["monotonic_ns"], deferred_ack["monotonic_ns"])
        self.assertLess(deferred_ack["monotonic_ns"], timing["draft_clear_before_write_ns"])
        parent_ids = {role: priority["bridge"]["hello"][role]["ompSessionId"]
                      for role in ("manager", "worker")}
        child = next(row["childSessionId"] for row in priority["budget_trace"]
                     if row["event"] == "startup")
        self.assertEqual(len(set([*parent_ids.values(), child])), 3)
        samples = timing["state_samples"]
        self.assertEqual(len(samples), 5)
        for sample in samples:
            self.assertEqual(sample["state"]["sessionId"], parent_ids[sample["role"]])
        draft_sample = next(sample for sample in samples if sample["role"] == "manager"
                            and not sample["state"]["editorEmpty"])
        self.assertLess(timing["draft_input_after_write_ns"], draft_sample["observed_ns"])
        self.assertLess(draft_sample["observed_ns"], timing["end_deferred_enqueue_ns"])

        for role, kind, enqueue in (("worker", "terminal_check",
                                    priority["scheduler"]["notice_dispatches"][0]["enqueue_ns"]),
                                   ("manager", "worker_terminal_done",
                                    timing["end_accepted_enqueue_ns"])):
            request = next(row for row in priority["bridge"]["requests"]
                           if row["role"] == role and row["frame"].get("notice", {}).get("type") == kind
                           and row["ack"]["status"] == "api_accepted")
            notice = request["frame"]["notice"]["notice_id"]
            raw_request = next(row for row in wire if row["direction"] == "host_to_omp"
                               and row["frame"].get("notice", {}).get("notice_id") == notice
                               and row["frame"].get("requestId") == request["ack"]["requestId"])
            raw_ack = next(row for row in wire if row["direction"] == "omp_to_host"
                           and row["frame"].get("requestId") == raw_request["frame"]["requestId"]
                           and row["frame"].get("status") == "api_accepted")
            post = next(post for post in priority["providers"][role]["posts"]
                        if post["stage"] == "parent" and post["notice_id"] == notice)
            start = next(event["observed_ns"] for event in timing["parent_turn_events"]
                         if event["role"] == role and event["name"] == "agent_start")
            end = next(event["observed_ns"] for event in timing["parent_turn_events"]
                       if event["role"] == role and event["name"] == "agent_end")
            self.assertLessEqual(enqueue, raw_request["monotonic_ns"])
            self.assertLessEqual(request["sent_ns"], raw_request["monotonic_ns"])
            self.assertLessEqual(raw_request["monotonic_ns"], raw_ack["monotonic_ns"])
            self.assertLessEqual(raw_ack["monotonic_ns"], request["ack_observed_ns"])
            self.assertLess(request["ack_observed_ns"], start)
            self.assertLess(start, post["received_ns"])
            self.assertLess(post["received_ns"], release)
            self.assertLess(start, end)
            self.assertGreater(min(end, release) - max(start, hold), 0)
        current = load(Path(self.rejudge["observations_path"]))
        priority_row = next(row for row in current if row["id"] == "Q01-priority")
        self.assertIsNone(priority_row["facts"]["max_block_recorded"])
        self.assertIsNone(priority_row["facts"]["measured_block_ms"])
        self.assertEqual(self.rejudge["reconstruction_verdicts"]["Q01-priority"]["result"], "passed")
        self.assertEqual(self.rejudge["verdicts"]["Q01-priority"]["result"], "inconclusive")
        self.assertTrue(self.rejudge["source_issues"])
        observed = priority_row["facts"]["nonshared_priority_measurement"]
        self.assertEqual(observed["session_ids"], {**parent_ids, "child": child})
        self.assertEqual(observed["child_hold_interval_ns"], [hold, release])
        self.assertEqual(observed["internal_lock_wait"], "unavailable")
        self.assertEqual(len(observed["notice_windows"]), 2)
        for window in observed["notice_windows"]:
            self.assertEqual(window["enqueue_to_start_ms"],
                             (window["agent_start_ns"] - window["enqueue_ns"]) / 1000000)
            self.assertEqual(window["enqueue_to_post_ms"],
                             (window["provider_post_ns"] - window["enqueue_ns"]) / 1000000)
        self.assertEqual(observed["max_enqueue_to_start_ms"],
                         max(w["enqueue_to_start_ms"] for w in observed["notice_windows"]))
        self.assertEqual(observed["max_enqueue_to_post_ms"],
                         max(w["enqueue_to_post_ms"] for w in observed["notice_windows"]))

    def test_retained_notice_ack_request_id_must_match_wire(self) -> None:
        from q01_recheck import judge_envelope
        stages = copy.deepcopy(self.stages)
        request = next(row for row in stages["priority_runtime"]["bridge"]["requests"]
                       if row.get("role") == "worker" and row.get("ack", {}).get("status") == "api_accepted")
        request["ack"]["requestId"] = "unrelated-ack-request"
        outcome = self.compose(stages)
        self.assertNotEqual(judge_envelope(outcome, adapter="probe-local")["Q01-priority"]["result"], "passed")

    def test_missing_corrupt_or_unattributed_wire_cannot_pass(self) -> None:
        from q01_recheck import judge_envelope
        original = [json.loads(line) for line in (self.private / "bridge-wire.jsonl").read_text().splitlines()]
        worker_request = next(row for row in self.priority["bridge"]["requests"]
                              if row.get("role") == "worker" and row.get("ack", {}).get("status") == "api_accepted")
        request_id = worker_request["ack"]["requestId"]

        def frame(row):
            return json.loads(base64.b64decode(row["raw_b64"], validate=True))

        sent_index = next(i for i, row in enumerate(original) if row["direction"] == "host_to_omp"
                          and frame(row).get("requestId") == request_id)
        ack_index = next(i for i, row in enumerate(original) if row["direction"] == "omp_to_host"
                         and frame(row).get("requestId") == request_id)
        cases = (
            ("missing_file", None),
            ("sha", lambda rows: rows[ack_index].update(sha256="0" * 64)),
            ("missing_request", lambda rows: rows.pop(sent_index)),
            ("wrong_send_connection", lambda rows: rows[sent_index].update(connection="manager")),
            ("wrong_ack_connection", lambda rows: rows[ack_index].update(connection="unknown-connection")),
            ("reversed_wire_time", lambda rows: rows[ack_index].update(monotonic_ns=rows[sent_index]["monotonic_ns"] - 1)),
            ("malformed_time", lambda rows: rows[ack_index].update(monotonic_ns="unknown")),
        )
        for label, mutate in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory(prefix="wire-custody-", dir=self.output) as name:
                private = Path(name) / "private"
                private.mkdir()
                if mutate is not None:
                    rows = copy.deepcopy(original)
                    mutate(rows)
                    (private / "bridge-wire.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
                saved = self.paths["priority_runtime"]
                try:
                    self.paths["priority_runtime"] = Path(name) / "runtime-evidence.json"
                    outcome = self.compose(copy.deepcopy(self.stages))
                finally:
                    self.paths["priority_runtime"] = saved
                self.assertTrue(any(issue.startswith("priority_runtime:wire_") for issue in outcome["unknowns"]))
                self.assertNotEqual(judge_envelope(outcome, adapter="probe-local")["Q01-priority"]["result"], "passed")

    def test_reviewed_huge_negative_enqueue_rejects_without_overflow(self) -> None:
        from q01_recheck import judge_envelope
        stages = copy.deepcopy(self.stages)
        stages["priority_runtime"]["scheduler"]["notice_dispatches"][0]["enqueue_ns"] = -(10 ** 400)
        outcome = self.compose(stages)
        self.assertNotEqual(judge_envelope(outcome, adapter="probe-local")["Q01-priority"]["result"], "passed")

    def test_reviewed_non_notice_with_list_is_total_and_missing_required_notice_rejects(self) -> None:
        from q01_recheck import judge_envelope
        for replacement in (True, False):
            stages = copy.deepcopy(self.stages)
            requests = stages["priority_runtime"]["bridge"]["requests"]
            if replacement:
                required = next(row for row in requests if row.get("role") == "worker"
                                and row.get("ack", {}).get("status") == "api_accepted")
                required["frame"] = {"kind": "other", "notice": []}
            else:
                requests.append({"role": "worker", "frame": {"kind": "other", "notice": []}, "ack": {}})
            with self.subTest(replaces_required_notice=replacement):
                outcome = self.compose(stages)
                verdicts = judge_envelope(outcome, adapter="probe-local")
                self.assertEqual(len(verdicts), 8)
                if replacement:
                    self.assertNotEqual(verdicts["Q01-priority"]["result"], "passed")
                elif verdicts["Q01-priority"]["result"] == "passed":
                    row = next(row for row in outcome["observations"] if row["id"] == "Q01-priority")
                    self.assertEqual(len(row["facts"]["nonshared_priority_measurement"]["notice_windows"]), 2)

    def test_reviewed_malformed_raw_body_path_is_unknown_not_exception(self) -> None:
        from q01_recheck import judge_envelope
        stages = copy.deepcopy(self.stages)
        post = next(row for row in stages["priority_runtime"]["providers"]["worker"]["posts"]
                    if row["stage"] == "parent")
        post["raw_body_path"] = []
        outcome = self.compose(stages)
        self.assertTrue(outcome["unknowns"])
        self.assertNotEqual(judge_envelope(outcome, adapter="probe-local")["Q01-priority"]["result"], "passed")

    def test_reviewed_manager_raw_body_cannot_prove_worker_notice_delivery(self) -> None:
        from q01_recheck import judge_envelope
        stages = copy.deepcopy(self.stages)
        parent = {role: next(post for post in stages["priority_runtime"]["providers"][role]["posts"]
                             if post["stage"] == "parent") for role in ("manager", "worker")}
        notices = {}
        for role, post in parent.items():
            raw = Path(post["raw_body_path"]).read_bytes()
            self.assertEqual((len(raw), sha256(raw).hexdigest()), (post["bytes"], post["sha256"]))
            body = json.loads(raw)
            actual_notices = []
            for message in body["messages"]:
                parts = message.get("content", [])
                parts = [{"text": parts}] if isinstance(parts, str) else parts
                for part in parts:
                    text = part.get("text") if isinstance(part, dict) else None
                    if not isinstance(text, str):
                        continue
                    try:
                        content = json.loads(text)
                    except ValueError:
                        continue
                    if isinstance(content, dict) and "workbench_notice" in content:
                        actual_notices.append(content)
            self.assertEqual(len(actual_notices), 1)
            notices[role] = actual_notices[0]
            self.assertEqual(notices[role]["notice_id"], post["notice_id"])
        self.assertEqual(notices["manager"]["workbench_notice"], "worker_terminal_done")
        self.assertEqual(notices["worker"]["workbench_notice"], "terminal_check")
        self.assertNotEqual(notices["manager"]["notice_id"], notices["worker"]["notice_id"])
        preserved = {field: parent["worker"][field] for field in ("notice_id", "received_ns", "stage")}
        for field in ("raw_body_path", "sha256", "bytes"):
            parent["worker"][field] = parent["manager"][field]
        self.assertEqual({field: parent["worker"][field] for field in preserved}, preserved)
        outcome = self.compose(stages)
        self.assertNotEqual(judge_envelope(outcome, adapter="probe-local")["Q01-priority"]["result"], "passed")

    def test_malformed_retained_timestamp_fails_closed_without_crashing(self) -> None:
        from q01_recheck import judge_envelope
        for value in ("not-a-monotonic-time", None, True, []):
            stages = copy.deepcopy(self.stages)
            post = next(p for p in stages["priority_runtime"]["providers"]["worker"]["posts"]
                        if p["stage"] == "parent")
            post["received_ns"] = value
            with self.subTest(value=value):
                outcome = self.compose(stages)
                self.assertNotEqual(judge_envelope(outcome, adapter="probe-local")["Q01-priority"]["result"], "passed")

    def test_malformed_retained_nested_collections_fail_closed_without_crashing(self) -> None:
        from q01_recheck import judge_envelope
        cases = (
            ("hello", lambda p: p["bridge"].update(hello=[])),
            ("requests", lambda p: p["bridge"].update(requests={})),
            ("turn_events", lambda p: p["timing"].update(parent_turn_events=[None])),
            ("provider_role", lambda p: p["providers"].update(worker=[])),
            ("dispatches", lambda p: p["scheduler"].update(notice_dispatches=[None])),
        )
        for label, mutate in cases:
            stages = copy.deepcopy(self.stages)
            mutate(stages["priority_runtime"])
            with self.subTest(case=label):
                outcome = self.compose(stages)
                self.assertNotEqual(judge_envelope(outcome, adapter="probe-local")["Q01-priority"]["result"], "passed")

    def test_oversize_complete_body_and_missing_timing_cannot_pass(self) -> None:
        baseline = self.compose(copy.deepcopy(self.stages))
        facts = {row["id"]: row["facts"] for row in baseline["observations"]}
        self.assertTrue(facts["Q01-context"]["body_within_32k"])
        self.assertIsNone(facts["Q01-priority"]["max_block_recorded"])
        with tempfile.TemporaryDirectory(prefix="wire-copy-", dir=self.output) as folder:
            stages = copy.deepcopy(self.stages)
            post = stages["budget"]["posts"][0]
            original = json.loads(Path(post["raw_body_path"]).read_bytes())
            original["messages"][0]["content"] += "x" * 40000
            raw = json.dumps(original, ensure_ascii=False, separators=(",", ":")).encode()
            self.assertGreater(len(raw), 32768)
            self.assertLess(stages["budget"]["trace"][0]["promptBytes"], 32768)
            copied = Path(folder) / "oversize.body"
            copied.write_bytes(raw)
            post.update(raw_body_path=str(copied), sha256=sha256(raw).hexdigest(), bytes=len(raw))
            measured = self.compose(stages)
            fact = next(row["facts"] for row in measured["observations"]
                        if row["id"] == "Q01-context")
            self.assertFalse(fact["body_within_32k"])
            stages = copy.deepcopy(self.stages)
            stages["budget"]["posts"][0]["sha256"] = "0" * 64
            damaged = self.compose(stages)
            fact = next(row["facts"] for row in damaged["observations"]
                        if row["id"] == "Q01-context")
            self.assertFalse(fact["body_within_32k"])
        stages = copy.deepcopy(self.stages)
        stages["priority_runtime"]["timing"]["parent_turn_events"] = [
            event for event in stages["priority_runtime"]["timing"]["parent_turn_events"]
            if not (event["role"] == "worker" and event["name"] == "agent_start")]
        damaged = self.compose(stages)
        priority_row = next(row for row in damaged["observations"] if row["id"] == "Q01-priority")
        self.assertIsNone(priority_row["facts"]["max_block_recorded"])
        from q01_recheck import judge_envelope
        self.assertNotEqual(judge_envelope(damaged, adapter="probe-local")["Q01-priority"]["result"], "passed")
        self.assertEqual(len(damaged["boundaries"]["priority_notice_windows"]), 1)


if __name__ == "__main__":
    unittest.main()
