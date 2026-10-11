"""Observed Q01 probe-local port over private installed OMP and backend contracts.

This collector reports missing operating-model, priority, recovery and origin
evidence as unknown. It never turns scripted metadata into an operating claim.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from workbench.contracts.logs_v1 import canonical_json, parse_summary_request, parse_summary_result, summary_input_key  # noqa: E402
from workbench.tasks.repository import InvalidTransitionError, TaskRepository  # noqa: E402

from . import run_full, run_prepare

HERE = Path(__file__).resolve().parent
BODY = HERE.parent / "q01_sdk_private_probe/agent-body.txt"
SKILL = HERE.parent / "q01_sdk_private_probe/summary-skill.txt"
WORK = ROOT / ".workflow/dvl-local/q01-contracts-reviewfix01"
ITEMS = ("Q01-route", "Q01-tools", "Q01-skill", "Q01-result", "Q01-context",
         "Q01-priority", "Q01-recovery", "Q01-origin")


def _sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _event(trace: list[dict], kind: str, case: str | None = None) -> list[dict]:
    return [row for row in trace if row.get("event") == kind and (case is None or row.get("case") == case)]


def _ensure_private_context(context: dict) -> None:
    if type(context) is not dict:
        raise ValueError("Q01 context must be a mapping")
    base = Path(context["isolated_root"]).resolve(strict=True)
    if not base.is_relative_to(ROOT / ".workflow/dvl-local"):
        raise ValueError("Q01 isolated root must be in private operational store")
    for name in ("home", "project", "data", "socket_dir"):
        path = Path(context[name]).resolve(strict=True)
        if path == base or not path.is_relative_to(base):
            raise ValueError("Q01 private path escapes isolated root: " + name)
    if any(name in context for name in ("credential", "auth_store", "api_key")):
        raise ValueError("credential material is forbidden")


def prepare(context: dict) -> dict:
    """Create/dispose one real private child and read model/roster without POST."""
    _ensure_private_context(context)
    WORK.mkdir(parents=True, exist_ok=True, mode=0o700)
    out = WORK / ("prepare-" + str(uuid4()))
    evidence = run_prepare.run(out)
    if evidence["provider_posts"] or not all(evidence["checks"].values()):
        raise RuntimeError("prepare sent a model request or failed private readback")
    readback = _event(evidence["trace"], "readback")[0]
    if readback.get("agentDefinition") != {"name": "task", "source": "bundled", "filePath": "embedded:task.md"}:
        raise RuntimeError("installed bundled Task definition was not read back")
    if readback.get("fixtureMatches") or readback.get("fixtureSha") != _sha(BODY):
        raise RuntimeError("bundled Task and historical fixture provenance is ambiguous")
    model = readback["model"]
    harness_files = (HERE / "full_probe.py", HERE / "full_extension.ts", HERE / "role_admission.ts", HERE / "run_full.py",
                     HERE / "prepare_extension.ts", HERE / "run_prepare.py",
                     HERE / "contract_extension.ts", HERE / "run_contract.py")
    harness_digest = sha256("".join(_sha(p) for p in harness_files).encode()).hexdigest()
    route_identity = {
        "route": "existing-parent/private-sdk-child", "role": "Manager/task",
        "agent_definition_sha256": readback["bodySha"], "skill_sha256": readback["skillSha"],
        "resolved_model": model["provider"] + "/" + model["id"], "provider": model["provider"],
        "context_limit_tokens": model["contextWindow"],
        "harness": "q01-full-" + harness_digest, "selection_epoch": readback["sessionId"],
        "source": "omp_readback",
    }
    return {"route_identity": route_identity,
            "agent_definition_provenance": {"name": "task", "source": "bundled", "filePath": "embedded:task.md",
                                            "bodySha": readback["bodySha"], "fixtureSha": readback["fixtureSha"]},
            "prepare_evidence": str(out / "prepare-evidence.json"),
            "prepare_model": {key: model.get(key) for key in ("provider", "id", "api", "contextWindow", "maxTokens")},
            "prepare_thinking": readback.get("thinkingLevel"),
            "prepare_tier": readback.get("serviceTierByFamily"),
            "harness_sha": harness_digest, "runtime_output": str(WORK / ("runtime-" + str(uuid4()))),
            "contract_runtime_output": str(WORK / ("contract-" + str(uuid4()))),
            "closed": False}


def _verified_runtime(evidence: dict, path: Path) -> list[str]:
    issues = []
    if evidence.get("stop_reason") is not None:
        issues.append("runtime_stop:" + str(evidence.get("stop_reason")))
    expected_sources = {str(HERE / "full_extension.ts"), str(HERE / "role_admission.ts"), str(HERE / "run_full.py"),
                        str(BODY), str(SKILL),
                        str(HERE.parent / "q01_script_only_probe/fixed_writer.py")}
    manifest = evidence.get("source_sha")
    if not isinstance(manifest, dict) or set(manifest) != expected_sources:
        issues.append("source_manifest_missing_or_unexpected")
    if isinstance(manifest, dict):
        for source in expected_sources:
            if not Path(source).is_file() or manifest.get(source) != _sha(Path(source)):
                issues.append("source_identity_changed:" + Path(source).name)
    raw_trace = path.parent / "private/trace.jsonl"
    if not raw_trace.exists() or [json.loads(line) for line in raw_trace.read_text().splitlines()] != evidence.get("trace"):
        issues.append("raw_trace_mismatch")
    db = path.parent / "private/summary.sqlite3"
    if not db.exists():
        issues.append("private_db_missing")
    else:
        with sqlite3.connect("file:" + str(db) + "?mode=ro", uri=True) as connection:
            rows = [list(row) for row in connection.execute("SELECT id, summary FROM summaries ORDER BY id")]
        if rows != evidence.get("db_rows"):
            issues.append("private_db_rows_mismatch")
    if evidence.get("process", {}).get("remaining_owned") or not evidence.get("process", {}).get("pump_stopped"):
        issues.append("owned_process_not_stopped")
    if evidence.get("permits_remaining"):
        issues.append("permit_remained")
    return issues


def _request(repo: TaskRepository, summary: str, skill_sha: str) -> tuple[dict, dict]:
    task = str(uuid4())
    repo.create_task({"goal": "private Q01 result contract"}, task_id=task)
    repo.approve_scope(task, 1, {"paths": ["private/"]})
    repo.proceed(task, 1, "run")
    run = repo.start_run(task, 1)
    subject = {"schemaVersion": 1, "kind": "task", "id": task}
    source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
              "taskId": task, "revision": 1, "runId": run}
    raw = summary.encode("utf-8")
    span = {"schemaVersion": 1, "source": source, "start": 0, "end": len(raw),
            "digest": sha256(raw).hexdigest()}
    stamp = "2026-10-11T00:00:00Z"
    health = {"schemaVersion": 1, "dbUuid": str(uuid4()), "measuredAt": stamp,
              "stale": False, "mainBytes": 1, "walBytes": 0, "shmBytes": 0, "totalBytes": 1,
              "logicalLiveBytes": 1, "freelistBytes": 0, "subjectStats": [],
              "cleanupCandidates": None, "lastWrite": None, "lastLock": None,
              "lastMaintenance": None, "diskFreeBytes": None, "lastQuickCheck": None,
              "metadataOutsideBudget": True}
    request = {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject, "revision": 1,
               "authorizationRef": str(uuid4()), "authorizationEpoch": 1,
               "inputKey": summary_input_key(subject, 1, [span], [], skill_sha),
               "skillName": "q01-private-summary", "skillDigest": skill_sha,
               "sources": [span], "sourceGaps": [], "priorCumulativeEntryId": None,
               "dbHealth": health, "requestedAt": stamp}
    return parse_summary_request(request), span


def _backend_result_controls(runtime: dict, out: Path) -> dict:
    """Exercise the existing typed journal with bytes saved by the actual OMP tool."""
    repo = TaskRepository(out / "backend-controls.sqlite3")
    events: list[dict] = []
    try:
        summary = runtime["db_rows"][0][1]
        request, span = _request(repo, summary, _sha(SKILL))
        repo.reserve_summary_request(request)
        repo.record_summary_state(request["requestId"], "sending")
        result = {"schemaVersion": 1, "requestId": request["requestId"], "inputKey": request["inputKey"],
                  "skillDigest": request["skillDigest"], "sources": [span],
                  "facts": [summary], "interpretations": [], "unknowns": [],
                  "usage": {"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None}}
        result = parse_summary_result(result)
        negatives = {
            "wrong_id": {**result, "requestId": str(uuid4())},
            "wrong_revision": {**result, "sources": [{**span, "source": {**span["source"], "revision": 2}}]},
            "wrong_range": {**result, "sources": [{**span, "end": span["end"] + 1}]},
        }
        for name, candidate in negatives.items():
            rejected = False
            try:
                repo.record_summary_result_receipt(candidate, str(uuid4()))
            except (InvalidTransitionError, ValueError):
                rejected = True
            events.append({"case": name, "rejected": rejected})
        accepted = repo.record_summary_result_receipt(result, str(uuid4()))
        duplicate = repo.record_summary_result_receipt(result, str(uuid4()))
        events.append({"case": "normal", "accepted": accepted, "result_bytes": len(canonical_json(result))})
        events.append({"case": "duplicate", "rejected": duplicate is False})

        late_request, late_span = _request(repo, summary, _sha(SKILL))
        repo.reserve_summary_request(late_request)
        repo.record_summary_state(late_request["requestId"], "sending")
        repo.record_summary_state(late_request["requestId"], "unknown", detail="controlled disconnect")
        repo.close()
        repo = TaskRepository(out / "backend-controls.sqlite3")
        unresolved = [x["request"]["requestId"] for x in repo.list_unresolved_summary_requests()]
        late_result = {**result, "requestId": late_request["requestId"],
                       "inputKey": late_request["inputKey"], "sources": [late_span]}
        late_accepted = repo.record_summary_result_receipt(late_result, str(uuid4()))
        events.append({"case": "late_after_reopen", "unresolved_before": unresolved,
                       "accepted": late_accepted, "resolved_after": repo.list_unresolved_summary_requests() == []})
    finally:
        repo.close()
    controls = {
        "typed_result_valid": next(x for x in events if x["case"] == "normal")["accepted"],
        "wrong_id_rejected": next(x for x in events if x["case"] == "wrong_id")["rejected"],
        "wrong_revision_rejected": next(x for x in events if x["case"] == "wrong_revision")["rejected"],
        "wrong_range_rejected": next(x for x in events if x["case"] == "wrong_range")["rejected"],
        "duplicate_rejected": next(x for x in events if x["case"] == "duplicate")["rejected"],
        "late_bound_after_reopen": next(x for x in events if x["case"] == "late_after_reopen")["accepted"] and
                                   next(x for x in events if x["case"] == "late_after_reopen")["resolved_after"],
        "result_within_8k": next(x for x in events if x["case"] == "normal")["result_bytes"] <= 8192,
    }
    evidence = {"events": events, "checks": controls,
                "source": "synthetic typed contract controls in private TaskRepository/logs_v1 using an actual fixed-writer summary string",
                "sdk_child_emitted_typed_summary_result": False}
    (out / "backend-controls.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    return evidence


def _row(name: str, facts: dict, unknowns: list[str], evidence_ref: str, source: str = "omp_event") -> dict:
    return {"id": name, "adapter": "probe-local", "source": source,
            "evidence_level": "runtime", "evidence_ref": evidence_ref,
            "facts": facts, "unknowns": unknowns}


def _bound_receipts(trace: list[dict]) -> tuple[bool, bool]:
    """Attribute successful tool receipts to each admitted request and child epoch."""
    request_bound = True
    epoch_bound = True
    for case in ("normal", "normal2", "normal3", "recover_missing", "recover_noop",
                 "recover_api", "recover_stale", "recover_exception"):
        admitted = _event(trace, "startup_admitted", case)
        before = _event(trace, "before_prompt", case)
        sessions = _event(trace, "session_created", case)
        tools = [e for e in _event(trace, "tool_end", case)
                 if e.get("toolName") == "db_summary_save" and e.get("isError") is False]
        if len(admitted) != 1 or len(before) != 1 or len(tools) != 1 or not sessions:
            request_bound = epoch_bound = False
            continue
        admission = admitted[0].get("receipt")
        prompted = before[0].get("receipt")
        try:
            saved = json.loads(tools[0]["result"])
        except (KeyError, TypeError, ValueError):
            request_bound = epoch_bound = False
            continue
        if (not isinstance(saved, dict) or not isinstance(admission, dict) or not isinstance(prompted, dict)
                or admission.get("requestId") != "q01-" + case
                or saved.get("saved") is not True
                or saved.get("request_id") != admission.get("requestId")
                or len(_event(trace, "result_accepted", case)) != 1):
            request_bound = False
        if (not isinstance(saved, dict) or not isinstance(admission, dict) or not isinstance(prompted, dict)
                or admission != prompted or not admission.get("epoch")
                or not admission.get("sessionId")
                or admission.get("sessionId") != sessions[-1].get("sessionId")
                or saved.get("session_id") != admission.get("sessionId")):
            epoch_bound = False
    return request_bound, epoch_bound


def collect(prepared: dict) -> dict:
    if prepared.get("closed"):
        raise ValueError("prepared Q01 context is closed")
    out = Path(prepared["runtime_output"])
    if out.exists():
        raise ValueError("refuse to overwrite Q01 runtime output")
    exit_code = run_full.main(out)
    path = out / "runtime-evidence.json"
    runtime = json.loads(path.read_text())
    custody_issues = _verified_runtime(runtime, path)
    prepare_record = json.loads(Path(prepared["prepare_evidence"]).read_text()) if prepared.get("prepare_evidence") else None
    if prepare_record and (prepare_record.get("omp_binary_sha256") != runtime.get("omp_binary_sha") or
                           prepare_record.get("omp_version") != runtime.get("omp_version")):
        custody_issues.append("prepare_full_binary_or_version_mismatch")
    child_models = _event(runtime["trace"], "child_model_readback")
    model_identity = prepared["prepare_model"]
    if not child_models or any(x.get("model") != model_identity or
                               x.get("thinkingLevel") != prepared["prepare_thinking"] or
                               x.get("serviceTierByFamily") != prepared["prepare_tier"]
                               for x in child_models):
        custody_issues.append("prepare_collect_model_epoch_mismatch")
    custody = {"prepared_selection_epoch": prepared["route_identity"]["selection_epoch"],
               "prepared_model": model_identity,
               "collected_child_epochs": [{"case": x.get("case"), "sessionId": x.get("sessionId"),
                                           "model": x.get("model"),
                                           "thinkingLevel": x.get("thinkingLevel"),
                                           "serviceTierByFamily": x.get("serviceTierByFamily")}
                                          for x in child_models],
               "prepared_epoch_reused_as_child_epoch": False,
               "model_readback_matches": "prepare_collect_model_epoch_mismatch" not in custody_issues}
    (out / "route-custody.json").write_text(json.dumps(custody, ensure_ascii=False, indent=2) + "\n")
    if exit_code != 0:
        custody_issues.append("runtime_suite_exit_nonzero")
    contract = None
    contract_ref = prepared.get("contract_runtime_output")
    if contract_ref and exit_code == 0 and not custody_issues:
        from . import run_contract
        contract = run_contract.run(Path(contract_ref))
        expected_contract_sources = (HERE / "contract_extension.ts", HERE / "role_admission.ts", HERE / "run_contract.py",
                                     HERE / "full_probe.py", BODY, SKILL,
                                     HERE.parent / "q01_script_only_probe/fixed_writer.py")
        manifest = contract.get("source_sha", {})
        if (not all(contract.get("checks", {}).values()) or contract.get("stop_reason") is not None or
                contract.get("process", {}).get("remaining_owned") or
                not isinstance(manifest, dict) or set(manifest) != {str(p) for p in expected_contract_sources} or
                any(manifest.get(str(p)) != _sha(p) for p in expected_contract_sources) or
                contract.get("bundle_body_sha") != prepared["route_identity"].get("agent_definition_sha256")):
            custody_issues.append("current_contract_runtime_incomplete_or_unbound")
        if (contract.get("omp_binary_sha") != runtime.get("omp_binary_sha") or
                contract.get("omp_version") != runtime.get("omp_version")):
            custody_issues.append("full_contract_binary_or_version_mismatch")
        cap_keys = ("id", "api", "contextWindow", "maxTokens")
        expected_caps = {key: prepared["prepare_model"].get(key) for key in cap_keys}
        contract_models = _event(contract.get("trace", []), "child_model_readback")
        if (not contract_models or any(not isinstance(row.get("model"), dict) or
                                       {key: row["model"].get(key) for key in cap_keys} != expected_caps
                                       for row in contract_models)):
            custody_issues.append("contract_model_capability_mismatch")
        (out / "stage-provenance.json").write_text(json.dumps({
            "binary_sha256": runtime.get("omp_binary_sha"), "version": runtime.get("omp_version"),
            "prepare": {"binary_sha256": prepare_record.get("omp_binary_sha256") if prepare_record else None,
                        "version": prepare_record.get("omp_version") if prepare_record else None,
                        "provider_label": prepared["prepare_model"].get("provider"),
                        "provider_port": prepare_record.get("provider_port") if prepare_record else None,
                        "selection_epoch": prepared["route_identity"].get("selection_epoch")},
            "full": {"provider_label": model_identity.get("provider"),
                     "provider_port": runtime.get("provider_port"),
                     "child_epochs": [row.get("sessionId") for row in child_models]},
            "contract": {"provider_label": "q01contract", "provider_port": contract.get("provider_port"),
                         "child_epochs": [row.get("sessionId") for row in contract_models]},
            "common_model_capabilities": expected_caps,
            "fixture_differences": ["provider label", "loopback port", "fresh child session epoch"],
            "operating_model_metadata_claimed": False,
        }, ensure_ascii=False, indent=2) + "\n")
        # Bind the actual child record, parent persistence receipt and protected provisional case.
        positive = contract.get("requests", {}).get("positive", {})
        backend_actual = contract.get("backend_controls", {})
        typed = backend_actual.get("result", {})
        metadata_bytes = backend_actual.get("actual_result_bytes")
        saved = _event(contract.get("trace", []), "saved", "positive")
        accepted = _event(contract.get("trace", []), "result_accepted", "positive")
        parent = _event(contract.get("trace", []), "parent_receipt", "positive")
        typed_path = Path(contract_ref) / "private/typed-positive.json"
        typed_ok = (isinstance(typed, dict) and isinstance(positive, dict) and
                    all(typed.get(key) == positive.get(key) for key in
                        ("requestId", "inputKey", "skillDigest", "sources")) and
                    type(metadata_bytes) is int and 0 < metadata_bytes <= 8192 and
                    len(canonical_json(typed)) <= 8192 and
                    len(saved) == len(accepted) == len(parent) == 1 and
                    saved[0].get("requestId") == positive.get("requestId") and
                    saved[0].get("resultSha") == accepted[0].get("resultSha") ==
                    backend_actual.get("actual_result_sha") and
                    saved[0].get("resultBytes") == accepted[0].get("resultBytes") == metadata_bytes and
                    parent[0].get("requestId") == positive.get("requestId") and
                    parent[0].get("bytes", 4097) <= 4096)
        if typed_ok:
            try:
                receipt_text = parent[0]["parentReceipt"]
                receipt = json.loads(receipt_text)
                typed_ok = (receipt.get("requestId") == positive["requestId"] and
                            receipt.get("resultSha") == backend_actual["actual_result_sha"] and
                            len(receipt_text.encode("utf-8")) == parent[0]["bytes"] and
                            sha256(receipt_text.encode("utf-8")).hexdigest() == parent[0]["sha"])
                raw = typed_path.read_bytes()
                typed_ok = typed_ok and len(raw) == metadata_bytes and len(raw) <= 8192 and \
                    sha256(raw).hexdigest() == backend_actual["actual_result_sha"] and \
                    parse_summary_result(json.loads(raw)) == typed
            except (KeyError, TypeError, ValueError, OSError):
                typed_ok = False
        forbidden_trace = contract.get("trace", [])
        forbidden_saved = _event(forbidden_trace, "saved", "saved_then_bash")
        forbidden_bash = [e for e in _event(forbidden_trace, "tool_end", "saved_then_bash")
                          if e.get("toolName") == "bash" and e.get("isError") is True]
        forbidden_rejected = _event(forbidden_trace, "result_rejected", "saved_then_bash")
        provisional_ok = (len(forbidden_saved) == len(forbidden_bash) == len(forbidden_rejected) == 1 and
                          bool(_event(forbidden_trace, "abort_completed", "saved_then_bash")) and
                          forbidden_rejected[0].get("saved") is True and
                          forbidden_rejected[0].get("forbiddenRefused") is True and
                          not _event(forbidden_trace, "result_accepted", "saved_then_bash") and
                          not _event(forbidden_trace, "parent_receipt", "saved_then_bash") and
                          not (Path(contract_ref) / "private/typed-saved_then_bash.json").exists())
        if not typed_ok:
            custody_issues.append("actual_typed_parent_request_or_bytes_unbound")
        if not provisional_ok:
            custody_issues.append("provisional_forbidden_result_not_protected")
    backend = _backend_result_controls(runtime, out)
    checks = runtime["checks"]
    controls = backend["checks"]
    trace = runtime["trace"]
    posts = runtime["provider_posts"]
    receipts = [e for e in trace if e.get("event") == "tool_end" and e.get("toolName") == "db_summary_save"
                and e.get("isError") is False]
    request_bound, epoch_bound = _bound_receipts(trace)
    begin = [e for e in trace if e.get("event") == "begin"]
    fixture_bytes_exact = (len(begin) == 1 and begin[0].get("bodySha") == _sha(BODY)
                           and begin[0].get("skillSha") == _sha(SKILL)
                           and checks["system_exact"] and bool(posts)
                           and all(p.get("system_exact") for p in posts))
    definition = {"name": "task", "source": "bundled", "filePath": "embedded:task.md"}
    authoritative_body = (len(begin) == 1 and begin[0].get("agentDefinition") == definition
                          and begin[0].get("bodySha") == prepared.get("route_identity", {}).get("agent_definition_sha256")
                          and begin[0].get("fixtureMatches") is False and
                          checks["system_exact"] and bool(posts) and all(p.get("system_exact") for p in posts))
    contract_proved = contract is not None and not any(issue in custody_issues for issue in (
        "current_contract_runtime_incomplete_or_unbound", "actual_typed_parent_request_or_bytes_unbound",
        "provisional_forbidden_result_not_protected", "prepare_full_binary_or_version_mismatch",
        "full_contract_binary_or_version_mismatch", "contract_model_capability_mismatch"))
    contract_controls = contract.get("backend_controls", {}).get("checks", {}) if contract_proved else {}
    parent_receipt = _event(contract["trace"], "parent_receipt", "positive") if contract_proved else []
    receipt_bytes = [len(e["result"].encode("utf-8")) for e in receipts]
    no_extra_bash = len([p for p in posts if p["case"] == "bash"]) == 1
    evidence_ref = str(path)
    backend_ref = str(out / "backend-controls.json")
    rows = [
        _row("Q01-route", {
            "fixture_system_bytes_exact": fixture_bytes_exact,
            "installed_bundled_task_bytes_exact": authoritative_body,
            "existing_agent": authoritative_body,
            "no_new_agent": authoritative_body,
            "typed_return": contract_proved and contract["checks"]["positive_actual_typed"],
            "host_assembled_SummaryResult_from_structured_tool": contract_proved and contract["checks"]["positive_actual_typed"],
            "isolated_turn": checks["three_normal_isolated_turns"],
            "no_standing_process": checks["all_processes_stopped"]},
            ([] if authoritative_body and contract_proved else
             ["authoritative_installed_task_definition_or_typed_handoff_unobserved"]),
            str(Path(contract_ref) / "contract-evidence.json") if contract_proved else evidence_ref),
        _row("Q01-tools", {
            "receipt_request_bound": request_bound,
            "session_turn_epoch_bound": epoch_bound,
            "startup_private_config_verified": checks["private_config_readback"],
            "startup_sole_db_tool_readback": checks["private_config_readback"],
            "startup_guard_effective_verified": checks["first_failure_before_prompt"],
            "startup_missing_guard_prompt_sent_zero": checks["first_failure_before_prompt"],
            "startup_noop_guard_prompt_sent_zero": checks["first_failure_before_prompt"],
            "startup_api_mismatch_prompt_sent_zero": checks["first_failure_before_prompt"],
            "startup_stale_receipt_prompt_sent_zero": checks["first_failure_before_prompt"],
            "startup_failed_session_disposed": checks["rebuild_once_success"],
            "startup_rebuild_once_reverified": checks["rebuild_once_success"] and checks["exception_rebuild_once_success"],
            "startup_repeated_failure_prompt_sent_zero": checks["repeat_failure_deferred"],
            "startup_repeated_failure_summary_deferred": checks["repeat_failure_deferred"],
            "startup_repeated_failure_main_continues": checks["repeat_failure_deferred"] and checks["parent_rpc_after"],
            "runtime_binding_pre_dispatch": checks["runtime_roster_block"] and checks["runtime_binding_blocks"],
            "runtime_request_mismatch_sent_zero": checks["runtime_binding_blocks"],
            "runtime_roster_mismatch_sent_zero": checks["runtime_roster_block"],
            "runtime_model_mismatch_sent_zero": checks["runtime_binding_blocks"],
            "runtime_policy_mismatch_sent_zero": checks["runtime_binding_blocks"],
            "malicious_tools_blocked": checks["bash_tool_not_found"],
            "sentinel_unchanged": checks["sentinels_unchanged"],
            "forbidden_event_immediate_protection": checks["bash_abort_POST_one"],
            "post_receipt_violation_rejected": contract_proved and contract["checks"]["forbidden_after_save_protected"],
            "forbidden_action_after_saved_receipt_rejected": contract_proved and contract["checks"]["forbidden_after_save_protected"],
            "no_extra_model_turn": no_extra_bash},
            ["route_receipt_policy_nonce_negative_and_parent_permissions_unobserved",
             "upper_turn_permissions_and_transcript_unobserved"], evidence_ref),
        _row("Q01-skill", {
            "exact_bytes_in_context": checks["system_exact"],
            "digest_matched": authoritative_body and _sha(SKILL) == trace[0].get("skillSha"),
            "existing_bundled_task_and_host_skill": authoritative_body and
                                                     contract_proved and contract["checks"]["system_exact_and_db_only"],
            "role_filter_enforced": contract_proved and contract["checks"]["manager_role_admitted"] and
                                    contract["checks"]["role_negative_no_post"],
            "isolated_skill_source": checks["private_config_readback"]},
            [] if contract_proved else ["probe_role_filter_unobserved"], evidence_ref),
        _row("Q01-result", {
            "wrong_id_rejected": contract_controls.get("wrong_id_rejected", controls["wrong_id_rejected"]),
            "wrong_revision_rejected": contract_controls.get("wrong_revision_rejected", controls["wrong_revision_rejected"]),
            "wrong_range_rejected": contract_controls.get("wrong_range_rejected", controls["wrong_range_rejected"]),
            "duplicate_rejected": contract_controls.get("duplicate_rejected", controls["duplicate_rejected"]),
            "typed_channel": contract_proved and contract_controls.get("actual_accepted", False),
            "result_within_8k": contract_proved and contract_controls.get("result_utf8_bounded", False),
            "actual_result_within_8k": contract_proved and contract_controls.get("result_utf8_bounded", False),
            "synthetic_contract_result_within_8k": controls["result_within_8k"]},
            [] if contract_proved else ["typed_SummaryResult_from_OMP_child_unobserved"],
            str(Path(contract_ref) / "contract-evidence.json") if contract_proved else backend_ref, "backend_event"),
        _row("Q01-context", {
            "three_turns_observed": checks["three_normal_isolated_turns"],
            "child_tool_receipt_within_4k": len(receipt_bytes) >= 3 and max(receipt_bytes) <= 4096,
            "parent_entry_count_unchanged": checks["parent_unchanged"],
            "parent_persistence_receipt_within_4k": contract_proved and contract["checks"]["parent_receipt_bounded"],
            "parent_persistence_receipt_no_raw_summary": bool(parent_receipt) and not parent_receipt[0]["entryContainsRawSummary"],
            "result_within_8k": contract_proved and contract_controls.get("result_utf8_bounded", False),
            "body_within_32k": checks["scripted_budget_preflight"],
            "output_ceiling_enforced": checks["serialized_scripted_output_ceiling"],
            "full_output_reserved": checks["scripted_budget_preflight"]},
            ["collector_source_selection_1MiB_unobserved",
             "parent_model_context_receipt_delivery_unobserved",
             "three_turn_parent_transcript_delta_unobserved"], evidence_ref),
        _row("Q01-priority", {},
            ["same_parent_busy_user_composer_due_delivery_latency_unmeasured"], evidence_ref),
        _row("Q01-recovery", {
            "late_result_bound": controls["late_bound_after_reopen"]},
            ["actual_pause_disconnect_restart_and_unknown_no_replay_unobserved"], backend_ref, "backend_event"),
        _row("Q01-origin", {},
            ["Task_grant_terminal_close_and_taskless_direct_instruction_provenance_unobserved"], evidence_ref),
    ]
    if custody_issues:
        for row in rows:
            row["unknowns"].extend(custody_issues)
    if len(rows) != len(ITEMS) or tuple(row["id"] for row in rows) != ITEMS:
        raise AssertionError("Q01 collector must emit all eight rows exactly once")
    envelope = {"observations": rows, "unknowns": []}
    (out / "capability-boundaries.json").write_text(json.dumps({
        "scope": "probe-local private OMP scripted parent and trusted role overlay",
        "later_product_or_operating_facts_unobserved": [
            "product Worker role composition", "actual operating Manager parent identity",
            "actual configured operating model metadata"],
        "genuine_q01_unknowns": ["parent model-context receipt delivery", "three-turn parent transcript delta"],
        "parent_receipt_semantics": "pi.appendEntry custom persistence entry; model context delivery not asserted",
        "typed_channel_semantics": "child submits typed summary tool arguments; host assembles SummaryResult with trusted request/source closure and passes that exact record to TaskRepository",
        "direct_child_full_envelope_return": "unobserved",
    }, ensure_ascii=False, indent=2) + "\n")
    (out / "collector-envelope.json").write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
    return envelope


def close(prepared: dict) -> None:
    prepared["closed"] = True
