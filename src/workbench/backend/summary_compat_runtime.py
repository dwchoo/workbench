"""One owned C1 fixture, with bounded startup and truthful partial observations.

Normal native catalog preparation is allowed. No operating user session, auth
store, sandbox, trace facility or development runtime is used by this verifier.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from uuid import uuid4

from workbench.backend.paths import DataLayout, write_private_json
from workbench.contracts.logs_v1 import canonical_json, decode_json, parse_summary_request
from workbench.storage.important_log import ImportantLogCatalog, ImportantLogStore
from workbench.tasks.repository import InvalidTransitionError, TaskRepository

DEADLINE_SECONDS = 90
SUMMARY = {"facts": ["소유 private scripted fixture의 연결을 확인했다."],
           "interpretations": [], "unknowns": ["실제 MODEL 및 전체 Q01은 미검증이다."]}
IO_KEYS = ("omp_children", "model_calls", "network_requests", "auth_store_open", "settings_writes")
PAUSE_NO_SEND_DETAIL = "owner_predicate_false_confirmed_before_send"


def unavailable(reason: str) -> dict[str, Any]:
    return {"level": "unavailable", "observations": [], "items": {},
            "model_metadata": {"status": "inconclusive", "unknowns": [
                "operating_session_unavailable", "output_budget_unavailable"]},
            "unknowns": [reason], "io": dict.fromkeys(IO_KEYS, 0)}


def _identity(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def _remaining(pgid: int) -> list[int]:
    result = []
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            try:
                fields = (path / "stat").read_text().split(") ", 1)[1].split()
                if int(fields[2]) == pgid:
                    result.append(int(path.name))
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
    return result


def _signal_leader(process: subprocess.Popen, ref: dict | None, sig: int) -> bool:
    """Signal only before reap, with a live leader and one immediate identity read."""
    if process.returncode is not None or not ref or ref.get("start_ticks") is None:
        return False
    try:
        fields = Path(f"/proc/{process.pid}/stat").read_text().split(") ", 1)[1].split()
    except FileNotFoundError:
        return False
    if fields[0] in {"Z", "X", "x"} or fields[19] != ref["start_ticks"]:
        return False
    # No poll/wait may reap the leader between the identity read and this signal.
    os.killpg(process.pid, sig)
    return True


def _stop_leader(process: subprocess.Popen, ref: dict | None) -> str | None:
    if process.poll() is not None:
        return None
    if not _signal_leader(process, ref, signal.SIGTERM):
        return "owned_process_identity_unavailable"
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if not _signal_leader(process, ref, signal.SIGKILL):
            return "owned_process_identity_unavailable"
        process.wait(timeout=3)
    return None


def _private_root(root: Path) -> None:
    info = root.lstat()
    if (root.resolve() != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("unsafe_verification_root")


def _environment(root: Path) -> dict[str, str]:
    # Construct rather than filter inherited environment: no provider credentials,
    # proxy, user OMP profile, preload, session or alternate executable hooks.
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(root / "home"),
           "TERM": "dumb", "LANG": "C.UTF-8", "TMPDIR": str(root / "tmp"),
           "PI_CODING_AGENT_DIR": str(root / "agent"), "PI_CONFIG_DIR": str(root / "config"),
           "PYTHONDONTWRITEBYTECODE": "1", "WORKBENCH_SUMMARY_VERIFY_ROOT": str(root),
           "WORKBENCH_SUMMARY_VERIFY_PYTHON": str(Path(sys.executable).resolve())}
    for name in ("CONFIG", "DATA", "STATE", "CACHE"):
        env[f"XDG_{name}_HOME"] = str(root / ("xdg-" + name.lower()))
    return env


def _copy_runtime(source: Path, root: Path) -> Path:
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700)
    for name in ("src/workbench", "omp_bridge", "pyproject.toml"):
        original = source / name
        if not original.exists() or original.is_symlink():
            raise ValueError("runtime_assets_unavailable")
        paths = original.rglob("*") if original.is_dir() else [original]
        if any(path.is_symlink() for path in paths):
            raise ValueError("unsafe_runtime_assets")
        target = runtime / name
        if original.is_dir():
            shutil.copytree(original, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copyfile(original, target)
    for path in runtime.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    if not (runtime / "omp_bridge/g3/summary_verification.ts").is_file():
        raise ValueError("verification_entry_unavailable")
    return runtime


class _Server(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, *args, **kwargs):
        self._accepted_sockets: set[socket.socket] = set()
        self._socket_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        with self._socket_lock:
            self._accepted_sockets.add(connection)
        return connection, address

    def shutdown_request(self, connection):
        try:
            # The request handler remains the owner of close()/makefile lifetime.
            super().shutdown_request(connection)
        finally:
            with self._socket_lock:
                self._accepted_sockets.discard(connection)

    def shutdown_accepted(self):
        with self._socket_lock:
            connections = tuple(self._accepted_sockets)
        for connection in connections:
            try:
                # Unblock header/body recv without closing another thread's fd.
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                # A handler may have already closed this same socket object.
                pass

    def accepted_count(self) -> int:
        with self._socket_lock:
            return len(self._accepted_sockets)


class _RecoveryControl:
    """Owned journal control only; no disconnect, restart or pause claim."""
    def __init__(self, root: Path, *, pause_control: bool = False):
        self.root = root
        self.pause_control = pause_control
        self.request_id: str | None = None
        self.observations: dict[str, Any] = {}

    def before_response(self, expected: dict, body: dict, text) -> None:
        users = [row for row in body.get("messages", []) if row.get("role") == "user"]
        if len(users) != 1:
            raise ValueError("recovery_prompt_binding_mismatch")
        content = users[0].get("content")
        if isinstance(content, str):
            parts = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            parts = content
        else:
            raise ValueError("prompt_envelope_unrecognized")
        matched = []
        other_parts = []
        for part in parts:
            is_text = isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
            raw = part["text"].encode("utf-8") if is_text else canonical_json(part)
            digest = sha256(raw).hexdigest()
            if is_text and len(raw) == expected.get("promptBytes") and digest == expected.get("promptSha256"):
                matched.append(raw)
            else:
                other_parts.append({"type": part.get("type") if isinstance(part, dict) else type(part).__name__,
                                    "bytes": len(raw), "sha256": digest})
        self.observations.update(prompt_matching_parts=len(matched),
                                 sdk_envelope_parts_unattributed=other_parts)
        if len(matched) != 1:
            raise ValueError("prompt_envelope_unrecognized")
        prompt = decode_json(matched[0], max_bytes=32768)
        with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
            record = journal.get_summary_request(expected["requestId"])
            if (not isinstance(prompt, dict) or record is None or record["state"] != "sending"
                    or canonical_json(prompt.get("request")) != canonical_json(record["request"])):
                raise ValueError("recovery_prompt_binding_mismatch")
            self.request_id = expected["requestId"]
            self.observations.update(kind="owner_controlled_recovery", result="inconclusive",
                request_id=self.request_id, request_binding_matches=True, pause_restart="not_run")
            journal.record_summary_state(self.request_id, "unknown",
                                         detail="owner_controlled_unknown_before_response")
            self.observations.update(unknown_before_response=True,
                                     unknown_detail="owner_controlled_unknown_before_response")
            replay = deepcopy(record["request"])
            replay["requestId"] = str(uuid4())
            refused = False
            try:
                journal.reserve_summary_request(replay)
            except InvalidTransitionError:
                refused = True
            present = journal.get_summary_request(replay["requestId"]) is not None
            self.observations.update(replay_request_id=replay["requestId"], replay_reserve_refused=refused,
                                     replay_row_present=present)
            if not refused or present:
                raise ValueError("recovery_replay_reservation_not_refused")

    def after_result(self, completed: dict, catalog: ImportantLogCatalog) -> None:
        if (self.request_id != completed["requestId"]
                or self.observations.get("unknown_before_response") is not True):
            raise ValueError("recovery_control_unobserved")
        outcome = completed["outcome"]
        with TaskRepository(DataLayout(self.root / "data").tasks) as journal:
            if self.pause_control:
                pause = completed.get("pauseControl")
                if (not isinstance(pause, dict) or pause.get("control") != "owner_predicate_false_control"
                        or type(pause.get("predicateCalls")) is not int or pause["predicateCalls"] < 1
                        or pause.get("status") != "deferred" or pause.get("sent") is not False
                        or pause.get("childCreates") != 0 or pause.get("promptStarts") != 0
                        or pause.get("noSendConfirmed") is not True or pause.get("positiveControlSameMetadata") is not True
                        or pause.get("requestId") == self.request_id):
                    raise ValueError("pause_no_send_control_unobserved")
                held = journal.get_summary_request(pause["requestId"])
                fresh = journal.get_summary_request(self.request_id)
                history = [tuple(row) for row in journal._connection.execute(
                    "SELECT state,detail FROM summary_request_events_v1 WHERE request_id=? ORDER BY event_id",
                    (pause["requestId"],))]
                receipt_count = journal._connection.execute(
                    "SELECT COUNT(*) FROM summary_result_receipts_v1 WHERE request_id=?", (pause["requestId"],)).fetchone()[0]
                drop_count = journal._connection.execute(
                    "SELECT COUNT(*) FROM summary_result_drops_v1 WHERE request_id=?", (pause["requestId"],)).fetchone()[0]
                matches = (held is not None and fresh is not None and held["request"]["inputKey"] == fresh["request"]["inputKey"]
                           and held["request"]["sources"] == fresh["request"]["sources"]
                           and held["request"]["sourceGaps"] == fresh["request"]["sourceGaps"])
                self.observations["owner_predicate_false_control"] = {**pause, "state_order": [row[0] for row in history],
                    "detail": PAUSE_NO_SEND_DETAIL, "receipt_count": receipt_count, "drop_count": drop_count,
                    "same_input_and_ranges": matches, "actualAutomationController": "not_run", "pause_store": "not_run", "restart": "not_run"}
                if (not matches or held["state"] != "not_sent" or held["receipt"] is not None
                        or history != [("prepared", None), ("not_sent", PAUSE_NO_SEND_DETAIL)]
                        or receipt_count != 0 or drop_count != 0):
                    raise ValueError("pause_journal_observation_mismatch")
            states = [row[0] for row in journal._connection.execute(
                "SELECT state FROM summary_request_events_v1 WHERE request_id=? ORDER BY event_id",
                (self.request_id,))]
            if states != ["prepared", "sending", "unknown", "stored"]:
                raise ValueError("recovery_state_order_mismatch")
            first_receipt = journal.get_summary_request(self.request_id)["receipt"]
            accepted = journal.record_summary_result_receipt(outcome["result"], outcome["writer"]["entryId"])
            receipts = journal._connection.execute(
                "SELECT COUNT(*) FROM summary_result_receipts_v1 WHERE request_id=?", (self.request_id,)).fetchone()[0]
            drops = journal._connection.execute(
                "SELECT COUNT(*) FROM summary_result_drops_v1 WHERE request_id=?", (self.request_id,)).fetchone()[0]
            after = journal.get_summary_request(self.request_id)
            state_after = after["state"]
            receipt_unchanged = after["receipt"] == first_receipt
            states_after = [row[0] for row in journal._connection.execute(
                "SELECT state FROM summary_request_events_v1 WHERE request_id=? ORDER BY event_id",
                (self.request_id,))]
        with ImportantLogStore.open(DataLayout(self.root / "data").important_logs,
                                    catalog.active_uuid(), readonly=True) as store:
            entries = store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        self.observations.update(state_order=states_after, state_after_duplicate=state_after,
            duplicate_receipt_accepted=accepted, receipt_unchanged=receipt_unchanged,
            receipt_count=receipts, duplicate_drop_count=drops, catalog_entries_after_duplicate=entries)
        if (accepted is not False or receipts != 1 or drops != 1 or entries != 1
                or state_after != "stored" or states_after != states or not receipt_unchanged):
            raise ValueError("recovery_duplicate_observation_mismatch")


def _handler(root: Path, posts: list[dict], violations: list[str], recovery: _RecoveryControl | None = None):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def do_GET(self):
            # Catalog requests are neither MODEL prompts nor attributed C2 I/O.
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32768:
                    raise ValueError("fixture_body_limit")
                raw = self.rfile.read(size)
                body = json.loads(raw)
                tools = [tool.get("function", {}).get("name") for tool in body.get("tools", [])]
                expected = json.loads((root / "expected.json").read_text())
                def text(content):
                    return content if isinstance(content, str) else "".join(
                        part.get("text", "") for part in content or [] if part.get("type") == "text")
                system = "\n".join(text(row.get("content")) for row in body.get("messages", [])
                                   if row.get("role") == "system")
                exact = sha256(system.encode()).hexdigest() == expected["systemSha"]
                posts.append({"path": self.path, "model": body.get("model"), "tools": tools,
                              "body_bytes": size, "body_sha256": sha256(raw).hexdigest(),
                              "system_exact": exact, "max_completion_tokens": body.get("max_completion_tokens")})
                if (self.path != "/v1/chat/completions" or body.get("model") != "scripted"
                        or tools != ["db_summary_save"] or not exact or len(posts) > 2):
                    raise ValueError("unexpected_loopback_POST")
                continuation = any(row.get("role") == "tool" for row in body.get("messages", []))
                if continuation != (len(posts) == 2):
                    raise ValueError("unexpected_continuation")
                if recovery is not None and len(posts) == 1:
                    recovery.before_response(expected, body, text)
                delta = {"role": "assistant", "content": "완료"} if continuation else {
                    "role": "assistant", "tool_calls": [{"index": 0, "id": "owned-save-1",
                    "type": "function", "function": {"name": "db_summary_save",
                    "arguments": json.dumps(SUMMARY, ensure_ascii=False)}}]}
                frames = [{"id": "owned-scripted", "object": "chat.completion.chunk", "created": 0,
                           "model": "scripted", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {"id": "owned-scripted", "object": "chat.completion.chunk", "created": 0,
                           "model": "scripted", "choices": [{"index": 0, "delta": {},
                           "finish_reason": "stop" if continuation else "tool_calls"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}]
                payload = b"".join(("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n").encode()
                                   for frame in frames) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, InvalidTransitionError) as error:
                violations.append(type(error).__name__ + ":" + str(error))
                self.send_error(400)
    return Handler


def _execute(source: Path, binary: Path, root: Path, *, recovery_control: bool = False,
             pause_control: bool = False) -> dict[str, Any]:
    _private_root(root)
    if pause_control and not recovery_control:
        raise ValueError("pause_control_requires_recovery")
    for name in ("home", "agent", "config", "project", "data", "tmp", "xdg-config",
                 "xdg-data", "xdg-state", "xdg-cache"):
        (root / name).mkdir(mode=0o700)
    runtime = _copy_runtime(source, root)
    layout = DataLayout(root / "data")
    with TaskRepository(layout.tasks):
        pass
    layout.tasks.chmod(0o600)
    catalog = ImportantLogCatalog(layout.important_logs, create=True)
    posts: list[dict] = []
    violations: list[str] = []
    recovery = _RecoveryControl(root, pause_control=pause_control) if recovery_control else None
    server = _Server(("127.0.0.1", 0), _handler(root, posts, violations, recovery))
    server.timeout = 1
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    write_private_json(root / "fixture.json", {"root": str(root), "endpoint": endpoint,
        "dbHealth": catalog.health_snapshot()["active"], **({"pauseControl": True} if pause_control else {})})
    models = root / "agent/models.yml"
    models.write_text("providers:\n  ownedverify:\n    api: openai-completions\n    auth: none\n"
        + f"    baseUrl: {endpoint}\n" + "    models:\n      - id: scripted\n        name: scripted\n"
        + "        contextWindow: 131072\n        maxTokens: 8192\n        reasoning: false\n")
    models.chmod(0o600)
    overlay = root / "overlay.yml"
    overlay.write_text("startup:\n  setupWizard: false\n")
    overlay.chmod(0o600)
    env = _environment(root)
    argv = [str(binary), "--thinking", "off", "--mode", "rpc", "--no-session", "--no-title",
            "--no-extensions", "--no-skills", "--no-rules", "--no-pty", "--no-lsp", "--no-prewalk",
            "--no-tools", "--config", str(overlay), "--extension",
            str(runtime / "omp_bridge/g3/summary_verification.ts"), "--model", "ownedverify/scripted",
            "--cwd", str(root / "project")]
    result = unavailable("owned_verification_incomplete")
    process = None
    ref = None
    completed = None
    error = None
    streams = []
    handler_threads = ()
    started = time.monotonic()
    thread.start()
    try:
        streams = [(root / name).open("wb") for name in ("stdout.log", "stderr.log")]
        # If an unexpected exception prevents an observation return after spawn,
        # the wrapper conservatively retains this launch-attempt evidence.
        write_private_json(root / "launch.json", {"launch_requested": True})
        process = subprocess.Popen(argv, cwd=root / "project", env=env, stdin=subprocess.PIPE,
                                   stdout=streams[0], stderr=streams[1], start_new_session=True)
        ref = {"pid": process.pid, "pgid": process.pid, "start_ticks": _identity(process.pid)}
        result["level"] = "inconclusive"
        result["io"] = dict.fromkeys(IO_KEYS)
        result["io"].update(omp_children=1)
        if ref["start_ticks"] is None:
            raise RuntimeError("owned_process_identity_unavailable")
        ref["binary_sha256"] = sha256(binary.read_bytes()).hexdigest()
        write_private_json(root / "launch.json", ref)
        process.stdin.write(b'{"id":"owned-state","type":"get_state"}\n')
        process.stdin.flush()
        while True:
            if (root / "completed.json").exists():
                completed = json.loads((root / "completed.json").read_text())
                break
            if violations:
                raise ValueError(violations[0])
            if process.poll() is not None:
                raise RuntimeError("owned_OMP_exited:" + str(process.returncode))
            if time.monotonic() - started >= DEADLINE_SECONDS:
                raise TimeoutError("owned_verification_timeout")
            time.sleep(0.02)
        if completed.get("outcome", {}).get("status") == "summary_result":
            with TaskRepository(layout.tasks) as journal:
                record = journal.get_summary_request(completed["requestId"])
            with ImportantLogStore.open(layout.important_logs, catalog.active_uuid(), readonly=True) as store:
                entry = store.get_entry(completed["outcome"]["writer"]["entryId"])
                count = store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            completed["writerObserved"] = {"catalog_entries": count, "request_state": record["state"],
                                            "receipt": record["receipt"], "actual_body": entry["body"]}
            request = record["request"]
            outcome = completed["outcome"]
            receipt = record["receipt"]
            binding_matches = (request["requestId"] == completed["requestId"]
                and all(request[name] == outcome["result"][name]
                        for name in ("requestId", "inputKey", "skillDigest", "sources"))
                and entry["subject"] == request["subject"] and entry["sources"] == request["sources"]
                and entry["sourceGaps"] == request["sourceGaps"])
            receipt_matches = (receipt is not None and receipt["request_id"] == completed["requestId"]
                and receipt["entry_id"] == outcome["writer"]["entryId"]
                and receipt["result_digest"] == sha256(canonical_json(outcome["result"])).hexdigest())
            completed["writerObserved"].update(request_binding_matches=binding_matches,
                                               receipt_matches=receipt_matches)
            if (record["state"] != "stored" or not receipt_matches or not binding_matches or count != 1
                    or entry["body"] != {"schemaVersion": 1,
                        "facts": ["소유 private scripted fixture의 연결을 확인했다."],
                        "interpretations": [], "unknowns": ["실제 MODEL 및 전체 Q01은 미검증이다."]}):
                raise ValueError("owned_writer_observation_mismatch")
            if recovery is not None:
                recovery.after_result(completed, catalog)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, TimeoutError, sqlite3.Error, InvalidTransitionError) as failure:
        error = type(failure).__name__ + ":" + str(failure)
    finally:
        if process is not None:
            try:
                cleanup_error = _stop_leader(process, ref)
                if cleanup_error:
                    error = cleanup_error
            except ProcessLookupError:
                error = "owned_cleanup_identity_lost"
            except (OSError, subprocess.TimeoutExpired) as failure:
                error = "owned_cleanup_incomplete:" + str(failure)
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        for stream in streams:
            stream.close()
        server.shutdown()
        server.shutdown_accepted()
        # ThreadingMixIn owns and joins this non-daemon request thread list.
        if isinstance(server._threads, list):
            handler_threads = tuple(server._threads)
        server.server_close()
        thread.join(timeout=3)
    remaining = _remaining(ref["pgid"]) if ref else []
    handlers_alive = sum(handler.is_alive() for handler in handler_threads)
    sockets_remaining = server.accepted_count()
    cleanup_complete = (not remaining and not thread.is_alive() and not handlers_alive and not sockets_remaining
                        and (process is None or process.returncode is not None)
                        and not (error or "").startswith(("owned_cleanup", "owned_process_identity")))
    # Even a successful local C1 control has no complete Q01 item or C2 proof.
    if process is not None or not cleanup_complete:
        result["level"] = "inconclusive"
        result["io"].update(model_calls=len(posts), network_requests=None)
        result["unknowns"] = ["complete_q01_unverified", "observer_child_io_unattributed",
                              "operating_session_unavailable", "output_budget_unavailable"]
        result["observations"] = [{"kind": "owned_C1_partial", "result": "inconclusive",
            "owned_process": ref, "remaining_pgid_members": remaining, "cleanup_complete": cleanup_complete,
            "server_thread_alive": thread.is_alive(),
            "http_handler_threads_alive": handlers_alive, "http_sockets_remaining": sockets_remaining,
            "localhost_model_posts": posts, "credential_environment_entries": 0,
            "completed": completed, "error": error, "elapsed_seconds": time.monotonic() - started}]
        if recovery is not None:
            result["observations"][0]["recovery_control"] = recovery.observations
        if completed:
            exercised = completed.get("exercised", [])
            result["items"] = {name: "inconclusive" for name in exercised}
            if completed.get("error"):
                result["unknowns"].append(completed["error"])
            outcome = completed.get("outcome", {})
            if outcome.get("reason"):
                result["unknowns"].append(outcome["reason"])
            for key in ("parentUnchanged", "registryUnchanged", "settingsUnchanged", "childDisposed"):
                if completed.get(key) is False:
                    result["unknowns"].append(key + "_false")
        if recovery is not None and recovery.observations:
            result["items"]["Q01-recovery"] = "inconclusive"
        if error:
            result["unknowns"].append(error)
        if not cleanup_complete:
            result["unknowns"].append("owned_cleanup_incomplete")
            result["observations"][0]["retained_private_root"] = str(root)
    elif error:
        result["unknowns"] = [error]
    write_private_json(root / "observation.json", result)
    return result


def observe_owned(source: Path, *, recovery_control: bool = False, pause_control: bool = False) -> dict[str, Any]:
    binary_name = shutil.which("omp")
    if not binary_name or sys.platform != "linux":
        return unavailable("owned_runtime_unavailable")
    binary = Path(binary_name).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return unavailable("owned_runtime_unavailable")
    temporary = None
    result = None
    try:
        temporary = Path(tempfile.mkdtemp(prefix="workbench-summary-verify-")).resolve()
        options = {"recovery_control": recovery_control}
        if pause_control:
            options["pause_control"] = True
        result = _execute(source, binary, temporary, **options)
        return result
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, InvalidTransitionError) as error:
        result = unavailable(type(error).__name__ + ":" + str(error))
        if temporary is not None and (temporary / "launch.json").exists():
            # A returned exception cannot turn an attempted launch into zero I/O.
            result.update(level="inconclusive", io=dict.fromkeys(IO_KEYS), observations=[{
                "kind": "owned_C1_partial", "result": "inconclusive", "cleanup_complete": False,
                "retained_private_root": str(temporary), "error": result["unknowns"][0]}])
            result["unknowns"].append("owned_cleanup_incomplete")
        return result
    finally:
        # An ambiguous cleanup retains private evidence; no numeric orphan kill.
        if temporary is not None and (result is not None or not (temporary / "launch.json").exists()):
            retained = any(row.get("retained_private_root") for row in (result or {}).get("observations", []))
            if not retained:
                shutil.rmtree(temporary)


def _reserve(root: Path) -> None:
    # Only the product-owned entry uses this bounded stdin/file seam. No code or
    # database destination comes from the scripted model tool arguments.
    _private_root(root)
    if Path(os.environ.get("HOME", "")).resolve() != root / "home":
        raise ValueError("unsafe_verification_root")
    request = decode_json(sys.stdin.buffer.read(32769), max_bytes=32768)
    with TaskRepository(DataLayout(root / "data").tasks) as journal:
        journal.reserve_summary_request(request)
        journal.record_summary_state(request["requestId"], "sending")


def _pause_request(root: Path) -> dict:
    _private_root(root)
    if Path(os.environ.get("HOME", "")).resolve() != root / "home":
        raise ValueError("unsafe_verification_root")
    return parse_summary_request(decode_json(sys.stdin.buffer.read(32769), max_bytes=32768))


def _hold(root: Path) -> None:
    request = _pause_request(root)
    with TaskRepository(DataLayout(root / "data").tasks) as journal:
        journal.reserve_summary_request(request)


def _confirm_not_sent(root: Path) -> None:
    request = _pause_request(root)
    with TaskRepository(DataLayout(root / "data").tasks) as journal:
        stored = journal.get_summary_request(request["requestId"])
        if (stored is None or stored["state"] != "prepared" or stored["receipt"] is not None
                or canonical_json(stored["request"]) != canonical_json(request)):
            raise ValueError("pause_confirmation_binding_mismatch")
        journal.record_summary_state(request["requestId"], "not_sent", detail=PAUSE_NO_SEND_DETAIL)


if __name__ == "__main__":
    modes = {"--reserve": _reserve, "--hold": _hold, "--confirm-not-sent": _confirm_not_sent}
    if len(sys.argv) != 3 or sys.argv[1] not in modes:
        raise SystemExit(2)
    modes[sys.argv[1]](Path(sys.argv[2]))
