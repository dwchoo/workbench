"""Fixed product Q01 port, bound to an actually prepared SDK transport.

Development owners may supply the owned runtime object in-process. JSON cannot
encode that live transport. No development collector, fixture or old verdict is
imported here, and absence of a transport never implies compatibility.
"""
from __future__ import annotations

from typing import Any, Protocol


class ProductSummaryRuntime(Protocol):
    def prepare_route(self, context: dict[str, Any]) -> dict[str, Any]: ...
    def collect_observations(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


def prepare_q01_context(context: dict[str, Any]) -> dict[str, Any]:
    runtime = context.get("product_runtime")
    if not all(callable(getattr(runtime, name, None)) for name in
               ("prepare_route", "collect_observations", "close")):
        raise RuntimeError("product_runtime_transport_unavailable")
    try:
        identity = runtime.prepare_route(context)
        if not isinstance(identity, dict) or identity.get("source") != "product_adapter_readback":
            raise RuntimeError("product_route_readback_unavailable")
        return {"route_identity": identity, "runtime": runtime}
    except BaseException:
        runtime.close()
        raise


def collect_q01_observations(prepared: dict[str, Any]) -> dict[str, Any]:
    # The transport owns actual request/event collection, not a list of facts
    # accepted from the model or the JSON invocation context.
    result = prepared["runtime"].collect_observations()
    if (not isinstance(result, dict) or not isinstance(result.get("observations"), list)
            or not isinstance(result.get("unknowns"), list)):
        raise RuntimeError("product_runtime_observations_unavailable")
    return result


def close_q01_context(prepared: dict[str, Any]) -> None:
    runtime = prepared.pop("runtime", None)
    if runtime is not None:
        runtime.close()
