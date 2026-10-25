"""A stdlib-only MCP Streamable HTTP test client (``http.client`` + ``json``, no ``mcp`` SDK, D-29).

Usage (``discover -s tests/hub`` puts ``tests/hub`` on ``sys.path``)::

    from support.mcp_client import McpClient
    client = McpClient('127.0.0.1', port, key)        # key: the raw ``wbk_…`` API key
    client.initialize()                               # POST initialize; keeps ``Mcp-Session-Id``
    client.initialized()                              # POST notifications/initialized (202)
    tools = client.list_tools()                       # tools/list -> [{name, description, inputSchema}]
    value = client.tool('inbox_submit', project_id=…, title=…, body=…, idempotency_key=…)
    error = client.tool_error('inbox_get', request_id=…)   # structuredContent.error of an isError result
    client.close_session()                            # DELETE /mcp (204)

Every message is one JSON-RPC 2.0 object per POST with ``Accept: application/json, text/event-stream``
and ``Authorization: Bearer <key>`` (what OMP 18.12 sends, PROBE-OMP-MCP.md). ``raw()`` sends anything
(other methods, bodies, headers) for transport tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import itertools
import json

ACCEPT = 'application/json, text/event-stream'
DEFAULT_VERSION = '2025-06-18'


@dataclass
class Reply:
    status: int
    headers: dict = field(default_factory=dict)  # lower-case names
    body: bytes = b''

    @property
    def json(self):
        return json.loads(self.body.decode('utf-8'))

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class McpError(AssertionError):
    """A JSON-RPC error (or a non-200 reply) where the caller expected a result."""

    def __init__(self, reply: Reply):
        super().__init__(f'HTTP {reply.status}: {reply.body[:500]!r}')
        self.reply = reply


class McpClient:
    def __init__(self, host: str, port: int, key: str | None, *, host_header: str | None = None,
                 timeout: float = 30.0, path: str = '/mcp'):
        self.host, self.port, self.key, self.timeout, self.path = host, port, key, timeout, path
        self.host_header = host_header or f'{host}:{port}'
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._ids = itertools.count(1)

    # --- transport -------------------------------------------------------------------------------

    def headers(self, extra: dict | None = None) -> dict:
        headers = {'Host': self.host_header, 'Accept': ACCEPT, 'Content-Type': 'application/json'}
        if self.key is not None:
            headers['Authorization'] = f'Bearer {self.key}'
        if self.session_id is not None:
            headers['Mcp-Session-Id'] = self.session_id
        if self.protocol_version is not None:
            headers['MCP-Protocol-Version'] = self.protocol_version
        for name, value in (extra or {}).items():
            for existing in [key for key in headers if key.lower() == name.lower()]:
                del headers[existing]
            if value is not None:
                headers[name] = value
        return headers

    def raw(self, method: str = 'POST', body: bytes | None = None, headers: dict | None = None, *,
            path: str | None = None, replace_headers: bool = False) -> Reply:
        """One HTTP request; ``headers`` are merged over :meth:`headers` (``None`` drops one) unless
        ``replace_headers``."""
        sent = dict(headers or {}) if replace_headers else self.headers(headers)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.putrequest(method, path or self.path, skip_host=True, skip_accept_encoding=True)
            for name, value in sent.items():
                connection.putheader(name, value)
            if body is not None and not any(name.lower() in ('content-length', 'transfer-encoding') for name in sent):
                connection.putheader('Content-Length', str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            data = response.read()
            return Reply(response.status, {name.lower(): value for name, value in response.getheaders()}, data)
        finally:
            connection.close()

    def post(self, message, headers: dict | None = None) -> Reply:
        return self.raw('POST', json.dumps(message).encode('utf-8'), headers)

    def request(self, method: str, params: dict | None = None, headers: dict | None = None) -> Reply:
        message = {'jsonrpc': '2.0', 'id': next(self._ids), 'method': method}
        if params is not None:
            message['params'] = params
        return self.post(message, headers)

    def result(self, method: str, params: dict | None = None) -> dict:
        reply = self.request(method, params)
        if reply.status != 200 or 'result' not in reply.json:
            raise McpError(reply)
        return reply.json['result']

    # --- MCP lifecycle ---------------------------------------------------------------------------

    def initialize(self, version: str = DEFAULT_VERSION) -> dict:
        reply = self.request('initialize', {'protocolVersion': version, 'capabilities': {},
                                            'clientInfo': {'name': 'wb-test-client', 'version': '1'}})
        if reply.status != 200 or 'result' not in reply.json:
            raise McpError(reply)
        self.session_id = reply.header('mcp-session-id')
        self.protocol_version = reply.json['result']['protocolVersion']
        return reply.json['result']

    def initialized(self) -> Reply:
        return self.post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def list_tools(self) -> list[dict]:
        return self.result('tools/list')['tools']

    def call(self, name: str, **arguments) -> dict:
        """The raw ``tools/call`` result (``content``, ``structuredContent``, ``isError``)."""
        return self.result('tools/call', {'name': name, 'arguments': arguments})

    def tool(self, name: str, **arguments) -> dict:
        result = self.call(name, **arguments)
        if result.get('isError') is not False:
            raise AssertionError(f'{name} failed: {result!r}')
        return result['structuredContent']

    def tool_error(self, name: str, **arguments) -> dict:
        result = self.call(name, **arguments)
        if result.get('isError') is not True:
            raise AssertionError(f'{name} unexpectedly succeeded: {result!r}')
        return result['structuredContent']['error']

    def close_session(self) -> Reply:
        return self.raw('DELETE')

    def start(self) -> 'McpClient':
        """initialize → notifications/initialized, like a real client's start."""
        self.initialize()
        self.initialized()
        return self
