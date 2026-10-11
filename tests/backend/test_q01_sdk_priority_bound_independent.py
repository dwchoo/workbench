"""Independent checks of the retained near-bound priority input fixture.

The explicit actual result is read-only; this suite never launches OMP.
"""

from __future__ import annotations

import ast
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import unittest

from tests.backend import test_q01_sdk_priority_runtime_independent as generic_priority


ROOT = Path(__file__).resolve().parents[2]
SUMMARY = "Q01 priority runtime private literal summary"
DRAFT = "Q01-PRIORITY-UNSENT-DRAFT"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


class PriorityBoundIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_PRIORITY_BOUND_RESULT")
        if not supplied:
            raise AssertionError("Q01_SDK_PRIORITY_BOUND_RESULT actual worker result required")
        cls.result = json.loads(Path(supplied).resolve(strict=True).read_text())
        cls.runtime_path = Path(cls.result["actual_runtime"]["path"]).resolve(strict=True)
        cls.runtime = json.loads(cls.runtime_path.read_text())
        cls.private = cls.runtime_path.parent / "private"
        cls.wire = []
        for line in (cls.private / "bridge-wire.jsonl").read_text().splitlines():
            row = json.loads(line)
            raw = base64.b64decode(row["raw_b64"], validate=True)
            if sha256(raw).hexdigest() != row["sha256"]:
                raise AssertionError("G3 raw frame SHA mismatch")
            cls.wire.append({**row, "frame": json.loads(raw)})

    def frame(self, direction: str, kind: str, request_id: str) -> dict:
        found = [row for row in self.wire if row["direction"] == direction and
                 row["frame"].get("kind") == kind and
                 row["frame"].get("requestId") == request_id]
        self.assertEqual(len(found), 1, (direction, kind, request_id))
        return found[0]

    def test_source_wrapper_artifacts_sessions_and_cleanup(self) -> None:
        r, x = self.result, self.runtime
        self.assertEqual(digest(self.runtime_path), r["actual_runtime"]["sha256"])
        for name, item in r["artifact_manifest"].items():
            self.assertEqual(Path(name).stat().st_size, item["bytes"], name)
            self.assertEqual(digest(Path(name)), item["sha256"], name)
        for name, expected in r["source_sha256"].items():
            self.assertEqual(digest(ROOT / name), expected, name)
        for name, expected in x["source_sha256"].items():
            self.assertEqual(digest(Path(name)), expected, name)
        old_path = Path(r["retained_previous_priority_index"]["path"])
        self.assertEqual(digest(old_path), r["retained_previous_priority_index"]["sha256"])
        old = json.loads(old_path.read_text())
        runner = ROOT / "tests/backend/q01_sdk_budget_probe/run_priority_runtime.py"
        self.assertEqual(digest(runner), old["source_sha256"][str(runner)])
        wrapper_path = Path(r["operational_fixture_wrapper"]["path"])
        self.assertEqual(digest(wrapper_path), r["operational_fixture_wrapper"]["sha256"])
        tree = ast.parse(wrapper_path.read_text())
        assignments = [target.attr for node in ast.walk(tree) if isinstance(node, ast.Assign)
                       for target in node.targets if isinstance(target, ast.Attribute)
                       and isinstance(target.value, ast.Name) and target.value.id == "runner"]
        self.assertEqual(sorted(assignments), ["SELECTION_MAX", "fixture"])
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
            self.assertEqual(found[0]["ompSessionId"],
                             x["bridge"]["initial_state"][role]["sessionId"])
        child = next(row for row in x["budget_trace"] if row["event"] == "startup")
        self.assertEqual(child["active"], ["db_summary_save"])
        self.assertEqual(len({hellos["manager"]["ompSessionId"],
                              hellos["worker"]["ompSessionId"], child["childSessionId"]}), 3)
        self.assertIn(DRAFT.encode(), (self.private / "manager/pty-output.raw").read_bytes())
        self.assertEqual(digest(Path(x["processes"]["manager"]["argv"][2])), x["omp_binary_sha256"])
        for role, proc in r["owned_processes"].items():
            self.assertTrue(proc["pump_stopped"])
            with self.assertRaises(ProcessLookupError):
                os.killpg(proc["pgid"], 0)
            self.assertTrue(x["providers"][role]["thread_stopped"])
            self.assertIsNone(x["providers"][role]["violation"])
        self.assertTrue(x["bridge"]["thread_stopped"])

    def test_existing_g3_transport_and_parent_post_checks_on_bound_run(self) -> None:
        generic_priority.PriorityRuntimeIndependentTests.test_raw_g3_notice_frames_due_clock_and_real_write_to_ack_latency(self)
        generic_priority.PriorityRuntimeIndependentTests.test_raw_posts_and_g3_events_show_parent_delivery_during_child_hold(self)

    def test_350_byte_source_near_bound_preflight_typed_and_sqlite(self) -> None:
        x = self.runtime
        trace = x["budget_trace"]
        selection = next(row for row in trace if row["event"] == "selection")
        segments = json.loads((self.private / "segments.json").read_text())
        request = json.loads((self.private / "request.json").read_text())
        self.assertEqual(sum(len(s["text"].encode()) for s in segments), 1_048_576)
        self.assertEqual((selection["collectedBytes"], selection["selectionMax"],
                          selection["selectedBytes"]), (1_048_576, 650, 350))
        self.assertEqual(len(selection["selected"]), 1)
        chosen = selection["selected"][0]
        source = next(s for s in segments if s["source"] == chosen["source"])
        self.assertEqual(chosen["textBytes"], len(source["text"].encode()))
        self.assertEqual(chosen["textSha"], sha256(source["text"].encode()).hexdigest())
        self.assertEqual((chosen["start"], chosen["end"]), (0, 350))
        self.assertEqual((request["sources"][0]["start"], request["sources"][0]["end"]),
                         (0, 350))
        self.assertEqual(request["sources"][0]["digest"], chosen["textSha"])
        self.assertEqual(request["requestId"], x["request_id"])
        self.assertEqual(request["skillDigest"], digest(ROOT / "tests/backend/q01_sdk_private_probe/summary-skill.txt"))
        # The wrapper's one-source fixture fits; adding the next candidate crosses its cap.
        one = len(json.dumps({"selected": segments[:1]}, separators=(",", ":")))
        two = len(json.dumps({"selected": segments[:2]}, separators=(",", ":")))
        self.assertLessEqual(one, 650)
        self.assertGreater(two, 650)
        preflight = [row for row in trace if row["event"] == "dispatch_preflight"]
        self.assertEqual([p["contextBytes"] for p in preflight], [2440, 3248])
        self.assertEqual([p["inputLimit"] - p["contextBytes"] for p in preflight], [836, 28])
        for p in preflight:
            self.assertTrue(p["admitted"])
            self.assertEqual(p["names"], ["db_summary_save"])
            self.assertEqual((p["inputLimit"], p["outputCap"], p["fittedCap"]),
                             (3276, 8192, 8192))
            self.assertFalse(p["nativeDiagnostic"]["exact"])
        typed_path = self.private / "typed-result.json"
        typed = json.loads(typed_path.read_text())
        self.assertEqual(typed_path.stat().st_size, 710)
        for key in ("requestId", "inputKey", "skillDigest", "sources"):
            self.assertEqual(typed[key], request[key])
        self.assertEqual(typed["facts"], [SUMMARY])
        self.assertEqual(digest(typed_path), next(r for r in trace if r["event"] == "saved")["resultSha"])
        self.assertEqual(next(r for r in trace if r["event"] == "execute")["params"],
                         {"summary": SUMMARY})
        self.assertEqual([r["event"] for r in trace][-2:], ["done", "disposed"])
        with sqlite3.connect(f"file:{self.private / 'summary.sqlite3'}?mode=ro", uri=True) as db:
            self.assertEqual(db.execute("SELECT id,summary FROM summaries").fetchall(), [(1, SUMMARY)])
        with sqlite3.connect(f"file:{self.private / 'backend.sqlite3'}?mode=ro", uri=True) as db:
            stored = db.execute("SELECT request_json FROM summary_requests_v1").fetchall()
            states = db.execute("SELECT state FROM summary_request_events_v1 ORDER BY event_id").fetchall()
            receipts = db.execute("SELECT request_id,result_digest FROM summary_result_receipts_v1").fetchall()
        self.assertEqual(len(stored), 1)
        self.assertEqual(json.loads(stored[0][0]), request)
        self.assertEqual(states, [("prepared",), ("sending",), ("stored",)])
        canonical = json.dumps(typed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        self.assertEqual(receipts, [(request["requestId"], sha256(canonical).hexdigest())])


if __name__ == "__main__":
    unittest.main()
