"""TI-18 / seam T10: run the Board page's node tests (``tests/hub/js/*.test.mjs``) from the unittest suite.

``board_state.test.mjs`` covers the pure state machine (``static/board-state.js``), ``board_glue.test.mjs`` the
DOM glue (``static/board.js``) against stub globals. Node is only a test runner here (no package, no install);
the case is skipped where ``node`` is missing. Run with ``discover -s tests/hub -p 'test_board_state_js.py'``.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest

JS_DIR = Path(__file__).resolve().parent / 'js'


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class BoardNodeTests(unittest.TestCase):
    def test_node_suites_pass(self):
        files = sorted(str(path) for path in JS_DIR.glob('*.test.mjs'))
        self.assertGreaterEqual(len(files), 2)
        result = subprocess.run(['node', '--test', *files], capture_output=True, text=True, timeout=120,
                                stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-1000:])
        self.assertRegex(result.stdout, r'# fail 0|ℹ fail 0')


if __name__ == '__main__':
    unittest.main()
