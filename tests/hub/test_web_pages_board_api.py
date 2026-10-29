"""WBX-D1 behaviour 13: the Board session API (web-board-ux SPEC s4 "Session API: 화면과 panel 동작"; seams S1 + S6).

The real ``create_app`` with ``board_routes`` over a real :class:`BoardSet` whose terminals run the recorder of
``support/board_stub.py`` (no OMP). Expected values: the SPEC table (paths, bodies, success shapes) and its error
code/status list, the session/CSRF rules (401 without a session, 403 without the session's ``X-CSRF-Token``, a Bearer
API key never reaches it), ``Cache-Control: no-store`` and the removed routes.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_hub_web_auth as web_auth  # noqa: E402  (module import: its test classes stay uncollected here)
from support.board_stub import RecorderLauncher  # noqa: E402

from workbench.hub.board.set import BoardSet  # noqa: E402
from workbench.hub.web.app import board_routes  # noqa: E402

CODES = {  # SPEC s4 error codes -> status
    'bad_input': 400, 'name_invalid': 400, 'confirm_required': 400, 'limit_out_of_range': 400, 'wrong_kind': 400,
    'bad_state': 400, 'unknown_terminal': 404, 'name_taken': 409, 'limit_reached': 409, 'limit_below_count': 409,
    'terminal_busy': 409, 'registry_write_failed': 500, 'delete_failed': 500, 'board_unavailable': 503,
    'registry_invalid': 503,
}


class BoardApiCase(web_auth.WebAppCase):
    async def asyncSetUp(self):
        self.board_ref = [None]
        self.routes = (board_routes(lambda: self.board_ref[0]),)
        await super().asyncSetUp()
        self.launcher = RecorderLauncher(Path(self.temp.name) / 'terminals')
        self.board = self.board_ref[0] = BoardSet(self.launcher, grace=1.0)
        self.addAsyncCleanup(self.stop_board)
        self.session = self.services.sessions.create('127.0.0.1', 'test', time.time(), generation=self.generation)
        self.cookies = {'wb_hub_session': self.session.token}

    async def stop_board(self):
        if self.board_ref[0] is not None:
            await self.board_ref[0].stop()

    async def call(self, method: str, path: str, body=None, *, csrf: str | None = '', cookies=None, raw=None,
                   headers=None):
        sent = {'Host': self.host(), 'Origin': self.origin, **(headers or {})}
        jar = self.cookies if cookies is None else cookies
        if jar:
            sent['Cookie'] = '; '.join(f'{name}={value}' for name, value in jar.items())
        if csrf is not None:
            sent['X-CSRF-Token'] = csrf or self.session.csrf
        data = raw if raw is not None else (None if body is None else json.dumps(body))
        if data is not None:
            sent.setdefault('Content-Type', 'application/json')
        async with self.http.request(method, self.base + path, data=data, headers=sent,
                                     allow_redirects=False) as response:
            text = await response.text()
            try:
                response.json_body = json.loads(text)
            except ValueError:
                response.json_body = None
            response.body_text = text
            return response

    async def refused(self, response, code: str):
        self.assertEqual((response.status, response.json_body and response.json_body.get('reason')),
                         (CODES[code], code), response.body_text)
        self.assertIs(response.json_body['ok'], False)
        self.assertIsInstance(response.json_body['detail'], str)
        self.assertTrue(response.json_body['detail'])

    async def first_agent(self) -> dict:
        return (await self.call('GET', '/api/board')).json_body['terminals'][0]


class ReadAndAccessTests(BoardApiCase):
    async def test_get_board_is_session_only_no_store_with_ranges_and_terminals(self):
        self.assertEqual((await self.call('GET', '/api/board', cookies={})).status, 401)
        key = self.services.api_keys.issue('editor-one', 'editor', {'all': True}, None, now=time.time()).raw
        bearer = await self.call('GET', '/api/board', cookies={}, headers={'Authorization': f'Bearer {key}'})
        self.assertEqual(bearer.status, 401)
        response = await self.call('GET', '/api/board')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['Content-Type'].split(';')[0], 'application/json')
        body = response.json_body
        self.assertEqual((body['ok'], body['board'], body['options']),
                         (True, {'state': 'ok'}, {'agent_limit': 4, 'shell_limit': 10, 'agent_range': [1, 8],
                                                  'shell_range': [1, 32]}))
        terminal, = body['terminals']
        self.assertEqual({key: terminal[key] for key in ('kind', 'name', 'state', 'clients', 'busy')},
                         {'kind': 'agent', 'name': 'agent-1', 'state': 'not_started', 'clients': 0, 'busy': False})

    async def test_every_state_change_needs_the_sessions_csrf_header_and_a_bearer_key_is_401(self):
        key = self.services.api_keys.issue('editor-two', 'editor', {'all': True}, None, now=time.time()).raw
        for path, body in (('/api/board/open', {'visible': []}), ('/api/board/agents', {}), ('/api/board/shells', {}),
                           ('/api/board/options', {'agent_limit': 5})):
            with self.subTest(path):
                self.assertEqual((await self.call('POST', path, body, csrf=None)).status, 403)
                self.assertEqual((await self.call('POST', path, body, csrf='x' * 43)).status, 403)
                bearer = await self.call('POST', path, body, cookies={}, csrf=None,
                                         headers={'Authorization': f'Bearer {key}'})
                self.assertEqual(bearer.status, 401)
        self.assertEqual(self.board.views()[0]['state'], 'not_started')
        self.assertEqual(self.board.options(), {'agent_limit': 4, 'shell_limit': 10})
        self.assertEqual(len(self.board.views()), 1)

    async def test_the_old_board_routes_are_gone(self):
        for method, path in (('GET', '/board/restart?channel=agent'), ('POST', '/board/restart'), ('GET', '/ws/board')):
            with self.subTest(path):
                self.assertEqual((await self.call(method, path, csrf='')).status, 404)

    async def test_without_a_board_every_route_is_503_board_unavailable(self):
        await self.board.stop()
        self.board_ref[0] = None
        await self.refused(await self.call('GET', '/api/board'), 'board_unavailable')
        await self.refused(await self.call('POST', '/api/board/agents', {}), 'board_unavailable')

    async def test_an_invalid_registry_is_reported_and_refuses_actions(self):
        await self.board.stop()
        (Path(self.launcher.state_dir) / 'board-agents' / 'registry.json').write_text('[]')
        self.board = self.board_ref[0] = BoardSet(self.launcher, grace=1.0)
        response = await self.call('GET', '/api/board')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json_body['board']['state'], 'registry_invalid')
        self.assertIn('registry.json', response.json_body['board']['message'])
        self.assertEqual(response.json_body['terminals'], [])
        await self.refused(await self.call('POST', '/api/board/open', {'visible': []}), 'registry_invalid')


class ActionTests(BoardApiCase):
    async def test_open_makes_shell_1_and_starts_the_visible_agent(self):
        agent = await self.first_agent()
        response = await self.call('POST', '/api/board/open', {'visible': [agent['id']]})
        self.assertEqual(response.status, 200, response.body_text)
        await self.board.settled()
        terminals = response.json_body['terminals']
        self.assertEqual([(t['kind'], t['name']) for t in terminals], [('agent', 'agent-1'), ('shell', 'shell-1')])
        self.assertIn(terminals[0]['state'], ('starting', 'running'))
        self.assertEqual(terminals[1]['state'], 'running')

    async def test_create_rename_options_stop_start_restart_close_delete(self):
        made = await self.call('POST', '/api/board/agents', {})
        self.assertEqual(made.status, 201, made.body_text)
        agent = made.json_body['terminal']
        self.assertEqual((made.json_body['ok'], agent['name']), (True, 'agent-2'))
        shell = await self.call('POST', '/api/board/shells', {})
        self.assertEqual(shell.status, 201, shell.body_text)
        shell = shell.json_body['terminal']
        renamed = await self.call('POST', f"/api/board/terminals/{agent['id']}/rename", {'name': ' 전처리 '})
        self.assertEqual((renamed.status, renamed.json_body['terminal']['name']), (200, '전처리'))
        options = await self.call('POST', '/api/board/options', {'agent_limit': 6})
        self.assertEqual((options.status, options.json_body), (200, {'ok': True, 'options': {'agent_limit': 6,
                                                                                            'shell_limit': 10}}))
        await self.board.settled()
        stopped = await self.call('POST', f"/api/board/terminals/{agent['id']}/stop", {'confirm': True})
        self.assertEqual((stopped.status, stopped.json_body['terminal']['state']), (200, 'stopped'))
        started = await self.call('POST', f"/api/board/terminals/{agent['id']}/start", {})
        self.assertEqual(started.status, 200, started.body_text)
        restarted = await self.call('POST', f"/api/board/terminals/{shell['id']}/restart", {'confirm': True})
        self.assertEqual((restarted.status, restarted.json_body['terminal']['state']), (200, 'running'))
        closed = await self.call('POST', f"/api/board/terminals/{shell['id']}/close", {'confirm': True})
        self.assertEqual((closed.status, closed.json_body), (200, {'ok': True}))
        deleted = await self.call('POST', f"/api/board/terminals/{agent['id']}/delete", {'confirm': True})
        self.assertEqual((deleted.status, deleted.json_body), (200, {'ok': True}))
        self.assertEqual([view['name'] for view in self.board.views()], ['agent-1'])

    async def test_each_refusal_has_its_code_and_status(self):
        agent = await self.first_agent()
        shell = (await self.call('POST', '/api/board/shells', {})).json_body['terminal']
        base = f"/api/board/terminals/{agent['id']}"
        for body in ({}, {'confirm': 1}, {'confirm': 'true'}, {'confirm': False}):
            await self.refused(await self.call('POST', base + '/stop', body), 'confirm_required')
        await self.refused(await self.call('POST', base + '/stop', {'confirm': True}), 'bad_state')
        await self.refused(await self.call('POST', base + '/close', {'confirm': True}), 'wrong_kind')
        await self.refused(await self.call('POST', f"/api/board/terminals/{shell['id']}/delete", {'confirm': True}),
                           'wrong_kind')
        await self.refused(await self.call('POST', '/api/board/terminals/a-ffffffffffff/start', {}),
                           'unknown_terminal')
        await self.refused(await self.call('POST', '/api/board/terminals/nonsense/start', {}), 'unknown_terminal')
        await self.refused(await self.call('POST', base + '/rename', {'name': ''}), 'name_invalid')
        await self.refused(await self.call('POST', base + '/rename', {'name': 7}), 'name_invalid')
        second = (await self.call('POST', '/api/board/agents', {})).json_body['terminal']
        await self.refused(await self.call('POST', f"/api/board/terminals/{second['id']}/rename", {'name': 'AGENT-1'}),
                           'name_taken')
        await self.refused(await self.call('POST', '/api/board/options', {'agent_limit': 9}), 'limit_out_of_range')
        await self.refused(await self.call('POST', '/api/board/options', {'agent_limit': 1}), 'limit_below_count')
        await self.call('POST', '/api/board/options', {'agent_limit': 2})
        await self.refused(await self.call('POST', '/api/board/agents', {}), 'limit_reached')
        for raw in ('not json', '[]', '{"visible": "x"}', json.dumps({'visible': ['a'] * 41}),
                    json.dumps({'visible': [1]}), json.dumps({'visible': [], 'extra': 1}),
                    json.dumps({'visible': ['a' * 5000]})):
            with self.subTest(raw=raw[:30]):
                await self.refused(await self.call('POST', '/api/board/open', raw=raw), 'bad_input')
        await self.refused(await self.call('POST', '/api/board/agents', {'name': 'x'}), 'bad_input')
        await self.refused(await self.call('POST', base + '/rename', {'name': 'x', 'more': 1}), 'bad_input')
        await self.refused(await self.call('POST', '/api/board/agents', raw='{}',
                                           headers={'Content-Type': 'text/plain'}), 'bad_input')


if __name__ == '__main__':
    unittest.main()
