"""Strict decoding of catalog control identities shared by catalog and direct writers."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID


class ControlError(ValueError):
    """A catalog control record cannot establish one unambiguous identity."""


def parse_control(raw: bytes) -> dict[str, Any]:
    def unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, member in pairs:
            if key in value:
                raise ControlError("duplicate important catalog control member")
            value[key] = member
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique_members)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ControlError("invalid important catalog control") from exc
    if type(value) is not dict:
        raise ControlError("invalid important catalog control")
    return value


def _uuid(value: Any) -> None:
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError(value)
    except ValueError as exc:
        raise ControlError("database UUID must be canonical") from exc


def validate_pointer(value: dict[str, Any]) -> dict[str, Any]:
    if (set(value) not in ({"version", "generation", "active", "archives"},
                            {"version", "generation", "active", "archives", "archiveChecks"})
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["generation"]) is not int or value["generation"] < 1
            or type(value["archives"]) is not list):
        raise ControlError("invalid active pointer")
    _uuid(value["active"])
    archives = value["archives"]
    if any(type(identity) is not str for identity in archives):
        raise ControlError("invalid archive identities")
    for identity in archives:
        _uuid(identity)
    if len(archives) != len(set(archives)) or value["active"] in archives:
        raise ControlError("invalid archive identities")
    checks = value.get("archiveChecks", {})
    if type(checks) is not dict or set(checks) - set(archives):
        raise ControlError("invalid archive checkpoint metadata")
    if any(type(status) is not str or status not in ("complete", "busy", "error")
           for status in checks.values()):
        raise ControlError("invalid archive checkpoint state")
    return {**value, "archiveChecks": checks}
