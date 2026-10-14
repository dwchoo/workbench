"""DVL-09 entry selection; no OMP or model process is started."""
import json
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

    def test_manager_summary_selection_reuses_worker_default_and_strips_ambient_selection(self):
        table = launcher.load_model_table()
        table["manager"]["models"]["task"] = "fixture/other:high"
        table["worker"]["models"]["default"] = "fixture/worker:max"
        plan = launcher.LaunchPlan(ShellChoice("bash", "/bin/bash"), sys.executable, "fixture", "bridge.ts")
        base = {"WORKBENCH_G3_SUMMARY_SELECTION": "ambient"}
        env = launcher.omp_environment(base, plan, role="manager", token="fixture",
            bridge_socket=Path("/tmp/fixture.sock"), models=table)
        selection = json.loads(env["WORKBENCH_G3_SUMMARY_SELECTION"])
        self.assertEqual(selection["selector"], "fixture/worker:max")
        self.assertEqual(selection["source"], "configured_worker_default")
        self.assertEqual(len(selection["modelTableDigest"]), 64)
        worker = launcher.omp_environment(base, plan, role="worker", token="fixture",
            bridge_socket=Path("/tmp/fixture.sock"), models=table)
        self.assertNotIn("WORKBENCH_G3_SUMMARY_SELECTION", worker)
        self.assertNotIn("WORKBENCH_G3_SUMMARY_SELECTION", launcher.shell_environment(base))
        table["worker"]["models"]["default"] = "fixture/new:high"
        next_env = launcher.omp_environment(base, plan, role="manager", token="fixture",
            bridge_socket=Path("/tmp/fixture.sock"), models=table)
        self.assertNotEqual(json.loads(next_env["WORKBENCH_G3_SUMMARY_SELECTION"])["modelTableDigest"], selection["modelTableDigest"])


if __name__ == "__main__":
    unittest.main()
