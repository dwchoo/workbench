"""One private three-OMP canonical summary recovery discriminator."""
from __future__ import annotations

from copy import deepcopy
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
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from tests.backend.q01_sdk_full_probe.full_probe import _request
from workbench.contracts.logs_v1 import parse_summary_request, parse_summary_result, summary_input_key
from workbench.tasks.repository import InvalidTransitionError, TaskRepository

HERE = Path(__file__).resolve().parent
WRITER = HERE.parent / "q01_script_only_probe/fixed_writer.py"
SKILL = HERE.parent / "q01_sdk_private_probe/summary-skill.txt"
SUMMARY = "Literal command and SQL: touch /tmp/NO_EXECUTE; DROP TABLE summaries; --"
PHYSICAL_BYTES = SUMMARY.encode() * 3


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(part.get("text", "")) for part in value
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


def events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:  # A concurrent append can expose an incomplete final line.
            pass
    return rows


def wait_event(path: Path, name: str, timeout: float, **match: object) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = next((row for row in events(path) if row.get("event") == name and
                      all(row.get(key) == value for key, value in match.items())), None)
        if found is not None:
            return found
        time.sleep(0.025)
    return None


def canonical_request(template: dict, source: dict, start: int, end: int,
                      skill_sha: str, revision: int = 1) -> dict:
    item = deepcopy(template)
    item["requestId"] = str(uuid4())
    item["authorizationRef"] = str(uuid4())
    item["revision"] = revision
    span_source = {**source, "revision": revision}
    span = {"schemaVersion": 1, "source": span_source, "start": start, "end": end,
            "digest": sha256(PHYSICAL_BYTES[start:end]).hexdigest()}
    item["sources"] = [span]
    item["sourceGaps"] = []
    item["inputKey"] = summary_input_key(item["subject"], revision, [span], [], skill_sha)
    return parse_summary_request(item)


class Provider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, output: Path):
        self.output = output
        self.posts: list[dict] = []
        self.outbound: list[dict] = []
        self.violation: str | None = None
        self.active: str | None = None
        self.phase_private: dict[str, Path] = {}
        self.requests: dict[str, dict] = {}
        self.hold_seen = {name: threading.Event() for name in ("live_late", "killed", "fresh")}
        self.release = {name: threading.Event() for name in ("live_late", "killed", "fresh")}
        self.lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(inner) -> None:
                size = int(inner.headers.get("Content-Length", "0"))
                if size > 131072:
                    inner.send_error(413)
                    return
                raw = inner.rfile.read(size)
                try:
                    request = json.loads(raw)
                except ValueError:
                    request = {}
                messages = request.get("messages") or []
                users = [m for m in messages if m.get("role") == "user"]
                prompt = as_text(users[-1].get("content")) if users else ""
                tools = [tool.get("function", {}).get("name") for tool in request.get("tools") or []]
                tool_count = sum(m.get("role") == "tool" for m in messages)
                with self.lock:
                    phase = self.active
                    private = self.phase_private.get(phase or "")
                    expected = self.requests.get(phase or "")
                    if private is None or expected is None:
                        self.violation = "post_without_phase"
                        inner.send_error(503)
                        return
                    raw_path = private / f"provider-post-{sum(p['phase'] == phase for p in self.posts) + 1:03d}.body"
                    raw_path.write_bytes(raw)
                    bundled_path = private / "bundle-body.txt"
                    system = [as_text(m.get("content")) for m in messages if m.get("role") == "system"]
                    exact = bundled_path.exists() and system == [bundled_path.read_text() + "\n\n" + SKILL.read_text()]
                    permit_path = private / "permit-positive"
                    permit = permit_path.exists() and permit_path.read_text() == expected["requestId"]
                    record = {"phase": phase, "received_ns": time.monotonic_ns(),
                              "path": inner.path, "sha256": sha256(raw).hexdigest(), "bytes": len(raw),
                              "raw_body_path": str(raw_path), "tools": tools,
                              "tools_present": "tools" in request, "system_exact": exact,
                              "permit": permit, "request_id": expected["requestId"],
                              "tool_message_count": tool_count, "roles": [m.get("role") for m in messages],
                              "max_completion_tokens": request.get("max_completion_tokens")}
                    self.posts.append(record)
                    if not permit or not exact or tools != ["db_summary_save"] or \
                            "q01-contract:positive" not in prompt or \
                            sum(p["phase"] == phase for p in self.posts) > (1 if phase == "killed" else 2):
                        self.violation = f"forbidden_post:{phase}"
                    violation = self.violation
                if violation:
                    inner.send_error(503)
                    return
                if tool_count == 0 and phase in ("live_late", "killed"):
                    self.hold_seen[phase].set()
                    record["hold_started_ns"] = time.monotonic_ns()
                    released = self.release[phase].wait(3.0)
                    record["hold_released_ns"] = time.monotonic_ns()
                    record["hold_timed_out"] = not released
                if tool_count == 0:
                    call = {"id": f"recovery-{phase}-call", "type": "function", "function": {
                        "name": "db_summary_save", "arguments": json.dumps({"summary": SUMMARY})}}
                    delta = {"role": "assistant", "tool_calls": [{"index": 0, **call}]}
                    finish = "tool_calls"
                else:
                    call = None
                    delta = {"role": "assistant", "content": '{"status":"done"}'}
                    finish = "stop"
                frames = [
                    {"id": "recovery", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"id": "recovery", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                ]
                payload = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
                response_path = private / f"provider-response-{sum(p['phase'] == phase for p in self.posts):03d}.body"
                response_path.write_bytes(payload)
                record["response_sha256"] = sha256(payload).hexdigest()
                record["response_body_path"] = str(response_path)
                with self.lock:
                    self.outbound.append({"phase": phase, "call": call, "sha256": record["response_sha256"]})
                try:
                    inner.send_response(200)
                    inner.send_header("Content-Type", "text/event-stream")
                    inner.send_header("Content-Length", str(len(payload)))
                    inner.end_headers()
                    inner.wfile.write(payload)
                    record["responded_ns"] = time.monotonic_ns()
                except (BrokenPipeError, ConnectionResetError):
                    record["disconnected_ns"] = time.monotonic_ns()

            def log_message(inner, *_args: object) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)


class OwnedOmp:
    def __init__(self, phase: str, private: Path, provider: Provider, omp: Path, version: str):
        self.phase = phase
        self.private = private
        self.provider = provider
        self.omp = omp
        self.version = version
        self.proc: subprocess.Popen | None = None
        self.pump_thread: threading.Thread | None = None
        self.pump_stop = threading.Event()
        self.frames: list[dict] = []
        self.stderr_file = None
        self.record: dict = {"phase": phase, "process": {}, "rpc_frames": []}

    def start(self) -> None:
        private = self.private
        for name in ("home", "agent", "config", "cwd"):
            (private / name).mkdir(parents=True, mode=0o700)
        (private / "agent/models.yml").write_text(
            "providers:\n  q01contract:\n" f"    baseUrl: http://127.0.0.1:{self.provider.server_port}/v1\n"
            "    api: openai-completions\n    auth: none\n    models:\n"
            "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 8192\n")
        (private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
        (private / "control.txt").write_text("0")
        (private / "pause.txt").write_text("paused" if self.phase == "live_late" else "run")
        proxy = "http://127.0.0.1:9"
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(private / "home"),
               "TERM": "dumb", "LANG": "C.UTF-8", "PI_CODING_AGENT_DIR": str(private / "agent"),
               "PI_CONFIG_DIR": str(private / "config"), "XDG_CONFIG_HOME": str(private / "xdg-config"),
               "XDG_DATA_HOME": str(private / "xdg-data"), "XDG_STATE_HOME": str(private / "xdg-state"),
               "XDG_CACHE_HOME": str(private / "xdg-cache"), "Q01_CONTRACT_TRACE": str(private / "contract-trace.jsonl"),
               "Q01_CONTRACT_PRIVATE": str(private), "Q01_RECOVERY_TRACE": str(private / "recovery-trace.jsonl"),
               "Q01_RECOVERY_CONTROL": str(private / "control.txt"),
               "Q01_RECOVERY_PAUSE": str(private / "pause.txt"),
               "Q01_FIXED_WRITER": str(self.provider.output / "fixed_writer.py"),
               "Q01_FIXED_PYTHON": sys.executable,
               "Q01_SUMMARY_SKILL": str(SKILL), "PYTHONDONTWRITEBYTECODE": "1",
               "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
        env.update({key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                          "http_proxy", "https_proxy", "all_proxy")})
        argv = [str(self.omp), "--mode", "rpc", "--no-session", "--no-title", "--no-extensions",
                "--no-skills", "--no-rules", "--no-tools", "--no-pty", "--config",
                str(private / "overlay.yml"), "--extension", str(HERE / "recovery_extension.ts"),
                "--model", "q01contract/scripted", "--cwd", str(private / "cwd")]
        self.record.update(argv=argv, omp_version=self.version, omp_binary_sha256=digest(self.omp),
                           environment_keys=sorted(env))
        self.stderr_file = (private / "omp-stderr.log").open("wb")
        try:
            self.proc = subprocess.Popen(argv, cwd=private / "cwd", env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=self.stderr_file,
                                         start_new_session=True)
        except BaseException:
            self.stderr_file.close()
            raise
        self.record["process"] = {"pid": self.proc.pid, "pgid": self.proc.pid}

        def pump() -> None:
            pending = b""
            while not self.pump_stop.is_set():
                try:
                    ready, _, _ = select.select([self.proc.stdout.fileno()], [], [], 0.2)
                    if not ready:
                        if self.proc.poll() is not None:
                            return
                        continue
                    chunk = os.read(self.proc.stdout.fileno(), 65536)
                    if not chunk:
                        return
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    for line in lines:
                        try:
                            frame = json.loads(line)
                        except ValueError:
                            continue
                        self.frames.append({"observed_ns": time.monotonic_ns(), "frame": frame})
                except OSError:
                    return

        self.pump_thread = threading.Thread(target=pump, daemon=True)
        self.pump_thread.start()
        self.proc.stdin.write(b'{"id":"ready","type":"get_state"}\n')
        self.proc.stdin.flush()
        self.record["session_start_captured"] = wait_event(private / "recovery-trace.jsonl",
                                                             "session_start_captured", 12) is not None
        if not self.record["session_start_captured"]:
            raise RuntimeError(f"{self.phase}:session_start_not_captured")

    def trigger(self, number: int) -> dict:
        (self.private / "control.txt").write_text(str(number))
        found = wait_event(self.private / "recovery-trace.jsonl", "admission_attempt", 4, attempt=number)
        if found is None:
            raise RuntimeError(f"{self.phase}:admission_attempt_{number}_missing")
        return found

    def stop(self, *, kill: bool = False) -> dict:
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(5)
            self.pump_stop.set()
            if self.pump_thread is not None and self.pump_thread.ident is not None:
                self.pump_thread.join(2)
            try:
                os.killpg(self.proc.pid, 0)
                absent = False
            except ProcessLookupError:
                absent = True
            self.record["process"].update(returncode=self.proc.returncode, pgid_absent=absent,
                                          pump_stopped=self.pump_thread is None or not self.pump_thread.is_alive(),
                                          remaining_owned=[] if self.proc.poll() is not None else [self.proc.pid])
            self.record["rpc_frames"] = [{"observed_ns": row["observed_ns"],
                                          "id": row["frame"].get("id"),
                                          "type": row["frame"].get("type"),
                                          "error": row["frame"].get("error")}
                                         for row in self.frames[:100]]
            self.record["contract_trace"] = events(self.private / "contract-trace.jsonl")
            self.record["recovery_trace"] = events(self.private / "recovery-trace.jsonl")
            self.proc.stdout.close()
            self.proc.stdin.close()
            self.proc = None
        if self.stderr_file is not None and not self.stderr_file.closed:
            self.stderr_file.close()
        return self.record


def run(out: Path) -> dict:
    out = out.resolve()
    if out.exists():
        raise ValueError("refuse to overwrite evidence")
    out.mkdir(parents=True, mode=0o700)
    private = out / "private"
    private.mkdir(mode=0o700)
    shutil.copyfile(WRITER, private / "fixed_writer.py")
    journal = private / "backend.sqlite3"
    (private / "physical-source.txt").write_bytes(PHYSICAL_BYTES)
    skill_sha = digest(SKILL)
    repo = TaskRepository(journal)
    template, _ = _request(repo, SUMMARY, skill_sha)
    physical_source = {"schemaVersion": 1, "sourceKind": "event", "collectorEpoch": 1,
                       "taskId": template["subject"]["id"], "revision": 1, "eventId": str(uuid4())}
    unit = len(SUMMARY.encode())
    paused = canonical_request(template, physical_source, 0, unit, skill_sha)
    load_failed = canonical_request(template, physical_source, 0, unit, skill_sha)
    live = canonical_request(template, physical_source, 0, unit, skill_sha)
    killed = canonical_request(template, physical_source, unit, 2 * unit, skill_sha)
    fresh = canonical_request(template, physical_source, 2 * unit, 3 * unit, skill_sha)
    requests = {"paused": paused, "load_failed": load_failed, "live_late": live,
                "killed": killed, "fresh": fresh}
    phases = {name: private / name for name in ("live_late", "killed", "fresh")}
    for name, directory in phases.items():
        directory.mkdir(mode=0o700)
        (directory / "requests.json").write_text(json.dumps({"positive": requests[name],
            "paused": paused, "load_failed": load_failed}, ensure_ascii=False) + "\n")
        (directory / "role-inputs.json").write_text(json.dumps({
            "positive": {"role": "Manager", "agentName": "task"},
            "saved_then_bash": {"role": "Worker", "agentName": "task"},
            "worker_role": {"role": "Worker", "agentName": "task"}}, ensure_ascii=False) + "\n")
    result: dict = {"invocation": "q01-sdk-recovery-runtime01", "requests": requests,
                    "physical_source": physical_source, "source_bytes": unit * 3,
                    "phases": {}, "journal": {}, "checks": {}, "provider_posts": []}
    provider: Provider | None = None
    provider_thread: threading.Thread | None = None
    active: OwnedOmp | None = None
    stopped: list[dict] = []
    try:
        repo.reserve_summary_request(paused)
        provider = Provider(private)
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        omp_name = shutil.which("omp")
        if not omp_name:
            raise RuntimeError("installed OMP not found")
        omp = Path(omp_name).resolve()
        proxy_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(private),
                     "PI_CODING_AGENT_DIR": str(private), "NO_PROXY": "127.0.0.1"}
        version = subprocess.run([str(omp), "--version"], env=proxy_env, text=True,
                                 capture_output=True, timeout=10).stdout.strip()
        result.update(omp_version=version, omp_binary_sha256=digest(omp),
                      source_sha256={str(p): digest(p) for p in
                                     (HERE / "recovery_extension.ts", HERE / "run_recovery.py",
                                      HERE / "contract_extension.ts", HERE / "role_admission.ts",
                                      HERE / "full_probe.py", HERE / "run_contract.py", WRITER, SKILL,
                                      ROOT / "src/workbench/tasks/repository.py")})

        def start_phase(name: str) -> OwnedOmp:
            nonlocal active
            provider.active = name
            provider.phase_private[name] = phases[name]
            provider.requests[name] = requests[name]
            active = OwnedOmp(name, phases[name], provider, omp, version)
            active.start()
            return active

        first = start_phase("live_late")
        gate1 = first.trigger(1)
        first_defer = wait_event(phases["live_late"] / "recovery-trace.jsonl", "deferred", 2,
                                 attempt=1, reason="paused")
        first_zero = not provider.posts and not events(phases["live_late"] / "contract-trace.jsonl")
        result["journal"]["paused_gate"] = {"requestId": paused["requestId"],
                                             "deferred": bool(first_defer), "postCount": len(provider.posts),
                                             "contractEvents": len(events(phases["live_late"] / "contract-trace.jsonl"))}
        if not first_defer or not first_zero or gate1["admitted"]:
            raise RuntimeError("pause_did_not_stop_callback_before_send")
        repo.record_summary_state(paused["requestId"], "not_sent", detail="probe_pause_confirmed_before_send")
        repo.reserve_summary_request(load_failed)
        (phases["live_late"] / "pause.txt").unlink()
        gate2 = first.trigger(2)
        second_defer = wait_event(phases["live_late"] / "recovery-trace.jsonl", "deferred", 2,
                                  attempt=2, reason="load_failed")
        second_zero = not provider.posts and not events(phases["live_late"] / "contract-trace.jsonl")
        result["journal"]["load_failed_gate"] = {"requestId": load_failed["requestId"],
                                                  "deferred": bool(second_defer), "postCount": len(provider.posts),
                                                  "contractEvents": len(events(phases["live_late"] / "contract-trace.jsonl"))}
        if not second_defer or not second_zero or gate2["admitted"]:
            raise RuntimeError("pause_load_failure_did_not_fail_closed")
        repo.record_summary_state(load_failed["requestId"], "not_sent", detail="probe_pause_load_failed_before_send")
        repo.reserve_summary_request(live)
        repo.record_summary_state(live["requestId"], "sending")
        (phases["live_late"] / "pause.txt").write_text("run")
        gate3 = first.trigger(3)
        if not gate3["admitted"] or not provider.hold_seen["live_late"].wait(25):
            raise RuntimeError("live_first_post_not_held")
        result["journal"]["live_first_post_before_unknown"] = len(provider.posts)
        repo.record_summary_state(live["requestId"], "unknown", detail="host_timeout_child_still_alive")
        repo.close()
        repo = TaskRepository(journal)
        unresolved = repo.list_unresolved_summary_requests()
        result["journal"]["reopened_live_unknown"] = repo.get_summary_request(live["requestId"])
        result["journal"]["unresolved_after_live_reopen"] = unresolved
        provider.release["live_late"].set()
        if wait_event(phases["live_late"] / "contract-trace.jsonl", "done", 25) is None:
            raise RuntimeError("live_child_did_not_complete")
        typed_live_path = phases["live_late"] / "typed-positive.json"
        if not typed_live_path.exists():
            raise RuntimeError("live_actual_typed_missing")
        typed_live = parse_summary_result(json.loads(typed_live_path.read_text()))
        result["journal"]["live_typed_exact"] = all(typed_live[key] == live[key] for key in
            ("requestId", "inputKey", "skillDigest", "sources"))
        negative: dict[str, bool] = {}
        for name, value in {
            "wrong_id": {**typed_live, "requestId": str(uuid4())},
            "wrong_revision": {**typed_live, "sources": [{**typed_live["sources"][0], "source": {
                **typed_live["sources"][0]["source"], "revision": 2}}]},
            "wrong_range": {**typed_live, "sources": [{**typed_live["sources"][0],
                "end": typed_live["sources"][0]["end"] + 1}]},
        }.items():
            try:
                repo.record_summary_result_receipt(value, str(uuid4()))
            except (InvalidTransitionError, ValueError):
                negative[name + "_rejected"] = True
            else:
                negative[name + "_rejected"] = False
        result["journal"]["live_bad_result_controls"] = negative
        result["journal"]["live_late_accepted"] = repo.record_summary_result_receipt(typed_live, str(uuid4()))
        result["journal"]["live_duplicate_dropped"] = repo.record_summary_result_receipt(typed_live, str(uuid4())) is False
        result["phases"]["live_late"] = first.stop()
        stopped.append(result["phases"]["live_late"]["process"])
        active = None

        repo.reserve_summary_request(killed)
        repo.record_summary_state(killed["requestId"], "sending")
        second = start_phase("killed")
        second.trigger(3)
        if not provider.hold_seen["killed"].wait(25):
            raise RuntimeError("killed_first_post_not_held")
        result["journal"]["killed_first_post_before_stop"] = sum(p["phase"] == "killed" for p in provider.posts)
        result["phases"]["killed"] = second.stop(kill=True)
        stopped.append(result["phases"]["killed"]["process"])
        active = None
        provider.release["killed"].set()
        settled_deadline = time.monotonic() + 2
        while time.monotonic() < settled_deadline and not all(
                "response_sha256" in post for post in provider.posts if post["phase"] == "killed"):
            time.sleep(0.025)
        repo.record_summary_state(killed["requestId"], "unknown", detail="owned_omp_killed_after_first_post")
        repo.close()
        repo = TaskRepository(journal)
        result["journal"]["reopened_killed_unknown"] = repo.get_summary_request(killed["requestId"])
        result["journal"]["unresolved_after_killed_reopen"] = repo.list_unresolved_summary_requests()
        exact = canonical_request(template, physical_source, unit, 2 * unit, skill_sha)
        overlap = canonical_request(template, physical_source, unit + 1, 2 * unit, skill_sha, revision=2)
        controls = {}
        for name, request in (("exact_new_uuid", exact), ("overlap_new_revision", overlap)):
            try:
                repo.reserve_summary_request(request)
            except InvalidTransitionError as error:
                controls[name] = {"rejected": True, "reason": str(error),
                                  "requestId": request["requestId"], "inputKey": request["inputKey"],
                                  "sources": request["sources"]}
            else:
                controls[name] = {"rejected": False, "requestId": request["requestId"]}
        result["journal"]["replay_reservation_controls"] = controls
        if not all(row["rejected"] for row in controls.values()):
            raise RuntimeError("unknown_range_replay_reservation_accepted")

        repo.reserve_summary_request(fresh)
        repo.record_summary_state(fresh["requestId"], "sending")
        third = start_phase("fresh")
        third.trigger(3)
        if wait_event(phases["fresh"] / "contract-trace.jsonl", "done", 25) is None:
            raise RuntimeError("fresh_child_did_not_complete")
        typed_fresh_path = phases["fresh"] / "typed-positive.json"
        if not typed_fresh_path.exists():
            raise RuntimeError("fresh_actual_typed_missing")
        typed_fresh = parse_summary_result(json.loads(typed_fresh_path.read_text()))
        result["journal"]["fresh_typed_exact"] = all(typed_fresh[key] == fresh[key] for key in
            ("requestId", "inputKey", "skillDigest", "sources"))
        result["journal"]["fresh_accepted"] = repo.record_summary_result_receipt(typed_fresh, str(uuid4()))
        result["phases"]["fresh"] = third.stop()
        stopped.append(result["phases"]["fresh"]["process"])
        active = None
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if active is not None:
            result["phases"][active.phase] = active.stop(kill=True)
            stopped.append(result["phases"][active.phase]["process"])
        if provider is not None:
            for released in provider.release.values():
                released.set()
            if provider_thread is not None and provider_thread.ident is not None:
                provider.shutdown()
            provider.server_close()
            if provider_thread is not None and provider_thread.ident is not None:
                provider_thread.join(2)
            result["provider_stopped"] = provider_thread is None or not provider_thread.is_alive()
            result["provider_posts"] = provider.posts
            result["provider_outbound"] = provider.outbound
            result["provider_violation"] = provider.violation
        else:
            result["provider_stopped"] = True
        repo.close()
    with sqlite3.connect(journal) as connection:
        result["journal"]["states"] = [list(row) for row in connection.execute(
            "SELECT request_id,state FROM summary_request_events_v1 ORDER BY event_id")]
        result["journal"]["receipts"] = [list(row) for row in connection.execute(
            "SELECT request_id,result_digest FROM summary_result_receipts_v1 ORDER BY rowid")]
        result["journal"]["drops"] = [list(row) for row in connection.execute(
            "SELECT request_id,reason FROM summary_result_drops_v1 ORDER BY rowid")]
    summary_db = private / "summary.sqlite3"
    if summary_db.exists():
        with sqlite3.connect(summary_db) as connection:
            result["db_rows"] = [list(row) for row in connection.execute(
                "SELECT id,summary FROM summaries ORDER BY id")]
    else:
        result["db_rows"] = []
    posts = result.get("provider_posts", [])
    per_phase = {name: [post for post in posts if post["phase"] == name] for name in phases}
    contract_traces = {name: result["phases"].get(name, {}).get("contract_trace", []) for name in phases}
    gate_trace = result["phases"].get("live_late", {}).get("recovery_trace", [])
    result["checks"] = {
        "pause_and_loadfail_zero_send": all(result["journal"].get(key) is not None for key in
            ("live_first_post_before_unknown",)) and
            all(result["journal"].get(key, {}).get("deferred") is True and
                result["journal"].get(key, {}).get("postCount") == 0 and
                result["journal"].get(key, {}).get("contractEvents") == 0
                for key in ("paused_gate", "load_failed_gate")) and
            [paused["requestId"], "not_sent"] in result["journal"]["states"] and
            [load_failed["requestId"], "not_sent"] in result["journal"]["states"] and
            len([e for e in gate_trace if e.get("event") == "deferred"]) == 2 and
            [e.get("reason") for e in gate_trace if e.get("event") == "deferred"] == ["paused", "load_failed"] and
            len([e for e in gate_trace if e.get("event") == "captured_callback_invoked"]) == 1,
        "same_physical_not_sent_retry": all(requests[name]["sources"] == live["sources"] and
            requests[name]["inputKey"] == live["inputKey"] and
            requests[name]["subject"] == live["subject"] for name in ("paused", "load_failed")) and
            len({paused["requestId"], load_failed["requestId"], live["requestId"]}) == 3,
        "live_unknown_reopen_and_late": result["journal"].get("reopened_live_unknown", {}).get("state") == "unknown" and
            any(item["request"]["requestId"] == live["requestId"] for item in
                result["journal"].get("unresolved_after_live_reopen", [])) and
            result["journal"].get("live_first_post_before_unknown") == 1 and
            result["journal"].get("live_typed_exact") is True and
            result["journal"].get("live_late_accepted") is True and
            result["journal"].get("live_duplicate_dropped") is True,
        "live_bad_results_rejected": all(result["journal"].get("live_bad_result_controls", {}).values()) and
            len(result["journal"].get("live_bad_result_controls", {})) == 3,
        "kill_before_unknown_no_replay": result["journal"].get("killed_first_post_before_stop") == 1 and
            result["journal"].get("reopened_killed_unknown", {}).get("state") == "unknown" and
            result["phases"].get("killed", {}).get("process", {}).get("pgid_absent") is True and
            not (phases["killed"] / "typed-positive.json").exists() and len(per_phase["killed"]) == 1,
        "exact_overlap_rejected": all(row.get("rejected") and
            ("reserved or sent" in row.get("reason", "")) for row in
            result["journal"].get("replay_reservation_controls", {}).values()) and
            len(result["journal"].get("replay_reservation_controls", {})) == 2,
        "fresh_nonoverlap_saved": result["journal"].get("fresh_typed_exact") is True and
            result["journal"].get("fresh_accepted") is True and len(per_phase["fresh"]) == 2 and
            result["db_rows"] == [[1, SUMMARY], [2, SUMMARY]],
        "five_db_only_posts": len(posts) == 5 and
            [len(per_phase[name]) for name in ("live_late", "killed", "fresh")] == [2, 1, 2] and
            all(post["tools"] == ["db_summary_save"] and post["system_exact"] and
                post["permit"] and post["tools_present"] for post in posts),
        "role_other_cases_refused": all(len([e for e in contract_traces[name] if
            e.get("event") == "role_rejected" and e.get("case") == case]) == 1
            for name in ("live_late", "fresh") for case in ("saved_then_bash", "worker_role")),
        "all_owned_stopped": len(stopped) == 3 and all(p.get("pgid_absent") and
            p.get("pump_stopped") and not p.get("remaining_owned") for p in stopped) and
            result["provider_stopped"],
        "raw_response_sha": all(digest(Path(post["raw_body_path"])) == post["sha256"] and
            digest(Path(post["response_body_path"])) == post["response_sha256"] for post in posts),
    }
    result["status"] = "candidate_ready" if all(result["checks"].values()) and not result.get("error") and \
        not result.get("provider_violation") else "needs_reroute"
    (out / "recovery-evidence.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    value = run(Path(sys.argv[1]))
    print(json.dumps({"status": value["status"], "checks": value["checks"],
                      "error": value.get("error"), "posts": len(value["provider_posts"])}, ensure_ascii=False))
    raise SystemExit(0 if value["status"] == "candidate_ready" else 2)
