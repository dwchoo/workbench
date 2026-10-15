"""Explicit owned scripted capability execution, never an operating MODEL pass."""
from __future__ import annotations

from pathlib import Path
import time
import subprocess
from typing import Any

from workbench.backend.summary_compat_runtime import Q01_ITEMS, observe_owned
from workbench.backend.summary_compat_priority import observe_priority


class ScriptedCompatibilityProvider:
    def observe(self) -> dict[str, Any]:
        started = time.monotonic()
        source = Path(__file__).resolve().parents[3]
        result = observe_owned(source, recovery_control=True, pause_control=True, restart_control=True)
        # Preserve the prior execution and do not start another owned group when
        # its runtime was unavailable or cleanup remains ambiguous.
        if result["level"] != "inconclusive" or not result["observations"] or any(
                row.get("cleanup_complete") is not True for row in result["observations"]):
            return result
        try:
            priority = observe_priority(source, budget_seconds=150 - (time.monotonic() - started))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
            priority = {"kind": "owned_product_priority_partial", "result": "inconclusive",
                        "coverage_complete": False, "errors": [type(error).__name__ + ":" + str(error)]}
        result["observations"].append(priority)
        result["level"] = "inconclusive"
        # Named local POSTs live in each observation. Complete native I/O is
        # unattributed; the prior RPC count alone would be misleading here.
        result["io"]["model_calls"] = None
        result["io"]["omp_children"] = None
        result["unknowns"].extend(["priority_operating_io_unattributed", "priority_Backend_TaskFlow_unverified"])
        result["unknowns"].extend(priority.get("errors", []))
        native_launches = priority.get("native_launches")
        launched = type(native_launches) is int and native_launches > 0
        parents = priority.get("parents") if isinstance(priority.get("parents"), dict) else {}
        parent_rows = [row for row in parents.values() if isinstance(row, dict)]
        parent_complete = bool(parent_rows) and len(parent_rows) == len(parents) and all(
            isinstance(row.get("before"), dict) and isinstance(row.get("after"), dict)
            for row in parent_rows)

        def parent_check(field: str, expected: bool) -> bool | None:
            violating = any(row.get(field) is not expected for row in parent_rows
                            if isinstance(row.get("before"), dict)
                            and isinstance(row.get("after"), dict)
                            and type(row.get(field)) is bool)
            if violating:
                return False
            if not parent_complete or any(type(row.get(field)) is not bool for row in parent_rows):
                return None
            return True

        parent_violations = priority.get("provider_violations")
        exact_parent_violation = (isinstance(parent_violations, list)
                                  and "ValueError:parent_tools_or_upper_contamination" in parent_violations)
        priority_failed = (priority.get("result") == "failed"
                           or parent_check("strict_parent_unchanged", True) is False
                           or parent_check("raw_parent_contamination", False) is False
                           or parent_check("prior_entry_views_unchanged", True) is False
                           or exact_parent_violation)
        priority_row_status = "failed" if priority_failed else ("inconclusive" if launched else "not_run")
        if priority_row_status != "not_run":
            result["items"]["Q01-priority"] = priority_row_status
        priority_index = len(result["observations"]) - 1
        result.setdefault("item_rows", [])
        result["item_rows"] = [row for row in result["item_rows"] if row.get("item") != "Q01-priority"]
        result["item_rows"].append({"item": "Q01-priority", "result": priority_row_status,
            "checks": {"priority_native_launch_observed": launched if type(native_launches) is int else None,
                       "priority_coverage_complete": priority.get("coverage_complete")
                       if type(priority.get("coverage_complete")) is bool else None,
                       "priority_cleanup_complete": priority.get("cleanup_complete")
                       if type(priority.get("cleanup_complete")) is bool else None,
                       "strict_parent_unchanged": parent_check("strict_parent_unchanged", True),
                       "raw_parent_contamination_absent": parent_check("raw_parent_contamination", False),
                       "prior_entry_views_unchanged": parent_check("prior_entry_views_unchanged", True),
                       "parent_tools_or_upper_contamination_absent": (
                           not exact_parent_violation if isinstance(parent_violations, list) else None)},
            "unknowns": ["priority_contract_unverified"] if launched else ["priority_scenarios_not_run"],
            "evidence_refs": [{"observation_index": priority_index, "kind": priority.get("kind")} ]})
        result["unknowns"].extend(unknown for row in result["item_rows"]
                                  for unknown in row.get("unknowns", []) if isinstance(unknown, str))
        return result
