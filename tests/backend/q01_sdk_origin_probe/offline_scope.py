"""Deterministic LG-D24 source and grant controls against private fixture records."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
from uuid import uuid4

from workbench.contracts.logs_v1 import LogContractError, parse_summary_request, summary_input_key
from workbench.storage.important_log import ImportantLogStore, SubjectLifecycle


AT = "2026-10-11T00:00:00Z"


def run(base: Path) -> dict:
    base = base.resolve()
    actual = base / "origin-actual01" / "actual" / "private"
    request = json.loads((actual / "requests.json").read_text())["positive"]
    meta = json.loads((base / "origin-actual01" / "result.json").read_text())
    life = SubjectLifecycle(actual / "important", meta["important_db_uuid"])
    span = request["sources"][0]
    decisions: dict[str, dict] = {}

    def check(name: str, candidate: dict) -> None:
        try:
            decisions[name] = life.authorize_summary_request(candidate, active=False,
                paused=False, revoked=False, has_new_input=True, closing_event=True)
        except (LogContractError, ValueError) as exc:
            decisions[name] = {"allowed": False, "reason": type(exc).__name__ + ": " + str(exc)}

    changed = {**span, "end": span["end"] + 1}
    check("changed_range", {**request, "sources": [changed], "inputKey":
        summary_input_key(request["subject"], None, [changed], [], request["skillDigest"])})
    foreign = {**span, "source": {**span["source"],
               "commandId": meta["cases"]["saved_then_bash"]["command_id"]}}
    check("foreign_command_source", {**request, "sources": [foreign], "inputKey":
        summary_input_key(request["subject"], None, [foreign], [], request["skillDigest"])})
    gap = {"schemaVersion": 1, "source": span["source"], "start": 0,
           "end": span["end"], "reason": "collector_gap"}
    check("invented_gap", {**request, "sources": [], "sourceGaps": [gap], "inputKey":
        summary_input_key(request["subject"], None, [], [gap], request["skillDigest"])})

    task_dir = base / "offline-task-important"
    with ImportantLogStore.create(task_dir) as store:
        task_db_uuid = store.db_uuid
    task_life = SubjectLifecycle(task_dir, task_db_uuid)
    task_id, command_id = str(uuid4()), str(uuid4())
    subject = {"schemaVersion": 1, "kind": "task", "id": task_id}
    grant = str(uuid4())
    task_life.record_flow({"seq": 10, "at": AT, "type": "delegated", "task_id": task_id,
        "revision": 1, "scope_decision_id": str(uuid4()), "proceed_decision_id": grant})
    task_life.record_terminal({"seq": 11, "at": AT, "type": "terminal_started",
        "command_id": command_id, "task_id": task_id, "started_at": AT}, revision=1)
    source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
              "taskId": task_id, "revision": 1, "commandId": command_id}
    raw = b"fixture selected task range"
    task_span = {"schemaVersion": 1, "source": source, "start": 0, "end": len(raw),
                 "digest": sha256(raw).hexdigest()}
    task_life.record_terminal_range(subject, str(uuid4()), task_span, observed_at=AT)
    authority = task_life.authority(subject, active=True, paused=False,
                                   revoked=False, has_new_input=True)
    task_request = parse_summary_request({**request, "requestId": str(uuid4()),
        "subject": subject, "revision": 1, "authorizationRef": authority["authorization_ref"],
        "authorizationEpoch": authority["authorization_epoch"], "sources": [task_span],
        "sourceGaps": [], "inputKey": summary_input_key(subject, 1, [task_span], [], request["skillDigest"])})
    for name, flags, candidate in (
        ("task_grant", {}, task_request),
        ("task_paused", {"paused": True}, task_request),
        ("task_revoked", {"revoked": True}, task_request),
        ("task_wrong_grant", {}, {**task_request, "authorizationRef": str(uuid4())}),
    ):
        decisions[name] = task_life.authorize_summary_request(candidate, active=True,
            paused=flags.get("paused", False), revoked=flags.get("revoked", False),
            has_new_input=True)
    checks = {"changed_range_denied": not decisions["changed_range"]["allowed"],
              "foreign_command_source_denied": not decisions["foreign_command_source"]["allowed"],
              "invented_gap_denied": not decisions["invented_gap"]["allowed"],
              "task_grant_allowed": decisions["task_grant"]["allowed"],
              "task_paused_denied": not decisions["task_paused"]["allowed"],
              "task_revoked_denied": not decisions["task_revoked"]["allowed"],
              "task_wrong_grant_denied": not decisions["task_wrong_grant"]["allowed"]}
    result = {"checks": checks, "decisions": decisions, "task_db_uuid": task_db_uuid}
    (base / "offline-controls01.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    result = run(Path(sys.argv[1]))
    print(json.dumps(result["checks"]))
    raise SystemExit(0 if all(result["checks"].values()) else 2)
