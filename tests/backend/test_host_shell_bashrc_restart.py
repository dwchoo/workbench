"""HSB-D06: restart_pane{host_shell} starts the new shell with the same init, the user's ~/.bashrc included.

Real Backend + PersistentShell (Bash) with the fake OMP panes of test_shell_kill_restart; the temp HOME's
``.bashrc`` is written by the test after the first start, so only the restarted shell can have read it.
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))

import test_shell_kill_restart as base  # noqa: E402

from workbench.contracts.v1 import PaneId  # noqa: E402


class HostShellBashrcRestartTests(unittest.TestCase):
    setUp = base.ShellKillRestartTests.setUp
    close = base.ShellKillRestartTests.close
    wait = base.ShellKillRestartTests.wait
    mode = base.ShellKillRestartTests.mode
    type = base.ShellKillRestartTests.type

    def user_rc(self):
        return self.backend.snapshot()["panes"]["host_shell"]["shell"]["user_rc"]

    def exit_and_restart(self):
        old = self.backend.shell
        self.type("exit")
        self.wait(lambda: old._exited, "the shell exit")
        self.backend.restart_pane(PaneId.HOST_SHELL)
        self.assertIsNot(self.backend.shell, old)
        self.wait(lambda: self.mode() == "manual_prompt", "the restarted shell's prompt")

    def test_restarted_host_shell_applies_the_rc_and_falls_back_the_same_way(self):
        home = Path(self.env["HOME"])
        self.assertEqual(self.user_rc()["state"], "absent")

        (home / ".bashrc").write_text("export RCVAR=rc-ok\nalias wbal='echo alias-ok'\n")
        self.exit_and_restart()
        self.assertEqual(self.user_rc(), {"state": "applied", "path": str(home / ".bashrc"), "reason": None})
        out = self.root / "out"
        self.type(f'{{ echo "$RCVAR:$WORKBENCH_SHELL"; wbal; }} > {out}')
        self.wait(lambda: out.exists() and out.read_text().count("\n") == 2, "the rc's alias and variable")
        self.assertEqual(out.read_text(), "rc-ok:1\nalias-ok\n")

        (home / ".bashrc").write_text("export RCVAR=x\nexec sh\n")
        self.exit_and_restart()
        state = self.user_rc()
        self.assertEqual(state["state"], "fallback", state)
        self.assertIn("exec", state["reason"])


if __name__ == "__main__":
    unittest.main()
