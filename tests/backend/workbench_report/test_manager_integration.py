"""WPR-04: shipped assets and role isolation; no OMP/model process is started."""
from pathlib import Path
import tempfile
import unittest

from workbench.backend import launcher


class ManagerReportIntegrationTests(unittest.TestCase):
    def test_shipped_report_assets_resolve_from_manager_skill(self):
        directory = launcher.default_skills_dir() / "workbench-report"
        text = (directory / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(launcher._frontmatter_name(directory / "SKILL.md"), "workbench-report")
        self.assertIn("disableModelInvocation: true", text.split("---", 2)[1])
        for relative in ("references/report-template.md", "scripts/report_io.py"):
            self.assertIn(relative, text)
            self.assertTrue((directory / relative).is_file(), relative)

    def test_role_overlays_offer_report_only_to_manager(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for role, expected in (
                ("manager", {"to-worker", "workbench-inbox", "workbench-recovery", "workbench-report"}),
                ("worker", {"to-manager"}),
            ):
                with self.subTest(role=role):
                    overlay = launcher.role_overlay(role, project_dir=root, home=root)
                    actual = launcher.workbench_skill_names(
                        launcher.default_skills_dir(), overlay["skills"]["includeSkills"])
                    self.assertEqual(set(actual), expected)
                    self.assertEqual(set(launcher.role_skill_allowlist(role)), expected)
                    self.assertEqual(overlay["skills"]["ignoredSkills"], [])
        self.assertEqual(launcher.role_skill_allowlist("unknown"), ())

    def test_report_connection_does_not_widen_worker_tools_or_agents(self):
        self.assertEqual(launcher.WORKER_TOOLS,
                         ("read", "grep", "glob", "web_search", "todo", "task"))  # MWR-D07: no edit/write
        self.assertEqual(launcher.WORKER_BRIDGE_TOOLS, {"to_manager", "terminal", "workbench_logs", "workbench_records"})
        self.assertEqual(launcher.ROLE_TASK_AGENTS["worker"], {"explorer"})
        self.assertNotIn("important-log-summary", launcher.role_skill_allowlist("manager"))
        self.assertNotIn("workbench-report", launcher.role_skill_allowlist("worker"))


if __name__ == "__main__":
    unittest.main()
