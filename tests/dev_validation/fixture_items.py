"""Subprocess-only fixtures used to test the developer runner itself."""

import os
from pathlib import Path
import subprocess
import sys
import time
import unittest


class RunnerFixture(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(True)

    def test_fail(self):
        self.fail("first assertion failure")

    def test_hang(self):
        time.sleep(3)

    def test_cross_exec_backend_finishes_before_suite(self):
        """A separate CLI Python double-forks a backend that exits before the first scan."""
        script = '''
import os, sys, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from workbench.backend import cli
from workbench.backend.paths import DataLayout
root = Path(os.environ["WB_DVL_NAMESPACE"])
data = root / "data"
data.mkdir(mode=0o700)
ready = root / "backend.ready"
bootstrap = (
    "import os; from pathlib import Path; "
    "start=int(Path('/proc/self/stat').read_bytes().rsplit(b') ',1)[1].split()[19]); "
    "Path(os.environ['WB_DVL_NAMESPACE'], 'backend.ready').write_text(f'{os.getpid()} {start}')"
)
with patch.object(cli, "_bootstrap_source", return_value=bootstrap):
    pid = cli.spawn_backend(DataLayout(data), SimpleNamespace(to_argv=lambda: []), "/")
deadline = time.monotonic() + 2
while not ready.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if not ready.exists():
    raise SystemExit("controlled backend did not write ready receipt")
print("BACKEND_EXITED_PID=" + ready.read_text().strip(), flush=True)
'''
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, timeout=5, check=True)
        receipt = result.stdout.strip()
        self.assertTrue(receipt.startswith("BACKEND_EXITED_PID="), receipt)
        print(receipt, flush=True)
        # The separate CLI is gone. The backend has already exited by the time
        # this test completes and the parent runner starts its normal cleanup.
        pid, start = map(int, receipt.removeprefix("BACKEND_EXITED_PID=").split())
        self.assertEqual(int((Path(os.environ["WB_DVL_NAMESPACE"]) / "backend.ready").read_text().split()[0]), pid)
        self.assertGreater(start, 0)


class EmptyFixture:
    pass
