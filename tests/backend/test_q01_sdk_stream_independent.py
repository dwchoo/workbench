"""Independent observations of the installed OMP stream guard probe.

These tests verify both the local preventive observation and required failed
missing/noop controls. They do not certify Q01. Set Q01_SDK_STREAM_EVIDENCE.
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
PROBE = HERE / "q01_sdk_stream_guard_probe"
BODY = HERE / "q01_sdk_private_probe/agent-body.txt"
SKILL = HERE / "q01_sdk_private_probe/summary-skill.txt"
WRITER = HERE / "q01_script_only_probe/fixed_writer.py"
CASES = ("normal", "retry", "checksend", "stale", "schema", "policy", "model",
         "missing", "noop", "bash")
GUARDED = {"checksend": "roster_mismatch", "stale": "stale_binding",
           "schema": "schema_mismatch", "policy": "policy_mismatch", "model": "model_mismatch"}
SUMMARY = "Literal command and SQL: touch /tmp/NO_EXECUTE; DROP TABLE summaries; --"
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string", "minLength": 1,
                                                      "maxLength": 2000}},
          "required": ["summary"], "additionalProperties": True}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stable_sha(value: object) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _events(trace: list[dict], case: str, event: str) -> list[dict]:
    return [e for e in trace if e.get("case") == case and e.get("event") == event]


def _one(trace: list[dict], case: str, event: str) -> dict:
    found = _events(trace, case, event)
    assert len(found) == 1, (case, event, len(found))
    return found[0]


def _order(trace: list[dict], case: str, *events: str) -> None:
    positions = [trace.index(_one(trace, case, name)) for name in events]
    assert positions == sorted(set(positions)), (case, events, positions)


def _source_and_custody(d: dict, artifact: Path) -> None:
    expected = {str(PROBE / "stream_extension.ts"): PROBE / "stream_extension.ts",
                str(PROBE / "run_stream.py"): PROBE / "run_stream.py",
                str(BODY): BODY, str(SKILL): SKILL, str(WRITER): WRITER}
    assert set(d["source_sha"]) == set(expected)
    for name, path in expected.items():
        assert d["source_sha"][name] == _sha(path.read_bytes()), name
    argv = d["argv"]
    assert argv[argv.index("--extension") + 1] == str(PROBE / "stream_extension.ts")
    assert "--no-tools" in argv and argv[argv.index("--mode") + 1] == "rpc"
    assert d["omp_binary_sha"] == _sha(Path(argv[0]).read_bytes())
    private = artifact.parent / "private"
    trace = [json.loads(line) for line in (private / "trace.jsonl").read_text().splitlines()]
    assert trace == d["trace"]
    assert _sha((private / "fixed_writer.py").read_bytes()) == d["source_sha"][str(WRITER)]
    with sqlite3.connect(f"file:{private / 'summary.sqlite3'}?mode=ro", uri=True) as db:
        rows = [list(r) for r in db.execute("SELECT id, summary FROM summaries ORDER BY id")]
    assert rows == d["db_rows"] == [[1, SUMMARY], [2, SUMMARY]]
    assert d["sentinels"] == {"command-sentinel": "ORIGINAL", "file-sentinel": "ORIGINAL"}
    assert all((private / "cwd" / k).read_text() == v for k, v in d["sentinels"].items())
    assert d["permits_remaining"] == [] and list((private / "permits").iterdir()) == []
    assert d["stop_reason"] is None
    assert d["rpc_sent"] == [{"id": "stream-parent-state", "type": "get_state"}]
    assert any(f.get("id") == "stream-parent-state" and f.get("type") == "response"
               and f.get("error") is None for f in d["rpc_frames"])
    pid = d["process"]["pid"]
    assert pid > 0 and d["process"]["returncode"] is not None
    assert d["process"]["pump_stopped"] is True and d["process"]["remaining_owned"] == []
    assert all(e.get("pid") == pid for e in d["trace"])
    assert d["trace"][0]["event"] == "begin" and d["trace"][-1]["event"] == "done"
    assert d["trace"][-1]["ok"] is True
    assert d["trace"][0]["parentBefore"] == d["trace"][-1]["parentBefore"] == d["trace"][-1]["parentAfter"]
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("owned OMP process group remains")


def _request_rosters(d: dict) -> None:
    trace, posts = d["trace"], d["provider_posts"]
    assert len([e for e in trace if e["event"] == "initial_check"]) == len(CASES)
    assert len({_one(trace, c, "initial_check")["sessionId"] for c in CASES}) == len(CASES)
    assert Counter(p["case"] for p in posts) == {
        "normal": 2, "retry": 3, "missing": 1, "noop": 1, "bash": 1}
    assert len(posts) == len(d["provider_outbound"]) == 8
    assert [p["case"] for p in posts] == [x["case"] for x in d["provider_outbound"]]
    expected_system = BODY.read_bytes() + b"\n\n" + SKILL.read_bytes()
    assert trace[0]["bodySha"] == _sha(BODY.read_bytes())
    assert trace[0]["skillSha"] == _sha(SKILL.read_bytes())
    assert trace[0]["rawSchemaSha"] == _stable_sha(SCHEMA)
    for case in CASES:
        initial = _one(trace, case, "initial_check")
        assert initial["active"] == ["db_summary_save"]
        assert initial["privateHas"] is True and initial["globalHas"] is False
        receipt = initial["receipt"]
        assert receipt["requestId"] == f"q01-{case}" and receipt["sessionId"] == initial["sessionId"]
        assert receipt["rosterSha"] == _sha(b"db_summary_save")
        assert receipt["nonce"] and receipt["schemaSha"] and receipt["policySha"]
        assert len(_events(trace, case, "disposed")) == 1
        assert _one(trace, case, "final_gate")["allowed"] is True
    for post in posts:
        assert post["path"] == "/v1/chat/completions" and post["permit"] is True
        assert post["roles"][:2] == ["system", "user"]
        assert post["model"] == "scripted" and post["system_sha"] == _sha(expected_system)
        assert post["request_sha"] and len(post["request_sha"]) == 64
        if post["case"] in {"missing", "noop"}:
            assert post["tools"] == []
            assert post["tools_field_present"] is False
            assert post["tools_field_type"] == "absent" and post["tools_raw"] is None
        else:
            assert post["tools"] == ["db_summary_save"]
            assert post["tools_field_present"] is True
            assert post["tools_field_type"] == "list"
            assert len(post["tools_raw"]) == 1
            assert post["tools_raw"][0]["function"]["name"] == "db_summary_save"
            assert post["tools_raw"][0]["function"]["parameters"] == SCHEMA


def _positive_and_retry(d: dict) -> None:
    trace, posts, outbound = d["trace"], d["provider_posts"], d["provider_outbound"]
    for case in ("normal", "retry"):
        guards = _events(trace, case, "stream_guard")
        originals = _events(trace, case, "original_stream_called")
        assert len(guards) == len(originals) == 2
        assert [g["call"] for g in guards] == [1, 2]
        assert all(g["names"] == ["db_summary_save"] and g.get("reason") is None for g in guards)
        assert all(g["schemaSha"] == g["expectedSchemaSha"] == _stable_sha(SCHEMA) for g in guards)
        call = next(x["call"] for x in outbound if x["case"] == case and x.get("call"))
        assert call["function"]["name"] == "db_summary_save"
        assert json.loads(call["function"]["arguments"]) == {"summary": SUMMARY}
        received = _one(trace, case, "execute_received")
        assert received["callId"] == call["id"] and received["params"] == {"summary": SUMMARY}
        assert received["armed"] is True and received["spent"] is False and received["protected"] is False
        writer = _one(trace, case, "writer_invoked")
        assert Path(writer["argv"][0]).name == "fixed_writer.py"
        assert json.loads(writer["argv"][1]) == {"summary": SUMMARY}
        assert 0 < _one(trace, case, "saved")["receiptBytes"] <= 8192
        end = _one(trace, case, "tool_end")
        assert end["toolName"] == "db_summary_save" and end["isError"] is False
        assert json.loads(end["result"]) == {"request_id": f"q01-{case}",
                                               "session_id": _one(trace, case, "initial_check")["sessionId"],
                                               "saved": True}
        assert _one(trace, case, "prompt_done")["saved"] is True
        _one(trace, case, "result_accepted")
        assert not _events(trace, case, "result_rejected")
    assert len(_events(trace, "normal", "original_stream_called")) == 2
    retry = [p for p in posts if p["case"] == "retry"]
    assert len(retry) == 3 and retry[0]["request_sha"] == retry[1]["request_sha"]
    retry_outbound = [x for x in outbound if x["case"] == "retry"]
    assert retry_outbound[0] == {"case": "retry", "kind": "retryable_503"}
    assert retry_outbound[1]["call"]["function"]["name"] == "db_summary_save"
    assert len([e for e in trace if e["event"] == "writer_invoked"]) == 2
    assert d["db_rows"] == [[1, SUMMARY], [2, SUMMARY]]


def _guarded_and_leaky(d: dict) -> None:
    trace, posts = d["trace"], d["provider_posts"]
    for case, reason in GUARDED.items():
        guard = _one(trace, case, "stream_guard")
        assert guard["reason"] == reason
        assert _one(trace, case, "prompt_done")["protected"] is True
        assert _one(trace, case, "prompt_done")["saved"] is False
        assert _one(trace, case, "result_rejected")
        assert not _events(trace, case, "original_stream_called")
        assert not _events(trace, case, "writer_invoked")
        assert not any(p["case"] == case for p in posts)
        _order(trace, case, "final_gate", "prompt_start", "stream_guard", "prompt_done", "result_rejected")
    _order(trace, "checksend", "final_gate", "fault_after_gate_before_prompt", "fault_applied",
           "prompt_start", "stream_guard", "prompt_done")
    assert _one(trace, "checksend", "fault_applied")["active"] == []
    assert _one(trace, "checksend", "stream_guard")["names"] == []
    assert _one(trace, "schema", "stream_guard")["schemaSha"] != _stable_sha(SCHEMA)
    assert _one(trace, "model", "controlled_model_change")["modelKey"] != \
        _one(trace, "model", "initial_check")["receipt"]["modelKey"]
    for case in ("missing", "noop"):
        assert len([p for p in posts if p["case"] == case]) == 1
        assert not _events(trace, case, "stream_guard")
        assert _one(trace, case, "fault_applied")["active"] == []
        _order(trace, case, "final_gate", "fault_after_gate_before_prompt", "fault_applied",
               "prompt_start", "prompt_done", "result_rejected")
        done = _one(trace, case, "prompt_done")
        assert done["saved"] is False and done["spent"] is False
        assert not _events(trace, case, "writer_invoked")
        assert not _events(trace, case, "result_accepted")
    assert _one(trace, "missing", "wrapper_absent_control")
    missing_done = _one(trace, "missing", "prompt_done")
    assert missing_done["originalCalls"] is None and missing_done["wrapperCalls"] is None
    assert missing_done["streamCounter"] == "uninstrumented"
    assert _one(trace, "noop", "noop_wrapper")["tools"] == []
    noop_done = _one(trace, "noop", "prompt_done")
    assert noop_done["originalCalls"] == noop_done["wrapperCalls"] == 1
    assert noop_done["streamCounter"] == "noop_delegate"
    assert d["seams"]["noop"]["original_stream_calls"] == 1
    assert d["seams"]["noop"]["stream_counter"] == "noop_delegate"
    assert d["seams"]["missing"]["original_stream_calls"] is None
    assert d["seams"]["missing"]["stream_counter"] == "uninstrumented"
    assert d["seams"]["noop"]["provider_posts"] == 1
    assert d["seams"]["missing"]["provider_posts"] == 1


def _bash(d: dict) -> None:
    trace, posts, outbound = d["trace"], d["provider_posts"], d["provider_outbound"]
    assert len([p for p in posts if p["case"] == "bash"]) == 1
    call = next(x["call"] for x in outbound if x["case"] == "bash" and x.get("call"))
    assert call["function"]["name"] == "bash"
    end = _one(trace, "bash", "tool_end")
    assert end["toolName"] == "bash" and end["isError"] is True
    assert end["result"] == "Tool bash not found"
    assert len(_events(trace, "bash", "stream_guard")) == 2
    assert _events(trace, "bash", "stream_guard")[1]["reason"] == "already_protected"
    assert len(_events(trace, "bash", "original_stream_called")) == 1
    _order(trace, "bash", "prompt_start", "abort_requested", "tool_end", "abort_completed",
           "prompt_done", "result_rejected")
    assert not _events(trace, "bash", "abort_failed")
    assert not _events(trace, "bash", "writer_invoked")
    assert _one(trace, "bash", "prompt_done")["protected"] is True


class StreamIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_STREAM_EVIDENCE")
        if not supplied:
            raise unittest.SkipTest("Q01_SDK_STREAM_EVIDENCE required; skip is not Q01 pass")
        cls.artifact = Path(supplied).resolve()
        cls.evidence = json.loads(cls.artifact.read_text())

    def test_source_binary_raw_trace_sqlite_and_cleanup(self) -> None:
        _source_and_custody(self.evidence, self.artifact)

    def test_provider_rosters_system_and_raw_tools_presence(self) -> None:
        _request_rosters(self.evidence)

    def test_normal_and_retry_only_two_fixed_db_rows(self) -> None:
        _positive_and_retry(self.evidence)

    def test_guarded_post_zero_and_missing_noop_post_one_required_failure(self) -> None:
        _guarded_and_leaky(self.evidence)

    def test_forbidden_tool_abort_then_no_extra_post(self) -> None:
        _bash(self.evidence)

    def test_corrupt_missing_post_deleted_is_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        bad["provider_posts"] = [p for p in bad["provider_posts"] if p["case"] != "missing"]
        with self.assertRaises(AssertionError):
            _guarded_and_leaky(bad)

    def test_corrupt_absent_tools_relabelled_empty_array_is_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        for p in bad["provider_posts"]:
            if p["case"] == "noop":
                p.update(tools_field_present=True, tools_field_type="list", tools_raw=[])
        with self.assertRaises(AssertionError):
            _request_rosters(bad)

    def test_corrupt_checksend_order_is_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        a = _one(bad["trace"], "checksend", "final_gate")
        b = _one(bad["trace"], "checksend", "fault_after_gate_before_prompt")
        i, j = bad["trace"].index(a), bad["trace"].index(b)
        bad["trace"][i], bad["trace"][j] = bad["trace"][j], bad["trace"][i]
        with self.assertRaises(AssertionError):
            _guarded_and_leaky(bad)

    def test_corrupt_source_hash_is_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        bad["source_sha"][str(PROBE / "stream_extension.ts")] = "0" * 64
        with self.assertRaises(AssertionError):
            _source_and_custody(bad, self.artifact)

    def test_counter_correction_cannot_relabel_missing_or_noop(self) -> None:
        for case, false_count in (("missing", 0), ("noop", 0)):
            with self.subTest(case=case):
                bad = deepcopy(self.evidence)
                bad["seams"][case]["original_stream_calls"] = false_count
                with self.assertRaises(AssertionError):
                    _guarded_and_leaky(bad)


if __name__ == "__main__":
    unittest.main()
