"""Independent checks of the retained one-run OMP parent/child overlap evidence.

An explicit result path is required; this suite does not invoke OMP or a model.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import unittest


ROOT = Path(__file__).resolve().parents[2]
MARKER = "Q01-private-raw-marker-1-REMOVE-FROM-PARENT"
PARENT_PROMPT = "q01-priority-normal-turn"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def content_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(item.get("text", "") for item in content
                       if isinstance(item, dict) and item.get("type") == "text")
    return ""


def overlaps(timeline: dict) -> bool:
    return (timeline["child_held_ns"] < timeline["parent_prompt_sent_ns"]
            < timeline["parent_post_ns"] < timeline["parent_agent_end_ns"]
            < timeline["owner_release_ns"] < timeline["child_released_ns"])


class PriorityOverlapIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_PRIORITY_RESULT")
        if not supplied:
            raise unittest.SkipTest("Q01_SDK_PRIORITY_RESULT actual worker result required; skipped tests are not a gate pass")
        cls.result = json.loads(Path(supplied).resolve(strict=True).read_text())
        cls.evidence_path = Path(cls.result["runtime_evidence"]).resolve(strict=True)
        cls.evidence = json.loads(cls.evidence_path.read_text())
        cls.private = cls.evidence_path.parent / "private"

    def event(self, kind: str) -> dict:
        found = [row for row in self.evidence["trace"] if row.get("event") == kind]
        self.assertEqual(len(found), 1, kind)
        return found[0]

    def test_source_raw_posts_and_owned_process_custody(self) -> None:
        e = self.evidence
        self.assertEqual(digest(self.evidence_path), self.result["runtime_evidence_sha256"])
        for name, expected in self.result["artifact_sha256"].items():
            self.assertEqual(digest(Path(name)), expected, name)
        for name, expected in self.result["source_sha256"].items():
            self.assertEqual(digest(ROOT / name), expected, name)
        self.assertEqual(e["source_sha256"],
                         {str(ROOT / name): expected
                          for name, expected in self.result["source_sha256"].items()})
        self.assertEqual(digest(Path(e["argv"][0])), e["omp_sha256"])
        self.assertEqual(e["omp_sha256"], self.result["omp_binary_sha256"])
        self.assertEqual(e["omp_version"], self.result["omp_version"])
        self.assertEqual([json.loads(line) for line in
                          (self.private / "trace.jsonl").read_text().splitlines()], e["trace"])
        self.assertEqual([post["stage"] for post in e["posts"]],
                         ["child_first", "parent", "child_second"])
        for number, post in enumerate(e["posts"], 1):
            raw_path = Path(post["raw_body_path"])
            self.assertEqual(raw_path, self.private / f"provider-post-{number:03d}.body")
            raw = raw_path.read_bytes()
            self.assertEqual(len(raw), post["bytes"])
            self.assertEqual(sha256(raw).hexdigest(), post["sha256"])
            self.assertEqual(post["path"], "/v1/chat/completions")
            self.assertEqual(json.loads(raw)["model"], "scripted")
        self.assertEqual(e["process"]["pid"], e["process"]["pgid"])
        self.assertEqual({row["pid"] for row in e["trace"]}, {e["process"]["pid"]})
        self.assertTrue(e["process"]["pump_stopped"])
        self.assertEqual(e["process"]["remaining_owned"], [])
        self.assertTrue(e["provider_stopped"])
        self.assertIsNone(e["provider_state"]["violation"])
        self.assertFalse(e["provider_state"]["hold_timed_out"])
        with self.assertRaises(ProcessLookupError):
            os.killpg(e["process"]["pgid"], 0)

    def test_parent_dispatch_and_agent_end_precede_child_release(self) -> None:
        e = self.evidence
        t = e["timeline"]
        self.assertTrue(overlaps(t))
        self.assertEqual(t["child_held_ns"], e["provider_state"]["held_ns"])
        self.assertEqual(t["child_released_ns"], e["provider_state"]["released_ns"])
        self.assertEqual(t["parent_post_ns"], e["posts"][1]["received_ns"])
        self.assertLess(e["posts"][0]["received_ns"], t["child_held_ns"])
        self.assertGreater(e["posts"][0]["responded_ns"], t["child_released_ns"])
        self.assertLess(e["posts"][1]["responded_ns"], t["owner_release_ns"])
        self.assertGreater(e["posts"][2]["received_ns"], t["child_released_ns"])
        frames = e["rpc_frames"]
        self.assertEqual(len([f for f in frames if f["id"] == "priority-parent"
                              and f["type"] == "response" and f["error"] is None]), 1)
        self.assertEqual(len([f for f in frames if f["id"] == "priority-parent"
                              and f["type"] == "prompt_result" and f["error"] is None]), 1)
        ends = [f for f in frames if f["type"] == "agent_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["observed_ns"], t["parent_agent_end_ns"])
        self.assertLess(t["parent_post_ns"], ends[0]["observed_ns"])
        self.assertLess(ends[0]["observed_ns"], t["owner_release_ns"])
        self.assertTrue(any(f["type"] == "turn_end" and
                            t["parent_post_ns"] < f["observed_ns"] < ends[0]["observed_ns"]
                            for f in frames))
        altered = dict(t, parent_agent_end_ns=t["owner_release_ns"] + 1)
        self.assertFalse(overlaps(altered))

    def test_child_db_only_and_parent_normal_roster_receipt_sqlite(self) -> None:
        e = self.evidence
        bodies = [json.loads(Path(post["raw_body_path"]).read_bytes()) for post in e["posts"]]
        child_first, parent, child_second = bodies
        def names(body: dict) -> list[str]:
            return [tool["function"]["name"] for tool in body["tools"]]
        self.assertEqual(names(child_first), ["db_summary_save"])
        self.assertEqual(names(child_second), ["db_summary_save"])
        self.assertTrue({"read", "bash", "edit", "write"}.issubset(names(parent)))
        self.assertNotIn("db_summary_save", names(parent))
        self.assertEqual([body["max_completion_tokens"] for body in bodies], [1024] * 3)
        self.assertTrue(any(PARENT_PROMPT in content_text(message)
                            for message in parent["messages"] if message["role"] == "user"))
        self.assertTrue(all(MARKER not in content_text(message) for message in parent["messages"]))
        self.assertTrue(any(MARKER in content_text(message) for body in (child_first, child_second)
                            for message in body["messages"]))
        self.assertEqual([m["role"] for m in child_first["messages"]], ["system", "user"])
        self.assertEqual([m["role"] for m in child_second["messages"]],
                         ["system", "user", "assistant", "tool"])
        self.assertTrue(self.event("startup")["admitted"])
        self.assertEqual(self.event("startup")["active"], ["db_summary_save"])
        self.assertEqual(len([row for row in e["trace"] if row["event"] == "stream_guard"]), 2)
        self.assertEqual(self.event("execute")["params"], {"summary": MARKER})
        self.assertEqual(json.loads(self.event("writer")["argv"][1]), {"summary": MARKER})
        self.assertIsNone(self.event("writer")["error"])
        self.assertEqual(json.loads(self.event("writer")["stdout"]), {"saved": True})
        with sqlite3.connect(f"file:{self.private / 'summary.sqlite3'}?mode=ro", uri=True) as db:
            rows = [list(row) for row in db.execute("SELECT id, summary FROM summaries ORDER BY id")]
        self.assertEqual(rows, [[1, MARKER]])
        self.assertEqual(rows, e["db_rows"])
        sent = self.event("cycle_receipt_sent")
        receipt = json.loads(sent["receipt"])
        self.assertEqual(sent["bytes"], len(sent["receipt"].encode()))
        self.assertEqual(sent["bytes"], 153)
        self.assertLessEqual(sent["bytes"], 4096)
        self.assertEqual(receipt, {"kind": "summary_result", "requestId": "q01-context-1",
                                   "status": "accepted", "summarySha": sha256(MARKER.encode()).hexdigest()})
        self.assertEqual(sent["parentRosterBefore"], sent["parentRosterAfter"])
        self.assertLess(e["trace"].index(sent), e["trace"].index(self.event("child_disposed")))


if __name__ == "__main__":
    unittest.main()
