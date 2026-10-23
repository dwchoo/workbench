"""Independent checks of retained G3/TUI scheduler and private SDK overlap evidence.

The explicit result is read-only; these tests never launch OMP or a provider.
"""

from __future__ import annotations

import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import unittest


ROOT = Path(__file__).resolve().parents[2]
SUMMARY = "Q01 priority runtime private literal summary"
DRAFT = "Q01-PRIORITY-UNSENT-DRAFT"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def text(message: dict) -> str:
    value = message.get("content")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(item.get("text", "") for item in value
                       if isinstance(item, dict) and item.get("type") == "text")
    return ""


class PriorityRuntimeIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_PRIORITY_RUNTIME_RESULT")
        if not supplied:
            raise unittest.SkipTest("Q01_SDK_PRIORITY_RUNTIME_RESULT actual worker result required; skipped tests are not a gate pass")
        cls.result_path = Path(supplied).resolve(strict=True)
        cls.result = json.loads(cls.result_path.read_text())
        cls.runtime_path = Path(cls.result["actual_runtime"]["path"]).resolve(strict=True)
        cls.runtime = json.loads(cls.runtime_path.read_text())
        cls.private = cls.runtime_path.parent / "private"
        cls.wire = []
        for line in (cls.private / "bridge-wire.jsonl").read_text().splitlines():
            record = json.loads(line)
            raw = base64.b64decode(record["raw_b64"], validate=True)
            if sha256(raw).hexdigest() != record["sha256"]:
                raise AssertionError("G3 raw frame SHA mismatch")
            cls.wire.append({**record, "frame": json.loads(raw)})

    def frame(self, direction: str, kind: str, request_id: str) -> dict:
        found = [row for row in self.wire if row["direction"] == direction and
                 row["frame"].get("kind") == kind and
                 row["frame"].get("requestId") == request_id]
        self.assertEqual(len(found), 1, (direction, kind, request_id))
        return found[0]

    def test_source_artifact_hello_sessions_and_four_owned_groups(self) -> None:
        r, x = self.result, self.runtime
        self.assertEqual(digest(self.runtime_path), r["actual_runtime"]["sha256"])
        for name, item in r["artifact_manifest"].items():
            self.assertEqual(Path(name).stat().st_size, item["bytes"], name)
            self.assertEqual(digest(Path(name)), item["sha256"], name)
        for name, expected in x["source_sha256"].items():
            self.assertEqual(digest(Path(name)), expected, name)
        for name, expected in r["source_sha256"].items():
            self.assertEqual(digest(Path(name)), expected, name)
        for name, expected in r["unchanged_input_sha256_verified"].items():
            self.assertEqual(digest(ROOT / name), expected, name)
        self.assertEqual(digest(Path(x["processes"]["manager"]["argv"][2])), x["omp_binary_sha256"])
        self.assertEqual(x["omp_binary_sha256"], r["actual_runtime"]["omp_binary_sha256"])
        self.assertEqual(len(self.wire), 41)
        self.assertEqual([json.loads(line) for line in
                          (self.private / "budget-trace.jsonl").read_text().splitlines()],
                         x["budget_trace"])
        self.assertEqual([json.loads(line) for line in
                          (self.private / "priority-trace.jsonl").read_text().splitlines()],
                         x["wrapper_trace"])
        hellos = {}
        for role in ("manager", "worker"):
            found = [row["frame"] for row in self.wire if row["direction"] == "omp_to_host"
                     and row["frame"].get("kind") == "hello" and row["frame"].get("role") == role]
            self.assertEqual(len(found), 1)
            hellos[role] = found[0]
            self.assertEqual(found[0], x["bridge"]["hello"][role])
            self.assertEqual(found[0]["pid"], x["processes"][role]["pid"])
            self.assertEqual(found[0]["ompVersion"], x["omp_version"])
            self.assertEqual(x["bridge"]["initial_state"][role]["sessionId"],
                             found[0]["ompSessionId"])
            self.assertEqual(x["bridge"]["initial_state"][role]["role"], role)
        child = next(row for row in x["budget_trace"] if row["event"] == "startup")
        self.assertTrue(child["admitted"])
        self.assertEqual(child["active"], ["db_summary_save"])
        self.assertEqual(len({hellos["manager"]["ompSessionId"],
                              hellos["worker"]["ompSessionId"], child["childSessionId"]}), 3)
        self.assertEqual(x["wrapper_trace"][0]["parentSessionId"],
                         hellos["manager"]["ompSessionId"])
        self.assertIn(DRAFT.encode(), (self.private / "manager/pty-output.raw").read_bytes())
        for name, process in r["owned_processes"].items():
            self.assertEqual(process["returncode"], 143, name)
            with self.assertRaises(ProcessLookupError):
                os.killpg(process["pgid"], 0)
        for role in ("manager", "worker"):
            self.assertTrue(x["processes"][role]["pump_stopped"])
            self.assertTrue(x["providers"][role]["thread_stopped"])
            self.assertIsNone(x["providers"][role]["violation"])
        self.assertTrue(x["bridge"]["thread_stopped"])

    def test_raw_g3_notice_frames_due_clock_and_real_write_to_ack_latency(self) -> None:
        x = self.runtime
        scheduler = x["scheduler"]
        self.assertEqual(scheduler["clock_injected"], [0.0, 60.0])
        self.assertEqual((scheduler["tick0"]["status"], scheduler["tick0"]["review_count"]),
                         ("waiting", 0))
        self.assertEqual((scheduler["tick60"]["status"], scheduler["tick60"]["review_count"]),
                         ("dispatched", 1))
        requests = x["bridge"]["requests"]
        self.assertEqual(len(requests), 8)
        for item in requests:
            rid = item["ack"]["requestId"]
            sent = self.frame("host_to_omp", item["frame"]["kind"], rid)
            ack = self.frame("omp_to_host", "api_ack", rid)
            self.assertEqual(sent["frame"]["requestId"], rid)
            self.assertEqual(ack["frame"], item["ack"])
            self.assertLessEqual(item["sent_ns"], sent["monotonic_ns"])
            self.assertLess(sent["monotonic_ns"], ack["monotonic_ns"])
            self.assertLessEqual(ack["monotonic_ns"], item["ack_observed_ns"])
            if item["frame"]["kind"] == "notice":
                self.assertEqual(sent["frame"]["notice"], item["frame"]["notice"])
        notices = [item for item in requests if item["frame"]["kind"] == "notice"]
        self.assertEqual([(n["role"], n["frame"]["notice"]["type"], n["ack"]["status"])
                          for n in notices],
                         [("manager", "worker_terminal_done", "deferred"),
                          ("worker", "terminal_check", "api_accepted"),
                          ("manager", "worker_terminal_done", "api_accepted")])
        deferred, worker, accepted = notices
        manager_id = deferred["frame"]["notice"]["notice_id"]
        self.assertEqual(accepted["frame"]["notice"]["notice_id"], manager_id)
        self.assertEqual(accepted["ack"]["noticeId"], manager_id)
        self.assertEqual(x["manager_draft_state"]["editorLength"], len(DRAFT))
        self.assertFalse(x["manager_draft_state"]["editorEmpty"])
        self.assertEqual(x["manager_draft_ack"], deferred["ack"])
        self.assertTrue(x["manager_clear_state"]["editorEmpty"])
        self.assertEqual(x["manager_clear_state"]["editorLength"], 0)
        self.assertEqual(x["manager_accept_ack"], accepted["ack"])
        for state in (x["manager_draft_state"], x["manager_clear_state"]):
            self.assertTrue(state["idle"])
            self.assertFalse(state["pending"])
            self.assertEqual(state["inFlightToolCount"], 0)
        worker_notice = worker["frame"]["notice"]
        self.assertEqual(worker_notice["due_at"], 60.0)
        self.assertEqual(worker_notice["notice_id"], worker["ack"]["noticeId"])
        dispatch = scheduler["notice_dispatches"]
        self.assertEqual(len(dispatch), 1)
        self.assertEqual(dispatch[0]["notice_id"], worker_notice["notice_id"])
        self.assertEqual(dispatch[0]["review_due_at"], 60.0)
        worker_ack_wire = self.frame("omp_to_host", "api_ack", worker["ack"]["requestId"])
        self.assertGreater(dispatch[0]["sent_ns"], worker_ack_wire["monotonic_ns"])
        # The scheduler field is after ack. Wire write/read timestamps measure the transport interval.
        self.assertGreater(worker_ack_wire["monotonic_ns"] -
                           self.frame("host_to_omp", "notice", worker["ack"]["requestId"])["monotonic_ns"], 0)

    def test_raw_posts_and_g3_events_show_parent_delivery_during_child_hold(self) -> None:
        x = self.runtime
        manager_posts = x["providers"]["manager"]["posts"]
        worker_posts = x["providers"]["worker"]["posts"]
        self.assertEqual([p["stage"] for p in manager_posts],
                         ["child_first", "parent", "child_second"])
        self.assertEqual([p["stage"] for p in worker_posts], ["parent"])
        posts = manager_posts + worker_posts
        for p in posts:
            raw = Path(p["raw_body_path"]).read_bytes()
            self.assertEqual(len(raw), p["bytes"])
            self.assertEqual(sha256(raw).hexdigest(), p["sha256"])
            self.assertEqual(digest(Path(p["response_body_path"])), p["response_sha256"])
            body = json.loads(raw)
            self.assertEqual(body["model"], "scripted")
            self.assertEqual([tool["function"]["name"] for tool in body["tools"]], p["tools"])
            self.assertEqual(body["max_completion_tokens"], p["max_completion_tokens"])
            self.assertEqual(p["path"], "/v1/chat/completions")
            self.assertTrue(all(DRAFT not in text(m) for m in body["messages"]))
            if p["stage"] == "parent":
                self.assertTrue({"read", "bash", "edit", "write"}.issubset(p["tools"]))
                self.assertNotIn("db_summary_save", p["tools"])
                self.assertTrue(any(p["notice_id"] in text(m) for m in body["messages"]))
            else:
                self.assertEqual(p["tools"], ["db_summary_save"])
                self.assertIsNone(p["notice_id"])
                self.assertEqual(p["max_completion_tokens"], 8192)
        hold = x["child_hold_started_ns"]
        release = x["child_release_signal_ns"]
        self.assertEqual(hold, manager_posts[0]["received_ns"])
        self.assertLess(hold, worker_posts[0]["received_ns"])
        self.assertLess(worker_posts[0]["received_ns"], manager_posts[1]["received_ns"])
        self.assertLess(manager_posts[1]["received_ns"], release)
        self.assertLess(x["both_notice_posts_before_release_ns"], release)
        self.assertLess(release, manager_posts[0]["hold_released_ns"])
        self.assertLess(manager_posts[0]["hold_released_ns"], manager_posts[2]["received_ns"])
        for role, parent_post in (("worker", worker_posts[0]), ("manager", manager_posts[1])):
            hello = x["bridge"]["hello"][role]
            starts = [row for row in self.wire if row["direction"] == "omp_to_host" and
                      row["frame"].get("kind") == "omp_event" and
                      row["frame"].get("name") == "provider_request_started" and
                      row["frame"].get("sessionId") == hello["ompSessionId"]]
            self.assertEqual(len(starts), 1)
            self.assertLess(starts[0]["monotonic_ns"], parent_post["received_ns"])
            self.assertLess(parent_post["received_ns"], release)
        # An observed state/probe is a snapshot, not proof that every lock stays unoccupied.
        self.assertTrue(any(row["frame"].get("name") == "agent_end" and
                            row["frame"].get("sessionId") == x["bridge"]["hello"]["worker"]["ompSessionId"]
                            and row["monotonic_ns"] < release for row in self.wire))

    def test_selected_input_preflight_typed_result_and_sqlite_binding(self) -> None:
        x = self.runtime
        trace = x["budget_trace"]
        selection = next(row for row in trace if row["event"] == "selection")
        segments = json.loads((self.private / "segments.json").read_text())
        request = json.loads((self.private / "request.json").read_text())
        typed_path = self.private / "typed-result.json"
        typed = json.loads(typed_path.read_text())
        self.assertEqual(sum(len(s["text"].encode()) for s in segments), 1048576)
        self.assertEqual(selection["collectedBytes"], 1048576)
        self.assertEqual(selection["selectionMax"], 400)
        self.assertEqual(selection["selectedBytes"], 120)
        self.assertEqual(len(selection["selected"]), 1)
        selected = selection["selected"][0]
        source = next(s for s in segments if s["source"] == selected["source"])
        self.assertEqual(selected["textSha"], sha256(source["text"].encode()).hexdigest())
        self.assertEqual((selected["start"], selected["end"]),
                         (request["sources"][0]["start"], request["sources"][0]["end"]))
        self.assertEqual(selected["textSha"], request["sources"][0]["digest"])
        self.assertEqual(request["requestId"], x["request_id"])
        self.assertEqual(request["skillDigest"], digest(ROOT / "tests/backend/q01_sdk_private_probe/summary-skill.txt"))
        preflights = [row for row in trace if row["event"] == "dispatch_preflight"]
        self.assertEqual([p["contextBytes"] for p in preflights], [2212, 3019])
        self.assertEqual([p["inputLimit"] - p["contextBytes"] for p in preflights], [1064, 257])
        for p in preflights:
            self.assertTrue(p["admitted"])
            self.assertEqual(p["names"], ["db_summary_save"])
            self.assertEqual((p["inputLimit"], p["outputCap"], p["fittedCap"]),
                             (3276, 8192, 8192))
            self.assertFalse(p["nativeDiagnostic"]["exact"])
        self.assertEqual(typed_path.stat().st_size, 710)
        self.assertEqual(digest(typed_path), x["typed_contract"]["sha256"])
        for field in ("requestId", "inputKey", "skillDigest", "sources"):
            self.assertEqual(typed[field], request[field])
        self.assertEqual(typed["facts"], [SUMMARY])
        self.assertEqual(next(r for r in trace if r["event"] == "execute")["params"],
                         {"summary": SUMMARY})
        writer = next(r for r in trace if r["event"] == "writer")
        self.assertEqual(json.loads(writer["argv"][1]), {"summary": SUMMARY})
        self.assertEqual(json.loads(writer["stdout"]), {"saved": True})
        self.assertIsNone(writer["error"])
        self.assertEqual([r["event"] for r in trace][-2:], ["done", "disposed"])
        with sqlite3.connect(f"file:{self.private / 'summary.sqlite3'}?mode=ro", uri=True) as db:
            self.assertEqual(db.execute("SELECT id,summary FROM summaries ORDER BY id").fetchall(),
                             [(1, SUMMARY)])
        with sqlite3.connect(f"file:{self.private / 'backend.sqlite3'}?mode=ro", uri=True) as db:
            saved = db.execute("SELECT request_json FROM summary_requests_v1").fetchall()
            states = db.execute("SELECT state FROM summary_request_events_v1 ORDER BY event_id").fetchall()
            receipts = db.execute("SELECT request_id,result_digest FROM summary_result_receipts_v1").fetchall()
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0][0]), request)
        self.assertEqual(states, [("prepared",), ("sending",), ("stored",)])
        canonical = json.dumps(typed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        self.assertEqual(receipts, [(request["requestId"], sha256(canonical).hexdigest())])


if __name__ == "__main__":
    unittest.main()
