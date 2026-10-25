"""Minimal JSON control endpoints (TI-08): ``POST /api/projects/<project_id>/<action>``.

Session + CSRF only (the middleware: no session -> 401, a wrong CSRF -> 403; an API key Bearer is
never read here). The pages and confirm screens are TI-12's; they call the same
:class:`workbench.hub.web_control.ProjectControl` (``request.app[CONTROL]``).

Actions: ``power-on``, ``power-off`` (the first step: ``confirm_required`` with ``data.token`` and
``data.active``), ``power-off-confirm`` (form field ``token``), ``pause``, ``resume``,
``confirm-boot``. The reply is ``Result.as_dict()``; HTTP 200 for every finished call (ok or not),
404 ``unknown_project``/unknown action, 409 ``control_in_progress``/``confirm_token_invalid``,
503 ``control_busy`` (every control worker is busy). Calls run on the control's own workers.
"""
from __future__ import annotations

from aiohttp import web

from ... import web_control
from ..middleware import SERVICES, api, principal_of, session_of

CONTROL: web.AppKey = web.AppKey("project_control", web_control.ProjectControl)
STATUS = {web_control.UNKNOWN_PROJECT: 404, web_control.CONTROL_IN_PROGRESS: 409,
          web_control.CONFIRM_TOKEN_INVALID: 409, web_control.CONTROL_BUSY: 503}


@api
async def post_control(request: web.Request) -> web.Response:
    control = request.app[CONTROL]
    project_id, action = request.match_info["project_id"], request.match_info["action"]
    principal, session = principal_of(request), session_of(request)
    session_id = session.token_hash if session is not None else ""
    address = request.remote
    if action == "power-off-confirm":
        form = await request.post()
        token = form.get("token")
        result = await control.run("power_off", project_id, lambda: control.power_off_confirm(
            principal, project_id, token if isinstance(token, str) else "", session=session_id,
            client_addr=address))
    elif action == "power-off":
        result = await control.run("power_off", project_id, lambda: control.power_off_request(
            principal, project_id, session=session_id, client_addr=address))
    elif action in ("power-on", "pause", "resume", "confirm-boot"):
        name = action.replace("-", "_")
        method = getattr(control, name)
        result = await control.run(name, project_id, lambda: method(principal, project_id, client_addr=address))
    else:
        return web.json_response({"ok": False, "reason": "unknown_action"}, status=404)
    status = 403 if result.reason == web_control.FORBIDDEN else STATUS.get(result.reason, 200)
    return web.json_response(result.as_dict(), status=status)


def register(app: web.Application) -> None:
    services = app[SERVICES]
    app[CONTROL] = web_control.ProjectControl(audit=services.audit, clock=services.clock)
    app.router.add_post("/api/projects/{project_id}/{action}", post_control)


def use(control: web_control.ProjectControl):
    """A ``create_app(routes=[...])`` entry that replaces the app's controller (tests, TI-12)."""
    def register_control(app: web.Application) -> None:
        app[CONTROL] = control
    return register_control
