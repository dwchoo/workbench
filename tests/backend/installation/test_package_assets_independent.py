"""Independent Git-ref install and fail-closed installed-resource scenarios.

Only subprocesses use the product, with isolated homes and no source PYTHONPATH.
Real SDK compatibility is assessed separately from these packaging checks.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


def _remove_tree(path):
    """Delete a build tree whose installed files may be read-only (0o444/0o555)."""
    if os.environ.get('WB_KEEP_PACKAGE_TEST_DIRS') == '1':
        print(f'kept package test dir (WB_KEEP_PACKAGE_TEST_DIRS=1): {path}', file=sys.stderr)
        return
    def onexc(function, target, _exc):
        parent = os.path.dirname(target)
        os.chmod(parent, 0o700)
        if os.path.isdir(target) and not os.path.islink(target):
            os.chmod(target, 0o700)
        function(target)
    shutil.rmtree(path, onexc=onexc)


class GitInstalledAssetsTests(unittest.TestCase):
    @classmethod
    def command(cls, argv, *, input=None, timeout=180):
        import time

        # Regular files cannot be held open by descendants like PIPE readers.
        # Never poll/communicate/wait before cleanup: the unreaped child reserves
        # its PID/PGID even after exit, so killpg cannot target a reused identity.
        limit = 8 * 1024 * 1024
        with tempfile.TemporaryFile(mode='w+', dir=cls.env['TMPDIR']) as stdin_file, \
                tempfile.TemporaryFile(mode='w+', dir=cls.env['TMPDIR']) as stdout_file, \
                tempfile.TemporaryFile(mode='w+', dir=cls.env['TMPDIR']) as stderr_file:
            if input is not None:
                stdin_file.write(input)
            stdin_file.seek(0)
            process = subprocess.Popen(argv, cwd=cls.project, env=cls.env, text=True,
                                       stdin=stdin_file, stdout=stdout_file,
                                       stderr=stderr_file, start_new_session=True)
            deadline = time.monotonic() + timeout
            try:
                while os.waitid(os.P_PID, process.pid,
                                os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                    if any(os.fstat(stream.fileno()).st_size > limit
                           for stream in (stdout_file, stderr_file)):
                        raise AssertionError(f'{argv!r}: command output exceeds {limit} bytes')
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    time.sleep(0.01)
            finally:
                try:
                    # ChildProcessError means some other owner reaped the child;
                    # fail closed rather than signal an unreserved numeric PGID.
                    os.waitid(os.P_PID, process.pid,
                              os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except ChildProcessError:
                    pass
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    finally:
                        process.wait(timeout=10)
            if any(os.fstat(stream.fileno()).st_size > limit
                   for stream in (stdout_file, stderr_file)):
                raise AssertionError(f'{argv!r}: command output exceeds {limit} bytes')
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(limit + 1)
            stderr = stderr_file.read(limit + 1)
            if len(stdout) > limit or len(stderr) > limit:
                raise AssertionError(f'{argv!r}: command output exceeds {limit} bytes')
        with (cls.artifact_run / 'commands.jsonl').open('a') as log:
            log.write(json.dumps({'argv':argv,'exit':process.returncode,'stdout':stdout,'stderr':stderr})+'\n')
        if process.returncode:
            raise AssertionError(f'{argv!r}: exit {process.returncode}\n{stdout}\n{stderr}')
        return stdout

    @classmethod
    def setUpClass(cls):
        # Build tree under TMPDIR; removed on success and failure (set WB_KEEP_PACKAGE_TEST_DIRS=1 to keep it).
        cls.artifact_run = Path(tempfile.mkdtemp(prefix='git-install-'))
        cls.addClassCleanup(_remove_tree, cls.artifact_run)
        cls.source = cls.artifact_run / 'source'
        cls.source.mkdir()
        for name in ('src', 'omp_bridge', 'build_support'):
            shutil.copytree(ROOT / name, cls.source / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', '_assets'))
        for name in ('pyproject.toml','MANIFEST.in'):
            shutil.copy2(ROOT / name, cls.source / name)
        # Expected bytes are canonical source inputs, not resolver output.
        cls.expected = {str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest()
                        for base in ('src/workbench','omp_bridge') for p in (ROOT/base).rglob('*')
                        if p.is_file() and '__pycache__' not in p.parts and '_assets' not in p.parts
                        and p.suffix != '.pyc'}
        cls.expected['pyproject.toml'] = sha256((ROOT/'pyproject.toml').read_bytes()).hexdigest()
        (cls.artifact_run/'expected-canonical-hashes.json').write_text(json.dumps(cls.expected,indent=2)+'\n')
        cls.project = cls.artifact_run / 'research'
        cls.project.mkdir()
        (cls.project/'sentinel').write_bytes(b'independent research data\n')
        (cls.project/'workbench.py').write_text("raise RuntimeError('project-shadow-imported')\n")
        cls.env = {k:v for k,v in os.environ.items()
                   if k in ('PATH','LANG','LC_ALL','SSL_CERT_FILE','SSL_CERT_DIR')}
        for key, name in (('HOME','home'),('XDG_CONFIG_HOME','config'),('XDG_DATA_HOME','data'),
                          ('XDG_CACHE_HOME','xdg-cache'),('XDG_RUNTIME_DIR','runtime'),
                          ('UV_TOOL_DIR','tools'),('UV_TOOL_BIN_DIR','bin'),('UV_CACHE_DIR','uv-cache'),
                          ('TMPDIR','tmp')):
            folder=cls.artifact_run/name;folder.mkdir(mode=0o700)
            cls.env[key]=str(folder)
        cls.env.update(PYTHONDONTWRITEBYTECODE='1',UV_PYTHON_DOWNLOADS='never',UV_NO_PROGRESS='1',
                       WORKBENCH_HUB_AUTOSTART='0',  # HB-11: the installed wb must not leave a Hub behind
                       GIT_AUTHOR_NAME='Owned packaging fixture',GIT_AUTHOR_EMAIL='fixture@localhost',
                       GIT_COMMITTER_NAME='Owned packaging fixture',GIT_COMMITTER_EMAIL='fixture@localhost')
        cls.command(['git','-c','core.hooksPath=/dev/null','init',str(cls.source)])
        cls.command(['git','-C',str(cls.source),'-c','core.hooksPath=/dev/null','add','--all'])
        cls.command(['git','-C',str(cls.source),'-c','core.hooksPath=/dev/null','commit','-m','owned exact packaging fixture'])
        cls.commit=cls.command(['git','-C',str(cls.source),'rev-parse','HEAD']).strip()
        cls.command(['uv','tool','install','--python',sys.executable,
                     'git+'+cls.source.as_uri()+'@'+cls.commit])
        cls.python=cls.artifact_run/'tools/workbench/bin/python'
        cls.package=Path(cls.command([str(cls.python),'-I','-c',
                     'import workbench; from pathlib import Path; print(Path(workbench.__file__).parent)']).strip())
        # Make the original source location unavailable, then use only installed files.
        cls.source.rename(cls.artifact_run/'source-unavailable')
        for p in cls.package.rglob('*'):
            if not p.is_symlink():
                p.chmod(0o555 if p.is_dir() else 0o444)
        cls.package.chmod(0o555)
        cls.package_hashes={str(p.relative_to(cls.package)):sha256(p.read_bytes()).hexdigest()
                            for p in cls.package.rglob('*') if p.is_file()}

    def test_exact_git_ref_is_noneditable_and_help_and_resources_work_readonly_outside_checkout(self):
        report=json.loads(self.command([str(self.python),'-I','-c',r'''
from hashlib import sha256
from importlib.metadata import distribution
import json
from workbench.assets import canonical_path, package_root
from workbench.backend.summary_compatibility import collect_runtime_identity
from workbench.backend.summary_adapter import skill_bytes
identity=collect_runtime_identity()
print(json.dumps({'direct':json.loads(distribution('workbench').read_text('direct_url.json')),
                  'package':str(package_root()),'unknowns':identity['unknowns'],
                  'entries':{k:v['sha256'] for k,v in identity['runtime_entries'].items() if v['kind']=='file'},
                  'summary':sha256(skill_bytes()).hexdigest()}))
''']))
        self.assertEqual(report['direct']['vcs_info']['commit_id'],self.commit)
        self.assertEqual(report['direct']['vcs_info']['vcs'],'git')
        self.assertFalse(report['direct'].get('dir_info',{}).get('editable',False))
        self.assertEqual(report['package'],str(self.package))
        self.assertEqual(report['unknowns'],[])
        self.assertEqual(report['entries'],self.expected)
        self.assertEqual(report['summary'],self.expected['omp_bridge/skills/important-log-summary/SKILL.md'])
        for name in ('wb','omp-workbench'):
            self.assertIn('start',self.command([str(self.artifact_run/'bin'/name),'--help']))
        self.assertEqual({p.name for p in self.project.iterdir()},{'sentinel','workbench.py'})
        self.assertEqual((self.project/'sentinel').read_bytes(),b'independent research data\n')
        self.assertFalse((self.project/'.wb').exists())
        self.assertEqual({str(p.relative_to(self.package)):sha256(p.read_bytes()).hexdigest()
                          for p in self.package.rglob('*') if p.is_file()},self.package_hashes)

    def test_missing_explicit_frozen_policy_fails_instead_of_falling_back_to_installed_assets(self):
        report=json.loads(self.command([str(self.python),'-I','-c',r'''
import json
from pathlib import Path
from workbench.assets import canonical_path
from workbench.backend.summary_compatibility import collect_runtime_identity
root=Path('missing-policy-snapshot').resolve();root.mkdir()
logical='src/workbench/contracts/summary_compat_policy_v1.json'
selected=canonical_path(logical,root)
try:
    collect_runtime_identity(root)
except FileNotFoundError:
    failed=True
else:
    failed=False
print(json.dumps({'selected':str(selected),'expected':str(root/logical),'failed':failed}))
''']))
        self.assertEqual(report['selected'],report['expected'])
        self.assertTrue(report['failed'],'explicit incomplete snapshot silently used installed policy')
        shutil.rmtree(self.project/'missing-policy-snapshot')

    def test_public_identity_detects_missing_snapshot_seam_and_rejects_symlinked_policy(self):
        report=json.loads(self.command([str(self.python),'-I','-c',r'''
import json, shutil
from pathlib import Path
from workbench.assets import package_root
from workbench.backend.summary_compatibility import collect_runtime_identity
package=package_root();root=Path('detached').resolve();root.mkdir()
shutil.copytree(package,root/'src/workbench',ignore=shutil.ignore_patterns('_assets','__pycache__'))
shutil.copytree(package/'_assets/omp_bridge',root/'omp_bridge')
shutil.copy2(package/'_assets/pyproject.toml',root/'pyproject.toml')
for p in root.rglob('*'):
    if p.is_dir():p.chmod(0o700)
before=collect_runtime_identity(root)
seam=root/'omp_bridge/g3/bridge.ts';seam.unlink()
after=collect_runtime_identity(root)
policy=root/'src/workbench/contracts/summary_compat_policy_v1.json'
policy.unlink();policy.symlink_to(package/'contracts/summary_compat_policy_v1.json')
try:
    collect_runtime_identity(root)
except ValueError:
    rejected=True
else:
    rejected=False
print(json.dumps({'before':before['unknowns'],'after':after['unknowns'],
                  'changed':before['runtime_digest']!=after['runtime_digest'],'rejected':rejected}))
''']))
        self.assertEqual(report['before'],[])
        self.assertIn('missing_required_seam:omp_bridge/g3/bridge.ts',report['after'])
        self.assertTrue(report['changed'])
        self.assertTrue(report['rejected'],'symlinked frozen policy was trusted')
        # Copies were read-only; restore only our detached copy for cleanup.
        detached=self.project/'detached'
        for p in detached.rglob('*'):
            if p.is_dir():p.chmod(0o700)
        shutil.rmtree(detached)


if __name__=='__main__':
    unittest.main()
