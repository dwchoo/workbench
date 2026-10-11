"""One private interactive OMP editor/busy admission discriminator."""
from __future__ import annotations

import fcntl
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import termios
import threading
import time

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent / "q01_sdk_private_probe/summary-skill.txt"
WRITER = HERE.parent / "q01_script_only_probe/fixed_writer.py"
DRAFT = "Q01-UNSENT-DRAFT"
NORMAL = "Q01-priority-input-normal"
SUMMARY = "Q01-private-raw-marker-1-REMOVE-FROM-PARENT"


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
    control = private / "control.txt"
    control.write_text("0")
    posts: list[dict] = []
    post_lock = threading.Lock()
    main_held = threading.Event()
    release_main = threading.Event()
    provider_state: dict = {"violation": None, "main_held_ns": None,
                            "main_released_ns": None, "main_hold_timed_out": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 131072:
                self.send_error(413)
                return
            raw = self.rfile.read(size)
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
            tools = [item.get("function", {}).get("name") for item in body.get("tools") or []]
            messages = body.get("messages") or []
            user_text = " ".join(text_content(m.get("content")) for m in messages
                                 if m.get("role") == "user")
            kind = "child" if tools == ["db_summary_save"] else "parent"
            tool_count = sum(message.get("role") == "tool" for message in messages)
            stage = "parent" if kind == "parent" else "child_first" if tool_count == 0 else "child_second"
            with post_lock:
                raw_path = private / f"provider-post-{len(posts) + 1:03d}.body"
                raw_path.write_bytes(raw)
                record = {"kind": kind, "stage": stage, "received_ns": time.monotonic_ns(),
                          "path": self.path, "sha256": sha256(raw).hexdigest(), "bytes": len(raw),
                          "raw_body_path": str(raw_path), "tools": tools,
                          "tools_field_present": "tools" in body,
                          "roles": [m.get("role") for m in messages],
                          "normal_prompt_present": NORMAL in user_text,
                          "draft_in_parent": DRAFT in user_text if kind == "parent" else None,
                          "raw_marker_count": raw.count(SUMMARY.encode())}
                posts.append(record)
                if len(posts) > 3 or (kind == "parent" and (NORMAL not in user_text or DRAFT in user_text)) or \
                        (kind == "child" and "q01-context-child:1" not in user_text):
                    provider_state["violation"] = f"unexpected_post:{stage}"
            if stage == "parent":
                provider_state["main_held_ns"] = time.monotonic_ns()
                main_held.set()
                released = release_main.wait(3.0)
                provider_state["main_released_ns"] = time.monotonic_ns()
                provider_state["main_hold_timed_out"] = not released
            if provider_state["violation"]:
                self.send_error(503)
                return
            if stage == "child_first":
                call = {"id": "priority-input-child-call", "type": "function", "function": {
                    "name": "db_summary_save", "arguments": json.dumps({"summary": SUMMARY})}}
                delta = {"role": "assistant", "tool_calls": [{"index": 0, **call}]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "parent-done" if kind == "parent" else "child-done"}
                finish = "stop"
            frame = {"id": "priority-input", "object": "chat.completion.chunk", "created": 0,
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
                record["disconnected_ns"] = time.monotonic_ns()

        def log_message(self, *_args: object) -> None:
            pass

    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    proc: subprocess.Popen | None = None
    master: int | None = None
    drain_thread: threading.Thread | None = None
    drain_stop = threading.Event()
    result: dict = {"invocation": "q01-sdk-priority-input01", "posts": posts,
                    "signals": [], "process": {}, "provider_state": provider_state}
    priority_trace = private / "priority-trace.jsonl"
    context_trace = private / "context-trace.jsonl"

    def read_events(path: Path) -> list[dict]:
        if not path.exists():
            return []
        events = []
        for line in path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:  # A concurrent append may leave one incomplete final line.
                pass
        return events

    def wait_event(path: Path, name: str, timeout: float, **match: object) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and (proc is None or proc.poll() is None):
            found = next((event for event in read_events(path)
                          if event.get("event") == name and
                          all(event.get(key) == value for key, value in match.items())), None)
            if found is not None:
                return found
            time.sleep(0.025)
        return None

    def signal_attempt(number: int) -> None:
        result["signals"].append({"attempt": number, "sent_ns": time.monotonic_ns()})
        control.write_text(str(number))

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
        setsid_name = shutil.which("setsid")
        if not omp_name or not setsid_name:
            raise RuntimeError("installed omp or setsid missing")
        omp = Path(omp_name).resolve()
        proxy = "http://127.0.0.1:9"
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(private / "home"),
               "TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "C.UTF-8",
               "PI_CODING_AGENT_DIR": str(private / "agent"), "PI_CONFIG_DIR": str(private / "config"),
               "XDG_CONFIG_HOME": str(private / "xdg-config"), "XDG_DATA_HOME": str(private / "xdg-data"),
               "XDG_STATE_HOME": str(private / "xdg-state"), "XDG_CACHE_HOME": str(private / "xdg-cache"),
               "Q01_CONTEXT_TRACE": str(context_trace), "Q01_PRIORITY_TRACE": str(priority_trace),
               "Q01_PRIORITY_CONTROL": str(control), "Q01_CONTEXT_MODE": "cycles",
               "Q01_CONTEXT_RECEIPT": "unused", "Q01_CONTEXT_SKILL": str(SKILL),
               "Q01_CONTEXT_WRITER": str(private / "fixed_writer.py"),
               "Q01_CONTEXT_PYTHON": sys.executable, "NO_PROXY": "127.0.0.1",
               "no_proxy": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1"}
        env.update({key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                          "http_proxy", "https_proxy", "all_proxy")})
        argv = [str(setsid_name), "--ctty", str(omp), "--no-session", "--no-title",
                "--no-extensions", "--no-skills", "--no-rules", "--no-pty", "--config",
                str(private / "overlay.yml"), "--extension", str(HERE / "priority_input_extension.ts"),
                "--model", "q01context/scripted", "--cwd", str(private / "cwd")]
        version = subprocess.run([str(omp), "--version"], env=env, text=True,
                                 capture_output=True, timeout=10)
        result.update(argv=argv, omp_version=version.stdout.strip(), omp_sha256=digest(omp),
                      environment_keys=sorted(env), provider_port=server.server_port,
                      source_sha256={str(p): digest(p) for p in
                                     (HERE / "priority_input_extension.ts", HERE / "run_priority_input.py",
                                      HERE / "context_extension.ts", HERE / "run_context.py", WRITER, SKILL)})
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        try:
            proc = subprocess.Popen(argv, cwd=private / "cwd", env=env, stdin=slave,
                                    stdout=slave, stderr=slave, close_fds=True)
        finally:
            os.close(slave)
        result["process"] = {"pid": proc.pid, "pgid": proc.pid}
        os.set_blocking(master, False)

        def drain() -> None:
            with (out / "pty-output.raw").open("wb") as output:
                while not drain_stop.is_set():
                    try:
                        if select.select([master], [], [], 0.1)[0]:
                            data = os.read(master, 65536)
                            if not data:
                                return
                            output.write(data)
                    except OSError:
                        return

        drain_thread = threading.Thread(target=drain, daemon=True)
        drain_thread.start()
        result["interactive_session"] = wait_event(priority_trace, "interactive_session", 12)
        if result["interactive_session"] is None:
            raise RuntimeError("interactive_session_not_observed")
        os.write(master, DRAFT.encode())
        time.sleep(0.25)
        signal_attempt(1)
        result["draft_attempt"] = wait_event(priority_trace, "admission_attempt", 3, attempt=1)
        if result["draft_attempt"] is None:
            raise RuntimeError("draft_attempt_not_observed")
        os.write(master, b"\x15")  # Real TUI Ctrl-U clears the unsent editor line.
        time.sleep(0.15)
        os.write(master, NORMAL.encode() + b"\r")
        result["main_held_seen"] = main_held.wait(8)
        if not result["main_held_seen"]:
            raise RuntimeError("main_provider_hold_not_observed")
        signal_attempt(2)
        result["busy_attempt"] = wait_event(priority_trace, "admission_attempt", 2, attempt=2)
        if result["busy_attempt"] is None:
            raise RuntimeError("busy_attempt_not_observed")
        release_main.set()
        result["main_end"] = wait_event(priority_trace, "main_agent_end", 8)
        if result["main_end"] is None:
            raise RuntimeError("main_agent_end_not_observed")
        time.sleep(0.15)
        signal_attempt(3)
        result["ready_attempt"] = wait_event(priority_trace, "admission_attempt", 3, attempt=3)
        result["callback_invoked"] = wait_event(priority_trace, "captured_callback_invoked", 3, attempt=3)
        result["receipt_sent"] = wait_event(context_trace, "cycle_receipt_sent", 12, cycle=1)
        result["child_disposed"] = wait_event(context_trace, "child_disposed", 3, cycle=1)
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        release_main.set()
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
            result["process"].update(returncode=proc.returncode,
                                     remaining_owned=[] if proc.poll() is not None else [proc.pid])
        drain_stop.set()
        if drain_thread is not None and drain_thread.ident is not None:
            drain_thread.join(2)
        result["process"]["pty_drain_stopped"] = drain_thread is None or not drain_thread.is_alive()
        if master is not None:
            os.close(master)
        if server is not None:
            if server_thread is not None and server_thread.ident is not None:
                server.shutdown()
            server.server_close()
            if server_thread is not None and server_thread.ident is not None:
                server_thread.join(2)
        result["provider_stopped"] = server_thread is None or not server_thread.is_alive()
    result["priority_trace"] = read_events(priority_trace)
    result["context_trace"] = read_events(context_trace)
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
    attempts = {event.get("attempt"): event for event in result["priority_trace"]
                if event.get("event") == "admission_attempt"}
    first = attempts.get(1, {})
    second = attempts.get(2, {})
    third = attempts.get(3, {})
    interactive = result.get("interactive_session") or {}
    signals = {item["attempt"]: item["sent_ns"] for item in result["signals"]}
    result["checks"] = {
        "actual_tui_public_api": interactive.get("mode") == "tui" and
            interactive.get("hasUI") is True and
            all(interactive.get(key) == "function" for key in
                ("editorGetter", "idleGetter", "pendingGetter")),
        "actual_draft_deferred": first.get("editor") == DRAFT and first.get("ready") is False and
            any(e.get("event") == "deferred" and e.get("attempt") == 1 and e.get("reason") == "draft"
                for e in result["priority_trace"]),
        "actual_busy_deferred": second.get("editor") == "" and second.get("idle") is False and
            second.get("ready") is False and provider_state["main_held_ns"] is not None and
            any(e.get("event") == "deferred" and e.get("attempt") == 2 and e.get("reason") == "busy"
                for e in result["priority_trace"]),
        "no_child_before_clear_idle": len([e for e in result["priority_trace"]
                                           if e.get("event") == "captured_callback_invoked"]) == 1 and
            len(child) == 2 and len([e for e in result["context_trace"]
                                     if e.get("event") == "child_prompt"]) == 1 and
            child[0]["received_ns"] > signals.get(3, 2**63),
        "actual_ready_one_callback": third.get("editor") == "" and third.get("idle") is True and
            third.get("pending") is False and third.get("ready") is True and
            result.get("callback_invoked") is not None,
        "parent_normal_one_post": len(parent) == 1 and parent[0]["normal_prompt_present"] and
            parent[0]["draft_in_parent"] is False and
            {"read", "bash", "edit", "write"}.issubset(set(parent[0]["tools"])),
        "child_db_only_two_posts": len(child) == 2 and all(p["tools"] == ["db_summary_save"] for p in child),
        "exact_three_posts": len(posts) == 3 and provider_state["violation"] is None,
        "child_save_receipt_dispose": result["db_rows"] == [[1, SUMMARY]] and
            result.get("receipt_sent") is not None and result["receipt_sent"].get("bytes", 999999) <= 4096 and
            result.get("child_disposed") is not None,
        "raw_post_sha": all(digest(Path(p["raw_body_path"])) == p["sha256"] for p in posts),
        "main_hold_bounded": result.get("main_held_seen") is True and
            provider_state["main_hold_timed_out"] is False and
            provider_state["main_released_ns"] is not None and provider_state["main_held_ns"] is not None and
            provider_state["main_released_ns"] - provider_state["main_held_ns"] <= 3_000_000_000,
        "owned_stopped": result["provider_stopped"] and
            result["process"].get("pty_drain_stopped") is True and
            not result["process"].get("remaining_owned"),
    }
    result["status"] = "candidate_ready" if all(result["checks"].values()) and not result.get("error") else \
        "needs_reroute"
    (out / "priority-input-evidence.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    value = run(Path(sys.argv[1]))
    print(json.dumps({"status": value["status"], "checks": value["checks"],
                      "error": value.get("error"), "posts": len(value["posts"])}, ensure_ascii=False))
    raise SystemExit(0 if value["status"] == "candidate_ready" else 2)
