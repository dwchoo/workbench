"""A stdlib-only REST test client for ``/api/v1/`` (``http.client`` + ``json``; seam S2 client side).

Usage (``discover -s tests/hub`` puts ``tests/hub`` on ``sys.path``)::

    from support.rest_client import RestClient
    client = RestClient('127.0.0.1', port, key)           # key: the raw ``wbk_…`` API key (None: no header)
    reply = client.get('/projects', query='alp')          # GET /api/v1/projects?query=alp
    reply = client.post('/requests', {'project_id': …, 'title': …, 'body': …, 'idempotency_key': …})
    reply.status, reply.json, reply.header('location')    # status, parsed body, a lower-case header
    client.get('/requests', state=['NEW', 'READY'])       # a list value repeats the query parameter
    client.patch(f'/requests/{rid}', {'base_version': 1, 'title': 'x'})
    client.raw('PUT', '/api/v1/requests', b'…', {'Content-Type': 'text/plain'})   # anything, full path

Every request sends ``Host: <host>:<port>`` and ``Authorization: Bearer <key>``; ``post``/``patch`` send
``Content-Type: application/json`` with the body as compact UTF-8 JSON. ``headers`` are merged over the defaults
(``None`` drops one) unless ``replace_headers``. Python ``True``/``False`` query values become ``true``/``false``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import json
from urllib.parse import quote

PREFIX = '/api/v1'


@dataclass
class Reply:
    status: int
    headers: dict = field(default_factory=dict)  # lower-case names
    body: bytes = b''

    @property
    def json(self):
        return json.loads(self.body.decode('utf-8'))

    @property
    def error(self) -> dict:
        return self.json['error']

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


def query_string(query: dict) -> str:
    parts = []
    for name, value in query.items():
        for item in value if isinstance(value, list) else [value]:
            if item is None:
                continue
            if isinstance(item, bool):
                item = 'true' if item else 'false'
            parts.append(f'{quote(name, safe="")}={quote(str(item), safe="")}')
    return '&'.join(parts)


class RestClient:
    def __init__(self, host: str, port: int, key: str | None, *, host_header: str | None = None,
                 timeout: float = 30.0):
        self.host, self.port, self.key, self.timeout = host, port, key, timeout
        self.host_header = host_header or f'{host}:{port}'

    def headers(self, extra: dict | None = None) -> dict:
        headers = {'Host': self.host_header}
        if self.key is not None:
            headers['Authorization'] = f'Bearer {self.key}'
        for name, value in (extra or {}).items():
            for existing in [key for key in headers if key.lower() == name.lower()]:
                del headers[existing]
            if value is not None:
                headers[name] = value
        return headers

    def raw(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None, *,
            replace_headers: bool = False) -> Reply:
        """One HTTP request to ``path`` (the full path, query included)."""
        sent = dict(headers or {}) if replace_headers else self.headers(headers)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in sent.items():
                for item in value if isinstance(value, list) else [value]:
                    connection.putheader(name, item)
            if body is not None and not any(name.lower() in ('content-length', 'transfer-encoding') for name in sent):
                connection.putheader('Content-Length', str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            data = response.read()
            return Reply(response.status, {name.lower(): value for name, value in response.getheaders()}, data)
        finally:
            connection.close()

    def call(self, method: str, path: str, value=None, headers: dict | None = None, **query) -> Reply:
        """``method`` on ``/api/v1<path>``; ``value`` (if given) as the JSON body."""
        target = PREFIX + path
        text = query_string(query)
        if text:
            target += '?' + text
        body = None
        if value is not None:
            body = json.dumps(value, ensure_ascii=False).encode('utf-8')
            headers = {'Content-Type': 'application/json', **(headers or {})}
        return self.raw(method, target, body, headers)

    def get(self, path: str, headers: dict | None = None, **query) -> Reply:
        return self.call('GET', path, None, headers, **query)

    def post(self, path: str, value=None, headers: dict | None = None) -> Reply:
        return self.call('POST', path, {} if value is None else value, headers)

    def patch(self, path: str, value=None, headers: dict | None = None) -> Reply:
        return self.call('PATCH', path, {} if value is None else value, headers)
