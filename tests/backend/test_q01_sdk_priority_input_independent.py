"""Independent checks of retained installed-OMP TUI draft/busy/ready observations.

The actual runtime result is an explicit input. This suite never starts OMP.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import unittest


ROOT = Path(__file__).resolve().parents[2]
DRAFT = "Q01-UNSENT-DRAFT"
MARKER = "Q01-private-raw-marker-1-REMOVE-FROM-PARENT"
NORMAL = "Q01-priority-input-normal"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(item.get("text", "") for item in content
                       if isinstance(item, dict) and item.get("type") == "text")
    return ""


class PriorityInputIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_PRIORITY_INPUT_RESULT")
        if not supplied:
            raise AssertionError("Q01_SDK_PRIORITY_INPUT_RESULT actual worker result required")
        cls.result = json.loads(Path(supplied).resolve(strict=True).read_text())
        cls.evidence_path = Path(cls.result["runtime_evidence"]).resolve(strict=True)
        cls.evidence = json.loads(cls.evidence_path.read_text())
        cls.private = cls.evidence_path.parent / "private"

    def one(self, trace: list[dict], event: str, **match: object) -> dict:
        found = [row for row in trace if row.get("event") == event and
                 all(row.get(key) == value for key, value in match.items())]
        self.assertEqual(len(found), 1, (event, match))
        return found[0]

    def test_actual_tui_source_raw_and_owned_resource_custody(self) -> None:
        e = self.evidence
        self.assertEqual(digest(self.evidence_path), self.result["runtime_evidence_sha256"])
        for name, expected in self.result["source_sha256"].items():
            self.assertEqual(digest(ROOT / name), expected, name)
        self.assertEqual(e["source_sha256"],
                         {str(ROOT / name): expected
                          for name, expected in self.result["source_sha256"].items()})
        for name, expected in self.result["artifact_sha256"].items():
            self.assertEqual(digest(Path(name)), expected, name)
        self.assertEqual(digest(Path(e["argv"][2])), e["omp_sha256"])
        self.assertEqual(e["omp_sha256"], self.result["omp_binary_sha256"])
        self.assertEqual(e["omp_version"], self.result["omp_version"])
        self.assertEqual([json.loads(line) for line in
                          (self.private / "priority-trace.jsonl").read_text().splitlines()],
                         e["priority_trace"])
        self.assertEqual([json.loads(line) for line in
                          (self.private / "context-trace.jsonl").read_text().splitlines()],
                         e["context_trace"])
        self.assertIn(DRAFT.encode(), (self.evidence_path.parent / "pty-output.raw").read_bytes())
        self.assertEqual({row["pid"] for row in e["priority_trace"] + e["context_trace"]},
                         {e["process"]["pid"]})
        self.assertEqual(e["process"]["pid"], e["process"]["pgid"])
        self.assertEqual(e["process"]["remaining_owned"], [])
        self.assertTrue(e["process"]["pty_drain_stopped"])
        self.assertTrue(e["provider_stopped"])
        self.assertIsNone(e["provider_state"]["violation"])
        self.assertFalse(e["provider_state"]["main_hold_timed_out"])
        with self.assertRaises(ProcessLookupError):
            os.killpg(e["process"]["pgid"], 0)

    def test_public_draft_busy_ready_readbacks_defer_until_third_callback(self) -> None:
        e = self.evidence
        trace = e["priority_trace"]
        session = self.one(trace, "interactive_session")
        self.assertEqual((session["mode"], session["hasUI"]), ("tui", True))
        self.assertEqual([session[key] for key in
                          ("editorGetter", "idleGetter", "pendingGetter")], ["function"] * 3)
        self.assertTrue({"read", "bash", "edit", "ask", "write"}.issubset(session["activeTools"]))
        states = [self.one(trace, "admission_attempt", attempt=n) for n in (1, 2, 3)]
        expected = [(DRAFT, True, False, False), ("", False, False, False),
                    ("", True, False, True)]
        for state, (editor, idle, pending, ready) in zip(states, expected):
            self.assertEqual((state["mode"], state["hasUI"]), ("tui", True))
            self.assertEqual((state["editor"], state["idle"], state["pending"], state["ready"]),
                             (editor, idle, pending, ready))
            self.assertEqual(state["editorBytes"], len(editor.encode()))
            self.assertEqual(state["editorSha"], sha256(editor.encode()).hexdigest())
            self.assertEqual(state["childCallbackCalls"], 0)
        self.assertEqual(self.one(trace, "deferred", attempt=1)["reason"], "draft")
        self.assertEqual(self.one(trace, "deferred", attempt=2)["reason"], "busy")
        self.assertEqual(len([row for row in trace if row["event"] == "deferred"]), 2)
        callback = self.one(trace, "captured_callback_invoked", attempt=3)
        self.assertLess(trace.index(states[0]), trace.index(states[1]))
        self.assertLess(trace.index(states[1]), trace.index(self.one(trace, "main_agent_end")))
        self.assertLess(trace.index(self.one(trace, "main_agent_end")), trace.index(states[2]))
        self.assertLess(trace.index(states[2]), trace.index(callback))
        self.assertEqual([row["attempt"] for row in e["signals"]], [1, 2, 3])
        sent = [row["sent_ns"] for row in e["signals"]]
        parent = e["posts"][0]
        child = e["posts"][1:]
        hold = e["provider_state"]
        self.assertLess(sent[0], parent["received_ns"])
        self.assertLess(parent["received_ns"], hold["main_held_ns"])
        self.assertLess(hold["main_held_ns"], sent[1])
        self.assertLess(sent[1], hold["main_released_ns"])
        self.assertLess(hold["main_released_ns"], sent[2])
        self.assertTrue(all(post["received_ns"] > sent[2] for post in child))
        self.assertLess(hold["main_released_ns"] - hold["main_held_ns"], 3_000_000_000)

    def test_parent_normal_raw_and_child_db_only_save_receipt_disposal(self) -> None:
        e = self.evidence
        self.assertEqual([post["stage"] for post in e["posts"]],
                         ["parent", "child_first", "child_second"])
        bodies = []
        for number, post in enumerate(e["posts"], 1):
            path = Path(post["raw_body_path"])
            self.assertEqual(path, self.private / f"provider-post-{number:03d}.body")
            raw = path.read_bytes()
            self.assertEqual(len(raw), post["bytes"])
            self.assertEqual(sha256(raw).hexdigest(), post["sha256"])
            self.assertEqual(post["path"], "/v1/chat/completions")
            bodies.append(json.loads(raw))
        def tools(body: dict) -> list[str]:
            return [tool["function"]["name"] for tool in body["tools"]]
        parent, child_first, child_second = bodies
        self.assertTrue({"read", "bash", "edit", "ask", "write"}.issubset(tools(parent)))
        self.assertEqual(len(tools(parent)), 12)
        self.assertNotIn("db_summary_save", tools(parent))
        self.assertEqual(tools(child_first), ["db_summary_save"])
        self.assertEqual(tools(child_second), ["db_summary_save"])
        self.assertEqual([body["max_completion_tokens"] for body in bodies], [1024] * 3)
        self.assertTrue(any(NORMAL in text(m) for m in parent["messages"] if m["role"] == "user"))
        self.assertTrue(all(DRAFT not in text(m) and MARKER not in text(m)
                            for m in parent["messages"]))
        self.assertTrue(any(MARKER in text(m) for body in (child_first, child_second)
                            for m in body["messages"]))
        self.assertEqual([m["role"] for m in child_second["messages"]],
                         ["system", "user", "assistant", "tool"])
        context = e["context_trace"]
        self.assertTrue(self.one(context, "startup")["admitted"])
        self.assertEqual(self.one(context, "startup")["active"], ["db_summary_save"])
        self.assertEqual(len([r for r in context if r["event"] == "stream_guard"]), 2)
        self.assertEqual(self.one(context, "execute")["params"], {"summary": MARKER})
        writer = self.one(context, "writer")
        self.assertEqual(json.loads(writer["argv"][1]), {"summary": MARKER})
        self.assertIsNone(writer["error"])
        self.assertEqual(json.loads(writer["stdout"]), {"saved": True})
        with sqlite3.connect(f"file:{self.private / 'summary.sqlite3'}?mode=ro", uri=True) as db:
            rows = [list(row) for row in db.execute("SELECT id, summary FROM summaries ORDER BY id")]
        self.assertEqual(rows, [[1, MARKER]])
        self.assertEqual(rows, e["db_rows"])
        receipt = self.one(context, "cycle_receipt_sent")
        self.assertEqual(receipt, e["receipt_sent"])
        self.assertEqual(receipt["bytes"], len(receipt["receipt"].encode()))
        self.assertEqual(receipt["bytes"], 153)
        self.assertEqual(json.loads(receipt["receipt"]),
                         {"kind": "summary_result", "requestId": "q01-context-1",
                          "status": "accepted", "summarySha": sha256(MARKER.encode()).hexdigest()})
        self.assertEqual(receipt["parentRosterBefore"], receipt["parentRosterAfter"])
        self.assertLess(context.index(receipt), context.index(self.one(context, "child_disposed")))


if __name__ == "__main__":
    unittest.main()
