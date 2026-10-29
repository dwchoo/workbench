"""The Board session API (web-board-ux SPEC s4 "Session API: 화면과 panel 동작", WBX-D44..D56).

Session routes only (``@api``): no session -> 401, a state change without the session's ``X-CSRF-Token`` -> 403 (the
middleware); a Bearer API key is never read (AC-08/38). Bodies are one JSON object of at most 4 KiB with only the
route's fields (else 400 ``bad_input``); replies are JSON, ``Cache-Control: no-store``: ``{"ok": true, ...}`` or
``{"ok": false, "reason": <code>, "detail": <fixed sentence>}``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable

from aiohttp import web

from workbench.contracts.ui_v1 import ClientType

from ...board import panes as pane_relay
from ...board import registry
from ...board.set import VISIBLE_MAX, BoardError
from ..middleware import SERVICES, api

BODY_MAX = 4096
STATUS = {
    "bad_input": 400, "name_invalid": 400, "confirm_required": 400, "limit_out_of_range": 400, "wrong_kind": 400,
    "bad_state": 400, "unknown_terminal": 404, "name_taken": 409, "limit_reached": 409, "limit_below_count": 409,
    "terminal_busy": 409, "registry_write_failed": 500, "delete_failed": 500, "board_unavailable": 503,
    "registry_invalid": 503,
    # project panes (SPEC s5 "Pane 동작 API")
    "wrong_pane": 400, "backend_refused": 409, "backend_off": 503, "backend_unreachable": 503,
    "backend_too_old": 503, "backend_timeout": 504,
}
DETAIL = {  # fixed sentences: never an exception text or a path
    "bad_input": "요청 형식이 맞지 않습니다.",
    "name_invalid": "이름은 앞뒤 공백을 뺀 1~40자이고 제어 문자가 없어야 합니다.",
    "confirm_required": "확인이 필요한 동작입니다.",
    "limit_out_of_range": "한도는 agent 1~8, shell 1~32입니다.",
    "wrong_kind": "이 terminal 종류에는 할 수 없는 동작입니다.",
    "bad_state": "지금 상태에서는 할 수 없는 동작입니다.",
    "unknown_terminal": "그런 terminal이 없습니다.",
    "name_taken": "같은 종류에 같은 이름(대소문자 무시)이 이미 있습니다.",
    "limit_reached": "한도에 닿아 더 만들 수 없습니다.",
    "limit_below_count": "지금 개수보다 작은 한도는 정할 수 없습니다. 먼저 닫거나 삭제하세요.",
    "terminal_busy": "이 terminal의 다른 동작이 진행 중입니다.",
    "registry_write_failed": "Board 목록 파일을 쓰지 못했습니다. 아무것도 바뀌지 않았습니다.",
    "delete_failed": "Board agent의 home을 지우지 못했습니다. 목록에 남아 있으니 고친 뒤 다시 삭제하세요.",
    "board_unavailable": "Board를 사용할 수 없습니다 (웹이 꺼졌거나 Hub가 멈추는 중입니다).",
    "registry_invalid": "Board 목록 파일을 읽을 수 없습니다. wb hub status의 안내를 따르세요.",
    "wrong_pane": "이 pane에는 할 수 없는 동작입니다 (host 전용).",
    "backend_refused": "프로젝트 backend가 요청을 거절했습니다.",
    "backend_off": "프로젝트 backend가 꺼져 있습니다.",
    "backend_unreachable": "프로젝트 backend에 연결하지 못했습니다.",
    "backend_too_old": ("이 프로젝트의 backend는 이전 판입니다. 프로젝트를 끄고 다시 켜면 웹에서 pane을 쓸 수 "
                        "있습니다"),
    "backend_timeout": "결과를 알 수 없음 — pane 상태를 확인하세요.",
}
NO_STORE = {"Cache-Control": "no-store"}


class _BadInput(Exception):
    pass


def reply(value: dict, status: int = 200) -> web.Response:
    return web.json_response({"ok": True, **value}, status=status, headers=NO_STORE)


def refusal(reason: str) -> web.Response:
    return web.json_response({"ok": False, "reason": reason, "detail": DETAIL.get(reason, DETAIL["bad_input"])},
                             status=STATUS.get(reason, 400), headers=NO_STORE)


async def _body(request: web.Request, fields: tuple[str, ...]) -> dict:
    if request.content_type != "application/json":
        raise _BadInput
    if request.content_length is not None and request.content_length > BODY_MAX:
        raise _BadInput
    raw = await request.content.read(BODY_MAX + 1)
    if len(raw) > BODY_MAX:
        raise _BadInput
    try:
        value = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _BadInput from None
    if not isinstance(value, dict) or set(value) - set(fields):
        raise _BadInput
    return value


def _confirmed(body: dict) -> None:
    if body.get("confirm") is not True:
        raise BoardError("confirm_required")


# Pane actions (SPEC s5 "Pane 동작 API"): accepted body fields, host only, ui_v1 request type
PANE_ACTIONS = {
    "takeover_request": ((), True, ClientType.TAKEOVER_REQUEST),
    "takeover_confirm": ((), True, ClientType.TAKEOVER_CONFIRM),
    "handoff": ((), True, ClientType.HANDOFF),
    "stop": (("confirm", "command_id"), True, ClientType.STOP_COMMAND),
    "restart": ((), False, ClientType.RESTART_PANE),
    "kill": (("confirm",), True, ClientType.KILL_PANE),
}
PANE_AUDIT = {"takeover_request": "pane_takeover_request", "takeover_confirm": "pane_takeover_confirm",
              "handoff": "pane_handoff", "restart": "pane_restart", "kill": "pane_kill"}


def pane_refusal(reason: str, backend_reason: str | None = None) -> web.Response:
    body = {"ok": False, "reason": reason, "detail": DETAIL.get(reason, DETAIL["bad_input"])}
    if backend_reason is not None:
        body["backend_reason"] = backend_reason
    return web.json_response(body, status=STATUS.get(reason, 400), headers=NO_STORE)


def pane_routes(panes: Callable[[], object | None]) -> Callable[[web.Application], None]:
    """``POST /api/board/panes/{pane_id}/{action}`` over ``panes()`` (the Hub's ``PaneHub``, None: 503)."""

    def audit(request: web.Request, event: str, detail: dict) -> None:
        services = request.app[SERVICES]
        services.audit.record(event, "owner", request.remote, detail, now=float(services.clock()))

    def action_handler(name: str):
        accepted, host_only, kind = PANE_ACTIONS[name]

        @api
        async def handle(request: web.Request) -> web.Response:
            hub = panes()
            if hub is None or hub.stopped:
                return pane_refusal("board_unavailable")
            pane_id = request.match_info["pane_id"]
            parsed = pane_relay.parse_pane_id(pane_id)
            if parsed is None or await hub.entry(parsed[0]) is None:
                return pane_refusal("unknown_terminal")
            project_id, pane = parsed
            try:
                body = await _body(request, accepted)
            except _BadInput:
                return pane_refusal("bad_input")
            if host_only and pane != "host":
                return pane_refusal("wrong_pane")
            if name in ("stop", "kill") and body.get("confirm") is not True:
                return pane_refusal("confirm_required")
            command_id = body.get("command_id")
            if name == "stop" and not (isinstance(command_id, str) and 0 < len(command_id) <= 64):
                return pane_refusal("bad_input")
            relay = hub.relay(project_id)
            if relay is None:
                return pane_refusal("board_unavailable")
            if pane in relay.busy:
                return pane_refusal("terminal_busy")
            fields: dict = {}
            if name in ("restart", "kill"):
                fields["pane"] = pane_relay.BACKEND_PANES[pane]
            if name == "stop":
                fields["command_id"] = command_id
            relay.busy.add(pane)
            relay.check_idle()  # an action in flight keeps the relay open
            relay._changed()
            try:
                try:
                    header = await relay.action(pane, kind, fields, address=request.remote,
                                                command_id=command_id if name == "stop" else None)
                except pane_relay.RelayError as exc:
                    if name != "stop":
                        audit_row(request, name, relay, pane, ok=False, reason=exc.reason)
                    else:
                        await request.app[SERVICES].blocking(
                            audit, request, "pane_stop", relay.detail(pane, command_id=command_id,
                                                                      outcome=exc.reason))
                    return pane_refusal(exc.reason)
            finally:
                relay.busy.discard(pane)
                relay._changed()
                relay.check_idle()
            if header.get("ok") is not True:
                reason = header.get("reason")
                reason = reason if isinstance(reason, str) else "unknown"
                if name == "stop":
                    await request.app[SERVICES].blocking(
                        audit, request, "pane_stop", relay.detail(pane, command_id=command_id,
                                                                  outcome=f"refused:{reason}"))
                else:
                    await audit_row_async(request, name, relay, pane, ok=False, reason=reason)
                return pane_refusal("backend_refused", reason)
            if name == "stop":
                stop = header.get("stop") if isinstance(header.get("stop"), dict) else {"status": "stopping"}
                return reply({"command_id": command_id, "stop": stop}, 202)
            shell = header.get("shell")
            if isinstance(shell, dict) and isinstance(relay.snapshot, dict):  # the answer before the next push
                host = (relay.snapshot.get("panes") or {}).get("host_shell")
                if isinstance(host, dict):
                    host["shell"] = shell
                    if shell.get("input_owner") in ("manager", "user"):
                        host["input_owner"] = shell["input_owner"]
                    relay._changed()
            view = relay.view(pane)
            more = {"input_owner": view["input_owner"]} if name in ("takeover_request", "takeover_confirm",
                                                                    "handoff") else {}
            await audit_row_async(request, name, relay, pane, ok=True, **more)
            return reply({"pane": view})
        return handle

    def audit_row(request, name, relay, pane, **more) -> None:
        asyncio.ensure_future(audit_row_async(request, name, relay, pane, **more))

    async def audit_row_async(request, name, relay, pane, *, ok: bool, reason: str | None = None, **more) -> None:
        detail = relay.detail(pane, ok=ok, **({"reason": reason} if reason is not None else {}), **more)
        if name in ("takeover_request", "takeover_confirm", "handoff") and "input_owner" not in detail:
            detail["input_owner"] = relay.view(pane)["input_owner"]
        try:
            await request.app[SERVICES].blocking(audit, request, PANE_AUDIT[name], detail)
        except Exception:  # an audit problem never changes the answer
            pass

    def register(app: web.Application) -> None:
        for name in PANE_ACTIONS:
            app.router.add_post(f"/api/board/panes/{{pane_id}}/{name}", action_handler(name))
    return register


def routes(board: Callable[[], object | None], panes: Callable[[], object | None] = lambda: None
           ) -> Callable[[web.Application], None]:
    """The SPEC s4 session API over ``board()`` (the Hub's ``BoardSet``, None without one: 503); s5 adds the project
    panes (``projects`` of ``GET /api/board`` and the pane actions) over ``panes()`` (the Hub's ``PaneHub``)."""

    def handler(fields: tuple[str, ...] | None, action):
        @api
        async def handle(request: web.Request) -> web.Response:
            current = board()
            if current is None:
                return refusal("board_unavailable")
            try:
                body = await _body(request, fields) if fields is not None else {}
                return await action(current, request, body)
            except _BadInput:
                return refusal("bad_input")
            except BoardError as exc:
                return refusal(exc.reason)
        return handle

    async def get_board(current, request, body):
        options = current.options()
        hub = panes()
        return reply({"board": current.board_view(),
                      "options": {**options, "agent_range": list(registry.AGENT_RANGE),
                                  "shell_range": list(registry.SHELL_RANGE)},
                      "terminals": current.views(),
                      "projects": await hub.projects() if hub is not None else []})

    async def post_open(current, request, body):
        visible = body.get("visible", [])
        if (not isinstance(visible, list) or len(visible) > VISIBLE_MAX
                or not all(isinstance(item, str) and len(item) <= 64 for item in visible)):
            raise _BadInput
        return reply({"terminals": await current.open(visible, actor="owner", address=request.remote)})

    async def post_agents(current, request, body):
        return reply({"terminal": await current.create_agent(actor="owner", address=request.remote)}, 201)

    async def post_shells(current, request, body):
        return reply({"terminal": await current.create_shell(actor="owner", address=request.remote)}, 201)

    def terminal_action(name: str):
        async def act(current, request, body):
            terminal_id = request.match_info["terminal_id"]
            if current.find(terminal_id) is None:
                current.get(terminal_id)  # board_unavailable / registry_invalid / unknown_terminal, in that order
            address = request.remote
            if name == "start":
                return reply({"terminal": await current.start(terminal_id, actor="owner", address=address)})
            if name == "rename":
                return reply({"terminal": await current.rename(terminal_id, body.get("name"), actor="owner",
                                                               address=address)})
            kind = current.find(terminal_id).kind
            if (name in ("stop", "delete") and kind != "agent") or (name == "close" and kind != "shell"):
                raise BoardError("wrong_kind")
            _confirmed(body)
            if name == "stop":
                return reply({"terminal": await current.stop_terminal(terminal_id, actor="owner", address=address)})
            if name == "restart":
                return reply({"terminal": await current.restart(terminal_id, actor="owner", address=address)})
            if name == "close":
                await current.close(terminal_id, actor="owner", address=address)
            else:
                await current.delete(terminal_id, actor="owner", address=address)
            return reply({})
        return act

    async def post_options(current, request, body):
        return reply({"options": await current.set_options(agent_limit=body.get("agent_limit"),
                                                           shell_limit=body.get("shell_limit"), actor="owner",
                                                           address=request.remote)})

    fields = {"start": (), "stop": ("confirm",), "restart": ("confirm",), "close": ("confirm",),
              "delete": ("confirm",), "rename": ("name",)}

    def register(app: web.Application) -> None:
        app.router.add_get("/api/board", handler(None, get_board))
        app.router.add_post("/api/board/open", handler(("visible",), post_open))
        app.router.add_post("/api/board/agents", handler((), post_agents))
        app.router.add_post("/api/board/shells", handler((), post_shells))
        for name, accepted in fields.items():
            app.router.add_post(f"/api/board/terminals/{{terminal_id}}/{name}", handler(accepted, terminal_action(name)))
        app.router.add_post("/api/board/options", handler(("agent_limit", "shell_limit"), post_options))
        pane_routes(panes)(app)
    return register
