"""G-06 Manager roster/isolation fixtures; no OMP or scripted model claims."""
from pathlib import Path
import tempfile
import unittest

from workbench.backend import launcher


class IndependentManagerReportIntegration(unittest.TestCase):
    def observed(self, *, role, model_exposes_report=False, command_exposes_report=True):
        names = ["to-worker", "workbench-recovery"] if role == "manager" else ["to-manager"]
        prompt = launcher.DEFAULT_PROMPT_MARKER + "\n<skills>\n" + "\n".join(
            "- " + name + ": fixture skill" for name in names
        )
        if model_exposes_report:
            prompt += "\n- workbench-report: manual-only report fixture"
        prompt += "\n</skills>\n"
        command_names = names + (["workbench-report"] if command_exposes_report else [])
        return launcher.observe_isolation({"systemPrompt": prompt, "dumpTools": []},
            [{"name": "skill:" + name, "source": "skill"} for name in command_names])

    def test_shipped_role_rosters_expose_report_only_to_manager(self):
        self.assertEqual(set(launcher.role_skill_allowlist("manager")),
                         {"to-worker", "workbench-inbox", "workbench-recovery", "workbench-report"})  # TI-11: + inbox
        self.assertEqual(set(launcher.role_skill_allowlist("worker")), {"to-manager"})
        self.assertEqual(launcher.role_skill_allowlist("unknown"), ())
        with tempfile.TemporaryDirectory(prefix="wpr-manager-overlay-") as directory:
            root = Path(directory)
            for role in ("manager", "worker"):
                overlay = launcher.role_overlay(role, project_dir=root, home=root, environment={"HOME": str(root)})
                patterns = overlay["skills"]["includeSkills"]
                self.assertEqual("workbench-report" in patterns, role == "manager")

    def test_manager_command_only_is_allowed_and_model_exposure_is_rejected(self):
        allowed = ("to-worker", "workbench-inbox", "workbench-recovery", "workbench-report")
        manual = self.observed(role="manager")
        self.assertIn("workbench-report", manual["skill_commands"])
        self.assertNotIn("workbench-report", manual["skills"])
        self.assertEqual(launcher.isolation_leaks(manual, role="manager", allowed_skills=allowed), [])
        automatic = self.observed(role="manager", model_exposes_report=True)
        leaks = launcher.isolation_leaks(automatic, role="manager", allowed_skills=allowed)
        self.assertTrue(any("workbench-report" in leak for leak in leaks),
                        "manual-only skill in the model prompt must be an isolation failure")

    def test_worker_rejects_report_command_and_model_discovery(self):
        for model_exposure in (False, True):
            with self.subTest(model_exposure=model_exposure):
                observed = self.observed(role="worker", model_exposes_report=model_exposure)
                leaks = launcher.isolation_leaks(observed, role="worker", allowed_skills=("to-manager",))
                self.assertTrue(any("workbench-report" in leak for leak in leaks))


if __name__ == "__main__":
    unittest.main()
