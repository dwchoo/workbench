"""Public CLI packaging checks; these are not model skill-behavior evidence."""
import shutil
from pathlib import Path
import unittest

from tests.backend.workbench_report.test_report_io_independent import CliFixture, HELPER, REFERENCE


class IndependentReferenceContract(CliFixture):
    def test_actual_shipped_reference_is_accepted_by_actual_shipped_helper(self):
        reference = REFERENCE.read_text(encoding="utf-8")
        code, result, _ = self.cli(reference)
        self.assertEqual((code, result["status"]), (0, "saved"))
        self.assertEqual(Path(result["path"]).read_text(encoding="utf-8"), reference)

    def test_colocated_reference_is_validation_authority_not_competing_headings(self):
        skill = self.root / "installed-skill"
        (skill / "scripts").mkdir(parents=True)
        (skill / "references").mkdir()
        helper = skill / "scripts/report_io.py"
        shutil.copyfile(HELPER, helper)
        original = REFERENCE.read_text(encoding="utf-8")
        # Deliberate packaging fixture, not a product heading change. A helper
        # with a second hard-coded template would disagree with this reference.
        revised = original.replace("## 영향\n", "## Fixture impact heading\n")
        (skill / "references/report-template.md").write_text(revised, encoding="utf-8")
        code, result, _ = self.cli(revised, helper=helper)
        self.assertEqual((code, result["status"]), (0, "saved"))
        code, result, _ = self.cli(original, helper=helper, output="old-template.md")
        self.assertEqual((code, result["status"]), (2, "invalid_draft"))
        self.assertFalse((self.project / "old-template.md").exists())


if __name__ == "__main__":
    unittest.main()
