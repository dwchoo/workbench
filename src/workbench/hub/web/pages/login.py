"""``GET/POST /login``, ``POST /logout``, ``GET /settings`` + ``POST /settings/password``
(Task Inbox SPEC "인증·세션·CSRF", "로그인 실패 지연").

Login uses a pre-session double-submit token (``wb_hub_login`` cookie + hidden field). Password
verification is Hub-wide one at a time (``services.login_lock``) and runs in an executor. A client
that must still wait gets ``429`` with ``Retry-After`` at once: nothing ever sleeps, so a LAN
attacker cannot make the owner queue behind a sleeping lock holder.
"""
from __future__ import annotations

import hmac
import math
import secrets

from aiohttp import web

from ... import auth
from .. import render
from ..middleware import (SERVICES, SESSION_TOKEN, clear_session_cookie, public, refuse, see_other,
                          session_of, set_session_cookie)

LOGIN_COOKIE = "wb_hub_login"
LOGIN_COOKIE_MAX_AGE = 3600
LOGIN_FIELD = "login_token"
WRONG_PASSWORD = "비밀번호가 맞지 않습니다"
WAIT_TEXT = "로그인 시도가 너무 잦습니다. {seconds}초 뒤 다시 시도하세요."
USER_AGENT_LIMIT = 256


def login_form(token: str, *, error: str | None = None) -> render.Markup:
    parts = [render.tag("h1", "로그인")]
    if error:
        parts.append(render.tag("p", error, class_="error", role="alert"))
    parts.append(render.tag("form", render.html_join([
        render.hidden(LOGIN_FIELD, token),
        render.tag("label", render.html_join(["비밀번호 ", render.void(
            "input", type="password", name="password", autocomplete="current-password", required=True,
            autofocus=True)])),
        render.tag("button", "로그인", type="submit")]), method="post", action="/login", class_="stack"))
    return render.html_join(parts)


def login_page(request: web.Request, *, status: int = 200, error: str | None = None,
               headers: dict | None = None) -> web.Response:
    """The login form with a fresh double-submit token (cookie and hidden field)."""
    services = request.app[SERVICES]
    token = secrets.token_urlsafe(32)
    response = render.page("로그인", login_form(token, error=error), status=status,
                           insecure_warning=services.insecure_warning)
    if headers:
        response.headers.update(headers)
    response.set_cookie(LOGIN_COOKIE, token, max_age=LOGIN_COOKIE_MAX_AGE, path="/login", httponly=True,
                        samesite="Strict", secure=services.policy.tls)
    return response


def _same(left: object, right: object) -> bool:
    return (isinstance(left, str) and isinstance(right, str) and bool(left) and bool(right)
            and hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8")))


@public
async def get_login(request: web.Request) -> web.Response:
    if session_of(request) is not None:
        return see_other("/")
    return login_page(request)


def _too_many(request: web.Request, wait: float) -> web.Response:
    seconds = max(1, math.ceil(wait))
    return login_page(request, status=429, error=WAIT_TEXT.format(seconds=seconds),
                      headers={"Retry-After": str(seconds)})


async def verify_password(request: web.Request, password: object, *, actor: str = "anonymous"):
    """The throttled, serialized password check shared by login and the web password change.

    Returns the matched generation, or a ready response (429 wait, 403 wrong password, 503).
    A wrong password counts as a login failure and is audited ``login_failure``.
    """
    services = request.app[SERVICES]
    throttle, address = services.throttle, request.remote
    if not throttle.try_enter():
        return _too_many(request, 1)
    try:
        async with services.login_lock:
            now = float(services.clock())
            wait = await services.blocking(throttle.check, address, now)
            if wait > 0:
                return _too_many(request, wait)
            delay = await services.blocking(throttle.delay_for, address)
            try:
                generation = await services.blocking(services.passwords.verify,
                                                     password if isinstance(password, str) else "")
            except auth.AuthInvalid:
                return web.Response(status=503, text="password_unavailable")
            if generation is None:
                await services.blocking(throttle.record_failure, address, now)
                await services.blocking(lambda: services.audit.record(
                    "login_failure", actor, address, {"delay_seconds": delay}, now=now))
                return None, delay
            await services.blocking(throttle.record_success, address)
            return generation, delay
    finally:
        throttle.leave()


@public
async def post_login(request: web.Request) -> web.Response:
    services = request.app[SERVICES]
    form = await request.post()
    if not _same(request.cookies.get(LOGIN_COOKIE), form.get(LOGIN_FIELD)):
        return refuse(request, 403, "csrf_failed")
    outcome = await verify_password(request, form.get("password"))
    if isinstance(outcome, web.StreamResponse):
        return outcome
    generation, delay = outcome
    if generation is None:
        return login_page(request, status=403, error=WRONG_PASSWORD)
    now = float(services.clock())
    address = request.remote
    agent = request.headers.get("User-Agent", "")[:USER_AGENT_LIMIT]
    issued = await services.blocking(lambda: services.sessions.create(address, agent, now, generation=generation))
    await services.blocking(lambda: services.audit.record("login_success", "owner", address,
                                                          {"delay_seconds": delay}, now=now))
    response = see_other("/")
    set_session_cookie(response, issued.token, secure=services.policy.tls)
    response.del_cookie(LOGIN_COOKIE, path="/login")
    return response


async def post_logout(request: web.Request) -> web.Response:
    services = request.app[SERVICES]
    now = float(services.clock())
    await services.blocking(services.sessions.delete, request[SESSION_TOKEN])
    await services.blocking(lambda: services.audit.record("logout", "owner", request.remote,
                                                          {"delay_seconds": 0}, now=now))
    response = see_other("/login")
    clear_session_cookie(response, secure=services.policy.tls)
    return response


async def get_home(request: web.Request) -> web.Response:
    """Placeholder status board (TI-12 draws the real one)."""
    services = request.app[SERVICES]
    return render.page("", render.tag("h1", "WB Hub"), insecure_warning=services.insecure_warning,
                       csrf=session_of(request).csrf)


MISMATCH_TEXT = "새 비밀번호 두 칸이 서로 다릅니다"
LENGTH_TEXT = "비밀번호는 10~1024자여야 합니다"


def password_form(*, error: str | None = None, csrf: str) -> render.Markup:
    """The web password change form (TI-07 places it in the full settings page)."""
    parts = [render.tag("h1", "설정"), render.tag("h2", "비밀번호 변경")]
    if error:
        parts.append(render.tag("p", error, class_="error", role="alert"))

    def field(label: str, name: str, autocomplete: str) -> render.Markup:
        return render.tag("label", render.html_join([label + " ", render.void(
            "input", type="password", name=name, autocomplete=autocomplete, required=True)]))
    parts.append(render.tag("form", render.html_join([
        render.csrf_field(csrf), field("현재 비밀번호", "current", "current-password"),
        field("새 비밀번호", "new", "new-password"), field("새 비밀번호 확인", "confirm", "new-password"),
        render.tag("button", "바꾸기", type="submit")]), method="post", action="/settings/password", class_="stack"))
    return render.html_join(parts)


def settings_page(request: web.Request, *, status: int = 200, error: str | None = None) -> web.Response:
    services, session = request.app[SERVICES], session_of(request)
    return render.page("설정", password_form(error=error, csrf=session.csrf), status=status,
                       insecure_warning=services.insecure_warning, csrf=session.csrf)


async def get_settings(request: web.Request) -> web.Response:
    return settings_page(request)


def _field(form, name: str) -> str:
    value = form.get(name, "")
    return value if isinstance(value, str) else ""


async def post_password(request: web.Request) -> web.Response:
    """Current password (same delay and audit as a login failure), then ``generation + 1``: this
    session is reissued for the new generation, every other session is deleted."""
    services = request.app[SERVICES]
    form = await request.post()
    current, new, confirm = _field(form, "current"), _field(form, "new"), _field(form, "confirm")
    if new != confirm:
        return settings_page(request, status=400, error=MISMATCH_TEXT)
    if auth.password_problem(new) is not None:
        return settings_page(request, status=400, error=LENGTH_TEXT)
    outcome = await verify_password(request, current, actor="owner")
    if isinstance(outcome, web.StreamResponse):
        return outcome
    generation, _delay = outcome
    if generation is None:
        return settings_page(request, status=403, error=WRONG_PASSWORD)
    token, address = request[SESSION_TOKEN], request.remote
    async with services.password_lock:  # the auth.json watch never audits this change as ``cli``
        try:
            new_generation = await services.blocking(services.passwords.set_password, new)
        except auth.PasswordRejected:
            return settings_page(request, status=400, error=LENGTH_TEXT)
        now = float(services.clock())
        reissued = await services.blocking(
            lambda: services.sessions.reissue(token, now, expected_generation=generation))
        invalidated = None if reissued is None else reissued.invalidated
        await services.blocking(lambda: services.audit.note_password_generation(
            new_generation, "web", now=now, invalidated=invalidated, client_addr=address))
    if reissued is None:  # another change slipped in between: log in again with the newest password
        response = see_other("/login")
        clear_session_cookie(response, secure=services.policy.tls)
        return response
    response = see_other("/settings")
    remaining = max(1, int(reissued.session.expires_at - now))
    set_session_cookie(response, reissued.session.token, secure=services.policy.tls, max_age=remaining)
    return response


def register(app: web.Application) -> None:
    app.router.add_get("/login", get_login)
    app.router.add_post("/login", post_login)
    app.router.add_post("/logout", post_logout)
    app.router.add_get("/", get_home)
    app.router.add_get("/settings", get_settings)
    app.router.add_post("/settings/password", post_password)
