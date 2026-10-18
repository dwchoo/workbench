"""Static shipped-asset checks; these do not measure model skill behavior."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "omp_bridge/skills/workbench-report/SKILL.md"
REFERENCE = ROOT / "omp_bridge/skills/workbench-report/references/report-template.md"
EXPECTED_HEADINGS = [
    "문제 요약",
    "기대/실제 동작",
    "환경·버전",
    "발생 순서·재현 정보",
    "영향",
    "증거·로그",
    "원인 가설과 근거",
    "이미 시도한 조치",
    "미확인·누락 정보",
    "다음 조사 제안",
]


class WorkbenchReportAssetContractTests(unittest.TestCase):
    def test_skill_has_measured_manager_skill_metadata_and_reference_path(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        metadata = text.split("---\n", 2)[1]
        self.assertEqual(
            {line.split(":", 1)[0] for line in metadata.splitlines() if ":" in line},
            {"name", "description", "disableModelInvocation"},
        )
        self.assertRegex(metadata, r"(?m)^name: workbench-report$")
        self.assertRegex(metadata, r"(?m)^description: .+\S$")
        self.assertRegex(metadata, r"(?m)^disableModelInvocation: true$")
        self.assertIn("references/report-template.md", text)

    def test_helper_contract_uses_bounded_cli_and_non_expanding_stdin(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("scripts/report_io.py", text)
        self.assertIn("--project-dir", text)
        self.assertIn("standard input", text)
        self.assertIn("pty: false", text)
        self.assertIn("<<'WBR_REPORT_UNIQUE_7F3C9D2A'", text)
        self.assertIn("does not occur as a complete line", text)
        self.assertIn("32 KiB", text)
        for result in ("`saved`", "`not_saved`", "`invalid_draft`"):
            self.assertIn(result, text)

    def test_reference_has_only_the_canonical_ten_headings_in_order(self):
        text = REFERENCE.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# WB 문제 보고서\n"))
        self.assertIn("- 작성 시각 (UTC): 미확인", text)
        self.assertIn("- WB revision: 미확인", text)
        self.assertIn("- OMP version: 미확인", text)
        headings = re.findall(r"(?m)^## (.+?)\s*$", text)
        self.assertEqual(headings, EXPECTED_HEADINGS)
        sections = re.split(r"(?m)^## .+\s*$", text)[1:]
        self.assertEqual(len(sections), len(EXPECTED_HEADINGS))
        self.assertTrue(all(section.strip() for section in sections))


if __name__ == "__main__":
    unittest.main()
