"""Small, credential-free subprocess fixtures for independent runner contracts."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


if os.environ.get("WB_DVL_SPOOF") == "zero":
    os.write(2, b"Ran 1 test in 0.000s\n\nOK\n")
elif os.environ.get("WB_DVL_SPOOF") == "skipped":
    os.write(2, b"OK (skipped=0)\n")


class NoCollectedTests(unittest.TestCase):
    pass


class IndependentFixture(unittest.TestCase):
    def test_pass(self) -> None:
        self.assertTrue(True)

    @unittest.skip("deliberate fully skipped collection")
    def test_skip(self) -> None:
        self.fail("skip marker must not execute")

    def test_fail(self) -> None:
        self.fail("independent first-failure marker")

    def test_hostile_environment_is_replaced(self) -> None:
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TMUX", "HERDR_SOCKET",
                    "WORKBENCH_DATA_DIR", "PI_CONFIG_DIR", "SSH_AUTH_SOCK"):
            self.assertNotIn(key, os.environ)
        namespace = Path(os.environ["WB_DVL_NAMESPACE"])
        home = Path(os.environ["HOME"])
        self.assertTrue(home.is_relative_to(namespace))
        self.assertEqual(Path(os.environ["TMPDIR"]), namespace)
        self.assertEqual(Path(os.environ["XDG_CONFIG_HOME"]), home / "config")

    def test_bounded_both_streams(self) -> None:
        os.write(1, b"STDOUT-BEGIN|" + b"o" * 40_000 + b"|STDOUT-END")
        os.write(2, b"STDERR-BEGIN|" + b"e" * 40_000 + b"|STDERR-END\n")

    def test_silent_stall(self) -> None:
        time.sleep(2)

    def test_chatter_without_predicate(self) -> None:
        for _ in range(15):
            os.write(1, b"still waiting; no readiness predicate\n")
            time.sleep(0.08)

    def test_descendant_holds_pipes_without_namespace_in_argv(self) -> None:
        # The child belongs to this item/session and inherits both pipes, but its
        # argv/cwd contain no private namespace marker. It is bounded as a safety net.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                                 stdin=subprocess.DEVNULL)
        print(f"OWNED_CHILD_PID={child.pid}", flush=True)

    def test_fake_unittest_progress_without_completed_assertions(self) -> None:
        for index in range(15):
            os.write(1, f"stdout marker {index}: Ran 1 test in 0.000s\n".encode())
            os.write(2, f"test_fake_{index} (fake.Case) ... ok\n".encode())
            time.sleep(0.08)

    def test_detached_devnull_helper_outside_namespace(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, cwd="/", start_new_session=True)
        print(f"DETACHED_CHILD_PID={child.pid}", flush=True)

    def test_product_doublefork_backend_waits_without_progress(self) -> None:
        from tests.integration import cw16_harness as harness
        from workbench.backend import cli
        from workbench.backend.paths import DataLayout

        namespace = Path(os.environ["WB_DVL_NAMESPACE"])
        data = namespace / "data"
        data.mkdir(mode=0o700)
        ready = namespace / "backend.ready"
        bootstrap = (
            "import os,time; from pathlib import Path; "
            "start=int(Path('/proc/self/stat').read_bytes().rsplit(b') ',1)[1].split()[19]); "
            "Path(os.environ['WB_DVL_NAMESPACE'], 'backend.ready').write_text(f'{os.getpid()} {start}'); "
            "time.sleep(8)"
        )
        with patch.object(cli, "_bootstrap_source", return_value=bootstrap):
            pid = cli.spawn_backend(DataLayout(data), SimpleNamespace(to_argv=lambda: []), "/")
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "controlled backend did not reach ready state")
        recorded_pid, recorded_ticks = map(int, ready.read_text().split())
        self.assertEqual(recorded_pid, pid)
        ticks = harness.ticks(pid)
        self.assertEqual(ticks, recorded_ticks)
        print(f"BACKEND_READY_PID={pid} START_TICKS={ticks}", flush=True)
        time.sleep(5)

    def test_cli_exec_doublefork_backend_waits_without_progress(self) -> None:
        script = """
import os
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import patch
from workbench.backend import cli
from workbench.backend.paths import DataLayout

namespace = Path(os.environ['WB_DVL_NAMESPACE'])
data = namespace / 'cli-data'
data.mkdir(mode=0o700)
bootstrap = (
    "import os,time; from pathlib import Path; "
    "start=int(Path('/proc/self/stat').read_bytes().rsplit(b') ',1)[1].split()[19]); "
    "Path(os.environ['WB_DVL_NAMESPACE'], 'cli-backend.ready').write_text(f'{os.getpid()} {start}'); "
    "time.sleep(8)"
)
with patch.object(cli, '_bootstrap_source', return_value=bootstrap):
    pid = cli.spawn_backend(DataLayout(data), SimpleNamespace(to_argv=lambda: []), '/')
ready = namespace / 'cli-backend.ready'
deadline = time.monotonic() + 2
while not ready.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
assert ready.exists(), 'controlled CLI backend did not reach ready state'
recorded_pid, recorded_ticks = map(int, ready.read_text().split())
assert recorded_pid == pid
print(f'CLI_BACKEND_READY_PID={pid} START_TICKS={recorded_ticks}', flush=True)
"""
        completed = subprocess.run([sys.executable, "-c", script],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   cwd=Path(__file__).resolve().parents[2], timeout=4,
                                   env=os.environ.copy())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("CLI_BACKEND_READY_PID=", completed.stdout)
        print(completed.stdout.strip(), flush=True)
        time.sleep(5)

    def test_cli_exec_backend_is_zombie_before_suite_completion(self) -> None:
        from tests.integration import cw16_harness as harness

        script = """
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from workbench.backend import cli
from workbench.backend.paths import DataLayout

namespace = Path(os.environ['WB_DVL_NAMESPACE'])
data = namespace / 'early-exit-data'
data.mkdir(mode=0o700)
bootstrap = (
    "import os; from pathlib import Path; "
    "start=int(Path('/proc/self/stat').read_bytes().rsplit(b') ',1)[1].split()[19]); "
    "Path(os.environ['WB_DVL_NAMESPACE'], 'early-exit.ready').write_text(f'{os.getpid()} {start}')"
)
with patch.object(cli, '_bootstrap_source', return_value=bootstrap):
    pid = cli.spawn_backend(DataLayout(data), SimpleNamespace(to_argv=lambda: []), '/')
print(f'CLI_SPAWNED_PID={pid}', flush=True)
"""
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                   cwd=Path(__file__).resolve().parents[2], timeout=4,
                                   env=os.environ.copy())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.strip().startswith("CLI_SPAWNED_PID="), completed.stdout)
        spawned = int(completed.stdout.strip().removeprefix("CLI_SPAWNED_PID="))
        ready = Path(os.environ["WB_DVL_NAMESPACE"]) / "early-exit.ready"
        deadline = time.monotonic() + 2
        while not ready.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.is_file(), "native backend did not leave a ready receipt")
        pid, start = map(int, ready.read_text().split())
        self.assertEqual(pid, spawned)
        fields = harness.stat_fields(pid)
        while (fields is None or fields[0] != b"Z") and time.monotonic() < deadline:
            time.sleep(0.01)
            fields = harness.stat_fields(pid)
        self.assertIsNotNone(fields, "native backend disappeared before Z identity was observed")
        self.assertEqual((fields[0], int(fields[1]), harness.ticks(pid)),
                         (b"Z", os.getppid(), start),
                         "backend must be an exact adopted zombie before this test emits its suite result")
        print(f"EARLY_NATIVE_Z_PID={pid} START_TICKS={start}", flush=True)
