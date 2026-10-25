"""``InboxService``: request state machine, permissions and per-project serialization.

SPEC "Principal과 권한", "요청 저장소"(직렬화), "요청 상태 기계". Every operation on a project
runs on that project's single executor thread in arrival order (D-35); each change is one
``BEGIN IMMEDIATE`` transaction that reads the current row, checks the transition, writes and
commits. Side effects leave only through ``on_committed(event)`` after the commit.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
import secrets
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable
import uuid

from . import limits
from .limits import InboxError
from .store import IDLE_SECONDS, InboxStore

__all__ = ["CommittedEvent", "InboxError", "InboxService", "Principal", "ProjectRef"]

LOG = logging.getLogger(__name__)
STATES = ("NEW", "REVIEWING", "NEEDS_INFO", "READY", "RUNNING", "CLOSED")
PRE_DISPATCH = ("NEW", "REVIEWING", "NEEDS_INFO")
OPEN_STATES = STATES[:-1]
LINKED = ("READY", "RUNNING")
LINK_TTL = 30.0
TASK_VIEW_MAX_BYTES = 256 * 1024  # SPEC schema: task_view ≤ 256 KiB
# Task statuses (SPEC "Task 동기화") that make a linked request RUNNING rather than READY.
RUNNING_TASK_STATUSES = frozenset({"running", "waiting_report", "held", "cancelling", "finished", "blocked"})
REVIEW_ACTIONS = ("begin_review", "ask", "comment", "reject", "close")
UNKNOWN_DELIVERY_COMMENT = "manager 전달 여부 확인 불명"
REQUEUED_COMMENT = ("backend가 다시 시작되어(이전 manager 전달은 확인되지 않음) 이 요청을 manager에게 다시 전달합니다.")
EXCERPT_MAX = 1000  # SPEC ``inbox_notice`` fields.excerpt
# TI-13 cancel system comments (SPEC "취소": acceptance is not the end; unknown is never a success)
CANCEL_ACCEPTED_COMMENT = "Task 취소를 요청했습니다. Task가 닫히면 이 요청이 취소로 닫힙니다."
CANCEL_STOPPING_COMMENT = ("Task 취소를 요청했고 실행 중인 명령을 멈추는 중입니다(SIGINT부터). "
                           "명령이 끝나고 Task가 닫히면 이 요청이 취소로 닫힙니다.")
CANCEL_UNKNOWN_COMMENT = ("backend가 취소 요청에 답하지 않아 결과를 알 수 없습니다. "
                          "Task가 사용자 취소로 닫히는 것이 보이기 전에는 취소된 것으로 보지 않습니다.")
CANCEL_REFUSED_COMMENT = "backend가 취소를 거절했습니다. 아무것도 멈추지 않았습니다."
CANCEL_REFUSED_COMMENTS = {
    "task_changed": "확인 뒤 Task나 실행 중인 명령이 바뀌어 취소하지 않았습니다. 아무것도 멈추지 않았습니다.",
    "backend_not_running": "프로젝트 backend가 꺼져 있어 취소하지 않았습니다.",
}
STOP_UNKNOWN_COMMENT = ("실행 중인 명령의 stop 결과를 확인하지 못했습니다(unknown). 명령이 아직 남아 있을 수 있어 "
                        "취소가 끝난 것으로 보지 않습니다.")
# The last event of a request that names the notice of its pending round (SPEC ``event`` values).
NOTICE_EVENT_SOURCES = {"submitted": "new", "reopened": "new", "state_set": "new", "link_voided": "new",
                        "answered": "answered", "updated": "updated", "revised": "revised",
                        "linked_task_closed": "linked_task_closed", "cancelled": "cancelled"}
_NOT_FOUND = "request not found"

# SPEC "Principal과 권한". Reading is checked first (hidden → not_found), then the operation.
READERS = frozenset({"owner", "board_agent", "editor", "requester", "manager"})
PERMISSIONS = {
    "submit": frozenset({"owner", "board_agent", "editor", "requester"}),
    "edit": frozenset({"owner", "board_agent", "editor"}),
    "comment": frozenset({"owner", "board_agent", "editor", "requester"}),
    "set_state": frozenset({"owner", "board_agent", "editor"}),
    "cancel": frozenset({"owner", "board_agent", "editor", "requester"}),
    "start_review": frozenset({"owner"}),
    "review": frozenset({"manager"}),
    "link": frozenset({"manager"}),
    "review_mode_read": frozenset({"owner", "board_agent", "editor", "manager", "local"}),
    "review_mode_set": frozenset({"owner", "local"}),
}


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling. ``scope`` is ``None`` for every project, else the visible project ids."""

    kind: str  # owner | board_agent | editor | requester | manager | local
    key_id: str | None = None
    name: str | None = None
    scope: frozenset[str] | None = None

    @classmethod
    def owner(cls) -> "Principal":
        return cls("owner")

    @classmethod
    def board_agent(cls, key_id: str) -> "Principal":
        return cls("board_agent", key_id, "board-agent")

    @classmethod
    def editor(cls, key_id: str, name: str, scope: Iterable[str] | None) -> "Principal":
        return cls("editor", key_id, name, None if scope is None else frozenset(scope))

    @classmethod
    def requester(cls, key_id: str, name: str, scope: Iterable[str] | None) -> "Principal":
        return cls("requester", key_id, name, None if scope is None else frozenset(scope))

    @classmethod
    def manager(cls, project_id: str) -> "Principal":
        return cls("manager", None, "manager", frozenset({project_id}))

    @classmethod
    def local(cls) -> "Principal":
        return cls("local")

    @property
    def origin(self) -> tuple[str, str | None, str | None]:
        """(origin_kind, origin_key_id, origin_name) for a request this principal submits."""
        if self.kind == "owner":
            return "owner", None, None
        if self.kind == "board_agent":
            return "board_agent", self.key_id, None
        return "api_key", self.key_id, self.name


@dataclass(frozen=True, slots=True)
class ProjectRef:
    project_id: str
    root: Path
    name: str = ""
    unavailable: str | None = None  # e.g. registry "mismatch" -> project_unavailable


@dataclass(frozen=True, slots=True)
class CommittedEvent:
    """Passed to ``on_committed`` after a change is durable. A failing callback changes nothing."""

    type: str
    project_id: str
    request_id: str | None
    state: str | None
    version: int | None
    actor_kind: str
    detail: dict = field(default_factory=dict)


def origin_json(kind: str, key_id: str | None, name: str | None) -> dict:
    label = {"owner": "소유자", "board_agent": "Board agent"}.get(kind, f"key:{name}")
    return {"kind": kind, "key_id": key_id, "name": name, "label": label}


def _content_hash(title: str, body: str) -> str:
    return hashlib.sha256(json.dumps([title, body], ensure_ascii=False).encode("utf-8")).hexdigest()


class _Project:
    def __init__(self, ref: ProjectRef, store: InboxStore):
        self.ref = ref
        self.store = store
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"inbox-{ref.project_id[:8]}")
        self.indexed = False
        # ``gate`` orders "is it closing?" + enqueue against the final ``store.close`` so nothing can be
        # queued behind the close (which would reopen the store and re-take ``inbox.lock``).
        self.gate = threading.Lock()
        self.closing: str | None = None  # hub_stopping (service close) | project_unavailable (removed)

    def schedule(self, fn: Callable[[], Any]) -> Future:
        with self.gate:
            if self.closing is not None:
                raise InboxError(self.closing, f"project {self.ref.project_id} inbox is closing")
            try:
                return self.executor.submit(fn)
            except RuntimeError as exc:  # executor already shut down
                raise InboxError(self.closing or "hub_stopping", str(exc)) from None

    def shutdown(self, reason: str) -> None:
        with self.gate:
            if self.closing is not None:
                return
            self.closing = reason
            final = self.executor.submit(self.store.close)
        try:
            final.result()
        finally:
            self.executor.shutdown(wait=True)


class _Tx:
    """One write transaction's helpers: the connection, its time, and the events to emit."""

    def __init__(self, service: "InboxService", project: _Project, connection: sqlite3.Connection,
                 actor: Principal | None):
        self.service, self.project, self.connection = service, project, connection
        self.now = service._clock()
        self.actor_kind = actor.kind if actor is not None else "hub"
        self.actor_name = actor.name if actor is not None else None
        self.events: list[CommittedEvent] = []

    def event(self, request_id: str | None, type_: str, from_state: str | None, to_state: str | None,
              detail: dict | None = None, *, version: int | None = None, notify: bool = True) -> None:
        self.connection.execute(
            "INSERT INTO events (request_id, at, actor_kind, actor_name, type, from_state, to_state, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, self.now, self.actor_kind, self.actor_name, type_, from_state, to_state,
             json.dumps(detail or {}, ensure_ascii=False)))
        if notify:
            self.events.append(CommittedEvent(type_, self.project.ref.project_id, request_id, to_state, version,
                                              self.actor_kind, dict(detail or {})))


class InboxService:
    def __init__(self, *, on_committed: Callable[[CommittedEvent], None] | None = None,
                 backend_running: Callable[[str], bool] | None = None,
                 clock: Callable[[], float] = time.time, idle_seconds: float = IDLE_SECONDS,
                 link_ttl: float = LINK_TTL,
                 cancel_port: Callable[[ProjectRef, str, str, dict, str | None], dict] | None = None):
        """``on_committed``: post-commit side-effect port (notices, backend ``cancel_task``). It runs on
        the committing project's executor thread, so it must not call this service for the same
        project with ``wait=True`` (that waits on its own thread and deadlocks); hand work off
        (``wait=False``, a queue or the event loop) and keep it short.
        ``backend_running(project_id)``: whether READY/RUNNING cancel can reach the backend now.
        ``cancel_port(ref, task_id, request_id, expect, reason)`` (TI-13): the backend ``cancel_task`` for a
        committed ``cancel_requested``, called on the cancelling caller's thread right after that commit; it answers
        ``{outcome: "accepted", cancel, stop}``, ``{outcome: "refused", reason}`` (nothing was cancelled) or
        ``{outcome: "unknown"}``. Without it the ``cancel_requested`` event is all there is (tests)."""
        self._on_committed = on_committed
        self._backend_running = backend_running or (lambda project_id: False)
        self._cancel_port = cancel_port
        self._clock = clock
        self._idle_seconds = idle_seconds
        self._link_ttl = link_ttl
        self._lock = threading.Lock()
        self._projects: dict[str, _Project] = {}
        self._index: dict[str, str] = {}
        self._link_tokens: dict[str, tuple[str, str, int, float]] = {}
        # TI-10: the Hub list's counts and review mode per project, kept from the reads and writes the inbox does
        # anyway (list polling never opens a store or counts as its use)
        self._summaries: dict[str, dict] = {}

    # -- projects ---------------------------------------------------------------------------
    def set_projects(self, refs: Iterable[ProjectRef]) -> None:
        """Replace the attached projects (Hub start and registry changes) and index their requests."""
        refs = list(refs)
        with self._lock:
            wanted = {ref.project_id: ref for ref in refs}
            removed = [p for pid, p in self._projects.items()
                       if pid not in wanted or p.ref.root != wanted[pid].root]
            for project in removed:
                del self._projects[project.ref.project_id]
                self._summaries.pop(project.ref.project_id, None)
            self._index = {rid: pid for rid, pid in self._index.items() if pid in self._projects}
            added = []
            for ref in refs:
                current = self._projects.get(ref.project_id)
                if current is None:
                    current = _Project(ref, InboxStore(ref.root, ref.project_id, clock=self._clock,
                                                       idle_seconds=self._idle_seconds))
                    self._projects[ref.project_id] = current
                    added.append(current)
                else:
                    current.ref = ref
        for project in removed:
            project.shutdown("project_unavailable")
        for project in added:
            self._try_index(project)

    def close(self) -> None:
        """Close every inbox and release every ``inbox.lock`` (Hub stop)."""
        with self._lock:
            projects, self._projects, self._index = list(self._projects.values()), {}, {}
        for project in projects:
            project.shutdown("hub_stopping")

    def close_idle(self, *, force: bool = False) -> list[str]:
        """Close inboxes unused for ``idle_seconds`` (all with ``force``); returns the closed project ids."""
        closed = []
        with self._lock:
            projects = list(self._projects.values())
        for project in projects:
            def check(store: InboxStore = project.store) -> bool:
                if force and store.is_open:
                    store.close()
                    return True
                return store.close_if_idle()
            try:
                if self._run(project, check):
                    closed.append(project.ref.project_id)
            except InboxError as exc:
                if exc.code not in ("hub_stopping", "project_unavailable"):
                    raise
        return closed

    def _try_index(self, project: _Project) -> None:
        def scan() -> list[str]:
            def fn(connection: sqlite3.Connection) -> list[str]:
                self._remember(project, connection)
                return [row[0] for row in connection.execute("SELECT request_id FROM requests")]
            return project.store.read(fn, missing=[])
        try:
            if project.ref.unavailable is not None:
                return
            ids = project.schedule(scan).result()
        except InboxError:
            return
        with self._lock:
            if self._projects.get(project.ref.project_id) is project:
                for request_id in ids:
                    self._index[request_id] = project.ref.project_id
                project.indexed = True

    # -- dispatch helpers -------------------------------------------------------------------
    def _run(self, project: _Project, fn: Callable[[], Any], wait: bool = True) -> Any:
        future = project.schedule(fn)
        return future.result() if wait else future

    def _visible_projects(self, principal: Principal) -> list[_Project]:
        with self._lock:
            projects = list(self._projects.values())
        return [p for p in projects if principal.scope is None or p.ref.project_id in principal.scope]

    def _project(self, principal: Principal, project_id: str) -> _Project:
        with self._lock:
            project = self._projects.get(project_id)
        if project is None or (principal.scope is not None and project_id not in principal.scope):
            raise InboxError("not_found", "project not found")
        if project.ref.unavailable is not None:
            raise InboxError("project_unavailable", project.ref.unavailable)
        return project

    def _locate(self, principal: Principal, request_id: object) -> _Project:
        if not isinstance(request_id, str):
            raise InboxError("not_found", _NOT_FOUND)
        with self._lock:
            project_id = self._index.get(request_id)
            stale = [p for p in self._projects.values() if not p.indexed]
        if project_id is None and stale:
            for project in stale:
                self._try_index(project)
            with self._lock:
                project_id = self._index.get(request_id)
        if project_id is None:
            raise InboxError("not_found", _NOT_FOUND)
        try:
            return self._project(principal, project_id)
        except InboxError as exc:
            if exc.code == "not_found":
                raise InboxError("not_found", _NOT_FOUND) from None
            raise

    # -- list summary (TI-10) -----------------------------------------------------------------
    @staticmethod
    def _summary_of(connection: sqlite3.Connection) -> dict:
        counts = dict(connection.execute("SELECT state, count(*) FROM requests GROUP BY state").fetchall())
        mode = connection.execute("SELECT value FROM meta WHERE key = 'review_mode'").fetchone()
        return {"pending_requests": {name.lower(): counts.get(name, 0)
                                     for name in ("NEW", "REVIEWING", "NEEDS_INFO", "READY", "RUNNING")},
                "review_mode": mode[0] if mode else "auto"}

    def _remember(self, project: _Project, connection: sqlite3.Connection, summary: dict | None = None) -> None:
        """Keep the project's list summary (project thread, on a connection the inbox opened anyway)."""
        summary = self._summary_of(connection) if summary is None else summary
        with self._lock:
            if self._projects.get(project.ref.project_id) is project:
                self._summaries[project.ref.project_id] = summary

    def list_summary(self, project_id: str) -> dict | None:
        """The Hub list's ``{pending_requests, review_mode}`` for a project, from memory: the last value an inbox
        read or write left, filled once from an inbox that is open already. It never opens an inbox and does not
        count as its use (an idle inbox still closes). A missing inbox file is empty (``auto``); a project not
        known yet whose inbox is closed, or one that cannot be read (``project_unavailable``), is None."""
        with self._lock:
            project = self._projects.get(project_id)
            cached = self._summaries.get(project_id)
        if project is None or project.ref.unavailable is not None:
            return None
        if cached is not None:
            return json.loads(json.dumps(cached))
        if not os.path.lexists(project.store.path):
            return {"pending_requests": {"new": 0, "reviewing": 0, "needs_info": 0, "ready": 0, "running": 0},
                    "review_mode": "auto"}

        def peek() -> dict | None:
            summary = project.store.peek(self._summary_of)
            if summary is not None:
                self._remember(project, None, summary)  # type: ignore[arg-type]
            return summary
        try:
            return self._run(project, peek)
        except InboxError:
            return None

    def _write(self, project: _Project, actor: Principal | None, fn: Callable[[_Tx], Any]) -> Any:
        """Run ``fn`` in one write transaction on the project thread, then emit its events."""
        holder: dict[str, Any] = {}

        def body(connection: sqlite3.Connection) -> Any:
            tx = holder["tx"] = _Tx(self, project, connection, actor)
            result = fn(tx)
            holder["summary"] = self._summary_of(connection)  # the state this commit leaves
            return result
        result = project.store.write(body)
        self._remember(project, None, holder["summary"])  # type: ignore[arg-type]
        for event in holder["tx"].events:
            self._emit(event)
        return result

    def _emit(self, event: CommittedEvent) -> None:
        if self._on_committed is None:
            return
        try:
            self._on_committed(event)
        except Exception as exc:  # a failed notification never undoes the commit
            LOG.warning("inbox on_committed callback failed for %s %s: %r", event.type, event.request_id, exc)

    # -- row helpers ------------------------------------------------------------------------
    @staticmethod
    def _row(connection: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()

    @staticmethod
    def _can_see(principal: Principal, row: sqlite3.Row) -> bool:
        if principal.kind in ("owner", "board_agent", "editor", "manager"):
            return True
        if principal.kind == "requester":
            return row["origin_kind"] == "api_key" and row["origin_key_id"] == principal.key_id
        return False

    def _visible_row(self, principal: Principal, connection: sqlite3.Connection | None, request_id: str):
        row = None if connection is None else self._row(connection, request_id)
        if row is None or not self._can_see(principal, row):
            raise InboxError("not_found", _NOT_FOUND)
        return row

    def _summary(self, project: _Project, row: sqlite3.Row) -> dict:
        return {"request_id": row["request_id"], "project_id": project.ref.project_id, "seq": row["seq"],
                "title": row["title"], "state": row["state"], "outcome": row["outcome"],
                "origin": origin_json(row["origin_kind"], row["origin_key_id"], row["origin_name"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"], "task_id": row["task_id"]}

    def _full(self, project: _Project, connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        value = self._summary(project, row)
        request_id = row["request_id"]
        revisions = connection.execute(
            "SELECT revision, title, body, created_at, author_kind, author_name FROM request_revisions "
            "WHERE request_id = ? ORDER BY revision", (request_id,)).fetchall()
        comments = connection.execute(
            "SELECT comment_id, seq, created_at, author_kind, author_name, kind, body FROM comments "
            "WHERE request_id = ? ORDER BY seq", (request_id,)).fetchall()
        value.update({
            "body": row["body"], "revision": row["revision"], "version": row["version"],
            "revisions": [dict(r) for r in revisions], "comments": [dict(c) for c in comments],
            "task": {"task_id": row["task_id"], "spec_revision": row["spec_revision"],
                     "view": json.loads(row["task_view"]) if row["task_view"] else None},
            "result_summary": row["result_summary"], "cancel_state": row["cancel_state"],
            "delivery_state": row["delivery_state"], "review_round": row["review_round"],
        })
        return value

    # -- submit -----------------------------------------------------------------------------
    def submit(self, principal: Principal, project_id: str, title: str, body: str,
               idempotency_key: str | None = None, *, wait: bool = True) -> Any:
        """Store a NEW request and return its id at once (P2-AC-02/03/07)."""
        if principal.kind not in ("owner", "board_agent", "editor", "requester"):
            raise InboxError("forbidden", f"{principal.kind} cannot submit requests")
        title, body = limits.title(title), limits.body(body)
        idem = limits.idempotency_key(idempotency_key)
        project = self._project(principal, project_id)
        return self._run(project, lambda: self._submit(project, principal, title, body, idem), wait)

    def _submit(self, project: _Project, principal: Principal, title: str, body: str, idem: str | None) -> dict:
        origin_kind, origin_key_id, origin_name = principal.origin
        digest = _content_hash(title, body)

        def change(tx: _Tx) -> dict:
            connection = tx.connection
            connection.row_factory = sqlite3.Row
            if idem is not None:
                existing = connection.execute(
                    "SELECT * FROM requests WHERE origin_kind = ? AND ifnull(origin_key_id, '') = ? "
                    "AND idem_key = ?", (origin_kind, origin_key_id or "", idem)).fetchone()
                if existing is not None:
                    if existing["content_hash"] != digest:
                        raise InboxError("idempotency_conflict",
                                         "the idempotency_key was already used with different content",
                                         state=existing["state"], version=existing["version"])
                    return {"request_id": existing["request_id"], "project_id": project.ref.project_id,
                            "seq": existing["seq"], "state": existing["state"],
                            "origin": origin_json(origin_kind, origin_key_id, origin_name), "created": False}
            request_id = str(uuid.uuid4())
            seq = connection.execute("SELECT ifnull(max(seq), 0) + 1 FROM requests").fetchone()[0]
            connection.execute(
                "INSERT INTO requests (request_id, seq, created_at, updated_at, version, origin_kind, "
                "origin_key_id, origin_name, idem_key, content_hash, title, body, revision, state, outcome, "
                "review_round, delivery_state) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 1, 'NEW', NULL, 1, "
                "'pending')",
                (request_id, seq, tx.now, tx.now, origin_kind, origin_key_id, origin_name, idem, digest, title,
                 body))
            connection.execute(
                "INSERT INTO request_revisions (request_id, revision, title, body, created_at, author_kind, "
                "author_name) VALUES (?, 1, ?, ?, ?, ?, ?)",
                (request_id, title, body, tx.now, principal.kind, principal.name))
            tx.event(request_id, "submitted", None, "NEW", {"seq": seq}, version=1)
            return {"request_id": request_id, "project_id": project.ref.project_id, "seq": seq, "state": "NEW",
                    "origin": origin_json(origin_kind, origin_key_id, origin_name), "created": True}

        result = self._write(project, principal, change)
        if result["created"]:
            with self._lock:
                self._index[result["request_id"]] = project.ref.project_id
        return result

    # -- reads ------------------------------------------------------------------------------
    def get(self, principal: Principal, request_id: str, *, wait: bool = True) -> Any:
        if principal.kind not in ("owner", "board_agent", "editor", "requester", "manager"):
            raise InboxError("forbidden", f"{principal.kind} cannot read requests")
        project = self._locate(principal, request_id)

        def read() -> dict:
            def fn(connection: sqlite3.Connection) -> dict:
                return self._full(project, connection, self._visible_row(principal, connection, request_id))
            try:
                return project.store.read(fn, missing=None) or self._raise_not_found()
            except InboxError as exc:
                # Only the owner sees every request; for anyone else "busy" would reveal that an id
                # exists in some project, so it reads exactly like a missing request.
                if exc.code == "inbox_busy" and principal.kind != "owner":
                    raise InboxError("not_found", _NOT_FOUND) from None
                raise
        return self._run(project, read, wait)

    @staticmethod
    def _raise_not_found() -> dict:
        raise InboxError("not_found", _NOT_FOUND)

    def list_requests(self, principal: Principal, *, project_id: str | None = None,
                      states: Iterable[str] | None = None, include_closed: bool = False,
                      query: str | None = None, limit: int | None = None, cursor: str | None = None) -> dict:
        """Requests visible to ``principal`` across projects, newest first; CLOSED only on request."""
        if principal.kind not in ("owner", "board_agent", "editor", "requester", "manager"):
            raise InboxError("forbidden", f"{principal.kind} cannot read requests")
        query = limits.query(query)
        limit = limits.list_limit(limit)
        wanted = None
        if states is not None:
            wanted = set(states)
            if not wanted <= set(STATES):
                raise InboxError("invalid_input", f"unknown state in {sorted(wanted)}")
        offset = self._cursor(cursor)
        projects = [self._project(principal, project_id)] if project_id is not None \
            else self._visible_projects(principal)
        found: list[dict] = []
        unavailable: list[dict] = []
        for project in projects:
            if project.ref.unavailable is not None:  # like badge(); an explicit project_id raised above
                continue
            try:
                rows = self._run(project, lambda p=project: self._rows(p, principal))
            except InboxError as exc:
                if project_id is not None:
                    raise
                unavailable.append({"project_id": project.ref.project_id, "code": exc.code})
                continue
            for row, haystack in rows:
                if wanted is not None and row["state"] not in wanted:
                    continue
                if wanted is None and not include_closed and row["state"] == "CLOSED":
                    continue
                if query and query.casefold() not in haystack:
                    continue
                found.append(row)
        # Newest first by arrival order (seq), not by the wall clock, which can step backwards.
        found.sort(key=lambda r: (r["seq"], r["project_id"]), reverse=True)
        page = found[offset:offset + limit]
        more = offset + limit < len(found)
        result = {"requests": page, "next_cursor": f"o{offset + limit}" if more else None}
        if unavailable:
            result["unavailable"] = unavailable
        return result

    @staticmethod
    def _cursor(cursor: object) -> int:
        if cursor is None:
            return 0
        if isinstance(cursor, str) and cursor[:1] == "o" and cursor[1:].isdigit() and len(cursor) < 12:
            return int(cursor[1:])
        raise InboxError("invalid_input", "cursor is not valid")

    def _rows(self, project: _Project, principal: Principal) -> list[tuple[dict, str]]:
        def fn(connection: sqlite3.Connection) -> list[tuple[dict, str]]:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM requests ORDER BY seq").fetchall()
            return [(self._summary(project, row), f"{row['title']}\n{row['body']}".casefold())
                    for row in rows if self._can_see(principal, row)]
        return project.store.read(fn, missing=[])

    def badge(self, principal: Principal) -> dict:
        """``{pending: NEW 수, needs_info: NEEDS_INFO 수}`` over everything ``principal`` may see."""
        counts = {"pending": 0, "needs_info": 0}
        if principal.kind not in ("owner", "board_agent", "editor", "requester"):
            return counts
        for project in self._visible_projects(principal):
            if project.ref.unavailable is not None:
                continue
            try:
                rows = self._run(project, lambda p=project: self._rows(p, principal))
            except InboxError:
                continue
            for row, _ in rows:
                if row["state"] == "NEW":
                    counts["pending"] += 1
                elif row["state"] == "NEEDS_INFO":
                    counts["needs_info"] += 1
        return counts

    def pending_requests(self, project_id: str) -> dict | None:
        """Hub list counts; ``None`` when the inbox cannot be read."""
        with self._lock:
            project = self._projects.get(project_id)
        if project is None or project.ref.unavailable is not None:
            return None

        def fn(connection: sqlite3.Connection) -> dict:
            summary = self._summary_of(connection)
            self._remember(project, connection, summary)
            return summary["pending_requests"]
        empty = {"new": 0, "reviewing": 0, "needs_info": 0, "ready": 0, "running": 0}
        try:
            return self._run(project, lambda: project.store.read(fn, missing=empty))
        except InboxError:
            return None

    # -- review mode ------------------------------------------------------------------------
    def review_mode(self, principal: Principal, project_id: str, mode: str | None = None, *,
                    set: str | None = None) -> dict:  # noqa: A002 - ``set=`` kept for existing callers
        """Read or (owner/local) change ``meta.review_mode`` (D-32); a missing inbox reads ``auto``.
        ``mode`` is the new value; ``set=`` is the earlier keyword for the same thing."""
        if mode is not None and set is not None and mode != set:
            raise InboxError("invalid_input", "give the review mode once")
        mode = mode if mode is not None else set
        action = "review_mode_read" if mode is None else "review_mode_set"
        if principal.kind not in PERMISSIONS[action]:
            raise InboxError("forbidden", f"{principal.kind} cannot {action.replace('_', ' ')}")
        if mode is not None and mode not in ("auto", "manual"):
            raise InboxError("invalid_input", "review mode must be auto or manual")
        project = self._project(principal, project_id)

        def current(connection: sqlite3.Connection) -> str:
            row = connection.execute("SELECT value FROM meta WHERE key = 'review_mode'").fetchone()
            return row[0] if row else "auto"
        if mode is None:
            return {"review_mode": self._run(project, lambda: project.store.read(current, missing="auto"))}

        def change(tx: _Tx) -> dict:
            previous = current(tx.connection)
            if previous != mode:
                tx.connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('review_mode', ?)", (mode,))
                tx.event(None, "review_mode_changed", None, None, {"review_mode": mode, "previous": previous})
            return {"review_mode": mode}
        return self._run(project, lambda: self._write(project, principal, change))

    # -- request changes --------------------------------------------------------------------
    def _change(self, principal: Principal, request_id: str, action: str,
                fn: Callable[[_Tx, sqlite3.Row], Any], wait: bool) -> Any:
        """Visibility (not_found) → permission (forbidden) → ``fn`` in one transaction."""
        if principal.kind not in READERS:
            raise InboxError("forbidden", f"{principal.kind} cannot change requests")
        project = self._locate(principal, request_id)

        def change(tx: _Tx) -> Any:
            row = self._visible_row(principal, tx.connection, request_id)
            if principal.kind not in PERMISSIONS[action]:
                raise InboxError("forbidden", f"{principal.kind} cannot {action.replace('_', ' ')}",
                                 state=row["state"], version=row["version"])
            return fn(tx, row)
        return self._run(project, lambda: self._write(project, principal, change), wait)

    @staticmethod
    def _invalid(row: sqlite3.Row, detail: str) -> InboxError:
        return InboxError("invalid_transition", detail, state=row["state"], version=row["version"])

    @staticmethod
    def _base(row: sqlite3.Row, base_version: object) -> None:
        if isinstance(base_version, bool) or not isinstance(base_version, int):
            raise InboxError("invalid_input", "base_version must be an integer",
                             state=row["state"], version=row["version"])
        if base_version != row["version"]:
            raise InboxError("conflict", f"the request changed (base_version {base_version})",
                             state=row["state"], version=row["version"])

    @staticmethod
    def _set(tx: _Tx, row: sqlite3.Row, **fields: Any) -> sqlite3.Row:
        """Update the request row (version + 1, ``updated_at``) and return the new row."""
        fields.update(version=row["version"] + 1, updated_at=tx.now)
        columns = ", ".join(f"{name} = ?" for name in fields)
        cursor = tx.connection.execute(f"UPDATE requests SET {columns} WHERE request_id = ? AND version = ?",
                                       (*fields.values(), row["request_id"], row["version"]))
        if cursor.rowcount != 1:  # unreachable under the single writer; refuse rather than lose an update
            raise InboxError("internal_error", "request row changed underneath the writer")
        return InboxService._row(tx.connection, row["request_id"])

    @staticmethod
    def _comment(tx: _Tx, request_id: str, kind: str, body: str) -> str:
        comment_id = str(uuid.uuid4())
        seq = tx.connection.execute("SELECT ifnull(max(seq), 0) + 1 FROM comments WHERE request_id = ?",
                                    (request_id,)).fetchone()[0]
        tx.connection.execute(
            "INSERT INTO comments (comment_id, request_id, seq, created_at, author_kind, author_name, kind, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (comment_id, request_id, seq, tx.now, tx.actor_kind, tx.actor_name, kind, body))
        return comment_id

    @staticmethod
    def _new_round(row: sqlite3.Row) -> dict:
        return {"review_round": row["review_round"] + 1, "delivery_state": "pending", "delivery_id": None,
                "delivery_backend": None, "delivery_at": None}

    def update(self, principal: Principal, request_id: str, base_version: int, *, title: str | None = None,
               body: str | None = None, wait: bool = True) -> Any:
        """Edit rules (SPEC 편집): NEW/NEEDS_INFO in place, REVIEWING in place + new review round,
        READY/RUNNING a new request revision (``revised``), CLOSED refused."""
        if title is None and body is None:
            raise InboxError("invalid_input", "nothing to change (title or body)")
        title = None if title is None else limits.title(title)
        body = None if body is None else limits.body(body)

        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            self._base(row, base_version)
            state = row["state"]
            if state == "CLOSED":
                raise self._invalid(row, "a closed request cannot be edited; reopen it first")
            new_title = row["title"] if title is None else title
            new_body = row["body"] if body is None else body
            previous = {"title": row["title"], "body": row["body"], "revision": row["revision"]}
            if state in LINKED:
                revision = row["revision"] + 1
                tx.connection.execute(
                    "INSERT INTO request_revisions (request_id, revision, title, body, created_at, author_kind, "
                    "author_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (request_id, revision, new_title, new_body, tx.now, principal.kind, principal.name))
                new = self._set(tx, row, title=new_title, body=new_body, revision=revision,
                                delivery_state="pending", delivery_id=None, delivery_backend=None, delivery_at=None)
                tx.event(request_id, "revised", state, state, {"revision": revision}, version=new["version"])
                mode = "new_revision"
            else:
                tx.connection.execute(
                    "UPDATE request_revisions SET title = ?, body = ? WHERE request_id = ? AND revision = ?",
                    (new_title, new_body, request_id, row["revision"]))
                extra = self._new_round(row) if state == "REVIEWING" else {}
                new = self._set(tx, row, title=new_title, body=new_body, **extra)
                tx.event(request_id, "updated" if state == "REVIEWING" else "edited", state, state,
                         {"previous": previous, "review_round": new["review_round"]}, version=new["version"])
                mode = "in_place"
            return {"state": new["state"], "version": new["version"], "revision": new["revision"], "mode": mode}
        return self._change(principal, request_id, "edit", fn, wait)

    def comment(self, principal: Principal, request_id: str, body: str, *, wait: bool = True) -> Any:
        """A comment; on NEEDS_INFO it is the answer and the request returns to REVIEWING (new round)."""
        body = limits.comment(body)

        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            if row["state"] == "NEEDS_INFO":
                comment_id = self._comment(tx, request_id, "answer", body)
                new = self._set(tx, row, state="REVIEWING", **self._new_round(row))
                tx.event(request_id, "answered", "NEEDS_INFO", "REVIEWING",
                         {"comment_id": comment_id, "review_round": new["review_round"]}, version=new["version"])
            else:
                comment_id = self._comment(tx, request_id, "comment", body)
                new = self._set(tx, row)
                tx.event(request_id, "commented", row["state"], row["state"], {"comment_id": comment_id},
                         version=new["version"])
            return {"comment_id": comment_id, "state": new["state"], "version": new["version"]}
        return self._change(principal, request_id, "comment", fn, wait)

    def set_state(self, principal: Principal, request_id: str, base_version: int, to: str, *,
                  reason: str | None = None, wait: bool = True) -> Any:
        """NEW ↔ NEEDS_INFO and reopening a CLOSED request as NEW (D-34)."""
        if to not in ("NEW", "NEEDS_INFO"):
            raise InboxError("invalid_input", "to must be NEW or NEEDS_INFO")
        reason = None if reason is None else limits.comment(reason)

        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            self._base(row, base_version)
            state = row["state"]
            if (state, to) == ("NEW", "NEEDS_INFO"):
                new = self._set(tx, row, state="NEEDS_INFO")
                kind = "state_set"
            elif (state, to) == ("NEEDS_INFO", "NEW"):
                new = self._set(tx, row, state="NEW", **self._new_round(row))
                kind = "state_set"
            elif (state, to) == ("CLOSED", "NEW"):
                tx.connection.execute("UPDATE links SET state = 'detached' WHERE request_id = ? AND state = 'active'",
                                      (request_id,))
                new = self._set(tx, row, state="NEW", outcome=None, task_id=None, spec_revision=None,
                                task_view=None, cancel_state=None, result_summary=None, **self._new_round(row))
                kind = "reopened"
            else:
                raise self._invalid(row, f"{state} → {to} is not allowed")
            tx.event(request_id, kind, state, to, {"reason": reason, "previous_outcome": row["outcome"],
                                                   "task_id": row["task_id"]}, version=new["version"])
            return {"state": new["state"], "version": new["version"]}
        return self._change(principal, request_id, "set_state", fn, wait)

    def cancel(self, principal: Principal, request_id: str, *, reason: str | None = None,
               base_version: int | None = None, wait: bool = True) -> Any:
        """Before dispatch: CLOSED(cancelled) at once. READY/RUNNING: ``cancel_state=requested`` and a
        ``cancel_requested`` event for the backend port; the request closes only when the Task does."""
        reason = None if reason is None else limits.comment(reason)
        sent: dict = {}  # the committed ``expect`` for the backend call (TI-13)

        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            if base_version is not None:
                self._base(row, base_version)
            state = row["state"]
            # A REVIEWING/NEEDS_INFO request can still link a live Task (manager asked while RUNNING,
            # P2-AC-27). Closing it at once would orphan that Task, so it takes the linked path: the
            # active link stays and the backend cancels the Task (TI-13); the Task sync then closes it.
            if state in PRE_DISPATCH and row["task_id"] is None:
                told = state != "NEW" or row["delivery_state"] in ("offered", "delivered", "unknown")
                new = self._set(tx, row, state="CLOSED", outcome="cancelled",
                                delivery_state="pending" if told else "none", delivery_id=None)
                tx.event(request_id, "cancelled", state, "CLOSED", {"reason": reason, "notify_manager": told},
                         version=new["version"])
                return {"status": "cancelled", "state": "CLOSED", "version": new["version"]}
            if state != "CLOSED":
                if row["cancel_state"] is not None:
                    raise self._invalid(row, "cancel was already requested")
                if not self._backend_running(tx.project.ref.project_id):
                    raise InboxError("backend_not_running", "the project backend is not running",
                                     state=state, version=row["version"])
                view = json.loads(row["task_view"]) if row["task_view"] else {}
                command = view.get("command") if isinstance(view.get("command"), dict) else {}
                expect = {"status": view.get("status"), "command_id": command.get("command_id")}
                new = self._set(tx, row, cancel_state="requested")
                tx.event(request_id, "cancel_requested", state, state,
                         {"task_id": row["task_id"], "reason": reason, "expect": expect},
                         version=new["version"])
                sent["expect"] = expect
                return {"status": "cancel_requested", "state": state, "version": new["version"],
                        "task_id": row["task_id"]}
            raise self._invalid(row, "a closed request cannot be cancelled")
        result = self._change(principal, request_id, "cancel", fn, wait)
        if not wait or self._cancel_port is None or result.get("status") != "cancel_requested":
            return result
        return self._send_cancel(request_id, result, sent["expect"], reason)

    def _send_cancel(self, request_id: str, committed: dict, expect: dict, reason: str | None) -> dict:
        """TI-13: the backend ``cancel_task`` for the ``cancel_requested`` just committed, then its answer recorded:
        accepted keeps the request open (``stop_requested`` when a command stop started; the Task sync closes it),
        refused reverts ``cancel_state`` and raises (``task_changed`` as ``token_mismatch``), unknown stays requested
        with a system comment and is never reported as a success."""
        project = self._locate(Principal("hub"), request_id)
        task_id = committed["task_id"]
        try:
            answer = self._cancel_port(project.ref, task_id, request_id, expect, reason)
        except Exception as exc:  # the port never should; the outcome is then not known
            LOG.warning("inbox cancel port failed for %s: %r", request_id, exc)
            answer = {"outcome": "unknown"}
        outcome = answer.get("outcome") if isinstance(answer, dict) else None
        outcome = outcome if outcome in ("accepted", "refused") else "unknown"
        stop = answer.get("stop") if outcome == "accepted" and isinstance(answer.get("stop"), dict) else None
        refusal = str(answer.get("reason") or "backend_refused") if outcome == "refused" else None

        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            state = row["state"]
            current = row["cancel_state"] == "requested" and state != "CLOSED" and row["task_id"] == task_id
            if not current:  # the Task sync already closed it (or it moved on): nothing more to record
                return {"state": state, "version": row["version"]}
            if outcome == "refused":
                self._comment(tx, request_id, "system", CANCEL_REFUSED_COMMENTS.get(refusal, CANCEL_REFUSED_COMMENT))
                new = self._set(tx, row, cancel_state=None)
                tx.event(request_id, "cancel_refused", state, state, {"task_id": task_id, "reason": refusal},
                         version=new["version"])
            elif outcome == "accepted":
                self._comment(tx, request_id, "system", CANCEL_STOPPING_COMMENT if stop else CANCEL_ACCEPTED_COMMENT)
                new = self._set(tx, row, cancel_state="stop_requested" if stop else "requested")
                tx.event(request_id, "cancel_accepted", state, state,
                         {"task_id": task_id, "cancel": answer.get("cancel"), "stop": stop}, version=new["version"])
            else:
                self._comment(tx, request_id, "system", CANCEL_UNKNOWN_COMMENT)
                new = self._set(tx, row)
                tx.event(request_id, "cancel_unknown", state, state, {"task_id": task_id}, version=new["version"])
            return {"state": new["state"], "version": new["version"]}
        recorded = self._hub_change(request_id, fn, True)
        if outcome == "refused":
            code = "token_mismatch" if refusal == "task_changed" else (
                refusal if refusal in ("backend_not_running", "backend_unreachable", "backend_too_old")
                else "backend_refused")
            raise InboxError(code, "the backend did not cancel the Task", state=recorded["state"],
                             version=recorded["version"])
        value = {"status": "cancel_requested", "state": recorded["state"], "version": recorded["version"],
                 "task_id": task_id, "stop": stop}
        if outcome == "unknown":
            value["cancel"] = "unknown"
        return value

    def start_review(self, principal: Principal, request_id: str, *, wait: bool = True) -> Any:
        """Owner's manual "검토 시작" (or "다시 전달" after ``unknown``): mark the round for dispatch."""
        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            if row["state"] not in ("NEW", "REVIEWING") or row["delivery_state"] not in ("pending", "unknown"):
                raise self._invalid(row, "there is no review round waiting to be sent")
            # A pending round keeps a reserved delivery id: its notice may already be queued at the backend, so
            # the dispatcher sends that same id again (the backend queues it once) and its result still applies.
            # Only "다시 전달" after ``unknown`` starts a new delivery.
            keep = {} if row["delivery_state"] == "pending" else {"delivery_id": None}
            new = self._set(tx, row, delivery_state="pending", **keep)
            tx.event(request_id, "review_started", row["state"], row["state"],
                     {"review_round": row["review_round"]}, version=new["version"])
            return {"state": new["state"], "version": new["version"]}
        return self._change(principal, request_id, "start_review", fn, wait)

    # -- manager ----------------------------------------------------------------------------
    def _begin_if_new(self, tx: _Tx, row: sqlite3.Row) -> sqlite3.Row:
        if row["state"] != "NEW":
            return row
        new = self._set(tx, row, state="REVIEWING")
        tx.event(row["request_id"], "review_begun", "NEW", "REVIEWING", {}, version=new["version"])
        return new

    def review(self, principal: Principal, request_id: str, action: str, *, body: str | None = None,
               outcome: str | None = None, summary: str | None = None, wait: bool = True) -> Any:
        """Manager review results (``workbench_inbox``): begin_review, ask, comment, reject, close.
        Any action other than begin_review on a NEW request first moves it to REVIEWING."""
        if action not in REVIEW_ACTIONS:
            raise InboxError("invalid_input", f"unknown review action {action!r}")
        if action in ("ask", "comment", "reject"):
            body = limits.comment(body)
        if action == "close":
            if outcome not in ("success", "failed"):
                raise InboxError("invalid_input", "close needs outcome success or failed")
            summary = limits.summary(summary)

        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            if action == "begin_review":
                if row["state"] != "NEW":
                    raise self._invalid(row, "review can only begin on a NEW request")
                new = self._begin_if_new(tx, row)
                return {"state": new["state"], "version": new["version"]}
            # Judge against the state after the implicit NEW → REVIEWING, but refuse before writing it
            # so a refusal reports the committed state and leaves nothing behind.
            state = "REVIEWING" if row["state"] == "NEW" else row["state"]
            allowed = {"ask": ("REVIEWING", "RUNNING"), "comment": OPEN_STATES,
                       "reject": ("REVIEWING", "NEEDS_INFO"), "close": LINKED}[action]
            if state not in allowed:
                raise self._invalid(row, f"cannot {action} a {row['state']} request")
            row = self._begin_if_new(tx, row)
            if action == "ask":
                comment_id = self._comment(tx, request_id, "question", body)
                new = self._set(tx, row, state="NEEDS_INFO")
            elif action == "comment":
                comment_id = self._comment(tx, request_id, "progress", body)
                new = self._set(tx, row)
            elif action == "reject":
                # SPEC: reject is the manager's REVIEWING/NEEDS_INFO → CLOSED(rejected). A link still
                # active (asked while RUNNING) ends with the request in the link history; the Task
                # itself belongs to the manager, who cancels it through to_worker (the Hub does not).
                comment_id = self._comment(tx, request_id, "decision", body)
                tx.connection.execute("UPDATE links SET state = 'closed' WHERE request_id = ? AND state = 'active'",
                                      (request_id,))
                new = self._set(tx, row, state="CLOSED", outcome="rejected")
            else:
                comment_id = self._comment(tx, request_id, "decision", summary)
                tx.connection.execute("UPDATE links SET state = 'closed' WHERE request_id = ? AND state = 'active'",
                                      (request_id,))
                new = self._set(tx, row, state="CLOSED", outcome=outcome, result_summary=summary,
                                cancel_state=None)
            tx.event(request_id, {"ask": "asked", "comment": "progress", "reject": "rejected",
                                  "close": "closed"}[action], state, new["state"],
                     {"comment_id": comment_id, "outcome": new["outcome"]}, version=new["version"])
            return {"state": new["state"], "version": new["version"], "comment_id": comment_id}
        return self._change(principal, request_id, "review", fn, wait)

    def link_check(self, principal: Principal, request_id: str, *, wait: bool = True) -> Any:
        """First half of the approved-spec link: a 30 s token bound to the request's current version.
        Any later commit on the request (a cancel, an edit) invalidates it (D-35)."""
        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            if row["state"] not in PRE_DISPATCH or row["cancel_state"] is not None:
                raise self._invalid(row, f"a {row['state']} request cannot be linked")
            row = self._begin_if_new(tx, row)
            token = secrets.token_urlsafe(24)
            with self._lock:
                self._link_tokens = {k: v for k, v in self._link_tokens.items() if v[3] > tx.now}
                self._link_tokens[token] = (tx.project.ref.project_id, request_id, row["version"],
                                            tx.now + self._link_ttl)
            return {"link_token": token, "expires_in": int(self._link_ttl), "state": row["state"],
                    "version": row["version"]}
        return self._change(principal, request_id, "link", fn, wait)

    def link_commit(self, principal: Principal, link_token: str, task_id: str, spec_revision: int,
                    task_status: str, *, wait: bool = True) -> Any:
        """Second half: REVIEWING/NEEDS_INFO → READY (Task not started) or RUNNING (already running).
        The token is single-use; a commit on the request after the check makes it invalid."""
        if not isinstance(task_id, str) or not 0 < len(task_id) <= 128:
            raise InboxError("invalid_input", "task_id must be 1-128 characters")
        if isinstance(spec_revision, bool) or not isinstance(spec_revision, int) or spec_revision < 1:
            raise InboxError("invalid_input", "spec_revision must be a positive integer")
        if not isinstance(task_status, str) or not task_status or task_status == "closed":
            raise InboxError("invalid_input", "task_status must be an open Task status")
        if principal.kind not in PERMISSIONS["link"]:
            raise InboxError("forbidden", f"{principal.kind} cannot link")
        with self._lock:
            entry = self._link_tokens.pop(link_token, None) if isinstance(link_token, str) else None
        if entry is None or (principal.scope is not None and entry[0] not in principal.scope):
            raise InboxError("invalid_transition", "the link token is unknown or was already used")
        _, request_id, version, expires_at = entry

        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            if tx.now > expires_at:
                raise self._invalid(row, "the link token expired")
            if (row["version"] != version or row["state"] not in ("REVIEWING", "NEEDS_INFO")
                    or row["cancel_state"] is not None):
                raise self._invalid(row, "the request changed after the link check")
            state = "RUNNING" if task_status in RUNNING_TASK_STATUSES else "READY"
            tx.connection.execute("UPDATE links SET state = 'superseded' WHERE request_id = ? AND state = 'active'",
                                  (request_id,))
            tx.connection.execute(
                "INSERT INTO links (request_id, task_id, spec_revision, linked_at, state) "
                "VALUES (?, ?, ?, ?, 'active')", (request_id, task_id, spec_revision, tx.now))
            new = self._set(tx, row, state=state, task_id=task_id, spec_revision=spec_revision,
                            task_view=json.dumps({"status": task_status}))
            tx.event(request_id, "linked", row["state"], state,
                     {"task_id": task_id, "spec_revision": spec_revision, "task_status": task_status},
                     version=new["version"])
            return {"state": state, "version": new["version"]}
        return self._change(principal, request_id, "link", fn, wait)

    # -- Hub-internal transitions (dispatcher / Task sync) -----------------------------------
    def _hub_change(self, request_id: str, fn: Callable[[_Tx, sqlite3.Row], Any], wait: bool) -> Any:
        project = self._locate(Principal("hub"), request_id)

        def change(tx: _Tx) -> Any:
            row = self._row(tx.connection, request_id)
            if row is None:
                raise InboxError("not_found", _NOT_FOUND)
            return fn(tx, row)
        return self._run(project, lambda: self._write(project, None, change), wait)

    def mark_offered(self, request_id: str, delivery_id: str, backend: str, *, event: str | None = None,
                     version: int | None = None, wait: bool = True) -> Any:
        """Dispatcher: the pending round's notice (``event``: new, answered, updated, revised, cancelled,
        linked_task_closed) was queued by ``backend`` (``pid:start_ticks``). A CLOSED request can only
        be offered as its ``cancelled`` notice (SPEC "취소": the manager hears of a delivered round).
        ``version``: the row version :meth:`reserve_delivery` returned; any change since refuses."""
        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            if row["delivery_state"] != "pending":
                raise self._invalid(row, "no pending round to offer")
            if version is not None and (row["version"] != version or row["delivery_id"] != delivery_id):
                raise self._invalid(row, "the request changed after the delivery was reserved")
            if row["state"] == "CLOSED" and event != "cancelled":
                raise self._invalid(row, "a closed request is only offered as a cancelled notice")
            new = self._set(tx, row, delivery_state="offered", delivery_id=delivery_id, delivery_backend=backend,
                            delivery_at=tx.now)
            tx.event(request_id, "offered", row["state"], row["state"], {"delivery_id": delivery_id},
                     version=new["version"])
            return {"state": new["state"], "version": new["version"]}
        return self._hub_change(request_id, fn, wait)

    def record_delivery(self, request_id: str, delivery_id: str, outcome: str, *, wait: bool = True) -> Any:
        """Backend's notice result: ``delivered`` makes NEW → REVIEWING (P2-AC-24); ``unknown`` also
        REVIEWING but never resent (P2-AC-11); ``rejected``/``dropped`` return the round to pending."""
        if outcome not in ("delivered", "unknown", "rejected", "dropped"):
            raise InboxError("invalid_input", f"unknown delivery outcome {outcome!r}")

        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            # The result may come before the dispatcher marked the reserved notice offered (pending with
            # this delivery id), and after a notice call whose answer was lost (unknown, TI-10).
            if row["delivery_state"] not in ("offered", "pending", "unknown") or row["delivery_id"] != delivery_id:
                raise self._invalid(row, "the delivery is not the one on offer")
            state = row["state"]
            if outcome == "unknown" and row["delivery_state"] == "unknown":
                return {"state": state, "version": row["version"]}  # already recorded
            to = "REVIEWING" if state == "NEW" and outcome in ("delivered", "unknown") else state
            if outcome == "unknown":
                self._comment(tx, request_id, "system", UNKNOWN_DELIVERY_COMMENT)
            elif outcome in ("rejected", "dropped"):
                self._comment(tx, request_id, "system", f"manager 전달 실패({outcome}). 다시 보냅니다.")
            delivery = {"delivered": "delivered", "unknown": "unknown"}.get(outcome, "pending")
            extra = {"delivery_id": None} if delivery == "pending" else {}
            new = self._set(tx, row, state=to, delivery_state=delivery, **extra)
            tx.event(request_id, "delivery", state, to, {"delivery_id": delivery_id, "outcome": outcome},
                     version=new["version"])
            return {"state": to, "version": new["version"]}
        return self._hub_change(request_id, fn, wait)

    def reserve_delivery(self, request_id: str, delivery_id: str, version: int, *, wait: bool = True) -> Any:
        """Dispatcher: give the pending round ``delivery_id`` before its notice is sent, so a result that comes
        back before :meth:`mark_offered` (or after a Hub restart in between) still finds its round. Returns
        ``{state, version}`` (the version :meth:`mark_offered` expects)."""
        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            if row["version"] != version or row["delivery_state"] != "pending" or row["delivery_id"] is not None:
                raise self._invalid(row, "the round changed before its delivery was reserved")
            new = self._set(tx, row, delivery_id=delivery_id)
            return {"state": new["state"], "version": new["version"]}
        return self._hub_change(request_id, fn, wait)

    def requeue_offer(self, request_id: str, delivery_id: str, reason: str, *, wait: bool = True) -> Any:
        """Dispatcher: the backend that queued this offered notice is gone (its queue was memory): the round
        is pending again with a system comment (P2-AC-12); the manager may hear of it twice."""
        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            if row["delivery_state"] != "offered" or row["delivery_id"] != delivery_id:
                raise self._invalid(row, "the delivery is not the one on offer")
            self._comment(tx, request_id, "system", REQUEUED_COMMENT)
            new = self._set(tx, row, delivery_state="pending", delivery_id=None, delivery_backend=None,
                            delivery_at=None)
            tx.event(request_id, "requeued", row["state"], row["state"],
                     {"delivery_id": delivery_id, "reason": reason, "backend": row["delivery_backend"]},
                     version=new["version"])
            return {"state": new["state"], "version": new["version"]}
        return self._hub_change(request_id, fn, wait)

    def delivery_view(self, project_id: str) -> dict:
        """Dispatcher read: ``{review_mode, requests}`` where ``requests`` are the rows with a pending or
        offered round in ``seq`` order, each with its notice ``event``, ``excerpt`` and ``marked`` (the owner's
        "검토 시작" for this round). A missing inbox is ``auto`` with no requests."""
        project = self._project(Principal("hub"), project_id)

        def fn(connection: sqlite3.Connection) -> dict:
            self._remember(project, connection)
            connection.row_factory = sqlite3.Row
            mode = connection.execute("SELECT value FROM meta WHERE key = 'review_mode'").fetchone()
            rows = connection.execute(
                "SELECT * FROM requests WHERE delivery_state IN ('pending', 'offered') ORDER BY seq").fetchall()
            sources = tuple(NOTICE_EVENT_SOURCES)
            marks = ",".join("?" * len(sources))
            requests = []
            for row in rows:
                last = connection.execute(
                    f"SELECT type FROM events WHERE request_id = ? AND type IN ({marks}) "
                    "ORDER BY event_id DESC LIMIT 1", (row["request_id"], *sources)).fetchone()
                marked = connection.execute(
                    "SELECT 1 FROM events WHERE request_id = ? AND type = 'review_started' "
                    "AND json_extract(detail, '$.review_round') = ? LIMIT 1",
                    (row["request_id"], row["review_round"])).fetchone() is not None
                event = NOTICE_EVENT_SOURCES.get(last["type"] if last else "submitted", "new")
                if row["state"] == "CLOSED":
                    event = "cancelled"
                elif row["state"] in LINKED and event not in ("revised", "linked_task_closed"):
                    event = "revised"
                elif row["state"] in PRE_DISPATCH and event in ("revised", "linked_task_closed", "cancelled"):
                    event = "new"
                requests.append({
                    "request_id": row["request_id"], "seq": row["seq"], "state": row["state"],
                    "version": row["version"], "title": row["title"], "excerpt": row["body"][:EXCERPT_MAX],
                    "origin": origin_json(row["origin_kind"], row["origin_key_id"], row["origin_name"]),
                    "review_round": row["review_round"], "delivery_state": row["delivery_state"],
                    "delivery_id": row["delivery_id"], "delivery_backend": row["delivery_backend"],
                    "event": event, "marked": marked})
            return {"review_mode": mode[0] if mode else "auto", "requests": requests}
        return self._run(project, lambda: project.store.read(fn, missing={"review_mode": "auto", "requests": []}))

    def linked_view(self, project_id: str) -> list[dict]:
        """Task sync read (TI-11): the open requests that link a Task, in ``seq`` order, each
        ``{request_id, task_id, state}``. A missing inbox has none."""
        project = self._project(Principal("hub"), project_id)

        def fn(connection: sqlite3.Connection) -> list[dict]:
            self._remember(project, connection)
            rows = connection.execute(
                "SELECT request_id, task_id, state FROM requests WHERE task_id IS NOT NULL AND state != 'CLOSED' "
                "ORDER BY seq").fetchall()
            return [{"request_id": row[0], "task_id": row[1], "state": row[2]} for row in rows]
        return self._run(project, lambda: project.store.read(fn, missing=[]))

    def observe_task(self, request_id: str, task_id: str, status: str, *, closed_reason: str | None = None,
                     user_cancelled: bool = False, view: dict | None = None, wait: bool = True) -> Any:
        """Task sync for a READY/RUNNING request: READY → RUNNING when the Task starts, CLOSED(cancelled)
        when a user cancel closed it, READY → REVIEWING when the link was never confirmed, otherwise the
        state stays and a ``linked_task_closed`` event is emitted once (P2-AC-09)."""
        for name, value, optional in (("task_id", task_id, False), ("status", status, False),
                                      ("closed_reason", closed_reason, True)):
            if not (optional and value is None) and (not isinstance(value, str) or not 0 < len(value) <= 128):
                raise InboxError("invalid_input", f"{name} must be 1-128 characters")
        if view is not None and not isinstance(view, dict):
            raise InboxError("invalid_input", "view must be an object")
        try:
            view_size = len(json.dumps(dict(view or {}, status=status, closed=True, closed_reason=closed_reason),
                                       ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise InboxError("invalid_input", f"view is not JSON: {exc}") from None
        if view_size > TASK_VIEW_MAX_BYTES:
            raise InboxError("invalid_input", f"task_view exceeds {TASK_VIEW_MAX_BYTES} bytes")

        def fn(tx: _Tx, row: sqlite3.Row) -> dict:
            state = row["state"]
            # A REVIEWING/NEEDS_INFO request that still links the Task only follows a user cancel.
            follows = state in LINKED or (state in PRE_DISPATCH and status == "closed" and user_cancelled)
            if not follows or row["task_id"] != task_id:
                return {"state": state, "version": row["version"], "changed": False}
            old_view = json.loads(row["task_view"]) if row["task_view"] else {}
            new_view = dict(view or {}, status=status)
            command = new_view.get("command") if isinstance(new_view.get("command"), dict) else None
            old_command = old_view.get("command") if isinstance(old_view.get("command"), dict) else {}
            if (row["cancel_state"] is not None and command is not None and command.get("state") == "unknown"
                    and old_command.get("state") != "unknown"):
                self._comment(tx, request_id, "system", STOP_UNKNOWN_COMMENT)  # P2-AC-30: never a success
            if status == "closed":
                new_view.update(closed=True, closed_reason=closed_reason)
                if user_cancelled and command is not None:
                    # TI-13: the user-cancelled Task is closed but its command has not ended yet (being stopped, or
                    # its stop ended unknown): the request waits for the end; no linked_task_closed either
                    if old_view == new_view:
                        return {"state": state, "version": row["version"], "changed": False}
                    new = self._set(tx, row, task_view=json.dumps(new_view))
                    return {"state": new["state"], "version": new["version"], "changed": True}
                if user_cancelled:
                    tx.connection.execute(
                        "UPDATE links SET state = 'closed' WHERE request_id = ? AND state = 'active'", (request_id,))
                    # the manager hears of it through the dispatcher's ``cancelled`` notice (unless one is on offer)
                    told = {} if row["delivery_state"] == "offered" else {"delivery_state": "pending",
                                                                         "delivery_id": None}
                    new = self._set(tx, row, state="CLOSED", outcome="cancelled", cancel_state=None,
                                    task_view=json.dumps(new_view), **told)
                    tx.event(request_id, "cancelled", state, "CLOSED", {"task_id": task_id, "by": "user"},
                             version=new["version"])
                elif closed_reason == "inbox_link_unconfirmed" and state == "READY":
                    tx.connection.execute(
                        "UPDATE links SET state = 'void' WHERE request_id = ? AND state = 'active'", (request_id,))
                    self._comment(tx, request_id, "system", "Task 연결이 확인되지 않아 검토로 되돌렸습니다.")
                    new = self._set(tx, row, state="REVIEWING", task_id=None, spec_revision=None, task_view=None,
                                    cancel_state=None, **self._new_round(row))
                    tx.event(request_id, "link_voided", state, "REVIEWING", {"task_id": task_id},
                             version=new["version"])
                elif old_view.get("closed"):
                    if old_view == new_view:
                        return {"state": state, "version": row["version"], "changed": False}
                    new = self._set(tx, row, task_view=json.dumps(new_view))
                else:
                    new = self._set(tx, row, task_view=json.dumps(new_view), delivery_state="pending",
                                    delivery_id=None)
                    tx.event(request_id, "linked_task_closed", state, state,
                             {"task_id": task_id, "closed_reason": closed_reason}, version=new["version"])
                return {"state": new["state"], "version": new["version"], "changed": True}
            to = "RUNNING" if state == "READY" and status in RUNNING_TASK_STATUSES else state
            if to == state and old_view == new_view:
                return {"state": state, "version": row["version"], "changed": False}
            new = self._set(tx, row, state=to, task_view=json.dumps(new_view))
            if to != state:
                tx.event(request_id, "started", state, to, {"task_id": task_id, "status": status},
                         version=new["version"])
            return {"state": to, "version": new["version"], "changed": True}
        return self._hub_change(request_id, fn, wait)

