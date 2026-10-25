"""Settings sub-pages: API keys and the audit log (Task Inbox SPEC "웹 route와 화면", "API key",
"감사 기록"). ``GET /settings`` and ``POST /settings/password`` live in :mod:`.login`.

The owner is the only user of these pages, so every handler needs a session (the default) and
state changes are ``POST`` + CSRF (checked by the session middleware). The raw API key exists
only in the body of the one ``POST /settings/keys`` response that issues it: it is never put in
a redirect, header, audit row or log line, and the list page shows only the key id.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from typing import Callable, Iterable

from aiohttp import web

from ... import auth
from .. import render
from ..middleware import SERVICES, see_other, session_of

AUDIT_PAGE_SIZE = 50
KEY_ID_ALPHABET = frozenset(auth.KEY_ID_ALPHABET)
SYSTEM_KEY_LABEL = "Board agent(시스템)"
GRADE_LABELS = {"requester": "requester (요청 작성)", "editor": "editor (요청 편집)"}
REJECTED_TEXT = {
    "name_invalid": "이름은 1~64자이고 영문·숫자·공백·점·밑줄·하이픈만 쓸 수 있습니다",
    "name_reserved": "이 이름은 시스템이 쓰는 이름입니다",
    "name_taken": "같은 이름의 key가 이미 있습니다",
    "grade_invalid": "등급을 고르세요",
    "scope_invalid": "범위를 고르세요. 선택 범위는 등록된 프로젝트를 1개 이상 골라야 합니다",
    "expiry_invalid": "만료는 비우거나 1~3650일로 적으세요",
}

Projects = Callable[[], Iterable[tuple[str, str]]]


def settings_nav() -> render.Markup:
    return render.tag("nav", render.html_join([
        render.tag("a", "비밀번호", href="/settings"), " ",
        render.tag("a", "API key", href="/settings/keys"), " ",
        render.tag("a", "감사 기록", href="/settings/audit")]), class_="subnav")


def when(timestamp: float | None) -> str:
    if timestamp is None:
        return "-"
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def scope_text(scope: dict) -> str:
    return "전체" if scope.get("all") else ", ".join(scope.get("projects", ()))


def status_text(info: auth.ApiKeyInfo, now: float) -> str:
    if info.revoked_at is not None:
        return "폐기됨"
    if info.expires_at is not None and not now < info.expires_at:
        return "만료됨"
    return "사용 중"


def _page(request: web.Request, title: str, body: object, *, status: int = 200) -> web.Response:
    services, session = request.app[SERVICES], session_of(request)
    parts = [render.tag("h1", "설정"), settings_nav(), body]
    return render.page(title, render.html_join(parts), status=status,
                       insecure_warning=services.insecure_warning, csrf=session.csrf)


def _form_text(form, name: str) -> str:
    value = form.get(name, "")
    return value if isinstance(value, str) else ""


class KeyPages:
    """Handlers bound to a registry view (``projects()`` -> ``(project_id, name)`` pairs)."""

    def __init__(self, projects: Projects):
        self.projects = projects

    async def _registry(self, services) -> list[tuple[str, str]]:
        """The registered ``(project_id, name)`` pairs; the provider reads files, so off the loop."""
        found = await services.blocking(lambda: list(self.projects()))
        return [(str(project_id), str(name)) for project_id, name in found]

    # --- list and issue form ----------------------------------------------------------------

    def _key_rows(self, keys: list, now: float) -> render.Markup:
        head = render.tag("tr", render.html_join([render.tag("th", title) for title in (
            "이름", "key id", "등급", "범위", "마지막 사용", "만료", "상태", "")]))
        rows = [head]
        for info in keys:
            action: object = ""
            if not info.system and info.revoked_at is None:
                action = render.tag("a", "폐기", href=f"/settings/keys/{info.key_id}/revoke")
            rows.append(render.tag("tr", render.html_join([render.tag("td", cell) for cell in (
                SYSTEM_KEY_LABEL if info.system else info.name, info.key_id, info.grade,
                scope_text(info.scope), when(info.last_used_at),
                when(info.expires_at) if info.expires_at is not None else "없음",
                status_text(info, now), action)])))
        return render.tag("table", render.html_join(rows), class_="keys")

    def _issue_form(self, csrf: str, registry: list, *, error: str | None = None) -> render.Markup:
        parts = [render.tag("h2", "API key 발급")]
        if error:
            parts.append(render.tag("p", error, class_="error", role="alert"))
        projects = [render.tag("label", render.html_join([
            render.void("input", type="checkbox", name="project", value=project_id), f" {name} ({project_id})"]))
            for project_id, name in registry]
        parts.append(render.tag("form", render.html_join([
            render.csrf_field(csrf),
            render.tag("label", render.html_join(["이름 ", render.void(
                "input", type="text", name="name", maxlength=auth.KEY_NAME_MAX, required=True)])),
            render.tag("label", render.html_join(["등급 ", render.tag("select", render.html_join([
                render.tag("option", label, value=grade) for grade, label in GRADE_LABELS.items()]),
                name="grade")])),
            render.tag("fieldset", render.html_join([
                render.tag("legend", "범위"),
                render.tag("label", render.html_join([render.void(
                    "input", type="radio", name="scope", value="all", checked=True), " 전체 프로젝트"])),
                render.tag("label", render.html_join([render.void(
                    "input", type="radio", name="scope", value="projects"), " 선택한 프로젝트"])),
                *projects])),
            render.tag("label", render.html_join(["만료(일, 비우면 없음) ", render.void(
                "input", type="number", name="expires_days", min=1, max=auth.KEY_EXPIRY_DAYS_MAX)])),
            render.tag("button", "발급", type="submit")]), method="post", action="/settings/keys", class_="stack"))
        return render.html_join(parts)

    async def _keys_page(self, request: web.Request, *, status: int = 200, error: str | None = None) -> web.Response:
        services = request.app[SERVICES]
        keys = await services.blocking(services.api_keys.list)
        registry = await self._registry(services)
        body = render.html_join([render.tag("h2", "API key"), self._key_rows(keys, float(services.clock())),
                                 self._issue_form(session_of(request).csrf, registry, error=error)])
        return _page(request, "API key", body, status=status)

    async def get_keys(self, request: web.Request) -> web.Response:
        return await self._keys_page(request)

    # --- issue ------------------------------------------------------------------------------

    @staticmethod
    def _scope_of(form, known: set) -> dict:
        choice = _form_text(form, "scope")
        if choice == "all":
            return {"all": True}
        if choice == "projects":
            chosen = [value for value in form.getall("project", []) if isinstance(value, str)]
            if chosen and len(set(chosen)) == len(chosen) and all(item in known for item in chosen):
                return {"projects": chosen}
        raise auth.ApiKeyRejected("scope_invalid")

    @staticmethod
    def _expiry_of(form) -> int | None:
        text = _form_text(form, "expires_days").strip()
        if not text:
            return None
        if not (text.isascii() and text.isdigit()) or len(text) > 6:
            raise auth.ApiKeyRejected("expiry_invalid")
        return int(text)

    async def post_keys(self, request: web.Request) -> web.Response:
        services = request.app[SERVICES]
        form = await request.post()
        now = float(services.clock())
        try:
            known = {project_id for project_id, _ in await self._registry(services)}
            scope, expires_days = self._scope_of(form, known), self._expiry_of(form)
            issued = await services.blocking(lambda: services.api_keys.issue(
                _form_text(form, "name"), _form_text(form, "grade"), scope, expires_days, now=now))
        except auth.ApiKeyRejected as rejected:
            return await self._keys_page(request, status=400, error=REJECTED_TEXT.get(rejected.reason, rejected.reason))
        info = next(item for item in await services.blocking(services.api_keys.list) if item.key_id == issued.key_id)
        detail = {"key_id": info.key_id, "name": info.name, "grade": info.grade, "scope": info.scope,
                  "expires_at": info.expires_at}
        audited = await self._audit(services, "api_key_issued", request.remote, detail, now)
        warning = [] if audited else [render.tag(
            "p", "감사 기록을 남기지 못했습니다. key는 발급되었습니다. 원인은 Hub log를 보세요.", class_="error", role="alert")]
        body = render.html_join([
            render.tag("h2", "API key를 발급했습니다"),
            *warning,
            render.tag("p", "이 key는 지금 한 번만 보입니다. 지금 복사해 안전한 곳에 두세요.", class_="notice"),
            render.tag("p", render.tag("code", issued.raw, class_="raw-key")),
            render.tag("p", render.tag("a", "API key 목록으로", href="/settings/keys"))])
        return _page(request, "API key 발급", body)

    @staticmethod
    async def _audit(services, event: str, address: str | None, detail: dict, now: float) -> bool:
        """Record the audit row after the key change already happened. A failure is logged (event and
        error type only, never key material) and reported, not raised: the owner must still get
        the one-time raw key, and a revoked key stays revoked."""
        try:
            await services.blocking(lambda: services.audit.record(event, "owner", address, detail, now=now))
        except Exception as exc:  # noqa: BLE001 - sqlite3, HubDbError, AuditRejected, ...
            print(f"[workbench] audit write failed for {event}: {type(exc).__name__}", file=sys.stderr, flush=True)
            return False
        return True

    # --- revoke -----------------------------------------------------------------------------

    @staticmethod
    def _plain(status: int, text: str) -> web.Response:
        return web.Response(status=status, text=text)

    async def _target(self, request: web.Request):
        """(key info, ``None``) or (``None``, error response) for ``{key_id}`` of the path."""
        key_id = request.match_info["key_id"]
        services = request.app[SERVICES]
        if len(key_id) != auth.KEY_ID_LENGTH or not set(key_id) <= KEY_ID_ALPHABET:
            return None, self._plain(404, "not_found")
        info = next((item for item in await services.blocking(services.api_keys.list)
                     if item.key_id == key_id), None)
        if info is None:
            return None, self._plain(404, "not_found")
        if info.system:
            return None, self._plain(403, "system_key")
        if info.revoked_at is not None:
            return None, self._plain(409, "already_revoked")
        return info, None

    async def get_revoke(self, request: web.Request) -> web.Response:
        info, refusal = await self._target(request)
        if refusal is not None:
            return refusal
        csrf = session_of(request).csrf
        body = render.html_join([
            render.tag("h2", "API key 폐기"),
            render.tag("p", f"'{info.name}' ({info.key_id}) key를 폐기합니다. 이 key로 하는 다음 호출부터 401이 됩니다."),
            render.tag("form", render.html_join([
                render.csrf_field(csrf), render.tag("button", "폐기", type="submit")]),
                method="post", action=f"/settings/keys/{info.key_id}/revoke"),
            render.tag("p", render.tag("a", "취소", href="/settings/keys"))])
        return _page(request, "API key 폐기", body)

    async def post_revoke(self, request: web.Request) -> web.Response:
        services = request.app[SERVICES]
        info, refusal = await self._target(request)
        if refusal is not None:
            return refusal
        now = float(services.clock())
        try:
            await services.blocking(lambda: services.api_keys.revoke(info.key_id, now=now))
        except auth.ApiKeyRejected as rejected:  # raced with another revoke or a system rotation
            status = {"not_found": 404, "system_key": 403}.get(rejected.reason, 409)
            return self._plain(status, rejected.reason)
        detail = {"key_id": info.key_id, "name": info.name, "grade": info.grade, "scope": info.scope,
                  "expires_at": info.expires_at}
        await self._audit(services, "api_key_revoked", request.remote, detail, now)
        return see_other("/settings/keys")


def audit_detail(detail: dict) -> str:
    return ", ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in sorted(detail.items()))


async def get_audit(request: web.Request) -> web.Response:
    services = request.app[SERVICES]
    page = request.query.get("page", "1")
    number = int(page) if page.isascii() and page.isdigit() and len(page) < 10 else 1  # plain digits only
    result = await services.blocking(lambda: services.audit.list_audit(number, page_size=AUDIT_PAGE_SIZE))
    head = render.tag("tr", render.html_join([render.tag("th", title) for title in (
        "시각", "event", "주체", "주소", "detail")]))
    rows = [head] + [render.tag("tr", render.html_join([render.tag("td", cell) for cell in (
        when(entry.at), entry.event, entry.actor, entry.client_addr or "-", audit_detail(entry.detail))]))
        for entry in result.entries]
    last = max(1, -(-result.total // AUDIT_PAGE_SIZE))
    links = []
    if result.page > 1:
        links.append(render.tag("a", "이전", href=f"/settings/audit?page={min(result.page - 1, last)}"))
    if result.page < last:
        links.append(render.tag("a", "다음", href=f"/settings/audit?page={result.page + 1}"))
    body = render.html_join([render.tag("h2", f"감사 기록 ({result.total}건)"),
                             render.tag("table", render.html_join(rows), class_="audit"),
                             render.tag("p", render.html_join(links), class_="pager")])
    return _page(request, "감사 기록", body)


def make_register(projects: Projects) -> Callable[[web.Application], None]:
    """Route registrar for ``create_app(routes=[...])``; ``projects`` is the Hub registry view."""
    pages = KeyPages(projects)

    def register(app: web.Application) -> None:
        app.router.add_get("/settings/keys", pages.get_keys)
        app.router.add_post("/settings/keys", pages.post_keys)
        app.router.add_get("/settings/keys/{key_id}/revoke", pages.get_revoke)
        app.router.add_post("/settings/keys/{key_id}/revoke", pages.post_revoke)
        app.router.add_get("/settings/audit", get_audit)
    return register
