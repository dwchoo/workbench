"""The Board page and its restart confirm page (TI-18; SPEC "웹 route와 화면", "WebSocket terminal 프로토콜").

``GET /board`` is server-rendered HTML: a tab for the Board agent OMP and one for the shell (each an empty
``div`` the page script turns into an xterm.js terminal), the channel states, the connection state, the takeover
banner and the restart links. The state machine and the DOM glue are ``/static/board-state.js`` and
``/static/board.js`` (module scripts, no inline script); the CSRF token and this response's CSP style nonce
(:func:`..middleware.style_nonce`, set on xterm's ``<style>`` elements by ``/static/style-nonce.js``) reach them in
``<meta>`` elements. The page never starts the set: only a WebSocket attach does.

``GET /board/restart?channel=`` is the confirm page (nothing changes); its form posts to ``POST /board/restart``
(TI-16) and cancel is a plain link back.
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
BANNER_PREFIX = "다른 곳에서 연결됨 — "
UNAVAILABLE_TEXT = "Board agent 세트를 사용할 수 없습니다 (hub.toml을 확인하세요)"
STATE_LABELS = {"running": "실행 중", "starting": "시작 중", "not_started": "시작 전"}
RESTART_EFFECT = {
    "agent": "Board agent OMP를 끝내고 이전 세션을 이어서(--continue) 다시 띄웁니다. 진행 중인 응답은 끊깁니다.",
    "shell": "Shell을 끝내고 새로 띄웁니다. 그 shell에서 실행 중인 명령은 종료됩니다.",
}


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


def panel(name: str, info: object, first: bool) -> render.Markup:
    head = render.tag("div", render.html_join([
        render.tag("span", channel_label(info), id=f"state-{name}", class_="channel-state", data_channel=name),
        render.tag("a", "재시작", href=f"/board/restart?channel={name}", class_="btn btn-sm", id=f"restart-{name}")]),
        class_="panel-head")
    return render.tag("section", render.html_join([head, render.tag("div", "", id=f"term-{name}", class_="term")]),
                      id=f"panel-{name}", class_="panel active" if first else "panel", data_channel=name,
                      role="tabpanel")


def board_page(request: web.Request, board) -> web.Response:
    if board is None:
        return frame(request, "Board", render.html_join([
            render.tag("h1", "Board"), render.tag("p", UNAVAILABLE_TEXT, class_="error", role="alert")]),
            status=503)
    status = board.status() or {}
    bar = render.tag("div", render.html_join([
        render.tag("span", "연결 중", id="board-connection", class_="connection", role="status"),
        render.tag("button", "다시 연결", type="button", id="board-reconnect", class_="btn", hidden=True),
        render.tag("a", "다시 로그인", href="/login", id="board-login", class_="btn", hidden=True)]),
        class_="board-bar")
    banner = render.tag("div", render.html_join([
        render.tag("span", BANNER_PREFIX),
        render.tag("button", "다시 가져오기", type="button", id="board-retake", class_="btn btn-sm")]),
        id="board-banner", class_="banner taken", role="alert", hidden=True)
    tabs = render.tag("div", render.html_join(
        tab_button(name, label, index == 0) for index, (name, label) in enumerate(CHANNELS)),
        role="tablist", class_="tabs")
    panels = render.html_join(panel(name, status.get(name) or {"state": "not_started"}, index == 0)
                              for index, (name, _) in enumerate(CHANNELS))
    notice = render.tag("p", "", id="board-notice", class_="notice-line", role="status", hidden=True)
    return frame(request, "Board", render.html_join([render.tag("h1", "Board"), bar, banner, notice, tabs,
                                                     render.tag("div", panels, class_="panels")]), board=True)


def confirm_page(request: web.Request, channel: str) -> web.Response:
    label = dict(CHANNELS)[channel]
    form = render.tag("form", render.html_join([
        render.csrf_field(session_of(request).csrf), render.hidden("channel", channel),
        render.tag("button", "확인", type="submit", class_="btn btn-primary")]), method="post", action="/board/restart", class_="inline")
    return frame(request, f"{label} 재시작 확인", render.html_join([
        render.tag("h1", f"{label} 재시작 확인"), render.tag("p", RESTART_EFFECT[channel]), form,
        render.tag("p", render.tag("a", "취소", href="/board"))]))


def make_register(board: Callable[[], object | None]) -> Callable[[web.Application], None]:
    """``GET /board`` and ``GET /board/restart`` over ``board()`` (the Hub's ``BoardSet`` or None)."""
    @inline_styles  # xterm.js inserts <style> elements: this page's CSP alone allows nonced ones (and style attributes)
    async def get_board(request: web.Request) -> web.Response:
        return board_page(request, board())

    async def get_restart(request: web.Request) -> web.Response:
        channel = request.query.get("channel")
        if channel not in CHANNEL_NAMES:
            return web.Response(status=400, text="bad_channel")
        if board() is None:
            return frame(request, "Board", render.html_join([
                render.tag("h1", "Board"), render.tag("p", UNAVAILABLE_TEXT, class_="error", role="alert")]),
                status=503)
        return confirm_page(request, channel)

    def register(app: web.Application) -> None:
        app.router.add_get("/board", get_board)
        app.router.add_get("/board/restart", get_restart)
    return register
