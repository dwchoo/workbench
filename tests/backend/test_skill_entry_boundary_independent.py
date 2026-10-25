"""Installed entry/skill data boundaries; no Node loader or model compliance claim."""
from fnmatch import fnmatchcase
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from workbench.backend import launcher


class SkillEntryBoundaryIndependentTests(unittest.TestCase):
    def setUp(self):
        for boundary in ("subprocess.Popen", "socket.socket", "socket.create_connection"):
            tripwire = patch(boundary, side_effect=AssertionError("Native/network forbidden"))
            tripwire.start()
            self.addCleanup(tripwire.stop)

    def test_thin_entry_preserves_sdk_export_provenance_in_actual_bridge_binding(self):
        entry = Path(launcher.default_bridge_extension())
        text = entry.read_text()
        imported = {}
        for names, package in re.findall(r'import\s*\{([^}]+)\}\s*from\s*[\"\']([^\"\']+)[\"\']', text, re.S):
            for name in names.split(","):
                match = re.fullmatch(r"\s*(\w+)(?:\s+as\s+(\w+))?\s*", name)
                self.assertIsNotNone(match, "SDK import binding declaration")
                public, alias = match.groups()
                local = alias or public
                self.assertNotIn(local, imported)
                imported[local] = (package, public)
        alias, relative = re.search(r'import\s+(\w+)\s+from\s*[\"\'](\./[^\"\']+)[\"\']', text).groups()
        actual_bridge = (entry.parent / relative).resolve()
        self.assertEqual(actual_bridge, entry.with_name("bridge.ts").resolve())
        self.assertNotEqual(actual_bridge, entry.resolve())
        body = re.search(r'export\s+default\s+function\s+\w+\s*\(\s*(\w+)\s*:\s*any\s*\)\s*:\s*void\s*\{(.*?)\n\}', text, re.S)
        self.assertIsNotNone(body)
        parameter, source_body = body.groups()
        call = re.search(re.escape(alias) + r'\(\s*(\w+)\s*,\s*\{([^}]+)\}\s*\)', source_body, re.S)
        self.assertIsNotNone(call, "actual imported bridge receives the SDK object")
        self.assertEqual(call.group(1), parameter, "original extension API forwarded")
        forwarded = {}
        for member in call.group(2).split(","):
            key, separator, value = member.strip().partition(":")
            binding = value.strip() if separator else key
            self.assertIn(binding, imported, "no guessed SDK factory or substitute")
            self.assertNotIn(key, forwarded)
            forwarded[key] = imported[binding]
        # Public API provenance is the contract; spelling/whitespace/aliases of
        # the entry declarations are free to change without weakening this check.
        expected = {name: ("@oh-my-pi/pi-coding-agent", name) for name in
                    ("AgentRegistry", "createAgentSession", "SessionManager", "Settings")}
        expected.update({"toolWireSchema": ("@oh-my-pi/pi-ai", "toolWireSchema"),
                         "Tokenizer": ("@oh-my-pi/pi-agent-core", "Tokenizer"),
                         "fitOutputTokensToContextWindow": ("@oh-my-pi/pi-agent-core", "fitOutputTokensToContextWindow"),
                         "getBundledAgent": ("@oh-my-pi/pi-coding-agent/task/agents", "getBundledAgent")})
        self.assertEqual(forwarded, expected)
        bridge = actual_bridge.read_text()
        constructor = re.search(r'export\s+default\s+function\s+\w+\([^,]+,\s*(\w+)\??\s*:', bridge)
        self.assertIsNotNone(constructor, "actual bridge accepts injected SDK boundary")
        self.assertRegex(bridge, r"sdk:\s*" + re.escape(constructor.group(1)) + r"\b")

    def test_physical_internal_asset_is_excluded_by_overlay_and_rejected_by_ordinary_isolation(self):
        skills = launcher.default_skills_dir()
        physical = {path.parent.name for path in skills.glob("*/SKILL.md")}
        self.assertEqual(physical, {"to-worker", "to-manager", "workbench-inbox", "workbench-recovery", "important-log-summary", "workbench-report"})
        for role, expected in (("manager", {"to-worker", "workbench-inbox", "workbench-recovery", "workbench-report"}), ("worker", {"to-manager"})):
            with self.subTest(role=role):
                overlay = launcher.role_overlay(role, project_dir=skills, home=skills)
                offered = {name for name in physical if any(fnmatchcase(name, pattern)
                           for pattern in overlay["skills"]["includeSkills"])}
                self.assertEqual(offered, expected)
                self.assertEqual(set(launcher.role_skill_allowlist(role)), expected)
                # workbench-report is manual-only: a slash command, never in the model skill roster.
                observed = {"context_files": [], "skills": sorted(offered - {"workbench-report"}), "skill_commands": sorted(offered),
                            "rules": [], "mcp_tools": [], "task_agents": [], "other_commands": [],
                            "system_prompt_default": True}
                self.assertEqual(launcher.isolation_leaks(observed, allowed_skills=tuple(offered)), [])
                # The real internal asset is still a leak in either the model
                # roster or slash-command roster of an ordinary parent session.
                for surface in ("skills", "skill_commands"):
                    unexpected = {**observed, surface: [*observed[surface], "important-log-summary"]}
                    self.assertEqual(launcher.isolation_leaks(unexpected, allowed_skills=tuple(offered)),
                                     ["skill:important-log-summary"])


if __name__ == "__main__":
    unittest.main()
