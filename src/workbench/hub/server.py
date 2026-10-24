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

from .paths import HubLayout, hub_layout, prepare_socket_path, prepare_state_dir
from .process import (READY_LOST, READY_MANAGED, READY_STARTED, RECORD_SCHEMA_VERSION, installed_version,
                      lock_holder, managed_ancestor, send_ready, start_ticks, wait_reparented)


LOCAL_PRINCIPAL = "local"
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
WATCHDOG_INTERVAL = 2.0


def registry_summary(layout: HubLayout) -> dict:
    """Registry state for ``status``; the registry itself belongs to ``hub.registry``."""
    path = layout.registry
    summary = {"state": "ok", "count": 0, "path": str(path)}
    if not os.path.lexists(path):
        return summary
    try:
        value = read_private_json(path)
    except OSError:
        value = None
    projects = value.get("projects") if value is not None and value.get("schema_version") == 1 else None
    if not isinstance(projects, list):
        return {**summary, "state": "invalid"}
    return {**summary, "count": len(projects)}


def _peer_is_owner(writer: asyncio.StreamWriter) -> bool:
    """``SO_PEERCRED`` uid of the connecting process equals this Hub's euid."""
    sock = writer.get_extra_info("socket")
    try:
        _pid, uid, _gid = struct.unpack("iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    except (OSError, AttributeError, struct.error):
        return False
    return uid == os.geteuid()


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
            reason = await asyncio.get_running_loop().run_in_executor(None, self.guard_broken)
            if reason is not None:
                self.watchdog_reason = reason
                print(f"[workbench] Hub stopping: {reason}", file=sys.stderr, flush=True)
                self.request_stop()
                return

    def welcome_identity(self) -> dict:
        record = self.record or {}
        return {key: record.get(key) for key in ("pid", "start_ticks", "version", "started_at", "mode")}

    async def handle(self, principal: str, request: dict) -> dict:
        """One hub_v1 request from ``principal``; (A) adds web/API-key principals.

        File and ``/proc`` reads run in the default executor, never on the loop.
        """
        request_id = request["id"]
        op = request.get("type")
        if op == hub_v1.ClientType.STATUS.value:
            return hub_v1.result(request_id, **await self.status())
        if op == hub_v1.ClientType.STOP_REQUEST.value:
            return hub_v1.result(request_id, **self.offer_stop())
        if op == hub_v1.ClientType.STOP_CONFIRM.value:
            if not self.confirm_stop(request.get("token")):
                return hub_v1.failure(request_id, hub_v1.Reason.STOP_TOKEN_MISMATCH,
                                      "The stop token is unknown, expired or the active set changed.")
            return hub_v1.result(request_id, stopping=True)
        return hub_v1.failure(request_id, hub_v1.Reason.UNSUPPORTED_TYPE, f"unsupported request type: {op!r}")

    async def status(self) -> dict:
        registry = await asyncio.get_running_loop().run_in_executor(None, registry_summary, self.layout)
        return {"hub": dict(self.record or {}), "registry": registry,
                "web": {"state": "not_available"}, "board_agent": None}

    def active(self) -> list:
        """Live things this Hub owns that a stop would end; (A) adds Board agent sets."""
        return []

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
            if not _peer_is_owner(writer):
                return
            if len(self.tasks) >= self.max_connections:
                writer.write(hub_v1.encode_line(hub_v1.reject(hub_v1.Reason.CONNECTION_LIMIT)))
                await writer.drain()
                return
            self.tasks.add(task)
            await Connection(self, reader, writer).run()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.tasks.discard(task)
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
        watchdog = asyncio.create_task(self.watchdog())
        await self.stop_event.wait()
        watchdog.cancel()
        server.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

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

    def __init__(self, hub: Hub, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.hub, self.reader, self.writer = hub, reader, writer
        self.decoder = hub_v1.LineDecoder()
        self.greeted = False

    async def send(self, value: dict) -> None:
        self.writer.write(hub_v1.encode_line(value))
        await self.writer.drain()

    async def run(self) -> None:
        """Serve lines until EOF, a protocol error, a stop or ``idle_timeout`` without a request."""
        deadline = time.monotonic() + self.hub.idle_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
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
            if not hub_v1.valid_client_name(frame.get("client")):
                await self.send(hub_v1.reject(hub_v1.Reason.BAD_REQUEST))
                return False
            self.greeted = True
            await self.send(hub_v1.welcome(self.hub.welcome_identity()))
            return True
        request_id = frame.get("id")
        if frame.get("v") != hub_v1.VERSION or not hub_v1.valid_request_id(request_id):
            await self.send(hub_v1.reject(hub_v1.Reason.PROTOCOL_ERROR))
            return False
        await self.send(await self.hub.handle(LOCAL_PRINCIPAL, frame))
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
    sys.stdout.flush()
    sys.stderr.flush()
    os.replace(layout.log, layout.state_dir / (layout.log.name + ".1"))
    if redirected:
        fresh = os.open(layout.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            for fd in redirected:
                os.dup2(fresh, fd)
        finally:
            os.close(fresh)


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
    if managed_ancestor(os.getpid()) is not None:  # self-check before the lock (HB-03)
        send_ready(ready, READY_MANAGED)
        print("[workbench] Hub refused: inside a WB-managed process tree", file=sys.stderr)
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
    try:
        hub.bind()
        hub.publish_record()
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
