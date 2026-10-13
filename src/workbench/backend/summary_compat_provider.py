"""Product verification capability, separate from development probe verdicts.

LG-D28 permits owned private OMP startup/catalog preparation. The verifier-only
SDK creation seam reuses its native private registry and disables child power
assertions; this alone does not establish complete observer/child I/O evidence.
This provider remains unavailable, and never copies development capability
observations into eight product passes or operating model metadata.
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
