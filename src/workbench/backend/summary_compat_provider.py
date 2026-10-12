"""Product verification capability, separate from development probe verdicts.

No CLI child is launched until a supported credential/network-free preparation
path is established. Unavailability is an observation, never eight passes.
"""
from __future__ import annotations

from typing import Any


class ScriptedCompatibilityProvider:
    def observe(self) -> dict[str, Any]:
        return {"level": "unavailable", "observations": [],
                "model_metadata": {"status": "inconclusive", "unknowns": [
                    "operating_session_unavailable", "output_budget_unavailable"]},
                "unknowns": ["isolated_product_verifier_unavailable"],
                "io": {"omp_children": 0, "model_calls": 0, "network_requests": 0,
                       "auth_store_open": 0, "settings_writes": 0}}
