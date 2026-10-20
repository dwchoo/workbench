"""Keep canonical bridge sources outside Python; collect only for distribution."""
from pathlib import Path
import shutil

from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        source = Path(__file__).resolve().parents[1]
        target = Path(self.build_lib) / 'workbench' / '_assets'
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source / 'omp_bridge', target / 'omp_bridge', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(source / 'pyproject.toml', target / 'pyproject.toml')
