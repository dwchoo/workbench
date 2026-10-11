"""One private two-TUI G3 notice / budget-child overlap observation."""
from __future__ import annotations

import base64
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
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests/gates/g3_omp"))
from live_omp_probe import BridgeHarness, PeerHandler  # unchanged G3 test protocol
from tests.backend.q01_sdk_full_probe.full_probe import _request
from workbench.contracts.logs_v1 import parse_summary_request, parse_summary_result, summary_input_key
from workbench.observation.worker_review import ActiveRunRef, SerializedReviewAdmission, WorkerReviewScheduler
from workbench.tasks.repository import TaskRepository

HERE = Path(__file__).resolve().parent
BRIDGE = ROOT / "omp_bridge/g3/bridge.ts"
SKILL = HERE.parent / "q01_sdk_private_probe/summary-skill.txt"
WRITER = HERE.parent / "q01_script_only_probe/fixed_writer.py"
SELECTION_MAX = 400  # one source range; default 850 in budget_extension remains unchanged
DRAFT = "Q01-PRIORITY-UNSENT-DRAFT"
SUMMARY = "Q01 priority runtime private literal summary"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def wait_until(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.025)
    return bool(predicate())


class _Reader:
    def __init__(self, inner, harness, connection):
        self.inner, self.harness, self.connection = inner, harness, connection

    def readline(self, *args):
        raw = self.inner.readline(*args)
        if raw:
            self.harness.record_wire("omp_to_host", self.connection, raw)
        return raw

    def __getattr__(self, name):
        return getattr(self.inner, name)


class RecordingPeer(PeerHandler):
    def setup(self):
        super().setup()
        self.rfile = _Reader(self.rfile, self.server, str(id(self)))

    def _write(self, value):
        raw = (json.dumps(value) + "\n").encode()
        self.server.record_wire("host_to_omp", str(id(self)), raw)
        self.wfile.write(raw)
        self.wfile.flush()


class RecordingBridge(BridgeHarness):
    def __init__(self, socket_path, tokens, wire_path):
        super().__init__(socket_path, tokens)
        self.RequestHandlerClass = RecordingPeer
        self.wire_path = wire_path
        self.wire_lock = threading.Lock()

    def record_wire(self, direction, connection, raw):
        with self.wire_lock:
            with self.wire_path.open("a") as output:
                output.write(json.dumps({"direction": direction, "connection": connection,
                    "monotonic_ns": time.monotonic_ns(), "sha256": sha256(raw).hexdigest(),
                    "raw_b64": base64.b64encode(raw).decode()}) + "\n")

    def request(self, role, message, timeout=5):
        peer = self.peer(role)
        request_id = str(uuid4())
        frame = {**message, "requestId": request_id}
        raw = (json.dumps(frame) + "\n").encode()
        with peer["write_lock"]:
            self.record_wire("host_to_omp", role, raw)
            peer["socket"].sendall(raw)
        deadline = time.monotonic() + timeout
        with self.condition:
            while request_id not in self.acks and time.monotonic() < deadline:
                self.condition.wait(deadline - time.monotonic())
            answer = self.acks.pop(request_id, None)
        if answer is None:
            raise TimeoutError(f"G3 {role} {message.get('kind')} ack missing")
        return answer


class RoleProvider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, role, private):
        self.role, self.private = role, private
        self.posts = []
        self.lock = threading.Lock()
        self.child_held = threading.Event()
        self.release_child = threading.Event()
        self.notice_post = threading.Event()
        self.violation = None
        super().__init__(("127.0.0.1", 0), ProviderHandler)


class ProviderHandler(BaseHTTPRequestHandler):
    server: RoleProvider

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 131072:
            self.send_error(413)
            return
        raw = self.rfile.read(size)
        try:
            body = json.loads(raw)
        except ValueError:
            self.server.violation = "bad_json"
            self.send_error(400)
            return
        tools = [x.get("function", {}).get("name") for x in body.get("tools") or []]
        messages = body.get("messages") or []
        tool_count = sum(x.get("role") == "tool" for x in messages)
        is_child = self.server.role == "manager" and tools == ["db_summary_save"]
        stage = "child_first" if is_child and tool_count == 0 else "child_second" if is_child else "parent"
        notice_id = None
        for message in messages:
            if message.get("role") != "user":
                continue
            content = message.get("content")
            texts = [content] if isinstance(content, str) else [x.get("text") for x in content or []
                if isinstance(x, dict) and x.get("type") == "text"]
            for text in texts:
                if not isinstance(text, str):
                    continue
                try:
                    value = json.loads(text)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("workbench_notice"):
                    notice_id = value.get("notice_id")
        with self.server.lock:
            index = len(self.server.posts) + 1
            raw_path = self.server.private / f"{self.server.role}-post-{index:03d}.body"
            raw_path.write_bytes(raw)
            record = {"role": self.server.role, "stage": stage, "received_ns": time.monotonic_ns(),
                "path": self.path, "sha256": sha256(raw).hexdigest(), "bytes": len(raw),
                "raw_body_path": str(raw_path), "tools": tools, "roles": [x.get("role") for x in messages],
                "notice_id": notice_id, "draft_leaked": DRAFT.encode() in raw,
                "max_completion_tokens": body.get("max_completion_tokens"),
                "max_tokens": body.get("max_tokens")}
            self.server.posts.append(record)
            if stage == "parent" and notice_id:
                self.server.notice_post.set()
            if stage == "parent" and not notice_id:
                self.server.violation = "parent_without_notice"
            if self.server.role == "worker" and stage != "parent":
                self.server.violation = "worker_child_tool"
        if stage == "child_first":
            self.server.child_held.set()
            released = self.server.release_child.wait(5)
            record["hold_released_ns"] = time.monotonic_ns()
            record["hold_timed_out"] = not released
            if not released:
                self.server.violation = "child_hold_timeout"
        if self.server.violation:
            self.send_error(503)
            return
        if stage == "child_first":
            call = {"id": "priority-runtime-db", "type": "function", "function": {
                "name": "db_summary_save", "arguments": json.dumps({"summary": SUMMARY})}}
            delta = {"role": "assistant", "tool_calls": [{"index": 0, **call}]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "private-notice-done" if stage == "parent" else "private-child-done"}
            finish = "stop"
        frame = {"id": "priority-runtime", "object": "chat.completion.chunk", "created": 0,
            "model": "scripted", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        payload = ("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode()
        response_path = self.server.private / f"{self.server.role}-response-{index:03d}.body"
        response_path.write_bytes(payload)
        record["response_body_path"] = str(response_path)
        record["response_sha256"] = sha256(payload).hexdigest()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            record["responded_ns"] = time.monotonic_ns()
        except (BrokenPipeError, ConnectionResetError):
            record["disconnected_ns"] = time.monotonic_ns()

    def log_message(self, *_args):
        pass


def fixture(private: Path) -> dict:
    shutil.copyfile(WRITER, private / "fixed_writer.py")
    pieces = []
    for kind, source, letter in (("cumulative", "cumulative", "C"),
                                 ("start_spec", "start-spec", "S"),
                                 ("decision", "decisions", "D"),
                                 ("report", "reports", "R")):
        pieces.append({"source": source, "revision": 1, "kind": kind, "start": 0,
            "end": 120, "text": letter * 120, "sequence": 1})
    remaining = 1048576 - 480 - 120
    start, sequence = 0, 1
    while remaining:
        size = min(4096, remaining)
        pieces.append({"source": "terminal", "revision": 1, "kind": "terminal",
            "start": start, "end": start + size, "text": "T" * size, "sequence": sequence})
        start += size
        sequence += 1
        remaining -= size
    pieces.append({"source": "terminal", "revision": 1, "kind": "terminal", "start": start,
        "end": start + 120, "text": "N" * 120, "sequence": sequence})
    (private / "segments.json").write_text(json.dumps(pieces))
    selected = pieces[0]
    if len(json.dumps({"selected": [selected]}, separators=(",", ":"))) > SELECTION_MAX or \
            len(json.dumps({"selected": pieces[:2]}, separators=(",", ":"))) <= SELECTION_MAX:
        raise AssertionError("offline fixture selection changed")
    repo = TaskRepository(private / "backend.sqlite3")
    try:
        skill_sha = digest(SKILL)
        request, _seed = _request(repo, "Q01 priority synthetic summary", skill_sha)
        task_id = request["subject"]["id"]
        source = {"schemaVersion": 1, "sourceKind": "event", "collectorEpoch": 1,
            "taskId": task_id, "revision": 1, "eventId": str(uuid4())}
        spans = [{"schemaVersion": 1, "source": source, "start": 0, "end": 120,
            "digest": sha256(selected["text"].encode()).hexdigest()}]
        request = parse_summary_request({**request, "sources": spans,
            "inputKey": summary_input_key(request["subject"], 1, spans, [], skill_sha)})
        repo.reserve_summary_request(request)
        repo.record_summary_state(request["requestId"], "sending")
        (private / "request.json").write_text(json.dumps(request))
        return request
    finally:
        repo.close()


def run(out: Path) -> dict:
    out = out.resolve()
    if out.exists():
        raise ValueError("refuse to overwrite evidence")
    out.mkdir(parents=True, mode=0o700)
    private = out / "private"
    private.mkdir(mode=0o700)
    for role in ("manager", "worker"):
        for name in ("home", "agent", "config", "cwd", "sessions"):
            (private / role / name).mkdir(parents=True, mode=0o700)
    request = fixture(private)
    (private / "control.txt").write_text("idle")
    providers = {role: RoleProvider(role, private) for role in ("manager", "worker")}
    threads = {}
    bridge = None
    bridge_thread = None
    procs = {}
    masters = {}
    drains = {}
    trace = private / "budget-trace.jsonl"
    wrapper_trace = private / "priority-trace.jsonl"
    result = {"invocation": "q01-sdk-priority-runtime01", "request_id": request["requestId"],
        "source_sha256": {str(p): digest(p) for p in (HERE / "budget_extension.ts",
            HERE / "priority_runtime_extension.ts", HERE / "run_priority_runtime.py",
            HERE / "budget_support.ts", BRIDGE, WRITER, SKILL)},
        "providers": {}, "processes": {}, "bridge": {}, "scheduler": {}, "checks": {},
        "timing": {"clock": "time.monotonic_ns", "state_samples": []}}

    def bridge_request(role, frame):
        sent_ns = time.monotonic_ns()
        ack = bridge.request(role, frame, timeout=2)
        result["bridge"].setdefault("requests", []).append({"role": role, "frame": frame,
            "sent_ns": sent_ns, "ack": ack, "ack_observed_ns": time.monotonic_ns()})
        return ack

    def probe(role):
        answer = bridge_request(role, {"kind": "probe"})
        if answer.get("status") != "state":
            raise RuntimeError(f"{role} probe failed")
        result["timing"]["state_samples"].append({"role": role,
            "observed_ns": result["bridge"]["requests"][-1]["ack_observed_ns"],
            "state": answer["state"]})
        return answer["state"]

    def wait_state(role, predicate, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = probe(role)
            if predicate(state):
                return state
            time.sleep(0.05)
        raise TimeoutError(f"{role} desired TUI state missing")

    def start_tui(role, omp, env):
        phase = private / role
        argv = [str(shutil.which("setsid")), "--ctty", str(omp), "--no-session", "--no-title",
            "--no-extensions", "--no-skills", "--no-rules", "--no-pty",
            "--session-dir", str(phase / "sessions"), "--config", str(phase / "overlay.yml"),
            "--extension", str(BRIDGE)]
        if role == "manager":
            argv += ["--extension", str(HERE / "priority_runtime_extension.ts")]
        argv += ["--model", f"q01priority{role}/scripted", "--cwd", str(phase / "cwd")]
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        try:
            process = subprocess.Popen(argv, cwd=phase / "cwd", env=env, stdin=slave,
                stdout=slave, stderr=slave, close_fds=True)
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        procs[role], masters[role] = process, master
        stop = threading.Event()

        def drain():
            with (phase / "pty-output.raw").open("wb") as output:
                while not stop.is_set():
                    try:
                        if select.select([master], [], [], 0.1)[0]:
                            raw = os.read(master, 65536)
                            if not raw:
                                return
                            output.write(raw)
                    except OSError:
                        return

        worker = threading.Thread(target=drain, daemon=True)
        worker.start()
        drains[role] = (worker, stop)
        result["processes"][role] = {"pid": process.pid, "pgid": process.pid, "argv": argv}

    try:
        socket_path = private / "bridge.sock"
        tokens = {role: str(uuid4()) for role in providers}
        bridge = RecordingBridge(socket_path, tokens, private / "bridge-wire.jsonl")
        os.chmod(socket_path, 0o600)
        bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        bridge_thread.start()
        for role, provider in providers.items():
            threads[role] = threading.Thread(target=provider.serve_forever, daemon=True)
            threads[role].start()
        model_lines = ["providers:"]
        for role, provider in providers.items():
            model_lines += [f"  q01priority{role}:",
                f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1",
                "    api: openai-completions", "    auth: none", "    models:",
                "      - id: scripted", "        name: scripted", "        contextWindow: 32768",
                f"        maxTokens: {8192 if role == 'manager' else 1024}"]
        for role in providers:
            phase = private / role
            (phase / "agent/models.yml").write_text("\n".join(model_lines) + "\n")
            (phase / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
        omp_name = shutil.which("omp")
        if not omp_name or not shutil.which("setsid"):
            raise RuntimeError("installed omp or setsid unavailable")
        omp = Path(omp_name).resolve()
        base_env = {"PATH": os.environ.get("PATH", ""), "TERM": "xterm-256color",
            "COLORTERM": "truecolor", "LANG": "C.UTF-8", "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1"}
        base_env.update({key: "http://127.0.0.1:9" for key in
            ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")})
        version = subprocess.run([str(omp), "--version"], env=base_env, text=True,
            capture_output=True, timeout=10)
        result.update(omp_version=version.stdout.strip(), omp_binary_sha256=digest(omp),
            environment_keys=sorted(base_env))
        for role in providers:
            phase = private / role
            env = {**base_env, "HOME": str(phase / "home"), "PI_CODING_AGENT_DIR": str(phase / "agent"),
                "PI_CONFIG_DIR": str(phase / "config"), "XDG_CONFIG_HOME": str(phase / "xdg-config"),
                "XDG_DATA_HOME": str(phase / "xdg-data"), "XDG_STATE_HOME": str(phase / "xdg-state"),
                "XDG_CACHE_HOME": str(phase / "xdg-cache"),
                "WORKBENCH_G3_BRIDGE_SOCKET": str(socket_path), "WORKBENCH_G3_ROLE": role,
                "WORKBENCH_G3_TOKEN": tokens[role], "WORKBENCH_G3_GENERATION": "1",
                "WORKBENCH_G3_EXPECTED_OMP_VERSION": version.stdout.strip()}
            if role == "manager":
                env.update({"Q01_BUDGET_MODE": "full", "Q01_BUDGET_SELECTION_MAX": str(SELECTION_MAX),
                    "Q01_BUDGET_TRACE": str(trace), "Q01_BUDGET_INPUT": str(private / "segments.json"),
                    "Q01_BUDGET_SKILL": str(SKILL), "Q01_BUDGET_WRITER": str(private / "fixed_writer.py"),
                    "Q01_BUDGET_PYTHON": sys.executable, "Q01_BUDGET_REQUEST": str(private / "request.json"),
                    "Q01_BUDGET_TYPED": str(private / "typed-result.json"),
                    "Q01_PRIORITY_RUNTIME_TRACE": str(wrapper_trace),
                    "Q01_PRIORITY_RUNTIME_CONTROL": str(private / "control.txt")})
            start_tui(role, omp, env)
        peers = {role: bridge.peer(role, timeout=12) for role in providers}
        result["bridge"]["hello"] = {role: peers[role]["hello"] for role in providers}
        states = {role: wait_state(role, lambda x: x.get("idle") is True and
            x.get("editorEmpty") is True and x.get("editorKnown") is True, 8) for role in providers}
        result["bridge"]["initial_state"] = states
        if any(provider.posts for provider in providers.values()):
            raise RuntimeError("provider POST before budget start")
        (private / "control.txt").write_text("start")
        if not wait_until(lambda: any(x.get("event") == "budget_callback_invoked"
            for x in events(wrapper_trace)), 5):
            raise TimeoutError("captured budget callback not invoked")
        if not providers["manager"].child_held.wait(10):
            raise TimeoutError("child first POST not held")
        hold_ns = providers["manager"].posts[0]["received_ns"]
        result["child_hold_started_ns"] = hold_ns
        result["timing"]["child_hold_started_ns"] = hold_ns

        result["timing"]["draft_input_before_write_ns"] = time.monotonic_ns()
        os.write(masters["manager"], DRAFT.encode())
        result["timing"]["draft_input_after_write_ns"] = time.monotonic_ns()
        result["manager_draft_state"] = wait_state("manager", lambda x: x.get("editorEmpty") is False, 2)
        result["timing"]["draft_state_observed_ns"] = time.monotonic_ns()
        manager_notice_id = str(uuid4())
        manager_notice = {"kind": "notice", "notice": {"type": "worker_terminal_done",
            "notice_id": manager_notice_id, "task_id": str(uuid4()), "status": "finished"}}
        result["timing"]["end_deferred_enqueue_ns"] = time.monotonic_ns()
        result["manager_draft_ack"] = bridge_request("manager", manager_notice)
        result["timing"]["end_deferred_ack_ns"] = time.monotonic_ns()

        worker_peer = peers["worker"]
        active = ActiveRunRef(task_id=str(uuid4()), revision_id=str(uuid4()), revision=1,
            run_id=str(uuid4()), session_id=str(worker_peer["session_id"]),
            session_generation=int(worker_peer["generation"]))
        automation = {"portVersion": 2, "kind": "AutomationState", "payload": {
            "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}
        admission = SerializedReviewAdmission(active, automation)
        now = [0.0]
        sent_notices = []
        review_facts = {"phase": "running", "process_alive": True, "exit_confirmed": False,
            "raw_log_excerpt": "PRIVATE_SCRIPTED_PROCESS_RUNNING", "unknowns": []}

        def worker_state(_run):
            return probe("worker")

        def dispatch_review(review):
            notice_id = str(uuid4())
            frame = {"kind": "notice", "notice": {"type": "terminal_check",
                "notice_id": notice_id, "task_id": review.run.task_id,
                "run_id": review.run.run_id, "due_at": review.due_at}}
            enqueue_ns = time.monotonic_ns()
            ack = bridge_request("worker", frame)
            sent_notices.append({"notice_id": notice_id, "review_due_at": review.due_at,
                "ack": ack, "enqueue_ns": enqueue_ns,
                "ack_observed_ns": time.monotonic_ns(), "sent_ns": time.monotonic_ns()})
            processed = (ack.get("status") == "api_accepted" and
                providers["worker"].notice_post.wait(2) and
                wait_until(lambda: bridge.event_count("worker", "agent_end") >= 1, 2))
            return {"status": "omp_processed" if processed else "deferred"}

        scheduler = WorkerReviewScheduler(admission=admission, automation_state=lambda: automation,
            active_run=lambda: active, worker_state=worker_state,
            collect_non_model=lambda _run: review_facts, dispatch_review=dispatch_review,
            user_priority=lambda _run: False, on_exit=lambda _event: None, clock=lambda: now[0])
        tick0 = scheduler.tick()
        result["timing"]["due_clock_before_injection_ns"] = time.monotonic_ns()
        now[0] = 60.0
        result["timing"]["due_clock_after_injection_ns"] = time.monotonic_ns()
        tick60 = scheduler.tick()
        result["scheduler"] = {"tick0": {"status": tick0.status, "reason": tick0.reason,
            "pending": tick0.pending, "review_count": tick0.review_count},
            "tick60": {"status": tick60.status, "reason": tick60.reason,
                "pending": tick60.pending, "review_count": tick60.review_count},
            "notice_dispatches": sent_notices, "clock_injected": [0.0, 60.0],
            "admission_type": type(admission).__name__, "scheduler_type": type(scheduler).__name__}
        result["timing"]["draft_clear_before_write_ns"] = time.monotonic_ns()
        os.write(masters["manager"], b"\x15")  # real TUI Ctrl-U clears the unsent editor
        result["timing"]["draft_clear_after_write_ns"] = time.monotonic_ns()
        result["manager_clear_state"] = wait_state("manager", lambda x: x.get("editorEmpty") is True, 2)
        result["timing"]["clear_state_observed_ns"] = time.monotonic_ns()
        result["timing"]["end_accepted_enqueue_ns"] = time.monotonic_ns()
        result["manager_accept_ack"] = bridge_request("manager", manager_notice)
        result["timing"]["end_accepted_ack_ns"] = time.monotonic_ns()
        if not providers["manager"].notice_post.wait(2) or not providers["worker"].notice_post.wait(2):
            raise TimeoutError("normal parent notice POST missing before child release")
        result["both_notice_posts_before_release_ns"] = time.monotonic_ns()
        providers["manager"].release_child.set()
        result["child_release_signal_ns"] = time.monotonic_ns()
        result["timing"]["child_release_signal_ns"] = result["child_release_signal_ns"]
        if not wait_until(lambda: any(x.get("event") == "done" for x in events(trace)), 15):
            raise TimeoutError("budget child completion missing")
        typed_path = private / "typed-result.json"
        if typed_path.exists():
            typed = parse_summary_result(json.loads(typed_path.read_text()))
            repo = TaskRepository(private / "backend.sqlite3")
            try:
                result["typed_contract"] = {"bytes": typed_path.stat().st_size, "sha256": digest(typed_path),
                    "bound": typed["requestId"] == request["requestId"] and
                    typed["inputKey"] == request["inputKey"] and typed["sources"] == request["sources"],
                    "accepted": repo.record_summary_result_receipt(typed, str(uuid4()))}
            finally:
                repo.close()
        else:
            result["typed_contract"] = {"missing": True, "bound": False, "accepted": False}
        result["parent_end_observed"] = wait_until(lambda: all(
            bridge.event_count(role, "agent_end") >= 1 for role in providers), 8)
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        providers["manager"].release_child.set()
        for role, proc in procs.items():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for role, proc in procs.items():
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            info = result["processes"][role]
            info["returncode"] = proc.returncode
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                info["pgid_absent"] = True
            else:
                info["pgid_absent"] = False
        for role, (worker, stop) in drains.items():
            stop.set()
            worker.join(timeout=2)
            result["processes"][role]["pump_stopped"] = not worker.is_alive()
        for master in masters.values():
            os.close(master)
        if bridge is not None:
            with bridge.condition:
                result["bridge"]["events"] = list(bridge.events)
            result["timing"]["parent_turn_events"] = [{"role": event.get("role"),
                "name": event.get("name"), "observed_ns": round(event["observedAt"] * 1_000_000_000)}
                for event in result["bridge"]["events"]
                if event.get("name") in ("agent_start", "provider_request_started", "agent_end")
                and isinstance(event.get("observedAt"), (int, float))]
            bridge.shutdown()
            bridge.server_close()
        if bridge_thread is not None:
            bridge_thread.join(timeout=2)
        result["bridge"]["thread_stopped"] = bridge_thread is None or not bridge_thread.is_alive()
        for role, provider in providers.items():
            if role in threads:
                provider.shutdown()
            provider.server_close()
            if role in threads:
                threads[role].join(timeout=2)
            result["providers"][role] = {"posts": provider.posts,
                "violation": provider.violation,
                "thread_stopped": role not in threads or not threads[role].is_alive()}

    result["budget_trace"] = events(trace)
    result["wrapper_trace"] = events(wrapper_trace)
    if (private / "summary.sqlite3").exists():
        with sqlite3.connect(private / "summary.sqlite3") as db:
            result["db_rows"] = db.execute("SELECT id, summary FROM summaries ORDER BY id").fetchall()
    else:
        result["db_rows"] = []
    posts = [x for p in result["providers"].values() for x in p["posts"]]
    preflights = [x for x in result["budget_trace"] if x.get("event") == "dispatch_preflight"]
    selection = next((x for x in result["budget_trace"] if x.get("event") == "selection"), {})
    result["input_budget"] = {"selected_bytes": selection.get("selectedBytes"),
        "collected_bytes": selection.get("collectedBytes"), "selection_max": selection.get("selectionMax"),
        "dispatch_preflights": preflights,
        "headroom_bytes": [x.get("inputLimit", 0) - x.get("contextBytes", 0) for x in preflights],
        "tokenizer_meaning": "unknown encoding; UTF-8 bytes conservative fixture bound"}
    result["checks"] = {
        "two_real_tuis": len(result["processes"]) == 2 and
            len({x["pid"] for x in result["processes"].values()}) == 2,
        "bridge_hello_probe": len(result["bridge"].get("hello", {})) == 2 and
            all(x.get("idle") and x.get("editorEmpty") for x in result["bridge"].get("initial_state", {}).values()),
        "child_db_only": [x["stage"] for x in result["providers"].get("manager", {}).get("posts", [])].count("child_first") == 1 and
            [x["stage"] for x in result["providers"].get("manager", {}).get("posts", [])].count("child_second") == 1 and
            all(x["tools"] == ["db_summary_save"] for x in posts if x["stage"].startswith("child")),
        "manager_draft_deferred": result.get("manager_draft_ack", {}).get("status") == "deferred" and
            result.get("manager_draft_state", {}).get("editorEmpty") is False,
        "scheduler_due_worker_notice": result.get("scheduler", {}).get("tick0", {}).get("status") == "waiting" and
            result.get("scheduler", {}).get("tick60", {}).get("status") == "dispatched" and
            len(result.get("scheduler", {}).get("notice_dispatches", [])) == 1,
        "worker_normal_tools": any(x["stage"] == "parent" and x["tools"] and
            "db_summary_save" not in x["tools"] for x in result["providers"].get("worker", {}).get("posts", [])),
        "both_notice_posts_before_release": "child_release_signal_ns" in result and
            all(any(x["stage"] == "parent" and x["notice_id"] and
                x["received_ns"] < result["child_release_signal_ns"] for x in
                result["providers"].get(role, {}).get("posts", [])) for role in providers),
        "manager_clear_accepted": result.get("manager_accept_ack", {}).get("status") == "api_accepted" and
            result.get("manager_clear_state", {}).get("editorEmpty") is True,
        "budget_preflight_and_wire_cap": len(preflights) == 2 and all(
            x.get("admitted") is True and x.get("fittedCap") == 8192 and
            x.get("contextBytes", 999999) <= x.get("inputLimit", 0) for x in preflights) and
            all(8192 in (x.get("max_completion_tokens"), x.get("max_tokens"))
                for x in posts if x["stage"].startswith("child")),
        "typed_saved": result.get("typed_contract", {}).get("bound") is True and
            result.get("typed_contract", {}).get("accepted") is True and
            result["db_rows"] == [(1, SUMMARY)] and
            any(x.get("event") == "disposed" for x in result["budget_trace"]),
        "all_owned_stopped": all(x.get("pgid_absent") and x.get("pump_stopped")
            for x in result["processes"].values()) and
            all(x.get("thread_stopped") and x.get("violation") is None
                for x in result["providers"].values()) and result["bridge"].get("thread_stopped"),
    }
    result["status"] = "candidate_ready" if not result.get("error") and all(result["checks"].values()) else "partial"
    (out / "runtime-evidence.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    return result


if __name__ == "__main__":
    value = run(Path(sys.argv[1]))
    print(json.dumps({"status": value["status"], "checks": value["checks"],
        "error": value.get("error"), "processes": value["processes"]}, ensure_ascii=False))
    raise SystemExit(0 if value["status"] == "candidate_ready" else 2)
