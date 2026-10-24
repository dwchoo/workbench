"""Independent differential checks for the ``_start_unlocked`` / ``_follow`` / ``_execute`` split (commit 6f5b1e5).

Contract under test
-------------------
6f5b1e5 is a PURE refactor: ``TerminalService._start_unlocked`` (flow_terminal.py) and ``TaskFlow._execute`` /
``TaskFlow._follow`` (flow_tasks.py) were cut into helpers. Nothing observable may change: the journal / ledger record
sequence and contents, the Task status transitions, which statement runs under which lock, hold release points,
cancellation and hold early returns, start-failure handling, the ``_CallAbandoned`` path, the deferred-report retry,
judge waiting (busy / deferred), the receipt / final run record, stop-target registration (including a failed
registration) and the thread start.

Method (differential)
---------------------
The pre-refactor sources are read at test time with ``git show 0dc6392:<path>`` and loaded under other module names
(the flow_tasks baseline is wired to the baseline flow_terminal). Each scenario below is run against BOTH versions
with scripted collaborators that record every call they receive (host-shell port, workflow/run, stop service,
lifecycle, mailbox, handoff journal) together with the state of the service at that instant (lock held? status?
held_reason? ``_following``? ``_current``?). The normalised outcomes -- results, journal / ledger records, per-thread
event sequences, final Task / flow state, repository state -- must be IDENTICAL. The main scenarios additionally pin
their expected values written here from the contract, so that the oracle does not rest on the baseline alone.

Hygiene: every service, thread and temp dir is created here and closed at the end of each test; the tests assert
that no thread they started is left alive. Nothing outside the test's own temp dir is touched; no signal is sent.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from workbench.backend import flow_tasks as current_tasks
from workbench.backend import flow_terminal as current_terminal
from workbench.backend.flow import HandoffService
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import DeliveryReceipt, MailboxStatus
from workbench.tasks.repository import TaskRepository
from workbench.workflow.run import WorkerJudgmentUnavailable, WorkflowHeld
from workbench.workflow.worker_port import WorkerResponseRejected

REPO = Path(__file__).resolve().parents[2]
BASELINE_REV = "0dc6392"  # the commit just before the split
TERM_NAME = "wb_flow_terminal_baseline_0dc6392"
TASKS_NAME = "wb_flow_tasks_baseline_0dc6392"
WORKER_SESSION = "11111111-1111-4111-8111-111111111111"
MANAGER_SESSION = "22222222-2222-4222-8222-222222222222"
AUTOMATION = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}
COMMIT_A = "a" * 40
VOLATILE = {"at", "seq", "ts", "since", "time", "timestamp", "duration_seconds", "elapsed_seconds", "deadline"}
SMALL_WAIT = 0.05  # PREPARE_WAIT / RETURN_WAIT of both versions while this module runs (a stuck return must not cost seconds)


# ------------------------------------------------------------------------------------------ baseline loading
def _git_show(path: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(REPO), "show", f"{BASELINE_REV}:{path}"], capture_output=True,
                              text=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _load(name: str, source: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = f"<{BASELINE_REV}:{name}>"
    sys.modules[name] = module  # dataclasses look their module up
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def load_baselines():
    """``(flow_terminal, flow_tasks)`` as of ``BASELINE_REV`` under their own names, or None without git."""
    terminal = _git_show("src/workbench/backend/flow_terminal.py")
    tasks = _git_show("src/workbench/backend/flow_tasks.py")
    if terminal is None or tasks is None:
        return None
    marker = "from workbench.backend.flow_terminal import"
    if marker not in tasks:
        return None
    base_terminal = _load(TERM_NAME, terminal)
    base_tasks = _load(TASKS_NAME, tasks.replace(marker, f"from {TERM_NAME} import"))
    return base_terminal, base_tasks


BASELINES = load_baselines()
BASE_TERMINAL, BASE_TASKS = BASELINES if BASELINES else (None, None)
needs_baseline = unittest.skipIf(BASELINES is None, f"git cannot provide {BASELINE_REV} (differential checks only)")
_SAVED: list[tuple[types.ModuleType, str, object]] = []


def setUpModule():
    for module in (current_terminal, BASE_TERMINAL, current_tasks, BASE_TASKS):
        if module is None:
            continue
        for name, value in (("PREPARE_WAIT", SMALL_WAIT), ("RETURN_WAIT", SMALL_WAIT * 6)):
            if hasattr(module, name):
                _SAVED.append((module, name, getattr(module, name)))
                setattr(module, name, value)


def tearDownModule():
    while _SAVED:
        module, name, value = _SAVED.pop()
        setattr(module, name, value)


# ---------------------------------------------------------------------------------------------- normalising
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:?\d\d)?")
SLOT = re.compile(r"(-r\d+)-[0-9a-f]{8}\b")
HASH = re.compile(r"\b[0-9a-f]{64}\b")


def strip(value):
    if isinstance(value, dict):
        return {str(key): strip(item) for key, item in value.items()
                if key not in VOLATILE and not str(key).endswith("_at")}
    if isinstance(value, (list, tuple)):
        return [strip(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((strip(item) for item in value), key=repr)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def normalise(value, roots: dict[str, str]):
    """Structure -> JSON text with ids/paths/timestamps replaced in order of first appearance -> structure."""
    text = json.dumps(strip(value), sort_keys=True, default=str)
    for path, token in sorted(roots.items(), key=lambda item: -len(item[0])):
        text = text.replace(path, token)
    seen: dict[str, str] = {}

    def ident(match):
        return seen.setdefault(match.group(0), f"<id{len(seen)}>")

    text = UUID.sub(ident, text)
    text = STAMP.sub("<ts>", text)
    text = SLOT.sub(r"\1-<slot>", text)
    text = HASH.sub("<hash>", text)
    return json.loads(text)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ------------------------------------------------------------------------------------------------- tracing
def compress(lane: list[dict], longest: int = 8) -> list[dict]:
    """Drop immediate repeats of a block of events (a polling loop's pass count follows the clock, not the code)."""
    changed = True
    while changed:
        changed = False
        for size in range(1, longest + 1):
            index = 0
            while index + 2 * size <= len(lane):
                if lane[index:index + size] == lane[index + size:index + 2 * size]:
                    del lane[index + size:index + 2 * size]
                    changed = True
                else:
                    index += 1
    return lane


class Trace:
    """Per-thread-lane event lists (the order across threads is not part of the contract)."""

    NOISY = {"port.poll", "port.display_bytes"}  # their count follows the clock; consecutive repeats collapse

    def __init__(self, kind_of, state_of):
        self.kind_of, self.state_of = kind_of, state_of
        self.events: dict[str, list[dict]] = {}
        self._mutex = threading.Lock()

    timed = False  # a scenario whose loops run until a deadline: call counters are dropped and repeats collapse

    def add(self, name: str, **data) -> None:
        kind = self.kind_of()
        if self.timed:
            data.pop("call", None)
        event = {"n": name, **data}
        state = self.state_of(kind)
        if state is not None:
            event["st"] = state
        with self._mutex:
            lane = self.events.setdefault(kind, [])
            if name in self.NOISY and lane and lane[-1] == event:
                return
            lane.append(event)


class ThreadWatch:
    """Threads started during a test must be gone at its end."""

    def __init__(self):
        self.before = set(threading.enumerate())

    def leaked(self, grace: float = 3.0) -> list[str]:
        deadline = time.monotonic() + grace
        while True:
            alive = [t for t in threading.enumerate() if t not in self.before and t.is_alive()]
            if not alive or time.monotonic() >= deadline:
                return [t.name for t in alive]
            time.sleep(0.02)


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ================================================================================================== TERMINAL
class ScriptPort:
    """A host shell port that records every call and walks the managed-run lifecycle by call counts, not time."""

    choice = SimpleNamespace(kind="bash", executable="/usr/bin/bash")

    def __init__(self, env: "TermEnv", options: dict):
        self.env, self.o = env, options
        self.owner, self.mode = "user", "manual_prompt"
        self.submitted = self.ended = False
        self.polls = 0
        self.main_exit = None
        self.cwd_calls = 0
        self.pending_output = b""

    def ev(self, name: str, **data) -> None:
        self.env.trace.add("port." + name, **data)

    def act(self, name: str) -> None:
        hook = self.o.get("hooks", {}).get(name)
        if hook is not None:
            hook(self.env)
        factory = self.o.get("raise_on", {}).get(name)
        if factory is not None:
            raise factory()

    def busy(self):
        self.ev("busy")
        self.act("busy")
        return self.o.get("busy")

    def hold(self, reason):
        self.ev("hold", reason=reason)
        self.act("hold")
        return self.o.get("hold_result")

    def release_hold(self, reason=None):
        self.ev("release_hold", reason=reason)

    def detach(self):
        self.ev("detach")
        self.act("detach")

    def cwd(self):
        values = self.o.get("cwds", ["/work"])
        value = values[min(self.cwd_calls, len(values) - 1)]
        self.ev("cwd", call=self.cwd_calls, value=value)
        self.cwd_calls += 1
        self.act("cwd")
        return value

    def send_user(self, data):
        self.ev("send_user", data=bytes(data))
        self.act("send_user")
        if bytes(data).strip() == b"wb-handoff":
            if self.o.get("transit"):  # the shell reaches its control wait late, after the start gave up waiting
                self.mode, self.flip_at = "transit", time.monotonic() + self.o["transit"]
            else:
                self.mode = "control_wait"

    flip_at = None

    def life(self):
        return {"input_barrier": False, "input_returned": self.ended, "unknown": [], "control_returned": self.ended,
                "lifetime": "ended" if self.ended else "running", "main_exit": self.main_exit,
                "request_id": "r" if self.submitted else None}

    def poll(self, timeout=0):
        self.ev("poll")
        self.act("poll")
        if self.flip_at is not None:
            time.sleep(0.01)
            if time.monotonic() >= self.flip_at:
                self.mode, self.flip_at = "control_wait", None
        if self.submitted and not self.ended:
            self.polls += 1
            if self.polls >= self.o.get("polls", 2):
                self.ended, self.main_exit = True, self.o.get("exit", 0)
        phase = self.o.get("phase_after_submit", "running") if self.submitted else "running"
        return {"phase": phase, "parent_mode": self.mode, "input_owner": self.owner, "lifecycle": self.life()}

    def display_bytes(self):
        self.ev("display_bytes")
        data, self.pending_output = self.pending_output, b""
        return data

    def claim_manager(self):
        self.ev("claim_manager")
        self.act("claim_manager")
        if not self.o.get("claim_noop"):
            self.owner = "manager"

    def snapshot(self):
        self.ev("snapshot")
        self.act("snapshot")
        return {"parent_pid": 1, "generation": 1, "owner_epoch": 1, "input_owner": self.owner,
                "parent_mode": self.mode, "lifecycle": self.life()}

    def submit(self, control, argv, automation):
        self.ev("submit", control=control, argv=argv, automation=automation)
        self.act("submit")
        self.submitted = True
        self.pending_output = b"hello\r\n"

    def request_takeover(self):
        self.ev("request_takeover")
        if not self.o.get("stuck"):
            self.owner, self.mode = "user", "manual_prompt"

    def release_input(self):
        self.ev("release_input")

    def run_identity(self):
        self.ev("run_identity")
        return None


class FakeStop:
    """The stop registry as the owners see it; every call is traced."""

    def __init__(self, env: "TermEnv", options: dict):
        self.env, self.o = env, options

    def register(self, target):
        self.env.trace.add("stop.register", command_id=target.command_id, owner=target.owner, kind=target.kind,
                           task_id=target.task_id, identity=target.identity(), forbidden=sorted(target.forbidden()),
                           task_status=target.task_status())
        if self.o.get("register_raises"):
            raise RuntimeError("registry down")

    def registration_failed(self, target, name):
        self.env.trace.add("stop.registration_failed", command_id=target.command_id, error=name)

    def command_ended(self, command_id, result):
        self.env.trace.add("stop.command_ended", command_id=command_id, result=dict(result))

    def stop_marker(self, command_id):
        self.env.trace.add("stop.marker", command_id=command_id)
        return dict(self.o.get("marks", {}))


def term_call(call_id: str, command: str) -> dict:
    return {"request_id": "33333333-3333-4333-8333-333333333333", "tool_call_id": call_id, "tool": "terminal",
            "args": {"command": command}, "session_id": WORKER_SESSION, "generation": 1}


class TracingLock:
    """The service lock with its acquisitions traced (which statement runs under it is part of the contract)."""

    def __init__(self, env: "TermEnv", inner):
        self.env, self.inner = env, inner

    def _traced(self) -> bool:
        return self.env._kind() == "follow" or not self.env.terminal._threads

    def __enter__(self):
        self.inner.acquire()
        if self._traced():
            self.env.trace.add("lock+")
        return self

    def __exit__(self, *exc):
        if self._traced():
            self.env.trace.add("lock-")
        self.inner.release()

    def locked(self):
        return self.inner.locked()

    def acquire(self, *args, **kwargs):
        return self.inner.acquire(*args, **kwargs)

    def release(self):
        self.inner.release()


class TermEnv:
    def __init__(self, module, cfg: dict, root: Path):
        self.module, self.cfg, self.root = module, cfg, root
        self.flags = {"paused": cfg.get("paused", False), "hold": cfg.get("hold")}
        self.trace = Trace(self._kind, self._state)
        (root / "workflow").mkdir(mode=0o700)
        self.gate = module.HostGate()
        self.handoffs = HandoffService(root / "workflow" / "handoffs.jsonl", mailbox=Mailbox(None))
        recorder = self.handoffs.record
        failing = set(cfg.get("record_fails", ()))

        def record(entry):
            if entry.get("type") not in ("terminal_notice", "terminal_check"):  # their timing races the caller
                self.trace.add("journal", type=entry.get("type"))
            if entry.get("type") in failing:
                raise OSError("journal unavailable")
            return recorder(entry)

        self.handoffs.record = record
        self.port = ScriptPort(self, cfg.get("port", {}))
        self.stop = FakeStop(self, cfg["stop"]) if cfg.get("stop") is not None else None
        log_root = root / "workflow" / "terminal"
        if cfg.get("log_blocked"):
            (root / "blocker").write_text("not a directory")
            log_root = root / "blocker" / "terminal"
        self.log_root = log_root
        self.task = cfg.get("task")
        self.terminal = module.TerminalService(
            handoffs=self.handoffs, host_shell=self._host_shell, gate=self.gate, log_root=log_root,
            automation=lambda: AUTOMATION, paused=self._paused, hold=self._hold,
            activity=lambda: cfg.get("activity"),
            active_task=lambda: None if self.task is None else SimpleNamespace(task_id=self.task, revision=3),
            poll_interval=0.01, command_stop=self.stop, task_status=lambda task_id: "running",
            forbidden=lambda: frozenset({7}), wait_seconds=30)
        self.terminal._lock = TracingLock(self, self.terminal._lock)

    # -- scripted collaborators
    def _kind(self) -> str:
        return "follow" if threading.current_thread().name.startswith("terminal-") else "caller"

    def _state(self, kind: str):
        if kind != "caller" or self.terminal._threads:
            return None  # the follow thread races the caller after the start: only the start itself is compared
        terminal = self.terminal
        return {"locked": terminal._lock.locked(), "current": terminal._current is not None,
                "starting": terminal._starting, "gate": self.gate.owner}

    def _host_shell(self):
        shell = self.cfg.get("shell", "port")
        if shell == "raise":
            raise RuntimeError("no shell")
        return None if shell is None else self.port

    def _paused(self):
        value = self.flags["paused"]
        self.trace.add("paused?", value=repr(value))
        if value == "raise":
            raise RuntimeError("pause state unknown")
        return value

    def _hold(self):
        value = self.flags["hold"]
        self.trace.add("hold?", value=repr(value))
        if value == "raise":
            raise RuntimeError("hold state unknown")
        return value

    # -- running
    def abandon(self, call: dict) -> None:
        self.terminal.undelivered(ActorRole.WORKER, call)

    def run(self) -> dict:
        results = []
        try:
            for index, step in enumerate(self.cfg.get("steps", [{}])):
                call = term_call(step.get("call_id", f"call-{index}"), step.get("command", "echo hi"))
                self.current_call = call
                if "port_patch" in step:
                    self.port.o.update(step["port_patch"])
                if "pre" in step:
                    step["pre"](self)
                results.append(self.terminal.handle(ActorRole.WORKER, call))
        finally:
            self.terminal.close()
            self.handoffs.close()
        return self.outcome(results)

    def outcome(self, results) -> dict:
        terminal = self.terminal
        files = {}
        if self.log_root.exists():
            files = {path.name: path.read_bytes() for path in sorted(self.log_root.iterdir())}
        current = terminal._current
        data = {
            "results": results,
            # the completion-notice bookkeeping races the caller's own result record (unrelated to the start)
            "journal": [r for r in read_jsonl(self.root / "workflow" / "handoffs.jsonl")
                        if r.get("type") not in ("terminal_notice", "terminal_check")],
            "events": ({kind: compress(list(lane)) for kind, lane in self.trace.events.items()}
                       if self.cfg.get("timed") else self.trace.events),
            "gate_owner": self.gate.owner,
            "starting": terminal._starting,
            "current": None if current is None else {"done": current.done.is_set(), "result": current.result,
                                                     "handoff_typed": current.handoff_typed,
                                                     "submitting": current.submitting,
                                                     "stop_registered": current.stop_registered,
                                                     "summary_started": current.summary_started},
            "threads": [[re.sub(r"^terminal-[0-9a-f]{8}$", "terminal-<id8>", t.name), t.daemon]
                        for t in terminal._threads],
            "files": files,
            "runs": None if self.task is None else [dict(item) for item in terminal.runs_for_task(self.task)],
            "port": {"owner": self.port.owner, "mode": self.port.mode, "submitted": self.port.submitted},
        }
        return normalise(data, {str(self.root): "<root>"})


def run_terminal(module, cfg: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="flowrefac-term-") as tmp:
        return TermEnv(module, cfg, Path(tmp)).run()


class Mailbox:
    """Minimal mailbox double (the flow tests' ``FakeMailbox`` shape) with scripted delivery outcomes."""

    def __init__(self, env, outcomes=None):
        self.env, self.outcomes = env, dict(outcomes or {})
        self.created, self.delivered = [], []

    def create_message(self, task_id, revision, run_id, sender_role, target_role, kind, payload, *,
                       in_reply_to_message_id=None):
        target = ActorRole(target_role)
        session = WORKER_SESSION if target is ActorRole.WORKER else MANAGER_SESSION
        message = SimpleNamespace(message_id=str(uuid4()), session_id=session, session_generation=1,
                                  task_id=task_id, revision=revision, run_id=run_id,
                                  sender_role=ActorRole(sender_role), target_role=target, kind=MessageKind(kind),
                                  payload=dict(payload), in_reply_to_message_id=in_reply_to_message_id)
        self.created.append(message)
        return message

    def deliver(self, message, *, timeout=20):
        self.delivered.append(message.message_id)
        status = self.outcomes.get(message.target_role.value, MailboxStatus.OMP_PROCESSED)
        return DeliveryReceipt(message.message_id, str(uuid4()), message.target_role, message.session_id, 1,
                               status, {})

    def summary(self):
        return [{"target": m.target_role.value, "kind": m.kind.value, "payload": m.payload,
                 "revision": m.revision, "delivered": m.message_id in self.delivered} for m in self.created]


def request_for(tool: str, args: dict, call: str) -> dict:
    session = MANAGER_SESSION if tool == "to_worker" else WORKER_SESSION
    return {"request_id": str(uuid4()), "tool_call_id": call, "tool": tool, "args": args,
            "session_id": session, "generation": 1}


# ------------------------------------------------------------------------------------------- terminal matrix
def paused_after_hold(env):
    env.flags["paused"] = True


def held_after_hold(env):
    env.flags["hold"] = "maintenance"


def hold_unknown_after_hold(env):
    env.flags["hold"] = "raise"


def pause_unknown_after_hold(env):
    env.flags["paused"] = "raise"


def abandon_now(env):
    env.abandon(env.current_call)


TERMINAL_SCENARIOS: dict[str, dict] = {
    "exit_zero": {},
    "exit_nonzero": {"port": {"exit": 3}},
    "exit_by_signal": {"port": {"exit": -2}},
    "exit_with_task": {"task": "task-1", "stop": {}},
    "stop_marked": {"task": "task-1", "stop": {"marks": {"stopped_by": "manager", "stop_signal": "SIGINT"}}},
    "stop_registration_fails": {"task": "task-1", "stop": {"register_raises": True}},
    "no_stop_service": {"task": "task-1"},
    "spilled_long_command": {"steps": [{"command": "echo " + "x" * 4200}], "stop": {}},
    "journal_start_unconfirmed": {"record_fails": ["terminal_started"], "task": "task-1", "stop": {}},
    "journal_end_unconfirmed": {"record_fails": ["terminal_ended"]},
    "unknown_host_shell_state_while_running": {"port": {"phase_after_submit": "unknown"}},
    # refusals before and after the hold (the recheck path)
    "refused_paused": {"paused": True},
    "refused_held": {"hold": "maintenance"},
    "refused_pause_state_unknown": {"paused": "raise"},
    "refused_hold_state_unknown": {"hold": "raise"},
    "refused_busy": {"port": {"busy": "a line is being typed in the host shell"}},
    "refused_hold_taken": {"port": {"hold_result": "manager owns the host shell"}},
    "refused_hold_raises": {"port": {"raise_on": {"hold": lambda: RuntimeError("gone")}}},
    "refused_no_host_shell": {"shell": None},
    "refused_host_shell_raises": {"shell": "raise"},
    "refused_activity": {"activity": "an experiment run is starting"},
    "recheck_paused_after_hold": {"port": {"hooks": {"hold": paused_after_hold}}},
    "recheck_held_after_hold": {"port": {"hooks": {"hold": held_after_hold}}},
    "recheck_hold_unknown_after_hold": {"port": {"hooks": {"hold": hold_unknown_after_hold}}},
    "recheck_pause_unknown_after_hold": {"port": {"hooks": {"hold": pause_unknown_after_hold}}},
    "refused_log_unavailable": {"log_blocked": True},
    "detach_raises_on_refusal": {"port": {"busy": "jobs", "raise_on": {"detach": lambda: RuntimeError("x")}}},
    # call abandoned
    "abandoned_before_start": {"steps": [{"pre": abandon_now}]},
    "abandoned_at_first_dispatch_check": {"port": {"hooks": {"cwd": abandon_now}}},
    "abandoned_after_claim_before_typing": {"port": {"hooks": {"snapshot": abandon_now}}},
    "abandoned_with_task_and_spill": {"task": "task-1", "steps": [{"command": "echo " + "y" * 4200}],
                                      "port": {"hooks": {"snapshot": abandon_now}}},
    # dispatch failures
    "dispatch_cwd_unknown": {"port": {"cwds": [None]}},
    "dispatch_cwd_changed": {"port": {"cwds": ["/work", "/elsewhere"]}},
    "dispatch_claim_fails": {"port": {"raise_on": {"claim_manager": lambda: RuntimeError("claim failed")}}},
    "dispatch_handoff_send_fails": {"port": {"raise_on": {"send_user": lambda: OSError("pty gone")}}},
    "dispatch_submit_fails": {"port": {"raise_on": {"submit": lambda: OSError("partial managed request")}}},
    "dispatch_submit_fails_spilled": {"steps": [{"command": "echo " + "z" * 4200}],
                                      "port": {"raise_on": {"submit": lambda: OSError("partial")}}},
    "dispatch_paused_meanwhile": {"port": {"hooks": {"claim_manager": paused_after_hold}}},
    "dispatch_held_meanwhile": {"port": {"hooks": {"claim_manager": held_after_hold}}},
    "dispatch_return_not_confirmed": {"port": {"stuck": True,
                                               "raise_on": {"submit": lambda: OSError("partial managed request")}}},
    "dispatch_failure_task_and_stop": {"task": "task-1", "stop": {},
                                       "port": {"raise_on": {"submit": lambda: OSError("x")}}},
    "dispatch_failure_journal_unconfirmed": {"record_fails": ["terminal_start_failed"],
                                             "port": {"raise_on": {"submit": lambda: OSError("x")}}},
    "abort_journal_unconfirmed": {"record_fails": ["terminal_aborted"], "port": {"hooks": {"cwd": abandon_now}}},
    "abandoned_while_the_handoff_is_unclaimed": {"port": {"claim_noop": True, "hooks": {"snapshot": abandon_now}}},
    "start_fails_while_the_control_wait_comes_late": {"timed": True, "port": {"transit": 0.12, "raise_on": {
        "claim_manager": lambda: RuntimeError("claim failed")}}},
    "failed_start_then_new_start": {"steps": [{"port_patch": {"cwds": [None]}},
                                              {"port_patch": {"cwds": ["/work"]}}]},
    "refused_then_new_start": {"steps": [{"port_patch": {"busy": "jobs"}}, {"port_patch": {"busy": None}}]},
    "abandoned_then_new_start": {"steps": [{"pre": abandon_now}, {"call_id": "other"}]},
}


class TerminalPins(unittest.TestCase):
    """The expected values of the main terminal scenarios, written from the contract (not from the baseline)."""

    def run_current(self, name):
        return run_terminal(current_terminal, TERMINAL_SCENARIOS[name])

    def journal_types(self, outcome):
        return [record["type"] for record in outcome["journal"]]

    def port_calls(self, outcome, lane="caller"):
        return [event["n"] for event in outcome["events"].get(lane, []) if event["n"].startswith("port.")]

    def test_a_command_runs_from_start_to_exit(self):
        outcome = self.run_current("exit_zero")
        result = outcome["results"][0]
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 0))
        self.assertEqual(self.journal_types(outcome),
                         ["terminal_request", "terminal_started", "terminal_ended", "terminal_result"])
        self.assertEqual(outcome["gate_owner"], None, "the gate is released when the command ended")
        self.assertEqual([thread[1] for thread in outcome["threads"]], [True], "one daemon follow thread")
        self.assertTrue(outcome["threads"][0][0].startswith("terminal-"))
        calls = self.port_calls(outcome)
        # the hold is released once the managed request was submitted, before the start is journaled
        self.assertEqual(calls[:3], ["port.busy", "port.hold", "port.cwd"])
        self.assertLess(calls.index("port.submit"), calls.index("port.release_hold"))
        journal_at = [e["n"] for e in outcome["events"]["caller"] if e["n"] in ("port.release_hold", "journal")]
        self.assertLess(journal_at.index("port.release_hold"), len(journal_at) - 1)

    def test_the_start_is_registered_before_it_is_journaled_and_after_the_command_is_current(self):
        outcome = self.run_current("exit_with_task")
        caller = outcome["events"]["caller"]
        names = [(e["n"], e.get("type")) for e in caller]
        registered = names.index(("stop.register", None))
        started = names.index(("journal", "terminal_started"))
        self.assertLess(registered, started)
        self.assertTrue(caller[registered]["st"]["current"], "_current is set when the target is offered")
        self.assertFalse(caller[registered]["st"]["locked"], "the offer happens outside the service lock")
        self.assertFalse(caller[started]["st"]["locked"])
        self.assertEqual(caller[registered]["task_id"], "task-1")
        self.assertEqual((caller[registered]["owner"], caller[registered]["kind"]), ("worker", "terminal"))
        ended = [e for e in outcome["events"]["follow"] if e["n"] == "stop.command_ended"]
        self.assertEqual(len(ended), 1)
        self.assertEqual(ended[0]["result"]["status"], "exited")

    def test_a_failed_registration_keeps_the_command_running_and_is_not_reported_ended(self):
        outcome = self.run_current("stop_registration_fails")
        self.assertEqual(outcome["results"][0]["status"], "exited")
        names = [e["n"] for lane in outcome["events"].values() for e in lane]
        self.assertEqual(names.count("stop.registration_failed"), 1)
        self.assertNotIn("stop.command_ended", names)
        self.assertIs(outcome["current"]["stop_registered"], False)
        self.assertIn("terminal_started", self.journal_types(outcome))

    def test_pause_between_the_first_check_and_the_hold_refuses_after_releasing_the_hold(self):
        outcome = self.run_current("recheck_paused_after_hold")
        self.assertEqual(outcome["results"][0]["status"], "paused")
        calls = self.port_calls(outcome)
        self.assertEqual(calls, ["port.busy", "port.hold", "port.release_hold", "port.detach"])
        self.assertEqual(self.journal_types(outcome), ["terminal_request", "terminal_refused", "terminal_result"])
        self.assertIsNone(outcome["gate_owner"])
        self.assertEqual(outcome["threads"], [])

    def test_hold_between_the_first_check_and_the_hold_refuses_with_the_reason(self):
        outcome = self.run_current("recheck_held_after_hold")
        self.assertEqual((outcome["results"][0]["status"], outcome["results"][0]["reason"]),
                         ("held", "maintenance"))
        self.assertEqual(self.port_calls(outcome), ["port.busy", "port.hold", "port.release_hold", "port.detach"])

    def test_an_abandoned_call_before_typing_types_nothing_and_releases_everything(self):
        outcome = self.run_current("abandoned_before_start")
        result = outcome["results"][0]
        self.assertEqual((result["status"], result["reason"]), ("aborted", "call_abandoned"))
        self.assertNotIn("command_id", result)
        self.assertEqual(self.port_calls(outcome), [])
        self.assertIn("terminal_aborted", self.journal_types(outcome))
        aborted = [r for r in outcome["journal"] if r["type"] == "terminal_aborted"][0]
        self.assertEqual(aborted["stage"], "before_start")

    def test_a_call_abandoned_before_submit_gives_the_shell_back_and_drops_the_unrun_files(self):
        outcome = self.run_current("abandoned_after_claim_before_typing")
        result = outcome["results"][0]
        self.assertEqual((result["status"], result["reason"]), ("aborted", "call_abandoned"))
        aborted = [r for r in outcome["journal"] if r["type"] == "terminal_aborted"][0]
        self.assertEqual((aborted["stage"], aborted["returned_to_user"]), ("before_submit", True))
        self.assertNotIn("port.submit", self.port_calls(outcome))
        self.assertIn("port.request_takeover", self.port_calls(outcome))
        self.assertEqual(outcome["files"], {}, "no empty log is left for a command that never ran")
        self.assertIsNone(outcome["gate_owner"])
        self.assertEqual(outcome["threads"], [])

    def test_a_dispatch_failure_is_reported_with_the_shell_state_and_releases_the_gate(self):
        outcome = self.run_current("dispatch_submit_fails")
        result = outcome["results"][0]
        self.assertEqual(result["status"], "start_failed")
        self.assertEqual(result["reason"], "OSError: partial managed request")
        self.assertEqual(result["host_terminal"], {"input_owner": "user", "parent_mode": "manual_prompt"})
        failed = [r for r in outcome["journal"] if r["type"] == "terminal_start_failed"][0]
        self.assertIs(failed["returned_to_user"], True)
        self.assertIsNone(outcome["gate_owner"])
        self.assertEqual(outcome["threads"], [])
        self.assertIn("port.detach", self.port_calls(outcome))

    def test_an_unclaimed_handoff_is_given_back_before_the_state_is_read(self):
        calls = self.port_calls(self.run_current("abandoned_while_the_handoff_is_unclaimed"))
        self.assertLess(calls.index("port.request_takeover"), len(calls) - 1 - calls[::-1].index("port.poll"))
        self.assertEqual(calls.count("port.request_takeover"), 1)

    def test_a_control_wait_that_shows_up_late_is_given_back_once_more(self):
        outcome = self.run_current("start_fails_while_the_control_wait_comes_late")
        self.assertEqual(outcome["results"][0]["host_terminal"], {"input_owner": "user", "parent_mode": "manual_prompt"})
        calls = self.port_calls(outcome)
        self.assertEqual(calls.count("port.request_takeover"), 1)
        self.assertLess(calls.index("port.claim_manager"), calls.index("port.request_takeover"))

    def test_the_current_command_is_set_under_the_service_lock_right_after_the_hold_is_released(self):
        lane = self.run_current("exit_zero")["events"]["caller"]
        names = [e["n"] for e in lane]
        released = len(names) - 1 - names[::-1].index("port.release_hold")
        self.assertEqual(names[released + 1:released + 3], ["lock+", "lock-"])

    def test_a_spilled_script_survives_a_submit_failure_but_not_an_abort(self):
        failed = self.run_current("dispatch_submit_fails_spilled")
        aborted = self.run_current("abandoned_with_task_and_spill")
        self.assertEqual(sum(name.endswith(".sh") for name in failed["files"]), 1)
        self.assertEqual(sum(name.endswith(".sh") for name in aborted["files"]), 0)
        self.assertEqual(aborted["files"], {})


# ================================================================================================== differential
class DifferentialBase(unittest.TestCase):
    def setUp(self):
        self.watch = ThreadWatch()
        self.addCleanup(self.assert_no_leak)

    def assert_no_leak(self):
        self.assertEqual(self.watch.leaked(), [], "a thread started by the scenario is still alive")


@needs_baseline
class TerminalDifferential(DifferentialBase):
    def test_every_scenario_is_identical_before_and_after_the_split(self):
        for name, cfg in TERMINAL_SCENARIOS.items():
            with self.subTest(scenario=name):
                before = run_terminal(BASE_TERMINAL, cfg)
                after = run_terminal(current_terminal, cfg)
                self.assertEqual(after, before)

    def test_the_scenarios_are_repeatable(self):
        # a flaky scenario would make the differential check meaningless: the same version twice must agree
        for name in ("exit_zero", "dispatch_submit_fails", "abandoned_after_claim_before_typing",
                     "stop_registration_fails", "recheck_held_after_hold"):
            with self.subTest(scenario=name):
                self.assertEqual(run_terminal(current_terminal, TERMINAL_SCENARIOS[name]),
                                 run_terminal(current_terminal, TERMINAL_SCENARIOS[name]))


# ================================================================================================ TASK FLOW
class Gate:
    def __init__(self):
        self.reached, self.proceed = threading.Event(), threading.Event()


class FlowPort:
    """The host shell port of an experiment run: busy / hold / take-over / cwd restore, every call traced."""

    def __init__(self, env: "FlowEnv"):
        self.env = env
        self.owner, self.mode = "user", "manual_prompt"
        self.cwd_value = "/home/user"

    def ev(self, name: str, **data) -> None:
        self.env.trace.add("port." + name, **data)

    def busy(self):
        self.ev("busy")
        return self.env.flags.get("busy")

    def hold(self, reason):
        self.ev("hold", reason=reason)
        return self.env.flags.get("busy") or self.env.flags.get("hold_result")

    def release_hold(self, reason=None):
        self.ev("release_hold", reason=reason)

    def detach(self):
        self.ev("detach")

    def cwd(self):
        self.ev("cwd")
        return self.cwd_value

    def send_user(self, data):
        self.ev("send_user", data=bytes(data))
        if bytes(data).strip() == b"wb-handoff":
            self.mode = "control_wait"

    def snapshot(self):
        self.ev("snapshot")
        life = {"request_id": None}
        if self.env.flags.get("exited"):
            life = {"request_id": "r", "unknown": [], "lifetime": "ended", "input_returned": True,
                    "control_returned": True}
        return {"input_owner": self.owner if not self.env.flags.get("exited") else "manager",
                "parent_mode": self.mode, "lifecycle": life}

    def poll(self, timeout=0):
        self.ev("poll")
        return {"parent_mode": self.mode, "lifecycle": {"request_id": None}}

    def request_takeover(self):
        self.ev("request_takeover")
        self.owner, self.mode = "user", "manual_prompt"

    def hold_return(self, reason):
        self.ev("hold_return", reason=reason)
        return True

    def restore_cwd(self, target, root, *, reason, idle_wait):
        self.ev("restore_cwd", target=target, reason=reason, idle_wait=idle_wait)
        return "restored"

    def run_identity(self):
        self.ev("run_identity")
        return None


class FlowStop(FakeStop):
    def register(self, target):
        self.env.trace.add("stop.register", command_id=target.command_id, owner=target.owner, kind=target.kind,
                           task_id=target.task_id, task_status=target.task_status(),
                           forbidden=sorted(target.forbidden()))
        if self.o.get("register_raises"):
            raise RuntimeError("registry down")


class Lifecycle:
    def __init__(self, env):
        self.env = env
        self.calls: list[dict] = []
        self.ordinal: dict[str, str] = {}
        self._mutex = threading.Lock()

    def _add(self, name, *args):
        with self._mutex:
            ids = [getattr(a, "run_id", a) for a in args]
            # run ids are replaced by the order they were first seen in (the raw uuids differ between two runs)
            self.calls.append({"n": name, "args": [self.ordinal.setdefault(i, f"run#{len(self.ordinal)}")
                                                   if isinstance(i, str) and UUID.fullmatch(i) else i for i in ids]})

    def experiment_started(self, run):
        self._add("experiment_started", run)

    def run_ended(self, run_id):
        self._add("run_ended", run_id)

    def work_started(self, *args):
        self._add("work_started", *args)

    def status(self):
        return {"review": self.env.cfg.get("review", {})}


class ScriptRun:
    def __init__(self, env: "FlowEnv", repository, run_id: str):
        self.env, self.repository, self.run_id = env, repository, run_id
        self.task_message_id = str(uuid4())
        self.shell_request_id = "req-1"
        self.revision = 1
        self._record: dict = {}
        self.collects = 0
        self.judges = 0
        self.retries = 0
        style = env.cfg.get("judge_signature", "full")
        self.judge = {"full": self._judge_full, "plain": self._judge_plain, "hook": self._judge_hook}[style]

    def ev(self, name: str, **data) -> None:
        self.env.trace.add("run." + name, **data)

    def close(self):
        self.ev("close")

    def _persist(self):
        self.ev("persist")

    def collect(self, *, timeout=1, paused=False):
        script = self.env.cfg.get("collect", ["exited"])
        step = script[min(self.collects, len(script) - 1)]
        self.collects += 1
        self.ev("collect", call=self.collects, paused=paused, step=step if isinstance(step, str) else "gate")
        self.env.gate(f"collect{self.collects}")
        if step == "running":
            time.sleep(0.01)
            return {"shell_state": "running", "exit_confirmed": False}
        if step == "unknown":
            return {"shell_state": "unknown", "exit_confirmed": False}
        self.env.flags["exited"] = True
        return {"shell_state": "exited", "exit_confirmed": True, "exit_status": self.env.cfg.get("exit_status", 0)}

    # -- judge signatures
    def _judge_full(self, on_report=None, report_hold=None):
        return self._judge(on_report, report_hold)

    def _judge_hook(self, on_report=None):
        return self._judge(on_report, None)

    def _judge_plain(self):
        return self._judge(None, None)

    def _step(self, key: str):
        script = self.env.cfg.get(key, ["completed"])
        index = self.judges if key == "judge" else self.retries
        return script[min(index, len(script) - 1)]

    def _judge(self, on_report, report_hold):
        step = self._step("judge")
        self.judges += 1
        self.ev("judge", call=self.judges, step=step, on_report=on_report is not None,
                report_hold=report_hold is not None)
        self.env.gate(f"judge{self.judges}")
        if report_hold is not None:
            self.ev("report_hold", value=report_hold())
        return self.behave(step, on_report)

    def retry_report(self, on_report=None):
        step = self._step("retry")
        self.retries += 1
        self.ev("retry_report", call=self.retries, step=step, on_report=on_report is not None)
        self.env.gate(f"retry{self.retries}")
        return self.behave(step, on_report)

    def behave(self, step: str, on_report):
        good = {"judgment": "success"}
        record = {"report": {"status": "omp_processed"}, "worker_judgment": good}
        if step in ("accepted", "completed", "unfinished", "rejected", "deferred"):
            if step == "rejected":
                record["report"]["status"] = "rejected"
            if step == "deferred":
                record["report"]["status"] = "deferred"
            self._record["report"] = dict(record["report"])
            self._record["worker_judgment"] = good
        if step == "accepted":
            record["report"]["accepted_by_manager"] = True
            on_report("sending", dict(record))
            self.repository.complete_run(self.run_id, {"judgment": "success"})
            on_report("submitted", dict(record))
            return record
        if step == "completed":
            self.repository.complete_run(self.run_id, {"judgment": "success"})
            return record
        if step == "unfinished":
            return record
        if step == "rejected":
            return {"report": {"status": "rejected"}, "worker_judgment": good}
        if step == "deferred":
            return {"report": {"status": "deferred"}, "worker_judgment": good}
        if step == "unavailable":
            raise WorkerJudgmentUnavailable("analysis_rejected", {"judgment": "x", "reasons": ["r1"]})
        if step == "held_deferred":
            self._record["worker_analysis_request"] = {"status": "deferred"}
            raise WorkflowHeld("analysis deferred")
        if step == "held":
            raise WorkflowHeld("some other hold")
        if step == "error":
            raise RuntimeError("boom")
        if step == "error_report_accepted":
            self._record["report"] = {"status": "omp_processed", "accepted_by_manager": True}
            self._record["worker_judgment"] = good
            raise RuntimeError("boom after report")
        if step == "error_report_unknown":
            self._record["report"] = {"status": "unknown"}
            self._record["worker_judgment"] = good
            raise RuntimeError("boom after send")
        if step == "sent_outcome_unknown":
            on_report("sending", {})
            self._record["report"] = {"status": "unknown"}
            self._record["worker_judgment"] = good
            return {"report": {"status": "unknown"}, "worker_judgment": good}
        if step == "none":
            return None
        raise AssertionError(step)


class Workflow:
    """``start`` variants: with and without the ``before_shell_input`` keyword (the two code paths of ``_execute``)."""

    def __init__(self, env: "FlowEnv", repository):
        self.env, self.repository = env, repository
        if not env.cfg.get("late_hold", True):
            self.start = self._start_plain

    def _start_plain(self, task_id, revision, *, worktree_path, artifacts_root, automation, shell):
        return self._begin(task_id, revision, worktree_path, shell, None)

    def start(self, task_id, revision, *, worktree_path, artifacts_root, automation, shell, before_shell_input=None):
        return self._begin(task_id, revision, worktree_path, shell, before_shell_input)

    def _begin(self, task_id, revision, worktree_path, shell, before_shell_input):
        env = self.env
        script = env.cfg.get("start", ["ok"])
        step = script[min(env.starts, len(script) - 1)]
        env.starts += 1
        env.trace.add("workflow.start", call=env.starts, step=step, revision=revision,
                      worktree=str(worktree_path), shell_is_port=shell is env.port,
                      before_shell_input=before_shell_input is not None)
        env.gate("start")
        if before_shell_input is not None:
            before_shell_input()
        env.gate("typed")
        if step == "type_handoff_then_fail":
            shell.send_user(b"wb-handoff\n")
            raise RuntimeError("claim failed")
        if step == "fail":
            raise RuntimeError("start exploded")
        if step == "rejected":
            raise WorkerResponseRejected("bad_response")
        if step == "failure_notice":
            error = RuntimeError("start exploded")
            error.workbench_start_failure = {"run_id": str(uuid4()), "task_message_id": str(uuid4()),
                                             "manager_report": None, "revision": revision}
            raise error
        if step == "held":
            raise WorkflowHeld("held by the workflow")
        run_id = self.repository.start_run(task_id, revision, inputs={})
        return ScriptRun(env, self.repository, run_id)


class FlowEnv:
    def __init__(self, tasks_module, terminal_module, cfg: dict, root: Path):
        self.tasks, self.terminal_module, self.cfg, self.root = tasks_module, terminal_module, cfg, root
        self.flags: dict = {"paused": False, "busy": cfg.get("busy"), "hold_result": cfg.get("hold_result"), "hold": {}}
        self.gates: dict[str, Gate] = {name: Gate() for name in cfg.get("gates", ())}
        self.starts = 0
        self.idle_calls = {"worker": 0, "manager": 0}
        self.trace = Trace(self._kind, self._state)
        self.trace.timed = bool(cfg.get("timed"))
        (root / "workflow").mkdir(mode=0o700)
        self.db = root / "tasks.sqlite3"
        self.mailbox = Mailbox(self, cfg.get("deliver"))
        self.port = FlowPort(self)
        self.lifecycle = Lifecycle(self)
        self.handoffs = HandoffService(root / "workflow" / "handoffs.jsonl", mailbox=self.mailbox,
                                       paused=self.paused, retry_interval=0.01)
        ports = tasks_module.ExperimentPorts(
            host_shell=lambda: self.port, make_workflow=lambda repository: Workflow(self, repository),
            automation=lambda: AUTOMATION, environment_names=lambda: {"PATH"}, worktrees_root=root,
            artifacts_root=root)
        self.flow = tasks_module.TaskFlow(
            root / "workflow" / "tasks-flow.jsonl", repository_factory=lambda: TaskRepository(self.db),
            handoffs=self.handoffs, omp_idle=self.omp_idle, paused=self.paused, hold=self.hold,
            experiment=ports, lifecycle=self.lifecycle, poll_interval=0.02, collect_slice=0.05,
            analysis_idle_limit=cfg.get("idle_limit", 180.0), host_gate=terminal_module.HostGate())
        appended = self.flow._ledger.append

        def append(record):
            task = record.get("task") if isinstance(record.get("task"), dict) else {}
            self.trace.add("ledger", type=record.get("type"), status=task.get("status"),
                           held=task.get("held_reason"), outcome=record.get("outcome"))
            return appended(record)

        self.flow._ledger.append = append
        if cfg.get("stop") is not None:
            self.flow.command_stop = FlowStop(self, cfg["stop"])
        self.flow.stop_forbidden = lambda: frozenset({7})
        self.handoffs.configure(policy=self.flow, active_task=self.flow.active_task)
        self.handoffs.start()
        self.flow.start()
        self.excs: list[str] = []
        self.calls = 0
        self.results: list = []

    # -- scripted collaborators
    def _kind(self) -> str:
        thread = threading.current_thread()
        return "runner" if thread is self.flow._thread else "other"

    def _state(self, kind: str):
        if kind != "runner":
            return None
        flow = self.flow
        try:
            task = next(iter(list(flow.tasks.values())), None)
        except RuntimeError:
            task = None
        state = {"lock": flow._lock._is_owned(), "following": flow._following}
        if task is not None:
            state.update(status=task.status, held=task.held_reason, run_id=task.run_id,
                         runs_started=task.runs_started, pending=task.pending_start is not None,
                         cancel=task.cancel_requested is not None)
        return state

    def paused(self):
        return self.flags["paused"]

    def hold(self, role):
        value = self.flags["hold"].get(role.value)
        if value == "raise":
            raise RuntimeError("hold unknown")
        return value

    def omp_idle(self, role):
        key = role.value
        script = self.cfg.get("idle_" + key)
        # the scripted answers apply once the host command exited (before that the worker is simply idle)
        if script is None or not self.flags.get("exited"):
            return True
        index = self.idle_calls[key]
        self.idle_calls[key] += 1
        value = script[min(index, len(script) - 1)]
        if index < len(script):
            self.trace.add("omp_idle", role=key, value=value)
        return value

    def gate(self, name: str) -> None:
        gate = self.gates.get(name)
        if gate is not None:
            gate.reached.set()
            gate.proceed.wait(15)

    # -- driving
    def call(self, args: dict, tool="to_worker") -> dict:
        self.calls += 1
        role = ActorRole.MANAGER if tool == "to_worker" else ActorRole.WORKER
        return self.handoffs.handle(role, request_for(tool, args, f"call-{self.calls}"))

    def dispatch_experiment(self, **spec) -> dict:
        execution = {"source": "/src/repo", "commit": COMMIT_A, "command": "./run.sh",
                     "criteria": {"log_contains": "PASS", "result_file": "out.txt", "result_contains": "PASS"},
                     "environment": ["PATH"], "shell": "bash"}
        args = {"kind": "experiment", "message": "run the parser benchmark",
                "spec": {"goal": "faster parser", "paths": ["src/"], "execution": execution, **spec}}
        result = self.call(args)
        self.results.append(result)
        return result

    def cancel(self, message="stop this task") -> dict:
        task = next(iter(self.flow.tasks.values()))
        result = self.call({"kind": task.kind, "message": message, "cancel": True, "task_id": task.task_id})
        self.results.append(result)
        return result

    def task(self):
        return next(iter(self.flow.tasks.values()), None)

    def reached(self, name: str, timeout=10.0) -> bool:
        return self.gates[name].reached.wait(timeout)

    def release(self, name: str) -> None:
        self.gates[name].proceed.set()

    def wait_for(self, predicate, timeout=15.0) -> bool:
        return wait_until(predicate, timeout)

    def status(self):
        task = self.task()
        return None if task is None else task.status

    def held(self):
        task = self.task()
        return None if task is None else task.held_reason

    def detaches(self) -> int:
        return sum(1 for event in list(self.trace.events.get("runner", [])) if event["n"] == "port.detach")

    def done(self, status=None, held=None, closed=False, detaches=1) -> bool:
        return self.detaches() >= detaches and self.settled(status, held, closed)

    def settled(self, status=None, held=None, closed=False) -> bool:
        task = self.task()
        if task is None or self.flow._following is not None:
            return False
        if closed:
            return task.status == "closed"
        return (status is None or task.status == status) and (held is None or task.held_reason == held)

    # -- finishing
    def close(self):
        for gate in self.gates.values():
            gate.proceed.set()
        self.flow.close(timeout=3.0)
        alive = self.flow._thread is not None and self.flow._thread.is_alive()
        self.handoffs.close()
        if alive:
            raise AssertionError("the flow runner did not stop when the flow was closed")

    def outcome(self) -> dict:
        flow, repository_db = self.flow, self.db
        tasks = {}
        for task in flow.tasks.values():
            tasks[task.task_id] = {
                "status": task.status, "held": task.held_reason, "run_id": task.run_id,
                "run_revision": task.run_revision, "runs_started": task.runs_started,
                "last_result": task.last_result, "closed_reason": getattr(task, "closed_reason", None),
                "cancel": task.cancel_requested, "pending": task.pending_start,
                "task_message_id_set": task.task_message_id is not None}
        runs = {}
        with TaskRepository(repository_db) as repository:
            for task in flow.tasks.values():
                for revision in range(1, 4):
                    try:
                        decisions = [d["kind"] for d in repository.get_decisions(task.task_id, revision)]
                    except Exception:
                        decisions = None
                    if decisions:
                        runs.setdefault(task.task_id, {})["decisions"] = decisions
                current = repository.get_current_run(task.task_id)
                runs.setdefault(task.task_id, {})["current"] = None if current is None else current.get("run_id")
            for item in self.run_ids:
                runs.setdefault("history", {})[item] = [h["kind"] for h in repository.get_run_history(item)]
        data = {
            "results": self.results,
            "ledger": read_jsonl(self.root / "workflow" / "tasks-flow.jsonl"),
            "journal": read_jsonl(self.root / "workflow" / "handoffs.jsonl"),
            "mailbox": self.mailbox.summary(),
            "events": ({kind: compress(list(lane)) for kind, lane in self.trace.events.items()}
                       if self.trace.timed else self.trace.events),
            "lifecycle": sorted(self.lifecycle.calls, key=lambda c: json.dumps(c, sort_keys=True, default=str)),
            "tasks": tasks,
            "repository": runs,
            "flow": {"following": flow._following, "gate": flow._host_gate.owner,
                     "host_runs": flow._host_runs, "run_requests": flow._run_requests,
                     "stop_registered": sorted(flow._stop_registered),
                     "report_inflight": sorted(flow._report_inflight), "notices": flow.notices,
                     "home_cwd": flow._home_cwd,
                     "port_send_user_wrapped": "send_user" in vars(self.port)},
            "exceptions": self.excs,
        }
        return normalise(data, {str(self.root): "<root>"})

    @property
    def run_ids(self) -> list[str]:
        found = []
        for lane in self.trace.events.values():
            for event in lane:
                if event["n"] == "stop.register" or event["n"].startswith("run."):
                    pass
        ledger = read_jsonl(self.root / "workflow" / "tasks-flow.jsonl")
        for record in ledger:
            for key in ("run_id",):
                if isinstance(record.get(key), str) and record[key] not in found:
                    found.append(record[key])
        return found


def run_flow(tasks_module, terminal_module, scenario) -> dict:
    cfg, driver = scenario
    with tempfile.TemporaryDirectory(prefix="flowrefac-flow-") as tmp:
        env = FlowEnv(tasks_module, terminal_module, cfg, Path(tmp))
        previous = threading.excepthook

        def hook(args):
            env.excs.append(f"{args.exc_type.__name__}: {args.exc_value}")

        threading.excepthook = hook
        try:
            driver(env)
            time.sleep(0.05)  # a settled follow has nothing left to do; a late stray record would show here
        finally:
            threading.excepthook = previous
            env.close()
        return env.outcome()


# ------------------------------------------------------------------------------------------ flow scenarios
def expect(env: FlowEnv, **kwargs) -> None:
    if not env.wait_for(lambda: env.done(**kwargs)):
        raise AssertionError(f"the flow did not settle at {kwargs}: status={env.status()} held={env.held()} "
                             f"detaches={env.detaches()} following={env.flow._following} "
                             f"excs={env.excs} lanes={ {k: [e['n'] for e in v][-12:] for k, v in env.trace.events.items()} }")


def finishes(**kwargs):
    kwargs.setdefault("status", "finished")

    def driver(env):
        env.dispatch_experiment()
        expect(env, **kwargs)
    return driver


def run_and_cancel_at(gate: str, status: str | None = None, **kwargs):
    def driver(env):
        env.dispatch_experiment()
        assert env.reached(gate), f"{gate} was not reached"
        env.cancel()
        env.release(gate)
        expect(env, **{"closed": True, **kwargs})
    return driver


def busy_then_clear(flag: str, value, held: str, **kwargs):
    def driver(env):
        env.dispatch_experiment()
        if not env.wait_for(lambda: env.held() == held):
            raise AssertionError(f"never held as {held}: {env.held()} {env.status()}")
        env.flags[flag] = value
        expect(env, **kwargs)
    return driver


def flag_at_gate(gate: str, apply, clear, held: str | None, **kwargs):
    """Set a pause / hold while the runner is parked at ``gate``, release it, wait for the hold it causes, clear."""
    def driver(env):
        env.dispatch_experiment()
        assert env.reached(gate), f"{gate} was not reached"
        apply(env)
        env.release(gate)
        if held is not None and not env.wait_for(lambda: env.held() == held):
            raise AssertionError(f"never held as {held}: {env.held()} {env.status()}")
        time.sleep(0.08)
        clear(env)
        expect(env, **kwargs)
    return driver


def set_paused(env):
    env.flags["paused"] = True


def clear_paused(env):
    env.flags["paused"] = False


def set_hold(role: str, reason: str = "maintenance"):
    return lambda env: env.flags["hold"].__setitem__(role, reason)


def clear_hold(role: str):
    return lambda env: env.flags["hold"].__setitem__(role, None)


def hold_before_dispatch(role: str, reason: str, held_reason: str, **kwargs):
    def driver(env):
        env.flags["hold"][role] = reason
        env.dispatch_experiment()
        if not env.wait_for(lambda: env.held() == held_reason):
            raise AssertionError(f"never held as {held_reason}: {env.held()} {env.status()}")
        time.sleep(0.06)
        env.flags["hold"][role] = None
        expect(env, **kwargs)
    return driver


def pause_inside_slices(env):
    """collect #2 is called while paused, collect #3 after the resume (the pause flag reaches ``collect``)."""
    env.dispatch_experiment()
    assert env.reached("collect1")
    env.flags["paused"] = True
    env.release("collect1")
    assert env.reached("collect2")
    env.flags["paused"] = False
    env.release("collect2")
    expect(env)


def rerun_after_failure(env):
    env.dispatch_experiment()
    expect(env, status="finished", held="start_failed:RuntimeError")
    task = env.task()
    env.results.append(env.call({"kind": "experiment", "message": "again", "task_id": task.task_id, "run": True}))
    expect(env, status="finished", detaches=2)


def two_runs(env):
    env.dispatch_experiment()
    expect(env)
    task = env.task()
    env.results.append(env.call({"kind": "experiment", "message": "again", "task_id": task.task_id, "run": True}))
    expect(env, detaches=2)


def cancel_at_gate_then(gate: str, **kwargs):
    def driver(env):
        env.dispatch_experiment()
        assert env.reached(gate)
        env.cancel()
        env.release(gate)
        expect(env, **kwargs)
    return driver


def stop_while_held(held: str):
    """Leave the flow waiting (held) and let ``run_flow`` close it: the follow must end by itself."""
    def driver(env):
        env.dispatch_experiment()
        if not env.wait_for(lambda: env.held() == held):
            raise AssertionError(f"never held as {held}: {env.held()} {env.status()}")
    return driver


def stop_while_collecting(env):
    env.dispatch_experiment()
    if not env.wait_for(lambda: env.trace.events.get("runner") and any(
            e["n"] == "run.collect" for e in list(env.trace.events["runner"]))):
        raise AssertionError("never collecting")


def late_pause_before_typing(env):
    env.dispatch_experiment()
    assert env.reached("start")
    env.flags["paused"] = True
    env.release("start")
    expect(env, status="finished", held="start_failed:paused")
    env.flags["paused"] = False


def late_hold_before_typing(env):
    env.dispatch_experiment()
    assert env.reached("start")
    env.flags["hold"]["worker"] = "maintenance"
    env.release("start")
    expect(env, status="finished", held="start_failed:maintenance")
    env.flags["hold"]["worker"] = None


def late_busy_before_typing(env):
    env.dispatch_experiment()
    assert env.reached("start")
    env.flags["busy"] = "typing"
    env.release("start")
    expect(env, status="finished", held="start_failed:host_terminal_busy")
    env.flags["busy"] = None


def late_cancel_before_typing(env):
    env.dispatch_experiment()
    assert env.reached("start")
    env.cancel()
    env.release("start")
    expect(env, closed=True)


FLOW_SCENARIOS: dict[str, tuple[dict, object]] = {
    # -- normal paths
    "accepted_report": ({"judge": ["accepted"], "stop": {}}, finishes()),
    "accepted_report_plain_start": ({"judge": ["accepted"], "late_hold": False}, finishes()),
    "report_without_hook_run_completed": ({"judge": ["completed"]}, finishes()),
    "judge_with_plain_signature": ({"judge": ["completed"], "judge_signature": "plain"}, finishes()),
    "judge_with_report_hook_only": ({"judge": ["accepted"], "judge_signature": "hook"}, finishes()),
    "report_outcome_unknown": ({"judge": ["unfinished"]}, finishes(status="closed", closed=True)),
    "report_rejected": ({"judge": ["rejected"]}, finishes(status="closed", closed=True)),
    "exit_status_nonzero": ({"judge": ["accepted"], "exit_status": 3, "stop": {}}, finishes()),
    "exit_status_by_signal": ({"judge": ["accepted"], "exit_status": -9, "stop": {}}, finishes()),
    "two_consecutive_runs": ({"judge": ["accepted"]}, two_runs),
    "slow_command_collected_in_slices": ({"judge": ["accepted"], "collect": ["running", "running", "exited"]},
                                         finishes()),
    # -- stop target
    "stop_target_registered_and_ended": ({"judge": ["completed"], "stop": {}}, finishes()),
    "stop_registration_fails": ({"judge": ["completed"], "stop": {"register_raises": True}}, finishes()),
    "stopped_by_the_manager": ({"judge": ["accepted"], "stop": {"marks": {"stopped_by": "manager",
                                                                          "stop_signal": "SIGINT"}},
                                "exit_status": -2}, finishes()),
    "no_stop_service": ({"judge": ["completed"]}, finishes()),
    # -- deferred report retry
    "report_deferred_then_accepted": ({"judge": ["deferred"], "retry": ["accepted"],
                                       "idle_manager": [False, False, True]}, finishes()),
    "report_deferred_then_completed": ({"judge": ["deferred"], "retry": ["completed"],
                                        "idle_manager": [False, True]}, finishes()),
    "report_deferred_twice": ({"judge": ["deferred"], "retry": ["deferred", "accepted"],
                               "idle_manager": [True]}, finishes()),
    "report_deferred_retry_fails": ({"judge": ["deferred"], "retry": ["error"], "idle_manager": [True]},
                                    finishes(status="closed", closed=True)),
    "report_deferred_retry_returns_none": ({"judge": ["deferred"], "retry": ["none"], "idle_manager": [True]},
                                           finishes(status="waiting_report", held="runner_error:AttributeError")),
    "report_deferred_while_paused": ({"judge": ["deferred"], "retry": ["accepted"], "gates": ["judge1"],
                                      "idle_manager": [False, True]},
                                     flag_at_gate("judge1", set_paused, clear_paused, "manager_busy")),
    "report_deferred_manager_hold": ({"judge": ["deferred"], "retry": ["accepted"], "gates": ["judge1"],
                                      "idle_manager": [False, True]},
                                     flag_at_gate("judge1", set_hold("manager", "manager_maintenance"),
                                                  clear_hold("manager"), "manager_maintenance")),
    "report_hold_is_offered_to_the_judge": ({"judge": ["accepted"], "gates": ["judge1"]},
                                            flag_at_gate("judge1", set_hold("manager", "manager_maintenance"),
                                                         clear_hold("manager"), None)),
    # -- judge waiting
    "judge_busy_then_ready": ({"judge": ["accepted"], "idle_worker": [False, False, True], "timed": True}, finishes()),
    "judge_busy_for_too_long": ({"idle_worker": [False], "idle_limit": 0.15, "timed": True,
                                 "review": {"status": "failed", "reason": "stuck"}}, finishes()),
    "judge_deferred_then_ready": ({"judge": ["held_deferred", "held_deferred", "accepted"]}, finishes()),
    "judge_deferred_for_too_long": ({"judge": ["held_deferred"], "idle_limit": 0.15, "timed": True}, finishes()),
    "judge_busy_then_deferred_then_ready": ({"judge": ["held_deferred", "accepted"],
                                             "idle_worker": [False, True], "timed": True}, finishes()),
    "judge_unavailable": ({"judge": ["unavailable"], "stop": {}}, finishes()),
    "judge_held_by_workflow": ({"judge": ["held"]}, finishes(status="held")),
    "judge_error_before_report": ({"judge": ["error"]}, finishes()),
    "judge_error_after_report_accepted": ({"judge": ["error_report_accepted"]}, finishes()),
    "judge_error_after_report_unknown": ({"judge": ["error_report_unknown"]}, finishes(status="closed", closed=True)),
    "judge_returns_none": ({"judge": ["none"]},
                           finishes(status="waiting_report", held="runner_error:AttributeError")),
    "paused_while_waiting_to_judge": ({"judge": ["accepted"], "gates": ["collect1"]},
                                      flag_at_gate("collect1", set_paused, clear_paused, "paused")),
    "paused_during_collect": ({"judge": ["accepted"], "gates": ["collect1", "collect2"],
                               "collect": ["running", "running", "exited"]}, pause_inside_slices),
    "worker_hold_while_waiting_to_judge": ({"judge": ["accepted"], "gates": ["collect1"]},
                                           flag_at_gate("collect1", set_hold("worker", "worker_maintenance"),
                                                        clear_hold("worker"), "worker_maintenance")),
    "worker_hold_before_the_start": ({"judge": ["accepted"], "timed": True},
                                     hold_before_dispatch("worker", "worker_maintenance", "worker_maintenance",
                                                          status="finished")),
    "shell_state_unknown": ({"collect": ["unknown"], "stop": {}}, finishes(status="held")),
    "report_deferred_retry_fails_after_acceptance": ({"judge": ["deferred"], "retry": ["error_report_accepted"],
                                                      "idle_manager": [True]}, finishes()),
    "report_sent_but_its_outcome_is_unknown": ({"judge": ["sent_outcome_unknown"]},
                                               finishes(status="closed", closed=True)),
    "cancel_then_shell_state_unknown": ({"gates": ["collect1"], "collect": ["unknown"]},
                                        cancel_at_gate_then("collect1", status="cancelling")),
    "flow_closed_while_the_manager_is_busy": ({"judge": ["deferred"], "retry": ["accepted"],
                                               "idle_manager": [False], "timed": True},
                                              stop_while_held("manager_busy")),
    "flow_closed_while_the_worker_is_busy": ({"idle_worker": [False], "timed": True},
                                             stop_while_held("worker_busy")),
    "flow_closed_during_the_host_command": ({"collect": ["running"], "timed": True}, stop_while_collecting),
    # -- cancellation
    "cancel_while_pending_start": ({"busy": "typing", "judge": ["accepted"], "timed": True},
                                   lambda env: (env.dispatch_experiment(),
                                                env.wait_for(lambda: env.held() == "host_terminal_busy"),
                                                env.cancel(), expect(env, closed=True))),
    "cancel_while_starting": ({"gates": ["typed"], "judge": ["accepted"], "stop": {}}, run_and_cancel_at("typed")),
    "cancel_while_starting_and_start_fails": ({"gates": ["typed"], "start": ["fail"]}, run_and_cancel_at("typed")),
    "cancel_before_the_first_keystroke": ({"gates": ["start"]}, late_cancel_before_typing),
    "cancel_during_the_host_command": ({"gates": ["collect1"], "judge": ["accepted"], "stop": {}},
                                       run_and_cancel_at("collect1")),
    "cancel_while_waiting_for_the_worker": ({"idle_worker": [False], "judge": ["accepted"], "timed": True},
                                            lambda env: (env.dispatch_experiment(),
                                                         env.wait_for(lambda: env.held() == "worker_busy"),
                                                         env.cancel(), expect(env, closed=True))),
    "cancel_while_judging": ({"gates": ["judge1"], "judge": ["completed"]}, run_and_cancel_at("judge1")),
    "cancel_while_the_report_is_accepted": ({"gates": ["judge1"], "judge": ["accepted"]},
                                            run_and_cancel_at("judge1")),
    "cancel_after_a_stop": ({"gates": ["collect1"], "judge": ["accepted"],
                             "stop": {"marks": {"stopped_by": "manager", "stop_signal": "SIGINT"}},
                             "exit_status": -2}, run_and_cancel_at("collect1")),
    # -- start failures
    "start_fails": ({"start": ["fail"]}, finishes(held="start_failed:RuntimeError")),
    "start_fails_then_rerun": ({"start": ["fail", "ok"], "judge": ["accepted"]}, rerun_after_failure),
    "start_rejected_by_the_worker_response": ({"start": ["rejected"]}, finishes(held="start_failed:bad_response")),
    "start_fails_and_tells_the_manager": ({"start": ["failure_notice"]}, finishes(held="start_failed:RuntimeError")),
    "start_held_by_the_workflow": ({"start": ["held"]}, finishes(held="start_failed:WorkflowHeld")),
    "start_types_handoff_then_fails": ({"start": ["type_handoff_then_fail"], "home": True},
                                       finishes(held="start_failed:RuntimeError")),
    "start_fails_plain_workflow": ({"start": ["fail"], "late_hold": False}, finishes(held="start_failed:RuntimeError")),
    "paused_just_before_the_first_keystroke": ({"gates": ["start"]}, late_pause_before_typing),
    "held_just_before_the_first_keystroke": ({"gates": ["start"]}, late_hold_before_typing),
    "busy_just_before_the_first_keystroke": ({"gates": ["start"]}, late_busy_before_typing),
    "plain_workflow_whole_start_hold_refused": ({"late_hold": False, "hold_result": "typing", "judge": ["accepted"],
                                                 "timed": True},
                                                busy_then_clear("hold_result", None, "host_terminal_busy",
                                                                status="finished")),
    "late_hold_workflow_busy_before_start": ({"busy": "typing", "judge": ["accepted"], "timed": True},
                                             busy_then_clear("busy", None, "host_terminal_busy",
                                                             status="finished")),
}
FLOW_SCENARIOS = {name: (dict(cfg), driver) for name, (cfg, driver) in FLOW_SCENARIOS.items()}


# ------------------------------------------------------------------------- intentional behaviour deltas (CS-O01 / CS-O02)
# The split commits are pure refactors, but later commits changed the flow ON PURPOSE and the baseline (0dc6392 /
# 1cdc387) predates them: a8e630d (CS-O02, worker reports in send order) added ``FlowTask.report_seq`` and 8d09b67
# (CS-O01, one refusal of a task-less command after a free-work cancel) added ``FlowTask.cancel_fence``. Both fields are
# part of every saved ``task`` ledger record. The differential comparison accepts exactly those two keys inside the
# ``task`` object of a ``task`` ledger record -- nothing else -- and pins their values separately:
#   * CS-O01 arms ``cancel_fence`` only when the MANAGER cancels a free-work (``kind == "work"``) Task that had a run, and
#     writes one extra ``task`` record for it. CS-O02 sets ``report_seq`` only for a worker ``to_manager`` message.
#     No scenario of these suites does either (they are experiments cancelled by the manager or the user, and no worker
#     reports), so in them every record carries the defaults ``cancel_fence is None`` / ``report_seq == 0`` and the
#     record sequence has no extra ``task`` record (the equality below proves the latter). A scenario that arms the
#     fence or numbers a report must be given its own expectation, not slip through this normalisation.
INTENTIONAL_TASK_FIELDS = {"cancel_fence": None, "report_seq": 0}  # field -> value of an untouched Task


def _find_keys(value, keys, path="") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys:
                found.append(f"{path}/{key}")
            found.extend(_find_keys(item, keys, f"{path}/{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_find_keys(item, keys, f"{path}[{index}]"))
    return found


def split_intentional_deltas(outcome: dict) -> tuple[dict, list[tuple[int, str, object]]]:
    """``(outcome without the CS-O01/CS-O02 task fields, [(ledger index, field, value), ...])``.

    Only ``ledger[i]["task"][field]`` of records whose ``type`` is ``task`` is removed; the same keys anywhere else
    stay in the structure (so the equality with the baseline fails on them)."""
    stripped, removed = copy.deepcopy(outcome), []
    for index, record in enumerate(stripped["ledger"]):
        task = record.get("task") if record.get("type") == "task" else None
        if isinstance(task, dict):
            for field in INTENTIONAL_TASK_FIELDS:
                if field in task:
                    removed.append((index, field, task.pop(field)))
    return stripped, removed


def assert_identical_but_for_intentional_deltas(case: unittest.TestCase, after: dict, before: dict) -> None:
    """``after`` (current code) equals ``before`` (baseline) except the two CS-O01/CS-O02 ``task`` record fields."""
    base_stripped, base_removed = split_intentional_deltas(before)
    case.assertEqual(base_removed, [], "the baseline predates cancel_fence/report_seq")
    stripped, removed = split_intentional_deltas(after)
    task_records = [index for index, record in enumerate(after["ledger"]) if record.get("type") == "task"]
    # shape: every saved Task record carries both fields, with the untouched-Task values (see the comment above)
    case.assertEqual(sorted((index, field) for index, field, _ in removed),
                     sorted((index, field) for index in task_records for field in INTENTIONAL_TASK_FIELDS))
    case.assertEqual([(field, value) for _, field, value in removed if value != INTENTIONAL_TASK_FIELDS[field]], [],
                     "an armed/lifted fence or a numbered report appeared where no scenario causes one")
    # nothing outside ledger[].task may mention the new fields
    case.assertEqual(_find_keys(stripped, INTENTIONAL_TASK_FIELDS), [])
    case.assertEqual(stripped, base_stripped)


@needs_baseline
class FlowDifferential(DifferentialBase):
    only: tuple[str, ...] | None = None

    def test_every_scenario_is_identical_before_and_after_the_split(self):
        for name, scenario in FLOW_SCENARIOS.items():
            with self.subTest(scenario=name):
                before = run_flow(BASE_TASKS, BASE_TERMINAL, scenario)
                after = run_flow(current_tasks, current_terminal, scenario)
                assert_identical_but_for_intentional_deltas(self, after, before)

    def test_the_normalisation_hides_only_the_two_task_fields(self):
        """Self-check of the normalisation: it removes the fields in ``task`` records and nothing else."""
        task = {"task_id": "t", "status": "running", "cancel_fence": None, "report_seq": 0}
        outcome = {"ledger": [{"type": "task", "task": dict(task)},
                              {"type": "run_started", "report_seq": 3, "task": dict(task)},
                              {"type": "task", "task": {**task, "cancel_fence": "armed"}}],
                   "tasks": {"t": {"cancel_fence": None}}}
        stripped, removed = split_intentional_deltas(outcome)
        self.assertEqual(stripped["ledger"][0], {"type": "task", "task": {"task_id": "t", "status": "running"}})
        self.assertEqual(stripped["ledger"][1], outcome["ledger"][1], "a non-task record is left alone")
        self.assertEqual(stripped["tasks"], outcome["tasks"])
        self.assertEqual(sorted(removed, key=repr), sorted(
            [(0, "cancel_fence", None), (0, "report_seq", 0), (2, "cancel_fence", "armed"), (2, "report_seq", 0)],
            key=repr))
        self.assertEqual(_find_keys(stripped, INTENTIONAL_TASK_FIELDS),
                         ["/ledger[1]/report_seq", "/ledger[1]/task/cancel_fence", "/ledger[1]/task/report_seq",
                          "/tasks/t/cancel_fence"])
        self.assertEqual(outcome["ledger"][0]["task"], task, "the input is not modified")
        # an armed fence or a numbered report, an unrelated field, a missing field or a changed record is not hidden
        base = {"ledger": [{"type": "task", "task": {"task_id": "t", "status": "running"}}]}
        good = {"ledger": [{"type": "task", "task": dict(task)}]}
        assert_identical_but_for_intentional_deltas(self, good, base)
        for label, bad_task in (("armed", {**task, "cancel_fence": "armed"}), ("lifted", {**task, "cancel_fence": "lifted:x"}),
                                ("seq", {**task, "report_seq": 1}), ("other field", {**task, "extra": 1}),
                                ("changed value", {**task, "status": "held"}),
                                ("missing field", {k: v for k, v in task.items() if k != "report_seq"})):
            with self.subTest(label), self.assertRaises(AssertionError):
                assert_identical_but_for_intentional_deltas(self, {"ledger": [{"type": "task", "task": bad_task}]}, base)
        with self.assertRaises(AssertionError):  # an extra ledger record is not hidden either
            assert_identical_but_for_intentional_deltas(
                self, {"ledger": good["ledger"] * 2}, base)


# ------------------------------------------------------------------------------------------------ flow pins
_CACHE: dict[str, dict] = {}


def current_flow(name: str) -> dict:
    if name not in _CACHE:
        _CACHE[name] = run_flow(current_tasks, current_terminal, FLOW_SCENARIOS[name])
    return _CACHE[name]


def ledger_types(outcome) -> list[str]:
    return [record["type"] for record in outcome["ledger"] if record["type"] != "task"]


def statuses(outcome) -> list[str]:
    """The Task status after each saved snapshot, consecutive repeats collapsed."""
    found: list[str] = []
    for record in outcome["ledger"]:
        if record["type"] == "task" and (not found or found[-1] != record["task"]["status"]):
            found.append(record["task"]["status"])
    return found


def held_reasons(outcome) -> list:
    found: list = []
    for record in outcome["ledger"]:
        if record["type"] == "task" and (not found or found[-1] != record["task"]["held_reason"]):
            found.append(record["task"]["held_reason"])
    return found


def runner(outcome, *prefixes) -> list[str]:
    names = [event["n"] for event in outcome["events"].get("runner", [])]
    return [name for name in names if not prefixes or name.startswith(prefixes)]


def ledger_writes(outcome) -> list[dict]:
    return [event for event in outcome["events"].get("runner", []) if event["n"] == "ledger"]


class FlowPins(unittest.TestCase):
    """Expected values written from the contract (CW-10, CW-18, CS-09, R1, G2); no baseline involved."""

    def test_an_accepted_report_runs_the_whole_chain_in_order(self):
        outcome = current_flow("accepted_report")
        self.assertEqual(ledger_types(outcome),
                         ["task_created", "delegated", "run_started", "host_cwd_restore", "run_ended", "report_receipt"])
        self.assertEqual(statuses(outcome), ["dispatched", "starting", "running", "waiting_report", "finished"])
        order = runner(outcome, "stop.", "run.", "workflow.")
        self.assertEqual(order, ["workflow.start", "stop.register", "run.collect", "stop.command_ended",
                                 "stop.marker", "run.judge", "run.report_hold", "run.close"])
        task = next(iter(outcome["tasks"].values()))
        self.assertEqual((task["status"], task["last_result"]["outcome"], task["last_result"]["report"]),
                         ("finished", "reported", "omp_processed"))
        self.assertEqual(outcome["flow"]["following"], None)
        self.assertEqual(outcome["flow"]["stop_registered"], [])
        self.assertEqual(outcome["flow"]["report_inflight"], [])
        self.assertEqual(outcome["lifecycle"], [{"args": ["run#0"], "n": "experiment_started"},
                                                {"args": ["run#0"], "n": "run_ended"}])

    def test_the_command_end_is_reported_before_the_shell_is_given_back_and_before_any_judgment(self):
        names = runner(current_flow("accepted_report"))
        self.assertLess(names.index("stop.command_ended"), names.index("port.snapshot"))
        self.assertLess(names.index("port.snapshot"), names.index("port.request_takeover"))
        self.assertLess(names.index("port.request_takeover"), names.index("stop.marker"))
        self.assertLess(names.index("stop.marker"), names.index("run.judge"))
        self.assertEqual(names[-2:], ["port.release_hold", "port.detach"])

    def test_the_ledger_writes_of_the_start_happen_under_the_flow_lock_in_order(self):
        outcome = current_flow("accepted_report")
        writes = ledger_writes(outcome)
        self.assertTrue(all(event["st"]["lock"] for event in writes), "every ledger write is under the flow lock")
        sequence = [(e["type"], e.get("status")) for e in writes]
        start = sequence.index(("task", "starting"))
        self.assertEqual(sequence[start:start + 3], [("task", "starting"), ("task", "running"), ("run_started", None)])
        self.assertEqual(writes[start]["st"]["following"], writes[start]["st"]["run_id"] or writes[start]["st"]["following"])
        self.assertIsNotNone(writes[start]["st"]["following"], "_following is set with the status change, before the save")
        self.assertEqual(writes[start]["st"]["runs_started"], 1)

    def test_a_registered_stop_target_is_reported_ended_once_and_a_failed_registration_never(self):
        registered = current_flow("stop_target_registered_and_ended")
        failed = current_flow("stop_registration_fails")
        self.assertEqual(runner(registered, "stop."), ["stop.register", "stop.command_ended", "stop.marker"])
        self.assertEqual(runner(failed, "stop."), ["stop.register", "stop.registration_failed", "stop.marker"])
        self.assertEqual(next(iter(failed["tasks"].values()))["status"], "finished", "still followed and judged")
        self.assertIn("run.judge", runner(failed))

    def test_a_stop_signalled_command_is_closed_as_stopped_without_judgment(self):
        outcome = current_flow("stopped_by_the_manager")
        self.assertNotIn("run.judge", runner(outcome))
        ended = [r for r in outcome["ledger"] if r["type"] == "run_ended"][0]
        self.assertEqual((ended["outcome"], ended["stopped_by"], ended["exit_code"], ended["signal"]),
                         ("stopped", "manager", 130, 2))
        self.assertEqual(statuses(outcome), ["dispatched", "starting", "running", "finished"])

    def test_a_failed_start_frees_the_worker_tells_the_manager_and_registers_nothing(self):
        outcome = current_flow("start_fails")
        failed = [r for r in outcome["ledger"] if r["type"] == "run_start_failed"][0]
        self.assertEqual(failed["error"], "RuntimeError")
        task = next(iter(outcome["tasks"].values()))
        self.assertEqual((task["status"], task["held"]), ("finished", "start_failed:RuntimeError"))
        self.assertEqual(task["last_result"]["outcome"], "start_failed")
        self.assertEqual(statuses(outcome), ["dispatched", "starting", "finished"])
        names = runner(outcome)
        self.assertNotIn("stop.register", names)
        self.assertNotIn("run.close", names)
        self.assertEqual(names.count("port.release_hold"), 3, "the start's hold is released after the give-back, then "
                                                           "once more by the failure path and the runner")
        self.assertEqual(outcome["flow"]["following"], None)
        self.assertEqual(task["runs_started"], 1)

    def test_a_start_that_typed_the_handoff_is_given_back_under_the_hold(self):
        outcome = current_flow("start_types_handoff_then_fails")
        names = runner(outcome, "port.")
        self.assertIn("port.send_user", names)
        self.assertLess(names.index("port.send_user"), names.index("port.request_takeover"))
        self.assertIn("port.poll", names, "a typed handoff waits for the shell to settle first")

    def test_the_manager_hears_of_a_start_failure_once(self):
        outcome = current_flow("start_fails_and_tells_the_manager")
        notices = [r for r in outcome["ledger"] if r["type"] == "start_failure_notice"]
        self.assertEqual([r["state"] for r in notices], ["queued", "submitted", "delivered"])
        self.assertEqual(len({r.get("message_id") for r in notices if "message_id" in r}), 1)
        reports = [m for m in outcome["mailbox"] if m["target"] == "manager" and m["kind"] == "report"]
        self.assertEqual(len(reports), 1, "one notice to the manager, never resent")
        self.assertEqual(reports[0]["payload"]["notice"], "run_start_failed")

    def test_a_cancel_while_the_start_is_in_flight_wins_over_the_start_failure(self):
        outcome = current_flow("cancel_while_starting_and_start_fails")
        self.assertEqual(ledger_types(outcome)[-3:], ["run_start_failed", "task_closed", "notice"])
        task = next(iter(outcome["tasks"].values()))
        self.assertEqual((task["status"], task["closed_reason"]), ("closed", "cancelled"))

    def test_a_cancel_before_the_start_returns_early_without_a_run(self):
        outcome = current_flow("cancel_while_pending_start")
        self.assertNotIn("workflow.start", runner(outcome))
        self.assertEqual(statuses(outcome)[-1], "closed")
        self.assertNotIn("run_started", ledger_types(outcome))

    def test_a_cancel_during_the_host_command_skips_the_judgment_and_closes_after_the_exit(self):
        outcome = current_flow("cancel_during_the_host_command")
        names = runner(outcome)
        self.assertNotIn("run.judge", names)
        self.assertLess(names.index("stop.command_ended"), names.index("port.request_takeover"))
        self.assertEqual(statuses(outcome)[-1], "closed")
        self.assertEqual(ledger_types(outcome)[-2:], ["task_closed", "notice"])

    def test_a_cancel_while_the_worker_is_busy_ends_the_run_without_judging(self):
        outcome = current_flow("cancel_while_waiting_for_the_worker")
        self.assertNotIn("run.judge", runner(outcome))
        self.assertIn("worker_busy", held_reasons(outcome))
        self.assertEqual(statuses(outcome)[-1], "closed")

    def test_the_judge_waits_for_an_idle_worker_and_asks_once(self):
        outcome = current_flow("judge_busy_then_ready")
        self.assertEqual([e["value"] for e in outcome["events"]["runner"] if e["n"] == "omp_idle"],
                         [False, False, True])
        self.assertEqual(runner(outcome, "run.judge"), ["run.judge"])
        self.assertIn("worker_busy", held_reasons(outcome))
        self.assertEqual(held_reasons(outcome)[-1], None)

    def test_a_deferred_analysis_is_asked_again_and_a_stuck_one_is_given_up(self):
        again = current_flow("judge_deferred_then_ready")
        self.assertEqual(runner(again, "run.judge"), ["run.judge"] * 3)
        self.assertIn("worker_busy", held_reasons(again))
        gave_up = current_flow("judge_deferred_for_too_long")
        task = next(iter(gave_up["tasks"].values()))
        self.assertEqual((task["status"], task["held"], task["last_result"]["outcome"]),
                         ("finished", "judgment_unavailable:worker_not_idle_for_analysis", "judgment_unavailable"))
        self.assertEqual(statuses(gave_up)[-1], "finished")

    def test_a_busy_worker_is_given_up_with_the_last_review_outcome(self):
        outcome = current_flow("judge_busy_for_too_long")
        task = next(iter(outcome["tasks"].values()))
        self.assertEqual(task["held"], "judgment_unavailable:worker_not_idle_for_analysis:review_failed:stuck")
        self.assertNotIn("run.judge", runner(outcome))

    def test_a_manager_deferred_report_is_resent_when_the_manager_can_take_it(self):
        outcome = current_flow("report_deferred_then_accepted")
        names = runner(outcome, "run.", "omp_idle")
        self.assertEqual(names, ["run.collect", "run.judge", "run.report_hold", "omp_idle", "omp_idle", "omp_idle",
                                 "run.retry_report", "run.close"])
        self.assertEqual([e["value"] for e in outcome["events"]["runner"] if e["n"] == "omp_idle"],
                         [False, False, True])
        self.assertIn("manager_busy", held_reasons(outcome))
        self.assertEqual(ledger_types(outcome)[-2:], ["run_ended", "report_receipt"])

    def test_a_manager_hold_is_offered_to_the_judge_and_blocks_the_resend(self):
        offered = current_flow("report_hold_is_offered_to_the_judge")
        values = [e["value"] for e in offered["events"]["runner"] if e["n"] == "run.report_hold"]
        self.assertEqual(values, ["manager_maintenance"])
        held = current_flow("report_deferred_manager_hold")
        self.assertIn("manager_maintenance", held_reasons(held))
        self.assertEqual(runner(held, "run.retry_report"), ["run.retry_report"])

    def test_a_pause_while_collecting_is_passed_to_the_collect_call(self):
        outcome = current_flow("paused_during_collect")
        collects = [e for e in outcome["events"]["runner"] if e["n"] == "run.collect"]
        self.assertEqual([e["paused"] for e in collects], [False, True, False])
        self.assertEqual([e["step"] for e in collects], ["running", "running", "exited"])

    def test_a_pause_before_judging_holds_and_resumes(self):
        outcome = current_flow("paused_while_waiting_to_judge")
        self.assertIn("paused", held_reasons(outcome))
        self.assertEqual(runner(outcome, "run.judge"), ["run.judge"])

    def test_nothing_is_typed_when_the_workflow_asks_just_before_the_first_keystroke(self):
        for name, error in (("paused_just_before_the_first_keystroke", "paused"),
                            ("held_just_before_the_first_keystroke", "maintenance"),
                            ("busy_just_before_the_first_keystroke", "host_terminal_busy"),
                            ("cancel_before_the_first_keystroke", "cancelled")):
            with self.subTest(name):
                outcome = current_flow(name)
                failed = [r for r in outcome["ledger"] if r["type"] == "run_start_failed"][0]
                self.assertEqual(failed["error"], error)
                self.assertNotIn("run.collect", runner(outcome))

    def test_the_whole_start_hold_of_a_workflow_without_a_late_hook_refuses_and_retries(self):
        outcome = current_flow("plain_workflow_whole_start_hold_refused")
        self.assertIn("host_terminal_busy", held_reasons(outcome))
        starts = [e for e in outcome["events"]["runner"] if e["n"] == "workflow.start"]
        self.assertEqual(len(starts), 1, "the held attempts never reached the workflow")
        self.assertIs(starts[0]["before_shell_input"], False)

    def test_a_judge_that_returns_nothing_leaves_the_runner_error_hold_and_never_finishes(self):
        for name in ("judge_returns_none", "report_deferred_retry_returns_none"):
            with self.subTest(name):
                outcome = current_flow(name)
                task = next(iter(outcome["tasks"].values()))
                self.assertEqual((task["status"], task["held"]), ("waiting_report", "runner_error:AttributeError"))
                self.assertIn("run.close", runner(outcome), "the run is still closed")
                self.assertEqual(outcome["flow"]["report_inflight"], [])

    def test_a_failed_resend_after_the_manager_took_the_report_ends_the_follow_quietly(self):
        outcome = current_flow("report_deferred_retry_fails_after_acceptance")
        task = next(iter(outcome["tasks"].values()))
        self.assertEqual((task["status"], task["held"]), ("finished", None), "no runner_error follows the error")
        self.assertEqual(task["last_result"]["error"], "report_error:RuntimeError")
        self.assertEqual(ledger_types(outcome)[-2:], ["run_ended", "run_error"])

    def test_a_report_whose_delivery_is_unknown_clears_the_in_flight_mark(self):
        outcome = current_flow("report_sent_but_its_outcome_is_unknown")
        self.assertEqual(outcome["flow"]["report_inflight"], [])
        self.assertEqual(next(iter(outcome["tasks"].values()))["last_result"]["outcome"], "report_outcome_unknown")

    def test_the_handoff_watch_is_removed_from_the_port_after_a_good_start(self):
        self.assertIs(current_flow("accepted_report")["flow"]["port_send_user_wrapped"], False)

    def test_an_unknown_shell_state_during_a_cancel_keeps_the_cancelling_status(self):
        outcome = current_flow("cancel_then_shell_state_unknown")
        task = next(iter(outcome["tasks"].values()))
        self.assertEqual((task["status"], task["held"]), ("cancelling", "shell_unknown"))
        self.assertNotIn("held", statuses(outcome))

    def test_closing_the_flow_ends_every_waiting_follow(self):
        for name, last in (("flow_closed_while_the_manager_is_busy", "run.report_hold"),
                           ("flow_closed_while_the_worker_is_busy", "port.release_hold"),
                           ("flow_closed_during_the_host_command", "run.collect")):
            with self.subTest(name):
                outcome = current_flow(name)
                self.assertIn("run.close", runner(outcome))
                self.assertEqual(runner(outcome)[-1], "port.detach")

    def test_a_judge_error_after_the_report_was_accepted_keeps_the_run_closed(self):
        outcome = current_flow("judge_error_after_report_accepted")
        self.assertEqual(ledger_types(outcome)[-2:], ["run_ended", "run_error"])
        error = [r for r in outcome["ledger"] if r["type"] == "run_error"][0]
        self.assertEqual((error["stage"], error["error"]), ("after_report_accepted", "report_error:RuntimeError"))

    def test_an_unavailable_analysis_is_indeterminate_and_not_replayed(self):
        outcome = current_flow("judge_unavailable")
        self.assertEqual(runner(outcome, "run.judge"), ["run.judge"])
        ended = [r for r in outcome["ledger"] if r["type"] == "run_ended"][0]
        self.assertEqual((ended["outcome"], ended["judgment"]), ("judgment_unavailable", "indeterminate"))
        self.assertEqual(next(iter(outcome["tasks"].values()))["held"], "judgment_unavailable:analysis_rejected")

    def test_the_shell_state_unknown_holds_without_judgment(self):
        outcome = current_flow("shell_state_unknown")
        task = next(iter(outcome["tasks"].values()))
        self.assertEqual((task["status"], task["held"]), ("held", "shell_unknown"))
        self.assertNotIn("run.judge", runner(outcome))
        self.assertEqual(runner(outcome, "stop.command_ended"), ["stop.command_ended"])

    def test_a_second_run_of_the_same_task_counts_and_follows_like_the_first(self):
        outcome = current_flow("two_consecutive_runs")
        task = next(iter(outcome["tasks"].values()))
        self.assertEqual(task["runs_started"], 2)
        self.assertEqual(ledger_types(outcome).count("run_started"), 2)
        self.assertEqual(runner(outcome, "run.close"), ["run.close", "run.close"])


@needs_baseline
class FlowRepeatability(DifferentialBase):
    def test_a_scenario_repeats_exactly_on_the_same_version(self):
        # a flaky scenario would make the differential check meaningless
        for name in ("accepted_report", "report_deferred_then_accepted", "judge_busy_for_too_long",
                     "cancel_while_judging", "start_fails"):
            with self.subTest(scenario=name):
                again = run_flow(current_tasks, current_terminal, FLOW_SCENARIOS[name])
                self.assertEqual(again, current_flow(name))
