"""Independent checks of retained three-process recovery evidence.

Only Q01_SDK_RECOVERY_RESULT is read. OMP and model calls are never launched.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from workbench.tasks.repository import InvalidTransitionError, TaskRepository  # noqa: E402


SUMMARY = "Literal command and SQL: touch /tmp/NO_EXECUTE; DROP TABLE summaries; --"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def rows(db_path: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        return db.execute(sql).fetchall()


def events(trace: list[dict], kind: str, case: str | None = None) -> list[dict]:
    return [row for row in trace if row.get("event") == kind and
            (case is None or row.get("case") == case)]


class RecoveryRuntimeIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_RECOVERY_RESULT")
        output = os.environ.get("Q01_SDK_RECOVERY_TEST_DIR")
        if not supplied or not output:
            raise AssertionError("explicit actual recovery result and private test dir required")
        cls.result = json.loads(Path(supplied).resolve(strict=True).read_text())
        cls.evidence_path = Path(cls.result["evidence_path"]).resolve(strict=True)
        cls.evidence = json.loads(cls.evidence_path.read_text())
        cls.output = Path(output).resolve(strict=True)
        cls.private = cls.evidence_path.parent / "private"
        cls.backend_db = cls.private / "backend.sqlite3"

    def one(self, trace: list[dict], kind: str, case: str | None = None) -> dict:
        found = events(trace, kind, case)
        self.assertEqual(len(found), 1, (kind, case))
        return found[0]

    def test_source_artifacts_three_owned_omp_and_five_raw_db_only_posts(self) -> None:
        e = self.evidence
        for name, item in self.result["artifact_manifest"].items():
            path = ROOT / name
            self.assertEqual(path.stat().st_size, item["bytes"], name)
            self.assertEqual(digest(path), item["sha256"], name)
        for name, expected in e["source_sha256"].items():
            self.assertEqual(digest(Path(name)), expected, name)
        for name, expected in self.result["source_sha256"].items():
            self.assertEqual(digest(Path(name)), expected, name)
        self.assertEqual(digest(Path(self.result["phase_argv"]["live_late"][0])),
                         self.result["omp_binary_sha256"])
        self.assertEqual(set(e["phases"]), {"live_late", "killed", "fresh"})
        self.assertEqual(len({e["phases"][name]["process"]["pid"] for name in e["phases"]}), 3)
        for name, phase in e["phases"].items():
            proc = phase["process"]
            self.assertEqual(proc["pid"], proc["pgid"])
            self.assertTrue(proc["pump_stopped"])
            self.assertEqual(proc["remaining_owned"], [])
            self.assertEqual(phase["omp_binary_sha256"], self.result["omp_binary_sha256"])
            self.assertEqual(phase["omp_version"], self.result["omp_version"])
            self.assertEqual(phase["argv"], self.result["phase_argv"][name])
            self.assertEqual({row["pid"] for row in phase["contract_trace"] +
                              phase["recovery_trace"]}, {proc["pid"]})
            for filename, key in (("contract-trace.jsonl", "contract_trace"),
                                  ("recovery-trace.jsonl", "recovery_trace")):
                actual = [json.loads(line) for line in
                          (self.private / name / filename).read_text().splitlines()]
                self.assertEqual(actual, phase[key])
            private_requests = json.loads((self.private / name / "requests.json").read_text())
            self.assertEqual(private_requests["positive"], e["requests"][name])
            self.assertEqual(private_requests["paused"], e["requests"]["paused"])
            self.assertEqual(private_requests["load_failed"], e["requests"]["load_failed"])
            roles = json.loads((self.private / name / "role-inputs.json").read_text())
            self.assertEqual(roles["positive"], {"role": "Manager", "agentName": "task"})
            self.assertEqual([r["admitted"] for r in events(phase["contract_trace"],
                                                               "role_admission", "positive")], [True])
            if name != "killed":
                self.assertEqual({r["case"] for r in phase["contract_trace"]
                                  if r["event"] == "role_rejected"},
                                 {"saved_then_bash", "worker_role"})
            with self.assertRaises(ProcessLookupError):
                os.killpg(proc["pgid"], 0)
        self.assertTrue(e["provider_stopped"])
        self.assertIsNone(e["provider_violation"])
        self.assertEqual([p["phase"] for p in e["provider_posts"]],
                         ["live_late", "live_late", "killed", "fresh", "fresh"])
        self.assertEqual([p["tool_message_count"] for p in e["provider_posts"]],
                         [0, 1, 0, 0, 1])
        systems = []
        for post in e["provider_posts"]:
            raw_path = Path(post["raw_body_path"])
            self.assertEqual(raw_path.parent, self.private / post["phase"])
            raw = raw_path.read_bytes()
            self.assertEqual(len(raw), post["bytes"])
            self.assertEqual(sha256(raw).hexdigest(), post["sha256"])
            body = json.loads(raw)
            self.assertEqual(post["path"], "/v1/chat/completions")
            self.assertEqual(body["model"], "scripted")
            self.assertEqual(body["max_completion_tokens"], 8192)
            self.assertEqual([tool["function"]["name"] for tool in body["tools"]],
                             ["db_summary_save"])
            self.assertEqual(sum(m["role"] == "tool" for m in body["messages"]),
                             post["tool_message_count"])
            systems.append(body["messages"][0]["content"])
            self.assertEqual(digest(Path(post["response_body_path"])), post["response_sha256"])
        self.assertEqual(len(set(systems)), 1)
        self.assertEqual(rows(self.private / "summary.sqlite3",
                              "SELECT id,summary FROM summaries ORDER BY id"),
                         [(1, SUMMARY), (2, SUMMARY)])

    def test_pause_load_failure_zero_send_then_same_physical_not_sent_retry(self) -> None:
        e = self.evidence
        req = e["requests"]
        physical = (self.private / "physical-source.txt").read_bytes()
        self.assertEqual(physical, SUMMARY.encode() * 3)
        self.assertEqual(len(physical), e["source_bytes"])
        for name, start, end in (("paused", 0, 72), ("load_failed", 0, 72),
                                 ("live_late", 0, 72), ("killed", 72, 144),
                                 ("fresh", 144, 216)):
            item = req[name]
            span = item["sources"][0]
            self.assertEqual((span["start"], span["end"]), (start, end))
            self.assertEqual(span["digest"], sha256(physical[start:end]).hexdigest())
            self.assertEqual(span["source"], e["physical_source"])
            self.assertEqual(item["skillDigest"], digest(ROOT / "tests/backend/q01_sdk_private_probe/summary-skill.txt"))
        self.assertEqual(len({req[k]["requestId"] for k in req}), 5)
        self.assertEqual({req[k]["inputKey"] for k in ("paused", "load_failed", "live_late")},
                         {req["live_late"]["inputKey"]})
        self.assertEqual({req[k]["subject"]["id"] for k in req},
                         {req["live_late"]["subject"]["id"]})
        gate = e["phases"]["live_late"]["recovery_trace"]
        self.assertEqual([(r["attempt"], r["state"], r["admitted"]) for r in
                          events(gate, "admission_attempt")],
                         [(1, "paused", False), (2, "load_failed", False), (3, "run", True)])
        self.assertEqual([(r["attempt"], r["reason"], r["childCallbackCalls"])
                          for r in events(gate, "deferred")],
                         [(1, "paused", 0), (2, "load_failed", 0)])
        callback = self.one(gate, "captured_callback_invoked")
        self.assertEqual(callback["attempt"], 3)
        self.assertEqual(len(events(gate, "captured_callback_invoked")), 1)
        self.assertEqual({p["request_id"] for p in e["provider_posts"]
                          if p["phase"] == "live_late"}, {req["live_late"]["requestId"]})
        for name in ("paused", "load_failed"):
            self.assertEqual(e["journal"][name + "_gate"]["postCount"], 0)
            self.assertEqual(e["journal"][name + "_gate"]["contractEvents"], 0)
        states = rows(self.backend_db,
                      "SELECT request_id,state FROM summary_request_events_v1 ORDER BY event_id")
        for name in ("paused", "load_failed"):
            self.assertEqual([s for rid, s in states if rid == req[name]["requestId"]],
                             ["prepared", "not_sent"])
        self.assertEqual([s for rid, s in states if rid == req["live_late"]["requestId"]],
                         ["prepared", "sending", "unknown", "stored"])

    def test_live_unknown_reopen_actual_typed_late_store_duplicate_and_mismatches(self) -> None:
        e = self.evidence
        req = e["requests"]["live_late"]
        phase = e["phases"]["live_late"]
        trace = phase["contract_trace"]
        posts = [p for p in e["provider_posts"] if p["phase"] == "live_late"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["request_id"], req["requestId"])
        self.assertLess(posts[0]["received_ns"], posts[0]["hold_started_ns"])
        self.assertLess(posts[0]["hold_started_ns"], posts[0]["hold_released_ns"])
        self.assertLess(posts[0]["hold_released_ns"], posts[1]["received_ns"])
        self.assertEqual(e["journal"]["reopened_live_unknown"],
                         {"request": req, "state": "unknown", "receipt": None})
        self.assertIn(e["journal"]["reopened_live_unknown"],
                      e["journal"]["unresolved_after_live_reopen"])
        typed_path = self.private / "live_late/typed-positive.json"
        typed = json.loads(typed_path.read_text())
        for field in ("requestId", "inputKey", "skillDigest", "sources"):
            self.assertEqual(typed[field], req[field])
        saved = self.one(trace, "saved", "positive")
        self.assertEqual(saved["requestId"], req["requestId"])
        self.assertEqual(saved["resultSha"], digest(typed_path))
        self.assertEqual(self.one(trace, "result_accepted", "positive")["resultSha"],
                         digest(typed_path))
        self.assertEqual(self.one(trace, "writer_invoked", "positive")["argv"][0],
                         str(self.private / "fixed_writer.py"))
        self.assertEqual(self.one(trace, "execute_received", "positive")["params"],
                         {"summary": SUMMARY})
        receipts = rows(self.backend_db,
                        "SELECT request_id,result_digest FROM summary_result_receipts_v1 ORDER BY request_id")
        drops = rows(self.backend_db,
                     "SELECT request_id,reason,result_digest FROM summary_result_drops_v1")
        self.assertEqual(len(receipts), 2)
        typed_digest = sha256(json.dumps(typed, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()
        self.assertEqual([r for r in receipts if r[0] == req["requestId"]],
                         [(req["requestId"], typed_digest)])
        self.assertEqual(drops, [(req["requestId"], "duplicate", typed_digest)])
        # Apply the three contract mismatches to a private copy of the actual backend DB.
        with tempfile.TemporaryDirectory(dir=self.output, prefix="typed-negative-") as tmp:
            copy_path = Path(tmp) / "backend.sqlite3"
            shutil.copyfile(self.backend_db, copy_path)
            receipt_sql = "SELECT request_id,result_digest FROM summary_result_receipts_v1 ORDER BY request_id"
            drop_sql = "SELECT request_id,reason,result_digest FROM summary_result_drops_v1 ORDER BY drop_id"
            before = (rows(copy_path, receipt_sql), rows(copy_path, drop_sql))
            repo = TaskRepository(copy_path)
            try:
                wrong_id = dict(typed, requestId=str(uuid4()))
                wrong_revision = deepcopy(typed)
                wrong_revision["sources"][0]["source"]["revision"] += 1
                wrong_range = deepcopy(typed)
                wrong_range["sources"][0]["end"] += 1
                for changed in (wrong_id, wrong_revision, wrong_range):
                    with self.assertRaises((InvalidTransitionError, ValueError)):
                        repo.record_summary_result_receipt(changed, str(uuid4()))
            finally:
                repo.close()
            self.assertEqual((rows(copy_path, receipt_sql), rows(copy_path, drop_sql)), before)

    def test_killed_unknown_blocks_overlap_and_fresh_nonoverlap_stores(self) -> None:
        e = self.evidence
        killed = e["requests"]["killed"]
        fresh = e["requests"]["fresh"]
        killed_phase = e["phases"]["killed"]
        self.assertEqual(killed_phase["process"]["returncode"], -9)
        self.assertEqual(len([p for p in e["provider_posts"] if p["phase"] == "killed"]), 1)
        self.assertEqual(events(killed_phase["contract_trace"], "writer_invoked", "positive"), [])
        self.assertEqual(events(killed_phase["contract_trace"], "result_accepted", "positive"), [])
        self.assertFalse((self.private / "killed/typed-positive.json").exists())
        self.assertEqual(e["journal"]["reopened_killed_unknown"],
                         {"request": killed, "state": "unknown", "receipt": None})
        self.assertIn(e["journal"]["reopened_killed_unknown"],
                      e["journal"]["unresolved_after_killed_reopen"])
        controls = e["journal"]["replay_reservation_controls"]
        exact = controls["exact_new_uuid"]
        overlap = controls["overlap_new_revision"]
        self.assertTrue(exact["rejected"] and overlap["rejected"])
        self.assertNotEqual(exact["requestId"], killed["requestId"])
        self.assertEqual(exact["inputKey"], killed["inputKey"])
        self.assertEqual(exact["sources"], killed["sources"])
        self.assertNotEqual(overlap["requestId"], killed["requestId"])
        self.assertEqual(overlap["sources"][0]["source"]["revision"], 2)
        self.assertLess(overlap["sources"][0]["start"], killed["sources"][0]["end"])
        self.assertGreater(overlap["sources"][0]["end"], killed["sources"][0]["start"])
        self.assertEqual((fresh["sources"][0]["start"], fresh["sources"][0]["end"]),
                         (144, 216))
        self.assertEqual(killed["sources"][0]["end"], fresh["sources"][0]["start"])
        typed_path = self.private / "fresh/typed-positive.json"
        typed = json.loads(typed_path.read_text())
        for field in ("requestId", "inputKey", "skillDigest", "sources"):
            self.assertEqual(typed[field], fresh[field])
        trace = e["phases"]["fresh"]["contract_trace"]
        self.assertEqual(self.one(trace, "saved", "positive")["resultSha"], digest(typed_path))
        self.assertEqual(len([p for p in e["provider_posts"] if p["phase"] == "fresh"]), 2)
        states = rows(self.backend_db,
                      "SELECT request_id,state FROM summary_request_events_v1 ORDER BY event_id")
        self.assertEqual([s for rid, s in states if rid == killed["requestId"]],
                         ["prepared", "sending", "unknown"])
        self.assertEqual([s for rid, s in states if rid == fresh["requestId"]],
                         ["prepared", "sending", "stored"])
        present = {row[0] for row in rows(self.backend_db,
                                           "SELECT request_id FROM summary_requests_v1")}
        self.assertNotIn(exact["requestId"], present)
        self.assertNotIn(overlap["requestId"], present)
        receipts = {row[0] for row in rows(self.backend_db,
                                            "SELECT request_id FROM summary_result_receipts_v1")}
        self.assertEqual(receipts, {e["requests"]["live_late"]["requestId"], fresh["requestId"]})
        fresh_digest = sha256(json.dumps(typed, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(rows(self.backend_db,
                              "SELECT result_digest FROM summary_result_receipts_v1 WHERE request_id='" +
                              fresh["requestId"] + "'"), [(fresh_digest,)])


if __name__ == "__main__":
    unittest.main()
