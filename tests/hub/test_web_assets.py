"""TI-01 / T9 static checks: pinned runtime deps, vendored xterm.js assets, wheel contents.

Expected values come from the ticket (D-29 pin list, xterm file names) and plain hashlib/zipfile
reads, not from product code. The wheel test builds with ``uv build`` into a temp dir (no network
beyond what the build already needs). Run with ``discover -s tests/hub -p 'test_web_assets*.py'``.
"""
from __future__ import annotations

from hashlib import sha256
from importlib import metadata
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[2]
VENDOR = REPO / 'src/workbench/hub/web/static/vendor/xterm'
WHEEL_VENDOR = 'workbench/hub/web/static/vendor/xterm/'
ASSETS = ('xterm.js', 'xterm.css', 'addon-fit.js')
LICENSES = ('LICENSE', 'LICENSE.addon-fit')
SHIPPED = ASSETS + LICENSES + ('SOURCES.md',)
D29_PINS = {
    'aiohttp': '3.14.5', 'aiohappyeyeballs': '2.7.1', 'aiosignal': '1.4.0', 'attrs': '26.1.0',
    'frozenlist': '1.8.0', 'idna': '3.20', 'multidict': '7.2.0', 'propcache': '0.5.4',
    'typing-extensions': '4.16.0', 'yarl': '1.25.1', 'pyte': '0.8.2', 'wcwidth': '0.8.4',
}


class PinnedDependencyTests(unittest.TestCase):
    def test_pyproject_pins_exactly_the_d29_set(self):
        project = tomllib.loads((REPO / 'pyproject.toml').read_text())['project']
        pins = dict(item.split('==') for item in project['dependencies'])
        self.assertEqual(len(pins), len(project['dependencies']), 'every dependency uses ==')
        self.assertEqual(pins, D29_PINS)

    def test_installed_runtime_matches_the_pins_and_imports_aiohttp(self):
        for name, version in D29_PINS.items():
            self.assertEqual(metadata.version(name), version, name)
        import aiohttp
        self.assertEqual(aiohttp.__version__, D29_PINS['aiohttp'])

    def test_src_has_no_mcp_sdk_import(self):
        pattern = re.compile(r'^\s*(import mcp\b|from mcp\b)', re.MULTILINE)
        offenders = [str(p.relative_to(REPO)) for p in (REPO / 'src').rglob('*.py')
                     if pattern.search(p.read_text())]
        self.assertEqual(offenders, [])


class VendoredXtermTests(unittest.TestCase):
    def test_assets_and_licenses_exist_and_are_nonempty(self):
        for name in SHIPPED:
            path = VENDOR / name
            self.assertTrue(path.is_file() and path.stat().st_size > 0, name)
        for name in LICENSES:
            self.assertIn('Permission is hereby granted, free of charge', (VENDOR / name).read_text(), name)

    def test_sources_notice_records_version_origin_license_and_matching_sha256(self):
        notice = (VENDOR / 'SOURCES.md').read_text()
        for needle in ('@xterm/xterm', '@xterm/addon-fit', 'https://registry.npmjs.org/', 'MIT'):
            self.assertIn(needle, notice)
        self.assertRegex(notice, r'@xterm/xterm[^\n]*\b5\.\d+\.\d+\b')
        for name in ASSETS + LICENSES:
            digest = sha256((VENDOR / name).read_bytes()).hexdigest()
            self.assertIn(digest, notice, f'SOURCES.md lacks the sha256 of vendored {name}')

    def test_assets_have_no_runtime_external_reference(self):
        for name in ASSETS:
            text = (VENDOR / name).read_text()
            self.assertNotIn('sourceMappingURL', text, f'{name} keeps a source map reference')
            self.assertNotRegex(text, r'url\(\s*["\']?https?://', name)
            self.assertNotRegex(text, r'@import\s+(url\()?["\']?https?://', name)
        for name in ('xterm.js', 'addon-fit.js'):
            self.assertEqual(re.findall(r'https?://[^\s"\')]+', (VENDOR / name).read_text()), [],
                             f'{name} carries a URL literal')


class PackagingConfigTests(unittest.TestCase):
    def test_static_tree_is_declared_package_data_and_in_manifest(self):
        config = tomllib.loads((REPO / 'pyproject.toml').read_text())
        data = config['tool']['setuptools']['package-data']
        self.assertIn('workbench.hub.web', data)
        self.assertTrue(any(p.startswith('static/') for p in data['workbench.hub.web']))
        self.assertTrue((REPO / 'src/workbench/hub/web/__init__.py').is_file())
        manifest = (REPO / 'MANIFEST.in').read_text()
        self.assertRegex(manifest, r'recursive-include src/workbench/hub/web/static\b')


class WheelContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix='ti01-wheel-'))
        cls.addClassCleanup(shutil.rmtree, cls.root, ignore_errors=True)
        source = cls.root / 'source'
        source.mkdir()
        for name in ('src', 'omp_bridge', 'build_support'):
            shutil.copytree(REPO / name, source / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', '_assets'))
        for name in ('pyproject.toml', 'MANIFEST.in'):
            shutil.copy2(REPO / name, source / name)
        env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL')}
        for key, name in (('HOME', 'home'), ('UV_CACHE_DIR', 'cache'), ('TMPDIR', 'tmp')):
            (cls.root / name).mkdir()
            env[key] = str(cls.root / name)
        env.update(PYTHONDONTWRITEBYTECODE='1', UV_PYTHON_DOWNLOADS='never', UV_NO_PROGRESS='1')
        dist = cls.root / 'dist'
        result = subprocess.run(['uv', 'build', '--sdist', '--wheel', '--out-dir', str(dist), str(source)],
                                cwd=cls.root, env=env, text=True, capture_output=True, timeout=240)
        if result.returncode:
            raise AssertionError(f'uv build failed: {result.stdout}\n{result.stderr}')
        cls.wheel = next(dist.glob('*.whl'))
        cls.sdist = next(dist.glob('*.tar.gz'))

    def test_wheel_carries_vendored_assets_byte_for_byte(self):
        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
            for name in SHIPPED:
                self.assertIn(WHEEL_VENDOR + name, names)
                self.assertEqual(archive.read(WHEEL_VENDOR + name), (VENDOR / name).read_bytes(), name)

    def test_wheel_requires_exactly_the_pinned_dependencies(self):
        with zipfile.ZipFile(self.wheel) as archive:
            meta = next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))
            lines = archive.read(meta).decode().splitlines()
        requires = {line.split(': ', 1)[1] for line in lines if line.startswith('Requires-Dist: ')}
        self.assertEqual(requires, {f'{n}=={v}' for n, v in D29_PINS.items()})

    def test_sdist_carries_vendored_assets(self):
        with tarfile.open(self.sdist) as archive:
            names = {m.name.split('/', 1)[1] for m in archive.getmembers() if '/' in m.name}
        for name in SHIPPED:
            self.assertIn('src/' + WHEEL_VENDOR + name, names)


if __name__ == '__main__':
    unittest.main()
