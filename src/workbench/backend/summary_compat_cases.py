"""Owned synthetic cases using the real Task, lifecycle, writer and shell ports.

These are explicit host admissions, not a Backend boot or a user command audit.
No credential, development artifact or model client is used here.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import shlex
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4

from workbench.backend.flow import HandoffJournal, HandoffService
from workbench.backend.flow_terminal import HostGate, TerminalService
from workbench.backend.launcher import choose_shell
from workbench.backend.panes import HostShellPort, ShellPane
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.summary_adapter import skill_bytes
from workbench.backend.summary_writer import store_summary
from workbench.contracts.logs_v1 import canonical_json, decode_json, parse_summary_request, summary_input_key
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog, ImportantLogReader, ImportantLogStore
from workbench.storage.important_log.lifecycle import source_json
from workbench.tasks.repository import TaskRepository

NAMES = ("disconnect", "restart", "normal2", "normal3", "worker", "roster_rebuild",
         "api_invalid", "stale_invalid", "saved_then_bash", "wrong_id", "wrong_revision", "wrong_range")
FLAGS = dict(active=True, paused=False, revoked=False, has_new_input=True)


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request(life, subject, sources, health, revision):
    auth = life.authority(subject, **FLAGS)
    digest = sha256(skill_bytes()).hexdigest()
    return parse_summary_request({"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject,
        "revision": revision, "authorizationRef": auth["authorization_ref"],
        "authorizationEpoch": auth["authorization_epoch"], "skillName": "important-log-summary",
        "skillDigest": digest, "sources": sources, "sourceGaps": [], "priorCumulativeEntryId": None,
        "inputKey": summary_input_key(subject, revision, sources, [], digest), "dbHealth": health,
        "requestedAt": utc()})


def create_cases(root: Path) -> dict:
    layout = DataLayout(root / "data")
    catalog = ImportantLogCatalog(layout.important_logs, create=False)
    life, cases = ActiveSubjectLog(catalog), {}
    ensure_private_dir(layout.workflow)
    flow = HandoffJournal(layout.workflow / "tasks-flow.jsonl")
    try:
        with TaskRepository(layout.tasks) as repo:
            for name in NAMES:
                task = repo.create_task({"description": "소유 합성 명세의 입력·저장 연결", "case": name})
                scope = repo.approve_scope(task, 1, {"operation": "one DB-only synthetic summary"})
                grant = repo.proceed(task, 1, "explicit owned scripted admission")
                run = repo.start_run(task, 1)
                text = canonical_json(repo.get_task_spec(task, 1)["spec"]).decode()
                life.record_flow(flow.append({"type": "delegated", "task_id": task, "revision": 1,
                    "scope_decision_id": scope, "proceed_decision_id": grant, "run_id": run}))
                subject = {"schemaVersion": 1, "kind": "task", "id": task}
                message = str(uuid4())
                life.record_delivery(subject, message_id=message, revision=1, kind="task_spec", content=text,
                                     delivered_at=utc())
                page = ImportantLogReader(layout.important_logs).show(subject)
                entry = next(row["entry"] for row in page["entries"]
                             if row["entry"]["sources"][0]["source"].get("messageId") == message)
                cases[name] = {"request": _request(life, subject, entry["sources"], catalog.health_snapshot()["active"], 1),
                               "text": text, "run_id": run, "scope_id": scope, "grant_id": grant,
                               "source_entry": entry}
    finally:
        flow.close()
    with ImportantLogStore.open(layout.important_logs, catalog.active_uuid(), readonly=True) as store:
        count = store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    return {"cases": cases, "initial_entries": count}


def current(root: Path, request: dict) -> bool:
    fixture = json.loads((root / "fixture.json").read_text())
    match = next((row for row in fixture["caseControls"]["cases"].values()
                  if row["request"]["inputKey"] == request["inputKey"]), None)
    if match is None:
        return False
    expected = {**match["request"], "requestId": request["requestId"]}
    if canonical_json(expected) != canonical_json(request):
        return False
    layout = DataLayout(root / "data")
    with TaskRepository(layout.tasks) as repo:
        task = request["subject"]["id"]
        run = repo.get_current_run(task)
        if run is None or run["run_id"] != match["run_id"] or run["revision"] != request["revision"]:
            return False
        if canonical_json(repo.get_task_spec(task, 1)["spec"]).decode() != match["text"]:
            return False
        for key, kind in (("scope_id", "scope_approved"), ("grant_id", "proceed")):
            decision = repo.get_decision(match[key])
            if (decision["task_id"], decision["revision"], decision["kind"]) != (task, 1, kind):
                return False
    catalog = ImportantLogCatalog(layout.important_logs, create=False)
    return ActiveSubjectLog(catalog).authorize_summary_request(request, **FLAGS).get("allowed") is True


def disposition(root: Path, value: dict) -> None:
    request, outcome = value["request"], value["outcome"]
    with TaskRepository(DataLayout(root / "data").tasks) as repo:
        row = repo.get_summary_request(request["requestId"])
        if row is not None and row["state"] == "sending":
            repo.record_summary_state(request["requestId"], "not_sent" if outcome.get("sent") is False else "unknown",
                                      detail="owned_case_confirmed_not_sent" if outcome.get("sent") is False else "owned_case_not_replayed")


def result_controls(root: Path, completed: dict) -> dict:
    fixture = json.loads((root / "fixture.json").read_text())["caseControls"]
    layout = DataLayout(root / "data")
    catalog = ImportantLogCatalog(layout.important_logs, create=False)
    checks, details = {}, {}
    with TaskRepository(layout.tasks) as repo:
        for name in ("wrong_id", "wrong_revision", "wrong_range"):
            request = fixture["cases"][name]["request"]
            repo.reserve_summary_request(request)
            repo.record_summary_state(request["requestId"], "sending")
            result = {**deepcopy(completed["outcome"]["result"]), **{key: deepcopy(request[key]) for key in
                      ("requestId", "inputKey", "skillDigest", "sources")}}
            if name == "wrong_id":
                result["requestId"] = fixture["cases"]["normal2"]["request"]["requestId"]
            elif name == "wrong_revision":
                result["sources"][0]["source"]["revision"] += 1
            else:
                result["sources"][0]["end"] += 1
            try:
                store_summary(layout.root, request["requestId"], result)
            except ValueError as error:
                checks[name + "_rejected"] = True
                details[name] = type(error).__name__
            else:
                checks[name + "_rejected"] = False
            checks[name + "_receipt_absent"] = repo.get_summary_request(request["requestId"])["receipt"] is None
            repo.record_summary_state(request["requestId"], "unknown", detail="owned_wrong_result_not_replayed")
        primary = repo.get_summary_request(completed["requestId"])
        accepted = repo.record_summary_result_receipt(completed["outcome"]["result"], primary["receipt"]["entry_id"])
        checks["duplicate_receipt_rejected"] = accepted is False
        checks["duplicate_receipt_unchanged"] = repo.get_summary_request(completed["requestId"])["receipt"] == primary["receipt"]
        checks["task_spec_unchanged"] = all(canonical_json(repo.get_task_spec(row["request"]["subject"]["id"], 1)["spec"]).decode() == row["text"]
                                           for row in fixture["cases"].values())
    return {"checks": checks, "error_types": details}


def origin_controls(root: Path, env: dict) -> dict:
    """One finite actual TerminalService command, plus owned Task close/revoke ports."""
    layout = DataLayout(root / "data")
    fixture = json.loads((root / "fixture.json").read_text())["caseControls"]
    catalog = ImportantLogCatalog(layout.important_logs, create=False)
    life, checks = ActiveSubjectLog(catalog), {}
    task_request = fixture["cases"]["normal2"]["request"]
    checks["task_grant_and_ranges"] = life.authorize_summary_request(task_request, **FLAGS)["allowed"]
    checks["pause_refused"] = not life.authorize_summary_request(task_request, **{**FLAGS, "paused": True})["allowed"]
    unrelated = {**task_request, "authorizationRef": fixture["cases"]["normal3"]["grant_id"]}
    checks["unrelated_task_refused"] = not life.authorize_summary_request(unrelated, **FLAGS)["allowed"]
    flow = HandoffJournal(layout.workflow / "tasks-flow.jsonl")
    try:
        with TaskRepository(layout.tasks) as repo:
            repo.complete_run(fixture["cases"]["normal2"]["run_id"])
            closed = flow.append({"type": "task_closed", "task_id": task_request["subject"]["id"], "reason": "done"})
            life.record_flow(closed)
            state = life.state(task_request["subject"])
            closing = {**task_request, "authorizationEpoch": state["event_cursor"]}
            checks["task_closed_not_active"] = not life.authorize_summary_request(task_request, **FLAGS)["allowed"]
            checks["task_close_prior_grant"] = life.authorize_summary_request(closing, **{**FLAGS, "active": False, "closing_event": True})["allowed"]
            checks["task_not_reactivated"] = repo.get_current_run(task_request["subject"]["id"]) is None
            revoked = fixture["cases"]["normal3"]["request"]
            repo.revoke_authority(revoked["subject"]["id"], 1, "owned revocation control")
            life.record_flow(flow.append({"type": "task_closed", "task_id": revoked["subject"]["id"], "reason": "cancelled"}))
            checks["revoked_close_refused"] = not life.authorize_summary_request(revoked,
                **{**FLAGS, "active": False, "closing_event": True, "revoked": True})["allowed"]
            unknown = repo.create_task({"description": "notice only, no delegation"})
            checks["notice_unpaused_not_authority"] = not life.authority({"schemaVersion": 1, "kind": "task", "id": unknown}, **FLAGS)["allowed"]
    finally:
        flow.close()
    pane, handoffs, terminal, pump = None, None, None, None
    stop = threading.Event()
    cleanup = {}
    try:
        env = {**env, "PYTHONPATH": str(root / "runtime/src"), "PYTHONNOUSERSITE": "1"}
        pane = ShellPane(choose_shell(env), env)
        def pumping():
            while not stop.is_set():
                pane.pump()
                stop.wait(.01)
        pump = threading.Thread(target=pumping, name="owned-origin-shell", daemon=True)
        pump.start()
        deadline = time.monotonic() + 5
        while pane.automation_busy() is not None:
            if time.monotonic() >= deadline:
                raise TimeoutError("owned_origin_shell_not_ready")
            time.sleep(.01)
        handoffs = HandoffService(layout.workflow / "handoffs.jsonl")
        terminal = TerminalService(handoffs=handoffs, host_shell=lambda: HostShellPort(pane, lambda: pane),
            gate=HostGate(), log_root=layout.workflow / "terminal", automation=lambda: {
                "portVersion": 2, "kind": "AutomationState", "payload": {
                    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}},
            subject_log=life, repository_factory=lambda: TaskRepository(layout.tasks), wait_seconds=8)
        result = terminal.handle("worker", {"request_id": str(uuid4()), "tool_call_id": "owned-origin-command",
            "session_id": str(uuid4()), "generation": 1, "tool": "terminal",
            "args": {"command": "cd -- " + shlex.quote(str(root / "project")) + "; printf 'owned origin input\\n'"}})
        command = terminal.current()
        if command is None or command.get("running") is not False:
            raise ValueError("owned_origin_command_incomplete")
        checks["actual_command_exited"] = result.get("status") == "exited" and result.get("exit_code") == 0
        subject = {"schemaVersion": 1, "kind": "command", "id": command["command_id"]}
        page = ImportantLogReader(layout.important_logs).show(subject)
        from workbench.backend.summary_scheduler import SummarySourceCollector
        collector = SummarySourceCollector(layout.root, lambda: life)
        ended = handoffs.read_records(("terminal_ended",))[-1]
        end_bytes = source_json(ended)
        event_spans = [span for row in page["entries"] for span in row["entry"]["sources"]
                       if span["source"]["sourceKind"] == "event"]
        checks["terminal_float_source_bytes"] = (type(ended.get("duration_seconds")) is float
            and any(collector._event(span["source"]["eventId"]) == end_bytes
                    and span["digest"] == sha256(end_bytes).hexdigest() and span["end"] == len(end_bytes)
                    for span in event_spans))
        spans = [span for row in page["entries"] for span in row["entry"]["sources"] if span["source"]["sourceKind"] == "terminal"]
        auth = life.authority(subject, **{**FLAGS, "active": False, "closing_event": True})
        if auth["allowed"] is not True or not spans:
            raise ValueError("owned_origin_command_sources_unavailable")
        digest = sha256(skill_bytes()).hexdigest()
        request = {**task_request, "requestId": str(uuid4()), "subject": subject, "revision": None,
            "authorizationRef": auth["authorization_ref"], "authorizationEpoch": auth["authorization_epoch"],
            "sources": spans, "inputKey": summary_input_key(subject, None, spans, [], digest)}
        checks["actual_command_range_and_close"] = life.authorize_summary_request(request,
            **{**FLAGS, "active": False, "closing_event": True})["allowed"]
        checks["command_not_active_after_end"] = not life.authority(subject, **FLAGS)["allowed"]
        life.mark_command_end_summary_processed(command["command_id"])
        checks["command_close_once"] = not life.authority(subject, **{**FLAGS, "active": False, "closing_event": True})["allowed"]
    finally:
        if terminal is not None:
            terminal.close()
        if handoffs is not None:
            handoffs.close()
        stop.set()
        if pump is not None:
            pump.join(2)
        if pane is not None:
            cleanup = pane.close()
    checks["owned_origin_cleanup"] = pane is not None and not cleanup.get("survivors", ["unknown"]) and not pump.is_alive()
    return {"checks": checks, "command_id": subject["id"], "shell_cleanup": cleanup,
            "boundary": "owned public TerminalService command and Task repository/lifecycle ports; not Backend boot"}


if __name__ == "__main__":
    from workbench.backend.summary_compat_runtime import _private_root
    mode, root_text = sys.argv[1:]
    root = Path(root_text)
    _private_root(root)
    if Path(os.environ.get("HOME", "")).resolve() != root / "home":
        raise ValueError("unsafe_owned_case_home")
    value = decode_json(sys.stdin.buffer.read(32769), max_bytes=32768)
    if mode == "--current":
        sys.stdout.write("true" if current(root, parse_summary_request(value)) else "false")
    elif mode == "--disposition":
        disposition(root, value)
    else:
        raise SystemExit(2)
