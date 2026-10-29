"""The Board session API (web-board-ux SPEC s4 "Session API: 화면과 panel 동작", WBX-D44..D56).

Session routes only (``@api``): no session -> 401, a state change without the session's ``X-CSRF-Token`` -> 403 (the
middleware); a Bearer API key is never read (AC-08/38). Bodies are one JSON object of at most 4 KiB with only the
route's fields (else 400 ``bad_input``); replies are JSON, ``Cache-Control: no-store``: ``{"ok": true, ...}`` or
``{"ok": false, "reason": <code>, "detail": <fixed sentence>}``.
"""
from __future__ import annotations

import json
from typing import Callable

from aiohttp import web

from ...board import registry
from ...board.set import VISIBLE_MAX, BoardError
from ..middleware import api

BODY_MAX = 4096
STATUS = {
    "bad_input": 400, "name_invalid": 400, "confirm_required": 400, "limit_out_of_range": 400, "wrong_kind": 400,
    "bad_state": 400, "unknown_terminal": 404, "name_taken": 409, "limit_reached": 409, "limit_below_count": 409,
    "terminal_busy": 409, "registry_write_failed": 500, "delete_failed": 500, "board_unavailable": 503,
    "registry_invalid": 503,
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


def routes(board: Callable[[], object | None]) -> Callable[[web.Application], None]:
    """The SPEC s4 session API over ``board()`` (the Hub's ``BoardSet``, None without one: 503)."""

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
        return reply({"board": current.board_view(),
                      "options": {**options, "agent_range": list(registry.AGENT_RANGE),
                                  "shell_range": list(registry.SHELL_RANGE)},
                      "terminals": current.views()})

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
    return register
