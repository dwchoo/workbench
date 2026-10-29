"""`wb hub status` and the stop question for the Board terminals (web-board-ux SPEC s4 "Hub 상태·정지·nav").

Expected strings are the user-visible sentences of the SPEC (``board_agent: agent 2/4 실행 중 · shell 3 · 연결 5``,
one indented line per failed terminal, ``registry_invalid`` with its message, the stop list with names and states),
not the product's helpers. Pure functions, no Hub process.
"""
from __future__ import annotations

import unittest

from workbench.hub import cli

MESSAGE = ("OMP selection failed (selected /h/.local/bin/omp): unsafe_managed_ancestry: /h/.local/bin is "
           "group-writable (mode 775). Fix: chmod g-w /h/.local/bin, then run wb setup.")


def view(name, kind='agent', state='running', clients=0, **more):
    return {'id': f'{kind[0]}-{name}', 'kind': kind, 'name': name, 'state': state, 'pid': 7, 'clients': clients,
            'detail': more.pop('detail', None), **more}


class BoardStatusLines(unittest.TestCase):
    def test_the_board_line_counts_running_agents_of_the_limit_shells_and_connections(self):
        board = {'state': 'ok', 'options': {'agent_limit': 4, 'shell_limit': 10},
                 'agents': [view('agent-1', clients=2), view('전처리', clients=1), view('agent-3', state='stopped')],
                 'shells': [view('shell-1', 'shell', clients=2), view('shell-2', 'shell'),
                            view('shell-3', 'shell', state='exited')]}
        self.assertEqual(cli.board_lines(board), ['board_agent: agent 2/4 실행 중 · shell 3 · 연결 5'])

    def test_a_failed_terminal_gets_one_indented_line_with_its_message_or_detail(self):
        board = {'state': 'ok', 'options': {'agent_limit': 4, 'shell_limit': 10},
                 'agents': [view('agent-1', state='failed', detail='omp_unavailable', message=MESSAGE),
                            view('agent-2', state='isolation_failed', detail='leak:mcp:x')],
                 'shells': [view('shell-1', 'shell', state='failed', detail='shell_unavailable')]}
        self.assertEqual(cli.board_lines(board), [
            'board_agent: agent 0/4 실행 중 · shell 1 · 연결 0',
            f'    agent-1: {MESSAGE}', '    agent-2: leak:mcp:x', '    shell-1: shell_unavailable'])

    def test_an_invalid_registry_shows_its_message(self):
        board = {'state': 'registry_invalid', 'message': 'Board registry /x/registry.json is unusable (not JSON).',
                 'options': {'agent_limit': 4, 'shell_limit': 10}, 'agents': [], 'shells': []}
        self.assertEqual(cli.board_lines(board), ['board_agent: registry_invalid',
                                                  '    Board registry /x/registry.json is unusable (not JSON).'])

    def test_no_board_is_none(self):
        self.assertEqual(cli.board_lines(None), ['board_agent: 없음'])

    def test_the_stop_question_names_every_live_terminal_and_its_state(self):
        item = {'kind': 'board_agent_set', 'terminals': [
            {'id': 'a-1', 'kind': 'agent', 'name': '전처리', 'pid': 11, 'state': 'running'},
            {'id': 's-1', 'kind': 'shell', 'name': 'shell-1', 'pid': 12, 'state': 'running'}]}
        self.assertEqual(cli.active_text(item),
                         'Board terminal: agent 전처리 running(pid 11) · shell shell-1 running(pid 12)')


if __name__ == '__main__':
    unittest.main()
