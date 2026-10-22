"""Live checks for the command-stop feature (CS-10). Opt-in via environment variables, never in default discovery.

Four groups, each its own gated or ungated class (see the section comments below):

* ``OmpSchemaAcceptanceTests`` (CS-G12, ``WB_LIVE_COMMAND_STOP_SCHEMA=1``): does the real installed OMP accept and
  forward the command-stop tool schemas?
* ``RealTimeStopTests`` (CS-G10, ``WB_LIVE_COMMAND_STOP_TIMING=1``): the production stage timers on a real Backend.
* ``BridgeSchemaFrameTests``, ``BridgeBackendCrossCheckTests``, ``InputFrameCtrlCTests``: the committed bridge schema and
  the frames it allows, through ``Backend._tool_request`` and ``Backend.admit`` (not gated; fake OMPs).
* ``AuditCompletenessTests`` (CS-G11): the audit records of one combined scenario (not gated; fake OMPs).

Part 1 (CS-G12):

The installed OMP (``omp`` on PATH or ``WB_OMP``; the version is recorded, never pinned) is started exactly as the
backend builds it (``omp_home.prepare_omp_home`` -> ``launcher.omp_environment`` -> ``launcher.role_overlay`` ->
``launcher.write_role_overlay`` -> ``launcher.omp_command``) in an isolated HOME under a throw-away directory,
against a LOCAL fake OpenAI-responses provider (127.0.0.1, dummy key; no real provider, no network, no credential).
The fake provider answers every request with HTTP 500 and captures every request body; the tests read the tool
definitions OMP actually sends:

* Worker session: ``terminal`` carries ``stop`` (nullable, not required; ``command`` still required, nullable
  string); the Worker has no ``stop_command``.
* Manager session: ``stop_command`` with the strict schema ``{command_id (uuid), reason (1..500)}``, both required,
  no extra properties; the Manager has no ``terminal``.

Expectations come from docs/features/command-stop/SPEC.md ("Worker: terminal tool stop argument", "Manager:
stop_command tool"), not from the implementation.  If the OMP binary is missing or cannot start, the tests skip /
fail with the exact reason; no other evidence substitutes (ticket CS-10: report ``not_run``).

Run: WB_LIVE_COMMAND_STOP_SCHEMA=1 PYTHONPATH=src:. .venv/bin/python -m unittest tests/integration/live_command_stop.py

The real-time and audit scenarios (CS-G10, CS-G11) are separate classes further down in this module.
"""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from workbench.backend import launcher, omp_home

OMP = os.environ.get("WB_OMP") or shutil.which("omp")  # no fixed path: absent means the CS-G12 class skips
SCHEMA_GATE = "WB_LIVE_COMMAND_STOP_SCHEMA"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ROUTING_HINT = "Compress into one routing hint"
REASON_MAX = 500


def _tool_name(tool):
    return (tool.get("function") or tool).get("name") or tool.get("type")


def _tool_parameters(tool):
    return (tool.get("function") or tool).get("parameters")


class CapturingProvider:
    """127.0.0.1 only; every request body captured; every request answered with HTTP 500 (no model output)."""

    def __init__(self):
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        provider = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                with provider.lock:
                    provider.requests.append(body)
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"error":"fake provider"}')

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def main_requests(self):
        """Requests of the main session (the ones that carry a tool list and are not OMP's own routing-hint call)."""
        with self.lock:
            return [r for r in self.requests
                    if r.get("tools") and ROUTING_HINT not in json.dumps(r.get("input"))]


# OMP (observed 18.11.1) adds its own ``i`` ("concise intent") property, and lists it as required, on every tool it
# sends; it is OMP's harness field, not part of the Workbench contract, so comparisons ignore it.
OMP_INJECTED = {"i"}


def _own_properties(schema):
    return {name: value for name, value in (schema.get("properties") or {}).items() if name not in OMP_INJECTED}


def _own_required(schema):
    return [name for name in schema.get("required") or [] if name not in OMP_INJECTED]


def _types(schema):
    kind = schema.get("type")
    return set(kind) if isinstance(kind, list) else {kind} if kind else set()


def _nullable(schema):
    if "null" in _types(schema):
        return True
    return any("null" in _types(branch) for branch in schema.get("anyOf") or schema.get("oneOf") or [])


@unittest.skipUnless(os.environ.get(SCHEMA_GATE) == "1",
                     f"live OMP schema acceptance (CS-G12) is opt-in: set {SCHEMA_GATE}=1")
@unittest.skipUnless(OMP, "no omp binary: neither WB_OMP is set nor omp is on PATH (CS-G12 is not_run)")
class OmpSchemaAcceptanceTests(unittest.TestCase):
    captured: dict[str, dict] = {}   # role -> {"request": body, "tools": {name: tool}}

    @classmethod
    def setUpClass(cls):
        cls.version = launcher.omp_version(OMP)
        cls.base = Path(tempfile.mkdtemp(prefix="wb-cs-g12-", dir=os.environ.get("WB_LIVE_TMP") or None))
        cls.captured = {}
        sys.stderr.write(f"\n[CS-G12] omp binary={OMP} version={cls.version} tmp={cls.base}\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def capture(self, role: str, *, budget: float = 60.0) -> dict:
        """Start the real OMP for ``role`` against the fake provider; return the first main-session request."""
        if role in self.captured:
            return self.captured[role]
        root = self.base / role
        home = root / "home"
        project = root / "project"
        home.mkdir(parents=True)
        project.mkdir()
        provider = CapturingProvider()
        self.addCleanup(provider.close)
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "dumb",
               "LANG": "C.UTF-8"}
        wb_home = omp_home.prepare_omp_home(home / "data", env, skills_dir=launcher.default_skills_dir(),
                                            provider_ids=launcher.ISOLATION_PROVIDER_IDS)
        (wb_home.agent_dir / "models.yml").write_text(
            "providers:\n  openai-codex:\n"
            f"    baseUrl: http://127.0.0.1:{provider.port}/v1\n"
            "    api: openai-responses\n    apiKey: dummy-not-a-secret\n    models:\n"
            "      - id: gpt-6-luna\n        name: Luna\n        reasoning: true\n"
            "      - id: gpt-6.1-sol\n        name: Sol\n        reasoning: true\n")
        plan = launcher.LaunchPlan(None, OMP, self.version, str(launcher.default_bridge_extension()))
        omp_env = launcher.omp_environment(env, plan, role=role, token="t", bridge_socket=root / "absent.sock",
                                           home=wb_home.environment())
        overlay = launcher.write_role_overlay(root, role, launcher.role_overlay(
            role, project_dir=project, home=home, environment=omp_env))
        command = launcher.omp_command(plan, overlay)
        proc = subprocess.Popen(command + ["--mode", "rpc", "--no-session"], cwd=project, env=omp_env,
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                start_new_session=True)

        def stop():
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # our own process group only
            except OSError:
                pass
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                pass

        self.addCleanup(stop)
        proc.stdin.write(b'{"id":"p","type":"prompt","message":"please look around"}\n')
        proc.stdin.flush()
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline and not provider.main_requests() and proc.poll() is None:
            time.sleep(0.2)
        requests = provider.main_requests()
        stop()
        if not requests:
            self.fail(f"not_run: {role}: OMP sent no provider request within {budget:.0f}s "
                      f"(exit={proc.returncode}); stderr={proc.stderr.read()[:800]!r}")
        body = requests[0]
        tools = {_tool_name(t): t for t in body["tools"]}
        entry = {"request": body, "tools": tools, "model": body.get("model")}
        type(self).captured[role] = entry
        sys.stderr.write(f"[CS-G12] {role}: model={body.get('model')} tools={sorted(tools)}\n")
        for name in ("terminal", "stop_command"):
            if name in tools:
                sys.stderr.write(f"[CS-G12] {role}.{name} tool = "
                                 f"{json.dumps(tools[name], sort_keys=True, ensure_ascii=False)}\n")
        return entry

    # -- Worker: terminal.stop --------------------------------------------------------------------
    def test_worker_terminal_tool_carries_a_nullable_optional_stop_argument(self):
        worker = self.capture("worker")
        terminal = worker["tools"].get("terminal")
        self.assertIsNotNone(terminal, f"worker request lacks terminal: {sorted(worker['tools'])}")
        schema = _tool_parameters(terminal)
        self.assertIsInstance(schema, dict, "terminal has no parameters schema")
        properties = _own_properties(schema)
        self.assertIn("stop", properties, f"terminal.stop missing: {sorted(properties)}")
        self.assertIn("command", properties)
        stop = properties["stop"]
        self.assertTrue(_nullable(stop), f"terminal.stop must be nullable (SPEC): {stop}")
        self.assertIn("string", _types(stop) | {t for b in stop.get("anyOf", []) for t in _types(b)},
                      f"terminal.stop is a command_id string: {stop}")
        required = _own_required(schema)
        self.assertIn("command", required, f"terminal.command stays required: {required}")
        self.assertNotIn("stop", required, f"terminal.stop must not be required (existing {{command}}-only calls "
                                           f"stay valid): {required}")
        command = properties["command"]
        self.assertTrue(_nullable(command), f"terminal.command stays nullable (state query): {command}")
        self.assertIs(schema.get("additionalProperties"), False)
        self.assertEqual(set(properties), {"command", "stop"}, f"terminal parameters: {sorted(properties)}")

    def test_worker_has_no_stop_command_tool(self):
        worker = self.capture("worker")
        self.assertNotIn("stop_command", worker["tools"], "stop_command is Manager-only (SPEC)")
        self.assertIn("terminal", worker["tools"])

    # -- Manager: stop_command -----------------------------------------------------------------
    def test_manager_stop_command_tool_has_the_strict_command_id_reason_schema(self):
        manager = self.capture("manager")
        tool = manager["tools"].get("stop_command")
        self.assertIsNotNone(tool, f"manager request lacks stop_command: {sorted(manager['tools'])}")
        schema = _tool_parameters(tool)
        self.assertIsInstance(schema, dict)
        self.assertEqual(schema.get("type"), "object")
        self.assertIs(schema.get("additionalProperties"), False, "stop_command schema must be strict")
        properties = _own_properties(schema)
        self.assertEqual(set(properties), {"command_id", "reason"}, sorted(properties))
        self.assertEqual(sorted(_own_required(schema)), ["command_id", "reason"], "both are required")
        command_id = properties["command_id"]
        self.assertEqual(command_id.get("type"), "string", command_id)
        pattern = command_id.get("pattern")
        self.assertIsInstance(pattern, str, f"command_id must carry the uuid pattern: {command_id}")
        compiled = re.compile(pattern)
        self.assertTrue(compiled.search("0b9c7f3e-3a52-4c6e-9d1b-7f8a2c4d5e6f"), "a uuid must match the pattern")
        for bad in ("", "not-a-uuid", "0b9c7f3e3a524c6e9d1b7f8a2c4d5e6f", "0b9c7f3e-3a52-4c6e-9d1b-7f8a2c4d5e6"):
            self.assertIsNone(compiled.search(bad), f"{bad!r} must not match the command_id pattern {pattern}")
        reason = properties["reason"]
        self.assertEqual(reason.get("type"), "string", reason)
        self.assertEqual(reason.get("minLength"), 1, reason)
        self.assertEqual(reason.get("maxLength"), REASON_MAX, reason)

    def test_manager_lacks_terminal_and_keeps_its_recovery_tools(self):
        manager = self.capture("manager")
        self.assertNotIn("terminal", manager["tools"], "terminal is the Worker's tool (SPEC)")
        self.assertIn("stop_command", manager["tools"])
        self.assertIn("stop_survivor", manager["tools"], "existing manager tool unchanged")

    def test_the_two_sessions_were_captured_from_the_recorded_omp_version(self):
        self.assertTrue(self.version, "omp --version produced nothing")
        self.capture("worker")
        self.capture("manager")
        sys.stderr.write(f"[CS-G12] omp version={self.version}\n")



# =====================================================================================================================
# Part 2 (CS-G10 real time, CS-10 items 2 and 4): a real ``Backend`` (fake OMPs, a real host-shell PTY) driven through
# ``Backend._tool_request`` -- the entry the bridge's frames reach -- and ``Backend.admit``.
#
# Real processes, real signals, real time: no clock is mocked and the production ``StopPolicy`` defaults (10/5/5 s,
# call wait 25 s) are used unchanged.  Expectations come from docs/features/command-stop/SPEC.md ("단계와 타이머",
# "결과 계약", "Worker: terminal 도구의 stop 인자", "Manager: stop_command", "사용자 Ctrl+C") and from what the
# commands themselves log ($EPOCHREALTIME at the moment their trap fired) -- never from the production formatter.
# Every process these tests start runs inside the Backend's own host shell (its process group dies with the
# Backend); the pids a test learns of are additionally killed at cleanup, each only while it still has the start
# ticks it had when it was learned (pidfd, re-checked), so a recycled pid is never touched.
# =====================================================================================================================

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from test_cd70_restart_worker import RestartWorkerFixture  # noqa: E402  (the fake-OMP Backend fixture)
from workbench.contracts.v1 import ActorRole, PaneId  # noqa: E402

TIMING_GATE = "WB_LIVE_COMMAND_STOP_TIMING"
# SPEC "단계와 타이머": SIGINT -> 10 s -> SIGTERM -> 5 s -> SIGKILL -> 5 s confirm; identity wait 3 s; call wait 25 s.
SIGINT_STAGE, SIGTERM_STAGE, SIGKILL_STAGE, IDENTITY_WAIT, CALL_WAIT = 10.0, 5.0, 5.0, 3.0, 25.0
# How much later than the signal a command's own trap may log it (bash wakes from ``wait``, runs the trap, echoes).
RECEIPT_JITTER = 0.25
# How late (generously) a stage may fire on a loaded machine.
LATE = 3.0


def _stat(pid: int) -> list[str] | None:
    """Fields 3.. of /proc/<pid>/stat (read by the test itself): [0] state, [1] ppid, [2] pgrp, [19] start ticks."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return None


def _live(pid: int, ticks: int | None = None) -> bool:
    fields = _stat(pid)
    return fields is not None and fields[0] not in ("Z", "X") and (ticks is None or int(fields[19]) == ticks)


def _pids_running(*argv: str) -> list[int]:
    wanted = b"\0".join(item.encode() for item in argv) + b"\0"
    found = []
    for name in os.listdir("/proc"):
        if name.isdecimal():
            try:
                if Path(f"/proc/{name}/cmdline").read_bytes() == wanted and _live(int(name)):
                    found.append(int(name))
            except OSError:
                continue
    return found


def _kill_same(pid: int, ticks: int) -> None:
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if _live(pid, ticks):
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(fd)


# Receipt-logging scripts.  Each line a script writes is "<KIND> <$EPOCHREALTIME>" ("ready <pid> <time>" first), the
# time being when the command's own trap ran -- the command's witness of a signal, independent of the backend.
IGNORE_SIGINT_SCRIPT = r"""log="$1"
trap 'echo INT $EPOCHREALTIME >> "$log"' INT
trap 'echo TERM $EPOCHREALTIME >> "$log"; exit 143' TERM
echo ready $$ $EPOCHREALTIME >> "$log"
while :; do sleep 0.2 & wait $!; done
"""
IGNORE_SIGINT_AND_SIGTERM_SCRIPT = r"""log="$1"
trap 'echo INT $EPOCHREALTIME >> "$log"' INT
trap 'echo TERM $EPOCHREALTIME >> "$log"' TERM
echo ready $$ $EPOCHREALTIME >> "$log"
while :; do echo HB $EPOCHREALTIME >> "$log"; sleep 0.1 & wait $!; done
"""
IGNORING_CHILD_SCRIPT = r"""log="$1"
trap '' INT TERM
bash -c 'trap "" INT TERM; echo child-ready $$ >> "$1"; while :; do sleep 0.2; done' _ "$log" &
echo ready $$ $EPOCHREALTIME >> "$log"
while :; do sleep 0.2; done
"""


class Call:
    """One bridge tool request running on its own thread while the test thread drives the backend loop."""

    def __init__(self, backend, role, tool, args, *, key=None):
        from uuid import uuid4
        self.tool = tool
        peer = backend.bridge.peer(role)
        self.request = {"request_id": str(uuid4()), "tool_call_id": key or f"call-{uuid4()}", "tool": tool,
                        "args": args, "session_id": peer.session_id, "generation": peer.generation}
        self.box: dict = {}

        def run():
            self.box["result"] = backend._tool_request(peer, self.request)
            self.box["ended"] = time.monotonic()
        self.thread = threading.Thread(target=run, daemon=True)
        self.began = time.monotonic()
        self.began_wall = time.time()
        self.thread.start()

    @property
    def done(self) -> bool:
        return "result" in self.box

    @property
    def result(self) -> dict:
        return self.box["result"]

    @property
    def seconds(self) -> float:
        return self.box["ended"] - self.began


class StopBackendFixture(RestartWorkerFixture):
    """A real Backend with fake OMPs and a real host shell; the default production ``StopPolicy`` is left alone."""

    def setUp(self):
        super().setUp()
        self.backend.terminal._wait_seconds = 1
        self.work = Path(self._dir.name) / "work"
        self.work.mkdir()
        self.handoffs_path = self.backend.layout.workflow / "handoffs.jsonl"
        self.pane = self.backend.panes[PaneId.HOST_SHELL]

    # -- calls ------------------------------------------------------------------------------------------------
    def start(self, role, tool, args, *, key=None) -> Call:
        call = Call(self.backend, role, tool, args, key=key)
        self.addCleanup(call.thread.join, 1)
        return call

    def finish(self, call: Call, what: str, timeout: float = 45.0) -> dict:
        self.wait(lambda: call.done, f"the {what} result", timeout=timeout)
        call.thread.join(1)
        return call.result

    def settle(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.backend._tick(0.02)

    def terminal(self, **args) -> dict:
        return self.finish(self.start(ActorRole.WORKER, "terminal", args), "terminal", timeout=20)

    def manager(self, tool, args) -> dict:
        return self.finish(self.start(ActorRole.MANAGER, tool, args), tool, timeout=20)

    # -- commands the test starts -----------------------------------------------------------------------------------
    def begin(self, command: str) -> dict:
        self.wait(lambda: self.pane.state["parent_mode"] == "manual_prompt" and self.pane.state["input_owner"] == "user",
                  "the host shell to be free")
        started = self.terminal(command=command)
        self.assertEqual(started["status"], "running", started)
        self.addCleanup(self._end_command, started["command_id"])
        return started

    def _end_command(self, command_id: str) -> None:
        """Safety net for a failed test: a command that still runs is stopped (idempotent), never left to the shell."""
        try:
            if self.backend.terminal.current().get("running"):
                self.backend.command_stop.stop("worker", command_id)
        except Exception:  # noqa: BLE001 - cleanup must not mask the failure
            pass

    def script(self, name: str, text: str) -> tuple[str, Path]:
        path = self.work / name
        path.write_text(text)
        return f"bash {path} {self.work / (name + '.log')}", self.work / (name + ".log")

    def reap_later(self, pid: int) -> int:
        fields = _stat(pid)
        if fields is not None:
            ticks = int(fields[19])
            self.addCleanup(_kill_same, pid, ticks)
        return pid

    def receipts(self, log: Path) -> list[list[str]]:
        return [line.split() for line in log.read_text().splitlines() if line.strip()] if log.exists() else []

    def ready(self, log: Path, kind="ready") -> int:
        self.wait(lambda: any(row[0] == kind for row in self.receipts(log)), f"{kind} in {log.name}")
        return int(next(row[1] for row in self.receipts(log) if row[0] == kind))

    def times(self, log: Path, kind: str) -> list[float]:
        return [float(row[-1]) for row in self.receipts(log) if row[0] == kind]

    def records(self, *types):
        lines = self.handoffs_path.read_text().splitlines() if self.handoffs_path.exists() else []
        found = [json.loads(line) for line in lines if line.strip()]
        return [r for r in found if r.get("type") in types] if types else found

    def pane_bytes(self) -> bytes:
        return b"".join(chunk.data for chunk in self.pane.replay())

    def report(self, label: str, **values) -> None:
        sys.stderr.write(f"\n[CS-G10] {label}: " + " ".join(
            f"{key}={value:.3f}" if isinstance(value, float) else f"{key}={value}" for key, value in values.items()))


def _signal_names(result: dict) -> list[str]:
    return [item["signal"] for item in result["signals"]]


@unittest.skipUnless(os.environ.get(TIMING_GATE) == "1",
                     f"real-time stop timing (CS-G10) takes about a minute: set {TIMING_GATE}=1")
class RealTimeStopTests(StopBackendFixture):
    def setUp(self):
        super().setUp()
        policy = self.backend.command_stop.policy
        self.assertEqual((policy.identity_wait, policy.sigint_wait, policy.sigterm_wait, policy.sigkill_wait,
                          policy.call_wait),
                         (IDENTITY_WAIT, SIGINT_STAGE, SIGTERM_STAGE, SIGKILL_STAGE, CALL_WAIT),
                         "this gate measures the production default policy (SPEC), not a reduced one")

    def test_a_sigint_ignoring_command_gets_sigterm_ten_seconds_after_sigint_through_the_workers_terminal_stop(self):
        command, log = self.script("ignore_int.sh", IGNORE_SIGINT_SCRIPT)
        started = self.begin(command)
        self.reap_later(self.ready(log))
        call = self.start(ActorRole.WORKER, "terminal", {"command": None, "stop": started["command_id"]})
        result = self.finish(call, "terminal stop")
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["requested_by"], result["command_id"]),
                         (143, "worker", started["command_id"]))
        self.assertEqual(_signal_names(result), ["SIGINT", "SIGTERM"], result["signals"])
        (sigint,), (sigterm,) = self.times(log, "INT"), self.times(log, "TERM")
        gap = sigterm - sigint
        self.report("(a) worker terminal stop, SIGINT ignored", sigint_to_sigterm=gap, call=call.seconds,
                    first_signal_after_call_start=sigint - call.began_wall, exit_code=result["exit_code"])
        self.assertGreaterEqual(gap, SIGINT_STAGE - RECEIPT_JITTER, "SIGTERM waited out the whole SIGINT stage")
        self.assertLess(gap, SIGINT_STAGE + LATE)
        self.assertGreaterEqual(call.seconds, SIGINT_STAGE, "the call waited for the episode")
        self.assertLess(call.seconds, SIGINT_STAGE + LATE + IDENTITY_WAIT)
        self.assertLess(call.seconds, CALL_WAIT, "no `stopping` interim on the normal path")
        self.assertEqual(result["residual"]["alive"], [])

    def test_a_command_ignoring_sigint_and_sigterm_is_killed_five_seconds_after_sigterm_through_the_managers_stop_command(self):
        command, log = self.script("ignore_both.sh", IGNORE_SIGINT_AND_SIGTERM_SCRIPT)
        started = self.begin(command)
        pid = self.reap_later(self.ready(log))
        ticks = int(_stat(pid)[19])
        call = self.start(ActorRole.MANAGER, "stop_command",
                          {"command_id": started["command_id"], "reason": "CS-G10 timing check"})
        result = self.finish(call, "stop_command")
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"], result["signal_name"], result["requested_by"]),
                         (137, 9, "SIGKILL", "manager"), result)
        self.assertEqual(_signal_names(result), ["SIGINT", "SIGTERM", "SIGKILL"], result["signals"])
        (sigint,), (sigterm,) = self.times(log, "INT"), self.times(log, "TERM")
        beats = self.times(log, "HB")
        killed_about = max(beats)  # the command's last heartbeat: it was killed within 0.1 s of that moment
        self.report("(b) manager stop_command, SIGINT+SIGTERM ignored", sigint_to_sigterm=sigterm - sigint,
                    sigterm_to_sigkill=killed_about - sigterm, sigint_to_sigkill=killed_about - sigint,
                    call=call.seconds, exit_code=result["exit_code"], signal_name=result["signal_name"])
        self.assertGreaterEqual(sigterm - sigint, SIGINT_STAGE - RECEIPT_JITTER)
        self.assertLess(sigterm - sigint, SIGINT_STAGE + LATE)
        self.assertGreaterEqual(killed_about - sigterm, SIGTERM_STAGE - RECEIPT_JITTER - 0.1,
                                "SIGKILL waited out the whole SIGTERM stage")
        self.assertLess(killed_about - sigterm, SIGTERM_STAGE + LATE)
        self.assertGreaterEqual(call.seconds, SIGINT_STAGE + SIGTERM_STAGE, "both earlier stages were waited out")
        self.assertLess(call.seconds, SIGINT_STAGE + SIGTERM_STAGE + SIGKILL_STAGE + LATE, "SIGKILL plus its confirm")
        self.assertLess(call.seconds, CALL_WAIT)
        self.assertFalse(_live(pid, ticks), "the command's process is gone")
        self.assertEqual(result["residual"]["alive"], [])

    def test_a_child_that_also_ignores_the_signals_is_killed_with_the_command_and_nothing_is_left(self):
        command, log = self.script("ignoring_child.sh", IGNORING_CHILD_SCRIPT)
        started = self.begin(command)
        parent = self.reap_later(self.ready(log))
        child = self.reap_later(self.ready(log, "child-ready"))
        self.assertEqual(int(_stat(child)[1]), parent, "the ignoring child is the command's child")
        ticks = {pid: int(_stat(pid)[19]) for pid in (parent, child)}
        call = self.start(ActorRole.WORKER, "terminal", {"command": None, "stop": started["command_id"]})
        result = self.finish(call, "terminal stop")
        self.report("(c) ignoring child tree", call=call.seconds, exit_code=result.get("exit_code"),
                    signals=_signal_names(result), residual=result.get("residual"))
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal_name"]), (137, "SIGKILL"), result)
        self.assertEqual(_signal_names(result), ["SIGINT", "SIGTERM", "SIGKILL"])
        for item in result["signals"]:  # every stage reached the command and its child
            self.assertTrue({parent, child} <= {target["pid"] for target in item["targets"]}, item)
        self.assertGreaterEqual(call.seconds, SIGINT_STAGE + SIGTERM_STAGE)
        self.assertLess(call.seconds, SIGINT_STAGE + SIGTERM_STAGE + SIGKILL_STAGE + LATE)
        self.assertEqual(result["residual"]["alive"], [], "residual is empty at the end")
        self.assertEqual(result["residual"]["left_session"], [])
        self.assertTrue(result["residual"]["unchecked"], "`unchecked` is always reported (never a clean claim)")
        for pid, start in ticks.items():
            self.assertFalse(_live(pid, start), f"pid {pid} is gone (read from /proc)")
        self.assertEqual(_pids_running("sleep", "0.2"), [], "no sleeper of this command is left")

    def test_the_host_terminal_stays_occupied_and_nothing_is_typed_into_it_during_the_stop(self):
        command, log = self.script("occupied.sh", IGNORE_SIGINT_SCRIPT)
        started = self.begin(command)
        self.reap_later(self.ready(log))
        command_id = started["command_id"]
        stop = self.start(ActorRole.WORKER, "terminal", {"command": None, "stop": command_id})
        deadline = time.monotonic() + 8
        state = {}
        while state.get("status") != "stopping" and time.monotonic() < deadline:
            state = self.terminal(command=None)
        self.assertEqual((state.get("status"), state.get("command_id")), ("stopping", command_id), state)
        before = self.pane_bytes()
        marker = "never-typed-while-stopping-cs-g10"
        refused = self.terminal(command=f"echo {marker}")
        self.assertEqual((refused["status"], refused.get("command_id")), ("terminal_command_running", command_id),
                         refused)
        self.assertFalse(stop.done, "the stop episode is still under way (the SIGINT stage is 10 s)")
        self.settle(0.5)
        self.assertEqual(self.pane.state["input_owner"], "manager", "the command still holds the host terminal")
        self.assertNotIn(marker.encode(), self.pane_bytes()[len(before):], "nothing was typed into the pane")
        result = self.finish(stop, "terminal stop")
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 143), result)
        self.report("(d) occupancy during stop", observed_status=state["status"], refused=refused["status"],
                    call=stop.seconds)
        self.assertNotIn(marker.encode(), self.pane_bytes(), "the refused command never reached the terminal")
        self.assertNotIn(marker.encode(), Path(started["log_path"]).read_bytes())
        self.wait(lambda: self.pane.state["input_owner"] == "user" and self.pane.state["parent_mode"] == "manual_prompt",
                  "the host shell to be the user's again")
        self.backend.terminal._wait_seconds = 10
        after = self.terminal(command="echo host-terminal-free-again")
        self.assertEqual((after["status"], after["exit_code"]), ("exited", 0), after)

    def test_a_repeat_during_the_episode_joins_it_and_no_second_signal_set_is_sent(self):
        command, log = self.script("repeat.sh", IGNORE_SIGINT_SCRIPT)
        started = self.begin(command)
        self.reap_later(self.ready(log))
        command_id = started["command_id"]
        first = self.start(ActorRole.WORKER, "terminal", {"command": None, "stop": command_id})
        deadline = time.monotonic() + 8
        while self.terminal(command=None).get("status") != "stopping" and time.monotonic() < deadline:
            pass
        self.settle(2.0)
        self.assertFalse(first.done)
        manager = self.start(ActorRole.MANAGER, "stop_command", {"command_id": command_id, "reason": "repeat"})
        worker = self.start(ActorRole.WORKER, "terminal", {"command": None, "stop": command_id})
        original = self.finish(first, "first stop")
        by_manager, by_worker = self.finish(manager, "repeat stop_command"), self.finish(worker, "repeat terminal stop")
        self.report("(e) concurrent repeat", first_call=first.seconds, manager_repeat_call=manager.seconds,
                    worker_repeat_call=worker.seconds,
                    repeat_ended_after_first=max(manager.box["ended"], worker.box["ended"]) - first.box["ended"])
        self.assertEqual((original["status"], original["exit_code"], original["requested_by"]),
                         ("exited", 143, "worker"), original)
        self.assertNotIn("repeat", original)
        for repeat in (by_manager, by_worker):
            self.assertIs(repeat.get("repeat"), True, repeat)
            self.assertEqual((repeat["status"], repeat["exit_code"], repeat["requested_by"], repeat["command_id"]),
                             ("exited", 143, "worker", command_id), "the repeat reports the original episode")
            self.assertEqual(_signal_names(repeat), ["SIGINT", "SIGTERM"], "the same signal set, not a new one")
        self.assertLess(manager.seconds, first.seconds, "the repeat joined an episode already under way")
        self.assertLess(abs(manager.box["ended"] - first.box["ended"]), 2.0)
        self.assertLess(abs(worker.box["ended"] - first.box["ended"]), 2.0)
        self.assertEqual(len(self.times(log, "INT")), 1, "the command itself received SIGINT once")
        self.assertEqual(len(self.times(log, "TERM")), 1, "and SIGTERM once")
        self.assertEqual([r["signal"] for r in self.records("command_stop_signal")], ["SIGINT", "SIGTERM"])


# -- CS-10 item 2: the frames the committed bridge schema allows, fed straight to Backend._tool_request ---------------

REPO = Path(__file__).resolve().parents[2]
SCHEMA_DUMP = (
    "const { default: extension } = await import(process.argv[1]);"
    "const tools = {};"
    "extension({ on() {}, registerTool(tool) { tools[tool.name] = tool.parameters; },"
    " async sendUserMessage() {}, logger: { error() {} } });"
    "console.log(JSON.stringify(tools));")


def bridge_schemas(role: str) -> dict[str, dict]:
    """The tool parameter schemas the committed bridge registers for ``role`` (read by loading the extension)."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
           "WORKBENCH_G3_ROLE": role, "WORKBENCH_G3_TOKEN": "cross-check", "WORKBENCH_G3_GENERATION": "1",
           "WORKBENCH_G3_BRIDGE_SOCKET": "/nonexistent/bridge.sock"}  # the extension is never started: no connect
    done = subprocess.run([shutil.which("node"), "--input-type=module", "-e", SCHEMA_DUMP,
                           str(REPO / "omp_bridge" / "g3" / "bridge.ts")], capture_output=True, text=True,
                          timeout=60, env=env, cwd=tempfile.gettempdir())
    if done.returncode:
        raise RuntimeError(f"cannot read the bridge schemas: {done.stderr[-400:]}")
    return json.loads(done.stdout)


def schema_problems(schema: dict, value, where="$") -> list[str]:
    """The JSON-Schema subset the bridge schemas use: type, pattern, minLength, maxLength, required, properties,
    additionalProperties (false)."""
    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds] if kinds else []
    names = {str: "string", dict: "object", type(None): "null", bool: "boolean", int: "integer", float: "number",
             list: "array"}
    if kinds and names.get(type(value)) not in kinds:
        return [f"{where}: {type(value).__name__} is not {kinds}"]
    problems = []
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            problems.append(f"{where}: does not match the pattern")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1 << 30):
            problems.append(f"{where}: length {len(value)} out of bounds")
    if isinstance(value, dict):
        problems += [f"{where}.{name}: required" for name in schema.get("required", []) if name not in value]
        properties = schema.get("properties", {})
        for name, item in value.items():
            if name in properties:
                problems += schema_problems(properties[name], item, f"{where}.{name}")
            elif schema.get("additionalProperties") is False:
                problems.append(f"{where}.{name}: not allowed")
    return problems


_SCHEMAS: dict[str, dict] = {}


def committed_schemas() -> dict[str, dict]:
    if not _SCHEMAS:
        if not shutil.which("node"):
            raise unittest.SkipTest("node is required to read the committed bridge schema (not_run)")
        _SCHEMAS.update({role: bridge_schemas(role) for role in ("worker", "manager")})
    return _SCHEMAS


class BridgeSchemaFrameTests(unittest.TestCase):
    """The committed bridge schema allows the frames the cross-check feeds the backend (and the validator is real)."""

    def test_the_validator_is_not_vacuous_and_the_committed_schemas_allow_the_frames_under_test(self):
        from uuid import uuid4
        stop_id = str(uuid4())
        schemas = committed_schemas()
        terminal, stop_command = schemas["worker"]["terminal"], schemas["manager"]["stop_command"]
        for frame in ({"command": "x"}, {"command": None}, {"command": None, "stop": stop_id},
                      {"command": "", "stop": stop_id}, {"command": None, "stop": None}):
            self.assertEqual(schema_problems(terminal, frame), [], frame)
        self.assertEqual(schema_problems(stop_command, {"command_id": stop_id, "reason": "r"}), [])
        for frame in ({"command": None, "stop": "not-a-uuid"}, {"command": None, "force": True}, {"stop": stop_id}):
            self.assertNotEqual(schema_problems(terminal, frame), [], frame)
        for frame in ({"command_id": stop_id}, {"reason": "r"}, {"command_id": stop_id, "reason": ""},
                      {"command_id": "x", "reason": "r"}, {"command_id": stop_id, "reason": "r", "force": True},
                      {"command_id": stop_id, "reason": "r" * 501}):
            self.assertNotEqual(schema_problems(stop_command, frame), [], frame)
        self.assertNotIn("stop_command", schemas["worker"], "stop_command is Manager-only")
        self.assertNotIn("terminal", schemas["manager"], "terminal is the Worker's tool")


class BridgeBackendCrossCheckTests(StopBackendFixture):
    """Frames the committed bridge schema allows go straight into ``Backend._tool_request`` and are processed with the
    SPEC result shapes.  One backend (about ten seconds to start) serves every frame, each in its own subTest."""

    def allowed(self, role: str, tool: str, frame: dict) -> dict:
        problems = schema_problems(committed_schemas()[role][tool], frame)
        self.assertEqual(problems, [], f"the committed {role} {tool} schema must allow {frame}")
        return frame

    def test_every_allowed_frame_is_accepted_and_processed_and_the_legacy_call_still_works(self):
        for name in ("legacy_command_only", "stop_with_null_command", "stop_with_blank_command", "unknown_ids",
                     "command_and_stop_together", "manager_stop_command"):
            with self.subTest(frame=name):
                getattr(self, f"frame_{name}")()

    def frame_legacy_command_only(self):
        self.backend.terminal._wait_seconds = 10
        frame = self.allowed("worker", "terminal", {"command": "echo legacy-cross-check"})
        self.assertNotIn("stop", frame)
        result = self.terminal(**frame)
        self.backend.terminal._wait_seconds = 1
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 0), result)
        self.assertNotIn("stopped_by", result)
        self.assertIn("legacy-cross-check", result["output_tail"])
        state = self.terminal(**self.allowed("worker", "terminal", {"command": None}))
        self.assertEqual((state["status"], state["command_id"]), ("exited", result["command_id"]), state)
        self.report("cross-check legacy {command}", status=result["status"], exit_code=result["exit_code"])

    def frame_stop_with_null_command(self):
        started = self.begin("sleep 7401.1")
        (pid,) = _pids_running("sleep", "7401.1")
        frame = self.allowed("worker", "terminal", {"command": None, "stop": started["command_id"]})
        began = time.monotonic()
        result = self.terminal(**frame)
        self.report("cross-check terminal {command:null, stop}", call=time.monotonic() - began,
                    status=result["status"], exit_code=result.get("exit_code"), signals=_signal_names(result))
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal"], result["signal_name"]), (130, 2, "SIGINT"))
        self.assertEqual((result["command_id"], result["requested_by"]), (started["command_id"], "worker"))
        self.assertEqual(_signal_names(result), ["SIGINT"])
        self.assertEqual(result["residual"]["alive"], [])
        self.assertFalse(_live(pid))
        after = self.terminal(command=None)
        self.assertEqual((after["status"], after["stopped_by"], after["stop_signal"]), ("exited", "worker", "SIGINT"))

    def frame_stop_with_blank_command(self):
        started = self.begin("sleep 7401.2")
        frame = self.allowed("worker", "terminal", {"command": "", "stop": started["command_id"]})
        result = self.terminal(**frame)
        self.report("cross-check terminal {command:'', stop}", status=result["status"],
                    exit_code=result.get("exit_code"))
        self.assertEqual((result["status"], result["exit_code"], result["requested_by"]), ("exited", 130, "worker"),
                         result)
        self.assertFalse(_pids_running("sleep", "7401.2"))

    def frame_unknown_ids(self):
        from uuid import uuid4
        sleeper = self.begin("sleep 7401.3")
        unknown = str(uuid4())
        worker = self.terminal(**self.allowed("worker", "terminal", {"command": None, "stop": unknown}))
        self.assertEqual((worker["status"], worker["reason"], worker["command_id"], worker["requested_by"]),
                         ("not_found", "unknown_command", unknown, "worker"), worker)
        manager = self.manager("stop_command", self.allowed("manager", "stop_command",
                                                            {"command_id": unknown, "reason": "cross-check"}))
        self.assertEqual((manager["status"], manager["reason"], manager["requested_by"]),
                         ("not_found", "unknown_command", "manager"), manager)
        self.report("cross-check unknown id", worker=worker["status"], manager=manager["status"])
        self.assertTrue(_pids_running("sleep", "7401.3"), "nothing was signalled")
        self.assertEqual(self.terminal(command=None)["status"], "running")
        self.assertEqual(self.terminal(command=None, stop=sleeper["command_id"])["status"], "exited")

    def frame_command_and_stop_together(self):
        started = self.begin("sleep 7401.4")
        frame = self.allowed("worker", "terminal",
                             {"command": "echo cross-check-never-run", "stop": started["command_id"]})
        result = self.terminal(**frame)
        self.assertEqual((result["status"], result["reason"]), ("rejected", "invalid_arguments"), result)
        self.assertTrue(any(error.startswith(("stop", "command")) for error in result["errors"]), result)
        self.assertNotIn(started["command_id"], json.dumps(result), "the value sent is not echoed")
        self.assertNotIn(b"cross-check-never-run", self.pane_bytes())
        self.assertTrue(_pids_running("sleep", "7401.4"), "nothing was signalled")
        self.report("cross-check {command:'x', stop} (schema-allowed, SPEC-refused)", status=result["status"],
                    reason=result["reason"])
        self.assertEqual(self.terminal(command=None, stop=started["command_id"])["status"], "exited")

    def frame_manager_stop_command(self):
        started = self.begin("sleep 7401.5")
        (pid,) = _pids_running("sleep", "7401.5")
        frame = self.allowed("manager", "stop_command", {"command_id": started["command_id"],
                                                         "reason": "cross-check of the committed schema"})
        result = self.manager("stop_command", frame)
        self.report("cross-check stop_command", status=result["status"], exit_code=result.get("exit_code"),
                    requested_by=result.get("requested_by"))
        self.assertEqual(result["status"], "exited", result)
        self.assertEqual((result["exit_code"], result["signal_name"], result["requested_by"], result["command_id"]),
                         (130, "SIGINT", "manager", started["command_id"]))
        self.assertFalse(_live(pid))
        requested = [r for r in self.records("command_stop_requested") if r["requested_by"] == "manager"
                     and r["command_id"] == started["command_id"]]
        self.assertEqual([r["reason"] for r in requested], ["cross-check of the committed schema"])
        self.assertEqual(self.terminal(command=None)["stopped_by"], "manager")


# -- CS-10 item 4: a lone Ctrl+C at the `input` frame level (the UI path is out of scope) ----------------------------


class InputFrameCtrlCTests(StopBackendFixture):
    """``Backend.admit(host_shell, b"\\x03", "input")`` on a running Workbench command while the Manager owns input."""

    def running(self, sleeper: str):
        started = self.begin(f"sleep {sleeper}")
        (pid,) = _pids_running("sleep", sleeper)
        fields = _stat(pid)
        self.assertEqual(self.pane.state["input_owner"], "manager", "a Workbench command owns the host terminal input")
        return started, pid, int(fields[19]), int(fields[2])

    def exited(self, command_id: str) -> dict:
        deadline = time.monotonic() + 10
        result = {}
        while time.monotonic() < deadline:
            result = self.terminal(command=None)
            if result.get("status") == "exited":
                break
        self.assertEqual(result.get("command_id"), command_id, result)
        return result

    def test_a_lone_ctrl_c_input_frame_reaches_the_command_while_the_manager_owns_input(self):
        started, pid, ticks, group = self.running("7501.1")
        delivered = self.backend.admit(PaneId.HOST_SHELL, b"\x03", "input")
        self.assertIsNone(delivered, "None means the frame was accepted")
        result = self.exited(started["command_id"])
        self.report("input-frame Ctrl+C", delivered=delivered, status=result["status"], exit_code=result["exit_code"],
                    signal=result.get("signal"))
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), result)
        self.assertFalse(_live(pid, ticks), "the sleep was interrupted")
        (record,) = self.records("command_interrupt")
        self.assertEqual((record["requested_by"], record["command_id"], record["pid"], record["start_ticks"],
                          record["process_group"], record["delivered"], record["input_owner"]),
                         ("user", started["command_id"], pid, ticks, group, True, "manager"), record)
        self.assertTrue(record["at"])
        self.assertEqual(self.pane.state["input_owner"], "user", "the shell is the user's again afterwards")

    def test_paste_and_mixed_bytes_are_rejected_and_the_command_keeps_running(self):
        started, pid, ticks, _ = self.running("7501.2")
        refused = [(b"\x03", "paste"), (b"\x03a", "input"), (b"a\x03", "input"), (b"\x03\x03", "input"),
                   (b"a", "input")]
        for data, kind in refused:
            outcome = self.backend.admit(PaneId.HOST_SHELL, data, kind)
            self.assertIsNotNone(outcome, f"{data!r} as {kind!r} must be refused (no partial bytes)")
            self.settle(0.1)
        self.settle(0.5)
        self.assertTrue(_live(pid, ticks), "no refused frame reached the command")
        self.assertEqual(self.terminal(command=None)["status"], "running")
        self.assertEqual(self.records("command_interrupt"), [], "a refusal leaves no interrupt record")
        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"\x03", "input"), "the lone byte still goes through")
        result = self.exited(started["command_id"])
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 130), result)
        self.assertEqual(len(self.records("command_interrupt")), 1)



# -- CS-10 item 3 (CS-G11, CS-AC-20): audit completeness over one combined scenario ---------------------------------
#
# One real Backend (fake OMPs, a real host shell, the production wiring of ``CommandStopService``/``TaskFlow``) runs, in
# sequence: a Worker ``terminal`` stop, a Manager ``stop_command`` of a Worker command, an unknown stop that a late end
# corrects (S7: the SIGTERM/SIGKILL the service sends "have no effect"), a user's lone Ctrl+C at the ``input`` frame, a
# refusal (the Worker against the Manager's experiment run), an attempt to put an environment value into a stop reason,
# and the Manager's stop of the experiment run (outcome ``stopped``).  Afterwards the handoff journal, the task flow
# ledger and the task repository are read back and every audit item of SPEC "감사 기록" must be queryable, with no
# environment variable value, auth token or command argv in them.  Expectations come from the SPEC record list and from
# what the test itself learned from ``/proc`` and chose (pids, start ticks, secret value, commands) -- not from the
# production formatter.  The reduced ``StopPolicy`` only shortens the waits; the production defaults are measured by
# ``RealTimeStopTests``.

from uuid import uuid4  # noqa: E402
from workbench.backend.command_stop import StopPolicy  # noqa: E402
from workbench.tasks.repository import TaskRepository  # noqa: E402
from test_command_stop_outcomes import OutcomeEnvironment  # noqa: E402  (S7: signals that have no effect)
from test_task_flow import PublicMailboxFixture  # noqa: E402  (a mailbox whose worker instruction is processed)
from workbench.workflow.run import TaskWorkflow  # noqa: E402

AUDIT_POLICY = StopPolicy(identity_wait=3.0, sigint_wait=1.5, sigterm_wait=1.0, sigkill_wait=1.0, call_wait=20.0)
ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=CS10 Audit", "-c", "user.email=audit@example.invalid", *args],
                          cwd=cwd, capture_output=True, text=True, timeout=60, check=True)
    return done.stdout.strip()


class AuditCompletenessTests(StopBackendFixture):
    maxDiff = None

    def setUp(self):
        super().setUp()
        self.stop_env = OutcomeEnvironment()
        self.service = self.backend.command_stop
        self.service.env = self.stop_env
        self.service.policy = AUDIT_POLICY
        self.began_at = time.time()
        # a secret-looking variable in the backend environment; its value is unique to this test
        self.secret = f"wb-audit-secret-{uuid4().hex}"
        self.backend.environment["WB_AUDIT_API_TOKEN"] = self.secret
        # The fake OMPs never process a Task instruction (an experiment would end ``start_failed:WorkflowHeld``), so the
        # experiment runner gets the ``test_experiment_command_stop`` mailbox fixture; everything else -- the host shell,
        # the repository, the ledger, the stop service and its wiring -- is the backend's own.
        self.backend.flow._experiment.make_workflow = lambda repository: TaskWorkflow(
            repository, PublicMailboxFixture(repository))
        self.ledger_path = self.backend.layout.workflow / "tasks-flow.jsonl"
        self.db_path = self.backend.layout.tasks

    # -- scenario steps ------------------------------------------------------------------------------------
    def worker_stop(self, command_id: str) -> dict:
        return self.terminal(command=None, stop=command_id)

    def process(self, sleeper: str) -> tuple[int, int, int, int]:
        """pid, start ticks, process group, session of the one live ``sleep <sleeper>`` (read from /proc)."""
        (pid,) = _pids_running("sleep", sleeper)
        self.reap_later(pid)
        fields = _stat(pid)
        return pid, int(fields[19]), int(fields[2]), int(fields[3])

    def experiment(self) -> tuple[str, str, dict]:
        source = self.work / "source"
        source.mkdir()
        _git(source, "init", "-q")
        (source / "tracked.txt").write_text("base\n")
        _git(source, "add", "tracked.txt")
        _git(source, "commit", "-qm", "baseline")
        spec = {"goal": "audit fixture run", "paths": ["outcome.txt"], "execution": {
            "source": str(source), "commit": _git(source, "rev-parse", "HEAD"),
            "command": "printf 'started\\n'; sleep 7601.7; printf PASS > outcome.txt",
            "criteria": {"log_contains": "PASS", "result_file": "outcome.txt", "result_contains": "PASS"},
            "environment": ["PATH"], "shell": "bash"}}
        self.wait(lambda: self.pane.state["parent_mode"] == "manual_prompt" and self.pane.state["input_owner"] == "user",
                  "the host shell to be free for the experiment")
        created = self.manager("to_worker", {"kind": "experiment", "message": "run the audit fixture", "spec": spec})
        self.assertEqual(created["status"], "dispatched", created)
        self.wait(lambda: (self.backend.flow.task_view().get("run_id") is not None
                           and _pids_running("sleep", "7601.7")),
                  "the experiment command to run (view=%s)" % json.dumps(self.backend.flow.task_view(), default=str),
                  timeout=60)
        view = self.backend.flow.task_view()
        return created["task_id"], view["run_id"], spec["execution"]

    def read_back(self):
        journal = self.records()
        ledger = [json.loads(line) for line in self.ledger_path.read_text().splitlines() if line.strip()]
        return journal, ledger

    def of(self, journal, kind, command_id):
        return [r for r in journal if r.get("type") == kind and r.get("command_id") == command_id]

    # -- the scenario --------------------------------------------------------------------------------------
    def test_every_audit_item_of_a_combined_stop_scenario_is_queryable_and_nothing_secret_is_recorded(self):
        self.backend.terminal._wait_seconds = 1
        self.argv_markers = ["sleep 7601.1", "sleep 7601.2", "sleep 7601.4", "trap '' INT"]  # the commands typed above
        shown = {}  # the audit completeness table: item -> where it was found / its value

        # 1. a Worker `terminal` stop
        a = self.begin("sleep 7601.1")
        a_pid, a_ticks, a_group, a_session = self.process("7601.1")
        a_result = self.worker_stop(a["command_id"])
        self.assertEqual((a_result["status"], a_result["exit_code"], a_result["signal_name"]), ("exited", 130, "SIGINT"))
        self.assertFalse(_live(a_pid, a_ticks))

        # 2. the Manager's `stop_command` of a Worker command
        b = self.begin("sleep 7601.2")
        b_pid, b_ticks, _, _ = self.process("7601.2")
        b_result = self.manager("stop_command", {"command_id": b["command_id"], "reason": "audit: manager stops it"})
        self.assertEqual((b_result["status"], b_result["exit_code"], b_result["requested_by"]),
                         ("exited", 130, "manager"), b_result)
        self.assertFalse(_live(b_pid, b_ticks))

        # 3. unknown (SIGTERM/SIGKILL have no effect, S7), then the owner observes the end: corrected
        self.stop_env.ignored = {signal.SIGTERM, signal.SIGKILL}
        c = self.begin("bash -c \"trap '' INT; exec sleep 7601.3\"")
        c_pid, c_ticks, c_group, c_session = self.process("7601.3")
        c_unknown = self.worker_stop(c["command_id"])
        self.assertEqual((c_unknown["status"], c_unknown["reason"]), ("unknown", "still_alive_after_sigkill"), c_unknown)
        self.assertTrue(_live(c_pid, c_ticks), "the process really survived (the injected signals did nothing)")
        self.stop_env.ignored = set()
        _kill_same(c_pid, c_ticks)  # the test ends its own process; the owner then observes the end
        self.wait(lambda: self.records("command_stop_corrected"), "the correction record")
        c_corrected = self.worker_stop(c["command_id"])
        self.assertEqual((c_corrected["status"], c_corrected.get("corrected"), c_corrected["exit_code"],
                          c_corrected["signal"]), ("exited", True, 137, 9), c_corrected)
        self.assertEqual([(h["status"], h["reason"]) for h in c_corrected["history"]],
                         [("unknown", "still_alive_after_sigkill")])
        self.wait(lambda: self.pane.state["input_owner"] == "user" and self.pane.state["parent_mode"] == "manual_prompt",
                  "the host shell to be the user's again")

        # 4. a user's lone Ctrl+C at the `input` frame
        d = self.begin("sleep 7601.4")
        d_pid, d_ticks, d_group, _ = self.process("7601.4")
        self.assertIsNone(self.backend.admit(PaneId.HOST_SHELL, b"\x03", "input"))
        deadline = time.monotonic() + 15
        d_end = {}
        while d_end.get("status") != "exited" and time.monotonic() < deadline:
            d_end = self.terminal(command=None)
        self.assertEqual((d_end["command_id"], d_end["exit_code"]), (d["command_id"], 130), d_end)
        self.assertFalse(_live(d_pid, d_ticks))
        self.wait(lambda: self.pane.state["input_owner"] == "user" and self.pane.state["parent_mode"] == "manual_prompt",
                  "the host shell to be the user's again")

        # 5. the Manager's experiment run: a refusal (Worker, not_owner), an environment value in a reason, the stop
        task_id, run_id, execution = self.experiment()
        sleeper = self.process("7601.7")  # a member of the experiment command (its sleep)
        main = self.pane.shell.run_identity()["child"]["pid"]  # the experiment command's own process, from the shell
        self.reap_later(main)
        main_fields = _stat(main)
        e_pid, e_ticks, e_group, e_session = main, int(main_fields[19]), int(main_fields[2]), int(main_fields[3])
        self.assertEqual(int(_stat(sleeper[0])[1]), main, "the sleep is the experiment command's child")
        refused = self.worker_stop(run_id)
        self.assertEqual((refused["status"], refused["reason"], refused["requested_by"]),
                         ("refused", "not_owner", "worker"), refused)
        leaked = self.manager("stop_command", {"command_id": run_id, "reason": f"because {self.secret}"})
        self.assertEqual((leaked["status"], leaked["reason"]), ("rejected", "environment_value"), leaked)
        self.assertNotIn(self.secret, json.dumps(leaked), "the rejection does not echo the value")
        self.assertTrue(_live(e_pid, e_ticks), "neither the refusal nor the rejected reason signalled anything")
        e_result = self.manager("stop_command", {"command_id": run_id, "reason": "audit: the run is not needed"})
        self.assertEqual((e_result["status"], e_result["requested_by"], e_result["exit_code"]),
                         ("exited", "manager", 130), e_result)
        self.wait(lambda: self.backend.flow.task_view()["status"] == "finished", "the stopped run to close", timeout=60)
        last_result = self.backend.flow.task_view()["last_result"]
        self.settle(0.3)
        self.report("scenario done", seconds=time.time() - self.began_at)

        journal, ledger = self.read_back()
        self.check_worker_stop(journal, a, a_result, (a_pid, a_ticks, a_group, a_session), shown)
        self.check_manager_stop(journal, b, b_result, b_pid, b_ticks, shown)
        self.check_unknown_and_correction(journal, c, c_unknown, c_corrected, (c_pid, c_ticks, c_group, c_session), shown)
        self.check_ctrl_c(journal, d, (d_pid, d_ticks, d_group), shown)
        self.check_refusal(journal, run_id, refused, shown)
        self.check_experiment(journal, ledger, task_id, run_id, execution, e_result, last_result,
                              (e_pid, e_ticks, e_group, e_session), sleeper[:2], shown)
        self.check_no_secrets(journal, ledger, execution)
        for item, found in shown.items():
            sys.stderr.write(f"\n[CS-G11] {item}: {found}")

    # -- per-item checks -----------------------------------------------------------------------------------
    def check_chain(self, journal, command_id, requester, kind, owner, identity, shown, label):
        pid, ticks, group, session = identity
        (requested,) = self.of(journal, "command_stop_requested", command_id)
        self.assertEqual((requested["requested_by"], requested["kind"], requested["owner"]), (requester, kind, owner))
        self.assertRegex(requested["at"], ISO_TIME)
        shown[f"{label}: requester"] = f"command_stop_requested.requested_by={requested['requested_by']} " \
                                       f"kind={requested['kind']} owner={requested['owner']} at={requested['at']}"
        (target,) = self.of(journal, "command_stop_target", command_id)
        self.assertEqual((target["pid"], target["start_ticks"], target["process_group"], target["session"],
                          target["uid"]), (pid, ticks, group, session, os.getuid()),
                         "the verified target is what /proc showed for the command")
        self.assertTrue(target["boot_id"])
        self.assertIn((pid, ticks), {(m["pid"], m["start_ticks"]) for m in target["tracked"]})
        supervisor = target["supervisor"]
        self.assertTrue(type(supervisor["pid"]) is int and type(supervisor["start_ticks"]) is int, supervisor)
        self.assertNotEqual(supervisor["pid"], pid)  # (the supervisor has exited with the command: no /proc to compare)
        shown[f"{label}: target"] = (f"command_stop_target pid={target['pid']} start_ticks={target['start_ticks']} "
                                     f"pgid={target['process_group']} sid={target['session']} uid={target['uid']} "
                                     f"supervisor={supervisor}")
        signals = self.of(journal, "command_stop_signal", command_id)
        self.assertTrue(signals)
        for record in signals:
            self.assertRegex(record["at"], ISO_TIME)
            self.assertIn("stage", record)
            self.assertIn((pid, ticks), {(t["pid"], t["start_ticks"]) for t in record["targets"]})
            self.assertIs(record["auxiliary"], False)
        stamps = [r["at"] for r in signals]
        self.assertEqual(stamps, sorted(stamps), "signal times are ordered")
        shown[f"{label}: signals"] = [(r["signal"], r["at"], r["stage"]) for r in signals]
        (finished,) = self.of(journal, "command_stop_finished", command_id)
        return requested, target, signals, finished

    def check_worker_stop(self, journal, started, result, identity, shown):
        command_id = started["command_id"]
        requested, _, signals, finished = self.check_chain(journal, command_id, "worker", "terminal", "worker",
                                                           identity, shown, "worker stop")
        self.assertEqual([r["signal"] for r in signals], ["SIGINT"])
        self.assertEqual((finished["status"], finished["exit_code"], finished["signal"]), ("exited", 130, 2))
        self.assertEqual(finished["residual"]["alive"], [])
        self.assertTrue(finished["residual"]["checked"])
        self.assertTrue(finished["residual"]["unchecked"])
        self.assertIn("elapsed_seconds", finished)
        self.assertEqual(finished["repeat_requests"], 0)
        self.assertIn("task_id", requested)
        shown["worker stop: final state"] = (f"command_stop_finished status=exited exit_code={finished['exit_code']} "
                                             f"signal={finished['signal']}")
        shown["worker stop: residual"] = f"command_stop_finished.residual={finished['residual']}"

    def check_manager_stop(self, journal, started, result, pid, ticks, shown):
        command_id = started["command_id"]
        requested = self.of(journal, "command_stop_requested", command_id)
        self.assertEqual([(r["requested_by"], r["owner"], r["kind"], r["reason"]) for r in requested],
                         [("manager", "worker", "terminal", "audit: manager stops it")])
        (target,) = self.of(journal, "command_stop_target", command_id)
        self.assertEqual((target["pid"], target["start_ticks"]), (pid, ticks))
        (signal_record,) = self.of(journal, "command_stop_signal", command_id)
        self.assertEqual(signal_record["signal"], "SIGINT")
        (finished,) = self.of(journal, "command_stop_finished", command_id)
        self.assertEqual((finished["status"], finished["exit_code"]), ("exited", 130))
        shown["manager stop of a worker command"] = (f"requested_by=manager reason={requested[0]['reason']!r} "
                                                     f"pid={pid} ticks={ticks} signal=SIGINT exit_code=130")
        # the stop_command request itself is journaled with its key (HandoffService), reason included
        asked = [r for r in journal if r.get("type") == "stop_command_request" and r.get("command_id") == command_id]
        self.assertEqual([r["reason"] for r in asked], ["audit: manager stops it"])
        shown["manager stop: tool request"] = f"stop_command_request key={asked[0]['key']['tool_call_id']} reason"

    def check_unknown_and_correction(self, journal, started, unknown, corrected, identity, shown):
        command_id = started["command_id"]
        pid, ticks, _, _ = identity
        requested, target, signals, finished = self.check_chain(journal, command_id, "worker", "terminal", "worker",
                                                                identity, shown, "unknown stop")
        self.assertEqual([r["signal"] for r in signals], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertEqual((finished["status"], finished["reason"]), ("unknown", "still_alive_after_sigkill"))
        self.assertIn((pid, ticks), {(m["pid"], m["start_ticks"]) for m in finished["residual"]["alive"]},
                      "the survivor is reported by pid and start ticks")
        self.assertTrue(finished["residual"]["unchecked"])
        (correction,) = self.of(journal, "command_stop_corrected", command_id)
        self.assertEqual((correction["from"], correction["previous_reason"], correction["to"],
                          correction["exit_code"], correction["signal"]),
                         ("unknown", "still_alive_after_sigkill", "exited", 137, 9))
        self.assertRegex(correction["at"], ISO_TIME)
        self.assertGreaterEqual(correction["at"], signals[-1]["at"], "the correction came after the last signal")
        self.assertEqual(len(self.of(journal, "command_stop_finished", command_id)), 1, "the episode finished once")
        self.assertEqual(corrected["history"][0]["reason"], "still_alive_after_sigkill", "history in the result too")
        shown["unknown: reason"] = f"command_stop_finished status=unknown reason={finished['reason']}"
        shown["unknown: residual"] = f"command_stop_finished.residual.alive={finished['residual']['alive']}"
        shown["unknown -> corrected: history"] = (f"command_stop_corrected from=unknown "
                                                  f"previous_reason={correction['previous_reason']} to=exited "
                                                  f"exit_code={correction['exit_code']} signal={correction['signal']} "
                                                  f"at={correction['at']}")

    def check_ctrl_c(self, journal, started, identity, shown):
        pid, ticks, group = identity
        (record,) = [r for r in journal if r.get("type") == "command_interrupt"]
        self.assertEqual((record["requested_by"], record["command_id"], record["pid"], record["start_ticks"],
                          record["process_group"], record["delivered"], record["input_owner"]),
                         ("user", started["command_id"], pid, ticks, group, True, "manager"))
        self.assertTrue(record["parent_mode"])
        self.assertRegex(record["at"], ISO_TIME)
        self.assertEqual(self.of(journal, "command_stop_signal", started["command_id"]), [],
                         "a user's Ctrl+C is not a stop episode")
        shown["user Ctrl+C"] = (f"command_interrupt requested_by=user pid={record['pid']} "
                                f"start_ticks={record['start_ticks']} pgid={record['process_group']} "
                                f"delivered={record['delivered']} input_owner={record['input_owner']} at={record['at']}")

    def check_refusal(self, journal, run_id, refused, shown):
        records = [r for r in journal if r.get("type") == "command_stop_refused" and r.get("command_id") == run_id]
        self.assertEqual([(r["requested_by"], r["status"], r["reason"]) for r in records],
                         [("worker", "refused", "not_owner")])
        self.assertRegex(records[0]["at"], ISO_TIME)
        shown["refusal"] = (f"command_stop_refused requested_by=worker status=refused reason=not_owner "
                            f"at={records[0]['at']}")

    def check_experiment(self, journal, ledger, task_id, run_id, execution, result, last_result, identity, member,
                         shown):
        requested = self.of(journal, "command_stop_requested", run_id)
        self.assertEqual([(r["requested_by"], r["kind"], r["owner"], r["task_id"], r["reason"]) for r in requested
                          if r["requested_by"] == "manager"],
                         [("manager", "experiment", "manager", task_id, "audit: the run is not needed")])
        manager_request = next(r for r in requested if r["requested_by"] == "manager")
        self.assertEqual(manager_request["task_status"], "running")
        (target,) = self.of(journal, "command_stop_target", run_id)
        pid, ticks, group, session = identity
        self.assertEqual((target["pid"], target["start_ticks"], target["process_group"], target["session"]),
                         (pid, ticks, group, session))
        self.assertIn(member, {(m["pid"], m["start_ticks"]) for m in target["tracked"]}, "the sleep is a member")
        (signal_record,) = self.of(journal, "command_stop_signal", run_id)
        self.assertEqual(signal_record["signal"], "SIGINT")
        self.assertTrue({(pid, ticks), member} <= {(t["pid"], t["start_ticks"]) for t in signal_record["targets"]})
        (finished,) = self.of(journal, "command_stop_finished", run_id)
        self.assertEqual((finished["status"], finished["exit_code"], finished["signal"]), ("exited", 130, 2))
        ended = [r for r in ledger if r.get("type") == "run_ended" and r.get("run_id") == run_id]
        self.assertEqual([(r["outcome"], r["stopped_by"]) for r in ended], [("stopped", "manager")])
        self.assertEqual(last_result, {"run_id": run_id, "outcome": "stopped", "stopped_by": "manager",
                                       "exit_code": 130, "signal": 2, "run_closed": True})
        with TaskRepository(self.db_path) as repository:
            history = repository.get_run_history(run_id)
        details = history[-1]["details"]
        self.assertEqual(history[-1]["kind"], "completed")
        self.assertEqual((details["outcome"], details["stopped_by"], details["judgment"]), ("stopped", "manager", None))
        self.assertNotIn("7601.7", json.dumps(history[-1], default=str), "no argv in the run's closing event")
        shown["experiment: stop request"] = (f"command_stop_requested kind=experiment owner=manager task_id={task_id} "
                                             f"task_status={manager_request['task_status']} "
                                             f"reason={manager_request['reason']!r}")
        shown["experiment: target/final"] = (f"command_stop_target pid={pid} start_ticks={ticks}; "
                                             f"command_stop_finished exit_code={finished['exit_code']} "
                                             f"signal={finished['signal']}")
        shown["experiment: stopped_by"] = (f"tasks-flow.jsonl run_ended outcome=stopped stopped_by="
                                           f"{ended[0]['stopped_by']}; repository completed.details outcome="
                                           f"{details['outcome']} stopped_by={details['stopped_by']}; "
                                           f"task last_result={last_result}")
        rejected = [r for r in journal if r.get("type") == "stop_command_request" and r.get("redacted")
                    == "environment_value"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["fields"], ["reason:sensitive_value"], "it names the field, never the value")
        shown["rejected reason with an env value"] = (f"stop_command_request redacted=environment_value "
                                                      f"fields={rejected[0]['fields']} (value not stored)")

    def check_no_secrets(self, journal, ledger, experiment):
        tokens = [launch["env"].get("WORKBENCH_G3_TOKEN", "") for launch in list(self.backend._launch.values())]
        tokens = [token for token in tokens if token]
        self.assertTrue(tokens, "the bridge tokens exist (otherwise the auth check would be vacuous)")
        files = [self.handoffs_path, self.ledger_path] + sorted(self.db_path.parent.glob(self.db_path.name + "*"))
        blobs = {path.name: path.read_bytes() for path in files if path.exists()}
        self.assertIn("handoffs.jsonl", blobs)
        self.assertIn(self.db_path.name, blobs)
        for name, blob in blobs.items():
            self.assertNotIn(self.secret.encode(), blob, f"the environment value is not in {name}")
            for token in tokens:
                self.assertNotIn(token.encode(), blob, f"an auth token is not in {name}")
        # (the pre-existing terminal_request / terminal_started / terminal_result / request records carry the command
        # text by design; this feature's audit records are the ones checked)
        stop_records = [r for r in journal if str(r.get("type", "")).startswith(
            ("command_stop_", "command_interrupt", "stop_command_request"))]
        self.assertGreaterEqual(len(stop_records), 20)
        text = json.dumps(stop_records)
        for argv in self.argv_markers + [experiment["command"], "7601.", "bash -c", "exec sleep"]:
            self.assertNotIn(argv, text, f"command argv {argv!r} is not in the stop audit records")
        for record in stop_records:
            self.assertFalse({"argv", "command", "cmdline", "comm", "env", "environment"} & set(record), record)
        flow_text = json.dumps([r for r in ledger if r.get("type") == "run_ended"])
        self.assertNotIn(experiment["command"], flow_text)
        self.assertNotIn("7601.7", flow_text)



if __name__ == "__main__":
    unittest.main()
