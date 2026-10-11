"""Local controlled guard matrix against one installed OMP parent and scripted provider."""

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
OUT = Path(sys.argv[1]).resolve()
CASES = ("normal", "missing_guard", "noop_guard", "api_shape_absent", "stale", "wrong_session",
         "disposed", "roster_change", "schema_change", "policy_change", "bash", "checksend")
BLOCKED = set(CASES) - {"normal", "bash", "checksend"}
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
                tools = [t.get("function", {}).get("name") for t in req.get("tools") or []]
                sys_messages = [as_text(m.get("content")) for m in messages if m.get("role") == "system"]
                exact = sys_messages == [(OLD / "agent-body.txt").read_text() + "\n\n" +
                                         (OLD / "summary-skill.txt").read_text()]
                permit = (private / "permits" / case).exists()
                rec = {"case": case, "path": inner.path, "tools": tools, "model": req.get("model"),
                       "request_sha": hashlib.sha256(raw).hexdigest(), "roles": [m.get("role") for m in messages],
                       "system_sha": hashlib.sha256("\n".join(sys_messages).encode()).hexdigest(),
                       "system_exact": exact, "permit": permit}
                with self.lock:
                    self.posts.append(rec)
                    if not permit or not exact or case in BLOCKED or case == "unknown":
                        self.violation = f"unexpected_post:{case}:permit={permit}:system={exact}"
                    if case in {"normal", "bash"} and tools != ["db_summary_save"]:
                        self.violation = f"wrong_tools:{case}:{tools}"
                    violation = self.violation
                if violation:
                    inner.send_response(503)
                    inner.end_headers()
                    return
                tool_messages = [m for m in messages if m.get("role") == "tool"]
                call = None
                if not tool_messages and case in {"normal", "bash"}:
                    name = "db_summary_save" if case == "normal" else "bash"
                    args = {"summary": SUMMARY} if name == "db_summary_save" else {
                        "command": f"printf CHANGED > {private / 'cwd/command-sentinel'}"}
                    call = {"id": f"matrix-{case}-1", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}
                delta = {"role": "assistant", "tool_calls": [{"index": 0, **call}]} if call else \
                    {"role": "assistant", "content": '{"status":"done"}'}
                finish = "tool_calls" if call else "stop"
                frames = [
                    {"id": "matrix", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"id": "matrix", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
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


def main() -> int:
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
    provider_thread.start()
    (private / "agent/models.yml").write_text(
        "providers:\n  q01matrix:\n" f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
        "    api: openai-completions\n    auth: none\n    models:\n"
        "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 1024\n")
    (private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
    omp = Path(shutil.which("omp") or "")
    proxy = "http://127.0.0.1:9"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(private / "home"),
           "TERM": "dumb", "LANG": "C.UTF-8", "PI_CODING_AGENT_DIR": str(private / "agent"),
           "PI_CONFIG_DIR": str(private / "config"), "XDG_CONFIG_HOME": str(private / "xdg-config"),
           "XDG_DATA_HOME": str(private / "xdg-data"), "XDG_STATE_HOME": str(private / "xdg-state"),
           "XDG_CACHE_HOME": str(private / "xdg-cache"), "Q01_MATRIX_TRACE": str(private / "trace.jsonl"),
           "Q01_MATRIX_PERMITS": str(private / "permits"), "Q01_FIXED_WRITER": str(writer),
           "Q01_FIXED_PYTHON": sys.executable, "Q01_AGENT_BODY": str(OLD / "agent-body.txt"),
           "Q01_SUMMARY_SKILL": str(OLD / "summary-skill.txt"), "PYTHONDONTWRITEBYTECODE": "1",
           "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    env.update({k: proxy for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")})
    argv = [str(omp), "--mode", "rpc", "--no-session", "--no-title", "--no-extensions", "--no-skills",
            "--no-rules", "--no-tools", "--no-pty", "--config", str(private / "overlay.yml"),
            "--extension", str(HERE / "matrix_extension.ts"), "--model", "q01matrix/scripted",
            "--cwd", str(private / "cwd")]
    version = subprocess.run([str(omp), "--version"], env=env, capture_output=True, text=True, timeout=10)
    result: dict = {"invocation": "q01-sdk-matrix01", "omp_version": version.stdout.strip(),
                    "omp_binary_sha": sha(omp), "argv": argv, "environment_keys": sorted(env),
                    "source_sha": {str(p): sha(p) for p in (HERE / "matrix_extension.ts", HERE / "run_matrix.py",
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
            command = {"id": "matrix-parent-state", "type": "get_state"}
            proc.stdin.write((json.dumps(command) + "\n").encode())
            proc.stdin.flush()
            result["rpc_sent"].append(command)
            deadline = time.monotonic() + 90
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
            provider.shutdown()
            provider.server_close()
            provider_thread.join(2)
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
    readbacks = {e["case"]: e for e in trace_events if e.get("event") == "initial_check"}
    rejects = {e["case"]: e.get("reason") for e in trace_events if e.get("event") == "admission_rejected"}
    fault = next((e for e in trace_events if e.get("event") == "fault_after_gate_before_prompt"), None)
    fault_post = [p for p in posts if p["case"] == "checksend"]
    result["fault_seam"] = {"after_final_gate_before_prompt": bool(fault),
                            "applied": any(e.get("event") == "fault_applied" for e in trace_events),
                            "provider_POST": bool(fault_post), "provider_tools": [p["tools"] for p in fault_post],
                            "native_mandatory_guard_observed": not fault_post}
    result["checks"] = {
        "run_done": any(e.get("event") == "done" and e.get("ok") for e in trace_events),
        "all_cases_created": set(readbacks) == set(CASES),
        "private_registry": bool(readbacks) and all(e["privateHas"] and not e["globalHas"] for e in readbacks.values()),
        "initial_sole_db": bool(readbacks) and all(e["active"] == ["db_summary_save"] for e in readbacks.values()),
        "controlled_rejections": set(rejects) == BLOCKED,
        "blocked_prompt_zero": all(not any(e.get("event") == "prompt_start" and e.get("case") == c
                                          for e in trace_events) for c in BLOCKED),
        "blocked_POST_zero": all(not any(p["case"] == c for p in posts) for c in BLOCKED),
        "normal_sole_db": [p["tools"] for p in posts if p["case"] == "normal"] == [
            ["db_summary_save"], ["db_summary_save"]],
        "normal_saved_exact_one": result["db_rows"] == [[1, SUMMARY]],
        "bash_tool_not_found": any(e.get("event") == "tool_end" and e.get("case") == "bash"
                                   and e.get("toolName") == "bash" and e.get("isError") is True
                                   and "not found" in str(e.get("result", "")) for e in trace_events),
        "bash_abort_POST_one": sum(p["case"] == "bash" for p in posts) == 1
                               and any(e.get("event") == "abort_completed" and e.get("case") == "bash"
                                       for e in trace_events)
                               and any(e.get("event") == "result_rejected" and e.get("case") == "bash"
                                       for e in trace_events),
        "system_exact": bool(posts) and all(p["system_exact"] for p in posts),
        "sentinels_unchanged": all(v == "ORIGINAL" for v in result["sentinels"].values()),
        "parent_unchanged": any(e.get("event") == "done" and e.get("parentBefore") == e.get("parentAfter")
                                for e in trace_events),
        "all_processes_stopped": not result["process"]["remaining_owned"] and result["process"]["pump_stopped"],
    }
    (OUT / "runtime-evidence.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0 if all(result["checks"].values()) and not result["stop_reason"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
