"""SPEC §6.16: both roles may query logs; worker ambient isolation remains strict.

These are observation-predicate tests, not native OMP or model evidence. Read-only
actions and the real bridge port are covered by bridge_dvl07_tools_independent.
"""
from __future__ import annotations

import unittest

from workbench.backend import launcher


class WorkerLogsAllowlistTests(unittest.TestCase):
    # Requirement-owned examples, deliberately not imported from launcher allowlists.
    WORKER = ("read", "grep", "glob", "edit", "write", "web_search", "todo", "task",
              "to_manager", "terminal", "workbench_logs")
    MANAGER = ("read", "grep", "glob", "edit", "write", "bash", "eval", "web_search",
               "todo", "task", "wait", "to_worker", "terminal", "workbench_logs")

    def observed(self, tools, **changes):
        observed = launcher.observe_isolation({
            "systemPrompt": ["You are omp's coding assistant."],
            "dumpTools": [{"name": name, "description": ""} for name in tools],
        }, [])
        observed.update(changes)
        return observed

    def leaks(self, tools=None, **changes):
        return launcher.isolation_leaks(
            self.observed(self.WORKER if tools is None else tools, **changes),
            allowed_skills=(), role="worker")

    def test_worker_readonly_logs_with_approved_tools_is_not_an_ambient_leak(self):
        self.assertEqual(self.leaks(), [])

    def test_bash_and_eval_remain_worker_tool_leaks(self):
        for tool in ("bash", "eval"):
            with self.subTest(tool=tool):
                self.assertEqual(self.leaks((*self.WORKER, tool)), [f"tool:{tool}"])

    def test_unknown_workbench_prefix_does_not_grant_permission(self):
        self.assertEqual(self.leaks((*self.WORKER, "workbench_fake")),
                         ["tool:workbench_fake"])

    def test_mcp_browser_and_ambient_task_agents_remain_leaks(self):
        with self.subTest(channel="mcp"):
            self.assertEqual(self.leaks((*self.WORKER, "mcp__fixture"),
                                       mcp_tools=["mcp__fixture"]),
                             ["mcp:mcp__fixture"])
        with self.subTest(channel="browser"):
            self.assertEqual(self.leaks((*self.WORKER, "browser"), browser=True),
                             ["tool:browser"])
        with self.subTest(channel="task_agent"):
            self.assertEqual(self.leaks(task_agents=["explorer", "ambient_agent"]),
                             ["task_agent:ambient_agent"])

    def test_manager_original_tools_and_readonly_logs_remain_allowed(self):
        observed = self.observed(self.MANAGER,
                                 task_agents=["scout", "reviewer", "security-reviewer", "task", "sonic"])
        self.assertEqual(launcher.isolation_leaks(observed, allowed_skills=(),
                                                 role="manager"), [])


if __name__ == "__main__":
    unittest.main()
