"""WBX-B1 ``/api/v1/`` REST through a real loopback Hub web app and the stdlib REST client (seam S2).

Expected values: the web-board-ux SPEC "외부 요청 API" (endpoint table, error -> status table, body/query rules,
Origin, concurrency, catch-all), the task-inbox SPEC "Principal과 권한", "입력 한도", "취소", "결과 조회", and rows
the test reads back with stdlib ``sqlite3``. Run with ``discover -s tests/hub -p 'test_hub_rest*.py'``.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from support.rest_hub import RestHub  # noqa: E402
from workbench.hub.inbox.limits import InboxError  # noqa: E402

WWW_AUTHENTICATE = 'Bearer realm="workbench-hub"'


class RestCase(unittest.TestCase):
    hub_options: dict = {}

    def setUp(self):
        self.hub = RestHub(self, **self.hub_options)

    def assert_error(self, reply, status: int, code: str) -> dict:
        self.assertEqual(reply.status, status, reply.body)
        self.assertEqual(reply.header('content-type').split(';')[0], 'application/json')
        self.assertEqual(reply.header('cache-control'), 'no-store')
        error = reply.json['error']
        self.assertEqual(error['code'], code, reply.body)
        self.assertIsInstance(error['message'], str)
        self.assertTrue(set(error) <= {'code', 'message', 'state', 'version'}, error)
        return error

    def request_count(self, root=None) -> int:
        rows = self.hub.inbox_rows('SELECT count(*) FROM requests', root=root)
        return rows[0][0] if rows else 0


class AuthTests(RestCase):
    """AC-08: Bearer only; none, wrong, revoked or expired is 401 + ``WWW-Authenticate``; a cookie alone is 401."""

    def assert_unauthorized(self, reply):
        error = self.assert_error(reply, 401, 'unauthorized')
        self.assertEqual(error['message'], 'unauthorized')
        self.assertEqual(reply.header('www-authenticate'), WWW_AUTHENTICATE)

    def test_without_a_key_every_endpoint_is_401(self):
        client = self.hub.rest(None)
        rid = '00000000-0000-4000-8000-000000000000'
        for method, path in (('GET', '/projects'), ('GET', '/requests'), ('POST', '/requests'),
                             ('GET', f'/requests/{rid}'), ('PATCH', f'/requests/{rid}'),
                             ('POST', f'/requests/{rid}/comments'), ('POST', f'/requests/{rid}/state'),
                             ('POST', f'/requests/{rid}/cancel'), ('GET', f'/requests/{rid}/result')):
            with self.subTest(method=method, path=path):
                value = None if method == 'GET' else {}
                self.assert_unauthorized(client.call(method, path, value))

    def test_wrong_malformed_and_non_bearer_keys_are_401(self):
        real = self.hub.key('r1')
        forged = real[:15] + ('A' if real[15] != 'A' else 'B') + real[16:]
        client = self.hub.rest(None)
        for value in (f'Bearer {forged}', 'Bearer wbk_nope', 'Bearer not-a-key', f'Bearer {real}x',
                      f'Basic {real}', real, f'Bearer  {real}', 'Bearer'):
            with self.subTest(value=value[:16]):
                self.assert_unauthorized(client.get('/projects', headers={'Authorization': value}))

    def test_two_authorization_headers_are_401(self):
        real = self.hub.key('r1')
        reply = self.hub.rest(None).get('/projects', headers={'Authorization': [f'Bearer {real}', f'Bearer {real}']})
        self.assert_unauthorized(reply)

    def test_revoked_key_is_401_from_the_next_call(self):
        key = self.hub.key('r1')
        self.assertEqual(self.hub.rest(key).get('/projects').status, 200)
        self.hub.keys.revoke(key[4:14], now=self.hub.now[0])
        self.assert_unauthorized(self.hub.rest(key).get('/projects'))

    def test_expired_key_is_401(self):
        key = self.hub.key('r1', expires_days=1)
        self.assertEqual(self.hub.rest(key).get('/projects').status, 200)
        self.hub.now[0] += 86400.0
        self.assert_unauthorized(self.hub.rest(key).get('/projects'))

    def test_a_session_cookie_alone_is_401_and_never_counts(self):
        issued = self.hub.services.sessions.create('127.0.0.1', 'ua', self.hub.now[0], generation=1)
        cookie = {'Cookie': f'wb_hub_session={issued.token}'}
        self.assert_unauthorized(self.hub.rest(None).get('/projects', headers=cookie))
        self.assert_unauthorized(self.hub.rest(None).post('/requests', {'project_id': self.hub.a, 'title': 't',
                                                                         'body': 'b', 'idempotency_key': 'k'},
                                                          headers={**cookie, 'X-CSRF-Token': issued.csrf}))
        self.assertEqual(self.request_count(), 0)

    def test_cookie_plus_a_bad_key_is_401_and_cookie_plus_a_good_key_is_the_key(self):
        issued = self.hub.services.sessions.create('127.0.0.1', 'ua', self.hub.now[0], generation=1)
        cookie = f'wb_hub_session={issued.token}'
        self.assert_unauthorized(self.hub.rest('wbk_nope').get('/projects', headers={'Cookie': cookie}))
        key = self.hub.key('r1')
        reply = self.hub.rest(key).post('/requests', {'project_id': self.hub.a, 'title': 't', 'body': 'b',
                                                      'idempotency_key': 'k1'}, headers={'Cookie': cookie})
        self.assertEqual(reply.status, 201, reply.body)
        self.assertEqual((reply.json['origin']['kind'], reply.json['origin']['label']), ('api_key', 'key:r1'))


class OriginHostTests(RestCase):
    """SPEC: a present ``Origin`` must be the listener's own origin (403 ``origin_mismatch``, REST shape, before the
    key check); the web Host rule stays (421)."""

    def submit_body(self, key='k1'):
        return {'project_id': self.hub.a, 'title': 't', 'body': 'b', 'idempotency_key': key}

    def test_a_foreign_origin_is_403_on_reads_and_writes_and_nothing_is_stored(self):
        key = self.hub.key('e1', 'editor')
        client = self.hub.rest(key)
        for origin in ('http://evil.example', 'null', f'http://127.0.0.1:{self.hub.port + 1}',
                       f'https://127.0.0.1:{self.hub.port}', f'http://127.0.0.1:{self.hub.port}/x'):
            with self.subTest(origin=origin):
                self.assert_error(client.get('/projects', headers={'Origin': origin}), 403, 'origin_mismatch')
                self.assert_error(client.post('/requests', self.submit_body(), headers={'Origin': origin}),
                                  403, 'origin_mismatch')
        self.assertEqual(self.request_count(), 0)

    def test_two_origin_headers_are_403(self):
        own = f'http://127.0.0.1:{self.hub.port}'
        reply = self.hub.rest(self.hub.key('r1')).get('/projects', headers={'Origin': [own, own]})
        self.assert_error(reply, 403, 'origin_mismatch')

    def test_origin_is_checked_before_the_key(self):
        reply = self.hub.rest(None).get('/projects', headers={'Origin': 'http://evil.example'})
        self.assert_error(reply, 403, 'origin_mismatch')

    def test_the_own_origin_or_none_is_served(self):
        client = self.hub.rest(self.hub.key('r1'))
        own = f'http://127.0.0.1:{self.hub.port}'
        self.assertEqual(client.get('/projects', headers={'Origin': own}).status, 200)
        self.assertEqual(client.post('/requests', self.submit_body(), headers={'Origin': own}).status, 201)
        self.assertEqual(client.get('/projects').status, 200)

    def test_a_foreign_host_is_421(self):
        client = self.hub.rest(self.hub.key('r1'), host_header='evil.example')
        reply = client.get('/projects')
        self.assertEqual(reply.status, 421, reply.body)


RID = '00000000-0000-4000-8000-000000000000'


class TransportTests(RestCase):
    """SPEC body/method rules: 405 + ``Allow`` (``invalid_input``) after the key check, 415 for a non-JSON type,
    413 ``too_large`` over 256 KiB (declared or read), 400 ``invalid_input`` for anything but one strict UTF-8 JSON
    object and for unknown fields; ``null`` optional fields count as absent."""

    def setUp(self):
        super().setUp()
        self.key = self.hub.key('e1', 'editor')
        self.client = self.hub.rest(self.key)

    def submit_body(self, key='k1', **extra):
        return {'project_id': self.hub.a, 'title': 't', 'body': 'b', 'idempotency_key': key, **extra}

    def test_other_methods_are_405_with_allow(self):
        cases = [('/projects', 'DELETE', 'GET'), ('/projects', 'POST', 'GET'), ('/requests', 'PUT', 'GET, POST'),
                 ('/requests', 'DELETE', 'GET, POST'), (f'/requests/{RID}', 'POST', 'GET, PATCH'),
                 (f'/requests/{RID}', 'DELETE', 'GET, PATCH'), (f'/requests/{RID}/comments', 'GET', 'POST'),
                 (f'/requests/{RID}/state', 'PATCH', 'POST'), (f'/requests/{RID}/cancel', 'GET', 'POST'),
                 (f'/requests/{RID}/result', 'POST', 'GET'), ('/requests', 'OPTIONS', 'GET, POST')]
        for path, method, allow in cases:
            with self.subTest(method=method, path=path):
                reply = self.client.call(method, path, {} if method not in ('GET', 'OPTIONS', 'DELETE') else None)
                self.assert_error(reply, 405, 'invalid_input')
                self.assertEqual(reply.header('allow'), allow)

    def test_the_key_is_checked_before_the_method(self):
        reply = self.hub.rest(None).call('DELETE', '/projects')
        self.assert_error(reply, 401, 'unauthorized')

    def test_a_non_json_content_type_is_415(self):
        body = json.dumps(self.submit_body()).encode()
        for value in ('text/plain', 'application/x-www-form-urlencoded', 'application/jsonx', None):
            with self.subTest(content_type=value):
                reply = self.client.raw('POST', '/api/v1/requests', body, {'Content-Type': value})
                self.assert_error(reply, 415, 'invalid_input')
        reply = self.client.raw('PATCH', f'/api/v1/requests/{RID}', b'{}', {'Content-Type': 'text/plain'})
        self.assert_error(reply, 415, 'invalid_input')
        self.assertEqual(self.request_count(), 0)

    def test_a_json_content_type_with_a_charset_is_accepted(self):
        body = json.dumps(self.submit_body()).encode()
        reply = self.client.raw('POST', '/api/v1/requests', body, {'Content-Type': 'application/json; charset=utf-8'})
        self.assertEqual(reply.status, 201, reply.body)

    def test_a_body_over_256_kib_is_413_declared_or_streamed(self):
        big = json.dumps(self.submit_body(body='x' * (256 * 1024))).encode()
        reply = self.client.raw('POST', '/api/v1/requests', big, {'Content-Type': 'application/json'})
        self.assert_error(reply, 413, 'too_large')
        reply = self.client.raw('POST', '/api/v1/requests', b'{}', {'Content-Type': 'application/json',
                                                                    'Content-Length': str(256 * 1024 + 1)})
        self.assert_error(reply, 413, 'too_large')
        chunks = b''.join(b'%x\r\n%s\r\n' % (len(part), part) for part in (big[:200000], big[200000:])) + b'0\r\n\r\n'
        reply = self.client.raw('POST', '/api/v1/requests', chunks, {'Content-Type': 'application/json',
                                                                     'Transfer-Encoding': 'chunked'})
        self.assert_error(reply, 413, 'too_large')
        self.assertEqual(self.request_count(), 0)

    def test_the_key_is_checked_before_the_body_limit(self):
        reply = self.hub.rest(None).raw('POST', '/api/v1/requests', b'{}', {'Content-Type': 'application/json',
                                                                            'Content-Length': str(300 * 1024)})
        self.assert_error(reply, 401, 'unauthorized')

    def test_anything_but_one_strict_json_object_is_400(self):
        deep = b'[' * 100000 + b']' * 100000
        for raw in (b'', b'not json', b'[]', b'"text"', b'1', b'null', b'{"title": NaN}', b'{"title": Infinity}',
                    b'{"base_version": 1e999}', b'{"title": "\xff\xfe"}', b'{"a": 1} {"b": 2}', deep,
                    b'{"body": "' + b'9' * 10 + b'"', b'{"idempotency_key": ' + b'9' * 5000 + b'}'):
            with self.subTest(raw=raw[:30]):
                reply = self.client.raw('POST', '/api/v1/requests', raw, {'Content-Type': 'application/json'})
                self.assert_error(reply, 400, 'invalid_input')
        self.assertEqual(self.request_count(), 0)

    def test_a_false_content_length_stores_nothing(self):
        # A short declared length breaks the HTTP framing: aiohttp's parser answers its own bare 400 for the bytes
        # after the declared body (no REST error shape is possible there); a long one never completes the body.
        body = json.dumps(self.submit_body()).encode()
        reply = self.client.raw('POST', '/api/v1/requests', body, {'Content-Type': 'application/json',
                                                                   'Content-Length': str(len(body) - 5)})
        self.assertEqual(reply.status, 400, reply.body)
        short = self.hub.rest(self.key, timeout=1.0)
        with self.assertRaises(OSError):  # the server waits for the missing bytes; the client gives up
            short.raw('POST', '/api/v1/requests', body, {'Content-Type': 'application/json',
                                                         'Content-Length': str(len(body) + 5)})
        self.assertEqual(self.request_count(), 0)

    def test_unknown_fields_and_path_fields_in_the_body_are_400(self):
        for path, method, body in (('/requests', 'POST', self.submit_body(extra=1)),
                                   ('/requests', 'POST', self.submit_body(request_id=RID)),
                                   (f'/requests/{RID}/comments', 'POST', {'body': 'x', 'request_id': RID}),
                                   (f'/requests/{RID}', 'PATCH', {'base_version': 1, 'request_id': RID}),
                                   (f'/requests/{RID}/state', 'POST', {'base_version': 1, 'to': 'NEW', 'x': None}),
                                   (f'/requests/{RID}/cancel', 'POST', {'confirm': 'x'})):
            with self.subTest(path=path, body=body):
                self.assert_error(self.client.call(method, path, body), 400, 'invalid_input')
        self.assertEqual(self.request_count(), 0)

    def test_wrong_types_are_400(self):
        for body in (self.submit_body(title=1), self.submit_body(project_id=['x']), self.submit_body(body=True)):
            with self.subTest(body=body):
                self.assert_error(self.client.post('/requests', body), 400, 'invalid_input')

    def test_a_null_optional_field_is_absent(self):
        rid = self.client.post('/requests', self.submit_body()).json['request_id']
        reply = self.client.post(f'/requests/{rid}/cancel', {'confirm_token': None, 'reason': None})
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.json, {'status': 'cancelled', 'state': 'CLOSED'})


class ProjectsTests(RestCase):
    """``GET /api/v1/projects``: the key's scope; requester keys see only id and name (task-inbox "Principal과 권한");
    ``query`` matches the name (editors: also the path) (AC-07)."""

    EDITOR_FIELDS = {'project_id', 'name', 'root', 'power', 'power_label', 'task', 'command', 'waits',
                     'pending_requests', 'review_mode', 'untrusted_text'}

    def test_requester_sees_only_id_and_name_in_scope(self):
        key = self.hub.key('r1', scope={'projects': [self.hub.a]})
        reply = self.hub.rest(key).get('/projects')
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.json, {'projects': [{'project_id': self.hub.a, 'name': 'alpha'}]})

    def test_editor_sees_the_status_fields_in_scope(self):
        self.hub.rows[0].update(power='on', review_mode='manual')
        key = self.hub.key('e1', 'editor', scope={'projects': [self.hub.a]})
        projects = self.hub.rest(key).get('/projects').json['projects']
        self.assertEqual([item['project_id'] for item in projects], [self.hub.a])
        self.assertEqual(set(projects[0]), self.EDITOR_FIELDS)
        self.assertEqual((projects[0]['power'], projects[0]['review_mode'], projects[0]['root']),
                         ('on', 'manual', str(self.hub.root_a)))

    def test_query_matches_the_name_and_for_editors_the_path(self):
        board = self.hub.keys.rotate_system_key(now=self.hub.now[0]).raw
        names = lambda reply: [item['name'] for item in reply.json['projects']]  # noqa: E731
        self.assertEqual(names(self.hub.rest(board).get('/projects', query='BET')), ['beta'])
        self.assertEqual(names(self.hub.rest(board).get('/projects', query=str(self.hub.root_a))), ['alpha'])
        requester = self.hub.rest(self.hub.key('r1'))
        self.assertEqual(requester.get('/projects', query=str(self.hub.root_a)).json, {'projects': []})
        self.assertEqual(names(requester.get('/projects', query='alp')), ['alpha'])

    def test_unknown_or_repeated_query_parameters_are_400(self):
        client = self.hub.rest(self.hub.key('r1'))
        for target in ('/api/v1/projects?q=alpha', '/api/v1/projects?query=a&query=b', '/api/v1/projects?limit=1'):
            with self.subTest(target=target):
                self.assert_error(client.raw('GET', target), 400, 'invalid_input')

    def test_an_over_long_query_is_400(self):
        reply = self.hub.rest(self.hub.key('r1')).get('/projects', query='x' * 201)
        self.assert_error(reply, 400, 'invalid_input')


class SubmitTests(RestCase):
    """``POST /api/v1/requests`` (AC-07, AC-09): 201 + ``Location`` when new, 200 and the same id for the same key
    and content, 409 ``idempotency_conflict`` for other content; field limits refused with nothing stored."""

    FIELDS = {'request_id', 'project_id', 'seq', 'state', 'origin', 'created'}

    def setUp(self):
        super().setUp()
        self.key = self.hub.key('r1', scope={'projects': [self.hub.a]})
        self.client = self.hub.rest(self.key)

    def body(self, key='k1', **extra):
        return {'project_id': self.hub.a, 'title': '제목', 'body': '본문', 'idempotency_key': key, **extra}

    def rows(self):
        return self.hub.inbox_rows('SELECT request_id, state, origin_kind, origin_name, title, body FROM requests '
                                   'ORDER BY seq')

    def test_a_new_request_is_201_with_location(self):
        reply = self.client.post('/requests', self.body())
        self.assertEqual(reply.status, 201, reply.body)
        value = reply.json
        self.assertEqual(set(value), self.FIELDS)
        self.assertEqual((value['project_id'], value['seq'], value['state'], value['created']),
                         (self.hub.a, 1, 'NEW', True))
        self.assertEqual(reply.header('location'), f"/api/v1/requests/{value['request_id']}")
        self.assertEqual(self.rows(), [(value['request_id'], 'NEW', 'api_key', 'r1', '제목', '본문')])

    def test_the_same_key_and_content_is_200_with_the_same_id(self):
        first = self.client.post('/requests', self.body()).json
        again = self.client.post('/requests', self.body())
        self.assertEqual(again.status, 200, again.body)
        self.assertIsNone(again.header('location'))
        self.assertEqual(again.json, {**first, 'created': False})
        self.assertEqual(len(self.rows()), 1)

    def test_the_same_key_with_other_content_is_409(self):
        self.client.post('/requests', self.body())
        error = self.assert_error(self.client.post('/requests', self.body(body='다른 본문')), 409,
                                  'idempotency_conflict')
        self.assertNotIn('본문', error['message'])
        self.assertEqual(len(self.rows()), 1)

    def test_field_limits_are_refused_and_nothing_is_stored(self):
        cases = [(self.body(title=''), 400, 'invalid_input'), (self.body(title='x' * 201), 413, 'too_large'),
                 (self.body(title='a\nb'), 400, 'invalid_input'), (self.body(body=''), 400, 'invalid_input'),
                 (self.body(body='x' * (64 * 1024 + 1)), 413, 'too_large'),
                 (self.body(key=''), 400, 'invalid_input'), (self.body(key='x' * 129), 400, 'invalid_input'),
                 (self.body(key='with space'), 400, 'invalid_input'),
                 (self.body(project_id=None), 400, 'invalid_input')]
        for body, status, code in cases:
            with self.subTest(body={name: str(value)[:12] for name, value in body.items()}):
                error = self.assert_error(self.client.post('/requests', body), status, code)
                self.assertTrue(error['message'])
        self.assertEqual(self.rows(), [])

    def test_the_limits_themselves_are_accepted(self):
        reply = self.client.post('/requests', self.body(title='가' * 200, body='x' * (64 * 1024), key='k' * 128))
        self.assertEqual(reply.status, 201, reply.body)

    def test_a_project_outside_the_scope_is_403_and_an_unknown_one_is_not_stored(self):
        self.assert_error(self.client.post('/requests', self.body(project_id=self.hub.b)), 403, 'forbidden')
        self.assertEqual(self.hub.inbox_rows('SELECT count(*) FROM requests', root=self.hub.root_b), [])
        everywhere = self.hub.rest(self.hub.key('all'))
        reply = everywhere.post('/requests', self.body(project_id='00000000-0000-4000-8000-00000000abcd'))
        self.assert_error(reply, 404, 'not_found')


class ListGetTests(RestCase):
    """``GET /api/v1/requests`` and ``GET /api/v1/requests/{id}``: requester keys see their own requests, editors
    every request in scope; ``state`` repeats, ``include_closed``/``limit``/``cursor`` page; a request out of reach
    is ``not_found`` (AC-07)."""

    SUMMARY = {'request_id', 'project_id', 'seq', 'title', 'state', 'outcome', 'origin', 'created_at', 'updated_at',
               'task_id', 'untrusted_text'}

    def setUp(self):
        super().setUp()
        self.r1, self.r2 = self.hub.key('r1'), self.hub.key('r2')
        self.ed = self.hub.key('e1', 'editor', scope={'projects': [self.hub.a]})
        self.mine = [self.submit(self.r1, f'k{i}', f'r1 요청 {i}') for i in range(3)]
        self.theirs = self.submit(self.r2, 'k0', 'r2 요청')
        self.beta = self.submit(self.r1, 'kb', 'beta 요청', self.hub.b)

    def submit(self, key, idem, title, project=None):
        reply = self.hub.rest(key).post('/requests', {'project_id': project or self.hub.a, 'title': title,
                                                      'body': 'b', 'idempotency_key': idem})
        self.assertEqual(reply.status, 201, reply.body)
        return reply.json['request_id']

    def ids(self, reply):
        self.assertEqual(reply.status, 200, reply.body)
        return [item['request_id'] for item in reply.json['requests']]

    def test_a_requester_lists_only_its_own_requests(self):
        reply = self.hub.rest(self.r1).get('/requests')
        self.assertEqual(set(self.ids(reply)), {*self.mine, self.beta})
        self.assertEqual(set(reply.json['requests'][0]), self.SUMMARY)
        self.assertIsNone(reply.json['next_cursor'])

    def test_an_editor_lists_every_request_in_its_scope(self):
        self.assertEqual(set(self.ids(self.hub.rest(self.ed).get('/requests'))), {*self.mine, self.theirs})
        self.assert_error(self.hub.rest(self.ed).get('/requests', project_id=self.hub.b), 403, 'forbidden')

    def test_limit_and_cursor_page_through_the_list(self):
        client = self.hub.rest(self.r1)
        first = client.get('/requests', project_id=self.hub.a, limit=2)
        self.assertEqual(len(self.ids(first)), 2)
        self.assertIsInstance(first.json['next_cursor'], str)
        rest = client.get('/requests', project_id=self.hub.a, limit=2, cursor=first.json['next_cursor'])
        self.assertEqual(len(self.ids(rest)), 1)
        self.assertIsNone(rest.json['next_cursor'])
        self.assertEqual(set(self.ids(first)) | set(self.ids(rest)), set(self.mine))

    def test_state_repeats_and_include_closed_shows_closed_requests(self):
        client = self.hub.rest(self.r1)
        self.assertEqual(client.post(f'/requests/{self.mine[0]}/cancel', {}).json['state'], 'CLOSED')
        self.assertNotIn(self.mine[0], self.ids(client.get('/requests')))
        self.assertIn(self.mine[0], self.ids(client.get('/requests', include_closed=True)))
        self.assertEqual(self.ids(client.get('/requests', state='CLOSED')), [self.mine[0]])
        both = self.ids(client.get('/requests', project_id=self.hub.a, state=['CLOSED', 'NEW']))
        self.assertEqual(set(both), set(self.mine))

    def test_query_parameter_rules_are_400(self):
        client = self.hub.rest(self.r1)
        for target in ('?limit=0', '?limit=101', '?limit=-1', '?limit=1.5', '?limit=%2B5', '?limit=', '?limit=a',
                       '?include_closed=1', '?include_closed=True', '?include_closed=yes', '?state=DONE',
                       '?limit=1&limit=2', '?cursor=a&cursor=b', '?states=NEW', '?query=' + 'x' * 201,
                       '?cursor=not-a-cursor'):
            with self.subTest(target=target):
                self.assert_error(client.raw('GET', '/api/v1/requests' + target), 400, 'invalid_input')

    def test_get_reads_a_visible_request_and_hides_the_others(self):
        reply = self.hub.rest(self.r1).get(f'/requests/{self.mine[0]}')
        self.assertEqual(reply.status, 200, reply.body)
        request = reply.json['request']
        self.assertEqual((request['request_id'], request['title'], request['body']), (self.mine[0], 'r1 요청 0', 'b'))
        self.assertIs(reply.json['untrusted_text'], True)
        for key, rid in ((self.r2, self.mine[0]), (self.ed, self.beta), (self.r1, '00000000-0000-4000-8000-0000')):
            with self.subTest(rid=rid):
                self.assert_error(self.hub.rest(key).get(f'/requests/{rid}'), 404, 'not_found')



class RequestCase(RestCase):
    """One NEW request of requester ``r1`` in project alpha, plus an editor (``ed``) and another requester (``r2``)."""

    def setUp(self):
        super().setUp()
        self.r1, self.r2 = self.hub.key('r1'), self.hub.key('r2')
        self.ed = self.hub.key('e1', 'editor')
        reply = self.hub.rest(self.r1).post('/requests', {'project_id': self.hub.a, 'title': '제목', 'body': '본문',
                                                          'idempotency_key': 'k1'})
        self.rid = reply.json['request_id']

    def request_row(self):
        return self.hub.inbox_rows('SELECT state, version, revision, title, body FROM requests WHERE request_id = ?',
                                   self.rid)[0]

    def comments(self):
        return self.hub.inbox_rows('SELECT kind, author_kind, body FROM comments WHERE request_id = ? ORDER BY seq',
                                   self.rid)


class CommentTests(RequestCase):
    """``POST /api/v1/requests/{id}/comments`` (task-inbox "Principal과 권한": own request for requesters): 201
    ``{comment_id, state, version}``; on NEEDS_INFO the comment is the answer and the request goes to REVIEWING."""

    def test_a_comment_is_201(self):
        reply = self.hub.rest(self.r1).post(f'/requests/{self.rid}/comments', {'body': '추가 설명'})
        self.assertEqual(reply.status, 201, reply.body)
        self.assertEqual(set(reply.json), {'comment_id', 'state', 'version'})
        self.assertEqual(reply.json['state'], 'NEW')
        self.assertIn(('comment', 'requester', '추가 설명'), self.comments())

    def test_an_answer_on_needs_info_goes_back_to_reviewing(self):
        version = self.hub.rest(self.ed).post(f'/requests/{self.rid}/state',
                                              {'base_version': 1, 'to': 'NEEDS_INFO'}).json['version']
        reply = self.hub.rest(self.r1).post(f'/requests/{self.rid}/comments', {'body': '답변'})
        self.assertEqual(reply.status, 201, reply.body)
        self.assertEqual(reply.json['state'], 'REVIEWING')
        self.assertGreater(reply.json['version'], version)
        self.assertEqual(self.request_row()[0], 'REVIEWING')

    def test_another_requester_cannot_comment_and_limits_hold(self):
        self.assert_error(self.hub.rest(self.r2).post(f'/requests/{self.rid}/comments', {'body': 'x'}), 404,
                          'not_found')
        client = self.hub.rest(self.r1)
        self.assert_error(client.post(f'/requests/{self.rid}/comments', {'body': ''}), 400, 'invalid_input')
        self.assert_error(client.post(f'/requests/{self.rid}/comments', {'body': 'x' * (16 * 1024 + 1)}), 413,
                          'too_large')
        self.assert_error(client.post(f'/requests/{self.rid}/comments', {}), 400, 'invalid_input')
        self.assertEqual([row for row in self.comments() if row[1] != 'system'], [])


class EditorOnlyTests(RequestCase):
    """``PATCH /api/v1/requests/{id}`` and ``POST …/state``: editor and Board agent keys only (requester keys 403
    ``forbidden``, nothing changes); ``base_version`` conflicts are 409 with the current state and version."""

    def test_a_requester_gets_403_and_nothing_changes(self):
        client = self.hub.rest(self.r1)
        self.assert_error(client.patch(f'/requests/{self.rid}', {'base_version': 1, 'title': '바뀐 제목'}), 403,
                          'forbidden')
        self.assert_error(client.post(f'/requests/{self.rid}/state', {'base_version': 1, 'to': 'NEEDS_INFO'}), 403,
                          'forbidden')
        self.assertEqual(self.request_row(), ('NEW', 1, 1, '제목', '본문'))

    def test_an_editor_edits_in_place(self):
        reply = self.hub.rest(self.ed).patch(f'/requests/{self.rid}', {'base_version': 1, 'title': '바뀐 제목',
                                                                      'body': None})
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.json, {'state': 'NEW', 'version': 2, 'revision': 1, 'mode': 'in_place'})
        self.assertEqual(self.request_row(), ('NEW', 2, 1, '바뀐 제목', '본문'))

    def test_a_stale_base_version_is_409_with_state_and_version(self):
        self.hub.rest(self.ed).patch(f'/requests/{self.rid}', {'base_version': 1, 'body': '새 본문'})
        error = self.assert_error(self.hub.rest(self.ed).patch(f'/requests/{self.rid}', {'base_version': 1,
                                                                                         'title': 'x'}),
                                  409, 'conflict')
        self.assertEqual((error['state'], error['version']), ('NEW', 2))

    def test_the_board_agent_moves_states_and_reopens(self):
        board = self.hub.rest(self.hub.keys.rotate_system_key(now=self.hub.now[0]).raw)
        reply = board.post(f'/requests/{self.rid}/state', {'base_version': 1, 'to': 'NEEDS_INFO', 'reason': '질문'})
        self.assertEqual((reply.status, reply.json), (200, {'state': 'NEEDS_INFO', 'version': 2}))
        self.assert_error(board.post(f'/requests/{self.rid}/state', {'base_version': 2, 'to': 'READY'}), 400,
                          'invalid_input')
        self.hub.rest(self.r1).post(f'/requests/{self.rid}/cancel', {})
        version = self.request_row()[1]
        reply = board.post(f'/requests/{self.rid}/state', {'base_version': version, 'to': 'NEW'})
        self.assertEqual((reply.status, reply.json['state']), (200, 'NEW'))

    def test_an_invalid_transition_is_409(self):
        self.hub.link_running(self.rid)
        version = self.request_row()[1]
        error = self.assert_error(self.hub.rest(self.ed).post(f'/requests/{self.rid}/state',
                                                              {'base_version': version, 'to': 'NEW'}),
                                  409, 'invalid_transition')
        self.assertEqual(error['state'], 'RUNNING')


COMMAND = {'command_id': 'cmd-1', 'label': 'python train.py', 'state': 'running'}


class CancelTests(RequestCase):
    """``POST /api/v1/requests/{id}/cancel`` (AC-10, task-inbox "취소"): before dispatch 200 ``cancelled``; READY 202
    ``cancel_requested``; RUNNING first 200 ``confirm_required`` (token + impact, nothing stops), the right token 202
    and the cancel port once; an unknown, expired, foreign-key, other-request or stale-fingerprint token 409
    ``token_mismatch`` and no port call."""

    def cancel_row(self) -> tuple:
        return self.hub.inbox_rows('SELECT state, outcome, cancel_state FROM requests WHERE request_id = ?',
                                   self.rid)[0]

    def cancel(self, key=None, rid=None, **body):
        return self.hub.rest(key or self.r1).post(f'/requests/{rid or self.rid}/cancel', body)

    def running(self, view=None):
        self.hub.link_running(self.rid, view={'command': COMMAND} if view is None else view)

    def first(self, key=None) -> str:
        reply = self.cancel(key)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.json['status'], 'confirm_required')
        return reply.json['confirm_token']

    def assert_untouched(self):
        self.assertEqual(self.hub.cancels, [])
        self.assertEqual(self.cancel_row(), ('RUNNING', None, None))

    def mismatch(self, reply):
        self.assert_error(reply, 409, 'token_mismatch')
        self.assert_untouched()

    def test_before_dispatch_cancels_at_once(self):
        reply = self.cancel(reason='필요 없음')
        self.assertEqual((reply.status, reply.json), (200, {'status': 'cancelled', 'state': 'CLOSED'}))
        self.assertEqual(self.cancel_row(), ('CLOSED', 'cancelled', None))
        self.assertEqual(self.hub.cancels, [])

    def test_ready_is_202_without_a_token(self):
        self.hub.link_running(self.rid, status='dispatched')
        reply = self.cancel()
        self.assertEqual((reply.status, reply.json), (202, {'status': 'cancel_requested'}))
        self.assertEqual([event.request_id for event in self.hub.cancels], [self.rid])

    def test_ready_with_the_backend_off_is_409(self):
        self.hub.link_running(self.rid, status='dispatched')
        self.hub.backend_up = False
        self.assert_error(self.cancel(), 409, 'backend_not_running')
        self.assertEqual(self.hub.cancels, [])

    def test_running_first_call_returns_a_token_and_the_impact_and_stops_nothing(self):
        self.running()
        reply = self.cancel()
        self.assertEqual(reply.status, 200, reply.body)
        value = reply.json
        self.assertEqual(value['status'], 'confirm_required')
        self.assertGreaterEqual(len(value['confirm_token']), 24)
        self.assertEqual(value['expires_in'], 60)
        self.assertEqual(value['impact'], {'task_id': 'task-1', 'task_status': 'running', 'command': COMMAND})
        self.assertIs(value['untrusted_text'], True)  # the command label is worker text
        self.assert_untouched()

    def test_the_right_token_is_202_and_calls_the_port_once(self):
        self.running()
        token = self.first()
        reply = self.cancel(confirm_token=token, reason='중지')
        self.assertEqual((reply.status, reply.json), (202, {'status': 'cancel_requested', 'stop': None}))
        self.assertEqual(self.cancel_row(), ('RUNNING', None, 'requested'))
        self.assertEqual([event.request_id for event in self.hub.cancels], [self.rid])
        self.assert_error(self.cancel(confirm_token=token), 409, 'token_mismatch')  # used up
        self.assertEqual(len(self.hub.cancels), 1)

    def test_an_unknown_token_is_409(self):
        self.running()
        self.first()
        self.mismatch(self.cancel(confirm_token='x' * 32))

    def test_an_expired_token_is_409(self):
        self.running()
        token = self.first()
        self.hub.mono[0] += 60.0
        self.mismatch(self.cancel(confirm_token=token))

    def test_a_token_from_another_key_is_409_and_stays_for_its_owner(self):
        self.running()
        token = self.first(self.ed)
        self.mismatch(self.cancel(self.hub.key('e2', 'editor'), confirm_token=token))
        self.assertEqual(self.cancel(self.ed, confirm_token=token).status, 202)

    def test_a_token_for_another_request_is_409(self):
        other = self.hub.rest(self.r1).post('/requests', {'project_id': self.hub.a, 'title': '다른', 'body': 'b',
                                                          'idempotency_key': 'k2'}).json['request_id']
        self.hub.link_running(other, task_id='task-2', view={'command': COMMAND})
        token = self.hub.rest(self.r1).post(f'/requests/{other}/cancel', {}).json['confirm_token']
        self.running()
        self.mismatch(self.cancel(confirm_token=token))

    def test_a_changed_request_makes_the_token_409(self):
        self.running()
        token = self.first()
        version = self.request_row()[1]
        self.hub.rest(self.ed).patch(f'/requests/{self.rid}', {'base_version': version, 'body': '바뀐 본문'})
        self.mismatch(self.cancel(confirm_token=token))



class ResultTests(RestCase):
    """``GET /api/v1/requests/{id}/result`` (task-inbox "결과 조회", the ``Results`` shapes): the summary by default,
    ``full=true`` the full view, ``full=true`` with ``run_id``/``artifact_path`` one page (``offset``/``length``,
    paged by ``next_offset``); page arguments need ``full=true``. The backend is ``test_inbox_results.FakeBackend``."""

    def setUp(self):
        super().setUp()
        import test_inbox_results as fixtures  # module import: its test classes stay uncollected here
        from workbench.hub.inbox import results
        self.fx = fixtures
        self.backend = fixtures.FakeBackend()
        self.hub.api.results = results.Results(self.hub.inbox, port=self.backend)
        self.r1 = self.hub.key('r1', scope={'projects': [self.hub.a]})
        self.client = self.hub.rest(self.r1)
        self.rid = self.client.post('/requests', {'project_id': self.hub.a, 'title': 'Train', 'body': 'Run it',
                                                  'idempotency_key': 'k-1'}).json['request_id']
        self.hub.link_running(self.rid, task_id=fixtures.TASK, view={'status': 'running', 'runs': [
            {'run_id': fixtures.RUN, 'status': 'running', 'exit_code': None}]})

    def result(self, **query):
        reply = self.client.get(f'/requests/{self.rid}/result', **query)
        self.assertEqual(reply.status, 200, reply.body)
        return reply.json

    def test_the_default_is_the_summary(self):
        value = self.result()
        self.assertEqual(set(value), {'summary', 'state', 'outcome', 'task', 'untrusted_text', 'note'})
        self.assertIn(f'run {self.fx.RUN}: running', value['summary'])
        self.assertEqual(self.result(full=False), value)
        self.assertEqual(self.backend.calls, [])

    def test_full_is_the_full_view(self):
        full = self.result(full=True)['full']
        self.assertEqual((full['available'], full['task']['task_id']), (True, self.fx.TASK))
        self.assertEqual(full['task']['reports'][0]['text'], self.fx.REPORT)

    def test_a_raw_page_and_paging_to_the_end(self):
        page = self.result(full=True, run_id=self.fx.RUN, offset=0, length=17)['raw']
        self.assertEqual((page['offset'], page['length'], page['next_offset'], page['size']),
                         (0, 17, 17, len(self.fx.RUN_LOG)))
        self.assertEqual(page['text'], self.fx.RUN_LOG[:17].decode())
        data, offset = b'', 0
        while offset is not None and offset < len(self.fx.RUN_LOG):
            page = self.result(full=True, run_id=self.fx.RUN, offset=offset, length=300)['raw']
            data += page['text'].encode() if 'text' in page else base64.b64decode(page['data_base64'])
            offset = page['next_offset']
        self.assertEqual(data, self.fx.RUN_LOG)

    def test_an_artifact_page(self):
        (self.hub.root_a / 'out.txt').write_text('ARTIFACT-TEXT')
        value = self.result(full=True, artifact_path='out.txt', offset=2, length=6)
        self.assertEqual(value['artifact']['text'], 'TIFACT')
        self.assertIs(value['untrusted_text'], True)

    def test_a_run_or_artifact_page_without_full_true_is_400(self):  # WBX-B8: was "adds nothing"
        (self.hub.root_a / 'out.txt').write_text('ARTIFACT-TEXT')
        for query in ({'run_id': self.fx.RUN}, {'artifact_path': 'out.txt'},
                      {'run_id': self.fx.RUN, 'artifact_path': 'out.txt', 'offset': 0, 'length': 5},
                      {'full': False, 'run_id': self.fx.RUN}, {'full': False, 'artifact_path': 'out.txt'}):
            with self.subTest(query=query):
                reply = self.client.get(f'/requests/{self.rid}/result', **query)
                error = self.assert_error(reply, 400, 'invalid_input')
                self.assertIn('full=true', error['message'])
                self.assertNotIn('ARTIFACT-TEXT', reply.body.decode())
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(set(self.result(offset=0, length=5)), {'summary', 'state', 'outcome', 'task', 'untrusted_text',
                                                                'note'})  # offset/length alone still add nothing

    def test_page_ranges_and_reach_are_refused(self):
        for query in ({'full': True, 'run_id': self.fx.RUN, 'length': 0},
                      {'full': True, 'run_id': self.fx.RUN, 'length': 131073},
                      {'full': True, 'run_id': self.fx.RUN, 'offset': -1},
                      {'full': True, 'run_id': self.fx.RUN, 'offset': len(self.fx.RUN_LOG) + 1}, {'full': 'yes'}):
            with self.subTest(query=query):
                self.assert_error(self.client.get(f'/requests/{self.rid}/result', **query), 400, 'invalid_input')
        other = self.hub.rest(self.hub.key('r2', scope={'projects': [self.hub.a]}))
        self.assert_error(other.get(f'/requests/{self.rid}/result'), 404, 'not_found')

    def test_a_raw_page_with_the_backend_off_is_409(self):
        self.hub.backend_up = False
        self.backend.down = 'backend_not_running'
        reply = self.client.get(f'/requests/{self.rid}/result', full=True, run_id=self.fx.RUN)
        self.assert_error(reply, 409, 'backend_not_running')


class ErrorNormalisationTests(RequestCase):
    """SPEC "오류 → HTTP status": passthrough codes keep the inbox sentence; every store, lock or backend failure
    gets a fixed sentence and its status; an unknown code or an exception is 500 ``internal_error``; no path,
    exception text or stack trace reaches the client."""

    SECRET = '/home/someone/secret/.wb/inbox.sqlite3'

    def failing(self, exc):
        patcher = mock.patch.object(self.hub.inbox, 'get', side_effect=exc)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.hub.rest(self.r1).get(f'/requests/{self.rid}')

    def assert_clean(self, reply):
        self.assertNotIn(b'secret', reply.body)
        self.assertNotIn(b'Traceback', reply.body)
        self.assertNotIn(b'/home', reply.body)

    def test_store_lock_and_backend_failures_are_503_with_a_fixed_sentence(self):
        for code, shown in (('project_unavailable', 'project_unavailable'),
                            ('inbox_unavailable', 'project_unavailable'),
                            ('inbox_busy', 'inbox_busy'), ('inbox_mismatch', 'inbox_mismatch'),
                            ('backend_unreachable', 'backend_unreachable'), ('hub_stopping', 'hub_stopping')):
            with self.subTest(code=code):
                reply = self.failing(InboxError(code, f'cannot open {self.SECRET}'))
                self.assert_error(reply, 503, shown)
                self.assert_clean(reply)
                mock.patch.stopall()

    def test_backend_not_running_is_409_with_a_fixed_sentence(self):
        reply = self.failing(InboxError('backend_not_running', self.SECRET))
        self.assertEqual(self.assert_error(reply, 409, 'backend_not_running')['message'],
                         "The project's backend is not running.")

    def test_an_unknown_code_and_an_exception_are_500_internal_error(self):
        for exc in (InboxError('disk_full', self.SECRET), RuntimeError(self.SECRET), OSError(2, 'x', self.SECRET)):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch('sys.stderr', new_callable=io.StringIO) as log:
                    reply = self.failing(exc)
                self.assertEqual(self.assert_error(reply, 500, 'internal_error')['message'], 'Internal error.')
                self.assert_clean(reply)
                self.assertNotIn('secret', log.getvalue())
                mock.patch.stopall()

    def test_a_passthrough_code_keeps_the_inbox_sentence_and_its_state(self):
        error = self.assert_error(self.hub.rest(self.ed).patch(f'/requests/{self.rid}', {'base_version': 9,
                                                                                         'title': 'x'}),
                                  409, 'conflict')
        self.assertIn('base_version', error['message'])
        self.assertEqual((error['state'], error['version']), ('NEW', 1))


class BusyTests(RestCase):
    """SPEC 동시성 (security review fix 2 carried over): one key has at most 4 calls answered at once, the rest
    wait; a call without a slot for ``busy_wait`` (30 s in the Hub, shortened here) is 503 ``busy``; other keys
    are not held up and the slots free again."""

    def setUp(self):
        super().setUp()
        self.key = self.hub.key('r1')
        self.release = threading.Event()
        self.entered, self.running, self.peak, self.count_lock = [], 0, 0, threading.Lock()
        original = self.hub.inbox.list_requests

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
        self.addCleanup(self._finish)

    def _finish(self):
        self.release.set()
        for thread in self.threads:
            thread.join(30)

    def in_background(self) -> dict:
        box: dict = {}

        def run():
            box['reply'] = self.hub.rest(self.key).get('/requests')
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.threads.append(thread)
        return box

    def wait_entered(self, count):
        deadline = time.monotonic() + 10
        while len(self.entered) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.entered), count)

    def test_a_call_waiting_past_the_bounded_wait_is_503_busy(self):
        self.assertEqual(self.hub.api.busy_wait, 30.0)
        self.hub.api.busy_wait = 0.5
        boxes = [self.in_background() for _ in range(4)]
        self.wait_entered(4)
        started = time.monotonic()
        reply = self.hub.rest(self.key).get('/requests')
        self.assertGreaterEqual(time.monotonic() - started, 0.45, 'refused without waiting')
        self.assert_error(reply, 503, 'busy')
        self.assertEqual(self.peak, 4)
        self.assertEqual(self.hub.rest(self.hub.key('other')).get('/projects').status, 200)
        self._finish()
        self.assertTrue(all(box['reply'].status == 200 for box in boxes))
        self.assertEqual(self.hub.rest(self.key).get('/projects').status, 200)  # the slots are free again


class CatchAllTests(RestCase):
    """SPEC: an unknown ``/api/v1/…`` path is the REST 401 without a key and 404 ``not_found`` with one (never the
    session API's ``{"ok": false}`` shape), for every method."""

    PATHS = ('/api/v1/', '/api/v1/nope', '/api/v1/requests/x/y', f'/api/v1/requests/{RID}/raw',
             '/api/v1/keys', '/api/v1/projects/x', '/api/v1/requests/x/start-review')

    def test_without_a_key_it_is_401(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                reply = self.hub.rest(None).raw('GET', path)
                self.assert_error(reply, 401, 'unauthorized')
                self.assertEqual(reply.header('www-authenticate'), WWW_AUTHENTICATE)

    def test_with_a_key_it_is_404_for_every_method(self):
        client = self.hub.rest(self.hub.key('e1', 'editor'))
        for path in self.PATHS:
            for method in ('GET', 'POST', 'DELETE'):
                with self.subTest(path=path, method=method):
                    body = b'{}' if method == 'POST' else None
                    reply = client.raw(method, path, body, {'Content-Type': 'application/json'} if body else None)
                    self.assert_error(reply, 404, 'not_found')

    def test_a_foreign_origin_is_still_403(self):
        reply = self.hub.rest(self.hub.key('r1')).raw('GET', '/api/v1/nope', headers={'Origin': 'http://evil.example'})
        self.assert_error(reply, 403, 'origin_mismatch')

    def test_the_bare_prefix_without_a_slash_is_an_unknown_rest_path(self):
        reply = self.hub.rest(None).raw('GET', '/api/v1')
        self.assert_error(reply, 401, 'unauthorized')
        self.assertEqual(reply.header('www-authenticate'), WWW_AUTHENTICATE)
        client = self.hub.rest(self.hub.key('e1', 'editor'))
        for method in ('GET', 'POST', 'DELETE'):
            with self.subTest(method=method):
                body = b'{}' if method == 'POST' else None
                reply = client.raw(method, '/api/v1', body, {'Content-Type': 'application/json'} if body else None)
                self.assert_error(reply, 404, 'not_found')
        reply = client.raw('POST', '/api/v1', b'{}', {'Content-Type': 'application/json',
                                                      'Origin': 'http://evil.example'})
        self.assert_error(reply, 403, 'origin_mismatch')


if __name__ == '__main__':
    unittest.main()
