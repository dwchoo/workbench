"""Version 1 contract for explicit project work records."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re
from uuid import UUID

SCHEMA_VERSION = 1
MAX_TITLE_CHARS = 256
MAX_BODY_BYTES = 64 * 1024
MAX_QUERY_CHARS = 256
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
RECENT_LIMIT = 5
PREVIEW_CHARS = 160
MAX_CONTEXT_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 900_000


class RecordContractError(ValueError):
    """An invalid request; messages exclude user content."""
    code = "invalid_record_contract_v1"


REQUEST_FIELDS = {"action", "record_id", "revision", "expected_revision", "title", "body",
                  "task_id", "query", "limit", "after", "output_path"}
ACTION_FIELDS = {
    "create": ({"title", "body"}, {"task_id"}),
    "revise": ({"record_id", "expected_revision", "title", "body"}, {"task_id"}),
    "show": ({"record_id"}, {"revision"}),
    "history": ({"record_id"}, {"limit", "after"}),
    "list": (set(), {"task_id", "limit", "after"}),
    "search": ({"query"}, {"task_id", "limit", "after"}),
    "recent": (set(), set()),
    "export": ({"record_id"}, {"revision", "output_path"}),
}


def text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RecordContractError(f"{name} must be a nonempty string")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise RecordContractError(f"{name} must be UTF-8") from exc
    return value


def record_id(value: object) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise RecordContractError("canonical record UUID required") from exc
    return value


def positive(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= 9007199254740991:
        raise RecordContractError(f"{name} must be a positive safe integer")
    return value


def validate_snapshot(title: object, body: object, task_id: object, actor: object) -> None:
    if len(text(title, "title")) > MAX_TITLE_CHARS:
        raise RecordContractError("title_too_large")
    if len(text(body, "body").encode("utf-8")) > MAX_BODY_BYTES:
        raise RecordContractError("body_too_large")
    if task_id is not None and text(task_id, "task_id").strip() != task_id:
        raise RecordContractError("invalid task_id")
    if actor not in ("manager", "worker"):
        raise RecordContractError("invalid actor")


def parse_request(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != REQUEST_FIELDS:
        raise RecordContractError("unsupported request fields")
    action = value["action"]
    if not isinstance(action, str) or action not in ACTION_FIELDS:
        raise RecordContractError("unsupported action")
    required, optional = ACTION_FIELDS[action]
    for key in REQUEST_FIELDS - {"action"}:
        item = value[key]
        if key in required and item is None:
            raise RecordContractError(f"{key} is required")
        if key not in required | optional and item is not None:
            raise RecordContractError(f"{key} is unused")
        if item is None:
            continue
        if key == "record_id":
            record_id(item)
        elif key in {"revision", "expected_revision", "limit"}:
            positive(item, key)
            if key == "limit" and item > MAX_LIMIT:
                raise RecordContractError("limit_too_large")
        else:
            text(item, key)
            if key == "title" and len(item) > MAX_TITLE_CHARS:
                raise RecordContractError("title_too_large")
            if key == "body" and len(item.encode("utf-8")) > MAX_BODY_BYTES:
                raise RecordContractError("body_too_large")
            if key == "query" and len(item) > MAX_QUERY_CHARS:
                raise RecordContractError("query_too_large")
            if key == "task_id" and item.strip() != item:
                raise RecordContractError("invalid task_id")
            if key == "output_path" and "\0" in item:
                raise RecordContractError("invalid output_path")
    return deepcopy(value)


def parse_record(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"record_id", "revision", "title", "body", "task_id", "created_at", "recorded_at", "actor"}:
        raise RecordContractError("unsupported snapshot fields")
    record_id(value["record_id"])
    positive(value["revision"], "revision")
    validate_snapshot(value["title"], value["body"], value["task_id"], value["actor"])
    for key in ("created_at", "recorded_at"):
        item = value[key]
        if not isinstance(item, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", item) is None:
            raise RecordContractError("UTC timestamp required")
        try:
            datetime.fromisoformat(item)
        except ValueError as exc:
            raise RecordContractError("invalid UTC timestamp") from exc
    return deepcopy(value)
