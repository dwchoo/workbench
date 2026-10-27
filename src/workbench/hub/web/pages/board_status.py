"""Status board home page and the pieces the project pages share (TI-12; SPEC "웹 route와 화면").

``GET /`` lists the registered projects with the Hub's own labels (:mod:`workbench.hub.presentation`).
A row whose observation failed reads ``연결 없음/불명``, never ``꺼짐``. The page refreshes itself with a
``<meta http-equiv="refresh">`` (no JavaScript); the badge script is the same one every page loads.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import quote

from aiohttp import web

from ... import presentation
from .. import render
from ..middleware import SERVICES, session_of
from . import login

REFRESH_SECONDS = 10
STYLESHEETS = ("/static/app.css",)
SCRIPTS = ("/static/badge.js",)  # header badge + tab title (D-24); no inline script
UNREADABLE_TEXT = "프로젝트 상태를 읽지 못했습니다. 잠시 뒤 다시 시도하세요"
REGISTRY_TEXT = "등록된 프로젝트 목록을 읽지 못했습니다"
REVIEW_MODE_LABELS = {"auto": "자동", "manual": "수동"}
UNKNOWN_POWER = "unknown"


@dataclass(slots=True)
class StatusPort:
    """What the status and project pages use: the Hub list (``Lister.list``) and the inbox."""

    rows: Callable[..., Awaitable[dict]]
    inbox: object
    one: Callable[..., Awaitable[dict]] | None = None  # ``one(project_id)``: one project's row, observed afresh


PORT: web.AppKey = web.AppKey("status_port", StatusPort)


def project_url(project_id: str, suffix: str = "") -> str:
    return f"/projects/{quote(project_id, safe='')}{suffix}"


def frame(request: web.Request, title: str, body: object, *, status: int = 200,
          refresh: int | None = None) -> web.Response:
    services, session = request.app[SERVICES], session_of(request)
    return render.page(title, body, status=status, insecure_warning=services.insecure_warning, csrf=session.csrf,
                       stylesheets=STYLESHEETS, scripts=SCRIPTS, refresh=refresh, nav=render.nav_for(request))


def normalized(row: dict) -> dict:
    """The row with a power value this page can label; anything else is the unknown (연결 없음/불명) state."""
    if row.get("power") in presentation.POWER_LABELS:
        return row
    return {**row, "power": UNKNOWN_POWER, "power_detail": None}


def review_mode_text(mode: object) -> str:
    return REVIEW_MODE_LABELS.get(mode, presentation.EMPTY)


async def fetch(request: web.Request, *, fresh: bool = False,
                project_id: str | None = None) -> tuple[list[dict], str | None]:
    """``(rows, problem)``: the Hub list rows and a fixed problem text when it could not be read.

    ``fresh`` with a ``project_id`` observes that one project afresh (the others stay cached); ``fresh`` alone
    observes every project."""
    port = request.app[PORT]
    try:
        if fresh and project_id is not None and port.one is not None:
            listing = await port.one(project_id)
        else:
            listing = await port.rows(fresh)
    except Exception:  # noqa: BLE001 - a broken list is a page problem, never a 500 or a "꺼짐" row
        return [], UNREADABLE_TEXT
    if not isinstance(listing, dict):
        return [], UNREADABLE_TEXT
    registry = listing.get("registry")
    if isinstance(registry, dict) and registry.get("state") != "ok":
        return [], REGISTRY_TEXT
    return [normalized(row) for row in listing.get("rows") or () if isinstance(row, dict)], None


async def get_board(request: web.Request) -> web.Response:
    rows, problem = await fetch(request)
    now = float(request.app[SERVICES].clock())
    head = render.tag("tr", render.html_join(
        render.tag("th", title) for title in (*presentation.COLUMNS, "검토 방식")))
    table_rows = [head]
    for row in rows:
        cells = presentation.row_cells(row, now)
        project_id = row.get("project_id")
        if isinstance(project_id, str) and project_id:
            cells[0] = render.tag("a", cells[0], href=project_url(project_id))
        cells.append(review_mode_text(row.get("review_mode")))
        table_rows.append(render.tag("tr", render.html_join(render.tag("td", cell) for cell in cells)))
    body = [render.tag("h1", "프로젝트 상태")]
    if problem is not None:
        body.append(render.tag("p", problem, class_="error", role="alert"))
    elif rows:
        body.append(render.data_table(render.html_join(table_rows), "board"))
    else:
        body.append(render.tag("p", "등록된 프로젝트가 없습니다", class_="empty"))
    body.append(render.tag("p", render.tag("a", "요청 목록", href="/requests")))
    return frame(request, "상태", render.html_join(body), refresh=REFRESH_SECONDS)


def register(app: web.Application, port: StatusPort) -> None:
    app[PORT] = port
    app[login.HOME] = get_board
