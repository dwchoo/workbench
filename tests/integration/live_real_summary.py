"""Opt-in, single manual host admission: synthetic Task spec -> SDK -> DB -> query.

This is not the automatic scheduler, an operating session, Q01 or a full gate.
No quality judgement, retry, fallback, parent prompt or raw rotation is performed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from workbench.backend import summary_compat_runtime as rt
from workbench.backend.flow import HandoffJournal
from workbench.backend.launcher import (LaunchPlan, choose_shell, default_isolation_overlay, load_model_table, omp_command,
    omp_environment, role_overlay, selector_level, selector_model, write_role_overlay)
from workbench.backend.omp_home import auth_store_state, config_dir_value, user_auth_store, user_omp_locations
from workbench.backend.paths import DataLayout
from workbench.backend.summary_adapter import (SDKSummaryAdapter, SourcePiece, SummaryDeferred,
    _resolved_metadata, prepare_summary, skill_bytes, validate_result)
from workbench.contracts.logs_v1 import canonical_json, parse_summary_request, summary_input_key
from workbench.ipc.bridge_g3.mailbox import BridgeDisconnected, G3BridgeServer
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog, ImportantLogReader
from workbench.tasks.repository import TaskRepository

ROOT = Path(__file__).resolve().parents[2]
TEXT = "합성 작업: 작은 메모의 제목을 정리했다. 확인 결과 세 항목이 유지되었다. 실제 사용자 로그는 사용하지 않았다."
FLAGS = dict(active=True, paused=False, revoked=False, has_new_input=True)


class TrialFailure(RuntimeError):
    pass


def _private_directory(path: Path) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise TrialFailure("fresh_absolute_directory_required")
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    rt._private_root(path)


def _link_auth(source: Path, root: Path) -> None:
    """Share the given auth store through ``<root>/agent/agent.db`` -> ``source`` (C-D64), never a copy.

    OAuth refresh tokens rotate on use: an OMP on a copy would spend the original's
    refresh token and keep the new one in the copy, logging the real account out
    (WBX-B12). Like ``omp_home.prepare_omp_home`` only ``lstat`` is used on the
    source; it is never opened, read, hashed or copied. SQLite resolves the link,
    so the -wal/-shm files stay next to the source.
    """
    source = Path(source)
    if not source.is_absolute():
        raise TrialFailure("auth_source_not_absolute")
    if not auth_store_state(source).ok:
        raise TrialFailure("auth_source_not_regular")
    os.symlink(source, root / "agent/agent.db")


def _fixture(layout: DataLayout):
    catalog = ImportantLogCatalog(layout.important_logs, create=True)
    life = ActiveSubjectLog(catalog)
    with TaskRepository(layout.tasks) as repo:
        task = repo.create_task({"description": TEXT})
        scope = repo.approve_scope(task, 1, {"operation": "one synthetic DB-only summary"})
        grant = repo.proceed(task, 1, "manual host admission for one summary")
        run = repo.start_run(task, 1)
        text = canonical_json(repo.get_task_spec(task, 1)["spec"]).decode("utf-8")
    layout.tasks.chmod(0o600)
    journal = HandoffJournal(layout.root / "manual-admission.jsonl")
    try:
        journal.append({"type": "delegated", "task_id": task, "revision": 1,
                        "scope_decision_id": scope, "proceed_decision_id": grant, "run_id": run,
                        "origin": "manual_owned_summary_trial"})
        life.record_flow(journal.read_all()[0])
    finally:
        journal.close()
    subject = {"schemaVersion": 1, "kind": "task", "id": task}
    # The host receives its own persisted spec as summary input, not a Worker turn.
    message = str(uuid4())
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    life.record_delivery(subject, message_id=message, revision=1, kind="task_spec",
                         content=text, delivered_at=stamp)
    entries = ImportantLogReader(layout.important_logs).show(subject)
    source_entry = next(row["entry"] for row in entries["entries"]
                        if row["entry"]["sources"][0]["source"].get("messageId") == message)
    sources = source_entry["sources"]
    auth = life.authority(subject, **FLAGS)
    digest = sha256(skill_bytes()).hexdigest()
    request = parse_summary_request({"schemaVersion": 1, "requestId": str(uuid4()),
        "subject": subject, "revision": 1, "authorizationRef": auth["authorization_ref"],
        "authorizationEpoch": auth["authorization_epoch"], "skillName": "important-log-summary",
        "skillDigest": digest, "sources": sources, "sourceGaps": [],
        "inputKey": summary_input_key(subject, 1, sources, [], digest),
        "priorCumulativeEntryId": None, "dbHealth": catalog.health_snapshot()["active"],
        "requestedAt": stamp})
    return life, request, text, source_entry, {"run_id": run, "scope_id": scope, "grant_id": grant}


class _Authority:
    def __init__(self, layout, life, request, text, owned):
        self.layout, self.life, self.request, self.text, self.owned = layout, life, request, text, owned
        self.claimed = False

    def current(self):
        request = self.request
        task = request["subject"]["id"]
        with TaskRepository(self.layout.tasks) as repo:
            run = repo.get_current_run(task)
            if (run is None or run["run_id"] != self.owned["run_id"] or run["revision"] != 1
                    or canonical_json(repo.get_task_spec(task, 1)["spec"]).decode() != self.text):
                return False
            for key, kind in (("scope_id", "scope_approved"), ("grant_id", "proceed")):
                decision = repo.get_decision(self.owned[key])
                if (decision["task_id"], decision["revision"], decision["kind"]) != (task, 1, kind):
                    return False
            record = repo.get_summary_request(request["requestId"])
            if (record is None or record["state"] != "sending" or record["receipt"] is not None
                    or canonical_json(record["request"]) != canonical_json(request)):
                return False
        return self.life.authorize_summary_request(request, **FLAGS).get("allowed") is True

    def claim(self):
        if self.claimed or not self.current():
            return False
        self.claimed = True
        return True


def _query(layout, request, source_entry, text, outcome):
    with TaskRepository(layout.tasks) as repo:
        record = repo.get_summary_request(request["requestId"])
        unchanged = canonical_json(repo.get_task_spec(request["subject"]["id"], 1)["spec"]).decode() == text
    page = ImportantLogReader(layout.important_logs).show(request["subject"])
    entries = {row["entry"]["entryId"]: row["entry"] for row in page["entries"]}
    if (not page["complete"] or entries.get(source_entry["entryId"]) != source_entry or not unchanged):
        raise TrialFailure("prior_source_or_query_mismatch")
    if record is None:
        return {"request_state": "not_reserved", "prior_source_unchanged": True}
    if canonical_json(record["request"]) != canonical_json(request):
        raise TrialFailure("journal_request_binding_mismatch")
    receipt = record["receipt"]
    if record["state"] != "stored" or receipt is None:
        return {"request_state": record["state"], "prior_source_unchanged": True}
    entry = entries.get(receipt["entry_id"])
    if (entry is None or entry["subject"] != request["subject"] or entry["sources"] != request["sources"]
            or entry["sourceGaps"] != request["sourceGaps"] or receipt["request_id"] != request["requestId"]):
        raise TrialFailure("stored_receipt_binding_mismatch")
    digest_match = None
    if outcome.get("status") == "summary_result":
        result = validate_result(request, outcome.get("result"))
        digest_match = receipt["result_digest"] == sha256(canonical_json(result)).hexdigest()
        if not digest_match or any(entry["body"][key] != result[key]
                                   for key in ("facts", "interpretations", "unknowns")):
            raise TrialFailure("stored_result_digest_mismatch")
    return {"request_state": "stored", "entry_id": entry["entryId"],
            "receipt_id": receipt["receipt_id"], "result_digest": receipt["result_digest"],
            "response_digest_matches": digest_match, "prior_source_unchanged": True,
            "saved_summary": entry["body"]}


def run_real_summary(output, *, real_model=False, auth_source=None):
    # Default/help cannot inspect auth or launch native/model/network work.
    if real_model is not True or auth_source is None:
        return 2, {"status": "not_opted_in", "summary_requests": 0}
    output = Path(output)
    _private_directory(output)
    result = {"schema_version": 1, "kind": "single_manual_owned_actual_summary",
              "status": "inconclusive", "phase": "private_setup", "raw_activation": False,
              "c2": "hold", "overall_gate_status": "inconclusive", "parent_prompts": 0,
              "summary_requests": 0, "paid_summary_invocations_requested": 0,
              "actual_http_requests": None, "native_io_attribution": "unknown",
              "native_output_captured": False, "quality_evaluated": False,
              "auth_store": "shared_symlink",
              "native_version": "unobserved"}
    root = bridge = process = ref = request = base = None
    outcome = {}
    cleanup = {"status": "incomplete", "private_root_removed": False, "remaining_owned": []}
    layout = DataLayout(output / "data")
    try:
        _private_directory(layout.root)
        life, base, text, source_entry, owned = _fixture(layout)
        result.update(task_id=base["subject"]["id"], source_entry_id=source_entry["entryId"],
                      source_digest=base["sources"][0]["digest"], **owned)
        root = Path(tempfile.mkdtemp(prefix="dvl-summary-")).resolve()
        os.chmod(root, 0o700)
        rt._private_root(root)
        for name in ("home", "project", "tmp", "agent", "config", "xdg-config", "xdg-data", "xdg-state", "xdg-cache"):
            (root / name).mkdir(mode=0o700)
        runtime = rt._copy_runtime(ROOT, root)
        env = rt._environment(root)
        result["phase"] = "binary_selection"
        binary_name = shutil.which("omp")
        if not binary_name:
            raise TrialFailure("omp_missing")
        binary = Path(binary_name).resolve(strict=True)
        with binary.open("rb") as binary_stream:
            result["binary_sha256"] = file_digest(binary_stream, "sha256").hexdigest()
        result["phase"] = "auth_link"
        _link_auth(Path(auth_source), root)
        table = load_model_table(runtime / "omp_bridge/omp-models.yml")
        manager = table["manager"]["models"]["default"]
        child = table["worker"]["models"]["default"]
        if child != "openai-codex/gpt-6-luna:max":
            raise TrialFailure("shipped_worker_default_changed")
        socket = root / "b.sock"
        tokens = {role: uuid4().hex for role in ("manager", "worker")}
        bridge = G3BridgeServer(socket, tokens)
        bridge.start()
        # Same Manager.default discriminator as the verified installed getter.
        plan = LaunchPlan(choose_shell(env), str(binary), "unobserved", str(runtime / "omp_bridge/g3/entry.ts"),
                          ("--mode", "rpc", "--no-session", "--model", selector_model(manager)))
        child_env = omp_environment(env, plan, role="manager", token=tokens["manager"],
            bridge_socket=socket, models=table, home={
                "PI_CONFIG_DIR": config_dir_value(root / "config", env),
                "PI_CODING_AGENT_DIR": str(root / "agent")})
        locations = user_omp_locations(child_env)
        linked_auth = root / "agent/agent.db"
        info = linked_auth.lstat()
        if (locations.config_root != root / "config" or locations.agent_dir != root / "agent"
                or locations.auth_store != linked_auth or user_auth_store(child_env) != linked_auth
                or locations.config_root.resolve() != root / "config"
                or not stat.S_ISLNK(info.st_mode) or os.readlink(linked_auth) != str(auth_source)):
            raise TrialFailure("private_auth_selection_mismatch")
        overlay = write_role_overlay(root / "config", "manager", role_overlay("manager",
            project_dir=root / "project", home=root / "home", skills_dir=runtime / "omp_bridge/skills",
            environment=child_env, models=table))
        result["phase"] = "native_launch"
        argv = omp_command(plan, overlay, role="manager")
        argv = [str(runtime / "omp_bridge/omp-isolation.yml") if arg == str(default_isolation_overlay()) else arg for arg in argv]
        process = subprocess.Popen(argv, cwd=root / "project",
            env=child_env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        ref = {"pid": process.pid, "pgid": process.pid, "start_ticks": rt._identity(process.pid)}
        result["owned_process"] = ref
        if ref["start_ticks"] is None:
            raise TrialFailure("owned_process_identity_unavailable")
        deadline = time.monotonic() + 50
        while True:
            if process.poll() is not None:
                raise TrialFailure("native_exited_before_manager_peer")
            if time.monotonic() >= deadline:
                raise TrialFailure("manager_peer_timeout")
            try:
                peer = bridge.peer("manager", timeout=min(0.25, deadline - time.monotonic()))
                break
            except BridgeDisconnected:
                continue
        if peer.pid != process.pid:
            raise TrialFailure("manager_peer_process_mismatch")
        adapter = SDKSummaryAdapter(bridge, timeout=40)
        bound = (peer.session_id, peer.generation)
        result["phase"] = "runtime_readback"
        observed = adapter.read_runtime(expected_peer=bound)
        metadata = _resolved_metadata(observed["metadata"])
        selection = metadata["childSelection"]
        expected = json.loads(child_env["WORKBENCH_G3_SUMMARY_SELECTION"])
        if (selection["selector"] != child or selection["modelTableDigest"] != expected["modelTableDigest"]
                or selection["thinkingLevel"] != selector_level(child)
                or selection["model"]["provider"] + "/" + selection["model"]["id"] != selector_model(child)
                or metadata["model"]["provider"] + "/" + metadata["model"]["id"] != selector_model(manager)
                or metadata["thinkingLevel"] != selector_level(manager)):
            raise TrialFailure("actual_model_selection_mismatch")
        result.update(manager_selector=manager, child_selector=selection["selector"],
                      model_table_digest=selection["modelTableDigest"], metadata_status=metadata.get("status"),
                      metadata_unknowns=metadata.get("unknowns"))
        prepared = prepare_summary(base, [SourcePiece(base["sources"][0], text, "start_spec")],
                                   metadata=metadata, instructions=observed["instructions"])
        request = prepared["request"]
        if request["sources"] != base["sources"]:
            raise TrialFailure("owned_spec_not_selected")
        with TaskRepository(layout.tasks) as repo:
            repo.reserve_summary_request(request)
            repo.record_summary_state(request["requestId"], "sending")
        authority = _Authority(layout, life, request, text, owned)
        result.update(task_id=request["subject"]["id"], request_id=request["requestId"],
                      input_key=request["inputKey"], sources=request["sources"], **owned)
        result["phase"] = "single_summary_dispatch"
        result["summary_requests"] = result["paid_summary_invocations_requested"] = 1
        try:
            outcome = adapter.dispatch_outcome(prepared, expected_peer=bound, authority_token=authority,
                writer={"python": str(Path(sys.executable).resolve()), "moduleRoot": str(runtime / "src"),
                        "dataRoot": str(layout.root)})
        except SummaryDeferred:
            outcome = {"sent": False, "status": "deferred"}
        except Exception as exc:
            result["dispatch_error_type"] = type(exc).__name__
        finally:
            status = outcome.get("status")
            result["sdk_outcome"] = status if status in ("summary_result", "deferred", "rejected", "storage_failed") else "unknown"
            result["sent"] = outcome.get("sent") if type(outcome.get("sent")) is bool else None
            with TaskRepository(layout.tasks) as repo:
                record = repo.get_summary_request(request["requestId"])
                if record["state"] == "sending":
                    repo.record_summary_state(request["requestId"], "not_sent" if outcome.get("sent") is False else "unknown",
                                              detail="single_trial_confirmed_not_sent" if outcome.get("sent") is False else "single_trial_no_confirmed_receipt")
    except Exception as exc:
        result["failure_reason"] = str(exc) if isinstance(exc, TrialFailure) else "trial_failed"
        result["error_type"] = type(exc).__name__
    finally:
        if process is not None:
            if ref is not None and ref.get("start_ticks") is None and process.poll() is None:
                ref["start_ticks"] = rt._identity(process.pid)
            try:
                cleanup["leader_error"] = rt._stop_leader(process, ref)
            except Exception as exc:
                cleanup["leader_error"] = type(exc).__name__
            cleanup["leader_exit_code"] = process.poll()
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                cleanup["remaining_owned"] = rt._remaining(process.pid)
            except Exception:
                cleanup["remaining_owned"] = ["ownership_scan_unavailable"]
        if bridge is not None:
            try:
                bridge.close()
                thread = getattr(bridge, "_thread", None)
                cleanup["bridge_closed"] = not socket.exists() and (thread is None or not thread.is_alive())
            except Exception:
                cleanup["bridge_closed"] = False
        else:
            cleanup["bridge_closed"] = True
        stopped = (process is None or (cleanup.get("leader_error") is None
                   and cleanup["leader_exit_code"] is not None and not cleanup["remaining_owned"]))
        if root is not None:
            # Remove the auth store link (never its target) even if process cleanup is inconclusive.
            try:
                shutil.rmtree(root / "agent")
                cleanup["auth_link_removed"] = True
                if stopped and cleanup["bridge_closed"]:
                    shutil.rmtree(root)
                    cleanup["private_root_removed"] = not root.exists()
            except OSError:
                cleanup["auth_link_removed"] = not (root / "agent").exists()
            if root.exists():
                cleanup["retained_private_root"] = str(root)
        cleanup["status"] = "complete" if stopped and cleanup["bridge_closed"] and (root is None or cleanup["private_root_removed"]) else "incomplete"
        result["cleanup"] = cleanup
    if base is not None:
        try:
            # Observe receipts after stopping the owned runtime, including a late
            # durable writer result. Never append or dispatch a second time.
            result["query"] = _query(layout, request or base, source_entry, text, outcome)
            if request is not None:
                result["status"] = result["query"]["request_state"]
        except Exception as exc:
            result.update(failure_reason="readonly_query_failed", error_type=type(exc).__name__)
    fd = os.open(output / "result.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return (0 if result["status"] == "stored" and cleanup["status"] == "complete" else 2), result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", action="store_true", help="explicitly allow one actual paid summary invocation")
    parser.add_argument("--auth-source", type=Path, help="absolute path of the agent.db to share through a symlink (never copied)")
    parser.add_argument("--output", type=Path, help="fresh absolute private fixture directory (DB and sanitized result retained)")
    args = parser.parse_args(argv)
    if not args.real_model or args.auth_source is None or args.output is None:
        print(json.dumps({"status": "not_opted_in", "summary_requests": 0}))
        return 2
    try:
        code, result = run_real_summary(args.output, real_model=True, auth_source=args.auth_source)
    except Exception as exc:
        code, result = 2, {"status": "inconclusive", "failure_reason": "trial_setup_failed", "error_type": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
