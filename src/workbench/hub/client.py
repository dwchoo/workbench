"""``HubClient``: a verified, blocking hub_v1 connection for ``wb`` processes."""
from __future__ import annotations

import os
import select
import socket
import struct
import time
from typing import Any

from workbench.contracts import hub_v1

from .paths import HubLayout, hub_layout
from .process import RUNNING, HubIdentity, HubUnknown, HubUnresponsive, verified_record


class HubClient:
    """Connect only to the proven Hub; raises ``HubNotRunning``/``HubUnknown``/``HubUnresponsive``.

    Order: ``hub.json`` → ``/proc/locks`` holder → ``process_state`` → socket
    ``SO_PEERCRED`` pid/uid → ``welcome`` pid/start_ticks. The socket location comes
    from the record only. ``timeout`` bounds connect, hello and each request.
    """

    def __init__(self, layout: HubLayout | None = None, *, name: str = "workbench-cli", timeout: float = 5.0):
        self.layout = hub_layout() if layout is None else layout
        self.timeout = timeout
        self.record = verified_record(self.layout)
        self.decoder = hub_v1.LineDecoder()
        self.pending: list[dict[str, Any]] = []
        self.sequence = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._connect(name)
        except BaseException:
            self.sock.close()
            raise

    def _connect(self, name: str) -> None:
        record = self.record
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(record["socket"])
        except socket.timeout as exc:
            # The socket peer is unproven here, so this never authorizes a signal.
            raise HubUnknown("connect_timeout", record) from exc
        except OSError as exc:
            raise HubUnknown("socket_unavailable", record) from exc
        peer_pid, peer_uid, _gid = struct.unpack("iII", self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer_pid != record["pid"] or peer_uid != os.geteuid() or peer_uid != record["uid"]:
            raise HubUnknown("peer_mismatch", record)
        self._send(hub_v1.hello(name))
        frame = self._next(time.monotonic() + self.timeout)
        if frame.get("type") != hub_v1.ServerType.WELCOME.value:
            raise HubUnknown(f"rejected:{frame.get('reason')}", record)
        hub = frame.get("hub") if isinstance(frame.get("hub"), dict) else {}
        if hub.get("pid") != record["pid"] or hub.get("start_ticks") != record["start_ticks"]:
            raise HubUnknown("welcome_mismatch", record)
        self.welcome = hub
        self.identity = HubIdentity(RUNNING, self.layout, record, None, hub)

    def _send(self, value: dict[str, Any]) -> None:
        self.sock.settimeout(self.timeout)
        try:
            self.sock.sendall(hub_v1.encode_line(value))
        except socket.timeout as exc:
            raise HubUnresponsive("timeout", self.record) from exc
        except OSError as exc:
            raise HubUnknown("connection_lost", self.record) from exc

    def _next(self, deadline: float) -> dict[str, Any]:
        while not self.pending:
            remaining = deadline - time.monotonic()
            poller = select.poll()  # not select(): the caller may already hold fds >= 1024
            poller.register(self.sock, select.POLLIN)
            if remaining <= 0 or not poller.poll(max(1, int(remaining * 1000))):
                raise HubUnresponsive("timeout", self.record)
            try:
                data = self.sock.recv(65536)
            except OSError as exc:
                raise HubUnknown("connection_lost", self.record) from exc
            if not data:
                raise HubUnknown("connection_lost", self.record)
            try:
                self.pending.extend(self.decoder.feed(data))
            except hub_v1.ProtocolError as exc:
                raise HubUnknown("protocol_error", self.record) from exc
        return self.pending.pop(0)

    def request(self, op: str, **fields: Any) -> dict[str, Any]:
        """Send one request and return its ``result`` frame (``ok`` may be false)."""
        self.sequence += 1
        request_id = f"c{self.sequence}"
        self._send(hub_v1.request(request_id, op, **fields))
        deadline = time.monotonic() + self.timeout
        while True:
            frame = self._next(deadline)
            if frame.get("type") == hub_v1.ServerType.REJECT.value:
                raise HubUnknown(f"rejected:{frame.get('reason')}", self.record)
            if frame.get("type") == hub_v1.ServerType.RESULT.value and frame.get("id") == request_id:
                return frame

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "HubClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
