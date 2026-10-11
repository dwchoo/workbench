"""Private actual-OMP parent custom-message delivery discriminator."""
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
RECEIPT = "Q01-COMPACT-RECEIPT-" + "5a7f23c814de409d"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def run(out: Path) -> dict:
    out = out.resolve()
    if out.exists():
        raise ValueError("refuse to overwrite evidence")
    out.mkdir(parents=True, mode=0o700)
    cycles = len(sys.argv) > 2 and sys.argv[2] == "cycles"
    private = out / "private"
    for name in ("home", "agent", "config", "cwd"):
        (private / name).mkdir(parents=True, mode=0o700)
    if cycles:
        shutil.copyfile(HERE.parent / "q01_script_only_probe/fixed_writer.py", private / "fixed_writer.py")
    posts: list[dict] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 131072:
                self.send_error(413)
                return
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
            tools = [x.get("function", {}).get("name") for x in body.get("tools") or []]
            child = tools == ["db_summary_save"]
            contents = []
            for message in body.get("messages") or []:
                content = message.get("content")
                if isinstance(content, str):
                    contents.append(content)
                elif isinstance(content, list):
                    contents.extend(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text")
            record = {"path": self.path, "sha256": sha256(raw).hexdigest(),
                      "raw_bytes": len(raw), "receipt_occurrences": raw.count(RECEIPT.encode()),
                      "messages": body.get("messages"),
                      "tools_present": "tools" in body,
                      "tools": tools, "kind": "child" if child else "parent",
                      "cycle_receipts": [sum(text.count(f'"requestId":"q01-context-{n}"') for text in contents)
                                         for n in range(1, 4)],
                      "raw_markers": [raw.count(f"Q01-private-raw-marker-{n}-REMOVE-FROM-PARENT".encode()) for n in range(1, 4)]}
            with lock:
                if len(posts) >= 16:
                    self.send_error(429)
                    return
                raw_path = private / f"provider-post-{len(posts) + 1:03d}.body"
                raw_path.write_bytes(raw)
                record["raw_body_path"] = str(raw_path)
                posts.append(record)
            if child:
                tool_count = sum(x.get("role") == "tool" for x in body.get("messages") or [])
                prompt = " ".join(str(x.get("content", "")) for x in body.get("messages") or [] if x.get("role") == "user")
                cycle = next((n for n in range(1, 4) if f"q01-context-child:{n}" in prompt), 0)
                if cycle and tool_count == 0:
                    call = {"id": f"q01-context-call-{cycle}", "type": "function",
                            "function": {"name": "db_summary_save", "arguments": json.dumps(
                                {"summary": f"Q01-private-raw-marker-{cycle}-REMOVE-FROM-PARENT"})}}
                    delta = {"role": "assistant", "tool_calls": [{"index": 0, **call}]}
                    finish = "tool_calls"
                else:
                    delta = {"role": "assistant", "content": "child-done"}
                    finish = "stop"
            else:
                delta = {"role": "assistant", "content": "parent-done"}
                finish = "stop"
            frame = {"id": "context", "object": "chat.completion.chunk", "created": 0,
                     "model": "scripted", "choices": [{"index": 0,
                     "delta": delta, "finish_reason": finish}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            payload = ("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = None
    thread_started = False
    provider_closed = False

    def close_provider() -> None:
        nonlocal provider_closed
        if provider_closed:
            return
        provider_closed = True
        try:
            if thread_started:
                server.shutdown()
        finally:
            server.server_close()
            if thread_started:
                thread.join(2)

    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        thread_started = True
        (private / "agent/models.yml").write_text(
            "providers:\n  q01context:\n" + f"    baseUrl: http://127.0.0.1:{server.server_port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 1024\n")
        (private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
        omp = Path(shutil.which("omp") or "")
        if not omp.is_file():
            raise RuntimeError("installed OMP missing")
        omp = omp.resolve()
        argv = [str(omp), "--mode", "rpc", "--no-session", "--no-title", "--no-extensions",
                "--no-skills", "--no-rules"] + ([] if cycles else ["--no-tools"]) + ["--no-pty", "--config",
                str(private / "overlay.yml"), "--extension", str(HERE / "context_extension.ts"),
                "--model", "q01context/scripted", "--cwd", str(private / "cwd")]
        proxy = "http://127.0.0.1:9"
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(private / "home"),
               "TERM": "dumb", "LANG": "C.UTF-8", "PI_CODING_AGENT_DIR": str(private / "agent"),
               "PI_CONFIG_DIR": str(private / "config"), "XDG_CONFIG_HOME": str(private / "xdg-config"),
               "XDG_DATA_HOME": str(private / "xdg-data"), "XDG_STATE_HOME": str(private / "xdg-state"),
               "XDG_CACHE_HOME": str(private / "xdg-cache"), "Q01_CONTEXT_TRACE": str(private / "trace.jsonl"),
               "Q01_CONTEXT_RECEIPT": RECEIPT, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
               "PYTHONDONTWRITEBYTECODE": "1"}
        if cycles:
            env.update({"Q01_CONTEXT_MODE": "cycles", "Q01_CONTEXT_SKILL": str(HERE.parent / "q01_sdk_private_probe/summary-skill.txt"),
                        "Q01_CONTEXT_WRITER": str(private / "fixed_writer.py"), "Q01_CONTEXT_PYTHON": sys.executable})
        env.update({k: proxy for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                       "http_proxy", "https_proxy", "all_proxy")})
        version = subprocess.run([str(omp), "--version"], env=env, text=True, capture_output=True, timeout=10)
        result = {"invocation": "q01-sdk-context-delivery01", "mode": "cycles" if cycles else "discriminator",
                  "argv": argv,
                  "omp_version": version.stdout.strip(), "omp_sha256": digest(omp),
                  "source_sha256": {p.name: digest(p) for p in (HERE / "context_extension.ts", HERE / "run_context.py")},
                  "environment_keys": sorted(env), "provider_port": server.server_port,
                  "receipt": RECEIPT, "posts": [], "rpc_frames": [], "trace": [], "process": {}}
        frames: list[dict] = []
        cond = threading.Condition()
    except BaseException:
        close_provider()
        raise
    try:
        with (out / "omp-stderr.log").open("wb") as stderr:
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
                        with cond:
                            frames.append(frame)
                            cond.notify_all()

            pump_thread = threading.Thread(target=pump, daemon=True)
            pump_started = False

            def wait(predicate, timeout: float) -> dict | None:
                deadline = time.monotonic() + timeout
                with cond:
                    while True:
                        found = next((f for f in frames if predicate(f)), None)
                        if found is not None:
                            return found
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or proc.poll() is not None:
                            return None
                        cond.wait(min(remaining, 0.25))

            try:
                pump_thread.start()
                pump_started = True
                proc.stdin.write((json.dumps({"id": "ready", "type": "get_state"}) + "\n").encode())
                proc.stdin.flush()
                result["state_ready"] = wait(lambda f: f.get("id") == "ready", 15) is not None
                trace = private / "trace.jsonl"
                if not cycles:
                    until = time.monotonic() + 10
                    while time.monotonic() < until and not (trace.exists() and "sendMessage_called" in trace.read_text()):
                        time.sleep(0.05)
                if cycles:
                    result["cycles"] = []
                    for n in range(1, 4):
                        deadline = time.monotonic() + 30
                        while time.monotonic() < deadline and proc.poll() is None:
                            events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
                            if any(e.get("event") in ("cycle_receipt_sent", "cycle_failed") and e.get("cycle") == n for e in events):
                                break
                            time.sleep(0.05)
                        events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
                        sent = next((e for e in events if e.get("event") == "cycle_receipt_sent" and e.get("cycle") == n), None)
                        count_before = len(posts)
                        if not sent:
                            result["cycles"].append({"cycle": n, "receipt_sent": False, "posts_before": count_before})
                            break
                        # The public call is void; the following actual provider body is the acknowledgement.
                        time.sleep(0.2)
                        no_extra = len(posts) == count_before
                        prior_ends = sum(f.get("type") == "agent_end" for f in frames)
                        proc.stdin.write((json.dumps({"id": f"normal-{n}", "type": "prompt",
                                                     "message": f"q01-context-normal-turn-{n}"}) + "\n").encode())
                        proc.stdin.flush()
                        ended = wait(lambda f: sum(x.get("type") == "agent_end" for x in frames) > prior_ends, 25)
                        result["cycles"].append({"cycle": n, "receipt_sent": True, "no_unsolicited_post": no_extra,
                                                  "normal_ended": ended is not None, "posts_before": count_before,
                                                  "posts_after": len(posts)})
                        if ended is None:
                            break
                    time.sleep(0.3)
                else:
                    result["sent_before_prompt"] = trace.exists() and "sendMessage_called" in trace.read_text()
                    time.sleep(0.4)
                    result["posts_before_normal_prompt"] = len(posts)
                    if result["state_ready"] and result["sent_before_prompt"] and len(posts) == 0:
                        proc.stdin.write((json.dumps({"id": "normal", "type": "prompt",
                                                     "message": "q01-context-normal-turn"}) + "\n").encode())
                        proc.stdin.flush()
                        result["normal_response"] = wait(lambda f: f.get("type") == "agent_end", 25) is not None
                        time.sleep(0.4)
                    else:
                        result["normal_response"] = False
                result["rpc_frames"] = [{"id": f.get("id"), "type": f.get("type"), "error": f.get("error")}
                                        for f in frames[:80]]
                result["trace"] = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
                result["posts"] = posts.copy()
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
                result["process"].update(returncode=proc.returncode,
                                         pump_stopped=not pump_started or not pump_thread.is_alive(),
                                         remaining_owned=[] if proc.poll() is not None else [proc.pid])
    finally:
        close_provider()
    if cycles:
        parent_posts = [p for p in result["posts"] if p["kind"] == "parent"]
        child_posts = [p for p in result["posts"] if p["kind"] == "child"]
        events = result["trace"]
        receipts = [e for e in events if e.get("event") == "cycle_receipt_sent"]
        result["parent_context_deltas"] = [parent_posts[n]["raw_bytes"] - parent_posts[n-1]["raw_bytes"]
                                          for n in range(1, len(parent_posts))]
        result["parent_context"] = [{"cycle": n + 1, "request_bytes": p["raw_bytes"],
                                     "message_count": len(p["messages"] or []),
                                     "token_count": None, "tools": p["tools"]}
                                    for n, p in enumerate(parent_posts)]
        with sqlite3.connect(private / "summary.sqlite3") as db:
            result["db_rows"] = [list(row) for row in db.execute("SELECT id, summary FROM summaries ORDER BY id")]
        result["checks"] = {"three_receipts": len(receipts) == 3,
                            "three_normal_turns": len(result["cycles"]) == 3 and all(x.get("normal_ended") for x in result["cycles"]),
                            "no_unsolicited_posts": all(x.get("no_unsolicited_post") for x in result["cycles"]),
                            "parent_post_count": len(parent_posts) == 3,
                            "child_db_only": len(child_posts) == 6 and all(p["tools"] == ["db_summary_save"] for p in child_posts),
                            "parent_receipts_exact": len(parent_posts) == 3 and all(
                                parent_posts[n]["cycle_receipts"][n] == 1 for n in range(3)),
                            "parent_raw_absent": all(all(c == 0 for c in p["raw_markers"]) for p in parent_posts),
                            "parent_tools_unchanged": len(parent_posts) == 3 and
                                all(p["tools"] == parent_posts[0]["tools"] for p in parent_posts) and
                                {"read", "bash", "edit", "write"}.issubset(set(parent_posts[0]["tools"])) and
                                all(e.get("parentRosterBefore") == e.get("parentRosterAfter") and
                                    set(parent_posts[0]["tools"]).issubset(set(e.get("parentRosterBefore") or []))
                                    for e in receipts),
                            "three_exact_db_rows": result["db_rows"] ==
                                [[n, f"Q01-private-raw-marker-{n}-REMOVE-FROM-PARENT"] for n in range(1, 4)],
                            "owned_stopped": not result["process"].get("remaining_owned") and result["process"].get("pump_stopped")}
    else:
        result["checks"] = {"no_unsolicited_post": result.get("posts_before_normal_prompt") == 0,
                            "normal_parent_turn": result.get("normal_response") is True,
                            "exact_receipt_in_next_provider_post": len(result["posts"]) == 1 and
                            result["posts"][0]["receipt_occurrences"] == 1,
                            "owned_stopped": not result["process"].get("remaining_owned") and
                            result["process"].get("pump_stopped")}
    (out / ("cycles.json" if cycles else "discriminator.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    data = run(Path(sys.argv[1]))
    print(json.dumps({"checks": data["checks"], "posts": len(data["posts"])}))
    raise SystemExit(0 if all(data["checks"].values()) else 2)
