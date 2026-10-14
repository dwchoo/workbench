"""Repository-only fast and representative core precise validation runner.

Every child gets a fresh credential-free environment and a private namespace.
This is developer evidence, never a product compatibility/adoption verdict.
"""

from __future__ import annotations

import argparse
import ctypes
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tests.integration import cw16_harness as harness  # noqa: E402

SCHEMA_VERSION = 1
TARGET_SECONDS = {"fast": 120, "precise": 600}
CAPTURE_BYTES = 16_384
READINESS_SECONDS = 30
SCENARIO_IDLE_SECONDS = 120
CLEANUP_SECONDS = 20
CONTROL_BYTES = 1_000_000
CONTROL_LINE_BYTES = 1024
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_PR_SET_PDEATHSIG = 1
CELL_RESULT_BYTES = 4 * 1024 * 1024
BASELINE_REPORT_BYTES = 32 * 1024 * 1024
_CELL_OBSERVER = None


class _Interrupted(BaseException):
    pass


@contextmanager
def _interrupt_signals():
    """One interruption initiates cleanup; repeated signals cannot cut it short."""
    interrupted = False

    def stop(signum, frame):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise _Interrupted(f"signal {signum}")

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _cell_observe(event: str, **fields: Any) -> None:
    if _CELL_OBSERVER is not None:
        _CELL_OBSERVER(event, **fields)


@contextmanager
def _owned_subreaper():
    """Temporarily adopt only descendants of this runner; restore prior Linux state."""
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(previous), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot read child-subreaper state")
    changed = previous.value == 0
    if changed and libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot enable child subreaper")
    try:
        yield
    finally:
        if changed and libc.prctl(_PR_SET_CHILD_SUBREAPER, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot restore child-subreaper state")


@dataclass(frozen=True)
class Item:
    key: str
    assertion_ids: tuple[str, ...]
    modules: tuple[str, ...]
    evidence_level: str
    expected_wait_seconds: float = 0
    env: dict[str, str] = field(default_factory=dict)
    scenario_steps: dict[str, tuple[str, ...]] = field(default_factory=dict)
    allowed_na: tuple[tuple[str, str], ...] = ()
    outer: str | None = None


FAST_ITEMS = (
    Item("harness_fixture", ("DV-AC-02", "DV-AC-04"),
         ("tests.integration.test_cw16_harness", "tests.dev_validation.test_runner"), "fixture"),
    Item("launcher_skill_filter", ("DV-AC-01", "DV-AC-04"),
         ("tests.backend.test_paths_launcher", "tests.backend.test_workbench_skills"), "fixture"),
    Item("installed_plain_bash", ("DV-AC-02", "DV-AC-04"),
         ("tests.dev_validation.test_fast_smoke",), "runtime", env={"WB_DVL_REAL_SMOKE": "1"}),
)
_COMPAT = "tests.integration.live_cw16_compat"
_POLICY = "tests.integration.live_cw16_flow_policy.Cw16PolicyScenarios"
_FAULT = "tests.integration.live_cw16_fault.Cw16FaultScenarios"
_CORE_ASSERTIONS = ("DV-AC-02", "DV-AC-03", "DV-AC-04", "DV-AC-05")
_COMPAT_STEPS = ("C1", "C2", "C3", "C4", "C5", "C6", "C7")

# Exact existing Bash and sh(dash) unittest IDs, not module discovery:
# VM/model/full regression suites are never implicitly selected here.
PRECISE_ITEMS = tuple(
    Item(f"compat_{outer}_{shell}", (*_CORE_ASSERTIONS, *_COMPAT_STEPS),
         (f"{_COMPAT}.Cw16CompatMatrix.test_{outer}_{shell}",), "runtime", 65,
         {"WB_LIVE_CW16": "1", "WB_CW16_OUTERS": outer, "WB_CW16_SHELLS": shell,
          "WB_CW16_DETACH_SECONDS": "65"},
         {f"compat-{outer}-{shell}": _COMPAT_STEPS},
         ((f"compat-plain-{shell}", "C6"),) if outer == "plain" else (), outer)
    for outer in ("plain", "tmux", "herdr") for shell in ("bash", "dash")
) + (
    Item("shell_selection", (*_CORE_ASSERTIONS, "SEL1", "SEL2", "SEL3"), (
        f"{_COMPAT}.Cw16ShellSelection.test_sel1_bash_first_fake_zsh_never_executed",
        f"{_COMPAT}.Cw16ShellSelection.test_sel2_no_bash_falls_back_to_dash",
        f"{_COMPAT}.Cw16ShellSelection.test_sel3_neither_bash_nor_sh_guidance_nonzero_nothing_started",
    ), "runtime", env={"WB_LIVE_CW16": "1", "WB_CW16_SHELLS": "bash"},
         scenario_steps={"sel-1": ("start", "operate", "shutdown"),
                         "sel-2": ("start", "operate", "shutdown"), "sel-3": ("start_refused",)}),
    Item("review_busy_end", (*_CORE_ASSERTIONS, "P1", "P7"),
         (f"{_POLICY}.test_p1_periodic_reviews_merge_and_p7_usage",), "runtime", 200,
         {"WB_LIVE_CW16": "1", "WB_CW16_SCENARIOS": "P1"},
         {"policy-P1-P7": ("start", "P1_reviews_60s_merge_when_busy",
                           "P7_usage_in_review_facts_and_display", "shutdown")}),
    Item("pause_end_no_replay", (*_CORE_ASSERTIONS, "P2"),
         (f"{_POLICY}.test_p2_pause_holds_automatic_work",), "runtime", 90,
         {"WB_LIVE_CW16": "1", "WB_CW16_SCENARIOS": "P2"},
         {"policy-P2": ("start", "P2_pause_holds_sends_and_terminal",
                        "P2_run_end_shown_no_review_while_paused", "P2_resume_reconciled_no_replay", "shutdown")}),
    Item("pause_interrupts_manager_turn", (*_CORE_ASSERTIONS, "P3"),
         (f"{_POLICY}.test_p3_pause_interrupts_manager_turn",), "runtime", 100,
         {"WB_LIVE_CW16": "1", "WB_CW16_SCENARIOS": "P3"},
         {"policy-P3": ("start", "P3_pause_aborts_streaming_manager_turn",
                         "P3_running_manager_tool_recorded_unknown", "shutdown")}),
    Item("restart_worker_survivor_host", (*_CORE_ASSERTIONS, "P6"),
         (f"{_POLICY}.test_p6_restart_worker_stop_survivor_kill_host",), "runtime",
         env={"WB_LIVE_CW16": "1", "WB_CW16_SCENARIOS": "P6"},
         scenario_steps={"policy-P6": ("start", "P6_restart_worker_verified",
                                        "P6_stop_survivor_unknown_refused", "P6_host_terminal_force_kill",
                                        "shutdown")}),
    Item("detach_active_work", (*_CORE_ASSERTIONS, "X1"),
         (f"{_FAULT}.test_x1_detach_with_active_work",), "runtime", 515,
         {"WB_LIVE_CW16": "1", "WB_CW16_FAULTS": "X1", "WB_CW16_DETACH_SECONDS": "65"},
         {"fault-x1": ("X1.start_run", "X1.detach_active_review_continues", "X1.takeover_requested",
                       "X1.detach_takeover_state_kept", "X1.detach_paused_pause_kept",
                       "X1.resume_no_replay", "X1.shutdown")}),
    Item("backend_restart_no_replay", (*_CORE_ASSERTIONS, "X2"),
         (f"{_FAULT}.test_x2_backend_sigkill_restart",), "runtime", 25,
         {"WB_LIVE_CW16": "1", "WB_CW16_FAULTS": "X2"},
         {"fault-x2": ("X2.running_open_task", "X2.sigkill_backend", "X2.restart_reconcile",
                       "X2.backend_restarted_once", "X2.no_replay", "X2.status_text",
                       "X2.stop_survivor_unverified_refused", "X2.stop_survivor_verified_stopped", "X2.shutdown")}),
    Item("scripted_model_fault_holds", (*_CORE_ASSERTIONS, "X3"),
         (f"{_FAULT}.test_x3_model_fault_holds",), "runtime", 45,
         {"WB_LIVE_CW16": "1", "WB_CW16_FAULTS": "X3"},
         {"fault-x3": ("X3.worker_fault_hold", "X3.analysis_waits_for_worker", "X3.worker_recovers",
                       "X3.analysis_after_recovery", "X3.p2_2_first_tool_allowed",
                       "X3.p2_1_manager_fault_report_kept", "X3.p2_1_delivered_once", "X3.shutdown")}),
    Item("shutdown_confirmation", (*_CORE_ASSERTIONS, "X4"),
         (f"{_FAULT}.test_x4_shutdown_confirmation",), "runtime",
         env={"WB_LIVE_CW16": "1", "WB_CW16_FAULTS": "X4"},
         scenario_steps={"fault-x4": ("X4.no_yes_refused_active_listed", "X4.yes_verified_nothing_left",
                                     "X4.owned_broker_not_alive_at_verified", "X4.survivor_unverified_exit1")}),
    Item("core_metadata_raw_faults", (*_CORE_ASSERTIONS, "X6"),
         (f"{_FAULT}.test_x6_storage_faults",), "runtime",
         env={"WB_LIVE_CW16": "1", "WB_CW16_FAULTS": "X6"},
         scenario_steps={"fault-x6": ("X6.run_cap_64mib_run_continues", "X6.restart_keeps_task_spec_worktree",
                                     "X6.project_cap_reduced_fixture", "X6.start_fault_sandbox",
                                     "X6.raw_log_store_failure_shown", "X6.metadata_hold_and_pause_store_failure",
                                     "X6.pause_store_failure_visible_at_170", "X6.metadata_restored", "X6.shutdown")}),
    Item("takeover_restart_no_replay", (*_CORE_ASSERTIONS, "X7"),
         # Existing terminal fixed 120 s wait plus the original 15 s no-replay observation.
         (f"{_FAULT}.test_x7_takeover_during_delivery_restart",), "runtime", 135,
         {"WB_LIVE_CW16": "1", "WB_CW16_FAULTS": "X7"},
         {"fault-x7": ("X7.command_delivered", "X7.takeover_holds_new_sends", "X7.backend_restart",
                       "X7.old_command_unknown_no_replay", "X7.shutdown")}),
)
PRECISE_PENDING = (
    "q01_product_adapter_recheck", "new_storage_summary_raw_product_gates",
    "model_metadata", "product_full_dvl14",
)


def _private_dir(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or path.parent.resolve() != path.parent:
        raise ValueError("report-dir must be an absolute non-symlink private path")
    if not path.exists():
        path.mkdir(mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("report-dir must be an owned 0700 directory")
    if path.resolve() == REPO.resolve() or path.resolve() == Path.home().resolve():
        raise ValueError("report-dir must not be repository or HOME root")
    if list(path.iterdir()):
        raise ValueError("report-dir must be empty for this invocation")
    return path


def _input_manifest() -> dict[str, Any]:
    roots = ("src", "omp_bridge", "tests", "tools")
    files: list[dict[str, Any]] = []
    for root in roots:
        for path in sorted((REPO / root).rglob("*")):
            if any(part in ("__pycache__", ".pytest_cache", ".workflow") for part in path.parts):
                continue
            if path.is_symlink():
                files.append({"path": str(path.relative_to(REPO)), "kind": "symlink", "target": os.readlink(path)})
            elif path.is_file():
                files.append({"path": str(path.relative_to(REPO)), "kind": "file",
                              "mode": path.stat().st_mode & 0o777, "sha256": sha256(path.read_bytes()).hexdigest()})
    for name in ("pyproject.toml", "AGENTS.md"):
        path = REPO / name
        if path.is_file():
            files.append({"path": name, "kind": "file", "mode": path.stat().st_mode & 0o777,
                          "sha256": sha256(path.read_bytes()).hexdigest()})
    payload = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {"sha256": sha256(payload).hexdigest(), "fileCount": len(files), "files": files}


def _base_env(home: Path, namespace: Path, omp: Path, item: Item) -> dict[str, str]:
    """Do not inherit credentials, outer-terminal context or user configuration."""
    home.mkdir(mode=0o700)
    for leaf in ("config", "data", "state", "cache", "runtime"):
        (home / leaf).mkdir(mode=0o700)
    env = harness.clean_base_env(
        PATH=f"/usr/bin:/bin:{omp.parent}", HOME=str(home),
        XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
        XDG_STATE_HOME=str(home / "state"), XDG_CACHE_HOME=str(home / "cache"),
        XDG_RUNTIME_DIR=str(home / "runtime"), TMPDIR=str(namespace),
        PYTHONPATH=str(REPO / "src"), PYTHONDONTWRITEBYTECODE="1", SHELL="/usr/bin/bash",
        WB_DVL_OMP=str(omp), WB_DVL_NAMESPACE=str(namespace),
        WB_CW16_REPORT_DIR=str(namespace / "reports"),
        **{key: harness.BLOCKED_PROXY for key in harness.PROXY_KEYS},
        NO_PROXY="127.0.0.1", no_proxy="127.0.0.1", **item.env,
    )
    if item.scenario_steps:
        env.update(WB_CW16_OMP=str(omp), WB_CW16_RUN_ID=item.key)
        if item.outer in ("tmux", "herdr"):
            tool = harness.find_tool(item.outer)
            if tool:
                env[f"WB_CW16_{item.outer.upper()}"] = tool
    # The harness's guard only permits its own WORKBENCH data paths, never inherited ones.
    harness.assert_env_isolated(env, home.parent)
    return env


def _append_bounded(existing: bytearray, chunk: bytes) -> int:
    dropped = max(0, len(existing) + len(chunk) - CAPTURE_BYTES)
    if dropped:
        del existing[:min(dropped, len(existing))]
    existing.extend(chunk)
    if len(existing) > CAPTURE_BYTES:
        del existing[:len(existing) - CAPTURE_BYTES]
    return dropped


def _driver_main(invocation: str, item_key: str, nonce: str,
                 control_fd: int, modules: list[str]) -> int:
    """Trusted unittest result/ownership channel, separate from test-controlled stdout/stderr."""
    # The parent defers interruption until its Popen cleanup guard is installed.
    # A driver/test/Native descendant must not inherit that temporary mask.
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
    import unittest

    sequence = 0

    def emit(event: str, *, ordered: bool = True, **fields: Any) -> None:
        nonlocal sequence
        if ordered:
            sequence += 1
        frame = {"version": 1, "invocation": invocation, "item": item_key, "nonce": nonce,
                 "sequence": sequence if ordered else 0, "event": event, **fields}
        try:
            os.write(control_fd, json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        except OSError:
            pass  # Missing/invalid control evidence is inconclusive in the parent.

    original_popen = subprocess.Popen

    class OwnedPopen(original_popen):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, **kwargs)
            start = harness.ticks(self.pid)
            if start is not None:
                emit("owned", ordered=False, pid=self.pid, startTicks=start,
                     emitterPid=os.getpid(), emitterTicks=harness.ticks(os.getpid()),
                     origin="popen")

    subprocess.Popen = OwnedPopen
    original_fork = os.fork

    def owned_fork() -> int:
        child = original_fork()
        if child > 0:
            start = harness.ticks(child)
            if start is not None:
                emit("owned", ordered=False, pid=child, startTicks=start,
                     emitterPid=os.getpid(), emitterTicks=harness.ticks(os.getpid()),
                     origin="fork")
        return child

    os.fork = owned_fork

    class StructuredResult(unittest.TextTestResult):
        def stopTest(self, test: Any) -> None:
            super().stopTest(test)
            emit("completed")

    try:
        suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2,
                                         resultclass=StructuredResult).run(suite)
        emit("suite", testsRun=result.testsRun, skipped=len(result.skipped),
             failures=len(result.failures), errors=len(result.errors),
             unexpectedSuccesses=len(result.unexpectedSuccesses))
        return 0 if result.wasSuccessful() else 1
    except BaseException as exc:
        emit("driver_error", error=type(exc).__name__)
        raise
    finally:
        os.close(control_fd)


class _ControlState:
    """Bounded, invocation-bound frames from the runner-owned driver fd."""

    def __init__(self, invocation: str, item: str, nonce: str):
        self.invocation = invocation
        self.item = item
        self.nonce = nonce
        self.pending = bytearray()
        self.bytes_seen = 0
        self.sequence = 0
        self.completed = 0
        self.owned: dict[int, int] = {}
        self.claims: list[tuple[int, int, int, int]] = []
        self.root: tuple[int, int] | None = None
        self.suite: dict[str, int] | None = None
        self.error: str | None = None

    def bind_root(self, pid: int, start: int | None) -> None:
        if start is None:
            self.error = "driver start ticks unavailable"
            return
        self.root = (pid, start)
        self._resolve_owned()

    def _resolve_owned(self) -> None:
        if self.root is None:
            return
        connected = {self.root}
        changed = True
        while changed:
            changed = False
            for pid, start, parent_pid, parent_start in self.claims:
                if (parent_pid, parent_start) in connected and (pid, start) not in connected:
                    connected.add((pid, start))
                    changed = True
        self.owned = {pid: start for pid, start in connected if (pid, start) != self.root}

    def feed(self, chunk: bytes) -> bool:
        """Return True only when a genuine test completion advanced the watchdog."""
        self.bytes_seen += len(chunk)
        if self.bytes_seen > CONTROL_BYTES:
            self.error = "control channel exceeded bound"
            return False
        self.pending.extend(chunk)
        advanced = False
        while b"\n" in self.pending:
            line, _, tail = self.pending.partition(b"\n")
            self.pending = bytearray(tail)
            if len(line) > CONTROL_LINE_BYTES:
                self.error = "control frame exceeded bound"
                continue
            try:
                frame = json.loads(line)
                if (type(frame) is not dict or frame.get("version") != 1
                        or frame.get("invocation") != self.invocation
                        or frame.get("item") != self.item or frame.get("nonce") != self.nonce
                        or type(frame.get("sequence")) is not int):
                    raise ValueError("control identity/order mismatch")
                event = frame.get("event")
                if event == "owned":
                    pid, start = frame.get("pid"), frame.get("startTicks")
                    parent_pid, parent_start = frame.get("emitterPid"), frame.get("emitterTicks")
                    if (frame["sequence"] != 0 or frame.get("origin") not in {"popen", "fork"}
                            or any(type(value) is not int or value < 1
                                   for value in (pid, start, parent_pid, parent_start))):
                        raise ValueError("invalid owned identity")
                    self.claims.append((pid, start, parent_pid, parent_start))
                    self._resolve_owned()
                    continue
                if frame["sequence"] != self.sequence + 1 or self.suite is not None:
                    raise ValueError("control sequence mismatch")
                self.sequence += 1
                if event == "completed":
                    self.completed += 1
                    advanced = True
                elif event == "suite":
                    names = ("testsRun", "skipped", "failures", "errors", "unexpectedSuccesses")
                    values = {name: frame.get(name) for name in names}
                    if (any(type(value) is not int or value < 0 for value in values.values())
                            or values["testsRun"] != self.completed
                            or values["skipped"] > values["testsRun"]):
                        raise ValueError("invalid suite result counts")
                    self.suite = values
                elif event == "driver_error":
                    self.error = "driver error: " + str(frame.get("error"))[:80]
                else:
                    raise ValueError("unknown control event")
            except (ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.error = "malformed control frame: " + type(exc).__name__
        if len(self.pending) > CONTROL_LINE_BYTES:
            self.error = "control frame exceeded bound"
            self.pending.clear()
        return advanced and self.error is None

    def valid(self) -> bool:
        return (self.error is None and not self.pending and self.suite is not None
                and self.root is not None and len(self.owned) == len({(pid, start)
                    for pid, start, _, _ in self.claims}))


def _direct_children(parent_pid: int) -> dict[int, int]:
    """Read exact direct children, including Z for the pre-item safety baseline."""
    found: dict[int, int] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        fields = harness.stat_fields(pid)
        if (fields is not None and fields[0] != b"X"
                and int(fields[1]) == parent_pid):
            found[pid] = int(fields[19])
    return found


def _owned_members(proc: subprocess.Popen[bytes], namespace: Path,
                   inherited: dict[int, int] | None = None, *,
                   baseline_children: dict[int, int] | None = None,
                   adopted_seen: dict[int, int] | None = None,
                   adopted_zombies: set[int] | None = None,
                   driver_start: int | None = None) -> dict[int, int]:
    """Exact item session/driver receipts plus corroborated subreaper adoptions.

    Namespace text alone is never ownership. A candidate must be a new direct
    child adopted by this runner, born no earlier than its driver, and visibly
    using this item's private path. Pre-existing direct children are excluded.
    """
    members = harness.session_members((proc.pid,))
    members.update({pid: start for pid, start in (inherited or {}).items()
                    if harness.alive(pid, start)})
    if baseline_children is not None and adopted_seen is not None and driver_start is not None:
        mentioned = harness.processes_mentioning(str(namespace))
        for pid, start in _direct_children(os.getpid()).items():
            if pid == proc.pid or start < driver_start or baseline_children.get(pid) == start:
                continue
            fields = harness.stat_fields(pid)
            if fields is None or int(fields[19]) != start or int(fields[1]) != os.getpid():
                continue
            if fields[0] == b"Z":
                # A zombie has no useful cmdline/cwd. Within the single-cell
                # subreaper window, new exact direct children absent from the
                # baseline are adopted item children; reap only by this PID.
                adopted_seen[pid] = start
                if adopted_zombies is not None:
                    adopted_zombies.add(pid)
            elif mentioned.get(pid) == start:
                adopted_seen[pid] = start
        members.update({pid: start for pid, start in adopted_seen.items()
                        if harness.alive(pid, start)})
    members.pop(os.getpid(), None)
    return members


def _stop_owned(proc: subprocess.Popen[bytes], start_ticks: int | None, namespace: Path,
                inherited: dict[int, int] | None = None, *,
                baseline_children: dict[int, int] | None = None,
                adopted_seen: dict[int, int] | None = None,
                adopted_zombies: set[int] | None = None) -> dict[str, Any]:
    """Signal only PID/start-ticks identities in the item's private session/root."""
    signalled: list[int] = []
    def current() -> dict[int, int]:
        return _owned_members(proc, namespace, inherited,
                              baseline_children=baseline_children,
                              adopted_seen=adopted_seen, adopted_zombies=adopted_zombies,
                              driver_start=start_ticks)

    deadline = time.monotonic() + CLEANUP_SECONDS
    attempted: set[tuple[int, int]] = set()
    while time.monotonic() < deadline:
        targets = current()
        if start_ticks is not None and harness.alive(proc.pid, start_ticks):
            targets[proc.pid] = start_ticks
        if not targets:
            break
        for pid, ticks in targets.items():
            if (pid, ticks) not in attempted:
                attempted.add((pid, ticks))
                if harness.kill_exact(pid, ticks, signal.SIGTERM):
                    signalled.append(pid)
        time.sleep(0.05)
    for pid, ticks in current().items():
        if harness.kill_exact(pid, ticks):
            signalled.append(pid)
    return {"signalledPids": sorted(set(signalled)),
            "remaining": sorted(current())}


def _reap_adopted(owned: dict[int, int]) -> list[int]:
    """Wait only for exact, trusted owned children adopted by this runner."""
    reaped = []
    for pid, start in owned.items():
        if harness.ticks(pid) != start:
            continue
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if waited == pid:
            reaped.append(pid)
    return sorted(reaped)


def _scenario_evidence(item: Item, index: int, namespace: Path,
                       report_dir: Path) -> list[dict[str, Any]]:
    """Check and retain fresh legacy assertions, in addition to control/ownership evidence."""
    evidence: list[dict[str, Any]] = []
    for scenario, required_steps in item.scenario_steps.items():
        row: dict[str, Any] = {"scenario": scenario, "requiredSteps": list(required_steps),
                               "status": "inconclusive", "steps": {}, "reason": None}
        path = namespace / "reports" / item.key / f"{scenario}.json"
        try:
            if not path.resolve().is_relative_to(namespace.resolve()) or path.is_symlink():
                raise ValueError("legacy report escaped the owned namespace")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("legacy report is not a regular file")
                raw = stream.read(CONTROL_BYTES + 1)
            if len(raw) > CONTROL_BYTES:
                raise ValueError("legacy report exceeds 1 MiB")
            data = json.loads(raw)
            target = report_dir / f"{index:02d}-{item.key}-{scenario}.json"
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
            row.update(reportPath=str(target), sha256=sha256(raw).hexdigest())
            if not isinstance(data, dict) or data.get("run_id") != item.key or data.get("scenario") != scenario:
                raise ValueError("legacy report scenario/invocation identity mismatch")
            steps = data.get("steps")
            if not isinstance(steps, dict) or any(not isinstance(step, dict) for step in steps.values()):
                raise ValueError("legacy report has invalid steps")
            row["steps"] = steps
            missing = [name for name in required_steps if name not in steps]
            failed = [name for name, step in steps.items() if step.get("status") == harness.FAIL]
            if failed:
                row.update(status="failed", reason=f"legacy assertion failure: {failed}")
            unknown = [name for name, step in steps.items()
                       if step.get("status") != harness.PASS
                       and not (step.get("status") == harness.NA and (scenario, name) in item.allowed_na)
                       and name not in failed]
            cleanups = data.get("cleanups", [data.get("cleanup")])
            if not isinstance(cleanups, list) or not cleanups or any(not isinstance(c, dict) for c in cleanups):
                raise ValueError("legacy report is missing cleanup evidence")
            row["cleanups"] = cleanups
            dirty_cleanup = any(c.get("residue_before_fallback") or c.get("residue_after_fallback")
                                or c.get("cleanup_errors") or c.get("shutdown_error")
                                or c.get("root_removed") is not True
                                or c.get("shutdown_exit", 0) != 0 for c in cleanups)
            row["cleanupFailed"] = dirty_cleanup
            provider = data.get("provider", {})  # SEL reports do not contain provider snapshots.
            if not isinstance(provider, dict):
                raise ValueError("legacy report has invalid provider evidence")
            if failed or dirty_cleanup:
                row.update(status="failed", reason=f"legacy assertion/cleanup failure: {failed}")
            elif missing or unknown or data.get("unknowns") or provider.get("errors"):
                row.update(reason=f"legacy missing/not_run/unknown/provider error: {missing + unknown}")
            elif data.get("result") != harness.PASS:
                row.update(reason="legacy scenario result is not pass")
            else:
                row["status"] = "passed"
        except (OSError, ValueError, TypeError) as exc:
            row["reason"] = f"legacy report unavailable/invalid: {type(exc).__name__}: {exc}"
        evidence.append(row)
    return evidence


def _run_item_scoped(item: Item, index: int, report_dir: Path, omp: Path,
                     invocation_id: str | None = None,
                     baseline_children: dict[int, int] | None = None) -> dict[str, Any]:
    """Install a cleanup guard before spawn, including post-Popen setup failures/signals."""
    lifetime: dict[str, Any] = {"mask": signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM}),
                               "maskRestored": False, "cleanupDone": False, "returned": False}
    try:
        row = _run_item_body(item, index, report_dir, omp, invocation_id, baseline_children, lifetime)
        lifetime["returned"] = True
        return row
    finally:
        proc = lifetime.get("proc")
        namespace = lifetime.get("namespace")
        if proc is not None and not lifetime["cleanupDone"]:
            owned = lifetime["control"].owned
            stop = _stop_owned(proc, lifetime.get("start"), namespace, owned,
                               baseline_children=baseline_children,
                               adopted_seen=lifetime.get("adopted", {}))
            try:
                proc.wait(timeout=CLEANUP_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            _reap_adopted({**owned, **lifetime.get("adopted", {})})
            selector = lifetime.get("selector")
            if selector is not None:
                selector.close()
            for fd in lifetime.get("fds", ()):
                os.close(fd)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        if not lifetime["returned"] and namespace is not None and not harness.processes_mentioning(str(namespace)):
            shutil.rmtree(namespace, ignore_errors=True)
        if not lifetime["maskRestored"]:
            signal.pthread_sigmask(signal.SIG_SETMASK, lifetime["mask"])


def _run_item_body(item: Item, index: int, report_dir: Path, omp: Path,
                   invocation_id: str | None, baseline_children: dict[int, int] | None,
                   lifetime: dict[str, Any]) -> dict[str, Any]:
    # AF_UNIX paths in the installed Workbench must stay short even when report-dir is deep.
    namespace = Path(tempfile.mkdtemp(prefix=f"dvl01-{index:02d}-", dir="/tmp"))
    lifetime["namespace"] = namespace
    _cell_observe("namespace", path=str(namespace))
    started = time.monotonic()
    invocation = invocation_id or str(uuid4())
    nonce = str(uuid4())
    control = _ControlState(invocation, item.key, nonce)
    lifetime["control"] = control
    argv: list[str] = []
    control_read = control_write = None
    out, err = bytearray(), bytearray()
    dropped_out = dropped_err = 0
    timed_out = False
    stop: dict[str, Any] = {}
    try:
        os.chmod(namespace, 0o700)
        env = _base_env(namespace / "home", namespace, omp, item)
        control_read, control_write = os.pipe()
        argv = [sys.executable, "-m", "tools.dev_validate", "--_driver", invocation, item.key,
                nonce, str(control_write), *item.modules]
        proc = subprocess.Popen(argv, cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                pass_fds=(control_write,), start_new_session=True)
        lifetime["proc"] = proc
        fields = harness.stat_fields(proc.pid)
        lifetime["start"] = int(fields[19]) if fields is not None else None
    except (OSError, ValueError, AssertionError) as exc:
        for fd in (control_read, control_write):
            if fd is not None:
                os.close(fd)
        # No Popen handle/identity was returned. Namespace mention alone is not
        # proof that a process is ours, so never signal such a process.
        unknown_occupants = harness.processes_mentioning(str(namespace))
        if not unknown_occupants:
            shutil.rmtree(namespace)
        return {"id": item.key, "assertionIds": list(item.assertion_ids), "required": True,
                "evidenceLevel": item.evidence_level, "status": "inconclusive",
                "reason": f"item spawn failed: {type(exc).__name__}",
                "failureClass": "probe_environment",
                "command": argv, "cwd": str(REPO), "exitCode": None, "collectedTests": 0,
                "skippedTests": 0, "durationSeconds": round(time.monotonic() - started, 3),
                "expectedWaitSeconds": item.expected_wait_seconds,
                "watchdogSeconds": SCENARIO_IDLE_SECONDS + item.expected_wait_seconds,
                "progressPredicate": "structured unittest test completion",
                "progressEvents": 0, "stdoutTail": "", "stderrTail": "",
                "stdoutDroppedBytes": 0, "stderrDroppedBytes": 0,
                "cleanup": {"remaining": [], "unknownNamespaceUsers": sorted(unknown_occupants),
                            "namespaceRemoved": not namespace.exists()},
                "namespace": str(namespace)}
    assert control_read is not None and control_write is not None
    os.close(control_write)
    lifetime["fds"] = (control_read,)
    ticks = harness.ticks(proc.pid)
    control.bind_root(proc.pid, ticks)
    if ticks is not None:
        _cell_observe("owned", pid=proc.pid, startTicks=ticks)
    adopted_seen: dict[int, int] = {}
    lifetime["adopted"] = adopted_seen
    adopted_zombies: set[int] = set()

    def owned_now() -> dict[int, int]:
        members = _owned_members(proc, namespace, control.owned,
                              baseline_children=baseline_children,
                              adopted_seen=adopted_seen, adopted_zombies=adopted_zombies,
                              driver_start=ticks)
        for pid, start in {**control.owned, **adopted_seen, **members}.items():
            _cell_observe("owned", pid=pid, startTicks=start)
        return members

    def stop_now() -> dict[str, Any]:
        return _stop_owned(proc, ticks, namespace, control.owned,
                           baseline_children=baseline_children, adopted_seen=adopted_seen,
                           adopted_zombies=adopted_zombies)

    selector = selectors.DefaultSelector()
    lifetime["selector"] = selector
    interrupted = False
    observed_completed = 0
    observed_suite = False
    try:
        lifetime["maskRestored"] = True
        signal.pthread_sigmask(signal.SIG_SETMASK, lifetime["mask"])
        for pipe, label in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            assert pipe is not None
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, label)
        os.set_blocking(control_read, False)
        selector.register(control_read, selectors.EVENT_READ, "control")
        last_progress = started
        while selector.get_map() or proc.poll() is None:
            events = selector.select(0.2)
            for key, _ in events:
                descriptor = key.fileobj if type(key.fileobj) is int else key.fileobj.fileno()
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    dropped_out += _append_bounded(out, chunk)
                elif key.data == "stderr":
                    dropped_err += _append_bounded(err, chunk)
                else:
                    if control.feed(chunk):
                        last_progress = time.monotonic()
                    for _ in range(control.completed - observed_completed):
                        _cell_observe("completed")
                    observed_completed = control.completed
                    if control.suite is not None and not observed_suite:
                        _cell_observe("suite", **control.suite)
                        observed_suite = True
                    owned_now()
            if time.monotonic() - last_progress > SCENARIO_IDLE_SECONDS + item.expected_wait_seconds:
                timed_out = True
                stop = stop_now()
                break
        if proc.poll() is None:
            stop = stop_now()
        try:
            exit_code = proc.wait(timeout=CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            exit_code = None
    except BaseException:
        interrupted = True
        raise
    finally:
        if interrupted or proc.poll() is None:
            stop = stop_now()
        try:
            proc.wait(timeout=CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        owned_now()  # Include a backend that became adopted Z before the first cleanup scan.
        stop["reapedPids"] = _reap_adopted({**control.owned, **adopted_seen})
        close_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            selector.close()
            lifetime["selector"] = None
            os.close(control_read)
            lifetime["fds"] = ()
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, close_mask)
    elapsed = round(time.monotonic() - started, 3)
    transcript = err.decode("utf-8", "replace")
    collected = control.suite["testsRun"] if control.valid() else 0
    skipped = control.suite["skipped"] if control.valid() else 0
    remaining_before = owned_now()
    if remaining_before:
        cleanup = stop_now()
        stop = {**stop, "signalledPids": sorted(set(stop.get("signalledPids", []))
                                               | set(cleanup["signalledPids"])),
                "remaining": cleanup["remaining"]}
    owned_now()  # A final exact scan also covers a late normal child exit.
    stop["reapedPids"] = sorted(set(stop.get("reapedPids", []))
                               | set(_reap_adopted({**control.owned, **adopted_seen})))
    remaining = owned_now()
    # The last membership read itself can discover an already-exited adopted Z.
    stop["reapedPids"] = sorted(set(stop["reapedPids"])
                               | set(_reap_adopted({**control.owned, **adopted_seen})))
    remaining = owned_now()
    unattributed = harness.processes_mentioning(str(namespace))
    unattributed = {pid: start for pid, start in unattributed.items() if pid not in control.owned
                    and pid not in adopted_seen and pid not in remaining}
    if remaining or exit_code is None:
        status, reason, failure_class = "failed", "owned child process survived the item", "cleanup"
    elif unattributed:
        status, reason, failure_class = "inconclusive", "namespace process without owned identity", "cleanup"
    elif timed_out:
        status, reason, failure_class = "inconclusive", "scenario watchdog: no observed output/progress", "watchdog"
    elif not control.valid():
        status, reason, failure_class = "inconclusive", control.error or "missing structured suite result", "collection"
    elif collected == 0 or skipped == collected:
        status, reason, failure_class = "inconclusive", "empty or fully skipped collection", "collection"
    elif exit_code == 0 and not any(control.suite[name] for name in ("failures", "errors", "unexpectedSuccesses")):
        status, reason, failure_class = "passed", None, None
    elif any(marker in transcript for marker in ("DVL_PROVIDER_ERROR", "data dir error:", "timed out waiting")):
        status, reason, failure_class = "inconclusive", "provider, environment or readiness probe error", "probe_environment"
    elif exit_code == 1 and any(control.suite[name] for name in ("failures", "errors", "unexpectedSuccesses")):
        status, reason, failure_class = "failed", "unittest assertion/error", "product_assertion"
    else:
        status, reason, failure_class = "inconclusive", "probe/provider/environment error or unclassified exit", "probe_environment"
    scenario_evidence = _scenario_evidence(item, index, namespace, report_dir)
    if item.scenario_steps:
        failed_scenarios = [row for row in scenario_evidence if row["status"] == "failed"]
        unknown_scenarios = [row for row in scenario_evidence if row["status"] != "passed"]
        if failed_scenarios:
            status, reason, failure_class = "failed", failed_scenarios[0]["reason"], "legacy_assertion_cleanup"
        elif status == "passed" and (unknown_scenarios or skipped):
            status, reason, failure_class = ("inconclusive", "required legacy evidence missing/not_run/skipped",
                                           "legacy_evidence")
        elif (item.outer and failure_class == "product_assertion" and unknown_scenarios
              and all(row["steps"] and all(step.get("status") == harness.NOT_RUN
                                          for step in row["steps"].values()) for row in unknown_scenarios)):
            # Legacy unittest reports a mismatch for an uninstalled/unisolatable
            # outer's not_run steps. Preserve those steps as required unknown.
            status, reason, failure_class = "inconclusive", unknown_scenarios[0]["reason"], "legacy_evidence"
        if (item.outer in ("tmux", "herdr") and failure_class == "legacy_assertion_cleanup"
                and not remaining and not unattributed and exit_code is not None
                and not any(row.get("cleanupFailed") for row in scenario_evidence)):
            c1 = next(iter(scenario_evidence), {}).get("steps", {}).get("C1", {})
            assertion = c1.get("assertion", "")
            if isinstance(assertion, str) and assertion.startswith(("herdr isolation preflight failed:",
                                                                    "isolated tmux did not start:")):
                # Keep the original failed assertion in evidence while reporting
                # unavailable isolation as required unknown, never a product pass.
                status, reason, failure_class = "inconclusive", assertion, "probe_environment"
    if not remaining and not unattributed:
        shutil.rmtree(namespace)
    namespace_removed = not namespace.exists()
    lifetime["cleanupDone"] = True
    return {"id": item.key, "assertionIds": list(item.assertion_ids), "required": True,
            "evidenceLevel": item.evidence_level, "status": status, "reason": reason,
            "failureClass": failure_class,
            "command": argv, "cwd": str(REPO), "exitCode": exit_code, "collectedTests": collected,
            "skippedTests": skipped, "durationSeconds": elapsed,
            "expectedWaitSeconds": item.expected_wait_seconds,
            "watchdogSeconds": SCENARIO_IDLE_SECONDS + item.expected_wait_seconds,
            "progressPredicate": "structured unittest test completion",
            "progressEvents": control.completed,
            "stdoutTail": out.decode("utf-8", "replace"), "stderrTail": transcript,
            "stdoutDroppedBytes": dropped_out, "stderrDroppedBytes": dropped_err,
            "cleanup": {"remaining": sorted(remaining), **stop, "adoptedPids": sorted(adopted_seen),
                        "adoptedZombiePids": sorted(adopted_zombies),
                        "unknownNamespaceUsers": sorted(unattributed),
                        "namespaceRemoved": namespace_removed}, "namespace": str(namespace),
            "testIds": list(item.modules), "scenarioEvidence": scenario_evidence}


def _run_item(item: Item, index: int, report_dir: Path, omp: Path,
              invocation_id: str | None = None) -> dict[str, Any]:
    """One Linux-owned cell, restoring this process's prior child-adoption mode."""
    if item.outer in ("tmux", "herdr") and not harness.find_tool(item.outer):
        shell_label = "sh(dash)" if item.env.get("WB_CW16_SHELLS", "bash") == "dash" else "Bash"
        return {"id": item.key, "assertionIds": list(item.assertion_ids), "required": True,
                "evidenceLevel": item.evidence_level, "status": "not_run",
                "reason": f"required {item.outer}/{shell_label} cell: outer is not installed",
                "failureClass": "probe_environment", "testIds": list(item.modules), "command": [],
                "collectedTests": 0, "skippedTests": 0, "durationSeconds": 0,
                "expectedWaitSeconds": item.expected_wait_seconds,
                "watchdogSeconds": SCENARIO_IDLE_SECONDS + item.expected_wait_seconds,
                "scenarioEvidence": [], "cleanup": {"remaining": [], "namespaceRemoved": None},
                "namespace": None}
    try:
        baseline_children = _direct_children(os.getpid())
        with _owned_subreaper():
            return _run_item_scoped(item, index, report_dir, omp, invocation_id,
                                    baseline_children)
    except OSError as exc:
        return {"id": item.key, "assertionIds": list(item.assertion_ids), "required": True,
                "evidenceLevel": item.evidence_level, "status": "inconclusive",
                "reason": "runner ownership setup/restore failed: " + type(exc).__name__,
                "failureClass": "probe_environment", "command": [], "cwd": str(REPO),
                "exitCode": None, "collectedTests": 0, "skippedTests": 0,
                "durationSeconds": 0, "expectedWaitSeconds": item.expected_wait_seconds,
                "watchdogSeconds": SCENARIO_IDLE_SECONDS + item.expected_wait_seconds,
                "progressPredicate": "structured unittest test completion", "progressEvents": 0,
                "stdoutTail": "", "stderrTail": "", "stdoutDroppedBytes": 0,
                "stderrDroppedBytes": 0, "cleanup": {"remaining": [], "namespaceRemoved": None},
                "namespace": None}


def _unknown_item(item: Item, reason: str, *, status: str = "inconclusive") -> dict[str, Any]:
    return {"id": item.key, "assertionIds": list(item.assertion_ids), "testIds": list(item.modules),
            "required": True, "evidenceLevel": item.evidence_level, "status": status, "reason": reason,
            "failureClass": "cell_worker", "collectedTests": 0, "skippedTests": 0,
            "durationSeconds": 0, "expectedWaitSeconds": item.expected_wait_seconds,
            "cleanup": {"remaining": [], "namespaceRemoved": None}, "scenarioEvidence": []}


def _cell_worker_main(item: Item, index: int, invocation: str, nonce: str,
                      parent_pid: int, parent_start: int, control_fd: int, result_fd: int,
                      namespace: Path, report_dir: Path, omp: Path) -> int:
    """One single-cell subreaper in a fresh interpreter; parent death uses its normal finally."""
    global _CELL_OBSERVER
    sequence = 0
    seen: set[tuple[int, int]] = set()
    item_namespace: Path | None = None
    worker_start = harness.ticks(os.getpid())

    def observe(event: str, **fields: Any) -> None:
        nonlocal sequence, item_namespace
        if event == "namespace":
            item_namespace = Path(fields["path"])
            return
        if event == "owned":
            identity = (fields["pid"], fields["startTicks"])
            if identity in seen:
                return
            seen.add(identity)
            fields.update(emitterPid=os.getpid(), emitterTicks=worker_start, origin="popen")
        else:
            sequence += 1
        frame = {"version": 1, "invocation": invocation, "item": item.key, "nonce": nonce,
                 "sequence": 0 if event == "owned" else sequence, "event": event, **fields}
        try:
            os.write(control_fd, json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        except OSError:
            pass  # Parent may already be dead; this must never interrupt owned cleanup.

    _CELL_OBSERVER = observe
    try:
        with _interrupt_signals():
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), "cannot set cell parent-death signal")
            # Close the fork/exec -> prctl race before any scenario can start.
            if os.getppid() != parent_pid or harness.ticks(parent_pid) != parent_start:
                raise _Interrupted("cell parent identity changed before start")
            try:
                result = _run_item(item, index, report_dir, omp, invocation)
            except _Interrupted as exc:
                result = _unknown_item(item, "cell interrupted: " + str(exc))
                residue = _direct_children(os.getpid())
                unknown = harness.processes_mentioning(str(item_namespace)) if item_namespace else {}
                if item_namespace and not residue and not unknown:
                    shutil.rmtree(item_namespace, ignore_errors=True)
                result["cleanup"] = {"remaining": sorted(residue), "unknownNamespaceUsers": sorted(unknown),
                                     "namespaceRemoved": item_namespace is None or not item_namespace.exists()}
                if residue:
                    result.update(status="failed", failureClass="cleanup")
    except (_Interrupted, OSError) as exc:
        result = _unknown_item(item, "cell setup/interruption: " + str(exc))
    finally:
        _CELL_OBSERVER = None
    envelope = {"invocation": invocation, "item": item.key, "nonce": nonce,
                "workerPid": os.getpid(), "workerStartTicks": worker_start, "result": result}
    try:
        payload = json.dumps(envelope, ensure_ascii=False).encode()
        if len(payload) > CELL_RESULT_BYTES:
            raise ValueError("cell result exceeded bound")
        while payload:
            sent = os.write(result_fd, payload)
            payload = payload[sent:]
    except (OSError, ValueError):
        return 2
    finally:
        os.close(control_fd)
        os.close(result_fd)
    return 0


def _worker_argv(item: Item, index: int, invocation: str, nonce: str, parent_pid: int,
                 parent_start: int, control_fd: int, result_fd: int, namespace: Path,
                 report_dir: Path, omp: Path) -> list[str]:
    return [sys.executable, "-B", "-m", "tools.dev_validate", "--_cell-worker", item.key, str(index),
            invocation, nonce, str(parent_pid), str(parent_start), str(control_fd), str(result_fd),
            str(namespace), str(report_dir), str(omp)]


def _run_cell_batch(batch: list[tuple[int, Item]], report_dir: Path, omp: Path,
                    invocation: str) -> tuple[list[dict[str, Any]], bool]:
    """At most two cells. Only this dispatcher creates direct children in this subreaper window."""
    workers: list[dict[str, Any]] = []
    results: dict[int, dict[str, Any]] = {}
    selector = selectors.DefaultSelector()
    interrupted = False
    baseline = _direct_children(os.getpid())
    adopted: dict[int, int] = {}
    unknown_adoptions: dict[int, int] = {}
    owned_sessions: dict[int, int] = {}
    observed_owned: dict[int, int] = {}
    signalled: set[int] = set()

    def members() -> dict[int, int]:
        # Adoption/freshness alone grants nothing: an unrelated ancestor can
        # also exit during this subreaper window. Retain receipt-backed SID proof
        # while its exact leader is observable, including after that leader exits.
        receipts: dict[int, int] = {}
        for worker in workers:
            receipts.update(worker["control"].owned)
            if worker["start"] is not None:
                receipts[worker["proc"].pid] = worker["start"]
        for pid, start in receipts.items():
            fields = harness.stat_fields(pid)
            if fields is not None and int(fields[19]) == start and int(fields[3]) == pid:
                owned_sessions[pid] = start
        owned = {**observed_owned, **receipts, **harness.session_members(owned_sessions)}
        for pid, start in _direct_children(os.getpid()).items():
            if baseline.get(pid) == start:
                continue
            fields = harness.stat_fields(pid)
            if fields is None or int(fields[19]) != start:
                continue
            sid = int(fields[3])
            if sid in receipts:
                owned_sessions[sid] = receipts[sid]  # Actual member SID corroborates a receipt-backed leader.
            if owned.get(pid) == start or sid in owned_sessions:
                adopted[pid] = start
                owned[pid] = start
                unknown_adoptions.pop(pid, None)
            else:
                unknown_adoptions[pid] = start
        observed_owned.update(owned)
        return {pid: start for pid, start in owned.items() if start is not None and harness.alive(pid, start)}

    with _owned_subreaper():
        try:
            for index, item in batch:
                previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
                namespace = Path(tempfile.mkdtemp(prefix=f"dvl02-worker-{index:02d}-", dir="/tmp"))
                os.chmod(namespace, 0o700)
                control_read, control_write = os.pipe()
                result_read, result_write = os.pipe()
                nonce = str(uuid4())
                sentinel = namespace / "sentinel"
                sentinel.write_bytes(nonce.encode())
                sentinel_before = sha256(sentinel.read_bytes()).hexdigest()
                try:
                    env = _base_env(namespace / "home", namespace, omp, item)
                    argv = _worker_argv(item, index, invocation, nonce, os.getpid(), harness.ticks(os.getpid()),
                                        control_write, result_write, namespace, report_dir, omp)
                    proc = subprocess.Popen(argv, cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            pass_fds=(control_write, result_write), start_new_session=True)
                except (OSError, ValueError, AssertionError) as exc:
                    for fd in (control_read, control_write, result_read, result_write):
                        os.close(fd)
                    results[index] = _unknown_item(item, "cell worker spawn failed: " + type(exc).__name__)
                    shutil.rmtree(namespace)
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                    continue
                os.close(control_write)
                os.close(result_write)
                start = harness.ticks(proc.pid)
                control = _ControlState(invocation, item.key, nonce)
                control.bind_root(proc.pid, start)
                worker = {"index": index, "item": item, "namespace": namespace, "proc": proc,
                          "start": start, "control": control, "nonce": nonce, "argv": argv,
                          "result": bytearray(), "stdout": bytearray(), "stderr": bytearray(),
                          "error": None, "fds": [], "lastProgress": time.monotonic(),
                          "started": time.monotonic(), "finished": None,
                          "stopRequested": None, "sentinelBefore": sentinel_before}
                workers.append(worker)
                for fd, label in ((control_read, "control"), (result_read, "result"),
                                  (proc.stdout.fileno(), "stdout"), (proc.stderr.fileno(), "stderr")):
                    os.set_blocking(fd, False)
                    worker["fds"].append(fd)
                    selector.register(fd, selectors.EVENT_READ, (worker, label))
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            while selector.get_map() or any(w["proc"].poll() is None for w in workers):
                members()
                for key, _ in selector.select(0.1):
                    worker, label = key.data
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fd)
                    elif label == "control":
                        if worker["control"].feed(chunk):
                            worker["lastProgress"] = time.monotonic()
                    elif label == "result":
                        if len(worker["result"]) + len(chunk) > CELL_RESULT_BYTES:
                            worker["error"] = "cell result exceeded bound"
                        else:
                            worker["result"].extend(chunk)
                    else:
                        _append_bounded(worker[label], chunk)
                for worker in workers:
                    if worker["proc"].poll() is not None and worker["finished"] is None:
                        worker["finished"] = time.monotonic()
                    allowance = SCENARIO_IDLE_SECONDS + worker["item"].expected_wait_seconds + 3 * CLEANUP_SECONDS
                    if worker["proc"].poll() is None and time.monotonic() - worker["lastProgress"] > allowance:
                        worker["error"] = "cell supervisor watchdog after inner cleanup allowance"
                        harness.kill_exact(worker["proc"].pid, worker["start"], signal.SIGTERM)
                        worker["stopRequested"] = worker["stopRequested"] or time.monotonic()
                        # A stopped cell never prevents the other selected cell from finishing.
                        worker["lastProgress"] = time.monotonic()
                    if (worker["proc"].poll() is None and worker["stopRequested"] is not None
                            and time.monotonic() - worker["stopRequested"] > 3 * CLEANUP_SECONDS):
                        harness.kill_exact(worker["proc"].pid, worker["start"])
        except _Interrupted:
            interrupted = True
        finally:
            for worker in workers:
                if worker["proc"].poll() is None and harness.kill_exact(worker["proc"].pid, worker["start"], signal.SIGTERM):
                    signalled.add(worker["proc"].pid)
            deadline = time.monotonic() + 3 * CLEANUP_SECONDS
            while any(w["proc"].poll() is None for w in workers) and time.monotonic() < deadline:
                # Keep draining ownership receipts during cooperative signal cleanup.
                for key, _ in selector.select(0.05):
                    worker, label = key.data
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fd)
                    elif label == "control":
                        worker["control"].feed(chunk)
                    elif label == "result" and len(worker["result"]) + len(chunk) <= CELL_RESULT_BYTES:
                        worker["result"].extend(chunk)
                members()
            for pid, start in members().items():
                if harness.kill_exact(pid, start):
                    signalled.add(pid)
            for worker in workers:
                try:
                    worker["proc"].wait(timeout=CLEANUP_SECONDS)
                except subprocess.TimeoutExpired:
                    worker["error"] = "cell worker survived cleanup"
            members()
            reaped = _reap_adopted(adopted)
            remaining = members()
            for worker in workers:
                item = worker["item"]
                try:
                    envelope = json.loads(worker["result"])
                    expected = {"invocation": invocation, "item": item.key, "nonce": worker["nonce"],
                                "workerPid": worker["proc"].pid, "workerStartTicks": worker["start"]}
                    if any(envelope.get(key) != value for key, value in expected.items()):
                        raise ValueError("cell result identity mismatch")
                    row = envelope["result"]
                    if not isinstance(row, dict) or row.get("id") != item.key:
                        raise ValueError("cell result item mismatch")
                    if row.get("status") == "passed" and not worker["control"].valid():
                        raise ValueError("missing trusted cell suite/ownership receipts")
                    if worker["proc"].returncode != 0 or worker["error"]:
                        raise ValueError(worker["error"] or "cell worker exit error")
                except (ValueError, TypeError, KeyError) as exc:
                    row = _unknown_item(item, "cell result unavailable/invalid: " + str(exc))
                sentinel = worker["namespace"] / "sentinel"
                try:
                    with os.fdopen(os.open(sentinel, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise ValueError("sentinel not regular")
                        sentinel_bytes = stream.read(1025)
                    after_sha = sha256(sentinel_bytes).hexdigest() if len(sentinel_bytes) <= 1024 else None
                except (OSError, ValueError):
                    after_sha = None
                worker_meta = {"pid": worker["proc"].pid, "startTicks": worker["start"],
                               "namespace": str(worker["namespace"]), "home": str(worker["namespace"] / "home"),
                               "startedMonotonic": worker["started"],
                               "finishedMonotonic": worker["finished"] or time.monotonic(),
                               "command": worker["argv"], "exitCode": worker["proc"].returncode,
                               "sentinelBeforeSha256": worker["sentinelBefore"], "sentinelAfterSha256": after_sha,
                               "ownedIdentities": worker["control"].owned,
                               "ownedSessions": owned_sessions, "unknownAdoptions": unknown_adoptions,
                               "remaining": sorted(remaining), "signalledPids": sorted(signalled),
                               "reapedPids": reaped, "interrupted": interrupted}
                if row.get("status") == "failed":
                    pass  # Preserve the confirmed failure and its original reason across cleanup/interruption.
                elif remaining or (signalled and not interrupted) or after_sha != worker["sentinelBefore"]:
                    row.update(status="failed", reason="cell parent cleanup/sentinel failure", failureClass="cleanup")
                elif interrupted:
                    row.update(status="inconclusive", reason="runner interrupted; cell cleanup completed")
                if not remaining:
                    shutil.rmtree(worker["namespace"])
                worker_meta["namespaceRemoved"] = not worker["namespace"].exists()
                row["cellWorker"] = worker_meta
                results[worker["index"]] = row
                for fd in worker["fds"][:2]:
                    os.close(fd)
                worker["proc"].stdout.close()
                worker["proc"].stderr.close()
            selector.close()
    return [results.get(index, _unknown_item(item, "runner interrupted before cell registration", status="not_run"))
            for index, item in batch], interrupted


def _parallel_items(items: tuple[Item, ...], report_dir: Path, omp: Path,
                    invocation: str, offset: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = 0
    with _interrupt_signals():
        while cursor < len(items):
            item = items[cursor]
            count = 2 if (item.outer == "tmux" and cursor + 1 < len(items)
                          and items[cursor + 1].outer == "tmux") else 1
            batch = [(offset + index, items[index]) for index in range(cursor, cursor + count)]
            completed, interrupted = _run_cell_batch(batch, report_dir, omp, invocation)
            rows.extend(completed)
            cursor += count
            if interrupted:
                rows.extend(_unknown_item(item, "runner interrupted before cell start", status="not_run")
                            for item in items[cursor:])
                break
    return rows


def _item_identity(item: Item) -> dict[str, Any]:
    return {"id": item.key, "modules": list(item.modules), "testIds": list(item.modules),
            "assertionIds": list(item.assertion_ids), "required": True,
            "evidenceLevel": item.evidence_level, "expectedWaitSeconds": item.expected_wait_seconds,
            "env": item.env, "requiredSteps": {name: list(steps) for name, steps in item.scenario_steps.items()},
            "allowedNa": [list(pair) for pair in item.allowed_na], "outer": item.outer}


def _cost_identity(items: tuple[Item, ...], omp: Path) -> dict[str, Any]:
    # Only concrete private namespace names and requested jobs are excluded.
    # Actual tool/interpreter paths and every item/env/assertion definition remain.
    return {"selectedItems": [_item_identity(item) for item in items],
            "environmentTemplate": {"builder": "harness.clean_base_env + _base_env/v1",
                "HOME": "<namespace>/home", "XDG": "<namespace>/home/{config,data,state,cache,runtime}",
                "TMPDIR": "<namespace>", "reportDir": "<namespace>/reports", "runId": "<item.id>",
                "PATH": f"/usr/bin:/bin:{omp.parent}", "PYTHONPATH": str(REPO / "src"),
                "pythonExecutable": sys.executable, "ompPath": str(omp),
                "tmuxPath": harness.find_tool("tmux"), "herdrPath": harness.find_tool("herdr"),
                "proxy": harness.BLOCKED_PROXY, "proxyKeys": list(harness.PROXY_KEYS),
                "userAuthInherited": False, "provider": "scripted loopback only",
                "readinessSeconds": READINESS_SECONDS, "idleSeconds": SCENARIO_IDLE_SECONDS,
                "cleanupSeconds": CLEANUP_SECONDS},
            "measurement": "wall before input snapshot through after tool versions; comparison/serialization excluded"}


def _scope_cost(report: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    identity = report.get("costIdentity")
    if not isinstance(identity, dict) or not isinstance(identity.get("selectedItems"), list):
        return None, ["canonical executable scope missing"]
    definitions = identity["selectedItems"]
    if not definitions or any(not isinstance(item, dict) for item in definitions):
        return None, ["canonical executable scope invalid"]
    keys = [item.get("id") for item in definitions]
    if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
        return None, ["canonical executable scope duplicate/invalid IDs"]
    rows = report.get("items")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None, ["full item evidence missing"]
    if any(not isinstance(row.get("id"), str) for row in rows):
        return None, ["invalid item evidence ID"]
    by_key = {row.get("id"): row for row in rows}
    if len(by_key) != len(rows):
        reasons.append("duplicate item evidence")
    costs: dict[str, float] = {}
    for definition in definitions:
        key = definition["id"]
        row = by_key.get(key, {})
        count, seconds = row.get("collectedTests"), row.get("durationSeconds")
        if row.get("status") != "passed":
            reasons.append(f"{key}: executable item is not passed")
        if type(count) is not int or count <= 0 or row.get("skippedTests") != 0:
            reasons.append(f"{key}: positive complete collection missing")
        if (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0):
            reasons.append(f"{key}: measured item duration invalid")
        else:
            costs[key] = seconds
        for field in ("testIds", "assertionIds", "required", "evidenceLevel", "expectedWaitSeconds"):
            if row.get(field) != definition.get(field):
                reasons.append(f"{key}: {field} evidence differs from selected definition")
        cleanup = row.get("cleanup")
        if (not isinstance(cleanup, dict) or cleanup.get("remaining") != []
                or cleanup.get("namespaceRemoved") is not True or cleanup.get("unknownNamespaceUsers")):
            reasons.append(f"{key}: successful owned cleanup missing")
        worker = row.get("cellWorker")
        digests = (worker.get("sentinelBeforeSha256"), worker.get("sentinelAfterSha256")) if isinstance(worker, dict) else ()
        valid_digests = len(digests) == 2 and all(isinstance(value, str) and len(value) == 64
                                                and all(char in "0123456789abcdef" for char in value)
                                                for value in digests)
        if worker is not None and (not isinstance(worker, dict) or worker.get("remaining") != []
                                  or worker.get("namespaceRemoved") is not True
                                  or not valid_digests
                                  or worker.get("sentinelBeforeSha256") != worker.get("sentinelAfterSha256")):
            reasons.append(f"{key}: cell-worker cleanup/sentinel failed")
        required = definition.get("requiredSteps", {})
        if not isinstance(required, dict) or any(not isinstance(key, str) or not isinstance(value, list)
                                                for key, value in required.items()):
            reasons.append(f"{key}: invalid required step definition")
            continue
        evidence = row.get("scenarioEvidence", [])
        if not isinstance(evidence, list) or any(not isinstance(value, dict) for value in evidence):
            reasons.append(f"{key}: full scenario evidence missing")
            continue
        if any(not isinstance(value.get("scenario"), str) for value in evidence):
            reasons.append(f"{key}: invalid scenario identity")
            continue
        observed = {value.get("scenario"): value for value in evidence}
        if len(observed) != len(evidence) or set(observed) != set(required):
            reasons.append(f"{key}: required scenario inventory differs")
        for scenario, steps in required.items():
            value = observed.get(scenario, {})
            if value.get("status") != "passed" or value.get("requiredSteps") != steps:
                reasons.append(f"{key}/{scenario}: required steps not passed")
            path, digest = value.get("reportPath"), value.get("sha256")
            try:
                if not isinstance(path, str) or not isinstance(digest, str):
                    raise ValueError("raw report reference missing")
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("raw report is not regular")
                    raw = stream.read(CONTROL_BYTES + 1)
                if len(raw) > CONTROL_BYTES or sha256(raw).hexdigest() != digest:
                    raise ValueError("raw report changed or exceeded bound")
            except (OSError, ValueError) as exc:
                reasons.append(f"{key}/{scenario}: retained raw evidence unavailable: {type(exc).__name__}")
    wall = report.get("elapsedSeconds")
    if type(wall) not in (int, float) or not math.isfinite(wall) or wall < 0:
        reasons.append("measured wall duration invalid")
    if report.get("result") == "failed" or any(row.get("status") == "failed" for row in rows):
        reasons.append("report contains a confirmed failure")
    if any(row.get("required") and row.get("status") != "passed" and row.get("id") not in PRECISE_PENDING
           for row in rows):
        reasons.append("report contains required unknown outside future pending scope")
    failure = report.get("firstFailure")
    if failure and (not isinstance(failure, dict) or failure.get("status") != "not_run"
                    or failure.get("item") not in PRECISE_PENDING):
        reasons.append("original failed/unknown attempt is still unresolved")
    return ({"wallSeconds": wall, "itemSumSeconds": round(sum(costs.values()), 3), "items": costs}
            if not reasons else None), reasons


def _compare_cost(report: dict[str, Any], baseline_path: Path | None) -> dict[str, Any]:
    if baseline_path is None:
        return {"status": "not_requested", "reasons": []}
    result: dict[str, Any] = {"status": "unknown", "baselineReport": str(baseline_path.absolute()), "reasons": []}
    try:
        fd = os.open(baseline_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("baseline report must be regular")
            raw = stream.read(BASELINE_REPORT_BYTES + 1)
        if len(raw) > BASELINE_REPORT_BYTES:
            raise ValueError("baseline report exceeded bound")
        baseline = json.loads(raw)
        if (not isinstance(baseline, dict) or type(baseline.get("schemaVersion")) is not int
                or baseline.get("schemaVersion") != SCHEMA_VERSION
                or not isinstance(baseline.get("attemptId"), str) or not baseline["attemptId"]):
            raise ValueError("invalid baseline schema")
    except (OSError, ValueError) as exc:
        result["reasons"] = ["baseline unavailable/invalid: " + str(exc)]
        return result
    reasons = result["reasons"]
    for label, value in (("current", report), ("baseline", baseline)):
        if not value.get("inputBefore") or value.get("inputBefore") != value.get("inputAfter"):
            reasons.append(f"{label}: source identity changed or absent")
        if not value.get("versionsBefore") or value.get("versionsBefore") != value.get("versionsAfter"):
            reasons.append(f"{label}: tool versions changed or absent")
    for field in ("inputBefore", "versionsBefore", "ompPath", "mode", "cwd", "costIdentity"):
        if field not in baseline or baseline[field] != report.get(field):
            reasons.append("different comparison identity: " + field)
    old_cost, old_reasons = _scope_cost(baseline)
    new_cost, new_reasons = _scope_cost(report)
    reasons.extend("baseline: " + reason for reason in old_reasons)
    reasons.extend("current: " + reason for reason in new_reasons)
    if old_cost is not None and new_cost is not None:
        old_counts = {row["id"]: row["collectedTests"] for row in baseline["items"]
                      if row["id"] in old_cost["items"]}
        new_counts = {row["id"]: row["collectedTests"] for row in report["items"]
                      if row["id"] in new_cost["items"]}
        if old_counts != new_counts:
            reasons.append("different comparison identity: collectedTests map")
    old_execution, new_execution = baseline.get("execution"), report.get("execution")
    if (not isinstance(old_execution, dict) or not isinstance(new_execution, dict)
            or type(old_execution.get("jobs")) is not int or old_execution["jobs"] not in (1, 2)
            or type(new_execution.get("jobs")) is not int or new_execution["jobs"] not in (1, 2)):
        reasons.append("verified jobs metadata missing/invalid")
    result.update(baselineAttemptId=baseline.get("attemptId"), baselineFirstFailure=baseline.get("firstFailure"),
                  baselineJobs=old_execution.get("jobs") if isinstance(old_execution, dict) else None,
                  currentJobs=new_execution.get("jobs") if isinstance(new_execution, dict) else None)
    if not reasons:
        result.update(status="comparable", before=old_cost, after=new_cost,
                      wallDeltaSeconds=round(new_cost["wallSeconds"] - old_cost["wallSeconds"], 3),
                      itemSumDeltaSeconds=round(new_cost["itemSumSeconds"] - old_cost["itemSumSeconds"], 3),
                      itemCosts=[{"id": key, "beforeSeconds": value, "afterSeconds": new_cost["items"][key],
                                  "deltaSeconds": round(new_cost["items"][key] - value, 3)}
                                 for key, value in old_cost["items"].items()])
    return result


def _prior(path: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if path is None:
        return [], None
    raw = path.read_bytes()
    if len(raw) > 1_000_000:
        raise ValueError("prior report exceeds 1 MiB")
    prior = json.loads(raw)
    if prior.get("schemaVersion") != SCHEMA_VERSION or not isinstance(prior.get("attemptChain"), list):
        raise ValueError("invalid prior report")
    return prior["attemptChain"], prior.get("firstFailure")


def _overall(items: list[dict[str, Any]], first_failure: dict[str, Any] | None) -> tuple[str, int]:
    if any(item["status"] == "failed" for item in items) or (first_failure and first_failure.get("status") == "failed"):
        return "failed", 1
    if any(item["required"] and item["status"] != "passed" for item in items) or first_failure:
        return "inconclusive", 2
    return "passed", 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=tuple(TARGET_SECONDS), required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--omp", type=Path)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--prior-report", type=Path)
    parser.add_argument("--baseline-report", type=Path)
    args = parser.parse_args(argv)
    if args.jobs not in (1, 2):
        parser.error("only --jobs 1/2 is verified; jobs 2 overlaps the two tmux cells only")
    omp = args.omp or (Path(shutil.which("omp")) if shutil.which("omp") else None)
    if omp is None or not omp.is_absolute() or not omp.is_file() or not os.access(omp, os.X_OK):
        parser.error("an installed absolute executable --omp is required")
    try:
        report_dir = _private_dir(args.report_dir)
        chain, inherited_failure = _prior(args.prior_report)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    invocation_id = str(uuid4())
    attempt_id = str(uuid4())
    started = time.monotonic()
    before = _input_manifest()
    versions_before = harness.tool_versions(str(omp))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                          text=True, check=True, env=harness.clean_base_env()).stdout.strip()
    items: list[dict[str, Any]] = []
    selected_items = FAST_ITEMS + (PRECISE_ITEMS if args.mode == "precise" else ())
    if args.jobs == 1:
        for index, item in enumerate(selected_items):
            items.append(_run_item(item, index, report_dir, omp, invocation_id))
    else:
        for index, item in enumerate(FAST_ITEMS):
            items.append(_run_item(item, index, report_dir, omp, invocation_id))
        if args.mode == "precise":
            items.extend(_parallel_items(PRECISE_ITEMS, report_dir, omp, invocation_id, len(FAST_ITEMS)))
    if args.mode == "precise":
        items.extend({"id": name, "assertionIds": ["DV-AC-03"], "required": True,
                      "evidenceLevel": "runtime", "status": "not_run", "reason": "DVL-13/final product registry pending",
                      "collectedTests": 0, "durationSeconds": 0, "expectedWaitSeconds": 0}
                     for name in PRECISE_PENDING)
    after = _input_manifest()
    versions_after = harness.tool_versions(str(omp))
    if before["sha256"] != after["sha256"] or versions_before != versions_after:
        items.append({"id": "input_identity", "assertionIds": ["DV-AC-04"], "required": True,
                      "evidenceLevel": "static", "status": "inconclusive",
                      "reason": "inputs or tool versions changed during validation", "collectedTests": 0,
                      "durationSeconds": 0, "expectedWaitSeconds": 0})
    own_failure = next(({"item": item["id"], "status": item["status"], "reason": item.get("reason"),
                         "attemptId": attempt_id} for item in items
                        if item["status"] in ("failed", "inconclusive", "not_run")), None)
    first_failure = inherited_failure or own_failure
    status, exit_code = _overall(items, first_failure)
    elapsed = round(time.monotonic() - started, 3)
    baseline_identity = sha256(json.dumps({"input": before["sha256"], "versions": versions_before,
                                           "mode": args.mode}, sort_keys=True).encode()).hexdigest()
    report = {"schemaVersion": SCHEMA_VERSION, "invocationId": invocation_id, "attemptId": attempt_id,
              "attemptChain": [*chain, {"attemptId": attempt_id, "status": status,
                                       "inputSha256": before["sha256"]}],
              "mode": args.mode, "scope": ("developer fast subset; not product compatibility pass"
                                             if args.mode == "fast" else
                                             "developer fast + representative core Bash/sh precise; DVL-13/product gates pending"),
              "cwd": str(REPO), "gitHead": head, "inputBefore": before, "inputAfter": after,
              "versionsBefore": versions_before, "versionsAfter": versions_after,
              "ompPath": str(omp), "targetSeconds": TARGET_SECONDS[args.mode],
              "elapsedSeconds": elapsed, "targetDeltaSeconds": round(elapsed - TARGET_SECONDS[args.mode], 3),
              "targetExceeded": elapsed > TARGET_SECONDS[args.mode],
              "readinessWatchdogSeconds": READINESS_SECONDS, "scenarioWatchdogSeconds": SCENARIO_IDLE_SECONDS,
              "cleanupWatchdogSeconds": CLEANUP_SECONDS, "items": items, "firstFailure": first_failure,
              "result": status, "exitCode": exit_code,
              "environment": {"privateNamespace": str(report_dir), "allowlistChildEnv": True,
                              "provider": "scripted loopback only", "userAuthInherited": False,
                              "parallelCells": 2 if args.jobs == 2 and args.mode == "precise" else 1},
              "execution": {"jobs": args.jobs, "fastSerial": True,
                            "parallelEligibility": ["compat_tmux_bash", "compat_tmux_dash"] if args.jobs == 2 and args.mode == "precise" else [],
                            "otherCells": "serialized", "herdrSerial": True},
              "costIdentity": _cost_identity(selected_items, omp),
              "measuredCost": {"wallSeconds": elapsed,
                               "itemSumSeconds": round(sum(item["durationSeconds"] for item in items), 3)},
              "baselineIdentity": baseline_identity,
              "baseline": {item["id"]: {"durationSeconds": item["durationSeconds"],
                                        "expectedWaitSeconds": item["expectedWaitSeconds"]} for item in items}}
    if args.mode == "precise":
        report["matrix"] = {"outers": ["plain", "tmux", "herdr"], "shells": ["bash", "dash"],
                            "shellLabels": {"bash": "Bash", "dash": "sh(dash)"},
                            "selection": ["SEL1", "SEL2", "SEL3"], "decision": "LG-D29",
                            "shFallback": "existing dash cells exercise this host's sh fallback; historical failures/holds retained",
                            "testIds": [name for item in PRECISE_ITEMS for name in item.modules]}
        report["notRunCoverage"] = [
            {"id": "legacy_full_regression", "status": "not_run", "reason": "remaining legacy flow/policy/fault assertions require separate regression"},
            {"id": "VM_GUI_paid_model", "status": "not_run", "reason": "outside local scripted-provider scope"},
        ]
    comparison = _compare_cost(report, args.baseline_report)
    report["costComparison"] = comparison
    if comparison["status"] == "unknown":
        items.append({"id": "cost_comparison", "assertionIds": ["DV-AC-03", "DV-AC-05"], "required": True,
                      "evidenceLevel": "static", "status": "inconclusive", "reason": comparison["reasons"],
                      "collectedTests": 0, "durationSeconds": 0, "expectedWaitSeconds": 0})
        if first_failure is None:
            first_failure = {"item": "cost_comparison", "status": "inconclusive",
                             "reason": comparison["reasons"], "attemptId": attempt_id}
        status, exit_code = _overall(items, first_failure)
        report.update(result=status, exitCode=exit_code, firstFailure=first_failure)
        report["attemptChain"][-1]["status"] = status
    target = report_dir / "report.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"result": status, "exitCode": exit_code, "report": str(target),
                      "elapsedSeconds": elapsed, "firstFailure": first_failure}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    if len(sys.argv) == 13 and sys.argv[1] == "--_cell-worker":
        registry = {item.key: item for item in (*FAST_ITEMS, *PRECISE_ITEMS)}
        raise SystemExit(_cell_worker_main(registry[sys.argv[2]], int(sys.argv[3]), sys.argv[4], sys.argv[5],
                                          int(sys.argv[6]), int(sys.argv[7]), int(sys.argv[8]), int(sys.argv[9]),
                                          Path(sys.argv[10]), Path(sys.argv[11]), Path(sys.argv[12])))
    if len(sys.argv) >= 7 and sys.argv[1] == "--_driver":
        raise SystemExit(_driver_main(sys.argv[2], sys.argv[3], sys.argv[4],
                                      int(sys.argv[5]), sys.argv[6:]))
    raise SystemExit(main())
