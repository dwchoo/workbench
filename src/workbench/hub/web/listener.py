"""Web listener lifetime inside the Hub loop (Task Inbox SPEC "Web listener 수명").

The listener opens only when ``hub.toml`` is valid, ``auth.json`` holds a valid password and the
TLS decision is ``tls``/``insecure_ok``. Until then (and on failure) ``status()`` names why. The
Hub watchdog calls :meth:`WebListener.poll` every 2 s: a new ``auth.json`` opens the listener
without a Hub restart, a failed bind is retried every :data:`BIND_RETRY` seconds.
"""
from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Callable, Iterable

from aiohttp import WSCloseCode, web

from .. import auth
from ..config import ConfigInvalid, HubConfig, TlsDecision, insecure_warning
from .app import WEBSOCKETS, OriginPolicy, WebServices, create_app

BIND_RETRY = 30.0
SHUTDOWN_TIMEOUT = 3.0
KEEPALIVE_TIMEOUT = 15.0  # also closes a connection whose request headers never finish
MAX_CONNECTIONS = 256
TLS_HANDSHAKE_TIMEOUT = 10.0
RETRIED_STATES = frozenset({"bind_failed", "hub_db_invalid"})  # transient: retried every BIND_RETRY s


def _log(text: str) -> None:
    print(f"[workbench] web: {text}", file=sys.stderr, flush=True)


def _signature(path: Path) -> tuple | None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


def listen_url(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://[{host}]:{port}" if ":" in host else f"{scheme}://{host}:{port}"


class _Refuse(asyncio.Protocol):
    """Over the connection cap: close at once without reading anything."""

    def connection_made(self, transport) -> None:
        transport.abort()


class GuardedSite(web.BaseSite):
    """``TCPSite`` with a connection cap and a TLS handshake timeout (aiohttp has neither)."""

    __slots__ = ("_host", "_port", "_max_connections")

    def __init__(self, runner: web.BaseRunner, host: str, port: int, *, ssl_context=None,
                 max_connections: int = MAX_CONNECTIONS):
        super().__init__(runner, ssl_context=ssl_context)
        self._host, self._port, self._max_connections = host, port, max_connections

    @property
    def name(self) -> str:
        return listen_url("https" if self._ssl_context else "http", self._host, self._port)

    async def start(self) -> None:
        await super().start()
        server, limit = self._runner.server, self._max_connections

        def factory():
            return _Refuse() if len(server.connections) >= limit else server()
        self._server = await asyncio.get_running_loop().create_server(
            factory, self._host, self._port, ssl=self._ssl_context, backlog=self._backlog, reuse_address=True,
            ssl_handshake_timeout=TLS_HANDSHAKE_TIMEOUT if self._ssl_context else None)


class WebListener:
    def __init__(self, state_dir: Path, *, config: HubConfig | None, config_error: ConfigInvalid | None,
                 tls: TlsDecision | None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, bind_retry: float = BIND_RETRY,
                 routes=(), keepalive_timeout: float = KEEPALIVE_TIMEOUT, max_connections: int = MAX_CONNECTIONS):
        self.state_dir = Path(state_dir)
        self.config, self.config_error, self.tls = config, config_error, tls
        self.passwords = auth.PasswordStore(self.state_dir)
        self.clock, self.monotonic, self.bind_retry = clock, monotonic, bind_retry
        self.routes = tuple(routes)
        self.projects: Callable[[], Iterable[tuple[str, str]]] = lambda: ()  # registry view; the Hub sets it
        self.keepalive_timeout, self.max_connections = keepalive_timeout, max_connections
        self._state: dict = {"state": "password_not_set"}
        self._signature: tuple | None = None
        self._retry_at: float | None = None
        self.db: auth.HubDb | None = None
        self.services: WebServices | None = None
        self.runner: web.AppRunner | None = None
        self.stopping = False
        # Extension point of the stop order (Board agent set end, TI-16): awaited after WebSockets close.
        self.stop_hooks: list[Callable] = []

    def status(self) -> dict:
        return dict(self._state)

    @property
    def listening(self) -> bool:
        return self._state.get("state") == "listening"

    def _password_state(self) -> tuple[dict | None, int | None]:
        """(closed state or ``None``, current generation)."""
        try:
            record = self.passwords.load()
        except auth.AuthInvalid:
            return {"state": "password_not_set", "detail": "auth_invalid"}, None
        if record is None:
            return {"state": "password_not_set"}, None
        return None, record.generation

    def _open_db(self) -> auth.HubDb:
        if self.db is None:
            self.db = auth.HubDb.open(self.state_dir)
        return self.db

    async def _blocking(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(None, function, *args)

    async def start(self) -> None:
        if self.config_error is not None:
            self._state = {"state": "config_invalid", "detail": self.config_error.item}
            return
        if not self.tls.opens:
            self._state = {"state": self.tls.state, **({"detail": self.tls.detail} if self.tls.detail else {})}
            return
        await self._evaluate()

    async def poll(self) -> None:
        """One watchdog tick: react to a changed ``auth.json`` and retry a failed bind when due."""
        if self.stopping or self.config_error is not None or not self.tls.opens:
            return
        signature = await self._blocking(_signature, self.passwords.path)
        changed = signature != self._signature
        retry_due = (self._state.get("state") in RETRIED_STATES and self._retry_at is not None
                     and self.monotonic() >= self._retry_at)
        if changed or retry_due:
            await self._evaluate()

    async def _evaluate(self) -> None:
        self._signature = await self._blocking(_signature, self.passwords.path)
        closed, generation = await self._blocking(self._password_state)
        if generation is not None:
            await self._note_generation()
        if self.listening:
            return
        if closed is not None:
            self._state = closed
            return
        await self._open()

    async def _note_generation(self) -> None:
        """A password generation the audit has not seen yet is a ``wb hub passwd`` (SPEC 감사).

        The generation is read inside ``password_lock`` (which a web password change holds from
        its write to its own audit), so a web change is never taken for a CLI one.
        """
        try:
            db = await self._blocking(self._open_db)
        except (auth.HubDbError, OSError) as exc:
            _log(f"hub.sqlite3 unavailable: {exc}")
            return
        services = self.services
        try:
            if services is not None:
                async with services.password_lock:
                    await self._blocking(self._note, db)
            else:
                await self._blocking(self._note, db)
        except (auth.HubDbError, auth.AuditRejected, OSError, sqlite3.Error) as exc:  # never blocks the listener
            _log(f"password generation audit failed: {exc!r}")

    def _note(self, db: auth.HubDb) -> None:
        try:
            generation = self.passwords.generation()
        except auth.AuthInvalid:
            return
        if generation is not None:
            auth.AuditLog(db).note_password_generation(generation, "cli", now=float(self.clock()))

    async def _open(self) -> None:
        web_config = self.config.web
        try:
            db = await self._blocking(self._open_db)
        except (auth.HubDbError, OSError) as exc:
            _log(f"hub.sqlite3 unavailable: {exc}; retrying in {self.bind_retry:g} s")
            self._retry_at = self.monotonic() + self.bind_retry
            self._state = {"state": "hub_db_invalid", "retry_in": self.bind_retry}
            return
        tls = self.tls.state == "tls"
        warn = insecure_warning(web_config.bind, tls=tls)
        if self.services is None:
            self.services = WebServices(self.passwords, db, OriginPolicy(web_config.bind, tls, web_config.server_names),
                                        insecure_warning=warn, clock=self.clock,
                                        projects=lambda: self.projects())
        runner = web.AppRunner(create_app(self.services, routes=self.routes), access_log=None,
                               handle_signals=False, shutdown_timeout=SHUTDOWN_TIMEOUT,
                               keepalive_timeout=self.keepalive_timeout)
        await runner.setup()
        site = GuardedSite(runner, web_config.bind, web_config.port, ssl_context=self.tls.context,
                           max_connections=self.max_connections)
        try:
            await site.start()
        except BaseException as exc:
            await runner.cleanup()
            if not isinstance(exc, OSError):
                raise
            name = errno.errorcode.get(exc.errno or 0, "OSError")
            self._retry_at = self.monotonic() + self.bind_retry
            self._state = {"state": "bind_failed", "detail": name, "retry_in": self.bind_retry}
            _log(f"bind {web_config.bind}:{web_config.port} failed: {name}; retrying in {self.bind_retry:g} s")
            return
        self.runner, self._retry_at = runner, None
        port = runner.addresses[0][1]
        url = listen_url("https" if tls else "http", web_config.bind, port)
        self._state = {"state": "listening", "url": url, "tls": tls, "insecure_warning": warn,
                       "bind": web_config.bind, "port": port, "warnings": list(self.tls.warnings)}
        _log(f"listening {url}")

    async def stop(self) -> None:
        """Stop order (SPEC): refuse new connections (site stop) -> close WebSockets (1001) ->
        extension hooks (Board agent set) -> flush API key last use -> end open connections ->
        close ``hub.sqlite3``. Each step runs even when an earlier one failed."""
        self.stopping = True
        runner, self.runner = self.runner, None
        self._state = {"state": "stopping"}
        if runner is not None:
            for site in list(runner.sites):
                await self._step("site stop", site.stop())
            sockets = list(runner.app[WEBSOCKETS])
            for socket_ in sockets:
                await self._step("websocket close", socket_.close(code=WSCloseCode.GOING_AWAY,
                                                                  message=b"hub stopping"))
        for hook in self.stop_hooks:
            await self._step("stop hook", hook())
        if self.services is not None:
            await self._step("api key flush", self._blocking(self.services.api_keys.flush))
        if runner is not None:
            await self._step("runner cleanup", runner.cleanup())
        if self.db is not None:
            await self._step("hub.sqlite3 close", self._blocking(self.db.close))
        self._state = {"state": "stopped"}

    async def _step(self, what: str, awaitable) -> None:
        try:
            await awaitable
        except Exception as exc:
            _log(f"{what} failed during stop: {exc!r}")
