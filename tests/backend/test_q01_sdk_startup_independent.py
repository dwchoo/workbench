"""Independent observations of LG-D23 local installed-OMP startup evidence.

Q01_SDK_STARTUP_EVIDENCE names a retained actual run. No OMP is launched here.
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
PROBE = HERE / "q01_sdk_startup_probe"
BODY = HERE / "q01_sdk_private_probe/agent-body.txt"
SKILL = HERE / "q01_sdk_private_probe/summary-skill.txt"
WRITER = HERE / "q01_script_only_probe/fixed_writer.py"
POSITIVES = ("normal", "recover_missing", "recover_noop", "recover_api", "recover_stale",
             "recover_exception")
RECOVER = {"recover_missing": "guard_api_or_missing", "recover_noop": "ineffective_guard",
           "recover_api": "guard_api_or_missing", "recover_stale": "stale_or_invalid_valid_case"}
ALL_CASES = (*POSITIVES, "repeat_fail", "runtime_roster", "bash")
SUMMARY = "Literal command and SQL: touch /tmp/NO_EXECUTE; DROP TABLE summaries; --"
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string", "minLength": 1,
                                                      "maxLength": 2000}},
          "required": ["summary"], "additionalProperties": True}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _events(trace: list[dict], case: str, event: str, attempt: int | None = None) -> list[dict]:
    return [e for e in trace if e.get("case") == case and e.get("event") == event
            and (attempt is None or e.get("attempt") == attempt)]


def _one(trace: list[dict], case: str, event: str, attempt: int | None = None) -> dict:
    found = _events(trace, case, event, attempt)
    assert len(found) == 1, (case, event, attempt, len(found))
    return found[0]


def _before(trace: list[dict], case: str, first: str, second: str,
            first_attempt: int | None = None, second_attempt: int | None = None) -> None:
    a = _one(trace, case, first, first_attempt)
    b = _one(trace, case, second, second_attempt)
    assert trace.index(a) < trace.index(b), (case, first, second)


def _custody(d: dict, artifact: Path) -> None:
    expected = {str(PROBE / "startup_extension.ts"): PROBE / "startup_extension.ts",
                str(PROBE / "run_startup.py"): PROBE / "run_startup.py",
                str(BODY): BODY, str(SKILL): SKILL, str(WRITER): WRITER}
    assert set(d["source_sha"]) == set(expected)
    for name, path in expected.items():
        assert d["source_sha"][name] == _sha(path.read_bytes()), name
    argv = d["argv"]
    assert argv[argv.index("--extension") + 1] == str(PROBE / "startup_extension.ts")
    assert "--no-tools" in argv and argv[argv.index("--mode") + 1] == "rpc"
    assert d["omp_binary_sha"] == _sha(Path(argv[0]).read_bytes())
    private = artifact.parent / "private"
    assert [json.loads(line) for line in (private / "trace.jsonl").read_text().splitlines()] == d["trace"]
    assert _sha((private / "fixed_writer.py").read_bytes()) == d["source_sha"][str(WRITER)]
    with sqlite3.connect(f"file:{private / 'summary.sqlite3'}?mode=ro", uri=True) as conn:
        rows = [list(r) for r in conn.execute("SELECT id, summary FROM summaries ORDER BY id")]
    assert rows == d["db_rows"] == [[n, SUMMARY] for n in range(1, 7)]
    assert d["sentinels"] == {"command-sentinel": "ORIGINAL", "file-sentinel": "ORIGINAL"}
    assert all((private / "cwd" / name).read_text() == value for name, value in d["sentinels"].items())
    assert d["permits_remaining"] == [] and list((private / "permits").iterdir()) == []
    assert d["stop_reason"] is None
    assert d["rpc_sent"] == [{"id": "startup-parent-before", "type": "get_state"},
                             {"id": "startup-parent-after", "type": "get_state"}]
    assert all(any(f.get("id") == requested["id"] and f.get("type") == "response"
                   and f.get("error") is None for f in d["rpc_frames"]) for requested in d["rpc_sent"])
    proc = d["process"]
    assert proc["pid"] > 0 and proc["returncode"] is not None
    assert proc["pump_stopped"] is True and proc["remaining_owned"] == []
    trace = d["trace"]
    assert all(e.get("pid") == proc["pid"] for e in trace)
    assert trace[0]["event"] == "begin" and trace[-1]["event"] == "done"
    assert trace[-1]["ok"] is True
    assert trace[0]["parentBefore"] == trace[-1]["parentBefore"] == trace[-1]["parentAfter"]
    assert _one(trace, "repeat_fail", "summary_deferred")["parentAfter"] == trace[0]["parentBefore"]
    try:
        os.killpg(proc["pid"], 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("owned process group remains")


def _transport(d: dict) -> None:
    trace, posts, outbound = d["trace"], d["provider_posts"], d["provider_outbound"]
    assert Counter(p["case"] for p in posts) == {**{c: 2 for c in POSITIVES}, "bash": 1}
    assert len(posts) == len(outbound) == 13
    assert [p["case"] for p in posts] == [o["case"] for o in outbound]
    system = BODY.read_bytes() + b"\n\n" + SKILL.read_bytes()
    assert trace[0]["bodySha"] == _sha(BODY.read_bytes())
    assert trace[0]["skillSha"] == _sha(SKILL.read_bytes())
    for p in posts:
        assert p["path"] == "/v1/chat/completions" and p["permit"] is True
        assert p["roles"][:2] == ["system", "user"]
        assert p["system_sha"] == _sha(system)
        assert p["tools"] == ["db_summary_save"]
        assert p["tools_field_present"] is True and p["tools_field_type"] == "list"
        assert len(p["tools_raw"]) == 1
        assert p["tools_raw"][0]["function"]["name"] == "db_summary_save"
        assert p["tools_raw"][0]["function"]["parameters"] == SCHEMA
    assert len([e for e in trace if e["event"] == "writer_invoked"]) == 6
    assert d["db_rows"] == [[n, SUMMARY] for n in range(1, 7)]


def _startup_selfchecks(d: dict) -> None:
    trace, posts = d["trace"], d["provider_posts"]
    assert len([e for e in trace if e["event"] == "case_done"]) == len(ALL_CASES)
    assert not [e for e in trace if e["event"] == "case_error"]
    expected_attempts = {"normal": 1, **{c: 2 for c in RECOVER}, "recover_exception": 2,
                         "repeat_fail": 2, "runtime_roster": 1, "bash": 1}
    sessions = []
    for case in ALL_CASES:
        checks = _events(trace, case, "startup_selfcheck")
        checked_attempts = [2] if case == "recover_exception" else \
            list(range(1, expected_attempts[case] + 1))
        assert [e["attempt"] for e in checks] == checked_attempts
        created = _events(trace, case, "session_created")
        assert [e["attempt"] for e in created] == list(range(1, expected_attempts[case] + 1))
        assert [e["attempt"] for e in _events(trace, case, "disposed")] == \
            list(range(1, expected_attempts[case] + 1))
        assert all(e["sessionId"] for e in created)
        sessions.extend(e["sessionId"] for e in created)
        for e in checks:
            assert e["active"] == ["db_summary_save"]
            assert e["privateHas"] is True and e["globalHas"] is False
            assert e["settingsIdentity"] is True and e["sessionId"]
            assert e["sessionId"] == _one(trace, case, "session_created", e["attempt"])["sessionId"]
        done = _one(trace, case, "case_done")
        assert done["ok"] is True and done["attempts"] == expected_attempts[case]
        for prompt in _events(trace, case, "prompt_start"):
            assert not any(trace.index(e) > trace.index(prompt)
                           for e in created), case
    assert len(sessions) == len(set(sessions))

    for case, reason in RECOVER.items():
        first, second = _events(trace, case, "startup_selfcheck")
        assert first["admitted"] is False and second["admitted"] is True
        assert first["sessionId"] != second["sessionId"]
        assert _one(trace, case, "startup_rejected", 1)["reason"] == reason
        assert _one(trace, case, "startup_rejected", 1)["promptCount"] == 0
        _before(trace, case, "startup_rejected", "disposed", 1, 1)
        _before(trace, case, "disposed", "startup_selfcheck", 1, 2)
        _before(trace, case, "startup_selfcheck", "startup_admitted", 2, 2)
        _before(trace, case, "startup_admitted", "prompt_start", 2, 2)
        assert not _events(trace, case, "prompt_start", 1)
        assert not _events(trace, case, "startup_admitted", 1)
        assert not _events(trace, case, "writer_invoked", 1)
        assert len([p for p in posts if p["case"] == case]) == 2
        assert _one(trace, case, "case_done")["promptCount"] == 1
        assert _one(trace, case, "case_done")["saved"] is True
    assert _one(trace, "recover_missing", "startup_selfcheck", 1)["checks"]["api"] is False
    assert _one(trace, "recover_noop", "startup_selfcheck", 1)["checks"].get("wrongRoster") is None
    assert _one(trace, "recover_api", "startup_selfcheck", 1)["checks"]["api"] is False
    assert _one(trace, "recover_stale", "startup_selfcheck", 1)["checks"]["valid"] == "stale_binding"

    thrown = "recover_exception"
    error = _one(trace, thrown, "startup_exception", 1)
    assert error["promptCount"] == 0
    assert error["error"] == "Error: controlled_startup_checker_throw"
    assert not _events(trace, thrown, "startup_selfcheck", 1)
    assert not _events(trace, thrown, "startup_rejected", 1)
    assert not _events(trace, thrown, "startup_admitted", 1)
    assert not _events(trace, thrown, "prompt_start", 1)
    assert not _events(trace, thrown, "writer_invoked", 1)
    assert _one(trace, thrown, "session_created", 1)["sessionId"] != \
        _one(trace, thrown, "session_created", 2)["sessionId"]
    _before(trace, thrown, "session_created", "startup_exception", 1, 1)
    _before(trace, thrown, "startup_exception", "disposed", 1, 1)
    _before(trace, thrown, "disposed", "session_created", 1, 2)
    _before(trace, thrown, "session_created", "startup_selfcheck", 2, 2)
    assert _one(trace, thrown, "case_done")["promptCount"] == 1
    assert _one(trace, thrown, "case_done")["saved"] is True

    for case in ("normal", *RECOVER, thrown, "runtime_roster", "bash"):
        attempt = expected_attempts[case]
        check = _one(trace, case, "startup_selfcheck", attempt)
        assert check["admitted"] is True
        assert check["checks"]["config"] is True and check["checks"]["api"] is True
        assert check["checks"].get("valid") is None
        assert check["checks"]["wrongRoster"] == "roster_mismatch"
        assert check["checks"]["stale"] == "stale_binding"
        admission = _one(trace, case, "startup_admitted", attempt)
        receipt = admission["receipt"]
        assert receipt["requestId"] == f"q01-{case}"
        assert receipt["sessionId"] == check["sessionId"]
        assert receipt["epoch"] == f"epoch-{case}-{attempt}"
        assert receipt["route"] == "private-manager-task"
        assert receipt["nonce"] and receipt["rosterSha"] == _sha(b"db_summary_save")
        assert receipt["schemaSha"] and receipt["policySha"] and receipt["modelKey"]
        assert _one(trace, case, "before_prompt", attempt)["receipt"] == receipt
        _before(trace, case, "startup_selfcheck", "startup_admitted", attempt, attempt)
        _before(trace, case, "startup_admitted", "prompt_start", attempt, attempt)


def _positive_and_deferred(d: dict) -> None:
    trace, posts, outbound = d["trace"], d["provider_posts"], d["provider_outbound"]
    for case in POSITIVES:
        attempt = 1 if case == "normal" else 2
        assert len(_events(trace, case, "prompt_start")) == 1
        assert len(_events(trace, case, "writer_invoked")) == 1
        assert len(_events(trace, case, "saved")) == 1
        assert len(_events(trace, case, "result_accepted")) == 1
        assert not _events(trace, case, "result_rejected")
        guard = _events(trace, case, "stream_guard")
        assert len(guard) == 2 and all(e["attempt"] == attempt and e["names"] == ["db_summary_save"]
                                       and e.get("reason") is None for e in guard)
        call = next(x["call"] for x in outbound if x["case"] == case and x.get("call"))
        assert call["function"]["name"] == "db_summary_save"
        assert json.loads(call["function"]["arguments"]) == {"summary": SUMMARY}
        execute = _one(trace, case, "execute_received", attempt)
        assert execute["callId"] == call["id"] and execute["params"] == {"summary": SUMMARY}
        assert execute["armed"] is True and execute["spent"] is False and execute["protected"] is False
        writer = _one(trace, case, "writer_invoked", attempt)
        assert Path(writer["argv"][0]).name == "fixed_writer.py"
        assert json.loads(writer["argv"][1]) == {"summary": SUMMARY}
        assert 0 < _one(trace, case, "saved", attempt)["receiptBytes"] <= 8192
        result = _one(trace, case, "tool_end")
        assert result["toolName"] == "db_summary_save" and result["isError"] is False
        receipt = json.loads(result["result"])
        assert receipt == {"request_id": f"q01-{case}",
                           "session_id": _one(trace, case, "startup_selfcheck", attempt)["sessionId"],
                           "saved": True}
        assert _one(trace, case, "prompt_done", attempt)["saved"] is True
        assert _one(trace, case, "case_done")["attempts"] == attempt
        assert len([p for p in posts if p["case"] == case]) == 2
    assert d["db_rows"] == [[n, SUMMARY] for n in range(1, 7)]

    rejects = _events(trace, "repeat_fail", "startup_rejected")
    assert [r["attempt"] for r in rejects] == [1, 2]
    assert all(r["reason"] == "ineffective_guard" and r["promptCount"] == 0 for r in rejects)
    assert not _events(trace, "repeat_fail", "startup_admitted")
    assert not _events(trace, "repeat_fail", "prompt_start")
    assert not _events(trace, "repeat_fail", "writer_invoked")
    assert not any(p["case"] == "repeat_fail" for p in posts)
    deferred = _one(trace, "repeat_fail", "summary_deferred")
    assert deferred["attempts"] == 2 and deferred["promptCount"] == 0
    assert _one(trace, "repeat_fail", "case_done")["protected"] is True
    _before(trace, "repeat_fail", "disposed", "summary_deferred", 2, None)
    # A later parent state response is captured; the parent remained usable.
    assert any(f.get("id") == "startup-parent-after" and f.get("type") == "response"
               and f.get("error") is None for f in d["rpc_frames"])


def _runtime_protection(d: dict) -> None:
    trace, posts, outbound = d["trace"], d["provider_posts"], d["provider_outbound"]
    assert _one(trace, "runtime_roster", "before_prompt")["active"] == []
    assert _one(trace, "runtime_roster", "stream_guard")["reason"] == "roster_mismatch"
    assert not any(p["case"] == "runtime_roster" for p in posts)
    assert not _events(trace, "runtime_roster", "writer_invoked")
    assert _one(trace, "runtime_roster", "prompt_done")["protected"] is True
    assert _one(trace, "runtime_roster", "result_rejected")
    assert _one(trace, "runtime_roster", "case_done")["attempts"] == 1
    assert len([p for p in posts if p["case"] == "bash"]) == 1
    assert next(x["call"] for x in outbound if x["case"] == "bash" and x.get("call"))["function"]["name"] == "bash"
    tool = _one(trace, "bash", "tool_end")
    assert tool["toolName"] == "bash" and tool["isError"] is True
    assert tool["result"] == "Tool bash not found"
    assert len(_events(trace, "bash", "stream_guard")) == 2
    assert _events(trace, "bash", "stream_guard")[1]["reason"] == "owner_inactive"
    _before(trace, "bash", "abort_requested", "abort_completed")
    _before(trace, "bash", "abort_completed", "result_rejected")
    assert not _events(trace, "bash", "abort_failed")
    assert not _events(trace, "bash", "writer_invoked")
    assert _one(trace, "bash", "case_done")["attempts"] == 1


class StartupIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_STARTUP_EVIDENCE")
        if not supplied:
            raise unittest.SkipTest("Q01_SDK_STARTUP_EVIDENCE required; skip is not a runtime pass")
        cls.artifact = Path(supplied).resolve()
        cls.evidence = json.loads(cls.artifact.read_text())

    def test_actual_source_binary_trace_sqlite_and_cleanup(self) -> None:
        _custody(self.evidence, self.artifact)

    def test_real_provider_posts_use_only_fixed_db_tool(self) -> None:
        _transport(self.evidence)

    def test_startup_selfcheck_then_at_most_one_fresh_rebuild(self) -> None:
        _startup_selfchecks(self.evidence)

    def test_positive_one_prompt_one_write_and_repeated_failure_deferred(self) -> None:
        _positive_and_deferred(self.evidence)

    def test_runtime_roster_and_forbidden_tool_protection(self) -> None:
        _runtime_protection(self.evidence)

    def test_missing_first_disposal_is_not_accepted(self) -> None:
        bad = deepcopy(self.evidence)
        bad["trace"] = [e for e in bad["trace"] if not
                        (e.get("case") == "recover_missing" and e.get("event") == "disposed" and e.get("attempt") == 1)]
        with self.assertRaises(AssertionError):
            _startup_selfchecks(bad)

    def test_thrown_checker_requires_disposal_and_fresh_retry(self) -> None:
        for event in ("startup_exception", "disposed"):
            with self.subTest(missing=event):
                bad = deepcopy(self.evidence)
                bad["trace"] = [e for e in bad["trace"] if not
                                (e.get("case") == "recover_exception" and
                                 e.get("event") == event and e.get("attempt") == 1)]
                with self.assertRaises(AssertionError):
                    _startup_selfchecks(bad)
        bad = deepcopy(self.evidence)
        first_id = _one(bad["trace"], "recover_exception", "session_created", 1)["sessionId"]
        _one(bad["trace"], "recover_exception", "session_created", 2)["sessionId"] = first_id
        with self.assertRaises(AssertionError):
            _startup_selfchecks(bad)

    def test_stale_rebuild_identity_or_second_failure_post_corruption_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        first = _one(bad["trace"], "recover_stale", "startup_selfcheck", 1)
        _one(bad["trace"], "recover_stale", "startup_selfcheck", 2)["sessionId"] = first["sessionId"]
        with self.assertRaises(AssertionError):
            _startup_selfchecks(bad)
        bad = deepcopy(self.evidence)
        bad["provider_posts"].append({"case": "repeat_fail"})
        with self.assertRaises(AssertionError):
            _positive_and_deferred(bad)

    def test_corrupt_source_hash_or_false_positive_result_rejected(self) -> None:
        bad = deepcopy(self.evidence)
        bad["source_sha"][str(PROBE / "startup_extension.ts")] = "0" * 64
        with self.assertRaises(AssertionError):
            _custody(bad, self.artifact)
        bad = deepcopy(self.evidence)
        bad["trace"] = [e for e in bad["trace"] if not
                        (e.get("case") == "runtime_roster" and e.get("event") == "result_rejected")]
        with self.assertRaises(AssertionError):
            _runtime_protection(bad)


if __name__ == "__main__":
    unittest.main()
