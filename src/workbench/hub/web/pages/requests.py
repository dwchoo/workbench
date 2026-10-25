"""Request pages (TI-06; SPEC "웹 route와 화면"): list, detail, create/edit/comment/state forms and the
cancel confirmation page; TI-14 result pages: ``/full`` (HTML) and the raw log / artifact pages ``/raw``,
``/artifact`` (the original bytes as inline ``text/plain; charset=utf-8``, never HTML).

Every page is server-rendered through :mod:`workbench.hub.web.render`: user text only ever reaches the
document as an escaped text node or inside ``<pre>`` (no Markdown, no HTML), URLs are built with
``urlencode``/``quote`` and nothing is inline script or style. State changes are POST then ``303``.
The web session is the owner; the handlers call :class:`~workbench.hub.inbox.service.InboxService`
with ``principal_of(request)`` so the service stays the one place that decides what is allowed.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import secrets
import sys
import time
from typing import Callable, Iterable
from urllib.parse import quote, urlencode

from aiohttp import web

from ...inbox.limits import InboxError
from ...inbox.results import MAX_PAGE as MAX_RESULT_PAGE, Results, page_bounds, summary as result_summary
from ...inbox.service import PERMISSIONS, STATES, Principal, ProjectRef
from .. import render
from ..middleware import SERVICES, api, principal_of, see_other, session_of
from ...web_control import DaemonExecutor

RESULT_WORKERS = 8

# -- labels: the one place for 출처/상태/outcome/comment-kind text --------------------------------
STATE_LABELS = {"NEW": "신규", "REVIEWING": "검토 중", "NEEDS_INFO": "추가 정보 필요", "READY": "실행 준비",
                "RUNNING": "실행 중", "CLOSED": "닫힘"}
OUTCOME_LABELS = {"success": "성공", "failed": "실패", "rejected": "거절", "cancelled": "취소"}
COMMENT_KIND_LABELS = {"comment": "댓글", "answer": "답변", "question": "질문", "progress": "진행",
                       "decision": "결정", "system": "시스템"}
AUTHOR_LABELS = {"owner": "소유자", "board_agent": "Board agent", "manager": "manager", "local": "로컬 CLI",
                 "hub": "시스템"}
EMPTY = "—"
PAGE_SIZE = 50
STYLESHEETS = ("/static/app.css",)
SCRIPTS = ("/static/badge.js",)  # header badge + tab title (D-24); no inline script
CANCEL_TOKEN_TTL = 60.0  # SPEC 취소: token 60 s
MAX_CANCEL_TOKENS = 64  # live tokens are capped; the oldest goes first
UTC_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(slots=True)
class RequestsPort:
    """What the handlers use: the inbox and the attached projects (id, name) for names and selection."""

    inbox: object
    projects: Callable[[], Iterable[ProjectRef]]
    cancel_tokens: dict = field(default_factory=dict)  # token -> (binding, expires_at); in memory only
    results: object = None  # :class:`~workbench.hub.inbox.results.Results` over ``inbox`` (TI-14)
    # Result reads (backend calls, file pages) run on this bounded pool, never on the loop's default executor that
    # logins and session checks use (security review fix 2).
    reads: object = field(default_factory=lambda: DaemonExecutor(RESULT_WORKERS, "hub-results"))


PORT: web.AppKey = web.AppKey("requests_port", RequestsPort)


def state_label(state: object) -> str:
    return STATE_LABELS.get(state, str(state))


def when(epoch: object) -> str:
    if not isinstance(epoch, (int, float)):
        return EMPTY
    return time.strftime(UTC_FORMAT, time.gmtime(epoch)) + " UTC"


def request_url(request_id: str, suffix: str = "") -> str:
    return f"/requests/{quote(request_id, safe='')}{suffix}"


def _query_url(path: str, **params: object) -> str:
    pairs = {key: value for key, value in params.items() if value not in (None, "", False)}
    return f"{path}?{urlencode(pairs)}" if pairs else path


# -- shared page pieces ---------------------------------------------------------------------------
def _frame(request: web.Request, title: str, body: object, *, status: int = 200) -> web.Response:
    services, session = request.app[SERVICES], session_of(request)
    return render.page(title, body, status=status, insecure_warning=services.insecure_warning, csrf=session.csrf,
                       stylesheets=STYLESHEETS, scripts=SCRIPTS)


async def _projects(request: web.Request) -> list[ProjectRef]:
    port = request.app[PORT]
    return list(await request.app[SERVICES].blocking(lambda: list(port.projects())))


def _form_text(form, name: str) -> str:
    value = form.get(name, "")
    return value.replace("\r\n", "\n").replace("\r", "\n") if isinstance(value, str) else ""


def _error_status(error: InboxError) -> int:
    return {"not_found": 404, "forbidden": 403, "conflict": 409, "invalid_transition": 409,
            "idempotency_conflict": 409, "backend_not_running": 409, "token_mismatch": 409,
            "backend_unreachable": 503, "backend_too_old": 409, "backend_refused": 409,
            "inbox_busy": 503, "inbox_unavailable": 503, "project_unavailable": 503,
            "internal_error": 500}.get(error.code, 400)


ERROR_TEXT = {"not_found": "찾을 수 없습니다", "forbidden": "이 동작은 허용되지 않습니다",
              "invalid_input": "입력이 올바르지 않습니다", "too_large": "입력이 한도를 넘었습니다",
              "conflict": "다른 곳에서 바뀌었습니다", "invalid_transition": "지금 상태에서는 할 수 없는 동작입니다",
              "idempotency_conflict": "이미 제출된 양식입니다. 새 양식으로 다시 제출하세요",
              "backend_not_running": "프로젝트 backend가 꺼져 있어 실행 중인 작업은 취소할 수 없습니다",
              "token_mismatch": "작업이 바뀌었습니다. 확인 화면을 다시 여세요",
              "backend_unreachable": "프로젝트 backend에 연결하지 못해 취소하지 않았습니다",
              "backend_too_old": "프로젝트 backend가 오래되어 취소할 수 없습니다",
              "backend_refused": "프로젝트 backend가 취소를 거절했습니다",
              "project_unavailable": "프로젝트를 사용할 수 없습니다", "inbox_busy": "요청 저장소가 사용 중입니다",
              "inbox_unavailable": "요청 저장소를 읽을 수 없습니다", "internal_error": "저장하지 못했습니다"}


# Only errors caused by what the user typed may show the service's detail; storage and internal errors
# carry paths and sqlite messages, so they get the fixed text alone.
DETAIL_CODES = frozenset({"invalid_input", "too_large", "invalid_transition", "idempotency_conflict"})


def _error_block(error: InboxError) -> render.Markup:
    text = ERROR_TEXT.get(error.code, error.code)
    parts = [render.tag("strong", text)]
    if error.detail and error.code in DETAIL_CODES:
        parts.append(render.tag("span", f" ({error.detail})"))
    return render.tag("p", render.html_join(parts), class_="error", role="alert")


def error_page(request: web.Request, error: InboxError, *, back: str | None = None) -> web.Response:
    parts = [render.tag("h1", ERROR_TEXT.get(error.code, "요청을 처리하지 못했습니다")), _error_block(error)]
    if back:
        parts.append(render.tag("p", render.tag("a", "돌아가기", href=back)))
    parts.append(render.tag("p", render.tag("a", "요청 목록", href="/requests")))
    return _frame(request, "오류", render.html_join(parts), status=_error_status(error))


async def _call(request: web.Request, function, *args, **kwargs):
    """Run a blocking ``InboxService`` call off the loop."""
    return await request.app[SERVICES].blocking(lambda: function(*args, **kwargs))


# -- list -----------------------------------------------------------------------------------------
def _select(name: str, options: Iterable[tuple[str, str]], current: str | None, *, blank: str) -> render.Markup:
    items = [render.tag("option", blank, value="", selected=not current)]
    items += [render.tag("option", label, value=value, selected=value == current) for value, label in options]
    return render.tag("select", render.html_join(items), name=name)


NUMBER_MAX_DIGITS = 9


def number_field(text: object, what: str = "숫자") -> int:
    """A non-negative decimal from a form or query field; anything else is ``invalid_input``.

    ``str.isdigit`` alone accepts ``'²'`` (``int`` then raises) and huge digit strings, so ASCII and a
    length cap are checked first.
    """
    if not (isinstance(text, str) and text.isascii() and text.isdigit() and len(text) <= NUMBER_MAX_DIGITS):
        raise InboxError("invalid_input", f"{what}이(가) 올바른 숫자가 아닙니다")
    return int(text)


async def get_list(request: web.Request) -> web.Response:
    port, query = request.app[PORT], request.query
    projects = await _projects(request)
    names = {ref.project_id: ref.name or ref.project_id for ref in projects}
    project_id = query.get("project") or None
    state = query.get("state") or None
    archive = bool(query.get("archive"))
    text = query.get("q") or None
    page, error = 1, None
    result = {"requests": [], "next_cursor": None}
    try:
        if "page" in query:
            page = number_field(query["page"], "page")
            if page < 1:
                raise InboxError("invalid_input", "page는 1부터입니다")
        cursor = f"o{(page - 1) * PAGE_SIZE}" if page > 1 else None
        if state is not None and state not in STATES:
            raise InboxError("invalid_input", "알 수 없는 상태 filter")
        result = await _call(request, port.inbox.list_requests, principal_of(request), project_id=project_id,
                             states=None if state is None else [state], include_closed=archive, query=text,
                             limit=PAGE_SIZE, cursor=cursor)
    except InboxError as exc:
        error = exc
    filters = dict(project=project_id, state=state, archive="1" if archive else None, q=text)
    form = render.tag("form", render.html_join([
        _select("project", [(ref.project_id, ref.name or ref.project_id) for ref in projects], project_id,
                blank="모든 프로젝트"),
        _select("state", [(name, STATE_LABELS[name]) for name in STATES], state, blank="모든 상태"),
        render.tag("label", render.html_join([render.void("input", type="checkbox", name="archive", value="1",
                                                          checked=archive), " 보관함(닫힘 포함)"])),
        render.void("input", type="search", name="q", value=text or "", placeholder="검색어"),
        render.tag("button", "찾기", type="submit")]), method="get", action="/requests", class_="filters")
    body = [render.tag("h1", "요청"), render.tag("p", render.tag("a", "새 요청", href="/requests/new")), form]
    if error is not None:
        body.append(_error_block(error))
    for problem in result.get("unavailable", ()):
        body.append(render.tag("p", f"읽을 수 없는 프로젝트: {names.get(problem['project_id'], '?')} "
                                     f"({problem['code']})", class_="error"))
    rows = [render.tag("tr", render.html_join(render.tag("th", head) for head in
                                              ("#", "프로젝트", "제목", "상태", "출처", "갱신")))]
    for item in result["requests"]:
        label = state_label(item["state"])
        if item["state"] == "CLOSED" and item.get("outcome"):
            label = f"{label}({OUTCOME_LABELS.get(item['outcome'], item['outcome'])})"
        rows.append(render.tag("tr", render.html_join([
            render.tag("td", f"#{item['seq']}"), render.tag("td", names.get(item["project_id"], item["project_id"])),
            render.tag("td", render.tag("a", render.tag("bdi", item["title"]), href=request_url(item["request_id"])),
                       class_="title"),
            render.tag("td", label), render.tag("td", item["origin"]["label"]),
            render.tag("td", when(item["updated_at"]))])))
    if result["requests"]:
        body.append(render.tag("table", render.html_join(rows), class_="requests"))
    elif error is None:
        body.append(render.tag("p", "요청이 없습니다", class_="empty"))
    pager = []
    if page > 1:
        pager.append(render.tag("a", "이전", href=_query_url("/requests", **filters, page=page - 1 if page > 2 else None),
                                rel="prev"))
    if result["next_cursor"]:
        pager.append(render.tag("a", "다음", href=_query_url("/requests", **filters, page=page + 1), rel="next"))
    if pager:
        body.append(render.tag("nav", render.html_join(pager), class_="pager"))
    return _frame(request, "요청", render.html_join(body), status=200 if error is None else _error_status(error))

# -- create -----------------------------------------------------------------------------------------
def _field_row(label: str, control: object) -> render.Markup:
    return render.tag("label", render.html_join([label, render.void("br"), control]))


def _textarea(name: str, text: str, **attributes: object) -> render.Markup:
    # The HTML parser drops one newline right after <textarea>; add one so a leading newline survives.
    return render.tag("textarea", "\n" + text, name=name, **attributes)


def create_form(request: web.Request, projects: list[ProjectRef], *, project: str | None = None,
                title: str = "", body: str = "", nonce: str | None = None,
                error: InboxError | None = None) -> render.Markup:
    chosen = project if any(ref.project_id == project for ref in projects) else None
    options = [render.tag("option", ref.name or ref.project_id, value=ref.project_id,
                          selected=ref.project_id == chosen)
               for ref in projects if ref.unavailable is None]
    parts = [render.tag("h1", "새 요청")]
    if error is not None:
        parts.append(_error_block(error))
    parts.append(render.tag("form", render.html_join([
        render.csrf_field(session_of(request).csrf), render.hidden("nonce", nonce or secrets.token_urlsafe(18)),
        _field_row("프로젝트", render.tag("select", render.html_join(options), name="project", required=True)),
        # No maxlength: HTML counts UTF-16 units (an emoji is 2) and cuts IME input; the server counts code points.
        _field_row("제목", render.void("input", type="text", name="title", value=title, required=True)),
        _field_row("본문", _textarea("body", body, rows=12, required=True)),
        render.tag("button", "제출", type="submit")]), method="post", action="/requests", class_="stack wide"))
    parts.append(render.tag("p", render.tag("a", "요청 목록", href="/requests")))
    return render.html_join(parts)


async def get_new(request: web.Request) -> web.Response:
    return _frame(request, "새 요청", create_form(request, await _projects(request),
                                                  project=request.query.get("project")))


async def post_create(request: web.Request) -> web.Response:
    port, form = request.app[PORT], await request.post()
    project, title, body = _form_text(form, "project"), _form_text(form, "title"), _form_text(form, "body")
    nonce = _form_text(form, "nonce")
    try:
        created = await _call(request, port.inbox.submit, principal_of(request), project, title, body, nonce or None)
    except InboxError as exc:
        # A reused nonce with other text must not silently become a second request: hand out a fresh one.
        fresh = None if exc.code == "idempotency_conflict" else nonce
        return _frame(request, "새 요청", create_form(request, await _projects(request), project=project,
                                                      title=title, body=body, nonce=fresh, error=exc),
                      status=_error_status(exc))
    return see_other(request_url(created["request_id"]))

# -- detail -----------------------------------------------------------------------------------------
def author_label(kind: object, name: object) -> str:
    if kind in AUTHOR_LABELS:
        return AUTHOR_LABELS[kind]
    return f"key:{name}" if name else str(kind)


def allowed_actions(principal: Principal, detail: dict, *, resends_cancel: bool = False) -> set[str]:
    """Which buttons ``principal`` gets on this request: the SPEC permission table narrowed by state.

    The service still decides on every POST; this only keeps buttons it would refuse out of the page.
    """
    kind, state = principal.kind, detail["state"]
    allowed = {action for action in ("edit", "comment", "set_state", "cancel") if kind in PERMISSIONS[action]}
    if state == "CLOSED":
        allowed -= {"edit", "cancel"}
    elif state not in ("NEW", "NEEDS_INFO"):
        allowed.discard("set_state")  # only NEW <-> NEEDS_INFO and reopening a CLOSED request (D-34)
    # a cancel left ``requested`` (lost answer, Hub restart) may be asked again when the inbox can send it again
    # (``InboxService.resends_cancel``); a started stop may not
    if detail.get("cancel_state") is not None and not (detail.get("cancel_state") == "requested" and resends_cancel):
        allowed.discard("cancel")
    return allowed


def _definition(label: str, value: object) -> render.Markup:
    return render.html_join([render.tag("dt", label), render.tag("dd", value)])


def _task_section(detail: dict) -> render.Markup:
    task = detail["task"]
    parts = [render.tag("h2", "연결 Task")]
    if not task["task_id"]:
        parts.append(render.tag("p", EMPTY, class_="none"))
    else:
        view = task["view"] or {}
        items = [_definition("Task", task["task_id"]), _definition("명세 revision", f"명세 revision {task['spec_revision']}"),
                 _definition("진행", view.get("status") or EMPTY)]
        if view.get("closed"):
            items.append(_definition("닫힘", view.get("closed_reason") or "닫힘"))
        for key, value in sorted(view.items()):
            if key not in ("status", "closed", "closed_reason") and isinstance(value, (str, int, float)):
                items.append(_definition(str(key), str(value)[:300]))
        parts.append(render.tag("dl", render.html_join(items)))
    return render.tag("section", render.html_join(parts), id="task")


def _revisions_section(detail: dict) -> render.Markup:
    revisions = detail["revisions"]
    if len(revisions) < 2:
        return render.Markup("")
    items = []
    for revision in revisions:
        head = f"r{revision['revision']} · {author_label(revision['author_kind'], revision['author_name'])} · " \
               f"{when(revision['created_at'])}"
        items.append(render.tag("li", render.tag("details", render.html_join([
            render.tag("summary", render.html_join([head + " · ", render.tag("bdi", revision["title"])])),
            render.pre(revision["body"])]), open=revision["revision"] == detail["revision"])))
    return render.tag("section", render.html_join([render.tag("h2", "revision 이력"),
                                                   render.tag("ol", render.html_join(items))]), id="revisions")


def _thread_section(detail: dict) -> render.Markup:
    items = []
    for comment in detail["comments"]:
        kind = comment["kind"]
        head = (f"[{COMMENT_KIND_LABELS.get(kind, kind)}] {author_label(comment['author_kind'], comment['author_name'])}"
                f" · {when(comment['created_at'])}")
        items.append(render.tag("li", render.html_join([render.tag("div", head, class_="meta"),
                                                        render.pre(comment["body"])]),
                                class_=f"comment kind-{kind if kind in COMMENT_KIND_LABELS else 'other'}"))
    body = render.tag("ol", render.html_join(items), class_="thread") if items else \
        render.tag("p", "댓글이 없습니다", class_="none")
    return render.tag("section", render.html_join([render.tag("h2", "댓글"), body]), id="thread")


def _post_form(request: web.Request, action: str, fields: dict, button: str, *, extra: object = "",
               class_: str | None = None) -> render.Markup:
    hidden = [render.hidden(name, value) for name, value in fields.items()]
    return render.tag("form", render.html_join([render.csrf_field(session_of(request).csrf), *hidden, extra,
                                                render.tag("button", button, type="submit")]),
                      method="post", action=action, class_=class_)


NOTICES = {"created": "요청을 접수했습니다", "edited": "내용을 바꿨습니다", "revised": "새 revision으로 저장했습니다",
           "commented": "댓글을 남겼습니다", "answered": "답변을 남기고 검토로 돌려보냈습니다",
           "state_set": "상태를 바꿨습니다", "reopened": "요청을 다시 열었습니다", "cancelled": "요청을 취소했습니다",
           "cancel_requested": "실행 중인 작업의 취소를 요청했습니다(아직 취소되지 않았습니다)",
           "cancel_stopping": "취소를 요청했고 실행 중인 명령을 멈추는 중입니다(아직 취소되지 않았습니다)",
           "cancel_unknown": "backend가 답하지 않아 취소 결과를 알 수 없습니다",
           "review_started": "검토를 시작했습니다. manager에게 알립니다"}


def start_review_label(principal: Principal, detail: dict, review_mode: object) -> str | None:
    """The owner's review button for this request, or None (SPEC 상세: 검토 시작은 수동·``pending``일 때, unknown 뒤에는
    "다시 전달"). The service still decides on the POST."""
    if principal.kind != "owner" or detail["state"] not in ("NEW", "REVIEWING"):
        return None
    if detail["delivery_state"] == "unknown":
        return "다시 전달"
    if detail["delivery_state"] == "pending" and review_mode == "manual":
        return "검토 시작"
    return None


async def _review_mode(request: web.Request, detail: dict) -> object:
    """The project's review mode when the page may need it (an open round waiting to be sent), else None."""
    if detail["state"] not in ("NEW", "REVIEWING") or detail["delivery_state"] != "pending":
        return None
    port, principal = request.app[PORT], principal_of(request)
    try:
        return (await _call(request, port.inbox.review_mode, principal, detail["project_id"]))["review_mode"]
    except InboxError:
        return None


def detail_page(request: web.Request, detail: dict, *, notice: str | None = None, comment_error: InboxError | None = None,
                comment_draft: str = "", projects: list[ProjectRef] | None = None,
                status: int = 200, review_mode: object = None) -> web.Response:
    principal = principal_of(request)
    allowed = allowed_actions(principal, detail,
                              resends_cancel=getattr(request.app[PORT].inbox, "resends_cancel", False))
    rid = detail["request_id"]
    names = {ref.project_id: ref.name or ref.project_id for ref in projects or ()}
    state = state_label(detail["state"])
    if detail["state"] == "CLOSED" and detail["outcome"]:
        state = f"{state}({OUTCOME_LABELS.get(detail['outcome'], detail['outcome'])})"
    facts = [_definition("프로젝트", names.get(detail["project_id"], detail["project_id"])),
             _definition("출처", detail["origin"]["label"]), _definition("상태", state),
             _definition("revision", f"r{detail['revision']}"), _definition("접수", when(detail["created_at"])),
             _definition("갱신", when(detail["updated_at"]))]
    if detail.get("cancel_state"):
        facts.append(_definition("취소", "취소 요청됨 · 명령 멈추는 중" if detail["cancel_state"] == "stop_requested"
                                 else "취소 요청됨"))
    summary = render.tag("section", render.html_join([
        render.tag("h1", render.html_join([f"#{detail['seq']} ", render.tag("bdi", detail["title"])])),
        render.tag("dl", render.html_join(facts))]), id="summary")
    buttons = []
    if "edit" in allowed:
        buttons.append(render.tag("a", "편집", href=request_url(rid, "/edit"), class_="button"))
    if "set_state" in allowed:
        target, label = {"NEW": ("NEEDS_INFO", "추가 정보 필요로 지정"), "NEEDS_INFO": ("NEW", "신규로 되돌리기"),
                         "CLOSED": ("NEW", "다시 열기")}[detail["state"]]
        buttons.append(_post_form(request, request_url(rid, "/state"),
                                  {"to": target, "base_version": detail["version"]}, label, class_="inline"))
    review_label = start_review_label(principal, detail, review_mode)
    if review_label is not None:
        buttons.append(_post_form(request, request_url(rid, "/start-review"), {}, review_label, class_="inline"))
    if "cancel" in allowed:
        buttons.append(render.tag("a", "취소", href=request_url(rid, "/cancel"), class_="button danger"))
    buttons.append(render.tag("a", "전체 보기", href=request_url(rid, "/full"), class_="button"))  # TI-14
    body = [summary]
    if notice in NOTICES:
        body.append(render.tag("p", NOTICES[notice], class_="notice", role="status"))
    if buttons:
        body.append(render.tag("div", render.html_join(buttons), class_="actions"))
    body.append(render.tag("section", render.html_join([render.tag("h2", "본문"), render.pre(detail["body"])]),
                           id="body"))
    body.append(_revisions_section(detail))
    body.append(_task_section(detail))
    text = result_summary(detail)["summary"]
    result = render.pre(text) if text else render.tag("p", EMPTY, class_="none")
    body.append(render.tag("section", render.html_join([render.tag("h2", "결과 요약"), result]), id="result"))
    body.append(_thread_section(detail))
    if "comment" in allowed:
        label = "답변" if detail["state"] == "NEEDS_INFO" else "댓글"
        extra = render.html_join([comment_error and _error_block(comment_error) or "",
                                  _field_row(label, _textarea("body", comment_draft, rows=5, required=True))])
        body.append(render.tag("section", render.html_join([
            render.tag("h2", f"{label} 쓰기"),
            _post_form(request, request_url(rid, "/comments"), {}, "남기기", extra=extra, class_="stack wide")]),
            id="comment-form"))
    body.append(render.tag("p", render.tag("a", "요청 목록", href="/requests")))
    return _frame(request, f"#{detail['seq']} {detail['title']}", render.html_join(body), status=status)


async def _detail(request: web.Request, request_id: str) -> dict:
    return await _call(request, request.app[PORT].inbox.get, principal_of(request), request_id)


async def get_detail(request: web.Request) -> web.Response:
    try:
        detail = await _detail(request, request.match_info["rid"])
    except InboxError as exc:
        return error_page(request, exc)
    return detail_page(request, detail, notice=request.query.get("notice"), projects=await _projects(request),
                       review_mode=await _review_mode(request, detail))

# -- edit -------------------------------------------------------------------------------------------
def edit_form(request: web.Request, detail: dict, *, title: str | None = None, body: str | None = None,
              error: InboxError | None = None, latest: bool = False,
              base_version: int | None = None) -> render.Markup:
    rid = detail["request_id"]
    parts = [render.tag("h1", render.html_join([f"#{detail['seq']} ", render.tag("bdi", detail["title"]), " 편집"]))]
    if error is not None:
        parts.append(_error_block(error))
    if latest:
        parts.append(render.tag("section", render.html_join([
            render.tag("h2", "지금 저장된 내용"), render.tag("p", render.tag("bdi", detail["title"])),
            render.pre(detail["body"])]), id="latest"))
        parts.append(render.tag("p", "입력하던 내용은 아래 양식에 그대로 두었습니다. 확인하고 다시 저장하세요."))
    parts.append(_post_form(request, request_url(rid, "/edit"), {"base_version": detail["version"] if base_version is None else base_version}, "저장", extra=render.html_join([
        _field_row("제목", render.void("input", type="text", name="title", value=detail["title"] if title is None else title,
                                      required=True)),
        _field_row("본문", _textarea("body", detail["body"] if body is None else body, rows=12, required=True))]),
        class_="stack wide"))
    parts.append(render.tag("p", render.tag("a", "돌아가기", href=request_url(rid))))
    return render.html_join(parts)


async def get_edit(request: web.Request) -> web.Response:
    try:
        detail = await _detail(request, request.match_info["rid"])
    except InboxError as exc:
        return error_page(request, exc)
    if detail["state"] == "CLOSED":
        return error_page(request, InboxError("invalid_transition", "닫힌 요청은 먼저 다시 열어야 편집할 수 있습니다"),
                          back=request_url(detail["request_id"]))
    return _frame(request, "편집", edit_form(request, detail))


async def post_edit(request: web.Request) -> web.Response:
    port, rid, form = request.app[PORT], request.match_info["rid"], await request.post()
    title, body, base = _form_text(form, "title"), _form_text(form, "body"), _form_text(form, "base_version")
    posted = None
    try:
        detail = await _detail(request, rid)
        posted = number_field(base, "base_version")
        done = await _call(request, port.inbox.update, principal_of(request), rid, posted, title=title, body=body)
    except InboxError as exc:
        try:
            detail = await _detail(request, rid)
        except InboxError:
            return error_page(request, exc)
        if exc.code in ("invalid_transition",) or detail["state"] == "CLOSED":
            return error_page(request, exc, back=request_url(rid))
        # A rejected edit built on an out-of-date version keeps that version, and the page shows what is
        # saved now: resubmitting then conflicts openly instead of overwriting a change nobody saw.
        stale = posted is not None and posted != detail["version"]
        return _frame(request, "편집", edit_form(request, detail, title=title, body=body, error=exc,
                                                 latest=stale or exc.code == "conflict",
                                                 base_version=posted if stale and exc.code != "conflict" else None),
                      status=_error_status(exc))
    notice = "revised" if done["mode"] == "new_revision" else "edited"
    return see_other(_query_url(request_url(rid), notice=notice))

# -- comments and state -------------------------------------------------------------------------------
async def post_comment(request: web.Request) -> web.Response:
    port, rid, form = request.app[PORT], request.match_info["rid"], await request.post()
    body = _form_text(form, "body")
    try:
        before = await _detail(request, rid)
        done = await _call(request, port.inbox.comment, principal_of(request), rid, body)
    except InboxError as exc:
        try:
            detail = await _detail(request, rid)
        except InboxError:
            return error_page(request, exc)
        return detail_page(request, detail, comment_error=exc, comment_draft=body, projects=await _projects(request),
                           status=_error_status(exc), review_mode=await _review_mode(request, detail))
    return see_other(_query_url(request_url(rid), notice="answered" if done["state"] != before["state"] else "commented"))


async def post_state(request: web.Request) -> web.Response:
    port, rid, form = request.app[PORT], request.match_info["rid"], await request.post()
    target, base = _form_text(form, "to"), _form_text(form, "base_version")
    try:
        version = number_field(base, "base_version")
        before = await _detail(request, rid)
        await _call(request, port.inbox.set_state, principal_of(request), rid, version, target)
    except InboxError as exc:
        return error_page(request, exc, back=request_url(rid))
    return see_other(_query_url(request_url(rid), notice="reopened" if before["state"] == "CLOSED" else "state_set"))

# -- cancel -----------------------------------------------------------------------------------------
def _is_linked(detail: dict) -> bool:
    """A live Task is attached: the cancel stops it (READY/RUNNING, or a re-asked request that still links one)."""
    return detail["state"] != "CLOSED" and detail["task"]["task_id"] is not None


def _binding(request: web.Request, detail: dict) -> tuple:
    """What a cancel token is tied to: the session (its csrf), who, which request and version, the Task
    status and its command."""
    principal, view = principal_of(request), detail["task"]["view"] or {}
    return (session_of(request).csrf, principal.kind, principal.key_id, detail["request_id"], detail["version"],
            view.get("status"), (_view_command(view) or {}).get("command_id"))


def _view_command(view: dict) -> dict | None:
    """The Task's running command in the synced view (``task_status.command``), or None."""
    command = view.get("command")
    return command if isinstance(command, dict) and isinstance(command.get("command_id"), str) else None


def issue_cancel_token(request: web.Request, detail: dict) -> str:
    port, now = request.app[PORT], float(request.app[SERVICES].clock())
    live = {token: entry for token, entry in port.cancel_tokens.items() if entry[1] > now}
    while len(live) >= MAX_CANCEL_TOKENS:  # dicts keep insertion order: drop the oldest
        del live[next(iter(live))]
    token = secrets.token_urlsafe(24)
    live[token] = (_binding(request, detail), now + CANCEL_TOKEN_TTL)
    port.cancel_tokens = live
    return token


def take_cancel_token(request: web.Request, token: str, detail: dict) -> bool:
    """Single use: the token disappears even when it no longer matches."""
    port = request.app[PORT]
    entry = port.cancel_tokens.pop(token, None) if token else None
    return (entry is not None and entry[1] > float(request.app[SERVICES].clock())
            and entry[0] == _binding(request, detail))


async def get_cancel(request: web.Request) -> web.Response:
    try:
        detail = await _detail(request, request.match_info["rid"])
        if detail["state"] == "CLOSED":
            raise InboxError("invalid_transition", "이미 닫힌 요청입니다", state="CLOSED", version=detail["version"])
    except InboxError as exc:
        return error_page(request, exc, back=request_url(request.match_info["rid"]))
    rid, linked = detail["request_id"], _is_linked(detail)
    parts = [render.tag("h1", "요청 취소"),
             render.tag("p", render.html_join(["이 요청을 취소할까요? ", render.tag("bdi", f"#{detail['seq']} {detail['title']}")]))]
    fields = {"base_version": detail["version"]}
    if linked:
        view = detail["task"]["view"] or {}
        command = _view_command(view)
        command_text = EMPTY if command is None else (
            f"{command.get('label') or command['command_id']} ({command.get('state') or 'running'})")
        parts.append(render.tag("section", render.html_join([
            render.tag("h2", "영향"),
            render.tag("p", "연결된 Task와 실행 중인 명령을 멈추고(명령은 SIGINT부터), Task가 닫히고 명령이 끝나야 "
                            "이 요청이 취소로 닫힙니다."),
            render.tag("dl", render.html_join([_definition("Task", detail["task"]["task_id"]),
                                               _definition("진행", view.get("status") or EMPTY),
                                               _definition("실행 중인 명령", render.tag("bdi", command_text)),
                                               _definition("상태", state_label(detail["state"]))]))]), id="impact"))
        fields["token"] = issue_cancel_token(request, detail)
    parts.append(_post_form(request, request_url(rid, "/cancel"), fields, "확인", class_="inline"))
    parts.append(render.tag("p", render.tag("a", "취소", href=request_url(rid))))
    return _frame(request, "요청 취소", render.html_join(parts))


async def post_cancel(request: web.Request) -> web.Response:
    port, rid, form = request.app[PORT], request.match_info["rid"], await request.post()
    base, token = _form_text(form, "base_version"), _form_text(form, "token")
    try:
        detail = await _detail(request, rid)
        version = number_field(base, "base_version")
        if _is_linked(detail) and not take_cancel_token(request, token, detail):
            raise InboxError("token_mismatch", "", state=detail["state"], version=detail["version"])
        done = await _call(request, port.inbox.cancel, principal_of(request), rid, base_version=version)
    except InboxError as exc:
        return error_page(request, exc, back=request_url(rid))
    notice = ("cancelled" if done["status"] == "cancelled" else
              "cancel_unknown" if done.get("cancel") == "unknown" else
              "cancel_stopping" if done.get("stop") else "cancel_requested")
    return see_other(_query_url(request_url(rid), notice=notice))


async def post_start_review(request: web.Request) -> web.Response:
    """The owner's manual "검토 시작" / "다시 전달" (D-31, P2-AC-39). A pending round of an auto project is the
    dispatcher's own to send, so the web refuses to start it by hand."""
    port, rid = request.app[PORT], request.match_info["rid"]
    try:
        detail = await _detail(request, rid)
        if detail["delivery_state"] == "pending" and await _review_mode(request, detail) != "manual":
            raise InboxError("invalid_transition", "이 프로젝트는 자동 검토라 직접 시작하지 않습니다",
                             state=detail["state"], version=detail["version"])
        await _call(request, port.inbox.start_review, principal_of(request), rid)
    except InboxError as exc:
        return error_page(request, exc, back=request_url(rid))
    return see_other(_query_url(request_url(rid), notice="review_started"))


# -- results (TI-14; SPEC "결과 조회") -------------------------------------------------------------------
RESULT_ERROR_TEXT = {"backend_not_running": "프로젝트 backend가 꺼져 있어 원문 로그를 읽을 수 없습니다",
                     "backend_unreachable": "프로젝트 backend에 연결하지 못했습니다",
                     "forbidden": "이 경로는 읽을 수 없습니다"}
TEXT_HEADERS = {"Content-Disposition": "inline"}


def _result_error(request: web.Request, error: InboxError, rid: str) -> web.Response:
    text = RESULT_ERROR_TEXT.get(error.code) or ERROR_TEXT.get(error.code, "요청을 처리하지 못했습니다")
    parts = [render.tag("h1", text), _error_block(error) if error.code not in RESULT_ERROR_TEXT else "",
             render.tag("p", render.tag("a", "전체 보기로 돌아가기", href=request_url(rid, "/full"))),
             render.tag("p", render.tag("a", "요청 목록", href="/requests"))]
    status = {"backend_unreachable": 503}.get(error.code) or _error_status(error)
    return _frame(request, "오류", render.html_join(parts), status=status)


def _page_query(request: web.Request) -> tuple[int, int]:
    """``offset``/``length`` of a raw or artifact page from the query (defaults 0 and the page maximum)."""
    query = request.query
    offset = number_field(query["offset"], "offset") if query.get("offset", "") != "" else 0
    length = number_field(query["length"], "length") if query.get("length", "") != "" else MAX_RESULT_PAGE
    return page_bounds(offset, length)


def _page_response(page: dict) -> web.Response:
    """The original bytes: UTF-8 text as inline ``text/plain``, anything else as an ``application/octet-stream``
    download (never rendered as HTML; ``nosniff`` comes from the middleware)."""
    if "text" in page:
        return web.Response(body=page["text"].encode("utf-8"),
                            headers={"Content-Type": "text/plain; charset=utf-8", **TEXT_HEADERS})
    return web.Response(body=base64.b64decode(page["data_base64"]),
                        headers={"Content-Type": "application/octet-stream", "Content-Disposition": "attachment"})


async def _result_call(request: web.Request, function, *args):
    """A result read off the loop; an unexpected failure is ``internal_error`` (its type goes to the Hub log only)."""
    try:
        return await request.app[PORT].reads.run(function, *args)
    except InboxError:
        raise
    except Exception as exc:
        print(f"[workbench] result page failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        raise InboxError("internal_error", "the result could not be read") from None


def _raw_link(rid: str, source_id: object) -> render.Markup:
    if not isinstance(source_id, str):
        return render.Markup(EMPTY)
    return render.tag("a", "원문", href=f"{request_url(rid, '/raw')}?{urlencode({'run_id': source_id})}")


def _full_task_section(rid: str, full: dict) -> render.Markup:
    if not full["available"]:
        parts = [render.tag("p", f"backend에서 실행 기록을 읽지 못했습니다({full['reason']}). 마지막으로 본 Task 상태만 "
                                 "보입니다. 원문 로그는 backend를 켠 뒤 볼 수 있습니다.", class_="notice")]
        view = full.get("view") or {}
        runs = [run for run in view.get("runs") or [] if isinstance(run, dict)]
        rows = [render.tag("tr", render.html_join([render.tag("td", str(run.get("run_id"))),
                                                   render.tag("td", str(run.get("status"))),
                                                   render.tag("td", str(run.get("exit_code")))])) for run in runs]
        parts.append(_definition("진행", view.get("status") or EMPTY))
        if rows:
            parts.append(render.tag("table", render.html_join([render.tag("tr", render.html_join(
                [render.tag("th", "run"), render.tag("th", "상태"), render.tag("th", "exit")])), *rows])))
        return render.tag("section", render.html_join([render.tag("h2", "실행 기록"), *parts]), id="runs")
    task = full["task"]
    if task is None:
        return render.tag("section", render.html_join([render.tag("h2", "실행 기록"),
                                                       render.tag("p", "연결된 Task가 없습니다", class_="none")]), id="runs")
    head = render.tag("p", f"Task {task['task_id']} · {task['status']}"
                      + (f" · 닫힘({task['closed_reason']})" if task.get("closed_reason") else ""))
    run_rows = [render.tag("tr", render.html_join([
        render.tag("td", str(run["run_id"])), render.tag("td", str(run["status"])),
        render.tag("td", str(run["exit_code"])), render.tag("td", when(run["started_at"])),
        render.tag("td", when(run["ended_at"])), render.tag("td", _raw_link(rid, run["run_id"]))]))
        for run in task["runs"]]
    runs = render.tag("table", render.html_join([render.tag("tr", render.html_join([
        render.tag("th", name) for name in ("run", "상태", "exit", "시작", "끝", "원문")])), *run_rows])) \
        if run_rows else render.tag("p", "run이 없습니다", class_="none")
    command_rows = [render.tag("tr", render.html_join([
        render.tag("td", str(command["command_id"])), render.tag("td", str(command["owner"])),
        render.tag("td", str(command["exit_code"])), render.tag("td", _raw_link(rid, command["command_id"]))]))
        for command in task["commands"]]
    commands = render.tag("table", render.html_join([render.tag("tr", render.html_join([
        render.tag("th", name) for name in ("명령", "주체", "exit", "출력")])), *command_rows])) \
        if command_rows else render.tag("p", "명령이 없습니다", class_="none")
    reports = [render.tag("li", render.html_join([render.tag("div", f"[{report['kind']}] {when(report['at'])}",
                                                             class_="meta"), render.pre(report["text"])]))
               for report in task["reports"]]
    return render.html_join([
        render.tag("section", render.html_join([render.tag("h2", "실행 기록"), head, runs]), id="runs"),
        render.tag("section", render.html_join([render.tag("h2", "명령"), commands]), id="commands"),
        render.tag("section", render.html_join([render.tag("h2", "worker 보고 원문"),
                                                render.tag("ol", render.html_join(reports)) if reports else
                                                render.tag("p", "보고가 없습니다", class_="none")]), id="reports")])


def full_page(request: web.Request, value: dict, rid: str) -> web.Response:
    detail_link = render.tag("p", render.tag("a", "요청으로 돌아가기", href=request_url(rid)))
    text = value["summary"]
    artifact_form = render.tag("form", render.html_join([
        _field_row("산출물 경로(프로젝트 root 기준)", render.void("input", name="path", required=True)),
        _field_row("offset", render.void("input", name="offset", value="0", inputmode="numeric")),
        _field_row("length", render.void("input", name="length", value=str(MAX_RESULT_PAGE), inputmode="numeric")),
        render.tag("button", "열기", type="submit")]), method="get", action=request_url(rid, "/artifact"),
        class_="stack")
    body = [render.tag("h1", "전체 보기"), detail_link,
            render.tag("section", render.html_join([render.tag("h2", "결과 요약"),
                                                    render.pre(text) if text else render.tag("p", EMPTY, class_="none")]),
                       id="result"),
            _full_task_section(rid, value["full"]),
            render.tag("section", render.html_join([
                render.tag("h2", "산출물"),
                render.tag("p", "프로젝트 root 아래 일반 파일만 열립니다(.wb·.git, symlink 제외). 한 번에 최대 128 KiB."),
                artifact_form]), id="artifact")]
    return _frame(request, "전체 보기", render.html_join(body))


async def get_full(request: web.Request) -> web.Response:
    rid = request.match_info["rid"]
    try:
        value = await _result_call(request, request.app[PORT].results.full, principal_of(request), rid)
    except InboxError as exc:
        return error_page(request, exc)
    return full_page(request, value, rid)


async def get_raw(request: web.Request) -> web.Response:
    rid = request.match_info["rid"]
    try:
        offset, length = _page_query(request)
        page = await _result_call(request, request.app[PORT].results.raw, principal_of(request), rid,
                                  request.query.get("run_id", ""), offset, length)
    except InboxError as exc:
        return _result_error(request, exc, rid)
    return _page_response(page)


async def get_artifact(request: web.Request) -> web.Response:
    rid = request.match_info["rid"]
    try:
        offset, length = _page_query(request)
        page = await _result_call(request, request.app[PORT].results.artifact, principal_of(request), rid,
                                  request.query.get("path", ""), offset, length)
    except InboxError as exc:
        return _result_error(request, exc, rid)
    return _page_response(page)


@api
async def get_badge(request: web.Request) -> web.Response:
    """``{pending: NEW 수, needs_info: NEEDS_INFO 수}`` over everything the session may see (D-24)."""
    return web.json_response(await _call(request, request.app[PORT].inbox.badge, principal_of(request)))


async def _close_reads(app: web.Application) -> None:
    """App cleanup (listener stop, Hub stop): the result-read pool's queued reads are cancelled, no wait."""
    app[PORT].reads.shutdown(wait=False, cancel_futures=True)


def routes(inbox, projects: Callable[[], Iterable[ProjectRef]], *, results=None) -> Callable[[web.Application], None]:
    """``create_app(..., routes=[routes(inbox, projects)])``: the request pages over ``inbox``.

    ``projects()`` returns the attached :class:`ProjectRef` list (names and the create form's choices).
    ``results`` reads results (default: :class:`~workbench.hub.inbox.results.Results` over ``inbox`` and the
    project backends).
    """
    def register(app: web.Application) -> None:
        app[PORT] = RequestsPort(inbox, projects, results=results if results is not None else Results(inbox))
        app.on_cleanup.append(_close_reads)
        app.router.add_get("/requests", get_list)
        app.router.add_get("/api/badge", get_badge)
        app.router.add_get("/requests/new", get_new)
        app.router.add_post("/requests", post_create)
        app.router.add_get("/requests/{rid}", get_detail)
        app.router.add_get("/requests/{rid}/edit", get_edit)
        app.router.add_post("/requests/{rid}/edit", post_edit)
        app.router.add_post("/requests/{rid}/comments", post_comment)
        app.router.add_post("/requests/{rid}/state", post_state)
        app.router.add_get("/requests/{rid}/cancel", get_cancel)
        app.router.add_post("/requests/{rid}/cancel", post_cancel)
        app.router.add_post("/requests/{rid}/start-review", post_start_review)
        app.router.add_get("/requests/{rid}/full", get_full)
        app.router.add_get("/requests/{rid}/raw", get_raw)
        app.router.add_get("/requests/{rid}/artifact", get_artifact)
    return register
