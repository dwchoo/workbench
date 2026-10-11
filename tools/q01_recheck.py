"""Development Q01 runner port. Live execution requires an explicit --run.

The fixed product port is supplied by DVL-09. Its absence is unavailable, never
a probe-local or product pass. This tool and its verdict rules are frozen before
any Q01 runtime result is judged.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / ".agents/skills/workflow-ledger/scripts"))
sys.path.insert(0, str(ROOT / "tests/backend"))
sys.path.insert(0, str(ROOT / "src"))
import workflow_tools as common  # noqa: E402
from summary_probe_support import Q01_ITEMS, collect_projection, same_projection  # noqa: E402


# Each key must be established by an observed event/measurement, not a model claim.
REQUIRED_FACTS = {
    "Q01-route": ("existing_agent", "isolated_turn", "typed_return", "no_new_agent", "no_standing_process"),
    "Q01-tools": ("receipt_request_bound", "session_turn_epoch_bound",
                  "receipt_route_bound", "policy_nonce_bound", "startup_private_config_verified",
                  "startup_sole_db_tool_readback", "startup_guard_effective_verified",
                  "startup_missing_guard_prompt_sent_zero", "startup_noop_guard_prompt_sent_zero",
                  "startup_api_mismatch_prompt_sent_zero", "startup_stale_receipt_prompt_sent_zero",
                  "startup_failed_session_disposed", "startup_rebuild_once_reverified",
                  "startup_repeated_failure_prompt_sent_zero",
                  "startup_repeated_failure_summary_deferred",
                  "startup_repeated_failure_main_continues", "runtime_binding_pre_dispatch",
                  "runtime_request_mismatch_sent_zero", "runtime_roster_mismatch_sent_zero",
                  "runtime_model_mismatch_sent_zero", "runtime_policy_mismatch_sent_zero",
                  "malicious_tools_blocked", "sentinel_unchanged",
                  "post_receipt_violation_rejected", "forbidden_event_immediate_protection",
                  "upper_turn_unaffected",
                  "normal_permissions_restored", "no_extra_model_turn"),
    "Q01-skill": ("exact_bytes_in_context", "digest_matched", "role_filter_enforced", "isolated_skill_source"),
    "Q01-result": ("typed_channel", "wrong_id_rejected", "wrong_revision_rejected",
                   "wrong_range_rejected", "duplicate_rejected"),
    "Q01-context": ("three_turns_observed", "parent_receipt_within_4k", "result_within_8k",
                    "body_within_32k", "collector_within_1m", "token_preflight",
                    "output_ceiling_enforced", "full_output_reserved", "no_main_raw_accumulation"),
    "Q01-priority": ("busy_deferred", "user_priority", "due_check_priority",
                     "end_priority", "max_block_recorded"),
    "Q01-recovery": ("pause_blocks_new", "disconnect_reconciled", "restart_reconciled",
                     "late_result_bound", "unknown_not_replayed"),
    "Q01-origin": ("task_grant_bound", "command_start_bound", "work_source_ranges_bound",
                   "terminal_close_bound", "unrelated_scope_rejected", "notice_not_authority",
                   "user_role_not_authority", "pause_revoke_preserved", "end_once_no_replay"),
}
TRUSTED_SOURCES = {"omp_event", "backend_event", "product_adapter"}


def _monotonic_ns(value: object) -> bool:
    # Native clock representation, not a product latency/deadline limit.
    return type(value) is int and 0 < value <= 2**63 - 1


def _finite_number(value: object) -> bool:
    # math.isfinite converts ints to float and can itself overflow.
    return ((type(value) is int and abs(value) <= 2**63 - 1) or
            (type(value) is float and math.isfinite(value)))


def nonshared_priority_issues(measurement: object) -> list[str]:
    """Validate observed delivery/turn windows, never infer a mutex duration.

    Runtime custody is checked by the collector. This boundary also rejects
    flag-only assertions and internally inconsistent copied measurements.
    """
    if not isinstance(measurement, dict):
        return ["nonshared_measurement_missing"]
    issues = []

    def require(condition: bool, name: str) -> None:
        if not condition:
            issues.append("nonshared_" + name)

    require(measurement.get("kind") == "delivery_turn_overlap", "measurement_kind")
    require(measurement.get("clock") == "time.monotonic_ns", "clock")
    sessions = measurement.get("session_ids")
    sessions = sessions if isinstance(sessions, dict) else {}
    ids = [sessions.get(role) for role in ("manager", "worker", "child")]
    require(all(isinstance(value, str) and value.strip() for value in ids) and
            len(set(value for value in ids if isinstance(value, str))) == 3,
            "distinct_session_ids")
    hold = measurement.get("child_hold_interval_ns")
    valid_hold = (isinstance(hold, list) and len(hold) == 2 and
                  all(_monotonic_ns(value) for value in hold) and hold[0] < hold[1])
    require(valid_hold, "child_hold_interval")
    require(measurement.get("internal_lock_wait") == "unavailable", "internal_lock_limitation")
    require(measurement.get("due_scheduler_status") == "dispatched", "due_scheduler")
    windows = measurement.get("notice_windows")
    if not isinstance(windows, list) or len(windows) != 2 or not all(
            isinstance(window, dict) for window in windows):
        return issues + ["nonshared_two_notice_windows"]
    require({(row.get("role"), row.get("kind")) for row in windows
             if isinstance(row.get("role"), str) and isinstance(row.get("kind"), str)} ==
            {("manager", "worker_terminal_done"), ("worker", "terminal_check")}, "notice_roles")
    notice_ids = [window.get("notice_id") for window in windows]
    require(all(isinstance(value, str) and value.strip() for value in notice_ids) and
            len(set(value for value in notice_ids if isinstance(value, str))) == 2, "notice_ids")
    request_ids = [window.get("ack_request_id") for window in windows]
    require(all(isinstance(value, str) and value.strip() for value in request_ids) and
            len(set(value for value in request_ids if isinstance(value, str))) == 2, "request_ids")
    starts, posts = [], []
    for row in windows:
        role, notice_id = row.get("role"), row.get("notice_id")
        require(role in ("manager", "worker") and row.get("session_id") == sessions.get(role),
                "notice_session_binding")
        require(row.get("ack_status") == "api_accepted" and row.get("provider_stage") == "parent"
                and row.get("ack_notice_id") == notice_id and row.get("provider_notice_id") == notice_id
                and isinstance(row.get("ack_request_id"), str) and bool(row["ack_request_id"].strip()),
                "notice_attribution")
        if role == "worker":
            require(row.get("scheduler_notice_id") == notice_id, "due_notice_binding")
        names = ("enqueue_ns", "frame_request_ns", "ack_observed_ns", "agent_start_ns",
                 "provider_post_ns", "agent_end_ns")
        values = [row.get(name) for name in names]
        valid_order = (all(_monotonic_ns(value) for value in values) and
                       values == sorted(values))
        require(valid_order, "monotonic_window")
        if not valid_order or not valid_hold:
            continue
        enqueue, _, ack, start, post, end = values
        overlap = min(end, hold[1]) - max(start, hold[0])
        require(hold[0] <= enqueue <= start <= post < hold[1] and overlap > 0, "held_child_overlap")
        derived = {"enqueue_to_ack_ms": (ack - enqueue) / 1_000_000,
                   "enqueue_to_start_ms": (start - enqueue) / 1_000_000,
                   "enqueue_to_post_ms": (post - enqueue) / 1_000_000,
                   "turn_hold_overlap_ms": max(0, overlap) / 1_000_000}
        for name, value in derived.items():
            measured = row.get(name)
            require(_finite_number(measured) and measured == value,
                    "derived_" + name)
        starts.append(derived["enqueue_to_start_ms"])
        posts.append(derived["enqueue_to_post_ms"])
    for name, values in (("max_enqueue_to_start_ms", starts), ("max_enqueue_to_post_ms", posts)):
        measured = measurement.get(name)
        require(len(values) == 2 and _finite_number(measured) and
                measured == max(values), "derived_" + name)
    return sorted(set(issues))


def judge_observations(observations: list[dict], *, adapter: str) -> dict[str, dict]:
    """Check collector facts, not their real-world truth.

    The fixed runtime collector must derive facts from bounded, attributable
    OMP/backend events and measurements. Model self-report and terminal text
    are never trusted runtime sources. Synthetic rows exercise Q01-validator
    fixture behavior only; they cannot supply Q01's eight runtime checks.
    """
    if adapter not in ("probe-local", "product"):
        raise ValueError("unsupported adapter")
    by_id: dict[str, list[dict]] = {name: [] for name in Q01_ITEMS}
    for observation in observations:
        if not isinstance(observation, dict) or observation.get("id") not in by_id:
            raise ValueError("unowned or malformed Q01 observation")
        by_id[observation["id"]].append(observation)
    verdicts = {}
    for item_id in Q01_ITEMS:
        found = by_id[item_id]
        if len(found) != 1:
            verdicts[item_id] = {"result": "not_run", "unknowns": ["missing_or_duplicate_observation"]}
            continue
        row = found[0]
        facts = row.get("facts")
        unknowns = row.get("unknowns")
        if row.get("adapter") != adapter or row.get("source") not in TRUSTED_SOURCES or (
                adapter == "product" and row.get("source") != "product_adapter") or (
                not isinstance(facts, dict) or not isinstance(unknowns, list)
                or any(not isinstance(x, str) for x in unknowns)):
            verdicts[item_id] = {"result": "not_run", "unknowns": ["untrusted_or_malformed_observation"]}
            continue
        required = REQUIRED_FACTS[item_id]
        if item_id == "Q01-priority" and facts.get("shared_session_or_lock") is False:
            required = tuple(name for name in required if name != "max_block_recorded")
        missing = [name for name in required if facts.get(name) is not True]
        if item_id == "Q01-priority":
            shared = facts.get("shared_session_or_lock")
            measured = facts.get("measured_block_ms")
            if type(shared) is not bool:
                missing.append("sharing_unobserved")
            if shared is True:
                if type(measured) is not int or measured < 0:
                    missing.append("max_block_unmeasured")
                if not isinstance(facts.get("user_adoption_ref"), str) or not facts["user_adoption_ref"]:
                    missing.append("shared_route_adoption")
            elif shared is False:
                if facts.get("independent_delivery_observed") is not True:
                    missing.append("independent_delivery_unobserved")
                if measured is not None or facts.get("max_block_recorded") is not None:
                    missing.append("nonshared_mutex_duration_inferred")
                missing.extend(nonshared_priority_issues(facts.get("nonshared_priority_measurement")))
        if row.get("evidence_level") != "runtime":
            missing.append("runtime_evidence")
        if not row.get("evidence_ref") or not isinstance(row["evidence_ref"], str):
            missing.append("evidence_ref")
        if unknowns:
            missing.extend(unknowns)
        if missing:
            result = ("blocked_adoption" if missing == ["shared_route_adoption"]
                      else "inconclusive")
            if any(facts.get(name) is False for name in required):
                result = "failed"
            if item_id == "Q01-priority" and facts.get("independent_delivery_observed") is False:
                result = "failed"
            verdicts[item_id] = {"result": result, "unknowns": sorted(set(missing))}
        else:
            verdicts[item_id] = {"result": "passed", "unknowns": [],
                                 "evidence_ref": row["evidence_ref"]}
    return verdicts


def judge_envelope(envelope: dict, *, adapter: str) -> dict[str, dict]:
    """Adapter-level uncertainty applies to every Q01 item, including positive rows."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("observations"), list):
        raise ValueError("adapter returned invalid Q01 observation envelope")
    unknowns = envelope.get("unknowns")
    if (not isinstance(unknowns, list)
            or any(not isinstance(value, str) or not value.strip() for value in unknowns)):
        raise ValueError("adapter returned malformed Q01 unknowns")
    verdicts = judge_observations(envelope["observations"], adapter=adapter)
    if unknowns:
        for verdict in verdicts.values():
            if verdict["result"] == "passed":
                verdict["result"] = "inconclusive"
                verdict.pop("evidence_ref", None)
            verdict["unknowns"] = sorted(set(verdict["unknowns"] + unknowns))
    return verdicts


def _private_output(root: Path, target: Path) -> Path:
    target = target.resolve()
    allowed = root / ".workflow/dvl-local"
    if not target.is_relative_to(allowed):
        raise ValueError("output must be under local operational store")
    allowed.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(allowed, 0o700)
    os.chmod(target.parent, 0o700)
    if target.exists():
        raise ValueError("refuse to overwrite Q01 output: " + str(target))
    return target


def _isolated_context(value: dict) -> None:
    base = Path(value["isolated_root"]).resolve(strict=True)
    for key in ("home", "project", "data", "socket_dir"):
        path = Path(value[key]).resolve(strict=True)
        if not path.is_relative_to(base) or path == base:
            raise ValueError("runtime namespace escapes isolated root: " + key)
    if any(key in value for key in ("credential", "auth_store", "api_key")):
        raise ValueError("credential material is forbidden in Q01 context")


def _collect(adapter: str, context: dict) -> dict:
    """Read-only port availability check used by fixture tests.

    Runtime uses prepare, collect and close as one owned operation below.
    """
    module_name = ("live_summary_feasibility" if adapter == "probe-local"
                   else "workbench.backend.summary_compat_adapter")
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        return {"observations": [], "unknowns": ["adapter_unavailable:" + module_name],
                "detail": type(exc).__name__}
    return {"observations": [], "unknowns": ["adapter_not_executed"]}


def _load_port(adapter: str):
    """Fixed adapter protocol, shared by probe-local and product modes.

    prepare_q01_context(context) -> prepared mapping with observed route_identity;
    collect_q01_observations(prepared) -> {observations, unknowns};
    close_q01_context(prepared) -> None. Preparation must not send a model turn.
    Preparation owns partial resource cleanup if it raises before returning.
    Once it returns, the runner closes it after every validation, projection,
    collection or output failure. The collector must observe real request/turn
    events and emit bounded provenance; model/terminal claims are input data.
    Product code supplies these functions in its fixed module and does not
    import this development file. Each observation contains id, adapter,
    source, evidence_level, evidence_ref, facts and unknowns.
    """
    module_name = ("live_summary_feasibility" if adapter == "probe-local"
                   else "workbench.backend.summary_compat_adapter")
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    if not all(callable(getattr(module, name, None)) for name in (
            "prepare_q01_context", "collect_q01_observations", "close_q01_context")):
        return None
    return module


def run(*, root: Path, requirements: dict, adapter: str, context: dict,
        omp_executable: Path, omp_bundle_identity: str, output: Path, log: Path,
        projection_output: Path, snapshot_output: Path) -> dict:
    _isolated_context(context)
    full_identity = common.requirements(requirements)
    required = {item["id"]: item for item in requirements["gate_items"] if item["id"] in Q01_ITEMS}
    if set(required) != set(Q01_ITEMS):
        raise ValueError("full requirements missing Q01 items")
    before = common.snapshot(root, exclude=(".workflow",))
    port = _load_port(adapter)
    if port is not None:
        prepared = port.prepare_q01_context(context)
        try:
            if not isinstance(prepared, dict) or not isinstance(prepared.get("route_identity"), dict):
                raise ValueError("adapter did not read back prepared route identity")
            route_identity = prepared["route_identity"]
            projection_before = collect_projection(root, omp_executable=omp_executable,
                                                   omp_bundle_identity=omp_bundle_identity,
                                                   route_identity=route_identity)
            started = datetime.now(timezone.utc).isoformat()
            envelope = port.collect_q01_observations(prepared)
        finally:
            port.close_q01_context(prepared)
    else:
        route_identity = None
        projection_before = collect_projection(root, omp_executable=omp_executable,
                                               omp_bundle_identity=omp_bundle_identity,
                                               route_identity=route_identity)
        started = datetime.now(timezone.utc).isoformat()
        envelope = {"observations": [], "unknowns": ["adapter_port_unavailable:" + adapter]}
    verdicts = judge_envelope(envelope, adapter=adapter)
    after = common.snapshot(root, exclude=(".workflow",))
    projection_after = collect_projection(root, omp_executable=omp_executable,
                                          omp_bundle_identity=omp_bundle_identity,
                                          route_identity=route_identity)
    projection_ok, projection_issues = same_projection(projection_before, projection_after)
    if not projection_ok:
        for item in verdicts.values():
            item["result"] = "inconclusive"
            item.pop("evidence_ref", None)
            item["unknowns"] = sorted(set(item["unknowns"] + projection_issues))
    complete = all(v["result"] == "passed" for v in verdicts.values())
    complete = complete and before["stable"] and after["stable"] and before["candidate"] == after["candidate"]
    log_text = json.dumps({"adapter": adapter, "observations": envelope["observations"],
                           "verdicts": verdicts,
                           "adapter_unknowns": envelope.get("unknowns", []),
                           "projection_issues": projection_issues}, sort_keys=True, ensure_ascii=False)
    if len(log_text.encode()) > 65536:
        raise ValueError("Q01 bounded observation log exceeded 64 KiB")
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(log_text + "\n")
    record = {
        "schema_version": 1, "check_id": "Q01-" + adapter,
        "requirements_identity": full_identity,
        "argv": [sys.executable, str(Path(__file__).resolve()), "--run", "--adapter", adapter],
        "cwd": str(root), "environment": {"adapter": adapter, "omp": str(omp_executable)},
        "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
        "exit_code": 0 if complete else 2, "timed_out": False,
        "log": str(log), "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "before": before["candidate"], "after": after["candidate"],
        "stable": before["stable"] and after["stable"] and before["candidate"] == after["candidate"],
        "items": [
            {"id": name, "acceptance_ids": required[name]["acceptance_ids"],
             "result": "passed" if verdicts[name]["result"] == "passed" else "failed",
             "observed_evidence_level": "runtime" if verdicts[name]["result"] == "passed" else "fixture",
             "evidence": verdicts[name].get("evidence_ref", "Q01 " + verdicts[name]["result"]),
             "unknowns": verdicts[name]["unknowns"]}
            for name in Q01_ITEMS],
    }
    for target, value in ((output, record),
                          (projection_output, {**projection_before, "candidate_id": before["candidate"]}),
                          (snapshot_output, before)):
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")
    return {"passed": complete, "check": str(output), "log": str(log),
            "projection": str(projection_output), "snapshot": str(snapshot_output),
            "verdicts": verdicts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="explicitly execute the isolated adapter")
    parser.add_argument("--adapter", choices=("probe-local", "product"), required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--omp", type=Path, required=True)
    parser.add_argument("--omp-bundle-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--projection-output", type=Path, required=True)
    parser.add_argument("--snapshot-output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.run:
        print(json.dumps({"passed": False, "reason": "runtime execution requires --run"}))
        return 2
    try:
        root = args.root.resolve(strict=True)
        if root != ROOT:
            raise ValueError("q01_recheck must run from its own checkout")
        output = _private_output(root, args.output)
        log = _private_output(root, args.log)
        projection_output = _private_output(root, args.projection_output)
        snapshot_output = _private_output(root, args.snapshot_output)
        if len({output, log, projection_output, snapshot_output}) != 4:
            raise ValueError("Q01 output paths must be distinct")
        report = run(root=root, requirements=json.loads(args.requirements.read_text()),
                     adapter=args.adapter, context=json.loads(args.context.read_text()),
                     omp_executable=args.omp, omp_bundle_identity=args.omp_bundle_sha256,
                     output=output, log=log, projection_output=projection_output,
                     snapshot_output=snapshot_output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"passed": False, "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
