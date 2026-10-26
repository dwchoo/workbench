"""`wb hub status` text for a failed Board agent: the explanation (directory and fix) follows the board line.

Expected strings are the user-visible sentences, not the product's helpers. Pure functions, no Hub process.
"""
from __future__ import annotations

import unittest

from workbench.hub import cli

MESSAGE = ("OMP selection failed (selected /h/.local/bin/omp): unsafe_managed_ancestry: /h/.local/bin is "
           "group-writable (mode 775). Fix: chmod g-w /h/.local/bin, then run wb setup.")


class BoardFailureLines(unittest.TestCase):
    def test_a_failed_agent_with_a_message_gets_one_indented_line_naming_the_channel(self):
        board = {'agent': {'state': 'failed', 'detail': 'omp_unavailable', 'message': MESSAGE, 'pid': None},
                 'shell': {'state': 'running', 'pid': 5}, 'lease': {'state': 'none'}}
        self.assertEqual(cli.board_failure_lines(board), [f'    agent: {MESSAGE}'])

    def test_no_message_means_no_line(self):
        board = {'agent': {'state': 'failed', 'detail': 'omp_unavailable'}, 'shell': {'state': 'running'}}
        self.assertEqual(cli.board_failure_lines(board), [])
        self.assertEqual(cli.board_failure_lines({}), [])

    def test_the_board_line_itself_is_unchanged(self):
        board = {'agent': {'state': 'failed', 'message': MESSAGE}, 'shell': {'state': 'running', 'pid': 5}}
        self.assertEqual(cli.board_text(board, 'none'), 'agent failed · shell running(pid 5) · 연결 없음')


if __name__ == '__main__':
    unittest.main()
