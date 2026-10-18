"""Small owned fixtures for the standalone report preparation/storage helper."""

from concurrent.futures import ThreadPoolExecutor
import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[3] / "omp_bridge/skills/workbench-report/scripts/report_io.py"
HEADINGS = (
    "문제 요약", "기대/실제 동작", "환경·버전", "발생 순서·재현 정보", "영향", "증거·로그",
    "원인 가설과 근거", "이미 시도한 조치", "미확인·누락 정보", "다음 조사 제안",
)


def draft(evidence="미확인: 로그를 제공받지 못했습니다."):
    return "# WB 문제 보고서\n\n작성 시각: 미확인\nWB revision·OMP 버전: 미확인\n\n" + "".join(
        f"## {heading}\n{evidence if heading == '증거·로그' else '미확인: 관련 자료 없음'}\n\n"
        for heading in HEADINGS
    )


class ReportIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wpr02-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skill = self.root / "skill"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "references").mkdir()
        self.helper = self.skill / "scripts/report_io.py"
        shutil.copyfile(SOURCE, self.helper)
        (self.skill / "references/report-template.md").write_text(draft(), encoding="utf-8")
        self.project = self.root / "한글 project $(touch NEVER)"
        self.project.mkdir(mode=0o755)
        spec = importlib.util.spec_from_file_location("owned_report_io", self.helper)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def cli(self, text=None, output=None, project=None):
        args = [sys.executable, str(self.helper), "--project-dir", str(project or self.project)]
        if output is not None:
            args += ["--output", str(output)]
        result = subprocess.run(args, input=(text or draft()).encode("utf-8") if isinstance(text or draft(), str) else text,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=10, check=False)
        self.assertEqual(result.stderr, b"")
        return result.returncode, json.loads(result.stdout)

    def test_default_portable_artifact_and_permissions(self):
        code, result = self.cli()
        self.assertEqual((code, result['status']), (0, "saved"))
        output = Path(result['path'])
        self.assertEqual(output.parent, self.project / "bug-reports")
        self.assertRegex(output.name, r"^wb-\d{8}T\d{12}Z-[0-9a-f]+\.md$")
        self.assertEqual(output.read_text(), draft())
        self.assertEqual(result['utf8_bytes'], len(draft().encode()))
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.project.stat().st_mode), 0o755)
        moved = self.root / "portable.md"
        shutil.move(output, moved)
        self.assertIn("## 증거·로그", moved.read_text())
        self.assertFalse((self.project / "NEVER").exists())

    def test_collision_symlink_and_concurrent_creates(self):
        existing = self.project / "보고서.md"
        existing.write_text("original")
        code, result = self.cli(output=existing)
        self.assertEqual(code, 0)
        self.assertNotEqual(result['path'], str(existing))
        self.assertEqual(existing.read_text(), "original")
        link = self.project / "link.md"
        link.symlink_to(existing)
        code, result = self.cli(output=link)
        self.assertEqual(code, 0)
        self.assertTrue(link.is_symlink())
        self.assertEqual(existing.read_text(), "original")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.cli(output="parallel.md"), range(4)))
        self.assertTrue(all(code == 0 for code, _ in results))
        self.assertEqual(len({result['path'] for _, result in results}), 4)

    def test_directory_output_nested_paths_and_no_symlink_parent(self):
        destination = self.project / "existing directory"
        destination.mkdir(mode=0o755)
        code, result = self.cli(output=destination)
        self.assertEqual(code, 0)
        self.assertEqual(Path(result['path']).parent, destination)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o755)
        code, result = self.cli(output="new/sub/보고서.md")
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE((self.project / "new/sub").stat().st_mode), 0o700)
        (self.project / "alias").symlink_to(destination, target_is_directory=True)
        code, result = self.cli(output="alias/never.md")
        self.assertEqual((code, result['status']), (1, "not_saved"))
        self.assertFalse((destination / "never.md").exists())
        self.assertEqual(result['markdown'], draft())

    def test_invalid_headings_sections_fences_and_size(self):
        cases = [
            draft().replace("## 영향", "## UNKNOWN"),
            draft().replace("## 영향", "## 영향\n미확인\n## 영향"),
            draft().replace("## 영향\n미확인: 관련 자료 없음", "## 영향\n"),
            draft().replace("## 영향", "## 영향\n```\nunterminated"),
            draft("````text\n" + "가" * 1400 + "\n````"),
            draft().replace("# WB 문제 보고서", "# " + "가" * 11000),
        ]
        for text in cases:
            with self.subTest(size=len(text)):
                code, result = self.cli(text)
                self.assertEqual((code, result['status']), (2, "invalid_draft"))
                self.assertEqual(result['markdown'], text)
                self.assertFalse((self.project / "bug-reports").exists())
        code, result = self.cli(draft("````log\n## not a section\n```\n````\n누락: E-01 앞부분 미제공"))
        self.assertEqual(code, 0)
        self.assertIn("누락: E-01 앞부분 미제공", Path(result['path']).read_text())

    def test_exact_utf8_byte_boundaries_and_excerpt_total(self):
        # The fence newline is part of the raw excerpt byte count.
        exact_excerpt = "가" * 1365 + "\n"
        self.assertEqual(len(exact_excerpt.encode()), 4096)
        code, result = self.cli(draft("```log\n" + exact_excerpt + "```"))
        self.assertEqual(code, 0)
        code, result = self.cli(draft("```log\n" + exact_excerpt + "x\n```"))
        self.assertEqual(code, 2)
        four = "\n".join("```log\n" + exact_excerpt + "```" for _ in range(4))
        code, result = self.cli(draft(four))
        self.assertEqual(code, 0)
        code, result = self.cli(draft(four + "\n```log\nx\n```"))
        self.assertEqual(code, 2)
        text = draft()
        exact_report = text + "x" * (32768 - len(text.encode()))
        code, result = self.cli(exact_report)
        self.assertEqual(code, 0)
        self.assertEqual(result['utf8_bytes'], 32768)
        code, result = self.cli(exact_report + "가")
        self.assertEqual(code, 2)
        self.assertEqual(result['markdown'], exact_report + "가")

    def test_utf8_and_hard_input_limit(self):
        code, result = self.cli(b"token=UTF8_CANARY\xff")
        self.assertEqual(code, 2)
        self.assertNotIn("UTF8_CANARY", json.dumps(result))
        code, result = self.cli(b"x" * (1024 * 1024 + 1))
        self.assertEqual(code, 2)
        self.assertNotIn("markdown", result)
        self.assertFalse((self.project / "bug-reports").exists())

    def test_canonical_nested_fences_limits_and_shielding(self):
        canonical = (SOURCE.parent.parent / "references/report-template.md").read_text()
        code, result = self.cli(canonical)
        self.assertEqual(code, 0)
        for size, count in ((5000, 1), (3500, 5)):
            blocks = "\n".join("    ```text\n    " + "x" * size + "\n    ```" for _ in range(count))
            text = canonical.replace("    ```text\n    미확인\n    ```", blocks)
            code, result = self.cli(text)
            self.assertEqual((code, result['status']), (2, "invalid_draft"))
            self.assertEqual(result['markdown'], text)
        # Container indentation is Markdown structure, not raw excerpt data.
        exact = "    " + "가" * 1365 + "\n"
        text = canonical.replace("    미확인\n", exact)
        code, result = self.cli(text)
        self.assertEqual(code, 0)
        text = canonical.replace("    ```text\n    미확인\n    ```", (
            "    ````text\n    ## 영향\n    ```\n    ~~~~\n    "
            "## 다음 조사 제안\n    ````"
        ))
        code, result = self.cli(text)
        self.assertEqual(code, 0)
        self.assertIn("    ## 영향", Path(result['path']).read_text())
        code, result = self.cli(canonical.replace("    ```\n\n## 원인", "    ~~\n\n## 원인"))
        self.assertEqual(code, 2)

    def test_name_max_collisions_preserve_ascii_utf8_files_and_symlinks(self):
        limit = os.pathconf(self.project, "PC_NAME_MAX")
        for stem_char in ("a", "가"):
            with self.subTest(stem_char=stem_char):
                budget = limit - len(".md".encode())
                char_bytes = len(stem_char.encode())
                stem = stem_char * (budget // char_bytes) + "a" * (budget % char_bytes)
                name = stem + ".md"
                self.assertEqual(len(name.encode()), limit)
                original = self.project / name
                original.write_text("preserved")
                code, result = self.cli(output=original)
                self.assertEqual(code, 0)
                sibling = Path(result['path'])
                self.assertEqual(sibling.parent, original.parent)
                self.assertNotEqual(sibling, original)
                self.assertLessEqual(len(sibling.name.encode()), limit)
                self.assertEqual(sibling.read_text(), draft())
                self.assertEqual(original.read_text(), "preserved")
                original.unlink()
                original.symlink_to(sibling)
                code, result = self.cli(output=original)
                self.assertEqual(code, 0)
                self.assertTrue(original.is_symlink())
                self.assertNotEqual(Path(result['path']), sibling)
                self.assertEqual(sibling.read_text(), draft())

    def test_supported_secrets_saved_errors_and_identical_fallback(self):
        secrets = ["ASSIGN_CANARY", "BEARER_CANARY", "URL_CANARY", "PRIVATE_CANARY", "sk-" + "a" * 30]
        evidence = '\n'.join([
            'api_key="ASSIGN_CANARY" token: "TOKEN_CANARY" password=PASSWORD_CANARY',
            'OPENAI_API_KEY=ENV_KEY_CANARY GITHUB_TOKEN=ENV_TOKEN_CANARY DB_PASSWORD=ENV_PASS_CANARY',
            'Authorization: Bearer BEARER_CANARY', 'source=https://user:URL_CANARY@example.test/path',
            '-----BEGIN RSA PRIVATE KEY-----\nPRIVATE_CANARY\n-----END RSA PRIVATE KEY-----', secrets[-1],
        ])
        text = draft(evidence)
        code, saved = self.cli(text)
        self.assertEqual(code, 0)
        prepared = Path(saved['path']).read_text()
        blocker = self.project / "token=PATH_CANARY"
        blocker.write_text("keep")
        code, failure = self.cli(text, output=blocker / "out.md")
        self.assertEqual((code, failure['status']), (1, "not_saved"))
        self.assertEqual(failure['markdown'], prepared)
        combined = prepared + json.dumps(failure) + json.dumps(saved)
        for secret in secrets + ["TOKEN_CANARY", "PASSWORD_CANARY", "PATH_CANARY", "ENV_KEY_CANARY", "ENV_TOKEN_CANARY", "ENV_PASS_CANARY"]:
            self.assertNotIn(secret, combined)
        self.assertIn("가림:", prepared)
        self.assertIn("[REDACTED]", prepared)
        self.assertEqual(blocker.read_text(), "keep")
        self.assertFalse(any(path.suffix == ".tmp" for path in self.project.rglob("*")))
        # A quote in a filename must not escape the assignment syntax before
        # error redaction (OSError normally repr-escapes its filename).
        quoted = self.project / 'prefix"-token=\'QUOTED_PATH_CANARY\''
        quoted.write_text("keep")
        code, result = self.cli(text, output=quoted / "out.md")
        self.assertEqual(code, 1)
        self.assertNotIn("QUOTED_PATH_CANARY", json.dumps(result))

    def test_partial_write_enospc_and_close_failures_clean_only_owned(self):
        prepared, _, reason = self.module.prepare(draft("password=FAULT_CANARY"))
        self.assertIsNone(reason)
        preserved = self.project / "out.md"
        preserved.write_text("preserved")
        real_write = os.write
        calls = 0

        def fail_write(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:13])
            raise OSError(errno.ENOSPC, "password=FAULT_CANARY")

        with patch.object(self.module.os, "write", fail_write):
            with self.assertRaises(OSError):
                self.module.save(prepared, str(self.project), "out.md")
        self.assertEqual(list(self.project.iterdir()), [preserved])
        self.assertEqual(preserved.read_text(), "preserved")
        real_close = os.close

        def fail_file_close(fd):
            is_file = stat.S_ISREG(os.fstat(fd).st_mode)
            real_close(fd)
            if is_file:
                raise OSError(errno.EIO, "owned close failed")

        with patch.object(self.module.os, "close", fail_file_close):
            with self.assertRaises(OSError):
                self.module.save(prepared, str(self.project), "close.md")
        self.assertEqual(list(self.project.iterdir()), [preserved])

    def test_permission_failure_and_full_fallback_through_main(self):
        class Input:
            buffer = None
        import io
        stdin = Input()
        stdin.buffer = io.BytesIO(draft("password=DENIED_CANARY").encode())
        stdout = io.StringIO()
        prepared, _, _ = self.module.prepare(draft("password=DENIED_CANARY"))
        with patch.object(self.module.sys, "stdin", stdin), patch.object(self.module.sys, "stdout", stdout), \
                patch.object(self.module, "save", side_effect=PermissionError("password=DENIED_CANARY")):
            code = self.module.main(["--project-dir", str(self.project)])
        result = json.loads(stdout.getvalue())
        self.assertEqual((code, result['status']), (1, "not_saved"))
        self.assertEqual(result['markdown'], prepared)
        self.assertNotIn("DENIED_CANARY", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
