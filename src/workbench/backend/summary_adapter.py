"""Request-specific summary admission and deterministic source selection.

The SDK session lives in the existing Manager process. This module never calls
a model directly and does not schedule, retry sent input or change permissions.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from workbench.contracts.logs_v1 import (
    LogContractError, canonical_json, parse_source_range, parse_summary_request,
    parse_summary_result, summary_input_key,
)

SKILL_NAME = "important-log-summary"
COLLECTION_BYTES = 1_048_576
WIRE_BYTES = 32_768
RESULT_BYTES = 8_192
RECEIPT_BYTES = 4_096
_PRIORITY = {"start_spec": 0, "decision": 1, "report": 2, "terminal": 3}


class SummaryDeferred(RuntimeError):
    """Confirmed pre-dispatch refusal; its input has not been sent."""


class SummaryUnknown(RuntimeError):
    """Dispatch occurred or cannot be ruled out; callers must not replay."""


def skill_bytes() -> bytes:
    # Fixed shipped asset; never resolve a model-supplied skill name or path.
    return (Path(__file__).resolve().parents[3] / "omp_bridge" / "skills" /
            SKILL_NAME / "SKILL.md").read_bytes()


@dataclass(frozen=True)
class SourcePiece:
    range: dict[str, Any]
    text: str
    kind: str = "terminal"
    sequence: int = 0


def _resolved_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Require actual parent-epoch information; settings are not a substitute."""
    if not isinstance(value, Mapping) or value.get("source") != "parent_session_readback":
        raise SummaryDeferred("model_metadata_unavailable")
    result = deepcopy(dict(value))
    for key in ("epoch", "parentSessionId", "model", "thinkingLevel", "serviceTierByFamily"):
        if key not in result or result[key] is None:
            raise SummaryDeferred("model_metadata_unavailable")
    if (not isinstance(result["epoch"], str) or not result["epoch"]
            or not isinstance(result["parentSessionId"], str) or not result["parentSessionId"]
            or not isinstance(result["thinkingLevel"], str)
            or not isinstance(result["serviceTierByFamily"], dict)):
        raise SummaryDeferred("model_metadata_unavailable")
    model = result["model"]
    if not isinstance(model, dict) or any(not isinstance(model.get(key), str) or not model[key]
                                          for key in ("id", "provider", "api", "baseUrl")):
        raise SummaryDeferred("model_metadata_unavailable")
    for key in ("contextWindow", "maxTokens"):
        if type(model.get(key)) is not int or model[key] <= 0:
            raise SummaryDeferred("model_metadata_unavailable")
    for key in ("existingContextTokens", "outputCap", "reasoningTokens", "visibleResultTokens"):
        if type(result.get(key)) is not int or result[key] < 0:
            raise SummaryDeferred("output_budget_unavailable")
    cap = result["outputCap"]
    if (not 0 < cap <= min(8192, model["maxTokens"])
            or result.get("outputMeaning") != "reasoning_and_visible"
            or result.get("outputLimitEnforced") is not True
            or not isinstance(result.get("outputLimitSource"), str)
            or not result["outputLimitSource"]
            or result["visibleResultTokens"] <= 0
            or result["reasoningTokens"] + result["visibleResultTokens"] > cap):
        raise SummaryDeferred("output_budget_unavailable")
    if result.get("estimator") != "utf8_bytes_upper_bound":
        raise SummaryDeferred("input_budget_unavailable")
    return result


def prepare_summary(request: dict[str, Any], pieces: Sequence[SourcePiece], *,
                    metadata: Mapping[str, Any] | None, instructions: str,
                    prior_cumulative: dict[str, Any] | None = None) -> dict[str, Any]:
    """Choose complete ranges only; budget omissions remain explicit references."""
    item = parse_summary_request(request)
    skill = skill_bytes()
    if item["skillName"] != SKILL_NAME or item["skillDigest"] != sha256(skill).hexdigest():
        raise SummaryDeferred("skill_digest_mismatch")
    resolved = _resolved_metadata(metadata)
    if type(instructions) is not str or not instructions:
        raise SummaryDeferred("agent_definition_unavailable")
    if prior_cumulative is not None:
        if (set(prior_cumulative) != {"entryId", "body"}
                or prior_cumulative["entryId"] != item["priorCumulativeEntryId"]):
            raise LogContractError("prior cumulative identity mismatch")
        from workbench.contracts.logs_v1 import parse_important_body
        parse_important_body(prior_cumulative["body"])
    collected = 0
    checked: list[SourcePiece] = []
    expected = {canonical_json(span) for span in item["sources"]}
    seen: set[bytes] = set()
    for piece in pieces:
        span = parse_source_range(piece.range)
        raw = piece.text.encode("utf-8")
        identity = canonical_json(span)
        if (piece.kind not in _PRIORITY or type(piece.sequence) is not int
                or identity not in expected or identity in seen
                or span["end"] - span["start"] != len(raw)
                or span["digest"] != sha256(raw).hexdigest()):
            raise LogContractError("source piece identity or bytes mismatch")
        seen.add(identity)
        collected += len(raw)
        if collected > COLLECTION_BYTES:
            raise SummaryDeferred("collection_budget_exceeded")
        checked.append(piece)
    if seen != expected:
        raise LogContractError("missing source piece; use explicit source gaps")
    checked.sort(key=lambda p: (_PRIORITY[p.kind],
                               -p.sequence if p.kind == "terminal" else p.sequence,
                               canonical_json(p.range)))
    selected: list[SourcePiece] = []
    omitted: list[dict[str, Any]] = []
    limit = min(8192, resolved["model"]["contextWindow"] // 10)

    def envelope(keep: list[SourcePiece], omissions: list[dict[str, Any]]) -> dict[str, Any]:
        current = {**item, "sources": [p.range for p in keep]}
        current["inputKey"] = summary_input_key(current["subject"], current["revision"],
                                               current["sources"], current["sourceGaps"], current["skillDigest"])
        return {"request": current, "selected": [{"range": p.range, "text": p.text} for p in keep],
                "omitted": omissions, "priorCumulative": prior_cumulative,
                "resultByteLimit": RESULT_BYTES}

    def size(payload: dict[str, Any]) -> int:
        # Conservative framing/tool reserve, checked again against actual SDK
        # context before every stream call. This is not an exact tokenizer count.
        return len(canonical_json(payload)) + len(instructions.encode()) + len(skill) + 2048

    for piece in checked:
        trial = envelope([*selected, piece], omitted)
        if size(trial) <= limit:
            selected.append(piece)
        else:
            omitted.append({"range": piece.range, "reason": "input_budget",
                            "estimatedBytes": len(piece.text.encode())})
    payload = envelope(selected, omitted)
    input_estimate = size(payload)
    if (not selected and not item["sourceGaps"] or input_estimate > limit
            or input_estimate + RESULT_BYTES > WIRE_BYTES
            or input_estimate + resolved["existingContextTokens"] + resolved["outputCap"]
            > resolved["model"]["contextWindow"]):
        raise SummaryDeferred("input_budget_unavailable")
    parse_summary_request(payload["request"])
    return {**payload, "metadata": resolved,
            "budget": {"collectedBytes": collected, "selectedBytes": sum(len(p.text.encode()) for p in selected),
                       "inputEstimate": input_estimate, "estimator": resolved["estimator"],
                       "inputLimit": limit, "outputReserve": resolved["outputCap"],
                       "wireByteLimit": WIRE_BYTES, "resultByteLimit": RESULT_BYTES},
            "definitionDigest": sha256(instructions.encode()).hexdigest()}


def validate_result(request: dict[str, Any], result: object) -> dict[str, Any]:
    item = parse_summary_request(request)
    value = parse_summary_result(result)
    for key in ("requestId", "inputKey", "skillDigest", "sources"):
        if value[key] != item[key]:
            raise LogContractError("summary result binding mismatch: " + key)
    return value


class SDKSummaryAdapter:
    """Bounded bridge port; the DVL-10 owner provides authority and journal state."""
    def __init__(self, bridge: Any, *, timeout: float = 30):
        self.bridge = bridge
        self.timeout = timeout

    def dispatch_prepared(self, prepared: dict[str, Any], *, expected_peer: tuple[str, int],
                          authority_token: object, writer: dict[str, str]) -> dict[str, Any]:
        if not callable(getattr(authority_token, "current", None)) or authority_token.current() is not True:
            raise SummaryDeferred("authority_unavailable")
        response = self.bridge.request("manager", {"kind": "summary_request", "summary": prepared,
                                                   "writer": writer}, timeout=self.timeout,
                                       expected_peer=expected_peer, authority_token=authority_token)
        if response.get("status") == "deferred" and response.get("sent") is False:
            raise SummaryDeferred(str(response.get("reason", "summary_deferred")))
        if response.get("status") != "summary_result" or response.get("protected") is not False:
            raise SummaryUnknown(str(response.get("reason", "summary_result_unknown")))
        return validate_result(prepared["request"], response.get("result"))
