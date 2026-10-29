"""manager-worker-roles MWR-AC-05/06: the role skills carry the plan-first, unit-script and executor rules.

Seam: the installed skill files (entry plus the references it links), read as data. No OMP, no model.
"""
import inspect
from pathlib import Path
import re
import unittest

from workbench.backend import flow_tasks, launcher

SKILLS = launcher.default_skills_dir()


def entry(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def body(name: str) -> str:
    """The entry plus every reference it links (the text a role reads for an action)."""
    path = SKILLS / name / "SKILL.md"
    text = entry(name)
    parts, seen = [text], set()
    for target in re.findall(r"\[[^\]]+\]\(([^)#]+\.md)(?:#[^)]*)?\)", text):
        reference = (path.parent / target).resolve()
        if reference not in seen:
            seen.add(reference)
            parts.append(reference.read_text(encoding="utf-8"))
    return " ".join("\n\n".join(parts).split())


def scripts() -> str:
    return " ".join((SKILLS / "to-worker" / "references" / "task-scripts.md").read_text(encoding="utf-8").split())


class ManagerSkillPlanTests(unittest.TestCase):
    def test_to_worker_says_plan_first_with_todo_and_a_plan_file(self):
        text = " ".join(entry("to-worker").split())
        self.assertRegex(text, r"(?i)plan first")
        self.assertRegex(text, r"(?i)units?[^.]*one simple command or one short script")
        self.assertRegex(text, r"`todo`")
        self.assertRegex(text, r"`<project>/\.wb/plans/<YYYYMMDD>-<slug>\.md`")
        self.assertRegex(text, r"(?i)checkbox")
        self.assertRegex(text, r"(?i)you write (each|the) unit's script")


class TaskScriptRuleTests(unittest.TestCase):
    def test_one_script_per_unit_shell_or_uv_with_inline_dependencies(self):
        text = scripts()
        self.assertRegex(text, r"(?i)no multi-purpose script")
        self.assertRegex(text, r"(?i)one script per unit")
        self.assertRegex(text, r"`<project-root>/\.wb/scripts/`")
        self.assertRegex(text, r"(?i)`uv run`")
        self.assertRegex(text, r"(?i)PEP 723")
        self.assertRegex(text, r"# /// script")

    def test_install_needs_approval_and_the_approval_lasts_the_session(self):
        text = scripts()
        self.assertRegex(text, r"(?i)install or setup that changes the environment[^.]*approv")
        self.assertRegex(text, r"(?i)same kind[^.]*rest of (this|that|the) (manager )?session")

    def test_single_line_progress_with_percent_elapsed_and_eta(self):
        text = scripts()
        self.assertRegex(text, r"(?i)one line[^.]*percent[^.]*elapsed[^.]*ETA")
        self.assertRegex(text, r"(?i)unknown total[^.]*(count|stage)[^.]*elapsed")
        self.assertRegex(text, r"(?i)never print a new line per step or interval")
        self.assertRegex(text, r"(?i)never invented percentages")


class WorkerSkillExecutorTests(unittest.TestCase):
    def test_to_manager_says_ask_the_manager_to_split_or_simplify(self):
        text = body("to-manager")
        self.assertRegex(text, r"(?i)never (write|create) or edit (code|task code)")
        self.assertRegex(text, r"(?i)complex[^.]*(code|judgement)[^.]*`blocked`[^.]*`reason`[^.]*split or simplify")

    def test_to_manager_no_longer_invites_the_worker_to_edit_files(self):
        text = body("to-manager")
        self.assertNotRegex(text, r"(?i)own tools \(read, edit")
        self.assertNotRegex(text, r"(?i)before creating or editing task code")


def bridge() -> str:
    source = Path(launcher.default_bridge_extension()).with_name("bridge.ts").read_text(encoding="utf-8")
    return " ".join(re.sub(r'"\s*\+\s*"', "", source).split())


class ContractWordingTests(unittest.TestCase):
    """MWR-D07 (Q69 b): the manager creates and edits files and scripts; the worker only runs the Task's commands."""

    def test_to_worker_tool_text_says_the_manager_writes_and_the_worker_runs(self):
        text = bridge()
        self.assertNotIn("the worker does the work itself", text)
        self.assertNotRegex(text, r"(?i)paths the worker may change")
        self.assertRegex(text, r"(?i)work: the worker runs the Task's commands; you create and edit the files and "
                               r"scripts \(small changes: exact edit commands such as `sed -i` in commands\)")
        self.assertRegex(text, r"(?i)paths the Task's commands may change")

    def test_task_fields_say_the_manager_writes_and_the_worker_runs(self):
        text = " ".join((SKILLS / "to-worker" / "references" / "task-fields.md").read_text(encoding="utf-8").split())
        self.assertNotRegex(text, r"(?i)with its own tools|code changes\)|paths the worker may change")
        self.assertRegex(text, r"(?i)`work`[^.]*the worker runs your `commands`[^.]*you create and edit the files "
                               r"and scripts")
        self.assertRegex(text, r"(?i)exact edit commands such as `sed -i`")
        self.assertRegex(text, r"(?i)the only paths the Task's commands may change")

    def test_worker_report_fields_do_not_ask_what_it_changed(self):
        text = body("to-manager")
        self.assertNotRegex(text, r"(?i)what you changed")
        self.assertLessEqual(len((SKILLS / "to-manager" / "references" / "reports.md").read_text(encoding="utf-8")), 5000)

    def test_paths_validation_names_the_task_not_the_worker(self):
        self.assertNotIn("the worker may change", Path(inspect.getsourcefile(flow_tasks)).read_text(encoding="utf-8"))

    def test_the_manager_rule_leaves_file_and_script_writing_to_the_manager(self):
        for where, text in (("MANAGER_RULE", flow_tasks.MANAGER_RULE), ("bridge.ts", bridge()),
                            ("to-worker", " ".join(entry("to-worker").split()))):
            with self.subTest(where=where):
                self.assertNotRegex(text, r"(?i)edits or checks")
                self.assertRegex(text, r"(?i)do not do it yourself \(do not run its commands or checks; you write "
                                       r"its files and scripts beforehand\)")

    def test_to_worker_entry_keeps_without_approval(self):
        self.assertRegex(" ".join(entry("to-worker").split()), r"`to_worker` creates and immediately delivers a Task "
                                                                 r"without approval")

    def test_inline_dependencies_are_described_as_uv_cache_not_the_project_environment(self):
        text = scripts()
        self.assertRegex(text, r"(?i)inline dependencies stay in uv's cache, not the project environment")
        self.assertNotRegex(text, r"(?i)do not change the environment")


if __name__ == "__main__":
    unittest.main()
