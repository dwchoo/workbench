"""Private installed-OMP bundled Task and typed handoff discriminator."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4

from .full_probe import _request
from workbench.contracts.logs_v1 import canonical_json, parse_summary_result
from workbench.tasks.repository import InvalidTransitionError, TaskRepository

HERE = Path(__file__).resolve().parent
WRITER = HERE.parent / "q01_script_only_probe/fixed_writer.py"
SKILL = HERE.parent / "q01_sdk_private_probe/summary-skill.txt"
OLD_BODY = HERE.parent / "q01_sdk_private_probe/agent-body.txt"
SUMMARY = "Literal command and SQL: touch /tmp/NO_EXECUTE; DROP TABLE summaries; --"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(x.get("text", "")) for x in value if isinstance(x, dict) and x.get("type") == "text")
    return ""


class Provider(ThreadingHTTPServer):
    def __init__(self, private: Path, requests: dict):
        self.private = private
        self.requests = requests
        self.posts: list[dict] = []
        self.outbound: list[dict] = []
        self.violation: str | None = None
        self.lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(inner) -> None:
                raw = inner.rfile.read(int(inner.headers.get("Content-Length", "0")))
                try:
                    request = json.loads(raw)
                except ValueError:
                    request = {}
                messages = request.get("messages") or []
                users = [m for m in messages if m.get("role") == "user"]
                prompt = as_text(users[-1].get("content")) if users else ""
                case = next((name for name in ("positive", "saved_then_bash", "worker_role")
                             if "q01-contract:" + name in prompt), "unknown")
                tools = [x.get("function", {}).get("name") for x in request.get("tools") or []]
                expected_path = private / "bundle-body.txt"
                expected_body = expected_path.read_text() if expected_path.exists() else ""
                system = [as_text(m.get("content")) for m in messages if m.get("role") == "system"]
                exact = bool(expected_body) and system == [expected_body + "\n\n" + SKILL.read_text()]
                permit_path = private / ("permit-" + case)
                permit = (case in self.requests and permit_path.exists() and
                          permit_path.read_text() == self.requests[case]["requestId"])
                record = {"case": case, "path": inner.path, "tools": tools,
                          "tools_present": "tools" in request, "permit": permit, "system_exact": exact,
                          "system_sha": sha256("\n".join(system).encode()).hexdigest(),
                          "body_sha": sha256(raw).hexdigest(), "model": request.get("model"),
                          "max_completion_tokens": request.get("max_completion_tokens"),
                          "roles": [m.get("role") for m in messages]}
                with self.lock:
                    self.posts.append(record)
                    if not permit or not exact or tools != ["db_summary_save"] or case == "worker_role":
                        self.violation = "forbidden_post:" + case
                    violation = self.violation
                if violation:
                    inner.send_response(503)
                    inner.end_headers()
                    return
                tool_count = sum(m.get("role") == "tool" for m in messages)
                name = ("db_summary_save" if tool_count == 0 else
                        "bash" if case == "saved_then_bash" and tool_count == 1 else None)
                call = None
                if name:
                    args = ({"summary": SUMMARY} if name == "db_summary_save" else
                            {"command": f"printf CHANGED > {private / 'cwd/command-sentinel'}"})
                    call = {"id": f"contract-{case}-{tool_count}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}
                delta = ({"role": "assistant", "tool_calls": [{"index": 0, **call}]} if call else
                         {"role": "assistant", "content": '{"status":"done"}'})
                finish = "tool_calls" if call else "stop"
                frames = [
                    {"id": "contract", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"id": "contract", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                ]
                body = b"".join(f"data: {json.dumps(f)}\n\n".encode() for f in frames) + b"data: [DONE]\n\n"
                with self.lock:
                    self.outbound.append({"case": case, "call": call, "body_sha": sha256(body).hexdigest()})
                try:
                    inner.send_response(200)
                    inner.send_header("Content-Type", "text/event-stream")
                    inner.send_header("Content-Length", str(len(body)))
                    inner.end_headers()
                    inner.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(inner, *_args: object) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)


def _backend_controls(private: Path, requests: dict) -> dict:
    path = private / "typed-positive.json"
    if not path.exists():
        return {"actual_typed_result_missing": True, "checks": {"actual_typed_result": False}}
    raw = path.read_bytes()
    parsed = parse_summary_result(json.loads(raw))
    expected = requests["positive"]
    binding = (parsed["requestId"] == expected["requestId"] and
               parsed["inputKey"] == expected["inputKey"] and
               parsed["skillDigest"] == expected["skillDigest"] and
               parsed["sources"] == expected["sources"] and
               parsed["facts"] == [SUMMARY])
    repo = TaskRepository(private / "backend.sqlite3")
    outcomes: dict[str, bool] = {}
    try:
        span = parsed["sources"][0]
        changed = {
            "wrong_id": {**parsed, "requestId": str(uuid4())},
            "wrong_revision": {**parsed, "sources": [{**span, "source": {**span["source"], "revision": 2}}]},
            "wrong_range": {**parsed, "sources": [{**span, "end": span["end"] + 1}]},
        }
        for name, value in changed.items():
            try:
                repo.record_summary_result_receipt(value, str(uuid4()))
            except (InvalidTransitionError, ValueError):
                outcomes[name + "_rejected"] = True
            else:
                outcomes[name + "_rejected"] = False
        outcomes["actual_accepted"] = repo.record_summary_result_receipt(parsed, str(uuid4()))
        outcomes["duplicate_rejected"] = repo.record_summary_result_receipt(parsed, str(uuid4())) is False
    finally:
        repo.close()
    outcomes["exact_actual_binding"] = binding
    outcomes["result_utf8_bounded"] = len(raw) <= 8192 and len(canonical_json(parsed)) <= 8192
    return {"source": "exact OMP child typed-positive.json consumed by existing TaskRepository",
            "actual_result_sha": sha256(raw).hexdigest(), "actual_result_bytes": len(raw),
            "result": parsed, "checks": outcomes}


def run(out: Path) -> dict:
    out = out.resolve()
    if out.exists():
        raise ValueError("refuse to overwrite contract evidence")
    out.mkdir(parents=True, mode=0o700)
    private = out / "private"
    for name in ("home", "agent", "config", "cwd"):
        (private / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("command-sentinel", "file-sentinel"):
        (private / "cwd" / name).write_text("ORIGINAL")
    writer = private / "fixed_writer.py"
    shutil.copyfile(WRITER, writer)
    repo = TaskRepository(private / "backend.sqlite3")
    requests: dict = {}
    try:
        for case in ("positive", "saved_then_bash"):
            request, _ = _request(repo, SUMMARY, digest(SKILL))
            repo.reserve_summary_request(request)
            repo.record_summary_state(request["requestId"], "sending")
            requests[case] = request
    finally:
        repo.close()
    (private / "requests.json").write_text(json.dumps(requests, ensure_ascii=False) + "\n")
    (private / "role-inputs.json").write_text(json.dumps({
        "positive": {"role": "Manager", "agentName": "task"},
        "saved_then_bash": {"role": "Manager", "agentName": "task"},
        "worker_role": {"role": "Worker", "agentName": "task"},
    }, ensure_ascii=False) + "\n")
    provider = Provider(private, requests)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    try:
        thread.start()
        result = _run_owned(out, private, provider, writer, requests)
    finally:
        if thread.is_alive():
            provider.shutdown()
        provider.server_close()
        if thread.ident is not None:
            thread.join(2)
    (out / "contract-evidence.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def _run_owned(out: Path, private: Path, provider: Provider, writer: Path, requests: dict) -> dict:
    (private / "agent/models.yml").write_text(
        "providers:\n  q01contract:\n" f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
        "    api: openai-completions\n    auth: none\n    models:\n"
        "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 8192\n")
    (private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
    omp_name = shutil.which("omp")
    if omp_name is None:
        raise RuntimeError("installed OMP not found")
    omp = Path(omp_name).resolve()
    proxy = "http://127.0.0.1:9"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(private / "home"),
           "TERM": "dumb", "LANG": "C.UTF-8", "PI_CODING_AGENT_DIR": str(private / "agent"),
           "PI_CONFIG_DIR": str(private / "config"), "XDG_CONFIG_HOME": str(private / "xdg-config"),
           "XDG_DATA_HOME": str(private / "xdg-data"), "XDG_STATE_HOME": str(private / "xdg-state"),
           "XDG_CACHE_HOME": str(private / "xdg-cache"), "Q01_CONTRACT_TRACE": str(private / "trace.jsonl"),
           "Q01_CONTRACT_PRIVATE": str(private), "Q01_FIXED_WRITER": str(writer),
           "Q01_FIXED_PYTHON": sys.executable, "Q01_SUMMARY_SKILL": str(SKILL),
           "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    env.update({key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")})
    argv = [str(omp), "--mode", "rpc", "--no-session", "--no-title", "--no-extensions", "--no-skills",
            "--no-rules", "--no-tools", "--no-pty", "--config", str(private / "overlay.yml"),
            "--extension", str(HERE / "contract_extension.ts"), "--model", "q01contract/scripted",
            "--cwd", str(private / "cwd")]
    version = subprocess.run([str(omp), "--version"], env=env, capture_output=True, text=True, timeout=10)
    result: dict = {"invocation": "q01-sdk-contracts01", "omp_version": version.stdout.strip(),
                    "omp_binary_sha": digest(omp), "argv": argv, "environment_keys": sorted(env),
                    "source_sha": {str(p): digest(p) for p in
                    (HERE / "contract_extension.ts", HERE / "role_admission.ts", HERE / "run_contract.py", HERE / "full_probe.py",
                     SKILL, OLD_BODY, WRITER)}, "provider_port": provider.server_port}
    frames: list[dict] = []
    condition = threading.Condition()
    trace = private / "trace.jsonl"
    with (out / "omp-stderr.log").open("wb") as stderr:
        proc = subprocess.Popen(argv, cwd=private / "cwd", env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
        result["process"] = {"pid": proc.pid}

        def pump() -> None:
            pending = b""
            while True:
                ready, _, _ = select.select([proc.stdout.fileno()], [], [], 0.2)
                if not ready:
                    if proc.poll() is not None:
                        return
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    return
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        continue
                    with condition:
                        frames.append(frame)
                        condition.notify_all()

        pump_thread = threading.Thread(target=pump, daemon=True)
        pump_started = False
        try:
            pump_thread.start()
            pump_started = True
            command = {"id": "contract-parent-before", "type": "get_state"}
            proc.stdin.write((json.dumps(command) + "\n").encode())
            proc.stdin.flush()
            deadline = time.monotonic() + 70
            completed = False
            while time.monotonic() < deadline and proc.poll() is None:
                if trace.exists() and '"event":"done"' in trace.read_text():
                    completed = True
                    break
                if trace.exists() and '"event":"fatal"' in trace.read_text():
                    break
                if provider.violation:
                    break
                with condition:
                    condition.wait(0.2)
            if completed and proc.poll() is None:
                command = {"id": "contract-parent-after", "type": "get_state"}
                proc.stdin.write((json.dumps(command) + "\n").encode())
                proc.stdin.flush()
                with condition:
                    condition.wait_for(lambda: any(f.get("id") == "contract-parent-after" for f in frames), 3)
            result["stop_reason"] = provider.violation or (None if completed else "timeout_or_parent_exit")
            result["trace"] = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
            result["rpc_frames"] = [{"id": f.get("id"), "type": f.get("type"), "error": f.get("error")}
                                    for f in frames[:50]]
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(5)
            if pump_started:
                pump_thread.join(2)
            result["process"] = {"pid": proc.pid, "returncode": proc.returncode,
                                 "pump_stopped": not pump_started or not pump_thread.is_alive(),
                                 "remaining_owned": [] if proc.poll() is not None else [proc.pid]}
    result["provider_posts"] = provider.posts
    result["provider_outbound"] = provider.outbound
    result["requests"] = requests
    result["role_inputs"] = json.loads((private / "role-inputs.json").read_text())
    result["role_inputs_sha"] = digest(private / "role-inputs.json")
    result["bundle_body_sha"] = digest(private / "bundle-body.txt") if (private / "bundle-body.txt").exists() else None
    db = private / "summary.sqlite3"
    with sqlite3.connect(db) as connection:
        result["db_rows"] = [list(row) for row in connection.execute("SELECT id, summary FROM summaries ORDER BY id")]
    result["sentinels"] = {name: (private / "cwd" / name).read_text()
                           for name in ("command-sentinel", "file-sentinel")}
    result["backend_controls"] = _backend_controls(private, requests)
    counts = Counter(p["case"] for p in provider.posts)
    events = lambda name, case=None: [e for e in result["trace"] if e.get("event") == name and
                                       (case is None or e.get("case") == case)]
    definition = events("begin")[0].get("definition", {}) if events("begin") else {}
    model_readbacks = events("child_model_readback")
    expected_model = {"provider": "q01contract", "id": "scripted", "api": "openai-completions",
                      "contextWindow": 32768, "maxTokens": 8192,
                      "baseUrl": f"http://127.0.0.1:{provider.server_port}/v1"}
    parent = events("parent_receipt", "positive")
    result["checks"] = {
        "run_done": bool(events("done")) and not events("fatal"),
        "bundled_authority": definition.get("name") == "task" and definition.get("source") == "bundled" and
                             definition.get("filePath") == "embedded:task.md" and
                             definition.get("bodySha") == result["bundle_body_sha"],
        "child_model_capability_readback": len(model_readbacks) >= 2 and
                                           all(x.get("model") == expected_model for x in model_readbacks),
        "startup_controls_no_post": all(len(events("startup_rejected", name)) == 1 and
                                         events("startup_rejected", name)[0]["attempt"] == 1 and
                                         len(events("startup_admitted", name)) == 1 and
                                         events("startup_admitted", name)[0]["attempt"] == 2 and
                                         len(events("startup_selfcheck", name)) == 2 and
                                         events("startup_selfcheck", name)[0]["admitted"] is False and
                                         events("startup_selfcheck", name)[1]["admitted"] is True and
                                         len(events("session_created", name)) == 2 and
                                         events("session_created", name)[0]["sessionId"] !=
                                         events("session_created", name)[1]["sessionId"] and
                                         len(events("disposed", name)) == 2 and
                                         len(events("prompt_start", name)) == 0 and
                                         counts[name] == 0
                                         for name in ("startup_missing", "startup_noop", "startup_stale")) and
                                     len(events("startup_rejected", "startup_defer")) == 2 and
                                     len(events("startup_admitted", "startup_defer")) == 0 and
                                     all(x["admitted"] is False for x in events("startup_selfcheck", "startup_defer")) and
                                     len(events("summary_deferred", "startup_defer")) == 1 and
                                     len(events("session_created", "startup_defer")) == 2 and
                                     len(events("disposed", "startup_defer")) == 2 and
                                     counts["startup_defer"] == 0,
        "fixture_difference_observed": result["bundle_body_sha"] != digest(OLD_BODY),
        "system_exact_and_db_only": len(provider.posts) == 4 and
                                    all(p["system_exact"] and p["tools"] == ["db_summary_save"] and
                                        p["tools_present"] and p["permit"] for p in provider.posts),
        "post_counts": counts == Counter({"positive": 2, "saved_then_bash": 2}),
        "positive_actual_typed": len(events("result_accepted", "positive")) == 1 and
                                 all(result["backend_controls"]["checks"].values()),
        "actual_startup_admitted_before_prompt": all(len(events("startup_selfcheck", name)) == 1 and
                                                    events("startup_selfcheck", name)[0]["admitted"] is True and
                                                    len(events("startup_admitted", name)) == 1 and
                                                    len(events("prompt_start", name)) == 1 and
                                                    result["trace"].index(events("startup_admitted", name)[0]) <
                                                    result["trace"].index(events("prompt_start", name)[0])
                                                    for name in ("positive", "saved_then_bash")),
        "manager_role_admitted": all(len(events("role_admission", case)) == 1 and
                                      events("role_admission", case)[0]["admitted"] is True and
                                      events("role_admission", case)[0]["reason"] == "approved_task_manager" and
                                      len(events("session_created", case)) == 1
                                      for case in ("positive", "saved_then_bash")),
        "parent_receipt_bounded": len(parent) == 1 and parent[0]["bytes"] <= 4096 and
                                  parent[0]["parentEntriesAfter"] == parent[0]["parentEntriesBefore"] + 1 and
                                  not parent[0]["entryContainsRawSummary"],
        "forbidden_after_save_protected": len(events("saved", "saved_then_bash")) == 1 and
                                          len(events("abort_completed", "saved_then_bash")) == 1 and
                                          len(events("result_rejected", "saved_then_bash")) == 1 and
                                          not (private / "typed-saved_then_bash.json").exists(),
        "role_negative_no_post": len(events("role_admission", "worker_role")) == 1 and
                                 events("role_admission", "worker_role")[0]["input"] ==
                                 {"role": "Worker", "agentName": "task"} and
                                 events("role_admission", "worker_role")[0]["admitted"] is False and
                                 events("role_admission", "worker_role")[0]["reason"] == "role_not_allowed" and
                                 len(events("role_rejected", "worker_role")) == 1 and
                                 events("role_rejected", "worker_role")[0]["childCreated"] is False and
                                 len(events("session_created", "worker_role")) == 0 and
                                 len(events("prompt_start", "worker_role")) == 0 and
                                 counts["worker_role"] == 0,
        "fixed_db_and_sentinels": result["db_rows"] == [[1, SUMMARY], [2, SUMMARY]] and
                                  all(value == "ORIGINAL" for value in result["sentinels"].values()),
        "parent_rpc_after": any(f.get("id") == "contract-parent-after" and not f.get("error")
                                for f in result["rpc_frames"]),
        "owned_stopped": not result["process"]["remaining_owned"] and result["process"]["pump_stopped"],
    }
    return result


if __name__ == "__main__":
    artifact = run(Path(sys.argv[1]))
    print(json.dumps({"checks": artifact["checks"], "stop_reason": artifact["stop_reason"]}, ensure_ascii=False))
    raise SystemExit(0 if artifact["stop_reason"] is None and all(artifact["checks"].values()) else 2)
