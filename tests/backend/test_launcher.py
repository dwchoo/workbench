"""DVL-09 entry selection; no OMP or model process is started."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from workbench.backend import launcher
from workbench.terminal.shell_g2.prototype import ShellChoice


class SDKEntryLauncherTests(unittest.TestCase):
    def test_default_launch_plan_uses_the_shipped_static_sdk_entry(self):
        with patch.object(launcher, "choose_shell", return_value=ShellChoice("bash", "/bin/bash")), \
                patch.object(launcher, "omp_version", return_value="fixture-version"):
            plan = launcher.build_plan({}, omp=sys.executable)
        entry = launcher.default_bridge_extension()
        self.assertTrue(entry.is_file())
        self.assertEqual(entry.name, "entry.ts")
        self.assertEqual(plan.bridge_extension, str(entry.resolve()))

    def test_explicit_bridge_override_is_preserved(self):
        bridge = launcher.default_bridge_extension().with_name("bridge.ts")
        with patch.object(launcher, "choose_shell", return_value=ShellChoice("bash", "/bin/bash")), \
                patch.object(launcher, "omp_version", return_value="fixture-version"):
            plan = launcher.build_plan({}, omp=sys.executable, bridge_extension=str(bridge))
        self.assertEqual(plan.bridge_extension, str(bridge.resolve()))


if __name__ == "__main__":
    unittest.main()
