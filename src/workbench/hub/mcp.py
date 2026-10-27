"""MCP Streamable HTTP endpoint ``/mcp`` (Task Inbox SPEC "MCP Streamable HTTP endpoint").

Implemented without the ``mcp`` SDK (D-29). Every request needs ``Authorization: Bearer wbk_…``
(an API key verified by :meth:`workbench.hub.auth.ApiKeyStore.verify`); the web session cookie is
never accepted here. One JSON-RPC 2.0 object per ``POST`` (a batch is ``-32600``), a single
``application/json`` answer (no SSE), notifications ``202``, ``GET`` ``405``, ``DELETE`` ends the
session (``204``).

Wiring: the external listener serves it with ``create_app(services, routes=[routes(server)])``;
the Board agent's loopback listener is :class:`InternalListener` (``/mcp`` only, ``Host`` fixed to
``127.0.0.1:<port>``). Tool errors carry SPEC codes with a fixed detail for every store, lock or
backend failure, so no path or exception text reaches a client.

Only the JSON-RPC transport and its sessions live here (WBX-B1 expand): the tools' processing (keys, argument
checks, error normalisation, untrusted marks, cancel tokens, executor and per-key slots) is
:class:`workbench.hub.inbox_api.InboxApi`, which ``/api/v1/`` (:mod:`workbench.hub.rest`) shares.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import secrets
import sys
import time
from typing import Awaitable, Callable

from aiohttp import web

from . import auth
from .inbox.limits import InboxError
from .inbox.service import Principal
# The processing layer lives in ``inbox_api`` (WBX-B1 expand); its names stay importable from here until /mcp goes.
from .inbox_api import (ALIASES, BUSY_WAIT, CALLS_PER_KEY, CANCEL_TOKEN_TTL, DETAIL_MAX, EDITOR_KINDS,  # noqa: F401
                        GENERIC_DETAILS, MCP_WORKERS, PASSTHROUGH, PROJECT_FIELDS, STATES, TOOLS, UNTRUSTED_NOTE,
                        WWW_AUTHENTICATE, InboxApi, ParseError as _ParseError, Tool, bearer_key, check_arguments,
                        error_payload, marked, parse_json as _parse, principal_for, visible_tools)
from .web.app import OriginPolicy, flush_last_used
from .web.middleware import (BODY_LIMIT, SERVICES, _listening_port as listening_port, body_limit, expected_origin,
                             origin_of_header, own_auth, security_headers)

SERVER_NAME = "workbench-hub"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
UNAUTHORIZED = -32001
SESSION_NOT_FOUND = -32001
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
SESSION_HEADER = "Mcp-Session-Id"
VERSION_HEADER = "MCP-Protocol-Version"
SESSION_LIMIT = 4096  # live session ids kept; the oldest is dropped first (clients may re-initialize)
SESSIONS_PER_KEY = 64  # one key cannot push the other keys' sessions out
TOKENS_PER_KEY = 32  # live cancel tokens per key (the oldest is dropped); read at issue time (tests patch it)
BUSY = -32000  # JSON-RPC error of a call that waited ``busy_wait`` without a slot


def _json(value: object, *, status: int = 200, headers: dict | None = None) -> web.Response:
    # ASCII output: user text (lone surrogates included) can never make the body unencodable.
    return web.Response(status=status, body=json.dumps(value, ensure_ascii=True).encode("ascii"),
                        content_type="application/json", headers=headers)


def rpc_error(code: int, message: str, request_id: object = None, *, status: int = 200,
              headers: dict | None = None) -> web.Response:
    return _json({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
                 status=status, headers=headers)


def _result(request_id: object, result: dict) -> web.Response:
    return _json({"jsonrpc": "2.0", "id": request_id, "result": result})


def _accepts_json(values: list[str]) -> bool:
    for value in values:
        for item in value.split(","):
            if item.split(";", 1)[0].strip().lower() == "application/json":
                return True
    return False


def tool_result(value: dict, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "structuredContent": value, "isError": is_error}


class McpServer(InboxApi):
    """The ``/mcp`` endpoint: JSON-RPC transport and live session ids (memory only) over :class:`InboxApi`
    (operations, two-step cancel tokens, executor and per-key slots)."""

    def __init__(self, inbox, *, projects: Callable[[], Awaitable[list[dict]]] | None = None,
                 monotonic: Callable[[], float] = time.monotonic, token_ttl: float = CANCEL_TOKEN_TTL,
                 version: str | None = None, results=None, busy_wait: float = BUSY_WAIT):
        super().__init__(inbox, projects=projects, monotonic=monotonic, token_ttl=token_ttl, version=version,
                         results=results, busy_wait=busy_wait)
        self._sessions: OrderedDict[str, str] = OrderedDict()  # session id -> key_id

    def _tokens_per_key(self) -> int:
        return TOKENS_PER_KEY

    # -- sessions ---------------------------------------------------------------------------
    def _new_session(self, principal: Principal) -> str:
        session_id = secrets.token_urlsafe(24)
        owner = principal.key_id or ""
        with self._lock:
            own = [sid for sid, key in self._sessions.items() if key == owner]  # oldest first
            for stale in own[:max(0, len(own) - SESSIONS_PER_KEY + 1)]:
                del self._sessions[stale]
            self._sessions[session_id] = owner
            while len(self._sessions) > SESSION_LIMIT:
                self._sessions.popitem(last=False)
        return session_id

    def _session_ok(self, session_id: str, principal: Principal) -> bool:
        with self._lock:
            owner = self._sessions.get(session_id)
        return owner is not None and secrets.compare_digest(owner, principal.key_id or "")

    def _end_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    # -- tools ------------------------------------------------------------------------------
    async def call_tool(self, principal: Principal, tool: Tool, arguments: object) -> dict:
        try:
            value = await self.call(principal, tool, arguments)
        except InboxError as exc:
            return tool_result(error_payload(exc.code, exc.detail, exc.state, exc.version), is_error=True)
        except Exception as exc:  # never the exception text (paths) to the client; the Hub log keeps the type
            print(f"[workbench] mcp {tool.name} failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            return tool_result(error_payload("internal_error"), is_error=True)
        return tool_result(value)

    # -- HTTP -------------------------------------------------------------------------------
    async def handle(self, request: web.Request) -> web.StreamResponse:
        """Origin (403) -> API key (401) -> headers -> method -> body -> one JSON-RPC message."""
        origins = request.headers.getall("Origin", [])
        if origins and (len(origins) != 1 or origin_of_header(origins[0]) is None
                        or origin_of_header(origins[0]) != expected_origin(request)):
            return rpc_error(INVALID_REQUEST, "origin not allowed", status=403)
        if request.content_length is not None and request.content_length > BODY_LIMIT:
            return rpc_error(INVALID_REQUEST, "request body too large", status=413)
        principal = await self.authenticate(request)
        if principal is None:
            return rpc_error(UNAUTHORIZED, "unauthorized", status=401, headers={"WWW-Authenticate": WWW_AUTHENTICATE})
        versions = request.headers.getall(VERSION_HEADER, [])
        if versions and (len(versions) != 1 or versions[0] not in PROTOCOL_VERSIONS):
            return rpc_error(INVALID_REQUEST, "unsupported MCP-Protocol-Version", status=400)
        sessions = request.headers.getall(SESSION_HEADER, [])
        if len(sessions) > 1:
            return rpc_error(INVALID_REQUEST, "more than one Mcp-Session-Id", status=400)
        session_id = sessions[0] if sessions else None
        if request.method == "DELETE":
            if session_id is None:
                return rpc_error(INVALID_REQUEST, "Mcp-Session-Id required", status=400)
            if not self._session_ok(session_id, principal):
                return rpc_error(SESSION_NOT_FOUND, "session not found", status=404)
            self._end_session(session_id)
            return web.Response(status=204)
        if request.method != "POST":  # GET: no server-initiated SSE stream
            return rpc_error(INVALID_REQUEST, "method not allowed", status=405, headers={"Allow": "POST, DELETE"})
        if not _accepts_json(request.headers.getall("Accept", [])):
            return rpc_error(INVALID_REQUEST, "Accept must include application/json", status=406)
        if request.content_type != "application/json":
            return rpc_error(INVALID_REQUEST, "Content-Type must be application/json", status=415)
        try:
            raw = await request.read()
        except web.HTTPRequestEntityTooLarge:
            return rpc_error(INVALID_REQUEST, "request body too large", status=413)
        try:
            message = _parse(raw)
        except _ParseError:
            return rpc_error(PARSE_ERROR, "parse error", status=400)
        if not isinstance(message, dict):  # a batch (array) or a bare value
            return rpc_error(INVALID_REQUEST, "invalid request", status=400)
        if "method" not in message and "id" in message and ("result" in message or "error" in message) \
                and message.get("jsonrpc") == "2.0":
            return web.Response(status=202)  # a client response: nothing to answer
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return rpc_error(INVALID_REQUEST, "invalid request", status=400)
        notification = "id" not in message
        request_id = message.get("id")
        if not notification and (isinstance(request_id, bool) or not isinstance(request_id, (str, int, float))):
            return rpc_error(INVALID_REQUEST, "invalid request id", status=400)
        if method != "initialize" and session_id is not None and not self._session_ok(session_id, principal):
            return rpc_error(SESSION_NOT_FOUND, "session not found", status=404)
        if notification:
            return web.Response(status=202)  # notifications (initialized, cancelled, ...) change nothing
        return await self.in_slot(principal,
                                  lambda: self._answer(principal, method, message, request_id, session_id),
                                  lambda: rpc_error(BUSY, "busy", request_id))


    async def _answer(self, principal: Principal, method: str, message: dict, request_id: object,
                      session_id: str | None) -> web.StreamResponse:
        """One JSON-RPC request of an authenticated key (inside its in-flight slot)."""
        params = message.get("params", {})
        if not isinstance(params, dict):
            return rpc_error(INVALID_PARAMS, "params must be an object", request_id)
        if method == "initialize":
            version = params.get("protocolVersion")
            result = {"protocolVersion": version if isinstance(version, str) and version in PROTOCOL_VERSIONS
                      else PROTOCOL_VERSIONS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": SERVER_NAME, "version": self.version}}
            return _json({"jsonrpc": "2.0", "id": request_id, "result": result},
                         headers={SESSION_HEADER: self._new_session(principal)})
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": [tool.listing() for tool in visible_tools(principal)]})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in TOOLS:
                return rpc_error(INVALID_PARAMS, "unknown tool", request_id)
            return _result(request_id, await self.call_tool(principal, TOOLS[name], params.get("arguments")))
        return rpc_error(METHOD_NOT_FOUND, "method not found", request_id)


def routes(server: McpServer) -> Callable[[web.Application], None]:
    """``create_app(services, routes=[routes(server)])``: ``POST/GET/DELETE /mcp`` (own API key auth)."""
    @own_auth
    async def handler(request: web.Request) -> web.StreamResponse:
        return await server.handle(request)

    def register(app: web.Application) -> None:
        app.router.add_route("*", "/mcp", handler)
    return register


# --- Board agent internal listener (SPEC "Board agent 세트", 기술 보완 14) ---------------------------

INTERNAL_HOST = "127.0.0.1"
FLUSH_INTERVAL = 10.0  # SPEC API key: last use written every 10 s
INTERNAL_MAX_CONNECTIONS = 16  # the Board agent is one client; more is closed at once (security review fix 5)


@dataclass(slots=True)
class _InternalServices:
    """What ``/mcp`` reads from ``app[SERVICES]``: the origin policy, the key store and the clock."""

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
        print("[workbench] board mcp: connection reset by the client during a request", file=sys.stderr, flush=True)
        return web.Response(status=400, text="connection_reset")


def create_internal_app(api_keys: auth.ApiKeyStore, server: McpServer, *, clock: Callable[[], float] = time.time,
                        flush_interval: float = FLUSH_INTERVAL) -> web.Application:
    """``/mcp`` only (every other path 404), Host fixed, same body limit, headers and key auth."""
    app = web.Application(middlewares=[_internal_host, body_limit], client_max_size=BODY_LIMIT)
    app[SERVICES] = _InternalServices(api_keys, clock, flush_interval)
    app.on_response_prepare.append(security_headers)
    app.cleanup_ctx.append(flush_last_used)
    routes(server)(app)
    return app


class InternalListener:
    """The Board agent's loopback MCP listener on ``127.0.0.1:0`` (TI-16 starts and stops it), at most
    ``max_connections`` connections at once (the web listener's :class:`~workbench.hub.web.listener.GuardedSite`)."""

    def __init__(self, api_keys: auth.ApiKeyStore, server: McpServer, *, clock: Callable[[], float] = time.time,
                 flush_interval: float = FLUSH_INTERVAL, max_connections: int = INTERNAL_MAX_CONNECTIONS):
        self.api_keys, self.server, self.clock, self.flush_interval = api_keys, server, clock, flush_interval
        self.max_connections = max_connections
        self.runner: web.AppRunner | None = None
        self.port: int | None = None
        self._lock = asyncio.Lock()  # one start/stop at a time: a second start never opens a second port

    @property
    def url(self) -> str | None:
        return None if self.port is None else f"http://{INTERNAL_HOST}:{self.port}/mcp"

    async def start(self) -> str:
        async with self._lock:
            if self.runner is not None:
                return self.url
            app = create_internal_app(self.api_keys, self.server, clock=self.clock,
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
