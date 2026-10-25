"""A real loopback Hub web app with ``/mcp`` for MCP tests (seam T4 server side).

``McpHub(case)`` builds, in temp directories: ``auth.json`` + ``hub.sqlite3`` (real ``auth`` stores),
two WB projects (``alpha``, ``beta``) with real ``.wb/inbox.sqlite3`` through a real ``InboxService``,
and serves ``create_app(services, routes=[mcp.routes(server)])`` on ``127.0.0.1:0`` from its own
event-loop thread, so blocking stdlib clients can call it. The clock (``hub.now``) and the token
clock (``hub.mono``) are injectable; ``hub.cancels`` records every ``cancel_requested`` commit (the
cancel port, TI-13 fills the real one) and ``hub.backend_up`` is what ``backend_running`` answers.
Everything is stopped and removed by ``addCleanup``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
import uuid

from aiohttp import web

from workbench.hub import auth, mcp
from workbench.hub.inbox.service import InboxService, Principal, ProjectRef
from workbench.hub.web.app import OriginPolicy, WebServices, create_app

from .mcp_client import McpClient

PASSWORD = 'correct horse battery'


def make_project(base: Path, name: str) -> tuple[Path, str]:
    root = base / name
    root.mkdir(parents=True)
    root = root.resolve()
    data = root / '.wb'
    data.mkdir(mode=0o700)
    os.chmod(data, 0o700)
    project_id = str(uuid.uuid4())
    descriptor = os.open(data / 'project.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump({'schema_version': 1, 'project_id': project_id, 'project_root': str(root),
                   'state': 'completed'}, stream)
    return root, project_id


class McpHub:
    def __init__(self, case: unittest.TestCase, *, flush_interval: float = 10.0, token_ttl: float = 60.0):
        self.case = case
        self.base = Path(tempfile.mkdtemp(prefix='wb-mcp-')).resolve()
        case.addCleanup(shutil.rmtree, self.base, True)
        self.now = [1_000_000.0]
        self.mono = [5_000.0]
        self.backend_up = True
        self.cancels: list = []
        self.root_a, self.a = make_project(self.base, 'alpha')
        self.root_b, self.b = make_project(self.base, 'beta')
        self.inbox = InboxService(on_committed=self._committed, backend_running=lambda project_id: self.backend_up,
                                  clock=lambda: self.now[0])
        case.addCleanup(self.inbox.close)
        self.inbox.set_projects([ProjectRef(self.a, self.root_a, 'alpha'), ProjectRef(self.b, self.root_b, 'beta')])
        self.state = self.base / 'hub'
        self.passwords = auth.PasswordStore(self.state)
        self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(self.state)
        case.addCleanup(self.db.close)
        self.rows = [self.row(self.a, self.root_a, 'alpha'), self.row(self.b, self.root_b, 'beta')]
        self.server = mcp.McpServer(self.inbox, projects=self._projects, monotonic=lambda: self.mono[0],
                                    token_ttl=token_ttl)
        self.services = WebServices(self.passwords, self.db, OriginPolicy('127.0.0.1'), clock=lambda: self.now[0],
                                    flush_interval=flush_interval)
        self.keys = self.services.api_keys
        self._serve()

    @staticmethod
    def row(project_id: str, root: Path, name: str) -> dict:
        return {'root': str(root), 'name': name, 'project_id': project_id, 'power': 'off', 'power_detail': None,
                'task': None, 'command': None, 'waits': [], 'pending_requests': None, 'review_mode': 'auto',
                'last_activity': None, 'backend': None, 'observed_at': 0.0}

    async def _projects(self) -> list[dict]:
        return [dict(row) for row in self.rows]

    def _committed(self, event) -> None:
        if event.type == 'cancel_requested':
            self.cancels.append(event)

    # --- server thread ---------------------------------------------------------------------------

    def _serve(self) -> None:
        self.loop = asyncio.new_event_loop()
        started = threading.Event()
        failure: list = []

        async def start() -> None:
            self.runner = web.AppRunner(create_app(self.services, routes=[mcp.routes(self.server)]),
                                        access_log=None, handle_signals=False)
            await self.runner.setup()
            site = web.TCPSite(self.runner, '127.0.0.1', 0)
            await site.start()
            self.port = self.runner.addresses[0][1]

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(start())
            except BaseException as exc:  # reported to the test thread
                failure.append(exc)
                started.set()
                return
            started.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, name='mcp-hub', daemon=True)
        self.thread.start()
        started.wait(15)
        if failure:
            raise failure[0]
        self.case.addCleanup(self._stop)

    def _stop(self) -> None:
        future = asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop)
        try:
            future.result(15)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(15)
            self.loop.close()

    def run(self, coroutine, timeout: float = 15):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    # --- keys and clients ------------------------------------------------------------------------

    def key(self, name: str, grade: str = 'requester', scope: dict | None = None,
            expires_days: int | None = None) -> str:
        return self.keys.issue(name, grade, scope or {'all': True}, expires_days, now=self.now[0]).raw

    def client(self, key: str | None, **kwargs) -> McpClient:
        return McpClient('127.0.0.1', self.port, key, **kwargs)

    def key_row(self, key: str) -> tuple:
        key_id = key[4:14]
        return self.db.query('SELECT last_used_at, revoked_at FROM api_keys WHERE key_id = ?', (key_id,))[0]

    # --- direct inbox access (manager/hub side, as a test fixture) ---------------------------------

    def link_running(self, request_id: str, project_id: str | None = None, *, task_id: str = 'task-1',
                     status: str = 'running', view: dict | None = None) -> None:
        """Move a request to READY/RUNNING the way a manager and the Task sync would."""
        manager = Principal.manager(project_id or self.a)
        self.inbox.review(manager, request_id, 'begin_review')
        token = self.inbox.link_check(manager, request_id)['link_token']
        self.inbox.link_commit(manager, token, task_id, 1, status)
        if view is not None:
            self.inbox.observe_task(request_id, task_id, status, view=view)

    def inbox_rows(self, sql: str, *args, root: Path | None = None) -> list[tuple]:
        import sqlite3
        db = (root or self.root_a) / '.wb' / 'inbox.sqlite3'
        if not db.exists():
            return []
        connection = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()
