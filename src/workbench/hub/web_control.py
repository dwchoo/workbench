"""Hub-side project control for the web (Task Inbox SPEC "프로젝트 제어 실행", TI-08).

:class:`ProjectControl` runs the HB-07 library (``workbench.hub.projects``) for a logged-in owner
only. Every method is blocking and is meant for an executor thread; none of them touches the
process-wide ``sys.stdout``/``sys.stderr`` or forks inside the Hub: ``power_on`` runs
``projects.power_on(attach=False)`` in a short-lived driver subprocess (the backend it starts is
double-forked into its own session, never a Hub descendant).
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
import queue
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
CONTROL_BUSY = "control_busy"  # every control worker is busy (other projects): try again later
INTERNAL_ERROR = "internal_error"  # the library raised; the state is unknown (audited, slot released)
REGISTRY_INVALID = "registry_invalid"  # the registry could not be read
CONFIRM_REQUIRED = "confirm_required"  # power off step 1: shown, nothing stopped (data.token/active)
CONFIRM_TOKEN_INVALID = "confirm_token_invalid"  # unknown/used/expired, or another session's/project's
WORK_CHANGED = "work_changed"  # the confirm was refused: a new page with the new list (data.token/active)
TOKEN_TTL = 120.0  # a web confirm token lives as long as the backend's (``expires_in``, else this)
# The driver waits for ``cmd_start`` (its own ``START_TIMEOUT`` is 120 s) plus process start-up.
DRIVER_TIMEOUT = 180.0
DRIVER_OUTPUT_CAP = 256 * 1024  # bytes of each driver stream kept (the outcome is its last stdout line)
DETAIL_TAIL = 500  # characters of driver output in a start_failed detail
CONTROL_WORKERS = 4  # control calls running at once (own executor); more is ``control_busy``


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


class _DaemonExecutor:
    """A fixed pool of daemon worker threads (``concurrent.futures`` workers are joined at
    interpreter exit, which would keep a stopping Hub alive for a running control)."""

    def __init__(self, workers: int, name: str):
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._shutdown = False
        self._threads: list[threading.Thread] = []
        self._workers, self._name = workers, name

    def submit(self, function: Callable[[], Any]) -> Future:
        future: Future = Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot submit after shutdown")
            if len(self._threads) < self._workers:  # started on demand, at most ``workers``
                thread = threading.Thread(target=self._work, name=f"{self._name}-{len(self._threads)}", daemon=True)
                self._threads.append(thread)
                thread.start()
            self._queue.put((future, function))
        return future

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, function = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(function())
            except BaseException as exc:  # noqa: BLE001 - handed to the waiter
                future.set_exception(exc)

    async def run(self, function: Callable[..., Any], *args: Any) -> Any:
        """``function(*args)`` on these workers, awaited from the loop (``loop.run_in_executor`` with this pool)."""
        return await asyncio.wrap_future(self.submit(lambda: function(*args)))

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            self._shutdown = True
            threads = list(self._threads)
        if cancel_futures:
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    item[0].cancel()
        for _thread in threads:
            self._queue.put(None)
        if wait:
            for thread in threads:
                thread.join()


def _keep_tail(stream, tail: bytearray) -> None:
    """Read ``stream`` to its end, keeping only its last :data:`DRIVER_OUTPUT_CAP` bytes in ``tail``."""
    with stream:
        while chunk := stream.read1(65536):
            tail += chunk
            if len(tail) > DRIVER_OUTPUT_CAP:
                del tail[:len(tail) - DRIVER_OUTPUT_CAP]


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



DaemonExecutor = _DaemonExecutor  # the Hub's other bounded pools (MCP, result pages)

class ProjectControl:
    """Owner-only ``power_on``/``power_off_request``/``power_off_confirm``/``pause``/``resume``/
    ``confirm_boot`` for registered projects. ``environment`` (default ``os.environ``) gives the
    Hub registry location and the driver's environment (without project-only variables)."""

    def __init__(self, *, environment: Mapping[str, str] | None = None, audit=None,
                 clock: Callable[[], float] | None = None):
        self.environment = dict(os.environ if environment is None else environment)
        self.audit = audit
        self.clock = clock or time.time
        self.driver_timeout = DRIVER_TIMEOUT
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}  # web confirm token -> the shutdown request it stands for
        self._busy: set[str] = set()  # project ids with a control call running
        # Controls block for seconds to minutes (a start, a shutdown result): they get their own
        # bounded workers, so the loop's default executor (sessions, logins, SQLite) never waits.
        self._executor = _DaemonExecutor(CONTROL_WORKERS, "wb-control")
        self._slots = threading.BoundedSemaphore(CONTROL_WORKERS)
        self._running: dict[int, tuple[str, str, str | None, asyncio.Future]] = {}  # awaited controls
        self._closed = False

    async def run(self, action: str, project_id: str, call: Callable[[], Result], *,
                  client_addr: str | None = None) -> Result:
        """Run one blocking control ``call`` on the control workers; ``control_busy`` at once when
        all :data:`CONTROL_WORKERS` are taken or the control is closed (nothing started, not
        audited). A slot is held until the call itself returns, even when the awaiting request
        goes away."""
        if self._closed or not self._slots.acquire(blocking=False):
            return Result(False, action, project_id, CONTROL_BUSY, "every project control worker is busy; try again")

        def job() -> Result:
            try:
                return call()
            finally:
                self._slots.release()
        try:
            future = self._executor.submit(job)
        except RuntimeError:  # shut down
            self._slots.release()
            return Result(False, action, project_id, CONTROL_BUSY, "project control is shutting down")
        waiting = asyncio.wrap_future(future)
        key = id(waiting)
        self._running[key] = (action, project_id, client_addr, waiting)
        try:
            return await waiting
        finally:
            self._running.pop(key, None)

    async def close(self) -> None:
        """Hub stop: queued controls are cancelled, running ones are abandoned (their worker
        threads are daemons and their drivers run in their own sessions, so a started backend
        still comes up on its own) and audited ``unknown``; nothing waits for them."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        running, self._running = list(self._running.values()), {}
        for action, project_id, client_addr, waiting in running:
            self._audit(action, project_id, "unknown", client_addr)
            waiting.cancel()

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

    def _audit(self, action: str, project_id: str, outcome: str, client_addr: str | None) -> None:
        """One ``web_control`` row (a failing audit never changes a control result)."""
        if self.audit is None:
            return
        try:
            self.audit.record("web_control", "owner", client_addr,
                              {"project_id": project_id[:64], "action": action, "result": outcome},
                              now=float(self.clock()))
        except Exception as exc:  # noqa: BLE001 - the audit store never decides a control result
            print(f"[workbench] web_control audit failed: {exc!r}", file=sys.stderr, flush=True)

    def _record(self, result: Result, client_addr: str | None) -> Result:
        """Audit the result; after :meth:`close` an abandoned control was already audited ``unknown``."""
        if not self._closed:
            self._audit(result.action, result.project_id, "ok" if result.ok else str(result.reason), client_addr)
        return result

    @staticmethod
    def _outcome(action: str, project_id: str, outcome: projects.Outcome) -> Result:
        return Result(outcome.ok, action, project_id, outcome.reason, outcome.detail, dict(outcome.data))

    def _driver_command(self, spec: dict) -> list[str]:
        return [sys.executable, "-I", "-c", _driver_source(), json.dumps(spec)]

    def _run_driver(self, entry: registry.Entry) -> projects.Outcome:
        """The driver's outcome (its last stdout line). Only the last :data:`DRIVER_OUTPUT_CAP`
        bytes of each stream are kept; a driver past ``driver_timeout`` is killed (``start_failed``,
        the backend state unknown)."""
        try:
            process = subprocess.Popen(self._driver_command({"entry": entry.to_json()}),
                                       env=hub_environment(self.environment), cwd="/", stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            return projects.Outcome(False, projects.START_FAILED, f"driver: {type(exc).__name__}: {exc}")
        tails = [bytearray(), bytearray()]
        readers = [threading.Thread(target=_keep_tail, args=(stream, tail), daemon=True)
                   for stream, tail in ((process.stdout, tails[0]), (process.stderr, tails[1]))]
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=self.driver_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return projects.Outcome(False, projects.START_FAILED,
                                    f"no start result within {self.driver_timeout:g} s; the backend state is "
                                    "unknown: recheck the project before turning it on again")
        finally:
            for reader in readers:
                reader.join(5)
        stdout, stderr = (bytes(tail).decode("utf-8", "replace") for tail in tails)
        lines = [line for line in stdout.splitlines() if line.strip()]
        try:
            value = json.loads(lines[-1])
            return projects.Outcome(value["ok"], value.get("reason"), value.get("detail"), value.get("data") or {})
        except (IndexError, ValueError, KeyError, TypeError):
            tail = (stderr or stdout).strip()[-DETAIL_TAIL:]
            return projects.Outcome(False, projects.START_FAILED, f"driver exit {returncode}: {tail}")

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
        except Exception as exc:  # noqa: BLE001 - a library surprise is a result, never a 500 or a stuck slot
            print(f"[workbench] web_control {action} failed: {exc!r}", file=sys.stderr, flush=True)
            result = Result(False, action, project_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"[:500])
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
                running = projects.backend_reference(entry)
                if running is not None:
                    return Result(True, action, project_id, data={"backend": running, "already_running": True})
            result = self._outcome(action, project_id, self._run_driver(entry))
            if result.ok:
                result.data["already_running"] = False
            return result
        return self._guarded(action, principal, project_id, client_addr, body)

    def _ask_shutdown(self, entry: registry.Entry, session: str, refusals: list) -> Result:
        """``projects.power_off_request`` (nothing is stopped): ``confirm_required`` with a web
        token bound to ``session`` and this project, the ``active`` list and ``expires_in``; the
        backend's token stays here."""
        action, project_id = "power_off", entry.project_id
        pending = projects.power_off_request(entry)
        if not pending.ok:
            return self._outcome(action, project_id, pending)
        if not isinstance(pending.data.get("token"), str):
            return Result(False, action, project_id, projects.UNREACHABLE, "the shutdown request carried no token")
        expires_in = pending.data.get("expires_in")
        expires_in = float(expires_in) if type(expires_in) in (int, float) and expires_in > 0 else TOKEN_TTL
        active = pending.data.get("active") or []
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._drop_expired()
            # one shown page per session and project: an earlier one confirms a replaced request
            for earlier in [key for key, item in self._pending.items()
                            if item.session == session and item.project_id == project_id]:
                del self._pending[earlier]
            self._pending[token] = _Pending(session, project_id, pending.data["token"], pending.data.get("backend"),
                                            list(refusals), active, time.monotonic() + expires_in)
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
        return self._guarded("power_off", principal, project_id, client_addr,
                             lambda entry: self._ask_shutdown(entry, session, []))

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
        of that page's request, then its ``closing`` result (``projects.power_off_confirm``).

        A token of another session or project, an unknown, used or expired one is
        ``confirm_token_invalid`` (nothing sent). ``shutdown_token_mismatch`` (the work changed, or
        the backend token expired or was replaced) is ``work_changed``: a new request's page data
        (new token and ``active``); after :data:`projects.MAX_WORK_CHANGE_RETRIES` new lists the
        next refusal is ``work_kept_changing``. Nothing was stopped in either case. Only
        ``verified is True`` is ``ok`` (``data.result``); ``no_result``/``unverified`` are not."""
        action = "power_off"

        def body(entry: registry.Entry) -> Result:
            item = self._take(token, session, project_id)
            if item is None:
                return Result(False, action, project_id, CONFIRM_TOKEN_INVALID,
                              "this confirm is unknown, used, expired or not this session's")
            confirmed = projects.power_off_confirm(entry, item.backend_token, None, item.backend)
            if confirmed.reason != projects.SHUTDOWN_TOKEN_MISMATCH:
                return self._outcome(action, project_id, confirmed)
            detail = confirmed.detail
            refusals = [*item.refusals, detail]
            if len(refusals) > projects.MAX_WORK_CHANGE_RETRIES:
                return Result(False, action, project_id, projects.WORK_KEPT_CHANGING, detail,
                              {"active": item.active, "refusals": refusals, "text": projects.WORK_KEPT_CHANGING_TEXT})
            asked = self._ask_shutdown(entry, session, refusals)
            if asked.reason != CONFIRM_REQUIRED:
                return asked
            cause = f" ({detail})" if isinstance(detail, str) and detail else ""
            return Result(False, action, project_id, WORK_CHANGED,
                          f"{projects.WORK_CHANGED_TEXT}{cause}. {projects.RE_ASK_TEXT}", asked.data)
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
