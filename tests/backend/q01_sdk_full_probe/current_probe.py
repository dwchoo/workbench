"""Current Q01 eight-row collector from separate, owned installed-OMP stage runs.

All sources are private scripted fixtures. A row states only what its raw stage
events establish; configured operating-model quality and product wiring are
recorded separately from the DVL-08 probe-local verdict.
"""

from __future__ import annotations

from collections import Counter
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from . import run_contract, run_full, run_prepare
from . import run_recovery
from workbench.contracts.logs_v1 import parse_summary_request, summary_input_key
from workbench.storage.important_log import ImportantLogStore, SubjectLifecycle
from tests.backend.q01_sdk_budget_probe import run_budget, run_priority_runtime
from tests.backend.q01_sdk_context_probe import run_context, run_priority_input
from tests.backend.q01_sdk_origin_probe import run_origin

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent / "q01_sdk_private_probe/summary-skill.txt"
ITEMS = ("Q01-route", "Q01-tools", "Q01-skill", "Q01-result", "Q01-context",
         "Q01-priority", "Q01-recovery", "Q01-origin")
WORK = ROOT / ".workflow/dvl-local/lg24-current-eight01"


def _sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _events(trace: list[dict], name: str, case: str | None = None) -> list[dict]:
    return [item for item in trace if item.get("event") == name and
            (case is None or item.get("case") == case)]


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(item.get("text", "")) for item in value
                       if isinstance(item, dict) and item.get("type") == "text")
    return ""


def _source_issues(stage: str, evidence: dict) -> list[str]:
    manifest = evidence.get("source_sha256", evidence.get("source_sha"))
    if not isinstance(manifest, dict) or not manifest:
        return [stage + ":source_manifest_missing"]
    issues = []
    stage_dir = {"prepare": HERE, "full": HERE, "contract": HERE,
                 "context": HERE.parent / "q01_sdk_context_probe",
                 "budget": HERE.parent / "q01_sdk_budget_probe",
                 "priority_input": HERE.parent / "q01_sdk_context_probe",
                 "priority_runtime": HERE.parent / "q01_sdk_budget_probe",
                 "recovery": HERE, "origin": HERE.parent / "q01_sdk_origin_probe"}[stage]
    for name, digest in manifest.items():
        path = Path(name)
        if not path.is_absolute():
            path = (stage_dir / path) if "/" not in name else ROOT / path
        if not path.is_file() or _sha(path) != digest:
            issues.append(stage + ":source_changed:" + name)
    return issues


def _raw_issues(stage: str, posts: list[dict]) -> list[str]:
    issues = []
    for index, post in enumerate(posts):
        raw = post.get("raw_body_path")
        expected = post.get("sha256", post.get("request_sha", post.get("body_sha")))
        try:
            bound = isinstance(raw, str) and bool(raw) and Path(raw).is_file() and _sha(Path(raw)) == expected
        except (OSError, ValueError, TypeError):
            bound = False
        if not bound:
            issues.append(f"{stage}:raw_post_{index}_unbound")
    return issues


def _owned_stopped(stages: dict[str, dict]) -> tuple[bool, list[dict]]:
    observed = []
    for name, value in stages.items():
        processes = []
        if name == "priority_runtime":
            processes = list(value.get("processes", {}).values())
        elif name == "recovery":
            processes = [phase.get("process", {}) for phase in value.get("phases", {}).values()]
        else:
            processes = [value.get("process", {})]
        if not processes:
            observed.append({"stage": name, "missing_process": True, "stopped": False})
        for proc in processes:
            pid = proc.get("pgid", proc.get("pid"))
            absent = False
            if type(pid) is int and pid > 0:
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    absent = True
            stopped = (absent and not proc.get("remaining_owned") and
                       proc.get("pump_stopped", proc.get("pty_drain_stopped", True)) is True)
            observed.append({"stage": name, "pid": pid, "pgid_absent": absent,
                             "stopped": stopped})
    return bool(observed) and all(item["stopped"] for item in observed), observed


def _rows(facts: dict[str, dict], refs: dict[str, Path]) -> list[dict]:
    rows = []
    for item in ITEMS:
        values = facts.get(item, {})
        metadata = {"shared_session_or_lock", "measured_block_ms", "user_adoption_ref",
                    "nonshared_priority_measurement"}
        if item == "Q01-priority" and values.get("shared_session_or_lock") is False:
            metadata.add("max_block_recorded")
        missing = ["unobserved:" + name for name, value in values.items()
                   if name not in metadata and value is not True]
        rows.append({"id": item, "adapter": "probe-local",
                     "source": "backend_event" if item in ("Q01-result", "Q01-origin", "Q01-recovery") else "omp_event",
                     "evidence_level": "runtime", "evidence_ref": str(refs[item]),
                     "facts": values, "unknowns": missing})
    return rows


def _priority_shape(priority: object) -> tuple[dict, list[str]]:
    """Reject malformed retained collections while keeping composition total."""
    issues = []

    def mapping(value: object, name: str) -> dict:
        if not isinstance(value, dict):
            issues.append("priority_runtime:malformed:" + name)
            return {}
        return dict(value)

    def rows(value: object, name: str) -> list[dict]:
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            issues.append("priority_runtime:malformed:" + name)
            return []
        return [dict(row) for row in value]

    value = mapping(priority, "stage")
    value["timing"] = timing = mapping(value.get("timing"), "timing")
    timing["parent_turn_events"] = rows(timing.get("parent_turn_events"), "turn_events")
    value["bridge"] = bridge = mapping(value.get("bridge"), "bridge")
    bridge["hello"] = hello = mapping(bridge.get("hello"), "hello")
    for role in ("manager", "worker"):
        hello[role] = mapping(hello.get(role), "hello_" + role)
    bridge["requests"] = requests = rows(bridge.get("requests"), "requests")
    for request in requests:
        request["frame"] = frame = mapping(request.get("frame"), "frame")
        if frame.get("kind") == "notice":
            frame["notice"] = mapping(frame.get("notice"), "notice")
        request["ack"] = mapping(request.get("ack"), "ack")
    value["providers"] = providers = mapping(value.get("providers"), "providers")
    if set(providers) - {"manager", "worker"}:
        issues.append("priority_runtime:malformed:provider_roles")
        providers = value["providers"] = {role: providers.get(role) for role in ("manager", "worker")}
    for role in ("manager", "worker"):
        providers[role] = provider = mapping(providers.get(role), "provider_" + role)
        provider["posts"] = rows(provider.get("posts"), "posts_" + role)
    value["scheduler"] = scheduler = mapping(value.get("scheduler"), "scheduler")
    scheduler["tick60"] = mapping(scheduler.get("tick60"), "tick60")
    scheduler["notice_dispatches"] = rows(scheduler.get("notice_dispatches"), "dispatches")
    value["budget_trace"] = rows(value.get("budget_trace"), "budget_trace")
    value["manager_draft_ack"] = mapping(value.get("manager_draft_ack", {}), "manager_draft_ack")
    return value, issues


def _priority_wire_issues(priority: dict, artifact: Path) -> list[str]:
    """Bind copied acknowledgements to hash-checked original wire frames."""
    try:
        wire = []
        for line in (artifact.parent / "private/bridge-wire.jsonl").read_text().splitlines():
            row = json.loads(line)
            raw = base64.b64decode(row["raw_b64"], validate=True)
            if sha256(raw).hexdigest() != row.get("sha256"):
                return ["priority_runtime:wire_sha_mismatch"]
            frame = json.loads(raw)
            if (not isinstance(frame, dict) or type(row.get("monotonic_ns")) is not int or
                    not 0 < row["monotonic_ns"] <= 2**63 - 1):
                return ["priority_runtime:wire_malformed"]
            wire.append({**row, "frame": frame})
        connections = {}
        for role in ("manager", "worker"):
            hello = priority["bridge"]["hello"][role]
            matches = [row for row in wire if row.get("direction") == "omp_to_host" and
                       row["frame"] == hello]
            if len(matches) != 1:
                return ["priority_runtime:wire_hello_mismatch:" + role]
            connections[role] = matches[0].get("connection")
        for request in priority["bridge"]["requests"]:
            if request["frame"].get("kind") != "notice":
                continue
            role, ack = request.get("role"), request["ack"]
            if role not in connections:
                return ["priority_runtime:wire_role_mismatch"]
            request_id = ack.get("requestId")
            expected = {**request["frame"], "requestId": request_id}
            sent = [row for row in wire if row.get("direction") == "host_to_omp" and
                    # The existing G3 writer logs the role on send; its reader
                    # logs the socket identity bound by the original hello.
                    row.get("connection") == role and row["frame"] == expected]
            received = [row for row in wire if row.get("direction") == "omp_to_host" and
                        row.get("connection") == connections[role] and row["frame"] == ack]
            sent_ns, ack_ns = request.get("sent_ns"), request.get("ack_observed_ns")
            if (len(sent) != 1 or len(received) != 1 or
                    not all(type(t) is int and 0 < t <= 2**63 - 1 for t in (sent_ns, ack_ns)) or
                    not sent_ns <= sent[0]["monotonic_ns"] <= received[0]["monotonic_ns"] <= ack_ns):
                return ["priority_runtime:wire_request_ack_mismatch"]
        return []
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return ["priority_runtime:wire_unavailable_or_malformed"]


def _priority_body_notice_matches(post: dict, role: str, kind: str, notice_id: str) -> bool:
    """Attribute the copied notice to the actual, hash-checked parent body."""
    if {"manager": "worker_terminal_done", "worker": "terminal_check"}.get(role) != kind:
        return False
    try:
        path = post.get("raw_body_path")
        if not isinstance(path, str) or not path:
            return False
        raw = Path(path).read_bytes()
        if sha256(raw).hexdigest() != post.get("sha256") or len(raw) != post.get("bytes"):
            return False
        body = json.loads(raw)
        notices = []
        for message in body["messages"]:
            content = message.get("content", [])
            parts = [{"text": content}] if isinstance(content, str) else content
            for part in parts:
                text = part.get("text") if isinstance(part, dict) else None
                if not isinstance(text, str):
                    continue
                try:
                    value = json.loads(text)
                except ValueError:
                    continue
                if isinstance(value, dict) and "workbench_notice" in value:
                    notices.append(value)
        return (len(notices) == 1 and notices[0].get("workbench_notice") == kind and
                notices[0].get("notice_id") == notice_id)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def _priority_measurement(priority: dict) -> dict:
    """Copy attributable monotonic observations without inventing lock spans."""
    priority, shape_issues = _priority_shape(priority)
    timing = priority.get("timing", {})
    bridge = priority.get("bridge", {})
    hello = bridge.get("hello", {})
    sessions = {role: hello.get(role, {}).get("ompSessionId")
                if hello.get(role, {}).get("role") == role else None
                for role in ("manager", "worker")}
    children = [event.get("childSessionId") for event in priority.get("budget_trace", [])
                if event.get("event") == "startup"]
    sessions["child"] = children[0] if len(children) == 1 else None
    hold, release = priority.get("child_hold_started_ns"), priority.get("child_release_signal_ns")
    windows = []
    turn_events = timing.get("parent_turn_events", [])
    dispatches = priority.get("scheduler", {}).get("notice_dispatches", [])
    for role, kind in (("worker", "terminal_check"), ("manager", "worker_terminal_done")):
        requests = [row for row in bridge.get("requests", []) if row.get("role") == role and
                    row.get("frame", {}).get("kind") == "notice" and
                    row.get("frame", {}).get("notice", {}).get("type") == kind and
                    row.get("ack", {}).get("status") == "api_accepted"]
        if len(requests) != 1:
            continue
        request = requests[0]
        notice_id = request["frame"]["notice"].get("notice_id")
        due = [row for row in dispatches if row.get("notice_id") == notice_id]
        enqueue = (due[0].get("enqueue_ns") if len(due) == 1 else None) if role == "worker" else (
            timing.get("end_accepted_enqueue_ns"))
        posts = [post for post in priority.get("providers", {}).get(role, {}).get("posts", [])
                 if post.get("stage") == "parent" and post.get("notice_id") == notice_id]
        if type(enqueue) is not int or not 0 < enqueue <= 2**63 - 1 or len(posts) != 1:
            continue
        starts = [event["observed_ns"] for event in turn_events if event.get("role") == role and
                  event.get("name") == "agent_start" and type(event.get("observed_ns")) is int and
                  event["observed_ns"] >= enqueue]
        ends = [event["observed_ns"] for event in turn_events if event.get("role") == role and
                event.get("name") == "agent_end" and type(event.get("observed_ns")) is int and
                starts and event["observed_ns"] >= min(starts)]
        if len(starts) != 1 or len(ends) != 1:
            continue
        start, end, post, ack, frame = (starts[0], ends[0], posts[0].get("received_ns"),
                                      request.get("ack_observed_ns"), request.get("sent_ns"))
        if not all(type(value) is int and 0 < value <= 2**63 - 1
                   for value in (start, end, post, ack, frame, hold, release)):
            continue
        overlap = max(0, min(end, release) - max(start, hold))
        windows.append({"role": role, "kind": kind, "session_id": sessions[role],
                        "notice_id": notice_id, "ack_notice_id": request["ack"].get("noticeId"),
                        "ack_request_id": request["ack"].get("requestId"),
                        "ack_status": request["ack"].get("status"),
                        "provider_notice_id": posts[0].get("notice_id"),
                        "provider_stage": posts[0].get("stage"),
                        "scheduler_notice_id": due[0].get("notice_id") if len(due) == 1 else None,
                        "enqueue_ns": enqueue, "frame_request_ns": frame, "ack_observed_ns": ack,
                        "agent_start_ns": start, "provider_post_ns": post, "agent_end_ns": end,
                        "enqueue_to_ack_ms": (ack - enqueue) / 1_000_000,
                        "enqueue_to_start_ms": (start - enqueue) / 1_000_000,
                        "enqueue_to_post_ms": (post - enqueue) / 1_000_000,
                        "turn_hold_overlap_ms": overlap / 1_000_000})
    return {"kind": "delivery_turn_overlap", "clock": timing.get("clock") if not shape_issues else None,
            "session_ids": sessions, "child_hold_interval_ns": [hold, release],
            "notice_windows": windows,
            "due_scheduler_status": priority.get("scheduler", {}).get("tick60", {}).get("status"),
            "max_enqueue_to_start_ms": max((row["enqueue_to_start_ms"] for row in windows), default=None),
            "max_enqueue_to_post_ms": max((row["enqueue_to_post_ms"] for row in windows), default=None),
            "internal_lock_wait": "unavailable"}


def _with_mode(mode: str, runner: Callable[[Path], dict], out: Path) -> dict:
    previous = sys.argv
    try:
        sys.argv = [previous[0], str(out), mode]
        return runner(out)
    finally:
        sys.argv = previous


def _task_contract_proof(contract: dict, artifact: Path) -> tuple[bool, list[str]]:
    """Reconcile copied contract facts with the immutable private backend records."""
    issues: list[str] = []

    def require(condition: bool, name: str) -> None:
        if not condition:
            issues.append("contract:task_proof_" + name)

    try:
        private = artifact.parent / "private"
        external = json.loads((artifact.parent / "task-binding.json").read_text())
        copied = contract["task_binding"]
        require(copied["path"] == str(artifact.parent / "task-binding.json") and
                copied["bindings"] == external["bindings"] and
                copied["checks"] == external["checks"], "binding_copy_mismatch")
        binding = copied["bindings"]["positive"]
        request = parse_summary_request(contract["requests"]["positive"])
        require(request == json.loads((private / "requests.json").read_text())["positive"],
                "request_copy_mismatch")
        typed = json.loads((private / "typed-positive.json").read_text())
        task_id, revision = binding["task_id"], request["revision"]
        request_id = request["requestId"]
        with sqlite3.connect("file:" + str(private / "backend.sqlite3") + "?mode=ro", uri=True) as db:
            task = db.execute("SELECT task_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            spec = db.execute("SELECT revision FROM task_specs WHERE task_id=? AND revision=?",
                              (task_id, revision)).fetchone()
            decisions = db.execute("""SELECT decision_id,task_id,revision,kind,created_at
                FROM task_decisions WHERE task_id=? ORDER BY rowid""", (task_id,)).fetchall()
            run = db.execute("SELECT run_id,task_id,revision,created_at FROM runs WHERE run_id=?",
                             (binding["run_id"],)).fetchone()
            reserved = db.execute("""SELECT subject_kind,subject_id,revision,input_key,
                skill_digest,request_json,created_at FROM summary_requests_v1 WHERE request_id=?""",
                (request_id,)).fetchone()
            receipt = db.execute("SELECT request_id,created_at FROM summary_result_receipts_v1 WHERE request_id=?",
                                 (request_id,)).fetchall()
        require(task == (task_id,) and spec == (revision,), "task_revision_missing")
        require(len(decisions) == 2 and
                [(row[0], row[1], row[2], row[3]) for row in decisions] ==
                [(binding["scope_ref"], task_id, revision, "scope_approved"),
                 (binding["proceed_ref"], task_id, revision, "proceed")],
                "persisted_decisions_mismatch")
        require(run is not None and run[:3] == (binding["run_id"], task_id, revision),
                "persisted_run_mismatch")
        require(reserved is not None and reserved[:5] ==
                ("task", task_id, revision, request["inputKey"], request["skillDigest"]) and
                json.loads(reserved[5]) == request, "stored_request_mismatch")
        require(len(decisions) == 2 and run is not None and reserved is not None and
                decisions[0][4] < decisions[1][4] < run[3] < reserved[6],
                "decision_reservation_order_mismatch")
        require(len(receipt) == 1 and receipt[0][0] == request_id and
                reserved is not None and reserved[6] < receipt[0][1],
                "stored_result_receipt_missing")
        require(request["subject"] == {"schemaVersion": 1, "kind": "task", "id": task_id} and
                revision == 1 and request["authorizationRef"] == binding["proceed_ref"] and
                request["sources"] == [binding["selected_span"]] and
                request["authorizationEpoch"] == binding["authorization"]["authorization_epoch"],
                "request_grant_or_source_mismatch")
        require(all(typed.get(key) == request[key] for key in
                    ("requestId", "inputKey", "skillDigest", "sources")),
                "typed_result_request_mismatch")
        db_uuid = external["important_db_uuid"]
        require(type(db_uuid) is str and str(UUID(db_uuid)) == db_uuid,
                "important_db_uuid_invalid")
        important = private / "important" / (db_uuid + ".sqlite3")
        with sqlite3.connect("file:" + str(important) + "?mode=ro", uri=True) as db:
            state = db.execute("""SELECT grant_ref,grant_revision,event_cursor
                FROM subject_lifecycle_v1 WHERE subject_kind='task' AND subject_id=?""",
                (task_id,)).fetchone()
            started = db.execute("""SELECT source_id,event_key FROM subject_events_v1
                WHERE subject_kind='task' AND subject_id=? AND event_kind='terminal_started'""",
                (task_id,)).fetchall()
            command = db.execute("SELECT task_id,revision FROM subject_commands_v1 WHERE command_id=?",
                                 (binding["command_id"],)).fetchone()
            spans = [json.loads(row[0]) for row in db.execute(
                "SELECT sources_json FROM entries WHERE subject_kind='task' AND subject_id=?", (task_id,))]
        require(state == (binding["proceed_ref"], revision, request["authorizationEpoch"]) and
                binding["authorization"]["authorization_ref"] == binding["proceed_ref"],
                "lifecycle_grant_mismatch")
        require(len(started) == 1 and started[0][1].startswith("terminal:") and
                started[0][0] == str(uuid5(NAMESPACE_URL,
                    "workbench-important-v1:" + started[0][1])) and
                command == (task_id, revision) and
                request["sources"][0] in [span for group in spans for span in group],
                "confirmed_source_missing")
    except (KeyError, TypeError, ValueError, IndexError, sqlite3.Error, OSError) as exc:
        issues.append("contract:task_proof_unreadable:" + type(exc).__name__)
    return not issues, issues


def _task_contract(out: Path) -> dict:
    """Supply actual TaskRepository decision IDs before the unchanged SDK runner reserves.

    This is a trusted private fixture source, never a new grant or dispatch API.
    """
    original_factory, original_provider = run_contract._request, run_contract.Provider
    binding: dict[str, dict] = {}
    role_only: dict = {}
    life: SubjectLifecycle | None = None
    db_uuid: str | None = None

    def factory(repo, summary: str, skill_sha: str) -> tuple[dict, dict]:
        nonlocal life, db_uuid
        if life is None:
            directory = out / "private" / "important"
            with ImportantLogStore.create(directory) as store:
                db_uuid = store.db_uuid
            life = SubjectLifecycle(directory, db_uuid)
        if not role_only:
            # A user-labelled Task input is a real private backend record, but
            # it has neither a delegated Task grant nor a terminal start.
            user_task, message_id = str(uuid4()), str(uuid4())
            content = "user-only fixture input; no approved work scope"
            repo.create_task({"goal": content}, task_id=user_task)
            user_subject = {"schemaVersion": 1, "kind": "task", "id": user_task}
            life.record_delivery(user_subject, message_id=message_id, revision=1,
                                 kind="task_spec", content=content,
                                 delivered_at="2026-10-11T00:00:00Z")
            user_source = {"schemaVersion": 1, "sourceKind": "task_spec", "collectorEpoch": 1,
                           "taskId": user_task, "revision": 1, "messageId": message_id}
            raw_user = content.encode("utf-8")
            user_span = {"schemaVersion": 1, "source": user_source, "start": 0,
                         "end": len(raw_user), "digest": sha256(raw_user).hexdigest()}
            user_request = parse_summary_request({"schemaVersion": 1, "requestId": str(uuid4()),
                "subject": user_subject, "revision": 1, "authorizationRef": str(uuid4()),
                "authorizationEpoch": 1, "sources": [user_span], "sourceGaps": [],
                "priorCumulativeEntryId": None,
                "inputKey": summary_input_key(user_subject, 1, [user_span], [], skill_sha),
                "skillName": "q01-private-summary", "skillDigest": skill_sha,
                "dbHealth": run_origin._health(), "requestedAt": "2026-10-11T00:00:00Z"})
            decision = life.authorize_summary_request(user_request, active=True,
                paused=False, revoked=False, has_new_input=True)
            role_only.update({"trusted_probe_role": "user", "request": user_request,
                              "backend_decision": decision,
                              "subject_state": life.state(user_subject)})
        case = "positive" if not binding else "saved_then_bash"
        task_id, command_id = str(uuid4()), str(uuid4())
        repo.create_task({"goal": "private canonical Task work summary"}, task_id=task_id)
        scope_ref = repo.approve_scope(task_id, 1, {"paths": ["private/"]})
        proceed_ref = repo.proceed(task_id, 1, "run")
        run_id = repo.start_run(task_id, 1)
        stamp = "2026-10-11T00:00:00Z"
        seq = 11 if case == "positive" else 21
        life.record_flow({"seq": seq, "at": stamp, "type": "delegated", "task_id": task_id,
                          "revision": 1, "scope_decision_id": scope_ref,
                          "proceed_decision_id": proceed_ref})
        life.record_terminal({"seq": seq + 1, "at": stamp, "type": "terminal_started",
                              "command_id": command_id, "task_id": task_id,
                              "started_at": stamp}, revision=1)
        subject = {"schemaVersion": 1, "kind": "task", "id": task_id}
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                  "taskId": task_id, "revision": 1, "commandId": command_id}
        raw = summary.encode("utf-8")
        span = {"schemaVersion": 1, "source": source, "start": 0, "end": len(raw),
                "digest": sha256(raw).hexdigest()}
        life.record_terminal_range(subject, str(uuid4()), span, observed_at=stamp)
        grant = life.authority(subject, active=True, paused=False, revoked=False,
                               has_new_input=True)
        request = parse_summary_request({"schemaVersion": 1, "requestId": str(uuid4()),
            "subject": subject, "revision": 1,
            "authorizationRef": grant["authorization_ref"],
            "authorizationEpoch": grant["authorization_epoch"], "sources": [span],
            "sourceGaps": [], "priorCumulativeEntryId": None,
            "inputKey": summary_input_key(subject, 1, [span], [], skill_sha),
            "skillName": "q01-private-summary", "skillDigest": skill_sha,
            "dbHealth": run_origin._health(), "requestedAt": stamp})
        admission = life.authorize_summary_request(request, active=True, paused=False,
            revoked=False, has_new_input=True)
        if not admission["allowed"] or grant["authorization_ref"] != proceed_ref:
            raise RuntimeError("canonical Task grant/source preflight failed")
        negatives = {
            "paused": life.authorize_summary_request(request, active=True, paused=True,
                revoked=False, has_new_input=True),
            "revoked": life.authorize_summary_request(request, active=True, paused=False,
                revoked=True, has_new_input=True),
            "wrong_grant": life.authorize_summary_request({**request, "authorizationRef": str(uuid4())},
                active=True, paused=False, revoked=False, has_new_input=True),
        }
        binding[case] = {"task_id": task_id, "command_id": command_id,
                         "scope_ref": scope_ref, "proceed_ref": proceed_ref,
                         "run_id": run_id, "request_id": request["requestId"],
                         "authorization": grant, "admission": admission,
                         "selected_span": span, "negative_admissions": negatives}
        return request, span

    try:
        run_contract._request = factory
        run_contract.Provider = run_origin.RawProvider
        result = run_contract.run(out)
    finally:
        run_contract._request = original_factory
        run_contract.Provider = original_provider
    private = out / "private"
    actual_request = result["requests"]["positive"]
    typed = json.loads((private / "typed-positive.json").read_text())
    actual = binding["positive"]
    with run_contract.TaskRepository(private / "backend.sqlite3") as repo:
        role_only["backend_request_record"] = repo.get_summary_request(role_only["request"]["requestId"])
    with sqlite3.connect("file:" + str(private / "important" / (db_uuid + ".sqlite3")) + "?mode=ro",
                         uri=True) as conn:
        role_only["confirmed_terminal_starts"] = conn.execute(
            "SELECT COUNT(*) FROM subject_events_v1 WHERE subject_kind='task' AND subject_id=? AND event_kind='terminal_started'",
            (role_only["request"]["subject"]["id"],)).fetchone()[0]
    checks = {"actual_decision_ref": actual_request["authorizationRef"] == actual["proceed_ref"] and
              actual["authorization"]["authorization_ref"] == actual["proceed_ref"],
              "actual_revision": actual_request["revision"] == 1 and
              actual_request["subject"] == {"schemaVersion": 1, "kind": "task", "id": actual["task_id"]},
              "actual_selected_source": actual_request["sources"] == [actual["selected_span"]] and
              typed["sources"] == actual_request["sources"],
              "actual_request_result": typed["requestId"] == actual_request["requestId"] and
              typed["inputKey"] == actual_request["inputKey"] and
              result["backend_controls"]["checks"]["actual_accepted"],
              "contract_success": result.get("stop_reason") is None and all(result["checks"].values()),
              "role_only_no_grant_denied": role_only["trusted_probe_role"] == "user" and
              role_only["backend_decision"] == {"allowed": False, "reason": "no_matching_task_grant"} and
              role_only["subject_state"].get("grant_ref") is None and
              role_only["confirmed_terminal_starts"] == 0,
              "role_only_not_dispatched": role_only["backend_request_record"] is None and
              role_only["request"]["requestId"] not in
              [request["requestId"] for request in result["requests"].values()]}
    (out / "task-binding.json").write_text(json.dumps({"important_db_uuid": db_uuid,
        "bindings": binding, "role_only": role_only, "checks": checks,
        "raw_posts": [{"path": str(path), "sha256": _sha(path)}
                      for path in sorted(private.glob("raw-post-*.json"))]}, indent=2) + "\n")
    result["task_binding"] = {"path": str(out / "task-binding.json"), "checks": checks,
                              "bindings": binding, "role_only": role_only}
    return result


def prepare(context: dict) -> dict:
    base = Path(context["isolated_root"]).resolve(strict=True)
    if not base.is_relative_to(WORK) or base == WORK:
        raise ValueError("current Q01 context must use its assigned private namespace")
    for key in ("home", "project", "data", "socket_dir"):
        path = Path(context[key]).resolve(strict=True)
        if path == base or not path.is_relative_to(base):
            raise ValueError("private context path escapes its namespace: " + key)
    output = base / "prepare"
    evidence = run_prepare.run(output)
    readback = _events(evidence["trace"], "readback")
    if evidence["provider_posts"] or len(readback) != 1 or not all(evidence["checks"].values()):
        raise RuntimeError("actual transmission-free prepare incomplete")
    definition = readback[0].get("agentDefinition")
    if definition != {"name": "task", "source": "bundled", "filePath": "embedded:task.md"}:
        raise RuntimeError("actual installed bundled Task identity mismatch")
    model = readback[0].get("model", {})
    route = {"route": "existing-parent/private-sdk-child", "role": "Manager/task",
             "agent_definition_sha256": readback[0].get("bodySha"),
             "skill_sha256": readback[0].get("skillSha"),
             "resolved_model": str(model.get("provider")) + "/" + str(model.get("id")),
             "provider": model.get("provider"), "context_limit_tokens": model.get("contextWindow"),
             "selection_epoch": readback[0].get("sessionId"),
             "harness": "q01-current-" + _sha(Path(__file__)), "source": "omp_readback"}
    return {"route_identity": route, "isolated_root": str(base),
            "prepare_evidence": str(output / "prepare-evidence.json"),
            "prepared_readback": readback[0], "closed": False}


def close(prepared: dict) -> None:
    prepared["closed"] = True


def collect(prepared: dict) -> dict:
    if prepared.get("closed"):
        raise ValueError("prepared Q01 context is closed")
    base = Path(prepared["isolated_root"])
    stages: dict[str, dict] = {"prepare": json.loads(Path(prepared["prepare_evidence"]).read_text())}
    stage_paths: dict[str, Path] = {"prepare": Path(prepared["prepare_evidence"])}
    errors: list[str] = []

    def stage(name: str, call: Callable[[Path], dict], artifact: str) -> None:
        # The G3 bridge uses AF_UNIX; this one stage needs a short private path.
        output = base / ("p" if name == "priority_runtime" else name)
        value = call(output)
        stages[name] = value
        stage_paths[name] = output / artifact
        errors.extend(_source_issues(name, value))
        if value.get("error") or value.get("stop_reason"):
            errors.append(name + ":runtime_error:" + str(value.get("error") or value.get("stop_reason")))

    try:
        stage("full", lambda out: (run_full.main(out), json.loads((out / "runtime-evidence.json").read_text()))[1],
              "runtime-evidence.json")
        stage("contract", _task_contract, "contract-evidence.json")
        stage("context", lambda out: _with_mode("cycles", run_context.run, out), "cycles.json")
        stage("budget", lambda out: _with_mode("full", run_budget.run, out), "full.json")
        stage("priority_input", run_priority_input.run, "priority-input-evidence.json")
        stage("priority_runtime", run_priority_runtime.run, "runtime-evidence.json")
        stage("recovery", run_recovery.run, "recovery-evidence.json")
        stage("origin", run_origin.run, "result.json")
    except Exception as exc:
        errors.append("batch_exception:" + type(exc).__name__ + ":" + str(exc))
    (base / "stage-index.json").write_text(json.dumps({
        "stages": {name: {"artifact": str(stage_paths[name]),
                          "sha256": _sha(stage_paths[name]) if stage_paths[name].exists() else None,
                          "task_binding": ({"artifact": value["task_binding"]["path"],
                                            "sha256": _sha(Path(value["task_binding"]["path"]))}
                                           if name == "contract" and value.get("task_binding") else None),
                          "checks": value.get("checks"),
                          "version": value.get("omp_version"),
                          "binary_sha256": value.get("omp_binary_sha256", value.get("omp_binary_sha", value.get("omp_sha256")))}
                   for name, value in stages.items()}, "errors": errors,
        "current_probe_sha256": _sha(Path(__file__)),
        "live_port_sha256": _sha(HERE.parent / "live_summary_feasibility.py")},
        ensure_ascii=False, indent=2) + "\n")
    if set(stages) != {"prepare", "full", "contract", "context", "budget", "priority_input",
                       "priority_runtime", "recovery", "origin"}:
        rows = [_rows({item: {} for item in ITEMS}, {item: base / "stage-index.json" for item in ITEMS})[i]
                for i in range(len(ITEMS))]
        for row in rows:
            row["unknowns"] = errors or ["batch_incomplete"]
        return {"observations": rows, "unknowns": errors or ["batch_incomplete"]}
    return _compose(prepared, stages, stage_paths, errors)


def _compose(prepared: dict, stages: dict[str, dict], paths: dict[str, Path], errors: list[str]) -> dict:
    prep, full, contract = (stages[name] for name in ("prepare", "full", "contract"))
    context, budget = (stages[name] for name in ("context", "budget"))
    priority_input, priority = (stages[name] for name in ("priority_input", "priority_runtime"))
    priority, priority_shape_issues = _priority_shape(priority)
    errors.extend(priority_shape_issues)
    errors.extend(_priority_wire_issues(priority, paths["priority_runtime"]))
    recovery, origin = (stages[name] for name in ("recovery", "origin"))
    full_trace = full.get("trace", [])
    contract_trace = contract.get("trace", [])
    full_posts = full.get("provider_posts", [])
    contract_posts = contract.get("provider_posts", [])
    full_counts = Counter(post.get("case") for post in full_posts)
    prep_readback = prepared["prepared_readback"]
    definition = {"name": "task", "source": "bundled", "filePath": "embedded:task.md"}
    contract_begin = _events(contract_trace, "begin")
    contract_private = paths["contract"].parent / "private"
    body = (contract_private / "bundle-body.txt").read_text() if (contract_private / "bundle-body.txt").exists() else ""
    skill = SKILL.read_text()
    raw_contract = sorted(contract_private.glob("raw-post-*.json"))
    actual_system = []
    for raw in raw_contract:
        payload = json.loads(raw.read_bytes())
        actual_system.append([_text(m.get("content")) for m in payload.get("messages", [])
                              if m.get("role") == "system"])
    exact_system = bool(body) and bool(actual_system) and all(
        value == [body + "\n\n" + skill] for value in actual_system)
    observed_definition = contract_begin[0].get("definition", {}) if contract_begin else {}
    bundled = (prep_readback.get("agentDefinition") == definition and
               prep_readback.get("bodySha") == sha256(body.encode()).hexdigest() and
               len(contract_begin) == 1 and
               all(observed_definition.get(key) == value for key, value in definition.items()) and
               observed_definition.get("bodySha") == prep_readback.get("bodySha"))
    normal = ("normal", "normal2", "normal3")
    all_owned_stopped, owned_processes = _owned_stopped(stages)
    if not all_owned_stopped:
        errors.append("owned_process_not_stopped")
    normal_sessions = [_events(full_trace, "session_created", case) for case in normal]
    isolated = all(len(value) == 1 for value in normal_sessions) and len({
        value[0].get("sessionId") for value in normal_sessions if value}) == 3
    positives = ("normal", "normal2", "normal3", "recover_missing", "recover_noop",
                 "recover_api", "recover_stale", "recover_exception")
    receipt_request = True
    receipt_epoch = True
    positive_receipts: list[dict] = []
    for case in positives:
        admitted, before = (_events(full_trace, name, case) for name in ("startup_admitted", "before_prompt"))
        saved = [event for event in _events(full_trace, "tool_end", case)
                 if event.get("toolName") == "db_summary_save" and event.get("isError") is False]
        if len(admitted) != 1 or len(before) != 1 or len(saved) != 1:
            receipt_request = receipt_epoch = False
            continue
        issued, prompted = admitted[0].get("receipt"), before[0].get("receipt")
        try:
            tool_result = json.loads(saved[0]["result"])
        except (ValueError, TypeError, KeyError):
            tool_result = {}
        if not isinstance(issued, dict) or not isinstance(prompted, dict):
            receipt_request = receipt_epoch = False
            continue
        positive_receipts.append(issued)
        receipt_request &= (issued == prompted and issued.get("requestId") == "q01-" + case and
                            tool_result.get("request_id") == issued.get("requestId") and
                            len(_events(full_trace, "result_accepted", case)) == 1)
        receipt_epoch &= (issued.get("sessionId") == tool_result.get("session_id") and
                          bool(issued.get("epoch")) and
                          bool(_events(full_trace, "session_created", case)) and
                          issued.get("sessionId") == _events(full_trace, "session_created", case)[-1].get("sessionId"))

    def zero_block(case: str, reason: str) -> bool:
        return (full_counts[case] == 0 and len(_events(full_trace, "startup_admitted", case)) == 1 and
                any(item.get("reason") == reason for item in _events(full_trace, "stream_guard", case)) and
                len(_events(full_trace, "result_rejected", case)) == 1)

    # Parent-body attribution uses raw provider request bytes, not the SDK's
    # self-report or an immediate custom-entry count.
    context_posts = context.get("posts", [])
    parent_posts = [post for post in context_posts if post.get("kind") == "parent"]
    child_posts = [post for post in context_posts if post.get("kind") == "child"]
    receipts = _events(context.get("trace", []), "cycle_receipt_sent")
    parent_three = len(parent_posts) == 3 and len(child_posts) == 6 and len(receipts) == 3
    parent_receipt = parent_three and all(
        isinstance(item.get("bytes"), int) and item["bytes"] <= 4096 and
        parent_posts[index].get("cycle_receipts", [0, 0, 0])[index] == 1
        for index, item in enumerate(receipts))
    parent_tools = (len(parent_posts) == 3 and all(post.get("tools") == parent_posts[0].get("tools")
        for post in parent_posts) and
        {"read", "bash", "edit", "write"}.issubset(set(parent_posts[0].get("tools") or [])))
    no_parent_raw = parent_three and all(all(count == 0 for count in post.get("raw_markers", []))
                                          for post in parent_posts)
    budget_posts = budget.get("posts", [])
    # Check the complete serialized provider request, including system,
    # messages, tool schema and native framing. The selected prompt is only
    # one component of this body.
    budget_wire_bytes = []
    for post in budget_posts:
        raw_path = post.get("raw_body_path")
        if not isinstance(raw_path, str) or not Path(raw_path).is_file():
            continue
        raw = Path(raw_path).read_bytes()
        if (sha256(raw).hexdigest() == post.get("sha256") and
                len(raw) == post.get("bytes")):
            budget_wire_bytes.append(len(raw))
    budget_trace = budget.get("trace", [])
    selection = next((item for item in budget_trace if item.get("event") == "selection"), {})
    preflights = _events(budget_trace, "dispatch_preflight")
    budget_cap = len(budget_posts) == 2 and all(8192 in (
        post.get("max_completion_tokens"), post.get("max_tokens")) for post in budget_posts)
    budget_admitted = len(preflights) == 2 and all(
        item.get("admitted") is True and item.get("reason") is None and
        item.get("contextBytes", 10**9) <= item.get("inputLimit", 0) and
        item.get("conservativeWireBound", 10**9) <= 32768 for item in preflights)
    typed_contract = contract.get("backend_controls", {}).get("checks", {})
    contract_task = contract.get("task_binding")
    contract_task = contract_task if isinstance(contract_task, dict) else {}
    contract_task_checks = contract_task.get("checks", {})
    contract_task_checks = contract_task_checks if isinstance(contract_task_checks, dict) else {}
    task_proof, task_proof_issues = _task_contract_proof(contract, paths["contract"])
    errors.extend(task_proof_issues)
    origin_negative = origin.get("negative", {})
    origin_actual = json.loads(Path(origin["actual_runtime"]).read_text())
    origin_typed = Path(origin["actual_runtime"]).parent / "private/typed-positive.json"
    actual_typed_bytes = (origin_typed.stat().st_size if origin_typed.exists() else 10**9)

    # The G3 frame's notice ID must appear in an actual parent provider POST
    # before the held DB child is released. This is dispatch priority only;
    # backend summary-request merging remains outside DVL-08.
    role_posts = {role: row.get("posts", []) for role, row in priority.get("providers", {}).items()}
    release_ns = priority.get("child_release_signal_ns", -1)
    bridge_requests = priority.get("bridge", {}).get("requests", [])
    def notice_precedes(role: str, kind: str) -> bool:
        submitted = [item for item in bridge_requests if item.get("role") == role and
                     item.get("frame", {}).get("kind") == "notice" and
                     item.get("frame", {}).get("notice", {}).get("type") == kind and
                     item.get("ack", {}).get("status") == "api_accepted"]
        return type(release_ns) is int and 0 < release_ns <= 2**63 - 1 and any(any(
            post.get("stage") == "parent" and
            post.get("notice_id") == item["frame"]["notice"].get("notice_id") and
            _priority_body_notice_matches(post, role, kind, item["frame"]["notice"].get("notice_id")) and
            type(item.get("sent_ns")) is int and type(post.get("received_ns")) is int and
            0 < item["sent_ns"] <= post["received_ns"] < release_ns
            for post in role_posts.get(role, [])) for item in submitted)
    end_notice = notice_precedes("manager", "worker_terminal_done")
    due_notice = (notice_precedes("worker", "terminal_check") and
                  priority.get("scheduler", {}).get("tick60", {}).get("status") == "dispatched")
    timing = priority.get("timing", {})
    priority_measurement = _priority_measurement(priority)
    notice_windows = priority_measurement["notice_windows"]
    session_ids = list(priority_measurement["session_ids"].values())
    distinct_sessions = (all(isinstance(value, str) and value for value in session_ids) and
                         len(set(value for value in session_ids if isinstance(value, str))) == 3)
    observed_overlap = len(notice_windows) == 2 and all(
        item["turn_hold_overlap_ms"] > 0 for item in notice_windows)
    input_attempts = {item.get("attempt"): item for item in priority_input.get("priority_trace", [])
                      if item.get("event") == "admission_attempt"}
    input_deferred = [item for item in priority_input.get("priority_trace", [])
                      if item.get("event") == "deferred"]

    journal = recovery.get("journal", {})
    rec_checks = recovery.get("checks", {})
    bindings = contract_task.get("bindings")
    bindings = bindings if isinstance(bindings, dict) else {}
    origin_task_binding = bindings.get("positive")
    origin_task_binding = origin_task_binding if isinstance(origin_task_binding, dict) else {}
    task_negative = origin_task_binding.get("negative_admissions")
    task_negative = task_negative if isinstance(task_negative, dict) else {}
    task_negative = {key: value for key, value in task_negative.items() if isinstance(value, dict)}
    task_admission = origin_task_binding.get("admission")
    task_admission = task_admission if isinstance(task_admission, dict) else {}
    role_only = contract_task.get("role_only")
    role_only = role_only if isinstance(role_only, dict) else {}
    origin_span = origin.get("cases", {}).get("positive", {}).get("selected_confirmed_span")
    origin_request = origin_actual.get("requests", {}).get("positive", {})
    origin_command = origin.get("cases", {}).get("positive", {}).get("command_id")
    origin_db = Path(origin["actual_runtime"]).parent / "private/important" / (str(origin.get("important_db_uuid")) + ".sqlite3")
    start_event = None
    selected_persisted = False
    if origin_db.is_file() and origin_command:
        with sqlite3.connect("file:" + str(origin_db) + "?mode=ro", uri=True) as conn:
            start_event = conn.execute("""SELECT source_id,event_key FROM subject_events_v1
                WHERE subject_kind='command' AND subject_id=? AND event_kind='terminal_started'""",
                (origin_command,)).fetchone()
            selected_persisted = any(origin_span in json.loads(row[0]) for row in conn.execute(
                "SELECT sources_json FROM entries WHERE subject_kind='command' AND subject_id=?",
                (origin_command,)))
    origin_source_bound = (origin_span is not None and origin_request.get("sources") == [origin_span] and
                           selected_persisted and
                           origin.get("checks", {}).get("actual_typed_same_scope") is True and
                           origin_negative.get("spoofed_digest", {}).get("reason") ==
                           "unrelated_or_unobserved_source_range")

    facts = {
        "Q01-route": {
            "existing_agent": bundled and exact_system,
            "isolated_turn": isolated,
            "typed_return": task_proof and bool(contract_task_checks.get("actual_request_result")) and
                            bool(origin.get("checks", {}).get("actual_typed_same_scope")),
            "no_new_agent": bundled and prep_readback.get("fixtureMatches") is False,
            "no_standing_process": all_owned_stopped},
        "Q01-tools": {
            "receipt_request_bound": receipt_request and len(positive_receipts) == len(positives),
            "session_turn_epoch_bound": receipt_epoch and len(positive_receipts) == len(positives),
            "receipt_route_bound": all(item.get("route") == "private-manager-task" for item in positive_receipts)
                                   and zero_block("runtime_route", "stale_binding"),
            "policy_nonce_bound": len({item.get("nonce") for item in positive_receipts}) == len(positives)
                                  and zero_block("runtime_nonce", "stale_binding"),
            "startup_private_config_verified": prep.get("checks", {}).get("readback_private_sole_db") is True,
            "startup_sole_db_tool_readback": prep_readback.get("active") == ["db_summary_save"] and
                                              all(post.get("tools") == ["db_summary_save"] for post in full_posts),
            "startup_guard_effective_verified": all(_events(full_trace, "startup_rejected", c)
                for c in ("recover_missing", "recover_noop", "recover_api", "recover_stale", "repeat_fail")),
            "startup_missing_guard_prompt_sent_zero": any(e.get("attempt") == 1 and
                e.get("reason") == "guard_api_or_missing" for e in
                _events(full_trace, "startup_rejected", "recover_missing")) and
                not any(e.get("attempt") == 1 for e in _events(full_trace, "prompt_start", "recover_missing")),
            "startup_noop_guard_prompt_sent_zero": any(e.get("attempt") == 1 and
                e.get("reason") == "ineffective_guard" for e in
                _events(full_trace, "startup_rejected", "recover_noop")) and
                not any(e.get("attempt") == 1 for e in _events(full_trace, "prompt_start", "recover_noop")),
            "startup_api_mismatch_prompt_sent_zero": any(e.get("attempt") == 1 and
                e.get("reason") == "guard_api_or_missing" for e in
                _events(full_trace, "startup_rejected", "recover_api")) and
                not any(e.get("attempt") == 1 for e in _events(full_trace, "prompt_start", "recover_api")),
            "startup_stale_receipt_prompt_sent_zero": any(e.get("attempt") == 1 and
                e.get("reason") == "stale_or_invalid_valid_case" for e in
                _events(full_trace, "startup_rejected", "recover_stale")) and
                not any(e.get("attempt") == 1 for e in _events(full_trace, "prompt_start", "recover_stale")),
            "startup_failed_session_disposed": all(len(_events(full_trace, "disposed", c)) == 2
                for c in ("recover_missing", "recover_noop", "recover_api", "recover_stale", "recover_exception")),
            "startup_rebuild_once_reverified": all(len(_events(full_trace, "startup_selfcheck", c)) == 2 and
                len(_events(full_trace, "startup_admitted", c)) == 1 for c in
                ("recover_missing", "recover_noop", "recover_api", "recover_stale")) and
                len(_events(full_trace, "disposed", "recover_exception")) == 2 and
                len(_events(full_trace, "startup_selfcheck", "recover_exception")) == 1 and
                _events(full_trace, "startup_selfcheck", "recover_exception")[0].get("attempt") == 2 and
                len(_events(full_trace, "startup_admitted", "recover_exception")) == 1,
            "startup_repeated_failure_prompt_sent_zero": full_counts["repeat_fail"] == 0 and
                not _events(full_trace, "prompt_start", "repeat_fail"),
            "startup_repeated_failure_summary_deferred": len(_events(full_trace, "summary_deferred", "repeat_fail")) == 1,
            "startup_repeated_failure_main_continues": any(frame.get("id") == "full-parent-after" and
                not frame.get("error") for frame in full.get("rpc_frames", [])),
            "runtime_binding_pre_dispatch": all(zero_block(c, r) for c, r in (
                ("runtime_request", "stale_binding"), ("runtime_route", "stale_binding"),
                ("runtime_nonce", "stale_binding"), ("runtime_model", "model_mismatch"),
                ("runtime_policy", "policy_mismatch"))),
            "runtime_request_mismatch_sent_zero": zero_block("runtime_request", "stale_binding"),
            "runtime_roster_mismatch_sent_zero": zero_block("runtime_roster", "roster_mismatch"),
            "runtime_model_mismatch_sent_zero": zero_block("runtime_model", "model_mismatch"),
            "runtime_policy_mismatch_sent_zero": zero_block("runtime_policy", "policy_mismatch"),
            "malicious_tools_blocked": any(item.get("toolName") == "bash" and item.get("isError") is True
                for item in _events(full_trace, "tool_end", "bash")),
            "sentinel_unchanged": set(full.get("sentinels", {})) == {"command-sentinel", "file-sentinel"} and
                set(contract.get("sentinels", {})) == {"command-sentinel", "file-sentinel"} and
                all(value == "ORIGINAL" for value in full["sentinels"].values()) and
                all(value == "ORIGINAL" for value in contract["sentinels"].values()),
            "post_receipt_violation_rejected": len(_events(contract_trace, "result_rejected", "saved_then_bash")) == 1 and
                len(_events(contract_trace, "abort_completed", "saved_then_bash")) == 1 and
                any(e.get("toolName") == "db_summary_save" and e.get("isError") is False for e in
                    _events(contract_trace, "tool_end", "saved_then_bash")) and
                not _events(contract_trace, "result_accepted", "saved_then_bash") and
                not (contract_private / "typed-saved_then_bash.json").exists(),
            "forbidden_event_immediate_protection": full_counts["bash"] == 1 and
                len(_events(full_trace, "abort_completed", "bash")) == 1,
            "upper_turn_unaffected": parent_tools and
                any(frame.get("id") == "full-parent-after" and not frame.get("error")
                    for frame in full.get("rpc_frames", [])),
            "normal_permissions_restored": parent_tools,
            "no_extra_model_turn": full_counts["bash"] == 1 and full.get("checks", {}).get("bash_abort_POST_one") is True,
        },
        "Q01-skill": {
            "exact_bytes_in_context": exact_system,
            "digest_matched": _sha(SKILL) == prep_readback.get("skillSha") and
                              contract_begin[0].get("definition", {}).get("bodySha") == prep_readback.get("bodySha")
                              if contract_begin else False,
            "role_filter_enforced": len(_events(contract_trace, "role_rejected", "worker_role")) == 1 and
                not any(post.get("case") == "worker_role" for post in contract_posts) and
                any(event.get("reason") == "approved_task_manager" for event in
                    _events(contract_trace, "role_admission", "positive")),
            "isolated_skill_source": prep_readback.get("privateHas") is True and
                prep_readback.get("globalHas") is False,
        },
        "Q01-result": {
            "typed_channel": task_proof and all(contract_task_checks.values()) and
                origin.get("checks", {}).get("actual_typed_same_scope") is True,
            "wrong_id_rejected": typed_contract.get("wrong_id_rejected") is True,
            "wrong_revision_rejected": typed_contract.get("wrong_revision_rejected") is True,
            "wrong_range_rejected": typed_contract.get("wrong_range_rejected") is True,
            "duplicate_rejected": typed_contract.get("duplicate_rejected") is True,
        },
        "Q01-context": {
            "three_turns_observed": parent_three and len(context.get("cycles", [])) == 3,
            "parent_receipt_within_4k": parent_receipt,
            "result_within_8k": actual_typed_bytes <= 8192 and
                                  budget.get("typed_contract", {}).get("bytes", 10**9) <= 8192,
            "body_within_32k": len(budget_posts) == len(budget_wire_bytes) == 2 and
                               all(0 < size <= 32768 for size in budget_wire_bytes),
            "collector_within_1m": selection.get("collectedBytes") == 1048576,
            "token_preflight": budget_admitted and len(budget_posts) == 2,
            "output_ceiling_enforced": budget_cap and all(item.get("fittedCap") == 8192 for item in preflights),
            "full_output_reserved": budget_admitted and all(item.get("outputReserve", 0) >= 8192
                                                          for item in preflights),
            "no_main_raw_accumulation": no_parent_raw,
        },
        "Q01-priority": {
            "busy_deferred": input_attempts.get(2, {}).get("idle") is False and
                any(item.get("attempt") == 2 and item.get("reason") == "busy" for item in input_deferred),
            "user_priority": input_attempts.get(1, {}).get("editor") == run_priority_input.DRAFT and
                any(item.get("attempt") == 1 and item.get("reason") == "draft" for item in input_deferred) and
                priority_input.get("checks", {}).get("actual_draft_deferred") is True and
                priority.get("manager_draft_ack", {}).get("status") == "deferred",
            "due_check_priority": due_notice,
            "end_priority": end_notice,
            # These windows measure dispatch/start delay and concurrent turn
            # occupancy. G3 does not expose actual internal lock-wait spans;
            # a maximum blocking duration cannot be inferred from latency.
            "max_block_recorded": None,
            "shared_session_or_lock": False if distinct_sessions and observed_overlap else None,
            "measured_block_ms": None,
            "independent_delivery_observed": end_notice and due_notice,
            "nonshared_priority_measurement": priority_measurement,
        },
        "Q01-recovery": {
            "pause_blocks_new": journal.get("paused_gate", {}).get("postCount") == 0 and
                journal.get("paused_gate", {}).get("deferred") is True,
            "disconnect_reconciled": journal.get("reopened_live_unknown", {}).get("state") == "unknown" and
                journal.get("live_late_accepted") is True,
            "restart_reconciled": journal.get("reopened_killed_unknown", {}).get("state") == "unknown" and
                rec_checks.get("fresh_nonoverlap_saved") is True,
            "late_result_bound": journal.get("live_typed_exact") is True and
                journal.get("live_duplicate_dropped") is True,
            "unknown_not_replayed": rec_checks.get("exact_overlap_rejected") is True and
                journal.get("killed_first_post_before_stop") == 1 and
                len(recovery.get("provider_posts", [])) == 5,
        },
        "Q01-origin": {
            "task_grant_bound": task_proof and all(contract_task_checks.values()) and
                task_admission.get("allowed") is True and
                task_negative.get("wrong_grant", {}).get("reason") == "work_scope_authorization_mismatch",
            "command_start_bound": start_event is not None and
                origin_request.get("authorizationRef") == start_event[0] and
                origin_request.get("authorizationEpoch") == int(start_event[1].split(":", 1)[1]),
            "work_source_ranges_bound": task_proof and origin_source_bound and
                typed_contract.get("wrong_range_rejected") is True,
            "terminal_close_bound": origin.get("cases", {}).get("positive", {}).get("closing") is True and
                origin.get("post_end", {}).get("reason") == "command_summary_scope_expired",
            "unrelated_scope_rejected": origin_negative.get("wrong_ref", {}).get("reason") ==
                "work_scope_authorization_mismatch" and origin_source_bound,
            "notice_not_authority": origin_negative.get("notice_only", {}).get("allowed") is False,
            "user_role_not_authority": role_only.get("trusted_probe_role") == "user" and
                role_only.get("backend_decision") == {"allowed": False, "reason": "no_matching_task_grant"} and
                role_only.get("confirmed_terminal_starts") == 0 and
                role_only.get("backend_request_record", object()) is None and
                contract_task_checks.get("role_only_no_grant_denied") is True and
                contract_task_checks.get("role_only_not_dispatched") is True,
            "pause_revoke_preserved": all(task_negative.get(name, {}).get("allowed") is False
                for name in ("paused", "revoked")) and
                origin_negative.get("paused", {}).get("allowed") is False and
                origin_negative.get("revoked", {}).get("allowed") is False,
            "end_once_no_replay": origin.get("post_end", {}).get("allowed") is False and
                rec_checks.get("exact_overlap_rejected") is True,
        },
    }
    binary = prep.get("omp_binary_sha256")
    version = prep.get("omp_version")
    for name, value in stages.items():
        current_binary = value.get("omp_binary_sha256", value.get("omp_binary_sha", value.get("omp_sha256")))
        if name == "origin":
            current_binary = value.get("omp_binary_sha")
        if current_binary != binary or value.get("omp_version") != version:
            errors.append(name + ":installed_omp_binary_or_version_changed")
    for name, posts in (("full", full_posts), ("context", context_posts),
                        ("budget", budget_posts), ("priority_input", priority_input.get("posts", [])),
                        ("priority_runtime", [post for role in role_posts.values() for post in role]),
                        ("recovery", recovery.get("provider_posts", []))):
        errors.extend(_raw_issues(name, posts))
    for name, result, posts in (("contract", contract, contract_posts),
                                ("origin", origin_actual, origin_actual.get("provider_posts", []))):
        private = Path(paths[name]).parent / ("private" if name == "contract" else "actual/private")
        raw_files = sorted(private.glob("raw-post-*.json"))
        if len(raw_files) != len(posts) or sorted(_sha(path) for path in raw_files) != sorted(
            post.get("body_sha") for post in posts):
            errors.append(name + ":raw_http_body_unbound")
    result_ref = contract_task.get("path")
    result_ref = Path(result_ref) if isinstance(result_ref, str) else paths["contract"]
    rows = _rows(facts, {
        "Q01-route": paths["contract"], "Q01-tools": paths["full"],
        "Q01-skill": paths["contract"], "Q01-result": result_ref,
        "Q01-context": paths["context"], "Q01-priority": paths["priority_runtime"],
        "Q01-recovery": paths["recovery"], "Q01-origin": paths["origin"]})
    boundaries = {"operating_model_metadata": "unobserved; each stage uses private scripted capability",
                  "provider_capabilities": {name: {"model": value.get("omp_version"),
                      "binary_sha256": value.get("omp_binary_sha256", value.get("omp_binary_sha", value.get("omp_sha256")))}
                      for name, value in stages.items()},
                  "origin_summary_quality": "unobserved: scripted marker content only",
                  "origin_role_only_negative": "trusted probe role=user Task spec denied by actual SubjectLifecycle before reserve/dispatch; no real user utterance observed",
                  "product_09_10_14_wiring": "unobserved; probe-local fixed collector",
                  "priority_semantics": "parent end notice and Worker due check delivery during held DB child; backend summary-request merge not assessed",
                  "priority_measurement": priority_measurement,
                  "priority_notice_windows": notice_windows,
                  "priority_child_hold_interval_ns": [priority.get("child_hold_started_ns"), release_ns],
                  "priority_timing": timing,
                  "priority_session_ids_distinct": distinct_sessions,
                  "priority_block_limit": "internal lock-wait span unavailable; observed enqueue/start/POST delays are not blocking durations",
                  "budget_complete_wire_body_bytes": budget_wire_bytes,
                  "selected_range_bytes": selection.get("selectedBytes"),
                  "omitted_bytes": selection.get("omittedBytes"),
                  "fixture_total_bytes": selection.get("collectedBytes"),
                  "source_custody_errors": errors}
    boundaries["owned_processes"] = owned_processes
    (Path(prepared["isolated_root"]) / "capability-boundaries.json").write_text(
        json.dumps(boundaries, ensure_ascii=False, indent=2) + "\n")
    (Path(prepared["isolated_root"]) / "current-observations.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    return {"observations": rows, "unknowns": errors}
