"""Independent checks of retained, source-bound OMP binding observations.

The actual runtime result is supplied explicitly; these tests never start OMP.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import unittest


ROOT = Path(__file__).resolve().parents[2]
POSITIVES = (
    "normal", "normal2", "normal3", "recover_missing", "recover_noop",
    "recover_api", "recover_stale", "recover_exception",
)


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def events(trace: list[dict], case: str, kind: str) -> list[dict]:
    return [item for item in trace if item.get("case") == case and item.get("event") == kind]


class BindingIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_BINDING_RESULT")
        if not supplied:
            raise AssertionError("Q01_SDK_BINDING_RESULT actual worker result required")
        cls.result = json.loads(Path(supplied).resolve(strict=True).read_text())
        cls.runtime_path = Path(cls.result["actual_runtime"]["path"]).resolve(strict=True)
        cls.runtime = json.loads(cls.runtime_path.read_text())
        cls.private = cls.runtime_path.parent / "private"

    def one(self, case: str, kind: str) -> dict:
        found = events(self.runtime["trace"], case, kind)
        self.assertEqual(len(found), 1, (case, kind))
        return found[0]

    def test_actual_source_raw_wire_sqlite_and_cleanup_custody(self) -> None:
        for source, expected in self.runtime["source_sha"].items():
            self.assertEqual(digest(Path(source)), expected, source)
        for source, expected in self.result["source_sha256"].items():
            self.assertEqual(digest(ROOT / source), expected, source)
        for artifact, expected in self.result["artifact_sha256"].items():
            self.assertEqual(digest(Path(artifact)), expected, artifact)
        self.assertEqual(digest(self.runtime_path), self.result["actual_runtime"]["sha256"])
        self.assertEqual(digest(Path(self.runtime["argv"][0])), self.runtime["omp_binary_sha"])
        self.assertEqual([json.loads(line) for line in (self.private / "trace.jsonl").read_text().splitlines()],
                         self.runtime["trace"])
        self.assertEqual(len(self.runtime["provider_posts"]), 17)
        for post in self.runtime["provider_posts"]:
            raw_path = Path(post["raw_body_path"])
            self.assertEqual(raw_path.parent, self.private)
            raw = raw_path.read_bytes()
            self.assertEqual(sha256(raw).hexdigest(), post["request_sha"])
            body = json.loads(raw)
            self.assertEqual(body["model"], "scripted")
            self.assertEqual([tool["function"]["name"] for tool in body["tools"]],
                             ["db_summary_save"])
        with sqlite3.connect(f"file:{self.private / 'summary.sqlite3'}?mode=ro", uri=True) as db:
            rows = [list(row) for row in db.execute("SELECT id, summary FROM summaries ORDER BY id")]
        self.assertEqual(rows, self.runtime["db_rows"])
        self.assertEqual(len(rows), 8)
        written = [json.loads(self.one(case, "writer_invoked")["argv"][1])["summary"]
                   for case in POSITIVES]
        self.assertEqual([row[1] for row in rows], written)
        self.assertEqual(self.runtime["sentinels"],
                         {"command-sentinel": "ORIGINAL", "file-sentinel": "ORIGINAL"})
        self.assertEqual(self.runtime["permits_remaining"], [])
        self.assertTrue(self.runtime["process"]["pump_stopped"])
        self.assertEqual(self.runtime["process"]["remaining_owned"], [])
        with self.assertRaises(ProcessLookupError):
            os.kill(self.runtime["process"]["pid"], 0)

    def test_eight_admitted_receipts_bind_identity_policy_and_one_use_nonce(self) -> None:
        nonces = []
        sessions = []
        for case in POSITIVES:
            admission = self.one(case, "startup_admitted")
            admitted = admission["receipt"]
            before = self.one(case, "before_prompt")
            self.assertEqual(before["receipt"], admitted)
            self.assertEqual(admitted["requestId"], f"q01-{case}")
            created = [item for item in events(self.runtime["trace"], case, "session_created")
                       if item["attempt"] == admission["attempt"]]
            self.assertEqual(len(created), 1)
            self.assertEqual(admitted["sessionId"], created[0]["sessionId"])
            self.assertEqual(admitted["epoch"], f"epoch-{case}-{admission['attempt']}")
            self.assertEqual(admitted["route"], "private-manager-task")
            self.assertEqual(before["active"], ["db_summary_save"])
            self.assertEqual(before["model"], admitted["modelKey"])
            for key in ("policySha", "rosterSha", "schemaSha"):
                self.assertEqual(len(admitted[key]), 64)
                int(admitted[key], 16)
            self.assertEqual(self.one(case, "case_done")["saved"], True)
            self.assertEqual(len(events(self.runtime["trace"], case, "saved")), 1)
            self.assertEqual(len(events(self.runtime["trace"], case, "result_accepted")), 1)
            created_events = events(self.runtime["trace"], case, "session_created")
            disposed_events = events(self.runtime["trace"], case, "disposed")
            self.assertEqual(len(disposed_events), len(created_events))
            self.assertEqual({item["attempt"] for item in disposed_events},
                             {item["attempt"] for item in created_events})
            self.assertEqual(sum(post["case"] == case for post in self.runtime["provider_posts"]), 2)
            nonces.append(admitted["nonce"])
            sessions.append(admitted["sessionId"])
        self.assertEqual(len(set(nonces)), 8)
        self.assertEqual(len(set(sessions)), 8)

    def test_route_and_nonce_single_field_delta_reject_before_transport_or_write(self) -> None:
        trace = self.runtime["trace"]
        for case, field in (("runtime_route", "route"), ("runtime_nonce", "nonce")):
            admitted_event = self.one(case, "startup_admitted")
            admitted = admitted_event["receipt"]
            before = self.one(case, "before_prompt")["receipt"]
            self.assertEqual(set(admitted), set(before))
            self.assertEqual({key for key in admitted if admitted[key] != before[key]}, {field})
            self.assertEqual(admitted["sessionId"], self.one(case, "session_created")["sessionId"])
            self.assertLess(trace.index(admitted_event), trace.index(self.one(case, "before_prompt")))
            guard = self.one(case, "stream_guard")
            self.assertEqual(guard["reason"], "stale_binding")
            self.assertLess(trace.index(self.one(case, "prompt_start")), trace.index(guard))
            self.assertLess(trace.index(guard), trace.index(self.one(case, "prompt_done")))
            for kind in ("original_stream_called", "execute_received", "writer_invoked",
                         "saved", "result_accepted"):
                self.assertEqual(events(trace, case, kind), [], (case, kind))
            self.assertEqual([post for post in self.runtime["provider_posts"]
                              if post["case"] == case], [])
            done = self.one(case, "prompt_done")
            self.assertFalse(done["saved"])
            self.assertTrue(done["protected"])
            self.assertEqual(len(events(trace, case, "result_rejected")), 1)
            self.assertEqual(len(events(trace, case, "disposed")), 1)

    def test_negative_custody_controls_detect_mislabelled_post_or_missing_delta(self) -> None:
        # Check the observation predicates against altered copies, not the runner's checks dict.
        case = "runtime_route"
        admitted = self.one(case, "startup_admitted")["receipt"]
        before = self.one(case, "before_prompt")["receipt"]
        self.assertEqual({key for key in admitted if admitted[key] != before[key]}, {"route"})
        altered = dict(before, route=admitted["route"])
        self.assertEqual({key for key in admitted if admitted[key] != altered[key]}, set())
        posts = list(self.runtime["provider_posts"])
        self.assertFalse(any(post["case"] == case for post in posts))
        posts.append(dict(posts[0], case=case))
        self.assertTrue(any(post["case"] == case for post in posts))


if __name__ == "__main__":
    unittest.main()
