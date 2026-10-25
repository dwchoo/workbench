"""Project pages, control confirm pages and the review mode form (TI-12; SPEC "웹 route와 화면",
"프로젝트 제어 실행").

Every control goes through a server-rendered confirm page (``GET``, nothing changes) and then a ``POST`` with the
CSRF field (checked by the session middleware). The control itself is :class:`~workbench.hub.web_control.ProjectControl`
(``request.app[control.CONTROL]``); this module only draws pages. Backend details, paths and driver output never
reach a page: a failed control shows a fixed text for its ``reason`` code.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from aiohttp import web

from ... import presentation, projects as project_library, web_control
from ...inbox.limits import InboxError
from .. import render
from ..middleware import SERVICES, principal_of, see_other, session_of
from . import board_status, requests as request_pages
from .board_status import REGISTRY_TEXT, PORT, fetch, frame, project_url, review_mode_text
from .control import CONTROL

CONTROL_LABELS = {"power-on": "켜기", "power-off": "끄기", "pause": "일시정지", "resume": "재개",
                  "confirm-boot": "재부팅 확인"}
UNCONTROLLABLE_TEXT = {
    "unknown": "연결 없음/불명 상태라 지금은 제어할 수 없습니다",
    "mismatch": "등록 정보가 맞지 않아 제어할 수 없습니다. 프로젝트 폴더를 확인하세요",
    "starting": "시작하는 중에는 제어할 수 없습니다",
    "closing": "끄는 중에는 제어할 수 없습니다",
}
BUSY_TEXT = "이 프로젝트의 다른 제어가 진행 중입니다"


def wait_codes(row: dict) -> set[str]:
    return {wait.get("code") for wait in row.get("waits") or () if isinstance(wait, dict)}


def available_controls(row: dict) -> list[str]:
    """The control actions that fit this row's state (SPEC: only the buttons that make sense)."""
    power, waits = row.get("power"), wait_codes(row)
    if power == "off":
        return ["power-on"]
    if power != "on":
        return []
    controls = ["power-off"]
    if "boot_confirmation_required" in waits:
        controls.append("confirm-boot")
    if "paused" in waits:
        controls.append("resume")
    elif "boot_confirmation_required" not in waits:
        controls.append("pause")
    return controls


async def find_row(request: web.Request, project_id: str, *, fresh: bool = True) -> tuple[dict | None, str | None]:
    rows, problem = await fetch(request, fresh=fresh)
    return next((row for row in rows if row.get("project_id") == project_id), None), problem


def not_found(request: web.Request) -> web.Response:
    body = render.html_join([render.tag("h1", "프로젝트를 찾을 수 없습니다"),
                             render.tag("p", render.tag("a", "프로젝트 상태", href="/"))])
    return frame(request, "오류", body, status=404)


def _fact(label: str, value: object) -> render.Markup:
    return render.html_join([render.tag("dt", label), render.tag("dd", value)])


def _busy(request: web.Request, project_id: str) -> bool:
    return request.app[CONTROL].in_progress(project_id)


async def current_review_mode(request: web.Request, project_id: str, listed: object) -> object:
    """The stored review mode (the inbox is the source of truth), else the Hub list's value (None when unknown)."""
    port, principal = request.app[PORT], principal_of(request)
    try:
        return (await request.app[SERVICES].blocking(lambda: port.inbox.review_mode(principal, project_id)))["review_mode"]
    except InboxError:
        return listed


def review_mode_form(request: web.Request, project_id: str, mode: object) -> render.Markup:
    options = [render.tag("option", label, value=value, selected=value == mode)
               for value, label in board_status.REVIEW_MODE_LABELS.items()]
    return render.tag("section", render.html_join([
        render.tag("h2", "검토 방식"),
        render.tag("p", "자동: 새 요청을 바로 manager에게 알립니다. 수동: 요청 화면에서 '검토 시작'을 눌러야 알립니다."),
        render.tag("form", render.html_join([
            render.csrf_field(session_of(request).csrf),
            render.tag("label", render.html_join(["검토 방식 ", render.tag("select", render.html_join(options),
                                                                        name="mode")])),
            render.tag("button", "저장", type="submit")]),
            method="post", action=project_url(project_id, "/review-mode"), class_="inline")]), id="review-mode")


async def post_review_mode(request: web.Request) -> web.Response:
    project_id, port = request.match_info["project_id"], request.app[PORT]
    form = await request.post()
    mode = form.get("mode")
    principal = principal_of(request)
    try:
        await request.app[SERVICES].blocking(
            lambda: port.inbox.review_mode(principal, project_id, mode if isinstance(mode, str) else ""))
    except InboxError as exc:
        return request_pages.error_page(request, exc, back=project_url(project_id))
    return see_other(f"{project_url(project_id)}?notice=review_mode")


async def get_project(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    row, problem = await find_row(request, project_id)
    if row is None and problem is None:
        return not_found(request)
    body = []
    if row is None:
        body += [render.tag("h1", "프로젝트"), render.tag("p", problem, class_="error", role="alert")]
        return frame(request, "프로젝트", render.html_join(body), status=200)
    notice = NOTICES.get(request.query.get("notice", ""))
    if notice:
        body.append(render.tag("p", notice, class_="notice", role="status"))
    now = float(request.app[SERVICES].clock())
    cells = dict(zip(presentation.COLUMNS, presentation.row_cells(row, now)))
    mode = await current_review_mode(request, project_id, row.get("review_mode"))
    facts = [_fact("이름", cells["이름"]), _fact("경로", cells["경로"]), _fact("상태", cells["상태"]),
             _fact("Task·단계", cells["Task·단계"]), _fact("명령", cells["명령"]), _fact("대기 사유", cells["대기 사유"]),
             _fact("요청", cells["요청"]), _fact("마지막 활동", cells["마지막 활동"]),
             _fact("검토 방식", review_mode_text(mode))]
    body.append(render.tag("h1", cells["이름"]))
    body.append(render.tag("section", render.tag("dl", render.html_join(facts)), id="status"))
    controls = available_controls(row)
    if _busy(request, project_id):
        body.append(render.tag("p", BUSY_TEXT, class_="notice", role="status"))
    elif controls:
        body.append(render.tag("div", render.html_join(
            render.tag("a", CONTROL_LABELS[action], href=project_url(project_id, f"/{action}"), class_="button")
            for action in controls), class_="actions", id="controls"))
    elif row.get("power") in UNCONTROLLABLE_TEXT:
        body.append(render.tag("p", UNCONTROLLABLE_TEXT[row["power"]], class_="notice", role="status"))
    body.append(review_mode_form(request, project_id, mode))
    body.append(render.tag("p", render.tag("a", "이 프로젝트의 요청", href=f"/requests?project={project_id}")))
    body.append(render.tag("p", render.tag("a", "프로젝트 상태", href="/")))
    return frame(request, cells["이름"], render.html_join(body))


NOTICES = {"power_on": "프로젝트를 켰습니다", "power_on_already": "프로젝트가 이미 켜져 있습니다",
           "power_off": "프로젝트가 꺼졌습니다(종료 결과를 확인했습니다)", "pause": "일시정지했습니다", "resume": "재개했습니다",
           "confirm_boot": "재부팅을 확인했습니다", "review_mode": "검토 방식을 바꿨습니다"}
DESCRIPTIONS = {
    "power-on": "프로젝트를 켭니다. backend와 두 OMP session, host shell이 시작됩니다.",
    "power-off": "프로젝트를 끕니다. backend와 두 OMP session, host shell이 끝나고 아래 진행 중인 작업이 중단됩니다.",
    "pause": "프로젝트를 일시정지합니다. 새 알림과 자동 진행이 멈춥니다.",
    "resume": "일시정지를 풀고 보류된 알림과 진행을 다시 시작합니다.",
    "confirm-boot": "재부팅 뒤 보류된 상태를 확인하고 진행을 허용합니다.",
}
ACTIVE_KINDS = {"shell_jobs": "host shell에서 실행 중인 프로세스", "shell_unknown": "host shell 작업(확인 불가)",
                "shell_request": "host shell 요청 처리 중", "shell_foreground": "host shell 앞에서 사람이 쓰는 프로그램",
                "omp_turn": "OMP 작업 중", "automation": "자동화 진행 중"}
ACTIVE_FIELDS = ("role", "state", "phase", "status", "task_id", "run_id")
# reason code -> (fixed text, HTTP status). Backend detail strings never reach a page (paths, sockets, output).
REASON_TEXT = {
    web_control.FORBIDDEN: ("소유자 세션만 프로젝트를 제어할 수 있습니다", 403),
    web_control.UNKNOWN_PROJECT: ("프로젝트를 찾을 수 없습니다", 404),
    web_control.CONTROL_IN_PROGRESS: (BUSY_TEXT, 409),
    web_control.CONTROL_BUSY: ("제어 작업이 모두 사용 중입니다. 잠시 뒤 다시 시도하세요", 503),
    web_control.CONFIRM_TOKEN_INVALID: ("확인 화면이 만료되었거나 이미 사용되었습니다. 확인 화면을 다시 여세요", 409),
    web_control.INTERNAL_ERROR: ("제어하지 못했습니다. 프로젝트 상태를 확인하세요", 500),
    web_control.REGISTRY_INVALID: (REGISTRY_TEXT, 503),
    project_library.MISMATCH: ("등록 정보가 맞지 않아 제어할 수 없습니다. 프로젝트 폴더를 확인하세요", 200),
    project_library.NOT_RUNNING: ("프로젝트가 켜져 있지 않습니다", 200),
    project_library.UNREACHABLE: ("프로젝트에 연결하지 못했습니다. 아무것도 바뀌지 않았습니다", 200),
    project_library.START_FAILED: ("프로젝트를 켜지 못했습니다. 상태를 확인한 뒤 다시 시도하세요", 200),
    project_library.START_REQUIREMENT: ("시작에 필요한 조건이 맞지 않아 켜지 못했습니다", 200),
    project_library.WORK_KEPT_CHANGING: (project_library.WORK_KEPT_CHANGING_TEXT, 200),
    project_library.NO_RESULT: ("끄기를 요청했지만 종료 결과를 확인하지 못했습니다. 프로젝트 상태를 확인하세요", 200),
    project_library.UNVERIFIED: ("종료가 확인되지 않았습니다. 프로젝트 상태를 확인하세요", 200),
    project_library.BACKEND_TOO_OLD: ("이 backend는 이 제어를 지원하지 않습니다. 프로젝트를 다시 시작하세요", 200),
    project_library.BOOT_ID_UNAVAILABLE: ("재부팅 정보를 읽지 못했습니다. 잠시 뒤 다시 시도하세요", 200),
    "boot_confirmation_not_required": ("재부팅 확인이 필요하지 않습니다", 200),
    "boot_id_mismatch": ("재부팅 정보가 바뀌었습니다. 프로젝트 화면을 다시 여세요", 200),
}
WORK_CHANGED_NOTICE = f"{project_library.WORK_CHANGED_TEXT}. {project_library.RE_ASK_TEXT} 아무것도 끄지 않았습니다."
GENERIC_FAILURE =("처리하지 못했습니다. 프로젝트 상태를 확인하세요", 200)
METHODS = {"power-on": "power_on", "pause": "pause", "resume": "resume", "confirm-boot": "confirm_boot"}


def _scalar(value: object) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _active_item(item: object) -> render.Markup:
    """One ``active`` entry as text: the kind label, a few safe fields and each process's pid and command."""
    if not isinstance(item, dict):
        return render.tag("li", "진행 중인 작업")
    parts = [ACTIVE_KINDS.get(item.get("kind"), "진행 중인 작업")]
    parts += [f"{key} {str(item[key])[:60]}" for key in ACTIVE_FIELDS if _scalar(item.get(key))]
    for process in item.get("processes") or ():
        if isinstance(process, dict) and _scalar(process.get("pid")):
            command = f" {str(process['command'])[:120]}" if _scalar(process.get("command")) else ""
            parts.append(f"pid {process['pid']}{command}")
    return render.tag("li", " · ".join(parts))


def confirm_page(request: web.Request, project_id: str, row: dict, action: str, *, token: str | None = None,
                 active: list | None = None, notice: str | None = None) -> web.Response:
    csrf = session_of(request).csrf
    name = presentation.row_cells(row, 0.0)[0]
    body = [render.tag("h1", f"{CONTROL_LABELS[action]} 확인"),
            render.tag("p", render.html_join([render.tag("strong", name), " · ", DESCRIPTIONS[action]]))]
    if notice:
        body.append(render.tag("p", notice, class_="notice", role="status"))
    if action == "power-off":
        items = [_active_item(item) for item in active or ()]
        body.append(render.tag("section", render.html_join([
            render.tag("h2", "진행 중인 작업"),
            render.tag("ul", render.html_join(items)) if items else render.tag("p", "진행 중인 작업이 없습니다")]),
            id="active"))
    fields = [render.csrf_field(csrf)]
    if token:
        fields.append(render.hidden("token", token))
    fields.append(render.tag("button", "확인", type="submit"))
    body.append(render.tag("form", render.html_join(fields), method="post",
                           action=project_url(project_id, f"/{action}"), class_="inline"))
    body.append(render.tag("p", render.tag("a", "취소", href=project_url(project_id))))
    return frame(request, f"{CONTROL_LABELS[action]} 확인", render.html_join(body))


def result_page(request: web.Request, project_id: str, action: str, result: web_control.Result) -> web.Response:
    text, status = REASON_TEXT.get(result.reason, GENERIC_FAILURE)
    body = [render.tag("h1", f"{CONTROL_LABELS.get(action, '제어')} 결과"),
            render.tag("p", text, class_="error", role="alert")]
    if result.reason == web_control.CONFIRM_TOKEN_INVALID:
        body.append(render.tag("p", render.tag("a", "확인 화면 다시 열기", href=project_url(project_id, f"/{action}"))))
    body.append(render.tag("p", render.tag("a", "프로젝트로 돌아가기", href=project_url(project_id))))
    return frame(request, "제어 결과", render.html_join(body), status=status)


def _caller(request: web.Request) -> tuple:
    session = session_of(request)
    return principal_of(request), session.token_hash if session is not None else "", request.remote


async def _power_off_request(request: web.Request, project_id: str) -> web_control.Result:
    control = request.app[CONTROL]
    principal, session_id, address = _caller(request)
    return await control.run("power_off", project_id, lambda: control.power_off_request(
        principal, project_id, session=session_id, client_addr=address), client_addr=address)


async def get_confirm(request: web.Request) -> web.Response:
    project_id, action = request.match_info["project_id"], request.match_info["action"]
    if action not in CONTROL_LABELS:
        return not_found(request)
    row, problem = await find_row(request, project_id)
    if row is None:
        return frame(request, "오류", render.tag("p", problem, class_="error", role="alert")) if problem \
            else not_found(request)
    if _busy(request, project_id):
        return result_page(request, project_id, action,
                           web_control.Result(False, action, project_id, web_control.CONTROL_IN_PROGRESS))
    if action not in available_controls(row):
        body = render.html_join([render.tag("h1", "지금은 할 수 없는 제어입니다"),
                                 render.tag("p", "프로젝트 상태가 바뀌었습니다. 화면을 다시 확인하세요"),
                                 render.tag("p", render.tag("a", "프로젝트로 돌아가기", href=project_url(project_id)))])
        return frame(request, "오류", body, status=409)
    if action != "power-off":
        return confirm_page(request, project_id, row, action)
    result = await _power_off_request(request, project_id)
    if result.reason != web_control.CONFIRM_REQUIRED:
        return result_page(request, project_id, action, result)
    return confirm_page(request, project_id, row, action, token=result.data.get("token"),
                        active=result.data.get("active"))


def _success_notice(action: str, result: web_control.Result) -> str | None:
    """The notice key of a finished control, or None when it must not read as success (C-AC-22: a power off
    counts only when the backend's own result says ``verified``)."""
    if action == "power-off":
        outcome = result.data.get("result")
        return "power_off" if isinstance(outcome, dict) and outcome.get("verified") is True else None
    if action == "power-on" and result.data.get("already_running") is True:
        return "power_on_already"
    return action.replace("-", "_")


async def post_control(request: web.Request) -> web.Response:
    project_id, action = request.match_info["project_id"], request.match_info["action"]
    if action not in CONTROL_LABELS:
        return not_found(request)
    control = request.app[CONTROL]
    principal, session_id, address = _caller(request)
    if action == "power-off":
        form = await request.post()
        token = form.get("token")
        if not isinstance(token, str) or not token:
            return result_page(request, project_id, action,
                               web_control.Result(False, "power_off", project_id, web_control.CONFIRM_TOKEN_INVALID))
        result = await control.run("power_off", project_id, lambda: control.power_off_confirm(
            principal, project_id, token, session=session_id, client_addr=address), client_addr=address)
    else:
        name = METHODS[action]
        method = getattr(control, name)
        result = await control.run(name, project_id, lambda: method(principal, project_id, client_addr=address),
                                   client_addr=address)
    if result.ok:
        notice = _success_notice(action, result)
        if notice is None:
            result = web_control.Result(False, result.action, project_id, project_library.UNVERIFIED)
        else:
            return see_other(f"{project_url(project_id)}?notice={notice}")
    if result.reason == web_control.WORK_CHANGED and isinstance(result.data.get("token"), str):
        row, _problem = await find_row(request, project_id)
        if row is not None:  # HB-AC-09: the work changed since the page was shown; ask again with the new list
            return confirm_page(request, project_id, row, action, token=result.data["token"],
                                active=result.data.get("active"), notice=WORK_CHANGED_NOTICE)
    return result_page(request, project_id, action, result)


def routes(rows: Callable[..., Awaitable[dict]], inbox) -> Callable[[web.Application], None]:
    """``create_app(..., routes=[routes(lister.list, inbox)])``: the status board and the project pages."""
    def register(app: web.Application) -> None:
        board_status.register(app, board_status.StatusPort(rows, inbox))
        app.router.add_get("/projects/{project_id}", get_project)
        app.router.add_get("/projects/{project_id}/{action}", get_confirm)
        app.router.add_post("/projects/{project_id}/review-mode", post_review_mode)
        app.router.add_post("/projects/{project_id}/{action}", post_control)
    return register
