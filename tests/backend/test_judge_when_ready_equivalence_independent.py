"""Independent differential checks for the ``_judge_when_ready`` split (commits da64675 and 5aa7d9e).

Contract under test
-------------------
Both commits are PURE refactors:

* da64675 (+ bf7f151): ``WorkflowRun.worker_analysis_request`` (property, the RAW stored value) and
  ``WorkflowRun.record_snapshot()`` replace the direct reads of the private ``_record`` in ``automation.py``
  (``_collect_non_model``) and ``workflow_binding.py`` (``collect_and_notify``).
* 5aa7d9e: ``TaskFlow._judge_when_ready`` was cut into ``_wait_out_hold``, ``_busy_past_limit``, ``_judge_once``,
  ``_analysis_held`` (+ ``_AnalysisWait``, ``_JUDGE_AGAIN``, ``_worker_analysis_request``).

Nothing observable may change: the order and arguments of every collaborator call, every ``time.monotonic`` read
(count and order), the busy / deferred period bookkeeping, the Task held reasons and statuses, the journal / ledger
records, lock ownership at each statement, the returned record / ``_FOLLOW_ENDED``, and the exceptions that escape.

Method (differential)
---------------------
The baseline (``1cdc387``) sources are read at test time with ``git show`` and loaded under other module names. Every
scenario runs against BOTH versions and the normalised outcomes must be IDENTICAL:

* layer A: ``_judge_when_ready`` driven directly with scripted collaborators and a fake ``time`` module (every
  ``monotonic`` read recorded), so the exact call sequence and the clock reads are compared;
* layer B: the real ``TaskFlow`` runner thread (the ``FlowEnv`` of ``test_flow_refactor_equivalence_independent``),
  comparing ledger / journal records, per-thread traces with lock ownership and Task status, and the final state;
* layer C: ``collect_and_notify`` and ``AutomationController._collect_non_model`` for the ``record_snapshot()`` copy
  semantics (changed vs unchanged record; mutating the copy);
* layer D: pins written from the contract (not from the baseline) for the new accessors and helper values.

Hygiene: every thread, scheduler and temp dir is created here and closed at the end of each test; no signal is sent
and nothing outside the test's own temp dir is touched.
"""

from __future__ import annotations

import collections
import copy
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from workbench.backend import automation as current_automation
from workbench.backend import flow_tasks as current_tasks
from workbench.backend import flow_terminal as current_terminal
from workbench.contracts.v1 import ActorRole
from workbench.observation import workflow_binding as current_binding
from workbench.observation.worker_review import SerializedReviewAdmission, WorkerReviewScheduler
from workbench.workflow.run import WorkerJudgmentUnavailable, WorkflowHeld, WorkflowRun

REPO = Path(__file__).resolve().parents[2]
BASELINE_REV = "1cdc387"  # the commit just before da64675 / 5aa7d9e
SUMMARY_MAX = current_tasks.SUMMARY_MAX


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
    paths = {"tasks": "src/workbench/backend/flow_tasks.py",
             "binding": "src/workbench/observation/workflow_binding.py",
             "automation": "src/workbench/backend/automation.py"}
    sources = {key: _git_show(path) for key, path in paths.items()}
    if any(value is None for value in sources.values()):
        return None
    return {key: _load(f"wb_{key}_baseline_{BASELINE_REV}", source) for key, source in sources.items()}


BASELINES = load_baselines()
BASE_TASKS = BASELINES["tasks"] if BASELINES else None
BASE_BINDING = BASELINES["binding"] if BASELINES else None
BASE_AUTOMATION = BASELINES["automation"] if BASELINES else None
needs_baseline = unittest.skipIf(BASELINES is None, f"git cannot provide {BASELINE_REV} (differential checks only)")

try:  # layer B reuses the whole-flow harness of the sibling differential suite
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_flow_refactor_equivalence_independent as fx  # noqa: E402
except Exception:  # pragma: no cover - the layer is skipped, not weakened
    fx = None
finally:
    if sys.path and sys.path[0] == str(Path(__file__).resolve().parent):
        sys.path.pop(0)
needs_flow_harness = unittest.skipIf(fx is None, "the whole-flow harness is not importable")


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


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


class NoLeak(unittest.TestCase):
    def setUp(self):
        self.watch = ThreadWatch()
        self.addCleanup(self.assert_no_leak)

    def assert_no_leak(self):
        self.assertEqual(self.watch.leaked(), [], "a thread started by the scenario is still alive")


# =================================================================================================== LAYER A
class LoopCap(Exception):
    """Raised by the scripted ``_wait`` so that a runaway loop ends the scenario (identically on both versions)."""


def pick(script, index, default):
    if script is None:
        return default
    return script[min(index, len(script) - 1)]


class FakeTime:
    """Stands in for the ``time`` module of a flow_tasks module: every ``monotonic`` read is traced and numbered."""

    def __init__(self, env: "DirectEnv"):
        self.env = env

    def monotonic(self) -> float:
        env = self.env
        value = env.now + env.spec.get("tick", 0.0) * env.reads
        env.reads += 1
        env.trace.append(("mono", value))
        return value

    def __getattr__(self, name):
        return getattr(time, name)


REQUEST_STEPS: dict[str, Any] = {  # step -> value written to ``_record["worker_analysis_request"]`` before WorkflowHeld
    "held_deferred": {"status": "deferred"},
    "held_deferred_extra": {"status": "deferred", "message_id": "m-1", "attempt": 2},
    "held_sent": {"status": "sent"},
    "held_empty": {},
    "held_status_none": {"status": None},
    "held_status_int": {"status": 5},
    "held_none_value": None,
    "held_falsy_list": [],
    "held_falsy_zero": 0,
    "held_falsy_str": "",
}


class DirectRun:
    """A duck-typed run: only ``_record``, ``run_id`` and ``judge`` (what the flow tests' doubles have)."""

    run_id = "run-1"

    def __init__(self, env: "DirectEnv"):
        self.env = env
        self._record: dict = copy.deepcopy(env.spec.get("record", {}))
        self.calls = 0
        style = env.spec.get("sig", "full")
        self.judge = {"full": self._judge_full, "plain": self._judge_plain, "hook": self._judge_hook}[style]

    def _judge_full(self, on_report=None, report_hold=None):
        return self._judge(on_report, report_hold)

    def _judge_hook(self, on_report=None):
        return self._judge(on_report, None)

    def _judge_plain(self):
        return self._judge(None, None)

    def _judge(self, on_report, report_hold):
        env = self.env
        step = pick(env.spec.get("judge"), self.calls, "ok")
        self.calls += 1
        env.trace.append(("judge", self.calls, step, on_report is env.on_report if on_report else None,
                          report_hold is not None))
        if report_hold is not None:
            env.trace.append(("report_hold_value", report_hold()))
        if step == "ok":
            if on_report is not None:
                on_report("sending", {})
            return {"report": {"status": "omp_processed"}, "worker_judgment": {"judgment": "success"}}
        if step == "none":
            return None
        if step == "unavailable":
            raise WorkerJudgmentUnavailable("analysis_rejected", {"judgment": "x", "reasons": ["r1"]})
        if step == "unavailable_noevidence":
            raise WorkerJudgmentUnavailable("analysis_unknown", {})
        if step == "unavailable_deferred_request":  # the subclass is caught first even with a deferred request
            self._record["worker_analysis_request"] = {"status": "deferred"}
            raise WorkerJudgmentUnavailable("analysis_rejected", {"judgment": "y"})
        if step == "error":
            raise RuntimeError("boom")
        if step == "error_with_hold":  # a plain RuntimeError is not a WorkflowHeld
            self._record["worker_analysis_request"] = {"status": "deferred"}
            raise RuntimeError("boom with a deferred request")
        if step == "base_exception":  # BaseException is not an ``Exception``: escapes both versions
            raise KeyboardInterrupt("stop")
        if step == "held_keep":  # raise without touching the record (a request may be present from before)
            raise WorkflowHeld("held keep")
        if step == "held_missing":
            self._record.pop("worker_analysis_request", None)
            raise WorkflowHeld("held missing")
        if step == "held_long":
            self._record["worker_analysis_request"] = {"status": "sent"}
            raise WorkflowHeld("x" * (SUMMARY_MAX + 500))
        if step in REQUEST_STEPS:
            self._record["worker_analysis_request"] = copy.deepcopy(REQUEST_STEPS[step])
            raise WorkflowHeld(f"analysis {step}")
        raise AssertionError(step)


class DirectRunProp(DirectRun):
    """Like ``WorkflowRun``: exposes ``worker_analysis_request`` as a property returning the RAW stored value."""

    @property
    def worker_analysis_request(self):
        return self._record.get("worker_analysis_request")


class DirectRunReal(DirectRun, WorkflowRun):
    """A real ``WorkflowRun`` instance (its own property) whose ``judge`` is the scripted one."""


class DirectEnv:
    """One scripted ``_judge_when_ready`` call: every collaborator is a recording stub on a bare ``TaskFlow``."""

    def __init__(self, module, spec: dict):
        self.module, self.spec = module, spec
        self.trace: list[tuple] = []
        self.count: collections.Counter = collections.Counter()
        self.now = 0.0
        self.reads = 0
        self.task = SimpleNamespace(task_id="task-1")
        self.accepted: list[bool] = []
        self.on_report = lambda event, record: self.trace.append(("on_report", event))
        kind = spec.get("prop")
        self.run = (DirectRunReal if kind == "real" else DirectRunProp if kind else DirectRun)(self)
        flow = object.__new__(module.TaskFlow)
        flow._poll_interval = spec.get("poll", 1.0)
        flow._analysis_idle_limit = spec.get("limit", 10.0)
        for name in ("_wait", "_cancelled", "_end_cancelled_run", "_paused_now", "_held_now", "_omp_idle",
                     "_set_held", "_worker_not_idle_reason", "_judgment_unavailable", "_finish_run", "_run_error"):
            setattr(flow, name, getattr(self, "s" + name))
        self.flow = flow

    # -- scripted collaborators (each one is traced with its arguments)
    def _next(self, key: str) -> int:
        index = self.count[key]
        self.count[key] += 1
        return index

    def _maybe_raise(self, key: str) -> None:
        factory = self.spec.get("raise_in", {}).get(key)
        if factory is not None:
            raise factory(f"{key} failed")

    def s_wait(self, seconds):
        index = self._next("wait")
        if index >= self.spec.get("max_waits", 80):
            raise LoopCap()
        self.trace.append(("wait", seconds))
        if self.spec.get("advance", True):
            self.now += seconds
        return index not in self.spec.get("wait_false", ())

    def s_cancelled(self, task):
        value = pick(self.spec.get("cancel"), self._next("cancel"), False)
        self.trace.append(("cancelled", task is self.task, value))
        return value

    def s_end_cancelled_run(self, task, run):
        self.trace.append(("end_cancelled", task is self.task, run is self.run))

    def s_paused_now(self):
        value = pick(self.spec.get("paused"), self._next("paused"), False)
        self.trace.append(("paused", value))
        return value

    def s_held_now(self, role=ActorRole.WORKER):
        key = "held_" + role.value
        value = pick(self.spec.get(key), self._next(key), None)
        self.trace.append(("held", role.value, value))
        return value

    def s_omp_idle(self, role):
        value = pick(self.spec.get("idle"), self._next("idle"), True)
        self.trace.append(("idle", role.value, value))
        return value

    def s_set_held(self, task_id, reason):
        self.trace.append(("set_held", task_id, reason))
        self._maybe_raise("set_held")

    def s_worker_not_idle_reason(self):
        self.trace.append(("not_idle_reason",))
        return "worker_not_idle_for_analysis"

    def s_judgment_unavailable(self, task, run, reason, host):
        self.trace.append(("unavailable", task is self.task, run is self.run, reason, host))
        self._maybe_raise("unavailable")

    def s_finish_run(self, task, run, outcome, details):
        self.trace.append(("finish", task is self.task, run is self.run, outcome, dict(details)))
        self._maybe_raise("finish")

    def s_run_error(self, task, run, accepted, on_report, exc):
        self.trace.append(("run_error", task is self.task, run is self.run, accepted is self.accepted,
                           on_report is self.on_report, type(exc).__name__, str(exc)))
        self._maybe_raise("run_error")

    # -- driving
    def run_it(self) -> dict:
        module = self.module
        saved = module.time
        module.time = FakeTime(self)
        try:
            try:
                result = self.flow._judge_when_ready(self.task, self.run, self.accepted, self.on_report)
            except BaseException as exc:  # the escaping exception is part of the outcome
                outcome = ("raised", type(exc).__name__, str(exc))
            else:
                outcome = ("returned", self._describe(result))
        finally:
            module.time = saved
        return {"outcome": outcome, "trace": self.trace, "record": copy.deepcopy(self.run._record),
                "accepted": list(self.accepted), "reads": self.reads, "now": self.now,
                "counts": dict(self.count)}

    def _describe(self, result):
        if result is module_attr(self.module, "_FOLLOW_ENDED"):
            return "FOLLOW_ENDED"
        if result is module_attr(self.module, "_JUDGE_AGAIN"):
            return "JUDGE_AGAIN_LEAKED"
        return result


def module_attr(module, name):
    return getattr(module, name, object())


def run_direct(module, spec: dict) -> dict:
    return DirectEnv(module, spec).run_it()


def names(outcome: dict, *keep: str) -> list:
    return [event for event in outcome["trace"] if not keep or event[0] in keep]


def mono(outcome: dict) -> list[float]:
    return [event[1] for event in outcome["trace"] if event[0] == "mono"]


def set_helds(outcome: dict) -> list:
    return [event[2] for event in outcome["trace"] if event[0] == "set_held"]


FOLLOW = ("returned", "FOLLOW_ENDED")

DIRECT_SCENARIOS: dict[str, dict] = {
    # -- plain judge outcomes
    "ready_at_once": {"judge": ["ok"]},
    "ready_report_hook_only": {"judge": ["ok"], "sig": "hook"},
    "ready_plain_signature": {"judge": ["ok"], "sig": "plain"},
    "judge_returns_none": {"judge": ["none"]},
    "judge_unavailable_with_evidence": {"judge": ["unavailable"]},
    "judge_unavailable_without_evidence": {"judge": ["unavailable_noevidence"]},
    "judge_unavailable_beats_a_deferred_request": {"judge": ["unavailable_deferred_request"]},
    "judge_error": {"judge": ["error"]},
    "judge_error_with_a_deferred_request_is_not_a_hold": {"judge": ["error_with_hold"]},
    "judge_base_exception_escapes": {"judge": ["base_exception"]},
    "report_hold_value_manager_none": {"judge": ["ok"], "held_manager": [None]},
    "report_hold_value_manager_reason": {"judge": ["ok"], "held_manager": ["manager_maintenance"]},
    "report_hold_value_worker_reason_is_not_the_managers": {"judge": ["ok"], "held_worker": [None, "w"],
                                                            "held_manager": ["m"]},
    # -- held judge that is not a deferred analysis delivery -> the run ends held
    "held_missing_request": {"judge": ["held_missing"]},
    "held_empty_request": {"judge": ["held_empty"]},
    "held_sent_request": {"judge": ["held_sent"]},
    "held_status_none": {"judge": ["held_status_none"]},
    "held_status_int": {"judge": ["held_status_int"]},
    "held_none_value": {"judge": ["held_none_value"]},
    "held_falsy_list_request": {"judge": ["held_falsy_list"]},
    "held_falsy_zero_request": {"judge": ["held_falsy_zero"]},
    "held_falsy_str_request": {"judge": ["held_falsy_str"]},
    "held_long_message_is_cut": {"judge": ["held_long"]},
    "held_request_present_from_before_deferred": {"judge": ["held_keep"], "limit": 3.0,
                                                  "record": {"worker_analysis_request": {"status": "deferred"}},
                                                  "idle": [True, True, True, True, True, True]},
    "held_request_present_from_before_sent": {"judge": ["held_keep"],
                                              "record": {"worker_analysis_request": {"status": "sent"}}},
    "held_without_any_record_key": {"judge": ["held_keep"]},
    "finish_run_raises_in_the_held_handler": {"judge": ["held_empty"], "raise_in": {"finish": ValueError}},
    "unavailable_raises_in_its_handler": {"judge": ["unavailable"], "raise_in": {"unavailable": ValueError}},
    "run_error_raises_in_the_generic_handler": {"judge": ["error"], "raise_in": {"run_error": ValueError}},
    # -- deferred analysis delivery
    "deferred_then_ready": {"judge": ["held_deferred", "ok"], "limit": 10.0},
    "deferred_twice_then_ready": {"judge": ["held_deferred", "held_deferred", "ok"], "limit": 10.0},
    "deferred_extra_fields_then_ready": {"judge": ["held_deferred_extra", "ok"]},
    "deferred_up_to_the_limit": {"judge": ["held_deferred"], "limit": 3.0},
    "deferred_limit_just_not_reached": {"judge": ["held_deferred"] * 3 + ["ok"], "limit": 3.0},
    "deferred_limit_exactly_reached": {"judge": ["held_deferred"], "limit": 2.0},
    "deferred_limit_one_poll_longer": {"judge": ["held_deferred"], "limit": 2.5},
    "deferred_limit_zero_gives_up_at_once": {"judge": ["held_deferred"], "limit": 0.0},
    "deferred_with_a_tick_clock": {"judge": ["held_deferred"], "limit": 3.0, "tick": 0.25},
    "deferred_slow_poll": {"judge": ["held_deferred"], "limit": 5.0, "poll": 2.0},
    "deferred_wait_does_not_advance": {"judge": ["held_deferred", "held_deferred", "ok"], "advance": False,
                                       "limit": 1.0},
    "deferred_then_judge_error": {"judge": ["held_deferred", "error"]},
    "deferred_then_unavailable": {"judge": ["held_deferred", "unavailable"]},
    "deferred_then_held_other": {"judge": ["held_deferred", "held_sent"]},
    "deferred_then_none": {"judge": ["held_deferred", "none"]},
    # -- worker busy
    "busy_then_ready": {"idle": [False, False, True], "judge": ["ok"], "limit": 10.0},
    "busy_unknown_then_ready": {"idle": [None, True], "judge": ["ok"]},
    "busy_unknown_forever": {"idle": [None], "limit": 3.0},
    "idle_truthy_non_bool_is_not_idle": {"idle": [1, True], "judge": ["ok"]},
    "idle_string_is_not_idle": {"idle": ["yes", "yes", True], "judge": ["ok"]},
    "idle_zero_is_not_idle": {"idle": [0, True], "judge": ["ok"]},
    "idle_truthy_non_bool_forever": {"idle": [1], "limit": 2.0},
    "busy_past_the_limit": {"idle": [False], "limit": 3.0},
    "busy_limit_exactly_reached_on_first_read": {"idle": [False], "limit": 0.0},
    "busy_limit_one_poll_longer": {"idle": [False], "limit": 3.5},
    "busy_with_a_tick_clock": {"idle": [False], "limit": 2.0, "tick": 0.3},
    "busy_clock_does_not_advance": {"idle": [False, False, False, True], "advance": False, "limit": 1.0,
                                    "judge": ["ok"]},
    "busy_slow_poll": {"idle": [False], "limit": 5.0, "poll": 2.0},
    # -- busy and deferred alternating (busy_since / deferred_since continuity)
    "busy_then_deferred_then_ready": {"idle": [False, True, True], "judge": ["held_deferred", "ok"]},
    "deferred_then_busy_then_ready": {"idle": [True, False, True], "judge": ["held_deferred", "ok"]},
    "deferred_busy_deferred_clears_deferred_since": {"idle": [True, False, True, True], "limit": 2.0,
                                                     "judge": ["held_deferred", "held_deferred", "ok"]},
    "deferred_busy_deferred_limit_loose": {"idle": [True, False, True, True], "limit": 4.0,
                                           "judge": ["held_deferred", "held_deferred", "ok"]},
    "busy_deferred_busy_deferred_giving_up": {"idle": [False, True, False, True, False, True], "limit": 5.0,
                                              "judge": ["held_deferred"]},
    "idle_resets_busy_since_before_a_deferral": {"idle": [False, False, True, True, True], "limit": 3.0,
                                                 "judge": ["held_deferred", "held_deferred", "ok"]},
    "busy_resets_after_idle_new_period": {"idle": [False, True, False, False, True], "limit": 3.0,
                                          "judge": ["held_deferred", "ok"]},
    "alternating_never_reaches_the_limit": {"idle": [True, False] * 6 + [True], "limit": 100.0,
                                            "judge": ["held_deferred"] * 6 + ["ok"]},
    "alternating_with_tick": {"idle": [True, False] * 4, "limit": 2.2, "tick": 0.2,
                              "judge": ["held_deferred"], },
    # -- paused / worker hold / hold unknown
    "paused_then_ready": {"paused": [True, True, False], "judge": ["ok"]},
    "paused_forever_then_stopped": {"paused": [True], "wait_false": {7}},
    "worker_hold_then_ready": {"held_worker": ["maintenance", "maintenance", None], "judge": ["ok"]},
    "worker_hold_unknown_then_ready": {"held_worker": ["hold_unknown", None], "judge": ["ok"]},
    "paused_beats_worker_hold": {"paused": [True, False], "held_worker": ["maintenance", None], "judge": ["ok"]},
    "pause_resets_the_busy_period": {"idle": [False, False, False, False], "paused": [False, True, False],
                                     "limit": 3.0},
    "pause_resets_the_busy_period_restart_visible": {"idle": [False, False, False, False, False],
                                                     "paused": [False, False, True, False], "limit": 3.0},
    "worker_hold_resets_the_busy_period": {"idle": [False], "held_worker": [None, None, "m", None], "limit": 3.0},
    "pause_resets_a_deferral": {"idle": [True], "judge": ["held_deferred"], "limit": 3.0,
                                "paused": [False, True, False]},
    "worker_hold_resets_a_deferral": {"idle": [True], "judge": ["held_deferred"], "limit": 3.0,
                                      "held_worker": [None, "m", None]},
    "pause_after_deferral_then_deferral_again_within_limit": {
        "idle": [True], "judge": ["held_deferred", "held_deferred", "ok"], "limit": 2.0,
        "paused": [False, True, True, False, False]},
    "hold_after_deferral_then_deferral_again_within_limit": {
        "idle": [True], "judge": ["held_deferred", "held_deferred", "ok"], "limit": 2.0,
        "held_worker": [None, "m", "m", None, None]},
    "busy_then_paused_then_busy_to_the_limit": {"idle": [False], "paused": [False, False, True, False], "limit": 4.0},
    "idle_after_hold_does_not_use_stale_busy": {"idle": [False, True], "held_worker": [None, "m", None], "judge": ["ok"]},
    # -- cancellation and flow stop
    "cancel_before_anything": {"cancel": [True]},
    "cancel_while_busy": {"idle": [False], "cancel": [False, False, True]},
    "cancel_while_paused": {"paused": [True], "cancel": [False, False, True]},
    "cancel_while_deferred": {"idle": [True], "judge": ["held_deferred"], "cancel": [False, True]},
    "cancel_after_hold_wait": {"held_worker": ["m"], "cancel": [False, False, True]},
    "stopped_at_the_first_wait": {"wait_false": {0}},
    "stopped_while_busy": {"idle": [False], "wait_false": {2}},
    "stopped_while_deferred": {"idle": [True], "judge": ["held_deferred"], "wait_false": {2}},
    "stopped_after_a_poll_wait_returns_false": {"idle": [False], "wait_false": {1}},
    "stopped_in_the_deferral_poll": {"idle": [True], "judge": ["held_deferred"], "wait_false": {1}},
    "cancel_beats_pause": {"paused": [True], "cancel": [True]},
    # -- misc
    "runaway_loop_is_capped": {"idle": [False], "limit": 1e9, "max_waits": 12},
    "set_held_raises_while_busy": {"idle": [False], "raise_in": {"set_held": ValueError}},
    "set_held_raises_after_the_idle_observation": {"judge": ["ok"], "raise_in": {"set_held": ValueError}},
    "set_held_raises_in_the_deferral_wait": {"idle": [True], "judge": ["held_deferred"], "limit": 100.0,
                                             "raise_in": {"set_held": ValueError}},
    "everything_at_once": {"idle": [False, True, True, False, True, True], "paused": [False, False, True, False],
                           "held_worker": [None, None, None, "m", None], "limit": 6.0, "tick": 0.1,
                           "judge": ["held_deferred", "held_deferred", "ok"]},
}


class DirectDifferential(NoLeak):
    """Layer A: the exact collaborator call sequence, every ``monotonic`` read and every outcome stay identical."""

    @needs_baseline
    def test_every_scenario_is_identical_before_and_after_the_split(self):
        for name, spec in DIRECT_SCENARIOS.items():
            for prop in (False, True, "real"):  # duck-typed run / run with the property / a real WorkflowRun
                with self.subTest(scenario=name, prop=prop):
                    case = dict(spec, prop=prop)
                    before = run_direct(BASE_TASKS, case)
                    after = run_direct(current_tasks, case)
                    self.assertEqual(after, before)

    @needs_baseline
    def test_the_scenarios_are_repeatable(self):
        for name in ("everything_at_once", "deferred_busy_deferred_clears_deferred_since", "busy_with_a_tick_clock"):
            with self.subTest(scenario=name):
                self.assertEqual(run_direct(current_tasks, DIRECT_SCENARIOS[name]),
                                 run_direct(current_tasks, DIRECT_SCENARIOS[name]))

    @needs_baseline
    def test_the_scenarios_cover_every_outcome_kind(self):
        # a differential check over scenarios that all end the same way would prove little
        seen = collections.Counter()
        for name, spec in DIRECT_SCENARIOS.items():
            result = run_direct(current_tasks, spec)
            kind, value = result["outcome"][0], result["outcome"][1]
            seen["raised" if kind == "raised" else "record" if isinstance(value, dict) else str(value)] += 1
            for event in result["trace"]:
                if event[0] in ("unavailable", "finish", "run_error", "end_cancelled"):
                    seen["call:" + event[0]] += 1
                if event[0] == "set_held":
                    seen["held:" + str(event[2])] += 1
        for needed in ("FOLLOW_ENDED", "record", "raised", "call:unavailable", "call:finish", "call:run_error",
                       "call:end_cancelled", "held:paused", "held:worker_busy", "held:None", "held:maintenance",
                       "held:hold_unknown", "None"):
            self.assertGreater(seen[needed], 0, needed)

    @needs_baseline
    def test_a_truthy_non_dict_request_fails_exactly_as_before_for_every_kind_of_run(self):
        # the raw stored value is passed on (bf7f151): ``.get`` on a non-dict raises AttributeError out of the
        # ``except WorkflowHeld`` handler, on a duck-typed run, a run with the property and a real WorkflowRun alike
        for value in ("deferred", ["status", "deferred"], 7, True, 1.5, ("status",), {"status"}):
            for kind in (False, True, "real"):
                with self.subTest(value=value, run=kind):
                    spec = {"judge": ["held_keep"], "prop": kind, "record": {"worker_analysis_request": value}}
                    before, after = run_direct(BASE_TASKS, spec), run_direct(current_tasks, spec)
                    self.assertEqual(after, before)
                    self.assertEqual(after["outcome"][:2], ("raised", "AttributeError"))
                    self.assertEqual(names(after, "finish", "unavailable", "run_error"), [])

    def test_a_real_workflow_run_is_used_by_the_direct_layer(self):
        env = DirectEnv(current_tasks, {"prop": "real", "record": {"worker_analysis_request": {"status": "deferred"}}})
        self.assertIsInstance(env.run, WorkflowRun)
        self.assertIs(type(env.run).worker_analysis_request, WorkflowRun.worker_analysis_request)
        self.assertEqual(env.run.worker_analysis_request, {"status": "deferred"})


class DirectPins(NoLeak):
    """Layer A pins written from the contract (the original loop's rules), not from the baseline's output."""

    def run_current(self, name: str, **extra) -> dict:
        return run_direct(current_tasks, dict(DIRECT_SCENARIOS[name], **extra))

    def test_a_busy_worker_is_waited_for_with_one_busy_period_and_the_judge_asks_once(self):
        outcome = self.run_current("busy_then_ready")
        self.assertEqual(outcome["outcome"][0], "returned")
        self.assertEqual(set_helds(outcome), ["worker_busy", "worker_busy", None])
        # first busy observation reads the clock twice (start of the period, then the limit check), later ones once
        self.assertEqual(mono(outcome), [0.0, 0.0, 1.0])
        self.assertEqual([e[1] for e in names(outcome, "judge")], [1])
        self.assertEqual([e[:3] for e in names(outcome, "idle")], [("idle", "worker", False)] * 2
                         + [("idle", "worker", True)])
        self.assertEqual(outcome["accepted"], [])

    def test_a_busy_worker_is_given_up_exactly_at_the_idle_limit_without_judging(self):
        outcome = self.run_current("busy_past_the_limit")
        self.assertEqual(outcome["outcome"], FOLLOW)
        self.assertEqual(mono(outcome), [0.0, 0.0, 1.0, 2.0, 3.0])  # `>=`: reached at 3.0, not one poll later
        self.assertEqual(set_helds(outcome), ["worker_busy"] * 3)
        self.assertEqual(names(outcome, "judge"), [])
        self.assertEqual(names(outcome, "unavailable"),
                         [("unavailable", True, True, "worker_not_idle_for_analysis", None)])
        self.assertEqual(names(outcome, "finish"), [])

    def test_a_deferred_delivery_asks_again_and_is_given_up_at_the_limit_measured_from_the_first_deferral(self):
        outcome = self.run_current("deferred_up_to_the_limit")
        self.assertEqual(outcome["outcome"], FOLLOW)
        self.assertEqual(mono(outcome), [0.0, 1.0, 2.0, 3.0])  # one read per deferral
        self.assertEqual([e[1] for e in names(outcome, "judge")], [1, 2, 3, 4])
        self.assertEqual(set_helds(outcome), [None, "worker_busy", None, "worker_busy", None, "worker_busy", None])
        self.assertEqual(names(outcome, "unavailable"),
                         [("unavailable", True, True, "worker_not_idle_for_analysis", None)])

    def test_an_unavailable_analysis_ends_the_follow_with_the_exception_reason_and_evidence(self):
        outcome = self.run_current("judge_unavailable_with_evidence")
        self.assertEqual(outcome["outcome"], FOLLOW)
        self.assertEqual(names(outcome, "unavailable"),
                         [("unavailable", True, True, "analysis_rejected", {"judgment": "x", "reasons": ["r1"]})])
        self.assertEqual(names(outcome, "finish"), [])
        empty = self.run_current("judge_unavailable_without_evidence")
        self.assertEqual(names(empty, "unavailable"), [("unavailable", True, True, "analysis_unknown", None)],
                         "empty host evidence becomes None")
        subclass_first = self.run_current("judge_unavailable_beats_a_deferred_request")
        self.assertEqual(names(subclass_first, "finish"), [])
        self.assertEqual(len(names(subclass_first, "unavailable")), 1)

    def test_a_held_judge_without_a_deferred_request_ends_the_run_held_with_a_cut_reason(self):
        for name in ("held_missing_request", "held_empty_request", "held_sent_request", "held_falsy_list_request"):
            with self.subTest(scenario=name):
                outcome = self.run_current(name)
                self.assertEqual(outcome["outcome"], FOLLOW)
                self.assertEqual([e[3] for e in names(outcome, "finish")], ["held"])
                self.assertEqual(names(outcome, "unavailable"), [])
        long = self.run_current("held_long_message_is_cut")
        self.assertEqual(len(names(long, "finish")[0][4]["reason"]), SUMMARY_MAX)

    def test_a_generic_failure_goes_to_run_error_with_the_accepted_list_and_hook_once(self):
        outcome = self.run_current("judge_error")
        self.assertEqual(outcome["outcome"], FOLLOW)
        self.assertEqual(names(outcome, "run_error"), [("run_error", True, True, True, True, "RuntimeError", "boom")])
        deferred_but_plain_error = self.run_current("judge_error_with_a_deferred_request_is_not_a_hold")
        self.assertEqual(len(names(deferred_but_plain_error, "run_error")), 1)
        self.assertEqual(names(deferred_but_plain_error, "finish"), [])
        self.assertEqual(self.run_current("judge_base_exception_escapes")["outcome"][:2],
                         ("raised", "KeyboardInterrupt"))

    def test_a_judge_that_returns_nothing_returns_nothing_and_nothing_is_called(self):
        outcome = self.run_current("judge_returns_none")
        self.assertEqual(outcome["outcome"], ("returned", None))
        self.assertEqual(names(outcome, "unavailable", "finish", "run_error"), [])

    def test_the_report_hold_offered_to_the_judge_reads_the_manager_hold(self):
        outcome = self.run_current("report_hold_value_manager_reason")
        self.assertEqual(names(outcome, "report_hold_value"), [("report_hold_value", "manager_maintenance")])
        self.assertEqual([e[:2] for e in names(outcome, "held")], [("held", "worker"), ("held", "manager")])
        other = self.run_current("report_hold_value_worker_reason_is_not_the_managers")
        self.assertEqual(names(other, "report_hold_value"), [("report_hold_value", "m")])
        for name in ("ready_report_hook_only", "ready_plain_signature"):
            self.assertEqual(names(self.run_current(name), "report_hold_value"), [], name)
        hook = self.run_current("ready_report_hook_only")
        self.assertEqual([e[3:] for e in names(hook, "judge")], [(True, False)], "on_report only")
        plain = self.run_current("ready_plain_signature")
        self.assertEqual([e[3:] for e in names(plain, "judge")], [(None, False)], "no keyword at all")
        full = self.run_current("ready_at_once")
        self.assertEqual([e[3:] for e in names(full, "judge")], [(True, True)])

    def test_a_pause_or_worker_hold_is_shown_waits_one_poll_and_no_busy_period_counts(self):
        paused = self.run_current("paused_then_ready")
        self.assertEqual(set_helds(paused), ["paused", "paused", None])
        self.assertEqual(names(paused, "idle")[0][2], True)
        self.assertEqual([e[1] for e in names(paused, "wait")], [0, 1.0, 0, 1.0, 0])
        hold = self.run_current("worker_hold_then_ready")
        self.assertEqual(set_helds(hold), ["maintenance", "maintenance", None])
        self.assertEqual(mono(hold), [], "a hold never reads the clock")
        self.assertEqual(set_helds(self.run_current("worker_hold_unknown_then_ready")), ["hold_unknown", None])
        # the pause is checked before the hold: a paused flow does not even ask the worker hold
        beats = self.run_current("paused_beats_worker_hold")
        self.assertEqual([e[2] for e in names(beats, "held") if e[1] == "worker"], ["maintenance", None],
                         "the worker hold is asked only on the passes that are not paused")
        self.assertEqual(set_helds(beats), ["paused", "maintenance", None])

    def test_a_pause_or_hold_starts_a_new_busy_period(self):
        # limit 4 s, poll 1 s: busy from t=0 (2 reads), busy t=1, a paused pass (t=2 -> 3), then busy again: the
        # period restarts at t=3 (2 reads), so it is given up at t=7; an unreset period (from t=0) ended at t=4
        outcome = self.run_current("busy_then_paused_then_busy_to_the_limit")
        self.assertEqual(outcome["outcome"], FOLLOW)
        self.assertEqual(mono(outcome), [0.0, 0.0, 1.0, 3.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        self.assertEqual(set_helds(outcome), ["worker_busy", "worker_busy", "paused"] + ["worker_busy"] * 4)

    def test_a_deferral_clears_when_the_worker_looks_busy_in_between(self):
        # limit 2: deferral at t=0, busy t=1 (clears deferred_since), idle + deferral at t=2: only the busy period
        # (restarted by the idle observation) is counted, so the run waits on instead of giving up at t=2
        outcome = self.run_current("deferred_busy_deferred_clears_deferred_since")
        self.assertEqual(outcome["outcome"][0], "returned")
        self.assertEqual(names(outcome, "unavailable"), [])
        self.assertEqual([e[1] for e in names(outcome, "judge")], [1, 2, 3])
        tight = self.run_current("deferred_busy_deferred_limit_loose")
        self.assertEqual(tight["outcome"][0], "returned")

    def test_an_idle_observation_restarts_the_busy_period_before_a_deferral(self):
        outcome = self.run_current("idle_resets_busy_since_before_a_deferral")
        self.assertEqual(mono(outcome)[:3], [0.0, 0.0, 1.0])
        self.assertEqual(outcome["outcome"][0], "returned")

    def test_only_an_exact_true_idle_answer_lets_the_judge_run(self):
        for name in ("idle_truthy_non_bool_is_not_idle", "idle_string_is_not_idle", "idle_zero_is_not_idle"):
            with self.subTest(scenario=name):
                outcome = self.run_current(name)
                self.assertEqual(set_helds(outcome)[0], "worker_busy")
                self.assertEqual([e[1] for e in names(outcome, "judge")], [1])
        self.assertEqual(self.run_current("idle_truthy_non_bool_forever")["outcome"], FOLLOW)

    def test_a_deferral_keeps_an_earlier_busy_mark_and_measures_from_the_earliest_mark(self):
        # ``_analysis_held`` on its own: busy mark 0, deferred mark 5, now 6, limit 5 -> measured from 0 (min)
        env = DirectEnv(current_tasks, {"limit": 5.0, "judge": ["held_deferred"]})
        env.now = 6.0
        env.run._record["worker_analysis_request"] = {"status": "deferred"}
        wait = current_tasks._AnalysisWait()
        wait.busy_since, wait.deferred_since = 0.0, 5.0
        module, saved = current_tasks, current_tasks.time
        module.time = FakeTime(env)
        try:
            result = env.flow._analysis_held(env.task, env.run, WorkflowHeld("x"), wait)
        finally:
            module.time = saved
        self.assertIs(result, current_tasks._FOLLOW_ENDED)
        self.assertEqual((wait.busy_since, wait.deferred_since), (0.0, 5.0), "existing marks are kept")
        self.assertEqual(names({"trace": env.trace}, "unavailable"),
                         [("unavailable", True, True, "worker_not_idle_for_analysis", None)])
        # a fresh wait takes now for both marks and asks again
        env2 = DirectEnv(current_tasks, {"limit": 5.0})
        env2.now = 6.0
        env2.run._record["worker_analysis_request"] = {"status": "deferred"}
        fresh = current_tasks._AnalysisWait()
        current_tasks.time = FakeTime(env2)
        try:
            again = env2.flow._analysis_held(env2.task, env2.run, WorkflowHeld("x"), fresh)
        finally:
            current_tasks.time = saved
        self.assertIs(again, current_tasks._JUDGE_AGAIN)
        self.assertEqual((fresh.busy_since, fresh.deferred_since), (6.0, 6.0))

    def test_a_cancel_is_ended_before_any_pause_or_hold_is_looked_at(self):
        outcome = self.run_current("cancel_beats_pause")
        self.assertEqual(outcome["outcome"], FOLLOW)
        self.assertEqual([e[0] for e in outcome["trace"]], ["wait", "cancelled", "end_cancelled"])
        stopped = self.run_current("stopped_at_the_first_wait")
        self.assertEqual([e[0] for e in stopped["trace"]], ["wait"])
        self.assertEqual(stopped["outcome"], FOLLOW)
        self.assertEqual(self.run_current("stopped_after_a_poll_wait_returns_false")["outcome"], FOLLOW)

    def test_exceptions_of_the_handlers_escape_and_are_not_caught_by_the_sibling_handlers(self):
        for name in ("finish_run_raises_in_the_held_handler", "unavailable_raises_in_its_handler",
                     "run_error_raises_in_the_generic_handler"):
            with self.subTest(scenario=name):
                outcome = self.run_current(name)
                self.assertEqual(outcome["outcome"][:2], ("raised", "ValueError"))
        generic = self.run_current("run_error_raises_in_the_generic_handler")
        self.assertEqual(len(names(generic, "run_error")), 1, "the failing handler is not entered twice")
        self.assertEqual(names(generic, "finish"), [])


# =================================================================================================== LAYER B
if fx is not None:
    class _FlowRun(fx.ScriptRun):
        """The whole-flow script run with extra held steps (the request recorded before the WorkflowHeld)."""

        def behave(self, step, on_report):
            if step in REQUEST_STEPS:
                self._record["worker_analysis_request"] = copy.deepcopy(REQUEST_STEPS[step])
                raise WorkflowHeld(f"analysis {step}")
            if step == "held_missing":
                raise WorkflowHeld("analysis missing")
            return super().behave(step, on_report)

    class _FlowRunProp(_FlowRun):
        @property
        def worker_analysis_request(self):
            value = self._record.get("worker_analysis_request")
            return value if isinstance(value, dict) else None

        def record_snapshot(self):
            return dict(self._record)

    def _flow_scenarios() -> dict[str, tuple]:
        finishes, flag_at_gate = fx.finishes, fx.flag_at_gate
        wanted = ("judge_busy_then_ready", "judge_busy_for_too_long", "judge_deferred_then_ready",
                  "judge_deferred_for_too_long", "judge_busy_then_deferred_then_ready", "judge_unavailable",
                  "judge_held_by_workflow", "judge_error_before_report", "judge_error_after_report_accepted",
                  "judge_error_after_report_unknown", "judge_returns_none", "paused_while_waiting_to_judge",
                  "worker_hold_while_waiting_to_judge", "cancel_while_waiting_for_the_worker",
                  "report_hold_is_offered_to_the_judge", "report_deferred_manager_hold", "accepted_report",
                  "judge_with_plain_signature", "judge_with_report_hook_only", "cancel_while_judging")
        scenarios = {name: fx.FLOW_SCENARIOS[name] for name in wanted}
        scenarios.update({
            "held_without_a_request": ({"judge": ["held_missing"]}, finishes(status="held")),
            "held_with_a_sent_request": ({"judge": ["held_sent"]}, finishes(status="held")),
            "held_with_a_falsy_non_dict_request": ({"judge": ["held_falsy_list"]}, finishes(status="held")),
            "held_with_a_none_request": ({"judge": ["held_none_value"]}, finishes(status="held")),
            "deferred_three_times_then_ready": ({"judge": ["held_deferred"] * 3 + ["accepted"]}, finishes()),
            "deferred_with_extra_request_fields": ({"judge": ["held_deferred_extra", "accepted"]}, finishes()),
            "deferred_then_error": ({"judge": ["held_deferred", "error"]}, finishes()),
            "deferred_then_unavailable": ({"judge": ["held_deferred", "unavailable"], "stop": {}}, finishes()),
            "deferred_then_held_other": ({"judge": ["held_deferred", "held_sent"]}, finishes(status="held")),
            "deferred_then_busy_then_ready": ({"judge": ["held_deferred", "accepted"], "idle_worker": [True, False, True],
                                               "timed": True}, finishes()),
            "deferred_then_paused_then_ready": (
                {"judge": ["held_deferred", "accepted"], "gates": ["judge1"]},
                flag_at_gate("judge1", fx.set_paused, fx.clear_paused, "paused")),
            "deferred_then_worker_hold_then_ready": (
                {"judge": ["held_deferred", "accepted"], "gates": ["judge1"]},
                flag_at_gate("judge1", fx.set_hold("worker", "worker_maintenance"), fx.clear_hold("worker"),
                             "worker_maintenance")),
            "busy_too_long_after_a_deferral": ({"judge": ["held_deferred"], "idle_worker": [True, False],
                                                "idle_limit": 0.15, "timed": True}, finishes()),
            "cancel_while_deferred": ({"gates": ["judge1"], "judge": ["held_deferred", "accepted"]},
                                      fx.run_and_cancel_at("judge1")),
        })
        return scenarios

    FLOW_JUDGE_SCENARIOS = _flow_scenarios()
else:  # pragma: no cover
    FLOW_JUDGE_SCENARIOS = {}
    _FlowRun = _FlowRunProp = None


def run_flow_with(run_class, tasks_module, scenario) -> dict:
    saved = fx.ScriptRun
    fx.ScriptRun = run_class  # ``Workflow._begin`` looks the class up in the harness module
    try:
        outcome = fx.run_flow(tasks_module, current_terminal, scenario)
    finally:
        fx.ScriptRun = saved
    if scenario[0].get("timed") or scenario[0].get("idle_limit"):  # the number of poll passes follows the clock
        outcome["ledger"] = fx.compress(list(outcome["ledger"]))
    return outcome


@needs_flow_harness
class FlowDifferential(NoLeak):
    """Layer B: the real runner thread, with lock ownership / Task status per event and every record written."""

    @needs_baseline
    def test_every_scenario_is_identical_before_and_after_the_split(self):
        for name, scenario in FLOW_JUDGE_SCENARIOS.items():
            for label, run_class in (("duck", _FlowRun), ("property", _FlowRunProp)):
                with self.subTest(scenario=name, run=label):
                    before = run_flow_with(run_class, BASE_TASKS, scenario)
                    after = run_flow_with(run_class, current_tasks, scenario)
                    self.assertEqual(after, before)

    def test_the_scenarios_repeat_exactly_on_the_same_version(self):
        for name in ("judge_deferred_then_ready", "deferred_then_paused_then_ready", "held_without_a_request"):
            with self.subTest(scenario=name):
                scenario = FLOW_JUDGE_SCENARIOS[name]
                self.assertEqual(run_flow_with(_FlowRunProp, current_tasks, scenario),
                                 run_flow_with(_FlowRunProp, current_tasks, scenario))

    def test_the_runner_pins(self):
        deferred = run_flow_with(_FlowRunProp, current_tasks, FLOW_JUDGE_SCENARIOS["deferred_three_times_then_ready"])
        self.assertEqual(fx.runner(deferred, "run.judge"), ["run.judge"] * 4)
        self.assertIn("worker_busy", fx.held_reasons(deferred))
        self.assertEqual(fx.statuses(deferred)[-1], "finished")
        held = run_flow_with(_FlowRunProp, current_tasks, FLOW_JUDGE_SCENARIOS["held_without_a_request"])
        task = next(iter(held["tasks"].values()))
        self.assertEqual((task["status"], task["last_result"]["outcome"]), ("held", "held"))
        self.assertEqual(fx.runner(held, "run.judge"), ["run.judge"], "a plain hold is never asked again")
        gave_up = run_flow_with(_FlowRunProp, current_tasks, FLOW_JUDGE_SCENARIOS["judge_deferred_for_too_long"])
        task = next(iter(gave_up["tasks"].values()))
        self.assertEqual(task["held"], "judgment_unavailable:worker_not_idle_for_analysis")
        self.assertEqual(task["last_result"]["outcome"], "judgment_unavailable")
        cancelled = run_flow_with(_FlowRunProp, current_tasks,
                                  FLOW_JUDGE_SCENARIOS["cancel_while_waiting_for_the_worker"])
        self.assertNotIn("run.judge", fx.runner(cancelled))
        self.assertEqual(fx.statuses(cancelled)[-1], "closed")


# =================================================================================================== LAYER C
RUN_ID, TASK_ID, MESSAGE_ID, SESSION_ID = ("aaaaaaaa-0000-4000-8000-%012d" % n for n in range(1, 5))


def _revision_id(task_id: str) -> str:
    from uuid import UUID, uuid5
    return str(uuid5(UUID(task_id), "task-spec-revision:1"))


class Repo:
    def __init__(self, message):
        self.message = message

    def get_message(self, _message_id):
        return copy.deepcopy(self.message)


def exited_run(record_extra: dict | None = None):
    """A ``WorkflowRun`` whose collected record is complete and exited (the binding's happy path)."""
    content = {"task_id": TASK_ID, "revision": 1, "run_id": RUN_ID, "revision_id": _revision_id(TASK_ID),
               "sender_role": "manager", "target_role": "worker", "kind": "task",
               "target_session_id": SESSION_ID, "target_session_generation": 3}
    message = {"message_id": MESSAGE_ID, "task_id": TASK_ID, "revision": 1, "run_id": RUN_ID, "content": content}
    run = object.__new__(WorkflowRun)
    run.repository = Repo(message)
    run.task_id, run.revision, run.run_id, run.task_message_id = TASK_ID, 1, RUN_ID, MESSAGE_ID
    run._record = {"task_id": TASK_ID, "revision": 1, "run_id": RUN_ID, "shell_state": "exited",
                   "exit_confirmed": True, "exit_status": 0, **(record_extra or {})}
    run._terminal = True
    run._observed = {"sent", "ended"}
    lifecycle = {"control_returned": True, "input_returned": True, "lifetime": "ended"}
    run.shell = SimpleNamespace(snapshot=lambda: {"lifecycle": lifecycle})
    return run


AUTHORITY = {"portVersion": 2, "kind": "AutomationState", "payload": {
    "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}}


def bind_and_collect(binding_module, collect_script: str, extra: dict | None = None, **overrides):
    run = exited_run(copy.deepcopy(extra))
    for key, value in overrides.items():
        setattr(run, key, value)
    record = run._record

    def collect(**_kwargs):
        if collect_script == "copy":
            return dict(record)
        if collect_script == "live":
            return record
        if collect_script == "deep":
            return copy.deepcopy(record)
        if collect_script == "non_dict":
            return ["not", "a", "dict"]
        if collect_script == "changed_key_after":
            snapshot = dict(record)
            record["late"] = True
            return snapshot
        if collect_script == "changed_value_after":
            snapshot = dict(record)
            record["exit_status"] = 1
            return snapshot
        if collect_script == "removed_key_after":
            snapshot = dict(record)
            record.pop("exit_confirmed")
            return snapshot
        if collect_script == "nested_changed_after":
            snapshot = copy.deepcopy(record)
            record["raw_log_status"]["observed_bytes"] += 1
            return snapshot
        if collect_script == "nested_shared_changed_after":
            snapshot = dict(record)  # shallow: the nested dict is shared, so a later nested change is not a change
            record["raw_log_status"]["observed_bytes"] += 1
            return snapshot
        if collect_script == "extra_key_in_returned":
            snapshot = dict(record)
            snapshot["extra"] = 1
            return snapshot
        if collect_script == "equal_but_reordered":
            return dict(reversed(list(record.items())))
        if collect_script == "float_vs_int":
            snapshot = dict(record)
            snapshot["exit_status"] = 0.0 if snapshot["exit_status"] == 0 else snapshot["exit_status"]
            return snapshot
        raise AssertionError(collect_script)

    run.collect = collect
    binding = binding_module.WorkflowObservationBinding.attach(run)
    exits: list[dict] = []
    scheduler = WorkerReviewScheduler(
        admission=SerializedReviewAdmission(None, AUTHORITY), automation_state=lambda: AUTHORITY,
        active_run=lambda: binding.run, worker_state=lambda _: None, collect_non_model=lambda _: {},
        dispatch_review=lambda _: {"status": "omp_processed"}, user_priority=lambda _: False,
        on_exit=lambda event: exits.append(dict(event)), clock=lambda: 0)
    source = scheduler.activate_run_source(binding.run, run)
    if source is None or not binding.bind(scheduler, source):
        raise AssertionError("the fixture could not bind")
    try:
        record_out, notified = binding.collect_and_notify(scheduler)
    finally:
        scheduler.close() if hasattr(scheduler, "close") else None
    return {"record": record_out, "notified": notified, "exits": exits, "run_record": dict(run._record)}


BINDING_SCENARIOS = {
    "unchanged_copy": ("copy", None),
    "live_alias": ("live", None),
    "deep_copy_equal": ("deep", {"raw_log_status": {"observed_bytes": 5}, "unknowns": []}),
    "non_dict_return": ("non_dict", None),
    "key_added_after_collect": ("changed_key_after", None),
    "value_changed_after_collect": ("changed_value_after", None),
    "key_removed_after_collect": ("removed_key_after", None),
    "nested_value_changed_after_collect": ("nested_changed_after", {"raw_log_status": {"observed_bytes": 5}}),
    "nested_shared_dict_changed_after_collect": ("nested_shared_changed_after",
                                                 {"raw_log_status": {"observed_bytes": 5}}),
    "returned_has_an_extra_key": ("extra_key_in_returned", None),
    "equal_but_reordered": ("equal_but_reordered", None),
    "int_zero_equals_float_zero": ("float_vs_int", None),
}


class BindingDifferential(NoLeak):
    @needs_baseline
    def test_every_scenario_is_identical_before_and_after_the_change(self):
        for name, (script, extra) in BINDING_SCENARIOS.items():
            with self.subTest(scenario=name):
                before = bind_and_collect(BASE_BINDING, script, extra)
                after = bind_and_collect(current_binding, script, extra)
                self.assertEqual(after, before)

    @needs_baseline
    def test_not_terminal_and_other_guards_are_identical(self):
        for override in ({"_terminal": False}, {"_observed": {"sent"}}):
            with self.subTest(override=sorted(override)):
                self.assertEqual(bind_and_collect(current_binding, "copy", None, **override),
                                 bind_and_collect(BASE_BINDING, "copy", None, **override))

    def test_a_changed_record_is_not_an_exit_and_an_unchanged_one_is(self):
        unchanged = bind_and_collect(current_binding, "copy")
        self.assertTrue(unchanged["notified"])
        self.assertEqual(len(unchanged["exits"]), 1)
        self.assertTrue(bind_and_collect(current_binding, "live")["notified"], "the live dict equals itself")
        self.assertTrue(bind_and_collect(current_binding, "deep")["notified"], "equality is by value")
        for script in ("changed_key_after", "changed_value_after", "removed_key_after", "extra_key_in_returned"):
            with self.subTest(script=script):
                changed = bind_and_collect(current_binding, script)
                self.assertFalse(changed["notified"])
                self.assertEqual(changed["exits"], [])
        nested = bind_and_collect(current_binding, "nested_changed_after", {"raw_log_status": {"observed_bytes": 1}})
        self.assertFalse(nested["notified"], "a nested change of the live record is a change")
        shared = bind_and_collect(current_binding, "nested_shared_changed_after",
                                  {"raw_log_status": {"observed_bytes": 1}})
        self.assertTrue(shared["notified"], "the copy is shallow, exactly like dict(record)")
        non_dict = bind_and_collect(current_binding, "non_dict")
        self.assertEqual((non_dict["record"], non_dict["notified"], non_dict["exits"]), ({}, False, []))


def stub_automation(workflow_run, *, wall=1000.0):
    return SimpleNamespace(_lock=threading.Lock(), _bound=SimpleNamespace(workflow_run=workflow_run),
                           _wall=lambda: wall)


class SnapshotRun:
    """What ``_collect_non_model`` reads: the record (live or as a snapshot), ``shell`` and ``raw_log``."""

    def __init__(self, record: dict, raw_log, shell_script: str, owner_is_real: bool):
        self._record = record
        self.raw_log = raw_log
        self.shell_script = shell_script
        self.snapshots = 0
        self.shell = SimpleNamespace(snapshot=self._shell_snapshot)
        if not owner_is_real:
            self.record_snapshot = None  # baseline: only the private record is read
        self.real = owner_is_real

    def _shell_snapshot(self):
        self.snapshots += 1
        if self.shell_script == "raises":
            raise RuntimeError("shell gone")
        if self.shell_script == "mutates_record":  # the runner thread updates the record while we read the shell
            self._record["shell_state"] = "exited"
            self._record["exit_confirmed"] = True
            self._record["exit_status"] = 9
            self._record.setdefault("unknowns", []).append("late")
        if self.shell_script == "none":
            return None
        if self.shell_script == "lifecycle_none":
            return {"lifecycle": None}
        return {"lifecycle": {"lifetime": "alive" if self.shell_script == "alive" else "ended"}}

    def record_snapshot(self):  # replaced by None for the baseline in __init__
        return dict(self._record)


NON_MODEL_SCENARIOS: dict[str, dict] = {
    "segmented_running": {"record": {"shell_state": "running", "log_format": "segmented-v1",
                                     "raw_log_status": {"observed_bytes": 12, "last_observed_at": 990.0}}},
    "segmented_exited_confirmed": {"record": {"shell_state": "exited", "exit_confirmed": True, "exit_status": 0,
                                              "log_format": "segmented-v1", "raw_log_status": {
                                                  "observed_bytes": 3, "last_observed_at": 1001.0}}},
    "segmented_unknown_observation": {"record": {"shell_state": "running", "log_format": "segmented-v1",
                                                 "raw_log_status": {}, "unknowns": ["a", 3, None, "b"]}},
    "segmented_raw_status_missing": {"record": {"shell_state": "sent", "log_format": "segmented-v1"}},
    "legacy_log_missing": {"record": {"shell_state": "sent"}, "raw_log": "/nonexistent-wb-judge-test/raw.log"},
    "legacy_log_present": {"record": {"shell_state": "exited", "exit_confirmed": True, "exit_status": 2},
                           "raw_log": "present"},
    "exit_status_not_int": {"record": {"shell_state": "exited", "exit_confirmed": True, "exit_status": "2"},
                            "raw_log": "present"},
    "exit_status_bool": {"record": {"shell_state": "exited", "exit_confirmed": True, "exit_status": True},
                         "raw_log": "present"},
    "exit_unconfirmed": {"record": {"shell_state": "exited", "exit_confirmed": False}, "raw_log": "present"},
    "unknown_state": {"record": {"shell_state": "unknown", "unknowns": ["x"]}, "raw_log": "present"},
    "shell_snapshot_raises": {"record": {"shell_state": "running"}, "raw_log": "present", "shell": "raises"},
    "shell_snapshot_none": {"record": {"shell_state": "running"}, "raw_log": "present", "shell": "none"},
    "shell_lifecycle_none": {"record": {"shell_state": "running"}, "raw_log": "present", "shell": "lifecycle_none"},
    "shell_alive": {"record": {"shell_state": "running"}, "raw_log": "present", "shell": "alive"},
    "record_changes_during_the_shell_read": {"record": {"shell_state": "running", "unknowns": ["first"]},
                                             "raw_log": "present", "shell": "mutates_record"},
    "empty_record": {"record": {}, "raw_log": "present"},
}


def non_model(module, case: dict, tmp: Path, real_run: bool) -> dict:
    raw = case.get("raw_log")
    if raw == "present":
        raw = tmp / "raw.log"
        raw.write_bytes(b"1234567")
        os.utime(raw, (990.0, 990.0))
    record = copy.deepcopy(case["record"])
    if real_run:
        run = object.__new__(WorkflowRun)
        run._record = record
        run.raw_log = Path(raw) if raw else raw
        helper = SnapshotRun(record, run.raw_log, case.get("shell", "ended"), True)
        run.shell = helper.shell
    else:
        run = SnapshotRun(record, Path(raw) if raw else raw, case.get("shell", "ended"), False)
    stub = stub_automation(run)
    facts = module.AutomationController._collect_non_model(stub, None)
    return {"facts": facts, "record": record}


class AutomationNonModelDifferential(unittest.TestCase):
    @needs_baseline
    def test_every_scenario_is_identical_before_and_after_the_change(self):
        for name, case in NON_MODEL_SCENARIOS.items():
            with self.subTest(scenario=name), tempfile.TemporaryDirectory(prefix="judgesnap-") as tmp:
                before = non_model(BASE_AUTOMATION, case, Path(tmp), real_run=False)
                after = non_model(current_automation, case, Path(tmp), real_run=True)
                self.assertEqual(after, before)

    def test_no_bound_run_is_unknown(self):
        for bound in (None, SimpleNamespace(workflow_run=None)):
            for module in (current_automation, BASE_AUTOMATION):
                if module is None:
                    continue
                stub = SimpleNamespace(_lock=threading.Lock(), _bound=bound, _wall=lambda: 0.0)
                self.assertEqual(module.AutomationController._collect_non_model(stub, None),
                                 {"phase": "unknown", "unknowns": ["no_host_run"]})

    def test_the_facts_never_alias_the_run_record(self):
        record = {"shell_state": "exited", "exit_confirmed": True, "exit_status": 0, "unknowns": ["a"],
                  "log_format": "segmented-v1", "raw_log_status": {"observed_bytes": 3, "last_observed_at": 1.0}}
        run = object.__new__(WorkflowRun)
        run._record = record
        run.shell = SimpleNamespace(snapshot=lambda: {"lifecycle": {"lifetime": "ended"}})
        facts = current_automation.AutomationController._collect_non_model(stub_automation(run), None)
        self.assertEqual(facts["unknowns"], ["a"])
        self.assertIsNot(facts["unknowns"], record["unknowns"])
        facts["unknowns"].append("zzz")
        facts["phase"] = "changed"
        self.assertEqual(record["unknowns"], ["a"])
        self.assertEqual(record["shell_state"], "exited")

    def test_the_record_is_read_before_the_shell_snapshot_and_is_not_the_live_dict(self):
        # a record update that lands while the shell is read must not show up in the facts of this call
        case = NON_MODEL_SCENARIOS["record_changes_during_the_shell_read"]
        with tempfile.TemporaryDirectory(prefix="judgesnap-") as tmp:
            facts = non_model(current_automation, case, Path(tmp), real_run=True)["facts"]
        self.assertEqual(facts["phase"], "running")
        self.assertIs(facts["exit_confirmed"], False)
        self.assertIsNone(facts["exit_status"], "the exit status 9 written during the shell read is not seen")
        self.assertEqual(facts["unknowns"], ["first", "late"], "the copy is shallow: the nested list is shared")


# =================================================================================================== LAYER D
class AccessorPins(unittest.TestCase):
    """Pins of the new ``WorkflowRun`` accessors and the flow helper, written from the contract."""

    @staticmethod
    def run_with(record: dict) -> WorkflowRun:
        run = object.__new__(WorkflowRun)
        run._record = record
        return run

    def test_worker_analysis_request_is_the_stored_value_itself(self):
        request = {"status": "deferred", "message_id": "m"}
        run = self.run_with({"worker_analysis_request": request})
        self.assertIs(run.worker_analysis_request, request, "no copy: the live dict")
        for record in ({}, {"worker_analysis_request": None}):
            with self.subTest(record=record):
                self.assertIsNone(self.run_with(record).worker_analysis_request)
        for value in ([], "deferred", 0, ["status"], 7, "", True):
            with self.subTest(value=value):
                got = self.run_with({"worker_analysis_request": value}).worker_analysis_request
                self.assertIs(got, value, "a non-dict is returned as stored, not turned into None")
        empty = {}
        self.assertIs(self.run_with({"worker_analysis_request": empty}).worker_analysis_request, empty,
                      "an empty dict is still the stored dict (the caller applies ``or {}``)")

    def test_the_property_follows_later_changes_of_the_record(self):
        run = self.run_with({})
        self.assertIsNone(run.worker_analysis_request)
        run._record["worker_analysis_request"] = {"status": "sent"}
        self.assertEqual(run.worker_analysis_request, {"status": "sent"})

    def test_record_snapshot_is_a_shallow_copy_of_the_current_record(self):
        nested = {"observed_bytes": 1}
        record = {"b": 1, "a": 2, "raw_log_status": nested}
        run = self.run_with(record)
        snapshot = run.record_snapshot()
        self.assertEqual(snapshot, record)
        self.assertIsNot(snapshot, record)
        self.assertEqual(list(snapshot), list(record), "insertion order is kept")
        self.assertIs(snapshot["raw_log_status"], nested, "shallow: nested values are shared like ``dict(record)``")
        self.assertIs(type(snapshot), dict)
        # mutating the copy never reaches the run; mutating the run afterwards never reaches an earlier copy
        snapshot["x"] = 1
        snapshot.pop("a")
        self.assertEqual(record, {"b": 1, "a": 2, "raw_log_status": nested})
        record["late"] = True
        self.assertNotIn("late", snapshot)
        again = run.record_snapshot()
        self.assertIsNot(again, snapshot)
        self.assertIn("late", again)

    def test_the_helper_reads_the_property_once_and_falls_back_to_the_record_on_attribute_error(self):
        helper = current_tasks._worker_analysis_request
        duck = SimpleNamespace(_record={"worker_analysis_request": {"status": "deferred"}})
        self.assertEqual(helper(duck), {"status": "deferred"})
        self.assertIsNone(helper(SimpleNamespace(_record={})))
        prop_only = SimpleNamespace(worker_analysis_request={"status": "sent"}, _record={})
        self.assertEqual(helper(prop_only), {"status": "sent"}, "the accessor wins over a private record")
        real = self.run_with({"worker_analysis_request": {"status": "deferred"}})
        self.assertEqual(helper(real), {"status": "deferred"})
        none_prop = SimpleNamespace(worker_analysis_request=None, _record={"worker_analysis_request": {"s": 1}})
        self.assertIsNone(helper(none_prop), "a property that says None is not overridden by the record")
        self.assertEqual(helper(self.run_with({"worker_analysis_request": "deferred"})), "deferred",
                         "a non-dict is passed on raw")

        class Counting:
            reads = 0
            _record = {"worker_analysis_request": {"from": "record"}}

            @property
            def worker_analysis_request(self):
                type(self).reads += 1
                return {"from": "property"}

        self.assertEqual(helper(Counting()), {"from": "property"})
        self.assertEqual(Counting.reads, 1, "the property is evaluated exactly once")

        class Missing:
            reads = 0
            _record = {"worker_analysis_request": {"from": "record"}}

            @property
            def worker_analysis_request(self):
                type(self).reads += 1
                raise AttributeError("no such thing")

        self.assertEqual(helper(Missing()), {"from": "record"}, "an AttributeError of the property falls back")
        self.assertEqual(Missing.reads, 1)

        class Broken:
            _record = {}

            @property
            def worker_analysis_request(self):
                raise RuntimeError("property failed")

        with self.assertRaises(RuntimeError):
            helper(Broken())

    def test_the_wait_state_starts_empty_and_reset_clears_both_marks(self):
        wait = current_tasks._AnalysisWait()
        self.assertEqual((wait.busy_since, wait.deferred_since), (None, None))
        wait.busy_since, wait.deferred_since = 1.0, 2.0
        wait.reset()
        self.assertEqual((wait.busy_since, wait.deferred_since), (None, None))
        with self.assertRaises(AttributeError):
            wait.other = 1  # slots: no stray state

    def test_the_sentinels_are_distinct_and_private(self):
        self.assertIsNot(current_tasks._JUDGE_AGAIN, current_tasks._FOLLOW_ENDED)
        self.assertIsNot(current_tasks._JUDGE_AGAIN, None)

    def test_the_flow_never_leaks_the_again_sentinel_to_its_caller(self):
        for name, spec in DIRECT_SCENARIOS.items():
            with self.subTest(scenario=name):
                result = run_direct(current_tasks, spec)
                self.assertNotEqual(result["outcome"], ("returned", "JUDGE_AGAIN_LEAKED"))

    @needs_baseline
    def test_only_judge_when_ready_changed_in_flow_tasks_and_only_the_helpers_were_added(self):
        import ast

        def functions(source: str) -> dict[str, str]:
            found: dict[str, str] = {}

            def visit(node, prefix):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        qualified = prefix + child.name
                        found[qualified] = ast.dump(child) if not isinstance(child, ast.ClassDef) else ""
                        visit(child, qualified + ".")
                    else:
                        visit(child, prefix)

            visit(ast.parse(source), "")
            return found

        before = functions(_git_show("src/workbench/backend/flow_tasks.py"))
        after = functions((REPO / "src/workbench/backend/flow_tasks.py").read_text())
        self.assertEqual(set(before) - set(after), set())
        self.assertEqual({name for name in before if before[name] != after[name]}, {"TaskFlow._judge_when_ready"})
        self.assertEqual(set(after) - set(before), {
            "_AnalysisWait", "_AnalysisWait.__init__", "_AnalysisWait.reset", "_worker_analysis_request",
            "TaskFlow._wait_out_hold", "TaskFlow._busy_past_limit", "TaskFlow._judge_once", "TaskFlow._analysis_held"})


if __name__ == "__main__":
    unittest.main()
