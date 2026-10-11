"""One installed OMP parent, private SDK children, local scripted provider."""

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
OLD_WRITER = HERE.parent / "q01_script_only_probe" / "fixed_writer.py"
OUT = Path(sys.argv[1]).resolve()
PROMPTS = {"p1", "p2", "switch", "invalid", "writer_fail", "bash", "think", "retry", "noop"}
BLOCKED = {"missing", "expand", "stale"}
SUMMARY = "Literal log: touch /tmp/NO_EXECUTE; DROP TABLE summaries; -- kept as data"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("type") == "text")
    return ""


def rows(db: Path) -> list[list[object]]:
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        return [list(row) for row in conn.execute("SELECT id, summary FROM summaries ORDER BY id")]


class Provider(ThreadingHTTPServer):
    def __init__(self, private: Path):
        self.private = private
        self.posts: list[dict] = []
        self.outbound: list[dict] = []
        self.called: dict[str, int] = {}
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
                prompt = text(users[-1].get("content")) if users else ""
                case = next((c for c in PROMPTS | BLOCKED if f"q01-case:{c} " in prompt), "unknown")
                tools = [t.get("function", {}).get("name") for t in request.get("tools") or []]
                sys_messages = [text(m.get("content")) for m in messages if m.get("role") == "system"]
                expected_body = (HERE / "agent-body.txt").read_text()
                expected_skill = (HERE / "summary-skill.txt").read_text()
                joined = "\n".join(sys_messages)
                sys_exact = expected_body in joined and expected_skill in joined and joined.index(expected_body) < joined.index(expected_skill)
                permit = private / "permits" / case
                rec = {"case": case, "path": inner.path, "tools": tools, "model": request.get("model"),
                       "api_path": inner.path, "raw_sha": hashlib.sha256(raw).hexdigest(),
                       "roles": [m.get("role") for m in messages], "system_sha": hashlib.sha256(joined.encode()).hexdigest(),
                       "system_bytes": len(joined.encode()), "system_messages": sys_messages,
                       "body_exact_in_order": sys_exact,
                       "pre_prompt": not permit.exists(),
                       "tool_schemas_sha": hashlib.sha256(json.dumps(request.get("tools"), sort_keys=True).encode()).hexdigest()}
                with self.lock:
                    self.posts.append(rec)
                    if case not in PROMPTS or rec["pre_prompt"] or tools != ["db_summary_save"] or not sys_exact:
                        self.violation = f"unexpected_POST:{case}:pre={rec['pre_prompt']}:tools={tools}:system={sys_exact}"
                    count = self.called.get(case, 0)
                    self.called[case] = count + 1
                    violation = self.violation
                if violation:
                    inner.send_response(503)
                    inner.end_headers()
                    return
                if case == "retry" and count == 0:
                    inner.send_response(503)
                    inner.send_header("Retry-After", "0")
                    inner.end_headers()
                    with self.lock:
                        self.outbound.append({"case": case, "kind": "retryable_503"})
                    return
                tool_messages = [m for m in messages if m.get("role") == "tool"]
                call = None
                if case != "noop" and (not tool_messages or
                                       (case in {"bash", "think"} and len(tool_messages) == 1)):
                    name = case if case in {"bash", "think"} and not tool_messages else "db_summary_save"
                    args = ({"summary": SUMMARY, "command": "printf CHANGED > /forbidden"}
                    if case == "invalid" else {"summary": SUMMARY} if name == "db_summary_save" else (
                        {"command": f"printf CHANGED > {private / 'cwd/command-sentinel'}"}
                        if name == "bash" else {"thought": "try forbidden think"}))
                    call = {"id": f"private-{case}-{len(tool_messages) + 1}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}
                delta = {"role": "assistant", "tool_calls": [{"index": 0, **call}]} if call else \
                    {"role": "assistant", "content": '{"status":"done"}'}
                finish = "tool_calls" if call else "stop"
                frames = [
                    {"id": "private", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"id": "private", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                ]
                payload = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
                with self.lock:
                    self.outbound.append({"case": case, "call": call,
                                          "body_sha": hashlib.sha256(payload).hexdigest()})
                try:
                    inner.send_response(200)
                    inner.send_header("Content-Type", "text/event-stream")
                    inner.send_header("Content-Length", str(len(payload)))
                    inner.end_headers()
                    inner.wfile.write(payload)
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
    (private / "cwd/command-sentinel").write_text("ORIGINAL")
    (private / "cwd/file-sentinel").write_text("ORIGINAL")
    writer = private / "fixed_writer.py"
    shutil.copyfile(OLD_WRITER, writer)
    fail_writer = private / "failwriter/fixed_writer.py"
    fail_writer.parent.mkdir()
    shutil.copyfile(OLD_WRITER, fail_writer)
    (fail_writer.parent / "summary.sqlite3").mkdir()
    provider = Provider(private)
    server_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    server_thread.start()
    (private / "agent/models.yml").write_text(
        "providers:\n  q01private:\n"
        f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
        "    api: openai-completions\n    auth: none\n    models:\n"
        "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 1024\n"
        "  q01privresp:\n"
        f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
        "    api: openai-responses\n    auth: none\n    models:\n"
        "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 1024\n")
    (private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
    omp = Path(shutil.which("omp") or "")
    proxy = "http://127.0.0.1:9"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(private / "home"), "TERM": "dumb", "LANG": "C.UTF-8",
           "PI_CODING_AGENT_DIR": str(private / "agent"), "PI_CONFIG_DIR": str(private / "config"),
           "XDG_CONFIG_HOME": str(private / "xdg-config"), "XDG_DATA_HOME": str(private / "xdg-data"),
           "XDG_STATE_HOME": str(private / "xdg-state"), "XDG_CACHE_HOME": str(private / "xdg-cache"),
           "Q01_PRIVATE_TRACE": str(private / "trace.jsonl"), "Q01_PRIVATE_PERMITS": str(private / "permits"),
           "Q01_FIXED_WRITER": str(writer), "Q01_FIXED_PYTHON": sys.executable,
           "Q01_FAILING_WRITER": str(fail_writer),
           "Q01_AGENT_BODY": str(HERE / "agent-body.txt"), "Q01_SUMMARY_SKILL": str(HERE / "summary-skill.txt"),
           "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    env.update({k: proxy for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")})
    argv = [str(omp), "--mode", "rpc", "--no-session", "--no-title", "--no-extensions", "--no-skills",
            "--no-rules", "--no-tools", "--no-pty", "--config", str(private / "overlay.yml"),
            "--extension", str(HERE / "private_extension.ts"), "--model", "q01private/scripted",
            "--cwd", str(private / "cwd")]
    version = subprocess.run([str(omp), "--version"], env=env, capture_output=True, text=True, timeout=10)
    result: dict = {"invocation": "q01-sdk-private01", "omp_version": version.stdout.strip(),
                    "omp_binary_sha": sha(omp), "argv": argv, "environment_keys": sorted(env),
                    "source_sha": {str(p): sha(p) for p in
                                   (HERE / "private_extension.ts", HERE / "run_private_probe.py",
                                    HERE / "agent-body.txt", HERE / "summary-skill.txt", OLD_WRITER)},
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
            command = {"id": "private-parent-state", "type": "get_state"}
            proc.stdin.write((json.dumps(command) + "\n").encode())
            proc.stdin.flush()
            result["rpc_sent"].append(command)
            deadline = time.monotonic() + 150
            completed = False
            trace = private / "trace.jsonl"
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
            server_thread.join(2)
    result["provider_posts"] = provider.posts
    result["provider_outbound"] = provider.outbound
    result["db_rows"] = rows(private / "summary.sqlite3")
    result["sentinels"] = {n: (private / "cwd" / n).read_text() for n in ("command-sentinel", "file-sentinel")}
    result["permits_remaining"] = sorted(p.name for p in (private / "permits").iterdir())
    trace_events = result["trace"]
    readback = {e["case"]: e for e in trace_events if e.get("event") == "readback"}
    posts = provider.posts
    result["checks"] = {
        "run_done": any(e.get("event") == "done" and e.get("ok") for e in trace_events),
        "all_cases_readback": set(readback) == PROMPTS | BLOCKED,
        "private_registry_no_global_entry": bool(readback) and all(e["privateHas"] and not e["globalHas"] for e in readback.values()),
        "normal_sole_roster": all(readback.get(c, {}).get("active") == ["db_summary_save"] for c in PROMPTS),
        "blocked_roster_or_stale": all(any(e.get("event") == "admission_rejected" and e.get("case") == c
                                            for e in trace_events) for c in BLOCKED),
        "blocked_post_zero": all(p["case"] not in BLOCKED for p in posts),
        "post_only_db": bool(posts) and all(p["tools"] == ["db_summary_save"] for p in posts),
        "post_prompt_only": all(not p["pre_prompt"] for p in posts),
        "exact_system_bytes_twice": len([p for p in posts if p["case"] in {"p1", "p2"}]) >= 2
                                  and all(p["body_exact_in_order"] for p in posts)
                                  and len({p["system_sha"] for p in posts if p["case"] in {"p1", "p2"}}) == 1,
        "two_rows_then_retry_switch": result["db_rows"] == [[1, SUMMARY], [2, SUMMARY], [3, SUMMARY], [4, SUMMARY]],
        "forbidden_refused": all(any(e.get("event") == "child_event" and e.get("case") == c
                                      and e.get("type") == "tool_execution_end" and e.get("isError") is True
                                      for e in trace_events) for c in ("bash", "think")),
        "forbidden_and_noop_result_rejected": all(any(e.get("event") == "result_rejected"
                                                        and e.get("case") == c for e in trace_events)
                                                    for c in ("bash", "think", "noop")),
        "forbidden_abort_no_next_post": all(sum(p["case"] == c for p in posts) == 1
                                             and any(e.get("event") == "abort_completed" and e.get("case") == c
                                                     for e in trace_events)
                                             and not any(e.get("event") == "writer_invoked" and e.get("case") == c
                                                         for e in trace_events)
                                             for c in ("bash", "think")),
        "failed_saves_rejected": all(any(e.get("event") == "result_rejected" and e.get("case") == c
                                         for e in trace_events)
                                     and not any(e.get("event") == "saved" and e.get("case") == c
                                                 for e in trace_events)
                                     for c in ("invalid", "writer_fail")),
        "invalid_extra_params_reached_execute": any(e.get("event") == "execute_received"
                                                     and e.get("case") == "invalid"
                                                     and set(e.get("params", {})) == {"summary", "command"}
                                                     for e in trace_events),
        "actual_writer_failure": any(e.get("event") == "writer_invoked" and e.get("case") == "writer_fail"
                                     for e in trace_events)
                                 and any(e.get("event") == "writer_failed" and e.get("case") == "writer_fail"
                                         and e.get("error") for e in trace_events),
        "normal_results_accepted": all(any(e.get("event") == "result_accepted" and e.get("case") == c
                                            for e in trace_events) for c in ("p1", "p2", "switch", "retry")),
        "sentinels_unchanged": all(v == "ORIGINAL" for v in result["sentinels"].values()),
        "parent_unchanged": any(e.get("event") == "done" and e.get("parentBefore") == e.get("parentAfter")
                                for e in trace_events),
        "retry_post": len([p for p in posts if p["case"] == "retry"]) >= 2,
        "no_owned_process": not result["process"]["remaining_owned"] and result["process"]["pump_stopped"],
        "no_provider_violation": provider.violation is None,
    }
    (OUT / "runtime-evidence.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0 if all(result["checks"].values()) and not result["stop_reason"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
