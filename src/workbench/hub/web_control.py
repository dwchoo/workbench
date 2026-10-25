"""Hub-side project control for the web (Task Inbox SPEC "프로젝트 제어 실행", TI-08).

:class:`ProjectControl` runs the HB-07 library (``workbench.hub.projects``) for a logged-in owner
only. Every method is blocking and is meant for an executor thread; none of them touches the
process-wide ``sys.stdout``/``sys.stderr`` or forks inside the Hub: ``power_on`` runs
``projects.power_on(attach=False)`` in a short-lived driver subprocess (the backend it starts is
double-forked into its own session, never a Hub descendant).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping

from . import projects, registry
from .inbox.service import Principal
from .paths import hub_layout
from .process import hub_environment
from .status import check_entry

FORBIDDEN = "forbidden"  # not an owner session principal (API keys, MCP, backends, local CLI)
UNKNOWN_PROJECT = "unknown_project"  # no registry entry has this project id
CONTROL_IN_PROGRESS = "control_in_progress"  # another control call of this project is still running
REGISTRY_INVALID = "registry_invalid"  # the registry could not be read
CONFIRM_REQUIRED = "confirm_required"  # power off step 1: shown, nothing stopped (data.token/active)
CONFIRM_TOKEN_INVALID = "confirm_token_invalid"  # unknown/used/expired, or another session's/project's
WORK_CHANGED = "work_changed"  # the confirm was refused: a new page with the new list (data.token/active)
TOKEN_TTL = 120.0  # a web confirm token lives as long as the backend's (``expires_in``, else this)
# The driver waits for ``cmd_start`` (its own ``START_TIMEOUT`` is 120 s) plus process start-up.
DRIVER_TIMEOUT = 180.0


@dataclass(frozen=True)
class Result:
    """One control call: ``ok`` with ``reason``/``detail`` when not ok; ``data`` call-specific."""
    ok: bool
    action: str
    project_id: str
    reason: str | None = None
    detail: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "action": self.action, "project_id": self.project_id, "reason": self.reason,
                "detail": self.detail, "data": self.data}


@dataclass
class _Pending:
    """One shown power-off confirm page: who may confirm it and the backend token it confirms."""
    session: str
    project_id: str
    backend_token: str
    backend: dict | None  # the backend's ``{pid, start_ticks}`` named by its shutdown_request
    refusals: list  # backend details of the refused confirms that led to this page
    active: list
    expires: float  # monotonic


def _owner(principal: object) -> bool:
    return isinstance(principal, Principal) and principal.kind == "owner"


def _driver_source() -> str:
    package_parent = str(Path(__file__).resolve().parents[2])
    return (f"import sys; sys.path.insert(0, {package_parent!r}); "
            "from workbench.hub.web_control import _driver_main; raise SystemExit(_driver_main(sys.argv[1:]))")


def _driver_main(argv: list[str]) -> int:
    """``power_on`` driver: one ``projects.power_on(attach=False, out=...)`` call; the outcome is
    the last stdout line (JSON). The start lines go to ``data.output``, never to this stdout."""
    import io
    spec = json.loads(argv[0])
    entry = registry.Entry(**spec["entry"])
    outcome = projects.power_on(entry, attach=False, out=io.StringIO())
    sys.stdout.write("\n" + json.dumps(outcome.as_dict()) + "\n")
    sys.stdout.flush()
    return 0


class ProjectControl:
    """Owner-only ``power_on``/``power_off_request``/``power_off_confirm``/``pause``/``resume``/
    ``confirm_boot`` for registered projects. ``environment`` (default ``os.environ``) gives the
    Hub registry location and the driver's environment (without project-only variables)."""

    def __init__(self, *, environment: Mapping[str, str] | None = None, audit=None,
                 clock: Callable[[], float] | None = None):
        self.environment = dict(os.environ if environment is None else environment)
        self.audit = audit
        self.clock = clock or time.time
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}  # web confirm token -> the shutdown request it stands for
        self._busy: set[str] = set()  # project ids with a control call running

    def _refuse(self, action: str, principal: object, project_id: str) -> Result | None:
        if not _owner(principal):
            return Result(False, action, project_id, FORBIDDEN, "only a logged-in owner session controls projects")
        return None

    def _entry(self, action: str, project_id: str) -> registry.Entry | Result:
        try:
            entries = registry.read(hub_layout(self.environment))
        except registry.RegistryError as exc:
            return Result(False, action, project_id, REGISTRY_INVALID, str(exc))
        for entry in entries:
            if entry.project_id == project_id:
                return entry
        return Result(False, action, project_id, UNKNOWN_PROJECT, "no registered project has this id")

    def _record(self, result: Result, client_addr: str | None) -> Result:
        """Audit ``web_control`` (a failing audit never changes the control result)."""
        if self.audit is not None:
            try:
                self.audit.record("web_control", "owner", client_addr,
                                  {"project_id": result.project_id[:64], "action": result.action,
                                   "result": "ok" if result.ok else str(result.reason)},
                                  now=float(self.clock()))
            except Exception as exc:  # noqa: BLE001 - the audit store never decides a control result
                print(f"[workbench] web_control audit failed: {exc!r}", file=sys.stderr, flush=True)
        return result

    @staticmethod
    def _outcome(action: str, project_id: str, outcome: projects.Outcome) -> Result:
        return Result(outcome.ok, action, project_id, outcome.reason, outcome.detail, dict(outcome.data))

    def _run_driver(self, entry: registry.Entry) -> projects.Outcome:
        spec = {"entry": entry.to_json()}
        try:
            done = subprocess.run([sys.executable, "-I", "-c", _driver_source(), json.dumps(spec)],
                                  env=hub_environment(self.environment), cwd="/", stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, errors="replace", timeout=DRIVER_TIMEOUT,
                                  start_new_session=True)
        except subprocess.TimeoutExpired:
            return projects.Outcome(False, projects.START_FAILED, f"no start result within {DRIVER_TIMEOUT:.0f} s")
        except OSError as exc:
            return projects.Outcome(False, projects.START_FAILED, f"driver: {type(exc).__name__}: {exc}")
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        try:
            value = json.loads(lines[-1])
            return projects.Outcome(value["ok"], value.get("reason"), value.get("detail"), value.get("data") or {})
        except (IndexError, ValueError, KeyError, TypeError):
            tail = (done.stderr or done.stdout).strip()[-500:]
            return projects.Outcome(False, projects.START_FAILED, f"driver exit {done.returncode}: {tail}")

    def _guarded(self, action: str, principal: object, project_id: str, client_addr: str | None,
                 body: Callable[[registry.Entry], Result]) -> Result:
        """Owner check, then this project's single control slot (``control_in_progress`` while
        another call of the same project runs), the registry entry and ``body``; audited."""
        refused = self._refuse(action, principal, project_id)
        if refused is not None:
            return refused
        with self._lock:
            busy = project_id in self._busy
            if not busy:
                self._busy.add(project_id)
        if busy:
            return self._record(Result(False, action, project_id, CONTROL_IN_PROGRESS,
                                       "another control of this project is still running"), client_addr)
        try:
            entry = self._entry(action, project_id)
            result = entry if isinstance(entry, Result) else body(entry)
        finally:
            with self._lock:
                self._busy.discard(project_id)
        return self._record(result, client_addr)

    def in_progress(self, project_id: str) -> bool:
        """Whether a control call of ``project_id`` is running now (a page may hide its buttons)."""
        with self._lock:
            return project_id in self._busy

    def power_on(self, principal: Principal, project_id: str, *, client_addr: str | None = None) -> Result:
        """``projects.power_on(attach=False)`` in a driver subprocess (``data.backend``, ``data.output``).

        A verified running backend is reported as is (``data.already_running: true``) without a
        driver; otherwise ``data.already_running`` is false (the library still joins a backend
        that came up meanwhile, never a second one)."""
        action = "power_on"

        def body(entry: registry.Entry) -> Result:
            if check_entry(entry) is None:
                running = projects._backend_reference(entry)
                if running is not None:
                    return Result(True, action, project_id, data={"backend": running, "already_running": True})
            result = self._outcome(action, project_id, self._run_driver(entry))
            if result.ok:
                result.data["already_running"] = False
            return result
        return self._guarded(action, principal, project_id, client_addr, body)

    def _ask_shutdown(self, entry: registry.Entry, session: str, refusals: list) -> Result:
        """A ``shutdown_request`` (nothing is stopped): ``confirm_required`` with a web token bound
        to ``session`` and this project, the ``active`` list and ``expires_in``; the backend's
        token stays here."""
        from workbench.backend.client import ClientError
        from workbench.contracts.ui_v1 import ClientType
        action, project_id = "power_off", entry.project_id
        client, failed = projects._connect(entry)
        if failed is not None:
            return self._outcome(action, project_id, failed)
        with client:
            try:
                pending = client.request(ClientType.SHUTDOWN_REQUEST)
            except (OSError, TimeoutError, ClientError) as exc:
                return Result(False, action, project_id, projects.UNREACHABLE,
                              f"shutdown request failed (nothing was stopped): {type(exc).__name__}: {exc}")
        if not pending.get("ok", True) or not isinstance(pending.get("token"), str):
            return Result(False, action, project_id, pending.get("reason") or projects.UNREACHABLE,
                          pending.get("detail"))
        expires_in = pending.get("expires_in")
        expires_in = float(expires_in) if type(expires_in) in (int, float) and expires_in > 0 else TOKEN_TTL
        active = pending.get("active", [])
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._drop_expired()
            self._pending[token] = _Pending(session, project_id, pending["token"],
                                            (pending.get("processes") or {}).get("backend"), list(refusals),
                                            active, time.monotonic() + expires_in)
        return Result(False, action, project_id, CONFIRM_REQUIRED, None,
                      {"token": token, "active": active, "expires_in": expires_in, "refusals": list(refusals)})

    def _drop_expired(self) -> None:
        now = time.monotonic()
        for token in [token for token, item in self._pending.items() if item.expires <= now]:
            del self._pending[token]

    def power_off_request(self, principal: Principal, project_id: str, *, session: str,
                          client_addr: str | None = None) -> Result:
        """The first step of a web power off (SPEC "프로젝트 제어 실행"): the backend's
        ``shutdown_request`` only. ``reason: confirm_required`` with ``data.token`` (for this
        ``session`` and project only), ``data.active`` and ``data.expires_in``. Nothing stops
        until :meth:`power_off_confirm`; not confirming (the page's cancel) changes nothing."""
        action = "power_off"

        def body(entry: registry.Entry) -> Result:
            mismatch = check_entry(entry)
            if mismatch is not None:
                return Result(False, action, project_id, projects.MISMATCH, mismatch)
            return self._ask_shutdown(entry, session, [])
        return self._guarded(action, principal, project_id, client_addr, body)

    def _take(self, token: object, session: str, project_id: str) -> _Pending | None:
        """The pending confirm of ``token`` when it is this session's for this project (used up)."""
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            self._drop_expired()
            item = self._pending.get(token)
            if item is None or item.session != session or item.project_id != project_id:
                return None
            del self._pending[token]
            return item

    def power_off_confirm(self, principal: Principal, project_id: str, token: str, *, session: str,
                          client_addr: str | None = None) -> Result:
        """The confirm of a shown power-off page: the backend's ``shutdown_confirm`` with the token
        of that page's request, then its ``closing`` result (``cli.confirm_shutdown``, as HB-07).

        A token of another session or project, an unknown, used or expired one is
        ``confirm_token_invalid`` (nothing sent). ``shutdown_token_mismatch`` (the work changed, or
        the backend token expired or was replaced) is ``work_changed``: a new request's page data
        (new token and ``active``); after :data:`projects.MAX_WORK_CHANGE_RETRIES` new lists the
        next refusal is ``work_kept_changing``. Nothing was stopped in either case. Only
        ``verified is True`` is ``ok`` (``data.result``); ``no_result``/``unverified`` are not."""
        action = "power_off"

        def body(entry: registry.Entry) -> Result:
            from workbench.backend import cli
            from workbench.contracts.ui_v1 import Reason
            item = self._take(token, session, project_id)
            if item is None:
                return Result(False, action, project_id, CONFIRM_TOKEN_INVALID,
                              "this confirm is unknown, used, expired or not this session's")
            mismatch = check_entry(entry)
            if mismatch is not None:
                return Result(False, action, project_id, projects.MISMATCH, mismatch)
            client, failed = projects._connect(entry)
            if failed is not None:
                return self._outcome(action, project_id, failed)
            with client:
                wait = cli.confirm_shutdown(client, item.backend_token, progress=None)
            if wait.refused is not None:
                reason, detail = wait.refused.get("reason"), wait.refused.get("detail")
                if reason != Reason.SHUTDOWN_TOKEN_MISMATCH.value:
                    return Result(False, action, project_id, reason or projects.UNREACHABLE, detail)
                refusals = [*item.refusals, detail]
                if len(refusals) > projects.MAX_WORK_CHANGE_RETRIES:
                    return Result(False, action, project_id, projects.WORK_KEPT_CHANGING, detail,
                                  {"active": item.active, "refusals": refusals,
                                   "text": projects.WORK_KEPT_CHANGING_TEXT})
                asked = self._ask_shutdown(entry, session, refusals)
                if asked.reason != CONFIRM_REQUIRED:
                    return asked
                cause = f" ({detail})" if isinstance(detail, str) and detail else ""
                return Result(False, action, project_id, WORK_CHANGED,
                              f"{projects.WORK_CHANGED_TEXT}{cause}. {projects.RE_ASK_TEXT}", asked.data)
            if wait.result is None:  # never a success (C-AC-22)
                backend = cli._backend_state(item.backend, cli.BACKEND_GONE_WAIT)
                return Result(False, action, project_id, projects.NO_RESULT,
                              wait.lost or "the backend sent no result", {"backend": backend})
            if wait.result.get("verified") is not True:
                return Result(False, action, project_id, projects.UNVERIFIED,
                              ", ".join(wait.result.get("problems") or ["result unknown"]), {"result": wait.result})
            return Result(True, action, project_id, data={"result": wait.result})
        return self._guarded(action, principal, project_id, client_addr, body)

    def _library(self, action: str, call: Callable[[registry.Entry], projects.Outcome], principal: object,
                 project_id: str, client_addr: str | None) -> Result:
        return self._guarded(action, principal, project_id, client_addr,
                             lambda entry: self._outcome(action, project_id, call(entry)))

    def pause(self, principal: Principal, project_id: str, *, client_addr: str | None = None) -> Result:
        """``projects.pause`` (HB-06 non-attached ``pause``); the outcome as is."""
        return self._library("pause", projects.pause, principal, project_id, client_addr)

    def resume(self, principal: Principal, project_id: str, *, client_addr: str | None = None) -> Result:
        """``projects.resume`` (``reconciled: true``); the outcome as is."""
        return self._library("resume", projects.resume, principal, project_id, client_addr)

    def confirm_boot(self, principal: Principal, project_id: str, *, client_addr: str | None = None) -> Result:
        """``projects.confirm_boot`` for the current boot id; the backend's refusal as is."""
        return self._library("confirm_boot", projects.confirm_boot, principal, project_id, client_addr)
