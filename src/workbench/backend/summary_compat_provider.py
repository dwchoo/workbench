"""Owned installed-SDK controls and exact per-item evidence; no model prompt outside loopback."""
from hashlib import sha256
from pathlib import Path
import json
import os
import time
import subprocess
from typing import Any

from workbench.assets import runtime_root
from workbench.backend.summary_compat_runtime import Q01_ITEMS, REQUIRED_CHECKS, _item_rows, observe_owned
from workbench.backend.summary_compat_priority import observe_priority
from workbench.backend.summary_compat_identity import observe_configured
from workbench.contracts.logs_v1 import canonical_json

IO_CAVEATS = {"observer_child_io_unattributed", "priority_operating_io_unattributed"}


def priority_row(priority: dict, index: int) -> dict:
    parents = priority.get("parents") if isinstance(priority.get("parents"), dict) else {}
    rows = list(parents.values())
    def parent(field, expected):
        values = [row.get(field) for row in rows if isinstance(row, dict)]
        if any(type(value) is bool and value is not expected for value in values):
            return False
        return True if len(values) == 2 and all(value is expected for value in values) else None
    measurement = priority.get("measurement", {})
    ids = measurement.get("session_ids", {})
    windows = measurement.get("notice_windows", [])
    violations = priority.get("provider_violations")
    checks = {
        "priority_native_launch_observed": priority.get("native_launches") == 2 if type(priority.get("native_launches")) is int else None,
        "priority_coverage_complete": priority.get("coverage_complete"),
        "priority_cleanup_complete": priority.get("cleanup_complete"),
        "strict_parent_unchanged": parent("strict_parent_unchanged", True),
        "raw_parent_contamination_absent": parent("raw_parent_contamination", False),
        "prior_entry_views_unchanged": parent("prior_entry_views_unchanged", True),
        "parent_tools_or_upper_contamination_absent": (
            "ValueError:parent_tools_or_upper_contamination" not in violations
            if isinstance(violations, list) else None),
        "nonshared_observed_route": (len(ids) == 3 and len(set(ids.values())) == 3
            and len(windows) == 2 and all(window.get("turn_hold_overlap_ms", 0) > 0 for window in windows)),
        "actual_core_question": priority.get("Core_periodic_question", {}).get("returned_before_child_release"),
        "busy_parent_observed": (priority.get("busy_parent_during_summary", {}).get("state", {}).get("idle") is False
            and priority.get("busy_parent_during_summary", {}).get("child_still_held") is True),
        "draft_deferred": priority.get("draft_ack", {}).get("status") == "deferred" and priority.get("draft", {}).get("Enter_sent") is False,
        "due_check_and_end": (priority.get("scheduler", {}).get("tick60") == "dispatched"
            and priority.get("end_ack", {}).get("status") == "api_accepted" and measurement.get("due_scheduler_status") == "dispatched"),
        "upper_source_binding": (priority.get("prepared", {}).get("whole_range_identity_preserved") is True
            and priority.get("producer", {}).get("authority", {}).get("allowed") is True
            and priority.get("writer", {}).get("sources_matches") is True),
    }
    errors = priority.get("errors", []) + priority.get("provider_violations", [])
    failed = priority.get("result") == "failed" or any(checks.get(key) is False for key in
        ("strict_parent_unchanged", "raw_parent_contamination_absent", "prior_entry_views_unchanged",
         "parent_tools_or_upper_contamination_absent"))
    result = "failed" if failed else "pass" if not errors and all(checks.get(key) is True for key in REQUIRED_CHECKS["Q01-priority"]) else "inconclusive"
    if priority.get("native_launches") == 0:
        result = "not_run"
    return {"item": "Q01-priority", "result": result, "checks": checks,
        "unknowns": [] if result == "pass" else ["priority_controls_incomplete"],
        "evidence_refs": [{"observation_index": index, "kind": priority.get("kind"),
                           "sha256": sha256(json.dumps(priority, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"), allow_nan=False).encode()).hexdigest()}],
        "boundary": "actual owned private SDK/public Core composition and finite command; not Backend boot"}


def item_rows(observations: list) -> list:
    result = {"observations": observations}
    rows = _item_rows(result)
    for index, observation in enumerate(observations):
        if observation.get("kind") == "owned_product_priority_partial":
            rows = [row for row in rows if row["item"] != "Q01-priority"] + [priority_row(observation, index)]
    return sorted(rows, key=lambda row: Q01_ITEMS.index(row["item"]))


class ScriptedCompatibilityProvider:
    def observe(self) -> dict[str, Any]:
        started = time.monotonic()
        source = runtime_root()
        result = observe_owned(source, recovery_control=True, pause_control=True, restart_control=True, cases_control=True)
        if result["level"] != "inconclusive" or not result["observations"] or any(
                row.get("cleanup_complete") is not True for row in result["observations"]):
            return result
        completed = result["observations"][0].get("completed") or {}
        if completed.get("caseControls"):
            if result["observations"][0].get("error"):
                return result
            configured = observe_configured(source, environment=os.environ)
            result["observations"].append(configured)
            result["model_metadata"] = configured.get("metadata") or result["model_metadata"]
            result["omp"] = configured.get("omp")
            if configured.get("result") != "pass":
                result["unknowns"].append("configured_model_readback_unavailable")
        try:
            priority = observe_priority(source, budget_seconds=150 - (time.monotonic() - started))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
            priority = {"kind": "owned_product_priority_partial", "result": "inconclusive",
                        "coverage_complete": False, "errors": [type(error).__name__]}
        result["observations"].append(priority)
        result["io"]["model_calls"] = None
        result["io"]["omp_children"] = None
        result["unknowns"].append("priority_operating_io_unattributed")
        result["unknowns"].extend(priority.get("errors", []))
        result["item_rows"] = item_rows(result["observations"])
        result["items"] = {row["item"]: row["result"] for row in result["item_rows"]}
        result["unknowns"].extend(reason for row in result["item_rows"] for reason in row["unknowns"])
        result["caveats"] = sorted(set(result["unknowns"]) & IO_CAVEATS)
        result["unknowns"] = sorted(set(result["unknowns"]) - IO_CAVEATS)
        result["level"] = ("failed" if any(row["result"] == "failed" for row in result["item_rows"]) else
                           "pass" if not result["unknowns"] and all(row["result"] == "pass" for row in result["item_rows"]) else "inconclusive")
        return result
