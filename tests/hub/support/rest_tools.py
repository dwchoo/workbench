"""REST calls named by the operation (``RestTools``) and the SPEC error status table (``ERROR_STATUS``).

Shared by the inbox result, cancel and flow tests (moved from ``test_inbox_results_independent.py``, WBX-B8)::

    from support.rest_tools import ERROR_STATUS, RestTools
    r1 = RestTools(RestClient('127.0.0.1', port, key))   # or RestTools(hub.rest(key))
    r1.tool('inbox_submit', project_id=…, title=…, body=…, idempotency_key=…)   # the 2xx body
    r1.tool_error('inbox_result', request_id=rid, run_id=…)                     # the error object, status checked
    r1.call('inbox_get', request_id=rid)                                         # the raw reply
"""
from __future__ import annotations

from urllib.parse import quote

from workbench.hub import rest


# SPEC "오류 → HTTP status": the status every error code must come with (``RestTools.tool_error`` checks it).
ERROR_STATUS = {'invalid_input': 400, 'unauthorized': 401, 'forbidden': 403, 'origin_mismatch': 403, 'not_found': 404,
                'conflict': 409, 'invalid_transition': 409, 'idempotency_conflict': 409, 'token_mismatch': 409,
                'backend_not_running': 409, 'too_large': 413, 'internal_error': 500, 'project_unavailable': 503,
                'inbox_busy': 503, 'inbox_mismatch': 503, 'backend_unreachable': 503, 'hub_stopping': 503,
                'busy': 503}
# SPEC endpoint table: operation -> (method, path); ``request_id`` is the path's id, the other arguments are the
# query (GET) or the JSON body, ``states`` is the repeated ``state`` query parameter.
ROUTES = {'inbox_projects': ('GET', '/projects'), 'inbox_submit': ('POST', '/requests'),
          'inbox_list': ('GET', '/requests'), 'inbox_get': ('GET', '/requests/{rid}'),
          'inbox_comment': ('POST', '/requests/{rid}/comments'), 'inbox_update': ('PATCH', '/requests/{rid}'),
          'inbox_set_state': ('POST', '/requests/{rid}/state'), 'inbox_cancel': ('POST', '/requests/{rid}/cancel'),
          'inbox_result': ('GET', '/requests/{rid}/result')}


class RestTools:
    """One key's REST calls, named by the operation (``inbox_api.TOOLS``): ``call`` is the reply, ``tool`` the
    body of a 2xx reply, ``tool_error`` the ``error`` object of a refusal whose status is its code's SPEC status."""

    def __init__(self, client):
        self.client = client

    def call(self, name, **arguments):
        method, path = ROUTES[name]
        if 'request_id' in arguments:
            path = path.format(rid=quote(arguments.pop('request_id'), safe=''))
        if method == 'GET':
            if 'states' in arguments:
                arguments['state'] = arguments.pop('states')
            # Attack paths are sent as they are: '/' stays literal (a path of 2100 directories must fit the request
            # line) and a lone surrogate becomes its raw (invalid UTF-8) bytes, as the web tests do.
            parts = []
            for name, value in arguments.items():
                for item in value if isinstance(value, list) else [value]:
                    text = ('true' if item else 'false') if isinstance(item, bool) else str(item)
                    parts.append(f'{name}={quote(text, safe="/", errors="surrogatepass")}')
            return self.client.raw('GET', rest.PREFIX + path + ('?' + '&'.join(parts) if parts else ''))
        return self.client.call(method, path, arguments)

    def tool(self, name, **arguments):
        reply = self.call(name, **arguments)
        if not 200 <= reply.status < 300:
            raise AssertionError(f'{name} failed: {reply.status} {reply.body!r}')
        return reply.json

    def tool_error(self, name, **arguments):
        reply = self.call(name, **arguments)
        if reply.status < 400:
            raise AssertionError(f'{name} unexpectedly succeeded: {reply.status} {reply.body!r}')
        error = reply.error
        if reply.status != ERROR_STATUS[error['code']]:
            raise AssertionError(f'{name}: {error["code"]} came with HTTP {reply.status}')
        return error
