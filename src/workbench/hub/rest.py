"""External request REST API ``/api/v1/`` (web-board-ux SPEC "외부 요청 API", WBX-D05..D07).

The nine operations of :mod:`workbench.hub.inbox_api` (the external request API's processing layer) as JSON resources.
Every request needs ``Authorization: Bearer wbk_…``; the session cookie is never read. Errors are
``{"error": {"code", "message", "state"?, "version"?}}`` with the SPEC status of their code.

Order per request: Origin (403) -> API key (401) -> method (405 + ``Allow``) -> ``Content-Type`` (415) -> body size
(413, the declared length before reading) -> one strict JSON object (400) -> fields (400) -> the operation.

Wiring: the external listener serves it with ``create_app(services, routes=[routes(api)])``; every route is
``own_auth``, so the web middleware's allow_from, Host and (state-change) Origin checks run first and the session
check is skipped. The Board agent's loopback listener is :class:`InternalListener` (``/api/v1/`` only, ``Host`` fixed
to ``127.0.0.1:<port>``, the system key only; WBX-B5).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import sys
import time
from typing import Callable

from aiohttp import web

from . import auth
from .inbox.limits import InboxError
from .inbox_api import TOOLS, WWW_AUTHENTICATE, InboxApi, ParseError, error_payload, parse_json
from .web.app import OriginPolicy, flush_last_used
from .web.middleware import (BODY_LIMIT, SERVICES, _listening_port as listening_port, body_limit, expected_origin,
                             origin_of_header, own_auth, security_headers)

PREFIX = "/api/v1"


def json_response(value: object, *, status: int = 200, headers: dict | None = None) -> web.Response:
    # ASCII output: user text (lone surrogates included) can never make the body unencodable.
    return web.Response(status=status, body=json.dumps(value, ensure_ascii=True).encode("ascii"),
                        content_type="application/json", headers=headers)


def error_response(code: str, message: str, *, status: int, state: str | None = None, version: int | None = None,
                   headers: dict | None = None) -> web.Response:
    error: dict = {"code": code, "message": message}
    if state is not None:
        error["state"] = state
    if version is not None:
        error["version"] = version
    return json_response({"error": error}, status=status, headers=headers)


# SPEC "오류 → HTTP status"; a code not listed is ``internal_error`` (``error_payload`` already maps it).
STATUS = {
    "invalid_input": 400, "unauthorized": 401, "forbidden": 403, "origin_mismatch": 403, "not_found": 404,
    "conflict": 409, "invalid_transition": 409, "idempotency_conflict": 409, "token_mismatch": 409,
    "backend_not_running": 409, "too_large": 413, "internal_error": 500,
    "project_unavailable": 503, "inbox_busy": 503, "inbox_mismatch": 503, "backend_unreachable": 503,
    "hub_stopping": 503, "busy": 503,
}


def inbox_error(code: str, detail: str = "", state: str | None = None, version: int | None = None) -> web.Response:
    """An operation's refusal: ``error_payload``'s normalised code and sentence with the code's status."""
    error = error_payload(code, detail, state, version)["error"]
    return error_response(error["code"], error["detail"], status=STATUS.get(error["code"], 500),
                          state=error.get("state"), version=error.get("version"))


ORIGIN_MESSAGE = "origin not allowed"


def origin_ok(request: web.Request) -> bool:
    """No ``Origin``, or exactly one that is this listener's own origin (every method; no CORS)."""
    origins = request.headers.getall("Origin", [])
    if not origins:
        return True
    return len(origins) == 1 and origin_of_header(origins[0]) is not None \
        and origin_of_header(origins[0]) == expected_origin(request)


def unauthorized() -> web.Response:
    return error_response("unauthorized", "unauthorized", status=401, headers={"WWW-Authenticate": WWW_AUTHENTICATE})


class _Refused(Exception):
    def __init__(self, response: web.Response):
        super().__init__()
        self.response = response


def _invalid(message: str, status: int = 400) -> _Refused:
    return _Refused(error_response("invalid_input", message, status=status))


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One method of one resource: the operation, the JSON body fields it takes (``None``: no body) and the query
    parameters it takes. The path's ``{rid}`` is the operation's ``request_id``."""

    tool: str
    body: tuple | None = None
    query: tuple = ()


async def _read_body(request: web.Request, fields: tuple) -> dict:
    if request.content_type != "application/json":
        raise _invalid("Content-Type must be application/json", 415)
    if request.content_length is not None and request.content_length > BODY_LIMIT:
        raise _Refused(error_response("too_large", "request body too large", status=413))
    try:
        raw = await request.read()
    except web.HTTPRequestEntityTooLarge:
        raise _Refused(error_response("too_large", "request body too large", status=413)) from None
    except ConnectionResetError:  # the client went away mid-body (a false Content-Length): no traceback
        raise _invalid("the body is incomplete") from None
    try:
        value = parse_json(raw)
    except ParseError:
        raise _invalid("the body is not strict JSON") from None
    if not isinstance(value, dict):
        raise _invalid("the body must be a JSON object")
    if set(value) - set(fields):
        raise _invalid("unknown field")
    return value


# Query parameters that are not the operation's argument of the same name (repeatable ones become a list).
QUERY_ARGUMENTS = {"state": "states"}


def _read_query(request: web.Request, tool: str, names: tuple) -> dict:
    """SPEC query rules: only the endpoint's parameters, each once (``state`` may repeat), integers as unsigned
    decimals, booleans ``true|false``; the operation's own checks (enums, ranges, sizes) come after."""
    properties = TOOLS[tool].properties
    arguments: dict = {}
    for name in request.query:
        if name not in names:
            raise _invalid("unknown query parameter")
        argument = QUERY_ARGUMENTS.get(name, name)
        values = request.query.getall(name)
        kind = properties[argument]["type"]
        if kind == "array":
            arguments[argument] = list(values)
            continue
        if len(values) != 1:
            raise _invalid(f"{name} is given more than once")
        value = values[0]
        if kind == "integer":
            if not (value.isascii() and value.isdigit()) or len(value) > 18:
                raise _invalid(f"{name} must be an unsigned decimal integer")
            arguments[argument] = int(value)
        elif kind == "boolean":
            if value not in ("true", "false"):
                raise _invalid(f"{name} must be true or false")
            arguments[argument] = value == "true"
        else:
            arguments[argument] = value
    return arguments


class RestApi:
    """The ``/api/v1/`` handlers over one :class:`InboxApi` (the Hub's one, shared by both listeners).
    ``system_only``: only the system key authenticates (the Board agent's internal listener, WBX-D24)."""

    def __init__(self, api: InboxApi, *, system_only: bool = False):
        self.api = api
        self.system_only = system_only

    async def _principal(self, request: web.Request):
        """Origin (403) then the API key (401): the principal, or the refusal to answer with."""
        if not origin_ok(request):
            return error_response("origin_mismatch", ORIGIN_MESSAGE, status=403)
        principal = await (self.api.authenticate(request, system_only=True) if self.system_only
                           else self.api.authenticate(request))
        return unauthorized() if principal is None else principal

    def unknown(self):
        """The catch-all: an unknown ``/api/v1/…`` path is 404 ``not_found`` after the key check."""
        async def handler(request: web.Request) -> web.StreamResponse:
            principal = await self._principal(request)
            if isinstance(principal, web.Response):
                return principal
            return error_response("not_found", "unknown endpoint", status=404)
        return own_auth(handler)

    def resource(self, methods: dict[str, Endpoint]):
        allow = ", ".join(methods)

        async def handler(request: web.Request) -> web.StreamResponse:
            principal = await self._principal(request)
            if isinstance(principal, web.Response):
                return principal
            endpoint = methods.get(request.method)
            if endpoint is None:
                return error_response("invalid_input", "method not allowed", status=405, headers={"Allow": allow})
            try:
                arguments = _read_query(request, endpoint.tool, endpoint.query)
                if endpoint.body is not None:
                    arguments.update(await _read_body(request, endpoint.body))
            except _Refused as refused:
                return refused.response
            if "rid" in request.match_info:
                arguments["request_id"] = request.match_info["rid"]
            return await self.api.in_slot(principal, lambda: self._call(principal, endpoint.tool, arguments),
                                          lambda: error_response("busy", "busy", status=503))
        return own_auth(handler)

    async def _call(self, principal, tool: str, arguments: dict) -> web.Response:
        try:
            value = await self.api.call(principal, TOOLS[tool], arguments)
        except InboxError as exc:
            return inbox_error(exc.code, exc.detail, exc.state, exc.version)
        except Exception as exc:  # never the exception text (paths) to the client; the Hub log keeps the type
            print(f"[workbench] rest {tool} failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            return inbox_error("internal_error")
        return self._answer(tool, value)

    @staticmethod
    def _answer(tool: str, value: dict) -> web.Response:
        if tool == "inbox_submit" and value.get("created"):
            return json_response(value, status=201, headers={"Location": f"{PREFIX}/requests/{value['request_id']}"})
        if tool == "inbox_comment":
            return json_response(value, status=201)
        if tool == "inbox_cancel" and value.get("status") == "cancel_requested":
            return json_response(value, status=202)
        return json_response(value)


RESOURCES: dict[str, dict[str, Endpoint]] = {
    "/projects": {"GET": Endpoint("inbox_projects", query=("query",))},
    "/requests": {"GET": Endpoint("inbox_list", query=("project_id", "state", "include_closed", "query",
                                                        "limit", "cursor")),
                  "POST": Endpoint("inbox_submit", body=("project_id", "title", "body", "idempotency_key"))},
    "/requests/{rid}": {"GET": Endpoint("inbox_get"),
                        "PATCH": Endpoint("inbox_update", body=("base_version", "title", "body"))},
    "/requests/{rid}/comments": {"POST": Endpoint("inbox_comment", body=("body",))},
    "/requests/{rid}/state": {"POST": Endpoint("inbox_set_state", body=("base_version", "to", "reason"))},
    "/requests/{rid}/cancel": {"POST": Endpoint("inbox_cancel", body=("confirm_token", "reason"))},
    "/requests/{rid}/result": {"GET": Endpoint("inbox_result",
                                               query=("full", "run_id", "artifact_path", "offset", "length"))},
}


def routes(api: InboxApi, *, system_only: bool = False) -> Callable[[web.Application], None]:
    """``create_app(services, routes=[routes(api)])``: the ``/api/v1/`` resources (own API key auth)."""
    rest = RestApi(api, system_only=system_only)
    handlers = {path: rest.resource(methods) for path, methods in RESOURCES.items()}
    unknown = rest.unknown()

    def register(app: web.Application) -> None:
        for path, handler in handlers.items():
            app.router.add_route("*", PREFIX + path, handler)
        app.router.add_route("*", PREFIX, unknown)  # ``/api/v1`` itself is an unknown REST path too
        app.router.add_route("*", PREFIX + "/{tail:.*}", unknown)  # after the resources: they match first
    return register


# --- Board agent internal listener (web-board-ux SPEC "Board agent 전환", WBX-D11/D24) ---------------------------

INTERNAL_HOST = "127.0.0.1"
FLUSH_INTERVAL = 10.0  # SPEC API key: last use written every 10 s
INTERNAL_MAX_CONNECTIONS = 16  # the Board agent is one client; more is closed at once (security review fix 5)


@dataclass(slots=True)
class _InternalServices:
    """What the REST handlers read from ``app[SERVICES]``: the origin policy, the key store and the clock."""

    api_keys: auth.ApiKeyStore
    clock: Callable[[], float]
    flush_interval: float = FLUSH_INTERVAL
    policy: OriginPolicy = field(default_factory=lambda: OriginPolicy(INTERNAL_HOST))

    async def blocking(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(None, function, *args)


@web.middleware
async def _internal_host(request: web.Request, handler):
    """Only ``Host: 127.0.0.1:<this port>`` (no ``localhost``, no other name): 421 otherwise."""
    hosts = request.headers.getall("Host", [])
    port = listening_port(request)
    if port is None or hosts != [f"{INTERNAL_HOST}:{port}"]:
        return web.Response(status=421, text="misdirected_request")
    try:
        return await handler(request)
    except ConnectionResetError:  # the Board agent ended mid-request (restart, stop): one line, no traceback
        print("[workbench] board rest: connection reset by the client during a request", file=sys.stderr, flush=True)
        return web.Response(status=400, text="connection_reset")


def create_internal_app(api_keys: auth.ApiKeyStore, api: InboxApi, *, clock: Callable[[], float] = time.time,
                        flush_interval: float = FLUSH_INTERVAL) -> web.Application:
    """``/api/v1/`` only (every other path 404: no ``/skill/`` or pages), Host fixed, the web listener's
    body limit and headers, and only the system key (another valid key or a previous system key is 401)."""
    app = web.Application(middlewares=[_internal_host, body_limit], client_max_size=BODY_LIMIT)
    app[SERVICES] = _InternalServices(api_keys, clock, flush_interval)
    app.on_response_prepare.append(security_headers)
    app.cleanup_ctx.append(flush_last_used)
    routes(api, system_only=True)(app)
    return app


class InternalListener:
    """The Board agent's loopback REST listener on ``127.0.0.1:0`` (the Board set starts and stops it), at most
    ``max_connections`` connections at once (the web listener's :class:`~workbench.hub.web.listener.GuardedSite`).
    ``allow_from`` never applies here: the internal app has no peer check (SPEC "Board agent 전환")."""

    def __init__(self, api_keys: auth.ApiKeyStore, api: InboxApi, *, clock: Callable[[], float] = time.time,
                 flush_interval: float = FLUSH_INTERVAL, max_connections: int = INTERNAL_MAX_CONNECTIONS):
        self.api_keys, self.api, self.clock, self.flush_interval = api_keys, api, clock, flush_interval
        self.max_connections = max_connections
        self.runner: web.AppRunner | None = None
        self.port: int | None = None
        self._lock = asyncio.Lock()  # one start/stop at a time: a second start never opens a second port

    @property
    def url(self) -> str | None:
        """The bare origin ``http://127.0.0.1:<port>`` (the Board agent's ``WB_HUB_URL``), None while stopped."""
        return None if self.port is None else f"http://{INTERNAL_HOST}:{self.port}"

    async def start(self) -> str:
        async with self._lock:
            if self.runner is not None:
                return self.url
            app = create_internal_app(self.api_keys, self.api, clock=self.clock,
                                      flush_interval=self.flush_interval)
            runner = web.AppRunner(app, access_log=None, handle_signals=False)
            await runner.setup()
            from .web.listener import GuardedSite
            try:
                await GuardedSite(runner, INTERNAL_HOST, 0, max_connections=self.max_connections).start()
            except BaseException:
                await runner.cleanup()
                raise
            self.runner, self.port = runner, runner.addresses[0][1]
            return self.url

    async def stop(self) -> None:
        async with self._lock:
            runner, self.runner, self.port = self.runner, None, None
            if runner is not None:
                await runner.cleanup()
                await asyncio.get_running_loop().run_in_executor(None, self.api_keys.flush)  # last uses
