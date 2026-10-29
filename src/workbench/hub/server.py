"""``wb _hub``: the per-user Hub process and its asyncio control server."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import struct
import sys
import time

from workbench.backend.boot import read_boot_id
from workbench.backend.paths import (BackendLocked, DataDirError, InstanceLock, read_private_json,
                                     unlink_stale_socket, write_private_json)
from workbench.contracts import hub_v1

from . import config as hub_config, registry, rest
from .inbox import backend_port
from .inbox.dispatch import Dispatcher
from .inbox.limits import InboxError
from .inbox.service import OPEN_STATES, InboxService, Principal, ProjectRef
from .inbox_api import InboxApi
from .status import Lister, check_entry
from .paths import HubLayout, hub_layout, prepare_socket_path, prepare_state_dir
from .web.app import board_routes
from .web.listener import WebListener
from .web.pages import projects as project_pages
from .web.pages import requests as request_pages
from .process import (READY_LOST, READY_MANAGED, READY_STARTED, RECORD_SCHEMA_VERSION, UNVERIFIABLE,
                      installed_version,
                      lock_holder, managed_ancestor, send_ready, start_ticks, wait_reparented)


LOCAL_PRINCIPAL = Principal.local()
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
WATCHDOG_INTERVAL = 2.0
WEB_POLL_TIMEOUT = 20.0  # one listener poll (auth.json read, audit note, open) never stalls the watchdog
RELOAD_TIMEOUT = 20.0  # one ``wb hub reload`` (file read, TLS decision, listener close and open)
BOARD_RUNNING_NOTE = ("웹을 꺼도 실행 중인 Board agent 세트는 멈추지 않습니다. 'wb hub stop'까지 계속 실행되고, "
                      "웹을 다시 켜면 같은 세트를 그대로 씁니다.")
BOARD_CONFIG_NOTE = "[board_agent] 변경은 다음 Board agent 시작(재시작) 때 적용됩니다."


def registry_summary(layout: HubLayout) -> dict:
    """Registry state for ``status``; reading and validation belong to ``hub.registry``."""
    summary = {"state": "ok", "count": 0, "path": str(layout.registry)}
    try:
        return {**summary, "count": len(registry.read(layout))}
    except registry.RegistryError:
        return {**summary, "state": "invalid"}


def _peer_credentials(writer: asyncio.StreamWriter) -> tuple[int, int] | None:
    """``SO_PEERCRED`` ``(pid, uid)`` of the connecting process, None when unreadable."""
    sock = writer.get_extra_info("socket")
    try:
        pid, uid, _gid = struct.unpack("iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    except (OSError, AttributeError, struct.error):
        return None
    return pid, uid


def backend_mismatch(entries: list[registry.Entry], project_id: str, peer: tuple[int, int | None] | None) -> str | None:
    """Why ``peer`` (pid, start ticks read at accept) is not the running backend of registered ``project_id``,
    or None when it is (SPEC "Hub ↔ backend 요청 통로"). Blocking file and ``/proc`` reads: executor only."""
    from workbench.backend.paths import DataDirError, DataLayout
    from workbench.backend.process_identity import BackendIdentityError, inspect_backend
    if peer is None or peer[1] is None:
        return "peer_unreadable"
    pid, ticks = peer
    entry = next((item for item in entries if item.project_id == project_id), None)
    if entry is None:
        return "not_registered"
    mismatch = check_entry(entry)
    if mismatch is not None:
        return mismatch
    try:
        identity = inspect_backend(DataLayout(Path(entry.root) / ".wb"))
    except BackendIdentityError as exc:
        return f"identity:{exc.reason}"
    except (DataDirError, OSError, ValueError):
        return "identity:unavailable"
    if identity.state != "running" or identity.record is None:
        return f"backend_{identity.state}"
    if identity.project_id != project_id:
        return "project_id_mismatch"
    reference = identity.record["processes"]["backend"]
    if reference.get("pid") != pid or reference.get("start_ticks") != ticks:
        return "peer_not_backend"
    if start_ticks(pid) != ticks:  # the peer process itself is gone (its pid may be reused)
        return "peer_gone"
    return None


def tls_signature(tls: hub_config.TlsDecision, cert: Path, key: Path) -> tuple:
    """What a reload compares to see a changed TLS decision or replaced cert/key files ('wb hub tls')."""
    def stat_of(path: Path) -> tuple | None:
        try:
            info = os.stat(path)
        except OSError:
            return None
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, info.st_mode)
    return (tls.state, tls.detail, str(cert), stat_of(cert), str(key), stat_of(key))


def config_changes(old: hub_config.HubConfig | None, new: hub_config.HubConfig, old_tls: tuple | None,
                   new_tls: tuple) -> list[str]:
    """The ``hub.toml`` keys whose value differs (``web.bind`` covers the list form; ``web.tls_files`` a changed TLS
    decision or cert/key file); ``web`` and ``board_agent`` when the running config was ``config_invalid``."""
    from dataclasses import fields
    if old is None:
        return ["web", "board_agent"]
    changed: list[str] = []
    for table, before, after in (("web", old.web, new.web), ("board_agent", old.board_agent, new.board_agent)):
        for item in fields(before):
            if getattr(before, item.name) != getattr(after, item.name):
                key = f"{table}.{'bind' if item.name == 'binds' else item.name}"
                if key not in changed:
                    changed.append(key)
    if old_tls != new_tls:
        changed.append("web.tls_files")
    return changed


class Hub:
    def __init__(self, layout: HubLayout, mode: str, *, stop_token_ttl: int = hub_v1.STOP_TOKEN_TTL,
                 idle_timeout: float = hub_v1.IDLE_TIMEOUT, max_connections: int = hub_v1.MAX_CONNECTIONS):
        self.layout = layout
        self.mode = mode
        self.stop_token_ttl = stop_token_ttl
        self.idle_timeout = idle_timeout
        self.max_connections = max_connections
        self.lock = InstanceLock(layout.lock)
        self.socket_path: Path | None = None
        self.socket_identity: tuple[int, int] | None = None
        self.listener: socket.socket | None = None
        self.record: dict | None = None
        self.stop_offer: tuple[str, float, list] | None = None  # token, monotonic deadline, active
        self.stop_requested = False
        self.stop_event: asyncio.Event | None = None
        self.tasks: set[asyncio.Task] = set()
        self.early_stop = False  # a stop signal that arrived before the event loop ran
        self.state_identity: tuple[int, int] | None = None
        self.watchdog_reason: str | None = None
        self.lister = Lister(layout, requests=self._row_requests)
        # TI-10: every inbox commit wakes the dispatcher (it only marks work; it never calls back in)
        # TI-13: a READY/RUNNING cancel asks the backend's cancel_task right after its commit (backend_port)
        self.inbox = InboxService(on_committed=lambda event: self.dispatcher.committed(event),
                                  backend_running=self._backend_running, cancel_port=backend_port.cancel_port)
        self.dispatcher = Dispatcher(self.inbox, self._dispatch_targets)
        # The external request API's processing layer: ``/api/v1/`` on the web listener and on the Board agent's
        # internal listener (WBX-B1/B5)
        self.api = InboxApi(self.inbox, projects=self._api_projects)
        # Verified backend connections by project id (one each): outside ``tasks`` and its limit (hub_v1).
        self.backend_tasks: dict[str, asyncio.Task] = {}
        self.web: WebListener | None = None
        # TI-16: the Board agent set (None while ``hub.toml`` is invalid or web is disabled) and its internal
        # REST listener (WBX-B5; started lazily by the set, so it never starts without one).
        self.board = None
        self.internal_rest: rest.InternalListener | None = None
        # web-board-ux s5 트랙 E: the project pane relays (one shared attach per project, opened from the web)
        from .board.panes import PaneHub
        self.panes = PaneHub(lambda: registry.read(self.layout), power=self.lister.list,
                             audit=lambda event, address, detail: self._board_audit(event, "owner", address, detail))
        # 2026-10-27 ``wb hub reload``: what the running config came from, and the last refused reload.
        self.config_environment = None
        self.config_info: dict = {}
        self.tls_signature: tuple | None = None
        self.reload_error: dict | None = None
        self.reload_lock = asyncio.Lock()

    def project_pairs(self) -> list[tuple[str, str]]:
        """``(project_id, name)`` of the registered projects for the web settings pages; executor only."""
        try:
            return [(entry.project_id, entry.name) for entry in registry.read(self.layout)]
        except registry.RegistryError:
            return []

    def board_state(self) -> str:
        """The nav's Board dot (web-board-ux SPEC s4): ``error`` when an agent is ``failed``/``isolation_failed``, the
        registry is invalid or ``hub.toml`` is; ``on`` while an agent is starting/running; else ``off``. Non-blocking."""
        if self.board is None:
            return "error" if self.config_info.get("state") == "config_invalid" else "off"
        return self.board.dot()

    def board_counts(self) -> dict | None:
        """The dashboard's Board card numbers (running agents, shells), None without a Board."""
        return self.board.counts() if self.board is not None else None

    def web_routes(self) -> list:
        """Route registrations the web app adds to its defaults: the status board and project pages over this
        Hub's list, the request pages over its inbox, the ``/api/v1/`` REST (WBX-B1) and the Board restart endpoint."""
        return [project_pages.routes(self.lister.list, self.inbox, self.lister.list_one),
                request_pages.routes(self.inbox, self._dispatch_targets), rest.routes(self.api),
                board_routes(lambda: self.board, lambda: self.panes)]

    # --- Board agent set (TI-16) -----------------------------------------------------------------

    def _web_services(self):
        services = self.web.services if self.web is not None else None
        if services is None:  # the set starts from a logged-in web request, so this only races a stop
            raise RuntimeError("web services are not open")
        return services

    def _internal_listener(self) -> rest.InternalListener:
        if self.internal_rest is None:
            self.internal_rest = rest.InternalListener(self._web_services().api_keys, self.api)
        return self.internal_rest

    def _board_audit(self, event: str, actor: str, address: str | None, detail: dict) -> None:
        """The Board's audit rows (``board_start``/``stop``/``restart``, ``board_terminal_open``/``close``,
        ``board_rename``, ``board_options``); executor only (SQLite)."""
        services = self.web.services if self.web is not None else None
        if services is None:
            return
        services.audit.record(event, actor, address, detail, now=float(services.clock()))

    def make_board(self, config: hub_config.BoardAgentConfig, environment=None) -> None:
        from .board.launch import BoardLauncher
        from .board.set import BoardSet
        launcher = BoardLauncher(self.layout.state_dir, config, api_keys=lambda: self._web_services().api_keys,
                                 listener=self._internal_listener, environment=environment)
        self.board = BoardSet(launcher, audit=self._board_audit)
        # WBX-D58: a system key passes (on both listeners) only as the current start's key of a running agent.
        self.api.board_agents = self._board_agent_for_key

    def _board_agent_for_key(self, key_id: str, name: str):
        return self.board.agent_for_key(key_id, name) if self.board is not None else None

    async def stop_board(self) -> None:
        """Stop order (A): after the WebSockets close, before the API key flush; idempotent. The project pane relays
        (s5 트랙 E) end here too, each with a best-effort ``share_detach``."""
        try:
            await self.panes.stop()
        finally:
            if self.board is not None:
                await self.board.stop()

    async def _api_projects(self) -> list[dict]:
        """``inbox_projects`` rows: the Hub list (cached observation, request counts and review mode)."""
        return (await self.lister.list())["rows"]

    def _backend_running(self, project_id: str) -> bool:
        """READY/RUNNING cancel can reach the project's backend now (verified ``backend.json`` identity)."""
        try:
            entries = registry.read(self.layout)
        except (registry.RegistryError, OSError):
            return False
        entry = next((item for item in entries if item.project_id == project_id), None)
        return entry is not None and self.dispatcher.port.identity(Path(entry.root)) is not None

    def read_config(self):
        """``(config, tls decision, path, tls signature)`` from ``hub.toml``; :class:`ConfigInvalid` when unusable.
        Blocking (file and TLS reads): at start or in an executor."""
        path = hub_config.config_path(self.config_environment)
        loaded = hub_config.load_config(path)
        cert, key = hub_config.tls_paths(loaded.web, self.config_environment)
        tls = hub_config.decide_tls(loaded.web, cert, key)
        return loaded, tls, path, tls_signature(tls, cert, key)

    def load_web(self, environment=None) -> None:
        """Read ``hub.toml`` and decide TLS at start; ``wb hub reload`` (:meth:`reload`) applies later changes."""
        self.config_environment = environment
        path = hub_config.config_path(environment)
        try:
            loaded, tls, path, self.tls_signature = self.read_config()
        except hub_config.ConfigInvalid as exc:
            print(f"[workbench] hub.toml invalid: {exc.reason}; web listener stays closed", file=sys.stderr, flush=True)
            self.config_info = {"path": str(path), "loaded_at": time.time(), "state": "config_invalid",
                                "detail": exc.item}
            self.web = WebListener(self.layout.state_dir, config=None, config_error=exc, tls=None,
                                   routes=self.web_routes(), config_file=path)
            self.web.projects = self.project_pairs
            self.web.board_state = self.board_state
            self.web.board_counts = self.board_counts
            return
        self.config_info = {"path": str(path), "loaded_at": time.time(), "state": "ok"}
        self.web = WebListener(self.layout.state_dir, config=loaded, config_error=None, tls=tls,
                               routes=self.web_routes(), config_file=path)
        self.web.projects = self.project_pairs
        self.web.board_state = self.board_state
        self.web.board_counts = self.board_counts
        if not loaded.web.enabled:  # the Board agent set is used only from the web: none while web is off
            return
        self.ensure_board(loaded.board_agent)

    def ensure_board(self, config: hub_config.BoardAgentConfig) -> None:
        """The Board agent set for an enabled web: made once, then reused (a reload never makes a second one);
        ``config`` applies from its next start."""
        if self.board is None:
            self.make_board(config, self.config_environment)
        else:
            self.board.launcher.config = config
        if self.stop_board not in self.web.stop_hooks:
            self.web.stop_hooks.append(self.stop_board)

    def config_status(self) -> dict:
        """``status.config``: the file, when the running config was loaded, and the last refused reload."""
        return {**self.config_info, "reload_error": dict(self.reload_error) if self.reload_error else None}

    async def reload(self, request_id: str) -> dict:
        """hub_v1 ``reload`` (2026-10-27 사용자 결정). Only the web listener and the Board agent set's next-start
        config change; WB projects, the registry, the inbox and review dispatch are never touched."""
        async with self.reload_lock:
            loop = asyncio.get_running_loop()
            try:
                loaded, tls, path, signature = await loop.run_in_executor(None, self.read_config)
            except hub_config.ConfigInvalid as exc:
                self.reload_error = {"reason": exc.reason, "at": time.time()}
                print(f"[workbench] hub.toml reload refused: {exc.reason}; the running config stays",
                      file=sys.stderr, flush=True)
                return hub_v1.failure(request_id, "config_invalid", exc.reason, item=exc.item)
            changed = config_changes(self.web.config, loaded, self.tls_signature, signature)
            if any(key == "web" or key.startswith("web.") for key in changed):
                await self.web.reconfigure(loaded, tls, path)
            else:
                self.web.config, self.web.config_file = loaded, path  # only [board_agent] (or nothing) changed
            self.tls_signature = signature
            self.config_info = {"path": str(path), "loaded_at": time.time(), "state": "ok"}
            self.reload_error = None
            notes = []
            if loaded.web.enabled:
                self.ensure_board(loaded.board_agent)
            elif self.board is not None:
                self.board.launcher.config = loaded.board_agent
                if self.board.active():
                    notes.append(BOARD_RUNNING_NOTE)
            if self.board is not None and any(key == "board_agent" or key.startswith("board_agent.")
                                              for key in changed):
                notes.append(BOARD_CONFIG_NOTE)
            print(f"[workbench] hub.toml reloaded: changed {', '.join(changed) or 'nothing'}; "
                  f"web {self.web.status().get('state')}", file=sys.stderr, flush=True)
            return hub_v1.result(request_id, changed=changed, web=self.web.status(),
                                 board_agent=self.board.status() if self.board is not None else None,
                                 board_agent_note=" ".join(notes) or None, config=self.config_status())

    def bind(self) -> None:
        path = prepare_socket_path()
        unlink_stale_socket(path)  # only after the lock is held
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            listener.bind(str(path))
        except BaseException:
            listener.close()
            raise
        finally:
            os.umask(previous)
        info = path.lstat()
        listener.listen(32)
        listener.setblocking(False)
        self.socket_path, self.socket_identity, self.listener = path, (info.st_dev, info.st_ino), listener

    def publish_record(self) -> None:
        pid = os.getpid()
        self.record = {"schema_version": RECORD_SCHEMA_VERSION, "pid": pid, "start_ticks": start_ticks(pid),
                       "boot_id": read_boot_id(), "uid": os.geteuid(), "socket": str(self.socket_path),
                       "started_at": time.time(), "mode": self.mode, "log": str(self.layout.log),
                       "version": installed_version()}
        write_private_json(self.layout.record, self.record)

    def remember_state_dir(self) -> None:
        info = self.layout.state_dir.lstat()
        self.state_identity = (info.st_dev, info.st_ino)

    def guard_broken(self) -> str | None:
        """Why single instance can no longer be guaranteed, or None.

        A removed/replaced ``hub.lock`` lets a second Hub take a new lock; a removed
        or replaced state directory (e.g. a deleted temp HOME) orphans this Hub.
        """
        try:
            info = self.layout.state_dir.lstat()
        except FileNotFoundError:
            return "state_dir_removed"
        except OSError:
            return "state_dir_unreadable"
        if (info.st_dev, info.st_ino) != self.state_identity:
            return "state_dir_replaced"
        try:
            self.lock.verify_reservation(self.layout.lock)
        except FileNotFoundError:
            return "lock_removed"
        except (DataDirError, OSError):
            return "lock_replaced"
        return None

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            loop = asyncio.get_running_loop()
            reason = await loop.run_in_executor(None, self.guard_broken)
            try:
                await loop.run_in_executor(None, self.inbox.close_idle)
            except Exception as exc:  # an inbox problem never stops the Hub
                print(f"[workbench] inbox idle close failed: {exc!r}", file=sys.stderr, flush=True)
            if self.web is not None and reason is None:
                try:
                    await asyncio.wait_for(self.web.poll(), WEB_POLL_TIMEOUT)
                except Exception as exc:  # a web problem (or a stuck poll) never stops the Hub
                    print(f"[workbench] web listener poll failed: {exc!r}", file=sys.stderr, flush=True)
            if reason is not None:
                self.watchdog_reason = reason
                print(f"[workbench] Hub stopping: {reason}", file=sys.stderr, flush=True)
                self.request_stop()
                return

    def welcome_identity(self) -> dict:
        record = self.record or {}
        return {key: record.get(key) for key in ("pid", "start_ticks", "version", "started_at", "mode")}

    async def handle(self, principal: Principal, request: dict) -> dict:
        """One hub_v1 request from ``principal``; (A) adds web/API-key principals.

        A backend principal reaches :data:`hub_v1.BACKEND_OPS` only, any other connection everything else.
        File and ``/proc`` reads run in the default executor, never on the loop.
        """
        request_id = request["id"]
        op = request.get("type")
        if (op in hub_v1.BACKEND_OPS) != (principal.kind == "manager"):
            return hub_v1.failure(request_id, hub_v1.Reason.UNSUPPORTED_TYPE, f"unsupported request type: {op!r}")
        if principal.kind == "manager" or op == hub_v1.ClientType.REVIEW_MODE.value:
            return await self.inbox_op(principal, request)
        if op == hub_v1.ClientType.STATUS.value:
            return hub_v1.result(request_id, **await self.status())
        if op == hub_v1.ClientType.RELOAD.value:
            try:
                return await asyncio.wait_for(self.reload(request_id), RELOAD_TIMEOUT)
            except asyncio.TimeoutError:
                return hub_v1.failure(request_id, "reload_timeout", f"the reload did not finish in {RELOAD_TIMEOUT:g} s")
            except Exception as exc:  # a reload never stops the Hub
                print(f"[workbench] hub.toml reload failed: {exc!r}", file=sys.stderr, flush=True)
                return hub_v1.failure(request_id, "reload_failed", f"{type(exc).__name__}: {exc}")
        if op == hub_v1.ClientType.LIST.value:
            fresh = request.get("fresh", False)
            if not isinstance(fresh, bool):
                return hub_v1.failure(request_id, hub_v1.Reason.BAD_REQUEST, "fresh must be a boolean")
            return hub_v1.result(request_id, **await self.lister.list(fresh))
        if op == hub_v1.ClientType.STOP_REQUEST.value:
            return hub_v1.result(request_id, **self.offer_stop())
        if op == hub_v1.ClientType.STOP_CONFIRM.value:
            if not self.confirm_stop(request.get("token")):
                return hub_v1.failure(request_id, hub_v1.Reason.STOP_TOKEN_MISMATCH,
                                      "The stop token is unknown, expired or the active set changed.")
            return hub_v1.result(request_id, stopping=True)
        return hub_v1.failure(request_id, hub_v1.Reason.UNSUPPORTED_TYPE, f"unsupported request type: {op!r}")

    async def inbox_op(self, principal: Principal, request: dict) -> dict:
        """A backend principal's inbox op, run on an executor thread (the inbox blocks on its project thread)."""
        request_id = request["id"]
        try:
            value = await asyncio.get_running_loop().run_in_executor(None, self._inbox_op, principal, request)
        except InboxError as exc:
            extra = {key: value for key, value in (("state", exc.state), ("version", exc.version))
                     if value is not None}
            return hub_v1.failure(request_id, exc.code, exc.detail or exc.code, **extra)
        return hub_v1.result(request_id, **value)

    def _inbox_op(self, principal: Principal, request: dict) -> dict:
        op, inbox = request.get("type"), self.inbox
        if op == hub_v1.ClientType.REVIEW_MODE.value:  # local (``wb hub review-mode``)
            return inbox.review_mode(principal, self._registered_project(request.get("root")), request.get("set"))
        (project_id,) = principal.scope
        if op == hub_v1.ClientType.INBOX_LIST.value:
            states = request.get("states", None)
            if states is None:
                states = OPEN_STATES
            elif not isinstance(states, list) or not all(isinstance(state, str) for state in states):
                raise InboxError("invalid_input", "states must be a list of state names")
            return inbox.list_requests(principal, project_id=project_id, states=states, limit=request.get("limit"))
        if op == hub_v1.ClientType.INBOX_SHOW.value:
            return {"request": inbox.get(principal, request.get("request_id"))}
        if op == hub_v1.ClientType.INBOX_REVIEW.value:
            return inbox.review(principal, request.get("request_id"), request.get("action"), body=request.get("body"),
                                outcome=request.get("outcome"), summary=request.get("summary"))
        if op == hub_v1.ClientType.INBOX_LINK_CHECK.value:
            return inbox.link_check(principal, request.get("request_id"))
        if op == hub_v1.ClientType.INBOX_LINK_COMMIT.value:
            return inbox.link_commit(principal, request.get("link_token"), request.get("task_id"),
                                     request.get("spec_revision"), request.get("task_status"))
        # inbox_delivery: the request must be this project's (visible to the principal) before the Hub records it
        request_id = request.get("request_id")
        inbox.get(principal, request_id)
        return inbox.record_delivery(request_id, request.get("delivery_id"), request.get("outcome"))

    def sync_projects(self) -> list[registry.Entry]:
        """Give the inbox the registry's projects (a mismatched entry is ``project_unavailable``); executor only."""
        entries = registry.read(self.layout)
        self.inbox.set_projects(ProjectRef(entry.project_id, Path(entry.root), entry.name, check_entry(entry))
                                for entry in entries)
        return entries

    def _row_requests(self, entries: list[registry.Entry]) -> dict[str, dict]:
        """``list`` rows (TI-10): each readable project's open request counts and review mode; an inbox that cannot
        be read (a mismatched entry, a busy or broken file) is ``null`` for both. Executor only."""
        try:
            self.sync_projects()
        except (registry.RegistryError, OSError):
            return {}
        rows = {}
        for entry in entries:
            # from memory (InboxService.list_summary): polling never opens an inbox or keeps it open
            summary = self.inbox.list_summary(entry.project_id) if check_entry(entry) is None else None
            rows[entry.root] = summary or {"pending_requests": None, "review_mode": None}
        return rows

    def _dispatch_targets(self) -> list[ProjectRef]:
        """The registered projects for the dispatcher (its thread); an unreadable registry gives none."""
        try:
            entries = self.sync_projects()
        except (registry.RegistryError, OSError):
            return []
        return [ProjectRef(entry.project_id, Path(entry.root), entry.name, check_entry(entry)) for entry in entries]

    def _registered_project(self, root: object) -> str:
        """The project id of the registry entry for ``root`` (an absolute path; resolved like the registry)."""
        if not isinstance(root, str) or not os.path.isabs(root):
            raise InboxError("invalid_input", "root must be an absolute path")
        try:
            entries = self.sync_projects()
        except registry.RegistryError as exc:
            raise InboxError("registry_invalid", str(exc)) from None
        wanted = {root, os.path.realpath(root)}
        entry = next((item for item in entries if item.root in wanted), None)
        if entry is None:
            raise InboxError("not_found", "project not registered")
        return entry.project_id

    def verify_backend(self, project_id: str, peer: tuple[int, int | None] | None, *, sync: bool) -> str | None:
        """:func:`backend_mismatch` against the current registry (``sync`` also refreshes the inbox projects)."""
        try:
            entries = self.sync_projects() if sync else registry.read(self.layout)
        except registry.RegistryError:
            return "registry_invalid"
        return backend_mismatch(entries, project_id, peer)

    def adopt_backend(self, project_id: str, task: asyncio.Task | None) -> None:
        """Move a verified backend connection out of the general pool; a newer one replaces an older one."""
        if task is None:
            return
        self.tasks.discard(task)
        previous = self.backend_tasks.get(project_id)
        self.backend_tasks[project_id] = task
        if previous is not None and previous is not task:
            previous.cancel()

    def drop_backend(self, task: asyncio.Task | None) -> None:
        for project_id, current in list(self.backend_tasks.items()):
            if current is task:
                del self.backend_tasks[project_id]

    async def status(self) -> dict:
        registry = await asyncio.get_running_loop().run_in_executor(None, registry_summary, self.layout)
        web = self.web.status() if self.web is not None else {"state": "not_available"}
        board = self.board.status() if self.board is not None else None
        return {"hub": dict(self.record or {}), "registry": registry, "web": web, "board_agent": board,
                "config": self.config_status()}

    def active(self) -> list:
        """Live things this Hub owns that a stop would end: the Board terminals while one lives."""
        return self.board.active() if self.board is not None else []

    def offer_stop(self) -> dict:
        token = secrets.token_hex(16)
        active = self.active()
        self.stop_offer = (token, time.monotonic() + self.stop_token_ttl, active)
        return {"token": token, "active": active, "expires_in": self.stop_token_ttl}

    def confirm_stop(self, token: object) -> bool:
        """Accept only the latest offer's token, unexpired, with an unchanged active set.

        There is a single offer and any confirm consumes it, right or wrong: a wrong
        guess cannot be retried against the same token (fail-safe); a caller that
        lost the race simply asks for a new ``stop_request``.
        """
        offer, self.stop_offer = self.stop_offer, None
        if (offer is None or not isinstance(token, str) or not secrets.compare_digest(token, offer[0])
                or time.monotonic() > offer[1] or self.active() != offer[2]):
            return False
        self.stop_requested = True
        return True

    def request_stop(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()

    async def connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        try:
            credentials = _peer_credentials(writer)
            if credentials is None or credentials[1] != os.geteuid():
                return
            if len(self.tasks) >= self.max_connections:
                writer.write(hub_v1.encode_line(hub_v1.reject(hub_v1.Reason.CONNECTION_LIMIT)))
                await writer.drain()
                return
            self.tasks.add(task)
            # The peer's start ticks are read now, at accept, so a later pid reuse cannot pass as this peer.
            ticks = await asyncio.get_running_loop().run_in_executor(None, start_ticks, credentials[0])
            await Connection(self, reader, writer, (credentials[0], ticks)).run()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.tasks.discard(task)
            self.drop_backend(task)
            writer.close()

    async def serve(self) -> None:
        """Serve until a stop; then refuse new connections and end the open ones."""
        self.stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in STOP_SIGNALS:
            loop.add_signal_handler(signum, self.request_stop)
        if self.early_stop:
            self.request_stop()
        server = await asyncio.start_unix_server(self.connection, sock=self.listener)
        if self.web is not None:
            try:
                await self.web.start()
            except Exception as exc:  # a web problem never stops the Hub's other functions
                print(f"[workbench] web listener start failed: {exc!r}", file=sys.stderr, flush=True)
            if self.board is not None:  # WBX-D58: system key rows of an earlier run go (again before a first start)
                try:
                    await self.board.purge_system_keys()
                except Exception as exc:
                    print(f"[workbench] Board system key purge failed: {exc!r}", file=sys.stderr, flush=True)
        watchdog = asyncio.create_task(self.watchdog())
        self.dispatcher.start()  # TI-10: request notices to each running project's manager
        await self.stop_event.wait()
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)  # no web open can be half done below
        if self.web is not None:  # (A) stop order: the web listener goes first
            try:
                await self.web.stop()
            except Exception as exc:
                print(f"[workbench] web listener stop failed: {exc!r}", file=sys.stderr, flush=True)
        try:
            await self.stop_board()  # already done by the web stop hook; a failed web stop must not skip it
        except Exception as exc:
            print(f"[workbench] Board agent set stop failed: {exc!r}", file=sys.stderr, flush=True)
        server.close()
        tasks = [*self.tasks, *self.backend_tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.lister.close()
        self.api.close()  # the REST pool: queued calls cancelled, no wait (security review fix 2)
        await loop.run_in_executor(None, self.dispatcher.close)  # before the inbox closes under it
        await loop.run_in_executor(None, self.inbox.close)  # releases every inbox.lock

    def new_owner_possible(self) -> bool:
        """After the guard broke, does another process hold a lock at ``hub.lock``?

        Such a process may be a new Hub that already owns the record/socket paths.
        Unreadable lock state counts as possible (leave the files).
        """
        try:
            holder = lock_holder(self.layout.lock)
        except (OSError, ValueError, IndexError):
            return True
        return holder not in (None, os.getpid())

    def cleanup(self, *, remove_files: bool = True) -> None:
        """Remove only our own socket (dev/ino) and record (pid/start_ticks), then
        release the lock; ``remove_files=False`` leaves both paths untouched."""
        if not remove_files:
            self.lock.release()
            return
        if self.socket_path is not None and self.socket_identity is not None:
            try:
                info = self.socket_path.lstat()
                if (info.st_dev, info.st_ino) == self.socket_identity:
                    self.socket_path.unlink()
            except FileNotFoundError:
                pass
        try:
            current = read_private_json(self.layout.record)
        except OSError:
            current = None
        if (current is not None and self.record is not None and current.get("pid") == self.record["pid"]
                and current.get("start_ticks") == self.record["start_ticks"]):
            try:
                self.layout.record.unlink()
            except FileNotFoundError:
                pass
        self.lock.release()


class Connection:
    """One hub_v1 peer: hello first, then line requests until EOF or protocol error."""

    def __init__(self, hub: Hub, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 peer: tuple[int, int | None] | None = None):
        self.hub, self.reader, self.writer = hub, reader, writer
        self.peer = peer  # (SO_PEERCRED pid, its /proc start ticks at accept)
        self.decoder = hub_v1.LineDecoder()
        self.greeted = False
        self.principal = LOCAL_PRINCIPAL

    async def send(self, value: dict) -> None:
        self.writer.write(hub_v1.encode_line(value))
        await self.writer.drain()

    async def verify(self, project_id: str, *, sync: bool) -> str | None:
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: self.hub.verify_backend(project_id, self.peer, sync=sync))

    async def run(self) -> None:
        """Serve lines until EOF, a protocol error, a stop or ``idle_timeout`` without a request (a backend
        principal's connection has no idle timeout: the backend keeps it open)."""
        deadline = time.monotonic() + self.hub.idle_timeout
        while True:
            remaining = None if self.principal.kind == "manager" else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return
            try:
                data = await asyncio.wait_for(self.reader.read(65536), remaining)
            except asyncio.TimeoutError:
                return
            if not data:
                return
            try:
                frames = self.decoder.feed(data)
            except hub_v1.ProtocolError:
                await self.send(hub_v1.reject(hub_v1.Reason.PROTOCOL_ERROR))
                return
            for frame in frames:
                if not await self.handle(frame):
                    return
            if frames:  # a partial line is not a request
                deadline = time.monotonic() + self.hub.idle_timeout

    async def handle(self, frame: dict) -> bool:
        """Answer one line; False closes the connection."""
        if not self.greeted:
            if frame.get("type") != hub_v1.ClientType.HELLO.value:
                await self.send(hub_v1.reject(hub_v1.Reason.PROTOCOL_ERROR))
                return False
            if frame.get("v") not in hub_v1.SUPPORTED_VERSIONS:
                await self.send(hub_v1.reject(hub_v1.Reason.VERSION_MISMATCH))
                return False
            if not hub_v1.valid_client_name(frame.get("client")) or (
                    frame.get("project_id") is not None and not hub_v1.valid_project_id(frame["project_id"])):
                await self.send(hub_v1.reject(hub_v1.Reason.BAD_REQUEST))
                return False
            project_id = frame.get("project_id")
            if frame["client"] == hub_v1.BACKEND_CLIENT or project_id is not None:
                # Stricter of the SPEC readings: the name alone grants nothing, and a project id without the
                # backend name (or the name without an id) is refused rather than served as ``local``.
                why = ("hello_incomplete" if frame["client"] != hub_v1.BACKEND_CLIENT or project_id is None
                       else await self.verify(project_id, sync=True))
                if why is not None:
                    print(f"[workbench] backend principal refused: project {project_id} peer {self.peer}: {why}",
                          file=sys.stderr, flush=True)
                    await self.send(hub_v1.reject(hub_v1.Reason.PRINCIPAL_MISMATCH))
                    return False
                self.principal = Principal.manager(project_id)
                self.hub.adopt_backend(project_id, asyncio.current_task())
                print(f"[workbench] backend principal: project {project_id} pid {self.peer[0]} "
                      f"start_ticks {self.peer[1]}", file=sys.stderr, flush=True)
            self.greeted = True
            await self.send(hub_v1.welcome(self.hub.welcome_identity()))
            return True
        request_id = frame.get("id")
        if frame.get("v") != hub_v1.VERSION or not hub_v1.valid_request_id(request_id):
            await self.send(hub_v1.reject(hub_v1.Reason.PROTOCOL_ERROR))
            return False
        if self.principal.kind == "manager":
            (project_id,) = self.principal.scope
            why = await self.verify(project_id, sync=False)
            if why is not None:  # per connection: a backend this connection no longer is gets nothing
                print(f"[workbench] backend principal withdrawn: project {project_id} peer {self.peer}: {why}",
                      file=sys.stderr, flush=True)
                await self.send(hub_v1.failure(request_id, hub_v1.Reason.PRINCIPAL_MISMATCH,
                                               "This connection is no longer the project's running backend."))
                return False
        await self.send(await self.hub.handle(self.principal, frame))
        if self.hub.stop_requested:
            self.hub.request_stop()
            return False
        return True


LOG_ROTATE_BYTES = 1024 * 1024


def rotate_log(layout: HubLayout) -> None:
    """Once at start, under the lock: a ``hub.log`` over 1 MiB becomes ``hub.log.1``.

    stdout/stderr that point at the old file (spawned mode) move to a fresh
    ``hub.log``; a foreground Hub's terminal is left alone.
    """
    try:
        info = os.stat(layout.log, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_size <= LOG_ROTATE_BYTES:
        return
    redirected = []
    for fd in (1, 2):
        try:
            opened = os.fstat(fd)
        except OSError:
            continue
        if (opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino):
            redirected.append(fd)
    rotated = layout.state_dir / (layout.log.name + ".1")
    staged = layout.state_dir / (layout.log.name + ".new")
    try:
        staged.unlink()  # left by an interrupted rotation; only a lock holder writes here
    except FileNotFoundError:
        pass
    # The fresh log exists before anything moves, so a failure leaves fds 1/2 on hub.log.
    fresh = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600)
    try:
        old = os.open(layout.log, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            pinned = os.fstat(old)
            if (pinned.st_dev, pinned.st_ino) != (info.st_dev, info.st_ino):
                return
            os.fchmod(old, 0o600)  # the rotated generation stays private
        finally:
            os.close(old)
        sys.stdout.flush()
        sys.stderr.flush()
        os.replace(layout.log, rotated)
        try:
            os.replace(staged, layout.log)
        except OSError:
            os.replace(rotated, layout.log)  # roll back: hub.log is again the file fds 1/2 use
            raise
        for fd in redirected:
            os.dup2(fresh, fd)
    finally:
        os.close(fresh)
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def main_hub(args: argparse.Namespace) -> int:
    mode = args.mode or ("foreground" if args.foreground else "spawned")
    layout = hub_layout()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    hub = Hub(layout, mode, stop_token_ttl=args.stop_token_ttl, idle_timeout=args.idle_timeout)

    def early(_signum, _frame):
        hub.early_stop = True

    for signum in STOP_SIGNALS:
        signal.signal(signum, early)
    ready = getattr(args, "ready_fd", None)  # spawned: report R/L/M; any other exit is EOF
    if ready is not None:
        os.set_inheritable(ready, False)
    wait_reparented(getattr(args, "spawner_pid", None))
    ancestor = managed_ancestor(os.getpid())  # self-check before the lock (HB-03)
    if ancestor is not None:
        send_ready(ready, READY_MANAGED)
        print("[workbench] Hub refused: " + ("process information (/proc) is unreadable (proc_unreadable)"
                                             if ancestor is UNVERIFIABLE else
                                             f"inside a WB-managed process tree (ancestor {ancestor})"),
              file=sys.stderr)
        return 3
    try:
        prepare_state_dir(layout)
        hub.remember_state_dir()
        hub.lock.acquire()
    except BackendLocked:
        send_ready(ready, READY_LOST)
        print("[workbench] another Hub already holds the lock", file=sys.stderr)
        return 0
    except (DataDirError, OSError) as exc:
        print(f"[workbench] Hub start failed: {exc}", file=sys.stderr)
        return 1
    try:
        rotate_log(layout)
    except OSError as exc:  # a log problem never blocks the Hub
        print(f"[workbench] hub.log rotation skipped: {exc}", file=sys.stderr)
    # Every start path (autostart, ``wb hub start``, systemd, foreground) runs here, under the lock and
    # before ``hub.json`` is published: the template exists by the time anyone sees the Hub ready.
    template = hub_config.write_template()
    if template.startswith("skipped:"):
        print(f"[workbench] hub.toml template not created ({template.partition(':')[2]}): "
              f"{hub_config.config_path()}; the Hub uses the defaults", file=sys.stderr, flush=True)
    try:
        hub.bind()
        hub.publish_record()
        hub.load_web()
    except (DataDirError, OSError) as exc:
        hub.cleanup()
        print(f"[workbench] Hub start failed: {exc}", file=sys.stderr)
        return 1
    send_ready(ready, READY_STARTED)
    try:
        asyncio.run(hub.serve())
    finally:
        hub.cleanup(remove_files=hub.watchdog_reason is None or not hub.new_owner_possible())
    return 1 if hub.watchdog_reason else 0
