"""Independent, truthful negative witness for the local SDK guard matrix.

Set Q01_SDK_MATRIX_EVIDENCE to retained actual OMP runtime-evidence.json.
Passing tests mean the observed fault is correctly described, not Q01 pass.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unittest


HERE = Path(__file__).resolve().parent
MATRIX = HERE / "q01_sdk_guard_matrix_probe"
BODY = HERE / "q01_sdk_private_probe/agent-body.txt"
SKILL = HERE / "q01_sdk_private_probe/summary-skill.txt"
WRITER = HERE / "q01_script_only_probe/fixed_writer.py"
BLOCKED = {
    "missing_guard": "missing_guard",
    "noop_guard": "noop_readback",
    "api_shape_absent": "method_shape",
    "stale": "stale_or_wrong_session",
    "wrong_session": "stale_or_wrong_session",
    "disposed": "missing_receipt_or_disposed",
    "roster_change": "roster_changed",
    "schema_change": "schema_changed",
    "policy_change": "policy_changed",
}
ALL_CASES = ("normal", *BLOCKED, "bash", "checksend")
SUMMARY = "Literal command and SQL: touch /tmp/NO_EXECUTE; DROP TABLE summaries; --"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _events(trace: list[dict], case: str, event: str) -> list[dict]:
    return [x for x in trace if x.get("case") == case and x.get("event") == event]


def _one(trace: list[dict], case: str, event: str) -> dict:
    found = _events(trace, case, event)
    assert len(found) == 1, (case, event, len(found))
    return found[0]


def _order(trace: list[dict], case: str, *events: str) -> None:
    indexes = [trace.index(_one(trace, case, event)) for event in events]
    assert indexes == sorted(indexes) and len(indexes) == len(set(indexes)), (case, events, indexes)


def _source_bound(d: dict) -> None:
    expected = {
        str(MATRIX / "matrix_extension.ts"): MATRIX / "matrix_extension.ts",
        str(MATRIX / "run_matrix.py"): MATRIX / "run_matrix.py",
        str(BODY): BODY,
        str(SKILL): SKILL,
        str(WRITER): WRITER,
    }
    assert set(d["source_sha"]) == set(expected)
    for path, source in expected.items():
        assert _sha(source.read_bytes()) == d["source_sha"][path], path
    argv = d["argv"]
    assert argv[argv.index("--extension") + 1] == str(MATRIX / "matrix_extension.ts")
    assert "--no-tools" in argv and "--mode" in argv and argv[argv.index("--mode") + 1] == "rpc"
    assert _sha(Path(argv[0]).read_bytes()) == d["omp_binary_sha"]


def _custody(d: dict, artifact: Path) -> None:
    _source_bound(d)
    trace = d["trace"]
    private = artifact.parent / "private"
    assert [json.loads(line) for line in (private / "trace.jsonl").read_text().splitlines()] == trace
    assert _sha((private / "fixed_writer.py").read_bytes()) == d["source_sha"][str(WRITER)]
    with sqlite3.connect(f"file:{private / 'summary.sqlite3'}?mode=ro", uri=True) as conn:
        rows = [list(row) for row in conn.execute("SELECT id, summary FROM summaries ORDER BY id")]
    assert rows == d["db_rows"] == [[1, SUMMARY]]
    assert d["sentinels"] == {"command-sentinel": "ORIGINAL", "file-sentinel": "ORIGINAL"}
    for name, value in d["sentinels"].items():
        assert (private / "cwd" / name).read_text() == value
    assert d["permits_remaining"] == [] and list((private / "permits").iterdir()) == []
    assert d["stop_reason"] is None
    assert d["rpc_sent"] == [{"id": "matrix-parent-state", "type": "get_state"}]
    assert any(x.get("id") == "matrix-parent-state" and x.get("type") == "response"
               and x.get("error") is None for x in d["rpc_frames"])
    proc = d["process"]
    assert proc["pid"] > 0 and proc["returncode"] is not None
    assert proc["pump_stopped"] is True and proc["remaining_owned"] == []
    assert all(e["pid"] == proc["pid"] for e in trace)
    assert trace[0]["event"] == "begin" and trace[-1]["event"] == "done"
    assert trace[-1]["ok"] is True
    assert trace[0]["parentBefore"] == trace[-1]["parentBefore"] == trace[-1]["parentAfter"]
    try:
        os.killpg(proc["pid"], 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("retained OMP process group still exists")


def _common_observations(d: dict) -> None:
    trace = d["trace"]
    posts = d["provider_posts"]
    outbound = d["provider_outbound"]
    assert len([e for e in trace if e["event"] == "initial_check"]) == len(ALL_CASES)
    assert len({_one(trace, c, "initial_check")["sessionId"] for c in ALL_CASES}) == len(ALL_CASES)
    roster_sha = _sha(b"db_summary_save")
    for case in ALL_CASES:
        initial = _one(trace, case, "initial_check")
        receipt = initial["receipt"]
        assert initial["active"] == ["db_summary_save"]
        assert initial["privateHas"] is True and initial["globalHas"] is False
        assert receipt["requestId"] == f"q01-{case}" and receipt["sessionId"] == initial["sessionId"]
        assert receipt["rosterSha"] == roster_sha
        assert initial["sessionId"] and receipt["nonce"]
        _one(trace, case, "final_gate")
        if case != "disposed":
            assert len(_events(trace, case, "disposed")) == 1
    assert len(posts) == len(outbound) == 4
    assert [p["case"] for p in posts] == [x["case"] for x in outbound]
    assert Counter(p["case"] for p in posts) == {"normal": 2, "bash": 1, "checksend": 1}
    expected_system = BODY.read_bytes() + b"\n\n" + SKILL.read_bytes()
    begin = trace[0]
    assert begin["bodySha"] == _sha(BODY.read_bytes())
    assert begin["skillSha"] == _sha(SKILL.read_bytes())
    for post in posts:
        assert post["path"] == "/v1/chat/completions"
        assert post["permit"] is True
        assert post["roles"][:2] == ["system", "user"]
        assert post["system_sha"] == _sha(expected_system)
        assert post["model"] == "scripted"


def _blocked_nine(d: dict) -> None:
    trace = d["trace"]
    posts = d["provider_posts"]
    for case, reason in BLOCKED.items():
        gate = _one(trace, case, "final_gate")
        assert gate["allowed"] is False and gate["reason"] == reason
        assert _one(trace, case, "admission_rejected")["reason"] == reason
        _order(trace, case, "initial_check", "final_gate", "admission_rejected")
        assert not _events(trace, case, "prompt_start")
        assert not _events(trace, case, "execute_received")
        assert not _events(trace, case, "writer_invoked")
        assert not any(p["case"] == case for p in posts)
    assert len(BLOCKED) == 9
    assert _one(trace, "disposed", "disposed_before_gate")
    assert _one(trace, "disposed", "final_gate")["active"] is None
    assert _one(trace, "roster_change", "final_gate")["active"] == []


def _normal_and_bash(d: dict) -> None:
    trace = d["trace"]
    posts = d["provider_posts"]
    outbound = d["provider_outbound"]
    assert _one(trace, "normal", "final_gate")["allowed"] is True
    assert _one(trace, "normal", "prompt_start")["active"] == ["db_summary_save"]
    assert [p["tools"] for p in posts if p["case"] == "normal"] == [["db_summary_save"]] * 2
    call = next(x["call"] for x in outbound if x["case"] == "normal" and x.get("call"))
    assert call["function"]["name"] == "db_summary_save"
    assert json.loads(call["function"]["arguments"]) == {"summary": SUMMARY}
    received = _one(trace, "normal", "execute_received")
    assert received["callId"] == call["id"] and received["params"] == {"summary": SUMMARY}
    assert received["armed"] is True and received["spent"] is False
    writer = _one(trace, "normal", "writer_invoked")
    assert Path(writer["argv"][0]).name == "fixed_writer.py"
    assert json.loads(writer["argv"][1]) == {"summary": SUMMARY}
    assert 0 < _one(trace, "normal", "saved")["receiptBytes"] <= 8192
    tool = _one(trace, "normal", "tool_end")
    assert tool["toolName"] == "db_summary_save" and tool["isError"] is False
    assert json.loads(tool["result"]) == {"request_id": "q01-normal",
                                            "session_id": _one(trace, "normal", "initial_check")["sessionId"],
                                            "saved": True}
    assert _one(trace, "normal", "prompt_done")["saved"] is True
    _one(trace, "normal", "result_accepted")
    assert len([e for e in trace if e["event"] == "writer_invoked"]) == 1
    assert d["db_rows"] == [[1, SUMMARY]]

    assert _one(trace, "bash", "final_gate")["allowed"] is True
    assert [p["tools"] for p in posts if p["case"] == "bash"] == [["db_summary_save"]]
    bash_call = next(x["call"] for x in outbound if x["case"] == "bash" and x.get("call"))
    assert bash_call["function"]["name"] == "bash"
    tool = _one(trace, "bash", "tool_end")
    assert tool["toolName"] == "bash" and tool["isError"] is True
    assert tool["result"] == "Tool bash not found"
    _order(trace, "bash", "prompt_start", "abort_requested", "tool_end", "abort_completed",
           "prompt_done", "result_rejected")
    assert _one(trace, "bash", "result_rejected")["reason"] == "forbidden_tool"
    assert not _events(trace, "bash", "writer_invoked")
    assert not _events(trace, "bash", "execute_received")
    assert not _events(trace, "bash", "abort_failed")
    assert _one(trace, "bash", "prompt_done")["saved"] is False


def _failed_checksend(d: dict) -> None:
    trace = d["trace"]
    posts = d["provider_posts"]
    outbound = d["provider_outbound"]
    gate = _one(trace, "checksend", "final_gate")
    assert gate["allowed"] is True and gate["active"] == ["db_summary_save"]
    fault = _one(trace, "checksend", "fault_after_gate_before_prompt")
    assert fault["kind"] == "public_setActiveToolsByName_empty"
    assert _one(trace, "checksend", "fault_applied")["active"] == []
    assert _one(trace, "checksend", "prompt_start")["active"] == []
    _order(trace, "checksend", "final_gate", "fault_after_gate_before_prompt",
           "fault_applied", "prompt_start", "prompt_done", "result_rejected")
    leaked_posts = [p for p in posts if p["case"] == "checksend"]
    assert len(leaked_posts) == 1 and leaked_posts[0]["tools"] == []
    assert leaked_posts[0]["permit"] is True
    assert [x["call"] for x in outbound if x["case"] == "checksend"] == [None]
    done = _one(trace, "checksend", "prompt_done")
    assert done["saved"] is False and done["spent"] is False and done["active"] == []
    assert _one(trace, "checksend", "result_rejected")["reason"] == "checksend_fault"
    assert not _events(trace, "checksend", "writer_invoked")
    assert not _events(trace, "checksend", "result_accepted")
    assert d["db_rows"] == [[1, SUMMARY]]
    # The sole bad POST is itself the Q01 zero-send failure. A rejected result
    # and unchanged database do not turn it into a passed no-send gate.


class MatrixIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_MATRIX_EVIDENCE")
        if not supplied:
            raise unittest.SkipTest("Q01_SDK_MATRIX_EVIDENCE required; skipped tests are not a gate pass")
        cls.artifact = Path(supplied).resolve()
        cls.evidence = json.loads(cls.artifact.read_text())

    def test_source_raw_trace_sqlite_and_cleanup(self) -> None:
        _custody(self.evidence, self.artifact)
        _common_observations(self.evidence)

    def test_nine_owner_guards_prompt_zero_post_zero(self) -> None:
        _blocked_nine(self.evidence)

    def test_normal_single_save_and_bash_abort(self) -> None:
        _normal_and_bash(self.evidence)

    def test_post_gate_fault_is_a_real_zero_send_failure(self) -> None:
        _failed_checksend(self.evidence)

    def test_corrupt_fault_post_cannot_disappear(self) -> None:
        bad = deepcopy(self.evidence)
        bad["provider_posts"] = [p for p in bad["provider_posts"] if p["case"] != "checksend"]
        with self.assertRaises(AssertionError):
            _failed_checksend(bad)

    def test_corrupt_gate_to_fault_order_is_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        gate = _one(bad["trace"], "checksend", "final_gate")
        fault = _one(bad["trace"], "checksend", "fault_after_gate_before_prompt")
        i, j = bad["trace"].index(gate), bad["trace"].index(fault)
        bad["trace"][i], bad["trace"][j] = bad["trace"][j], bad["trace"][i]
        with self.assertRaises(AssertionError):
            _failed_checksend(bad)

    def test_corrupt_fault_result_rejection_is_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        bad["trace"] = [e for e in bad["trace"] if not
                        (e.get("case") == "checksend" and e.get("event") == "result_rejected")]
        with self.assertRaises(AssertionError):
            _failed_checksend(bad)

    def test_corrupt_source_hash_is_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        bad["source_sha"][str(MATRIX / "matrix_extension.ts")] = "0" * 64
        with self.assertRaises(AssertionError):
            _source_bound(bad)


if __name__ == "__main__":
    unittest.main()
