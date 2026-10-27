"""WBX-B6: the transport, app and process-level behaviours that ``test_hub_mcp*.py`` checked, over ``/api/v1/``.

Seam S2 (real loopback Hub web app + stdlib REST client) plus S3 for the real-Hub smoke. A port: the expected
values are the original tests' (SPEC "API key" last-use write every 10 s, "동시성": the API work runs on its own
bounded executor, one key has at most 4 calls answered at once and further calls wait, Hub stop cancels queued
work; the web app waits for a last-use flush in flight). JSON-RPC framing, sessions, protocol versions and the
loopback ``/mcp`` listener have no REST counterpart here (see the WBX-B6 mapping table). Run with
``discover -s tests/hub -p 'test_hub_rest*.py'``.
"""
from __future__ import annotations

import asyncio
import http.client
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from aiohttp import test_utils

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.web_sandbox import WebSandbox  # noqa: E402
from support.rest_client import RestClient  # noqa: E402
from support.rest_hub import PASSWORD, RestHub  # noqa: E402
from workbench.backend.project import initialize_project, resolve_project_context  # noqa: E402
from workbench.hub import auth, inbox_api  # noqa: E402
from workbench.hub.inbox.service import InboxService  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


class LastUsedPortTests(unittest.TestCase):
    """SPEC API key: ``last_used_at`` is gathered in memory and written every 10 s (shortened here)."""

    def test_a_rest_call_writes_the_last_use(self):
        hub = RestHub(self, flush_interval=0.2)
        key = hub.key('r1')
        self.assertEqual(hub.key_row(key)[0], None)
        self.assertEqual(hub.rest(key).get('/projects').status, 200)
        self.assertTrue(wait_until(lambda: hub.key_row(key)[0] == hub.now[0]), hub.key_row(key))

    def test_refused_keys_do_not_mark_a_last_use(self):
        hub = RestHub(self, flush_interval=0.2)
        revoked = hub.key('gone')
        hub.keys.revoke(revoked[4:14], now=hub.now[0])
        self.assertEqual(hub.rest(revoked).get('/projects').status, 401)
        self.assertEqual(hub.rest(revoked + 'x').get('/projects').status, 401)
        time.sleep(1.0)
        self.assertIsNone(hub.key_row(revoked)[0])
        live = hub.key('live')
        self.assertEqual(hub.rest(live).get('/projects').status, 200)
        self.assertTrue(wait_until(lambda: hub.key_row(live)[0] is not None))

    def test_last_use_is_flushed_within_about_ten_seconds_by_default(self):
        hub = RestHub(self)
        key = hub.key('r')
        self.assertIsNone(hub.key_row(key)[0])
        hub.rest(key).get('/projects')
        hub.now[0] += 3.0
        marker = hub.now[0]
        hub.rest(key).get('/requests')
        seen = None
        deadline = time.monotonic() + 14.0
        while time.monotonic() < deadline:
            seen = hub.key_row(key)[0]
            if seen is not None and seen >= marker:
                break
            time.sleep(0.25)
        self.assertIsNotNone(seen, 'last_used_at was never written within 14 s')
        self.assertGreaterEqual(seen, marker)


class _SlowFlush:
    """An ``ApiKeyStore`` stand-in whose flush takes a while (a slow ``hub.sqlite3`` write)."""

    def __init__(self):
        self.started, self.finished = threading.Event(), threading.Event()

    def flush(self) -> int:
        self.started.set()
        time.sleep(0.5)
        self.finished.set()
        return 1


class ShutdownPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_shutdown_waits_for_a_flush_in_flight(self):
        temp = tempfile.TemporaryDirectory(prefix='wb-rest-flush-')
        self.addCleanup(temp.cleanup)
        state = Path(temp.name) / 'hub'
        db = auth.HubDb.open(state)
        self.addCleanup(db.close)
        services = WebServices(auth.PasswordStore(state), db, OriginPolicy('127.0.0.1'), flush_interval=0.05)
        services.api_keys = slow = _SlowFlush()
        server = test_utils.TestServer(create_app(services), host='127.0.0.1', port=0)
        await server.start_server()
        self.assertTrue(await asyncio.to_thread(slow.started.wait, 5))
        await server.close()
        self.assertTrue(slow.finished.is_set(), 'the app stopped while a last-use flush was still writing')


class PoolShutdownPortTests(unittest.TestCase):
    """Hub stop shuts the API pool and an app's result-read pool down: queued work cancelled, nothing waited for."""

    def test_close_cancels_queued_api_work_and_refuses_new_work(self):
        inbox = InboxService()
        self.addCleanup(inbox.close)
        api = inbox_api.InboxApi(inbox, version='test')
        release = threading.Event()
        self.addCleanup(release.set)
        started = threading.Semaphore(0)

        def hold():
            started.release()
            return release.wait(10)
        running = [api.executor.submit(hold) for _ in range(inbox_api.MCP_WORKERS)]
        for _ in running:
            self.assertTrue(started.acquire(timeout=10))
        queued = api.executor.submit(lambda: 'never')
        api.close()  # returns at once although every worker is busy
        self.assertTrue(queued.cancelled())
        with self.assertRaises(RuntimeError):
            api.executor.submit(lambda: None)
        release.set()
        self.assertTrue(all(future.result(10) for future in running))

    def test_app_cleanup_shuts_the_result_read_pool(self):
        from aiohttp import web
        from workbench.hub.web.pages import requests as request_pages
        inbox = InboxService()
        self.addCleanup(inbox.close)
        app = web.Application()
        request_pages.routes(inbox, lambda: [])(app)
        reads = app[request_pages.PORT].reads

        async def cycle():
            runner = web.AppRunner(app)
            await runner.setup()
            await runner.cleanup()
        asyncio.run(cycle())
        with self.assertRaises(RuntimeError):
            reads.submit(lambda: None)


class ExecutorPortTests(unittest.TestCase):
    """Security review fix 2: REST work (key checks and calls) runs on its own bounded executor, never on the loop's
    shared default executor that logins and session checks use; one key has at most 4 calls in flight and further
    calls wait for a slot (no request is refused at once, nothing counts requests: no rate limit)."""

    def setUp(self):
        from concurrent.futures import ThreadPoolExecutor
        self.hub = RestHub(self)
        self.key = self.hub.key('r1')
        # a small shared default executor: REST work running there would starve logins and session checks
        small = ThreadPoolExecutor(2)
        self.addCleanup(small.shutdown, wait=False)
        self.hub.run(self._set_default(small))
        self.release = threading.Event()
        self.entered = []
        original = self.hub.inbox.list_requests
        self.running, self.peak, self.count_lock = 0, 0, threading.Lock()

        def blocked(*args, **kwargs):
            with self.count_lock:
                self.running += 1
                self.peak = max(self.peak, self.running)
            self.entered.append(1)
            try:
                self.release.wait(20)
                return original(*args, **kwargs)
            finally:
                with self.count_lock:
                    self.running -= 1
        patcher = mock.patch.object(self.hub.inbox, 'list_requests', side_effect=blocked)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.threads = []
        self.addCleanup(self._finish)  # first: free the blocked calls, then join their threads

    def _finish(self) -> None:
        self.release.set()
        for thread in self.threads:
            thread.join(30)

    @staticmethod
    async def _set_default(executor):
        asyncio.get_running_loop().set_default_executor(executor)

    def call_in_background(self, key: str) -> dict:
        box: dict = {}

        def run():
            box['reply'] = self.hub.rest(key, timeout=30).get('/requests')
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.threads.append(thread)
        return box

    def wait_entered(self, count: int) -> None:
        deadline = time.monotonic() + 10
        while len(self.entered) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(len(self.entered), count, 'the blocked calls did not start')

    def login_and_session_page(self) -> float:
        """Seconds for GET /login, POST /login and a session page (scrypt and session checks)."""
        started = time.monotonic()
        host = f'127.0.0.1:{self.hub.port}'
        connection = http.client.HTTPConnection('127.0.0.1', self.hub.port, timeout=10)
        try:
            connection.request('GET', '/login', headers={'Host': host})
            response = connection.getresponse()
            page = response.read().decode()
            cookie = re.search(r'wb_hub_login=([^;]+)', response.getheader('Set-Cookie')).group(1)
            token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
            body = f'login_token={token}&password={PASSWORD.replace(" ", "+")}'
            connection.request('POST', '/login', body=body, headers={
                'Host': host, 'Origin': f'http://{host}', 'Cookie': f'wb_hub_login={cookie}',
                'Content-Type': 'application/x-www-form-urlencoded'})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 303)
            session = re.search(r'wb_hub_session=([^;]+)', response.getheader('Set-Cookie')).group(1)
            connection.request('GET', '/settings', headers={'Host': host, 'Cookie': f'wb_hub_session={session}'})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
        finally:
            connection.close()
        return time.monotonic() - started

    def test_login_and_session_pages_answer_while_the_api_is_saturated(self):
        keys = [self.hub.key(f'busy{index}') for index in range(3)]
        boxes = [self.call_in_background(key) for key in keys for _ in range(4)]
        self.wait_entered(2)
        self.assertLess(self.login_and_session_page(), 8, 'login waited for REST work')
        self.release.set()
        for thread in self.threads:
            thread.join(30)
        self.assertTrue(all(box['reply'].status == 200 and 'requests' in box['reply'].json for box in boxes),
                        [box.get('reply') and box['reply'].body[:200] for box in boxes])

    def test_eight_parallel_calls_of_one_key_all_complete_four_at_a_time_and_login_answers(self):
        self.assertEqual(self.hub.api.busy_wait, 30.0)
        boxes = [self.call_in_background(self.key) for _ in range(8)]
        self.wait_entered(4)
        time.sleep(0.5)  # the other four wait for a slot; none is refused
        self.assertEqual((len(self.entered), self.peak), (4, 4))
        self.assertTrue(all('reply' not in box for box in boxes), 'a waiting call was answered')
        self.assertLess(self.login_and_session_page(), 8, 'login waited for REST work')
        self.release.set()
        for thread in self.threads:
            thread.join(30)
        self.assertTrue(all(box['reply'].status == 200 and 'requests' in box['reply'].json for box in boxes),
                        [box.get('reply') and box['reply'].body[:200] for box in boxes])
        self.assertEqual((len(self.entered), self.peak), (8, 4))


class RealLoginCookiePortTests(unittest.TestCase):
    """An owner session made by the real ``GET/POST /login`` is not a credential on ``/api/v1/``."""

    def setUp(self):
        self.hub = RestHub(self)

    def login_cookie(self) -> str:
        """A real owner session through ``GET/POST /login`` (own http.client, no harness shortcut)."""
        def call(method, path, body=None, cookie=None):
            connection = http.client.HTTPConnection('127.0.0.1', self.hub.port, timeout=15)
            headers = {'Host': f'127.0.0.1:{self.hub.port}'}
            if cookie:
                headers['Cookie'] = cookie
            if body is not None:
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
            try:
                connection.request(method, path, body, headers)
                response = connection.getresponse()
                return response.status, response.getheaders(), response.read().decode()
            finally:
                connection.close()

        status, headers, page = call('GET', '/login')
        self.assertEqual(status, 200)
        jar = {}
        for name, value in headers:
            if name.lower() == 'set-cookie':
                pair = value.split(';', 1)[0]
                jar[pair.split('=', 1)[0]] = pair.split('=', 1)[1]
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        status, headers, _ = call('POST', '/login', urlencode({'login_token': token, 'password': PASSWORD}),
                                  '; '.join(f'{k}={v}' for k, v in jar.items()))
        self.assertEqual(status, 303)
        session = [value.split(';', 1)[0] for name, value in headers
                   if name.lower() == 'set-cookie' and value.startswith('wb_hub_session=')]
        self.assertTrue(session, 'login produced a session cookie')
        return session[0]

    def test_a_real_login_session_cookie_is_not_a_credential(self):
        cookie = self.login_cookie()
        rid = '00000000-0000-4000-8000-000000000000'
        for headers in ({'Cookie': cookie}, {'Cookie': cookie, 'Authorization': 'Bearer wbk_wrong'}):
            with self.subTest(headers=list(headers)):
                client = self.hub.rest(None)
                for method, path, value in (('GET', '/projects', None), ('GET', '/requests', None),
                                            ('GET', f'/requests/{rid}', None),
                                            ('POST', '/requests', {'project_id': self.hub.a, 'title': 't',
                                                                   'body': 'b', 'idempotency_key': 'k'}),
                                            ('POST', f'/requests/{rid}/cancel', {})):
                    reply = client.call(method, path, value, headers)
                    self.assertEqual((reply.status, reply.json['error']['code']), (401, 'unauthorized'), reply.body)
        self.assertEqual(self.hub.inbox_rows('SELECT count(*) FROM requests'), [])

    def test_a_cookie_next_to_a_valid_key_changes_nothing(self):
        # the key's grade decides, not the owner session riding along
        cookie = self.login_cookie()
        client = self.hub.rest(self.hub.key('r', scope={'projects': [self.hub.a]}))
        reply = client.post('/requests', {'project_id': self.hub.a, 'title': 't', 'body': 'b',
                                          'idempotency_key': 'k'}, {'Cookie': cookie})
        self.assertEqual(reply.status, 201, reply.body)
        self.assertEqual(self.hub.inbox_rows('SELECT origin_kind, origin_name FROM requests'), [('api_key', 'r')])
        rid = reply.json['request_id']
        denied = client.patch(f'/requests/{rid}', {'base_version': 1, 'title': 'x'}, {'Cookie': cookie})
        self.assertEqual((denied.status, denied.json['error']['code']), (403, 'forbidden'))
        self.assertEqual([item['name'] for item in client.get('/projects', {'Cookie': cookie}).json['projects']],
                         ['alpha'])


class RealHubRestPortTests(unittest.TestCase):
    """TI-05 smoke on a real ``wb _hub --foreground`` (seam S3): the Hub itself serves ``/api/v1/``.

    Password set with ``wb hub passwd``, a real initialized project registered with ``wb hub add``, an API key
    issued straight into the Hub's ``hub.sqlite3`` through ``ApiKeyStore``, then the stdlib REST client over
    127.0.0.1: projects -> submit -> get -> unauthenticated 401, and the stored row after the Hub stopped."""

    def test_projects_submit_get(self):
        box = WebSandbox(self)
        box.write_config()
        box.set_password()
        root = box.base / 'work' / 'alpha'
        root.mkdir(parents=True)
        root = root.resolve()
        initialize_project(resolve_project_context(root, box.env), confirm=lambda _root: True, interactive=True,
                           environment=box.env)
        project_id = json.loads((root / '.wb' / 'project.json').read_text())['project_id']
        hub = box.start_hub()
        self.assertEqual(box.cli('hub', 'add', str(root)).returncode, 0)
        host, port = box.wait_listening(hub.pid)
        self.assertEqual(host, '127.0.0.1')

        db = auth.HubDb.open(box.state)
        try:
            key = auth.ApiKeyStore(db).issue('smoke', 'requester', {'projects': [project_id]}, None,
                                             now=time.time()).raw
        finally:
            db.close()

        client = RestClient(host, port, key)
        self.assertEqual(client.get('/projects').json, {'projects': [{'project_id': project_id, 'name': 'alpha'}]})
        submitted = client.post('/requests', {'project_id': project_id, 'title': 'From REST', 'body': 'Smoke body',
                                              'idempotency_key': 'smoke-1'})
        self.assertEqual(submitted.status, 201, submitted.body)
        self.assertEqual((submitted.json['state'], submitted.json['created']), ('NEW', True))
        request = client.get(f"/requests/{submitted.json['request_id']}").json['request']
        self.assertEqual((request['title'], request['body'], request['origin']['label']),
                         ('From REST', 'Smoke body', 'key:smoke'))
        self.assertEqual(RestClient(host, port, None).get('/projects').status, 401)

        self.assertEqual(box.stop_hub(hub), 0)
        connection = sqlite3.connect(f'file:{root / ".wb" / "inbox.sqlite3"}?mode=ro', uri=True)
        try:
            rows = connection.execute('SELECT request_id, origin_name FROM requests').fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [(submitted.json['request_id'], 'smoke')])


if __name__ == '__main__':
    unittest.main()
