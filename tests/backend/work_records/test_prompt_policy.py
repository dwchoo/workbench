"""Short shared policy is packaging guidance, verified without a fabricated RED."""
import unittest
from pathlib import Path
from workbench.backend.launcher import LaunchPlan,omp_command
class RecordsPromptPolicyTests(unittest.TestCase):
    def test_both_roles_always_receive_record_and_document_boundaries(self):
        for role in ("manager","worker"):
            argv=omp_command(LaunchPlan(None,"omp","omp/test","bridge.ts"),Path(f"omp-isolation-{role}.yml"))
            prompt=argv[argv.index("--append-system-prompt")+1]
            for phrase in ("workbench_records","SQLite","BRIEF","SPEC","ADR","workbench-report","explicit","approval","output path"):
                self.assertIn(phrase,prompt)
            if role=="worker":self.assertIn("Do not send routine progress",prompt)
