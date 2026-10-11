"""Independent witness checks for the local SDK tool-roster diagnostic."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "tests/backend/q01_sdk_guard_probe"
EXPECTED_EVENTS = [
    "run_begin", "guard_readback", "alternate_model", "guard_readback",
    "guard_result", "guard_disposed", "run_done",
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _witness_failures(evidence: dict) -> list[str]:
    """Judge raw observations; a harness `checks` summary is intentionally ignored."""
    failures: list[str] = []
    executed_sources = {
        "dispatch_extension.ts": _sha(PROBE / "dispatch_extension.ts"),
        "run_dispatch_probe.py": _sha(PROBE / "run_dispatch_probe.py"),
    }
    observed_sources = evidence.get("source_sha256", {})
    if any(observed_sources.get(name) != digest for name, digest in executed_sources.items()):
        failures.append("source_mismatch")
    argv = evidence.get("argv", [])
    if not argv or evidence.get("omp_binary_sha256") != _sha(Path(argv[0])):
        failures.append("omp_binary_mismatch")
    if not {"--no-tools", "--no-skills", "--no-extensions", "--no-rules", "--no-pty"}.issubset(argv):
        failures.append("parent_flags")
    if not evidence.get("omp_version", "").startswith("omp/"):
        failures.append("omp_version")

    trace = evidence.get("trace", [])
    if [item.get("event") for item in trace] != EXPECTED_EVENTS:
        failures.append("trace_sequence")
    readbacks = {item.get("phase"): item for item in trace if item.get("event") == "guard_readback"}
    before = readbacks.get("before_model_change")
    after = readbacks.get("after_model_change")
    if before is None or after is None:
        failures.append("missing_readback")
    else:
        if not before.get("sessionId") or before["sessionId"] != after.get("sessionId"):
            failures.append("session_mismatch")
        if before.get("active") != ["db_summary_save"]:
            failures.append("initial_roster")
        if after.get("active") != ["db_summary_save", "think"]:
            failures.append("expanded_roster")
        if before.get("modelApi") != "openai-completions" or after.get("modelApi") != "openai-responses":
            failures.append("model_transition")
    alternate = [item for item in trace if item.get("event") == "alternate_model"]
    if len(alternate) != 1 or (alternate[0].get("provider"), alternate[0].get("api")) != (
        "q01guardresp", "openai-responses"
    ):
        failures.append("alternate_model")
    result = [item for item in trace if item.get("event") == "guard_result"]
    if len(result) != 1 or result[0].get("kind") != "think_expanded" or result[0].get("active") != [
        "db_summary_save", "think"
    ]:
        failures.append("guard_result")
    done = [item for item in trace if item.get("event") == "run_done"]
    if len(done) != 1 or done[0].get("ok") is not True:
        failures.append("run_done")
    elif not isinstance(done[0].get("parentEntriesBefore"), int) or (
        done[0]["parentEntriesBefore"] != done[0].get("parentEntriesAfter")
    ):
        failures.append("parent_transcript_changed")
    if evidence.get("parent_rpc_sent") != [{"id": "q01-dispatch-parent-state", "type": "get_state"}]:
        failures.append("parent_rpc_command")
    frames = evidence.get("parent_rpc_frames", [])
    if not any(item.get("type") == "response" and item.get("id") == "q01-dispatch-parent-state"
               and item.get("error") is None for item in frames):
        failures.append("parent_rpc_response")
    if evidence.get("provider_posts") or evidence.get("provider_violation") is not None:
        failures.append("unauthorized_post")
    if evidence.get("sentinels") != {"command-sentinel": "ORIGINAL", "file-sentinel": "ORIGINAL"}:
        failures.append("sentinel_changed")
    if evidence.get("permit_files_remaining") != []:
        failures.append("permit_leftover")
    process = evidence.get("process", {})
    if not process.get("pid") or process.get("stdout_pump_stopped") is not True or (
        process.get("remaining_owned") != [] or process.get("returncode") is None
    ):
        failures.append("process_not_stopped")
    elif any(item.get("pid") != process["pid"] for item in trace):
        failures.append("trace_pid_mismatch")
    if evidence.get("timed_out") is not False or evidence.get("stop_reason") is not None:
        failures.append("incomplete_run")
    return failures


class Q01SdkGuardIndependentTests(unittest.TestCase):
    def _evidence(self) -> dict:
        location = os.environ.get("Q01_SDK_GUARD_EVIDENCE")
        if location is None:
            self.skipTest("Q01_SDK_GUARD_EVIDENCE is required for actual OMP observation")
        return json.loads(Path(location).read_text())

    def test_actual_same_child_roster_expansion_is_a_negative_feasibility_witness(self) -> None:
        evidence = self._evidence()
        self.assertEqual(_witness_failures(evidence), [])
        self.assertEqual(evidence["provider_posts"], [])
        self.assertEqual(len(evidence["trace"]), 7)

    def test_witness_detects_missing_stale_session_and_unauthorized_post(self) -> None:
        original = self._evidence()
        self.assertEqual(_witness_failures(original), [])
        mutations = [
            ("missing_readback", lambda item: item.__setitem__(
                "trace", [event for event in item["trace"]
                          if not (event.get("event") == "guard_readback"
                                  and event.get("phase") == "after_model_change")]
            )),
            ("source_mismatch", lambda item: item["source_sha256"].__setitem__(
                "dispatch_extension.ts", "0" * 64
            )),
            ("session_mismatch", lambda item: next(
                event for event in item["trace"] if event.get("phase") == "after_model_change"
            ).__setitem__("sessionId", "different-session")),
            ("unauthorized_post", lambda item: item["provider_posts"].append({
                "path": "/v1/chat/completions", "tools": ["think"]
            })),
        ]
        for expected, change in mutations:
            with self.subTest(expected=expected):
                variant = copy.deepcopy(original)
                change(variant)
                self.assertIn(expected, _witness_failures(variant))

    def test_witness_does_not_read_unused_private_prompt_mirror(self) -> None:
        evidence = self._evidence()
        original_read = Path.read_bytes

        def read_without_private_mirror(path: Path) -> bytes:
            if ".workflow" in path.parts:
                raise AssertionError(f"private mirror read: {path}")
            return original_read(path)

        # The runtime artifact is explicitly supplied and loaded before read_bytes is denied.
        with patch.object(Path, "read_bytes", read_without_private_mirror):
            self.assertEqual(_witness_failures(evidence), [])
            unused_metadata = copy.deepcopy(evidence)
            unused_metadata["source_sha256"]["12-task.md"] = "0" * 64
            unused_metadata["source_sha256"]["summary-skill.txt"] = "0" * 64
            self.assertEqual(_witness_failures(unused_metadata), [])


if __name__ == "__main__":
    unittest.main()
