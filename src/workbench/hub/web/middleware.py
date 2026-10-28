"""Request checks every web route passes (Task Inbox SPEC "인증·세션·CSRF", "보안 header").

Order, outermost first: security headers (on every response, also errors) -> client IP ``allow_from``
(bare 403, 2026-10-27) -> ``Host`` (421,
DNS rebinding) -> body size (413) -> ``Origin``/``Referer`` on state changes (403) -> session
(page ``303 /login``, API 401) and CSRF on state changes (403 ``csrf_failed``).
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets
from urllib.parse import urlsplit

from aiohttp import web

from ..config import _server_name, is_loopback
from ..inbox.service import Principal
from .refusals import printable, sanitize

# The app's ``WebServices`` (defined in ``app``, which imports this module).
SERVICES: web.AppKey = web.AppKey("services", object)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
SECURITY_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY"}
STATIC_PREFIX = "/static/"
BODY_LIMIT = 256 * 1024  # SPEC 입력 한도: HTTP 요청 body (form, ``/mcp``) -> 413
STATIC_CACHE = "public, max-age=3600"
SKILL_PREFIX = "/skill/"  # WBX-B3: the served text differs per Host, so it is always revalidated
SKILL_CACHE = "no-cache"
SESSION_PAGE_CACHE = "private, no-cache"
NO_STORE = "no-store"

SESSION_COOKIE = "wb_hub_session"
SESSION_MAX_AGE = 86400
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
API_PREFIXES = ("/api/", "/ws/")
# Request keys a handler reads after the session middleware.
SESSION: web.RequestKey = web.RequestKey("wb_session", object)
SESSION_TOKEN: web.RequestKey = web.RequestKey("wb_session_token", str)
PRINCIPAL: web.RequestKey = web.RequestKey("wb_principal", Principal)
STYLE_NONCE: web.RequestKey = web.RequestKey("wb_style_nonce", str)

# Route access marks (attribute ``wb_access`` on the handler): unmarked routes need a session.
ACCESS_PUBLIC = "public"  # no session (login, static)
ACCESS_API = "api"  # session; refusals are 401 instead of ``303 /login``
ACCESS_OWN = "own"  # the handler authenticates by itself (``/mcp`` with an API key, TI-05)


def inline_styles(handler):
    """Mark one page handler whose 200 response may carry nonced ``<style>`` elements (see :func:`csp_for`)."""
    handler.wb_inline_styles = True
    return handler


def style_nonce(request: web.Request) -> str:
    """This request's CSP style nonce: made once per request (``secrets.token_urlsafe(16)``), then reused, so the
    page body and the response's CSP carry the same value."""
    nonce = request.get(STYLE_NONCE)
    if nonce is None:
        nonce = request[STYLE_NONCE] = secrets.token_urlsafe(16)
    return nonce


def public(handler):
    handler.wb_access = ACCESS_PUBLIC
    return handler


def api(handler):
    handler.wb_access = ACCESS_API
    return handler


def own_auth(handler):
    handler.wb_access = ACCESS_OWN
    return handler


def csp_for(request: web.Request, response: web.StreamResponse) -> str:
    """The CSP of this response: the strict global one, except for a 200 from a route marked
    :func:`inline_styles` (the Board page only).

    xterm.js 5.5's DOM renderer creates two ``<style>`` elements and fills them with ``textContent``, and writes
    24-bit colours (and ``minimumContrastRatio`` colours) through ``setAttribute("style", ...)``; under ``style-src
    'self'`` the browser refuses both and the terminal draws nothing or loses its colours. The Board page therefore
    gets ``style-src 'self' 'nonce-<N>'; style-src-attr 'unsafe-inline'`` (2026-10-26 사용자 결정; it replaces the
    TI-18 ``style-src 'self' 'unsafe-inline'``). ``N`` is :func:`style_nonce`, fresh per request; the page hands it
    to ``static/style-nonce.js``, which sets it on the ``<style>`` elements xterm creates. Injected ``<style>``
    markup without the nonce stays refused; ``style-src-attr`` stays only because xterm's truecolor cells have no
    other route. Nothing else changes: ``script-src 'self'`` stays, and no other page, error, redirect or static
    file is loosened. Style attributes alone cannot run script; the page holds no user-controlled HTML (terminal
    output is written through xterm, not as markup).
    """
    marked = getattr(request.match_info.handler, "wb_inline_styles", False)
    if marked and response.status == 200:
        return CSP.replace("style-src 'self'", f"style-src 'self' 'nonce-{style_nonce(request)}'; "
                           "style-src-attr 'unsafe-inline'", 1)
    return CSP


async def security_headers(request: web.Request, response: web.StreamResponse) -> None:
    """``on_response_prepare``: the SPEC headers on every response (errors and 421 included)."""
    headers = response.headers
    headers.update(SECURITY_HEADERS)
    headers["Content-Security-Policy"] = csp_for(request, response)
    headers["Server"] = "wb-hub"
    headers["Cache-Control"] = cache_control(request, response)


# SPEC exceptions to the logged-in page rule, as ``(method, route pattern)``: every response of these (also a refusal
# or error page) is ``no-store``. ``POST /settings/keys`` shows the one-time raw key; the request result views show
# raw logs, reports and project files.
NO_STORE_ROUTES = frozenset({("POST", "/settings/keys")} | {
    (method, f"/requests/{{rid}}/{view}") for method in ("GET", "HEAD") for view in ("raw", "full", "artifact")})


def route_of(request: web.Request) -> str | None:
    """The pattern of the route this request matched (``/requests/{rid}/raw``), ``None`` for an unmatched path."""
    resource = getattr(request.match_info.route, "resource", None)
    return None if resource is None else resource.canonical


def cache_control(request: web.Request, response: web.StreamResponse) -> str:
    """The web-board-ux SPEC "Cache 정책과 bfcache" table (WBX-D02). A logged-in page's HTML may be kept for the
    back/forward cache (``private, no-cache``: never shared, always revalidated); everything else that is not a
    static file stays ``no-store``."""
    if request.path.startswith(STATIC_PREFIX) and response.status == 200:
        return STATIC_CACHE
    if (request.path.startswith(SKILL_PREFIX) or request.path == SKILL_PREFIX[:-1]) and response.status in (200, 404):
        return SKILL_CACHE
    if (request.method, route_of(request)) in NO_STORE_ROUTES:
        return NO_STORE
    if (session_of(request) is not None and access_of(request) == "page" and response.content_type == "text/html"
            and not 300 <= response.status < 400):
        return SESSION_PAGE_CACHE
    return NO_STORE


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


def _policy_addresses(policy) -> tuple[str, ...]:
    return getattr(policy, "addresses", None) or (policy.bind,)


def host_allowed(policy, host: object, port: int, listening_port: int) -> bool:
    """SPEC Host rule: ``<bind IP>:<port>`` (any of the bind IPs), ``<server_name>:<port>``,
    ``localhost:<port>`` when a bind IP is loopback; an unspecified bind (``0.0.0.0``/``::``) takes
    every IP literal."""
    if port != listening_port:
        return False
    addresses = _policy_addresses(policy)
    if isinstance(host, str):
        return host in policy.server_names or (host == "localhost" and any(map(is_loopback, addresses)))
    binds = [ipaddress.ip_address(address) for address in addresses]
    if any(bind.is_unspecified for bind in binds):
        return True
    return host in binds


def peer_address(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The peer IP of a transport ``peername`` host, an IPv4-mapped IPv6 one as IPv4; ``None`` when unreadable."""
    if not isinstance(value, str) or not value or "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def peer_allowed(allow_from, peer: object) -> bool:
    """2026-10-27 ``allow_from``: an empty list allows every peer; otherwise the peer (IPv4-mapped as IPv4)
    must be in a listed network, and an unknown peer is refused."""
    if not allow_from:
        return True
    address = peer_address(peer)
    return address is not None and any(address in network for network in allow_from)


def peer_of(request: web.Request) -> str | None:
    """The TCP peer host of this request's connection: never a header (``X-Forwarded-For`` and the like)."""
    transport = request.transport
    peername = transport.get_extra_info("peername") if transport is not None else None
    return peername[0] if isinstance(peername, tuple) and peername else None


@web.middleware
async def peer_check(request: web.Request, handler):
    """Outermost check (2026-10-27): a peer outside ``allow_from`` gets a bare 403 before Host, session, CSRF,
    throttle or any handler, on every route (pages, static, ``/mcp``, the ``/ws/board`` upgrade)."""
    services = request.app[SERVICES]
    allow_from = services.policy.allow_from
    if allow_from:
        peer = peer_of(request)
        if not peer_allowed(allow_from, peer):
            shown = str(peer_address(peer) or sanitize(peer))
            services.refusals.note("allow_from", shown, f"[workbench] web: refused client {shown} on "
                                   f"{_listening_text(request)} (403 allow_from); not in [web] allow_from")
            return web.Response(status=403)
    return await handler(request)


def _listening_text(request: web.Request) -> str:
    """``<address>:<port>`` the request arrived on (``[v6]:port``)."""
    transport = request.transport
    sockname = transport.get_extra_info("sockname") if transport is not None else None
    if not (isinstance(sockname, tuple) and len(sockname) >= 2):
        return "(unknown)"
    host = str(sockname[0])
    return f"[{host}]:{sockname[1]}" if ":" in host else f"{host}:{sockname[1]}"


HOST_HINT = "allowed: bind IP, server_names, localhost on loopback bind"


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
        shown = sanitize(hosts[0]) if len(hosts) == 1 else ("(none)" if not hosts else "(multiple)")
        request.app[SERVICES].refusals.note(
            "host", shown, f"[workbench] web: refused Host {shown} on {_listening_text(request)} "
            f"(421 misdirected_request); {HOST_HINT}")
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
    """413 for a declared body over the limit. Own-auth routes (``/mcp``) answer it themselves in their
    own error shape; ``client_max_size`` still caps every body that is read."""
    if access_of(request) == ACCESS_OWN:
        return await handler(request)
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


REST_PREFIX = "/api/v1/"
# The fixed sentences of the refusals a REST request can get from these checks (web-board-ux SPEC "외부 요청 API").
REST_MESSAGES = {"origin_mismatch": "origin not allowed"}


def refuse(request: web.Request, status: int, reason: str, *, access: str | None = None) -> web.Response:
    if request.path.startswith(REST_PREFIX) or request.path == REST_PREFIX[:-1]:  # REST error shape (WBX-B1)
        return web.json_response({"error": {"code": reason, "message": REST_MESSAGES.get(reason, reason)}},
                                 status=status)
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
        peer, path = printable(peer_of(request)), printable(request.path)
        kind = "API" if access == ACCESS_API else "form"
        services.refusals.note("csrf", f"{peer} {request.method} {path}",
                               f"[workbench] web: refused {kind} {printable(request.method)} {path} from {peer} "
                               "(403 csrf_failed: the page's CSRF value is not this session's; a stale page)")
        if access == ACCESS_API:
            return refuse(request, 403, "csrf_failed", access=access)
        return stale_form_page(request)
    return await handler(request)


STALE_FORM_TEXT = "화면이 오래되었습니다. 새로 고친 뒤 다시 시도하세요."


def back_link(request: web.Request) -> str:
    """The same-origin ``Referer`` path (with its query) to go back to, else the dashboard (``render.HOME_PATH``)."""
    from .render import HOME_PATH
    referers = request.headers.getall("Referer", [])
    if len(referers) != 1 or origin_of_referer(referers[0]) != expected_origin(request):
        return HOME_PATH
    parts = urlsplit(referers[0])
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    return target if target.startswith("/") and not target.startswith("//") else HOME_PATH


def stale_form_page(request: web.Request) -> web.Response:
    """A logged-in page form whose CSRF value is not the session's (an old tab, a page from the cache): a small
    Korean 403 page with a link back instead of the bare ``csrf_failed`` text (2026-10-27). Nothing changed."""
    from . import render
    body = render.html_join([
        render.tag("h1", "다시 시도하세요"),
        render.tag("p", STALE_FORM_TEXT, class_="error", role="alert"),
        render.tag("p", render.tag("a", "돌아가기", href=back_link(request))),
        render.tag("p", render.tag("code", "csrf_failed"))])
    return render.page("화면이 오래되었습니다", body, status=403, scripts=(render.SESSION_CHECK_SCRIPT,),
                       insecure_warning=request.app[SERVICES].insecure_warning)
