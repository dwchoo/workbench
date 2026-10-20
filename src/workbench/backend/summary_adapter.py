"""Request-specific summary admission and deterministic source selection.

The SDK session lives in the existing Manager process. This module never calls
a model directly and does not schedule, retry sent input or change permissions.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from workbench.assets import canonical_path
from pathlib import Path
import re
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
BYTES_ESTIMATOR = "utf8_bytes_upper_bound"
NATIVE_ESTIMATOR = "native_known_family_strict"
TOKENIZER_ENCODINGS = {"claude-v3": "ClaudeV3", "claude-v47": "ClaudeV47",
    "claude-v5": "ClaudeV5", "claude-v5-sonnet": "ClaudeV5Sonnet", "qwen3": "Qwen3",
    "deepseek-v3": "DeepSeekV3", "kimi-k2": "KimiK2", "glm5": "Glm5"}
_PRIORITY = {"start_spec": 0, "decision": 1, "report": 2, "terminal": 3}


class SummaryDeferred(RuntimeError):
    """Confirmed pre-dispatch refusal; its input has not been sent."""


class SummaryUnknown(RuntimeError):
    """Dispatch occurred or cannot be ruled out; callers must not replay."""


def skill_bytes() -> bytes:
    # Fixed shipped asset; never resolve a model-supplied skill name or path.
    return canonical_path("omp_bridge/skills/" + SKILL_NAME + "/SKILL.md").read_bytes()


@dataclass(frozen=True)
class SourcePiece:
    range: dict[str, Any]
    text: str
    kind: str = "terminal"
    sequence: int = 0
    ranges: tuple[dict[str, Any], ...] = ()

    @property
    def source_ranges(self) -> tuple[dict[str, Any], ...]:
        return self.ranges or (self.range,)


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
    selection = result.get("childSelection")
    if (not isinstance(selection, dict) or selection.get("source") != "configured_worker_default"
            or not isinstance(selection.get("modelTableDigest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", selection["modelTableDigest"]) is None
            or selection.get("descriptorSource") != "manager_model_registry_getAll_all"
            or selection.get("serviceTierSource") != "parent_session_readback"):
        raise SummaryDeferred("model_metadata_unavailable")
    for model in (result["model"], selection.get("model")):
        if not isinstance(model, dict) or any(not isinstance(model.get(key), str) or not model[key]
                                              for key in ("id", "provider", "api", "baseUrl")):
            raise SummaryDeferred("model_metadata_unavailable")
        for key in ("contextWindow", "maxTokens"):
            if type(model.get(key)) is not int or model[key] <= 0:
                raise SummaryDeferred("model_metadata_unavailable")
    model = selection["model"]
    if (selection.get("thinkingLevel") not in ("minimal", "low", "medium", "high", "xhigh", "max")
            or selection.get("selector") != f"{model['provider']}/{model['id']}:{selection['thinkingLevel']}"):
        raise SummaryDeferred("model_metadata_unavailable")
    if (type(result.get("outputCap")) is not int
            or result["outputCap"] != min(8192, model["maxTokens"])
            or result.get("outputLimitSource") != "host_default"):
        raise SummaryDeferred("output_budget_unavailable")
    if result.get("estimator") not in (BYTES_ESTIMATOR, NATIVE_ESTIMATOR):
        raise SummaryDeferred("input_budget_unavailable")
    return result


def prepare_summary(request: dict[str, Any], pieces: Sequence[SourcePiece], *,
                    metadata: Mapping[str, Any] | None, instructions: str,
                    prior_cumulative: dict[str, Any] | None = None,
                    count_fragments: Callable[[dict[str, Any]], Mapping[str, Any] | None] | None = None) -> dict[str, Any]:
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
        spans = [parse_source_range(span) for span in piece.source_ranges]
        raw = piece.text.encode("utf-8")
        if (piece.kind not in _PRIORITY or type(piece.sequence) is not int
                or canonical_json(piece.range) != canonical_json(spans[0])
                or (len(spans) > 1 and piece.kind != "terminal")):
            raise LogContractError("source piece identity or bytes mismatch")
        cursor = 0
        for index, span in enumerate(spans):
            identity = canonical_json(span)
            length = span["end"] - span["start"]
            if (identity not in expected or identity in seen
                    or (index > 0 and (canonical_json(span["source"]) != canonical_json(spans[0]["source"])
                                      or spans[index - 1]["end"] != span["start"]))
                    or span["digest"] != sha256(raw[cursor:cursor + length]).hexdigest()
                    or len(raw[cursor:cursor + length]) != length):
                raise LogContractError("source piece identity or bytes mismatch")
            cursor += length
            seen.add(identity)
        if cursor != len(raw):
            raise LogContractError("source piece identity or bytes mismatch")
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
    limit = resolved["childSelection"]["model"]["contextWindow"]

    def envelope(keep: list[SourcePiece], omissions: list[dict[str, Any]]) -> dict[str, Any]:
        current = {**item, "sources": [span for p in keep for span in p.source_ranges]}
        current["inputKey"] = summary_input_key(current["subject"], current["revision"],
                                               current["sources"], current["sourceGaps"], current["skillDigest"])
        return {"request": current, "selected": [
            {"ranges": list(p.source_ranges), "text": p.text} if len(p.source_ranges) > 1 else
            {"range": p.range, "text": p.text} for p in keep],
                "omitted": omissions, "priorCumulative": prior_cumulative,
                "resultByteLimit": RESULT_BYTES}

    def size(payload: dict[str, Any]) -> int:
        # Conservative framing/tool reserve, checked again against actual SDK
        # context before every stream call. This is not an exact tokenizer count.
        return len(canonical_json(payload)) + len(instructions.encode()) + len(skill) + 2048

    # Mirror the SDK's mandatory assistant/toolResult framing with object arguments.
    # Serialized args reserve bytes only; the previous full outputCap reserves their
    # context contribution, so arguments are not charged twice as context tokens.
    arguments = {"facts": [""], "interpretations": [], "unknowns": []}
    arguments["facts"][0] = "x" * (RESULT_BYTES - len(canonical_json(arguments)))
    arguments_bytes = len(canonical_json(arguments))
    model = resolved["childSelection"]["model"]
    call_id = "summary-preflight"  # Placeholder; arbitrary provider extras are not bounded.
    continuation = [
        {"role": "assistant", "content": [{"type": "toolCall", "id": call_id,
          "name": "db_summary_save", "arguments": arguments}], "api": model["api"],
         "provider": model["provider"], "model": model["id"],
         "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                   "totalTokens": 0, "cost": {"input": 0, "output": 0, "cacheRead": 0,
                                            "cacheWrite": 0, "total": 0}},
         "stopReason": "toolUse", "timestamp": 9_999_999_999_999},
        {"role": "toolResult", "toolCallId": call_id, "toolName": "db_summary_save",
         "content": [{"type": "text", "text": canonical_json({"received": True, "saved": False,
                      "requestId": item["requestId"]}).decode()}], "isError": False,
         "timestamp": 9_999_999_999_999}]
    # Two added messages replace the closing bracket of the existing message list:
    # their own list brackets cost one byte more than the appended comma framing.
    continuation_bytes = len(canonical_json(continuation)) - 1
    fixed_framing = continuation_bytes - arguments_bytes

    def count(payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None, str | None]:
        # This shared host-fragment estimate is NOT a bound for the SDK's full
        # projection. The SDK keeps its independent actual context/output checks.
        if count_fragments is None:
            return size(payload), None, "count_port_absent"
        encoding = (TOKENIZER_ENCODINGS.get(model.get("tokenizer"))
                    if isinstance(model.get("tokenizer"), str) else None)
        if encoding is None:
            return size(payload), None, "tokenizer_family_unavailable"
        fragments = [instructions + "\n\n" + skill.decode("utf-8"), canonical_json(payload).decode("utf-8")]
        owner = {"epoch": resolved["epoch"], "parentSessionId": resolved["parentSessionId"], "model": model}
        query = {"version": "summary-host-input-v1", "owner": deepcopy(owner),
                 "ownerDigest": sha256(canonical_json(owner)).hexdigest(), "fragments": fragments,
                 "fragmentDigests": [sha256(f.encode()).hexdigest() for f in fragments]}
        try:
            response = count_fragments(deepcopy(query))
        except SummaryDeferred:
            raise
        except Exception:
            raise SummaryDeferred("input_count_unavailable") from None
        if response is None:
            return size(payload), None, "count_port_unavailable"
        if (not isinstance(response, Mapping) or response.get("version") != query["version"]
                or response.get("ownerDigest") != query["ownerDigest"]
                or response.get("fragmentDigests") != query["fragmentDigests"]
                or type(response.get("exact")) is not bool):
            raise SummaryDeferred("input_count_binding_mismatch")
        if response["exact"] is False:
            return size(payload), None, "native_count_unavailable"
        tokens = response.get("tokens")
        if (response.get("encoding") != encoding or response.get("estimator") != NATIVE_ESTIMATOR
                or not isinstance(tokens, list) or len(tokens) != len(fragments)
                or any(type(n) is not int or n < 0 for n in tokens)):
            raise SummaryDeferred("input_count_binding_mismatch")
        binding = {"version": query["version"], "ownerDigest": query["ownerDigest"],
            "fragmentDigests": list(query["fragmentDigests"]), "encoding": encoding,
            "tokens": list(tokens), "exact": True, "estimator": NATIVE_ESTIMATOR}
        return sum(tokens) + 2048, binding, None

    def fits(payload: dict[str, Any], estimate: int) -> bool:
        # Wire size always remains bytes, including its once-only 8192 allowance.
        wire_estimate = size(payload)
        return (estimate <= limit
                and wire_estimate + continuation_bytes + RESULT_BYTES <= WIRE_BYTES
                and estimate + fixed_framing
                + resolved["outputCap"] + resolved["outputCap"] <= model["contextWindow"])

    for piece in checked:
        trial = envelope([*selected, piece], omitted)
        # A byte-wire-impossible candidate cannot benefit from native counting.
        # Preserve ordered complete-range selection without shipping up to 1MiB
        # over the count-only IPC for a range that must be omitted anyway.
        if (size(trial) + continuation_bytes + RESULT_BYTES <= WIRE_BYTES
                and fits(trial, count(trial)[0])):
            selected.append(piece)
        else:
            omitted.extend({"range": span, "reason": "input_budget",
                            "estimatedBytes": span["end"] - span["start"]} for span in piece.source_ranges)
    payload = envelope(selected, omitted)
    input_estimate, count_binding, fallback = count(payload)
    wire_estimate = size(payload)
    estimator = NATIVE_ESTIMATOR if count_binding is not None else BYTES_ESTIMATOR
    if (not selected and not item["sourceGaps"] or not fits(payload, input_estimate)):
        raise SummaryDeferred("input_budget_unavailable")
    parse_summary_request(payload["request"])
    return {**payload, **({"countBinding": count_binding} if count_binding is not None else {}), "metadata": resolved,
            "budget": {"collectedBytes": collected, "selectedBytes": sum(len(p.text.encode()) for p in selected),
                       "inputEstimate": input_estimate, "estimator": estimator,
                       "fallbackReason": fallback, "wireInputEstimateBytes": wire_estimate,
                       "inputLimit": limit, "outputReserve": resolved["outputCap"],
                       "previousOutputReserve": resolved["outputCap"],
                       "nextOutputReserve": resolved["outputCap"],
                       "mandatoryArgumentsBytes": arguments_bytes,
                       "fixedContinuationEstimate": fixed_framing,
                       "mandatoryContinuationEstimate": input_estimate + continuation_bytes,
                       "mandatoryWireBytes": wire_estimate + continuation_bytes + RESULT_BYTES,
                       "dynamicExtrasReserve": WIRE_BYTES - wire_estimate - continuation_bytes - RESULT_BYTES,
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

    def read_runtime(self, *, expected_peer: tuple[str, int]) -> dict[str, Any]:
        """Read the current Manager's descriptor and bundled definition, without input."""
        from workbench.ipc.bridge_g3.mailbox import BridgeBoundMismatch
        try:
            response = self.bridge.request("manager", {"kind": "summary_runtime",
                "expectedPeer": {"sessionId": expected_peer[0], "generation": expected_peer[1]}},
                timeout=self.timeout, expected_peer=expected_peer)
        except BridgeBoundMismatch as exc:
            raise SummaryDeferred("authority_changed_before_submission") from exc
        if not isinstance(response, Mapping):
            raise SummaryDeferred("model_metadata_unavailable")
        if response.get("status") == "deferred" and response.get("sent") is False:
            raise SummaryDeferred(str(response.get("reason", "model_metadata_unavailable")))
        runtime = response.get("runtime")
        metadata = runtime.get("metadata") if isinstance(runtime, Mapping) else None
        instructions = runtime.get("instructions") if isinstance(runtime, Mapping) else None
        if (response.get("status") != "summary_runtime" or response.get("sent") is not False
                or response.get("protected") is not False
                or type(response.get("generation")) is not int
                or (response.get("sessionId"), response.get("generation")) != expected_peer
                or not isinstance(metadata, Mapping)
                or metadata.get("source") != "parent_session_readback"
                or metadata.get("parentSessionId") != expected_peer[0]
                or type(instructions) is not str or not instructions):
            raise SummaryDeferred("model_metadata_unavailable")
        peer = self.bridge.peer("manager")
        if (peer.session_id, peer.generation) != expected_peer:
            raise SummaryDeferred("authority_changed_before_submission")
        return {"metadata": deepcopy(dict(metadata)), "instructions": instructions,
                "expected_peer": expected_peer}

    def count_fragments(self, query: dict[str, Any], *, expected_peer: tuple[str, int],
                        authority_token: object) -> Mapping[str, Any] | None:
        """Pure local native count via the existing bound Manager connection."""
        if not callable(getattr(authority_token, "current", None)) or authority_token.current() is not True:
            raise SummaryDeferred("authority_unavailable")
        from workbench.ipc.bridge_g3.mailbox import BridgeBoundMismatch
        try:
            response = self.bridge.request("manager", {"kind": "summary_count", "query": deepcopy(query),
                "expectedPeer": {"sessionId": expected_peer[0], "generation": expected_peer[1]}},
                timeout=self.timeout, expected_peer=expected_peer, authority_token=authority_token)
        except BridgeBoundMismatch as exc:
            raise SummaryDeferred("authority_changed_before_submission") from exc
        if authority_token.current() is not True:
            raise SummaryDeferred("authority_changed")
        if response.get("status") == "deferred" and response.get("sent") is False:
            raise SummaryDeferred(str(response.get("reason", "input_count_unavailable")))
        if response.get("status") == "count_unavailable" and response.get("sent") is False:
            if response.get("reason") == "model_metadata_unavailable":
                return None
        proof = response.get("proof")
        if (response.get("status") != "summary_count" or not isinstance(proof, Mapping)
                or proof.get("version") != query.get("version")
                or proof.get("ownerDigest") != query.get("ownerDigest")
                or proof.get("fragmentDigests") != query.get("fragmentDigests")
                or type(proof.get("exact")) is not bool):
            raise SummaryDeferred("input_count_binding_mismatch")
        return deepcopy(dict(proof))

    def dispatch_prepared(self, prepared: dict[str, Any], *, expected_peer: tuple[str, int],
                          authority_token: object, writer: dict[str, str]) -> dict[str, Any]:
        response = self.dispatch_outcome(prepared, expected_peer=expected_peer,
                                         authority_token=authority_token, writer=writer)
        if response.get("status") == "deferred" and response.get("sent") is False:
            raise SummaryDeferred(str(response.get("reason", "summary_deferred")))
        if response.get("status") != "summary_result" or response.get("protected") is not False:
            raise SummaryUnknown(str(response.get("reason", "summary_result_unknown")))
        return validate_result(prepared["request"], response.get("result"))

    def dispatch_outcome(self, prepared: dict[str, Any], *, expected_peer: tuple[str, int],
                         authority_token: object, writer: dict[str, str]) -> dict[str, Any]:
        """Preserve sent/protection/reason for the durable scheduler owner."""
        if not callable(getattr(authority_token, "current", None)) or authority_token.current() is not True:
            raise SummaryDeferred("authority_unavailable")
        from workbench.ipc.bridge_g3.mailbox import BridgeBoundMismatch
        try:
            response = self.bridge.request("manager", {"kind": "summary_request", "summary": prepared,
                                                       "writer": writer}, timeout=self.timeout,
                                           expected_peer=expected_peer, authority_token=authority_token)
        except BridgeBoundMismatch as exc:
            # The bridge's bound/claim rejection precedes all frame bytes.
            raise SummaryDeferred("authority_changed_before_submission") from exc
        if response.get("status") == "summary_result" and response.get("protected") is False:
            validate_result(prepared["request"], response.get("result"))
        return response
