"""The Hub relay of WB project panes to the Board workspace (web-board-ux SPEC s5 "Hub 중계: PaneRelay", WBX-D66/D67).

One :class:`PaneRelay` per project: one ui_v1 connection to the project's backend (``<runtime>/ui.sock``) made a shared
attachment (``share_attach``, WBX-E1), shared by every pane WebSocket (``/ws/board/<project_id>:<pane>``) and every
pane action (``POST /api/board/panes/{id}/{action}``) of that project. It keeps a 256 KiB ring per pane, judges each
display frame's ``(session_id, generation)``/``sequence`` (another session or a gap: ``reset``), pipelines web input
in arrival order and remembers which web connection each request came from.

Lifetime: opened by a pane WebSocket's hello or a pane action; closed 30 s after the last pane WebSocket left with no
action in flight (``share_detach``), at once at Hub stop or when the project's registration is gone. A backend that
is off, unreachable or too old for ``share_attach`` is a pane state; the Hub never (re)starts a backend.

Everything here runs on the Hub's event loop; blocking file and ``/proc`` reads go to the default executor.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import socket
import struct
import sys
from typing import Any, Callable

from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, ServerType

from .set import Ring

PANES = ("manager", "worker", "host")
BACKEND_PANES = {"manager": "manager_omp", "worker": "worker_omp", "host": "host_shell"}
FROM_BACKEND = {value: key for key, value in BACKEND_PANES.items()}
_PANE_ID = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(manager|worker|host)\Z")

CLIENT_NAME = "workbench-hub"
RING_BYTES = 256 * 1024  # the backend's REPLAY_BYTES
CLOSE_GRACE = 30.0  # the relay stays this long after its last pane WebSocket left (no re-attach per page reload)
BACKOFF_MAX = 15.0  # reconnect 1 -> 2 -> 4 ... up to this while web clients remain
REFUSED_BACKOFF_MAX = 60.0  # a refused share_attach (not_attached, queue_full) backs off up to this
TOO_OLD_RECHECK = 60.0  # an older backend is checked again after this (or on a new web hello)
ACTION_TIMEOUT = 10.0  # a pane action's answer (more: 504, the outcome is unknown)
CONNECT_TIMEOUT = 5.0  # identity check + connect + hello + share_attach answer
REGISTRY_CHECK = 2.0  # while a relay is open the registry is read this often (``wb hub remove`` -> gone)
MAX_UNANSWERED = 64  # input requests sent and not answered, per relay ...
MAX_UNANSWERED_BYTES = 1 << 20  # ... or their bytes; past either the sending WebSocket stops being read
DEFAULT_SIZE = (30, 100)

MESSAGES = {  # the pane view ``message`` of a state without a screen (fixed sentences)
    "off": "프로젝트 backend가 꺼져 있습니다. 프로젝트를 켜면 pane이 보입니다.",
    "unreachable": "프로젝트 backend에 연결하지 못했습니다. 다시 시도하는 중입니다.",
    "too_old": ("이 프로젝트의 backend는 이전 판입니다. 프로젝트를 끄고 다시 켜면 웹에서 pane을 쓸 수 "
                "있습니다"),
    "unavailable": "이 pane은 지금 backend에 없습니다 (준비 중이거나 끝났습니다).",
}
REFUSED = {  # ``input_refused`` ``detail`` per reason (fixed sentences)
    "backend_off": "프로젝트 backend가 꺼져 있어 입력을 보내지 않았습니다.",
    "pane_unavailable": "pane이 실행 중이 아니어서 입력을 보내지 않았습니다.",
    "input_owner_manager": "host terminal의 입력 주인이 manager입니다. 인수 요청·확인 뒤에 입력할 수 있습니다.",
    "host_shell_automation": "host terminal에서 자동 작업이 진행 중이라 입력을 받지 않았습니다.",
    "input_target_unknown": "입력을 받을 foreground 명령을 확인할 수 없어 보내지 않았습니다.",
    "queue_full": "backend 입력 대기열이 가득 찼습니다. 잠시 뒤 다시 입력하세요.",
    "paste_too_large": "입력이 너무 큽니다.",
    "backend_shutdown": "프로젝트 backend가 끝나는 중입니다.",
}
REFUSED_OTHER = "backend가 입력을 거절했습니다."


def _log(text: str) -> None:
    print(f"[workbench] board panes: {text}", file=sys.stderr, flush=True)


def parse_pane_id(value: object) -> tuple[str, str] | None:
    """``(project_id, pane)`` of a pane id ``<lower-case canonical UUID>:(manager|worker|host)``, else None."""
    if not isinstance(value, str):
        return None
    match = _PANE_ID.fullmatch(value)  # (``$`` would let a trailing line feed through)
    return (match.group(1), match.group(2)) if match else None


def refused_detail(reason: str) -> str:
    return REFUSED.get(reason, REFUSED_OTHER)


class RelayError(Exception):
    """A pane action that did not reach a backend answer: ``backend_off``, ``backend_unreachable``,
    ``backend_too_old``, ``backend_timeout`` (sent, no answer in time: the outcome is unknown)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Unreachable(Exception):
    pass


class _Off(Exception):
    pass


def _identify(root: str) -> tuple[str, int, int, str]:
    """The verified backend endpoint of project ``root``: ``(ui socket, pid, start ticks, boot id)``. Blocking."""
    from workbench.backend.paths import DataDirError, DataLayout
    from workbench.backend.process_identity import BackendIdentityError, inspect_backend
    try:
        identity = inspect_backend(DataLayout(Path(root) / ".wb"))
    except BackendIdentityError as exc:
        raise _Unreachable(f"identity:{exc.reason}") from None
    except (DataDirError, OSError, ValueError) as exc:
        raise _Unreachable(f"identity:{type(exc).__name__}") from None
    if identity.state == "stopped":
        raise _Off(identity.state)
    if identity.state != "running" or identity.record is None:
        raise _Unreachable(f"backend_{identity.state}")
    reference = identity.record["processes"]["backend"]
    return (str(identity.layout.ui_socket), int(reference["pid"]), int(reference["start_ticks"]),
            str(identity.record.get("boot_id") or ""))


def _peer_alive(pid: int, ticks: int, boot_id: str) -> bool:
    from workbench.backend.process_identity import process_state
    return process_state(pid, ticks, uid=os.geteuid(), boot_id=boot_id) == "alive"


def _host_actions(info: dict, alive: bool) -> list[str]:
    """The host buttons for ``info`` (snapshot ``host_shell``), as the TUI judges them."""
    actions: list[str] = []
    shell = info.get("shell") if isinstance(info.get("shell"), dict) else {}
    owner = info.get("input_owner")
    if alive:
        if owner == "manager":
            if shell.get("takeover_requested"):
                actions.append("takeover_confirm")
            else:
                actions.append("takeover_request")
        elif owner == "user" and info.get("automation_hold") is None:
            actions.append("handoff")
    command = info.get("command")
    if isinstance(command, dict) and isinstance(command.get("command_id"), str) and command.get("state") != "stopping":
        actions.append("stop")
    if alive:
        actions.append("kill")
    else:
        actions.append("restart")
    return actions


def _host_command(info: dict) -> dict | None:
    found = info.get("command")
    if isinstance(found, dict) and isinstance(found.get("command_id"), str) and found["command_id"]:
        return {key: found.get(key) for key in ("command_id", "label", "owner", "state")}
    return None


class PaneRelay:
    """One project's shared attachment and its three pane screens. Loop-thread only."""

    def __init__(self, hub: "PaneHub", project_id: str, root: str, name: str):
        self.hub = hub
        self.project_id, self.root, self.name = project_id, root, name
        self.state = "connecting"  # connecting / ready / off / unreachable / too_old
        self.snapshot: dict | None = None
        self.rings = {pane: Ring(RING_BYTES) for pane in PANES}
        self.keys: dict[str, tuple | None] = {pane: None for pane in PANES}  # (session_id, generation) of the ring
        self.sequences: dict[str, int | None] = {pane: None for pane in PANES}
        self.clients = {pane: 0 for pane in PANES}
        self.busy: set[str] = set()  # panes with an action in flight
        self.sizes: dict[str, tuple[int, int] | None] = {pane: None for pane in PANES}  # last known pane size
        self.wanted_size: dict[str, tuple[int, int]] = {}  # a size to set once ready (no TUI)
        self.writer: asyncio.StreamWriter | None = None
        self.task: asyncio.Task | None = None
        self.timer: asyncio.TimerHandle | None = None
        self.poked = asyncio.Event()
        self.settled = asyncio.Event()  # an attempt ended in a state (ready, off, unreachable, too_old)
        self.attempts = 0
        self.pending: dict[str, tuple] = {}
        self.unanswered = 0
        self.unanswered_bytes = 0
        self.room = asyncio.Event()
        self.room.set()
        self.serial = 0
        self.closed = False
        self.views: dict[str, dict] = {}

    # --- views -------------------------------------------------------------------------------------

    def pane_id(self, pane: str) -> str:
        return f"{self.project_id}:{pane}"

    def info(self, pane: str) -> dict | None:
        if self.state != "ready" or not isinstance(self.snapshot, dict):
            return None
        panes = self.snapshot.get("panes")
        info = panes.get(BACKEND_PANES[pane]) if isinstance(panes, dict) else None
        return info if isinstance(info, dict) else None

    def ui_attached(self) -> bool:
        return isinstance(self.snapshot, dict) and self.snapshot.get("attached") is True

    def pane_state(self, pane: str) -> str:
        if self.state != "ready":
            return self.state
        info = self.info(pane)
        if info is None:
            return "unavailable"
        return "running" if info.get("alive", True) is not False else "exited"

    def size(self, pane: str) -> tuple[int, int]:
        info = self.info(pane)
        if info is not None and type(info.get("rows")) is int and type(info.get("cols")) is int:
            return info["rows"], info["cols"]
        return self.sizes[pane] or DEFAULT_SIZE

    def view(self, pane: str) -> dict:
        state = self.pane_state(pane)
        info = self.info(pane) or {}
        host = pane == "host"
        view: dict[str, Any] = {
            "id": self.pane_id(pane), "kind": "pane", "project_id": self.project_id, "project_name": self.name,
            "pane": pane, "name": f"{self.name} · {pane}", "state": state, "size": list(self.size(pane)),
            "ui_attached": self.ui_attached(),
            "input_owner": (info.get("input_owner") if info.get("input_owner") in ("manager", "user") else None)
            if host else "user",
            "automation_hold": info.get("automation_hold") if host else None,
            "command": _host_command(info) if host else None,
            "actions": [], "clients": self.clients[pane], "busy": pane in self.busy,
        }
        if state == "exited":
            view["exit_status"] = info.get("exit_status")
        if state in ("running", "exited"):
            view["actions"] = _host_actions(info, state == "running") if host else (
                ["restart"] if state == "exited" else [])
        if state in MESSAGES:
            view["message"] = MESSAGES[state]
        return view

    def _changed(self) -> None:
        """Tell the pane WebSockets about a changed view (``state``) and a changed pane size (``size``)."""
        for pane in PANES:
            view = self.view(pane)
            before = self.views.get(pane)
            self.views[pane] = view
            if before is not None and before.get("size") != view["size"]:
                self.hub.emit(self.pane_id(pane), "size", tuple(view["size"]))
            if before != view:
                self.hub.emit(self.pane_id(pane), "state", view)

    def _set_state(self, state: str) -> None:
        self.state = state
        self._changed()

    # --- lifetime ---------------------------------------------------------------------------------------

    def want(self) -> None:
        """Opened (a pane WebSocket's hello, a pane action): run the connection if it is not running."""
        if self.closed:
            return
        if self.task is None or self.task.done():
            self.task = asyncio.ensure_future(self._run())
        self.check_idle()

    def poke(self) -> None:
        """Try again now (a new web hello for a too-old backend, a project seen ``on``)."""
        self.poked.set()

    def set_clients(self, pane: str, count: int) -> None:
        self.clients[pane] = count
        self.views[pane] = self.view(pane)
        self.check_idle()

    def idle(self) -> bool:
        """No pane WebSocket, no action in flight and no ``stop_command`` final outcome still to come."""
        return (not any(self.clients.values()) and not self.busy
                and not any(entry[0] == "stop_final" for entry in self.pending.values()))

    def check_idle(self) -> None:
        """Idle: close after the grace; anything else cancels that."""
        idle = self.idle()
        if idle and self.timer is None and not self.closed:
            self.timer = asyncio.get_running_loop().call_later(self.hub.grace, self._idle_timeout)
        elif not idle and self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def _idle_timeout(self) -> None:
        self.timer = None
        if self.idle():  # (a connection that attached meanwhile cancelled the timer; this checks once more)
            asyncio.ensure_future(self.close("idle"))

    async def close(self, why: str, *, detach: bool = True) -> None:
        """End the relay (idle grace, Hub stop, registration gone): ``share_detach`` best effort, then the socket."""
        if self.closed:
            return
        self.closed = True
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        writer, self.writer = self.writer, None
        if writer is not None and detach:
            try:
                writer.write(ui_v1.encode_frame(ui_v1.request(ClientType.SHARE_DETACH, self._next_id())))
                await asyncio.wait_for(writer.drain(), 1.0)
            except Exception:
                pass
        if writer is not None:
            writer.close()
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self._fail_pending()
        if self.hub.relays.get(self.project_id) is self:
            del self.hub.relays[self.project_id]
        self.settled.set()
        _log(f"relay {self.project_id} closed ({why})")

    def _fail_pending(self) -> None:
        for request_id, entry in list(self.pending.items()):
            kind = entry[0]
            if kind == "action" and not entry[1].done():
                entry[1].set_exception(RelayError("backend_timeout"))
            elif kind == "stop_final":
                _, pane, command_id, address = entry
                self.hub.record("pane_stop", address, self.detail(pane, command_id=command_id,
                                                                  outcome="unknown"))
        self.pending.clear()
        self.unanswered = self.unanswered_bytes = 0
        self.room.set()

    async def _run(self) -> None:
        delay = 1.0
        while not self.closed:
            self.poked.clear()
            outcome = await self._session()
            if outcome == "attached":
                delay = 1.0
                continue  # the attached session ended (EOF, closing): try again at once, then back off
            if self.closed:
                return
            if outcome == "too_old":
                wait = self.hub.too_old_recheck
            else:
                cap = self.hub.refused_backoff_max if outcome == "refused" else self.hub.backoff_max
                wait = min(delay, cap)
                delay = min(delay * 2, cap)
            try:
                await asyncio.wait_for(self.poked.wait(), wait)
            except asyncio.TimeoutError:
                pass

    def _settle(self, state: str) -> None:
        self.attempts += 1
        self._set_state(state)
        self.settled.set()
        self.settled = asyncio.Event()

    async def _session(self) -> str:
        """One connection: verify, attach, read until it ends. ``attached`` when it was ready once; else why not."""
        loop = asyncio.get_running_loop()
        if self.state not in ("ready", "connecting"):
            self._set_state("connecting")
        try:
            path, pid, ticks, boot_id = await loop.run_in_executor(None, _identify, self.root)
        except _Off:
            self._settle("off")
            return "off"
        except _Unreachable as exc:
            _log(f"relay {self.project_id}: {exc}")
            self._settle("unreachable")
            return "unreachable"
        except Exception as exc:
            _log(f"relay {self.project_id}: identity {type(exc).__name__}")
            self._settle("unreachable")
            return "unreachable"
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), self.hub.connect_timeout)
            sock = writer.get_extra_info("socket")
            peer_pid, peer_uid, _gid = struct.unpack("iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if peer_uid != os.geteuid() or peer_pid != pid:
                raise _Unreachable("peer is not the recorded backend")
            if not await loop.run_in_executor(None, _peer_alive, pid, ticks, boot_id):
                raise _Unreachable("backend process changed")
            decoder = ui_v1.FrameDecoder()
            frames: list = []
            writer.write(ui_v1.encode_frame(ui_v1.hello(CLIENT_NAME)))
            welcome = await self._next_frame(reader, decoder, frames)
            if welcome.header.get("type") != ServerType.WELCOME.value or welcome.header.get("backend_pid") != pid:
                raise _Unreachable("welcome does not name the recorded backend")
            request_id = self._next_id()
            writer.write(ui_v1.encode_frame(ui_v1.request(ClientType.SHARE_ATTACH, request_id)))
            while True:
                answer = await self._next_frame(reader, decoder, frames)
                if answer.header.get("type") == ServerType.RESULT.value and answer.header.get("id") == request_id:
                    break
                if answer.header.get("type") in (ServerType.CLOSING.value, ServerType.REJECT.value):
                    raise _Unreachable(f"closed: {answer.header.get('reason')}")
        except _Unreachable as exc:
            _log(f"relay {self.project_id}: {exc}")
            if writer is not None:
                writer.close()
            self._settle("unreachable")
            return "unreachable"
        except (OSError, asyncio.TimeoutError, ui_v1.ProtocolError, struct.error, EOFError) as exc:
            _log(f"relay {self.project_id}: connect {type(exc).__name__}")
            if writer is not None:
                writer.close()
            self._settle("unreachable")
            return "unreachable"
        header = answer.header
        if header.get("ok") is not True:
            writer.close()
            reason = header.get("reason")
            if reason == ui_v1.Reason.UNSUPPORTED_TYPE.value:
                _log(f"relay {self.project_id}: the backend predates share_attach")
                self._settle("too_old")
                return "too_old"
            _log(f"relay {self.project_id}: share_attach refused ({reason})")
            self._settle("unreachable")
            return "refused" if reason in (ui_v1.Reason.NOT_ATTACHED.value, ui_v1.Reason.QUEUE_FULL.value) \
                else "unreachable"
        if self.closed:
            writer.close()
            return "closed"
        self.writer = writer
        snapshot = header.get("snapshot")
        self.snapshot = snapshot if isinstance(snapshot, dict) else {}
        for pane in PANES:  # every new shared attach: the screens start again from the backend's replay
            self._reset(pane)
        self._take_sizes()
        self._settle("ready")
        self._send_wanted_sizes()
        try:
            for frame in frames:
                self._frame(frame)
            while not self.closed:
                data = await reader.read(65536)
                if not data:
                    break
                for frame in decoder.feed(data):
                    if self._frame(frame):
                        raise EOFError
        except (OSError, EOFError, ui_v1.ProtocolError) as exc:
            if not isinstance(exc, EOFError):
                _log(f"relay {self.project_id}: read {type(exc).__name__}")
        finally:
            if self.writer is writer:
                self.writer = None
            writer.close()
        if not self.closed:
            self._fail_pending()
            self._set_state("unreachable")
            _log(f"relay {self.project_id}: the backend connection ended")
        return "attached"

    async def _next_frame(self, reader: asyncio.StreamReader, decoder: ui_v1.FrameDecoder, frames: list):
        while not frames:
            data = await asyncio.wait_for(reader.read(65536), self.hub.connect_timeout)
            if not data:
                raise EOFError
            frames.extend(decoder.feed(data))
        return frames.pop(0)

    def _next_id(self) -> str:
        self.serial += 1
        return f"h{self.serial}"

    # --- backend frames --------------------------------------------------------------------------------

    def _frame(self, frame: ui_v1.Frame) -> bool:
        """One backend frame; True when the backend ends the connection (``closing``)."""
        kind = frame.header.get("type")
        if kind == ServerType.DISPLAY.value:
            self._display(frame)
        elif kind == ServerType.STATE.value:
            snapshot = frame.header.get("snapshot")
            if isinstance(snapshot, dict):
                self.snapshot = snapshot
                self._take_sizes()
                self._changed()
        elif kind == ServerType.RESULT.value:
            self._result(frame.header)
        elif kind in (ServerType.CLOSING.value, ServerType.REJECT.value):
            _log(f"relay {self.project_id}: backend closing ({frame.header.get('reason')})")
            return True
        return False

    def _reset(self, pane: str) -> None:
        self.rings[pane] = Ring(RING_BYTES)
        self.keys[pane] = self.sequences[pane] = None
        self.hub.emit(self.pane_id(pane), "reset", None)

    def _display(self, frame: ui_v1.Frame) -> None:
        header = frame.header
        pane = FROM_BACKEND.get(header.get("pane"))
        if pane is None or frame.discarded_bytes:
            return
        key = (header.get("session_id"), header.get("generation"))
        sequence = header.get("sequence")
        known, last = self.keys[pane], self.sequences[pane]
        if known is not None:
            if known != key:
                self._reset(pane)  # a restarted pane or a new backend: the screen starts again
            elif type(sequence) is not int or type(last) is not int or sequence != last + 1:
                _log(f"relay {self.project_id}: {pane} sequence {last} -> {sequence}; reset")
                self._reset(pane)
        self.keys[pane], self.sequences[pane] = key, sequence
        if frame.payload:
            self.rings[pane].append(frame.payload)
            self.hub.emit(self.pane_id(pane), "data", frame.payload)

    def _take_sizes(self) -> None:
        for pane in PANES:
            info = self.info(pane) if self.state == "ready" else None
            if info is None and isinstance(self.snapshot, dict):
                panes = self.snapshot.get("panes")
                info = panes.get(BACKEND_PANES[pane]) if isinstance(panes, dict) else None
            if isinstance(info, dict) and type(info.get("rows")) is int and type(info.get("cols")) is int:
                self.sizes[pane] = (info["rows"], info["cols"])

    def _result(self, header: dict) -> None:
        request_id = header.get("id")
        entry = self.pending.get(request_id) if isinstance(request_id, str) else None
        if entry is None:
            return
        kind = entry[0]
        if kind == "input":
            del self.pending[request_id]
            _, _pane, size, refused = entry
            self.unanswered -= 1
            self.unanswered_bytes -= size
            if self.unanswered < self.hub.max_unanswered and self.unanswered_bytes < self.hub.max_unanswered_bytes:
                self.room.set()
            if header.get("ok") is not True:
                reason = header.get("reason")
                refused(reason if isinstance(reason, str) else "unknown")
        elif kind == "resize":
            del self.pending[request_id]  # resize_owned_by_ui: the TUI just attached; the next state says so
        elif kind == "action":
            del self.pending[request_id]
            _, future, pane, command_id, address = entry
            if not future.done():
                future.set_result(header)
            if header.get("ok") is True and command_id is not None and header.get("final") is not True:
                self.pending[request_id] = ("stop_final", pane, command_id, address)
        elif kind == "stop_final" and header.get("final") is True:
            del self.pending[request_id]
            _, pane, command_id, address = entry
            stop = header.get("stop") if isinstance(header.get("stop"), dict) else {}
            self.hub.emit(self.pane_id(pane), "notice", {"t": "notice", "action": "stop", "command_id": command_id,
                                                         "stop": stop})
            outcome = stop.get("status")
            if stop.get("reason"):
                outcome = f"{outcome}:{stop.get('reason')}"
            self.hub.record("pane_stop", address, self.detail(pane, command_id=command_id, outcome=outcome))
            self.check_idle()

    # --- requests -------------------------------------------------------------------------------------

    def _write(self, kind: ClientType, fields: dict, payload: bytes = b"") -> str:
        request_id = self._next_id()
        self.writer.write(ui_v1.encode_frame(ui_v1.request(kind, request_id, **fields), payload))
        return request_id

    def input(self, pane: str, data: bytes, refused: Callable[[str], None]) -> None:
        """One web frame as one ``input`` request, in call order; a refusal goes back through ``refused``."""
        state = self.pane_state(pane)
        if state == "off":
            refused("backend_off")
            return
        if state != "running" or self.writer is None:
            refused("pane_unavailable")
            return
        request_id = self._write(ClientType.INPUT, {"pane": BACKEND_PANES[pane]}, data)
        self.pending[request_id] = ("input", pane, len(data), refused)
        self.unanswered += 1
        self.unanswered_bytes += len(data)
        if self.unanswered >= self.hub.max_unanswered or self.unanswered_bytes >= self.hub.max_unanswered_bytes:
            self.room.clear()

    def input_full(self) -> bool:
        return not self.room.is_set()

    def resize(self, pane: str, size: tuple[int, int]) -> None:
        """The web size owner's size for ``pane``: sent only while no TUI is attached and it differs."""
        if self.state != "ready" or self.writer is None:
            self.wanted_size[pane] = size
            return
        if self.ui_attached() or self.sizes[pane] == size:
            return
        rows, cols = size
        request_id = self._write(ClientType.RESIZE, {"pane": BACKEND_PANES[pane], "rows": rows, "cols": cols})
        self.pending[request_id] = ("resize", pane)
        self.sizes[pane] = size

    def _send_wanted_sizes(self) -> None:
        wanted, self.wanted_size = self.wanted_size, {}
        for pane, size in wanted.items():
            self.resize(pane, size)

    async def action(self, pane: str, kind: ClientType, fields: dict, *, address: str | None,
                     command_id: str | None = None) -> dict:
        """One pane action's backend answer (its result header); :class:`RelayError` without one."""
        self.want()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.hub.action_timeout
        if self.state != "ready":
            if self.state in ("off", "unreachable", "too_old"):
                self.poke()  # closed or backing off: an action opens it (one fresh attempt)
                attempts = self.attempts
                while self.attempts == attempts and not self.closed:
                    await self._wait_settled(deadline)
            while self.state == "connecting" and not self.closed:
                await self._wait_settled(deadline)
        if self.state != "ready" or self.writer is None:
            raise RelayError({"off": "backend_off", "too_old": "backend_too_old"}.get(self.state,
                                                                                  "backend_unreachable"))
        future = loop.create_future()
        request_id = self._write(kind, fields)
        self.pending[request_id] = ("action", future, pane, command_id, address)
        try:
            return await asyncio.wait_for(asyncio.shield(future), max(0.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            raise RelayError("backend_timeout") from None

    async def _wait_settled(self, deadline: float) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RelayError("backend_timeout")
        try:
            await asyncio.wait_for(self.settled.wait(), remaining)
        except asyncio.TimeoutError:
            raise RelayError("backend_timeout") from None

    def detail(self, pane: str, **more) -> dict:
        return {"pane_id": self.pane_id(pane), "project_id": self.project_id, "project_name": self.name,
                "pane": pane, **more}


Emit = Callable[[str, str, Any], None]  # (pane id, event, value): data, reset, state, size, notice, gone


class PaneHub:
    """Every project's :class:`PaneRelay`, the registry view and the event fan-out to the pane WebSockets."""

    def __init__(self, entries: Callable[[], list], *, power: Callable[[], Any] | None = None,
                 audit: Callable[[str, str | None, dict], None] | None = None, grace: float = CLOSE_GRACE,
                 backoff_max: float = BACKOFF_MAX, refused_backoff_max: float = REFUSED_BACKOFF_MAX,
                 too_old_recheck: float = TOO_OLD_RECHECK, action_timeout: float = ACTION_TIMEOUT,
                 connect_timeout: float = CONNECT_TIMEOUT, registry_check: float = REGISTRY_CHECK,
                 max_unanswered: int = MAX_UNANSWERED, max_unanswered_bytes: int = MAX_UNANSWERED_BYTES):
        """``entries()`` (blocking): the registered projects (``hub.registry.Entry``) in registry order.
        ``power()`` (awaitable): the Hub list (``{rows: [{project_id, power}]}``, the dashboard's value).
        ``audit(event, address, detail)`` (blocking): one ``audit`` row (actor ``owner``)."""
        self.entries = entries
        self.power = power
        self.audit = audit
        self.grace, self.backoff_max, self.refused_backoff_max = grace, backoff_max, refused_backoff_max
        self.too_old_recheck, self.action_timeout = too_old_recheck, action_timeout
        self.connect_timeout, self.registry_check = connect_timeout, registry_check
        self.max_unanswered, self.max_unanswered_bytes = max_unanswered, max_unanswered_bytes
        self.relays: dict[str, PaneRelay] = {}
        self.known: dict[str, Any] = {}  # project id -> its last read registry entry
        self.watchers: list[Emit] = []
        self.watcher: asyncio.Task | None = None
        self._audit_tail: asyncio.Future | None = None
        self.stopped = False

    def watch(self, callback: Emit) -> Callable[[], None]:
        self.watchers.append(callback)

        def unwatch() -> None:
            if callback in self.watchers:
                self.watchers.remove(callback)
        return unwatch

    def emit(self, pane_id: str, event: str, value: Any) -> None:
        for callback in list(self.watchers):
            try:
                callback(pane_id, event, value)
            except Exception as exc:  # one bad watcher never stops the relay
                _log(f"watcher failed: {exc!r}")

    def record(self, event: str, address: str | None, detail: dict) -> None:
        """Queue one audit row; written in order in the executor, never delaying the relay."""
        if self.audit is None:
            return
        tail = self._audit_tail
        audit = self.audit

        async def write() -> None:
            if tail is not None:
                await asyncio.gather(tail, return_exceptions=True)
            try:
                await asyncio.get_running_loop().run_in_executor(None, audit, event, address, detail)
            except Exception as exc:
                _log(f"audit {event} failed: {type(exc).__name__}")
        self._audit_tail = asyncio.ensure_future(write())

    # --- the registry ----------------------------------------------------------------------------------

    async def read(self) -> list | None:
        """The registry entries (None when unreadable); relays of projects no longer registered end (``gone``)."""
        try:
            entries = list(await asyncio.get_running_loop().run_in_executor(None, self.entries))
        except Exception:
            return None
        self.known = {entry.project_id: entry for entry in entries}
        for project_id, relay in list(self.relays.items()):
            entry = self.known.get(project_id)
            if entry is None:
                for pane in PANES:
                    self.emit(relay.pane_id(pane), "gone", None)
                await relay.close("registration gone")
            else:
                relay.name = entry.name
        return entries

    async def entry(self, project_id: str):
        """The registered entry of ``project_id`` read now, else None."""
        await self.read()
        return self.known.get(project_id)

    def name(self, project_id: str) -> str | None:
        entry = self.known.get(project_id)
        return entry.name if entry is not None else None

    async def _watch_registry(self) -> None:
        while self.relays and not self.stopped:
            await asyncio.sleep(self.registry_check)
            await self.read()

    def relay(self, project_id: str) -> PaneRelay | None:
        """The open relay of a registered project, opened when it is not (the caller read the registry)."""
        if self.stopped:
            return None
        relay = self.relays.get(project_id)
        if relay is None or relay.closed:  # one still closing (share_detach, task cancel) is gone: a fresh one
            entry = self.known.get(project_id)
            if entry is None:
                return None
            relay = self.relays[project_id] = PaneRelay(self, project_id, str(entry.root), entry.name)
            relay.views = {pane: relay.view(pane) for pane in PANES}
            if self.watcher is None or self.watcher.done():
                self.watcher = asyncio.ensure_future(self._watch_registry())
        relay.want()
        return relay

    # --- the panel list ------------------------------------------------------------------------------

    async def projects(self) -> list[dict]:
        """``GET /api/board`` ``projects``: every registered project in registry order with its three pane views;
        never connects to a backend (``power`` is the Hub list's value; a pane without a relay reads from it)."""
        entries = await self.read()
        if entries is None:
            return []
        powers: dict[str, str] = {}
        if self.power is not None:
            try:
                listed = await self.power()
                powers = {row.get("project_id"): row.get("power") for row in listed.get("rows") or ()}
            except Exception as exc:
                _log(f"power list failed: {type(exc).__name__}")
        found = []
        for entry in entries:
            power = powers.get(entry.project_id)
            power = power if power in ("on", "off") else "unknown"
            relay = self.relays.get(entry.project_id)
            if relay is not None and relay.state in ("off", "unreachable") and power == "on":
                relay.poke()
            panes = [relay.view(pane) if relay is not None else self.idle_view(entry, pane, power) for pane in PANES]
            found.append({"project_id": entry.project_id, "name": entry.name, "power": power, "panes": panes})
        return found

    @staticmethod
    def idle_view(entry, pane: str, power: str) -> dict:
        state = {"on": "idle", "off": "off"}.get(power, "unreachable")
        view = {"id": f"{entry.project_id}:{pane}", "kind": "pane", "project_id": entry.project_id,
                "project_name": entry.name, "pane": pane, "name": f"{entry.name} · {pane}", "state": state,
                "size": list(DEFAULT_SIZE), "ui_attached": False,
                "input_owner": None if pane == "host" else "user", "automation_hold": None, "command": None,
                "actions": [], "clients": 0, "busy": False}
        if state in MESSAGES:
            view["message"] = MESSAGES[state]
        return view

    async def stop(self) -> None:
        """Hub stop: every relay ends now (best-effort ``share_detach``); queued audit rows are written."""
        self.stopped = True
        for relay in list(self.relays.values()):
            await relay.close("hub stop")
        if self.watcher is not None:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
        while self._audit_tail is not None and not self._audit_tail.done():
            await asyncio.gather(self._audit_tail, return_exceptions=True)
