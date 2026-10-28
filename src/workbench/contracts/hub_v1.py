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

Backend principal (Task Inbox SPEC "Hub ↔ backend 요청 통로"; additive, the version stays 1): a hello may carry
``project_id`` (a canonical UUID string; ``null`` counts as absent; anything else is ``bad_request``). A hello with ``client``
:data:`BACKEND_CLIENT` or with a ``project_id`` must have both, and the Hub grants the principal ``manager`` of that
project only when the registry entry of ``project_id`` names a running backend (``backend.json``) whose pid and
start ticks are the socket peer's (``SO_PEERCRED`` pid, ``/proc`` start ticks read at accept); otherwise it answers
``reject``/``principal_mismatch`` and closes. The check runs again before every request of such a connection; a
failed re-check answers that request with ``principal_mismatch`` and closes. A backend connection has no idle
timeout, is not counted in :data:`MAX_CONNECTIONS` and replaces an earlier one of the same project.
:data:`BACKEND_OPS` are served only to a backend principal and :data:`LOCAL_OPS` only to any other connection
(principal ``local``); an op of the other set is ``unsupported_type``. A refused inbox op answers ``ok:false`` with
the inbox reason code (the REST API's error code names: ``not_found``, ``forbidden``, ``invalid_input``,
``invalid_transition``, ...) and, when the caller may see the request, its ``state``/``version``.

- ``inbox_list{states?, limit?}`` -> ``{requests, next_cursor}`` (this project's requests; the default states are
  the open ones), ``inbox_show{request_id}`` -> ``{request}``;
- ``inbox_review{request_id, action, body?, outcome?, summary?}``, ``inbox_link_check{request_id}`` ->
  ``{link_token, expires_in, ...}``, ``inbox_link_commit{link_token, task_id, spec_revision, task_status}`` ->
  ``{state, version}``, ``inbox_delivery{request_id, delivery_id, outcome}`` -> ``{state, version}``;
- ``review_mode{root, set?}`` (local) -> ``{review_mode}``.

``reload{}`` (local, 2026-10-27 사용자 결정): the Hub re-reads ``hub.toml`` with the strict parser. An invalid file
keeps the running config and answers ``ok:false``, ``reason`` ``config_invalid``, ``detail``
``config_invalid:<item>`` and ``item``. A valid one answers ``{changed: [<key>...], web: <status.web>,
board_agent: <status.board_agent>, board_agent_note: <text>|null, config: <status.config>}``: a web change
re-creates only the web listener (an open that fails shows in ``web``); nothing else (WB projects, registry,
inbox, review dispatch) is touched. ``reload_failed``/``reload_timeout`` when the Hub could not finish it.
"""
from __future__ import annotations

from enum import StrEnum
import json
from typing import Any
from uuid import UUID

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
    REVIEW_MODE = "review_mode"
    RELOAD = "reload"
    INBOX_LIST = "inbox_list"
    INBOX_SHOW = "inbox_show"
    INBOX_REVIEW = "inbox_review"
    INBOX_LINK_CHECK = "inbox_link_check"
    INBOX_LINK_COMMIT = "inbox_link_commit"
    INBOX_DELIVERY = "inbox_delivery"


# hello ``client`` of a backend's own connection (the backend principal).
BACKEND_CLIENT = "workbench-backend"
BACKEND_OPS = frozenset({ClientType.INBOX_LIST.value, ClientType.INBOX_SHOW.value, ClientType.INBOX_REVIEW.value,
                         ClientType.INBOX_LINK_CHECK.value, ClientType.INBOX_LINK_COMMIT.value,
                         ClientType.INBOX_DELIVERY.value})
LOCAL_OPS = frozenset({ClientType.STATUS.value, ClientType.LIST.value, ClientType.STOP_REQUEST.value,
                       ClientType.STOP_CONFIRM.value, ClientType.REVIEW_MODE.value, ClientType.RELOAD.value})


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
    PRINCIPAL_MISMATCH = "principal_mismatch"


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


def hello(client: str, version: int = VERSION, *, project_id: str | None = None) -> dict[str, Any]:
    value = {"v": version, "type": ClientType.HELLO.value, "client": client}
    if project_id is not None:
        value["project_id"] = project_id
    return value


def welcome(hub: dict[str, Any]) -> dict[str, Any]:
    return {"v": VERSION, "type": ServerType.WELCOME.value, "hub": hub}


def reject(reason: Reason) -> dict[str, Any]:
    return {"v": VERSION, "type": ServerType.REJECT.value, "reason": reason.value}


def request(request_id: str, op: str, /, **fields: Any) -> dict[str, Any]:
    """``request_id`` is the frame ``id`` (positional: inbox ops carry a ``request_id`` field)."""
    return {"v": VERSION, "id": request_id, "type": op, **fields}


def result(request_id: str, **fields: Any) -> dict[str, Any]:
    return {"v": VERSION, "id": request_id, "type": ServerType.RESULT.value, "ok": True, **fields}


def failure(request_id: str, reason: Reason | str, detail: str, **fields: Any) -> dict[str, Any]:
    """``reason`` is a :class:`Reason` or an inbox reason code (``not_found``, ``forbidden``, ...)."""
    return {"v": VERSION, "id": request_id, "type": ServerType.RESULT.value, "ok": False,
            "reason": reason.value if isinstance(reason, Reason) else reason, "detail": detail, **fields}


def valid_client_name(value: object) -> bool:
    """hello ``client``: a non-empty string of at most :data:`MAX_CLIENT_NAME_LENGTH`."""
    return isinstance(value, str) and 0 < len(value) <= MAX_CLIENT_NAME_LENGTH


def valid_project_id(value: object) -> bool:
    """hello ``project_id``: a canonical (lower-case, hyphenated) UUID string."""
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def valid_request_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_ID_LENGTH
