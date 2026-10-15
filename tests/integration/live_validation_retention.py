"""Explicit owned DVL13 runtime composition; no paid model or user auth input.

The Backend runs only in a private Python child. Runtime assertions use actual
TaskFlow/TerminalService/SummaryScheduler stores and the unchanged product bridge.
The full 300-second and default raw-cap scenarios run only on explicit selection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
from uuid import uuid4

from tests.integration import cw16_harness as h
from tests.integration.live_raw_activation import (consume_product, feature_context,
    prepare_activation, read_json, require_selected, selected_binary, selected_path, write_json)

REPO = Path(__file__).resolve().parents[2]
SUMMARY = {"facts": ["전용 합성 작업의 입력과 실행 결과를 확인했다."],
           "interpretations": [], "unknowns": []}
# Sequential planning bounds, not performance criteria or product/model caps.
RUNTIME_PHASE_SECONDS = {
    "fixture_git": 105, "plan_version": 10, "readiness": 90, "metadata_readback": 30,
    "legacy_terminal": 35, "product_verify_activate": 300, "task_delegation": 35,
    "task_run_start": 40, "task_terminal": 35, "regular_summary": 345,
    "notice_idle": 30, "notice_delivery": 35, "resume_after_origin_pause": 30,
    "task_done_report": 35, "task_close": 40, "task_end_summary": 45,
    "late_command": 35, "late_first_post": 40, "late_receipt": 40, "late_settle": 5,
    "resume_after_late": 30, "raw_terminal": 600, "local_storage_and_reporting": 30,
}
RUNTIME_CLEANUP_SECONDS = 150 + 31 + 25 + 5 + 5
RUNTIME_SUPERVISOR_SECONDS = 2250  # phase2020 + cleanup216 +14s wrapper/scheduling margin


def _private(path: Path) -> None:
    path.mkdir(mode=0o700)
    if path.resolve() != path or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError("owned feature root required")


def _child(work: Path) -> dict:
    """Isolate Core globals, cwd and os.environ from the runner."""
    _private(work)
    directory, invocation, _ = feature_context()
    path, selected = selected_path(work)
    env = h.clean_base_env(PATH=path, HOME=str(work),
        LANG="C.UTF-8", PYTHONPATH=str(REPO / "src") + ":" + str(REPO),
        PYTHONDONTWRITEBYTECODE="1", WB_DVL_OMP=selected["binary_path"],
        WB_DVL_FEATURE_EVIDENCE=str(directory), WB_DVL_INVOCATION_ID=invocation,
        DVL10_INDEPENDENT_OUTPUT=str(work), WB_DVL_PRECISE_TEST_OUTPUT=str(work))
    process = subprocess.Popen([sys.executable, "-m", "tests.integration.live_validation_retention",
        "--owned-child", str(work)], cwd=REPO, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    identity = h.ticks(process.pid)
    if identity is None:
        raise RuntimeError("owned child identity unavailable")
    try:
        process.wait(timeout=RUNTIME_SUPERVISOR_SECONDS)
    finally:
        if h.alive(process.pid, identity):
            h.kill_exact(process.pid, identity)
            process.wait(timeout=10)
    if process.returncode != 0:
        raise RuntimeError("owned runtime child failed")
    require_selected(selected_binary(), selected)
    return read_json(work / "result.json")


def run_retention_runtime(work: Path) -> dict:
    return _child(work)


def run_product_runtime(work: Path) -> dict:
    _private(work)
    return consume_product()


def run_model_metadata(work: Path) -> dict:
    _private(work)
    from workbench.backend.summary_compat_identity import observe_configured
    path, selected = selected_path(work)
    observation = observe_configured(REPO, environment={"PATH": path})
    after = selected_binary()
    binding = observation.get("omp") or {}
    selected_match = after == selected and all(binding.get(key) == value for key, value in selected.items())
    write_json(work / "metadata.json", observation)
    return {"result": "passed" if observation.get("result") == "pass" and selected_match else "inconclusive",
        "selected_binary": selected, "selected_binary_after": after, "selected_binary_binding": selected_match,
        "execution_binding": "Private PATH-first alias and original public getter path/digest; no /proc executable observation.",
        "observation": observation, "boundary": "actual configured getter; distinct from scripted descriptors",
        "io": {"native_internal_io": None},
        "cleanup": {"complete": observation.get("cleanup_complete") is True,
                    "remaining_owned": [observation["retained_private_root"]]
                        if observation.get("retained_private_root") else []}}


def run_q01_recheck(work: Path) -> dict:
    _private(work)
    directory, invocation, candidate = feature_context()
    try:
        origin = read_json(directory / "backend-origin.json")
        producer = read_json(directory / "product-producer.json")
        if any(row.get("invocation") != invocation or row.get("candidate") != candidate
               for row in (origin, producer)):
            raise ValueError("stale owned feature producer")
    except (OSError, ValueError):
        return {"result": "inconclusive", "reason": "actual_backend_origin_producer_unavailable",
                "cleanup": {"complete": True, "remaining_owned": []}}
    from tests.backend.q01_product_adapter_probe.runtime import ProductRuntime
    from tools.q01_recheck import run
    output = directory / "q01"
    _private(output)
    private = output / "private"
    for name in ("home", "project", "data", "socket-dir"):
        (private / name).mkdir(parents=True, mode=0o700)
    runtime = _CompositeRuntime(ProductRuntime(output), origin, directory / "backend-origin.json",
                                producer["record_omp"]["bundled_sha256"], output)
    context = {"isolated_root": str(private), "home": str(private / "home"),
        "project": str(private / "project"), "data": str(private / "data"),
        "socket_dir": str(private / "socket-dir"), "product_runtime": runtime}
    try:
        result = run(root=REPO, requirements=json.loads((REPO / "docs/features/dev-validation-log-retention/requirements-r0.9-p5.json").read_text()),
            adapter="product", context=context, omp_executable=Path(os.environ["WB_DVL_OMP"]),
            omp_bundle_identity=runtime.bundle, output=output / "q01-check.json", log=output / "q01-observations.json",
            projection_output=output / "projection.json", snapshot_output=output / "snapshot.json")
    finally:
        runtime.close()
        remaining = h.processes_mentioning(str(output))
        if not remaining:
            # Keep raw reports/checks at their original evidence paths; discard
            # only the ended owned HOME/config/data trees beneath them.
            for private_root in sorted(output.rglob("private"), key=lambda path: len(path.parts), reverse=True):
                shutil.rmtree(private_root)
            for path in output.rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)
    return {"result": "passed" if result["passed"] else "inconclusive", "q01": result,
        "origin_sha256": sha256((directory / "backend-origin.json").read_bytes()).hexdigest(),
        "delegate_envelope_sha256": sha256((output / "delegate-envelope.json").read_bytes()).hexdigest(),
        "boundary": "Existing SDK controls unchanged; separate actual Backend origin projection. Delegate synthetic origin is retained but not judged.",
        "cleanup": {"complete": runtime.closed and not remaining and not list(output.rglob("private")),
                    "remaining_owned": list(remaining)}}


class _CompositeRuntime:
    """The existing whole-port protocol, with separately attributable origin facts."""
    def __init__(self, delegate, origin, origin_path, bundle, output):
        self.delegate, self.origin, self.origin_path = delegate, origin, origin_path
        self.bundle, self.output, self.closed = bundle, output, False

    def prepare_route(self, context):
        route = self.delegate.prepare_route(context)
        if route["agent_definition_sha256"] != self.bundle:
            raise ValueError("fresh bundled definition differs from actual product producer")
        return {**route, "harness": "dvl13-composite-" + sha256((route["harness"]
            + sha256(Path(__file__).read_bytes()).hexdigest()).encode()).hexdigest()}

    def collect_observations(self):
        raw = self.delegate.collect_observations()
        write_json(self.output / "delegate-envelope.json", raw)
        from tools.q01_recheck import REQUIRED_FACTS
        facts = {name: self.origin["facts"].get(name) for name in REQUIRED_FACTS["Q01-origin"]}
        # Port the frozen product judge label; raw Backend provenance stays in evidence_ref.
        row = {"id": "Q01-origin", "adapter": "product", "source": "product_adapter",
            "evidence_level": "runtime", "evidence_ref": str(self.origin_path), "facts": facts,
            "unknowns": ["actual_backend_origin_unobserved:" + key for key, value in facts.items()
                         if type(value) is not bool]}
        return {"observations": [value for value in raw["observations"] if value["id"] != "Q01-origin"] + [row],
                "unknowns": list(raw["unknowns"])}

    def close(self):
        self.delegate.close()
        self.closed = True


def _runtime(work: Path) -> dict:
    from workbench.backend.launcher import build_plan
    from workbench.backend.omp_home import AGENT_DIR_NAME, omp_root
    from workbench.backend.paths import DataLayout, ensure_private_dir
    from workbench.backend.service import Backend
    from workbench.storage.important_log import ImportantLogReader
    from workbench.tasks.repository import TaskRepository
    ownership = {key: os.environ[key] for key in ("WB_DVL_FEATURE_EVIDENCE", "WB_DVL_INVOCATION_ID", "WB_DVL_OMP")}
    sb = h.Sandbox("d13runtime", omp=ownership["WB_DVL_OMP"], root_parent=work)
    backend = None
    release = threading.Event()
    trace, checks, details = [], {}, {}
    mode = {"hold": False, "fail": False}
    result = {"result": "inconclusive", "evidence_level": "runtime", "checks": checks,
        "details": details, "owned_root": str(sb.root), "operating_model_pass": False}
    details["timing_budget"] = {"phases": RUNTIME_PHASE_SECONDS,
        "phase_seconds": sum(RUNTIME_PHASE_SECONDS.values()), "cleanup_seconds": RUNTIME_CLEANUP_SECONDS,
        "supervisor_seconds": RUNTIME_SUPERVISOR_SECONDS,
        "boundary": "Planning allowances; actual elapsed and timeout outcomes remain separate."}

    def respond(request):
        if request.tool_names != ["db_summary_save"]:
            return None
        inputs = []
        for message in request.body.get("messages", []):
            if message.get("role") == "user":
                for text in h.text_blocks(message.get("content")):
                    try:
                        value = json.loads(text)
                    except ValueError:
                        continue
                    if isinstance(value, dict) and isinstance(value.get("request"), dict):
                        inputs.append(value)
        if len(inputs) != 1:
            raise ValueError("actual summary envelope not uniquely attributed")
        value = inputs[0]
        req = value["request"]
        continuation = request.last_role == "tool"
        trace.append({"requestId": req["requestId"], "subject": req["subject"],
            "authorizationRef": req["authorizationRef"], "authorizationEpoch": req["authorizationEpoch"],
            "sources": req["sources"], "sourceGaps": req["sourceGaps"],
            "inputKey": req["inputKey"], "continuation": continuation,
            "at": time.monotonic(), "body_sha256": sha256(json.dumps(request.body,
                sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "tools": request.tool_names, "model": request.body.get("model")})
        if len(trace) > 64:
            raise ValueError("bounded summary trace exceeded")
        if continuation:
            return h.text("완료")
        if mode["hold"] and not release.wait(20):
            raise TimeoutError("owned summary release unavailable")
        if mode["fail"]:
            return h.error(503, "owned scripted temporary failure")
        return h.tools(("db_summary_save", SUMMARY, str(uuid4())))

    sb.provider.on(lambda request: request.tool_names == ["db_summary_save"], respond)

    def wait(predicate, seconds=30):
        if predicate():
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            backend._tick(.03)
            if predicate():
                return
        raise TimeoutError("owned runtime predicate unavailable")

    def call(function, seconds=35):
        value, errors = [], []
        def invoke():
            try:
                value.append(function())
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=invoke)
        thread.daemon = True
        thread.start()
        wait(lambda: not thread.is_alive(), seconds)
        thread.join()
        if errors:
            raise errors[0]
        return value[0]

    def frame(role, tool, args):
        peer = backend.bridge.peer(role)
        return {"request_id": str(uuid4()), "tool_call_id": str(uuid4()),
            "session_id": peer.session_id, "generation": peer.generation, "tool": tool, "args": args}

    def terminal(command, seconds=35):
        deadline = time.monotonic() + seconds
        response = call(lambda: backend.terminal.handle("worker", frame("worker", "terminal",
            {"command": command})), seconds)
        wait(lambda: backend.terminal.current() is not None
            and backend.terminal.current().get("running") is False,
            max(0, deadline - time.monotonic()))
        current = dict(backend.terminal.current())
        ended = next((row for row in reversed(backend.handoffs.read_records(("terminal_ended",)))
                      if row.get("command_id") == current["command_id"]), None)
        if ended is None or ended.get("status") != "exited" or ended.get("exit_code") != 0:
            raise AssertionError("actual owned terminal command did not exit successfully")
        return {**current, "ended": ended, "initial_response_status": response.get("status")}

    def receipts():
        with TaskRepository(sb.data / "tasks.sqlite3") as journal:
            rows = journal._connection.execute("SELECT request_id FROM summary_requests_v1 ORDER BY request_id").fetchall()
            return [journal.get_summary_request(row[0]) for row in rows]

    def stored(subject):
        return [row for row in receipts() if row["request"]["subject"] == subject and row["state"] == "stored"]

    try:
        result["stage"] = "private_models_and_backend_readiness"
        models = sb.provider.models_yml().replace("        maxTokens: 1024\n",
            "        maxTokens: 1024\n        reasoning: true\n")
        models += ("  openai-codex:\n    baseUrl: http://127.0.0.1:" + str(sb.provider.port)
            + "/v1\n    api: openai-completions\n    auth: none\n    models:\n"
              "      - id: gpt-6-luna\n        name: DVL13 controlled child\n"
              "        contextWindow: 131072\n        maxTokens: 16384\n        reasoning: true\n")
        (omp_root(sb.data) / AGENT_DIR_NAME / "models.yml").write_text(models)
        os.chdir(sb.project)
        os.environ.clear()
        os.environ.update(sb.env)
        # Forward only harness ownership keys after clearing the isolated child.
        for key, value in ownership.items():
            os.environ[key] = value
        plan = build_plan(sb.env, omp=sb.omp, omp_args=("--model", sb.provider.model))
        ensure_private_dir(sb.data)
        backend = Backend(DataLayout(sb.data), plan, project_dir=str(sb.project), environment=sb.env)
        backend._lock.acquire()
        backend._reconcile_startup()
        backend._open()
        wait(lambda: backend.phase == "ready" and backend._isolation_thread is not None
            and not backend._isolation_thread.is_alive(), 90)
        runtime = backend._summary_runtime()
        expected = f"http://127.0.0.1:{sb.provider.port}/v1"
        checks["actual_loopback_readback"] = (isinstance(runtime["metadata"]["thinkingLevel"], str)
            and runtime["metadata"]["childSelection"]["model"]["baseUrl"] == expected
            and runtime["metadata"]["childSelection"]["model"]["api"] == "openai-completions"
            and runtime["metadata"]["childSelection"]["selector"] == "openai-codex/gpt-6-luna:max")
        if not checks["actual_loopback_readback"]:
            raise RuntimeError("controlled loopback metadata unavailable before summary")
        checks["default_backend_fixed_writer"] = runtime["writer"] == {"python": sys.executable,
            "moduleRoot": str(Path(__file__).resolve().parents[2] / "src"), "dataRoot": str(sb.data)}
        details["startup_provider_counts"] = sb.provider.snapshot()["requests"]
        result["stage"] = "legacy_source_and_product_activation"
        legacy = terminal("printf 'owned legacy source\\n'")
        legacy_path = sb.data / "workflow/terminal" / (legacy["command_id"] + ".log")
        legacy_sha = sha256(legacy_path.read_bytes()).hexdigest()
        checks["default_legacy_source"] = backend._new_raw_port() is None and not (sb.data / "raw-rotation.json").exists()
        activation = work / "activation"
        activation.mkdir(mode=0o700)
        details["product"] = call(lambda: prepare_activation(activation, sb.data),
                                  RUNTIME_PHASE_SECONDS["product_verify_activate"])
        if details["product"].get("result") != "passed":
            raise RuntimeError("actual product verification or activation incomplete")
        checks["actual_owned_cli_activation"] = details["product"]["activation"]["warning_observed"]
        checks["legacy_source_unchanged"] = sha256(legacy_path.read_bytes()).hexdigest() == legacy_sha

        result["stage"] = "actual_task_and_real_300_seconds"
        start = time.monotonic()
        task_result = call(lambda: backend.handoffs.handle("manager", frame("manager", "to_worker", {
            "task_id": None, "kind": "work", "message": "전용 합성 검증 작업을 시작하고 보고를 기다린다.",
            "spec": {"goal": "합성 작업 명세·실행·요약 연결", "paths": ["README"],
                     "instructions": "작업 입력만 확인하고 완료 보고는 host가 명시할 때까지 보류한다.", "execution": None},
            "analysis": None, "commands": None, "run": None, "cancel": None})))
        if task_result.get("status") != "dispatched":
            raise AssertionError("actual TaskFlow delegation failed")
        task = task_result["task_id"]
        subject = {"schemaVersion": 1, "kind": "task", "id": task}
        wait(lambda: backend.flow.active_task() is not None and backend.flow.active_task().run_id is not None, 40)
        with TaskRepository(sb.data / "tasks.sqlite3") as journal:
            original_spec = journal.get_task_spec(task, 1)
            decisions = journal.get_decisions(task, 1)
        checks["actual_task_approval"] = {row["kind"] for row in decisions} >= {"scope_approved", "proceed"}
        task_command = terminal("printf 'owned task command input\\n'")
        checks["task_command_bound"] = any(row.get("command_id") == task_command["command_id"]
            and row.get("task_id") == task for row in backend.handoffs.read_records(("terminal_started",)))
        wait(lambda: bool(stored(subject)), 345)
        first = stored(subject)[0]
        first_post = next(row for row in trace if row["requestId"] == first["request"]["requestId"])
        elapsed = first_post["at"] - start
        checks["real_300_seconds"] = elapsed >= 300
        details["summary_interval_seconds"] = elapsed
        checks["task_source_unchanged"] = original_spec == backend.repository.get_task_spec(task, 1)
        first_entry = first["receipt"]["entry_id"]
        page = ImportantLogReader(backend.layout.important_logs).show(subject)
        checks["readonly_query_receipt"] = any(row["entry"]["entryId"] == first_entry
            and row["entry"]["sources"] == first["request"]["sources"] for row in page["entries"])
        flags = {**backend._summary_authority_flags(subject, None), "paused": False, "has_new_input": True}
        authorized = backend.subject_log.authorize_summary_request(first["request"], **flags)
        with TaskRepository(sb.data / "tasks.sqlite3") as journal:
            other = journal.create_task({"description": "unrelated owned negative scope"})
            journal.approve_scope(other, 1, {"operation": "owned unrelated scope control"})
            other_grant = journal.proceed(other, 1, "owned unrelated negative control")
        unrelated = backend.subject_log.authorize_summary_request(
            {**first["request"], "authorizationRef": other_grant}, **flags)
        notice_subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        def notice_scope():
            from workbench.storage.important_log import ImportantLogStore
            snapshot = backend.important_catalog.catalog_snapshot()
            subject_rows = 0
            for identity in (snapshot["active"], *snapshot["archives"]):
                with ImportantLogStore.open(backend.layout.important_logs, identity, readonly=True) as store:
                    subject_rows += store._connection.execute("SELECT COUNT(*) FROM subject_lifecycle_v1 "
                        "WHERE subject_kind='command' AND subject_id=?", (notice_subject["id"],)).fetchone()[0]
            with TaskRepository(sb.data / "tasks.sqlite3") as journal:
                request_rows = journal._connection.execute("SELECT COUNT(*) FROM summary_requests_v1 "
                    "WHERE subject_kind='command' AND subject_id=?", (notice_subject["id"],)).fetchone()[0]
            return {"authority": backend.subject_log.authority(notice_subject,
                        active=True, paused=False, revoked=False, has_new_input=True),
                "subject_rows": subject_rows, "request_rows": request_rows,
                "start_rows": sum(row.get("command_id") == notice_subject["id"]
                    for row in backend.handoffs.read_records(("terminal_started",)))}
        notice_before = notice_scope()
        wait(lambda: backend._omp_idle("worker") is True, RUNTIME_PHASE_SECONDS["notice_idle"])
        notice_payload = {"type": "status_check", "notice_id": str(uuid4()),
            "command_id": notice_subject["id"], "message": "자료 알림이며 실행 위임이 아니다."}
        notice_status = call(lambda: backend._notice("worker", notice_payload),
            RUNTIME_PHASE_SECONDS["notice_delivery"])
        notice_after = notice_scope()
        notice_authority = notice_after["authority"]
        invalid_role = backend.handoffs.handle("user", frame("manager", "to_worker", {}))
        backend.pause_hook()
        paused_authority = backend.subject_log.authorize_summary_request(first["request"],
            **{**flags, "paused": backend._automation_paused()})
        backend.resume_hook()
        wait(lambda: not backend._automation_paused(), 30)
        details["origin_task"] = {"request": first["request"], "receipt": first["receipt"],
            "decisions": decisions, "authorization": authorized, "unrelated": unrelated,
            "notice_status": notice_status, "notice_authority": notice_authority,
            "notice_payload": notice_payload, "notice_scope_before": notice_before,
            "notice_scope_after": notice_after, "notice_raw_ack": None,
            "notice_raw_ack_limit": "Backend._notice exposes mapped disposition, not raw ACK; no retransmission.",
            "notice_provider_response": "Existing non-summary ScriptedProvider default text responder; summary trace requires sole db_summary_save roster.",
            "user_role_response": invalid_role, "paused_authority": paused_authority}

        result["stage"] = "task_end_and_valid_late_pause"
        # A real accepted done report closes the Task and makes end work due.
        call(lambda: backend.handoffs.handle("worker", frame("worker", "to_manager", {
            "kind": "done", "message": "전용 합성 작업의 실행과 검증을 마쳤다.", "task_id": task,
            "in_reply_to": None, "requires_code_change": None, "reason": None, "request": None})))
        wait(lambda: backend.flow.active_task() is None, 40)
        wait(lambda: len(stored(subject)) >= 2, 45)
        checks["actual_task_end_summary"] = backend.subject_log.state(subject).get("core_closed_reason") == "done"

        # Hold one actual SDK summary while the user pause is published.
        mode["hold"] = True
        prior_posts = len(trace)
        command = terminal("printf 'owned command late result\\n'")
        command_subject = {"schemaVersion": 1, "kind": "command", "id": command["command_id"]}
        wait(lambda: len(trace) > prior_posts and any(row["subject"] == command_subject for row in trace), 40)
        backend.pause_hook()
        checks["actual_pause"] = backend._automation_paused() is True
        release.set()
        wait(lambda: bool(stored(command_subject)), 40)
        before_pause_posts = len(trace)
        end = time.monotonic() + 1
        wait(lambda: time.monotonic() >= end, 5)
        checks["late_valid_saved_without_resume"] = backend._automation_paused() is True and len(trace) == before_pause_posts
        checks["command_closed_once"] = not backend.subject_log.authority(command_subject,
            active=False, paused=False, revoked=False, has_new_input=True, closing_event=True)["allowed"]
        backend.resume_hook()
        wait(lambda: not backend._automation_paused(), 30)
        mode["hold"] = False
        unique = {row["requestId"] for row in trace if not row["continuation"]}
        checks["summary_no_replay"] = len(unique) == len([row for row in trace if not row["continuation"]])
        checks["db_only_tool_roster"] = all(row["tools"] == ["db_summary_save"] for row in trace)
        command_record = stored(command_subject)[0]
        from workbench.tasks.repository import InvalidTransitionError
        with TaskRepository(sb.data / "tasks.sqlite3") as journal:
            try:
                journal.reserve_summary_request({**command_record["request"], "requestId": str(uuid4())})
            except InvalidTransitionError:
                replay_refused = True
            else:
                replay_refused = False
            journal.revoke_authority(task, 1, "owned explicit revocation control after done")
        task_closed = stored(subject)[-1]
        revoked = backend.subject_log.authorize_summary_request(task_closed["request"],
            **backend._summary_authority_flags(subject, {}), paused=False,
            has_new_input=True, closing_event=True)
        command_page = ImportantLogReader(backend.layout.important_logs).show(command_subject)
        command_sources_match = any(row["entry"]["entryId"] == command_record["receipt"]["entry_id"]
            and row["entry"]["sources"] == command_record["request"]["sources"] for row in command_page["entries"])
        origin_facts = {
            "task_grant_bound": authorized["allowed"] is True and any(row["kind"] == "proceed"
                and row["decision_id"] == first["request"]["authorizationRef"] for row in decisions),
            "command_start_bound": any(row.get("command_id") == command["command_id"]
                and row.get("task_id") is None for row in backend.handoffs.read_records(("terminal_started",))),
            "work_source_ranges_bound": checks["readonly_query_receipt"] and command_sources_match,
            "terminal_close_bound": command["ended"]["status"] == "exited" and command_record["state"] == "stored",
            "unrelated_scope_rejected": unrelated["allowed"] is False,
            "notice_not_authority": (notice_before == notice_after and notice_authority["allowed"] is False
                and all(notice_after[key] == 0 for key in ("subject_rows", "start_rows", "request_rows")))
                if notice_status == "delivered" else None,
            "user_role_not_authority": invalid_role.get("status") == "rejected",
            "pause_revoke_preserved": paused_authority["allowed"] is False and revoked["allowed"] is False,
            "end_once_no_replay": checks["command_closed_once"] and replay_refused and checks["summary_no_replay"]}
        directory, invocation, candidate = feature_context()
        write_json(directory / "backend-origin.json", {"invocation": invocation, "candidate": candidate,
            "facts": origin_facts, "task": details["origin_task"], "command": command,
            "command_request": command_record["request"], "command_receipt": command_record["receipt"],
            "revoked_authorization": revoked, "replay_reserve_refused": replay_refused,
            "source": "actual Backend TaskFlow/TerminalService/SummaryScheduler/fixed-writer trace"})

        # Keep the small catalog fixture independent of the bulk raw span backlog.
        result["stage"] = "owned_catalog_storage_fixture"
        details["storage"] = _storage_controls(backend, subject)
        checks.update(details["storage"]["checks"])
        # The product default cap is exercised once by the final feature run.
        result["stage"] = "actual_default_raw_eviction_and_storage"
        chunk_count = 505
        raw_command = "python3 -c " + shlex.quote("import os; p=b'DVL13-raw\\n'*104857; [os.write(1,p) for _ in range(" + str(chunk_count) + ")]")
        raw = terminal(raw_command, RUNTIME_PHASE_SECONDS["raw_terminal"])
        from workbench.storage.log_raw.port import raw_query
        read = raw_query(sb.data, "command", raw["command_id"], offset=0, limit=64,
                         segmented=backend.segmented_raw_port)
        checks["actual_default_raw_eviction"] = (read.get("log_format") == "segmented-v1"
            and any(gap.get("reason") == "evicted" for gap in read["read"].get("missing", [])))
        checks["legacy_after_eviction_unchanged"] = sha256(legacy_path.read_bytes()).hexdigest() == legacy_sha
        details["raw_eviction"] = {"command_id": raw["command_id"], "read": read,
            "default_segment_bytes": 100_000_000, "default_segment_count": 5}
        details["summary_trace"] = trace
        details["provider_counts_final"] = sb.provider.snapshot()["requests"]
        result["result"] = "passed" if checks and all(value is True for value in checks.values()) else "failed"
    except AssertionError as error:
        result.update(result="failed", error_type=type(error).__name__, error_reason=str(error)[:256])
    except Exception as error:
        result.update(result="inconclusive", error_type=type(error).__name__)
        if (isinstance(error, TimeoutError)
                and result.get("stage") == "actual_task_and_real_300_seconds"):
            def bounded_text(value, limit):
                if not isinstance(value, str):
                    return "unknown"
                return value.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")

            diagnostic = {
                "summary_scheduler": {"state": "unknown", "reason": "unknown", "inFlight": "unknown"},
                "summary_request_sample": "unknown",
                "request_order": "unknown",
                "summary_responder_attributed_count": len(trace),
                "provider_request_counters": "unknown",
            }
            try:
                scheduler = getattr(backend, "summary_scheduler", None)
                status = scheduler.status() if scheduler is not None else None
                if isinstance(status, dict):
                    diagnostic["summary_scheduler"] = {
                        "state": bounded_text(status.get("state"), 32),
                        "reason": (None if "reason" in status and status.get("reason") is None else
                                   bounded_text(status.get("reason"), 96)),
                        "inFlight": status.get("inFlight") if type(status.get("inFlight")) is bool else "unknown",
                    }
            except BaseException:
                pass
            try:
                trace_ids = {row.get("requestId") for row in trace if isinstance(row, dict)
                             and isinstance(row.get("requestId"), str)}
                rows = receipts()
                diagnostic["summary_request_sample"] = [{
                    "requestId": bounded_text((row.get("request") or {}).get("requestId"), 64),
                    "state": bounded_text(row.get("state"), 32),
                    "last_detail_or_defercode": "unavailable",
                    "attribution": ("summary_responder" if
                                    (row.get("request") or {}).get("requestId") in trace_ids else "unknown"),
                } for row in rows[-8:] if isinstance(row, dict)]
                diagnostic["request_order"] = "unknown"
            except BaseException:
                pass
            try:
                counts = sb.provider.snapshot()["requests"]
                if isinstance(counts, dict):
                    diagnostic["provider_request_counters"] = {
                        bounded_text(key, 32): value
                        for key, value in sorted(counts.items(), key=lambda item: bounded_text(item[0], 32))[:8]
                        if isinstance(key, str) and type(value) is int and 0 <= value <= 2**31 - 1
                    }
            except BaseException:
                pass
            try:
                encoded_diagnostic = json.dumps(diagnostic, ensure_ascii=False,
                                                separators=(",", ":")).encode("utf-8", "replace")
                if len(encoded_diagnostic) > 4096:
                    sample = diagnostic["summary_request_sample"]
                    if isinstance(sample, list):
                        diagnostic["summary_request_sample"] = sample[:4]
                        for row in diagnostic["summary_request_sample"]:
                            row["last_detail_or_defercode"] = bounded_text(row["last_detail_or_defercode"], 16)
                    encoded_diagnostic = json.dumps(diagnostic, ensure_ascii=False,
                                                    separators=(",", ":")).encode("utf-8", "replace")
                if len(encoded_diagnostic) > 4096:
                    diagnostic = {"summary_scheduler": {"state": "unknown", "reason": "unknown",
                                                         "inFlight": "unknown"},
                                  "summary_request_sample": "unknown",
                                  "request_order": "unknown",
                                  "summary_responder_attributed_count": len(trace),
                                  "provider_request_counters": "unknown"}
            except BaseException:
                diagnostic = {"summary_scheduler": {"state": "unknown", "reason": "unknown",
                                                     "inFlight": "unknown"},
                              "summary_request_sample": "unknown",
                              "request_order": "unknown",
                              "summary_responder_attributed_count": len(trace),
                              "provider_request_counters": "unknown"}
            details["actual_task_timeout_diagnostic"] = diagnostic
    finally:
        release.set()
        cleanup = {}
        try:
            cleanup = backend._close() if backend is not None else {}
        except Exception as error:
            result["cleanup_error_type"] = type(error).__name__
        finally:
            if backend is not None:
                backend._lock.release()
        result["backend_cleanup"] = cleanup
        sandbox_cleanup = sb.close()
        result["sandbox_cleanup"] = sandbox_cleanup
        result["cleanup"] = {"complete": cleanup.get("verified") is True
            and sandbox_cleanup.get("root_removed") is True
            and not sandbox_cleanup.get("residue_before_fallback")
            and not sandbox_cleanup.get("residue_after_fallback"), "remaining_owned": cleanup.get("left_running", [])}
        if not result["cleanup"]["complete"]:
            result["result"] = "inconclusive"
    return result


def _storage_controls(backend, subject) -> dict:
    """Actual private catalog with small DB targets; simulated age stays explicit."""
    import fcntl
    from workbench.contracts.logs_v1 import canonical_json
    from workbench.storage.important_log import ImportantLogReader, ImportantLogStore
    from workbench.storage.important_log.maintenance import MAX_DELETE_ROWS
    catalog = backend.important_catalog
    checks, details = {}, {}
    original_targets = catalog.target_bytes, catalog.low_water_bytes
    try:
        catalog.target_bytes, catalog.low_water_bytes = 48_000, 24_000
        active_before = catalog.active_uuid()
        def counts():
            with ImportantLogStore.open(catalog.directory, active_before, readonly=True) as store:
                health = store.health_snapshot()
            if health["stale"]:
                raise AssertionError("owned storage fixture entry counts unavailable")
            return {"entry_count": sum(row["entryCount"] for row in health["subjectStats"]),
                "task_entry_count": sum(row["entryCount"] for row in health["subjectStats"]
                    if row["subject"] == subject), "logical_live_bytes": health["logicalLiveBytes"]}
        before = counts()
        pressure_subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        appends = []
        for _ in range(32):
            written = catalog.append(pressure_subject, "event", [], {"schemaVersion": 1,
                "facts": ["전용 보관 압력 입력 " * 80], "interpretations": [], "unknowns": []})
            if written.get("state") != "ok" or written.get("entry") is None:
                raise AssertionError("owned storage pressure append failed: " + str(written.get("state")))
            appends.append({"state": written["state"], "prune": written["prune"],
                "rollover": written["rollover"], "entry_bytes": len(canonical_json(written["entry"]))})
            if written["rollover"] is not None:
                break
        details["pressure_fixture"] = {"subject": pressure_subject, "evidence_level": "fixture",
            "boundary": "Event pressure only; no lifecycle/start/Task authority or origin evidence.",
            "before": before, "after_original_db": counts(), "append_bound": 32,
            "max_delete_rows_per_pass": MAX_DELETE_ROWS, "appends": appends}
        if appends[-1]["rollover"] is None:
            raise AssertionError("owned catalog no rollover after32: entries=" + str(before["entry_count"])
                + " maxDeleteRows=" + str(MAX_DELETE_ROWS) + " pruneRemaining=" + str(appends[-1]["prune"]["remaining"]))
        reader = ImportantLogReader(catalog.directory)
        status = reader.status()
        checks["actual_db_rollover_warning"] = status["complete"] and status["databaseCount"] >= 2 and status["cleanupNeeded"]
        snapshot = catalog.catalog_snapshot()
        old = appends[-1]["rollover"]["from"]
        if old not in snapshot.get("archives", []):
            raise AssertionError("owned rollover source is not archived")
        old_path = catalog.directory / (old + ".sqlite3")
        old_hash = sha256(old_path.read_bytes()).hexdigest()
        page = reader.show(pressure_subject)
        checks["archive_readonly_hash"] = page["complete"] and sha256(old_path.read_bytes()).hexdigest() == old_hash
        try:
            catalog.prepare_and_delete_archive(catalog.active_uuid())
        except (ValueError, RuntimeError):
            checks["active_db_delete_refused"] = True
        else:
            checks["active_db_delete_refused"] = False
        details["delete"] = backend.important_delete_db(old)
        checks["explicit_archive_delete_recount"] = not old_path.exists() and reader.status()["databaseCount"] == status["databaseCount"] - 1
        # Public maintenance accepts an explicit observation time; not proof of expired deletion.
        details["retention"] = catalog.maintenance_step(now=datetime.now(timezone.utc) + timedelta(days=121))
        if details["retention"].get("state") != "ok":
            raise AssertionError("owned storage maintenance failed: " + str(details["retention"].get("state")))
        details["retention_evidence_level"] = "fixture"
        lock = os.open(catalog.directory / "maintenance.lock", os.O_RDWR)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            failed = catalog.append(pressure_subject, "event", [], {"schemaVersion": 1,
                "facts": ["owned lock contention"], "interpretations": [], "unknowns": []})
            checks["storage_failure_explicit"] = failed.get("state") == "busy" and failed.get("entry") is None
            details["contention_state"] = failed.get("state")
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
        details["recovery"] = catalog.recovery_probe(force=True)
        if details["recovery"].get("state") != "healthy":
            raise AssertionError("owned storage recovery failed: " + str(details["recovery"].get("state")))
        checks["storage_recovery_without_resume"] = backend._automation_paused() is False and catalog.active_uuid() is not None
        details["status"] = reader.status()
        details["checks"] = checks
    finally:
        catalog.target_bytes, catalog.low_water_bytes = original_targets
    details["targets_restored"] = [catalog.target_bytes, catalog.low_water_bytes]
    return details


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--owned-child":
        raise SystemExit("explicit owned child invocation required")
    owned = Path(sys.argv[2])
    observation = _runtime(owned)
    write_json(owned / "result.json", observation)
