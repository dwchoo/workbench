"""One credential-free installed OMP parent for SDK roster and context feasibility."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import threading
import time


HERE = Path(__file__).resolve().parent
OUT = Path(sys.argv[1]).resolve()
AGENT_PROMPT = Path(sys.argv[2]).resolve()
SKILL = HERE / "summary-skill.txt"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(part.get("text", "")) for part in value
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


class Provider(ThreadingHTTPServer):
    def __init__(self, private: Path):
        self.private = private
        self.posts: list[dict[str, object]] = []
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
                system = "\n".join(text_content(msg.get("content")) for msg in messages
                                   if isinstance(msg, dict) and msg.get("role") == "system")
                users = [text_content(msg.get("content")) for msg in messages
                         if isinstance(msg, dict) and msg.get("role") == "user"]
                case = "context" if users and "q01-case:context" in users[-1] else "unknown"
                tools = [tool.get("function", {}).get("name") for tool in request.get("tools") or []]
                pre_prompt = not (private / "permits/context").exists()
                post = {"case": case, "path": inner.path, "model": request.get("model"),
                        "tools": tools, "roles": [msg.get("role") for msg in messages if isinstance(msg, dict)],
                        "system_sha256": hashlib.sha256(system.encode()).hexdigest(),
                        "system_text": system, "request_sha256": hashlib.sha256(raw).hexdigest(),
                        "pre_prompt": pre_prompt}
                with self.lock:
                    self.posts.append(post)
                    if pre_prompt or case != "context" or tools != ["db_summary_save"]:
                        self.violation = f"forbidden_POST:case={case}:pre={pre_prompt}:tools={tools}"
                    violation = self.violation
                if violation:
                    inner.send_response(503)
                    inner.end_headers()
                    return
                frames = [
                    {"id": "q01guard", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "context-ok"}, "finish_reason": None}]},
                    {"id": "q01guard", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                ]
                body = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
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
    private.mkdir(mode=0o700)
    home, agent, config, cwd, permits = (private / name for name in ("home", "agent", "config", "cwd", "permits"))
    for directory in (home, agent, config, cwd, permits):
        directory.mkdir(mode=0o700)
    for name in ("command-sentinel", "file-sentinel"):
        (cwd / name).write_text("ORIGINAL")
    provider = Provider(private)
    server_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    server_thread.start()
    (agent / "models.yml").write_text(
        "providers:\n  q01guard:\n" f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
        "    api: openai-completions\n    auth: none\n    models:\n"
        "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 1024\n"
        "  q01guardresp:\n" f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
        "    api: openai-responses\n    auth: none\n    models:\n"
        "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 1024\n")
    (private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
    omp = Path(shutil.which("omp") or "")
    proxy = "http://127.0.0.1:9"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TERM": "dumb", "LANG": "C.UTF-8",
           "PI_CODING_AGENT_DIR": str(agent), "PI_CONFIG_DIR": str(config),
           "XDG_CONFIG_HOME": str(private / "xdg-config"), "XDG_DATA_HOME": str(private / "xdg-data"),
           "XDG_STATE_HOME": str(private / "xdg-state"), "XDG_CACHE_HOME": str(private / "xdg-cache"),
           "Q01_DISPATCH_TRACE": str(private / "trace.jsonl"), "Q01_DISPATCH_PERMITS": str(permits),
           "Q01_AGENT_PROMPT": str(AGENT_PROMPT), "Q01_SKILL_BYTES": str(SKILL),
           "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1"}
    env.update({key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")})
    argv = [str(omp), "--mode", "rpc", "--no-session", "--no-title", "--no-extensions", "--no-skills",
            "--no-rules", "--no-tools", "--no-pty", "--config", str(private / "overlay.yml"),
            "--extension", str(HERE / "dispatch_extension.ts"), "--model", "q01guard/scripted", "--cwd", str(cwd)]
    version = subprocess.run([str(omp), "--version"], env=env, capture_output=True, text=True, timeout=10)
    result: dict[str, object] = {
        "invocation": "q01-sdk-dispatch01", "omp_version": version.stdout.strip(), "omp_binary_sha256": sha(omp),
        "argv": argv, "environment_keys": sorted(env), "provider_port": provider.server_port,
        "source_sha256": {path.name: sha(path) for path in (HERE / "dispatch_extension.ts", HERE / "run_dispatch_probe.py", SKILL, AGENT_PROMPT)},
        "parent_rpc_sent": [], "parent_rpc_frames": [], "process": {},
    }
    frames: list[dict] = []
    condition = threading.Condition()
    try:
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
                command = {"id": "q01-dispatch-parent-state", "type": "get_state"}
                proc.stdin.write((json.dumps(command) + "\n").encode())
                proc.stdin.flush()
                result["parent_rpc_sent"].append(command)
                deadline = time.monotonic() + 90
                trace = private / "trace.jsonl"
                complete = False
                while time.monotonic() < deadline and proc.poll() is None:
                    if trace.exists() and '"event":"run_done"' in trace.read_text():
                        complete = True
                        break
                    if provider.violation:
                        break
                    with condition:
                        condition.wait(0.2)
                result["timed_out"] = not complete and not provider.violation
                result["stop_reason"] = provider.violation or ("timeout_or_parent_exit" if not complete else None)
                result["trace"] = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
                result["parent_rpc_frames"] = [{"type": frame.get("type"), "id": frame.get("id"),
                                                "error": frame.get("error")} for frame in frames[:100]]
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
    finally:
        provider.shutdown()
        provider.server_close()
        server_thread.join(2)
    result["provider_posts"] = provider.posts
    result["provider_violation"] = provider.violation
    result["sentinels"] = {name: (cwd / name).read_text() for name in ("command-sentinel", "file-sentinel")}
    result["permit_files_remaining"] = sorted(path.name for path in permits.iterdir())
    trace_events = result.get("trace", [])
    guard_results = [event for event in trace_events if event.get("event") == "guard_result"]
    run_done = [event for event in trace_events if event.get("event") == "run_done"]
    result["checks"] = {
        "one_guard_result": len(guard_results) == 1,
        "guard_expansion_observed": bool(guard_results and guard_results[0].get("kind") == "think_expanded"),
        "run_done": bool(run_done and run_done[-1].get("ok") is True),
        "provider_POST_zero": provider.posts == [],
        "sentinels_unchanged": result["sentinels"] == {"command-sentinel": "ORIGINAL", "file-sentinel": "ORIGINAL"},
        "child_disposed": any(event.get("event") == "guard_disposed" for event in trace_events),
        "parent_stopped": result["process"]["remaining_owned"] == [] and result["process"]["stdout_pump_stopped"],
    }
    (OUT / "runtime-evidence.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0 if not result["timed_out"] and not provider.violation and all(result["checks"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
