"""The dashboard ``/dashboard`` (WBX-A6; ``/`` redirects there) and its session JSON ``GET /api/dashboard`` (WBX-C2; web-board-ux BRIEF WBX-D13/D18).

One model (:func:`build`) feeds both: the page renders it server-side (complete without JavaScript) and
``/api/dashboard`` serves it as JSON for ``static/dashboard.js``, which redraws the numbers and tables from the same
fields every 10 s. The JSON therefore holds nothing the page does not show, in the same display words.
"""
from __future__ import annotations

import time
from urllib.parse import quote

from aiohttp import web

from ... import presentation
from ...inbox.limits import InboxError, LIST_LIMIT_MAX
from .. import render
from ..middleware import SERVICES, api, principal_of
from . import board_status, login, requests as request_pages

SCRIPTS = (*board_status.SCRIPTS, "/static/dashboard.js")

# The chip kind of a power value (app.css ``.p-<kind>``): a transition is ``busy``; a mismatch reads like an unknown.
POWER_KINDS = {"on": "on", "off": "off", "starting": "busy", "closing": "busy", "unknown": "unknown",
               "mismatch": "unknown"}
RECENT_LIMIT = 10
ATTENTION_LIMIT = 10
ATTENTION_REASONS = {"question": "추가 정보 필요", "decision": "Task 결정 대기"}
PROJECT_COUNT_KEYS = ("total", "running", "closed", "unknown", "other")
REQUEST_CARDS = {"NEW": "new", "REVIEWING": "reviewing", "NEEDS_INFO": "needs_info", "RUNNING": "running"}


def project_counts(rows: list[dict] | None) -> dict:
    """``{total, running, closed, unknown, other}`` of the Hub rows by power; all ``None`` when the list is unreadable.
    ``running`` is power ``on``, ``closed`` is ``off``, ``unknown`` is ``연결 없음/불명``; ``other`` is a transition or a
    mismatch (starting, closing, mismatch)."""
    if rows is None:
        return dict.fromkeys(PROJECT_COUNT_KEYS)
    powers = [row.get("power") for row in rows]
    return {"total": len(rows), "running": powers.count("on"), "closed": powers.count("off"),
            "unknown": powers.count(board_status.UNKNOWN_POWER),
            "other": sum(power not in ("on", "off", board_status.UNKNOWN_POWER) for power in powers)}


def project_item(row: dict, now: float) -> dict:
    """One project row as display text plus its two links: the Hub list columns (HB-D09), the review mode and the
    pending-request text."""
    name, path, power, task, command, wait, requests, activity = presentation.row_cells(row, now)
    project_id = row.get("project_id")
    linked = isinstance(project_id, str) and project_id
    return {"project_id": project_id, "url": board_status.project_url(project_id) if linked else None,
            "requests_url": f"/requests?project={quote(project_id, safe='')}" if linked else None,
            "name": name, "path": path, "power": power, "power_kind": POWER_KINDS.get(row.get("power"), "unknown"),
            "task": task, "command": command, "wait": wait, "requests": requests, "last_activity": activity,
            "review_mode": board_status.review_mode_text(row.get("review_mode"))}


async def open_requests(request: web.Request) -> tuple[list[dict], bool]:
    """``(requests, partial)``: every request that is not CLOSED in all projects the session may see (pages of the
    service's own limit); ``partial`` when some project's inbox could not be read."""
    inbox, principal = request.app[board_status.PORT].inbox, principal_of(request)
    found: list[dict] = []
    partial, cursor = False, None
    while True:
        page = await request.app[SERVICES].blocking(lambda: inbox.list_requests(
            principal, include_closed=False, limit=LIST_LIMIT_MAX, cursor=cursor))
        found += page["requests"]
        partial = partial or bool(page.get("unavailable"))
        cursor = page["next_cursor"]
        if cursor is None:
            return found, partial


def request_counts(items: list[dict] | None) -> dict:
    """``{new, reviewing, needs_info, running}`` plus the owner-waiting split: a NEEDS_INFO request is the owner's to
    answer (WBX-D18); one that still links a Task is the retry-limit decision of P2-AC-27 (the manager asked while the
    Task was RUNNING and the link stays), the others are plain questions."""
    if items is None:
        return {"requests": dict.fromkeys(REQUEST_CARDS.values()),
                "attention": {"total": None, "questions": None, "decisions": None}}
    states = [item["state"] for item in items]
    cards = {name: states.count(state) for state, name in REQUEST_CARDS.items()}
    decisions = sum(item["state"] == "NEEDS_INFO" and item.get("task_id") is not None for item in items)
    questions = cards["needs_info"] - decisions
    return {"requests": cards, "attention": {"total": questions + decisions, "questions": questions,
                                              "decisions": decisions}}


def board_card(request: web.Request) -> dict:
    """The nav's Board state and (web-board-ux SPEC s4) how many agents run and how many shells there are."""
    state = render.nav_for(request).board_state
    try:
        counts = request.app[SERVICES].board_counts()
    except Exception:  # noqa: BLE001 - the card shows the state alone
        counts = None
    if isinstance(counts, dict):
        counts = {"agents_running": int(counts.get("agents_running") or 0), "shells": int(counts.get("shells") or 0)}
        summary = f"agent {counts['agents_running']} 실행 중 · shell {counts['shells']}"
    else:
        counts = summary = None
    return {"state": state, "label": render.BOARD_DOT_TITLES[state], "url": "/board", "counts": counts,
            "summary": summary}


def state_text(item: dict) -> str:
    label = request_pages.state_label(item["state"])
    if item["state"] == "CLOSED" and item.get("outcome"):
        label = f"{label}({request_pages.OUTCOME_LABELS.get(item['outcome'], item['outcome'])})"
    return label


def project_name(names: dict, project_id: str) -> str:
    return names.get(project_id, project_id)


def recent_item(item: dict, names: dict, now: float) -> dict:
    return {"request_id": item["request_id"], "url": request_pages.request_url(item["request_id"]),
            "title": item["title"], "project": project_name(names, item["project_id"]), "state": item["state"],
            "state_label": state_text(item), "state_kind": request_pages.STATE_KINDS.get(item["state"], "unknown"),
            "origin": item["origin"]["label"], "updated": presentation.relative_time(item["updated_at"], now),
            "attention": item["state"] == "NEEDS_INFO"}


def attention_item(item: dict, names: dict, now: float) -> dict:
    reason = "decision" if item.get("task_id") is not None else "question"
    return {"request_id": item["request_id"], "url": request_pages.request_url(item["request_id"]),
            "title": item["title"], "project": project_name(names, item["project_id"]),
            "reason": ATTENTION_REASONS[reason], "updated": presentation.relative_time(item["updated_at"], now)}


async def recent_requests(request: web.Request) -> list[dict]:
    inbox, principal = request.app[board_status.PORT].inbox, principal_of(request)
    page = await request.app[SERVICES].blocking(lambda: inbox.list_requests(
        principal, include_closed=True, limit=RECENT_LIMIT))
    return page["requests"]


def cards(summary: dict) -> list[dict]:
    """The summary cards as display text: ``{id, kind, label, value, sub, href}`` (``value`` is ``None`` when unknown)."""
    projects, requests, attention = summary["projects"], summary["requests"], summary["attention"]
    other = f" · 그 밖 {projects['other']}" if projects["other"] else ""
    sub_projects = "" if projects["total"] is None else f"전체 {projects['total']}개 중"
    sub_closed = "" if projects["unknown"] is None else f"연결 없음/불명 {projects['unknown']}{other}"
    sub_attention = "" if attention["total"] is None else (
        f"추가 정보 필요 {attention['questions']} · Task 결정 대기 {attention['decisions']}")
    return [
        {"id": "projects-running", "kind": "running", "label": "프로젝트 실행 중", "value": projects["running"],
         "sub": sub_projects, "href": "#projects"},
        {"id": "projects-closed", "kind": "closed", "label": "프로젝트 꺼짐", "value": projects["closed"],
         "sub": sub_closed, "href": "#projects"},
        {"id": "requests-new", "kind": "new", "label": "신규", "value": requests["new"], "sub": "검토 대기",
         "href": "/requests?state=NEW"},
        {"id": "requests-reviewing", "kind": "reviewing", "label": "검토 중", "value": requests["reviewing"],
         "sub": "manager 검토", "href": "/requests?state=REVIEWING"},
        {"id": "requests-needs-info", "kind": "needs", "label": "추가 정보 필요", "value": requests["needs_info"],
         "sub": "질문 답변 대기", "href": "/requests?state=NEEDS_INFO"},
        {"id": "requests-running", "kind": "running", "label": "실행 중", "value": requests["running"],
         "sub": "승인된 요청", "href": "/requests?state=RUNNING"},
        {"id": "attention", "kind": "attn", "label": "내 답이 필요한 요청", "value": attention["total"],
         "sub": sub_attention, "href": "/requests?state=NEEDS_INFO"}]


UNREADABLE_REQUESTS_TEXT = "요청 저장소를 읽지 못했습니다"
PARTIAL_REQUESTS_TEXT = "일부 프로젝트의 요청을 읽지 못했습니다"
EMPTY_PROJECTS = "등록된 프로젝트가 없습니다"
EMPTY_REQUESTS = "요청이 없습니다"
EMPTY_ATTENTION = "답을 기다리는 요청이 없습니다"


def message(kind: str, text: str, when: bool = True) -> dict | None:
    return {"kind": kind, "text": text} if when else None


async def build(request: web.Request) -> dict:
    """The dashboard model, JSON-ready. A part that could not be read is ``None``/empty with a ``messages`` entry of kind
    ``error`` (fixed text, no detail); an empty part has one of kind ``empty``; a part that shows rows has ``None``."""
    rows, problem = await board_status.fetch(request)
    now = float(request.app[SERVICES].clock())
    names = {row["project_id"]: presentation.row_cells(row, now)[0] for row in rows if isinstance(row.get("project_id"), str)}
    try:
        opened, partial = await open_requests(request)
        recent = [recent_item(item, names, now) for item in await recent_requests(request)]
    except InboxError:  # a broken inbox is "unknown" on the cards, never 0 (HB-AC-19), and its detail stays out
        opened, partial, recent = None, False, []
    waiting = sorted((item for item in opened or () if item["state"] == "NEEDS_INFO"),
                     key=lambda item: item["updated_at"], reverse=True)
    summary = {"projects": project_counts(None if problem else rows), **request_counts(opened)}
    attention = [attention_item(item, names, now) for item in waiting[:ATTENTION_LIMIT]]
    readable = opened is not None
    messages = {
        "projects": message("error", problem, bool(problem)) or message("empty", EMPTY_PROJECTS, not rows),
        "requests": message("error", UNREADABLE_REQUESTS_TEXT, not readable) or message("error", PARTIAL_REQUESTS_TEXT, partial),
        "recent": message("empty", EMPTY_REQUESTS, readable and not recent),
        "attention": message("empty", EMPTY_ATTENTION, readable and not attention)}
    return {"generated": time.strftime("%H:%M:%S UTC", time.gmtime(now)), "messages": messages, "summary": summary,
            "cards": cards(summary), "projects": [project_item(row, now) for row in rows], "recent": recent,
            "attention": attention, "board": board_card(request)}


# -- the page: the model drawn on the server. ``static/dashboard.js`` redraws the same structure from the JSON. -------
def number(value: object) -> str:
    return render.escape(presentation.EMPTY if value is None else value)


def plain(text: str) -> render.Markup:
    """A cell value; the Hub's ``—`` (nothing to say) is muted."""
    return render.tag("span", text, class_="none") if text == presentation.EMPTY else render.escape(text)


def card_markup(card: dict) -> render.Markup:
    return render.tag("a", render.html_join([
        render.tag("span", card["label"], class_="stat-label"),
        render.tag("strong", number(card["value"]), class_="stat-value", id=f"card-{card['id']}-value"),
        render.tag("span", card["sub"], class_="stat-sub", id=f"card-{card['id']}-sub")]),
        class_="stat stat-attn" if card["kind"] == "attn" else "stat", id=f"card-{card['id']}", data_card=card["id"],
        data_kind=card["kind"], href=card["href"])


def labelled_cell(content: object, label: str | None, class_: str | None = None) -> render.Markup:
    """A table cell; ``data-label`` is its column's name, which the narrow-screen card layout shows in front of it
    (``td[data-label]::before`` in app.css). The first cell is the card's title and has none."""
    return render.tag("td", content, class_=class_, data_label=label)


def project_row(item: dict) -> render.Markup:
    """Seven cells (2026-10-28, so the table fits without sideways scrolling): the path under the name, the command
    under the Task, the review mode under the wait reason. ``static/dashboard.js`` draws the same cells."""
    labels = PROJECT_COLUMNS
    name = render.tag("a", item["name"], href=item["url"], class_="proj-link") if item["url"] else item["name"]
    links = render.html_join([render.tag("a", "상태", href=item["url"]), " ",
                              render.tag("a", "요청", href=item["requests_url"])]) if item["url"] else ""
    wait = render.tag("span", item["wait"], class_="wait") if item["wait"] != presentation.EMPTY else plain(item["wait"])
    return render.tag("tr", render.html_join([
        labelled_cell(render.html_join([name, render.tag("span", item["path"], class_="path")]), None),
        labelled_cell(render.tag("span", item["power"], class_=f"chip p-{item['power_kind']}"), labels[1]),
        labelled_cell(render.html_join([plain(item["task"]), render.tag("span", plain(item["command"]), class_="sub")]),
                      labels[2]),
        labelled_cell(render.html_join([wait, render.tag("span", item["review_mode"], class_="sub")]), labels[3]),
        labelled_cell(plain(item["requests"]), labels[4]),
        labelled_cell(plain(item["last_activity"]), labels[5]),
        labelled_cell(links, labels[6], "links")]))


def request_row(item: dict) -> render.Markup:
    labels = REQUEST_COLUMNS
    title = render.html_join([render.tag("a", render.tag("bdi", item["title"]), href=item["url"]),
                              render.tag("span", "내 답 필요", class_="flag") if item["attention"] else ""])
    return render.tag("tr", render.html_join([
        labelled_cell(title, None, "title"), labelled_cell(item["project"], labels[1], "proj"),
        labelled_cell(render.tag("span", item["state_label"], class_=f"chip s-{item['state_kind']}"), labels[2]),
        labelled_cell(item["origin"], labels[3], "src"), labelled_cell(item["updated"], labels[4], "when")]))


def attention_row(item: dict) -> render.Markup:
    return render.tag("li", render.html_join([
        render.tag("a", render.tag("bdi", item["title"]), href=item["url"]),
        render.tag("span", " · ".join((item["project"], item["reason"], item["updated"])), class_="meta")]))


def table(columns: tuple, rows: list[render.Markup], body_id: str, wrap_id: str, class_: str = "") -> render.Markup:
    head = render.tag("thead", render.tag("tr", render.html_join(render.tag("th", title, scope="col") for title in columns)))
    return render.data_table(render.html_join([head, render.tag("tbody", render.html_join(rows), id=body_id)]), class_,
                             id=wrap_id, hidden=not rows)


def section_head(heading_id: str, title: str, extra: object = "") -> render.Markup:
    return render.tag("div", render.html_join([render.tag("h2", title, id=heading_id), extra]), class_="card-head")


# Column names: the ``th`` texts and the narrow-screen ``data-label`` of each cell. The project table has seven (the CLI's
# eight Hub columns plus 검토 방식 and 바로가기, merged in pairs); ``dashboard.js`` carries the same words.
PROJECT_COLUMNS = ("프로젝트", "상태", "Task·명령", "대기·검토", "요청", "마지막 활동", "바로가기")
REQUEST_COLUMNS = ("제목", "프로젝트", "상태", "출처", "갱신")


def message_markup(name: str, entry: dict | None) -> render.Markup:
    """The one status line of a part (``p#<name>-message``): an alert for an error, muted for an empty part, hidden when
    there is nothing to say. ``dashboard.js`` rewrites it in place."""
    kind = entry["kind"] if entry else "empty"
    return render.tag("p", entry["text"] if entry else "", id=f"{name}-message", class_=kind,
                      role="alert" if kind == "error" else None, hidden=entry is None)


def dashboard_body(model: dict) -> render.Markup:
    messages = model["messages"]
    projects, recent, attention = model["projects"], model["recent"], model["attention"]
    total = model["summary"]["attention"]["total"]
    head = render.tag("div", render.html_join([
        render.tag("h1", "대시보드"),
        render.tag("p", render.html_join([
            render.tag("span", "숫자와 표는 자동으로 갱신됩니다 · ", id="dash-auto", hidden=True), "마지막 갱신 ",
            render.tag("span", model["generated"], id="dash-time")]), class_="updated")]), class_="page-head")
    notice = message_markup("requests", messages["requests"])
    summary = render.tag("section", render.html_join([
        render.tag("h2", "요약", id="summary-h", class_="visually-hidden"),
        *(card_markup(card) for card in model["cards"])]), id="summary", class_="summary", aria_labelledby="summary-h")
    projects_card = render.tag("section", render.html_join([
        section_head("projects-h", "프로젝트", render.tag("span", f"{len(projects)}개 · 이름을 누르면 상세",
                                                         id="projects-note", class_="card-note")),
        message_markup("projects", messages["projects"]),
        table(PROJECT_COLUMNS, [project_row(item) for item in projects], "projects-body", "projects-wrap", "board tcards")]),
        id="projects", class_="card dash-card", aria_labelledby="projects-h")
    recent_card = render.tag("section", render.html_join([
        section_head("recent-h", "최근 요청", render.tag("a", "전체 보기", href="/requests", class_="card-link")),
        message_markup("recent", messages["recent"]),
        table(REQUEST_COLUMNS, [request_row(item) for item in recent], "recent-body", "recent-wrap", "requests tcards")]),
        id="recent-requests", class_="card dash-card", aria_labelledby="recent-h")
    board = model["board"]
    board_card = render.tag("section", render.html_join([
        section_head("agent-h", "Board agent"),
        render.tag("p", render.html_join([
            render.tag("span", "", id="board-dot", class_=f"dot dot-{board['state']}"),
            render.tag("strong", board["label"], id="board-label")]), class_="agent-state"),
        render.tag("p", board["summary"], id="board-counts", class_="muted") if board["summary"] else "",
        render.tag("a", "열기", href=board["url"], id="board-open", class_="btn btn-primary btn-block")]),
        id="board-agent", class_="card agent-card", aria_labelledby="agent-h")
    attention_card = render.tag("section", render.html_join([
        section_head("attn-h", "내 답이 필요한 요청", render.tag("span", number(total), id="attention-count", class_="count")),
        render.tag("ul", render.html_join(attention_row(item) for item in attention), id="attention-list", class_="attn-list"),
        message_markup("attention", messages["attention"])]),
        id="attention", class_="card attn-card", aria_labelledby="attn-h")
    layout = render.tag("div", render.html_join([
        render.tag("div", render.html_join([projects_card, recent_card]), class_="primary"),
        render.tag("aside", render.html_join([board_card, attention_card]), class_="side", aria_label="요약 카드")]),
        class_="layout")
    return render.html_join([head, notice, summary, layout])


async def get_home(request: web.Request) -> web.Response:
    return board_status.frame(request, "대시보드", dashboard_body(await build(request)), scripts=SCRIPTS)


@api
async def get_json(request: web.Request) -> web.Response:
    return web.json_response(await build(request))


def register(app: web.Application) -> None:
    app[login.HOME] = get_home
    app.router.add_get("/api/dashboard", get_json)
