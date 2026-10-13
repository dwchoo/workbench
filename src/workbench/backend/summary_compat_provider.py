"""Explicit owned scripted capability execution, never an operating MODEL pass."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from workbench.backend.summary_compat_runtime import observe_owned


class ScriptedCompatibilityProvider:
    def observe(self) -> dict[str, Any]:
        return observe_owned(Path(__file__).resolve().parents[3], recovery_control=True, pause_control=True)
