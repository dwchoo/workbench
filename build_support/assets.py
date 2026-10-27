"""Keep canonical bridge sources outside Python; collect only for distribution."""
from pathlib import Path
import shutil

from setuptools.command.build_py import build_py


SKILL_FILES = ('README.md', 'wb-inbox/SKILL.md', 'wb-inbox/wb_inbox.py', 'wb-inbox/.env.example')


class BuildPy(build_py):
    def run(self):
        super().run()
        source = Path(__file__).resolve().parents[1]
        target = Path(self.build_lib) / 'workbench' / '_assets'
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source / 'omp_bridge', target / 'omp_bridge', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        # WBX-B3: the user skills the Hub serves under /skill/: exactly these files (an allowlist, so a developer's
        # `.env`, `.env.local` or any stray file never ships). A source tree without them has nothing to serve.
        for name in SKILL_FILES:
            if (source / 'skills' / name).is_file():
                (target / 'skills' / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / 'skills' / name, target / 'skills' / name)
        shutil.copy2(source / 'pyproject.toml', target / 'pyproject.toml')
