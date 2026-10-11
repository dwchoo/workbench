"""Private installed-OMP public budget API discriminator."""
from __future__ import annotations

from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from tests.backend.q01_sdk_full_probe.full_probe import _request
from workbench.contracts.logs_v1 import parse_summary_request, parse_summary_result, summary_input_key
from workbench.tasks.repository import TaskRepository

HERE = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def run(out: Path) -> dict:
    out = out.resolve()
    if out.exists():
        raise ValueError("refuse to overwrite evidence")
    out.mkdir(parents=True, mode=0o700)
    full = len(sys.argv) > 2 and sys.argv[2] == "full"
    private = out / "private"
    for name in ("home", "agent", "config", "cwd"):
        (private / name).mkdir(parents=True, mode=0o700)
    if full:
        shutil.copyfile(HERE.parent / "q01_script_only_probe/fixed_writer.py", private / "fixed_writer.py")
        pieces = []
        for kind, source, letter in (("cumulative", "cumulative", "C"),
                                     ("start_spec", "start-spec", "S"),
                                     ("decision", "decisions", "D"),
                                     ("report", "reports", "R")):
            pieces.append({"source": source, "revision": 1, "kind": kind, "start": 0,
                           "end": 120, "text": letter * 120, "sequence": 1})
        remaining = 1048576 - 480 - 120
        start = 0
        sequence = 1
        while remaining:
            size = min(4096, remaining)
            pieces.append({"source": "terminal", "revision": 1, "kind": "terminal",
                           "start": start, "end": start + size, "text": "T" * size, "sequence": sequence})
            start += size
            sequence += 1
            remaining -= size
        pieces.append({"source": "terminal", "revision": 1, "kind": "terminal",
                       "start": start, "end": start + 120, "text": "N" * 120, "sequence": sequence})
        (private / "segments.json").write_text(json.dumps(pieces, ensure_ascii=False))
        repo = TaskRepository(private / "backend.sqlite3")
        try:
            skill_sha = digest(HERE.parent / "q01_sdk_private_probe/summary-skill.txt")
            request, seed = _request(repo, "Q01 budget synthetic summary", skill_sha)
            task_id = request["subject"]["id"]
            bases = (("event", "eventId", "C" * 120),
                     ("task_spec", "messageId", "S" * 120),
                     ("decision", "messageId", "D" * 120))
            spans = []
            for kind, id_key, source_text in bases:
                source = {"schemaVersion": 1, "sourceKind": kind, "collectorEpoch": 1,
                          "taskId": task_id, "revision": 1, id_key: str(uuid4())}
                spans.append({"schemaVersion": 1, "source": source, "start": 0,
                              "end": len(source_text), "digest": sha256(source_text.encode()).hexdigest()})
            request = {**request, "sources": spans,
                       "inputKey": summary_input_key(request["subject"], 1, spans, [], skill_sha)}
            request = parse_summary_request(request)
            repo.reserve_summary_request(request)
            repo.record_summary_state(request["requestId"], "sending")
            (private / "request.json").write_text(json.dumps(request, ensure_ascii=False))
        finally:
            repo.close()
    posts: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            if size > (32768 if full else 131072):
                self.send_error(413)
                return
            raw = self.rfile.read(size)
            body = json.loads(raw)
            names = [x.get("function", {}).get("name") for x in body.get("tools") or []]
            raw_path = private / f"provider-post-{len(posts) + 1:03d}.body"
            raw_path.write_bytes(raw)
            posts.append({"path": self.path, "sha256": sha256(raw).hexdigest(), "bytes": len(raw),
                          "raw_body_path": str(raw_path), "tools": names,
                          "max_completion_tokens": body.get("max_completion_tokens"),
                          "max_tokens": body.get("max_tokens"),
                          "roles": [m.get("role") for m in body.get("messages") or []]})
            if not full:
                self.send_error(503)
                return
            if names != ["db_summary_save"] or len(posts) > 2:
                self.send_error(503)
                return
            tool_count = sum(m.get("role") == "tool" for m in body.get("messages") or [])
            if tool_count == 0:
                call = {"id": "budget-tool-1", "type": "function",
                        "function": {"name": "db_summary_save",
                                     "arguments": json.dumps({"summary": "Q01 budget synthetic summary"})}}
                delta = {"role": "assistant", "tool_calls": [{"index": 0, **call}]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "done"}
                finish = "stop"
            frames = [
                {"id": "budget", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "budget", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            payload = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
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
    thread: threading.Thread | None = None
    started = False
    proc: subprocess.Popen | None = None
    result: dict = {"invocation": "q01-sdk-budget01-full" if full else "q01-sdk-budget01-discriminator", "posts": posts,
                    "process": {}, "trace": []}
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = True
        (private / "agent/models.yml").write_text(
            "providers:\n  q01budget:\n" f"    baseUrl: http://127.0.0.1:{server.server_port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 8192\n")
        (private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
        name = shutil.which("omp")
        if name is None:
            raise RuntimeError("installed OMP unavailable")
        omp = Path(name).resolve()
        proxy = "http://127.0.0.1:9"
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(private / "home"),
               "TERM": "dumb", "LANG": "C.UTF-8", "PI_CODING_AGENT_DIR": str(private / "agent"),
               "PI_CONFIG_DIR": str(private / "config"), "XDG_CONFIG_HOME": str(private / "xdg-config"),
               "XDG_DATA_HOME": str(private / "xdg-data"), "XDG_STATE_HOME": str(private / "xdg-state"),
               "XDG_CACHE_HOME": str(private / "xdg-cache"), "Q01_BUDGET_TRACE": str(private / "trace.jsonl"),
               "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1"}
        if full:
            env.update({"Q01_BUDGET_MODE": "full", "Q01_BUDGET_INPUT": str(private / "segments.json"),
                        "Q01_BUDGET_SKILL": str(HERE.parent / "q01_sdk_private_probe/summary-skill.txt"),
                        "Q01_BUDGET_WRITER": str(private / "fixed_writer.py"),
                        "Q01_BUDGET_PYTHON": sys.executable,
                        "Q01_BUDGET_REQUEST": str(private / "request.json"),
                        "Q01_BUDGET_TYPED": str(private / "typed-result.json")})
        env.update({key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                        "http_proxy", "https_proxy", "all_proxy")})
        argv = [str(omp), "--mode", "rpc", "--no-session", "--no-title", "--no-extensions",
                "--no-skills", "--no-rules", "--no-tools", "--no-pty", "--config",
                str(private / "overlay.yml"), "--extension", str(HERE / "budget_extension.ts"),
                "--model", "q01budget/scripted", "--cwd", str(private / "cwd")]
        version = subprocess.run([str(omp), "--version"], env=env, text=True,
                                 capture_output=True, timeout=10)
        result.update(omp_version=version.stdout.strip(), omp_sha256=digest(omp), argv=argv,
                      source_sha256={p.name: digest(p) for p in
                                     (HERE / "budget_extension.ts", HERE / "run_budget.py", HERE / "budget_support.ts")},
                      environment_keys=sorted(env), provider_port=server.server_port)
        with (out / "omp-stdout.log").open("wb") as stdout, (out / "omp-stderr.log").open("wb") as stderr:
            proc = subprocess.Popen(argv, cwd=private / "cwd", env=env, stdin=subprocess.PIPE,
                                    stdout=stdout, stderr=stderr, start_new_session=True)
            result["process"] = {"pid": proc.pid, "pgid": proc.pid}
            proc.stdin.write(b'{"id":"ready","type":"get_state"}\n')
            proc.stdin.flush()
            trace = private / "trace.jsonl"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and proc.poll() is None:
                if trace.exists() and (("done" if full else "discriminator") in trace.read_text() or
                                       '"event":"fatal"' in trace.read_text()):
                    break
                time.sleep(0.05)
            result["trace"] = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    finally:
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
        if started:
            server.shutdown()
        server.server_close()
        if started:
            thread.join(2)
        result["provider_stopped"] = not started or not thread.is_alive()
    if full:
        events = result["trace"]
        select = next((x for x in events if x.get("event") == "selection"), {})
        preflights = [x for x in events if x.get("event") == "dispatch_preflight"]
        preliminary = [x for x in events if x.get("event") == "preliminary_admission"]
        negatives = [x for x in events if x.get("event") == "negative_preflight"]
        expected_negative = {"mandatory_oversize": "input_budget_unavailable",
                             "fitter_lowered": "output_budget_unavailable",
                             "meaning_unknown": "output_budget_unavailable"}
        prompt_start_positions = [i for i, row in enumerate(events) if row.get("event") == "prompt_start"]
        negative_admission_proven = (len(negatives) == 3 and
            {x.get("case") for x in negatives} == set(expected_negative) and
            all(x.get("admitted") is False and x.get("reason") == expected_negative[x["case"]] and
                x.get("promptCount") == 0 and x.get("originalStreamCount") == 0
                for x in negatives) and
            len(prompt_start_positions) == 1 and
            all(i < prompt_start_positions[0] for i, row in enumerate(events)
                if row.get("event") == "negative_preflight"))
        typed_path = private / "typed-result.json"
        if typed_path.exists():
            typed = parse_summary_result(json.loads(typed_path.read_text()))
            request = parse_summary_request(json.loads((private / "request.json").read_text()))
            bound = typed["requestId"] == request["requestId"] and typed["inputKey"] == request["inputKey"] and \
                typed["skillDigest"] == request["skillDigest"] and typed["sources"] == request["sources"]
            repo = TaskRepository(private / "backend.sqlite3")
            try:
                accepted = repo.record_summary_result_receipt(typed, str(uuid4())) if bound else False
            finally:
                repo.close()
            result["typed_contract"] = {"bytes": len(typed_path.read_bytes()),
                                         "sha256": digest(typed_path), "request_bound": bound,
                                         "repository_accepted": accepted}
        else:
            result["typed_contract"] = {"missing": True, "request_bound": False,
                                         "repository_accepted": False}
        result["checks"] = {"collected_1MiB": select.get("collectedBytes") == 1048576,
                            "positive_db_only_two_posts": len(posts) == 2 and
                                all(x["tools"] == ["db_summary_save"] for x in posts),
                            "positive_cap_wire_8192": len(posts) == 2 and
                                all(8192 in (x["max_completion_tokens"], x["max_tokens"]) for x in posts),
                            "preflight_passed_each_post": len(preflights) == 2 and
                                all(x.get("admitted") is True and x.get("reason") is None and
                                    x.get("contextBytes", 999999) <= x.get("inputLimit", 0)
                                    and x.get("fittedCap") == 8192 and x.get("conservativeWireBound", 999999) <= 32768
                                    for x in preflights),
                            "negative_no_post": negative_admission_proven and len(posts) == 2 and
                                len(preliminary) == 1 and preliminary[0].get("admitted") is True and
                                len([x for x in events if x.get("event") == "done" and
                                     x.get("promptCount") == 1 and x.get("originalStreamCount") == 2]) == 1,
                            "saved_and_disposed": any(x.get("event") == "done" and x.get("saved") and not x.get("protected")
                                                      for x in events) and any(x.get("event") == "disposed" for x in events),
                            "actual_typed_contract": result["typed_contract"].get("request_bound") is True and
                                result["typed_contract"].get("repository_accepted") is True and
                                result["typed_contract"].get("bytes", 999999) <= 8192,
                            "stopped": result["provider_stopped"] and not result["process"].get("remaining_owned")}
    else:
        rows = [row for row in result["trace"] if row.get("event") == "discriminator"]
        result["checks"] = {"public_import_and_call": len(rows) == 1 and
                            not any(row.get("event") == "discriminator_error" for row in result["trace"]),
                            "no_model_post": len(posts) == 0,
                            "unchanged_and_reduced": len(rows) == 1 and rows[0].get("unchanged") == 8192 and
                            isinstance(rows[0].get("reduced"), int) and rows[0]["reduced"] < 8192,
                            "stopped": result["provider_stopped"] and not result["process"].get("remaining_owned")}
    (out / ("full.json" if full else "discriminator.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    value = run(Path(sys.argv[1]))
    print(json.dumps({"checks": value["checks"], "trace": value["trace"]}, ensure_ascii=False))
    raise SystemExit(0 if all(value["checks"].values()) else 2)
