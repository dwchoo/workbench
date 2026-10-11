"""Independent observations of the local private SDK session proof.

Q01_SDK_PRIVATE_EVIDENCE must name a retained actual OMP runtime-evidence.json.
No OMP or provider is launched by these tests.
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
FIXTURES = HERE / "q01_sdk_private_probe"
WRITER = HERE / "q01_script_only_probe" / "fixed_writer.py"
POSITIVE = ("p1", "p2", "switch", "retry")
DENIED = ("missing", "expand", "stale")
FAILED_SAVE = ("invalid", "writer_fail")
FORBIDDEN = ("bash", "think")
ALL_CASES = POSITIVE + DENIED + FAILED_SAVE + FORBIDDEN + ("noop",)
SUMMARY = "Literal log: touch /tmp/NO_EXECUTE; DROP TABLE summaries; -- kept as data"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _events(trace: list[dict], case: str, event: str) -> list[dict]:
    return [x for x in trace if x.get("case") == case and x.get("event") == event]


def _one(trace: list[dict], case: str, event: str) -> dict:
    matches = _events(trace, case, event)
    assert len(matches) == 1, (case, event, len(matches))
    return matches[0]


def _assert_source_binding(d: dict) -> None:
    expected = {
        "private_extension.ts": FIXTURES / "private_extension.ts",
        "run_private_probe.py": FIXTURES / "run_private_probe.py",
        "agent-body.txt": FIXTURES / "agent-body.txt",
        "summary-skill.txt": FIXTURES / "summary-skill.txt",
        "fixed_writer.py": WRITER,
    }
    sources = d["source_sha"]
    assert len(sources) == len(expected), sources
    for reported_path, reported_hash in sources.items():
        name = Path(reported_path).name
        assert name in expected, name
        path = expected[name]
        assert Path(reported_path).resolve() == path.resolve(), (reported_path, path)
        assert _sha(path.read_bytes()) == reported_hash, name
    argv = d["argv"]
    assert "--extension" in argv
    assert argv[argv.index("--extension") + 1] == str(expected["private_extension.ts"])
    assert _sha(Path(argv[0]).read_bytes()) == d["omp_binary_sha"]


def _assert_runtime(d: dict, artifact: Path, *, external: bool = True) -> None:
    """Check observed effect and event order; deliberately ignore d['checks']."""
    _assert_source_binding(d)
    trace = d["trace"]
    posts = d["provider_posts"]
    outbound = d["provider_outbound"]
    assert d["stop_reason"] is None
    assert d["rpc_sent"] == [{"id": "private-parent-state", "type": "get_state"}]
    assert any(f.get("id") == "private-parent-state" and f.get("type") == "response"
               and f.get("error") is None for f in d["rpc_frames"])
    pid = d["process"]["pid"]
    assert pid > 0 and all(e.get("pid") == pid for e in trace)
    assert trace[0]["event"] == "begin" and trace[-1]["event"] == "done"
    assert trace[-1]["ok"] is True
    assert trace[0]["parentBefore"] == trace[-1]["parentBefore"] == trace[-1]["parentAfter"]
    assert all(_one(trace, c, "readback")["parentEntries"] == trace[0]["parentBefore"]
               for c in ALL_CASES)
    assert d["process"]["pump_stopped"] is True
    assert d["process"]["remaining_owned"] == []
    assert d["process"]["returncode"] is not None
    assert d["sentinels"] == {"command-sentinel": "ORIGINAL", "file-sentinel": "ORIGINAL"}
    assert d["permits_remaining"] == []

    body = (FIXTURES / "agent-body.txt").read_bytes()
    skill = (FIXTURES / "summary-skill.txt").read_bytes()
    expected_system = (body + b"\n\n" + skill).decode("utf8")
    begin = trace[0]
    assert (begin["bodySha"], begin["bodyBytes"]) == (_sha(body), len(body))
    assert (begin["skillSha"], begin["skillBytes"]) == (_sha(skill), len(skill))
    assert Counter(p["case"] for p in posts) == {
        "p1": 2, "p2": 2, "switch": 2, "invalid": 2,
        "writer_fail": 2, "bash": 1, "think": 1, "retry": 3, "noop": 1,
    }
    assert len(posts) == len(outbound) == 16
    assert [p["case"] for p in posts] == [x["case"] for x in outbound]
    for post in posts:
        assert post["path"] == post["api_path"] == "/v1/chat/completions"
        assert post["tools"] == ["db_summary_save"]
        assert post["pre_prompt"] is False
        assert post["system_messages"] == [expected_system]
        assert post["system_bytes"] == len(expected_system.encode("utf8"))
        assert post["system_sha"] == _sha(expected_system.encode("utf8"))
        assert post["roles"][:2] == ["system", "user"]
    for case in ("p1", "p2"):
        assert len([p for p in posts if p["case"] == case and
                    p["system_messages"] == [expected_system]]) == 2

    assert len([e for e in trace if e.get("event") == "readback"]) == len(ALL_CASES)
    assert len({_one(trace, c, "readback")["sessionId"] for c in ALL_CASES}) == len(ALL_CASES)
    digest = _sha(b"db_summary_save")
    for case in ALL_CASES:
        r = _one(trace, case, "readback")
        assert r["privateHas"] is True and r["globalHas"] is False
        assert r["expectedDigest"] == digest
        assert len(_events(trace, case, "disposed")) == 1
    for case in POSITIVE + FAILED_SAVE + FORBIDDEN + ("noop",):
        r = _one(trace, case, "readback")
        assert r["active"] == ["db_summary_save"] and r["rosterDigest"] == digest
        assert _one(trace, case, "prompt_start")["active"] == ["db_summary_save"]
        assert _one(trace, case, "prompt_done")["active"] == ["db_summary_save"]
    assert _one(trace, "switch", "model_switch")["active"] == ["db_summary_save"]
    assert _one(trace, "expand", "model_switch")["active"] == ["db_summary_save", "think"]
    for case, roster, reason in (("missing", [], "roster_mismatch"),
                                 ("expand", ["db_summary_save", "think"], "roster_mismatch"),
                                 ("stale", ["db_summary_save"], "stale_receipt")):
        assert _one(trace, case, "readback")["active"] == roster
        assert _one(trace, case, "admission_rejected")["reason"] == reason
        assert not _events(trace, case, "prompt_start")
        assert not _events(trace, case, "writer_invoked")
        assert not any(p["case"] == case for p in posts)
    assert _one(trace, "stale", "readback")["sessionId"] != _one(trace, "p1", "readback")["sessionId"]

    for case in POSITIVE:
        e = _one(trace, case, "execute_received")
        assert e["armed"] is True and e["spent"] is False
        assert e["params"] == {"summary": SUMMARY}
        assert e["callId"] == f"private-{case}-1"
        writer = _one(trace, case, "writer_invoked")
        assert Path(writer["argv"][0]).name == "fixed_writer.py"
        assert json.loads(writer["argv"][1]) == {"summary": SUMMARY}
        assert 0 < _one(trace, case, "saved")["receiptBytes"] <= 8192
        receipt = json.loads(next(x["result"] for x in _events(trace, case, "child_event")
                                  if x.get("type") == "tool_execution_end"))
        assert receipt == {"request_id": f"q01-{case}",
                           "session_id": _one(trace, case, "readback")["sessionId"],
                           "epoch": f"epoch-{case}", "saved": True}
        done = _one(trace, case, "prompt_done")
        assert done["spent"] is True and isinstance(done.get("savedReceipt"), str)
        assert json.loads(done["savedReceipt"]) == receipt
        accepted = _one(trace, case, "result_accepted")
        assert isinstance(accepted.get("verifiedReceipt"), str)
        assert json.loads(accepted["verifiedReceipt"]) == receipt
        assert not _events(trace, case, "result_rejected")
    assert d["db_rows"] == [[n, SUMMARY] for n in range(1, 5)]
    retry = [x for x in outbound if x["case"] == "retry"]
    assert len(retry) == 3 and retry[0] == {"case": "retry", "kind": "retryable_503"}
    assert retry[1]["call"]["function"]["name"] == "db_summary_save"

    invalid_call = next(x["call"] for x in outbound if x["case"] == "invalid" and x.get("call"))
    assert invalid_call["function"]["name"] == "db_summary_save"
    invalid_args = json.loads(invalid_call["function"]["arguments"])
    assert invalid_args == {"summary": SUMMARY, "command": "printf CHANGED > /forbidden"}
    invalid = _one(trace, "invalid", "execute_received")
    assert invalid["params"] == invalid_args and invalid["armed"] is True and invalid["spent"] is False
    assert invalid["callId"] == invalid_call["id"]
    assert _one(trace, "invalid", "execute_rejected")["reason"] == "invalid_data"
    assert not _events(trace, "invalid", "writer_invoked")
    assert not _events(trace, "invalid", "saved")

    failure_call = next(x["call"] for x in outbound if x["case"] == "writer_fail" and x.get("call"))
    assert json.loads(failure_call["function"]["arguments"]) == {"summary": SUMMARY}
    failed = _one(trace, "writer_fail", "execute_received")
    assert failed["params"] == {"summary": SUMMARY} and failed["armed"] is True and failed["spent"] is False
    assert failed["callId"] == failure_call["id"]
    failed_writer = _one(trace, "writer_fail", "writer_invoked")
    assert json.loads(failed_writer["argv"][1]) == {"summary": SUMMARY}
    assert Path(failed_writer["argv"][0]).name == "fixed_writer.py"
    writer_error = _one(trace, "writer_fail", "writer_failed")
    assert "sqlite3.OperationalError: unable to open database file" in writer_error["error"]
    assert writer_error["stdout"] == ""
    assert not _events(trace, "writer_fail", "saved")
    for case, reason in (("invalid", "invalid_data"), ("writer_fail", "writer_failed")):
        result = next(e for e in _events(trace, case, "child_event") if
                      e.get("type") == "tool_execution_end" and e.get("toolName") == "db_summary_save")
        assert result["isError"] is True
        assert json.loads(result["result"]) == {"saved": False, "reason": reason}
        done = _one(trace, case, "prompt_done")
        assert done["spent"] is True and done.get("savedReceipt") is None
        assert _one(trace, case, "result_rejected")["reason"] == reason
        assert not _events(trace, case, "result_accepted")

    for case in FORBIDDEN:
        calls = [x["call"] for x in outbound if x["case"] == case and x.get("call")]
        assert [x["function"]["name"] for x in calls] == [case]
        assert len([p for p in posts if p["case"] == case]) == 1
        end = next(e for e in _events(trace, case, "child_event") if
                   e.get("type") == "tool_execution_end" and e.get("toolName") == case)
        assert end["isError"] is True and end["result"] == f"Tool {case} not found"
        assert _one(trace, case, "writer_disarmed")["reason"] == "unadvertised_tool_attempt"
        _one(trace, case, "abort_requested")
        _one(trace, case, "abort_completed")
        assert not _events(trace, case, "abort_failed")
        assert not _events(trace, case, "execute_received")
        assert not [e for e in _events(trace, case, "child_event") if
                    e.get("toolName") == "db_summary_save"]
        assert _one(trace, case, "result_rejected")["reason"] == "unadvertised_tool_attempt"
        ordered = [(e.get("event"), e.get("case")) for e in trace]
        assert ordered.index(("writer_disarmed", case)) < ordered.index(("abort_requested", case))
        assert ordered.index(("abort_requested", case)) < ordered.index(("abort_completed", case))
        assert ordered.index(("abort_completed", case)) < ordered.index(("result_rejected", case))
        assert not _events(trace, case, "writer_invoked")
        assert not _events(trace, case, "saved")
    assert len([p for p in posts if p["case"] == "noop"]) == 1
    assert _one(trace, "noop", "prompt_done")["spent"] is False
    assert _one(trace, "noop", "result_rejected")["reason"] == "missing_db_summary_save"
    assert not _events(trace, "noop", "writer_invoked")
    assert not _events(trace, "noop", "saved")

    if external:
        private = artifact.parent / "private"
        raw_trace = [json.loads(line) for line in (private / "trace.jsonl").read_text().splitlines()]
        assert raw_trace == trace
        assert _sha((private / "fixed_writer.py").read_bytes()) == d["source_sha"][str(WRITER)]
        assert Path(failed_writer["argv"][0]).resolve() == (private / "failwriter/fixed_writer.py").resolve()
        assert _sha(Path(failed_writer["argv"][0]).read_bytes()) == d["source_sha"][str(WRITER)]
        assert (private / "failwriter/summary.sqlite3").is_dir()
        with sqlite3.connect(f"file:{private / 'summary.sqlite3'}?mode=ro", uri=True) as conn:
            assert [list(row) for row in conn.execute("SELECT id, summary FROM summaries ORDER BY id")] == d["db_rows"]
        for name, value in d["sentinels"].items():
            assert (private / "cwd" / name).read_text() == value
        assert list((private / "permits").iterdir()) == []
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError(f"owned process group still exists: {pid}")


class PrivateSDKIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_PRIVATE_EVIDENCE")
        if not supplied:
            raise unittest.SkipTest("Q01_SDK_PRIVATE_EVIDENCE is required for runtime verification")
        cls.artifact = Path(supplied).resolve()
        cls.evidence = json.loads(cls.artifact.read_text())

    def test_actual_trace_effects_and_source_binding(self) -> None:
        _assert_runtime(self.evidence, self.artifact)

    def test_corrupt_source_hash_is_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        source = str(FIXTURES / "private_extension.ts")
        bad["source_sha"][source] = "0" * 64
        with self.assertRaises(AssertionError):
            _assert_runtime(bad, self.artifact, external=False)

    def test_corrupt_raw_trace_is_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        bad["trace"] = [e for e in bad["trace"] if not
                        (e.get("case") == "bash" and e.get("event") == "writer_disarmed")]
        with self.assertRaises(AssertionError):
            _assert_runtime(bad, self.artifact, external=False)

    def test_missing_result_rejection_is_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        bad["trace"] = [e for e in bad["trace"] if not
                        (e.get("case") == "noop" and e.get("event") == "result_rejected")]
        with self.assertRaises(AssertionError):
            _assert_runtime(bad, self.artifact, external=False)

    def test_spent_failed_attempt_cannot_be_reported_accepted(self) -> None:
        for case in FAILED_SAVE:
            with self.subTest(case=case):
                bad = deepcopy(self.evidence)
                bad["trace"] = [e for e in bad["trace"] if not
                                (e.get("case") == case and e.get("event") == "result_rejected")]
                bad["trace"].insert(-1, {"pid": bad["process"]["pid"],
                                           "event": "result_accepted", "case": case})
                with self.assertRaises(AssertionError):
                    _assert_runtime(bad, self.artifact, external=False)

    def test_positive_acceptance_without_saved_receipt_is_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        _one(bad["trace"], "p1", "prompt_done")["savedReceipt"] = None
        with self.assertRaises(AssertionError):
            _assert_runtime(bad, self.artifact, external=False)


if __name__ == "__main__":
    unittest.main()
