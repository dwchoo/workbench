"""WBX-B2 static checks of the user skill files (seam S5): layout, README first line, placeholder, stdlib-only imports.

Documents are the TDD exception (no RED); expected values are the web-board-ux SPEC "`wb-inbox` skill" repo layout and
BRIEF WBX-D08/D09/D23. Run with ``discover -s tests/hub -p 'test_wb_inbox*.py'``.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / 'skills'
SKILL = SKILLS / 'wb-inbox'
FILES = (SKILLS / 'README.md', SKILL / 'SKILL.md', SKILL / 'wb_inbox.py', SKILL / '.env.example')
DOCS = (SKILLS / 'README.md', SKILL / 'SKILL.md', SKILL / '.env.example')
PLACEHOLDER = 'http://YOUR-HUB-HOST:8780'
FIRST_LINE = ('This folder holds Workbench user skills for agents outside Workbench; it is not `.agents/skills/` '
              '(Workbench development skills).')


class SkillFilesTests(unittest.TestCase):
    def test_the_four_files_exist_and_nothing_else_ships(self):
        for path in FILES:
            self.assertTrue(path.is_file(), path)
        self.assertEqual(sorted(p.name for p in SKILL.iterdir() if p.name != '__pycache__'),
                         ['.env.example', 'SKILL.md', 'wb_inbox.py'])
        self.assertFalse((SKILL / '.env').exists())

    def test_the_readme_first_line_and_marker(self):
        lines = (SKILLS / 'README.md').read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines[0], FIRST_LINE)
        self.assertEqual(lines.count('<!-- wb:skill-repo -->'), 1)

    def test_the_hub_address_placeholder_is_one_exact_string_in_the_three_documents(self):
        for path in DOCS:
            text = path.read_text(encoding='utf-8')
            self.assertIn(PLACEHOLDER, text, path)
            for found in re.findall(r'https?://YOUR[A-Za-z0-9.-]*(?::[0-9]+)?', text):
                self.assertEqual(found, PLACEHOLDER, path)
        self.assertNotIn('YOUR-HUB-HOST', (SKILL / 'wb_inbox.py').read_text(encoding='utf-8'))

    def test_documents_and_comments_are_english(self):
        for path in FILES:
            self.assertIsNone(re.search('[ᄀ-ᇿ㄰-㆏가-힣]', path.read_text(encoding='utf-8')),
                              path)

    def test_the_skill_frontmatter(self):
        text = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
        match = re.match(r'---\nname: wb-inbox\ndescription: (.+)\n---\n', text)
        self.assertIsNotNone(match)
        self.assertGreater(len(match.group(1)), 40)

    def test_the_readme_has_the_install_env_curl_and_tls_guidance(self):
        text = (SKILLS / 'README.md').read_text(encoding='utf-8')
        for needle in ('chmod 600', 'curl -H "Authorization: Bearer $WB_API_KEY" "$WB_HUB_URL/api/v1/projects"',
                       'SSL_CERT_FILE', '/skill/wb-inbox/', '~/.config/omp-workbench/client.env', '.env', 'pip'):
            self.assertIn(needle, text)

    def test_only_the_two_settings_exist_and_no_real_key_is_shipped(self):
        names = set()
        for path in FILES:
            text = path.read_text(encoding='utf-8')
            names |= set(re.findall(r'\bWB_[A-Z_]+\b', text))
            self.assertIsNone(re.search(r'wbk_[A-Za-z0-9]{16,}', text), path)
        self.assertEqual(names, {'WB_HUB_URL', 'WB_API_KEY'})

    def test_the_example_env_has_exactly_the_two_assignments(self):
        lines = [line for line in (SKILL / '.env.example').read_text(encoding='utf-8').splitlines()
                 if line.strip() and not line.startswith('#')]
        self.assertEqual([line.split('=')[0] for line in lines], ['WB_HUB_URL', 'WB_API_KEY'])

    def test_gitignore_hides_local_env_files(self):
        self.assertIn('skills/**/.env', (REPO / '.gitignore').read_text(encoding='utf-8').splitlines())
        ignored = subprocess.run(['git', 'check-ignore', '-q', 'skills/wb-inbox/.env'], cwd=REPO)
        self.assertEqual(ignored.returncode, 0)
        tracked = subprocess.run(['git', 'check-ignore', '-q', 'skills/wb-inbox/.env.example'], cwd=REPO)
        self.assertEqual(tracked.returncode, 1)


class StdlibOnlyTests(unittest.TestCase):
    def test_every_import_is_standard_library(self):
        tree = ast.parse((SKILL / 'wb_inbox.py').read_text(encoding='utf-8'))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {alias.name.split('.')[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                modules.add(node.module.split('.')[0])
        import sys
        self.assertTrue(modules)
        self.assertLessEqual(modules, set(sys.stdlib_module_names))

    def test_it_runs_isolated_without_the_project_environment(self):
        python = shutil.which('python3')
        with tempfile.TemporaryDirectory(prefix='wb-iso-') as home:
            completed = subprocess.run([python, '-I', str(SKILL / 'wb_inbox.py'), '--help'], cwd=home, env={
                'HOME': home, 'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for command in ('projects', 'submit', 'list', 'get', 'comment', 'update', 'set-state', 'cancel', 'result',
                        'config'):
            self.assertIn(command, completed.stdout)


if __name__ == '__main__':
    unittest.main()
