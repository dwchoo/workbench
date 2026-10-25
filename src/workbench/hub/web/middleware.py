"""Request checks every web route passes (Task Inbox SPEC "인증·세션·CSRF", "보안 header").

Order, outermost first: security headers (on every response, also errors) -> ``Host`` (421,
DNS rebinding) -> body size (413) -> ``Origin``/``Referer`` on state changes (403) -> session
(page ``303 /login``, API 401) and CSRF on state changes (403 ``csrf_failed``).
"""
from __future__ import annotations

import hmac
import ipaddress
from urllib.parse import urlsplit

from aiohttp import web

from ..config import _server_name, is_loopback
from ..inbox.service import Principal

# The app's ``WebServices`` (defined in ``app``, which imports this module).
SERVICES: web.AppKey = web.AppKey("services", object)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
SECURITY_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}
STATIC_PREFIX = "/static/"
BODY_LIMIT = 256 * 1024  # SPEC 입력 한도: HTTP 요청 body (form, ``/mcp``) -> 413
STATIC_CACHE = "public, max-age=3600"

SESSION_COOKIE = "wb_hub_session"
SESSION_MAX_AGE = 86400
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
API_PREFIXES = ("/api/", "/ws/")
# Request keys a handler reads after the session middleware.
SESSION: web.RequestKey = web.RequestKey("wb_session", object)
SESSION_TOKEN: web.RequestKey = web.RequestKey("wb_session_token", str)
PRINCIPAL: web.RequestKey = web.RequestKey("wb_principal", Principal)

# Route access marks (attribute ``wb_access`` on the handler): unmarked routes need a session.
ACCESS_PUBLIC = "public"  # no session (login, static)
ACCESS_API = "api"  # session; refusals are 401 instead of ``303 /login``
ACCESS_OWN = "own"  # the handler authenticates by itself (``/mcp`` with an API key, TI-05)


def public(handler):
    handler.wb_access = ACCESS_PUBLIC
    return handler


def api(handler):
    handler.wb_access = ACCESS_API
    return handler


def own_auth(handler):
    handler.wb_access = ACCESS_OWN
    return handler


async def security_headers(request: web.Request, response: web.StreamResponse) -> None:
    """``on_response_prepare``: the SPEC headers on every response (errors and 421 included)."""
    headers = response.headers
    headers.update(SECURITY_HEADERS)
    headers["Server"] = "wb-hub"
    if request.path.startswith(STATIC_PREFIX) and response.status == 200:
        headers["Cache-Control"] = STATIC_CACHE
    else:
        headers["Cache-Control"] = "no-store"


def parse_authority(text: str, default_port: int) -> tuple[object, int] | None:
    """``(host, port)`` of a ``Host``-style authority: an ``ip_address`` or a lower-case DNS name.

    Only the canonical forms a browser sends pass: bracketed IPv6, dotted IPv4 without leading
    zeros, a DNS name, and an optional decimal port without leading zeros.
    """
    if not isinstance(text, str) or not text or not text.isascii():
        return None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return None
        name, rest = text[1:end], text[end + 1:]
        if "%" in name:
            return None
        try:
            host: object = ipaddress.IPv6Address(name)
        except ValueError:
            return None
    else:
        name, colon, port_text = text.partition(":")
        rest = colon + port_text
        try:
            host = ipaddress.IPv4Address(name)
        except ValueError:
            host = _server_name(name)
            if host is None or host.replace(".", "").isdigit():
                return None
    if not rest:
        return host, default_port
    digits = rest[1:]
    if not rest.startswith(":") or not digits.isdigit() or digits != str(int(digits)) or not 0 < int(digits) < 65536:
        return None
    return host, int(digits)


def host_allowed(policy, host: object, port: int, listening_port: int) -> bool:
    """SPEC Host rule: ``<bind IP>:<port>``, ``<server_name>:<port>``, ``localhost:<port>`` on a
    loopback bind; an unspecified bind (``0.0.0.0``/``::``) takes every IP literal."""
    if port != listening_port:
        return False
    if isinstance(host, str):
        return host in policy.server_names or (host == "localhost" and is_loopback(policy.bind))
    bind = ipaddress.ip_address(policy.bind)
    if bind.is_unspecified:
        return True
    return host == bind


def _listening_port(request: web.Request) -> int | None:
    transport = request.transport
    sockname = transport.get_extra_info("sockname") if transport is not None else None
    return sockname[1] if isinstance(sockname, tuple) and len(sockname) >= 2 else None


@web.middleware
async def host_check(request: web.Request, handler):
    policy = request.app[SERVICES].policy
    hosts = request.headers.getall("Host", [])
    listening = _listening_port(request)
    authority = parse_authority(hosts[0], 443 if policy.tls else 80) if len(hosts) == 1 else None
    if listening is None or authority is None or not host_allowed(policy, *authority, listening):
        return web.Response(status=421, text="misdirected_request")
    return await handler(request)


def _origin_tuple(scheme: str, authority: str) -> tuple | None:
    scheme = scheme.lower()
    if scheme not in ("http", "https"):
        return None
    parsed = parse_authority(authority, 443 if scheme == "https" else 80)
    return None if parsed is None else (scheme, *parsed)


def origin_of_header(value: str) -> tuple | None:
    """``(scheme, host, port)`` of an ``Origin`` value (``scheme://authority``, nothing after)."""
    scheme, sep, authority = value.partition("://")
    if not sep or "/" in authority:
        return None
    return _origin_tuple(scheme, authority)


def origin_of_referer(value: str) -> tuple | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if "@" in parts.netloc:
        return None
    return _origin_tuple(parts.scheme, parts.netloc)


def expected_origin(request: web.Request) -> tuple | None:
    """``<scheme>://<Host>`` of this (already Host-checked) request as ``(scheme, host, port)``."""
    policy = request.app[SERVICES].policy
    return _origin_tuple(policy.scheme, request.headers.get("Host", ""))


@web.middleware
async def body_limit(request: web.Request, handler):
    if request.content_length is not None and request.content_length > BODY_LIMIT:
        return refuse(request, 413, "too_large")
    return await handler(request)


@web.middleware
async def origin_check(request: web.Request, handler):
    """State changes and WebSocket upgrades: a present ``Origin`` (else ``Referer``) must be this
    origin (403 otherwise)."""
    if request.method in UNSAFE_METHODS or request.headers.get("Upgrade", "").lower() == "websocket":
        origins = request.headers.getall("Origin", [])
        referers = request.headers.getall("Referer", [])
        if len(origins) > 1 or len(referers) > 1:
            return refuse(request, 403, "origin_mismatch")
        expected = expected_origin(request)
        if origins:
            seen = origin_of_header(origins[0])
        elif referers:
            seen = origin_of_referer(referers[0])
        else:
            seen = expected
        if expected is None or seen != expected:
            return refuse(request, 403, "origin_mismatch")
    return await handler(request)


CSRF_FIELD = "csrf"
CSRF_HEADER = "X-CSRF-Token"
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


async def csrf_token_of(request: web.Request) -> str | None:
    header = request.headers.getall(CSRF_HEADER, [])
    if len(header) == 1:
        return header[0]
    if header or request.content_type not in FORM_TYPES:
        return None
    form = await request.post()
    values = form.getall(CSRF_FIELD, [])
    return values[0] if len(values) == 1 and isinstance(values[0], str) else None


def csrf_matches(given: object, expected: str) -> bool:
    return (isinstance(given, str) and bool(given) and isinstance(expected, str)
            and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")))


def access_of(request: web.Request) -> str:
    """``public``/``own``/``api``/``page`` for this request's route (unknown paths are pages)."""
    if request.path.startswith(STATIC_PREFIX):
        return ACCESS_PUBLIC
    marked = getattr(request.match_info.handler, "wb_access", None)
    if marked is not None:
        return marked
    return ACCESS_API if request.path.startswith(API_PREFIXES) else "page"


def session_of(request: web.Request):
    """The verified :class:`workbench.hub.auth.Session` of this request, or ``None``."""
    return request.get(SESSION)


def principal_of(request: web.Request) -> Principal | None:
    """``Principal.owner()`` for a logged-in session; ``None`` otherwise (API-key routes resolve their own)."""
    return request.get(PRINCIPAL)


def refuse(request: web.Request, status: int, reason: str, *, access: str | None = None) -> web.Response:
    access = access or access_of(request)
    if access in (ACCESS_API, ACCESS_OWN):
        return web.json_response({"ok": False, "reason": reason}, status=status)
    return web.Response(status=status, text=reason)


def see_other(location: str) -> web.Response:
    return web.Response(status=303, headers={"Location": location})


def set_session_cookie(response: web.StreamResponse, token: str, *, secure: bool, max_age: int = SESSION_MAX_AGE) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, path="/", httponly=True, samesite="Strict",
                        secure=secure)


def clear_session_cookie(response: web.StreamResponse, *, secure: bool) -> None:
    response.set_cookie(SESSION_COOKIE, "", max_age=0, path="/", httponly=True, samesite="Strict", secure=secure)


@web.middleware
async def session_check(request: web.Request, handler):
    """Resolve the session cookie; pages without one go to ``303 /login``, APIs get 401."""
    access = access_of(request)
    if request.path.startswith(STATIC_PREFIX):
        return await handler(request)
    services = request.app[SERVICES]
    token = request.cookies.get(SESSION_COOKIE)
    session = None
    if token:
        session = await services.blocking(services.sessions.verify, token, float(services.clock()))
    if session is not None:
        request[SESSION], request[SESSION_TOKEN], request[PRINCIPAL] = session, token, Principal.owner()
    if access in (ACCESS_PUBLIC, ACCESS_OWN):
        return await handler(request)
    if session is None:
        if access == ACCESS_API:
            return refuse(request, 401, "unauthorized", access=access)
        response = see_other("/login")
        if token:
            clear_session_cookie(response, secure=services.policy.tls)
        return response
    if request.method in UNSAFE_METHODS and not csrf_matches(await csrf_token_of(request), session.csrf):
        return refuse(request, 403, "csrf_failed", access=access)
    return await handler(request)
