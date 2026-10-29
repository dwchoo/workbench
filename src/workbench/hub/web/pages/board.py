"""The Board page (TI-18; web-board-ux SPEC s4 "트랙 D" until WBX-D2's workspace).

``GET /board`` is server-rendered HTML: a tab for the first Board agent (``agent-1`` after the migration) and one
for the first shell (each an empty ``div`` the page script turns into an xterm.js terminal), their states, the
connection state and a restart button per tab. The page script (``/static/board.js`` with the protocol v2 state
machine ``/static/board-state.js``; module scripts, no inline script) asks ``POST /api/board/open`` for the terminals
(the first shell of this Hub run is made then, and a not yet started agent starts), then opens one
``/ws/board/{terminal id}`` per tab. The CSRF token and this response's CSP style nonce (:func:`..middleware.style_nonce`,
set on xterm's ``<style>`` elements by ``/static/style-nonce.js``) reach it in ``<meta>`` elements. Rendering the page
starts nothing.
"""
from __future__ import annotations

from typing import Callable

from aiohttp import web

from .. import render
from ..middleware import SERVICES, inline_styles, session_of, style_nonce

CHANNELS = (("agent", "Board agent"), ("shell", "Shell"))
CHANNEL_NAMES = tuple(name for name, _ in CHANNELS)
STYLESHEETS = ("/static/app.css", "/static/vendor/xterm/xterm.css", "/static/board.css")
SCRIPTS = ("/static/badge.js", "/static/vendor/xterm/xterm.js", "/static/vendor/xterm/addon-fit.js")
MODULE_SCRIPTS = ("/static/board.js",)
UNAVAILABLE_TEXT = "Board agent 세트를 사용할 수 없습니다 (hub.toml을 확인하세요)"
STATE_LABELS = {"running": "실행 중", "starting": "시작 중", "not_started": "시작 전", "stopped": "중지됨"}


def channel_label(info: object) -> str:
    """The text for one channel's ``status`` view; the same mapping ``board-state.js`` ``channelLabel`` uses."""
    if not isinstance(info, dict) or not isinstance(info.get("state"), str):
        return "알 수 없음"
    state, detail = info["state"], info.get("detail")
    suffix = f": {detail}" if detail else ""
    if state == "exited":
        status = info.get("exit_status")
        return "종료됨" if status is None else f"종료됨 (종료 코드 {status})"
    if state == "failed":
        return f"실패{suffix}"
    if state == "isolation_failed":
        return f"격리 실패{suffix}"
    return STATE_LABELS.get(state, state)


def frame(request: web.Request, title: str, body: object, *, status: int = 200, board: bool = False) -> web.Response:
    services, session = request.app[SERVICES], session_of(request)
    return render.page(title, body, status=status, insecure_warning=services.insecure_warning, csrf=session.csrf,
                       stylesheets=STYLESHEETS if board else STYLESHEETS[:1],
                       scripts=SCRIPTS if board else SCRIPTS[:1],
                       module_scripts=MODULE_SCRIPTS if board else (),
                       metas={"csrf-token": session.csrf, "style-nonce": style_nonce(request)} if board else None,
                       nav=render.nav_for(request))


def tab_button(name: str, label: str, first: bool) -> render.Markup:
    return render.tag("button", label, type="button", role="tab", id=f"tab-{name}", data_channel=name,
                      aria_controls=f"panel-{name}", aria_selected="true" if first else "false",
                      class_="tab active" if first else "tab")


def panel(name: str, info: dict | None, first: bool) -> render.Markup:
    """One tab's panel; ``data-terminal`` is the terminal's id when it exists yet (the first shell of a run is made by
    the page's ``open`` call)."""
    view = info or {"state": "not_started"}
    head = render.tag("div", render.html_join([
        render.tag("span", view.get("name") or "", id=f"name-{name}", class_="channel-name", data_channel=name),
        render.tag("span", channel_label(view), id=f"state-{name}", class_="channel-state", data_channel=name),
        render.tag("button", "재시작", type="button", class_="btn btn-sm", id=f"restart-{name}", data_channel=name)]),
        class_="panel-head")
    attributes = {"data_terminal": info["id"]} if info and info.get("id") else {}
    return render.tag("section", render.html_join([head, render.tag("div", "", id=f"term-{name}", class_="term")]),
                      id=f"panel-{name}", class_="panel active" if first else "panel", data_channel=name,
                      role="tabpanel", **attributes)


def first_views(board) -> dict[str, dict | None]:
    """The first agent and the first shell (registry order), the two terminals this page shows until WBX-D2."""
    found: dict[str, dict | None] = {"agent": None, "shell": None}
    for view in board.views():
        if found.get(view.get("kind"), 0) is None:
            found[view["kind"]] = view
    return found


def unavailable(request: web.Request, text: str = UNAVAILABLE_TEXT) -> web.Response:
    return frame(request, "Board", render.html_join([
        render.tag("h1", "Board"), render.tag("p", text, class_="error", role="alert")]), status=503)


def board_page(request: web.Request, board) -> web.Response:
    if board is None:
        return unavailable(request)
    state = board.board_view()
    if state.get("state") == "registry_invalid":
        return unavailable(request, f"Board 목록 파일을 읽을 수 없습니다: {state.get('message') or ''}")
    views = first_views(board)
    bar = render.tag("div", render.html_join([
        render.tag("span", "연결 중", id="board-connection", class_="connection", role="status"),
        render.tag("button", "다시 연결", type="button", id="board-reconnect", class_="btn", hidden=True),
        render.tag("a", "다시 로그인", href="/login", id="board-login", class_="btn", hidden=True)]),
        class_="board-bar")
    tabs = render.tag("div", render.html_join(
        tab_button(name, label, index == 0) for index, (name, label) in enumerate(CHANNELS)),
        role="tablist", class_="tabs")
    panels = render.html_join(panel(name, views[name], index == 0) for index, (name, _) in enumerate(CHANNELS))
    notice = render.tag("p", "", id="board-notice", class_="notice-line", role="status", hidden=True)
    return frame(request, "Board", render.html_join([render.tag("h1", "Board"), bar, notice, tabs,
                                                     render.tag("div", panels, class_="panels")]), board=True)


def make_register(board: Callable[[], object | None]) -> Callable[[web.Application], None]:
    """``GET /board`` over ``board()`` (the Hub's ``BoardSet`` or None). (``GET /board/restart`` is gone: restarts
    are ``POST /api/board/terminals/{id}/restart``.)"""
    @inline_styles  # xterm.js inserts <style> elements: this page's CSP alone allows nonced ones (and style attributes)
    async def get_board(request: web.Request) -> web.Response:
        return board_page(request, board())

    def register(app: web.Application) -> None:
        app.router.add_get("/board", get_board)
    return register
