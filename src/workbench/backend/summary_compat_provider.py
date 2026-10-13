"""Explicit owned scripted capability execution, never an operating MODEL pass."""
from __future__ import annotations

from pathlib import Path
import time
import subprocess
from typing import Any

from workbench.backend.summary_compat_runtime import observe_owned
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
        if priority.get("native_launches"):
            result["items"]["Q01-priority"] = "inconclusive"
        return result
