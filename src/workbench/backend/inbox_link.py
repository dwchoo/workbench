"""The backend's own hub_v1 connection (Task Inbox SPEC "Backend → Hub (hub_v1, backend principal)").

The backend keeps one connection to the Hub with hello ``client: workbench-backend`` and its ``project_id``; the Hub
grants the backend principal only when the socket peer is the running backend recorded for that project. The link
never starts a Hub. A background thread reconnects every :data:`RECONNECT_INTERVAL` seconds while disconnected, and a
call that finds no connection tries once at once, unless the last connect failed less than ``reconnect_interval`` ago
(then it is ``unavailable``/``backoff`` without a new attempt). Each call waits at most ``timeout`` (:data:`CALL_TIMEOUT`) and
returns a :class:`LinkResult` whose ``status`` is

- ``ok``: the Hub answered ``ok: true`` (``value`` = the result fields);
- ``rejected``: the Hub answered ``ok: false`` (``reason`` = its code, nothing was recorded by that call);
- ``unavailable``: nothing reached the Hub (no Hub, not verifiable, principal refused, send failed);
- ``unknown``: the request was sent but no answer came (timeout, connection lost): it may have been recorded.

Nothing is retried automatically after a send (P2-AC-11). Calls are serialized on one connection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import select
import socket
import struct
import threading
import time
from typing import Any, Mapping

from workbench.contracts import hub_v1

RECONNECT_INTERVAL = 5.0
CALL_TIMEOUT = 5.0


@dataclass(frozen=True)
class LinkResult:
    status: str  # ok | rejected | unavailable | unknown
    reason: str | None = None
    detail: str | None = None
    value: dict[str, Any] = field(default_factory=dict)


class HubPeerCheck:
    """``check(pid)``: is ``pid`` the verified running Hub (``hub.process.verified_record``: the ``hub.json`` of the
    live ``hub.lock`` holder with exact start ticks), and does ``/proc/<pid>`` still have those start ticks? The Hub
    identity is cached for ``ttl`` seconds so repeated checks do not read the disk; a mismatch reads it again at most
    every ``refresh`` seconds (a Hub that just restarted). Any failure is "not the Hub"."""

    def __init__(self, layout: Any, *, ttl: float = 2.0, refresh: float = 0.5):
        self.layout, self.ttl, self.refresh = layout, ttl, refresh
        self._lock = threading.Lock()
        self._identity: tuple[int, int] | None = None
        self._read_at: float | None = None

    def _read(self) -> None:
        from workbench.hub import process as hub_process
        try:
            record = hub_process.verified_record(self.layout)
            self._identity = (int(record["pid"]), int(record["start_ticks"]))
        except Exception:
            self._identity = None
        self._read_at = time.monotonic()

    def __call__(self, pid: int) -> bool:
        from workbench.hub.process import start_ticks
        with self._lock:
            now = time.monotonic()
            if self._read_at is None or now - self._read_at >= self.ttl:
                self._read()
            elif (self._identity is None or self._identity[0] != pid) and now - self._read_at >= self.refresh:
                self._read()
            identity = self._identity
        return identity is not None and identity[0] == pid and start_ticks(pid) == identity[1]


class _Unavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class InboxLink:
    def __init__(self, project_id: str, *, environment: Mapping[str, str] | None = None, root: str | None = None,
                 timeout: float = CALL_TIMEOUT, reconnect_interval: float = RECONNECT_INTERVAL):
        """``root``: the project root; when given, each connect first reads the Hub registry (read only) and
        contacts the Hub only if it lists ``project_id`` with this root (a backend of an unregistered project, e.g.
        an in-process test under a real HOME, never touches a Hub)."""
        from workbench.hub.paths import hub_layout
        self.project_id = project_id
        self.root = root
        self.layout = hub_layout(dict(os.environ if environment is None else environment))
        self.timeout = timeout
        self.reconnect_interval = reconnect_interval
        self._lock = threading.Lock()  # one call (or connect) at a time on the connection
        self._sock: socket.socket | None = None
        self._decoder = hub_v1.LineDecoder()
        self._pending: list[dict[str, Any]] = []
        self._sequence = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = "disconnected"  # connected | disconnected
        self.last_reason: str | None = None
        self._failed_at: float | None = None  # monotonic time of the last failed connect (call backoff)

    # -- lifetime ------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._keep, name="inbox-link", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.timeout + 1.0)
        with self._lock:
            self._drop()

    def _keep(self) -> None:
        while not self._stop.is_set():
            if self._lock.acquire(timeout=0.1):
                try:
                    if self._sock is None:
                        try:
                            self._connect()
                        except _Unavailable:
                            pass
                    else:
                        self._check_alive()
                finally:
                    self._lock.release()
            self._stop.wait(self.reconnect_interval if self._sock is None else min(1.0, self.reconnect_interval))

    # -- connection (caller holds ``_lock``) ----------------------------------------------
    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock, self._pending, self._decoder = None, [], hub_v1.LineDecoder()
        self.state = "disconnected"

    def _connect(self) -> None:
        """Verified like ``HubClient``: record → lock holder → live process → peer pid/uid → welcome identity."""
        from workbench.hub.process import HubNotRunning, HubUnknown, verified_record
        if self.root is not None and not self._registered():
            raise self._unavailable("not_registered")
        try:
            record = verified_record(self.layout)
        except HubNotRunning:
            raise self._unavailable("hub_not_running") from None
        except HubUnknown as exc:
            raise self._unavailable(f"hub_unknown:{exc.reason}") from None
        except (OSError, ValueError):
            raise self._unavailable("hub_unreadable") from None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock = sock
        try:
            sock.settimeout(self.timeout)
            sock.connect(record["socket"])
            pid, uid, _gid = struct.unpack("iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid != record["pid"] or uid != os.geteuid() or uid != record["uid"]:
                raise _Unavailable("peer_mismatch")
            sock.sendall(hub_v1.encode_line(hub_v1.hello(hub_v1.BACKEND_CLIENT, project_id=self.project_id)))
            frame = self._next(time.monotonic() + self.timeout)
            if frame is None:
                raise _Unavailable("no_welcome")
            if frame.get("type") != hub_v1.ServerType.WELCOME.value:
                raise _Unavailable(str(frame.get("reason") or "rejected"))
            hub = frame.get("hub") if isinstance(frame.get("hub"), dict) else {}
            if hub.get("pid") != record["pid"] or hub.get("start_ticks") != record["start_ticks"]:
                raise _Unavailable("welcome_mismatch")
        except _Unavailable as exc:
            self._drop()
            raise self._unavailable(exc.reason) from None
        except (OSError, hub_v1.ProtocolError, KeyError, TypeError) as exc:
            self._drop()
            raise self._unavailable(f"connect_failed:{type(exc).__name__}") from None
        self.state, self.last_reason, self._failed_at = "connected", None, None

    def _registered(self) -> bool:
        from workbench.hub import registry
        try:
            entries = registry.read(self.layout)
        except (registry.RegistryError, OSError):
            return False
        return any(entry.project_id == self.project_id and entry.root == self.root for entry in entries)

    def _unavailable(self, reason: str) -> _Unavailable:
        self.last_reason, self._failed_at = reason, time.monotonic()
        return _Unavailable(reason)

    def _next(self, deadline: float) -> dict[str, Any] | None:
        """The next frame, or None on timeout/EOF; a broken stream raises ``ProtocolError``."""
        assert self._sock is not None
        while not self._pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            poller = select.poll()
            poller.register(self._sock, select.POLLIN)
            if not poller.poll(max(1, int(remaining * 1000))):
                return None
            try:
                data = self._sock.recv(65536)
            except OSError:
                return None
            if not data:
                return None
            self._pending.extend(self._decoder.feed(data))
        return self._pending.pop(0)

    def _check_alive(self) -> None:
        """Idle connection: an EOF or any unsolicited frame (a ``reject``) means it is gone."""
        assert self._sock is not None
        poller = select.poll()
        poller.register(self._sock, select.POLLIN)
        if poller.poll(0):
            self._drop()

    # -- calls -----------------------------------------------------------------------------
    def call(self, op: str, **fields: Any) -> LinkResult:
        with self._lock:
            for attempt in (1, 2):
                if self._sock is None:
                    if (attempt == 1 and self._failed_at is not None
                            and time.monotonic() - self._failed_at < self.reconnect_interval):
                        return LinkResult("unavailable", "backoff", self.last_reason)  # no new attempt yet
                    try:
                        self._connect()
                    except _Unavailable as exc:
                        return LinkResult("unavailable", exc.reason)
                self._sequence += 1
                request_id = f"b{self._sequence}"
                try:
                    line = hub_v1.encode_line(hub_v1.request(request_id, op, **fields))
                except (hub_v1.ProtocolError, TypeError, ValueError) as exc:
                    return LinkResult("rejected", "invalid_input", str(exc))
                try:
                    self._sock.settimeout(self.timeout)
                    self._sock.sendall(line)
                except OSError:
                    # the whole line (its newline last) did not go out, so the Hub cannot have acted on it
                    self._drop()
                    if attempt == 1:
                        continue
                    return LinkResult("unavailable", "send_failed")
                return self._answer(request_id)
            return LinkResult("unavailable", "send_failed")

    def _answer(self, request_id: str) -> LinkResult:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                frame = self._next(deadline)
            except hub_v1.ProtocolError:
                frame = None
            if frame is None:
                self._drop()  # a late answer must not be read as the next call's
                return LinkResult("unknown", "no_answer")
            if frame.get("type") == hub_v1.ServerType.REJECT.value:
                self._drop()
                return LinkResult("unknown", str(frame.get("reason")))
            if frame.get("type") != hub_v1.ServerType.RESULT.value or frame.get("id") != request_id:
                continue
            value = {key: item for key, item in frame.items() if key not in ("v", "id", "type", "ok")}
            if frame.get("ok") is True:
                return LinkResult("ok", value=value)
            if frame.get("reason") == hub_v1.Reason.PRINCIPAL_MISMATCH.value:
                self._drop()  # the Hub closes it too
            return LinkResult("rejected", str(frame.get("reason")), str(frame.get("detail") or ""), value)
