"""The Hub's aiohttp application (Task Inbox SPEC "웹 route와 화면", "인증·세션·CSRF").

``create_app(services)`` builds the app the listener serves; tests drive the same factory with
aiohttp's ``TestServer``/``TestClient`` (seam T2). Later tickets add their routes through
``create_app(services, routes=[...])`` with :func:`RouteTable`-style callables.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import sys
import time
import weakref
from typing import Callable, Iterable

from aiohttp import web

from .. import auth
from . import middleware
from .refusals import RefusalLog
from .middleware import (PRINCIPAL, SERVICES, SESSION, api, own_auth, principal_of, public,  # noqa: F401
                         session_of)
from .pages import board as board_page
from .pages import control, login, settings, skill

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Open WebSockets (TI-17 adds each one); the listener closes them with 1001 when the Hub stops.
WEBSOCKETS: web.AppKey = web.AppKey("websockets", weakref.WeakSet)


@dataclass(frozen=True, slots=True)
class OriginPolicy:
    """What ``Host``/``Origin`` the listener accepts (its bind IPs, ``server_names`` and scheme) and which
    client IPs it serves (``allow_from``)."""

    bind: str
    tls: bool = False
    server_names: tuple[str, ...] = ()
    binds: tuple[str, ...] = ()  # every listening address (2026-10-27); empty: ``bind`` alone
    allow_from: tuple = ()  # client networks (``ipaddress``); empty: every client (2026-10-27)
    skill_repo_url: str = ""  # ``[web] skill_repo_url``: the README's "Source repository" line (WBX-B3); empty: none

    @property
    def scheme(self) -> str:
        return "https" if self.tls else "http"

    @property
    def addresses(self) -> tuple[str, ...]:
        return self.binds or (self.bind,)


@dataclass(slots=True)
class WebServices:
    """Everything a handler may use. Blocking calls (scrypt, SQLite, ``auth.json``) go through
    :meth:`blocking`, never straight on the loop."""

    passwords: auth.PasswordStore
    db: auth.HubDb
    policy: OriginPolicy
    insecure_warning: bool = False
    clock: Callable[[], float] = time.time
    # The Board agent's state dot in the nav: ``"on"``/``"off"``/``"error"`` (non-blocking; the Hub supplies it).
    board_state: Callable[[], str] = lambda: "off"
    sessions: auth.SessionStore = field(init=False)
    throttle: auth.LoginThrottle = field(init=False)
    audit: auth.AuditLog = field(init=False)
    api_keys: auth.ApiKeyStore = field(init=False)
    login_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    password_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    throttle_factory: Callable | None = None
    flush_interval: float = 10.0  # API key ``last_used_at`` (SPEC: every 10 s and at Hub stop)
    # ``(project_id, name)`` of the registered projects (blocking read; the Hub supplies the registry view).
    projects: Callable[[], Iterable[tuple[str, str]]] = lambda: ()
    # Called with a logged-out session token, or None after a password change (every session ended); the
    # Board terminal ends those sessions' WebSockets at once (TI-17).
    session_listeners: list = field(default_factory=list)
    # Rate-limited hub.log lines for refused requests (2026-10-27); kept across a listener reopen.
    refusals: RefusalLog = field(default_factory=RefusalLog)

    def __post_init__(self) -> None:
        self.sessions = auth.SessionStore(self.db, self.passwords)
        self.throttle = (self.throttle_factory or auth.LoginThrottle)(self.db, clock=self.clock)
        self.audit = auth.AuditLog(self.db)
        self.api_keys = auth.ApiKeyStore(self.db)

    def sessions_ended(self, token: str | None) -> None:
        for listener in list(self.session_listeners):
            try:
                listener(token)
            except Exception as exc:  # a listener problem never fails the logout
                print(f"[workbench] web: session listener failed: {exc!r}", file=sys.stderr, flush=True)

    async def blocking(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(None, function, *args)


async def flush_last_used(app: web.Application):
    """``cleanup_ctx``: write API key last-use times every ``services.flush_interval`` seconds while the
    app runs (the listener's stop step does the final flush)."""
    services = app[SERVICES]
    in_flight: list[asyncio.Future] = []

    async def loop() -> None:
        while True:
            await asyncio.sleep(services.flush_interval)
            flush = asyncio.ensure_future(services.blocking(services.api_keys.flush))
            in_flight[:] = [flush]
            try:
                await asyncio.shield(flush)  # stopping cancels the timer, never a write in progress
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # kept in memory for the next try; never stops the app
                print(f"[workbench] web: api key last-use flush failed: {exc!r}", file=sys.stderr, flush=True)
    task = asyncio.create_task(loop())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # A write already running finishes before the app (and then ``hub.sqlite3``) closes.
    await asyncio.gather(*in_flight, return_exceptions=True)


BOARD_CHANNELS = ("agent", "shell")


def board_routes(board: Callable[[], object | None]) -> Callable[[web.Application], None]:
    """``POST /board/restart`` (form ``channel``): the logged-in owner restarts one Board channel (TI-16), and
    ``GET /ws/board``, the Board terminal WebSocket (TI-17).

    Session and CSRF are the ``session_check`` middleware's (a page route, unsafe method); API keys cannot reach
    it. ``board()`` returns the Hub's ``BoardSet`` or None (``hub.toml`` invalid). The Board page and the restart
    confirm page (``GET /board``, ``GET /board/restart``) are TI-18's (``pages/board.py``).
    The set's one :class:`~workbench.hub.board.ws.BoardTerminal` (its lease and epochs) outlives a listener
    reopen: it is kept here, per set.
    """
    from ..board import ws as board_ws
    terminals: dict[str, board_ws.BoardTerminal] = {}

    def terminal_for(app: web.Application) -> board_ws.BoardTerminal | None:
        current = board()
        if current is None:
            return None
        terminal = terminals.get("current")
        if terminal is None or terminal.board is not current:
            terminal = terminals["current"] = board_ws.BoardTerminal(current, audit=board_ws.audit_to(app[SERVICES]))
        return terminal

    async def post_restart(request: web.Request) -> web.Response:
        current = board()
        if current is None:
            return web.Response(status=503, text="board_unavailable")
        form = await request.post()
        channel = form.get("channel")
        if channel not in BOARD_CHANNELS:
            return web.Response(status=400, text="bad_channel")
        await current.restart(channel, actor="owner", address=request.remote)
        return middleware.see_other("/board")

    def register(app: web.Application) -> None:
        app.router.add_post("/board/restart", post_restart)
        board_ws.routes(lambda: terminal_for(app))(app)
        board_page.make_register(board)(app)
    return register


def create_app(services: WebServices, *, routes: Iterable[Callable[[web.Application], None]] = ()) -> web.Application:
    app = web.Application(middlewares=[middleware.peer_check, middleware.host_check, middleware.body_limit, middleware.origin_check,
                                       middleware.session_check],
                          client_max_size=middleware.BODY_LIMIT)
    app[SERVICES] = services
    app[WEBSOCKETS] = weakref.WeakSet()
    app.on_response_prepare.append(middleware.security_headers)
    app.cleanup_ctx.append(flush_last_used)
    app.router.add_static(middleware.STATIC_PREFIX, STATIC_DIR, follow_symlinks=False, show_index=False)
    login.register(app)
    settings.make_register(lambda: services.projects())(app)
    control.register(app)
    skill.register(app)
    for register in routes:
        register(app)
    return app
