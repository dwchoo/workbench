"""Credential-free full-route OMP events for the Q01 probe-local collector."""

from __future__ import annotations

import hashlib
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

HERE = Path(__file__).resolve().parent
OLD = HERE.parent / "q01_sdk_private_probe"
WRITER = HERE.parent / "q01_script_only_probe/fixed_writer.py"
OUT: Path | None = None
CASES = ("normal", "normal2", "normal3", "recover_missing", "recover_noop", "recover_api", "recover_stale", "recover_exception",
         "repeat_fail", "runtime_roster", "runtime_request", "runtime_route", "runtime_nonce",
         "runtime_model", "runtime_policy", "bash")
POSITIVES = {"normal", "normal2", "normal3", "recover_missing", "recover_noop", "recover_api", "recover_stale", "recover_exception"}
NO_POST = set(CASES) - POSITIVES - {"bash"}
SUMMARY = "Literal command and SQL: touch /tmp/NO_EXECUTE; DROP TABLE summaries; --"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def as_text(v: object) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "".join(str(x.get("text", "")) for x in v if isinstance(x, dict) and x.get("type") == "text")
    return ""


class Provider(ThreadingHTTPServer):
    def __init__(self, private: Path):
        self.private = private
        self.posts: list[dict] = []
        self.outbound: list[dict] = []
        self.violation: str | None = None
        self.lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(inner) -> None:
                raw = inner.rfile.read(int(inner.headers.get("Content-Length", "0")))
                try:
                    req = json.loads(raw)
                except ValueError:
                    req = {}
                messages = req.get("messages") or []
                users = [m for m in messages if m.get("role") == "user"]
                prompt = as_text(users[-1].get("content")) if users else ""
                case = next((c for c in CASES if f"q01-case:{c} " in prompt), "unknown")
                tools_present = "tools" in req
                raw_tools = req["tools"] if tools_present else None
                tools = [t.get("function", {}).get("name") for t in req.get("tools") or []]
                sys_messages = [as_text(m.get("content")) for m in messages if m.get("role") == "system"]
                bundled_body = private / "bundle-body.txt"
                exact = bundled_body.exists() and sys_messages == [bundled_body.read_text() + "\n\n" +
                                                                    (OLD / "summary-skill.txt").read_text()]
                permit = (private / "permits" / case).exists()
                rec = {"case": case, "path": inner.path, "tools": tools, "model": req.get("model"),
                       "max_tokens": req.get("max_tokens"),
                       "max_completion_tokens": req.get("max_completion_tokens"),
                       "max_output_tokens": req.get("max_output_tokens"),
                       "tools_field_present": tools_present,
                       "tools_field_type": type(raw_tools).__name__ if tools_present else "absent",
                       "tools_raw": raw_tools,
                       "request_sha": hashlib.sha256(raw).hexdigest(), "roles": [m.get("role") for m in messages],
                       "system_sha": hashlib.sha256("\n".join(sys_messages).encode()).hexdigest(),
                       "system_exact": exact, "permit": permit}
                with self.lock:
                    raw_path = self.private / f"provider-post-{len(self.posts) + 1:03d}.body"
                    raw_path.write_bytes(raw)
                    rec["raw_body_path"] = str(raw_path)
                    self.posts.append(rec)
                    if not permit or not exact or case in NO_POST or case == "unknown":
                        self.violation = f"unexpected_post:{case}:permit={permit}:system={exact}"
                    if case in POSITIVES | {"bash"} and tools != ["db_summary_save"]:
                        self.violation = f"wrong_tools:{case}:{tools}"
                    violation = self.violation
                if violation:
                    inner.send_response(503)
                    inner.end_headers()
                    return
                tool_messages = [m for m in messages if m.get("role") == "tool"]
                call = None
                if not tool_messages and case in POSITIVES | {"bash"}:
                    name = "db_summary_save" if case in POSITIVES else "bash"
                    args = {"summary": SUMMARY} if name == "db_summary_save" else {
                        "command": f"printf CHANGED > {private / 'cwd/command-sentinel'}"}
                    call = {"id": f"stream-{case}-1", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}
                delta = {"role": "assistant", "tool_calls": [{"index": 0, **call}]} if call else \
                    {"role": "assistant", "content": '{"status":"done"}'}
                finish = "tool_calls" if call else "stop"
                frames = [
                    {"id": "stream", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"id": "stream", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                ]
                body = b"".join(f"data: {json.dumps(f)}\n\n".encode() for f in frames) + b"data: [DONE]\n\n"
                with self.lock:
                    self.outbound.append({"case": case, "call": call, "body_sha": hashlib.sha256(body).hexdigest()})
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


def main(output: Path | None = None) -> int:
    global OUT
    OUT = (output if output is not None else Path(sys.argv[1])).resolve()
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = OUT / "private"
    for name in ("home", "agent", "config", "cwd", "permits"):
        (private / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("command-sentinel", "file-sentinel"):
        (private / "cwd" / name).write_text("ORIGINAL")
    writer = private / "fixed_writer.py"
    shutil.copyfile(WRITER, writer)
    provider = Provider(private)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    try:
        provider_thread.start()
        return _run_with_provider(private, provider, writer)
    finally:
        if provider_thread.is_alive():
            provider.shutdown()
        provider.server_close()
        if provider_thread.ident is not None:
            provider_thread.join(2)


def _run_with_provider(private: Path, provider: Provider, writer: Path) -> int:
    (private / "agent/models.yml").write_text(
        "providers:\n  q01startup:\n" f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
        "    api: openai-completions\n    auth: none\n    models:\n"
        "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 8192\n"
        "  q01startupalt:\n" f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
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
           "XDG_CACHE_HOME": str(private / "xdg-cache"), "Q01_FULL_TRACE": str(private / "trace.jsonl"),
           "Q01_STARTUP_PERMITS": str(private / "permits"), "Q01_FIXED_WRITER": str(writer),
           "Q01_FIXED_PYTHON": sys.executable, "Q01_AGENT_BODY": str(OLD / "agent-body.txt"),
           "Q01_BUNDLE_BODY": str(private / "bundle-body.txt"),
           "Q01_SUMMARY_SKILL": str(OLD / "summary-skill.txt"), "PYTHONDONTWRITEBYTECODE": "1",
           "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    env.update({k: proxy for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")})
    argv = [str(omp), "--mode", "rpc", "--no-session", "--no-title", "--no-extensions", "--no-skills",
            "--no-rules", "--no-tools", "--no-pty", "--config", str(private / "overlay.yml"),
            "--extension", str(HERE / "full_extension.ts"), "--model", "q01startup/scripted",
            "--cwd", str(private / "cwd")]
    version = subprocess.run([str(omp), "--version"], env=env, capture_output=True, text=True, timeout=10)
    result: dict = {"invocation": "q01-sdk-full-runtime01", "omp_version": version.stdout.strip(),
                    "omp_binary_sha": sha(omp), "argv": argv, "environment_keys": sorted(env),
                    "source_sha": {str(p): sha(p) for p in (HERE / "full_extension.ts", HERE / "role_admission.ts", HERE / "run_full.py",
                                                            OLD / "agent-body.txt", OLD / "summary-skill.txt", WRITER)},
                    "provider_port": provider.server_port, "rpc_sent": [], "rpc_frames": []}
    frames: list[dict] = []
    cond = threading.Condition()
    with (OUT / "omp-stderr.log").open("wb") as stderr:
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
                    with cond:
                        frames.append(frame)
                        cond.notify_all()

        pump_thread = threading.Thread(target=pump, daemon=True)
        pump_thread.start()
        try:
            command = {"id": "full-parent-before", "type": "get_state"}
            proc.stdin.write((json.dumps(command) + "\n").encode())
            proc.stdin.flush()
            result["rpc_sent"].append(command)
            deadline = time.monotonic() + 120
            trace = private / "trace.jsonl"
            completed = False
            while time.monotonic() < deadline and proc.poll() is None:
                if trace.exists() and '"event":"done"' in trace.read_text():
                    completed = True
                    break
                if provider.violation:
                    break
                with cond:
                    cond.wait(0.2)
            if completed and proc.poll() is None:
                command = {"id": "full-parent-after", "type": "get_state"}
                proc.stdin.write((json.dumps(command) + "\n").encode())
                proc.stdin.flush()
                result["rpc_sent"].append(command)
                with cond:
                    cond.wait_for(lambda: any(f.get("id") == "full-parent-after" for f in frames), 3)
            result["stop_reason"] = provider.violation or (None if completed else "timeout_or_parent_exit")
            result["trace"] = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
            result["rpc_frames"] = [{"id": f.get("id"), "type": f.get("type"), "error": f.get("error")} for f in frames[:100]]
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
            pump_thread.join(2)
            result["process"] = {"pid": proc.pid, "returncode": proc.returncode,
                                 "pump_stopped": not pump_thread.is_alive(),
                                 "remaining_owned": [] if proc.poll() is not None else [proc.pid]}
    result["provider_posts"] = provider.posts
    result["provider_outbound"] = provider.outbound
    db = private / "summary.sqlite3"
    if db.exists():
        with sqlite3.connect(db) as con:
            result["db_rows"] = [list(row) for row in con.execute("SELECT id, summary FROM summaries ORDER BY id")]
    else:
        result["db_rows"] = []
    result["sentinels"] = {n: (private / "cwd" / n).read_text() for n in ("command-sentinel", "file-sentinel")}
    result["permits_remaining"] = sorted(p.name for p in (private / "permits").iterdir())
    trace_events = result["trace"]
    posts = provider.posts
    events = lambda kind, c: [e for e in trace_events if e.get("event") == kind and e.get("case") == c]
    post_count = {c: sum(p["case"] == c for p in posts) for c in CASES}
    first_failures = {"recover_missing", "recover_noop", "recover_api", "recover_stale", "repeat_fail"}
    result["cases"] = {c: {"selfchecks": events("startup_selfcheck", c),
                           "exceptions": events("startup_exception", c),
                           "created": events("session_created", c),
                           "rejections": events("startup_rejected", c),
                           "admissions": events("startup_admitted", c),
                           "disposals": events("disposed", c),
                           "prompts": len(events("prompt_start", c)),
                           "provider_posts": post_count[c],
                           "stream_guards": events("stream_guard", c),
                           "case_done": events("case_done", c)} for c in CASES}
    result["checks"] = {
        "run_done": any(e.get("event") == "done" and e.get("ok") for e in trace_events),
        "owner_role_agent_admitted": len([e for e in trace_events if e.get("event") == "role_admission"]) == 1 and
                                      next((e.get("reason") for e in trace_events
                                            if e.get("event") == "role_admission"), None) == "approved_task_manager" and
                                      any(e.get("event") == "role_admission" and e.get("admitted") is True
                                          for e in trace_events),
        "all_cases_completed": [e.get("case") for e in trace_events if e.get("event") == "case_done"] == list(CASES),
        "private_config_readback": all(s["checks"]["config"] and s["active"] == ["db_summary_save"] and
                                        s["privateHas"] and not s["globalHas"] and s["settingsIdentity"]
                                        for c in CASES for s in result["cases"][c]["selfchecks"]),
        "first_failure_before_prompt": all(len(events("startup_rejected", c)) >= 1 and
                                           events("startup_rejected", c)[0]["attempt"] == 1 and
                                           not any(e["attempt"] == 1 for e in events("prompt_start", c))
                                           for c in first_failures),
        "missing_noop_api_stale_causes": {c: events("startup_rejected", c)[0]["reason"]
                                           for c in first_failures if events("startup_rejected", c)} == {
            "recover_missing": "guard_api_or_missing", "recover_noop": "ineffective_guard",
            "recover_api": "guard_api_or_missing", "recover_stale": "stale_or_invalid_valid_case",
            "repeat_fail": "ineffective_guard"},
        "rebuild_once_success": all(len(events("startup_selfcheck", c)) == 2 and
                                    len(events("disposed", c)) == 2 and
                                    events("startup_admitted", c)[0]["attempt"] == 2 and
                                    events("startup_selfcheck", c)[0]["sessionId"] !=
                                    events("startup_selfcheck", c)[1]["sessionId"]
                                    for c in first_failures - {"repeat_fail"}),
        "exception_rebuild_once_success": len(events("startup_exception", "recover_exception")) == 1 and
                                          events("startup_exception", "recover_exception")[0]["attempt"] == 1 and
                                          events("startup_exception", "recover_exception")[0]["promptCount"] == 0 and
                                          "controlled_startup_checker_throw" in
                                          events("startup_exception", "recover_exception")[0]["error"] and
                                          len(events("session_created", "recover_exception")) == 2 and
                                          events("session_created", "recover_exception")[0]["sessionId"] !=
                                          events("session_created", "recover_exception")[1]["sessionId"] and
                                          len(events("disposed", "recover_exception")) == 2 and
                                          len(events("startup_admitted", "recover_exception")) == 1 and
                                          events("startup_admitted", "recover_exception")[0]["attempt"] == 2 and
                                          len(events("prompt_start", "recover_exception")) == 1 and
                                          post_count["recover_exception"] == 2,
        "repeat_failure_deferred": len(events("startup_rejected", "repeat_fail")) == 2 and
                                   len(events("disposed", "repeat_fail")) == 2 and
                                   len(events("summary_deferred", "repeat_fail")) == 1 and
                                   len(events("prompt_start", "repeat_fail")) == 0 and
                                   post_count["repeat_fail"] == 0,
        "one_prompt_per_positive": all(len(events("prompt_start", c)) == 1 and
                                       len(events("result_accepted", c)) == 1 for c in POSITIVES),
        "positive_sole_db_posts": all(post_count[c] == 2 and all(p["tools"] == ["db_summary_save"] and
                                      p["tools_field_present"] for p in posts if p["case"] == c)
                                      for c in POSITIVES),
        "positive_exact_rows": result["db_rows"] == [[i, SUMMARY] for i in range(1, 9)],
        "three_normal_isolated_turns": all(len(events("session_created", c)) == 1 and
                                           len(events("prompt_start", c)) == 1 and
                                           len(events("result_accepted", c)) == 1
                                           for c in ("normal", "normal2", "normal3")) and
                                       len({events("session_created", c)[0]["sessionId"]
                                            for c in ("normal", "normal2", "normal3")}) == 3,
        "child_model_readback_before_prompt": all(len(events("child_model_readback", c)) ==
                                                  len(events("session_created", c)) and
                                                  all(e["model"]["provider"] == "q01startup" and
                                                      e["model"]["contextWindow"] == 32768 and
                                                      e["model"]["maxTokens"] == 8192
                                                      for e in events("child_model_readback", c))
                                                  for c in CASES),
        "scripted_budget_preflight": all(len(events("budget_preflight", c)) == 1 and
                                         events("budget_preflight", c)[0]["preflightOk"] is True and
                                         events("budget_preflight", c)[0]["inputBytes"] <=
                                         events("budget_preflight", c)[0]["inputLimit"] and
                                         events("budget_preflight", c)[0]["outputReserve"] == 8192
                                         for c in CASES if c != "repeat_fail"),
        "runtime_roster_block": len(events("startup_admitted", "runtime_roster")) == 1 and
                                len(events("prompt_start", "runtime_roster")) == 1 and
                                post_count["runtime_roster"] == 0 and
                                any(e.get("reason") == "roster_mismatch" for e in events("stream_guard", "runtime_roster")) and
                                len(events("result_rejected", "runtime_roster")) == 1,
        "runtime_binding_blocks": all(len(events("startup_admitted", c)) == 1 and
                                      len(events("prompt_start", c)) == 1 and post_count[c] == 0 and
                                      len(events("result_rejected", c)) == 1 and
                                      any(e.get("reason") == reason for e in events("stream_guard", c))
                                      for c, reason in (("runtime_request", "stale_binding"),
                                                        ("runtime_route", "stale_binding"),
                                                        ("runtime_nonce", "stale_binding"),
                                                        ("runtime_model", "model_mismatch"),
                                                        ("runtime_policy", "policy_mismatch"))),
        "route_nonce_block_before_stream": all(
            len(events("startup_admitted", c)) == 1 and len(events("prompt_start", c)) == 1 and
            post_count[c] == 0 and not events("original_stream_called", c) and
            not events("writer_invoked", c) and not events("saved", c) and
            len(events("stream_guard", c)) >= 1 and
            all(e.get("reason") == "stale_binding" for e in events("stream_guard", c)) and
            len(events("prompt_done", c)) == 1 and events("prompt_done", c)[0]["protected"] is True and
            len(events("result_rejected", c)) == 1 and len(events("disposed", c)) == 1 and
            len(events("case_done", c)) == 1 and events("case_done", c)[0]["protected"] is True
            for c in ("runtime_route", "runtime_nonce")),
        "route_nonce_only_binding_changed": all(
            len(events("startup_admitted", c)) == 1 and len(events("before_prompt", c)) == 1 and
            {key for key, value in events("startup_admitted", c)[0]["receipt"].items()
             if events("before_prompt", c)[0]["receipt"].get(key) != value} == {field} and
            len(events("startup_admitted", c)[0]["receipt"]) ==
                len(events("before_prompt", c)[0]["receipt"])
            for c, field in (("runtime_route", "route"), ("runtime_nonce", "nonce"))),
        "positive_receipts_bound_unique": all(
            len(events("startup_admitted", c)) == 1 and len(events("before_prompt", c)) == 1 and
            events("startup_admitted", c)[0]["receipt"] == events("before_prompt", c)[0]["receipt"] and
            events("startup_admitted", c)[0]["receipt"]["sessionId"] ==
                events("session_created", c)[-1]["sessionId"] and
            events("startup_admitted", c)[0]["receipt"]["requestId"] == "q01-" + c and
            events("startup_admitted", c)[0]["receipt"]["epoch"] ==
                "epoch-" + c + "-" + str(events("startup_admitted", c)[0]["attempt"]) and
            events("startup_admitted", c)[0]["receipt"]["route"] == "private-manager-task" and
            bool(events("startup_admitted", c)[0]["receipt"]["policySha"]) and
            len(events("result_accepted", c)) == 1 for c in POSITIVES) and
            len({events("startup_admitted", c)[0]["receipt"]["nonce"] for c in POSITIVES}) == len(POSITIVES),
        "bash_tool_not_found": any(e.get("event") == "tool_end" and e.get("case") == "bash"
                                   and e.get("toolName") == "bash" and e.get("isError") is True
                                   and "not found" in str(e.get("result", "")) for e in trace_events),
        "bash_abort_POST_one": post_count["bash"] == 1
                               and any(e.get("event") == "abort_completed" and e.get("case") == "bash"
                                       for e in trace_events)
                               and any(e.get("event") == "result_rejected" and e.get("case") == "bash"
                                       for e in trace_events),
        "no_rebuild_after_prompt": all(len(events("startup_selfcheck", c)) == 1 for c in
                                       ("normal", "normal2", "normal3", "runtime_roster", "runtime_request",
                                        "runtime_route", "runtime_nonce",
                                        "runtime_model", "runtime_policy", "bash")),
        "serialized_scripted_output_ceiling": all(p["max_tokens"] == 8192 or
                                                  p["max_completion_tokens"] == 8192 or
                                                  p["max_output_tokens"] == 8192 for p in posts),
        "raw_post_bytes_attributed": all(Path(p["raw_body_path"]).exists() and
                                         sha(Path(p["raw_body_path"])) == p["request_sha"] and
                                         json.loads(Path(p["raw_body_path"]).read_bytes())["model"] == p["model"]
                                         for p in posts),
        "system_exact": bool(posts) and all(p["system_exact"] for p in posts),
        "sentinels_unchanged": all(v == "ORIGINAL" for v in result["sentinels"].values()),
        "parent_unchanged": any(e.get("event") == "done" and e.get("parentBefore") == e.get("parentAfter")
                                for e in trace_events),
        "parent_rpc_after": any(f.get("id") == "full-parent-after" and not f.get("error")
                                for f in result["rpc_frames"]),
        "all_processes_stopped": not result["process"]["remaining_owned"] and result["process"]["pump_stopped"],
    }
    (OUT / "runtime-evidence.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0 if all(result["checks"].values()) and not result["stop_reason"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
