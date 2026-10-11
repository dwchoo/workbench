"""One private OMP discriminator for parent dispatch while an SDK child awaits a provider."""
from __future__ import annotations

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

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent / "q01_sdk_private_probe/summary-skill.txt"
WRITER = HERE.parent / "q01_script_only_probe/fixed_writer.py"
CHILD_SUMMARY = "Q01-private-raw-marker-1-REMOVE-FROM-PARENT"
PARENT_PROMPT = "q01-priority-normal-turn"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def text_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(item.get("text", "")) for item in value
                       if isinstance(item, dict) and item.get("type") == "text")
    return ""


def run(out: Path) -> dict:
    out = out.resolve()
    if out.exists():
        raise ValueError("refuse to overwrite evidence")
    out.mkdir(parents=True, mode=0o700)
    private = out / "private"
    for name in ("home", "agent", "config", "cwd"):
        (private / name).mkdir(parents=True, mode=0o700)
    shutil.copyfile(WRITER, private / "fixed_writer.py")
    posts: list[dict] = []
    post_lock = threading.Lock()
    held = threading.Event()
    release = threading.Event()
    parent_post = threading.Event()
    provider_state: dict = {"violation": None, "held_ns": None, "released_ns": None,
                            "hold_timed_out": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 131072:
                self.send_error(413)
                return
            raw = self.rfile.read(size)
            at = time.monotonic_ns()
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
            tools = [item.get("function", {}).get("name") for item in body.get("tools") or []]
            messages = body.get("messages") or []
            user_text = " ".join(text_content(m.get("content")) for m in messages
                                 if m.get("role") == "user")
            tool_count = sum(m.get("role") == "tool" for m in messages)
            kind = "child" if tools == ["db_summary_save"] else "parent"
            stage = "child_first" if kind == "child" and tool_count == 0 else \
                "child_second" if kind == "child" else "parent"
            with post_lock:
                raw_path = private / f"provider-post-{len(posts) + 1:03d}.body"
                raw_path.write_bytes(raw)
                record = {"kind": kind, "stage": stage, "received_ns": at,
                          "sha256": sha256(raw).hexdigest(), "bytes": size,
                          "raw_body_path": str(raw_path), "path": self.path,
                          "tools": tools, "tools_field_present": "tools" in body,
                          "roles": [m.get("role") for m in messages],
                          "raw_marker_count": raw.count(CHILD_SUMMARY.encode()),
                          "parent_prompt_present": PARENT_PROMPT in user_text}
                posts.append(record)
                if len(posts) > 3 or (kind == "child" and "q01-context-child:1" not in user_text) or \
                        (kind == "parent" and PARENT_PROMPT not in user_text):
                    provider_state["violation"] = f"unexpected_post:{stage}"
            if kind == "parent":
                parent_post.set()
            if stage == "child_first":
                provider_state["held_ns"] = time.monotonic_ns()
                held.set()
                released_by_owner = release.wait(3.0)
                provider_state["released_ns"] = time.monotonic_ns()
                provider_state["hold_timed_out"] = not released_by_owner
            if provider_state["violation"]:
                self.send_error(503)
                return
            if stage == "child_first":
                call = {"id": "priority-child-call-1", "type": "function", "function": {
                    "name": "db_summary_save", "arguments": json.dumps({"summary": CHILD_SUMMARY})}}
                delta = {"role": "assistant", "tool_calls": [{"index": 0, **call}]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "child-done" if kind == "child" else "parent-done"}
                finish = "stop"
            frame = {"id": "priority", "object": "chat.completion.chunk", "created": 0,
                     "model": "scripted", "choices": [{"index": 0, "delta": delta,
                                                   "finish_reason": finish}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            payload = ("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                record["responded_ns"] = time.monotonic_ns()
            except (BrokenPipeError, ConnectionResetError):
                record["response_disconnected_ns"] = time.monotonic_ns()

        def log_message(self, *_args: object) -> None:
            pass

    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    proc: subprocess.Popen | None = None
    pump_thread: threading.Thread | None = None
    frames: list[dict] = []
    frame_cond = threading.Condition()
    result: dict = {"invocation": "q01-sdk-priority-overlap01", "posts": posts,
                    "timeline": {}, "process": {}, "rpc_frames": [], "trace": []}
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        (private / "agent/models.yml").write_text(
            "providers:\n  q01context:\n" f"    baseUrl: http://127.0.0.1:{server.server_port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 1024\n")
        (private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
        omp_name = shutil.which("omp")
        if not omp_name:
            raise RuntimeError("installed OMP missing")
        omp = Path(omp_name).resolve()
        proxy = "http://127.0.0.1:9"
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(private / "home"),
               "TERM": "dumb", "LANG": "C.UTF-8", "PI_CODING_AGENT_DIR": str(private / "agent"),
               "PI_CONFIG_DIR": str(private / "config"), "XDG_CONFIG_HOME": str(private / "xdg-config"),
               "XDG_DATA_HOME": str(private / "xdg-data"), "XDG_STATE_HOME": str(private / "xdg-state"),
               "XDG_CACHE_HOME": str(private / "xdg-cache"), "Q01_CONTEXT_TRACE": str(private / "trace.jsonl"),
               "Q01_CONTEXT_RECEIPT": "unused", "Q01_CONTEXT_MODE": "cycles",
               "Q01_CONTEXT_SKILL": str(SKILL), "Q01_CONTEXT_WRITER": str(private / "fixed_writer.py"),
               "Q01_CONTEXT_PYTHON": sys.executable, "NO_PROXY": "127.0.0.1",
               "no_proxy": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1"}
        env.update({key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                          "http_proxy", "https_proxy", "all_proxy")})
        argv = [str(omp), "--mode", "rpc", "--no-session", "--no-title", "--no-extensions",
                "--no-skills", "--no-rules", "--no-pty", "--config", str(private / "overlay.yml"),
                "--extension", str(HERE / "context_extension.ts"), "--model", "q01context/scripted",
                "--cwd", str(private / "cwd")]
        version = subprocess.run([str(omp), "--version"], env=env, text=True,
                                 capture_output=True, timeout=10)
        result.update(omp_version=version.stdout.strip(), omp_sha256=digest(omp), argv=argv,
                      environment_keys=sorted(env), provider_port=server.server_port,
                      source_sha256={str(p): digest(p) for p in
                                     (HERE / "run_priority.py", HERE / "context_extension.ts",
                                      HERE / "run_context.py", WRITER, SKILL)})
        with (out / "omp-stdout.log").open("wb") as stdout, (out / "omp-stderr.log").open("wb") as stderr:
            proc = subprocess.Popen(argv, cwd=private / "cwd", env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
            result["process"] = {"pid": proc.pid, "pgid": proc.pid}

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
                        frame["observed_ns"] = time.monotonic_ns()
                        with frame_cond:
                            frames.append(frame)
                            frame_cond.notify_all()

            pump_thread = threading.Thread(target=pump, daemon=True)
            pump_thread.start()
            proc.stdin.write(b'{"id":"ready","type":"get_state"}\n')
            proc.stdin.flush()
            deadline = time.monotonic() + 15
            with frame_cond:
                while not any(f.get("id") == "ready" for f in frames) and time.monotonic() < deadline:
                    frame_cond.wait(min(0.2, deadline - time.monotonic()))
            result["state_ready"] = any(f.get("id") == "ready" for f in frames)
            result["held_seen"] = held.wait(10)
            if result["state_ready"] and result["held_seen"] and proc.poll() is None:
                result["timeline"]["parent_prompt_sent_ns"] = time.monotonic_ns()
                proc.stdin.write((json.dumps({"id": "priority-parent", "type": "prompt",
                                             "message": PARENT_PROMPT}) + "\n").encode())
                proc.stdin.flush()
                cutoff = time.monotonic() + 2.4
                with frame_cond:
                    while not any(f.get("type") == "agent_end" and
                                  f["observed_ns"] >= result["timeline"]["parent_prompt_sent_ns"]
                                  for f in frames) and time.monotonic() < cutoff and proc.poll() is None:
                        frame_cond.wait(min(0.1, cutoff - time.monotonic()))
            result["timeline"]["owner_release_ns"] = time.monotonic_ns()
            release.set()
            trace_path = private / "trace.jsonl"
            stop = time.monotonic() + 20
            while time.monotonic() < stop and proc.poll() is None:
                trace = [json.loads(line) for line in trace_path.read_text().splitlines()] if trace_path.exists() else []
                if any(e.get("event") in ("cycle_receipt_sent", "cycle_failed") and
                       e.get("cycle") == 1 for e in trace) and any(e.get("event") == "child_disposed" and
                                                          e.get("cycle") == 1 for e in trace):
                    break
                time.sleep(0.05)
            time.sleep(0.2)
            result["trace"] = [json.loads(line) for line in trace_path.read_text().splitlines()] if trace_path.exists() else []
            result["rpc_frames"] = [{"id": f.get("id"), "type": f.get("type"),
                                     "observed_ns": f["observed_ns"], "error": f.get("error")}
                                    for f in frames[:100]]
            result["timeline"]["parent_post_seen"] = parent_post.is_set()
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        release.set()
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(5)
            if pump_thread is not None and pump_thread.ident is not None:
                pump_thread.join(2)
            result["process"].update(returncode=proc.returncode,
                                     remaining_owned=[] if proc.poll() is not None else [proc.pid],
                                     pump_stopped=pump_thread is None or not pump_thread.is_alive())
        if server is not None:
            if server_thread is not None and server_thread.ident is not None:
                server.shutdown()
            server.server_close()
            if server_thread is not None and server_thread.ident is not None:
                server_thread.join(2)
        result["provider_stopped"] = server_thread is None or not server_thread.is_alive()
    result["provider_state"] = provider_state
    result["posts"] = posts
    db = private / "summary.sqlite3"
    if db.exists():
        with sqlite3.connect(db) as con:
            result["db_rows"] = [list(row) for row in con.execute(
                "SELECT id,summary FROM summaries ORDER BY id")]
    else:
        result["db_rows"] = []
    parent = [p for p in posts if p["kind"] == "parent"]
    child = [p for p in posts if p["kind"] == "child"]
    end = next((f for f in result["rpc_frames"] if f["type"] == "agent_end" and
                f["observed_ns"] >= result["timeline"].get("parent_prompt_sent_ns", 2**63)), None)
    receipt = [e for e in result["trace"] if e.get("event") == "cycle_receipt_sent" and e.get("cycle") == 1]
    disposed = [e for e in result["trace"] if e.get("event") == "child_disposed" and e.get("cycle") == 1]
    result["timeline"].update(child_held_ns=provider_state["held_ns"],
                              child_released_ns=provider_state["released_ns"],
                              parent_post_ns=parent[0]["received_ns"] if len(parent) == 1 else None,
                              parent_agent_end_ns=end["observed_ns"] if end else None)
    result["checks"] = {
        "ready_and_held": result.get("state_ready") is True and result.get("held_seen") is True,
        "parent_post_before_child_release": len(parent) == 1 and provider_state["released_ns"] is not None and
            parent[0]["received_ns"] < provider_state["released_ns"],
        "parent_agent_end_before_child_release": end is not None and provider_state["released_ns"] is not None and
            end["observed_ns"] < provider_state["released_ns"],
        "parent_normal_tools": len(parent) == 1 and parent[0]["tools_field_present"] and
            {"read", "bash", "edit", "write"}.issubset(set(parent[0]["tools"])),
        "child_db_only_two_posts": len(child) == 2 and all(p["tools"] == ["db_summary_save"] for p in child),
        "exact_three_posts_no_extra_cycle": len(posts) == 3 and
            len([e for e in result["trace"] if e.get("event") == "child_prompt"]) == 1,
        "child_saved_receipt_disposed": result["db_rows"] == [[1, CHILD_SUMMARY]] and
            len(receipt) == 1 and receipt[0].get("bytes", 999999) <= 4096 and len(disposed) == 1,
        "no_private_raw_in_parent": len(parent) == 1 and parent[0]["raw_marker_count"] == 0,
        "raw_post_sha": all(digest(Path(p["raw_body_path"])) == p["sha256"] for p in posts),
        "hold_bounded_owner_released": provider_state["hold_timed_out"] is False and
            provider_state["released_ns"] is not None and provider_state["held_ns"] is not None and
            provider_state["released_ns"] - provider_state["held_ns"] <= 3_000_000_000,
        "owned_stopped": result["provider_stopped"] and not result["process"].get("remaining_owned") and
            result["process"].get("pump_stopped") is True,
    }
    result["status"] = "candidate_ready" if all(result["checks"].values()) and not result.get("error") and \
        not provider_state["violation"] else "needs_reroute"
    (out / "priority-evidence.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    value = run(Path(sys.argv[1]))
    print(json.dumps({"status": value["status"], "checks": value["checks"],
                      "timeline": value["timeline"]}, ensure_ascii=False))
    raise SystemExit(0 if value["status"] == "candidate_ready" else 2)
