"""Requirement-derived public CLI checks; synthetic data, no collection/model.

These observe file bytes, CLI results and filesystem permissions. They do not
prove that an agent collects appropriate evidence or writes a sound diagnosis.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "omp_bridge/skills/workbench-report/scripts/report_io.py"
REFERENCE = ROOT / "omp_bridge/skills/workbench-report/references/report-template.md"
HEADINGS = (
    "문제 요약", "기대/실제 동작", "환경·버전", "발생 순서·재현 정보", "영향", "증거·로그",
    "원인 가설과 근거", "이미 시도한 조치", "미확인·누락 정보", "다음 조사 제안",
)


def report(evidence="미확인: 해당 자료를 받지 못했습니다."):
    return "# WB 문제 보고서\n\n작성 시각: 미확인\nWB revision·OMP version: 미확인\n\n" + "".join(
        f"## {heading}\n{evidence if heading == '증거·로그' else '미확인: 관련 자료 없음'}\n\n"
        for heading in HEADINGS
    )


class CliFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wpr-u2-independent-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "프로젝트"
        self.project.mkdir(mode=0o755)
        self.home = self.root / "owned-home"
        self.home.mkdir(mode=0o700)

    def cli(self, markdown, *, output=None, helper=HELPER, umask=-1):
        arguments = [sys.executable, str(helper), "--project-dir", str(self.project)]
        if output is not None:
            arguments += ["--output", str(output)]
        completed = subprocess.run(arguments, input=markdown.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.root, timeout=5,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home),
                 "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}, umask=umask, check=False)
        self.assertEqual(completed.stderr, b"", completed.stderr.decode("utf-8", "replace"))
        # Exactly one JSON object, with no diagnostic/raw draft preceding it.
        result = json.loads(completed.stdout)
        self.assertIsInstance(result, dict)
        return completed.returncode, result, completed.stdout


class IndependentReportIO(CliFixture):
    def nested_reference(self, excerpts):
        reference = REFERENCE.read_text(encoding="utf-8")
        placeholder = "    ```text\n    미확인\n    ```"
        self.assertEqual(reference.count(placeholder), 1,
                         "fixture must use the actual reference's nested-list excerpt")
        replacement = "\n".join("    ```text\n    " + excerpt + "\n    ```" for excerpt in excerpts)
        return reference.replace(placeholder, replacement)

    def test_normal_nested_reference_excerpt_is_valid(self):
        markdown = self.nested_reference(["synthetic nested log line"])
        code, result, _ = self.cli(markdown)
        self.assertEqual((code, result["status"]), (0, "saved"))
        self.assertEqual(Path(result["path"]).read_text(encoding="utf-8"), markdown)

    def test_nested_reference_excerpt_over_4k_is_rejected_without_clipping(self):
        markdown = self.nested_reference(["x" * 5000])
        self.assertLess(len(markdown.encode("utf-8")), 32768)
        code, result, _ = self.cli(markdown)
        self.assertEqual((code, result["status"]), (2, "invalid_draft"), result)
        self.assertEqual(result["markdown"], markdown)
        self.assertFalse((self.project / "bug-reports").exists())

    def test_nested_reference_aggregate_over_16k_is_rejected_without_clipping(self):
        excerpts = ["x" * 3500 for _ in range(5)]
        self.assertTrue(all(len(excerpt.encode("utf-8")) < 4096 for excerpt in excerpts))
        self.assertGreater(sum(len(excerpt.encode("utf-8")) for excerpt in excerpts), 16384)
        markdown = self.nested_reference(excerpts)
        self.assertLess(len(markdown.encode("utf-8")), 32768)
        code, result, _ = self.cli(markdown)
        self.assertEqual((code, result["status"]), (2, "invalid_draft"), result)
        self.assertEqual(result["markdown"], markdown)
        self.assertFalse((self.project / "bug-reports").exists())

    def test_name_max_collision_creates_valid_sibling_for_ascii_and_utf8(self):
        name_max = os.pathconf(self.project, "PC_NAME_MAX")
        suffix = ".md"
        stem_bytes = name_max - len(suffix)
        names = {
            "ascii": "x" * stem_bytes + suffix,
            "utf8": "가" * (stem_bytes // 3) + "x" * (stem_bytes % 3) + suffix,
        }
        markdown = report("E-01: synthetic UTF-8 evidence 한글 원문")
        for label, name in names.items():
            with self.subTest(label=label, name_bytes=len(name.encode("utf-8"))):
                self.assertEqual(len(name.encode("utf-8")), name_max)
                original = self.project / name
                original.write_text("preserved original", encoding="utf-8")
                code, result, _ = self.cli(markdown, output=original)
                self.assertEqual(original.read_text(encoding="utf-8"), "preserved original")
                self.assertEqual((code, result["status"]), (0, "saved"), result)
                sibling = Path(result["path"])
                self.assertEqual(sibling.parent, original.parent)
                self.assertNotEqual(sibling, original)
                self.assertLessEqual(len(sibling.name.encode("utf-8")), name_max)
                self.assertEqual(sibling.read_text(encoding="utf-8"), markdown)

    def test_saved_report_is_readable_after_its_source_and_project_are_removed(self):
        source = self.project / "old-backend.log"
        source.write_text("synthetic resize failure\n", encoding="utf-8")
        evidence = (
            f"E-01; 출처: {source}; 수집 시각: 2026-10-18T00:00:00Z; 원자료 시각: 미확인; "
            "줄: 1; Task/run/command ID: 미확인\n"
            "````text\nsynthetic resize failure\n```literal payload\n````\n"
            "누락: E-01의 이전 구간은 제공되지 않았습니다."
        )
        markdown = report(evidence).replace("## 원인 가설과 근거\n미확인: 관련 자료 없음",
            "## 원인 가설과 근거\n가설: resize와 관련될 수 있음(E-01). 원인은 미확정, 대안 설명은 미확인.")
        code, result, _ = self.cli(markdown)
        self.assertEqual((code, result["status"]), (0, "saved"))
        portable = self.root / "transferred.md"
        shutil.copyfile(result["path"], portable)
        shutil.rmtree(self.project)
        text = portable.read_text(encoding="utf-8")
        self.assertEqual(text, markdown)
        for heading in HEADINGS:
            self.assertEqual(text.count("## " + heading + "\n"), 1)
        self.assertIn("synthetic resize failure", text)
        self.assertIn("E-01", text)
        self.assertIn("```literal payload", text)
        self.assertIn("이전 구간은 제공되지 않았습니다", text)

    def test_redaction_is_stable_when_saved_content_is_resubmitted(self):
        fake = "FAKE_U2_NEVER_A_REAL_TOKEN_4c37"
        markdown = report(f'출처: log token="{fake}"\n```text\nAuthorization: Bearer {fake}\n```')
        code, first, first_stdout = self.cli(markdown)
        self.assertEqual(code, 0)
        prepared = Path(first["path"]).read_text(encoding="utf-8")
        code, second, second_stdout = self.cli(prepared)
        self.assertEqual(code, 0)
        self.assertEqual(Path(second["path"]).read_text(encoding="utf-8"), prepared)
        self.assertEqual(second["redaction_count"], 0)
        self.assertNotIn(fake, prepared + first_stdout.decode() + second_stdout.decode())
        self.assertIn("[REDACTED]", prepared)
        self.assertIn("가림:", prepared)

    def test_redaction_disclosure_counts_toward_final_utf8_size(self):
        fake = "FAKE_U2_SIZE_SECRET_1d9e"
        markdown = report(f"token={fake}")
        tail = "END_OF_UNTRUNCATED_DRAFT"
        markdown += "x" * (32760 - len(markdown.encode("utf-8")) - len(tail)) + tail
        self.assertEqual(len(markdown.encode("utf-8")), 32760)
        code, result, stdout = self.cli(markdown)
        self.assertEqual((code, result["status"]), (2, "invalid_draft"))
        self.assertIn("32 KiB", result["reason"])
        self.assertTrue(result["markdown"].endswith(tail))
        self.assertNotIn(fake, stdout.decode("utf-8"))
        self.assertFalse((self.project / "bug-reports").exists())

    def test_private_modes_even_when_child_umask_allows_public_access(self):
        output = self.project / "new" / "nested" / "report.md"
        code, result, _ = self.cli(report(), output=output, umask=0)
        self.assertEqual((code, result["status"]), (0, "saved"))
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(output.parent.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.project.stat().st_mode), 0o755)

    @unittest.skipIf(os.geteuid() == 0, "requires real unprivileged filesystem permission denial")
    def test_real_permission_denial_returns_complete_same_redacted_report(self):
        fake = "FAKE_U2_PERMISSION_SECRET_6bf2"
        markdown = report(f"password={fake}")
        code, saved, _ = self.cli(markdown)
        self.assertEqual(code, 0)
        expected = Path(saved["path"]).read_text(encoding="utf-8")
        blocked = self.project / "read-only"
        blocked.mkdir(mode=0o500)
        self.addCleanup(blocked.chmod, 0o700)
        code, failure, stdout = self.cli(markdown, output=blocked / "must-not-exist.md")
        self.assertEqual((code, failure["status"]), (1, "not_saved"))
        self.assertTrue(failure["reason"])
        self.assertEqual(failure["markdown"], expected)
        self.assertNotIn(fake, stdout.decode("utf-8"))
        self.assertEqual(list(blocked.iterdir()), [])
        self.assertEqual(stat.S_IMODE(blocked.stat().st_mode), 0o500)


if __name__ == "__main__":
    unittest.main()
