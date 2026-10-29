"""manager-worker-roles MWR-AC-01..03: the always-on role prompts in the OMP argv.

Seam: ``launcher.omp_command`` (the argv the backend spawns at start and on every pane restart). The rules are
checked in the ``--append-system-prompt`` value, so a fresh OMP session (e.g. after Ctrl+D and a restart) has them
before it has loaded any skill. No OMP, no model.
"""
from pathlib import Path
import re
import unittest

from workbench.backend import launcher
from workbench.backend.launcher import LaunchPlan, omp_command

PLAN = LaunchPlan(None, "/x/omp", "omp/test", "/x/bridge.ts")  # type: ignore[arg-type]


def appended(role: str) -> str:
    argv = omp_command(PLAN, Path(f"/d/omp-isolation-{role}.yml"))
    assert argv.count("--append-system-prompt") == 1, argv
    return argv[argv.index("--append-system-prompt") + 1]


def flat(text: str) -> str:
    return " ".join(text.split())


MANAGER_RULES = {
    "plan first": r"(?i)plan first",
    "multi-step request, trivial command needs no plan":
        r"(?i)on a work order or a multi-step request, plan first \(a single trivial command needs no plan\)",
    "units of one simple command or script": r"(?i)units?[^.]*one simple command or one short script",
    "plan in todo": r"`todo`",
    "plan checklist file": r"\.wb/plans/<YYYYMMDD>-<slug>\.md",
    "checkboxes it ticks": r"(?i)checkbox",
    "manager writes every file": r"(?i)you write every script and file the worker runs",
    "exact edit commands for small changes": r"(?i)small changes[^.]*exact edit commands[^.]*`commands`",
    "no multi-purpose scripts": r"(?i)never write multi-purpose scripts",
    "one script per unit in .wb/scripts": r"(?i)one script per unit in `?<project>/\.wb/scripts/",
    "shell or uv run with inline deps": r"(?i)shell script[^.]*`uv run`[^.]*inline \(PEP 723\) dependencies",
    "install approval": r"(?i)ask the user before any install or setup that changes the environment",
    "approval lasts the session": r"(?i)approves?[^.]*same kind[^.]*rest of this session without asking",
    "single-line progress": r"(?i)update one terminal line in place",
    "percent elapsed ETA": r"(?i)percent, elapsed time and ETA",
    "unknown total": r"(?i)unknown total[^.]*count or stage[^.]*elapsed",
    "no scrolling lines": r"(?i)never print a new line per step or interval",
}
WORKER_RULES = {
    "no shell": r"(?i)you have no file-editing or shell tools",
    "no files, code or decisions": r"(?i)never write or edit files, code or scripts and never decide on your own",
    "terminal only": r"(?i)run only the Task's commands[^.]*through the Workbench `terminal` tool",
    "ask to split or simplify":
        r"(?i)needs code, a file change or a judgement[^.]*report `blocked` and ask the manager to split or simplify it",
    "response_contract overrides": r"(?i)a `response_contract` stage overrides this",
    "report facts at the end or on an issue": r"(?i)report facts[^.]*at the end or on an actionable issue",
    "load the skill unless response_contract":
        r"(?i)load the to-manager skill before handling a manager message unless it has a `response_contract`",
}


class ManagerRolePromptTests(unittest.TestCase):
    def test_manager_argv_carries_the_plan_first_and_unit_script_rules(self):
        prompt = flat(appended("manager"))
        for name, pattern in MANAGER_RULES.items():
            with self.subTest(rule=name):
                self.assertRegex(prompt, pattern)

    def test_manager_role_prompt_comes_before_the_records_prompt(self):
        prompt = appended("manager")
        self.assertTrue(prompt.endswith(launcher.RECORDS_PROMPT), prompt)
        self.assertRegex(flat(prompt[:-len(launcher.RECORDS_PROMPT)]), MANAGER_RULES["plan first"])

    def test_the_worker_does_not_get_the_manager_rules(self):
        prompt = flat(appended("worker"))
        for name in ("plan checklist file", "install approval", "one script per unit in .wb/scripts"):
            with self.subTest(rule=name):
                self.assertNotRegex(prompt, MANAGER_RULES[name])


class WorkerRolePromptTests(unittest.TestCase):
    def test_worker_argv_carries_the_executor_rules(self):
        prompt = flat(appended("worker"))
        for name, pattern in WORKER_RULES.items():
            with self.subTest(rule=name):
                self.assertRegex(prompt, pattern)

    def test_worker_role_rules_come_before_the_reporting_and_records_prompts(self):
        prompt = appended("worker")
        tail = launcher.WORKER_REPORTING_PROMPT + "\n\n" + launcher.RECORDS_PROMPT
        self.assertTrue(prompt.endswith(tail), prompt)
        self.assertRegex(flat(prompt[:-len(tail)]), WORKER_RULES["no shell"])

    def test_the_manager_does_not_get_the_worker_rules(self):
        prompt = flat(appended("manager"))
        for name in ("no shell", "ask to split or simplify"):
            with self.subTest(rule=name):
                self.assertNotRegex(prompt, WORKER_RULES[name])


class WorkerFileToolTests(unittest.TestCase):
    """MWR-D07 (Q69 b): the worker has no file-editing tools; the manager writes every file and script."""

    def test_worker_argv_tools_have_no_edit_or_write(self):
        argv = omp_command(PLAN, Path("/d/omp-isolation-worker.yml"))
        tools = argv[argv.index("--tools") + 1].split(",")
        self.assertEqual(tools, ["read", "grep", "glob", "web_search", "todo", "task"])

    def test_an_observed_worker_edit_or_write_tool_is_a_leak(self):
        seen = {"context_files": [], "skills": [], "skill_commands": [], "rules": [], "mcp_tools": [],
                "task_agents": ["explorer"], "other_commands": [], "system_prompt_default": True,
                "autoqa": False, "browser": False,
                "tools": ["read", "grep", "glob", "edit", "write", "web_search", "todo", "task", "to_manager",
                          "terminal", "workbench_logs", "workbench_records"]}
        self.assertEqual(launcher.isolation_leaks(seen, allowed_skills=(), role="worker"), ["tool:edit", "tool:write"])
        manager = {**seen, "task_agents": sorted(launcher.BUNDLED_TASK_AGENTS), "tools": ["edit", "write", "bash"]}
        self.assertEqual(launcher.isolation_leaks(manager, allowed_skills=(), role="manager"), [],
                         "the manager keeps its file tools")


class RolePromptLengthTests(unittest.TestCase):
    def test_role_prompts_stay_short(self):
        # MWR-AC-03: the always-on part stays short; details stay in the skills.
        self.assertLessEqual(len(launcher.MANAGER_ROLE_PROMPT), 1200)
        self.assertLessEqual(len(launcher.WORKER_ROLE_PROMPT), 800)
        self.assertLessEqual(len(appended("manager")) - len(launcher.RECORDS_PROMPT), 1200 + 2)
        self.assertLessEqual(len(appended("worker")) - len(launcher.RECORDS_PROMPT)
                             - len(launcher.WORKER_REPORTING_PROMPT), 800 + 4)


if __name__ == "__main__":
    unittest.main()
