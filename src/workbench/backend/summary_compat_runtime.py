"""One owned C1 fixture, with bounded startup and truthful partial observations.

Normal native catalog preparation is allowed. No operating user session, auth
store, sandbox, trace facility or development runtime is used by this verifier.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from uuid import uuid4

from workbench.backend.launcher import LaunchPlan, load_model_table, omp_environment
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.backend.paths import DataLayout, write_private_json
from workbench.contracts.logs_v1 import canonical_json, decode_json, parse_summary_request
from workbench.storage.important_log import ImportantLogCatalog, ImportantLogStore
from workbench.tasks.repository import InvalidTransitionError, TaskRepository

DEADLINE_SECONDS = 90
SUMMARY = {"facts": ["소유 private scripted fixture의 연결을 확인했다."],
           "interpretations": [], "unknowns": ["실제 MODEL 및 전체 Q01은 미검증이다."]}
IO_KEYS = ("omp_children", "model_calls", "network_requests", "auth_store_open", "settings_writes")
Q01_ITEMS = tuple("Q01-" + item for item in
                  ("route", "tools", "skill", "result", "context", "priority", "recovery", "origin"))
REQUIRED_CHECKS = {
    "Q01-route": ("owned_c1_cleanup_complete", "parent_unchanged", "registry_unchanged", "settings_unchanged", "role_filter_controls", "actual_owned_authority"),
    "Q01-tools": ("owned_c1_cleanup_complete", "observed_post_used_db_summary_save_only", "child_disposed", "negative_tool_control", "startup_roster_rebuild", "startup_api_refused", "startup_stale_refused"),
    "Q01-skill": ("owned_c1_cleanup_complete", "exact_skill_prompt_observed", "writer_skill_binding_matches", "role_filter_expectation"),
    "Q01-result": ("owned_c1_cleanup_complete", "writer_request_binding_matches", "receipt_matches", "body_matches_fixture", "single_catalog_entry", "wrong_id_rejected", "wrong_revision_rejected", "wrong_range_rejected", "wrong_id_receipt_absent", "wrong_revision_receipt_absent", "wrong_range_receipt_absent", "duplicate_receipt_rejected", "duplicate_receipt_unchanged", "task_spec_unchanged"),
    "Q01-context": ("owned_c1_cleanup_complete", "three_context_scenarios", "bounded_receipts", "actual_context_preflights", "parent_context_unchanged"),
    "Q01-priority": ("priority_native_launch_observed", "priority_coverage_complete", "priority_cleanup_complete", "strict_parent_unchanged", "raw_parent_contamination_absent", "prior_entry_views_unchanged", "nonshared_observed_route", "actual_core_question", "busy_parent_observed", "draft_deferred", "due_check_and_end", "upper_source_binding"),
    "Q01-recovery": ("owned_c1_cleanup_complete", "recovery_unknown_before_response", "recovery_replay_refused", "recovery_replay_row_absent", "duplicate_receipt_rejected", "recovery_receipt_unchanged", "pause_no_send_confirmed", "restart_response_absent", "restart_replay_refused", "restart_replay_row_absent", "final_replay_row_absent", "fresh_owned_distinct_input"),
    "Q01-origin": ("owned_c1_cleanup_complete", "request_scope_binding_matches", "actual_origin_controls"),
}
PAUSE_NO_SEND_DETAIL = "owner_predicate_false_confirmed_before_send"
DISCONNECT_REQUEST_ID = "fc8416c2-7d8c-46d4-9615-b833b567f9fe"
RESTART_REQUEST_ID = "a40f9a18-a0fc-4d75-9260-7f83cb06622d"


def _initial_entries(root: Path) -> int:
    return json.loads((root / "fixture.json").read_text()).get("caseControls", {}).get("initial_entries", 0)


def _item_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    observations = result.get("observations") if isinstance(result.get("observations"), list) else []
    c1_index = next((index for index, row in enumerate(observations)
                     if isinstance(row, dict) and row.get("kind") == "owned_C1_partial"), None)
    c1 = observations[c1_index] if c1_index is not None else {}
    completed = c1.get("completed") if isinstance(c1.get("completed"), dict) else None
    outcome = completed.get("outcome") if completed and isinstance(completed.get("outcome"), dict) else {}
    summary_result = outcome.get("result") if isinstance(outcome.get("result"), dict) else {}
    writer = completed.get("writerObserved") if completed and isinstance(completed.get("writerObserved"), dict) else {}
    posts = c1.get("localhost_model_posts") if isinstance(c1.get("localhost_model_posts"), list) else []
    exercised = set(completed.get("exercised", [])) if completed and isinstance(completed.get("exercised"), list) else set()
    recovery = c1.get("recovery_control") if isinstance(c1.get("recovery_control"), dict) else {}
    restart = c1.get("restart_control") if isinstance(c1.get("restart_control"), dict) else {}
    recovery_pause = recovery.get("owner_predicate_false_control") if isinstance(
        recovery.get("owner_predicate_false_control"), dict) else {}

    def boolean(value: Any) -> bool | None:
        return value if type(value) is bool else None

    def inverse(value: Any) -> bool | None:
        return not value if type(value) is bool else None

    tool_posts = [post for post in posts if isinstance(post, dict)]
    tool_only = all(post.get("tools") == ["db_summary_save"] for post in tool_posts) if tool_posts else None
    system_exact = all(post.get("system_exact") is True for post in tool_posts) if tool_posts else None
    source_binding = boolean(writer.get("request_binding_matches"))
    receipt_matches = boolean(writer.get("receipt_matches"))
    body_matches = (writer.get("actual_body") == {"schemaVersion": 1,
        "facts": ["소유 private scripted fixture의 연결을 확인했다."], "interpretations": [],
        "unknowns": ["실제 MODEL 및 전체 Q01은 미검증이다."]}) if "actual_body" in writer else None
    source_count = len(summary_result["sources"]) if isinstance(summary_result.get("sources"), list) else None
    rows: list[dict[str, Any]] = []
    for item in Q01_ITEMS:
        checks: dict[str, bool | None] = {"owned_c1_cleanup_complete": boolean(c1.get("cleanup_complete"))}
        if item == "Q01-route":
            checks.update(c1_case_exercised=item in exercised if completed else None,
                          parent_unchanged=boolean(completed.get("parentUnchanged")) if completed else None,
                          registry_unchanged=boolean(completed.get("registryUnchanged")) if completed else None,
                          settings_unchanged=boolean(completed.get("settingsUnchanged")) if completed else None,
                          role_filter_controls=None)
            required = ("parent_unchanged", "registry_unchanged", "settings_unchanged")
        elif item == "Q01-tools":
            checks.update(c1_case_exercised=item in exercised if completed else None,
                          observed_post_used_db_summary_save_only=tool_only,
                          tool_call_observed=(completed.get("toolCalls", 0) > 0)
                          if completed and type(completed.get("toolCalls")) is int else None,
                          child_disposed=boolean(completed.get("childDisposed")) if completed else None,
                          negative_tool_control=None)
            required = ("observed_post_used_db_summary_save_only", "child_disposed")
        elif item == "Q01-skill":
            checks.update(c1_case_exercised=item in exercised if completed else None,
                          exact_skill_prompt_observed=system_exact,
                          writer_skill_binding_matches=source_binding,
                          role_filter_expectation=None)
            required = ("exact_skill_prompt_observed", "writer_skill_binding_matches")
        elif item == "Q01-result":
            checks.update(summary_result_returned=(outcome.get("status") == "summary_result")
                          if "status" in outcome else None,
                          writer_request_binding_matches=source_binding,
                          receipt_matches=receipt_matches,
                          body_matches_fixture=body_matches,
                          single_catalog_entry=(writer.get("catalog_entries") == 1)
                          if type(writer.get("catalog_entries")) is int else None)
            required = ("writer_request_binding_matches", "receipt_matches", "body_matches_fixture", "single_catalog_entry")
        elif item == "Q01-context":
            checks.update(c1_case_exercised=item in exercised if completed else None,
                          operating_metadata_pass=boolean(completed.get("operatingMetadataPass")) if completed else None,
                          three_context_scenarios=None)
            required = ()
        elif item == "Q01-priority":
            checks["priority_observation_run"] = None
            required = ()
        elif item == "Q01-recovery":
            checks.update(recovery_unknown_before_response=boolean(recovery.get("unknown_before_response")),
                          recovery_replay_refused=boolean(recovery.get("replay_reserve_refused")),
                          recovery_replay_row_absent=inverse(recovery.get("replay_row_present")),
                          duplicate_receipt_rejected=inverse(recovery.get("duplicate_receipt_accepted")),
                          recovery_receipt_unchanged=boolean(recovery.get("receipt_unchanged")),
                          pause_no_send_confirmed=boolean(recovery_pause.get("noSendConfirmed")),
                          restart_response_absent=boolean(restart.get("completed_artifact_absent")),
                          restart_replay_refused=boolean(restart.get("replay_reserve_refused")),
                          restart_replay_row_absent=inverse(restart.get("replay_row_present")),
                          final_replay_row_absent=boolean(restart.get("final_replay_row_absent")))
            required = ("recovery_unknown_before_response", "recovery_replay_refused", "recovery_replay_row_absent",
                        "duplicate_receipt_rejected", "recovery_receipt_unchanged", "pause_no_send_confirmed",
                        "restart_response_absent", "restart_replay_refused", "restart_replay_row_absent",
                        "final_replay_row_absent")
        else:  # Q01-origin
            checks.update(c1_case_exercised=item in exercised if completed else None,
                          request_scope_binding_matches=source_binding,
                          one_fixture_source_observed=source_count == 1 if source_count is not None else None)
            required = ("request_scope_binding_matches",)

        if item == "Q01-priority":
            observed = False
        elif item == "Q01-recovery":
            observed = bool(recovery or restart)
        else:
            observed = completed is not None and item in exercised
        status = "inconclusive" if observed else "not_run"
        if any(checks.get(name) is False for name in required):
            status = "failed"
        if status == "not_run":
            unknowns = ["item_not_observed"]
        elif item == "Q01-route":
            unknowns = ["role_filter_controls_unobserved"]
        elif item == "Q01-tools":
            unknowns = ["negative_tool_control_unobserved"]
        elif item == "Q01-skill":
            unknowns = ["role_filter_expectation_unverified"]
        elif item == "Q01-result":
            unknowns = ["wrong_binding_and_duplicate_controls_unverified"]
        elif item == "Q01-context":
            unknowns = ["three_context_scenarios_unobserved", "output_budget_unavailable"]
        elif item == "Q01-priority":
            unknowns = ["priority_observation_not_run"]
        elif item == "Q01-recovery":
            unknowns = ["complete_recovery_contract_unverified"]
        else:
            unknowns = ["origin_scope_and_close_controls_unverified"]
        rows.append({"item": item, "result": status, "checks": checks, "unknowns": unknowns,
                     "evidence_refs": ([{"observation_index": c1_index, "kind": "owned_C1_partial"}]
                                       if c1_index is not None else [])})
    controls = completed.get("caseControls") if completed else None
    if not isinstance(controls, list) or not controls:
        return rows
    cases = {case.get("name"): case for case in controls if isinstance(case, dict)}
    host = completed.get("caseHost", {})
    result_checks = host.get("result", {}).get("checks", {})
    origin_checks = host.get("origin", {}).get("checks", {})

    def posts_for(name):
        return [post for post in tool_posts if post.get("case") == name]

    def refused(name):
        case = cases.get(name, {})
        return (case.get("outcome", {}).get("sent") is False and case.get("childDisposed") is True
                and case.get("childCreates") == 2 and not posts_for(name)
                and case.get("disposalBeforeRebuild") is True)

    worker = cases.get("worker", {})
    role = (worker.get("outcome", {}).get("sent") is False and worker.get("childCreates") == 0
            and not posts_for("worker"))
    rebuilt = cases.get("roster_rebuild", {})
    negative = cases.get("saved_then_bash", {})
    normal = [{"outcome": outcome, "events": completed.get("events", []),
               "childDisposed": completed.get("childDisposed"), "parentUnchanged": completed.get("parentUnchanged"),
               "receiptBytes": len(canonical_json(outcome.get("receipt"))) if outcome.get("receipt") else None},
              cases.get("normal2", {}), cases.get("normal3", {})]
    preflights = [[event for event in case.get("events", []) if event.get("event") == "dispatch_preflight"] for case in normal]
    for row in rows:
        item, checks = row["item"], row["checks"]
        if item == "Q01-route":
            checks.update(role_filter_controls=role, actual_owned_authority=bool(origin_checks) and all(value is True for value in origin_checks.values()))
        elif item == "Q01-tools":
            checks.update(negative_tool_control=(negative.get("outcome", {}).get("protected") is True
                and negative.get("outcome", {}).get("status") in ("rejected", "unknown")
                and negative.get("receiptAbsent") is True and negative.get("writerCalls") == 0
                and negative.get("writerEntries") == 0 and len(posts_for("saved_then_bash")) == 2),
                startup_roster_rebuild=(rebuilt.get("outcome", {}).get("status") == "summary_result"
                    and rebuilt.get("childCreates") == 2 and rebuilt.get("disposalBeforeRebuild") is True
                    and rebuilt.get("childDisposed") is True and len(posts_for("roster_rebuild")) == 2),
                startup_api_refused=refused("api_invalid"), startup_stale_refused=refused("stale_invalid"))
        elif item == "Q01-skill":
            checks["role_filter_expectation"] = role
        elif item == "Q01-result":
            checks.update({key: result_checks.get(key) for key in REQUIRED_CHECKS[item] if key in result_checks})
        elif item == "Q01-context":
            checks.update(three_context_scenarios=all(case.get("outcome", {}).get("status") == "summary_result"
                and case.get("childDisposed") is True for case in normal),
                bounded_receipts=all(type(case.get("receiptBytes")) is int and 0 < case["receiptBytes"] <= 4096 for case in normal),
                actual_context_preflights=all(events and all(type(event.get("outputCap")) is int
                    and 0 < event["outputCap"] <= 8192 and event.get("fittedCap") == event["outputCap"]
                    and event.get("conservativeWireBytes", 32769) <= 32768
                    and event.get("mandatoryWireBytes", 0) <= 32768 for event in events) for events in preflights),
                parent_context_unchanged=all(case.get("parentUnchanged") is True for case in normal))
        elif item == "Q01-recovery":
            checks["fresh_owned_distinct_input"] = boolean(restart.get("fresh_owned_distinct_input"))
        elif item == "Q01-origin":
            checks["actual_origin_controls"] = bool(origin_checks) and all(value is True for value in origin_checks.values())
        if item != "Q01-priority":
            required = REQUIRED_CHECKS[item]
            row["result"] = ("failed" if any(checks.get(key) is False for key in required) else
                             "pass" if all(checks.get(key) is True for key in required) else "inconclusive")
            row["unknowns"] = ["missing_check:" + key for key in required if checks.get(key) is not True]
        row["evidence_refs"] = [{"observation_index": c1_index, "kind": c1.get("kind"),
            "sha256": sha256(json.dumps(c1, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest()}]
    return rows


def unavailable(reason: str) -> dict[str, Any]:
    return {"level": "unavailable", "observations": [], "items": {},
            "model_metadata": {"status": "inconclusive", "unknowns": [
                "operating_session_unavailable", "output_budget_unavailable"]},
            "unknowns": [reason], "io": dict.fromkeys(IO_KEYS, 0)}


def _identity(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def _remaining(pgid: int) -> list[int]:
    result = []
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            try:
                fields = (path / "stat").read_text().split(") ", 1)[1].split()
                if int(fields[2]) == pgid:
                    result.append(int(path.name))
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
    return result


def _signal_leader(process: subprocess.Popen, ref: dict | None, sig: int) -> bool:
    """Signal only before reap, with a live leader and one immediate identity read."""
    if process.returncode is not None or not ref or ref.get("start_ticks") is None:
        return False
    try:
        fields = Path(f"/proc/{process.pid}/stat").read_text().split(") ", 1)[1].split()
    except FileNotFoundError:
        return False
    if fields[0] in {"Z", "X", "x"} or fields[19] != ref["start_ticks"]:
        return False
    # No poll/wait may reap the leader between the identity read and this signal.
    os.killpg(process.pid, sig)
    return True


def _stop_leader(process: subprocess.Popen, ref: dict | None) -> str | None:
    if process.poll() is not None:
        return None
    if not _signal_leader(process, ref, signal.SIGTERM):
        return "owned_process_identity_unavailable"
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if not _signal_leader(process, ref, signal.SIGKILL):
            return "owned_process_identity_unavailable"
        process.wait(timeout=3)
    return None


def _private_root(root: Path) -> None:
    info = root.lstat()
    if (root.resolve() != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("unsafe_verification_root")


def _environment(root: Path) -> dict[str, str]:
    # Construct rather than filter inherited environment: no provider credentials,
    # proxy, user OMP profile, preload, session or alternate executable hooks.
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(root / "home"),
           "TERM": "dumb", "LANG": "C.UTF-8", "TMPDIR": str(root / "tmp"),
           "PI_CODING_AGENT_DIR": str(root / "agent"), "PI_CONFIG_DIR": str(root / "config"),
           "PYTHONDONTWRITEBYTECODE": "1", "WORKBENCH_SUMMARY_VERIFY_ROOT": str(root),
           "WORKBENCH_SUMMARY_VERIFY_PYTHON": str(Path(sys.executable).resolve())}
    for name in ("CONFIG", "DATA", "STATE", "CACHE"):
        env[f"XDG_{name}_HOME"] = str(root / ("xdg-" + name.lower()))
    return env


def _owned_model_table(runtime: Path) -> dict:
    # Detached controlled snapshot; never rewrite the shipped/copied model table.
    table = load_model_table(runtime / "omp_bridge/omp-models.yml")
    table["worker"]["models"]["default"] = "ownedverify/worker:high"
    return table


def _write_owned_models(root: Path, endpoint: str, *, tokenizer: str | None = None) -> None:
    models = root / "agent/models.yml"
    text = "providers:\n  ownedverify:\n    api: openai-completions\n    auth: none\n" + f"    baseUrl: {endpoint}\n    models:\n"
    for name in ("scripted", "worker"):
        text += (f"      - id: {name}\n        name: {name}\n"
                 "        contextWindow: 131072\n        maxTokens: 8192\n        reasoning: true\n")
        if tokenizer is not None:
            text += f"        tokenizer: {tokenizer}\n"
    models.write_text(text)
    models.chmod(0o600)


def _copy_runtime(source: Path, root: Path) -> Path:
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700)
    for name in ("src/workbench", "omp_bridge", "pyproject.toml"):
        original = source / name
        if not original.exists() or original.is_symlink():
            raise ValueError("runtime_assets_unavailable")
        paths = original.rglob("*") if original.is_dir() else [original]
        if any(path.is_symlink() for path in paths):
            raise ValueError("unsafe_runtime_assets")
        target = runtime / name
        if original.is_dir():
            shutil.copytree(original, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copyfile(original, target)
    for path in runtime.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    if not (runtime / "omp_bridge/g3/summary_verification.ts").is_file():
        raise ValueError("verification_entry_unavailable")
    return runtime


class _Server(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, *args, **kwargs):
        self._accepted_sockets: set[socket.socket] = set()
        self._socket_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        with self._socket_lock:
            self._accepted_sockets.add(connection)
        return connection, address

    def shutdown_request(self, connection):
        try:
            # The request handler remains the owner of close()/makefile lifetime.
            super().shutdown_request(connection)
        finally:
            with self._socket_lock:
                self._accepted_sockets.discard(connection)

    def shutdown_accepted(self):
        with self._socket_lock:
            connections = tuple(self._accepted_sockets)
        for connection in connections:
            try:
                # Unblock header/body recv without closing another thread's fd.
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                # A handler may have already closed this same socket object.
                pass

    def accepted_count(self) -> int:
        with self._socket_lock:
            return len(self._accepted_sockets)


def _matched_prompt(expected: dict, body: dict, observations: dict) -> object:
    users = [row for row in body.get("messages", []) if row.get("role") == "user"]
    if len(users) != 1:
        raise ValueError("recovery_prompt_binding_mismatch")
    content = users[0].get("content")
    if isinstance(content, str):
        parts = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        parts = content
    else:
        raise ValueError("prompt_envelope_unrecognized")
    matched = []
    other_parts = []
    for part in parts:
        is_text = isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        raw = part["text"].encode("utf-8") if is_text else canonical_json(part)
        digest = sha256(raw).hexdigest()
        if is_text and len(raw) == expected.get("promptBytes") and digest == expected.get("promptSha256"):
            matched.append(raw)
        else:
            other_parts.append({"type": part.get("type") if isinstance(part, dict) else type(part).__name__,
                                "bytes": len(raw), "sha256": digest})
    observations.update(prompt_matching_parts=len(matched),
                             sdk_envelope_parts_unattributed=other_parts)
    if len(matched) != 1:
        raise ValueError("prompt_envelope_unrecognized")
    return decode_json(matched[0], max_bytes=32768)


class _RecoveryControl:
    """Owned journal control only; no disconnect, restart or pause claim."""
    def __init__(self, root: Path, *, pause_control: bool = False):
        self.root = root
        self.pause_control = pause_control
        self.request_id: str | None = None
        self.observations: dict[str, Any] = {}

    def before_response(self, expected: dict, body: dict, text) -> None:
        prompt = _matched_prompt(expected, body, self.observations)
        with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
            record = journal.get_summary_request(expected["requestId"])
            if (not isinstance(prompt, dict) or record is None or record["state"] != "sending"
                    or canonical_json(prompt.get("request")) != canonical_json(record["request"])):
                raise ValueError("recovery_prompt_binding_mismatch")
            self.request_id = expected["requestId"]
            self.observations.update(kind="owner_controlled_recovery", result="inconclusive",
                request_id=self.request_id, request_binding_matches=True, pause_restart="not_run")
            journal.record_summary_state(self.request_id, "unknown",
                                         detail="owner_controlled_unknown_before_response")
            self.observations.update(unknown_before_response=True,
                                     unknown_detail="owner_controlled_unknown_before_response")
            replay = deepcopy(record["request"])
            replay["requestId"] = str(uuid4())
            refused = False
            try:
                journal.reserve_summary_request(replay)
            except InvalidTransitionError:
                refused = True
            present = journal.get_summary_request(replay["requestId"]) is not None
            self.observations.update(replay_request_id=replay["requestId"], replay_reserve_refused=refused,
                                     replay_row_present=present)
            if not refused or present:
                raise ValueError("recovery_replay_reservation_not_refused")

    def after_result(self, completed: dict, catalog: ImportantLogCatalog) -> None:
        if (self.request_id != completed["requestId"]
                or self.observations.get("unknown_before_response") is not True):
            raise ValueError("recovery_control_unobserved")
        outcome = completed["outcome"]
        with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
            if self.pause_control:
                pause = completed.get("pauseControl")
                if (not isinstance(pause, dict) or pause.get("control") != "owner_predicate_false_control"
                        or type(pause.get("predicateCalls")) is not int or pause["predicateCalls"] < 1
                        or pause.get("status") != "deferred" or pause.get("sent") is not False
                        or pause.get("childCreates") != 0 or pause.get("promptStarts") != 0
                        or pause.get("noSendConfirmed") is not True or pause.get("positiveControlSameMetadata") is not True
                        or pause.get("requestId") == self.request_id):
                    raise ValueError("pause_no_send_control_unobserved")
                held = journal.get_summary_request(pause["requestId"])
                fresh = journal.get_summary_request(self.request_id)
                history = [tuple(row) for row in journal._connection.execute(
                    "SELECT state,detail FROM summary_request_events_v1 WHERE request_id=? ORDER BY event_id",
                    (pause["requestId"],))]
                receipt_count = journal._connection.execute(
                    "SELECT COUNT(*) FROM summary_result_receipts_v1 WHERE request_id=?", (pause["requestId"],)).fetchone()[0]
                drop_count = journal._connection.execute(
                    "SELECT COUNT(*) FROM summary_result_drops_v1 WHERE request_id=?", (pause["requestId"],)).fetchone()[0]
                matches = (held is not None and fresh is not None and held["request"]["inputKey"] == fresh["request"]["inputKey"]
                           and held["request"]["sources"] == fresh["request"]["sources"]
                           and held["request"]["sourceGaps"] == fresh["request"]["sourceGaps"])
                self.observations["owner_predicate_false_control"] = {**pause, "state_order": [row[0] for row in history],
                    "detail": PAUSE_NO_SEND_DETAIL, "receipt_count": receipt_count, "drop_count": drop_count,
                    "same_input_and_ranges": matches, "actualAutomationController": "not_run", "pause_store": "not_run", "restart": "not_run"}
                if (not matches or held["state"] != "not_sent" or held["receipt"] is not None
                        or history != [("prepared", None), ("not_sent", PAUSE_NO_SEND_DETAIL)]
                        or receipt_count != 0 or drop_count != 0):
                    raise ValueError("pause_journal_observation_mismatch")
            states = [row[0] for row in journal._connection.execute(
                "SELECT state FROM summary_request_events_v1 WHERE request_id=? ORDER BY event_id",
                (self.request_id,))]
            if states != ["prepared", "sending", "unknown", "stored"]:
                raise ValueError("recovery_state_order_mismatch")
            first_receipt = journal.get_summary_request(self.request_id)["receipt"]
            accepted = journal.record_summary_result_receipt(outcome["result"], outcome["writer"]["entryId"])
            receipts = journal._connection.execute(
                "SELECT COUNT(*) FROM summary_result_receipts_v1 WHERE request_id=?", (self.request_id,)).fetchone()[0]
            drops = journal._connection.execute(
                "SELECT COUNT(*) FROM summary_result_drops_v1 WHERE request_id=?", (self.request_id,)).fetchone()[0]
            after = journal.get_summary_request(self.request_id)
            state_after = after["state"]
            receipt_unchanged = after["receipt"] == first_receipt
            states_after = [row[0] for row in journal._connection.execute(
                "SELECT state FROM summary_request_events_v1 WHERE request_id=? ORDER BY event_id",
                (self.request_id,))]
        with ImportantLogStore.open(DataLayout(self.root / "data").important_logs,
                                    catalog.active_uuid(), readonly=True) as store:
            entries = store._connection.execute("SELECT COUNT(*) FROM entries WHERE kind='cumulative' AND subject_kind=? AND subject_id=?",
                (completed["outcome"]["result"]["sources"][0]["source"].get("taskId") and "task" or "command",
                 completed["outcome"]["result"]["sources"][0]["source"].get("taskId") or completed["outcome"]["result"]["sources"][0]["source"]["commandId"])).fetchone()[0]
        self.observations.update(state_order=states_after, state_after_duplicate=state_after,
            duplicate_receipt_accepted=accepted, receipt_unchanged=receipt_unchanged,
            receipt_count=receipts, duplicate_drop_count=drops, catalog_entries_after_duplicate=entries)
        if (accepted is not False or receipts != 1 or drops != 1 or entries != 1
                or state_after != "stored" or states_after != states or not receipt_unchanged):
            raise ValueError("recovery_duplicate_observation_mismatch")


class _RestartControl:
    """One validated response-free POST and owned process replacement, not backend restart."""
    def __init__(self, root: Path, deadline: float):
        self.root, self.deadline = root, deadline
        self.phase = "disconnect"
        self.phase_posts = {"disconnect": 0, "between": 0, "restart": 0}
        self.post_seen, self.release, self.handler_done = threading.Event(), threading.Event(), threading.Event()
        self.handler_thread: threading.Thread | None = None
        self.original: dict | None = None
        self.replay_id: str | None = None
        self.observations: dict[str, Any] = {}

    def hold(self, expected: dict, body: dict) -> bool:
        prompt = _matched_prompt(expected, body, self.observations)
        with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
            record = journal.get_summary_request(DISCONNECT_REQUEST_ID)
            if (record is None or record["state"] != "sending" or not isinstance(prompt, dict)
                    or canonical_json(prompt.get("request")) != canonical_json(record["request"])):
                raise ValueError("disconnect_prompt_binding_mismatch")
            self.original = record["request"]
        self.handler_thread = threading.current_thread()
        self.observations.update(request_id=DISCONNECT_REQUEST_ID, validated_post=True, response_bytes=0,
                                 child_disposal="unknown")
        self.post_seen.set()
        try:
            return self.release.wait(timeout=max(0, self.deadline - time.monotonic()))
        finally:
            self.handler_done.set()

    def confirm_unknown(self, process: subprocess.Popen, ref: dict, catalog: ImportantLogCatalog) -> None:
        if not self.handler_done.wait(timeout=max(0, self.deadline - time.monotonic())):
            raise TimeoutError("disconnect_handler_not_done")
        if self.handler_thread is not None:
            self.handler_thread.join(timeout=max(0, self.deadline - time.monotonic()))
        remaining = _remaining(ref["pgid"])
        completed_absent = not (self.root / "completed.json").exists()
        if (not self.post_seen.is_set() or process.returncode != -signal.SIGKILL or remaining
                or not completed_absent or self.handler_thread is None or self.handler_thread.is_alive()
                or self.phase_posts != {"disconnect": 1, "between": 0, "restart": 0}):
            raise ValueError("disconnect_no_response_proof_unavailable")
        with ImportantLogStore.open(DataLayout(self.root / "data").important_logs, catalog.active_uuid(), readonly=True) as store:
            entries = store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
            record = journal.get_summary_request(DISCONNECT_REQUEST_ID)
            states = [row[0] for row in journal._connection.execute(
                "SELECT state FROM summary_request_events_v1 WHERE request_id=? ORDER BY event_id", (DISCONNECT_REQUEST_ID,))]
            receipts = journal._connection.execute("SELECT COUNT(*) FROM summary_result_receipts_v1 WHERE request_id=?", (DISCONNECT_REQUEST_ID,)).fetchone()[0]
            drops = journal._connection.execute("SELECT COUNT(*) FROM summary_result_drops_v1 WHERE request_id=?", (DISCONNECT_REQUEST_ID,)).fetchone()[0]
            if (record is None or record["state"] != "sending" or record["receipt"] is not None
                    or states != ["prepared", "sending"] or receipts != 0 or drops != 0 or entries != _initial_entries(self.root)
                    or canonical_json(record["request"]) != canonical_json(self.original)):
                raise ValueError("disconnect_journal_proof_unavailable")
            self.observations.update(reaped_returncode=process.returncode, remaining_phase1_pgid_members=remaining,
                completed_artifact_absent=completed_absent, handler_done=True, state_before_unknown=record["state"],
                receipt_before_unknown=receipts, drops_before_unknown=drops, catalog_before_unknown=entries,
                child_disposal="not_applicable_process_killed")
            journal.record_summary_state(DISCONNECT_REQUEST_ID, "unknown", detail="owner_killed_after_post_before_response")
        # Reopen before replacement launch; refusal must leave no replay row.
        with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
            record = journal.get_summary_request(DISCONNECT_REQUEST_ID)
            unresolved = journal.list_unresolved_summary_requests()
            if record["state"] != "unknown" or record["receipt"] is not None or not any(
                    row["request"]["requestId"] == DISCONNECT_REQUEST_ID for row in unresolved):
                raise ValueError("disconnect_reopen_unknown_unobserved")
            replay = deepcopy(record["request"])
            self.replay_id = replay["requestId"] = str(uuid4())
            refused = False
            try:
                journal.reserve_summary_request(replay)
            except InvalidTransitionError:
                refused = True
            row_present = journal.get_summary_request(self.replay_id) is not None
            self.observations.update(reopened_unknown=True, unresolved_contains_original=True,
                replay_request_id=self.replay_id, replay_reserve_refused=refused, replay_row_present=row_present)
            if not refused or row_present:
                raise ValueError("disconnect_replay_reservation_not_refused")

    def after_result(self, completed: dict) -> None:
        with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
            original, fresh = journal.get_summary_request(DISCONNECT_REQUEST_ID), journal.get_summary_request(RESTART_REQUEST_ID)
            states = [tuple(row) for row in journal._connection.execute(
                "SELECT state,detail FROM summary_request_events_v1 WHERE request_id=? ORDER BY event_id", (DISCONNECT_REQUEST_ID,))]
            receipts = journal._connection.execute("SELECT COUNT(*) FROM summary_result_receipts_v1 WHERE request_id=?", (DISCONNECT_REQUEST_ID,)).fetchone()[0]
            drops = journal._connection.execute("SELECT COUNT(*) FROM summary_result_drops_v1 WHERE request_id=?", (DISCONNECT_REQUEST_ID,)).fetchone()[0]
            no_replay_row = self.replay_id is not None and journal.get_summary_request(self.replay_id) is None
            unresolved_contains_original = any(row["request"]["requestId"] == DISCONNECT_REQUEST_ID
                                               for row in journal.list_unresolved_summary_requests())
        a, b = original["request"], fresh["request"]
        adjacent = (a["subject"] == b["subject"] and len(a["sources"]) == len(b["sources"]) == 1
            and a["sources"][0]["source"] == b["sources"][0]["source"]
            and a["sources"][0]["end"] == b["sources"][0]["start"]
            and a["sources"][0]["end"] - a["sources"][0]["start"] == b["sources"][0]["end"] - b["sources"][0]["start"]
            and a["sources"][0]["digest"] != b["sources"][0]["digest"] and a["inputKey"] != b["inputKey"])
        case_controls = json.loads((self.root / "fixture.json").read_text()).get("caseControls")
        distinct = adjacent
        if case_controls:
            distinct = (canonical_json(a) == canonical_json(case_controls["cases"]["disconnect"]["request"])
                        and canonical_json(b) == canonical_json(case_controls["cases"]["restart"]["request"])
                        and a["inputKey"] != b["inputKey"])
        self.observations.update(final_original_state=original["state"], original_state_order=[row[0] for row in states],
            original_receipt_count=receipts, original_drop_count=drops, final_replay_row_absent=no_replay_row,
            final_unresolved_contains_original=unresolved_contains_original,
            fresh_adjacent_distinct_input=adjacent, fresh_owned_distinct_input=distinct, fresh_state=fresh["state"], phase_post_counts=self.phase_posts,
            original_postrestart_posts=0, post_OMP_restart_late="not_applicable_child_died_in_process",
            actualBackend="not_run", scheduler_restart="not_run")
        if (completed["requestId"] != RESTART_REQUEST_ID or original["state"] != "unknown" or original["receipt"] is not None
                or states != [("prepared", None), ("sending", None), ("unknown", "owner_killed_after_post_before_response")]
                or receipts != 0 or drops != 0 or not no_replay_row or not unresolved_contains_original
                or not distinct or fresh["state"] != "stored"
                or self.phase_posts != {"disconnect": 1, "between": 0, "restart": 2}):
            raise ValueError("restart_final_observation_mismatch")


def _handler(root: Path, posts: list[dict], violations: list[str], recovery: _RecoveryControl | None = None,
             restart: _RestartControl | None = None):
    per_request: dict[str, int] = {}
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def do_GET(self):
            # Catalog requests are neither MODEL prompts nor attributed C2 I/O.
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            try:
                phase = restart.phase if restart is not None else None
                if restart is not None and phase == "between":
                    restart.phase_posts[phase] += 1
                    raise ValueError("between_phase_POST_refused")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32768:
                    raise ValueError("fixture_body_limit")
                raw = self.rfile.read(size)
                body = json.loads(raw)
                tools = [tool.get("function", {}).get("name") for tool in body.get("tools", [])]
                expected = json.loads((root / "expected.json").read_text())
                suite_case = expected.get("case") not in (None, "disconnect", "restart")
                def text(content):
                    return content if isinstance(content, str) else "".join(
                        part.get("text", "") for part in content or [] if part.get("type") == "text")
                system = "\n".join(text(row.get("content")) for row in body.get("messages", [])
                                   if row.get("role") == "system")
                exact = sha256(system.encode()).hexdigest() == expected["systemSha"]
                posts.append({"path": self.path, "model": body.get("model"), "tools": tools,
                              "body_bytes": size, "body_sha256": sha256(raw).hexdigest(),
                              "system_exact": exact, "max_completion_tokens": body.get("max_completion_tokens"),
                              "case": expected.get("case"), "expected_request_id": expected["requestId"]})
                index = len(posts)
                cap = 2
                if suite_case:
                    per_request[expected["requestId"]] = per_request.get(expected["requestId"], 0) + 1
                    index = per_request[expected["requestId"]]
                    if len(posts) > 11:
                        raise ValueError("owned_case_POST_limit")
                if restart is not None and not suite_case:
                    restart.phase_posts[phase] += 1
                    index = restart.phase_posts[phase]
                    cap = 1 if phase == "disconnect" else 2
                    posts[-1].update(phase=phase, expected_request_id=expected["requestId"])
                    required = DISCONNECT_REQUEST_ID if phase == "disconnect" else RESTART_REQUEST_ID
                    if expected["requestId"] != required or len(posts) > 3:
                        raise ValueError("restart_phase_request_mismatch")
                if (self.path != "/v1/chat/completions" or body.get("model") != "worker"
                        or tools != ["db_summary_save"] or not exact or index > cap):
                    raise ValueError("unexpected_loopback_POST")
                continuation = any(row.get("role") == "tool" for row in body.get("messages", []))
                if continuation != (index == 2):
                    raise ValueError("unexpected_continuation")
                if restart is not None and phase == "disconnect":
                    if not restart.hold(expected, body):
                        violations.append("disconnect_hold_timeout")
                    return  # Deliberately no response header/body/error bytes.
                if recovery is not None and index == 1 and not suite_case:
                    recovery.before_response(expected, body, text)
                if restart is not None and phase == "restart" and not suite_case:
                    prompt = _matched_prompt(expected, body, {})
                    if not isinstance(prompt, dict) or prompt.get("request", {}).get("requestId") != RESTART_REQUEST_ID:
                        raise ValueError("restart_phase_prompt_mismatch")
                    posts[-1]["bound_request_id"] = RESTART_REQUEST_ID
                delta = {"role": "assistant", "content": "완료"} if continuation else {
                    "role": "assistant", "tool_calls": [{"index": 0, "id": "owned-save-1",
                    "type": "function", "function": {"name": "db_summary_save",
                    "arguments": json.dumps(SUMMARY, ensure_ascii=False)}}]}
                if expected.get("case") == "saved_then_bash" and continuation:
                    delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "owned-forbidden-1",
                        "type": "function", "function": {"name": "bash", "arguments": '{"command":"true"}'}}]}
                frames = [{"id": "owned-scripted", "object": "chat.completion.chunk", "created": 0,
                           "model": "worker", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {"id": "owned-scripted", "object": "chat.completion.chunk", "created": 0,
                           "model": "worker", "choices": [{"index": 0, "delta": {},
                           "finish_reason": "stop" if continuation else "tool_calls"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}]
                payload = b"".join(("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n").encode()
                                   for frame in frames) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, InvalidTransitionError) as error:
                violations.append(type(error).__name__ + ":" + str(error))
                self.send_error(400)
    return Handler


def _execute(source: Path, binary: Path, root: Path, *, recovery_control: bool = False,
             pause_control: bool = False, restart_control: bool = False, cases_control: bool = False) -> dict[str, Any]:
    restart_started = time.monotonic() if restart_control else None
    _private_root(root)
    if pause_control and not recovery_control:
        raise ValueError("pause_control_requires_recovery")
    if restart_control and not (recovery_control and pause_control):
        raise ValueError("restart_control_requires_existing_controls")
    for name in ("home", "agent", "config", "project", "data", "tmp", "xdg-config",
                 "xdg-data", "xdg-state", "xdg-cache"):
        (root / name).mkdir(mode=0o700)
    runtime = _copy_runtime(source, root)
    layout = DataLayout(root / "data")
    with TaskRepository(layout.tasks):
        pass
    layout.tasks.chmod(0o600)
    catalog = ImportantLogCatalog(layout.important_logs, create=True)
    posts: list[dict] = []
    violations: list[str] = []
    started = restart_started if restart_started is not None else time.monotonic()
    restart = _RestartControl(root, started + DEADLINE_SECONDS) if restart_control else None
    recovery = _RecoveryControl(root, pause_control=pause_control) if recovery_control else None
    server = _Server(("127.0.0.1", 0), _handler(root, posts, violations, recovery, restart))
    server.timeout = 1
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    fixture = {"root": str(root), "endpoint": endpoint, "dbHealth": catalog.health_snapshot()["active"],
               **({"pauseControl": True} if pause_control else {})}
    if cases_control:
        from workbench.backend.summary_compat_cases import create_cases
        fixture["caseControls"] = create_cases(root)
        fixture["caseControls"]["cases"]["disconnect"]["request"]["requestId"] = DISCONNECT_REQUEST_ID
        fixture["caseControls"]["cases"]["restart"]["request"]["requestId"] = RESTART_REQUEST_ID
    if restart is not None:
        fixture.update(restartControl=True, phase="disconnect")
    write_private_json(root / "fixture.json", fixture)
    _write_owned_models(root, endpoint)
    overlay = root / "overlay.yml"
    overlay.write_text("startup:\n  setupWizard: false\n")
    overlay.chmod(0o600)
    plan = LaunchPlan(ShellChoice("bash", "/bin/bash"), str(binary), "18.8.6",
                      str(runtime / "omp_bridge/g3/summary_verification.ts"))
    env = omp_environment(_environment(root), plan, role="manager", token=uuid4().hex,
                          bridge_socket=root / "absent.sock",
                          home={"PI_CODING_AGENT_DIR": str(root / "agent"), "PI_CONFIG_DIR": str(root / "config")},
                          models=_owned_model_table(runtime))
    argv = [str(binary), "--thinking", "off", "--mode", "rpc", "--no-session", "--no-title",
            "--no-extensions", "--no-skills", "--no-rules", "--no-pty", "--no-lsp", "--no-prewalk",
            "--no-tools", "--config", str(overlay), "--extension",
            str(runtime / "omp_bridge/g3/summary_verification.ts"), "--model", "ownedverify/scripted",
            "--cwd", str(root / "project")]
    result = unavailable("owned_verification_incomplete")
    process = None
    ref = None
    completed = None
    error = None
    streams = []
    all_streams = []
    owned = []
    handler_threads = ()
    if restart is None:
        started = time.monotonic()

    def launch():
        opened = [(root / name).open("wb") for name in ("stdout.log", "stderr.log")]
        all_streams.extend(opened)
        write_private_json(root / "launch.json", {"launch_requested": True})
        child = subprocess.Popen(argv, cwd=root / "project", env=env, stdin=subprocess.PIPE,
                                 stdout=opened[0], stderr=opened[1], start_new_session=True)
        identity = {"pid": child.pid, "pgid": child.pid, "start_ticks": None}
        owned.append((child, identity))
        result["level"] = "inconclusive"
        result["io"] = dict.fromkeys(IO_KEYS)
        result["io"].update(omp_children=len(owned))
        if restart is not None:
            identity["phase"] = restart.phase
        identity["start_ticks"] = _identity(child.pid)
        if identity["start_ticks"] is None:
            for stream in opened:
                stream.close()
            raise RuntimeError("owned_process_identity_unavailable")
        identity["binary_sha256"] = sha256(binary.read_bytes()).hexdigest()
        write_private_json(root / "launch.json", identity)
        child.stdin.write(b'{"id":"owned-state","type":"get_state"}\n')
        child.stdin.flush()
        return child, identity, opened

    thread.start()
    try:
        process, ref, streams = launch()
        if restart is not None:
            while not restart.post_seen.wait(timeout=min(0.02, max(0, restart.deadline - time.monotonic()))):
                if violations:
                    raise ValueError(violations[0])
                if (root / "completed.json").exists():
                    raise ValueError("disconnect_completed_before_POST")
                if process.poll() is not None:
                    raise RuntimeError("disconnect_OMP_exited_before_POST")
                if time.monotonic() >= restart.deadline:
                    raise TimeoutError("owned_verification_timeout")
            if time.monotonic() >= restart.deadline or not _signal_leader(process, ref, signal.SIGKILL):
                raise ValueError("disconnect_owned_identity_unavailable")
            process.wait(timeout=max(0.001, min(3, restart.deadline - time.monotonic())))
            restart.release.set()
            restart.confirm_unknown(process, ref, catalog)
            restart.phase = "between"
            try:
                process.stdin.close()
            except OSError:
                pass
            for stream in streams:
                stream.close()
            streams = []
            destination = root / "phases/disconnect"
            destination.mkdir(mode=0o700, parents=True)
            artifacts = {}
            for name in ("stdout.log", "stderr.log", "launch.json", "expected.json", "fixture.json"):
                target = destination / name
                (root / name).rename(target)
                data = target.read_bytes()
                artifacts[name] = {"path": str(target.relative_to(root)), "bytes": len(data), "sha256": sha256(data).hexdigest()}
                if name in {"launch.json", "expected.json", "fixture.json"}:
                    artifacts[name]["facts"] = json.loads(data)
            restart.observations["phase1_artifacts"] = artifacts
            if violations or time.monotonic() >= restart.deadline:
                raise ValueError("restart_preparation_unavailable")
            restart.phase = "restart"
            fixture["phase"] = "restart"
            write_private_json(root / "fixture.json", fixture)
            process, ref, streams = launch()
        while True:
            if (root / "completed.json").exists():
                completed = json.loads((root / "completed.json").read_text())
                break
            if violations:
                raise ValueError(violations[0])
            if process.poll() is not None:
                raise RuntimeError("owned_OMP_exited:" + str(process.returncode))
            if time.monotonic() - started >= DEADLINE_SECONDS:
                raise TimeoutError("owned_verification_timeout")
            time.sleep(0.02)
        if completed.get("outcome", {}).get("status") == "summary_result":
            with TaskRepository(layout.tasks) as journal:
                record = journal.get_summary_request(completed["requestId"])
            with ImportantLogStore.open(layout.important_logs, catalog.active_uuid(), readonly=True) as store:
                entry = store.get_entry(completed["outcome"]["writer"]["entryId"])
                if cases_control:
                    count = store._connection.execute("SELECT COUNT(*) FROM entries WHERE kind='cumulative' AND subject_kind=? AND subject_id=?",
                        (record["request"]["subject"]["kind"], record["request"]["subject"]["id"])).fetchone()[0]
                else:
                    count = store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            completed["writerObserved"] = {"catalog_entries": count, "request_state": record["state"],
                                            "receipt": record["receipt"], "actual_body": entry["body"]}
            request = record["request"]
            outcome = completed["outcome"]
            receipt = record["receipt"]
            binding_matches = (request["requestId"] == completed["requestId"]
                and all(request[name] == outcome["result"][name]
                        for name in ("requestId", "inputKey", "skillDigest", "sources"))
                and entry["subject"] == request["subject"] and entry["sources"] == request["sources"]
                and entry["sourceGaps"] == request["sourceGaps"])
            receipt_matches = (receipt is not None and receipt["request_id"] == completed["requestId"]
                and receipt["entry_id"] == outcome["writer"]["entryId"]
                and receipt["result_digest"] == sha256(canonical_json(outcome["result"])).hexdigest())
            completed["writerObserved"].update(request_binding_matches=binding_matches,
                                               receipt_matches=receipt_matches)
            if (record["state"] != "stored" or not receipt_matches or not binding_matches or count != 1
                    or entry["body"] != {"schemaVersion": 1,
                        "facts": ["소유 private scripted fixture의 연결을 확인했다."],
                        "interpretations": [], "unknowns": ["실제 MODEL 및 전체 Q01은 미검증이다."]}):
                raise ValueError("owned_writer_observation_mismatch")
            if recovery is not None:
                recovery.after_result(completed, catalog)
            if restart is not None:
                restart.after_result(completed)
            if cases_control:
                from workbench.backend.summary_compat_cases import origin_controls, result_controls
                with TaskRepository(layout.tasks) as journal:
                    with ImportantLogStore.open(layout.important_logs, catalog.active_uuid(), readonly=True) as store:
                        for case in completed.get("caseControls", []):
                            row = journal.get_summary_request(case["requestId"])
                            case["receiptAbsent"] = row is not None and row["receipt"] is None
                            case["journalState"] = row["state"] if row else None
                            case["writerEntries"] = store._connection.execute(
                                "SELECT COUNT(*) FROM entries WHERE kind='cumulative' AND subject_kind=? AND subject_id=?",
                                (row["request"]["subject"]["kind"], row["request"]["subject"]["id"])).fetchone()[0] if row else None
                completed["caseHost"] = {"result": result_controls(root, completed),
                    "origin": origin_controls(root, _environment(root))}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, TimeoutError, sqlite3.Error, InvalidTransitionError) as failure:
        error = type(failure).__name__ + ":" + str(failure)
    finally:
        if restart is not None:
            restart.release.set()  # Event wait must be released before server_close join.
        for child, identity in owned:
            try:
                cleanup_error = _stop_leader(child, identity)
                if cleanup_error:
                    error = cleanup_error
            except ProcessLookupError:
                error = "owned_cleanup_identity_lost"
            except (OSError, subprocess.TimeoutExpired) as failure:
                error = "owned_cleanup_incomplete:" + str(failure)
            if child.stdin:
                try:
                    child.stdin.close()
                except OSError:
                    pass
        for stream in all_streams:
            stream.close()
        server.shutdown()
        server.shutdown_accepted()
        # ThreadingMixIn owns and joins this non-daemon request thread list.
        if isinstance(server._threads, list):
            handler_threads = tuple(server._threads)
        server.server_close()
        thread.join(timeout=3)
    remaining_by_phase = [{"owned_process": identity, "remaining_pgid_members": _remaining(identity["pgid"])} for child, identity in owned]
    remaining = [pid for row in remaining_by_phase for pid in row["remaining_pgid_members"]]
    handlers_alive = sum(handler.is_alive() for handler in handler_threads)
    sockets_remaining = server.accepted_count()
    cleanup_complete = (not remaining and not thread.is_alive() and not handlers_alive and not sockets_remaining
                        and all(child.returncode is not None for child, identity in owned)
                        and not (error or "").startswith(("owned_cleanup", "owned_process_identity")))
    # Even a successful local C1 control has no complete Q01 item or C2 proof.
    if owned or not cleanup_complete:
        result["level"] = "inconclusive"
        result["io"].update(model_calls=len(posts), network_requests=None)
        result["unknowns"] = ["complete_q01_unverified", "observer_child_io_unattributed",
                              "operating_session_unavailable", "output_budget_unavailable"]
        result["observations"] = [{"kind": "owned_C1_partial", "result": "inconclusive",
            "owned_process": ref, "remaining_pgid_members": remaining, "cleanup_complete": cleanup_complete,
            "server_thread_alive": thread.is_alive(),
            "http_handler_threads_alive": handlers_alive, "http_sockets_remaining": sockets_remaining,
            "localhost_model_posts": posts, "credential_environment_entries": 0,
            "completed": completed, "error": error, "elapsed_seconds": time.monotonic() - started}]
        if cases_control and completed:
            result["model_metadata"] = completed.get("operatingMetadata", result["model_metadata"])
            result["omp"] = {"version": completed.get("version"), "binary_sha256": sha256(binary.read_bytes()).hexdigest(),
                             "bundled_sha256": completed.get("definitionDigest")}
            if not error and cleanup_complete and completed.get("caseControls"):
                result["unknowns"] = ["observer_child_io_unattributed"]
        if recovery is not None:
            result["observations"][0]["recovery_control"] = recovery.observations
        if restart is not None:
            restart.observations.update(phase_post_counts=restart.phase_posts)
            result["observations"][0].update(restart_control=restart.observations,
                                             owned_processes=[identity for child, identity in owned],
                                             remaining_by_phase=remaining_by_phase)
        if completed:
            exercised = completed.get("exercised", [])
            result["items"] = {name: "inconclusive" for name in exercised}
            if completed.get("error"):
                result["unknowns"].append(completed["error"])
            outcome = completed.get("outcome", {})
            if outcome.get("reason"):
                result["unknowns"].append(outcome["reason"])
            for key in ("parentUnchanged", "registryUnchanged", "settingsUnchanged", "childDisposed"):
                if completed.get(key) is False:
                    result["unknowns"].append(key + "_false")
        if recovery is not None and recovery.observations:
            result["items"]["Q01-recovery"] = "inconclusive"
        if restart is not None and restart.observations:
            result["items"]["Q01-recovery"] = "inconclusive"
        if error:
            result["unknowns"].append(error)
        if not cleanup_complete:
            result["unknowns"].append("owned_cleanup_incomplete")
            result["observations"][0]["retained_private_root"] = str(root)
    elif error:
        result["unknowns"] = [error]
    result["item_rows"] = _item_rows(result)
    write_private_json(root / "observation.json", result)
    return result


def observe_owned(source: Path, *, recovery_control: bool = False, pause_control: bool = False,
                  restart_control: bool = False, cases_control: bool = False) -> dict[str, Any]:
    binary_name = shutil.which("omp")
    if not binary_name or sys.platform != "linux":
        return unavailable("owned_runtime_unavailable")
    binary = Path(binary_name).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return unavailable("owned_runtime_unavailable")
    temporary = None
    result = None
    try:
        temporary = Path(tempfile.mkdtemp(prefix="workbench-summary-verify-")).resolve()
        options = {"recovery_control": recovery_control}
        if pause_control:
            options["pause_control"] = True
        if restart_control:
            options["restart_control"] = True
        if cases_control:
            options["cases_control"] = True
        result = _execute(source, binary, temporary, **options)
        return result
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, InvalidTransitionError) as error:
        result = unavailable(type(error).__name__ + ":" + str(error))
        if temporary is not None and (temporary / "launch.json").exists():
            # A returned exception cannot turn an attempted launch into zero I/O.
            result.update(level="inconclusive", io=dict.fromkeys(IO_KEYS), observations=[{
                "kind": "owned_C1_partial", "result": "inconclusive", "cleanup_complete": False,
                "retained_private_root": str(temporary), "error": result["unknowns"][0]}])
            result["unknowns"].append("owned_cleanup_incomplete")
        return result
    finally:
        # An ambiguous cleanup retains private evidence; no numeric orphan kill.
        if temporary is not None and (result is not None or not (temporary / "launch.json").exists()):
            retained = any(row.get("retained_private_root") for row in (result or {}).get("observations", []))
            if not retained:
                shutil.rmtree(temporary)


def _reserve(root: Path) -> None:
    # Only the product-owned entry uses this bounded stdin/file seam. No code or
    # database destination comes from the scripted model tool arguments.
    _private_root(root)
    if Path(os.environ.get("HOME", "")).resolve() != root / "home":
        raise ValueError("unsafe_verification_root")
    request = decode_json(sys.stdin.buffer.read(32769), max_bytes=32768)
    if json.loads((root / "fixture.json").read_text()).get("caseControls"):
        from workbench.backend.summary_compat_cases import current
        if not current(root, request):
            raise ValueError("owned_case_authority_unavailable")
    with TaskRepository(DataLayout(root / "data").tasks) as journal:
        journal.reserve_summary_request(request)
        journal.record_summary_state(request["requestId"], "sending")


def _pause_request(root: Path) -> dict:
    _private_root(root)
    if Path(os.environ.get("HOME", "")).resolve() != root / "home":
        raise ValueError("unsafe_verification_root")
    return parse_summary_request(decode_json(sys.stdin.buffer.read(32769), max_bytes=32768))


def _hold(root: Path) -> None:
    request = _pause_request(root)
    with TaskRepository(DataLayout(root / "data").tasks) as journal:
        journal.reserve_summary_request(request)


def _confirm_not_sent(root: Path) -> None:
    request = _pause_request(root)
    with TaskRepository(DataLayout(root / "data").tasks) as journal:
        stored = journal.get_summary_request(request["requestId"])
        if (stored is None or stored["state"] != "prepared" or stored["receipt"] is not None
                or canonical_json(stored["request"]) != canonical_json(request)):
            raise ValueError("pause_confirmation_binding_mismatch")
        journal.record_summary_state(request["requestId"], "not_sent", detail=PAUSE_NO_SEND_DETAIL)


if __name__ == "__main__":
    modes = {"--reserve": _reserve, "--hold": _hold, "--confirm-not-sent": _confirm_not_sent}
    if len(sys.argv) != 3 or sys.argv[1] not in modes:
        raise SystemExit(2)
    modes[sys.argv[1]](Path(sys.argv[2]))
