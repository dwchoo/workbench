"""One private OMP parent with scripted provider and public in-process SDK children."""

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
SUMMARY = "literal: touch /tmp/q01-should-not-run; DROP TABLE summaries; -- data only"
PROMPT_CASES = ("positive", "bash", "write", "edit", "inject_command", "inject_sql", "inject_path")
BLOCKED_CASES = ("missing_allow", "extra_tool", "stale_session", "reused_request")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def db_rows(db: Path) -> list[list[object]]:
    if not db.exists():
        return []
    with sqlite3.connect(db) as connection:
        return [list(row) for row in connection.execute("SELECT id, summary FROM summaries ORDER BY id")]


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("type") == "text")
    return ""


class ScriptedProvider(ThreadingHTTPServer):
    def __init__(self, private: Path):
        self.private = private
        self.posts: list[dict[str, object]] = []
        self.outbound: list[dict[str, object]] = []
        self.receipts: dict[str, list[str]] = {}
        self.called: set[str] = set()
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
                prompt = content_text(users[-1].get("content")) if users else ""
                case = next((x for x in PROMPT_CASES + BLOCKED_CASES if f"q01-case:{x}" in prompt), "unknown")
                tools = [t.get("function", {}).get("name") for t in request.get("tools") or []]
                permit = private / "permits" / case
                pre_prompt = not permit.exists()
                with self.lock:
                    self.posts.append({"case": case, "path": inner.path, "tools": tools,
                                       "request_sha256": hashlib.sha256(raw).hexdigest(),
                                       "pre_prompt": pre_prompt, "model": request.get("model")})
                    if pre_prompt or tools != ["db_summary_save"] or case not in PROMPT_CASES:
                        self.violation = f"unexpected_provider_post:{case}:pre={pre_prompt}:tools={tools}"
                    tool_messages = [m for m in messages if m.get("role") == "tool"]
                    if tool_messages:
                        self.receipts[case] = [content_text(m.get("content"))[:1000] for m in tool_messages]
                    first = case not in self.called
                    self.called.add(case)
                    violation = self.violation
                if violation:
                    inner.send_response(503)
                    inner.end_headers()
                    return
                calls: list[dict[str, object]] = []
                if first:
                    if case == "positive":
                        calls = [self.call(case, "db_summary_save", {"summary": SUMMARY}, i) for i in (1, 2)]
                    elif case == "bash":
                        calls = [self.call(case, "bash", {"command": f"printf CHANGED > {private / 'cwd/command-sentinel'}"}, 1)]
                    elif case == "write":
                        calls = [self.call(case, "write", {"path": str(private / "cwd/file-sentinel"), "content": "CHANGED"}, 1)]
                    elif case == "edit":
                        calls = [self.call(case, "edit", {"path": str(private / "cwd/file-sentinel"),
                                                          "oldText": "ORIGINAL", "newText": "CHANGED"}, 1)]
                    else:
                        extra = case.removeprefix("inject_")
                        value = {"command": f"printf CHANGED > {private / 'cwd/command-sentinel'}",
                                 "sql": "DROP TABLE summaries", "path": str(private / "cwd/file-sentinel")}[extra]
                        calls = [self.call(case, "db_summary_save", {"summary": SUMMARY, extra: value}, 1)]
                delta = {"role": "assistant", "tool_calls": [{"index": i, **call} for i, call in enumerate(calls)]} if calls else \
                    {"role": "assistant", "content": "{\"status\":\"done\"}"}
                finish = "tool_calls" if calls else "stop"
                frames = [
                    {"id": "q01child", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"id": "q01child", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                ]
                body = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
                with self.lock:
                    self.outbound.append({"case": case, "calls": calls, "body_sha256": hashlib.sha256(body).hexdigest()})
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

    def call(self, case: str, name: str, args: dict[str, object], index: int) -> dict[str, object]:
        return {"id": f"q01-{case}-{index}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = OUT / "private"
    private.mkdir(mode=0o700)
    home, agent, config, cwd, permits = (private / name for name in ("home", "agent", "config", "cwd", "permits"))
    for directory in (home, agent, config, cwd, permits):
        directory.mkdir(mode=0o700)
    for name in ("command-sentinel", "file-sentinel"):
        (cwd / name).write_text("ORIGINAL")
    writer = private / "fixed_writer.py"
    shutil.copyfile(OLD_WRITER, writer)
    provider = ScriptedProvider(private)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    (agent / "models.yml").write_text(
        "providers:\n  q01child:\n" f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
        "    api: openai-completions\n    auth: none\n    models:\n"
        "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 1024\n")
    (private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
    omp = Path(shutil.which("omp") or "")
    proxy = "http://127.0.0.1:9"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TERM": "dumb", "LANG": "C.UTF-8",
           "PI_CODING_AGENT_DIR": str(agent), "PI_CONFIG_DIR": str(config),
           "XDG_CONFIG_HOME": str(private / "xdg-config"), "XDG_DATA_HOME": str(private / "xdg-data"),
           "XDG_STATE_HOME": str(private / "xdg-state"), "XDG_CACHE_HOME": str(private / "xdg-cache"),
           "Q01_CHILD_TRACE": str(private / "child-trace.jsonl"), "Q01_PROMPT_PERMIT_DIR": str(permits),
           "Q01_FIXED_WRITER": str(writer), "Q01_FIXED_PYTHON": sys.executable,
           "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    env.update({key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")})
    argv = [str(omp), "--mode", "rpc", "--no-session", "--no-title", "--no-extensions", "--no-skills",
            "--no-rules", "--no-tools", "--no-pty", "--config", str(private / "overlay.yml"),
            "--extension", str(HERE / "sdk_child_extension.ts"), "--model", "q01child/scripted", "--cwd", str(cwd)]
    version = subprocess.run([str(omp), "--version"], env=env, capture_output=True, text=True, timeout=10)
    result: dict[str, object] = {"invocation": "q01-sdk-child01", "omp_version": version.stdout.strip(),
        "omp_binary_sha256": sha(omp), "source_sha256": {p.name: sha(p) for p in (HERE / "sdk_child_extension.ts", HERE / "run_sdk_child.py", OLD_WRITER)},
        "argv": argv, "environment_keys": sorted(env), "provider_port": provider.server_port,
        "parent_rpc_sent": [], "parent_rpc_frames": [], "process": {}}
    frames: list[dict] = []
    condition = threading.Condition()
    with (OUT / "omp-stderr.log").open("wb") as stderr:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=stderr, start_new_session=True)
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
                    if isinstance(frame, dict):
                        with condition:
                            frames.append(frame)
                            condition.notify_all()

        pump_thread = threading.Thread(target=pump, daemon=True)
        pump_thread.start()
        try:
            command = {"id": "q01-child-parent-state", "type": "get_state"}
            proc.stdin.write((json.dumps(command) + "\n").encode())
            proc.stdin.flush()
            result["parent_rpc_sent"].append(command)
            deadline = time.monotonic() + 150
            trace = private / "child-trace.jsonl"
            completed = False
            while time.monotonic() < deadline and proc.poll() is None:
                if trace.exists() and '"event":"run_done"' in trace.read_text():
                    completed = True
                    break
                if provider.violation:
                    break
                with condition:
                    condition.wait(0.2)
            result["timed_out"] = not completed and not provider.violation
            result["stop_reason"] = provider.violation or ("timeout_or_parent_exit" if not completed else None)
            result["child_trace"] = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
            result["parent_rpc_frames"] = [{"type": frame.get("type"), "id": frame.get("id"),
                                            "error": frame.get("error")} for frame in frames[:200]]
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
                                 "stdout_pump_stopped": not pump_thread.is_alive(),
                                 "remaining_owned": [] if proc.poll() is not None else [proc.pid]}
            provider.shutdown()
            provider.server_close()
            provider_thread.join(2)
    result["provider_posts"] = provider.posts
    result["provider_outbound"] = provider.outbound
    result["provider_receipts"] = provider.receipts
    result["final_db_rows"] = db_rows(private / "summary.sqlite3")
    result["sentinels"] = {name: (cwd / name).read_text() for name in ("command-sentinel", "file-sentinel")}
    result["permit_files_remaining"] = sorted(p.name for p in permits.iterdir())
    trace_events: list[dict] = result["child_trace"]
    readbacks = {e["case"]: e for e in trace_events if e.get("event") == "child_readback"}
    prompt_starts = [e["case"] for e in trace_events if e.get("event") == "prompt_start"]
    admission_rejects = {e["case"]: e["reason"] for e in trace_events if e.get("event") == "admission_rejected"}
    writer_invocations = [e for e in trace_events if e.get("event") == "writer_invoked"]
    execute_saved = [e for e in trace_events if e.get("event") == "execute_saved"]
    tool_ends = [e for e in trace_events if e.get("event") == "child_event" and e.get("type") == "tool_execution_end"]
    result["checks"] = {
        "run_done": any(e.get("event") == "run_done" and e.get("ok") is True for e in trace_events),
        "all_cases_created": len(readbacks) == len(PROMPT_CASES + BLOCKED_CASES),
        "fresh_sessions": len({e["sessionId"] for e in readbacks.values()}) == len(readbacks),
        "accepted_sole_tool": all(readbacks[c]["active"] == ["db_summary_save"] for c in PROMPT_CASES),
        "only_expected_prompts": sorted(prompt_starts) == sorted(PROMPT_CASES),
        "blocked_admissions": admission_rejects == {"missing_allow": "tool_set_mismatch",
                                                    "extra_tool": "tool_set_mismatch",
                                                    "stale_session": "stale_session",
                                                    "reused_request": "request_reused"},
        "blocked_POST_zero": all(not any(p["case"] == c for p in provider.posts) for c in BLOCKED_CASES),
        "advertised_sole_tool": bool(provider.posts) and all(p["tools"] == ["db_summary_save"] for p in provider.posts),
        "no_POST_before_prompt": all(not p["pre_prompt"] for p in provider.posts),
        "exact_one_db_row": result["final_db_rows"] == [[1, SUMMARY]],
        "writer_single_use": len(writer_invocations) == 1 and writer_invocations[0]["case"] == "positive" and len(execute_saved) == 1,
        "duplicate_rejected": any(e.get("event") == "execute_rejected" and e.get("case") == "positive" and
                                  e.get("reason") == "missing_identity_or_used" for e in trace_events),
        "unrelated_calls_refused": all(any(e.get("case") == c and e.get("toolName") == c and e.get("isError") is True
                                            for e in tool_ends) for c in ("bash", "write", "edit")),
        "positive_one_success_one_rejection": len([e for e in tool_ends if e.get("case") == "positive"]) == 2 and
                                              sorted(e.get("isError") for e in tool_ends if e.get("case") == "positive") == [False, True],
        "extra_data_rejected": all(any(e.get("event") == "execute_rejected" and e.get("case") == c and
                                       e.get("reason") == "invalid_data" for e in trace_events)
                                   for c in ("inject_command", "inject_sql", "inject_path")),
        "extra_params_reached_execute": all(any(e.get("event") == "execute_received" and e.get("case") == c and
                                                set(e.get("params", {})) == {"summary", c.removeprefix("inject_")}
                                                for e in trace_events)
                                            for c in ("inject_command", "inject_sql", "inject_path")),
        "sentinels_unchanged": all(text == "ORIGINAL" for text in result["sentinels"].values()),
        "result_under_8k_utf8": len(execute_saved) == 1 and execute_saved[0]["resultBytes"] <= 8192,
        "child_sessions_disposed": len([e for e in trace_events if e.get("event") == "child_disposed"]) == len(readbacks),
        "parent_stopped": not result["process"]["remaining_owned"] and result["process"]["stdout_pump_stopped"],
        "no_forbidden_provider_post": provider.violation is None,
    }
    (OUT / "runtime-evidence.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0 if all(result["checks"].values()) and not result["stop_reason"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
