"""Real noneditable artifact boundary; expected bytes come from canonical sources."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[3]
RUN = ROOT / '.workflow/WI-02-attempt-1'


class PackageAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RUN.mkdir(parents=True, exist_ok=True)
        cls.artifact_run = Path(tempfile.mkdtemp(prefix='package-', dir=RUN))
        cls.source = cls.artifact_run / 'source'
        cls.source.mkdir()
        for name in ('src', 'omp_bridge', 'build_support'):
            if (ROOT / name).exists():
                shutil.copytree(ROOT / name, cls.source / name,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', '_assets'))
        for name in ('pyproject.toml', 'MANIFEST.in'):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, cls.source / name)
        cls.cwd = cls.artifact_run / 'research'
        cls.cwd.mkdir()
        cls.env = {key: value for key, value in os.environ.items()
                   if key in ('PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
        for key, name in (('HOME', 'home'), ('UV_TOOL_DIR', 'tools'), ('UV_TOOL_BIN_DIR', 'bin'),
                          ('UV_CACHE_DIR', 'cache'), ('TMPDIR', 'tmp')):
            path = cls.artifact_run / name
            path.mkdir()
            cls.env[key] = str(path)
        cls.env.update(PYTHONDONTWRITEBYTECODE='1', UV_PYTHON_DOWNLOADS='never', UV_NO_PROGRESS='1')
        cls.command(['uv', 'build', '--sdist', '--wheel', '--out-dir', str(cls.artifact_run / 'dist'), str(cls.source)])
        cls.wheel = next((cls.artifact_run / 'dist').glob('*.whl'))
        cls.sdist = next((cls.artifact_run / 'dist').glob('*.tar.gz'))
        cls.command(['uv', 'tool', 'install', '--python', sys.executable, str(cls.sdist)])
        cls.python = cls.artifact_run / 'tools/omp-workbench/bin/python'
        (cls.artifact_run / 'artifact-hashes.json').write_text(json.dumps({
            p.name: sha256(p.read_bytes()).hexdigest() for p in (cls.wheel, cls.sdist)}, indent=2))

    @classmethod
    def command(cls, argv, **kwargs):
        result = subprocess.run(argv, cwd=cls.cwd, env=cls.env, text=True,
                                capture_output=True, timeout=180, **kwargs)
        with (cls.artifact_run / 'commands.log').open('a') as log:
            log.write(json.dumps(argv) + '\n' + result.stdout + result.stderr + f'\nexit={result.returncode}\n')
        if result.returncode:
            raise AssertionError(f'{argv!r} failed: {result.stdout}\n{result.stderr}')
        return result.stdout

    def test_noneditable_artifacts_include_canonical_assets_and_both_help_commands(self):
        # Canonical product classification, independent of package resolver/build code.
        expected = {f'workbench/_assets/omp_bridge/{p.relative_to(ROOT / "omp_bridge")}': p
                    for p in (ROOT / 'omp_bridge').rglob('*') if p.is_file()}
        expected['workbench/_assets/pyproject.toml'] = ROOT / 'pyproject.toml'
        expected.update({f'workbench/{p.relative_to(ROOT / "src/workbench")}': p
                         for p in (ROOT / 'src/workbench').rglob('*')
                         if p.is_file() and p.suffix in ('.py', '.json')})
        with zipfile.ZipFile(self.wheel) as archive:
            missing = sorted(set(expected) - set(archive.namelist()))
            self.assertEqual(missing, [], 'wheel lacks canonical runtime closure')
            for name, original in expected.items():
                self.assertEqual(sha256(archive.read(name)).hexdigest(), sha256(original.read_bytes()).hexdigest(), name)
            self.assertFalse(any('/.agents/' in name or '/.codex/' in name for name in archive.namelist()))
        for name in ('wb', 'omp-workbench'):
            output = self.command([str(self.artifact_run / 'bin' / name), '--help'])
            self.assertIn('start', output)
        self.assertEqual(list(self.cwd.iterdir()), [])
        location = self.command([str(self.python), '-I', '-c', 'import workbench; print(workbench.__file__)']).strip()
        self.assertTrue(location.startswith(str(self.artifact_run / 'tools')))
        self.assertNotIn(str(self.source), location)


    def test_installed_default_assets_are_readable_and_role_filtered_outside_checkout(self):
        output = self.command([str(self.python), '-I', '-c', r"""
import json
from hashlib import sha256
from workbench.backend.launcher import (default_bridge_extension, default_isolation_overlay,
    default_skills_dir, default_models_file, default_agents_dir, role_skill_allowlist, load_model_table)
from workbench.backend.omp_home import default_agents_dir as home_agents
from workbench.backend.summary_adapter import skill_bytes
paths = [default_bridge_extension(), default_isolation_overlay(), default_skills_dir(),
         default_models_file(), default_agents_dir()]
files = {p for path in paths for p in (path.rglob('*') if path.is_dir() else [path]) if p.is_file()}
assert home_agents() == default_agents_dir()
assert load_model_table()['manager']['models']['default']
print(json.dumps({'files': {str(p): sha256(p.read_bytes()).hexdigest() for p in files},
    'skills': str(default_skills_dir()), 'summary': sha256(skill_bytes()).hexdigest(),
    'manager': role_skill_allowlist('manager'), 'worker': role_skill_allowlist('worker')}))
"""])
        result = json.loads(output)
        self.assertEqual(result['manager'], ['to-worker', 'workbench-recovery', 'workbench-report'])
        self.assertEqual(result['worker'], ['to-manager'])
        self.assertEqual(result['summary'], sha256((ROOT / 'omp_bridge/skills/important-log-summary/SKILL.md').read_bytes()).hexdigest())
        for path, digest in result['files'].items():
            self.assertTrue(path.startswith(str(self.artifact_run / 'tools')))
            relative = path.split('/_assets/omp_bridge/', 1)[1]
            self.assertEqual(digest, sha256((ROOT / 'omp_bridge' / relative).read_bytes()).hexdigest())
        skills = Path(result['skills'])
        self.assertIn('disableModelInvocation: true', (skills / 'workbench-report/SKILL.md').read_text())
        for path in skills.rglob('*'):
            path.chmod(0o555 if path.is_dir() else 0o444)
        output = self.command([str(self.python), '-I', str(skills / 'workbench-report/scripts/report_io.py'), '--help'])
        self.assertIn('--project-dir', output)
        # Execute the fixed helper against an independent draft with canonical headings.
        reference = ROOT / 'omp_bridge/skills/workbench-report/references/report-template.md'
        draft = '# WB problem report\n' + ''.join(line + '\n미확인\n\n'
            for line in reference.read_text().splitlines() if line.startswith('## '))
        output = self.command([str(self.python), '-I', str(skills / 'workbench-report/scripts/report_io.py'),
            '--project-dir', str(self.cwd), '--output', str(self.cwd / 'report.md')], input=draft)
        saved = json.loads(output)
        self.assertEqual(saved['status'], 'saved')
        self.assertEqual(Path(saved['path']).read_text(), draft)
        Path(saved['path']).unlink()
        self.assertEqual(list(self.cwd.iterdir()), [])


    def test_installed_summary_identity_fingerprints_canonical_inputs_and_rejects_missing_seams(self):
        output = self.command([str(self.python), '-I', '-c', r"""
import json
from workbench.backend.summary_compatibility import collect_runtime_identity
print(json.dumps(collect_runtime_identity()))
"""])
        identity = json.loads(output)
        self.assertEqual(identity['unknowns'], [])
        self.assertEqual(identity['policy_sha256'], sha256((ROOT / 'src/workbench/contracts/summary_compat_policy_v1.json').read_bytes()).hexdigest())
        expected = {p.relative_to(ROOT).as_posix(): sha256(p.read_bytes()).hexdigest()
                    for base in ('src/workbench', 'omp_bridge') for p in (ROOT / base).rglob('*')
                    if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
        expected['pyproject.toml'] = sha256((ROOT / 'pyproject.toml').read_bytes()).hexdigest()
        observed = {name: entry['sha256'] for name, entry in identity['runtime_entries'].items() if entry['kind'] == 'file'}
        self.assertEqual(observed, expected)
        output = self.command([str(self.python), '-I', '-c', r"""
import json
import tempfile
from pathlib import Path
from workbench.assets import runtime_root
from workbench.backend.summary_compat_runtime import _copy_runtime
from workbench.backend.summary_compatibility import collect_runtime_identity
with tempfile.TemporaryDirectory() as directory:
    snapshot = _copy_runtime(runtime_root(), Path(directory))
    before = collect_runtime_identity(snapshot)
    bridge = snapshot / 'omp_bridge/g3/bridge.ts'
    bridge.write_bytes(bridge.read_bytes() + b'\n// changed canonical input\n')
    changed = collect_runtime_identity(snapshot)
    bridge.unlink()
    missing = collect_runtime_identity(snapshot)
    print(json.dumps({'before': before['unknowns'], 'changed': before['closure_digest'] != changed['closure_digest'],
                      'missing': missing['unknowns'], 'python': (snapshot / 'src/workbench/backend/summary_writer.py').is_file()}))
"""])
        result = json.loads(output)
        self.assertEqual(result['before'], [])
        self.assertTrue(result['changed'])
        self.assertTrue(result['python'])
        self.assertIn('missing_required_seam:omp_bridge/g3/bridge.ts', result['missing'])


    def test_installed_sdk_dispatch_reads_policy_and_fixed_python_helpers(self):
        # Reuse the public dispatch fixture; its only double is the external OMP SDK.
        package = Path(self.command([str(self.python), '-I', '-c',
            'import workbench; from pathlib import Path; print(Path(workbench.__file__).parent)']).strip())
        bridge = package / '_assets/omp_bridge'
        fixture = (ROOT / 'tests/bridge/summary_sdk.test.ts').read_text()
        fixture = fixture.replace('../../omp_bridge/contract/logs_v1.ts', (bridge / 'contract/logs_v1.ts').as_uri())
        fixture = fixture.replace('../../omp_bridge/g3/summary_sdk.ts', (bridge / 'g3/summary_sdk.ts').as_uri())
        fixture = fixture.replace('"omp_bridge/skills/important-log-summary/SKILL.md"',
                                  json.dumps(str(bridge / 'skills/important-log-summary/SKILL.md')))
        path = self.artifact_run / 'installed-summary.test.ts'
        path.write_text(fixture)
        self.command(['node', '--experimental-strip-types', '--test', '--test-name-pattern',
                      'normal result writes only', str(path)])
        for argv in ([str(self.python), '-I', '-m', 'workbench.backend.summary_writer'],
                     [str(self.python), str(package / 'terminal/shell_g2/lifecycle.py')]):
            result = subprocess.run(argv, cwd=self.cwd, env=self.env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn('Traceback', result.stderr)




    def test_installed_omp_home_links_agent_definitions_to_canonical_resources(self):
        self.addCleanup(shutil.rmtree, self.cwd / 'data', ignore_errors=True)
        output = self.command([str(self.python), '-I', '-c', r"""
import json
import os
from pathlib import Path
from workbench.backend.launcher import default_agents_dir, default_skills_dir, ISOLATION_PROVIDER_IDS
from workbench.backend.omp_home import prepare_omp_home
root = Path.cwd() / 'data'
home = prepare_omp_home(root, dict(os.environ), skills_dir=default_skills_dir(), provider_ids=ISOLATION_PROVIDER_IDS)
print(json.dumps({'agents': {str(p): {'link': p.is_symlink(), 'target': str(p.resolve())}
    for p in (home.agent_dir / 'agents').glob('*.md')}, 'canonical': str(default_agents_dir())}))
"""])
        result = json.loads(output)
        self.assertTrue(result['agents'])
        for path, entry in result['agents'].items():
            self.assertTrue(entry['link'], path)
            self.assertEqual(entry['target'], str(Path(result['canonical']) / Path(path).name))
        shutil.rmtree(self.cwd / 'data')


if __name__ == '__main__':
    unittest.main()
