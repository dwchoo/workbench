"""hub_v1: the Hub control socket contract (SPEC "hub_v1 제어 socket 프로토콜").

Transport is the Hub's 0600 Unix socket; the server closes a peer whose
``SO_PEERCRED`` uid differs from its euid. Framing is one UTF-8 JSON object per
``\\n``-terminated line of at most :data:`MAX_LINE_BYTES`; an oversized or non-JSON
line is answered with ``reject``/``protocol_error`` and the connection is closed.

The first line is ``{"v":1,"type":"hello","client":<name>}`` answered with
``{"v":1,"type":"welcome","hub":{pid,start_ticks,version,started_at,mode}}`` or
``reject``/``version_mismatch`` (``bad_request`` when ``client`` is not a non-empty
string of at most :data:`MAX_CLIENT_NAME_LENGTH`). Requests are ``{"v":1,"id":<<=64 chars>,"type":<op>,...}``
answered with ``{"v":1,"id":...,"type":"result","ok":true,...}`` or
``{"ok":false,"reason":<code>,"detail":<sentence>}``. Unknown and reserved ops are
``unsupported_type``. At most :data:`MAX_CONNECTIONS` connections are served and a
connection without a request for :data:`IDLE_TIMEOUT` seconds is closed.
"""
from __future__ import annotations

from enum import StrEnum
import json
from typing import Any

from workbench.contracts.v1 import ContractError

CONTRACT_NAME = "workbench.hub"
VERSION = 1
SUPPORTED_VERSIONS = (VERSION,)
MAX_LINE_BYTES = 1 << 20
MAX_ID_LENGTH = 64
MAX_CLIENT_NAME_LENGTH = 64
MAX_CONNECTIONS = 16
IDLE_TIMEOUT = 60.0
STOP_TOKEN_TTL = 60


class ClientType(StrEnum):
    HELLO = "hello"
    STATUS = "status"
    LIST = "list"
    STOP_REQUEST = "stop_request"
    STOP_CONFIRM = "stop_confirm"


# Reserved names; (B) answers them with ``unsupported_type`` and (A) opens them.
RESERVED_OPS = frozenset({"power_on", "power_off_request", "power_off_confirm", "pause", "resume",
                          "confirm_boot"})


class ServerType(StrEnum):
    WELCOME = "welcome"
    RESULT = "result"
    REJECT = "reject"


class Reason(StrEnum):
    PROTOCOL_ERROR = "protocol_error"
    VERSION_MISMATCH = "version_mismatch"
    UNSUPPORTED_TYPE = "unsupported_type"
    CONNECTION_LIMIT = "connection_limit"
    BAD_REQUEST = "bad_request"
    STOP_TOKEN_MISMATCH = "stop_token_mismatch"


class ProtocolError(ContractError):
    """A line that breaks hub_v1 framing; the connection must be closed."""


def encode_line(value: dict[str, Any]) -> bytes:
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_LINE_BYTES:
        raise ProtocolError("line exceeds MAX_LINE_BYTES")
    return data


def decode_line(line: bytes) -> dict[str, Any]:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("line is not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("line is not a JSON object")
    return value


class LineDecoder:
    """Incremental splitter enforcing :data:`MAX_LINE_BYTES` before buffering further."""

    def __init__(self, limit: int = MAX_LINE_BYTES):
        self.limit = limit
        self.buffer = b""

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self.buffer += data
        values = []
        while (end := self.buffer.find(b"\n")) >= 0:
            line, self.buffer = self.buffer[:end], self.buffer[end + 1:]
            if len(line) + 1 > self.limit:
                raise ProtocolError("line exceeds MAX_LINE_BYTES")
            values.append(decode_line(line))
        if len(self.buffer) >= self.limit:
            raise ProtocolError("line exceeds MAX_LINE_BYTES")
        return values


def hello(client: str, version: int = VERSION) -> dict[str, Any]:
    return {"v": version, "type": ClientType.HELLO.value, "client": client}


def welcome(hub: dict[str, Any]) -> dict[str, Any]:
    return {"v": VERSION, "type": ServerType.WELCOME.value, "hub": hub}


def reject(reason: Reason) -> dict[str, Any]:
    return {"v": VERSION, "type": ServerType.REJECT.value, "reason": reason.value}


def request(request_id: str, op: str, **fields: Any) -> dict[str, Any]:
    return {"v": VERSION, "id": request_id, "type": op, **fields}


def result(request_id: str, **fields: Any) -> dict[str, Any]:
    return {"v": VERSION, "id": request_id, "type": ServerType.RESULT.value, "ok": True, **fields}


def failure(request_id: str, reason: Reason, detail: str) -> dict[str, Any]:
    return {"v": VERSION, "id": request_id, "type": ServerType.RESULT.value, "ok": False,
            "reason": reason.value, "detail": detail}


def valid_client_name(value: object) -> bool:
    """hello ``client``: a non-empty string of at most :data:`MAX_CLIENT_NAME_LENGTH`."""
    return isinstance(value, str) and 0 < len(value) <= MAX_CLIENT_NAME_LENGTH


def valid_request_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_ID_LENGTH
