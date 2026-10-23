"""Independent custody checks for retained LG-D24 OMP origin evidence.

The confirmed terminal bytes are a synthetic fixture. Provider POST, child tool
result and backend receipt are retained actual installed-OMP observations.
"""

from __future__ import annotations

import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import unittest

from workbench.contracts.logs_v1 import summary_input_key


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


class ActualOriginEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_ORIGIN_RESULT")
        if not supplied:
            raise unittest.SkipTest("Q01_SDK_ORIGIN_RESULT retained actual evidence required; skipped tests are not a gate pass")
        cls.result_path = Path(supplied).resolve(strict=True)
        cls.origin = json.loads(cls.result_path.read_text())
        cls.runtime_path = Path(cls.origin["actual_runtime"]).resolve(strict=True)
        cls.runtime = json.loads(cls.runtime_path.read_text())
        cls.private = cls.runtime_path.parent / "private"
        cls.requests = json.loads((cls.private / "requests.json").read_text())
        cls.typed_path = cls.private / "typed-positive.json"
        cls.typed = json.loads(cls.typed_path.read_text())

    def test_source_raw_post_and_child_trace_custody(self) -> None:
        origin, run = self.origin, self.runtime
        for name, expected in {**origin["source_sha256"], **run["source_sha"]}.items():
            self.assertEqual(digest(Path(name)), expected, name)
        self.assertEqual(origin["omp_binary_sha"], run["omp_binary_sha"])
        self.assertEqual(digest(Path(origin["argv"][0])), run["omp_binary_sha"])
        self.assertEqual(run["requests"], self.requests)
        posts = run["provider_posts"]
        self.assertEqual(len(posts), 4)
        self.assertEqual([p["case"] for p in posts],
                         ["positive", "positive", "saved_then_bash", "saved_then_bash"])
        for index, post in enumerate(posts, 1):
            raw = self.private / f"raw-post-{index:02d}.json"
            self.assertEqual(digest(raw), post["body_sha"])
            wire = json.loads(raw.read_text())
            self.assertEqual(wire["model"], post["model"])
            self.assertEqual([tool["function"]["name"] for tool in wire["tools"]],
                             ["db_summary_save"])
            self.assertEqual([m["role"] for m in wire["messages"]], post["roles"])
            self.assertEqual(post["path"], "/v1/chat/completions")
        outbound = run["provider_outbound"]
        self.assertEqual([row["call"]["function"]["name"] if row["call"] else None
                          for row in outbound],
                         ["db_summary_save", None, "db_summary_save", "bash"])
        trace = run["trace"]
        for case in ("positive", "saved_then_bash"):
            events = [row["event"] for row in trace if row.get("case") == case]
            self.assertLess(events.index("startup_admitted"), events.index("saved"))
            self.assertIn("disposed", events)
        accepted = [row for row in trace if row["event"] == "result_accepted"]
        self.assertEqual([row["case"] for row in accepted], ["positive"])
        self.assertEqual(accepted[0]["resultSha"], digest(self.typed_path))
        self.assertEqual(run["sentinels"],
                         {"command-sentinel": "ORIGINAL", "file-sentinel": "ORIGINAL"})
        self.assertTrue(run["process"]["pump_stopped"])
        self.assertEqual(run["process"]["remaining_owned"], [])
        with self.assertRaises(ProcessLookupError):
            os.kill(run["process"]["pid"], 0)

    def test_durable_start_source_request_actual_typed_and_repo_receipt_match(self) -> None:
        origin, run = self.origin, self.runtime
        request = self.requests["positive"]
        positive = origin["cases"]["positive"]
        span = request["sources"][0]
        self.assertEqual(request["subject"], {"schemaVersion": 1, "kind": "command",
                                              "id": positive["command_id"]})
        self.assertIsNone(request["revision"])
        self.assertEqual(request["sourceGaps"], [])
        self.assertEqual(request["sources"], [positive["selected_confirmed_span"]])
        self.assertEqual(span["source"]["commandId"], positive["command_id"])
        self.assertEqual(request["inputKey"], summary_input_key(request["subject"], None,
                            request["sources"], [], request["skillDigest"]))
        self.assertEqual(span["end"] - span["start"],
                         len(self.typed["facts"][0].encode("utf-8")))
        self.assertEqual(span["digest"], sha256(self.typed["facts"][0].encode()).hexdigest())
        important_db = self.private / "important" / f"{origin['important_db_uuid']}.sqlite3"
        with sqlite3.connect(f"file:{important_db}?mode=ro", uri=True) as db:
            start = db.execute("""SELECT source_id FROM subject_events_v1 WHERE event_key='terminal:1'
                AND subject_kind='command' AND subject_id=? AND event_kind='terminal_started'""",
                (positive["command_id"],)).fetchone()
            source_rows = db.execute("""SELECT sources_json FROM entries
                WHERE subject_kind='command' AND subject_id=? AND kind='event'""",
                (positive["command_id"],)).fetchall()
            life = db.execute("""SELECT command_end_status,direct_expired FROM subject_lifecycle_v1
                WHERE subject_kind='command' AND subject_id=?""", (positive["command_id"],)).fetchone()
        self.assertIsNotNone(start)
        self.assertEqual((request["authorizationRef"], request["authorizationEpoch"]),
                         (start[0], positive["terminal_start"]["seq"]))
        self.assertTrue(any(span in json.loads(row[0]) for row in source_rows))
        self.assertEqual(life, ("exited", 1))
        self.assertEqual(origin["post_end"]["reason"], "command_summary_scope_expired")
        self.assertEqual(self.typed_path.stat().st_size, run["backend_controls"]["actual_result_bytes"])
        self.assertEqual(digest(self.typed_path), run["backend_controls"]["actual_result_sha"])
        for key in ("requestId", "inputKey", "skillDigest", "sources"):
            self.assertEqual(self.typed[key], request[key])
        self.assertEqual(run["backend_controls"]["result"], self.typed)
        with sqlite3.connect(f"file:{self.private / 'backend.sqlite3'}?mode=ro", uri=True) as db:
            stored = db.execute("SELECT request_json FROM summary_requests_v1 ORDER BY created_at").fetchall()
            receipts = db.execute("SELECT request_id,result_digest FROM summary_result_receipts_v1").fetchall()
            events = db.execute("SELECT request_id,state FROM summary_request_events_v1 ORDER BY event_id").fetchall()
        self.assertEqual([json.loads(row[0]) for row in stored],
                         [self.requests["positive"], self.requests["saved_then_bash"]])
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0][0], request["requestId"])
        canonical = json.dumps(self.typed, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False).encode()
        self.assertEqual(receipts[0][1], sha256(canonical).hexdigest())
        self.assertIn((request["requestId"], "stored"), events)
        self.assertNotIn((self.requests["saved_then_bash"]["requestId"], "stored"), events)
        with sqlite3.connect(f"file:{self.private / 'summary.sqlite3'}?mode=ro", uri=True) as db:
            rows = db.execute("SELECT id,summary FROM summaries ORDER BY id").fetchall()
        self.assertEqual(rows, [(1, self.typed["facts"][0]), (2, self.typed["facts"][0])])
        # The second fixed DB write is provisional: a forbidden bash call rejects its result.
        self.assertNotEqual(run["requests"]["positive"]["requestId"],
                            run["requests"]["saved_then_bash"]["requestId"])

    def test_corrupt_custody_fields_are_detected_without_relabeling_fixture(self) -> None:
        request = self.requests["positive"]
        typed = self.typed
        expected_post_hash = self.runtime["provider_posts"][0]["body_sha"]
        expected_source = self.origin["cases"]["positive"]["selected_confirmed_span"]

        def matches(candidate_request: dict, candidate_typed: dict,
                    post_hash: str, source: dict) -> bool:
            return (candidate_request["sources"] == [source]
                    and candidate_request["inputKey"] == summary_input_key(
                        candidate_request["subject"], candidate_request["revision"],
                        candidate_request["sources"], candidate_request["sourceGaps"],
                        candidate_request["skillDigest"])
                    and all(candidate_typed[key] == candidate_request[key]
                            for key in ("requestId", "inputKey", "skillDigest", "sources"))
                    and post_hash == digest(self.private / "raw-post-01.json"))

        self.assertTrue(matches(request, typed, expected_post_hash, expected_source))
        wrong_id = copy.deepcopy(typed)
        wrong_id["requestId"] = self.requests["saved_then_bash"]["requestId"]
        wrong_span = copy.deepcopy(typed)
        wrong_span["sources"][0]["digest"] = "0" * 64
        wrong_key = copy.deepcopy(request)
        wrong_key["inputKey"] = "f" * 64
        for candidate in ((request, wrong_id, expected_post_hash, expected_source),
                          (request, wrong_span, expected_post_hash, expected_source),
                          (wrong_key, typed, expected_post_hash, expected_source),
                          (request, typed, "0" * 64, expected_source)):
            with self.subTest(candidate=candidate[1].get("requestId"), post_hash=candidate[2]):
                self.assertFalse(matches(*candidate))


if __name__ == "__main__":
    unittest.main()
