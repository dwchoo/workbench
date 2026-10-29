"""The Board page (TI-18; web-board-ux SPEC s4 "트랙 D", WBX-D2 workspace).

``GET /board`` is server-rendered HTML but only a frame: the management panel (``aside.board-panel``: agent and shell
lists, create buttons, layout reset, limit form), the empty grid workspace (``section.board-workspace``) and the
confirm ``<dialog>``. No terminal, name or state is rendered here: the page script (``/static/board.js`` with the
pure modules ``board-layout.js``, ``board-panel.js`` and the protocol v2 state machine ``board-state.js``; module
scripts, no inline script) takes the list from ``GET /api/board``, asks ``POST /api/board/open`` for the visible
widgets and opens one ``/ws/board/{terminal id}`` per visible widget. Names reach the DOM through ``textContent`` only.
The CSRF token and this response's CSP style nonce (:func:`..middleware.style_nonce`, set on xterm's ``<style>``
elements by ``/static/style-nonce.js``) reach the script in ``<meta>`` elements. Rendering the page starts nothing.
"""
from __future__ import annotations

from typing import Callable

from aiohttp import web

from .. import render
from ..middleware import SERVICES, inline_styles, session_of, style_nonce

STYLESHEETS = ("/static/app.css", "/static/vendor/xterm/xterm.css", "/static/board.css")
SCRIPTS = ("/static/badge.js", "/static/vendor/xterm/xterm.js", "/static/vendor/xterm/addon-fit.js")
MODULE_SCRIPTS = ("/static/board.js",)
UNAVAILABLE_TEXT = "Board agent 세트를 사용할 수 없습니다 (hub.toml을 확인하세요)"
STATE_LABELS = {"running": "실행 중", "starting": "시작 중", "not_started": "시작 전", "stopped": "중지됨"}


def channel_label(info: object) -> str:
    """The text for one terminal view; the reference of ``board-state.js`` ``channelLabel`` (tests/hub/js/channel_labels.json
    is checked against both)."""
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
                       nav=render.nav_for(request), body_class="page-board" if board else None)


def unavailable(request: web.Request, text: str = UNAVAILABLE_TEXT) -> web.Response:
    return frame(request, "Board", render.html_join([
        render.tag("h1", "Board"), render.tag("p", text, class_="error", role="alert")]), status=503)


def panel_markup() -> render.Markup:
    """The management panel's frame: the lists, the create buttons, the layout reset and the limit form. The rows are
    drawn by the script from ``GET /api/board`` (names are text there)."""
    def section(heading: str, list_id: str, button_id: str, button: str, note_id: str) -> render.Markup:
        return render.tag("section", render.html_join([
            render.tag("h2", heading), render.tag("ul", "", id=list_id, class_="panel-list"),
            render.tag("div", render.html_join([
                render.tag("button", button, type="button", id=button_id, class_="btn btn-sm"),
                render.tag("span", "", id=note_id, class_="panel-note")]), class_="panel-create")]))
    limits = render.tag("form", render.html_join([
        render.tag("label", render.html_join(["Board agent 한도 ", render.void(
            "input", type="number", id="board-agent-limit", name="agent_limit", min="1", max="8", inputmode="numeric")]),
            class_="check"),
        render.tag("label", render.html_join(["shell 한도 ", render.void(
            "input", type="number", id="board-shell-limit", name="shell_limit", min="1", max="32", inputmode="numeric")]),
            class_="check"),
        render.tag("button", "저장", type="submit", class_="btn btn-sm"),
        render.tag("p", "", id="board-options-error", class_="error", role="alert", hidden=True)]),
        id="board-options", class_="panel-limits", novalidate=True)
    layout = render.tag("section", render.html_join([
        render.tag("h2", "배치"),
        render.tag("button", "배치 초기화", type="button", id="board-reset-layout", class_="btn btn-sm"),
        render.tag("span", "이 브라우저만", class_="panel-note")]))
    return render.tag("aside", render.html_join([
        section("Board agent", "board-agents", "board-new-agent", "새 Board agent", "board-agent-note"),
        section("Shell", "board-shells", "board-new-shell", "새 shell", "board-shell-note"),
        layout, render.tag("section", render.html_join([render.tag("h2", "한도"), limits]))]),
        id="board-panel", class_="board-panel", aria_label="Board 목록")


def confirm_markup() -> render.Markup:
    return render.tag("dialog", render.html_join([
        render.tag("p", "", id="board-confirm-text"),
        render.tag("div", render.html_join([
            render.tag("button", "취소", type="button", id="board-confirm-cancel", class_="btn"),
            render.tag("button", "확인", type="button", id="board-confirm-ok", class_="btn btn-danger")]),
            class_="actions")]), id="board-confirm", class_="board-confirm", aria_labelledby="board-confirm-text")


def board_page(request: web.Request, board) -> web.Response:
    if board is None:
        return unavailable(request)
    state = board.board_view()
    if state.get("state") == "registry_invalid":
        return unavailable(request, f"Board 목록 파일을 읽을 수 없습니다: {state.get('message') or ''}")
    bar = render.tag("div", render.html_join([
        render.tag("h1", "Board"),
        render.tag("button", "목록", type="button", id="board-panel-toggle", class_="btn btn-sm",
                   aria_controls="board-panel", aria_expanded="true"),
        render.tag("p", "", id="board-notice", class_="notice-line", role="status", hidden=True)]), class_="board-bar")
    nojs = render.tag("noscript", render.tag("p", "이 화면은 JavaScript가 필요합니다", class_="error"))
    workspace = render.tag("section", "", id="board-workspace", class_="board-workspace", aria_label="Board 작업 공간")
    shell = render.tag("div", render.html_join([panel_markup(), workspace]), class_="board-shell", id="board-shell")
    return frame(request, "Board", render.html_join([bar, nojs, shell, confirm_markup()]), board=True)


def make_register(board: Callable[[], object | None]) -> Callable[[web.Application], None]:
    """``GET /board`` over ``board()`` (the Hub's ``BoardSet`` or None). (``GET /board/restart`` is gone: restarts
    are ``POST /api/board/terminals/{id}/restart``.)"""
    @inline_styles  # xterm.js inserts <style> elements: this page's CSP alone allows nonced ones (and style attributes)
    async def get_board(request: web.Request) -> web.Response:
        return board_page(request, board())

    def register(app: web.Application) -> None:
        app.router.add_get("/board", get_board)
    return register
