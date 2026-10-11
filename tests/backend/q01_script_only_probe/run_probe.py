"""Credential-free real OMP 18.8.6 scripted-provider probe for one fixed DB writer."""

from __future__ import annotations

import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
OUT = Path(sys.argv[1]).resolve()
OMP = Path(shutil.which("omp") or "")
SUMMARY = "literal: touch /tmp/q01-should-not-run; DROP TABLE summaries; -- data only"
CASES = ["positive", "bash", "write", "edit", "inject_command", "inject_sql", "inject_path", "inject_extra"]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(db: Path) -> list[list[object]]:
    if not db.exists():
        return []
    with sqlite3.connect(db) as con:
        tables = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='summaries'").fetchall()
        return [list(row) for row in con.execute("SELECT id, summary FROM summaries ORDER BY id")] if tables else []


class Model:
    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.posts: list[dict[str, object]] = []
        self.receipts: dict[str, list[str]] = {}
        self.outbound_calls: list[dict[str, object]] = []
        self.response_hashes: list[dict[str, str]] = []
        self.called: set[str] = set()
        self.lock = threading.Lock()

    def respond(self, request: dict) -> tuple[list[dict], str]:
        with self.lock:
            messages = request.get("messages") or []
            tools = [item.get("function", {}).get("name") for item in request.get("tools") or []]
            users = [m for m in messages if m.get("role") == "user"]
            raw = users[-1].get("content", "") if users else ""
            if isinstance(raw, list):
                case_text = " ".join(str(x.get("text", "")) for x in raw if isinstance(x, dict))
            else:
                case_text = str(raw)
            case = next((x for x in CASES if f"q01-case:{x}" in case_text), "unknown")
            self.posts.append({"case": case, "tools": tools, "path": "/v1/chat/completions", "model": request.get("model")})
            tool_messages = [m for m in messages if m.get("role") == "tool"]
            if tool_messages:
                self.receipts[case] = [str(m.get("content"))[:500] for m in tool_messages]
            if case in self.called or case == "unknown":
                return [], case
            self.called.add(case)
            args: dict[str, object] = {"summary": SUMMARY}
            name = "db_summary_save"
            if case == "bash":
                name, args = "bash", {"command": f"printf CHANGED > {self.cwd / 'command-sentinel'}"}
            elif case == "write":
                name, args = "write", {"path": str(self.cwd / "file-sentinel"), "content": "CHANGED"}
            elif case == "edit":
                name, args = "edit", {"path": str(self.cwd / "file-sentinel"), "oldText": "ORIGINAL", "newText": "CHANGED"}
            elif case.startswith("inject_"):
                extra = case.removeprefix("inject_")
                args[extra] = {"command": f"printf CHANGED > {self.cwd / 'command-sentinel'}",
                               "sql": "DROP TABLE summaries", "path": str(self.cwd / "file-sentinel"),
                               "extra": "unexpected"}[extra]
            call = {"id": f"q01-{case}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            self.outbound_calls.append({"case": case, "call": call})
            return [call], case


def make_server(model: Model) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls, case = model.respond(json.loads(raw))
            delta = {"role": "assistant", "tool_calls": [{"index": i, **c} for i, c in enumerate(calls)]} if calls else {"role": "assistant", "content": "done"}
            finish = "tool_calls" if calls else "stop"
            frames = [
                {"id": "q01", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "q01", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            body = b"".join(f"data: {json.dumps(f)}\n\n".encode() for f in frames) + b"data: [DONE]\n\n"
            with model.lock:
                model.response_hashes.append({"case": case, "sha256": hashlib.sha256(body).hexdigest()})
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args: object) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = OUT / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    writer = private / "fixed_writer.py"
    shutil.copyfile(HERE / "fixed_writer.py", writer)
    cwd = private / "cwd"
    cwd.mkdir()
    (cwd / "file-sentinel").write_text("ORIGINAL")
    (cwd / "command-sentinel").write_text("ORIGINAL")
    sentinels = {name: digest(cwd / name) for name in ("file-sentinel", "command-sentinel")}
    agent, config = private / "agent", private / "config"
    agent.mkdir()
    config.mkdir()
    model = Model(cwd)
    server = make_server(model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (agent / "models.yml").write_text(
        "providers:\n  q01loop:\n" f"    baseUrl: http://127.0.0.1:{server.server_port}/v1\n"
        "    api: openai-completions\n    auth: none\n    models:\n"
        "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 1024\n")
    (private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
    exe = str(OMP)
    argv = [exe, "--mode", "rpc", "--no-session", "--no-title", "--no-extensions", "--no-skills",
            "--no-rules", "--no-tools", "--no-pty", "--approval-mode=yolo", "--config",
            str(private / "overlay.yml"), "--extension", str(HERE / "script_only_extension.ts"),
            "--model", "q01loop/scripted", "--cwd", str(cwd)]
    proxy = "http://127.0.0.1:9"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(private), "TERM": "dumb", "LANG": "C.UTF-8",
           "PI_CODING_AGENT_DIR": str(agent), "PI_CONFIG_DIR": str(config), "XDG_CONFIG_HOME": str(private / "xdg-config"),
           "XDG_DATA_HOME": str(private / "xdg-data"), "XDG_STATE_HOME": str(private / "xdg-state"),
           "XDG_CACHE_HOME": str(private / "xdg-cache"), "Q01_FIXED_WRITER": str(writer),
           "Q01_FIXED_PYTHON": sys.executable, "Q01_TRACE_PATH": str(private / "execute-trace.jsonl"),
           "PYTHONDONTWRITEBYTECODE": "1",
           "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    env.update({key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")})
    version = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10, env=env)
    result: dict[str, object] = {"invocation": "q01-script-only-fix02", "omp_version": version.stdout.strip(),
        "omp_sha256": digest(OMP), "python": sys.version.split()[0], "argv": argv,
        "environment_keys": sorted(env), "source_sha256": {p.name: digest(p) for p in (HERE / "fixed_writer.py", HERE / "script_only_extension.ts")},
        "provider_port": server.server_port, "cases": {}, "process": {}}
    frames: list[dict] = []
    cond = threading.Condition()
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
                    with cond:
                        frames.append(frame)
                        cond.notify_all()

        pump_thread = threading.Thread(target=pump, daemon=True)
        pump_thread.start()

        def wait(predicate, after: int, timeout: float = 30.0):
            deadline = time.monotonic() + timeout
            with cond:
                while True:
                    found = next((f for f in frames[after:] if predicate(f)), None)
                    if found is not None:
                        return found
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or proc.poll() is not None:
                        return None
                    cond.wait(min(remaining, 0.25))

        try:
            proc.stdin.write((json.dumps({"id": "state", "type": "get_state"}) + "\n").encode())
            proc.stdin.flush()
            state = wait(lambda f: f.get("type") == "response" and f.get("id") == "state", 0)
            result["state_response"] = state
            if state is None:
                result["stop_reason"] = "no_state_response"
            else:
                for case in CASES:
                    start = len(frames)
                    proc.stdin.write((json.dumps({"id": case, "type": "prompt", "message": f"q01-case:{case}"}) + "\n").encode())
                    proc.stdin.flush()
                    ended = wait(lambda f: f.get("type") == "agent_end", start)
                    span = frames[start:]
                    ends = [{"name": f.get("toolName"), "id": f.get("toolCallId"), "isError": f.get("isError"),
                             "result": f.get("result")} for f in span if f.get("type") == "tool_execution_end"]
                    posts = [p for p in model.posts if p["case"] == case]
                    unchanged = {name: digest(cwd / name) == before for name, before in sentinels.items()}
                    result["cases"][case] = {"agent_end": ended is not None, "posts": posts, "tool_ends": ends,
                        "provider_receipts": model.receipts.get(case, []), "db_rows": rows(private / "summary.sqlite3"),
                        "sentinels_unchanged": unchanged}
                    if not ended:
                        result["stop_reason"] = f"timeout_or_exit:{case}"
                        break
                    if not all(unchanged.values()) or any(x["name"] in {"bash", "write", "edit"} and not x["isError"] for x in ends):
                        result["stop_reason"] = f"uncontained_execution:{case}"
                        break
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
            result["process"] = {"pid": proc.pid, "returncode": proc.returncode, "stdout_pump_stopped": not pump_thread.is_alive(),
                                 "remaining_owned": [] if proc.poll() is not None else [proc.pid]}
            server.shutdown()
            server.server_close()
            thread.join(2)
    result["all_provider_posts"] = model.posts
    result["outbound_calls"] = model.outbound_calls
    result["response_body_sha256"] = model.response_hashes
    trace = private / "execute-trace.jsonl"
    result["execute_trace"] = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    result["final_db_rows"] = rows(private / "summary.sqlite3")
    cases = result["cases"]
    trace_events = result["execute_trace"]
    result["checks"] = {
        "all_eight_completed": len(cases) == len(CASES) and all(c["agent_end"] for c in cases.values()),
        "only_dedicated_advertised": bool(model.posts) and all(p["tools"] == ["db_summary_save"] for p in model.posts),
        "exact_one_expected_row": result["final_db_rows"] == [[1, SUMMARY]],
        "positive_saved": len(cases.get("positive", {}).get("db_rows", [])) == 1,
        "unrelated_rejected": all(len(cases.get(n, {}).get("tool_ends", [])) == 1 and
                                  cases[n]["tool_ends"][0]["isError"] is True for n in ("bash", "write", "edit")),
        "injections_rejected": all(len(cases.get(n, {}).get("tool_ends", [])) == 1 and
                                   cases[n]["tool_ends"][0]["isError"] is True for n in CASES[4:]),
        "all_sentinels_unchanged": len(cases) == len(CASES) and
                                   all(all(c["sentinels_unchanged"].values()) for c in cases.values()),
        "raw_calls_captured": len(model.outbound_calls) == len(CASES) and
                              len(model.response_hashes) >= len(CASES),
        "execute_params_captured": len([e for e in trace_events if e["event"] == "received"]) == 5 and
                                   len([e for e in trace_events if e["event"] == "writer_invoked"]) == 1 and
                                   len([e for e in trace_events if e["event"] == "rejected"]) == 4,
        "process_stopped": not result["process"]["remaining_owned"] and result["process"]["stdout_pump_stopped"],
    }
    (OUT / "runtime-evidence.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n")
    return 0 if not result.get("stop_reason") and all(result["checks"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
